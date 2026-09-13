"""U-14: does the truth slot still supersede correctly on Postgres?

D-48 verified supersession only on SQLite. T0-4 makes Postgres `relationships_for_scope`
return structural MENTIONS rows (no `fact`, no `truth_key`, `scope_kind=None`), and
`_materialize_episode` consumes exactly that call — so the input to slot matching differs
between backends on the one code path that decides whether a fact supersedes or coexists.

Why this is the highest-stakes untested consequence of T0-4: `search()` raising `KeyError`
is loud. A truth slot that silently fails to match would leave **two contradictory rows both
`active`** — the exact failure the design exists to prevent — and nothing would report it.

**Deliberately bypasses `AgentMemoryPlatform`.** T0-5: every platform/adoption entry point
(`build_platform_from_env`, `build_platform_from_project`) passes only `graph_path` and never
wires `storage_settings_from_env()`, so a probe built on them runs on SQLite no matter what
DSN is set — a null test that looks like a passing one. This constructs `Memotron` directly,
which does honour the DSN, and **verifies the backend by attributing a write** before trusting
any result.

**Held constant:** one client, one scope, same two contradicting facts, same order, no LLM
(client-managed writes). The only variable is the storage engine.

    uv run python scripts/verify/probe_supersession_backend.py "host=... dbname=..."

exit 0 = both backends supersede correctly · 1 = a backend left contradictions active · 2 = error
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

SUBJECT = "the operational store"
PREDICATE = "will be"
OLD = "Postgres, not SQLite"
NEW = "SQLite, not Postgres"

CHILD = r"""
import asyncio, json, os, sys
from memotron import Memotron, MemoryScope, ScopeKind

async def main():
    dw = Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"])
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="u14")
    subj, pred, old, new = sys.argv[1:5]

    await dw.add_memory(subject=subj, predicate=pred, object=old,
                        relationship_type="REQUIRES", scope=scope)
    await dw.add_memory(subject=subj, predicate=pred, object=new,
                        relationship_type="REQUIRES", scope=scope)

    rows = [r for r in dw.graph.relationships() if r.type != "MENTIONS"]
    out = []
    for r in rows:
        p = r.properties or {}
        out.append({"status": p.get("status"),
                    "truth_key": str(p.get("truth_key"))[:52] if p.get("truth_key") else None,
                    "valid_to": bool(p.get("valid_to")),
                    "fact": str(p.get("fact"))[:64]})
    # engine attribution: what backend class is actually in play?
    engine = type(dw.graph).__name__
    print("__R__" + json.dumps({"rows": out, "engine": engine}))

asyncio.run(main())
"""


def pg_rowcount(dsn: str) -> int | str:
    try:
        import psycopg

        with psycopg.connect(dsn) as c:
            return c.execute("SELECT count(*) FROM relationships").fetchone()[0]
    except Exception as e:
        # a missing relationships table means migrations have not run yet == zero rows,
        # which is a legitimate "before" reading, not an attribution failure
        if "UndefinedTable" in type(e).__name__ or "does not exist" in str(e):
            return 0
        return f"{type(e).__name__}"


def run(dsn: str | None, tag: str) -> dict:
    env = dict(os.environ)
    env["MEMOTRON_GRAPH_PATH"] = os.path.join(tempfile.mkdtemp(prefix=f"u14-{tag}-"), "graph.db")
    if dsn:
        env["MEMOTRON_OPERATIONAL_STORE_DSN"] = dsn
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE", "1")
        env.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE", "4")
    else:
        env.pop("MEMOTRON_OPERATIONAL_STORE_DSN", None)

    before = pg_rowcount(dsn) if dsn else None
    p = subprocess.run(
        [sys.executable, "-c", CHILD, SUBJECT, PREDICATE, OLD, NEW],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    after = pg_rowcount(dsn) if dsn else None
    for line in p.stdout.splitlines():
        if line.startswith("__R__"):
            d = json.loads(line[5:])
            d["pg_rows_before"], d["pg_rows_after"] = before, after
            return d
    return {"error": (p.stdout[-300:] + p.stderr[-1200:]).strip(), "pg_rows_before": before, "pg_rows_after": after}


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dsn = sys.argv[1]

    failures: list[str] = []
    for tag, d in (("sqlite", None), ("postgres", dsn)):
        r = run(d, tag)
        print(f"\n  === {tag} ===")
        if "error" in r:
            print(f"    HARNESS ERROR: {r['error'][-400:]}")
            return 2
        print(f"    graph class: {r['engine']}")
        if d:
            print(f"    postgres relationships: {r['pg_rows_before']} -> {r['pg_rows_after']}")
            if not (
                isinstance(r["pg_rows_after"], int)
                and isinstance(r["pg_rows_before"], int)
                and r["pg_rows_after"] > r["pg_rows_before"]
            ):
                print("    >>> WRITE NOT ATTRIBUTED TO POSTGRES — this arm is a NULL TEST (T0-5).")
                failures.append(f"{tag}: never reached Postgres; result proves nothing")
                continue
        for i, row in enumerate(r["rows"], 1):
            print(f"    [{i}] status={row['status']!r:12s} valid_to_set={row['valid_to']} {row['fact']!r}")
            print(f"        truth_key={row['truth_key']!r}")

        active = [x for x in r["rows"] if str(x["status"]).lower() == "active"]
        superseded = [x for x in r["rows"] if "supersed" in str(x["status"]).lower()]
        keys = {x["truth_key"] for x in r["rows"]}
        if len(active) > 1:
            failures.append(
                f"{tag}: {len(active)} rows ACTIVE — contradictions coexist ({len(keys)} distinct truth_key(s))"
            )
        elif not superseded:
            failures.append(f"{tag}: nothing superseded and only {len(active)} active row(s)")
        else:
            print(f"    OK: {len(superseded)} superseded, {len(active)} active, {len(keys)} truth_key(s)")

    print(f"\n=== supersession across backends: {'FAIL' if failures else 'PASS'} ===")
    for f in failures:
        print(f"  - {f}")
    if not failures:
        print("  both backends closed the incumbent and left exactly one active row.")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
