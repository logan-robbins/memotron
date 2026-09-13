"""The hosted platform API answers, on the surface `main()` actually builds.

C1 / #194. Every `/api/platform/*` route returned
``503 {"error":"Memotron platform API is not enabled for this server"}`` because
NOTHING in `admin_server.main()` ever assigned `handler.platform`. `local_platform.py:216`
was the only assignment in the tree, so the API the README documents worked in local dev
and nowhere else. `_platform()` raises 503 when it is None, which is the whole prefix.

**Two gates had to learn about the platform's scopes, not one**, and the first version of
this fix set only one of them:

* `configured_scopes` feeds `_scope_payload`, so the console can address them
* `build_demo_control_plane(..., extra_scopes=)` feeds the control plane, and
  `_request_policy` consults THAT -- this is the one that produced the measured 400 on
  `memory/search` and `memory/remember`, `scope 'user:...' is not registered for tenant`

So `test_the_two_most_used_routes_do_not_400` is the load-bearing test here. A version of
this file that only checked "no longer 503" would pass with the control plane still
disagreeing, because 400 is not 503.

The handler is built by replicating `main()`'s wiring rather than by starting a server:
the defect lived in how `main()` wires things, so a harness that wires it differently
would prove something about the harness. That is the same reason
`test_admin_scope_isolation.py` does it.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib

import pytest

from memotron import MemoryScope, PrincipalRole, ScopeKind
from memotron.admin_server import (
    HttpApiError,
    MemoryGraphHandler,
    agent_scope,
    build_demo_control_plane,
    build_demo_principal,
    build_hosted_platform,
)
from memotron.identity import REQUIRE_IDENTITY_ENV

TENANT = "tenant-under-test"
AGENT = "probe-agent"
SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id=TENANT)
ADMIN_SERVER = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "admin_server" / "__init__.py"


@pytest.fixture(autouse=True)
def _identity_armed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here needs the platform to EXIST, and it only exists where callers can
    be attributed. `test_the_platform_is_OFF_where_identity_is_disarmed` opts out."""
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")


def _handler_as_main_builds_it(tmp_path: pathlib.Path):
    """`main()`'s wiring, including the two lines this change added."""
    # The REAL decision function, not a copy of it. `main()` binds a socket so nothing
    # can execute it, which is exactly why this logic is extracted -- reproducing it here
    # would prove something about the harness.
    platform, platform_scopes = build_hosted_platform(
        graph_path=tmp_path / "platform.sqlite",
        store=None,
        tenant_id=TENANT,
        agent_id=AGENT,
    )
    assert platform is not None, "the fixture needs identity armed; see `_identity_armed`"

    handler = type("PlatformEnabledProbe", (MemoryGraphHandler,), {})
    handler.client = platform.client
    handler.runtime_client = platform.client
    handler.platform = platform
    handler.default_scope = SCOPE
    handler.tenant_id = TENANT
    handler.agent_ids = (AGENT,)
    handler.configured_scopes = platform_scopes
    handler.static_dir = tmp_path / "missing-dist"
    handler.control_plane = build_demo_control_plane(
        client=platform.client,
        default_scope=SCOPE,
        tenant_id=TENANT,
        agent_id=AGENT,
        extra_scopes=(agent_scope(AGENT), *platform_scopes),
    )
    platform.client.control_plane = handler.control_plane
    handler.principal = build_demo_principal(
        principal_id="admin",
        tenant_id=TENANT,
        agent_id=AGENT,
        default_scope=SCOPE,
        role=PrincipalRole.ADMIN,
    )
    instance = object.__new__(handler)
    instance.path = "/api/platform/status"
    return instance, platform, platform_scopes


def test_the_platform_is_no_longer_None_so_the_prefix_stops_503ing(tmp_path: pathlib.Path) -> None:
    """`_platform()` is the accessor all 18 routes share; 503 came from it being None."""
    handler, platform, _ = _handler_as_main_builds_it(tmp_path)
    assert handler._platform() is platform


