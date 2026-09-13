"""WS-10 coherence remediation sweep — operator demo (hermetic, offline).

Brings an existing memory graph onto the cross-artifact coherence implementation.
It reproduces the operator incident that motivated WS-10 — a feedback directive
that escalated across many cycles because a stale, self-authored skill silently
governed the behavior — then sweeps every scope:

    1. DRY RUN  — detect windup / contradiction incidents and print the plan
                  (no holds written, no decisions recorded, nothing retired).
    2. APPLY    — write anti-windup holds + record incidents in the audit log.
    3. APPLY + RETIRE — additionally soft-retire the futilely-escalated
                  directives (evidence preserved; never destroyed).

No API keys, no network calls.

    uv run examples/coherence_remediation.py
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory

from memotron import (
    ArtifactClass,
    ArtifactDirective,
    DirectiveStance,
    Memotron,
    MemoryScope,
    PersistentArtifact,
    ScopeKind,
    StaticArtifactSource,
)


async def seed_escalated_directive(client: Memotron, scope: MemoryScope, obj: str, cycles: int) -> None:
    """Simulate the same feedback given over and over (the windup signature)."""
    for _ in range(cycles):
        await client.add_memory(
            subject="Agent",
            predicate="should",
            object=obj,
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.6,
            source_text="(repeated operator feedback)",
        )


def print_report(title: str, report) -> None:
    print(f"\n{'=' * 78}\n  {title}  (dry_run={report.dry_run})\n{'=' * 78}")
    print(
        f"  scopes swept: {report.scope_count}   affected: {report.affected_scope_count}   "
        f"windup: {report.total_windup}   contradiction: {report.total_contradiction}   "
        f"held: {report.total_held}   retired: {report.total_retired}"
    )
    for scope_result in report.scopes:
        if scope_result.report.incident_count == 0:
            continue
        print(f"\n  scope {scope_result.scope.key}:")
        for incident in scope_result.report.incidents:
            gov = incident.governing_directive
            print(f"    • [{incident.kind.value}] {incident.subject}")
            print(f"        {incident.summary}")
            if gov is not None:
                print(
                    f"        governing artifact: {gov.artifact_class.value} '{gov.artifact_id}'"
                    f" (precedence {gov.precedence}, author {gov.author})"
                )
            print(f"        proposed repair: {incident.proposed_repair}")


async def main() -> None:
    with TemporaryDirectory() as tmp:
        client = Memotron(graph_path=f"{tmp}/remediation_demo.sqlite")

        # Two existing scopes carrying escalation damage from the coherence-blind era.
        cost_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="cost-eval-agent")
        clean_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="general-agent")
        await seed_escalated_directive(
            client, cost_scope, "evaluate the cost efficiency of other agents thoroughly", cycles=5
        )
        # A scope that also escalated, but has NO stale governing artifact — must be left alone.
        await seed_escalated_directive(client, clean_scope, "summarize meeting notes within the hour", cycles=5)

        # Register the agent's live skills/files (what the coherence cycle needs to see).
        client.register_artifact_source(
            StaticArtifactSource(
                [
                    PersistentArtifact(
                        artifact_id="cost-efficiency-evaluator",
                        artifact_class=ArtifactClass.SKILL,
                        scope=cost_scope,
                        location=".claude/skills/cost-efficiency-evaluator/SKILL.md",
                        author="self:agent-session-2026-05",
                        self_authored=True,
                        updated_at=datetime.now(UTC) - timedelta(days=30),
                        directives=[
                            ArtifactDirective(
                                subject="evaluate cost efficiency of other agents",
                                instruction="A cost-efficiency check passes when total tokens are below the static budget.",
                                stance=DirectiveStance.ASSERT,
                            )
                        ],
                    )
                ]
            )
        )

        # 1) DRY RUN — preview the plan; writes nothing. (Run this first, every time.)
        preview = await client.remediate_coherence(dry_run=True, retire_held_directives=True)
        print_report("DRY RUN (preview — nothing written)", preview)

        # 2) APPLY (+ opt-in retire) — write holds, record incidents, soft-retire the
        #    held directives. Idempotent: a second sweep won't re-flag an already-held
        #    directive, so this is a one-shot operator action after reviewing the preview.
        applied = await client.remediate_coherence(dry_run=False, retire_held_directives=True)
        print_report("APPLY + RETIRE (holds written, incidents recorded, directives retired)", applied)

        # Evidence survives retirement — never destroyed.
        retired_uuid = applied.scopes[0].retired_relationship_uuids[0]
        evidence = await client.memory_evidence(relationship_uuid=retired_uuid, scope=cost_scope)
        print(f"\n  Persisted coherence incidents (survive restart): {len(await client.coherence_incidents())}")
        print(
            f"  Retired directive {retired_uuid[:8]}… still has "
            f"{len(evidence.episodes)} source episode(s) in the audit trail."
        )
        print(
            "  Done. The stale SKILL is flagged for repair; the escalated memory is held,"
            " soft-retired, evidence intact.\n"
        )


if __name__ == "__main__":
    asyncio.run(main())
