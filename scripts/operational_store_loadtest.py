#!/usr/bin/env python3
"""Two-replica concurrency proof for the Operational Store (Postgres).

What this proves
----------------
The persistence milestone claims that *two replicas pass a sustained mix of
writes and scheduled maintenance with no lock errors and no lost updates*.
"Two replicas" is taken literally: this generator forks N **separate OS
processes** with :mod:`multiprocessing` (spawn context, so nothing is
inherited), each of which builds its own :class:`PostgresStorageBackend` with
its own connection pool, its own event-loop thread, and its own migration
attempt, all pointed at **one** database.  Threads inside one interpreter would
not prove the claim: they would share a pool, a GIL, and a process-local
transaction registry.

Each replica runs a sustained mix, driven entirely through the public
``StorageBackend`` surface:

``fact``          the synchronous fact-write path — truth-key dedup probe
                  (``find_active_truth_relationships``), ``upsert_node`` onto a
                  small shared subject pool so replicas contend on the same
                  ``graph_key``, then ``add_relationship``; one transaction.
``reinforce``     the contended read-modify-write: probe the *shared* hot truth
                  key, then increment ``observed_count`` on the single hot
                  relationship every worker in every replica is fighting over.
``control``       the same increment written the way a caller that ignores the
                  locking rule would write it (read in one interface call, write
                  in another, no enclosing transaction).  This is a **negative
                  control**, not an assertion: it exists to show the invariant
                  above can actually fail, so a passing ``reinforce`` means
                  something.
``event``         use/outcome event appends (the idempotent append plane).
``receipt``       a receipted mutation: state-hash bracket around a relationship
                  mutation with ``receipts.emit`` in the middle, one transaction.
                  Every replica emits onto **one shared run**, so the receipt
                  chain is written concurrently by all of them.
``maintenance``   a scheduled maintenance pass of the kind a dream job runs —
                  scan a scope, mutate several relationships, record the job run,
                  the decision log entry, and the job watermark — wrapped in
                  ``backend.transaction()`` so the whole pass commits once.

Invariants checked afterwards
-----------------------------
* **Zero lock errors.**  Deadlocks (40P01), lock timeouts (55P03), serialization
  failures (40001), statement cancellations (57014), pool timeouts, and engine
  call timeouts are classified out of every exception chain and counted.  Any
  non-zero count fails the run.
* **No lost updates, append form.**  Every relationship, use event, outcome
  event, receipt, and job run a replica committed is present exactly once; the
  persisted count equals the sum of the per-replica committed counts.
* **No lost updates, counter form.**  ``observed_count`` on the single hot
  relationship equals its seed value plus the total number of increments every
  replica committed.  One lost increment fails this.
* **Chain integrity.**  Receipts on the shared run form a dense ``event_index``
  sequence 0..n-1 with no gap and no duplicate, every ``previous_receipt_hash``
  links to its predecessor's ``receipt_hash``, the head links to genesis, and
  the ledger's own ``verify_chain`` accepts the checkpointed run.
* **State-hash correctness.**  For every scope, the incrementally maintained
  ``graph_state_hash`` equals a full rebuild of the same scope via
  ``recompute_scope_state_tuples``.

Usage
-----
    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=5433 user=dw dbname=dw_load"
    python scripts/operational_store_loadtest.py --replicas 2 --duration 60 --workers 4

Exit status is 0 only when every invariant holds.  Re-run this against LATEST to
re-certify; the numbers in ``docs/operational-store-sizing.md`` came from this
script.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import random
import statistics
import sys
import tempfile
import threading
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from memotron.models import (
    DreamDecisionRecord,
    DreamJobKind,
    DreamJobRunRecord,
    MemoryScope,
    OutcomeEvent,
    OutcomeVerdict,
    RelationshipStatus,
    ScopeKind,
    UseEvent,
    UseEventKind,
)
from memotron.storage.receipts import (
    GENESIS_RECEIPT_HASH,
    ReceiptDecisionType,
    ReceiptRun,
)

DEFAULT_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"

#: Operation kinds, in the mix weights each worker cycles through.
OP_WEIGHTS: dict[str, int] = {
    "fact": 4,
    "reinforce": 3,
    "event": 3,
    "receipt": 2,
    "maintenance": 1,
    "control": 1,
}

OP_ORDER: tuple[str, ...] = ("fact", "reinforce", "event", "receipt", "maintenance", "control")

#: SQLSTATEs that mean "the engine refused to let this caller take a lock".
#: 40P01 deadlock_detected, 40001 serialization_failure, 55P03 lock_not_available,
#: 57014 query_canceled (which is how ``statement_timeout`` and a cancelled
#: ``lock_timeout`` surface), 55006 object_in_use.
LOCK_SQLSTATES: dict[str, str] = {
    "40P01": "deadlock_detected",
    "40001": "serialization_failure",
    "55P03": "lock_not_available",
    "57014": "query_canceled",
    "55006": "object_in_use",
}

#: Exception *types* that are lock/contention failures without carrying a
#: SQLSTATE: the pool gave up handing out a connection, or the engine's
#: synchronous bridge timed out waiting for the loop thread.
LOCK_EXCEPTION_TYPES: frozenset[str] = frozenset({"PoolTimeout", "PoolClosed", "PostgresEngineError"})

HOT_TRUTH_KEY_SUFFIX = "hot-contended-fact"
CONTROL_TRUTH_KEY_SUFFIX = "hot-control-fact"
SUBJECTS_PER_SCOPE = 8
MAINTENANCE_MUTATIONS = 5


# ---------------------------------------------------------------------------
# Configuration and results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoadTestConfig:
    dsn: str
    replicas: int = 2
    duration_seconds: float = 60.0
    workers: int = 4
    pool_min: int = 2
    pool_max: int = 10
    scopes: int = 3
    seed_relationships: int = 200
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])

    def as_dict(self) -> dict[str, Any]:
        return {
            "replicas": self.replicas,
            "duration_seconds": self.duration_seconds,
            "workers_per_replica": self.workers,
            "pool_min": self.pool_min,
            "pool_max": self.pool_max,
            "scopes": self.scopes,
            "seed_relationships": self.seed_relationships,
            "run_id": self.run_id,
            "connections_budget": self.replicas * self.pool_max,
        }


@dataclass
class ReplicaResult:
    """Everything one replica process reports back to the parent."""

    replica: int
    pid: int
    attempted: dict[str, int]
    committed: dict[str, int]
    latencies_ms: dict[str, list[float]]
    lock_errors: list[dict[str, str]]
    other_errors: list[dict[str, str]]
    #: Increments this replica committed on the hot relationship, under the lock.
    hot_increments: int
    #: Increments attempted on the negative-control relationship (no lock).
    control_increments: int
    relationship_uuids: list[str]
    receipts_emitted: int
    use_events: int
    outcome_events: int
    job_runs: int
    task_run_id: str
    pool_stats: dict[str, Any]
    started_at: float
    finished_at: float

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> ReplicaResult:
        return cls(**payload)


@dataclass
class LoadTestReport:
    config: LoadTestConfig
    replicas: list[ReplicaResult]
    invariants: list[tuple[str, bool, str]]
    wall_seconds: float
    server_version: str
    pgvector_enabled: bool
    #: Measurements that are reported but not asserted on (the negative control).
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[str]:
        return [f"{name}: {detail}" for name, ok, detail in self.invariants if not ok]

    @property
    def ok(self) -> bool:
        return not self.failures

    @property
    def lock_errors(self) -> list[dict[str, str]]:
        return [err for replica in self.replicas for err in replica.lock_errors]

    @property
    def other_errors(self) -> list[dict[str, str]]:
        return [err for replica in self.replicas for err in replica.other_errors]


# ---------------------------------------------------------------------------
# Error classification
# ---------------------------------------------------------------------------


def classify_lock_error(exc: BaseException) -> str | None:
    """Return a label if *exc* (or anything it wraps) is a lock/contention failure.

    The port deliberately re-raises database errors as interface errors
    (``UniqueViolation`` becomes ``ValueError``/``RuntimeError``), so the whole
    ``__cause__``/``__context__`` chain is walked rather than just the outermost
    type.  Returning ``None`` means "this was not the database refusing a lock".
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(seen) < 16:
        seen.add(id(current))
        name = type(current).__name__
        sqlstate = getattr(current, "sqlstate", None)
        if sqlstate in LOCK_SQLSTATES:
            return f"{LOCK_SQLSTATES[sqlstate]} ({sqlstate}) via {name}"
        if name in LOCK_EXCEPTION_TYPES:
            return f"{name}: {current}"
        current = current.__cause__ or current.__context__
    return None


