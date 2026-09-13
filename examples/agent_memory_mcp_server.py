"""Entry point for the Memotron agent-memory MCP server.

Serves an agent-friendly memory workflow over Streamable HTTP:
  POST /mcp
  GET  /health

Environment variables
---------------------
MEMOTRON_GRAPH_PATH   SQLite graph file (default: .memotron/agent-memory.sqlite).
MEMOTRON_PROJECT_ID   Project/tenant id (default: jedai-platform).
MEMOTRON_PROJECT_NAME Optional display name.
MEMOTRON_AGENT_IDS    Required comma-separated agent ids.
MCP_HOST                 Bind host (default: 127.0.0.1).
MCP_PORT                 Bind port (default: 8010).
MEMOTRON_MCP_ALLOWED_HOSTS
                         Comma-separated `Host` values to accept beyond localhost.
                         UNSET MEANS LOCALHOST ONLY, so a deployment behind an ingress
                         must set it or every request gets 421. `*` disables the check.
LITELLM_API_KEY          JedAI Gateway virtual key. Enables Claude extraction,
                         rollup synthesis, and dream-agent decisions.
LITELLM_API_BASE         Gateway base URL override, including /v1
                         (default: https://preview.jedai-gateway.wdprapps.disney.com/v1).
MEMOTRON_LLM_MODEL    Undated gateway model alias (default: claude-haiku-4-5).
MEMOTRON_REQUIRE_GATEWAY_IDENTITY
                         Arm the per-request identity guard (#126). Unset by default,
                         which is the current unauthenticated behaviour. When set, this
                         process resolves each caller's virtual key via the gateway admin
                         plane, so THE GATEWAY BECOMES A HARD DEPENDENCY of every memory
                         operation. See `memotron.mcp_auth`.
LOG_LEVEL                uvicorn's log level: DEBUG | INFO | WARNING | ERROR (default:
                         INFO). It no longer sets the MCP server's own logger --
                         FastMCP 4 dropped the `log_level` constructor argument, and
                         this entry point does not call `configure_observability`.

Usage
-----
  MEMOTRON_AGENT_IDS=platform,sdk uv run examples/agent_memory_mcp_server.py
  (with LITELLM_API_KEY exported or present in the gitignored .env)
"""

from __future__ import annotations

import os

from memotron.agent_memory_mcp import (
    MCP_HOST,
    MCP_PORT,
    build_platform_from_env,
    get_platform,
    mcp,
)
from memotron.gateway_identity import resolve_gateway_identity
from memotron.mcp_auth import GatewayIdentityMiddleware
from memotron.runtime import mcp_bind_from_env, serve_mcp_http


def _serve() -> None:
    """Serve over HTTP with the identity middleware installed.

    All of the serving decisions -- bind address, `Host` allowlist, stateless sessions,
    mount path -- belong to `runtime.serve_mcp_http`, which this and
    `examples/mcp_server.py` both call. The only thing that differs between them is the
    principal lookup below, which is the point: two entry points that must agree about
    security now agree by construction rather than by both being edited.

    `GatewayIdentityMiddleware` is ASGI, not FastMCP middleware, and deliberately so.
    A FastMCP-native middleware refuses by raising, which surfaces as an MCP protocol
    error -- but our refusals have to be HTTP: LiteLLM reads `_meta.server_outcomes` for
    an `http_status`, and `docs/consumer-onboarding.md` documents 401/403/421 as the
    consumer contract. An MCP-level refusal produces none of those.

    Until `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is set the wrapper returns on the first
    line of `__call__`, so this changes no behaviour today. That is deliberate: #206
    Phase 1 is the wiring, and the authorization it enables is Phase 2, in
    `AgentMemoryPlatform` beneath both transports. Arming the flag before Phase 2 buys
    authentication without scope authorization -- a caller would be identified and then
    permitted anything the process's `project_id` can reach.
    """
    from starlette.middleware import Middleware

    host, port = mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT)
    serve_mcp_http(
        mcp,
        host=host,
        port=port,
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
        # `Middleware(cls, **kwargs)`, never the bare class. Starlette builds its stack
        # lazily on the first request, so a bare class constructs fine, passes any
        # start-up check, and then 500s on the first real call with
        # `TypeError: cannot unpack non-iterable type object`.
        asgi_middleware=[
            Middleware(
                GatewayIdentityMiddleware,
                resolver=resolve_gateway_identity,
                # Resolved at request time, not captured now: binding the bound method
                # here would pin this to whichever backend existed at startup and
                # quietly survive a rebuild.
                principal_lookup=lambda alias: get_platform().client.graph.principal_for_key_alias(alias),
            )
        ],
    )


def main() -> None:
    """Build the platform, then serve.

    THIS FUNCTION USED TO REASSIGN `mcp.settings.host` AND `.port` AFTER CONSTRUCTION.
    Under the old SDK, FastMCP resolved transport security -- including the `Host`
    allowlist -- when the server object was constructed, at import of
    `agent_memory_mcp`; assigning `host` afterwards moved the bind address without
    moving the allowlist. That binds `0.0.0.0` while accepting only localhost `Host`
    headers, so every request through an ingress gets 421 while `/health` stays 200 and
    the service looks healthy while being unusable.

    It was worse here than in the module it serves: `agent_memory_mcp` read `MCP_HOST`
    defaulting to `127.0.0.1`, this file re-read the same variable defaulting to
    `0.0.0.0` and assigned the result after the fact -- so with `MCP_HOST` unset the two
    disagreed by construction. Never deployed, so it never fired.

    It cannot be written again. There is no `mcp.settings` under FastMCP 4, and bind
    address and allowlist are now arguments to the same `serve_mcp_http` call.
    """
    build_platform_from_env()
    _serve()


if __name__ == "__main__":
    main()
