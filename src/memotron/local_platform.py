"""Run Memotron locally as a single shared platform process.

The process owns one SQLite graph, the React admin UI/API, and the agent-memory
MCP server. Tenant LLM credentials are read from the graph at startup and can be
updated live through the admin UI.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from http.server import HTTPServer
from pathlib import Path
from queue import Queue
from threading import Event, Thread

from memotron import AgentMemoryMode, AgentMemoryPlatform, PrincipalRole
from memotron.admin_server import (
    DEFAULT_ADMIN_STATIC_DIR,
    MemoryGraphHandler,
    build_demo_principal,
    parse_scope_key,
    warn_if_admin_build_missing,
)
from memotron.agent_memory import project_scope
from memotron.agent_memory_mcp import install_platform, mcp
from memotron.observability import configure_observability
from memotron.runtime import (
    build_embedding_transport_from_tenant_graph,
    build_synthesis_transport_from_tenant_graph,
    build_transports_from_tenant_graph,
    load_env_file,
    seed_tenant_llm_credentials_from_env,
    serve_mcp_http,
)

_log = logging.getLogger(__name__)


def display_host(host: str) -> str:
    return "127.0.0.1" if host in {"0.0.0.0", "::"} else host


async def run_maintenance_cycle(platform: AgentMemoryPlatform) -> int:
    """Run due jobs once for every registered agent and the shared project."""

    agent_ids = tuple(platform.agent_ids)
    if not agent_ids:
        return 0
    job_runs = 0
    for agent_id in agent_ids:
        refreshed = await platform.memory_refresh(
            agent_id=agent_id,
            include_project=False,
        )
        job_runs += len(refreshed.agent_run.job_runs)
        if refreshed.user_run is not None:
            job_runs += len(refreshed.user_run.job_runs)
    project_run = await platform.client.run_due_dreams(
        tenant_id=platform.tenant_id,
        agent_id=agent_ids[0],
        scope=platform.project_scope,
    )
    job_runs += len(project_run.job_runs)
    return job_runs


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Memotron platform.")
    parser.add_argument("--graph-path", default=".memotron/local-platform.sqlite")
    parser.add_argument("--tenant-id", default="local-platform")
    parser.add_argument("--project-name", default="")
    parser.add_argument(
        "--mode",
        choices=[mode.value for mode in AgentMemoryMode],
        default=AgentMemoryMode.SIMPLE.value,
    )
    parser.add_argument("--user-id", default="")
    parser.add_argument("--scope", default="")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--ui-host", default="")
    parser.add_argument("--mcp-host", default="")
    parser.add_argument("--ui-port", type=int, default=8765)
    parser.add_argument("--mcp-port", type=int, default=8010)
    parser.add_argument(
        "--maintenance-interval-seconds",
        type=float,
        default=15.0,
    )
    parser.add_argument("--static-dir", default=str(DEFAULT_ADMIN_STATIC_DIR))
    parser.add_argument(
        "--env-file",
        default=".env",
        help=(
            "Path to a .env read at startup, resolved against the current directory "
            "(default: .env). Set to '' to read the process environment only. "
            "T1-14: this launcher loads it ONCE, explicitly, here -- the transport "
            "builders never read a .env on their own."
        ),
    )
    parser.add_argument("--principal-id", default="local-admin")
    parser.add_argument(
        "--principal-role",
        choices=[role.value for role in PrincipalRole],
        default=PrincipalRole.ADMIN.value,
    )
    args = parser.parse_args()

    # #129. Before anything else in main(): a process with no log handler is worse
    # than one with no traces, and setup_telemetry must precede any ASGI wrapping.
    configure_observability(service_name="memotron-local-platform")
    if args.maintenance_interval_seconds <= 0:
        raise SystemExit("--maintenance-interval-seconds must be greater than zero")

    graph_path = Path(args.graph_path).expanduser().resolve()
    agent_ids: tuple[str, ...] = ()
    admin_agent_id = args.principal_id
    default_scope = parse_scope_key(args.scope) if args.scope else project_scope(args.tenant_id)
    ui_host = args.ui_host or args.host
    mcp_host = args.mcp_host or args.host
    ui_url = f"http://{display_host(ui_host)}:{args.ui_port}/"
    mcp_url = f"http://{display_host(mcp_host)}:{args.mcp_port}/mcp"

    # T1-14: the launcher owns .env loading, because it is an ENTRY POINT and can
    # do it once, visibly, before anything reads the environment. The transport
    # builders below deliberately do not -- a library function that re-reads a file
    # cannot be cleared by a caller that deletes the key. README documents that this
    # launcher reads the repository .env, so removing it here would be a silent
    # regression rather than a fix.
    if args.env_file:
        env_path = Path(args.env_file).expanduser()
        if env_path.is_file():
            _log.info("loading environment from %s", env_path.resolve())
            load_env_file(env_path)

    seed_tenant_llm_credentials_from_env(
        graph_path=graph_path,
        tenant_id=args.tenant_id,
    )
    extraction_transport, dream_agent_transport = build_transports_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=args.tenant_id,
    )
    # WS-17 T18: None means "nothing configured" — the client keeps the hermetic
    # LocalEmbeddingTransport default.
    embedding_transport = build_embedding_transport_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=args.tenant_id,
    )
    rollup_synthesis_transport = build_synthesis_transport_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=args.tenant_id,
    )
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id=args.tenant_id,
        project_name=args.project_name or None,
        agent_ids=agent_ids,
        extraction_transport=extraction_transport,
        dream_agent_transport=dream_agent_transport,
        embedding_transport=embedding_transport,
        rollup_synthesis_transport=rollup_synthesis_transport,
        mode=args.mode,
        user_id=args.user_id or None,
    )
    configured_scopes = (
        platform.project_scope,
        *((platform.user_scope,) if platform.user_scope is not None else ()),
    )
    install_platform(platform, platform_api_url=ui_url, mcp_url=mcp_url)

    ui_queue: Queue[HTTPServer | BaseException] = Queue()

    def serve_ui() -> None:
        try:
            ui_extraction_transport, ui_dream_agent_transport = build_transports_from_tenant_graph(
                graph_path=graph_path,
                tenant_id=args.tenant_id,
            )
            ui_embedding_transport = build_embedding_transport_from_tenant_graph(
                graph_path=graph_path,
                tenant_id=args.tenant_id,
            )
            ui_synthesis_transport = build_synthesis_transport_from_tenant_graph(
                graph_path=graph_path,
                tenant_id=args.tenant_id,
            )
            ui_platform = AgentMemoryPlatform.create(
                graph_path=graph_path,
                project_id=args.tenant_id,
                project_name=args.project_name or None,
                agent_ids=agent_ids,
                extraction_transport=ui_extraction_transport,
                dream_agent_transport=ui_dream_agent_transport,
                embedding_transport=ui_embedding_transport,
                rollup_synthesis_transport=ui_synthesis_transport,
                mode=args.mode,
                user_id=args.user_id or None,
            )
            ui_platform.register_agent(
                agent_id=admin_agent_id,
                agent_name=f"{admin_agent_id} agent",
                source="admin",
            )
            AgentMemoryPlatform.apply_tenant_prompt_override_to_client(
                client=ui_platform.client,
                tenant_id=args.tenant_id,
            )
            # Annotated, not suppressed -- same fix and same reason as
            # admin_server/__init__.py:2069 (#138). Three-arg `type()` infers as bare `type`,
            # which discarded the base and made all 13 following assignments `attr-defined`
            # errors; MemoryGraphHandler already declares each attribute with a type.
            handler: type[MemoryGraphHandler] = type("LocalPlatformMemoryGraphHandler", (MemoryGraphHandler,), {})
            handler.client = ui_platform.client
            handler.runtime_client = platform.client
            handler.platform = ui_platform
            handler.default_scope = default_scope
            handler.graph_path = graph_path
            handler.static_dir = Path(args.static_dir).expanduser().resolve()
            handler.tenant_id = args.tenant_id
            handler.agent_ids = agent_ids
            handler.configured_scopes = configured_scopes
            handler.ui_url = ui_url
            handler.mcp_url = mcp_url
            handler.control_plane = ui_platform.client.control_plane
            handler.principal = build_demo_principal(
                principal_id=args.principal_id,
                tenant_id=args.tenant_id,
                agent_id=admin_agent_id,
                default_scope=default_scope,
                role=PrincipalRole(args.principal_role),
                allowed_scopes=configured_scopes,
            )
            ui_server = HTTPServer((ui_host, args.ui_port), handler)
            ui_queue.put(ui_server)
            try:
                ui_server.serve_forever()
            finally:
                ui_server.server_close()
                ui_platform.client.graph.close()
        except BaseException as exc:
            ui_queue.put(exc)
            raise

    ui_thread = Thread(target=serve_ui, daemon=True)
    ui_thread.start()
    ui_server_or_error = ui_queue.get(timeout=10)
    if isinstance(ui_server_or_error, BaseException):
        raise ui_server_or_error
    ui_server = ui_server_or_error

    maintenance_stop = Event()

    def maintain_memory() -> None:
        while not maintenance_stop.is_set():
            maintenance_platform: AgentMemoryPlatform | None = None
            try:
                maintenance_extraction, maintenance_agent = build_transports_from_tenant_graph(
                    graph_path=graph_path,
                    tenant_id=args.tenant_id,
                )
                maintenance_embedding = build_embedding_transport_from_tenant_graph(
                    graph_path=graph_path,
                    tenant_id=args.tenant_id,
                )
                maintenance_synthesis = build_synthesis_transport_from_tenant_graph(
                    graph_path=graph_path,
                    tenant_id=args.tenant_id,
                )
                maintenance_platform = AgentMemoryPlatform.create(
                    graph_path=graph_path,
                    project_id=args.tenant_id,
                    project_name=args.project_name or None,
                    extraction_transport=maintenance_extraction,
                    dream_agent_transport=maintenance_agent,
                    embedding_transport=maintenance_embedding,
                    rollup_synthesis_transport=maintenance_synthesis,
                    mode=args.mode,
                    user_id=args.user_id or None,
                )
                asyncio.run(run_maintenance_cycle(maintenance_platform))
            except Exception:
                _log.exception("Memotron automatic maintenance cycle failed")
            finally:
                if maintenance_platform is not None:
                    maintenance_platform.client.graph.close()
            maintenance_stop.wait(args.maintenance_interval_seconds)

    maintenance_thread = Thread(
        target=maintain_memory,
        name="memotron-maintenance",
        daemon=True,
    )
    maintenance_thread.start()

    print(f"Memotron admin UI:  {ui_url}")
    print(f"Memotron platform:  {ui_url}api/platform/status")
    print(f"Memotron contract:  {ui_url}api/platform/integration-contract")
    print(f"Memotron MCP:       {mcp_url}")
    print(f"graph_path={graph_path}")
    print(f"static_dir={Path(args.static_dir).expanduser().resolve()}")
    # Same check and same reason as admin_server's entry point (T2-10). The emitter flushes,
    # unlike every other print in this block: found by running it under nohup, where Python
    # block-buffers a non-tty stdout so this whole block -- warning included -- never reached
    # the log. A warning nobody can see is the exact failure being fixed. The rest of this
    # block is still unflushed and still invisible in a container; that is worth fixing
    # wholesale and is not this change.
    warn_if_admin_build_missing(Path(args.static_dir))
    print(f"tenant_id={args.tenant_id}")
    print(f"mode={args.mode}")
    print("agents=explicit-registration-required")
    interrupted = False
    try:
        # `serve_mcp_http`, not `mcp.run()`. They are not equivalent: FastMCP 4
        # defaults `http_host_origin_protection` to False, so `mcp.run()` accepts any
        # `Host` header. That is survivable while `--mcp-host` stays on loopback and
        # is not once someone passes `--mcp-host 0.0.0.0`, which this parser allows.
        # Going through the shared helper means local dev gets the same fail-closed
        # default as the deployed servers instead of its own weaker one.
        serve_mcp_http(mcp, host=mcp_host, port=args.mcp_port)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        maintenance_stop.set()
        maintenance_thread.join(timeout=5)
        if not interrupted:
            ui_server.shutdown()
            ui_thread.join(timeout=5)
        platform.client.graph.close()


if __name__ == "__main__":
    main()
