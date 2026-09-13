"""`/api/platform/*` authenticates its caller. The console routes deliberately do not.

#50. Before this, `admin_server` resolved no identity at all: `principal` is a CLASS
attribute set once by `main()`, so #206 Phase 2's tenant check at `_platform()` compared
that one principal's tenant against itself and could refuse nothing. `principal_lookup`
appeared zero times in the module.

**Why only the platform routes**, which is the part most likely to read as a gap:

The console is a browser app and `ui/admin/src/api.ts:31-39` sends `Accept` and
`Content-Type` and nothing else -- no credential, by construction. Requiring one on
`/api/overview` would 401 every console request in any environment with the flag armed,
which is `latest` today: the console would simply go blank. That is the failure #200
already taught us to test for, at production scale. Browser auth is #23 (SSO).

The programmatic surface is different in kind: `/api/platform/*` is the agent-memory HTTP
API that callers reach THROUGH the gateway, so they do carry a key. It is also the surface
#194 wants to enable, and could not safely, because enabling it exposes 15 write routes
that authenticate nobody.

So the split is the product boundary, not a shortcut -- and the tests below assert BOTH
halves of it, because "the console still works" is exactly as load-bearing here as "the
neighbour is refused".
"""

from __future__ import annotations

import ast
import json
import pathlib

import pytest

from memotron import MemoryScope, PrincipalRole, ScopeKind
from memotron.admin_server import (
    HttpApiError,
    MemoryGraphHandler,
    build_demo_control_plane,
    build_demo_principal,
)
from memotron.agent_memory import AgentMemoryPlatform
from memotron.gateway_identity import (
    FORWARDED_KEY_HEADER,
    GatewayIdentity,
    GatewayPrincipalResolver,
)
from memotron.identity import REQUIRE_IDENTITY_ENV

TENANT = "tenant-under-test"
SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id=TENANT)
ADMIN_SERVER = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron" / "admin_server" / "__init__.py"

#: Functions that may read `self.platform` without going through `_platform()`.
#:
#: Every entry is either the accessor itself, wiring, or a CONSOLE-side reader. None is an
#: `/api/platform/*` handler -- checked by `test_the_exemption_list_holds_no_platform_route`
#: below, because an exemption list that quietly absorbs an operation makes the tripwire
#: mean nothing while still passing.
_EXEMPT = frozenset(
    {
        "_platform",  # the accessor -- it IS the check
        "main",  # wiring
        "_build_principal_resolver",  # wiring
        "_request_policy",  # console: /api/scopes, /api/graph scope policy
        "_tenant_config_payload",  # console: /api/tenant-config
        "_purge_tenant_state_payload",  # console admin write, gated by --no-admin-writes
        "_resolve_dream_request",  # console: /api/dream/*
        "_agent_ids",  # helper; its platform-route caller checks first
        "_tenant_purge_agent_ids",  # purge helper, console side
        # #217: console /api/overview. Reads PRESENCE, not the platform -- the expression
        # is `getattr(self, "platform", None) is not None`, which yields a bool and calls
        # no platform method, so no operation happens without the caller check. It exists
        # because `platform_api_ready` previously tested `bool(platform_api_url)`, a string
        # `main()` always assigns, and so could never be false: the console reported the API
        # ready in exactly the environments where all 18 routes answered 503. Readiness now
        # keys off the same condition `_platform()` uses to raise that 503, which is the
        # point -- the two must not be able to disagree.
        "_overview_payload",
    }
)


class _Headers:
    """The slice of `email.message.Message` that `_sole_header` uses."""

    def __init__(self, pairs: list[tuple[str, str]]) -> None:
        self._pairs = pairs

    def get_all(self, name: str) -> list[str] | None:
        values = [v for k, v in self._pairs if k.lower() == name.lower()]
        return values or None


