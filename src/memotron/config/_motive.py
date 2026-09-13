"""Motive, and the contracts and packs a scope is configured with.

A Motive is why a scope keeps memory at all -- it decides which memory types are
admissible, so it is the first gate a candidate meets. FormationContract is the
attestable record of the policy inputs a formation run used; MemoryBank, PromptPack
and DreamMode are the reusable bundles an operator composes from."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from memotron.config._actionability import (
    ActionabilityPolicy,
)
from memotron.config._governance import (
    GovernancePolicy,
)
from memotron.config._instructions import (
    DreamPromptOverride,
    DreamPromptProfile,
    SalienceRubric,
    _normalize_non_blank_text,
    _normalize_prompt_lines,
)
from memotron.config._policies import (
    DedupPolicy,
    EntityResolutionPolicy,
    MemoryHealthPolicy,
    PruningPolicy,
    RetentionPolicy,
    RollupConsolidationPolicy,
)
from memotron.config._retrieval import (
    ProfilePolicy,
)
from memotron.models import (
    DreamJobKind,
    MemoryType,
)


class Motive(BaseModel):
    """WS-3: Named, selectable driver of memory-making.

    A Motive bundles formation intent into one object so callers can select
    a named policy instead of configuring individual knobs.  Motives are
    ALWAYS opt-in — a job or episode with no motive runs exactly as before.

    Fields
    ------
    name
        Unique, non-blank key used to look up the Motive in a MemoryBank.
    goal
        Human-readable description of what this Motive is trying to remember.
    allowed_memory_types
        When non-empty, only extracted memories whose resolved memory_type is
        in this tuple are materialized.  Empty = allow all types (no-op filter).
    prompt_profile / prompt_profile_version
        When set, override the job's prompt profile selection.  Translated
        through the existing DreamPromptProfile.with_override() machinery.
    prompt_override
        Job-local prompt guidance layered on top of the resolved profile, via
        the existing with_override() path.  None = no additional override.
    salience_rubric
        When set, overrides the rubric precedence chain for this Motive:
        Motive rubric > job rubric > instruction-set rubric > defaults.
    dedup_threshold
        When set, overrides DedupPolicy.cosine_threshold for this Motive's
        formation runs.  None = use the DedupPolicy global.
    retrieval_budget_share
        WS-5 budget-aware typed retrieval.  Drives two read-side levers:
        profile() allocates this share of the token budget to the Motive's
        allowed_memory_types, and search_context() boosts those types by
        ``1 + retrieval_budget_share`` at rerank.
    governance
        WS-7 per-Motive governance policy.  When set, overrides the config-level
        DreamConfig.governance for formation runs driven by this Motive.  None (default)
        = use the config-level policy (or no governance if that is also None).
    system_prompt_override
        WS-21: when set, replaces the instruction set's ``system_prompt`` as the
        system message sent to the LLM extraction transport for episodes this
        Motive governs — so a Motive can alter the temporal-extraction contract
        and confidence-calibration guidance, not only the user-message prompt.
        None (default) = the instruction-set system prompt, byte-for-byte
        today's behaviour.  Pinned into the FormationContract digest (the
        contract freezes the full Motive dump) and into
        ``motive_version_digest``.
    """

    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    allowed_memory_types: tuple[MemoryType, ...] = ()
    prompt_profile: str | None = None
    prompt_profile_version: str | None = None
    prompt_override: DreamPromptOverride | None = None
    system_prompt_override: str | None = None
    """WS-21: optional replacement for the instruction-set extraction system
    prompt.  None = instruction-set system prompt (legacy behaviour)."""
    salience_rubric: SalienceRubric | None = None
    dedup_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_budget_share: float | None = Field(default=None, ge=0.0, le=1.0)
    """WS-5: share of profile() token budget reserved for allowed_memory_types,
    and the rerank boost (``1 + share``) those types get in search_context()."""
    governance: GovernancePolicy | None = None
    """WS-7: Optional per-Motive governance policy.

    When set, overrides the config-level ``DreamConfig.governance`` for memory
    formation runs driven by this Motive.  Resolution order:
        Motive.governance > DreamConfig.governance > None (legacy behaviour).

    None (default) = use the config-level policy (or no policy if that too is None).
    """
    retention: RetentionPolicy | None = None
    """Optional lifecycle retention override for memories governed by this Motive."""
    actionability: ActionabilityPolicy | None = None
    """WS-24: per-Motive override of the actionability gate.

    Resolution order mirrors every other formation lever:
        Motive.actionability > DreamConfig.actionability.
    A Motive whose whole purpose is exhaustive capture (a compliance archive,
    a schema catalogue) can loosen or disable the gate without loosening it for
    the tenant's other Motives.  ``None`` (default) uses the config policy."""
    health: MemoryHealthPolicy | None = None
    """WS-24: per-Motive override of the distribution health gates.

    A Motive with a deliberately narrow ``allowed_memory_types`` will
    legitimately concentrate share, so it needs its own thresholds rather than
    a tenant-wide loosening.  ``None`` (default) uses the config policy."""

    @field_validator("name", "goal")
    @classmethod
    def normalize_motive_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"motive {info.field_name}")

    @field_validator("system_prompt_override")
    @classmethod
    def normalize_system_prompt_override(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError(
                "motive system_prompt_override cannot be blank; use None to keep the instruction-set system prompt"
            )
        return normalized

    @field_validator("allowed_memory_types", mode="before")
    @classmethod
    def normalize_allowed_memory_types(cls, value: Any) -> tuple[MemoryType, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(dict.fromkeys(MemoryType(v) if isinstance(v, str) else v for v in value))
        raise ValueError("allowed_memory_types must be a list or tuple of MemoryType values")


class FormationContract(BaseModel):
    """Immutable, fully resolved formation policy for one episode.

    The contract is deliberately data, not prompt prose: it is what receipts
    bind, deterministic gates consume, and certification attests.  Its digest
    is computed with the receipt canonicalizer at the write boundary.
    """

    model_config = {"frozen": True}

    schema_version: int = 1
    scope_key: str = Field(min_length=1)
    instruction_set: dict[str, Any]
    formation_profile: DreamPromptProfile
    motive: Motive | None = None
    governance: GovernancePolicy | None = None
    dedup: DedupPolicy
    entity_resolution: EntityResolutionPolicy = Field(default_factory=EntityResolutionPolicy)
    """WS-17 T16b: the entity-resolution policy governing this episode's
    formation — pinned into the contract digest exactly like ``dedup`` so a
    changed alias threshold/weighting is a changed contract."""
    actionability: ActionabilityPolicy = Field(default_factory=ActionabilityPolicy)
    """WS-24: the actionability gate governing this episode.  Pinned like
    ``dedup`` because it decides which candidates became memories at all — a
    counterfactual re-gate that could not see the gate's own thresholds would
    be replaying a different formation."""
    source_trace: dict[str, str] = Field(default_factory=dict)
    model_identifier: str | None = None
    extractor_identifier: str | None = None
    embedding_identifier: str | None = None


class ConsolidationSynthesisProfile(BaseModel):
    """A separate contract for derived-rollup synthesis, never extraction."""

    name: str = Field(default="rollup-synthesis", min_length=1)
    version: str = Field(default="v1", min_length=1)
    objective: str = Field(
        default="Compress cited graph evidence without introducing facts or normative authority.",
        min_length=1,
    )
    rules: tuple[str, ...] = (
        "Cite every source relationship.",
        "Preserve temporal bounds and uncertainty.",
        "Do not resolve contradictory children into a false consensus.",
    )
    model_identifier: str | None = None
    """Optional OVERRIDE of the provenance string recorded for LLM rollup synthesis.

    It is NOT the on-switch.  Configuring a ``SynthesisTransport``
    (``Memotron(rollup_synthesis_transport=...)``, or one resolved by the
    runtime from the sealed tenant credential / environment) is what turns LLM
    rollup synthesis on; left ``None`` this field inherits that transport's own
    ``identifier``.  Set it only to pin a different provenance string than the
    transport reports.

    (WS-18 made this a second, independent opt-in and nothing ever set it, so
    the feature could not run on any packaged config — a configured model seat
    that silently never fires.  WS-24 demoted it to an override.)

    With no transport configured the value is irrelevant: rollup text is the
    deterministic structural label and the row records
    ``rollup_text_source="fallback_no_transport"``.
    """

    @field_validator("name", "version", "objective")
    @classmethod
    def normalize_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"consolidation profile {info.field_name}")

    @field_validator("rules", mode="before")
    @classmethod
    def normalize_rules(cls, value: Any) -> tuple[str, ...]:
        return _normalize_prompt_lines(value, "consolidation profile rules")

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"


class MemoryBank(BaseModel):
    """WS-3: Ordered catalog of Motives available to a tenant or agent.

    The Memory Bank is the product surface that lets callers select a named
    Motive instead of configuring individual formation knobs.  Motive names
    within a bank must be unique.

    Usage
    -----
    bank = MemoryBank(motives=[...])
    motive = bank.motive("learn-compliance-requirements")

    Unknown names fail fast with a clear ValueError (mirrors run_dream_job
    unknown-job behavior and the unknown-instruction-set / unknown-prompt-profile
    checks in DreamConfig.validate_config).
    """

    motives: tuple[Motive, ...] = ()

    @field_validator("motives", mode="before")
    @classmethod
    def normalize_motives(cls, value: Any) -> tuple[Motive, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(Motive.model_validate(m) if isinstance(m, dict) else m for m in value)
        raise ValueError("motives must be a list or tuple of Motive objects")

    @model_validator(mode="after")
    def validate_unique_names(self) -> MemoryBank:
        names = [m.name for m in self.motives]
        if len(names) != len(set(names)):
            raise ValueError("MemoryBank motive names must be unique")
        return self

    def motive(self, name: str) -> Motive:
        """Look up a Motive by name.  Fails fast on unknown names."""
        for m in self.motives:
            if m.name == name:
                return m
        known = [m.name for m in self.motives]
        raise ValueError(f"unknown motive: {name!r}. Known motives in this bank: {known}")

    @property
    def names(self) -> list[str]:
        return [m.name for m in self.motives]


class PromptPack(BaseModel):
    """Named prompt selection that can be assigned at tenant, agent, or scope level."""

    name: str = Field(min_length=1)
    prompt_profile: str = Field(min_length=1)
    prompt_profile_version: str = Field(default="v1", min_length=1)
    prompt_override: DreamPromptOverride = Field(default_factory=DreamPromptOverride)

    @field_validator("name", "prompt_profile", "prompt_profile_version")
    @classmethod
    def normalize_prompt_pack_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"prompt pack {info.field_name}")


class DreamMode(BaseModel):
    """Operational mode for offline dreaming.

    Motives describe what should be remembered.  A DreamMode describes how the
    offline engine is allowed to mutate memory for a resolved tenant/agent/scope
    context: which job kinds can run, whether extra review is required, and which
    coarse-grained policy objects should override the global defaults.
    """

    name: str = Field(min_length=1)
    description: str = ""
    enabled_job_kinds: tuple[DreamJobKind, ...] = (
        DreamJobKind.FORMATION,
        DreamJobKind.CONSOLIDATION,
        DreamJobKind.PRUNING,
    )
    prompt_pack: str | None = None
    motive: str | None = None
    max_items_per_run: int | None = Field(default=None, gt=0)
    dedup: DedupPolicy | None = None
    pruning: PruningPolicy | None = None
    profile: ProfilePolicy | None = None
    governance: GovernancePolicy | None = None
    require_dream_agent_approval_for_untrusted_directives: bool | None = None
    rollup_consolidation: bool | None = None
    rollup_consolidation_policy: RollupConsolidationPolicy | None = None

    @field_validator("name")
    @classmethod
    def normalize_mode_name(cls, value: str) -> str:
        return _normalize_non_blank_text(value, "dream mode name")

    @field_validator("prompt_pack", "motive")
    @classmethod
    def normalize_optional_mode_ref(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, f"dream mode {info.field_name}")

    @field_validator("description")
    @classmethod
    def normalize_description(cls, value: str) -> str:
        return value.strip()

    @field_validator("enabled_job_kinds", mode="before")
    @classmethod
    def normalize_enabled_job_kinds(cls, value: Any) -> tuple[DreamJobKind, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(dict.fromkeys(DreamJobKind(v) if isinstance(v, str) else v for v in value))
        raise ValueError("enabled_job_kinds must be a list or tuple of DreamJobKind values")


def default_dream_modes() -> tuple[DreamMode, ...]:
    return (
        DreamMode(
            name="observe_only",
            description="Ingest episodes and allow read-side inspection, but run no mutating dream jobs.",
            enabled_job_kinds=(),
        ),
        DreamMode(
            name="review_required",
            description="Run formation with the strictest untrusted directive gate enabled.",
            enabled_job_kinds=(DreamJobKind.FORMATION,),
            require_dream_agent_approval_for_untrusted_directives=True,
        ),
        DreamMode(
            name="formation_only",
            description="Create new memory from queued episodes without consolidation or pruning.",
            enabled_job_kinds=(DreamJobKind.FORMATION,),
        ),
        DreamMode(
            name="balanced",
            description="Run the standard formation, consolidation, and pruning maintenance loop.",
            enabled_job_kinds=(
                DreamJobKind.FORMATION,
                DreamJobKind.CONSOLIDATION,
                DreamJobKind.PRUNING,
            ),
        ),
        DreamMode(
            name="aggressive_cleanup",
            description="Run the full loop with stronger semantic compaction and rollup consolidation.",
            enabled_job_kinds=(
                DreamJobKind.FORMATION,
                DreamJobKind.CONSOLIDATION,
                DreamJobKind.PRUNING,
            ),
            dedup=DedupPolicy(cosine_threshold=0.82, memory_type_thresholds={"directive": 0.76}),
            rollup_consolidation=True,
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.70,
                min_cluster_size=2,
                max_depth=2,
            ),
        ),
        DreamMode(
            name="audit",
            description="Resolve policy for inspection only; no mutating dream jobs are enabled.",
            enabled_job_kinds=(),
        ),
    )
