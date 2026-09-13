"""What a principal permits, and what happens when there isn't one.

#126. `_scope` is the choke point 33 of 39 MCP tools pass through, so one authorization
call there covers all of them. These tests exercise that decision with no network, no
gateway and no server: a real `RequestContext` carrying a real Starlette request, with a
principal on `request.state`, exactly as the middleware will leave it.

The request is built from an actual ASGI scope rather than a stand-in object with the right
attribute names. That matters more than it looks: the reason the principal lives on
`request.state` at all is that `scope["state"]` is the one container shared across the
anyio task boundary between middleware and tool body
(`streamable_http_manager.py:227-229`), and a hand-rolled fake would happily model a
mechanism that does not exist. It still is not proof that the middleware's write is visible
to the tool -- only a run through the real ASGI app is -- which is why that test exists
separately and why this docstring says so instead of implying coverage it lacks.

Since the FastMCP 4 move the request is entered through `set_http_request`, the very
context manager `RequestContextMiddleware` uses in production, so one more piece of this
setup is the real mechanism rather than a model of it.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import pytest
from fastmcp.server.http import set_http_request
from starlette.requests import Request

import memotron.mcp_server as mcp_server_module
from memotron.config import MemoryPrincipal, PrincipalRole
from memotron.mcp_auth import (
    REQUIRE_IDENTITY_ENV,
    IdentityUnavailableError,
    ScopeNotAuthorizedError,
    authorize_scope,
    authorize_tenant,
    current_principal,
    require_explicit_scope,
    require_fleet_wide_read,
    require_fleet_wide_write,
)
from memotron.mcp_server import _scope
from memotron.models import MemoryScope, ScopeKind


@contextlib.contextmanager
def _in_request(principal: MemoryPrincipal | None) -> Iterator[Request]:
    """Enter an MCP request whose Starlette request carries `principal` (or nothing)."""
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": [],
        "query_string": b"",
        "state": {},
    }
    request = Request(scope)
    if principal is not None:
        request.state.dw_principal = principal
    # `set_http_request` is the SAME context manager `RequestContextMiddleware` uses on
    # every real request (`fastmcp/server/http.py:354`), so this enters the request the
    # way the server does rather than reconstructing a low-level `RequestContext` by
    # hand -- which is what this file used to do, and what its docstring warned could
    # model a mechanism that does not exist.
    with set_http_request(request):
        yield request


def _principal(
    *,
    allowed: set[str] | None = None,
    default_scope: str | None = None,
    role: PrincipalRole = PrincipalRole.USER,
) -> MemoryPrincipal:
    return MemoryPrincipal(
        principal_id="p-alpha",
        tenant_id="tenant-alpha",
        allowed_scope_keys=allowed or {"tenant:tenant-alpha"},
        default_scope=MemoryScope.from_key(default_scope) if default_scope else None,
        role=role,
    )


@pytest.fixture
def guard_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(REQUIRE_IDENTITY_ENV, "1")


@pytest.fixture(autouse=True)
def guard_off_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test inherits a flag from the ambient environment, in either direction."""
    monkeypatch.delenv(REQUIRE_IDENTITY_ENV, raising=False)


class TestTheFlagOffChangesNothing:
    """The rollout default. Every assertion here is the current production behaviour."""

    def test_scope_resolves_with_no_principal_at_all(self) -> None:
        scope = _scope("tenant", "any-tenant-you-like")
        assert scope.kind is ScopeKind.TENANT
        assert scope.scope_id == "any-tenant-you-like"

    def test_a_scope_outside_the_principals_allowlist_still_resolves(self) -> None:
        """Flag off means the allowlist is not consulted -- not that it is consulted and
        passes. Asserting the refusal is absent is the only way to tell those apart."""
        with _in_request(_principal(allowed={"tenant:tenant-alpha"})):
            assert _scope("tenant", "tenant-bravo").scope_id == "tenant-bravo"

    def test_a_fleet_wide_call_is_not_refused(self) -> None:
        require_explicit_scope("run_due_dreams")  # must not raise


