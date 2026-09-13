"""The aggregate: a dream job, the whole configuration, and the default.

DreamConfig is layer 2 of this module's dependency graph -- it composes twelve of
the policy types and is what every engine entry point is handed. default_config()
is a pure factory returning a fresh instance; there is deliberately no module-level
singleton anywhere in this package."""

from __future__ import annotations

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from memotron.config._actionability import (
    ActionabilityPolicy,
)
from memotron.config._governance import (
    GovernancePolicy,
)
from memotron.config._instructions import (
    DreamAgentConfig,
    DreamInstructionSet,
    DreamPromptOverride,
    DreamPromptProfile,
    NodeInstruction,
    RelationshipInstruction,
    SalienceRubric,
    _normalize_non_blank_text,
)
from memotron.config._motive import (
    ConsolidationSynthesisProfile,
    MemoryBank,
    Motive,
)
from memotron.config._policies import (
    CoherencePolicy,
    ConfidencePolicy,
    DedupPolicy,
    DreamContextPolicy,
    DreamEpisodeFilter,
    EntityResolutionPolicy,
    MemoryHealthPolicy,
    PredicateCanonicalizationPolicy,
    PruningPolicy,
    RollupConsolidationPolicy,
    SupersessionPolicy,
)
from memotron.config._prompts import (
    default_prompt_profiles,
)
from memotron.config._retrieval import (
    ProfilePolicy,
    RetrievalPolicy,
)
from memotron.config._storage_env import (
    storage_settings_from_env,
)
from memotron.config._storage_settings import StorageSettings
from memotron.models import (
    DreamJobKind,
    MemoryScope,
    RelationshipCardinality,
)


def _dedupe_prompt_profiles(profiles: tuple[DreamPromptProfile, ...]) -> tuple[DreamPromptProfile, ...]:
    by_key: dict[str, DreamPromptProfile] = {}
    for profile in profiles:
        by_key[profile.key] = profile
    return tuple(by_key[key] for key in sorted(by_key))


class DreamJob(BaseModel):
    name: str = Field(min_length=1)
    kind: DreamJobKind
    cadence_seconds: int = Field(gt=0)
    max_items_per_run: int = Field(default=1000, gt=0)
    scope: MemoryScope | None = None
    agent: DreamAgentConfig = Field(default_factory=DreamAgentConfig)
    instruction_set: str = "default"
    prompt_profile: str = "support-memory"
    prompt_profile_version: str = "v1"
    prompt_override: DreamPromptOverride = Field(default_factory=DreamPromptOverride)
    consolidation_profile: str = "rollup-synthesis"
    consolidation_profile_version: str = "v1"
    episode_filter: DreamEpisodeFilter = Field(default_factory=DreamEpisodeFilter)
    context_policy: DreamContextPolicy = Field(default_factory=DreamContextPolicy)
    salience_rubric: SalienceRubric | None = None
    """WS-2: Optional per-job salience rubric override.

    When set, takes precedence over the instruction set's rubric for this job.
    None means "use the instruction set's rubric" (which defaults to no-op).
    WS-3 Motives will populate this field per persona intent at job-construction time.
    """
    motive: str | None = None
    """WS-3: Optional Motive name pinned to this job.

    When set, every episode processed by this job resolves the named Motive from
    the DreamConfig's MemoryBank.  The Motive drives prompt, rubric, dedup
    threshold, and allowed_memory_types for that run.

    Resolution precedence (per episode):
        job.motive > episode metadata["motive"] > none (legacy behaviour)

    The name is validated at DreamConfig construction time against the bank when
    a bank is present (mirrors the instruction-set and prompt-profile checks).
    """
    rollup_consolidation: bool = True
    """Compatibility field for the canonical rollup consolidation reducer.

    Consolidation is always rollup.  A false value is rejected rather than
    silently enabling the retired flat peer-producing path.
    """
    rollup_consolidation_policy: RollupConsolidationPolicy = Field(default_factory=RollupConsolidationPolicy)
    """WS-4: Clustering and recursion parameters for rollup consolidation.

    Used by every consolidation job.
    """
    coherence_policy: CoherencePolicy = Field(default_factory=CoherencePolicy)
    """WS-10: Cross-artifact coherence parameters.

    Only used by COHERENCE-kind jobs (and ``run_coherence_scan``).  Safe to
    ignore for all other job kinds — no existing behaviour changes when unused.
    """

    @field_validator(
        "prompt_profile",
        "prompt_profile_version",
        "consolidation_profile",
        "consolidation_profile_version",
    )
    @classmethod
    def normalize_prompt_ref(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"dream job {info.field_name}")

    @model_validator(mode="after")
    def require_canonical_consolidation(self) -> DreamJob:
        if self.kind == DreamJobKind.CONSOLIDATION and not self.rollup_consolidation:
            raise ValueError(
                "consolidation jobs require rollup_consolidation=True; "
                "flat peer-producing consolidation has been retired"
            )
        return self


