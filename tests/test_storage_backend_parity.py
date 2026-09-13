"""Backend parity: one suite of behavioural assertions, run against both engines.

This is the acceptance suite for the persistence milestone — "the backend parity
test suite passes against Postgres".  Every assertion here is written against
:class:`~memotron.storage.base.StorageBackend`, the *contract*, and is
executed twice: once on :class:`SQLiteStorageBackend` (the hermetic substrate)
and once on ``PostgresStorageBackend`` (the Operational Store).  There is
deliberately one copy of each assertion; the engine is a fixture parameter, so
a divergence surfaces as the same test failing on exactly one engine.

What the suite proves
---------------------
* The observable behaviour of the two engines is interchangeable behind the
  interface: return values, filters, ordering, idempotency, and the exact
  exception type *and* message on the failure paths.
* :meth:`MemoryGraphStorage.graph_state_hash` produces the **same string** on
  both engines for the same graph.  Postgres maintains that hash incrementally
  where SQLite recomputes it, so equality across engines — not merely
  self-consistency on each — is what proves the optimisation preserved the
  value.  Receipt chains and replay proofs are anchored on it.
* The receipt ledger's hash chain and Merkle root are byte-identical across
  engines for the same input sequence.

Running it
----------
The SQLite parameters need nothing.  The Postgres parameters read their DSN
from ``MEMOTRON_TEST_POSTGRES_DSN``; with that variable unset they skip, so
the hermetic offline suite (DW-002) still passes with no database and no
network.  Example::

    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=5433 user=dw dbname=dw_parity"
    pytest tests/test_storage_backend_parity.py

Rules this file follows
-----------------------
* No ``sqlite3`` import and no SQL anywhere a store is *exercised* — every
  operation under test goes through the interface, which is the whole point.
  The single exception is :func:`_reset_postgres_schema`, two DDL statements
  that give each Postgres test a clean schema so the suite is order-independent
  (SQLite gets the same guarantee for free from ``:memory:``).
* Floats are compared with :func:`pytest.approx`; no assertion depends on a
  wall-clock value.
* Where ordering is asserted, the ordering key is supplied explicitly by the
  test and a comment records why that order is guaranteed by the contract.
"""

from __future__ import annotations

import json
import math
import os
import uuid as uuid_module
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memotron.client import Memotron
from memotron.config import default_config
from memotron.crypto import (
    SHREDDED_CONTENT_PLACEHOLDER,
    ContentKeyUnavailableError,
    seal_content,
)
from memotron.models import (
    ArtifactClass,
    ArtifactDirective,
    DirectiveStance,
    DreamDecisionRecord,
    DreamJobKind,
    DreamJobRunRecord,
    Episode,
    EpisodeType,
    MemoryScope,
    MemoryType,
    OutcomeEvent,
    OutcomeVerdict,
    PersistentArtifact,
    QuarantineStatus,
    RelationshipStatus,
    ScopeKind,
    UseEvent,
    UseEventKind,
)
from memotron.storage import SQLiteStorageBackend, StorageBackend
from memotron.storage.receipts import (
    ReceiptDecisionType,
    config_effective_policy_digest,
    payload_digest,
)

POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"
POSTGRES_SKIP_REASON = f"set {POSTGRES_DSN_ENV} to run Postgres parity"

# A fixed instant every test derives its timestamps from.  Nothing in this file
# asserts on a wall-clock value; every ordering key is one of these.
EPOCH = datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC)

# The utility half-life per memory type, in days. This is a SPECIFICATION, deliberately
# written out here rather than imported from either backend -- a parity test that reads one
# engine's table cannot detect that engine being the wrong one.
#
# It is a third copy of the table today, and that is the point: `postgres/_operational.py`
# keyed its 365-day entry on `"identity"`, the name `anchor` carried before it was renamed
# (`models/_enums.py` records the rename and that the old name absorbed 84% of a corpus).
# `"identity"` matches no MemoryType, so anchors silently took the 90-day default on Postgres.
# When the two tables are extracted to one place, this stays as the independent check on it.
#
# `rollup` is in neither engine's table and correctly falls through to the 90-day default.
EXPECTED_HALF_LIFE_DAYS: dict[str, float] = {
    "state": 7.0,
    "directive": 30.0,
    "preference": 90.0,
    "requirement": 180.0,
    "anchor": 365.0,
    "decision": 365.0,
    "incident": 180.0,
    "rollup": 90.0,
}


# ---------------------------------------------------------------------------
# Engine fixtures
# ---------------------------------------------------------------------------


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(POSTGRES_SKIP_REASON)
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    """Give the next Postgres backend an empty database.

    The ONLY engine-specific statements in this file, and they touch no table
    the suite asserts on: they drop and recreate the schema so each test starts
    from nothing and the suite is order-independent, exactly as ``:memory:``
    does for SQLite.  The backend re-runs its migrations on construction.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


def _open_backend(engine: str) -> StorageBackend:
    if engine == "sqlite":
        return SQLiteStorageBackend(":memory:")
    dsn = _postgres_dsn_or_skip()
    _reset_postgres_schema(dsn)
    # Imported lazily so a hermetic run never needs psycopg installed.
    from memotron.storage.postgres import PostgresStorageBackend

    return PostgresStorageBackend(dsn, min_size=1, max_size=4)


def _engine_name(backend: StorageBackend) -> str:
    """The engine label used in every parity failure message."""
    return "sqlite" if isinstance(backend, SQLiteStorageBackend) else "postgres"


def _diverged(backend: StorageBackend, what: str, observed: Any, expected: Any) -> str:
    return (
        f"engine {_engine_name(backend)!r} diverged on {what}: "
        f"returned {observed!r}, the contract requires {expected!r}"
    )


# The engine is a fixture PARAMETER, so the postgres marker rides on the param
# rather than the module: `-m "not postgres"` must still run every [sqlite] twin
# of these assertions. Marking the file instead would silently halve the suite.
@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def backend(request: pytest.FixtureRequest, tmp_path: Any) -> Iterator[StorageBackend]:
    """One live backend per engine; the same assertions run against both."""
    store = _open_backend(request.param)
    try:
        yield store
    finally:
        store.close()


@pytest.fixture
def both_backends(tmp_path: Any) -> Iterator[tuple[StorageBackend, StorageBackend]]:
    """SQLite and Postgres side by side, for the cross-engine identity tests."""
    _postgres_dsn_or_skip()
    sqlite_backend = _open_backend("sqlite")
    postgres_backend = _open_backend("postgres")
    try:
        yield sqlite_backend, postgres_backend
    finally:
        sqlite_backend.close()
        postgres_backend.close()


class _SequentialUUIDs:
    """A deterministic stand-in for ``uuid4``.

    The state hash and the receipt chain both bind generated identifiers, so
    two engines can only be compared byte-for-byte if they mint the same ids.
    A fresh instance is installed before each engine's build, so both walk the
    identical sequence and storage is the only variable.
    """

    def __init__(self) -> None:
        self._counter = 0

    def __call__(self) -> uuid_module.UUID:
        self._counter += 1
        return uuid_module.UUID(int=self._counter)


# ---------------------------------------------------------------------------
# Builders — identical inputs for both engines
# ---------------------------------------------------------------------------

AGENT_SCOPE = MemoryScope(kind=ScopeKind.AGENT, scope_id="parity-agent")
OTHER_SCOPE = MemoryScope(kind=ScopeKind.AGENT, scope_id="other-agent")


def _node(backend: StorageBackend, key: str, **properties: Any) -> Any:
    node, _created = backend.upsert_node(labels=["Entity"], key=key, properties={"name": key, **properties})
    return node


def _relationship(
    backend: StorageBackend,
    *,
    source: Any,
    target: Any,
    relationship_type: str = "REQUIRES",
    scope: MemoryScope = AGENT_SCOPE,
    **properties: Any,
) -> Any:
    return backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type=relationship_type,
        properties={
            "scope_key": scope.key,
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "status": RelationshipStatus.ACTIVE.value,
            "fact": "parity fact",
            "predicate": "requires",
            "memory_type": "requirement",
            **properties,
        },
    )


def _scoped_relationship(
    backend: StorageBackend, *, scope: MemoryScope = AGENT_SCOPE, suffix: str = "", **properties: Any
) -> Any:
    source = _node(backend, f"subject {scope.scope_id}{suffix}")
    target = _node(backend, f"object {scope.scope_id}{suffix}")
    return _relationship(backend, source=source, target=target, scope=scope, **properties)


def _episode(scope: MemoryScope, name: str, *, created_at: datetime, **metadata: Any) -> Episode:
    return Episode(
        name=name,
        body=f"body of {name}",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=created_at,
        created_at=created_at,
        metadata=dict(metadata),
    )


def _artifact(
    scope: MemoryScope,
    *,
    artifact_id: str = "parity-skill",
    instruction: str = "Check the SLA before escalating.",
    updated_at: datetime,
) -> PersistentArtifact:
    return PersistentArtifact(
        artifact_id=artifact_id,
        artifact_class=ArtifactClass.SKILL,
        scope=scope,
        author="operator@example.test",
        location="skills/parity/SKILL.md",
        created_at=EPOCH,
        updated_at=updated_at,
        directives=[
            ArtifactDirective(
                subject="escalation",
                instruction=instruction,
                stance=DirectiveStance.REQUIRE,
            )
        ],
    )


def _use_event(
    relationship_uuid: str,
    *,
    kind: UseEventKind,
    used_at: datetime,
    task_run_id: str = "task-1",
    idempotency_key: str,
    scope: MemoryScope = AGENT_SCOPE,
) -> UseEvent:
    # Impression kinds (retrieved / injected) carry mandatory propensity fields;
    # fixed values keep the event byte-identical for both engines.
    propensity: dict[str, Any] = {}
    if kind in {UseEventKind.RETRIEVED, UseEventKind.INJECTED}:
        propensity = {
            "rank": 0,
            "retrieval_score": 0.75,
            "candidate_set_size": 5,
            "context_budget_competition": 3,
            "retrieval_policy_digest": "f" * 64,
        }
    return UseEvent(
        relationship_uuid=relationship_uuid,
        scope=scope,
        kind=kind,
        used_at=used_at,
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
        **propensity,
    )


def _outcome_event(
    use_id: str,
    *,
    verdict: OutcomeVerdict,
    judged_at: datetime,
    task_run_id: str = "task-1",
    idempotency_key: str,
    scope: MemoryScope = AGENT_SCOPE,
) -> OutcomeEvent:
    return OutcomeEvent(
        use_id=use_id,
        scope=scope,
        judged_at=judged_at,
        verdict=verdict,
        judge_identity="parity-judge",
        judge_version="1",
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
    )


def _certified_contract(backend: StorageBackend, *, name: str, passed: bool) -> str:
    payload = {"policy": name, "version": 1}
    digest = payload_digest(payload)
    backend.stage_policy_contract(
        contract_digest=digest,
        payload=payload,
        certification={"suite": "parity", "passed": passed},
        certification_passed=passed,
        staged_at=EPOCH,
    )
    return digest


# ===========================================================================
# 1. Graph plane
# ===========================================================================


def test_upsert_node_is_idempotent_and_reports_creation(backend: StorageBackend) -> None:
    node, created = backend.upsert_node(
        labels=["Entity", "Person"], key="  Corporate   TRAVEL ", properties={"name": "travel"}
    )
    assert created is True, _diverged(backend, "upsert_node created flag", created, True)
    # The key is normalised engine-independently, so a differently spelled key
    # addresses the same node.
    again, created_again = backend.upsert_node(labels=["Entity"], key="corporate travel", properties={"note": "second"})
    assert created_again is False, _diverged(backend, "upsert_node created flag on repeat", created_again, False)
    assert again.uuid == node.uuid, _diverged(
        backend, "upsert_node convergence on the normalised key", again.uuid, node.uuid
    )
    assert again.properties["graph_key"] == "corporate travel"
    assert again.properties["name"] == "travel"
    assert again.properties["note"] == "second"
    assert backend.get_node(node.uuid).properties == again.properties


def test_upsert_node_never_overwrites_a_sealed_name(backend: StorageBackend) -> None:
    # Sealing is nonce-random and the stored name feeds graph_state_hash, so a
    # re-seal of the same plaintext must not rewrite the stored name.
    key = backend.get_or_create_governance_key(AGENT_SCOPE.key)
    first_seal = seal_content("Alice", key)
    second_seal = seal_content("Alice", key)
    assert first_seal != second_seal, "the seal must be nonce-random for this test to mean anything"

    _node, _ = backend.upsert_node(labels=["Entity"], key="alice", properties={"name": first_seal})
    merged, created = backend.upsert_node(
        labels=["Entity"], key="alice", properties={"name": second_seal, "role": "lead"}
    )
    assert created is False
    assert merged.properties["name"] == first_seal, _diverged(
        backend, "sealed-name non-overwrite", merged.properties["name"], first_seal
    )
    assert merged.properties["role"] == "lead"
    # A plaintext name, by contrast, is an ordinary property merge.
    plain, _ = backend.upsert_node(labels=["Entity"], key="bob", properties={"name": "Bob"})
    renamed, _ = backend.upsert_node(labels=["Entity"], key="bob", properties={"name": "Bobby"})
    assert renamed.properties["name"] == "Bobby"
    assert renamed.uuid == plain.uuid


def test_upsert_node_merges_the_validity_window(backend: StorageBackend) -> None:
    early = EPOCH
    late = EPOCH + timedelta(days=10)
    node, _ = backend.upsert_node(labels=["Entity"], key="window", properties={}, valid_from=late, valid_to=late)
    # valid_from takes the minimum, valid_to the maximum.
    merged, _ = backend.upsert_node(
        labels=["Entity"], key="window", properties={}, valid_from=early, valid_to=late + timedelta(days=1)
    )
    assert merged.valid_from == early, _diverged(
        backend, "upsert_node valid_from merge (min)", merged.valid_from, early
    )
    assert merged.valid_to == late + timedelta(days=1), _diverged(
        backend, "upsert_node valid_to merge (max)", merged.valid_to, late + timedelta(days=1)
    )
    # An upsert that omits valid_to clears the window's upper bound.
    cleared, _ = backend.upsert_node(labels=["Entity"], key="window", properties={})
    assert cleared.valid_to is None, _diverged(backend, "upsert_node valid_to clearing", cleared.valid_to, None)
    assert backend.get_node(node.uuid).valid_from == early


def test_relationship_add_get_update_and_mark(backend: StorageBackend) -> None:
    source = _node(backend, "subject")
    target = _node(backend, "object")
    relationship = _relationship(backend, source=source, target=target, confidence=0.5)

    fetched = backend.get_relationship(relationship.uuid)
    assert fetched.uuid == relationship.uuid
    assert fetched.source_uuid == source.uuid
    assert fetched.target_uuid == target.uuid
    assert fetched.type == "REQUIRES"
    assert fetched.properties["confidence"] == pytest.approx(0.5)

    # update_relationship merges properties (last-write-wins by uuid) and can
    # move or clear the validity window.
    updated = backend.update_relationship(
        relationship.uuid,
        properties={"confidence": 0.9, "observed_count": 3},
        valid_from=EPOCH,
        valid_to=EPOCH + timedelta(days=1),
    )
    assert updated.properties["confidence"] == pytest.approx(0.9)
    assert updated.properties["observed_count"] == 3
    assert updated.properties["fact"] == "parity fact", "the merge must not drop untouched keys"
    assert updated.valid_from == EPOCH
    assert updated.valid_to == EPOCH + timedelta(days=1)

    cleared = backend.update_relationship(relationship.uuid, clear_valid_to=True)
    assert cleared.valid_to is None, _diverged(backend, "update_relationship clear_valid_to", cleared.valid_to, None)

    marked = backend.mark_relationship(
        relationship.uuid,
        status=RelationshipStatus.SUPERSEDED,
        valid_to=EPOCH + timedelta(days=2),
        properties={"superseded_by_relationship_uuid": "successor"},
    )
    assert marked.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert marked.properties["superseded_by_relationship_uuid"] == "successor"
    assert marked.valid_to == EPOCH + timedelta(days=2)
    # Read-your-writes: the mutation is visible to the next read on this instance.
    assert backend.get_relationship(relationship.uuid).properties["status"] == (RelationshipStatus.SUPERSEDED.value)


def test_active_relationships_filter_by_scope_and_type(backend: StorageBackend) -> None:
    active_here = _scoped_relationship(backend, suffix=" a")
    typed_here = _scoped_relationship(backend, suffix=" b", relationship_type="PREFERS")
    superseded = _scoped_relationship(backend, suffix=" c")
    backend.mark_relationship(superseded.uuid, status=RelationshipStatus.SUPERSEDED)
    elsewhere = _scoped_relationship(backend, scope=OTHER_SCOPE, suffix=" d")

    def uuids(items: list[Any]) -> set[str]:
        return {item.uuid for item in items}

    all_active = uuids(backend.active_relationships())
    assert all_active == {active_here.uuid, typed_here.uuid, elsewhere.uuid}, _diverged(
        backend,
        "active_relationships()",
        sorted(all_active),
        sorted({active_here.uuid, typed_here.uuid, elsewhere.uuid}),
    )
    scoped = uuids(backend.active_relationships(scope=AGENT_SCOPE))
    assert scoped == {active_here.uuid, typed_here.uuid}
    typed = uuids(backend.active_relationships(scope=AGENT_SCOPE, relationship_types={"PREFERS"}))
    assert typed == {typed_here.uuid}
    assert backend.active_relationships(scope=AGENT_SCOPE, relationship_types=set()) == []


def test_context_visible_excludes_demoted_but_includes_a_missing_flag(
    backend: StorageBackend,
) -> None:
    # The asymmetry the contract is explicit about: context-visible means
    # ``active_in_context is not False``.  A row that never carried the key is
    # visible; a row demoted to False is not.
    missing_flag = _scoped_relationship(backend, suffix=" missing")
    explicit_true = _scoped_relationship(backend, suffix=" true", active_in_context=True)
    demoted = _scoped_relationship(backend, suffix=" demoted", active_in_context=False)
    inactive = _scoped_relationship(backend, suffix=" inactive")
    backend.mark_relationship(inactive.uuid, status=RelationshipStatus.PRUNED)
    other_scope = _scoped_relationship(backend, scope=OTHER_SCOPE, suffix=" other")

    visible = {item.uuid for item in backend.context_visible_relationships(scope=AGENT_SCOPE)}
    expected = {missing_flag.uuid, explicit_true.uuid}
    assert visible == expected, _diverged(backend, "context_visible_relationships", sorted(visible), sorted(expected))
    assert demoted.uuid not in visible, _diverged(
        backend, "context_visible_relationships demotion filter", "included", "excluded"
    )
    assert inactive.uuid not in visible
    assert other_scope.uuid not in visible

    # A demotion applied after the fact removes the row from the tier, and
    # restoring the flag brings it back — the read is a live projection.
    backend.update_relationship(missing_flag.uuid, properties={"active_in_context": False})
    assert {item.uuid for item in backend.context_visible_relationships(scope=AGENT_SCOPE)} == {explicit_true.uuid}
    backend.update_relationship(missing_flag.uuid, properties={"active_in_context": True})
    assert {item.uuid for item in backend.context_visible_relationships(scope=AGENT_SCOPE)} == expected

    typed = backend.context_visible_relationships(scope=AGENT_SCOPE, relationship_types={"NOTHING_MATCHES"})
    assert typed == [], _diverged(backend, "context_visible type filter", typed, [])


def test_explain_context_visible_read_returns_engine_native_evidence(
    backend: StorageBackend,
) -> None:
    # The plan text is engine-native by design (SQLite EXPLAIN QUERY PLAN rows,
    # a Postgres plan tree), so the contract-level assertion is that each engine
    # produces non-empty, textual access-path evidence for certification.
    plan = backend.explain_context_visible_read(scope=AGENT_SCOPE)
    assert isinstance(plan, list) and plan, _diverged(
        backend, "explain_context_visible_read", plan, "a non-empty list of plan lines"
    )
    assert all(isinstance(line, str) and line.strip() for line in plan)


def test_truth_key_and_truth_prefix_lookups_normalise_and_filter_status(
    backend: StorageBackend,
) -> None:
    matching = _scoped_relationship(
        backend,
        suffix=" truth",
        truth_key="agent:parity-agent:alice:prefers",
        truth_prefix="agent:parity-agent:alice",
    )
    superseded = _scoped_relationship(
        backend,
        suffix=" old truth",
        truth_key="agent:parity-agent:alice:prefers",
        truth_prefix="agent:parity-agent:alice",
    )
    backend.mark_relationship(superseded.uuid, status=RelationshipStatus.SUPERSEDED)
    unrelated = _scoped_relationship(backend, suffix=" unrelated", truth_key="agent:parity-agent:bob:prefers")

    # The lookup normalises its argument the same way the writer did.
    found = backend.find_active_truth_relationships("  AGENT:parity-agent:Alice:PREFERS ", scope_key=AGENT_SCOPE.key)
    assert {item.uuid for item in found} == {matching.uuid}, _diverged(
        backend,
        "find_active_truth_relationships",
        sorted(item.uuid for item in found),
        [matching.uuid],
    )
    assert superseded.uuid not in {item.uuid for item in found}
    assert unrelated.uuid not in {item.uuid for item in found}

    by_prefix = backend.find_active_relationships_by_truth_prefix(
        " Agent:Parity-Agent:Alice ", scope_key=AGENT_SCOPE.key
    )
    assert {item.uuid for item in by_prefix} == {matching.uuid}, _diverged(
        backend,
        "find_active_relationships_by_truth_prefix",
        sorted(item.uuid for item in by_prefix),
        [matching.uuid],
    )
    assert backend.find_active_truth_relationships("nothing at all", scope_key=AGENT_SCOPE.key) == []


def test_the_three_truth_reads_never_cross_a_scope_boundary(backend: StorageBackend) -> None:
    """**This is the arm that confirms Postgres.**

    ``truth_key`` is built by unescaped concatenation of ``scope_key:subject:predicate``
    and then casefolded, so the tuple is not recoverable from the string and two distinct
    scopes can land on one truth slot. Before ``scope_key`` became a required argument,
    these three reads took no scope at all -- so a write in one tenant found, and then
    retired, a live row belonging to another.

    Both original reproductions ran on **SQLite**. This test exists because this repo's own
    history says a source-level argument about Postgres is unverified until a parity arm
    runs: the SQLite twin has been fixed while its Postgres counterpart was not, three times
    (T0-4, T0-7, T0-10).

    The same literal ``truth_key`` and ``truth_prefix`` are seeded in **two** scopes, which
    is exactly what the collision produces. Each assertion is paired with a positive control
    in the same test -- an empty result proves nothing on its own, and a fix that simply
    stopped these reads returning anything would satisfy every "must not see" assertion.
    """
    shared_key = "agent:collide:subject:predicate"
    shared_prefix = "agent:collide:subject"

    mine = _scoped_relationship(
        backend,
        scope=AGENT_SCOPE,
        suffix=" collide mine",
        truth_key=shared_key,
        truth_prefix=shared_prefix,
    )
    theirs = _scoped_relationship(
        backend,
        scope=OTHER_SCOPE,
        suffix=" collide theirs",
        truth_key=shared_key,
        truth_prefix=shared_prefix,
    )
    assert mine.uuid != theirs.uuid

    for name, call in (
        (
            "find_active_truth_relationships",
            lambda key: backend.find_active_truth_relationships(shared_key, scope_key=key),
        ),
        (
            "find_active_relationships_by_truth_prefix",
            lambda key: backend.find_active_relationships_by_truth_prefix(shared_prefix, scope_key=key),
        ),
        (
            "relationships_for_truth_prefix",
            lambda key: backend.relationships_for_truth_prefix(shared_prefix, scope_key=key),
        ),
    ):
        ours = {item.uuid for item in call(AGENT_SCOPE.key)}
        # Positive control: without this, an unconditionally-empty read would pass.
        assert ours == {mine.uuid}, _diverged(backend, f"{name} in its own scope", sorted(ours), [mine.uuid])
        yours = {item.uuid for item in call(OTHER_SCOPE.key)}
        assert yours == {theirs.uuid}, _diverged(backend, f"{name} in the other scope", sorted(yours), [theirs.uuid])
        assert theirs.uuid not in ours, (
            f"{name} returned {OTHER_SCOPE.key}'s row to a caller in {AGENT_SCOPE.key} -- "
            "the two share a truth key, and this read is what a supersession acts on"
        )


def test_scopes_lists_every_scope_that_has_a_relationship(backend: StorageBackend) -> None:
    _scoped_relationship(backend, suffix=" one")
    _scoped_relationship(backend, suffix=" two")
    _scoped_relationship(backend, scope=OTHER_SCOPE, suffix=" three")
    # A relationship carrying no scope terms names no scope.
    unscoped_source = _node(backend, "unscoped source")
    unscoped_target = _node(backend, "unscoped target")
    backend.add_relationship(
        source_uuid=unscoped_source.uuid,
        target_uuid=unscoped_target.uuid,
        relationship_type="REQUIRES",
        properties={"status": "active"},
    )

    # Membership, not order: "first-seen" is decided by the rows' wall-clock
    # ``created_at``, which this suite refuses to depend on.
    keys = {scope.key for scope in backend.scopes()}
    assert keys == {AGENT_SCOPE.key, OTHER_SCOPE.key}, _diverged(
        backend, "scopes()", sorted(keys), sorted({AGENT_SCOPE.key, OTHER_SCOPE.key})
    )
    assert len(backend.scopes()) == 2, "each scope must appear exactly once"


def test_nodes_for_scope_selects_only_the_scoped_nodes(backend: StorageBackend) -> None:
    mine, _ = backend.upsert_node(
        labels=["Entity"], key="mine", properties={"name": "mine", "scope_key": AGENT_SCOPE.key}
    )
    theirs, _ = backend.upsert_node(
        labels=["Entity"], key="theirs", properties={"name": "theirs", "scope_key": OTHER_SCOPE.key}
    )
    _node(backend, "unscoped")
    # Set comparison: ``nodes_for_scope`` is an erasure sweep and the contract
    # states no order for it.
    scoped = {node.uuid for node in backend.nodes_for_scope(AGENT_SCOPE.key)}
    assert scoped == {mine.uuid}, _diverged(backend, "nodes_for_scope", sorted(scoped), [mine.uuid])
    assert {node.uuid for node in backend.nodes_for_scope(OTHER_SCOPE.key)} == {theirs.uuid}
    assert backend.nodes_for_scope("agent:nobody") == []


def test_delete_nodes_and_relationships_are_idempotent(backend: StorageBackend) -> None:
    relationship = _scoped_relationship(backend, suffix=" doomed")
    source_uuid = relationship.source_uuid
    target_uuid = relationship.target_uuid

    assert backend.delete_relationships([]) == 0
    assert backend.delete_relationships(["never-existed"]) == 0
    assert backend.delete_relationships([relationship.uuid]) == 1
    # Idempotent by uuid set, per the cross-store rule.
    assert backend.delete_relationships([relationship.uuid]) == 0, _diverged(
        backend, "delete_relationships idempotency", "non-zero", 0
    )
    with pytest.raises(ValueError):
        backend.get_relationship(relationship.uuid)

    assert backend.delete_nodes([]) == 0
    assert backend.delete_nodes([source_uuid, target_uuid, "never-existed"]) == 2
    assert backend.delete_nodes([source_uuid, target_uuid]) == 0, _diverged(
        backend, "delete_nodes idempotency", "non-zero", 0
    )
    assert backend.nodes() == []
    assert backend.relationships() == []


def test_export_dumps_every_node_and_relationship(backend: StorageBackend) -> None:
    relationship = _scoped_relationship(backend, suffix=" export")
    exported = backend.export()
    assert set(exported) == {"nodes", "relationships"}
    # JSON-serialisable is part of the contract.
    json.dumps(exported)
    assert len(exported["nodes"]) == 2
    assert len(exported["relationships"]) == 1
    assert exported["relationships"][0]["uuid"] == relationship.uuid
    assert exported["relationships"][0]["properties"]["fact"] == "parity fact"
    assert {node["uuid"] for node in exported["nodes"]} == {
        relationship.source_uuid,
        relationship.target_uuid,
    }


# ===========================================================================
# 2. graph_state_hash — equality ACROSS engines
# ===========================================================================


# Both engines must store byte-identical properties for their hashes to be
# comparable, so the sealed fact and its write-time commitment are computed
# once here rather than per engine (sealing is nonce-random, and each store
# holds its own ephemeral DEK).
_FIXED_FACT_COMMITMENT = "b" * 64


def _state_hash_stages(backend: StorageBackend) -> list[tuple[str, str]]:
    """Drive one graph through its whole mutation lifecycle, hashing each step.

    Returns ``[(stage label, state hash), ...]``.  Both engines are driven by
    this single function with the same deterministic identifiers, so any
    difference in the returned hashes is a difference in the storage engine.
    """
    scope = AGENT_SCOPE
    stages: list[tuple[str, str]] = [("empty scope", backend.graph_state_hash(scope.key))]

    subject, _ = backend.upsert_node(labels=["Entity"], key="alice", properties={"name": "Alice"})
    laptop, _ = backend.upsert_node(labels=["Entity"], key="laptop", properties={"name": "Laptop"})
    desk, _ = backend.upsert_node(labels=["Entity"], key="desk", properties={"name": "Desk"})

    requires = backend.add_relationship(
        source_uuid=subject.uuid,
        target_uuid=laptop.uuid,
        relationship_type="REQUIRES",
        properties={
            "scope_key": scope.key,
            "status": RelationshipStatus.ACTIVE.value,
            "predicate": "requires",
            "memory_type": "requirement",
            "fact": "alice requires a laptop",
            "fact_commitment": _FIXED_FACT_COMMITMENT,
            "observed_count": 1,
        },
        valid_from=EPOCH,
    )
    prefers = backend.add_relationship(
        source_uuid=subject.uuid,
        target_uuid=desk.uuid,
        relationship_type="PREFERS",
        properties={
            "scope_key": scope.key,
            "status": RelationshipStatus.ACTIVE.value,
            "predicate": "prefers",
            "memory_type": "preference",
            "fact": "alice prefers a standing desk",
            "observed_count": 2,
        },
    )
    stages.append(("two memory relationships", backend.graph_state_hash(scope.key)))

    # MENTIONS is a structural edge and is excluded from the hash: adding one
    # must not move the value.
    mentions = backend.add_relationship(
        source_uuid=subject.uuid,
        target_uuid=laptop.uuid,
        relationship_type="MENTIONS",
        properties={"scope_key": scope.key, "status": RelationshipStatus.ACTIVE.value},
    )
    stages.append(("after adding a MENTIONS edge", backend.graph_state_hash(scope.key)))
    backend.delete_relationships([mentions.uuid])
    stages.append(("after deleting the MENTIONS edge", backend.graph_state_hash(scope.key)))

    backend.update_relationship(requires.uuid, properties={"observed_count": 7})
    stages.append(("after a property update", backend.graph_state_hash(scope.key)))

    backend.mark_relationship(prefers.uuid, status=RelationshipStatus.SUPERSEDED)
    stages.append(("after a status change", backend.graph_state_hash(scope.key)))

    # The subject node's name is part of every outgoing relationship's tuple.
    backend.upsert_node(labels=["Entity"], key="alice", properties={"name": "Alice Renamed"})
    stages.append(("after a subject-node rename", backend.graph_state_hash(scope.key)))

    backend.delete_relationships([prefers.uuid])
    stages.append(("after a relationship delete", backend.graph_state_hash(scope.key)))

    # Crypto-shred destroys the scope's DEK and rewrites no graph row, so the
    # hash — which binds stored write-time digests — must not move.
    backend.get_or_create_governance_key(scope.key)
    assert backend.shred_governance_key(scope.key) is True
    stages.append(("after crypto-shred", backend.graph_state_hash(scope.key)))

    stages.append(("an untouched scope", backend.graph_state_hash(OTHER_SCOPE.key)))
    return stages


@pytest.mark.postgres
def test_graph_state_hash_is_identical_across_engines(
    both_backends: tuple[StorageBackend, StorageBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single most important assertion in the file.

    Postgres maintains the scope state hash incrementally (one canonical tuple
    row per relationship, refreshed on write) while SQLite recomputes it from
    the store on every call.  Equality of the two *strings* is what proves the
    optimisation preserved the value that receipts and replay proofs are
    anchored on; self-consistency on each engine would not.
    """
    # Patch uuid4 in EVERY models submodule that binds it, not on the package.
    #
    # Before the split, models.py was one module: `uuid4` sat in its globals and every
    # `default_factory=lambda: str(uuid4())` resolved through it, so patching
    # `memotron.models.uuid4` intercepted all of them. After the split each
    # submodule does its own `from uuid import uuid4`, so the package attribute is not
    # what the factories resolve against -- and the package no longer re-exports it.
    #
    # Restoring that re-export would be WORSE than the AttributeError this raised:
    # setattr would succeed, the patch would intercept nothing, and both engines would
    # mint random uuids while the test still claimed to pin them. Patching every
    # binding reproduces the pre-split semantics exactly.
    #
    # Found by the Postgres lane on its first run of this effort. 1053 tests passed
    # without it.
    import memotron.models._artifacts as _m_artifacts
    import memotron.models._coherence as _m_coherence
    import memotron.models._events as _m_events
    import memotron.models._graph as _m_graph
    import memotron.models._runs as _m_runs

    _uuid_modules = (_m_artifacts, _m_coherence, _m_events, _m_graph, _m_runs)

    sqlite_backend, postgres_backend = both_backends
    results: dict[str, list[tuple[str, str]]] = {}
    for store in (sqlite_backend, postgres_backend):
        # A fresh sequence per engine: the hash binds relationship uuids, so
        # both engines must mint the identical ones.
        _sequence = _SequentialUUIDs()
        for _mod in _uuid_modules:
            monkeypatch.setattr(_mod, "uuid4", _sequence)
        results[_engine_name(store)] = _state_hash_stages(store)

    sqlite_stages = results["sqlite"]
    postgres_stages = results["postgres"]
    assert [label for label, _ in sqlite_stages] == [label for label, _ in postgres_stages]
    for (label, sqlite_hash), (_, postgres_hash) in zip(sqlite_stages, postgres_stages, strict=False):
        assert sqlite_hash == postgres_hash, (
            f"graph_state_hash diverged at stage {label!r}: "
            f"sqlite returned {sqlite_hash!r} but postgres returned {postgres_hash!r} "
            "— the incrementally maintained Postgres hash no longer reproduces the "
            "value receipts and replay proofs are anchored on"
        )

    by_label = dict(sqlite_stages)
    empty = by_label["empty scope"]
    populated = by_label["two memory relationships"]
    assert empty != populated, "a populated scope must not hash like an empty one"
    assert by_label["an untouched scope"] == empty, "a scope with no relationships must hash like any other empty scope"
    assert by_label["after adding a MENTIONS edge"] == populated, (
        "MENTIONS structural edges must be excluded from the state hash"
    )
    assert by_label["after deleting the MENTIONS edge"] == populated
    assert by_label["after a property update"] != populated
    assert by_label["after a status change"] != by_label["after a property update"]
    assert by_label["after a subject-node rename"] != by_label["after a status change"]
    assert by_label["after a relationship delete"] != by_label["after a subject-node rename"]
    assert by_label["after crypto-shred"] == by_label["after a relationship delete"], (
        "graph_state_hash must be stable across a crypto-shred: the shred destroys "
        "the key, not the stored write-time digests"
    )


