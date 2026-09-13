from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    ENGINEERING_AGENT_MEMORY_MOTIVE,
    GENERAL_AGENT_MEMORY_MOTIVE,
    PRODUCT_AGENT_MEMORY_MOTIVE,
    PROJECT_AGENT_MEMORY_MOTIVE,
    AgentMemoryPlatform,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryScope,
    MemoryType,
    OutcomeVerdict,
    ScopeKind,
    agent_memory_bank,
    agent_memory_config,
    builtin_memory_bank,
)
from memotron.config import default_config
from memotron.local_platform import run_maintenance_cycle
from memotron.models import RelationshipStatus


@pytest.mark.asyncio
async def test_negated_single_active_fact_is_not_semantically_reinforced(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "negated.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    first_time = datetime(2026, 7, 1, tzinfo=UTC)
    second_time = first_time + timedelta(days=1)

    first = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="two approvals",
        relationship_type="REQUIRES",
        scope=scope,
        reference_time=first_time,
    )
    second = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="do not require two approvals",
        relationship_type="REQUIRES",
        scope=scope,
        reference_time=second_time,
    )

    original = client.graph.get_relationship(first.relationship_uuid)
    replacement = client.graph.get_relationship(second.relationship_uuid)
    assert second.relationship_uuid != first.relationship_uuid
    assert second.created_relationships == 1
    assert second.reinforced_relationships == 0
    assert second.superseded_relationships == 1
    assert original.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert replacement.properties["status"] == RelationshipStatus.ACTIVE.value
    assert original.properties["semantic_polarity"] == "positive"
    assert replacement.properties["semantic_polarity"] == "negative"


@pytest.mark.asyncio
async def test_lower_authority_candidate_cannot_replace_current_truth(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "authority.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    first_time = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="two operator approvals",
        relationship_type="REQUIRES",
        scope=scope,
        reference_time=first_time,
        metadata={"_verified_source_authority": "operator"},
    )
    candidate = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="no approval",
        relationship_type="REQUIRES",
        scope=scope,
        reference_time=first_time + timedelta(days=1),
        metadata={"_verified_source_authority": "agent"},
    )

    active = client.graph.get_relationship(incumbent.relationship_uuid)
    rejected = client.graph.get_relationship(candidate.relationship_uuid)
    current = await client.search(query="operator approvals", scope=scope)
    assert active.properties["status"] == RelationshipStatus.ACTIVE.value
    assert rejected.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert rejected.properties["requires_operator_review"] is True
    assert rejected.properties["supersession_gate_reason"] == "lower_authority:agent<operator"
    assert [item.relationship_uuid for item in current] == [incumbent.relationship_uuid]


@pytest.mark.asyncio
async def test_expired_temporary_state_restores_previous_current_truth(tmp_path) -> None:
    config = agent_memory_config()
    client = Memotron(
        config=config,
        graph_path=tmp_path / "temporary-restoration.sqlite",
    )
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="worker")
    baseline_time = datetime(2026, 7, 1, tzinfo=UTC)
    temporary_time = baseline_time + timedelta(days=1)
    restore_time = temporary_time + timedelta(days=2)

    baseline = await client.add_memory(
        subject="worker",
        predicate="has state",
        object="ready for normal work",
        relationship_type="HAS_STATE",
        scope=scope,
        reference_time=baseline_time,
    )
    temporary = await client.add_memory(
        subject="worker",
        predicate="has state",
        object="temporarily paused for migration",
        relationship_type="HAS_STATE",
        scope=scope,
        reference_time=temporary_time,
        valid_to=restore_time,
    )

    result = await client.run_due_dreams(now=restore_time + timedelta(seconds=1))
    restored_matches = await client.search(query="ready for normal work", scope=scope)
    original = client.graph.get_relationship(baseline.relationship_uuid)
    expired = client.graph.get_relationship(temporary.relationship_uuid)
    restored = client.graph.get_relationship(restored_matches[0].relationship_uuid)
    pruning_runs = [run for run in result.job_runs if run.job_kind == DreamJobKind.PRUNING]

    assert original.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert expired.properties["status"] == RelationshipStatus.PRUNED.value
    assert restored.uuid not in {original.uuid, expired.uuid}
    assert restored.properties["status"] == RelationshipStatus.ACTIVE.value
    assert restored.properties["restored_from_relationship_uuid"] == original.uuid
    assert restored.valid_from == restore_time
    assert pruning_runs[0].created_relationships == 1
    assert pruning_runs[0].pruned_relationships >= 1
    run_uuids = {receipt.run_uuid for receipt in client.graph.receipts.receipts_for_scope(scope.key)}
    assert all(client.graph.receipts.verify_chain(run_uuid).valid for run_uuid in run_uuids)