def test_status_and_integration_contract_return_real_payloads(tmp_path: pathlib.Path) -> None:
    """The two GET routes that do NOT delegate to `AgentMemoryPlatform`, so they would
    inherit nothing from a platform-level fix and have to be checked separately."""
    handler, _, _ = _handler_as_main_builds_it(tmp_path)

    status = handler._platform_status_payload()
    assert status["tenant_id"] == TENANT
    assert status["project_scope_key"] == f"tenant:{TENANT}"

    contract = handler._platform_integration_contract_payload()
    assert contract, "the integration contract came back empty"


def test_the_two_most_used_routes_do_not_400(tmp_path: pathlib.Path) -> None:
    """THE LOAD-BEARING TEST. `not 503` is not the same as `works`.

    #197's spike measured 400 on `memory/search` and `memory/remember` after enabling the
    platform, because `main()`'s control plane and the platform's scope set were built
    independently: `scope 'user:...' is not registered for tenant '...'`. That refusal is
    a 400, so a test asserting only "no longer 503" passes while both routes are broken.

    Driven through `_platform()` so the caller check runs, then through the platform's own
    operations -- the same path the HTTP handlers take.
    """
    handler, _, _ = _handler_as_main_builds_it(tmp_path)
    platform = handler._platform()

    written = asyncio.run(
        platform.memory_remember(
            agent_id=AGENT,
            subject="Platform API",
            predicate="is",
            object="reachable",
            relationship_type="IS",
        )
    )
    assert written is not None, "memory_remember returned nothing"

    found = asyncio.run(platform.memory_search(agent_id=AGENT, query="Platform API"))
    assert found is not None, "memory_search returned nothing"


def test_every_scope_the_platform_WRITES_to_is_registered_for_the_tenant(tmp_path: pathlib.Path) -> None:
    """The mechanism behind the test above, asserted directly.

    `_request_policy` refuses a scope the control plane does not know. The platform writes
    to its project scope and, in SIMPLE mode, its user scope -- so both must be registered,
    and the user scope is the one the old hardcoded `(default_scope,)` left out.
    """
    handler, _, platform_scopes = _handler_as_main_builds_it(tmp_path)
    for scope in platform_scopes:
        handler._request_policy(scope)  # raises ValueError if unregistered

    assert any(s.kind == ScopeKind.USER for s in platform_scopes), (
        "SIMPLE mode should expose a user scope; without one this test cannot catch the regression it exists for"
    )


def test_a_scope_the_platform_does_NOT_own_is_still_refused(tmp_path: pathlib.Path) -> None:
    """The positive control's opposite. Registering the platform's scopes must not have
    registered everything -- that would trade a 400 for a scope-plane leak."""
    handler, _, _ = _handler_as_main_builds_it(tmp_path)
    with pytest.raises((ValueError, HttpApiError)):
        handler._request_policy(MemoryScope(kind=ScopeKind.TENANT, scope_id="some-other-tenant"))


def test_main_CALLS_the_helper_and_routes_its_scopes_to_BOTH_gates() -> None:
    """The only claim here that cannot be executed, so the only one left to the source.

    `build_hosted_platform` is now driven directly by the tests above -- the decision and
    the gate are covered behaviourally. What no test can execute is `main()` itself: it
    binds a socket. So this asserts the two things `main()` still has to get right, and
    nothing more.

    The second assertion is the one worth having. Assigning the platform without routing
    its scopes to BOTH gates is precisely the state #197 measured as a 400 on
    `memory/search` and `memory/remember`: green on "no longer 503", broken in practice.
    """
    tree = ast.parse(ADMIN_SERVER.read_text())
    main = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == "main"
    )
    source = ast.unparse(main)

    assert "build_hosted_platform(" in source, (
        "main() no longer calls build_hosted_platform -- every /api/platform/* route is back to 503"
    )
    assert source.count("platform_scopes") >= 3, (
        "the platform's scopes must reach BOTH gates -- `handler.configured_scopes` AND "
        "the control plane's `extra_scopes` -- or memory/search and memory/remember 400. "
        f"Found {source.count('platform_scopes')} uses; expected the binding plus both consumers"
    )


