"""The Operational Store: a pooled Postgres ``StorageBackend`` implementation.

This is the managed-Postgres backend described by DW-001.  In the designed
deployment it owns the relational and structural surface — the queue of incoming
work, job state and claims, receipts, the keys used for erasure, audit records,
quota counters and tenant policy — while the graph and its vectors live in the
Memory Graph.  Under the DW-001 contingency the same class also serves the graph
plane, with vector search pushed into SQL (pgvector when the extension is
installed), so a delay standing up the Memory Graph does not block retrieval.

Conventions for anything added to this package
----------------------------------------------
* **Placeholders are ``%s``**, never ``?``.  Parameters are always bound, never
  interpolated.
* **All SQL goes through** :class:`~memotron.storage.postgres._engine.PostgresEngine`
  (``fetchone`` / ``fetchall`` / ``fetchvalue`` / ``execute`` / ``executemany``).
  Rows come back as ``dict``.
* **Wrap multi-statement logic in ``with self._engine.transaction():``** so it
  commits once.  The context manager is re-entrant, so nesting is free and inner
  interface calls join the outer transaction.
* **Read-then-write needs a lock.**  Use ``SELECT ... FOR UPDATE`` inside the
  transaction, or an ``INSERT ... ON CONFLICT DO UPDATE`` upsert.  A bare
  ``SELECT`` followed by an ``UPDATE`` is a lost update under two replicas.
* **JSON columns are real ``jsonb``** and are named without the ``_json``
  suffix (``payload``, ``properties``, ``report``, ``config``, ``override``,
  ``certification``, ``prior_payload``).  Bind with ``%s::jsonb`` and a
  ``json.dumps(...)`` string; read back with ``as_json`` (jsonb decodes to Python
  objects already, but tolerate text).
* **Timestamps stay ISO-8601 ``text``**, exactly as SQLite stored them, so
  ordering, receipts, and the state hash remain byte-comparable across engines.
  Use ``datetime_to_text`` / ``optional_datetime_from_text``.
* **Booleans are real ``boolean``**, not 0/1 integers.
* **Filter in SQL, not in Python.**  If a predicate needs an index, add it as a
  migration rather than post-filtering a full-store read.
* **Uniqueness violations** surface as ``psycopg.errors.UniqueViolation``; catch
  that (not ``sqlite3.IntegrityError``) and re-raise the ``ValueError`` the
  interface contract specifies.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from typing import Any

from memotron.crypto import KeyManager, LocalKeyManager
from memotron.storage._shared import (
    DreamClaimPlaneMixin,
    SharedGovernancePlaneMixin,
    SharedGraphPlaneMixin,
    UtilityProjectionPlaneMixin,
)
from memotron.storage.base import MemoryGraphStorage, StorageBackend
from memotron.storage.postgres._artifacts import ArtifactPlaneMixin
from memotron.storage.postgres._engine import PostgresEngine
from memotron.storage.postgres._epochs import EpochRegistryPlaneMixin
from memotron.storage.postgres._governance import GovernancePlaneMixin
from memotron.storage.postgres._graph import GraphPlaneMixin
from memotron.storage.postgres._migrations import MIGRATIONS, PGVECTOR_COLUMN, PGVECTOR_INDEX
from memotron.storage.postgres._operational import OperationalPlaneMixin
from memotron.storage.postgres._policy import PolicyPlaneMixin
from memotron.storage.postgres._receipts import PostgresReceiptLedger

logger = logging.getLogger(__name__)


class PostgresStorageBackend(
    GraphPlaneMixin,
    OperationalPlaneMixin,
    ArtifactPlaneMixin,
    PolicyPlaneMixin,
    GovernancePlaneMixin,
    EpochRegistryPlaneMixin,
    # Engine-agnostic planes, shared with SQLiteStorageBackend. LAST among the
    # mixins on purpose: they must never shadow an engine plane, and a name they
    # both define would be an `api_surface._mro_shadows` failure rather than a
    # silent leftmost-wins. See memotron.storage._shared.
    DreamClaimPlaneMixin,
    UtilityProjectionPlaneMixin,
    SharedGovernancePlaneMixin,
    SharedGraphPlaneMixin,
    StorageBackend,
):
    """Pooled Postgres backend for the whole :class:`StorageBackend` surface.

    Parameters
    ----------
    dsn:
        libpq connection string.  In LATEST this is assembled from the Vault-
        injected credentials; see ``.helm/templates/secret-operational-store.yaml``.
    key_manager:
        KEK provider for wrapped per-scope DEKs.  Production passes a KMS-backed
        manager; omitting it yields an ephemeral local manager, which is only
        appropriate for tests.
    memory_graph:
        Set in a split deployment to route graph-plane reads and writes to the
        Memory Graph.  Left unset, this instance serves both planes (the DW-001
        pgvector contingency).
    min_size / max_size:
        Per-replica pool bounds.  ``max_size`` multiplied by the replica count
        must stay inside the instance connection budget; see
        ``docs/operational-store-sizing.md``.
    """

    def __init__(
        self,
        dsn: str,
        *,
        key_manager: KeyManager | None = None,
        memory_graph: MemoryGraphStorage | None = None,
        min_size: int = 2,
        max_size: int = 10,
        application_name: str = "memotron",
        migrate: bool = True,
        enable_pgvector: bool = True,
    ) -> None:
        self._engine = PostgresEngine(
            dsn,
            min_size=min_size,
            max_size=max_size,
            application_name=application_name,
        )
        self._memory_graph: MemoryGraphStorage = memory_graph if memory_graph is not None else self
        if key_manager is None and os.environ.get("MEMOTRON_ALLOW_EPHEMERAL_KEK", "").strip().lower() not in {
            "1",
            "true",
            "yes",
        }:
            # T0-2 fail-closed guard: an ephemeral KEK is random per process, so a
            # Postgres store sealed by this process is unreadable after any restart,
            # and the failure is silent. Refuse rather than shred quietly.
            raise ValueError(
                "refusing to build a Postgres storage backend with an ephemeral KEK: "
                "sealed content would be unreadable after any restart and by every other "
                "replica. Pass an explicit key_manager, or set "
                "MEMOTRON_ALLOW_EPHEMERAL_KEK=1 for local/dev and tests."
            )
        self.key_manager: KeyManager = key_manager if key_manager is not None else LocalKeyManager.ephemeral()
        self._pgvector_enabled = False

        if migrate:
            self._engine.migrate(MIGRATIONS)
            if enable_pgvector:
                self._pgvector_enabled = self._try_enable_pgvector()
        else:
            self._pgvector_enabled = enable_pgvector and self._engine.has_extension("vector")

        if not self._engine.is_byte_ordered():
            logger.warning(
                "Operational Store database collation is %r, not a byte-ordered "
                "collation. Text ordering will diverge from the SQLite substrate, "
                "so backend parity assertions on list order may fail. Create the "
                "database with LC_COLLATE='C'.",
                self._engine.database_collation(),
            )

        # The ledger shares this engine, so a receipt and the graph writes it
        # brackets commit in one transaction — the cross-store anchor rule.
        self.receipts = PostgresReceiptLedger(self._engine)

    # ------------------------------------------------------------------ setup

    def _try_enable_pgvector(self) -> bool:
        """Enable pgvector if the extension is installable; degrade quietly if not.

        Absence is expected, not exceptional: the float8[] path with
        ``dw_cosine_similarity`` gives the same results without an ANN index, and
        the managed instance may not have the extension whitelisted.

        TWO TRANSACTIONS, and that is the whole point. They used to be one, so the ANN
        index -- an optimization that CANNOT succeed against a deliberately dimensionless
        column -- rolled back the extension and column with it and disabled the entire
        pgvector path. Measured on latest 2026-09-04, where `vector 0.8.5` was installed
        and working while every pod logged "pgvector unavailable":

            ADD COLUMN embedding_vec vector -> OK
            CREATE hnsw INDEX               -> InvalidParameterValue: column does not have dimensions

        The message was worse than the fallback: it blamed the environment for our own DDL,
        and someone checked the instance and correctly found the extension present.
        """
        try:
            with self._engine.transaction():
                self._engine.execute("CREATE EXTENSION IF NOT EXISTS vector")
                self._engine.execute(PGVECTOR_COLUMN)
        except Exception as exc:
            logger.info(
                "pgvector unavailable; vector retrieval falls back to dw_cosine_similarity over float8[] (%s)",
                exc.__class__.__name__,
            )
            return False

        # Separate, and non-fatal: `<=>` works without an index. Failure here costs an ANN
        # index, not the pgvector path, so it must not flip the return value.
        try:
            with self._engine.transaction():
                self._engine.execute(PGVECTOR_INDEX)
        except Exception as exc:
            logger.info(
                "pgvector IS enabled; the ANN index was not created (%s). Vector retrieval uses the "
                "native `<=>` operator without an index -- correct results, sequential scan. HNSW "
                "needs a fixed dimension and relationship_embeddings stores `dimensions` per row.",
                exc.__class__.__name__,
            )
        return True

    # ------------------------------------------------------------------ introspection

    @property
    def pgvector_enabled(self) -> bool:
        """True when vector search is served by a pgvector ANN index."""
        return self._pgvector_enabled

    def pool_stats(self) -> dict[str, Any]:
        return self._engine.pool_stats()

    def schema_version(self) -> int:
        return self._engine.schema_version()

    def transaction(self) -> Any:
        """Public re-entrant transaction scope.

        A maintenance run wraps its whole pass in this so it commits once, and a
        cross-store operation wraps the operational write plus the graph writes it
        anchors.
        """
        return self._engine.transaction()

    # A fixed advisory-lock key: every ``exclusive_write_transaction`` on this
    # database contends for this one lock, the faithful twin of SQLite's
    # BEGIN IMMEDIATE (which locks the whole database for writing).
    _EXCLUSIVE_WRITE_LOCK_KEY = 0x44_57_45_58  # 'DWEX'

    @contextmanager
    def exclusive_write_transaction(self) -> Iterator[None]:
        """Serialise a read-then-write critical section across replicas.

        SQLite takes the database's RESERVED lock up front with ``BEGIN
        IMMEDIATE`` so no other connection can commit a write until this block
        exits.  The Postgres twin opens a transaction and takes one fixed
        transaction-scoped advisory lock, which every other
        ``exclusive_write_transaction`` also contends for — so the "check a
        condition, then act on it" sequence the dream-worker claim paths rely on
        is atomic against a concurrent writer.  The engine's transaction is
        re-entrant, so a method that opens its own bracket internally composes
        with this outer critical section instead of deadlocking; the advisory
        lock releases automatically at COMMIT.
        """
        with self._engine.transaction():
            self._engine.execute("SELECT pg_advisory_xact_lock(%s)", (self._EXCLUSIVE_WRITE_LOCK_KEY,))
            yield

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        self._engine.close()

    # ------------------------------------------------------------------ shared helpers

    @staticmethod
    def _ordered_scope_keys(scope_keys: Iterable[str]) -> tuple[str, ...]:
        return tuple(sorted({str(key) for key in scope_keys if str(key).strip()}))


__all__ = ["PostgresStorageBackend"]