@pytest.mark.asyncio
async def test_same_episode_can_be_processed_by_multiple_motives(tmp_path) -> None:
    base = default_config()
    config = base.model_copy(
        update={
            "memory_bank": builtin_memory_bank(),
            "jobs": (
                DreamJob(
                    name="preferences-formation",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    motive="capture-preferences",
                ),
                DreamJob(
                    name="requirements-formation",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    motive="learn-compliance-requirements",
                ),
            ),
        }
    )
    client = Memotron(config=config, graph_path=tmp_path / "multi-motive.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="multi-motive")
    await client.add_episode(
        name="mixed evidence",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "user",
                        "predicate": "prefers",
                        "object": "concise status updates",
                        "relationship_type": "PREFERS",
                        "confidence": 0.9,
                    },
                    {
                        "subject": "deployments",
                        "predicate": "require",
                        "object": "change tickets",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.95,
                    },
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=datetime(2026, 7, 1, tzinfo=UTC),
    )

    first = await client.run_due_dreams(now=datetime(2026, 7, 1, 0, 0, 2, tzinfo=UTC))
    second = await client.run_due_dreams(now=datetime(2026, 7, 1, 0, 0, 4, tzinfo=UTC))
    relationships = [relationship for relationship in client.graph.relationships() if relationship.type != "MENTIONS"]

    assert [run.processed_episodes for run in first.job_runs] == [1, 1]
    assert [run.processed_episodes for run in second.job_runs] == [0, 0]
    assert {relationship.properties["memory_type"] for relationship in relationships} == {
        MemoryType.PREFERENCE.value,
        MemoryType.REQUIREMENT.value,
    }


@pytest.mark.asyncio
async def test_agent_role_motives_persist_and_runtime_records_utility(tmp_path) -> None:
    graph_path = tmp_path / "agent-motives.sqlite"
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="memotron",
        agent_ids=(
            "runtime",
            "roadmap-product-manager",
            "delivery-project-manager",
            "operator",
        ),
    )
    expected = {
        "runtime": ENGINEERING_AGENT_MEMORY_MOTIVE,
        "roadmap-product-manager": PRODUCT_AGENT_MEMORY_MOTIVE,
        "delivery-project-manager": PROJECT_AGENT_MEMORY_MOTIVE,
        "operator": GENERAL_AGENT_MEMORY_MOTIVE,
    }
    assert {item["agent_id"]: item["motive_name"] for item in platform.agent_motives()} == expected

    observer = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="memotron",
    )
    assignment = platform.configure_agent_motive(
        agent_id="runtime",
        motive_name=GENERAL_AGENT_MEMORY_MOTIVE,
    )
    assert observer.agent_motive(agent_id="runtime")["motive_name"] == (GENERAL_AGENT_MEMORY_MOTIVE)
    observer.client.graph.close()
    remembered = await platform.memory_remember(
        agent_id="runtime",
        subject="runtime",
        predicate="should",
        object="record memory utility automatically",
        relationship_type="SHOULD",
    )
    search = await platform.memory_search(
        agent_id="runtime",
        query="memory utility",
        include_project=False,
        task_run_id="task-utility",
    )
    outcome = await platform.memory_outcome(
        agent_id="runtime",
        use_id=search.use_events[0].use_id,
        verdict=OutcomeVerdict.POSITIVE,
        task_run_id=search.task_run_id,
        idempotency_key="task-utility:positive",
        judge_identity="test-runtime",
        judge_version="v1",
    )
    utility = await platform.memory_utility(
        agent_id="runtime",
        relationship_uuid=remembered.relationship_uuid,
    )
    platform.client.graph.close()

    reloaded = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="memotron",
    )
    try:
        assert assignment["motive_name"] == GENERAL_AGENT_MEMORY_MOTIVE
        assert reloaded.agent_motive(agent_id="runtime")["motive_name"] == (GENERAL_AGENT_MEMORY_MOTIVE)
        assert search.task_run_id == "task-utility"
        assert search.use_events[0].relationship_uuid == remembered.relationship_uuid
        assert outcome.verdict == OutcomeVerdict.POSITIVE
        assert utility[0].positive_outcome_count == 1
        assert utility[0].impression_count == 1
    finally:
        reloaded.client.graph.close()


def test_agent_memory_is_bounded_and_can_consolidate_rollups() -> None:
    config = agent_memory_config()
    motive = agent_memory_bank().motive(ENGINEERING_AGENT_MEMORY_MOTIVE)

    assert MemoryType.ROLLUP in motive.allowed_memory_types
    assert config.pruning.growth.soft_cap == 300
    assert config.pruning.growth.per_type_soft_caps[MemoryType.STATE.value] == 80
    assert any(job.kind == DreamJobKind.CONSOLIDATION and job.rollup_consolidation for job in config.jobs)


@pytest.mark.asyncio
async def test_platform_maintenance_processes_queued_agent_evidence(tmp_path) -> None:
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "maintenance.sqlite",
        project_id="memotron",
        agent_ids=("runtime",),
    )
    await platform.memory_log(
        agent_id="runtime",
        decisions=("Run memory maintenance without a caller refresh.",),
    )

    job_run_count = await run_maintenance_cycle(platform)
    search = await platform.memory_search(
        agent_id="runtime",
        query="caller refresh",
        include_project=False,
    )

    assert job_run_count >= 1
    assert any(item.object == "Run memory maintenance without a caller refresh." for item in search.results)
