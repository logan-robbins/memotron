"""Pruning, soft caps, retention scoring and single-active repair.

One mixin rather than the three the plan named, because the call graph says pruning
and retention are one concern cut at an arbitrary line: 10 of the retention helpers
are called from the pruning path and _mark_and_receipt_pruned goes back the other
way. Splitting them would have added 12 cross-mixin edges and bought nothing.

What lives here is everything that decides a relationship should STOP being active,
and by which of the four independent routes: past its valid_to, past max age, stale
and unused, or superseded past its retention window. Those four predicates are
deliberately separate and deliberately dull -- each is the whole reason a memory
disappears, and a memory disappearing is the failure mode users notice and cannot
diagnose. _apply_soft_cap is the fifth route and the only one driven by pressure
rather than by the relationship itself."""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import TYPE_CHECKING

from memotron.config import (
    DreamJob,
    RetentionPolicy,
)
from memotron.dreaming._common import _log
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.models import (
    DreamJobRun,
    GraphRelationship,
    MemoryType,
    RelationshipCardinality,
    RelationshipStatus,
)
from memotron.receipts import (
    MemoryReceipt,
    ReceiptDecisionType,
    ReceiptRun,
)
from memotron.retrieval import (
    use_need as compute_use_need,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # At runtime the base is plain object, so DreamEngine's MRO is unchanged.
    _Base = object


class RelationshipLifecycleMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    async def _apply_generic_prune(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        relationship: GraphRelationship,
        reason: str,
        proposed_action: str,
        details: dict[str, object],
        properties: dict[str, object],
        valid_to: datetime | None = None,
    ) -> bool:
        """Run the retention gate, approval seam, and receipted archive mutation."""
        eligible, eligibility_reason, policy, policy_source = self._retention_eligibility(
            job=job,
            relationship=relationship,
            now=now,
            reason=reason,
        )
        components = self._retention_components(
            relationship=relationship,
            now=now,
            policy=policy,
            policy_source=policy_source,
        )
        components.update(
            {
                "eligibility": "eligible" if eligible else "held",
                "eligibility_reason": eligibility_reason,
                "prune_reason": reason,
            }
        )
        if not eligible:
            await self._record_decision(
                job=job,
                now=now,
                decision_type="pruning_retention_held",
                summary=(
                    f"Retention gate held relationship {relationship.uuid} out of {reason} pruning: "
                    f"{eligibility_reason}."
                ),
                subject_id=relationship.uuid,
                subject_name=str(relationship.properties.get("fact", relationship.uuid)),
                scope=self._relationship_scope(relationship),
                details=components,
            )
            return False
        decision = await self._pruning_decision(
            job=job,
            now=now,
            receipt_run=receipt_run,
            relationship=relationship,
            decision_type="pruning_relationship_pruned",
            proposed_action=proposed_action,
            reason=reason,
            details={**details, "retention_components": components},
        )
        if not decision.approved:
            return False
        self._mark_and_receipt_pruned(
            receipt_run,
            relationship=relationship,
            status=RelationshipStatus.PRUNED,
            valid_to=valid_to,
            properties=properties,
            reason=reason,
            now=now,
            decision_type=ReceiptDecisionType.PRUNING_RELATIONSHIP_PRUNED,
            retention_components=components,
        )
        return True

    async def _run_pruning(self, *, job: DreamJob, now: datetime, receipt_run: ReceiptRun) -> DreamJobRun:
        run = DreamJobRun(job_name=job.name, job_kind=job.kind)
        # Run-level claim: most pruning mutations are idempotent status writes
        # (re-pruning an already-pruned row converges; the ghost insert is an
        # ``ON CONFLICT`` upsert; single-active repair picks a deterministic
        # winner), but temporary-predecessor restoration CREATES a new active
        # row, and duplicated decisions/receipts from two interleaved pruning
        # runs are audit noise.  A second concurrent pruning run over the same
        # breadth completes as a clean zero-work run; the next cadence retries.
        if not self._graph.claim_scope_work(
            work_kind="pruning",
            scope_key=job.scope.key if job.scope is not None else "*",
            consumer_key="pruning",
            run_uuid=receipt_run.run_uuid,
            now=now,
        ):
            _log.info(
                "pruning run %s skipped: another pruning run holds the claim",
                job.name,
            )
            return run
        repaired, repair_decisions = await self._repair_single_active_conflicts(
            job=job,
            now=now,
            max_repairs=job.max_items_per_run,
            receipt_run=receipt_run,
        )
        run.superseded_relationships += repaired
        run.decision_count += repair_decisions
        for relationship in self._graph.relationships():
            if run.superseded_relationships + run.pruned_relationships >= job.max_items_per_run:
                break
            if relationship.type == "MENTIONS":
                continue
            if job.scope is not None and relationship.properties.get("scope_key") != job.scope.key:
                continue
            status = relationship.properties.get("status")
            confidence = float(relationship.properties.get("confidence", 1.0))
            # WS-16 T13 decay knob: the min-confidence gate evaluates an
            # age-discounted view when ConfidencePolicy.half_life_days is set;
            # the stored confidence is NEVER mutated.  Default (None) returns
            # the stored value — byte-identical pre-WS-16 behavior.
            effective_confidence = self._effective_confidence(relationship, now=now, stored_confidence=confidence)
            min_confidence = self._config.pruning.min_confidence_for(relationship.type)
            if status == RelationshipStatus.ACTIVE.value and self._is_active_past_valid_to(relationship, now):
                restored = self._restore_temporary_predecessor(
                    relationship=relationship,
                    now=now,
                    receipt_run=receipt_run,
                )
                run.created_relationships += int(restored is not None)
                pruned = await self._apply_generic_prune(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    proposed_action="Prune active relationship because its valid_to window has elapsed.",
                    reason="valid_to_elapsed",
                    details={"valid_to": relationship.valid_to.isoformat() if relationship.valid_to else None},
                    properties={"pruned_reason": "valid_to_elapsed"},
                )
                run.decision_count += 1
                run.pruned_relationships += int(pruned)
            elif status == RelationshipStatus.ACTIVE.value and effective_confidence < min_confidence:
                below_details: dict[str, object] = {
                    "confidence": confidence,
                    "min_confidence": min_confidence,
                }
                if effective_confidence != confidence:
                    below_details["effective_confidence"] = effective_confidence
                    below_details["half_life_days"] = self._config.confidence.half_life_days
                pruned = await self._apply_generic_prune(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    proposed_action="Prune active relationship because confidence is below the configured threshold.",
                    reason="below_min_confidence",
                    details=below_details,
                    valid_to=now,
                    properties={
                        "pruned_reason": "below_min_confidence",
                        "pruned_threshold": min_confidence,
                    },
                )
                run.decision_count += 1
                run.pruned_relationships += int(pruned)
            elif status == RelationshipStatus.ACTIVE.value and self._is_active_past_max_age(relationship, now):
                max_age = self._config.pruning.active_max_age_for(relationship.type)
                pruned = await self._apply_generic_prune(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    proposed_action="Prune active relationship because it exceeded active max age.",
                    reason="active_max_age_elapsed",
                    details={
                        "active_max_age_seconds": int(max_age.total_seconds()) if max_age is not None else None,
                    },
                    valid_to=now,
                    properties={
                        "pruned_reason": "active_max_age_elapsed",
                        "pruned_active_max_age_seconds": int(max_age.total_seconds()) if max_age is not None else None,
                    },
                )
                run.decision_count += 1
                run.pruned_relationships += int(pruned)
            elif status == RelationshipStatus.ACTIVE.value and self._is_active_stale_unused(relationship, now):
                # WS-20 T25: staleness lifecycle — retention plane only, truth
                # untouched.  Flows through the SAME generic gate chain, so
                # protected types, corrections, holds, and pins all still win;
                # the archive is a restorable PruneGhost, never a deletion.
                stale_after = self._config.pruning.stale_after_for(
                    self.memory_type_for_relationship(dict(relationship.properties))
                )
                stale_seconds = int(stale_after.total_seconds()) if stale_after is not None else None
                pruned = await self._apply_generic_prune(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    proposed_action=(
                        "Prune active relationship because it exceeded its per-type staleness "
                        "window without any observation or use event."
                    ),
                    reason="stale_unused",
                    details={
                        "stale_after_seconds": stale_seconds,
                        "last_observed_at": self._last_observed_at(relationship).isoformat(),
                    },
                    valid_to=now,
                    properties={
                        "pruned_reason": "stale_unused",
                        "pruned_stale_after_seconds": stale_seconds,
                    },
                )
                run.decision_count += 1
                run.pruned_relationships += int(pruned)
            elif status == RelationshipStatus.SUPERSEDED.value and self._is_superseded_past_retention(
                relationship, now
            ):
                pruned = await self._apply_generic_prune(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    proposed_action="Prune superseded relationship because its retention window has elapsed.",
                    reason="superseded_retention_elapsed",
                    details={
                        "valid_to": relationship.valid_to.isoformat() if relationship.valid_to else None,
                        "retention_seconds": int(
                            self._config.pruning.superseded_retention_for(relationship.type).total_seconds()
                        ),
                    },
                    properties={"pruned_reason": "superseded_retention_elapsed"},
                )
                run.decision_count += 1
                run.pruned_relationships += int(pruned)

        # WS-6: Soft-cap governance of last resort.
        # Runs AFTER WS-1 dedup and WS-4 rollup consolidation have done the real work.
        # Only fires when context-visible count exceeds the configured ceiling.
        # Off by default (GrowthPolicy.soft_cap = None → zero change to existing behaviour).
        # Kept as an internal backstop; never tenant-exposed as a hard cap.
        run.pruned_relationships += await self._apply_soft_cap(job=job, now=now, run=run, receipt_run=receipt_run)

        return run

    def _restore_temporary_predecessor(
        self,
        *,
        relationship: GraphRelationship,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> GraphRelationship | None:
        predecessor_ids = relationship.properties.get("temporary_predecessor_relationship_uuids")
        if not isinstance(predecessor_ids, list) or not predecessor_ids:
            return None
        if relationship.properties.get("temporary_restored_relationship_uuid"):
            return None
        truth_key = relationship.properties.get("truth_key")
        if not isinstance(truth_key, str) or not truth_key:
            raise ValueError(f"temporary relationship {relationship.uuid} has no truth_key")
        scope_key = relationship.properties.get("scope_key")
        if not isinstance(scope_key, str) or not scope_key:
            # Raise rather than falling through with `str(None)` -> "None", which would match
            # no rows and read as "no competing current", i.e. fail QUIET on the branch that
            # decides whether a predecessor may be restored. Same shape as the truth_key
            # guard two lines up, and every writer sets both.
            raise ValueError(f"temporary relationship {relationship.uuid} has no scope_key")
        competing_current = [
            candidate
            for candidate in self._graph.find_active_truth_relationships(truth_key, scope_key=scope_key)
            if candidate.uuid != relationship.uuid and not self._is_active_past_valid_to(candidate, now)
        ]
        if competing_current:
            return None
        predecessors: list[GraphRelationship] = []
        for predecessor_uuid in predecessor_ids:
            if not isinstance(predecessor_uuid, str):
                raise ValueError(f"temporary relationship {relationship.uuid} has an invalid predecessor id")
            predecessors.append(self._graph.get_relationship(predecessor_uuid))
        predecessors.sort(
            key=lambda item: item.valid_from or item.created_at,
            reverse=True,
        )
        predecessor = predecessors[0]
        original_valid_to_map = relationship.properties.get("temporary_predecessor_valid_to")
        original_valid_to: datetime | None = None
        if isinstance(original_valid_to_map, dict):
            raw_original_valid_to = original_valid_to_map.get(predecessor.uuid)
            if isinstance(raw_original_valid_to, str):
                original_valid_to = datetime.fromisoformat(raw_original_valid_to)
                if original_valid_to.tzinfo is None:
                    raise ValueError(f"temporary predecessor {predecessor.uuid} has a naive original valid_to")
        restore_at = relationship.valid_to
        if restore_at is None:
            raise ValueError(f"temporary relationship {relationship.uuid} has no valid_to")
        if original_valid_to is not None and original_valid_to <= restore_at:
            return None
        restored_properties = dict(predecessor.properties)
        for key in tuple(restored_properties):
            if key.startswith(("superseded_", "pruned_")) or key in {
                "archive_tier",
                "archived_at",
                "rolled_up_by",
                "demoted_at",
                "demoted_reason",
            }:
                restored_properties.pop(key, None)
        restored_properties.update(
            {
                "status": RelationshipStatus.ACTIVE.value,
                "active_in_context": True,
                "created_by": "temporary-restoration",
                "restored_from_relationship_uuid": predecessor.uuid,
                "restored_after_temporary_relationship_uuid": relationship.uuid,
                "restored_at": restore_at.isoformat(),
            }
        )
        metadata = restored_properties.get("metadata")
        restored_properties["metadata"] = {
            **(metadata if isinstance(metadata, dict) else {}),
            "temporary_restoration": True,
            "temporary_override_relationship_uuid": relationship.uuid,
        }
        state_before = self._graph.graph_state_hash(scope_key)
        restored = self._graph.add_relationship(
            source_uuid=predecessor.source_uuid,
            target_uuid=predecessor.target_uuid,
            relationship_type=predecessor.type,
            properties=restored_properties,
            valid_from=restore_at,
            valid_to=original_valid_to,
        )
        self._graph.update_relationship(
            relationship.uuid,
            properties={
                "temporary_restored_relationship_uuid": restored.uuid,
                "temporary_restored_at": now.isoformat(),
            },
        )
        # The restoration receipt covers both mutations as one atomic lifecycle
        # transition: materialize the restored interval and bind it to the expired
        # temporary override. Hash only after both writes are complete.
        state_after = self._graph.graph_state_hash(scope_key)
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.PRUNING_TEMPORARY_PREDECESSOR_RESTORED,
            decision_reason="temporary_override_expired",
            decision_result="materialized",
            now=now,
            scope_key=scope_key,
            relationship_uuid=restored.uuid,
            successor_relationship_uuid=restored.uuid,
            superseded_relationship_uuid=relationship.uuid,
            relationship_type=restored.type,
            memory_type=self.memory_type_for_relationship(restored.properties),
            truth_key=str(restored.properties.get("truth_key") or ""),
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
        )
        return restored

    def _emit_pruned_receipt(
        self,
        receipt_run: ReceiptRun,
        *,
        relationship: GraphRelationship,
        reason: str,
        now: datetime,
        decision_type: ReceiptDecisionType,
        decision_result: str = "pruned",
        successor_relationship_uuid: str | None = None,
        graph_state_hash_before: str | None = None,
        graph_state_hash_after: str | None = None,
        retention_components: dict[str, object] | None = None,
    ) -> MemoryReceipt:
        """WS-11: receipt one prune / soft-cap / single-active-repair disposition ([0021])."""
        props = relationship.properties
        return self._emit_receipt(
            receipt_run,
            decision_type=decision_type,
            decision_reason=reason,
            decision_result=decision_result,
            now=now,
            scope_key=str(props.get("scope_key") or receipt_run.scope_key),
            memory_type=self.memory_type_for_relationship(dict(props)),
            relationship_type=relationship.type,
            relationship_uuid=relationship.uuid,
            # A "superseded" disposition must name both pointers for replay's
            # fail-closed artifact census ([0025] step 430).
            superseded_relationship_uuid=(relationship.uuid if decision_result == "superseded" else None),
            successor_relationship_uuid=successor_relationship_uuid,
            graph_state_hash_before=graph_state_hash_before,
            graph_state_hash_after=graph_state_hash_after,
            retention_components=(
                json.dumps(retention_components, sort_keys=True, separators=(",", ":"))
                if retention_components is not None
                else None
            ),
        )

    def _mark_and_receipt_pruned(
        self,
        receipt_run: ReceiptRun,
        *,
        relationship: GraphRelationship,
        status: RelationshipStatus,
        reason: str,
        now: datetime,
        decision_type: ReceiptDecisionType,
        decision_result: str = "pruned",
        valid_to: datetime | None = None,
        properties: dict[str, object] | None = None,
        successor_relationship_uuid: str | None = None,
        retention_components: dict[str, object] | None = None,
    ) -> None:
        """WS-11/WS-12: apply one pruning mutation bracketed by per-mutation
        graph-state hashes and receipt it — pruning receipts fold in byte replay
        exactly like formation mutations (claim 4)."""
        scope_key = str(relationship.properties.get("scope_key") or receipt_run.scope_key)
        state_before = self._graph.graph_state_hash(scope_key)
        archive_properties = dict(properties or {})
        if status == RelationshipStatus.PRUNED:
            archive_properties.update(
                {
                    "archive_tier": True,
                    "active_in_context": False,
                    "archived_at": now.isoformat(),
                    # WS-26 T1: prune-side provenance mirrors the create/
                    # supersede stamps so "everything run R did" traverses
                    # every disposition, not only materializations.
                    "pruned_by_run": receipt_run.run_uuid,
                }
            )
        elif status == RelationshipStatus.SUPERSEDED:
            archive_properties.setdefault("superseded_by_run", receipt_run.run_uuid)
        marked = self._graph.mark_relationship(
            relationship.uuid,
            status=status,
            valid_to=valid_to,
            properties=archive_properties,
        )
        state_after = self._graph.graph_state_hash(scope_key)
        receipt = self._emit_pruned_receipt(
            receipt_run,
            relationship=marked,
            reason=reason,
            now=now,
            decision_type=decision_type,
            decision_result=decision_result,
            successor_relationship_uuid=successor_relationship_uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            retention_components=retention_components,
        )
        # Valid-time expiry is an intentional temporal transition, not a
        # speculative eviction; restoring it would resurrect stale state.
        if status == RelationshipStatus.PRUNED and reason != "valid_to_elapsed":
            self._graph.create_prune_ghost(
                relationship_uuid=marked.uuid,
                scope_key=scope_key,
                prune_receipt_uuid=receipt.receipt_uuid,
                reason=reason,
                pruned_at=now,
            )
        if status in {RelationshipStatus.PRUNED, RelationshipStatus.SUPERSEDED}:
            self.invalidate_rollups_for_dependency(
                relationship_uuid=marked.uuid,
                reason=reason,
                now=now,
                receipt_run=receipt_run,
            )
            # WS-17 T17: duplicates parked behind the retired row return to context.
            self.repromote_duplicates_for_dependency(
                relationship_uuid=marked.uuid,
                reason=reason,
                now=now,
                receipt_run=receipt_run,
            )

    async def _apply_soft_cap(
        self,
        *,
        job: DreamJob,
        now: datetime,
        run: DreamJobRun,
        receipt_run: ReceiptRun,
    ) -> int:
        """Enforce a soft cap through retention gates then expected-loss ranking.

        The candidate value is ``P(future need) × loss_if_absent ÷
        retention_cost``.  Truth confidence, formation salience, and raw
        observation counts are intentionally absent: their meanings are not
        utility and cannot be converted into one by multiplication.
        """
        growth = self._config.pruning.growth
        if growth.soft_cap is None and not growth.per_type_soft_caps:
            # Fast path: no cap configured → zero change to existing behaviour.
            return 0

        scoped_job_key = job.scope.key if job.scope is not None else None
        total_pruned = 0

        # Collect context-visible active candidates for each scope.  Eligibility
        # is deliberately evaluated before ranking; a protected row never enters
        # a generic soft-cap deletion lottery.
        scope_actives: dict[str, list[tuple[GraphRelationship, dict[str, object]]]] = {}
        scope_context_counts: dict[str, int] = {}
        scope_context_type_counts: dict[str, dict[str, int]] = {}
        for relationship in self._graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            props = relationship.properties
            scope_key = props.get("scope_key")
            if not isinstance(scope_key, str):
                continue
            if scoped_job_key is not None and scope_key != scoped_job_key:
                continue
            status = props.get("status")
            if status != RelationshipStatus.ACTIVE.value:
                continue
            # Validity window: valid_to must not have elapsed.
            if self._is_active_past_valid_to(relationship, now):
                continue
            # WS-4 demotion: demoted members are already out of context.
            if props.get("active_in_context") is False:
                continue
            scope_context_counts[scope_key] = scope_context_counts.get(scope_key, 0) + 1
            memory_type = str(props.get("memory_type", "")) or "unknown"
            type_counts = scope_context_type_counts.setdefault(scope_key, {})
            type_counts[memory_type] = type_counts.get(memory_type, 0) + 1
            eligible, eligibility_reason, policy, policy_source = self._retention_eligibility(
                job=job,
                relationship=relationship,
                now=now,
                reason="soft_cap_exceeded",
            )
            components = self._retention_components(
                relationship=relationship,
                now=now,
                policy=policy,
                policy_source=policy_source,
            )
            components.update(
                {
                    "eligibility": "eligible" if eligible else "held",
                    "eligibility_reason": eligibility_reason,
                    "prune_reason": "soft_cap_exceeded",
                }
            )
            if not eligible:
                await self._record_decision(
                    job=job,
                    now=now,
                    decision_type="pruning_retention_held",
                    summary=(
                        f"Retention gate held relationship {relationship.uuid} out of soft-cap pruning: "
                        f"{eligibility_reason}."
                    ),
                    subject_id=relationship.uuid,
                    subject_name=str(props.get("fact", relationship.uuid)),
                    scope=self._relationship_scope(relationship),
                    details=components,
                )
                continue
            scope_actives.setdefault(scope_key, []).append((relationship, components))

        for scope_key, context_count in scope_context_counts.items():
            candidates = scope_actives.get(scope_key, [])
            remaining_budget = (
                job.max_items_per_run - run.pruned_relationships - run.superseded_relationships - total_pruned
            )
            pruned_ids: set[str] = set()

            # --- Global soft cap ---
            if growth.soft_cap is not None and context_count > growth.soft_cap and remaining_budget > 0:
                # Lowest expected loss leaves context first.
                sorted_by_value = sorted(candidates, key=lambda item: float(item[1]["retention_value"]))
                to_prune_count = min(context_count - growth.soft_cap, remaining_budget, len(sorted_by_value))
                for rel, components in sorted_by_value[:to_prune_count]:
                    decision = await self._pruning_decision(
                        job=job,
                        now=now,
                        receipt_run=receipt_run,
                        relationship=rel,
                        decision_type="pruning_context_budget_exceeded",
                        proposed_action=(
                            f"Prune lowest-value active relationship because the scope "
                            f"context-visible count ({context_count}) exceeds the soft cap ({growth.soft_cap})."
                        ),
                        reason="soft_cap_exceeded",
                        details={
                            "soft_cap": growth.soft_cap,
                            "context_visible_count": context_count,
                            "retention_components": components,
                        },
                    )
                    run.decision_count += 1
                    if not decision.approved:
                        continue
                    self._mark_and_receipt_pruned(
                        receipt_run,
                        relationship=rel,
                        status=RelationshipStatus.PRUNED,
                        valid_to=now,
                        properties={
                            "pruned_reason": "soft_cap_exceeded",
                            "pruned_soft_cap": growth.soft_cap,
                        },
                        reason="soft_cap_exceeded",
                        now=now,
                        decision_type=ReceiptDecisionType.PRUNING_SOFT_CAP_PRUNED,
                        retention_components=components,
                    )
                    total_pruned += 1
                    remaining_budget -= 1
                    pruned_ids.add(rel.uuid)

            # --- Per-type soft caps ---
            if growth.per_type_soft_caps:
                # Group candidates by memory_type.
                by_type: dict[str, list[tuple[GraphRelationship, dict[str, object]]]] = {}
                for rel, components in candidates:
                    if rel.uuid in pruned_ids:
                        continue
                    mem_type = str(rel.properties.get("memory_type", "")) or "unknown"
                    by_type.setdefault(mem_type, []).append((rel, components))

                for type_key, type_cap in growth.per_type_soft_caps.items():
                    type_candidates = by_type.get(type_key, [])
                    type_count = scope_context_type_counts[scope_key].get(type_key, 0)
                    type_count -= sum(
                        1
                        for relationship_uuid in pruned_ids
                        if str(self._graph.get_relationship(relationship_uuid).properties.get("memory_type", ""))
                        == type_key
                    )
                    remaining_budget = (
                        job.max_items_per_run - run.pruned_relationships - run.superseded_relationships - total_pruned
                    )
                    if type_count > type_cap and remaining_budget > 0:
                        sorted_by_value = sorted(type_candidates, key=lambda item: float(item[1]["retention_value"]))
                        to_prune_count = min(type_count - type_cap, remaining_budget, len(sorted_by_value))
                        for rel, components in sorted_by_value[:to_prune_count]:
                            decision = await self._pruning_decision(
                                job=job,
                                now=now,
                                receipt_run=receipt_run,
                                relationship=rel,
                                decision_type="pruning_context_budget_exceeded",
                                proposed_action=(
                                    f"Prune lowest-value active relationship of type {type_key!r} because the "
                                    f"per-type context-visible count ({type_count}) exceeds the soft cap ({type_cap})."
                                ),
                                reason="soft_cap_exceeded",
                                details={
                                    "per_type_soft_cap": type_cap,
                                    "memory_type": type_key,
                                    "context_visible_count": type_count,
                                    "retention_components": components,
                                },
                            )
                            run.decision_count += 1
                            if not decision.approved:
                                continue
                            self._mark_and_receipt_pruned(
                                receipt_run,
                                relationship=rel,
                                status=RelationshipStatus.PRUNED,
                                valid_to=now,
                                properties={
                                    "pruned_reason": "soft_cap_exceeded",
                                    "pruned_soft_cap_type": type_key,
                                    "pruned_soft_cap": type_cap,
                                },
                                reason="soft_cap_exceeded",
                                now=now,
                                decision_type=ReceiptDecisionType.PRUNING_SOFT_CAP_PRUNED,
                                retention_components=components,
                            )
                            total_pruned += 1
                            remaining_budget -= 1
                            pruned_ids.add(rel.uuid)

        return total_pruned

    def _recency_for_soft_cap(self, relationship: object, now: datetime) -> float:
        """Exponential recency decay for soft-cap value scoring.

        Uses last_seen_at (most recently reinforced) as the reference point so
        frequently-reinforced facts retain high recency.  Falls back to valid_from
        then created_at.  Half-life = 7 days (604800 s) — a soft-cap-appropriate
        window for distinguishing recent from stale actives.
        """

        half_life = 604800.0  # 7 days in seconds
        for attr in ("last_seen_at", "valid_from"):
            raw = relationship.properties.get(attr) if hasattr(relationship, "properties") else None
            if isinstance(raw, str):
                try:
                    ts = datetime.fromisoformat(raw)
                    age = (now - ts).total_seconds()
                    if age <= 0:
                        return 1.0
                    lam = math.log(2.0) / half_life
                    return math.exp(-lam * age)
                except (ValueError, TypeError):
                    pass
        try:
            ts = relationship.created_at
            age = (now - ts).total_seconds()
            if age <= 0:
                return 1.0
            lam = math.log(2.0) / half_life
            return math.exp(-lam * age)
        except (TypeError, AttributeError):
            return 1.0

    def _retention_policy_for_relationship(
        self, *, job: DreamJob, relationship: GraphRelationship
    ) -> tuple[RetentionPolicy, str]:
        motive_name = relationship.properties.get("motive_name") or job.motive
        if isinstance(motive_name, str) and self._config.memory_bank is not None:
            try:
                motive = self._config.memory_bank.motive(motive_name)
            except ValueError:
                # A Motive can be renamed or removed from the bank after memories
                # were formed under it.  Retention must stay decidable for those
                # rows: fall back to the config default and surface the orphan in
                # the receipted policy_source instead of crashing the pruning run.
                return (
                    self._config.pruning.retention,
                    f"config.pruning.retention:orphaned_motive:{motive_name}",
                )
            if motive.retention is not None:
                return motive.retention, f"motive:{motive.name}"
        return self._config.pruning.retention, "config.pruning.retention"

    def _retention_eligibility(
        self,
        *,
        job: DreamJob,
        relationship: GraphRelationship,
        now: datetime,
        reason: str,
    ) -> tuple[bool, str, RetentionPolicy, str]:
        """Apply non-negotiable gates before any loss-aware eviction ranking."""
        policy, policy_source = self._retention_policy_for_relationship(job=job, relationship=relationship)
        props = relationship.properties
        memory_type = self.memory_type_for_relationship(dict(props))
        if reason == "valid_to_elapsed":
            return True, "valid_time_elapsed", policy, policy_source
        raw_until = props.get("retention_protected_until")
        if isinstance(raw_until, str):
            try:
                if datetime.fromisoformat(raw_until) > now:
                    return False, "delay_hold_active", policy, policy_source
            except ValueError as exc:
                raise ValueError(f"relationship {relationship.uuid} has invalid retention_protected_until") from exc
        raw_lifted_at = props.get("retention_protection_lifted_at")
        if isinstance(raw_lifted_at, str):
            try:
                lifted_at = datetime.fromisoformat(raw_lifted_at)
            except ValueError as exc:
                raise ValueError(
                    f"relationship {relationship.uuid} has invalid retention_protection_lifted_at"
                ) from exc
            if lifted_at.tzinfo is None:
                raise ValueError(f"relationship {relationship.uuid} has naive retention_protection_lifted_at")
            if lifted_at.timestamp() + policy.protection_grace_seconds > now.timestamp():
                return False, "protection_lift_grace_active", policy, policy_source
        if props.get("coherence_hold"):
            return False, "coherence_hold", policy, policy_source
        if props.get("secret_reference") and props.get("secret_lifecycle", "active") in {"active", "rotated"}:
            return False, "active_secret_reference", policy, policy_source
        if props.get("claim_mode") == "correction":
            return False, "operator_correction", policy, policy_source
        # WS-20 T23: an operator pin is a guaranteed-retention hold — pruning
        # must never archive a pinned row (like operator corrections).
        if props.get("pinned") is True:
            return False, "pinned", policy, policy_source
        if props.get("status") == RelationshipStatus.ACTIVE.value and memory_type in {
            item.value for item in policy.protected_memory_types
        }:
            # A directive with consistently harmful *outcomes* is deliberately
            # not preserved because of past exposure or an old type floor.
            scope_key = str(props.get("scope_key"))
            utility = self._graph.utility_projection(
                scope_key=scope_key, relationship_uuid=relationship.uuid, as_of=now
            )
            if not (
                memory_type == MemoryType.DIRECTIVE.value
                and utility
                and utility[0].negative_outcome_count > utility[0].positive_outcome_count
            ):
                return False, f"protected_memory_type:{memory_type}", policy, policy_source
        return True, "eligible", policy, policy_source

    def _retention_components(
        self,
        *,
        relationship: GraphRelationship,
        now: datetime,
        policy: RetentionPolicy,
        policy_source: str,
    ) -> dict[str, object]:
        """Three-estimand retention value; no raw popularity term is permitted."""
        props = relationship.properties
        scope_key = str(props.get("scope_key"))
        utility_rows = self._graph.utility_projection(
            scope_key=scope_key, relationship_uuid=relationship.uuid, as_of=now
        )
        utility = utility_rows[0] if utility_rows else None
        observation_recency = self._recency_for_soft_cap(relationship, now)
        use_stability = utility.use_stability if utility is not None else 0.0
        outcome_quality = utility.outcome_quality if utility is not None else 0.5
        positive = utility.positive_outcome_count if utility is not None else 0
        negative = utility.negative_outcome_count if utility is not None else 0
        # WS-15 T11: one shared estimand — retention and the stage-6 rerank
        # utility term read the exact same formula (retrieval.use_need).
        use_need = compute_use_need(use_stability=use_stability, outcome_quality=outcome_quality)
        use_recency_weight, observation_recency_weight, regret = self._adaptive_retention_weights(
            relationship=relationship,
            policy=policy,
        )
        total_weight = use_recency_weight + observation_recency_weight
        future_need = (use_recency_weight * use_need + observation_recency_weight * observation_recency) / total_weight
        if negative > positive:
            future_need *= max(0.01, (positive + 1.0) / (negative + 1.0) ** 2)
        memory_type = self.memory_type_for_relationship(dict(props)) or "unknown"
        severity = {
            "anchor": 25.0,
            "requirement": 30.0,
            "decision": 20.0,
            "incident": 18.0,
            "directive": 12.0,
            "rollup": 10.0,
            "preference": 8.0,
            "state": 4.0,
        }.get(memory_type, 5.0)
        if props.get("claim_mode") == "correction":
            severity *= 4.0
        if props.get("secret_reference"):
            severity *= 4.0
        episode_uuids = props.get("episode_uuids")
        receipt_backed = any(
            receipt.relationship_uuid == relationship.uuid
            and receipt.episode_uuid is not None
            and receipt.decision_type
            in {
                ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
                ReceiptDecisionType.FORMATION_SEMANTIC_DEDUP_REINFORCED,
            }
            for receipt in self._graph.receipts.receipts_for_scope(scope_key)
        )
        recoverable = isinstance(episode_uuids, list) and bool(episode_uuids) and receipt_backed
        recovery_cost = 2.0 + 0.25 * len(episode_uuids) if recoverable else 1_000_000.0
        fact = str(props.get("fact", ""))
        retention_cost = 1.0 + (len(fact) / 4096.0)
        loss_if_absent = severity + recovery_cost
        value = future_need * loss_if_absent / retention_cost
        return {
            "policy_source": policy_source,
            "future_need": future_need,
            "observation_recency": observation_recency,
            "use_stability": use_stability,
            "use_recency_weight": use_recency_weight,
            "observation_recency_weight": observation_recency_weight,
            "ghost_regret": regret,
            "outcome_quality": outcome_quality,
            "positive_outcomes": positive,
            "negative_outcomes": negative,
            "loss_if_absent": loss_if_absent,
            "recovery_cost": recovery_cost,
            "recovery_path": (
                f"receipt_chain:relationships/{relationship.uuid};episodes:{','.join(episode_uuids)}"
                if recoverable
                else "unrecoverable_operator_fact"
            ),
            "retention_cost": retention_cost,
            "retention_value": value,
        }

    def _adaptive_retention_weights(
        self,
        *,
        relationship: GraphRelationship,
        policy: RetentionPolicy,
    ) -> tuple[float, float, dict[str, object]]:
        """Derive bounded ARC-style recency weights from matching ghost regret.

        This is a deterministic projection over reversible-pruning history, not
        a hidden mutable tuning knob.  A restore indicates that the prior
        candidate ranking underweighted demonstrated use; once enough samples
        exist, shift a bounded portion from observation recency to use recency
        for that exact Motive/type cohort.  Every resulting prune receipt
        carries the cohort rate and effective weights.
        """
        props = relationship.properties
        scope_key = str(props.get("scope_key"))
        memory_type = self.memory_type_for_relationship(dict(props)) or "unknown"
        motive_name = props.get("motive_name")
        cohort = []
        for ghost in self._graph.prune_ghosts(scope_key=scope_key):
            try:
                pruned = self._graph.get_relationship(ghost.relationship_uuid)
            except ValueError:
                continue
            pruned_props = pruned.properties
            if self.memory_type_for_relationship(dict(pruned_props)) != memory_type:
                continue
            if pruned_props.get("motive_name") != motive_name:
                continue
            cohort.append(ghost)
        restored = sum(ghost.restored_at is not None for ghost in cohort)
        regret_rate = restored / len(cohort) if cohort else 0.0
        use_weight = policy.use_recency_weight
        observation_weight = policy.observation_recency_weight
        adapted = False
        if (
            policy.adaptive_regret_enabled
            and len(cohort) >= policy.adaptive_regret_min_samples
            and regret_rate > policy.ghost_regret_alert_rate
            and observation_weight > 0
        ):
            shift = min(policy.adaptive_regret_step, observation_weight)
            use_weight += shift
            observation_weight -= shift
            adapted = True
        return (
            use_weight,
            observation_weight,
            {
                "cohort_memory_type": memory_type,
                "cohort_motive_name": motive_name,
                "ghost_count": len(cohort),
                "ghost_restored_count": restored,
                "ghost_regret_rate": regret_rate,
                "ghost_regret_alert": regret_rate > policy.ghost_regret_alert_rate,
                "adapted": adapted,
            },
        )

    async def _repair_single_active_conflicts(
        self,
        *,
        job: DreamJob,
        now: datetime,
        max_repairs: int,
        receipt_run: ReceiptRun,
    ) -> tuple[int, int]:
        if max_repairs <= 0:
            return 0, 0
        grouped: dict[str, list[GraphRelationship]] = {}
        for relationship in self._graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            if job.scope is not None and relationship.properties.get("scope_key") != job.scope.key:
                continue
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            if self._is_active_past_valid_to(relationship, now):
                continue
            if relationship.properties.get("truth_cardinality") != RelationshipCardinality.SINGLE_ACTIVE.value:
                continue
            raw_truth_key = relationship.properties.get("truth_key")
            if not isinstance(raw_truth_key, str) or not raw_truth_key.strip():
                raise ValueError(f"single-active relationship missing truth_key: {relationship.uuid}")
            grouped.setdefault(raw_truth_key, []).append(relationship)

        repaired = 0
        decisions = 0
        for relationships in grouped.values():
            if len(relationships) <= 1:
                continue
            winner = self._single_active_winner(relationships)
            for relationship in self._single_active_losers(relationships, winner):
                if repaired >= max_repairs:
                    return repaired, decisions
                decision = await self._pruning_decision(
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    relationship=relationship,
                    decision_type="pruning_single_active_repair",
                    proposed_action="Supersede an older active relationship to repair a single-active truth conflict.",
                    reason="single_active_truth_repair",
                    details={
                        "winner_relationship_uuid": winner.uuid,
                        "winner_valid_from": winner.valid_from.isoformat() if winner.valid_from else None,
                    },
                )
                decisions += 1
                if not decision.approved:
                    continue
                self._mark_and_receipt_pruned(
                    receipt_run,
                    relationship=relationship,
                    status=RelationshipStatus.SUPERSEDED,
                    valid_to=self._repair_valid_to(relationship, winner, now),
                    properties={
                        "superseded_at": now.isoformat(),
                        "superseded_by_relationship_uuid": winner.uuid,
                        "superseded_reason": "single_active_truth_repair",
                    },
                    reason="single_active_truth_repair",
                    now=now,
                    decision_type=ReceiptDecisionType.PRUNING_SINGLE_ACTIVE_REPAIRED,
                    decision_result="superseded",
                    successor_relationship_uuid=winner.uuid,
                )
                repaired += 1
        return repaired, decisions

    def _single_active_winner(self, relationships: list[GraphRelationship]) -> GraphRelationship:
        return max(
            relationships,
            key=lambda relationship: (
                self._relationship_truth_time(relationship),
                float(relationship.properties.get("confidence", 0.0)),
                self._last_observed_at(relationship),
                relationship.created_at,
                relationship.uuid,
            ),
        )

    def _single_active_losers(
        self,
        relationships: list[GraphRelationship],
        winner: GraphRelationship,
    ) -> list[GraphRelationship]:
        losers = [relationship for relationship in relationships if relationship.uuid != winner.uuid]
        losers.sort(
            key=lambda relationship: (
                self._relationship_truth_time(relationship),
                relationship.created_at,
                relationship.uuid,
            )
        )
        return losers

    def _relationship_truth_time(self, relationship: GraphRelationship) -> datetime:
        return relationship.valid_from or self._last_observed_at(relationship)

    def _repair_valid_to(
        self,
        relationship: GraphRelationship,
        winner: GraphRelationship,
        now: datetime,
    ) -> datetime:
        if winner.valid_from is not None:
            return winner.valid_from
        if relationship.valid_from is not None:
            return max(now, relationship.valid_from)
        return now

    def _is_superseded_past_retention(self, relationship: GraphRelationship, now: datetime) -> bool:
        if relationship.valid_to is None:
            return False
        return now - relationship.valid_to >= self._config.pruning.superseded_retention_for(relationship.type)

    def _is_active_past_max_age(self, relationship: GraphRelationship, now: datetime) -> bool:
        max_age = self._config.pruning.active_max_age_for(relationship.type)
        if max_age is None:
            return False
        observed_at = self._last_observed_at(relationship)
        return now - observed_at >= max_age

    def _is_active_stale_unused(self, relationship: GraphRelationship, now: datetime) -> bool:
        """WS-20 T25: is this ACTIVE row past its per-type staleness window AND unused?

        Stale = the row's memory_type has a ``PruningPolicy.stale_after_seconds``
        entry, its age since ``last_seen_at`` (falling back to ``created_at`` --
        transaction time, never ``valid_from``) exceeds that window, AND the use-event store records ZERO
        use events for the row within the window.  A single use event inside the
        window — retrieval, injection, or citation — resets the staleness clock.
        """
        stale_after = self._config.pruning.stale_after_for(
            self.memory_type_for_relationship(dict(relationship.properties))
        )
        if stale_after is None:
            return False
        observed_at = self._last_observed_at(relationship)
        if now - observed_at < stale_after:
            return False
        scope_key = relationship.properties.get("scope_key")
        if not isinstance(scope_key, str) or not scope_key:
            return False
        window_start = now - stale_after
        return not any(
            window_start <= event.used_at <= now
            for event in self._graph.use_events(scope_key=scope_key, relationship_uuid=relationship.uuid)
        )

    def _is_active_past_valid_to(self, relationship: GraphRelationship, now: datetime) -> bool:
        return relationship.valid_to is not None and relationship.valid_to <= now

    def _last_observed_at(self, relationship: GraphRelationship) -> datetime:
        """When WE last observed this row — transaction time, never valid time.

        Retention, staleness and confidence decay all ask the same question:
        how long have we held this without seeing it again?  That is measured
        in TRANSACTION time (``last_seen_at``, else ``created_at``).  It must
        never fall back to ``valid_from``, which is VALID time — when the world
        says the fact became true.

        The two diverge badly on any historical document.  Measured: a decision
        log carrying ``<Badge text="2025-12-09">`` produced facts stamped
        ``valid_from=2025-12-09`` and ``created_at=2026-08-13``.  Reading
        ``valid_from`` made them eight months old the instant they were written,
        so the 30-day ``state`` window pruned seven of eight state facts on the
        very first pruning run — including "integration mirrors production" and
        every RPO target, minutes after extraction.  A fact cannot go stale
        before we have had it.

        ``valid_from`` still governs whether a fact is in effect (a row dated
        2027 is correctly not yet valid); it just says nothing about how long
        we have held it.
        """
        last_seen_at = relationship.properties.get("last_seen_at")
        if isinstance(last_seen_at, str):
            return datetime.fromisoformat(last_seen_at)
        return relationship.created_at

    def _effective_confidence(
        self,
        relationship: GraphRelationship,
        *,
        now: datetime,
        stored_confidence: float,
    ) -> float:
        """WS-16 T13: age-discounted confidence for the pruning min-confidence gate.

        ``stored * 2 ** (-age_days / half_life_days)`` with age measured from
        ``last_seen_at`` (falling back to ``created_at`` via
        :meth:`_last_observed_at` -- transaction time, never ``valid_from``).  READ-ONLY — the stored confidence is never
        mutated.  ``half_life_days=None`` (default) returns the stored value,
        keeping pruning byte-identical to the undecayed behavior.
        """
        half_life_days = self._config.confidence.half_life_days
        if half_life_days is None:
            return stored_confidence
        age_days = (now - self._last_observed_at(relationship)).total_seconds() / 86400.0
        if age_days <= 0:
            return stored_confidence
        return stored_confidence * (2.0 ** (-age_days / half_life_days))
