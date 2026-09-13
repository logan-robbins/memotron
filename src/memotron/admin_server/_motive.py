"""Which motive is in force for a scope, and the evidence for that answer.

Six functions. `scope_motive_evidence` is the interesting one -- the admin UI shows
not just the resolved motive but WHY, because a motive resolved from the wrong layer
changes what gets remembered and is otherwise invisible."""

from __future__ import annotations

from typing import Any

from memotron import (
    Memotron,
    MemoryScope,
    ScopeKind,
)
from memotron.agent_memory import (
    DEFAULT_AGENT_MEMORY_MOTIVE,
)


def resolve_scope_motive(
    *,
    client: Memotron,
    tenant_id: str,
    scope: MemoryScope | None,
    agent_id: str = "",
    requested_motive_name: str = "",
    active_prompt_motive_name: str = "",
    prompt_pack: str | None = None,
) -> dict[str, str]:
    """Resolve the Motive a formation run in ``scope`` would actually use.

    One resolver backs both the read surfaces (``/api/tenant-prompts``) and the
    write surface (``/api/dream-sequence/run``), so what the console displays can
    never drift from what the run applies. Precedence mirrors the run path
    exactly: an admin override, else the active tenant prompt's Motive (both
    passed to the control plane as request-level overrides), else whatever the
    control plane resolves for this tenant/agent/scope — which is the persisted
    project-memory Motive on a project scope, and the persisted assignment on an
    agent scope.
    """
    effective_motive_name = requested_motive_name or active_prompt_motive_name
    resolved_agent_id = agent_id
    if not resolved_agent_id and scope is not None and scope.kind == ScopeKind.AGENT:
        resolved_agent_id = scope.scope_id
    policy = None
    if scope is not None:
        try:
            policy = client.resolve_policy(
                tenant_id=tenant_id,
                agent_id=resolved_agent_id or None,
                scope=scope,
                motive=effective_motive_name or None,
                prompt_pack=prompt_pack,
            )
        except ValueError:
            policy = None
    if requested_motive_name:
        return {
            "name": (policy.motive_name if policy else "") or requested_motive_name,
            "source": "admin_override",
            "source_label": "admin override",
        }
    if active_prompt_motive_name:
        return {
            "name": (policy.motive_name if policy else "") or active_prompt_motive_name,
            "source": "active_prompt",
            "source_label": "active prompt override",
        }
    if policy is not None:
        source = policy.source_trace.get("motive", "none") if policy.motive_name else "none"
        return {
            "name": policy.motive_name or "",
            "source": source,
            "source_label": _motive_source_label(source),
        }
    motive_name = _default_motive_name(client, tenant_id)
    return {
        "name": motive_name,
        "source": "tenant_default" if motive_name else "none",
        "source_label": "tenant default" if motive_name else "none",
    }


def default_motive_payload(
    client: Memotron,
    tenant_id: str,
    *,
    scope: MemoryScope | None = None,
    agent_id: str = "",
) -> dict[str, Any]:
    active_version = client.graph.active_tenant_prompt_version(tenant_id)
    active_prompt_motive_name = (
        str(active_version["motive_name"])
        if active_version is not None and str(active_version.get("motive_name") or "")
        else ""
    )
    resolved = resolve_scope_motive(
        client=client,
        tenant_id=tenant_id,
        scope=scope,
        agent_id=agent_id,
        active_prompt_motive_name=active_prompt_motive_name,
    )
    return {**resolved, "scope": scope.key if scope is not None else ""}


def scope_motive_evidence(client: Memotron, scope: MemoryScope | None) -> dict[str, Any]:
    """Report the Motive actually STAMPED on this scope's materialized facts.

    Resolution answers "what does policy say now"; this answers "what formed these
    rows". The two legitimately diverge — policy can change long after formation —
    so the console shows both rather than letting the newer one quietly stand in
    for the older one. ``motive_name`` and ``motive_version_digest`` are lineage
    plane and stay readable even in a content-protected scope.
    """
    if scope is None:
        return {"scope": "", "fact_count": 0, "observed": []}
    counts: dict[tuple[str, str], int] = {}
    fact_count = 0
    for relationship in client.export_graph()["relationships"]:
        if relationship.get("type") == "MENTIONS":
            continue
        props = relationship.get("properties", {})
        if props.get("scope_key") != scope.key:
            continue
        fact_count += 1
        raw_name = props.get("motive_name")
        raw_digest = props.get("motive_version_digest")
        key = (
            str(raw_name) if isinstance(raw_name, str) and raw_name.strip() else "",
            str(raw_digest) if isinstance(raw_digest, str) and raw_digest.strip() else "",
        )
        counts[key] = counts.get(key, 0) + 1
    observed = [
        {
            "motive_name": name,
            "motive_version_digest": digest,
            "fact_count": count,
        }
        for (name, digest), count in counts.items()
    ]
    observed.sort(key=lambda item: (-item["fact_count"], item["motive_name"]))
    return {"scope": scope.key, "fact_count": fact_count, "observed": observed}


def _motive_source_label(source: str) -> str:
    labels = {
        "active_prompt": "active prompt override",
        "admin_override": "admin override",
        "request": "admin override",
        "scope": "scope policy",
        "dream_mode": "dream mode",
        "agent": "agent policy",
        "tenant": "tenant default",
        "tenant_default": "tenant default",
        "none": "none",
    }
    if source.startswith("dream_mode:"):
        return "dream mode"
    return labels.get(source, source.replace("_", " "))


def motive_options_payload(client: Memotron, tenant_id: str) -> list[dict[str, Any]]:
    bank = None
    if client.control_plane is not None:
        try:
            bank = client.control_plane.tenant(tenant_id).memory_bank
        except ValueError:
            bank = None
    bank = bank or client.config.memory_bank
    if bank is None:
        return []
    return [
        {
            "name": motive.name,
            "goal": motive.goal,
            "allowed_memory_types": [memory_type.value for memory_type in motive.allowed_memory_types],
            "prompt_profile": motive.prompt_profile or "",
            "prompt_profile_version": motive.prompt_profile_version or "",
            "dedup_threshold": motive.dedup_threshold,
            "retrieval_budget_share": motive.retrieval_budget_share,
            "salience_rubric": (
                motive.salience_rubric.model_dump(mode="json") if motive.salience_rubric is not None else None
            ),
        }
        for motive in bank.motives
    ]


def _default_motive_name(client: Memotron, tenant_id: str) -> str:
    if client.control_plane is not None:
        try:
            tenant = client.control_plane.tenant(tenant_id)
            if tenant.default_motive:
                return tenant.default_motive
        except ValueError:
            pass
    if client.config.memory_bank is not None:
        names = client.config.memory_bank.names
        if DEFAULT_AGENT_MEMORY_MOTIVE in names:
            return DEFAULT_AGENT_MEMORY_MOTIVE
        if names:
            return names[0]
    return ""
