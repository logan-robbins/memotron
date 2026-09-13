"""What a Memotron concern mixin may assume the composed client provides.

Fifth of these, and the last. **56 cross-boundary members over 183 methods (31%)** --
between DreamEngine's 61/152 (40%) and AgentMemoryPlatform's 17/63 (27%).

That ordering is not arbitrary and it is the clearest evidence this branch has about
where coupling comes from. `AgentMemoryPlatform` holds no storage handle and is the
loosest. `DreamEngine` holds `self._graph` and every one of its mixins touches it, and
it is the tightest. `Memotron` holds `self.graph` too -- 279 call sites over 72
distinct backend members, more than all ten dreaming mixins combined -- and lands in
between only because its concerns are wider and its clusters cleaner.

**Coupling here tracks the shared mutable substrate, not class size.**

Generated from the measured call graph, not hand-listed: a member appears only if
some OTHER concern calls it. Two assignments are deliberate rather than positional:

  the visibility helpers (`_agent_may_view_relationship`, `_relationship_is_visible`,
    `_relationship_window_contains`) sit on the COMPOSER, because each is called from
    more than one concern -- R-A2 -- and leaving them where their line numbers put
    them closed a three-module cycle;
  `_coherence` is MERGED into `_lifecycle`, because `forget_memory` and
    `run_coherence_scan` call each other: remediating coherence forgets a memory and
    forgetting triggers a scan. One concern cut at an arbitrary line.

Both were measured before the first cut. Result: 14 groups, ZERO cycles.

THE ONE MEMBER TO BE CAREFUL WITH
`_require_authorized_scope` has 65 callers -- more in-edges than any member in any
package this branch has split. It stays on the composer and no mixin may define it.
If it is ever shadowed, the failure is a SILENT AUTHORISATION BYPASS, not a crash:
a wrong-but-valid scope returns an empty result rather than an error.

Usage, as everywhere else -- `_Base = object` at runtime, so the MRO is unchanged.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    import memotron.context as context_module
    from memotron.agents import DreamAgentTransport
    from memotron.coherence import ArtifactSource
    from memotron.config import (
        CoherencePolicy,
        DreamConfig,
        EffectiveMemoryPolicy,
        GovernancePolicy,
        MemoryControlPlane,
        Motive,
        ProfilePolicy,
    )
    from memotron.dreaming import (
        DreamEngine,
    )
    from memotron.embedding import EmbeddingTransport
    from memotron.models import (
        AddEpisodeResult,
        AddMemoryResult,
        ArchivedMatchReport,
        CoherenceIncident,
        CoherenceIncidentStatus,
        CoherenceReport,
        DreamJobRun,
        DreamJobRunRecord,
        DreamRunResult,
        Episode,
        EvidenceEpisode,
        MemoryEvolutionFact,
        MemoryEvolutionProof,
        MemoryEvolutionSignal,
        MemoryProfile,
        MemoryScope,
        RelationshipStatus,
    )
    from memotron.replay import (
        CounterfactualReport,
        MotiveCertificate,
        ReplayProof,
    )
    from memotron.storage import StorageBackend
    from memotron.storage.receipts import (
        NegativeSpaceEntry,
        ReceiptDecisionType,
        ReceiptRun,
    )
    from memotron.synthesis import SynthesisTransport


class ComposedMemotron(Protocol):
    """The composed :class:`Memotron` surface, as one mixin sees it."""

    # -- state owned by the composer, set in __init__ (which never moves, R-A1) --
    _artifact_sources: list[ArtifactSource]
    _dream_agent_transport: DreamAgentTransport | None
    _embedding_transport: EmbeddingTransport | None
    _engine: Any
    _extractor: Any
    authorized_scope_keys: frozenset[str] | None
    config: Any
    control_plane: MemoryControlPlane | None
    graph: StorageBackend
    rollup_synthesis_transport: SynthesisTransport | None

    # -- kept on the composer, per R-A2 --------------------------
    @staticmethod
    def _agent_may_view_relationship(properties: dict[str, Any], reader_agent_id: str | None) -> bool: ...
    def _begin_operator_run(self, *, job_name: str, scope_key: str) -> ReceiptRun: ...
    def _check_scope_not_read_only(self, scope: MemoryScope) -> None: ...
    def _chunk_context(self, content: str, max_chars: int) -> list[str]: ...
    def _coherence_policy_for_scope(self, scope: MemoryScope) -> CoherencePolicy: ...
    def _context_episode_name(self, *, name: str, scope: MemoryScope, chunk_index: int, chunk_count: int) -> str: ...
    def _corrected_relationship(
        self,
        *,
        scope: MemoryScope,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        correction_episode_uuid: str,
    ): ...
    def _correction_valid_to(self, *, target_valid_to: datetime | None, correction_time: datetime) -> datetime: ...
    def _datetime_sort_value(self, value: datetime | None) -> float: ...
    def _edge_direction(self, *, source_matches: bool, target_matches: bool) -> str: ...
    def _emit_receipt(
        self,
        receipt_run: ReceiptRun,
        *,
        decision_type: ReceiptDecisionType,
        decision_reason: str,
        decision_result: str,
        episode: Episode | None = ...,
        now: datetime | None = ...,
        **fields: Any,
    ): ...
    def _evidence_episode(self, episode: Episode) -> EvidenceEpisode: ...
    def _forget_valid_to(
        self, *, status: RelationshipStatus, valid_to: datetime | None, pruned_at: datetime
    ) -> datetime | None: ...
    def _materialized_memory_relationship(
        self,
        *,
        scope: MemoryScope,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        episode_uuid: str,
    ): ...
    def _node_search_text(self, properties: dict[str, Any]) -> str: ...
    def _normalized_relationship_type(self, relationship_type: str) -> str: ...
    def _normalized_relationship_types(self, relationship_types: set[str] | None) -> set[str] | None: ...
    def _optional_string(self, value: Any) -> str | None: ...
    def _receipt_utility_auto_enabled(
        self, *, scope: MemoryScope, event_volume: int, floor: int, resolved_weight: float, now: datetime
    ) -> None: ...
    def _relationship_entity_label(self, labels: tuple[str, ...]) -> str: ...
    def _relationship_episode_uuids(self, properties: dict[str, Any]) -> list[str]: ...
    def _relationship_is_visible(
        self,
        *,
        status: RelationshipStatus,
        valid_from: datetime | None,
        valid_to: datetime | None,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
    ) -> bool: ...
    def _relationship_metadata(self, properties: dict[str, Any]) -> dict[str, Any]: ...
    def _relationship_scope(self, properties: dict[str, Any]) -> MemoryScope: ...
    def _relationship_status(self, raw_status: Any) -> RelationshipStatus: ...
    def _relationship_status_rank(self, status: RelationshipStatus) -> int: ...
    def _relationship_window_contains(
        self, *, valid_from: datetime | None, valid_to: datetime | None, instant: datetime
    ) -> bool: ...
    def _require_authorized_scope(self, *scopes: MemoryScope | None) -> None: ...
    def _require_explicit_authorized_scope(self, scope: MemoryScope | None, *, operation: str) -> None: ...
    async def _run_job_and_record(
        self,
        *,
        job_name: str,
        now: datetime,
        engine: DreamEngine | None = ...,
        config: DreamConfig | None = ...,
        effective_policy: EffectiveMemoryPolicy | None = ...,
    ) -> DreamJobRun: ...
    def _same_fact(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        corrected_subject: str,
        corrected_predicate: str,
        corrected_object: str,
        corrected_relationship_type: str,
    ) -> bool: ...
    def _store_episode(self, episode: Episode) -> Episode: ...

    # -- contributed by _archive ---------------------------------
    def _empty_archived_report(
        self, *, query: str, scopes: list[MemoryScope], semantic: bool
    ) -> ArchivedMatchReport: ...
    async def _retrieval_archived_report(
        self, *, query: str, scopes: list[MemoryScope], semantic: bool, reader_agent_id: str | None = ...
    ) -> ArchivedMatchReport: ...

    # -- contributed by _crypto ----------------------------------
    def _ingest_governance(self, metadata: dict[str, Any]) -> GovernancePolicy | None: ...

    # -- contributed by _governance ------------------------------
    def _entity_alias_group_names(self, scope_key: str, entity: str) -> frozenset[str]: ...

    # -- contributed by _ingest ----------------------------------
    async def add_episode_bulk(self, episodes: list[Episode]) -> list[AddEpisodeResult]: ...
    async def add_memory(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        relationship_type: str,
        scope: MemoryScope,
        confidence: float = ...,
        name: str | None = ...,
        source_description: str = ...,
        reference_time: datetime | None = ...,
        valid_from: datetime | None = ...,
        valid_to: datetime | None = ...,
        subject_label: str = ...,
        object_label: str = ...,
        subject_properties: dict[str, Any] | None = ...,
        object_properties: dict[str, Any] | None = ...,
        source_text: str | None = ...,
        metadata: dict[str, Any] | None = ...,
        instruction_set: str = ...,
        motive: Motive | str | None = ...,
        saves_step: str | None = ...,
    ) -> AddMemoryResult: ...

    # -- contributed by _lifecycle -------------------------------
    async def coherence_incidents(
        self, *, scope: MemoryScope | None = ..., status: CoherenceIncidentStatus | None = ..., limit: int = ...
    ) -> list[CoherenceIncident]: ...
    async def run_coherence_scan(
        self, *, scope: MemoryScope, now: datetime | None = ..., policy: CoherencePolicy | None = ...
    ) -> CoherenceReport: ...

    # -- contributed by _outcomes --------------------------------
    async def byte_replay(self, *, run_uuid: str) -> ReplayProof: ...
    async def certify_motive(self, *, motive: Motive, corpus: Any, scope: MemoryScope) -> MotiveCertificate: ...
    async def counterfactual(self, *, run_uuid: str, motive: Motive | str) -> CounterfactualReport: ...

    # -- contributed by _profile ---------------------------------
    @staticmethod
    def _estimate_tokens(text: str) -> int: ...
    @staticmethod
    def _profile_fact_line(
        *, relationship_type: str, fact: str, confidence: float, observed_count: int, pinned: bool
    ) -> str: ...
    async def profile(
        self,
        *,
        scope: MemoryScope,
        policy: ProfilePolicy | None = ...,
        as_of: datetime | None = ...,
        token_budget: int | None = ...,
        motive: Motive | str | None = ...,
        reader_agent_id: str | None = ...,
        maintain_context: bool = ...,
        context_policy: context_module.ContextPolicy | None = ...,
        context_transport: context_module.ContextTransport | None = ...,
    ) -> MemoryProfile: ...

    # -- contributed by _runtime ---------------------------------
    def _checkpoint_operator_run(self, run: ReceiptRun) -> None: ...
    def _dream_job(self, job_name: str, *, config: DreamConfig | None = ...): ...
    def _dream_job_run_record(self, *, job_run: DreamJobRun, ran_at: datetime) -> DreamJobRunRecord: ...
    def _operator_effective_policy_digest(self) -> str: ...
    async def negative_space(self, *, scope: MemoryScope | None = ..., **filters: Any) -> list[NegativeSpaceEntry]: ...
    async def run_dream_job(
        self,
        *,
        job_name: str,
        now: datetime | None = ...,
        tenant_id: str | None = ...,
        agent_id: str | None = ...,
        scope: MemoryScope | None = ...,
        dream_mode: str | None = ...,
        motive: str | None = ...,
        prompt_pack: str | None = ...,
    ) -> DreamRunResult: ...

    # -- contributed by _timeline --------------------------------
    def _memory_evolution_fact(self, relationship: Any, *, scope: MemoryScope) -> MemoryEvolutionFact: ...
    def _memory_evolution_signals(self, proof: MemoryEvolutionProof) -> list[MemoryEvolutionSignal]: ...
    def _relationship_is_current_active(self, relationship: Any, *, as_of: datetime) -> bool: ...
    def _scope_memory_relationships(self, scope: MemoryScope): ...
