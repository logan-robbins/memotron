"""WS-10: cross-artifact behavioral coherence.

The headline scenario is the operator's real incident: an agent kept forming
stricter and stricter feedback memories because the same feedback was given over
and over, and the root cause was a self-authored skill (written a month earlier,
never updated) that silently overrode the feedback at execution time. Ordinary
(memory-vs-memory) dreaming is structurally blind to that contradiction; the
coherence cycle attributes the escalation to the stale skill.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    ArtifactClass,
    ArtifactDirective,
    CoherenceIncidentKind,
    CoherenceIncidentStatus,
    CoherencePolicy,
    DirectiveStance,
    Memotron,
    MemoryScope,
    PersistentArtifact,
    ScopeKind,
    StaticArtifactSource,
)

COST_TOPIC = "evaluate the cost efficiency of other agents thoroughly"
SKILL_TOPIC = "evaluate cost efficiency of other agents"


def agent_scope() -> MemoryScope:
    return MemoryScope(kind=ScopeKind.AGENT, scope_id="cost-eval-agent")


async def escalate_feedback_directive(
    client: Memotron, scope: MemoryScope, *, cycles: int, confidence: float = 0.6
) -> str:
    """Add the same behavioral directive *cycles* times (the repeated feedback)."""
    relationship_uuid = ""
    for _ in range(cycles):
        result = await client.add_memory(
            subject="Agent",
            predicate="should",
            object=COST_TOPIC,
            relationship_type="SHOULD",
            scope=scope,
            confidence=confidence,
            source_text="Be more thorough about how you evaluate other agents' cost efficiency.",
        )
        relationship_uuid = result.relationship_uuid
    return relationship_uuid


def stale_self_authored_skill(
    scope: MemoryScope,
    *,
    stance: DirectiveStance = DirectiveStance.ASSERT,
    updated_at: datetime | None = None,
) -> PersistentArtifact:
    return PersistentArtifact(
        artifact_id="cost-efficiency-evaluator",
        artifact_class=ArtifactClass.SKILL,
        scope=scope,
        location=".claude/skills/cost-efficiency-evaluator/SKILL.md",
        author="self:agent-session-2026-05",
        self_authored=True,
        updated_at=updated_at if updated_at is not None else datetime.now(UTC) - timedelta(days=30),
        directives=[
            ArtifactDirective(
                subject=SKILL_TOPIC,
                instruction="A cost-efficiency check passes when total tokens are below the static budget.",
                stance=stance,
            )
        ],
    )


@pytest.mark.asyncio
async def test_escalation_windup_attributed_to_stale_self_authored_skill(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()

    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.run_coherence_scan(scope=scope)

    # Exactly one incident, and it is the windup (not double-counted as a contradiction).
    assert report.escalation_windup_count == 1
    assert report.contradiction_count == 0
    assert report.incident_count == 1

    incident = report.incidents[0]
    assert incident.kind == CoherenceIncidentKind.ESCALATION_WINDUP
    assert incident.escalation_cycles == 4

    # Culprit attribution points at the stale skill, not the escalating memory.
    assert incident.escalating_directive is not None
    assert incident.escalating_directive.relationship_uuid == memory_uuid
    assert incident.escalating_directive.artifact_class == ArtifactClass.MEMORY
    assert incident.governing_directive is not None
    assert incident.governing_directive.artifact_class == ArtifactClass.SKILL
    assert incident.governing_directive.artifact_id == "cost-efficiency-evaluator"

    # Precedence gap (skill 30 - memory 10) and a strong topic identity.
    assert incident.precedence_gap == 20
    assert incident.identity_cosine >= 0.5

    # The rationale explains *why* this is the culprit, and the repair targets the skill.
    assert "self-authored" in incident.attribution_rationale
    assert "cost-efficiency-evaluator" in incident.proposed_repair
    assert "skill" in incident.proposed_repair.lower()


@pytest.mark.asyncio
async def test_windup_marks_directive_coherence_hold(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    await client.run_coherence_scan(scope=scope)

    held = client.graph.get_relationship(memory_uuid)
    assert held.properties.get("coherence_hold") is True
    assert held.properties.get("coherence_incident_id")


@pytest.mark.asyncio
async def test_low_confidence_attribution_requests_disambiguation_without_holding_memory(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "low-confidence.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.run_coherence_scan(
        scope=scope,
        policy=CoherencePolicy(min_attribution_confidence=1.0),
    )

    incident = report.incidents[0]
    assert incident.attribution_confidence < 1.0
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is not True
    requests = await client.coherence_disambiguation_requests(scope=scope)
    assert len(requests) == 1
    request = requests[0]
    assert request.incident_id == incident.incident_id
    assert request.governing_artifact_id == "cost-efficiency-evaluator"
    assert request.minimum_confidence == 1.0
    assert request.required_evidence


@pytest.mark.asyncio
async def test_coherence_hold_freezes_directive_confidence(tmp_path: Path) -> None:
    """Anti-windup actuator: once held, repeated feedback no longer strengthens the memory."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4, confidence=0.6)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))
    await client.run_coherence_scan(scope=scope)

    # WS-16 T13 evidence accumulation: create at 0.6 then three reinforcing
    # observations, each contributing gain=0.25 of the remaining headroom
    # scaled by the observation's confidence (0.6).
    accumulated = 0.6
    for _ in range(3):
        accumulated = min(0.99, accumulated + (1.0 - accumulated) * 0.25 * 0.6)
    before = client.graph.get_relationship(memory_uuid)
    assert before.properties.get("confidence") == pytest.approx(accumulated)

    # The user gives the same feedback again, now with high confidence — normally
    # evidence accumulation would raise confidence further. With the hold in
    # place, it is frozen.
    await client.add_memory(
        subject="Agent",
        predicate="should",
        object=COST_TOPIC,
        relationship_type="SHOULD",
        scope=scope,
        confidence=0.95,
    )

    after = client.graph.get_relationship(memory_uuid)
    assert after.properties.get("confidence") == pytest.approx(accumulated)  # frozen, not escalated
    assert after.properties.get("observed_count") == 5  # still recorded as evidence
    assert after.properties.get("escalation_suppressed_count") == 1


