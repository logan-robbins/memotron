from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

import memotron.agent_memory_mcp as agent_memory_mcp
import memotron.mcp_server as generic_mcp
from memotron import (
    DEFAULT_AGENT_MEMORY_MOTIVE,
    ENGINEERING_AGENT_MEMORY_MOTIVE,
    GENERAL_AGENT_MEMORY_MOTIVE,
    PROJECT_MEMORY_POLICY_MOTIVE,
    AgentMemoryMode,
    AgentMemoryPlatform,
    MemoryType,
    OpenAICompatibleDreamAgentTransport,
    OpenAICompatibleExtractionTransport,
    RuleBasedExtractionTransport,
    ScopeKind,
    agent_memory_bank,
    build_agent_memory_control_plane,
    project_scope,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
)
from memotron.runtime import (
    build_transports_from_env,
    load_env_file,
    seed_tenant_llm_credentials_from_env,
)


def test_agent_memory_control_plane_registers_project_and_agent_scopes() -> None:
    control_plane = build_agent_memory_control_plane(
        project_id="jedai-platform",
        agent_ids=("platform", "sdk"),
    )

    policy = control_plane.resolve(tenant_id="jedai-platform", agent_id="platform")
    project_policy = control_plane.resolve(
        tenant_id="jedai-platform",
        agent_id="platform",
        scope=project_scope("jedai-platform"),
    )

    tenant = control_plane.tenant("jedai-platform")
    assert tenant.default_agent_id == "platform"
    assert tenant.default_motive == DEFAULT_AGENT_MEMORY_MOTIVE
    assert policy.scope.kind == ScopeKind.AGENT
    assert policy.scope.scope_id == "platform"
    assert project_policy.tenant_id == "jedai-platform"
    assert project_policy.scope.kind == ScopeKind.TENANT
    assert "tenant:jedai-platform" in tenant.registered_scope_keys()
    assert "agent:platform" in tenant.registered_scope_keys()
    assert "agent:sdk" in tenant.registered_scope_keys()
    assert any(key.startswith("user:") for key in tenant.registered_scope_keys())
    assert agent_memory_bank().motive(DEFAULT_AGENT_MEMORY_MOTIVE).name == DEFAULT_AGENT_MEMORY_MOTIVE


@pytest.mark.asyncio
async def test_agent_memory_start_search_remember_and_authorization(tmp_path) -> None:
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "agent-memory.sqlite",
        project_id="jedai-platform",
        agent_ids=("platform", "sdk"),
    )

    await platform.memory_remember(
        agent_id="platform",
        subject="platform agent",
        predicate="should",
        object="check worktree status before editing",
        relationship_type="SHOULD",
        source_text="operator preference",
    )
    config = platform.configure_project_memory(
        project_goal="Deliver the JedAI Platform project safely.",
        memory_goal="Keep durable cross-agent project decisions.",
        keep=("Decisions that affect multiple project agents.",),
        exclude=("Private scratch work.",),
        configured_by="test-operator",
    )
    assert config.version == "project-v1"
    published = await platform.memory_publish(
        agent_id="platform",
        content=(
            "Memory: subject=jedai-platform project; predicate=decided; "
            "object=use Memotron as memory of record; "
            "relationship_type=DECIDES; confidence=0.9"
        ),
        task_run_id="platform-task-1",
        source_reference="engineering-chat-42",
    )
    assert published.project_scope.key == "tenant:jedai-platform"
    refreshed = await platform.memory_refresh(
        agent_id="platform",
        include_project=True,
    )
    assert refreshed.project_run is not None
    assert refreshed.project_run.processed_episodes == 1

    start = await platform.memory_start(agent_id="platform")
    assert start.mode == AgentMemoryMode.SIMPLE
    assert start.project_scope.kind == ScopeKind.TENANT
    assert start.user_scope is not None
    assert start.user_scope.kind == ScopeKind.USER
    assert start.agent_scope.scope_id == "platform"
    assert "Memotron project memory (tenant:jedai-platform)" in start.rendered_context
    assert "Memotron personal memory" in start.rendered_context
    assert "Memotron session continuity" in start.rendered_context
    assert "check worktree status" in start.rendered_context

    search = await platform.memory_search(
        agent_id="platform",
        query="Memotron memory of record",
    )
    assert search.results[0].scope.key == "tenant:jedai-platform"
    assert "use Memotron as memory of record" in search.results[0].fact

    sdk_search = await platform.memory_search(agent_id="sdk", query="worktree", include_project=False)
    assert sdk_search.results[0].scope.kind == ScopeKind.USER
    assert "check worktree status" in sdk_search.results[0].fact

    assert platform.client.control_plane is not None
    with pytest.raises(ValueError, match="not authorized"):
        platform.client.control_plane.resolve_for_principal(
            principal=platform.principal_for_agent("platform"),
            scope=platform.agent_scope("sdk"),
        )

    registered_dynamic = platform.register_agent(
        agent_id="runtime-agent",
        agent_name="Runtime Agent",
    )
    assert registered_dynamic.created is True
    started_dynamic = await platform.memory_start(agent_id="runtime-agent")
    assert started_dynamic.agent_scope.key == "agent:runtime-agent"
    assert platform.agent_ids == ("platform", "sdk", "runtime-agent")
    assert "agent:runtime-agent" in platform.client.control_plane.tenant("jedai-platform").registered_scope_keys()

    remembered_dynamic = await platform.memory_remember(
        agent_id="runtime-agent",
        subject="runtime agent",
        predicate="should",
        object="register explicitly before Memotron API use",
        relationship_type="SHOULD",
    )
    assert remembered_dynamic.scope.kind == ScopeKind.USER
    assert remembered_dynamic.scope == platform.user_scope


