"""Per-request identity for the MCP surfaces: read the principal, authorize the scope.

#126. Before this module the MCP server had no identity concept at all. Tools take
``scope_kind``/``scope_id`` as plain strings and hand them straight to ``MemoryScope``, so
any caller that reaches the ingress can read or write **any tenant's** memory by naming it.
Measured on ``origin/main``: zero credential reads
(``Authorization|Bearer|x-api-key|x-litellm|headers[``) anywhere in ``mcp_server.py``,
``agent_memory_mcp.py`` or ``admin_server/__init__.py``. The only thing standing between the
store and an arbitrary tenant's data was ``ingress.class: gce-internal``.

This module holds the two halves that are independent of *how* the caller is identified, so
that the authorization decision can be tested without a gateway, a network, or a server:

* :func:`current_principal` -- where a principal lives on a request, and how a tool body
  reaches it;
* :func:`authorize_scope` / :func:`require_explicit_scope` -- what a principal permits.

The authentication half (resolving a JedAI Gateway virtual key to a ``key_principals`` row)
is deliberately *not* here. It is a network call with its own failure taxonomy and it is
what makes this surface depend on the gateway being up.

Where the principal lives
-------------------------
On ``request.state``, **not** in a contextvar, and that is not a stylistic choice. MCP
dispatch runs the tool body in a different anyio task from the ASGI middleware
(``streamable_http_manager.py:227-229``), so a contextvar set in middleware reads back as
its default inside the tool. ``request.state`` is backed by the shared ``scope["state"]``
dict (``starlette/requests.py:189-196``) and does survive the hop. Getting this wrong does
not raise -- it reads ``None``, which without :func:`identity_required`'s fail-closed branch
would authenticate at the door and then authorize nothing.

Fail-closed, including against ourselves
----------------------------------------
When ``MEMOTRON_REQUIRE_GATEWAY_IDENTITY`` is on and no principal is present, every
guarded call **raises**. A missing principal there is not an anonymous caller to be granted
the benefit of the doubt; it means the middleware did not run -- wrong entry point, stdio
transport, a refactor that dropped the wrapper -- and the safe reading of "the component
that assigns identity is absent" is not "allow".

The flag defaults **off**, so with it unset this module is byte-for-byte a no-op and the
server behaves exactly as it does today. That default is itself the defect #126 exists to
correct; it is a rollout order, not a resting place. Turning it on before rows exist in
``key_principals`` is a total outage, so the order is: bind rows, flip the flag as its own
reversible commit, run the probe.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Sequence
from typing import Any

from anyio import CapacityLimiter, to_thread

from memotron.config import MemoryPrincipal, PrincipalRole

# Re-exported for the same reason as `FORWARDED_KEY_HEADER` below: both were part of this
# module's importable surface before the resolver moved to infra (#50), neither is read
# here any more, and dropping them would be a REMOVAL for nothing. `api_removals.sh` is
# the gate that would report it; keeping them makes this commit additions-only.
from memotron.config import (
    principal_from_registry_row as principal_from_registry_row,
)
from memotron.gateway import (
    GatewayRequestError as GatewayRequestError,
)

# Re-exported, not merely imported: nothing in this module reads it any more now that the
# header PRECEDENCE lives in `gateway_identity` (#50), but it has been part of this
# module's public surface and dropping it would be a removal `api_removals.sh` reports for
# no reason.
from memotron.gateway_identity import (
    FORWARDED_KEY_HEADER as FORWARDED_KEY_HEADER,
)
from memotron.gateway_identity import (
    GatewayIdentity,
    GatewayIdentityRefusalError,
    GatewayPrincipalResolver,
    IdentityCache,
    gateway_key_from_headers,
    key_fingerprint,
)
from memotron.identity import (
    REQUIRE_IDENTITY_ENV as REQUIRE_IDENTITY_ENV,
)
from memotron.identity import (
    IdentityUnavailableError as IdentityUnavailableError,
)
from memotron.identity import (
    ScopeNotAuthorizedError as ScopeNotAuthorizedError,
)
from memotron.identity import (
    identity_required as identity_required,
)
from memotron.models import MemoryScope

_log = logging.getLogger(__name__)

#: Concurrency cap for the gateway self-lookup, SEPARATE from anyio's process-wide default.
#:
#: That default is a ``CapacityLimiter(40)`` shared by every ``anyio.to_thread`` user in the
#: process. Without a private limiter, an unauthenticated caller -- a non-empty header value
#: is the only requirement -- can open 40 concurrent requests bearing distinct junk keys and
#: occupy every slot. Failures are never cached (deliberately: an outage must not pin real
#: callers to a refusal), so each junk key is a fresh lookup, and during a gateway brownout
#: each holds its slot for the full retry budget. Everything else in the process that wants
#: a worker thread then queues behind it.
#:
#: The failure is quiet in the way this repo has already paid for once: ``/health`` skips
#: this middleware entirely, so probes stay 200 and the load balancer reports the backend
#: healthy while the API is wedged.
#:
#: 8 is a ceiling on the damage, not a throughput target -- cache hits never reach here, so
#: this bounds only concurrent MISSES.
_GATEWAY_LOOKUP_LIMITER = CapacityLimiter(8)

#: Attribute name on ``request.state`` carrying the resolved principal. Named with a
#: ``dw_`` prefix because ``scope["state"]`` is shared with every other middleware in the
#: stack and a generic ``principal`` would be a collision waiting to happen.
PRINCIPAL_STATE_ATTR = "dw_principal"


# REQUIRE_IDENTITY_ENV, ScopeNotAuthorizedError, IdentityUnavailableError and
# identity_required MOVED TO `memotron.identity` (core) in #206 Phase 2, and are
# re-exported here so this module's public surface is unchanged.
#
# Why: the agent-memory platform needs them, and `application agent_memory -> delivery
# mcp_auth` is an upward tier edge that `coupling_report.py` fails on. Relocated rather
# than baselined, following the SZ-2 precedent in that file. None of the four touches a
# transport -- two exception types, a constant, and an env-var read -- whereas the ASGI
# middleware and the in-flight request lookup that make this module delivery-tier both
# stay here.


def current_principal() -> MemoryPrincipal | None:
    """The principal attached to the in-flight MCP request, or ``None``.

    ``None`` means "there is no HTTP request here" -- stdio transport, a direct call to a
    tool function in a test, a lifespan hook -- and is **never** interpreted as permission.
    `authorize_tenant` raises `IdentityUnavailableError` on ``None``, so an unauthenticated
    caller is refused rather than defaulted. That is the only reason this may return
    ``None`` at all instead of raising.

    The lookup used to walk three layers of the low-level SDK by hand
    (``request_ctx.get()`` -> ``.request`` -> ``.state``), each wrapped defensively because
    each was a different library's private-ish surface. FastMCP 4 publishes
    `get_http_request` as the supported accessor for exactly this, and it already handles
    both context variables including the ``on_initialize`` case the hand-rolled version
    missed. It raises ``RuntimeError`` when there is no HTTP request -- not the
    ``LookupError`` the contextvar used to raise, which is why that except clause changed
    with it.

    ``request.state`` is left as a plain `getattr`: Starlette guarantees the attribute
    exists, but the principal is only on it once `GatewayIdentityMiddleware` has run.
    """
    try:
        from fastmcp.server.dependencies import get_http_request
    except ImportError:  # pragma: no cover - fastmcp is a hard dependency
        return None

    try:
        request = get_http_request()
    except RuntimeError:
        # Not inside an HTTP request: stdio, or a unit test calling the tool directly.
        return None

    principal = getattr(request.state, PRINCIPAL_STATE_ATTR, None)
    return principal if isinstance(principal, MemoryPrincipal) else None


def authorize_scope(scope: MemoryScope) -> None:
    """Raise unless the in-flight principal is allowed to name ``scope``.

    ``effective_allowed_scope_keys`` (``config/_tenancy.py:136``) is the allowlist, and it
    is a *set* -- a principal legitimately names several scopes, which is why the tools keep
    their ``scope_kind``/``scope_id`` arguments. The argument says which scope you mean; the
    principal says which you may name.

    Deliberately does **not** replicate the ADMIN bypass in ``config/_control_plane.py:297``,
    where admins skip the allowlist entirely. Deferring the decision about that bypass does
    not oblige importing it into a new guard; a plain allowlist check for every role is
    strictly the safer of the two and touches no existing behaviour.
    """
    if not identity_required():
        return

    principal = current_principal()
    if principal is None:
        raise IdentityUnavailableError(
            f"{REQUIRE_IDENTITY_ENV} is set but no authenticated principal reached this "
            "call; the identity middleware did not run for this request"
        )

    allowed = principal.effective_allowed_scope_keys()
    if scope.key not in allowed:
        raise ScopeNotAuthorizedError(
            f"scope {scope.key!r} is not one of principal {principal.principal_id!r}'s allowed scopes"
        )


def require_explicit_scope(operation: str) -> None:
    """Raise when a fleet-wide call is attempted under the identity guard.

    Three tools -- ``run_due_dreams``, ``run_dream_job``, ``coherence_incidents`` -- take
    ``scope_kind``/``scope_id`` but default them to ``""`` and treat the empty pair as
    *every scope in the store*. They therefore **declare** a scope while being able to skip
    :func:`authorize_scope` entirely, which is precisely the shape of guard that passes its
    own test while the thing it guards walks around it. Omission is the widening move here,
    so omission is what has to be refused.

    Mirrors ``client/__init__.py:_require_explicit_authorized_scope``, which fails the same
    calls closed on a scope-guarded SDK client. That guard is construction-time on a
    process-wide client and this one is per-request; the duplication is the cost of the
    process-wide client not knowing who is calling it.
    """
    if not identity_required():
        return
    if current_principal() is None:
        raise IdentityUnavailableError(
            f"{REQUIRE_IDENTITY_ENV} is set but no authenticated principal reached this "
            "call; the identity middleware did not run for this request"
        )
    raise ScopeNotAuthorizedError(
        f"{operation} spans every scope in the store when scope_kind/scope_id are omitted; name a scope explicitly"
    )


def authorize_tenant(tenant_id: str, *, operation: str) -> None:
    """Raise unless the in-flight principal owns ``tenant_id``.

    Three tools -- ``configure_tenant_llm``, ``clear_tenant_llm``, ``tenant_llm_status`` --
    take a **caller-named** ``tenant_id`` and never reach :func:`authorize_scope`, because a
    tenant credential is not addressed by a `MemoryScope` at all. Authenticating at the door
    therefore did not authorize them: any principal with a valid gateway key could name any
    tenant.

    ``configure_tenant_llm`` is the sharpest of the three and the reason this is not merely
    a data-integrity concern: it takes a ``base_url`` alongside the key, so naming another
    tenant **redirects that tenant's extraction traffic to a caller-chosen endpoint** on its
    next dream cycle. ``clear_tenant_llm`` deletes their credential outright.

    A scope allowlist cannot express this. `effective_allowed_scope_keys` is a set of scope
    keys; ``tenant_id`` is a different axis, and `MemoryPrincipal.tenant_id` is the only
    thing that speaks to it. Hence a separate guard rather than a wider `authorize_scope`.

    No ADMIN exemption, matching :func:`authorize_scope`: an admin of tenant A still has no
    business rewriting tenant B's credential, and `config/_control_plane.py`'s ADMIN bypass
    is a known finding rather than a pattern to copy.
    """
    if not identity_required():
        return

    principal = current_principal()
    if principal is None:
        raise IdentityUnavailableError(
            f"{REQUIRE_IDENTITY_ENV} is set but no authenticated principal reached this "
            "call; the identity middleware did not run for this request"
        )

    named = tenant_id.strip()
    if not named:
        raise ScopeNotAuthorizedError(f"{operation} requires an explicit tenant_id")
    if named != principal.tenant_id:
        raise ScopeNotAuthorizedError(
            f"{operation} names tenant {named!r}; principal {principal.principal_id!r} "
            f"belongs to tenant {principal.tenant_id!r}"
        )


def require_fleet_wide_read(operation: str) -> None:
    """Raise unless the in-flight principal may read across every scope in the store.

    ``dream_history`` and ``dream_decisions`` are fleet-wide by construction -- they take
    no scope and return rows from every tenant, so "name a scope" is not available as a
    remedy and the only question is *who may do this*.

    ADMIN only. That is deliberately narrower than "any authenticated principal" and
    deliberately not "the tenant's own rows": those two tools have no scope parameter to
    filter on, so a per-tenant answer would require changing what they return.

    **``remediate_coherence`` is no longer in that position.** B3 gave it
    ``scope_kind``/``scope_id``, so it reaches this guard only when the caller OMITS the
    scope; a bounded call goes through :func:`authorize_scope` instead and needs no ADMIN
    role. That was the "widen later with a scoped variant" this docstring used to promise,
    and it is why the tool now sits in ``_OPTIONAL_SCOPE_TOOLS`` rather than
    ``_CROSS_SCOPE_TOOLS``. The remaining two still have no scoped variant.

    NOTE the asymmetry with :func:`authorize_scope`, which deliberately does not honour an
    ADMIN bypass. There, ADMIN would *widen*; here it is the *restriction*. Same principle
    both times -- the role must never be the reason a check is skipped.
    """
    if not identity_required():
        return

    principal = current_principal()
    if principal is None:
        raise IdentityUnavailableError(
            f"{REQUIRE_IDENTITY_ENV} is set but no authenticated principal reached this "
            "call; the identity middleware did not run for this request"
        )
    if principal.role is not PrincipalRole.ADMIN:
        raise ScopeNotAuthorizedError(
            f"{operation} reads every scope in the store and is restricted to role "
            f"{PrincipalRole.ADMIN.value!r}; principal {principal.principal_id!r} has role "
            f"{principal.role.value!r}"
        )


def require_fleet_wide_write(operation: str) -> None:
    """Like :func:`require_fleet_wide_read`, but it does NOT wave the call through when
    identity is disarmed.

    That single difference is the whole point. ``require_fleet_wide_read`` opens with
    ``if not identity_required(): return``, which is defensible for a READ -- a store
    with no identity configured should still let its own operator tooling look at
    ``dream_history``. Applying the same escape hatch to a DESTRUCTIVE FLEET-WIDE WRITE
    is not: ``remediate_coherence(dry_run=False, retire_held_directives=True)``
    soft-retires memories in EVERY scope in the store, and running it for an unknown
    caller is not a lesser version of running it for an authorized one.

    Measured 2026-09-08, which is why this exists:

    * ``MEMOTRON_REQUIRE_GATEWAY_IDENTITY`` is rendered ONLY on ``latest``
      (``requireGatewayIdentity: true``). ``stage``, ``prod``, ``preview`` and ``load``
      inherit ``false``, so the guard returned immediately there.
    * the MCP container runs in EVERY environment, and ``examples/mcp_server.py`` --
      the deployed entry point -- does ``from memotron.mcp_server import ... mcp``,
      so it serves all 39 tools including this one. The exposure was reachable, not
      theoretical.
    * ``prod`` carries a real operational store (``operationalStore.enabled: true``).

    Arming the flag on those environments is NOT a substitute and NOT a quick fix:
    turning it on with an empty ``key_principals`` table refuses every caller, so it
    needs rows bound in-cluster first (see the note #183 left in
    ``values-latest.yaml``). This guard closes the destructive path meanwhile, and
    keeps working afterwards -- once identity IS armed, the ADMIN rule below applies
    exactly as it does for reads.

    The read-only preview (``dry_run=True``, the default) deliberately still runs under
    :func:`require_fleet_wide_read`, so an UNSCOPED preview remains fleet-wide disclosure
    where identity is disarmed.

    **That is now avoidable rather than forced.** This docstring used to say narrowing it
    "needs the tool to grow a scope parameter, which is a larger change"; B3 grew it. A
    caller that names a scope never reaches either fleet-wide guard, and gets the ordinary
    :func:`authorize_scope` check instead. What is left here is the genuinely unbounded
    case, which no per-scope check can answer.
    """
    if not identity_required():
        raise IdentityUnavailableError(
            f"{operation} writes to every scope in the store, and this deployment cannot "
            f"identify its callers: {REQUIRE_IDENTITY_ENV} is not set. Refusing rather "
            "than applying a fleet-wide change for an unknown caller. Run it with "
            "dry_run=True to preview, or arm the identity guard -- which requires "
            "key_principals rows to exist first, or every caller is refused."
        )
    require_fleet_wide_read(operation)


# ---------------------------------------------------------------------------
# Authentication: the ASGI middleware that puts a principal on the request
# ---------------------------------------------------------------------------


#: Paths that never carry identity. `/health` is here because Kubernetes probes it with no
#: credential: 401 the liveness probe and the pod restarts in a loop, which presents as a
#: crashing service rather than an auth misconfiguration and costs a long time to read
#: correctly. This repo has already paid for the same class of mistake once -- 421s through
#: the ingress while `/health` stayed 200.
UNAUTHENTICATED_PATHS: frozenset[str] = frozenset({"/health"})


def extract_gateway_key(raw_headers: Sequence[tuple[bytes, bytes]]) -> str:
    """The caller's virtual key from ASGI byte-pair headers, or ``""``.

    The PRECEDENCE moved to `gateway_identity.gateway_key_from_headers` in #50 so the
    HTTP admin surface obeys the same rule; what stays here is reading it out of the ASGI
    header shape. Signature unchanged for every existing caller and test.
    """
    return gateway_key_from_headers(lambda name: _sole_header(raw_headers, name))


def _sole_header(raw_headers: Sequence[tuple[bytes, bytes]], name: str) -> str | None:
    """The one value of *name*, ``None`` if absent. Raises if callers disagree about it.

    Not ``dict(...)`` over the raw pairs, which is what this was: that silently takes the
    LAST duplicate, while Starlette's ``Headers.get`` takes the FIRST. Two consequences, and
    the second is worse than the first. A caller who can get a header placed after the
    gateway's injected one would CHOOSE the credential; and any audit line written later
    through ``request.headers`` would name a different key than the one actually authorized,
    so the log would exonerate the key that was used.

    Refusing outright, rather than picking a side, is the only answer that cannot be wrong:
    a request carrying two different credentials has no defensible interpretation.
    """
    values = [v.decode("latin-1") for k, v in raw_headers if k.decode("latin-1").lower() == name]
    if not values:
        return None
    if len({v.strip() for v in values}) > 1:
        raise _RefusalError(401, f"{name} was sent more than once with different values")
    return values[0]


class GatewayIdentityMiddleware:
    """Resolve each request's key to a :class:`MemoryPrincipal` on ``request.state``.

    Pure ASGI rather than ``BaseHTTPMiddleware``: the latter wraps the request in its own
    task and buffers the body, which the streamable-HTTP transport does not tolerate.

    Two collaborators are injected rather than imported, because both are what makes this
    otherwise untestable. *resolver* answers "who does the gateway say this key is" and
    *principal_lookup* is a storage read of ``key_principals``. A test supplies fakes; the
    entry point supplies the real gateway and the running store.

    **The principal is not cached; the identity is.** The gateway lookup costs p50 131 ms /
    p95 461 ms (DW-026) and is somebody else's system, so it is cached for a TTL. The
    principal is a local read against our own store and it is the revocation lever we
    control -- ``memotron key unbind`` has to take effect on the next request, not within
    a minute of it.
    """

    def __init__(
        self,
        app: Any,
        *,
        resolver: Callable[[str], GatewayIdentity],
        principal_lookup: Callable[[str], dict[str, Any] | None],
        cache: IdentityCache | None = None,
    ) -> None:
        self.app = app
        # The two decisions that are the same on every transport live in infra now (#50),
        # because `admin_server` needs them and cannot import this module -- both are
        # `delivery`, an edge `coupling_report.py` fails. What stays here is what is
        # genuinely ASGI: header extraction, the worker thread, and rendering the refusal.
        #
        # The cache stays owned HERE and is passed in, rather than letting the resolver
        # make its own. It is one object either way, but `_cache` is reached directly by
        # 50 assertions in `test_mcp_identity_middleware.py`, and a refactor that is meant
        # to be behaviour-neutral should not rewrite the tests that would notice if it
        # were not.
        self._cache = IdentityCache() if cache is None else cache
        self._identity = GatewayPrincipalResolver(
            resolver=resolver,
            principal_lookup=principal_lookup,
            cache=self._cache,
        )

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http" or not identity_required():
            await self.app(scope, receive, send)
            return
        if scope.get("path", "") in UNAUTHENTICATED_PATHS:
            await self.app(scope, receive, send)
            return

        key = ""
        try:
            key = extract_gateway_key(scope.get("headers", []))
            if not key:
                raise _RefusalError(401, "no gateway key on the request")
            # ONLY the gateway lookup goes to a thread -- see `_identity_for_key`.
            identity = await to_thread.run_sync(self._identity.identity_for_key, key, limiter=_GATEWAY_LOOKUP_LIMITER)
            principal = self._identity.principal_for_identity(identity)
        except _RefusalError as refusal:
            # The full reason goes HERE and only here. `public_detail` is what the caller
            # sees, and the two differ on purpose -- see `_RefusalError`.
            _log.warning(
                "mcp identity refused: status=%s key_fp=%s reason=%s",
                refusal.status,
                key_fingerprint(key) if key else "-",
                refusal.detail,
            )
            await self._refuse(send, refusal.status, refusal.public_detail, retry_after=refusal.retry_after)
            return

        _log.info(
            "mcp identity resolved: principal=%s tenant=%s alias=%s key_fp=%s",
            principal.principal_id,
            principal.tenant_id,
            identity.key_alias,
            key_fingerprint(key),
        )

        # `scope["state"]` is what crosses the task boundary into the tool body; writing
        # through a Request object here would be identical, and this is one less import.
        scope.setdefault("state", {})[PRINCIPAL_STATE_ATTR] = principal
        await self.app(scope, receive, send)

    @staticmethod
    async def _refuse(send: Any, status: int, detail: str, *, retry_after: str | None = None) -> None:
        body = json.dumps({"error": detail}).encode()
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
        if retry_after is not None:
            headers.append((b"retry-after", retry_after.encode()))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": body})


#: Kept as a module-private ALIAS rather than a second class. The definition moved to
#: infra with the resolver (#50) so `admin_server` can raise and render the same
#: refusals; every use in this module reads unchanged, and `isinstance` still matches
#: across both transports -- which it must, or the two would drift apart silently.
_RefusalError = GatewayIdentityRefusalError
