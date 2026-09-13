#!/usr/bin/env python3
"""Retrieval benchmark: SQLite substrate vs the Postgres Operational Store.

What is measured, and why those four
------------------------------------
``context_visible_relationships``
    The default working-context read.  The contract requires it to be served by
    an access path over ``(scope_key, status, context-visibility)`` so its cost
    is bounded by the scope's context-visible set and **decoupled from total
    store size**.  This is the read every retrieval pays.

``find_active_truth_relationships``
    The duplicate-check probe paid on *every* synchronous fact write.  On the
    SQLite substrate it is a full-store read materialised into Python objects
    and filtered with a comprehension; on Postgres it is an index seek.  This is
    where the two engines are expected to diverge most sharply, because the
    SQLite cost is a function of the whole store while the write it guards is
    a function of one scope.

``graph_state_hash``
    Paid twice per receipted change (the bracket).  SQLite recomputes the whole
    scope and does one subject lookup per row (N+1); Postgres keeps a canonical
    tuple per relationship and reads a single indexed aggregate, memoised while
    the scope is clean.  Both the memo-hit and the recompute paths are reported,
    because a bracket sees both.

``similar_relationships``
    Vector retrieval, Postgres only — the DW-001 contingency that keeps recall
    working if the Memory Graph is not ready.  The report states whether it ran
    on a pgvector ANN index or the ``dw_cosine_similarity`` float8[] fallback,
    because those are very different numbers and only one of them is available
    on an instance without the extension whitelisted.

How the store is grown, and why it matters
------------------------------------------
Store size is grown by adding **scopes**, holding relationships-per-scope fixed.
That is the shape the decoupling claim is actually about: a tenant's working set
does not grow because another tenant wrote something.  Growing one scope instead
would measure a different (and much weaker) claim.  So a read that is properly
scoped should be flat across every size below, and one that scans the store
should grow linearly — which is exactly what separates the two engines here.

Usage
-----
    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=5433 user=dw dbname=dw_load"
    python scripts/operational_store_benchmark.py --sizes 1000,10000,50000

WARNING: the Postgres run TRUNCATEs the graph tables in the target database
before each size so each measurement starts from a known store.  Point it at a
scratch database, never at LATEST.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from memotron.models import MemoryScope, RelationshipStatus, ScopeKind

DEFAULT_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"

#: Relationships per scope, held constant while the store grows.  See module docstring.
RELATIONSHIPS_PER_SCOPE = 200

#: Subjects per scope; the state tuple carries the subject's name, so this is
#: also the fan-out of the SQLite N+1 subject lookup.
SUBJECTS_PER_SCOPE = 10

#: Graph-plane tables reset between Postgres sizes.  Ordered children-first for
#: readability; TRUNCATE takes them in one statement anyway.
POSTGRES_RESET_TABLES = (
    "relationship_state_tuples",
    "relationship_embeddings",
    "scope_state_hash",
    "memory_outcome_events",
    "memory_use_events",
    "coherence_repair_monitors",
    "memory_prune_ghosts",
    "relationships",
    "nodes",
)

OPERATIONS = (
    "context_visible_relationships",
    "find_active_truth_relationships",
    "graph_state_hash (memo hit)",
    "graph_state_hash (recompute)",
    "similar_relationships",
)


@dataclass
class Measurement:
    engine: str
    size: int
    operation: str
    samples_ms: list[float]
    note: str = ""

    @property
    def p50(self) -> float:
        return percentile(self.samples_ms, 0.50)

    @property
    def p95(self) -> float:
        return percentile(self.samples_ms, 0.95)

    @property
    def mean(self) -> float:
        return statistics.mean(self.samples_ms) if self.samples_ms else float("nan")


@dataclass
class BenchmarkReport:
    sizes: list[int]
    measurements: list[Measurement] = field(default_factory=list)
    build_times: dict[tuple[str, int], float] = field(default_factory=dict)
    explains: dict[str, list[str]] = field(default_factory=dict)
    pgvector_enabled: bool = False
    server_version: str = ""
    sqlite_version: str = ""

    def find(self, engine: str, size: int, operation: str) -> Measurement | None:
        for item in self.measurements:
            if item.engine == engine and item.size == size and item.operation == operation:
                return item
        return None


def report_to_json(report: BenchmarkReport) -> dict[str, Any]:
    return {
        "sizes": report.sizes,
        "pgvector_enabled": report.pgvector_enabled,
        "server_version": report.server_version,
        "sqlite_version": report.sqlite_version,
        "build_times": [
            {"engine": engine, "size": size, "seconds": seconds}
            for (engine, size), seconds in report.build_times.items()
        ],
        "explains": report.explains,
        "measurements": [
            {
                "engine": item.engine,
                "size": item.size,
                "operation": item.operation,
                "p50_ms": item.p50,
                "p95_ms": item.p95,
                "mean_ms": item.mean,
                "note": item.note,
                "samples_ms": item.samples_ms,
            }
            for item in report.measurements
        ],
    }


def report_from_json(data: dict[str, Any]) -> BenchmarkReport:
    report = BenchmarkReport(
        sizes=list(data.get("sizes", [])),
        pgvector_enabled=bool(data.get("pgvector_enabled", False)),
        server_version=str(data.get("server_version", "")),
        sqlite_version=str(data.get("sqlite_version", "")),
        explains={k: list(v) for k, v in (data.get("explains") or {}).items()},
    )
    for entry in data.get("build_times", []):
        report.build_times[(entry["engine"], int(entry["size"]))] = float(entry["seconds"])
    for entry in data.get("measurements", []):
        report.measurements.append(
            Measurement(
                engine=entry["engine"],
                size=int(entry["size"]),
                operation=entry["operation"],
                samples_ms=[float(value) for value in entry.get("samples_ms", [])],
                note=entry.get("note", ""),
            )
        )
    return report


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


# ---------------------------------------------------------------------------
# Store construction
# ---------------------------------------------------------------------------


@dataclass
class Store:
    """What the benchmark needs to know about a built store to read from it."""

    scopes: list[MemoryScope]
    #: ``(scope_key, truth_key)``.  The scope travels alongside the key rather than
    #: being parsed back out of it -- the key is an unescaped concatenation, so the
    #: scope is not recoverable from the string.
    truth_keys: list[tuple[str, str]]
    probe_scope: MemoryScope
    embedding: list[float]
    relationships: int


def build_store(backend: Any, *, size: int, embedding_dim: int, run_tag: str, seed: int = 7) -> Store:
    """Fill *backend* with ``size`` relationships spread over fixed-size scopes."""
    rng = random.Random(seed)
    scope_count = max(size // RELATIONSHIPS_PER_SCOPE, 1)
    scopes: list[MemoryScope] = []
    truth_keys: list[tuple[str, str]] = []
    created = 0

    for index in range(scope_count):
        scope = MemoryScope(kind=ScopeKind.AGENT, scope_id=f"bench-{run_tag}-{index}")
        scopes.append(scope)
        with backend.transaction() if hasattr(backend, "transaction") else _null():
            subjects = []
            for s in range(SUBJECTS_PER_SCOPE):
                node, _ = backend.upsert_node(
                    labels=["Entity"],
                    key=f"{scope.key}:subject-{s}",
                    properties={"name": f"subject {s} of {scope.scope_id}", "scope_key": scope.key},
                )
                subjects.append(node.uuid)
            for i in range(RELATIONSHIPS_PER_SCOPE):
                if created >= size:
                    break
                truth_key = f"{scope.key}:pred:{i}"
                properties = {
                    "scope_key": scope.key,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "status": RelationshipStatus.ACTIVE.value,
                    "memory_type": "semantic",
                    "predicate": "prefers",
                    "fact": f"fact {i} in {scope.scope_id}",
                    "truth_key": truth_key,
                    "truth_prefix": f"{scope.key}:pred",
                    "observed_count": 1,
                    # A tenth of the scope is demoted out of the working context,
                    # so the context-visible read is genuinely narrower than the
                    # scope and the index predicate is doing work.
                    "active_in_context": (i % 10) != 0,
                    "embedding": [rng.uniform(-1.0, 1.0) for _ in range(embedding_dim)],
                }
                backend.add_relationship(
                    source_uuid=subjects[i % SUBJECTS_PER_SCOPE],
                    target_uuid=subjects[(i + 1) % SUBJECTS_PER_SCOPE],
                    relationship_type="FACT",
                    properties=properties,
                )
                truth_keys.append((scope.key, truth_key))
                created += 1
        if created >= size:
            break

    return Store(
        scopes=scopes,
        truth_keys=truth_keys,
        # Read from the middle scope: the first would flatter any engine that
        # happens to keep early rows hot.
        probe_scope=scopes[len(scopes) // 2],
        embedding=[rng.uniform(-1.0, 1.0) for _ in range(embedding_dim)],
        relationships=created,
    )


class _null:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------


def _time(fn, iterations: int, warmup: int = 3) -> list[float]:
    for _ in range(warmup):
        fn()
    samples: list[float] = []
    for _ in range(iterations):
        start = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - start) * 1000.0)
    return samples


def measure(
    backend: Any,
    store: Store,
    *,
    engine: str,
    size: int,
    iterations: int,
    rng: random.Random,
) -> list[Measurement]:
    results: list[Measurement] = []
    scope = store.probe_scope

    results.append(
        Measurement(
            engine,
            size,
            "context_visible_relationships",
            _time(lambda: backend.context_visible_relationships(scope=scope), iterations),
            note=f"{len(backend.context_visible_relationships(scope=scope))} rows returned",
        )
    )

    # Probe keys are drawn from across the whole store, which is what a fact
    # write does: the key it is checking has nothing to do with where the
    # previous one lived.
    probe_keys = [rng.choice(store.truth_keys) for _ in range(iterations + 3)]
    counter = iter(probe_keys)
    results.append(
        Measurement(
            engine,
            size,
            "find_active_truth_relationships",
            _time(
                lambda: backend.find_active_truth_relationships(
                    (probe := next(counter, probe_keys[0]))[1], scope_key=probe[0]
                ),
                iterations,
            ),
            note="1 row returned per probe",
        )
    )

    # Memo-hit path: repeated calls on a scope nothing is writing to.  On SQLite
    # there is no memo, so this is simply the cost of the read.
    results.append(
        Measurement(
            engine,
            size,
            "graph_state_hash (memo hit)",
            _time(lambda: backend.graph_state_hash(scope.key), iterations),
        )
    )

    # Recompute path: what a bracket pays after its own mutation dirties the
    # scope.  On Postgres that is the indexed aggregate over the scope's state
    # tuples; the private constant is imported rather than copied so this cannot
    # drift away from the query the backend actually runs.
    if engine == "postgres":
        from memotron.storage.postgres._graph import _STATE_HASH_SQL

        results.append(
            Measurement(
                engine,
                size,
                "graph_state_hash (recompute)",
                _time(
                    lambda: backend._engine.fetchvalue(_STATE_HASH_SQL, (scope.key,)),
                    iterations,
                ),
                note="indexed aggregate over relationship_state_tuples",
            )
        )
        results.append(
            Measurement(
                engine,
                size,
                "similar_relationships",
                _time(
                    lambda: backend.similar_relationships(scope=scope, embedding=store.embedding, limit=20),
                    iterations,
                ),
                note=(
                    "pgvector ANN index"
                    if getattr(backend, "pgvector_enabled", False)
                    else "dw_cosine_similarity over float8[] (pgvector not installed)"
                ),
            )
        )
    else:
        # SQLite has no memo, so every call is already the full recompute; the
        # same number is reported under both labels rather than pretending the
        # engine has a fast path it does not have.
        memo = results[-1]
        results.append(
            Measurement(
                engine,
                size,
                "graph_state_hash (recompute)",
                list(memo.samples_ms),
                note="no memo on this engine: every call is a full recompute",
            )
        )

    return results


# ---------------------------------------------------------------------------
# EXPLAIN evidence
# ---------------------------------------------------------------------------


def collect_explains(backend: Any, store: Store) -> dict[str, list[str]]:
    """Engine-native plan evidence that the Postgres reads are seeks, not scans."""
    from memotron.storage.postgres._graph import _STATE_HASH_SQL

    scope = store.probe_scope
    explains: dict[str, list[str]] = {}
    explains["context_visible_relationships"] = backend.explain_context_visible_read(scope=scope)

    # Mirrors find_active_truth_relationships in _graph.py.
    explains["find_active_truth_relationships"] = [
        str(next(iter(row.values())))
        for row in backend._engine.fetchall(
            """
            EXPLAIN SELECT * FROM relationships
            WHERE properties->>'truth_key' = %s
              AND properties->>'scope_key' = %s
              AND properties->>'status' = %s
            ORDER BY created_at, uuid
            """,
            (store.truth_keys[0][1], store.truth_keys[0][0], RelationshipStatus.ACTIVE.value),
        )
    ]
    explains["graph_state_hash (recompute)"] = [
        str(next(iter(row.values()))) for row in backend._engine.fetchall("EXPLAIN " + _STATE_HASH_SQL, (scope.key,))
    ]
    explains["similar_relationships (candidate selection)"] = [
        str(next(iter(row.values())))
        for row in backend._engine.fetchall(
            """
            EXPLAIN SELECT e.relationship_uuid
            FROM relationship_embeddings e
            WHERE e.scope_key = %s AND e.dimensions = %s
            """,
            (scope.key, len(store.embedding)),
        )
    ]
    return explains


def _is_seek(plan: list[str]) -> bool:
    """True when no plan line is a sequential scan of a graph-plane table."""
    return not any("Seq Scan" in line for line in plan)


# ---------------------------------------------------------------------------
# Drivers
# ---------------------------------------------------------------------------


def run_sqlite(size: int, *, iterations: int, embedding_dim: int, report: BenchmarkReport) -> None:
    from memotron.storage.sqlite import SQLiteStorageBackend

    with tempfile.TemporaryDirectory(prefix="dw-bench-sqlite-") as tmpdir:
        path = Path(tmpdir) / "graph.sqlite"
        backend = SQLiteStorageBackend(str(path))
        try:
            start = time.perf_counter()
            store = build_store(backend, size=size, embedding_dim=embedding_dim, run_tag=uuid.uuid4().hex[:6])
            report.build_times[("sqlite", size)] = time.perf_counter() - start
            report.measurements.extend(
                measure(
                    backend,
                    store,
                    engine="sqlite",
                    size=size,
                    iterations=iterations,
                    rng=random.Random(11),
                )
            )
        finally:
            backend.close()


def reset_postgres(dsn: str) -> None:
    from memotron.storage.postgres import PostgresStorageBackend

    backend = PostgresStorageBackend(dsn, application_name="dw-bench-reset")
    try:
        backend._engine.execute(f"TRUNCATE {', '.join(POSTGRES_RESET_TABLES)} RESTART IDENTITY CASCADE")
    finally:
        backend.close()


def run_postgres(dsn: str, size: int, *, iterations: int, embedding_dim: int, report: BenchmarkReport) -> None:
    from memotron.storage.postgres import PostgresStorageBackend

    reset_postgres(dsn)
    backend = PostgresStorageBackend(dsn, application_name="dw-bench", min_size=2, max_size=8)
    try:
        report.pgvector_enabled = backend.pgvector_enabled
        report.server_version = backend._engine.server_version()
        start = time.perf_counter()
        store = build_store(backend, size=size, embedding_dim=embedding_dim, run_tag=uuid.uuid4().hex[:6])
        report.build_times[("postgres", size)] = time.perf_counter() - start
        # ANALYZE before measuring: an unanalysed table gives the planner default
        # estimates and the plan evidence below would be about nothing.
        backend._engine.execute("ANALYZE")
        report.measurements.extend(
            measure(
                backend,
                store,
                engine="postgres",
                size=size,
                iterations=iterations,
                rng=random.Random(11),
            )
        )
        report.explains = collect_explains(backend, store)
    finally:
        backend.close()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    head = "  ".join(h.ljust(widths[i]) for i, h in enumerate(headers))
    rule = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)) for row in rows]
    return "\n".join([head, rule, *body])


def format_report(report: BenchmarkReport) -> str:
    out: list[str] = []
    out.append("=" * 96)
    out.append("Operational Store retrieval benchmark - SQLite substrate vs Postgres")
    out.append("=" * 96)
    out.append(f"server            : {report.server_version.split(' on ')[0]}")
    out.append(f"sqlite            : {report.sqlite_version}")
    out.append(
        f"pgvector          : {'enabled (ANN index)' if report.pgvector_enabled else 'NOT installed - dw_cosine_similarity float8[] fallback'}"
    )
    out.append(f"store sizes       : {', '.join(f'{n:,}' for n in report.sizes)} relationships")
    out.append(
        f"growth model      : {RELATIONSHIPS_PER_SCOPE} relationships per scope held constant; "
        "size grows by adding scopes"
    )
    out.append("")

    out.append("-- store build " + "-" * 81)
    rows = []
    for size in report.sizes:
        for engine in ("sqlite", "postgres"):
            seconds = report.build_times.get((engine, size))
            if seconds is None:
                continue
            rows.append(
                [
                    engine,
                    f"{size:,}",
                    f"{max(size // RELATIONSHIPS_PER_SCOPE, 1):,}",
                    f"{seconds:.1f}",
                    f"{size / seconds:.0f}",
                ]
            )
    out.append(_table(["engine", "relationships", "scopes", "build s", "writes/s"], rows))
    out.append("")

    out.append("-- read latency (p50 / p95 ms) " + "-" * 65)
    headers = ["operation", "engine"]
    for size in report.sizes:
        headers.append(f"{size:,} p50")
        headers.append(f"{size:,} p95")
    headers.append("scale 1k->max")
    rows = []
    for operation in OPERATIONS:
        labelled = False
        for engine in ("sqlite", "postgres"):
            cells = [operation if not labelled else "", engine]
            first: float | None = None
            last: float | None = None
            present = False
            for size in report.sizes:
                item = report.find(engine, size, operation)
                if item is None:
                    cells.extend(["-", "-"])
                    continue
                present = True
                cells.append(f"{item.p50:.2f}")
                cells.append(f"{item.p95:.2f}")
                if first is None:
                    first = item.p50
                last = item.p50
            if not present:
                continue
            labelled = True
            if first and last and first > 0:
                cells.append(f"x{last / first:.1f}")
            else:
                cells.append("-")
            rows.append(cells)
    out.append(_table(headers, rows))
    out.append("")
    out.append(
        "'scale 1k->max' is p50 at the largest store divided by p50 at the smallest.\n"
        "A read that is properly scoped stays near x1 as the store grows around it;\n"
        "a read that scans the store grows with it."
    )
    out.append("")

    speedups = []
    for operation in OPERATIONS:
        for size in report.sizes:
            sqlite_item = report.find("sqlite", size, operation)
            pg_item = report.find("postgres", size, operation)
            if sqlite_item and pg_item and pg_item.p50 > 0:
                speedups.append(
                    [
                        operation,
                        f"{size:,}",
                        f"{sqlite_item.p50:.2f}",
                        f"{pg_item.p50:.2f}",
                        f"x{sqlite_item.p50 / pg_item.p50:.1f}",
                    ]
                )
    if speedups:
        out.append("-- Postgres speedup over the SQLite substrate (p50) " + "-" * 44)
        out.append(_table(["operation", "store", "sqlite ms", "postgres ms", "speedup"], speedups))
        out.append("")

    if report.explains:
        out.append("-- EXPLAIN evidence (Postgres, largest store) " + "-" * 50)
        rows = []
        for name, plan in report.explains.items():
            rows.append([name, "INDEX SEEK" if _is_seek(plan) else "SEQ SCAN", plan[0][:70]])
        out.append(_table(["read", "verdict", "top plan node"], rows))
        out.append("")
        for name, plan in report.explains.items():
            out.append(f"  {name}:")
            for line in plan:
                out.append(f"    {line}")
            out.append("")

    notes = [[item.operation, item.engine, f"{item.size:,}", item.note] for item in report.measurements if item.note]
    if notes:
        out.append("-- notes " + "-" * 87)
        out.append(_table(["operation", "engine", "store", "note"], notes))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    import sqlite3

    parser = argparse.ArgumentParser(
        description="Retrieval benchmark: SQLite substrate vs the Postgres Operational Store.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--dsn", default=os.environ.get(DEFAULT_DSN_ENV, ""))
    parser.add_argument("--sizes", default="1000,10000,50000", help="comma-separated relationship counts")
    parser.add_argument("--iterations", type=int, default=25, help="timed calls per measurement")
    parser.add_argument("--embedding-dim", type=int, default=64)
    parser.add_argument("--skip-sqlite", action="store_true")
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--json", default=None, help="write (and with --resume, read) raw results here")
    parser.add_argument(
        "--resume",
        action="store_true",
        help=(
            "load --json if it exists and skip any engine/size already measured. "
            "The 50k build is long; this lets the run be done in stages and "
            "re-formatted without repeating it."
        ),
    )
    args = parser.parse_args(argv)

    sizes = [int(value) for value in args.sizes.split(",") if value.strip()]
    if not args.skip_postgres and not args.dsn:
        parser.error(f"no DSN: pass --dsn or set {DEFAULT_DSN_ENV} (or use --skip-postgres)")

    report = BenchmarkReport(sizes=sizes, sqlite_version=sqlite3.sqlite_version)
    if args.resume and args.json and Path(args.json).exists():
        report = report_from_json(json.loads(Path(args.json).read_text()))
        report.sizes = sorted({*report.sizes, *sizes})
        report.sqlite_version = sqlite3.sqlite_version

    def already(engine: str, size: int) -> bool:
        return any(item.engine == engine and item.size == size for item in report.measurements)

    for size in sizes:
        if not args.skip_sqlite and not already("sqlite", size):
            print(f"[sqlite]   building {size:,} relationships...", file=sys.stderr, flush=True)
            run_sqlite(
                size,
                iterations=args.iterations,
                embedding_dim=args.embedding_dim,
                report=report,
            )
        if not args.skip_postgres and not already("postgres", size):
            print(f"[postgres] building {size:,} relationships...", file=sys.stderr, flush=True)
            run_postgres(
                args.dsn,
                size,
                iterations=args.iterations,
                embedding_dim=args.embedding_dim,
                report=report,
            )
        if args.json:
            Path(args.json).write_text(json.dumps(report_to_json(report), indent=2))

    print(format_report(report))

    if args.json:
        Path(args.json).write_text(json.dumps(report_to_json(report), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