@pytest.mark.asyncio
async def test_project_memory_policy_is_versioned_persistent_and_gates_types(
    tmp_path,
) -> None:
    graph_path = tmp_path / "project-memory-policy.sqlite"
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
        agent_ids=("platform",),
    )

    with pytest.raises(ValueError, match="project memory is not configured"):
        await platform.memory_publish(
            agent_id="platform",
            content="Memory: subject=project; predicate=decided; object=ship",
            task_run_id="task-before-config",
        )

    first = platform.configure_project_memory(
        project_goal="Ship a reliable project memory platform.",
        memory_goal="Retain shared architectural decisions only.",
        keep=("Decisions that change architecture or delivery.",),
        exclude=("Personal preferences and transient scratch state.",),
        rules=("Reject candidate facts that do not affect another agent.",),
        allowed_memory_types=(MemoryType.DECISION,),
        protected_memory_types=(MemoryType.DECISION,),
        min_salience=0.0,
        max_memories_per_candidate=4,
        dedup_threshold=0.91,
        configured_by="project-owner",
    )
    assert first.version == "project-v1"
    policy = platform.client.resolve_policy(
        tenant_id="jedai-platform",
        agent_id="platform",
        scope=project_scope("jedai-platform"),
    )
    assert policy.motive is not None
    assert policy.motive.name == PROJECT_MEMORY_POLICY_MOTIVE
    assert policy.motive.allowed_memory_types == (MemoryType.DECISION,)
    assert (
        "Advance the overall project goal: Ship a reliable project memory platform." in policy.prompt_override.include
    )

    published = await platform.memory_publish(
        agent_id="platform",
        content="Memory: subject=project; predicate=decided; object=use governed promotion; relationship_type=DECIDES; confidence=0.95\nMemory: subject=project owner; predicate=prefers; object=verbose daily status; relationship_type=PREFERS; confidence=0.95",
        task_run_id="task-policy-gate",
        source_reference="architecture-review-7",
    )
    assert published.policy_version == "project-v1"
    refreshed = await platform.memory_refresh(
        agent_id="platform",
        include_project=True,
    )
    assert refreshed.project_run is not None
    assert refreshed.project_run.processed_episodes == 1
    project_relationships = platform.client.graph.active_relationships(scope=project_scope("jedai-platform"))
    relationship_types = [relationship.type for relationship in project_relationships]
    assert "DECIDES" in relationship_types
    assert "PREFERS" not in relationship_types
    status = platform.project_memory_status()
    assert status["candidate_count"] == 1
    assert status["pending_candidate_count"] == 0

    second = platform.configure_project_memory(
        project_goal="Ship a reliable project memory platform.",
        memory_goal="Retain current cross-agent decisions and incidents.",
        keep=("Decisions and incidents that change architecture or delivery.",),
        exclude=("Personal preferences and transient scratch state.",),
        allowed_memory_types=(MemoryType.DECISION, MemoryType.INCIDENT),
        protected_memory_types=(MemoryType.DECISION, MemoryType.INCIDENT),
        configured_by="project-owner",
    )
    assert second.version == "project-v2"
    platform.client.graph.close()

    reloaded = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
    )
    try:
        active = reloaded.project_memory_config()
        assert active is not None
        assert active.version == "project-v2"
        assert [item["version"] for item in reloaded.project_memory_status()["versions"]] == [
            "project-v2",
            "project-v1",
        ]
        reloaded_policy = reloaded.client.resolve_policy(
            tenant_id="jedai-platform",
            agent_id="platform",
            scope=project_scope("jedai-platform"),
        )
        assert reloaded_policy.motive is not None
        assert reloaded_policy.motive.allowed_memory_types == (
            MemoryType.DECISION,
            MemoryType.INCIDENT,
        )
    finally:
        reloaded.client.graph.close()


