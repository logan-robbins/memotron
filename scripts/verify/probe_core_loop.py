"""The product promise, not the surfaces.

Two things users depend on and neither is covered by the sweeps:

  A. STALENESS — a newer contradicting fact must supersede the older one. The profile
     must show current truth; history must be preserved, not deleted.
  B. COMPACTION — the context injected at session start / after compaction must contain
     the current fact and NOT the stale one.

Uses add_memory (client-managed) so it tests supersession and retrieval directly,
independent of the LLM formation gate (which is separately suppressed — see T1-1).

exit 0 = all promises hold; 1 = a promise is broken; 2 = harness error.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback

from memotron import Memotron, MemoryScope, ScopeKind

OLD = "Postgres 15"
NEW = "Postgres 16"
FAILURES: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' — ' + detail) if detail else ''}")
    if not ok:
        FAILURES.append(name)


async def main() -> int:
    graph = os.environ["MEMOTRON_GRAPH_PATH"]
    dw = Memotron(graph_path=graph)
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=os.environ["MEMOTRON_PROJECT_ID"])

    print("\n--- A. staleness / supersession ---")
    await dw.add_memory(
        subject="the database", predicate="requires", object=OLD, relationship_type="REQUIRES", scope=scope
    )
    hits = await dw.search(query="database", scope=scope)
    check("older fact is retrievable", any(OLD in h.fact for h in hits))

    # the contradicting update
    await dw.add_memory(
        subject="the database", predicate="requires", object=NEW, relationship_type="REQUIRES", scope=scope
    )

    hits = await dw.search(query="database", scope=scope)
    facts = [h.fact for h in hits]
    check("newer fact is retrievable", any(NEW in f for f in facts))
    check(
        "STALE fact no longer returned by search",
        not any(OLD in f for f in facts),
        f"search returned: {[f[:48] for f in facts]}",
    )

    prof = await dw.profile(scope=scope)
    rendered = getattr(prof, "rendered_context", "") or str(prof)
    check("profile shows current truth", NEW in rendered)
    check("profile does NOT show stale truth", OLD not in rendered)

    # history must be preserved, not destroyed
    tl = await dw.truth_timeline(
        scope=scope, subject="the database", predicate="requires", relationship_type="REQUIRES"
    )
    objs = [getattr(e, "object", "") for e in tl]
    check(
        "supersession history preserved",
        any(OLD in str(o) for o in objs),
        f"timeline objects: {[str(o)[:24] for o in objs]}",
    )
    current = [e for e in tl if getattr(e, "is_current", False)]
    check("exactly one current version", len(current) == 1, f"got {len(current)}")
    if current:
        check("the current version is the NEW fact", NEW in str(getattr(current[0], "object", "")))

    print("\n--- B. compaction / context injection ---")
    from memotron.agent_memory_mcp import build_platform_from_env

    p = build_platform_from_env()
    p.register_agent(agent_id="core-loop", agent_name="Core Loop")
    started = await p.memory_start(agent_id="core-loop")
    ctx = getattr(started, "rendered_context", "") or ""
    check("session-start injects non-empty context", bool(ctx.strip()), f"{len(ctx)} chars")
    check("injected context carries current truth", NEW in ctx)
    check(
        "injected context omits stale truth", OLD not in ctx, "stale fact would be re-injected into every new session"
    )

    # PostCompact writes a checkpoint (memory_log); the NEXT memory_start is what
    # re-injects context. memory_refresh returns run records, not context — asserting
    # rendered_context on it tests the wrong method.
    await p.memory_log(agent_id="core-loop", summary="compaction checkpoint")
    await p.memory_refresh(agent_id="core-loop")
    restarted = await p.memory_start(agent_id="core-loop")
    rctx = getattr(restarted, "rendered_context", "") or ""
    check("context after compaction still carries current truth", NEW in rctx)
    check("context after compaction still omits stale truth", OLD not in rctx)
    check(
        "context is token-budgeted",
        getattr(restarted, "tokens_used", 0) > 0,
        f"tokens_used={getattr(restarted, 'tokens_used', None)}",
    )

    print("\n  " + ("ALL PROMISES HOLD" if not FAILURES else f"BROKEN: {', '.join(FAILURES)}"))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
