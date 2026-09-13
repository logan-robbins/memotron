"""Per-candidate schema rejection is non-fatal, receipted, and sibling-safe ([0024]).

Real models invent a node label, a property key, or a relationship type from
time to time.  Validating an episode's candidates in one all-or-nothing pass
turned that into a run-aborting failure: a single non-conforming candidate
discarded every good candidate extracted from the same episode and left the
episode unprocessed.  Against a ~1,000-episode knowledge-base ingest that is a
fuse in the wrong place.

The contract these tests pin down:

1. One bad candidate is DROPPED; its siblings still materialize.
2. Every drop emits exactly one ``CANDIDATE_SCHEMA_REJECTED`` receipt naming
   the specific violation — no candidate is silently dropped.
3. An episode whose candidates are ALL rejected completes cleanly (zero
   memories, every rejection receipted, run checkpointed) instead of raising.
4. A malformed response ENVELOPE is still fatal.  That is a transport/contract
   failure, not a candidate-level one.
5. Under crypto-shred governance the detailed reason is withheld and sealed by
   the ledger choke point, while the machine violation code — content-free by
   construction — survives.
6. The operator-facing single-candidate path (``add_memory``) still fails fast.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    ErasureBehavior,
    GovernancePolicy,
    MemoryScope,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.config import default_config
from memotron.crypto import is_sealed_content, open_content
from memotron.erasure import sweep_scope
from memotron.models import DreamJobKind
from memotron.receipts import ReceiptDecisionType, is_content_free_reason

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(scope_id: str = "acme") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _client(tmp_path: Path, config: DreamConfig | None = None) -> Memotron:
    return Memotron(
        graph_path=tmp_path / "rejection.sqlite",
        config=config if config is not None else default_config(),
    )


def strict_properties_config() -> DreamConfig:
    """``default_config()`` with the Entity allow-list made STRICT again.

    T0-11 changed the default to non-strict, because leaving it strict meant a real
    extractor's one invented property key quarantined the whole candidate and
    ``default_config()`` formed nothing. The strict mechanism still exists and is
    still worth pinning — it is just no longer the default, so the tests that
    exercise it now say so instead of inheriting it.
    """
    base = default_config()
    return base.model_copy(
        update={
            "instruction_sets": tuple(
                iset.model_copy(
                    update={
                        "node_instructions": tuple(
                            node.model_copy(update={"strict_properties": True}) for node in iset.node_instructions
                        )
                    }
                )
                for iset in base.instruction_sets
            )
        }
    )


def _confidence_floor_config() -> DreamConfig:
    """``default_config`` with a REQUIRES floor, so a low-confidence candidate
    trips the confidence gate rather than pydantic's [0,1] bound."""
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                min_confidence=0.8,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
    )


def _rejections(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED.value
    ]


def _quarantined(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_QUARANTINED.value
    ]


def _objects(client: Memotron, scope: MemoryScope) -> list[str]:
    return sorted(
        str(relationship.properties["object"])
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    )


def _batch(*memories: dict) -> str:
    return json.dumps({"memories": list(memories)})


_GOOD_A = {
    "subject": "Acme Parks",
    "predicate": "requires",
    "object": "SOC2 report",
    "relationship_type": "REQUIRES",
    "confidence": 0.93,
}
_GOOD_B = {
    "subject": "Acme Parks",
    "predicate": "prefers",
    "object": "quarterly business reviews",
    "relationship_type": "PREFERS",
    "confidence": 0.9,
}
# What claude-haiku-4-5 actually emitted through the JedAI Gateway: a subject
# label outside the instruction set, and an invented object property key.
_BAD_LABEL = {
    "subject": "Billing System",
    "predicate": "should",
    "object": "retry failed charges once",
    "relationship_type": "SHOULD",
    "confidence": 0.88,
    "subject_label": "system",
}
_BAD_PROPERTY_KEY = {
    "subject": "Acme Parks",
    "predicate": "should",
    "object": "escalate P1 tickets within an hour",
    "relationship_type": "SHOULD",
    "confidence": 0.91,
    "object_properties": {"scope": "global"},
}


