"""Changing what the graph asserts, and reconciling the consequences.

Fifteen members, and this is TWO adjacency blocks deliberately merged: coherence
scanning/remediation and the forget/pin/correct verbs. They call each other --
`remediate_coherence` forgets a memory, and `forget_memory` triggers a scan -- so
splitting them by line number left a two-module cycle. One concern.

Nothing here deletes. `forget_memory` archives, `set_memory_visibility` hides,
`correct_memory` supersedes. That is what makes _archive's restore possible at all."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    CoherencePolicy,
    DreamJob,
)
from memotron.models import (
    ClaimMode,
    CoherenceDisambiguationRequest,
    CoherenceIncident,
    CoherenceIncidentKind,
    CoherenceIncidentResolution,
    CoherenceIncidentStatus,
    CoherenceRemediationReport,
    CoherenceRepairMonitor,
    CoherenceReport,
    CorrectMemoryResult,
    DreamDecisionRecord,
    DreamJobKind,
    Episode,
    EpisodeType,
    ExtractedMemory,
    ForgetMemoryResult,
    MemoryScope,
    MemoryVisibilityResult,
    PinMemoryResult,
    RelationshipStatus,
    ScopeRemediationResult,
)
from memotron.receipts import (
    MemoryReceipt,
    ReceiptDecisionType,
    payload_digest,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class MemoryLifecycleMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def run_coherence_scan(
        self,
        *,
        scope: MemoryScope,
        now: datetime | None = None,
        policy: CoherencePolicy | None = None,
    ) -> CoherenceReport:
        """WS-10: run one cross-artifact coherence scan over *scope*.

        Projects memory + registered skill/file artifacts into a common directive
        representation; raises an ``ESCALATION_WINDUP`` incident when a memory
        directive has escalated across many feedback cycles while a higher-precedence,
        staler non-memory artifact governs the same subject, and a
        ``CROSS_ARTIFACT_CONTRADICTION`` incident when two artifact classes assert
        conflicting stances on the same subject.  Each incident is attributed to its
        governing artifact and recorded as an auditable dream-agent decision (visible
        via ``dream_decisions`` and ``coherence_incidents``).  Returns the full report.
        """
        self._require_authorized_scope(scope)
        ran_at = now or datetime.now(UTC)
        scan_policy = policy or CoherencePolicy()
        job = DreamJob(
            name="coherence-scan",
            kind=DreamJobKind.COHERENCE,
            cadence_seconds=1,
            scope=scope,
            coherence_policy=scan_policy,
        )
        return await self._engine.scan_coherence(scope=scope, job=job, now=ran_at, policy=scan_policy)

    async def coherence_incidents(
        self,
        *,
        scope: MemoryScope | None = None,
        status: CoherenceIncidentStatus | None = None,
        limit: int = 20,
    ) -> list[CoherenceIncident]:
        """WS-10: query recorded cross-artifact coherence incidents.

        Reconstructs ``CoherenceIncident`` objects from the persisted dream-decision
        audit log (so incidents survive process restarts), most recent first.

        WS-16 T14: an incident's ``status`` reflects the LATEST recorded operator
        transition (``resolve_coherence_incident``): OPEN at detection, then
        ACKNOWLEDGED/RESOLVED folded in from the durable transition decisions.
        """
        self._require_explicit_authorized_scope(scope, operation="coherence_incidents")
        incidents: list[CoherenceIncident] = []
        # Filter to coherence decisions at the query layer (seeks the decision_type index)
        # so a flood of formation/consolidation/pruning decisions cannot push coherence
        # incidents out of the read window. Coherence decisions are sparse (one per real
        # incident), so over-fetching for scope/status attrition — and for the status
        # transitions interleaved in the same prefix — is cheap and safe.
        fetch_limit = max(limit * 5, 200)
        records = self.graph.dream_decisions(limit=fetch_limit, decision_type_prefix="coherence_")
        # Newest-first: the first transition seen per incident is its current status.
        latest_transitions: dict[str, CoherenceIncidentStatus] = {}
        for record in records:
            transition = record.details.get("status_transition")
            if not isinstance(transition, dict):
                continue
            incident_id = str(transition.get("incident_id") or "")
            if not incident_id or incident_id in latest_transitions:
                continue
            try:
                latest_transitions[incident_id] = CoherenceIncidentStatus(str(transition.get("to_status")))
            except ValueError as exc:
                raise RuntimeError("persisted coherence status transition is invalid") from exc
        for record in records:
            payload = record.details.get("incident")
            if not isinstance(payload, dict):
                continue
            incident = CoherenceIncident.model_validate(payload)
            folded_status = latest_transitions.get(incident.incident_id)
            if folded_status is not None:
                incident = incident.model_copy(update={"status": folded_status})
            if scope is not None and incident.scope.key != scope.key:
                continue
            if status is not None and incident.status != status:
                continue
            incidents.append(incident)
            if len(incidents) >= limit:
                break
        return incidents

    async def coherence_disambiguation_requests(
        self, *, scope: MemoryScope | None = None, limit: int = 20
    ) -> list[CoherenceDisambiguationRequest]:
        """Return evidence requests emitted for low-confidence attribution.

        WS-16 T14: a request's ``status`` is reconstructed from the audit log —
        ``"pending"`` until an operator records the requested evidence via
        ``resolve_disambiguation_request``, then ``"resolved"`` with the supplied
        runtime trace, artifact version, and operator statement folded onto the
        ``resolution_*`` fields.
        """
        self._require_explicit_authorized_scope(scope, operation="coherence_disambiguation_requests")
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        requests: list[CoherenceDisambiguationRequest] = []
        records = self.graph.dream_decisions(limit=max(limit * 10, 200), decision_type_prefix="coherence_")
        # Newest-first: the first resolution seen per incident is authoritative.
        resolutions: dict[str, dict[str, Any]] = {}
        for record in records:
            resolution = record.details.get("disambiguation_resolution")
            if not isinstance(resolution, dict):
                continue
            incident_id = str(resolution.get("incident_id") or "")
            if incident_id and incident_id not in resolutions:
                resolutions[incident_id] = resolution
        for record in records:
            payload = record.details.get("disambiguation_request")
            if not isinstance(payload, dict):
                continue
            try:
                request_scope_key = str(payload["scope_key"])
                kind, scope_id = request_scope_key.split(":", 1)
                request = CoherenceDisambiguationRequest(
                    incident_id=str(payload["incident_id"]),
                    scope=MemoryScope(kind=kind, scope_id=scope_id),
                    subject=str(payload["subject"]),
                    requested_at=datetime.fromisoformat(str(payload["requested_at"])),
                    attribution_confidence=float(payload["attribution_confidence"]),
                    minimum_confidence=float(payload["minimum_confidence"]),
                    governing_artifact_id=(
                        str(payload["governing_artifact_id"])
                        if payload.get("governing_artifact_id") is not None
                        else None
                    ),
                    required_evidence=tuple(str(item) for item in payload["required_evidence"]),
                    status=str(payload.get("status", "pending")),
                )
                resolution = resolutions.get(request.incident_id)
                if resolution is not None:
                    request = request.model_copy(
                        update={
                            "status": "resolved",
                            "resolution_runtime_trace": str(resolution["runtime_trace"]),
                            "resolution_artifact_version": str(resolution["artifact_version"]),
                            "resolution_statement": str(resolution["statement"]),
                            "resolved_by": str(resolution["resolved_by"]),
                            "resolved_at": datetime.fromisoformat(str(resolution["resolved_at"])),
                        }
                    )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError("persisted coherence disambiguation request is invalid") from exc
            if scope is not None and request.scope.key != scope.key:
                continue
            requests.append(request)
            if len(requests) >= limit:
                break
        return requests

    _COHERENCE_STATUS_ORDER: ClassVar[dict[CoherenceIncidentStatus, int]] = {
        CoherenceIncidentStatus.OPEN: 0,
        CoherenceIncidentStatus.ACKNOWLEDGED: 1,
        CoherenceIncidentStatus.RESOLVED: 2,
    }

    async def resolve_coherence_incident(
        self,
        *,
        incident_id: str,
        scope: MemoryScope,
        status: Literal["acknowledged", "resolved"],
        statement: str,
        resolved_by: str,
        allow_held: bool = False,
        now: datetime | None = None,
    ) -> CoherenceIncidentResolution:
        """WS-16 T14: durably transition a coherence incident's lifecycle status.

        Transitions are forward-only (OPEN -> ACKNOWLEDGED -> RESOLVED); a
        backward or repeated transition fails fast.  Each transition is receipted
        (``COHERENCE_INCIDENT_STATUS_CHANGED``) in an operator run and recorded as
        a coherence dream decision, from which ``coherence_incidents`` folds the
        incident's current status.

        Resolving an incident does NOT release any anti-windup ``coherence_hold``
        it applied: per WS-10, hold release stays outcome-driven — a hold lifts
        only after a judged positive outcome under the repaired governing
        artifact (``record_memory_outcome``), never by operator paperwork, so a
        resolved incident record can never silently re-enable the runaway
        escalation it arrested.  Passing ``status="resolved"`` while the
        incident's hold is still in force therefore fails fast unless the caller
        explicitly acknowledges the surviving hold with ``allow_held=True``.
        """
        self._require_authorized_scope(scope)
        normalized_incident = incident_id.strip()
        normalized_statement = statement.strip()
        normalized_resolved_by = resolved_by.strip()
        if not normalized_incident:
            raise ValueError("incident_id cannot be blank")
        if not normalized_statement:
            raise ValueError("statement cannot be blank")
        if not normalized_resolved_by:
            raise ValueError("resolved_by cannot be blank")
        if status not in {"acknowledged", "resolved"}:
            raise ValueError(f"status must be 'acknowledged' or 'resolved', got {status!r}")
        new_status = CoherenceIncidentStatus(status)

        incidents = await self.coherence_incidents(scope=scope, limit=500)
        incident = next((item for item in incidents if item.incident_id == normalized_incident), None)
        if incident is None:
            raise ValueError(f"coherence incident {normalized_incident!r} was not found in scope {scope.key}")
        current_status = incident.status
        if self._COHERENCE_STATUS_ORDER[new_status] <= self._COHERENCE_STATUS_ORDER[current_status]:
            raise ValueError(
                f"coherence incident status can only move forward: "
                f"{current_status.value} -> {new_status.value} is not allowed"
            )
        if new_status == CoherenceIncidentStatus.RESOLVED and not allow_held:
            held_uuids = [
                relationship.uuid
                for relationship in self._scope_memory_relationships(scope)
                if relationship.properties.get("coherence_hold")
                and relationship.properties.get("coherence_incident_id") == normalized_incident
            ]
            if held_uuids:
                raise ValueError(
                    f"coherence incident {normalized_incident!r} still holds "
                    f"{held_uuids} under an unreleased coherence_hold; hold release is "
                    "outcome-driven (record_memory_outcome under the repaired governor) — "
                    "pass allow_held=True to resolve the incident record while the hold persists"
                )

        resolved_at = now or datetime.now(UTC)
        state_hash = self.graph.graph_state_hash(scope.key)
        run = self._begin_operator_run(job_name="resolve_coherence_incident", scope_key=scope.key)
        receipt = self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.COHERENCE_INCIDENT_STATUS_CHANGED,
            decision_reason=(f"{normalized_incident}:{current_status.value}->{new_status.value}"),
            decision_result="recorded",
            now=resolved_at,
            scope_key=scope.key,
            relationship_uuid=(
                incident.escalating_directive.relationship_uuid if incident.escalating_directive is not None else None
            ),
            graph_state_hash_before=state_hash,
            graph_state_hash_after=state_hash,
        )
        self._checkpoint_operator_run(run)

        job = DreamJob(
            name="coherence-adjudication",
            kind=DreamJobKind.COHERENCE,
            cadence_seconds=1,
            scope=scope,
        )
        self.graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=resolved_at,
                job_name=job.name,
                job_kind=job.kind,
                agent_id=job.agent.agent_id,
                agent_name=job.agent.name,
                agent_scope=job.agent.scope,
                decision_type="coherence_incident_status_changed",
                summary=normalized_statement,
                subject_id=normalized_incident,
                subject_name=incident.subject,
                scope=scope,
                details={
                    "status_transition": {
                        "incident_id": normalized_incident,
                        "from_status": current_status.value,
                        "to_status": new_status.value,
                        "statement": normalized_statement,
                        "resolved_by": normalized_resolved_by,
                        "resolved_at": resolved_at.isoformat(),
                        "allow_held": allow_held,
                    },
                    "receipt_uuid": receipt.receipt_uuid,
                    "receipt_hash": receipt.receipt_hash,
                    "run_uuid": run.run_uuid,
                },
            )
        )
        return CoherenceIncidentResolution(
            incident_id=normalized_incident,
            scope=scope,
            previous_status=current_status,
            status=new_status,
            statement=normalized_statement,
            resolved_by=normalized_resolved_by,
            resolved_at=resolved_at,
        )

    async def resolve_disambiguation_request(
        self,
        *,
        request_id: str,
        scope: MemoryScope,
        runtime_trace: str,
        artifact_version: str,
        statement: str,
        resolved_by: str,
        now: datetime | None = None,
    ) -> CoherenceDisambiguationRequest:
        """WS-16 T14: record the evidence a low-confidence incident asked for.

        ``request_id`` is the disambiguation request's ``incident_id`` (a request
        is keyed by the incident that emitted it).  The supplied runtime trace,
        competing-artifact version metadata, and operator statement — exactly the
        ``required_evidence`` the request enumerated — are receipted
        (``COHERENCE_DISAMBIGUATION_RESOLVED``) in an operator run and recorded
        as a coherence dream decision; ``coherence_disambiguation_requests`` then
        reconstructs the request as ``status="resolved"`` carrying that evidence.
        Recording evidence never holds, demotes, or quarantines anything by
        itself — it unblocks a subsequent human decision.
        """
        self._require_authorized_scope(scope)
        normalized_request = request_id.strip()
        normalized_trace = runtime_trace.strip()
        normalized_version = artifact_version.strip()
        normalized_statement = statement.strip()
        normalized_resolved_by = resolved_by.strip()
        if not normalized_request:
            raise ValueError("request_id cannot be blank")
        if not normalized_trace:
            raise ValueError("runtime_trace cannot be blank")
        if not normalized_version:
            raise ValueError("artifact_version cannot be blank")
        if not normalized_statement:
            raise ValueError("statement cannot be blank")
        if not normalized_resolved_by:
            raise ValueError("resolved_by cannot be blank")

        requests = await self.coherence_disambiguation_requests(scope=scope, limit=500)
        request = next((item for item in requests if item.incident_id == normalized_request), None)
        if request is None:
            raise ValueError(f"disambiguation request {normalized_request!r} was not found in scope {scope.key}")
        if request.status == "resolved":
            raise ValueError(f"disambiguation request {normalized_request!r} is already resolved")

        resolved_at = now or datetime.now(UTC)
        evidence = {
            "incident_id": normalized_request,
            "runtime_trace": normalized_trace,
            "artifact_version": normalized_version,
            "statement": normalized_statement,
            "resolved_by": normalized_resolved_by,
            "resolved_at": resolved_at.isoformat(),
        }
        evidence_digest = payload_digest(evidence)
        state_hash = self.graph.graph_state_hash(scope.key)
        run = self._begin_operator_run(job_name="resolve_disambiguation_request", scope_key=scope.key)
        receipt = self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.COHERENCE_DISAMBIGUATION_RESOLVED,
            decision_reason=f"disambiguation_evidence:{normalized_request}:{evidence_digest}",
            decision_result="recorded",
            now=resolved_at,
            scope_key=scope.key,
            source_span_digest=evidence_digest,
            graph_state_hash_before=state_hash,
            graph_state_hash_after=state_hash,
        )
        self._checkpoint_operator_run(run)

        job = DreamJob(
            name="coherence-adjudication",
            kind=DreamJobKind.COHERENCE,
            cadence_seconds=1,
            scope=scope,
        )
        self.graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=resolved_at,
                job_name=job.name,
                job_kind=job.kind,
                agent_id=job.agent.agent_id,
                agent_name=job.agent.name,
                agent_scope=job.agent.scope,
                decision_type="coherence_disambiguation_resolved",
                summary=normalized_statement,
                subject_id=normalized_request,
                subject_name=request.subject,
                scope=scope,
                details={
                    "disambiguation_resolution": evidence,
                    "evidence_digest": evidence_digest,
                    "receipt_uuid": receipt.receipt_uuid,
                    "receipt_hash": receipt.receipt_hash,
                    "run_uuid": run.run_uuid,
                },
            )
        )
        return request.model_copy(
            update={
                "status": "resolved",
                "resolution_runtime_trace": normalized_trace,
                "resolution_artifact_version": normalized_version,
                "resolution_statement": normalized_statement,
                "resolved_by": normalized_resolved_by,
                "resolved_at": resolved_at,
            }
        )

    async def remediate_coherence(
        self,
        *,
        scopes: list[MemoryScope] | None = None,
        dry_run: bool = True,
        retire_held_directives: bool = False,
        policy: CoherencePolicy | None = None,
        now: datetime | None = None,
    ) -> CoherenceRemediationReport:
        """WS-10: fleet-wide cross-artifact coherence remediation sweep.

        Brings existing memory onto the coherence implementation by scanning each
        scope (with the agent's skills/files registered via
        ``register_artifact_source``) for escalation-windup and cross-artifact
        contradiction incidents accumulated during the coherence-blind era.

        ``dry_run=True`` (the default) previews the plan: it detects and reports
        incidents and their proposed repairs without writing any anti-windup hold,
        recording any decision, or retiring any memory. Run it first, review the
        report, then re-run with ``dry_run=False`` to apply.

        When applied (``dry_run=False``), each windup incident's escalating memory
        directive is marked ``coherence_hold`` (per policy) so formation stops
        strengthening it, and each incident is recorded in the dream-decision audit
        log. Memory retirement is opt-in and approval-gated: only when
        ``retire_held_directives=True`` are the windup-implicated directives
        soft-retired (via ``forget_memory``; evidence and history are preserved —
        never destroyed). Repair of the *governing* artifact (the stale skill/file)
        is surfaced as a proposed repair for the operator; it is never auto-applied.

        ``scopes=None`` sweeps every scope present in the graph
        (``graph.scopes()``); pass a list to target specific scopes.
        """
        if scopes is None:
            self._require_explicit_authorized_scope(None, operation="remediate_coherence")
        else:
            self._require_authorized_scope(*scopes)
        ran_at = now or datetime.now(UTC)
        scan_policy = policy or CoherencePolicy()
        target_scopes = scopes if scopes is not None else self.graph.scopes()
        results: list[ScopeRemediationResult] = []
        for scope in target_scopes:
            # Read-only scopes (shared KBs) must never be mutated: detect + report only,
            # regardless of the requested mode. This forces a dry scan and skips retirement.
            scope_read_only = scope.key in self.config.read_only_scopes
            effective_dry_run = dry_run or scope_read_only
            job = DreamJob(
                name="coherence-remediation",
                kind=DreamJobKind.COHERENCE,
                cadence_seconds=1,
                scope=scope,
                coherence_policy=scan_policy,
            )
            report = await self._engine.scan_coherence(
                scope=scope, job=job, now=ran_at, policy=scan_policy, dry_run=effective_dry_run
            )
            held: list[str] = []
            for incident in report.incidents:
                if (
                    incident.kind == CoherenceIncidentKind.ESCALATION_WINDUP
                    and scan_policy.hold_escalating_directives
                    and incident.escalating_directive is not None
                    and incident.escalating_directive.relationship_uuid is not None
                ):
                    held.append(incident.escalating_directive.relationship_uuid)  # noqa: PERF401 - a comprehension would inline a multi-clause guard
            retired: list[str] = []
            if retire_held_directives and not effective_dry_run:
                # Retire every directive currently under an anti-windup hold in this scope —
                # held by THIS sweep (just written above) or a PRIOR one. Sourcing from the
                # persisted graph (not just this sweep's incidents) makes a deferred
                # apply(retire=True) work after a hold-only apply, since the detector skips
                # already-held directives so they raise no fresh incident. Holds are only ever
                # written for windup incidents, so this targets only windup-implicated rows.
                hold_targets = [
                    relationship.uuid
                    for relationship in self.graph.active_relationships(scope=scope)
                    if relationship.properties.get("coherence_hold")
                ]
                for relationship_uuid in hold_targets:
                    try:
                        await self.forget_memory(
                            relationship_uuid=relationship_uuid,
                            scope=scope,
                            reason="coherence_windup_remediation",
                            now=ran_at,
                        )
                        retired.append(relationship_uuid)
                    except ValueError:
                        # Already retired / not a graph memory relationship — skip.
                        continue
            results.append(
                ScopeRemediationResult(
                    scope=scope,
                    report=report,
                    held_relationship_uuids=held,
                    retired_relationship_uuids=retired,
                )
            )
        return CoherenceRemediationReport(ran_at=ran_at, dry_run=dry_run, scopes=results)

    async def record_coherence_repair(
        self,
        *,
        scope: MemoryScope,
        incident_id: str,
        repaired_artifact_id: str,
        repair_summary: str,
        artifact_version_digest: str,
        now: datetime | None = None,
    ) -> MemoryReceipt:
        """Receipt an operator-confirmed repair of a stale governing skill/file.

        The coherence detector identifies and proposes repair of the governing
        artifact; this method records the repair once it has been applied outside
        the memory graph. The receipt binds the repair to the original incident,
        the repaired artifact id, and the repaired artifact version digest.  A
        repair is monitorable only when the artifact was registered before and
        after repair: the prior durable projection is the rollback target.
        """
        self._require_authorized_scope(scope)
        normalized_incident = incident_id.strip()
        normalized_artifact = repaired_artifact_id.strip()
        normalized_summary = repair_summary.strip()
        normalized_digest = artifact_version_digest.strip()
        if not normalized_incident:
            raise ValueError("incident_id cannot be blank")
        if not normalized_artifact:
            raise ValueError("repaired_artifact_id cannot be blank")
        if not normalized_summary:
            raise ValueError("repair_summary cannot be blank")
        if not normalized_digest:
            raise ValueError("artifact_version_digest cannot be blank")

        incidents = await self.coherence_incidents(scope=scope, limit=500)
        incident = next((item for item in incidents if item.incident_id == normalized_incident), None)
        if incident is None:
            raise ValueError(f"coherence incident {normalized_incident!r} was not found in scope {scope.key}")
        if incident.governing_directive is not None and incident.governing_directive.artifact_id != normalized_artifact:
            raise ValueError(
                "repaired_artifact_id does not match the incident's governing artifact "
                f"({incident.governing_directive.artifact_id!r})"
            )
        escalating_relationship_uuid = (
            incident.escalating_directive.relationship_uuid if incident.escalating_directive is not None else None
        )
        if not escalating_relationship_uuid:
            raise ValueError("post-repair monitoring requires an incident with a memory relationship target")

        recorded_at = now or datetime.now(UTC)
        state_hash = self.graph.graph_state_hash(scope.key)
        repair_digest = payload_digest(
            {
                "incident_id": normalized_incident,
                "artifact_id": normalized_artifact,
                "artifact_version_digest": normalized_digest,
                "repair_summary": normalized_summary,
            }
        )
        run = self._begin_operator_run(job_name="coherence_repair", scope_key=scope.key)
        monitor = self.graph.open_coherence_repair_monitor(
            scope=scope,
            incident_id=normalized_incident,
            relationship_uuid=escalating_relationship_uuid,
            artifact_id=normalized_artifact,
            repaired_artifact_version_digest=normalized_digest,
            opened_at=recorded_at,
        )
        receipt = self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.COHERENCE_GOVERNOR_REPAIR_RECORDED,
            decision_reason=f"coherence_repair:{repair_digest}",
            decision_result="recorded",
            now=recorded_at,
            scope_key=scope.key,
            relationship_uuid=escalating_relationship_uuid,
            source_span_digest=repair_digest,
            graph_state_hash_before=state_hash,
            graph_state_hash_after=state_hash,
        )
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.COHERENCE_REPAIR_MONITOR_OPENED,
            decision_reason=(
                f"post_repair_monitor:{monitor.incident_id}:"
                f"prior={monitor.prior_artifact_version_digest}:"
                f"repaired={monitor.repaired_artifact_version_digest}"
            ),
            decision_result="recorded",
            now=recorded_at,
            scope_key=scope.key,
            relationship_uuid=escalating_relationship_uuid,
            source_span_digest=monitor.repaired_artifact_version_digest,
            graph_state_hash_before=state_hash,
            graph_state_hash_after=state_hash,
        )
        self._checkpoint_operator_run(run)

        job = DreamJob(
            name="coherence-repair",
            kind=DreamJobKind.COHERENCE,
            cadence_seconds=1,
            scope=scope,
        )
        self.graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=recorded_at,
                job_name=job.name,
                job_kind=job.kind,
                agent_id=job.agent.agent_id,
                agent_name=job.agent.name,
                agent_scope=job.agent.scope,
                decision_type=ReceiptDecisionType.COHERENCE_GOVERNOR_REPAIR_RECORDED.value,
                summary=normalized_summary,
                subject_id=normalized_artifact,
                subject_name=normalized_artifact,
                scope=scope,
                details={
                    "incident_id": normalized_incident,
                    "repaired_artifact_id": normalized_artifact,
                    "artifact_version_digest": normalized_digest,
                    "repair_digest": repair_digest,
                    "receipt_uuid": receipt.receipt_uuid,
                    "receipt_hash": receipt.receipt_hash,
                    "run_uuid": run.run_uuid,
                    "repair_monitor": monitor.model_dump(mode="json"),
                },
            )
        )
        return receipt

    def coherence_repair_monitor(self, *, scope: MemoryScope, incident_id: str) -> CoherenceRepairMonitor | None:
        """Read the durable post-repair recovery window for one incident."""
        self._require_authorized_scope(scope)
        return self.graph.coherence_repair_monitor(scope=scope, incident_id=incident_id)

    async def forget_memory(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope | None = None,
        reason: str = "manual_forget",
        now: datetime | None = None,
    ) -> ForgetMemoryResult:
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        relationship = self.graph.get_relationship(relationship_uuid)
        if relationship.type == "MENTIONS":
            raise ValueError("MENTIONS relationships cannot be forgotten directly")
        relationship_scope = self._relationship_scope(relationship.properties)
        self._require_authorized_scope(relationship_scope)
        if scope is not None and relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        previous_status = self._relationship_status(relationship.properties.get("status"))
        pruned_at = now or datetime.now(UTC)
        forget_valid_to = self._forget_valid_to(
            status=previous_status,
            valid_to=relationship.valid_to,
            pruned_at=pruned_at,
        )
        # WS-11/WS-12: the operator forget is a receipted, state-hash-bracketed
        # mutation — no memory-store change happens outside the chain.
        operator_run = self._begin_operator_run(job_name="forget_memory", scope_key=relationship_scope.key)
        forget_state_before = self.graph.graph_state_hash(relationship_scope.key)
        self.graph.mark_relationship(
            relationship.uuid,
            status=RelationshipStatus.PRUNED,
            valid_to=forget_valid_to,
            properties={
                "pruned_reason": normalized_reason,
                "pruned_at": pruned_at.isoformat(),
            },
        )
        forget_state_after = self.graph.graph_state_hash(relationship_scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.PRUNING_RELATIONSHIP_PRUNED,
            decision_reason=normalized_reason,
            decision_result="pruned",
            now=pruned_at,
            scope_key=relationship_scope.key,
            relationship_type=relationship.type,
            memory_type=self._engine.memory_type_for_relationship(dict(relationship.properties)),
            relationship_uuid=relationship.uuid,
            graph_state_hash_before=forget_state_before,
            graph_state_hash_after=forget_state_after,
        )
        # WS-17 T17: duplicates parked behind the forgotten row return to context.
        self._engine.repromote_duplicates_for_dependency(
            relationship_uuid=relationship.uuid,
            reason=normalized_reason,
            now=pruned_at,
            receipt_run=operator_run,
        )
        self._checkpoint_operator_run(operator_run)
        return ForgetMemoryResult(
            relationship_uuid=relationship.uuid,
            previous_status=previous_status,
            status=RelationshipStatus.PRUNED,
            scope=relationship_scope,
            valid_to=forget_valid_to,
            pruned_reason=normalized_reason,
            pruned_at=pruned_at,
        )

    def _pin_target_relationship(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        reason: str,
        pinned_by: str,
    ) -> tuple[Any, str, str]:
        """WS-20 T23 / WS-19 T22: shared fail-fast validation for pin/unpin
        and set_memory_visibility row mutations."""
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        normalized_pinned_by = pinned_by.strip()
        if not normalized_pinned_by:
            raise ValueError("pinned_by cannot be blank")
        relationship = self.graph.get_relationship(relationship_uuid)  # unknown uuid fails fast
        if relationship.type == "MENTIONS":
            raise ValueError("MENTIONS relationships cannot be pinned or visibility-restricted")
        relationship_scope = self._relationship_scope(relationship.properties)
        self._require_authorized_scope(relationship_scope, scope)
        if relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        return relationship, normalized_reason, normalized_pinned_by

    async def pin_memory(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        reason: str,
        pinned_by: str,
        now: datetime | None = None,
    ) -> PinMemoryResult:
        """WS-20 T23: pin one fact for guaranteed retrieval — receipted row state.

        A pinned fact is included in ``profile()`` BEFORE the score-ranked
        budget fill (marked ``[PINNED]``, degrading to a REF line rather than
        dropping when the budget cannot hold it), reserved a slot in every
        ``search``/``search_context`` result over its scope, and held out of
        every pruning path (retention gate reason ``"pinned"``).  Pins are row
        state, not retrieval policy — the RetrievalContract is unchanged.
        Visibility rules still apply: a superseded or pruned pin does not
        surface, and pinning does not block supersession by newer truth.
        """
        relationship, normalized_reason, normalized_pinned_by = self._pin_target_relationship(
            relationship_uuid=relationship_uuid,
            scope=scope,
            reason=reason,
            pinned_by=pinned_by,
        )
        status = self._relationship_status(relationship.properties.get("status"))
        if not self._relationship_is_visible(
            status=status,
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            as_of=None,
            include_statuses=None,
        ):
            raise ValueError(
                f"relationship {relationship.uuid} is not currently visible "
                f"(status={status.value}); only visible active rows can be pinned"
            )
        pinned_at = now or datetime.now(UTC)
        operator_run = self._begin_operator_run(job_name="pin_memory", scope_key=scope.key)
        state_before = self.graph.graph_state_hash(scope.key)
        self.graph.update_relationship(
            relationship.uuid,
            properties={
                "pinned": True,
                "pinned_reason": normalized_reason,
                "pinned_by": normalized_pinned_by,
                "pinned_at": pinned_at.isoformat(),
            },
        )
        state_after = self.graph.graph_state_hash(scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.MEMORY_PINNED,
            decision_reason=normalized_reason,
            decision_result="recorded",
            now=pinned_at,
            scope_key=scope.key,
            relationship_type=relationship.type,
            memory_type=self._engine.memory_type_for_relationship(dict(relationship.properties)),
            relationship_uuid=relationship.uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            event_payload=json.dumps(
                {"pinned": True, "pinned_by": normalized_pinned_by},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self._checkpoint_operator_run(operator_run)
        return PinMemoryResult(
            relationship_uuid=relationship.uuid,
            scope=scope,
            pinned=True,
            reason=normalized_reason,
            pinned_by=normalized_pinned_by,
            pinned_at=pinned_at,
        )

    async def unpin_memory(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        reason: str,
        pinned_by: str,
        now: datetime | None = None,
    ) -> PinMemoryResult:
        """WS-20 T23: remove a pin — the fact returns to normal ranking and retention."""
        relationship, normalized_reason, normalized_pinned_by = self._pin_target_relationship(
            relationship_uuid=relationship_uuid,
            scope=scope,
            reason=reason,
            pinned_by=pinned_by,
        )
        if relationship.properties.get("pinned") is not True:
            raise ValueError(f"relationship {relationship.uuid} is not pinned")
        unpinned_at = now or datetime.now(UTC)
        operator_run = self._begin_operator_run(job_name="unpin_memory", scope_key=scope.key)
        state_before = self.graph.graph_state_hash(scope.key)
        self.graph.update_relationship(
            relationship.uuid,
            properties={
                "pinned": None,
                "pinned_reason": None,
                "pinned_by": None,
                "pinned_at": None,
            },
        )
        state_after = self.graph.graph_state_hash(scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.MEMORY_UNPINNED,
            decision_reason=normalized_reason,
            decision_result="recorded",
            now=unpinned_at,
            scope_key=scope.key,
            relationship_type=relationship.type,
            memory_type=self._engine.memory_type_for_relationship(dict(relationship.properties)),
            relationship_uuid=relationship.uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            event_payload=json.dumps(
                {"pinned": False, "pinned_by": normalized_pinned_by},
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self._checkpoint_operator_run(operator_run)
        return PinMemoryResult(
            relationship_uuid=relationship.uuid,
            scope=scope,
            pinned=False,
            reason=normalized_reason,
            pinned_by=normalized_pinned_by,
            pinned_at=unpinned_at,
        )

    async def set_memory_visibility(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope,
        agents: tuple[str, ...] | list[str] | None,
        reason: str,
        set_by: str,
        now: datetime | None = None,
    ) -> MemoryVisibilityResult:
        """WS-19 T22: set (or clear) a per-memory agent visibility allowlist.

        A receipted, hash-bracketed operator-run row mutation (mirrors
        ``pin_memory``).  ``agents`` is the explicit allowlist of agent ids
        that may read the row; ``None`` clears the restriction and returns the
        row to scope-default visibility.  Enforcement is agent-plane and
        fail-closed: reads that identify a calling agent (``reader_agent_id``)
        see a restricted row only when that agent is listed, while operator /
        SDK-owner reads (``reader_agent_id=None``) are unaffected — search,
        profile, evidence, utility, pinned-row sweeps, and rollup-member
        expansion all honor the same allowlist, so a pinned-but-restricted row
        never leaks to other agents.
        """
        relationship, normalized_reason, normalized_set_by = self._pin_target_relationship(
            relationship_uuid=relationship_uuid,
            scope=scope,
            reason=reason,
            pinned_by=set_by,
        )
        normalized_agents: tuple[str, ...] | None = None
        if agents is not None:
            if not isinstance(agents, (list, tuple)):
                raise ValueError("agents must be a list/tuple of agent ids, or None to clear")
            cleaned = tuple(dict.fromkeys(str(agent).strip() for agent in agents))
            if not cleaned or any(not agent for agent in cleaned):
                raise ValueError(
                    "agents must contain at least one non-blank agent id; pass None to clear the restriction"
                )
            normalized_agents = cleaned
        status = self._relationship_status(relationship.properties.get("status"))
        if not self._relationship_is_visible(
            status=status,
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            as_of=None,
            include_statuses=None,
        ):
            raise ValueError(
                f"relationship {relationship.uuid} is not currently visible "
                f"(status={status.value}); only visible active rows can have "
                "their visibility set"
            )
        if normalized_agents is None and not relationship.properties.get("visibility_agents"):
            raise ValueError(f"relationship {relationship.uuid} has no visibility restriction to clear")
        set_at = now or datetime.now(UTC)
        operator_run = self._begin_operator_run(job_name="set_memory_visibility", scope_key=scope.key)
        state_before = self.graph.graph_state_hash(scope.key)
        self.graph.update_relationship(
            relationship.uuid,
            properties={
                "visibility_agents": (list(normalized_agents) if normalized_agents is not None else None),
            },
        )
        state_after = self.graph.graph_state_hash(scope.key)
        visibility_payload = {
            "visibility_agents": (list(normalized_agents) if normalized_agents is not None else None),
            "set_by": normalized_set_by,
        }
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.MEMORY_VISIBILITY_SET,
            decision_reason=normalized_reason,
            decision_result="recorded",
            now=set_at,
            scope_key=scope.key,
            relationship_type=relationship.type,
            memory_type=self._engine.memory_type_for_relationship(dict(relationship.properties)),
            relationship_uuid=relationship.uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            event_payload=json.dumps(visibility_payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(visibility_payload),
        )
        self._checkpoint_operator_run(operator_run)
        return MemoryVisibilityResult(
            relationship_uuid=relationship.uuid,
            scope=scope,
            visibility_agents=normalized_agents,
            reason=normalized_reason,
            set_by=normalized_set_by,
            set_at=set_at,
        )

    async def correct_memory(
        self,
        *,
        relationship_uuid: str,
        corrected_object: str,
        scope: MemoryScope | None = None,
        corrected_subject: str | None = None,
        corrected_predicate: str | None = None,
        relationship_type: str | None = None,
        confidence: float = 1.0,
        reason: str = "manual_correction",
        source_text: str | None = None,
        metadata: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        instruction_set: str = "default",
        now: datetime | None = None,
    ) -> CorrectMemoryResult:
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason cannot be blank")
        corrected_object_value = corrected_object.strip()
        if not corrected_object_value:
            raise ValueError("corrected_object cannot be blank")
        instructions = self.config.instruction_set(instruction_set)
        target = self.graph.get_relationship(relationship_uuid)
        if target.type == "MENTIONS":
            raise ValueError("MENTIONS relationships cannot be corrected directly")
        target_status = self._relationship_status(target.properties.get("status"))
        if target_status != RelationshipStatus.ACTIVE:
            raise ValueError("only active relationships can be corrected")
        relationship_scope = self._relationship_scope(target.properties)
        self._require_authorized_scope(relationship_scope)
        if scope is not None and relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        corrected_relationship_type = self._normalized_relationship_type(relationship_type or target.type)
        if corrected_relationship_type not in instructions.allowed_relationship_types:
            raise ValueError(
                f"relationship type {corrected_relationship_type!r} is not allowed by instruction set {instruction_set!r}"
            )
        correction_time = valid_from or now or datetime.now(UTC)
        if target.valid_from is not None and correction_time < target.valid_from:
            raise ValueError("correction valid_from cannot be before the target relationship valid_from")
        if confidence < 0.0 or confidence > 1.0:
            raise ValueError("confidence must be between 0.0 and 1.0")

        # WS-12: decrypt-on-read — a crypto-shred row stores sealed names/facts;
        # corrections operate on the revealed text while the scope DEK lives.
        target_subject = str(
            self.graph.reveal(relationship_scope.key, self.graph.get_node(target.source_uuid).properties["name"])
        )
        target_object = str(
            self.graph.reveal(relationship_scope.key, self.graph.get_node(target.target_uuid).properties["name"])
        )
        target_predicate = str(target.properties["predicate"])
        corrected_subject_value = corrected_subject.strip() if corrected_subject is not None else target_subject
        corrected_predicate_value = corrected_predicate.strip() if corrected_predicate is not None else target_predicate
        if not corrected_subject_value:
            raise ValueError("corrected_subject cannot be blank")
        if not corrected_predicate_value:
            raise ValueError("corrected_predicate cannot be blank")
        if self._same_fact(
            subject=target_subject,
            predicate=target_predicate,
            object_value=target_object,
            relationship_type=target.type,
            corrected_subject=corrected_subject_value,
            corrected_predicate=corrected_predicate_value,
            corrected_object=corrected_object_value,
            corrected_relationship_type=corrected_relationship_type,
        ):
            raise ValueError("corrected memory must differ from the target relationship")

        source_node = self.graph.get_node(target.source_uuid)
        target_node = self.graph.get_node(target.target_uuid)
        target_fact = str(self.graph.reveal(relationship_scope.key, target.properties["fact"]))
        correction_metadata = {
            **(metadata or {}),
            "manual_correction": True,
            "corrected_relationship_uuid": target.uuid,
            "correction_reason": normalized_reason,
        }
        correction_episode = Episode(
            name=f"manual correction for {target.uuid}",
            body=(
                f"Manual correction for relationship {target.uuid}. "
                f"Old fact: {target_fact}. "
                f"Corrected fact: {corrected_subject_value} {corrected_predicate_value} {corrected_object_value}. "
                f"Reason: {normalized_reason}."
            ),
            source=EpisodeType.JSON,
            source_description="manual memory correction",
            scope=relationship_scope,
            reference_time=correction_time,
            metadata=correction_metadata,
            instruction_set=instruction_set,
        )
        memory = ExtractedMemory(
            subject=corrected_subject_value,
            predicate=corrected_predicate_value,
            object=corrected_object_value,
            relationship_type=corrected_relationship_type,
            confidence=confidence,
            subject_label=self._relationship_entity_label(source_node.labels),
            object_label=self._relationship_entity_label(target_node.labels),
            valid_from=correction_time,
            valid_to=valid_to,
            scope=relationship_scope,
            source_text=source_text,
            metadata=correction_metadata,
            claim_mode=ClaimMode.CORRECTION,
        )
        # WS-11: manual corrections materialize under an operator receipt run.
        operator_run = self._begin_operator_run(job_name="correct_memory", scope_key=relationship_scope.key)
        try:
            self._engine._secret_reference_metadata(memory)
        except ValueError as exc:
            self._engine._reject_raw_secret_memory(
                receipt_run=operator_run,
                episode=correction_episode,
                memory=memory,
                now=correction_time,
                motive_name=None,
                governance_policy_digest=None,
                protection=None,
                error=exc,
            )
            self._checkpoint_operator_run(operator_run)
            raise
        correction_episode = self._store_episode(correction_episode)
        _, created_relationships, reinforced, superseded = self._engine.materialize_episode(
            correction_episode,
            [memory],
            created_by="manual-correction",
            receipt_run=operator_run,
            now=correction_time,
        )
        self.graph.mark_episode_processed(correction_episode.uuid, processed_at=datetime.now(UTC))
        corrected_relationship = self._corrected_relationship(
            scope=relationship_scope,
            subject=corrected_subject_value,
            predicate=corrected_predicate_value,
            object_value=corrected_object_value,
            relationship_type=corrected_relationship_type,
            correction_episode_uuid=correction_episode.uuid,
        )
        corrected_valid_to = self._correction_valid_to(
            target_valid_to=target.valid_to,
            correction_time=correction_time,
        )
        # WS-11/WS-12: the target supersession is a receipted, state-hash-bracketed
        # mutation INSIDE the operator run — mutating the store after the checkpoint
        # would silently drift the live state hash away from the recorded chain.
        target_supersede_before = self.graph.graph_state_hash(relationship_scope.key)
        self.graph.mark_relationship(
            target.uuid,
            status=RelationshipStatus.SUPERSEDED,
            valid_to=corrected_valid_to,
            properties={
                "superseded_at": (now or correction_time).isoformat(),
                "superseded_by_relationship_uuid": corrected_relationship.uuid,
                "superseded_reason": normalized_reason,
                "corrected_by_episode_uuid": correction_episode.uuid,
                "metadata": {
                    **self._relationship_metadata(target.properties),
                    "manual_correction_target": True,
                    "correction_reason": normalized_reason,
                    "corrected_by_episode_uuid": correction_episode.uuid,
                },
            },
        )
        target_supersede_after = self.graph.graph_state_hash(relationship_scope.key)
        self._emit_receipt(
            operator_run,
            decision_type=ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED,
            decision_reason="manual_correction_superseded",
            decision_result="superseded",
            now=now or correction_time,
            scope_key=relationship_scope.key,
            relationship_type=target.type,
            memory_type=self._engine.memory_type_for_relationship(dict(target.properties)),
            relationship_uuid=target.uuid,
            superseded_relationship_uuid=target.uuid,
            successor_relationship_uuid=corrected_relationship.uuid,
            graph_state_hash_before=target_supersede_before,
            graph_state_hash_after=target_supersede_after,
        )
        self._engine.invalidate_rollups_for_dependency(
            relationship_uuid=target.uuid,
            reason="manual_correction_superseded",
            now=now or correction_time,
            receipt_run=operator_run,
        )
        # WS-17 T17: duplicates parked behind the corrected row return to context.
        self._engine.repromote_duplicates_for_dependency(
            relationship_uuid=target.uuid,
            reason="manual_correction_superseded",
            now=now or correction_time,
            receipt_run=operator_run,
        )
        self._checkpoint_operator_run(operator_run)
        # A manual correction is a first-strike control signal, not merely a
        # higher-confidence observation.  Scan immediately for the governor it
        # may contradict before any escalation counter can grow.
        await self.run_coherence_scan(
            scope=relationship_scope,
            now=now or correction_time,
            policy=self._coherence_policy_for_scope(relationship_scope),
        )
        return CorrectMemoryResult(
            relationship_uuid=target.uuid,
            corrected_relationship_uuid=corrected_relationship.uuid,
            correction_episode_uuid=correction_episode.uuid,
            previous_status=target_status,
            status=RelationshipStatus.SUPERSEDED,
            scope=relationship_scope,
            valid_to=corrected_valid_to,
            correction_reason=normalized_reason,
            corrected_fact=str(self.graph.reveal(relationship_scope.key, corrected_relationship.properties["fact"])),
            created_relationships=created_relationships,
            reinforced_relationships=reinforced,
            superseded_relationships=superseded,
        )
