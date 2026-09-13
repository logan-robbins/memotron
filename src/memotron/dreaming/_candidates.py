"""The stage-1 raw candidate row, and the four ways it can end.

A candidate that never becomes a memory still leaves a row. That is the whole point
of this file: FILED (refused by the actionability gate), STORED (accepted into
stage 1), QUARANTINED (dropped at stage 3), PROMOTED (materialized). Without the
row, a candidate that vanishes is indistinguishable from one that was never
extracted -- and the quarantine-and-continue regime in _materialization would be
lossy rather than auditable.

Extracted out of _formation.py to break a cycle, not for tidiness. mixin_dag.py
found _decisions and _materialization both calling back UP into _formation for
these transitions, which put seven of ten mixins in one strongly-connected
component. They are a leaf concern -- nothing here calls another mixin -- so moving
them down makes those edges point the right way."""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from memotron.config import (
    DreamJob,
)
from memotron.dreaming._common import _ContentProtection
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.extraction import (
    CandidateAbstention,
)
from memotron.models import (
    Episode,
    ExtractedMemory,
    QuarantineStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    payload_digest,
    rejected_candidate_digest,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class RawCandidateMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    def _file_quarantined_candidates(
        self,
        quarantined: tuple[CandidateAbstention, ...],
        *,
        episode: Episode,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None,
    ) -> None:
        """Persist and receipt every candidate the actionability gate refused."""
        for abstention in quarantined:
            candidate_uuid = uuid4().hex
            payload_text = json.dumps(abstention.raw, sort_keys=True, separators=(",", ":"))
            digest = rejected_candidate_digest(
                abstention.raw,
                episode_uuid=episode.uuid,
                content_key=protection.key if protection is not None else None,
            )
            proposed_memory_type = (
                self._resolve_memory_type_value(
                    relationship_type=abstention.proposed_relationship_type,
                    instruction_set_name=episode.instruction_set,
                )
                if abstention.proposed_relationship_type
                else None
            )

            def hide(text: str) -> str:
                return protection.seal(text) if protection is not None and text else text

            self._graph.quarantine_candidate(
                candidate_uuid=candidate_uuid,
                scope_key=episode.scope.key,
                episode_uuid=episode.uuid,
                reason=abstention.violation.value,
                detail=hide(abstention.reason),
                saves_step=(hide(abstention.saves_step) if abstention.saves_step is not None else None),
                subject=hide(abstention.subject),
                predicate=hide(abstention.predicate),
                object_text=hide(abstention.object),
                proposed_relationship_type=abstention.proposed_relationship_type,
                proposed_memory_type=proposed_memory_type,
                candidate_payload=(protection.seal(payload_text) if protection is not None else payload_text),
                candidate_digest=digest,
                instruction_set=episode.instruction_set,
                motive_name=motive_name,
                quarantined_at=now,
            )
            quarantine_payload: dict[str, object] = {
                "violation": abstention.violation.value,
                "candidate_index": abstention.index,
                "candidate_uuid": candidate_uuid,
                "stated_saves_step": abstention.saves_step is not None,
            }
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CANDIDATE_QUARANTINED,
                # The abstention reason quotes model-authored text, so the
                # plaintext lineage column carries only the bounded machine code
                # and the human detail lives in the quarantine row (sealed for a
                # protected scope) alongside the candidate it explains.
                decision_reason=f"quarantined:{abstention.violation.value}",
                decision_result="quarantined",
                episode=episode,
                now=now,
                candidate_uuid=candidate_uuid,
                candidate_digest=digest,
                motive_name=motive_name,
                memory_type=proposed_memory_type,
                relationship_type=(
                    abstention.proposed_relationship_type
                    if abstention.proposed_relationship_type
                    in self._config.instruction_set(episode.instruction_set).allowed_relationship_types
                    else None
                ),
                governance_policy_digest=governance_policy_digest,
                event_payload=json.dumps(quarantine_payload, sort_keys=True, separators=(",", ":")),
                event_payload_digest=payload_digest(quarantine_payload),
            )

    def _store_raw_candidate(
        self,
        candidate: ExtractedMemory,
        *,
        episode: Episode,
        memory_type: str,
        digest: str,
        instruction_set: str | None,
        motive_name: str | None,
        now: datetime | None,
        protection: _ContentProtection | None,
    ) -> None:
        def hide(text: str | None) -> str | None:
            if text is None:
                return None
            return protection.seal(text) if protection is not None else text

        payload_text = json.dumps(candidate.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        self._graph.quarantine_candidate(
            candidate_uuid=digest,
            scope_key=episode.scope.key,
            episode_uuid=episode.uuid,
            reason="pending",
            detail="",
            saves_step=hide(candidate.saves_step),
            subject=hide(candidate.subject) or "",
            predicate=hide(candidate.predicate) or "",
            object_text=hide(candidate.object) or "",
            proposed_relationship_type=candidate.relationship_type,
            proposed_memory_type=memory_type,
            candidate_payload=(protection.seal(payload_text) if protection is not None else payload_text),
            candidate_digest=digest,
            instruction_set=instruction_set,
            motive_name=motive_name,
            quarantined_at=now or episode.reference_time,
            status=QuarantineStatus.PENDING,
        )

    def _mark_raw_candidate_quarantined(
        self,
        *,
        digest: str,
        now: datetime | None,
        reason: str,
        resolution_note: str,
        resolved_by: str = "governance",
    ) -> None:
        """Transition a stage-1 raw row to QUARANTINED after a stage-3 drop.

        Best-effort: a candidate materialized/quarantined outside the
        raw-storage path (an operator write, a promotion replay) has no raw
        row to transition, so a lookup miss is silently ignored rather than
        raised — the receipt already emitted at the call site is the
        authoritative record either way, this is the supplementary queryable
        artifact.
        """
        with contextlib.suppress(ValueError):
            self._graph.resolve_quarantined_candidate(
                digest,
                status=QuarantineStatus.QUARANTINED,
                resolved_at=now or datetime.now(UTC),
                resolved_by=resolved_by,
                resolution_note=resolution_note,
                reason=reason,
            )

    def _mark_raw_candidate_promoted(
        self,
        *,
        digest: str,
        now: datetime | None,
        relationship_uuid: str,
        resolved_by: str = "governance",
    ) -> None:
        """Transition a stage-1 raw row to PROMOTED once it materializes.

        Same best-effort contract as :meth:`_mark_raw_candidate_quarantined` —
        callers outside the raw-storage path (operator writes, promotion
        replay) have no row to transition.
        """
        with contextlib.suppress(ValueError):
            self._graph.resolve_quarantined_candidate(
                digest,
                status=QuarantineStatus.PROMOTED,
                resolved_at=now or datetime.now(UTC),
                resolved_by=resolved_by,
                resolution_note="materialized",
                promoted_relationship_uuid=relationship_uuid,
                reason="accepted",
            )

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
    ) -> None:
        """File + receipt a candidate whose memory_type/instruction could not be
        resolved at a stage-3 checkpoint — defense in depth (see callers): by
        construction this should be unreachable for anything that passed
        extraction's OWN (real-label) resolution, so reaching here indicates a
        config change mid-run rather than a bad candidate.  Filed straight into
        QUARANTINED (no PENDING step — the row was never stored raw, since this
        check runs BEFORE this restructure's raw-storage point) so the run
        still never aborts.
        """
        digest = rejected_candidate_digest(
            candidate.model_dump(mode="json"),
            episode_uuid=episode.uuid,
            content_key=protection.key if protection is not None else None,
        )

        def hide(text: str) -> str:
            return protection.seal(text) if protection is not None and text else text

        payload_text = json.dumps(candidate.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        self._graph.quarantine_candidate(
            candidate_uuid=digest,
            scope_key=episode.scope.key,
            episode_uuid=episode.uuid,
            reason=reason,
            detail=hide(f"cannot resolve memory_type for relationship type {candidate.relationship_type!r}"),
            saves_step=hide(candidate.saves_step) if candidate.saves_step else None,
            subject=hide(candidate.subject),
            predicate=hide(candidate.predicate),
            object_text=hide(candidate.object),
            proposed_relationship_type=candidate.relationship_type,
            proposed_memory_type=None,
            candidate_payload=(protection.seal(payload_text) if protection is not None else payload_text),
            candidate_digest=digest,
            instruction_set=episode.instruction_set,
            motive_name=motive_name,
            quarantined_at=now or episode.reference_time,
            status=QuarantineStatus.QUARANTINED,
        )
        quarantine_payload = {"violation": reason, "relationship_type": candidate.relationship_type}
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.CANDIDATE_QUARANTINED,
            decision_reason=f"quarantined:{reason}",
            decision_result="quarantined",
            episode=episode,
            now=now,
            candidate_uuid=uuid4().hex,
            candidate_digest=digest,
            motive_name=motive_name,
            relationship_type=candidate.relationship_type,
            governance_policy_digest=governance_policy_digest,
            event_payload=json.dumps(quarantine_payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(quarantine_payload),
        )
