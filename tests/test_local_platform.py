from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import memotron.agent_memory_mcp as agent_memory_mcp
from memotron import AgentMemoryPlatform, Memotron, MemoryScope, PrincipalRole, ScopeKind
from memotron.admin_server import (
    DEMO_TENANT_ID,
    MemoryGraphHandler,
    apply_persisted_tenant_policy,
    build_demo_control_plane,
    build_demo_principal,
    operator_tenant_detail,
    parse_datetime,
    resolve_launch_tenant_id,
    resolve_scope_motive,
    tenant_config_payload,
    tenant_prompts_payload,
)
from memotron.admin_server._tenant import _environment_llm_key_env
from memotron.agent_memory import PROJECT_MEMORY_POLICY_MOTIVE
from memotron.gateway import GATEWAY_API_KEY_ENV


def load_example_module(name: str):
    module_path = Path(__file__).resolve().parents[1] / "examples" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(name, module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(target: str | Request) -> dict:
    return json.loads(urlopen(target, timeout=5).read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    return read_json(
        Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )


def test_admin_parse_datetime_normalizes_date_picker_values() -> None:
    assert parse_datetime("2026-07-09") == datetime(2026, 7, 9, 23, 59, 59, 999999, tzinfo=UTC)
    assert parse_datetime("2026-07-09T12:00:00") == datetime(2026, 7, 9, 12, tzinfo=UTC)
    assert parse_datetime("2026-07-09T05:00:00-07:00") == datetime(2026, 7, 9, 12, tzinfo=UTC)


def test_environment_llm_key_env_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both positive branches of the env-credential probe, pinned deterministically.

    These two lines used to be covered only by accident.  The test below scrubs
    the environment on purpose, so it exercises the ``return ""`` branch; nothing
    exercised the two that return a variable NAME.  They showed as covered anyway
    whenever the developer running the suite had a gateway key exported -- which
    is how ``admin_server._tenant``'s 87% floor came to be blessed.  Once
    ``tests/conftest.py`` made the environment hermetic the accident stopped and
    the module fell to 84%, which is the honest number for what was actually
    tested.

    Precedence is the real subject: the gateway key must win over
    ``OPENAI_API_KEY``, mirroring ``runtime._env_endpoint``.  The third case sets
    BOTH, so a future change that reverses the order fails here rather than only
    in the runtime twin.
    """
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-gateway")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert _environment_llm_key_env() == GATEWAY_API_KEY_ENV

    monkeypatch.delenv(GATEWAY_API_KEY_ENV, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    assert _environment_llm_key_env() == "OPENAI_API_KEY"

    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-gateway")
    assert _environment_llm_key_env() == GATEWAY_API_KEY_ENV, (
        "the gateway key must win when both are set; this mirrors runtime._env_endpoint and the two must not drift"
    )

    # Whitespace is not a credential. `.strip()` is what makes an exported-but-empty
    # variable -- the shape `export LITELLM_API_KEY=` leaves behind -- read as absent.
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "   ")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert _environment_llm_key_env() == ""


@pytest.mark.asyncio
async def test_tenant_config_api_redacts_and_clears_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # This test's "no credential configured" assertions must hold regardless
    # of the ambient process environment (e.g. a developer's or a live-demo
    # worktree's exported gateway key) -- admin_server now layers that
    # environment fallback onto the sealed-credential status (see
    # effective_llm_status), so pin the environment hermetically here.
    # T1-14: clear_tenant_llm_credentials() rebuilds transports from env, and that
    # path used to call a bare runtime.load_env_file() which re-populated the key
    # from a ".env" in the repo root -- defeating the delenv below. It no longer
    # does, so these two deletions are now sufficient on their own and the
    # monkeypatch that used to be needed here has been removed. If a future change
    # reintroduces self-loading, this test goes red rather than passing on a patch
    # that silently keeps working.
    monkeypatch.delenv(GATEWAY_API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    graph_path = tmp_path / "tenant-config.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="local-platform")
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text('<div id="root">Memotron Admin</div>', encoding="utf-8")
    server_queue = Queue()

    def serve() -> None:
        handler = type("TenantConfigHandler", (MemoryGraphHandler,), {})
        client = Memotron(graph_path=graph_path)
        handler.client = client
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        handler.static_dir = static_dir
        handler.tenant_id = "local-platform"
        handler.agent_ids = ("consumer-app",)
        handler.configured_scopes = (default_scope,)
        handler.ui_url = "http://127.0.0.1:8765/"
        handler.mcp_url = "http://127.0.0.1:8010/mcp"
        handler.control_plane = build_demo_control_plane(
            client=client,
            default_scope=default_scope,
            tenant_id="local-platform",
            agent_id="consumer-app",
        )
        handler.principal = build_demo_principal(
            principal_id="local-admin",
            tenant_id="local-platform",
            agent_id="consumer-app",
            default_scope=default_scope,
            role=PrincipalRole.ADMIN,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        server_queue.put(server)
        try:
            server.serve_forever()
        finally:
            client.graph.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = server_queue.get(timeout=5)
    base_url = f"http://127.0.0.1:{server.server_port}"
    secret = "test-anthropic-key"
    try:
        initial = read_json(f"{base_url}/api/tenant-config")
        assert initial["llm"] == {"tenant_id": "local-platform", "has_api_key": False}
        assert "MemotronPlatformClient" in initial["snippets"]["sdk"]
        assert "agent_name=" in initial["snippets"]["sdk"]
        assert "memory_bootstrap" in initial["snippets"]["sdk"]
        assert "memory_start" in initial["snippets"]["sdk"]
        assert "memory_search" in initial["snippets"]["sdk"]
        assert "memory_log" in initial["snippets"]["sdk"]
        assert "memory_outcome" in initial["snippets"]["sdk"]
        assert 'checkpoint_reason="context_compaction"' in initial["snippets"]["sdk"]
        assert "memory_refresh" in initial["snippets"]["sdk"]
        assert "project_memory_config" in initial["snippets"]["sdk"]
        assert "memory_publish" in initial["snippets"]["sdk"]
        assert "Motive resolves automatically" in initial["snippets"]["sdk"]
        assert "memory_bootstrap" in initial["snippets"]["mcp"]
        assert "memory_start" in initial["snippets"]["mcp"]
        assert "memory_search" in initial["snippets"]["mcp"]
        assert "memory_log" in initial["snippets"]["mcp"]
        assert "memory_outcome" in initial["snippets"]["mcp"]
        assert '"checkpoint_reason": "context_compaction"' in initial["snippets"]["mcp"]
        assert "memory_refresh" in initial["snippets"]["mcp"]
        assert "project_memory_config" in initial["snippets"]["mcp"]
        assert "memory_publish" in initial["snippets"]["mcp"]
        assert "Motive resolves automatically" in initial["snippets"]["mcp"]
        assert "memotron_agent_memory" in initial["snippets"]["codex_mcp_config"]
        assert "required = true" in initial["snippets"]["codex_mcp_config"]
        assert "tool_timeout_sec = 120" in initial["snippets"]["codex_mcp_config"]
        assert "automatically" in initial["snippets"]["codex_mcp_config"]
        assert initial["integration"]["labels"] == [
            "No seeding required",
            "One-call session bootstrap",
            "Agent IDs and names are tenant-unique",
            "Motive resolves automatically",
        ]
        assert "test-anthropic-key" not in json.dumps(initial)

        overview = read_json(f"{base_url}/api/overview")
        assert overview["tenant"]["tenant_id"] == "local-platform"
        assert overview["tenant"]["agent_ids"] == ["consumer-app"]
        assert overview["operator"]["platform_api_url"] == "http://127.0.0.1:8765/"
        assert overview["operator"]["mcp_url"] == "http://127.0.0.1:8010/mcp"
        assert overview["llm"] == {"tenant_id": "local-platform", "has_api_key": False}
        assert overview["readiness"]["agents_observed"] is True
        assert overview["memory"]["visible_facts"] == 0
        assert overview["evolution"]["dream_run_count"] == 0

        with pytest.raises(HTTPError) as invalid_json:
            urlopen(
                Request(
                    f"{base_url}/api/tenant-config/llm",
                    data=b"{",
                    headers={"Content-Type": "application/json"},
                    method="POST",
                ),
                timeout=5,
            )
        assert invalid_json.value.code == 400
        assert "valid JSON" in invalid_json.value.read().decode("utf-8")

        with pytest.raises(HTTPError) as missing_provider:
            post_json(f"{base_url}/api/tenant-config/llm", {"api_key": secret})
        assert missing_provider.value.code == 400
        assert "provider is required" in missing_provider.value.read().decode("utf-8")

        saved = post_json(
            f"{base_url}/api/tenant-config/llm",
            {
                "provider": "litellm",
                "api_key": secret,
                "model": "claude-sonnet-4-6",
                "base_url": "https://preview.jedai-gateway.wdprapps.disney.com/v1",
            },
        )
        assert saved["llm"]["tenant_id"] == "local-platform"
        assert saved["llm"]["provider"] == "litellm"
        assert saved["llm"]["has_api_key"] is True
        assert saved["llm"]["model"] == "claude-sonnet-4-6"
        assert secret not in json.dumps(saved)
        inspector = Memotron(graph_path=graph_path)
        try:
            assert inspector.graph.tenant_llm_credentials("local-platform")["api_key"] == secret
        finally:
            inspector.graph.close()

        cleared = post_json(f"{base_url}/api/tenant-config/llm/clear", {})
        assert cleared["cleared"] is True
        assert cleared["config"]["llm"] == {"tenant_id": "local-platform", "has_api_key": False}
        inspector = Memotron(graph_path=graph_path)
        try:
            assert inspector.graph.tenant_llm_credential_state("local-platform") is None
        finally:
            inspector.graph.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_admin_server_serves_react_index_html(tmp_path: Path) -> None:
    graph_path = tmp_path / "ui.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="local-platform")
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div><script src="/assets/index.js"></script></body></html>',
        encoding="utf-8",
    )
    server_queue = Queue()

    def serve() -> None:
        handler = type("StaticAssetHandler", (MemoryGraphHandler,), {})
        client = Memotron(graph_path=graph_path)
        handler.client = client
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        handler.static_dir = static_dir
        handler.tenant_id = "local-platform"
        handler.agent_ids = ("consumer-app",)
        handler.configured_scopes = (default_scope,)
        handler.ui_url = "http://127.0.0.1:8765/"
        handler.mcp_url = "http://127.0.0.1:8010/mcp"
        handler.control_plane = build_demo_control_plane(
            client=client,
            default_scope=default_scope,
            tenant_id="local-platform",
            agent_id="consumer-app",
        )
        handler.principal = build_demo_principal(
            principal_id="local-admin",
            tenant_id="local-platform",
            agent_id="consumer-app",
            default_scope=default_scope,
            role=PrincipalRole.ADMIN,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        server_queue.put(server)
        try:
            server.serve_forever()
        finally:
            client.graph.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = server_queue.get(timeout=5)
    try:
        body = urlopen(f"http://127.0.0.1:{server.server_port}/", timeout=5).read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert '<div id="root"></div>' in body
    assert "/assets/index.js" in body


def test_platform_status_and_contract_are_black_box_safe(tmp_path: Path) -> None:
    graph_path = tmp_path / "hosted-contract.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="local-platform")
    server_queue = Queue()
    secret = "test-anthropic-key"

    def serve() -> None:
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id="local-platform",
        )
        platform.client.graph.set_tenant_llm_credentials(
            tenant_id="local-platform",
            provider="litellm",
            api_key=secret,
            base_url="https://preview.jedai-gateway.wdprapps.disney.com/v1",
            model="claude-sonnet-4-6",
        )
        handler = type("HostedContractHandler", (MemoryGraphHandler,), {})
        handler.client = platform.client
        handler.runtime_client = platform.client
        handler.platform = platform
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        handler.static_dir = tmp_path / "missing-dist"
        handler.tenant_id = "local-platform"
        handler.agent_ids = ()
        handler.configured_scopes = (default_scope,)
        handler.ui_url = "http://127.0.0.1:0/"
        handler.mcp_url = "http://127.0.0.1:8010/mcp"
        handler.control_plane = build_demo_control_plane(
            client=platform.client,
            default_scope=default_scope,
            tenant_id="local-platform",
            agent_id="consumer-app",
            extra_scopes=handler.configured_scopes,
        )
        handler.principal = build_demo_principal(
            principal_id="local-admin",
            tenant_id="local-platform",
            agent_id="consumer-app",
            default_scope=default_scope,
            role=PrincipalRole.ADMIN,
            allowed_scopes=handler.configured_scopes,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        handler.ui_url = f"http://127.0.0.1:{server.server_port}/"
        server_queue.put(server)
        try:
            server.serve_forever()
        finally:
            platform.client.graph.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = server_queue.get(timeout=5)
    base_url = f"http://127.0.0.1:{server.server_port}/"
    try:
        status = read_json(f"{base_url}api/platform/status")
        assert status["tenant_id"] == "local-platform"
        assert status["agent_ids"] == []
        assert status["registered_agents"] == []
        assert status["project_scope"] == {
            "kind": "tenant",
            "scope_id": "local-platform",
        }
        assert status["project_scope_key"] == "tenant:local-platform"
        assert status["integration_contract_url"] == f"{base_url}api/platform/integration-contract"
        assert status["llm"]["has_api_key"] is True
        assert secret not in json.dumps(status)

        contract = read_json(status["integration_contract_url"])
        assert contract["tenant_id"] == "local-platform"
        assert contract["platform_api_url"] == base_url
        assert contract["ui_url"] == base_url
        assert contract["mcp_url"] == "http://127.0.0.1:8010/mcp"
        assert contract["recommended_mcp_server_name"] == "memotron_agent_memory"
        assert contract["mcp_server"]["recommended_name"] == "memotron_agent_memory"
        assert contract["seeding_required"] is False
        assert contract["mode"] == "simple"
        assert contract["scope_rules"]["personal"]["meaning"].startswith("User-owned")
        assert contract["scope_rules"]["project"]["scope_key_format"] == "tenant:local-platform"
        assert contract["motive"]["selection"] == "automatic_unless_explicitly_overridden"
        assert contract["motive_guidance"] == "automatic unless intentionally overridden"
        assert contract["recommended_lifecycle_sequence"] == [
            "memory_bootstrap",
            "memory_search",
            "memory_remember",
            "memory_publish",
            "memory_outcome",
            "memory_log",
            "memory_refresh",
        ]
        assert [item["tool"] for item in contract["recommended_call_sequence"]] == [
            "memory_bootstrap",
            "memory_search",
            "memory_remember",
            "memory_publish",
            "memory_outcome",
            "memory_log",
            "memory_refresh",
        ]
        assert "memory_contract" in contract["required_tools"]
        assert "memory_bootstrap" in contract["required_tools"]
        assert "agent_register" in contract["required_tools"]
        assert "memory_publish" in contract["required_tools"]
        assert "project_memory_configure" in contract["required_tools"]
        assert "memory_evolution" in contract["required_tools"]
        assert contract["scope_rules"]["project"]["write_tool"] == "memory_publish"
        assert contract["project_memory"]["configuration_required_before_publish"] is True
        assert contract["agent_registration"]["model"] == "explicit_idempotent"
        assert contract["outcome_reporting"]["model"] == "explicit_observed_only"
        assert contract["context_compaction"]["checkpoint_reasons"][0] == ("context_compaction")
        assert secret not in json.dumps(contract)

        with pytest.raises(HTTPError) as unregistered:
            post_json(f"{base_url}api/platform/memory/start", {"agent_id": "runtime-api"})
        assert unregistered.value.code == 400
        post_json(
            f"{base_url}api/platform/agents/register",
            {"agent_id": "runtime-api", "agent_name": "Runtime API Agent"},
        )
        project_memory = read_json(f"{base_url}api/platform/project-memory/config")
        assert project_memory["configured"] is False
        configured_project_memory = post_json(
            f"{base_url}api/platform/project-memory/config",
            {
                "project_goal": "Ship the local platform safely.",
                "memory_goal": "Keep durable cross-agent decisions.",
                "keep": ["Decisions that affect multiple agents."],
                "exclude": ["Personal scratch work."],
                "configured_by": "http-test",
            },
        )
        assert configured_project_memory["version"] == "project-v1"
        published = post_json(
            f"{base_url}api/platform/memory/publish",
            {
                "agent_id": "runtime-api",
                "content": (
                    "Memory: subject=local platform; predicate=decided; "
                    "object=use governed project promotion; "
                    "relationship_type=DECIDES; confidence=0.9"
                ),
                "task_run_id": "runtime-api-project",
                "source_reference": "http-test",
            },
        )
        assert published["policy_version"] == "project-v1"
        assert published["project_scope"]["scope_id"] == "local-platform"
        post_json(f"{base_url}api/platform/memory/start", {"agent_id": "runtime-api"})
        registered_status = read_json(f"{base_url}api/platform/status")
        assert registered_status["agent_ids"] == ["runtime-api"]
        assert registered_status["registered_agents"][0]["agent_id"] == "runtime-api"
        assert registered_status["registered_agents"][0]["agent_name"] == "Runtime API Agent"
        registered_contract = read_json(status["integration_contract_url"])
        assert registered_contract["agent_registration"]["registered_agents"] == [
            {"agent_id": "runtime-api", "agent_name": "Runtime API Agent"}
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_sdk_consumer_example_uses_hosted_platform_api(tmp_path: Path) -> None:
    sdk_consumer = load_example_module("sdk_consumer")
    graph_path = tmp_path / "sdk-hosted-consumer.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="local-platform")
    server_queue = Queue()

    def serve() -> None:
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id="local-platform",
        )
        handler = type("HostedPlatformHandler", (MemoryGraphHandler,), {})
        handler.client = platform.client
        handler.runtime_client = platform.client
        handler.platform = platform
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        handler.static_dir = tmp_path / "missing-dist"
        handler.tenant_id = "local-platform"
        handler.agent_ids = ()
        handler.configured_scopes = (default_scope,)
        handler.ui_url = "http://127.0.0.1:0/"
        handler.mcp_url = "http://127.0.0.1:8010/mcp"
        handler.control_plane = build_demo_control_plane(
            client=platform.client,
            default_scope=default_scope,
            tenant_id="local-platform",
            agent_id="consumer-app",
            extra_scopes=handler.configured_scopes,
        )
        handler.principal = build_demo_principal(
            principal_id="local-admin",
            tenant_id="local-platform",
            agent_id="consumer-app",
            default_scope=default_scope,
            role=PrincipalRole.ADMIN,
            allowed_scopes=handler.configured_scopes,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        handler.ui_url = f"http://127.0.0.1:{server.server_port}/"
        server_queue.put(server)
        try:
            server.serve_forever()
        finally:
            platform.client.graph.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = server_queue.get(timeout=5)
    base_url = f"http://127.0.0.1:{server.server_port}/"
    try:
        status = read_json(f"{base_url}api/platform/status")
        assert status["tenant_id"] == "local-platform"
        assert status["agent_ids"] == []
        assert status["registered_agents"] == []
        assert status["project_scope_key"] == "tenant:local-platform"
        assert status["integration_contract_url"] == f"{base_url}api/platform/integration-contract"

        dynamic_bootstrap = post_json(
            f"{base_url}api/platform/memory/bootstrap",
            {
                "agent_id": "runtime-api",
                "agent_name": "Runtime API Agent",
                "task_run_id": "runtime-api-task",
            },
        )
        dynamic_start = dynamic_bootstrap["start"]
        assert dynamic_bootstrap["registration"]["created"] is True
        assert dynamic_start["agent_scope"]["scope_id"] == "runtime-api"
        assert dynamic_start["task_run_id"] == "runtime-api-task"

        dynamic_status = read_json(f"{base_url}api/platform/status")
        assert dynamic_status["agent_ids"] == ["runtime-api"]
        assert dynamic_status["registered_agents"][0]["agent_id"] == "runtime-api"
        assert dynamic_status["registered_agents"][0]["agent_name"] == "Runtime API Agent"
        scopes = read_json(f"{base_url}api/scopes")
        assert any(scope["key"] == "agent:runtime-api" for scope in scopes["scopes"])

        prompts = read_json(f"{base_url}api/tenant-prompts")
        assert prompts["tenant_id"] == "local-platform"
        assert prompts["active_pack"] == "tenant-active"
        assert prompts["current"]["prompt_text"].startswith("Dream prompt profile:")
        assert prompts["current"]["motive_name"] == ""
        assert prompts["resolved_motive"]["name"] == "agent-memory"
        assert any(motive["name"] == "agent-memory" for motive in prompts["motives"])

        saved_prompts = post_json(
            f"{base_url}api/tenant-prompts",
            {
                "prompt_text": "Dream prompt profile: support-memory@v1\nRemember JedAI Platform operating decisions.",
                "motive_name": "agent-memory",
                "source_profile": "support-memory",
                "source_profile_version": "v1",
            },
        )
        assert saved_prompts["current"]["version"] == "tenant-v1"
        assert saved_prompts["current"]["motive_name"] == "agent-memory"
        assert saved_prompts["resolved_motive"]["source"] == "active_prompt"
        assert "Remember JedAI Platform" in saved_prompts["current"]["prompt_text"]

        sequence = post_json(
            f"{base_url}api/dream-sequence/run",
            {"scope": "agent:runtime-api"},
        )
        assert sequence["scope"]["scope_id"] == "runtime-api"
        assert sequence["agent_id"] == "runtime-api"
        assert sequence["running"] is True
        assert sequence["motive_name"] == "agent-memory"
        assert sequence["motive_source"] == "active_prompt"
        run_id = sequence["run_id"]
        for _ in range(40):
            sequence = read_json(f"{base_url}api/dream-sequence/status?run_id={run_id}")
            if not sequence["running"]:
                break
            time.sleep(0.05)
        assert sequence["status"] == "completed"
        assert sequence["motive_source_label"] == "active prompt override"
        assert any(event["phase"] == "policy" for event in sequence["events"])
        assert any(signal["name"] == "Motive policy" for signal in sequence["capability_signals"])
        logged = post_json(
            f"{base_url}api/platform/memory/log",
            {"agent_id": "runtime-api", "summary": "Optional log arrays can be omitted."},
        )
        assert logged["episode_uuid"]

        result = await sdk_consumer.run_consumer(
            base_url=base_url,
            agent_id="consumer-app",
            agent_name="SDK Consumer Agent",
        )

        personal_scope_id = result["remembered"]["scope"]["scope_id"]
        options = read_json(f"{base_url}api/filter-options?scope=user%3A{personal_scope_id}")
        assert "SHOULD" in options["options"]["types"]
        assert "active" in options["options"]["statuses"]
        assert "consumer-app consumer" in options["options"]["subjects"]
        aggregate_overview = read_json(f"{base_url}api/overview?scope=tenant%3Alocal-platform")
        assert aggregate_overview["scope"] == {"kind": "tenant", "scope_id": "local-platform"}
        assert aggregate_overview["memory"]["visible_facts"] >= 1
        assert aggregate_overview["memory"]["active_facts"] >= 1
        assert aggregate_overview["memory"]["episodes"] >= 1
        dated_graph = read_json(f"{base_url}api/graph?scope=agent%3Aconsumer-app&as_of=2999-01-01")
        assert dated_graph["relationship_count"] >= 1

        purge = post_json(f"{base_url}api/tenant-config/purge", {})
        assert purge["tenant_id"] == "local-platform"
        assert purge["purged"]["tenant_id"] == "local-platform"
        assert purge["purged"]["scope_keys"] == [
            "tenant:local-platform",
            "agent:consumer-app",
            "agent:runtime-api",
        ]
        assert purge["purged"]["raw_episodes_preserved"] >= 1
        assert (purge["purged"]["processed_episodes_reset"] + purge["purged"]["episode_processing_reset"]) >= 1
        assert purge["purged"]["relationships_deleted"] >= 1
        assert purge["purged"]["tenant_agents_deleted"] >= 2
        assert purge["purged"]["tenant_prompt_versions_deleted"] == 1
        assert purge["purged"]["project_memory_config_versions_deleted"] == 1
        assert purge["purged"]["credentials_preserved"] is False
        assert purge["config"]["tenant"]["agent_ids"] == []
        assert purge["prompts"]["current"]["kind"] == "library"
        assert purge["prompts"]["resolved_motive"]["source"] == "tenant_default"

        purged_status = read_json(f"{base_url}api/platform/status")
        assert purged_status["agent_ids"] == []
        assert purged_status["registered_agents"] == []
        purged_contract = read_json(f"{base_url}api/platform/integration-contract")
        assert purged_contract["agent_registration"]["registered_agents"] == []
        post_json(
            f"{base_url}api/platform/agents/register",
            {"agent_id": "consumer-app", "agent_name": "SDK Consumer Agent"},
        )
        purged_graph = read_json(f"{base_url}api/graph?scope=agent%3Aconsumer-app")
        assert purged_graph["relationship_count"] == 0
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result["status"]["tenant_id"] == "local-platform"
    assert result["remembered"]["scope"]["kind"] == "user"
    assert result["remembered"]["scope"]["scope_id"].startswith("local-")
    assert result["published"]["project_scope"]["scope_id"] == "local-platform"
    assert result["published"]["policy_version"] == "project-v1"
    assert any(
        item["object"] == "call memory_start before relying on prior context" for item in result["search_results"]
    )
    assert result["outcome"]["verdict"] == "positive"
    assert result["utility"][0]["positive_outcome_count"] == 1
    assert result["checkpoint"]["queued_for_dreaming"] is True
    assert result["rehydrated_task_run_id"].endswith("-post-compaction")
    assert sum(run["processed_episodes"] for run in result["refreshed"]["agent_run"]["job_runs"]) >= 1
    assert sum(run["processed_episodes"] for run in result["refreshed"]["project_run"]["job_runs"]) >= 1
    inspector = Memotron(graph_path=graph_path)
    try:
        agent_episodes = inspector.graph.episodes_for_scope("agent:consumer-app")
        assert agent_episodes
        assert any(episode.metadata.get("checkpoint_reason") == "context_compaction" for episode in agent_episodes)
        assert all(not inspector.graph.is_episode_processed(episode.uuid) for episode in agent_episodes)
        assert (
            inspector.graph.active_relationships(scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="consumer-app")) == []
        )
    finally:
        inspector.graph.close()


@pytest.mark.asyncio
async def test_mcp_consumer_example_uses_agent_memory_tools(tmp_path: Path) -> None:
    mcp_consumer = load_example_module("mcp_consumer")
    agent_memory_mcp.build_platform(
        graph_path=tmp_path / "mcp-consumer.sqlite",
        project_id="local-platform",
    )

    class FakeSession:
        async def call_tool(self, name: str, arguments: dict) -> SimpleNamespace:
            text = await getattr(agent_memory_mcp, name)(**arguments)
            return SimpleNamespace(content=[SimpleNamespace(text=text)])

    result = await mcp_consumer.run_consumer_with_session(
        FakeSession(),
        agent_id="consumer-app",
        agent_name="MCP Consumer Agent",
    )

    assert result["contract"]["agent_registration"]["model"] == "explicit_idempotent"
    assert result["registration"]["agent_name"] == "MCP Consumer Agent"
    assert result["start"]["tenant_id"] == "local-platform"
    assert result["remembered"]["scope"]["kind"] == "user"
    assert result["remembered"]["scope"]["scope_id"].startswith("local-")
    assert result["published"]["project_scope"]["scope_id"] == "local-platform"
    assert result["published"]["policy_version"] == "project-v1"
    assert result["searched"]["results"][0]["object"] == "call memory_search before repeating prior work"
    assert result["outcome"]["verdict"] == "positive"
    assert result["checkpoint"]["queued_for_dreaming"] is True
    assert result["rehydrated"]["task_run_id"].endswith("-post-compaction")
    assert sum(run["processed_episodes"] for run in result["refreshed"]["agent_run"]["job_runs"]) >= 1
    assert sum(run["processed_episodes"] for run in result["refreshed"]["project_run"]["job_runs"]) >= 1


async def _seed_project_memory_graph(graph_path: Path) -> None:
    """Seed a graph whose project scope is governed by a project-memory policy."""
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="memotron-demo",
        agent_ids=("claude-code",),
    )
    try:
        platform.configure_project_memory(
            project_goal="Keep the checkout-api team's durable engineering decisions correct.",
            memory_goal="Preserve only confirmed facts a future session must know.",
            keep=("Confirmed architectural decisions, with their rationale.",),
            exclude=("Personal preferences and scratch work.",),
            allowed_memory_types=("decision", "requirement", "directive"),
            protected_memory_types=("decision", "requirement"),
            min_salience=0.0,
            configured_by="project-owner",
        )
        await platform.memory_publish(
            agent_id="claude-code",
            content=(
                "Memory: subject=checkout-api; predicate=decided; "
                "object=use PostgreSQL for the reservation ledger; "
                "relationship_type=DECIDES; confidence=0.9"
            ),
            task_run_id="seed-task-1",
            source_reference="architecture-review-1",
        )
        await platform.memory_refresh(agent_id="claude-code", include_project=True)
    finally:
        platform.client.graph.close()


def _admin_handler_like_main(
    *,
    graph_path: Path,
    default_scope: MemoryScope,
    tenant_id: str,
    agent_id: str,
    extra_scopes: tuple[MemoryScope, ...] = (),
) -> tuple[type[MemoryGraphHandler], Memotron]:
    """Wire a handler exactly the way ``admin_server.main`` does."""
    handler = type("MotiveHandler", (MemoryGraphHandler,), {})
    client = Memotron(graph_path=graph_path)
    handler.client = client
    handler.runtime_client = client
    handler.default_scope = default_scope
    handler.graph_path = graph_path
    handler.tenant_id = tenant_id
    handler.agent_ids = (agent_id,)
    handler.configured_scopes = (default_scope,)
    handler.ui_url = "http://127.0.0.1:8765/"
    handler.mcp_url = ""
    handler.control_plane = build_demo_control_plane(
        client=client,
        default_scope=default_scope,
        tenant_id=tenant_id,
        agent_id=agent_id,
        extra_scopes=extra_scopes,
    )
    client.control_plane = handler.control_plane
    apply_persisted_tenant_policy(client=client, tenant_id=tenant_id)
    handler.control_plane = client.control_plane
    handler.principal = build_demo_principal(
        principal_id="demo-user",
        tenant_id=tenant_id,
        agent_id=agent_id,
        default_scope=default_scope,
        role=PrincipalRole.ADMIN,
        allowed_scopes=(default_scope, *extra_scopes),
    )
    return handler, client


@pytest.mark.asyncio
async def test_admin_resolved_motive_is_the_project_memory_motive_not_a_bank_default(
    tmp_path: Path,
) -> None:
    """The Dreaming screen must name the Motive that actually governs the scope.

    Regression: the admin server built a control plane whose tenant/scope default
    was the hardcoded ``learn-compliance-requirements`` persona, and resolved the
    displayed Motive from ``tenant.default_motive`` without ever looking at the
    scope. A tenant scope with an active project-memory policy was therefore
    reported as governed by a Motive whose ``allowed_memory_types`` could not have
    produced the facts on screen.
    """
    graph_path = tmp_path / "project-motive.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo")
    await _seed_project_memory_graph(graph_path)
    handler, client = _admin_handler_like_main(
        graph_path=graph_path,
        default_scope=default_scope,
        tenant_id="memotron-demo",
        agent_id="claude-code",
    )
    try:
        payload = tenant_prompts_payload(client, "memotron-demo", scope=default_scope)

        assert payload["resolved_motive"]["name"] == PROJECT_MEMORY_POLICY_MOTIVE
        assert payload["resolved_motive"]["source"] == "scope"
        assert payload["resolved_motive"]["scope"] == default_scope.key

        catalog = {motive["name"]: motive for motive in payload["motives"]}
        governing = catalog[PROJECT_MEMORY_POLICY_MOTIVE]
        assert governing["allowed_memory_types"] == ["decision", "requirement", "directive"]
        assert "checkout-api" in governing["goal"]

        # The bank default that used to be displayed admits only `requirement`,
        # so it provably could not have formed the decision rows in this scope.
        compliance = catalog["learn-compliance-requirements"]
        assert compliance["allowed_memory_types"] == ["requirement"]
        assert payload["resolved_motive"]["name"] != "learn-compliance-requirements"

        # A dream run started now resolves the very same Motive: one resolver
        # backs the read surface and the run surface, so they cannot drift.
        instance = handler.__new__(handler)
        run_motive = instance._resolve_dream_motive(
            scope=default_scope,
            agent_id="claude-code",
            requested_motive_name="",
            active_prompt_motive_name="",
            prompt_pack=None,
        )
        assert run_motive["name"] == payload["resolved_motive"]["name"]
        assert run_motive["source"] == payload["resolved_motive"]["source"]
    finally:
        client.graph.close()


@pytest.mark.asyncio
async def test_admin_motive_evidence_reports_what_actually_formed_the_facts(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "motive-evidence.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo")
    await _seed_project_memory_graph(graph_path)
    _, client = _admin_handler_like_main(
        graph_path=graph_path,
        default_scope=default_scope,
        tenant_id="memotron-demo",
        agent_id="claude-code",
    )
    try:
        evidence = tenant_prompts_payload(client, "memotron-demo", scope=default_scope)["motive_evidence"]
        assert evidence["scope"] == default_scope.key
        assert evidence["fact_count"] >= 1
        stamped = {item["motive_name"]: item for item in evidence["observed"] if item["motive_name"]}
        assert PROJECT_MEMORY_POLICY_MOTIVE in stamped
        assert len(stamped[PROJECT_MEMORY_POLICY_MOTIVE]["motive_version_digest"]) == 64
        # Policy and evidence agree here, so nothing is flagged as divergent.
        assert evidence["divergent"] is False
    finally:
        client.graph.close()


@pytest.mark.asyncio
async def test_admin_motive_evidence_flags_policy_evidence_divergence(
    tmp_path: Path,
) -> None:
    graph_path = tmp_path / "motive-divergence.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo")
    await _seed_project_memory_graph(graph_path)
    _, client = _admin_handler_like_main(
        graph_path=graph_path,
        default_scope=default_scope,
        tenant_id="memotron-demo",
        agent_id="claude-code",
    )
    try:
        payload = tenant_prompts_payload(
            client,
            "memotron-demo",
            scope=default_scope,
        )
        assert payload["motive_evidence"]["divergent"] is False

        # An admin override selects a different Motive for the NEXT run; the rows
        # on screen were still formed under the project-memory Motive, and the
        # console must be able to say so rather than silently restating policy.
        overridden = resolve_scope_motive(
            client=client,
            tenant_id="memotron-demo",
            scope=default_scope,
            requested_motive_name="learn-compliance-requirements",
        )
        assert overridden["name"] == "learn-compliance-requirements"
        assert overridden["source"] == "admin_override"
        formed = {item["motive_name"] for item in payload["motive_evidence"]["observed"] if item["motive_name"]}
        assert formed == {PROJECT_MEMORY_POLICY_MOTIVE}
    finally:
        client.graph.close()


@pytest.mark.asyncio
async def test_admin_agent_scope_motive_follows_persisted_assignment(tmp_path: Path) -> None:
    graph_path = tmp_path / "agent-motive.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo")
    agent_only_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="claude-code")
    await _seed_project_memory_graph(graph_path)
    inspector = Memotron(graph_path=graph_path)
    try:
        assignment = inspector.graph.agent_motive_assignment(
            tenant_id="memotron-demo",
            agent_id="claude-code",
        )
        assert assignment is not None
        expected_motive = assignment["motive_name"]
    finally:
        inspector.graph.close()

    _, client = _admin_handler_like_main(
        graph_path=graph_path,
        default_scope=default_scope,
        tenant_id="memotron-demo",
        agent_id="claude-code",
        extra_scopes=(agent_only_scope,),
    )
    try:
        resolved = resolve_scope_motive(
            client=client,
            tenant_id="memotron-demo",
            scope=agent_only_scope,
        )
        assert resolved["name"] == expected_motive == "engineering-agent-memory"
        assert resolved["name"] != "learn-compliance-requirements"
    finally:
        client.graph.close()


def test_resolve_launch_tenant_id_derives_and_warns(tmp_path: Path) -> None:
    graph_path = tmp_path / "tenant-derivation.sqlite"
    client = Memotron(graph_path=graph_path)
    try:
        assert client.graph.known_tenant_ids() == ()
        empty_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="nothing-here")
        # Empty graph: nothing to derive, nothing to warn about.
        assert resolve_launch_tenant_id(
            client=client,
            requested_tenant_id=None,
            default_scope=empty_scope,
        ) == (DEMO_TENANT_ID, ())

        client.graph.register_tenant_agent(
            tenant_id="memotron-demo",
            agent_id="claude-code",
            name="Claude Code",
            source="test",
        )
        assert client.graph.known_tenant_ids() == ("memotron-demo",)

        # Sole tenant in the graph is derived even when the scope does not name it.
        derived, warnings = resolve_launch_tenant_id(
            client=client,
            requested_tenant_id=None,
            default_scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="claude-code"),
        )
        assert (derived, warnings) == ("memotron-demo", ())

        # A tenant-kind scope names its tenant directly.
        derived, warnings = resolve_launch_tenant_id(
            client=client,
            requested_tenant_id=None,
            default_scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo"),
        )
        assert (derived, warnings) == ("memotron-demo", ())

        # An explicit id that matches nothing in the graph is honoured but loud:
        # every tenant-keyed read (and purge) would target the wrong tenant.
        used, warnings = resolve_launch_tenant_id(
            client=client,
            requested_tenant_id=DEMO_TENANT_ID,
            default_scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="memotron-demo"),
        )
        assert used == DEMO_TENANT_ID
        assert len(warnings) == 1
        assert "matches no tenant in this graph" in warnings[0]
        # This assertion used to require the OTHER tenant's name in the message. It was
        # inverted deliberately: these warnings are served on /api/tenant-config and
        # /api/overview, which authenticate nobody, so naming the roster there is the
        # same cross-tenant enumeration that removing `graph_tenant_ids` closed. The
        # count carries the operator's remedy ("pass --tenant-id") without the names;
        # the names go to the process log instead. See
        # test_the_launch_WARNING_does_not_name_the_other_tenants.
        assert "memotron-demo" not in warnings[0], (
            "the warning names another tenant again, re-opening the enumeration leak"
        )
        assert "1 other tenant(s) present" in warnings[0], (
            "the count must survive the redaction, or the warning no longer tells the "
            "operator that a real tenant exists and this one is not it"
        )

        # Two tenants and no --tenant-id: never guess, always warn.
        client.graph.register_tenant_agent(
            tenant_id="other-tenant",
            agent_id="other-agent",
            name="Other Agent",
            source="test",
        )
        used, warnings = resolve_launch_tenant_id(
            client=client,
            requested_tenant_id=None,
            default_scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="claude-code"),
        )
        assert used == DEMO_TENANT_ID
        assert len(warnings) == 1
        assert "more than one tenant" in warnings[0]
    finally:
        client.graph.close()


def test_admin_tenant_config_reports_unconfigured_mcp_url(tmp_path: Path) -> None:
    graph_path = tmp_path / "mcp-unset.sqlite"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="local-platform")
    client = Memotron(graph_path=graph_path)
    try:
        payload = tenant_config_payload(
            client=client,
            tenant_id="local-platform",
            agent_ids=("consumer-app",),
            graph_path=graph_path,
            default_scope=default_scope,
            ui_url="http://127.0.0.1:8765/",
            mcp_url="",
            warnings=("tenant id 'local-platform' matches no tenant in this graph",),
        )
        assert payload["operator"]["mcp_url"] == ""
        assert payload["operator"]["mcp_configured"] is False
        assert "127.0.0.1:8010" not in json.dumps(payload)
        assert "No MCP endpoint is configured" in payload["snippets"]["mcp"]
        assert "No MCP endpoint is configured" in payload["snippets"]["codex_mcp_config"]
        assert payload["tenant"]["warnings"] == ["tenant id 'local-platform' matches no tenant in this graph"]
    finally:
        client.graph.close()


def test_tenant_config_does_not_enumerate_OTHER_tenants(tmp_path) -> None:
    """B2: one tenant's config must not name another tenant in the same store.

    Asserted against the neighbour's ID appearing ANYWHERE in the serialized body,
    deliberately, rather than against the absence of the `graph_tenant_ids` key that
    used to carry it. A key-absence assertion passes the moment the field is renamed
    or the same list is nested under `operator`; this one does not care how the leak
    is spelled. `/api/tenant-config` and `/api/overview` authenticate nobody --
    `principal` is a class attribute -- so any VPC caller reaching the port got the
    full tenant roster, which is why the property and not the field is what is pinned.
    """
    graph_path = tmp_path / "two-tenants.sqlite"
    client = Memotron(graph_path=graph_path)
    try:
        client.graph.register_tenant_agent(tenant_id="tenant-under-test", agent_id="agent-a", name="Agent A")
        # The neighbour. Governance state alone is enough to appear in
        # `known_tenant_ids()` -- it is a UNION over six tenant-keyed tables and
        # touches no memory scope, so a tenant that merely registered an agent is
        # just as enumerable as one holding facts.
        client.graph.register_tenant_agent(tenant_id="secret-neighbour-tenant", agent_id="agent-b", name="Agent B")
        assert "secret-neighbour-tenant" in client.graph.known_tenant_ids(), (
            "positive control: the neighbour must really be in the store, or this "
            "test would pass against an empty graph and prove nothing"
        )

        payload = tenant_config_payload(
            client=client,
            tenant_id="tenant-under-test",
            agent_ids=("agent-a",),
            graph_path=graph_path,
            default_scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="tenant-under-test"),
            ui_url="http://127.0.0.1:8765/",
            mcp_url="http://127.0.0.1:8010/mcp",
        )

        assert "secret-neighbour-tenant" not in json.dumps(payload), (
            "cross-tenant enumeration: this tenant's config names another tenant in the same store"
        )
        assert payload["tenant"]["tenant_id"] == "tenant-under-test", (
            "the caller's OWN tenant must still be reported -- the fix is to stop "
            "enumerating neighbours, not to blank the identity the console renders"
        )
    finally:
        client.graph.close()


def test_the_launch_WARNING_does_not_name_the_other_tenants(tmp_path) -> None:
    """B2, second half: the warning text must not re-leak what the field removal closed.

    `resolve_launch_tenant_id`'s warnings are served on `/api/tenant-config` and
    `/api/overview` — surfaces that authenticate nobody — and they used to interpolate
    `', '.join(graph_tenants)`, the full roster. Deleting the `graph_tenant_ids` field
    while leaving that in place would have looked like a complete fix and leaked the
    same list through the message.

    Both warning branches are exercised, because they were written separately and only
    one of them is reachable at a time.
    """
    client = Memotron(graph_path=tmp_path / "warn.sqlite")
    try:
        for name in ("tenant-under-test", "secret-neighbour-one", "secret-neighbour-two"):
            client.graph.register_tenant_agent(tenant_id=name, agent_id=f"a-{name}", name=f"A {name}")
        customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:x")

        # Branch 1: no --tenant-id, several tenants -> "more than one tenant" warning.
        derived, derived_warnings = resolve_launch_tenant_id(
            client=client, requested_tenant_id=None, default_scope=customer_scope
        )
        assert derived_warnings, "positive control: this case must actually warn"

        # Branch 2: an explicit id that matches nothing -> "matches no tenant" warning.
        _, mismatch_warnings = resolve_launch_tenant_id(
            client=client, requested_tenant_id="no-such-tenant", default_scope=customer_scope
        )
        assert mismatch_warnings, "positive control: this case must actually warn"

        for warnings in (derived_warnings, mismatch_warnings):
            payload = tenant_config_payload(
                client=client,
                tenant_id=derived,
                agent_ids=("a-tenant-under-test",),
                graph_path=tmp_path / "warn.sqlite",
                default_scope=customer_scope,
                ui_url="http://127.0.0.1:8765/",
                mcp_url="http://127.0.0.1:8010/mcp",
                warnings=warnings,
            )
            body = json.dumps(payload)
            for neighbour in ("secret-neighbour-one", "secret-neighbour-two"):
                assert neighbour not in body, f"the served warning names {neighbour}: {warnings}"
            # The warning must still be USEFUL, or the redaction has just deleted the
            # signal that diagnosed this defect in the first place.
            assert "tenant" in " ".join(warnings).lower()
    finally:
        client.graph.close()


def test_operator_tenant_detail_names_what_the_response_redacts(tmp_path) -> None:
    """The other half of the split: names reach the LOG, never the HTTP response.

    Without this, redacting the warning would be indistinguishable from destroying the
    signal — the operator would lose the only thing that diagnosed the wrong-tenant
    defect in the first place.
    """
    client = Memotron(graph_path=tmp_path / "detail.sqlite")
    try:
        for name in ("alpha-tenant", "beta-tenant"):
            client.graph.register_tenant_agent(tenant_id=name, agent_id=f"a-{name}", name=f"A {name}")
        lines = operator_tenant_detail(client, ("some warning",))
        assert lines, "a warning was present, so the operator detail must be emitted"
        assert "alpha-tenant" in lines[0] and "beta-tenant" in lines[0], (
            "the operator detail must NAME the tenants; it is the log-side counterpart to "
            f"the redacted warning. Got {lines!r}"
        )
        assert operator_tenant_detail(client, ()) == (), (
            "with no warning there is nothing to explain, so nothing extra is logged — "
            "this branch lives here rather than in main(), which no test drives"
        )
    finally:
        client.graph.close()
