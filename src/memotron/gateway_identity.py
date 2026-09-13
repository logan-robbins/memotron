"""Resolve a caller's JedAI Gateway virtual key to the identity the gateway asserts.

This is the **authentication** half of the authorization model decided in #27 and
recorded as DW-014 / DW-026 / DW-027 / DW-028 in ``docs/authorization-decisions.md``.
It answers exactly one question -- *who does the gateway say this key belongs to?* --
and deliberately stops there. Mapping that identity onto a
:class:`~memotron.config.MemoryPrincipal` is authorization, is a separate decision,
and is not made here.

Why a self-lookup rather than a claim on the request
----------------------------------------------------
DW-018 refuses a tenant claim in gateway team metadata (team admins are the use case's
own owners) and refuses one in key metadata (team members hold key create and update
rights). Either would be self-asserted: the tenant writing a claim about itself.
DW-027 measured the same thing from the other side -- ``/key/generate`` and
``/key/update`` are not admin-only and ``team_key_generation`` admits role ``user``,
so a caller can set its own metadata. **So no claim on the request is trusted. The
gateway is asked.**

What the gateway is asked, and where
------------------------------------
``GET /key/info`` with the caller's own key and no ``key`` parameter returns that key's
record -- a key may query itself, no admin credential involved (DW-026, observed).

**The lookup must target the ADMIN plane.** The base chart sets
``DISABLE_ADMIN_ENDPOINTS: "true"`` for all roles and the admin role overrides it to
``"false"``, so the data plane answers ``403 "Management routes are disabled for this
instance."`` to ``/key/info`` -- *including* the ``?key=<self>`` form. That is why
:func:`admin_base_url_from` exists rather than the caller passing
``LITELLM_API_BASE`` straight through: pointing this at the data plane fails 100% of
the time, and it fails as a 403 that looks like an authorization refusal rather than a
misconfiguration.

The master key does NOT self-look-up
------------------------------------
``/key/info`` resolves DB-backed virtual keys. The proxy master key is the configured
secret rather than a key row, so a self-lookup with it returns
``404 {"error": {"message": "Key not found in database"}}`` -- observed on LATEST
2026-09-02. This is correct behaviour and not a misconfiguration, but it will mislead
anyone who reaches for the master key to smoke-test this path. Use a minted virtual key.

Cost, measured
--------------
One cache miss on this path: **p50 131 ms, p95 461 ms** (DW-026, 25 samples from a
workstation over VPN; the in-cluster figure is still owed by #12). The p95 is roughly
three times the admin key's, so the tail is worse than the median suggests -- which is
why :class:`IdentityCache` exists and why its TTL is a security parameter rather than a
performance knob.

Fail closed
-----------
Every failure raises. There is no partial identity and no "unauthenticated" return
value a caller could forget to check: a gateway that is unreachable, slow, or refusing
means the request does not proceed. Cached entries are never served past their TTL, so
a revoked key stops working within one TTL of revocation and no sooner -- Memotron
cannot revoke faster than the gateway's own per-pod auth cache does (DW-026).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urlunparse

from memotron.config import MemoryPrincipal, principal_from_registry_row
from memotron.gateway import (
    GatewayRequestError,
    gateway_base_url_from_env,
    gateway_request_with_retry,
)

#: Seconds a resolved identity may be reused. A SECURITY parameter, not a performance
#: one: it is exactly the window in which a revoked key still works. DW-026 records 60 s
#: as directional, and notes the gateway's own key cache is per-pod, unshared, and also
#: 60 s -- so lowering this below that buys nothing the gateway will honour.
#: Explicit admin-plane URL. Set this when the host is not the deployed
#: ``<env>.<service>.<domain>`` shape -- the derivation refuses to guess.
GATEWAY_ADMIN_BASE_URL_ENV = "MEMOTRON_GATEWAY_ADMIN_BASE_URL"

IDENTITY_CACHE_TTL_ENV = "MEMOTRON_IDENTITY_CACHE_TTL"
DEFAULT_IDENTITY_CACHE_TTL_SECONDS = 60.0

#: The header MCP clients send. DW-014 records that the gateway STRIPS a caller
#: ``Authorization`` header when it is the gateway key itself, so this is the channel a
#: forwarded key actually arrives on. DW-028 proved the forwarding works, provided the
#: MCP server's registration lists this header in its ``extra_headers`` allowlist.
FORWARDED_KEY_HEADER = "x-litellm-api-key"


@dataclass(frozen=True, slots=True)
class GatewayIdentity:
    """What the gateway asserts about one virtual key. No Memotron concepts."""

    key_alias: str | None
    team_id: str | None
    user_id: str | None

    @property
    def is_anonymous(self) -> bool:
        """True when the gateway resolved the key but asserts no identity for it.

        Observed on admin-minted personal keys: ``user_id`` and ``team_id`` both
        ``None`` (DW-027). Such a key authenticates -- it is real and unrevoked -- but
        carries nothing to authorize against, which is a caller-side decision.
        """
        return self.key_alias is None and self.team_id is None and self.user_id is None


def admin_base_url_from(base_url: str) -> str:
    """Return the admin-plane URL for a gateway base URL.

    ``https://latest.jedai-gateway.wdprapps.disney.com/v1``
    -> ``https://latest.jedai-gateway-admin.wdprapps.disney.com``

    Management routes live only on the admin deployment (DW-026); the data plane 403s
    ``/key/info`` including the ``?key=<self>`` form. The path is dropped because
    ``/key/info`` is not under ``/v1``.

    **The derivation is deliberately narrow.** It handles the deployed host shape
    ``<env>.<service>.<domain...>`` and nothing else, because a rule general enough to
    also cover ``<service>.<domain>`` cannot tell which label is the service without
    guessing -- and guessing wrong sends a caller's credential to the wrong host. Any
    other shape must set :data:`GATEWAY_ADMIN_BASE_URL_ENV` explicitly, and is told so.
    """
    override = os.environ.get(GATEWAY_ADMIN_BASE_URL_ENV, "").strip()
    if override:
        return override.rstrip("/")
    parsed = urlparse(base_url.strip())
    if not parsed.scheme or not parsed.netloc:
        raise GatewayRequestError(f"gateway base URL is not absolute: {base_url!r}", attempts=0, retryable=False)
    host, _, port = parsed.netloc.partition(":")
    labels = host.split(".")
    if len(labels) < 4:
        raise GatewayRequestError(
            f"cannot derive an admin host from {host!r}: expected <env>.<service>.<domain>; "
            f"set {GATEWAY_ADMIN_BASE_URL_ENV} instead",
            attempts=0,
            retryable=False,
        )
    if not labels[1].endswith("-admin"):
        labels[1] = f"{labels[1]}-admin"
    admin_host = ".".join(labels)
    netloc = f"{admin_host}:{port}" if port else admin_host
    return urlunparse((parsed.scheme, netloc, "", "", "", ""))


def key_fingerprint(raw_key: str) -> str:
    """A stable, non-reversible handle for one key.

    Used as the cache key so the raw credential is never held in memory beyond the
    request that carried it, and never appears in a log line or a repr.
    """
    return hashlib.sha256(raw_key.strip().encode("utf-8")).hexdigest()


def resolve_gateway_identity(
    raw_key: str,
    *,
    base_url: str | None = None,
    urlopen: Callable[..., Any] = urllib.request.urlopen,
    timeout: float = 10.0,
) -> GatewayIdentity:
    """Ask the gateway who this key belongs to. Raises on any failure.

    *raw_key* is the caller's own virtual key, forwarded to us. It is sent once, to the
    admin plane, and never stored.
    """
    key = raw_key.strip()
    if not key:
        raise GatewayRequestError("no gateway key supplied", attempts=0, retryable=False)

    admin_url = admin_base_url_from(base_url or gateway_base_url_from_env())
    request = urllib.request.Request(f"{admin_url}/key/info", method="GET")
    request.add_header("Authorization", f"Bearer {key}")
    request.add_header("Accept", "application/json")

    def perform() -> dict[str, Any]:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read() or b"{}")

    # A 401/403 is the gateway's considered answer and is deliberately NOT retried;
    # only transport faults and 5xx are. See gateway_request_with_retry.
    body = gateway_request_with_retry(perform, description="gateway /key/info self-lookup")

    # An `info` key that is present but not an object is a MALFORMED record, not an
    # absent one -- falling back to the envelope would read it as an anonymous identity,
    # which is the fail-open this module exists to prevent.
    info = body.get("info", body)
    if not isinstance(info, dict):
        raise GatewayRequestError(f"gateway /key/info returned no key record: {body!r}", attempts=0, retryable=False)

    def field(name: str) -> str | None:
        value = info.get(name)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return GatewayIdentity(
        key_alias=field("key_alias"),
        team_id=field("team_id"),
        user_id=field("user_id"),
    )


def identity_cache_ttl_from_env() -> float:
    """Cache TTL in seconds, from the environment. Unparseable input -> default.

    ``inf`` and ``nan`` are REFUSED, not accepted, and that is a security decision rather
    than input hygiene. ``float("inf")`` parses, is ``>= 0``, and was returned -- making a
    cached identity live forever, so a revoked key would keep working indefinitely and
    nothing would report it. Every rejected value fails toward the 60 s default, i.e.
    toward revoking sooner, because this TTL is exactly the window in which a revoked key
    still works. ``nan`` is worse than useless: every comparison against it is False, so
    expiry silently never fires either.

    This became reachable when #126 put the cache on the live request path.
    """
    raw = os.environ.get(IDENTITY_CACHE_TTL_ENV, "").strip()
    if not raw:
        return DEFAULT_IDENTITY_CACHE_TTL_SECONDS
    try:
        ttl = float(raw)
    except ValueError:
        return DEFAULT_IDENTITY_CACHE_TTL_SECONDS
    if not math.isfinite(ttl) or ttl < 0:
        return DEFAULT_IDENTITY_CACHE_TTL_SECONDS
    return ttl


class IdentityCache:
    """TTL cache of resolved identities, keyed by key fingerprint.

    Exists because the uncached path costs p50 131 ms / p95 461 ms per call (DW-026),
    which is a per-request tax on every memory operation.

    **Failures are never cached.** A gateway outage must not pin every caller to a
    refusal for a TTL, and a revoked key must not be kept alive by a cached success --
    so only successful resolutions are stored, and only until they expire.

    **And it is BOUNDED.** Expired entries are evicted lazily, on re-access of the same
    fingerprint, so a workload that never repeats a key never evicts anything. DW-027
    records that ``/key/generate`` is not admin-only, so a caller can mint keys in a loop;
    every distinct valid key would otherwise add a permanent entry until the pod is
    OOMKilled. The bound matters only once #126 put this on the live request path.
    """

    __slots__ = ("_entries", "_max_entries", "_now", "_ttl")

    def __init__(
        self,
        *,
        ttl_seconds: float | None = None,
        now: Callable[[], float] = time.monotonic,
        max_entries: int = 4096,
    ) -> None:
        self._ttl = identity_cache_ttl_from_env() if ttl_seconds is None else ttl_seconds
        self._now = now
        self._max_entries = max(1, max_entries)
        self._entries: dict[str, tuple[float, GatewayIdentity]] = {}

    @property
    def ttl_seconds(self) -> float:
        return self._ttl

    def get(self, raw_key: str) -> GatewayIdentity | None:
        """A live cached identity, or None. Expired entries are dropped, not returned."""
        fingerprint = key_fingerprint(raw_key)
        entry = self._entries.get(fingerprint)
        if entry is None:
            return None
        expires_at, identity = entry
        if self._now() >= expires_at:
            del self._entries[fingerprint]
            return None
        return identity

    def put(self, raw_key: str, identity: GatewayIdentity) -> None:
        if len(self._entries) >= self._max_entries:
            self._evict()
        self._entries[key_fingerprint(raw_key)] = (self._now() + self._ttl, identity)

    def _evict(self) -> None:
        """Drop everything expired; if that frees nothing, drop the oldest half.

        The second clause is the one that matters. Sweeping only expired entries leaves a
        cache full of live ones exactly as full as it was, so `put` would sweep on every
        call and still grow -- a bound that cannot bind. Evicting live entries costs a
        gateway round-trip on the next request for those keys, which is the correct thing
        to trade for a ceiling.
        """
        now = self._now()
        self._entries = {fp: e for fp, e in self._entries.items() if now < e[0]}
        if len(self._entries) < self._max_entries:
            return
        by_expiry = sorted(self._entries.items(), key=lambda item: item[1][0])
        self._entries = dict(by_expiry[len(by_expiry) // 2 :])

    def clear(self) -> None:
        self._entries.clear()

    def resolve(self, raw_key: str, resolver: Callable[[str], GatewayIdentity]) -> GatewayIdentity:
        """Cached resolution. *resolver* is called only on a miss, and only its
        successes are cached -- an exception propagates and leaves the cache untouched.
        """
        cached = self.get(raw_key)
        if cached is not None:
            return cached
        identity = resolver(raw_key)
        self.put(raw_key, identity)
        return identity


class GatewayIdentityRefusalError(Exception):
    """One HTTP refusal, carrying TWO messages on purpose.

    ``detail`` is for our log and may quote whatever upstream said. ``public_detail`` is
    what the caller receives, and defaults to ``detail`` only where that text was written
    to be read by a stranger.

    The split exists because the single-message version leaked. The gateway's
    ``GatewayRequestError`` message embeds the upstream response body verbatim, and
    LiteLLM's auth-error body was observed carrying ``Received API Key = sk-...`` together
    with the key's verification-table hash -- returned, before this, to any unauthenticated
    caller who could reach the ingress. A refusal is the one response an attacker can always
    elicit, so it is the last place to relay somebody else's error text.

    Lives HERE, in infra, rather than beside the ASGI middleware that first raised it,
    because `admin_server` needs the same refusals and is FORBIDDEN from importing
    `mcp_auth` -- both are `delivery`, so that edge is a sideways tier edge
    `coupling_report.py` fails on. The refusal shape is transport-independent; only the
    rendering is not.
    """

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        retry_after: str | None = None,
        public_detail: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.public_detail = detail if public_detail is None else public_detail
        self.retry_after = retry_after


class GatewayPrincipalResolver:
    """raw key -> `MemoryPrincipal`, for every transport that has to authenticate a caller.

    Extracted from `GatewayIdentityMiddleware` for #50 without changing a line of its
    logic. The middleware keeps what is genuinely ASGI -- pulling the key out of a
    `headers` sequence, running the gateway call in a worker thread, and rendering the
    refusal -- and delegates the two decisions that are the same on any transport.

    That split is not stylistic. `admin_server` serves `/api/platform/*`, the same
    operations over HTTP, and cannot import `mcp_auth`: both are `delivery`, so
    `coupling_report.py` fails the edge. Reimplementing the refusal ladder there would
    have produced a second one that drifts -- and every branch below was written in
    response to something that actually went wrong, so a drifting copy loses those
    specific lessons rather than generic tidiness.

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
        *,
        resolver: Callable[[str], GatewayIdentity],
        principal_lookup: Callable[[str], dict[str, Any] | None],
        cache: IdentityCache | None = None,
    ) -> None:
        self._resolver = resolver
        self._principal_lookup = principal_lookup
        self._cache = IdentityCache() if cache is None else cache

    def identity_for_key(self, key: str) -> GatewayIdentity:
        """Ask the gateway who this key is. **The only part that belongs in a thread.**

        ``resolve_gateway_identity`` is ``urllib.request.urlopen``, so calling it inline
        from an event loop stalls the whole loop for the p95 461 ms and serialises every
        concurrent request behind it. The ASGI caller is responsible for the thread; a
        synchronous caller such as `admin_server`'s handler is already on its own thread
        and calls this directly.

        The principal lookup deliberately does NOT come along. It is a synchronous storage
        read, and ``SQLiteStorageBackend`` opens its connection with the default
        ``check_same_thread=True`` (``storage/sqlite/__init__.py:116``), so running it in a
        worker thread raises ``ProgrammingError`` on every request against a SQLite store
        -- turning "arm the flag" into a 500 for every caller in any environment without a
        Postgres DSN. Postgres survives it (its engine has a dedicated loop thread), which
        is exactly what makes the bug invisible in the environments this was written for.
        Every test substitutes a fake lookup, so no test would have caught it either.
        """
        try:
            return self._cache.resolve(key, self._resolver)
        except GatewayRequestError as error:
            # `public_detail` is deliberately fixed text. The full message is built at
            # `gateway.py` as "<description> failed with HTTP <code>: <body>" -- the
            # gateway's response body VERBATIM -- and LiteLLM's auth-error body was observed
            # on the preview gateway to contain `Received API Key = sk-...` and the key's
            # `LiteLLM_VerificationTokenTable` hash. Returning that to an unauthenticated
            # caller puts credential material in an HTTP response body that any
            # 4xx-body-logging intermediary then captures. The body still reaches the log.
            if error.retryable:
                # The gateway never answered, or kept answering 5xx. That is an outage, not
                # a refusal, and 401 would send the caller off to re-check a valid key.
                raise GatewayIdentityRefusalError(
                    503,
                    f"gateway identity lookup unavailable: {error}",
                    retry_after="5",
                    public_detail="gateway identity lookup is unavailable; retry shortly",
                ) from error
            raise GatewayIdentityRefusalError(
                401,
                f"gateway rejected the supplied key: {error}",
                public_detail="the gateway rejected the supplied key",
            ) from error

    def principal_for_identity(self, identity: GatewayIdentity) -> MemoryPrincipal:
        """Map a gateway identity to a principal. A local indexed read, never threaded.

        Doing this on the caller's own thread rather than in the worker thread is what
        keeps the SQLite path working -- see `identity_for_key`.
        """
        if identity.key_alias is None:
            # Authenticated and unidentifiable. DW-027 measured ~40% of gateway keys
            # carrying no identity at all; the alias IS the lookup key, so there is nothing
            # to map. 403, not 401 -- the key is real, it just cannot be authorized.
            raise GatewayIdentityRefusalError(
                403, "the gateway asserts no key_alias for this key; it cannot be mapped to a principal"
            )

        try:
            row = self._principal_lookup(identity.key_alias)
        except Exception as error:
            # Outside the try that wraps `principal_from_registry_row` this used to be an
            # uncaught 500. Still fail-closed either way, but a 500 is a different incident
            # from a 403 and it is the store's fault, not the caller's.
            raise GatewayIdentityRefusalError(
                503,
                f"principal lookup failed for {identity.key_alias!r}: {error}",
                retry_after="5",
                # WITHOUT this the store's exception became the caller's response body.
                # `error` here is whatever the operational store raised, and psycopg says
                # things like: connection to server at "dw-operational-pg.<ns>.svc.cluster
                # .local" (10.154.9.31), port 5432 failed: FATAL: password authentication
                # failed for user "memotron_rw" -- internal DNS name, pod IP, port and
                # DB username, to an UNAUTHENTICATED caller, reachable whenever the store
                # is unhealthy. This was the one of the seven sites that omitted it (#263).
                # The full text still reaches the log; only the caller's copy is scrubbed.
                public_detail="principal lookup is unavailable; retry shortly",
            ) from error

        if row is None:
            raise GatewayIdentityRefusalError(
                403,
                f"gateway key_alias {identity.key_alias!r} is not bound to a principal; "
                f"bind it with: memotron key bind --alias {identity.key_alias} "
                "--principal-id <id> --tenant-id <tenant>",
                # The alias and the remedy are safe to return: the caller supplied the key
                # the alias belongs to, and without the command an operator has no way to
                # learn what to bind.
                public_detail=(
                    f"gateway key_alias {identity.key_alias!r} is not bound to a principal; "
                    f"bind it with: memotron key bind --alias {identity.key_alias} "
                    "--principal-id <id> --tenant-id <tenant>"
                ),
            )

        try:
            return principal_from_registry_row(row)
        except Exception as error:
            # `principal_from_registry_row` raises rather than defaulting, precisely so this
            # is a decision. An unreadable row must not degrade to a permissive principal:
            # a missing default_scope is one FEWER allowlist entry, so degrading widens.
            raise GatewayIdentityRefusalError(
                403,
                f"key_principals row for {identity.key_alias!r} is unreadable: {error}",
                public_detail=f"key_principals row for {identity.key_alias!r} is unreadable",
            ) from error


