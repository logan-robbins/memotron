"""Multi-tenant policy resolution -- who is asking, and what applies to them.

A principal carries a role; a tenant carries policy; a scope may narrow it further.
EffectiveMemoryPolicy is the resolved answer after all three compose, and it is what
the engine actually reads -- nothing downstream re-derives precedence."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from memotron.config._dream_config import (
    DreamConfig,
    DreamJob,
    _dedupe_prompt_profiles,
)
from memotron.config._governance import (
    GovernancePolicy,
)
from memotron.config._instructions import (
    DreamAgentConfig,
    DreamPromptOverride,
    DreamPromptProfile,
    _normalize_non_blank_text,
)
from memotron.config._motive import (
    DreamMode,
    MemoryBank,
    Motive,
    PromptPack,
    default_dream_modes,
)
from memotron.config._policies import (
    DedupPolicy,
    PruningPolicy,
)
from memotron.config._retrieval import (
    ProfilePolicy,
)
from memotron.models import (
    DreamJobKind,
    MemoryScope,
)
from memotron.models import (
    RelationshipCardinality as RelationshipCardinality,
)


class ScopeMemoryPolicy(BaseModel):
    """Controls the resolved policy for one registered memory scope."""

    scope: MemoryScope
    dream_mode: str | None = None
    prompt_pack: str | None = None
    motive: str | None = None
    governance: GovernancePolicy | None = None
    profile: ProfilePolicy | None = None
    dedup: DedupPolicy | None = None
    pruning: PruningPolicy | None = None
    read_only: bool | None = None

    @field_validator("dream_mode", "prompt_pack", "motive")
    @classmethod
    def normalize_optional_scope_ref(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, f"scope policy {info.field_name}")


class AgentMemoryPolicy(BaseModel):
    """Controls defaults for one application agent inside a tenant."""

    agent: DreamAgentConfig
    default_scope: MemoryScope | None = None
    dream_mode: str | None = None
    prompt_pack: str | None = None
    motive: str | None = None
    governance: GovernancePolicy | None = None
    profile: ProfilePolicy | None = None
    dedup: DedupPolicy | None = None
    pruning: PruningPolicy | None = None
    read_only_scope_keys: set[str] = Field(default_factory=set)

    @property
    def agent_id(self) -> str:
        return self.agent.agent_id

    @field_validator("dream_mode", "prompt_pack", "motive")
    @classmethod
    def normalize_optional_agent_ref(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, f"agent policy {info.field_name}")

    @field_validator("read_only_scope_keys")
    @classmethod
    def normalize_read_only_scope_keys(cls, value: set[str]) -> set[str]:
        return {_normalize_non_blank_text(scope_key, "read_only_scope_key") for scope_key in value}


class PrincipalRole(StrEnum):
    USER = "user"
    OPERATOR = "operator"
    ADMIN = "admin"


class MemoryPrincipal(BaseModel):
    """Authenticated actor used to authorize tenant/scope policy resolution."""

    principal_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    agent_id: str | None = None
    default_scope: MemoryScope | None = None
    allowed_scope_keys: set[str] = Field(default_factory=set)
    role: PrincipalRole = PrincipalRole.USER

    @field_validator("principal_id", "tenant_id")
    @classmethod
    def normalize_principal_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"principal {info.field_name}")

    @field_validator("agent_id")
    @classmethod
    def normalize_optional_agent_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, "principal agent_id")

    @field_validator("allowed_scope_keys")
    @classmethod
    def normalize_allowed_scope_keys(cls, value: set[str]) -> set[str]:
        return {_normalize_non_blank_text(scope_key, "allowed_scope_key") for scope_key in value}

    def effective_allowed_scope_keys(self) -> set[str]:
        keys = set(self.allowed_scope_keys)
        if self.default_scope is not None:
            keys.add(self.default_scope.key)
        return keys


def principal_from_registry_row(row: dict[str, Any]) -> MemoryPrincipal:
    """Turn a `key_principals` row into the principal an authorization decision reads.

    The join between DW-030's per-key registry and this model. `principal_for_key_alias`
    answers *which principal does this gateway `key_alias` belong to* and returns storage's
    dict; every surface that authenticates a caller needs a `MemoryPrincipal`, and until
    this existed there was no supported way across (#126 / #137).

    **Every failure raises. Nothing is defaulted.** That is the whole design of this
    function, and it is not defensiveness for its own sake:

    * A **role** that does not parse must not fall back to ``USER``. It would look like a
      successful downgrade and it is really an unreadable record -- and the opposite
      mistake, silently reading as something permissive, is worse.
    * A **`default_scope_key`** that does not parse must not become ``None``. On this path
      an absent default scope means *one fewer entry in the allowlist*
      (`effective_allowed_scope_keys` adds it), so degrading widens access rather than
      denying it. A caller that fails closed needs the exception.

    The caller decides what a raise means for the request -- almost certainly 401/403 --
    but it must be a decision, not a value that quietly reads as authorised.

    Blank/absent ``agent_id`` and ``default_scope_key`` are legitimately optional and map
    to ``None``; that is different from *present and unparseable*, which raises.
    """
    default_scope_key = (row.get("default_scope_key") or "").strip()
    agent_id = (row.get("agent_id") or "").strip()
    return MemoryPrincipal(
        principal_id=row["principal_id"],
        tenant_id=row["tenant_id"],
        agent_id=agent_id or None,
        role=PrincipalRole(row.get("role") or PrincipalRole.USER.value),
        default_scope=MemoryScope.from_key(default_scope_key) if default_scope_key else None,
        allowed_scope_keys=set(row.get("allowed_scope_keys") or ()),
    )


class TenantMemoryPolicy(BaseModel):
    """Top-level tenant control plane entry.

    All memory scopes that may be used by the tenant must be explicitly
    registered in ``scopes`` or selected as a tenant/agent default scope.
    """

    tenant_id: str = Field(min_length=1)
    name: str | None = None
    default_agent_id: str | None = None
    default_scope: MemoryScope | None = None
    default_dream_mode: str = "balanced"
    default_prompt_pack: str | None = None
    default_motive: str | None = None
    memory_bank: MemoryBank | None = None
    prompt_profiles: tuple[DreamPromptProfile, ...] = ()
    prompt_packs: tuple[PromptPack, ...] = ()
    dream_modes: tuple[DreamMode, ...] = Field(default_factory=default_dream_modes)
    agents: tuple[AgentMemoryPolicy, ...] = ()
    scopes: tuple[ScopeMemoryPolicy, ...] = ()
    governance: GovernancePolicy | None = None
    profile: ProfilePolicy | None = None
    dedup: DedupPolicy | None = None
    pruning: PruningPolicy | None = None
    read_only_scope_keys: set[str] = Field(default_factory=set)
    require_dream_agent_approval_for_untrusted_directives: bool | None = None

    @field_validator("tenant_id")
    @classmethod
    def normalize_tenant_id(cls, value: str) -> str:
        return _normalize_non_blank_text(value, "tenant_id")

    @field_validator("name")
    @classmethod
    def normalize_optional_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, "tenant name")

    @field_validator("default_agent_id", "default_dream_mode", "default_prompt_pack", "default_motive")
    @classmethod
    def normalize_optional_tenant_ref(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, f"tenant policy {info.field_name}")

    @field_validator("prompt_profiles", mode="before")
    @classmethod
    def normalize_prompt_profiles(cls, value: Any) -> tuple[DreamPromptProfile, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(DreamPromptProfile.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("prompt_profiles must be a list or tuple of DreamPromptProfile objects")

    @field_validator("prompt_packs", mode="before")
    @classmethod
    def normalize_prompt_packs(cls, value: Any) -> tuple[PromptPack, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(PromptPack.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("prompt_packs must be a list or tuple of PromptPack objects")

    @field_validator("dream_modes", mode="before")
    @classmethod
    def normalize_dream_modes(cls, value: Any) -> tuple[DreamMode, ...]:
        if value is None:
            return default_dream_modes()
        if isinstance(value, (list, tuple)):
            return tuple(DreamMode.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("dream_modes must be a list or tuple of DreamMode objects")

    @field_validator("agents", mode="before")
    @classmethod
    def normalize_agents(cls, value: Any) -> tuple[AgentMemoryPolicy, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(AgentMemoryPolicy.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("agents must be a list or tuple of AgentMemoryPolicy objects")

    @field_validator("scopes", mode="before")
    @classmethod
    def normalize_scopes(cls, value: Any) -> tuple[ScopeMemoryPolicy, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(ScopeMemoryPolicy.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("scopes must be a list or tuple of ScopeMemoryPolicy objects")

    @field_validator("read_only_scope_keys")
    @classmethod
    def normalize_tenant_read_only_scope_keys(cls, value: set[str]) -> set[str]:
        return {_normalize_non_blank_text(scope_key, "read_only_scope_key") for scope_key in value}

    @model_validator(mode="after")
    def validate_tenant_policy(self) -> TenantMemoryPolicy:
        self._validate_unique(
            "agent ids",
            [agent.agent_id.casefold() for agent in self.agents],
        )
        self._validate_unique(
            "agent names",
            [agent.agent.name.casefold() for agent in self.agents],
        )
        # DELIBERATELY case-SENSITIVE, unlike the agent ids and names above. Casefolding
        # here was tried and reverted, and the reasons are worth keeping because the
        # "obvious" change is to put it back.
        #
        # `normalize_key` casefolds `truth_key`, so two scopes differing only by case once
        # shared a truth slot and destroyed each other. The three truth reads take a required
        # `scope_key` now, so `tenant:Acme` and `tenant:acme` SHARE NO TRUTH SLOT -- pinned by
        # `test_two_scopes_differing_only_by_CASE_do_not_share_a_truth_slot`. That is a claim
        # about the TRUTH plane only; see the node-plane caveat below.
        #
        # Casefolding here is REACHABLE and breaking: `agent_memory/_registry.py:163` dedups
        # the scope list case-SENSITIVELY and then constructs this policy, so a case-differing
        # pair appends and raises at agent-registration time where it previously succeeded.
        # Making that dedup casefold instead would be worse -- it would silently merge two
        # distinct scopes.
        #
        # And it never covered the case that justified it: this validator sees ONE tenant's
        # scope list, while two TENANTS named `Acme` and `acme` are separate policies whose
        # ids are compared case-SENSITIVELY in `_control_plane.py`'s `validate_control_plane`.
        #
        # NODE-PLANE CAVEAT, measured: `node_identity_key` composes `scope_key:label:name` and
        # `upsert_node` casefolds it into the globally-UNIQUE `graph_key`, so case-differing
        # scopes DO still share entity nodes and the second write overwrites the first's
        # `scope_key`. Pre-existing and unchanged by this branch, and casefolding here would
        # not have prevented it (the guard gates policy construction, not node writes).
        self._validate_unique("scope keys", [scope.scope.key for scope in self.scopes])
        self._validate_unique("prompt pack names", [pack.name for pack in self.prompt_packs])
        self._validate_unique("dream mode names", [mode.name for mode in self.dream_modes])
        self._validate_unique("prompt profile keys", [profile.key for profile in self.prompt_profiles])
        known_modes = {mode.name for mode in self.dream_modes}
        known_packs = {pack.name for pack in self.prompt_packs}
        known_agents = {agent.agent_id for agent in self.agents}
        known_scopes = {scope.scope.key for scope in self.scopes}
        if self.default_dream_mode not in known_modes:
            raise ValueError(f"tenant default_dream_mode {self.default_dream_mode!r} is not a known dream mode")
        if self.default_prompt_pack is not None and self.default_prompt_pack not in known_packs:
            raise ValueError(f"tenant default_prompt_pack {self.default_prompt_pack!r} is not a known prompt pack")
        if self.default_agent_id is not None and self.default_agent_id not in known_agents:
            raise ValueError(f"tenant default_agent_id {self.default_agent_id!r} is not a known agent")
        if self.default_scope is not None:
            known_scopes.add(self.default_scope.key)
        for agent in self.agents:
            if agent.default_scope is not None:
                known_scopes.add(agent.default_scope.key)
            self._validate_optional_ref(agent.dream_mode, known_modes, "agent dream_mode")
            self._validate_optional_ref(agent.prompt_pack, known_packs, "agent prompt_pack")
        for scope in self.scopes:
            self._validate_optional_ref(scope.dream_mode, known_modes, "scope dream_mode")
            self._validate_optional_ref(scope.prompt_pack, known_packs, "scope prompt_pack")
        for mode in self.dream_modes:
            self._validate_optional_ref(mode.prompt_pack, known_packs, "dream mode prompt_pack")
        return self

    def agent_policy(self, agent_id: str) -> AgentMemoryPolicy:
        for agent in self.agents:
            if agent.agent_id == agent_id:
                return agent
        raise ValueError(f"unknown agent_id {agent_id!r} for tenant {self.tenant_id!r}")

    def scope_policy(self, scope: MemoryScope) -> ScopeMemoryPolicy | None:
        for policy in self.scopes:
            if policy.scope == scope:
                return policy
        return None

    def dream_mode(self, name: str) -> DreamMode:
        for mode in self.dream_modes:
            if mode.name == name:
                return mode
        raise ValueError(f"unknown dream mode {name!r} for tenant {self.tenant_id!r}")

    def prompt_pack(self, name: str) -> PromptPack:
        for pack in self.prompt_packs:
            if pack.name == name:
                return pack
        raise ValueError(f"unknown prompt pack {name!r} for tenant {self.tenant_id!r}")

    def registered_scope_keys(self) -> set[str]:
        keys = {scope.scope.key for scope in self.scopes}
        if self.default_scope is not None:
            keys.add(self.default_scope.key)
        for agent in self.agents:
            if agent.default_scope is not None:
                keys.add(agent.default_scope.key)
        return keys

    @staticmethod
    def _validate_unique(label: str, values: list[str]) -> None:
        if len(values) != len(set(values)):
            raise ValueError(f"tenant policy {label} must be unique")

    @staticmethod
    def _validate_optional_ref(value: str | None, known: set[str], label: str) -> None:
        if value is not None and value not in known:
            raise ValueError(f"{label} {value!r} is not known")


class EffectiveMemoryPolicy(BaseModel):
    """Resolved, auditable policy snapshot for one tenant/agent/scope request."""

    model_config = {"frozen": True}

    tenant_id: str
    agent_id: str
    scope: MemoryScope
    agent: DreamAgentConfig
    dream_mode: DreamMode
    enabled_job_kinds: tuple[DreamJobKind, ...]
    prompt_pack: PromptPack | None = None
    prompt_profile: str | None = None
    prompt_profile_version: str | None = None
    prompt_override: DreamPromptOverride = Field(default_factory=DreamPromptOverride)
    motive_name: str | None = None
    motive: Motive | None = None
    memory_bank: MemoryBank | None = None
    governance: GovernancePolicy | None = None
    profile: ProfilePolicy
    dedup: DedupPolicy
    pruning: PruningPolicy
    read_only: bool = False
    require_dream_agent_approval_for_untrusted_directives: bool = False
    prompt_profiles: tuple[DreamPromptProfile, ...] = ()
    source_trace: dict[str, str] = Field(default_factory=dict)
    policy_alias: str | None = None
    policy_contract_digest: str | None = None
    certification_verdict: str = "uncertified"

    def apply_to_job(self, job: DreamJob) -> DreamJob | None:
        if job.kind not in self.enabled_job_kinds:
            return None
        updates: dict[str, Any] = {
            "name": self.scoped_job_name(job.name),
            "agent": self.agent,
            "scope": self.scope,
        }
        if self.dream_mode.max_items_per_run is not None:
            updates["max_items_per_run"] = self.dream_mode.max_items_per_run
        if self.prompt_pack is not None:
            updates["prompt_profile"] = self.prompt_pack.prompt_profile
            updates["prompt_profile_version"] = self.prompt_pack.prompt_profile_version
            updates["prompt_override"] = self.prompt_pack.prompt_override
        if self.motive_name is not None:
            updates["motive"] = self.motive_name
        if self.dream_mode.rollup_consolidation is not None:
            updates["rollup_consolidation"] = self.dream_mode.rollup_consolidation
        if self.dream_mode.rollup_consolidation_policy is not None:
            updates["rollup_consolidation_policy"] = self.dream_mode.rollup_consolidation_policy
        return job.model_copy(update=updates)

    def enabled_jobs(self, base_config: DreamConfig) -> tuple[DreamJob, ...]:
        jobs: list[DreamJob] = []
        for job in base_config.jobs:
            scoped = self.apply_to_job(job)
            if scoped is not None:
                jobs.append(scoped)
        return tuple(jobs)

    def to_dream_config(self, base_config: DreamConfig) -> DreamConfig:
        jobs = self.enabled_jobs(base_config)
        if not jobs:
            raise ValueError(f"dream mode {self.dream_mode.name!r} enables no dream jobs")
        read_only_scopes = set(base_config.read_only_scopes)
        if self.read_only:
            read_only_scopes.add(self.scope.key)
        prompt_profiles = _dedupe_prompt_profiles((*base_config.prompt_profiles, *self.prompt_profiles))
        return base_config.model_copy(
            update={
                "prompt_profiles": prompt_profiles,
                "jobs": jobs,
                "profile": self.profile,
                "dedup": self.dedup,
                "pruning": self.pruning,
                "memory_bank": self.memory_bank,
                "governance": self.governance,
                "read_only_scopes": read_only_scopes,
                "require_dream_agent_approval_for_untrusted_directives": (
                    self.require_dream_agent_approval_for_untrusted_directives
                ),
                "runtime_policy_source_trace": self.source_trace,
                "runtime_policy_alias": self.policy_alias,
                "runtime_policy_contract_digest": self.policy_contract_digest,
                "runtime_certification_verdict": self.certification_verdict,
            }
        )

    def scoped_job_name(self, job_name: str) -> str:
        return f"{job_name} [{self.tenant_id}/{self.agent_id}/{self.scope.key}/{self.dream_mode.name}]"