class TestTheFlagOnAuthorizes:
    def test_a_scope_on_the_allowlist_resolves(self, guard_on: None) -> None:
        """THE POSITIVE CONTROL. A guard that refuses everything passes every negative
        test in this file and is useless; this is what distinguishes working from broken."""
        with _in_request(_principal(allowed={"tenant:tenant-alpha"})):
            assert _scope("tenant", "tenant-alpha").key == "tenant:tenant-alpha"

    def test_BOTH_of_a_two_scope_principals_scopes_resolve(self, guard_on: None) -> None:
        """`allowed_scope_keys` is a set and the CLI takes a repeatable
        `--allowed-scope-key`, so naming several scopes is legitimate. A guard that let
        through only the first would pass a single-scope test."""
        principal = _principal(allowed={"tenant:tenant-alpha", "agent:agent-seven"})
        with _in_request(principal):
            assert _scope("tenant", "tenant-alpha").key == "tenant:tenant-alpha"
            assert _scope("agent", "agent-seven").key == "agent:agent-seven"

    def test_the_default_scope_counts_as_allowed(self, guard_on: None) -> None:
        """`effective_allowed_scope_keys` folds `default_scope` in. A principal carrying
        only a default and an empty allowlist is a normal row, and refusing it would lock
        out the common case."""
        principal = _principal(allowed=set(), default_scope="tenant:tenant-alpha")
        with _in_request(principal):
            assert _scope("tenant", "tenant-alpha").key == "tenant:tenant-alpha"

    def test_another_tenants_scope_is_refused_and_names_it(self, guard_on: None) -> None:
        with (
            _in_request(_principal(allowed={"tenant:tenant-alpha"})),
            pytest.raises(ScopeNotAuthorizedError) as excinfo,
        ):
            _scope("tenant", "tenant-bravo")
        message = str(excinfo.value)
        assert "tenant:tenant-bravo" in message, "the refusal must name the scope refused"
        assert "p-alpha" in message

    def test_an_ADMIN_principal_gets_NO_bypass(self, guard_on: None) -> None:
        """`config/_control_plane.py:297` lets admins skip the allowlist entirely. That
        bypass is out of scope for this slice, and deferring a decision about it is not a
        reason to reproduce it in new code. If a future change adds an admin path here it
        should have to delete this test."""
        principal = _principal(allowed={"tenant:tenant-alpha"}, role=PrincipalRole.ADMIN)
        with _in_request(principal), pytest.raises(ScopeNotAuthorizedError):
            _scope("tenant", "tenant-bravo")


class TestTheGuardFailsClosedOnItsOwnAbsence:
    def test_no_principal_under_the_flag_raises(self, guard_on: None) -> None:
        """A missing principal is a deployment fault, not an anonymous caller. Without this
        branch the flag would authenticate at the door and authorize nothing -- the worst
        of the three states, because it looks armed."""
        with _in_request(None), pytest.raises(IdentityUnavailableError):
            _scope("tenant", "tenant-alpha")

    def test_no_REQUEST_at_all_under_the_flag_raises(self, guard_on: None) -> None:
        """stdio transport, or a tool called directly. Same reasoning, different absence."""
        with pytest.raises(IdentityUnavailableError):
            _scope("tenant", "tenant-alpha")

    def test_the_two_failures_are_distinguishable(self, guard_on: None) -> None:
        """`IdentityUnavailableError` is ours and `ScopeNotAuthorizedError` is the caller's. One
        exception type for both would file our own outage as somebody else's misuse."""
        assert not issubclass(IdentityUnavailableError, ScopeNotAuthorizedError)
        assert not issubclass(ScopeNotAuthorizedError, IdentityUnavailableError)
        assert issubclass(IdentityUnavailableError, PermissionError)
        assert issubclass(ScopeNotAuthorizedError, PermissionError)


