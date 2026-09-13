"""Return types for the SDK's write and read operations.

One result model per public entry point, plus the search result set. SearchResults
subclasses list[SearchResult] and is the package's only true ordering constraint --
layer.py reports it as the single non-enum hard edge -- so the two must share a file."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from memotron.models._enums import (
    ArchivedMatchDisposition,
    MemoryIngestionMode,
    RelationshipStatus,
)
from memotron.models._graph import (
    MemoryScope,
)


class AddEpisodeResult(BaseModel):
    episode_uuid: str
    queued_for_dreaming: bool
    memory_mode: MemoryIngestionMode = MemoryIngestionMode.CLIENT_MANAGED


class AddMemoryResult(BaseModel):
    episode_uuid: str
    relationship_uuid: str
    scope: MemoryScope
    fact: str
    created_relationships: int = 0
    reinforced_relationships: int = 0
    superseded_relationships: int = 0
    queued_for_dreaming: bool = False
    memory_mode: MemoryIngestionMode = MemoryIngestionMode.CLIENT_MANAGED


class AddContextResult(BaseModel):
    document_id: str
    scope_keys: list[str]
    chunks_created: int
    episodes_created: int
    episode_uuids: list[str]
    queued_for_dreaming: bool
    memory_mode: MemoryIngestionMode = MemoryIngestionMode.MANAGED_DREAMING


class AddArtifactResult(BaseModel):
    """WS-9: Result of ingesting a multimodal artifact via ``add_artifact``.

    Fields mirror ``AddContextResult`` but add artifact-specific provenance.
    The ``artifact_id`` is the stable identifier for the source artifact;
    ``episode_uuids`` are the queued episodes carrying the normalised text.
    """

    artifact_id: str
    """Stable identifier for the source artifact (caller-supplied or auto-UUID)."""
    artifact_type: str
    """Modality of the ingested artifact (e.g. ``"image"``, ``"code"``)."""
    artifact_location: str
    """Source coordinate string (file path + line range, URL, filename, …)."""
    normalized_text_length: int
    """Character count of the normalised text representation."""
    episode_uuids: list[str]
    """UUIDs of the queued episodes (one per scope)."""
    scope_keys: list[str]
    queued_for_dreaming: bool = True
    memory_mode: MemoryIngestionMode = MemoryIngestionMode.MANAGED_DREAMING


class ConversationTurn(BaseModel):
    role: str = Field(min_length=1)
    content: str
    timestamp: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("role")
    @classmethod
    def normalize_role(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("role cannot be blank")
        return normalized


class AddSessionResult(BaseModel):
    session_id: str
    scope_keys: list[str]
    turns_ingested: int
    windows_created: int
    episodes_created: int
    episode_uuids: list[str]
    queued_for_dreaming: bool
    memory_mode: MemoryIngestionMode = MemoryIngestionMode.MANAGED_DREAMING


class DreamContextMemory(BaseModel):
    relationship_uuid: str
    relationship_type: str
    fact: str
    scope: MemoryScope
    status: RelationshipStatus
    subject: str
    predicate: str
    object: str
    confidence: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    observed_count: int = 1
    episode_uuids: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    memory_type: str | None = None


class SearchResult(BaseModel):
    relationship_uuid: str
    fact: str
    scope: MemoryScope
    scope_rank: int = 0
    subject: str
    predicate: str
    object: str
    relationship_type: str = ""
    confidence: float
    status: RelationshipStatus = RelationshipStatus.ACTIVE
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    episode_uuid: str
    episode_uuids: list[str] = Field(default_factory=list)
    observed_count: int = 1
    score: float = 0.0
    """WS-5: Final rerank score (relevance + confidence + recency + scope
    priority, type-weighted).  Higher is better; comparable within one search."""
    relevance: float = 0.0
    """Stage-2/4 query relevance in [0, 1] before quality terms."""
    origin: str = "direct"
    """How the result entered the candidate set: ``direct``, ``seed``,
    ``entity_hop``, ``rollup_member``, ``member_rollup``, or ``pinned``
    (WS-20 T23 guaranteed-inclusion sweep)."""
    hops: int = 0
    """Expansion distance from a direct match or seed (0 = direct)."""
    retrieval_policy_digest: str = ""
    """Digest of the RetrievalContract that produced this result — stamp it
    onto use events so retrieval is replayable and certifiable."""
    pinned: bool = False
    """WS-20 T23: True when the row carries an operator pin.  Pinned rows that
    pass stage-1/5 visibility are always included in the final result list —
    reserved slots ahead of the score-ranked fill — so callers can see WHY a
    low-scoring result outranked higher-scoring ones."""


class ArchivedMemoryMatch(BaseModel):
    """One archived (pruned) row whose prune ghost matches a live query.

    Whether the row was revived by the read that reported it depends on the
    retrieval mode — see ``revived`` and
    :class:`ArchivedMatchDisposition`.  Either way it can be revived
    explicitly with :meth:`memotron.Memotron.restore_archived_memory`
    (MCP: ``memory_restore``).
    """

    relationship_uuid: str
    scope: MemoryScope
    fact: str
    prune_receipt_uuid: str
    pruned_at: datetime
    pruned_reason: str
    restorable: bool = True
    retrieval_mode: str = "keyword"
    """``"keyword"`` or ``"semantic"`` — which retrieval path matched the ghost."""
    revived: bool = False
    """True when the read that reported this row also revived it.

    True in the default mode (revive-on-read), False when
    ``DreamConfig.pure_read_retrieval`` is on, where the row is reported as
    available to restore and nothing is written.
    """


class ArchivedMatchReport(BaseModel):
    """Report of the archived memories a retrieval query matched.

    Additive in both retrieval modes: a caller that ignores this report sees
    exactly the retrieval it has always seen.  ``disposition`` says which mode
    produced it — ``revived`` (the default: these rows were revived by the read
    and are in ``results`` too) or ``available_to_restore``
    (``DreamConfig.pure_read_retrieval`` is on: these rows were left alone and
    are *not* in ``results``).
    """

    query: str = ""
    scope_keys: list[str] = Field(default_factory=list)
    retrieval_mode: str = "keyword"
    count: int = 0
    matches: list[ArchivedMemoryMatch] = Field(default_factory=list)
    disposition: ArchivedMatchDisposition = ArchivedMatchDisposition.REVIVED
    """Whether ``matches`` were revived by the read or are merely available."""
    restore_action: str = "restore_archived_memory"
    """Name of the explicit curation action that revives one of ``matches``."""


class SearchResults(list[SearchResult]):
    """``list[SearchResult]`` that also carries the archived-match report.

    This is a plain list subclass so every existing caller — indexing, slicing,
    iteration, ``len()``, pydantic ``list[SearchResult]`` fields — keeps working
    byte-for-byte.  ``archived`` is the additive channel through which a read
    reports the archived rows it matched: revived by default, or merely
    available to restore when ``DreamConfig.pure_read_retrieval`` is on.
    """

    __slots__ = ("archived",)

    def __init__(
        self,
        results: list[SearchResult] | tuple[SearchResult, ...] = (),
        *,
        archived: ArchivedMatchReport | None = None,
    ) -> None:
        super().__init__(results)
        self.archived = archived if archived is not None else ArchivedMatchReport()


class RestoreArchivedMemoryResult(BaseModel):
    """Outcome of the explicit ``restore_archived_memory`` curation action."""

    relationship_uuid: str
    scope: MemoryScope
    fact: str
    previous_status: RelationshipStatus
    status: RelationshipStatus
    restored_at: datetime
    reason: str
    requested_by: str
    prune_receipt_uuid: str
    restore_receipt_uuid: str
    ghost_regret_rate: float = 0.0
    ghost_regret_alert: bool = False
