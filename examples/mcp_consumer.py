"""Golden-path MCP consumer for the local Memotron agent-memory server."""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any, Protocol

from fastmcp import Client


class ToolSession(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


def tool_result_json(result: Any) -> dict[str, Any]:
    content = getattr(result, "content", None)
    if not content:
        raise ValueError("MCP tool result did not contain content")
    text = getattr(content[0], "text", "")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("MCP tool result content was not text")
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("MCP tool result JSON must be an object")
    return parsed


async def call_tool_json(session: ToolSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return tool_result_json(await session.call_tool(name, arguments))


async def run_consumer_with_session(
    session: ToolSession,
    *,
    agent_id: str,
    agent_name: str,
) -> dict[str, Any]:
    bootstrap = await call_tool_json(
        session,
        "memory_bootstrap",
        {
            "agent_id": agent_id,
            "agent_name": agent_name,
            "task_run_id": f"{agent_id}-mcp-example",
        },
    )
    contract = await call_tool_json(session, "memory_contract", {})
    registration = bootstrap["registration"]
    start = bootstrap["start"]
    task_run_id = start["task_run_id"]
    project_policy = await call_tool_json(
        session,
        "project_memory_config",
        {},
    )
    if not project_policy["configured"]:
        await call_tool_json(
            session,
            "project_memory_configure",
            {
                "project_goal": "Deliver the hosted Memotron integration safely.",
                "memory_goal": ("Keep shared integration decisions, requirements, incidents, and blockers."),
                "keep_json": json.dumps(
                    [
                        "Decisions and requirements that affect multiple project agents.",
                        "Incidents and blockers that change delivery.",
                    ]
                ),
                "exclude_json": json.dumps(["Personal preferences and scratch work."]),
                "configured_by": "mcp-consumer-example",
            },
        )
    remembered = await call_tool_json(
        session,
        "memory_remember",
        {
            "agent_id": agent_id,
            "subject": f"{agent_id} mcp consumer",
            "predicate": "should",
            "object": "call memory_search before repeating prior work",
            "relationship_type": "SHOULD",
            "source_text": "MCP consumer golden path",
        },
    )
    published = await call_tool_json(
        session,
        "memory_publish",
        {
            "agent_id": agent_id,
            "content": (
                "Memory: subject=Memotron integration; predicate=decided; "
                "object=Use MCP for process-isolated shared memory; "
                "relationship_type=DECIDES; confidence=0.9"
            ),
            "task_run_id": task_run_id,
            "source_reference": "examples/mcp_consumer.py",
        },
    )
    searched = await call_tool_json(
        session,
        "memory_search",
        {
            "agent_id": agent_id,
            "query": "memory_search prior work",
            "task_run_id": task_run_id,
        },
    )
    if not searched["use_events"]:
        raise RuntimeError("MCP consumer expected memory_search to return a use event")
    use_event = searched["use_events"][0]
    outcome_scope = {
        "user": "personal",
        "agent": "agent",
        "tenant": "project",
    }[use_event["scope"]["kind"]]
    outcome = await call_tool_json(
        session,
        "memory_outcome",
        {
            "agent_id": agent_id,
            "use_id": use_event["use_id"],
            "verdict": "positive",
            "task_run_id": use_event["task_run_id"],
            "idempotency_key": f"{task_run_id}:verified-search",
            "judge_identity": "mcp-consumer-example",
            "judge_version": "v1",
            "scope": outcome_scope,
        },
    )
    checkpoint = await call_tool_json(
        session,
        "memory_log",
        {
            "agent_id": agent_id,
            "summary": "MCP consumer checkpointed state before context compaction.",
            "decisions_json": json.dumps(["Use MCP tools for process-isolated consumers."]),
            "checkpoint_reason": "context_compaction",
            "task_run_id": task_run_id,
        },
    )
    rehydrated = await call_tool_json(
        session,
        "memory_start",
        {
            "agent_id": agent_id,
            "task_run_id": f"{agent_id}-mcp-example-post-compaction",
        },
    )
    refreshed = await call_tool_json(
        session,
        "memory_refresh",
        {"agent_id": agent_id, "include_project": True},
    )
    return {
        "contract": contract,
        "bootstrap_next_actions": bootstrap["next_actions"],
        "registration": registration,
        "agent_id": agent_id,
        "start": start,
        "remembered": remembered,
        "published": published,
        "searched": searched,
        "outcome": outcome,
        "checkpoint": checkpoint,
        "rehydrated": rehydrated,
        "refreshed": refreshed,
    }


async def run_consumer(*, mcp_url: str, agent_id: str, agent_name: str) -> dict[str, Any]:
    # `fastmcp.Client` rather than the SDK's `streamablehttp_client` + `ClientSession`
    # pair: it infers the transport from the URL, performs the initialize handshake on
    # `__aenter__`, and follows the 307 that `/mcp` -> `/mcp/` issues. That redirect is
    # not a detail -- a raw client that does not follow it sees an empty response and
    # reports the server down. `Client` satisfies `ToolSession` unchanged.
    async with Client(mcp_url) as session:
        return await run_consumer_with_session(
            session,
            agent_id=agent_id,
            agent_name=agent_name,
        )


async def async_main() -> None:
    parser = argparse.ArgumentParser(description="Run the Memotron MCP consumer example.")
    parser.add_argument("--mcp-url", default="http://127.0.0.1:8010/mcp")
    parser.add_argument("--agent-id", default="consumer-app")
    parser.add_argument("--agent-name", default="Memotron MCP Consumer")
    args = parser.parse_args()
    result = await run_consumer(
        mcp_url=args.mcp_url,
        agent_id=args.agent_id,
        agent_name=args.agent_name,
    )
    print(json.dumps(result, indent=2, sort_keys=True))


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
