"""What a DreamEngine concern mixin may assume the composed engine provides.

Third of these, after storage/sqlite/_protocol.py and storage/postgres/_protocol.py,
and by some distance the largest -- which is the honest measurement of what
DreamEngine is. The call graph over its 152 members has 280 internal self-calls, and
under the chosen 9-way partition **61 members are called across a mixin boundary**.
This Protocol is that number written down.

Read that as a finding, not as scaffolding. Splitting DreamEngine makes it navigable
-- ten files instead of one 10,000-line class -- but it does NOT decouple it, and
nothing in this branch claims otherwise. The dependency structure was already this
shape; it was just invisible while everything sat in one class body and every call
looked local. A future pass that wants to actually reduce coupling now has a
measurement to work against: this file shrinking is the metric.

Why it is needed at all: mypy checks each mixin in isolation, so it sees
``self._emit_receipt`` on a class that does not define it. The alternative is a
blanket ``disable_error_code = ["attr-defined"]`` per mixin, which is what the sqlite
split did first -- and which also silenced every genuine attribute typo in 5,700
lines. Declaring the surface instead means a misspelled helper is still an error.

Generated from the measured call graph rather than hand-listed, so it is neither
over- nor under-inclusive: a member appears here only if some OTHER mixin calls it.
Members a mixin defines and only the composer calls are absent on purpose -- the
composer reaches them through the MRO.

How a mixin uses it
-------------------
    if TYPE_CHECKING:
        _Base = ComposedDreamEngine
    else:
        _Base = object

    class RelationshipLifecycleMixin(_Base):
        ...

At runtime the base is plain ``object``, so DreamEngine's MRO is unchanged. Only the
type checker sees the Protocol.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from memotron.agents import DreamAgentDecision
    from memotron.coherence import ArtifactSource
    from memotron.config import (
        DreamConfig,
        DreamContextPolicy,
        DreamInstructionSet,
        DreamJob,
        EffectiveMemoryPolicy,
        FormationContract,
        GovernancePolicy,
        Motive,
        SalienceRubric,
    )
    from memotron.dreaming._common import _ContentProtection
    from memotron.dreaming._identity import _EntityResolution, _TruthGateDecision
    from memotron.dreaming.agent import DreamAgentTransport
    from memotron.embedding import EmbeddingTransport
    from memotron.extraction import ExtractedMemory, InstructionalExtractor
    from memotron.models import (
        ClaimMode,
        DirectiveStance,
        DreamContextMemory,
        Episode,
        GraphRelationship,
        MemoryAuthority,
        MemoryScope,
        RelationshipStatus,
    )
    from memotron.storage import StorageBackend
    from memotron.storage.receipts import MemoryReceipt, ReceiptDecisionType, ReceiptRun
    from memotron.synthesis import SynthesisTransport


class ComposedDreamEngine(Protocol):
    """The composed :class:`DreamEngine` surface, as one mixin sees it."""

    # -- state owned by the composer, set in __init__ (which never moves, R-A1) --
    _agent_transport: DreamAgentTransport
    _artifact_sources: list[ArtifactSource]
    _config: DreamConfig
    _embedding_transport: EmbeddingTransport
    _epoch_override: str | None
    _extractor: InstructionalExtractor
    _graph: StorageBackend
    _promotion_extractor: InstructionalExtractor
    _rollup_synthesis_transport: SynthesisTransport | None

    # -- helpers kept on the composer, per R-A2 --------------------
    def _bind_formation_contract(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        job: DreamJob,
        instructions: object,
        prompt_profile: object,
        motive: Motive | None,
        governance: GovernancePolicy | None,
    ) -> FormationContract: ...
    def _claim_mode_for(self, *, memory: ExtractedMemory, memory_type_value: str) -> ClaimMode: ...
    def _content_protection(
        self, *, scope_key: str, governance: GovernancePolicy | None
    ) -> _ContentProtection | None: ...
    def _embed_content(
        self,
        text: str,
        *,
        scope_key: str,
        protection: _ContentProtection | None,
        receipt_run: ReceiptRun | None = ...,
        now: datetime | None = ...,
    ) -> list[float]: ...
    def _emit_receipt(
        self,
        receipt_run: ReceiptRun,
        *,
        decision_type: ReceiptDecisionType,
        decision_reason: str,
        decision_result: str,
        episode: Episode | None = ...,
        now: datetime | None = ...,
        **fields: object,
    ) -> MemoryReceipt: ...
    def _receipt_sensitive_payload(
        self, *, fact: str, protection: _ContentProtection | None
    ) -> tuple[str | None, bool]: ...
    def _recency_authoritative_bypass(
        self,
        *,
        memory_type: str | None,
        candidate_authority: MemoryAuthority,
        incumbent: GraphRelationship,
        effective_from: datetime,
    ) -> bool: ...
    def _relationship_authority(self, relationship: GraphRelationship) -> MemoryAuthority: ...
    def _resolved_inputs_dict(self, job: DreamJob) -> dict[str, object]: ...
    def _run_policy_digests(
        self, job: DreamJob, effective_policy: EffectiveMemoryPolicy | None
    ) -> tuple[str, str | None]: ...
    @staticmethod
    def _semantic_polarity(text: str) -> str: ...
    def _source_authority(self, *, episode: Episode, created_by: str, claim_mode: ClaimMode) -> MemoryAuthority: ...
    def _supersession_gate_reason(
        self,
        *,
        incumbent: GraphRelationship,
        candidate_authority: MemoryAuthority,
        severity_score: int,
        temporary: bool,
        corroboration_total: int | None = ...,
    ) -> str | None: ...
    def content_embedding_transport(
        self, *, scope_key: str, protection: _ContentProtection | None = ...
    ) -> EmbeddingTransport: ...

    # -- contributed by _formation ---------------------------------
    def _effective_governance(self, *, motive: Motive | None) -> GovernancePolicy | None: ...
    def _mark_raw_candidate_promoted(
        self, *, digest: str, now: datetime | None, relationship_uuid: str, resolved_by: str = ...
    ) -> None: ...
    def _mark_raw_candidate_quarantined(
        self, *, digest: str, now: datetime | None, reason: str, resolution_note: str, resolved_by: str = ...
    ) -> None: ...
    def _quarantine_unresolvable_candidate(
        self,
        candidate: ExtractedMemory,
        *,
        episode: Episode,
        receipt_run: ReceiptRun,
        now: datetime | None,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None,
        reason: str,
    ) -> None: ...
    def _resolve_memory_type_value(
        self, *, relationship_type: str, instruction_set_name: str, subject_label: str = ..., object_label: str = ...
    ) -> str | None: ...

    # -- contributed by _consolidation -----------------------------
    @staticmethod
    def _formation_consumer_key(job: DreamJob) -> str: ...
    def _graph_context_for_episode(
        self, *, episode: Episode, policy: DreamContextPolicy
    ) -> list[DreamContextMemory]: ...
    def _record_rollup_reinforcement(
        self, *, relationship: GraphRelationship, now: datetime, receipt_run: ReceiptRun
    ) -> None: ...
    def episode_matches_job(self, *, episode: Episode, job: DreamJob, consumer_key: str | None = ...) -> bool: ...
    def invalidate_rollups_for_dependency(
        self, *, relationship_uuid: str, reason: str, now: datetime, receipt_run: ReceiptRun
    ) -> int: ...
    def repromote_duplicates_for_dependency(
        self, *, relationship_uuid: str, reason: str, now: datetime, receipt_run: ReceiptRun
    ) -> int: ...

    # -- contributed by _decisions ---------------------------------
    async def _decide(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        decision_type: str,
        proposed_action: str,
        subject_id: str | None = ...,
        subject_name: str | None = ...,
        scope: MemoryScope | None = ...,
        context_facts: tuple[str, ...] = ...,
        details: dict[str, object] | None = ...,
    ) -> DreamAgentDecision: ...
    def _filter_scored_memories(
        self,
        *,
        scored: list[ExtractedMemory],
        episode: Episode,
        rubric: SalienceRubric,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        motive_name: str | None = ...,
        governance_policy_digest: str | None = ...,
        protection: _ContentProtection | None = ...,
    ) -> list[ExtractedMemory]: ...
    async def _pruning_decision(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        relationship: GraphRelationship,
        decision_type: str,
        proposed_action: str,
        reason: str,
        details: dict[str, object] | None = ...,
    ) -> DreamAgentDecision: ...
    async def _record_decision(
        self,
        *,
        job: DreamJob,
        now: datetime,
        decision_type: str,
        summary: str,
        subject_id: str | None = ...,
        subject_name: str | None = ...,
        scope: MemoryScope | None = ...,
        details: dict[str, object] | None = ...,
    ) -> None: ...
    def _reject_raw_secret_memory(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        memory: ExtractedMemory,
        now: datetime | None,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None,
        error: ValueError,
    ) -> None: ...
    def _relationship_episode_uuids(self, relationship: GraphRelationship) -> list[str]: ...
    def _relationship_scope(self, relationship: GraphRelationship) -> MemoryScope: ...
    def _relationship_status(self, raw_status: object) -> RelationshipStatus: ...
    def _relationship_visible_at_episode_time(self, relationship: GraphRelationship, episode: Episode) -> bool: ...
    def _score_memories(
        self, *, memories: list[ExtractedMemory], episode: Episode, rubric: SalienceRubric
    ) -> list[ExtractedMemory]: ...
    def _secret_reference_metadata(self, memory: ExtractedMemory) -> tuple[str | None, str | None]: ...

    # -- contributed by _predicates --------------------------------
    @staticmethod
    def _rebridgeable_row(relationship: GraphRelationship) -> bool: ...
    @staticmethod
    def memory_type_for_relationship(properties: dict[str, object]) -> str | None: ...
    def rebridge_scope_truth_keys(
        self,
        *,
        scope_key: str,
        receipt_run: ReceiptRun,
        decision_type: ReceiptDecisionType,
        reason_prefix: str,
        now: datetime,
        register_predicates: bool = ...,
        predicate_surfaces: frozenset[str] | None = ...,
        entity_names: frozenset[str] | None = ...,
        episode: Episode | None = ...,
        motive_name: str | None = ...,
    ) -> list[dict[str, Any]]: ...
    def resolve_canonical_predicate(
        self,
        *,
        scope_key: str,
        predicate: str,
        receipt_run: ReceiptRun | None = ...,
        episode: Episode | None = ...,
        now: datetime | None = ...,
        motive_name: str | None = ...,
        decided_by: str = ...,
    ) -> str: ...

    # -- contributed by _entities ----------------------------------
    def _discount_entity_aliases_on_conflict(
        self,
        *,
        scope: MemoryScope,
        canonical_subject: str,
        current_surface: str,
        conflicting_incumbents: list[GraphRelationship],
        challenger_confidence: float,
        receipt_run: ReceiptRun | None,
        episode: Episode | None,
        now: datetime | None,
        motive_name: str | None,
    ) -> None: ...
    def _register_declared_aliases(
        self,
        *,
        scope: MemoryScope,
        label: str,
        canonical_name: str,
        properties: dict[str, Any],
        instructions: DreamInstructionSet,
        protection: _ContentProtection | None,
        now: datetime | None,
    ) -> None: ...
    def _stamp_node_name_embedding(
        self,
        *,
        node: object,
        name: str,
        labels: tuple[str, ...],
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> None: ...
    def entity_inventory_for_scope(self, scope: MemoryScope) -> list[dict[str, str]]: ...
    def resolve_canonical_entity(
        self,
        *,
        scope: MemoryScope,
        name: str,
        entity_ref: str | None = ...,
        link_confidence: float | None = ...,
        receipt_run: ReceiptRun | None = ...,
        episode: Episode | None = ...,
        now: datetime | None = ...,
        motive_name: str | None = ...,
        protection: _ContentProtection | None = ...,
        decided_by: str = ...,
    ) -> _EntityResolution: ...

    # -- contributed by _materialization ---------------------------
    def _materialize_episode(
        self,
        episode: Episode,
        memories: list[ExtractedMemory],
        *,
        created_by: str = ...,
        receipt_run: ReceiptRun,
        job: DreamJob | None = ...,
        now: datetime | None = ...,
        dedup_threshold_override: float | None = ...,
        governance: GovernancePolicy | None = ...,
        motive_name: str | None = ...,
        motive_version_digest_value: str | None = ...,
        governance_policy_digest: str | None = ...,
        raw_candidates_stored: bool = ...,
        self_promote_raw_candidates: bool = ...,
    ) -> tuple[int, int, int, int]: ...

    # -- contributed by _contradiction -----------------------------
    def _add_mentions(
        self, episode_node_uuid: str, entity_node_uuid: str, reference_time: datetime, scope_key: str, created_by: str
    ) -> None: ...
    def _apply_incumbent_dispute_discount(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        now: datetime | None,
        incumbent_uuid: str,
        challenger_relationship_uuid: str,
        challenger_confidence: float,
        scope_key: str,
        truth_key: str,
        memory_type: str | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        relationship_type: str,
        governance_policy_digest: str | None,
    ) -> None: ...
    def _apply_reinforce(
        self, relationship: GraphRelationship, memory: ExtractedMemory, episode: Episode, observed_at: datetime
    ) -> GraphRelationship: ...
    def _contradiction_decision(
        self,
        *,
        scope_key: str,
        contradictions: list[GraphRelationship],
        candidate_authority: MemoryAuthority,
        severity_score: int,
        temporary: bool,
        truth_prefix: str,
        new_object: str,
        new_object_commitment: str | None,
        new_object_embedding: list[float],
        memory_type: str | None,
        dedup_threshold_override: float | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
        protection: _ContentProtection | None,
        effective_from: datetime,
    ) -> _TruthGateDecision: ...
    def _earliest_valid_to(
        self, current_valid_to: datetime | None, successor_valid_from: datetime | None
    ) -> datetime | None: ...
    def _find_polarity_conflicts(
        self,
        *,
        scope_key: str,
        truth_prefix: str,
        new_object: str,
        new_object_commitment: str | None,
        new_object_embedding: list[float],
        semantic_polarity: str,
        memory_type: str | None,
        dedup_threshold_override: float | None,
        protection: _ContentProtection | None,
    ) -> list[GraphRelationship]: ...
    def _find_reinforce_target(
        self,
        *,
        scope_key: str,
        truth_prefix: str,
        truth_key: str,
        new_object: str,
        new_object_commitment: str | None,
        new_embedding: list[float],
        memory_type: str | None,
        dedup_threshold_override: float | None = ...,
        protection: _ContentProtection | None = ...,
        authority_class: str = ...,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
        source_authority: MemoryAuthority,
        force_identifier_conflict: bool = ...,
    ) -> tuple[GraphRelationship | None, str, float, str | None, list[dict[str, object]]]: ...
    def _historical_successor_relationship(
        self,
        *,
        scope_key: str,
        truth_key: str,
        new_object: str,
        new_object_commitment: str | None,
        observed_at: datetime,
    ) -> GraphRelationship | None: ...
    def _record_semantic_reinforce(
        self,
        *,
        job: DreamJob,
        now: datetime,
        fact: str,
        scope: MemoryScope,
        episode_uuid: str,
        reinforced_uuid: str,
        cosine: float,
        matched_uuid: str | None,
    ) -> None: ...
    def _resolve_corroborated_challengers(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        now: datetime | None,
        siblings: tuple[GraphRelationship, ...],
        winner_uuid: str,
        scope_key: str,
        truth_key: str,
        memory_type: str | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        relationship_type: str,
        governance_policy_digest: str | None,
    ) -> None: ...
    def _same_stored_object(
        self, relationship: GraphRelationship, *, new_object: str, new_object_commitment: str | None
    ) -> bool: ...
    def _statement_compatible(
        self,
        relationship: GraphRelationship,
        *,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
    ) -> bool: ...
    def _stored_semantic_polarity(self, relationship: GraphRelationship) -> str: ...
    def _supersede_target_relationships(
        self,
        *,
        targets: list[GraphRelationship],
        valid_to: datetime,
        superseded_by_relationship_uuid: str,
        scope_key: str,
        superseded_by_run: str | None = ...,
    ) -> list[tuple[str, str, str]]: ...

    # -- contributed by _lifecycle ---------------------------------
    @staticmethod
    def _context_demotion_held(relationship: GraphRelationship) -> bool: ...

    # -- contributed by the two consolidation workers -------------------------
    # The ONLY two edges the measured partition found between _consolidation,
    # _dedup and _rollups. If a third appears here, the three-way split has
    # stopped paying for itself and should be re-measured.
    async def _cross_prefix_duplicate_sweep(
        self,
        *,
        job: DreamJob,
        scope: MemoryScope,
        policy: object,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> tuple[int, int]: ...
    async def _rollup_pass_for_scope(
        self,
        *,
        job: DreamJob,
        scope: MemoryScope,
        policy: object,
        depth: int,
        created_by: str,
        now: datetime,
        receipt_run: ReceiptRun,
        rebuilt_stale_rollup_uuids: set[str],
    ) -> tuple[int, int, int]: ...