@pytest.mark.asyncio
async def test_incident_round_trips_through_audit_log(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))
    await client.run_coherence_scan(scope=scope)

    incidents = await client.coherence_incidents(scope=scope)
    assert len(incidents) == 1
    assert incidents[0].kind == CoherenceIncidentKind.ESCALATION_WINDUP
    assert incidents[0].status == CoherenceIncidentStatus.OPEN
    assert incidents[0].governing_directive.artifact_id == "cost-efficiency-evaluator"

    decisions = await client.dream_decisions(limit=20)
    assert any(d.decision_type == "coherence_escalation_windup" for d in decisions)


@pytest.mark.asyncio
async def test_fresh_governing_artifact_does_not_trigger_windup(tmp_path: Path) -> None:
    """A skill updated *after* the feedback is not a windup culprit (but a stance clash
    is still surfaced as a plain cross-artifact contradiction)."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    fresh_skill = stale_self_authored_skill(scope, updated_at=datetime.now(UTC) + timedelta(days=1))
    client.register_artifact_source(StaticArtifactSource([fresh_skill]))

    report = await client.run_coherence_scan(scope=scope)

    assert report.escalation_windup_count == 0  # freshness defeats the windup attribution
    assert report.contradiction_count == 1  # REQUIRE (memory) vs ASSERT (skill) still clashes


@pytest.mark.asyncio
async def test_below_escalation_threshold_does_not_trigger_windup(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=1)  # observed_count == 1 < K(3)
    # GUIDE stance so no contradiction either — isolating the escalation-cycle gate.
    skill = stale_self_authored_skill(scope, stance=DirectiveStance.GUIDE)
    client.register_artifact_source(StaticArtifactSource([skill]))

    report = await client.run_coherence_scan(scope=scope)
    assert report.incident_count == 0


@pytest.mark.asyncio
async def test_cross_artifact_contradiction_between_memory_and_file(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    pii_topic = "redact personally identifiable information before logging"

    # A single high-trust requirement (observed_count 1 → not a windup candidate).
    await client.add_memory(
        subject="Agent",
        predicate="requires",
        object=pii_topic,
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.95,
    )
    note = PersistentArtifact(
        artifact_id="logging-notes",
        artifact_class=ArtifactClass.FILE,
        scope=scope,
        location="notes/logging.md",
        author="self:agent",
        directives=[
            ArtifactDirective(
                subject=pii_topic,
                instruction="PII redaction is handled upstream; this module can log raw payloads.",
                stance=DirectiveStance.ASSERT,
            )
        ],
    )
    client.register_artifact_source(StaticArtifactSource([note]))

    report = await client.run_coherence_scan(scope=scope)
    assert report.escalation_windup_count == 0
    assert report.contradiction_count == 1
    incident = report.incidents[0]
    assert incident.kind == CoherenceIncidentKind.CROSS_ARTIFACT_CONTRADICTION
    assert incident.governing_directive.artifact_class == ArtifactClass.FILE
    assert incident.precedence_gap == 10  # file 20 - memory 10


@pytest.mark.asyncio
async def test_no_artifact_sources_yields_no_incidents(tmp_path: Path) -> None:
    """Coherence over memory alone never invents an incident (memory-vs-memory is dreaming's job)."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=5)

    report = await client.run_coherence_scan(scope=scope)
    assert report.incident_count == 0
    assert report.directive_count == 1
    assert report.per_class_directive_counts == {"memory": 1}


@pytest.mark.asyncio
async def test_artifact_source_scope_isolation(tmp_path: Path) -> None:
    """A skill registered under a different scope cannot raise an incident in this scope."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    other = MemoryScope(kind=ScopeKind.AGENT, scope_id="other-agent")
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(other)]))

    report = await client.run_coherence_scan(scope=scope)
    assert report.incident_count == 0


@pytest.mark.asyncio
async def test_coherence_scan_is_idempotent_across_runs(tmp_path: Path) -> None:
    """A held directive is not re-raised on subsequent scans (no duplicate incidents)."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    first = await client.run_coherence_scan(scope=scope)
    assert first.escalation_windup_count == 1

    second = await client.run_coherence_scan(scope=scope)
    assert second.escalation_windup_count == 0  # already held → not re-detected

    # The audit log holds exactly one incident, not a duplicate per scan.
    incidents = await client.coherence_incidents(scope=scope)
    assert len(incidents) == 1


@pytest.mark.asyncio
async def test_windup_handles_timezone_naive_artifact_timestamp(tmp_path: Path) -> None:
    """A file-style naive updated_at (e.g. datetime.fromtimestamp(mtime)) must not crash."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    # Deliberately naive -- the assertion on the next line is the point.
    naive_month_ago = datetime.now().replace(tzinfo=None) - timedelta(days=30)  # noqa: DTZ005
    assert naive_month_ago.tzinfo is None
    client.register_artifact_source(
        StaticArtifactSource([stale_self_authored_skill(scope, updated_at=naive_month_ago)])
    )

    report = await client.run_coherence_scan(scope=scope)  # must not raise
    assert report.escalation_windup_count == 1


@pytest.mark.asyncio
async def test_coherence_incidents_survive_a_flood_of_other_decisions(tmp_path: Path) -> None:
    """coherence_incidents() filters at the query layer, so unrelated decisions can't bury it."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))
    await client.run_coherence_scan(scope=scope)

    # Flood the audit log with many non-coherence decisions recorded after the incident.
    from memotron.models import DreamDecisionRecord, DreamJobKind

    for i in range(300):
        client.graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=datetime.now(UTC) + timedelta(seconds=i + 1),
                job_name="formation-default",
                job_kind=DreamJobKind.FORMATION,
                agent_id="a",
                agent_name="a",
                agent_scope=scope,
                decision_type="formation_episode_selected",
                summary="noise",
            )
        )

    incidents = await client.coherence_incidents(scope=scope)
    assert len(incidents) == 1  # still found despite 300 newer non-coherence decisions


