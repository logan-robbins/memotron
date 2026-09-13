"""The behaviour policies a dream pass is governed by.

One model per decision the engine makes without asking: what counts as a duplicate,
when an entity resolves to an existing one, when a fact is pruned, which of two
contradictory claims wins, how confident is confident enough. Every default here is
a product decision, which is why they are values rather than constants in code."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from memotron.config._metadata import (
    MetadataFilterValue,
    metadata_matches_filter,
    validate_metadata_filter,
)
from memotron.models import (
    EpisodeType,
    MemoryAuthority,
    MemoryType,
    RelationshipStatus,
)


class MemoryHealthPolicy(BaseModel):
    """WS-24: distribution gates that BLOCK a degenerate formation run.

    A memory graph whose types have collapsed into one bucket is not a graph
    with a cosmetic problem — it is a retrieval failure waiting to happen, and
    it is silent.  These thresholds make it loud.

    Every threshold is ``None``-able (that gate is off) and every trip is
    receipted.  ``min_instances`` exists because share and entropy are
    meaningless on a handful of rows: a three-fact scope trivially has a 0.33
    max share and a two-fact scope a 0.5 one, so gating them would fail every
    healthy new scope instead of the degenerate mature ones.
    """

    enabled: bool = True

    min_instances: int = Field(default=50, ge=0)
    """Context-visible active facts a scope must hold before share/entropy
    gates apply.  Calibrated so the healthy demo graph is inert and the
    degenerate 141-fact portal graph is judged."""

    max_type_share_block: float | None = Field(default=0.40, gt=0.0, le=1.0)
    max_type_share_warn: float | None = Field(default=None, gt=0.0, le=1.0)
    min_normalized_entropy_warn: float | None = Field(default=0.60, ge=0.0, le=1.0)
    min_normalized_entropy_block: float | None = Field(default=0.40, ge=0.0, le=1.0)

    warn_on_zero_abstain_rate: bool = True
    """A zero abstain rate means the gate is a sink: every candidate found a
    home, which is precisely the signature of the failure being gated."""

    dead_type_min_instances: int = Field(default=200, ge=1)
    """Instances a scope must hold before a type with zero instances counts as
    dead rather than as merely unobserved."""

    dead_types_block: bool = False
    """Dead types warn by default; a deployment that has declared its type
    vocabulary deliberately can make them fatal."""

    raise_on_block: bool = True
    """``True`` fails the run loudly.  ``False`` records the trip on the report
    and lets the run finish — for an operator draining a known-degenerate
    backlog who wants the receipts without the abort."""

    general_labels: tuple[str, ...] = ("Concept",)
    """WS-27 T4: node LABELS treated as the ontology's catch-all/general
    bucket.  This is a DIFFERENT axis from every gate above — those measure
    the ``memory_type`` (identity/preference/requirement/...) distribution of
    RELATIONSHIPS; this measures the ENTITY-LABEL distribution of NODES,
    which is exactly where a catch-all like ``Concept`` silently absorbs a
    corpus ("previous vocabulary absorbed 84% of this corpus" —
    ``ingest/kb_config.py``).  Default targets the one built-in catch-all
    label; a tenant with its own general label(s) overrides this tuple."""

    max_general_label_share_block: float | None = Field(default=0.40, gt=0.0, le=1.0)
    max_general_label_share_warn: float | None = Field(default=None, gt=0.0, le=1.0)

    @field_validator("general_labels")
    @classmethod
    def normalize_general_labels(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raise ValueError("general_labels must be a sequence of strings, not a bare string")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError("general_labels entries must be non-blank strings")
            normalized.append(item.strip())
        return tuple(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_thresholds(self) -> MemoryHealthPolicy:
        if (
            self.max_type_share_warn is not None
            and self.max_type_share_block is not None
            and self.max_type_share_warn > self.max_type_share_block
        ):
            raise ValueError("max_type_share_warn must be at or below max_type_share_block")
        if (
            self.min_normalized_entropy_warn is not None
            and self.min_normalized_entropy_block is not None
            and self.min_normalized_entropy_warn < self.min_normalized_entropy_block
        ):
            raise ValueError("min_normalized_entropy_warn must be at or above min_normalized_entropy_block")
        if (
            self.max_general_label_share_warn is not None
            and self.max_general_label_share_block is not None
            and self.max_general_label_share_warn > self.max_general_label_share_block
        ):
            raise ValueError("max_general_label_share_warn must be at or below max_general_label_share_block")
        return self


class DedupPolicy(BaseModel):
    """Per-type thresholds for write-side semantic dedup (WS-1).

    cosine_threshold — global default.  Paraphrases with cosine ≥ threshold
        reinforce an existing row instead of creating a new one.  Exact string
        matches always reinforce (cosine ≈ 1.0 satisfies any threshold).

    memory_type_thresholds — per-type overrides (keyed by MemoryType value):
        ``directive`` is more aggressive (lower threshold — merge near-duplicates
        faster to control D2 churn).
        ``identity`` is conservative (higher threshold — never merge "Mike"/"Michael").

    Default baseline: 0.88.
    """

    cosine_threshold: float = Field(default=0.88, ge=0.0, le=1.0)
    memory_type_thresholds: dict[str, float] = Field(default_factory=dict)

    @field_validator("memory_type_thresholds")
    @classmethod
    def normalize_memory_type_thresholds(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for memory_type, threshold in value.items():
            normalized_type = memory_type.strip().lower()
            if not normalized_type:
                raise ValueError("memory_type_thresholds keys cannot be blank")
            if threshold < 0.0 or threshold > 1.0:
                raise ValueError("memory_type_thresholds values must be between 0.0 and 1.0")
            normalized[normalized_type] = threshold
        return normalized

    def threshold_for(self, memory_type: str | None) -> float:
        """Return the cosine threshold for a given memory type value."""
        if memory_type is not None:
            override = self.memory_type_thresholds.get(memory_type.strip().lower())
            if override is not None:
                return override
        return self.cosine_threshold


class PredicateCanonicalizationPolicy(BaseModel):
    """WS-17 T16: per-scope canonical predicate registry policy.

    Truth keys are built on the CANONICAL predicate so paraphrased predicate
    surfaces ("resides in" vs "lives in") land on ONE truth slot and
    corrections/supersession/dedup bridge automatically.  The registry is
    first-wins per scope: the first surface to arrive becomes the canonical for
    every later surface that maps onto it, so resolution is deterministic for a
    given arrival order.

    enabled
        When False, formation behaves byte-identically to the pre-T16 code:
        truth keys use the surface predicate and no registry rows, receipts, or
        ``predicate_canonical`` properties are written.
    synonyms
        Operator-supplied surface→canonical map (normalized keys and values;
        the validator lowercases both).  Consulted before the embedding pass —
        the deterministic bridge for known paraphrase families.
    embedding_threshold
        Minimum cosine (active transport, predicates embedded fresh — never a
        cross-space comparison) between a new predicate and an existing scope
        canonical for the new surface to map onto that canonical.
    max_candidates
        Cap on how many of the scope's earliest-registered canonical predicates
        the embedding pass compares against (deterministic registry order).
    """

    model_config = {"frozen": True}

    enabled: bool = True
    synonyms: dict[str, str] = Field(default_factory=dict)
    embedding_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    max_candidates: int = Field(default=256, ge=1)

    @field_validator("synonyms")
    @classmethod
    def normalize_synonyms(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for surface, canonical in value.items():
            normalized_surface = " ".join(str(surface).casefold().strip().split())
            normalized_canonical = " ".join(str(canonical).casefold().strip().split())
            if not normalized_surface:
                raise ValueError("synonyms keys cannot be blank")
            if not normalized_canonical:
                raise ValueError("synonyms values cannot be blank")
            normalized[normalized_surface] = normalized_canonical
        return normalized


class EntityResolutionPolicy(BaseModel):
    """WS-17 T16b: per-scope semantic entity resolution (alias registry) policy.

    Node identity and truth keys are NAME-keyed (``scope:label:name`` /
    ``scope:subject:predicate[:object]``), so surface-form variants of one
    real-world entity ("Jedai Gateway" / "the gateway" / "JedAI GW") fragment
    both the node graph and the truth slots.  This policy governs the
    name-level alias registry that bridges both planes: formation resolves
    subject and object names through the per-scope registry BEFORE node upsert
    and truth-key computation, so aliased mentions land on ONE node and ONE
    truth slot — non-destructively (relationship endpoints are never
    rewritten; reads union across the alias boundary).

    Linking is fail-closed and multi-signal.  A mention links to a canonical
    entity only through a composed score::

        link_score = weight_llm * llm_confidence
                   + weight_name * name_cosine
                   + weight_context * context_overlap
                   + weight_neighborhood * neighborhood_overlap

    where a missing signal contributes 0.0 (never fabricated).  With the
    default weights the three offline signals sum to at most 0.6 — BELOW
    ``review_threshold`` — so an alias can never auto-link (or even propose)
    without either extractor attestation (``entity_ref`` + link confidence)
    or an operator synonym.  Two names carrying DIFFERING identifier-like
    tokens (the distinct-ID gauntlet: ``kb_ds_..._wdw_...`` vs
    ``kb_ds_..._dlr_...``) are hard-blocked from linking regardless of score.

    enabled
        When False, formation behaves byte-identically to the pre-T16b code:
        no registry rows, no receipts, no node name embeddings, no prompt
        inventory block, no read-side alias unions.
    synonyms
        Operator-supplied alias→canonical name map (normalized/lowercased both
        sides).  Registered as ACTIVE on first use — the deterministic bridge
        for known alias families.
    auto_link_threshold
        Composed score at or above which a mention auto-links: the alias is
        registered ACTIVE and the mention resolves to the canonical name for
        node identity and truth keys (receipted FORMATION_ENTITY_LINKED).
    review_threshold
        Composed score band [review_threshold, auto_link_threshold) parks a
        'proposed' alias row for human adjudication
        (``pending_entity_alias_proposals`` / ``resolve_entity_alias_proposal``)
        while the mention materializes under its surface name unchanged.
    weight_llm / weight_name / weight_context / weight_neighborhood
        Composed-score weights (must sum to 1.0).  ``weight_llm`` scales the
        extractor's per-mention link confidence; ``weight_name`` the same-space
        name-embedding cosine; ``weight_context`` the Jaccard overlap of
        provenance coordinates (episode metadata slug/section/source vs the
        candidate's accumulated fact provenance); ``weight_neighborhood`` the
        shared-neighbor overlap between the mention's existing node (when one
        exists) and the candidate node.
    max_inventory
        Cap on canonical entity names rendered into the extraction prompt's
        ENTITY INVENTORY block (names + kinds only — cheap).
    max_pair_scan
        Cap on canonical candidates the offline resolution pass compares
        against (deterministic node order: created_at, uuid).
    """

    model_config = {"frozen": True}

    enabled: bool = True
    synonyms: dict[str, str] = Field(default_factory=dict)
    auto_link_threshold: float = Field(default=0.85, ge=0.0, le=1.0)
    review_threshold: float = Field(default=0.65, ge=0.0, le=1.0)
    weight_llm: float = Field(default=0.4, ge=0.0, le=1.0)
    weight_name: float = Field(default=0.3, ge=0.0, le=1.0)
    weight_context: float = Field(default=0.15, ge=0.0, le=1.0)
    weight_neighborhood: float = Field(default=0.15, ge=0.0, le=1.0)
    max_inventory: int = Field(default=500, ge=1)
    max_pair_scan: int = Field(default=512, ge=1)

    @field_validator("synonyms")
    @classmethod
    def normalize_synonyms(cls, value: dict[str, str]) -> dict[str, str]:
        normalized: dict[str, str] = {}
        for surface, canonical in value.items():
            normalized_surface = " ".join(str(surface).casefold().strip().split())
            normalized_canonical = " ".join(str(canonical).casefold().strip().split())
            if not normalized_surface:
                raise ValueError("synonyms keys cannot be blank")
            if not normalized_canonical:
                raise ValueError("synonyms values cannot be blank")
            if normalized_surface == normalized_canonical:
                raise ValueError(f"synonyms cannot map a name to itself: {normalized_surface!r}")
            normalized[normalized_surface] = normalized_canonical
        return normalized

    @model_validator(mode="after")
    def validate_thresholds_and_weights(self) -> EntityResolutionPolicy:
        if self.review_threshold > self.auto_link_threshold:
            raise ValueError("EntityResolutionPolicy.review_threshold cannot exceed auto_link_threshold")
        weight_sum = self.weight_llm + self.weight_name + self.weight_context + self.weight_neighborhood
        if abs(weight_sum - 1.0) > 1e-9:
            raise ValueError(f"EntityResolutionPolicy weights must sum to 1.0, got {weight_sum!r}")
        return self


class RollupConsolidationPolicy(BaseModel):
    """Policy for canonical dependency-tracked rollup consolidation.

    Every consolidation job uses this reducer.  It is intentionally not an
    optional second pass: allowing a flat peer-producing path alongside it
    defeats the context-reduction and dependency-invalidation guarantees.

    cluster_threshold
        Minimum cosine similarity (on full fact embeddings) for two memories to
        be placed in the same cluster.  Default 0.75 — tunable per deployment.
    min_cluster_size
        Minimum number of members before a cluster is synthesized into a ROLLUP.
        Default 3.
    max_depth
        Maximum recursive rollup depth.  rollup_depth=1 = raw facts summarized;
        rollup_depth=2 = rollups summarized into higher rollups; etc.
        Default 2.
    """

    cluster_threshold: float = Field(default=0.75, ge=0.0, le=1.0)
    min_cluster_size: int = Field(default=3, ge=2)
    max_depth: int = Field(default=2, ge=1)
    reinforcement_materiality_observed_count_delta: int = Field(default=3, ge=1)
    """New corroborations of a child required before a live rollup is rebuilt.

    Reinforcement is evidence about an existing assertion, not a new assertion.
    Rollups therefore maintain an evidence aggregate on every reinforcement and
    only leave context when the aggregate has changed enough to be material.
    """
    reinforcement_materiality_confidence_delta: float = Field(default=0.10, ge=0.0, le=1.0)
    """Minimum confidence increase of a child that makes a rollup stale."""
    cross_prefix_duplicate_threshold: float | None = Field(default=0.93, ge=0.0, le=1.0)
    """WS-17 T17: full-fact embedding cosine at or above which two active rows on
    DIFFERENT truth prefixes (same memory type, compatible claim/stance/polarity/
    authority class, same-identifier stored vectors) are treated as duplicates —
    the weaker row is demoted from context (``duplicate_of``), never deleted.
    ``None`` disables the sweep entirely."""
    max_duplicate_demotions_per_run: int = Field(default=32, ge=1)
    """WS-17 T17: per-run cap on cross-prefix duplicate demotions per scope."""


def _default_class_precedence() -> dict[str, int]:
    # Execution precedence: a procedural SKILL is run step-by-step and therefore
    # dominates behavior; a FILE is referenced; MEMORY is advisory context. Higher
    # wins at run time. These defaults make a skill out-rank memory by design —
    # which is exactly why a stale skill can silently override escalating feedback.
    return {"skill": 30, "file": 20, "memory": 10, "generated_output": 0}


class CoherencePolicy(BaseModel):
    """WS-10: Policy for the cross-artifact coherence dream cycle.

    The coherence cycle extends reconciliation *beyond the memory store*: it
    projects memory, skills, and files into a common directive representation and
    detects contradictions that ordinary (memory-vs-memory) dreaming cannot see.

    All fields have safe defaults and the cycle is fully opt-in (a COHERENCE
    DreamJob, or an explicit ``run_coherence_scan`` call) — no existing behaviour
    changes when it is never invoked.

    identity_threshold
        Minimum cosine similarity between two directives' topic embeddings for
        them to be treated as concerning the same subject.  Default 0.50, tuned
        for the hermetic n-gram embedding substrate (shared topic vocabulary).
    escalation_cycles_threshold
        Minimum ``observed_count`` (reinforcement cycles) for a memory directive
        to qualify as a windup candidate — the "feedback given over and over"
        signature.  Default 3.
    class_precedence
        Execution precedence per artifact class.  Higher governs at run time.
    participating_memory_types
        Which memory types count as behavioral directives projected into the
        coherence graph.  Default: directive + requirement (procedural/normative).
    detect_escalation_windup / detect_contradictions
        Toggle each detection mode independently.
    hold_escalating_directives
        When True, an escalating memory directive implicated in a windup incident
        is marked ``coherence_hold`` so subsequent formation stops blindly
        strengthening it (the anti-windup actuator that closes the loop).
    """

    identity_threshold: float = Field(default=0.50, ge=0.0, le=1.0)
    escalation_cycles_threshold: int = Field(default=3, ge=1)
    negative_outcome_scan_threshold: int = Field(default=2, ge=1)
    min_attribution_confidence: float = Field(default=0.70, ge=0.0, le=1.0)
    artifact_contribution_min_evidence: int = Field(default=3, ge=1)
    artifact_quarantine_threshold: float = Field(default=-0.50, ge=-1.0, le=1.0)
    class_precedence: dict[str, int] = Field(default_factory=_default_class_precedence)
    participating_memory_types: tuple[str, ...] = ("directive", "requirement")
    detect_escalation_windup: bool = True
    detect_contradictions: bool = True
    hold_escalating_directives: bool = True

    @field_validator("class_precedence")
    @classmethod
    def validate_precedence(cls, value: dict[str, int]) -> dict[str, int]:
        for key, weight in value.items():
            if not str(key).strip():
                raise ValueError("class_precedence keys cannot be blank")
            if int(weight) < 0:
                raise ValueError("class_precedence values must be non-negative")
        return value

    def precedence_for(self, artifact_class: str) -> int:
        return int(self.class_precedence.get(str(artifact_class), 0))


class GrowthPolicy(BaseModel):
    """WS-6: Optional soft-cap governance of last resort.

    Design intent
    -------------
    The soft cap is an INTERNAL BACKSTOP, not the primary growth control.
    WS-1 semantic dedup and WS-4 rollup consolidation are the real mechanisms
    that keep context-visible counts bounded.  This cap fires only when those
    mechanisms have not yet reduced the count to an acceptable level — i.e.,
    it should rarely fire in a well-tuned deployment.

    It is NEVER exposed as a tenant-facing "hard cap"; the tenant sees healthy
    ``memory_evolution()`` metrics.  Operators configure it here.

    Configuration
    -------------
    soft_cap
        Optional global ceiling on context-visible active relationships per scope.
        When None (default), no cap is applied and pruning behaviour is unchanged.
        Must be a positive integer if set; negative values are rejected at
        construction time (fail-fast).

    per_type_soft_caps
        Optional per-memory_type ceilings (keyed by MemoryType value, e.g. "directive").
        Checked independently of soft_cap.  None (default) = no per-type cap.

    All defaults are None = off = zero change to existing pruning behaviour.
    """

    soft_cap: int | None = Field(default=None, ge=1)
    """Global context-visible active-relationship ceiling per scope.

    None = off (default, existing behaviour unchanged).
    Positive int = prune lowest-value rows until count ≤ soft_cap.
    """

    per_type_soft_caps: dict[str, int] = Field(default_factory=dict)
    """Per-memory_type context-visible active-relationship ceilings.

    Keyed by MemoryType value (e.g. ``"directive"``, ``"preference"``).
    Each is a positive int; zero or negative values are rejected at construction.
    Empty dict (default) = no per-type caps.
    """

    @field_validator("per_type_soft_caps")
    @classmethod
    def validate_per_type_soft_caps(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for memory_type, cap in value.items():
            key = memory_type.strip().lower()
            if not key:
                raise ValueError("per_type_soft_caps keys cannot be blank")
            if cap < 1:
                raise ValueError(f"per_type_soft_caps[{memory_type!r}] must be >= 1, got {cap}")
            normalized[key] = cap
        return normalized


class RetentionPolicy(BaseModel):
    """Hard eligibility gates and loss-aware ranking inputs for generic pruning."""

    protected_memory_types: tuple[MemoryType, ...] = (
        MemoryType.ANCHOR,
        MemoryType.REQUIREMENT,
        MemoryType.DECISION,
        MemoryType.INCIDENT,
        MemoryType.DIRECTIVE,
        MemoryType.ROLLUP,
    )
    protection_grace_seconds: int = Field(default=7 * 24 * 3600, ge=0)
    ghost_regret_alert_rate: float = Field(default=0.05, ge=0.0, le=1.0)
    archive_ttl_seconds: int | None = Field(default=None, ge=0)
    use_recency_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    observation_recency_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    adaptive_regret_enabled: bool = True
    adaptive_regret_min_samples: int = Field(default=5, ge=1)
    adaptive_regret_step: float = Field(default=0.15, ge=0.0, le=1.0)

    @field_validator("protected_memory_types", mode="before")
    @classmethod
    def normalize_protected_types(cls, value: Any) -> tuple[MemoryType, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("protected_memory_types must be a list or tuple of MemoryType values")
        return tuple(dict.fromkeys(MemoryType(item) for item in value))

    @model_validator(mode="after")
    def validate_recency_weights(self) -> RetentionPolicy:
        if self.use_recency_weight + self.observation_recency_weight <= 0:
            raise ValueError("at least one retention recency weight must be positive")
        return self


class PruningPolicy(BaseModel):
    min_confidence: float = Field(default=0.25, ge=0.0, le=1.0)
    superseded_retention_seconds: int = Field(default=0, ge=0)
    active_max_age_seconds: int | None = Field(default=None, ge=0)
    relationship_min_confidence: dict[str, float] = Field(default_factory=dict)
    relationship_superseded_retention_seconds: dict[str, int] = Field(default_factory=dict)
    relationship_active_max_age_seconds: dict[str, int] = Field(default_factory=dict)
    growth: GrowthPolicy = Field(default_factory=GrowthPolicy)
    retention: RetentionPolicy = Field(default_factory=RetentionPolicy)
    """WS-6: Soft-cap governance policy.

    Default (GrowthPolicy()) has soft_cap=None and no per_type_soft_caps —
    zero change to existing pruning behaviour (the additive/opt-in invariant).
    """
    stale_after_seconds: dict[str, int] = Field(default_factory=lambda: {MemoryType.STATE.value: 30 * 86400})
    """WS-20 T25: per-memory-type staleness lifecycle (retention plane only).

    An ACTIVE row whose memory_type has an entry here, whose age since
    ``last_seen_at`` (falling back to ``valid_from`` then ``created_at``)
    exceeds the entry, AND which has zero use events within that window becomes
    a generic-prune candidate with reason ``"stale_unused"`` — flowing through
    the existing retention gate chain (protected types, corrections, holds, and
    pins all still win), archived to a restorable PruneGhost, never deleted.
    Truth is untouched.

    The default covers ONLY ``"state"`` (30 days — the taxonomy's short-lived
    type), so with default config an uncontradicted, never-used state fact is
    no longer immortal while every other type keeps existing behaviour.
    Operators opt other types in per key; ``{}`` disables staleness entirely.
    Keys are MemoryType values; unknown types fail fast at construction.
    """

    @field_validator("relationship_min_confidence")
    @classmethod
    def normalize_relationship_min_confidence(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for relationship_type, threshold in value.items():
            normalized_type = relationship_type.strip().replace(" ", "_").upper()
            if not normalized_type:
                raise ValueError("relationship_min_confidence keys cannot be blank")
            if threshold < 0.0 or threshold > 1.0:
                raise ValueError("relationship_min_confidence values must be between 0.0 and 1.0")
            normalized[normalized_type] = threshold
        return normalized

    @field_validator("relationship_superseded_retention_seconds")
    @classmethod
    def normalize_relationship_superseded_retention_seconds(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for relationship_type, seconds in value.items():
            normalized_type = relationship_type.strip().replace(" ", "_").upper()
            if not normalized_type:
                raise ValueError("relationship_superseded_retention_seconds keys cannot be blank")
            if seconds < 0:
                raise ValueError("relationship_superseded_retention_seconds values must be non-negative")
            normalized[normalized_type] = seconds
        return normalized

    @field_validator("relationship_active_max_age_seconds")
    @classmethod
    def normalize_relationship_active_max_age_seconds(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for relationship_type, seconds in value.items():
            normalized_type = relationship_type.strip().replace(" ", "_").upper()
            if not normalized_type:
                raise ValueError("relationship_active_max_age_seconds keys cannot be blank")
            if seconds < 0:
                raise ValueError("relationship_active_max_age_seconds values must be non-negative")
            normalized[normalized_type] = seconds
        return normalized

    @field_validator("stale_after_seconds")
    @classmethod
    def normalize_stale_after_seconds(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for memory_type, seconds in value.items():
            key = memory_type.strip().lower()
            if not key:
                raise ValueError("stale_after_seconds keys cannot be blank")
            normalized_key = MemoryType(key).value  # unknown memory types fail fast
            if seconds < 1:
                raise ValueError(f"stale_after_seconds[{memory_type!r}] must be >= 1 second, got {seconds}")
            normalized[normalized_key] = seconds
        return normalized

    @property
    def superseded_retention(self) -> timedelta:
        return timedelta(seconds=self.superseded_retention_seconds)

    def min_confidence_for(self, relationship_type: str) -> float:
        return self.relationship_min_confidence.get(
            relationship_type.strip().replace(" ", "_").upper(),
            self.min_confidence,
        )

    def superseded_retention_for(self, relationship_type: str) -> timedelta:
        seconds = self.relationship_superseded_retention_seconds.get(
            relationship_type.strip().replace(" ", "_").upper(),
            self.superseded_retention_seconds,
        )
        return timedelta(seconds=seconds)

    def active_max_age_for(self, relationship_type: str) -> timedelta | None:
        relationship_key = relationship_type.strip().replace(" ", "_").upper()
        if relationship_key in self.relationship_active_max_age_seconds:
            return timedelta(seconds=self.relationship_active_max_age_seconds[relationship_key])
        if self.active_max_age_seconds is None:
            return None
        return timedelta(seconds=self.active_max_age_seconds)

    def stale_after_for(self, memory_type: str | None) -> timedelta | None:
        """WS-20 T25: the staleness window for *memory_type*, or None when exempt."""
        if memory_type is None:
            return None
        seconds = self.stale_after_seconds.get(memory_type.strip().lower())
        if seconds is None:
            return None
        return timedelta(seconds=seconds)


def _default_authority_ranks() -> dict[str, int]:
    return {
        MemoryAuthority.GENERATED_OUTPUT.value: 0,
        MemoryAuthority.UNTRUSTED.value: 10,
        MemoryAuthority.AGENT.value: 20,
        MemoryAuthority.USER.value: 30,
        MemoryAuthority.OPERATOR.value: 40,
        MemoryAuthority.SYSTEM.value: 50,
    }


def _default_memory_severity() -> dict[str, int]:
    return {
        MemoryType.STATE.value: 10,
        MemoryType.PREFERENCE.value: 20,
        MemoryType.ROLLUP.value: 25,
        MemoryType.DIRECTIVE.value: 60,
        MemoryType.INCIDENT.value: 70,
        MemoryType.DECISION.value: 75,
        MemoryType.REQUIREMENT.value: 90,
        MemoryType.ANCHOR.value: 95,
    }


class SupersessionPolicy(BaseModel):
    """Authority- and severity-aware rules for changing current truth."""

    authority_ranks: dict[str, int] = Field(default_factory=_default_authority_ranks)
    memory_type_severity: dict[str, int] = Field(default_factory=_default_memory_severity)
    temporary_review_severity: int = Field(default=60, ge=0, le=100)
    gate_lower_authority: bool = True
    gate_equal_authority_temporary_high_severity: bool = True
    corroboration_margin: int = Field(default=3, ge=1)
    """WS-16 T12: evidence weight an incumbent must have before corroboration is required.

    An ACTIVE incumbent whose ``observed_count`` is at or above this margin cannot
    be flipped by a single equal-authority observation; the challenger is parked
    (inserted pre-SUPERSEDED with ``requires_operator_review``) until
    ``corroboration_required`` distinct observations of the same challenger
    statement exist.  Incumbents below the margin keep pre-WS-16 behavior: one
    equal-authority contradiction supersedes immediately.  Higher-authority
    challengers are NEVER gated by corroboration."""
    corroboration_required: int = Field(default=2, ge=1)
    """WS-16 T12: total distinct challenger observations needed to flip a
    corroborated incumbent.

    Counts the CURRENT observation plus previously parked observations of the
    same challenger statement (same normalized/committed object, or an
    object-embedding cosine at/above the effective dedup threshold with
    compatible claim mode, stance, and polarity) on the same truth slot.  When
    the count reaches this threshold the supersession is allowed and every
    counted parked sibling is resolved with ``review_resolution="corroborated"``."""
    recency_authoritative_types: frozenset[str] = Field(
        default_factory=lambda: frozenset(
            {MemoryType.PREFERENCE.value, MemoryType.DIRECTIVE.value, MemoryType.STATE.value}
        )
    )
    """WS-25 T2: memory types for which a newer, same-slot, contradictory
    candidate whose authority is at least the incumbent's auto-closes the
    incumbent — bypassing the ``temporary_high_severity_requires_higher_authority``
    and ``insufficient_corroboration`` gates (never the ``lower_authority`` gate:
    a genuinely lower-authority challenger is still parked, regardless of
    recency).  "Newer" is evaluated against a clamped effective time (the
    extractor's claimed ``valid_from`` can never postdate the episode that
    carries it), so a spoofed future ``valid_from`` cannot deterministically
    flip truth.

    This is deliberately NOT global last-writer-wins (NEXT.md WS-25 §9): types
    outside this set — the world-fact types ``anchor``/``decision``/
    ``incident``/``requirement`` — keep the unmodified evidence/corroboration
    gate.  Config-driven, not hard-coded, so a deployment can narrow or widen
    the set."""

    @field_validator("recency_authoritative_types")
    @classmethod
    def validate_recency_authoritative_types(cls, value: frozenset[str]) -> frozenset[str]:
        known = {item.value for item in MemoryType}
        unknown = {item for item in value if item not in known}
        if unknown:
            raise ValueError(f"recency_authoritative_types contains unknown memory types: {sorted(unknown)}")
        return frozenset(str(item) for item in value)

    @field_validator("authority_ranks")
    @classmethod
    def validate_authority_ranks(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {item.value for item in MemoryAuthority}
        missing = expected.difference(value)
        if missing:
            raise ValueError(f"authority_ranks is missing authority classes: {sorted(missing)}")
        if any(rank < 0 for rank in value.values()):
            raise ValueError("authority ranks must be non-negative")
        return {str(key): int(rank) for key, rank in value.items()}

    @field_validator("memory_type_severity")
    @classmethod
    def validate_memory_type_severity(cls, value: dict[str, int]) -> dict[str, int]:
        expected = {item.value for item in MemoryType}
        missing = expected.difference(value)
        if missing:
            raise ValueError(f"memory_type_severity is missing memory types: {sorted(missing)}")
        if any(score < 0 or score > 100 for score in value.values()):
            raise ValueError("memory type severity scores must be between 0 and 100")
        return {str(key): int(score) for key, score in value.items()}

    def authority_rank(self, authority: MemoryAuthority | str) -> int:
        return int(self.authority_ranks[MemoryAuthority(authority).value])

    def severity(self, memory_type: MemoryType | str) -> int:
        return int(self.memory_type_severity[MemoryType(memory_type).value])


class ConfidencePolicy(BaseModel):
    """WS-16 T13: principled truth-plane confidence (bounded evidence combination).

    Governs how a stored relationship's ``confidence`` evolves:

    - Reinforcement accumulates bounded evidence instead of max-pooling:
      ``c' = min(ceiling, c + (1 - c) * reinforcement_gain * c_observation)``.
    - A challenger PARKED by the supersession gate discounts the surviving
      ACTIVE incumbent once per parked challenger:
      ``c' = max(floor, c * (1 - contradiction_discount * c_challenger))``.
    - ``half_life_days`` is a READ-ONLY decay knob consumed solely by the
      pruning min-confidence evaluation; stored confidence is never mutated
      by decay, and ``None`` (default) keeps pruning byte-identical to the
      undecayed behavior.

    A ``coherence_hold`` freezes the row's confidence against both
    reinforcement and the dispute discount (WS-10 anti-windup).
    """

    model_config = {"frozen": True}

    reinforcement_gain: float = Field(default=0.25, gt=0.0, le=1.0)
    """Fraction of the remaining headroom ``(1 - c)`` one corroborating
    observation contributes, scaled by that observation's own confidence."""
    contradiction_discount: float = Field(default=0.25, ge=0.0, le=1.0)
    """Multiplicative penalty strength applied to a disputed incumbent per
    parked challenger, scaled by the challenger's confidence."""
    floor: float = Field(default=0.05, ge=0.0, le=1.0)
    """Lower bound: the dispute discount never pushes confidence below this."""
    ceiling: float = Field(default=0.99, ge=0.0, le=1.0)
    """Upper bound: evidence accumulation never pushes confidence above this
    (no amount of repetition yields certainty)."""
    half_life_days: float | None = Field(default=None, gt=0.0)
    """Optional decay half-life in days. ``None`` (default) = truth confidence
    never decays.  When set, the pruning min-confidence gate evaluates
    ``stored * 2 ** (-age_days / half_life_days)`` (age from ``last_seen_at``,
    falling back to ``valid_from`` then ``created_at``) WITHOUT mutating the
    stored value."""

    @model_validator(mode="after")
    def validate_bounds(self) -> ConfidencePolicy:
        if self.floor >= self.ceiling:
            raise ValueError("ConfidencePolicy.floor must be strictly below ceiling")
        return self


