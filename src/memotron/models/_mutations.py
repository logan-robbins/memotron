"""Results of operator mutations to stored memory.

Forget, pin, visibility and correction each return what they changed rather than
a bare boolean, because every one of them is invalidate-don't-delete: the row
survives with a closed validity window, and the caller needs the uuid to audit
it. The truth timeline is the same story read back over time."""

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


class ForgetMemoryResult(BaseModel):
    relationship_uuid: str
    previous_status: RelationshipStatus
    status: RelationshipStatus
    scope: MemoryScope
    valid_to: datetime | None = None
    pruned_reason: str
    pruned_at: datetime


class PinMemoryResult(BaseModel):
    """WS-20 T23: receipted per-fact pin / unpin state transition."""

    relationship_uuid: str
    scope: MemoryScope
    pinned: bool
    reason: str
    pinned_by: str
    pinned_at: datetime


class MemoryVisibilityResult(BaseModel):
    """WS-19 T22: receipted per-memory agent-allowlist state transition.

    ``visibility_agents`` is the allowlist now in force — ``None`` means the
    restriction was cleared and the row returns to scope-default visibility.
    Restriction is agent-plane only: reads that identify a calling agent
    (``reader_agent_id``) fail closed against the allowlist, while operator /
    SDK-owner reads (``reader_agent_id=None``) see everything.
    """

    relationship_uuid: str
    scope: MemoryScope
    visibility_agents: tuple[str, ...] | None = None
    reason: str
    set_by: str
    set_at: datetime


class CorrectMemoryResult(BaseModel):
    relationship_uuid: str
    corrected_relationship_uuid: str
    correction_episode_uuid: str
    previous_status: RelationshipStatus
    status: RelationshipStatus
    scope: MemoryScope
    valid_to: datetime | None = None
    correction_reason: str
    corrected_fact: str
    created_relationships: int = 0
    reinforced_relationships: int = 0
    superseded_relationships: int = 0


class TruthTimelineEntry(BaseModel):
    relationship_uuid: str
    relationship_type: str
    fact: str
    scope: MemoryScope
    subject: str
    predicate: str
    object: str
    confidence: float
    status: RelationshipStatus
    is_current: bool
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    episode_uuid: str
    episode_uuids: list[str] = Field(default_factory=list)
    observed_count: int = 1
    created_by: str
    superseded_by_relationship_uuid: str | None = None
    pruned_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
