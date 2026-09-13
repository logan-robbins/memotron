"""T0-5 gate: does every construction path honour MEMOTRON_OPERATIONAL_STORE_DSN?

STATUS 2026-08-31: **PASSES (exit 0). T0-5 is fixed** by `29624cb`, which sets
`storage=storage_settings_from_env()` at `agent_memory/_config.py:122`. Measured against the
local Postgres: both paths wrote to Postgres, SQLite stayed at 0 rows. `29624cb` is an ancestor
of this branch and is NOT on `origin/main`, so the defect described below is still live there.
The description is kept in the present tense as the record of what this gate was built to catch.


`storage_settings_from_env()` has only two call sites — `default_config()` and
`mcp_server.py:173`. `agent_memory_config()` never sets `storage`, and `client/__init__.py:524`
raises if both `control_plane` and `config` are passed, so `base_config` is the only seam
the DSN can reach the agent-memory surface through, and it is unwired. The result is that
the entire AgentMemoryPlatform surface — 27 MCP tools, the layer agents use for
compaction/context — silently stays on a per-pod SQLite file when the deployment enables
Postgres.

The verdict comes from ATTRIBUTING A WRITE (count rows in each store before and after),
not from reading the construction code. A green return value says nothing about where the
bytes went.

    uv run python scripts/verify/probe_storage_wiring.py "host=... dbname=..."

exit 0 = every path honours the DSN · 1 = a path silently used SQLite · 2 = harness error

CITATIONS REPAIRED 2026-08-31: every `file.py:NNN` below was rewritten after the module
split dissolved the god files they named. Each now carries the SYMBOL as well as the line,
so the next move makes them findable by grep rather than silently wrong.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import tempfile

import psycopg

from memotron import Memotron, MemoryScope, ScopeKind

SCOPE_ID = "storage-wiring-probe"


def counts(dsn: str, shim: str) -> tuple[int, int]:
    with psycopg.connect(dsn) as c:
        pg = c.execute("SELECT count(*) FROM relationships").fetchone()[0]
    try:
        sq = sqlite3.connect(shim).execute("SELECT count(*) FROM relationships").fetchone()[0]
    except sqlite3.Error:
        sq = 0
    return pg, sq


async def attribute(label: str, client, subject: str, dsn: str, shim: str) -> str:
    b_pg, b_sq = counts(dsn, shim)
    await client.add_memory(
        subject=subject,
        predicate="requires",
        object="a store attribution probe",
        relationship_type="REQUIRES",
        scope=MemoryScope(kind=ScopeKind.TENANT, scope_id=SCOPE_ID),
    )
    a_pg, a_sq = counts(dsn, shim)
    dest = "POSTGRES" if a_pg > b_pg else ("SQLITE" if a_sq > b_sq else "NEITHER")
    print(f"  {label:34s} postgres {b_pg:>3}->{a_pg:<3}  sqlite {b_sq:>3}->{a_sq:<3}   => {dest}")
    return dest


async def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    dsn = sys.argv[1]
    shim = os.path.join(tempfile.mkdtemp(prefix="dwwiring-"), "graph.db")

    os.environ["MEMOTRON_OPERATIONAL_STORE_DSN"] = dsn
    os.environ.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE", "1")
    os.environ.setdefault("MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE", "4")
    os.environ["MEMOTRON_GRAPH_PATH"] = shim
    os.environ.setdefault("MEMOTRON_PROJECT_ID", SCOPE_ID)

    print("  DSN set; every path below should write to POSTGRES\n")

    results = {}
    results["Memotron(graph_path=)"] = await attribute(
        "A. Memotron(graph_path=)", Memotron(graph_path=shim), "path A probe", dsn, shim
    )

    from memotron.agent_memory_mcp import build_platform_from_env

    plat = build_platform_from_env()
    inner = next(
        (
            getattr(plat, a)
            for a in ("_memotron", "memotron", "_client", "client")
            if getattr(plat, a, None) is not None
        ),
        None,
    )
    if inner is None:
        print("  B. could not reach the platform's inner client — harness needs updating")
        return 2
    results["build_platform_from_env()"] = await attribute(
        "B. build_platform_from_env()", inner, "path B probe", dsn, shim
    )

    strayed = [k for k, v in results.items() if v != "POSTGRES"]
    print(f"\n=== storage wiring: {'FAIL' if strayed else 'PASS'} ===")
    for k in strayed:
        print(f"  - {k} wrote to {results[k]} while a Postgres DSN was set (T0-5)")
    if not strayed:
        print("  every construction path honoured the DSN.")
    return 1 if strayed else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
