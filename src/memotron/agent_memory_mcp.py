"""Agent-aware MCP tools for Memotron memory projects."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from memotron.agent_memory import AgentMemoryMode, AgentMemoryPlatform
from memotron.agents import DreamAgentTransport
from memotron.embedding import EmbeddingTransport
from memotron.extraction import ExtractionTransport
from memotron.mcp_auth import current_principal
from memotron.runtime import (
    advertised_mcp_url,
    build_embedding_transport_from_tenant_graph,
    build_synthesis_transport_from_tenant_graph,
    build_transports_from_tenant_graph,
    mcp_bind_from_env,
)
from memotron.synthesis import SynthesisTransport

#: This server's own bind defaults.  Loopback, because it is not the deployed one;
#: and 8010, so it runs alongside `memotron.mcp_server` on 8000 in local dev.
MCP_HOST = "127.0.0.1"
MCP_PORT = 8010

mcp = FastMCP(
    "memotron-agent-memory",
    instructions=(
        "Memotron provides durable personal/agent and governed project memory. "
        "At each process or session start, call memory_bootstrap once with one stable "
        "agent ID/name pair. Inject start.rendered_context and reuse start.task_run_id. "
        "Call memory_search before relying on prior requirements, decisions, incidents, "
        "preferences, or handoff state. Use memory_remember only for one exact durable "
        "personal/agent fact. Use memory_publish only for sourced evidence useful in a "
        "later project session; dreaming governs promotion and agents never write shared "
        "truth directly. Writes are event-driven, never per-turn; zero writes is valid. "
        "Review at confirmation, validation, handoff, and task-completion milestones. "
        "Keep returned use_events. Call memory_outcome only after a named "
        "user, test, or workflow observes the result; never infer a verdict. Harnesses "
        "must call memory_log at compaction/handoff boundaries and memory_start afterward; "
        "Claude Code hooks installed by memotron init do this automatically. Never "
        "store secrets, transient output, or model speculation. Call memory_contract for "
        "the complete mode, scope, registration, compaction, and Motive contract."
    ),
    # Identity only.  Serving -- bind address, Host allowlist, session mode, ASGI
    # middleware -- is `runtime.serve_mcp_http`'s, for the reasons in
    # memotron.mcp_server.  This server is deployed nowhere today, which is exactly
    # why it must not drift: it would inherit the other one's outage on its first
    # deploy.  Sharing the serving helper is what stops that.
)

_platform: AgentMemoryPlatform | None = None
_platform_api_url = ""
_mcp_url = ""


def _json_list(raw: str, label: str) -> list[str]:
    if not raw.strip():
        return []
    parsed: Any = json.loads(raw)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise ValueError(f"{label} must be a JSON array of strings")
    return parsed


def build_platform(
    *,
    graph_path: str | Path,
    project_id: str,
    agent_ids: list[str] | tuple[str, ...] = (),
    project_name: str | None = None,
    platform_api_url: str = "",
    mcp_url: str = "",
    extraction_transport: ExtractionTransport | None = None,
    dream_agent_transport: DreamAgentTransport | None = None,
    embedding_transport: EmbeddingTransport | None = None,
    rollup_synthesis_transport: SynthesisTransport | None = None,
    mode: AgentMemoryMode | str = AgentMemoryMode.SIMPLE,
    user_id: str | None = None,
) -> AgentMemoryPlatform:
    global _platform, _platform_api_url, _mcp_url
    _platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id=project_id,
        project_name=project_name,
        agent_ids=agent_ids,
        extraction_transport=extraction_transport,
        dream_agent_transport=dream_agent_transport,
        embedding_transport=embedding_transport,
        rollup_synthesis_transport=rollup_synthesis_transport,
        mode=mode,
        user_id=user_id,
        # #206 Phase 2: this transport's answer to "who is calling". `current_principal`
        # reads the in-flight MCP request and returns None outside one, which the guard
        # treats as a deployment fault only when the identity flag is armed.
        principal_provider=current_principal,
    )
    _platform_api_url = platform_api_url.strip()
    _mcp_url = mcp_url.strip()
    return _platform


def install_platform(
    platform: AgentMemoryPlatform,
    *,
    platform_api_url: str = "",
    mcp_url: str = "",
) -> AgentMemoryPlatform:
    """Install an existing platform instance for MCP tool handlers."""

    global _platform, _platform_api_url, _mcp_url
    _platform = platform
    _platform_api_url = platform_api_url.strip()
    _mcp_url = mcp_url.strip()
    return _platform


def build_platform_from_env() -> AgentMemoryPlatform:
    graph_path = os.environ.get("MEMOTRON_GRAPH_PATH", ".memotron/agent-memory.sqlite")
    project_id = os.environ.get("MEMOTRON_PROJECT_ID", "jedai-platform")
    extraction_transport, dream_agent_transport = build_transports_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=project_id,
    )
    embedding_transport = build_embedding_transport_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=project_id,
    )
    rollup_synthesis_transport = build_synthesis_transport_from_tenant_graph(
        graph_path=graph_path,
        tenant_id=project_id,
    )
    return build_platform(
        graph_path=graph_path,
        project_id=project_id,
        project_name=os.environ.get("MEMOTRON_PROJECT_NAME") or None,
        agent_ids=(),
        platform_api_url=os.environ.get("MEMOTRON_PLATFORM_API_URL", ""),
        mcp_url=os.environ.get("MEMOTRON_MCP_URL", ""),
        extraction_transport=extraction_transport,
        dream_agent_transport=dream_agent_transport,
        embedding_transport=embedding_transport,
        rollup_synthesis_transport=rollup_synthesis_transport,
        mode=os.environ.get("MEMOTRON_MODE", AgentMemoryMode.SIMPLE.value),
        user_id=os.environ.get("MEMOTRON_USER_ID") or None,
    )


def get_platform() -> AgentMemoryPlatform:
    if _platform is None:
        raise RuntimeError("Agent memory platform not initialised. Call build_platform() first.")
    return _platform


def _current_mcp_url() -> str:
    if _mcp_url:
        return _mcp_url
    # Read from the environment, not from the server object: FastMCP 4 has no
    # `.settings`, and the bind address is now an argument to `serve_mcp_http`. Same
    # helper and same defaults the entry point binds with, so the URL this advertises
    # cannot drift from the address the server is actually on.
    return advertised_mcp_url(*mcp_bind_from_env(default_host=MCP_HOST, default_port=MCP_PORT))


@mcp.tool()
async def memory_contract() -> str:
    """Read the complete mode, scope, lifecycle, and governance contract."""

    return json.dumps(
        get_platform().integration_contract(
            platform_api_url=_platform_api_url,
            mcp_url=_current_mcp_url(),
        )
    )


@mcp.tool()
async def memory_bootstrap(
    agent_id: str,
    agent_name: str,
    agent_token_budget: int = 1200,
    project_token_budget: int = 800,
    task_run_id: str = "",
) -> str:
    """Call once at session start to register and load prompt-ready memory."""

    result = await get_platform().memory_bootstrap(
        agent_id=agent_id,
        agent_name=agent_name,
        agent_token_budget=agent_token_budget,
        project_token_budget=project_token_budget,
        task_run_id=task_run_id or None,
        source="mcp",
    )
    return result.model_dump_json()


@mcp.tool()
async def agent_register(agent_id: str, agent_name: str) -> str:
    """Low-level registration; prefer memory_bootstrap at session start."""

    result = get_platform().register_agent(
        agent_id=agent_id,
        agent_name=agent_name,
        source="mcp",
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def tenant_llm_configure(
    provider: str,
    api_key: str,
    base_url: str = "",
    model: str = "",
) -> str:
    """Configure litellm (JedAI Gateway) or openai; never source keys from memory."""

    result = get_platform().configure_llm_credentials(
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
    return json.dumps(result)


@mcp.tool()
async def tenant_llm_status() -> str:
    """Return non-secret status for this tenant's stored LLM credentials."""

    return json.dumps(get_platform().llm_credential_state())


