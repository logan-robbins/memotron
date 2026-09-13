"""Memory health and evolution reporting.

The health report is the gate: it can trip and refuse formation, which is why
MemoryHealthGateError lives beside it rather than with the other exceptions. The
evolution types answer the auditor's question -- how did this scope's memory get
to its current state, and what proves it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from memotron.models._enums import (
    RelationshipStatus,
)
from memotron.models._graph import (
    MemoryScope,
)


class MemoryHealthGateTrip(BaseModel):
    """WS-24: one health threshold that was crossed, with the numbers that crossed it."""

    gate: str
    severity: str
    """``"warn"`` or ``"block"``."""
    observed: float
    threshold: float | None = None
    detail: str = ""


class MemoryHealthReport(BaseModel):
    """WS-24: the type-distribution health of one scope, plus every gate trip.

    Four measurements, chosen because each one catches a different shape of the
    same failure — a graph whose types have collapsed into one bucket:

    ``max_type_share``
        the largest single type's share of context-visible facts.  The direct
        reading of "148 of 165 facts are one type".
    ``normalized_type_entropy``
        Shannon entropy over the type distribution divided by ``ln K`` (K =
        the eligible type vocabulary), so it is comparable across
        vocabularies.  1.0 is a uniform spread; 0.0 is total collapse.  It
        catches the case where no single type breaks the share ceiling but the
        mass has still concentrated in two of eight.
    ``abstain_rate``
        the share of evaluated candidates the gate quarantined.  Exactly zero
        is itself a warning: it means every candidate found a home, which is
        the signature of a gate acting as a sink rather than a filter.
    ``dead_types``
        eligible types with zero instances once the scope is large enough for
        absence to mean something.  A type nothing ever lands in is either
        mis-specified or unreachable.
    """

    scope: MemoryScope
    evaluated_at: datetime
    total_instances: int = 0
    per_type_counts: dict[str, int] = Field(default_factory=dict)
    eligible_types: list[str] = Field(default_factory=list)
    max_type_share: float = 0.0
    dominant_type: str | None = None
    normalized_type_entropy: float = 1.0
    abstain_rate: float = 0.0
    quarantined_count: int = 0
    admitted_count: int = 0
    dead_types: list[str] = Field(default_factory=list)
    gates_evaluated: bool = False
    """False when the scope holds fewer than ``MemoryHealthPolicy.min_instances``
    facts — share and entropy are meaningless on a handful of rows, so the
    thresholds are not applied and no trip is recorded."""
    trips: list[MemoryHealthGateTrip] = Field(default_factory=list)

    entity_label_counts: dict[str, int] = Field(default_factory=dict)
    """WS-27 T4: context-visible NODE counts per entity LABEL (Concept,
    System, Credential, ...) — a DIFFERENT population from
    ``per_type_counts`` above, which counts RELATIONSHIPS by ``memory_type``.
    Empty (the default) when the caller supplies no label counts, which also
    keeps ``general_label_share``/``label_gates_evaluated`` at their inert
    defaults — existing callers of ``compute_memory_health`` that never pass
    label counts get a byte-identical report."""
    general_label_share: float = 0.0
    """Combined share of ``entity_label_counts`` held by
    ``MemoryHealthPolicy.general_labels`` (e.g. ``Concept``) — the direct
    reading of "how much of this corpus fell into the catch-all label"."""
    general_labels_observed: list[str] = Field(default_factory=list)
    """Which of ``MemoryHealthPolicy.general_labels`` actually appeared in
    ``entity_label_counts`` for this scope."""
    label_gates_evaluated: bool = False
    """False when ``entity_label_counts`` is empty or its total is below
    ``MemoryHealthPolicy.min_instances`` — mirrors ``gates_evaluated`` but for
    the label-share axis, which has its own population size."""

    @property
    def blocked(self) -> bool:
        return any(trip.severity == "block" for trip in self.trips)

    @property
    def warned(self) -> bool:
        return any(trip.severity == "warn" for trip in self.trips)


class MemoryHealthGateError(RuntimeError):
    """WS-24: a formation run produced a degenerate type distribution.

    Raised at the end of the run rather than swallowed, because the entire
    point of the health gates is that a collapsed graph must not be a silent
    outcome.  The full :class:`MemoryHealthReport` rides on ``.report`` so a
    caller can receipt, display, or re-run against a loosened policy without
    re-deriving the numbers.
    """

    def __init__(self, report: MemoryHealthReport) -> None:
        self.report = report
        blocking = [trip for trip in report.trips if trip.severity == "block"]
        detail = "; ".join(f"{trip.gate}={trip.observed:.3f} (threshold {trip.threshold})" for trip in blocking)
        super().__init__(f"memory health gate blocked formation for scope {report.scope.key}: {detail}")


class MemoryEvolutionFact(BaseModel):
    relationship_uuid: str
    relationship_type: str
    fact: str
    scope: MemoryScope
    subject: str
    predicate: str
    object: str
    confidence: float
    status: RelationshipStatus
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    episode_uuids: list[str] = Field(default_factory=list)
    observed_count: int = 1
    created_by: str
    superseded_by_relationship_uuid: str | None = None
    pruned_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_type: str | None = None


class MemoryEvolutionSignal(BaseModel):
    name: str
    observed: bool
    count: int = 0
    evidence: str


class MemoryEvolutionProof(BaseModel):
    scope: MemoryScope
    as_of: datetime
    episode_count: int = 0
    processed_episode_count: int = 0
    pending_episode_count: int = 0
    dream_run_count: int = 0
    decision_count: int = 0
    created_relationship_count: int = 0
    reinforced_relationship_count: int = 0
    superseded_relationship_count: int = 0
    pruned_relationship_count: int = 0
    consolidation_relationship_count: int = 0
    active_relationship_count: int = 0
    inactive_relationship_count: int = 0
    active_facts: list[MemoryEvolutionFact] = Field(default_factory=list)
    inactive_facts: list[MemoryEvolutionFact] = Field(default_factory=list)
    signals: list[MemoryEvolutionSignal] = Field(default_factory=list)

    # WS-6: Growth governance & proof — additive fields (safe defaults; backward-compat).
    context_visible_relationship_count: int = 0
    """Count of context-visible active relationships.

    Context-visible active (WS-4 counting recipe):
        status == ACTIVE
        AND validity window passes (valid_to is None or valid_to > as_of)
        AND properties.get("active_in_context") is not False
        AND type != "MENTIONS"

    Equals active_relationship_count when no WS-4 demotion has occurred
    (guaranteed invariant for all existing tests and the offline simulation).
    """

    rollup_relationship_count: int = 0
    """Count of context-visible active relationships whose memory_type == "rollup".

    A rollup is a WS-4 rollup consolidation node — a relationship synthesized from
    a cluster of member facts.  Zero when WS-4 rollup consolidation has not run.
    """

    demoted_relationship_count: int = 0
    """Count of WS-4 demoted members: status == ACTIVE but active_in_context == False.

    These are queryable via evidence and as_of retrieval but excluded from default
    profile context.  Zero when no rollup consolidation demotion has occurred.
    """

    compression_ratio: float = 0.0
    """Ratio of raw episodes to context-visible active facts.

    Definition: episode_count / max(1, context_visible_relationship_count).

    Interpretation:
        ≈ 1.0  → one episode per active fact (no compression yet; typical for a
                  fresh scope or when episodes exactly match materialized facts).
        > 1.0  → raw episodes have been compressed (via dedup/reinforcement/supersession/
                  demotion) into fewer context-visible facts — the healthy steady state.
        < 1.0  → more active facts than raw episodes, which can happen when a scope
                  has many add_memory() direct writes without episode records.

    In the existing offline simulation (no demotions, episodes > active facts) this
    is guaranteed to be >= 1.0 by the existing simulation proof contract.
    """

    semantic_dedup_rate: float = 0.0
    """Fraction of memory observations that reinforced an existing row rather than creating a new one.

    Definition:
        Let total_observations = sum(observed_count for all active + inactive facts).
        Let unique_facts = total number of created relationships (created_relationship_count).
        reinforced_observations = total_observations - unique_facts  (each row contributes
            at least 1 "first observation"; additional observed_count increments are reinforcements).
        semantic_dedup_rate = reinforced_observations / max(1, total_observations).

    Ranges from 0.0 (no reinforcement — every observation created a new row) to approaching
    1.0 (almost every observation reinforced an existing row).

    Note: this counts ALL reinforcements (exact-string and semantic), not only WS-1 semantic
    reinforcements, because the graph does not currently distinguish which path triggered the
    increment.  This is the correct and auditable definition given the current schema.
    """

    per_type_active_counts: dict[str, int] = Field(default_factory=dict)
    """Context-visible active counts keyed by memory_type value (e.g. "requirement", "directive").

    Multi-tenant dashboard primitive: lets operators see how many active context-visible facts
    each memory type contributes for this scope.  Only types with ≥1 context-visible active fact
    appear as keys; types with zero active facts are omitted.

    Uses the WS-4 context-visible active counting recipe (same as context_visible_relationship_count).
    """

    # WS-22 T28: token-savings measurement vs. the raw-episode baseline — all
    # counted with the platform's single deterministic estimator
    # (``Memotron._estimate_tokens``, ceiling 4 chars/token), decrypt-on-read.
    tokens_raw_episodes: int = 0
    """Estimator tokens over the scope's raw episode bodies — the no-memory
    baseline (what re-reading every raw transcript/document would cost).
    Crypto-shredded bodies contribute their placeholder length."""

    tokens_unbudgeted_facts: int = 0
    """Estimator tokens of ALL context-visible facts rendered in the profile
    line format with no budget and no count caps — the cost of injecting the
    whole working set."""

    tokens_rendered_profile: int = 0
    """Estimator tokens of the actual default profile render for this scope at
    ``memory_evolution(token_budget=...)`` (None = unbudgeted render of the
    CURRENT visible set under the default count caps).  0 when the scope has no
    context-visible facts (an empty scope renders no working context)."""

    tokens_saved_vs_raw: int = 0
    """``max(0, tokens_raw_episodes - tokens_rendered_profile)`` — tokens saved
    per task by injecting the evolved profile instead of the raw episodes."""

    tokens_saved_by_demotion: int = 0
    """Tokens the two-tier read saves via demotion: the summed profile-line
    tokens of ACTIVE-but-demoted rows (rollup members + cross-prefix duplicates)
    MINUS the line tokens of their distinct rollup/survivor replacements,
    floored at 0."""

    # WS-22 T29: repeat-search / answered-from-profile telemetry — event-plane
    # and rebuildable (computed from use events exactly like the utility
    # projection reads them).  ``None`` means "not measured" (denominator 0);
    # a measured 0.0 is a real observation, never a placeholder.
    repeat_search_rate: float | None = None
    """Over the scope's RETRIEVED use events carrying a ``query_digest``: the
    fraction of DISTINCT query digests that were searched under MORE than one
    session boundary (``session_boundary_for_task_run``).  The value-prop (b)
    signal — this falling across sessions means agents stop re-discovering the
    same answers.  None when no digest-stamped searches exist."""

    answered_from_profile_rate: float | None = None
    """Over the scope's CITED_OR_USED events: the fraction whose originating
    use event (same relationship, same session boundary — the join the WS-15
    citation scanner recorded) was INJECTED (profile) rather than RETRIEVED
    (search).  A fact both injected and retrieved in the session counts as
    profile (the profile already held it; no tool call was needed).  Citations
    with no resolvable INJECTED/RETRIEVED origin are excluded from the
    denominator; None when nothing resolves."""

    # WS-24: type-distribution health.  Deliberately on the evolution proof and
    # not on a side channel — "is this memory healthy?" is the same question
    # this proof already answers, and a degenerate distribution is the failure
    # that silently ruins every other number on it.
    health: MemoryHealthReport | None = None
    """Max type share, normalized type entropy, abstain rate, dead types, and
    every threshold trip for this scope.  ``None`` only when the health policy
    is disabled."""

    # WS-28 T4: context-pollution accounting.  Event-plane and rebuildable
    # (INJECTED/RETRIEVED use events joined to CITED_OR_USED, same session-
    # boundary join `answered_from_profile_rate` already uses), token-weighted
    # with the platform's single deterministic estimator.  ``None`` means
    # "not measured" (no injected tokens in-window) — never a fake ``0.0``.
    injected_waste_rate: float | None = None
    """Token-weighted share of this scope's INJECTED impressions that were
    never cited: ``tokens(injected AND NOT cited) / tokens(injected)`` over
    (session boundary, relationship) pairs.  A fact injected in five
    sessions and cited in one contributes waste for the other four —
    this is the direct, measured cost of a bloated context brief.  ``None``
    when the scope has no INJECTED use events."""
    injected_waste_rate_by_type: dict[str, float] = Field(default_factory=dict)
    """``injected_waste_rate`` partitioned by ``memory_type``.  Only types
    with at least one INJECTED impression appear as keys."""
    injected_waste_rate_by_role: dict[str, float] = Field(default_factory=dict)
    """``injected_waste_rate`` partitioned by :class:`memotron.context.ContextRole`
    value (``standing`` / ``recent`` / ``structure``).  Only roles with at
    least one INJECTED impression appear as keys.  This is the partition
    ``ContextPolicy.responsive_to()`` (WS-28 T4) consumes to shrink a
    wasteful role's budget share."""
