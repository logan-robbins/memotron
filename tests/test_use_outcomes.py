from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    Memotron,
    MemoryScope,
    OutcomeVerdict,
    ScopeKind,
    UseEventKind,
)
from memotron.receipts import ReceiptDecisionType


@pytest.mark.asyncio
async def test_use_outcome_events_are_receipted_idempotent_and_do_not_change_truth(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "graph.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="utility-user")
    created = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="answers with supporting evidence",
        relationship_type="PREFERS",
        confidence=0.91,
        scope=scope,
    )
    relationship = client.graph.get_relationship(created.relationship_uuid)
    original_confidence = relationship.properties["confidence"]
    original_observed_count = relationship.properties["observed_count"]
    when = datetime(2026, 7, 15, tzinfo=UTC)

    retrieved = await client.record_memory_use(
        relationship_uuid=created.relationship_uuid,
        scope=scope,
        kind=UseEventKind.RETRIEVED,
        task_run_id="task-1",
        idempotency_key="task-1-retrieved",
        used_at=when,
        rank=0,
        retrieval_score=0.88,
        candidate_set_size=12,
        context_budget_competition=4,
        retrieval_policy_digest="a" * 64,
    )
    retry = await client.record_memory_use(
        relationship_uuid=created.relationship_uuid,
        scope=scope,
        kind=UseEventKind.RETRIEVED,
        task_run_id="task-1",
        idempotency_key="task-1-retrieved",
        used_at=when,
        rank=0,
        retrieval_score=0.88,
        candidate_set_size=12,
        context_budget_competition=4,
        retrieval_policy_digest="a" * 64,
    )
    assert retry.use_id == retrieved.use_id

    utility = await client.memory_utility(scope=scope, relationship_uuid=created.relationship_uuid, as_of=when)
    assert utility[0].impression_count == 1
    assert utility[0].injected_count == 0
    assert utility[0].use_stability == 0.0
    assert await client.retrieval_negative_space(scope=scope, task_run_id="task-1")

    await client.record_memory_use(
        relationship_uuid=created.relationship_uuid,
        scope=scope,
        kind=UseEventKind.INJECTED,
        task_run_id="task-1",
        idempotency_key="task-1-injected",
        used_at=when,
        rank=0,
        retrieval_score=0.88,
        candidate_set_size=12,
        context_budget_competition=4,
        retrieval_policy_digest="a" * 64,
    )
    cited = await client.record_memory_use(
        relationship_uuid=created.relationship_uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="task-1",
        idempotency_key="task-1-cited",
        used_at=when,
    )
    outcome = await client.record_memory_outcome(
        use_id=cited.use_id,
        scope=scope,
        verdict=OutcomeVerdict.POSITIVE,
        task_run_id="task-1",
        idempotency_key="task-1-positive",
        judge_identity="operator",
        judge_version="v1",
        judged_at=when + timedelta(seconds=1),
    )
    assert outcome.use_id == cited.use_id
    with pytest.raises(
        ValueError,
        match="task_run_id must match the referenced use event",
    ):
        await client.record_memory_outcome(
            use_id=cited.use_id,
            scope=scope,
            verdict=OutcomeVerdict.NEGATIVE,
            task_run_id="another-task",
            idempotency_key="another-task:negative",
            judge_identity="operator",
            judge_version="v1",
            judged_at=when + timedelta(seconds=2),
        )
    assert await client.retrieval_negative_space(scope=scope, task_run_id="task-1") == []

    utility = await client.memory_utility(
        scope=scope, relationship_uuid=created.relationship_uuid, as_of=when + timedelta(seconds=2)
    )
    assert utility[0].impression_count == 1
    assert utility[0].injected_count == 1
    assert utility[0].cited_or_used_count == 1
    assert utility[0].positive_outcome_count == 1
    assert utility[0].use_stability > 0.0
    assert utility[0].outcome_quality > 0.5

    unchanged = client.graph.get_relationship(created.relationship_uuid)
    assert unchanged.properties["confidence"] == original_confidence
    assert unchanged.properties["observed_count"] == original_observed_count
    receipts = await client.memory_receipts(scope=scope)
    assert sum(r.decision_type == ReceiptDecisionType.USE_EVENT_RECORDED for r in receipts) == 3
    assert sum(r.decision_type == ReceiptDecisionType.OUTCOME_EVENT_RECORDED for r in receipts) == 1
    for receipt in receipts:
        if receipt.decision_type in {
            ReceiptDecisionType.USE_EVENT_RECORDED,
            ReceiptDecisionType.OUTCOME_EVENT_RECORDED,
        }:
            assert receipt.event_payload_digest
            assert receipt.event_payload

    replayed = await client.replay_memory_utility(
        scope=scope,
        relationship_uuid=created.relationship_uuid,
        as_of=when + timedelta(seconds=2),
    )
    assert [item.model_dump(mode="json") for item in replayed] == [item.model_dump(mode="json") for item in utility]


@pytest.mark.asyncio
async def test_successful_spaced_use_strengthens_utility_without_popularity_reward(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "graph.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="spaced-use-user")
    created = await client.add_memory(
        subject="Credential reference",
        predicate="is stored in",
        object="vault://team/service-account",
        relationship_type="PREFERS",
        confidence=0.9,
        scope=scope,
    )
    relationship = client.graph.get_relationship(created.relationship_uuid)
    original_truth = (
        relationship.properties["confidence"],
        relationship.properties["observed_count"],
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stabilities: list[float] = []
    for month in range(3):
        used_at = start + timedelta(days=30 * month)
        # Many injections are diagnostics only; none may create utility reward.
        for exposure in range(4):
            await client.record_memory_use(
                relationship_uuid=created.relationship_uuid,
                scope=scope,
                kind=UseEventKind.INJECTED,
                task_run_id=f"month-{month}",
                idempotency_key=f"month-{month}-injected-{exposure}",
                used_at=used_at,
                rank=exposure,
                retrieval_score=0.8,
                candidate_set_size=4,
                context_budget_competition=2,
                retrieval_policy_digest="b" * 64,
            )
        used = await client.record_memory_use(
            relationship_uuid=created.relationship_uuid,
            scope=scope,
            kind=UseEventKind.CITED_OR_USED,
            task_run_id=f"month-{month}",
            idempotency_key=f"month-{month}-used",
            used_at=used_at,
        )
        await client.record_memory_outcome(
            use_id=used.use_id,
            scope=scope,
            verdict=OutcomeVerdict.POSITIVE,
            task_run_id=f"month-{month}",
            idempotency_key=f"month-{month}-positive",
            judge_identity="runtime-evaluator",
            judge_version="v1",
            judged_at=used_at,
        )
        projection = await client.memory_utility(
            scope=scope, relationship_uuid=created.relationship_uuid, as_of=used_at
        )
        stabilities.append(projection[0].use_stability)

    assert stabilities == sorted(stabilities)
    projection = (await client.memory_utility(scope=scope, relationship_uuid=created.relationship_uuid))[0]
    assert projection.impression_count == 0
    assert projection.injected_count == 12
    unchanged = client.graph.get_relationship(created.relationship_uuid)
    assert (unchanged.properties["confidence"], unchanged.properties["observed_count"]) == original_truth
