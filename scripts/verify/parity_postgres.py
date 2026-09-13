"""Cross-backend parity gate: does Postgres answer the same questions as SQLite?

`test_storage_backend_parity.py` (3,310 lines, 117 tests) asserts that both backends store
the same bytes. It does NOT assert that a scoped read returns the same rows to a caller,
which is how T0-4 survived it. This harness checks the caller's view instead.

Runs the SAME script against both engines with the same seed, holding everything but the
storage engine constant, and fails on divergence.

    docker compose -p $(basename $PWD) -f docker-compose.local.yml up -d postgres
    uv run python scripts/verify/parity_postgres.py \
        "host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=dwparity"

exit 0 = backends agree · 1 = divergence · 2 = harness error

Checks:
  1. relationships_for_scope returns the same rows (T0-4: Postgres omits the MENTIONS filter)
  2. search() behaves the same          (T0-4 symptom: KeyError 'fact' on Postgres)
  3. epoch_content_digest is STABLE across independent recomputation on each engine, and
     equal between them for identical content (its whole stated purpose)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile

FACTS = [
    ("the api service", "requires", "a durable store", "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
]

# The child runs in a clean interpreter so the engine is chosen by env at import time.
CHILD = r"""
import asyncio, json, os, sys
from memotron import Memotron, MemoryScope, ScopeKind
from memotron.epochs import epoch_content_digest

FACTS = json.loads(sys.argv[1])

async def main():
    dw = Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"])
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="parity")
    for s, p, o, t in FACTS:
        await dw.add_memory(subject=s, predicate=p, object=o, relationship_type=t, scope=scope)

    rows = dw.graph.relationships_for_scope(scope.key)
    out = {
        "row_count": len(rows),
        "types": dict(sorted((k, v) for k, v in
                             __import__("collections").Counter(r.type for r in rows).items())),
        "first_type": rows[0].type if rows else None,
        "digest": epoch_content_digest(storage=dw.graph, scope=scope),
    }
    try:
        hits = await dw.search(query="durable store", scope=scope)
        out["search"] = f"OK:{len(hits)}"
    except Exception as e:
        out["search"] = f"{type(e).__name__}: {e}"
    print("__RESULT__" + json.dumps(out))

asyncio.run(main())
"""


def _fresh_database(dsn: str, tag: str) -> str:
    """Create a per-run database off the given DSN and return a DSN pointing at it.

    Byte-comparable collation is required by the receipt/state-hash path — see
    ``docs/operational-store-deployment.md`` — so the database is created with
    ``LC_COLLATE='C'``, matching what the runbook specifies for the real instance.
    """
    import psycopg

    parts = dict(kv.split("=", 1) for kv in dsn.split() if "=" in kv)
    base = parts["dbname"]
    fresh = f"{base}_{tag}"
    admin = " ".join(f"{k}={'postgres' if k == 'dbname' else v}" for k, v in parts.items())
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'DROP DATABASE IF EXISTS "{fresh}"')
        conn.execute(
            f"""CREATE DATABASE "{fresh}" LC_COLLATE='C' LC_CTYPE='C'
                TEMPLATE template0 ENCODING 'UTF8'"""
        )
    return " ".join(f"{k}={fresh if k == 'dbname' else v}" for k, v in parts.items())


def run(dsn: str | None, tag: str) -> dict:
    """Run the child once against one engine, in its OWN FRESH store.

    Each run must get a clean store or this measures accumulation, not recomputation.
    The SQLite path gets a fresh tempdir; the Postgres path gets a freshly created
    database (the DSN's ``dbname`` is suffixed per run) — reusing one database across
    runs re-seeds an already-populated store and the digest legitimately changes,
    which looks exactly like nondeterminism and is not.
    """
    import json

    env = dict(os.environ)
    tmp = tempfile.mkdtemp(prefix=f"dwparity-{tag}-")
    env["MEMOTRON_GRAPH_PATH"] = os.path.join(tmp, "graph.db")
    if dsn:
        dsn = _fresh_database(dsn, tag)
        env["MEMOTRON_OPERATIONAL_STORE_DSN"] = dsn
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE", "1")
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE", "4")
    else:
        env.pop("MEMOTRON_OPERATIONAL_STORE_DSN", None)
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, json.dumps(FACTS)], env=env, capture_output=True, text=True, timeout=300
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__") :])
    raise RuntimeError(f"{tag} child produced no result:\n{proc.stdout[-800:]}\n{proc.stderr[-800:]}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dsn_template = sys.argv[1]

    failures: list[str] = []
    print("  running SQLite x2 and Postgres x2 — same script, same seed, engine is the only variable\n")

    sq = [run(None, f"sqlite{i}") for i in range(2)]
    pg = [run(dsn_template, f"pg{i}") for i in range(2)]

    for label, runs in (("sqlite", sq), ("postgres", pg)):
        r = runs[0]
        print(f"  {label:9s} rows={r['row_count']:<3} types={r['types']} first={r['first_type']} search={r['search']}")
        print(f"  {'':9s} digests: " + "  ".join(x["digest"][:12] for x in runs))

    # 1. scoped read parity
    if sq[0]["row_count"] != pg[0]["row_count"] or sq[0]["types"] != pg[0]["types"]:
        failures.append(
            f"relationships_for_scope diverges: sqlite {sq[0]['row_count']} rows {sq[0]['types']} "
            f"vs postgres {pg[0]['row_count']} rows {pg[0]['types']} (T0-4: missing MENTIONS filter)"
        )
    if pg[0]["first_type"] == "MENTIONS":
        failures.append("postgres returns a MENTIONS edge FIRST — any caller taking [0] gets a structural edge")

    # 2. search parity
    if sq[0]["search"].split(":")[0] != pg[0]["search"].split(":")[0]:
        failures.append(f"search() diverges: sqlite {sq[0]['search']} vs postgres {pg[0]['search']}")

    # 3. digest stability, per engine, then across engines
    for label, runs in (("sqlite", sq), ("postgres", pg)):
        if runs[0]["digest"] != runs[1]["digest"]:
            failures.append(
                f"epoch_content_digest is NON-DETERMINISTIC on {label}: independent recomputation of "
                f"identical content gave {runs[0]['digest'][:12]} then {runs[1]['digest'][:12]}. "
                f"Its purpose is comparing independently recomputed shadows; this voids it."
            )
    if sq[0]["digest"] != pg[0]["digest"]:
        failures.append("epoch_content_digest differs BETWEEN backends for identical content")

    print(f"\n=== cross-backend parity: {'FAIL' if failures else 'PASS'} ===")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("  backends agree on the caller's view, and the digest is stable on both.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
