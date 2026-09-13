"""WS-26: unit-level coverage for the derivation-DAG primitives.

* T1 — run-provenance stamps on materialized/superseded/pruned versions.
* T2 — epoch tables, the per-scope HEAD pointer, and ancestry resolution.
* T3 — episode selection (the four ``EpisodeSelector`` kinds).
* T8 — ``select_tier``, the pure override-set -> tier function.
* T9 — the registry epoch-lineage column and administrative overlay/delete
  primitives (isolation itself — "invisible to HEAD" — is proven end to end
  in ``test_redream_end_to_end.py``, since it is a property of the shadow
  store's physical separation, not of anything unit-testable in isolation).

Full branch -> recompute -> diff -> adopt/rollback flows, the zero-LLM-calls
proof, and the Tier B == Tier C content-equality proof live in
``tests/test_redream_end_to_end.py``.
"""

from __future__ import annotations

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
from memotron.config import DedupPolicy, EntityResolutionPolicy, GovernancePolicy, Motive
from memotron.epochs import (
    EpisodeSelector,
    RedreamOverrides,
    RedreamTier,
    select_episodes,
    select_tier,
)
from memotron.models import DreamJobKind, RelationshipCardinality, RelationshipStatus
from memotron.storage.sqlite import SQLiteStorageBackend

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _instructions() -> DreamInstructionSet:
    return DreamInstructionSet(
        name="default",
        node_instructions=(NodeInstruction(label="Entity", query="entities", properties=(), strict_properties=False),),
        relationship_instructions=(
            RelationshipInstruction(type="PREFERS", source_label="Entity", target_label="Entity", query="prefs"),
        ),
    )


def _config() -> DreamConfig:
    return DreamConfig(
        instruction_sets=(_instructions(),),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
    )


def _client(tmp_path: Path) -> Memotron:
    return Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")


def _line(subject: str, predicate: str, obj: str, confidence: float = 0.9) -> str:
    return (
        f"Memory: subject={subject}; predicate={predicate}; object={obj}; "
        f"relationship_type=PREFERS; confidence={confidence}"
    )


async def _formed(client: Memotron, scope: MemoryScope, body: str, now: datetime):
    await client.add_episode(
        name="ep",
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=now,
    )
    return await client.run_dream_job(job_name="formation-default", now=now)


# ---------------------------------------------------------------------------
# T2 — epoch tables + HEAD pointer
# ---------------------------------------------------------------------------


