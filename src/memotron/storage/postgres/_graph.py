"""Memory Graph plane on Postgres (the DW-001 pgvector contingency path).

Four things differ from the SQLite implementation, and each is a scope item on the
persistence issue rather than an incidental porting choice:

1. **Writes are race-free.**  ``upsert_node`` is an ``INSERT ... ON CONFLICT DO
   NOTHING`` probe followed, only on conflict, by a ``SELECT ... FOR UPDATE``
   merge inside the same transaction.  The property-merge and validity-window
   semantics are too conditional to express as a single ``DO UPDATE``, so the row
   lock is what removes the lost update; relationship mutations use the same
   pattern.  Every row also carries a ``version`` counter, bumped on write, so
   callers that want optimistic checks have one.

2. **Filters are pushed into SQL.**  Scope, status, truth-key, and truth-prefix
   predicates are indexed jsonb expressions, not Python list comprehensions over
   a full-store read.

3. **The state hash is incremental.**  Each memory relationship keeps its own
   canonical tuple row, refreshed when that relationship (or its subject node's
   name) changes.  ``graph_state_hash`` is then a single indexed aggregate with a
   memoised result, instead of a whole-scope scan plus one subject lookup per row
   performed twice per receipted change.  The hash *value* is unchanged — see
   :meth:`_state_tuple_json` — so existing receipt chains and replay proofs stay
   valid.

4. **Embeddings live in a queryable column.**  Vector comparison happens in the
   database (pgvector when installed, ``dw_cosine_similarity`` otherwise), never
   in Python.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any

from memotron.models import (
    GraphNode,
    GraphRelationship,
    MemoryScope,
    RelationshipStatus,
    ScopeKind,
)
from memotron.storage._shared import canonical_visibility_agents
from memotron.storage.base import normalize_key
from memotron.storage.postgres._common import as_json
from memotron.storage.postgres._engine import (
    datetime_to_text,
    optional_datetime_from_text,
)

# Structural edges excluded from the state hash, matching the SQLite backend: the
# hash covers the reconstructable memory-relationship set byte replay folds from
# receipts.
_STATE_HASH_EXCLUDED_TYPES = frozenset({"MENTIONS"})


class PostgresScopeStateHashTracker:
    """Incrementally maintained ``graph_state_hash`` for one scope.

    The interface twin of ``memotron.storage.sqlite._graph.ScopeStateHashTracker``:
    ``digest`` is the hash the store would return for the scope right now, and
    ``record`` folds a just-written relationship in and returns the new hash.
    Because the Postgres backend maintains the scope hash incrementally in the
    database, both simply read ``graph_state_hash`` — the relationship's tuple
    is already persisted (add_relationship wrote it) by the time ``record`` is
    called, and the read reflects uncommitted writes inside the caller's
    transaction.
    """

    def __init__(self, store: Any, scope_key: str) -> None:
        self._store = store
        self.scope_key = scope_key

    @property
    def digest(self) -> str:
        return self._store.graph_state_hash(self.scope_key)

    def record(self, relationship: GraphRelationship) -> str:
        # MENTIONS edges are excluded from the hash on both engines; recording
        # one is a no-op, and graph_state_hash already excludes them.
        return self._store.graph_state_hash(self.scope_key)


# ``COLLATE "C"`` forces byte ordering, which is what SQLite's BINARY collation
# gives.  Without it the aggregate order (and therefore the hash) would depend on
# the database's lc_collate.
_STATE_HASH_SQL = """
SELECT encode(
           sha256(
               convert_to(
                   '[' || COALESCE(
                       string_agg(tuple_json, ',' ORDER BY relationship_uuid COLLATE "C"),
                       ''
                   ) || ']',
                   'UTF8'
               )
           ),
           'hex'
       ) AS state_hash
FROM relationship_state_tuples
WHERE scope_key = %s
"""

# WS-26 T2/T9: the epoch-ancestry-filtered variant.  When a scope has ever
# registered an active epoch, the hash folds over only rows whose ``epoch_id``
# is absent (the implicit base layer) or inside the active epoch's ancestry — a
# rolled-back or never-adopted branch's rows are excluded, exactly as
# ``SQLiteStorageBackend._scope_state_tuples`` excludes them.  Byte-identical to
# the unfiltered form for a scope whose rows all carry no ``epoch_id``.
_STATE_HASH_EPOCH_SQL = """
SELECT encode(
           sha256(
               convert_to(
                   '[' || COALESCE(
                       string_agg(t.tuple_json, ',' ORDER BY t.relationship_uuid COLLATE "C"),
                       ''
                   ) || ']',
                   'UTF8'
               )
           ),
           'hex'
       ) AS state_hash
FROM relationship_state_tuples t
JOIN relationships r ON r.uuid = t.relationship_uuid
WHERE t.scope_key = %s
  AND (
      r.properties->>'epoch_id' IS NULL
      OR r.properties->>'epoch_id' = ''
      OR r.properties->>'epoch_id' = ANY(%s)
  )
"""

_CONTEXT_VISIBLE_SELECT = """
SELECT * FROM relationships
WHERE properties->>'scope_key' = %s
  AND properties->>'status' = %s
  AND (properties->'active_in_context') IS DISTINCT FROM 'false'::jsonb
