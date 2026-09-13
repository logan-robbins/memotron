"""The property-graph plane: nodes, relationships, and the scope state hash.

Mirrors :mod:`memotron.storage.postgres._graph`. The state-tuple helpers travel
with it because graph_state_hash is their only caller -- together they define what
a scope state hash commits to, which is what epoch rollback verifies byte-for-byte.

relationships_for_node_uuids carries the T0-10 fix: the entity-hop frontier applies
the SAME active-epoch ancestry filter as relationships_for_scope. Without it a
rolled-back epoch keeps being served through retrieval expansion, so the audit view
and search disagree about whether a memory exists."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Annotation-only, and deferred by the future import above. A runtime import
    # would be a cycle: the package __init__ imports this module to compose the
    # class this annotation names.
    from memotron.storage.sqlite import SQLiteStorageBackend

import hashlib
import json
import sqlite3
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from memotron.embedding import cosine_similarity
from memotron.models import (
    GraphNode,
    GraphRelationship,
    MemoryScope,
    RelationshipStatus,
    ScopeKind,
)
from memotron.storage._shared import canonical_visibility_agents
from memotron.storage.base import (
    normalize_key,
)

# WS-3×WS-4 §101: single source of truth for the index-backed context-visible read.
# Seeks the ``relationships_ctx_idx`` generated-column index on
# (gen_scope_key, gen_status, gen_context_visible) so the default working-context
# tier never scans other scopes, non-active rows, or demoted members.
_CONTEXT_VISIBLE_SELECT = """
    SELECT * FROM relationships
    WHERE gen_scope_key = ?
      AND gen_status = ?
      AND gen_context_visible = 1
    ORDER BY created_at, uuid
