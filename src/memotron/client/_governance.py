"""Operator adjudication: alias proposals, supersession reviews, quarantined candidates.

Twelve members, all variations on one shape -- the system produced a candidate answer
it is not confident enough to apply, and a human decides. `resolve_supersession_review`
is the largest (164 lines) because approving a supersession has to write the
successor, retire the incumbent and receipt both without leaving either half applied."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from memotron.client._protocol import ComposedMemotron
from memotron.models import (
    EntityAliasProposal,
    EntityAliasResolution,
    MemoryScope,
    QuarantinedCandidate,
    QuarantineResolution,
    QuarantineStatus,
    RelationshipStatus,
    SupersessionReviewItem,
    SupersessionReviewResolution,
)
from memotron.receipts import (
    ReceiptDecisionType,
    payload_digest,
)
from memotron.storage import (
    StorageBackend,
    normalize_key,
)

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class EntityGovernanceMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def canonicalize_scope_predicates(
        self,
        *,
        scope: MemoryScope,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """WS-17 T16: receipted backfill bridging existing rows onto canonical predicates.

        An explicit operator maintenance pass (an operator receipt run, like
        ``correct_memory``): walks the scope's live truth-slot rows, resolves
        each row's predicate through the same first-wins registry formation
        uses, and where the canonical differs from what the row's truth keys
        were built on, rewrites ``truth_key``/``truth_prefix``/
        ``predicate_canonical`` in place — hash-bracketed receipt per rewritten
        row.  Genuinely historical rows (superseded lineage, pruned, resolved
        reviews) are NEVER rewritten; their truth keys are frozen audit lineage.

        WS-23 C4: gate-parked challengers ARE rewritten.  They are SUPERSEDED
        but not history — the corroboration scan finds them by ``truth_prefix``,
        so a parked row left on a pre-canonical prefix strands forever.

        WS-23 C5: the subject and object are resolved through the entity alias
        registry here too, so running this backfill after ``resolve_scope_entities``
        no longer reverts the entity half of the key.  The two backfills now
        converge on the same final truth keys in either order.

        After a backfill collapses two surfaces onto one truth slot, the
        existing single-active repair pass (pruning) supersedes the losing
        duplicate on its next run — this API only rebridges the keys.

        Crypto-shred scopes recompute truth-key commitments under the live DEK;
        a sealed row whose scope key has been destroyed fails fast
        (``ContentKeyUnavailableError``) rather than silently skipping.
        """
        self._require_authorized_scope(scope)
        if not self.config.predicate_canonicalization.enabled:
            raise ValueError(
                "predicate canonicalization is disabled: enable "
                "DreamConfig.predicate_canonicalization before running the backfill"
            )
        anchor = now or datetime.now(UTC)
        operator_run = self._begin_operator_run(job_name="canonicalize_scope_predicates", scope_key=scope.key)
        scanned = sum(
            1
            for relationship in self.graph.relationships_for_scope(scope.key)
            if self._engine._rebridgeable_row(relationship)
        )
        rewritten = self._engine.rebridge_scope_truth_keys(
            scope_key=scope.key,
            receipt_run=operator_run,
            decision_type=ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED,
            reason_prefix="backfill",
            now=anchor,
            register_predicates=True,
        )
        self._checkpoint_operator_run(operator_run)
        return {
            "scope_key": scope.key,
            "scanned": scanned,
            "rewritten_count": len(rewritten),
            "rewritten": rewritten,
            "run_uuid": operator_run.run_uuid,
        }

    async def resolve_scope_entities(
        self,
        *,
        scope: MemoryScope,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """WS-17 T16b: receipted backfill bridging existing rows onto canonical entities.

        Mirrors :meth:`canonicalize_scope_predicates` on the entity plane and
        shares its one rebridging implementation: walks the scope's live
        truth-slot rows, resolves each row's subject and object names through the
        ACTIVE alias registry (and its predicate through the predicate registry,
        so the two backfills compose in either order — WS-23 C5), and where a
        canonical differs from the name the row's truth keys were built on,
        rewrites ``truth_key``/``truth_prefix`` in place (surface forms preserved
        on ``subject_surface``/``object_surface``) — hash-bracketed receipt per
        rewritten row.  Relationship ENDPOINTS are never rewritten (the alias
        node keeps its rows; reads union across the boundary); genuinely
        historical rows are frozen audit lineage, while gate-parked challengers
        are rebridged like ACTIVE rows (WS-23 C4).

        After a backfill collapses two surfaces onto one truth slot, the
        existing single-active repair pass (pruning) supersedes the losing
        duplicate on its next run — this API only rebridges the keys.
        """
        self._require_authorized_scope(scope)
        if not self.config.entity_resolution.enabled:
            raise ValueError(
                "entity resolution is disabled: enable DreamConfig.entity_resolution before running the backfill"
            )
        anchor = now or datetime.now(UTC)
        operator_run = self._begin_operator_run(job_name="resolve_scope_entities", scope_key=scope.key)
        scanned = sum(
            1
            for relationship in self.graph.relationships_for_scope(scope.key)
            if self._engine._rebridgeable_row(relationship)
        )
        rewritten = self._engine.rebridge_scope_truth_keys(
            scope_key=scope.key,
            receipt_run=operator_run,
            decision_type=ReceiptDecisionType.FORMATION_ENTITY_LINKED,
            reason_prefix="backfill",
            now=anchor,
        )
        self._checkpoint_operator_run(operator_run)
        return {
            "scope_key": scope.key,
            "scanned": scanned,
            "rewritten_count": len(rewritten),
            "rewritten": rewritten,
            "run_uuid": operator_run.run_uuid,
        }

    def _entity_alias_group_names(self, scope_key: str, entity: str) -> frozenset[str]:
        """Normalized names of *entity*'s ACTIVE alias group (canonical + aliases).

        The singleton set of the entity itself when no ACTIVE alias links it —
        callers can always match against this set unconditionally.
        """
        normalized = normalize_key(entity)
        canonical = self.graph.canonical_entity_name_for(scope_key, normalized)
        canonical_normalized = normalize_key(canonical) if canonical is not None else normalized
        group = {canonical_normalized, normalized}
        for row in self.graph.entity_alias_rows_for_scope(scope_key, status="active"):
            if normalize_key(str(row["canonical_name"])) == canonical_normalized:
                group.add(str(row["name_normalized"]))
        return frozenset(group)

    def _entity_alias_sample_fact_uuids(self, scope: MemoryScope, name_normalized: str, *, limit: int = 5) -> list[str]:
        """Up to *limit* ACTIVE relationships mentioning one alias surface.

        A row mentions the surface when its preserved ``subject_surface``/
        ``object_surface`` matches, or its endpoint node names match —
        deterministic store order (created_at, uuid).
        """
        samples: list[str] = []
        for relationship in self.graph.relationships_for_scope(scope.key):
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            mentioned = False
            for surface_key in ("subject_surface", "object_surface"):
                surface = relationship.properties.get(surface_key)
                if isinstance(surface, str) and normalize_key(surface) == name_normalized:
                    mentioned = True
                    break
            if not mentioned:
                for node_uuid in (relationship.source_uuid, relationship.target_uuid):
                    node_name = self.graph.reveal(scope.key, self.graph.get_node(node_uuid).properties.get("name", ""))
                    if isinstance(node_name, str) and normalize_key(node_name) == name_normalized:
                        mentioned = True
                        break
            if mentioned:
                samples.append(relationship.uuid)
                if len(samples) >= limit:
                    break
        return samples

    async def pending_entity_alias_proposals(self, *, scope: MemoryScope, limit: int = 50) -> list[EntityAliasProposal]:
        """WS-17 T16b: list mid-band entity alias proposals awaiting adjudication.

        Every 'proposed' registry row for *scope* — mentions that scored inside
        ``[review_threshold, auto_link_threshold)`` (or ACTIVE links demoted by
        a truth conflict) — in deterministic (proposed_at, name) order, each
        with its link score, composed signals, and sample fact uuids.
        """
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        return [
            EntityAliasProposal(
                name=str(row["name_normalized"]),
                scope=scope,
                canonical_name=str(row["canonical_name"]),
                link_score=row["link_score"],
                link_signals=dict(row["link_signals"] or {}),
                proposed_at=datetime.fromisoformat(str(row["proposed_at"])),
                sample_fact_uuids=self._entity_alias_sample_fact_uuids(scope, str(row["name_normalized"])),
            )
            for row in self.graph.entity_alias_rows_for_scope(scope.key, status="proposed")[:limit]
        ]

    async def resolve_entity_alias_proposal(
        self,
        *,
        scope: MemoryScope,
        name: str,
        decision: Literal["approve", "reject"],
        reason: str,
        resolved_by: str,
        backfill: bool = True,
        now: datetime | None = None,
    ) -> EntityAliasResolution:
        """WS-17 T16b: adjudicate one proposed entity alias.

        ``approve`` activates the alias (receipted ENTITY_ALIAS_RESOLVED) and —
        unless ``backfill=False`` — immediately runs the receipted
        :meth:`resolve_scope_entities` backfill so existing ACTIVE rows recorded
        under the surface collapse onto the canonical truth slots (the existing
        single-active repair then settles any collision on its next pruning
        run).

        ``reject`` stamps the row 'rejected' with ``last_rejected_score`` in its
        link signals: the same pair is never re-proposed unless a later
        composed score exceeds that record by at least 0.05.
        """
        self._require_authorized_scope(scope)
        normalized_name = normalize_key(name)
        if not normalized_name:
            raise ValueError("name cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        normalized_resolved_by = resolved_by.strip()
        if not normalized_resolved_by:
            raise ValueError("resolved_by cannot be blank")
        if decision not in {"approve", "reject"}:
            raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
        row = self.graph.entity_alias_row(scope.key, normalized_name)
        if row is None:
            raise ValueError(f"entity alias {normalized_name!r} is not registered in scope {scope.key}")
        if row["status"] != "proposed":
            raise ValueError(f"entity alias {normalized_name!r} is not pending review (status {row['status']!r})")
        resolved_at = now or datetime.now(UTC)
        signals = dict(row["link_signals"] or {})
        if decision == "approve":
            new_status = "active"
        else:
            new_status = "rejected"
            signals["last_rejected_score"] = float(row["link_score"]) if row["link_score"] is not None else 0.0
        updated = self.graph.resolve_entity_alias(
            scope.key,
            normalized_name,
            status=new_status,
            resolved_by=normalized_resolved_by,
            resolved_at=resolved_at,
            link_signals=signals,
        )
        operator_run = self._begin_operator_run(job_name="resolve_entity_alias_proposal", scope_key=scope.key)
        state_hash = self.graph.graph_state_hash(scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.ENTITY_ALIAS_RESOLVED,
            decision_reason=(
                f"operator_{'approved' if decision == 'approve' else 'rejected'}:"
                f"{normalized_name}->{normalize_key(str(updated['canonical_name']))}"
            ),
            decision_result="recorded",
            now=resolved_at,
            scope_key=scope.key,
            dedup_score=updated["link_score"],
            event_payload=json.dumps(
                {
                    "decision": decision,
                    "reason": normalized_reason,
                    "link_signals": updated["link_signals"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            graph_state_hash_before=state_hash,
            graph_state_hash_after=state_hash,
        )
        self._checkpoint_operator_run(operator_run)
        backfill_rewritten = 0
        if decision == "approve" and backfill:
            backfill_result = await self.resolve_scope_entities(scope=scope, now=resolved_at)
            backfill_rewritten = int(backfill_result["rewritten_count"])
        return EntityAliasResolution(
            name=normalized_name,
            scope=scope,
            canonical_name=str(updated["canonical_name"]),
            decision=decision,
            status=str(updated["status"]),
            link_score=updated["link_score"],
            backfill_rewritten_count=backfill_rewritten,
            reason=normalized_reason,
            resolved_by=normalized_resolved_by,
            resolved_at=resolved_at,
        )

    def _pending_review_relationship(self, relationship: Any) -> bool:
        """A gate-parked challenger still awaiting adjudication: the operator-review
        flag is set and no resolution (corroborated / operator_*) has landed yet."""
        properties = relationship.properties
        return bool(properties.get("requires_operator_review")) and not properties.get("review_resolution")

    def _review_incumbents(self, relationship: Any) -> list[Any]:
        """The ACTIVE row(s) a parked challenger disputes.

        Single-active parks: every active row on the challenger's ``truth_key``
        (the slot it failed to claim).  Multi-active polarity parks store the
        object in the truth_key, so the slot has no active sibling — fall back to
        the still-ACTIVE row the gate parked the challenger against
        (``superseded_by_relationship_uuid``).  Empty when neither survives.
        """
        truth_key = relationship.properties.get("truth_key")
        if isinstance(truth_key, str) and truth_key.strip():
            # The challenger's OWN scope. A truth key is an unescaped concatenation, so it
            # does not identify a scope on its own; approving a park here flips truth, and
            # unscoped this would flip it in whichever scope happened to share the slot.
            scope_key = relationship.properties.get("scope_key")
            if not isinstance(scope_key, str) or not scope_key:
                raise ValueError(f"relationship {relationship.uuid} has no scope_key")
            incumbents = [
                row
                for row in self.graph.find_active_truth_relationships(truth_key, scope_key=scope_key)
                if row.uuid != relationship.uuid
            ]
            if incumbents:
                incumbents.sort(key=lambda row: (row.created_at, row.uuid))
                return incumbents
        parked_against = relationship.properties.get("superseded_by_relationship_uuid")
        if isinstance(parked_against, str) and parked_against:
            try:
                candidate = self.graph.get_relationship(parked_against)
            except ValueError:
                return []
            if candidate.properties.get("status") == RelationshipStatus.ACTIVE.value:
                return [candidate]
        return []

    async def pending_supersession_reviews(
        self, *, scope: MemoryScope, limit: int = 50
    ) -> list[SupersessionReviewItem]:
        """WS-16 T14: list gate-parked challengers awaiting human adjudication.

        Sources every relationship in *scope* that the supersession gate parked
        (``requires_operator_review`` truthy) and that no resolution — operator
        or corroboration escape — has settled yet.  Facts decrypt on read; a
        crypto-shredded scope degrades to the ``[CRYPTO-SHREDDED]`` placeholder.
        Deterministic order: (created_at, uuid) ascending.
        """
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        pending = [
            relationship
            for relationship in self._scope_memory_relationships(scope)
            if self._pending_review_relationship(relationship)
        ]
        pending.sort(key=lambda relationship: (relationship.created_at, relationship.uuid))
        items: list[SupersessionReviewItem] = []
        for relationship in pending[:limit]:
            incumbents = self._review_incumbents(relationship)
            items.append(
                SupersessionReviewItem(
                    relationship_uuid=relationship.uuid,
                    scope=scope,
                    fact=str(self.graph.reveal(scope.key, relationship.properties.get("fact", ""))),
                    truth_key=str(relationship.properties.get("truth_key", "")),
                    gate_reason=str(relationship.properties.get("supersession_gate_reason", "")),
                    source_authority=str(relationship.properties.get("source_authority", "")),
                    created_at=relationship.created_at,
                    current_incumbent_uuid=incumbents[0].uuid if incumbents else None,
                    challenger_confidence=float(relationship.properties.get("confidence", 0.0)),
                )
            )
        return items

    async def resolve_supersession_review(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        decision: Literal["approve", "reject"],
        reason: str,
        resolved_by: str,
        now: datetime | None = None,
    ) -> SupersessionReviewResolution:
        """WS-16 T14: adjudicate one gate-parked supersession challenger.

        ``reject`` keeps current truth: the challenger stays SUPERSEDED and is
        stamped ``review_resolution="operator_rejected"`` (with resolver/time/
        reason) so it stops appearing in ``pending_supersession_reviews``; the
        incumbent is untouched.

        ``approve`` makes the parked candidate current truth THROUGH the same
        supersession machinery formation uses: each surviving ACTIVE incumbent is
        superseded (valid_to closed at resolution time,
        ``superseded_by_relationship_uuid`` -> candidate, receipted as
        FORMATION_TRUTH_KEY_SUPERSEDED with reason ``operator_review_approved``),
        then the candidate becomes ACTIVE with its park pointers cleared,
        ``review_resolution="operator_approved"``, its original ``valid_from``
        preserved, and its ``valid_to`` reopened.  Every row mutation is
        hash-bracketed and receipted in one operator run.
        """
        self._require_authorized_scope(scope)
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        normalized_resolved_by = resolved_by.strip()
        if not normalized_resolved_by:
            raise ValueError("resolved_by cannot be blank")
        if decision not in {"approve", "reject"}:
            raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
        target = self.graph.get_relationship(relationship_uuid)
        if target.type == "MENTIONS":
            raise ValueError("MENTIONS relationships cannot be review-resolved")
        relationship_scope = self._relationship_scope(target.properties)
        if relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        if not self._pending_review_relationship(target):
            existing_resolution = target.properties.get("review_resolution")
            if existing_resolution:
                raise ValueError(f"relationship {target.uuid} review was already resolved ({existing_resolution!r})")
            raise ValueError(f"relationship {target.uuid} is not parked for operator review")

        resolved_at = now or datetime.now(UTC)
        review_resolution = "operator_approved" if decision == "approve" else "operator_rejected"
        review_properties = {
            "requires_operator_review": False,
            "review_resolution": review_resolution,
            "review_resolved_by": normalized_resolved_by,
            "review_resolved_at": resolved_at.isoformat(),
            "review_reason": normalized_reason,
        }
        memory_type = self._engine.memory_type_for_relationship(dict(target.properties))
        truth_key = str(target.properties.get("truth_key", ""))
        operator_run = self._begin_operator_run(job_name="resolve_supersession_review", scope_key=scope.key)
        superseded_incumbent_uuids: list[str] = []
        if decision == "approve":
            incumbents = self._review_incumbents(target)
            superseded_transitions = self._engine._supersede_target_relationships(
                targets=incumbents,
                valid_to=resolved_at,
                superseded_by_relationship_uuid=target.uuid,
                scope_key=scope.key,
            )
            for incumbent, (superseded_uuid, supersede_before, supersede_after) in zip(
                incumbents, superseded_transitions, strict=True
            ):
                superseded_incumbent_uuids.append(superseded_uuid)
                self._emit_receipt(
                    operator_run,
                    decision_type=ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED,
                    decision_reason="operator_review_approved",
                    decision_result="superseded",
                    now=resolved_at,
                    scope_key=scope.key,
                    relationship_type=incumbent.type,
                    memory_type=self._engine.memory_type_for_relationship(dict(incumbent.properties)),
                    truth_key=truth_key,
                    relationship_uuid=superseded_uuid,
                    superseded_relationship_uuid=superseded_uuid,
                    successor_relationship_uuid=target.uuid,
                    graph_state_hash_before=supersede_before,
                    graph_state_hash_after=supersede_after,
                )
                self._engine.invalidate_rollups_for_dependency(
                    relationship_uuid=superseded_uuid,
                    reason="operator_review_approved",
                    now=resolved_at,
                    receipt_run=operator_run,
                )
                # WS-17 T17: duplicates parked behind the superseded incumbent
                # return to context.
                self._engine.repromote_duplicates_for_dependency(
                    relationship_uuid=superseded_uuid,
                    reason="operator_review_approved",
                    now=resolved_at,
                    receipt_run=operator_run,
                )
            # The candidate takes the slot: reactivate it in place — original
            # valid_from preserved, valid_to reopened, park pointers cleared.
            flip_before = self.graph.graph_state_hash(scope.key)
            self.graph.update_relationship(
                target.uuid,
                properties={
                    "status": RelationshipStatus.ACTIVE.value,
                    "superseded_at": None,
                    "superseded_by_relationship_uuid": None,
                    **review_properties,
                },
                clear_valid_to=True,
            )
            flip_after = self.graph.graph_state_hash(scope.key)
            final_status = RelationshipStatus.ACTIVE
        else:
            flip_before = self.graph.graph_state_hash(scope.key)
            self.graph.update_relationship(target.uuid, properties=review_properties)
            flip_after = self.graph.graph_state_hash(scope.key)
            final_status = RelationshipStatus.SUPERSEDED
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.OPERATOR_REVIEW_RESOLVED,
            decision_reason=review_resolution,
            decision_result="recorded",
            now=resolved_at,
            scope_key=scope.key,
            relationship_type=target.type,
            memory_type=memory_type,
            truth_key=truth_key,
            relationship_uuid=target.uuid,
            graph_state_hash_before=flip_before,
            graph_state_hash_after=flip_after,
        )
        self._checkpoint_operator_run(operator_run)
        return SupersessionReviewResolution(
            relationship_uuid=target.uuid,
            scope=scope,
            decision=decision,
            review_resolution=review_resolution,
            status=final_status,
            superseded_incumbent_uuids=superseded_incumbent_uuids,
            reason=normalized_reason,
            resolved_by=normalized_resolved_by,
            resolved_at=resolved_at,
        )

    async def quarantined_candidates(
        self,
        *,
        scope: MemoryScope,
        status: QuarantineStatus | str | None = QuarantineStatus.QUARANTINED,
        limit: int = 50,
    ) -> list[QuarantinedCandidate]:
        """List candidates the actionability gate kept out of this scope's graph.

        The mirror of :meth:`pending_supersession_reviews`, for the other
        adjudication the write path defers to a human.  A quarantined candidate
        is retained and inspectable but is NOT context-visible, NOT returned by
        search or profile, and holds no share of any retrieval budget — it never
        entered the relationship plane at all.

        Content fields decrypt on read; a crypto-shredded scope degrades to the
        placeholder exactly like every other content surface.  Pass
        ``status=None`` to include already-resolved rows.  Deterministic order:
        (quarantined_at, candidate_uuid) ascending.
        """
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        return self.graph.quarantined_candidates(scope_key=scope.key, status=status, limit=limit)

    async def resolve_quarantined_candidate(
        self,
        *,
        candidate_uuid: str,
        scope: MemoryScope,
        decision: Literal["promote", "discard"],
        reason: str,
        resolved_by: str,
        saves_step: str | None = None,
        now: datetime | None = None,
    ) -> QuarantineResolution:
        """Adjudicate one quarantined candidate.

        ``promote`` materializes it through the ordinary operator write path —
        same validation, same truth management, same receipts — so a promoted
        candidate is indistinguishable from any other operator-authored memory
        once it lands.  The operator may supply the ``saves_step`` the extractor
        could not; when they do not, the candidate's own stated step (if any)
        travels with it, because an operator promoting a candidate IS the
        judgement the gate was approximating.

        ``discard`` closes the row without writing anything.  Neither decision
        deletes the quarantine record: the candidate, its digest, and the reason
        it was refused stay auditable forever.
        """
        self._require_authorized_scope(scope)
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        normalized_resolved_by = resolved_by.strip()
        if not normalized_resolved_by:
            raise ValueError("resolved_by cannot be blank")
        if decision not in ("promote", "discard"):
            raise ValueError("decision must be 'promote' or 'discard'")
        candidate = self.graph.quarantined_candidate(candidate_uuid)
        if candidate.scope.key != scope.key:
            raise ValueError(
                f"quarantined candidate {candidate_uuid!r} belongs to scope {candidate.scope.key!r}, not {scope.key!r}"
            )
        resolved_at = now or datetime.now(UTC)

        promoted_relationship_uuid: str | None = None
        if decision == "promote":
            if not candidate.proposed_relationship_type:
                raise ValueError(
                    f"quarantined candidate {candidate_uuid!r} named no relationship "
                    "type; it cannot be promoted without one"
                )
            if not (candidate.subject and candidate.predicate and candidate.object):
                raise ValueError(
                    f"quarantined candidate {candidate_uuid!r} is missing subject/"
                    "predicate/object and cannot be promoted"
                )
            result = await self.add_memory(
                subject=candidate.subject,
                predicate=candidate.predicate,
                object=candidate.object,
                relationship_type=candidate.proposed_relationship_type,
                scope=scope,
                source_description="operator promotion from quarantine",
                instruction_set=candidate.instruction_set or "default",
                saves_step=saves_step or candidate.saves_step,
                metadata={
                    "promoted_from_quarantine": candidate_uuid,
                    "quarantine_reason": candidate.reason,
                },
            )
            promoted_relationship_uuid = result.relationship_uuid

        status = QuarantineStatus.PROMOTED if decision == "promote" else QuarantineStatus.DISCARDED
        self.graph.resolve_quarantined_candidate(
            candidate_uuid,
            status=status,
            resolved_at=resolved_at,
            resolved_by=normalized_resolved_by,
            resolution_note=normalized_reason,
            promoted_relationship_uuid=promoted_relationship_uuid,
        )
        operator_run = self._begin_operator_run(job_name="resolve_quarantined_candidate", scope_key=scope.key)
        resolution_payload = {
            "candidate_uuid": candidate_uuid,
            "decision": decision,
            "quarantine_reason": candidate.reason,
            "promoted_relationship_uuid": promoted_relationship_uuid,
        }
        self.graph.receipts.emit(
            operator_run,
            decision_type=(
                ReceiptDecisionType.QUARANTINE_CANDIDATE_PROMOTED
                if decision == "promote"
                else ReceiptDecisionType.QUARANTINE_CANDIDATE_DISCARDED
            ),
            decision_reason=f"quarantine_{decision}:{candidate.reason}",
            decision_result="materialized" if decision == "promote" else "rejected",
            scope_key=scope.key,
            created_at=resolved_at,
            candidate_uuid=candidate_uuid,
            candidate_digest=candidate.candidate_digest,
            relationship_uuid=promoted_relationship_uuid,
            memory_type=candidate.proposed_memory_type,
            event_payload=json.dumps(resolution_payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(resolution_payload),
        )
        self._checkpoint_operator_run(operator_run)
        return QuarantineResolution(
            candidate_uuid=candidate_uuid,
            scope=scope,
            decision=decision,
            status=status,
            promoted_relationship_uuid=promoted_relationship_uuid,
            resolved_by=normalized_resolved_by,
            resolved_at=resolved_at,
            reason=normalized_reason,
        )
