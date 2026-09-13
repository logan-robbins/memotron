"""The StorageBackend contract: interface conformance, engine selection, and
the no-SQLite-outside-the-backend-module acceptance guard."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from memotron.config import DreamConfig
from memotron.models import MemoryScope, ScopeKind
from memotron.storage import (
    EngineSettings,
    MemoryGraphStorage,
    OperationalStorage,
    SplitStorageBackend,
    SQLiteStorageBackend,
    StorageBackend,
    StorageSettings,
    create_storage_backend,
    open_storage,
)
from memotron.storage.base import normalize_key

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "memotron"


# ---------------------------------------------------------------------------
# Acceptance: no caller outside the backend module references SQLite.
# ---------------------------------------------------------------------------


#: The one file outside ``storage/`` allowed to speak SQL, and why.
#:
#: ``caveman/ledger.py`` is the #251 append-only claim ledger, and its use of
#: stdlib ``sqlite3`` is that design's explicit decision rather than an
#: oversight. From its Decisions table: folding the claim ledger behind
#: ``StorageBackend`` "puts `caveman` behind `storage/base.py`'s roughly
#: 60-method ABC and its Postgres parity gate -- the exact coupling this issue
#: exists to avoid while there is no graph database."
#:
#: Three properties keep this from being a hole. It is ONE exact path, so a
#: second file reaching for sqlite3 still fails, ``caveman/`` included. The
#: ledger is reached only through ``caveman.seams.LedgerStore``, so the SQL does
#: not leak past that Protocol. And the convergence is a named follow-up: when
#: the property-graph implementation lands, ``LedgerStore`` gets a
#: ``StorageBackend``-backed implementation and this line goes away.
SQL_EXEMPT = frozenset({"caveman/ledger.py"})


def test_the_sql_exemption_names_files_that_exist() -> None:
    """An exemption for a renamed file is an exemption for nothing.

    Without this, moving ``caveman/ledger.py`` would silently take the guard off
    it and leave a stale entry that reads as if it were still doing work.
    """
    for relative in sorted(SQL_EXEMPT):
        assert (SRC_ROOT / relative).is_file(), f"SQL_EXEMPT names a file that does not exist: {relative}"


def test_no_sqlite_references_outside_backend_module() -> None:
    import re

    # Engine references, private-attribute reach-through, and raw SQL strings
    # are all ways for a caller to bypass the contract; reject each of them.
    forbidden = (
        ("sqlite3", "references SQLite directly"),
        ("import sqlite", "references SQLite directly"),
        ("._connection", "reaches through the backend's private connection"),
        ("graph._", "accesses a private attribute of the backend"),
    )
    raw_sql = re.compile(r'["\'](?:SELECT|INSERT INTO|DELETE FROM|UPDATE)\s')
    offenders: list[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        if SRC_ROOT / "storage" in path.parents:
            continue
        if path.relative_to(SRC_ROOT).as_posix() in SQL_EXEMPT:
            continue
        text = path.read_text()
        for needle, why in forbidden:
            if needle in text:
                offenders.append(f"{path.relative_to(SRC_ROOT)} ({why})")
        if raw_sql.search(text):
            offenders.append(f"{path.relative_to(SRC_ROOT)} (contains a raw SQL statement string)")
    assert offenders == [], f"persistence must stay behind memotron.storage; offenders: {offenders}"


# ---------------------------------------------------------------------------
# Interface conformance
# ---------------------------------------------------------------------------


def test_sqlite_backend_implements_the_full_contract() -> None:
    backend = SQLiteStorageBackend(":memory:")
    try:
        assert isinstance(backend, StorageBackend)
        assert isinstance(backend, MemoryGraphStorage)
        assert isinstance(backend, OperationalStorage)
        # The contract is explicit: nothing may remain abstract.
        assert not getattr(SQLiteStorageBackend, "__abstractmethods__", frozenset())
    finally:
        backend.close()


def test_interface_admits_a_graph_engine_without_relational_vocabulary() -> None:
    # The Memory Graph surface a graph engine must implement never mentions
    # rows, cursors, SQL, or connections in its signatures.
    import inspect

    for name in MemoryGraphStorage.__abstractmethods__:
        signature = str(inspect.signature(getattr(MemoryGraphStorage, name)))
        for relational_term in ("sqlite", "sql", "row", "cursor", "connection"):
            assert relational_term not in signature.lower(), (
                f"MemoryGraphStorage.{name} leaks relational vocabulary: {signature}"
            )


def test_context_visible_read_is_index_backed() -> None:
    backend = SQLiteStorageBackend(":memory:")
    try:
        scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="probe")
        plan = backend.explain_context_visible_read(scope=scope)
        assert any("relationships_ctx_idx" in line for line in plan), plan
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Backend selection by configuration
# ---------------------------------------------------------------------------


def test_default_settings_build_one_sqlite_backend(tmp_path: Path) -> None:
    backend = open_storage(tmp_path / "graph.sqlite")
    try:
        assert isinstance(backend, SQLiteStorageBackend)
    finally:
        backend.close()


def test_unknown_engine_fails_with_registered_engine_list() -> None:
    with pytest.raises(ValueError, match="unknown storage engine 'neo4j'"):
        create_storage_backend(StorageSettings(backend=EngineSettings(engine="neo4j")))


def test_settings_reject_a_half_configured_split() -> None:
    with pytest.raises(ValueError, match="must configure both"):
        StorageSettings(operational_store=EngineSettings(path=":memory:"))


def test_dream_config_carries_storage_settings() -> None:
    from memotron.config import default_config

    config = default_config().model_copy(
        update={"storage": StorageSettings(backend=EngineSettings(engine="sqlite", path=":memory:"))}
    )
    assert isinstance(config, DreamConfig)
    assert config.storage is not None and not config.storage.is_split


def test_client_builds_backend_from_config_settings(tmp_path: Path) -> None:
    from memotron import Memotron
    from memotron.config import default_config

    config = default_config().model_copy(
        update={
            "storage": StorageSettings(
                backend=EngineSettings(engine="sqlite", path=str(tmp_path / "configured.sqlite"))
            )
        }
    )
    client = Memotron(config=config)
    try:
        assert isinstance(client.graph, StorageBackend)
        assert (tmp_path / "configured.sqlite").exists()
    finally:
        client.graph.close()


# ---------------------------------------------------------------------------
# The two stores on different engines at once
# ---------------------------------------------------------------------------


@pytest.fixture
def split_backend(tmp_path: Path) -> StorageBackend:
    backend = create_storage_backend(
        StorageSettings(
            operational_store=EngineSettings(engine="sqlite", path=str(tmp_path / "operational.sqlite")),
            memory_graph=EngineSettings(engine="sqlite", path=str(tmp_path / "memory-graph.sqlite")),
        )
    )
    yield backend
    backend.close()


def test_split_backend_satisfies_the_contract(split_backend: StorageBackend) -> None:
    assert isinstance(split_backend, SplitStorageBackend)
    assert isinstance(split_backend, StorageBackend)


def test_split_backend_routes_each_surface_to_its_store(split_backend: StorageBackend, tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="split-agent")
    source, _ = split_backend.upsert_node(labels=["Entity"], key="split source", properties={"name": "split source"})
    target, _ = split_backend.upsert_node(labels=["Entity"], key="split target", properties={"name": "split target"})
    relationship = split_backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={"scope_key": scope.key, "status": "active", "fact": "split fact"},
    )
    split_backend.set_job_last_run("split-job", datetime.now(UTC))

    # Graph rows live only in the memory-graph store; job state only in the
    # operational store.
    graph_only = open_storage(tmp_path / "memory-graph.sqlite")
    operational_only = open_storage(tmp_path / "operational.sqlite")
    try:
        assert graph_only.get_relationship(relationship.uuid).uuid == relationship.uuid
        assert graph_only.get_job_last_run("split-job") is None
        assert operational_only.relationships() == []
        assert operational_only.get_job_last_run("split-job") is not None
    finally:
        graph_only.close()
        operational_only.close()


def test_split_backend_anchors_cross_plane_writes_in_the_operational_store(
    split_backend: StorageBackend,
) -> None:
    from memotron.models import UseEvent, UseEventKind

    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="split-agent")
    source, _ = split_backend.upsert_node(labels=["Entity"], key="anchor source", properties={"name": "anchor source"})
    target, _ = split_backend.upsert_node(labels=["Entity"], key="anchor target", properties={"name": "anchor target"})
    relationship = split_backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={"scope_key": scope.key, "status": "active", "fact": "anchored"},
    )
    event = UseEvent(
        relationship_uuid=relationship.uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="task-1",
        idempotency_key="use-1",
        used_at=datetime.now(UTC),
    )
    stored = split_backend.record_use_event(event)
    # Idempotent replay per the contract.
    assert split_backend.record_use_event(event).use_id == stored.use_id
    assert [item.use_id for item in split_backend.use_events(scope_key=scope.key)] == [stored.use_id]


def test_split_backend_purges_each_store_through_its_own_plane(split_backend: StorageBackend, tmp_path: Path) -> None:
    tenant_id = "purge-tenant"
    split_backend.register_tenant_agent(tenant_id=tenant_id, agent_id="purge-agent", name="Purge Agent")
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="purge-agent")
    source, _ = split_backend.upsert_node(
        labels=["Entity"],
        key="purge source",
        properties={"name": "purge source", "scope_key": scope.key},
    )
    target, _ = split_backend.upsert_node(
        labels=["Entity"],
        key="purge target",
        properties={"name": "purge target", "scope_key": scope.key},
    )
    relationship = split_backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={"scope_key": scope.key, "status": "active", "fact": "purge me"},
    )

    counts = split_backend.purge_tenant_state(tenant_id)
    assert counts["relationships_deleted"] == 1
    assert counts["nodes_deleted"] == 2
    assert counts["tenant_agents_deleted"] == 1

    # The graph deletions landed in the memory-graph store, driven from the
    # operational anchor through the idempotent graph-plane primitives.
    graph_only = open_storage(tmp_path / "memory-graph.sqlite")
    try:
        assert graph_only.nodes() == []
        assert graph_only.relationships() == []
    finally:
        graph_only.close()
    with pytest.raises(ValueError, match="does not exist"):
        split_backend.get_relationship(relationship.uuid)
    # Idempotent: re-running the purge is safe and deletes nothing further.
    assert split_backend.purge_tenant_state(tenant_id)["nodes_deleted"] == 0


def test_split_backend_restores_prune_ghosts_across_planes(
    split_backend: StorageBackend,
) -> None:
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="ghost-agent")
    source, _ = split_backend.upsert_node(labels=["Entity"], key="ghost source", properties={"name": "ghost source"})
    target, _ = split_backend.upsert_node(labels=["Entity"], key="ghost target", properties={"name": "ghost target"})
    relationship = split_backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={"scope_key": scope.key, "status": "pruned", "fact": "ghosted"},
    )
    now = datetime.now(UTC)
    ghost = split_backend.create_prune_ghost(
        relationship_uuid=relationship.uuid,
        scope_key=scope.key,
        prune_receipt_uuid="receipt-1",
        reason="test prune",
        pruned_at=now,
    )
    assert ghost.restorable
    restored = split_backend.restore_prune_ghost(relationship.uuid, restored_at=now)
    assert restored.restored_at is not None
    # The reactivation (an idempotent graph write) landed in the graph plane.
    reactivated = split_backend.get_relationship(relationship.uuid)
    assert reactivated.properties["status"] == "active"


def test_mis_wired_split_composite_fails_at_construction(tmp_path: Path) -> None:
    operational = SQLiteStorageBackend(tmp_path / "operational.sqlite")
    other_graph = SQLiteStorageBackend(tmp_path / "graph.sqlite")
    try:
        # ``operational`` self-serves its graph plane, so pairing it with a
        # different graph plane would send cross-plane writes to the wrong
        # store — the composite must refuse.
        with pytest.raises(ValueError, match="was not constructed with"):
            SplitStorageBackend(operational=operational, memory_graph=other_graph)
    finally:
        operational.close()
        other_graph.close()


def test_contract_covers_the_entire_sqlite_public_surface() -> None:
    # A public method on the SQLite backend that is missing from the contract
    # would work single-engine but silently fail to route in a split
    # deployment; the contract must therefore cover the whole surface.
    #
    # FULL POSTGRES PARITY REACHED (2026-08-25): the WS-16..26 features (entity
    # resolution, predicate canonicalization, the quarantine store, object-level
    # promotion endorsements, dream work-claiming, the incremental state-hash
    # tracker, and the WS-26 derivation-DAG epoch layer) are now on the
    # StorageBackend contract and implemented on BOTH engines
    # (storage/postgres/_epochs.py + _graph.py + _operational.py + _governance.py),
    # proven by the backend parity suite. The SQLite-only allowlist is therefore
    # empty: every public SQLite method is on the contract, and any NEW public
    # method that slips the contract fails this invariant loudly.
    sqlite_only_pending_postgres_parity: set[str] = set()
    import inspect

    contract = set(StorageBackend.__abstractmethods__)
    implementation = {
        name for name, member in inspect.getmembers(SQLiteStorageBackend, callable) if not name.startswith("_")
    }
    uncovered = implementation - contract - sqlite_only_pending_postgres_parity
    assert uncovered == set(), (
        "public SQLite backend methods missing from the StorageBackend contract "
        f"(and not on the SQLite-only allowlist): {sorted(uncovered)}"
    )
    assert contract - implementation == set()


def test_split_backend_routes_retrieval_reads_to_the_graph_plane(
    split_backend: StorageBackend,
) -> None:
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="retrieval-agent")
    source, _ = split_backend.upsert_node(labels=["Entity"], key="vector source", properties={"name": "vector source"})
    target, _ = split_backend.upsert_node(labels=["Entity"], key="vector target", properties={"name": "vector target"})
    relationship = split_backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={
            "scope_key": scope.key,
            "status": "active",
            "fact": "vector fact",
            "embedding": [1.0, 0.0],
        },
    )
    assert [item.uuid for item in split_backend.relationships_for_scope(scope.key)] == [relationship.uuid]
    scored = split_backend.similar_relationships(scope=scope, embedding=[1.0, 0.0], threshold=0.5)
    assert [(item[0].uuid, round(item[1], 6)) for item in scored] == [(relationship.uuid, 1.0)]
    # The bracket anchors in the operational store and is routed there.
    with split_backend.transaction():
        split_backend.set_job_last_run("routed-bracket", datetime.now(UTC))
    assert split_backend.get_job_last_run("routed-bracket") is not None


async def test_client_receipt_surfaces_work_on_a_split_backend(
    split_backend: StorageBackend,
) -> None:
    # Regression for the review finding: memory_receipts(scope=...) and
    # run_checkpoints() used to reach through graph._connection with raw SQL,
    # which crashed on any backend without one.  They must work through the
    # contract on the split composite this PR ships.
    from memotron import Memotron

    client = Memotron(storage=split_backend)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="receipt-agent")
    assert await client.memory_receipts(scope=scope) == []
    assert await client.run_checkpoints() == []
    assert await client.run_checkpoints(scope=scope, limit=5) == []
    with pytest.raises(ValueError, match="limit must be greater than zero"):
        await client.run_checkpoints(limit=0)


def test_purge_is_one_transaction_on_the_sqlite_substrate() -> None:
    # Regression for the review finding: routing graph deletes through
    # delete_relationships/delete_nodes made one logical purge three commits.
    # A failure mid-purge must leave the store as if the purge never ran.
    backend = SQLiteStorageBackend(":memory:")
    try:
        backend.register_tenant_agent(tenant_id="rtbf-tenant", agent_id="rtbf-agent", name="RTBF Agent")
        scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="rtbf-agent")
        source, _ = backend.upsert_node(
            labels=["Entity"],
            key="rtbf source",
            properties={"name": "rtbf source", "scope_key": scope.key},
        )
        target, _ = backend.upsert_node(
            labels=["Entity"],
            key="rtbf target",
            properties={"name": "rtbf target", "scope_key": scope.key},
        )
        relationship = backend.add_relationship(
            source_uuid=source.uuid,
            target_uuid=target.uuid,
            relationship_type="REQUIRES",
            properties={"scope_key": scope.key, "status": "active", "fact": "rtbf"},
        )

        # Fail after relationships are deleted but before nodes are: without
        # the purge-wide bracket this stranded a half-purged store.
        def exploding_delete_nodes(uuids: object) -> int:
            raise RuntimeError("mid-purge crash")

        backend.delete_nodes = exploding_delete_nodes  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="mid-purge crash"):
            backend.purge_tenant_state("rtbf-tenant")

        # Everything rolled back together: one transaction, zero partial state.
        assert backend.get_relationship(relationship.uuid).uuid == relationship.uuid
        assert len(backend.nodes()) == 2
        assert backend.tenant_agents("rtbf-tenant") != ()
    finally:
        backend.close()


def test_normalize_key_is_engine_independent() -> None:
    assert normalize_key("  Corporate   TRAVEL ") == "corporate travel"