def test_graph_state_hash_is_deterministic_and_scope_local(backend: StorageBackend) -> None:
    mine = _scoped_relationship(backend, suffix=" hashed")
    first = backend.graph_state_hash(AGENT_SCOPE.key)
    assert backend.graph_state_hash(AGENT_SCOPE.key) == first, _diverged(
        backend, "graph_state_hash determinism", "a different hash", first
    )
    # Writing into another scope must not disturb this scope's hash.
    _scoped_relationship(backend, scope=OTHER_SCOPE, suffix=" elsewhere")
    assert backend.graph_state_hash(AGENT_SCOPE.key) == first, _diverged(
        backend, "graph_state_hash scope locality", "a changed hash", first
    )
    backend.update_relationship(mine.uuid, properties={"observed_count": 9})
    assert backend.graph_state_hash(AGENT_SCOPE.key) != first


def test_graph_state_hash_treats_a_visibility_allowlist_as_an_unordered_set(
    backend: StorageBackend,
) -> None:
    """Reordering a ``visibility_agents`` allowlist is not a state change; editing it is.

    The state tuple folds the allowlist through an order-stable projection
    (``storage/_shared/_rows.py::canonical_visibility_agents``) so the hash
    commits to *who may read the row*, not to the order the writer happened to
    supply.  The hashes are required to be equal across engines, so a divergence
    here is a divergence in the derived hash that receipt chains and replay
    proofs are anchored on.

    Until this change each engine carried its OWN copy of that projection --
    a module-level function in ``postgres/_graph.py`` and a ``@staticmethod`` on
    ``sqlite/_graph.py``, the second documented as "byte-identical" to the first.
    Both copies are gone and one definition now serves both engines, so the
    hashes cannot drift by a divergence in the projection itself.  That does NOT
    make this test redundant: what it pins is the *fold* -- each engine still
    builds its own state tuple and its own SQL around the shared call, and the
    empty/absent equivalence and the invalidate-on-widen behaviour are engine
    code.

    It also still drives the ``sorted(...)`` branch on BOTH engines, which is the
    reason it was written: before it, the non-empty path had never executed on
    Postgres and ``parity_coverage.py`` reported the projection as covered on
    SQLite only.  Note that gate can no longer see it -- ``_shared`` has no twin
    to compare against, so it is out of that script's population by construction
    (see its docstring).  This test IS the coverage now, which is why it is
    parameterised over ``backend`` rather than pinned to one engine.
    """
    row = _scoped_relationship(backend, suffix=" allowlisted", visibility_agents=["beta", "alpha"])
    unsorted_order = backend.graph_state_hash(AGENT_SCOPE.key)

    # Same readers, different order and a duplicate: the projection sorts and
    # de-duplicates, so the hash must not move.
    backend.update_relationship(row.uuid, properties={"visibility_agents": ["alpha", "beta", "alpha"]})
    assert backend.graph_state_hash(AGENT_SCOPE.key) == unsorted_order, _diverged(
        backend,
        "graph_state_hash after reordering a visibility allowlist",
        backend.graph_state_hash(AGENT_SCOPE.key),
        unsorted_order,
    )

    # Adding a reader IS a state change.
    backend.update_relationship(row.uuid, properties={"visibility_agents": ["alpha", "beta", "gamma"]})
    widened = backend.graph_state_hash(AGENT_SCOPE.key)
    assert widened != unsorted_order, _diverged(
        backend, "graph_state_hash after widening a visibility allowlist", widened, "a different hash"
    )

    # An empty allowlist is scope-default visibility, which the projection maps
    # to the same null a row that never had one contributes.
    backend.update_relationship(row.uuid, properties={"visibility_agents": []})
    emptied = backend.graph_state_hash(AGENT_SCOPE.key)
    backend.update_relationship(row.uuid, properties={"visibility_agents": None})
    assert backend.graph_state_hash(AGENT_SCOPE.key) == emptied, _diverged(
        backend,
        "graph_state_hash for an empty vs absent visibility allowlist",
        backend.graph_state_hash(AGENT_SCOPE.key),
        emptied,
    )


def test_graph_state_hash_follows_a_relationship_moved_between_scopes(
    backend: StorageBackend,
) -> None:
    """The scope a relationship *leaves* must be re-hashed, not just the one it joins.

    ``update_relationship`` can rewrite ``scope_key``.  An engine that memoises
    the hash has to invalidate both scopes; invalidating only the destination
    leaves the origin committing to a row it no longer contains, which is a
    state hash the graph never had.
    """
    empty = backend.graph_state_hash("agent:no-such-scope")
    moved = _scoped_relationship(backend, suffix=" travelling")
    staying = _scoped_relationship(backend, suffix=" staying")
    populated = backend.graph_state_hash(AGENT_SCOPE.key)
    assert populated != empty
    assert backend.graph_state_hash(OTHER_SCOPE.key) == empty

    backend.update_relationship(moved.uuid, properties={"scope_key": OTHER_SCOPE.key})
    after_origin = backend.graph_state_hash(AGENT_SCOPE.key)
    after_destination = backend.graph_state_hash(OTHER_SCOPE.key)
    assert after_origin != populated, _diverged(
        backend,
        "graph_state_hash of the scope a relationship left",
        after_origin,
        "a hash that no longer covers the moved relationship",
    )
    assert after_destination != empty, _diverged(
        backend,
        "graph_state_hash of the scope a relationship joined",
        after_destination,
        "a hash that covers the moved relationship",
    )
    # What remains in the origin is exactly the relationship that stayed.
    backend.delete_relationships([staying.uuid])
    assert backend.graph_state_hash(AGENT_SCOPE.key) == empty, _diverged(
        backend, "graph_state_hash after emptying the origin scope", "a stale hash", empty
    )

    # Dropping the scope key entirely removes the row from every scope's hash.
    backend.update_relationship(moved.uuid, properties={"scope_key": ""})
    assert backend.graph_state_hash(OTHER_SCOPE.key) == empty, _diverged(
        backend, "graph_state_hash after clearing a relationship's scope", "a stale hash", empty
    )


