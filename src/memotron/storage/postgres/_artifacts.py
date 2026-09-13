"""Live behavior artifacts and their coherence repair monitors on Postgres.

Three things differ from the SQLite implementation, and each of them is a
concurrency property rather than an incidental porting choice:

1. **Registration is one locked read-modify-write.**  ``register_live_artifact``
   derives ``active`` and ``quarantined_at`` from the row it is about to
   overwrite, so the read and the write have to be a single atomic step: the
   existing row is taken with ``SELECT ... FOR UPDATE`` inside the same
   transaction that upserts it and snapshots the prior version.  The SQLite shape
   — a bare ``SELECT`` followed by an upsert — is a lost update under two
   replicas, and the value lost is exactly the quarantine flag: a registration
   that read ``active = true`` would resurrect an artifact another replica had
   just quarantined.

2. **Outcome idempotency is decided by the database, not by a prior read.**  The
   dedupe key is the ``(scope_key, idempotency_key)`` unique constraint on
   ``live_artifact_outcome_events``: the insert is ``ON CONFLICT ... DO NOTHING
   RETURNING``, and the counters on ``live_artifacts`` are bumped only when that
   insert actually created a row.  The counters move in place
   (``positive_outcomes = positive_outcomes + 1``) rather than by writing back a
   total computed in Python, so a replay never double-counts and two concurrent
   outcomes cannot overwrite each other's increment.  Because both idempotent
   inserts here resolve their own conflicts, no unique violation is left for the
   caller to translate — see the note on ``open_coherence_repair_monitor``.

3. **Selection and ordering are pushed into SQL.**  The quarantine predicate, the
   contribution lookup, the "latest version preceding this one" pick, and the
   "newest still-monitoring monitor" pick are all index-backed queries
   (``live_artifacts_active_idx``, the ``live_artifacts`` primary key,
   ``live_artifact_versions_latest_idx``,
   ``coherence_repair_monitors_relationship_idx``) instead of a whole-scope read
   filtered and sorted in Python.

Payloads live in ``jsonb`` columns rather than SQLite's ``*_json`` text columns,
but digests are still :func:`payload_digest` over ``model_dump(mode="json")`` and
timestamps are still ISO-8601 text, so version digests and ordering stay
byte-identical across engines.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.models import (
    ArtifactClass,
    ArtifactContributionProjection,
    CoherenceRepairMonitor,
    MemoryScope,
    PersistentArtifact,
)
from memotron.storage.postgres._engine import (
    datetime_to_text,
    json_dumps,
    optional_datetime_from_text,
)
from memotron.storage.receipts import payload_digest

# Only these two artifact classes are projected into the live registry; memory
# relationships govern behavior through the graph plane instead.
_LIVE_ARTIFACT_CLASSES = frozenset({ArtifactClass.SKILL, ArtifactClass.FILE})

_MONITORING = "monitoring"


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class ArtifactPlaneMixin(_Base):
    """Implements the live-artifact and coherence-repair surface of ``OperationalStorage``."""

    # ------------------------------------------------------------------ live artifacts

    def register_live_artifact(self, artifact: PersistentArtifact) -> PersistentArtifact:
        """Persist a projected skill/file artifact for fail-safe coherence use.

        The whole operation is one transaction with the existing registry row
        locked, because ``active``/``quarantined_at`` are carried forward from
        that row.  Two replicas registering the same artifact therefore serialise
        on it and the quarantine state cannot be lost; when the row does not exist
        yet there is nothing to carry forward, so the upsert's conflict handling is
        sufficient to settle that race.
        """
        if artifact.artifact_class not in _LIVE_ARTIFACT_CLASSES:
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
        with self._engine.transaction():
            existing = self._engine.fetchone(
                """
                SELECT active, quarantined_at, payload
                FROM live_artifacts
                WHERE scope_key = %s AND artifact_id = %s
                FOR UPDATE
                """,
                (artifact.scope.key, artifact.artifact_id),
            )
            approved_reactivation = bool(artifact.metadata.get("approved_reactivation"))
            # A re-registration never lifts a quarantine on its own: only an
            # operator-approved reactivation, or a first registration, is active.
            active = existing is None or bool(existing["active"]) or approved_reactivation
            quarantined_at = None if active else existing["quarantined_at"]
            stored = artifact.model_copy(
                update={"created_at": artifact.created_at or now, "updated_at": artifact.updated_at}
            )
            if existing is not None:
                # Snapshot what is about to be overwritten *before* overwriting it,
                # so the version history keeps the pre-repair projection a repair
                # monitor will later need to roll back to.
                prior = self._artifact_from_payload(
                    existing["payload"], "persisted live artifact projection is invalid"
                )
                self._capture_live_artifact_version(prior, captured_at=now)
            self._engine.execute(
                """
                INSERT INTO live_artifacts (
                    scope_key, artifact_id, artifact_class, payload, registered_at, updated_at,
                    active, quarantined_at, positive_outcomes, negative_outcomes, last_outcome_at
                ) VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s, %s, 0, 0, NULL)
                ON CONFLICT (scope_key, artifact_id) DO UPDATE SET
                    artifact_class = excluded.artifact_class,
                    payload = excluded.payload,
                    updated_at = excluded.updated_at,
                    active = excluded.active,
                    quarantined_at = excluded.quarantined_at
                """,
                (
                    stored.scope.key,
                    stored.artifact_id,
                    stored.artifact_class.value,
                    stored.model_dump_json(),
                    datetime_to_text(now),
                    datetime_to_text(stored.updated_at),
                    active,
                    quarantined_at,
                ),
            )
            self._capture_live_artifact_version(stored, captured_at=now)
            return stored

    def _capture_live_artifact_version(self, artifact: PersistentArtifact, *, captured_at: datetime) -> str:
        """Persist an immutable projection snapshot and return its digest.

        The digest is taken over the Python payload with the shared
        :func:`payload_digest`, not over the stored ``jsonb``, so it matches the
        SQLite backend's digest for the same artifact byte for byte.  Re-capturing
        an unchanged projection is a no-op: the digest is part of the primary key.
        """
        payload = artifact.model_dump(mode="json")
        digest = payload_digest(payload)
        self._engine.execute(
            """
            INSERT INTO live_artifact_versions (
                scope_key, artifact_id, version_digest, payload, captured_at
            ) VALUES (%s, %s, %s, %s::jsonb, %s)
            ON CONFLICT (scope_key, artifact_id, version_digest) DO NOTHING
            """,
            (
                artifact.scope.key,
                artifact.artifact_id,
                digest,
                json_dumps(payload, "live artifact version payload"),
                datetime_to_text(captured_at),
            ),
        )
        return digest

    def live_artifact_version(self, *, scope: MemoryScope, artifact_id: str) -> tuple[PersistentArtifact, str]:
        """Return the exact active registry projection and canonical version digest."""
        row = self._engine.fetchone(
            "SELECT payload FROM live_artifacts WHERE scope_key = %s AND artifact_id = %s",
            (scope.key, artifact_id),
        )
        if row is None:
            raise ValueError(f"live artifact does not exist: {artifact_id!r}")
        artifact = self._artifact_from_payload(row["payload"], "persisted live artifact projection is invalid")
        return artifact, payload_digest(artifact.model_dump(mode="json"))

    def previous_live_artifact_version(
        self, *, scope: MemoryScope, artifact_id: str, excluding_digest: str
    ) -> tuple[PersistentArtifact, str]:
        """Return the latest immutable version preceding a repaired projection.

        The ordering matches ``live_artifact_versions_latest_idx`` exactly, so the
        pick is an index seek plus one row rather than a sort over the artifact's
        whole history.
        """
        row = self._engine.fetchone(
            """
            SELECT payload, version_digest
            FROM live_artifact_versions
            WHERE scope_key = %s AND artifact_id = %s AND version_digest != %s
            ORDER BY captured_at DESC, version_digest DESC
            LIMIT 1
            """,
            (scope.key, artifact_id, excluding_digest),
        )
        if row is None:
            raise ValueError(
                "post-repair monitoring requires a prior immutable live-artifact version; "
                "register the pre-repair and repaired projections before recording repair"
            )
        artifact = self._artifact_from_payload(row["payload"], "persisted live artifact version is invalid")
        return artifact, str(row["version_digest"])

    def live_artifacts(self, *, scope: MemoryScope, include_quarantined: bool = False) -> list[PersistentArtifact]:
        """Registered artifacts for a scope, newest first.

        The quarantine predicate is part of the query so the read seeks
        ``live_artifacts_active_idx(scope_key, active, updated_at)`` and gets its
        ordering from the index instead of loading the scope and filtering it.
        """
        sql = "SELECT payload FROM live_artifacts WHERE scope_key = %s"
        parameters: list[Any] = [scope.key]
        if not include_quarantined:
            sql += " AND active"
        rows = self._engine.fetchall(sql + " ORDER BY updated_at DESC, artifact_id", parameters)
        artifacts: list[PersistentArtifact] = []
        for row in rows:
            artifact = self._artifact_from_payload(row["payload"], "persisted live artifact projection is invalid")
            # The projection carries its own scope; a mismatch means the row and
            # the payload disagree, which no code path may treat as merely absent.
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
        """Outcome-attributed contribution state for one artifact (primary-key read)."""
        if minimum_evidence < 1:
            raise ValueError("minimum_evidence must be at least one")
        row = self._engine.fetchone(
            "SELECT * FROM live_artifacts WHERE scope_key = %s AND artifact_id = %s",
            (scope.key, artifact_id),
        )
        if row is None:
            raise ValueError(f"live artifact does not exist: {artifact_id!r}")
        return self._contribution_from_row(row, scope=scope, minimum_evidence=minimum_evidence)

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
        """Append one artifact outcome (idempotent) and update quarantine state.

        The event insert is the idempotency decision: ``ON CONFLICT DO NOTHING
        RETURNING`` tells us whether this call is the one that created the event,
        and only that call bumps the counters.  A replay of the same
        ``(scope_key, idempotency_key)`` — including a replay racing the original
        on another replica — therefore reports the current projection with
        ``created = False`` and leaves the counters alone.
        """
        if not -1.0 <= quarantine_threshold <= 1.0:
            raise ValueError("quarantine_threshold must be between -1 and 1")
        if not task_run_id.strip():
            raise ValueError("task_run_id cannot be blank")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be blank")

        with self._engine.transaction():
            # Lock the registry row up front: it is both the existence check the
            # interface's ValueError depends on and the row the increment below
            # mutates, and the event's foreign key would otherwise abort the whole
            # transaction with a database error instead.
            locked = self._engine.fetchone(
                """
                SELECT active FROM live_artifacts
                WHERE scope_key = %s AND artifact_id = %s
                FOR UPDATE
                """,
                (scope.key, artifact_id),
            )
            if locked is None:
                # SQLite reaches its idempotency probe before it reads the registry
                # row, so a key already bound elsewhere outranks "does not exist".
                self._assert_outcome_key_unbound(scope=scope, artifact_id=artifact_id, idempotency_key=idempotency_key)
                raise ValueError(f"live artifact does not exist: {artifact_id!r}")

            created = self._engine.fetchone(
                """
                INSERT INTO live_artifact_outcome_events (
                    event_id, scope_key, artifact_id, task_run_id, idempotency_key,
                    verdict, occurred_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (scope_key, idempotency_key) DO NOTHING
                RETURNING event_id
                """,
                (
                    secrets.token_hex(16),
                    scope.key,
                    artifact_id,
                    task_run_id,
                    idempotency_key,
                    "positive" if positive else "negative",
                    datetime_to_text(occurred_at),
                    json_dumps(payload, "live artifact outcome payload"),
                ),
            )
            if created is None:
                self._assert_outcome_key_unbound(scope=scope, artifact_id=artifact_id, idempotency_key=idempotency_key)
                return (
                    self.live_artifact_contribution(
                        scope=scope, artifact_id=artifact_id, minimum_evidence=minimum_evidence
                    ),
                    False,
                )

            # In-place increment: the new totals are computed by the database from
            # whatever it currently holds, so a concurrent outcome that committed
            # between the lock and here still counts.
            bumped = self._engine.fetchone(
                """
                UPDATE live_artifacts
                SET positive_outcomes = positive_outcomes + %s,
                    negative_outcomes = negative_outcomes + %s,
                    last_outcome_at = %s
                WHERE scope_key = %s AND artifact_id = %s
                RETURNING positive_outcomes, negative_outcomes, active
                """,
                (
                    int(positive),
                    int(not positive),
                    datetime_to_text(occurred_at),
                    scope.key,
                    artifact_id,
                ),
            )
            if bumped is None:  # pragma: no cover - the row is locked by this transaction
                raise RuntimeError("live artifact vanished while recording an outcome")

            next_positive = int(bumped["positive_outcomes"])
            next_negative = int(bumped["negative_outcomes"])
            total = next_positive + next_negative
            score = (next_positive - next_negative) / total
            # Quarantine is a latch: once set it survives further outcomes and is
            # only cleared by an approved reactivation or a monitor rollback.
            quarantined = (not bool(bumped["active"])) or (total >= minimum_evidence and score <= quarantine_threshold)
            # A second statement rather than one clever UPDATE: the flag is a
            # function of the *post*-increment totals, and the row is already
            # locked by the increment above, so this cannot interleave.
            stamped = self._engine.fetchone(
                """
                UPDATE live_artifacts
                SET active = %s, quarantined_at = %s
                WHERE scope_key = %s AND artifact_id = %s
                RETURNING *
                """,
                (
                    not quarantined,
                    datetime_to_text(occurred_at) if quarantined else None,
                    scope.key,
                    artifact_id,
                ),
            )
            if stamped is None:  # pragma: no cover - the row is locked by this transaction
                raise RuntimeError("live artifact vanished while recording an outcome")
            projection = self._contribution_from_row(stamped, scope=scope, minimum_evidence=minimum_evidence)
            return projection, True

    def _assert_outcome_key_unbound(self, *, scope: MemoryScope, artifact_id: str, idempotency_key: str) -> None:
        """Reject an idempotency key already spent on a different artifact."""
        bound = self._engine.fetchvalue(
            """
            SELECT artifact_id FROM live_artifact_outcome_events
            WHERE scope_key = %s AND idempotency_key = %s
            """,
            (scope.key, idempotency_key),
        )
        if bound is not None and str(bound) != artifact_id:
            raise ValueError("live-artifact outcome idempotency key is already bound to a different artifact")

    # ------------------------------------------------------------------ coherence repair monitors

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
        """Start an auditable recovery window after an operator-reviewed repair.

        Opening is idempotent per incident, and the insert decides that rather
        than a preceding read: two replicas opening the same incident cannot both
        insert, and the loser re-reads and applies the same identity check it
        would have applied to a pre-existing monitor.  That is why nothing here
        has to translate a ``psycopg.errors.UniqueViolation`` — one is never
        raised, and the engine has no savepoints, so an aborted transaction could
        not be recovered from inside this call anyway.
        """
        with self._engine.transaction():
            _current, current_digest = self.live_artifact_version(scope=scope, artifact_id=artifact_id)
            if current_digest != repaired_artifact_version_digest:
                raise ValueError("artifact_version_digest does not match the currently registered repaired artifact")
            prior, prior_digest = self.previous_live_artifact_version(
                scope=scope, artifact_id=artifact_id, excluding_digest=current_digest
            )
            created = self._engine.fetchone(
                """
                INSERT INTO coherence_repair_monitors (
                    scope_key, incident_id, relationship_uuid, artifact_id,
                    prior_artifact_version_digest, repaired_artifact_version_digest,
                    prior_payload, status, opened_at, recovered_at, reopened_at
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, 'monitoring', %s, NULL, NULL)
                ON CONFLICT (scope_key, incident_id) DO NOTHING
                RETURNING *
                """,
                (
                    scope.key,
                    incident_id,
                    relationship_uuid,
                    artifact_id,
                    prior_digest,
                    current_digest,
                    json_dumps(prior.model_dump(mode="json"), "repair monitor prior artifact"),
                    datetime_to_text(opened_at),
                ),
            )
            if created is not None:
                return self._coherence_repair_monitor_from_row(created)

            existing = self._engine.fetchone(
                "SELECT * FROM coherence_repair_monitors WHERE scope_key = %s AND incident_id = %s",
                (scope.key, incident_id),
            )
            if existing is None:  # pragma: no cover - deleted between the two statements
                raise RuntimeError("coherence repair monitor vanished while opening")
            monitor = self._coherence_repair_monitor_from_row(existing)
            if monitor.artifact_id != artifact_id or monitor.repaired_artifact_version_digest != current_digest:
                raise ValueError("coherence incident already has a different repair monitor")
            return monitor

    def coherence_repair_monitor(self, *, scope: MemoryScope, incident_id: str) -> CoherenceRepairMonitor | None:
        row = self._engine.fetchone(
            "SELECT * FROM coherence_repair_monitors WHERE scope_key = %s AND incident_id = %s",
            (scope.key, incident_id),
        )
        return self._coherence_repair_monitor_from_row(row) if row is not None else None

    def active_coherence_repair_monitor(
        self, *, scope: MemoryScope, relationship_uuid: str, artifact_id: str
    ) -> CoherenceRepairMonitor | None:
        """Newest still-monitoring window, picked by the relationship index."""
        row = self._engine.fetchone(
            """
            SELECT * FROM coherence_repair_monitors
            WHERE scope_key = %s AND relationship_uuid = %s AND artifact_id = %s
              AND status = 'monitoring'
            ORDER BY opened_at DESC, incident_id DESC LIMIT 1
            """,
            (scope.key, relationship_uuid, artifact_id),
        )
        return self._coherence_repair_monitor_from_row(row) if row is not None else None

    def recover_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, recovered_at: datetime
    ) -> CoherenceRepairMonitor:
        """Close a monitoring window as recovered; idempotent once terminal.

        Reading the status and then writing it is exactly the interleaving that
        would let a recovery and a rollback of the same incident both believe they
        won, so the monitor row is locked for the whole decision.
        """
        with self._engine.transaction():
            monitor = self._lock_coherence_repair_monitor(scope=scope, incident_id=incident_id)
            if monitor.status != _MONITORING:
                return monitor
            updated = self._engine.fetchone(
                """
                UPDATE coherence_repair_monitors
                SET status = 'recovered', recovered_at = %s
                WHERE scope_key = %s AND incident_id = %s
                RETURNING *
                """,
                (datetime_to_text(recovered_at), scope.key, incident_id),
            )
            if updated is None:  # pragma: no cover - the row is locked by this transaction
                raise RuntimeError("coherence repair monitor vanished while recovering")
            return self._coherence_repair_monitor_from_row(updated)

    def rollback_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, reopened_at: datetime
    ) -> CoherenceRepairMonitor:
        """Restore the prior registry projection after a monitored recurrence.

        This swaps only Memotron's live artifact alias/projection.  The
        underlying user-managed file or skill remains untouched for review.

        The monitor row is locked before its status is read, so the rollback and a
        concurrent recovery of the same incident cannot both apply — otherwise the
        registry could be reverted after the incident had already been closed.
        """
        with self._engine.transaction():
            monitor = self._lock_coherence_repair_monitor(scope=scope, incident_id=incident_id)
            if monitor.status != _MONITORING:
                return monitor
            prior = monitor.prior_artifact
            # A blind write: every value comes from the locked monitor row, so
            # nothing here depends on the registry row's current contents.  The
            # outcome counters are deliberately left in place — the rollback
            # changes which projection is live, not the evidence gathered about it.
            self._engine.execute(
                """
                UPDATE live_artifacts
                SET artifact_class = %s,
                    payload = %s::jsonb,
                    updated_at = %s,
                    active = true,
                    quarantined_at = NULL
                WHERE scope_key = %s AND artifact_id = %s
                """,
                (
                    prior.artifact_class.value,
                    prior.model_dump_json(),
                    datetime_to_text(prior.updated_at or reopened_at),
                    scope.key,
                    monitor.artifact_id,
                ),
            )
            updated = self._engine.fetchone(
                """
                UPDATE coherence_repair_monitors
                SET status = 'reopened_rolled_back', reopened_at = %s
                WHERE scope_key = %s AND incident_id = %s
                RETURNING *
                """,
                (datetime_to_text(reopened_at), scope.key, incident_id),
            )
            if updated is None:  # pragma: no cover - the row is locked by this transaction
                raise RuntimeError("coherence repair monitor vanished while rolling back")
            return self._coherence_repair_monitor_from_row(updated)

    def _lock_coherence_repair_monitor(self, *, scope: MemoryScope, incident_id: str) -> CoherenceRepairMonitor:
        """Take the monitor row for update, or raise the interface's ValueError."""
        row = self._engine.fetchone(
            """
            SELECT * FROM coherence_repair_monitors
            WHERE scope_key = %s AND incident_id = %s
            FOR UPDATE
            """,
            (scope.key, incident_id),
        )
        if row is None:
            raise ValueError(f"coherence repair monitor does not exist for incident {incident_id!r}")
        return self._coherence_repair_monitor_from_row(row)

    # ------------------------------------------------------------------ row mapping

    def _coherence_repair_monitor_from_row(self, row: dict[str, Any]) -> CoherenceRepairMonitor:
        scope_key = str(row["scope_key"])
        kind, scope_id = scope_key.split(":", 1)
        prior = self._artifact_from_payload(row["prior_payload"], "persisted coherence repair monitor is invalid")
        return CoherenceRepairMonitor(
            scope=MemoryScope(kind=kind, scope_id=scope_id),
            incident_id=str(row["incident_id"]),
            relationship_uuid=str(row["relationship_uuid"]),
            artifact_id=str(row["artifact_id"]),
            prior_artifact_version_digest=str(row["prior_artifact_version_digest"]),
            repaired_artifact_version_digest=str(row["repaired_artifact_version_digest"]),
            prior_artifact=prior,
            status=str(row["status"]),
            opened_at=datetime.fromisoformat(str(row["opened_at"])),
            recovered_at=optional_datetime_from_text(row["recovered_at"]),
            reopened_at=optional_datetime_from_text(row["reopened_at"]),
        )

    def _contribution_from_row(
        self, row: dict[str, Any], *, scope: MemoryScope, minimum_evidence: int
    ) -> ArtifactContributionProjection:
        positive = int(row["positive_outcomes"])
        negative = int(row["negative_outcomes"])
        total = positive + negative
        return ArtifactContributionProjection(
            scope=scope,
            artifact_id=str(row["artifact_id"]),
            artifact_class=ArtifactClass(str(row["artifact_class"])),
            positive_outcomes=positive,
            negative_outcomes=negative,
            contribution_score=(positive - negative) / total if total else 0.0,
            minimum_evidence_met=total >= minimum_evidence,
            quarantined=not bool(row["active"]),
            last_outcome_at=optional_datetime_from_text(row["last_outcome_at"]),
        )

    @staticmethod
    def _artifact_from_payload(payload: Any, invalid_message: str) -> PersistentArtifact:
        """Rebuild a stored projection, or raise the interface's RuntimeError.

        ``jsonb`` decodes to a Python object already; the text branch is here only
        so a column that has not been migrated yet still reads.
        """
        try:
            if isinstance(payload, (str, bytes, bytearray)):
                return PersistentArtifact.model_validate_json(payload)
            return PersistentArtifact.model_validate(payload)
        except ValueError as exc:
            raise RuntimeError(invalid_message) from exc
