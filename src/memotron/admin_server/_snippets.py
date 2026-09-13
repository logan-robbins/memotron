"""The onboarding code the admin console hands a developer to copy.

Three generators producing SDK and MCP snippets. They are code that writes code, so a
change here ships silently into someone else editor -- MCP_NOT_CONFIGURED_SNIPPET
travels with them because it is the fallback a developer sees when the server is not
wired, and it should not drift from the snippets it substitutes for."""

from __future__ import annotations

import json

from memotron.agent_memory import (
    RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME,
)


def sdk_lifecycle_snippet(
    *,
    platform_api_url: str,
    agent_id: str,
    agent_name: str,
) -> str:
    return "\n".join(
        [
            "from memotron import MemotronPlatformClient",
            "",
            "memory = MemotronPlatformClient(",
            f"    base_url={platform_api_url!r},",
            f"    agent_id={agent_id!r},",
            f"    agent_name={agent_name!r},",
            ")",
            "",
            "# One call registers/reconnects and loads prompt-ready memory.",
            "bootstrap = await memory.memory_bootstrap(",
            '    task_run_id="replace-with-current-task-run-id"',
            ")",
            "start = bootstrap.start",
            "task_run_id = start.task_run_id",
            "prior = await memory.memory_search(",
            '    query="prior decisions or requirements",',
            "    task_run_id=task_run_id,",
            ")",
            "# Retain start.use_events and prior.use_events. After a real evaluator observes a result:",
            "# outcome = await memory.memory_outcome(",
            '#     use_id=prior.use_events[0].use_id, verdict="positive",',
            "#     task_run_id=prior.use_events[0].task_run_id,",
            '#     idempotency_key=f"{task_run_id}:observed-result",',
            '#     judge_identity="actual-evaluator", judge_version="v1", scope="agent",',
            "# )",
            "project_policy = await memory.project_memory_config()",
            "remembered = await memory.memory_remember(",
            f"    subject={agent_id!r},",
            '    predicate="should",',
            '    object="use Memotron memory before repeating prior work",',
            '    relationship_type="SHOULD",',
            '    source_text="integration lifecycle fact",',
            ")",
            "# Publish shared evidence only after an operator configures project memory.",
            'if project_policy["configured"]:',
            "    published = await memory.memory_publish(",
            '        content="A durable project decision with its source evidence.",',
            "        task_run_id=task_run_id,",
            '        source_reference="replace-with-source-reference",',
            "    )",
            "logged = await memory.memory_log(",
            '    summary="Checkpoint before harness context compaction.",',
            '    decisions=("Use stable agent IDs for Memotron memory.",),',
            '    checkpoint_reason="context_compaction",',
            "    task_run_id=task_run_id,",
            ")",
            "# After compaction, keep the same identity and start a new task run.",
            'rehydrated = await memory.memory_start(task_run_id="replace-with-new-task-run-id")',
            "refreshed = await memory.memory_refresh()",
            "# Motive resolves automatically unless you intentionally override dream policy.",
        ]
    )


MCP_NOT_CONFIGURED_SNIPPET = (
    "# No MCP endpoint is configured for this admin server.\n"
    "# Start the agent-memory MCP server, then relaunch with "
    "--mcp-url http://<host>:<port>/mcp."
)


def mcp_lifecycle_snippet(
    *,
    mcp_url: str,
    agent_id: str,
    agent_name: str,
) -> str:
    if not mcp_url:
        return MCP_NOT_CONFIGURED_SNIPPET
    # The body below is written at the indentation the old two-level nesting needed
    # (`async with streamablehttp_client(...)` wrapping `async with ClientSession(...)`).
    # `fastmcp.Client` is one context manager -- it owns the transport AND performs the
    # initialize handshake -- so exactly one level comes back off, mechanically, below.
    # Dedenting here rather than reflowing 60 string literals keeps this diff reviewable
    # and keeps the emitted snippet correct Python, which is the only thing that matters:
    # a console visitor copies this straight into an editor.
    body = [
        "        bootstrap_result = await session.call_tool(",
        '            "memory_bootstrap",',
        f"            {json.dumps({'agent_id': agent_id, 'agent_name': agent_name, 'task_run_id': 'replace-with-current-task-run-id'})},",
        "        )",
        "        bootstrap = json.loads(bootstrap_result.content[0].text)",
        '        task_run_id = bootstrap["start"]["task_run_id"]',
        '        contract = await session.call_tool("memory_contract", {})',
        "        prior = await session.call_tool(",
        '            "memory_search",',
        "            {",
        f'                "agent_id": {agent_id!r},',
        '                "query": "prior decisions or requirements",',
        '                "task_run_id": task_run_id,',
        "            },",
        "        )",
        "        # Retain returned use_events; report only evaluator-observed results with memory_outcome.",
        '        project_policy_result = await session.call_tool("project_memory_config", {})',
        "        project_policy = json.loads(project_policy_result.content[0].text)",
        "        remembered = await session.call_tool(",
        '            "memory_remember",',
        "            {",
        f'                "agent_id": {agent_id!r},',
        f'                "subject": {agent_id!r},',
        '                "predicate": "should",',
        '                "object": "use Memotron memory before repeating prior work",',
        '                "relationship_type": "SHOULD",',
        '                "source_text": "integration lifecycle fact",',
        "            },",
        "        )",
        '        if project_policy["configured"]:',
        "            published = await session.call_tool(",
        '                "memory_publish",',
        "                {",
        f'                    "agent_id": {agent_id!r},',
        '                    "content": "A durable project decision with its source evidence.",',
        '                    "task_run_id": task_run_id,',
        '                    "source_reference": "replace-with-source-reference",',
        "                },",
        "            )",
        "        logged = await session.call_tool(",
        '            "memory_log",',
        "            {",
        f'                "agent_id": {agent_id!r},',
        '                "summary": "Checkpoint before harness context compaction.",',
        '                "decisions_json": "[\\"Use stable agent IDs for Memotron memory.\\"]",',
        '                "checkpoint_reason": "context_compaction",',
        '                "task_run_id": task_run_id,',
        "            },",
        "        )",
        "        # After compaction, keep the identity and call memory_start with a new task_run_id.",
        "        refreshed = await session.call_tool(",
        '            "memory_refresh",',
        f"            {json.dumps({'agent_id': agent_id})},",
        "        )",
        "        # Motive resolves automatically unless dream policy is overridden.",
    ]
    return "\n".join(
        [
            "import json",
            "from fastmcp import Client",
            "",
            f"async with Client({mcp_url!r}) as session:",
            *(line.removeprefix("    ") for line in body),
        ]
    )


def codex_mcp_config_snippet(*, mcp_url: str) -> str:
    if not mcp_url:
        return MCP_NOT_CONFIGURED_SNIPPET
    return "\n".join(
        [
            "# Codex MCP config for Memotron agent memory.",
            f"[mcp_servers.{RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME}]",
            f"url = {mcp_url!r}",
            "required = true",
            "tool_timeout_sec = 120",
            "# Recommended after trusting this tenant: approve Memotron memory tools automatically.",
        ]
    )