# ===========================================================================
# 3. Operational plane
# ===========================================================================


def test_episode_queue_round_trip_and_duplicate_rejection(backend: StorageBackend) -> None:
    # ``episodes()`` orders by (created_at, uuid); the test supplies distinct
    # created_at values, so the order is fully determined by the contract.
    first = _episode(AGENT_SCOPE, "first", created_at=EPOCH)
    second = _episode(AGENT_SCOPE, "second", created_at=EPOCH + timedelta(minutes=1))
    third = _episode(OTHER_SCOPE, "third", created_at=EPOCH + timedelta(minutes=2))
    for episode in (third, first, second):
        backend.add_episode(episode)

    stored = [episode.uuid for episode in backend.episodes()]
    assert stored == [first.uuid, second.uuid, third.uuid], _diverged(
        backend, "episodes() ordering", stored, [first.uuid, second.uuid, third.uuid]
    )
    assert backend.get_episode(second.uuid).name == "second"
    assert [item.uuid for item in backend.episodes_for_scope(AGENT_SCOPE.key)] == [
        first.uuid,
        second.uuid,
    ]
    assert backend.episodes_for_scope("agent:nobody") == []
    with pytest.raises(ValueError, match="episode already exists"):
        backend.add_episode(first)


def test_episode_processing_marks_are_per_consumer_and_idempotent(
    backend: StorageBackend,
) -> None:
    episode = _episode(AGENT_SCOPE, "processed", created_at=EPOCH)
    backend.add_episode(episode)
    assert backend.is_episode_processed(episode.uuid) is False
    assert backend.is_episode_processed(episode.uuid, consumer_key="formation") is False

    backend.mark_episode_processed(episode.uuid, processed_at=EPOCH, consumer_key="  Formation  ")
    # The consumer key is normalised the same way on both engines.
    assert backend.is_episode_processed(episode.uuid, consumer_key="formation") is True
    assert backend.is_episode_processed(episode.uuid, consumer_key="pruning") is False, _diverged(
        backend, "is_episode_processed per-consumer isolation", True, False
    )
    assert backend.is_episode_processed(episode.uuid) is True
    # Idempotent per (episode, consumer).
    backend.mark_episode_processed(episode.uuid, processed_at=EPOCH + timedelta(hours=1), consumer_key="formation")
    assert backend.is_episode_processed(episode.uuid, consumer_key="formation") is True

    # The legacy whole-episode marker wins outright, even for a named consumer.
    other = _episode(AGENT_SCOPE, "legacy", created_at=EPOCH + timedelta(minutes=1))
    backend.add_episode(other)
    backend.mark_episode_processed(other.uuid, processed_at=EPOCH)
    assert backend.is_episode_processed(other.uuid, consumer_key="anything") is True, _diverged(
        backend, "legacy processed marker precedence", False, True
    )


def test_episode_event_counts_report_total_and_globally_pending(
    backend: StorageBackend,
) -> None:
    pending = _episode(AGENT_SCOPE, "pending", created_at=EPOCH, agent_memory_event="reflection")
    claimed = _episode(
        AGENT_SCOPE,
        "claimed",
        created_at=EPOCH + timedelta(minutes=1),
        agent_memory_event="reflection",
    )
    legacy = _episode(
        AGENT_SCOPE,
        "legacy",
        created_at=EPOCH + timedelta(minutes=2),
        agent_memory_event="reflection",
    )
    other_kind = _episode(AGENT_SCOPE, "other", created_at=EPOCH + timedelta(minutes=3), agent_memory_event="digest")
    other_scope = _episode(OTHER_SCOPE, "elsewhere", created_at=EPOCH, agent_memory_event="reflection")
    for episode in (pending, claimed, legacy, other_kind, other_scope):
        backend.add_episode(episode)
    backend.mark_episode_processed(claimed.uuid, processed_at=EPOCH, consumer_key="formation")
    backend.mark_episode_processed(legacy.uuid, processed_at=EPOCH)

    counts = backend.episode_event_counts(scope=AGENT_SCOPE, event="  reflection ")
    assert counts == (3, 1), _diverged(backend, "episode_event_counts", counts, (3, 1))
    assert backend.episode_event_counts(scope=AGENT_SCOPE, event="digest") == (1, 1)
    assert backend.episode_event_counts(scope=OTHER_SCOPE, event="reflection") == (1, 1)
    assert backend.episode_event_counts(scope=AGENT_SCOPE, event="absent") == (0, 0)


def test_job_state_upserts_the_last_run_marker(backend: StorageBackend) -> None:
    assert backend.get_job_last_run("formation") is None
    backend.set_job_last_run("formation", EPOCH)
    assert backend.get_job_last_run("formation") == EPOCH
    backend.set_job_last_run("formation", EPOCH + timedelta(hours=2))
    assert backend.get_job_last_run("formation") == EPOCH + timedelta(hours=2), _diverged(
        backend, "set_job_last_run upsert", backend.get_job_last_run("formation"), EPOCH
    )
    assert backend.get_job_last_run("pruning") is None


def test_dream_job_runs_are_newest_first_and_filterable(backend: StorageBackend) -> None:
    # ``ran_at`` is supplied explicitly and is distinct per record, so the
    # contract's "newest first" order is fully determined.
    records = [
        DreamJobRunRecord(
            ran_at=EPOCH + timedelta(minutes=index),
            job_name="formation" if index % 2 == 0 else "pruning",
            job_kind=DreamJobKind.FORMATION if index % 2 == 0 else DreamJobKind.PRUNING,
            processed_episodes=index,
        )
        for index in range(4)
    ]
    for record in records:
        backend.record_dream_job_run(record)

    newest_first = [item.uuid for item in backend.dream_job_runs()]
    assert newest_first == [record.uuid for record in reversed(records)], _diverged(
        backend, "dream_job_runs ordering", newest_first, [r.uuid for r in reversed(records)]
    )
    assert [item.uuid for item in backend.dream_job_runs(limit=2)] == [
        records[3].uuid,
        records[2].uuid,
    ]
    formation = [item.uuid for item in backend.dream_job_runs(job_name="  formation ")]
    assert formation == [records[2].uuid, records[0].uuid], _diverged(
        backend, "dream_job_runs job_name filter", formation, [records[2].uuid, records[0].uuid]
    )
    assert backend.dream_job_runs(job_name="nothing") == []


def _decision(
    *,
    ran_at: datetime,
    job_name: str,
    agent_id: str,
    decision_type: str,
    scope: MemoryScope | None = AGENT_SCOPE,
) -> DreamDecisionRecord:
    return DreamDecisionRecord(
        ran_at=ran_at,
        job_name=job_name,
        job_kind=DreamJobKind.FORMATION,
        agent_id=agent_id,
        agent_name=f"{agent_id} agent",
        agent_scope=MemoryScope(kind=ScopeKind.AGENT, scope_id=agent_id),
        decision_type=decision_type,
        summary=f"{decision_type} for {agent_id}",
        scope=scope,
    )


def test_dream_decisions_filter_by_job_agent_and_type_prefix(backend: StorageBackend) -> None:
    # Distinct explicit ``ran_at`` values give the contract's newest-first order.
    alpha = _decision(ran_at=EPOCH, job_name="formation", agent_id="alpha", decision_type="formation.created")
    beta = _decision(
        ran_at=EPOCH + timedelta(minutes=1),
        job_name="formation",
        agent_id="beta",
        decision_type="formation.reinforced",
    )
    gamma = _decision(
        ran_at=EPOCH + timedelta(minutes=2),
        job_name="pruning",
        agent_id="alpha",
        decision_type="pruning.pruned",
        scope=OTHER_SCOPE,
    )
    unscoped = _decision(
        ran_at=EPOCH + timedelta(minutes=3),
        job_name="pruning",
        agent_id="beta",
        decision_type="pruning.kept",
        scope=None,
    )
    for record in (alpha, beta, gamma, unscoped):
        backend.record_dream_decision(record)

    everything = [item.uuid for item in backend.dream_decisions()]
    assert everything == [unscoped.uuid, gamma.uuid, beta.uuid, alpha.uuid], _diverged(
        backend,
        "dream_decisions ordering",
        everything,
        [unscoped.uuid, gamma.uuid, beta.uuid, alpha.uuid],
    )
    assert [item.uuid for item in backend.dream_decisions(limit=1)] == [unscoped.uuid]
    assert [item.uuid for item in backend.dream_decisions(job_name="formation")] == [
        beta.uuid,
        alpha.uuid,
    ]
    assert [item.uuid for item in backend.dream_decisions(agent_id="alpha")] == [
        gamma.uuid,
        alpha.uuid,
    ]
    by_prefix = [item.uuid for item in backend.dream_decisions(decision_type_prefix="formation.")]
    assert by_prefix == [beta.uuid, alpha.uuid], _diverged(
        backend, "dream_decisions prefix filter", by_prefix, [beta.uuid, alpha.uuid]
    )
    # '%' is escaped to a literal, so a wildcard cannot widen the prefix.
    assert backend.dream_decisions(decision_type_prefix="%") == [], _diverged(
        backend, "dream_decisions prefix wildcard escaping", "matches", []
    )
    combined = backend.dream_decisions(job_name="pruning", agent_id="alpha")
    assert [item.uuid for item in combined] == [gamma.uuid]

    # ``dream_decisions_for_scope`` drops unscoped decisions and orders oldest
    # first by the explicit ``ran_at``.
    scoped = [item.uuid for item in backend.dream_decisions_for_scope(AGENT_SCOPE.key)]
    assert scoped == [alpha.uuid, beta.uuid], _diverged(
        backend, "dream_decisions_for_scope", scoped, [alpha.uuid, beta.uuid]
    )
    assert [item.uuid for item in backend.dream_decisions_for_scope(OTHER_SCOPE.key)] == [gamma.uuid]
    assert backend.dream_decisions_for_scope("agent:nobody") == []


def test_use_event_append_is_idempotent_per_scope_and_key(backend: StorageBackend) -> None:
    relationship = _scoped_relationship(backend, suffix=" used")
    event = _use_event(
        relationship.uuid,
        kind=UseEventKind.CITED_OR_USED,
        used_at=EPOCH,
        idempotency_key="use-1",
    )
    stored = backend.record_use_event(event)
    assert stored.use_id == event.use_id

    # Replaying the identical event returns the stored one.
    replayed = backend.record_use_event(event)
    assert replayed.use_id == stored.use_id, _diverged(
        backend, "record_use_event replay", replayed.use_id, stored.use_id
    )
    assert len(backend.use_events(scope_key=AGENT_SCOPE.key)) == 1, _diverged(
        backend,
        "record_use_event replay side effect",
        len(backend.use_events(scope_key=AGENT_SCOPE.key)),
        1,
    )
    # A *different* event replayed under a used key is an error.
    conflicting = _use_event(
        relationship.uuid,
        kind=UseEventKind.RETRIEVED,
        used_at=EPOCH,
        idempotency_key="use-1",
    )
    with pytest.raises(ValueError) as error:
        backend.record_use_event(conflicting)
    assert str(error.value) == ("use event idempotency key 'use-1' was already used for a different event"), _diverged(
        backend, "idempotency conflict message", str(error.value), "the SQLite wording"
    )

    # The cross-plane check rejects an event whose scope is not the relationship's.
    mismatched = _use_event(
        relationship.uuid,
        kind=UseEventKind.RETRIEVED,
        used_at=EPOCH,
        idempotency_key="use-2",
        scope=OTHER_SCOPE,
    )
    with pytest.raises(ValueError) as scope_error:
        backend.record_use_event(mismatched)
    assert "does not match relationship scope" in str(scope_error.value)


def test_outcome_events_bind_to_their_use_event(backend: StorageBackend) -> None:
    relationship = _scoped_relationship(backend, suffix=" judged")
    use = backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH,
            idempotency_key="use-1",
        )
    )
    outcome = backend.record_outcome_event(
        _outcome_event(
            use.use_id,
            verdict=OutcomeVerdict.POSITIVE,
            judged_at=EPOCH + timedelta(minutes=5),
            idempotency_key="outcome-1",
        )
    )
    assert [item.outcome_id for item in backend.outcome_events(scope_key=AGENT_SCOPE.key)] == [outcome.outcome_id]
    assert (
        backend.record_outcome_event(
            _outcome_event(
                use.use_id,
                verdict=OutcomeVerdict.POSITIVE,
                judged_at=EPOCH + timedelta(minutes=5),
                idempotency_key="outcome-1",
            )
        ).outcome_id
        == outcome.outcome_id
    )

    with pytest.raises(ValueError) as conflict:
        backend.record_outcome_event(
            _outcome_event(
                use.use_id,
                verdict=OutcomeVerdict.NEGATIVE,
                judged_at=EPOCH + timedelta(minutes=5),
                idempotency_key="outcome-1",
            )
        )
    assert str(conflict.value) == ("outcome event idempotency key 'outcome-1' was already used for a different event")

    with pytest.raises(ValueError) as run_error:
        backend.record_outcome_event(
            _outcome_event(
                use.use_id,
                verdict=OutcomeVerdict.POSITIVE,
                judged_at=EPOCH,
                task_run_id="other-run",
                idempotency_key="outcome-2",
            )
        )
    assert str(run_error.value) == ("outcome event task_run_id must match the referenced use event task_run_id")

    with pytest.raises(ValueError) as scope_error:
        backend.record_outcome_event(
            _outcome_event(
                use.use_id,
                verdict=OutcomeVerdict.POSITIVE,
                judged_at=EPOCH,
                idempotency_key="outcome-3",
                scope=OTHER_SCOPE,
            )
        )
    assert str(scope_error.value) == ("outcome event scope must match the referenced use event scope")


def test_event_reads_are_filterable_and_ordered(backend: StorageBackend) -> None:
    first = _scoped_relationship(backend, suffix=" e1")
    second = _scoped_relationship(backend, suffix=" e2")
    # ``used_at`` is explicit and distinct, so the contract's
    # ``ORDER BY (used_at, use_id)`` is fully determined by these values.
    events = [
        backend.record_use_event(
            _use_event(
                first.uuid,
                kind=UseEventKind.RETRIEVED,
                used_at=EPOCH,
                idempotency_key="u1",
            )
        ),
        backend.record_use_event(
            _use_event(
                second.uuid,
                kind=UseEventKind.INJECTED,
                used_at=EPOCH + timedelta(seconds=1),
                task_run_id="task-2",
                idempotency_key="u2",
            )
        ),
        backend.record_use_event(
            _use_event(
                first.uuid,
                kind=UseEventKind.CITED_OR_USED,
                used_at=EPOCH + timedelta(seconds=2),
                idempotency_key="u3",
            )
        ),
    ]
    ordered = [item.use_id for item in backend.use_events(scope_key=AGENT_SCOPE.key)]
    assert ordered == [event.use_id for event in events], _diverged(
        backend, "use_events ordering", ordered, [event.use_id for event in events]
    )
    assert [item.use_id for item in backend.use_events(relationship_uuid=first.uuid)] == [
        events[0].use_id,
        events[2].use_id,
    ]
    assert [item.use_id for item in backend.use_events(task_run_id="task-2")] == [events[1].use_id]
    assert backend.use_events(scope_key=OTHER_SCOPE.key) == []

    outcomes = [
        backend.record_outcome_event(
            _outcome_event(
                events[0].use_id,
                verdict=OutcomeVerdict.POSITIVE,
                judged_at=EPOCH + timedelta(seconds=10),
                idempotency_key="o1",
            )
        ),
        backend.record_outcome_event(
            _outcome_event(
                events[2].use_id,
                verdict=OutcomeVerdict.NEGATIVE,
                judged_at=EPOCH + timedelta(seconds=11),
                idempotency_key="o2",
            )
        ),
    ]
    ordered_outcomes = [item.outcome_id for item in backend.outcome_events()]
    assert ordered_outcomes == [item.outcome_id for item in outcomes], _diverged(
        backend,
        "outcome_events ordering",
        ordered_outcomes,
        [item.outcome_id for item in outcomes],
    )
    assert [item.outcome_id for item in backend.outcome_events(use_id=events[2].use_id)] == [outcomes[1].outcome_id]
    assert [item.outcome_id for item in backend.outcome_events(task_run_id="task-1")] == [
        item.outcome_id for item in outcomes
    ]


def test_utility_projection_from_events_matches_the_receipt_replay(
    backend: StorageBackend,
) -> None:
    relationship = _scoped_relationship(backend, suffix=" utility", memory_type="preference")
    as_of = EPOCH + timedelta(days=1)
    retrieved = backend.record_use_event(
        _use_event(relationship.uuid, kind=UseEventKind.RETRIEVED, used_at=EPOCH, idempotency_key="u1")
    )
    injected = backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.INJECTED,
            used_at=EPOCH + timedelta(seconds=1),
            idempotency_key="u2",
        )
    )
    cited = backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH + timedelta(seconds=2),
            idempotency_key="u3",
        )
    )
    positive = backend.record_outcome_event(
        _outcome_event(
            cited.use_id,
            verdict=OutcomeVerdict.POSITIVE,
            judged_at=EPOCH + timedelta(seconds=3),
            idempotency_key="o1",
        )
    )
    negative = backend.record_outcome_event(
        _outcome_event(
            injected.use_id,
            verdict=OutcomeVerdict.CORRECTED,
            judged_at=EPOCH + timedelta(seconds=4),
            idempotency_key="o2",
        )
    )

    projections = backend.utility_projection(scope_key=AGENT_SCOPE.key, as_of=as_of)
    assert len(projections) == 1
    projection = projections[0]
    assert projection.relationship_uuid == relationship.uuid
    assert projection.impression_count == 1
    assert projection.injected_count == 1
    assert projection.cited_or_used_count == 1
    assert projection.positive_outcome_count == 1
    assert projection.negative_outcome_count == 1
    assert projection.beta_positive == pytest.approx(2.0)
    assert projection.beta_negative == pytest.approx(2.0)
    assert projection.last_used_at == cited.used_at
    assert projection.last_injected_at == injected.used_at
    assert projection.last_retrieved_at == retrieved.used_at
    # preference => 90-day half life; one citation and one positive outcome
    # decay from their explicit timestamps to the explicit as_of.
    half_life = 90 * 24 * 3600.0
    expected_stability = sum(
        2.0 ** (-max(0.0, (as_of - stamp).total_seconds()) / half_life) for stamp in (cited.used_at, positive.judged_at)
    )
    assert projection.use_stability == pytest.approx(expected_stability), _diverged(
        backend, "utility use_stability", projection.use_stability, expected_stability
    )
    assert projection.outcome_quality == pytest.approx(0.5)

    # An as_of before the events yields nothing.
    assert backend.utility_projection(scope_key=AGENT_SCOPE.key, as_of=EPOCH - timedelta(days=1)) == []

    # The same projection must be rebuildable from the hash-linked receipts
    # alone; the event tables are an operational index, not the audit path.
    run = backend.receipts.begin_run(
        run_kind="formation",
        job_name="parity-events",
        scope_key=AGENT_SCOPE.key,
        effective_policy_digest=config_effective_policy_digest({"parity": True}),
    )
    for index, event in enumerate((retrieved, injected, cited)):
        payload = event.model_dump(mode="json")
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.USE_EVENT_RECORDED,
            decision_reason="use_event_recorded",
            decision_result="recorded",
            relationship_uuid=event.relationship_uuid,
            use_event_id=event.use_id,
            event_payload=json.dumps(payload),
            event_payload_digest=payload_digest(payload),
            created_at=EPOCH + timedelta(seconds=index),
        )
    for index, outcome in enumerate((positive, negative)):
        payload = outcome.model_dump(mode="json")
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.OUTCOME_EVENT_RECORDED,
            decision_reason="outcome_event_recorded",
            decision_result="recorded",
            outcome_event_id=outcome.outcome_id,
            event_payload=json.dumps(payload),
            event_payload_digest=payload_digest(payload),
            created_at=EPOCH + timedelta(seconds=10 + index),
        )

    from_receipts = backend.utility_projection_from_receipts(scope_key=AGENT_SCOPE.key, as_of=as_of)
    assert [item.model_dump() for item in from_receipts] == [item.model_dump() for item in projections], _diverged(
        backend,
        "utility_projection_from_receipts vs utility_projection",
        [item.model_dump() for item in from_receipts],
        [item.model_dump() for item in projections],
    )


