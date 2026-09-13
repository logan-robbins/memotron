"""Entry point for the Memotron MCP server.

Serves the Memotron SDK as MCP tools over the Streamable-HTTP transport:
  POST /mcp   — MCP protocol (Claude Desktop, Claude Code, any MCP client)
  GET  /health — K8s liveness / readiness probe

Environment variables
---------------------
MEMOTRON_GRAPH_PATH   SQLite graph file (default: .memotron/mcp.sqlite locally,
                         /data/memotron.sqlite in K8s when PVC is mounted).
MCP_HOST                 Bind host (default: 0.0.0.0).
MCP_PORT                 Bind port (default: 8000).
LITELLM_API_KEY          JedAI Gateway virtual key. Enables Claude extraction,
                         rollup synthesis, and dream-agent decisions.
LITELLM_API_BASE         Gateway base URL override, including /v1
                         (default: https://preview.jedai-gateway.wdprapps.disney.com/v1).
MEMOTRON_LLM_MODEL    Undated gateway model alias (default: claude-haiku-4-5).
MEMOTRON_REQUIRE_GATEWAY_IDENTITY
                         Arm the per-request identity guard (#126). Unset by
                         default, which is the current unauthenticated behaviour.
                         When set, this process resolves each caller's virtual key
                         via the gateway admin plane, so THE GATEWAY BECOMES A HARD
                         DEPENDENCY of every memory operation. See
                         `memotron.mcp_auth` and `.helm/values.yaml`.
LOG_LEVEL                DEBUG | INFO | WARNING | ERROR (default: INFO).

Usage
-----
  uv run examples/mcp_server.py                  # local dev (in-memory graph)
  uv run --no-sync examples/mcp_server.py        # production (K8s, reads env)

Docker / K8s command
-----
  uv run --no-sync examples/mcp_server.py
"""

from __future__ import annotations

import os

from memotron.gateway_identity import resolve_gateway_identity
from memotron.mcp_auth import GatewayIdentityMiddleware
from memotron.mcp_server import MCP_HOST, MCP_PORT, build_client, get_client, mcp
from memotron.observability import configure_observability
from memotron.runtime import mcp_bind_from_env, serve_mcp_http

_GRAPH_PATH = os.environ.get("MEMOTRON_GRAPH_PATH", ".memotron/mcp.sqlite")


def main() -> None:
    # #129. FIRST, before build_client: that call constructs transports and a storage
    # backend, both of which log. Configuring afterwards would drop exactly the startup
    # lines that say what this process connected to.
    #
    # This is the API surface the chart runs (`.helm/values.yaml:73` invokes THIS file, not
    # `memotron.mcp_server`, which has no main()), so it is the one whose telemetry
    # reaches the deployed collector.
    #
    # Still NOT wrapped with `wrap_asgi_with_otel`. The original blocker -- `mcp.run()`
    # builds its ASGI app internally and never exposes it -- is long gone, and this file
    # no longer holds an app object either: `runtime.serve_mcp_http` owns it now, so that
    # is where the wrap belongs.
    #
    # What remains is only that nobody has done it. Logs and metrics export from here;
    # per-request HTTP SPANS still do not. #129 is a small change in `serve_mcp_http`
    # rather than a blocked one. Recorded rather than quietly skipped.
    configure_observability(service_name="memotron-api")

    build_client(graph_path=_GRAPH_PATH)
    _serve()


def _serve() -> None:
    """Serve over HTTP with the identity middleware installed.

    Every serving decision lives in `runtime.serve_mcp_http`: bind address, `Host`
    allowlist, stateless sessions, mount path. This function supplies only what is
    specific to *this* server -- its bind defaults and its principal lookup.

    That consolidation is the fix for a real outage, not tidiness. This file used to
    assign `mcp.settings.host` after construction; under the old SDK the `Host`
    allowlist was resolved at construction and the assignment did not re-evaluate it, so
    the server bound `0.0.0.0` while accepting localhost `Host` headers only. Every
    request through the ingress got 421 "Invalid Host header" while `/health` -- outside
    that middleware -- returned 200, so every pod reported Ready with the product
    unreachable. Bind address and allowlist are now arguments to one call and cannot
    disagree; `mcp.settings` no longer exists to be assigned.

    `GatewayIdentityMiddleware` stays **ASGI** rather than becoming FastMCP middleware.
    FastMCP middleware refuses by raising, which surfaces as an MCP protocol error, and
    our refusals must be HTTP: LiteLLM reads `_meta.server_outcomes` for an `http_status`
    and `docs/consumer-onboarding.md` documents 401/403/421 as the consumer contract.

    With `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` unset the wrapper is a pass-through on
    the first line of `__call__`, so deployed behaviour is unchanged until the flag flips.

    #129: per-request HTTP spans are still not exported. `serve_mcp_http` now owns the
    app object, so `wrap_asgi_with_otel` belongs there rather than here -- still a small
    change, still not done, recorded rather than quietly dropped.
    """
    from starlette.middleware import Middleware

    host, port = mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT)
    serve_mcp_http(
        mcp,
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        # `Middleware(cls, **kwargs)`, never the bare class. Starlette builds its stack
        # lazily on the first request, so a bare class constructs fine, survives any
        # start-up check, and then 500s on the first real call with
        # `TypeError: cannot unpack non-iterable type object`.
        asgi_middleware=[
            Middleware(
                GatewayIdentityMiddleware,
                resolver=resolve_gateway_identity,
                # `client.graph` IS the storage backend -- the attribute keeps its
                # original name from when SQLite was the only one, and it is a
                # PostgresStorageBackend in the deployed environments. Resolved at
                # request time rather than captured now: binding the bound method here
                # would pin this to whichever backend existed at startup and quietly
                # survive a client rebuild.
                principal_lookup=lambda alias: get_client().graph.principal_for_key_alias(alias),
            )
        ],
    )


if __name__ == "__main__":
    main()
