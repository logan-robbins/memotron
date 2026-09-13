"""What this platform promises an MCP client, rendered as a document.

450 lines producing the payload a client reads to learn what tools exist, what scopes
mean, and how to call them.

AGENT_MEMORY_REQUIRED_TOOLS travels with it, and that pairing is the point: the
contract serves that list as `required_tools`, and until 2026-08-28 nothing checked it
against the tools the server actually registers. They had drifted --
`memory_restore` was advertised and unreachable. tests/test_mcp_tool_registration.py
is the gate now; keeping the list beside the document that publishes it is so the two
cannot drift apart unnoticed again."""

from __future__ import annotations

from typing import Any

from memotron.agent_memory._common import (
    _normalize_non_blank,
)
from memotron.agent_memory._config import (
    DEFAULT_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._results import (
    AgentMemoryMode,
)
from memotron.agent_memory._scopes import (
    default_user_id,
    project_scope,
    user_scope,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
)
from memotron.identity import normalize_agent_id, normalize_agent_name

AGENT_MEMORY_MCP_SERVICE_NAME = "memotron-agent-memory"


RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME = "memotron_agent_memory"


AGENT_MEMORY_REQUIRED_TOOLS = (
    "memory_contract",
    "memory_bootstrap",
    "agent_register",
    "memory_start",
    "memory_search",
    "memory_remember",
    "memory_publish",
    "memory_log",
    "memory_refresh",
    "memory_evolution",
    "memory_explain",
    "memory_forget",
    "memory_restore",
    "memory_outcome",
    "memory_utility",
    "memory_promote",
    "memory_endorse_promotion",
    "memory_set_visibility",
    "project_memory_config",
    "project_memory_candidates",
    "project_memory_configure",
    "agent_motive_status",
    "agent_motive_configure",
)


AGENT_MEMORY_CHECKPOINT_REASONS = (
    "context_compaction",
    "handoff",
    "session_end",
)


def _normalized_base_url(value: str) -> str:
    normalized = value.strip()
    return normalized.rstrip("/") + "/" if normalized else ""


def _platform_url(base_url: str, path: str) -> str:
    normalized = _normalized_base_url(base_url)
    return f"{normalized}{path.lstrip('/')}" if normalized else ""


def _mcp_health_url(mcp_url: str) -> str:
    normalized = mcp_url.strip().rstrip("/")
    if not normalized:
        return ""
    origin = normalized.removesuffix("/mcp").rstrip("/")
    return f"{origin}/health"


def agent_memory_integration_contract(
    *,
    tenant_id: str,
    platform_api_url: str,
    mcp_url: str,
    mode: AgentMemoryMode | str = AgentMemoryMode.SIMPLE,
    user_id: str | None = None,
    registered_agents: list[dict[str, Any]] | tuple[dict[str, Any], ...] = (),
    pure_read_retrieval: bool = False,
) -> dict[str, Any]:
    """Return the non-secret hosted integration contract for agent hosts.

    ``pure_read_retrieval`` mirrors ``DreamConfig.pure_read_retrieval`` for this
    deployment so the published contract describes the retrieval mode the host
    will actually observe: False (the default) means a search revives the
    archived memories it matches, True means it only reports them.
    """

    normalized_tenant_id = _normalize_non_blank(tenant_id, "tenant_id")
    normalized_platform_api_url = _normalized_base_url(platform_api_url)
    normalized_mcp_url = mcp_url.strip()
    resolved_mode = AgentMemoryMode(mode)
    project = project_scope(normalized_tenant_id)
    personal = user_scope(user_id or default_user_id()) if resolved_mode == AgentMemoryMode.SIMPLE else None
    public_registered_agents = [
        {
            "agent_id": normalize_agent_id(str(item["agent_id"])),
            "agent_name": normalize_agent_name(str(item["agent_name"])),
        }
        for item in registered_agents
    ]
    return {
        "tenant_id": normalized_tenant_id,
        "mode": resolved_mode.value,
        "memory_owners": (["user", "project"] if resolved_mode == AgentMemoryMode.SIMPLE else ["agent", "project"]),
        "platform_api_url": normalized_platform_api_url,
        "ui_url": normalized_platform_api_url,
        "mcp_url": normalized_mcp_url,
        "recommended_mcp_server_name": RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME,
        "integration_contract_url": _platform_url(
            normalized_platform_api_url,
            "/api/platform/integration-contract",
        ),
        "status_url": _platform_url(normalized_platform_api_url, "/api/platform/status"),
        "mcp_server": {
            "recommended_name": RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME,
            "service_name": AGENT_MEMORY_MCP_SERVICE_NAME,
            "url": normalized_mcp_url,
            "health_url": _mcp_health_url(normalized_mcp_url),
            "required": True,
            "tool_timeout_ms": 120000,
            "approval_recommendation": (
                "After trusting this tenant, allow automatic execution of the listed "
                "Memotron memory tools so session startup and refresh are not blocked."
            ),
        },
        "quickstart": {
            "first_tool": "memory_bootstrap",
            "hosted_api_url": _platform_url(
                normalized_platform_api_url,
                "/api/platform/memory/bootstrap",
            ),
            "manual_equivalent": [
                "memory_contract",
                "agent_register",
                "memory_start",
            ],
            "then": [
                "Inject start.rendered_context into the active task context.",
                "Reuse start.task_run_id for searches, publications, and outcomes.",
                "Call memory_search before relying on prior work.",
            ],
        },
        "memory_decision_policy": {
            "cadence": "event_driven_not_periodic",
            "zero_writes_are_valid": True,
            "search_when": [
                "At a new task or topic, after compaction, or before relying on prior work.",
                "When current evidence conflicts with remembered state.",
            ],
            "remember_when": [
                "An exact durable personal/agent fact is explicitly stated or confirmed.",
            ],
            "publish_when": [
                "A sourced requirement, decision, serious incident/root cause, durable mitigation, blocker, or handoff is confirmed.",
                "The evidence is likely to matter in a later project session.",
            ],
            "do_not_store": [
                "Secrets or credentials.",
                "Raw transcripts, transient tool output, or scratch work.",
                "Unverified model speculation.",
                "Facts cheaply recoverable from the repository.",
            ],
            "refresh_when": (
                "After a meaningful batch must be visible immediately; otherwise rely "
                "on scheduled lifecycle maintenance."
            ),
            "restore_when": (
                "When result.archived.count is non-zero and one of those archived "
                "facts is actually needed, call memory_restore with that exact "
                "relationship_uuid and a specific reason. Required when "
                "result.archived.disposition is 'available_to_restore' (searching "
                "reported the memory without reviving it); optional otherwise, "
                "because the search already revived it."
            ),
        },
        "retrieval_contract": {
            "reads_are_pure": pure_read_retrieval,
            "archived_match_disposition": ("available_to_restore" if pure_read_retrieval else "revived"),
            "explanation": (
                "By default memory_search revives an archived memory whose prune "
                "ghost matches the query, as it always has, and reports it on "
                "result.archived with disposition 'revived'. Where the deployment "
                "sets pure_read_retrieval (so retrieval takes no row locks and can "
                "run on more than one replica), memory_search writes nothing: "
                "archived matches are reported with disposition "
                "'available_to_restore' and are revived only by memory_restore. "
                "memory_restore is available in both modes."
            ),
            "archived_match_report_field": "archived",
            "restore_tool": "memory_restore",
        },
        "llm_configuration": {
            "supported_providers": ["litellm", "openai"],
            "local_cli": (
                "memotron llm configure --provider litellm "
                f"--model {DEFAULT_GATEWAY_MODEL} "
                f"--base-url {DEFAULT_GATEWAY_BASE_URL} "
                f"--api-key-env {GATEWAY_API_KEY_ENV}"
            ),
            "local_status": "memotron llm status",
            "mcp_tools": [
                "tenant_llm_status",
                "tenant_llm_configure",
                "tenant_llm_clear",
            ],
            "secret_rule": (
                "Keep API keys in environment variables or a gitignored .env file. "
                "Never place raw keys in .memotron.yaml, prompts, memories, or logs."
            ),
            "litellm": {
                "transport": "OpenAI-compatible chat completions",
                "required": ["base_url", "model", "api_key"],
                "recommended_api_key_env": GATEWAY_API_KEY_ENV,
                "default_base_url": DEFAULT_GATEWAY_BASE_URL,
                "default_model": DEFAULT_GATEWAY_MODEL,
                "model_alias_rule": (
                    "Gateway model names are UNDATED aliases; dated Anthropic ids "
                    "such as claude-haiku-4-5-20251001 do not resolve."
                ),
            },
        },
        "required_tools": list(AGENT_MEMORY_REQUIRED_TOOLS),
        "available_read_only_tools": [
            "memory_contract",
            "memory_explain",
            "project_memory_config",
            "project_memory_candidates",
            "tenant_llm_status",
        ],
        "recommended_lifecycle_sequence": [
            "memory_bootstrap",
            "memory_search",
            "memory_remember",
            "memory_publish",
            "memory_outcome",
            "memory_log",
            "memory_refresh",
        ],
        "recommended_call_sequence": [
            {
                "order": 1,
                "tool": "memory_bootstrap",
                "required": True,
                "purpose": (
                    "Register or reconnect one stable identity and return prompt-ready "
                    "memory, one task_run_id, use receipts, and the next actions."
                ),
            },
            {
                "order": 2,
                "tool": "memory_search",
                "required": True,
                "purpose": (
                    "Search before relying on prior decisions, requirements, incidents, "
                    "preferences, or handoff state. Reuse the bootstrap task_run_id. "
                    "Archived matches are reported on result.archived; "
                    "result.archived.disposition says whether the search revived "
                    "them (default) or left them for memory_restore."
                ),
            },
            {
                "order": 3,
                "tool": "memory_remember",
                "required": True,
                "purpose": (
                    (
                        "Write exact durable facts to the user's personal scope. "
                        if resolved_mode == AgentMemoryMode.SIMPLE
                        else "Write exact durable facts to this agent's private scope. "
                    )
                    + "Project memory must use memory_publish so the configured "
                    "project-memory dream can govern it."
                ),
            },
            {
                "order": 4,
                "tool": "memory_publish",
                "required": False,
                "condition": "candidate_is_useful_across_project_agents",
                "purpose": (
                    "Submit attributed evidence to the project scope as a candidate. "
                    "It is not a direct fact write; the active versioned project-memory "
                    "policy governs formation."
                ),
            },
            {
                "order": 5,
                "tool": "memory_outcome",
                "required": False,
                "condition": "after_observable_result",
                "purpose": (
                    "After an observable task result, judge each materially relied-on "
                    "use_id returned by memory_start or memory_search. Never infer or "
                    "fabricate an outcome."
                ),
            },
            {
                "order": 6,
                "tool": "memory_log",
                "required": True,
                "purpose": (
                    "Queue reusable session learning. Before harness context compaction, "
                    'set checkpoint_reason="context_compaction" and pass the current '
                    "task_run_id. The episode is curated later and does not immediately "
                    "materialize facts."
                ),
            },
            {
                "order": 7,
                "tool": "memory_refresh",
                "required": True,
                "purpose": ("Run due curation/dreaming jobs for the active durable scopes."),
            },
        ],
        "agent_registration": {
            "model": "explicit_idempotent",
            "tool": "agent_register",
            "hosted_api_path": "/api/platform/agents/register",
            "hosted_api_url": _platform_url(
                normalized_platform_api_url,
                "/api/platform/agents/register",
            ),
            "required_before_memory_calls": True,
            "agent_id": {
                "purpose": "Stable machine identity for one logical agent.",
                "format": ("1-64 ASCII letters, digits, '.', '_', or '-'; must start and end with a letter or digit."),
                "uniqueness": "Case-insensitively unique within this tenant.",
            },
            "agent_name": {
                "purpose": "Stable human-readable display name for the same logical agent.",
                "format": "Non-blank, whitespace-normalized, at most 128 characters.",
                "uniqueness": "Case-insensitively unique within this tenant.",
            },
            "reconnect": (
                "Call agent_register on every process startup. Registering the same "
                "normalized agent_id/agent_name pair is idempotent and refreshes last_seen_at."
            ),
            "collision_behavior": (
                "If either value is already paired with a different counterpart, "
                "registration fails. Choose another identity; do not silently share memory."
            ),
            "identity_semantics": (
                "Two processes that intentionally use the same normalized pair are the same "
                "logical agent and share agent-scoped memory. Registration does not "
                "authenticate process ownership."
            ),
            "registered_agents": public_registered_agents,
        },
        "scope_rules": {
            **(
                {
                    "personal": {
                        "write_tool": "memory_remember",
                        "scope_key_format": personal.key if personal is not None else "",
                        "meaning": (
                            "User-owned memory shared by this user's Claude Code sessions across repositories."
                        ),
                    }
                }
                if resolved_mode == AgentMemoryMode.SIMPLE
                else {
                    "agent": {
                        "write_tool": "memory_remember",
                        "scope_key_format": "agent:<agent_id>",
                        "meaning": "Pod-local memory for one stable agent_id.",
                    }
                }
            ),
            "project": {
                "write_tool": "memory_publish",
                "scope_key_format": project.key,
                "meaning": (
                    "Tenant-wide project memory shared by registered agents. "
                    "Agent candidates materialize only through the configured "
                    "project-memory dream."
                ),
            },
        },
        "continuity_scope": {
            "scope_key_format": "agent:<agent_id>",
            "visible_memory_owner": resolved_mode == AgentMemoryMode.MULTI_AGENT,
            "meaning": (
                "Internal session and compaction continuity for the registered caller."
                if resolved_mode == AgentMemoryMode.SIMPLE
                else "Durable memory owned by one registered agent."
            ),
        },
        "project_memory": {
            "configuration_required_before_publish": True,
            "status_tool": "project_memory_config",
            "configure_tool": "project_memory_configure",
            "publish_tool": "memory_publish",
            "hosted_config_path": "/api/platform/project-memory/config",
            "hosted_publish_path": "/api/platform/memory/publish",
            "policy_inputs": [
                "project_goal",
                "memory_goal",
                "keep",
                "exclude",
                "rules",
                "allowed_memory_types",
                "protected_memory_types",
                "min_salience",
                "max_memories_per_candidate",
                "dedup_threshold",
            ],
            "flow": [
                "An operator saves a versioned project-memory policy.",
                "An agent submits evidence with memory_publish.",
                "memory_refresh runs formation in the tenant project scope.",
                "The project Motive and prompt apply hard type/salience gates and user guidance.",
                "Accepted memories become shared; rejected candidates remain auditable episodes.",
            ],
            "no_agent_bypass": (
                (
                    "memory_remember writes only personal scope. "
                    if resolved_mode == AgentMemoryMode.SIMPLE
                    else "memory_remember writes only agent scope. "
                )
                + "Agents cannot directly "
                "materialize project facts."
            ),
        },
        "outcome_reporting": {
            "model": "explicit_observed_only",
            "source_use_events": [
                "memory_start.use_events",
                "memory_search.use_events",
            ],
            "trigger": (
                "Call memory_outcome only after a user, test, workflow, or other named evaluator has observed a result."
            ),
            "linkage": (
                "Pass the returned use_id and its exact task_run_id. Set scope to "
                '"personal", "agent", or "project" to match the use event.'
            ),
            "judge": (
                "judge_identity and judge_version identify the real evaluator. "
                "Do not claim that Memotron inferred the verdict."
            ),
            "no_result": (
                "If no evaluation has occurred, defer outcome reporting. Use verdict "
                '"unknown" only when the named evaluator explicitly judged the result inconclusive.'
            ),
            "effect": (
                "memory_utility aggregates immutable use and outcome receipts. "
                "It does not rewrite memory truth or confidence."
            ),
        },
        "context_compaction": {
            "detection": (
                "Memotron cannot detect a harness context compaction; the harness must signal the boundary."
            ),
            "before": [
                (
                    "Write exact personal changes with memory_remember."
                    if resolved_mode == AgentMemoryMode.SIMPLE
                    else "Write exact agent-private changes with memory_remember."
                ),
                ("When project memory is configured, submit genuinely cross-agent evidence with memory_publish."),
                "Report every already-observed result with memory_outcome.",
                ('Call memory_log with checkpoint_reason="context_compaction" and the current task_run_id.'),
            ],
            "after": [
                "Keep the same registered agent_id and agent_name.",
                "Call memory_start with a new task_run_id and inject rendered_context.",
                "Call memory_search for the active task or handoff before continuing.",
            ],
            "checkpoint_reasons": list(AGENT_MEMORY_CHECKPOINT_REASONS),
            "internal_compaction": (
                "Harness context compaction is separate from Memotron dreaming, "
                "which deduplicates, consolidates, supersedes, and prunes curated memory "
                "while preserving immutable source episodes."
            ),
        },
        "project_scope": project.model_dump(mode="json"),
        "project_scope_key": project.key,
        "user_scope": (personal.model_dump(mode="json") if personal is not None else None),
        "user_scope_key": personal.key if personal is not None else None,
        "motive": {
            "selection": "automatic_unless_explicitly_overridden",
            "default_motive": DEFAULT_AGENT_MEMORY_MOTIVE,
            "guidance": (
                "Callers normally do not choose Motive. Memotron resolves Motive "
                "from active prompt, scope, agent, dream mode, and tenant policy unless "
                "the caller intentionally passes an override on dream-specific APIs."
            ),
        },
        "motive_guidance": "automatic unless intentionally overridden",
        "seeding_required": False,
        "read_only": True,
        "non_secret": True,
    }
