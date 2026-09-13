"""The identity middleware, driven through the real ASGI stack.

#126. `tests/test_mcp_principal_resolution.py` puts a principal on a request by hand and
checks what `_scope` does with it. That is necessary and it is not sufficient, because the
one thing it cannot test is the assumption the whole design rests on: **that a value written
in ASGI middleware is visible inside an MCP tool body.**

It is not obviously true. MCP dispatches tool calls in a different anyio task from the
middleware (`streamable_http_manager.py:227-229`), so the contextvar this would naturally
have used reads back as its default in the tool. `request.state` works only because it is
backed by the shared `scope["state"]` dict. Getting that wrong does not raise anywhere --
the tool reads `None`, and a server that authenticates every caller at the door and then
authorizes nothing is indistinguishable from a working one until someone reads another
tenant's memory.

So these tests speak MCP over HTTP: initialize, notifications/initialized, tools/call, with
real headers, through `GatewayIdentityMiddleware` wrapping `build_mcp_http_app(mcp)` --
the same call the entry point makes.

Two mechanics worth knowing before editing this file:

* **`base_url` must be `localhost`.** FastMCP's DNS-rebinding allowlist rejects TestClient's
  default `testserver` host with **421 Misdirected Request** and no other symptom. That is
  the same failure that once made every request through the deployed ingress 421 while
  `/health` stayed 200, reproduced here for free.
* **The client is built inside the lifespan.** `SQLiteStorageBackend` opens its connection
  without `check_same_thread=False`, and TestClient runs the app in a portal thread, so a
  client built at module scope raises "SQLite objects created in a thread can only be used
  in that same thread" from inside the tool -- an error that looks like a Memotron bug
  and is a test-harness artifact.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

from memotron import mcp_server
from memotron.gateway import GatewayRequestError
from memotron.gateway_identity import GatewayIdentity
from memotron.mcp_auth import REQUIRE_IDENTITY_ENV, GatewayIdentityMiddleware
from memotron.mcp_server import build_client, mcp
from memotron.runtime import build_mcp_http_app

ALPHA_KEY = "sk-alpha-virtual-key"
BRAVO_KEY = "sk-bravo-virtual-key"
#: An ADMIN of tenant-alpha. Needed as the POSITIVE CONTROL for the fleet-wide
#: readers: without it, "user is refused" is indistinguishable from "the tool is
#: broken for everyone".
ADMIN_KEY = "sk-admin-virtual-key"  # pragma: allowlist secret

#: Two keys, two aliases, two tenants. A single key proves nothing here: if header
#: extraction were broken and every request resolved to the same principal, a lone positive
#: arm would still pass. The differential is the control.
_IDENTITIES = {
    ALPHA_KEY: GatewayIdentity(key_alias="dw-alpha-01", team_id=None, user_id=None),
    BRAVO_KEY: GatewayIdentity(key_alias="dw-bravo-01", team_id=None, user_id=None),
    ADMIN_KEY: GatewayIdentity(key_alias="dw-admin-01", team_id=None, user_id=None),
}

_ROWS: dict[str, dict[str, Any]] = {
    "dw-alpha-01": {
        "principal_id": "p-alpha",
        "tenant_id": "tenant-alpha",
        "role": "user",
        "default_scope_key": "tenant:tenant-alpha",
        "allowed_scope_keys": ["tenant:tenant-alpha"],
    },
    "dw-bravo-01": {
        "principal_id": "p-bravo",
        "tenant_id": "tenant-bravo",
        "role": "user",
        "default_scope_key": "tenant:tenant-bravo",
        "allowed_scope_keys": ["tenant:tenant-bravo"],
    },
    "dw-admin-01": {
        "principal_id": "p-admin",
        "tenant_id": "tenant-alpha",
        "role": "admin",
        "default_scope_key": "tenant:tenant-alpha",
        "allowed_scope_keys": ["tenant:tenant-alpha"],
    },
}


class _FakeGateway:
    """Stands in for `/key/info`. Counts calls so cache behaviour is observable."""

    def __init__(self, *, raises: Exception | None = None) -> None:
        self.calls: list[str] = []
        self.raises = raises

    def __call__(self, raw_key: str) -> GatewayIdentity:
        self.calls.append(raw_key)
        if self.raises is not None:
            raise self.raises
        try:
            return _IDENTITIES[raw_key]
        except KeyError:
            raise GatewayRequestError("no such key", attempts=1, retryable=False, status=401) from None


def _make_app(middleware_box: dict[str, Any]) -> Any:
    """One app for the whole module, built the way the ENTRY POINTS build it.

    Deliberately not one app per test. `FastMCP` caches its `StreamableHTTPSessionManager`
    on the instance and the manager refuses a second run -- "can only be called once per
    instance" -- so a per-test app fails every test after the first against the
    module-global `mcp`. Swapping the middleware's resolver and lookup per test gives the
    same isolation without a second server.

    The identity middleware is passed through `asgi_middleware`, NOT wrapped around the
    result. That is not cosmetic: wrapping put it OUTSIDE FastMCP's guard, while both
    entry points put it inside. Real order in the shipped app is
    `StrictHostGuard -> RequestContextMiddleware -> HostOriginGuardMiddleware ->
    GatewayIdentityMiddleware`, so a forged Host is refused before the gateway lookup
    burns a `_GATEWAY_LOOKUP_LIMITER` slot. This file previously exercised the inverted
    order -- every assertion here held, but against a wiring the product does not ship.
    """
    from starlette.middleware import Middleware

    class _CapturingIdentityMiddleware(GatewayIdentityMiddleware):
        """Identical behaviour; records the instance so a test can clear its cache.

        Needed because Starlette instantiates `Middleware(cls, ...)` lazily on the first
        request, so there is no instance to hold on to at construction time.
        """

        def __init__(self, app: Any, **kwargs: Any) -> None:
            super().__init__(app, **kwargs)
            middleware_box["middleware"] = self

    inner = build_mcp_http_app(
        mcp,
        asgi_middleware=[
            Middleware(
                _CapturingIdentityMiddleware,
                resolver=lambda key: middleware_box["resolver"](key),
                principal_lookup=lambda alias: middleware_box["principal_lookup"](alias),
            )
        ],
    )

    # `build_client` MUST run inside the lifespan, not before it. TestClient drives the
    # app on a portal thread, and the SQLite connection is bound to the thread that
    # created it -- building the client on the main thread makes every tool call fail
    # with "SQLite objects created in a thread can only be used in that same thread".
    # Learned twice now; that is why it is a comment and not a blank line.
    #
    # The Starlette below is test scaffolding for the lifespan only. It does NOT change
    # the middleware order under test: the identity middleware is inside `inner`, where
    # `asgi_middleware` put it, exactly as the entry points arrange it.
    guarded_starlette = _starlette_under(inner)

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette) -> Any:
        build_client(":memory:")
        async with guarded_starlette.router.lifespan_context(guarded_starlette):
            yield

    return Starlette(routes=[Mount("/", app=inner)], lifespan=lifespan)


def _starlette_under(app: Any) -> Starlette:
    """The Starlette instance inside whatever `build_mcp_http_app` returned.

    It returns a `StrictHostGuard`, a plain ASGI callable holding the app on `.app`, so
    `app.router` does not exist on the outermost object.
    """
    node = app
    for _ in range(4):
        if isinstance(node, Starlette):
            return node
        node = getattr(node, "app", None)
        if node is None:
            break
    raise AssertionError(f"no Starlette found under {type(app).__name__}")


_JSON_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}


def _sse_payload(text: str) -> dict[str, Any]:
    """The JSON-RPC object out of an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: ") :])
    raise AssertionError(f"no SSE data frame in response: {text!r}")


