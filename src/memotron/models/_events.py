"""Use and outcome events -- the utility feedback loop.

A memory is used, an outcome is judged against that use, and the pair projects
into a utility score. Retrieval negative space records the opposite: impressions
that never progressed to a use at all."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator, model_validator

from memotron.models._enums import (
    OutcomeAttributionMethod,
    OutcomeVerdict,
    UseEventKind,
)
from memotron.models._graph import (
    MemoryScope,
)


def session_boundary_for_task_run(task_run_id: str) -> str:
    """WS-22 T29: the session boundary one task-run id belongs to.

    ``adoption.session_task_run_id`` builds every Claude Code hook task-run id
    as ``claude:{session_id}:{source}:{timestamp}``; the stable
    ``claude:{session_id}:`` prefix is the session's whole event lineage — the
    exact prefix ``AgentMemoryPlatform._session_use_events`` (WS-15) reads
    back.  For ids of that shape this returns that prefix; any other id is its
    own boundary (one task run = one session).  Pure and deterministic — the
    single derivation used by the ``repeat_search_rate`` /
    ``answered_from_profile_rate`` event-plane metrics.
    """
    parts = task_run_id.split(":")
    if len(parts) >= 3 and parts[0] == "claude" and parts[1]:
        return f"claude:{parts[1]}:"
    return task_run_id


class UseEvent(BaseModel):
    """Immutable observation that a memory reached (or influenced) a runtime."""

    model_config = {"frozen": True}

    use_id: str = Field(default_factory=lambda: str(uuid4()))
    relationship_uuid: str = Field(min_length=1)
    scope: MemoryScope
    kind: UseEventKind
    used_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    task_run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    rank: int | None = Field(default=None, ge=0)
    retrieval_score: float | None = None
    candidate_set_size: int | None = Field(default=None, ge=0)
    context_budget_competition: int | None = Field(default=None, ge=0)
    retrieval_policy_digest: str | None = None
    query_digest: str | None = None
    """WS-22 T29: sha256 of the raw search query (``retrieval.query_digest``)
    for RETRIEVED events recorded by a search surface — the same digest the
    event's pinned RetrievalContract carries.  Optional and additive: events
    recorded before this field (or by non-search surfaces) have ``None``."""
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("used_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("UseEvent.used_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_impression_propensity_data(self) -> UseEvent:
        if self.kind in {UseEventKind.RETRIEVED, UseEventKind.INJECTED}:
            missing = [
                name
                for name, value in (
                    ("rank", self.rank),
                    ("retrieval_score", self.retrieval_score),
                    ("candidate_set_size", self.candidate_set_size),
                    ("context_budget_competition", self.context_budget_competition),
                    ("retrieval_policy_digest", self.retrieval_policy_digest),
                )
                if value is None or value == ""
            ]
            if missing:
                raise ValueError(f"{self.kind.value} use events require propensity fields: {', '.join(missing)}")
        return self


class OutcomeEvent(BaseModel):
    """Immutable task result linked to the specific use event it judges."""

    model_config = {"frozen": True}

    outcome_id: str = Field(default_factory=lambda: str(uuid4()))
    use_id: str = Field(min_length=1)
    scope: MemoryScope
    judged_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    verdict: OutcomeVerdict
    judge_identity: str = Field(min_length=1)
    judge_version: str = Field(min_length=1)
    task_run_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    attribution_method: OutcomeAttributionMethod = OutcomeAttributionMethod.CITED_FULL_CREDIT
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("judged_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("OutcomeEvent.judged_at must be timezone-aware")
        return value.astimezone(UTC)


class MemoryUtilityProjection(BaseModel):
    """Replay-rebuildable utility read model; never stored on the fact row."""

    relationship_uuid: str
    scope: MemoryScope
    use_stability: float = Field(default=0.0, ge=0.0)
    beta_positive: float = Field(default=1.0, gt=0.0)
    beta_negative: float = Field(default=1.0, gt=0.0)
    positive_outcome_count: int = Field(default=0, ge=0)
    negative_outcome_count: int = Field(default=0, ge=0)
    cited_or_used_count: int = Field(default=0, ge=0)
    impression_count: int = Field(default=0, ge=0)
    injected_count: int = Field(default=0, ge=0)
    last_used_at: datetime | None = None
    last_injected_at: datetime | None = None
    last_retrieved_at: datetime | None = None

    @property
    def outcome_quality(self) -> float:
        return self.beta_positive / (self.beta_positive + self.beta_negative)


class RetrievalNegativeSpaceEntry(BaseModel):
    """A retrieved memory for which no downstream selection/use was recorded."""

    use_event: UseEvent
    injected: bool = False
    cited_or_used: bool = False
    outcome_recorded: bool = False


class PruneGhost(BaseModel):
    """Tombstone for an archived relationship that can be restored from evidence."""

    relationship_uuid: str
    scope: MemoryScope
    prune_receipt_uuid: str
    pruned_at: datetime
    reason: str
    restorable: bool = True
    restored_at: datetime | None = None
