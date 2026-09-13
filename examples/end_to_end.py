from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from memotron import (
    DreamAgentConfig,
    DreamContextPolicy,
    DreamEpisodeFilter,
    DreamJob,
    DreamJobKind,
    DreamPromptOverride,
    Memotron,
    EpisodeType,
    ExtractionRequest,
    MemoryScope,
    RuleBasedExtractionTransport,
    ScopeKind,
)
from memotron.config import default_config


class ExampleDreamTransport:
    def __init__(self) -> None:
        self._base_transport = RuleBasedExtractionTransport()

    async def extract_memories(self, request: ExtractionRequest) -> list[dict[str, object]]:
        if request.episode.metadata.get("dream_job_kind") != DreamJobKind.CONSOLIDATION.value:
            return await self._base_transport.extract_memories(request)

        facts = {memory.fact for memory in request.graph_context}
        if {
            "Support Agent should ask for order id before escalation",
            "Support Agent should confirm SLA tier before escalation",
        }.issubset(facts):
            return [
                {
                    "subject": "Support Agent",
                    "predicate": "should",
                    "object": "confirm order id and SLA tier before escalation",
                    "relationship_type": "SHOULD",
                    "confidence": 0.89,
                    "metadata": {"derived_from": "consolidation"},
                }
            ]
        return []


