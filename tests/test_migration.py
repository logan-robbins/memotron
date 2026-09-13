"""WS-13: Tenant memory migration primitive.

Every test builds its own isolated tmp_path graph and runs fully offline
(rule-based/no-LLM writes via ``Memotron.add_memory``), matching this
repo's existing test style (see test_erasure.py).
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    ErasureBehavior,
    GovernancePolicy,
    NodeInstruction,
    RelationshipInstruction,
    agent_scope,
    cli,
    project_scope,
)
from memotron.graph import PropertyGraphStore
from memotron.migration import migrate_tenant_memory, preview_tenant_migration
from memotron.models import DreamJobKind


def _config(*, crypto_shred: bool = False) -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Durable people, agents, teams, and concepts.",
                properties=("kind",),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Stable preferences.",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Explicit requirements.",
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        governance=GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED) if crypto_shred else None,
    )


async def _seed_tenant(
    client: Memotron,
    *,
    tenant_id: str,
    facts: tuple[tuple[str, str, str], ...] = (
        ("Priya", "prefers", "dark mode"),
        ("Priya", "requires", "SSO login"),
    ),
) -> None:
    scope = project_scope(tenant_id)
    for subject, predicate, obj in facts:
        relationship_type = "REQUIRES" if predicate == "requires" else "PREFERS"
        await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=obj,
            relationship_type=relationship_type,
            scope=scope,
            source_description="test fact",
        )


def _active_facts(store: PropertyGraphStore, tenant_id: str) -> list[str]:
    return _active_facts_in_scope(store, project_scope(tenant_id))


def _active_facts_in_scope(store: PropertyGraphStore, scope) -> list[str]:
    facts = []
    for relationship in store.active_relationships(scope=scope):
        if relationship.type == "MENTIONS":
            continue
        subject = store.get_node(relationship.source_uuid).properties["name"]
        facts.append(f"{subject} {relationship.properties['predicate']} {relationship.properties['object']}")
    return sorted(facts)


@pytest.mark.asyncio
async def test_preview_reports_counts_and_side_table_presence(tmp_path) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="claude-code", name="Claude Code")
    client.graph.set_tenant_llm_credentials(
        tenant_id="old-tenant", provider="litellm", api_key="sk-test", model="claude-x"
    )
    client.graph.set_tenant_prompt_override(
        tenant_id="old-tenant",
        prompt_profile="support-memory",
        prompt_profile_version="v2",
        override={"tone": "concise"},
    )

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")

    assert preview.blocked_reason is None
    assert preview.active_relationship_count == 2
    assert preview.episode_count == 2
    assert preview.node_count >= 2
    assert preview.agent_registration_count == 1
    assert preview.has_llm_credentials is True
    assert preview.has_prompt_override is True
    client.graph.close()


@pytest.mark.asyncio
async def test_migrate_within_one_shared_graph_file(tmp_path) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="claude-code", name="Claude Code")
    client.graph.set_agent_motive_assignment(
        tenant_id="old-tenant", agent_id="claude-code", motive_name="agent-memory", source="test"
    )
    client.graph.set_tenant_llm_credentials(
        tenant_id="old-tenant", provider="litellm", api_key="sk-test", model="claude-x"
    )
    client.graph.save_tenant_prompt_version(tenant_id="old-tenant", prompt_text="v1 prompt")
    client.graph.save_tenant_prompt_version(tenant_id="old-tenant", prompt_text="v2 prompt")

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")
    result = migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )

    assert result.relationships_migrated == preview.active_relationship_count == 2
    assert result.episodes_migrated == preview.episode_count == 2
    assert result.agents_migrated == 1
    assert result.agent_motive_assignments_migrated == 1
    assert result.llm_credentials_migrated is True
    assert result.prompt_versions_migrated == 2
    assert result.purged_source is True
    assert result.run_uuid is not None

    assert _active_facts(client.graph, "new-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(client.graph, "old-tenant") == []

    dest_agent = client.graph.tenant_agent(tenant_id="new-tenant", agent_id="claude-code")
    assert dest_agent is not None
    dest_assignment = client.graph.agent_motive_assignment(tenant_id="new-tenant", agent_id="claude-code")
    assert dest_assignment is not None and dest_assignment["motive_name"] == "agent-memory"
    dest_credentials = client.graph.tenant_llm_credentials("new-tenant")
    assert dest_credentials is not None and dest_credentials["api_key"] == "sk-test"
    active_version = client.graph.active_tenant_prompt_version("new-tenant")
    assert active_version is not None and active_version["prompt_text"] == "v2 prompt"

    # Old tenant's own agent registration is gone (purge_tenant_state cleans it up);
    # its raw episodes and sealed LLM credentials are preserved by that function.
    assert client.graph.tenant_agent(tenant_id="old-tenant", agent_id="claude-code") is None
    assert len(client.graph.episodes_for_scope(project_scope("old-tenant").key)) == 2
    client.graph.close()


@pytest.mark.asyncio
async def test_migrate_across_two_distinct_graph_files(tmp_path) -> None:
    source_client = Memotron(config=_config(), graph_path=tmp_path / "source.sqlite")
    await _seed_tenant(source_client, tenant_id="old-tenant")
    dest_store = PropertyGraphStore(tmp_path / "dest.sqlite")

    preview = preview_tenant_migration(
        source_client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        dest_store=dest_store,
    )
    result = migrate_tenant_memory(
        source_client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        dest_store=dest_store,
        purge_source=True,
    )

    assert result.relationships_migrated == preview.active_relationship_count == 2
    assert _active_facts(dest_store, "new-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(source_client.graph, "old-tenant") == []
    assert len(dest_store.episodes_for_scope(project_scope("new-tenant").key)) == 2

    source_client.graph.close()
    dest_store.close()


@pytest.mark.asyncio
async def test_refuses_crypto_shred_governed_source(tmp_path) -> None:
    client = Memotron(config=_config(crypto_shred=True), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")
    assert preview.blocked_reason is not None
    assert "CRYPTO_SHRED" in preview.blocked_reason

    with pytest.raises(ValueError, match="CRYPTO_SHRED"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
        )
    client.graph.close()


@pytest.mark.asyncio
async def test_same_file_move_transfers_agent_scoped_memory_instead_of_purging_it(
    tmp_path,
) -> None:
    """The purge sweeps agent scopes of still-registered agents — it must not here.

    Within one graph file an agent scope key is already its final key, so the
    move transfers ownership (``tenant_agents``) and the facts must survive
    untouched.
    """
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")
    assert preview.agent_scoped_relationship_count == 1
    assert preview.blocked_reason is None

    result = migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )
    assert result.purged_source is True
    assert result.agent_scoped_relationships_migrated == 1

    # The agent-scoped fact survived the purge, at its unchanged scope key...
    assert _active_facts(client.graph, "old-tenant") == []
    assert _active_facts_in_scope(client.graph, agent_scope("worker")) == ["worker learned retry with backoff"]
    # ...and the agent now belongs to the destination tenant only.
    assert [a["agent_id"] for a in client.graph.tenant_agents("new-tenant")] == ["worker"]
    assert client.graph.tenant_agents("old-tenant") == ()
    client.graph.close()


@pytest.mark.asyncio
async def test_cross_file_move_copies_agent_scoped_memory_under_the_same_key(
    tmp_path,
) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "source.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )
    destination = PropertyGraphStore(tmp_path / "destination.sqlite")

    result = migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        dest_store=destination,
        purge_source=True,
    )
    assert result.agent_scoped_relationships_migrated == 1

    assert _active_facts_in_scope(destination, agent_scope("worker")) == ["worker learned retry with backoff"]
    # The source file's agent scope was swept, since its content now lives in
    # the destination file.
    assert _active_facts_in_scope(client.graph, agent_scope("worker")) == []
    destination.close()
    client.graph.close()
    client.graph.close()


@pytest.mark.asyncio
async def test_purge_failure_between_copy_and_purge_never_loses_data(tmp_path, monkeypatch) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")

    def _boom(tenant_id: str, **kwargs):
        raise RuntimeError("simulated failure between copy and purge")

    monkeypatch.setattr(client.graph, "purge_tenant_state", _boom)

    with pytest.raises(RuntimeError, match="simulated failure"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
            purge_source=True,
        )

    # The failure happened strictly after copy+verify: the source is untouched
    # (never lost) and the destination already has the copy (at most duplicated).
    assert _active_facts(client.graph, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(client.graph, "new-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    client.graph.close()


def _write_fact_through_another_connection(graph_path: Path, *, scope, subject: str, predicate: str, obj: str) -> None:
    """Write one active fact into *scope* from a SEPARATE store handle.

    This is the multi-agent case the copy->verify->purge window has to survive:
    a different process holding its own connection to the same graph file. It
    writes through the same low-level primitives migration itself writes
    through, so the row is indistinguishable from any other agent's write.
    """
    other = PropertyGraphStore(graph_path)
    try:
        scope_label = scope.kind.value.title()
        node, _ = other.upsert_node(
            labels=("Entity", scope_label),
            key=f"{scope.key}|Entity|{subject}",
            properties={
                "name": subject,
                "scope_kind": scope.kind.value,
                "scope_id": scope.scope_id,
                "scope_key": scope.key,
            },
        )
        target, _ = other.upsert_node(
            labels=("Entity", scope_label),
            key=f"{scope.key}|Entity|{obj}",
            properties={
                "name": obj,
                "scope_kind": scope.kind.value,
                "scope_id": scope.scope_id,
                "scope_key": scope.key,
            },
        )
        other.add_relationship(
            source_uuid=node.uuid,
            target_uuid=target.uuid,
            relationship_type="PREFERS",
            properties={
                "scope_kind": scope.kind.value,
                "scope_id": scope.scope_id,
                "scope_key": scope.key,
                "status": "active",
                "predicate": predicate,
                "object": obj,
                "fact": f"{subject} {predicate} {obj}",
                "memory_type": "preference",
                "confidence": 0.9,
            },
        )
    finally:
        other.close()


@pytest.mark.asyncio
async def test_write_landing_between_copy_and_purge_aborts_instead_of_destroying_it(tmp_path, monkeypatch) -> None:
    """The concurrency window the whole copy->verify->purge ordering exists for.

    A fact written by another agent AFTER the copy read the source but BEFORE
    the purge was, until the pre-purge watermark re-verify existed, destroyed
    silently — the purge deleted it and ``TenantMigrationResult`` still reported
    success. The migration must now refuse to purge and say so.
    """
    from memotron import migration as migration_module

    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")

    real_verify_counts = migration_module._verify_migration_counts

    def _interleave(*args, **kwargs):
        # Lands in the copy -> purge window, from a second connection.
        _write_fact_through_another_connection(
            graph_path,
            scope=project_scope("old-tenant"),
            subject="Priya",
            predicate="prefers",
            obj="keyboard shortcuts",
        )
        return real_verify_counts(*args, **kwargs)

    monkeypatch.setattr(migration_module, "_verify_migration_counts", _interleave)

    with pytest.raises(RuntimeError, match="the source changed after it was copied"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
            purge_source=True,
        )

    # Nothing was purged: the source still holds BOTH the copied facts and the
    # interleaved write that would otherwise have been destroyed.
    assert _active_facts(client.graph, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya prefers keyboard shortcuts",
        "Priya requires SSO login",
    ]
    client.graph.close()


@pytest.mark.asyncio
async def test_interleaved_agent_scope_write_also_aborts_the_purge(tmp_path, monkeypatch) -> None:
    """The same guard covers agent scopes, which the purge also sweeps.

    In multi-agent mode agent-scope facts are the overwhelming majority, so a
    watermark over the tenant scope alone would leave the common case unguarded.
    """
    from memotron import migration as migration_module

    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )

    real_verify_counts = migration_module._verify_migration_counts

    def _interleave(*args, **kwargs):
        result = real_verify_counts(*args, **kwargs)
        _write_fact_through_another_connection(
            graph_path,
            scope=agent_scope("worker"),
            subject="worker",
            predicate="learned",
            obj="prefer idempotent retries",
        )
        return result

    monkeypatch.setattr(migration_module, "_verify_migration_counts", _interleave)

    with pytest.raises(RuntimeError, match="the source changed after it was copied"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
            purge_source=True,
        )

    assert _active_facts_in_scope(client.graph, agent_scope("worker")) == [
        "worker learned prefer idempotent retries",
        "worker learned retry with backoff",
    ]
    assert _active_facts(client.graph, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    client.graph.close()


def test_exclusive_write_transaction_actually_locks_out_other_writers(tmp_path) -> None:
    """The verify->purge critical section is a real lock, not a comment.

    Without this, ``_verify_source_unchanged`` would only ever be a statement
    about the past: a writer could land between the check and the purge.
    """
    import sqlite3

    graph_path = tmp_path / "graph.sqlite"
    store = PropertyGraphStore(graph_path)
    other = sqlite3.connect(graph_path)
    other.execute("PRAGMA busy_timeout = 0")  # fail immediately instead of waiting
    try:
        with store.exclusive_write_transaction(), pytest.raises(sqlite3.OperationalError, match="locked"):
            other.execute("BEGIN IMMEDIATE")
        # ...and the lock is released on exit.
        other.execute("BEGIN IMMEDIATE")
        other.rollback()
    finally:
        other.close()
        store.close()


@pytest.mark.asyncio
async def test_same_store_agent_verification_can_actually_fail(tmp_path, monkeypatch) -> None:
    """The same-store agent-scope count must be re-derived, not echoed.

    Assigning ``agent_relationships_migrated = preview.agent_scoped_relationship_count``
    and then checking it against that same preview value is a tautology that can
    never fail. Re-counting live from the store means an agent scope that grew
    after the preview is caught by the count check, BEFORE the purge.
    """
    from memotron import migration as migration_module

    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )

    real_migrate_relationships = migration_module._migrate_relationships

    def _grow_agent_scope(*args, **kwargs):
        # After the preview was taken, before the agent scopes are counted.
        _write_fact_through_another_connection(
            graph_path,
            scope=agent_scope("worker"),
            subject="worker",
            predicate="learned",
            obj="cache DNS lookups",
        )
        return real_migrate_relationships(*args, **kwargs)

    monkeypatch.setattr(migration_module, "_migrate_relationships", _grow_agent_scope)

    with pytest.raises(RuntimeError, match="agent-scoped relationships"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
            purge_source=True,
        )

    # Refused before the purge — both agent facts survive.
    assert _active_facts_in_scope(client.graph, agent_scope("worker")) == [
        "worker learned cache DNS lookups",
        "worker learned retry with backoff",
    ]
    client.graph.close()


@pytest.mark.asyncio
async def test_refuses_crypto_shred_governed_agent_scope(tmp_path) -> None:
    """A crypto-shredded AGENT scope blocks the move exactly like a tenant scope.

    Agent scopes move too, so a keyed-commitment agent scope must refuse for the
    same reason — testing only the tenant scope left the multi-agent case (where
    nearly all facts live) unguarded.
    """
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )
    # Only the agent scope is content-protected; the tenant scope is plaintext.
    client.graph.get_or_create_governance_key(agent_scope("worker").key)
    assert client.graph.governance_key_state(project_scope("old-tenant").key) is None

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")
    assert preview.blocked_reason is not None
    assert "CRYPTO_SHRED" in preview.blocked_reason
    assert agent_scope("worker").key in preview.blocked_reason

    with pytest.raises(ValueError, match="CRYPTO_SHRED"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="new-tenant",
        )
    assert _active_facts(client.graph, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    client.graph.close()


@pytest.mark.asyncio
async def test_same_tenant_id_within_one_file_is_refused(tmp_path) -> None:
    """Copying a scope onto itself and then purging it would delete both copies."""
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")

    with pytest.raises(ValueError, match="nothing to migrate"):
        migrate_tenant_memory(
            client.graph,
            source_tenant_id="old-tenant",
            dest_tenant_id="old-tenant",
            purge_source=True,
        )
    assert _active_facts(client.graph, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    client.graph.close()


@pytest.mark.asyncio
async def test_migration_receipts_replay_and_match_the_live_destination(tmp_path) -> None:
    """Per-row receipt brackets must still form a contiguous, replayable chain.

    The state hashes on those receipts are produced incrementally (one scope
    scan, folded per row) rather than by two full recomputes per row. This is
    the proof that the cheaper path produces the SAME digests: byte replay folds
    the receipt stream back to the live graph state hash.
    """
    from memotron.replay import verify_no_silent_mutation

    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(
        client,
        tenant_id="old-tenant",
        facts=(
            ("Priya", "prefers", "dark mode"),
            ("Priya", "requires", "SSO login"),
            ("Dev", "prefers", "trunk based development"),
            ("Dev", "requires", "signed commits"),
        ),
    )

    migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )

    dest_key = project_scope("new-tenant").key
    report = await verify_no_silent_mutation(client.graph.receipts, graph=client.graph, scope_key=dest_key)
    assert list(report.errors) == []
    assert report.mutating_receipt_count == 4
    client.graph.close()


@pytest.mark.asyncio
async def test_migration_receipts_replay_into_a_sealed_destination(tmp_path) -> None:
    """The same contiguity proof for a content-protected destination.

    Sealed scopes hash over write-time ``fact_commitment`` values rather than
    plaintext, so this exercises the other branch of the state-tuple builder the
    incremental tracker shares with a full recompute.
    """
    from memotron.replay import verify_no_silent_mutation

    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(
        client,
        tenant_id="old-tenant",
        facts=(
            ("Priya", "prefers", "dark mode"),
            ("Priya", "requires", "SSO login"),
            ("Dev", "prefers", "trunk based development"),
        ),
    )
    dest_scope = project_scope("new-tenant")
    client.graph.get_or_create_governance_key(dest_scope.key)

    migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )

    report = await verify_no_silent_mutation(client.graph.receipts, graph=client.graph, scope_key=dest_scope.key)
    assert list(report.errors) == []
    assert report.mutating_receipt_count == 3
    client.graph.close()


@pytest.mark.asyncio
async def test_state_hash_tracker_matches_a_full_recompute_after_every_write(
    tmp_path,
) -> None:
    """The incremental tracker is an optimization, never a different hash.

    Migration records per-row receipt brackets off a tracker instead of calling
    ``graph_state_hash`` twice per row. That is only legitimate if the digests
    are byte-identical to what the full recompute would have produced.
    """
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    scope = project_scope("tenant")
    await _seed_tenant(client, tenant_id="tenant")
    store = client.graph

    tracker = store.scope_state_hash_tracker(scope.key)
    assert tracker.digest == store.graph_state_hash(scope.key)

    for index in range(4):
        _write_fact_through_another_connection(
            tmp_path / "graph.sqlite",
            scope=scope,
            subject="Priya",
            predicate="prefers",
            obj=f"option {index}",
        )
        written = max(store.active_relationships(scope=scope), key=lambda r: r.created_at)
        assert tracker.record(written) == store.graph_state_hash(scope.key)
    store.close()


@pytest.mark.asyncio
async def test_migration_state_hash_cost_does_not_grow_with_the_number_of_facts(tmp_path, monkeypatch) -> None:
    """Finding: ``graph_state_hash`` is uncached and O(scope).

    Calling it twice per migrated row made a move quadratic on exactly the large
    graphs migration exists for. Bounding it per batch means the number of
    recomputes is a constant of the migration's shape, not of its size — so
    tripling the fact count must not change the call count at all.
    """
    calls: list[int] = []

    async def _migrate_with(fact_count: int, name: str) -> int:
        graph_path = tmp_path / f"{name}.sqlite"
        client = Memotron(config=_config(), graph_path=graph_path)
        await _seed_tenant(
            client,
            tenant_id="old-tenant",
            facts=tuple(("Priya", "prefers", f"option {index}") for index in range(fact_count)),
        )
        client.graph.close()

        store = PropertyGraphStore(graph_path)
        real = PropertyGraphStore.graph_state_hash
        counter = 0

        def _counting(self, scope_key):
            nonlocal counter
            counter += 1
            return real(self, scope_key)

        monkeypatch.setattr(PropertyGraphStore, "graph_state_hash", _counting)
        try:
            result = migrate_tenant_memory(
                store,
                source_tenant_id="old-tenant",
                dest_tenant_id="new-tenant",
                purge_source=True,
            )
        finally:
            monkeypatch.undo()
            store.close()
        assert result.relationships_migrated == fact_count
        return counter

    calls.append(await _migrate_with(4, "small"))
    calls.append(await _migrate_with(12, "large"))

    assert calls[0] == calls[1], f"state-hash recomputes scale with the migrated row count: {calls}"


@pytest.mark.asyncio
async def test_source_receipt_chain_still_verifies_after_migration(tmp_path) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")

    source_scope = project_scope("old-tenant")
    pre_migration_receipts = client.graph.receipts.receipts_for_scope(source_scope.key)
    assert pre_migration_receipts, "seeding must have produced source receipts to protect"
    run_uuids_before = {receipt.run_uuid for receipt in pre_migration_receipts}
    for run_uuid in run_uuids_before:
        assert client.graph.receipts.verify_chain(run_uuid).valid

    migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )

    post_migration_receipts = client.graph.receipts.receipts_for_scope(source_scope.key)
    assert {r.receipt_hash for r in post_migration_receipts} == {r.receipt_hash for r in pre_migration_receipts}
    for run_uuid in run_uuids_before:
        verification = client.graph.receipts.verify_chain(run_uuid)
        assert verification.valid, verification.errors
    client.graph.close()


@pytest.mark.asyncio
async def test_move_never_deletes_the_source_receipt_rows(tmp_path, monkeypatch) -> None:
    """The source chain survives because it is never removed — not re-inserted.

    Deleting the rows and restoring them afterwards would commit twice, and a
    crash between those two commits would destroy the source tenant's audit
    history permanently. Asserting the purge itself deletes nothing is what
    proves no such window exists; asserting only that the rows are present
    afterwards would pass for the delete-then-restore version too.
    """
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    assert client.graph.receipts.receipts_for_scope(project_scope("old-tenant").key)

    purge = client.graph.purge_tenant_state
    observed: list[tuple[dict, dict]] = []

    def _spy(tenant_id: str, **kwargs) -> dict:
        counts = purge(tenant_id, **kwargs)
        observed.append((kwargs, counts))
        return counts

    monkeypatch.setattr(client.graph, "purge_tenant_state", _spy)
    migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=True,
    )

    assert len(observed) == 1
    kwargs, counts = observed[0]
    assert kwargs["preserve_receipts"] is True
    assert counts["memory_receipts_deleted"] == 0
    assert counts["run_checkpoints_deleted"] == 0
    # The purge still did its actual job.
    assert _active_facts(client.graph, "old-tenant") == []
    client.graph.close()


def test_purge_still_deletes_receipts_by_default(tmp_path) -> None:
    """preserve_receipts is opt-in: the admin purge action keeps its behaviour."""
    store = PropertyGraphStore(tmp_path / "graph.sqlite")
    counts = store.purge_tenant_state("some-tenant")
    assert "memory_receipts_deleted" in counts
    assert "run_checkpoints_deleted" in counts
    store.close()


@pytest.mark.asyncio
async def test_migrating_into_crypto_shred_destination_seals_content(tmp_path) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant", facts=(("Priya", "prefers", "dark mode"),))

    dest_scope = project_scope("new-tenant")
    client.graph.get_or_create_governance_key(dest_scope.key)

    migrate_tenant_memory(
        client.graph,
        source_tenant_id="old-tenant",
        dest_tenant_id="new-tenant",
        purge_source=False,
    )

    from memotron.crypto import is_sealed_content

    relationships = client.graph.active_relationships(scope=dest_scope)
    assert relationships
    assert is_sealed_content(relationships[0].properties["fact"])
    client.graph.close()


def test_blank_tenant_ids_are_rejected(tmp_path) -> None:
    store = PropertyGraphStore(tmp_path / "graph.sqlite")
    with pytest.raises(ValueError):
        preview_tenant_migration(store, source_tenant_id="  ", dest_tenant_id="new-tenant")
    with pytest.raises(ValueError):
        preview_tenant_migration(store, source_tenant_id="old-tenant", dest_tenant_id=" ")
    store.close()


@pytest.mark.asyncio
async def test_preview_reports_agent_scoped_facts_and_blocks_move_not_copy(tmp_path) -> None:
    """A multi-agent tenant must never be previewed by its tenant-scope count alone.

    In multi-agent mode nearly all facts live in agent scope, which a v1
    migration does not move. Reporting only ``active_relationship_count``
    tells an operator "1 fact" for a graph holding thousands, and the move is
    then refused *after* they confirm it.
    """
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.register_tenant_agent(tenant_id="old-tenant", agent_id="worker", name="Worker")
    await client.add_memory(
        subject="worker",
        predicate="learned",
        object="retry with backoff",
        relationship_type="PREFERS",
        scope=agent_scope("worker"),
        source_description="agent-private fact",
    )

    preview = preview_tenant_migration(client.graph, source_tenant_id="old-tenant", dest_tenant_id="new-tenant")
    assert preview.agent_scoped_relationship_count == 1
    assert preview.agent_scoped_episode_count >= 1
    # Reported separately from the tenant-scope count, never folded into it.
    assert preview.active_relationship_count == 2
    assert preview.blocked_reason is None
    client.graph.close()


# --- `memotron init` wizard: non-interactive safety -------------------------
# The wizard's most safety-critical property is that a non-TTY caller (CI, a
# Claude Code hook, any piped invocation) is never prompted — a prompt there
# hangs the caller forever. These guard that gate in both directions.


def _git_repo(path: Path) -> Path:
    """Minimal Git repo with an origin remote, so project identity can be inferred."""
    path.mkdir(parents=True)
    subprocess.run(("git", "init", str(path)), check=True, capture_output=True)
    subprocess.run(
        ("git", "-C", str(path), "remote", "add", "origin", "git@github.example.com:parks/sample-repo.git"),
        check=True,
        capture_output=True,
    )
    return path


def _init_args(root: Path, **overrides: object) -> argparse.Namespace:
    fields: dict[str, object] = {
        "project_root": str(root),
        "mode": None,
        "project_id": "",
        "project_name": "",
        "project_goal": "",
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


def test_init_never_prompts_when_stdin_is_not_a_tty(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))  # keep ~/.memotron out of it
    root = _git_repo(tmp_path / "sample-repo")
    monkeypatch.setattr(sys, "stdin", io.StringIO())  # a pipe: isatty() is False

    def _explode(prompt: str = "") -> str:
        raise AssertionError(f"init prompted in a non-interactive session: {prompt!r}")

    monkeypatch.setattr("builtins.input", _explode)
    cli._run_init(_init_args(root, project_id="sample-repo", project_name="Sample Repo"))

    assert json.loads(capsys.readouterr().out)["project_id"] == "sample-repo"


def test_init_prompts_for_unspecified_fields_when_stdin_is_a_tty(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    root = _git_repo(tmp_path / "sample-repo")

    class _Tty(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.setattr(sys, "stdin", _Tty())
    asked: list[str] = []

    def _accept_default(prompt: str = "") -> str:
        asked.append(prompt)
        return ""

    monkeypatch.setattr("builtins.input", _accept_default)
    cli._run_init(_init_args(root))

    # mode, project id, project name, goal, LLM provider — all defaulted.
    assert len(asked) >= 5
    assert json.loads(capsys.readouterr().out)["project_id"]


# --- `memotron migrate`: the user-facing destructive entrypoint -------------
# This command's default is a purge of the source tenant. It gets the same
# non-TTY gate `init` has: relying on input() raising EOFError does not save an
# open-but-idle pipe from hanging forever on a destructive prompt.


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def _migrate_args(graph_path: Path, **overrides: object) -> argparse.Namespace:
    fields: dict[str, object] = {
        "from_tenant_id": "old-tenant",
        "to_tenant_id": "new-tenant",
        "graph_path": str(graph_path),
        "source_graph_path": "",
        "keep_source": False,
        "yes": False,
    }
    fields.update(overrides)
    return argparse.Namespace(**fields)


@pytest.mark.asyncio
async def test_migrate_fails_fast_without_yes_when_stdin_is_not_a_tty(tmp_path, monkeypatch) -> None:
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.close()

    monkeypatch.setattr(sys, "stdin", io.StringIO())  # a pipe: isatty() is False

    def _explode(prompt: str = "") -> str:
        raise AssertionError(f"migrate prompted in a non-interactive session: {prompt!r}")

    monkeypatch.setattr("builtins.input", _explode)

    with pytest.raises(ValueError, match="needs an interactive terminal"):
        cli._run_migrate(_migrate_args(graph_path))

    # Refused before touching anything.
    store = PropertyGraphStore(graph_path)
    assert _active_facts(store, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    store.close()


@pytest.mark.asyncio
async def test_migrate_with_yes_proceeds_without_prompting(tmp_path, monkeypatch, capsys) -> None:
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.close()

    monkeypatch.setattr(sys, "stdin", io.StringIO())

    def _explode(prompt: str = "") -> str:
        raise AssertionError(f"--yes still prompted: {prompt!r}")

    monkeypatch.setattr("builtins.input", _explode)
    cli._run_migrate(_migrate_args(graph_path, yes=True))

    # Preview object then result object, both as JSON.
    preview_json, result_json = _two_json_objects(capsys.readouterr().out)
    assert preview_json["active_relationship_count"] == 2
    assert result_json["purged_source"] is True
    assert result_json["relationships_migrated"] == 2

    store = PropertyGraphStore(graph_path)
    assert _active_facts(store, "new-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(store, "old-tenant") == []
    store.close()


@pytest.mark.asyncio
async def test_migrate_previews_then_honours_an_interactive_decline(tmp_path, monkeypatch, capsys) -> None:
    """Declining at the prompt must leave the source and destination untouched."""
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.close()

    monkeypatch.setattr(sys, "stdin", _Tty())
    asked: list[str] = []

    def _decline(prompt: str = "") -> str:
        asked.append(prompt)
        return "n"

    monkeypatch.setattr("builtins.input", _decline)
    cli._run_migrate(_migrate_args(graph_path))

    output = capsys.readouterr().out
    assert len(asked) == 1
    assert "'old-tenant'" in asked[0] and "'new-tenant'" in asked[0]
    assert "Aborted; nothing was changed." in output

    store = PropertyGraphStore(graph_path)
    assert _active_facts(store, "old-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(store, "new-tenant") == []
    store.close()


@pytest.mark.asyncio
async def test_migrate_previews_then_migrates_on_an_interactive_confirm(tmp_path, monkeypatch, capsys) -> None:
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.close()

    monkeypatch.setattr(sys, "stdin", _Tty())
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    cli._run_migrate(_migrate_args(graph_path))

    preview_json, result_json = _two_json_objects(capsys.readouterr().out)
    assert preview_json["source_tenant_id"] == "old-tenant"
    assert result_json["purged_source"] is True

    store = PropertyGraphStore(graph_path)
    assert _active_facts(store, "new-tenant") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    store.close()


@pytest.mark.asyncio
async def test_migrate_refuses_a_crypto_shred_source_before_any_prompt(tmp_path, monkeypatch, capsys) -> None:
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(crypto_shred=True), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-tenant")
    client.graph.close()

    monkeypatch.setattr(sys, "stdin", _Tty())

    def _explode(prompt: str = "") -> str:
        raise AssertionError(f"prompted despite a blocked preview: {prompt!r}")

    monkeypatch.setattr("builtins.input", _explode)
    with pytest.raises(ValueError, match="CRYPTO_SHRED"):
        cli._run_migrate(_migrate_args(graph_path, yes=True))

    assert "blocked_reason" in capsys.readouterr().out


def _project_config(project_id: str):
    """A minimal valid `.memotron.yaml` config claiming *project_id*."""
    from memotron.adoption import MemotronProjectConfig

    return MemotronProjectConfig.model_validate(
        {
            "project": {"id": project_id, "name": project_id.replace("-", " ").title()},
            "project_memory": {
                "project_goal": "Ship the thing.",
                "memory_goal": "Remember decisions and requirements.",
                "keep": ("decisions",),
            },
        }
    )


def _two_json_objects(output: str) -> tuple[dict, dict]:
    """Split the two pretty-printed JSON objects `migrate` prints."""
    decoder = json.JSONDecoder()
    remaining = output.strip()
    objects: list[dict] = []
    while remaining:
        value, index = decoder.raw_decode(remaining)
        objects.append(value)
        remaining = remaining[index:].strip()
    assert len(objects) == 2, output
    return objects[0], objects[1]


@pytest.mark.asyncio
async def test_offer_memory_migration_moves_the_previous_project_identity(tmp_path, monkeypatch, capsys) -> None:
    """`init`'s wizard branch: re-running init under a NEW project id offers to
    move the old identity's memory, and moves it when confirmed."""
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-project")
    client.graph.close()

    existing = _project_config("old-project")
    monkeypatch.setattr("builtins.input", lambda prompt="": "y")
    cli._offer_memory_migration(
        project_root=tmp_path,
        existing_config=existing,
        new_project_id="new-project",
        graph_path=graph_path,
    )

    output = capsys.readouterr().out
    assert "Found existing memory for the previous identity of this project" in output
    assert "Migrated 2 facts" in output

    store = PropertyGraphStore(graph_path)
    assert _active_facts(store, "new-project") == [
        "Priya prefers dark mode",
        "Priya requires SSO login",
    ]
    assert _active_facts(store, "old-project") == []
    store.close()


@pytest.mark.asyncio
async def test_offer_memory_migration_reports_a_blocked_tenant_and_never_moves_it(
    tmp_path, monkeypatch, capsys
) -> None:
    """A CRYPTO_SHRED tenant is surfaced, never silently skipped and never moved."""
    graph_path = tmp_path / "graph.sqlite"
    client = Memotron(config=_config(crypto_shred=True), graph_path=graph_path)
    await _seed_tenant(client, tenant_id="old-project")
    client.graph.close()

    existing = _project_config("old-project")

    def _explode(prompt: str = "") -> str:
        raise AssertionError(f"offered to move a blocked tenant: {prompt!r}")

    monkeypatch.setattr("builtins.input", _explode)
    cli._offer_memory_migration(
        project_root=tmp_path,
        existing_config=existing,
        new_project_id="new-project",
        graph_path=graph_path,
    )

    assert "cannot be imported automatically" in capsys.readouterr().out
    store = PropertyGraphStore(graph_path)
    # Facts stay put (content is sealed under crypto-shred, so count them).
    assert len(_active_facts(store, "old-project")) == 2
    assert _active_facts(store, "new-project") == []
    store.close()
