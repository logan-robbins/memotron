"""The agent-memory entry point must wrap its server in the identity middleware too.

#206 Phase 1. The sibling file `test_mcp_entry_point_installs_the_guard.py` makes these
claims for `examples/mcp_server.py`, the server the chart runs today. This one makes them
for `examples/agent_memory_mcp_server.py`, which #206 Phase 4 proposes to deploy — the
point being to have the guarantees in place *before* it is exposed, not after.

Two failure shapes are pinned here, and the second is specific to this file:

1. Revert `_serve()` to `mcp.run(transport="streamable-http")` and the wrapper silently
   disappears — `mcp.run()` builds its own ASGI app internally, so there is nothing to
   wrap and nothing else fails.

2. Reassign `mcp.settings.host` after construction. **This file did exactly that until
   #206 Phase 1**, and worse than the module it serves: `agent_memory_mcp` reads
   `MCP_HOST` with a `127.0.0.1` default *at construction*, while this entry point re-read
   the same variable with a `0.0.0.0` default and assigned it afterwards. FastMCP resolves
   transport security — including the `Host` allowlist — at construction, so the bind
   address moves and the allowlist does not. The result is 421 for every request through
   an ingress while `/health` returns 200: healthy-looking and unusable. It never fired
   because this server has never been deployed, which is precisely what Phase 4 changes.

Loading by path rather than importing, because `examples/` is not a package.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import sys
import types

import pytest
from starlette.testclient import TestClient

from memotron.agent_memory_mcp import MCP_HOST, MCP_PORT
from memotron.mcp_auth import REQUIRE_IDENTITY_ENV, GatewayIdentityMiddleware
from memotron.runtime import mcp_bind_from_env

DESCRIPTION = "the agent-memory entry point"


ENTRY = pathlib.Path(__file__).resolve().parents[1] / "examples" / "agent_memory_mcp_server.py"


def _entry_module():
    spec = importlib.util.spec_from_file_location("dw_agent_memory_entry_under_test", ENTRY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _serve_with_fake_uvicorn(module) -> dict:
    """Run `_serve()` with uvicorn stubbed, and return what it would have served."""
    served: dict = {}

    class _Config:
        def __init__(self, app, **kwargs):
            served["app"] = app
            served.update(kwargs)

    class _Server:
        def __init__(self, config):
            served["config"] = config

        def run(self):
            served["ran"] = True

    fake = types.ModuleType("uvicorn")
    fake.Config = _Config
    fake.Server = _Server
    original = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake
    try:
        module._serve()
    finally:
        if original is None:
            del sys.modules["uvicorn"]
        else:
            sys.modules["uvicorn"] = original
    return served


def _middleware_classes(app: object) -> list[type]:
    """Every middleware class in `app`, following Mounts one level down.

    `build_mcp_http_app` returns an OUTER Starlette that serves `/health` unguarded and
    mounts the guarded MCP app at `/` -- so the identity middleware lives on the inner
    app, not the outer one. Reading `app.user_middleware` alone finds nothing and reports
    a correctly-guarded server as unguarded. This function is what that assertion needs;
    the behavioural 401 below is what makes it more than a shape check.
    """
    from starlette.routing import Mount

    # Unwrap plain ASGI wrappers first. `build_mcp_http_app` returns `StrictHostGuard`,
    # which holds the Starlette app on `.app` and exposes no `user_middleware` of its
    # own -- so reading the outermost object alone finds nothing and reports a correctly
    # guarded server as unguarded.
    seen: list[type] = []
    node = app
    for _ in range(4):  # bounded: guard -> Starlette -> Mount -> guarded app
        seen.append(type(node))
        classes = [mw.cls for mw in getattr(node, "user_middleware", [])]
        if classes:
            break
        inner = getattr(node, "app", None)
        if inner is None or inner is node:
            break
        node = inner

    for route in getattr(node, "routes", []):
        if isinstance(route, Mount):
            classes.extend(mw.cls for mw in getattr(route.app, "user_middleware", []))
    return classes


def test_the_entry_point_serves_through_the_identity_middleware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted against the app object `_serve` builds, not the source text."""
    served = _serve_with_fake_uvicorn(_entry_module())

    assert served.get("ran") is True

    # The guard is in the served app's middleware stack. Under FastMCP 4 the app is a
    # Starlette instance whose stack is built lazily on the first request, so the served
    # object is NOT the middleware instance any more -- `isinstance(app, ...)`, which is
    # what this test used to assert, now reads False on perfectly correct code.
    app = served["app"]
    installed = _middleware_classes(app)
    assert GatewayIdentityMiddleware in installed, (
        f"{DESCRIPTION} is serving the raw MCP app -- the identity guard is not "
        f"installed, and every cross-scope tool is reachable unauthenticated. "
        f"middleware={[c.__name__ for c in installed]}"
    )

    # And it is not merely present: armed, with no credential, it refuses. A structural
    # assertion alone would pass on a middleware wired in backwards or short-circuited.
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")
    with TestClient(app, base_url="http://localhost", follow_redirects=True) as client:
        response = client.post(
            "/mcp",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
    assert response.status_code == 401, (
        f"{DESCRIPTION} answered {response.status_code} to an unauthenticated call with "
        f"{REQUIRE_IDENTITY_ENV} set; the guard is installed but not refusing."
    )


def test_it_serves_on_the_hostport_the_SETTINGS_carry() -> None:
    """It must hand uvicorn what FastMCP already resolved, not re-derive it."""

    served = _serve_with_fake_uvicorn(_entry_module())

    expected_host, expected_port = mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT)
    assert served["host"] == expected_host
    assert served["port"] == expected_port


def test_the_entry_point_does_not_call_mcp_run() -> None:
    """`mcp.run()` builds and serves its own app, so reverting to it drops the wrapper
    and nothing else fails."""
    tree = ast.parse(ENTRY.read_text())
    runs = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "mcp"
    ]
    assert not runs, f"`mcp.run(...)` is back at line(s) {runs}; the identity wrapper is bypassed"


def test_the_entry_point_does_not_REASSIGN_host_or_port_after_construction() -> None:
    """The 421 defect this file carried until #206 Phase 1.

    Transport security — the `Host` header allowlist — is resolved when the `FastMCP`
    object is constructed, at import of `agent_memory_mcp`. Assigning `mcp.settings.host`
    afterwards moves the bind address without moving the allowlist: `0.0.0.0` bound while
    only localhost `Host` headers are accepted, so every request through an ingress is
    421 and `/health` is 200.

    An AST assertion rather than a behavioural one, deliberately: the damage happens at
    import time in a real process, and by the time `_serve()` runs the settings already
    disagree. There is nothing to observe later — only the assignment to forbid.
    """
    tree = ast.parse(ENTRY.read_text())
    offenders = [
        f"{node.lineno}:mcp.settings.{t.attr}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Attribute)
        and t.attr in {"host", "port"}
        and isinstance(t.value, ast.Attribute)
        and t.value.attr == "settings"
    ]
    assert not offenders, (
        f"host/port reassigned after construction at {offenders} — this is the 421 defect. "
        "Set MCP_HOST/MCP_PORT in the environment; `agent_memory_mcp` reads them when it "
        "builds the FastMCP object, which is the only moment transport security is resolved."
    )
