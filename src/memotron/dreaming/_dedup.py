"""Finding claims that say the same thing twice, and un-finding them when they do not.

Deduplication is destructive in the way rollups are not: the loser stops being a
separate row. So the interesting member here is the smallest one. _weaker_duplicate
decides WHICH of two equivalent claims loses, and getting that backwards keeps the
worse-sourced, less-corroborated row and drops the better one -- with no error and
no receipt distinguishing the two outcomes.

_cross_prefix_duplicate_sweep is the expensive counterpart: duplicates that differ
only by a scope prefix are invisible to a per-scope pass, so the sweep is the only
thing that finds them, and it runs across scopes deliberately.

repromote_duplicates_for_dependency is the undo. When the row a duplicate was
folded into is later invalidated, the ones that lost have to come back -- otherwise
invalidating a rollup member silently deletes claims that were never wrong."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from memotron.config import (
    DreamJob,
    claim_mode_stance,
    default_claim_mode_for_memory_type,
)
from memotron.dreaming._common import _log
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.embedding import (
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.models import (
    ClaimMode,
    DirectiveStance,
    GraphRelationship,
    MemoryScope,
    MemoryType,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class DeduplicationMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    def _duplicate_statement_signature(
        self, relationship: GraphRelationship
    ) -> tuple[ClaimMode, DirectiveStance, str] | None:
        """WS-17 T17: (claim_mode, stance, polarity) of one stored row.

        Feeds the shared WS-16 :meth:`_statement_compatible` helper so
        cross-prefix duplicate detection applies exactly the compatibility rules
        semantic dedup applies — a negated or differently-clamed restatement is
        never treated as a duplicate.  Returns None when the row's claim mode
        cannot be resolved (such a row never participates in the sweep).
        """
        raw_claim = relationship.properties.get("claim_mode")
        if isinstance(raw_claim, str):
            try:
                claim_mode = ClaimMode(raw_claim)
            except ValueError:
                return None
        else:
            memory_type_value = self.memory_type_for_relationship(dict(relationship.properties))
            if memory_type_value is None:
                return None
            try:
                claim_mode = default_claim_mode_for_memory_type(MemoryType(memory_type_value))
            except ValueError:
                return None
        raw_stance = relationship.properties.get("directive_stance")
        if isinstance(raw_stance, str):
            try:
                stance = DirectiveStance(raw_stance)
            except ValueError:
                return None
        else:
            stance = claim_mode_stance(claim_mode)
        return claim_mode, stance, self._stored_semantic_polarity(relationship)

    @staticmethod
    def _weaker_duplicate(a: GraphRelationship, b: GraphRelationship) -> tuple[GraphRelationship, GraphRelationship]:
        """WS-17 T17: total order over a duplicate pair — returns (weaker, stronger).

        Weaker = lower ``observed_count``, then lower ``confidence``, then newer
        ``created_at``, then greater uuid (deterministic tiebreak).
        """
        a_key = (
            int(a.properties.get("observed_count", 1)),
            float(a.properties.get("confidence", 0.0)),
        )
        b_key = (
            int(b.properties.get("observed_count", 1)),
            float(b.properties.get("confidence", 0.0)),
        )
        if a_key != b_key:
            return (a, b) if a_key < b_key else (b, a)
        if a.created_at != b.created_at:
            return (a, b) if a.created_at > b.created_at else (b, a)
        return (a, b) if a.uuid > b.uuid else (b, a)

    async def _cross_prefix_duplicate_sweep(
        self,
        *,
        job: DreamJob,
        scope: MemoryScope,
        policy: object,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> tuple[int, int]:
        """WS-17 T17: demote near-duplicate facts living on DIFFERENT truth prefixes.

        Bucket-local dedup (same ``truth_prefix``) can never merge the same fact
        restated with a different subject or predicate surface.  This sweep runs
        over the scope's context-visible active non-MENTIONS raw facts (the same
        eligibility set depth-1 rollup clustering uses), pairs rows whose
        truth prefixes DIFFER but whose full-fact embeddings (same-identifier
        stored vectors only — the T18 space guard) reach
        ``cross_prefix_duplicate_threshold``, requires the WS-16 statement
        compatibility gates (same memory type + claim mode, non-conflicting
        stance, same polarity, same authority class), and demotes the weaker row
        from context — ``active_in_context=False`` + ``duplicate_of`` +
        ``duplicate_cosine`` — mirroring rollup-member demotion mechanics: out of
        the default profile, still searchable, NEVER deleted.

        Deterministic pair order (store order is ``created_at, uuid``); capped at
        ``max_duplicate_demotions_per_run`` with a log-visible receipt count.

        Returns ``(rows_demoted, decisions_recorded)``.
        """
        from memotron.config import RollupConsolidationPolicy

        policy_typed: RollupConsolidationPolicy = policy  # type: ignore[assignment]
        threshold = policy_typed.cross_prefix_duplicate_threshold
        if threshold is None:
            return 0, 0
        active_identifier = self._embedding_transport.identifier

        candidates: list[GraphRelationship] = []
        for relationship in self._graph.relationships_for_scope(scope.key):
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            if relationship.properties.get("active_in_context") is False:
                continue
            if relationship.properties.get("memory_type") == MemoryType.ROLLUP.value:
                continue
            candidates.append(relationship)
        if len(candidates) < 2:
            return 0, 0

        embeddings: list[list[float] | None] = []
        signatures: list[tuple[ClaimMode, DirectiveStance, str] | None] = []
        for relationship in candidates:
            if not stored_vector_in_active_space(relationship.properties, active_identifier=active_identifier):
                embeddings.append(None)
                signatures.append(None)
                continue
            embeddings.append(self._graph.reveal_vector(scope.key, relationship.properties.get("embedding")))
            signatures.append(self._duplicate_statement_signature(relationship))

        demoted_uuids: set[str] = set()
        demoted_pairs: list[dict[str, object]] = []
        cap_reached = False
        for i in range(len(candidates)):
            if cap_reached:
                break
            first = candidates[i]
            if first.uuid in demoted_uuids:
                continue
            if embeddings[i] is None or signatures[i] is None:
                continue
            for j in range(i + 1, len(candidates)):
                if len(demoted_pairs) >= policy_typed.max_duplicate_demotions_per_run:
                    cap_reached = True
                    break
                second = candidates[j]
                if first.uuid in demoted_uuids:
                    break
                if second.uuid in demoted_uuids:
                    continue
                if embeddings[j] is None or signatures[j] is None:
                    continue
                if first.properties.get("truth_prefix") == second.properties.get("truth_prefix"):
                    continue
                first_type = self.memory_type_for_relationship(dict(first.properties))
                second_type = self.memory_type_for_relationship(dict(second.properties))
                if first_type is None or first_type != second_type:
                    continue
                if first.properties.get("authority_class", "memory") != second.properties.get(
                    "authority_class", "memory"
                ):
                    continue
                second_claim, second_stance, second_polarity = signatures[j]
                if not self._statement_compatible(
                    first,
                    claim_mode=second_claim,
                    directive_stance=second_stance,
                    semantic_polarity=second_polarity,
                ):
                    continue
                try:
                    cosine = cosine_similarity(embeddings[i], embeddings[j])
                except ValueError:
                    continue
                if cosine < threshold:
                    continue
                weaker, stronger = self._weaker_duplicate(first, second)
                if self._context_demotion_held(weaker):
                    # WS-23 C3: an operator pin holds the row IN context.  The
                    # pair is left alone rather than silently re-ranked — a
                    # pinned row can still be the surviving side of a pair.
                    continue
                weaker_scope_key = str(weaker.properties.get("scope_key") or scope.key)
                state_before = self._graph.graph_state_hash(weaker_scope_key)
                self._graph.update_relationship(
                    weaker.uuid,
                    properties={
                        "active_in_context": False,
                        "duplicate_of": stronger.uuid,
                        "duplicate_cosine": cosine,
                    },
                )
                state_after = self._graph.graph_state_hash(weaker_scope_key)
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED,
                    decision_reason=f"cross_prefix_duplicate_of={stronger.uuid}:cosine={cosine:.4f}",
                    decision_result="demoted",
                    now=now,
                    scope_key=weaker_scope_key,
                    memory_type=first_type,
                    relationship_type=weaker.type,
                    relationship_uuid=weaker.uuid,
                    successor_relationship_uuid=stronger.uuid,
                    dedup_threshold=threshold,
                    dedup_score=cosine,
                    graph_state_hash_before=state_before,
                    graph_state_hash_after=state_after,
                )
                demoted_uuids.add(weaker.uuid)
                demoted_pairs.append(
                    {
                        "demoted_relationship_uuid": weaker.uuid,
                        "duplicate_of": stronger.uuid,
                        "cosine": cosine,
                        "memory_type": first_type,
                    }
                )
                if weaker.uuid == first.uuid:
                    break

        if not demoted_pairs:
            return 0, 0
        _log.info(
            "WS-17 cross-prefix duplicate sweep [%s] %s: %d duplicate row(s) demoted "
            "(receipts emitted: %d; per-run cap %d%s)",
            job.name,
            scope.key,
            len(demoted_pairs),
            len(demoted_pairs),
            policy_typed.max_duplicate_demotions_per_run,
            " REACHED" if cap_reached else "",
        )
        await self._record_decision(
            job=job,
            now=now,
            decision_type="consolidation_cross_prefix_duplicate_demoted",
            subject_id=scope.key,
            subject_name=f"{job.name} cross-prefix duplicate sweep for {scope.key}",
            scope=scope,
            summary=(
                f"{job.agent.name} demoted {len(demoted_pairs)} cross-prefix duplicate "
                f"row(s) in {scope.key} at cosine >= {threshold} (weaker row leaves "
                "default context, remains searchable, never deleted)."
            ),
            details={
                "approved": True,
                "demoted": demoted_pairs,
                "threshold": threshold,
                "max_duplicate_demotions_per_run": (policy_typed.max_duplicate_demotions_per_run),
                "cap_reached": cap_reached,
            },
        )
        return len(demoted_pairs), 1

    def repromote_duplicates_for_dependency(
        self,
        *,
        relationship_uuid: str,
        reason: str,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> int:
        """WS-17 T17 reversal path: re-promote duplicates of a retired stronger row.

        When a row that other rows point at via ``duplicate_of`` is superseded or
        pruned, its demoted duplicates must not stay invisibly parked behind a
        dead pointer.  Mirrors the stale-rollup member re-promotion mechanics:
        each still-active duplicate returns to context
        (``active_in_context=True``), its ``duplicate_of``/``duplicate_cosine``
        pointers are cleared, and the transition is receipted with per-mutation
        graph-state hashes (decision_result ``materialized`` — the same result
        the prune-ghost restore uses for a return-to-context transition).
        """
        repromoted = 0
        for relationship in self._graph.relationships():
            if relationship.properties.get("duplicate_of") != relationship_uuid:
                continue
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            if relationship.properties.get("active_in_context") is not False:
                continue
            scope_key = str(relationship.properties.get("scope_key"))
            state_before = self._graph.graph_state_hash(scope_key)
            self._graph.update_relationship(
                relationship.uuid,
                properties={
                    "active_in_context": True,
                    "duplicate_of": None,
                    "duplicate_cosine": None,
                    "repromoted_from_duplicate_of": relationship_uuid,
                },
            )
            state_after = self._graph.graph_state_hash(scope_key)
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_DUPLICATE_REPROMOTED,
                decision_reason=f"{reason}:duplicate_of={relationship_uuid}",
                decision_result="materialized",
                now=now,
                scope_key=scope_key,
                memory_type=self.memory_type_for_relationship(dict(relationship.properties)),
                relationship_type=relationship.type,
                relationship_uuid=relationship.uuid,
                graph_state_hash_before=state_before,
                graph_state_hash_after=state_after,
            )
            repromoted += 1
        return repromoted
