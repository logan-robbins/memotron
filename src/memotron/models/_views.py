"""Read-only projections of the graph, for humans and for agent context.

The entity neighbourhood and knowledge-graph views are visualisation shapes; the
profile is what actually gets injected into an agent's context window; the
evidence types are what let a fact be traced back to the episode that produced it."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from memotron.models._enums import (
    EpisodeType,
    RelationshipStatus,
)
from memotron.models._graph import (
    MemoryScope,
)


class EntityNeighborhoodEdge(BaseModel):
    relationship_uuid: str
    relationship_type: str
    direction: str
    fact: str
    scope: MemoryScope
    subject: str
    predicate: str
    object: str
    confidence: float
    status: RelationshipStatus
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    episode_uuid: str
    episode_uuids: list[str] = Field(default_factory=list)
    observed_count: int = 1
    created_by: str
    superseded_by_relationship_uuid: str | None = None
    pruned_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraphNode(BaseModel):
    id: str
    label: str
    node_type: str
    scope: MemoryScope | None = None
    graph_uuid: str | None = None
    relationship_uuid: str | None = None
    relationship_type: str | None = None
    memory_type: str | None = None
    status: RelationshipStatus | None = None
    active_in_context: bool = True
    confidence: float | None = None
    observed_count: int = 0
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    labels: tuple[str, ...] = Field(default_factory=tuple)
    properties: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraphEdge(BaseModel):
    id: str
    source_id: str
    target_id: str
    edge_type: str
    label: str
    relationship_uuid: str | None = None
    directed: bool = True
    properties: dict[str, Any] = Field(default_factory=dict)


class KnowledgeGraphView(BaseModel):
    scope: MemoryScope
    as_of: datetime | None = None
    nodes: list[KnowledgeGraphNode] = Field(default_factory=list)
    edges: list[KnowledgeGraphEdge] = Field(default_factory=list)
    relationship_count: int = 0
    memory_type_distribution: dict[str, int] = Field(default_factory=dict)
    status_distribution: dict[str, int] = Field(default_factory=dict)
    context_visible_relationship_count: int = 0
    demoted_relationship_count: int = 0
    rollup_relationship_count: int = 0


class MemoryProfileFact(BaseModel):
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
    last_seen_at: datetime | None = None
    episode_uuids: list[str] = Field(default_factory=list)
    observed_count: int = 1
    created_by: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_type: str | None = None
    pinned: bool = False
    """WS-20 T23: True when the row carries an operator pin.  Pinned facts are
    rendered first (``[PINNED]`` marker), bypass count caps, and consume the
    token budget before the score-ranked fill."""
    created_at: datetime | None = None
    """WS-20 T23: row creation time — the deterministic (created_at, uuid)
    ordering key for pinned-fact inclusion."""


class MemoryContextItem(BaseModel):
    """A runtime context item with the stable ID needed for use reporting."""

    relationship_uuid: str
    fact: str
    mode: str


class MemoryProfile(BaseModel):
    scope: MemoryScope
    as_of: datetime | None = None
    static_facts: list[MemoryProfileFact] = Field(default_factory=list)
    dynamic_facts: list[MemoryProfileFact] = Field(default_factory=list)
    rendered_context: str
    tokens_used: int = 0
    """WS-5: Approximate tokens consumed by rendered_context (4 chars/token estimator).
    Zero when no token_budget was requested (legacy callers unaffected)."""
    tokens_available: int | None = None
    """WS-5: The token_budget passed to profile(), or None when not budgeted."""
    injected_items: list[MemoryContextItem] = Field(default_factory=list)
    reference_items: list[MemoryContextItem] = Field(default_factory=list)


class EvidenceEpisode(BaseModel):
    uuid: str
    name: str
    body: str
    source: EpisodeType
    source_description: str
    scope: MemoryScope
    reference_time: datetime
    created_at: datetime
    metadata: dict[str, Any] = Field(default_factory=dict)
    instruction_set: str


class MemoryEvidence(BaseModel):
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
    source_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    episodes: list[EvidenceEpisode] = Field(default_factory=list)
    promotion_lineage: dict[str, Any] | None = None
    """WS-19 T20: present when this fact was materialized from an object-level
    promotion candidate — resolves the chain fact → candidate episode →
    ``source_relationship_uuid`` (plus source scope, the source row's pinned
    formation-receipt digest, the promoting agent, and every endorser)."""