@pytest.mark.asyncio
async def test_agent_memory_requires_explicit_registration_and_reloads_registry(tmp_path) -> None:
    graph_path = tmp_path / "agent-registry.sqlite"
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
    )
    assert platform.agent_ids == ()

    with pytest.raises(ValueError, match="call agent_register before memory tools"):
        await platform.memory_start(agent_id="jedai-platform-api")
    registration = platform.register_agent(
        agent_id="jedai-platform-api",
        agent_name="JedAI Platform API",
    )
    assert registration.created is True
    reconnect = platform.register_agent(
        agent_id="JEDAI-PLATFORM-API",
        agent_name="jedai platform api",
    )
    assert reconnect.created is False
    assert reconnect.agent_id == "jedai-platform-api"
    started = await platform.memory_start(agent_id="JEDAI-PLATFORM-API")
    assert started.agent_scope.key == "agent:jedai-platform-api"
    assert platform.agent_ids == ("jedai-platform-api",)
    assert platform.client.graph.tenant_agents("jedai-platform")[0]["agent_id"] == "jedai-platform-api"
    platform.client.graph.close()

    reloaded = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
    )
    try:
        assert reloaded.agent_ids == ("jedai-platform-api",)
        reloaded.register_agent(
            agent_id="jedai-platform-worker",
            agent_name="JedAI Platform Worker",
        )
        search = await reloaded.memory_search(agent_id="jedai-platform-worker", query="anything")
        assert search.results == []
        assert reloaded.agent_ids == ("jedai-platform-api", "jedai-platform-worker")
    finally:
        reloaded.client.graph.close()


@pytest.mark.asyncio
async def test_agent_memory_log_refresh_and_evolution(tmp_path) -> None:
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "agent-memory.sqlite",
        project_id="jedai-platform",
        agent_ids=("platform",),
    )

    queued = await platform.memory_log(
        agent_id="platform",
        summary="Completed the agent-memory adapter.",
        decisions=("Use tenant scopes for project memory.",),
        incidents=("Motive filter initially dropped decision memories.",),
        blockers=("none",),
        checkpoint_reason="context_compaction",
        task_run_id="adapter-task-1",
    )
    assert queued.queued_for_dreaming is True
    episode = platform.client.graph.episodes_for_scope("agent:platform")[0]
    assert episode.metadata["agent_memory_event"] == "checkpoint"
    assert episode.metadata["checkpoint_reason"] == "context_compaction"
    assert episode.metadata["task_run_id"] == "adapter-task-1"
    with pytest.raises(ValueError, match="checkpoint_reason must be one of"):
        await platform.memory_log(
            agent_id="platform",
            summary="Invalid checkpoint.",
            checkpoint_reason="automatic",
            task_run_id="adapter-task-1",
        )
    with pytest.raises(ValueError, match="task_run_id is required"):
        await platform.memory_log(
            agent_id="platform",
            summary="Unlinked checkpoint.",
            checkpoint_reason="context_compaction",
        )

    refreshed = await platform.memory_refresh(agent_id="platform", include_project=False)
    assert refreshed.agent_run.processed_episodes == 1
    assert refreshed.agent_run.created_relationships == 4

    search = await platform.memory_search(agent_id="platform", query="tenant scopes")
    assert any("Use tenant scopes for project memory" in result.object for result in search.results)

    evolution = await platform.memory_evolution(agent_id="platform")
    assert evolution.agent_evolution.episode_count >= 1
    assert evolution.agent_evolution.active_relationship_count == 3


@pytest.mark.asyncio
async def test_agent_facade_rejects_raw_credentials_before_episode_storage(
    tmp_path,
) -> None:
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "credential-gate.sqlite",
        project_id="jedai-platform",
        agent_ids=("platform",),
    )
    platform.configure_project_memory(
        project_goal="Ship safely.",
        memory_goal="Keep safe project memory.",
        keep=("Current project decisions.",),
        configured_by="test",
    )

    with pytest.raises(ValueError, match="raw credential"):
        await platform.memory_publish(
            agent_id="platform",
            content="api_key=sk-abcdefghijklmnop",
            task_run_id="secret-publish",
        )
    with pytest.raises(ValueError, match="raw credential"):
        await platform.memory_log(
            agent_id="platform",
            summary="password: dangerous-secret-value",
        )

    assert platform.client.graph.episodes_for_scope(platform.project_scope.key) == []
    assert platform.client.graph.episodes_for_scope("agent:platform") == []


