#!/usr/bin/env python
"""Run one Memotron dreaming session against the isolated demo tenant.

    uv run demo/dream.py --stage 1
    uv run demo/dream.py --stage 2

This is the demo's dreaming beat. It forces the three dream jobs — FORMATION,
CONSOLIDATION, PRUNING — for the demo tenant and prints exactly what each one
did, instead of waiting for a cadence timer.

Why explicit instead of `memory_refresh`
----------------------------------------
In production the Claude Code PreCompact / PostCompact / SessionEnd hooks call
`run_due_dreams`, which honours each job's cadence (formation 1 s,
consolidation 300 s, pruning 600 s). That is correct for real work and useless
for a demo: the second dreaming session would silently skip consolidation
because 300 s had not elapsed. `run_dream_job` runs a named job regardless of
cadence, on the same engine, same Motive, same policy, same receipts — so both
dreaming moments are complete, comparable, and repeatable on stage.

The hooks stay wired and still work; keep the Claude Code session OPEN between
stages so SessionEnd does not dream before you do.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime

import _demopath  # noqa: F401  MUST BE FIRST — unshadows stdlib `inspect`
from _demo import (
    DEMO_AGENT_ID,
    DEMO_GRAPH_PATH,
    DEMO_PROJECT_ID,
    banner,
    build_demo_platform,
    kv,
    section,
    short,
    table,
)

JOBS = (
    ("formation-default", "FORMATION", "episodes -> typed facts (dedup / reinforce / supersede)"),
    ("consolidation-default", "CONSOLIDATION", "cluster facts -> THEME, demote members"),
    ("pruning-default", "PRUNING", "expiry, repair, retention, growth backstop"),
)


async def run(stage: str) -> None:
    started_at = datetime.now(UTC)
    platform, _config = build_demo_platform()
    try:
        client = platform.client
        project = platform.project_scope
        personal = platform.user_scope
        continuity = platform.agent_scope(DEMO_AGENT_ID)

        banner(
            f"DREAMING SESSION {stage}".strip(),
            f"tenant {DEMO_PROJECT_ID}   graph {DEMO_GRAPH_PATH}",
        )

        section("Before — what is waiting to be dreamed")
        before = await _pending(client, {"project": project, "personal": personal})
        table(
            ["scope", "episodes", "unprocessed", "active facts"],
            [[name, str(row["episodes"]), str(row["pending"]), str(row["active"])] for name, row in before.items()],
        )
        if before["project"]["pending"] == 0:
            print("  note: nothing new is queued for the project scope — the dream")
            print("        will be a no-op there, which is itself worth showing.")

        section("Running dream jobs (forced, cadence ignored)")
        totals = {
            "processed_episodes": 0,
            "created_relationships": 0,
            "reinforced_relationships": 0,
            "superseded_relationships": 0,
            "pruned_relationships": 0,
            "decision_count": 0,
        }
        rows: list[list[str]] = []
        for job_name, label, _purpose in JOBS:
            for scope_label, scope in (
                ("project", project),
                ("personal", personal),
                ("continuity", continuity),
            ):
                if scope is None:
                    continue
                # Personal + continuity only need formation; the demo's whole
                # story lives in the project scope.
                if label != "FORMATION" and scope_label != "project":
                    continue
                result = await client.run_dream_job(
                    job_name=job_name,
                    tenant_id=DEMO_PROJECT_ID,
                    agent_id=DEMO_AGENT_ID,
                    scope=scope,
                )
                for job_run in result.job_runs:
                    for key in totals:
                        totals[key] += getattr(job_run, key)
                    rows.append(
                        [
                            label,
                            scope_label,
                            str(job_run.processed_episodes),
                            str(job_run.created_relationships),
                            str(job_run.reinforced_relationships),
                            str(job_run.superseded_relationships),
                            str(job_run.pruned_relationships),
                            short(job_run.run_uuid or "", 8),
                        ]
                    )
        table(
            ["job", "scope", "epis", "new", "reinf", "supers", "pruned", "run"],
            rows,
        )

        section("This dreaming session")
        kv("episodes processed", totals["processed_episodes"])
        kv("facts created", totals["created_relationships"])
        kv("facts reinforced", totals["reinforced_relationships"])
        kv("facts superseded", totals["superseded_relationships"])
        kv("facts pruned", totals["pruned_relationships"])
        kv("dream-agent decisions", totals["decision_count"])

        section("Dream-agent decisions from THIS run (deterministic transport)")
        decisions = [d for d in await client.dream_decisions(limit=60) if d.ran_at >= started_at]
        table(
            ["decision", "subject"],
            [[d.decision_type, (d.subject_name or "-")] for d in decisions],
        )
        if not decisions:
            print("  (no maintenance decisions were needed on this run)")

        section("Next")
        print("  uv run demo/inspect.py --diff <previous-label> --save <new-label>")
        print()
    finally:
        platform.client.graph.close()


async def _pending(client, scopes: dict) -> dict:
    out: dict = {}
    for name, scope in scopes.items():
        if scope is None:
            continue
        proof = await client.memory_evolution(scope=scope)
        out[name] = {
            "episodes": proof.episode_count,
            "pending": proof.pending_episode_count,
            "active": proof.context_visible_relationship_count,
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one demo dreaming session.")
    parser.add_argument(
        "--stage",
        default="",
        help='label for the banner, e.g. "1" or "2"',
    )
    args = parser.parse_args()
    asyncio.run(run(args.stage.strip()))


if __name__ == "__main__":
    main()
