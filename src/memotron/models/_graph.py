"""Core graph primitives -- the scoped property-graph substrate.

Everything downstream is a projection of these five: a scope, an episode that
enters it, the nodes and relationships it materialises, and the candidate an
extractor proposes before materialisation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from memotron.models._enums import (
    ClaimMode,
    EpisodeType,
    ScopeKind,
)


class MemoryScope(BaseModel):
    kind: ScopeKind
    scope_id: str = Field(min_length=1)

    @field_validator("scope_id")
    @classmethod
    def normalize_scope_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("scope_id cannot be blank")
        return normalized

    @property
    def key(self) -> str:
        return f"{self.kind.value}:{self.scope_id}"

    @classmethod
    def from_key(cls, value: str) -> MemoryScope:
        """The exact inverse of :attr:`key`. Raises rather than guessing.

        Storage persists a scope as its ``kind:id`` string -- `key_principals`'
        ``default_scope_key`` is one -- so something has to turn it back into a typed
        scope before an authorization decision can read it. This lived only inside
        `admin_server/_parsing.py` and so was unreachable from anywhere that is not the
        admin HTTP surface; `parse_scope_key` now delegates here and keeps its name and
        its error text.

        **Every failure raises**, deliberately, and the messages are unchanged from that
        original so the admin surface's 400s read the same. A scope that cannot be parsed
        must not degrade to "no scope": on this codepath an absent scope means *no
        allowlist entry*, which widens access rather than denying it. `scope_id` is
        rejected blank by the field validator, and an unknown ``kind`` raises from
        :class:`ScopeKind` -- neither is caught here.
        """
        if ":" not in value:
            raise ValueError("scope must use kind:id format, for example customer:acme")
        raw_kind, scope_id = value.split(":", 1)
        if not scope_id.strip():
            raise ValueError("scope id cannot be blank")
        return cls(kind=ScopeKind(raw_kind.strip()), scope_id=scope_id.strip())


class Episode(BaseModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1)
    body: str
    source: EpisodeType
    source_description: str = ""
    scope: MemoryScope
    reference_time: datetime = Field(default_factory=lambda: datetime.now(UTC))
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)
    instruction_set: str = "default"


class GraphNode(BaseModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    labels: tuple[str, ...]
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class GraphRelationship(BaseModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    source_uuid: str
    target_uuid: str
    type: str
    properties: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class ExtractedMemory(BaseModel):
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object: str = Field(min_length=1)
    relationship_type: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    subject_label: str = "Entity"
    object_label: str = "Entity"
    subject_properties: dict[str, Any] = Field(default_factory=dict)
    object_properties: dict[str, Any] = Field(default_factory=dict)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    scope: MemoryScope | None = None
    instruction_id: str | None = None
    source_text: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    claim_mode: ClaimMode | None = None
    """Typed semantic intent of the candidate, resolved before materialization.

    ``None`` is accepted only at the public boundary for backwards-compatible
    callers; the extractor and materializer deterministically resolve it from
    the relationship's memory type before a graph row or receipt is written.
    """
    claim_mode_coerced_from: ClaimMode | None = None
    """The extractor-supplied ``claim_mode`` that validation had to overwrite.

    Set by ``InstructionalExtractor._validate_memory`` when the transport tagged
    a candidate with a claim mode its relationship type's deterministic
    ``memory_type`` does not permit (e.g. ``directive`` on a DECIDES/decision
    candidate).  Rather than discard the whole episode's candidate batch over
    one mismatched field, the field is coerced to the type-consistent default
    and the ORIGINAL value is preserved here so formation can receipt the
    transformation (``CANDIDATE_CLAIM_MODE_COERCED``) — no candidate
    transformation is invisible to the receipt ledger.  ``None`` means the
    extractor's claim mode survived untouched.

    Outside ``candidate_digest``'s field list on purpose: the digest binds the
    candidate as STORED (post-coercion), which is the form a counterfactual
    re-gate must reproduce; the pre-coercion value is provenance, carried in the
    coercion receipt instead."""
    subject_entity_ref: str | None = None
    """WS-17 T16b: canonical entity (from the prompt's ENTITY INVENTORY) the
    extractor resolved the subject mention to.  Optional; must travel with
    ``subject_link_confidence`` (both-or-neither, enforced at validation)."""
    subject_link_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """WS-17 T16b: the extractor's confidence in ``subject_entity_ref`` — the
    ``llm_confidence`` term of the composed entity link score."""
    object_entity_ref: str | None = None
    """WS-17 T16b: canonical entity the extractor resolved the object mention to."""
    object_link_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    """WS-17 T16b: the extractor's confidence in ``object_entity_ref``."""
    saves_step: str | None = None
    """WS-24: the step an agent skips by knowing this fact — the actionability gate.

    Memory is a shortcut for the next action, not a description of the world,
    so the extraction contract makes every candidate name the work it avoids
    ("the agent goes straight to that path instead of searching the repo").  A
    candidate that cannot name one is quarantined rather than materialized.

    ``None`` is what a transport that never saw the contract produces (the
    deterministic rule-based extractor, a promotion replay); the gate's
    ``missing_justification`` disposition decides what that means.  Carried on
    the materialized row so the justification is auditable next to the fact it
    admitted.
    """
    salience_score: float = Field(default=0.0, ge=0.0, le=1.0)
    """WS-2: salience score = recency × importance × relevance (Generative Agents formula).

    Attached during salience scoring before filter/cap; persisted in relationship properties
    as "salience_score" at materialization time.  Default 0.0 is benign — the no-op path
    (min_salience=0.0, max_memories_per_episode=None) passes all memories regardless of score.
    WS-6 will report on this field for growth governance.
    """

    @field_validator("relationship_type")
    @classmethod
    def normalize_relationship_type(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "_").upper()
        if not normalized:
            raise ValueError("relationship_type cannot be blank")
        return normalized
