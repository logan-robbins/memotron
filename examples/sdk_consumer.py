"""Golden-path SDK consumer for a hosted Memotron platform."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from memotron import MemotronPlatformClient


async def run_consumer(
    *,
    base_url: str,
    agent_id: str,
    agent_name: str,
) -> dict[str, Any]:
    memory = MemotronPlatformClient(
        base_url=base_url,
        agent_id=agent_id,
        agent_name=agent_name,
    )
    bootstrap = await memory.memory_bootstrap(task_run_id=f"{agent_id}-sdk-example")
    contract = await memory.integration_contract()
    status = await memory.status()
    registration = bootstrap.registration
    start = bootstrap.start
    task_run_id = start.task_run_id
    project_policy = await memory.project_memory_config()
    if not project_policy["configured"]:
        await memory.project_memory_configure(
            project_goal="Deliver the hosted Memotron integration safely.",
            memory_goal="Keep shared integration decisions, requirements, incidents, and blockers.",
            keep=(
                "Decisions and requirements that affect more than one project agent.",
                "Incidents and blockers that change delivery or operating behavior.",
            ),
            exclude=("Personal preferences and scratch work.",),
            configured_by="sdk-consumer-example",
        )
    remembered = await memory.memory_remember(
        subject=f"{agent_id} consumer",
        predicate="should",
        object="call memory_start before relying on prior context",
        relationship_type="SHOULD",
        source_text="SDK consumer golden path",
    )
    published = await memory.memory_publish(
        content=(
            "Memory: subject=Memotron integration; predicate=decided; "
            "object=Use the hosted platform API for shared agent memory; "
            "relationship_type=DECIDES; confidence=0.9"
        ),
        task_run_id=task_run_id,
        source_reference="examples/sdk_consumer.py",
    )
    searched = await memory.memory_search(
        query="memory_start prior context",
        task_run_id=task_run_id,
    )
    if not searched.use_events:
        raise RuntimeError("SDK consumer expected memory_search to return a use event")
    used_scope = searched.use_events[0].scope.kind.value
    outcome_scope = {
        "user": "personal",
        "agent": "agent",
        "tenant": "project",
    }[used_scope]
    outcome = await memory.memory_outcome(
        use_id=searched.use_events[0].use_id,
        verdict="positive",
        task_run_id=searched.use_events[0].task_run_id,
        idempotency_key=f"{task_run_id}:verified-search",
        judge_identity="sdk-consumer-example",
        judge_version="v1",
        scope=outcome_scope,
    )
    utility = await memory.memory_utility(
        relationship_uuid=searched.use_events[0].relationship_uuid,
    )
    checkpoint = await memory.memory_log(
        summary="SDK consumer checkpointed state before context compaction.",
        decisions=("Use the hosted Memotron endpoint for SDK integration tests.",),
        checkpoint_reason="context_compaction",
        task_run_id=task_run_id,
    )
    rehydrated = await memory.memory_start(task_run_id=f"{agent_id}-sdk-example-post-compaction")
    refreshed = await memory.memory_refresh(include_project=True)
    return {
        "contract": contract,
        "bootstrap_next_actions": list(bootstrap.next_actions),
        "registration": registration.model_dump(mode="json"),
        "status": status,
        "agent_id": agent_id,
        "start_tokens": start.tokens_used,
        "start_use_events": [item.model_dump(mode="json") for item in start.use_events],
        "remembered": remembered.model_dump(mode="json"),
        "published": published.model_dump(mode="json"),
        "search_results": [result.model_dump(mode="json") for result in searched.results],
        "outcome": outcome.model_dump(mode="json"),
        "utility": [item.model_dump(mode="json") for item in utility],
        "checkpoint": checkpoint.model_dump(mode="json"),
        "rehydrated_task_run_id": rehydrated.task_run_id,
        "refreshed": refreshed.model_dump(mode="json"),
    }


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Run the Memotron SDK consumer example.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8765/")
    parser.add_argument("--agent-id", default="consumer-app")
    parser.add_argument("--agent-name", default="Memotron SDK Consumer")
    args = parser.parse_args()

    result = await run_consumer(
        base_url=args.base_url,
        agent_id=args.agent_id,
        agent_name=args.agent_name,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