# ---------------------------------------------------------------------------
# 1. Sibling survival — the regression this whole change exists for
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_bad_label_does_not_discard_its_sibling_candidates(tmp_path: Path) -> None:
    """A BATCH, not a lone candidate: the good ones must survive the bad one."""
    client = _client(tmp_path)
    scope = _scope()
    await client.add_episode(
        name="mixed-batch",
        episode_body=_batch(_GOOD_A, _BAD_LABEL, _GOOD_B),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _objects(client, scope) == ["SOC2 report", "quarterly business reviews"]

    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    receipt = rejected[0]
    assert receipt.decision_result == "rejected"
    assert receipt.candidate_digest is not None
    assert receipt.candidate_uuid is not None
    payload = json.loads(receipt.event_payload)
    assert payload == {
        "violation": "label_not_allowed",
        "field": "subject_label",
        "candidate_index": 1,
    }
    assert receipt.event_payload_digest is not None

    # Both survivors were receipted as extracted candidates; the rejected one
    # never became a typed candidate, so it has no CANDIDATE_EXTRACTED twin.
    extracted = [
        item
        for item in client.graph.receipts.receipts_for_scope(scope.key)
        if item.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED.value
    ]
    assert len(extracted) == 2
    assert receipt.candidate_digest not in {item.candidate_digest for item in extracted}


@pytest.mark.asyncio
async def test_two_different_violations_in_one_batch_are_receipted_separately(
    tmp_path: Path,
) -> None:
    """Each drop is its own ledger event, with its own violation code.

    ``_BAD_LABEL`` is a stage-1 structural failure (rejected, never stored);
    ``_BAD_PROPERTY_KEY`` is a stage-3 governance failure (quarantined,
    retained) — the two dispositions coexist in the same batch without
    interfering with each other or with the surviving good candidate.
    """
    client = _client(tmp_path, strict_properties_config())
    scope = _scope("multi")
    await client.add_episode(
        name="two-bad",
        episode_body=_batch(_BAD_LABEL, _GOOD_A, _BAD_PROPERTY_KEY),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _objects(client, scope) == ["SOC2 report"]
    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    assert json.loads(rejected[0].event_payload)["violation"] == "label_not_allowed"

    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].event_payload)["violation"] == "property_key_not_allowed"

    # Distinct candidates → distinct digests and distinct candidate uuids,
    # across BOTH dispositions.
    all_digests = {item.candidate_digest for item in (*rejected, *quarantined)}
    all_uuids = {item.candidate_uuid for item in (*rejected, *quarantined)}
    assert len(all_digests) == 2
    assert len(all_uuids) == 2


# ---------------------------------------------------------------------------
# 2. An all-rejected episode completes cleanly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_episode_with_every_candidate_rejected_completes_cleanly(
    tmp_path: Path,
) -> None:
    """Zero memories formed, every drop receipted (rejected or quarantined), no
    exception, run checkpointed — the episode is processed, not left erroring."""
    client = _client(tmp_path, strict_properties_config())
    scope = _scope("all-bad")
    await client.add_episode(
        name="all-bad",
        episode_body=_batch(_BAD_LABEL, _BAD_PROPERTY_KEY),
        source=EpisodeType.JSON,
        scope=scope,
    )

    run = await client.run_dream_job(job_name="formation-default")

    assert run.processed_episodes == 1
    assert run.created_relationships == 0
    assert _objects(client, scope) == []

    rejected = _rejections(client, scope)
    quarantined = _quarantined(client, scope)
    assert len(rejected) == 1
    assert len(quarantined) == 1
    run_uuid = rejected[0].run_uuid
    assert quarantined[0].run_uuid == run_uuid
    checkpoint = client.graph.receipts.checkpoint_for_run(run_uuid)
    assert len(checkpoint.merkle_root) == 64
    assert client.graph.receipts.verify_chain(run_uuid).valid

    # The episode is consumed: a second run has nothing left to do.
    second = await client.run_dream_job(job_name="formation-default")
    assert second.processed_episodes == 0