def test_retrieval_negative_space_reports_impressions_that_never_progressed(
    backend: StorageBackend,
) -> None:
    stalled = _scoped_relationship(backend, suffix=" stalled")
    progressed = _scoped_relationship(backend, suffix=" progressed")
    # Explicit, distinct ``used_at`` values fix the (used_at, use_id) order.
    stalled_use = backend.record_use_event(
        _use_event(stalled.uuid, kind=UseEventKind.RETRIEVED, used_at=EPOCH, idempotency_key="n1")
    )
    progressed_use = backend.record_use_event(
        _use_event(
            progressed.uuid,
            kind=UseEventKind.RETRIEVED,
            used_at=EPOCH + timedelta(seconds=1),
            idempotency_key="n2",
        )
    )
    backend.record_use_event(
        _use_event(
            progressed.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH + timedelta(seconds=2),
            idempotency_key="n3",
        )
    )
    other_run = backend.record_use_event(
        _use_event(
            stalled.uuid,
            kind=UseEventKind.RETRIEVED,
            used_at=EPOCH + timedelta(seconds=3),
            task_run_id="task-2",
            idempotency_key="n4",
        )
    )
    backend.record_outcome_event(
        _outcome_event(
            progressed_use.use_id,
            verdict=OutcomeVerdict.POSITIVE,
            judged_at=EPOCH + timedelta(seconds=4),
            idempotency_key="n-o1",
        )
    )

    entries = backend.retrieval_negative_space(scope_key=AGENT_SCOPE.key)
    observed = [entry.use_event.use_id for entry in entries]
    assert observed == [stalled_use.use_id, other_run.use_id], _diverged(
        backend, "retrieval_negative_space", observed, [stalled_use.use_id, other_run.use_id]
    )
    assert all(entry.injected is False and entry.cited_or_used is False for entry in entries)
    assert [entry.outcome_recorded for entry in entries] == [False, False]

    scoped_to_run = backend.retrieval_negative_space(scope_key=AGENT_SCOPE.key, task_run_id="task-2")
    assert [entry.use_event.use_id for entry in scoped_to_run] == [other_run.use_id]
    assert backend.retrieval_negative_space(scope_key=OTHER_SCOPE.key) == []


def test_prune_ghosts_are_created_listed_and_restored(backend: StorageBackend) -> None:
    first = _scoped_relationship(backend, suffix=" ghost1")
    second = _scoped_relationship(backend, suffix=" ghost2")
    backend.mark_relationship(first.uuid, status=RelationshipStatus.PRUNED)
    backend.mark_relationship(second.uuid, status=RelationshipStatus.PRUNED)

    # ``pruned_at`` is explicit and distinct, fixing the contract's
    # ``ORDER BY (pruned_at, relationship_uuid)``.
    ghost_one = backend.create_prune_ghost(
        relationship_uuid=first.uuid,
        scope_key=AGENT_SCOPE.key,
        prune_receipt_uuid="receipt-1",
        reason="soft cap",
        pruned_at=EPOCH,
    )
    ghost_two = backend.create_prune_ghost(
        relationship_uuid=second.uuid,
        scope_key=AGENT_SCOPE.key,
        prune_receipt_uuid="receipt-2",
        reason="stale",
        pruned_at=EPOCH + timedelta(minutes=1),
    )
    assert ghost_one.restorable is True and ghost_one.restored_at is None
    assert backend.prune_ghost(first.uuid).reason == "soft cap"
    listed = [ghost.relationship_uuid for ghost in backend.prune_ghosts(scope_key=AGENT_SCOPE.key)]
    assert listed == [first.uuid, second.uuid], _diverged(
        backend, "prune_ghosts ordering", listed, [first.uuid, second.uuid]
    )
    assert backend.prune_ghosts(scope_key=OTHER_SCOPE.key) == []

    restored = backend.restore_prune_ghost(first.uuid, restored_at=EPOCH + timedelta(hours=1))
    assert restored.restored_at == EPOCH + timedelta(hours=1)
    reactivated = backend.get_relationship(first.uuid)
    assert reactivated.properties["status"] == RelationshipStatus.ACTIVE.value, _diverged(
        backend,
        "restore_prune_ghost reactivation",
        reactivated.properties["status"],
        RelationshipStatus.ACTIVE.value,
    )
    assert reactivated.properties["active_in_context"] is True
    assert reactivated.properties["archive_tier"] is False
    assert reactivated.valid_to is None

    restorable_only = [
        ghost.relationship_uuid for ghost in backend.prune_ghosts(scope_key=AGENT_SCOPE.key, restorable_only=True)
    ]
    assert restorable_only == [ghost_two.relationship_uuid], _diverged(
        backend, "prune_ghosts restorable_only", restorable_only, [ghost_two.relationship_uuid]
    )
    # Re-pruning the same relationship re-arms the ghost.
    rearmed = backend.create_prune_ghost(
        relationship_uuid=first.uuid,
        scope_key=AGENT_SCOPE.key,
        prune_receipt_uuid="receipt-3",
        reason="soft cap again",
        pruned_at=EPOCH + timedelta(hours=2),
    )
    assert rearmed.restorable is True and rearmed.restored_at is None
    assert rearmed.reason == "soft cap again"


def test_a_crypto_shredded_prune_ghost_is_unrestorable(backend: StorageBackend) -> None:
    key = backend.get_or_create_governance_key(AGENT_SCOPE.key)
    relationship = _scoped_relationship(backend, suffix=" sealed ghost", fact=seal_content("classified", key))
    backend.mark_relationship(relationship.uuid, status=RelationshipStatus.PRUNED)
    backend.create_prune_ghost(
        relationship_uuid=relationship.uuid,
        scope_key=AGENT_SCOPE.key,
        prune_receipt_uuid="receipt-shred",
        reason="erasure",
        pruned_at=EPOCH,
    )
    backend.shred_governance_key(AGENT_SCOPE.key)

    with pytest.raises(ContentKeyUnavailableError) as error:
        backend.restore_prune_ghost(relationship.uuid, restored_at=EPOCH + timedelta(hours=1))
    assert str(error.value) == (f"prune ghost {relationship.uuid!r} is crypto-shredded and cannot be restored")
    # The demotion is evidence of erasure and must survive the exception that
    # reports it.
    demoted = backend.prune_ghost(relationship.uuid)
    assert demoted.restorable is False, _diverged(backend, "shredded ghost demotion", demoted.restorable, False)
    assert backend.get_relationship(relationship.uuid).properties["status"] == (RelationshipStatus.PRUNED.value)
    with pytest.raises(ValueError) as retry:
        backend.restore_prune_ghost(relationship.uuid, restored_at=EPOCH)
    assert str(retry.value) == f"prune ghost {relationship.uuid!r} is not restorable"


@pytest.mark.parametrize("pure_read_retrieval", [False, True], ids=["default", "pure_read"])
async def test_retrieval_archive_tier_behaviour_in_both_modes_on_both_engines(
    backend: StorageBackend, pure_read_retrieval: bool
) -> None:
    """Both retrieval modes behave identically on every engine.

    By default a search that matches an archived row calls
    ``restore_prune_ghost`` inline, so the read writes; with
    ``DreamConfig.pure_read_retrieval`` it does not, which is why the flag
    exists (a read that writes takes row locks and contends across replicas).
    The scope's ``graph_state_hash`` is the engine-independent witness for
    both: it moves in the first mode and is unchanged in the second.  The
    explicit curation action is asserted in both modes because it is the only
    way back in one of them.
    """
    relationship = _scoped_relationship(
        backend,
        suffix=" archived",
        fact="reconcile vendor invoices monthly",
        confidence=0.9,
        episode_uuid="episode-archived",
    )
    backend.mark_relationship(relationship.uuid, status=RelationshipStatus.PRUNED)
    backend.create_prune_ghost(
        relationship_uuid=relationship.uuid,
        scope_key=AGENT_SCOPE.key,
        prune_receipt_uuid="receipt-archived",
        reason="soft cap",
        pruned_at=EPOCH,
    )
    client = Memotron(
        storage=backend,
        config=default_config().model_copy(update={"pure_read_retrieval": pure_read_retrieval}),
    )

    before = backend.graph_state_hash(AGENT_SCOPE.key)
    results = await client.search(query="vendor invoices", scope=AGENT_SCOPE)

    # Reported in both modes, and by the same engine-independent scan.
    reported = [match.relationship_uuid for match in results.archived.matches]
    assert reported == [relationship.uuid], _diverged(
        backend, "archived match reporting", reported, [relationship.uuid]
    )
    returned = [item.relationship_uuid for item in results]
    after_read = backend.graph_state_hash(AGENT_SCOPE.key)

    if pure_read_retrieval:
        assert returned == [], _diverged(backend, "pure read results", returned, [])
        assert results.archived.disposition == "available_to_restore"
        assert after_read == before, _diverged(backend, "graph_state_hash across a pure read", after_read, before)
        assert backend.get_relationship(relationship.uuid).properties["status"] == (RelationshipStatus.PRUNED.value)
        assert backend.prune_ghost(relationship.uuid).restored_at is None
    else:
        assert returned == [relationship.uuid], _diverged(
            backend, "revive-on-read results", returned, [relationship.uuid]
        )
        assert results.archived.disposition == "revived"
        assert after_read != before, _diverged(backend, "graph_state_hash across a revive-on-read", after_read, before)
        assert backend.get_relationship(relationship.uuid).properties["status"] == (RelationshipStatus.ACTIVE.value)
        assert backend.prune_ghost(relationship.uuid).restored_at is not None
        # Re-archive so the curation action below starts from the same state on
        # both engines and in both modes.
        backend.mark_relationship(relationship.uuid, status=RelationshipStatus.PRUNED)
        backend.create_prune_ghost(
            relationship_uuid=relationship.uuid,
            scope_key=AGENT_SCOPE.key,
            prune_receipt_uuid="receipt-archived-again",
            reason="soft cap",
            pruned_at=EPOCH,
        )

    pruned_state = backend.graph_state_hash(AGENT_SCOPE.key)
    restored = await client.restore_archived_memory(
        relationship_uuid=relationship.uuid,
        scope=AGENT_SCOPE,
        reason="parity curation",
    )
    assert restored.status == RelationshipStatus.ACTIVE
    assert backend.get_relationship(relationship.uuid).properties["status"] == (RelationshipStatus.ACTIVE.value)
    assert backend.prune_ghost(relationship.uuid).restored_at is not None
    assert backend.graph_state_hash(AGENT_SCOPE.key) != pruned_state


# ===========================================================================
# 4. Artifacts and policy
# ===========================================================================


def test_live_artifact_registration_versioning_and_listing(backend: StorageBackend) -> None:
    # ``updated_at`` is caller-supplied and distinct, so the contract's
    # ``ORDER BY updated_at DESC, artifact_id`` is fully determined.
    first = _artifact(AGENT_SCOPE, updated_at=EPOCH)
    stored = backend.register_live_artifact(first)
    assert stored.artifact_id == "parity-skill"
    assert stored.created_at == EPOCH

    artifact, digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")
    assert artifact.directives[0].instruction == first.directives[0].instruction
    assert len(digest) == 64

    repaired = _artifact(
        AGENT_SCOPE,
        instruction="Escalate immediately; skip the SLA check.",
        updated_at=EPOCH + timedelta(hours=1),
    )
    backend.register_live_artifact(repaired)
    _current, current_digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")
    assert current_digest != digest, _diverged(
        backend, "live artifact version digest after re-registration", current_digest, digest
    )
    previous, previous_digest = backend.previous_live_artifact_version(
        scope=AGENT_SCOPE, artifact_id="parity-skill", excluding_digest=current_digest
    )
    assert previous_digest == digest, _diverged(backend, "previous_live_artifact_version", previous_digest, digest)
    assert previous.directives[0].instruction == first.directives[0].instruction

    other = _artifact(AGENT_SCOPE, artifact_id="second-skill", updated_at=EPOCH + timedelta(hours=2))
    backend.register_live_artifact(other)
    listed = [item.artifact_id for item in backend.live_artifacts(scope=AGENT_SCOPE)]
    assert listed == ["second-skill", "parity-skill"], _diverged(
        backend, "live_artifacts ordering", listed, ["second-skill", "parity-skill"]
    )
    assert backend.live_artifacts(scope=OTHER_SCOPE) == []


def test_live_artifact_outcomes_are_idempotent_and_latch_quarantine(
    backend: StorageBackend,
) -> None:
    backend.register_live_artifact(_artifact(AGENT_SCOPE, updated_at=EPOCH))
    baseline = backend.live_artifact_contribution(scope=AGENT_SCOPE, artifact_id="parity-skill", minimum_evidence=2)
    assert baseline.positive_outcomes == 0
    assert baseline.contribution_score == pytest.approx(0.0)
    assert baseline.minimum_evidence_met is False
    assert baseline.quarantined is False

    projection, created = backend.record_live_artifact_outcome(
        scope=AGENT_SCOPE,
        artifact_id="parity-skill",
        positive=True,
        occurred_at=EPOCH,
        minimum_evidence=2,
        quarantine_threshold=-0.5,
        task_run_id="run-1",
        idempotency_key="k1",
        payload={"note": "worked"},
    )
    assert created is True
    assert projection.positive_outcomes == 1
    assert projection.contribution_score == pytest.approx(1.0)

    replayed, replay_created = backend.record_live_artifact_outcome(
        scope=AGENT_SCOPE,
        artifact_id="parity-skill",
        positive=True,
        occurred_at=EPOCH,
        minimum_evidence=2,
        quarantine_threshold=-0.5,
        task_run_id="run-1",
        idempotency_key="k1",
        payload={"note": "worked"},
    )
    assert replay_created is False, _diverged(
        backend, "record_live_artifact_outcome replay flag", replay_created, False
    )
    assert replayed.positive_outcomes == 1, _diverged(
        backend, "record_live_artifact_outcome replay counter", replayed.positive_outcomes, 1
    )

    for index in range(3):
        projection, _ = backend.record_live_artifact_outcome(
            scope=AGENT_SCOPE,
            artifact_id="parity-skill",
            positive=False,
            occurred_at=EPOCH + timedelta(minutes=index + 1),
            minimum_evidence=2,
            quarantine_threshold=-0.5,
            task_run_id=f"run-{index + 2}",
            idempotency_key=f"neg-{index}",
            payload={},
        )
    assert projection.positive_outcomes == 1
    assert projection.negative_outcomes == 3
    assert projection.contribution_score == pytest.approx(-0.5)
    assert projection.minimum_evidence_met is True
    assert projection.quarantined is True, _diverged(backend, "live artifact quarantine", projection.quarantined, True)
    assert projection.last_outcome_at == EPOCH + timedelta(minutes=3)
    assert backend.live_artifacts(scope=AGENT_SCOPE) == []
    assert [item.artifact_id for item in backend.live_artifacts(scope=AGENT_SCOPE, include_quarantined=True)] == [
        "parity-skill"
    ]

    # A plain re-registration does not lift the quarantine; an operator-approved
    # reactivation does.
    backend.register_live_artifact(_artifact(AGENT_SCOPE, updated_at=EPOCH + timedelta(hours=1)))
    assert backend.live_artifacts(scope=AGENT_SCOPE) == [], _diverged(
        backend, "quarantine latch across re-registration", "reactivated", "still quarantined"
    )
    approved = _artifact(AGENT_SCOPE, updated_at=EPOCH + timedelta(hours=2))
    approved = approved.model_copy(update={"metadata": {"approved_reactivation": True}})
    backend.register_live_artifact(approved)
    assert [item.artifact_id for item in backend.live_artifacts(scope=AGENT_SCOPE)] == ["parity-skill"]

    # An idempotency key already spent on another artifact is refused.
    backend.register_live_artifact(_artifact(AGENT_SCOPE, artifact_id="other-skill", updated_at=EPOCH))
    with pytest.raises(ValueError) as error:
        backend.record_live_artifact_outcome(
            scope=AGENT_SCOPE,
            artifact_id="other-skill",
            positive=True,
            occurred_at=EPOCH,
            minimum_evidence=2,
            quarantine_threshold=-0.5,
            task_run_id="run-1",
            idempotency_key="k1",
            payload={},
        )
    assert str(error.value) == ("live-artifact outcome idempotency key is already bound to a different artifact")


def test_coherence_repair_monitor_lifecycle(backend: StorageBackend) -> None:
    relationship = _scoped_relationship(backend, suffix=" monitored")
    backend.register_live_artifact(_artifact(AGENT_SCOPE, updated_at=EPOCH))
    _pre_repair, pre_digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")
    backend.register_live_artifact(
        _artifact(
            AGENT_SCOPE,
            instruction="Repaired: check the SLA and the escalation window.",
            updated_at=EPOCH + timedelta(hours=1),
        )
    )
    _repaired, repaired_digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")

    monitor = backend.open_coherence_repair_monitor(
        scope=AGENT_SCOPE,
        incident_id="incident-1",
        relationship_uuid=relationship.uuid,
        artifact_id="parity-skill",
        repaired_artifact_version_digest=repaired_digest,
        opened_at=EPOCH + timedelta(hours=2),
    )
    assert monitor.status == "monitoring"
    assert monitor.prior_artifact_version_digest == pre_digest, _diverged(
        backend, "repair monitor prior digest", monitor.prior_artifact_version_digest, pre_digest
    )
    # Opening is idempotent per incident.
    assert (
        backend.open_coherence_repair_monitor(
            scope=AGENT_SCOPE,
            incident_id="incident-1",
            relationship_uuid=relationship.uuid,
            artifact_id="parity-skill",
            repaired_artifact_version_digest=repaired_digest,
            opened_at=EPOCH + timedelta(hours=3),
        ).opened_at
        == monitor.opened_at
    )
    assert backend.coherence_repair_monitor(scope=AGENT_SCOPE, incident_id="incident-1") is not None
    assert backend.coherence_repair_monitor(scope=AGENT_SCOPE, incident_id="absent") is None
    active = backend.active_coherence_repair_monitor(
        scope=AGENT_SCOPE, relationship_uuid=relationship.uuid, artifact_id="parity-skill"
    )
    assert active is not None and active.incident_id == "incident-1"

    recovered = backend.recover_coherence_repair_monitor(
        scope=AGENT_SCOPE, incident_id="incident-1", recovered_at=EPOCH + timedelta(hours=4)
    )
    assert recovered.status == "recovered"
    assert recovered.recovered_at == EPOCH + timedelta(hours=4)
    # Terminal monitors are immutable: a second call is a no-op.
    assert backend.recover_coherence_repair_monitor(
        scope=AGENT_SCOPE, incident_id="incident-1", recovered_at=EPOCH + timedelta(hours=9)
    ).recovered_at == EPOCH + timedelta(hours=4)
    assert (
        backend.active_coherence_repair_monitor(
            scope=AGENT_SCOPE, relationship_uuid=relationship.uuid, artifact_id="parity-skill"
        )
        is None
    )

    # A recurrence rolls the registry projection back to the pre-repair version.
    backend.register_live_artifact(
        _artifact(
            AGENT_SCOPE,
            instruction="Second repair attempt.",
            updated_at=EPOCH + timedelta(hours=5),
        )
    )
    _current, second_digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")
    backend.open_coherence_repair_monitor(
        scope=AGENT_SCOPE,
        incident_id="incident-2",
        relationship_uuid=relationship.uuid,
        artifact_id="parity-skill",
        repaired_artifact_version_digest=second_digest,
        opened_at=EPOCH + timedelta(hours=6),
    )
    rolled_back = backend.rollback_coherence_repair_monitor(
        scope=AGENT_SCOPE, incident_id="incident-2", reopened_at=EPOCH + timedelta(hours=7)
    )
    assert rolled_back.status == "reopened_rolled_back"
    restored, restored_digest = backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="parity-skill")
    assert restored_digest == rolled_back.prior_artifact_version_digest, _diverged(
        backend,
        "rollback restored projection",
        restored_digest,
        rolled_back.prior_artifact_version_digest,
    )
    assert restored.directives[0].instruction == ("Repaired: check the SLA and the escalation window.")