class _Session:
    """One initialized MCP session over TestClient, authenticated with `key`."""

    def __init__(self, client: TestClient, key: str | None) -> None:
        self.client = client
        self.headers = dict(_JSON_HEADERS)
        if key is not None:
            self.headers["x-litellm-api-key"] = key

    def initialize(self) -> Any:
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers=self.headers,
        )
        if response.status_code == 200:
            # The shipped server runs `stateless_http=True` (#246), so there is no
            # session to carry: each request stands alone and no `mcp-session-id` is
            # issued. Reading that header unconditionally is what this did before, and
            # it raised KeyError against the real serving configuration -- the tests
            # only passed because they exercised a stateful app deployed nowhere.
            #
            # The header is still forwarded WHEN present, so this harness keeps working
            # if a stateful app is ever passed in.
            session_id = response.headers.get("mcp-session-id")
            if session_id is not None:
                self.headers["mcp-session-id"] = session_id
            self.client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
                headers=self.headers,
            )
        return response

    def call(self, tool: str, **arguments: Any) -> dict[str, Any]:
        response = self.client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments},
            },
            headers=self.headers,
        )
        assert response.status_code == 200, response.text
        return _sse_payload(response.text)["result"]

    def add_memory(self, scope_id: str) -> dict[str, Any]:
        return self.call(
            "add_memory",
            subject="alpha-subject",
            predicate="REQUIRES",
            object="beta-object",
            relationship_type="REQUIRES",
            scope_kind="tenant",
            scope_id=scope_id,
        )


_BOX: dict[str, Any] = {}


@pytest.fixture(scope="module")
def client() -> Iterator[TestClient]:
    # `localhost`, not TestClient's default `testserver`: the DNS-rebinding allowlist
    # answers 421 to anything else, with no message that points at the host.
    app = _make_app(_BOX)
    with TestClient(app, base_url="http://localhost:8000") as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")


