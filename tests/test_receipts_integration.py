"""WS-11: Receipt-emission wiring integration tests (PATENT_REPLAY_RECEIPTS_SPEC.md).

These exercise the REAL formation / consolidation / pruning / operator paths end to
end through the SDK and assert that every memory-formation decision point emits its
canonical, hash-chained, Merkle-committed receipt ([0019]-[0027]).  The receipts core
(``memotron.receipts``) has its own unit tests; here we prove the instrumentation:

1. formation of N candidates where k are gated → N CANDIDATE_EXTRACTED + N dispositions,
   negative_space returns exactly the k, verify_chain passes, checkpoint has a merkle_root.
2. FORMATION_RELATIONSHIP_CREATED carries graph_state_hash_before != after (claim 4).
3. dedup reinforce + truth-key supersession receipts.
4. consolidation rollup summarize + member demote, and the Motive→ROLLUP gate (claim 6).
5. pruning receipts.
6. crypto_shred receipt + chain still verifies after the key is destroyed (claim 10).
7. a minor policy change flips the effective_policy_digest (claim 7).
8. determinism of the content-addressed digests + decision stream across two fresh clients.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    DedupPolicy,
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

# ---------------------------------------------------------------------------
# Helpers
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
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Remember stable preferences.",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that improve agent behaviour.",
            ),
        ),
    )


def _config(
    *,
    salience_rubric: SalienceRubric | None = None,
    dedup: DedupPolicy | None = None,
    governance: GovernancePolicy | None = None,
    memory_bank: MemoryBank | None = None,
    consolidation_motive: str | None = None,
    rollup: bool = True,
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
            ),
            DreamJob(
                name="consolidation-default",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=1,
                rollup_consolidation=rollup,
                rollup_consolidation_policy=rollup_policy,
                motive=consolidation_motive,
            ),
            DreamJob(
                name="pruning-default",
                kind=DreamJobKind.PRUNING,
                cadence_seconds=1,
            ),
        ),
        dedup=dedup or DedupPolicy(),
        governance=governance,
        memory_bank=memory_bank,
    )


def _client(tmp_path, config: DreamConfig, name: str = "graph.sqlite") -> Memotron:
    return Memotron(config=config, graph_path=tmp_path / name)


def _memory_line(subject: str, predicate: str, obj: str, rel: str = "PREFERS", confidence: float = 0.9) -> str:
    return (
        f"Memory: subject={subject}; predicate={predicate}; object={obj}; "
        f"relationship_type={rel}; confidence={confidence}"
    )


def _types(receipts, decision_type: ReceiptDecisionType):
    return [r for r in receipts if r.decision_type == decision_type]


# ---------------------------------------------------------------------------
# 1. N candidates, k gated → N candidate + N disposition receipts + negative space
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_formation_candidate_and_disposition_receipts(tmp_path):
    # A max_memories_per_episode cap of 1 deterministically gates 2 of 3 candidates.
    config = _config(salience_rubric=SalienceRubric(max_memories_per_episode=1))
    client = _client(tmp_path, config)
    scope = _scope("t1")
    body = "\n".join(
        [
            _memory_line("Alice", "prefers", "green tea"),
            _memory_line("Bob", "prefers", "loud rock music"),
            _memory_line("Carol", "prefers", "cold winter weather"),
        ]
    )
    await client.add_episode(name="e1", episode_body=body, source=EpisodeType.TEXT, scope=scope, source_description="d")
    result = await client.run_dream_job(job_name="formation-default")
    run_uuid = result.job_runs[0].run_uuid
    assert run_uuid is not None

    receipts = await client.memory_receipts(run_uuid=run_uuid)
    extracted = _types(receipts, ReceiptDecisionType.CANDIDATE_EXTRACTED)
    low = _types(receipts, ReceiptDecisionType.CANDIDATE_LOW_SALIENCE)
    created = _types(receipts, ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED)

    assert len(extracted) == 3, "every extracted candidate is receipted before gating ([0024])"
    # N disposition receipts: 2 gated + 1 materialized == 3 == N.
    assert len(low) == 2
    assert len(created) == 1
    assert len(low) + len(created) == len(extracted)

    # Every CANDIDATE_EXTRACTED carries a candidate_digest, salience_score, evidence pointer.
    for r in extracted:
        assert r.candidate_digest is not None
        assert r.salience_score is not None
        assert r.episode_digest is not None
        assert r.effective_policy_digest

    # negative_space returns exactly the k gated ones, with gate + threshold + policy + evidence.
    neg = await client.negative_space(scope=scope, decision_type=ReceiptDecisionType.CANDIDATE_LOW_SALIENCE)
    assert len(neg) == 2
    for entry in neg:
        assert entry.decision_result == "gated"
        assert entry.salience_threshold is not None
        assert entry.effective_policy_digest
        assert entry.candidate_digest is not None
        assert entry.episode_digest is not None

    # Chain + Merkle verify; checkpoint exists with a merkle_root.
    verification = client.graph.receipts.verify_chain(run_uuid)
    assert verification.valid, verification.errors
    checkpoint = client.graph.receipts.checkpoint_for_run(run_uuid)
    assert len(checkpoint.merkle_root) == 64
    assert await client.run_checkpoints(limit=5), "run_checkpoints SDK returns the checkpoint"


@pytest.mark.asyncio
async def test_schema_rejection_receipts_without_aborting_the_run(tmp_path):
    """An unknown relationship_type rejects that candidate; the run completes.

    Per-candidate schema rejection ([0024]): the candidate is dropped and
    receipted, the episode finishes with zero memories formed, and — unlike the
    aborting behaviour this replaced — the run checkpoints, so the rejection is
    inside a Merkle-committed, byte-replayable run.
    """
    client = _client(tmp_path, _config())
    scope = _scope("t1b")
    await client.add_episode(
        name="bad",
        episode_body=_memory_line("Al", "likes", "x", rel="UNKNOWN"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
    )
    run = await client.run_dream_job(job_name="formation-default")
    assert run.created_relationships == 0
    assert run.processed_episodes == 1

    receipts = await client.memory_receipts(scope=scope)
    rejected = _types(receipts, ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED)
    assert len(rejected) == 1
    assert rejected[0].decision_result == "rejected"
    assert rejected[0].candidate_digest is not None
    # No CANDIDATE_EXTRACTED sibling: the candidate never became a typed memory.
    assert not _types(receipts, ReceiptDecisionType.CANDIDATE_EXTRACTED)
    # The run completed, so it checkpointed and its chain verifies.
    checkpoint = client.graph.receipts.checkpoint_for_run(rejected[0].run_uuid)
    assert len(checkpoint.merkle_root) == 64
    assert client.graph.receipts.verify_chain(rejected[0].run_uuid).valid


# ---------------------------------------------------------------------------
# 2. FORMATION_RELATIONSHIP_CREATED graph_state_hash before != after (claim 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_relationship_created_state_hash_changes(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("t2")
    await client.add_episode(
        name="e2",
        episode_body=_memory_line("Alice", "prefers", "green tea"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
    )
    result = await client.run_dream_job(job_name="formation-default")
    receipts = await client.memory_receipts(run_uuid=result.job_runs[0].run_uuid)
    created = _types(receipts, ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED)
    assert len(created) == 1
    r = created[0]
    assert r.graph_state_hash_before is not None and r.graph_state_hash_after is not None
    assert r.graph_state_hash_before != r.graph_state_hash_after
    assert len(r.graph_state_hash_before) == 64 and len(r.graph_state_hash_after) == 64


# ---------------------------------------------------------------------------
# 3. Dedup reinforce + truth-key supersession
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinforce_and_supersede_receipts(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("t3")
    base = datetime(2026, 1, 1, tzinfo=UTC)

    # Reinforce: identical PREFERS fact in a second episode → exact-update dedup.
    await client.add_episode(
        name="r1",
        episode_body=_memory_line("Alice", "prefers", "green tea"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=base,
    )
    await client.run_dream_job(job_name="formation-default", now=base)
    await client.add_episode(
        name="r2",
        episode_body=_memory_line("Alice", "prefers", "green tea"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=base + timedelta(hours=1),
    )
    reinforce_run = await client.run_dream_job(job_name="formation-default", now=base + timedelta(hours=1))
    reinforce_receipts = await client.memory_receipts(run_uuid=reinforce_run.job_runs[0].run_uuid)
    reinforced = _types(reinforce_receipts, ReceiptDecisionType.FORMATION_SEMANTIC_DEDUP_REINFORCED)
    assert len(reinforced) == 1
    assert reinforced[0].decision_result == "reinforced"
    assert reinforced[0].dedup_match_relationship_uuid is not None
    assert reinforced[0].dedup_score is not None

    # Supersession: single-active REQUIRES with a new object supersedes the old row.
    await client.add_episode(
        name="s1",
        episode_body=_memory_line("Dana", "requires", "two-factor auth", rel="REQUIRES"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=base,
    )
    await client.run_dream_job(job_name="formation-default", now=base)
    await client.add_episode(
        name="s2",
        episode_body=_memory_line("Dana", "requires", "hardware passkey", rel="REQUIRES"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=base + timedelta(hours=2),
    )
    supersede_run = await client.run_dream_job(job_name="formation-default", now=base + timedelta(hours=2))
    supersede_receipts = await client.memory_receipts(run_uuid=supersede_run.job_runs[0].run_uuid)
    superseded = _types(supersede_receipts, ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED)
    assert len(superseded) == 1
    assert superseded[0].superseded_relationship_uuid is not None
    assert superseded[0].successor_relationship_uuid is not None
    assert superseded[0].superseded_relationship_uuid != superseded[0].successor_relationship_uuid


# ---------------------------------------------------------------------------
# 4. Consolidation rollup summarize + demote, and Motive->ROLLUP gate (claim 6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_rollup_summarize_and_demote(tmp_path):
    client = _client(tmp_path, _config(rollup=True))
    scope = _scope("t4")
    body = "\n".join(
        [
            _memory_line("Alice", "prefers", "green tea"),
            _memory_line("Bob", "prefers", "black coffee"),
        ]
    )
    await client.add_episode(name="c1", episode_body=body, source=EpisodeType.TEXT, scope=scope, source_description="d")
    await client.run_dream_job(job_name="formation-default")

    consolidation = await client.run_dream_job(job_name="consolidation-default")
    run_uuid = consolidation.job_runs[0].run_uuid
    receipts = await client.memory_receipts(run_uuid=run_uuid)

    summarized = _types(receipts, ReceiptDecisionType.CONSOLIDATION_ROLLUP_CREATED)
    demoted = _types(receipts, ReceiptDecisionType.CONSOLIDATION_MEMBER_DEMOTED)
    assert len(summarized) == 1
    rollup_uuid = summarized[0].relationship_uuid
    assert rollup_uuid is not None
    assert summarized[0].memory_type == MemoryType.ROLLUP.value
    assert summarized[0].graph_state_hash_before != summarized[0].graph_state_hash_after
    assert len(demoted) == 2
    for member in demoted:
        assert member.successor_relationship_uuid == rollup_uuid
        assert member.graph_state_hash_before != member.graph_state_hash_after
    assert client.graph.receipts.verify_chain(run_uuid).valid


@pytest.mark.asyncio
async def test_consolidation_motive_rollup_gate(tmp_path):
    bank = MemoryBank(
        motives=[Motive(name="prefs-only", goal="keep only preferences", allowed_memory_types=(MemoryType.PREFERENCE,))]
    )
    client = _client(tmp_path, _config(rollup=True, memory_bank=bank, consolidation_motive="prefs-only"))
    scope = _scope("t4b")
    body = "\n".join(
        [
            _memory_line("Alice", "prefers", "green tea"),
            _memory_line("Bob", "prefers", "black coffee"),
        ]
    )
    await client.add_episode(name="c2", episode_body=body, source=EpisodeType.TEXT, scope=scope, source_description="d")
    await client.run_dream_job(job_name="formation-default")
    consolidation = await client.run_dream_job(job_name="consolidation-default")
    receipts = await client.memory_receipts(run_uuid=consolidation.job_runs[0].run_uuid)

    gated = _types(receipts, ReceiptDecisionType.CONSOLIDATION_MOTIVE_ROLLUP_GATED)
    summarized = _types(receipts, ReceiptDecisionType.CONSOLIDATION_ROLLUP_CREATED)
    assert len(gated) == 1
    assert gated[0].memory_type == MemoryType.ROLLUP.value
    assert summarized == [], "a Motive excluding ROLLUP blocks all rollup summaries"


# ---------------------------------------------------------------------------
# 5. Pruning receipts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pruning_receipts(tmp_path):
    client = _client(tmp_path, _config())
    scope = _scope("t5")
    now = datetime(2026, 3, 1, tzinfo=UTC)
    # A memory whose valid_to has already elapsed is pruned (valid_to_elapsed branch).
    await client.add_memory(
        subject="Eve",
        predicate="prefers",
        object="expired preference",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=now - timedelta(days=2),
        valid_to=now - timedelta(days=1),
        reference_time=now - timedelta(days=2),
    )
    result = await client.run_dream_job(job_name="pruning-default", now=now)
    run_uuid = result.job_runs[0].run_uuid
    assert run_uuid is not None
    receipts = await client.memory_receipts(run_uuid=run_uuid)
    pruned = _types(receipts, ReceiptDecisionType.PRUNING_RELATIONSHIP_PRUNED)
    assert len(pruned) == 1
    assert pruned[0].decision_reason == "valid_to_elapsed"
    assert pruned[0].decision_result == "pruned"
    assert client.graph.receipts.verify_chain(run_uuid).valid


# ---------------------------------------------------------------------------
# 6. crypto_shred receipt + chain verifies after the key is destroyed (claim 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crypto_shred_receipt_and_chain_survives(tmp_path):
    gov = GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)
    client = _client(tmp_path, _config(governance=gov))
    scope = _scope("t6")
    client.provision_governance_key(scope=scope)
    await client.add_episode(
        name="k1",
        episode_body=_memory_line("Frank", "requires", "two-factor auth", rel="REQUIRES"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
    )
    formation = await client.run_dream_job(job_name="formation-default")
    formation_run = formation.job_runs[0].run_uuid

    # Formation receipts stored the ciphertext representation (never plaintext).
    formation_receipts = await client.memory_receipts(run_uuid=formation_run)
    encrypted = [r for r in formation_receipts if r.sensitive_payload_encrypted]
    assert encrypted, "crypto-shred scope receipts carry ciphertext sensitive_payload"
    assert all(r.sensitive_payload is not None for r in encrypted)
    assert client.graph.receipts.verify_chain(formation_run).valid

    shred = await client.crypto_shred(scope=scope)
    assert shred["shredded"] is True
    assert client.graph.get_governance_key(scope.key) is None

    # The shred itself is receipted under an operator run.
    scope_receipts = await client.memory_receipts(scope=scope)
    shred_receipts = _types(scope_receipts, ReceiptDecisionType.CRYPTO_SHRED_KEY_DESTROYED)
    assert len(shred_receipts) == 1
    assert shred_receipts[0].decision_result == "destroyed"
    assert client.graph.receipts.verify_chain(shred_receipts[0].run_uuid).valid

    # Chain still verifies end-to-end AFTER the key was destroyed (hashes are over ciphertext).
    assert client.graph.receipts.verify_chain(formation_run).valid


# ---------------------------------------------------------------------------
# 7. Minor policy change flips the effective_policy_digest (claim 7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_policy_change_flips_effective_policy_digest(tmp_path):
    scope = _scope("t7")
    body = _memory_line("Grace", "prefers", "green tea")

    client_a = _client(tmp_path, _config(dedup=DedupPolicy(cosine_threshold=0.88)), name="a.sqlite")
    await client_a.add_episode(
        name="p1", episode_body=body, source=EpisodeType.TEXT, scope=scope, source_description="d"
    )
    run_a = await client_a.run_dream_job(job_name="formation-default")
    digest_a = {r.effective_policy_digest for r in await client_a.memory_receipts(run_uuid=run_a.job_runs[0].run_uuid)}

    client_b = _client(tmp_path, _config(dedup=DedupPolicy(cosine_threshold=0.89)), name="b.sqlite")
    await client_b.add_episode(
        name="p1", episode_body=body, source=EpisodeType.TEXT, scope=scope, source_description="d"
    )
    run_b = await client_b.run_dream_job(job_name="formation-default")
    digest_b = {r.effective_policy_digest for r in await client_b.memory_receipts(run_uuid=run_b.job_runs[0].run_uuid)}

    assert len(digest_a) == 1 and len(digest_b) == 1
    assert digest_a != digest_b, "a tenant-layer dedup threshold change changes the policy digest"


# ---------------------------------------------------------------------------
# 8. Determinism of content-addressed digests + decision stream (two fresh clients)
# ---------------------------------------------------------------------------


def _fixed_episode(scope: MemoryScope, uuid: str, ref: datetime) -> Episode:
    return Episode(
        uuid=uuid,
        name="fixed",
        body="\n".join(
            [
                _memory_line("Heidi", "prefers", "green tea"),
                _memory_line("Ivan", "requires", "two-factor auth", rel="REQUIRES"),
            ]
        ),
        source=EpisodeType.TEXT,
        source_description="fixed",
        scope=scope,
        reference_time=ref,
        metadata={},
        instruction_set="default",
    )


def _content_signature(receipts):
    # graph_state_hash includes the (uuid4) relationship id per the spec tuple, so it is
    # deliberately excluded here: it is uuid-dependent and not cross-client reproducible.
    return [
        (
            r.decision_type.value,
            r.decision_result,
            r.candidate_digest,
            r.episode_digest,
            r.memory_type,
            r.salience_score,
            r.effective_policy_digest,
        )
        for r in receipts
    ]


@pytest.mark.asyncio
async def test_determinism_across_two_fresh_clients(tmp_path):
    # receipts.py mints uuid4 run/receipt ids (and relationship uuids are uuid4), so the
    # receipt_hash/merkle_root are intentionally NOT stable across fresh clients; the
    # MEANINGFUL determinism is that the content-addressed digests and the ordered decision
    # stream are byte-identical for identical fixtures + policy.
    scope = _scope("t8")
    ref = datetime(2026, 2, 2, tzinfo=UTC)

    async def run(name: str):
        client = _client(tmp_path, _config(), name=name)
        await client.add_episode_bulk([_fixed_episode(scope, "fixed-episode-uuid", ref)])
        result = await client.run_dream_job(job_name="formation-default", now=ref)
        return await client.memory_receipts(run_uuid=result.job_runs[0].run_uuid)

    receipts_a = await run("det_a.sqlite")
    receipts_b = await run("det_b.sqlite")

    assert receipts_a, "the fixture produced receipts"
    assert _content_signature(receipts_a) == _content_signature(receipts_b)
    assert {r.effective_policy_digest for r in receipts_a} == {r.effective_policy_digest for r in receipts_b}
    assert [r.candidate_digest for r in receipts_a] == [r.candidate_digest for r in receipts_b]