class TestEpochHeadPointer:
    def test_ensure_root_epoch_is_idempotent_and_resolves_to_exactly_one_head(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            first = backend.ensure_root_epoch("agent:x", now=now)
            second = backend.ensure_root_epoch("agent:x", now=now)
            assert first == second
            assert backend.active_epoch_for_scope("agent:x") == first
            row = backend.epoch(first)
            assert row["parent_epoch_id"] is None
            assert row["scope_key"] == "agent:x"
        finally:
            backend.close()

    def test_a_scope_that_never_touches_epochs_has_no_active_epoch(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            assert backend.active_epoch_for_scope("agent:untouched") is None
        finally:
            backend.close()

    def test_fork_sets_parent_epoch_id_and_leaves_head_untouched(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            root = backend.ensure_root_epoch("agent:x", now=now)
            child = backend.create_epoch(scope_key="agent:x", parent_epoch_id=root, now=now, label="branch")
            assert backend.epoch(child)["parent_epoch_id"] == root
            # HEAD is untouched by a fork — only set_active_epoch moves it.
            assert backend.active_epoch_for_scope("agent:x") == root
        finally:
            backend.close()

    def test_epoch_ancestry_is_self_first_then_ancestors_to_root(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            root = backend.ensure_root_epoch("agent:x", now=now)
            child = backend.create_epoch(scope_key="agent:x", parent_epoch_id=root, now=now)
            grandchild = backend.create_epoch(scope_key="agent:x", parent_epoch_id=child, now=now)
            assert backend.epoch_ancestry(grandchild) == (grandchild, child, root)
            assert backend.epoch_ancestry(root) == (root,)
        finally:
            backend.close()

    def test_set_active_epoch_is_the_pointer_flip(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            root = backend.ensure_root_epoch("agent:x", now=now)
            child = backend.create_epoch(scope_key="agent:x", parent_epoch_id=root, now=now)
            backend.set_active_epoch("agent:x", child, now=now)
            assert backend.active_epoch_for_scope("agent:x") == child
            backend.set_active_epoch("agent:x", root, now=now)
            assert backend.active_epoch_for_scope("agent:x") == root
        finally:
            backend.close()

    def test_epoch_run_uuids_are_recorded_and_ordered(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            root = backend.ensure_root_epoch("agent:x", now=now)
            backend.record_epoch_run(root, "run-1", now=now)
            backend.record_epoch_run(root, "run-2", now=now + timedelta(seconds=1))
            assert backend.epoch_run_uuids(root) == ["run-1", "run-2"]
            # Idempotent: replaying the same run is a no-op, not a duplicate.
            backend.record_epoch_run(root, "run-1", now=now)
            assert backend.epoch_run_uuids(root) == ["run-1", "run-2"]
        finally:
            backend.close()

    def test_epoch_run_recording_does_not_require_a_local_epoch_row(self) -> None:
        """A branch's shadow store records runs against an epoch_id whose
        bookkeeping row lives only in the MAIN store (see the epoch_runs DDL
        note) — record_epoch_run must not require a local graph_epochs row."""
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            backend.record_epoch_run("epoch-owned-elsewhere", "run-1", now=now)
            assert backend.epoch_run_uuids("epoch-owned-elsewhere") == ["run-1"]
        finally:
            backend.close()

    def test_epochs_for_scope_lists_every_epoch_oldest_first(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            root = backend.ensure_root_epoch("agent:x", now=now)
            child = backend.create_epoch(scope_key="agent:x", parent_epoch_id=root, now=now + timedelta(seconds=1))
            rows = backend.epochs_for_scope("agent:x")
            assert [row["epoch_id"] for row in rows] == [root, child]
        finally:
            backend.close()


# ---------------------------------------------------------------------------
# T1 — run provenance stamps
# ---------------------------------------------------------------------------


class TestRunProvenance:
    @pytest.mark.asyncio
    async def test_a_materialized_fact_carries_produced_by_run(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("prov-a")
        now = datetime.now(UTC)
        result = await _formed(client, scope, _line("Alice", "prefers", "green tea"), now)
        run_uuid = result.job_runs[0].run_uuid
        assert run_uuid is not None
        facts = [r for r in client.graph.relationships_for_scope(scope.key) if r.properties.get("status") == "active"]
        assert len(facts) == 1
        assert facts[0].properties.get("produced_by_run") == run_uuid
        assert facts[0].properties.get("epoch_id") is not None

    @pytest.mark.asyncio
    async def test_supersede_target_relationships_stamps_superseded_by_run(self, tmp_path: Path) -> None:
        """Exercises the SAME choke-point live formation uses
        (``DreamEngine._supersede_target_relationships``) via a REQUIRES
        (single_active) truth slot: a contradictory restatement supersedes
        the incumbent and stamps ``superseded_by_run`` with the SECOND run's
        uuid — the run that DID the superseding, not the one that created it.
        """
        config = DreamConfig(
            instruction_sets=(
                DreamInstructionSet(
                    name="default",
                    node_instructions=(
                        NodeInstruction(label="Entity", query="entities", properties=(), strict_properties=False),
                    ),
                    relationship_instructions=(
                        RelationshipInstruction(
                            type="REQUIRES",
                            source_label="Entity",
                            target_label="Entity",
                            query="reqs",
                            cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                        ),
                    ),
                ),
            ),
            jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        )
        client = Memotron(config=config, graph_path=tmp_path / "graph.sqlite")
        scope = _scope("prov-b")
        now = datetime.now(UTC)

        def req_line(subject: str, obj: str) -> str:
            return (
                f"Memory: subject={subject}; predicate=requires; object={obj}; "
                "relationship_type=REQUIRES; confidence=0.95"
            )

        first = await _formed(client, scope, req_line("Bob", "two-factor auth"), now)
        run_uuid_1 = first.job_runs[0].run_uuid
        assert run_uuid_1 is not None

        second = await _formed(client, scope, req_line("Bob", "single sign-on"), now + timedelta(hours=1))
        run_uuid_2 = second.job_runs[0].run_uuid
        assert run_uuid_2 is not None
        assert run_uuid_2 != run_uuid_1

        superseded = [
            r
            for r in client.graph.relationships_for_scope(scope.key)
            if r.properties.get("status") == RelationshipStatus.SUPERSEDED.value
        ]
        assert len(superseded) == 1
        assert superseded[0].properties.get("produced_by_run") == run_uuid_1
        assert superseded[0].properties.get("superseded_by_run") == run_uuid_2

    @pytest.mark.asyncio
    async def test_pruned_row_carries_pruned_by_run(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("prov-c")
        now = datetime.now(UTC)
        result = await _formed(client, scope, _line("Carol", "prefers", "coffee"), now)
        run_uuid = result.job_runs[0].run_uuid
        assert run_uuid is not None
        relationship = client.graph.relationships_for_scope(scope.key)[0]
        from memotron.models import RelationshipStatus

        marked = client.graph.mark_relationship(
            relationship.uuid,
            status=RelationshipStatus.PRUNED,
            properties={"pruned_by_run": run_uuid},
        )
        assert marked.properties["pruned_by_run"] == run_uuid

    @pytest.mark.asyncio
    async def test_provenance_fields_join_the_graph_state_hash_tuple(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("prov-d")
        now = datetime.now(UTC)
        await _formed(client, scope, _line("Dee", "prefers", "oolong"), now)
        relationship = client.graph.relationships_for_scope(scope.key)[0]
        before = client.graph.graph_state_hash(scope.key)
        # Re-stamping produced_by_run to a DIFFERENT run with no other change
        # must move the hash — it is part of the tuple, not incidental.
        client.graph.update_relationship(relationship.uuid, properties={"produced_by_run": "a-different-run"})
        after = client.graph.graph_state_hash(scope.key)
        assert after != before


# ---------------------------------------------------------------------------
# T3 — episode selection
# ---------------------------------------------------------------------------


class TestEpisodeSelector:
    def test_exactly_one_selector_field_is_required(self) -> None:
        with pytest.raises(ValueError):
            EpisodeSelector()
        with pytest.raises(ValueError):
            EpisodeSelector(session_id="s1", entity_uuid="e1")
        EpisodeSelector(session_id="s1")  # does not raise

    @pytest.mark.asyncio
    async def test_episode_uuids_selector_returns_exactly_the_named_episodes(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("sel-a")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("A", "prefers", "x"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
        )
        await client.add_episode(
            name="ep2",
            episode_body=_line("B", "prefers", "y"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now + timedelta(seconds=1),
        )
        episodes = client.graph.episodes_for_scope(scope.key)
        selector = EpisodeSelector(episode_uuids=(episodes[0].uuid,))
        selected = select_episodes(storage=client.graph, scope=scope, selector=selector)
        assert [e.uuid for e in selected] == [episodes[0].uuid]

    @pytest.mark.asyncio
    async def test_date_range_selector_is_half_open(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("sel-b")
        day1 = datetime(2026, 1, 1, tzinfo=UTC)
        day2 = datetime(2026, 1, 2, tzinfo=UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("A", "prefers", "x"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=day1,
        )
        await client.add_episode(
            name="ep2",
            episode_body=_line("B", "prefers", "y"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=day2,
        )
        selector = EpisodeSelector(date_range=(day1, day2))
        selected = select_episodes(storage=client.graph, scope=scope, selector=selector)
        assert len(selected) == 1
        assert selected[0].reference_time == day1

    @pytest.mark.asyncio
    async def test_session_id_selector_reads_episode_metadata(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("sel-c")
        now = datetime.now(UTC)
        await client.add_episode(
            name="ep1",
            episode_body=_line("A", "prefers", "x"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now,
            metadata={"session_id": "sess-1"},
        )
        await client.add_episode(
            name="ep2",
            episode_body=_line("B", "prefers", "y"),
            source=EpisodeType.TEXT,
            scope=scope,
            source_description="d",
            reference_time=now + timedelta(seconds=1),
            metadata={"session_id": "sess-2"},
        )
        selector = EpisodeSelector(session_id="sess-1")
        selected = select_episodes(storage=client.graph, scope=scope, selector=selector)
        assert len(selected) == 1
        assert selected[0].metadata["session_id"] == "sess-1"

    @pytest.mark.asyncio
    async def test_entity_uuid_selector_finds_episodes_that_mention_it(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("sel-d")
        now = datetime.now(UTC)
        await _formed(client, scope, _line("Erin", "prefers", "kombucha"), now)
        relationship = client.graph.relationships_for_scope(scope.key)[0]
        subject_node = client.graph.get_node(relationship.source_uuid)
        selector = EpisodeSelector(entity_uuid=subject_node.uuid)
        selected = select_episodes(storage=client.graph, scope=scope, selector=selector)
        assert len(selected) == 1

    @pytest.mark.asyncio
    async def test_selector_matching_zero_episodes_is_a_caller_error_at_branch_time(self, tmp_path: Path) -> None:
        client = _client(tmp_path)
        scope = _scope("sel-e")
        selector = EpisodeSelector(episode_uuids=("does-not-exist",))
        selected = select_episodes(storage=client.graph, scope=scope, selector=selector)
        assert selected == ()


# ---------------------------------------------------------------------------
# T8 — select_tier (pure function)
# ---------------------------------------------------------------------------


class TestSelectTier:
    def test_no_overrides_is_tier_a(self) -> None:
        tier, reason = select_tier(RedreamOverrides())
        assert tier is RedreamTier.A
        assert reason

    def test_governance_only_override_is_tier_a(self) -> None:
        tier, _ = select_tier(RedreamOverrides(governance=GovernancePolicy()))
        assert tier is RedreamTier.A

    def test_dedup_override_is_tier_b(self) -> None:
        tier, reason = select_tier(RedreamOverrides(dedup=DedupPolicy(cosine_threshold=0.5)))
        assert tier is RedreamTier.B
        assert "resolution" in reason or "dedup" in reason

    def test_entity_resolution_override_is_tier_b(self) -> None:
        tier, _ = select_tier(RedreamOverrides(entity_resolution=EntityResolutionPolicy()))
        assert tier is RedreamTier.B

    def test_motive_with_dedup_threshold_is_tier_b(self) -> None:
        motive = Motive(name="m", goal="g", dedup_threshold=0.5)
        tier, _ = select_tier(RedreamOverrides(motive="m"), resolved_motive=motive)
        assert tier is RedreamTier.B

    def test_instruction_set_override_is_tier_c(self) -> None:
        tier, reason = select_tier(RedreamOverrides(instruction_set="other"))
        assert tier is RedreamTier.C
        assert "instruction_set" in reason

    def test_extraction_transport_override_is_tier_c(self) -> None:
        tier, reason = select_tier(RedreamOverrides(extraction_transport=object()))
        assert tier is RedreamTier.C
        assert "extraction_transport" in reason

    def test_motive_with_prompt_override_is_tier_c(self) -> None:
        motive = Motive(name="m", goal="g", prompt_profile="alt-profile")
        tier, reason = select_tier(RedreamOverrides(motive="m"), resolved_motive=motive)
        assert tier is RedreamTier.C
        assert "prompt" in reason

    def test_motive_with_system_prompt_override_is_tier_c(self) -> None:
        motive = Motive(name="m", goal="g", system_prompt_override="alternate system prompt")
        tier, _ = select_tier(RedreamOverrides(motive="m"), resolved_motive=motive)
        assert tier is RedreamTier.C

    def test_a_tier_is_never_silently_upgraded_past_extraction_transport(self) -> None:
        # Even with a dedup override ALSO present, an explicit model change
        # still wins to Tier C — the more expensive tier a real change
        # requires, never masked by a cheaper one also being set.
        tier, _ = select_tier(RedreamOverrides(dedup=DedupPolicy(cosine_threshold=0.5), extraction_transport=object()))
        assert tier is RedreamTier.C


# ---------------------------------------------------------------------------
# T9 — registry epoch-lineage column and administrative primitives
# ---------------------------------------------------------------------------


class TestRegistryLineage:
    def test_entity_canon_rows_default_to_the_legacy_epoch_bucket(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            backend.register_entity_alias(
                "agent:x", "gcx", "guest content experience", status="active", decided_by="test"
            )
            row = backend.entity_alias_row("agent:x", "gcx")
            assert row is not None
            assert row["epoch_id"] == ""
        finally:
            backend.close()

    def test_overlay_entity_alias_row_upserts_and_tags_the_epoch(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            epoch_id = backend.ensure_root_epoch("agent:x", now=now)
            backend.overlay_entity_alias_row(
                "agent:x",
                {
                    "name_normalized": "gcx",
                    "canonical_name": "guest content experience",
                    "status": "active",
                    "decided_by": "branch",
                    "link_score": 0.9,
                    "link_signals": {},
                    "embedding_identifier": None,
                    "proposed_at": now.isoformat(),
                    "resolved_at": None,
                    "resolved_by": None,
                },
                epoch_id=epoch_id,
            )
            row = backend.entity_alias_row("agent:x", "gcx")
            assert row is not None
            assert row["canonical_name"] == "guest content experience"
            assert row["epoch_id"] == epoch_id
        finally:
            backend.close()

    def test_delete_entity_alias_row_removes_it_entirely(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            backend.register_entity_alias("agent:x", "gcx", "guest content experience", status="active", decided_by="t")
            assert backend.entity_alias_row("agent:x", "gcx") is not None
            backend.delete_entity_alias_row("agent:x", "gcx")
            assert backend.entity_alias_row("agent:x", "gcx") is None
        finally:
            backend.close()

    def test_overlay_predicate_row_upserts_and_tags_the_epoch(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            now = datetime.now(UTC)
            epoch_id = backend.ensure_root_epoch("agent:x", now=now)
            backend.overlay_predicate_row(
                "agent:x",
                {
                    "predicate_normalized": "resides in",
                    "canonical_predicate": "lives in",
                    "decided_by": "branch",
                    "embedding_identifier": None,
                    "cosine": None,
                    "created_at": now.isoformat(),
                },
                epoch_id=epoch_id,
            )
            row = backend.predicate_alias_row("agent:x", "resides in")
            assert row is not None
            assert row["canonical_predicate"] == "lives in"
            assert row["epoch_id"] == epoch_id
        finally:
            backend.close()

    def test_registry_state_digest_changes_when_an_alias_is_added(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            before = backend.registry_state_digest("agent:x")
            backend.register_entity_alias("agent:x", "gcx", "guest content experience", status="active", decided_by="t")
            after = backend.registry_state_digest("agent:x")
            assert before != after
        finally:
            backend.close()

    def test_registry_state_digest_is_stable_for_an_untouched_scope(self) -> None:
        backend = SQLiteStorageBackend(":memory:")
        try:
            assert backend.registry_state_digest("agent:x") == backend.registry_state_digest("agent:x")
        finally:
            backend.close()