# ---------------------------------------------------------------------------
# Fixture: what the parent builds before any replica starts
# ---------------------------------------------------------------------------


def _scope_for(run_id: str, index: int) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.AGENT, scope_id=f"loadtest-{run_id}-{index}")


def _fact_properties(
    *,
    scope: MemoryScope,
    run_id: str,
    truth_key: str,
    fact: str,
    op: str,
    replica: int,
    observed_count: int = 1,
) -> dict[str, Any]:
    return {
        "scope_key": scope.key,
        "scope_kind": scope.kind.value,
        "scope_id": scope.scope_id,
        "status": RelationshipStatus.ACTIVE.value,
        "memory_type": "semantic",
        "predicate": "prefers",
        "fact": fact,
        "truth_key": truth_key,
        "truth_prefix": truth_key.rsplit(":", 1)[0],
        "observed_count": observed_count,
        "active_in_context": True,
        "loadtest_run": run_id,
        "loadtest_op": op,
        "loadtest_replica": replica,
    }


def build_fixture(backend: Any, config: LoadTestConfig) -> dict[str, Any]:
    """Create the scopes, subject pool, hot rows, and shared run the replicas use.

    Returns a plain picklable dict; ``spawn`` children get it as an argument.
    """
    run_id = config.run_id
    scopes = [_scope_for(run_id, i) for i in range(config.scopes)]
    subjects: dict[str, list[str]] = {}
    seeded = 0

    for scope in scopes:
        uuids: list[str] = []
        with backend.transaction():
            for s in range(SUBJECTS_PER_SCOPE):
                node, _ = backend.upsert_node(
                    labels=["Entity"],
                    key=f"{scope.key}:subject-{s}",
                    properties={"name": f"subject-{s}", "scope_key": scope.key},
                )
                uuids.append(node.uuid)
        subjects[scope.key] = uuids

        # Background volume so the scans and aggregates are not measured on an
        # empty table.
        per_scope = max(config.seed_relationships // max(len(scopes), 1), 1)
        with backend.transaction():
            for i in range(per_scope):
                backend.add_relationship(
                    source_uuid=uuids[i % len(uuids)],
                    target_uuid=uuids[(i + 1) % len(uuids)],
                    relationship_type="FACT",
                    properties=_fact_properties(
                        scope=scope,
                        run_id=run_id,
                        truth_key=f"{scope.key}:seed:{i}",
                        fact=f"seed fact {i}",
                        op="seed",
                        replica=-1,
                    ),
                )
                seeded += 1

    # The two hot rows both live in scope 0 so every replica contends on one row.
    hot_scope = scopes[0]
    hot_truth_key = f"{hot_scope.key}:{HOT_TRUTH_KEY_SUFFIX}"
    control_truth_key = f"{hot_scope.key}:{CONTROL_TRUTH_KEY_SUFFIX}"
    with backend.transaction():
        hot = backend.add_relationship(
            source_uuid=subjects[hot_scope.key][0],
            target_uuid=subjects[hot_scope.key][1],
            relationship_type="FACT",
            properties=_fact_properties(
                scope=hot_scope,
                run_id=run_id,
                truth_key=hot_truth_key,
                fact="the contended fact",
                op="hot",
                replica=-1,
            ),
        )
        control = backend.add_relationship(
            source_uuid=subjects[hot_scope.key][0],
            target_uuid=subjects[hot_scope.key][2],
            relationship_type="FACT",
            properties=_fact_properties(
                scope=hot_scope,
                run_id=run_id,
                truth_key=control_truth_key,
                fact="the negative control fact",
                op="control",
                replica=-1,
            ),
        )

    return {
        "run_id": run_id,
        "scope_keys": [scope.key for scope in scopes],
        "scope_ids": [scope.scope_id for scope in scopes],
        "subjects": subjects,
        "hot_relationship_uuid": hot.uuid,
        "hot_truth_key": hot_truth_key,
        "hot_scope_key": hot_scope.key,
        "control_relationship_uuid": control.uuid,
        "control_truth_key": control_truth_key,
        "shared_run_uuid": uuid.uuid4().hex,
        "seeded_relationships": seeded + 2,
    }


# ---------------------------------------------------------------------------
# The replica process
# ---------------------------------------------------------------------------


class _Replica:
    """One replica's workload.  Lives entirely inside one child process."""

    def __init__(self, backend: Any, config: LoadTestConfig, fixture: dict[str, Any], replica: int):
        self.backend = backend
        self.config = config
        self.fixture = fixture
        self.replica = replica
        self.run_id = fixture["run_id"]
        self.task_run_id = f"{self.run_id}-r{replica}"
        self.scopes = [MemoryScope(kind=ScopeKind.AGENT, scope_id=scope_id) for scope_id in fixture["scope_ids"]]
        self.subjects: dict[str, list[str]] = fixture["subjects"]
        self.hot_uuid: str = fixture["hot_relationship_uuid"]
        self.hot_truth_key: str = fixture["hot_truth_key"]
        self.hot_scope_key: str = fixture["hot_scope_key"]
        self.control_uuid: str = fixture["control_relationship_uuid"]

        self._lock = threading.Lock()
        self.attempted: Counter[str] = Counter()
        self.committed: Counter[str] = Counter()
        self.latencies: dict[str, list[float]] = {op: [] for op in OP_ORDER}
        self.lock_errors: list[dict[str, str]] = []
        self.other_errors: list[dict[str, str]] = []
        self.hot_increments = 0
        self.control_increments = 0
        self.created_relationships: list[str] = []
        self.receipts_emitted = 0
        self.use_event_count = 0
        self.outcome_event_count = 0
        self.job_runs = 0
        self._sequence = 0

        # One shared run for the whole fleet: every replica appends onto the same
        # hash chain, which is the property `emit` claims to hold.
        self.receipt_run = ReceiptRun(
            run_uuid=fixture["shared_run_uuid"],
            run_kind="consolidation",
            job_name="operational-store-loadtest",
            scope_key=self.scopes[0].key,
            effective_policy_digest="loadtest-policy-digest",
            agent_id=f"replica-{replica}",
        )

    # -- helpers ---------------------------------------------------------

    def _next(self) -> int:
        with self._lock:
            self._sequence += 1
            return self._sequence

    def _scope(self, rng: random.Random) -> MemoryScope:
        return rng.choice(self.scopes)

    def _record_relationship(self, uuid_value: str) -> None:
        with self._lock:
            self.created_relationships.append(uuid_value)

    def _sample_relationship(self, rng: random.Random) -> str | None:
        with self._lock:
            if not self.created_relationships:
                return None
            return rng.choice(self.created_relationships)

    # -- operations ------------------------------------------------------

    def op_fact(self, rng: random.Random) -> None:
        """Synchronous fact write: dedup probe, subject upsert, relationship insert."""
        scope = self._scope(rng)
        n = self._next()
        truth_key = f"{scope.key}:fact:{self.replica}:{n}"
        with self.backend.transaction():
            # The dedup probe paid on every synchronous fact write.  This is an
            # index seek in the port; the assertion below is what makes it a
            # probe rather than decoration.
            duplicates = self.backend.find_active_truth_relationships(truth_key, scope_key=scope.key)
            if duplicates:
                raise AssertionError(f"unique truth key {truth_key!r} already had {len(duplicates)} active rows")
            subject_index = rng.randrange(SUBJECTS_PER_SCOPE)
            node, _ = self.backend.upsert_node(
                labels=["Entity"],
                key=f"{scope.key}:subject-{subject_index}",
                properties={
                    "name": f"subject-{subject_index}",
                    "scope_key": scope.key,
                    "last_writer": f"replica-{self.replica}",
                },
            )
            target = rng.choice(self.subjects[scope.key])
            relationship = self.backend.add_relationship(
                source_uuid=node.uuid,
                target_uuid=target,
                relationship_type="FACT",
                properties=_fact_properties(
                    scope=scope,
                    run_id=self.run_id,
                    truth_key=truth_key,
                    fact=f"replica {self.replica} fact {n}",
                    op="fact",
                    replica=self.replica,
                ),
            )
        self._record_relationship(relationship.uuid)

    def op_reinforce(self, rng: random.Random) -> None:
        """Contended increment on the ONE hot relationship, done correctly.

        The correct shape is read-under-lock: ``update_relationship`` with no
        property payload is a public ``SELECT ... FOR UPDATE`` that returns the
        row it locked, so the increment is computed from a value no other writer
        can be holding.  The second call merges the new count while this
        transaction still owns the row lock.
        """
        with self.backend.transaction():
            found = self.backend.find_active_truth_relationships(self.hot_truth_key, scope_key=self.hot_scope_key)
            if len(found) != 1:
                raise AssertionError(f"hot truth key resolved to {len(found)} active rows, expected exactly 1")
            locked = self.backend.update_relationship(self.hot_uuid)
            current = int(locked.properties.get("observed_count", 1))
            self.backend.update_relationship(
                self.hot_uuid,
                properties={
                    "observed_count": current + 1,
                    "last_reinforced_by": f"replica-{self.replica}",
                },
            )
        with self._lock:
            self.hot_increments += 1

    def op_control(self, rng: random.Random) -> None:
        """NEGATIVE CONTROL: the same increment written without the lock.

        Two interface calls, no enclosing transaction — precisely what the
        contract says a caller may *not* assume is atomic.  Nothing asserts on
        the result; the run reports how many increments this shape loses, which
        is the evidence that ``op_reinforce`` is testing something real.
        """
        relationship = self.backend.get_relationship(self.control_uuid)
        current = int(relationship.properties.get("observed_count", 1))
        self.backend.update_relationship(self.control_uuid, properties={"observed_count": current + 1})
        with self._lock:
            self.control_increments += 1

    def op_event(self, rng: random.Random) -> None:
        """Use/outcome append plane."""
        relationship_uuid = self._sample_relationship(rng) or self.hot_uuid
        relationship = self.backend.get_relationship(relationship_uuid)
        scope_key = str(relationship.properties["scope_key"])
        scope = next(item for item in self.scopes if item.key == scope_key)
        n = self._next()
        use = self.backend.record_use_event(
            UseEvent(
                relationship_uuid=relationship_uuid,
                scope=scope,
                kind=UseEventKind.RETRIEVED,
                task_run_id=self.task_run_id,
                idempotency_key=f"{self.task_run_id}:use:{n}",
                rank=rng.randrange(10),
                retrieval_score=rng.random(),
                candidate_set_size=20,
                context_budget_competition=5,
                retrieval_policy_digest="loadtest-retrieval-digest",
            )
        )
        with self._lock:
            self.use_event_count += 1
        self.backend.record_outcome_event(
            OutcomeEvent(
                use_id=use.use_id,
                scope=scope,
                verdict=rng.choice(list(OutcomeVerdict)),
                judge_identity="loadtest-judge",
                judge_version="1",
                task_run_id=self.task_run_id,
                idempotency_key=f"{self.task_run_id}:outcome:{n}",
            )
        )
        with self._lock:
            self.outcome_event_count += 1

    def op_receipt(self, rng: random.Random) -> None:
        """Receipted mutation: state-hash bracket around a mutation, one commit."""
        relationship_uuid = self._sample_relationship(rng)
        if relationship_uuid is None:
            return self.op_fact(rng)
        relationship = self.backend.get_relationship(relationship_uuid)
        scope_key = str(relationship.properties["scope_key"])
        n = self._next()
        with self.backend.transaction():
            before = self.backend.graph_state_hash(scope_key)
            self.backend.update_relationship(
                relationship_uuid,
                properties={
                    "confidence": round(rng.random(), 4),
                    "receipted_by": f"replica-{self.replica}",
                },
            )
            self.backend.receipts.emit(
                self.receipt_run,
                decision_type=ReceiptDecisionType.FORMATION_SEMANTIC_DEDUP_REINFORCED,
                decision_reason=f"loadtest replica {self.replica} mutation {n}",
                decision_result="reinforced",
                scope_key=scope_key,
                relationship_uuid=relationship_uuid,
                graph_state_hash_before=before,
            )
            after = self.backend.graph_state_hash(scope_key)
            if not after:
                raise AssertionError("graph_state_hash returned empty after mutation")
        with self._lock:
            self.receipts_emitted += 1
        return None

    def op_maintenance(self, rng: random.Random) -> None:
        """A scheduled maintenance pass: scan a scope, mutate, record the run.

        Everything is inside one ``backend.transaction()``, so the whole pass is
        one commit — the thing the re-entrant transaction scope exists for.
        """
        scope = self._scope(rng)
        ran_at = datetime.now(UTC)
        with self.backend.transaction():
            members = self.backend.context_visible_relationships(scope=scope)
            mutated = 0
            # Caller-side lock discipline: a transaction that locks several
            # relationships must take them in an order every other writer also
            # uses, or two maintenance passes over overlapping sets deadlock each
            # other.  uuid order is the one the storage plane itself uses.
            targets = sorted(members, key=lambda item: item.uuid)[:MAINTENANCE_MUTATIONS]
            for member in targets:
                self.backend.update_relationship(
                    member.uuid,
                    properties={
                        "last_maintenance_at": ran_at.isoformat(),
                        "last_maintenance_by": f"replica-{self.replica}",
                    },
                )
                mutated += 1
            self.backend.record_dream_job_run(
                DreamJobRunRecord(
                    ran_at=ran_at,
                    job_name=f"loadtest-maintenance-{self.run_id}",
                    job_kind=DreamJobKind.CONSOLIDATION,
                    processed_scopes=1,
                    reinforced_relationships=mutated,
                    decision_count=1,
                )
            )
            self.backend.record_dream_decision(
                DreamDecisionRecord(
                    ran_at=ran_at,
                    job_name=f"loadtest-maintenance-{self.run_id}",
                    job_kind=DreamJobKind.CONSOLIDATION,
                    agent_id=f"replica-{self.replica}",
                    agent_name=f"replica-{self.replica}",
                    agent_scope=scope,
                    decision_type="loadtest.maintenance.pass",
                    summary=f"scanned {len(members)} members, mutated {mutated}",
                    scope=scope,
                )
            )
            self.backend.set_job_last_run(f"loadtest-maintenance-{self.run_id}-r{self.replica}", ran_at)
        with self._lock:
            self.job_runs += 1

    # -- worker loop -----------------------------------------------------

    def _mix(self, seed: int) -> list[str]:
        mix: list[str] = []
        for op in OP_ORDER:
            mix.extend([op] * OP_WEIGHTS[op])
        random.Random(seed).shuffle(mix)
        return mix

    def worker(self, index: int, deadline: float) -> None:
        rng = random.Random((self.replica << 16) ^ index ^ 0xD1EA)
        mix = self._mix(index)
        handlers = {
            "fact": self.op_fact,
            "reinforce": self.op_reinforce,
            "event": self.op_event,
            "receipt": self.op_receipt,
            "maintenance": self.op_maintenance,
            "control": self.op_control,
        }
        i = 0
        while time.monotonic() < deadline:
            op = mix[i % len(mix)]
            i += 1
            with self._lock:
                self.attempted[op] += 1
            start = time.perf_counter()
            try:
                handlers[op](rng)
            except BaseException as exc:
                label = classify_lock_error(exc)
                record = {
                    "op": op,
                    "replica": str(self.replica),
                    "type": type(exc).__name__,
                    "message": str(exc)[:400],
                }
                with self._lock:
                    if label is not None:
                        record["lock_error"] = label
                        self.lock_errors.append(record)
                    else:
                        self.other_errors.append(record)
            else:
                elapsed_ms = (time.perf_counter() - start) * 1000.0
                with self._lock:
                    self.committed[op] += 1
                    self.latencies[op].append(elapsed_ms)

    def run(self) -> None:
        deadline = time.monotonic() + self.config.duration_seconds
        threads = [
            threading.Thread(target=self.worker, args=(i, deadline), name=f"worker-{i}")
            for i in range(self.config.workers)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

    def result(self, started_at: float) -> ReplicaResult:
        return ReplicaResult(
            replica=self.replica,
            pid=os.getpid(),
            attempted=dict(self.attempted),
            committed=dict(self.committed),
            latencies_ms={op: list(values) for op, values in self.latencies.items()},
            lock_errors=self.lock_errors,
            other_errors=self.other_errors,
            hot_increments=self.hot_increments,
            control_increments=self.control_increments,
            relationship_uuids=list(self.created_relationships),
            receipts_emitted=self.receipts_emitted,
            use_events=self.use_event_count,
            outcome_events=self.outcome_event_count,
            job_runs=self.job_runs,
            task_run_id=self.task_run_id,
            pool_stats=self.backend.pool_stats(),
            started_at=started_at,
            finished_at=time.time(),
        )


def replica_entrypoint(payload: dict[str, Any]) -> None:
    """Child-process entry point.  Must be importable at module level for spawn."""
    from memotron.storage.postgres import PostgresStorageBackend

    config = LoadTestConfig(**payload["config"])
    fixture = payload["fixture"]
    replica = payload["replica"]
    out_path = Path(payload["out_path"])

    started_at = time.time()
    backend = PostgresStorageBackend(
        config.dsn,
        # Every replica runs the migration path on startup, exactly as a fresh
        # pod would.  They contend on the advisory lock and must all survive it.
        migrate=True,
        min_size=config.pool_min,
        max_size=config.pool_max,
        application_name=f"dw-loadtest-replica-{replica}",
    )
    try:
        worker = _Replica(backend, config, fixture, replica)
        worker.run()
        result = worker.result(started_at)
    finally:
        backend.close()
    out_path.write_text(json.dumps(result.__dict__))


# ---------------------------------------------------------------------------
# Verification (parent process, after every replica has exited)
# ---------------------------------------------------------------------------


def _sum(results: list[ReplicaResult], attr: str) -> int:
    return sum(int(getattr(item, attr)) for item in results)


def verify(
    backend: Any,
    config: LoadTestConfig,
    fixture: dict[str, Any],
    results: list[ReplicaResult],
    diagnostics: dict[str, Any] | None = None,
) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    diagnostics = diagnostics if diagnostics is not None else {}
    run_id = fixture["run_id"]
    scopes = [MemoryScope(kind=ScopeKind.AGENT, scope_id=scope_id) for scope_id in fixture["scope_ids"]]

    # --- zero lock errors -------------------------------------------------
    lock_errors = [err for item in results for err in item.lock_errors]
    checks.append(
        (
            "zero lock errors",
            not lock_errors,
            "none" if not lock_errors else f"{len(lock_errors)} lock errors, e.g. {lock_errors[0]['lock_error']}",
        )
    )

    other_errors = [err for item in results for err in item.other_errors]
    checks.append(
        (
            "zero non-lock errors",
            not other_errors,
            "none"
            if not other_errors
            else f"{len(other_errors)} errors, e.g. {other_errors[0]['type']}: {other_errors[0]['message'][:160]}",
        )
    )

    # --- no lost updates: append form ------------------------------------
    committed_relationships = {uuid_value for item in results for uuid_value in item.relationship_uuids}
    persisted: set[str] = set()
    for scope in scopes:
        for relationship in backend.active_relationships(scope=scope):
            props = relationship.properties
            if props.get("loadtest_run") == run_id and props.get("loadtest_op") == "fact":
                persisted.add(relationship.uuid)
    missing = committed_relationships - persisted
    extra = persisted - committed_relationships
    checks.append(
        (
            "no lost relationship appends",
            not missing and not extra,
            f"{len(persisted)} persisted == {len(committed_relationships)} committed"
            if not missing and not extra
            else f"{len(missing)} lost, {len(extra)} unexpected",
        )
    )

    expected_use = _sum(results, "use_events")
    actual_use = sum(len(backend.use_events(task_run_id=item.task_run_id)) for item in results)
    checks.append(
        (
            "no lost use-event appends",
            actual_use == expected_use,
            f"{actual_use} persisted == {expected_use} committed"
            if actual_use == expected_use
            else f"{actual_use} persisted != {expected_use} committed",
        )
    )

    expected_outcome = _sum(results, "outcome_events")
    actual_outcome = sum(len(backend.outcome_events(task_run_id=item.task_run_id)) for item in results)
    checks.append(
        (
            "no lost outcome-event appends",
            actual_outcome == expected_outcome,
            f"{actual_outcome} persisted == {expected_outcome} committed"
            if actual_outcome == expected_outcome
            else f"{actual_outcome} persisted != {expected_outcome} committed",
        )
    )

    expected_jobs = _sum(results, "job_runs")
    actual_jobs = len(
        backend.dream_job_runs(limit=max(expected_jobs * 2, 10), job_name=f"loadtest-maintenance-{run_id}")
    )
    checks.append(
        (
            "no lost maintenance job runs",
            actual_jobs == expected_jobs,
            f"{actual_jobs} persisted == {expected_jobs} committed"
            if actual_jobs == expected_jobs
            else f"{actual_jobs} persisted != {expected_jobs} committed",
        )
    )

    # --- no lost updates: counter form -----------------------------------
    hot = backend.get_relationship(fixture["hot_relationship_uuid"])
    hot_value = int(hot.properties.get("observed_count", 1))
    expected_hot = 1 + _sum(results, "hot_increments")
    checks.append(
        (
            "no lost contended increments",
            hot_value == expected_hot,
            f"observed_count {hot_value} == 1 + {expected_hot - 1} committed increments"
            if hot_value == expected_hot
            else f"observed_count {hot_value} != expected {expected_hot} ({expected_hot - hot_value} increments lost)",
        )
    )

    # --- negative control, measured but never asserted --------------------
    control = backend.get_relationship(fixture["control_relationship_uuid"])
    control_value = int(control.properties.get("observed_count", 1))
    control_attempts = _sum(results, "control_increments")
    diagnostics["control_attempts"] = control_attempts
    diagnostics["control_observed_count"] = control_value
    diagnostics["control_lost"] = 1 + control_attempts - control_value

    # --- chain integrity --------------------------------------------------
    run_uuid = fixture["shared_run_uuid"]
    receipts = backend.receipts.receipts_for_run(run_uuid)
    expected_receipts = _sum(results, "receipts_emitted")
    indices = [receipt.event_index for receipt in receipts]
    dense = indices == list(range(len(receipts)))
    checks.append(
        (
            "receipt chain is dense",
            dense and len(receipts) == expected_receipts,
            f"{len(receipts)} receipts, event_index 0..{len(receipts) - 1}, no gap or duplicate"
            if dense and len(receipts) == expected_receipts
            else f"{len(receipts)} receipts vs {expected_receipts} emitted; "
            f"duplicates={len(indices) - len(set(indices))}; dense={dense}",
        )
    )

    link_errors: list[str] = []
    previous = GENESIS_RECEIPT_HASH
    for receipt in receipts:
        if receipt.previous_receipt_hash != previous:
            link_errors.append(
                f"event_index {receipt.event_index}: previous_receipt_hash "
                f"{receipt.previous_receipt_hash[:12]} != {previous[:12]}"
            )
            if len(link_errors) >= 3:
                break
        previous = receipt.receipt_hash
    checks.append(
        (
            "receipt chain links",
            not link_errors,
            f"{len(receipts)} receipts link head-to-genesis" if not link_errors else "; ".join(link_errors),
        )
    )

    if receipts:
        # The fleet's chain cursor is the *database's* cursor, not any one
        # replica's: no single ReceiptRun handle saw all the emits, so the
        # checkpoint handle is reconstructed from what was actually persisted.
        checkpoint_run = ReceiptRun(
            run_uuid=run_uuid,
            run_kind="consolidation",
            job_name="operational-store-loadtest",
            scope_key=scopes[0].key,
            effective_policy_digest="loadtest-policy-digest",
            next_event_index=len(receipts),
            previous_receipt_hash=receipts[-1].receipt_hash,
        )
        backend.receipts.checkpoint(checkpoint_run)
        verification = backend.receipts.verify_chain(run_uuid)
        checks.append(
            (
                "ledger verify_chain",
                verification.valid,
                f"valid over {verification.receipt_count} receipts, merkle "
                f"{(verification.computed_merkle_root or '')[:16]}"
                if verification.valid
                else f"invalid: {verification.errors}",
            )
        )

    # --- state-hash correctness ------------------------------------------
    mismatches: list[str] = []
    for scope in scopes:
        incremental = backend.graph_state_hash(scope.key)
        backend.recompute_scope_state_tuples(scope.key)
        rebuilt = backend.graph_state_hash(scope.key)
        if incremental != rebuilt:
            mismatches.append(f"{scope.key}: {incremental[:12]} != {rebuilt[:12]}")
    checks.append(
        (
            "state hash == full recompute",
            not mismatches,
            f"{len(scopes)} scopes agree with a full rebuild" if not mismatches else "; ".join(mismatches),
        )
    )

    return checks


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_loadtest(config: LoadTestConfig) -> LoadTestReport:
    from memotron.storage.postgres import PostgresStorageBackend

    parent = PostgresStorageBackend(
        config.dsn,
        min_size=config.pool_min,
        max_size=config.pool_max,
        application_name="dw-loadtest-parent",
    )
    try:
        server_version = parent._engine.server_version()
        pgvector = parent.pgvector_enabled
        fixture = build_fixture(parent, config)
    finally:
        # The parent's pool is closed for the duration of the run so its
        # connections do not count against the budget the replicas are proving.
        parent.close()

    context = multiprocessing.get_context("spawn")
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="dw-loadtest-") as tmpdir:
        processes: list[Any] = []
        out_paths: list[Path] = []
        for replica in range(config.replicas):
            out_path = Path(tmpdir) / f"replica-{replica}.json"
            out_paths.append(out_path)
            payload = {
                "config": config.__dict__,
                "fixture": fixture,
                "replica": replica,
                "out_path": str(out_path),
            }
            process = context.Process(target=replica_entrypoint, args=(payload,), name=f"dw-replica-{replica}")
            process.start()
            processes.append(process)

        for process in processes:
            process.join(timeout=config.duration_seconds + 300)
        wall = time.time() - started

        failed = [p for p in processes if p.exitcode not in (0, None)]
        results: list[ReplicaResult] = []
        for out_path in out_paths:
            if out_path.exists():
                results.append(ReplicaResult.from_json(json.loads(out_path.read_text())))

    verifier = PostgresStorageBackend(
        config.dsn,
        min_size=1,
        max_size=4,
        application_name="dw-loadtest-verify",
        migrate=False,
    )
    diagnostics: dict[str, Any] = {}
    try:
        checks = verify(verifier, config, fixture, results, diagnostics)
    finally:
        verifier.close()

    if failed:
        checks.insert(
            0,
            (
                "all replica processes exited cleanly",
                False,
                f"{len(failed)} replica(s) exited non-zero: {[p.exitcode for p in failed]}",
            ),
        )
    if len(results) != config.replicas:
        checks.insert(
            0,
            (
                "all replicas reported",
                False,
                f"{len(results)} of {config.replicas} replicas wrote a result",
            ),
        )

    return LoadTestReport(
        config=config,
        replicas=results,
        invariants=checks,
        wall_seconds=wall,
        server_version=server_version,
        pgvector_enabled=pgvector,
        diagnostics=diagnostics,
    )


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def percentile(values: list[float], q: float) -> float:
    if not values:
        return float("nan")
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join([line, rule, *body])


def format_report(report: LoadTestReport) -> str:
    out: list[str] = []
    cfg = report.config
    out.append("=" * 78)
    out.append("Operational Store concurrency proof - two-replica sustained load")
    out.append("=" * 78)
    out.append(f"run id            : {cfg.run_id}")
    out.append(f"server            : {report.server_version.split(' on ')[0]}")
    out.append(f"pgvector          : {'enabled' if report.pgvector_enabled else 'not installed'}")
    out.append(f"replicas          : {cfg.replicas} separate OS processes (spawn)")
    out.append(f"workers/replica   : {cfg.workers} threads")
    out.append(f"pool per replica  : min={cfg.pool_min} max={cfg.pool_max}")
    out.append(
        f"connection budget : {cfg.replicas} x {cfg.pool_max} = {cfg.replicas * cfg.pool_max} server connections"
    )
    out.append(f"target duration   : {cfg.duration_seconds:.0f}s")
    out.append(f"wall clock        : {report.wall_seconds:.1f}s")
    out.append(f"scopes            : {cfg.scopes}")
    out.append("")

    total_committed = Counter()
    total_attempted = Counter()
    for replica in report.replicas:
        total_committed.update(replica.committed)
        total_attempted.update(replica.attempted)

    out.append("-- operations per replica " + "-" * 52)
    rows = []
    for replica in report.replicas:
        rows.append(
            [
                f"replica {replica.replica} (pid {replica.pid})",
                str(sum(replica.attempted.values())),
                str(sum(replica.committed.values())),
                str(len(replica.lock_errors)),
                str(len(replica.other_errors)),
            ]
        )
    rows.append(
        [
            "TOTAL",
            str(sum(total_attempted.values())),
            str(sum(total_committed.values())),
            str(len(report.lock_errors)),
            str(len(report.other_errors)),
        ]
    )
    out.append(_table(["replica", "attempted", "committed", "lock errors", "other errors"], rows))
    out.append("")

    out.append("-- throughput and latency by operation " + "-" * 39)
    duration = max(report.config.duration_seconds, 1e-9)
    rows = []
    for op in OP_ORDER:
        samples: list[float] = []
        for replica in report.replicas:
            samples.extend(replica.latencies_ms.get(op, []))
        if not samples:
            continue
        rows.append(
            [
                op,
                str(total_committed[op]),
                f"{total_committed[op] / duration:.1f}",
                f"{statistics.mean(samples):.1f}",
                f"{percentile(samples, 0.50):.1f}",
                f"{percentile(samples, 0.95):.1f}",
                f"{percentile(samples, 0.99):.1f}",
                f"{max(samples):.1f}",
            ]
        )
    all_samples = [value for replica in report.replicas for values in replica.latencies_ms.values() for value in values]
    if all_samples:
        rows.append(
            [
                "ALL",
                str(sum(total_committed.values())),
                f"{sum(total_committed.values()) / duration:.1f}",
                f"{statistics.mean(all_samples):.1f}",
                f"{percentile(all_samples, 0.50):.1f}",
                f"{percentile(all_samples, 0.95):.1f}",
                f"{percentile(all_samples, 0.99):.1f}",
                f"{max(all_samples):.1f}",
            ]
        )
    out.append(
        _table(
            ["operation", "ops", "ops/s", "mean ms", "p50 ms", "p95 ms", "p99 ms", "max ms"],
            rows,
        )
    )
    out.append("")

    out.append("-- invariants " + "-" * 64)
    rows = [["PASS" if ok else "FAIL", name, detail] for name, ok, detail in report.invariants]
    out.append(_table(["", "invariant", "evidence"], rows))
    out.append("")

    if report.lock_errors:
        out.append("-- lock errors " + "-" * 63)
        counts = Counter(err["lock_error"] for err in report.lock_errors)
        out.append(
            _table(
                ["count", "classification"],
                [[str(count), label] for label, count in counts.most_common()],
            )
        )
        out.append("")
    if report.other_errors:
        out.append("-- non-lock errors " + "-" * 59)
        counts = Counter(f"{err['type']} in {err['op']}" for err in report.other_errors)
        out.append(
            _table(
                ["count", "error"],
                [[str(count), label] for label, count in counts.most_common()],
            )
        )
        sample = report.other_errors[0]
        out.append(f"first: {sample['type']}: {sample['message'][:300]}")
        out.append("")

    out.append("-- negative control (unlocked read-modify-write) " + "-" * 29)
    attempts = int(report.diagnostics.get("control_attempts", 0))
    landed = int(report.diagnostics.get("control_observed_count", 1)) - 1
    lost = int(report.diagnostics.get("control_lost", 0))
    out.append(
        _table(
            ["increments issued", "increments landed", "LOST", "loss rate"],
            [
                [
                    str(attempts),
                    str(landed),
                    str(lost),
                    f"{(lost / attempts * 100.0) if attempts else 0.0:.1f}%",
                ]
            ],
        )
    )
    out.append(
        "These increments were issued as two separate interface calls with no enclosing\n"
        "transaction - the shape the contract explicitly does not make atomic. Nothing\n"
        "asserts on them. They are here so the 'no lost contended increments' PASS above\n"
        "is evidence rather than luck: the same workload, written without the lock,\n"
        "demonstrably loses updates against this very database."
    )
    out.append("")

    out.append("-- pool stats at replica exit " + "-" * 48)
    rows = []
    for replica in report.replicas:
        stats = replica.pool_stats
        rows.append(
            [
                f"replica {replica.replica}",
                str(stats.get("pool_min")),
                str(stats.get("pool_max")),
                str(stats.get("pool_size")),
                str(stats.get("requests_num", 0)),
                str(stats.get("requests_waiting", 0)),
                str(stats.get("requests_queued", 0)),
                f"{stats.get('requests_wait_ms', 0)}",
                str(stats.get("connections_num", 0)),
            ]
        )
    out.append(
        _table(
            [
                "replica",
                "min",
                "max",
                "size",
                "requests",
                "waiting",
                "queued",
                "wait ms",
                "conns opened",
            ],
            rows,
        )
    )
    out.append("")

    out.append("=" * 78)
    out.append("VERDICT: " + ("PASS" if report.ok else "FAIL"))
    for failure in report.failures:
        out.append(f"  FAILED - {failure}")
    out.append("=" * 78)
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Two-replica concurrency proof for the Operational Store.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get(DEFAULT_DSN_ENV, ""),
        help=f"libpq DSN for the Operational Store (default: ${DEFAULT_DSN_ENV})",
    )
    parser.add_argument("--replicas", type=int, default=2, help="replica processes to run")
    parser.add_argument("--duration", type=float, default=60.0, help="seconds of sustained load per replica")
    parser.add_argument("--workers", type=int, default=4, help="worker threads per replica")
    parser.add_argument("--pool-min", type=int, default=2, help="pool min_size per replica")
    parser.add_argument("--pool-max", type=int, default=10, help="pool max_size per replica")
    parser.add_argument("--scopes", type=int, default=3, help="scopes the replicas share")
    parser.add_argument(
        "--seed-relationships",
        type=int,
        default=200,
        help="background relationships created before the run",
    )
    parser.add_argument("--json", default=None, help="also write the raw report to this path")
    args = parser.parse_args(argv)

    if not args.dsn:
        parser.error(
            f"no DSN: pass --dsn or set {DEFAULT_DSN_ENV}. This script needs a real "
            "Postgres instance; it has no offline mode."
        )
    if args.replicas < 2:
        parser.error("--replicas must be at least 2: the claim is about two replicas")

    config = LoadTestConfig(
        dsn=args.dsn,
        replicas=args.replicas,
        duration_seconds=args.duration,
        workers=args.workers,
        pool_min=args.pool_min,
        pool_max=args.pool_max,
        scopes=args.scopes,
        seed_relationships=args.seed_relationships,
    )
    report = run_loadtest(config)
    print(format_report(report))

    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {
                    "config": config.as_dict(),
                    "wall_seconds": report.wall_seconds,
                    "server_version": report.server_version,
                    "invariants": [
                        {"name": name, "ok": ok, "detail": detail} for name, ok, detail in report.invariants
                    ],
                    "replicas": [
                        {k: v for k, v in replica.__dict__.items() if k not in {"latencies_ms", "relationship_uuids"}}
                        for replica in report.replicas
                    ],
                },
                indent=2,
                default=str,
            )
        )
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
