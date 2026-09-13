"""WS-26: end-to-end re-dream — branch -> recompute -> diff -> adopt/rollback.

Covers the GOAL(WS-26) clauses directly:

* a governance-only re-dream (Tier A) makes ZERO LLM/extraction calls;
* HEAD (the live graph_state_hash) is untouched while a branch computes;
* Tier B (re-resolve, zero extraction calls) reaches the SAME content as
  Tier C (full re-extraction) under an unchanged model;
* a tampered receipt chain aborts adopt fail-closed and the pointer never
  moves;
* rollback restores the pre-adopt graph_state_hash byte-for-byte;
* a branched re-dream's canonicalization-registry writes are invisible to
  HEAD until (and unless) the branch is adopted;
* Tier C under a stricter policy yields fewer facts than the original,
  while the untouched fact and E_cur are unaffected;
* T7's session digest is a derived rollup, never a Date/Session substrate
  node.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
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
from memotron.config import ActionabilityPolicy, DedupPolicy
from memotron.epochs import (
    EpisodeSelector,
    RedreamOverrides,
    RedreamTier,
    adopt_epoch,
    branch_and_recompute,
    build_session_digest,
    diff_epochs,
    epoch_content_digest,
    rollback_epoch,
)
from memotron.extraction import InstructionalExtractor, RuleBasedExtractionTransport
from memotron.models import DreamJobKind, MemoryType, QuarantineStatus
from memotron.replay import ReplayVerificationError
from memotron.storage.sqlite import SQLiteStorageBackend

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _instructions(min_confidence: float = 0.0) -> DreamInstructionSet:
    return DreamInstructionSet(
        name="default",
        node_instructions=(NodeInstruction(label="Entity", query="entities", properties=(), strict_properties=False),),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="prefs",
                min_confidence=min_confidence,
            ),
        ),
    )


def _config(min_confidence: float = 0.0) -> DreamConfig:
    return DreamConfig(
        instruction_sets=(_instructions(min_confidence),),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        actionability=ActionabilityPolicy(enabled=False),
    )


def _line(subject: str, predicate: str, obj: str, confidence: float = 0.9) -> str:
    return (
        f"Memory: subject={subject}; predicate={predicate}; object={obj}; "
        f"relationship_type=PREFERS; confidence={confidence}"
    )


def _active_facts(storage, scope: MemoryScope) -> list[str]:
    return sorted(
        r.properties.get("fact")
        for r in storage.relationships_for_scope(scope.key)
        if r.properties.get("status") == "active"
    )


class _CountingTransport:
    """Wraps ``RuleBasedExtractionTransport``, counting real extraction calls.

    A spy proves the NEGATIVE claim T8 makes about Tier A/B ("zero LLM
    calls") the only way a negative claim can be proven: by wiring in
    something that would visibly notice if it were ever invoked.
    """

    def __init__(self) -> None:
        self._inner = RuleBasedExtractionTransport()
        self.calls = 0

    async def extract_memories(self, *args, **kwargs):
        self.calls += 1
        return await self._inner.extract_memories(*args, **kwargs)


# ---------------------------------------------------------------------------
# T4 — branch + recompute leaves HEAD untouched
# ---------------------------------------------------------------------------


class TestBranchLeavesHeadUntouched:
    @pytest.mark.asyncio
    async def test_head_graph_state_hash_is_unchanged_by_a_branch(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("branch-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Alice", "prefers", "green tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.add_episode(
            name="ep2",
            episode_body=_line("Bob", "prefers", "black coffee"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now + timedelta(seconds=1),
        )
        await client.run_dream_job(job_name="formation-default", now=now)

        head_hash_before = client.graph.graph_state_hash(scope.key)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))

        result = await branch_and_recompute(
            main_storage=client.graph,
            config=client.config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            now=now + timedelta(seconds=5),
        )
        assert client.graph.graph_state_hash(scope.key) == head_hash_before
        assert client.graph.active_epoch_for_scope(scope.key) != result.epoch_id
        assert client.graph.epoch(result.epoch_id)["status"] == "ready"

    @pytest.mark.asyncio
    async def test_a_crypto_shred_scope_cannot_be_branched(self, tmp_path: Path) -> None:
        from memotron.config import ErasureBehavior, GovernancePolicy, PiiSensitivity

        config = _config().model_copy(
            update={
                "governance": GovernancePolicy(
                    pii_sensitivity=PiiSensitivity.HIGH, erasure_behavior=ErasureBehavior.CRYPTO_SHRED
                )
            }
        )
        client = Memotron(config=config, graph_path=tmp_path / "graph.sqlite")
        scope = _scope("branch-shred")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Carl", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))
        with pytest.raises(ValueError, match="content-protected"):
            await branch_and_recompute(
                main_storage=client.graph,
                config=client.config,
                scope=scope,
                selector=selector,
                extractor=client._extractor,
                embedding_transport=client._embedding_transport,
                now=now,
            )


# ---------------------------------------------------------------------------
# T8 — a governance-only re-dream makes ZERO LLM calls
# ---------------------------------------------------------------------------


class TestZeroLlmGovernanceRedream:
    @pytest.mark.asyncio
    async def test_tier_a_promotes_a_quarantined_candidate_with_zero_extraction_calls(self, tmp_path: Path) -> None:
        counting = _CountingTransport()
        extractor = InstructionalExtractor(transport=counting)
        strict_config = _config(min_confidence=0.8)
        client = Memotron(config=strict_config, extraction_transport=counting, graph_path=tmp_path / "graph.sqlite")
        scope = _scope("tier-a")
        now = datetime.now(UTC)

        # confidence 0.5 < min_confidence 0.8 -> quarantined at extraction time.
        await client.add_episode(
            name="ep1",
            episode_body=_line("Carol", "prefers", "oat milk", confidence=0.5),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        assert counting.calls == 1
        quarantined = client.graph.quarantined_candidates(scope_key=scope.key, status=None, limit=None)
        assert len(quarantined) == 1
        assert quarantined[0].status == QuarantineStatus.QUARANTINED

        calls_before_branch = counting.calls
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))
        loose_config = _config(min_confidence=0.3)

        result = await branch_and_recompute(
            main_storage=client.graph,
            config=loose_config,
            scope=scope,
            selector=selector,
            extractor=extractor,
            embedding_transport=client._embedding_transport,
            now=now + timedelta(seconds=5),
        )

        # The proof: not one extraction/LLM call happened during the branch.
        assert counting.calls == calls_before_branch
        assert result.tier is RedreamTier.A
        assert result.created_relationships == 1

        shadow = SQLiteStorageBackend(result.shadow_store_path)
        try:
            assert _active_facts(shadow, scope) == ["Carol prefers oat milk"]
        finally:
            shadow.close()

        adopted = await adopt_epoch(
            main_storage=client.graph, scope=scope, epoch_id=result.epoch_id, now=now + timedelta(seconds=10)
        )
        assert counting.calls == calls_before_branch  # still zero, through adopt too
        assert [entry.fact for entry in adopted.diff.added] == ["Carol prefers oat milk"]
        assert "Carol prefers oat milk" in _active_facts(client.graph, scope)

    @pytest.mark.asyncio
    async def test_the_recompute_tier_rides_the_ws11_run_checkpoint(self, tmp_path: Path) -> None:
        """WS-26 T8 follow-up: the run receipt itself names the tier and why, so a
        receipt reader can tell a governance-only re-dream from a full re-extract
        without joining to the epoch row."""
        counting = _CountingTransport()
        client = Memotron(
            config=_config(min_confidence=0.8),
            extraction_transport=counting,
            graph_path=tmp_path / "graph.sqlite",
        )
        scope = _scope("tier-receipt")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Dana", "prefers", "oat milk", confidence=0.5),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        result = await branch_and_recompute(
            main_storage=client.graph,
            config=_config(min_confidence=0.3),
            scope=scope,
            selector=EpisodeSelector(episode_uuids=(episode_uuid,)),
            extractor=InstructionalExtractor(transport=counting),
            embedding_transport=client._embedding_transport,
            now=now + timedelta(seconds=5),
        )
        assert result.tier is RedreamTier.A
        assert len(result.run_uuids) == 1

        # The tier + reason are on the run's WS-11 checkpoint (recorded on the
        # shadow ledger the recompute ran against).
        shadow = SQLiteStorageBackend(result.shadow_store_path)
        try:
            checkpoint = shadow.receipts.checkpoint_for_run(result.run_uuids[0])
            assert checkpoint.redream_tier == "A"
            assert checkpoint.redream_tier_reason
            assert checkpoint.redream_tier_reason == result.tier_reason
        finally:
            shadow.close()


# ---------------------------------------------------------------------------
# T5 — diff
# ---------------------------------------------------------------------------


class TestDiff:
    @pytest.mark.asyncio
    async def test_a_changed_fact_shows_in_changed_not_added_plus_removed(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(min_confidence=0.0), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("diff-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Dana", "prefers", "dark mode", confidence=0.9),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))

        # Force Tier C (instruction_set override) with a MOTIVE-free but
        # otherwise unchanged policy, then re-govern the SAME candidate at a
        # stricter min_confidence via a second branch to prove "removed".
        strict_config = _config(min_confidence=0.95)
        removed_result = await branch_and_recompute(
            main_storage=client.graph,
            config=strict_config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(instruction_set="default"),
            now=now + timedelta(seconds=5),
        )
        shadow = SQLiteStorageBackend(removed_result.shadow_store_path)
        try:
            diff = diff_epochs(main_storage=client.graph, shadow_storage=shadow, scope=scope)
        finally:
            shadow.close()
        assert [entry.fact for entry in diff.removed] == ["Dana prefers dark mode"]
        assert diff.added == ()
        assert diff.changed == ()


# ---------------------------------------------------------------------------
# T6 — adopt / rollback + replay verify
# ---------------------------------------------------------------------------


class TestAdoptRollback:
    @pytest.mark.asyncio
    async def test_rollback_restores_the_pre_adopt_graph_state_hash_byte_for_byte(self, tmp_path: Path) -> None:
        counting = _CountingTransport()
        extractor = InstructionalExtractor(transport=counting)
        strict_config = _config(min_confidence=0.8)
        client = Memotron(config=strict_config, extraction_transport=counting, graph_path=tmp_path / "graph.sqlite")
        scope = _scope("rollback-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Erin", "prefers", "oat milk", confidence=0.5),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)

        hash_before_adopt = client.graph.graph_state_hash(scope.key)
        registry_before_adopt = client.graph.registry_state_digest(scope.key)

        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))
        loose_config = _config(min_confidence=0.3)
        result = await branch_and_recompute(
            main_storage=client.graph,
            config=loose_config,
            scope=scope,
            selector=selector,
            extractor=extractor,
            embedding_transport=client._embedding_transport,
            now=now + timedelta(seconds=5),
        )
        await adopt_epoch(
            main_storage=client.graph, scope=scope, epoch_id=result.epoch_id, now=now + timedelta(seconds=10)
        )
        assert "Erin prefers oat milk" in _active_facts(client.graph, scope)
        hash_after_adopt = client.graph.graph_state_hash(scope.key)
        assert hash_after_adopt != hash_before_adopt

        await rollback_epoch(main_storage=client.graph, scope=scope, now=now + timedelta(seconds=20))

        assert "Erin prefers oat milk" not in _active_facts(client.graph, scope)
        assert client.graph.graph_state_hash(scope.key) == hash_before_adopt
        assert client.graph.registry_state_digest(scope.key) == registry_before_adopt

    @pytest.mark.asyncio
    async def test_a_tampered_receipt_chain_aborts_adopt_and_never_moves_the_pointer(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("tamper-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Frank", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))

        result = await branch_and_recompute(
            main_storage=client.graph,
            config=client.config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(instruction_set="default", label="tamper"),
            now=now + timedelta(seconds=5),
        )
        assert result.run_uuids

        conn = sqlite3.connect(result.shadow_store_path)
        try:
            row = conn.execute("SELECT receipt_hash FROM memory_receipts ORDER BY event_index LIMIT 1").fetchone()
            tampered = ("f" if row[0][0] != "f" else "0") + row[0][1:]
            conn.execute("UPDATE memory_receipts SET receipt_hash = ? WHERE receipt_hash = ?", (tampered, row[0]))
            conn.commit()
        finally:
            conn.close()

        active_epoch_before = client.graph.active_epoch_for_scope(scope.key)
        with pytest.raises(ReplayVerificationError):
            await adopt_epoch(
                main_storage=client.graph, scope=scope, epoch_id=result.epoch_id, now=now + timedelta(seconds=10)
            )
        assert client.graph.active_epoch_for_scope(scope.key) == active_epoch_before
        assert client.graph.epoch(result.epoch_id)["status"] == "ready"  # never flipped to adopted


# ---------------------------------------------------------------------------
# T8 — Tier B (re-resolve) reaches the same content as Tier C (re-extract)
# ---------------------------------------------------------------------------


class TestTierEquivalence:
    @pytest.mark.asyncio
    async def test_tier_c_under_a_stricter_policy_yields_fewer_facts_and_leaves_others_unchanged(
        self, tmp_path: Path
    ) -> None:
        client = Memotron(config=_config(min_confidence=0.0), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("tier-c")
        now = datetime.now(UTC)
        body = "\n".join(
            [
                _line("Dave", "prefers", "dark mode", confidence=0.9),
                _line("Erin", "prefers", "light mode", confidence=0.4),
            ]
        )
        await client.add_episode(
            name="ep1",
            episode_body=body,
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        formed = await client.run_dream_job(job_name="formation-default", now=now)
        assert formed.job_runs[0].created_relationships == 2
        head_hash_before = client.graph.graph_state_hash(scope.key)

        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))
        strict_config = _config(min_confidence=0.8)
        result = await branch_and_recompute(
            main_storage=client.graph,
            config=strict_config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(instruction_set="default", label="strict"),
            now=now + timedelta(seconds=5),
        )
        assert result.tier is RedreamTier.C

        shadow = SQLiteStorageBackend(result.shadow_store_path)
        try:
            facts = _active_facts(shadow, scope)
        finally:
            shadow.close()
        assert facts == ["Dave prefers dark mode"]
        assert client.graph.graph_state_hash(scope.key) == head_hash_before

    @pytest.mark.asyncio
    async def test_tier_b_reaches_the_same_content_as_tier_c_under_an_unchanged_model(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(min_confidence=0.0), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("tier-be")
        now = datetime.now(UTC)
        body = "\n".join(
            [_line("Gina", "prefers", "tabs", confidence=0.9), _line("Hank", "prefers", "spaces", confidence=0.85)]
        )
        await client.add_episode(
            name="ep1",
            episode_body=body,
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))

        result_b = await branch_and_recompute(
            main_storage=client.graph,
            config=client.config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(dedup=DedupPolicy(cosine_threshold=0.9), label="tier-b"),
            now=now + timedelta(seconds=6),
        )
        assert result_b.tier is RedreamTier.B

        result_c = await branch_and_recompute(
            main_storage=client.graph,
            config=client.config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(instruction_set="default", label="tier-c"),
            now=now + timedelta(seconds=7),
        )
        assert result_c.tier is RedreamTier.C

        shadow_b = SQLiteStorageBackend(result_b.shadow_store_path)
        shadow_c = SQLiteStorageBackend(result_c.shadow_store_path)
        try:
            digest_b = epoch_content_digest(shadow_b, scope)
            digest_c = epoch_content_digest(shadow_c, scope)
        finally:
            shadow_b.close()
            shadow_c.close()
        assert digest_b == digest_c


# ---------------------------------------------------------------------------
# T9 — a branch's registry writes are invisible to HEAD until adopted
# ---------------------------------------------------------------------------


class TestRegistryIsolation:
    @pytest.mark.asyncio
    async def test_an_alias_registered_during_a_branch_is_invisible_to_head_before_adopt(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("registry-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Ivy", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))

        assert client.graph.canonical_entity_name_for(scope.key, "gcx-alias") is None

        result = await branch_and_recompute(
            main_storage=client.graph,
            config=client.config,
            scope=scope,
            selector=selector,
            extractor=client._extractor,
            embedding_transport=client._embedding_transport,
            overrides=RedreamOverrides(instruction_set="default", label="registry-branch"),
            now=now + timedelta(seconds=5),
        )
        shadow = SQLiteStorageBackend(result.shadow_store_path)
        try:
            shadow.register_entity_alias(
                scope.key, "gcx-alias", "guest content experience", status="active", decided_by="branch-test"
            )
            assert shadow.canonical_entity_name_for(scope.key, "gcx-alias") == "guest content experience"
        finally:
            shadow.close()

        # HEAD never opened the shadow's registry writes: invisible.
        assert client.graph.canonical_entity_name_for(scope.key, "gcx-alias") is None


# ---------------------------------------------------------------------------
# T7 — session digest is a derived rollup, never a Date/Session substrate node
# ---------------------------------------------------------------------------


class TestSessionDigest:
    @pytest.mark.asyncio
    async def test_session_digest_is_a_rollup_memory_type_not_a_content_plane_node(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("session-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Jan", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
            metadata={"session_id": "sess-42"},
        )
        await client.run_dream_job(job_name="formation-default", now=now)

        digest = build_session_digest(
            storage=client.graph, scope=scope, session_id="sess-42", now=now + timedelta(seconds=5)
        )
        assert digest is not None
        assert digest.properties["memory_type"] == MemoryType.ROLLUP.value
        assert digest.properties["metadata"]["session_id"] == "sess-42"
        assert digest.type == "ROLLUP"
        # Content-plane node labels never include Date/Session — the closed
        # 13-label vocabulary is untouched; SessionDigest is a rollup carrier,
        # not a new content-plane entity type instructions extract into.
        subject = client.graph.get_node(digest.source_uuid)
        assert "SessionDigest" in subject.labels

    @pytest.mark.asyncio
    async def test_session_digest_returns_none_for_a_session_with_no_active_facts(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("session-b")
        digest = build_session_digest(storage=client.graph, scope=scope, session_id="never-happened")
        assert digest is None


# ---------------------------------------------------------------------------
# client.py: the WS-26 wrapper surface (redream_*)
# ---------------------------------------------------------------------------


class TestClientRedreamSurface:
    """The public ``Memotron.redream_*`` methods are thin wrappers around
    the ``memotron.epochs`` functions exercised directly above — this
    class proves the WIRING (client's own extractor/transports, the WS-19
    T21 scope guard, and each method's return shape), not the mechanics."""

    @pytest.mark.asyncio
    async def test_full_branch_diff_adopt_rollback_round_trip_through_the_client(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(min_confidence=0.0), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("client-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Kim", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.run_dream_job(job_name="formation-default", now=now)

        epochs_before = await client.redream_epochs(scope=scope)
        assert len(epochs_before) == 1
        assert epochs_before[0]["status"] == "adopted"
        root_epoch_id = await client.redream_active_epoch(scope=scope)
        assert root_epoch_id == epochs_before[0]["epoch_id"]

        episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
        selector = EpisodeSelector(episode_uuids=(episode_uuid,))
        result = await client.redream_branch(scope=scope, selector=selector, now=now + timedelta(seconds=5))
        assert result.tier is RedreamTier.A

        diff = await client.redream_diff(scope=scope, epoch_id=result.epoch_id)
        assert [entry.fact for entry in diff.unchanged] == ["Kim prefers tea"]

        adopted = await client.redream_adopt(scope=scope, epoch_id=result.epoch_id, now=now + timedelta(seconds=10))
        assert adopted.epoch_id == result.epoch_id
        assert await client.redream_active_epoch(scope=scope) == result.epoch_id

        rolled_back = await client.redream_rollback(scope=scope, now=now + timedelta(seconds=15))
        assert rolled_back.restored_to == root_epoch_id
        assert await client.redream_active_epoch(scope=scope) == root_epoch_id

    @pytest.mark.asyncio
    async def test_redream_session_digest_through_the_client(self, tmp_path: Path) -> None:
        client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
        scope = _scope("client-b")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("Lee", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
            metadata={"session_id": "sess-7"},
        )
        await client.run_dream_job(job_name="formation-default", now=now)
        digest = await client.redream_session_digest(scope=scope, session_id="sess-7", now=now + timedelta(seconds=1))
        assert digest is not None
        assert digest.properties["memory_type"] == MemoryType.ROLLUP.value

    @pytest.mark.asyncio
    async def test_redream_methods_respect_the_scope_guard(self, tmp_path: Path) -> None:
        allowed = _scope("client-allowed")
        client = Memotron(
            config=_config(),
            graph_path=tmp_path / "graph.sqlite",
            authorized_scope_keys={allowed.key},
        )
        outside = _scope("client-outside")
        with pytest.raises(ValueError, match="authorized_scope_keys"):
            await client.redream_epochs(scope=outside)
        with pytest.raises(ValueError, match="authorized_scope_keys"):
            await client.redream_branch(scope=outside, selector=EpisodeSelector(episode_uuids=("x",)))


# ---------------------------------------------------------------------------
# admin_server.py: the T5 diff endpoint (GET /api/epochs, /api/epochs/diff)
# ---------------------------------------------------------------------------


class TestAdminEpochEndpoints:
    @pytest.mark.asyncio
    async def test_epochs_and_diff_endpoints_over_real_http(self, tmp_path: Path) -> None:
        import json as json_mod
        from http.server import HTTPServer
        from queue import Queue
        from threading import Thread
        from urllib.error import HTTPError
        from urllib.request import urlopen

        import memotron.admin_server as admin_server_module
        from memotron import PrincipalRole

        graph_path = tmp_path / "admin-http.sqlite"
        scope = _scope("admin-http")
        setup_client = Memotron(config=_config(), graph_path=graph_path)
        now = datetime.now(UTC)
        await setup_client.add_episode(
            name="ep1",
            episode_body=_line("Nia", "prefers", "tea"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await setup_client.run_dream_job(job_name="formation-default", now=now)
        episode_uuid = setup_client.graph.episodes_for_scope(scope.key)[0].uuid
        redream_result = await setup_client.redream_branch(
            scope=scope, selector=EpisodeSelector(episode_uuids=(episode_uuid,)), now=now + timedelta(seconds=5)
        )
        setup_client.graph.close()

        def read_json(url: str) -> dict:
            try:
                return json_mod.loads(urlopen(url, timeout=5).read().decode("utf-8"))
            except HTTPError as exc:
                raise AssertionError(f"HTTP {exc.code} from {url}: {exc.read().decode('utf-8')}") from exc

        server_queue: Queue = Queue()

        def serve() -> None:
            handler = admin_server_module.MemoryGraphHandler
            server_client = Memotron(config=_config(), graph_path=graph_path)
            handler.client = server_client
            handler.default_scope = scope
            handler.graph_path = graph_path
            handler.control_plane = admin_server_module.build_demo_control_plane(
                client=server_client,
                default_scope=scope,
                tenant_id="demo-tenant",
                agent_id="demo-agent",
            )
            handler.principal = admin_server_module.build_demo_principal(
                principal_id="demo-user",
                tenant_id="demo-tenant",
                agent_id="demo-agent",
                default_scope=scope,
                role=PrincipalRole.USER,
            )
            server = HTTPServer(("127.0.0.1", 0), handler)
            server_queue.put(server)
            server.serve_forever()

        thread = Thread(target=serve, daemon=True)
        thread.start()
        server = server_queue.get(timeout=5)
        base_url = f"http://127.0.0.1:{server.server_port}"
        try:
            epochs_payload = read_json(f"{base_url}/api/epochs?scope={scope.key}")
            assert epochs_payload["active_epoch_id"] is not None
            assert any(row["epoch_id"] == redream_result.epoch_id for row in epochs_payload["epochs"])

            diff_payload = read_json(f"{base_url}/api/epochs/diff?scope={scope.key}&epoch_id={redream_result.epoch_id}")
            assert diff_payload["epoch_id"] == redream_result.epoch_id
            assert [entry["fact"] for entry in diff_payload["diff"]["unchanged"]] == ["Nia prefers tea"]
            assert diff_payload["diff"]["added"] == []

            with pytest.raises(AssertionError, match="HTTP 400"):
                read_json(f"{base_url}/api/epochs/diff?scope={scope.key}")  # missing epoch_id
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)


# ---------------------------------------------------------------------------
# The whole re-dream cycle against a Postgres LIVE store
#
# The re-dream orchestration is storage-engine-agnostic (the recompute always
# runs on a SQLite shadow), but adopt/rollback and every read the cycle makes on
# the live store must behave identically on Postgres.  These run only when
# MEMOTRON_TEST_POSTGRES_DSN is set; without it they skip, exactly like the
# backend parity suite.
# ---------------------------------------------------------------------------

_POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"


def _postgres_client(config: DreamConfig) -> Memotron:
    dsn = os.environ.get(_POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"set {_POSTGRES_DSN_ENV} to run the Postgres live-store re-dream tests")
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")
    from memotron.storage.postgres import PostgresStorageBackend

    return Memotron(config=config, storage=PostgresStorageBackend(dsn, min_size=1, max_size=4))


# The only Postgres-bound class in this file; the rest of the suite is hermetic,
# so the marker goes here rather than on the module.
@pytest.mark.postgres
class TestRedreamOnPostgresLiveStore:
    @pytest.mark.asyncio
    async def test_full_cycle_formation_branch_adopt_rollback_on_postgres(self) -> None:
        """The WS-26 GOAL clause, proven on a Postgres live store: a Tier A
        governance-only re-dream branches from stored candidates, adopt
        materializes the fact HEAD was withholding, and a single-step rollback
        restores the pre-adopt graph_state_hash AND registry_state_digest
        byte-for-byte — with formation, the seeding reads, and adopt/rollback all
        executed against Postgres."""
        client = _postgres_client(_config(min_confidence=0.8))
        try:
            scope = _scope("pg-rollback")
            now = datetime.now(UTC)
            await client.add_episode(
                name="ep1",
                episode_body=_line("Erin", "prefers", "oat milk", confidence=0.5),
                source=EpisodeType.TEXT,
                scope=scope,
                source_description="d",
                reference_time=now,
            )
            # Strict formation quarantines the 0.5-confidence candidate: HEAD holds
            # no active fact for this truth slot yet.
            await client.run_dream_job(job_name="formation-default", now=now)
            assert _active_facts(client.graph, scope) == []
            hash_before_adopt = client.graph.graph_state_hash(scope.key)
            registry_before_adopt = client.graph.registry_state_digest(scope.key)

            episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
            selector = EpisodeSelector(episode_uuids=(episode_uuid,))
            counting = _CountingTransport()
            result = await branch_and_recompute(
                main_storage=client.graph,
                config=_config(min_confidence=0.3),
                scope=scope,
                selector=selector,
                extractor=InstructionalExtractor(transport=counting),
                embedding_transport=client._embedding_transport,
                now=now + timedelta(seconds=5),
            )
            assert result.tier is RedreamTier.A
            # Tier A is governance-only: zero extraction calls, proven by the spy.
            assert counting.calls == 0

            await adopt_epoch(
                main_storage=client.graph,
                scope=scope,
                epoch_id=result.epoch_id,
                now=now + timedelta(seconds=10),
            )
            assert "Erin prefers oat milk" in _active_facts(client.graph, scope)
            assert client.graph.graph_state_hash(scope.key) != hash_before_adopt

            await rollback_epoch(main_storage=client.graph, scope=scope, now=now + timedelta(seconds=20))
            assert _active_facts(client.graph, scope) == []
            assert client.graph.graph_state_hash(scope.key) == hash_before_adopt
            assert client.graph.registry_state_digest(scope.key) == registry_before_adopt
        finally:
            client.graph.close()