class TestFleetWideCallsNeedAnExplicitScope:
    def test_omitting_the_scope_is_refused_under_the_flag(self, guard_on: None) -> None:
        with _in_request(_principal()), pytest.raises(ScopeNotAuthorizedError) as excinfo:
            require_explicit_scope("run_due_dreams")
        assert "run_due_dreams" in str(excinfo.value)

    def test_naming_an_allowed_scope_still_works(self, guard_on: None) -> None:
        """The positive half: the guard restricts these tools, it does not disable them."""
        with _in_request(_principal(allowed={"tenant:tenant-alpha"})):
            assert _scope("tenant", "tenant-alpha").key == "tenant:tenant-alpha"


class TestThePrincipalLookupItself:
    def test_it_reads_the_principal_off_request_state(self) -> None:
        with _in_request(_principal()):
            found = current_principal()
        assert found is not None and found.principal_id == "p-alpha"

    def test_it_survives_the_state_being_the_shared_scope_dict(self) -> None:
        """The mechanism, asserted directly: `request.state` writes into `scope["state"]`,
        which is what crosses the task boundary. A second `Request` over the same scope
        sees the write -- if this ever stops holding, the middleware silently stops
        reaching the tool and every guarded call becomes `IdentityUnavailableError`."""
        with _in_request(_principal()) as request:
            assert Request(request.scope).state.dw_principal.principal_id == "p-alpha"

    def test_a_non_principal_on_that_attribute_reads_as_absent(self) -> None:
        """Anything can write to `scope["state"]`; only a `MemoryPrincipal` is identity.
        Duck-typing here would let an unrelated middleware's object become an authorization
        input."""
        with _in_request(None) as request:
            request.state.dw_principal = {"principal_id": "p-alpha", "tenant_id": "t"}
            assert current_principal() is None

    def test_outside_a_request_it_is_None_not_an_exception(self) -> None:
        assert current_principal() is None