@pytest.fixture(autouse=True)
def gateway() -> _FakeGateway:
    """Fresh gateway, fresh lookup and a CLEARED identity cache for every test.

    The cache reset is not housekeeping: it is shared mutable state on a module-scoped
    middleware, and without it `test_repeated_calls_hit_the_gateway_once` would measure
    whatever an earlier test left behind rather than its own behaviour.
    """
    fake = _FakeGateway()
    _BOX["resolver"] = fake
    _BOX["principal_lookup"] = _ROWS.get
    if "middleware" in _BOX:
        _BOX["middleware"]._cache.clear()
    return fake


def _resolve_with(resolver: Any) -> None:
    _BOX["resolver"] = resolver


def _look_up_with(principal_lookup: Any) -> None:
    _BOX["principal_lookup"] = principal_lookup


class TestTheWriteReachesTheToolBody:
    """The load-bearing claim. Everything else is arrangement around these two tests."""

    def test_an_authenticated_caller_writes_to_its_OWN_tenant(self, client: TestClient) -> None:
        """THE POSITIVE CONTROL, and the proof that `request.state` survives the task hop.

        If the principal did not reach the tool, `_scope` would raise
        `IdentityUnavailableError` and this would fail -- which is the fail-closed branch
        doing its job, and is exactly why that branch is not optional.
        """
        session = _Session(client, ALPHA_KEY)
        assert session.initialize().status_code == 200
        result = session.add_memory("tenant-alpha")
        assert not result.get("isError"), result
        assert "relationship_uuid" in result["content"][0]["text"]

    def test_the_SAME_call_to_ANOTHER_tenant_is_refused(self, client: TestClient) -> None:
        """Identical request, one field different. Paired with the test above this is the
        whole security claim: the scope a caller may name is decided by its key, not by its
        argument."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.add_memory("tenant-bravo")
        assert result["isError"] is True
        assert "tenant:tenant-bravo" in result["content"][0]["text"]


class TestTwoKeysResolveToTwoPrincipals:
    def test_each_key_reaches_its_own_tenant_and_only_its_own(self, client: TestClient, gateway: _FakeGateway) -> None:
        """The differential. Were header extraction broken -- or were the server falling
        back to its own `LITELLM_API_KEY` -- both keys would land on one principal, and a
        single-key test would pass anyway."""
        alpha = _Session(client, ALPHA_KEY)
        alpha.initialize()
        bravo = _Session(client, BRAVO_KEY)
        bravo.initialize()

        assert not alpha.add_memory("tenant-alpha").get("isError")
        assert not bravo.add_memory("tenant-bravo").get("isError")
        assert alpha.add_memory("tenant-bravo")["isError"] is True
        assert bravo.add_memory("tenant-alpha")["isError"] is True

        assert set(gateway.calls) == {ALPHA_KEY, BRAVO_KEY}


class TestRefusalsAtTheDoor:
    def test_no_key_is_401(self, client: TestClient, gateway: _FakeGateway) -> None:
        response = _Session(client, None).initialize()
        assert response.status_code == 401
        assert gateway.calls == [], "a keyless request must not reach the gateway at all"

    def test_health_needs_no_key_at_all(self, client: TestClient) -> None:
        """The one that turns an auth bug into a crashloop if it regresses: kubelet probes
        `/health` with no credential, so a 401 here restarts the pod forever and presents
        as a broken service rather than a misconfigured guard."""
        response = client.get("/health")
        assert response.status_code == 200
        assert response.text == "ok"

    def test_an_unbound_alias_is_403_and_says_how_to_bind_it(self, client: TestClient) -> None:
        """403, never a fallback principal. The message names the alias and the command,
        because the operator hitting this has no other way to learn which alias to bind."""
        _look_up_with(lambda alias: None)
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 403
        detail = response.json()["error"]
        assert "dw-alpha-01" in detail
        assert "memotron key bind" in detail

    def test_a_key_with_no_alias_is_403(self, client: TestClient) -> None:
        """DW-027 measured ~40% of gateway keys asserting no identity. The alias IS the
        lookup key, so such a key authenticates and cannot be authorized -- 403, not 401."""
        anonymous = GatewayIdentity(key_alias=None, team_id=None, user_id=None)
        _resolve_with(lambda key: anonymous)
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 403
        assert "key_alias" in response.json()["error"]

    def test_a_gateway_refusal_is_401(self, client: TestClient) -> None:
        _resolve_with(_FakeGateway(raises=GatewayRequestError("bad key", attempts=1, retryable=False, status=401)))
        assert _Session(client, ALPHA_KEY).initialize().status_code == 401

    def test_a_gateway_OUTAGE_is_503_with_Retry_After_not_401(self, client: TestClient) -> None:
        """The distinction that decides what an operator does next. A 401 during a gateway
        outage sends every caller off to re-check a credential that is perfectly valid,
        while the actual fault is somebody else's service being down.

        This is also where the blast radius of this slice becomes visible and is accepted:
        **gateway down means Memotron down**, including operations that need no LLM.
        """
        _resolve_with(_FakeGateway(raises=GatewayRequestError("connection refused", attempts=3, retryable=True)))
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"

    def test_an_unreadable_registry_row_is_403_not_a_degraded_principal(self, client: TestClient) -> None:
        """`principal_from_registry_row` raises rather than defaulting, and this asserts the
        middleware honours that. Degrading here WIDENS access: an unparsed
        `default_scope_key` silently becomes one fewer entry in the allowlist, not more."""
        _look_up_with(lambda alias: {"principal_id": "p-alpha", "tenant_id": "tenant-alpha", "role": "sorcerer"})
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 403
        assert "unreadable" in response.json()["error"]


class TestHeaderPrecedence:
    def test_x_litellm_api_key_WINS_over_Authorization(self, client: TestClient, gateway: _FakeGateway) -> None:
        """Pinned because the order is a security decision, not a style one. Through the
        gateway, `Authorization` may be carrying the GATEWAY's key rather than the caller's
        (DW-014), while `x-litellm-api-key` is the channel a forwarded caller key arrives on
        (DW-028). Preferring `Authorization` would authenticate the wrong party
        successfully -- a failure with no symptom.
        """
        session = _Session(client, BRAVO_KEY)
        session.headers["Authorization"] = f"Bearer {ALPHA_KEY}"
        session.initialize()
        # Resolved as bravo, so bravo's tenant is allowed and alpha's is not.
        assert not session.add_memory("tenant-bravo").get("isError")
        assert session.add_memory("tenant-alpha")["isError"] is True
        assert set(gateway.calls) == {BRAVO_KEY}

    def test_a_bearer_Authorization_alone_is_accepted(self, client: TestClient) -> None:
        """The fallback still has to work -- a direct caller not going through the gateway
        has only this header."""
        session = _Session(client, None)
        session.headers["Authorization"] = f"Bearer {ALPHA_KEY}"
        assert session.initialize().status_code == 200
        assert not session.add_memory("tenant-alpha").get("isError")


class TestTheIdentityIsCachedAndThePrincipalIsNot:
    def test_repeated_calls_hit_the_gateway_once(self, client: TestClient, gateway: _FakeGateway) -> None:
        """The gateway lookup costs p50 131 ms / p95 461 ms (DW-026); without the cache
        that is a per-request tax on every memory operation."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        session.add_memory("tenant-alpha")
        session.add_memory("tenant-alpha")
        assert gateway.calls.count(ALPHA_KEY) == 1

    def test_the_PRINCIPAL_is_re_read_every_request(self, client: TestClient) -> None:
        """The revocation lever we actually own. `memotron key unbind` has to take effect
        on the next request; caching the principal would hold a revoked binding open for a
        cache TTL, and the store read is local and cheap."""
        lookups: list[str] = []

        def lookup(alias: str) -> dict[str, Any] | None:
            lookups.append(alias)
            return _ROWS.get(alias)

        _look_up_with(lookup)
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        session.add_memory("tenant-alpha")
        session.add_memory("tenant-alpha")
        assert len(lookups) > 1, "the principal must be re-read, not cached with the identity"