@pytest.mark.asyncio
async def test_remediation_dry_run_previews_without_writing(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.remediate_coherence(dry_run=True)

    assert report.dry_run is True
    assert report.total_windup == 1
    assert report.total_held == 1  # planned, not applied
    assert report.affected_scope_count == 1
    # Nothing was written: no hold on the relationship, no decision in the log.
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is None
    assert await client.coherence_incidents(scope=scope) == []


@pytest.mark.asyncio
async def test_remediation_apply_writes_holds_and_records_incidents(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.remediate_coherence(dry_run=False)

    assert report.dry_run is False
    assert report.total_held == 1
    assert report.total_retired == 0  # retirement is opt-in
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is True
    assert len(await client.coherence_incidents(scope=scope)) == 1


@pytest.mark.asyncio
async def test_remediation_retire_is_opt_in_and_non_destructive(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.remediate_coherence(dry_run=False, retire_held_directives=True)

    assert report.total_retired == 1
    # Soft-retired: excluded from active retrieval but evidence/history preserved.
    active = await client.search(query="cost efficiency", scope=scope)
    assert all(r.relationship_uuid != memory_uuid for r in active)
    evidence = await client.memory_evidence(relationship_uuid=memory_uuid, scope=scope)
    assert evidence is not None  # raw evidence survives — never destroyed


@pytest.mark.asyncio
async def test_remediation_deferred_retire_after_hold_only_apply(tmp_path: Path) -> None:
    """Retire works even when requested in a LATER sweep than the one that first held."""
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    scope = agent_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    # First apply: hold only, no retirement.
    first = await client.remediate_coherence(dry_run=False)
    assert first.total_held == 1 and first.total_retired == 0
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is True

    # Later apply with retire: the already-held directive (no fresh incident) is still retired.
    second = await client.remediate_coherence(dry_run=False, retire_held_directives=True)
    assert second.total_retired == 1
    active = await client.search(query="cost efficiency", scope=scope)
    assert all(r.relationship_uuid != memory_uuid for r in active)


@pytest.mark.asyncio
async def test_remediation_never_mutates_read_only_scope(tmp_path: Path) -> None:
    """A read_only scope is detected and reported but never held or retired, even in apply mode."""
    from memotron.config import default_config

    scope = agent_scope()
    graph_path = tmp_path / "dw.sqlite"
    seeder = Memotron(graph_path=graph_path)
    memory_uuid = await escalate_feedback_directive(seeder, scope, cycles=4)

    config = default_config().model_copy(update={"read_only_scopes": {scope.key}})
    client = Memotron(graph_path=graph_path, config=config)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    report = await client.remediate_coherence(dry_run=False, retire_held_directives=True)

    assert report.total_windup == 1  # still detected and surfaced for review
    assert report.total_retired == 0  # but nothing mutated
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is None


@pytest.mark.asyncio
async def test_remediation_sweeps_all_scopes_and_skips_clean_ones(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "dw.sqlite")
    windup_scope = agent_scope()
    clean_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="clean-agent")
    await escalate_feedback_directive(client, windup_scope, cycles=4)
    # A different scope with an escalated directive but NO governing artifact.
    await escalate_feedback_directive(client, clean_scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(windup_scope)]))

    report = await client.remediate_coherence(dry_run=True)

    assert report.scope_count >= 2  # both scopes enumerated from the graph
    assert report.total_windup == 1  # only the scope with a stale governing skill
    affected = [s.scope.scope_id for s in report.scopes if s.report.incident_count > 0]
    assert affected == ["cost-eval-agent"]


@pytest.mark.asyncio
async def test_coherence_runs_as_a_configured_dream_job(tmp_path: Path) -> None:
    """The coherence cycle is a first-class dream job kind (a dream cycle beyond memory)."""
    from memotron import DreamJob, DreamJobKind
    from memotron.config import default_config

    scope = agent_scope()
    base = default_config()
    config = base.model_copy(
        update={
            "jobs": (
                *base.jobs,
                DreamJob(
                    name="coherence-cycle",
                    kind=DreamJobKind.COHERENCE,
                    cadence_seconds=60,
                    scope=scope,
                ),
            )
        }
    )
    client = Memotron(graph_path=tmp_path / "dw.sqlite", config=config)
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))

    result = await client.run_dream_job(job_name="coherence-cycle")
    assert result.job_runs[0].job_kind == DreamJobKind.COHERENCE
    assert result.job_runs[0].decision_count == 1

    incidents = await client.coherence_incidents(scope=scope)
    assert len(incidents) == 1
    assert incidents[0].kind == CoherenceIncidentKind.ESCALATION_WINDUP
