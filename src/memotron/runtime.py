from __future__ import annotations

import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memotron.agents import (
    DreamAgentTransport,
    OpenAICompatibleDreamAgentTransport,
)
from memotron.embedding import (
    EmbeddingTransport,
    LocalEmbeddingTransport,
    OpenAICompatibleEmbeddingTransport,
)
from memotron.extraction import (
    ExtractionTransport,
    OpenAICompatibleExtractionTransport,
    RuleBasedExtractionTransport,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    DREAM_AGENT_MODEL_ENV,
    GATEWAY_API_KEY_ENV,
    GATEWAY_MODEL_ENV,
    gateway_base_url_from_env,
)
from memotron.synthesis import (
    OpenAICompatibleSynthesisTransport,
    SynthesisTransport,
)

RETIRED_PROVIDER_MESSAGE = (
    "provider 'anthropic' is retired: Memotron reaches Claude through the "
    "JedAI Gateway. Re-seal this tenant with "
    "`memotron llm configure --provider litellm --model claude-haiku-4-5 "
    f"--base-url {DEFAULT_GATEWAY_BASE_URL} --api-key-env {GATEWAY_API_KEY_ENV}`"
)

SUPPORTED_PROVIDERS = ("litellm", "openai")


def _reject_provider(provider: str) -> None:
    """Fail fast, and name the fix, for any provider Memotron cannot build."""
    if provider == "anthropic":
        raise ValueError(RETIRED_PROVIDER_MESSAGE)
    raise ValueError("provider must be 'litellm' or 'openai'")


def _resolved_model(model: str, provider: str) -> str:
    """Model for a sealed credential: explicit value, else the provider default."""
    return model.strip() or (DEFAULT_GATEWAY_MODEL if provider == "litellm" else "gpt-4o-mini")


def _resolved_base_url(base_url: str, provider: str) -> str:
    """Endpoint for a sealed credential: explicit value, else the provider default."""
    return base_url.strip() or (DEFAULT_GATEWAY_BASE_URL if provider == "litellm" else "https://api.openai.com/v1")


def _env_endpoint() -> tuple[str, str, str] | None:
    """Resolve ``(api_key_env, base_url, model)`` from the process environment.

    One precedence order, used by every env-driven builder: the JedAI Gateway
    key first, then a generic OpenAI-compatible key.  ``None`` means no endpoint
    is configured, and callers fall back to their deterministic default
    (rule-based extraction, no dream agent, no synthesis).  Only variable NAMES
    are handled here; key VALUES are read fail-fast inside the transports.
    """
    model_override = os.environ.get(GATEWAY_MODEL_ENV, "").strip()
    if os.environ.get(GATEWAY_API_KEY_ENV, "").strip():
        return (
            GATEWAY_API_KEY_ENV,
            gateway_base_url_from_env(),
            model_override or DEFAULT_GATEWAY_MODEL,
        )
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return (
            "OPENAI_API_KEY",
            os.environ.get("OPENAI_API_URL", "").strip() or "https://api.openai.com/v1",
            model_override or "gpt-4o-mini",
        )
    return None


def load_env_file(env_file: str | Path) -> None:
    """Load simple KEY=VALUE entries without overriding process environment.

    T1-14: ``env_file`` is REQUIRED and has no default on purpose.  It used to
    default to the cwd-relative ``".env"``, which meant every builder below
    silently adopted credentials from whatever directory the process happened to
    start in — promoting deterministic rule-based extraction to a gateway-billed
    call, and writing into ``os.environ`` so that a caller which explicitly
    DELETED a key got it back from disk a moment later.  Pass a path anchored to
    a project root, the way :func:`memotron.adoption._ensure_project_llm_credentials`
    does.  Reproduction: ``scripts/verify/probe_env_file_cwd.py``.
    """
    path = Path(env_file)
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key or normalized_key in os.environ:
            continue
        normalized_value = value.strip().strip("\"'")
        os.environ[normalized_key] = normalized_value


