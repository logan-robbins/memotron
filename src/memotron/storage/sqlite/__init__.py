"""SQLite implementation of the StorageBackend contract.

The refactored ``SQLiteStorageBackend``: one engine class serving both the
Memory Graph and Operational Store surfaces for local, hermetic deployments
(no credentials, no network).  The contract itself — ownership, transactions,
the context-visible index, and the cross-store rule — is documented in
:mod:`memotron.storage.base`.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from memotron.crypto import (
    KeyManager,
    LocalKeyManager,
)
from memotron.storage._shared import (
    DreamClaimPlaneMixin,
    SharedGovernancePlaneMixin,
    SharedGraphPlaneMixin,
    UtilityProjectionPlaneMixin,
)
from memotron.storage.base import (
    MemoryGraphStorage,
    StorageBackend,
)
from memotron.storage.base import (
    normalize_key as normalize_key,
)
from memotron.storage.receipts import ReceiptLedger
from memotron.storage.sqlite._artifacts import ArtifactPlaneMixin
from memotron.storage.sqlite._epochs import (
    CONTENT_PLANE_SUBJECT_KEY as CONTENT_PLANE_SUBJECT_KEY,
)
from memotron.storage.sqlite._epochs import (
    EpochRegistryPlaneMixin,
)
from memotron.storage.sqlite._governance import (
    LLM_CREDENTIAL_SUBJECT_KEY as LLM_CREDENTIAL_SUBJECT_KEY,
)
from memotron.storage.sqlite._governance import (
    GovernancePlaneMixin,
)
from memotron.storage.sqlite._graph import (
    GraphPlaneMixin,
)
from memotron.storage.sqlite._graph import (
    ScopeStateHashTracker as ScopeStateHashTracker,
)
from memotron.storage.sqlite._migrations import MigrationsMixin
from memotron.storage.sqlite._operational import (
    DREAM_CLAIM_STALE_SECONDS as DREAM_CLAIM_STALE_SECONDS,
)
from memotron.storage.sqlite._operational import (
    OperationalPlaneMixin,
)
from memotron.storage.sqlite._policy import PolicyPlaneMixin

"""The scope's content-plane DEK — the key facts, object text, entity names,
embeddings and episode bodies are sealed under, and the key a crypto-shred
destroys.  ``DreamEngine._content_protection``,
``SQLiteStorageBackend.seal_receipt_detail``, the erasure sweep, and
``migration._source_governance_blocked_reason`` all mean exactly this row.  Its
existence is the definition of "this scope is content-protected"."""

"""The tenant's sealed LLM credential wrapping key.  Stored under the tenant
SCOPE key (``tenant:{id}``) because that is the credential's owner, NOT because
the tenant scope's content plane is sealed — the two are unrelated, and reading
this row as a content-protection signal silently forces a tenant that
configured ``text-embedding-3`` onto the hermetic 256-dim local transport."""


class SQLiteStorageBackend(
    MigrationsMixin,
    EpochRegistryPlaneMixin,
    GovernancePlaneMixin,
    ArtifactPlaneMixin,
    OperationalPlaneMixin,
    GraphPlaneMixin,
    PolicyPlaneMixin,
    # Engine-agnostic planes, shared with PostgresStorageBackend. LAST among the
    # mixins on purpose: they must never shadow an engine plane, and a name they
    # both define would be an `api_surface._mro_shadows` failure rather than a
    # silent leftmost-wins. See memotron.storage._shared.
    DreamClaimPlaneMixin,
    UtilityProjectionPlaneMixin,
    SharedGovernancePlaneMixin,
    SharedGraphPlaneMixin,
    StorageBackend,
):
    """SQLite-backed property graph store and durable dream queue.

    Implements the whole :class:`~memotron.storage.base.StorageBackend`
    surface in one database.  When constructed with an external
    ``memory_graph`` plane (a split deployment), the graph surface of this
    instance is unused: cross-plane operations read and write the graph
    through that reference instead, per the cross-store rule.
    """

    def __init__(
        self,
        db_path: str | Path = ".memotron/graph.sqlite",
        *,
        key_manager: KeyManager | None = None,
        memory_graph: MemoryGraphStorage | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._connection = sqlite3.connect(self.db_path)
        # Depth of the re-entrant logical-operation bracket (transaction()).
        self._txn_depth = 0
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        # Split deployments route graph-plane reads/writes through the Memory
        # Graph backend; in the default single-store mode this instance serves
        # both surfaces itself.  There are no foreign keys across the store
        # boundary (StorageBackend contract): referential integrity between
        # operational rows and graph rows is enforced by the write paths, so
        # intra-database FK enforcement is only enabled when this store owns
        # both planes.
        self._memory_graph: MemoryGraphStorage = memory_graph if memory_graph is not None else self
        self._connection.execute("PRAGMA foreign_keys = " + ("OFF" if memory_graph is not None else "ON"))
        # WS-12: envelope key management — scope DEKs are persisted only in wrapped
        # form; the KEK lives OUTSIDE the database (a 0600 sibling file for
        # persistent graphs, process memory for in-memory graphs).  Production
        # deployments pass a KMS-backed KeyManager here.
        if key_manager is not None:
            self.key_manager: KeyManager = key_manager
        elif str(db_path) == ":memory:":
            self.key_manager = LocalKeyManager.ephemeral()
        else:
            self.key_manager = LocalKeyManager.from_file(Path(str(self.db_path) + ".kek"))
        self._migrate()
        # WS-11: the canonical, hash-chained, Merkle-committed receipt ledger shares this
        # graph's SQLite connection ([0033]); it runs its own idempotent DDL on construction.
        self.receipts = ReceiptLedger(self._connection, commit=self._commit)
        # WS-23 M1: the ledger is the choke point every receipt passes through,
        # but only the store knows which scopes are content-protected and holds
        # the key manager that can seal a diverted reason.
        self.receipts.bind_content_protection(
            probe=self.scope_content_is_protected,
            sealer=self.seal_receipt_detail,
        )

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def exclusive_write_transaction(self) -> Iterator[None]:
        """Hold SQLite's writer lock across a read-then-write critical section.

        ``BEGIN IMMEDIATE`` takes the database's RESERVED lock up front, so from
        the moment this context is entered no OTHER connection can commit a
        write until it exits — a second process attempting one waits out
        ``busy_timeout`` and then fails with ``database is locked`` rather than
        interleaving. That is what makes a "check a condition, then act on it"
        sequence safe against a concurrent writer; without it the check is only
        ever a statement about the past.

        Shares the re-entrant ``_txn_depth`` counter with
        :meth:`transaction`, so a method that opens its own logical-operation
        bracket internally (``purge_tenant_state``) composes with this outer
        critical section instead of trying to ``BEGIN`` a second time: the whole
        block commits together at the outermost exit, or rolls back together on
        an exception.
        """
        self._txn_depth += 1
        outermost = self._txn_depth == 1
        if outermost:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if outermost:
                self._connection.rollback()
            raise
        else:
            if outermost:
                self._connection.commit()
        finally:
            self._txn_depth -= 1

    # ------------------------------------------------------------------ WS-26: epochs

    # ------------------------------------------------------------------
    # Dream-work claims: operational, cross-process mutual exclusion for the
    # read-decide-write windows in dreaming.  A claim is NOT a memory decision:
    # it lives outside `graph_state_hash` (which digests relationship rows
    # only) and outside the receipt ledger, exactly like the processed-episode
    # markers and the registries.  Claims are released on run completion and
    # EXPIRE after `DREAM_CLAIM_STALE_SECONDS`, so a crashed run can never
    # wedge the queue.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------ use / outcome event plane

    # ------------------------------------------------------------------
    # WS-24: the quarantine store — retained candidates that are not memories
    #
    # A SIBLING TABLE, not a status flag on ``relationships``.  The closest
    # precedent in this store is ``memory_prune_ghosts``: a retained, receipted,
    # promotable-back record that deliberately lives OUTSIDE the relationship
    # plane.  Putting a non-memory inside the truth plane would make every read
    # path — search, profile, truth-key repair, single-active collapse, the
    # erasure sweep, coherence, replay, every count on the evolution proof —
    # individually responsible for excluding it, which is the same "one more
    # filter to forget" shape that turned a classification error into a
    # retrieval failure in the first place.  A quarantined candidate also has
    # no truth key, no resolved entities, and possibly no valid relationship
    # type at all: there is nothing for the truth plane to key it on.
    # (Do not write "type:" at the start of a comment word here -- Python parses
    #  `# type: ...` as a PEP 484 type comment and mypy aborts the whole run.)

    # ------------------------------------------------------------------
    # Live behavior artifacts and their outcome-attributed utility state

    # ------------------------------------------------------------------
    # Immutable policy contracts, shadow stages, and atomic live aliases

    # ------------------------------------------------------------------
    # WS-17 T16: per-scope canonical predicate registry
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-17 T16b: per-scope entity alias registry
    # ------------------------------------------------------------------

    _ENTITY_ALIAS_STATUSES = ("active", "proposed", "rejected")

    # ------------------------------------------------------------------
    # WS-19 T20: promotion endorsement ledger
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-12: Crypto-shredding key management (envelope encryption)
    # The DEK is never persisted raw: governance_keys stores only the
    # KEK-wrapped form.  Production swaps LocalKeyManager for a KMS.
    # ------------------------------------------------------------------

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Re-entrant logical-operation bracket (StorageBackend contract).

        The outermost entry issues ``BEGIN IMMEDIATE`` (taking the write lock up
        front, mirroring the server engine's explicit transaction); nested
        entries join it.  Per-method commits become no-ops inside the bracket —
        everything commits together at the outermost exit, or rolls back
        together if the block raises.  The hermetic substrate is single-writer,
        so the depth counter is deliberately not thread-safe; the contract's
        one-maintenance-runner rule is what makes that sound.
        """
        self._txn_depth += 1
        outermost = self._txn_depth == 1
        if outermost:
            self._connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            if outermost:
                self._connection.rollback()
            raise
        else:
            if outermost:
                self._connection.commit()
        finally:
            self._txn_depth -= 1

    def _commit(self) -> None:
        """Commit now, unless a transaction() bracket owns the commit point."""
        if self._txn_depth == 0:
            self._connection.commit()

    def _in_clause_sql(self, template: str, values: Iterable[Any]) -> str:
        count = len(tuple(values))
        if count <= 0:
            raise ValueError("IN clause requires at least one value")
        return template.format(placeholders=", ".join("?" for _ in range(count)))

    def _json_dumps(self, value: Any, label: str) -> str:
        try:
            return json.dumps(value, sort_keys=True)
        except TypeError as exc:
            raise ValueError(f"{label} must be JSON serializable") from exc

    def _normalize_tenant_id(self, tenant_id: str) -> str:
        normalized = tenant_id.strip()
        if not normalized:
            raise ValueError("tenant_id cannot be blank")
        return normalized

    def _normalize_agent_id(self, agent_id: str) -> str:
        normalized = agent_id.strip()
        if not normalized:
            raise ValueError("agent_id cannot be blank")
        return normalized

    def _normalize_non_blank(self, value: str, label: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"{label} cannot be blank")
        return normalized

    def _normalize_llm_provider(self, provider: str) -> str:
        normalized = provider.strip().lower()
        if normalized not in {"openai", "litellm"}:
            if normalized == "anthropic":
                raise ValueError(
                    "provider 'anthropic' is retired: Memotron reaches Claude "
                    "through the JedAI Gateway. Use provider 'litellm' with the "
                    "gateway base_url and an undated model alias."
                )
            raise ValueError("provider must be 'litellm' or 'openai'")
        return normalized

    def _datetime_to_text(self, value: datetime | None) -> str | None:
        return None if value is None else value.isoformat()

    def _optional_datetime_from_text(self, value: Any) -> datetime | None:
        return None if value is None else self._datetime_from_text(str(value))

    def _datetime_from_text(self, value: str) -> datetime:
        return datetime.fromisoformat(value)


# Backwards-compatible alias: the pre-interface name of the SQLite backend.
PropertyGraphStore = SQLiteStorageBackend