# ---------------------------------------------------------------------------
# 3. The fatal boundary: a malformed response ENVELOPE still raises
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        pytest.param("{not json at all", id="not_json"),
        pytest.param(json.dumps({"memories": "not-a-list"}), id="memories_not_a_list"),
        pytest.param(json.dumps({"memories": ["not-an-object"]}), id="entry_not_an_object"),
    ],
)
async def test_malformed_response_envelope_is_still_fatal(tmp_path: Path, body: str) -> None:
    """Envelope failures mean nothing about the episode can be trusted, so they
    abort — and are receipted as fatal before re-raising."""
    client = _client(tmp_path)
    scope = _scope("envelope")
    await client.add_episode(
        name="broken-envelope",
        episode_body=body,
        source=EpisodeType.JSON,
        scope=scope,
    )

    with pytest.raises(ValueError):
        await client.run_due_dreams()

    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    assert json.loads(rejected[0].event_payload) == {
        "violation": "extraction_envelope_invalid",
        "fatal": True,
    }
    # An aborted run is never checkpointed, so byte replay of it fails closed.
    with pytest.raises(ValueError):
        client.graph.receipts.checkpoint_for_run(rejected[0].run_uuid)


# ---------------------------------------------------------------------------
# 4. Reason accuracy per violation type
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "violation", "field", "reason_fragment"),
    [
        pytest.param(
            _BAD_LABEL,
            "label_not_allowed",
            "subject_label",
            "subject label 'system' is not allowed",
            id="label",
        ),
        pytest.param(
            {**_GOOD_A, "relationship_type": "ENJOYS"},
            "relationship_type_not_allowed",
            "relationship_type",
            "relationship type 'ENJOYS' is not allowed",
            id="relationship_type",
        ),
        pytest.param(
            {**_GOOD_A, "scope": "user:someone-else"},
            "scope_mismatch",
            "scope",
            "does not match episode scope",
            id="scope_mismatch",
        ),
        pytest.param(
            {**_GOOD_A, "subject_entity_ref": "Acme"},
            "entity_ref_link_confidence_unpaired",
            "subject_entity_ref",
            "must be supplied together or not at all",
            id="entity_ref_unpaired",
        ),
        pytest.param(
            {**_GOOD_A, "confidence": 5.0},
            "model_validation_failed",
            None,
            "failed validation",
            id="pydantic_bounds",
        ),
    ],
)
async def test_rejection_reason_names_the_specific_violation(
    tmp_path: Path,
    candidate: dict,
    violation: str,
    field: str | None,
    reason_fragment: str,
) -> None:
    """Stage-1 structural failures: never storable, so REJECTED not quarantined."""
    client = _client(tmp_path)
    scope = _scope("reasons")
    await client.add_episode(
        name="one-bad",
        episode_body=_batch(candidate),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    payload = json.loads(rejected[0].event_payload)
    assert payload["violation"] == violation
    assert payload["field"] == field
    assert reason_fragment in rejected[0].decision_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "violation", "reason_fragment"),
    [
        pytest.param(
            _BAD_PROPERTY_KEY,
            "property_key_not_allowed",
            "object_properties key 'scope' is not allowed",
            id="property_key",
        ),
        pytest.param(
            {**_GOOD_A, "subject_properties": {"name": "spoofed"}},
            "property_key_reserved",
            "cannot set reserved node property 'name'",
            id="reserved_property_key",
        ),
    ],
)
async def test_governance_violation_quarantines_not_rejects(
    tmp_path: Path,
    candidate: dict,
    violation: str,
    reason_fragment: str,
) -> None:
    """A property-key whitelist violation is stage-3 GOVERNANCE, not a stage-1
    structural failure: the candidate is a valid entity/relation, so it is
    retained in quarantine (inspectable, promotable) instead of dropped."""
    client = _client(tmp_path, strict_properties_config())
    scope = _scope(f"reasons-{violation}")
    await client.add_episode(
        name="one-bad",
        episode_body=_batch(candidate),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    payload = json.loads(quarantined[0].event_payload)
    assert payload["violation"] == violation
    stored = client.graph.quarantined_candidate(quarantined[0].candidate_uuid)
    assert reason_fragment in stored.detail
    assert not _rejections(client, scope)


@pytest.mark.asyncio
async def test_confidence_floor_quarantines_not_rejects(tmp_path: Path) -> None:
    """The relationship's ``min_confidence`` gate is a stage-3 governance
    judgement: a low-confidence candidate is retained in quarantine, not
    dropped, so a later threshold change can re-govern it without
    re-extraction."""
    client = _client(tmp_path, _confidence_floor_config())
    scope = _scope("floor")
    await client.add_episode(
        name="too-low",
        episode_body=_batch({**_GOOD_A, "confidence": 0.5}),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_dream_job(job_name="formation-default")

    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    payload = json.loads(quarantined[0].event_payload)
    assert payload["violation"] == "confidence_below_minimum"
    stored = client.graph.quarantined_candidate(quarantined[0].candidate_uuid)
    assert "is below REQUIRES minimum 0.8" in stored.detail
    assert _objects(client, scope) == []
    assert not _rejections(client, scope)


@pytest.mark.asyncio
async def test_rejection_receipt_only_echoes_a_configured_relationship_type(
    tmp_path: Path,
) -> None:
    """``relationship_type`` is a plaintext lineage column.  A hallucinated type
    is model-authored text and must not land there; a configured one may."""
    client = _client(tmp_path)
    scope = _scope("rel-type")
    await client.add_episode(
        name="types",
        episode_body=_batch({**_GOOD_A, "relationship_type": "ENJOYS"}, _BAD_LABEL),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    by_violation = {json.loads(item.event_payload)["violation"]: item for item in _rejections(client, scope)}
    assert by_violation["relationship_type_not_allowed"].relationship_type is None
    assert by_violation["label_not_allowed"].relationship_type == "SHOULD"


# ---------------------------------------------------------------------------
# 5. Crypto-shred: rejection reasons stay content-free
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_reasons_are_content_free_under_crypto_shred(
    tmp_path: Path,
) -> None:
    """A rejection reason quotes model-authored text (pydantic errors carry
    ``input_value``).  In a sealed scope it must be withheld and sealed by the
    ledger choke point, the erasure sweep must find no violation, and the
    machine violation code must still be readable."""
    scope = _scope("sealed")
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    client = _client(tmp_path, config)
    leak = "patient Priya Nayar SSN 123-45-6789"
    await client.add_episode(
        name="leaky",
        episode_body=_batch(
            {**_GOOD_A, "subject": leak, "confidence": 5.0},
            _GOOD_B,
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    # The clean sibling still formed (its object is sealed in this scope).
    survivors = [
        relationship
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    ]
    assert len(survivors) == 1
    assert (
        open_content(
            str(survivors[0].properties["object"]),
            client.graph.get_governance_key(scope.key),
        )
        == "quarterly business reviews"
    )

    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    receipt = rejected[0]
    assert leak not in receipt.decision_reason
    assert receipt.decision_reason == "candidate_schema_rejected:reason_withheld"
    assert receipt.sensitive_payload_encrypted is True
    assert is_sealed_content(receipt.sensitive_payload)
    assert json.loads(receipt.event_payload)["violation"] == "model_validation_failed"
    assert leak not in receipt.event_payload
    assert all(
        is_content_free_reason(item.decision_reason) for item in client.graph.receipts.receipts_for_scope(scope.key)
    )
    assert sweep_scope(client.graph, scope_key=scope.key).violations == ()


@pytest.mark.asyncio
async def test_rejected_candidate_digest_is_keyed_in_a_protected_scope(
    tmp_path: Path,
) -> None:
    """Like ``candidate_digest``, the rejected-candidate digest is HMAC-keyed
    with the scope DEK so a destroyed key ends dictionary-testing it."""
    from memotron.receipts import rejected_candidate_digest

    scope = _scope("keyed")
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    client = _client(tmp_path, config)
    await client.add_episode(
        name="keyed",
        episode_body=_batch(_BAD_LABEL),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    receipt = _rejections(client, scope)[0]
    episode_uuid = receipt.episode_uuid
    unkeyed = rejected_candidate_digest(_BAD_LABEL, episode_uuid=episode_uuid)
    keyed = rejected_candidate_digest(
        _BAD_LABEL,
        episode_uuid=episode_uuid,
        content_key=client.graph.get_governance_key(scope.key),
    )
    assert receipt.candidate_digest == keyed
    assert receipt.candidate_digest != unkeyed


# ---------------------------------------------------------------------------
# 6. The operator path still fails fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_operator_add_memory_still_fails_fast_on_a_bad_label(
    tmp_path: Path,
) -> None:
    """``add_memory`` is a single explicit write, not a batch: an operator who
    names an unconfigured label gets a hard error, not a silent drop."""
    client = _client(tmp_path)
    with pytest.raises(ValueError, match="is not allowed by instruction set"):
        await client.add_memory(
            subject="Acme Parks",
            predicate="requires",
            object="SOC2 report",
            relationship_type="REQUIRES",
            scope=_scope("operator"),
            confidence=0.9,
            subject_label="system",
        )


# ---------------------------------------------------------------------------
# 7. Extractor-level contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extract_returns_rejections_rather_than_raising(tmp_path: Path) -> None:
    """The unit-level contract: ``extract`` hands back survivors plus reasons."""
    from memotron.extraction import (
        CandidateViolation,
        InstructionalExtractor,
        RuleBasedExtractionTransport,
    )
    from memotron.models import Episode

    config = default_config()
    instructions = config.instruction_set("default")
    episode = Episode(
        name="batch",
        body=_batch(_GOOD_A, _BAD_LABEL, _GOOD_B),
        source=EpisodeType.JSON,
        source_description="unit",
        scope=_scope("unit"),
    )
    extractor = InstructionalExtractor(transport=RuleBasedExtractionTransport())

    result = await extractor.extract(episode=episode, instructions=instructions)

    assert [memory.object for memory in result.memories] == [
        "SOC2 report",
        "quarterly business reviews",
    ]
    assert len(result.rejections) == 1
    rejection = result.rejections[0]
    assert rejection.index == 1
    assert rejection.violation is CandidateViolation.LABEL_NOT_ALLOWED
    assert rejection.field == "subject_label"
    assert rejection.raw == _BAD_LABEL, "the raw candidate travels unmutated for its digest"


@pytest.mark.asyncio
async def test_regovern_candidate_is_the_same_decision_as_extract(tmp_path: Path) -> None:
    """``regovern_candidate`` is not a second implementation: replaying the
    SAME raw dict through it, with no transport and no episode change, must
    reach the identical verdict ``extract`` reached the first time -- this is
    what makes re-governing a stored raw graph trustworthy."""
    from memotron.extraction import InstructionalExtractor, RuleBasedExtractionTransport
    from memotron.models import Episode

    config = default_config()
    instructions = config.instruction_set("default")
    episode = Episode(
        name="batch",
        body=_batch(_GOOD_A),
        source=EpisodeType.JSON,
        source_description="unit",
        scope=_scope("regovern-unit"),
    )
    extractor = InstructionalExtractor(transport=RuleBasedExtractionTransport())

    extracted = await extractor.extract(episode=episode, instructions=instructions)
    assert len(extracted.memories) == 1

    memory, rejection, abstention = extractor.regovern_candidate(_GOOD_A, episode=episode, instructions=instructions)

    assert rejection is None
    assert abstention is None
    assert memory is not None
    assert memory.object == extracted.memories[0].object
    assert memory.confidence == extracted.memories[0].confidence

    # A candidate extract() rejects must regovern to the identical rejection.
    _, bad_rejection, _ = extractor.regovern_candidate(_BAD_LABEL, episode=episode, instructions=instructions)
    assert bad_rejection is not None
    assert bad_rejection.violation.value == "label_not_allowed"