@mcp.tool()
async def tenant_llm_clear() -> str:
    """Remove this tenant's stored LLM credentials."""

    return json.dumps({"cleared": get_platform().clear_llm_credentials()})


@mcp.tool()
async def agent_motive_status(agent_id: str) -> str:
    """Return the persisted Motive assignment for one registered agent."""

    return json.dumps(get_platform().agent_motive(agent_id=agent_id))


@mcp.tool()
async def agent_motive_configure(agent_id: str, motive_name: str) -> str:
    """Persist and activate a validated Motive for one agent scope."""

    return json.dumps(
        get_platform().configure_agent_motive(
            agent_id=agent_id,
            motive_name=motive_name,
        )
    )


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return Response("ok", media_type="text/plain")


@mcp.tool()
async def memory_start(
    agent_id: str,
    agent_token_budget: int = 1200,
    project_token_budget: int = 800,
    task_run_id: str = "",
) -> str:
    """Low-level context load after registration; prefer memory_bootstrap initially."""

    result = await get_platform().memory_start(
        agent_id=agent_id,
        agent_token_budget=agent_token_budget,
        project_token_budget=project_token_budget,
        task_run_id=task_run_id or None,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_search(
    agent_id: str,
    query: str,
    include_project: bool = True,
    limit: int = 10,
    task_run_id: str = "",
) -> str:
    """Search before relying on prior work; reuse the active task_run_id."""

    result = await get_platform().memory_search(
        agent_id=agent_id,
        query=query,
        include_project=include_project,
        limit=limit,
        task_run_id=task_run_id or None,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_remember(
    agent_id: str,
    subject: str,
    predicate: str,
    object: str,
    relationship_type: str,
    source_text: str = "",
    confidence: float = 0.9,
) -> str:
    """Write one confirmed durable personal/agent fact; never project evidence."""

    result = await get_platform().memory_remember(
        agent_id=agent_id,
        subject=subject,
        predicate=predicate,
        object=object,
        relationship_type=relationship_type,
        source_text=source_text,
        confidence=confidence,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_publish(
    agent_id: str,
    content: str,
    task_run_id: str,
    source_reference: str = "",
) -> str:
    """Queue sourced cross-session project evidence; never secrets or speculation."""

    result = await get_platform().memory_publish(
        agent_id=agent_id,
        content=content,
        task_run_id=task_run_id,
        source_reference=source_reference,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_promote(
    agent_id: str,
    relationship_uuid: str,
    rationale: str,
    task_run_id: str = "",
) -> str:
    """Vote one exact own-scope fact up into project memory, lineage attached.

    Object-level promotion: the fact travels verbatim (never re-extracted by
    an LLM) with full lineage to its source row; your endorsement is recorded
    immediately, and formation waits until the project policy's
    min_endorsements votes exist. Re-promoting a pending fact endorses it.
    """

    result = await get_platform().memory_promote(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        rationale=rationale,
        task_run_id=task_run_id,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_endorse_promotion(
    agent_id: str,
    candidate_episode_uuid: str,
    rationale: str,
) -> str:
    """Add your vote to a pending promotion candidate (one vote per agent)."""

    result = await get_platform().memory_endorse_promotion(
        agent_id=agent_id,
        candidate_episode_uuid=candidate_episode_uuid,
        rationale=rationale,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_set_visibility(
    agent_id: str,
    relationship_uuid: str,
    agents_json: str,
    reason: str,
    scope: str = "default",
) -> str:
    """Restrict one own-scope memory to an agent allowlist; empty string clears.

    agents_json is a JSON array of agent ids (the allowlist), or "" to clear
    the restriction. Restricted rows disappear from other agents' reads;
    operator surfaces are unaffected. Project rows are refused.
    """

    agents = None if not agents_json.strip() else _json_list(agents_json, "agents_json")
    result = await get_platform().memory_set_visibility(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        agents=agents,
        reason=reason,
        scope=scope,
    )
    return result.model_dump_json()


@mcp.tool()
async def project_memory_config() -> str:
    """Return the active versioned project-memory goal and promotion policy."""

    return json.dumps(get_platform().project_memory_status())


@mcp.tool()
async def project_memory_candidates(
    agent_id: str,
    pending_only: bool = True,
    limit: int = 50,
) -> str:
    """List attributed project-memory candidates for review."""

    return json.dumps(
        get_platform().project_memory_candidates(
            agent_id=agent_id,
            pending_only=pending_only,
            limit=limit,
        )
    )


@mcp.tool()
async def project_memory_configure(
    project_goal: str,
    memory_goal: str,
    keep_json: str,
    configured_by: str,
    exclude_json: str = "[]",
    rules_json: str = "[]",
    allowed_memory_types_json: str = ('["requirement","directive","state","decision","incident","rollup"]'),
    protected_memory_types_json: str = '["requirement","decision","incident"]',
    min_salience: float = 0.0,
    max_memories_per_candidate: int = 12,
    dedup_threshold: float = 0.87,
    min_endorsements: int = 1,
) -> str:
    """Save and activate a new project-memory dream policy version."""

    result = get_platform().configure_project_memory(
        project_goal=project_goal,
        memory_goal=memory_goal,
        keep=_json_list(keep_json, "keep_json"),
        exclude=_json_list(exclude_json, "exclude_json"),
        rules=_json_list(rules_json, "rules_json"),
        allowed_memory_types=_json_list(
            allowed_memory_types_json,
            "allowed_memory_types_json",
        ),
        protected_memory_types=_json_list(
            protected_memory_types_json,
            "protected_memory_types_json",
        ),
        min_salience=min_salience,
        max_memories_per_candidate=max_memories_per_candidate,
        dedup_threshold=dedup_threshold,
        min_endorsements=min_endorsements,
        configured_by=configured_by,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_log(
    agent_id: str,
    summary: str = "",
    decisions_json: str = "[]",
    incidents_json: str = "[]",
    blockers_json: str = "[]",
    checkpoint_reason: str = "",
    task_run_id: str = "",
) -> str:
    """Queue learning; set context_compaction and the current task run at that boundary."""

    result = await get_platform().memory_log(
        agent_id=agent_id,
        summary=summary,
        decisions=_json_list(decisions_json, "decisions_json"),
        incidents=_json_list(incidents_json, "incidents_json"),
        blockers=_json_list(blockers_json, "blockers_json"),
        checkpoint_reason=checkpoint_reason,
        task_run_id=task_run_id,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_refresh(
    agent_id: str,
    include_project: bool = True,
) -> str:
    """Run due Memotron jobs for one agent scope and optionally the project scope."""

    result = await get_platform().memory_refresh(
        agent_id=agent_id,
        include_project=include_project,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_evolution(
    agent_id: str,
    include_project: bool = False,
) -> str:
    """Return memory health and compression proof for one agent."""

    result = await get_platform().memory_evolution(
        agent_id=agent_id,
        include_project=include_project,
    )
    return json.dumps(result.model_dump(mode="json"))


@mcp.tool()
async def memory_outcome(
    agent_id: str,
    use_id: str,
    verdict: str,
    task_run_id: str,
    idempotency_key: str,
    judge_identity: str,
    judge_version: str,
    scope: str = "default",
    attribution_method: str = "cited_full_credit",
) -> str:
    """Record a real evaluator's observed result for a returned use event; never infer it."""

    result = await get_platform().memory_outcome(
        agent_id=agent_id,
        use_id=use_id,
        verdict=verdict,
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
        judge_identity=judge_identity,
        judge_version=judge_version,
        scope=scope,
        attribution_method=attribution_method,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_utility(
    agent_id: str,
    relationship_uuid: str = "",
    scope: str = "default",
) -> str:
    """Return receipt-derived utility without changing memory truth."""

    result = await get_platform().memory_utility(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        scope=scope,
    )
    return json.dumps([item.model_dump(mode="json") for item in result])


@mcp.tool()
async def memory_explain(
    agent_id: str,
    relationship_uuid: str,
    scope: str = "default",
) -> str:
    """Return current fact state, lineage, and source evidence for one memory."""

    result = await get_platform().memory_explain(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        scope=scope,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_forget(
    agent_id: str,
    relationship_uuid: str,
    reason: str,
    scope: str = "default",
) -> str:
    """Soft-retire one exact memory after the user confirms the target."""

    result = await get_platform().memory_forget(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        reason=reason,
        scope=scope,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_pin(
    agent_id: str,
    relationship_uuid: str,
    reason: str,
    scope: str = "default",
) -> str:
    """Pin one exact memory for guaranteed retrieval in the caller's own writable scope."""

    result = await get_platform().memory_pin(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        reason=reason,
        scope=scope,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_unpin(
    agent_id: str,
    relationship_uuid: str,
    reason: str,
    scope: str = "default",
) -> str:
    """Remove a memory pin so the fact returns to normal ranking and retention."""

    result = await get_platform().memory_unpin(
        agent_id=agent_id,
        relationship_uuid=relationship_uuid,
        reason=reason,
        scope=scope,
    )
    return result.model_dump_json()


@mcp.tool()
async def memory_restore(
    agent_id: str,
    relationship_uuids_json: str,
    reason: str,
    scope: str = "default",
) -> str:
    """Revive archived memories reported by memory_search's ``archived`` matches.

    relationship_uuids_json: JSON array of relationship uuids taken from
    memory_search's ``archived.matches``. Needed when
    ``archived.disposition`` is 'available_to_restore' (the deployment made
    retrieval a pure read); by default a search revives what it matches and
    this is the way to ask for anything else back. Restoring is receipted; a
    crypto-shredded memory is refused.
    """

    results = await get_platform().memory_restore(
        agent_id=agent_id,
        relationship_uuids=_json_list(relationship_uuids_json, "relationship_uuids_json"),
        reason=reason,
        scope=scope,
    )
    return json.dumps([item.model_dump(mode="json") for item in results])