@pytest.mark.asyncio
async def test_agent_memory_mcp_wrappers_use_shared_platform(tmp_path) -> None:
    agent_memory_mcp.build_platform(
        graph_path=tmp_path / "mcp-agent-memory.sqlite",
        project_id="jedai-platform",
        agent_ids=("platform",),
    )

    contract = json.loads(await agent_memory_mcp.memory_contract())
    assert contract["tenant_id"] == "jedai-platform"
    assert contract["recommended_mcp_server_name"] == "memotron_agent_memory"
    assert contract["mcp_server"]["recommended_name"] == "memotron_agent_memory"
    assert "memory_contract" in contract["required_tools"]
    assert "memory_bootstrap" in contract["required_tools"]
    assert "agent_register" in contract["required_tools"]
    assert "memory_start" in contract["required_tools"]
    assert "memory_search" in contract["required_tools"]
    assert "memory_publish" in contract["required_tools"]
    assert "memory_outcome" in contract["required_tools"]
    assert "memory_utility" in contract["required_tools"]
    assert "project_memory_config" in contract["required_tools"]
    assert "project_memory_configure" in contract["required_tools"]
    assert "agent_motive_configure" in contract["required_tools"]
    assert contract["quickstart"]["first_tool"] == "memory_bootstrap"
    assert contract["memory_decision_policy"]["cadence"] == "event_driven_not_periodic"
    assert contract["memory_decision_policy"]["zero_writes_are_valid"] is True
    assert "litellm" in contract["llm_configuration"]["supported_providers"]
    assert contract["llm_configuration"]["litellm"]["recommended_api_key_env"] == ("LITELLM_API_KEY")
    assert contract["recommended_lifecycle_sequence"][:2] == [
        "memory_bootstrap",
        "memory_search",
    ]
    assert contract["agent_registration"]["model"] == "explicit_idempotent"
    assert contract["agent_registration"]["required_before_memory_calls"] is True
    assert contract["mode"] == "simple"
    assert contract["memory_owners"] == ["user", "project"]
    assert contract["scope_rules"]["personal"]["meaning"].startswith("User-owned")
    assert contract["scope_rules"]["project"]["meaning"].startswith("Tenant-wide")
    assert contract["continuity_scope"]["visible_memory_owner"] is False
    assert contract["scope_rules"]["project"]["write_tool"] == "memory_publish"
    assert contract["project_memory"]["publish_tool"] == "memory_publish"
    assert contract["project_memory"]["flow"][1] == ("An agent submits evidence with memory_publish.")
    assert contract["motive"]["selection"] == "automatic_unless_explicitly_overridden"
    assert contract["motive_guidance"] == "automatic unless intentionally overridden"
    assert contract["outcome_reporting"]["model"] == "explicit_observed_only"
    assert contract["context_compaction"]["detection"].startswith("Memotron cannot detect")
    assert contract["context_compaction"]["checkpoint_reasons"] == [
        "context_compaction",
        "handoff",
        "session_end",
    ]

    bootstrapped = json.loads(
        await agent_memory_mcp.memory_bootstrap(
            agent_id="platform",
            agent_name="platform agent",
            task_run_id="mcp-bootstrap-task",
        )
    )
    assert bootstrapped["registration"]["agent_id"] == "platform"
    assert bootstrapped["registration"]["created"] is False
    assert bootstrapped["start"]["task_run_id"] == "mcp-bootstrap-task"
    assert bootstrapped["next_actions"][0].startswith("Inject start.rendered_context")

    remembered = json.loads(
        await agent_memory_mcp.memory_remember(
            agent_id="platform",
            subject="platform agent",
            predicate="should",
            object="write durable facts explicitly",
            relationship_type="SHOULD",
        )
    )
    assert remembered["scope"]["kind"] == "user"
    assert remembered["scope"]["scope_id"].startswith("local-")

    unconfigured = json.loads(await agent_memory_mcp.project_memory_config())
    assert unconfigured["configured"] is False
    configured_project = json.loads(
        await agent_memory_mcp.project_memory_configure(
            project_goal="Ship JedAI Platform safely.",
            memory_goal="Keep durable shared decisions.",
            keep_json='["Cross-agent architectural decisions."]',
            exclude_json='["Personal scratch work."]',
            configured_by="mcp-test",
        )
    )
    assert configured_project["version"] == "project-v1"
    published = json.loads(
        await agent_memory_mcp.memory_publish(
            agent_id="platform",
            content=(
                "Memory: subject=JedAI Platform; predicate=decided; "
                "object=use governed project promotion; "
                "relationship_type=DECIDES; confidence=0.9"
            ),
            task_run_id="mcp-project-task",
            source_reference="mcp-test",
        )
    )
    assert published["project_scope"]["scope_id"] == "jedai-platform"

    started = json.loads(await agent_memory_mcp.memory_start(agent_id="platform"))
    assert started["tenant_id"] == "jedai-platform"
    assert "write durable facts explicitly" in started["rendered_context"]

    searched = json.loads(
        await agent_memory_mcp.memory_search(
            agent_id="platform",
            query="durable facts",
            include_project=False,
            task_run_id="mcp-task",
        )
    )
    assert searched["results"][0]["object"] == "write durable facts explicitly"
    assert searched["task_run_id"] == "mcp-task"
    assert searched["use_events"][0]["kind"] == "retrieved"

    outcome = json.loads(
        await agent_memory_mcp.memory_outcome(
            agent_id="platform",
            use_id=searched["use_events"][0]["use_id"],
            verdict="positive",
            task_run_id="mcp-task",
            idempotency_key="mcp-task:positive",
            judge_identity="mcp-test",
            judge_version="v1",
        )
    )
    utility = json.loads(
        await agent_memory_mcp.memory_utility(
            agent_id="platform",
            relationship_uuid=remembered["relationship_uuid"],
        )
    )
    assert outcome["verdict"] == "positive"
    assert utility[0]["positive_outcome_count"] == 1

    motive = json.loads(await agent_memory_mcp.agent_motive_status(agent_id="platform"))
    assert motive["motive_name"] == ENGINEERING_AGENT_MEMORY_MOTIVE
    configured = json.loads(
        await agent_memory_mcp.agent_motive_configure(
            agent_id="platform",
            motive_name=GENERAL_AGENT_MEMORY_MOTIVE,
        )
    )
    assert configured["motive_name"] == GENERAL_AGENT_MEMORY_MOTIVE

    logged = json.loads(
        await agent_memory_mcp.memory_log(
            agent_id="platform",
            summary="Logged from MCP.",
            decisions_json='["Keep MCP agent-aware."]',
            checkpoint_reason="context_compaction",
            task_run_id="mcp-task",
        )
    )
    assert logged["queued_for_dreaming"] is True

    refreshed = json.loads(
        await agent_memory_mcp.memory_refresh(
            agent_id="platform",
            include_project=True,
        )
    )
    assert refreshed["agent_run"]["job_runs"][0]["processed_episodes"] == 1
    assert sum(run["processed_episodes"] for run in refreshed["project_run"]["job_runs"]) == 1

    dynamic_registration = json.loads(
        await agent_memory_mcp.agent_register(
            agent_id="runtime-agent",
            agent_name="Runtime MCP Agent",
        )
    )
    assert dynamic_registration["created"] is True
    dynamic = json.loads(await agent_memory_mcp.memory_start(agent_id="runtime-agent"))
    assert dynamic["agent_scope"]["scope_id"] == "runtime-agent"
    assert agent_memory_mcp.get_platform().agent_ids == ("platform", "runtime-agent")


