#!/usr/bin/env python
"""Rehearse the whole demo headlessly and assert the numbers the runbook quotes.

    uv run demo/selftest.py --reset     # clean run from scratch (recommended)
    uv run demo/selftest.py             # run against the current demo graph

This drives the SAME `AgentMemoryPlatform` methods the `memotron_agent_memory`
MCP tools call — `memory_bootstrap`, `memory_search`, `memory_publish` — with the
exact candidate lines the Stage 1 / Stage 2 prompts are written to produce, then
shells out to the real `demo/dream.py` and `demo/inspect.py`.

It is a rehearsal, not the demo: a live Claude Code session decides *when* and
*how* to publish. What this proves is that once those publishes happen, the
dreaming, supersession, reinforcement, clustering, and token numbers land where
demo/README.md says they do.
"""

from __future__ import annotations

import argparse
import asyncio
import subprocess

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`
from _demo import (
    DEMO_AGENT_ID,
    DEMO_AGENT_NAME,
    DEMO_DIR,
    REPO_ROOT,
    banner,
    build_demo_platform,
    kv,
    section,
    table,
)

# The publish payloads the prompts in demo/prompts/ are written to elicit.
# Keep these and the prompt files in sync — they are the contract between the
# narrative and the numbers.
STAGE_1_PUBLISHES = (
    (
        "Memory: subject=checkout-api; predicate=decided; "
        "object=use PostgreSQL for the reservation ledger; "
        "relationship_type=DECIDES; confidence=0.9",
        "ledger-review-2026-08",
    ),
    (
        "Memory: subject=checkout-api; predicate=requires; "
        "object=reservation lookup p95 latency under 200 ms; "
        "relationship_type=REQUIRES; confidence=0.9",
        "platform-slo-review",
    ),
    (
        "Memory: subject=checkout-api; predicate=should; "
        "object=require a data-platform review on every reservation ledger migration; "
        "relationship_type=SHOULD; confidence=0.9",
        "team-rules-2026-08",
    ),
)

STAGE_2_PUBLISHES = (
    # (a) CORRECTION — same truth slot, different object -> supersession
    (
        "Memory: subject=checkout-api; predicate=decided; "
        "object=use DynamoDB for the reservation ledger; "
        "relationship_type=DECIDES; confidence=0.9",
        "architecture-review-2026-08-11",
    ),
    # (b) REINFORCEMENT — byte-identical restatement -> observed_count rises
    (
        "Memory: subject=checkout-api; predicate=requires; "
        "object=reservation lookup p95 latency under 200 ms; "
        "relationship_type=REQUIRES; confidence=0.9",
        "slo-review-2026-08-12",
    ),
    # (c) + (d) CLUSTER SEEDS — two more siblings of the Stage 1 directive
    (
        "Memory: subject=checkout-api; predicate=should; "
        "object=require a load test on every reservation ledger migration; "
        "relationship_type=SHOULD; confidence=0.9",
        "team-rules-2026-08",
    ),
    (
        "Memory: subject=checkout-api; predicate=should; "
        "object=require a rollback plan on every reservation ledger migration; "
        "relationship_type=SHOULD; confidence=0.9",
        "team-rules-2026-08",
    ),
)

EXPECTED_AFTER_DREAM_1 = {
    "episode_count": 3,
    "context_visible_relationship_count": 3,
    "theme_relationship_count": 0,
    "demoted_relationship_count": 0,
    "superseded_relationship_count": 0,
}

EXPECTED_AFTER_DREAM_2 = {
    "episode_count": 7,
    "context_visible_relationship_count": 3,
    "theme_relationship_count": 1,
    "demoted_relationship_count": 3,
    "superseded_relationship_count": 1,
    "reinforced_relationship_count": 1,
}


def sh(*args: str) -> None:
    print(f"\n$ {' '.join(args)}")
    subprocess.run(args, check=True, cwd=REPO_ROOT)


async def publish_stage(platform, publishes, task_run_id: str) -> None:
    for content, source_reference in publishes:
        result = await platform.memory_publish(
            agent_id=DEMO_AGENT_ID,
            content=content,
            task_run_id=task_run_id,
            source_reference=source_reference,
        )
        kv("published", f"{result.episode_uuid[:8]}  {content[:66]}…")


async def simulate_session(stage: str, publishes) -> None:
    platform, _config = build_demo_platform()
    try:
        bootstrap = await platform.memory_bootstrap(
            agent_id=DEMO_AGENT_ID,
            agent_name=DEMO_AGENT_NAME,
        )
        task_run_id = bootstrap.start.task_run_id
        section(f"Stage {stage}: memory_bootstrap")
        kv("task_run_id", task_run_id)
        kv("injected context chars", len(bootstrap.start.rendered_context))

        section(f"Stage {stage}: memory_search (what the agent recalls first)")
        found = await platform.memory_search(
            agent_id=DEMO_AGENT_ID,
            query="checkout-api reservation ledger decisions requirements rules",
            task_run_id=task_run_id,
        )
        kv("results", len(found.results))
        for item in found.results[:6]:
            kv("  ", item.fact)

        section(f"Stage {stage}: memory_publish x{len(publishes)}")
        await publish_stage(platform, publishes, task_run_id)
    finally:
        platform.client.graph.close()


async def check(label: str, expected: dict[str, int]) -> bool:
    platform, _config = build_demo_platform()
    try:
        proof = await platform.client.memory_evolution(scope=platform.project_scope)
    finally:
        platform.client.graph.close()

    rows = []
    ok = True
    for field, want in expected.items():
        got = getattr(proof, field)
        passed = got == want
        ok = ok and passed
        rows.append([field, str(want), str(got), "PASS" if passed else "FAIL"])
    section(f"Assertions — {label}")
    table(["signal", "expected", "actual", ""], rows)

    section(f"Observed numbers — {label} (quote these in the runbook)")
    for field in (
        "episode_count",
        "active_relationship_count",
        "context_visible_relationship_count",
        "theme_relationship_count",
        "demoted_relationship_count",
        "created_relationship_count",
        "reinforced_relationship_count",
        "superseded_relationship_count",
        "compression_ratio",
        "semantic_dedup_rate",
        "tokens_raw_episodes",
        "tokens_unbudgeted_facts",
        "tokens_rendered_profile",
        "tokens_saved_vs_raw",
        "tokens_saved_by_demotion",
    ):
        value = getattr(proof, field)
        kv(field, f"{value:.3f}" if isinstance(value, float) else value, width=38)
    return ok


async def run(reset: bool) -> int:
    banner("MEMOTRON DEMO — HEADLESS REHEARSAL", "same tool calls, no live session")

    if reset:
        sh(str(DEMO_DIR / "setup.sh"), "--reset")

    sh("uv", "run", str(DEMO_DIR / "inspect.py"), "--save", "before-dream-1")

    banner("STAGE 1 (simulated)", "3 confirmed facts published as candidates")
    await simulate_session("1", STAGE_1_PUBLISHES)

    sh("uv", "run", str(DEMO_DIR / "dream.py"), "--stage", "1")
    sh(
        "uv",
        "run",
        str(DEMO_DIR / "inspect.py"),
        "--diff",
        "before-dream-1",
        "--save",
        "after-dream-1",
    )
    ok_1 = await check("after Dream 1", EXPECTED_AFTER_DREAM_1)

    banner("STAGE 2 (simulated)", "correction + reinforcement + two cluster siblings")
    await simulate_session("2", STAGE_2_PUBLISHES)

    sh("uv", "run", str(DEMO_DIR / "dream.py"), "--stage", "2")
    sh(
        "uv",
        "run",
        str(DEMO_DIR / "inspect.py"),
        "--diff",
        "after-dream-1",
        "--save",
        "after-dream-2",
    )
    ok_2 = await check("after Dream 2", EXPECTED_AFTER_DREAM_2)

    banner("RESULT", "PASS — the runbook numbers are real" if ok_1 and ok_2 else "FAIL")
    return 0 if (ok_1 and ok_2) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="wipe the demo graph and workspace first (recommended)",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args.reset)))


if __name__ == "__main__":
    main()