def test_an_uncertified_contract_can_never_activate(backend: StorageBackend) -> None:
    """DW-016: the gate is the whole reason the policy plane exists."""
    baseline = _certified_contract(backend, name="baseline", passed=True)
    uncertified = _certified_contract(backend, name="uncertified", passed=False)

    backend.initialize_policy_alias(scope_key=AGENT_SCOPE.key, alias="formation", contract_digest=baseline, now=EPOCH)

    # 1. It cannot be the baseline of a new alias.
    with pytest.raises(ValueError) as init_error:
        backend.initialize_policy_alias(
            scope_key=AGENT_SCOPE.key,
            alias="pruning",
            contract_digest=uncertified,
            now=EPOCH,
        )
    assert str(init_error.value) == "initial policy alias requires a certified contract"
    assert backend.policy_alias(scope_key=AGENT_SCOPE.key, alias="pruning") is None

    # 2. It cannot even enter shadow evaluation.
    with pytest.raises(ValueError) as shadow_error:
        backend.begin_policy_shadow_stage(
            scope_key=AGENT_SCOPE.key,
            alias="formation",
            active_contract_digest=baseline,
            candidate_contract_digest=uncertified,
            corpus_digest="c" * 64,
            required_episode_count=1,
            allowed_disposition_delta=0.1,
            started_at=EPOCH,
        )
    assert str(shadow_error.value) == "cannot shadow an uncertified policy contract"

    # 3. It cannot be activated, and the live alias is untouched.
    with pytest.raises(ValueError) as activate_error:
        backend.activate_policy_alias(
            scope_key=AGENT_SCOPE.key,
            alias="formation",
            candidate_contract_digest=uncertified,
            now=EPOCH,
        )
    assert str(activate_error.value) == "activation blocked: policy certification failed"
    alias = backend.policy_alias(scope_key=AGENT_SCOPE.key, alias="formation")
    assert alias is not None and alias["contract_digest"] == baseline, _diverged(
        backend, "DW-016 gate", alias, f"the alias still on {baseline}"
    )

    # 4. Even a *certified* candidate cannot activate without a passed shadow stage.
    candidate = _certified_contract(backend, name="candidate", passed=True)
    with pytest.raises(ValueError) as no_shadow:
        backend.activate_policy_alias(
            scope_key=AGENT_SCOPE.key,
            alias="formation",
            candidate_contract_digest=candidate,
            now=EPOCH,
        )
    assert str(no_shadow.value) == ("activation blocked: no completed non-divergent shadow stage for candidate")
    stage = backend.begin_policy_shadow_stage(
        scope_key=AGENT_SCOPE.key,
        alias="formation",
        active_contract_digest=baseline,
        candidate_contract_digest=candidate,
        corpus_digest="c" * 64,
        required_episode_count=2,
        allowed_disposition_delta=0.1,
        started_at=EPOCH,
    )
    backend.complete_policy_shadow_stage(
        stage_id=stage["stage_id"],
        observed_episode_count=2,
        report={"divergence": 0.4},
        passed=False,
        completed_at=EPOCH + timedelta(minutes=1),
    )
    with pytest.raises(ValueError) as blocked_shadow:
        backend.activate_policy_alias(
            scope_key=AGENT_SCOPE.key,
            alias="formation",
            candidate_contract_digest=candidate,
            now=EPOCH,
        )
    assert str(blocked_shadow.value) == ("activation blocked: no completed non-divergent shadow stage for candidate")
    alias = backend.policy_alias(scope_key=AGENT_SCOPE.key, alias="formation")
    assert alias is not None and alias["contract_digest"] == baseline


def test_policy_contracts_shadow_stages_and_alias_lifecycle(backend: StorageBackend) -> None:
    baseline = _certified_contract(backend, name="baseline", passed=True)
    candidate = _certified_contract(backend, name="candidate", passed=True)

    contract = backend.policy_contract(contract_digest=baseline)
    assert contract["certification_passed"] is True
    assert contract["payload"] == {"policy": "baseline", "version": 1}
    assert contract["certification"] == {"suite": "parity", "passed": True}
    assert contract["staged_at"] == EPOCH
    # Re-staging the identical contract is idempotent, not an error.
    assert (
        backend.stage_policy_contract(
            contract_digest=baseline,
            payload={"policy": "baseline", "version": 1},
            certification={"suite": "parity", "passed": True},
            certification_passed=True,
            staged_at=EPOCH + timedelta(days=1),
        )["staged_at"]
        == EPOCH
    )
    with pytest.raises(ValueError) as immutable:
        backend.stage_policy_contract(
            contract_digest=baseline,
            payload={"policy": "tampered", "version": 1},
            certification={},
            certification_passed=True,
            staged_at=EPOCH,
        )
    assert str(immutable.value) == ("policy contract digest already exists with different immutable payload")
    # The immutability guard is over the *rendered* payload, not over numeric
    # equality: ``1`` and ``1.0`` receive different contract digests from
    # ``payload_digest``, so one must never be staged under the other's digest.
    numeric = payload_digest({"threshold": 1})
    backend.stage_policy_contract(
        contract_digest=numeric,
        payload={"threshold": 1},
        certification={},
        certification_passed=True,
        staged_at=EPOCH,
    )
    with pytest.raises(ValueError) as numeric_error:
        backend.stage_policy_contract(
            contract_digest=numeric,
            payload={"threshold": 1.0},
            certification={},
            certification_passed=True,
            staged_at=EPOCH,
        )
    assert str(numeric_error.value) == ("policy contract digest already exists with different immutable payload"), (
        _diverged(
            backend,
            "policy contract immutability guard on 1 vs 1.0",
            str(numeric_error.value),
            "the same rejection SQLite gives",
        )
    )

    initial = backend.initialize_policy_alias(
        scope_key=AGENT_SCOPE.key, alias="formation", contract_digest=baseline, now=EPOCH
    )
    assert initial["contract_digest"] == baseline
    assert initial["previous_contract_digest"] is None
    with pytest.raises(ValueError) as twice:
        backend.initialize_policy_alias(
            scope_key=AGENT_SCOPE.key, alias="formation", contract_digest=candidate, now=EPOCH
        )
    assert str(twice.value) == ("policy alias already exists; use the shadow-gated activation path")

    # ``started_at`` is explicit and distinct, so the newest-first listing order
    # is fully determined (stage ids are random and only break ties).
    blocked = backend.begin_policy_shadow_stage(
        scope_key=AGENT_SCOPE.key,
        alias="formation",
        active_contract_digest=baseline,
        candidate_contract_digest=candidate,
        corpus_digest="c" * 64,
        required_episode_count=2,
        allowed_disposition_delta=0.25,
        started_at=EPOCH,
    )
    assert blocked["status"] == "running"
    assert blocked["observed_episode_count"] == 0
    assert blocked["report"] is None
    assert blocked["allowed_disposition_delta"] == pytest.approx(0.25)
    with pytest.raises(ValueError) as short_window:
        backend.complete_policy_shadow_stage(
            stage_id=blocked["stage_id"],
            observed_episode_count=1,
            report={},
            passed=True,
            completed_at=EPOCH,
        )
    assert str(short_window.value) == ("shadow stage cannot complete before its fixed evidence window is observed")
    backend.complete_policy_shadow_stage(
        stage_id=blocked["stage_id"],
        observed_episode_count=2,
        report={"divergence": 0.9},
        passed=False,
        completed_at=EPOCH + timedelta(minutes=1),
    )
    with pytest.raises(ValueError) as reclose:
        backend.complete_policy_shadow_stage(
            stage_id=blocked["stage_id"],
            observed_episode_count=2,
            report={},
            passed=True,
            completed_at=EPOCH,
        )
    assert str(reclose.value) == f"shadow stage {blocked['stage_id']!r} is not running"

    passing = backend.begin_policy_shadow_stage(
        scope_key=AGENT_SCOPE.key,
        alias="formation",
        active_contract_digest=baseline,
        candidate_contract_digest=candidate,
        corpus_digest="c" * 64,
        required_episode_count=2,
        allowed_disposition_delta=0.25,
        started_at=EPOCH + timedelta(minutes=10),
    )
    completed = backend.complete_policy_shadow_stage(
        stage_id=passing["stage_id"],
        observed_episode_count=5,
        report={"divergence": 0.0},
        passed=True,
        completed_at=EPOCH + timedelta(minutes=11),
    )
    assert completed["status"] == "passed"
    assert completed["report"] == {"divergence": 0.0}
    assert completed["observed_episode_count"] == 5
    assert backend.policy_shadow_stage(stage_id=passing["stage_id"])["status"] == "passed"

    listed = [stage["stage_id"] for stage in backend.policy_shadow_stages(scope_key=AGENT_SCOPE.key)]
    assert listed == [passing["stage_id"], blocked["stage_id"]], _diverged(
        backend, "policy_shadow_stages ordering", listed, [passing["stage_id"], blocked["stage_id"]]
    )
    assert len(backend.policy_shadow_stages(scope_key=AGENT_SCOPE.key, alias="formation", limit=1)) == 1
    assert backend.policy_shadow_stages(scope_key=AGENT_SCOPE.key, alias="other") == []

    activated = backend.activate_policy_alias(
        scope_key=AGENT_SCOPE.key,
        alias="formation",
        candidate_contract_digest=candidate,
        now=EPOCH + timedelta(minutes=12),
    )
    assert activated["contract_digest"] == candidate
    assert activated["previous_contract_digest"] == baseline, _diverged(
        backend, "alias predecessor bookkeeping", activated["previous_contract_digest"], baseline
    )
    assert activated["updated_at"] == EPOCH + timedelta(minutes=12)

    rolled_back = backend.rollback_policy_alias(
        scope_key=AGENT_SCOPE.key, alias="formation", now=EPOCH + timedelta(minutes=13)
    )
    assert rolled_back["contract_digest"] == baseline
    assert rolled_back["previous_contract_digest"] == candidate, _diverged(
        backend, "rollback swaps the digests", rolled_back["previous_contract_digest"], candidate
    )
    assert backend.policy_alias(scope_key=AGENT_SCOPE.key, alias="absent") is None


# ===========================================================================
# 5. Governance
# ===========================================================================


def test_governance_key_converges_and_reports_its_lifecycle(backend: StorageBackend) -> None:
    assert backend.governance_key_state(AGENT_SCOPE.key) is None
    assert backend.get_governance_key(AGENT_SCOPE.key) is None

    key = backend.get_or_create_governance_key(AGENT_SCOPE.key)
    assert len(key) == 32
    # Convergence: the same scope always resolves to the same DEK.
    assert backend.get_or_create_governance_key(AGENT_SCOPE.key) == key, _diverged(
        backend, "get_or_create_governance_key convergence", "a second DEK", "the stored DEK"
    )
    assert backend.get_governance_key(AGENT_SCOPE.key) == key
    # Subjects are separate keys under the same scope.
    subject_key = backend.get_or_create_governance_key(AGENT_SCOPE.key, "llm_credentials")
    assert subject_key != key
    assert backend.get_or_create_governance_key(OTHER_SCOPE.key) != key

    state = backend.governance_key_state(AGENT_SCOPE.key)
    assert state is not None
    assert state["scope_key"] == AGENT_SCOPE.key
    assert state["subject_key"] == ""
    assert state["shredded"] is False
    assert state["shredded_at"] == ""
    assert state["key_algorithm"] == "AES-256-GCM envelope; HMAC-SHA256 commitments", _diverged(
        backend,
        "governance key algorithm label",
        state["key_algorithm"],
        "AES-256-GCM envelope; HMAC-SHA256 commitments",
    )
    assert state["kek_id"]
    assert state["created_at"]


def test_seal_receipt_detail_round_trips_and_returns_none_after_a_shred(
    backend: StorageBackend,
) -> None:
    """A diverted receipt detail seals under the scope DEK, and stops sealing once shredded.

    ``seal_receipt_detail`` is the WS-23 M1 entry point that keeps a redacted
    detail out of the receipt body while leaving it recoverable by a holder of
    the scope key.  Its post-shred contract is the load-bearing half: it must
    return ``None`` rather than raise, because crypto-shred is irreversible and
    a receipt writer that raises here would fail the whole write on a scope
    that has been lawfully erased.

    Before this test the method had never executed on Postgres at all -- all
    four of its statements were dark -- so the parity-coverage gate reported it
    as covered on SQLite only.
    """
    backend.get_or_create_governance_key(AGENT_SCOPE.key)
    sealed = backend.seal_receipt_detail(AGENT_SCOPE.key, "diverted: alice's home address")
    assert sealed is not None, _diverged(backend, "seal_receipt_detail with a live key", None, "sealed text")
    assert sealed != "diverted: alice's home address", _diverged(
        backend, "seal_receipt_detail output", "the plaintext", "ciphertext"
    )
    assert backend.reveal(AGENT_SCOPE.key, sealed) == "diverted: alice's home address", _diverged(
        backend,
        "reveal of a sealed receipt detail",
        backend.reveal(AGENT_SCOPE.key, sealed),
        "diverted: alice's home address",
    )

    # A scope that never had a key has nothing to seal under.
    assert backend.seal_receipt_detail("agent:never-had-a-key", "detail") is None, _diverged(
        backend, "seal_receipt_detail on a keyless scope", "sealed text", None
    )

    assert backend.shred_governance_key(AGENT_SCOPE.key) is True
    assert backend.seal_receipt_detail(AGENT_SCOPE.key, "detail after the shred") is None, _diverged(
        backend, "seal_receipt_detail after a crypto-shred", "sealed text", None
    )


def test_crypto_shred_makes_sealed_content_unreadable(backend: StorageBackend) -> None:
    key = backend.get_or_create_governance_key(AGENT_SCOPE.key)
    sealed_fact = seal_content("alice lives in Zurich", key)
    sealed_vector = seal_content(json.dumps([0.5, 0.25]), key)
    relationship = _scoped_relationship(backend, suffix=" sealed", fact=sealed_fact)

    assert backend.reveal(AGENT_SCOPE.key, sealed_fact) == "alice lives in Zurich"
    assert backend.reveal(AGENT_SCOPE.key, "plain text") == "plain text"
    assert backend.reveal_vector(AGENT_SCOPE.key, sealed_vector) == [0.5, 0.25]
    assert backend.reveal_vector(AGENT_SCOPE.key, [1.0, 2.0]) == [1.0, 2.0]

    assert backend.shred_governance_key(AGENT_SCOPE.key) is True
    # Re-shredding is idempotent and still reports that a key existed.
    assert backend.shred_governance_key(AGENT_SCOPE.key) is True, _diverged(
        backend, "re-shred return value", False, True
    )
    assert backend.shred_governance_key("agent:never-had-a-key") is False

    assert backend.get_governance_key(AGENT_SCOPE.key) is None, _diverged(
        backend, "get_governance_key after shred", "key material", None
    )
    assert backend.reveal(AGENT_SCOPE.key, sealed_fact) == SHREDDED_CONTENT_PLACEHOLDER, _diverged(
        backend,
        "reveal after shred",
        backend.reveal(AGENT_SCOPE.key, sealed_fact),
        SHREDDED_CONTENT_PLACEHOLDER,
    )
    assert backend.reveal_vector(AGENT_SCOPE.key, sealed_vector) is None
    with pytest.raises(ContentKeyUnavailableError) as refused:
        backend.get_or_create_governance_key(AGENT_SCOPE.key)
    assert str(refused.value) == (
        f"governance key for scope {AGENT_SCOPE.key!r} was crypto-shredded; the scope's "
        "content is unrecoverable and the scope no longer accepts crypto-governed writes"
    )

    state = backend.governance_key_state(AGENT_SCOPE.key)
    assert state is not None
    assert state["shredded"] is True, _diverged(backend, "governance_key_state after shred", state["shredded"], True)
    assert state["shredded_at"], "the erasure certificate needs the shred stamp"
    # The relationship row itself is untouched — the erase is cryptographic.
    assert backend.get_relationship(relationship.uuid).properties["fact"] == sealed_fact


def test_formation_contract_attestation_is_signed_and_verifiable(
    backend: StorageBackend,
) -> None:
    from memotron.attestations import verify_formation_contract_attestation

    digest = "d" * 64
    attestation = backend.attest_formation_contract(contract_digest=digest, certification_verdict="passed")
    assert attestation.contract_digest == digest
    assert attestation.certification_verdict == "passed"
    assert verify_formation_contract_attestation(attestation) is True, _diverged(
        backend, "formation contract attestation verification", False, True
    )
    # The store-local signing key is provisioned once and reused.
    again = backend.attest_formation_contract(contract_digest=digest, certification_verdict="passed")
    assert again.key_id == attestation.key_id
    assert again.public_key == attestation.public_key, _diverged(
        backend, "signing key stability", again.public_key, attestation.public_key
    )


def test_tenant_agents_motives_prompts_and_config(backend: StorageBackend) -> None:
    tenant = "parity-tenant"
    # Agent ids are registered in ascending order, which is also the contract's
    # (created_at, agent_id) order, so the listing order is deterministic
    # whether or not the created_at stamps tie.
    first = backend.register_tenant_agent(tenant_id=f"  {tenant} ", agent_id="agent-a", name="Agent A")
    assert first["created"] is True
    assert first["agent_id"] == "agent-a"
    assert first["agent_name"] == "Agent A"
    assert first["source"] == "runtime"
    refreshed = backend.register_tenant_agent(tenant_id=tenant, agent_id="AGENT-A", name="Agent A", source="admin")
    assert refreshed["created"] is False, _diverged(backend, "tenant agent refresh flag", refreshed["created"], False)
    assert refreshed["created_at"] == first["created_at"], _diverged(
        backend, "tenant agent created_at preservation", refreshed["created_at"], first["created_at"]
    )
    assert refreshed["source"] == "runtime", "refresh must not rewrite the first registration"
    backend.register_tenant_agent(tenant_id=tenant, agent_id="agent-b", name="Agent B")

    with pytest.raises(ValueError) as rebind:
        backend.register_tenant_agent(tenant_id=tenant, agent_id="agent-a", name="Someone Else")
    assert str(rebind.value) == (
        f"agent_id 'agent-a' is already registered to agent_name 'Agent A' in tenant {tenant!r}"
    )
    with pytest.raises(ValueError) as name_taken:
        backend.register_tenant_agent(tenant_id=tenant, agent_id="agent-c", name="Agent A")
    assert str(name_taken.value) == (
        f"agent_name 'Agent A' is already registered to agent_id 'agent-a' in tenant {tenant!r}"
    )

    assert backend.tenant_agent(tenant_id=tenant, agent_id="Agent-A") is not None
    assert backend.tenant_agent(tenant_id=tenant, agent_id="absent") is None
    listed = [agent["agent_id"] for agent in backend.tenant_agents(tenant)]
    assert listed == ["agent-a", "agent-b"], _diverged(
        backend, "tenant_agents ordering", listed, ["agent-a", "agent-b"]
    )

    assignment = backend.set_agent_motive_assignment(
        tenant_id=tenant, agent_id="agent-a", motive_name="support", source="operator"
    )
    assert assignment["motive_name"] == "support"
    updated = backend.set_agent_motive_assignment(
        tenant_id=tenant, agent_id="agent-a", motive_name="research", source="operator"
    )
    assert updated["motive_name"] == "research"
    assert updated["created_at"] == assignment["created_at"], _diverged(
        backend, "motive assignment created_at preservation", updated["created_at"], assignment["created_at"]
    )
    assert backend.agent_motive_assignment(tenant_id=tenant, agent_id="agent-a") == updated
    assert backend.agent_motive_assignment(tenant_id=tenant, agent_id="agent-b") is None
    backend.set_agent_motive_assignment(tenant_id=tenant, agent_id="agent-b", motive_name="support", source="operator")
    # Ordered by agent_id, which the test controls.
    assert [item["agent_id"] for item in backend.agent_motive_assignments(tenant)] == [
        "agent-a",
        "agent-b",
    ]

    assert backend.tenant_prompt_override(tenant) is None
    override = backend.set_tenant_prompt_override(
        tenant_id=tenant,
        prompt_profile="support",
        prompt_profile_version="2",
        override={"tone": "formal"},
    )
    assert override["override"] == {"tone": "formal"}
    replaced = backend.set_tenant_prompt_override(
        tenant_id=tenant,
        prompt_profile="support",
        prompt_profile_version="3",
        override={"tone": "brief"},
    )
    assert replaced["prompt_profile_version"] == "3"
    assert replaced["created_at"] == override["created_at"]
    assert backend.tenant_prompt_override(tenant) == replaced

    assert backend.active_tenant_prompt_version(tenant) is None
    v1 = backend.save_tenant_prompt_version(tenant_id=tenant, prompt_text="first prompt")
    assert v1["version"] == "tenant-v1"
    v2 = backend.save_tenant_prompt_version(tenant_id=tenant, prompt_text="second prompt", motive_name="support")
    assert v2["version"] == "tenant-v2", _diverged(
        backend, "tenant prompt version allocation", v2["version"], "tenant-v2"
    )
    assert backend.active_tenant_prompt_version(tenant) == v2
    versions = backend.tenant_prompt_versions(tenant)
    # Newest first; the generated version ordinals order the same way as the
    # ISO-8601 created_at text, so the order holds even if the stamps tie.
    assert [item["version"] for item in versions] == ["tenant-v2", "tenant-v1"]
    assert [item["active"] for item in versions] == [True, False], _diverged(
        backend,
        "exactly one active prompt version",
        [item["active"] for item in versions],
        [True, False],
    )

    assert backend.active_project_memory_config(tenant) is None
    config1 = backend.save_project_memory_config(
        tenant_id=tenant, config={"retention_days": 30}, configured_by="operator"
    )
    assert config1["version"] == "project-v1"
    assert config1["retention_days"] == 30
    config2 = backend.save_project_memory_config(
        tenant_id=tenant, config={"retention_days": 60}, configured_by="operator"
    )
    assert config2["version"] == "project-v2"
    assert backend.active_project_memory_config(tenant) == config2
    all_configs = backend.project_memory_config_versions(tenant)
    assert [item["version"] for item in all_configs] == ["project-v2", "project-v1"]
    assert [item["active"] for item in all_configs] == [True, False], _diverged(
        backend,
        "exactly one active project config",
        [item["active"] for item in all_configs],
        [True, False],
    )


