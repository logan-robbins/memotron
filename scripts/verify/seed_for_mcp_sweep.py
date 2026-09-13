"""Seed an identical graph and emit real per-store uuids for the MCP sweep.

Prints shell `export SWEEP_*=...` lines to stdout, so:

    eval "$(uv run python scripts/verify/seed_for_mcp_sweep.py)"
    uv run python scripts/verify/sweep_mcp.py <gov_url> <agentmem_url>

Backend comes from the environment (MEMOTRON_OPERATIONAL_STORE_DSN set = Postgres,
absent = SQLite), so the SAME script produces the SAME logical graph on both and the only
variable between two sweep runs is the storage engine.

**Explicitly filters MENTIONS.** On Postgres `relationships_for_scope` returns structural
MENTIONS edges first (T0-4), so harvesting row [0] blindly hands the sweep a structural
edge on one backend and a memory fact on the other — a difference in the HARNESS that
reads as a difference in the PRODUCT. That mistake produced four phantom findings in D-39.
"""

from __future__ import annotations

import asyncio
import os
import sys

from memotron import Memotron, MemoryScope, ScopeKind

SCOPE_ID = "memotron-dogfood"

FACTS = [
    ("the api service", "requires", "a durable store", "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
    ("the admin console", "requires", "an authenticated session", "REQUIRES"),
    ("the reviewer", "should", "check the receipt chain weekly", "SHOULD"),
]


async def main() -> int:
    dw = Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"])
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=SCOPE_ID)

    episode_uuid = None
    for s, p, o, t in FACTS:
        res = await dw.add_memory(subject=s, predicate=p, object=o, relationship_type=t, scope=scope)
        episode_uuid = episode_uuid or getattr(res, "episode_uuid", None)

    rows = dw.graph.relationships_for_scope(scope.key)
    facts = [r for r in rows if r.type != "MENTIONS"]
    if not facts:
        print("  no non-MENTIONS relationship after seeding", file=sys.stderr)
        return 1

    mentions = len(rows) - len(facts)
    print(
        f"  seeded scope={scope.key} rows={len(rows)} facts={len(facts)} "
        f"mentions={mentions} (harvesting a fact, not row[0])",
        file=sys.stderr,
    )

    print(f"export SWEEP_RELATIONSHIP_UUID={facts[0].uuid}")
    if episode_uuid:
        print(f"export SWEEP_EPISODE_UUID={episode_uuid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