async def main() -> None:
    temp_dir = TemporaryDirectory()
    graph_path = Path(temp_dir.name) / "memotron.sqlite"
    base_time = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)

    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme-parks")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    dream_agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-dream-agent")
    dream_agent = DreamAgentConfig(
        agent_id="support-dream-agent",
        name="Support Dream Agent",
        scope=dream_agent_scope,
        decision_policy=(
            "Approve auditable support-memory formation, consolidation, and pruning actions "
            "when they preserve scoped evidence and temporality."
        ),
    )
    support_metadata = {"pipeline": "support_memory"}
    base_config = default_config()
    config = base_config.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="formation-default",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    prompt_profile="support-memory",
                    prompt_profile_version="v2",
                    agent=dream_agent,
                    prompt_override=DreamPromptOverride(
                        include=(
                            "Support-specific customer requirements and user preferences that will change future replies.",
                        ),
                        exclude=("Transient routing details unless a valid_to window is explicit.",),
                        rules=(
                            "Use node properties for customer tier, region, and systems when the episode states them.",
                        ),
                    ),
                    episode_filter=DreamEpisodeFilter(metadata_filter={"pipeline": "support_memory"}),
                ),
                DreamJob(
                    name="consolidation-agent",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    scope=agent_scope,
                    prompt_profile="agent-lessons",
                    prompt_profile_version="v1",
                    agent=dream_agent,
                    context_policy=DreamContextPolicy(
                        max_relationships=5,
                        relationship_types=("SHOULD",),
                        metadata_filter={"source": ("agent_reflection", "shadow_workspace")},
                        include_metadata=True,
                    ),
                ),
                DreamJob(
                    name="pruning-default",
                    kind=DreamJobKind.PRUNING,
                    cadence_seconds=1,
                    agent=dream_agent,
                ),
            ),
        }
    )
    client = Memotron(
        graph_path=graph_path,
        config=config,
        extraction_transport=ExampleDreamTransport(),
    )

    await client.add_episode(
        name="customer-vendor-review",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "SOC2 report before vendor approval",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.93,
                        "valid_from": "2026-06-01T12:00:00Z",
                        "subject_properties": {
                            "kind": "customer",
                            "tier": "enterprise",
                            "region": "NA",
                        },
                        "object_properties": {
                            "kind": "compliance document",
                            "system": "audit portal",
                        },
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        source_description="support chat",
        scope=customer_scope,
        reference_time=base_time,
        metadata=support_metadata,
    )
    await client.add_episode(
        name="customer-vendor-review-correction",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "SOC2 and ISO27001 reports before vendor approval",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.97,
                        "valid_from": "2026-06-03T09:30:00Z",
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        source_description="support chat follow-up",
        scope=customer_scope,
        reference_time=base_time + timedelta(days=2),
        metadata=support_metadata,
    )
    await client.add_episode(
        name="customer-temporary-support-channel",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "prefers",
                        "object": "temporary phone support",
                        "relationship_type": "PREFERS",
                        "confidence": 0.86,
                        "valid_from": "2026-06-01T13:00:00Z",
                        "valid_to": "2026-06-01T18:00:00Z",
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        source_description="temporary support exception",
        scope=customer_scope,
        reference_time=base_time + timedelta(hours=1),
        metadata=support_metadata,
    )
    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=concise technical answers; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct user chat",
        scope=user_scope,
        reference_time=base_time,
        metadata=support_metadata,
    )
    await client.add_episode(
        name="user-preference-evidence",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=answers with supporting evidence; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct user chat",
        scope=user_scope,
        reference_time=base_time + timedelta(hours=1),
        metadata=support_metadata,
    )
    await client.add_episode(
        name="user-preference-evidence-repeat",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=answers with supporting evidence; "
            "relationship_type=PREFERS; confidence=0.93"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct user chat follow-up",
        scope=user_scope,
        reference_time=base_time + timedelta(hours=2),
        metadata=support_metadata,
    )
    await client.add_episode(
        name="agent-lesson",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; object=ask for order id before escalation; "
            "relationship_type=SHOULD; confidence=0.82"
        ),
        source=EpisodeType.TEXT,
        source_description="post-resolution reflection",
        scope=agent_scope,
        reference_time=base_time,
        metadata={**support_metadata, "source": "agent_reflection"},
    )
    shadow_context_result = await client.add_context(
        name="support-agent-shadow-setting",
        content=(
            "Shadow setting captured from the support workspace.\n"
            "Memory: subject=Support Agent; predicate=should; object=confirm SLA tier before escalation; "
            "relationship_type=SHOULD; confidence=0.84"
        ),
        scopes=[agent_scope],
        source_description="shadow workspace setting",
        custom_id="support-agent-setting-v1",
        metadata={**support_metadata, "source": "shadow_workspace"},
        reference_time=base_time + timedelta(hours=3),
        max_chars_per_episode=240,
    )

    dream_status_before = await client.dream_status(now=base_time + timedelta(days=3))
    dream_result = await client.run_due_dreams(now=base_time + timedelta(days=3))
    dream_status_after = await client.dream_status(now=base_time + timedelta(days=3))
    customer_correction_candidate = await client.search(query="ISO27001 reports", scope=customer_scope)
    corrected_customer_memory = await client.correct_memory(
        relationship_uuid=customer_correction_candidate[0].relationship_uuid,
        corrected_object="SOC2, ISO27001, and vendor risk reports before vendor approval",
        scope=customer_scope,
        confidence=0.99,
        reason="operator_added_vendor_risk_report",
        source_text="Vendor governance confirmed the risk report is also required.",
        metadata={"reviewed_by": "vendor_governance"},
        now=base_time + timedelta(days=3, minutes=2),
    )
    retired_user_candidate = await client.search(query="concise technical", scope=user_scope)
    forgot_user_memory = await client.forget_memory(
        relationship_uuid=retired_user_candidate[0].relationship_uuid,
        scope=user_scope,
        reason="operator_retired_stale_preference",
        now=base_time + timedelta(days=3, minutes=5),
    )
    customer_results = await client.search(query="vendor approval reports", scope=customer_scope)
    customer_evidence = await client.memory_evidence(
        relationship_uuid=customer_results[0].relationship_uuid,
        scope=customer_scope,
    )
    corrected_customer_memory_evidence = await client.memory_evidence(
        relationship_uuid=corrected_customer_memory.corrected_relationship_uuid,
        scope=customer_scope,
    )
    forgot_user_memory_evidence = await client.memory_evidence(
        relationship_uuid=forgot_user_memory.relationship_uuid,
        scope=user_scope,
    )
    customer_truth_timeline = await client.truth_timeline(
        scope=customer_scope,
        subject="Acme Parks",
        predicate="requires",
        relationship_type="REQUIRES",
    )
    customer_neighborhood = await client.entity_neighborhood(
        scope=customer_scope,
        entity="Acme Parks",
    )
    historical_customer_results = await client.search(
        query="SOC2 report",
        scope=customer_scope,
        as_of=base_time + timedelta(hours=1),
    )
    expired_customer_results = await client.search(
        query="temporary phone support",
        scope=customer_scope,
    )
    historical_expired_customer_results = await client.search(
        query="temporary phone support",
        scope=customer_scope,
        as_of=base_time + timedelta(hours=2),
    )
    retired_user_results = await client.search(
        query="concise technical",
        scope=user_scope,
    )
    historical_retired_user_results = await client.search(
        query="concise technical",
        scope=user_scope,
        as_of=base_time + timedelta(hours=1),
    )
    user_results = await client.search(query="supporting evidence", scope=user_scope)
    agent_results = await client.search(query="before escalation", scope=agent_scope)
    support_context_results = await client.search_context(
        query="approval escalation answers",
        scopes=[customer_scope, user_scope, agent_scope],
    )
    user_profile = await client.profile(scope=user_scope)
    customer_profile = await client.profile(scope=customer_scope)
    dream_history = await client.dream_history(limit=10)
    dream_decisions = await client.dream_decisions(limit=20, agent_id="support-dream-agent")
    graph = client.export_graph()
    customer_graph_node = next(node for node in graph["nodes"] if node["properties"].get("name") == "Acme Parks")

    print(
        json.dumps(
            {
                "formation_episode_filter": client.config.jobs[0].episode_filter.model_dump(mode="json"),
                "formation_context_policy": client.config.jobs[0].context_policy.model_dump(mode="json"),
                "formation_prompt_profile": {
                    "name": client.config.jobs[0].prompt_profile,
                    "version": client.config.jobs[0].prompt_profile_version,
                    "override": client.config.jobs[0].prompt_override.model_dump(mode="json"),
                },
                "formation_dream_agent": client.config.jobs[0].agent.model_dump(mode="json"),
                "consolidation_context_policy": client.config.jobs[1].context_policy.model_dump(mode="json"),
                "consolidation_prompt_profile": {
                    "name": client.config.jobs[1].prompt_profile,
                    "version": client.config.jobs[1].prompt_profile_version,
                    "override": client.config.jobs[1].prompt_override.model_dump(mode="json"),
                },
                "consolidation_dream_agent": client.config.jobs[1].agent.model_dump(mode="json"),
                "shadow_context_result": shadow_context_result.model_dump(mode="json"),
                "dream_status_before": [status.model_dump(mode="json") for status in dream_status_before],
                "dream_result": dream_result.model_dump(mode="json"),
                "dream_status_after": [status.model_dump(mode="json") for status in dream_status_after],
                "corrected_customer_memory": corrected_customer_memory.model_dump(mode="json"),
                "corrected_customer_memory_evidence": corrected_customer_memory_evidence.model_dump(mode="json"),
                "customer_results": [result.model_dump(mode="json") for result in customer_results],
                "customer_evidence": customer_evidence.model_dump(mode="json"),
                "forgot_user_memory": forgot_user_memory.model_dump(mode="json"),
                "forgot_user_memory_evidence": forgot_user_memory_evidence.model_dump(mode="json"),
                "customer_truth_timeline": [entry.model_dump(mode="json") for entry in customer_truth_timeline],
                "customer_neighborhood": [edge.model_dump(mode="json") for edge in customer_neighborhood],
                "historical_customer_results": [
                    result.model_dump(mode="json") for result in historical_customer_results
                ],
                "expired_customer_results": [result.model_dump(mode="json") for result in expired_customer_results],
                "historical_expired_customer_results": [
                    result.model_dump(mode="json") for result in historical_expired_customer_results
                ],
                "retired_user_results": [result.model_dump(mode="json") for result in retired_user_results],
                "historical_retired_user_results": [
                    result.model_dump(mode="json") for result in historical_retired_user_results
                ],
                "user_results": [result.model_dump(mode="json") for result in user_results],
                "agent_results": [result.model_dump(mode="json") for result in agent_results],
                "support_context_results": [result.model_dump(mode="json") for result in support_context_results],
                "user_profile": user_profile.model_dump(mode="json"),
                "customer_profile": customer_profile.model_dump(mode="json"),
                "dream_history": [record.model_dump(mode="json") for record in dream_history],
                "dream_decisions": [record.model_dump(mode="json") for record in dream_decisions],
                "customer_graph_node": customer_graph_node,
                "graph_counts": {
                    "nodes": len(graph["nodes"]),
                    "relationships": len(graph["relationships"]),
                },
                "graph_path": str(graph_path),
            },
            indent=2,
        )
    )
    temp_dir.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