def _handler(tmp_path: pathlib.Path, *, headers: list[tuple[str, str]], rows: dict[str, dict] | None = None):
    """A handler wired as `main()` wires it, instantiated without a socket.

    `object.__new__` rather than a real server: the decision under test is reached from
    `_platform()`, and standing up an `HTTPServer` would add a socket, a thread and a port
    to every one of these cases without changing what is being decided.
    """
    platform = AgentMemoryPlatform.create(graph_path=tmp_path / "identity.sqlite", project_id=TENANT)
    handler = type("PlatformIdentityProbe", (MemoryGraphHandler,), {})
    handler.client = platform.client
    handler.runtime_client = platform.client
    handler.platform = platform
    handler.default_scope = SCOPE
    handler.tenant_id = TENANT
    handler.agent_ids = ()
    handler.configured_scopes = (SCOPE,)
    handler.static_dir = tmp_path / "missing-dist"
    handler.control_plane = build_demo_control_plane(
        client=platform.client,
        default_scope=SCOPE,
        tenant_id=TENANT,
        agent_id="probe-agent",
        extra_scopes=(SCOPE,),
    )
    platform.client.control_plane = handler.control_plane
    handler.principal = build_demo_principal(
        principal_id="admin",
        tenant_id=TENANT,
        agent_id="probe-agent",
        default_scope=SCOPE,
        role=PrincipalRole.ADMIN,
    )
    handler.principal_resolver = GatewayPrincipalResolver(
        resolver=lambda key: GatewayIdentity(key_alias=f"alias-{key}", team_id=None, user_id=None),
        principal_lookup=lambda alias: (rows or {}).get(alias),
    )
    instance = object.__new__(handler)
    instance.headers = _Headers(headers)
    instance.path = "/api/platform/status"
    return instance


def _row(tenant: str) -> dict:
    return {
        "principal_id": f"p-{tenant}",
        "tenant_id": tenant,
        "agent_id": "probe-agent",
        "default_scope_key": f"tenant:{tenant}",
        "allowed_scope_keys": [f"tenant:{tenant}"],
        "role": "user",
    }


