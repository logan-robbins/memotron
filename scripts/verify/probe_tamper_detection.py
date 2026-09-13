"""Does the audit plane actually detect tampering — and does it detect it on both engines?

`verify_no_silent_mutation` (`replay.py:1018`) states the invariant every audit ledger has to
deliver: *every live state change is receipted*. Its four mechanical checks are

  * every receipt chain touching the scope verifies;
  * every mutating receipt carries before/after graph state hashes;
  * the per-scope mutating stream is **contiguous** — each before hash equals the prior after;
  * the latest receipted state hash equals the **live** graph state hash.

Reading that is not the same as knowing it fires. This probe attacks the store **behind the
API's back** with raw SQL and requires the system to notice. Four tamper modes, because a
reviewer will ask about each:

  MODIFY   change a fact's text in place
  DELETE   remove a memory row
  INSERT   add a memory row that no receipt explains
  LEDGER   alter a receipt itself

**The hypothesis worth testing, not just the happy path.** SQLite *recomputes* the graph state
hash from rows on read; Postgres *maintains it incrementally* (that asymmetry is stated in
`test_storage_backend_parity.py`'s own docstring, which asserts the two engines agree on the
value). If Postgres reads a stored hash rather than recomputing one, a raw-SQL tamper would not
move it — and the "latest receipted == live" check would pass on tampered data. Same invariant,
different detection power. So every mode runs on **both** engines and the results are compared.

    # SQLite only
    uv run python scripts/verify/probe_tamper_detection.py
    # both engines
    uv run python scripts/verify/probe_tamper_detection.py "host=... dbname=..."

exit 0 = every tamper detected on every engine · 1 = something went undetected · 2 = error
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memotron import Memotron, MemoryScope, ScopeKind

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="tamper")

FACTS = (
    ("the api service", "requires", "a durable store", "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
)


# --------------------------------------------------------------------------- tampers
# Each takes a raw cursor and the engine's properties-column name, and mutates the store
# with no receipt written. The two engines genuinely differ in schema -- SQLite stores
# `properties_json TEXT` and has no `version` column; Postgres stores `properties jsonb`
# -- so the SQL is parameterised rather than assumed identical.


def tamper_modify(exec_sql: Callable[[str], Any], props: str, cast: str) -> str:
    # jsonb will not accept a text expression, so the replacement is cast back on Postgres.
    exec_sql(
        f"UPDATE relationships SET {props} = REPLACE(CAST({props} AS TEXT), "
        "'a durable store', 'a durable store AND AN INJECTED CLAUSE')"
        f"{cast} WHERE type <> 'MENTIONS'"
    )
    return "rewrote a fact's text in place"


def tamper_delete(exec_sql: Callable[[str], Any], props: str, cast: str) -> str:
    exec_sql("DELETE FROM relationships WHERE uuid = (SELECT uuid FROM relationships WHERE type <> 'MENTIONS' LIMIT 1)")
    return "deleted a memory row"


def tamper_insert(exec_sql: Callable[[str], Any], props: str, cast: str) -> str:
    # Clone an existing row under a new uuid: no receipt explains it.
    exec_sql(
        f"INSERT INTO relationships (uuid, source_uuid, target_uuid, type, {props}, "
        "created_at, valid_from, valid_to) "
        f"SELECT 'tampered-' || uuid, source_uuid, target_uuid, type, {props}, "
        "created_at, valid_from, valid_to "
        "FROM relationships WHERE type <> 'MENTIONS' LIMIT 1"
    )
    return "inserted an unreceipted memory row"


def tamper_ledger(exec_sql: Callable[[str], Any], props: str, cast: str) -> str:
    exec_sql(
        "UPDATE memory_receipts SET graph_state_hash_after = "
        "'0000000000000000000000000000000000000000000000000000000000000000' "
        "WHERE graph_state_hash_after IS NOT NULL"
    )
    return "altered a receipt's after-state hash"


TAMPERS: tuple[tuple[str, Callable[..., str]], ...] = (
    ("MODIFY", tamper_modify),
    ("DELETE", tamper_delete),
    ("INSERT", tamper_insert),
    ("LEDGER", tamper_ledger),
)

PROPS_COLUMN = {"sqlite": "properties_json", "postgres": "properties"}
PROPS_CAST = {"sqlite": "", "postgres": "::jsonb"}


# --------------------------------------------------------------------------- engines


def _reset_postgres(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("DROP SCHEMA IF EXISTS public CASCADE")
        c.execute("CREATE SCHEMA public")


async def run_one(engine: str, dsn: str | None, name: str, tamper: Callable[..., str]) -> dict:
    """Seed, confirm clean, tamper out-of-band, then re-check."""
    tmp = Path(tempfile.mkdtemp(prefix=f"tamper-{engine}-"))
    graph_path = tmp / "graph.sqlite"
    env_dsn = None
    if engine == "postgres":
        assert dsn
        _reset_postgres(dsn)
        env_dsn = dsn
        os.environ["MEMOTRON_OPERATIONAL_STORE_DSN"] = dsn
        os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")
    else:
        os.environ.pop("MEMOTRON_OPERATIONAL_STORE_DSN", None)

    dw = Memotron(graph_path=str(graph_path))
    for s, p, o, t in FACTS:
        await dw.add_memory(subject=s, predicate=p, object=o, relationship_type=t, scope=SCOPE)

    clean = await dw.verify_no_silent_mutations(scope=SCOPE)
    if not clean.passed:
        return {"engine": engine, "tamper": name, "baseline_failed": True, "errors": list(clean.errors)[:2]}

    # --- mutate the store with no receipt ---
    if engine == "postgres":
        import psycopg

        conn = psycopg.connect(env_dsn, autocommit=True)
        exec_sql = conn.execute
        what = tamper(exec_sql, PROPS_COLUMN[engine], PROPS_CAST[engine])
        conn.close()
    else:
        conn = sqlite3.connect(str(graph_path))
        exec_sql = conn.execute
        what = tamper(exec_sql, PROPS_COLUMN[engine], PROPS_CAST[engine])
        conn.commit()
        conn.close()

    # Re-open so nothing is served from an in-process cache.
    dw2 = Memotron(graph_path=str(graph_path))
    after = await dw2.verify_no_silent_mutations(scope=SCOPE)
    return {
        "engine": engine,
        "tamper": name,
        "what": what,
        "detected": not after.passed,
        "errors": list(after.errors)[:2],
    }


async def main() -> int:
    dsn = sys.argv[1] if len(sys.argv) > 1 else None
    engines = ["sqlite"] + (["postgres"] if dsn else [])
    if not dsn:
        print("  (no DSN given — SQLite only; pass a DSN to test the engine asymmetry)")

    results: list[dict] = []
    for engine in engines:
        print(f"\n  === {engine} ===")
        for name, fn in TAMPERS:
            r = await run_one(engine, dsn, name, fn)
            results.append(r)
            if r.get("baseline_failed"):
                print(f"    {name:7s} BASELINE FAILED before tampering — {r['errors']}")
                continue
            mark = "detected" if r["detected"] else "*** UNDETECTED ***"
            print(f"    {name:7s} {mark:18s} {r['what']}")
            if r["detected"] and r["errors"]:
                print(f"            {str(r['errors'][0])[:104]}")

    missed = [r for r in results if not r.get("detected") and not r.get("baseline_failed")]
    broken = [r for r in results if r.get("baseline_failed")]
    print(f"\n=== tamper detection: {'FAIL' if (missed or broken) else 'PASS'} ===")
    for r in broken:
        print(f"  - {r['engine']}/{r['tamper']}: the invariant did not hold on clean data")
    for r in missed:
        print(f"  - {r['engine']}/{r['tamper']}: {r['what']} went UNDETECTED")
    if len(engines) == 2:
        for name, _ in TAMPERS:
            per = {r["engine"]: r.get("detected") for r in results if r["tamper"] == name}
            if len(set(per.values())) > 1:
                print(f"  - ENGINE ASYMMETRY on {name}: {per}")
    if not missed and not broken:
        print("  every out-of-band mutation was caught on every engine tested.")
    return 1 if (missed or broken) else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