class TestTheFlagIsNarrow:
    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " true "])
    def test_truthy_values(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        monkeypatch.setenv(REQUIRE_IDENTITY_ENV, value)
        with pytest.raises(IdentityUnavailableError):
            authorize_scope(MemoryScope.from_key("tenant:tenant-alpha"))

    @pytest.mark.parametrize("value", ["", "0", "false", "no", "on", "yes please", "enabled"])
    def test_everything_else_is_off(self, monkeypatch: pytest.MonkeyPatch, value: str) -> None:
        """`on` and `enabled` are FALSE here, matching `_env_flag` elsewhere in the repo.
        A flag that accepts any non-empty string turns a typo into a silent security change
        -- and on this flag the silent change is in the permissive direction."""
        monkeypatch.setenv(REQUIRE_IDENTITY_ENV, value)
        authorize_scope(MemoryScope.from_key("tenant:tenant-alpha"))  # must not raise


class TestFleetWideCallsFailClosedOnTheGuardsOwnAbsence:
    def test_no_principal_under_the_flag_raises_IdentityUnavailable(self, guard_on: None) -> None:
        """`authorize_scope`'s twin branch was covered and this one was not.

        The coverage floor for `mcp_auth` is justified by the claim that "every uncovered
        line in this module is a branch that decides whether an unauthenticated or
        wrong-tenant caller is refused" -- a justification that only holds if such branches
        are in fact covered. This one decides it for the three fleet-wide tools.
        """
        with _in_request(None), pytest.raises(IdentityUnavailableError):
            require_explicit_scope("run_due_dreams")

    def test_outside_a_request_entirely_it_also_raises(self, guard_on: None) -> None:
        with pytest.raises(IdentityUnavailableError):
            require_explicit_scope("coherence_incidents")


class TestTheTwoGuardsThatReplacedTheCrossScopeExemption:
    """`authorize_tenant` and `require_fleet_wide_read` — the six tools that never reach `_scope`.

    A tenant credential is not addressed by a `MemoryScope`, and the fleet-wide readers take
    no scope at all, so neither could be covered by widening `authorize_scope`. These pin the
    branches the transport-level tests in `test_mcp_identity_middleware.py` cannot reach:
    the flag-off passthrough, and the deployment fault where the middleware did not run.
    """

    def test_with_the_flag_OFF_authorize_tenant_permits_any_tenant(self) -> None:
        """The rollout default: byte-for-byte the previous behaviour."""
        authorize_tenant("someone-elses-tenant", operation="configure_tenant_llm")

    def test_with_the_flag_OFF_fleet_wide_reads_are_permitted(self) -> None:
        require_fleet_wide_read("dream_history")

    def test_authorize_tenant_permits_the_principals_OWN_tenant(self, guard_on: None) -> None:
        with _in_request(_principal()):
            authorize_tenant("tenant-alpha", operation="configure_tenant_llm")

    def test_authorize_tenant_refuses_another_tenant(self, guard_on: None) -> None:
        with _in_request(_principal()), pytest.raises(ScopeNotAuthorizedError, match="tenant-bravo"):
            authorize_tenant("tenant-bravo", operation="configure_tenant_llm")

    def test_authorize_tenant_refuses_an_EMPTY_tenant_id(self, guard_on: None) -> None:
        """Omission must not be the widening move. An empty string is not "my own tenant"."""
        with _in_request(_principal()), pytest.raises(ScopeNotAuthorizedError, match="explicit tenant_id"):
            authorize_tenant("   ", operation="clear_tenant_llm")

    def test_authorize_tenant_fails_closed_when_the_middleware_did_not_run(self, guard_on: None) -> None:
        """A missing principal under the flag is a deployment fault, not an anonymous caller."""
        with _in_request(None), pytest.raises(IdentityUnavailableError):
            authorize_tenant("tenant-alpha", operation="configure_tenant_llm")

    def test_fleet_wide_read_fails_closed_when_the_middleware_did_not_run(self, guard_on: None) -> None:
        with _in_request(None), pytest.raises(IdentityUnavailableError):
            require_fleet_wide_read("dream_history")

    def test_fleet_wide_read_refuses_a_USER(self, guard_on: None) -> None:
        with _in_request(_principal()), pytest.raises(ScopeNotAuthorizedError, match="admin"):
            require_fleet_wide_read("dream_decisions")

    def test_fleet_wide_read_permits_an_ADMIN(self, guard_on: None) -> None:
        """The positive control: without it, "user refused" cannot be told apart from
        "raises for everybody"."""
        with _in_request(_principal(role=PrincipalRole.ADMIN)):
            require_fleet_wide_read("dream_decisions")

    def test_authorize_tenant_has_NO_admin_exemption(self, guard_on: None) -> None:
        """An admin of tenant A still has no business rewriting tenant B's credential.

        `config/_control_plane.py`'s ADMIN bypass is a known finding, not a pattern to copy —
        and `authorize_scope` deliberately does not replicate it either.
        """
        with _in_request(_principal(role=PrincipalRole.ADMIN)), pytest.raises(ScopeNotAuthorizedError):
            authorize_tenant("tenant-bravo", operation="configure_tenant_llm")


class TestAFleetWideWriteDoesNotGetTheFlagOffPassthrough:
    """`remediate_coherence(dry_run=False)` writes to EVERY scope in the store.

    `require_fleet_wide_read` opens with `if not identity_required(): return`, which is
    defensible for a read — a store with no identity configured should still let its own
    operator tooling look at `dream_history`. Applying the same escape hatch to a
    destructive fleet-wide write is not, and it was not theoretical:

    * `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is rendered ONLY on `latest`
      (`requireGatewayIdentity: true`); stage, prod, preview and load inherit `false`
    * the MCP container runs in EVERY environment, and the deployed entry point
      (`examples/mcp_server.py`) imports the same `mcp` object, so it serves this tool
    * `prod` carries a real operational store

    Measured 2026-09-08. Arming the flag on those environments is not a substitute:
    doing it with an empty `key_principals` table refuses every caller, so rows must be
    bound in-cluster first (see the note #183 left in `values-latest.yaml`).
    """

    def test_with_the_flag_OFF_a_fleet_wide_WRITE_is_refused(self) -> None:
        """The fix. Contrast with `test_with_the_flag_OFF_fleet_wide_reads_are_permitted`
        immediately above — same flag state, deliberately different answer."""
        with pytest.raises(IdentityUnavailableError, match="every scope"):
            require_fleet_wide_write("remediate_coherence")

    def test_with_the_flag_OFF_the_READ_is_still_permitted(self) -> None:
        """The control. Without this, the test above passes just as well if the guard
        were changed to refuse everything, which would take `dream_history` down on
        every environment that has no identity armed."""
        require_fleet_wide_read("remediate_coherence")

    def test_the_refusal_names_the_remedy_and_the_flag(self) -> None:
        """A refusal an operator cannot act on sends them to the source. It must name
        the preview that still works AND the prerequisite that makes arming the flag
        safe, because arming it blind is its own outage."""
        with pytest.raises(IdentityUnavailableError) as excinfo:
            require_fleet_wide_write("remediate_coherence")
        message = str(excinfo.value)
        assert "dry_run=True" in message, f"no remedy offered: {message}"
        assert REQUIRE_IDENTITY_ENV in message, f"does not name the flag: {message}"
        assert "key_principals" in message, (
            f"does not warn that arming the flag without rows refuses every caller: {message}"
        )

    def test_with_the_flag_ON_an_ADMIN_may_write(self, guard_on: None) -> None:
        with _in_request(_principal(role=PrincipalRole.ADMIN)):
            require_fleet_wide_write("remediate_coherence")

    def test_with_the_flag_ON_a_non_admin_is_refused(self, guard_on: None) -> None:
        """Once identity IS armed this must behave exactly like the read guard — the
        write rule is strictly stronger, never a different rule."""
        with _in_request(_principal(role=PrincipalRole.USER)), pytest.raises(ScopeNotAuthorizedError):
            require_fleet_wide_write("remediate_coherence")

    def test_with_the_flag_ON_a_missing_principal_fails_closed(self, guard_on: None) -> None:
        """A missing principal under the flag is a deployment fault, not an anonymous
        caller — same as every other guard in this module."""
        with _in_request(None), pytest.raises(IdentityUnavailableError):
            require_fleet_wide_write("remediate_coherence")


class TestTheToolActuallyCallsTheWriteGuard:
    """The guard tests above prove the RULE; these prove the WIRING.

    Without them, deleting `require_fleet_wide_write("remediate_coherence")` from
    `mcp_server.remediate_coherence` leaves every test in this file green — the rule
    would be correct and unreachable, which is the failure mode this whole module
    exists to prevent.
    """

    def test_the_APPLY_path_refuses_when_identity_is_disarmed(self) -> None:
        """Drives the real tool, not the guard in isolation."""
        with pytest.raises(IdentityUnavailableError, match="every scope"):
            asyncio.run(mcp_server_module.remediate_coherence(dry_run=False))

    def test_the_APPLY_path_never_reaches_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Refusing after the write has begun would be worse than not refusing at all.

        The stub raises if it is reached, so this fails if the guard is ever moved
        below the client call.
        """

        class _ReachedTheClient(Exception):
            pass

        class _Stub:
            async def remediate_coherence(self, **_: object) -> object:
                raise _ReachedTheClient

        monkeypatch.setattr(mcp_server_module, "get_client", lambda: _Stub())
        with pytest.raises(IdentityUnavailableError):
            asyncio.run(mcp_server_module.remediate_coherence(dry_run=False))

    def test_the_PREVIEW_path_is_unaffected_and_reaches_the_client(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The control, at the tool level. `dry_run=True` must still work with identity
        disarmed — that is the whole reason the write guard is conditional rather than
        applied to the tool as a whole."""

        class _ReachedTheClient(Exception):
            pass

        class _Stub:
            async def remediate_coherence(self, **_: object) -> object:
                raise _ReachedTheClient

        monkeypatch.setattr(mcp_server_module, "get_client", lambda: _Stub())
        with pytest.raises(_ReachedTheClient):
            asyncio.run(mcp_server_module.remediate_coherence(dry_run=True))
