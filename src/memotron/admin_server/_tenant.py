"""Tenant configuration and prompt state, as the console presents it.

The largest group, and it sits at the top of the local dependency order -- it composes
_motive and _snippets. `tenant_prompts_payload` is 122 lines because it reconciles
three sources of prompt truth (tenant override, pack default, profile version) and has
to show which won."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from memotron import (
    DreamPromptOverride,
    Memotron,
    MemoryScope,
)
from memotron.admin_server._motive import (
    default_motive_payload,
    motive_options_payload,
    scope_motive_evidence,
)
from memotron.admin_server._snippets import (
    codex_mcp_config_snippet,
    mcp_lifecycle_snippet,
    sdk_lifecycle_snippet,
)
from memotron.agent_memory import (
    TENANT_ACTIVE_PROMPT_PACK,
    TENANT_ACTIVE_PROMPT_PROFILE,
    TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
    agent_memory_integration_contract,
)
from memotron.gateway import GATEWAY_API_KEY_ENV


def _environment_llm_key_env() -> str:
    """Name of the process env var the runtime will actually use, if any.

    Mirrors ``runtime._env_endpoint``'s precedence (the JedAI Gateway key
    wins over a generic OpenAI-compatible key) without importing that
    private helper. Only variable presence is checked here, never the value.
    """
    if os.environ.get(GATEWAY_API_KEY_ENV, "").strip():
        return GATEWAY_API_KEY_ENV
    if os.environ.get("OPENAI_API_KEY", "").strip():
        return "OPENAI_API_KEY"
    return ""


def effective_llm_status(sealed_status: dict[str, Any]) -> dict[str, Any]:
    """Layer the live environment fallback onto a sealed-credential status.

    ``client.tenant_llm_credential_state`` only reports whether a credential
    was sealed into the graph via ``memotron llm configure`` /
    the Tenant Setup screen. Absent that, ``runtime.build_transports_from_env``
    still builds a live extraction/dream-agent transport straight from
    ``LITELLM_API_KEY`` (or ``OPENAI_API_KEY``) in the process environment —
    the common case for this admin server, started with the key already
    exported. Reporting the sealed-only status as "unset" in that case is
    misleading: extraction is genuinely running. This never overrides an
    actual sealed credential's fields.
    """
    if sealed_status.get("has_api_key"):
        return {**sealed_status, "source": "sealed"}
    env_key = _environment_llm_key_env()
    if not env_key:
        return sealed_status
    return {
        **sealed_status,
        "has_api_key": True,
        "source": "environment",
        "environment_key_env": env_key,
    }


def tenant_config_payload(
    *,
    client: Memotron,
    tenant_id: str,
    agent_ids: tuple[str, ...],
    graph_path: Path | None,
    default_scope: MemoryScope,
    ui_url: str,
    mcp_url: str,
    mode: str = "simple",
    user_id: str | None = None,
    warnings: tuple[str, ...] = (),
) -> dict[str, Any]:
    llm = client.tenant_llm_credential_state(tenant_id)
    llm_status = effective_llm_status(llm or {"tenant_id": tenant_id, "has_api_key": False})
    example_agent = agent_ids[0] if agent_ids else "your-agent-id"
    graph_agents = client.graph.tenant_agents(tenant_id)
    example_registration = next(
        (item for item in graph_agents if item["agent_id"] == example_agent),
        None,
    )
    example_agent_name = (
        str(example_registration["agent_name"]) if example_registration is not None else "Your Agent Name"
    )
    contract = agent_memory_integration_contract(
        tenant_id=tenant_id,
        platform_api_url=ui_url,
        mcp_url=mcp_url,
        mode=mode,
        user_id=user_id,
        registered_agents=graph_agents,
        pure_read_retrieval=client.config.pure_read_retrieval,
    )
    return {
        "tenant": {
            "tenant_id": tenant_id,
            "agent_ids": list(agent_ids),
            "example_agent_id": example_agent,
            "default_scope": default_scope.key,
            # `graph_tenant_ids` was REMOVED here, not renamed. It returned
            # `known_tenant_ids()` -- a UNION across six tenant-keyed governance tables --
            # so /api/tenant-config and /api/overview handed EVERY tenant id in the store
            # to any caller that could reach the port, in every environment, on a surface
            # that authenticates nobody (`principal` is a class attribute). Nothing needs it.
            #
            # THIS IS NOT THE WHOLE LEAK, and an earlier version of this comment said it
            # was. Measured 2026-09-08 against the real handler with two tenants holding
            # memory: `/api/overview.scopes` and `/api/scopes` still list neighbouring
            # scope keys, and `/api/graph?scope=<neighbour>` returns their memory CONTENT
            # -- `--no-admin-writes` gates writes, not reads. The cause is upstream of
            # this field: `client.authorized_scope_keys` is None on the admin client, so
            # `_require_authorized_scope` is a no-op and `build_demo_control_plane`
            # registers every scope `discover_graph_scopes` finds. Removing this field
            # closes tenant-id enumeration through the tenant block and nothing more; the
            # scope-plane exposure is pre-existing, is tracked separately, and is not
            # exploitable on `latest` today only because it holds exactly one tenant.
            #
            # No UI component read it -- it existed only as an optional field in
            # types.ts and a test fixture, both deleted with this. The name was also
            # actively misleading: `graph_` implies the memory graph, but it never
            # touched a memory scope, which is how "jedai-platform holds memory" got
            # believed off a field that only proved a credential had been sealed.
            "warnings": list(warnings),
        },
        "operator": {
            # None whenever this server runs on the operational store. Reporting the
            # BACKEND rather than "None" is the point of the field for an operator: the
            # deployed admin surface used to report a /tmp demo SQLite path while the
            # real store was Postgres, which is how a fabricated overview passes for a
            # real one.
            "graph_path": str(graph_path) if graph_path is not None else None,
            "store": "sqlite" if graph_path is not None else type(client.graph).__name__,
            "platform_api_url": ui_url,
            "ui_url": ui_url,
            "mcp_url": mcp_url,
            "mcp_configured": bool(mcp_url),
            "integration_contract_url": contract["integration_contract_url"],
        },
        "llm": llm_status,
        "integration": {
            "contract_url": contract["integration_contract_url"],
            "labels": [
                "No seeding required",
                "One-call session bootstrap",
                "Agent IDs and names are tenant-unique",
                "Motive resolves automatically",
            ],
        },
        "snippets": {
            "sdk": sdk_lifecycle_snippet(
                platform_api_url=ui_url,
                agent_id=example_agent,
                agent_name=example_agent_name,
            ),
            "mcp": mcp_lifecycle_snippet(
                mcp_url=mcp_url,
                agent_id=example_agent,
                agent_name=example_agent_name,
            ),
            "codex_mcp_config": codex_mcp_config_snippet(mcp_url=mcp_url),
        },
    }


def tenant_prompts_payload(
    client: Memotron,
    tenant_id: str,
    *,
    scope: MemoryScope | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    library = [
        {
            "key": f"library:{profile.key}",
            "kind": "library",
            "label": f"{profile.key} (library)",
            "profile": profile.name,
            "profile_version": profile.version,
            "prompt_text": profile.render_prompt(),
            "motive_name": "",
            "created_at": "",
            "updated_at": "",
            "active": False,
        }
        for profile in client.config.prompt_profiles
    ]
    saved_versions = [
        {
            "key": f"tenant:{version['version']}",
            "kind": "tenant",
            "label": f"{version['version']} (saved)",
            "version": version["version"],
            "profile": version["source_profile"],
            "profile_version": version["source_profile_version"],
            "prompt_text": version["prompt_text"],
            "motive_name": version["motive_name"],
            "created_at": version["created_at"],
            "updated_at": version["updated_at"],
            "active": version["active"],
        }
        for version in client.graph.tenant_prompt_versions(tenant_id)
    ]
    active_version = client.graph.active_tenant_prompt_version(tenant_id)
    if active_version is not None:
        current = {
            "key": f"tenant:{active_version['version']}",
            "kind": "tenant",
            "label": f"{active_version['version']} (active)",
            "version": active_version["version"],
            "profile": active_version["source_profile"],
            "profile_version": active_version["source_profile_version"],
            "prompt_text": active_version["prompt_text"],
            "motive_name": active_version["motive_name"],
            "created_at": active_version["created_at"],
            "updated_at": active_version["updated_at"],
            "active": True,
        }
    else:
        legacy_state = client.graph.tenant_prompt_override(tenant_id)
        if legacy_state is not None:
            override = DreamPromptOverride.model_validate(legacy_state["override"])
            profile_name = str(legacy_state["prompt_profile"])
            profile_version = str(legacy_state["prompt_profile_version"])
            profile = client.config.prompt_profile(profile_name, profile_version).with_override(override)
            current = {
                "key": f"legacy:{profile.key}",
                "kind": "legacy",
                "label": f"{profile.key} (current)",
                "version": "",
                "profile": profile_name,
                "profile_version": profile_version,
                "prompt_text": profile.render_prompt(),
                "motive_name": "",
                "created_at": str(legacy_state["created_at"]),
                "updated_at": str(legacy_state["updated_at"]),
                "active": True,
            }
        else:
            profile = client.config.prompt_profile(
                TENANT_ACTIVE_PROMPT_PROFILE,
                TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
            )
            current = {
                "key": f"library:{profile.key}",
                "kind": "library",
                "label": f"{profile.key} (default)",
                "version": "",
                "profile": profile.name,
                "profile_version": profile.version,
                "prompt_text": profile.render_prompt(),
                "motive_name": "",
                "created_at": "",
                "updated_at": "",
                "active": True,
            }
    resolved_motive = default_motive_payload(
        client,
        tenant_id,
        scope=scope,
        agent_id=agent_id,
    )
    evidence = scope_motive_evidence(client, scope)
    formed_motive_names = {str(item["motive_name"]) for item in evidence["observed"] if str(item["motive_name"])}
    return {
        "tenant_id": tenant_id,
        "active_pack": TENANT_ACTIVE_PROMPT_PACK,
        "current": current,
        "versions": saved_versions,
        "library": library,
        "motives": motive_options_payload(client, tenant_id),
        "resolved_motive": resolved_motive,
        "motive_evidence": {
            **evidence,
            "divergent": bool(
                formed_motive_names
                and (not resolved_motive["name"] or formed_motive_names != {resolved_motive["name"]})
            ),
        },
    }
