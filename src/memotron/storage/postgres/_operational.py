"""Operational plane on Postgres: episode queue, job state, events, prune ghosts.

Five things differ from the SQLite implementation.  Each is a correctness or cost
property of running two replicas against one store, not an incidental porting
choice:

1. **The idempotent event writes are race-free.**  ``record_use_event`` and
   ``record_outcome_event`` used to read the ``(scope_key, idempotency_key)`` row
   and insert when it was absent.  Under two replicas replaying the same task run
   both see nothing, both insert, and the loser surfaces a raw integrity error
   instead of the ValueError the interface promises — or worse, a second event
   row for one logical use.  Here the insert *is* the probe: ``ON CONFLICT
   (scope_key, idempotency_key) DO NOTHING RETURNING`` lets the unique index
   arbitrate, and the stored payload is read back only on the losing branch.  The
   comparison that separates "same event replayed" from "key reused for a
   different event" is unchanged, so the messages and return values match.

2. **Read-then-write holds the row.**  ``restore_prune_ghost`` decides
   restorability from the ghost row and then stamps it; ``create_prune_ghost``
   re-stamps a ghost that may already exist.  The first takes ``SELECT ... FOR
   UPDATE`` inside the transaction, the second is a single upsert, so a
   concurrent restore cannot be lost.  One deliberate exception is documented at
   :meth:`OperationalPlaneMixin.restore_prune_ghost`: the demotion of a
   crypto-shredded ghost has to *survive* the exception raised right after it.

3. **The scope filters are queries.**  ``episodes_for_scope`` and
   ``dream_decisions_for_scope`` hydrated and validated every row in the table to
   compare one scope key in Python, so an erasure sweep cost a full store read
   per scope.  They are now jsonb predicates on the scope terms;
   ``episodes_scope_event_idx`` serves the episode one on its leading columns.

4. **The negative space is one grouped query.**  The injected / cited /
   outcome-recorded flags per ``(memory, task run)`` are computed by the server
   in a single statement rather than by bucketing the whole scope's use events in
   a Python dict.

5. **Fan-out reads collapse.**  ``prune_ghosts`` listed ids and then re-read each
   ghost by primary key; ``is_episode_processed`` issued two round trips to check
   the legacy table and then the per-consumer table.  Both are single statements.

The projection maths used to be carried over unchanged here, as a second copy,
under a note saying it was pure computation over events and receipts with only
the SQL beneath it differing. That was accurate, and a second copy of code whose
only justification is that it is identical is a divergence waiting to happen --
``parity_coverage.py`` opens with the time it did happen, an ``anchor`` half-life
of 90 days here against 365 on SQLite. ``utility_projection``,
``utility_projection_from_receipts`` and ``_utility_projection_from_events`` are
now contributed once, by
:class:`memotron.storage._shared._projection.UtilityProjectionPlaneMixin`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg import errors as psycopg_errors

from memotron.crypto import ContentKeyUnavailableError, is_sealed_content
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
from memotron.storage import _shared as _shared_planes
from memotron.storage.base import normalize_key
from memotron.storage.postgres._common import as_json
from memotron.storage.postgres._engine import (
    datetime_to_text,
    optional_datetime_from_text,
)

# ``(total, globally-pending)`` for one agent-memory event kind in one aggregate.
#
# "Globally pending" means no consumer has claimed the episode: neither the
# legacy whole-episode marker nor any per-consumer row.  Keeping this as one
# statement matters because the adoption surface polls it per scope per event
# kind; the WHERE terms are the leading columns of episodes_scope_event_idx.
_EPISODE_EVENT_COUNTS_SQL = """
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
WHERE episode.payload->'scope'->>'kind' = %s
  AND episode.payload->'scope'->>'scope_id' = %s
  AND episode.payload->'metadata'->>'agent_memory_event' = %s
