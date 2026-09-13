from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memotron import (
    ArtifactClass,
    ArtifactDirective,
    DirectiveStance,
    Memotron,
    MemoryScope,
    OutcomeVerdict,
    PersistentArtifact,
    ScopeKind,
)
from memotron.receipts import ReceiptDecisionType


def _artifact(scope: MemoryScope) -> PersistentArtifact:
    return PersistentArtifact(
        artifact_id="order-escalation-skill",
        artifact_class=ArtifactClass.SKILL,
        scope=scope,
        author="operator@example.test",
        location="skills/order-escalation/SKILL.md",
        updated_at=datetime.now(UTC),
        directives=[
            ArtifactDirective(
                subject="order escalation",
                instruction="Validate order ID and SLA before escalating.",
                stance=DirectiveStance.REQUIRE,
            )
        ],
    )


@pytest.mark.asyncio
async def test_live_artifact_outcomes_are_idempotent_and_quarantine_low_contributors(tmp_path) -> None:
    graph_path = tmp_path / "graph.sqlite"
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="live-artifacts")
    client = Memotron(graph_path=graph_path)
    client.register_live_artifact(_artifact(scope))
    assert [item.artifact_id for item in client.graph.live_artifacts(scope=scope)] == ["order-escalation-skill"]

    for index in range(3):
        projection = await client.record_live_artifact_outcome(
            scope=scope,
            artifact_id="order-escalation-skill",
            verdict=OutcomeVerdict.NEGATIVE,
            task_run_id=f"run-{index}",
            idempotency_key=f"negative-{index}",
        )

    assert projection.negative_outcomes == 3
    assert projection.minimum_evidence_met is True
    assert projection.contribution_score == -1.0
    assert projection.quarantined is True
    assert client.graph.live_artifacts(scope=scope) == []
    assert client.graph.live_artifacts(scope=scope, include_quarantined=True)[0].artifact_id == (
        "order-escalation-skill"
    )

    duplicate = await client.record_live_artifact_outcome(
        scope=scope,
        artifact_id="order-escalation-skill",
        verdict=OutcomeVerdict.NEGATIVE,
        task_run_id="run-2",
        idempotency_key="negative-2",
    )
    assert duplicate.negative_outcomes == 3

    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    assert (
        len(
            [
                receipt
                for receipt in receipts
                if receipt.decision_type == ReceiptDecisionType.COHERENCE_ARTIFACT_OUTCOME_RECORDED
            ]
        )
        == 3
    )
    assert (
        len(
            [
                receipt
                for receipt in receipts
                if receipt.decision_type == ReceiptDecisionType.COHERENCE_ARTIFACT_QUARANTINED
            ]
        )
        == 1
    )

    restarted = Memotron(graph_path=graph_path)
    assert restarted.graph.live_artifacts(scope=scope) == []
