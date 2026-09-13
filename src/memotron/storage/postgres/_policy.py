"""Policy contracts, shadow stages, and live aliases on Postgres (DW-016).

The gate this plane exists to enforce
------------------------------------
A policy change reaches production only by traversing stage → certify →
non-divergent shadow → activate, and an *uncertified policy must never take
effect*.  :meth:`PolicyPlaneMixin.activate_policy_alias` is where that is
enforced: it proves the candidate contract carries a passed certification and a
completed, non-divergent shadow stage for exactly this ``(scope_key, alias)``,
and only then swaps the alias.

On SQLite the proof and the swap were one operation because ``BEGIN IMMEDIATE``
takes the database-wide write lock, so nothing can run between them.  Postgres
has no such lock and runs many replicas, so the same guarantee has to be built
out of the right row locks — otherwise two concurrent activations interleave as
check(A), check(B), swap(B), swap(A) and the alias ends up on whichever contract
wrote last, with its ``previous_contract_digest`` bookkeeping computed from a
state that no longer exists.  A blocked candidate becoming live that way is
precisely the failure DW-016 forbids.  Hence:

1. **Every alias mutation is one locked transaction.**  ``activate_policy_alias``,
   ``initialize_policy_alias``, and ``rollback_policy_alias`` open a transaction,
   lock the alias through :meth:`PolicyPlaneMixin._lock_alias`, and only then
   validate and write.  The lock is a transaction-scoped advisory lock keyed on
   ``(scope_key, alias)`` *plus* ``SELECT ... FOR UPDATE`` on the row: ``FOR
   UPDATE`` alone locks nothing when the alias does not exist yet, which is
   exactly the create path both activation and initialisation must survive.  The
   swap itself is an ``INSERT ... ON CONFLICT (scope_key, alias) DO UPDATE``, so
   create and replace are the same atomic statement.

2. **Completing a shadow stage locks the stage row.**  The status and
   evidence-window checks are read-then-write, so ``complete_policy_shadow_stage``
   reads under ``FOR UPDATE``; two workers cannot both close one running stage
   and disagree about its verdict.

3. **Staging is one idempotent upsert.**  The digest is derived from the payload,
   so re-staging an identical contract is normal and must not error.  The
   ``ON CONFLICT (contract_digest) DO UPDATE`` re-states the *stored* values
   rather than the incoming ones, which keeps the contract immutable while still
   returning the row, and divergence from the stored payload is detected in SQL.

4. **Listing filters and orders in SQL.**  ``policy_shadow_stages`` and the
   latest-shadow lookup in ``activate_policy_alias`` seek
   ``policy_shadow_stages_lookup_idx`` on ``(scope_key, alias, ...)`` instead of
   reading the table and filtering in Python.

``payload``, ``certification``, and ``report`` are real ``jsonb``; they decode to
Python objects on read, which is the shape the SQLite backend produced by parsing
its TEXT columns, so callers see no difference.  ``certification_passed`` is a
real ``boolean`` rather than 0/1, and timestamps remain ISO-8601 text so
ordering and receipts stay byte-comparable across engines.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Any

from psycopg import errors as psycopg_errors

from memotron.storage.postgres._engine import (
    datetime_to_text,
    json_dumps,
    optional_datetime_from_text,
)

# Shadow-stage lifecycle values, spelled once.  These are persisted strings that
# receipts and the admin projection compare against, so they are contract.
_STATUS_RUNNING = "running"
_STATUS_PASSED = "passed"
_STATUS_BLOCKED = "blocked"

# Seed for the advisory lock key ("DWPO").  Arbitrary but fixed: it only has to
# keep alias locks from colliding with the migration lock and any other advisory
# lock the deployment takes.
_ALIAS_LOCK_SEED = 0x44_57_50_4F

# Newest shadow stage for one candidate under one alias.  Seeks
# ``policy_shadow_stages_lookup_idx``; the tie-break on stage_id matches the
# SQLite ordering so the same stage decides the gate on both engines.
_LATEST_SHADOW_FOR_CANDIDATE = """
SELECT * FROM policy_shadow_stages
WHERE scope_key = %s AND alias = %s AND candidate_contract_digest = %s
ORDER BY started_at DESC, stage_id DESC
LIMIT 1
"""


def _as_json(value: Any, label: str) -> Any:
    """jsonb arrives already decoded; tolerate a text value for the same column.

    The ``RuntimeError`` mirrors the SQLite backend, which parsed a TEXT column
    and reported an unparseable one as corruption rather than as a caller error.
    Under ``jsonb`` the column cannot hold invalid JSON, so this only fires on a
    value that reached the row as text.
    """
    if isinstance(value, (str, bytes, bytearray)):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"persisted {label} is invalid JSON") from exc
    return value


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class PolicyPlaneMixin(_Base):
    """Immutable policy contracts, shadow stages, and atomic live aliases."""

    # ------------------------------------------------------------------ contracts

    def stage_policy_contract(
        self,
        *,
        contract_digest: str,
        payload: dict[str, Any],
        certification: dict[str, Any],
        certification_passed: bool,
        staged_at: datetime,
    ) -> dict[str, Any]:
        """Persist one immutable policy contract and its certification evidence."""
        if len(contract_digest) != 64:
            raise ValueError("policy contract digest must be a SHA-256 hex digest")
        canonical_payload = json_dumps(payload, "policy contract payload")
        canonical_certification = json_dumps(certification, "policy certification payload")
        with self._engine.transaction():
            # Re-staging is the normal case, not an error: the digest is derived
            # from the payload, so a retry or a second operator arrives with the
            # identical row.  One upsert replaces SELECT-then-INSERT, which two
            # racing stagers could both pass.  The DO UPDATE deliberately writes
            # the *stored* values back — the contract is immutable, and this
            # branch exists only to take the row lock and return the row.  The
            # returned ``payload`` is therefore the persisted one, so comparing it
            # to the incoming document detects a digest reused for a different
            # payload without a second round trip.
            #
            # The divergence test compares the two documents' ``jsonb`` *text*,
            # not the jsonb values.  ``jsonb`` equality is numeric equality, so
            # ``{"a": 1}`` and ``{"a": 1.0}`` compare equal — while
            # ``payload_digest`` renders them differently and therefore assigns
            # them different contract digests.  A value comparison would let one
            # of them be staged under the other's digest, which is exactly the
            # immutability hole the SQLite backend's canonical-text comparison
            # closes.  Both sides are rendered through ``jsonb`` first, so key
            # order and whitespace stay normalised.
            row = self._engine.fetchone(
                """
                INSERT INTO policy_contract_versions (
                    contract_digest, payload, certification, certification_passed, staged_at
                ) VALUES (%s, %s::jsonb, %s::jsonb, %s, %s)
                ON CONFLICT (contract_digest) DO UPDATE
                SET payload = policy_contract_versions.payload,
                    certification = policy_contract_versions.certification,
                    certification_passed = policy_contract_versions.certification_passed,
                    staged_at = policy_contract_versions.staged_at
                RETURNING *, (payload::text IS DISTINCT FROM (%s::jsonb)::text)
                          AS payload_diverged
                """,
                (
                    contract_digest,
                    canonical_payload,
                    canonical_certification,
                    bool(certification_passed),
                    datetime_to_text(staged_at),
                    canonical_payload,
                ),
            )
            if row is None:  # pragma: no cover - DO UPDATE always returns a row
                raise ValueError(f"policy contract vanished while staging: {contract_digest}")
            if bool(row["payload_diverged"]):
                raise ValueError("policy contract digest already exists with different immutable payload")
            return self._contract_from_row(row)

    def policy_contract(self, *, contract_digest: str) -> dict[str, Any]:
        row = self._engine.fetchone(
            "SELECT * FROM policy_contract_versions WHERE contract_digest = %s",
            (contract_digest,),
        )
        if row is None:
            raise ValueError(f"policy contract does not exist: {contract_digest}")
        return self._contract_from_row(row)

    # ------------------------------------------------------------------ shadow stages

    def begin_policy_shadow_stage(
        self,
        *,
        scope_key: str,
        alias: str,
        active_contract_digest: str,
        candidate_contract_digest: str,
        corpus_digest: str,
        required_episode_count: int,
        allowed_disposition_delta: float,
        started_at: datetime,
    ) -> dict[str, Any]:
        """Open a shadow-evaluation window for a certified candidate contract.

        The evidence window is fixed here, before any comparison runs, so the
        stage cannot later be closed against a window resized to fit the result.
        """
        if required_episode_count < 1:
            raise ValueError("shadow stage requires at least one episode")
        if not 0.0 <= allowed_disposition_delta <= 1.0:
            raise ValueError("allowed shadow disposition delta must be between zero and one")
        with self._engine.transaction():
            # No row lock on the contract: contract rows are immutable and never
            # deleted, so this read cannot be invalidated by a concurrent writer.
            candidate = self.policy_contract(contract_digest=candidate_contract_digest)
            if not candidate["certification_passed"]:
                raise ValueError("cannot shadow an uncertified policy contract")
            if active_contract_digest == candidate_contract_digest:
                raise ValueError("shadow candidate must differ from the active contract")
            stage_id = secrets.token_hex(16)
            try:
                row = self._engine.fetchone(
                    """
                    INSERT INTO policy_shadow_stages (
                        stage_id, scope_key, alias, active_contract_digest,
                        candidate_contract_digest, corpus_digest, required_episode_count,
                        observed_episode_count, allowed_disposition_delta, report,
                        status, started_at, completed_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, 0, %s, NULL, %s, %s, NULL)
                    RETURNING *
                    """,
                    (
                        stage_id,
                        scope_key,
                        alias,
                        active_contract_digest,
                        candidate_contract_digest,
                        corpus_digest,
                        int(required_episode_count),
                        float(allowed_disposition_delta),
                        _STATUS_RUNNING,
                        datetime_to_text(started_at),
                    ),
                )
            except psycopg_errors.ForeignKeyViolation as exc:
                # The candidate was read above, so the foreign key is a backstop
                # rather than the error surface; keep the ValueError the interface
                # contract promises.
                raise ValueError(f"policy contract does not exist: {candidate_contract_digest}") from exc
            if row is None:  # pragma: no cover - a plain INSERT always returns its row
                raise ValueError(f"policy shadow stage vanished while opening: {stage_id!r}")
            return self._shadow_from_row(row)

    def complete_policy_shadow_stage(
        self,
        *,
        stage_id: str,
        observed_episode_count: int,
        report: dict[str, Any],
        passed: bool,
        completed_at: datetime,
    ) -> dict[str, Any]:
        """Close a running shadow stage with its divergence report."""
        if observed_episode_count < 0:
            raise ValueError("observed shadow episode count cannot be negative")
        with self._engine.transaction():
            # Read under the row lock: the status and evidence-window checks below
            # decide whether this write is allowed at all, so a second worker must
            # not be able to close the same stage in between and leave two
            # verdicts racing for the last write.
            stage = self._lock_shadow_stage(stage_id)
            if stage["status"] != _STATUS_RUNNING:
                raise ValueError(f"shadow stage {stage_id!r} is not running")
            if observed_episode_count < stage["required_episode_count"]:
                raise ValueError("shadow stage cannot complete before its fixed evidence window is observed")
            row = self._engine.fetchone(
                """
                UPDATE policy_shadow_stages
                SET observed_episode_count = %s,
                    report = %s::jsonb,
                    status = %s,
                    completed_at = %s
                WHERE stage_id = %s
                RETURNING *
                """,
                (
                    int(observed_episode_count),
                    json_dumps(report, "shadow-stage report"),
                    _STATUS_PASSED if passed else _STATUS_BLOCKED,
                    datetime_to_text(completed_at),
                    stage_id,
                ),
            )
            if row is None:  # pragma: no cover - the row is held by this transaction
                raise ValueError(f"policy shadow stage does not exist: {stage_id!r}")
            return self._shadow_from_row(row)

    def policy_shadow_stage(self, *, stage_id: str) -> dict[str, Any]:
        row = self._engine.fetchone("SELECT * FROM policy_shadow_stages WHERE stage_id = %s", (stage_id,))
        if row is None:
            raise ValueError(f"policy shadow stage does not exist: {stage_id!r}")
        return self._shadow_from_row(row)

    def policy_shadow_stages(
        self, *, scope_key: str, alias: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Shadow stages for a scope, newest first (seeks the lookup index)."""
        if limit < 1:
            raise ValueError("shadow stage limit must be at least one")
        clauses = ["scope_key = %s"]
        params: list[Any] = [scope_key]
        if alias is not None:
            clauses.append("alias = %s")
            params.append(alias)
        params.append(int(limit))
        rows = self._engine.fetchall(
            f"""
            SELECT * FROM policy_shadow_stages
            WHERE {" AND ".join(clauses)}
            ORDER BY started_at DESC, stage_id DESC
            LIMIT %s
            """,
            params,
        )
        return [self._shadow_from_row(row) for row in rows]

    def _lock_shadow_stage(self, stage_id: str) -> dict[str, Any]:
        """One stage read under ``FOR UPDATE``, for a writer about to close it."""
        row = self._engine.fetchone("SELECT * FROM policy_shadow_stages WHERE stage_id = %s FOR UPDATE", (stage_id,))
        if row is None:
            raise ValueError(f"policy shadow stage does not exist: {stage_id!r}")
        return self._shadow_from_row(row)

    # ------------------------------------------------------------------ live aliases

    def activate_policy_alias(
        self, *, scope_key: str, alias: str, candidate_contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """Atomically move a named live alias to a certified, shadow-clean contract.

        This is the DW-016 enforcement point.  The certification proof, the shadow
        proof, the predecessor bookkeeping, and the swap all happen inside one
        transaction that holds the alias lock from before the first check until
        after the write, so no concurrent activation can invalidate a proof this
        call already made — an uncertified or blocked candidate cannot become live
        by winning a race.
        """
        with self._engine.transaction():
            current = self._lock_alias(scope_key, alias)
            candidate = self._engine.fetchone(
                """
                SELECT certification_passed FROM policy_contract_versions
                WHERE contract_digest = %s
                """,
                (candidate_contract_digest,),
            )
            if candidate is None:
                raise ValueError(f"policy contract does not exist: {candidate_contract_digest}")
            if not bool(candidate["certification_passed"]):
                raise ValueError("activation blocked: policy certification failed")
            shadow = self._engine.fetchone(_LATEST_SHADOW_FOR_CANDIDATE, (scope_key, alias, candidate_contract_digest))
            if shadow is None or str(shadow["status"]) != _STATUS_PASSED:
                raise ValueError("activation blocked: no completed non-divergent shadow stage for candidate")
            # The predecessor is recorded from the locked row, which is what makes
            # rollback exact: it can only name the contract that was actually live
            # when this activation was proved.
            previous_digest = None if current is None else str(current["contract_digest"])
            try:
                row = self._engine.fetchone(
                    """
                    INSERT INTO policy_aliases (
                        scope_key, alias, contract_digest, previous_contract_digest, updated_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (scope_key, alias) DO UPDATE
                    SET contract_digest = excluded.contract_digest,
                        previous_contract_digest = excluded.previous_contract_digest,
                        updated_at = excluded.updated_at
                    RETURNING *
                    """,
                    (
                        scope_key,
                        alias,
                        candidate_contract_digest,
                        previous_digest,
                        datetime_to_text(now),
                    ),
                )
            except psycopg_errors.ForeignKeyViolation as exc:  # pragma: no cover - checked above
                raise ValueError(f"policy contract does not exist: {candidate_contract_digest}") from exc
            if row is None:  # pragma: no cover - DO UPDATE always returns a row
                raise ValueError(f"policy alias vanished while activating: {alias!r}")
            return self._alias_from_row(row)

    def initialize_policy_alias(
        self, *, scope_key: str, alias: str, contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """Register the already-live baseline when policy aliases are introduced.

        This is a one-time migration surface, not candidate activation.  It is
        intentionally allowed only for an absent alias; subsequent changes must
        traverse certification, shadow, and :meth:`activate_policy_alias`.
        """
        with self._engine.transaction():
            # Locked before validating, in the same order the activation path uses,
            # so an initialisation and an activation of one alias serialise instead
            # of both believing the alias is absent.
            existing = self._lock_alias(scope_key, alias)
            contract = self._engine.fetchone(
                """
                SELECT certification_passed FROM policy_contract_versions
                WHERE contract_digest = %s
                """,
                (contract_digest,),
            )
            if contract is None or not bool(contract["certification_passed"]):
                raise ValueError("initial policy alias requires a certified contract")
            if existing is not None:
                raise ValueError("policy alias already exists; use the shadow-gated activation path")
            # ``DO NOTHING`` rather than ``DO UPDATE``: an existing alias is an
            # error here, never something to overwrite, so the conflict has to
            # surface as the refusal above instead of silently replacing a live
            # contract that never passed a shadow stage.
            row = self._engine.fetchone(
                """
                INSERT INTO policy_aliases (
                    scope_key, alias, contract_digest, previous_contract_digest, updated_at
                ) VALUES (%s, %s, %s, NULL, %s)
                ON CONFLICT (scope_key, alias) DO NOTHING
                RETURNING *
                """,
                (scope_key, alias, contract_digest, datetime_to_text(now)),
            )
            if row is None:  # pragma: no cover - the alias lock already excluded this
                raise ValueError("policy alias already exists; use the shadow-gated activation path")
            return self._alias_from_row(row)

    def rollback_policy_alias(self, *, scope_key: str, alias: str, now: datetime) -> dict[str, Any]:
        """Atomically restore the alias predecessor without touching memory rows.

        The digests are swapped rather than cleared, so the rollback is itself
        reversible and an operator can return to the rolled-back-from contract
        without re-running the shadow stage.
        """
        with self._engine.transaction():
            current = self._lock_alias(scope_key, alias)
            if current is None:
                raise ValueError(f"policy alias does not exist: {alias!r}")
            previous = current["previous_contract_digest"]
            if previous is None or not str(previous):
                raise ValueError("policy alias has no prior contract to roll back to")
            row = self._engine.fetchone(
                """
                UPDATE policy_aliases
                SET contract_digest = %s,
                    previous_contract_digest = %s,
                    updated_at = %s
                WHERE scope_key = %s AND alias = %s
                RETURNING *
                """,
                (
                    str(previous),
                    str(current["contract_digest"]),
                    datetime_to_text(now),
                    scope_key,
                    alias,
                ),
            )
            if row is None:  # pragma: no cover - the row is held by this transaction
                raise ValueError(f"policy alias does not exist: {alias!r}")
            return self._alias_from_row(row)

    def policy_alias(self, *, scope_key: str, alias: str) -> dict[str, Any] | None:
        row = self._engine.fetchone(
            "SELECT * FROM policy_aliases WHERE scope_key = %s AND alias = %s",
            (scope_key, alias),
        )
        if row is None:
            return None
        return self._alias_from_row(row)

    def _lock_alias(self, scope_key: str, alias: str) -> dict[str, Any] | None:
        """Serialise every mutation of one alias, then read its current row.

        Two locks, because one is not enough.  ``SELECT ... FOR UPDATE`` covers a
        swap of an existing alias, but it locks nothing when the alias has yet to
        be created — and both activation and initialisation have to be correct on
        that path.  The transaction-scoped advisory lock closes it: it is keyed on
        the alias identity rather than on a row, so it exists before the row does.
        A hash collision between two unrelated aliases costs a little
        serialisation and nothing else.

        Returns the alias row as stored, or None when the alias does not exist.
        Must be called inside a transaction; the advisory lock is released by the
        commit or rollback, never left dangling.
        """
        self._engine.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, %s))",
            (f"policy_alias\x1f{scope_key}\x1f{alias}", _ALIAS_LOCK_SEED),
        )
        return self._engine.fetchone(
            """
            SELECT * FROM policy_aliases
            WHERE scope_key = %s AND alias = %s
            FOR UPDATE
            """,
            (scope_key, alias),
        )

    # ------------------------------------------------------------------ row mapping

    @staticmethod
    def _contract_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "contract_digest": str(row["contract_digest"]),
            "payload": _as_json(row["payload"], "policy contract"),
            "certification": _as_json(row["certification"], "policy contract"),
            "certification_passed": bool(row["certification_passed"]),
            "staged_at": datetime.fromisoformat(str(row["staged_at"])),
        }

    @staticmethod
    def _shadow_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "stage_id": str(row["stage_id"]),
            "scope_key": str(row["scope_key"]),
            "alias": str(row["alias"]),
            "active_contract_digest": str(row["active_contract_digest"]),
            "candidate_contract_digest": str(row["candidate_contract_digest"]),
            "corpus_digest": str(row["corpus_digest"]),
            "required_episode_count": int(row["required_episode_count"]),
            "observed_episode_count": int(row["observed_episode_count"]),
            "allowed_disposition_delta": float(row["allowed_disposition_delta"]),
            "report": _as_json(row["report"], "policy shadow stage report"),
            "status": str(row["status"]),
            "started_at": datetime.fromisoformat(str(row["started_at"])),
            "completed_at": optional_datetime_from_text(row["completed_at"]),
        }

    @staticmethod
    def _alias_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "scope_key": str(row["scope_key"]),
            "alias": str(row["alias"]),
            "contract_digest": str(row["contract_digest"]),
            "previous_contract_digest": (
                str(row["previous_contract_digest"]) if row["previous_contract_digest"] is not None else None
            ),
            "updated_at": datetime.fromisoformat(str(row["updated_at"])),
        }