class DreamConfig(BaseModel):
    instruction_sets: tuple[DreamInstructionSet, ...]
    prompt_profiles: tuple[DreamPromptProfile, ...] = Field(default_factory=default_prompt_profiles)
    consolidation_profiles: tuple[ConsolidationSynthesisProfile, ...] = Field(
        default_factory=lambda: (ConsolidationSynthesisProfile(),)
    )
    jobs: tuple[DreamJob, ...]
    pruning: PruningPolicy = Field(default_factory=PruningPolicy)
    profile: ProfilePolicy = Field(default_factory=ProfilePolicy)
    retrieval: RetrievalPolicy = Field(default_factory=RetrievalPolicy)
    """WS-5: Policy for the six-stage retrieval pipeline (search_context)."""
    dedup: DedupPolicy = Field(default_factory=DedupPolicy)
    supersession: SupersessionPolicy = Field(default_factory=SupersessionPolicy)
    confidence: ConfidencePolicy = Field(default_factory=ConfidencePolicy)
    """WS-16 T13: truth-plane confidence policy — bounded evidence accumulation
    on reinforcement, dispute discounting on gate-parked challengers, and the
    read-only pruning decay knob.  Defaults preserve bounded-accumulation
    semantics with no decay."""
    predicate_canonicalization: PredicateCanonicalizationPolicy = Field(default_factory=PredicateCanonicalizationPolicy)
    """WS-17 T16: per-scope canonical predicate registry — truth keys are built
    on the canonical predicate so paraphrased corrections supersede instead of
    coexisting.  ``enabled=False`` restores byte-identical legacy behavior."""
    actionability: ActionabilityPolicy = Field(default_factory=ActionabilityPolicy)
    """WS-24: the actionability gate — every candidate must name the step an
    agent skips by knowing it, and field-inventory entries are quarantined
    rather than materialized.  Default-ON; ``enabled=False`` restores
    byte-identical pre-gate behaviour."""
    health: MemoryHealthPolicy = Field(default_factory=MemoryHealthPolicy)
    """WS-24: distribution health gates evaluated after every formation run —
    max type share, normalized type entropy, abstain rate, and dead types.  A
    blocking trip fails the run loudly instead of silently producing a
    degenerate graph."""
    entity_resolution: EntityResolutionPolicy = Field(default_factory=EntityResolutionPolicy)
    """WS-17 T16b: per-scope semantic entity resolution — formation resolves
    subject/object names through the alias registry BEFORE node upsert and
    truth-key computation, so surface-form variants of one entity stop
    fragmenting nodes and truth slots.  ``enabled=False`` restores
    byte-identical legacy behavior."""
    storage: StorageSettings | None = None
    """Storage backend selection (see the StorageBackend contract in
    ``memotron.storage``).

    None (default) = one local SQLite backend at the client's ``graph_path``
    (no credentials, no network — what the test suite and simulation use).
    Set a single engine, or configure ``operational_store`` and
    ``memory_graph`` separately to run the two stores on different engines at
    once.
    """
    memory_bank: MemoryBank | None = None
    """WS-3: Optional catalog of Motives available to jobs in this config.

    When set, job.motive names are validated against the bank at construction
    time.  When None, no Motive resolution runs (legacy behaviour unchanged).
    """
    governance: GovernancePolicy | None = None
    """WS-7: Optional global governance policy applied to all memory formation.

    Provides config-level defaults for PII sensitivity, erasure behavior, audit
    verbosity, and retention TTL.  Per-Motive governance (Motive.governance) takes
    precedence over this when both are set.

    None (default) = no governance; all existing behaviour unchanged (opt-in invariant).
    """
    read_only_scopes: set[str] = Field(default_factory=set)
    """WS-7: Scope keys that are read-only. Writes to these scopes fail fast.

    Keys must be in ``{kind}:{scope_id}`` format (e.g. ``"agent:shared-kb"``).
    An empty set (default) means all scopes are writable (legacy behaviour).
    """
    require_dream_agent_approval_for_untrusted_directives: bool = False
    """WS-7: When True, untrusted-source episodes proposing directive/requirement
    memories require dream-agent approval before materialization.

    Episodes are marked untrusted when ingested via add_episode(trusted=False)
    or add_context(trusted=False).  The gate fires in _run_formation and records
    an auditable 'formation_untrusted_write_gated' decision.

    False (default) = no approval gate (legacy behaviour unchanged).
    """
    pure_read_retrieval: bool = False
    """When True, retrieval never writes: search reports archived matches instead
    of reviving them.

    False (default) = historical behaviour, byte-for-byte: a retrieval whose
    query matches an archived row's prune ghost revives that row inline, emits
    the ``PRUNING_GHOST_RESTORED`` receipt and the ``pruning_ghost_regret``
    dream decision, and returns the revived row in the results.  Nothing an
    existing caller does changes.

    True = ``search``/``search_context``/``semantic_search`` become pure reads.
    Matching archived rows are only *reported* (``results.archived``, with
    ``disposition == "available_to_restore"``) and are revived exclusively by
    the explicit :meth:`memotron.Memotron.restore_archived_memory`
    curation action (MCP: ``memory_restore``, ``restore_archived_memory``),
    which is available in both modes.

    Why an operator turns it on: revive-on-read makes every read a writer, and
    a read that writes takes row locks on the memory graph.  That is fine for a
    single process, but it contends — and serialises retrieval — once more than
    one replica serves reads against the same Operational Store.  Turn this on
    to run retrieval on multiple replicas, and accept that archived memories
    then come back only when something explicitly asks for them.
    """
    runtime_policy_source_trace: dict[str, str] = Field(default_factory=dict)
    runtime_policy_alias: str | None = None
    runtime_policy_contract_digest: str | None = None
    runtime_certification_verdict: str = "uncertified"

    @model_validator(mode="after")
    def validate_config(self) -> DreamConfig:
        instruction_names = [instruction.name for instruction in self.instruction_sets]
        if len(instruction_names) != len(set(instruction_names)):
            raise ValueError("instruction set names must be unique")
        prompt_profile_keys = [profile.key for profile in self.prompt_profiles]
        if len(prompt_profile_keys) != len(set(prompt_profile_keys)):
            raise ValueError("prompt profile name/version pairs must be unique")
        known = set(instruction_names)
        unknown_jobs = [job.name for job in self.jobs if job.instruction_set not in known]
        if unknown_jobs:
            raise ValueError(f"dream jobs reference unknown instruction sets: {unknown_jobs}")
        known_prompt_profiles = set(prompt_profile_keys)
        unknown_prompt_jobs = [
            job.name
            for job in self.jobs
            if f"{job.prompt_profile}@{job.prompt_profile_version}" not in known_prompt_profiles
        ]
        if unknown_prompt_jobs:
            raise ValueError(f"dream jobs reference unknown prompt profiles: {unknown_prompt_jobs}")
        consolidation_profile_keys = {profile.key for profile in self.consolidation_profiles}
        if len(consolidation_profile_keys) != len(self.consolidation_profiles):
            raise ValueError("consolidation profile name/version pairs must be unique")
        unknown_consolidation_jobs = [
            job.name
            for job in self.jobs
            if job.kind == DreamJobKind.CONSOLIDATION
            and f"{job.consolidation_profile}@{job.consolidation_profile_version}" not in consolidation_profile_keys
        ]
        if unknown_consolidation_jobs:
            raise ValueError(
                f"consolidation jobs reference unknown consolidation profiles: {unknown_consolidation_jobs}"
            )
        if not self.jobs:
            raise ValueError("at least one dream job is required")
        # WS-3: validate that jobs referencing a motive name do so against the bank.
        if self.memory_bank is not None:
            known_motives = set(self.memory_bank.names)
            unknown_motive_jobs = [
                job.name for job in self.jobs if job.motive is not None and job.motive not in known_motives
            ]
            if unknown_motive_jobs:
                raise ValueError(
                    f"dream jobs reference unknown motive names (not in memory_bank): "
                    f"{unknown_motive_jobs}. Known motives: {sorted(known_motives)}"
                )
        return self

    def instruction_set(self, name: str) -> DreamInstructionSet:
        for instruction_set in self.instruction_sets:
            if instruction_set.name == name:
                return instruction_set
        raise ValueError(f"unknown instruction set: {name}")

    def prompt_profile(self, name: str, version: str = "v1") -> DreamPromptProfile:
        key = f"{name}@{version}"
        for prompt_profile in self.prompt_profiles:
            if prompt_profile.key == key:
                return prompt_profile
        raise ValueError(f"unknown prompt profile: {key}")

    def prompt_profile_for_job(self, job: DreamJob) -> DreamPromptProfile:
        return self.prompt_profile(job.prompt_profile, job.prompt_profile_version).with_override(job.prompt_override)

    def consolidation_profile_for_job(self, job: DreamJob) -> ConsolidationSynthesisProfile:
        key = f"{job.consolidation_profile}@{job.consolidation_profile_version}"
        for profile in self.consolidation_profiles:
            if profile.key == key:
                return profile
        raise ValueError(f"unknown consolidation profile: {key}")

    def resolve_motive(self, *, job: DreamJob, episode_metadata: dict) -> Motive | None:
        """WS-3: Resolve the active Motive for a job/episode pair.

        Resolution precedence:
            1. job.motive  — pinned at job-configuration time
            2. episode metadata["motive"]  — per-call hint from the ingestion API
            3. None  — no Motive; legacy behaviour unchanged

        Returns None when no Motive is active, which is the no-op path that preserves
        full backward compatibility with all existing tests.
        """
        if self.memory_bank is None:
            return None
        motive_name: str | None = job.motive or episode_metadata.get("motive")
        if not motive_name:
            return None
        return self.memory_bank.motive(motive_name)