class TestTheFlagOffIsAPassThrough:
    def test_no_key_no_gateway_call_and_any_tenant(
        self, client: TestClient, gateway: _FakeGateway, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The rollout default, asserted rather than assumed: with the flag unset the
        wrapper does nothing, which is the current deployed behaviour byte for byte.

        It is also the vulnerability #126 exists to close -- an unauthenticated caller
        writing to a tenant it never named a credential for -- so this test passing is a
        statement about the rollout order, not about the system being safe.
        """
        monkeypatch.delenv(REQUIRE_IDENTITY_ENV, raising=False)
        session = _Session(client, None)
        assert session.initialize().status_code == 200
        assert not session.add_memory("any-tenant-at-all").get("isError")
        assert gateway.calls == []


class TestTheFleetWideToolsRefuseAnOmittedScope:
    """The three tools that go store-wide when the caller omits `scope_kind`/`scope_id`.

    They are the reason `_OPTIONAL_SCOPE_TOOLS` exists: each one DECLARES a scope, so it
    satisfies the "is it scoped" tripwire, and each one reaches a fleet-wide code path by
    simply not passing it. Exercised here rather than in a unit test because the omission
    has to travel the real MCP argument path -- the default is applied by the tool signature,
    not by anything a direct call would go through.
    """

    @pytest.mark.parametrize(
        "tool",
        ["run_due_dreams", "run_dream_job", "coherence_incidents"],
    )
    def test_omitting_the_scope_is_refused(self, client: TestClient, tool: str) -> None:
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        arguments = {"job_name": "formation-default"} if tool == "run_dream_job" else {}
        result = session.call(tool, **arguments)
        assert result["isError"] is True, result
        text = result["content"][0]["text"]
        assert tool in text
        assert "name a scope explicitly" in text

    def test_naming_an_ALLOWED_scope_is_permitted(self, client: TestClient) -> None:
        """The positive control for the same three. A guard that refused them outright would
        pass every test above while breaking the operator's way to trigger dreaming."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call("coherence_incidents", scope_kind="tenant", scope_id="tenant-alpha")
        assert not result.get("isError"), result

    def test_naming_ANOTHER_tenants_scope_is_still_refused(self, client: TestClient) -> None:
        """And the guard is the same one: naming a scope gets you into `_scope`, which is
        where the allowlist lives. Omitting it and naming someone else's are two different
        refusals for the same reason."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call("coherence_incidents", scope_kind="tenant", scope_id="tenant-bravo")
        assert result["isError"] is True
        assert "tenant:tenant-bravo" in result["content"][0]["text"]


class TestHeaderPermutations:
    """The axes a red-team pass found unguarded: empty, duplicated, and mis-schemed.

    Every one of these was reachable before the review and none was asserted. They share a
    shape -- the *parsing* of the credential, rather than the decision made about it -- and
    parsing is where a confused deputy lives.
    """

    def test_a_PRESENT_but_EMPTY_forwarded_header_does_NOT_fall_through(self, client: TestClient) -> None:
        """The confused deputy the precedence rule was supposed to close, re-opened by
        treating "empty" as "absent".

        A proxy that templates `x-litellm-api-key` sends it empty when the caller sent
        nothing. Falling through then authenticates whatever is in `Authorization` -- which
        DW-014 says may be the GATEWAY's own key. If that alias were ever bound during
        rollout debugging, every caller would collapse onto one identity, and the two-key
        probe would not notice: both of its arms send a non-empty forwarded header.
        """
        session = _Session(client, None)
        session.headers["x-litellm-api-key"] = ""
        session.headers["Authorization"] = f"Bearer {ALPHA_KEY}"
        response = session.initialize()
        assert response.status_code == 401
        assert "present but empty" in response.json()["error"]

    def test_a_WHITESPACE_only_forwarded_header_is_also_refused(self, client: TestClient) -> None:
        session = _Session(client, None)
        session.headers["x-litellm-api-key"] = "   "
        session.headers["Authorization"] = f"Bearer {ALPHA_KEY}"
        assert session.initialize().status_code == 401

    def test_DUPLICATE_forwarded_headers_with_different_values_are_refused(self, client: TestClient) -> None:
        """Last-wins was the bug, and picking first-wins would only have moved it.

        The dict comprehension took the LAST duplicate while Starlette's `Headers.get` takes
        the FIRST, so a caller able to place a header after the gateway's injected one chose
        the credential -- and any audit line written later through `request.headers` would
        have named the *other* key, exonerating the one actually used. A request bearing two
        different credentials has no defensible reading, so it is refused.
        """
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"n": "t", "version": "1"},
                },
            },
            headers=[
                ("Accept", "application/json, text/event-stream"),
                ("Content-Type", "application/json"),
                ("x-litellm-api-key", ALPHA_KEY),
                ("x-litellm-api-key", BRAVO_KEY),
            ],
        )
        assert response.status_code == 401
        assert "more than once" in response.json()["error"]

    def test_a_duplicate_header_with_the_SAME_value_is_fine(self, client: TestClient) -> None:
        """The positive control for the rule above. Refusing identical repeats would break
        callers behind a proxy that re-adds a header it already saw."""
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"n": "t", "version": "1"},
                },
            },
            headers=[
                ("Accept", "application/json, text/event-stream"),
                ("Content-Type", "application/json"),
                ("x-litellm-api-key", ALPHA_KEY),
                ("x-litellm-api-key", ALPHA_KEY),
            ],
        )
        assert response.status_code == 200

    @pytest.mark.parametrize("value", ["Basic {key}", "Token {key}", "Bearer", "Bearer ", "{key}"])
    def test_a_non_BEARER_authorization_is_not_a_credential(self, client: TestClient, value: str) -> None:
        """Only `Bearer <x>` is read. Anything else must be 401 rather than a partial parse
        that happens to produce a plausible-looking string."""
        session = _Session(client, None)
        session.headers["Authorization"] = value.replace("{key}", ALPHA_KEY)
        assert session.initialize().status_code == 401

    def test_a_lowercase_bearer_scheme_IS_accepted(self, client: TestClient) -> None:
        """RFC 7235 makes the scheme case-insensitive. The code handles it; nothing asserted
        it, so it was one refactor from becoming a silent 401 for a conforming client."""
        session = _Session(client, None)
        session.headers["Authorization"] = f"bearer {ALPHA_KEY}"
        assert session.initialize().status_code == 200


class TestThereIsNoSessionToHijack:
    """This class used to characterize a residual. #246 removed the residual.

    What it said before, and what was true: MCP ships an ownership check
    (`streamable_http_manager.py`) that rejects a session driven by a different
    requestor -- but it only arms when `scope["user"]` is an `AuthenticatedUser`, and
    this middleware writes `scope["state"]` instead, so the ownership map stayed empty.
    A leaked `mcp-session-id` (it travels as a plain header through the gateway and any
    logging proxy) therefore let another tenant attach to the victim's transport. What
    contained it was that identity rebinds per REQUEST, so the attacker gained no scope.

    The shipped server now runs `stateless_http=True`. That was #246's fix for a
    correctness problem -- sessions lived in one process, so with 2 replicas behind a
    round-robin Service 5 of 10 gateway calls failed -- and eliminating this residual is
    a side effect of it, not its purpose. Recorded because a side effect that closes a
    documented gap is worth knowing about deliberately: if anyone ever turns stateless
    mode off to solve some other problem, the hijack comes back, and this class is where
    they should find that out.

    The containment is still asserted below, because it is what makes the property hold
    per REQUEST rather than per session -- and it is what would still be load-bearing if
    stateless mode were ever reverted.
    """

    def test_no_session_id_is_issued_at_all(self, client: TestClient) -> None:
        """Nothing to leak, so nothing to attach to. This is the whole residual, gone."""
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 200
        assert "mcp-session-id" not in response.headers, (
            "the server issued a session id, so stateless_http is OFF -- #246's "
            "multi-replica failure is back, and so is the session-hijack residual this "
            "class used to characterize."
        )

    def test_identity_is_rebound_on_every_request(self, client: TestClient) -> None:
        """The containment, asserted without a session: bravo's key is bravo, always.

        Two callers over one TestClient, interleaved. Alpha succeeds in its own scope
        and bravo is refused alpha's, on requests that share a transport and differ only
        in the credential -- which is the property that makes per-request rebinding real
        rather than an artifact of using separate clients.
        """
        alpha = _Session(client, ALPHA_KEY)
        alpha.initialize()
        bravo = _Session(client, BRAVO_KEY)
        bravo.initialize()

        assert not alpha.add_memory("tenant-alpha").get("isError")

        result = bravo.add_memory("tenant-alpha")
        assert result["isError"] is True
        assert "tenant:tenant-alpha" in result["content"][0]["text"]

        # And alpha is unaffected by bravo's refusal on the same transport.
        assert not alpha.add_memory("tenant-alpha").get("isError")


class TestRefusalsDoNotRelayTheGatewaysErROR_BODY:
    def test_a_gateway_401_body_is_NOT_returned_to_the_caller(self, client: TestClient) -> None:
        """Observed on the preview gateway before this was fixed: the refusal body carried
        LiteLLM's `Received API Key = sk-...` together with the key's
        `LiteLLM_VerificationTokenTable` hash, returned to any unauthenticated caller.

        A refusal is the one response an attacker can always elicit, which makes it the last
        place to relay somebody else's error text. The upstream body still reaches our log.
        """
        leaky = "Authentication Error. Received API Key = sk-leaked-value, Key Hash = deadbeefcafe"
        _resolve_with(_FakeGateway(raises=GatewayRequestError(leaky, attempts=1, retryable=False, status=401)))
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 401
        body = response.text
        assert "sk-leaked-value" not in body
        assert "deadbeefcafe" not in body
        assert "Received API Key" not in body

    def test_a_gateway_5xx_body_is_NOT_returned_either(self, client: TestClient) -> None:
        leaky = "upstream said: internal-hostname-10-4-2-9 token=sk-also-leaked"
        _resolve_with(_FakeGateway(raises=GatewayRequestError(leaky, attempts=3, retryable=True)))
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 503
        assert "sk-also-leaked" not in response.text
        assert "internal-hostname" not in response.text

    def test_the_UNBOUND_ALIAS_message_is_still_actionable(self, client: TestClient) -> None:
        """The counterweight. Suppressing every detail would make the one refusal an operator
        must act on unreadable -- they have no other way to learn which alias to bind, and
        the alias belongs to the key the caller itself supplied."""
        _look_up_with(lambda alias: None)
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 403
        assert "dw-alpha-01" in response.json()["error"]
        assert "memotron key bind" in response.json()["error"]


class TestAStoreFailureIsOurFaultNotTheCallers:
    def test_a_raising_principal_lookup_is_503_not_500_and_not_a_pass(self, client: TestClient) -> None:
        """`principal_lookup` raising used to escape the try and become an uncaught 500.

        Fail-closed either way -- nothing proceeded -- but a 500 is a different incident from
        a 503, and the distinction is what tells an operator the store is broken rather than
        the caller. The SQLite thread-affinity bug produced exactly this exception.
        """

        def explode(alias: str) -> dict[str, Any] | None:
            raise RuntimeError("SQLite objects created in a thread can only be used in that same thread")

        _look_up_with(explode)
        response = _Session(client, ALPHA_KEY).initialize()
        assert response.status_code == 503
        assert response.headers["retry-after"] == "5"

    def test_and_it_certainly_does_not_authenticate(self, client: TestClient) -> None:
        """The control that matters: a broken store must not become an open door."""

        def explode(alias: str) -> dict[str, Any] | None:
            raise RuntimeError("boom")

        _look_up_with(explode)
        assert _Session(client, ALPHA_KEY).initialize().status_code != 200


class TestTheFormerlyExemptToolsNowRefuse:
    """WAS `TestWhatTheExemptToolsStillAllow`, and every assertion here is INVERTED.

    That class characterized a live exposure: six `_CROSS_SCOPE_TOOLS` never reach `_scope`,
    so authenticating at the door did not authorize them. Its docstring said they "must be
    REWRITTEN to assert refusal" when the remainder of #126 landed, because "a red-to-green
    edit is a decision someone makes; silence is not". This is that edit.

    Two different guards, because the tools fail in two different ways:

    * The three CREDENTIAL tools take a caller-named `tenant_id`, which no scope allowlist
      can express -- `authorize_tenant` pins it to the principal's own tenant.
    * The three FLEET-WIDE readers take no scope at all, so "name a scope" is not available
      as a remedy -- `require_fleet_wide_read` restricts them to ADMIN.

    Every refusal is paired with a positive control, because "refused" and "broken for
    everyone" are indistinguishable otherwise.
    """

    def test_a_tenant_can_NO_LONGER_write_another_tenants_LLM_credential(self, client: TestClient) -> None:
        """The sharpest one, and the reason this was urgent.

        `configure_tenant_llm` takes a `base_url` alongside the key, so this was never merely
        credential destruction -- it redirected tenant B's extraction traffic to an
        attacker-chosen endpoint on B's next dream cycle.
        """
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call(
            "configure_tenant_llm",
            tenant_id="tenant-bravo",
            provider="openai",
            api_key="sk-attacker-controlled",  # pragma: allowlist secret
            base_url="https://attacker.example/v1",
        )
        assert result.get("isError"), "tenant-alpha rewrote tenant-bravo's LLM credential and base_url"

    def test_but_a_tenant_can_STILL_configure_ITS_OWN_credential(self, client: TestClient) -> None:
        """THE POSITIVE CONTROL. A guard that refused every tenant_id would pass the test
        above while breaking the feature outright."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call(
            "configure_tenant_llm",
            tenant_id="tenant-alpha",
            provider="openai",
            api_key="sk-alphas-own-key",  # pragma: allowlist secret
        )
        assert not result.get("isError"), f"alpha was refused its OWN tenant: {result}"

    def test_a_tenant_can_NO_LONGER_delete_another_tenants_credential(self, client: TestClient) -> None:
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        assert session.call("clear_tenant_llm", tenant_id="tenant-bravo").get("isError")

    def test_a_tenant_can_NO_LONGER_read_another_tenants_credential_status(self, client: TestClient) -> None:
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        assert session.call("tenant_llm_status", tenant_id="tenant-bravo").get("isError")

    def test_a_tenant_can_STILL_read_its_OWN_credential_status(self, client: TestClient) -> None:
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        assert not session.call("tenant_llm_status", tenant_id="tenant-alpha").get("isError")

    @pytest.mark.parametrize("tool", ["dream_history", "dream_decisions", "remediate_coherence"])
    def test_a_USER_can_no_longer_read_fleet_wide(self, client: TestClient, tool: str) -> None:
        """These three return rows from every tenant and take no scope to filter on."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        assert session.call(tool).get("isError"), f"{tool} still served every scope to a non-admin"

    @pytest.mark.parametrize("tool", ["dream_history", "dream_decisions", "remediate_coherence"])
    def test_but_an_ADMIN_still_can(self, client: TestClient, tool: str) -> None:
        """THE POSITIVE CONTROL for the role check. Without it, "user refused" cannot be
        told apart from "the tool raises for everybody"."""
        session = _Session(client, ADMIN_KEY)
        session.initialize()
        result = session.call(tool)
        assert not result.get("isError"), f"{tool} refused an ADMIN: {result}"

    # -- B3: the scoped variant both fleet-wide guards said was owed ----------------
    #
    # The two parametrized tests above are the fleet-wide case and still hold. These are
    # the case that did not exist before: `remediate_coherence` now takes a scope, so the
    # question stops being "may this caller act on EVERY tenant" -- which only ADMIN can
    # answer yes to -- and becomes the ordinary one this surface asks everywhere else.

    def test_a_scoped_sweep_touches_ONE_scope_while_the_fleet_sweep_touches_MORE(self, client: TestClient) -> None:
        """THE POINT OF B3, and the only assertion here that `not isError` cannot make.

        Before this, a tenant's own operator had no way to run a coherence sweep on their
        own memory: the tool took no scope, so the only guard available was the fleet-wide
        one and the only answer for a non-ADMIN was refusal. That is why
        `require_fleet_wide_read`'s docstring says "narrow first, widen later with a scoped
        variant" -- this is the widening, and it is narrower than what it replaces.

        **Asserting the scope was BOUND, not merely accepted.** A version of this test that
        checked only `not isError` would pass just as happily if `scopes=` were dropped on
        the way to the client and the sweep quietly went fleet-wide -- the tool would still
        return a report and still not error. So it seeds two scopes first, then requires
        `scope_count` to tell them apart: 1 for the bounded call, more for the fleet sweep.
        The seeding is what stops `== 1` being vacuously true.
        """
        alpha = _Session(client, ALPHA_KEY)
        alpha.initialize()
        alpha.add_memory("tenant-alpha")
        bravo = _Session(client, BRAVO_KEY)
        bravo.initialize()
        bravo.add_memory("tenant-bravo")

        scoped_raw = alpha.call("remediate_coherence", scope_kind="tenant", scope_id="tenant-alpha")
        assert not scoped_raw.get("isError"), f"alpha was refused its OWN scope: {scoped_raw}"
        scoped = json.loads(scoped_raw["content"][0]["text"])

        admin = _Session(client, ADMIN_KEY)
        admin.initialize()
        fleet_raw = admin.call("remediate_coherence")
        assert not fleet_raw.get("isError"), f"admin was refused the fleet sweep: {fleet_raw}"
        fleet = json.loads(fleet_raw["content"][0]["text"])

        assert scoped["scope_count"] == 1, f"the bounded call swept {scoped['scope_count']} scopes"
        assert fleet["scope_count"] > scoped["scope_count"], (
            f"the fleet sweep saw {fleet['scope_count']} scopes and the bounded one "
            f"{scoped['scope_count']} -- with both seeded, these must differ, or the "
            "bounded call is not actually bounded"
        )

    def test_a_USER_can_APPLY_to_its_own_scope_not_just_preview(self, client: TestClient) -> None:
        """`dry_run=False` is the arm that matters. A scoped write is the same shape as
        every other scoped write here -- one named scope, checked against the principal's
        allowlist -- so gating it on ADMIN would be stricter than `add_memory`."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call(
            "remediate_coherence",
            scope_kind="tenant",
            scope_id="tenant-alpha",
            dry_run=False,
        )
        assert not result.get("isError"), f"alpha was refused a write to its OWN scope: {result}"

    def test_a_USER_still_cannot_remediate_ANOTHER_tenants_scope(self, client: TestClient) -> None:
        """The scoped path must not become a way around the boundary. Naming a scope you
        may not name is refused by `authorize_scope`, exactly as it is for `add_memory`."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        result = session.call(
            "remediate_coherence",
            scope_kind="tenant",
            scope_id="tenant-bravo",
            dry_run=False,
        )
        assert result.get("isError"), "alpha remediated tenant-bravo's scope"

    def test_a_HALF_SPECIFIED_scope_falls_through_to_the_fleet_wide_rule(self, client: TestClient) -> None:
        """`scope_kind` without `scope_id` is not a scope, and the branch reads
        `if scope_kind and scope_id`. It must land on the fleet-wide guard rather than
        silently sweeping everything -- an omission dressed as a scope is the shape
        `_OPTIONAL_SCOPE_TOOLS` exists to catch."""
        session = _Session(client, ALPHA_KEY)
        session.initialize()
        assert session.call("remediate_coherence", scope_kind="tenant").get("isError")
        assert session.call("remediate_coherence", scope_id="tenant-alpha").get("isError")


class TestNoExemptToolIsLeftUnguarded:
    """DERIVED, not hand-listed. The point of failure last time was a list going stale.

    `_CROSS_SCOPE_TOOLS` is the registry of tools that bypass `_scope`. Every member must
    now reach one of the two replacement guards. A seventh exempt tool added tomorrow fails
    this test on the day it is written, rather than being discovered by an audit later.
    """

    def test_every_cross_scope_tool_calls_one_of_the_two_guards(self) -> None:
        import ast
        import pathlib

        from memotron.mcp_server import _CROSS_SCOPE_TOOLS

        tree = ast.parse(pathlib.Path(mcp_server.__file__).read_text())
        guarded: dict[str, set[str]] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name in _CROSS_SCOPE_TOOLS:
                guarded[node.name] = {
                    call.func.id
                    for call in ast.walk(node)
                    if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                }

        assert set(guarded) == set(_CROSS_SCOPE_TOOLS), (
            f"tools in the registry with no function found: {set(_CROSS_SCOPE_TOOLS) - set(guarded)}"
        )
        unguarded = {
            name
            for name, calls in guarded.items()
            if not calls & {"authorize_tenant", "require_fleet_wide_read", "authorize_scope"}
        }
        assert not unguarded, f"these bypass _scope AND reach no replacement guard: {sorted(unguarded)}"

    def test_the_registry_is_not_empty_so_this_cannot_pass_vacuously(self) -> None:
        from memotron.mcp_server import _CROSS_SCOPE_TOOLS

        # 5 since B3, which gave `remediate_coherence` a scope and moved it to
        # `_OPTIONAL_SCOPE_TOOLS`. Shrinking is the direction this set is supposed to move;
        # the number is pinned so it cannot GROW unnoticed. `test_mcp_scope_guard.py` pins
        # the membership -- this only needs it to be non-empty, so the class above cannot
        # pass by iterating nothing.
        assert len(_CROSS_SCOPE_TOOLS) == 5, f"the exempt set changed: {sorted(_CROSS_SCOPE_TOOLS)}"
