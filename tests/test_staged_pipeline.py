"""The staged knowledge-graph pipeline: EXTRACT (stage 1, stored raw) is
separate from and prior to GOVERN (stage 3, marks not aborts).

Governance used to be interleaved with extraction: 17 checks ran inside one
``_validate_memory`` pass, and only survivors of every check ever became a
graph row.  A policy disagreement between extraction and a downstream
governance re-check could therefore abort an entire formation run and yield
zero facts, even though extraction itself worked correctly.

This file pins down the restructure's contract:

1. Stage 1 (structurally valid candidates) is stored, countable, and
   inspectable via the raw/quarantine store — independent of what stage 3
   later decides.
2. A stage-3 governance failure MARKS the stored row (quarantined); it never
   aborts the run, and the row is never deleted.
3. Re-governing an already-stored raw graph changes verdicts WITHOUT calling
   the extraction transport again (no re-extraction, no LLM).
4. The three specific disagreement classes that used to abort a run
   (claim-mode mismatch, memory_type resolution disagreement, an
   unresolvable/"open predicate" relationship type at materialization) all
   now degrade non-fatally.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    MemoryScope,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.config import default_config
from memotron.models import DreamJobKind, Episode, ExtractedMemory, QuarantineStatus
from memotron.receipts import ReceiptDecisionType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(scope_id: str = "acme") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _client(tmp_path: Path, config: DreamConfig | None = None) -> Memotron:
    return Memotron(
        graph_path=tmp_path / "staged.sqlite",
        config=config if config is not None else default_config(),
    )


def _batch(*memories: dict) -> str:
    return json.dumps({"memories": list(memories)})


def _confidence_floor_config(min_confidence: float = 0.8) -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind",),
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                min_confidence=min_confidence,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
    )


def _receipts_of(client: Memotron, scope: MemoryScope, decision_type: ReceiptDecisionType):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == decision_type.value
    ]


class _PoisonExtractionTransport:
    """Raises if ever asked to extract — proves a code path made NO transport
    call (no re-extraction, no LLM)."""

    async def extract_memories(self, request):
        raise AssertionError("extraction transport was called — this path must not re-extract")


# ---------------------------------------------------------------------------
# 1. Stage 1 stores a candidate that stage 3 later rejects
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage1_stores_every_structurally_valid_candidate_countable(
    tmp_path: Path,
) -> None:
    """Every structurally valid candidate lands in the raw/quarantine store —
    the countable stage-1 artifact — regardless of what stage 3 later decides.
    One candidate passes governance (PROMOTED); one fails it
    (confidence_below_minimum, QUARANTINED).  Both are stored; neither is
    dropped without a trace."""
    client = _client(tmp_path, _confidence_floor_config())
    scope = _scope()
    await client.add_episode(
        name="mixed",
        episode_body=_batch(
            {
                "subject": "Acme Parks",
                "predicate": "requires",
                "object": "SOC2 report",
                "relationship_type": "REQUIRES",
                "confidence": 0.93,
            },
            {
                "subject": "Acme Parks",
                "predicate": "requires",
                "object": "a signed indemnification form",
                "relationship_type": "REQUIRES",
                "confidence": 0.5,
            },
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    counts = client.graph.quarantine_counts(scope_key=scope.key)
    assert counts == {"promoted": 1, "quarantined": 1}

    quarantined = client.graph.quarantined_candidates(scope_key=scope.key, status=QuarantineStatus.QUARANTINED)
    assert len(quarantined) == 1
    stored = quarantined[0]
    # Retained and inspectable, not deleted: the exact candidate the model
    # produced is still queryable raw, with the governance verdict attached.
    assert stored.subject == "Acme Parks"
    assert stored.object == "a signed indemnification form"
    assert stored.reason == "confidence_below_minimum"
    assert stored.status is QuarantineStatus.QUARANTINED

    promoted = client.graph.quarantined_candidates(scope_key=scope.key, status=QuarantineStatus.PROMOTED)
    assert len(promoted) == 1
    assert promoted[0].promoted_relationship_uuid is not None
    live = client.graph.get_relationship(promoted[0].promoted_relationship_uuid)
    assert live.properties["object"] == "SOC2 report"


# ---------------------------------------------------------------------------
# 2. A governance failure marks rather than aborts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confidence_governance_failure_marks_not_aborts(tmp_path: Path) -> None:
    """The whole point: a stage-3 governance failure must never abort the run
    or cost the run's other candidates."""
    client = _client(tmp_path, _confidence_floor_config())
    scope = _scope("marks-not-aborts")
    await client.add_episode(
        name="two-candidates",
        episode_body=_batch(
            {
                "subject": "checkout-api",
                "predicate": "requires",
                "object": "reservation lookup under 200ms",
                "relationship_type": "REQUIRES",
                "confidence": 0.5,  # below the 0.8 floor -- governance failure
            },
            {
                "subject": "checkout-api",
                "predicate": "requires",
                "object": "p99 latency under 500ms",
                "relationship_type": "REQUIRES",
                "confidence": 0.95,
            },
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    run = await client.run_dream_job(job_name="formation-default")

    assert run.processed_episodes == 1
    assert run.created_relationships == 1
    quarantined = _receipts_of(client, scope, ReceiptDecisionType.CANDIDATE_QUARANTINED)
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].event_payload)["violation"] == "confidence_below_minimum"