def build_transports_from_credentials(
    *,
    provider: str,
    api_key: str,
    base_url: str = "",
    model: str = "",
) -> tuple[ExtractionTransport, DreamAgentTransport | None]:
    normalized_provider = provider.strip().lower()
    normalized_key = api_key.strip()
    if not normalized_key:
        raise ValueError("api_key cannot be blank")
    if normalized_provider not in SUPPORTED_PROVIDERS:
        _reject_provider(normalized_provider)
    # One sealed tenant credential powers BOTH transports: extraction and the
    # dream agent ride the same OpenAI-compatible endpoint.
    resolved_model = _resolved_model(model, normalized_provider)
    resolved_base_url = _resolved_base_url(base_url, normalized_provider)
    return (
        OpenAICompatibleExtractionTransport(
            model=resolved_model,
            base_url=resolved_base_url,
            api_key=normalized_key,
        ),
        OpenAICompatibleDreamAgentTransport(
            model=resolved_model,
            base_url=resolved_base_url,
            api_key=normalized_key,
        ),
    )


def build_transports_from_tenant_credentials(
    credentials: dict[str, Any] | None,
) -> tuple[ExtractionTransport, DreamAgentTransport | None]:
    if credentials is None:
        return build_transports_from_env()
    return build_transports_from_credentials(
        provider=str(credentials.get("provider", "")),
        api_key=str(credentials.get("api_key", "")),
        base_url=str(credentials.get("base_url", "")),
        model=str(credentials.get("model", "")),
    )


def build_transports_from_tenant_graph(
    *,
    graph_path: str | Path | None = None,
    tenant_id: str,
    store: Any | None = None,
) -> tuple[ExtractionTransport, DreamAgentTransport | None]:
    """Build a tenant's transports from its stored credentials.

    Accepts an already-open ``store`` so a caller holding an operational (Postgres)
    backend can read credentials without a file path it does not have. When *store*
    is supplied it is NOT closed here: the caller owns its lifetime, and closing a
    shared pool out from under it would take every other reader with it.

    Passing ``graph_path`` keeps the original behaviour exactly -- open, read, close.
    """
    if (graph_path is None) == (store is None):
        raise ValueError("pass exactly one of graph_path or store")

    if store is not None:
        return build_transports_from_tenant_credentials(store.tenant_llm_credentials(tenant_id))

    from memotron.storage import open_storage

    # The XOR above guarantees this, but the type checker cannot see it through the
    # `(a is None) == (b is None)` form. Asserting keeps the guarantee checkable at
    # runtime rather than silencing the checker with an ignore.
    assert graph_path is not None
    opened = open_storage(graph_path)
    try:
        return build_transports_from_tenant_credentials(opened.tenant_llm_credentials(tenant_id))
    finally:
        opened.close()


#: Comma-separated Host header values the MCP servers will accept, beyond localhost.
#: Set it to the ingress hostname in every deployed environment.  ``*`` disables
#: DNS-rebinding protection entirely and is the only way to do so.
MCP_ALLOWED_HOSTS_ENV = "MEMOTRON_MCP_ALLOWED_HOSTS"

#: Bind address and port for either MCP server.
MCP_HOST_ENV = "MCP_HOST"
MCP_PORT_ENV = "MCP_PORT"

#: Always accepted, so a port-forward, an in-cluster probe and a dev box keep working
#: no matter what a deployment declares.  These are the SDK's own localhost patterns,
#: kept verbatim across the FastMCP 4 move so the fail-closed default did not shift.
_LOCALHOST_HOSTS = ("127.0.0.1:*", "localhost:*", "[::1]:*")
_LOCALHOST_ORIGINS = ("http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*")