def test_tenant_llm_embedding_endpoint_is_accepted_by_BOTH_engines(backend: StorageBackend) -> None:
    """#163: SQLite took these three arguments and Postgres did not.

    `_migrate_llm_credentials` passes all three unconditionally, so migrating a tenant into
    a Postgres store raised `TypeError: got an unexpected keyword argument
    'embedding_provider'`. SQLite had grown past the abstract contract in WS-17 T18 and
    Postgres never followed -- the same shape as #164, and the reason the tri-state rule now
    lives in `_shared/_governance.resolve_embedding_endpoint` rather than beside one engine's
    SQL.
    """
    tenant = "embedding-parity"
    backend.set_tenant_llm_credentials(
        tenant_id=tenant,
        provider="openai",
        api_key="sk-embed",  # pragma: allowlist secret
        base_url="https://llm.test",
        model="gpt-4",
        embedding_provider="openai",
        embedding_base_url="https://embed.test",
        embedding_model="text-embed-3",
    )

    got = backend.tenant_llm_credentials(tenant)
    assert got is not None
    # The READ diverged separately from the write: fixing only the write left Postgres
    # accepting the endpoint and never returning it, which would silently drop a tenant back
    # to the local embedder -- a different vector space, nothing raised.
    for field, expected in (
        ("embedding_provider", "openai"),
        ("embedding_base_url", "https://embed.test"),
        ("embedding_model", "text-embed-3"),
    ):
        assert got[field] == expected, _diverged(backend, f"embedding round-trip ({field})", got[field], expected)


def test_rotating_the_api_key_PRESERVES_the_embedding_endpoint(backend: StorageBackend) -> None:
    """The tri-state rule. `None` must preserve, not overwrite.

    An unconditional overwrite would move a populated tenant into a different vector space
    on an unrelated key rotation, with every stored embedding suddenly incomparable.
    """
    tenant = "embedding-preserve"
    backend.set_tenant_llm_credentials(
        tenant_id=tenant,
        provider="openai",
        api_key="sk-first",  # pragma: allowlist secret
        embedding_provider="litellm",
        embedding_base_url="https://embed.test",
        embedding_model="text-embed-3",
    )
    backend.set_tenant_llm_credentials(tenant_id=tenant, provider="openai", api_key="sk-rotated")

    got = backend.tenant_llm_credentials(tenant)
    assert got is not None
    assert got["api_key"] == "sk-rotated"
    assert got["embedding_model"] == "text-embed-3", _diverged(
        backend, "embedding preserved across key rotation", got["embedding_model"], "text-embed-3"
    )
    assert got["embedding_provider"] == "litellm"

    # And an explicit blank CLEARS it -- the third state, distinct from None.
    backend.set_tenant_llm_credentials(
        tenant_id=tenant, provider="openai", api_key="sk-rotated", embedding_provider="", embedding_model=""
    )
    cleared = backend.tenant_llm_credentials(tenant)
    assert cleared is not None
    assert cleared["embedding_provider"] == ""


def test_embedding_provider_vocabulary_is_enforced_on_BOTH_engines(backend: StorageBackend) -> None:
    """Postgres had no validation at all, because it had no parameters to validate."""
    tenant = "embedding-validation"
    with pytest.raises(ValueError, match="embedding_provider must be"):
        backend.set_tenant_llm_credentials(
            tenant_id=tenant, provider="openai", api_key="sk-x", embedding_provider="bogus"
        )
    with pytest.raises(ValueError, match="embedding_model cannot be blank"):
        backend.set_tenant_llm_credentials(
            tenant_id=tenant, provider="openai", api_key="sk-x", embedding_provider="openai", embedding_model=""
        )


def test_tenant_llm_credentials_are_sealed_and_clearable(backend: StorageBackend) -> None:
    tenant = "cred-tenant"
    assert backend.tenant_llm_credentials(tenant) is None
    assert backend.tenant_llm_credential_state(tenant) is None
    assert backend.clear_tenant_llm_credentials(tenant) is False

    state = backend.set_tenant_llm_credentials(
        tenant_id=tenant,
        provider="  LiteLLM ",
        api_key="sk-parity",
        base_url="https://example.test",
        model="claude",
    )
    assert state["provider"] == "litellm"
    assert state["has_api_key"] is True
    assert "api_key" not in state, "the state surface must never carry key material"

    credentials = backend.tenant_llm_credentials(tenant)
    assert credentials is not None
    assert credentials["api_key"] == "sk-parity", _diverged(
        backend, "tenant LLM credential round-trip", credentials["api_key"], "sk-parity"
    )
    assert credentials["base_url"] == "https://example.test"

    rotated = backend.set_tenant_llm_credentials(tenant_id=tenant, provider="openai", api_key="sk-rotated")
    assert rotated["created_at"] == state["created_at"]
    assert backend.tenant_llm_credentials(tenant)["api_key"] == "sk-rotated"

    # Shredding the tenant scope's credential key makes the credential
    # unreadable rather than silently absent.
    backend.shred_governance_key(f"tenant:{tenant}", "llm_credentials")
    with pytest.raises(ContentKeyUnavailableError) as error:
        backend.tenant_llm_credentials(tenant)
    assert str(error.value) == f"tenant LLM credentials for {tenant!r} are unavailable"
    assert backend.clear_tenant_llm_credentials(tenant) is True
    assert backend.clear_tenant_llm_credentials(tenant) is False
    assert backend.tenant_llm_credential_state(tenant) is None


def test_purge_tenant_state_returns_an_identical_summary(backend: StorageBackend) -> None:
    """The summary is asserted against one literal, so both engines must produce it."""
    tenant = "purge-tenant"
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="purge-agent")
    tenant_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=tenant)
    backend.register_tenant_agent(tenant_id=tenant, agent_id="purge-agent", name="Purge Agent")
    backend.set_agent_motive_assignment(
        tenant_id=tenant, agent_id="purge-agent", motive_name="support", source="operator"
    )
    backend.set_tenant_prompt_override(tenant_id=tenant, prompt_profile="p", prompt_profile_version="1", override={})
    backend.save_tenant_prompt_version(tenant_id=tenant, prompt_text="prompt")
    backend.save_project_memory_config(tenant_id=tenant, config={"retention_days": 7}, configured_by="operator")
    backend.set_tenant_llm_credentials(tenant_id=tenant, provider="litellm", api_key="sk-keep")

    source, _ = backend.upsert_node(
        labels=["Entity"],
        key="purge source",
        properties={"name": "purge source", "scope_key": agent_scope.key},
    )
    target, _ = backend.upsert_node(
        labels=["Entity"],
        key="purge target",
        properties={"name": "purge target", "scope_key": agent_scope.key},
    )
    relationship = backend.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="REQUIRES",
        properties={"scope_key": agent_scope.key, "status": "active", "fact": "purge me"},
    )
    survivor = _scoped_relationship(backend, scope=OTHER_SCOPE, suffix=" survivor")

    use = backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH,
            idempotency_key="p1",
            scope=agent_scope,
        )
    )
    backend.record_outcome_event(
        _outcome_event(
            use.use_id,
            verdict=OutcomeVerdict.POSITIVE,
            judged_at=EPOCH + timedelta(minutes=1),
            idempotency_key="p2",
            scope=agent_scope,
        )
    )
    backend.mark_relationship(relationship.uuid, status=RelationshipStatus.PRUNED)
    backend.create_prune_ghost(
        relationship_uuid=relationship.uuid,
        scope_key=agent_scope.key,
        prune_receipt_uuid="receipt-purge",
        reason="purge",
        pruned_at=EPOCH,
    )
    episode = _episode(agent_scope, "purge episode", created_at=EPOCH)
    backend.add_episode(episode)
    backend.mark_episode_processed(episode.uuid, processed_at=EPOCH)
    backend.mark_episode_processed(episode.uuid, processed_at=EPOCH, consumer_key="formation")
    backend.record_dream_job_run(DreamJobRunRecord(ran_at=EPOCH, job_name="formation", job_kind=DreamJobKind.FORMATION))
    backend.set_job_last_run("formation", EPOCH)
    backend.record_dream_decision(
        _decision(
            ran_at=EPOCH,
            job_name="formation",
            agent_id="purge-agent",
            decision_type="formation.created",
            scope=agent_scope,
        )
    )

    summary = backend.purge_tenant_state(tenant)
    expected = {
        "tenant_id": tenant,
        "agent_ids": ["purge-agent"],
        "scope_keys": [tenant_scope.key, agent_scope.key],
        "raw_episodes_preserved": 1,
        "credentials_preserved": True,
        "processed_episodes_reset": 1,
        "episode_processing_reset": 1,
        "memory_outcome_events_deleted": 1,
        "memory_use_events_deleted": 1,
        "memory_prune_ghosts_deleted": 1,
        "quarantined_candidates_deleted": 0,
        "relationships_deleted": 1,
        "nodes_deleted": 2,
        "dream_decisions_deleted": 1,
        "memory_receipts_deleted": 0,
        "run_checkpoints_deleted": 0,
        "dream_job_runs_deleted": 1,
        "job_state_deleted": 1,
        "agent_motive_assignments_deleted": 1,
        "tenant_agents_deleted": 1,
        "tenant_prompt_overrides_deleted": 1,
        "tenant_prompt_versions_deleted": 1,
        "project_memory_config_versions_deleted": 1,
    }
    assert summary == expected, _diverged(backend, "purge_tenant_state summary", summary, expected)

    # Raw episodes and credentials survive; generated state does not.
    assert [item.uuid for item in backend.episodes()] == [episode.uuid], _diverged(
        backend, "purge preserving raw episodes", backend.episodes(), [episode]
    )
    assert backend.is_episode_processed(episode.uuid) is False
    assert backend.tenant_llm_credential_state(tenant) is not None
    assert backend.tenant_agents(tenant) == ()
    with pytest.raises(ValueError):
        backend.get_relationship(relationship.uuid)
    # Another tenant's graph is untouched.
    assert backend.get_relationship(survivor.uuid).uuid == survivor.uuid

    # Safe to re-run after a partial failure: the second pass deletes nothing.
    rerun = backend.purge_tenant_state(tenant)
    assert rerun["nodes_deleted"] == 0
    assert rerun["relationships_deleted"] == 0
    # The agents are gone, so the second pass addresses the tenant scope only.
    assert rerun["agent_ids"] == []
    assert rerun["scope_keys"] == [tenant_scope.key]
    assert [item.uuid for item in backend.episodes()] == [episode.uuid]


# ===========================================================================
# 6. Receipts
# ===========================================================================


def _emit_parity_chain(backend: StorageBackend, *, scope_key: str) -> Any:
    """Emit a fixed three-receipt run; explicit timestamps, no wall clock."""
    run = backend.receipts.begin_run(
        run_kind="formation",
        job_name="parity-chain",
        scope_key=scope_key,
        effective_policy_digest=config_effective_policy_digest({"dedup": {"threshold": 0.88}}),
        tenant_id="parity-tenant",
        agent_id="parity-agent",
        graph_state_hash_before="a" * 64,
    )
    backend.receipts.emit(
        run,
        decision_type=ReceiptDecisionType.CANDIDATE_EXTRACTED,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_digest="1" * 64,
        created_at=EPOCH,
    )
    backend.receipts.emit(
        run,
        decision_type=ReceiptDecisionType.CANDIDATE_LOW_SALIENCE,
        decision_reason="below_salience_threshold",
        decision_result="rejected",
        candidate_digest="2" * 64,
        salience_score=0.1,
        salience_threshold=0.5,
        created_at=EPOCH + timedelta(seconds=1),
    )
    backend.receipts.emit(
        run,
        decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
        decision_reason="materialized",
        decision_result="materialized",
        candidate_digest="3" * 64,
        created_at=EPOCH + timedelta(seconds=2),
    )
    return run


def test_receipt_chain_verifies_on_each_engine(backend: StorageBackend) -> None:
    run = _emit_parity_chain(backend, scope_key=AGENT_SCOPE.key)
    checkpoint = backend.receipts.checkpoint(run, graph_state_hash_after="b" * 64)
    assert checkpoint.receipt_count == 3

    verification = backend.receipts.verify_chain(run.run_uuid)
    assert verification.valid is True, _diverged(backend, "verify_chain", verification.errors, "no errors")
    assert verification.receipt_count == 3
    assert verification.computed_merkle_root == checkpoint.merkle_root
    assert verification.checkpoint_merkle_root == checkpoint.merkle_root
    assert verification.first_divergent_event_index is None

    receipts = backend.receipts.receipts_for_run(run.run_uuid)
    assert [item.event_index for item in receipts] == [0, 1, 2], _diverged(
        backend, "receipt event_index density", [item.event_index for item in receipts], [0, 1, 2]
    )
    assert receipts[0].previous_receipt_hash == "0" * 64
    assert receipts[1].previous_receipt_hash == receipts[0].receipt_hash
    assert receipts[2].previous_receipt_hash == receipts[1].receipt_hash
    assert receipts[1].salience_score == pytest.approx(0.1)
    assert backend.receipts.checkpoint_for_run(run.run_uuid).merkle_root == checkpoint.merkle_root

    # ``receipts_for_scope`` orders by (created_at, run_uuid, event_index); the
    # created_at values above are explicit and distinct.
    scoped = backend.receipts.receipts_for_scope(AGENT_SCOPE.key)
    assert [item.event_index for item in scoped] == [0, 1, 2]
    assert backend.receipts.receipts_for_scope(OTHER_SCOPE.key) == []

    # Negative space selects only the non-materialising decisions.
    negative = backend.receipts.negative_space(scope_key=AGENT_SCOPE.key)
    assert [item.decision_type for item in negative] == [ReceiptDecisionType.CANDIDATE_LOW_SALIENCE], _diverged(
        backend,
        "negative_space filtering",
        [item.decision_type for item in negative],
        [ReceiptDecisionType.CANDIDATE_LOW_SALIENCE],
    )
    assert negative[0].salience_threshold == pytest.approx(0.5)
    assert (
        backend.receipts.negative_space(
            scope_key=AGENT_SCOPE.key, decision_type=ReceiptDecisionType.CANDIDATE_EXTRACTED
        )
        == []
    )
    assert (
        backend.receipts.negative_space(scope_key=AGENT_SCOPE.key, since=EPOCH, until=EPOCH + timedelta(seconds=1))
        == []
    )
    assert len(backend.receipts.negative_space(decision_reason="below_salience_threshold")) == 1
    assert backend.receipts.negative_space(decision_reason="no-such-reason") == []
    assert backend.receipts.negative_space(scope_key=OTHER_SCOPE.key) == []

    backend.receipts.set_replay_status(run.run_uuid, "verified")
    assert backend.receipts.checkpoint_for_run(run.run_uuid).replay_status == "verified"

    # Ledger failure paths: same exception class, same wording, both engines.
    with pytest.raises(RuntimeError) as recheckpoint:
        backend.receipts.checkpoint(run)
    assert str(recheckpoint.value) == f"checkpoint already exists for run {run.run_uuid}"
    empty_run = backend.receipts.begin_run(
        run_kind="pruning",
        job_name="parity-empty",
        scope_key=AGENT_SCOPE.key,
        effective_policy_digest=config_effective_policy_digest({}),
    )
    with pytest.raises(RuntimeError) as empty:
        backend.receipts.checkpoint(empty_run)
    assert str(empty.value) == (f"cannot checkpoint run {empty_run.run_uuid}: the run emitted zero receipts")
    with pytest.raises(ValueError) as bad_status:
        backend.receipts.set_replay_status(run.run_uuid, "maybe")
    assert str(bad_status.value) == ("replay status 'maybe' is not one of ['mismatch', 'unverified', 'verified']")
    with pytest.raises(ValueError) as missing_checkpoint:
        backend.receipts.checkpoint_for_run("no-such-run")
    assert str(missing_checkpoint.value) == "no checkpoint recorded for run no-such-run"
    with pytest.raises(ValueError) as unknown_field:
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.CANDIDATE_EXTRACTED,
            decision_reason="c",
            decision_result="observed",
            not_a_field=1,
        )
    assert str(unknown_field.value) == "unknown receipt fields: ['not_a_field']"

    # An unknown run verifies as invalid rather than raising.
    unknown = backend.receipts.verify_chain("no-such-run")
    assert unknown.valid is False
    assert unknown.receipt_count == 0
    assert unknown.errors == ("no receipts recorded for run no-such-run",), _diverged(
        backend, "verify_chain on an unknown run", unknown.errors, "the SQLite wording"
    )