@pytest.fixture(autouse=True)
def _flag_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits the flag from the ambient environment, in either direction."""
    monkeypatch.delenv(REQUIRE_IDENTITY_ENV, raising=False)


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")


class TestTheRefusalLadder:
    def test_no_key_is_401(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        handler = _handler(tmp_path, headers=[])
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 401

    def test_a_key_whose_alias_is_UNBOUND_is_403_and_says_how_to_bind(
        self, tmp_path: pathlib.Path, guard_on: None
    ) -> None:
        """The remedy is safe to return and an operator has no other way to learn it."""
        handler = _handler(tmp_path, headers=[(FORWARDED_KEY_HEADER, "k1")], rows={})
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 403
        assert "memotron key bind --alias alias-k1" in str(excinfo.value)

    def test_a_key_bound_to_ANOTHER_tenant_is_refused(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """The whole point. Authenticated, real, and not this tenant's."""
        handler = _handler(tmp_path, headers=[(FORWARDED_KEY_HEADER, "k1")], rows={"alias-k1": _row("other-tenant")})
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 403

    def test_the_SAME_header_twice_with_different_values_is_401(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """A request carrying two different credentials has no defensible reading, and
        picking one silently means a later audit line can name the key that was NOT used."""
        handler = _handler(
            tmp_path,
            headers=[(FORWARDED_KEY_HEADER, "k1"), (FORWARDED_KEY_HEADER, "k2")],
            rows={"alias-k1": _row(TENANT), "alias-k2": _row(TENANT)},
        )
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 401

    def test_a_PRESENT_BUT_EMPTY_forwarded_header_does_not_fall_back(
        self, tmp_path: pathlib.Path, guard_on: None
    ) -> None:
        """DW-014: `Authorization` may be carrying the GATEWAY's own key. Falling through
        re-opens the confused deputy the precedence rule exists to close."""
        handler = _handler(
            tmp_path,
            headers=[(FORWARDED_KEY_HEADER, ""), ("Authorization", "Bearer k1")],
            rows={"alias-k1": _row(TENANT)},
        )
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 401

    def test_a_lookup_that_RAISES_is_503_not_403(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """Fail closed either way, but a broken store is a different incident from a
        refused caller, and only one of them is the caller's problem."""
        handler = _handler(tmp_path, headers=[(FORWARDED_KEY_HEADER, "k1")])
        handler.principal_resolver = GatewayPrincipalResolver(
            resolver=lambda key: GatewayIdentity(key_alias="alias-k1", team_id=None, user_id=None),
            principal_lookup=lambda alias: (_ for _ in ()).throw(RuntimeError("store down")),
        )
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        assert excinfo.value.status == 503

    def test_the_refusal_body_does_not_echo_the_key(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """A refusal is the one response an attacker can always elicit. LiteLLM's own auth
        error body was observed carrying `Received API Key = sk-...`.

        The resolver here is deliberately NOT the shared fake. That one derives the alias
        as `f"alias-{key}"`, so the key appears in the refusal *via the alias* and this
        test passed on its own construction rather than on the code -- caught on first
        run. A real `key_alias` is assigned by the gateway and carries no key material, so
        the fake has to model that or the assertion means nothing.
        """
        secret = "sk-do-not-echo-me"
        handler = _handler(tmp_path, headers=[(FORWARDED_KEY_HEADER, secret)], rows={})
        handler.principal_resolver = GatewayPrincipalResolver(
            resolver=lambda key: GatewayIdentity(key_alias="svc-consumer-01", team_id=None, user_id=None),
            principal_lookup=lambda alias: None,
        )
        with pytest.raises(HttpApiError) as excinfo:
            handler._platform()
        rendered = json.dumps({"error": str(excinfo.value)})
        assert secret not in rendered
        # Positive control: the refusal is not empty, and it DOES name the alias, which is
        # documented as safe to return -- the caller supplied the key it belongs to, and
        # without it an operator cannot learn what to bind.
        assert "svc-consumer-01" in rendered


class TestThePositiveControls:
    """A guard that refuses everything satisfies every test above and is an outage."""

    def test_the_matching_tenant_is_PERMITTED(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        handler = _handler(tmp_path, headers=[(FORWARDED_KEY_HEADER, "k1")], rows={"alias-k1": _row(TENANT)})
        assert handler._platform() is handler.platform

    def test_an_AUTHORIZATION_bearer_key_also_works(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """With no forwarded header at all, `Authorization: Bearer` is the fallback."""
        handler = _handler(tmp_path, headers=[("Authorization", "Bearer k1")], rows={"alias-k1": _row(TENANT)})
        assert handler._platform() is handler.platform

    def test_with_the_flag_OFF_no_key_is_needed(self, tmp_path: pathlib.Path) -> None:
        """The rollout default: byte-for-byte the pre-#50 behaviour."""
        handler = _handler(tmp_path, headers=[])
        assert handler._platform() is handler.platform

    def test_with_NO_RESOLVER_the_surface_is_unchanged_even_under_the_flag(
        self, tmp_path: pathlib.Path, guard_on: None
    ) -> None:
        """`main()` returns `None` when identity is not required, and every existing test
        constructs a handler without one. Neither may start failing."""
        handler = _handler(tmp_path, headers=[])
        handler.principal_resolver = None
        assert handler._platform() is handler.platform


class TestTheConsoleIsDELIBERATELYUnauthenticated:
    def test_console_routes_do_not_reach_the_caller_check(self, tmp_path: pathlib.Path, guard_on: None) -> None:
        """The scope decision, asserted rather than left to a comment. `/api/overview` must
        answer with NO key while the flag is armed, or the console blanks on `latest`.

        If someone later widens `_caller_principal` to all routes, this fails and points at
        the reason -- which is what a comment alone would not do.
        """
        handler = _handler(tmp_path, headers=[])
        payload = __import__("asyncio").run(handler._overview_payload({}))
        assert payload["tenant"]["tenant_id"] == TENANT

    def test_the_console_client_really_does_send_no_credential(self) -> None:
        """The premise the whole scope rests on. If the UI ever starts sending a key, the
        tradeoff above changes and this test is where that conversation starts."""
        api = (pathlib.Path(__file__).resolve().parents[1] / "ui" / "admin" / "src" / "api.ts").read_text()
        assert "Authorization" not in api
        assert FORWARDED_KEY_HEADER not in api


def test_EVERY_platform_route_reaches_the_caller_check() -> None:
    """The tripwire, and the only test here that survives a refactor.

    Everything above proves the check WORKS. This proves it is REACHED, by every
    `/api/platform/*` route including ones that do not exist yet -- the regression shape
    being a route added next month that reads `self.platform` directly and is therefore
    unauthenticated while every other test passes.

    `_platform()` is the choke point, so the assertion is that no handler body reaches
    `self.platform` around it.
    """
    tree = ast.parse(ADMIN_SERVER.read_text())
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name in _EXEMPT:
            continue
        # A body that calls `self._platform()` has ALREADY passed the caller check, so
        # reading `self.platform` afterwards is fine -- `_platform_status_payload` does
        # exactly that. Checking for the call rather than maintaining a name list is what
        # makes this gate self-maintaining: a NEW platform handler that reads the
        # attribute without going through the accessor fails, and no list needs editing.
        if any(
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr == "_platform"
            and isinstance(inner.func.value, ast.Name)
            and inner.func.value.id == "self"
            for inner in ast.walk(node)
        ):
            continue
        for inner in ast.walk(node):
            # Two spellings, because the module uses both. Checking only the attribute
            # form would leave this gate blind to the exact hazard it names --
            # `_resolve_dream_request` reads `getattr(self, "platform", None)`, which an
            # attribute-only walk cannot see.
            direct = (
                isinstance(inner, ast.Attribute)
                and inner.attr == "platform"
                and isinstance(inner.value, ast.Name)
                and inner.value.id == "self"
            )
            reflective = (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "getattr"
                and len(inner.args) >= 2
                and isinstance(inner.args[0], ast.Name)
                and inner.args[0].id == "self"
                and isinstance(inner.args[1], ast.Constant)
                and inner.args[1].value == "platform"
            )
            if direct or reflective:
                offenders.append(f"{node.name}:{inner.lineno}")
    assert not offenders, (
        "these read `self.platform` directly instead of going through `_platform()`, so they "
        f"skip the caller check: {offenders}. Call `self._platform()`, or add the name to the "
        "exemption set in this test with a reason."
    )


def test_the_exemption_list_holds_no_platform_route() -> None:
    """Guard the guard. `_EXEMPT` is only safe while every entry is console-side or
    wiring; one `/api/platform/*` handler in there and the tripwire above passes while
    saying nothing about it.

    A platform handler is identified the way the module identifies one: its name is
    dispatched under `AGENT_MEMORY_ROUTE_PREFIX`. Read from the dispatch, not from a
    hand-kept list, so a route renamed or added shows up here without anyone remembering.
    """
    source = ADMIN_SERVER.read_text()
    tree = ast.parse(source)

    dispatched_under_platform: set[str] = set()
    for node in ast.walk(tree):
        # `elif parsed.path == "/api/platform/x": self._send_json(self._handler(...))`
        if not isinstance(node, ast.Compare) or not isinstance(node.comparators[0], ast.Constant):
            continue
        path = node.comparators[0].value
        if not isinstance(path, str) or not path.startswith("/api/platform/"):
            continue
        parent = next(
            (n for n in ast.walk(tree) if isinstance(n, ast.If) and n.test is node),
            None,
        )
        if parent is None:
            continue
        for inner in ast.walk(parent):
            if isinstance(inner, ast.Attribute) and isinstance(inner.value, ast.Name) and inner.value.id == "self":
                dispatched_under_platform.add(inner.attr)

    assert dispatched_under_platform, "found no /api/platform/* dispatch at all -- this test cannot pass vacuously"
    # `_platform` appears in the dispatch because some routes call the accessor directly.
    # It and the wiring are the MECHANISM, not operations smuggled into the exemption
    # list, so they are excluded here -- everything else in `_EXEMPT` must be console-side.
    mechanism = {"_platform", "main", "_build_principal_resolver"}
    overlap = dispatched_under_platform & (_EXEMPT - mechanism)
    assert not overlap, (
        f"these are dispatched under /api/platform/ AND exempt from the caller check: {sorted(overlap)}. "
        "An exemption must be console-side, never a platform operation."
    )
