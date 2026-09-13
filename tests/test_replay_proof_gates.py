"""WS-11: End-to-end replay/counterfactual/certification proof gates.

Unlike ``test_receipts_integration`` (which proves receipts are *emitted* at every
decision point) and ``test_replay`` (which drives the replay engine over *synthetic*
receipt streams), these gates run the engine over receipts produced by the REAL
formation / consolidation / operator pipeline and prove the patent's headline
properties end to end (PATENT_REPLAY_RECEIPTS_SPEC.md):

* byte replay of a recorded run reconstructs the memory state without an LLM and
  is anchored to the live graph — verified, deterministic (claims 2, 4; [0025]).
* verification FAILS CLOSED on a tampered receipt field or a truncated chain, and
  the withheld proof is recorded as ``mismatch`` (claim 3; [0025] step 480).
* byte replay reconstructs a working->evidence-tier demotion (claim 6; [0027]).
* the receipt chain still verifies and still byte-replays AFTER a per-scope content
  key is crypto-shredded, because hashes are over the stored representation (claim 10).
* counterfactual evaluation flips the accepted set under an alternate Motive while
  leaving every production graph row and state hash unchanged (claims 16-19; [0026]).
* a Motive is certified over a fixture corpus with a receipt-backed pass/fail
  certificate bound to the run Merkle root (claim 14; [0028]).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    ErasureBehavior,
    GovernancePolicy,
    MemoryBank,
    MemoryScope,
    MemoryType,
    Motive,
    NodeInstruction,
    ReceiptDecisionType,
    RelationshipInstruction,
    SalienceRubric,
    ScopeKind,
)
from memotron.config import RollupConsolidationPolicy
from memotron.models import DreamJobKind, Episode, RelationshipCardinality
from memotron.replay import ReplayProof, ReplayVerificationError, byte_replay

# ---------------------------------------------------------------------------
# Fixtures / builders (mirroring test_receipts_integration.py idioms)
# ---------------------------------------------------------------------------


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _instructions() -> DreamInstructionSet:
    return DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(type="PREFERS", source_label="Entity", target_label="Entity", query="prefs"),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="requirements",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
        ),
    )


def _config(
    *,
    salience_rubric: SalienceRubric | None = None,
    governance: GovernancePolicy | None = None,
    memory_bank: MemoryBank | None = None,
    rollup: bool = True,
    consolidation_motive: str | None = None,
    formation_motive: str | None = None,
) -> DreamConfig:
    rollup_policy = RollupConsolidationPolicy(cluster_threshold=0.0, min_cluster_size=2, max_depth=1)
    return DreamConfig(
        instruction_sets=(_instructions(),),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                salience_rubric=salience_rubric,
                motive=formation_motive,
            ),
            DreamJob(
                name="consolidation-default",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=1,
                rollup_consolidation=rollup,
                rollup_consolidation_policy=rollup_policy,
                motive=consolidation_motive,
            ),
        ),
        governance=governance,
        memory_bank=memory_bank,
    )


def _client(tmp_path, config: DreamConfig, name: str = "graph.sqlite") -> Memotron:
    return Memotron(config=config, graph_path=tmp_path / name)


def _line(subject: str, predicate: str, obj: str, rel: str = "PREFERS", confidence: float = 0.9) -> str:
    return (
        f"Memory: subject={subject}; predicate={predicate}; object={obj}; "
        f"relationship_type={rel}; confidence={confidence}"
    )


async def _run_formation(client: Memotron, scope: MemoryScope, body: str, now=None) -> str:
    await client.add_episode(
        name="ep",
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=now,
    )
    result = await client.run_dream_job(job_name="formation-default", now=now)
    run_uuid = result.job_runs[0].run_uuid
    assert run_uuid is not None
    return run_uuid


# ---------------------------------------------------------------------------
# Gate A — byte replay of a REAL formation run, anchored to the live graph
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_byte_replay_real_formation_run_verified(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("g-a")
    body = "\n".join([_line("Alice", "prefers", "green tea"), _line("Bob", "prefers", "black coffee")])
    run_uuid = await _run_formation(client, scope, body)

    ledger = client.graph.receipts
    proof = await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)

    assert isinstance(proof, ReplayProof)
    assert proof.run_uuid == run_uuid
    assert proof.receipt_count >= 3  # 2 candidate + >=1 materialization
    assert proof.reconstructed_state_hash == client.graph.graph_state_hash(scope.key)
    assert len(proof.merkle_root) == 64
    assert ledger.checkpoint_for_run(run_uuid).replay_status == "verified"

    # [0025]: one recorded run yields arbitrarily many identical reconstructions.
    again = await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)
    assert again.reconstructed_state_hash == proof.reconstructed_state_hash
    assert again.merkle_root == proof.merkle_root


# ---------------------------------------------------------------------------
# Gate B — tamper a persisted receipt field → fail closed (claim 3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_tampered_receipt_fails_closed(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("g-b")
    run_uuid = await _run_formation(client, scope, _line("Alice", "prefers", "green tea"))
    ledger = client.graph.receipts

    # Sanity: it verifies before tamper.
    assert (await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)).run_uuid == run_uuid

    # Tamper a single persisted column of the first receipt.
    ledger._connection.execute(
        "UPDATE memory_receipts SET salience_score = 0.000123 WHERE run_uuid = ? AND event_index = 0",
        (run_uuid,),
    )
    ledger._connection.commit()

    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)
    assert excinfo.value.report.first_divergent_event_index == 0
    # The withheld proof is recorded, not silently dropped.
    assert ledger.checkpoint_for_run(run_uuid).replay_status == "mismatch"


# ---------------------------------------------------------------------------
# Gate C — a truncated chain (deleted receipt) → fail closed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_truncated_chain_fails_closed(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("g-c")
    body = "\n".join([_line("Alice", "prefers", "green tea"), _line("Bob", "prefers", "black coffee")])
    run_uuid = await _run_formation(client, scope, body)
    ledger = client.graph.receipts

    receipts = await client.memory_receipts(run_uuid=run_uuid)
    last_index = max(r.event_index for r in receipts)
    ledger._connection.execute(
        "DELETE FROM memory_receipts WHERE run_uuid = ? AND event_index = ?",
        (run_uuid, last_index),
    )
    ledger._connection.commit()

    with pytest.raises(ReplayVerificationError):
        await byte_replay(ledger, run_uuid=run_uuid, graph=client.graph)


# ---------------------------------------------------------------------------
# Gate D — byte replay reconstructs a consolidation demotion (claim 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_byte_replay_reconstructs_demotion(tmp_path):
    client = _client(tmp_path, _config(rollup=True))
    scope = _scope("g-d")
    body = "\n".join([_line("Alice", "prefers", "green tea"), _line("Bob", "prefers", "black coffee")])
    await _run_formation(client, scope, body)

    consolidation = await client.run_dream_job(job_name="consolidation-default")
    run_uuid = consolidation.job_runs[0].run_uuid
    receipts = await client.memory_receipts(run_uuid=run_uuid)
    demoted = [r for r in receipts if r.decision_type == ReceiptDecisionType.CONSOLIDATION_MEMBER_DEMOTED]
    assert demoted, "the consolidation demoted at least one member into the evidence tier"

    # The demotion run byte-replays: its per-mutation state hashes chain and match the live store.
    proof = await byte_replay(client.graph.receipts, run_uuid=run_uuid, graph=client.graph)
    assert proof.reconstructed_state_hash == client.graph.graph_state_hash(scope.key)
    assert client.graph.receipts.checkpoint_for_run(run_uuid).replay_status == "verified"


# ---------------------------------------------------------------------------
# Gate E — chain verifies AND byte-replays after crypto-shred (claim 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_replay_survives_crypto_shred(tmp_path):
    gov = GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)
    client = _client(tmp_path, _config(governance=gov))
    scope = _scope("g-e")
    client.provision_governance_key(scope=scope)
    run_uuid = await _run_formation(client, scope, _line("Frank", "requires", "two-factor auth", rel="REQUIRES"))

    assert (await byte_replay(client.graph.receipts, run_uuid=run_uuid, graph=client.graph)).run_uuid == run_uuid

    shred = await client.crypto_shred(scope=scope)
    assert shred["shredded"] is True

    # The graph rows are gone-in-content but the receipt skeleton + graph lineage survive:
    # the chain still verifies and the run still byte-replays (hashes are over ciphertext).
    assert client.graph.receipts.verify_chain(run_uuid).valid
    proof = await byte_replay(client.graph.receipts, run_uuid=run_uuid, graph=client.graph)
    assert proof.run_uuid == run_uuid


# ---------------------------------------------------------------------------
# Gate F — counterfactual flips the accepted set; production untouched (16-19)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gate_counterfactual_isolation_and_flip(tmp_path):
    bank = MemoryBank(
        motives=[
            Motive(
                name="requirements-only",
                goal="Persist only explicit requirements.",
                allowed_memory_types=(MemoryType.REQUIREMENT,),
            )
        ]
    )
    client = _client(tmp_path, _config(memory_bank=bank))
    scope = _scope("g-f")
    body = "\n".join(
        [_line("Alice", "prefers", "green tea"), _line("Dana", "requires", "two-factor auth", rel="REQUIRES")]
    )
    run_uuid = await _run_formation(client, scope, body)

    # Snapshot production state before the counterfactual.
    state_before = client.graph.graph_state_hash(scope.key)
    rows_before = client.graph.receipts._connection.execute("SELECT COUNT(*) FROM memory_receipts").fetchone()[0]

    report = await client.counterfactual(run_uuid=run_uuid, motive="requirements-only")

    # The preference flips OUT under the requirements-only Motive; the requirement survives both.
    assert report.original_materialized_count == 2
    assert report.alternate_materialized_count == 1
    assert len(report.accepted_by_both) == 1
    assert len(report.original_only) == 1

    # Isolation: production graph state hash and the receipt ledger are byte-for-byte unchanged.
    assert client.graph.graph_state_hash(scope.key) == state_before
    rows_after = client.graph.receipts._connection.execute("SELECT COUNT(*) FROM memory_receipts").fetchone()[0]
    assert rows_after == rows_before
    # The original run still byte-replays cleanly (counterfactual left it intact).
    assert (await byte_replay(client.graph.receipts, run_uuid=run_uuid, graph=client.graph)).run_uuid == run_uuid


@pytest.mark.asyncio
async def test_gate_counterfactual_withheld_on_tampered_chain(tmp_path):
    bank = MemoryBank(
        motives=[
            Motive(name="requirements-only", goal="only requirements", allowed_memory_types=(MemoryType.REQUIREMENT,))
        ]
    )
    client = _client(tmp_path, _config(memory_bank=bank))
    scope = _scope("g-f2")
    run_uuid = await _run_formation(client, scope, _line("Dana", "requires", "2fa", rel="REQUIRES"))

    client.graph.receipts._connection.execute(
        "UPDATE memory_receipts SET memory_type = 'directive' WHERE run_uuid = ? AND event_index = 0",
        (run_uuid,),
    )
    client.graph.receipts._connection.commit()

    with pytest.raises(ReplayVerificationError):
        await client.counterfactual(run_uuid=run_uuid, motive="requirements-only")


# ---------------------------------------------------------------------------
# Gate G — Motive certification pass / fail over a fixture corpus (claim 14)
# ---------------------------------------------------------------------------


def _corpus(scope: MemoryScope) -> list[Episode]:
    ref = datetime(2026, 5, 5, tzinfo=UTC)
    return [
        Episode(
            uuid="cert-ep-req",
            name="c-req",
            body=_line("Dana", "requires", "two-factor auth", rel="REQUIRES"),
            source=EpisodeType.TEXT,
            source_description="d",
            scope=scope,
            reference_time=ref,
            metadata={},
            instruction_set="default",
        ),
        Episode(
            uuid="cert-ep-pref",
            name="c-pref",
            body=_line("Alice", "prefers", "green tea"),
            source=EpisodeType.TEXT,
            source_description="d",
            scope=scope,
            reference_time=ref,
            metadata={},
            instruction_set="default",
        ),
    ]


@pytest.mark.asyncio
async def test_gate_certify_motive_passes_clean_corpus(tmp_path):
    # A requirements-only Motive filters the preference at formation, so nothing
    # disallowed is ever persisted → the certificate passes.
    motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    bank = MemoryBank(motives=[motive])
    client = _client(tmp_path, _config(memory_bank=bank))
    scope = _scope("g-g1")

    cert = await client.certify_motive(motive=motive, corpus=_corpus(scope), scope=scope)

    assert cert.passed is True
    assert cert.failing_receipt_uuids == ()
    assert cert.metrics["disallowed_type_persistence_count"] == 0
    assert cert.metrics["unexplained_candidate_count"] == 0
    assert cert.metrics["materialized_count"] == 1  # only the requirement
    assert len(cert.merkle_root) == 64
    assert len(cert.corpus_digest) == 64


@pytest.mark.asyncio
async def test_gate_certify_motive_fails_on_disallowed_persistence(tmp_path):
    # A permissive Motive that ALLOWS preference persists a preference; certifying
    # it against a requirements-ONLY policy flags the disallowed persistence.
    permissive = Motive(
        name="permissive",
        goal="Persist everything.",
        allowed_memory_types=(MemoryType.PREFERENCE, MemoryType.REQUIREMENT),
    )
    strict = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    bank = MemoryBank(motives=[permissive, strict])
    # Formation runs under the permissive Motive (so the preference materializes)...
    client = _client(tmp_path, _config(memory_bank=bank, formation_motive="permissive"))
    scope = _scope("g-g2")

    # ...but we certify the run against the STRICT policy's allowed types.
    run_uuid = await client.run_certification_formation(motive=permissive, corpus=_corpus(scope), scope=scope)
    ledger = client.graph.receipts
    from memotron.replay import _certification_metrics

    receipts = ledger.receipts_for_run(run_uuid)
    checkpoint = ledger.checkpoint_for_run(run_uuid)
    result = _certification_metrics(receipts, checkpoint, strict)

    assert result["passed"] is False
    assert result["metrics"]["disallowed_type_persistence_count"] == 1
    assert result["failing_receipt_uuids"]