def test_agent_registration_rejects_id_and_name_collisions(tmp_path) -> None:
    graph_path = tmp_path / "agent-collisions.sqlite"
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
    )
    observer = None
    try:
        platform.register_agent(agent_id="planner", agent_name="Planning Agent")
        observer = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id="jedai-platform",
        )

        with pytest.raises(ValueError, match="agent_id 'PLANNER' is already registered"):
            observer.register_agent(
                agent_id="PLANNER",
                agent_name="Different Agent",
            )
        with pytest.raises(ValueError, match="agent_name 'planning agent' is already registered"):
            observer.register_agent(
                agent_id="other-agent",
                agent_name="planning agent",
            )
        with pytest.raises(ValueError, match="agent_id must be 1-64"):
            platform.register_agent(
                agent_id="not a valid id",
                agent_name="Invalid Identifier",
            )
    finally:
        if observer is not None:
            observer.client.graph.close()
        platform.client.graph.close()


async def _archive_agent_memory(platform: AgentMemoryPlatform, *, object_value: str) -> str:
    """Remember one fact, retire it, and give it a restorable prune ghost."""
    remembered = await platform.memory_remember(
        agent_id="platform",
        subject="platform agent",
        predicate="prefers",
        object=object_value,
        relationship_type="PREFERS",
    )
    forgotten = await platform.memory_forget(
        agent_id="platform",
        relationship_uuid=remembered.relationship_uuid,
        reason="archived for the curation test",
    )
    assert forgotten.status.value == "pruned"
    receipts = await platform.client.memory_receipts(scope=forgotten.scope)
    platform.client.graph.create_prune_ghost(
        relationship_uuid=remembered.relationship_uuid,
        scope_key=forgotten.scope.key,
        prune_receipt_uuid=receipts[-1].receipt_uuid,
        reason="archived for the curation test",
        pruned_at=forgotten.pruned_at,
    )
    return remembered.relationship_uuid


