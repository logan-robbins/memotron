"""WS-16 T14: the human adjudication loop is closed — parked supersession
challengers, coherence incidents, and disambiguation requests are all listable
AND resolvable, with every transition receipted.

Pins the T14 contracts:

- ``pending_supersession_reviews`` lists every gate-parked challenger
  (``requires_operator_review`` truthy, no ``review_resolution``) with its gate
  reason and surviving incumbent, in deterministic (created_at, uuid) order.
- ``resolve_supersession_review(decision="approve")`` flips truth THROUGH the
  existing supersession machinery: incumbents close with lineage to the
  candidate, the candidate reactivates in place, and every mutation is
  hash-bracketed and receipted in one operator run.
- ``resolve_supersession_review(decision="reject")`` keeps current truth and
  stamps the challenger ``operator_rejected`` so it leaves the pending queue.
- ``resolve_coherence_incident`` records durable forward-only OPEN ->
  ACKNOWLEDGED -> RESOLVED transitions folded into ``coherence_incidents``;
  resolving an incident with a live anti-windup hold requires ``allow_held``
  because hold release stays outcome-driven (WS-10).
- ``resolve_disambiguation_request`` records the requested evidence;
  ``coherence_disambiguation_requests`` reconstructs it as resolved.
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
from memotron.models import RelationshipStatus
from memotron.receipts import ReceiptDecisionType

START = datetime(2026, 7, 1, tzinfo=UTC)


def review_scope() -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")


async def park_lower_authority_challenger(client: Memotron, scope: MemoryScope) -> tuple[str, str]:
    """Operator-authority incumbent, then a plain user observation parks."""
    incumbent = await client.add_memory(
        subject="deployment window",
        predicate="requires",
        object="change-board approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=START,
        metadata={"_verified_source_authority": "operator"},
    )
    challenger = await client.add_memory(
        subject="deployment window",
        predicate="requires",
        object="self-service approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.7,
        reference_time=START + timedelta(days=1),
    )
    return incumbent.relationship_uuid, challenger.relationship_uuid


async def park_corroboration_challenger(client: Memotron, scope: MemoryScope) -> tuple[str, str]:
    """Equal-authority challenger against an observed_count>=3 incumbent parks."""
    incumbent = None
    for index in range(3):
        incumbent = await client.add_memory(
            subject="production changes",
            predicate="require",
            object="two approvals",
            relationship_type="REQUIRES",
            scope=scope,
            confidence=0.8,
            reference_time=START + timedelta(days=index),
        )
    assert incumbent is not None
    challenger = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=START + timedelta(days=3),
    )
    return incumbent.relationship_uuid, challenger.relationship_uuid


def assert_all_receipt_chains_verify(client: Memotron, scope: MemoryScope) -> None:
    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    run_uuids = {receipt.run_uuid for receipt in receipts}
    assert run_uuids
    assert all(client.graph.receipts.verify_chain(run_uuid).valid for run_uuid in run_uuids)


# ---------------------------------------------------------------------------
# Pending review listing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_reviews_list_both_park_flavors_deterministically(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "pending.sqlite")
    scope = review_scope()

    authority_incumbent, authority_challenger = await park_lower_authority_challenger(client, scope)
    corroboration_incumbent, corroboration_challenger = await park_corroboration_challenger(client, scope)

    items = await client.pending_supersession_reviews(scope=scope)

    assert [item.relationship_uuid for item in items] == [
        authority_challenger,
        corroboration_challenger,
    ]
    by_uuid = {item.relationship_uuid: item for item in items}
    authority_item = by_uuid[authority_challenger]
    assert authority_item.gate_reason == "lower_authority:user<operator"
    assert authority_item.current_incumbent_uuid == authority_incumbent
    assert authority_item.source_authority == "user"
    assert authority_item.challenger_confidence == pytest.approx(0.7)
    assert "self-service approval" in authority_item.fact

    corroboration_item = by_uuid[corroboration_challenger]
    assert corroboration_item.gate_reason == (
        "insufficient_corroboration:incumbent_observed_count=3:corroborations=1/2"
    )
    assert corroboration_item.current_incumbent_uuid == corroboration_incumbent
    assert "one approval" in corroboration_item.fact

    # Deterministic order: (created_at, uuid) ascending.
    keys = [(item.created_at, item.relationship_uuid) for item in items]
    assert keys == sorted(keys)


@pytest.mark.asyncio
async def test_pending_reviews_validates_limit(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "limit.sqlite")
    with pytest.raises(ValueError, match="limit"):
        await client.pending_supersession_reviews(scope=review_scope(), limit=0)


# ---------------------------------------------------------------------------
# Approve: the parked candidate becomes current truth with full lineage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_flips_truth_with_lineage_and_receipts(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "approve.sqlite")
    scope = review_scope()
    incumbent_uuid, challenger_uuid = await park_corroboration_challenger(client, scope)
    resolved_at = START + timedelta(days=4)

    resolution = await client.resolve_supersession_review(
        relationship_uuid=challenger_uuid,
        scope=scope,
        decision="approve",
        reason="release policy changed to one approval",
        resolved_by="operator:logan",
        now=resolved_at,
    )

    assert resolution.decision == "approve"
    assert resolution.review_resolution == "operator_approved"
    assert resolution.status == RelationshipStatus.ACTIVE
    assert resolution.superseded_incumbent_uuids == [incumbent_uuid]

    incumbent = client.graph.get_relationship(incumbent_uuid)
    candidate = client.graph.get_relationship(challenger_uuid)
    assert incumbent.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert incumbent.properties["superseded_by_relationship_uuid"] == challenger_uuid
    assert incumbent.valid_to == resolved_at
    assert candidate.properties["status"] == RelationshipStatus.ACTIVE.value
    assert candidate.properties["requires_operator_review"] is False
    assert candidate.properties["review_resolution"] == "operator_approved"
    assert candidate.properties["review_resolved_by"] == "operator:logan"
    assert candidate.properties["review_reason"] == "release policy changed to one approval"
    assert candidate.properties.get("superseded_by_relationship_uuid") is None
    assert candidate.valid_from is not None  # original park-time valid_from preserved
    assert candidate.valid_to is None  # reopened

    # Current truth is ONLY the approved candidate.
    current = await client.search(query="approvals", scope=scope)
    assert [item.relationship_uuid for item in current] == [challenger_uuid]

    # The timeline shows the full lineage.
    timeline = await client.truth_timeline(scope=scope, subject="production changes", predicate="require")
    by_uuid = {entry.relationship_uuid: entry for entry in timeline}
    assert by_uuid[incumbent_uuid].status == RelationshipStatus.SUPERSEDED
    assert by_uuid[incumbent_uuid].superseded_by_relationship_uuid == challenger_uuid
    assert by_uuid[challenger_uuid].status == RelationshipStatus.ACTIVE
    assert by_uuid[challenger_uuid].is_current is True

    # Every mutation was receipted in the operator run and the chains verify.
    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    supersede_receipts = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED
        and receipt.decision_reason == "operator_review_approved"
    ]
    assert len(supersede_receipts) == 1
    assert supersede_receipts[0].superseded_relationship_uuid == incumbent_uuid
    assert supersede_receipts[0].successor_relationship_uuid == challenger_uuid
    resolved_receipts = [
        receipt for receipt in receipts if receipt.decision_type == ReceiptDecisionType.OPERATOR_REVIEW_RESOLVED
    ]
    assert len(resolved_receipts) == 1
    assert resolved_receipts[0].decision_reason == "operator_approved"
    assert resolved_receipts[0].relationship_uuid == challenger_uuid
    assert supersede_receipts[0].run_uuid == resolved_receipts[0].run_uuid
    assert_all_receipt_chains_verify(client, scope)

    # The queue is drained.
    assert await client.pending_supersession_reviews(scope=scope) == []


# ---------------------------------------------------------------------------
# Reject: current truth is kept and the challenger leaves the queue
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reject_keeps_incumbent_and_clears_pending(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "reject.sqlite")
    scope = review_scope()
    incumbent_uuid, challenger_uuid = await park_lower_authority_challenger(client, scope)
    incumbent_before = client.graph.get_relationship(incumbent_uuid)

    resolution = await client.resolve_supersession_review(
        relationship_uuid=challenger_uuid,
        scope=scope,
        decision="reject",
        reason="unverified self-service claim",
        resolved_by="operator:logan",
    )

    assert resolution.review_resolution == "operator_rejected"
    assert resolution.status == RelationshipStatus.SUPERSEDED
    assert resolution.superseded_incumbent_uuids == []

    candidate = client.graph.get_relationship(challenger_uuid)
    assert candidate.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert candidate.properties["requires_operator_review"] is False
    assert candidate.properties["review_resolution"] == "operator_rejected"
    assert candidate.properties["review_resolved_by"] == "operator:logan"

    # The incumbent is untouched by the resolution (its park-time dispute
    # discount is history, not re-applied).
    incumbent_after = client.graph.get_relationship(incumbent_uuid)
    assert incumbent_after.properties["status"] == RelationshipStatus.ACTIVE.value
    assert incumbent_after.properties["confidence"] == incumbent_before.properties["confidence"]
    assert incumbent_after.properties.get("superseded_by_relationship_uuid") is None

    current = await client.search(query="approval", scope=scope)
    assert [item.relationship_uuid for item in current] == [incumbent_uuid]
    assert await client.pending_supersession_reviews(scope=scope) == []
    assert_all_receipt_chains_verify(client, scope)


# ---------------------------------------------------------------------------
# Fail-fast validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_review_fail_fast(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "failfast.sqlite")
    scope = review_scope()
    incumbent_uuid, challenger_uuid = await park_lower_authority_challenger(client, scope)

    with pytest.raises(ValueError, match="reason cannot be blank"):
        await client.resolve_supersession_review(
            relationship_uuid=challenger_uuid,
            scope=scope,
            decision="reject",
            reason="   ",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="resolved_by cannot be blank"):
        await client.resolve_supersession_review(
            relationship_uuid=challenger_uuid,
            scope=scope,
            decision="reject",
            reason="unverified",
            resolved_by="",
        )
    with pytest.raises(ValueError, match="decision must be"):
        await client.resolve_supersession_review(
            relationship_uuid=challenger_uuid,
            scope=scope,
            decision="defer",  # type: ignore[arg-type]
            reason="unverified",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="does not exist"):
        await client.resolve_supersession_review(
            relationship_uuid="no-such-relationship",
            scope=scope,
            decision="reject",
            reason="unverified",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.resolve_supersession_review(
            relationship_uuid=challenger_uuid,
            scope=MemoryScope(kind=ScopeKind.USER, scope_id="someone-else"),
            decision="reject",
            reason="unverified",
            resolved_by="operator:logan",
        )
    # An active row that was never review-parked cannot be resolved.
    with pytest.raises(ValueError, match="not parked for operator review"):
        await client.resolve_supersession_review(
            relationship_uuid=incumbent_uuid,
            scope=scope,
            decision="reject",
            reason="unverified",
            resolved_by="operator:logan",
        )

    await client.resolve_supersession_review(
        relationship_uuid=challenger_uuid,
        scope=scope,
        decision="reject",
        reason="unverified self-service claim",
        resolved_by="operator:logan",
    )
    # Resolving twice fails fast.
    with pytest.raises(ValueError, match="already resolved"):
        await client.resolve_supersession_review(
            relationship_uuid=challenger_uuid,
            scope=scope,
            decision="approve",
            reason="second thoughts",
            resolved_by="operator:logan",
        )


# ---------------------------------------------------------------------------
# Coherence incident lifecycle
# ---------------------------------------------------------------------------

COST_TOPIC = "evaluate the cost efficiency of other agents thoroughly"


def coherence_scope() -> MemoryScope:
    return MemoryScope(kind=ScopeKind.AGENT, scope_id="cost-eval-agent")


async def escalate_feedback_directive(client: Memotron, scope: MemoryScope, *, cycles: int) -> str:
    relationship_uuid = ""
    for _ in range(cycles):
        result = await client.add_memory(
            subject="Agent",
            predicate="should",
            object=COST_TOPIC,
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.6,
        )
        relationship_uuid = result.relationship_uuid
    return relationship_uuid


def stale_self_authored_skill(scope: MemoryScope) -> PersistentArtifact:
    return PersistentArtifact(
        artifact_id="cost-efficiency-evaluator",
        artifact_class=ArtifactClass.SKILL,
        scope=scope,
        location=".claude/skills/cost-efficiency-evaluator/SKILL.md",
        author="self:agent-session-2026-05",
        self_authored=True,
        updated_at=datetime.now(UTC) - timedelta(days=30),
        directives=[
            ArtifactDirective(
                subject="evaluate cost efficiency of other agents",
                instruction=("A cost-efficiency check passes when total tokens are below the static budget."),
                stance=DirectiveStance.ASSERT,
            )
        ],
    )


@pytest.mark.asyncio
async def test_coherence_incident_lifecycle_transitions(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "incident.sqlite")
    scope = coherence_scope()
    memory_uuid = await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))
    report = await client.run_coherence_scan(scope=scope)
    incident = report.incidents[0]
    assert incident.kind == CoherenceIncidentKind.ESCALATION_WINDUP
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is True

    listed = await client.coherence_incidents(scope=scope)
    assert listed[0].status == CoherenceIncidentStatus.OPEN

    acknowledged = await client.resolve_coherence_incident(
        incident_id=incident.incident_id,
        scope=scope,
        status="acknowledged",
        statement="Triaged; skill repair is scheduled.",
        resolved_by="operator:logan",
    )
    assert acknowledged.previous_status == CoherenceIncidentStatus.OPEN
    assert acknowledged.status == CoherenceIncidentStatus.ACKNOWLEDGED
    listed = await client.coherence_incidents(scope=scope)
    assert listed[0].status == CoherenceIncidentStatus.ACKNOWLEDGED

    # The anti-windup hold is still in force: resolving requires the explicit
    # allow_held acknowledgement because hold release stays outcome-driven.
    with pytest.raises(ValueError, match="allow_held"):
        await client.resolve_coherence_incident(
            incident_id=incident.incident_id,
            scope=scope,
            status="resolved",
            statement="Skill repaired.",
            resolved_by="operator:logan",
        )

    resolved = await client.resolve_coherence_incident(
        incident_id=incident.incident_id,
        scope=scope,
        status="resolved",
        statement="Skill repaired; hold stays until a judged positive outcome.",
        resolved_by="operator:logan",
        allow_held=True,
    )
    assert resolved.previous_status == CoherenceIncidentStatus.ACKNOWLEDGED
    assert resolved.status == CoherenceIncidentStatus.RESOLVED
    listed = await client.coherence_incidents(scope=scope)
    assert listed[0].status == CoherenceIncidentStatus.RESOLVED
    # Resolving the incident record did NOT release the hold (outcome-driven).
    assert client.graph.get_relationship(memory_uuid).properties.get("coherence_hold") is True

    # Status filtering reflects folded transitions.
    assert await client.coherence_incidents(scope=scope, status=CoherenceIncidentStatus.OPEN) == []
    resolved_only = await client.coherence_incidents(scope=scope, status=CoherenceIncidentStatus.RESOLVED)
    assert [item.incident_id for item in resolved_only] == [incident.incident_id]

    # Backward or repeated transitions fail fast.
    with pytest.raises(ValueError, match="forward"):
        await client.resolve_coherence_incident(
            incident_id=incident.incident_id,
            scope=scope,
            status="acknowledged",
            statement="going backwards",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="forward"):
        await client.resolve_coherence_incident(
            incident_id=incident.incident_id,
            scope=scope,
            status="resolved",
            statement="resolving twice",
            resolved_by="operator:logan",
            allow_held=True,
        )
    assert_all_receipt_chains_verify(client, scope)


@pytest.mark.asyncio
async def test_coherence_incident_without_hold_resolves_directly(tmp_path: Path) -> None:
    """A contradiction incident writes no hold, so no allow_held escape is needed."""
    client = Memotron(graph_path=tmp_path / "contradiction.sqlite")
    scope = coherence_scope()
    pii_topic = "redact personally identifiable information before logging"
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
    incident = report.incidents[0]
    assert incident.kind == CoherenceIncidentKind.CROSS_ARTIFACT_CONTRADICTION

    resolved = await client.resolve_coherence_incident(
        incident_id=incident.incident_id,
        scope=scope,
        status="resolved",
        statement="File note corrected to match the redaction requirement.",
        resolved_by="operator:logan",
    )
    assert resolved.status == CoherenceIncidentStatus.RESOLVED
    listed = await client.coherence_incidents(scope=scope)
    assert listed[0].status == CoherenceIncidentStatus.RESOLVED


@pytest.mark.asyncio
async def test_resolve_coherence_incident_fail_fast(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "incident-failfast.sqlite")
    scope = coherence_scope()
    with pytest.raises(ValueError, match="statement cannot be blank"):
        await client.resolve_coherence_incident(
            incident_id="some-incident",
            scope=scope,
            status="acknowledged",
            statement=" ",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="resolved_by cannot be blank"):
        await client.resolve_coherence_incident(
            incident_id="some-incident",
            scope=scope,
            status="acknowledged",
            statement="triaged",
            resolved_by=" ",
        )
    with pytest.raises(ValueError, match="status must be"):
        await client.resolve_coherence_incident(
            incident_id="some-incident",
            scope=scope,
            status="closed",  # type: ignore[arg-type]
            statement="triaged",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="was not found"):
        await client.resolve_coherence_incident(
            incident_id="no-such-incident",
            scope=scope,
            status="acknowledged",
            statement="triaged",
            resolved_by="operator:logan",
        )


# ---------------------------------------------------------------------------
# Disambiguation requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disambiguation_request_resolves_with_evidence(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "disambiguation.sqlite")
    scope = coherence_scope()
    await escalate_feedback_directive(client, scope, cycles=4)
    client.register_artifact_source(StaticArtifactSource([stale_self_authored_skill(scope)]))
    await client.run_coherence_scan(scope=scope, policy=CoherencePolicy(min_attribution_confidence=1.0))

    requests = await client.coherence_disambiguation_requests(scope=scope)
    assert len(requests) == 1
    request = requests[0]
    assert request.status == "pending"

    resolved = await client.resolve_disambiguation_request(
        request_id=request.incident_id,
        scope=scope,
        runtime_trace="run-42: SKILL.md step 3 applied the static token budget",
        artifact_version="cost-efficiency-evaluator@sha256:abc123",
        statement="The stale skill governs; repair it before further escalation.",
        resolved_by="operator:logan",
    )
    assert resolved.status == "resolved"
    assert resolved.resolved_by == "operator:logan"

    # Reconstruction surfaces the resolved status plus the recorded evidence.
    requests = await client.coherence_disambiguation_requests(scope=scope)
    assert len(requests) == 1
    reconstructed = requests[0]
    assert reconstructed.incident_id == request.incident_id
    assert reconstructed.status == "resolved"
    assert reconstructed.resolution_runtime_trace == ("run-42: SKILL.md step 3 applied the static token budget")
    assert reconstructed.resolution_artifact_version == ("cost-efficiency-evaluator@sha256:abc123")
    assert reconstructed.resolution_statement == ("The stale skill governs; repair it before further escalation.")
    assert reconstructed.resolved_by == "operator:logan"
    assert reconstructed.resolved_at is not None

    # The resolution is durably receipted and the chains verify.
    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    assert any(receipt.decision_type == ReceiptDecisionType.COHERENCE_DISAMBIGUATION_RESOLVED for receipt in receipts)
    assert_all_receipt_chains_verify(client, scope)

    # Resolving twice fails fast.
    with pytest.raises(ValueError, match="already resolved"):
        await client.resolve_disambiguation_request(
            request_id=request.incident_id,
            scope=scope,
            runtime_trace="run-43",
            artifact_version="v2",
            statement="again",
            resolved_by="operator:logan",
        )


@pytest.mark.asyncio
async def test_resolve_disambiguation_request_fail_fast(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "disambiguation-failfast.sqlite")
    scope = coherence_scope()
    with pytest.raises(ValueError, match="runtime_trace cannot be blank"):
        await client.resolve_disambiguation_request(
            request_id="some-request",
            scope=scope,
            runtime_trace="  ",
            artifact_version="v1",
            statement="statement",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="artifact_version cannot be blank"):
        await client.resolve_disambiguation_request(
            request_id="some-request",
            scope=scope,
            runtime_trace="trace",
            artifact_version="",
            statement="statement",
            resolved_by="operator:logan",
        )
    with pytest.raises(ValueError, match="was not found"):
        await client.resolve_disambiguation_request(
            request_id="no-such-request",
            scope=scope,
            runtime_trace="trace",
            artifact_version="v1",
            statement="statement",
            resolved_by="operator:logan",
        )
