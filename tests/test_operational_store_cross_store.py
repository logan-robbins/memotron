"""The cross-store rule, proven against the real Operational Store.

Acceptance criterion: *a transaction that spans both stores anchors in this one,
and repeating a graph write is safe.*

``storage/base.py`` states the rule this file tests:

    A logical transaction **anchors in the Operational Store**: the receipt (and
    any queue/job/event row) is the commit point and the source of truth for
    whether an operation happened.  Writes to the Memory Graph are **safe to
    repeat** [...] so a crashed operation is recovered by re-driving the graph
    writes from the anchored operational record.

Two properties follow, and both are tested here rather than asserted in prose:

1. **The anchor is authoritative.**  If the operational transaction rolls back,
   the operation did not happen — nothing it wrote is visible, including the
   receipt that would have claimed it did.  A reader must never find a receipt
   for work that was not committed, because replay trusts the receipt.
2. **Graph writes are replayable.**  Re-driving the graph half of an operation
   converges on the same state: ``upsert_node`` keys on the normalised graph key
   and relationship property updates are last-write-wins by uuid.  The evidence
   is the scope state hash — re-running the graph writes must leave it unchanged,
   because that hash is exactly what receipts commit to and what byte replay
   compares against.

These run against Postgres when ``MEMOTRON_TEST_POSTGRES_DSN`` is set and skip
otherwise, keeping the hermetic offline gate (DW-002) intact.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest

from memotron.models import MemoryScope, RelationshipStatus, ScopeKind
from memotron.storage.receipts import ReceiptDecisionType

# all 6 tests need a live Postgres; the marker makes the CI lane selectable with `-m postgres`.
pytestmark = pytest.mark.postgres

POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"


def _dsn() -> str:
    dsn = os.environ.get(POSTGRES_DSN_ENV)
    if not dsn:
        pytest.skip(f"set {POSTGRES_DSN_ENV} to run Operational Store cross-store tests")
    return dsn


@pytest.fixture
def backend():
    """A migrated Postgres backend on a schema reset to empty for each test."""
    dsn = _dsn()
    from memotron.storage.postgres import PostgresStorageBackend

    reset = PostgresStorageBackend(dsn, max_size=4, migrate=False)
    try:
        # Order-independence: every test starts from an empty schema.
        with reset.transaction():
            reset._engine.execute("DROP SCHEMA public CASCADE")
            reset._engine.execute("CREATE SCHEMA public")
    finally:
        reset.close()

    store = PostgresStorageBackend(dsn, max_size=4)
    try:
        yield store
    finally:
        store.close()


SCOPE = MemoryScope(kind=ScopeKind.AGENT, scope_id="cross-store")


def _write_fact(backend, *, subject: str, fact: str, predicate: str = "prefers"):
    """The graph half of a fact write, expressed so it is safe to repeat.

    ``upsert_node`` keys on the normalised graph key and the relationship is
    addressed by its truth key, so re-running this function is a convergence
    step rather than a second write.
    """
    subject_node, _ = backend.upsert_node(
        labels=["Entity"], key=subject, properties={"name": subject, "scope_key": SCOPE.key}
    )
    object_node, _ = backend.upsert_node(labels=["Entity"], key=fact, properties={"name": fact, "scope_key": SCOPE.key})
    truth_key = f"{SCOPE.key}:{subject}:{predicate}"
    existing = backend.find_active_truth_relationships(truth_key, scope_key=SCOPE.key)
    properties = {
        "scope_key": SCOPE.key,
        "scope_kind": SCOPE.kind.value,
        "scope_id": SCOPE.scope_id,
        "status": RelationshipStatus.ACTIVE.value,
        "truth_key": truth_key,
        "truth_prefix": f"{SCOPE.key}:{subject}",
        "predicate": predicate,
        "fact": fact,
        "memory_type": "preference",
    }
    if existing:
        # Last-write-wins by uuid: the repeat path.
        return backend.update_relationship(existing[0].uuid, properties=properties)
    return backend.add_relationship(
        source_uuid=subject_node.uuid,
        target_uuid=object_node.uuid,
        relationship_type="REMEMBERS",
        properties=properties,
    )


# ---------------------------------------------------------------------------
# 1. The anchor is authoritative
# ---------------------------------------------------------------------------


def test_rolled_back_operation_leaves_neither_receipt_nor_graph_write(backend) -> None:
    """A failed cross-store operation must leave no trace in either plane.

    The receipt is the commit point, so a receipt visible without its graph write
    would make replay verify an operation that never happened.  Because both
    halves share one transaction anchored in the Operational Store, the rollback
    takes the graph write with it.
    """
    run = backend.receipts.begin_run(
        run_kind="formation",
        job_name="cross-store-rollback",
        scope_key=SCOPE.key,
        effective_policy_digest="policy-digest",
    )

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), backend.transaction():  # noqa: PT012 - the bracket under test spans several statements
        _write_fact(backend, subject="ada", fact="strong coffee")
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
            decision_reason="anchored write",
            decision_result="materialized",
            scope_key=SCOPE.key,
        )
        raise Boom("operation fails after both halves were written")

    assert backend.receipts.receipts_for_scope(SCOPE.key) == [], (
        "a rolled-back operation left a receipt behind; replay would trust it"
    )
    assert backend.find_active_truth_relationships(f"{SCOPE.key}:ada:prefers", scope_key=SCOPE.key) == []
    assert backend.relationships() == []


def test_committed_operation_makes_both_halves_visible_together(backend) -> None:
    """The positive case: one commit publishes the receipt and the graph write."""
    run = backend.receipts.begin_run(
        run_kind="formation",
        job_name="cross-store-commit",
        scope_key=SCOPE.key,
        effective_policy_digest="policy-digest",
    )
    with backend.transaction():
        before = backend.graph_state_hash(SCOPE.key)
        relationship = _write_fact(backend, subject="ada", fact="strong coffee")
        after = backend.graph_state_hash(SCOPE.key)
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
            decision_reason="anchored write",
            decision_result="materialized",
            scope_key=SCOPE.key,
            relationship_uuid=relationship.uuid,
            graph_state_hash_before=before,
            graph_state_hash_after=after,
        )

    receipts = backend.receipts.receipts_for_scope(SCOPE.key)
    assert len(receipts) == 1
    assert receipts[0].relationship_uuid == relationship.uuid
    # The hash the receipt committed to is the hash the store actually holds.
    assert receipts[0].graph_state_hash_after == backend.graph_state_hash(SCOPE.key)
    assert backend.receipts.verify_chain(run.run_uuid).valid


def test_maintenance_run_commits_once_for_many_writes(backend) -> None:
    """One transaction per logical operation, not one per write.

    The SQLite backend committed on every mutating call, so a maintenance pass
    was dozens of commits and a crash could leave it half-applied.  Wrapping the
    pass in ``backend.transaction()`` makes it atomic: either the whole run is
    visible or none of it is.
    """
    facts = [f"fact-{index}" for index in range(12)]

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), backend.transaction():  # noqa: PT012 - the bracket under test spans several statements
        for fact in facts:
            _write_fact(backend, subject="ada", fact=fact, predicate=fact)
        backend.set_job_last_run("maintenance", datetime.now(UTC))
        raise Boom("maintenance fails on its last step")

    assert backend.relationships() == [], "a partially applied maintenance run survived"
    assert backend.get_job_last_run("maintenance") is None

    with backend.transaction():
        for fact in facts:
            _write_fact(backend, subject="ada", fact=fact, predicate=fact)
        backend.set_job_last_run("maintenance", datetime.now(UTC))

    assert len(backend.relationships()) == len(facts)
    assert backend.get_job_last_run("maintenance") is not None


# ---------------------------------------------------------------------------
# 2. Graph writes are safe to repeat
# ---------------------------------------------------------------------------


def test_repeating_the_graph_write_converges_on_the_same_state(backend) -> None:
    """Re-driving the graph half is a no-op, measured by the state hash.

    This is the property that makes crash recovery possible: after a crash the
    operational record is the source of truth, and the graph writes are simply
    re-run.  If repeating them moved the state hash, the recovered store would no
    longer match the hash its receipt committed to and replay would fail.
    """
    with backend.transaction():
        _write_fact(backend, subject="ada", fact="strong coffee")
    settled = backend.graph_state_hash(SCOPE.key)
    relationships = backend.relationships()

    for _ in range(3):
        with backend.transaction():
            _write_fact(backend, subject="ada", fact="strong coffee")

    assert backend.graph_state_hash(SCOPE.key) == settled, (
        "repeating the graph write moved the scope state hash; the write is not idempotent"
    )
    repeated = backend.relationships()
    assert len(repeated) == len(relationships)
    assert {item.uuid for item in repeated} == {item.uuid for item in relationships}
    # Node upserts key on the normalised graph key, so no duplicate entities.
    assert len({node.properties["graph_key"] for node in backend.nodes()}) == len(backend.nodes())


def test_recovery_from_a_lost_graph_write_restores_content_but_not_the_hash(
    backend,
) -> None:
    """Re-driving a *lost* graph row recovers the memory but not the receipted hash.

    In a split deployment the Memory Graph is a separate store, so the anchored
    operational record can survive while the graph write does not.  Re-driving the
    write recovers the memory itself — the truth key resolves again and the fact is
    retrievable — which is what the cross-store rule promises.

    It does not, and cannot, reproduce the state hash the receipt committed to.
    The canonical tuple binds the relationship ``uuid`` (see
    ``graph_state_hash``), and a re-created row is assigned a fresh uuid, so the
    recovered scope hashes differently even though its *content* is identical.
    This is a real boundary on replay after graph-row loss, recorded here rather
    than left to be discovered during an incident: byte replay verifies a chain
    against a store whose rows still carry their original uuids, so a scope
    recovered this way must be re-anchored (a fresh receipted bracket) rather than
    verified against the pre-loss chain.

    The ordinary crash case — a failure *during* the operation — is covered by
    ``test_rolled_back_operation_leaves_neither_receipt_nor_graph_write``: both
    halves share one transaction, so neither survives.
    """
    run = backend.receipts.begin_run(
        run_kind="formation",
        job_name="cross-store-recovery",
        scope_key=SCOPE.key,
        effective_policy_digest="policy-digest",
    )
    with backend.transaction():
        relationship = _write_fact(backend, subject="ada", fact="strong coffee")
        receipted_hash = backend.graph_state_hash(SCOPE.key)
        backend.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
            decision_reason="anchored write",
            decision_result="materialized",
            scope_key=SCOPE.key,
            relationship_uuid=relationship.uuid,
            graph_state_hash_after=receipted_hash,
        )

    # The graph half is lost while the anchor survives.
    backend.delete_relationships([relationship.uuid])
    assert backend.graph_state_hash(SCOPE.key) != receipted_hash
    assert backend.find_active_truth_relationships(f"{SCOPE.key}:ada:prefers", scope_key=SCOPE.key) == []

    # The anchor is still the source of truth for whether the write happened.
    anchored = backend.receipts.receipts_for_scope(SCOPE.key)
    assert len(anchored) == 1, "the anchor must survive the graph-plane loss"
    assert anchored[0].relationship_uuid == relationship.uuid

    # Recovery: re-drive the graph write from the anchored record.
    with backend.transaction():
        recovered = _write_fact(backend, subject="ada", fact="strong coffee")

    # Content is recovered...
    active = backend.find_active_truth_relationships(f"{SCOPE.key}:ada:prefers", scope_key=SCOPE.key)
    assert len(active) == 1
    assert active[0].properties["fact"] == "strong coffee"
    # ...and re-driving the recovery is itself idempotent.
    settled = backend.graph_state_hash(SCOPE.key)
    with backend.transaction():
        _write_fact(backend, subject="ada", fact="strong coffee")
    assert backend.graph_state_hash(SCOPE.key) == settled

    # ...but the identity, and therefore the hash, is new.
    assert recovered.uuid != relationship.uuid
    assert settled != anchored[0].graph_state_hash_after


def test_incremental_state_hash_matches_a_full_recompute(backend) -> None:
    """The memoised hash must equal a rebuild from the relationships themselves.

    The Postgres backend maintains the state hash incrementally.  That is only
    sound while the maintained tuples agree with the underlying rows, so this
    forces a rebuild and compares — the invariant that would silently rot if the
    derived-state maintenance ever missed a write path.
    """
    with backend.transaction():
        for index in range(8):
            _write_fact(backend, subject="ada", fact=f"fact-{index}", predicate=f"p{index}")
        target = _write_fact(backend, subject="ada", fact="revised", predicate="p0")

    backend.mark_relationship(target.uuid, status=RelationshipStatus.SUPERSEDED)
    backend.update_relationship(target.uuid, properties={"observed_count": 4})

    incremental = backend.graph_state_hash(SCOPE.key)
    backend.recompute_scope_state_tuples(SCOPE.key)
    assert backend.graph_state_hash(SCOPE.key) == incremental, (
        "incrementally maintained state hash diverged from a full recompute"
    )