ORDER BY created_at, uuid
"""


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.postgres._protocol import ComposedPostgresBackend

    _Base = ComposedPostgresBackend
else:
    _Base = object


class GraphPlaneMixin(_Base):
    """Implements :class:`~memotron.storage.base.MemoryGraphStorage`."""

    # ------------------------------------------------------------------ nodes

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
        with self._engine.transaction():
            node = GraphNode(
                labels=tuple(dict.fromkeys(labels)),
                properties={**properties, "graph_key": normalized_key},
                valid_from=valid_from,
                valid_to=valid_to,
            )
            # Attempt the create first; the unique index on graph_key decides the
            # race, so two replicas inserting the same key cannot both win.
            created = self._engine.fetchone(
                """
                INSERT INTO nodes (uuid, graph_key, labels, properties, created_at, valid_from, valid_to)
                VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
                ON CONFLICT (graph_key) DO NOTHING
                RETURNING *
                """,
                (
                    node.uuid,
                    normalized_key,
                    json.dumps(list(node.labels), sort_keys=True),
                    json.dumps(node.properties, sort_keys=True),
                    datetime_to_text(node.created_at),
                    datetime_to_text(node.valid_from),
                    datetime_to_text(node.valid_to),
                ),
            )
            if created is not None:
                return self._row_to_node(created), True

            # Lost the race, or the node already existed: take the row lock so the
            # read-modify-write below cannot interleave with another writer.
            #
            # ``FOR NO KEY UPDATE``, deliberately, not ``FOR UPDATE``.  Every
            # ``INSERT INTO relationships`` takes ``FOR KEY SHARE`` on the node
            # rows its foreign keys point at, and ``FOR UPDATE`` conflicts with
            # that: two replicas each holding one subject node and each adding a
            # relationship pointing at the other's subject deadlock, which is a
            # cycle over two ordinary fact writes that never touch the same row.
            # The merge below changes no key column — the row is looked up by
            # ``graph_key`` and rewrites the same value — so the weaker mode is
            # the correct one.  It still conflicts with itself, so the
            # read-modify-write stays serialised against other upserts.
            locked = self._engine.fetchone(
                "SELECT * FROM nodes WHERE graph_key = %s FOR NO KEY UPDATE",
                (normalized_key,),
            )
            if locked is None:  # pragma: no cover - deleted between the two statements
                raise ValueError(f"node vanished during upsert: {normalized_key}")
            existing = self._row_to_node(locked)

            from memotron.crypto import is_sealed_content

            merge = properties
            # Never re-seal an already-sealed name: sealing is nonce-random and the
            # stored name feeds the state hash, so a rewrite would move the hash
            # outside any receipted mutation.
            if is_sealed_content(properties.get("name")) and is_sealed_content(existing.properties.get("name")):
                merge = {k: v for k, v in properties.items() if k != "name"}

            previous_name = existing.properties.get("name")
            existing.properties.update(merge)
            # The row was found *by* this key; a merged payload must not be able
            # to re-key it out from under the lookup (and re-keying would make
            # the lock above a key update, which the weaker lock mode forbids).
            existing.properties["graph_key"] = normalized_key
            if valid_from is not None:
                existing.valid_from = (
                    valid_from if existing.valid_from is None else min(existing.valid_from, valid_from)
                )
            if valid_to is None:
                existing.valid_to = None
            elif existing.valid_to is not None:
                existing.valid_to = max(existing.valid_to, valid_to)

            # Only an actual *change* of name invalidates the outgoing tuples.
            # Testing membership alone made the common re-assertion of an
            # unchanged name (every synchronous fact write upserts its subject)
            # rewrite and re-lock every tuple that node owns, which is both
            # wasted work and the widest lock footprint in the plane.
            name_changed = "name" in merge and existing.properties.get("name") != previous_name
            self._save_node(existing)
            if name_changed:
                # The subject name is part of every outgoing relationship's state
                # tuple, so those tuples are now stale.
                self._refresh_state_tuples_for_source(existing.uuid)
            return existing, False

    def get_node(self, uuid: str) -> GraphNode:
        row = self._engine.fetchone("SELECT * FROM nodes WHERE uuid = %s", (uuid,))
        if row is None:
            raise ValueError(f"node does not exist: {uuid}")
        return self._row_to_node(row)

    def nodes(self) -> list[GraphNode]:
        rows = self._engine.fetchall("SELECT * FROM nodes ORDER BY created_at, uuid")
        return [self._row_to_node(row) for row in rows]

    def nodes_for_scope(self, scope_key: str) -> list[GraphNode]:
        rows = self._engine.fetchall(
            "SELECT * FROM nodes WHERE properties->>'scope_key' = %s ORDER BY created_at, uuid",
            (scope_key,),
        )
        return [self._row_to_node(row) for row in rows]

    def delete_nodes(self, uuids: Iterable[str]) -> int:
        ordered = self._ordered_ids(uuids)
        if not ordered:
            return 0
        return self._engine.execute("DELETE FROM nodes WHERE uuid = ANY(%s)", (list(ordered),))

    def _save_node(self, node: GraphNode) -> None:
        self._engine.execute(
            """
            UPDATE nodes
            SET labels = %s::jsonb,
                properties = %s::jsonb,
                graph_key = %s,
                created_at = %s,
                valid_from = %s,
                valid_to = %s,
                version = version + 1
            WHERE uuid = %s
            """,
            (
                json.dumps(list(node.labels), sort_keys=True),
                json.dumps(node.properties, sort_keys=True),
                str(node.properties["graph_key"]),
                datetime_to_text(node.created_at),
                datetime_to_text(node.valid_from),
                datetime_to_text(node.valid_to),
                node.uuid,
            ),
        )

    # ------------------------------------------------------------------ relationships

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
        with self._engine.transaction():
            # Explicit existence checks keep the interface's ValueError contract;
            # the foreign keys are the backstop, not the error surface.
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
            self._engine.execute(
                """
                INSERT INTO relationships (
                    uuid, source_uuid, target_uuid, type, properties, created_at, valid_from, valid_to
                )
                VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (
                    relationship.uuid,
                    relationship.source_uuid,
                    relationship.target_uuid,
                    relationship.type,
                    json.dumps(relationship.properties, sort_keys=True),
                    datetime_to_text(relationship.created_at),
                    datetime_to_text(relationship.valid_from),
                    datetime_to_text(relationship.valid_to),
                ),
            )
            self._sync_relationship_derived(relationship)
            return relationship

    def get_relationship(self, uuid: str) -> GraphRelationship:
        row = self._engine.fetchone("SELECT * FROM relationships WHERE uuid = %s", (uuid,))
        if row is None:
            raise ValueError(f"relationship does not exist: {uuid}")
        return self._row_to_relationship(row)

    def relationships(self) -> list[GraphRelationship]:
        rows = self._engine.fetchall("SELECT * FROM relationships ORDER BY created_at, uuid")
        return [self._row_to_relationship(row) for row in rows]

    def relationships_for_scope(self, scope_key: str) -> list[GraphRelationship]:
        """Scope-bounded relationship read; seeks ``relationships_scope_idx``.

        WS-26 T6: filtered to the scope's active-epoch ancestry, so a
        rolled-back or never-adopted branch's rows do not surface — matching
        ``SQLiteStorageBackend.relationships_for_scope``.
        """
        rows = self._engine.fetchall(
            """
            SELECT * FROM relationships
            WHERE properties->>'scope_key' = %s AND type <> 'MENTIONS'
            ORDER BY created_at, uuid
            """,
            (scope_key,),
        )
        ancestry = self._active_epoch_ancestry(scope_key)
        return [
            relationship
            for row in rows
            if self._epoch_visible((relationship := self._row_to_relationship(row)), ancestry=ancestry)
        ]

    def active_relationships(
        self,
        *,
        scope: MemoryScope | None = None,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        # WS-26 T6: scoped to the active epoch's ancestry — precomputed when
        # *scope* narrows the call, resolved per-relationship for the fleet-wide
        # (scope=None) scan, exactly as the SQLite backend does.
        clauses = ["properties->>'status' = %s"]
        params: list[Any] = [RelationshipStatus.ACTIVE.value]
        if scope is not None:
            clauses.append("properties->>'scope_key' = %s")
            params.append(scope.key)
        if relationship_types is not None:
            if not relationship_types:
                return []
            clauses.append("type = ANY(%s)")
            params.append(sorted(relationship_types))
        rows = self._engine.fetchall(
            f"SELECT * FROM relationships WHERE {' AND '.join(clauses)} ORDER BY created_at, uuid",
            params,
        )
        scoped_ancestry = self._active_epoch_ancestry(scope.key) if scope is not None else None
        ancestry_cache: dict[str, frozenset[str] | None] = {}
        result: list[GraphRelationship] = []
        for row in rows:
            relationship = self._row_to_relationship(row)
            if scope is not None:
                row_ancestry = scoped_ancestry
            else:
                row_scope_key = str(relationship.properties.get("scope_key") or "")
                if row_scope_key not in ancestry_cache:
                    ancestry_cache[row_scope_key] = (
                        self._active_epoch_ancestry(row_scope_key) if row_scope_key else None
                    )
                row_ancestry = ancestry_cache[row_scope_key]
            if self._epoch_visible(relationship, ancestry=row_ancestry):
                result.append(relationship)
        return result

    def context_visible_relationships(
        self,
        *,
        scope: MemoryScope,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        """Index-backed read of the context-visible tier (seeks relationships_ctx_idx).

        WS-26 T6: filtered to the scope's active-epoch ancestry, like the other
        relationship reads.
        """
        rows = self._engine.fetchall(_CONTEXT_VISIBLE_SELECT, (scope.key, RelationshipStatus.ACTIVE.value))
        ancestry = self._active_epoch_ancestry(scope.key)
        result = [
            relationship
            for row in rows
            if self._epoch_visible((relationship := self._row_to_relationship(row)), ancestry=ancestry)
        ]
        if relationship_types is not None:
            result = [item for item in result if item.type in relationship_types]
        return result

    def explain_context_visible_read(self, *, scope: MemoryScope) -> list[str]:
        rows = self._engine.fetchall("EXPLAIN " + _CONTEXT_VISIBLE_SELECT, (scope.key, RelationshipStatus.ACTIVE.value))
        return [str(next(iter(row.values()))) for row in rows]

    def find_active_truth_relationships(self, truth_key: str, *, scope_key: str) -> list[GraphRelationship]:
        """The duplicate-check probe: indexed, so it no longer scans the store.

        The ``scope_key`` term is a recheck against ``relationships_truth_key_idx``, which
        still drives the scan -- so no plan regression. A composite
        ``((properties->>'scope_key'),(properties->>'truth_key'))`` index would let it drive
        directly; worth adding, not required for correctness.
        """
        rows = self._engine.fetchall(
            """
            SELECT * FROM relationships
            WHERE properties->>'truth_key' = %s
              AND properties->>'scope_key' = %s
              AND properties->>'status' = %s
            ORDER BY created_at, uuid
            """,
            (normalize_key(truth_key), scope_key, RelationshipStatus.ACTIVE.value),
        )
        return [self._row_to_relationship(row) for row in rows]

    def find_active_relationships_by_truth_prefix(
        self, truth_prefix: str, *, scope_key: str
    ) -> list[GraphRelationship]:
        rows = self._engine.fetchall(
            """
            SELECT * FROM relationships
            WHERE properties->>'truth_prefix' = %s
              AND properties->>'scope_key' = %s
              AND properties->>'status' = %s
            ORDER BY created_at, uuid
            """,
            (normalize_key(truth_prefix), scope_key, RelationshipStatus.ACTIVE.value),
        )
        return [self._row_to_relationship(row) for row in rows]

    def relationships_by_uuids(self, uuids: Sequence[str]) -> list[GraphRelationship]:
        """WS-5: batch primary-key read; missing uuids are silently absent."""
        materialized = list(dict.fromkeys(uuids))
        if not materialized:
            return []
        rows = self._engine.fetchall(
            "SELECT * FROM relationships WHERE uuid = ANY(%s) ORDER BY created_at, uuid",
            (materialized,),
        )
        return [self._row_to_relationship(row) for row in rows]

    def relationships_for_node_uuids(self, *, scope_key: str, node_uuids: Sequence[str]) -> list[GraphRelationship]:
        """WS-5: memory relationships incident to any node, one scope, MENTIONS stripped.

        T0-10: filtered to the scope's active-epoch ancestry, exactly like
        :meth:`relationships_for_scope`. Retrieval's stage 1 uses the scoped read and its
        entity-hop expansion uses this one, so an unfiltered frontier lets a row outside the
        ancestry re-enter retrieval through an expansion hop -- ``redream_rollback`` reverts
        the audit-visible state while ``search()`` keeps serving the rolled-back memory.

        THE SQLITE TWIN WAS FIXED AND THIS WAS NOT. The register said so at the time
        ("Postgres needs the same change"), and `tests/test_epoch_read_parity.py` -- written
        to pin precisely this invariant -- was hardcoded to SQLite, so it could not see the
        engine the defect was live on. Third instance of that shape after T0-4 and T0-7.
        The test is now parametrised over both engines and was RED here before this change.
        """
        materialized = list(dict.fromkeys(node_uuids))
        if not materialized:
            return []
        rows = self._engine.fetchall(
            """
            SELECT * FROM relationships
            WHERE properties->>'scope_key' = %s
              AND type != 'MENTIONS'
              AND (source_uuid = ANY(%s) OR target_uuid = ANY(%s))
            ORDER BY created_at, uuid
            """,
            (scope_key, materialized, materialized),
        )
        ancestry = self._active_epoch_ancestry(scope_key)
        return [
            relationship
            for row in rows
            if self._epoch_visible((relationship := self._row_to_relationship(row)), ancestry=ancestry)
        ]

    def relationships_for_truth_prefix(self, truth_prefix: str, *, scope_key: str) -> list[GraphRelationship]:
        """WS-16 T12: ALL relationships on ONE scope's truth slot, regardless of status."""
        rows = self._engine.fetchall(
            """
            SELECT * FROM relationships
            WHERE properties->>'truth_prefix' = %s
              AND properties->>'scope_key' = %s
            ORDER BY created_at, uuid
            """,
            (normalize_key(truth_prefix), scope_key),
        )
        return [self._row_to_relationship(row) for row in rows]

    def scope_state_hash_tracker(self, scope_key: str) -> PostgresScopeStateHashTracker:
        """Incremental ``graph_state_hash`` handle for one scope under a bulk writer.

        The SQLite tracker keeps an in-memory tuple set to avoid an O(N^2)
        whole-scope rescan.  Postgres already maintains the scope hash
        incrementally in the database (``relationship_state_tuples`` + the
        ``scope_state_hash`` memo, recomputed only when dirty), so the tracker
        simply reads back ``graph_state_hash`` — which reflects each
        just-written relationship's tuple, including uncommitted writes inside
        the caller's transaction.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_state_hash_tracker requires a non-blank scope_key")
        return PostgresScopeStateHashTracker(self, scope_key)

    def mark_relationship(
        self,
        relationship_uuid: str,
        *,
        status: RelationshipStatus,
        valid_to: datetime | None = None,
        properties: dict[str, Any] | None = None,
    ) -> GraphRelationship:
        with self._engine.transaction():
            relationship = self._lock_relationship(relationship_uuid)
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
        with self._engine.transaction():
            relationship = self._lock_relationship(relationship_uuid)
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

    def delete_relationships(self, uuids: Iterable[str]) -> int:
        ordered = self._ordered_ids(uuids)
        if not ordered:
            return 0
        with self._engine.transaction():
            scopes = self._engine.fetchall(
                "SELECT DISTINCT scope_key FROM relationship_state_tuples WHERE relationship_uuid = ANY(%s)",
                (list(ordered),),
            )
            # relationship_state_tuples / relationship_embeddings cascade.
            removed = self._engine.execute("DELETE FROM relationships WHERE uuid = ANY(%s)", (list(ordered),))
            for row in scopes:
                self._mark_scope_dirty(str(row["scope_key"]))
            return removed

    def _lock_relationship(self, relationship_uuid: str) -> GraphRelationship:
        # ``FOR NO KEY UPDATE`` for the same reason as ``upsert_node``:
        # ``relationship_state_tuples``, ``relationship_embeddings`` and
        # ``memory_use_events`` all carry a foreign key to ``relationships(uuid)``
        # and take ``FOR KEY SHARE`` on this row when they are written.  The
        # mutation below never changes ``uuid``, so blocking those writers (and
        # being blocked by them) would buy nothing but deadlocks.
        row = self._engine.fetchone(
            "SELECT * FROM relationships WHERE uuid = %s FOR NO KEY UPDATE",
            (relationship_uuid,),
        )
        if row is None:
            raise ValueError(f"relationship does not exist: {relationship_uuid}")
        return self._row_to_relationship(row)

    def _save_relationship(self, relationship: GraphRelationship) -> None:
        self._engine.execute(
            """
            UPDATE relationships
            SET source_uuid = %s,
                target_uuid = %s,
                type = %s,
                properties = %s::jsonb,
                created_at = %s,
                valid_from = %s,
                valid_to = %s,
                version = version + 1
            WHERE uuid = %s
            """,
            (
                relationship.source_uuid,
                relationship.target_uuid,
                relationship.type,
                json.dumps(relationship.properties, sort_keys=True),
                datetime_to_text(relationship.created_at),
                datetime_to_text(relationship.valid_from),
                datetime_to_text(relationship.valid_to),
                relationship.uuid,
            ),
        )
        self._sync_relationship_derived(relationship)

    def _node_exists(self, uuid: str) -> bool:
        return bool(self._engine.fetchvalue("SELECT EXISTS (SELECT 1 FROM nodes WHERE uuid = %s)", (uuid,)))

    # ------------------------------------------------------------------ scoped reads

    def scopes(self) -> list[MemoryScope]:
        """Distinct scopes with at least one relationship, in first-seen order."""
        rows = self._engine.fetchall(
            """
            SELECT properties->>'scope_kind' AS kind,
                   properties->>'scope_id'   AS scope_id,
                   MIN(created_at)           AS first_seen,
                   MIN(uuid)                 AS first_uuid
            FROM relationships
            WHERE properties->>'scope_kind' IS NOT NULL
              AND properties->>'scope_id' IS NOT NULL
            GROUP BY 1, 2
            ORDER BY first_seen, first_uuid
            """
        )
        result: list[MemoryScope] = []
        seen: set[str] = set()
        for row in rows:
            kind = str(row["kind"])
            scope_id = str(row["scope_id"])
            key = f"{kind}:{scope_id}"
            if key in seen:
                continue
            seen.add(key)
            result.append(MemoryScope(kind=ScopeKind(kind), scope_id=scope_id))
        return result

    @staticmethod
    def _epoch_visible(relationship: GraphRelationship, *, ancestry: frozenset[str] | None) -> bool:
        """WS-26 T2/T6: is *relationship* visible under the current active epoch?

        Byte-identical semantics to the SQLite backend: ``ancestry is None``
        (the scope never registered an epoch) makes every row visible; a row
        with no ``epoch_id`` is the implicit base layer (always visible); a row
        with one is visible only when that epoch is in *ancestry*.
        """
        if ancestry is None:
            return True
        epoch_id = relationship.properties.get("epoch_id")
        if not epoch_id:
            return True
        return epoch_id in ancestry

    def _compute_state_hash(self, scope_key: str) -> str:
        """The scope's state hash, epoch-ancestry filtered when the scope has one."""
        ancestry = self._active_epoch_ancestry(scope_key)
        if ancestry is None:
            return str(self._engine.fetchvalue(_STATE_HASH_SQL, (scope_key,)))
        return str(self._engine.fetchvalue(_STATE_HASH_EPOCH_SQL, (scope_key, sorted(ancestry))))

    def graph_state_hash(self, scope_key: str) -> str:
        """Memoised, incrementally maintained scope state hash.

        Identical in value to the SQLite backend's full recompute; the parity
        suite asserts that byte-for-byte.  Cost per call is one indexed aggregate
        on a dirty scope and nothing at all on a clean one, so the
        receipt-and-state-hash bracket no longer pays two whole-scope passes with
        a subject lookup per row.

        **The write-back into the memo is conditional, and must stay that way.**
        The aggregate reads a committed snapshot; a writer on another replica can
        commit both its ``relationship_state_tuples`` row and its ``dirty`` mark
        in the window between that read and this write-back.  Clearing ``dirty``
        unconditionally would then memoise a hash that omits the writer's
        relationship *and mark it clean*, so every later call on that scope
        returns the stale value until something else dirties it — the receipt
        bracket would commit to a state hash the graph never had, and replay
        would not verify.  The publish is therefore guarded by the row version
        (``xmin``) observed before the aggregate ran: if anything touched the row
        in between, the memo is left alone, the scope stays dirty, and the next
        call recomputes.  The value returned to *this* caller is correct either
        way — it is the hash of the snapshot the aggregate actually read, which
        is the same guarantee the SQLite full recompute gives.

        **The memo is only published by a call that owns its transaction.**
        Publishing writes the shared per-scope row, and inside a caller's
        transaction — a receipt bracket, say — that row lock would then be held
        while the caller went on to lock relationships, which is the inverted
        lock order that produces deadlocks (see
        :meth:`PostgresEngine.defer_to_commit`).  A nested call therefore
        computes and returns without writing; the memo stays dirty and the next
        standalone read refreshes it.  Nothing is lost by this: a bracket dirties
        the scope between its two hash calls anyway, so the publish it used to
        make was invalidated microseconds later.
        """
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("graph_state_hash requires a non-blank scope_key")
        owns_transaction = not self._engine.in_transaction
        with self._engine.transaction():
            if self._engine.has_pending_commit_key(self._dirty_key(scope_key)):
                # This transaction has already invalidated the scope; its own
                # uncommitted tuples are in its snapshot but the memo row does
                # not know that yet.  Recompute rather than trust the memo.
                return self._compute_state_hash(scope_key)
            cached = self._engine.fetchone(
                "SELECT state_hash, dirty, xmin::text AS row_version FROM scope_state_hash WHERE scope_key = %s",
                (scope_key,),
            )
            if cached is not None and not cached["dirty"]:
                return str(cached["state_hash"])
            state_hash = self._compute_state_hash(scope_key)
            if not owns_transaction:
                return state_hash
            if cached is None:
                # No memo row yet.  Publish only if no writer created one while
                # the aggregate ran; a writer's row carries dirty = true and must
                # win, because its tuple may post-date our snapshot.
                self._engine.execute(
                    """
                    INSERT INTO scope_state_hash (scope_key, state_hash, dirty, updated_at)
                    VALUES (%s, %s, false, now())
                    ON CONFLICT (scope_key) DO NOTHING
                    """,
                    (scope_key, state_hash),
                )
            else:
                self._engine.execute(
                    """
                    UPDATE scope_state_hash
                    SET state_hash = %s, dirty = false, updated_at = now()
                    WHERE scope_key = %s AND xmin::text = %s
                    """,
                    (state_hash, scope_key, str(cached["row_version"])),
                )
            return state_hash

    # ------------------------------------------------------------------ derived state

    def _sync_relationship_derived(self, relationship: GraphRelationship) -> None:
        """Refresh the state tuple and embedding row for one relationship.

        Both the scope the relationship is now in *and* the scope it just left
        are invalidated.  ``update_relationship`` can rewrite ``scope_key`` (or
        drop it), and a memoised hash for the abandoned scope would otherwise
        stay clean while still committing to a row that is no longer in that
        scope — SQLite recomputes from the current key and reports the move
        immediately, so this is also what keeps the two engines in parity.  The
        previous key is read in the same statement as the write, so the extra
        correctness costs no extra round trip.
        """
        scope_key = relationship.properties.get("scope_key")
        if not isinstance(scope_key, str):
            scope_key = ""
        if not scope_key or relationship.type in _STATE_HASH_EXCLUDED_TYPES:
            removed = self._engine.fetchone(
                "DELETE FROM relationship_state_tuples WHERE relationship_uuid = %s RETURNING scope_key",
                (relationship.uuid,),
            )
            previous_scope = None if removed is None else str(removed["scope_key"])
        else:
            # The data-modifying CTE always runs to completion, and ``previous``
            # is evaluated against the pre-insert snapshot, so this reads the old
            # scope key and writes the new tuple in one statement.
            row = self._engine.fetchone(
                """
                WITH previous AS (
                    SELECT scope_key FROM relationship_state_tuples
                    WHERE relationship_uuid = %s
                ), upserted AS (
                    INSERT INTO relationship_state_tuples (relationship_uuid, scope_key, tuple_json)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (relationship_uuid) DO UPDATE
                    SET scope_key = excluded.scope_key, tuple_json = excluded.tuple_json
                    RETURNING relationship_uuid
                )
                SELECT scope_key FROM previous
                """,
                (
                    relationship.uuid,
                    relationship.uuid,
                    scope_key,
                    self._state_tuple_json(relationship),
                ),
            )
            previous_scope = None if row is None else str(row["scope_key"])
        if previous_scope is not None and previous_scope != scope_key:
            self._mark_scope_dirty(previous_scope)
        if not scope_key:
            # The relationship no longer names a scope, so its embedding must not
            # stay searchable under the one it left.
            self._engine.execute(
                "DELETE FROM relationship_embeddings WHERE relationship_uuid = %s",
                (relationship.uuid,),
            )
            return
        self._sync_embedding(relationship, scope_key)
        self._mark_scope_dirty(scope_key)

    def _refresh_state_tuples_for_source(self, node_uuid: str) -> None:
        """Rebuild tuples for relationships whose subject name just changed.

        ``FOR UPDATE ... ORDER BY uuid`` is load-bearing twice over.  Without the
        lock this read sees each relationship as it was before any concurrent
        mutation committed, and the tuple written from it lands *after* that
        mutation's own tuple (the tuple row serialises them), so the state hash
        would be computed from a relationship's superseded properties — a lost
        update in the derived state, invisible until a replay failed.  Without
        the ordering this is the one place in the plane that takes many
        relationship locks at once, and taking them in an order no other writer
        uses is what turns two honest writers into ``deadlock_detected``.
        """
        rows = self._engine.fetchall(
            "SELECT * FROM relationships WHERE source_uuid = %s ORDER BY uuid FOR NO KEY UPDATE",
            (node_uuid,),
        )
        for row in rows:
            relationship = self._row_to_relationship(row)
            scope_key = relationship.properties.get("scope_key")
            if not isinstance(scope_key, str) or not scope_key:
                continue
            if relationship.type in _STATE_HASH_EXCLUDED_TYPES:
                continue
            self._engine.execute(
                """
                INSERT INTO relationship_state_tuples (relationship_uuid, scope_key, tuple_json)
                VALUES (%s, %s, %s)
                ON CONFLICT (relationship_uuid) DO UPDATE
                SET scope_key = excluded.scope_key, tuple_json = excluded.tuple_json
                """,
                (relationship.uuid, scope_key, self._state_tuple_json(relationship)),
            )
            self._mark_scope_dirty(scope_key)

    @staticmethod
    def _dirty_key(scope_key: str) -> tuple[str, str]:
        return ("scope_state_hash_dirty", scope_key)

    def _mark_scope_dirty(self, scope_key: str) -> None:
        """Invalidate the scope's state-hash memo — at ``COMMIT``, not inline.

        Every write to a scope has to touch this one row, which makes it the
        hottest lock in the plane.  Taking it *inline* meant a transaction that
        writes two relationships in a scope locked it while working on the first
        and held it to ``COMMIT``, then went on to wait for the second
        relationship's row — while a concurrent single-relationship writer held
        that row and waited for this same scope row.  Inverted order, cycle,
        ``deadlock_detected``; a two-replica mix reproduced it within seconds.

        Deferring the write to just before ``COMMIT`` means the row is locked
        only when the transaction is waiting on nothing else, so it cannot be
        the middle of a cycle.  The mark is still part of the same transaction,
        so it remains atomic with the tuple writes that made it necessary, and
        registering the same scope repeatedly collapses to one write.
        """
        self._engine.defer_to_commit(
            self._dirty_key(scope_key),
            lambda: self._engine.execute(
                """
                INSERT INTO scope_state_hash (scope_key, state_hash, dirty, updated_at)
                VALUES (%s, '', true, now())
                ON CONFLICT (scope_key) DO UPDATE SET dirty = true, updated_at = now()
                """,
                (scope_key,),
            ),
        )

    def _state_tuple_json(self, relationship: GraphRelationship) -> str:
        """One row's canonical tuple, encoded exactly as the SQLite backend does.

        The full canonical document is ``'[' + ','.join(tuples) + ']'``, which is
        what ``json.dumps(list_of_lists, separators=(",", ":"))`` produces, so
        aggregating these strings in SQL and hashing the result reproduces the
        SQLite hash byte-for-byte.  ``sort_keys`` is irrelevant for arrays but is
        passed for symmetry with the original call.
        """
        props = relationship.properties
        subject = str(self.get_node(relationship.source_uuid).properties.get("name", ""))
        fact_commitment = props.get("fact_commitment")
        if isinstance(fact_commitment, str) and fact_commitment:
            object_digest = fact_commitment
        else:
            fact = str(props.get("fact", ""))
            object_digest = hashlib.sha256(fact.encode("utf-8")).hexdigest()
        tuple_value = [
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
            datetime_to_text(relationship.valid_from),
            datetime_to_text(relationship.valid_to),
            int(props.get("observed_count", 1)),
            props.get("pinned") is True,
            canonical_visibility_agents(props.get("visibility_agents")),
            # WS-26 T1: derivation-DAG provenance — the same three fields the
            # SQLite substrate folds into the tuple, so byte replay covers a
            # re-dream's run lineage on both engines.
            props.get("produced_by_run"),
            props.get("superseded_by_run"),
            props.get("pruned_by_run"),
        ]
        return json.dumps(tuple_value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))

    def recompute_scope_state_tuples(self, scope_key: str) -> int:
        """Rebuild every state tuple in a scope (backfill / repair entry point)."""
        with self._engine.transaction():
            rows = self._engine.fetchall(
                "SELECT * FROM relationships WHERE properties->>'scope_key' = %s",
                (scope_key,),
            )
            self._engine.execute("DELETE FROM relationship_state_tuples WHERE scope_key = %s", (scope_key,))
            count = 0
            for row in rows:
                relationship = self._row_to_relationship(row)
                if relationship.type in _STATE_HASH_EXCLUDED_TYPES:
                    continue
                self._engine.execute(
                    """
                    INSERT INTO relationship_state_tuples (relationship_uuid, scope_key, tuple_json)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (relationship_uuid) DO UPDATE
                    SET scope_key = excluded.scope_key, tuple_json = excluded.tuple_json
                    """,
                    (relationship.uuid, scope_key, self._state_tuple_json(relationship)),
                )
                count += 1
            self._mark_scope_dirty(scope_key)
            return count

    # ------------------------------------------------------------------ embeddings

    def _sync_embedding(self, relationship: GraphRelationship, scope_key: str) -> None:
        embedding = relationship.properties.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            # A sealed embedding is unusable for comparison until revealed; the
            # row is dropped so vector search never returns a scope it cannot read.
            self._engine.execute(
                "DELETE FROM relationship_embeddings WHERE relationship_uuid = %s",
                (relationship.uuid,),
            )
            return
        try:
            vector = [float(value) for value in embedding]
        except (TypeError, ValueError):
            self._engine.execute(
                "DELETE FROM relationship_embeddings WHERE relationship_uuid = %s",
                (relationship.uuid,),
            )
            return
        self._engine.execute(
            """
            INSERT INTO relationship_embeddings (
                relationship_uuid, scope_key, dimensions, embedding, sealed, updated_at
            ) VALUES (%s, %s, %s, %s, false, now())
            ON CONFLICT (relationship_uuid) DO UPDATE
            SET scope_key = excluded.scope_key,
                dimensions = excluded.dimensions,
                embedding = excluded.embedding,
                sealed = false,
                updated_at = now()
            """,
            (relationship.uuid, scope_key, len(vector), vector),
        )
        if getattr(self, "_pgvector_enabled", False):
            self._engine.execute(
                "UPDATE relationship_embeddings SET embedding_vec = %s::vector WHERE relationship_uuid = %s",
                (json.dumps(vector), relationship.uuid),
            )

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
        """Vector search executed in the database.

        This is the DW-001 retrieval fallback: it keeps recall working through the
        storage interface if the Memory Graph is not ready.  pgvector serves it
        with an ANN index when the extension is installed; otherwise the same
        rows are scored by ``dw_cosine_similarity``.  Either way the comparison is
        a query, not a Python loop over the whole store.
        """
        if not embedding:
            raise ValueError("similar_relationships requires a non-empty embedding")
        if limit is not None and limit <= 0:
            raise ValueError("limit must be greater than zero")
        vector = [float(value) for value in embedding]

        clauses = ["e.scope_key = %s", "e.dimensions = %s"]
        params: list[Any] = [scope.key, len(vector)]
        if status is not None:
            clauses.append("r.properties->>'status' = %s")
            params.append(status)
        if relationship_types is not None:
            if not relationship_types:
                return []
            clauses.append("r.type = ANY(%s)")
            params.append(sorted(relationship_types))

        if getattr(self, "_pgvector_enabled", False):
            score_sql = "1 - (e.embedding_vec <=> %s::vector)"
            score_param: Any = json.dumps(vector)
        else:
            score_sql = "dw_cosine_similarity(e.embedding, %s)"
            score_param = vector

        rows = self._engine.fetchall(
            f"""
            SELECT r.*, {score_sql} AS similarity
            FROM relationship_embeddings e
            JOIN relationships r ON r.uuid = e.relationship_uuid
            WHERE {" AND ".join(clauses)}
              AND {score_sql} >= %s
            ORDER BY similarity DESC, r.uuid
            LIMIT %s
            """,
            [score_param, *params, score_param, float(threshold), limit],
        )
        return [(self._row_to_relationship(row), float(row["similarity"])) for row in rows]

    # ------------------------------------------------------------------ row mapping

    def _row_to_node(self, row: dict[str, Any]) -> GraphNode:
        return GraphNode(
            uuid=str(row["uuid"]),
            labels=tuple(as_json(row["labels"])),
            properties=dict(as_json(row["properties"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            valid_from=optional_datetime_from_text(row["valid_from"]),
            valid_to=optional_datetime_from_text(row["valid_to"]),
        )

    def _row_to_relationship(self, row: dict[str, Any]) -> GraphRelationship:
        return GraphRelationship(
            uuid=str(row["uuid"]),
            source_uuid=str(row["source_uuid"]),
            target_uuid=str(row["target_uuid"]),
            type=str(row["type"]),
            properties=dict(as_json(row["properties"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            valid_from=optional_datetime_from_text(row["valid_from"]),
            valid_to=optional_datetime_from_text(row["valid_to"]),
        )

    @staticmethod
    def _ordered_ids(values: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({str(value) for value in values if str(value).strip()}))