def default_config() -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable people, agents, users, customers, teams, systems, and concepts worth remembering.",
                properties=("kind", "role", "tier", "region", "system"),
                # T0-11: this default is an LLM-transport path, and `strict_properties`
                # defaults to True. Its own docstring prescribes the opposite for exactly
                # this case -- "Set False for LLM transports where the model may invent
                # valid-sounding property keys that are not in the schema". Left True, a
                # single invented key quarantines the WHOLE candidate
                # (`extraction.py:1041-1047`), so `default_config()` plus a real extractor
                # formed NOTHING: measured `created_relationships=0, admitted_candidates=0,
                # quarantined_candidates=3`, every one `property_key_not_allowed`, exit 0,
                # no error. `agent_memory/_config.py:60` already sets False, which is why
                # the agent-memory path formed memory and the documented SDK path did not.
                #
                # It also makes the prompt-rendering comment at `_instructions.py:816-828`
                # true. That comment justifies withholding the allow-list from the model on
                # the grounds that unknown keys are "silently strip[ped]" so "omission costs
                # nothing" -- which held only where strictness was already off. Here it now
                # does hold.
                #
                # THE COST, stated: an unknown property key is dropped rather than
                # quarantined, so the ATTRIBUTE is lost where it used to be recoverable. The
                # quarantine path exists so a widened allow-list can re-govern a row without
                # re-extraction (`extraction.py:292-299`), and property-key cases on this
                # config no longer reach it. Losing one attribute beats losing the memory.
                # A caller that wants the strict contract sets it back explicitly.
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Remember stable preferences that improve future agent behavior or customer support.",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements, constraints, and obligations.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that help an agent perform its role better.",
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),
            DreamJob(name="pruning-default", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        ),
        pruning=PruningPolicy(),
        storage=storage_settings_from_env(),
    )
