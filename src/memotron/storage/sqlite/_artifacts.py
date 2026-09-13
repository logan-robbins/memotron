"""Live non-memory artifacts and the coherence repair monitor.

Mirrors :mod:`memotron.storage.postgres._artifacts`. A live artifact is a
skill or file that governs behaviour alongside memory; the repair monitor tracks
whether a proposed coherence fix actually held."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.models import (
    ArtifactClass,
    ArtifactContributionProjection,
    CoherenceRepairMonitor,
    MemoryScope,
    PersistentArtifact,
)
from memotron.storage.receipts import payload_digest

if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class ArtifactPlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    def register_live_artifact(self, artifact: PersistentArtifact) -> PersistentArtifact:
        """Persist a projected skill/file artifact for fail-safe coherence use."""
        if artifact.artifact_class not in {ArtifactClass.SKILL, ArtifactClass.FILE}:
            raise ValueError("live artifacts must be skill or file artifacts")
        if not artifact.directives:
            raise ValueError("live artifact registration requires at least one structured directive")
        if not artifact.author.strip() or artifact.author == "unknown":
            raise ValueError("live artifact registration requires a known author")
        if artifact.updated_at is None:
            raise ValueError("live artifact registration requires updated_at")
        if artifact.scope.key != f"{artifact.scope.kind.value}:{artifact.scope.scope_id}":
            raise ValueError("live artifact has an invalid scope")
        now = datetime.now(UTC)
        existing = self._connection.execute(
            "SELECT active, quarantined_at, payload_json FROM live_artifacts WHERE scope_key = ? AND artifact_id = ?",
            (artifact.scope.key, artifact.artifact_id),
        ).fetchone()
        approved_reactivation = bool(artifact.metadata.get("approved_reactivation"))
        active = 1 if existing is None or bool(existing["active"]) or approved_reactivation else 0
        quarantined_at = None if active else existing["quarantined_at"]
        stored = artifact.model_copy(
            update={"created_at": artifact.created_at or now, "updated_at": artifact.updated_at}
        )
        if existing is not None:
            try:
                prior = PersistentArtifact.model_validate_json(str(existing["payload_json"]))
            except ValueError as exc:
                raise RuntimeError("persisted live artifact projection is invalid") from exc
            self._capture_live_artifact_version(prior, captured_at=now)
        self._connection.execute(
            """
            INSERT INTO live_artifacts (
                scope_key, artifact_id, artifact_class, payload_json, registered_at, updated_at,
                active, quarantined_at, positive_outcomes, negative_outcomes, last_outcome_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, NULL)
            ON CONFLICT(scope_key, artifact_id) DO UPDATE SET
                artifact_class = excluded.artifact_class,
                payload_json = excluded.payload_json,
                updated_at = excluded.updated_at,
                active = excluded.active,
                quarantined_at = excluded.quarantined_at
            """,
            (
                stored.scope.key,
                stored.artifact_id,
                stored.artifact_class.value,
                stored.model_dump_json(),
                self._datetime_to_text(now),
                self._datetime_to_text(stored.updated_at),
                active,
                quarantined_at,
            ),
        )
        self._capture_live_artifact_version(stored, captured_at=now)
        self._commit()
        return stored

    def _capture_live_artifact_version(self, artifact: PersistentArtifact, *, captured_at: datetime) -> str:
        """Persist an immutable projection snapshot and return its digest."""
        payload = artifact.model_dump(mode="json")
        digest = payload_digest(payload)
        self._connection.execute(
            """
            INSERT INTO live_artifact_versions (
                scope_key, artifact_id, version_digest, payload_json, captured_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, artifact_id, version_digest) DO NOTHING
            """,
            (
                artifact.scope.key,
                artifact.artifact_id,
                digest,
                self._json_dumps(payload, "live artifact version payload"),
                self._datetime_to_text(captured_at),
            ),
        )
        return digest

    def live_artifact_version(self, *, scope: MemoryScope, artifact_id: str) -> tuple[PersistentArtifact, str]:
        """Return the exact active registry projection and canonical version digest."""
        row = self._connection.execute(
            "SELECT payload_json FROM live_artifacts WHERE scope_key = ? AND artifact_id = ?",
            (scope.key, artifact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"live artifact does not exist: {artifact_id!r}")
        try:
            artifact = PersistentArtifact.model_validate_json(str(row["payload_json"]))
        except ValueError as exc:
            raise RuntimeError("persisted live artifact projection is invalid") from exc
        return artifact, payload_digest(artifact.model_dump(mode="json"))

    def previous_live_artifact_version(
        self, *, scope: MemoryScope, artifact_id: str, excluding_digest: str
    ) -> tuple[PersistentArtifact, str]:
        """Return the latest immutable version preceding a repaired projection."""
        row = self._connection.execute(
            """
            SELECT payload_json, version_digest
            FROM live_artifact_versions
            WHERE scope_key = ? AND artifact_id = ? AND version_digest != ?
            ORDER BY captured_at DESC, version_digest DESC
            LIMIT 1
            """,
            (scope.key, artifact_id, excluding_digest),
        ).fetchone()
        if row is None:
            raise ValueError(
                "post-repair monitoring requires a prior immutable live-artifact version; "
                "register the pre-repair and repaired projections before recording repair"
            )
        try:
            artifact = PersistentArtifact.model_validate_json(str(row["payload_json"]))
        except ValueError as exc:
            raise RuntimeError("persisted live artifact version is invalid") from exc
        return artifact, str(row["version_digest"])

    def open_coherence_repair_monitor(
        self,
        *,
        scope: MemoryScope,
        incident_id: str,
        relationship_uuid: str,
        artifact_id: str,
        repaired_artifact_version_digest: str,
        opened_at: datetime,
    ) -> CoherenceRepairMonitor:
        """Start an auditable recovery window after an operator-reviewed repair."""
        _current, current_digest = self.live_artifact_version(scope=scope, artifact_id=artifact_id)
        if current_digest != repaired_artifact_version_digest:
            raise ValueError("artifact_version_digest does not match the currently registered repaired artifact")
        prior, prior_digest = self.previous_live_artifact_version(
            scope=scope, artifact_id=artifact_id, excluding_digest=current_digest
        )
        existing = self._connection.execute(
            "SELECT * FROM coherence_repair_monitors WHERE scope_key = ? AND incident_id = ?",
            (scope.key, incident_id),
        ).fetchone()
        if existing is not None:
            monitor = self._coherence_repair_monitor_from_row(existing)
            if monitor.artifact_id != artifact_id or monitor.repaired_artifact_version_digest != current_digest:
                raise ValueError("coherence incident already has a different repair monitor")
            return monitor
        self._connection.execute(
            """
            INSERT INTO coherence_repair_monitors (
                scope_key, incident_id, relationship_uuid, artifact_id,
                prior_artifact_version_digest, repaired_artifact_version_digest,
                prior_payload_json, status, opened_at, recovered_at, reopened_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'monitoring', ?, NULL, NULL)
            """,
            (
                scope.key,
                incident_id,
                relationship_uuid,
                artifact_id,
                prior_digest,
                current_digest,
                self._json_dumps(prior.model_dump(mode="json"), "repair monitor prior artifact"),
                self._datetime_to_text(opened_at),
            ),
        )
        self._commit()
        return CoherenceRepairMonitor(
            scope=scope,
            incident_id=incident_id,
            relationship_uuid=relationship_uuid,
            artifact_id=artifact_id,
            prior_artifact_version_digest=prior_digest,
            repaired_artifact_version_digest=current_digest,
            prior_artifact=prior,
            status="monitoring",
            opened_at=opened_at,
        )

    def coherence_repair_monitor(self, *, scope: MemoryScope, incident_id: str) -> CoherenceRepairMonitor | None:
        row = self._connection.execute(
            "SELECT * FROM coherence_repair_monitors WHERE scope_key = ? AND incident_id = ?",
            (scope.key, incident_id),
        ).fetchone()
        return self._coherence_repair_monitor_from_row(row) if row is not None else None

    def active_coherence_repair_monitor(
        self, *, scope: MemoryScope, relationship_uuid: str, artifact_id: str
    ) -> CoherenceRepairMonitor | None:
        row = self._connection.execute(
            """
            SELECT * FROM coherence_repair_monitors
            WHERE scope_key = ? AND relationship_uuid = ? AND artifact_id = ? AND status = 'monitoring'
            ORDER BY opened_at DESC, incident_id DESC LIMIT 1
            """,
            (scope.key, relationship_uuid, artifact_id),
        ).fetchone()
        return self._coherence_repair_monitor_from_row(row) if row is not None else None

    def recover_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, recovered_at: datetime
    ) -> CoherenceRepairMonitor:
        monitor = self.coherence_repair_monitor(scope=scope, incident_id=incident_id)
        if monitor is None:
            raise ValueError(f"coherence repair monitor does not exist for incident {incident_id!r}")
        if monitor.status != "monitoring":
            return monitor
        self._connection.execute(
            """
            UPDATE coherence_repair_monitors
            SET status = 'recovered', recovered_at = ?
            WHERE scope_key = ? AND incident_id = ?
            """,
            (self._datetime_to_text(recovered_at), scope.key, incident_id),
        )
        self._commit()
        recovered = self.coherence_repair_monitor(scope=scope, incident_id=incident_id)
        assert recovered is not None
        return recovered

    def rollback_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, reopened_at: datetime
    ) -> CoherenceRepairMonitor:
        """Restore the prior registry projection after a monitored recurrence.

        This swaps only Memotron's live artifact alias/projection.  The
        underlying user-managed file or skill remains untouched for review.
        """
        monitor = self.coherence_repair_monitor(scope=scope, incident_id=incident_id)
        if monitor is None:
            raise ValueError(f"coherence repair monitor does not exist for incident {incident_id!r}")
        if monitor.status != "monitoring":
            return monitor
        prior = monitor.prior_artifact
        self._connection.execute(
            """
            UPDATE live_artifacts
            SET artifact_class = ?, payload_json = ?, updated_at = ?, active = 1, quarantined_at = NULL
            WHERE scope_key = ? AND artifact_id = ?
            """,
            (
                prior.artifact_class.value,
                prior.model_dump_json(),
                self._datetime_to_text(prior.updated_at or reopened_at),
                scope.key,
                monitor.artifact_id,
            ),
        )
        self._connection.execute(
            """
            UPDATE coherence_repair_monitors
            SET status = 'reopened_rolled_back', reopened_at = ?
            WHERE scope_key = ? AND incident_id = ?
            """,
            (self._datetime_to_text(reopened_at), scope.key, incident_id),
        )
        self._commit()
        rolled_back = self.coherence_repair_monitor(scope=scope, incident_id=incident_id)
        assert rolled_back is not None
        return rolled_back

    def _coherence_repair_monitor_from_row(self, row: sqlite3.Row) -> CoherenceRepairMonitor:
        scope_key = str(row["scope_key"])
        kind, scope_id = scope_key.split(":", 1)
        try:
            prior = PersistentArtifact.model_validate_json(str(row["prior_payload_json"]))
        except ValueError as exc:
            raise RuntimeError("persisted coherence repair monitor is invalid") from exc
        return CoherenceRepairMonitor(
            scope=MemoryScope(kind=kind, scope_id=scope_id),
            incident_id=str(row["incident_id"]),
            relationship_uuid=str(row["relationship_uuid"]),
            artifact_id=str(row["artifact_id"]),
            prior_artifact_version_digest=str(row["prior_artifact_version_digest"]),
            repaired_artifact_version_digest=str(row["repaired_artifact_version_digest"]),
            prior_artifact=prior,
            status=str(row["status"]),
            opened_at=self._datetime_from_text(str(row["opened_at"])),
            recovered_at=(
                self._datetime_from_text(str(row["recovered_at"])) if row["recovered_at"] is not None else None
            ),
            reopened_at=(self._datetime_from_text(str(row["reopened_at"])) if row["reopened_at"] is not None else None),
        )

    def live_artifacts(self, *, scope: MemoryScope, include_quarantined: bool = False) -> list[PersistentArtifact]:
        sql = "SELECT payload_json FROM live_artifacts WHERE scope_key = ?"
        parameters: list[object] = [scope.key]
        if not include_quarantined:
            sql += " AND active = 1"
        rows = self._connection.execute(sql + " ORDER BY updated_at DESC, artifact_id", parameters).fetchall()
        artifacts: list[PersistentArtifact] = []
        for row in rows:
            try:
                artifact = PersistentArtifact.model_validate_json(str(row["payload_json"]))
            except ValueError as exc:
                raise RuntimeError("persisted live artifact projection is invalid") from exc
            if artifact.scope.key != scope.key:
                raise RuntimeError("persisted live artifact projection has a scope mismatch")
            artifacts.append(artifact)
        return artifacts

    def live_artifact_contribution(
        self,
        *,
        scope: MemoryScope,
        artifact_id: str,
        minimum_evidence: int,
    ) -> ArtifactContributionProjection:
        if minimum_evidence < 1:
            raise ValueError("minimum_evidence must be at least one")
        row = self._connection.execute(
            "SELECT * FROM live_artifacts WHERE scope_key = ? AND artifact_id = ?",
            (scope.key, artifact_id),
        ).fetchone()
        if row is None:
            raise ValueError(f"live artifact does not exist: {artifact_id!r}")
        positive = int(row["positive_outcomes"])
        negative = int(row["negative_outcomes"])
        total = positive + negative
        last_outcome = row["last_outcome_at"]
        return ArtifactContributionProjection(
            scope=scope,
            artifact_id=str(row["artifact_id"]),
            artifact_class=ArtifactClass(str(row["artifact_class"])),
            positive_outcomes=positive,
            negative_outcomes=negative,
            contribution_score=(positive - negative) / total if total else 0.0,
            minimum_evidence_met=total >= minimum_evidence,
            quarantined=not bool(row["active"]),
            last_outcome_at=(self._datetime_from_text(str(last_outcome)) if last_outcome is not None else None),
        )

    def record_live_artifact_outcome(
        self,
        *,
        scope: MemoryScope,
        artifact_id: str,
        positive: bool,
        occurred_at: datetime,
        minimum_evidence: int,
        quarantine_threshold: float,
        task_run_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[ArtifactContributionProjection, bool]:
        if not -1.0 <= quarantine_threshold <= 1.0:
            raise ValueError("quarantine_threshold must be between -1 and 1")
        if not task_run_id.strip():
            raise ValueError("task_run_id cannot be blank")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be blank")
        existing = self._connection.execute(
            "SELECT artifact_id FROM live_artifact_outcome_events WHERE scope_key = ? AND idempotency_key = ?",
            (scope.key, idempotency_key),
        ).fetchone()
        if existing is not None:
            if str(existing["artifact_id"]) != artifact_id:
                raise ValueError("live-artifact outcome idempotency key is already bound to a different artifact")
            return (
                self.live_artifact_contribution(
                    scope=scope, artifact_id=artifact_id, minimum_evidence=minimum_evidence
                ),
                False,
            )
        projection = self.live_artifact_contribution(
            scope=scope, artifact_id=artifact_id, minimum_evidence=minimum_evidence
        )
        next_positive = projection.positive_outcomes + int(positive)
        next_negative = projection.negative_outcomes + int(not positive)
        total = next_positive + next_negative
        score = (next_positive - next_negative) / total
        quarantined = projection.quarantined or (total >= minimum_evidence and score <= quarantine_threshold)
        self._connection.execute(
            """
            UPDATE live_artifacts
            SET positive_outcomes = ?, negative_outcomes = ?, last_outcome_at = ?,
                active = ?, quarantined_at = ?
            WHERE scope_key = ? AND artifact_id = ?
            """,
            (
                next_positive,
                next_negative,
                self._datetime_to_text(occurred_at),
                0 if quarantined else 1,
                self._datetime_to_text(occurred_at) if quarantined else None,
                scope.key,
                artifact_id,
            ),
        )
        self._connection.execute(
            """
            INSERT INTO live_artifact_outcome_events (
                event_id, scope_key, artifact_id, task_run_id, idempotency_key,
                verdict, occurred_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                secrets.token_hex(16),
                scope.key,
                artifact_id,
                task_run_id,
                idempotency_key,
                "positive" if positive else "negative",
                self._datetime_to_text(occurred_at),
                self._json_dumps(payload, "live artifact outcome payload"),
            ),
        )
        self._commit()
        return (
            self.live_artifact_contribution(scope=scope, artifact_id=artifact_id, minimum_evidence=minimum_evidence),
            True,
        )