@pytest.mark.parametrize("pure_read_retrieval", [False, True], ids=["default", "pure_read"])
@pytest.mark.asyncio
async def test_memory_search_reports_archived_memory_and_memory_restore_revives_it(
    tmp_path, pure_read_retrieval: bool
) -> None:
    """``memory_search`` reports archived matches; ``memory_restore`` owns revival.

    By default the search also revives what it matched, exactly as it always
    has.  With ``pure_read_retrieval`` set the search writes nothing and
    ``memory_restore`` is the only way back.  The curation tool itself behaves
    the same either way — that is the point of it.
    """
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "curation.sqlite",
        project_id="spymaster",
    )
    try:
        platform.client.config.pure_read_retrieval = pure_read_retrieval
        agent_memory_mcp.install_platform(platform)
        platform.register_agent(agent_id="platform", agent_name="Platform Agent")
        searched_uuid = await _archive_agent_memory(platform, object_value="reconcile vendor invoices monthly")
        # A second archived memory no query below matches, so the explicit
        # curation action is exercised on a row retrieval never touched.
        curated_uuid = await _archive_agent_memory(platform, object_value="rotate the datacenter keys quarterly")
        scope_key = (platform.user_scope or platform.agent_scope("platform")).key

        state_before = platform.client.graph.graph_state_hash(scope_key)
        searched = await platform.memory_search(
            agent_id="platform",
            query="vendor invoices",
            include_project=False,
        )
        assert searched.archived.count == 1
        assert searched.archived.matches[0].relationship_uuid == searched_uuid
        assert searched.archived.restore_action == "restore_archived_memory"
        if pure_read_retrieval:
            assert [item.relationship_uuid for item in searched.results] == []
            assert searched.archived.disposition == "available_to_restore"
            assert searched.archived.matches[0].revived is False
            assert platform.client.graph.graph_state_hash(scope_key) == state_before
        else:
            assert [item.relationship_uuid for item in searched.results] == [searched_uuid]
            assert searched.archived.disposition == "revived"
            assert searched.archived.matches[0].revived is True

        # memory_restore works in both modes, on a memory no search revived.
        restored = json.loads(
            await agent_memory_mcp.memory_restore(
                agent_id="platform",
                relationship_uuids_json=json.dumps([curated_uuid]),
                reason="the runbook is still current",
            )
        )
        assert len(restored) == 1
        assert restored[0]["relationship_uuid"] == curated_uuid
        assert restored[0]["status"] == "active"
        assert restored[0]["requested_by"] == "agent:platform"
        curated_search = await platform.memory_search(
            agent_id="platform",
            query="datacenter keys",
            include_project=False,
        )
        assert [item.relationship_uuid for item in curated_search.results] == [curated_uuid]
        assert curated_search.archived.count == 0

        if pure_read_retrieval:
            # The reported memory comes back only when curation asks for it.
            await platform.memory_restore(
                agent_id="platform",
                relationship_uuids=[searched_uuid],
                reason="the vendor runbook is still current",
            )
        after_restore = await platform.memory_search(
            agent_id="platform",
            query="vendor invoices",
            include_project=False,
        )
        assert [item.relationship_uuid for item in after_restore.results] == [searched_uuid]
        assert after_restore.archived.count == 0

        with pytest.raises(ValueError, match="cannot restore project memory directly"):
            await platform.memory_restore(
                agent_id="platform",
                relationship_uuids=[curated_uuid],
                reason="agent attempted project curation",
                scope="project",
            )
    finally:
        platform.client.graph.close()


def test_agent_skill_documents_the_canonical_registration_lifecycle() -> None:
    skill = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "memotron-sdk" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert skill.startswith("---\nname: memotron-sdk\n")
    assert "`simple` is the default" in skill
    assert "`multi-agent` preserves explicit agent ownership" in skill
    assert "Every process uses `memory_bootstrap(agent_id, agent_name)` before memory tools." in skill
    assert "ID and name are each case-insensitively unique" in skill
    assert "The same normalized pair reconnects idempotently." in skill
    assert "Do not generate an ID per task" in skill
    assert "`PreCompact`" in skill
    assert "`PostCompact`" in skill
    assert "`SessionEnd`" in skill
    assert "Never infer an outcome from rank" in skill
    assert "`memory_remember` writes user-owned" in skill
    assert "`memory_publish` stores attributed candidate evidence" in skill
    assert "`project_memory_candidates`" in skill
    assert "`memory_explain`" in skill
    assert "`memory_forget`" in skill
    assert "`/memory` is Claude Code's built-in" in skill
    assert "Keep it event-driven." in skill
    assert "memotron llm configure" in skill
    assert "--provider litellm" in skill
    assert "Never ask the user to paste the raw key into chat." in skill


def test_concurrent_same_agent_registration_is_an_idempotent_reconnect(
    tmp_path,
) -> None:
    graph_path = tmp_path / "concurrent-registration.sqlite"
    seed = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id="jedai-platform",
    )
    seed.client.graph.close()
    barrier = Barrier(2)

    def register() -> bool:
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id="jedai-platform",
        )
        try:
            barrier.wait(timeout=5)
            result = platform.register_agent(
                agent_id="claude-code",
                agent_name="Claude Code",
            )
            return result.created
        finally:
            platform.client.graph.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: register(), range(2)))

    assert sorted(results) == [False, True]