class DreamContextPolicy(BaseModel):
    enabled: bool = True
    max_relationships: int = Field(default=12, ge=0)
    include_statuses: tuple[RelationshipStatus, ...] = (RelationshipStatus.ACTIVE,)
    relationship_types: tuple[str, ...] | None = None
    metadata_filter: dict[str, MetadataFilterValue] = Field(default_factory=dict)
    as_of_episode_time: bool = True
    include_metadata: bool = False

    @field_validator("relationship_types")
    @classmethod
    def normalize_relationship_types(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for relationship_type in value:
            normalized_type = relationship_type.strip().replace(" ", "_").upper()
            if not normalized_type:
                raise ValueError("relationship_types entries cannot be blank")
            normalized.append(normalized_type)
        return tuple(dict.fromkeys(normalized))

    @field_validator("metadata_filter", mode="before")
    @classmethod
    def normalize_metadata_filter(cls, value: Any) -> dict[str, MetadataFilterValue]:
        return validate_metadata_filter(value)


class DreamEpisodeFilter(BaseModel):
    source_types: tuple[EpisodeType, ...] | None = None
    source_descriptions: tuple[str, ...] | None = None
    metadata_filter: dict[str, MetadataFilterValue] = Field(default_factory=dict)

    @field_validator("source_types")
    @classmethod
    def normalize_source_types(cls, value: tuple[EpisodeType, ...] | None) -> tuple[EpisodeType, ...] | None:
        if value is None:
            return None
        return tuple(dict.fromkeys(value))

    @field_validator("source_descriptions")
    @classmethod
    def normalize_source_descriptions(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for source_description in value:
            if not source_description.strip():
                raise ValueError("source_descriptions entries cannot be blank")
            normalized.append(source_description.strip())
        return tuple(dict.fromkeys(normalized))

    @field_validator("metadata_filter", mode="before")
    @classmethod
    def normalize_metadata_filter(cls, value: Any) -> dict[str, MetadataFilterValue]:
        return validate_metadata_filter(value)

    def matches(self, episode: Any) -> bool:
        if self.source_types is not None and episode.source not in set(self.source_types):
            return False
        if self.source_descriptions is not None and episode.source_description not in set(self.source_descriptions):
            return False
        return metadata_matches_filter(episode.metadata, self.metadata_filter)
