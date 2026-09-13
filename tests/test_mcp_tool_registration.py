"""The advertised tool contract must match the tools that are actually registered.

`AGENT_MEMORY_REQUIRED_TOOLS` is served to clients as `required_tools` in the
integration contract (`agent_memory.py:863`). Nothing checked it against the live
FastMCP registry, and the two had drifted: `memory_restore` was in the contract, had
a full implementation and a passing test, and was **registered nowhere** -- its
`@mcp.tool()` decorator was missing, so it was unreachable over the protocol.

Why every existing check missed it
----------------------------------
* `tests/test_agent_memory.py:692` calls `agent_memory_mcp.memory_restore(...)` as a
  plain module attribute. Decorators only matter to FastMCP's registration and
  dispatch, so that passes whether or not the tool exists over MCP.
* ruff and mypy see a perfectly good async function.
* The contract payload is built by listing the constant, never by asking the server.

So the defect was invisible from inside the process and visible only by counting:
28 tool-shaped functions, 27 registered. `scripts/verify/sanity.sh` surfaced it that
way, at the protocol layer, on both a pre-refactor and a post-refactor wheel.

These tests ask the SERVER, not the source, which is the only question that matches
what a client experiences.
"""

from __future__ import annotations

import pytest

from memotron import agent_memory_mcp
from memotron.agent_memory import AGENT_MEMORY_REQUIRED_TOOLS


async def _registered() -> set[str]:
    return {tool.name for tool in await agent_memory_mcp.mcp.list_tools()}


@pytest.mark.parametrize("name", AGENT_MEMORY_REQUIRED_TOOLS)
async def test_every_advertised_tool_is_registered(name: str) -> None:
    """Parametrised so a failure names the missing tool rather than a set difference."""
    assert name in await _registered(), (
        f"{name} is in AGENT_MEMORY_REQUIRED_TOOLS -- and so advertised to clients as "
        "part of the integration contract -- but is not registered with the MCP "
        "server. The usual cause is a missing @mcp.tool() decorator, which nothing "
        "else in the suite can see because the function is still importable and "
        "callable directly."
    )


async def test_no_tool_shaped_function_is_left_unregistered() -> None:
    """The other direction: an implemented tool that nobody can reach.

    `memory_restore` failed exactly this way for as long as it existed. A function
    that takes the tool signature, returns a JSON string and is never registered is
    dead code that looks alive -- and the contract test above only catches it if
    someone also remembered to add it to the required list.
    """
    import inspect

    registered = await _registered()
    orphans = [
        name
        for name, fn in vars(agent_memory_mcp).items()
        if name.startswith(("memory_", "agent_", "project_"))
        and inspect.iscoroutinefunction(fn)
        and name not in registered
    ]
    assert not orphans, (
        f"implemented but unreachable over MCP: {', '.join(sorted(orphans))}. "
        "Either register it with @mcp.tool() or delete it -- a tool-shaped function "
        "that no client can call is worse than an absent one, because the contract "
        "and the tests both read as though it works."
    )