"""


class ScopeStateHashTracker:
    """Single-scan, incrementally maintained ``graph_state_hash`` for one scope.

    Built by :meth:`SQLiteStorageBackend.scope_state_hash_tracker`; see that
    method for when (and when not) to use one. ``digest`` is always the hash the
    store would return for the scope right now, given that every write to the
    scope since construction was passed to :meth:`record`.
    """

    def __init__(self, store: SQLiteStorageBackend, scope_key: str) -> None:
        self._store = store
        self.scope_key = scope_key
        self._tuples = store._scope_state_tuples(scope_key)
        self._digest = store._digest_state_tuples(self._tuples)

    @property
    def digest(self) -> str:
        """The scope's current state hash."""
        return self._digest

    def record(self, relationship: GraphRelationship) -> str:
        """Fold a just-written relationship in and return the new state hash.

        ``MENTIONS`` edges are structural navigation and are excluded from the
        hash, exactly as in a full recompute, so recording one is a no-op.
        """
        if relationship.type != "MENTIONS":
            self._tuples[relationship.uuid] = self._store._relationship_state_tuple(relationship)
            self._digest = self._store._digest_state_tuples(self._tuples)
        return self._digest


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class GraphPlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    def upsert_node(
        self,
        *,
        labels: Iterable[str],
        key: str,
        properties: dict[str, Any],
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> tuple[GraphNode, bool]:
        normalized_key = normalize_key(key)
        existing_uuid = self._connection.execute(
            "SELECT uuid FROM nodes WHERE graph_key = ?",
            (normalized_key,),
        ).fetchone()
        if existing_uuid is not None:
            node = self.get_node(str(existing_uuid["uuid"]))
            # WS-12: never overwrite an existing sealed name with a re-sealed copy —
            # sealing is nonce-random, and the stored name feeds graph_state_hash, so a
            # rewrite would shift the scope's state hash outside any receipted mutation.
            from memotron.crypto import is_sealed_content

            if is_sealed_content(properties.get("name")) and is_sealed_content(node.properties.get("name")):
                properties = {k: v for k, v in properties.items() if k != "name"}
            node.properties.update(properties)
            if valid_from is not None:
                node.valid_from = valid_from if node.valid_from is None else min(node.valid_from, valid_from)
            if valid_to is None:
                node.valid_to = None
            elif node.valid_to is not None:
                node.valid_to = max(node.valid_to, valid_to)
            self._save_node(node)
            return node, False

        node = GraphNode(
            labels=tuple(dict.fromkeys(labels)),
            properties={**properties, "graph_key": normalized_key},
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._connection.execute(
            """
            INSERT INTO nodes (uuid, graph_key, labels_json, properties_json, created_at, valid_from, valid_to)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                node.uuid,
                normalized_key,
                self._json_dumps(list(node.labels), "node labels"),
                self._json_dumps(node.properties, "node properties"),
                self._datetime_to_text(node.created_at),
                self._datetime_to_text(node.valid_from),
                self._datetime_to_text(node.valid_to),
            ),
        )
        self._commit()
        return node, True

    def add_relationship(
        self,
        *,
        source_uuid: str,
        target_uuid: str,
        relationship_type: str,
        properties: dict[str, Any],
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> GraphRelationship:
        if not self._node_exists(source_uuid):
            raise ValueError(f"source node does not exist: {source_uuid}")
        if not self._node_exists(target_uuid):
            raise ValueError(f"target node does not exist: {target_uuid}")
        relationship = GraphRelationship(
            source_uuid=source_uuid,
            target_uuid=target_uuid,
            type=relationship_type,
            properties=properties,
            valid_from=valid_from,
            valid_to=valid_to,
        )
        self._connection.execute(
            """
            INSERT INTO relationships (
                uuid, source_uuid, target_uuid, type, properties_json, created_at, valid_from, valid_to
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                relationship.uuid,
                relationship.source_uuid,
                relationship.target_uuid,
                relationship.type,
                self._json_dumps(relationship.properties, "relationship properties"),
                self._datetime_to_text(relationship.created_at),
                self._datetime_to_text(relationship.valid_from),
                self._datetime_to_text(relationship.valid_to),
            ),
        )
        self._commit()
        return relationship

    def get_node(self, uuid: str) -> GraphNode:
        row = self._connection.execute("SELECT * FROM nodes WHERE uuid = ?", (uuid,)).fetchone()
        if row is None:
            raise ValueError(f"node does not exist: {uuid}")
        return self._row_to_node(row)

    def get_relationship(self, uuid: str) -> GraphRelationship:
        row = self._connection.execute("SELECT * FROM relationships WHERE uuid = ?", (uuid,)).fetchone()
        if row is None:
            raise ValueError(f"relationship does not exist: {uuid}")
        return self._row_to_relationship(row)

    def nodes(self) -> list[GraphNode]:
        rows = self._connection.execute("SELECT * FROM nodes ORDER BY created_at, uuid").fetchall()
        return [self._row_to_node(row) for row in rows]

    def relationships(self) -> list[GraphRelationship]:
        rows = self._connection.execute("SELECT * FROM relationships ORDER BY created_at, uuid").fetchall()
        return [self._row_to_relationship(row) for row in rows]

    def active_relationships(
        self,
        *,
        scope: MemoryScope | None = None,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        # WS-26 T6: scoped to the current active epoch's ancestry so adopt/
        # rollback are visible here, not only in graph_state_hash — a branch
        # that was never adopted (or was rolled back away from) must not
        # surface as current truth.  Precomputed once when *scope* narrows the
        # call; resolved per-relationship for the fleet-wide (scope=None) scan.
        ancestry = self._active_epoch_ancestry(scope.key) if scope is not None else None
        ancestry_cache: dict[str, frozenset[str] | None] = {}
        relationships = []
        for relationship in self.relationships():
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            row_scope_key = relationship.properties.get("scope_key")
            if scope is not None and row_scope_key != scope.key:
                continue
            if relationship_types is not None and relationship.type not in relationship_types:
                continue
            if scope is not None:
                row_ancestry = ancestry
            else:
                row_ancestry = ancestry_cache.setdefault(
                    str(row_scope_key), self._active_epoch_ancestry(str(row_scope_key))
                )
            if not self._epoch_visible(relationship, ancestry=row_ancestry):
                continue
            relationships.append(relationship)
        return relationships

    def scopes(self) -> list[MemoryScope]:
        """Return the distinct scopes that have at least one relationship in the graph.

        Used by fleet-wide operations (e.g. coherence remediation) that must
        enumerate existing scopes. Order is first-seen, deterministic per store.
        """
        seen: dict[str, MemoryScope] = {}
        for relationship in self.relationships():
            kind = relationship.properties.get("scope_kind")
            scope_id = relationship.properties.get("scope_id")
            if not kind or not scope_id:
                continue
            key = f"{kind}:{scope_id}"
            if key not in seen:
                seen[key] = MemoryScope(kind=ScopeKind(kind), scope_id=str(scope_id))
        return list(seen.values())

    def graph_state_hash(self, scope_key: str) -> str:
        """WS-11: deterministic content hash of a scope's memory-relationship state.

        Per PATENT_REPLAY_RECEIPTS_SPEC.md ([0023], claims 4 & 6), the hash is a
        sha256 over the canonical, uuid-ordered list of relationship tuples in
        *scope_key*::

            (uuid, relationship_type, subject, predicate, object-digest,
             memory_type, status, active_in_context, rolled_up_by,
             superseded_by_relationship_uuid, valid_from, valid_to,
             observed_count, pinned, visibility_agents,
             produced_by_run, superseded_by_run, pruned_by_run)

        WS-23 M2: ``pinned`` and ``visibility_agents`` are IN the tuple.  Both
        are operator-controlled access state that decides what a caller sees,
        so leaving them out let a pin/unpin/visibility receipt bracket an
        unchanged hash (before == after) and let an out-of-band ACL write pass
        every chain verification.  Contract-visible change: state hashes
        recorded before this revision differ from the ones recomputed after it
        for any scope that carries pins or allowlists.

        WS-26 T1: ``produced_by_run``/``superseded_by_run``/``pruned_by_run``
        joined the tuple so a byte replay covers derivation-DAG provenance,
        not only the fact content — a run that silently re-stamped lineage
        without changing a fact's text would otherwise pass every chain check.

        WS-26 T2/T9: the ROW SET this hash folds over is additionally scoped to
        the scope's current active epoch's ancestry (self plus every ancestor
        up to the root) when the scope has ever registered one via
        :meth:`ensure_root_epoch` — a row tagged with an epoch outside that
        ancestry (e.g. a sibling branch that was never adopted, or a branch
        that was rolled back away from) is excluded, exactly as if it did not
        exist.  A row with NO ``epoch_id`` at all (written before WS-26, or by
        a caller that never touches epochs) is always included — it is the
        implicit base layer every epoch inherits.  A scope that never calls
        into :mod:`memotron.epochs` never registers an active epoch, so
        this filter is a no-op and the hash is byte-identical to pre-WS-26
        behaviour.

        T9 note: registry (entity/predicate canon) state does NOT join THIS
        digest.  It would be the tidier design, but registry mutations
        (auto-link, predicate canonicalization) are not bracketed by their own
        graph_state_hash_before/after receipts today, and this hash is the
        exact value byte replay chains receipt-to-receipt (PATENT_REPLAY_
        RECEIPTS_SPEC.md [0025] step 440) — folding registry state in here
        would desynchronize an EXISTING receipt's recorded ``after`` from the
        next receipt's ``before`` the moment any registry mutation lands
        between them (verified experimentally: it breaks byte replay on three
        pre-existing formation runs).  Registry state instead gets its own
        digest, :meth:`registry_state_digest`, which
        :mod:`memotron.epochs` composes with this hash to verify a
        rollback restores relationship state AND registry state byte-for-byte
        — without touching the pre-existing per-receipt chain invariant.

        ``object-digest`` binds the fact content without ever needing plaintext:
        for WS-12 crypto-shred scopes it is the stored ``fact_commitment`` (a keyed
        HMAC computed over the plaintext fact at write time, before sealing); for
        plaintext scopes it is ``sha256(stored fact string)``.  Either way the
        digest is a stored, write-time-stable string, so the hash is identical
        before and after key destruction (crypto-shred touches only the key table)
        and byte replay's live-store anchor survives the shred.  ``MENTIONS``
        structural edges are excluded so the state hash is over the reconstructable
        memory-relationship set that byte replay folds from receipts.
        Deterministic; O(scope); intentionally uncached.

        Seeks ``relationships_ctx_idx`` via the ``gen_scope_key`` generated column so
        the scan is bounded to the scope, then computes one subject lookup per row.

        A row-by-row bulk writer that needs a per-row before/after bracket would
        call this 2N times and pay O(N^2) queries; :meth:`scope_state_hash_tracker`
        gives such a writer the identical digests off a single scan.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("graph_state_hash requires a non-blank scope_key")
        return self._digest_state_tuples(self._scope_state_tuples(scope_key))

    def recompute_scope_state_tuples(self, scope_key: str) -> int:
        """No-op rebuild; returns the number of rows the hash is derived from.

        T0-7. The Postgres twin maintains ``relationship_state_tuples`` as a memo and
        rebuilds it here, because ``graph_state_hash`` there reads the memo. This backend has
        no such table -- ``_scope_state_tuples`` reads ``relationships`` on every call -- so
        there is nothing to rebuild and the hash is already derived from the rows.

        It exists so both engines answer the same contract and the audit plane can call it
        UNCONDITIONALLY. The alternative -- ``getattr(graph, "recompute_...", None)`` at the
        call site -- is an optional method nobody declares, which is the shape this repo keeps
        finding rotted. A real method with an honest no-op implementation is cheaper to
        reason about than an implicit capability check.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("recompute_scope_state_tuples requires a non-blank scope_key")
        return len(self._scope_state_tuples(scope_key))

    def _epoch_visible(self, relationship: GraphRelationship, *, ancestry: frozenset[str] | None) -> bool:
        """WS-26 T2/T6: is *relationship* visible under the current active epoch?

        ``ancestry`` is ``None`` for a scope that never registered an active
        epoch (:meth:`ensure_root_epoch` was never called) — every row is
        visible, byte-identical to pre-WS-26 behaviour.  Otherwise a row with
        no ``epoch_id`` property — or an empty one — is the implicit base layer
        (always visible); a row WITH one is visible only when that epoch is
        *ancestry* (the active epoch itself or one of its ancestors) — a sibling
        branch that was never adopted, or a branch rolled back away from, is
        excluded.

        The falsiness test is deliberate and load-bearing (T0-3, #120).  This line
        read ``epoch_id is None`` until 2026-09-02, which made ``epoch_id == ""``
        *hidden* here while :mod:`._epochs`, both Postgres copies and
        ``_STATE_HASH_EPOCH_SQL`` all treated it as the base layer — so SQLite
        disagreed with itself inside the pair composed into the rollback signature.
        One rule, five hand-written copies, one of them drifted by a token.  Do not
        "tighten" this back to an identity check without changing the other four.
        """
        if ancestry is None:
            return True
        epoch_id = relationship.properties.get("epoch_id")
        if not epoch_id:
            return True
        return epoch_id in ancestry

    def _scope_state_tuples(self, scope_key: str) -> dict[str, list[Any]]:
        """Canonical state tuple per non-``MENTIONS`` relationship uuid in *scope_key*.

        WS-26 T2: rows outside the scope's active-epoch ancestry are excluded
        (see :meth:`_epoch_visible`) — a rolled-back or never-adopted branch's
        versions do not contribute to the hash at all.
        """
        rows = self._connection.execute(
            "SELECT * FROM relationships WHERE gen_scope_key = ? ORDER BY uuid",
            (scope_key,),
        ).fetchall()
        ancestry = self._active_epoch_ancestry(scope_key)
        tuples: dict[str, list[Any]] = {}
        for row in rows:
            relationship = self._row_to_relationship(row)
            if relationship.type == "MENTIONS":
                continue
            if not self._epoch_visible(relationship, ancestry=ancestry):
                continue
            tuples[relationship.uuid] = self._relationship_state_tuple(relationship)
        return tuples

    def _relationship_state_tuple(self, relationship: GraphRelationship) -> list[Any]:
        """One row's contribution to :meth:`graph_state_hash` (see its docstring)."""
        props = relationship.properties
        subject = str(self.get_node(relationship.source_uuid).properties.get("name", ""))
        fact_commitment = props.get("fact_commitment")
        if isinstance(fact_commitment, str) and fact_commitment:
            # WS-12: keyed write-time commitment binds the content; stable across shred.
            object_digest = fact_commitment
        else:
            fact = str(props.get("fact", ""))
            object_digest = hashlib.sha256(fact.encode("utf-8")).hexdigest()
        return [
            relationship.uuid,
            relationship.type,
            subject,
            str(props.get("predicate", "")),
            object_digest,
            props.get("memory_type"),
            props.get("status"),
            props.get("active_in_context", True) is not False,
            props.get("rolled_up_by"),
            props.get("superseded_by_relationship_uuid"),
            self._datetime_to_text(relationship.valid_from),
            self._datetime_to_text(relationship.valid_to),
            int(props.get("observed_count", 1)),
            props.get("pinned") is True,
            canonical_visibility_agents(props.get("visibility_agents")),
            # WS-26 T1: derivation-DAG provenance.
            props.get("produced_by_run"),
            props.get("superseded_by_run"),
            props.get("pruned_by_run"),
        ]

    @staticmethod
    def _digest_state_tuples(tuples: dict[str, list[Any]]) -> str:
        """Digest the uuid-ordered tuple list — the single definition of the hash.

        Ordering is applied here rather than inherited from the ``ORDER BY uuid``
        scan so an incrementally maintained tuple map and a fresh scan can never
        disagree about canonical order.
        """
        ordered = [tuples[uuid] for uuid in sorted(tuples)]
        canonical = json.dumps(ordered, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def scope_state_hash_tracker(self, scope_key: str) -> ScopeStateHashTracker:
        """Incremental :meth:`graph_state_hash` for one scope under a bulk writer.

        ``graph_state_hash`` is intentionally uncached and O(scope): a full scan
        plus one subject lookup per row. That is the right cost for the ordinary
        call sites, which bracket a whole batch, cluster, or single mutation. A
        bulk writer that inserts N rows one at a time AND must record a per-row
        ``before -> after`` bracket (tenant migration, whose per-relationship
        receipts replay requires to form a contiguous chain) would call it 2N
        times — O(N^2) queries on exactly the large graphs migration exists for.

        A tracker pays the scan ONCE and then folds each newly written row into
        its in-memory tuple map, so N inserts cost one scan plus N single-row
        lookups. The digests are produced by the same
        :meth:`_relationship_state_tuple` / :meth:`_digest_state_tuples` pair
        ``graph_state_hash`` itself uses, so they are byte-identical to what the
        per-row calls produced — receipt semantics are unchanged, only the cost.

        Only valid while this tracker is the sole writer of *scope_key*: it folds
        in what it is told about and never re-reads the scope.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_state_hash_tracker requires a non-blank scope_key")
        return ScopeStateHashTracker(self, scope_key)

    def context_visible_relationships(
        self,
        *,
        scope: MemoryScope,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        """Index-backed read of the context-visible active relationships for a scope.

        Context-visible = status active AND not demoted (``active_in_context`` is not
        False).  This is the default working-context tier: it seeks the
        ``relationships_ctx_idx`` generated-column index, so the default profile read
        cost is decoupled from total store size — other scopes, non-active rows, and
        demoted members are never scanned.  Evidence, search, and historical (``as_of``)
        reads continue to read the full store via :meth:`relationships`.

        Note: the validity-window (``valid_from``/``valid_to``) check is intentionally
        left to the caller, which already applies it; this method narrows the candidate
        set to the bounded context-visible tier.

        WS-26 T6: additionally filtered to the scope's active-epoch ancestry
        (a cheap Python-side pass over the already-bounded result — it does
        not touch the seeked index), so adopt/rollback change what the
        default working-context tier returns, not only what
        :meth:`graph_state_hash` reports.
        """
        rows = self._connection.execute(
            _CONTEXT_VISIBLE_SELECT,
            (scope.key, RelationshipStatus.ACTIVE.value),
        ).fetchall()
        result = [self._row_to_relationship(row) for row in rows]
        if relationship_types is not None:
            result = [relationship for relationship in result if relationship.type in relationship_types]
        ancestry = self._active_epoch_ancestry(scope.key)
        return [r for r in result if self._epoch_visible(r, ancestry=ancestry)]

    def explain_context_visible_read(self, *, scope: MemoryScope) -> list[str]:
        """Return the SQLite query plan for the context-visible read (introspection).

        Used to verify that the default profile tier seeks ``relationships_ctx_idx``
        rather than scanning the full relationship store (the §101 scan-avoiding
        two-tier-retrieval property).
        """
        rows = self._connection.execute(
            "EXPLAIN QUERY PLAN " + _CONTEXT_VISIBLE_SELECT,
            (scope.key, RelationshipStatus.ACTIVE.value),
        ).fetchall()
        return [str(row["detail"]) for row in rows]

    def relationships_for_scope(self, scope_key: str) -> list[GraphRelationship]:
        """WS-5: Index-backed read of every memory relationship in one scope.

        Seeks the ``gen_scope_key`` prefix of ``relationships_ctx_idx`` and
        strips structural MENTIONS edges SQL-side.  Unlike
        :meth:`context_visible_relationships`, demoted and non-active rows ARE
        included — search is the audit/evidence surface, so status and
        validity filtering stay with the caller.

        WS-26 T6: filtered to the scope's active-epoch ancestry exactly like
        the other relationship reads (see :meth:`_epoch_visible`) — a branch
        that was never adopted, or one rolled back away from, does not leak
        into ordinary evidence/dedup/corroboration reads either.  A full,
        epoch-unfiltered audit of every version a scope ever held is the
        epoch bookkeeping surface (``memotron.epochs`` / ``graph_epochs``),
        not this method.
        """
        rows = self._connection.execute(
            """
            SELECT * FROM relationships
            WHERE gen_scope_key = ? AND type != 'MENTIONS'
            ORDER BY created_at, uuid
            """,
            (scope_key,),
        ).fetchall()
        ancestry = self._active_epoch_ancestry(scope_key)
        relationships = [self._row_to_relationship(row) for row in rows]
        return [r for r in relationships if self._epoch_visible(r, ancestry=ancestry)]

    def relationships_by_uuids(self, uuids: Sequence[str]) -> list[GraphRelationship]:
        """WS-5: Batch primary-key read of relationships.

        Missing uuids are silently absent from the result (callers resolve
        staleness through the supersession chain, not through errors here).
        An empty input returns an empty list.
        """
        materialized = tuple(dict.fromkeys(uuids))
        if not materialized:
            return []
        rows = self._connection.execute(
            self._in_clause_sql(
                "SELECT * FROM relationships WHERE uuid IN ({placeholders})",
                materialized,
            ),
            materialized,
        ).fetchall()
        return [self._row_to_relationship(row) for row in rows]

    def relationships_for_node_uuids(
        self,
        *,
        scope_key: str,
        node_uuids: Sequence[str],
    ) -> list[GraphRelationship]:
        """WS-5: Index-backed read of memory relationships incident to any node.

        The planner seeks ``gen_scope_key`` (bounding the read to one scope)
        with ``relationships_source_idx`` / ``relationships_target_idx``
        available when an endpoint probe is more selective; structural
        MENTIONS edges are stripped SQL-side.  This is the entity-hop
        frontier read for bounded retrieval expansion — cost never scales
        with the full multi-tenant store.

        T0-10: filtered to the scope's active-epoch ancestry exactly like
        :meth:`relationships_for_scope` (see :meth:`_epoch_visible`).  Without
        that filter this read is a hole in epoch isolation — retrieval's
        expansion stage re-admits rows the scoped read denies, so a
        ``redream_rollback`` reverts the audit-visible state while ``search()``
        keeps serving the rolled-back memory.  ``tests/test_epoch_read_parity.py``
        pins the two reads together.
        """
        materialized = tuple(dict.fromkeys(node_uuids))
        if not materialized:
            return []
        placeholders = ", ".join("?" for _ in materialized)
        rows = self._connection.execute(
            f"""
            SELECT * FROM relationships
            WHERE gen_scope_key = ?
              AND type != 'MENTIONS'
              AND (source_uuid IN ({placeholders}) OR target_uuid IN ({placeholders}))
            ORDER BY created_at, uuid
            """,
            (scope_key, *materialized, *materialized),
        ).fetchall()
        # T0-10: the entity-hop frontier must honour the SAME active-epoch ancestry that
        # relationships_for_scope applies, or rows outside the ancestry re-enter retrieval
        # through an expansion hop -- which is how a rolled-back epoch's memory keeps being
        # served after redream_rollback, and how migrated rows carrying a foreign epoch_id
        # surface from search() while profile() denies they exist.
        ancestry = self._active_epoch_ancestry(scope_key)
        relationships = [self._row_to_relationship(row) for row in rows]
        return [r for r in relationships if self._epoch_visible(r, ancestry=ancestry)]

    def find_active_truth_relationships(self, truth_key: str, *, scope_key: str) -> list[GraphRelationship]:
        normalized_truth_key = normalize_key(truth_key)
        return [
            relationship
            for relationship in self.relationships()
            if relationship.properties.get("truth_key") == normalized_truth_key
            and relationship.properties.get("scope_key") == scope_key
            and relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
        ]

    def find_active_relationships_by_truth_prefix(
        self, truth_prefix: str, *, scope_key: str
    ) -> list[GraphRelationship]:
        """Return all active relationships whose truth_prefix property matches.

        WS-1: The truth_prefix ``scope:subject:predicate`` is stored on every
        materialized memory relationship so we can broaden the candidate lookup
        for semantic dedup without parsing the truth_key at query time.
        """
        normalized_prefix = normalize_key(truth_prefix)
        return [
            relationship
            for relationship in self.relationships()
            if relationship.properties.get("truth_prefix") == normalized_prefix
            and relationship.properties.get("scope_key") == scope_key
            and relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
        ]

    def relationships_for_truth_prefix(self, truth_prefix: str, *, scope_key: str) -> list[GraphRelationship]:
        """Return ALL relationships on a truth slot regardless of status.

        WS-16 T12: the corroboration gate must count previously PARKED
        (pre-superseded, review-flagged) challenger rows on the same truth
        slot, so this mirrors :meth:`find_active_relationships_by_truth_prefix`
        without the status filter.  For a single-active slot the stored
        ``truth_prefix`` equals the slot's ``truth_key`` (same commitment
        family under crypto-shred), so this is the same-truth-key read.
        """
        normalized_prefix = normalize_key(truth_prefix)
        return [
            relationship
            for relationship in self.relationships()
            if relationship.properties.get("truth_prefix") == normalized_prefix
            and relationship.properties.get("scope_key") == scope_key
        ]

    def mark_relationship(
        self,
        relationship_uuid: str,
        *,
        status: RelationshipStatus,
        valid_to: datetime | None = None,
        properties: dict[str, Any] | None = None,
    ) -> GraphRelationship:
        row = self._connection.execute(
            "SELECT * FROM relationships WHERE uuid = ?",
            (relationship_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"relationship does not exist: {relationship_uuid}")
        relationship = self._row_to_relationship(row)
        relationship.properties["status"] = status.value
        if valid_to is not None:
            relationship.valid_to = valid_to
        if properties:
            relationship.properties.update(properties)
        self._save_relationship(relationship)
        return relationship

    def update_relationship(
        self,
        relationship_uuid: str,
        *,
        properties: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        clear_valid_to: bool = False,
    ) -> GraphRelationship:
        row = self._connection.execute(
            "SELECT * FROM relationships WHERE uuid = ?",
            (relationship_uuid,),
        ).fetchone()
        if row is None:
            raise ValueError(f"relationship does not exist: {relationship_uuid}")
        relationship = self._row_to_relationship(row)
        if properties:
            relationship.properties.update(properties)
        if valid_from is not None:
            relationship.valid_from = valid_from
        if clear_valid_to:
            relationship.valid_to = None
        elif valid_to is not None:
            relationship.valid_to = valid_to
        self._save_relationship(relationship)
        return relationship

    def nodes_for_scope(self, scope_key: str) -> list[GraphNode]:
        """All nodes whose properties carry this scope key.

        Index-backed via ``nodes_scope_idx`` (WS-5) — used by the erasure sweep
        and by retrieval seed-node resolution, so it must not scan the full
        multi-tenant node store.
        """
        rows = self._connection.execute(
            "SELECT * FROM nodes WHERE gen_scope_key = ? ORDER BY created_at, uuid",
            (scope_key,),
        ).fetchall()
        return [self._row_to_node(row) for row in rows]

    def similar_relationships(
        self,
        *,
        scope: MemoryScope,
        embedding: list[float],
        threshold: float = 0.0,
        limit: int | None = 20,
        relationship_types: set[str] | None = None,
        status: str | None = RelationshipStatus.ACTIVE.value,
    ) -> list[tuple[GraphRelationship, float]]:
        """Similarity search executed inside the backend, bounded to the scope.

        Parity twin of the Operational Store's vector-retrieval fallback: the
        candidate set is an indexed scope (+status) seek and the comparison
        happens here rather than in the caller.  Only plaintext stored
        embeddings participate — a sealed (WS-12) embedding never reaches
        engine-side comparison — and ordering (similarity DESC, uuid) and
        thresholding match the server engine exactly.
        """
        if not embedding:
            raise ValueError("similar_relationships requires a non-empty embedding")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        query_vector = [float(value) for value in embedding]
        sql = "SELECT * FROM relationships WHERE gen_scope_key = ?"
        parameters: list[object] = [scope.key]
        if status is not None:
            sql += " AND gen_status = ?"
            parameters.append(status)
        if relationship_types is not None:
            if not relationship_types:
                return []
            placeholders = ", ".join("?" for _ in relationship_types)
            sql += f" AND type IN ({placeholders})"
            parameters.extend(sorted(relationship_types))
        scored: list[tuple[GraphRelationship, float]] = []
        for row in self._connection.execute(sql, parameters).fetchall():
            relationship = self._row_to_relationship(row)
            stored = relationship.properties.get("embedding")
            if not isinstance(stored, list) or not stored:
                continue
            if len(stored) != len(query_vector):
                continue
            try:
                similarity = cosine_similarity(query_vector, [float(v) for v in stored])
            except (TypeError, ValueError):
                continue
            if similarity < float(threshold):
                continue
            scored.append((relationship, similarity))
        scored.sort(key=lambda item: (-item[1], item[0].uuid))
        return scored if limit is None else scored[:limit]

    def delete_nodes(self, uuids: Iterable[str]) -> int:
        """Idempotent node deletion by uuid (graph-plane erasure primitive)."""
        count = self._delete_by_ids("nodes", "uuid", uuids)
        self._commit()
        return count

    def delete_relationships(self, uuids: Iterable[str]) -> int:
        """Idempotent relationship deletion by uuid (graph-plane erasure primitive)."""
        count = self._delete_by_ids("relationships", "uuid", uuids)
        self._commit()
        return count

    def _save_node(self, node: GraphNode) -> None:
        graph_key = str(node.properties["graph_key"])
        self._connection.execute(
            """
            UPDATE nodes
            SET labels_json = ?, properties_json = ?, created_at = ?, valid_from = ?, valid_to = ?
            WHERE uuid = ?
            """,
            (
                self._json_dumps(list(node.labels), "node labels"),
                self._json_dumps(node.properties, "node properties"),
                self._datetime_to_text(node.created_at),
                self._datetime_to_text(node.valid_from),
                self._datetime_to_text(node.valid_to),
                node.uuid,
            ),
        )
        self._connection.execute(
            "UPDATE nodes SET graph_key = ? WHERE uuid = ?",
            (graph_key, node.uuid),
        )
        self._commit()

    def _save_relationship(self, relationship: GraphRelationship) -> None:
        self._connection.execute(
            """
            UPDATE relationships
            SET source_uuid = ?, target_uuid = ?, type = ?, properties_json = ?,
                created_at = ?, valid_from = ?, valid_to = ?
            WHERE uuid = ?
            """,
            (
                relationship.source_uuid,
                relationship.target_uuid,
                relationship.type,
                self._json_dumps(relationship.properties, "relationship properties"),
                self._datetime_to_text(relationship.created_at),
                self._datetime_to_text(relationship.valid_from),
                self._datetime_to_text(relationship.valid_to),
                relationship.uuid,
            ),
        )
        self._commit()

    def _node_exists(self, uuid: str) -> bool:
        row = self._connection.execute("SELECT 1 FROM nodes WHERE uuid = ?", (uuid,)).fetchone()
        return row is not None

    def _row_to_node(self, row: sqlite3.Row) -> GraphNode:
        return GraphNode(
            uuid=str(row["uuid"]),
            labels=tuple(json.loads(str(row["labels_json"]))),
            properties=dict(json.loads(str(row["properties_json"]))),
            created_at=self._datetime_from_text(str(row["created_at"])),
            valid_from=self._optional_datetime_from_text(row["valid_from"]),
            valid_to=self._optional_datetime_from_text(row["valid_to"]),
        )

    def _row_to_relationship(self, row: sqlite3.Row) -> GraphRelationship:
        return GraphRelationship(
            uuid=str(row["uuid"]),
            source_uuid=str(row["source_uuid"]),
            target_uuid=str(row["target_uuid"]),
            type=str(row["type"]),
            properties=dict(json.loads(str(row["properties_json"]))),
            created_at=self._datetime_from_text(str(row["created_at"])),
            valid_from=self._optional_datetime_from_text(row["valid_from"]),
            valid_to=self._optional_datetime_from_text(row["valid_to"]),
        )