"""


def _normalize_non_blank(value: str, label: str) -> str:
    """Trim and reject, with the SQLite backend's wording.

    Module-level rather than a method so it cannot collide with the identical
    helper other plane mixins carry into the same MRO.
    """
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be blank")
    return normalized


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class OperationalPlaneMixin(_Base):
    """Implements the queue, job, event, and ghost surface of ``OperationalStorage``."""

    # Provided by the composing backend.
    _engine: Any
    _memory_graph: Any
    receipts: Any
    # Provided by a sibling mixin in the same MRO: the scope DEK lookup that decides
    # whether a ghost's content still exists. Declared in DOMAIN_CYCLES
    # (scripts/verify/mixin_dag.py) as genuine domain coupling -- crypto-shred is
    # irreversible, so restore_prune_ghost has to consult governance key state.
    get_governance_key: Any

    # ------------------------------------------------------------------ episode queue

    def add_episode(self, episode: Episode) -> None:
        try:
            self._engine.execute(
                "INSERT INTO episodes (uuid, payload, created_at) VALUES (%s, %s::jsonb, %s)",
                (
                    episode.uuid,
                    episode.model_dump_json(),
                    datetime_to_text(episode.created_at),
                ),
            )
        except psycopg_errors.UniqueViolation as exc:
            raise ValueError(f"episode already exists: {episode.uuid}") from exc

    def get_episode(self, episode_uuid: str) -> Episode:
        row = self._engine.fetchone("SELECT payload FROM episodes WHERE uuid = %s", (episode_uuid,))
        if row is None:
            raise ValueError(f"episode does not exist: {episode_uuid}")
        return Episode.model_validate(as_json(row["payload"]))

    def episodes(self) -> list[Episode]:
        rows = self._engine.fetchall("SELECT payload FROM episodes ORDER BY created_at, uuid")
        return [Episode.model_validate(as_json(row["payload"])) for row in rows]

    def episodes_for_scope(self, scope_key: str) -> list[Episode]:
        """All episodes for one scope (erasure sweep + shred retire).

        Indexed jsonb equality on the two scope terms instead of validating every
        stored episode to compare one string: the erasure sweep runs this once per
        scope, and a shred retire runs it over a store it is about to rewrite.
        """
        scope_terms = self._split_scope_key(scope_key)
        if scope_terms is None:
            return []
        rows = self._engine.fetchall(
            """
            SELECT payload FROM episodes
            WHERE payload->'scope'->>'kind' = %s
              AND payload->'scope'->>'scope_id' = %s
            ORDER BY created_at, uuid
            """,
            scope_terms,
        )
        return [Episode.model_validate(as_json(row["payload"])) for row in rows]

    def episode_event_counts(self, *, scope: MemoryScope, event: str) -> tuple[int, int]:
        """``(total, globally-pending)`` counts for one agent-memory event kind."""
        normalized_event = _normalize_non_blank(event, "event")
        row = self._engine.fetchone(
            _EPISODE_EVENT_COUNTS_SQL,
            (scope.kind.value, scope.scope_id, normalized_event),
        )
        if row is None:  # pragma: no cover - an aggregate always returns one row
            return (0, 0)
        return (int(row["total_count"]), int(row["pending_count"]))

    def is_episode_processed(self, episode_uuid: str, *, consumer_key: str | None = None) -> bool:
        """Whether the episode was processed (optionally by one consumer).

        The legacy whole-episode marker still wins outright, even when a consumer
        is named: rows written before the per-consumer table existed have no
        consumer to attribute, and re-processing them would double-count.  Both
        probes ride one round trip.
        """
        clauses = "episode_uuid = %s"
        params: list[Any] = [episode_uuid, episode_uuid]
        if consumer_key is not None:
            clauses += " AND consumer_key = %s"
            params.append(normalize_key(consumer_key))
        return bool(
            self._engine.fetchvalue(
                f"""
                SELECT EXISTS (SELECT 1 FROM processed_episodes WHERE episode_uuid = %s)
                    OR EXISTS (SELECT 1 FROM episode_processing WHERE {clauses})
                """,
                params,
            )
        )

    def mark_episode_processed(
        self,
        episode_uuid: str,
        *,
        processed_at: datetime,
        consumer_key: str | None = None,
    ) -> None:
        """Record processing; idempotent per (episode, consumer).

        ``DO NOTHING`` keeps the *first* processing timestamp, so a replayed mark
        never moves the marker a receipt already anchored.
        """
        if consumer_key is None:
            self._engine.execute(
                """
                INSERT INTO processed_episodes (episode_uuid, processed_at)
                VALUES (%s, %s)
                ON CONFLICT (episode_uuid) DO NOTHING
                """,
                (episode_uuid, datetime_to_text(processed_at)),
            )
            return
        normalized_consumer = normalize_key(consumer_key)
        if not normalized_consumer:
            raise ValueError("consumer_key cannot be blank")
        self._engine.execute(
            """
            INSERT INTO episode_processing (episode_uuid, consumer_key, processed_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (episode_uuid, consumer_key) DO NOTHING
            """,
            (episode_uuid, normalized_consumer, datetime_to_text(processed_at)),
        )

    # ------------------------------------------------------------------ job state

    def get_job_last_run(self, job_name: str) -> datetime | None:
        value = self._engine.fetchvalue("SELECT last_run FROM job_state WHERE job_name = %s", (job_name,))
        return optional_datetime_from_text(value)

    def set_job_last_run(self, job_name: str, last_run: datetime) -> None:
        self._engine.execute(
            """
            INSERT INTO job_state (job_name, last_run)
            VALUES (%s, %s)
            ON CONFLICT (job_name) DO UPDATE SET last_run = excluded.last_run
            """,
            (job_name, datetime_to_text(last_run)),
        )

    # ------------------------------------------------------------------ maintenance history

    def record_dream_job_run(self, record: DreamJobRunRecord) -> None:
        self._engine.execute(
            """
            INSERT INTO dream_job_runs (uuid, ran_at, job_name, job_kind, payload)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            """,
            (
                record.uuid,
                datetime_to_text(record.ran_at),
                record.job_name,
                record.job_kind.value,
                record.model_dump_json(),
            ),
        )

    def dream_job_runs(self, *, limit: int = 20, job_name: str | None = None) -> list[DreamJobRunRecord]:
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if job_name is not None and not job_name.strip():
            raise ValueError("job_name cannot be blank")
        clause = ""
        params: list[Any] = []
        if job_name is not None:
            clause = "WHERE job_name = %s"
            params.append(job_name.strip())
        params.append(limit)
        rows = self._engine.fetchall(
            f"""
            SELECT payload FROM dream_job_runs
            {clause}
            ORDER BY ran_at DESC, uuid DESC
            LIMIT %s
            """,
            params,
        )
        return [DreamJobRunRecord.model_validate(as_json(row["payload"])) for row in rows]

    def record_dream_decision(self, record: DreamDecisionRecord) -> None:
        self._engine.execute(
            """
            INSERT INTO dream_decisions (
                uuid, ran_at, job_name, job_kind, agent_id, decision_type, payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (
                record.uuid,
                datetime_to_text(record.ran_at),
                record.job_name,
                record.job_kind.value,
                record.agent_id,
                record.decision_type,
                record.model_dump_json(),
            ),
        )

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
        params: list[Any] = []
        if job_name is not None:
            filters.append("job_name = %s")
            params.append(job_name.strip())
        if agent_id is not None:
            filters.append("agent_id = %s")
            params.append(agent_id.strip())
        if decision_type_prefix is not None:
            # Seeks dream_decisions_decision_type_idx (text_pattern_ops).  The
            # wildcards are escaped so a caller's '%' stays a literal character
            # rather than widening the prefix to a scan of every decision.
            escaped = decision_type_prefix.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            filters.append("decision_type LIKE %s ESCAPE '\\'")
            params.append(f"{escaped}%")
        where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
        params.append(limit)
        rows = self._engine.fetchall(
            f"""
            SELECT payload FROM dream_decisions
            {where_clause}
            ORDER BY ran_at DESC, uuid DESC
            LIMIT %s
            """,
            params,
        )
        return [DreamDecisionRecord.model_validate(as_json(row["payload"])) for row in rows]

    def dream_decisions_for_scope(self, scope_key: str) -> list[DreamDecisionRecord]:
        """All decisions targeting one scope (erasure sweep).

        ``scope`` is optional on the record, and a NULL jsonb member yields NULL
        here, so the equality drops unscoped decisions exactly as the Python
        ``record.scope is not None`` guard did.

        Note this predicate is *not* index-backed.  ``dream_decisions_scope_idx``
        covers ``payload->>'scope_key'``, which a ``DreamDecisionRecord`` dump
        never carries — its scope is a nested object, and ``MemoryScope.key`` is a
        property rather than a serialised field.  Filtering on the terms the
        payload actually has is still far cheaper than validating every row in
        Python; closing the gap properly is a migration adding an index on
        ``((payload->'scope'->>'kind'), (payload->'scope'->>'scope_id'))``, not a
        different query here.
        """
        scope_terms = self._split_scope_key(scope_key)
        if scope_terms is None:
            return []
        rows = self._engine.fetchall(
            """
            SELECT payload FROM dream_decisions
            WHERE payload->'scope'->>'kind' = %s
              AND payload->'scope'->>'scope_id' = %s
            ORDER BY ran_at, uuid
            """,
            scope_terms,
        )
        return [DreamDecisionRecord.model_validate(as_json(row["payload"])) for row in rows]

    # ------------------------------------------------------------------ use / outcome events

    def record_use_event(self, event: UseEvent) -> UseEvent:
        """Append an immutable use event, enforcing scoped idempotency.

        The insert doubles as the idempotency probe.  A bare SELECT first would
        let two replicas replaying one task run both decide the key is free; the
        unique index on ``(scope_key, idempotency_key)`` decides instead, and only
        the losing branch pays a read.
        """
        with self._engine.transaction():
            # Cross-plane check through the graph reference, never a local table:
            # in a split deployment the relationship lives in the Memory Graph.
            relationship = self._memory_graph.get_relationship(event.relationship_uuid)
            relationship_scope = relationship.properties.get("scope_key")
            if relationship_scope != event.scope.key:
                raise ValueError(
                    f"use event scope {event.scope.key!r} does not match relationship scope {relationship_scope!r}"
                )
            inserted = self._engine.fetchone(
                """
                INSERT INTO memory_use_events (
                    use_id, relationship_uuid, scope_key, kind, task_run_id,
                    idempotency_key, used_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (scope_key, idempotency_key) DO NOTHING
                RETURNING use_id
                """,
                (
                    event.use_id,
                    event.relationship_uuid,
                    event.scope.key,
                    event.kind.value,
                    event.task_run_id,
                    event.idempotency_key,
                    datetime_to_text(event.used_at),
                    event.model_dump_json(),
                ),
            )
            if inserted is not None:
                return event
            stored = self._stored_use_event(event.scope.key, event.idempotency_key)
            # The use_id is server-side identity, not part of the caller's
            # intent, so it is normalised away before the payloads are compared.
            comparable_event = event.model_copy(update={"use_id": stored.use_id})
            if stored != comparable_event:
                raise ValueError(
                    f"use event idempotency key {event.idempotency_key!r} was already used for a different event"
                )
            return stored

    def record_outcome_event(self, event: OutcomeEvent) -> OutcomeEvent:
        """Append an outcome only when its referenced use event exists in scope."""
        with self._engine.transaction():
            use_row = self._engine.fetchone(
                "SELECT scope_key, task_run_id FROM memory_use_events WHERE use_id = %s",
                (event.use_id,),
            )
            if use_row is None:
                raise ValueError(f"outcome event references unknown use_id {event.use_id!r}")
            if str(use_row["scope_key"]) != event.scope.key:
                raise ValueError("outcome event scope must match the referenced use event scope")
            if str(use_row["task_run_id"]) != event.task_run_id:
                raise ValueError("outcome event task_run_id must match the referenced use event task_run_id")
            inserted = self._engine.fetchone(
                """
                INSERT INTO memory_outcome_events (
                    outcome_id, use_id, scope_key, task_run_id, idempotency_key,
                    judged_at, payload
                ) VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (scope_key, idempotency_key) DO NOTHING
                RETURNING outcome_id
                """,
                (
                    event.outcome_id,
                    event.use_id,
                    event.scope.key,
                    event.task_run_id,
                    event.idempotency_key,
                    datetime_to_text(event.judged_at),
                    event.model_dump_json(),
                ),
            )
            if inserted is not None:
                return event
            stored = self._stored_outcome_event(event.scope.key, event.idempotency_key)
            comparable_event = event.model_copy(update={"outcome_id": stored.outcome_id})
            if stored != comparable_event:
                raise ValueError(
                    f"outcome event idempotency key {event.idempotency_key!r} was already used for a different event"
                )
            return stored

    def _stored_use_event(self, scope_key: str, idempotency_key: str) -> UseEvent:
        """Read back the row that won the idempotency race.

        No ``FOR UPDATE``: event rows are append-only, so there is nothing for a
        concurrent writer to change under the comparison.
        """
        row = self._engine.fetchone(
            "SELECT payload FROM memory_use_events WHERE scope_key = %s AND idempotency_key = %s",
            (scope_key, idempotency_key),
        )
        if row is None:  # pragma: no cover - the conflicting row cannot be deleted
            raise ValueError(f"use event idempotency key {idempotency_key!r} conflicted but could not be read back")
        return UseEvent.model_validate(as_json(row["payload"]))

    def _stored_outcome_event(self, scope_key: str, idempotency_key: str) -> OutcomeEvent:
        row = self._engine.fetchone(
            "SELECT payload FROM memory_outcome_events WHERE scope_key = %s AND idempotency_key = %s",
            (scope_key, idempotency_key),
        )
        if row is None:  # pragma: no cover - the conflicting row cannot be deleted
            raise ValueError(f"outcome event idempotency key {idempotency_key!r} conflicted but could not be read back")
        return OutcomeEvent.model_validate(as_json(row["payload"]))

    def use_events(
        self,
        *,
        scope_key: str | None = None,
        relationship_uuid: str | None = None,
        task_run_id: str | None = None,
    ) -> list[UseEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("scope_key", scope_key),
            ("relationship_uuid", relationship_uuid),
            ("task_run_id", task_run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._engine.fetchall(
            f"SELECT payload FROM memory_use_events {where} ORDER BY used_at, use_id",
            params,
        )
        return [UseEvent.model_validate(as_json(row["payload"])) for row in rows]

    def outcome_events(
        self,
        *,
        scope_key: str | None = None,
        use_id: str | None = None,
        task_run_id: str | None = None,
    ) -> list[OutcomeEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (
            ("scope_key", scope_key),
            ("use_id", use_id),
            ("task_run_id", task_run_id),
        ):
            if value is not None:
                clauses.append(f"{column} = %s")
                params.append(value)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._engine.fetchall(
            f"SELECT payload FROM memory_outcome_events {where} ORDER BY judged_at, outcome_id",
            params,
        )
        return [OutcomeEvent.model_validate(as_json(row["payload"])) for row in rows]

    def use_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> UseEvent | None:
        """The use event already stored under (scope, idempotency_key), if any."""
        row = self._engine.fetchone(
            "SELECT payload FROM memory_use_events WHERE scope_key = %s AND idempotency_key = %s",
            (scope_key, idempotency_key),
        )
        if row is None:
            return None
        return UseEvent.model_validate(as_json(row["payload"]))

    def outcome_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> OutcomeEvent | None:
        """The outcome event already stored under (scope, idempotency_key), if any."""
        row = self._engine.fetchone(
            "SELECT payload FROM memory_outcome_events WHERE scope_key = %s AND idempotency_key = %s",
            (scope_key, idempotency_key),
        )
        if row is None:
            return None
        return OutcomeEvent.model_validate(as_json(row["payload"]))

    def use_events_for_task_prefix(self, *, scope_key: str, task_run_id_prefix: str) -> list[UseEvent]:
        """WS-15 T8: indexed prefix read over one scope's use events.

        The prefix is LIKE-escaped so ``_``/``%`` in a session id can never
        widen the match, matching the SQLite substrate exactly.
        """
        if not task_run_id_prefix:
            raise ValueError("task_run_id_prefix cannot be blank")
        escaped = task_run_id_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = self._engine.fetchall(
            "SELECT payload FROM memory_use_events "
            "WHERE scope_key = %s AND task_run_id LIKE %s "
            "ORDER BY used_at, use_id",
            (scope_key, f"{escaped}%"),
        )
        return [UseEvent.model_validate(as_json(row["payload"])) for row in rows]

    # ------------------------------------------------------------------ dream-worker claims

    # 15 minutes. This used to read ``_DREAM_CLAIM_STALE_SECONDS = 900.0`` under
    # a comment saying it was "kept in sync by value so the operational plane
    # never imports the SQLite backend module" -- true, and the reason two copies
    # existed. Neither copy owns it now: it lives in
    # ``memotron.storage._shared._claims`` next to the two methods that default
    # to it, which no more imports the SQLite backend than this did. The name is
    # kept because it is part of this class's recorded surface.
    #
    # Reached through the MODULE, not `from ... import DREAM_CLAIM_STALE_SECONDS`.
    # A from-import binds the name in THIS module's namespace, and
    # scripts/verify/api_surface.py records every module-level binding -- so the
    # plain import silently added `storage.postgres._operational::
    # DREAM_CLAIM_STALE_SECONDS` to the recorded surface, a name this module never
    # exported. (The SQLite twin does carry it, deliberately and since before this
    # change: the constant was DEFINED there, and `storage/sqlite/__init__.py`,
    # `memotron.graph` and `admin_server` still import it from that path. This
    # module has no such consumer.) api_surface skips module objects, so the
    # qualified reference keeps the surface honest instead of blessing a new name.
    _DREAM_CLAIM_STALE_SECONDS = _shared_planes.DREAM_CLAIM_STALE_SECONDS

    # ``claim_episodes`` and ``claim_scope_work`` are contributed by
    # ``memotron.storage._shared._claims.DreamClaimPlaneMixin``. They take the
    # writer lock through ``exclusive_write_transaction`` -- this engine's
    # ``pg_advisory_xact_lock`` -- so the algorithm has no Postgres-specific part
    # left. The parts that DO are ``_claim_is_live`` and ``_write_claim`` below.

    def _claim_is_live(self, claim_key: str, *, run_uuid: str, cutoff: datetime) -> bool:
        """True when *claim_key* is held by ANOTHER run and has not expired."""
        row = self._engine.fetchone(
            "SELECT claimed_by_run, claimed_at FROM dream_claims WHERE claim_key = %s",
            (claim_key,),
        )
        if row is None:
            return False
        if str(row["claimed_by_run"]) == run_uuid:
            return False
        claimed_at = optional_datetime_from_text(row["claimed_at"])
        return claimed_at is not None and claimed_at > cutoff

    def _write_claim(self, claim_key: str, *, run_uuid: str, now: datetime) -> None:
        self._engine.execute(
            """
            INSERT INTO dream_claims (claim_key, claimed_by_run, claimed_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (claim_key) DO UPDATE SET
                claimed_by_run = excluded.claimed_by_run,
                claimed_at = excluded.claimed_at
            """,
            (claim_key, run_uuid, datetime_to_text(now)),
        )

    def release_dream_claims(self, run_uuid: str) -> int:
        """Release every claim held by *run_uuid*; returns the count released."""
        if not run_uuid or not run_uuid.strip():
            raise ValueError("run_uuid cannot be blank")
        return self._engine.execute("DELETE FROM dream_claims WHERE claimed_by_run = %s", (run_uuid,))

    # ------------------------------------------------------------------ promotion endorsements

    def record_promotion_endorsement(
        self,
        *,
        candidate_episode_uuid: str,
        agent_id: str,
        rationale: str,
        endorsed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one agent's endorsement of a promotion candidate (idempotent per agent)."""
        normalized_episode = _normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        normalized_agent = _normalize_non_blank(agent_id, "agent_id")
        normalized_rationale = _normalize_non_blank(rationale, "rationale")
        with self._engine.transaction():
            inserted = self._engine.fetchone(
                """
                INSERT INTO promotion_endorsements (
                    candidate_episode_uuid, agent_id, rationale, endorsed_at
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (candidate_episode_uuid, agent_id) DO NOTHING
                RETURNING candidate_episode_uuid
                """,
                (
                    normalized_episode,
                    normalized_agent,
                    normalized_rationale,
                    (endorsed_at or datetime.now(UTC)).isoformat(),
                ),
            )
            created = inserted is not None
            row = self._engine.fetchone(
                """
                SELECT candidate_episode_uuid, agent_id, rationale, endorsed_at
                FROM promotion_endorsements
                WHERE candidate_episode_uuid = %s AND agent_id = %s
                """,
                (normalized_episode, normalized_agent),
            )
        return {
            "candidate_episode_uuid": str(row["candidate_episode_uuid"]),
            "agent_id": str(row["agent_id"]),
            "rationale": str(row["rationale"]),
            "endorsed_at": str(row["endorsed_at"]),
            "created": created,
        }

    def promotion_endorsements_for(self, candidate_episode_uuid: str) -> tuple[dict[str, Any], ...]:
        """Every endorsement for one candidate, in deterministic vote order."""
        normalized_episode = _normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        rows = self._engine.fetchall(
            """
            SELECT candidate_episode_uuid, agent_id, rationale, endorsed_at
            FROM promotion_endorsements
            WHERE candidate_episode_uuid = %s
            ORDER BY endorsed_at COLLATE "C", agent_id COLLATE "C"
            """,
            (normalized_episode,),
        )
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
        normalized_episode = _normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        return int(
            self._engine.fetchvalue(
                "SELECT COUNT(*) FROM promotion_endorsements WHERE candidate_episode_uuid = %s",
                (normalized_episode,),
            )
        )

    # ------------------------------------------------------------------ utility projection

    def retrieval_negative_space(
        self, *, scope_key: str, task_run_id: str | None = None
    ) -> list[RetrievalNegativeSpaceEntry]:
        """Retrieved impressions that never progressed to selection or use.

        The grouping is the query.  SQLite hydrated every use event in the scope
        and bucketed them by ``(relationship, task run)`` in Python to answer
        three booleans; here ``bool_or`` answers them per group and the outer
        select keeps only the impressions whose group never progressed.  The
        outcome flag is deliberately group-wide, not per impression: an outcome
        judged against a *sibling* use in the same run still counts as feedback
        on that run.
        """
        use_clauses = ["scope_key = %s"]
        use_params: list[Any] = [scope_key]
        outcome_clauses = ["scope_key = %s"]
        outcome_params: list[Any] = [scope_key]
        if task_run_id is not None:
            use_clauses.append("task_run_id = %s")
            use_params.append(task_run_id)
            # The outcome side is filtered independently rather than joined
            # through the impression's run, matching the unfiltered case where
            # SQLite's outcome set spans the whole scope.
            outcome_clauses.append("task_run_id = %s")
            outcome_params.append(task_run_id)
        rows = self._engine.fetchall(
            f"""
            WITH scoped_uses AS (
                SELECT use_id, relationship_uuid, task_run_id, kind, used_at, payload
                FROM memory_use_events
                WHERE {" AND ".join(use_clauses)}
            ),
            scoped_outcomes AS (
                SELECT DISTINCT use_id FROM memory_outcome_events
                WHERE {" AND ".join(outcome_clauses)}
            ),
            progression AS (
                SELECT relationship_uuid,
                       task_run_id,
                       bool_or(kind = ANY(%s)) AS progressed,
                       bool_or(use_id IN (SELECT use_id FROM scoped_outcomes))
                           AS outcome_recorded
                FROM scoped_uses
                GROUP BY relationship_uuid, task_run_id
            )
            SELECT impression.payload, progression.outcome_recorded
            FROM scoped_uses AS impression
            JOIN progression
              ON progression.relationship_uuid = impression.relationship_uuid
             AND progression.task_run_id = impression.task_run_id
            WHERE impression.kind = %s
              AND NOT progression.progressed
            ORDER BY impression.used_at, impression.use_id
            """,
            [
                *use_params,
                *outcome_params,
                [UseEventKind.INJECTED.value, UseEventKind.CITED_OR_USED.value],
                UseEventKind.RETRIEVED.value,
            ],
        )
        return [
            RetrievalNegativeSpaceEntry(
                use_event=UseEvent.model_validate(as_json(row["payload"])),
                injected=False,
                cited_or_used=False,
                outcome_recorded=bool(row["outcome_recorded"]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------ prune ghosts

    def create_prune_ghost(
        self,
        *,
        relationship_uuid: str,
        scope_key: str,
        prune_receipt_uuid: str,
        reason: str,
        pruned_at: datetime,
    ) -> PruneGhost:
        """Record a restorable tombstone for a pruned relationship.

        Expressed as one upsert with ``RETURNING``, so re-pruning a relationship
        that was previously restored cannot interleave with a concurrent restore
        stamping ``restored_at``, and the created ghost needs no read-back.
        """
        if not prune_receipt_uuid:
            raise ValueError("prune_receipt_uuid must be non-blank")
        with self._engine.transaction():
            relationship = self._memory_graph.get_relationship(relationship_uuid)
            if relationship.properties.get("scope_key") != scope_key:
                raise ValueError("prune ghost scope must match relationship scope")
            row = self._engine.fetchone(
                """
                INSERT INTO memory_prune_ghosts (
                    relationship_uuid, scope_key, prune_receipt_uuid, pruned_at,
                    reason, restorable, restored_at
                ) VALUES (%s, %s, %s, %s, %s, true, NULL)
                ON CONFLICT (relationship_uuid) DO UPDATE SET
                    scope_key = excluded.scope_key,
                    prune_receipt_uuid = excluded.prune_receipt_uuid,
                    pruned_at = excluded.pruned_at,
                    reason = excluded.reason,
                    restorable = true,
                    restored_at = NULL
                RETURNING *
                """,
                (
                    relationship_uuid,
                    scope_key,
                    prune_receipt_uuid,
                    datetime_to_text(pruned_at),
                    reason,
                ),
            )
            if row is None:  # pragma: no cover - an upsert always returns its row
                raise ValueError(f"no prune ghost for relationship {relationship_uuid!r}")
            return self._row_to_prune_ghost(row)

    def prune_ghost(self, relationship_uuid: str) -> PruneGhost:
        row = self._engine.fetchone(
            "SELECT * FROM memory_prune_ghosts WHERE relationship_uuid = %s",
            (relationship_uuid,),
        )
        if row is None:
            raise ValueError(f"no prune ghost for relationship {relationship_uuid!r}")
        return self._row_to_prune_ghost(row)

    def prune_ghosts(self, *, scope_key: str, restorable_only: bool = False) -> list[PruneGhost]:
        """Ghosts for one scope, ordered by ``(pruned_at, relationship_uuid)``.

        Seeks memory_prune_ghosts_scope_idx and maps the rows it already holds
        instead of re-reading each ghost by primary key.
        """
        sql = "SELECT * FROM memory_prune_ghosts WHERE scope_key = %s"
        if restorable_only:
            sql += " AND restorable AND restored_at IS NULL"
        rows = self._engine.fetchall(sql + " ORDER BY pruned_at, relationship_uuid", (scope_key,))
        return [self._row_to_prune_ghost(row) for row in rows]

    def restore_prune_ghost(self, relationship_uuid: str, *, restored_at: datetime) -> PruneGhost:
        """Reactivate the pruned relationship and stamp the ghost.

        The restorability decision is read from the ghost row and then written
        back to it, so the row is locked ``FOR UPDATE`` for the whole decision:
        two retrieval paths matching the same ghost must not both materialise it.

        The crypto-shredded branch is the one place a write deliberately escapes
        this transaction.  ``client.py`` continues past
        ``ContentKeyUnavailableError`` on the assumption that the ghost has
        already been demoted to non-restorable — that demotion is evidence of
        erasure and must not be rolled back by the exception that reports it — so
        it is applied in its own transaction after this one commits.  (A caller
        that wraps the whole call in an outer transaction and then swallows the
        error would still lose the demotion; that is inherent to a nested scope
        and matches nothing SQLite could offer either.)
        """
        with self._engine.transaction():
            ghost = self._lock_prune_ghost(relationship_uuid)
            if not ghost.restorable:
                raise ValueError(f"prune ghost {relationship_uuid!r} is not restorable")
            relationship = self._memory_graph.get_relationship(relationship_uuid)
            fact = relationship.properties.get("fact")
            shredded = is_sealed_content(fact) and self.get_governance_key(ghost.scope.key) is None
            if not shredded:
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
                row = self._engine.fetchone(
                    """
                    UPDATE memory_prune_ghosts SET restored_at = %s
                    WHERE relationship_uuid = %s
                    RETURNING *
                    """,
                    (datetime_to_text(restored_at), relationship_uuid),
                )
                if row is None:  # pragma: no cover - the row is locked above
                    raise ValueError(f"no prune ghost for relationship {relationship_uuid!r}")
                return self._row_to_prune_ghost(row)
        self._engine.execute(
            "UPDATE memory_prune_ghosts SET restorable = false WHERE relationship_uuid = %s",
            (relationship_uuid,),
        )
        raise ContentKeyUnavailableError(f"prune ghost {relationship_uuid!r} is crypto-shredded and cannot be restored")

    def _lock_prune_ghost(self, relationship_uuid: str) -> PruneGhost:
        row = self._engine.fetchone(
            "SELECT * FROM memory_prune_ghosts WHERE relationship_uuid = %s FOR UPDATE",
            (relationship_uuid,),
        )
        if row is None:
            raise ValueError(f"no prune ghost for relationship {relationship_uuid!r}")
        return self._row_to_prune_ghost(row)

    # ------------------------------------------------------------------ row mapping

    def _row_to_prune_ghost(self, row: dict[str, Any]) -> PruneGhost:
        scope_key = str(row["scope_key"])
        if ":" not in scope_key:
            raise ValueError(f"invalid ghost scope key {scope_key!r}")
        kind, scope_id = scope_key.split(":", 1)
        return PruneGhost(
            relationship_uuid=str(row["relationship_uuid"]),
            scope=MemoryScope(kind=kind, scope_id=scope_id),
            prune_receipt_uuid=str(row["prune_receipt_uuid"]),
            pruned_at=datetime.fromisoformat(str(row["pruned_at"])),
            reason=str(row["reason"]),
            restorable=bool(row["restorable"]),
            restored_at=optional_datetime_from_text(row["restored_at"]),
        )

    @staticmethod
    def _split_scope_key(scope_key: str) -> tuple[str, str] | None:
        """Split ``kind:scope_id``, or None when the key cannot name a scope.

        SQLite compared ``record.scope.key == scope_key`` in Python, so a key with
        no separator simply matched nothing.  Returning None preserves that rather
        than sending a half-formed predicate to the server.
        """
        kind, separator, scope_id = scope_key.partition(":")
        if not separator:
            return None
        return kind, scope_id