def gateway_key_from_headers(sole_header: Callable[[str], str | None]) -> str:
    """The caller's virtual key, or ``""``. **Which header wins lives here, once.**

    *sole_header* returns the one value of a header name, ``None`` if absent, and raises
    if the request carried it twice with different values. Each transport supplies its own
    -- ASGI reads a sequence of byte pairs, `http.server` reads an
    ``email.message.Message`` -- because the SHAPE differs per transport while the
    PRECEDENCE below must not.

    ``x-litellm-api-key`` WINS over ``Authorization``, and the order is load-bearing rather
    than arbitrary. When a call arrives through the gateway, DW-014 records that the gateway
    strips a caller ``Authorization`` header when it is the gateway's own key -- so
    ``Authorization`` may well be carrying the *gateway's* credential, not the caller's,
    while ``x-litellm-api-key`` is the channel a forwarded caller key actually arrives on
    (DW-028). Preferring ``Authorization`` would authenticate the wrong party, and would do
    it successfully, which is the failure mode with no symptom.
    """
    forwarded = sole_header(FORWARDED_KEY_HEADER)
    if forwarded is not None:
        # PRESENT beats non-empty. A present-but-empty `x-litellm-api-key` is the normal
        # shape when a proxy templates the header and the caller sent nothing, and falling
        # through to `Authorization` there re-opens the exact confused deputy the precedence
        # rule exists to close: DW-014 says `Authorization` may be carrying the GATEWAY's
        # own credential. If that alias were ever bound to a principal -- entirely plausible
        # during rollout debugging -- every caller would collapse onto one identity, and a
        # two-key differential probe would not catch it, because both of its arms send a
        # non-empty forwarded header. So the header's PRESENCE is the assertion that the
        # forwarding path ran, and an empty value is a refusal, not a fallback.
        if not forwarded.strip():
            raise GatewayIdentityRefusalError(401, f"{FORWARDED_KEY_HEADER} is present but empty; not falling back")
        return forwarded.strip()

    authorization = sole_header("authorization")
    if authorization is None:
        return ""
    if authorization.strip().lower().startswith("bearer "):
        return authorization.strip()[len("bearer ") :].strip()
    return ""
