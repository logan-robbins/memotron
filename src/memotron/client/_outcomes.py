"""Recording what a memory was used for and whether it helped.

Nine members. This is where the feedback signal enters: `record_memory_outcome`
(226 lines) is what eventually moves a memory's utility, so a silent failure here
degrades retention quality weeks later with nothing pointing back at it.
`byte_replay` and `counterfactual` are the audit counterparts -- proving what the
system would have done."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    Motive,
)
from memotron.models import (
    DreamDecisionRecord,
    DreamJobKind,
    MemoryScope,
    MemoryUtilityProjection,
    OutcomeEvent,
    OutcomeVerdict,
    RetrievalNegativeSpaceEntry,
    UseEvent,
    UseEventKind,
)
from memotron.receipts import (
    ReceiptDecisionType,
    payload_digest,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    # Annotation-only, and deferred because the runtime imports they mirror are
    # function-local. The package docstring used to say every module here imports
    # `client` back; measured 2026-08-28 that is true only for `replay` (via
    # memotron -> agent_memory -> client). The rest are deferred by convention
    # now, not necessity, and hoisting them is its own commit.
    from memotron.replay import (
        CounterfactualReport,
        MotiveCertificate,
        ReplayProof,
    )


if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class OutcomeMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def record_memory_use(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        kind: UseEventKind,
        task_run_id: str,
        idempotency_key: str,
        used_at: datetime | None = None,
        rank: int | None = None,
        retrieval_score: float | None = None,
        candidate_set_size: int | None = None,
        context_budget_competition: int | None = None,
        retrieval_policy_digest: str | None = None,
        query_digest: str | None = None,
        metadata: dict[str, Any] | None = None,
        use_id: str | None = None,
    ) -> UseEvent:
        """Record a receipt-bound retrieval, injection, or cited-use event.

        This is deliberately explicit: reading a memory does not alter truth or
        utility until the runtime reports the stage it actually reached.
        ``query_digest`` (WS-22 T29) is the RetrievalContract's sha256 of the
        raw search query — search surfaces stamp it on RETRIEVED events so
        repeat-search lineage is measurable; optional and additive.
        """
        self._require_authorized_scope(scope)
        payload: dict[str, Any] = {
            "relationship_uuid": relationship_uuid,
            "scope": scope,
            "kind": kind,
            "task_run_id": task_run_id,
            "idempotency_key": idempotency_key,
            "used_at": used_at or datetime.now(UTC),
            "rank": rank,
            "retrieval_score": retrieval_score,
            "candidate_set_size": candidate_set_size,
            "context_budget_competition": context_budget_competition,
            "retrieval_policy_digest": retrieval_policy_digest,
            "query_digest": query_digest,
            "metadata": dict(metadata or {}),
        }
        if use_id is not None:
            payload["use_id"] = use_id
        event = UseEvent(**payload)
        stored = self.graph.record_use_event(event)
        if stored.use_id != event.use_id:
            return stored
        relationship = self.graph.get_relationship(stored.relationship_uuid)
        run = self._begin_operator_run(job_name="record_memory_use", scope_key=scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.USE_EVENT_RECORDED,
            decision_reason=f"{stored.kind.value}:{stored.task_run_id}",
            decision_result="recorded",
            now=stored.used_at,
            scope_key=scope.key,
            relationship_uuid=stored.relationship_uuid,
            relationship_type=relationship.type,
            use_event_id=stored.use_id,
            event_payload=stored.model_dump_json(),
            event_payload_digest=payload_digest(stored.model_dump(mode="json")),
        )
        self._checkpoint_operator_run(run)
        return stored

    async def record_memory_outcome(
        self,
        *,
        use_id: str,
        scope: MemoryScope,
        verdict: OutcomeVerdict,
        task_run_id: str,
        idempotency_key: str,
        judge_identity: str,
        judge_version: str,
        judged_at: datetime | None = None,
        attribution_method: str = "cited_full_credit",
        metadata: dict[str, Any] | None = None,
        outcome_id: str | None = None,
    ) -> OutcomeEvent:
        """Record a judged result against one use event, never directly against truth."""
        self._require_authorized_scope(scope)
        payload: dict[str, Any] = {
            "use_id": use_id,
            "scope": scope,
            "verdict": verdict,
            "task_run_id": task_run_id,
            "idempotency_key": idempotency_key,
            "judge_identity": judge_identity,
            "judge_version": judge_version,
            "judged_at": judged_at or datetime.now(UTC),
            "attribution_method": attribution_method,
            "metadata": dict(metadata or {}),
        }
        if outcome_id is not None:
            payload["outcome_id"] = outcome_id
        event = OutcomeEvent(**payload)
        stored = self.graph.record_outcome_event(event)
        if stored.outcome_id != event.outcome_id:
            return stored
        use_event = next(event for event in self.graph.use_events(scope_key=scope.key) if event.use_id == use_id)
        relationship = self.graph.get_relationship(use_event.relationship_uuid)
        run = self._begin_operator_run(job_name="record_memory_outcome", scope_key=scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.OUTCOME_EVENT_RECORDED,
            decision_reason=f"{stored.verdict.value}:{stored.attribution_method}",
            decision_result="recorded",
            now=stored.judged_at,
            scope_key=scope.key,
            relationship_uuid=use_event.relationship_uuid,
            relationship_type=relationship.type,
            use_event_id=stored.use_id,
            outcome_event_id=stored.outcome_id,
            event_payload=stored.model_dump_json(),
            event_payload_digest=payload_digest(stored.model_dump(mode="json")),
        )
        self._checkpoint_operator_run(run)
        repaired_artifact_id = stored.metadata.get("repaired_governor_artifact_id")
        monitor = (
            self.graph.active_coherence_repair_monitor(
                scope=scope,
                relationship_uuid=relationship.uuid,
                artifact_id=repaired_artifact_id,
            )
            if stored.metadata.get("repaired_governor") is True
            and isinstance(repaired_artifact_id, str)
            and repaired_artifact_id.strip()
            else None
        )
        if (
            stored.verdict == OutcomeVerdict.POSITIVE
            and monitor is not None
            and relationship.properties.get("coherence_hold") is True
        ):
            before = self.graph.graph_state_hash(scope.key)
            self.graph.update_relationship(
                relationship.uuid,
                properties={
                    "coherence_hold": False,
                    "coherence_hold_released_at": stored.judged_at.isoformat(),
                    "coherence_hold_release_outcome_id": stored.outcome_id,
                },
            )
            after = self.graph.graph_state_hash(scope.key)
            release_run = self._begin_operator_run(job_name="coherence-post-repair-release", scope_key=scope.key)
            self._emit_receipt(
                release_run,
                decision_type=ReceiptDecisionType.COHERENCE_HOLD_RELEASED,
                decision_reason="verified_positive_outcome_under_repaired_governor",
                decision_result="materialized",
                now=stored.judged_at,
                scope_key=scope.key,
                relationship_uuid=relationship.uuid,
                relationship_type=relationship.type,
                use_event_id=stored.use_id,
                outcome_event_id=stored.outcome_id,
                graph_state_hash_before=before,
                graph_state_hash_after=after,
            )
            self._checkpoint_operator_run(release_run)
            self.graph.recover_coherence_repair_monitor(
                scope=scope,
                incident_id=monitor.incident_id,
                recovered_at=stored.judged_at,
            )
            self.graph.record_dream_decision(
                DreamDecisionRecord(
                    ran_at=stored.judged_at,
                    job_name="coherence-post-repair-release",
                    job_kind=DreamJobKind.COHERENCE,
                    agent_id="runtime-outcome",
                    agent_name="runtime-outcome",
                    agent_scope=scope,
                    decision_type="coherence_hold_released_after_verified_recovery",
                    summary="A repaired governor produced a verified positive outcome; coherence hold released.",
                    subject_id=relationship.uuid,
                    subject_name=str(relationship.properties.get("fact", relationship.uuid)),
                    scope=scope,
                    details={
                        "outcome_id": stored.outcome_id,
                        "use_id": stored.use_id,
                        "incident_id": monitor.incident_id,
                        "repaired_artifact_id": monitor.artifact_id,
                    },
                )
            )
        if stored.verdict in {OutcomeVerdict.NEGATIVE, OutcomeVerdict.CORRECTED}:
            if monitor is not None:
                rolled_back = self.graph.rollback_coherence_repair_monitor(
                    scope=scope,
                    incident_id=monitor.incident_id,
                    reopened_at=stored.judged_at,
                )
                rollback_run = self._begin_operator_run(job_name="coherence-post-repair-rollback", scope_key=scope.key)
                state = self.graph.graph_state_hash(scope.key)
                reason = (
                    "negative_outcome_under_repaired_governor:"
                    f"incident={monitor.incident_id};artifact={monitor.artifact_id}"
                )
                self._emit_receipt(
                    rollback_run,
                    decision_type=ReceiptDecisionType.COHERENCE_INCIDENT_REOPENED,
                    decision_reason=reason,
                    decision_result="recorded",
                    now=stored.judged_at,
                    scope_key=scope.key,
                    relationship_uuid=relationship.uuid,
                    use_event_id=stored.use_id,
                    outcome_event_id=stored.outcome_id,
                    graph_state_hash_before=state,
                    graph_state_hash_after=state,
                )
                self._emit_receipt(
                    rollback_run,
                    decision_type=ReceiptDecisionType.COHERENCE_GOVERNOR_ROLLED_BACK,
                    decision_reason=(f"registry_projection_restored:{rolled_back.prior_artifact_version_digest}"),
                    decision_result="superseded",
                    now=stored.judged_at,
                    scope_key=scope.key,
                    relationship_uuid=relationship.uuid,
                    use_event_id=stored.use_id,
                    outcome_event_id=stored.outcome_id,
                    source_span_digest=rolled_back.prior_artifact_version_digest,
                    graph_state_hash_before=state,
                    graph_state_hash_after=state,
                )
                self._checkpoint_operator_run(rollback_run)
                self.graph.record_dream_decision(
                    DreamDecisionRecord(
                        ran_at=stored.judged_at,
                        job_name="coherence-post-repair-rollback",
                        job_kind=DreamJobKind.COHERENCE,
                        agent_id="runtime-outcome",
                        agent_name="runtime-outcome",
                        agent_scope=scope,
                        decision_type="coherence_incident_reopened_and_governor_rolled_back",
                        summary=(
                            "A negative outcome recurred under the repaired governor; "
                            "the incident was reopened and the live registry projection rolled back."
                        ),
                        subject_id=monitor.incident_id,
                        subject_name=monitor.artifact_id,
                        scope=scope,
                        details={
                            "outcome_id": stored.outcome_id,
                            "use_id": stored.use_id,
                            "repaired_artifact_id": monitor.artifact_id,
                            "prior_artifact_version_digest": rolled_back.prior_artifact_version_digest,
                            "repaired_artifact_version_digest": rolled_back.repaired_artifact_version_digest,
                        },
                    )
                )
            utility = self.graph.utility_projection(
                scope_key=scope.key, relationship_uuid=use_event.relationship_uuid, as_of=stored.judged_at
            )
            negative_count = utility[0].negative_outcome_count if utility else 0
            policy = self._coherence_policy_for_scope(scope)
            if negative_count >= policy.negative_outcome_scan_threshold:
                await self.run_coherence_scan(scope=scope, now=stored.judged_at, policy=policy)
                self.graph.record_dream_decision(
                    DreamDecisionRecord(
                        ran_at=stored.judged_at,
                        job_name="coherence-negative-outcome-trigger",
                        job_kind=DreamJobKind.COHERENCE,
                        agent_id="runtime-outcome",
                        agent_name="runtime-outcome",
                        agent_scope=scope,
                        decision_type="coherence_negative_outcome_triggered",
                        summary=(f"Repeated negative outcomes ({negative_count}) triggered a targeted coherence scan."),
                        subject_id=use_event.relationship_uuid,
                        subject_name=str(relationship.properties.get("fact", use_event.relationship_uuid)),
                        scope=scope,
                        details={
                            "negative_outcome_count": negative_count,
                            "threshold": policy.negative_outcome_scan_threshold,
                            "outcome_id": stored.outcome_id,
                            "use_id": stored.use_id,
                        },
                    )
                )
        return stored

    async def record_outcome_judge_rejection(
        self,
        *,
        scope: MemoryScope,
        session_id: str,
        task_run_id: str,
        reason: str,
        judge_identifier: str,
        cited_use_ids: tuple[str, ...] = (),
    ):
        """WS-15 T10: receipt an out-of-contract session-judge response.

        A judge response that fails strict JSON parsing or names a use_id
        outside the actual cited set is rejected wholesale — no verdict from
        an out-of-contract response is trusted.  The rejection itself must be
        auditable, so this emits a hash-chained
        ``SESSION_OUTCOME_JUDGE_REJECTED`` receipt (never the raw model
        output) before the caller re-raises.  Zero outcome events result.
        """
        self._require_authorized_scope(scope)
        if not session_id.strip():
            raise ValueError("session_id cannot be blank")
        if not reason.strip():
            raise ValueError("reason cannot be blank")
        rejection_payload = {
            "session_id": session_id,
            "task_run_id": task_run_id,
            "judge_identifier": judge_identifier,
            "cited_use_ids": sorted(cited_use_ids),
        }
        run = self._begin_operator_run(job_name="session_outcome_judge", scope_key=scope.key)
        receipt = self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.SESSION_OUTCOME_JUDGE_REJECTED,
            decision_reason=reason[:500],
            decision_result="gated",
            now=datetime.now(UTC),
            scope_key=scope.key,
            event_payload=json.dumps(rejection_payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(rejection_payload),
        )
        self._checkpoint_operator_run(run)
        return receipt

    async def memory_utility(
        self,
        *,
        scope: MemoryScope,
        relationship_uuid: str | None = None,
        as_of: datetime | None = None,
        reader_agent_id: str | None = None,
    ) -> list[MemoryUtilityProjection]:
        self._require_authorized_scope(scope)
        projections = self.graph.utility_projection(
            scope_key=scope.key, relationship_uuid=relationship_uuid, as_of=as_of
        )
        if reader_agent_id is None:
            return projections
        # WS-19 T22: agent-plane reads hide restricted rows' utility too — a
        # projection row names its relationship, so it leaks existence.
        visible_by_uuid = {
            row.uuid: self._agent_may_view_relationship(row.properties, reader_agent_id)
            for row in self.graph.relationships_by_uuids([projection.relationship_uuid for projection in projections])
        }
        return [projection for projection in projections if visible_by_uuid.get(projection.relationship_uuid, True)]

    async def replay_memory_utility(
        self,
        *,
        scope: MemoryScope,
        relationship_uuid: str | None = None,
        as_of: datetime | None = None,
    ) -> list[MemoryUtilityProjection]:
        """Rebuild the utility projection exclusively from hash-linked receipts."""
        self._require_authorized_scope(scope)
        return self.graph.utility_projection_from_receipts(
            scope_key=scope.key, relationship_uuid=relationship_uuid, as_of=as_of
        )

    async def retrieval_negative_space(
        self, *, scope: MemoryScope, task_run_id: str | None = None
    ) -> list[RetrievalNegativeSpaceEntry]:
        """Explain impressions that never progressed to injection or actual use."""
        self._require_authorized_scope(scope)
        return self.graph.retrieval_negative_space(scope_key=scope.key, task_run_id=task_run_id)

    async def byte_replay(self, *, run_uuid: str) -> ReplayProof:
        """WS-11: fail-closed byte replay of a receipted run ([0025], claims 2-4)."""
        from memotron.replay import byte_replay as _byte_replay

        return await _byte_replay(self.graph.receipts, run_uuid=run_uuid, graph=self.graph)

    async def counterfactual(
        self,
        *,
        run_uuid: str,
        motive: Motive | str,
    ) -> CounterfactualReport:
        """WS-11: re-gate a recorded candidate stream under an alternate Motive ([0026], claims 16-19).

        Resolves the alternate Motive's gate knobs with the same precedence the
        formation pipeline uses (Motive override > config default): the salience
        floor from the Motive's rubric, the dedup threshold from the Motive or the
        config ``DedupPolicy``, and the governance sensitivity from the Motive or
        config governance.  The re-gate is applied to the RECORDED candidate stream
        with no graph handle, so production memory is never touched (claim 17).
        """
        from memotron.replay import CounterfactualPolicy, counterfactual_evaluation

        resolved = motive if isinstance(motive, Motive) else None
        if resolved is None:
            if self.config.memory_bank is None:
                raise ValueError(
                    f"counterfactual motive {motive!r} not found: pass a Motive object or configure a memory_bank"
                )
            resolved = self.config.memory_bank.motive(motive)

        rubric = resolved.salience_rubric
        salience_threshold = float(rubric.min_salience) if rubric is not None else 0.0
        dedup_threshold = (
            float(resolved.dedup_threshold)
            if resolved.dedup_threshold is not None
            else float(self.config.dedup.cosine_threshold)
        )
        governance = resolved.governance or self.config.governance
        sensitivity = (
            governance.pii_sensitivity.value
            if governance is not None and governance.pii_sensitivity is not None
            else None
        )
        policy = CounterfactualPolicy(
            allowed_memory_types=tuple(t.value for t in resolved.allowed_memory_types),
            salience_threshold=salience_threshold,
            dedup_threshold=dedup_threshold,
            governance_sensitivity=sensitivity,
        )
        return await counterfactual_evaluation(
            self.graph.receipts, policy, run_uuid=run_uuid, alternate_motive=resolved
        )

    async def certify_motive(
        self,
        *,
        motive: Motive,
        corpus: Any,
        scope: MemoryScope,
    ) -> MotiveCertificate:
        """WS-11: certify a Motive over a fixture corpus ([0028], claim 14)."""
        self._require_authorized_scope(scope)
        from memotron.replay import certify_motive as _certify_motive

        return await _certify_motive(self, motive=motive, corpus=corpus, scope=scope)