@pytest.mark.postgres
def test_receipt_chain_and_merkle_root_are_identical_across_engines(
    both_backends: tuple[StorageBackend, StorageBackend],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same input sequence, same chain — storage is the only variable.

    The Postgres ledger derives ``event_index`` and the predecessor hash from
    the database under a per-run lock where SQLite takes them off the in-memory
    run cursor.  Identical hashes prove that change did not alter what is
    committed.
    """
    import memotron.storage.receipts as receipts_module

    sqlite_backend, postgres_backend = both_backends
    chains: dict[str, dict[str, Any]] = {}
    for store in (sqlite_backend, postgres_backend):
        sequence = _SequentialUUIDs()
        monkeypatch.setattr(receipts_module, "uuid4", sequence)
        if _engine_name(store) == "postgres":
            import memotron.storage.postgres._receipts as pg_receipts_module

            monkeypatch.setattr(pg_receipts_module, "uuid4", sequence)
        run = _emit_parity_chain(store, scope_key=AGENT_SCOPE.key)
        checkpoint = store.receipts.checkpoint(run, graph_state_hash_after="b" * 64)
        chains[_engine_name(store)] = {
            "run_uuid": run.run_uuid,
            "receipt_uuids": [item.receipt_uuid for item in store.receipts.receipts_for_run(run.run_uuid)],
            "payload_digests": [item.payload_digest for item in store.receipts.receipts_for_run(run.run_uuid)],
            "receipt_hashes": [item.receipt_hash for item in store.receipts.receipts_for_run(run.run_uuid)],
            "canonical_payloads": [item.canonical_payload for item in store.receipts.receipts_for_run(run.run_uuid)],
            "merkle_root": checkpoint.merkle_root,
            "first_receipt_hash": checkpoint.first_receipt_hash,
            "last_receipt_hash": checkpoint.last_receipt_hash,
            "receipt_count": checkpoint.receipt_count,
            "valid": store.receipts.verify_chain(run.run_uuid).valid,
        }

    assert chains["sqlite"]["valid"] is True
    assert chains["postgres"]["valid"] is True
    for field in (
        "run_uuid",
        "receipt_uuids",
        "canonical_payloads",
        "payload_digests",
        "receipt_hashes",
        "first_receipt_hash",
        "last_receipt_hash",
        "receipt_count",
        "merkle_root",
    ):
        assert chains["sqlite"][field] == chains["postgres"][field], (
            f"receipt ledger diverged on {field}: sqlite produced "
            f"{chains['sqlite'][field]!r} but postgres produced {chains['postgres'][field]!r}"
        )


# ===========================================================================
# 7. Error parity
# ===========================================================================


def test_failure_paths_raise_the_same_exception_type_and_message(
    backend: StorageBackend,
) -> None:
    """A representative set of failure paths, asserted type-and-message.

    Both engines run this identical table, so a divergent message or exception
    class on either one fails here with the offending call named.
    """
    relationship = _scoped_relationship(backend, suffix=" errors")
    episode = _episode(AGENT_SCOPE, "duplicate", created_at=EPOCH)
    backend.add_episode(episode)
    backend.register_tenant_agent(tenant_id="err-tenant", agent_id="agent-a", name="Agent A")
    backend.get_or_create_governance_key("agent:shredded")
    backend.shred_governance_key("agent:shredded")

    cases: list[tuple[str, Any, type[Exception], str]] = [
        (
            "get_node(missing)",
            lambda: backend.get_node("missing-node"),
            ValueError,
            "node does not exist: missing-node",
        ),
        (
            "get_relationship(missing)",
            lambda: backend.get_relationship("missing-rel"),
            ValueError,
            "relationship does not exist: missing-rel",
        ),
        (
            "add_relationship(missing source)",
            lambda: backend.add_relationship(
                source_uuid="missing-source",
                target_uuid=relationship.target_uuid,
                relationship_type="REQUIRES",
                properties={},
            ),
            ValueError,
            "source node does not exist: missing-source",
        ),
        (
            "add_relationship(missing target)",
            lambda: backend.add_relationship(
                source_uuid=relationship.source_uuid,
                target_uuid="missing-target",
                relationship_type="REQUIRES",
                properties={},
            ),
            ValueError,
            "target node does not exist: missing-target",
        ),
        (
            "mark_relationship(missing)",
            lambda: backend.mark_relationship("missing-rel", status=RelationshipStatus.PRUNED),
            ValueError,
            "relationship does not exist: missing-rel",
        ),
        (
            "update_relationship(missing)",
            lambda: backend.update_relationship("missing-rel", properties={"a": 1}),
            ValueError,
            "relationship does not exist: missing-rel",
        ),
        (
            "graph_state_hash(blank)",
            lambda: backend.graph_state_hash("   "),
            ValueError,
            "graph_state_hash requires a non-blank scope_key",
        ),
        (
            "get_episode(missing)",
            lambda: backend.get_episode("missing-episode"),
            ValueError,
            "episode does not exist: missing-episode",
        ),
        (
            "mark_episode_processed(blank consumer)",
            lambda: backend.mark_episode_processed(episode.uuid, processed_at=EPOCH, consumer_key="   "),
            ValueError,
            "consumer_key cannot be blank",
        ),
        (
            "episode_event_counts(blank event)",
            lambda: backend.episode_event_counts(scope=AGENT_SCOPE, event="  "),
            ValueError,
            "event cannot be blank",
        ),
        (
            "dream_job_runs(limit=0)",
            lambda: backend.dream_job_runs(limit=0),
            ValueError,
            "limit must be greater than zero",
        ),
        (
            "dream_job_runs(blank job_name)",
            lambda: backend.dream_job_runs(job_name="  "),
            ValueError,
            "job_name cannot be blank",
        ),
        (
            "dream_decisions(limit=0)",
            lambda: backend.dream_decisions(limit=0),
            ValueError,
            "limit must be greater than zero",
        ),
        (
            "dream_decisions(blank agent_id)",
            lambda: backend.dream_decisions(agent_id=" "),
            ValueError,
            "agent_id cannot be blank",
        ),
        (
            "dream_decisions(blank decision_type_prefix)",
            lambda: backend.dream_decisions(decision_type_prefix=" "),
            ValueError,
            "decision_type_prefix cannot be blank",
        ),
        (
            "record_outcome_event(unknown use_id)",
            lambda: backend.record_outcome_event(
                _outcome_event(
                    "missing-use",
                    verdict=OutcomeVerdict.POSITIVE,
                    judged_at=EPOCH,
                    idempotency_key="err-1",
                )
            ),
            ValueError,
            "outcome event references unknown use_id 'missing-use'",
        ),
        (
            "utility_projection(naive as_of)",
            lambda: backend.utility_projection(
                scope_key=AGENT_SCOPE.key,
                as_of=datetime(2024, 3, 1, 12, 0, 0),  # noqa: DTZ001 - naive IS the input under test
            ),
            ValueError,
            "utility projection as_of must be timezone-aware",
        ),
        (
            "utility_projection_from_receipts(naive as_of)",
            lambda: backend.utility_projection_from_receipts(
                scope_key=AGENT_SCOPE.key,
                as_of=datetime(2024, 3, 1, 12, 0, 0),  # noqa: DTZ001 - naive IS the input under test
            ),
            ValueError,
            "utility projection as_of must be timezone-aware",
        ),
        (
            "prune_ghost(missing)",
            lambda: backend.prune_ghost("missing-rel"),
            ValueError,
            "no prune ghost for relationship 'missing-rel'",
        ),
        (
            "create_prune_ghost(blank receipt)",
            lambda: backend.create_prune_ghost(
                relationship_uuid=relationship.uuid,
                scope_key=AGENT_SCOPE.key,
                prune_receipt_uuid="",
                reason="x",
                pruned_at=EPOCH,
            ),
            ValueError,
            "prune_receipt_uuid must be non-blank",
        ),
        (
            "create_prune_ghost(scope mismatch)",
            lambda: backend.create_prune_ghost(
                relationship_uuid=relationship.uuid,
                scope_key=OTHER_SCOPE.key,
                prune_receipt_uuid="receipt-x",
                reason="x",
                pruned_at=EPOCH,
            ),
            ValueError,
            "prune ghost scope must match relationship scope",
        ),
        (
            "live_artifact_version(missing)",
            lambda: backend.live_artifact_version(scope=AGENT_SCOPE, artifact_id="no-such-artifact"),
            ValueError,
            "live artifact does not exist: 'no-such-artifact'",
        ),
        (
            "live_artifact_contribution(minimum_evidence=0)",
            lambda: backend.live_artifact_contribution(
                scope=AGENT_SCOPE, artifact_id="no-such-artifact", minimum_evidence=0
            ),
            ValueError,
            "minimum_evidence must be at least one",
        ),
        (
            "register_live_artifact(wrong class)",
            lambda: backend.register_live_artifact(
                _artifact(AGENT_SCOPE, updated_at=EPOCH).model_copy(update={"artifact_class": ArtifactClass.MEMORY})
            ),
            ValueError,
            "live artifacts must be skill or file artifacts",
        ),
        (
            "register_live_artifact(no directives)",
            lambda: backend.register_live_artifact(
                _artifact(AGENT_SCOPE, updated_at=EPOCH).model_copy(update={"directives": []})
            ),
            ValueError,
            "live artifact registration requires at least one structured directive",
        ),
        (
            "register_live_artifact(unknown author)",
            lambda: backend.register_live_artifact(
                _artifact(AGENT_SCOPE, updated_at=EPOCH).model_copy(update={"author": "unknown"})
            ),
            ValueError,
            "live artifact registration requires a known author",
        ),
        (
            "record_live_artifact_outcome(missing artifact)",
            lambda: backend.record_live_artifact_outcome(
                scope=AGENT_SCOPE,
                artifact_id="no-such-artifact",
                positive=True,
                occurred_at=EPOCH,
                minimum_evidence=1,
                quarantine_threshold=0.0,
                task_run_id="run",
                idempotency_key="err-artifact",
                payload={},
            ),
            ValueError,
            "live artifact does not exist: 'no-such-artifact'",
        ),
        (
            "record_live_artifact_outcome(blank task_run_id)",
            lambda: backend.record_live_artifact_outcome(
                scope=AGENT_SCOPE,
                artifact_id="no-such-artifact",
                positive=True,
                occurred_at=EPOCH,
                minimum_evidence=1,
                quarantine_threshold=0.0,
                task_run_id="  ",
                idempotency_key="err-artifact",
                payload={},
            ),
            ValueError,
            "task_run_id cannot be blank",
        ),
        (
            "recover_coherence_repair_monitor(missing)",
            lambda: backend.recover_coherence_repair_monitor(
                scope=AGENT_SCOPE, incident_id="no-incident", recovered_at=EPOCH
            ),
            ValueError,
            "coherence repair monitor does not exist for incident 'no-incident'",
        ),
        (
            "rollback_coherence_repair_monitor(missing)",
            lambda: backend.rollback_coherence_repair_monitor(
                scope=AGENT_SCOPE, incident_id="no-incident", reopened_at=EPOCH
            ),
            ValueError,
            "coherence repair monitor does not exist for incident 'no-incident'",
        ),
        (
            "stage_policy_contract(short digest)",
            lambda: backend.stage_policy_contract(
                contract_digest="too-short",
                payload={},
                certification={},
                certification_passed=True,
                staged_at=EPOCH,
            ),
            ValueError,
            "policy contract digest must be a SHA-256 hex digest",
        ),
        (
            "policy_contract(missing)",
            lambda: backend.policy_contract(contract_digest="e" * 64),
            ValueError,
            f"policy contract does not exist: {'e' * 64}",
        ),
        (
            "policy_shadow_stage(missing)",
            lambda: backend.policy_shadow_stage(stage_id="no-stage"),
            ValueError,
            "policy shadow stage does not exist: 'no-stage'",
        ),
        (
            "policy_shadow_stages(limit=0)",
            lambda: backend.policy_shadow_stages(scope_key=AGENT_SCOPE.key, limit=0),
            ValueError,
            "shadow stage limit must be at least one",
        ),
        (
            "rollback_policy_alias(missing)",
            lambda: backend.rollback_policy_alias(scope_key=AGENT_SCOPE.key, alias="no-alias", now=EPOCH),
            ValueError,
            "policy alias does not exist: 'no-alias'",
        ),
        (
            "register_tenant_agent(blank tenant)",
            lambda: backend.register_tenant_agent(tenant_id="  ", agent_id="agent-a", name="Agent A"),
            ValueError,
            "tenant_id cannot be blank",
        ),
        (
            "set_agent_motive_assignment(unregistered agent)",
            lambda: backend.set_agent_motive_assignment(
                tenant_id="err-tenant",
                agent_id="ghost-agent",
                motive_name="support",
                source="operator",
            ),
            ValueError,
            "agent 'ghost-agent' is not registered for tenant 'err-tenant'",
        ),
        (
            "set_agent_motive_assignment(blank motive)",
            lambda: backend.set_agent_motive_assignment(
                tenant_id="err-tenant", agent_id="agent-a", motive_name=" ", source="operator"
            ),
            ValueError,
            "motive_name cannot be blank",
        ),
        (
            "save_tenant_prompt_version(blank text)",
            lambda: backend.save_tenant_prompt_version(tenant_id="err-tenant", prompt_text="  "),
            ValueError,
            "prompt_text cannot be blank",
        ),
        (
            "save_project_memory_config(blank configured_by)",
            lambda: backend.save_project_memory_config(tenant_id="err-tenant", config={}, configured_by=" "),
            ValueError,
            "configured_by cannot be blank",
        ),
        (
            "set_tenant_llm_credentials(unknown provider)",
            lambda: backend.set_tenant_llm_credentials(tenant_id="err-tenant", provider="bedrock", api_key="sk"),
            ValueError,
            "provider must be 'litellm' or 'openai'",
        ),
        (
            "set_tenant_llm_credentials(blank api key)",
            lambda: backend.set_tenant_llm_credentials(tenant_id="err-tenant", provider="litellm", api_key="   "),
            ValueError,
            "api_key cannot be blank",
        ),
        (
            "get_or_create_governance_key(shredded scope)",
            lambda: backend.get_or_create_governance_key("agent:shredded"),
            ContentKeyUnavailableError,
            "governance key for scope 'agent:shredded' was crypto-shredded; the scope's "
            "content is unrecoverable and the scope no longer accepts crypto-governed writes",
        ),
        # Deliberately last.  Both engines raise the same ValueError here, but on
        # SQLite the INSERT that raised leaves its implicit transaction open, so
        # any later interface call that needs its own ``BEGIN IMMEDIATE`` (the
        # policy-alias paths) fails with "cannot start a transaction within a
        # transaction".  That is a divergence in the SQLite substrate's
        # transaction hygiene, not in the method under test; it is reported
        # rather than worked around anywhere except in this ordering.
        (
            "add_episode(duplicate)",
            lambda: backend.add_episode(episode),
            ValueError,
            f"episode already exists: {episode.uuid}",
        ),
    ]

    for label, call, exception_type, message in cases:
        with pytest.raises(exception_type) as error:
            call()
        assert type(error.value) is exception_type, (
            f"engine {_engine_name(backend)!r} diverged on {label}: raised "
            f"{type(error.value).__name__}, the contract requires {exception_type.__name__}"
        )
        assert str(error.value) == message, (
            f"engine {_engine_name(backend)!r} diverged on {label}: message was "
            f"{str(error.value)!r}, the contract requires {message!r}"
        )


# ---------------------------------------------------------------------------
# Pushed-down retrieval reads and the logical-operation bracket
# (the Operational Store issue: filters into the query, comparison in the
# engine, one commit per logical operation)
# ---------------------------------------------------------------------------


def test_relationships_for_scope_is_scope_bounded_and_ordered(
    backend: StorageBackend,
) -> None:
    first = _scoped_relationship(backend, suffix="-a")
    second = _scoped_relationship(backend, suffix="-b", status=RelationshipStatus.SUPERSEDED.value)
    _scoped_relationship(backend, scope=OTHER_SCOPE, suffix="-elsewhere")

    observed = backend.relationships_for_scope(AGENT_SCOPE.key)
    assert [item.uuid for item in observed] == [first.uuid, second.uuid], _diverged(
        backend,
        "relationships_for_scope (scope bound + (created_at, uuid) order, any status)",
        [item.uuid for item in observed],
        [first.uuid, second.uuid],
    )


def test_similar_relationships_scores_in_the_engine(backend: StorageBackend) -> None:
    exact = _scoped_relationship(backend, suffix="-exact", embedding=[1.0, 0.0, 0.0])
    close = _scoped_relationship(backend, suffix="-close", embedding=[0.9, 0.1, 0.0])
    orthogonal = _scoped_relationship(backend, suffix="-orthogonal", embedding=[0.0, 0.0, 1.0])
    # Sealed embeddings never reach engine-side comparison.
    _scoped_relationship(backend, suffix="-sealed", embedding="dwsealed:v1:opaque")
    # Non-active rows are excluded by the default status filter.
    _scoped_relationship(
        backend,
        suffix="-superseded",
        embedding=[1.0, 0.0, 0.0],
        status=RelationshipStatus.SUPERSEDED.value,
    )
    # Other scopes never participate.
    _scoped_relationship(backend, scope=OTHER_SCOPE, suffix="-elsewhere", embedding=[1.0, 0.0, 0.0])

    scored = backend.similar_relationships(scope=AGENT_SCOPE, embedding=[1.0, 0.0, 0.0], threshold=0.0, limit=None)
    observed_order = [item[0].uuid for item in scored]
    expected_order = [exact.uuid, close.uuid, orthogonal.uuid]
    assert observed_order == expected_order, _diverged(
        backend,
        "similar_relationships ordering (similarity DESC, uuid)",
        observed_order,
        expected_order,
    )
    similarities = {item[0].uuid: item[1] for item in scored}
    assert abs(similarities[exact.uuid] - 1.0) < 1e-9, _diverged(
        backend, "exact-match similarity", similarities[exact.uuid], 1.0
    )
    assert abs(similarities[orthogonal.uuid]) < 1e-9, _diverged(
        backend, "orthogonal similarity", similarities[orthogonal.uuid], 0.0
    )

    thresholded = backend.similar_relationships(scope=AGENT_SCOPE, embedding=[1.0, 0.0, 0.0], threshold=0.5, limit=None)
    assert [item[0].uuid for item in thresholded] == [exact.uuid, close.uuid], _diverged(
        backend,
        "similar_relationships threshold pushdown",
        [item[0].uuid for item in thresholded],
        [exact.uuid, close.uuid],
    )

    limited = backend.similar_relationships(scope=AGENT_SCOPE, embedding=[1.0, 0.0, 0.0], threshold=0.0, limit=1)
    assert [item[0].uuid for item in limited] == [exact.uuid]

    typed = backend.similar_relationships(
        scope=AGENT_SCOPE,
        embedding=[1.0, 0.0, 0.0],
        threshold=0.0,
        limit=None,
        relationship_types={"PREFERS"},
    )
    assert typed == [], _diverged(backend, "similar_relationships type filter", typed, [])


def test_transaction_bracket_commits_once_and_rolls_back(
    backend: StorageBackend,
) -> None:
    with pytest.raises(RuntimeError, match="abort the bracket"), backend.transaction():  # noqa: PT012 - the bracket under test spans several statements
        _scoped_relationship(backend, suffix="-doomed")
        backend.set_job_last_run("bracket-job", datetime.now(UTC))
        raise RuntimeError("abort the bracket")

    assert backend.relationships_for_scope(AGENT_SCOPE.key) == [], _diverged(
        backend, "transaction rollback (graph write)", "rows persisted", "no rows"
    )
    assert backend.get_job_last_run("bracket-job") is None, _diverged(
        backend, "transaction rollback (job state)", "marker persisted", "no marker"
    )

    with backend.transaction():
        with backend.transaction():  # re-entrant: joins, does not commit early
            kept = _scoped_relationship(backend, suffix="-kept")
        backend.set_job_last_run("bracket-job", datetime.now(UTC))
    assert [item.uuid for item in backend.relationships_for_scope(AGENT_SCOPE.key)] == [kept.uuid]
    assert backend.get_job_last_run("bracket-job") is not None


# ===========================================================================
# 7. WS-26 derivation-DAG epoch layer, WS-17 registries, WS-24 quarantine
#
# These reached Postgres parity on 2026-08-25 (storage/postgres/_epochs.py).
# Every assertion runs on both engines through the `backend` fixture; the
# registry-state digest additionally asserts cross-engine string identity.
# ===========================================================================


def test_epoch_lifecycle_forks_flips_and_walks_ancestry(backend: StorageBackend) -> None:
    scope = AGENT_SCOPE
    root = backend.ensure_root_epoch(scope.key, now=EPOCH)
    # Idempotent: a scope that already has a HEAD returns it unchanged.
    assert backend.ensure_root_epoch(scope.key, now=EPOCH + timedelta(hours=1)) == root, _diverged(
        backend, "ensure_root_epoch idempotency", "new id", root
    )
    assert backend.active_epoch_for_scope(scope.key) == root

    branch = backend.create_epoch(
        scope_key=scope.key,
        parent_epoch_id=root,
        now=EPOCH + timedelta(minutes=1),
        label="branch-a",
        tier="A",
        overrides={"dedup": {"cosine_threshold": 0.8}},
    )
    row = backend.epoch(branch)
    assert row["parent_epoch_id"] == root
    assert row["status"] == "open"
    assert row["tier"] == "A"
    assert row["overrides"] == {"dedup": {"cosine_threshold": 0.8}}

    # epochs_for_scope is oldest-first: root then the branch.
    assert [e["epoch_id"] for e in backend.epochs_for_scope(scope.key)] == [root, branch]

    # Ancestry is self-first up to the root; the root's is just itself.
    assert backend.epoch_ancestry(branch) == (branch, root)
    assert backend.epoch_ancestry(root) == (root,)

    # Runs attach oldest-first and de-duplicate.
    backend.record_epoch_run(branch, "run-1", now=EPOCH + timedelta(minutes=2))
    backend.record_epoch_run(branch, "run-2", now=EPOCH + timedelta(minutes=3))
    backend.record_epoch_run(branch, "run-1", now=EPOCH + timedelta(minutes=4))  # idempotent
    assert backend.epoch_run_uuids(branch) == ["run-1", "run-2"]

    # Status transition is bookkeeping-only and returns the fresh row.
    assert backend.set_epoch_status(branch, "ready")["status"] == "ready"

    # The pointer flip is the whole visible effect of adopt/rollback.
    backend.set_active_epoch(scope.key, branch, now=EPOCH + timedelta(minutes=5))
    assert backend.active_epoch_for_scope(scope.key) == branch
    backend.set_active_epoch(scope.key, root, now=EPOCH + timedelta(minutes=6))
    assert backend.active_epoch_for_scope(scope.key) == root

    # Failure paths raise ValueError on both engines.
    with pytest.raises(ValueError):
        backend.create_epoch(scope_key=scope.key, parent_epoch_id="epoch-missing", now=EPOCH)
    with pytest.raises(ValueError):
        backend.epoch("epoch-missing")
    with pytest.raises(ValueError):
        backend.set_epoch_status("epoch-missing", "ready")


def test_epoch_pre_adopt_snapshot_round_trips(backend: StorageBackend) -> None:
    scope = AGENT_SCOPE
    root = backend.ensure_root_epoch(scope.key, now=EPOCH)
    branch = backend.create_epoch(scope_key=scope.key, parent_epoch_id=root, now=EPOCH)
    snapshot = {
        "relationships": [{"uuid": "r-1", "properties": {"fact": "x"}}],
        "entity_canon": [{"name": "acme", "before": None}],
        "predicate_canon": [],
    }
    backend.set_epoch_pre_adopt_snapshot(branch, snapshot)
    assert backend.epoch(branch)["pre_adopt_snapshot"] == snapshot
    with pytest.raises(ValueError):
        backend.set_epoch_pre_adopt_snapshot("epoch-missing", snapshot)


def test_predicate_registry_first_wins_overlay_and_delete(backend: StorageBackend) -> None:
    scope = AGENT_SCOPE
    backend.register_predicate(scope.key, "resides in", "lives in", decided_by="operator")
    # First-wins: a second registration for the same surface is a no-op.
    backend.register_predicate(scope.key, "resides in", "DIFFERENT", decided_by="operator")
    assert backend.canonical_predicate_for(scope.key, "resides in") == "lives in"
    assert backend.canonical_predicate_for(scope.key, "unmapped") is None

    backend.register_predicate(scope.key, "works at", "employed by", decided_by="operator")
    assert set(backend.canonical_predicates_for_scope(scope.key)) == {"lives in", "employed by"}

    # Overlay is an administrative upsert that bypasses first-wins and tags the epoch.
    backend.overlay_predicate_row(
        scope.key,
        {
            "predicate_normalized": "resides in",
            "canonical_predicate": "dwells in",
            "decided_by": "epoch-adopt",
            "created_at": EPOCH.isoformat(),
        },
        epoch_id="epoch-xyz",
    )
    overlaid = backend.predicate_alias_row(scope.key, "resides in")
    assert overlaid is not None
    assert overlaid["canonical_predicate"] == "dwells in"
    assert overlaid["epoch_id"] == "epoch-xyz"

    rows = backend.predicate_alias_rows_for_scope(scope.key)
    assert {r["predicate_normalized"] for r in rows} == {"resides in", "works at"}

    backend.delete_predicate_row(scope.key, "resides in")
    assert backend.predicate_alias_row(scope.key, "resides in") is None


def test_entity_registry_transitions_overlay_and_delete(backend: StorageBackend) -> None:
    scope = AGENT_SCOPE
    # A proposed alias does not resolve; promoting it to ACTIVE does.
    backend.register_entity_alias(scope.key, "the gateway", "Jedai Gateway", status="proposed", decided_by="resolver")
    assert backend.canonical_entity_name_for(scope.key, "the gateway") is None
    backend.register_entity_alias(scope.key, "the gateway", "Jedai Gateway", status="active", decided_by="resolver")
    assert backend.canonical_entity_name_for(scope.key, "the gateway") == "Jedai Gateway"

    row = backend.entity_alias_row(scope.key, "the gateway")
    assert row is not None and row["status"] == "active"

    # A second, still-proposed alias is filterable by status.
    backend.register_entity_alias(
        scope.key, "gcx", "guest content experience", status="proposed", decided_by="resolver"
    )
    assert {r["name_normalized"] for r in backend.entity_alias_rows_for_scope(scope.key, "active")} == {"the gateway"}
    assert len(backend.entity_alias_rows_for_scope(scope.key)) == 2

    # resolve_entity_alias transitions status and can carry a new canonical.
    resolved = backend.resolve_entity_alias(
        scope.key,
        "gcx",
        status="active",
        resolved_by="operator",
        resolved_at=EPOCH + timedelta(hours=1),
    )
    assert resolved["status"] == "active"

    backend.update_entity_alias_evidence(scope.key, "gcx", link_score=0.91, link_signals={"votes": 3})
    assert backend.entity_alias_row(scope.key, "gcx")["link_score"] == pytest.approx(0.91)

    # Overlay is the administrative upsert used at adopt; delete restores "absent".
    backend.overlay_entity_alias_row(
        scope.key,
        {
            "name_normalized": "acme",
            "canonical_name": "Acme Corp",
            "status": "active",
            "decided_by": "epoch-adopt",
            "link_signals": {},
            "proposed_at": EPOCH.isoformat(),
        },
        epoch_id="epoch-xyz",
    )
    overlaid = backend.entity_alias_row(scope.key, "acme")
    assert overlaid is not None and overlaid["epoch_id"] == "epoch-xyz"
    backend.delete_entity_alias_row(scope.key, "acme")
    assert backend.entity_alias_row(scope.key, "acme") is None

    # Name-to-itself is rejected identically on both engines.
    with pytest.raises(ValueError):
        backend.register_entity_alias(scope.key, "loop", "loop", status="active", decided_by="resolver")


@pytest.mark.postgres
def test_registry_state_digest_is_identical_across_engines(
    both_backends: tuple[StorageBackend, StorageBackend],
) -> None:
    scope = AGENT_SCOPE
    for store in both_backends:
        # An empty scope still has a well-defined, stable digest (not None).
        assert isinstance(store.registry_state_digest(scope.key), str)
        store.register_entity_alias(scope.key, "the gateway", "Jedai Gateway", status="active", decided_by="resolver")
        store.register_predicate(scope.key, "resides in", "lives in", decided_by="operator")

    sqlite_backend, postgres_backend = both_backends
    assert sqlite_backend.registry_state_digest(scope.key) == postgres_backend.registry_state_digest(scope.key), (
        "registry_state_digest diverged across engines for identical registry state"
    )


def test_quarantine_store_files_reads_and_resolves(backend: StorageBackend) -> None:
    scope = AGENT_SCOPE
    filed = backend.quarantine_candidate(
        candidate_uuid="cand-1",
        scope_key=scope.key,
        episode_uuid="ep-1",
        reason="pending",
        detail="raw candidate",
        saves_step=None,
        subject="Nia",
        predicate="prefers",
        object_text="tea",
        proposed_relationship_type="PREFERS",
        proposed_memory_type="preference",
        candidate_payload='{"subject": "Nia"}',
        candidate_digest="digest-1",
        instruction_set="default",
        motive_name=None,
        quarantined_at=EPOCH,
        status=QuarantineStatus.PENDING,
    )
    assert filed.status is QuarantineStatus.PENDING
    assert filed.subject == "Nia"

    # Idempotent file: a second call with the same uuid leaves the row untouched.
    backend.quarantine_candidate(
        candidate_uuid="cand-1",
        scope_key=scope.key,
        episode_uuid="ep-1",
        reason="CHANGED",
        detail="changed",
        saves_step=None,
        subject="CHANGED",
        predicate="prefers",
        object_text="tea",
        proposed_relationship_type="PREFERS",
        proposed_memory_type="preference",
        candidate_payload="{}",
        candidate_digest="digest-1",
        instruction_set="default",
        motive_name=None,
        quarantined_at=EPOCH,
        status=QuarantineStatus.QUARANTINED,
    )
    assert backend.quarantined_candidate("cand-1").subject == "Nia"
    assert backend.quarantined_candidate_payload("cand-1") == '{"subject": "Nia"}'

    # A second candidate, filed straight into QUARANTINED, so the status filter bites.
    backend.quarantine_candidate(
        candidate_uuid="cand-2",
        scope_key=scope.key,
        episode_uuid="ep-2",
        reason="not-actionable",
        detail="",
        saves_step=None,
        subject="weather",
        predicate="is",
        object_text="sunny",
        proposed_relationship_type=None,
        proposed_memory_type=None,
        candidate_payload="{}",
        candidate_digest="digest-2",
        instruction_set="default",
        motive_name=None,
        quarantined_at=EPOCH + timedelta(minutes=1),
        status=QuarantineStatus.QUARANTINED,
    )
    pending = backend.quarantined_candidates(scope_key=scope.key, status=QuarantineStatus.PENDING)
    assert [c.candidate_uuid for c in pending] == ["cand-1"]
    assert backend.quarantine_counts(scope_key=scope.key) == {"pending": 1, "quarantined": 1}

    # Ordering is by (quarantined_at, candidate_uuid) — deterministic on both engines.
    every = backend.quarantined_candidates(scope_key=scope.key, status=None)
    assert [c.candidate_uuid for c in every] == ["cand-1", "cand-2"]

    resolved = backend.resolve_quarantined_candidate(
        "cand-1",
        status=QuarantineStatus.PROMOTED,
        resolved_at=EPOCH + timedelta(hours=1),
        resolved_by="governance",
        promoted_relationship_uuid="rel-1",
        reason="actionable",
    )
    assert resolved.status is QuarantineStatus.PROMOTED
    assert resolved.promoted_relationship_uuid == "rel-1"
    assert resolved.reason == "actionable"

    # Re-resolving a terminal row fails identically on both engines.
    with pytest.raises(ValueError):
        backend.resolve_quarantined_candidate(
            "cand-1",
            status=QuarantineStatus.DISCARDED,
            resolved_at=EPOCH + timedelta(hours=2),
            resolved_by="operator",
        )

    fields = dict(backend.quarantined_candidate_content_fields(scope_key=scope.key))
    assert fields["cand-1"]["subject"] == "Nia"


@pytest.mark.parametrize("memory_type", [t.value for t in MemoryType])
def test_utility_half_life_is_identical_across_engines_for_every_memory_type(
    backend: StorageBackend,
    memory_type: str,
) -> None:
    """Every MemoryType, not two of them.

    The existing `utility_projection` parity tests use `preference` and `requirement`, whose
    half-lives happen to be equal in both engines' tables. That made the suite structurally
    blind to a real divergence: `postgres/_operational.py` keyed its 365-day entry on
    `"identity"`, the name this type carried BEFORE it was renamed to `anchor`
    (`models/_enums.py`, which records the rename and why). `"identity"` matches no
    `MemoryType`, so on Postgres an `anchor` fell through to the 90-day default and decayed
    **4x faster than on SQLite** — under a docstring claiming byte-identical implementations.

    Parametrising over the enum is what makes this test resistant to the same failure: a
    memory type added later is covered on the day it is added, not the day someone remembers.

    Measuring the half-life rather than the table: one CITED event, then a projection taken
    exactly one half-life-candidate later. `stability` is `2 ** (-elapsed / half_life)`, so
    the value at `elapsed == half_life` is 0.5. Comparing that number across engines catches
    a wrong key without either engine having to expose its lookup table.
    """
    relationship = _scoped_relationship(backend, suffix=f" half-life {memory_type}", memory_type=memory_type)
    backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH,
            idempotency_key=f"hl-{memory_type}",
        )
    )

    # 365 days is the longest half-life in either table, so sampling there separates every
    # bucket: a type on its correct 365-day life reads 0.5, one that fell through to the
    # 90-day default reads ~0.06.
    projections = backend.utility_projection(
        scope_key=AGENT_SCOPE.key,
        as_of=EPOCH + timedelta(days=365),
    )
    by_uuid = {projection.relationship_uuid: projection for projection in projections}
    assert relationship.uuid in by_uuid, f"no projection produced for {memory_type}"

    stability = by_uuid[relationship.uuid].use_stability
    implied_half_life_days = 365 / math.log2(1 / stability) if 0 < stability < 1 else None
    assert implied_half_life_days is not None, (
        f"{memory_type}: stability {stability} is degenerate; cannot infer a half-life"
    )
    assert implied_half_life_days == pytest.approx(EXPECTED_HALF_LIFE_DAYS[memory_type], rel=1e-6), (
        f"{memory_type}: implied half-life {implied_half_life_days:.1f}d != "
        f"{EXPECTED_HALF_LIFE_DAYS[memory_type]}d. The engines' half-life tables have diverged."
    )


# ===========================================================================
# 9. #149 — methods exercised on SQLite and DARK on Postgres
#
# `scripts/verify/parity_coverage.py` reports, per method, which engine's lines
# a test run actually executed. The four below were `sqlite=covered,
# postgres=N missing` with the WHOLE Postgres body dark: the contract was
# asserted only against the engine that will never run in production.
#
# That is the exact shape that produced the anchor half-life bug, where
# `postgres/_operational.py` keyed an `anchor` half-life on `"identity"` and
# anchor memories decayed 4x faster on Postgres under a docstring promising
# byte-identical behaviour. A 3,420-line parity suite passed, because it
# exercised the method with two memory types whose half-lives happen to match.
#
# Uncovered is not broken, and none of these was a known defect. The argument
# is the base rate: the last two times anyone looked at an unexercised Postgres
# path — that half-life and T0-10's epoch filter — both were genuinely
# divergent. These live in the parity suite rather than a Postgres-only file so
# the SQLite side stays a control: a test that passes on one engine and not the
# other is the finding.
# ===========================================================================


def test_use_event_is_findable_by_its_idempotency_key(backend: StorageBackend) -> None:
    """#149: the whole Postgres body of `use_event_for_idempotency_key` was dark.

    This is the read half of use-event idempotency — the lookup a caller makes to
    decide whether an effect has already been applied. A divergence here is a
    double-applied effect, and the SQLite twin was fully covered.

    Exercises both branches: the hit, and the `row is None` miss. Asserting only
    the hit would leave the miss dark, and a lookup that never returns None is
    indistinguishable from one that always finds *something*.
    """
    relationship = _scoped_relationship(backend, suffix=" idem-lookup")
    event = _use_event(
        relationship.uuid,
        kind=UseEventKind.CITED_OR_USED,
        used_at=EPOCH,
        idempotency_key="use-lookup-1",
    )
    stored = backend.record_use_event(event)

    found = backend.use_event_for_idempotency_key(scope_key=AGENT_SCOPE.key, idempotency_key="use-lookup-1")
    assert found is not None, _diverged(backend, "use_event_for_idempotency_key hit", found, "an event")
    assert found.use_id == stored.use_id, _diverged(
        backend, "use_event_for_idempotency_key identity", found.use_id, stored.use_id
    )
    assert found.idempotency_key == "use-lookup-1"

    # MISS: an unused key in a scope that does hold events.
    assert backend.use_event_for_idempotency_key(scope_key=AGENT_SCOPE.key, idempotency_key="never-issued") is None, (
        _diverged(backend, "use_event_for_idempotency_key unknown key", "an event", None)
    )

    # MISS: the RIGHT key in the WRONG scope. Idempotency is keyed per scope, so
    # a lookup that ignored scope would leak one tenant's replay state to another
    # and still pass the two assertions above.
    assert backend.use_event_for_idempotency_key(scope_key=OTHER_SCOPE.key, idempotency_key="use-lookup-1") is None, (
        _diverged(backend, "use_event_for_idempotency_key scope isolation", "an event", None)
    )


def test_outcome_event_is_findable_by_its_idempotency_key(backend: StorageBackend) -> None:
    """#149: the whole Postgres body of `outcome_event_for_idempotency_key` was dark.

    Same reasoning as the use-event twin above; an outcome recorded twice is a
    double-counted verdict against a memory's utility.
    """
    relationship = _scoped_relationship(backend, suffix=" outcome-lookup")
    use = backend.record_use_event(
        _use_event(
            relationship.uuid,
            kind=UseEventKind.CITED_OR_USED,
            used_at=EPOCH,
            idempotency_key="use-for-outcome-1",
        )
    )
    stored = backend.record_outcome_event(
        _outcome_event(
            use.use_id,
            verdict=OutcomeVerdict.POSITIVE,
            judged_at=EPOCH,
            idempotency_key="outcome-lookup-1",
        )
    )

    found = backend.outcome_event_for_idempotency_key(scope_key=AGENT_SCOPE.key, idempotency_key="outcome-lookup-1")
    assert found is not None, _diverged(backend, "outcome_event_for_idempotency_key hit", found, "an event")
    assert found.use_id == stored.use_id, _diverged(
        backend, "outcome_event_for_idempotency_key identity", found.use_id, stored.use_id
    )
    assert found.verdict == OutcomeVerdict.POSITIVE

    assert (
        backend.outcome_event_for_idempotency_key(scope_key=AGENT_SCOPE.key, idempotency_key="never-judged") is None
    ), _diverged(backend, "outcome_event_for_idempotency_key unknown key", "an event", None)

    assert (
        backend.outcome_event_for_idempotency_key(scope_key=OTHER_SCOPE.key, idempotency_key="outcome-lookup-1") is None
    ), _diverged(backend, "outcome_event_for_idempotency_key scope isolation", "an event", None)


def test_promotion_endorsements_record_list_and_count(backend: StorageBackend) -> None:
    """#149: all three Postgres endorsement methods were dark — 13 lines, the
    largest single gap in the report.

    Endorsements gate object-level promotion into project memory: a candidate
    forms once `min_endorsements` distinct agents have voted. The three
    properties that matter are asserted here because each one, if it diverged,
    would change *when a memory becomes shared truth* — and would do so silently.
    """
    episode = "cand-" + "0" * 8

    first = backend.record_promotion_endorsement(
        candidate_episode_uuid=episode, agent_id="agent-alpha", rationale="alpha says yes"
    )
    assert first["created"] is True, _diverged(backend, "first endorsement created", first["created"], True)

    # IDEMPOTENT PER AGENT. Without this, one agent voting twice would satisfy a
    # two-endorsement policy on its own -- promotion by repetition.
    repeat = backend.record_promotion_endorsement(
        candidate_episode_uuid=episode, agent_id="agent-alpha", rationale="alpha says yes again"
    )
    assert repeat["created"] is False, _diverged(
        backend, "duplicate endorsement created flag", repeat["created"], False
    )
    assert backend.promotion_endorsement_count(episode) == 1, _diverged(
        backend, "count after duplicate", backend.promotion_endorsement_count(episode), 1
    )

    backend.record_promotion_endorsement(
        candidate_episode_uuid=episode, agent_id="agent-bravo", rationale="bravo concurs"
    )
    assert backend.promotion_endorsement_count(episode) == 2, _diverged(
        backend, "count of distinct endorsers", backend.promotion_endorsement_count(episode), 2
    )

    listed = backend.promotion_endorsements_for(episode)
    agents = [row["agent_id"] for row in listed]
    assert agents == ["agent-alpha", "agent-bravo"], _diverged(
        backend, "endorsement vote order", agents, ["agent-alpha", "agent-bravo"]
    )
    assert listed[0]["rationale"] == "alpha says yes", (
        "the FIRST rationale must survive; a duplicate vote must not rewrite the record it failed to create"
    )

    # A candidate nobody endorsed reads as empty, not as an error. Without this the
    # count could be implemented as "rows or raise" and still pass everything above.
    assert backend.promotion_endorsement_count("cand-" + "9" * 8) == 0
    assert backend.promotion_endorsements_for("cand-" + "9" * 8) == ()


def test_set_agent_motive_assignment_rejects_an_unregistered_agent(backend: StorageBackend) -> None:
    """Both engines refuse a motive for an agent that was never registered.

    #149 lists this method's two dark Postgres lines as a coverage gap. Measured
    after writing this test: they are STILL dark, and correctly so. Postgres guards
    the insert with an explicit `SELECT EXISTS` over `tenant_agents` that raises
    this same `ValueError` first, so the `except ForeignKeyViolation` arm behind it
    is a TOCTOU fallback -- reachable only if the agent is deleted between the check
    and the insert. It is not reachable through the public API at all, which makes
    it a different category from the eleven ordinary gaps in that report.

    What this test does pin, and what was genuinely untested on Postgres: the
    refusal is a `ValueError`, not a raw `psycopg.errors.ForeignKeyViolation`
    leaking the driver's exception type through the storage contract.
    """
    tenant = "tenant-motive-fk"
    with pytest.raises(ValueError) as unregistered:
        backend.set_agent_motive_assignment(
            tenant_id=tenant, agent_id="never-registered", motive_name="support", source="operator"
        )
    message = str(unregistered.value)
    assert "never-registered" in message, _diverged(
        backend, "unregistered-agent message names the agent", message, "...never-registered..."
    )

    # POSITIVE CONTROL: the same call succeeds once the agent exists, so the test
    # above cannot be satisfied by a method that rejects everything.
    backend.register_tenant_agent(tenant_id=tenant, agent_id="never-registered", name="Now Real")
    assigned = backend.set_agent_motive_assignment(
        tenant_id=tenant, agent_id="never-registered", motive_name="support", source="operator"
    )
    assert assigned["motive_name"] == "support"
