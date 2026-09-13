"""The deployed entry point must wrap the server in the identity middleware.

#126. Every other test in this area proves the guard WORKS. None proved it is INSTALLED,
and those are different claims with different failure modes.

`examples/mcp_server.py` is the file the chart actually runs (`.helm/values.yaml` invokes
it, not `memotron.mcp_server`, which has no `main()`), it lives outside the package, and
it is outside `coverage_floors.py`. So the regression shape is: revert `_serve()` to
`mcp.run(transport="streamable-http")`, every other test still passes, and the pod serves
the six `_CROSS_SCOPE_TOOLS` -- one of which writes a tenant's LLM credential and one of
which deletes it -- to anyone who can reach the ingress, while the other 33 fail closed.

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

from memotron.mcp_auth import REQUIRE_IDENTITY_ENV, GatewayIdentityMiddleware
from memotron.mcp_server import MCP_HOST, MCP_PORT
from memotron.runtime import mcp_bind_from_env

DESCRIPTION = "the deployed entry point"

ENTRY = pathlib.Path(__file__).resolve().parents[1] / "examples" / "mcp_server.py"


def _entry_module():
    spec = importlib.util.spec_from_file_location("dw_entry_under_test", ENTRY)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
    """Asserted against the app object `_serve` builds, not against the source text.

    Uvicorn is stubbed so nothing binds a port; what is captured is the ASGI app that would
    have been served.
    """
    module = _entry_module()
    served: dict[str, object] = {}

    class _Config:
        def __init__(self, app, **kwargs):
            served["app"] = app
            served["kwargs"] = kwargs

    class _Server:
        def __init__(self, config):
            served["config"] = config

        def run(self):
            served["ran"] = True

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = _Config
    fake_uvicorn.Server = _Server
    original = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake_uvicorn
    try:
        module._serve()
    finally:
        if original is None:
            del sys.modules["uvicorn"]
        else:
            sys.modules["uvicorn"] = original

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


def test_it_serves_on_the_hostport_the_settings_carry() -> None:
    """The 421 lesson, pinned: ONE source for the bind address.

    Under the old SDK the allowlist was resolved at construction from `host` and the entry
    point assigned `mcp.settings.host` afterwards, so the two disagreed -- 0.0.0.0 bound
    while only localhost `Host` headers were accepted, 421 through the ingress and 200 on
    /health. Under FastMCP 4 there is no construction-time allowlist to go stale, but the
    same class of bug returns the moment the bind address is read in a second place with a
    second default. That is not hypothetical: `agent_memory_mcp` defaulted MCP_HOST to
    127.0.0.1 while its own entry point defaulted the SAME variable to 0.0.0.0.

    So this asserts uvicorn is handed exactly what `mcp_bind_from_env` returns -- not a
    re-read of the environment, and not a literal."""
    module = _entry_module()

    served: dict[str, object] = {}

    class _Config:
        def __init__(self, app, **kwargs):
            served.update(kwargs)

    class _Server:
        def __init__(self, config):
            pass

        def run(self):
            pass

    fake_uvicorn = types.ModuleType("uvicorn")
    fake_uvicorn.Config = _Config
    fake_uvicorn.Server = _Server
    original = sys.modules.get("uvicorn")
    sys.modules["uvicorn"] = fake_uvicorn
    try:
        module._serve()
    finally:
        if original is None:
            del sys.modules["uvicorn"]
        else:
            sys.modules["uvicorn"] = original

    expected_host, expected_port = mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT)
    assert served["host"] == expected_host
    assert served["port"] == expected_port


def test_the_entry_point_does_not_call_mcp_run() -> None:
    """The specific regression: `mcp.run()` builds and serves its own app internally, so
    reverting to it silently drops the wrapper. Nothing else would fail."""
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
    assert not runs, f"mcp.run() at line(s) {runs} bypasses the identity middleware entirely"
