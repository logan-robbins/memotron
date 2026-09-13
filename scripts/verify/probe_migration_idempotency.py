"""T1-25 + T1-27: does a tenant migration refuse in a common state, and does retrying it converge?

Two filed claims (AGENT-SOURCED), tested together because the first provokes the second:

  T1-25  migration fails verification when a tenant has agent episodes but no ACTIVE
         relationships -- the normal post-ingestion, pre-formation state. `_migrate_agent_scopes`
         sits inside `if preview.active_relationship_count +
         preview.agent_scoped_relationship_count > 0:` (`migration.py:874`) so
         `agent_episodes_migrated` stays 0, while `_verify_migration_counts` (`:705`) compares it
         against `preview.agent_scoped_episode_count` regardless.

  T1-27  migration has no idempotency, so a retry does not converge.

PRECONDITION that cost a run: agent scopes come from `store.tenant_agents()`
(`migration.py:184`), so an agent must be REGISTERED for its episodes to be counted at all.
Without `register_tenant_agent` the preview reports `agent_ep=0`, the guard is irrelevant, and
the migration succeeds -- which reads exactly like a refutation.

    uv run python scripts/verify/probe_migration_idempotency.py

exit 0 = migration refuses cleanly AND retries converge
exit 1 = a defect reproduced (see which arm)
exit 2 = harness error
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

os.environ.setdefault("MEMOTRON_ALLOW_EPHEMERAL_KEK", "1")

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.migration import migrate_tenant_memory, preview_tenant_migration
from memotron.models import EpisodeType
from memotron.storage import open_storage

DEST = MemoryScope(kind=ScopeKind.TENANT, scope_id="beta")


async def arm_blocked() -> bool:
    """T1-25: agent episodes + zero relationships -> does migration refuse?"""
    tmp = Path(tempfile.mkdtemp(prefix="dwmig25-"))
    dw = Memotron(graph_path=str(tmp / "g.sqlite"))
    dw.graph.register_tenant_agent(tenant_id="alpha", agent_id="agent-1", name="Agent One")
    await dw.add_episode(
        name="ep",
        episode_body="Ryan prefers postgres for the operational store.",
        source=EpisodeType.TEXT,
        scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="agent-1"),
        reference_time=datetime.now(UTC),
    )
    store = open_storage(str(tmp / "g.sqlite"))
    preview = preview_tenant_migration(store, source_tenant_id="alpha", dest_tenant_id="beta")
    print(
        f"    preview: active_rel={preview.active_relationship_count} "
        f"agent_rel={preview.agent_scoped_relationship_count} "
        f"agent_ep={preview.agent_scoped_episode_count}"
    )
    if preview.agent_scoped_episode_count == 0:
        print("    PRECONDITION FAILED: no agent-scoped episode was counted (agent not registered?)")
        raise SystemExit(2)
    try:
        migrate_tenant_memory(
            store, source_tenant_id="alpha", dest_tenant_id="beta", dest_store=store, purge_source=False
        )
        print("    migration SUCCEEDED -- T1-25 not reproduced")
        return False
    except RuntimeError as exc:
        print(f"    migration REFUSED: {str(exc)[:104]}")
        return True


async def arm_idempotency() -> int:
    """T1-27: run the same successful migration three times and count destination rows."""
    tmp = Path(tempfile.mkdtemp(prefix="dwmig27-"))
    dw = Memotron(graph_path=str(tmp / "src.sqlite"))
    await dw.add_memory(
        subject="the api",
        predicate="requires",
        object="a durable store",
        relationship_type="REQUIRES",
        scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="alpha"),
    )
    src = open_storage(str(tmp / "src.sqlite"))
    dst = open_storage(str(tmp / "dst.sqlite"))
    counts = []
    for attempt in (1, 2, 3):
        migrate_tenant_memory(src, source_tenant_id="alpha", dest_tenant_id="beta", dest_store=dst, purge_source=False)
        rows = [r for r in dst.relationships_for_scope(DEST.key) if r.type != "MENTIONS"]
        counts.append(len(rows))
        print(f"    after migrate #{attempt}: {len(rows)} row(s) for 1 source fact")
    return counts[-1]


async def main() -> int:
    print("\n  === T1-25: agent episodes, no active relationships ===")
    blocked = await arm_blocked()

    print("\n  === T1-27: the same migration, run three times ===")
    final = await arm_idempotency()

    print()
    failures = []
    if blocked:
        failures.append("T1-25: migration refuses in the post-ingestion, pre-formation state")
    if final > 1:
        failures.append(f"T1-27: three runs produced {final} copies of one fact -- no convergence")
    if not failures:
        print("  === both REFUTED ===")
        return 0
    print("  === CONFIRMED ===")
    for f in failures:
        print(f"    - {f}")
    if blocked and final > 1:
        print("\n  These compound. T1-25 makes the abort routine, and the natural operator")
        print("  response to an abort -- retry -- is what duplicates the destination. Note the")
        print("  refusal itself is fail-CLOSED (it declines to purge the source), so T1-25 alone")
        print("  costs availability, not data.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except SystemExit:
        raise
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
