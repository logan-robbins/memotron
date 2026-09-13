"""The control plane: tenants, agents, principals and their resolved policy.

The operator-facing surface of configuration -- register a tenant, attach a motive,
resolve what a given principal is allowed to do in a given scope. 341 lines and the
single largest definition in this module, which is why it gets a file rather than a
section."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from memotron.config._dream_config import (
    DreamConfig,
    default_config,
)
from memotron.config._instructions import (
    DreamPromptOverride,
    _normalize_non_blank_text,
)
from memotron.config._tenancy import (
    AgentMemoryPolicy,
    EffectiveMemoryPolicy,
    MemoryPrincipal,
    PrincipalRole,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.models import (
    DreamJobKind as DreamJobKind,
)
from memotron.models import (
    MemoryScope,
)
from memotron.models import (
    RelationshipCardinality as RelationshipCardinality,
)


class MemoryControlPlane(BaseModel):
    """Versioned multi-tenant resolver for memory policy.

    The control plane does not replace ``DreamConfig``.  It resolves one
    effective tenant/agent/scope policy over a base ``DreamConfig`` so runtime
    surfaces can use the same graph engine with deterministic per-request
    configuration.
    """

    base_config: DreamConfig = Field(default_factory=default_config)
    tenants: tuple[TenantMemoryPolicy, ...]

    @field_validator("tenants", mode="before")
    @classmethod
    def normalize_tenants(cls, value: Any) -> tuple[TenantMemoryPolicy, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(TenantMemoryPolicy.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("tenants must be a list or tuple of TenantMemoryPolicy objects")

    @model_validator(mode="after")
    def validate_control_plane(self) -> MemoryControlPlane:
        if not self.tenants:
            raise ValueError("at least one tenant policy is required")
        tenant_ids = [tenant.tenant_id for tenant in self.tenants]
        if len(tenant_ids) != len(set(tenant_ids)):
            raise ValueError("tenant_id values must be unique")
        for tenant in self.tenants:
            self._validate_tenant_references(tenant)
        return self

    def tenant(self, tenant_id: str) -> TenantMemoryPolicy:
        normalized = _normalize_non_blank_text(tenant_id, "tenant_id")
        for tenant in self.tenants:
            if tenant.tenant_id == normalized:
                return tenant
        known = [tenant.tenant_id for tenant in self.tenants]
        raise ValueError(f"unknown tenant_id {normalized!r}. Known tenants: {known}")

    def resolve(
        self,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> EffectiveMemoryPolicy:
        tenant = self.tenant(tenant_id)
        resolved_agent_id, agent_policy, agent_source = self._resolve_agent(tenant, agent_id)
        resolved_scope, scope_policy, scope_source = self._resolve_scope(tenant, agent_policy, scope)

        source_trace: dict[str, str] = {
            "tenant": f"tenant:{tenant.tenant_id}",
            "agent": agent_source,
            "scope": scope_source,
        }

        memory_bank = tenant.memory_bank or self.base_config.memory_bank
        prompt_profiles = tenant.prompt_profiles

        effective_agent = agent_policy.agent
        effective_profile = self.base_config.profile
        effective_dedup = self.base_config.dedup
        effective_pruning = self.base_config.pruning
        effective_governance = self.base_config.governance
        effective_approval = self.base_config.require_dream_agent_approval_for_untrusted_directives
        read_only = resolved_scope.key in self.base_config.read_only_scopes

        if tenant.profile is not None:
            effective_profile = tenant.profile
            source_trace["profile"] = "tenant"
        if tenant.dedup is not None:
            effective_dedup = tenant.dedup
            source_trace["dedup"] = "tenant"
        if tenant.pruning is not None:
            effective_pruning = tenant.pruning
            source_trace["pruning"] = "tenant"
        if tenant.governance is not None:
            effective_governance = tenant.governance
            source_trace["governance"] = "tenant"
        if tenant.require_dream_agent_approval_for_untrusted_directives is not None:
            effective_approval = tenant.require_dream_agent_approval_for_untrusted_directives
            source_trace["approval"] = "tenant"
        if resolved_scope.key in tenant.read_only_scope_keys:
            read_only = True
            source_trace["read_only"] = "tenant"

        if agent_policy.profile is not None:
            effective_profile = agent_policy.profile
            source_trace["profile"] = "agent"
        if agent_policy.dedup is not None:
            effective_dedup = agent_policy.dedup
            source_trace["dedup"] = "agent"
        if agent_policy.pruning is not None:
            effective_pruning = agent_policy.pruning
            source_trace["pruning"] = "agent"
        if agent_policy.governance is not None:
            effective_governance = agent_policy.governance
            source_trace["governance"] = "agent"
        if resolved_scope.key in agent_policy.read_only_scope_keys:
            read_only = True
            source_trace["read_only"] = "agent"

        mode_name = (
            self._normalize_optional_request_ref(dream_mode, "dream_mode")
            or (scope_policy.dream_mode if scope_policy is not None else None)
            or agent_policy.dream_mode
            or tenant.default_dream_mode
        )
        mode_source = (
            "request"
            if dream_mode is not None
            else "scope"
            if scope_policy is not None and scope_policy.dream_mode is not None
            else "agent"
            if agent_policy.dream_mode is not None
            else "tenant"
        )
        selected_mode = tenant.dream_mode(mode_name)
        source_trace["dream_mode"] = mode_source

        if selected_mode.profile is not None:
            effective_profile = selected_mode.profile
            source_trace["profile"] = f"dream_mode:{selected_mode.name}"
        if selected_mode.dedup is not None:
            effective_dedup = selected_mode.dedup
            source_trace["dedup"] = f"dream_mode:{selected_mode.name}"
        if selected_mode.pruning is not None:
            effective_pruning = selected_mode.pruning
            source_trace["pruning"] = f"dream_mode:{selected_mode.name}"
        if selected_mode.governance is not None:
            effective_governance = selected_mode.governance
            source_trace["governance"] = f"dream_mode:{selected_mode.name}"
        if selected_mode.require_dream_agent_approval_for_untrusted_directives is not None:
            effective_approval = selected_mode.require_dream_agent_approval_for_untrusted_directives
            source_trace["approval"] = f"dream_mode:{selected_mode.name}"

        if scope_policy is not None:
            if scope_policy.profile is not None:
                effective_profile = scope_policy.profile
                source_trace["profile"] = "scope"
            if scope_policy.dedup is not None:
                effective_dedup = scope_policy.dedup
                source_trace["dedup"] = "scope"
            if scope_policy.pruning is not None:
                effective_pruning = scope_policy.pruning
                source_trace["pruning"] = "scope"
            if scope_policy.governance is not None:
                effective_governance = scope_policy.governance
                source_trace["governance"] = "scope"
            if scope_policy.read_only is not None:
                read_only = scope_policy.read_only
                source_trace["read_only"] = "scope"

        pack_name = (
            self._normalize_optional_request_ref(prompt_pack, "prompt_pack")
            or (scope_policy.prompt_pack if scope_policy is not None else None)
            or selected_mode.prompt_pack
            or agent_policy.prompt_pack
            or tenant.default_prompt_pack
        )
        pack_source = None
        if prompt_pack is not None:
            pack_source = "request"
        elif scope_policy is not None and scope_policy.prompt_pack is not None:
            pack_source = "scope"
        elif selected_mode.prompt_pack is not None:
            pack_source = f"dream_mode:{selected_mode.name}"
        elif agent_policy.prompt_pack is not None:
            pack_source = "agent"
        elif tenant.default_prompt_pack is not None:
            pack_source = "tenant"
        selected_pack = tenant.prompt_pack(pack_name) if pack_name is not None else None
        if pack_source is not None:
            source_trace["prompt_pack"] = pack_source

        motive_name = (
            self._normalize_optional_request_ref(motive, "motive")
            or (scope_policy.motive if scope_policy is not None else None)
            or selected_mode.motive
            or agent_policy.motive
            or tenant.default_motive
        )
        motive_source = None
        if motive is not None:
            motive_source = "request"
        elif scope_policy is not None and scope_policy.motive is not None:
            motive_source = "scope"
        elif selected_mode.motive is not None:
            motive_source = f"dream_mode:{selected_mode.name}"
        elif agent_policy.motive is not None:
            motive_source = "agent"
        elif tenant.default_motive is not None:
            motive_source = "tenant"
        selected_motive = (
            memory_bank.motive(motive_name) if motive_name is not None and memory_bank is not None else None
        )
        if motive_name is not None and memory_bank is None:
            raise ValueError(f"motive {motive_name!r} requested but no memory bank is configured")
        if motive_source is not None:
            source_trace["motive"] = motive_source

        prompt_profile = selected_pack.prompt_profile if selected_pack is not None else None
        prompt_profile_version = selected_pack.prompt_profile_version if selected_pack is not None else None
        prompt_override = selected_pack.prompt_override if selected_pack is not None else DreamPromptOverride()
        if selected_motive is not None and selected_motive.prompt_profile is not None:
            prompt_profile = selected_motive.prompt_profile
            prompt_profile_version = selected_motive.prompt_profile_version or "v1"
            source_trace["prompt_profile"] = "motive"
        elif selected_pack is not None:
            source_trace["prompt_profile"] = "prompt_pack"

        return EffectiveMemoryPolicy(
            tenant_id=tenant.tenant_id,
            agent_id=resolved_agent_id,
            scope=resolved_scope,
            agent=effective_agent,
            dream_mode=selected_mode,
            enabled_job_kinds=selected_mode.enabled_job_kinds,
            prompt_pack=selected_pack,
            prompt_profile=prompt_profile,
            prompt_profile_version=prompt_profile_version,
            prompt_override=prompt_override,
            motive_name=motive_name,
            motive=selected_motive,
            memory_bank=memory_bank,
            governance=effective_governance,
            profile=effective_profile,
            dedup=effective_dedup,
            pruning=effective_pruning,
            read_only=read_only,
            require_dream_agent_approval_for_untrusted_directives=effective_approval,
            prompt_profiles=prompt_profiles,
            source_trace=source_trace,
        )

    def resolve_for_principal(
        self,
        *,
        principal: MemoryPrincipal,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> EffectiveMemoryPolicy:
        policy = self.resolve(
            tenant_id=principal.tenant_id,
            agent_id=principal.agent_id,
            scope=scope or principal.default_scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
        if principal.role != PrincipalRole.ADMIN:
            allowed_scope_keys = principal.effective_allowed_scope_keys()
            if policy.scope.key not in allowed_scope_keys:
                raise ValueError(
                    f"scope {policy.scope.key!r} is not authorized for principal "
                    f"{principal.principal_id!r}; allowed scopes: {sorted(allowed_scope_keys)}"
                )
            auth_source = "principal_allowed_scope"
        else:
            auth_source = "tenant_admin"
        return policy.model_copy(
            update={
                "source_trace": {
                    **policy.source_trace,
                    "principal": principal.principal_id,
                    "scope_authorization": auth_source,
                }
            }
        )

    def _resolve_agent(
        self,
        tenant: TenantMemoryPolicy,
        agent_id: str | None,
    ) -> tuple[str, AgentMemoryPolicy, str]:
        selected_agent_id = self._normalize_optional_request_ref(agent_id, "agent_id") or tenant.default_agent_id
        if selected_agent_id is None:
            raise ValueError(f"tenant {tenant.tenant_id!r} has no default_agent_id; pass agent_id explicitly")
        return (
            selected_agent_id,
            tenant.agent_policy(selected_agent_id),
            ("request" if agent_id is not None else "tenant_default"),
        )

    def _resolve_scope(
        self,
        tenant: TenantMemoryPolicy,
        agent_policy: AgentMemoryPolicy,
        scope: MemoryScope | None,
    ) -> tuple[MemoryScope, ScopeMemoryPolicy | None, str]:
        selected_scope = scope or agent_policy.default_scope or tenant.default_scope
        if selected_scope is None:
            raise ValueError(
                f"tenant {tenant.tenant_id!r} and agent {agent_policy.agent_id!r} have no default scope; pass scope explicitly"
            )
        registered = tenant.registered_scope_keys()
        if selected_scope.key not in registered:
            # #224: this message is rendered VERBATIM as the 400 body by admin_server, on
            # four routes that authenticate nobody (`/api/graph`, `/api/overview`,
            # `/api/profile`, `/api/archive`). It used to append
            # `registered scopes: {sorted(registered)}`, which turned any refusal into a
            # one-request enumeration of every registered `agent_id`, the tenant scope and
            # the user scope -- for a scope that need not exist, so not even a guessing
            # game. The list also GREW with use: probe agents from a morning of load
            # testing were in it by the afternoon.
            #
            # Deliberately NOT replaced with a count. A count still reports how many agents
            # a tenant runs, and nothing legitimate needs it here. Callers entitled to the
            # list have `/api/scopes`, which #200 bounded to the caller's own tenant.
            raise ValueError(f"scope {selected_scope.key!r} is not registered for tenant {tenant.tenant_id!r}")
        return (
            selected_scope,
            tenant.scope_policy(selected_scope),
            (
                "request"
                if scope is not None
                else "agent_default"
                if agent_policy.default_scope is not None
                else "tenant_default"
            ),
        )

    def _validate_tenant_references(self, tenant: TenantMemoryPolicy) -> None:
        prompt_profiles_by_key = {
            profile.key for profile in (*self.base_config.prompt_profiles, *tenant.prompt_profiles)
        }
        for pack in tenant.prompt_packs:
            key = f"{pack.prompt_profile}@{pack.prompt_profile_version}"
            if key not in prompt_profiles_by_key:
                raise ValueError(
                    f"tenant {tenant.tenant_id!r} prompt pack {pack.name!r} references unknown prompt profile {key!r}"
                )
        memory_bank = tenant.memory_bank or self.base_config.memory_bank
        known_motives = set(memory_bank.names) if memory_bank is not None else set()
        self._validate_optional_motive(tenant.default_motive, known_motives, tenant.tenant_id, "tenant default_motive")
        for agent in tenant.agents:
            self._validate_optional_motive(agent.motive, known_motives, tenant.tenant_id, "agent motive")
        for scope in tenant.scopes:
            self._validate_optional_motive(scope.motive, known_motives, tenant.tenant_id, "scope motive")
        for mode in tenant.dream_modes:
            self._validate_optional_motive(mode.motive, known_motives, tenant.tenant_id, "dream mode motive")

    @staticmethod
    def _validate_optional_motive(value: str | None, known: set[str], tenant_id: str, label: str) -> None:
        if value is None:
            return
        if value not in known:
            raise ValueError(f"tenant {tenant_id!r} {label} {value!r} is not in the configured memory bank")

    @staticmethod
    def _normalize_optional_request_ref(value: str | None, field_name: str) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, field_name)
