"""Canonicalization registries (WS-17), the raw/quarantine store (WS-24), and
the WS-26 derivation-DAG epoch layer, on Postgres.

These were folded into the SQLite backend SQLite-first; this module brings them
to Postgres parity so a re-dream (``memotron.epochs``) can branch, diff,
adopt, and roll back against a Postgres live store, and so the entity/predicate
resolution the retrieval and formation paths lean on works on both engines.

Every method mirrors its ``storage/sqlite.py`` counterpart exactly — same
validation, same return shapes, same ordering — translated to the Postgres
engine idiom (``%s`` binds, ``self._engine`` fetch/execute, re-entrant
``transaction()``).  JSON-bearing columns stay ``text`` (a serialized string) so
the row→dict mappers are byte-identical to the substrate's; the epoch/registry
tables sit outside ``graph_state_hash`` (its own ``registry_state_digest``
covers the registries), so this is bookkeeping parity, not a change to any
receipt-anchored value.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.models import (
    MemoryScope,
    QuarantinedCandidate,
    QuarantineStatus,
)
from memotron.storage._shared import (
    entity_alias_row_to_dict,
    epoch_row_to_dict,
    predicate_alias_row_to_dict,
)
from memotron.storage.base import normalize_key
from memotron.storage.postgres._engine import (
    datetime_from_text,
    datetime_to_text,
    optional_datetime_from_text,
)

# The content-plane row kind in ``governance_keys`` — an empty subject key, the
# same sentinel the SQLite substrate uses (``sqlite.CONTENT_PLANE_SUBJECT_KEY``).
_CONTENT_PLANE_SUBJECT_KEY = ""


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class EpochRegistryPlaneMixin(_Base):
    """Epoch bookkeeping + canonicalization registries + quarantine store."""

    _engine: Any
    _ENTITY_ALIAS_STATUSES = ("active", "proposed", "rejected")

    # ==================================================================
    # WS-23 M1: content-plane protection probe (governance_keys)
    # ==================================================================

    def scope_content_is_protected(self, scope_key: str) -> bool:
        """True once this scope's CONTENT-PLANE row has ever been sealed.

        Matches on the content-plane subject key specifically, not on
        ``scope_key`` alone — a sealed LLM credential lives under the same scope
        key but a different subject and must not announce the tenant scope as
        crypto-shred governed.  Parity with the SQLite substrate.
        """
        row = self._engine.fetchone(
            "SELECT 1 FROM governance_keys WHERE scope_key = %s AND subject_key = %s LIMIT 1",
            (scope_key, _CONTENT_PLANE_SUBJECT_KEY),
        )
        return row is not None

    # ==================================================================
    # WS-26 T2/T6: epoch bookkeeping + HEAD pointer
    # ==================================================================

    def ensure_root_epoch(self, scope_key: str, *, now: datetime) -> str:
        """Lazily create *scope_key*'s root epoch and return HEAD (idempotent)."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("ensure_root_epoch requires a non-blank scope_key")
        existing = self.active_epoch_for_scope(scope_key)
        if existing is not None:
            return existing
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        now_text = datetime_to_text(now) or now.isoformat()
        with self._engine.transaction():
            self._engine.execute(
                """
                INSERT INTO graph_epochs (
                    epoch_id, scope_key, parent_epoch_id, created_at, label, status
                ) VALUES (%s, %s, NULL, %s, 'root', 'adopted')
                """,
                (epoch_id, scope_key, now_text),
            )
            self._engine.execute(
                "INSERT INTO active_epochs (scope_key, epoch_id, updated_at) VALUES (%s, %s, %s)",
                (scope_key, epoch_id, now_text),
            )
        return epoch_id

    def active_epoch_for_scope(self, scope_key: str) -> str | None:
        """The scope's current HEAD epoch id, or ``None`` if never initialized."""
        row = self._engine.fetchone("SELECT epoch_id FROM active_epochs WHERE scope_key = %s", (scope_key,))
        return str(row["epoch_id"]) if row is not None else None

    def create_epoch(
        self,
        *,
        scope_key: str,
        parent_epoch_id: str,
        now: datetime,
        label: str = "",
        status: str = "open",
        tier: str | None = None,
        overrides: dict[str, Any] | None = None,
        shadow_store_path: str | None = None,
    ) -> str:
        """Fork a new (initially non-HEAD) epoch under *parent_epoch_id*."""
        if not self._epoch_exists(parent_epoch_id):
            raise ValueError(f"parent epoch does not exist: {parent_epoch_id}")
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        self._engine.execute(
            """
            INSERT INTO graph_epochs (
                epoch_id, scope_key, parent_epoch_id, created_at, label, status,
                tier, overrides_json, shadow_store_path
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                epoch_id,
                scope_key,
                parent_epoch_id,
                datetime_to_text(now) or now.isoformat(),
                label,
                status,
                tier,
                json.dumps(overrides or {}, sort_keys=True),
                shadow_store_path,
            ),
        )
        return epoch_id

    def _epoch_exists(self, epoch_id: str) -> bool:
        return self._engine.fetchone("SELECT 1 FROM graph_epochs WHERE epoch_id = %s", (epoch_id,)) is not None

    def epoch(self, epoch_id: str) -> dict[str, Any]:
        """One epoch's bookkeeping row, or raise ``ValueError``."""
        row = self._engine.fetchone("SELECT * FROM graph_epochs WHERE epoch_id = %s", (epoch_id,))
        if row is None:
            raise ValueError(f"epoch does not exist: {epoch_id}")
        return epoch_row_to_dict(row)

    def epochs_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """Every epoch ever created for *scope_key*, oldest first."""
        rows = self._engine.fetchall(
            'SELECT * FROM graph_epochs WHERE scope_key = %s ORDER BY created_at COLLATE "C", epoch_id COLLATE "C"',
            (scope_key,),
        )
        return [epoch_row_to_dict(row) for row in rows]

    def set_epoch_pre_adopt_snapshot(self, epoch_id: str, snapshot: dict[str, Any]) -> None:
        """Record the exact pre-adopt state an adopt is about to retire."""
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        self._engine.execute(
            "UPDATE graph_epochs SET pre_adopt_snapshot_json = %s WHERE epoch_id = %s",
            (json.dumps(snapshot, sort_keys=True), epoch_id),
        )

    def set_epoch_status(self, epoch_id: str, status: str) -> dict[str, Any]:
        """Bookkeeping-only status transition; never deletes or mutates rows."""
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        self._engine.execute("UPDATE graph_epochs SET status = %s WHERE epoch_id = %s", (status, epoch_id))
        return self.epoch(epoch_id)

    def set_active_epoch(self, scope_key: str, epoch_id: str, *, now: datetime) -> None:
        """The pointer flip — adopt or roll back a scope's HEAD."""
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        now_text = datetime_to_text(now) or now.isoformat()
        self._engine.execute(
            """
            INSERT INTO active_epochs (scope_key, epoch_id, updated_at) VALUES (%s, %s, %s)
            ON CONFLICT (scope_key) DO UPDATE SET epoch_id = excluded.epoch_id,
                updated_at = excluded.updated_at
            """,
            (scope_key, epoch_id, now_text),
        )
        # The scope's ``graph_state_hash`` is epoch-ancestry filtered, so flipping
        # HEAD changes the hash without touching a relationship row.  Invalidate
        # the memo so the next read recomputes under the new ancestry (the SQLite
        # backend recomputes from scratch every call and needs no equivalent).
        self._mark_scope_dirty(scope_key)

    def record_epoch_run(self, epoch_id: str, run_uuid: str, *, now: datetime) -> None:
        """Attribute one dream-job/receipt run to the epoch it wrote into.

        Deliberately does NOT require *epoch_id* to exist in this store's own
        ``graph_epochs`` table — epochs.py records a run against a branch's
        shadow store, where the epoch's bookkeeping row lives only in the main
        store (the same cross-store coordination the SQLite backend documents).
        """
        self._engine.execute(
            "INSERT INTO epoch_runs (epoch_id, run_uuid, recorded_at) VALUES (%s, %s, %s) "
            "ON CONFLICT (epoch_id, run_uuid) DO NOTHING",
            (epoch_id, run_uuid, datetime_to_text(now) or now.isoformat()),
        )

    def epoch_run_uuids(self, epoch_id: str) -> list[str]:
        """Every run that contributed to *epoch_id*, oldest first."""
        rows = self._engine.fetchall(
            "SELECT run_uuid FROM epoch_runs WHERE epoch_id = %s "
            'ORDER BY recorded_at COLLATE "C", run_uuid COLLATE "C"',
            (epoch_id,),
        )
        return [str(row["run_uuid"]) for row in rows]

    def epoch_ancestry(self, epoch_id: str) -> tuple[str, ...]:
        """*epoch_id* plus every ancestor up to the root, self first."""
        chain: list[str] = []
        current: str | None = epoch_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            row = self._engine.fetchone("SELECT parent_epoch_id FROM graph_epochs WHERE epoch_id = %s", (current,))
            current = str(row["parent_epoch_id"]) if row is not None and row["parent_epoch_id"] else None
        return tuple(chain)

    def _active_epoch_ancestry(self, scope_key: str) -> frozenset[str] | None:
        active = self.active_epoch_for_scope(scope_key)
        if active is None:
            return None
        return frozenset(self.epoch_ancestry(active))

    # ==================================================================
    # WS-26 T9: registry-state digest (entity aliases + predicate canonicals)
    # ==================================================================

    def registry_state_digest(self, scope_key: str) -> str:
        """Deterministic digest of a scope's canonicalization-registry state,
        epoch-ancestry filtered exactly like the SQLite substrate.

        The registry half of T6 byte-for-byte rollback verification; deliberately
        NOT folded into ``graph_state_hash`` (registry mutations are not
        receipt-bracketed).  A scope with no registry rows still returns a stable
        digest of the empty structure.
        """
        ancestry = self._active_epoch_ancestry(scope_key)
        entity_rows = self._engine.fetchall(
            "SELECT name_normalized, canonical_name, status, epoch_id FROM entity_canon WHERE scope_key = %s",
            (scope_key,),
        )
        predicate_rows = self._engine.fetchall(
            "SELECT predicate_normalized, canonical_predicate, epoch_id FROM predicate_canon WHERE scope_key = %s",
            (scope_key,),
        )

        def visible(epoch_id: Any) -> bool:
            if ancestry is None or not epoch_id:
                return True
            return epoch_id in ancestry

        entities = sorted(
            [row["name_normalized"], row["canonical_name"], row["status"]]
            for row in entity_rows
            if visible(row["epoch_id"])
        )
        predicates = sorted(
            [row["predicate_normalized"], row["canonical_predicate"]]
            for row in predicate_rows
            if visible(row["epoch_id"])
        )
        canonical = json.dumps(
            {"entities": entities, "predicates": predicates},
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # ==================================================================
    # WS-17 T16: per-scope canonical predicate registry
    # ==================================================================

    def canonical_predicate_for(self, scope_key: str, predicate: str) -> str | None:
        """Registered canonical for one predicate surface, or None when unmapped."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_predicate_for requires a non-blank scope_key")
        row = self._engine.fetchone(
            "SELECT canonical_predicate FROM predicate_canon WHERE scope_key = %s AND predicate_normalized = %s",
            (scope_key, normalize_key(predicate)),
        )
        return str(row["canonical_predicate"]) if row is not None else None

    def canonical_predicates_for_scope(self, scope_key: str) -> list[str]:
        """Distinct canonical predicates for a scope, in registration order."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_predicates_for_scope requires a non-blank scope_key")
        rows = self._engine.fetchall(
            """
            SELECT canonical_predicate,
                   MIN(created_at || '|' || predicate_normalized) AS first_seen
            FROM predicate_canon
            WHERE scope_key = %s
            GROUP BY canonical_predicate
            ORDER BY MIN(created_at || '|' || predicate_normalized) COLLATE "C"
            """,
            (scope_key,),
        )
        return [str(row["canonical_predicate"]) for row in rows]

    def register_predicate(
        self,
        scope_key: str,
        predicate: str,
        canonical: str,
        *,
        decided_by: str,
        embedding_identifier: str | None = None,
        cosine: float | None = None,
    ) -> None:
        """Record one first-wins predicate mapping for a scope (idempotent)."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("register_predicate requires a non-blank scope_key")
        normalized_predicate = normalize_key(predicate)
        normalized_canonical = normalize_key(canonical)
        if not normalized_predicate:
            raise ValueError("register_predicate requires a non-blank predicate")
        if not normalized_canonical:
            raise ValueError("register_predicate requires a non-blank canonical")
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise ValueError("register_predicate requires a non-blank decided_by")
        self._engine.execute(
            """
            INSERT INTO predicate_canon (
                scope_key, predicate_normalized, canonical_predicate, decided_by,
                embedding_identifier, cosine, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope_key, predicate_normalized) DO NOTHING
            """,
            (
                scope_key,
                normalized_predicate,
                normalized_canonical,
                decided_by.strip(),
                embedding_identifier,
                cosine,
                datetime.now(UTC).isoformat(),
            ),
        )

    def predicate_alias_row(self, scope_key: str, predicate: str) -> dict[str, Any] | None:
        """The full ``predicate_canon`` row for one surface, or None."""
        row = self._engine.fetchone(
            "SELECT * FROM predicate_canon WHERE scope_key = %s AND predicate_normalized = %s",
            (scope_key, normalize_key(predicate)),
        )
        return predicate_alias_row_to_dict(row) if row is not None else None

    def predicate_alias_rows_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """Every ``predicate_canon`` row for a scope, registration order."""
        rows = self._engine.fetchall(
            "SELECT * FROM predicate_canon WHERE scope_key = %s "
            'ORDER BY created_at COLLATE "C", predicate_normalized COLLATE "C"',
            (scope_key,),
        )
        return [predicate_alias_row_to_dict(row) for row in rows]

    def overlay_predicate_row(self, scope_key: str, row: dict[str, Any], *, epoch_id: str) -> None:
        """Administrative upsert of a full ``predicate_canon`` row (no first-wins)."""
        predicate_normalized = normalize_key(str(row["predicate_normalized"]))
        self._engine.execute(
            """
            INSERT INTO predicate_canon (
                scope_key, predicate_normalized, canonical_predicate, decided_by,
                embedding_identifier, cosine, created_at, epoch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope_key, predicate_normalized) DO UPDATE SET
                canonical_predicate = excluded.canonical_predicate,
                decided_by = excluded.decided_by,
                embedding_identifier = excluded.embedding_identifier,
                cosine = excluded.cosine,
                epoch_id = excluded.epoch_id
            """,
            (
                scope_key,
                predicate_normalized,
                row["canonical_predicate"],
                row["decided_by"],
                row.get("embedding_identifier"),
                row.get("cosine"),
                row.get("created_at") or datetime.now(UTC).isoformat(),
                epoch_id,
            ),
        )

    def delete_predicate_row(self, scope_key: str, predicate_normalized: str) -> None:
        """The predicate-registry counterpart of :meth:`delete_entity_alias_row`."""
        self._engine.execute(
            "DELETE FROM predicate_canon WHERE scope_key = %s AND predicate_normalized = %s",
            (scope_key, normalize_key(predicate_normalized)),
        )

    # ==================================================================
    # WS-17 T16b: per-scope entity alias registry
    # ==================================================================

    def canonical_entity_name_for(self, scope_key: str, name: str) -> str | None:
        """ACTIVE-alias canonical name for one entity surface, or None when unmapped."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_entity_name_for requires a non-blank scope_key")
        row = self._engine.fetchone(
            "SELECT canonical_name FROM entity_canon "
            "WHERE scope_key = %s AND name_normalized = %s AND status = 'active'",
            (scope_key, normalize_key(name)),
        )
        return str(row["canonical_name"]) if row is not None else None

    def entity_alias_row(self, scope_key: str, name: str) -> dict[str, Any] | None:
        """The full registry row for one surface name regardless of status."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("entity_alias_row requires a non-blank scope_key")
        row = self._engine.fetchone(
            "SELECT * FROM entity_canon WHERE scope_key = %s AND name_normalized = %s",
            (scope_key, normalize_key(name)),
        )
        return entity_alias_row_to_dict(row) if row is not None else None

    def register_entity_alias(
        self,
        scope_key: str,
        name: str,
        canonical: str,
        *,
        status: str,
        decided_by: str,
        link_score: float | None = None,
        link_signals: dict[str, Any] | None = None,
        embedding_identifier: str | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one alias→canonical mapping for a scope (first-wins for ACTIVE)."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("register_entity_alias requires a non-blank scope_key")
        if status not in self._ENTITY_ALIAS_STATUSES:
            raise ValueError(f"register_entity_alias status must be one of {self._ENTITY_ALIAS_STATUSES}")
        if not isinstance(decided_by, str) or not decided_by.strip():
            raise ValueError("register_entity_alias requires a non-blank decided_by")
        normalized_name = normalize_key(name)
        canonical_surface = canonical.strip()
        if not normalized_name:
            raise ValueError("register_entity_alias requires a non-blank name")
        if not canonical_surface:
            raise ValueError("register_entity_alias requires a non-blank canonical")
        if normalized_name == normalize_key(canonical_surface):
            raise ValueError("register_entity_alias cannot map a name to itself")
        anchor = (now or datetime.now(UTC)).isoformat()
        signals_json = json.dumps(link_signals or {}, sort_keys=True, separators=(",", ":"))
        with self._engine.transaction():
            existing = self.entity_alias_row(scope_key, normalized_name)
            if existing is None:
                self._engine.execute(
                    """
                    INSERT INTO entity_canon (
                        scope_key, name_normalized, canonical_name, status, link_score,
                        link_signals, decided_by, embedding_identifier, proposed_at,
                        resolved_at, resolved_by
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        scope_key,
                        normalized_name,
                        canonical_surface,
                        status,
                        link_score,
                        signals_json,
                        decided_by.strip(),
                        embedding_identifier,
                        anchor,
                        anchor if status == "active" else None,
                        decided_by.strip() if status == "active" else None,
                    ),
                )
            elif existing["status"] == "proposed":
                same_canonical = normalize_key(existing["canonical_name"]) == normalize_key(canonical_surface)
                if status == "active" and same_canonical:
                    self._engine.execute(
                        """
                        UPDATE entity_canon
                        SET status = 'active', link_score = %s, link_signals = %s,
                            embedding_identifier = %s, resolved_at = %s, resolved_by = %s
                        WHERE scope_key = %s AND name_normalized = %s
                        """,
                        (
                            link_score,
                            signals_json,
                            embedding_identifier,
                            anchor,
                            decided_by.strip(),
                            scope_key,
                            normalized_name,
                        ),
                    )
                elif status == "proposed" and same_canonical:
                    self._engine.execute(
                        """
                        UPDATE entity_canon
                        SET link_score = %s, link_signals = %s, embedding_identifier = %s
                        WHERE scope_key = %s AND name_normalized = %s
                        """,
                        (link_score, signals_json, embedding_identifier, scope_key, normalized_name),
                    )
            result = self.entity_alias_row(scope_key, normalized_name)
        assert result is not None  # the row exists by construction
        return result

    def entity_alias_rows_for_scope(self, scope_key: str, status: str | None = None) -> list[dict[str, Any]]:
        """Registry rows for a scope in deterministic (proposed_at, name) order."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("entity_alias_rows_for_scope requires a non-blank scope_key")
        if status is not None and status not in self._ENTITY_ALIAS_STATUSES:
            raise ValueError(f"entity_alias_rows_for_scope status must be one of {self._ENTITY_ALIAS_STATUSES}")
        if status is None:
            rows = self._engine.fetchall(
                "SELECT * FROM entity_canon WHERE scope_key = %s "
                'ORDER BY proposed_at COLLATE "C", name_normalized COLLATE "C"',
                (scope_key,),
            )
        else:
            rows = self._engine.fetchall(
                "SELECT * FROM entity_canon WHERE scope_key = %s AND status = %s "
                'ORDER BY proposed_at COLLATE "C", name_normalized COLLATE "C"',
                (scope_key, status),
            )
        return [entity_alias_row_to_dict(row) for row in rows]

    def resolve_entity_alias(
        self,
        scope_key: str,
        name_normalized: str,
        *,
        status: str,
        resolved_by: str,
        resolved_at: datetime,
        link_score: float | None = None,
        link_signals: dict[str, Any] | None = None,
        canonical_name: str | None = None,
    ) -> dict[str, Any]:
        """Transition one registry row's status (approve / reject / demote / re-propose)."""
        if status not in self._ENTITY_ALIAS_STATUSES:
            raise ValueError(f"resolve_entity_alias status must be one of {self._ENTITY_ALIAS_STATUSES}")
        if not isinstance(resolved_by, str) or not resolved_by.strip():
            raise ValueError("resolve_entity_alias requires a non-blank resolved_by")
        with self._engine.transaction():
            existing = self.entity_alias_row(scope_key, name_normalized)
            if existing is None:
                raise ValueError(f"entity alias {name_normalized!r} is not registered in scope {scope_key}")
            effective_score = link_score if link_score is not None else existing["link_score"]
            effective_signals = link_signals if link_signals is not None else existing["link_signals"]
            effective_canonical = (
                canonical_name.strip()
                if isinstance(canonical_name, str) and canonical_name.strip()
                else existing["canonical_name"]
            )
            if normalize_key(name_normalized) == normalize_key(effective_canonical):
                raise ValueError("resolve_entity_alias cannot map a name to itself")
            self._engine.execute(
                """
                UPDATE entity_canon
                SET status = %s, canonical_name = %s, link_score = %s, link_signals = %s,
                    resolved_at = %s, resolved_by = %s
                WHERE scope_key = %s AND name_normalized = %s
                """,
                (
                    status,
                    effective_canonical,
                    effective_score,
                    json.dumps(effective_signals or {}, sort_keys=True, separators=(",", ":")),
                    resolved_at.isoformat(),
                    resolved_by.strip(),
                    scope_key,
                    normalize_key(name_normalized),
                ),
            )
            result = self.entity_alias_row(scope_key, name_normalized)
        assert result is not None
        return result

    def update_entity_alias_evidence(
        self,
        scope_key: str,
        name_normalized: str,
        *,
        link_score: float,
        link_signals: dict[str, Any],
    ) -> None:
        """Mutate one row's living link evidence (score + signals) in place."""
        self._engine.execute(
            """
            UPDATE entity_canon SET link_score = %s, link_signals = %s
            WHERE scope_key = %s AND name_normalized = %s
            """,
            (
                link_score,
                json.dumps(link_signals or {}, sort_keys=True, separators=(",", ":")),
                scope_key,
                normalize_key(name_normalized),
            ),
        )

    def overlay_entity_alias_row(self, scope_key: str, row: dict[str, Any], *, epoch_id: str) -> None:
        """Administrative upsert of a full ``entity_canon`` row (no transition rules)."""
        name_normalized = normalize_key(str(row["name_normalized"]))
        self._engine.execute(
            """
            INSERT INTO entity_canon (
                scope_key, name_normalized, canonical_name, status, link_score,
                link_signals, decided_by, embedding_identifier, proposed_at,
                resolved_at, resolved_by, epoch_id
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (scope_key, name_normalized) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                status = excluded.status,
                link_score = excluded.link_score,
                link_signals = excluded.link_signals,
                decided_by = excluded.decided_by,
                embedding_identifier = excluded.embedding_identifier,
                resolved_at = excluded.resolved_at,
                resolved_by = excluded.resolved_by,
                epoch_id = excluded.epoch_id
            """,
            (
                scope_key,
                name_normalized,
                row["canonical_name"],
                row["status"],
                row.get("link_score"),
                json.dumps(row.get("link_signals") or {}, sort_keys=True, separators=(",", ":")),
                row["decided_by"],
                row.get("embedding_identifier"),
                row.get("proposed_at") or datetime.now(UTC).isoformat(),
                row.get("resolved_at"),
                row.get("resolved_by"),
                epoch_id,
            ),
        )

    def delete_entity_alias_row(self, scope_key: str, name_normalized: str) -> None:
        """Remove a registry row a rollback proves never existed pre-adopt."""
        self._engine.execute(
            "DELETE FROM entity_canon WHERE scope_key = %s AND name_normalized = %s",
            (scope_key, normalize_key(name_normalized)),
        )

    # ==================================================================
    # WS-24: the raw/quarantine store
    # ==================================================================

    def quarantine_candidate(
        self,
        *,
        candidate_uuid: str,
        scope_key: str,
        episode_uuid: str | None,
        reason: str,
        detail: str,
        saves_step: str | None,
        subject: str,
        predicate: str,
        object_text: str,
        proposed_relationship_type: str | None,
        proposed_memory_type: str | None,
        candidate_payload: str,
        candidate_digest: str,
        instruction_set: str | None,
        motive_name: str | None,
        quarantined_at: datetime,
        status: QuarantineStatus = QuarantineStatus.QUARANTINED,
    ) -> QuarantinedCandidate:
        """File one extraction candidate in the raw/quarantine store (idempotent)."""
        if not candidate_uuid:
            raise ValueError("candidate_uuid must be non-blank")
        if ":" not in scope_key:
            raise ValueError(f"invalid quarantine scope key {scope_key!r}")
        self._engine.execute(
            """
            INSERT INTO quarantined_candidates (
                candidate_uuid, scope_key, episode_uuid, reason, detail, saves_step,
                subject, predicate, object, proposed_relationship_type,
                proposed_memory_type, candidate_payload, candidate_digest,
                instruction_set, motive_name, quarantined_at, status
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (candidate_uuid) DO NOTHING
            """,
            (
                candidate_uuid,
                scope_key,
                episode_uuid,
                reason,
                detail,
                saves_step,
                subject,
                predicate,
                object_text,
                proposed_relationship_type,
                proposed_memory_type,
                candidate_payload,
                candidate_digest,
                instruction_set,
                motive_name,
                datetime_to_text(quarantined_at),
                QuarantineStatus(status).value,
            ),
        )
        return self.quarantined_candidate(candidate_uuid)

    def quarantined_candidate(self, candidate_uuid: str) -> QuarantinedCandidate:
        row = self._engine.fetchone(
            "SELECT * FROM quarantined_candidates WHERE candidate_uuid = %s",
            (candidate_uuid,),
        )
        if row is None:
            raise ValueError(f"no quarantined candidate {candidate_uuid!r}")
        return self._quarantined_candidate_from_row(row)

    def quarantined_candidate_payload(self, candidate_uuid: str) -> str:
        """The stored raw candidate JSON (sealed for a protected scope)."""
        row = self._engine.fetchone(
            "SELECT candidate_payload FROM quarantined_candidates WHERE candidate_uuid = %s",
            (candidate_uuid,),
        )
        if row is None:
            raise ValueError(f"no quarantined candidate {candidate_uuid!r}")
        return str(row["candidate_payload"])

    def quarantined_candidates(
        self,
        *,
        scope_key: str | None = None,
        status: QuarantineStatus | str | None = QuarantineStatus.QUARANTINED,
        limit: int | None = None,
    ) -> list[QuarantinedCandidate]:
        sql = "SELECT * FROM quarantined_candidates"
        clauses: list[str] = []
        parameters: list[object] = []
        if scope_key is not None:
            clauses.append("scope_key = %s")
            parameters.append(scope_key)
        if status is not None:
            clauses.append("status = %s")
            parameters.append(QuarantineStatus(status).value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += ' ORDER BY quarantined_at COLLATE "C", candidate_uuid COLLATE "C"'
        if limit is not None:
            sql += " LIMIT %s"
            parameters.append(limit)
        rows = self._engine.fetchall(sql, parameters)
        return [self._quarantined_candidate_from_row(row) for row in rows]

    def quarantined_candidate_content_fields(self, *, scope_key: str) -> list[tuple[str, dict[str, Any]]]:
        """RAW (undecrypted) content-plane values per quarantined candidate.

        The erasure sweep must see what is PERSISTED, not what decrypts on read.
        """
        rows = self._engine.fetchall(
            "SELECT candidate_uuid, candidate_payload, detail, saves_step, "
            "subject, predicate, object FROM quarantined_candidates WHERE scope_key = %s",
            (scope_key,),
        )
        return [
            (
                str(row["candidate_uuid"]),
                {
                    key: row[key]
                    for key in (
                        "candidate_payload",
                        "detail",
                        "saves_step",
                        "subject",
                        "predicate",
                        "object",
                    )
                },
            )
            for row in rows
        ]

    def quarantine_counts(self, *, scope_key: str) -> dict[str, int]:
        rows = self._engine.fetchall(
            "SELECT status, COUNT(*) AS n FROM quarantined_candidates WHERE scope_key = %s GROUP BY status",
            (scope_key,),
        )
        return {str(row["status"]): int(row["n"]) for row in rows}

    def resolve_quarantined_candidate(
        self,
        candidate_uuid: str,
        *,
        status: QuarantineStatus,
        resolved_at: datetime,
        resolved_by: str,
        resolution_note: str | None = None,
        promoted_relationship_uuid: str | None = None,
        reason: str | None = None,
    ) -> QuarantinedCandidate:
        """Transition one stored candidate to a terminal-ish status."""
        with self._engine.transaction():
            existing = self.quarantined_candidate(candidate_uuid)
            if existing.status not in (QuarantineStatus.PENDING, QuarantineStatus.QUARANTINED):
                raise ValueError(f"quarantined candidate {candidate_uuid!r} is already {existing.status.value}")
            self._engine.execute(
                """
                UPDATE quarantined_candidates
                   SET status = %s, resolved_at = %s, resolved_by = %s,
                       resolution_note = %s, promoted_relationship_uuid = %s,
                       reason = COALESCE(%s, reason)
                 WHERE candidate_uuid = %s
                """,
                (
                    QuarantineStatus(status).value,
                    datetime_to_text(resolved_at),
                    resolved_by,
                    resolution_note,
                    promoted_relationship_uuid,
                    reason,
                    candidate_uuid,
                ),
            )
            return self.quarantined_candidate(candidate_uuid)

    def _quarantined_candidate_from_row(self, row: dict[str, Any]) -> QuarantinedCandidate:
        scope_key = str(row["scope_key"])
        kind, scope_id = scope_key.split(":", 1)
        resolved_at = row["resolved_at"]
        return QuarantinedCandidate(
            candidate_uuid=str(row["candidate_uuid"]),
            scope=MemoryScope(kind=kind, scope_id=scope_id),
            episode_uuid=row["episode_uuid"],
            reason=str(row["reason"]),
            detail=str(self.reveal(scope_key, row["detail"] or "")),
            saves_step=(str(self.reveal(scope_key, row["saves_step"])) if row["saves_step"] is not None else None),
            subject=str(self.reveal(scope_key, row["subject"] or "")),
            predicate=str(self.reveal(scope_key, row["predicate"] or "")),
            object=str(self.reveal(scope_key, row["object"] or "")),
            proposed_relationship_type=row["proposed_relationship_type"],
            proposed_memory_type=row["proposed_memory_type"],
            candidate_digest=str(row["candidate_digest"]),
            instruction_set=row["instruction_set"],
            motive_name=row["motive_name"],
            quarantined_at=datetime_from_text(str(row["quarantined_at"])),
            status=QuarantineStatus(str(row["status"])),
            resolved_at=optional_datetime_from_text(resolved_at),
            resolved_by=row["resolved_by"],
            resolution_note=row["resolution_note"],
            promoted_relationship_uuid=row["promoted_relationship_uuid"],
        )
