"""Concurrency acceptance for the Operational Store (Postgres).

These are the automated form of the persistence milestone's concurrency
criterion: *two replicas pass a sustained mix of writes and scheduled
maintenance with no lock errors and no lost updates*.

Running them needs a real Postgres instance, named by
``MEMOTRON_TEST_POSTGRES_DSN``::

    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=5433 user=dw dbname=dw_load"
    pytest tests/test_operational_store_concurrency.py

With that variable unset the whole module skips, so the hermetic offline gate
still passes with no database, no credentials, and no network — the SQLite
substrate is what that gate exercises.

The heavy test drives :mod:`scripts.operational_store_loadtest`, which is the
same generator an operator re-runs against LATEST; there is deliberately one
implementation of the workload rather than a test copy and a script copy.  The
remaining tests are narrow regression guards, each pinned to a specific
concurrency defect that this suite found in the port.  Every one of them fails
against the code as it stood before its fix, which is the only reason to keep a
concurrency test: an assertion that cannot fail proves nothing.  **Do not relax
one to make a run go green** — a failure here is a real defect.
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

# The load generator lives in scripts/ because operators run it directly, and it
# is imported here rather than copied so there is exactly one implementation of
# the workload.  It is imported *before* anything from ``memotron`` because
# importing it also puts this checkout's ``src/`` at the front of ``sys.path``.
SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import operational_store_loadtest as loadtest  # noqa: E402
from memotron.models import MemoryScope, RelationshipStatus, ScopeKind  # noqa: E402

DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"
DSN = os.environ.get(DSN_ENV, "")

# `postgres` makes the CI lane selectable with `-m postgres`; the skipif keeps
# the detailed reason this file has always reported.
pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not DSN,
        reason=(
            f"{DSN_ENV} is not set; the Operational Store concurrency proof needs a real "
            "Postgres instance. The hermetic suite runs on the SQLite substrate."
        ),
    ),
]

#: Seconds of sustained two-replica load in the acceptance test.  The published
#: numbers in docs/operational-store-sizing.md come from a longer run of the
#: script itself; this default keeps the suite usable while staying real load.
LOAD_SECONDS = float(os.environ.get("MEMOTRON_TEST_LOADTEST_SECONDS", "20"))

#: Seconds each targeted race regression hammers its specific window.
RACE_SECONDS = float(os.environ.get("MEMOTRON_TEST_RACE_SECONDS", "6"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def backend():
    from memotron.storage.postgres import PostgresStorageBackend

    instance = PostgresStorageBackend(DSN, application_name="dw-concurrency-test", min_size=2, max_size=12)
    try:
        yield instance
    finally:
        instance.close()


def _unique_scope(label: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.AGENT, scope_id=f"{label}-{uuid.uuid4().hex[:10]}")


def _fact_props(scope: MemoryScope, fact: str, **extra) -> dict:
    props = {
        "scope_key": scope.key,
        "scope_kind": scope.kind.value,
        "scope_id": scope.scope_id,
        "status": RelationshipStatus.ACTIVE.value,
        "memory_type": "semantic",
        "predicate": "prefers",
        "fact": fact,
        "observed_count": 1,
        "active_in_context": True,
    }
    props.update(extra)
    return props


class _Racers:
    """Run *fn(index)* on N threads for a bounded time, classifying failures."""

    def __init__(self, threads: int, seconds: float) -> None:
        self.threads = threads
        self.seconds = seconds
        self.lock_errors: list[str] = []
        self.other_errors: list[BaseException] = []
        self.iterations = 0
        self._guard = threading.Lock()

    def run(self, fn) -> None:
        deadline = time.monotonic() + self.seconds

        def loop(index: int) -> None:
            while time.monotonic() < deadline:
                try:
                    fn(index)
                except BaseException as exc:
                    label = loadtest.classify_lock_error(exc)
                    with self._guard:
                        if label is not None:
                            self.lock_errors.append(label)
                        else:
                            self.other_errors.append(exc)
                        if len(self.lock_errors) + len(self.other_errors) > 40:
                            return
                else:
                    with self._guard:
                        self.iterations += 1

        workers = [threading.Thread(target=loop, args=(i,), name=f"racer-{i}") for i in range(self.threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=self.seconds + 120)

    def assert_clean(self) -> None:
        if self.other_errors:
            raise self.other_errors[0]
        assert self.lock_errors == [], (
            f"{len(self.lock_errors)} lock errors surfaced to the caller across "
            f"{self.iterations} operations: {sorted(set(self.lock_errors))}"
        )
        assert self.iterations > 0, "the race did no work; the test proves nothing"


# ---------------------------------------------------------------------------
# The acceptance criterion
# ---------------------------------------------------------------------------


def test_two_replicas_sustain_mixed_writes_and_maintenance() -> None:
    """Acceptance: two replica processes, one database, no lock errors, no lost updates.

    Everything this asserts is asserted by the generator itself; the test
    surfaces its report so a failure names the invariant that broke.
    """
    config = loadtest.LoadTestConfig(
        dsn=DSN,
        replicas=2,
        duration_seconds=LOAD_SECONDS,
        workers=3,
        pool_min=2,
        pool_max=10,
        scopes=3,
        seed_relationships=90,
    )
    report = loadtest.run_loadtest(config)
    print("\n" + loadtest.format_report(report))

    # Named separately from the catch-all so a regression reads as itself.
    assert report.lock_errors == [], (
        f"{len(report.lock_errors)} lock errors under two-replica load: "
        f"{sorted({err['lock_error'] for err in report.lock_errors})}"
    )
    assert report.other_errors == [], (
        f"{len(report.other_errors)} operations failed: {sorted({err['type'] for err in report.other_errors})}"
    )
    assert report.ok, "invariants failed:\n  " + "\n  ".join(report.failures)

    # The run has to have been substantial, or the invariants are vacuous.
    committed = sum(sum(replica.committed.values()) for replica in report.replicas)
    assert committed > 200, f"only {committed} operations committed; not a sustained mix"
    assert len(report.replicas) == 2

    # And the negative control has to have been genuinely at risk: if the
    # unlocked shape lost nothing either, the machine was not actually
    # contended and the locked result is not evidence.
    assert report.diagnostics["control_attempts"] > 0


# ---------------------------------------------------------------------------
# Regression guards, one per defect found
# ---------------------------------------------------------------------------


def test_state_hash_memo_never_publishes_a_stale_value(backend) -> None:
    """The memo must never be marked clean while holding a superseded hash.

    ``graph_state_hash`` reads the tuple aggregate and then writes the result
    back with ``dirty = false``.  A writer that commits its tuple *and* its dirty
    mark inside that window used to be erased by the write-back: the scope was
    left memoised-clean with a hash that omitted the writer's relationship, and
    every later call returned it.  A receipt bracket would then commit to a state
    hash the graph never had.

    The window is the width of the aggregate, so the scope is seeded first — a
    realistically sized scope is what makes this reproducible rather than rare.
    """
    scope = _unique_scope("statehash-race")
    node, _ = backend.upsert_node(
        labels=["Entity"],
        key=f"{scope.key}:subject",
        properties={"name": "subject", "scope_key": scope.key},
    )
    with backend.transaction():
        for i in range(2500):
            backend.add_relationship(
                source_uuid=node.uuid,
                target_uuid=node.uuid,
                relationship_type="FACT",
                properties=_fact_props(scope, f"seed {i}"),
            )

    stop = threading.Event()
    reader_errors: list[BaseException] = []

    def reader() -> None:
        try:
            while not stop.is_set():
                backend.graph_state_hash(scope.key)
        except BaseException as exc:
            reader_errors.append(exc)

    readers = [threading.Thread(target=reader, daemon=True) for _ in range(3)]
    for thread in readers:
        thread.start()

    writers = _Racers(threads=2, seconds=RACE_SECONDS)
    counter = iter(range(1_000_000))
    guard = threading.Lock()

    def write(_index: int) -> None:
        with guard:
            i = next(counter)
        backend.add_relationship(
            source_uuid=node.uuid,
            target_uuid=node.uuid,
            relationship_type="FACT",
            properties=_fact_props(scope, f"concurrent {i}"),
        )

    writers.run(write)
    # Let the readers keep going briefly after the last commit: the failure is a
    # stale publish landing *after* the final write.
    time.sleep(0.5)
    stop.set()
    for thread in readers:
        thread.join(timeout=30)

    assert not reader_errors, f"reader failed: {reader_errors[0]!r}"
    writers.assert_clean()

    memoised = backend.graph_state_hash(scope.key)
    backend.recompute_scope_state_tuples(scope.key)
    rebuilt = backend.graph_state_hash(scope.key)
    assert memoised == rebuilt, (
        "the incrementally maintained state hash disagrees with a full recompute "
        f"of {scope.key}: memoised {memoised} vs rebuilt {rebuilt}. A concurrent "
        "reader published a stale hash and marked the scope clean."
    )


def test_cross_referencing_fact_writes_do_not_deadlock(backend) -> None:
    """Two replicas writing facts that point at each other's subject must not deadlock.

    ``upsert_node`` takes a row lock for its property merge, and
    ``INSERT INTO relationships`` takes ``FOR KEY SHARE`` on the node rows its
    foreign keys reference.  While the merge used the stronger ``FOR UPDATE``
    those two conflicted: a writer holding subject A and inserting an edge to B
    deadlocked against a writer holding B and inserting an edge to A.  Nothing in
    that pair is a shared row from the caller's point of view; it is two ordinary
    fact writes.
    """
    scope = _unique_scope("fk-deadlock")
    subjects = []
    for i in range(2):
        node, _ = backend.upsert_node(
            labels=["Entity"],
            key=f"{scope.key}:subject-{i}",
            properties={"name": f"subject-{i}", "scope_key": scope.key},
        )
        subjects.append(node.uuid)

    racers = _Racers(threads=2, seconds=RACE_SECONDS)

    def write(index: int) -> None:
        # Thread 0 walks 0 -> 1, thread 1 walks 1 -> 0: opposite lock order.
        mine, theirs = (0, 1) if index % 2 == 0 else (1, 0)
        with backend.transaction():
            node, _ = backend.upsert_node(
                labels=["Entity"],
                key=f"{scope.key}:subject-{mine}",
                properties={
                    "name": f"subject-{mine}",
                    "scope_key": scope.key,
                    "touched_by": str(index),
                },
            )
            backend.add_relationship(
                source_uuid=node.uuid,
                target_uuid=subjects[theirs],
                relationship_type="FACT",
                properties=_fact_props(scope, f"cross edge {index}"),
            )

    racers.run(write)
    racers.assert_clean()


def test_multi_relationship_transactions_do_not_deadlock_single_writers(backend) -> None:
    """A maintenance pass and a single-row writer must not deadlock over the scope memo.

    Every write to a scope has to invalidate that scope's state-hash memo — one
    row shared by all of them.  Writing it inline meant a maintenance pass locked
    it while working on its first relationship and held it to COMMIT, then waited
    for its second relationship, while a concurrent single-relationship writer
    held that relationship and waited for the memo row.  The invalidation is now
    deferred to just before COMMIT, so the shared row is never held across
    another wait.
    """
    scope = _unique_scope("scope-memo-deadlock")
    node, _ = backend.upsert_node(
        labels=["Entity"],
        key=f"{scope.key}:subject",
        properties={"name": "subject", "scope_key": scope.key},
    )
    relationships = []
    with backend.transaction():
        for i in range(12):
            relationships.append(  # noqa: PERF401 - inside a transaction bracket, kept explicit
                backend.add_relationship(
                    source_uuid=node.uuid,
                    target_uuid=node.uuid,
                    relationship_type="FACT",
                    properties=_fact_props(scope, f"member {i}"),
                ).uuid
            )
    relationships.sort()

    racers = _Racers(threads=4, seconds=RACE_SECONDS)

    def work(index: int) -> None:
        if index % 2 == 0:
            # Maintenance-shaped: several relationships, one commit.
            with backend.transaction():
                members = backend.context_visible_relationships(scope=scope)
                for member in sorted(members, key=lambda item: item.uuid)[:5]:
                    backend.update_relationship(member.uuid, properties={"swept_by": str(index)})
                backend.graph_state_hash(scope.key)
        else:
            # Single-row writer racing the sweep.
            target = relationships[index % len(relationships)]
            backend.update_relationship(target, properties={"poked_by": str(index)})

    racers.run(work)
    racers.assert_clean()

    memoised = backend.graph_state_hash(scope.key)
    backend.recompute_scope_state_tuples(scope.key)
    assert memoised == backend.graph_state_hash(scope.key)


def test_contended_increment_under_the_row_lock_loses_nothing(backend) -> None:
    """N concurrent increments of one counter land exactly N times.

    The read has to happen under the row lock the write will use, which on this
    interface means ``update_relationship`` with no payload (a public
    ``SELECT ... FOR UPDATE`` returning the row it locked) inside the same
    transaction as the write.
    """
    scope = _unique_scope("contended-counter")
    node, _ = backend.upsert_node(
        labels=["Entity"],
        key=f"{scope.key}:subject",
        properties={"name": "subject", "scope_key": scope.key},
    )
    target = backend.add_relationship(
        source_uuid=node.uuid,
        target_uuid=node.uuid,
        relationship_type="FACT",
        properties=_fact_props(scope, "the contended fact"),
    )

    racers = _Racers(threads=6, seconds=RACE_SECONDS)

    def increment(_index: int) -> None:
        with backend.transaction():
            locked = backend.update_relationship(target.uuid)
            current = int(locked.properties.get("observed_count", 1))
            backend.update_relationship(target.uuid, properties={"observed_count": current + 1})

    racers.run(increment)
    racers.assert_clean()

    final = int(backend.get_relationship(target.uuid).properties["observed_count"])
    assert final == 1 + racers.iterations, (
        f"observed_count is {final} after {racers.iterations} committed increments "
        f"from a seed of 1; {1 + racers.iterations - final} were lost"
    )


def test_state_tuple_is_not_rebuilt_from_a_stale_relationship(backend) -> None:
    """Renaming a subject must not resurrect superseded relationship properties.

    Renaming rebuilds the state tuple of every relationship the node is the
    subject of.  That rebuild reads those relationships; without the row lock it
    read them as they were before a concurrent mutation committed and then wrote
    a tuple derived from the superseded properties, so the state hash described a
    graph that no longer existed.  The tuple row itself serialised the two
    writes, which is what made the stale one win.
    """
    scope = _unique_scope("tuple-staleness")
    node, _ = backend.upsert_node(
        labels=["Entity"],
        key=f"{scope.key}:subject",
        properties={"name": "subject-0", "scope_key": scope.key},
    )
    relationships = [
        backend.add_relationship(
            source_uuid=node.uuid,
            target_uuid=node.uuid,
            relationship_type="FACT",
            properties=_fact_props(scope, f"member {i}"),
        ).uuid
        for i in range(25)
    ]

    racers = _Racers(threads=4, seconds=RACE_SECONDS)
    names = iter(range(1_000_000))
    guard = threading.Lock()

    def work(index: int) -> None:
        if index % 2 == 0:
            with guard:
                n = next(names)
            backend.upsert_node(
                labels=["Entity"],
                key=f"{scope.key}:subject",
                properties={"name": f"subject-{n}", "scope_key": scope.key},
            )
        else:
            target = relationships[index % len(relationships)]
            backend.update_relationship(target, properties={"confidence": round(time.time() % 1, 6)})

    racers.run(work)
    racers.assert_clean()

    memoised = backend.graph_state_hash(scope.key)
    backend.recompute_scope_state_tuples(scope.key)
    rebuilt = backend.graph_state_hash(scope.key)
    assert memoised == rebuilt, (
        f"state tuples disagree with the relationships they are derived from: memoised {memoised} vs rebuilt {rebuilt}"
    )


def test_receipt_chain_stays_dense_under_concurrent_emitters(backend) -> None:
    """Concurrent writers on one run produce a dense, correctly linked chain."""
    from memotron.storage.receipts import GENESIS_RECEIPT_HASH, ReceiptDecisionType

    scope = _unique_scope("receipt-chain")
    run_uuid = uuid.uuid4().hex

    def handle():
        return loadtest.ReceiptRun(
            run_uuid=run_uuid,
            run_kind="consolidation",
            job_name="concurrency-test",
            scope_key=scope.key,
            effective_policy_digest="test-policy-digest",
        )

    racers = _Racers(threads=6, seconds=RACE_SECONDS)
    local = threading.local()

    def emit(index: int) -> None:
        if not hasattr(local, "run"):
            local.run = handle()
        backend.receipts.emit(
            local.run,
            decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
            decision_reason=f"thread {index}",
            decision_result="materialized",
        )

    racers.run(emit)
    racers.assert_clean()

    receipts = backend.receipts.receipts_for_run(run_uuid)
    indices = [receipt.event_index for receipt in receipts]
    assert len(receipts) == racers.iterations, (
        f"{len(receipts)} receipts persisted for {racers.iterations} successful emits"
    )
    assert indices == list(range(len(receipts))), (
        "event_index is not dense: "
        f"{len(indices) - len(set(indices))} duplicates, "
        f"max {max(indices) if indices else -1} over {len(indices)} receipts"
    )
    previous = GENESIS_RECEIPT_HASH
    for receipt in receipts:
        assert receipt.previous_receipt_hash == previous, f"chain break at event_index {receipt.event_index}"
        previous = receipt.receipt_hash


def test_replicas_can_migrate_concurrently(backend) -> None:
    """Every replica runs migrations on startup; they must serialise, not race."""
    from memotron.storage.postgres import PostgresStorageBackend

    baseline = backend.schema_version()
    built: list[object] = []
    failures: list[BaseException] = []
    guard = threading.Lock()

    def start(index: int) -> None:
        try:
            instance = PostgresStorageBackend(DSN, application_name=f"dw-migrate-race-{index}", min_size=1, max_size=2)
        except BaseException as exc:
            with guard:
                failures.append(exc)
        else:
            with guard:
                built.append(instance)

    threads = [threading.Thread(target=start, args=(i,)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    try:
        assert not failures, f"concurrent startup failed: {failures[0]!r}"
        assert len(built) == 4
        assert {instance.schema_version() for instance in built} == {baseline}
    finally:
        for instance in built:
            instance.close()
