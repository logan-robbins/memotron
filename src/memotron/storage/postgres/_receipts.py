"""WS-11 receipt ledger on Postgres: the hash chain under concurrent writers.

What changed versus SQLite, and why
-----------------------------------
1. **The chain is derived from the database, under a per-run lock.**  This is the
   whole point of this file.  The SQLite ledger takes the next ``event_index``
   and the ``previous_receipt_hash`` straight off the in-memory
   :class:`~memotron.storage.receipts.ReceiptRun` cursor, which is sound only
   because one process holds one connection to one file and the run handle is
   the only writer.  Once two replicas share the Operational Store that
   assumption is gone: two ``emit`` calls on the same run would read the same
   predecessor, compute the same ``event_index``, chain from the same hash, and
   one of them would lose the unique index — or, worse, both would succeed with
   a forked chain if the constraint were ever relaxed.  So :meth:`emit` opens a
   transaction, takes ``pg_advisory_xact_lock(hashtextextended(run_uuid, 0))``
   before reading anything, and derives the index and the predecessor hash from
   ``SELECT ... ORDER BY event_index DESC LIMIT 1 FOR UPDATE``.  The in-memory
   cursor is still updated (existing callers read it, and :meth:`checkpoint`
   cross-checks it), but it is now a mirror of the database, not the source of
   truth.  :meth:`checkpoint` takes the same lock, for the same reason: a Merkle
   root computed over a set that another writer is appending to is not a
   commitment to anything.

2. **Hashing is imported, never reimplemented.**  Canonicalization,
   ``payload_digest``, ``receipt_hash``, ``merkle_root``, the field tuples, the
   Pydantic models and ``_row_canonical_payload`` all come from
   :mod:`memotron.storage.receipts` unchanged.  A byte-different digest here
   would invalidate every receipt chain ever written, so there is exactly one
   implementation of each and this module calls it.

3. **Ordering is byte ordering.**  Where the SQLite reads order by a text column
   the query adds ``COLLATE "C"``, and the supporting indexes are declared the
   same way, so a page comes back in the same order on both engines regardless
   of the database's ``lc_collate``.

4. **Filters are pushed into SQL.**  ``receipts_for_scope`` and
   ``negative_space`` are indexed queries (migration 4), not a full-table read
   post-filtered in Python.

5. **Uniqueness violations arrive as** ``psycopg.errors.UniqueViolation`` rather
   than ``sqlite3.IntegrityError``; both are translated into the same
   ``RuntimeError`` messages the SQLite ledger raises.  psycopg is imported
   lazily inside the methods that need it, matching ``_engine.py``.

The class subclasses :class:`~memotron.storage.receipts.ReceiptLedger` and
overrides every method on it, including the two private helpers, so no
sqlite3-bound code path remains reachable.  The base class is kept only because
``memotron.replay._client_ledger`` locates a ledger with ``isinstance``, and
Motive certification would not find this one otherwise.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from memotron.storage.postgres._engine import PostgresEngine
from memotron.storage.receipts import (
    _ALL_FIELDS,
    _EMIT_FIELDS,
    _FLOAT_FIELDS,
    _RUN_DEFAULT_FIELDS,
    _STRING_EMIT_FIELDS,
    DECISION_RESULTS,
    GENESIS_RECEIPT_HASH,
    NON_MATERIALIZING_RESULTS,
    RECEIPT_SCHEMA_VERSION,
    REPLAY_STATUSES,
    RUN_KINDS,
    ChainVerification,
    MemoryReceipt,
    NegativeSpaceEntry,
    ReceiptDecisionType,
    ReceiptLedger,
    ReceiptRun,
    RunCheckpoint,
    _canonical_datetime_text,
    canonicalize_payload,
    merkle_root,
    receipt_hash,
)
from memotron.storage.receipts import (
    _row_canonical_payload as _canonical_payload_from_row,
)

# ---------------------------------------------------------------------------
# Column lists.  Derived from the model's field tuple rather than written out,
# so a new receipt field cannot be persisted on one engine and dropped on the
# other.  Migration 4 declares exactly these columns.
# ---------------------------------------------------------------------------

_RECEIPT_COLUMNS: str = ", ".join(_ALL_FIELDS)
_RECEIPT_PLACEHOLDERS: str = ", ".join("%s" for _ in _ALL_FIELDS)
_RECEIPT_SELECT: str = f"SELECT {_RECEIPT_COLUMNS} FROM memory_receipts"

_CHECKPOINT_FIELDS: tuple[str, ...] = tuple(RunCheckpoint.model_fields)
_CHECKPOINT_COLUMNS: str = ", ".join(_CHECKPOINT_FIELDS)
_CHECKPOINT_PLACEHOLDERS: str = ", ".join("%s" for _ in _CHECKPOINT_FIELDS)
_CHECKPOINT_SELECT: str = f"SELECT {_CHECKPOINT_COLUMNS} FROM run_checkpoints"

# The cross-run read order used by ``receipts_for_scope`` and ``negative_space``.
# ``COLLATE "C"`` is byte order, which is what SQLite's BINARY collation gives;
# without it the page order would depend on the database's lc_collate and the two
# engines could disagree on ties.  ``memory_receipts_scope_order_idx`` and
# ``memory_receipts_negative_space_idx`` are declared with the same collation so
# these stay index scans.
_CHAIN_ORDER: str = 'ORDER BY created_at COLLATE "C", run_uuid COLLATE "C", event_index'


def _row_canonical_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Rebuild the canonical payload dict from a persisted Postgres row.

    Delegates to the SQLite ledger's implementation: a psycopg ``dict`` row
    supports the same ``row[field]`` access ``sqlite3.Row`` does, and the one
    representation difference — Postgres returns a real ``bool`` for
    ``sensitive_payload_encrypted`` where SQLite returns 0/1 — is already
    normalised there by ``bool(value)``.  Forking this function is how a chain
    silently stops verifying, so it is deliberately not forked.
    """
    return _canonical_payload_from_row(row)  # type: ignore[arg-type]