@dataclass(frozen=True)
class McpHostSecurity:
    """What `FastMCP.http_app()` needs to refuse a forged `Host` or `Origin`.

    FastMCP 4 splits what the SDK bundled into one object, and the split is a trap:
    ``allowed_hosts`` **alone does nothing**. ``http_host_origin_protection`` defaults
    to ``False``, so the guard middleware is not installed and an allowlist configures
    no one. Measured on fastmcp 4.0.3, three servers, curl sending Host verbatim::

        allowed_hosts only ................ bogus Host -> 200   ACCEPTED
        protection=True + allowed_hosts ... bogus Host -> 421   refused
        nothing set (the default) ......... bogus Host -> 200   ACCEPTED

    So ``protection`` is never ``False`` here except on the explicit ``"*"`` opt-out.
    Do not add a code path that passes ``allowed_hosts`` without it.

    ``"auto"`` -- which FastMCP's own HTTP deployment docs recommend for reverse-proxy
    deployments -- is not used here, and the honest reason is narrower than it first
    looks. Measured on fastmcp 4.0.3, forged Host against a non-loopback bind::

        "auto", no allowed_hosts ......... 200   guards nothing
        "auto", WITH allowed_hosts ....... 421   identical to True
        True,   WITH allowed_hosts ....... 421

    So ``"auto"`` would be *equivalent* for us: `_should_validate_host` short-circuits on
    ``has_explicit_allowed_hosts`` before it ever consults the connection's local address,
    and we always pass an allowlist. The first row is the real trap and it is why the
    mode is not worth adopting -- its behaviour depends on whether a *different* argument
    happens to be set, so the day someone passes an empty allowlist the protection
    silently becomes "loopback only", which for a container bound to 0.0.0.0 is nothing.
    ``True`` says what it does regardless of its neighbours.

    ``True`` also beats the environment: `FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=false` sets
    ``Settings().http_host_origin_protection`` to False, and the explicit keyword still
    wins -- verified. The guard cannot be switched off by a deployment.
    """

    protection: bool
    allowed_hosts: tuple[str, ...]
    allowed_origins: tuple[str, ...]

    def as_http_app_kwargs(self) -> dict[str, Any]:
        """Keyword arguments for ``FastMCP.http_app``."""
        return {
            "host_origin_protection": self.protection,
            "allowed_hosts": list(self.allowed_hosts),
            "allowed_origins": list(self.allowed_origins),
        }


def mcp_host_security() -> McpHostSecurity:
    """Host/Origin allowlist from ``MEMOTRON_MCP_ALLOWED_HOSTS``. **Fail-closed.**

    Three cases, and the middle one is the one that matters:

    ``"*"``      explicit opt-out. Named in the value rather than inferred from an empty
                 string, so "unset" can never silently mean "open".
    unset        **localhost patterns only**. Stage, load and prod all rely on this: a
                 request arriving through the ingress gets 421 while ``/health`` stays
                 200, which is why prod was deliberately put back on it (#237, #252).
                 FastMCP 4's own default is the opposite -- unset means OPEN -- so this
                 function is the thing standing between three environments and an open
                 MCP surface. `tests/test_chart_mcp_host_interlock.py` cannot see that;
                 it compares chart values and knows nothing about a framework default.
    a host list  localhost patterns **plus** each declared host.
    """
    raw = os.environ.get(MCP_ALLOWED_HOSTS_ENV, "").strip()
    declared = tuple(host.strip() for host in raw.split(",") if host.strip())

    # ANY `*` element is the escape hatch, not just a value that is exactly `*`.
    # Matching only `raw == "*"` left `"*,foo"` reporting protection=True while
    # accepting everything: `_host_matches` short-circuits on `pattern == "*"`, so the
    # `*` we faithfully copied into `allowed_hosts` matched every Host while the object
    # said it was guarded. A security control that misreports its own state is worse
    # than one that is off, because the misreport is what gets read.
    if "*" in declared:
        return McpHostSecurity(protection=False, allowed_hosts=(), allowed_origins=())

    # A declared host is matched with `fnmatchcase`, so `*` and `?` inside one are GLOBS.
    # `*.disney.com` is a wildcard subtree, not a literal. That is occasionally wanted and
    # always worth knowing; it is called out here because nothing else says so.

    hosts = [*_LOCALHOST_HOSTS]
    origins = [*_LOCALHOST_ORIGINS]
    for host in declared:
        hosts.append(host)
        # A proxy may present the host with an explicit port.
        if ":" not in host:
            hosts.append(f"{host}:*")
        # Browser clients send Origin; a Host-only allowlist still 421s them. Ingress is
        # TLS-only in every deployed environment, so https is the scheme that matters.
        origins.append(f"https://{host}")

    return McpHostSecurity(protection=True, allowed_hosts=tuple(hosts), allowed_origins=tuple(origins))