def test_mcp_runtime_transports_follow_live_environment(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MEMOTRON_LLM_MODEL", raising=False)
    monkeypatch.delenv("MEMOTRON_DREAM_AGENT_MODEL", raising=False)

    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, RuleBasedExtractionTransport)
    assert dream_agent is None

    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    # WS-24: an OpenAI-compatible key now also powers dream-agent decisions
    # (previously None — the dream agent was the one Anthropic-only component).
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert dream_agent.base_url == extraction.base_url
    assert dream_agent.api_key_env == "OPENAI_API_KEY"

    # The JedAI Gateway key outranks every other credential in the environment.
    monkeypatch.setenv("LITELLM_API_KEY", "test-gateway-key")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert extraction.api_key_env == GATEWAY_API_KEY_ENV
    assert extraction.base_url == DEFAULT_GATEWAY_BASE_URL
    assert extraction.model == DEFAULT_GATEWAY_MODEL
    assert dream_agent.model == DEFAULT_GATEWAY_MODEL

    # An Anthropic key cannot select anything: no Anthropic-native transport
    # exists, so the gateway path stays selected.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    assert extraction.api_key_env == GATEWAY_API_KEY_ENV

    monkeypatch.setenv("MEMOTRON_PROJECT_ID", "local-platform")
    monkeypatch.setenv("MEMOTRON_GRAPH_PATH", str(tmp_path / "agent-platform.sqlite"))
    platform = agent_memory_mcp.build_platform_from_env()
    assert platform.agent_ids == ()
    assert isinstance(platform.client._extractor._transport, OpenAICompatibleExtractionTransport)
    assert isinstance(platform.client._dream_agent_transport, OpenAICompatibleDreamAgentTransport)

    client = generic_mcp.build_client(graph_path=str(tmp_path / "generic-platform.sqlite"))
    assert isinstance(client._extractor._transport, OpenAICompatibleExtractionTransport)
    assert isinstance(client._dream_agent_transport, OpenAICompatibleDreamAgentTransport)