# ---------------------------------------------------------------------------
# 3. Re-govern changes verdicts WITHOUT re-extraction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_regovern_promotes_a_quarantined_candidate_without_reextraction(
    tmp_path: Path,
) -> None:
    """Widening min_confidence and re-governing must flip the verdict using
    ONLY the stored raw candidate — the extraction transport is swapped for
    one that raises if ever called, so a passing test proves zero re-extraction."""
    client = _client(tmp_path, _confidence_floor_config(min_confidence=0.8))
    scope = _scope("regovern")
    await client.add_episode(
        name="too-low",
        episode_body=_batch(
            {
                "subject": "checkout-api",
                "predicate": "requires",
                "object": "reservation ledger backup nightly",
                "relationship_type": "REQUIRES",
                "confidence": 0.5,
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()
    assert client.graph.quarantine_counts(scope_key=scope.key) == {"quarantined": 1}
    assert not [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]

    # Widen the floor -- a policy change, not new evidence -- and make ANY
    # further extraction call fail the test.
    client._engine._config = _confidence_floor_config(min_confidence=0.3)
    client._engine._extractor._transport = _PoisonExtractionTransport()

    result = await client._engine.regovern_scope(scope=scope, now=datetime.now(UTC))

    assert result.candidates_considered == 1
    assert result.promoted == 1
    assert result.still_quarantined == 0
    assert client.graph.quarantine_counts(scope_key=scope.key) == {"promoted": 1}
    live = [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]
    assert len(live) == 1
    assert live[0].properties["object"] == "reservation ledger backup nightly"
    assert live[0].properties["confidence"] == 0.5


@pytest.mark.asyncio
async def test_regovern_leaves_a_still_failing_candidate_quarantined(tmp_path: Path) -> None:
    """A candidate that still fails under the new policy stays quarantined,
    re-stamped with the fresh reason -- re-govern is not a rubber stamp."""
    client = _client(tmp_path, _confidence_floor_config(min_confidence=0.8))
    scope = _scope("regovern-still-bad")
    await client.add_episode(
        name="too-low",
        episode_body=_batch(
            {
                "subject": "checkout-api",
                "predicate": "requires",
                "object": "an unreviewed guess",
                "relationship_type": "REQUIRES",
                "confidence": 0.1,
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    # Raise the floor slightly -- still above 0.1 -- so the candidate is
    # re-governed but still fails.
    client._engine._config = _confidence_floor_config(min_confidence=0.5)
    client._engine._extractor._transport = _PoisonExtractionTransport()

    result = await client._engine.regovern_scope(scope=scope, now=datetime.now(UTC))

    assert result.promoted == 0
    assert result.still_quarantined == 1
    assert client.graph.quarantine_counts(scope_key=scope.key) == {"quarantined": 1}


# ---------------------------------------------------------------------------
# 4. The three previously-fatal cases now degrade non-fatally
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_mode_mismatch_coerces_and_never_aborts(tmp_path: Path) -> None:
    """An extractor-supplied claim_mode the deterministic memory_type forbids
    (a REQUIRES/requirement candidate tagged claim_mode=directive) is coerced,
    receipted, and materializes -- it never raises through either the
    extraction-time path or the shared _claim_mode_for materialize uses."""
    client = _client(tmp_path)
    scope = _scope("claim-mode")
    await client.add_episode(
        name="mismatched-claim-mode",
        episode_body=_batch(
            {
                "subject": "Acme Parks",
                "predicate": "requires",
                "object": "SOC2 report",
                "relationship_type": "REQUIRES",
                "confidence": 0.9,
                "claim_mode": "directive",
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    live = [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]
    assert len(live) == 1
    assert live[0].properties["claim_mode"] == "requirement"

    coerced = _receipts_of(client, scope, ReceiptDecisionType.CANDIDATE_CLAIM_MODE_COERCED)
    assert len(coerced) == 1
    payload = json.loads(coerced[0].event_payload)
    assert payload["claim_mode_before"] == "directive"
    assert payload["claim_mode_after"] == "requirement"


@pytest.mark.asyncio
async def test_unresolvable_relationship_type_at_materialization_quarantines_not_raises(
    tmp_path: Path,
) -> None:
    """The 'open predicate' / memory_type-disagreement class: a candidate whose
    relationship_type/endpoint labels match NO instruction cannot occur through
    the public write path (extraction's own real-label matcher would already
    have rejected it), so this reaches directly into _materialize_episode --
    the stage-3 boundary the staged pipeline restructure made non-fatal -- to
    prove the degraded path.  raw_candidates_stored=True (the _run_formation
    shape) must quarantine and continue; the operator-write default
    (raw_candidates_stored=False) must still fail fast, unchanged."""
    client = _client(tmp_path)
    scope = _scope("open-predicate")
    episode = Episode(
        name="direct-materialize",
        body="{}",
        source=EpisodeType.JSON,
        source_description="unit",
        scope=scope,
    )
    good = ExtractedMemory(
        subject="Acme Parks",
        predicate="requires",
        object="SOC2 report",
        relationship_type="REQUIRES",
        confidence=0.9,
    )
    unresolvable = ExtractedMemory(
        subject="Acme Parks",
        predicate="orbits",
        object="Jupiter",
        relationship_type="NO_SUCH_RELATIONSHIP_TYPE",
        confidence=0.9,
    )

    receipt_run = client.graph.receipts.begin_run(
        run_kind="operator",
        job_name="test-direct-materialize",
        scope_key=scope.key,
        effective_policy_digest="test",
    )
    _created_nodes, created_relationships, _reinforced, _superseded = client._engine._materialize_episode(
        episode,
        [good, unresolvable],
        receipt_run=receipt_run,
        raw_candidates_stored=True,
    )
    assert created_relationships == 1
    live = [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]
    assert [r.properties["object"] for r in live] == ["SOC2 report"]

    quarantined = _receipts_of(client, scope, ReceiptDecisionType.CANDIDATE_QUARANTINED)
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].event_payload)["violation"] == "relationship_type_not_allowed"

    # The operator-write default must still fail fast -- unchanged behaviour.
    receipt_run_2 = client.graph.receipts.begin_run(
        run_kind="operator",
        job_name="test-direct-materialize-2",
        scope_key=scope.key,
        effective_policy_digest="test",
    )
    with pytest.raises(ValueError, match="matches no instruction"):
        client._engine._materialize_episode(
            episode,
            [unresolvable],
            receipt_run=receipt_run_2,
        )


@pytest.mark.asyncio
async def test_memory_type_resolution_agrees_across_the_formation_pipeline(
    tmp_path: Path,
) -> None:
    """The root cause of the disagreement bugs: multiple call sites resolved
    memory_type via a wildcard-label lookup that could miss an open_predicate
    instruction with real (non-'*') endpoint labels, disagreeing with
    materialization's real-label matcher.  An open-predicate instruction whose
    endpoints are NOT '*' must resolve identically (and non-fatally) end to
    end: extracted, scored, and materialized under the SAME memory_type."""
    from memotron.models import MemoryType

    # An open-predicate instruction with REAL (non-"*") endpoint labels: the
    # exact shape the wildcard-label lookup (subject_label="*",
    # object_label="*") could never match, since _label_admits("Entity", "*")
    # is False in both directions.  memory_type is explicit so this does not
    # depend on the built-in RELATIONSHIP_TYPE_MEMORY_TYPE_MAP either.
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(NodeInstruction(label="Entity", query="q", properties=()),),
        relationship_instructions=(
            RelationshipInstruction(
                type="*",
                source_label="Entity",
                target_label="Entity",
                query="Any verb phrase between two entities is a directive.",
                open_predicate=True,
                memory_type=MemoryType.DIRECTIVE,
            ),
        ),
    )
    config = DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
    )
    client = _client(tmp_path, config)
    scope = _scope("open-predicate-e2e")
    await client.add_episode(
        name="open-predicate",
        episode_body=_batch(
            {
                "subject": "Acme Parks",
                "predicate": "must escalate",
                "object": "P1 tickets within an hour",
                "relationship_type": "MUST_ESCALATE",
                "confidence": 0.9,
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    live = [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]
    assert len(live) == 1
    assert live[0].properties["memory_type"] == "directive"
    assert not _receipts_of(client, scope, ReceiptDecisionType.CANDIDATE_QUARANTINED)


#: A token the raw-secret detector recognises (`sk-` + 16 or more chars). Kept as a
#: constant so the "it never reaches the graph" assertion below searches for the exact
#: string that was submitted, not a paraphrase of it.
RAW_CREDENTIAL = "sk-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.mark.parametrize("raw_candidates_stored", [False, True], ids=["operator_write", "formation"])
@pytest.mark.asyncio
async def test_a_raw_credential_costs_one_candidate_not_the_whole_run(
    tmp_path: Path, raw_candidates_stored: bool
) -> None:
    """A rejected credential must skip its own candidate and nothing else.

    This path had NO test. Coverage measured 2026-08-30: the `except ValueError` that
    catches the secret detector, and the `continue` after it, were unexecuted across the
    entire suite -- `test_retention_safety.py`'s RAW_SECRET_REJECTED assertion comes
    through a different call path.

    It matters more than an ordinary uncovered branch because the code comment records
    what happened when this behaved differently: re-raising here aborted a 119-episode
    ingest after three facts had materialized, and a corpus that DOCUMENTS credentials
    is exactly where the detector fires most, so fatality guaranteed the corpus most
    needing ingest was the one that could never finish.

    Both regimes are parametrized because the rejection is deliberately NOT governed by
    `raw_candidates_stored` -- unlike the unresolvable-instruction path directly above
    it, which is. Pinning both stops a future refactor from "tidying" the two into one.

    Three properties, in order of how bad it is to lose them:
      1. the credential never reaches the graph      (the security contract)
      2. the sibling candidate still materializes    (the incident)
      3. the refusal is receipted                    (the audit trail)
    """
    client = _client(tmp_path)
    scope = _scope("raw-credential")
    episode = Episode(
        name="direct-materialize-secret",
        body="{}",
        source=EpisodeType.JSON,
        source_description="unit",
        scope=scope,
    )
    good = ExtractedMemory(
        subject="Acme Parks",
        predicate="requires",
        object="SOC2 report",
        relationship_type="REQUIRES",
        confidence=0.9,
    )
    credential = ExtractedMemory(
        subject="Acme Parks",
        predicate="requires",
        object=f"the billing key {RAW_CREDENTIAL}",
        relationship_type="REQUIRES",
        confidence=0.9,
    )

    receipt_run = client.graph.receipts.begin_run(
        run_kind="operator",
        job_name="test-raw-credential",
        scope_key=scope.key,
        effective_policy_digest="test",
    )
    _nodes, created_relationships, _reinforced, _superseded = client._engine._materialize_episode(
        episode,
        [credential, good],  # credential FIRST: if it aborts, `good` never runs
        receipt_run=receipt_run,
        raw_candidates_stored=raw_candidates_stored,
    )

    # 2. the incident: the sibling survived the rejection
    assert created_relationships == 1, (
        "the rejected credential took its sibling down with it -- this is the 119-episode ingest abort, back again"
    )
    live = [r for r in client.graph.active_relationships(scope=scope) if r.type != "MENTIONS"]
    assert [r.properties["object"] for r in live] == ["SOC2 report"]

    # 1. the security contract: the value is nowhere, in any plane
    for relationship in client.graph.relationships():
        for value in relationship.properties.values():
            assert RAW_CREDENTIAL not in str(value), f"the raw credential reached the graph on {relationship.uuid}"
    for receipt in client.graph.receipts.receipts_for_scope(scope.key):
        assert RAW_CREDENTIAL not in str(receipt.event_payload), (
            "the raw credential reached the receipt payload -- receipts are a content plane too"
        )

    # 3. the audit trail
    rejected = _receipts_of(client, scope, ReceiptDecisionType.FORMATION_RAW_SECRET_REJECTED)
    assert len(rejected) == 1, f"expected exactly one rejection receipt, got {len(rejected)}"