def mcp_bind_from_env(*, default_host: str, default_port: int) -> tuple[str, int]:
    """``(host, port)`` for an MCP server, from ``MCP_HOST`` / ``MCP_PORT``.

    A named function rather than two inline `os.environ.get` calls because the defaults
    used to be written at each site and **disagreed**: `agent_memory_mcp` read `MCP_HOST`
    defaulting to ``127.0.0.1`` while its own entry point re-read the same variable
    defaulting to ``0.0.0.0`` and assigned the result afterwards. With `MCP_HOST` unset
    the two were in conflict by construction. The default is still per-server -- the
    servers genuinely differ -- but it is now passed in once by the caller that owns it.
    """
    host = os.environ.get(MCP_HOST_ENV, default_host).strip() or default_host
    raw_port = os.environ.get(MCP_PORT_ENV, "").strip()
    return host, int(raw_port) if raw_port else default_port


def advertised_mcp_url(host: str, port: int, path: str = "/mcp") -> str:
    """The URL to hand a client for a server bound to ``host``/``port``.

    ``0.0.0.0`` and ``::`` are bind addresses, not destinations -- a client that dials
    them reaches nothing. They are rewritten to loopback, which is where a locally bound
    server actually answers. A deployment that must advertise something else passes its
    real URL in explicitly; this is the fallback.
    """
    resolved = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    # RFC 3986: an IPv6 literal in a URL must be bracketed, or the colons in the address
    # are read as the port separator. Unbracketed, `::1` produced `http://::1:8010/mcp`,
    # which no client can parse. Detected by counting colons rather than by parsing:
    # a bare hostname or IPv4 has none, `host:port` never reaches here (this function is
    # given host and port separately), so >1 colon means IPv6.
    if resolved.count(":") > 1 and not resolved.startswith("["):
        resolved = f"[{resolved}]"
    return f"http://{resolved}:{port}{path}"


#: Paths the SERVERS REGISTER (via `@mcp.custom_route`) that must be served outside the
#: Host guard.  This is the list to edit; keep it to endpoints that return a constant and
#: read nothing from the request.  See `build_mcp_http_app`.
HOISTABLE_PATHS = ("/health",)

#: What `StrictHostGuard` exempts: every hoistable path AND its trailing-slash spelling.
#: DERIVED, not written out, because the two must not drift -- exempting a path the hoist
#: does not also serve is worse than not exempting it, since the list then claims a path
#: is open while the inner guard still refuses it. That is exactly what happened when
#: `/health/` was added here by hand: it passed this guard, fell into `Mount("/")`, and
#: 421'd while `/health` returned 200.
UNGUARDED_PATHS = tuple(p for base in HOISTABLE_PATHS for p in (base, f"{base}/"))


class StrictHostGuard:
    """Refuse a `Host` this repo did not declare. Runs BEFORE FastMCP's own guard.

    It exists because FastMCP 4 accepts two hosts we never allowed, and one of them is a
    real widening relative to the SDK we migrated from.

    `HostOriginGuardMiddleware._allowed_hosts_for_scope` builds its allowlist as::

        DEFAULT_HOSTS ('127.0.0.1', 'localhost', '::1')     # unconditional
        + the allowed_hosts we passed
        + scope["server"][0]                                 # THE CONNECTION'S OWN ADDRESS

    That last entry is the problem. Under uvicorn `scope["server"]` is the local socket
    address of the accepted connection -- for a pod bound to `0.0.0.0`, the **pod IP**.
    curl's default `Host` when you dial an IP *is* that IP, so `curl http://<pod-ip>:8000/mcp`
    self-allowlists and succeeds.

    mcp 1.29.0, the SDK this replaced, had no such fallback: `transport_security.py`
    `_validate_host` consults `settings.allowed_hosts` and nothing else (read directly
    from the 1.29.0 wheel). So the same request was a 421 before this migration and a 200
    after. With `MEMOTRON_MCP_ALLOWED_HOSTS` unset and `requireGatewayIdentity` false
    -- the committed state of stage, load and prod -- that is all 39 tools, unauthenticated,
    to anything that can route to the pod. `.helm/values-prod.yaml` rests prod's entire
    safety argument on that request being refused.

    So the Host decision is made HERE, against exactly the set `mcp_host_security()`
    documents, and FastMCP's guard is left installed behind it to keep doing Origin.
    Loopback still passes -- it is in `_LOCALHOST_HOSTS` deliberately, port-forward and
    in-cluster probes depend on it, and it passed under the old SDK too. This restores
    the previous posture; it does not claim to improve on it. A caller who can open a
    socket can still send `Host: localhost`; the Host header is not an access control,
    and `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is what actually bounds callers.
    """

    def __init__(self, app: Any, *, security: McpHostSecurity, exempt: Sequence[str]) -> None:
        self.app = app
        self.security = security
        self.exempt = frozenset(exempt)

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        from fastmcp.server.http import _host_matches
        from starlette.responses import Response

        if scope["type"] != "http" or not self.security.protection:
            await self.app(scope, receive, send)
            return
        if scope.get("path") in self.exempt:
            await self.app(scope, receive, send)
            return

        host = ""
        for key, value in scope.get("headers", ()):
            if key == b"host":
                host = value.decode("latin-1")
                break

        if not _host_matches(host, self.security.allowed_hosts):
            # Same status and body FastMCP's guard uses, so a refusal is indistinguishable
            # from its own and nothing downstream has to learn a second shape.
            await Response("Misdirected Request", status_code=421)(scope, receive, send)
            return

        await self.app(scope, receive, send)


