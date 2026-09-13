"""Rendering the effective policy for a principal."""

from __future__ import annotations

from memotron import (
    MemoryPrincipal,
)


def effective_policy_payload(policy, principal: MemoryPrincipal) -> dict:
    return {
        "principal": {
            "principal_id": principal.principal_id,
            "tenant_id": principal.tenant_id,
            "agent_id": principal.agent_id,
            "role": principal.role.value,
            "default_scope": principal.default_scope.key if principal.default_scope else None,
            "allowed_scope_keys": sorted(principal.effective_allowed_scope_keys()),
        },
        "policy": {
            "tenant_id": policy.tenant_id,
            "agent_id": policy.agent_id,
            "scope": policy.scope.model_dump(mode="json"),
            "dream_mode": policy.dream_mode.name,
            "dream_mode_description": policy.dream_mode.description,
            "enabled_job_kinds": [kind.value for kind in policy.enabled_job_kinds],
            "prompt_pack": policy.prompt_pack.name if policy.prompt_pack else None,
            "prompt_profile": policy.prompt_profile,
            "prompt_profile_version": policy.prompt_profile_version,
            "motive_name": policy.motive_name,
            "motive_goal": policy.motive.goal if policy.motive else None,
            "read_only": policy.read_only,
            "approval_required_for_untrusted_directives": (
                policy.require_dream_agent_approval_for_untrusted_directives
            ),
            "dedup_cosine_threshold": policy.dedup.cosine_threshold,
            "dedup_memory_type_thresholds": policy.dedup.memory_type_thresholds,
            "profile_render_mode": policy.profile.render_mode,
            "profile_static_limit": policy.profile.max_static_facts,
            "profile_dynamic_limit": policy.profile.max_dynamic_facts,
            "source_trace": policy.source_trace,
            "policy_alias": policy.policy_alias,
            "policy_contract_digest": policy.policy_contract_digest,
            "certification_verdict": policy.certification_verdict,
        },
    }


def _platform_url(base_url: str, path: str) -> str:
    normalized = base_url.strip().rstrip("/")
    return f"{normalized}/{path.lstrip('/')}" if normalized else ""
