"""The operational plane: the episode queue, dream-run bookkeeping, and use/outcome events.

Mirrors :mod:`memotron.storage.postgres._operational`. This is the durable work
queue -- an episode is claimed, processed, and marked per consumer -- plus the
utility feedback loop that records whether a memory was used and how that turned out."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.crypto import (
    ContentKeyUnavailableError,
    is_sealed_content,
)
from memotron.models import (
    DreamDecisionRecord,
    DreamJobRunRecord,
    Episode,
    MemoryScope,
    OutcomeEvent,
    PruneGhost,
    RelationshipStatus,
    RetrievalNegativeSpaceEntry,
    UseEvent,
    UseEventKind,
)
from memotron.storage._shared import (
    DREAM_CLAIM_STALE_SECONDS as DREAM_CLAIM_STALE_SECONDS,
)
from memotron.storage.base import (
    normalize_key,
)

# ``DREAM_CLAIM_STALE_SECONDS`` is re-exported, not defined, here: it is now
# owned by ``memotron.storage._shared._claims`` alongside the two methods that
# default to it, so the SQLite and Postgres windows can no longer drift. The name
# and value at this path are unchanged, and ``storage/sqlite/__init__.py`` and
# ``admin_server`` keep importing it from where they always did.


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class OperationalPlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    def add_episode(self, episode: Episode) -> None:
        try:
            self._connection.execute(
                "INSERT INTO episodes (uuid, payload_json, created_at) VALUES (?, ?, ?)",
                (
                    episode.uuid,
                    episode.model_dump_json(),
                    self._datetime_to_text(episode.created_at),
                ),
            )
            self._commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"episode already exists: {episode.uuid}") from exc

    def get_episode(self, episode_uuid: str) -> Episode:
        row = self._connection.execute(
            "SELECT payload_json FROM episodes WHERE uuid = ?",
            (episode_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"episode does not exist: {episode_uuid}")
        return Episode.model_validate_json(str(row["payload_json"]))

    def episodes(self) -> list[Episode]:
        rows = self._connection.execute("SELECT payload_json FROM episodes ORDER BY created_at, uuid").fetchall()
        return [Episode.model_validate_json(str(row["payload_json"])) for row in rows]

    def is_episode_processed(self, episode_uuid: str, *, consumer_key: str | None = None) -> bool:
        legacy = self._connection.execute(
            "SELECT 1 FROM processed_episodes WHERE episode_uuid = ?",
            (episode_uuid,),
        ).fetchone()
        if legacy is not None:
            return True
        if consumer_key is None:
            row = self._connection.execute(
                "SELECT 1 FROM episode_processing WHERE episode_uuid = ? LIMIT 1",
                (episode_uuid,),
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT 1 FROM episode_processing
                WHERE episode_uuid = ? AND consumer_key = ?
                """,
                (episode_uuid, normalize_key(consumer_key)),
            ).fetchone()
        return row is not None

    def mark_episode_processed(
        self,
        episode_uuid: str,
        *,
        processed_at: datetime,
        consumer_key: str | None = None,
    ) -> None:
        if consumer_key is None:
            self._connection.execute(
                """
                INSERT INTO processed_episodes (episode_uuid, processed_at)
                VALUES (?, ?)
                ON CONFLICT(episode_uuid) DO NOTHING
                """,
                (episode_uuid, self._datetime_to_text(processed_at)),
            )
        else:
            normalized_consumer = normalize_key(consumer_key)
            if not normalized_consumer:
                raise ValueError("consumer_key cannot be blank")
            self._connection.execute(
                """
                INSERT INTO episode_processing (
                    episode_uuid, consumer_key, processed_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(episode_uuid, consumer_key) DO NOTHING
                """,
                (
                    episode_uuid,
                    normalized_consumer,
                    self._datetime_to_text(processed_at),
                ),
            )
        self._commit()

    def _claim_is_live(
        self,
        claim_key: str,
        *,
        run_uuid: str,
        cutoff: datetime,
    ) -> bool:
        """True when *claim_key* is held by ANOTHER run and has not expired."""
        row = self._connection.execute(
            "SELECT claimed_by_run, claimed_at FROM dream_claims WHERE claim_key = ?",
            (claim_key,),
        ).fetchone()
        if row is None:
            return False
        if str(row["claimed_by_run"]) == run_uuid:
            return False
        return self._datetime_from_text(str(row["claimed_at"])) > cutoff

    def _write_claim(self, claim_key: str, *, run_uuid: str, now: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO dream_claims (claim_key, claimed_by_run, claimed_at)
            VALUES (?, ?, ?)
            ON CONFLICT(claim_key) DO UPDATE SET
                claimed_by_run = excluded.claimed_by_run,
                claimed_at = excluded.claimed_at
            """,
            (claim_key, run_uuid, self._datetime_to_text(now)),
        )

    # ``claim_episodes`` and ``claim_scope_work`` are contributed by
    # ``memotron.storage._shared._claims.DreamClaimPlaneMixin``. They read the
    # queue through ``is_episode_processed`` and take the writer lock through
    # ``exclusive_write_transaction`` — this engine's ``BEGIN IMMEDIATE`` — so the
    # algorithm has no SQLite-specific part left. The parts that DO are
    # ``_claim_is_live`` and ``_write_claim`` immediately above, and they stay here.

    def release_dream_claims(self, run_uuid: str) -> int:
        """Release every claim held by *run_uuid*; returns the count released."""
        if not run_uuid or not run_uuid.strip():
            raise ValueError("run_uuid cannot be blank")
        cursor = self._connection.execute(
            "DELETE FROM dream_claims WHERE claimed_by_run = ?",
            (run_uuid,),
        )
        self._connection.commit()
        return cursor.rowcount

    def get_job_last_run(self, job_name: str) -> datetime | None:
        row = self._connection.execute(
            "SELECT last_run FROM job_state WHERE job_name = ?",
            (job_name,),
        ).fetchone()
        if row is None:
            return None
        return self._datetime_from_text(str(row["last_run"]))

    def set_job_last_run(self, job_name: str, last_run: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO job_state (job_name, last_run)
            VALUES (?, ?)
            ON CONFLICT(job_name) DO UPDATE SET last_run = excluded.last_run
            """,
            (job_name, self._datetime_to_text(last_run)),
        )
        self._commit()

    def record_dream_job_run(self, record: DreamJobRunRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO dream_job_runs (uuid, ran_at, job_name, job_kind, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                record.uuid,
                self._datetime_to_text(record.ran_at),
                record.job_name,
                record.job_kind.value,
                record.model_dump_json(),
            ),
        )
        self._commit()

    def dream_job_runs(
        self,
        *,
        limit: int = 20,
        job_name: str | None = None,
    ) -> list[DreamJobRunRecord]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if job_name is not None and not job_name.strip():
            raise ValueError("job_name cannot be blank")
        if job_name is None:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM dream_job_runs
                ORDER BY ran_at DESC, uuid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT payload_json FROM dream_job_runs
                WHERE job_name = ?
                ORDER BY ran_at DESC, uuid DESC
                LIMIT ?
                """,
                (job_name.strip(), limit),
            ).fetchall()
        return [DreamJobRunRecord.model_validate_json(str(row["payload_json"])) for row in rows]

    def record_dream_decision(self, record: DreamDecisionRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO dream_decisions (uuid, ran_at, job_name, job_kind, agent_id, decision_type, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.uuid,
                self._datetime_to_text(record.ran_at),
                record.job_name,
                record.job_kind.value,
                record.agent_id,
                record.decision_type,
                record.model_dump_json(),
            ),
        )
        self._commit()

    def dream_decisions(
        self,
        *,
        limit: int = 20,
        job_name: str | None = None,
        agent_id: str | None = None,
        decision_type_prefix: str | None = None,
    ) -> list[DreamDecisionRecord]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if job_name is not None and not job_name.strip():
            raise ValueError("job_name cannot be blank")
        if agent_id is not None and not agent_id.strip():
            raise ValueError("agent_id cannot be blank")
        if decision_type_prefix is not None and not decision_type_prefix.strip():
            raise ValueError("decision_type_prefix cannot be blank")

        filters: list[str] = []
        parameters: list[object] = []
        if job_name is not None:
            filters.append("job_name = ?")
            parameters.append(job_name.strip())
        if agent_id is not None:
            filters.append("agent_id = ?")
            parameters.append(agent_id.strip())
        if decision_type_prefix is not None:
            # Seeks dream_decisions_decision_type_idx; '%' is escaped so it is a literal prefix.
            escaped = decision_type_prefix.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append("decision_type LIKE ? ESCAPE '\\'")
            parameters.append(f"{escaped}%")
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        rows = self._connection.execute(
            f"""
            SELECT payload_json FROM dream_decisions
            {where_clause}
            ORDER BY ran_at DESC, uuid DESC
            LIMIT ?
            """,
            (*parameters, limit),
        ).fetchall()
        return [DreamDecisionRecord.model_validate_json(str(row["payload_json"])) for row in rows]

    def record_use_event(self, event: UseEvent) -> UseEvent:
        """Append an immutable use event, enforcing scoped idempotency."""
        relationship = self._memory_graph.get_relationship(event.relationship_uuid)
        relationship_scope = relationship.properties.get("scope_key")
        if relationship_scope != event.scope.key:
            raise ValueError(
                f"use event scope {event.scope.key!r} does not match relationship scope {relationship_scope!r}"
            )
        existing = self._connection.execute(
            "SELECT payload_json FROM memory_use_events WHERE scope_key = ? AND idempotency_key = ?",
            (event.scope.key, event.idempotency_key),
        ).fetchone()
        if existing is not None:
            stored = UseEvent.model_validate_json(str(existing["payload_json"]))
            comparable_event = event.model_copy(update={"use_id": stored.use_id})
            if stored != comparable_event:
                raise ValueError(
                    f"use event idempotency key {event.idempotency_key!r} was already used for a different event"
                )
            return stored
        self._connection.execute(
            """
            INSERT INTO memory_use_events (
                use_id, relationship_uuid, scope_key, kind, task_run_id, idempotency_key,
                used_at, payload_json, query_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.use_id,
                event.relationship_uuid,
                event.scope.key,
                event.kind.value,
                event.task_run_id,
                event.idempotency_key,
                self._datetime_to_text(event.used_at),
                event.model_dump_json(),
                event.query_digest,
            ),
        )
        self._commit()
        return event

    def record_outcome_event(self, event: OutcomeEvent) -> OutcomeEvent:
        """Append an outcome only when its referenced use event exists in scope."""
        use_row = self._connection.execute(
            "SELECT scope_key, task_run_id FROM memory_use_events WHERE use_id = ?",
            (event.use_id,),
        ).fetchone()
        if use_row is None:
            raise ValueError(f"outcome event references unknown use_id {event.use_id!r}")
        if str(use_row["scope_key"]) != event.scope.key:
            raise ValueError("outcome event scope must match the referenced use event scope")
        if str(use_row["task_run_id"]) != event.task_run_id:
            raise ValueError("outcome event task_run_id must match the referenced use event task_run_id")
        existing = self._connection.execute(
            "SELECT payload_json FROM memory_outcome_events WHERE scope_key = ? AND idempotency_key = ?",
            (event.scope.key, event.idempotency_key),
        ).fetchone()
        if existing is not None:
            stored = OutcomeEvent.model_validate_json(str(existing["payload_json"]))
            comparable_event = event.model_copy(update={"outcome_id": stored.outcome_id})
            if stored != comparable_event:
                raise ValueError(
                    f"outcome event idempotency key {event.idempotency_key!r} was already used for a different event"
                )
            return stored
        self._connection.execute(
            """
            INSERT INTO memory_outcome_events (
                outcome_id, use_id, scope_key, task_run_id, idempotency_key, judged_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event.outcome_id,
                event.use_id,
                event.scope.key,
                event.task_run_id,
                event.idempotency_key,
                self._datetime_to_text(event.judged_at),
                event.model_dump_json(),
            ),
        )
        self._commit()
        return event

    def use_events(
        self,
        *,
        scope_key: str | None = None,
        relationship_uuid: str | None = None,
        task_run_id: str | None = None,
    ) -> list[UseEvent]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (
            ("scope_key", scope_key),
            ("relationship_uuid", relationship_uuid),
            ("task_run_id", task_run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT payload_json FROM memory_use_events {where} ORDER BY used_at, use_id", parameters
        ).fetchall()
        return [UseEvent.model_validate_json(str(row["payload_json"])) for row in rows]

    def use_events_for_task_prefix(
        self,
        *,
        scope_key: str,
        task_run_id_prefix: str,
    ) -> list[UseEvent]:
        """WS-15 T8: indexed prefix read over one scope's use events.

        Session-derived task-run ids share a stable ``claude:{session_id}:``
        prefix (adoption.session_task_run_id), so the lifecycle hooks can
        gather every use event a session produced without knowing each
        firing's timestamped suffix.  Seeks the
        ``memory_use_events_task_idx`` (scope_key, task_run_id, used_at)
        index; the prefix is LIKE-escaped so ``_``/``%`` in a session id can
        never widen the match.
        """
        if not task_run_id_prefix:
            raise ValueError("task_run_id_prefix cannot be blank")
        escaped = task_run_id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._connection.execute(
            "SELECT payload_json FROM memory_use_events "
            "WHERE scope_key = ? AND task_run_id LIKE ? ESCAPE '\\' "
            "ORDER BY used_at, use_id",
            (scope_key, f"{escaped}%"),
        ).fetchall()
        return [UseEvent.model_validate_json(str(row["payload_json"])) for row in rows]

    def use_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> UseEvent | None:
        """The use event already stored under (scope, idempotency_key), if any.

        WS-15 T8: lifecycle hooks re-fire (pre-compact then session-end), and a
        re-derived event carries a fresh ``used_at``/``task_run_id`` — writing
        it again through :meth:`record_use_event` would fail the strict
        same-payload idempotency check.  Callers consult this first and treat
        an existing row as the recorded citation."""
        row = self._connection.execute(
            "SELECT payload_json FROM memory_use_events WHERE scope_key = ? AND idempotency_key = ?",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return UseEvent.model_validate_json(str(row["payload_json"]))

    def outcome_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> OutcomeEvent | None:
        """The outcome event already stored under (scope, idempotency_key), if any."""
        row = self._connection.execute(
            "SELECT payload_json FROM memory_outcome_events WHERE scope_key = ? AND idempotency_key = ?",
            (scope_key, idempotency_key),
        ).fetchone()
        if row is None:
            return None
        return OutcomeEvent.model_validate_json(str(row["payload_json"]))

    def outcome_events(
        self,
        *,
        scope_key: str | None = None,
        use_id: str | None = None,
        task_run_id: str | None = None,
    ) -> list[OutcomeEvent]:
        clauses: list[str] = []
        parameters: list[object] = []
        for column, value in (("scope_key", scope_key), ("use_id", use_id), ("task_run_id", task_run_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                parameters.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT payload_json FROM memory_outcome_events {where} ORDER BY judged_at, outcome_id", parameters
        ).fetchall()
        return [OutcomeEvent.model_validate_json(str(row["payload_json"])) for row in rows]

    # ``utility_projection``, ``utility_projection_from_receipts`` and
    # ``_utility_projection_from_events`` are contributed by
    # ``memotron.storage._shared._projection.UtilityProjectionPlaneMixin``. The
    # utility read model is DERIVED: it reads ``use_events``/``outcome_events``
    # and the receipt ledger and touches no fact-plane field, so it has no
    # SQLite-specific part. Its half-life table diverging between two copies is
    # the `anchor` bug that scripts/verify/parity_coverage.py was written for.

    def retrieval_negative_space(
        self, *, scope_key: str, task_run_id: str | None = None
    ) -> list[RetrievalNegativeSpaceEntry]:
        """Return retrieved impressions that never progressed to selection or use."""
        uses = self.use_events(scope_key=scope_key, task_run_id=task_run_id)
        outcomes = {outcome.use_id for outcome in self.outcome_events(scope_key=scope_key, task_run_id=task_run_id)}
        by_relation_task: dict[tuple[str, str], list[UseEvent]] = {}
        for event in uses:
            by_relation_task.setdefault((event.relationship_uuid, event.task_run_id), []).append(event)
        entries: list[RetrievalNegativeSpaceEntry] = []
        for event in uses:
            if event.kind != UseEventKind.RETRIEVED:
                continue
            related = by_relation_task[(event.relationship_uuid, event.task_run_id)]
            injected = any(item.kind == UseEventKind.INJECTED for item in related)
            cited = any(item.kind == UseEventKind.CITED_OR_USED for item in related)
            outcome_recorded = any(item.use_id in outcomes for item in related)
            if not injected and not cited:
                entries.append(
                    RetrievalNegativeSpaceEntry(
                        use_event=event,
                        injected=False,
                        cited_or_used=False,
                        outcome_recorded=outcome_recorded,
                    )
                )
        return entries

    def create_prune_ghost(
        self,
        *,
        relationship_uuid: str,
        scope_key: str,
        prune_receipt_uuid: str,
        reason: str,
        pruned_at: datetime,
    ) -> PruneGhost:
        if not prune_receipt_uuid:
            raise ValueError("prune_receipt_uuid must be non-blank")
        relationship = self._memory_graph.get_relationship(relationship_uuid)
        if relationship.properties.get("scope_key") != scope_key:
            raise ValueError("prune ghost scope must match relationship scope")
        self._connection.execute(
            """
            INSERT INTO memory_prune_ghosts (
                relationship_uuid, scope_key, prune_receipt_uuid, pruned_at, reason, restorable, restored_at
            ) VALUES (?, ?, ?, ?, ?, 1, NULL)
            ON CONFLICT(relationship_uuid) DO UPDATE SET
                scope_key = excluded.scope_key,
                prune_receipt_uuid = excluded.prune_receipt_uuid,
                pruned_at = excluded.pruned_at,
                reason = excluded.reason,
                restorable = 1,
                restored_at = NULL
            """,
            (
                relationship_uuid,
                scope_key,
                prune_receipt_uuid,
                self._datetime_to_text(pruned_at),
                reason,
            ),
        )
        self._commit()
        return self.prune_ghost(relationship_uuid)

    def prune_ghost(self, relationship_uuid: str) -> PruneGhost:
        row = self._connection.execute(
            "SELECT * FROM memory_prune_ghosts WHERE relationship_uuid = ?", (relationship_uuid,)
        ).fetchone()
        if row is None:
            raise ValueError(f"no prune ghost for relationship {relationship_uuid!r}")
        scope_key = str(row["scope_key"])
        if ":" not in scope_key:
            raise ValueError(f"invalid ghost scope key {scope_key!r}")
        kind, scope_id = scope_key.split(":", 1)
        restored = row["restored_at"]
        return PruneGhost(
            relationship_uuid=str(row["relationship_uuid"]),
            scope=MemoryScope(kind=kind, scope_id=scope_id),
            prune_receipt_uuid=str(row["prune_receipt_uuid"]),
            pruned_at=self._datetime_from_text(str(row["pruned_at"])),
            reason=str(row["reason"]),
            restorable=bool(row["restorable"]),
            restored_at=self._datetime_from_text(str(restored)) if restored is not None else None,
        )

    def prune_ghosts(self, *, scope_key: str, restorable_only: bool = False) -> list[PruneGhost]:
        sql = "SELECT relationship_uuid FROM memory_prune_ghosts WHERE scope_key = ?"
        parameters: list[object] = [scope_key]
        if restorable_only:
            sql += " AND restorable = 1 AND restored_at IS NULL"
        rows = self._connection.execute(sql + " ORDER BY pruned_at, relationship_uuid", parameters).fetchall()
        return [self.prune_ghost(str(row["relationship_uuid"])) for row in rows]

    def restore_prune_ghost(self, relationship_uuid: str, *, restored_at: datetime) -> PruneGhost:
        ghost = self.prune_ghost(relationship_uuid)
        if not ghost.restorable:
            raise ValueError(f"prune ghost {relationship_uuid!r} is not restorable")
        relationship = self._memory_graph.get_relationship(relationship_uuid)
        fact = relationship.properties.get("fact")
        if is_sealed_content(fact) and self.get_governance_key(ghost.scope.key) is None:
            self._connection.execute(
                "UPDATE memory_prune_ghosts SET restorable = 0 WHERE relationship_uuid = ?",
                (relationship_uuid,),
            )
            self._commit()
            raise ContentKeyUnavailableError(
                f"prune ghost {relationship_uuid!r} is crypto-shredded and cannot be restored"
            )
        self._memory_graph.update_relationship(
            relationship_uuid,
            properties={
                "status": RelationshipStatus.ACTIVE.value,
                "active_in_context": True,
                "archive_tier": False,
                "restored_from_ghost_at": restored_at.isoformat(),
            },
            clear_valid_to=True,
        )
        self._connection.execute(
            "UPDATE memory_prune_ghosts SET restored_at = ? WHERE relationship_uuid = ?",
            (self._datetime_to_text(restored_at), relationship_uuid),
        )
        self._commit()
        return self.prune_ghost(relationship_uuid)

    def record_promotion_endorsement(
        self,
        *,
        candidate_episode_uuid: str,
        agent_id: str,
        rationale: str,
        endorsed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one agent's endorsement of a promotion candidate episode.

        Idempotent per (candidate_episode_uuid, agent_id): a re-endorsement is a
        no-op — the FIRST rationale and timestamp stand, so the vote count is
        strictly one per agent.  Returns the stored row plus ``created``.
        """
        normalized_episode = self._normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        normalized_agent = self._normalize_agent_id(agent_id)
        normalized_rationale = self._normalize_non_blank(rationale, "rationale")
        cursor = self._connection.execute(
            """
            INSERT INTO promotion_endorsements (
                candidate_episode_uuid, agent_id, rationale, endorsed_at
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT(candidate_episode_uuid, agent_id) DO NOTHING
            """,
            (
                normalized_episode,
                normalized_agent,
                normalized_rationale,
                (endorsed_at or datetime.now(UTC)).isoformat(),
            ),
        )
        self._connection.commit()
        created = cursor.rowcount > 0
        row = self._connection.execute(
            """
            SELECT candidate_episode_uuid, agent_id, rationale, endorsed_at
            FROM promotion_endorsements
            WHERE candidate_episode_uuid = ? AND agent_id = ?
            """,
            (normalized_episode, normalized_agent),
        ).fetchone()
        return {
            "candidate_episode_uuid": str(row["candidate_episode_uuid"]),
            "agent_id": str(row["agent_id"]),
            "rationale": str(row["rationale"]),
            "endorsed_at": str(row["endorsed_at"]),
            "created": created,
        }

    def promotion_endorsements_for(self, candidate_episode_uuid: str) -> tuple[dict[str, Any], ...]:
        """Every endorsement for one candidate, in deterministic vote order."""
        normalized_episode = self._normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        rows = self._connection.execute(
            """
            SELECT candidate_episode_uuid, agent_id, rationale, endorsed_at
            FROM promotion_endorsements
            WHERE candidate_episode_uuid = ?
            ORDER BY endorsed_at, agent_id
            """,
            (normalized_episode,),
        ).fetchall()
        return tuple(
            {
                "candidate_episode_uuid": str(row["candidate_episode_uuid"]),
                "agent_id": str(row["agent_id"]),
                "rationale": str(row["rationale"]),
                "endorsed_at": str(row["endorsed_at"]),
            }
            for row in rows
        )

    def promotion_endorsement_count(self, candidate_episode_uuid: str) -> int:
        """Distinct endorsing agents for one candidate (index-backed)."""
        normalized_episode = self._normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        row = self._connection.execute(
            """
            SELECT COUNT(*) AS endorsement_count
            FROM promotion_endorsements
            WHERE candidate_episode_uuid = ?
            """,
            (normalized_episode,),
        ).fetchone()
        return int(row["endorsement_count"])

    def episodes_for_scope(self, scope_key: str) -> list[Episode]:
        """All stored episodes whose scope key matches (erasure sweep + shred retire)."""
        return [episode for episode in self.episodes() if episode.scope.key == scope_key]

    def episode_event_counts(
        self,
        *,
        scope: MemoryScope,
        event: str,
    ) -> tuple[int, int]:
        """Return total and globally pending episode counts without hydrating the store."""

        normalized_event = self._normalize_non_blank(event, "event")
        row = self._connection.execute(
            """
            SELECT
                COUNT(*) AS total_count,
                COALESCE(
                    SUM(
                        CASE
                            WHEN processed.episode_uuid IS NULL
                             AND NOT EXISTS (
                                SELECT 1
                                FROM episode_processing AS processing
                                WHERE processing.episode_uuid = episode.uuid
                             )
                            THEN 1
                            ELSE 0
                        END
                    ),
                    0
                ) AS pending_count
            FROM episodes AS episode
            LEFT JOIN processed_episodes AS processed
                ON processed.episode_uuid = episode.uuid
            WHERE json_extract(episode.payload_json, '$.scope.kind') = ?
              AND json_extract(episode.payload_json, '$.scope.scope_id') = ?
              AND json_extract(
                    episode.payload_json,
                    '$.metadata.agent_memory_event'
                  ) = ?
            """,
            (scope.kind.value, scope.scope_id, normalized_event),
        ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row["total_count"]), int(row["pending_count"]))

    def dream_decisions_for_scope(self, scope_key: str) -> list[DreamDecisionRecord]:
        """All persisted dream-agent decisions targeting this scope (erasure sweep)."""
        rows = self._connection.execute("SELECT payload_json FROM dream_decisions ORDER BY ran_at, uuid").fetchall()
        result: list[DreamDecisionRecord] = []
        for row in rows:
            record = DreamDecisionRecord.model_validate_json(str(row["payload_json"]))
            if record.scope is not None and record.scope.key == scope_key:
                result.append(record)
        return result