class PostgresReceiptLedger(ReceiptLedger):
    """Append-only receipt + checkpoint ledger on the Operational Store.

    :meth:`emit` remains the AUTHORITATIVE choke point — every receipt in the
    system goes through it, which is what makes the chain ([0023]) and the "no
    candidate silently dropped" property ([0024]) enforceable — but it is now
    also the serialization point for the run, so the property survives more than
    one writer.

    The ledger shares the backend's :class:`PostgresEngine`, so a receipt and the
    graph writes it brackets can commit in one transaction.
    """

    def __init__(self, engine: PostgresEngine) -> None:
        if engine is None:
            raise ValueError("PostgresReceiptLedger requires a PostgresEngine")
        self._engine = engine

    # ------------------------------------------------------------------ schema

    def _migrate(self) -> None:
        """No-op: the ledger's DDL is migration 4 in ``_migrations.MIGRATIONS``.

        Overridden so the inherited SQLite ``executescript``/``PRAGMA
        table_info`` probe can never run against this instance.  Postgres
        declares the full final column set up front; there is no additive
        ``ALTER TABLE ADD COLUMN`` path.
        """

    def _rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Engine-backed replacement for the SQLite per-cursor row helper."""
        return self._engine.fetchall(sql, parameters)

    # ------------------------------------------------------------------ per-run serialization

    def _lock_run(self, run_uuid: str) -> None:
        """Serialize every chain-mutating operation on one run.

        The lock is transaction-scoped, so it is released by the enclosing
        ``transaction()``'s COMMIT/ROLLBACK and cannot be leaked by a crashed
        writer.  Keying it on the run uuid means unrelated runs stay fully
        parallel; ``hashtextextended`` may collide two run uuids onto one lock
        id, which costs a little concurrency and nothing in correctness.

        Callers must already be inside ``self._engine.transaction()``.  When an
        outer transaction is open (a receipt committed together with the graph
        writes it brackets) this joins it and the lock is simply held until that
        outer commit.
        """
        self._engine.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (run_uuid,))

    def _chain_tip(self, run_uuid: str) -> tuple[int, str]:
        """Read the next ``event_index`` and the predecessor hash FROM THE DATABASE.

        ``FOR UPDATE`` on the tail row is belt-and-braces behind the advisory
        lock: the receipts table is append-only, so the row lock alone would not
        stop a second writer from appending, but taking it makes the intent
        explicit and blocks any future in-place edit of the tail.  Returns the
        genesis sentinel for an empty run ([0023]).
        """
        row = self._engine.fetchone(
            """
            SELECT event_index, receipt_hash
            FROM memory_receipts
            WHERE run_uuid = %s
            ORDER BY event_index DESC
            LIMIT 1
            FOR UPDATE
            """,
            (run_uuid,),
        )
        if row is None:
            return 0, GENESIS_RECEIPT_HASH
        return int(row["event_index"]) + 1, str(row["receipt_hash"])

    # ------------------------------------------------------------------ run lifecycle

    def begin_run(
        self,
        *,
        run_kind: str,
        job_name: str,
        scope_key: str,
        effective_policy_digest: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        motive_version_digest: str | None = None,
        graph_state_hash_before: str | None = None,
    ) -> ReceiptRun:
        """Begin a receipted run and return the chain-cursor handle.

        Nothing is persisted here; the checkpoint row is written by
        :meth:`checkpoint` at the end of the run ([0022]).  Validation is
        identical to the SQLite ledger's, message for message.
        """
        if run_kind not in RUN_KINDS:
            raise ValueError(f"run_kind {run_kind!r} is not one of {sorted(RUN_KINDS)}")
        if not isinstance(job_name, str) or not job_name.strip():
            raise ValueError("job_name must be a non-blank string")
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-blank string")
        if not isinstance(effective_policy_digest, str) or not effective_policy_digest.strip():
            raise ValueError("effective_policy_digest must be a non-blank string")
        if motive_version_digest is not None and (
            not isinstance(motive_version_digest, str) or not motive_version_digest.strip()
        ):
            raise ValueError("motive_version_digest must be None or a non-blank string")
        return ReceiptRun(
            run_uuid=uuid4().hex,
            run_kind=run_kind,
            job_name=job_name,
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope_key=scope_key,
            motive_version_digest=motive_version_digest,
            effective_policy_digest=effective_policy_digest,
            graph_state_hash_before=graph_state_hash_before,
        )

    def emit(self, run: ReceiptRun, **receipt_fields: Any) -> MemoryReceipt:
        """Build, chain, and persist one receipt; bump the run's chain cursor.

        Requires ``decision_type``, ``decision_reason``, and ``decision_result``.
        ``tenant_id``/``agent_id``/``scope_key``/``motive_version_digest``/
        ``effective_policy_digest`` default from the run and may be overridden
        per receipt.  Unknown fields are a hard error.

        Unlike the SQLite ledger, the ``event_index`` and ``previous_receipt_hash``
        that go into the hash are read from the database under a per-run advisory
        lock, so two replicas emitting on the same run append 0,1 rather than
        both trying to be 0.  See the module docstring.
        """
        # Lazily imported, matching how ``_engine`` defers its psycopg imports:
        # nothing in this package pulls the driver in at module import time.
        from psycopg.errors import UniqueViolation

        if not isinstance(run, ReceiptRun):
            raise ValueError("emit requires the ReceiptRun handle returned by begin_run")
        unknown = sorted(set(receipt_fields) - _EMIT_FIELDS)
        if unknown:
            raise ValueError(f"unknown receipt fields: {unknown}")
        for required in ("decision_type", "decision_reason", "decision_result"):
            if required not in receipt_fields:
                raise ValueError(f"emit requires {required}")

        values: dict[str, Any] = dict.fromkeys(_EMIT_FIELDS)
        for field in _RUN_DEFAULT_FIELDS:
            values[field] = getattr(run, field)
        values["sensitive_payload_encrypted"] = False
        values.update(receipt_fields)

        values["decision_type"] = ReceiptDecisionType(values["decision_type"])
        if values["decision_result"] not in DECISION_RESULTS:
            raise ValueError(f"decision_result {values['decision_result']!r} is not one of {sorted(DECISION_RESULTS)}")
        if not isinstance(values["decision_reason"], str) or not values["decision_reason"].strip():
            raise ValueError("decision_reason must be a non-blank string")
        created_at = values["created_at"] if values["created_at"] is not None else datetime.now(UTC)
        if not isinstance(created_at, datetime):
            raise ValueError("created_at must be a datetime")
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (naive datetimes are a hard error)")
        values["created_at"] = created_at
        for field in _FLOAT_FIELDS:
            if values[field] is not None:
                try:
                    values[field] = float(values[field])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field} must be a float or None") from exc
        if not isinstance(values["sensitive_payload_encrypted"], bool):
            raise ValueError("sensitive_payload_encrypted must be a bool")
        for field in sorted(_STRING_EMIT_FIELDS):
            value = values[field]
            if field == "decision_result" or value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None, got {type(value).__name__}")
        if not values["scope_key"] or not values["scope_key"].strip():
            raise ValueError("scope_key must be a non-blank string")
        if not values["effective_policy_digest"] or not values["effective_policy_digest"].strip():
            raise ValueError("effective_policy_digest must be a non-blank string")

        with self._engine.transaction():
            # Everything from here to COMMIT is serialized per run: read the tip,
            # hash against it, insert, publish the cursor.  Taking the lock BEFORE
            # the read is what makes the read-then-write safe; a bare SELECT
            # followed by an INSERT is a forked chain under two replicas.
            self._lock_run(run.run_uuid)
            event_index, previous_receipt_hash = self._chain_tip(run.run_uuid)

            payload: dict[str, Any] = {
                "receipt_uuid": uuid4().hex,
                "schema_version": RECEIPT_SCHEMA_VERSION,
                "run_uuid": run.run_uuid,
                "run_kind": run.run_kind,
                "event_index": event_index,
                **values,
            }
            canonical_bytes = canonicalize_payload(payload)
            digest = hashlib.sha256(canonical_bytes).hexdigest()
            chained_hash = receipt_hash(
                schema_version=RECEIPT_SCHEMA_VERSION,
                previous_receipt_hash=previous_receipt_hash,
                payload_digest=digest,
                run_uuid=run.run_uuid,
                event_index=event_index,
            )
            receipt = MemoryReceipt(
                **payload,
                canonical_payload=canonical_bytes.decode("utf-8"),
                payload_digest=digest,
                previous_receipt_hash=previous_receipt_hash,
                receipt_hash=chained_hash,
            )

            row_values: list[Any] = []
            for field in _ALL_FIELDS:
                value = getattr(receipt, field)
                if isinstance(value, datetime):
                    # Timestamps persist as their canonical text so they round-trip
                    # byte-identically into the rebuilt canonical payload.
                    value = _canonical_datetime_text(value)
                elif isinstance(value, ReceiptDecisionType):
                    value = value.value
                # Booleans are bound as real booleans: the column is ``boolean``,
                # not SQLite's 0/1 integer.
                row_values.append(value)
            try:
                self._engine.execute(
                    f"INSERT INTO memory_receipts ({_RECEIPT_COLUMNS}) VALUES ({_RECEIPT_PLACEHOLDERS})",
                    row_values,
                )
            except UniqueViolation as exc:
                # Reachable only if a writer bypassed the advisory lock (or a
                # receipt_uuid collided).  Same failure, same message as SQLite.
                raise RuntimeError(
                    f"receipt event_index collision for run {run.run_uuid}: "
                    "a run must be driven by exactly one ReceiptRun handle"
                ) from exc

            # Publish the cursor while the lock is still held, and by assignment
            # from the database-derived values rather than ``+= 1``: a run handle
            # shared by several threads must not lose an increment to an
            # interleaved read-modify-write.
            run.previous_receipt_hash = chained_hash
            run.next_event_index = event_index + 1
        return receipt

    def checkpoint(
        self,
        run: ReceiptRun,
        *,
        graph_state_hash_after: str | None = None,
        redream_tier: str | None = None,
        redream_tier_reason: str | None = None,
    ) -> RunCheckpoint:
        """Commit the run's Merkle root and chain endpoints ([0022]).

        Empty runs are a hard error: a run with zero receipts must not
        checkpoint.  Receipt hashes are read back from the persisted rows (the
        single source of truth), not from in-memory state — and the read happens
        under the same per-run lock :meth:`emit` takes, inside the transaction
        that writes the checkpoint, so the Merkle root commits to a set that
        cannot grow underneath it.
        """
        from psycopg.errors import UniqueViolation

        if not isinstance(run, ReceiptRun):
            raise ValueError("checkpoint requires the ReceiptRun handle returned by begin_run")

        with self._engine.transaction():
            self._lock_run(run.run_uuid)
            rows = self._engine.fetchall(
                "SELECT receipt_hash FROM memory_receipts WHERE run_uuid = %s ORDER BY event_index",
                (run.run_uuid,),
            )
            if not rows:
                raise RuntimeError(f"cannot checkpoint run {run.run_uuid}: the run emitted zero receipts")
            if len(rows) != run.next_event_index:
                raise RuntimeError(
                    f"cannot checkpoint run {run.run_uuid}: persisted receipt count {len(rows)} "
                    f"does not match the run handle's event count {run.next_event_index}"
                )
            hashes = [str(row["receipt_hash"]) for row in rows]
            checkpoint = RunCheckpoint(
                run_uuid=run.run_uuid,
                run_kind=run.run_kind,
                job_name=run.job_name,
                tenant_id=run.tenant_id,
                agent_id=run.agent_id,
                scope_key=run.scope_key,
                motive_version_digest=run.motive_version_digest,
                effective_policy_digest=run.effective_policy_digest,
                first_receipt_hash=hashes[0],
                last_receipt_hash=hashes[-1],
                receipt_count=len(hashes),
                merkle_root=merkle_root(hashes),
                graph_state_hash_before=run.graph_state_hash_before,
                graph_state_hash_after=graph_state_hash_after,
                redream_tier=redream_tier,
                redream_tier_reason=redream_tier_reason,
                replay_status="unverified",
                created_at=datetime.now(UTC),
            )
            row_values = [
                _canonical_datetime_text(value) if isinstance(value, datetime) else value
                for value in (getattr(checkpoint, field) for field in _CHECKPOINT_FIELDS)
            ]
            try:
                self._engine.execute(
                    f"INSERT INTO run_checkpoints ({_CHECKPOINT_COLUMNS}) VALUES ({_CHECKPOINT_PLACEHOLDERS})",
                    row_values,
                )
            except UniqueViolation as exc:
                raise RuntimeError(f"checkpoint already exists for run {run.run_uuid}") from exc
        return checkpoint

    # ------------------------------------------------------------------ reads

    def receipts_for_run(self, run_uuid: str) -> list[MemoryReceipt]:
        rows = self._engine.fetchall(
            f"{_RECEIPT_SELECT} WHERE run_uuid = %s ORDER BY event_index",
            (run_uuid,),
        )
        return [self._row_to_receipt(row) for row in rows]

    def receipts_for_scope(self, scope_key: str) -> list[MemoryReceipt]:
        """WS-12: every receipt recorded for a scope, in stable chain order
        (erasure-certificate sweep + chain/replay enumeration).

        Indexed by ``memory_receipts_scope_order_idx``, whose trailing columns are
        this ORDER BY: an equality seek on the scope, never a full-table read
        filtered in Python.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("receipts_for_scope requires a non-blank scope_key")
        rows = self._engine.fetchall(
            f"{_RECEIPT_SELECT} WHERE scope_key = %s {_CHAIN_ORDER}",
            (scope_key,),
        )
        return [self._row_to_receipt(row) for row in rows]

    def latest_formation_receipt_digest(self, relationship_uuid: str) -> str | None:
        """Postgres twin of the SQLite ledger's method (#164).

        The base implementation was INHERITED here and carried SQLite SQL into a psycopg
        cursor. Two engine-isms in one statement, and the second is the one that raised:

            ?                      psycopg needs %s
            LIKE 'formation_%'     the bare % is parsed as a placeholder; it needs %%

        Observed before this override existed, on an empty ledger -- the failure is at SQL
        parse time, before any row is read::

            ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%''

        The `%%` is load-bearing in the other direction too: a literal `%%` reaching SQL
        would match nothing, and this method would silently always return None -- which
        reads exactly like "no formation receipt" and would never be noticed.
        `tests/test_receipt_digest_parity.py` pins both directions on both engines.

        Seeks `memory_receipts_relationship_uuid_idx`, which migration 10 adds -- the
        SQLite-only index the base docstring already claimed. Without it this is a
        sequential scan of an append-only ledger.
        """
        if not isinstance(relationship_uuid, str) or not relationship_uuid.strip():
            raise ValueError("latest_formation_receipt_digest requires a non-blank relationship_uuid")
        rows = self._rows(
            "SELECT payload_digest FROM memory_receipts "
            "WHERE relationship_uuid = %s AND decision_type LIKE 'formation_%%' "
            "ORDER BY created_at DESC, run_uuid DESC, event_index DESC LIMIT 1",
            (relationship_uuid.strip(),),
        )
        return str(rows[0]["payload_digest"]) if rows else None

    def checkpoint_for_run(self, run_uuid: str) -> RunCheckpoint:
        row = self._engine.fetchone(f"{_CHECKPOINT_SELECT} WHERE run_uuid = %s", (run_uuid,))
        if row is None:
            raise ValueError(f"no checkpoint recorded for run {run_uuid}")
        return RunCheckpoint.model_validate(dict(row))

    def run_checkpoints(self, *, scope_key: str | None = None, limit: int = 20) -> list[RunCheckpoint]:
        """Per-run Merkle checkpoints, newest first ([0022])."""
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if scope_key is not None:
            rows = self._engine.fetchall(
                f"{_CHECKPOINT_SELECT} WHERE scope_key = %s ORDER BY created_at DESC, run_uuid DESC LIMIT %s",
                (scope_key, limit),
            )
        else:
            rows = self._engine.fetchall(
                f"{_CHECKPOINT_SELECT} ORDER BY created_at DESC, run_uuid DESC LIMIT %s",
                (limit,),
            )
        return [RunCheckpoint.model_validate(dict(row)) for row in rows]

    def set_replay_status(self, run_uuid: str, status: str) -> None:
        """Record byte-replay outcome on the checkpoint ([0025] steps 470/480)."""
        if status not in REPLAY_STATUSES:
            raise ValueError(f"replay status {status!r} is not one of {sorted(REPLAY_STATUSES)}")
        updated = self._engine.execute(
            "UPDATE run_checkpoints SET replay_status = %s WHERE run_uuid = %s",
            (status, run_uuid),
        )
        if updated == 0:
            raise ValueError(f"no checkpoint recorded for run {run_uuid}")

    # ------------------------------------------------------------------ row mapping

    @staticmethod
    def _row_to_receipt(row: dict[str, Any]) -> MemoryReceipt:
        """Map a persisted row onto the model.

        Every column round-trips by name, including the nullable ones replay
        reads (``graph_state_hash_before``/``_after``, ``sensitive_payload``,
        ``sensitive_payload_encrypted``, ``event_payload``,
        ``event_payload_digest``, ``use_event_id``, ``outcome_event_id`` and the
        formation-contract fields).  ``created_at`` is ISO text and is parsed by
        the model exactly as on SQLite; ``sensitive_payload_encrypted`` arrives as
        a real ``bool`` instead of 0/1, which the model accepts unchanged.
        """
        return MemoryReceipt.model_validate(dict(row))

    # ------------------------------------------------------------------ verification ([0023], [0025] step 410)

    def verify_chain(self, run_uuid: str) -> ChainVerification:
        """Recompute payload digests, chain hashes, and the Merkle root from persisted rows.

        Never raises on mismatch — returns a structured result, and the Merkle
        comparison against the checkpoint is reported the same way.  Each
        canonical payload is REBUILT from the row's typed columns, so tampering
        with any single column (not just the stored canonical text) is detected.
        """
        rows = self._engine.fetchall(
            f"{_RECEIPT_SELECT} WHERE run_uuid = %s ORDER BY event_index",
            (run_uuid,),
        )
        checkpoint_row = self._engine.fetchone(f"{_CHECKPOINT_SELECT} WHERE run_uuid = %s", (run_uuid,))
        checkpoint_root = str(checkpoint_row["merkle_root"]) if checkpoint_row is not None else None

        if not rows:
            return ChainVerification(
                run_uuid=run_uuid,
                valid=False,
                receipt_count=0,
                checkpoint_merkle_root=checkpoint_root,
                errors=(f"no receipts recorded for run {run_uuid}",),
            )

        errors: list[str] = []
        first_divergent: int | None = None
        expected: str | None = None
        actual: str | None = None
        previous = GENESIS_RECEIPT_HASH
        computed_hashes: list[str] = []

        for position, row in enumerate(rows):
            stored_index = int(row["event_index"])
            if stored_index != position:
                first_divergent, expected, actual = position, str(position), str(stored_index)
                errors.append(
                    f"event_index chain is not dense at position {position}: "
                    f"expected {position}, found {stored_index} (missing or reordered receipt row)"
                )
                break
            if str(row["previous_receipt_hash"]) != previous:
                first_divergent, expected, actual = (
                    position,
                    previous,
                    str(row["previous_receipt_hash"]),
                )
                errors.append(f"previous_receipt_hash does not chain at event_index {position}")
                break
            rebuilt = canonicalize_payload(_row_canonical_payload(row)).decode("utf-8")
            stored_canonical = str(row["canonical_payload"])
            if rebuilt != stored_canonical:
                first_divergent = position
                expected = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
                actual = hashlib.sha256(stored_canonical.encode("utf-8")).hexdigest()
                errors.append(
                    f"canonical payload rebuilt from columns diverges at event_index {position} "
                    "(a persisted receipt field was tampered)"
                )
                break
            digest = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
            if digest != str(row["payload_digest"]):
                first_divergent, expected, actual = position, digest, str(row["payload_digest"])
                errors.append(f"payload_digest mismatch at event_index {position}")
                break
            chained = receipt_hash(
                schema_version=int(row["schema_version"]),
                previous_receipt_hash=previous,
                payload_digest=digest,
                run_uuid=run_uuid,
                event_index=position,
            )
            if chained != str(row["receipt_hash"]):
                first_divergent, expected, actual = position, chained, str(row["receipt_hash"])
                errors.append(f"receipt_hash mismatch at event_index {position}")
                break
            computed_hashes.append(chained)
            previous = chained

        computed_root: str | None = None
        if first_divergent is None:
            computed_root = merkle_root(computed_hashes)
            if checkpoint_row is not None:
                if int(checkpoint_row["receipt_count"]) != len(computed_hashes):
                    errors.append(
                        f"checkpoint receipt_count {int(checkpoint_row['receipt_count'])} does not "
                        f"match persisted receipt count {len(computed_hashes)}"
                    )
                    if expected is None:
                        expected = str(int(checkpoint_row["receipt_count"]))
                        actual = str(len(computed_hashes))
                if str(checkpoint_row["first_receipt_hash"]) != computed_hashes[0]:
                    errors.append("checkpoint first_receipt_hash does not match the chain")
                if str(checkpoint_row["last_receipt_hash"]) != computed_hashes[-1]:
                    errors.append("checkpoint last_receipt_hash does not match the chain")
                if checkpoint_root != computed_root:
                    errors.append("checkpoint merkle_root does not match the recomputed root")
                    if expected is None:
                        expected, actual = computed_root, checkpoint_root

        return ChainVerification(
            run_uuid=run_uuid,
            valid=not errors,
            receipt_count=len(rows),
            computed_merkle_root=computed_root,
            checkpoint_merkle_root=checkpoint_root,
            first_divergent_event_index=first_divergent,
            expected=expected,
            actual=actual,
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------ negative space ([0027])

    def negative_space(
        self,
        *,
        scope_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        episode_uuid: str | None = None,
        motive_name: str | None = None,
        memory_type: str | None = None,
        decision_reason: str | None = None,
        decision_type: ReceiptDecisionType | str | None = None,
    ) -> list[NegativeSpaceEntry]:
        """Query what the system chose NOT to remember ([0027]).

        Selects only receipts whose ``decision_result`` is non-materializing
        (gated/rejected/transformed/reinforced/superseded/demoted/pruned),
        joined to candidate digest, evidence pointer, policy digests, and
        decision reason.  The time window is half-open: ``since <= created_at
        < until``.  Redaction-safe: returns stored (already redacted or
        encrypted) text only.

        Every predicate is a SQL predicate — the result-set gate is an ``= ANY``
        over a bound array, and the window compares the same ISO text SQLite
        compares — so ``memory_receipts_negative_space_idx`` serves the read
        instead of Python filtering a whole-table scan.
        """
        for label, value in (
            ("scope_key", scope_key),
            ("episode_uuid", episode_uuid),
            ("motive_name", motive_name),
            ("memory_type", memory_type),
            ("decision_reason", decision_reason),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{label} filter must be None or a non-blank string")

        filters = ["decision_result = ANY(%s)"]
        parameters: list[Any] = [sorted(NON_MATERIALIZING_RESULTS)]
        if scope_key is not None:
            filters.append("scope_key = %s")
            parameters.append(scope_key)
        if since is not None:
            filters.append('created_at COLLATE "C" >= %s')
            parameters.append(_canonical_datetime_text(since))
        if until is not None:
            filters.append('created_at COLLATE "C" < %s')
            parameters.append(_canonical_datetime_text(until))
        if episode_uuid is not None:
            filters.append("episode_uuid = %s")
            parameters.append(episode_uuid)
        if motive_name is not None:
            filters.append("motive_name = %s")
            parameters.append(motive_name)
        if memory_type is not None:
            filters.append("memory_type = %s")
            parameters.append(memory_type)
        if decision_reason is not None:
            filters.append("decision_reason = %s")
            parameters.append(decision_reason)
        if decision_type is not None:
            filters.append("decision_type = %s")
            parameters.append(ReceiptDecisionType(decision_type).value)

        rows = self._engine.fetchall(
            f"{_RECEIPT_SELECT} WHERE {' AND '.join(filters)} {_CHAIN_ORDER}",
            parameters,
        )
        return [NegativeSpaceEntry.model_validate(dict(row)) for row in rows]


__all__ = ["PostgresReceiptLedger"]
