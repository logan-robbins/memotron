"""Variant probe: does the dream agent approve formation under a different
motive / prompt profile?

Env:
  DW_MOTIVE          motive name to force on the formation job ('' = leave)
  DW_PROMPT_PROFILE  prompt profile key to force        ('' = leave)

exit 0 = GREEN (>=1 dream-agent fact), 1 = RED, 2 = harness error.

CITATIONS REPAIRED 2026-08-31: every `file.py:NNN` below was rewritten after the module
split dissolved the god files they named. Each now carries the SYMBOL as well as the line,
so the next move makes them findable by grep rather than silently wrong.
"""

from __future__ import annotations

import asyncio
import os
import sys
import traceback
from datetime import UTC, datetime

DEFAULT_PROSE = (
    "We decided the Postgres cutover is blocked until a real key manager exists, because "
    "the backend defaults to an ephemeral per-process KEK. Ryan prefers hardening the "
    "latest environment before building the stage and prod ladder."
)
PROSE = os.environ.get("DW_PROSE", "").strip() or DEFAULT_PROSE


async def main() -> int:
    from memotron.agent_memory_mcp import build_platform_from_env
    from memotron.graph import PropertyGraphStore

    # DW_GATE_BLIND=1 : simulate the candidate fix — withhold graph context from
    # the FORMATION GATE only (dreaming/_formation.py:333, the formation_episode_selected _decide call) while leaving it intact for the
    # EXTRACTION prompt (dreaming/_consolidation.py:84, _graph_context_for_episode). One-line change in real code.
    if os.environ.get("DW_GATE_BLIND") == "1":
        from memotron.dreaming import DreamEngine

        _orig = DreamEngine._decide

        async def _blind(self, **kw):
            if kw.get("decision_type") == "formation_episode_selected":
                kw["context_facts"] = ()
            return await _orig(self, **kw)

        DreamEngine._decide = _blind
        print("  [PATCH] formation gate blinded to graph context; extraction unchanged")

    graph = os.environ["MEMOTRON_GRAPH_PATH"]
    motive = os.environ.get("DW_MOTIVE", "").strip()
    profile = os.environ.get("DW_PROMPT_PROFILE", "").strip()

    if os.environ.get("DW_WIPE_FACTS") == "1":
        import sqlite3

        con = sqlite3.connect(graph)
        n = con.execute("select count(*) from relationships").fetchone()[0]
        con.execute("delete from relationships")
        con.commit()
        con.close()
        print(f"  [wiped {n} pre-existing relationships -> graph context should be empty]")

    p = build_platform_from_env()
    dw = p.client

    # DW_SEED_BENIGN=1 : after wiping, seed 3 neutral facts so context is
    # non-empty but carries no unresolved/negative content. Discriminates
    # "negative sentiment" from "any context at all".
    if os.environ.get("DW_SEED_BENIGN") == "1":
        from memotron import MemoryScope, ScopeKind

        sc = MemoryScope(kind=ScopeKind.TENANT, scope_id=os.environ["MEMOTRON_PROJECT_ID"])
        for subj, obj in [
            ("the team", "British English spelling in docs"),
            ("the team", "standups at 9am"),
            ("Ryan", "dark mode in all tooling"),
        ]:
            await dw.add_memory(subject=subj, predicate="prefers", object=obj, relationship_type="PREFERS", scope=sc)
        print("  [seeded 3 BENIGN context facts]")
    eng = dw._engine

    await p.memory_publish(agent_id="claude-code", content=PROSE, task_run_id="variant-1")

    job = next(j for j in dw.config.jobs if j.kind.value == "formation")
    updates = {}
    if motive:
        updates["motive"] = None if motive == "NONE" else motive
    if profile:
        updates["prompt_profile"] = profile
    ctx = os.environ.get("DW_CONTEXT", "").strip()
    if ctx == "off":
        updates["context_policy"] = job.context_policy.model_copy(update={"enabled": False})
    elif ctx.isdigit():
        updates["context_policy"] = job.context_policy.model_copy(update={"max_relationships": int(ctx)})
    if updates:
        job = job.model_copy(update=updates)
    print(f"  context_policy: enabled={job.context_policy.enabled} max={job.context_policy.max_relationships}")
    print(f"  motive={job.motive!r}  prompt_profile={job.prompt_profile!r}")

    run = await eng.run_job(job=job, episodes=dw.graph.episodes(), now=datetime.now(UTC))
    print(
        f"  decisions={run.decision_count}  processed={run.processed_episodes}  "
        f"created_rels={run.created_relationships}"
    )

    decs = await dw.dream_decisions(limit=2)
    for d in decs:
        print(f"    verdict: {str(d.summary)[:130]}")

    store = PropertyGraphStore(graph)
    dreamed = [
        str(r.properties.get("fact"))[:80]
        for sc in store.scopes()
        for r in store.relationships_for_scope(sc.key)
        if str(r.properties.get("created_by", "")).startswith("dream-agent")
    ]
    print(f"  dream-agent facts: {len(dreamed)}")
    for f in dreamed[:6]:
        print("     +", f)
    return 0 if dreamed else 1


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception:
        traceback.print_exc()
        sys.exit(2)