def test_explicit_dotenv_load_takes_gateway_default_without_exposing_secret(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A `.env` loaded EXPLICITLY still drives gateway defaults, and still never
    leaks the key into the credential state.

    T1-14: this used to rely on `build_transports_from_env()` picking the file up
    from the working directory by itself, which is the defect -- it made the
    builders adopt credentials from any directory they happened to run in.  The
    explicit `load_env_file(...)` below is what real callers now do
    (`_ensure_project_llm_credentials` anchors it to the project root).  The
    subject of the test -- gateway defaults and secret non-exposure -- is
    unchanged; only the route the key takes into the environment is.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("LITELLM_API_BASE", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("MEMOTRON_LLM_MODEL", raising=False)
    monkeypatch.delenv("MEMOTRON_DREAM_AGENT_MODEL", raising=False)
    secret = "test-gateway-key"
    (tmp_path / ".env").write_text(f"{GATEWAY_API_KEY_ENV}={secret}\n", encoding="utf-8")

    # The builders no longer read a `.env` on their own -- assert that first, so
    # this test also guards the T1-14 fix rather than merely tolerating it.
    assert isinstance(build_transports_from_env()[0], RuleBasedExtractionTransport)

    load_env_file(tmp_path / ".env")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert extraction.model == DEFAULT_GATEWAY_MODEL
    assert dream_agent.model == DEFAULT_GATEWAY_MODEL
    assert extraction.base_url == DEFAULT_GATEWAY_BASE_URL

    state = seed_tenant_llm_credentials_from_env(
        graph_path=tmp_path / "seeded.sqlite",
        tenant_id="local-platform",
    )
    assert state is not None
    assert state["provider"] == "litellm"
    assert state["model"] == DEFAULT_GATEWAY_MODEL
    assert state["base_url"] == DEFAULT_GATEWAY_BASE_URL
    assert state["has_api_key"] is True
    assert secret not in json.dumps(state)

    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "seeded.sqlite",
        project_id="local-platform",
    )
    try:
        stored = platform.client.graph.tenant_llm_credentials("local-platform")
        assert stored is not None
        assert stored["api_key"] == secret
        assert stored["model"] == DEFAULT_GATEWAY_MODEL
    finally:
        platform.client.graph.close()


def test_agent_memory_tenant_llm_credentials_are_stored_and_applied(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv(GATEWAY_API_KEY_ENV, raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "tenant-llm.sqlite",
        project_id="local-platform",
        agent_ids=("consumer-app",),
    )

    status = platform.configure_llm_credentials(
        provider="litellm",
        api_key="test-anthropic-key",
        base_url=DEFAULT_GATEWAY_BASE_URL,
        model="claude-sonnet-4-6",
    )

    assert status["tenant_id"] == "local-platform"
    assert status["provider"] == "litellm"
    assert status["has_api_key"] is True
    assert "test-anthropic-key" not in json.dumps(status)
    assert isinstance(platform.client._extractor._transport, OpenAICompatibleExtractionTransport)
    assert isinstance(platform.client._dream_agent_transport, OpenAICompatibleDreamAgentTransport)

    stored = platform.client.graph.tenant_llm_credentials("local-platform")
    assert stored is not None
    assert stored["api_key"] == "test-anthropic-key"
    assert "test-anthropic-key" not in json.dumps(platform.llm_credential_state())

    assert platform.clear_llm_credentials() is True
    assert platform.llm_credential_state() is None
    assert isinstance(platform.client._extractor._transport, RuleBasedExtractionTransport)
    assert platform.client._dream_agent_transport is None


@pytest.mark.asyncio
async def test_platform_memory_set_visibility_agent_plane_enforcement(tmp_path) -> None:
    """WS-19 T22 platform surface: an agent may restrict rows in its OWN
    writable scopes only (project refused), the calling agent's identity is
    enforced on memory_start/memory_search/memory_explain/memory_utility, and
    the operator plane (raw SDK reads) stays unaffected."""
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "visibility-platform.sqlite",
        project_id="jedai-platform",
        agent_ids=("alpha", "beta"),
    )
    fact = await platform.memory_remember(
        agent_id="alpha",
        subject="alpha",
        predicate="prefers",
        object="alpha-only deploy key rotation runbook",
        relationship_type="PREFERS",
    )

    # Simple mode shares the user scope: beta sees the row before restriction.
    before = await platform.memory_search(agent_id="beta", query="deploy key rotation runbook", include_project=False)
    assert fact.relationship_uuid in [item.relationship_uuid for item in before.results]

    restricted = await platform.memory_set_visibility(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        agents=("alpha",),
        reason="alpha-only working note",
    )
    assert restricted.visibility_agents == ("alpha",)
    assert restricted.set_by == "agent:alpha"

    # search: hidden from beta, present for alpha.
    after_beta = await platform.memory_search(
        agent_id="beta", query="deploy key rotation runbook", include_project=False
    )
    assert fact.relationship_uuid not in [item.relationship_uuid for item in after_beta.results]
    after_alpha = await platform.memory_search(
        agent_id="alpha", query="deploy key rotation runbook", include_project=False
    )
    assert fact.relationship_uuid in [item.relationship_uuid for item in after_alpha.results]

    # start context: beta's rendered profiles exclude the restricted row.
    beta_start = await platform.memory_start(agent_id="beta")
    assert "deploy key rotation runbook" not in beta_start.rendered_context
    alpha_start = await platform.memory_start(agent_id="alpha")
    assert "deploy key rotation runbook" in alpha_start.rendered_context

    # explain: fail-closed for beta, open for alpha.
    with pytest.raises(ValueError, match="not visible to agent"):
        await platform.memory_explain(agent_id="beta", relationship_uuid=fact.relationship_uuid)
    explained = await platform.memory_explain(agent_id="alpha", relationship_uuid=fact.relationship_uuid)
    assert explained.relationship_uuid == fact.relationship_uuid

    # utility: beta's projection omits the restricted row (alpha's start
    # recorded injection events for it).
    beta_utility = await platform.memory_utility(agent_id="beta")
    assert fact.relationship_uuid not in [item.relationship_uuid for item in beta_utility]
    alpha_utility = await platform.memory_utility(agent_id="alpha")
    assert fact.relationship_uuid in [item.relationship_uuid for item in alpha_utility]

    # Operator plane (reader None): the raw SDK still sees the row.
    operator_hits = await platform.client.search(query="deploy key rotation runbook", scope=platform.user_scope)
    assert fact.relationship_uuid in [item.relationship_uuid for item in operator_hits]

    # Authorization: project rows are refused on the agent surface, and only
    # registered agents may call.
    with pytest.raises(ValueError, match="cannot set project memory visibility"):
        await platform.memory_set_visibility(
            agent_id="alpha",
            relationship_uuid=fact.relationship_uuid,
            agents=("alpha",),
            reason="x",
            scope="project",
        )
    with pytest.raises(ValueError, match="not registered"):
        await platform.memory_set_visibility(
            agent_id="ghost",
            relationship_uuid=fact.relationship_uuid,
            agents=("alpha",),
            reason="x",
        )

    # Clearing restores shared visibility.
    cleared = await platform.memory_set_visibility(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        agents=None,
        reason="share again",
    )
    assert cleared.visibility_agents is None
    restored = await platform.memory_search(agent_id="beta", query="deploy key rotation runbook", include_project=False)
    assert fact.relationship_uuid in [item.relationship_uuid for item in restored.results]
