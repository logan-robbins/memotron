"""Policy-contract staging and alias rollout, SQLite side.

Deliberately mirrors :mod:`memotron.storage.postgres._policy` method-for-method:
once both backends carry the same concern boundaries, a structural diff between the
two files is itself a parity check. T0-4 -- SQLite stripping MENTIONS where Postgres
did not -- survived a 3,310-line behavioural parity suite, so a divergence you can
see is cheaper than one you have to test for.

The row-mapping helpers travel with the plane that uses them. Postgres names them
_contract_from_row / _shadow_from_row; SQLite calls the same things
_policy_contract_from_row / _policy_shadow_from_row. Postgres additionally has
_lock_alias / _lock_shadow_stage, which have no SQLite counterpart because SQLite
serialises writes rather than taking row locks -- an intended asymmetry, visible in
the diff rather than buried."""

from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class PolicyPlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

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
        canonical_payload = self._json_dumps(payload, "policy contract payload")
        canonical_certification = self._json_dumps(certification, "policy certification payload")
        existing = self._connection.execute(
            "SELECT * FROM policy_contract_versions WHERE contract_digest = ?", (contract_digest,)
        ).fetchone()
        if existing is not None:
            if str(existing["payload_json"]) != canonical_payload:
                raise ValueError("policy contract digest already exists with different immutable payload")
            return self._policy_contract_from_row(existing)
        self._connection.execute(
            """
            INSERT INTO policy_contract_versions (
                contract_digest, payload_json, certification_json, certification_passed, staged_at
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                contract_digest,
                canonical_payload,
                canonical_certification,
                int(certification_passed),
                self._datetime_to_text(staged_at),
            ),
        )
        self._commit()
        row = self._connection.execute(
            "SELECT * FROM policy_contract_versions WHERE contract_digest = ?", (contract_digest,)
        ).fetchone()
        assert row is not None
        return self._policy_contract_from_row(row)

    def policy_contract(self, *, contract_digest: str) -> dict[str, Any]:
        row = self._connection.execute(
            "SELECT * FROM policy_contract_versions WHERE contract_digest = ?", (contract_digest,)
        ).fetchone()
        if row is None:
            raise ValueError(f"policy contract does not exist: {contract_digest}")
        return self._policy_contract_from_row(row)

    def policy_alias(self, *, scope_key: str, alias: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            "SELECT * FROM policy_aliases WHERE scope_key = ? AND alias = ?", (scope_key, alias)
        ).fetchone()
        if row is None:
            return None
        return {
            "scope_key": str(row["scope_key"]),
            "alias": str(row["alias"]),
            "contract_digest": str(row["contract_digest"]),
            "previous_contract_digest": (
                str(row["previous_contract_digest"]) if row["previous_contract_digest"] is not None else None
            ),
            "updated_at": self._datetime_from_text(str(row["updated_at"])),
        }

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
        if required_episode_count < 1:
            raise ValueError("shadow stage requires at least one episode")
        if not 0.0 <= allowed_disposition_delta <= 1.0:
            raise ValueError("allowed shadow disposition delta must be between zero and one")
        candidate = self.policy_contract(contract_digest=candidate_contract_digest)
        if not candidate["certification_passed"]:
            raise ValueError("cannot shadow an uncertified policy contract")
        if active_contract_digest == candidate_contract_digest:
            raise ValueError("shadow candidate must differ from the active contract")
        stage_id = secrets.token_hex(16)
        self._connection.execute(
            """
            INSERT INTO policy_shadow_stages (
                stage_id, scope_key, alias, active_contract_digest, candidate_contract_digest,
                corpus_digest, required_episode_count, observed_episode_count,
                allowed_disposition_delta, report_json, status, started_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, 'running', ?, NULL)
            """,
            (
                stage_id,
                scope_key,
                alias,
                active_contract_digest,
                candidate_contract_digest,
                corpus_digest,
                required_episode_count,
                allowed_disposition_delta,
                self._datetime_to_text(started_at),
            ),
        )
        self._commit()
        return self.policy_shadow_stage(stage_id=stage_id)

    def complete_policy_shadow_stage(
        self,
        *,
        stage_id: str,
        observed_episode_count: int,
        report: dict[str, Any],
        passed: bool,
        completed_at: datetime,
    ) -> dict[str, Any]:
        if observed_episode_count < 0:
            raise ValueError("observed shadow episode count cannot be negative")
        stage = self.policy_shadow_stage(stage_id=stage_id)
        if stage["status"] != "running":
            raise ValueError(f"shadow stage {stage_id!r} is not running")
        if observed_episode_count < stage["required_episode_count"]:
            raise ValueError("shadow stage cannot complete before its fixed evidence window is observed")
        self._connection.execute(
            """
            UPDATE policy_shadow_stages
            SET observed_episode_count = ?, report_json = ?, status = ?, completed_at = ?
            WHERE stage_id = ?
            """,
            (
                observed_episode_count,
                self._json_dumps(report, "shadow-stage report"),
                "passed" if passed else "blocked",
                self._datetime_to_text(completed_at),
                stage_id,
            ),
        )
        self._commit()
        return self.policy_shadow_stage(stage_id=stage_id)

    def policy_shadow_stage(self, *, stage_id: str) -> dict[str, Any]:
        row = self._connection.execute("SELECT * FROM policy_shadow_stages WHERE stage_id = ?", (stage_id,)).fetchone()
        if row is None:
            raise ValueError(f"policy shadow stage does not exist: {stage_id!r}")
        return self._policy_shadow_from_row(row)

    def policy_shadow_stages(
        self, *, scope_key: str, alias: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        if limit < 1:
            raise ValueError("shadow stage limit must be at least one")
        sql = "SELECT * FROM policy_shadow_stages WHERE scope_key = ?"
        params: list[Any] = [scope_key]
        if alias is not None:
            sql += " AND alias = ?"
            params.append(alias)
        sql += " ORDER BY started_at DESC, stage_id DESC LIMIT ?"
        params.append(limit)
        return [self._policy_shadow_from_row(row) for row in self._connection.execute(sql, params).fetchall()]

    def activate_policy_alias(
        self, *, scope_key: str, alias: str, candidate_contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """Atomically move a named live alias to a certified, shadow-clean contract."""
        with self.transaction():
            candidate = self._connection.execute(
                "SELECT certification_passed FROM policy_contract_versions WHERE contract_digest = ?",
                (candidate_contract_digest,),
            ).fetchone()
            if candidate is None:
                raise ValueError(f"policy contract does not exist: {candidate_contract_digest}")
            if not bool(candidate["certification_passed"]):
                raise ValueError("activation blocked: policy certification failed")
            shadow = self._connection.execute(
                """
                SELECT * FROM policy_shadow_stages
                WHERE scope_key = ? AND alias = ? AND candidate_contract_digest = ?
                ORDER BY started_at DESC, stage_id DESC LIMIT 1
                """,
                (scope_key, alias, candidate_contract_digest),
            ).fetchone()
            if shadow is None or str(shadow["status"]) != "passed":
                raise ValueError("activation blocked: no completed non-divergent shadow stage for candidate")
            previous = self._connection.execute(
                "SELECT contract_digest FROM policy_aliases WHERE scope_key = ? AND alias = ?",
                (scope_key, alias),
            ).fetchone()
            previous_digest = str(previous["contract_digest"]) if previous is not None else None
            self._connection.execute(
                """
                INSERT INTO policy_aliases (
                    scope_key, alias, contract_digest, previous_contract_digest, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(scope_key, alias) DO UPDATE SET
                    contract_digest = excluded.contract_digest,
                    previous_contract_digest = excluded.previous_contract_digest,
                    updated_at = excluded.updated_at
                """,
                (
                    scope_key,
                    alias,
                    candidate_contract_digest,
                    previous_digest,
                    self._datetime_to_text(now),
                ),
            )
        alias_state = self.policy_alias(scope_key=scope_key, alias=alias)
        assert alias_state is not None
        return alias_state

    def initialize_policy_alias(
        self, *, scope_key: str, alias: str, contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """Register the already-live baseline when policy aliases are introduced.

        This is a one-time migration surface, not candidate activation.  It is
        intentionally allowed only for an absent alias; subsequent changes must
        traverse certification, shadow, and :meth:`activate_policy_alias`.
        """
        with self.transaction():
            contract = self._connection.execute(
                "SELECT certification_passed FROM policy_contract_versions WHERE contract_digest = ?",
                (contract_digest,),
            ).fetchone()
            if contract is None or not bool(contract["certification_passed"]):
                raise ValueError("initial policy alias requires a certified contract")
            existing = self._connection.execute(
                "SELECT 1 FROM policy_aliases WHERE scope_key = ? AND alias = ?", (scope_key, alias)
            ).fetchone()
            if existing is not None:
                raise ValueError("policy alias already exists; use the shadow-gated activation path")
            self._connection.execute(
                """
                INSERT INTO policy_aliases (
                    scope_key, alias, contract_digest, previous_contract_digest, updated_at
                ) VALUES (?, ?, ?, NULL, ?)
                """,
                (scope_key, alias, contract_digest, self._datetime_to_text(now)),
            )
        alias_state = self.policy_alias(scope_key=scope_key, alias=alias)
        assert alias_state is not None
        return alias_state

    def rollback_policy_alias(self, *, scope_key: str, alias: str, now: datetime) -> dict[str, Any]:
        """Atomically restore the alias predecessor without touching memory rows."""
        with self.transaction():
            current = self._connection.execute(
                "SELECT * FROM policy_aliases WHERE scope_key = ? AND alias = ?", (scope_key, alias)
            ).fetchone()
            if current is None:
                raise ValueError(f"policy alias does not exist: {alias!r}")
            previous = current["previous_contract_digest"]
            if previous is None or not str(previous):
                raise ValueError("policy alias has no prior contract to roll back to")
            self._connection.execute(
                """
                UPDATE policy_aliases
                SET contract_digest = ?, previous_contract_digest = ?, updated_at = ?
                WHERE scope_key = ? AND alias = ?
                """,
                (
                    str(previous),
                    str(current["contract_digest"]),
                    self._datetime_to_text(now),
                    scope_key,
                    alias,
                ),
            )
        alias_state = self.policy_alias(scope_key=scope_key, alias=alias)
        assert alias_state is not None
        return alias_state

    def _policy_contract_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(str(row["payload_json"]))
            certification = json.loads(str(row["certification_json"]))
        except json.JSONDecodeError as exc:
            raise RuntimeError("persisted policy contract is invalid JSON") from exc
        return {
            "contract_digest": str(row["contract_digest"]),
            "payload": payload,
            "certification": certification,
            "certification_passed": bool(row["certification_passed"]),
            "staged_at": self._datetime_from_text(str(row["staged_at"])),
        }

    def _policy_shadow_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        try:
            report = json.loads(str(row["report_json"])) if row["report_json"] is not None else None
        except json.JSONDecodeError as exc:
            raise RuntimeError("persisted policy shadow stage report is invalid JSON") from exc
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
            "report": report,
            "status": str(row["status"]),
            "started_at": self._datetime_from_text(str(row["started_at"])),
            "completed_at": (
                self._datetime_from_text(str(row["completed_at"])) if row["completed_at"] is not None else None
            ),
        }
