"""Epoch registry, quarantine, and the canonicalization registries.

Mirrors :mod:`memotron.storage.postgres._epochs`. An epoch is the unit a
re-dream branches and a rollback restores; the predicate and entity-alias
registries are epoch-scoped too, which is why they live here rather than with the
graph -- an overlay written on a branch must be invisible to HEAD until adopted."""

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
from memotron.storage.base import (
    normalize_key,
)

# ``governance_keys`` is keyed (scope_key, subject_key) and holds MORE THAN ONE
# kind of row.  ``subject_key`` is what tells them apart, and confusing the two
# is a correctness bug with no error message, so both kinds are named here and
# every query states which one it means.
CONTENT_PLANE_SUBJECT_KEY = ""


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class EpochRegistryPlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    def scope_content_is_protected(self, scope_key: str) -> bool:
        """WS-23 M1: has this scope's content plane ever been sealed?

        True from the moment a crypto-shred scope provisions its DEK and stays
        true after the key is destroyed — a post-shred write must not put
        plaintext where the sweep guarantees ciphertext.

        The question is about the CONTENT-PLANE row specifically
        (:data:`CONTENT_PLANE_SUBJECT_KEY`), not about "any row that happens to
        share this scope key".  ``governance_keys`` is a multi-kind table, and a
        tenant's sealed LLM credential lives at ``(tenant:{id},
        llm_credentials)`` — byte-identical scope key, entirely different row
        kind.  Matching on ``scope_key`` alone made sealing a credential
        announce that the tenant scope was crypto-shred governed, which forced
        every embedding in that scope onto the hermetic local transport
        (``DreamEngine.content_embedding_transport``) and withheld every receipt
        reason (``ReceiptLedger._enforce_content_free_reason``) — with no error,
        for an operator who had just configured ``text-embedding-3``.

        Filtering on the row kind, rather than special-casing the credential
        key, is the whitelist form: the predicate now answers the question it
        is named after, and any future row kind added to this table is correct
        by construction instead of needing another exclusion.  It also needs no
        data rewrite — existing graphs already carry their credential at
        ``llm_credentials`` (it has been written there since the row kind was
        introduced), so an affected store is corrected the moment it is
        reopened.  ``migration._source_governance_blocked_reason`` already made
        exactly this distinction; this is the one place that did not.
        """
        row = self._connection.execute(
            "SELECT 1 FROM governance_keys WHERE scope_key = ? AND subject_key = ? LIMIT 1",
            (scope_key, CONTENT_PLANE_SUBJECT_KEY),
        ).fetchone()
        return row is not None

    def _active_epoch_ancestry(self, scope_key: str) -> frozenset[str] | None:
        """The active epoch's ancestry chain for *scope_key*, or ``None``.

        ``None`` means the scope has never registered an active epoch (no
        caller has ever gone through :meth:`ensure_root_epoch` /
        :mod:`memotron.epochs`) — every epoch-filtered read treats that as
        "no filtering", preserving pre-WS-26 behaviour byte-for-byte.
        """
        active = self.active_epoch_for_scope(scope_key)
        if active is None:
            return None
        return frozenset(self.epoch_ancestry(active))

    def registry_state_digest(self, scope_key: str) -> str:
        """WS-26 T9: deterministic digest of a scope's canonicalization-registry
        state (entity aliases + predicate canonicals), epoch-ancestry filtered
        exactly like :meth:`_epoch_visible`.

        This is the registry half of "byte-for-byte" T6 rollback verification:
        :mod:`memotron.epochs` composes ``graph_state_hash(scope) +
        registry_state_digest(scope)`` into one full-state signature before
        an adopt and asserts it is restored exactly after a rollback.  It is
        deliberately NOT folded into :meth:`graph_state_hash` itself — see
        that method's docstring for why (registry mutations are not
        receipt-bracketed today, so folding them in would desynchronize the
        pre-existing byte-replay chain).  A scope with no registry rows at
        all still returns a stable digest (of the empty structure), not
        ``None`` — every scope has a well-defined registry state.
        """
        ancestry = self._active_epoch_ancestry(scope_key)
        entity_rows = self._connection.execute(
            "SELECT name_normalized, canonical_name, status, epoch_id FROM entity_canon WHERE scope_key = ?",
            (scope_key,),
        ).fetchall()
        predicate_rows = self._connection.execute(
            "SELECT predicate_normalized, canonical_predicate, epoch_id FROM predicate_canon WHERE scope_key = ?",
            (scope_key,),
        ).fetchall()

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

    def ensure_root_epoch(self, scope_key: str, *, now: datetime) -> str:
        """WS-26 T2: lazily create *scope_key*'s root epoch and return HEAD.

        Idempotent — a scope that already has an active epoch just returns
        it.  The formation provenance choke-point (``dreaming.py``) calls this
        on every write, so a scope that never branches ends up with exactly
        one epoch for its whole life and every read stays byte-identical to
        pre-WS-26 behaviour (see :meth:`_epoch_visible`).
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("ensure_root_epoch requires a non-blank scope_key")
        existing = self.active_epoch_for_scope(scope_key)
        if existing is not None:
            return existing
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        now_text = self._datetime_to_text(now) or now.isoformat()
        self._connection.execute(
            """
            INSERT INTO graph_epochs (
                epoch_id, scope_key, parent_epoch_id, created_at, label, status
            ) VALUES (?, ?, NULL, ?, 'root', 'adopted')
            """,
            (epoch_id, scope_key, now_text),
        )
        self._connection.execute(
            "INSERT INTO active_epochs (scope_key, epoch_id, updated_at) VALUES (?, ?, ?)",
            (scope_key, epoch_id, now_text),
        )
        self._commit()
        return epoch_id

    def active_epoch_for_scope(self, scope_key: str) -> str | None:
        """WS-26 T2: the scope's current HEAD epoch id, or ``None`` if never initialized."""
        row = self._connection.execute(
            "SELECT epoch_id FROM active_epochs WHERE scope_key = ?", (scope_key,)
        ).fetchone()
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
        """WS-26 T2/T4: fork a new (initially non-HEAD) epoch under *parent_epoch_id*.

        Recording the fork is copying a pointer, not data (NEXT.md §4): no
        relationship rows move.  HEAD (``active_epochs``) is untouched — the
        new epoch only becomes readable/adoptable once :mod:`memotron.epochs`
        populates it and it is explicitly adopted.
        """
        if not self._epoch_exists(parent_epoch_id):
            raise ValueError(f"parent epoch does not exist: {parent_epoch_id}")
        epoch_id = f"epoch-{secrets.token_hex(16)}"
        self._connection.execute(
            """
            INSERT INTO graph_epochs (
                epoch_id, scope_key, parent_epoch_id, created_at, label, status,
                tier, overrides_json, shadow_store_path
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                epoch_id,
                scope_key,
                parent_epoch_id,
                self._datetime_to_text(now) or now.isoformat(),
                label,
                status,
                tier,
                self._json_dumps(overrides or {}, "epoch overrides"),
                shadow_store_path,
            ),
        )
        self._commit()
        return epoch_id

    def _epoch_exists(self, epoch_id: str) -> bool:
        row = self._connection.execute("SELECT 1 FROM graph_epochs WHERE epoch_id = ?", (epoch_id,)).fetchone()
        return row is not None

    def epoch(self, epoch_id: str) -> dict[str, Any]:
        """WS-26 T2: one epoch's bookkeeping row, or raise ``ValueError``."""
        row = self._connection.execute("SELECT * FROM graph_epochs WHERE epoch_id = ?", (epoch_id,)).fetchone()
        if row is None:
            raise ValueError(f"epoch does not exist: {epoch_id}")
        return epoch_row_to_dict(row)

    def epochs_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """WS-26 T2: every epoch ever created for *scope_key*, oldest first."""
        rows = self._connection.execute(
            "SELECT * FROM graph_epochs WHERE scope_key = ? ORDER BY created_at, epoch_id",
            (scope_key,),
        ).fetchall()
        return [epoch_row_to_dict(row) for row in rows]

    def set_epoch_pre_adopt_snapshot(self, epoch_id: str, snapshot: dict[str, Any]) -> None:
        """WS-26 T6: record the exact pre-adopt state an adopt is about to
        retire, so :func:`memotron.epochs.rollback_epoch` can restore it
        byte-for-byte without needing to keep the recompute's shadow store
        indefinitely.  Written once, at adopt time, onto the epoch being
        adopted (never onto the epoch it superseded)."""
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        self._connection.execute(
            "UPDATE graph_epochs SET pre_adopt_snapshot_json = ? WHERE epoch_id = ?",
            (self._json_dumps(snapshot, "epoch pre-adopt snapshot"), epoch_id),
        )
        self._commit()

    def set_epoch_status(self, epoch_id: str, status: str) -> dict[str, Any]:
        """WS-26 T6: bookkeeping-only status transition (open/ready/adopted/
        superseded/discarded).  Never deletes or mutates the epoch's rows —
        "no destruction" applies to epoch records exactly like relationships."""
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        self._connection.execute("UPDATE graph_epochs SET status = ? WHERE epoch_id = ?", (status, epoch_id))
        self._commit()
        return self.epoch(epoch_id)

    def set_active_epoch(self, scope_key: str, epoch_id: str, *, now: datetime) -> None:
        """WS-26 T6: the pointer flip — adopt or roll back a scope's HEAD.

        Every relationship-version-carrying read (`graph_state_hash`,
        `active_relationships`, `context_visible_relationships`,
        `relationships_for_scope`) resolves this pointer's ancestry on every
        call, so flipping it is the entire visible effect: no relationship row
        is touched by this call itself.
        """
        if not self._epoch_exists(epoch_id):
            raise ValueError(f"epoch does not exist: {epoch_id}")
        now_text = self._datetime_to_text(now) or now.isoformat()
        self._connection.execute(
            """
            INSERT INTO active_epochs (scope_key, epoch_id, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(scope_key) DO UPDATE SET epoch_id = excluded.epoch_id,
                updated_at = excluded.updated_at
            """,
            (scope_key, epoch_id, now_text),
        )
        self._commit()

    def record_epoch_run(self, epoch_id: str, run_uuid: str, *, now: datetime) -> None:
        """WS-26 T2: attribute one dream-job/receipt run to the epoch it wrote into.

        Deliberately does NOT require ``epoch_id`` to exist in THIS store's
        own ``graph_epochs`` table: :mod:`memotron.epochs` calls this on a
        branch's shadow store, where the epoch's bookkeeping row lives only in
        the main store (the shadow is a scratch workspace, never a party to
        its own epoch's HEAD pointer) — see the ``epoch_runs`` DDL note.
        """
        self._connection.execute(
            "INSERT OR IGNORE INTO epoch_runs (epoch_id, run_uuid, recorded_at) VALUES (?, ?, ?)",
            (epoch_id, run_uuid, self._datetime_to_text(now) or now.isoformat()),
        )
        self._commit()

    def epoch_run_uuids(self, epoch_id: str) -> list[str]:
        """WS-26 T2/T6: every run that contributed to *epoch_id*, oldest first."""
        rows = self._connection.execute(
            "SELECT run_uuid FROM epoch_runs WHERE epoch_id = ? ORDER BY recorded_at, run_uuid",
            (epoch_id,),
        ).fetchall()
        return [str(row["run_uuid"]) for row in rows]

    def epoch_ancestry(self, epoch_id: str) -> tuple[str, ...]:
        """WS-26 T2/T9: *epoch_id* plus every ancestor up to the root, self first.

        The chain visibility reads (`_epoch_visible`, `_registry_state_digest`)
        resolve against: it is what "HEAD's registries / HEAD's facts" means
        for a branch that has not overlaid something itself.
        """
        chain: list[str] = []
        current: str | None = epoch_id
        seen: set[str] = set()
        while current is not None and current not in seen:
            seen.add(current)
            chain.append(current)
            row = self._connection.execute(
                "SELECT parent_epoch_id FROM graph_epochs WHERE epoch_id = ?", (current,)
            ).fetchone()
            current = str(row["parent_epoch_id"]) if row is not None and row["parent_epoch_id"] else None
        return tuple(chain)

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
        """File one extraction candidate in the raw/quarantine store.

        ``status`` defaults to ``QUARANTINED`` — the WS-24 abstain path's
        original, still-most-common call shape: a candidate governance refused
        outright, filed straight into its terminal state.

        The staged pipeline reuses this SAME store for stage-1 raw output
        (``status=PENDING``): a structurally-valid candidate is filed here the
        moment it is extracted, before stage-3 governance has run over it, so
        stage-1 output is countable and inspectable independent of what
        governance later decides.  ``ON CONFLICT DO NOTHING`` keeps this
        idempotent — a candidate whose digest already has a row (a re-run over
        the same episode) leaves the existing row exactly as governance left it,
        never resetting a governed verdict back to raw.

        Content-plane fields (``detail``, ``saves_step``, subject/predicate/
        object, the raw payload) arrive already sealed for a content-protected
        scope — the caller holds the DEK, the store never does.
        """
        if not candidate_uuid:
            raise ValueError("candidate_uuid must be non-blank")
        if ":" not in scope_key:
            raise ValueError(f"invalid quarantine scope key {scope_key!r}")
        self._connection.execute(
            """
            INSERT INTO quarantined_candidates (
                candidate_uuid, scope_key, episode_uuid, reason, detail, saves_step,
                subject, predicate, object, proposed_relationship_type,
                proposed_memory_type, candidate_payload, candidate_digest,
                instruction_set, motive_name, quarantined_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(candidate_uuid) DO NOTHING
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
                self._datetime_to_text(quarantined_at),
                QuarantineStatus(status).value,
            ),
        )
        self._connection.commit()
        return self.quarantined_candidate(candidate_uuid)

    def quarantined_candidate(self, candidate_uuid: str) -> QuarantinedCandidate:
        row = self._connection.execute(
            "SELECT * FROM quarantined_candidates WHERE candidate_uuid = ?",
            (candidate_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"no quarantined candidate {candidate_uuid!r}")
        return self._quarantined_candidate_from_row(row)

    def quarantined_candidate_payload(self, candidate_uuid: str) -> str:
        """The stored raw candidate JSON (sealed for a protected scope)."""
        row = self._connection.execute(
            "SELECT candidate_payload FROM quarantined_candidates WHERE candidate_uuid = ?",
            (candidate_uuid,),
        ).fetchone()
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
            clauses.append("scope_key = ?")
            parameters.append(scope_key)
        if status is not None:
            clauses.append("status = ?")
            parameters.append(QuarantineStatus(status).value)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY quarantined_at, candidate_uuid"
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        rows = self._connection.execute(sql, parameters).fetchall()
        return [self._quarantined_candidate_from_row(row) for row in rows]

    def quarantined_candidate_content_fields(self, *, scope_key: str) -> list[tuple[str, dict[str, Any]]]:
        """RAW (undecrypted) content-plane values per quarantined candidate.

        The erasure sweep must see what is PERSISTED, not what decrypts on read
        — a reveal would defeat the very check it is performing.
        """
        rows = self._connection.execute(
            "SELECT candidate_uuid, candidate_payload, detail, saves_step, "
            "subject, predicate, object FROM quarantined_candidates WHERE scope_key = ?",
            (scope_key,),
        ).fetchall()
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
        rows = self._connection.execute(
            "SELECT status, COUNT(*) AS n FROM quarantined_candidates WHERE scope_key = ? GROUP BY status",
            (scope_key,),
        ).fetchall()
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
        """Transition one stored candidate to a terminal-ish status.

        Two callers share this one state machine: an OPERATOR resolving a
        ``QUARANTINED`` row (``resolved_by`` names the human), and stage-3
        GOVERNANCE resolving a ``PENDING`` raw row the instant it decides the
        candidate's disposition (``resolved_by="governance"`` or
        ``"regovern"``) — the same transition, the same receipted record,
        whichever decided it.  Re-govern additionally reuses this to move an
        already-``QUARANTINED`` row to ``PROMOTED`` once a policy change makes
        it pass, which is why both starting states are accepted.

        ``reason`` (default ``None``, meaning "leave it") lets the AUTOMATIC
        governance callers stamp the machine violation/acceptance code onto a
        row filed raw as ``PENDING`` — that row's ``reason`` column was a
        placeholder at insert time, unlike a row ``_file_quarantined_candidates``
        files directly into ``QUARANTINED`` with its real reason already set.
        The operator promote/discard path never passes it, so an operator
        resolution leaves the original stage-3 reason exactly as governance
        recorded it.
        """
        existing = self.quarantined_candidate(candidate_uuid)
        if existing.status not in (QuarantineStatus.PENDING, QuarantineStatus.QUARANTINED):
            raise ValueError(f"quarantined candidate {candidate_uuid!r} is already {existing.status.value}")
        self._connection.execute(
            """
            UPDATE quarantined_candidates
               SET status = ?, resolved_at = ?, resolved_by = ?,
                   resolution_note = ?, promoted_relationship_uuid = ?,
                   reason = COALESCE(?, reason)
             WHERE candidate_uuid = ?
            """,
            (
                QuarantineStatus(status).value,
                self._datetime_to_text(resolved_at),
                resolved_by,
                resolution_note,
                promoted_relationship_uuid,
                reason,
                candidate_uuid,
            ),
        )
        self._connection.commit()
        return self.quarantined_candidate(candidate_uuid)

    def _quarantined_candidate_from_row(self, row: Any) -> QuarantinedCandidate:
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
            quarantined_at=self._datetime_from_text(str(row["quarantined_at"])),
            status=QuarantineStatus(str(row["status"])),
            resolved_at=(self._datetime_from_text(str(resolved_at)) if resolved_at is not None else None),
            resolved_by=row["resolved_by"],
            resolution_note=row["resolution_note"],
            promoted_relationship_uuid=row["promoted_relationship_uuid"],
        )

    def canonical_predicate_for(self, scope_key: str, predicate: str) -> str | None:
        """Registered canonical for one predicate surface, or None when unmapped."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_predicate_for requires a non-blank scope_key")
        row = self._connection.execute(
            """
            SELECT canonical_predicate FROM predicate_canon
            WHERE scope_key = ? AND predicate_normalized = ?
            """,
            (scope_key, normalize_key(predicate)),
        ).fetchone()
        return str(row["canonical_predicate"]) if row is not None else None

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
        """Record one first-wins predicate mapping for a scope.

        Idempotent per (scope_key, predicate): re-registering an already-mapped
        surface is a no-op — the FIRST decision stands, keeping resolution
        deterministic for a given arrival order.
        """
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
        self._connection.execute(
            """
            INSERT INTO predicate_canon (
                scope_key, predicate_normalized, canonical_predicate, decided_by,
                embedding_identifier, cosine, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, predicate_normalized) DO NOTHING
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
        self._connection.commit()

    def canonical_predicates_for_scope(self, scope_key: str) -> list[str]:
        """Distinct canonical predicates for a scope, in registration order.

        Registration order (created_at, then surface for same-instant ties) is
        the deterministic candidate order the T16 embedding pass compares
        against and caps with ``max_candidates``.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_predicates_for_scope requires a non-blank scope_key")
        rows = self._connection.execute(
            """
            SELECT canonical_predicate, MIN(created_at || '|' || predicate_normalized) AS first_seen
            FROM predicate_canon
            WHERE scope_key = ?
            GROUP BY canonical_predicate
            ORDER BY first_seen
            """,
            (scope_key,),
        ).fetchall()
        return [str(row["canonical_predicate"]) for row in rows]

    def predicate_alias_row(self, scope_key: str, predicate: str) -> dict[str, Any] | None:
        """WS-26 T5/T9: the full ``predicate_canon`` row for one surface, or None."""
        row = self._connection.execute(
            "SELECT * FROM predicate_canon WHERE scope_key = ? AND predicate_normalized = ?",
            (scope_key, normalize_key(predicate)),
        ).fetchone()
        return predicate_alias_row_to_dict(row) if row is not None else None

    def predicate_alias_rows_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """WS-26 T5/T9: every ``predicate_canon`` row for a scope, registration order."""
        rows = self._connection.execute(
            "SELECT * FROM predicate_canon WHERE scope_key = ? ORDER BY created_at, predicate_normalized",
            (scope_key,),
        ).fetchall()
        return [predicate_alias_row_to_dict(row) for row in rows]

    def overlay_predicate_row(self, scope_key: str, row: dict[str, Any], *, epoch_id: str) -> None:
        """WS-26 T6/T9: administrative upsert of a full ``predicate_canon`` row.

        The predicate-registry counterpart of :meth:`overlay_entity_alias_row`
        — see its docstring for why this bypasses :meth:`register_predicate`'s
        first-wins rule.
        """
        predicate_normalized = normalize_key(str(row["predicate_normalized"]))
        self._connection.execute(
            """
            INSERT INTO predicate_canon (
                scope_key, predicate_normalized, canonical_predicate, decided_by,
                embedding_identifier, cosine, created_at, epoch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, predicate_normalized) DO UPDATE SET
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
        self._commit()

    def delete_predicate_row(self, scope_key: str, predicate_normalized: str) -> None:
        """WS-26 T6: the predicate-registry counterpart of :meth:`delete_entity_alias_row`."""
        self._connection.execute(
            "DELETE FROM predicate_canon WHERE scope_key = ? AND predicate_normalized = ?",
            (scope_key, normalize_key(predicate_normalized)),
        )
        self._commit()

    def canonical_entity_name_for(self, scope_key: str, name: str) -> str | None:
        """ACTIVE-alias canonical name for one entity surface, or None when unmapped.

        Only ``status='active'`` rows resolve — a 'proposed' alias awaits
        adjudication and a 'rejected' alias never bridges, so formation and the
        read side stop resolving through a demoted or rejected link the moment
        its status changes.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("canonical_entity_name_for requires a non-blank scope_key")
        row = self._connection.execute(
            """
            SELECT canonical_name FROM entity_canon
            WHERE scope_key = ? AND name_normalized = ? AND status = 'active'
            """,
            (scope_key, normalize_key(name)),
        ).fetchone()
        return str(row["canonical_name"]) if row is not None else None

    def entity_alias_row(self, scope_key: str, name: str) -> dict[str, Any] | None:
        """The full registry row for one surface name regardless of status."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("entity_alias_row requires a non-blank scope_key")
        row = self._connection.execute(
            "SELECT * FROM entity_canon WHERE scope_key = ? AND name_normalized = ?",
            (scope_key, normalize_key(name)),
        ).fetchone()
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
        """Record one alias→canonical mapping for a scope (first-wins for ACTIVE).

        Transition rules (deterministic for a given call order):

        - no existing row       → insert with the given status.
        - existing ACTIVE       → no-op; the FIRST active decision stands.
        - existing PROPOSED     → a new ACTIVE registration for the SAME
          canonical upgrades the proposal in place (``resolved_by`` =
          *decided_by*); a new PROPOSED registration refreshes
          ``link_score``/``link_signals`` to the latest observation; an ACTIVE
          registration toward a DIFFERENT canonical is a no-op (the pending
          proposal must be adjudicated first).
        - existing REJECTED     → no-op; re-proposal goes through
          :meth:`resolve_entity_alias` under the engine's improved-score rule.

        Returns the row as stored after the operation.
        """
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
        existing = self.entity_alias_row(scope_key, normalized_name)
        if existing is None:
            self._connection.execute(
                """
                INSERT INTO entity_canon (
                    scope_key, name_normalized, canonical_name, status, link_score,
                    link_signals, decided_by, embedding_identifier, proposed_at,
                    resolved_at, resolved_by
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            self._connection.commit()
        elif existing["status"] == "proposed":
            same_canonical = normalize_key(existing["canonical_name"]) == normalize_key(canonical_surface)
            if status == "active" and same_canonical:
                self._connection.execute(
                    """
                    UPDATE entity_canon
                    SET status = 'active', link_score = ?, link_signals = ?,
                        embedding_identifier = ?, resolved_at = ?, resolved_by = ?
                    WHERE scope_key = ? AND name_normalized = ?
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
                self._connection.commit()
            elif status == "proposed" and same_canonical:
                self._connection.execute(
                    """
                    UPDATE entity_canon
                    SET link_score = ?, link_signals = ?, embedding_identifier = ?
                    WHERE scope_key = ? AND name_normalized = ?
                    """,
                    (link_score, signals_json, embedding_identifier, scope_key, normalized_name),
                )
                self._connection.commit()
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
            rows = self._connection.execute(
                """
                SELECT * FROM entity_canon WHERE scope_key = ?
                ORDER BY proposed_at, name_normalized
                """,
                (scope_key,),
            ).fetchall()
        else:
            rows = self._connection.execute(
                """
                SELECT * FROM entity_canon WHERE scope_key = ? AND status = ?
                ORDER BY proposed_at, name_normalized
                """,
                (scope_key, status),
            ).fetchall()
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
        """Transition one registry row's status (approve / reject / demote / re-propose).

        ``link_score``/``link_signals``/``canonical_name`` overwrite the stored
        values when supplied (None leaves them unchanged) so demotions and
        re-proposals can carry their updated evidence — a re-proposal after a
        rejection may target a different canonical.  Fails fast on a missing row.
        """
        if status not in self._ENTITY_ALIAS_STATUSES:
            raise ValueError(f"resolve_entity_alias status must be one of {self._ENTITY_ALIAS_STATUSES}")
        if not isinstance(resolved_by, str) or not resolved_by.strip():
            raise ValueError("resolve_entity_alias requires a non-blank resolved_by")
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
        self._connection.execute(
            """
            UPDATE entity_canon
            SET status = ?, canonical_name = ?, link_score = ?, link_signals = ?,
                resolved_at = ?, resolved_by = ?
            WHERE scope_key = ? AND name_normalized = ?
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
        self._connection.commit()
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
        """Mutate one row's living link evidence (score + signals) in place.

        Used by the WS-16-style bounded accumulation on corroborating
        resolutions and by the contradiction discount; status is untouched.
        """
        self._connection.execute(
            """
            UPDATE entity_canon SET link_score = ?, link_signals = ?
            WHERE scope_key = ? AND name_normalized = ?
            """,
            (
                link_score,
                json.dumps(link_signals or {}, sort_keys=True, separators=(",", ":")),
                scope_key,
                normalize_key(name_normalized),
            ),
        )
        self._connection.commit()

    def overlay_entity_alias_row(self, scope_key: str, row: dict[str, Any], *, epoch_id: str) -> None:
        """WS-26 T6/T9: administrative upsert of a full ``entity_canon`` row.

        Used only by :mod:`memotron.epochs` to merge a branch's overlay
        registry row into the main store at adopt time (and to restore a
        pre-adopt snapshot at rollback).  Unlike :meth:`register_entity_alias`
        this has no first-wins/transition business rules — it writes exactly
        the row it is given, tagged with the epoch that produced it, because
        the caller already resolved which version should win.
        """
        name_normalized = normalize_key(str(row["name_normalized"]))
        self._connection.execute(
            """
            INSERT INTO entity_canon (
                scope_key, name_normalized, canonical_name, status, link_score,
                link_signals, decided_by, embedding_identifier, proposed_at,
                resolved_at, resolved_by, epoch_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scope_key, name_normalized) DO UPDATE SET
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
        self._commit()

    def delete_entity_alias_row(self, scope_key: str, name_normalized: str) -> None:
        """WS-26 T6: remove a registry row that a rollback proves never existed
        pre-adopt (the row was created fresh by the branch being rolled back
        away from — restoring "did not exist" is a delete, not a value)."""
        self._connection.execute(
            "DELETE FROM entity_canon WHERE scope_key = ? AND name_normalized = ?",
            (scope_key, normalize_key(name_normalized)),
        )
        self._commit()