def test_the_platform_is_OFF_where_identity_is_disarmed(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate, executed rather than read out of the source.

    `--no-admin-writes` does not cover `/api/platform/`, and `requireGatewayIdentity` is
    `true` on `latest` only -- so an ungated creation would publish 15 unauthenticated
    WRITE routes on prod. Returning `None` here is what keeps `_platform()` answering 503
    on every environment that cannot say who is calling.
    """
    monkeypatch.delenv(REQUIRE_IDENTITY_ENV, raising=False)
    platform, scopes = build_hosted_platform(
        graph_path=tmp_path / "off.sqlite",
        store=None,
        tenant_id=TENANT,
        agent_id=AGENT,
    )
    assert platform is None, "the platform was built where no caller can be attributed"
    assert scopes == (), "scopes were registered for a platform that does not exist"


def test_it_returns_BOTH_the_project_and_user_scope(tmp_path: pathlib.Path) -> None:
    """The user scope is the one the old hardcoded `(default_scope,)` left out, and its
    absence is what made the two most-used routes 400."""
    _, scopes = build_hosted_platform(
        graph_path=tmp_path / "scopes.sqlite",
        store=None,
        tenant_id=TENANT,
        agent_id=AGENT,
    )
    kinds = {scope.kind for scope in scopes}
    assert ScopeKind.TENANT in kinds, f"no project scope in {[s.key for s in scopes]}"
    assert ScopeKind.USER in kinds, (
        f"no user scope in {[s.key for s in scopes]} -- in SIMPLE mode the platform writes "
        "to one, and leaving it unregistered is the measured 400"
    )


def test_MAIN_ITSELF_wires_a_working_platform(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed entry point, executed. Nothing else in the suite runs `main()`.

    That is not a small gap: `main()` is what the chart launches
    (`values.yaml` -> `pyproject.toml` -> `admin_server:main`), and the defect this commit
    fixes -- `handler.platform` never assigned -- lived there for the entire life of the
    hosted API while every other test passed. A defect in the one function no test
    executes is invisible by construction, which is why the alternative here was a source
    read rather than an assertion.

    Only the socket is stubbed. Argument parsing, store selection, tenant resolution, the
    control plane, the principal and the platform wiring all run for real.
    """
    import memotron.admin_server as module

    # `main()` refuses a graph path that does not exist, which is right: the deployed
    # server always has a store. Build one the way a first run would.
    graph = tmp_path / "main.sqlite"
    build_hosted_platform(graph_path=graph, store=None, tenant_id=TENANT, agent_id=AGENT)
    assert graph.exists(), "the fixture failed to create a graph, so this proves nothing"

    captured: dict[str, object] = {}

    class _NoSocketServer:
        def __init__(self, address: tuple[str, int], handler: object) -> None:
            captured["handler"] = handler

        def serve_forever(self) -> None:
            return None

        def server_close(self) -> None:
            return None

    monkeypatch.setattr(module, "HTTPServer", _NoSocketServer)
    monkeypatch.setattr(
        "sys.argv",
        [
            "memotron-admin-server",
            "--graph-path",
            str(tmp_path / "main.sqlite"),
            "--scope",
            f"tenant:{TENANT}",
            "--tenant-id",
            TENANT,
            "--agent-id",
            AGENT,
            "--static-dir",
            str(tmp_path / "missing-dist"),
        ],
    )

    module.main()

    handler = captured.get("handler")
    assert handler is not None, "main() never constructed a server"
    assert handler.platform is not None, (
        "main() ran to completion with handler.platform still None -- every "
        "/api/platform/* route would answer 503, which is the defect this commit fixes"
    )
    kinds = {scope.kind for scope in handler.configured_scopes}
    assert ScopeKind.USER in kinds, (
        f"main() registered {[s.key for s in handler.configured_scopes]} -- without the "
        "user scope, memory/search and memory/remember 400"
    )
