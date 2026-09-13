"""Coherence incidents, quarantine, and the operator review queues.

Everything here exists because the system refuses to guess: when it cannot pick a
winner between contradictory claims, or cannot resolve an entity, or cannot admit
a candidate safely, it parks the decision as a typed item a human resolves --
rather than silently choosing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from memotron.models._artifacts import (
    Directive,
)
from memotron.models._enums import (
    CoherenceIncidentKind,
    CoherenceIncidentStatus,
    QuarantineStatus,
    RelationshipStatus,
)
from memotron.models._graph import (
    MemoryScope,
)


class CoherenceIncident(BaseModel):
    """WS-10: A detected incoherence across persistent artifact classes.

    Either an ``ESCALATION_WINDUP`` (a memory directive strengthened across many
    feedback cycles while a higher-precedence non-memory artifact silently
    governs the same subject) or a ``CROSS_ARTIFACT_CONTRADICTION`` (two artifact
    classes assert conflicting stances on the same subject).  ``governing_directive``
    is the attributed culprit; ``escalating_directive`` is the directive that is
    being fruitlessly reinforced or contradicted.
    """

    incident_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: MemoryScope
    kind: CoherenceIncidentKind
    status: CoherenceIncidentStatus = CoherenceIncidentStatus.OPEN
    subject: str
    summary: str
    escalating_directive: Directive | None = None
    governing_directive: Directive | None = None
    conflicting_directives: list[Directive] = Field(default_factory=list)
    identity_cosine: float = 0.0
    escalation_cycles: int = 0
    precedence_gap: int = 0
    attribution_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    attribution_rationale: str = ""
    proposed_repair: str = ""
    detected_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class CoherenceDisambiguationRequest(BaseModel):
    """Evidence request emitted when coherence attribution is below policy confidence.

    ``status`` is reconstructed from the audit log: ``"pending"`` until an operator
    records the requested evidence via ``resolve_disambiguation_request``, then
    ``"resolved"`` with the supplied evidence carried on the ``resolution_*`` fields.
    """

    incident_id: str = Field(min_length=1)
    scope: MemoryScope
    subject: str
    requested_at: datetime
    attribution_confidence: float = Field(ge=0.0, le=1.0)
    minimum_confidence: float = Field(ge=0.0, le=1.0)
    governing_artifact_id: str | None = None
    required_evidence: tuple[str, ...]
    status: str = "pending"
    resolution_runtime_trace: str | None = None
    resolution_artifact_version: str | None = None
    resolution_statement: str | None = None
    resolved_by: str | None = None
    resolved_at: datetime | None = None


class SupersessionReviewItem(BaseModel):
    """WS-16 T14: one gate-parked challenger awaiting human adjudication.

    A challenger row inserted pre-SUPERSEDED by the supersession gate
    (``requires_operator_review`` with no ``review_resolution`` yet).
    ``current_incumbent_uuid`` is the ACTIVE row the challenger disputes: the
    active row on the same ``truth_key`` when one exists (single-active slots),
    otherwise the still-active row the challenger was parked against
    (multi-active polarity parks); ``None`` when neither survives.
    """

    relationship_uuid: str
    scope: MemoryScope
    fact: str
    truth_key: str
    gate_reason: str
    source_authority: str
    created_at: datetime
    current_incumbent_uuid: str | None = None
    challenger_confidence: float = 0.0


class QuarantinedCandidate(BaseModel):
    """WS-24: one extraction candidate the actionability gate kept out of the graph.

    Quarantine is the real abstain path.  Before it, every candidate had to
    pick a memory type, so a corpus with nothing actionable in it still
    produced a graph — the catch-all type simply absorbed the corpus.  A
    quarantined candidate is retained, receipted, inspectable, and promotable
    by an operator, but it is NOT context-visible, NOT retrievable by default,
    and carries no budget claim: it lives in its own store, outside the
    relationship plane entirely.
    """

    candidate_uuid: str
    scope: MemoryScope
    episode_uuid: str | None = None
    reason: str
    """Machine violation/abstain code — content-free by construction."""
    detail: str = ""
    """Human-readable reason.  May quote model-authored text, so it decrypts on
    read like any other content-plane field."""
    saves_step: str | None = None
    subject: str = ""
    predicate: str = ""
    object: str = ""
    proposed_relationship_type: str | None = None
    proposed_memory_type: str | None = None
    candidate_digest: str = ""
    instruction_set: str | None = None
    motive_name: str | None = None
    quarantined_at: datetime
    status: QuarantineStatus = QuarantineStatus.QUARANTINED
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str | None = None
    promoted_relationship_uuid: str | None = None


class QuarantineResolution(BaseModel):
    """WS-24: outcome of one operator adjudication of a quarantined candidate."""

    candidate_uuid: str
    scope: MemoryScope
    decision: str
    status: QuarantineStatus
    promoted_relationship_uuid: str | None = None
    resolved_by: str
    resolved_at: datetime
    reason: str


class SupersessionReviewResolution(BaseModel):
    """WS-16 T14: outcome of one operator adjudication of a parked challenger."""

    relationship_uuid: str
    scope: MemoryScope
    decision: str
    review_resolution: str
    status: RelationshipStatus
    superseded_incumbent_uuids: list[str] = Field(default_factory=list)
    reason: str
    resolved_by: str
    resolved_at: datetime


class CoherenceIncidentResolution(BaseModel):
    """WS-16 T14: durable operator status transition of a coherence incident."""

    incident_id: str
    scope: MemoryScope
    previous_status: CoherenceIncidentStatus
    status: CoherenceIncidentStatus
    statement: str
    resolved_by: str
    resolved_at: datetime


class EntityAliasProposal(BaseModel):
    """WS-17 T16b: one mid-band entity alias awaiting human adjudication.

    A 'proposed' registry row: the mention scored inside
    ``[review_threshold, auto_link_threshold)`` so it materialized under its
    surface name while this proposal parks for review.
    ``sample_fact_uuids`` are up to five ACTIVE relationships in the scope that
    mention the alias surface (deterministic order) — the evidence a reviewer
    inspects before approving.
    """

    name: str
    scope: MemoryScope
    canonical_name: str
    link_score: float | None = None
    link_signals: dict[str, Any] = Field(default_factory=dict)
    proposed_at: datetime
    sample_fact_uuids: list[str] = Field(default_factory=list)


class EntityAliasResolution(BaseModel):
    """WS-17 T16b: outcome of one operator adjudication of an alias proposal."""

    name: str
    scope: MemoryScope
    canonical_name: str
    decision: str
    status: str
    link_score: float | None = None
    backfill_rewritten_count: int = 0
    reason: str
    resolved_by: str
    resolved_at: datetime


class CoherenceReport(BaseModel):
    """WS-10: Result of one cross-artifact coherence scan over a scope."""

    scope: MemoryScope
    ran_at: datetime
    artifact_count: int = 0
    directive_count: int = 0
    per_class_directive_counts: dict[str, int] = Field(default_factory=dict)
    incidents: list[CoherenceIncident] = Field(default_factory=list)
    escalation_windup_count: int = 0
    contradiction_count: int = 0

    @property
    def incident_count(self) -> int:
        return len(self.incidents)


class ScopeRemediationResult(BaseModel):
    """WS-10: per-scope outcome of a coherence remediation sweep."""

    scope: MemoryScope
    report: CoherenceReport
    held_relationship_uuids: list[str] = Field(default_factory=list)
    retired_relationship_uuids: list[str] = Field(default_factory=list)

    @property
    def windup_count(self) -> int:
        return self.report.escalation_windup_count

    @property
    def contradiction_count(self) -> int:
        return self.report.contradiction_count


class CoherenceRemediationReport(BaseModel):
    """WS-10: aggregate result of a fleet-wide coherence remediation sweep.

    ``dry_run`` distinguishes a preview (no holds written, no decisions recorded,
    no directives retired) from an applied sweep. The per-scope ``report`` carries
    the full incidents and their proposed repairs for operator review.
    """

    ran_at: datetime
    dry_run: bool = True
    scopes: list[ScopeRemediationResult] = Field(default_factory=list)

    @property
    def scope_count(self) -> int:
        return len(self.scopes)

    @property
    def affected_scope_count(self) -> int:
        return sum(1 for s in self.scopes if s.report.incident_count > 0)

    @property
    def total_incidents(self) -> int:
        return sum(s.report.incident_count for s in self.scopes)

    @property
    def total_windup(self) -> int:
        return sum(s.windup_count for s in self.scopes)

    @property
    def total_contradiction(self) -> int:
        return sum(s.contradiction_count for s in self.scopes)

    @property
    def total_held(self) -> int:
        return sum(len(s.held_relationship_uuids) for s in self.scopes)

    @property
    def total_retired(self) -> int:
        return sum(len(s.retired_relationship_uuids) for s in self.scopes)