def build_mcp_http_app(
    mcp: Any,
    *,
    path: str = "/mcp",
    asgi_middleware: Sequence[Any] = (),
) -> Any:
    """The ASGI app for an MCP server, with this repo's security policy applied.

    Split out of `serve_mcp_http` so the policy can be **driven**, not just read: a test
    can build this app, send it a forged `Host` header and assert the 421. Asserting on
    the settings object instead is how the previous version of that check managed to pass
    on the broken code -- the SDK auto-enabled protection with a localhost-only allowlist
    exactly because the default host was 127.0.0.1, so "protection is on" was true and
    useless. A status code cannot be satisfied by the defect.

    ``stateless_http=True`` is #246's fix: sessions no longer live in one process, so a
    replica behind a round-robin Service can serve any request. Measured before the
    change: 5/10 gateway calls failed with 2 replicas and `sessionAffinity: None`.

    **`/health` is served outside the Host check, and that IS load-bearing -- but the
    first reason given for it was wrong, so here is the corrected one.**

    The original claim was that FastMCP's guard would 421 a kubelet probe. It would not.
    `HostOriginGuardMiddleware._allowed_hosts_for_scope` appends `scope["server"][0]`,
    which under uvicorn is the **local socket address of the accepted connection** -- for
    a pod bound to `0.0.0.0`, the pod IP. kubelet connects *to* the pod IP and sends it
    as `Host`, so it self-allowlists and returns 200. The recorded
    `Host: 10.154.184.212:8000 -> 421` only reproduces when the request is curled over
    loopback with a *fabricated* header, which is what was actually measured. Corrected
    after an independent review reproduced the real path.

    It is load-bearing now because of `StrictHostGuard`, which deliberately drops that
    self-allowlisting to close the regression described in its docstring. Once the pod IP
    is no longer auto-accepted, an `httpGet` probe with no `host:` -- which all six chart
    probes are -- would be refused. Verified in the built image, prod shape::

        Host: 172.17.0.2:8000 (the connection address)   /mcp 421   /health 200
        Host: evil.example.com                           /mcp 421   /health 200

    So the exemption is what keeps the fix from taking the probes down with it.

    The handler is *reused*, not reimplemented: it is taken from the server's own
    `@mcp.custom_route("/health")` registration, so there is one health payload per
    server and it stays next to the server that defines it. It is **copied, not moved** --
    the route remains registered inside the guarded app as well, which is harmless and
    means an allowlisted caller reaches the same handler by either path.

    Only `UNGUARDED_PATHS` is exempted, matched as an exact path. `/health/` (trailing
    slash) is not, and neither is anything else; both guards still apply to `/mcp`.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    security = mcp_host_security()
    guarded = mcp.http_app(
        path=path,
        middleware=list(asgi_middleware),
        stateless_http=True,
        # Still passed, and still with `host_origin_protection=True`: FastMCP's guard
        # keeps doing Origin, and a second Host check costs nothing. `StrictHostGuard`
        # in front of it is what makes the Host decision authoritative -- see its
        # docstring for the two hosts this one accepts that we never declared.
        **security.as_http_app_kwargs(),
    )

    from starlette.routing import Route

    hoisted: list[Any] = []
    for route in mcp._get_additional_http_routes():
        if getattr(route, "path", None) not in HOISTABLE_PATHS:
            continue
        hoisted.append(route)
        # Serve the trailing-slash spelling from the SAME endpoint. Starlette matches the
        # exact path and `Mount("/")` below matches everything, so without this `/health/`
        # never reaches the hoisted route: it falls into the guarded app and 421s while
        # `/health` returns 200. Exempting the path in `StrictHostGuard` alone was not
        # enough, and was worse than nothing -- the exemption list would have claimed a
        # path was open while the inner guard still refused it.
        hoisted.append(Route(f"{route.path}/", route.endpoint, methods=sorted(route.methods or ())))
    if not hoisted:
        # Still strict-guarded: the widening is in the guarded app, not the hoist.
        # No health route to hoist: return the guarded app unchanged rather than
        # wrapping it in a Starlette that adds nothing. Not an error -- a server may
        # legitimately declare no custom routes.
        return StrictHostGuard(guarded, security=security, exempt=())

    # The guarded app owns the session manager, so ITS lifespan must still run. This is
    # the same Mount-plus-explicit-lifespan shape `tests/test_mcp_identity_middleware.py`
    # already uses; get it wrong and the server starts and then 500s on the first call.
    @asynccontextmanager
    async def lifespan(_: Any) -> AsyncIterator[None]:
        async with guarded.router.lifespan_context(guarded):
            yield

    outer = Starlette(routes=[*hoisted, Mount("/", app=guarded)], lifespan=lifespan)
    # Outermost, so it runs before FastMCP's guard and before the mount. `/health`
    # is exempt for the same reason it is hoisted at all.
    return StrictHostGuard(outer, security=security, exempt=UNGUARDED_PATHS)


def serve_mcp_http(
    mcp: Any,
    *,
    host: str,
    port: int,
    path: str = "/mcp",
    asgi_middleware: Sequence[Any] = (),
    log_level: str = "info",
) -> None:
    """**The only supported way this repo serves an MCP server over HTTP.**

    It exists because the alternative was five places that each re-read the environment
    and decided part of the answer. That is not a style objection: the bind address and
    the `Host` allowlist have to be decided *together*, and when they were not, both
    servers bound ``0.0.0.0`` while accepting only localhost `Host` headers -- every
    request through the ingress got 421 while ``/health`` returned 200, so all pods
    reported Ready with the product API unreachable. It happened twice.

    Here they are arguments to one call, so they cannot disagree. FastMCP 4 helps: there
    is no instance ``.settings``, and ``FastMCP(host=...)`` raises outright, so the
    construct-then-mutate pattern that caused that outage cannot be expressed at all.

    ``asgi_middleware`` is **ASGI**, not FastMCP middleware, and that is deliberate.
    FastMCP-native middleware refuses by raising ``ToolError``/``McpError``, which surfaces
    as an MCP protocol error -- the docs are explicit that MCP "operates at the transport
    layer above HTTP semantics". Our refusals must be HTTP: LiteLLM reads
    ``_meta.server_outcomes`` for an ``http_status``, `docs/consumer-onboarding.md`
    documents 401/403/421 as the consumer contract, and the identity middleware answers
    401 with a JSON body. An MCP-level middleware can produce none of that.
    """
    import uvicorn

    app = build_mcp_http_app(mcp, path=path, asgi_middleware=asgi_middleware)
    uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level=log_level.lower())).run()


def build_transports_from_env() -> tuple[ExtractionTransport, DreamAgentTransport | None]:
    """Build extraction and dream-agent transports from process environment.

    ``LITELLM_API_KEY`` (JedAI Gateway) wins; ``OPENAI_API_KEY`` is the generic
    OpenAI-compatible alternative; neither configured means deterministic
    rule-based extraction and no dream agent.

    T1-14: reads the environment ONLY.  Loading a ``.env`` belongs to the entry
    point that knows the project root -- ``build_platform_from_project`` already
    does it for every CLI path -- because a builder that re-reads a file cannot
    be cleared by a caller that deletes the key.
    """

    endpoint = _env_endpoint()
    if endpoint is None:
        return RuleBasedExtractionTransport(), None
    api_key_env, base_url, model = endpoint
    return (
        OpenAICompatibleExtractionTransport(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
        ),
        # Same key/endpoint as extraction; MEMOTRON_DREAM_AGENT_MODEL still
        # selects a distinct decision model when an operator sets it.
        OpenAICompatibleDreamAgentTransport(
            model=os.environ.get(DREAM_AGENT_MODEL_ENV, "").strip() or model,
            base_url=base_url,
            api_key_env=api_key_env,
        ),
    )


def build_synthesis_transport_from_credentials(
    credentials: dict[str, Any] | None,
) -> SynthesisTransport | None:
    """WS-18 T19: synthesis transport from a sealed tenant credential record.

    Reuses the SAME sealed extraction credential (provider / api_key / model /
    base_url) — one tenant LLM configuration powers extraction, rollup
    synthesis, and the session outcome judge.  ``None`` credentials mean no
    endpoint is configured and resolution falls through to the caller's next
    source (env, then no transport — rollup text stays deterministic and the
    judge is skipped; zero behavior change).
    """
    if credentials is None:
        return None
    provider = str(credentials.get("provider", "")).strip().lower()
    api_key = str(credentials.get("api_key", "")).strip()
    if not provider or not api_key:
        return None
    if provider not in SUPPORTED_PROVIDERS:
        _reject_provider(provider)
    return OpenAICompatibleSynthesisTransport(
        model=_resolved_model(str(credentials.get("model", "")), provider),
        base_url=_resolved_base_url(str(credentials.get("base_url", "")), provider),
        api_key=api_key,
    )


def build_synthesis_transport_from_env() -> SynthesisTransport | None:
    """WS-18 T19: synthesis transport from process environment.

    Same key sources and precedence as :func:`build_transports_from_env` —
    ``LITELLM_API_KEY`` first, then ``OPENAI_API_KEY``; neither configured
    returns None (rollup text stays deterministic; the session judge is
    skipped).

    T1-14: environment only -- see :func:`build_transports_from_env`."""
    endpoint = _env_endpoint()
    if endpoint is None:
        return None
    api_key_env, base_url, model = endpoint
    return OpenAICompatibleSynthesisTransport(
        model=model,
        base_url=base_url,
        api_key_env=api_key_env,
    )


def build_synthesis_transport_from_tenant_graph(
    *,
    graph_path: str | Path,
    tenant_id: str,
) -> SynthesisTransport | None:
    """WS-18 T19: resolve the synthesis transport for one tenant.

    Precedence mirrors the embedding builder: sealed tenant credential entry,
    then environment; None when neither is configured."""
    from memotron.graph import PropertyGraphStore

    store = PropertyGraphStore(graph_path)
    try:
        credentials = store.tenant_llm_credentials(tenant_id)
    finally:
        store.close()
    transport = build_synthesis_transport_from_credentials(credentials)
    if transport is not None:
        return transport
    return build_synthesis_transport_from_env()


def build_embedding_transport_from_credentials(
    credentials: dict[str, Any] | None,
) -> EmbeddingTransport | None:
    """WS-17 T18: embedding transport from a sealed tenant credential record.

    The tenant credential schema is extended additively with
    ``embedding_provider`` / ``embedding_base_url`` / ``embedding_model``; a
    blank ``embedding_provider`` means no embedding endpoint is configured and
    resolution falls through to the caller's next source (env, then the
    hermetic ``LocalEmbeddingTransport`` default — zero behavior change).  The
    OpenAI-compatible embedding endpoint reuses the tenant's sealed API key.
    """
    if credentials is None:
        return None
    provider = str(credentials.get("embedding_provider", "")).strip().lower()
    if not provider:
        return None
    if provider == "local":
        return LocalEmbeddingTransport()
    if provider in {"openai", "litellm"}:
        model = str(credentials.get("embedding_model", "")).strip()
        if not model:
            raise ValueError("embedding_model cannot be blank for an OpenAI-compatible embedding provider")
        api_key = str(credentials.get("api_key", "")).strip()
        if not api_key:
            raise ValueError("api_key cannot be blank")
        return OpenAICompatibleEmbeddingTransport(
            model=model,
            base_url=(
                str(credentials.get("embedding_base_url", "")).strip()
                or str(credentials.get("base_url", "")).strip()
                or _resolved_base_url("", provider)
            ),
            api_key=api_key,
        )
    raise ValueError("embedding_provider must be 'openai', 'litellm', or 'local'")


def build_embedding_transport_from_env() -> EmbeddingTransport | None:
    """WS-17 T18: embedding transport from process environment.

    Reads ``MEMOTRON_EMBEDDING_PROVIDER`` (``openai``/``litellm``/``local``),
    ``MEMOTRON_EMBEDDING_BASE_URL``, ``MEMOTRON_EMBEDDING_MODEL``, and
    ``MEMOTRON_EMBEDDING_API_KEY_ENV`` (the NAME of the env var holding the
    key — same indirection contract as extraction's ``api_key_env``; the key is
    resolved fail-fast at embed time, never here).  An unset provider returns
    None so callers keep the hermetic ``LocalEmbeddingTransport`` default.

    Defaults for a ``litellm`` provider are the JedAI Gateway base URL and
    ``LITELLM_API_KEY``; ``MEMOTRON_EMBEDDING_MODEL`` stays REQUIRED because
    the model IS the vector space — ``text-embedding-3`` (3072 dims) is the
    documented gateway choice, and a tenant that already stored vectors under
    another identifier must not be switched silently.

    T1-14: environment only -- see :func:`build_transports_from_env`.
    """
    provider = os.environ.get("MEMOTRON_EMBEDDING_PROVIDER", "").strip().lower()
    if not provider:
        return None
    if provider == "local":
        return LocalEmbeddingTransport()
    if provider in {"openai", "litellm"}:
        model = os.environ.get("MEMOTRON_EMBEDDING_MODEL", "").strip()
        if not model:
            raise ValueError("MEMOTRON_EMBEDDING_MODEL is required when MEMOTRON_EMBEDDING_PROVIDER is set")
        return OpenAICompatibleEmbeddingTransport(
            model=model,
            base_url=(os.environ.get("MEMOTRON_EMBEDDING_BASE_URL", "").strip() or _resolved_base_url("", provider)),
            api_key_env=(
                os.environ.get("MEMOTRON_EMBEDDING_API_KEY_ENV", "").strip()
                or (GATEWAY_API_KEY_ENV if provider == "litellm" else "OPENAI_API_KEY")
            ),
        )
    raise ValueError("MEMOTRON_EMBEDDING_PROVIDER must be 'openai', 'litellm', or 'local'")


def build_embedding_transport_from_tenant_graph(
    *,
    graph_path: str | Path,
    tenant_id: str,
) -> EmbeddingTransport | None:
    """WS-17 T18: resolve the embedding transport for one tenant.

    Precedence: sealed tenant credential embedding entry, then environment;
    None when neither is configured (callers default to the hermetic
    ``LocalEmbeddingTransport`` — zero behavior change).
    """
    from memotron.graph import PropertyGraphStore

    store = PropertyGraphStore(graph_path)
    try:
        credentials = store.tenant_llm_credentials(tenant_id)
    finally:
        store.close()
    transport = build_embedding_transport_from_credentials(credentials)
    if transport is not None:
        return transport
    return build_embedding_transport_from_env()


def seed_tenant_llm_credentials_from_env(
    *,
    graph_path: str | Path,
    tenant_id: str,
) -> dict[str, Any] | None:
    """Seal the local environment's gateway credential for a tenant, if unsealed.

    Seeds ``provider="litellm"`` from ``LITELLM_API_KEY`` + ``LITELLM_API_BASE``
    (or a generic ``OPENAI_API_KEY`` endpoint when that is what the environment
    offers).  Never overwrites an existing sealed credential, and returns None
    when nothing is configured.

    T1-14: environment only.  This one matters most -- it SEALS what it finds,
    so a stray ``.env`` did not merely bill a call, it wrote the wrong key into
    the graph durably, and the early return on an existing row (below) means the
    repair path never runs.  Callers that want ``.env`` honoured must load it
    against the project root first, as ``_ensure_project_llm_credentials`` does.
    """

    endpoint = _env_endpoint()
    if endpoint is None:
        return None
    api_key_env, base_url, model = endpoint
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        return None
    provider = "litellm" if api_key_env == GATEWAY_API_KEY_ENV else "openai"

    from memotron.storage import open_storage

    store = open_storage(graph_path)
    try:
        existing = store.tenant_llm_credentials(tenant_id)
        if existing is not None:
            return store.tenant_llm_credential_state(tenant_id)
        return store.set_tenant_llm_credentials(
            tenant_id=tenant_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
    finally:
        store.close()
