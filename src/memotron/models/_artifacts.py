"""Non-memory governing artifacts, and the behaviour contract they resolve into.

WS-10's premise: an agent's effective behaviour is governed by a heterogeneous
set of persistent artifacts -- memory, skills, files -- authored on different
cadences and carrying different execution precedence. These are the types that
let a skill and a memory be compared on the same terms."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from memotron.models._enums import (
    ArtifactClass,
    DirectiveStance,
)
from memotron.models._graph import (
    MemoryScope,
)


class ArtifactDirective(BaseModel):
    """WS-10: A single behavioral directive declared by a non-memory artifact.

    A skill or file is projected into one or more of these.  ``subject`` is the
    topic the directive is about (the axis on which cross-artifact identity is
    matched); ``instruction`` is the directive text; ``stance`` says how it
    relates to behavior on that subject.
    """

    subject: str = Field(min_length=1)
    instruction: str = Field(min_length=1)
    stance: DirectiveStance = DirectiveStance.GUIDE
    metadata: dict[str, Any] = Field(default_factory=dict)


class PersistentArtifact(BaseModel):
    """WS-10: A persistent, behavior-governing artifact outside the memory store.

    Registered by the caller via :meth:`Memotron.register_artifact_source` so
    the coherence cycle can see skills and files alongside memory.  ``updated_at``
    is the staleness signal used for culprit attribution; ``self_authored`` flags
    an artifact the agent wrote in a prior session (the classic stale-self-skill
    failure mode).
    """

    artifact_id: str = Field(min_length=1)
    artifact_class: ArtifactClass
    scope: MemoryScope
    directives: list[ArtifactDirective] = Field(default_factory=list)
    author: str = "unknown"
    location: str | None = None
    self_authored: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("created_at", "updated_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        # A file artifact's natural timestamp (e.g. datetime.fromtimestamp(mtime)) is
        # timezone-naive; coerce to UTC so coherence staleness comparisons against the
        # tz-aware memory timeline never raise on naive-vs-aware.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class ArtifactContributionProjection(BaseModel):
    """Rebuildable outcome-attributed contribution state for one live artifact."""

    scope: MemoryScope
    artifact_id: str
    artifact_class: ArtifactClass
    positive_outcomes: int = Field(default=0, ge=0)
    negative_outcomes: int = Field(default=0, ge=0)
    contribution_score: float = Field(default=0.0, ge=-1.0, le=1.0)
    minimum_evidence_met: bool = False
    quarantined: bool = False
    last_outcome_at: datetime | None = None


class CoherenceRepairMonitor(BaseModel):
    """Durable post-repair monitor for a reviewed governing artifact.

    The monitor carries the exact pre-repair artifact projection needed for an
    auditable registry rollback.  It never grants permission to rewrite the
    user's underlying file or skill source.
    """

    scope: MemoryScope
    incident_id: str = Field(min_length=1)
    relationship_uuid: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    prior_artifact_version_digest: str = Field(min_length=64, max_length=64)
    repaired_artifact_version_digest: str = Field(min_length=64, max_length=64)
    prior_artifact: PersistentArtifact
    status: str = "monitoring"
    opened_at: datetime
    recovered_at: datetime | None = None
    reopened_at: datetime | None = None


class Directive(BaseModel):
    """WS-10: The common cross-artifact representation of a behavioral directive.

    Every governing artifact — a typed memory relationship, a skill, a file note
    — is projected into this single shape so contradictions can be detected
    across artifact-class boundaries.  ``precedence`` is the execution precedence
    of the artifact class (higher wins at run time); ``observed_count`` carries
    the escalation-cycle count for memory directives (the anti-windup signal);
    ``embedding`` is the L2-normalised topic vector used for semantic identity.
    """

    directive_id: str = Field(default_factory=lambda: str(uuid4()))
    scope: MemoryScope
    subject: str
    instruction: str
    stance: DirectiveStance
    artifact_class: ArtifactClass
    artifact_id: str
    artifact_location: str | None = None
    author: str = "unknown"
    self_authored: bool = False
    precedence: int = 0
    updated_at: datetime | None = None
    observed_count: int = 1
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    relationship_uuid: str | None = None
    embedding: list[float] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("updated_at")
    @classmethod
    def _ensure_aware(cls, value: datetime | None) -> datetime | None:
        # Guarantee tz-aware so staleness comparisons (other.updated_at < memory.updated_at)
        # in the windup detector never compare naive vs aware.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value


class ResolvedBehaviorDirective(BaseModel):
    """One authority-resolved instruction for a native runtime message slot."""

    subject: str
    instruction: str
    stance: DirectiveStance
    artifact_class: ArtifactClass
    artifact_id: str
    precedence: int
    source_relationship_uuid: str | None = None


class ResolvedBehaviorContract(BaseModel):
    """Bounded runtime behavior projection, separate from ordinary memory text."""

    scope: MemoryScope
    native_message_slot: str
    directives: list[ResolvedBehaviorDirective] = Field(default_factory=list)
    native_message: str
    prior_output_data: str
    prior_output_count: int = 0
    omitted_prior_output_count: int = 0
    contract_digest: str


class FrequencyAuthorityTrial(BaseModel):
    """One measured runtime response in a frequency-versus-authority evaluation."""

    trial_index: int = Field(ge=0)
    response_digest: str = Field(min_length=64, max_length=64)
    compliant: bool


class FrequencyAuthorityEvaluation(BaseModel):
    """Measured gate proving a current directive wins over repeated stale outputs."""

    scope: MemoryScope
    contract_digest: str = Field(min_length=64, max_length=64)
    expected_artifact_id: str
    stale_exemplar_count: int = Field(ge=1)
    injected_prior_output_count: int = Field(ge=0)
    trial_count: int = Field(ge=1)
    compliant_count: int = Field(ge=0)
    compliance_rate: float = Field(ge=0.0, le=1.0)
    minimum_compliance_rate: float = Field(ge=0.0, le=1.0)
    passed: bool
    runtime_identifier: str
    judge_identifier: str
    trials: tuple[FrequencyAuthorityTrial, ...]
