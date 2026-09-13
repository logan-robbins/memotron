"""The StorageBackend contract: the explicit interface in front of all persistence.

This module is the single authority on what a storage engine must provide.
``PropertyGraphStore`` grew as one SQLite class that owned every table and all
the SQL; this interface is step zero of the persistence work that splits that
surface across engines (SQLite for the hermetic local suite, Postgres for the
Operational Store, a graph engine for the Memory Graph).

Ownership: which store owns which part of the surface
-----------------------------------------------------
The persisted surface divides into two stores:

**Memory Graph** (:class:`MemoryGraphStorage`) — the temporal property graph
itself: entity nodes, memory relationships, the context-visible read tier, and
the scope state hash.  This is the surface a graph engine implements.
The interface is deliberately expressed in graph vocabulary (nodes,
relationships, traversal-free scoped reads) and never in relational vocabulary
(no rows, cursors, or SQL fragments cross this boundary), so a graph engine is
not forced into a relational shape.  Conversely, nothing in it requires
engine-side joins or multi-entity SQL transactions that a graph engine cannot
give: every mutation is a single node or single relationship write.

**Operational Store** (:class:`OperationalStorage`) — everything that is
bookkeeping *about* memory rather than memory itself: the durable queue of
incoming episodes and their per-consumer processing marks, job state,
maintenance (dream) runs and their decision log, the use/outcome event plane,
prune ghosts, live artifacts and repair monitors, policy contracts/aliases/
shadow stages, tenant identity and configuration, the governance keys used for
erasure (crypto-shred), tenant LLM credentials, and the hash-chained receipt
ledger.  This is the surface the relational Operational Store (Postgres)
implements.

:class:`StorageBackend` is the union of both surfaces plus the operations that
deliberately span them (e.g. :meth:`StorageBackend.purge_tenant_state`).  A
single-engine deployment (SQLite today) implements the union in one class; a
split deployment composes one implementation of each surface — see
``memotron.storage.composite.SplitStorageBackend``.

Transactions and concurrency: what callers may assume
------------------------------------------------------
* **Per-call atomicity.**  Every mutating interface method is atomic: it either
  fully applies and is durable when the call returns, or raises with no partial
  write visible to any subsequent read *of the same store*.
* **Logical-operation bracketing.**  :meth:`OperationalStorage.transaction`
  widens per-call atomicity to one logical operation: every Operational Store
  write issued inside the bracket commits together at the outermost exit, or
  not at all.  A maintenance pass wraps its write phase in one bracket so it
  commits once, not once per write.  The bracket is re-entrant per calling
  thread, must never be held across an LLM or network call, and — per the
  cross-store rule — does not extend to a Memory Graph running on a different
  engine: graph writes issued inside it remain independently durable and are
  safe to repeat.  Outside a bracket there is no cross-call atomicity.
* **Read-your-writes.**  Within one backend instance, a read issued after a
  mutating call returns observes that mutation.
* **Single-writer discipline.**  Backends must tolerate concurrent readers, but
  callers must not assume concurrent multi-process writers are safe beyond what
  the engine provides (SQLite serialises via WAL + busy timeout; server engines
  via their own MVCC).  Mutual exclusion between maintenance (dream) runners is
  the store's job, arbitrated by the work-claim surface (``claim_episodes`` /
  ``claim_scope_work`` / ``release_dream_claims`` — the #17 dream-worker
  design); until that surface joins :class:`OperationalStorage`, deployments
  keep one maintenance runner per store at a time.
* **Idempotent event appends.**  The event-plane writes
  (:meth:`OperationalStorage.record_use_event`,
  :meth:`OperationalStorage.record_outcome_event`,
  :meth:`OperationalStorage.record_live_artifact_outcome`) are idempotent per
  ``(scope_key, idempotency_key)``: replaying the same event returns the stored
  event; replaying a *different* event under a used key is an error.

The rule that spans both stores
-------------------------------
A logical transaction **anchors in the Operational Store**: the receipt (and
any queue/job/event row) is the commit point and the source of truth for
whether an operation happened.  Writes to the Memory Graph are **safe to
repeat**: every graph mutation in this contract is expressible as an idempotent
upsert/property-set (``upsert_node`` keys on the normalised graph key;
relationship property updates are last-write-wins by uuid), so a crashed
operation is recovered by re-driving the graph writes from the anchored
operational record.  There are therefore **no foreign keys across the store
boundary**: referential integrity between operational rows (events, ghosts)
and graph relationships is enforced by the write paths, not by the engine.

The index the visible-in-context read depends on
------------------------------------------------
:meth:`MemoryGraphStorage.context_visible_relationships` is the default
working-context tier and MUST be served by an index (or equivalent
access path) over ``(scope_key, status, context-visibility)`` so its cost is
bounded by the scope's context-visible set and decoupled from total store
size — other scopes, non-active rows, and demoted (``active_in_context`` is
False) members are never scanned.  The SQLite implementation seeks the
``relationships_ctx_idx`` generated-column index; a graph engine satisfies the
same clause with a property index on scoped relationship properties.
:meth:`MemoryGraphStorage.explain_context_visible_read` exposes engine-native
evidence of that access path for certification.  Evidence, search, and
historical (``as_of``) reads intentionally read the full store instead.

Retrieval writes by default; pure reads are opt-in
--------------------------------------------------
By default retrieval revives a matching prune ghost inline
(:meth:`OperationalStorage.restore_prune_ghost` called from search), so a read
is a writer that takes row locks on the rows it revives.  Backends must expect
ghost writes on the read path unless the deployment sets
``DreamConfig.pure_read_retrieval``, which makes ``search``,
``search_context`` and ``semantic_search`` pure reads that only *report*
matching archived rows.  Operators turn that flag on precisely because the
locks contend once more than one replica serves retrieval; the trade is that an
archived row then comes back only through the explicit curation action
(``Memotron.restore_archived_memory``), which is available in both modes.
The switch lives above this interface — no engine behaves differently for it.

Erasure
-------
Crypto-shred erasure destroys the wrapped per-scope DEK held by the
Operational Store (:meth:`OperationalStorage.shred_governance_key`); sealed
content in either store then becomes permanently unreadable without any graph
rewrite, and :meth:`MemoryGraphStorage.graph_state_hash` is stable across the
shred.  The graph-plane deletion primitives
(:meth:`MemoryGraphStorage.delete_nodes` /
:meth:`MemoryGraphStorage.delete_relationships`) are idempotent by uuid set,
per the cross-store rule.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Sequence
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

# Runtime, not TYPE_CHECKING: `receipts: ReceiptLedger` at :662 is a class-level
# annotation on a public ABC, so typing.get_type_hints() must resolve it -- and it
# could not. Same for KeyManager. These two were the ONLY things keeping PEP 561
# `py.typed` from shipping. No cycle: memotron.receipts depends only on
# memotron.models, and memotron.crypto on nothing in the package.
from memotron.crypto import KeyManager
from memotron.models import (
    ArtifactContributionProjection,
    CoherenceRepairMonitor,
    DreamDecisionRecord,
    DreamJobRunRecord,
    Episode,
    FormationContractAttestation,
    GraphNode,
    GraphRelationship,
    MemoryScope,
    MemoryUtilityProjection,
    OutcomeEvent,
    PersistentArtifact,
    PruneGhost,
    QuarantinedCandidate,
    QuarantineStatus,
    RelationshipStatus,
    RetrievalNegativeSpaceEntry,
    UseEvent,
)
from memotron.storage.receipts import ReceiptLedger


def normalize_key(value: str) -> str:
    """Canonical, engine-independent normalisation for graph keys."""
    return " ".join(value.casefold().strip().split())


class MemoryGraphStorage(ABC):
    """The Memory Graph surface: nodes, relationships, and scoped graph reads.

    This is the surface a graph engine implements.  See the module docstring
    for the contract (ownership, transactions, the context-visible index, and
    the cross-store rule).
    """

    # -- nodes ---------------------------------------------------------------

    @abstractmethod
    def upsert_node(
        self,
        *,
        labels: Iterable[str],
        key: str,
        properties: dict[str, Any],
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> tuple[GraphNode, bool]:
        """Create or update the node addressed by the normalised *key*.

        Idempotent per the cross-store rule: repeating the same upsert
        converges on the same node.  Returns ``(node, created)``.
        """

    @abstractmethod
    def get_node(self, uuid: str) -> GraphNode:
        """Return the node or raise ``ValueError`` if it does not exist."""

    @abstractmethod
    def nodes(self) -> list[GraphNode]:
        """All nodes ordered by ``(created_at, uuid)`` (full-store read)."""

    @abstractmethod
    def nodes_for_scope(self, scope_key: str) -> list[GraphNode]:
        """All nodes whose properties carry this scope key (erasure sweep)."""

    @abstractmethod
    def delete_nodes(self, uuids: Iterable[str]) -> int:
        """Delete nodes by uuid; idempotent; returns the number removed."""

    # -- relationships -------------------------------------------------------

    @abstractmethod
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
        """Create one directed relationship; both endpoints must exist."""

    @abstractmethod
    def get_relationship(self, uuid: str) -> GraphRelationship:
        """Return the relationship or raise ``ValueError`` if absent."""

    @abstractmethod
    def relationships(self) -> list[GraphRelationship]:
        """All relationships ordered by ``(created_at, uuid)`` (full-store read)."""

    @abstractmethod
    def relationships_for_scope(self, scope_key: str) -> list[GraphRelationship]:
        """All relationships in one scope, any status, ordered ``(created_at, uuid)``.

        The scope filter is pushed into the engine's query (index-backed), so
        retrieval candidate enumeration is bounded by the scope rather than by
        total store size.
        """

    @abstractmethod
    def active_relationships(
        self,
        *,
        scope: MemoryScope | None = None,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        """Status-active relationships, optionally narrowed by scope and type."""

    @abstractmethod
    def context_visible_relationships(
        self,
        *,
        scope: MemoryScope,
        relationship_types: set[str] | None = None,
    ) -> list[GraphRelationship]:
        """Index-backed read of the context-visible tier for one scope.

        Context-visible = status active AND not demoted (``active_in_context``
        is not False).  MUST be served by the scoped index/access path
        described in the module docstring; cost is bounded by the scope's
        context-visible set, never by total store size.  The validity-window
        check is left to the caller.
        """

    @abstractmethod
    def explain_context_visible_read(self, *, scope: MemoryScope) -> list[str]:
        """Engine-native access-plan evidence for the context-visible read.

        Implementations return their engine's plan description (e.g. SQLite
        ``EXPLAIN QUERY PLAN`` rows, a Cypher ``EXPLAIN`` operator summary)
        proving the read seeks the scoped index rather than scanning.
        """

    @abstractmethod
    def find_active_truth_relationships(self, truth_key: str, *, scope_key: str) -> list[GraphRelationship]:
        """Active relationships in ONE scope whose ``truth_key`` matches (normalised).

        ``scope_key`` is required and keyword-only, deliberately. Without it this read
        spanned every scope in the store, and a truth key is not scope-unique: it is built
        by UNESCAPED concatenation of ``scope_key:subject:predicate`` and then casefolded,
        so ``tenant:acme`` + ``roadmap:q3`` and ``tenant:acme:roadmap`` + ``q3`` collide,
        as do ``tenant:Acme`` and ``tenant:acme``. Because the supersession and reinforce
        paths WRITE to what this returns, the missing predicate was a cross-tenant
        destructive write, reproduced through the public SDK.

        No default: four other methods on this contract default a scope to ``None`` and
        every one of them is a fleet-wide read one omitted argument away.
        """

    @abstractmethod
    def find_active_relationships_by_truth_prefix(
        self, truth_prefix: str, *, scope_key: str
    ) -> list[GraphRelationship]:
        """Active relationships in ONE scope whose ``truth_prefix`` matches (normalised).

        ``scope_key`` is required for the same reason as
        :meth:`find_active_truth_relationships`.
        """

    @abstractmethod
    def mark_relationship(
        self,
        relationship_uuid: str,
        *,
        status: RelationshipStatus,
        valid_to: datetime | None = None,
        properties: dict[str, Any] | None = None,
    ) -> GraphRelationship:
        """Set a relationship's lifecycle status (and optional extra properties)."""

    @abstractmethod
    def update_relationship(
        self,
        relationship_uuid: str,
        *,
        properties: dict[str, Any] | None = None,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
        clear_valid_to: bool = False,
    ) -> GraphRelationship:
        """Merge properties / adjust validity window on one relationship.

        Safe to repeat (last-write-wins by uuid), per the cross-store rule.
        """

    @abstractmethod
    def delete_relationships(self, uuids: Iterable[str]) -> int:
        """Delete relationships by uuid; idempotent; returns the number removed."""

    @abstractmethod
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
        """Engine-side similarity search over stored relationship embeddings.

        The comparison is executed by the engine (an ANN or SQL expression on a
        server engine, an in-backend scan bounded to the scope on the hermetic
        substrate), never as a caller-side loop over the store.  Returns
        ``(relationship, similarity)`` pairs with similarity in ``[-1, 1]``,
        ordered by similarity descending then uuid, filtered to
        ``similarity >= threshold``.  ``limit=None`` returns every match.

        Only plaintext stored embeddings participate: a sealed (WS-12) embedding
        never reaches engine-side comparison, so a scope the engine cannot read
        is never returned by it.  Callers that must search a sealed scope while
        its DEK lives reveal and score those rows themselves via
        :meth:`OperationalStorage.reveal_vector`.
        """

    # -- scoped graph reads ----------------------------------------------------

    @abstractmethod
    def scopes(self) -> list[MemoryScope]:
        """Distinct scopes with at least one relationship, first-seen order."""

    @abstractmethod
    def graph_state_hash(self, scope_key: str) -> str:
        """Deterministic content hash of a scope's memory-relationship state.

        Stable across crypto-shred (binds stored write-time digests, never
        plaintext); excludes ``MENTIONS`` structural edges; O(scope).

        MAY be memoised. A caller that needs the hash to reflect the ROWS rather than the
        backend's own bookkeeping — an audit plane checking for out-of-band mutation — must
        call :meth:`recompute_scope_state_tuples` first. See T0-7.
        """

    @abstractmethod
    def recompute_scope_state_tuples(self, scope_key: str) -> int:
        """Rebuild whatever :meth:`graph_state_hash` derives from, and return the row count.

        T0-7. This is on the contract rather than being an optional Postgres extra, because
        the two engines answer :meth:`graph_state_hash` differently: SQLite recomputes from
        ``relationships`` on every call, Postgres reads an incrementally maintained memo that
        only writes THROUGH the backend update. A direct row edit therefore left the Postgres
        hash stale, and tampering — the one thing the audit plane exists to catch — passed
        undetected on three of four mutation kinds while SQLite caught all four.

        Declaring it here means the audit path calls it unconditionally instead of testing
        for the capability. An optional method nobody declares is the shape that rots; an
        honest no-op is cheaper to reason about. SQLite's implementation IS a no-op, and says
        so.

        Not free: O(scope). Call it on audit paths, not on the write path — the memo exists
        precisely so the write path does not pay this.
        """

    @abstractmethod
    def export(self) -> dict[str, Any]:
        """JSON-serialisable dump of all nodes and relationships."""

    # -- batch / slot reads and the incremental state-hash handle --------------

    @abstractmethod
    def relationships_by_uuids(self, uuids: Sequence[str]) -> list[GraphRelationship]:
        """WS-5: batch primary-key read; missing uuids are silently absent."""

    @abstractmethod
    def relationships_for_node_uuids(self, *, scope_key: str, node_uuids: Sequence[str]) -> list[GraphRelationship]:
        """WS-5: memory relationships incident to any node in one scope (MENTIONS stripped)."""

    @abstractmethod
    def relationships_for_truth_prefix(self, truth_prefix: str, *, scope_key: str) -> list[GraphRelationship]:
        """WS-16 T12: ALL relationships on ONE scope's truth slot, regardless of status.

        ``scope_key`` is required for the same reason as
        :meth:`find_active_truth_relationships`.
        """

    @abstractmethod
    def scope_state_hash_tracker(self, scope_key: str) -> Any:
        """A handle whose ``digest`` is the scope's current ``graph_state_hash``
        and whose ``record(relationship)`` folds a just-written relationship in
        and returns the new hash — an incremental alternative to rescanning the
        scope on every write during a bulk pass."""

    # -- WS-26 derivation-DAG epoch layer + registry overlays ------------------
    #
    # The branchable epoch spine (``graph_epochs`` / ``epoch_runs`` /
    # ``active_epochs``) and the epoch-tagged canonicalization-registry overlay
    # primitives.  These are graph-plane surface — a re-dream branches, diffs,
    # adopts, and rolls back one scope's graph, and ``registry_state_digest``
    # is the registry half of the byte-for-byte rollback proof that sits beside
    # ``graph_state_hash``.  ``memotron.epochs`` orchestrates the cycle over
    # exactly these methods; both engines must provide them so a re-dream runs
    # against a Postgres live store as it does against SQLite.

    @abstractmethod
    def ensure_root_epoch(self, scope_key: str, *, now: datetime) -> str:
        """Lazily create *scope_key*'s root epoch and return HEAD (idempotent)."""

    @abstractmethod
    def active_epoch_for_scope(self, scope_key: str) -> str | None:
        """The scope's current HEAD epoch id, or ``None`` if never initialized."""

    @abstractmethod
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

    @abstractmethod
    def epoch(self, epoch_id: str) -> dict[str, Any]:
        """One epoch's bookkeeping row, or raise ``ValueError``."""

    @abstractmethod
    def epochs_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """Every epoch ever created for *scope_key*, oldest first."""

    @abstractmethod
    def set_epoch_status(self, epoch_id: str, status: str) -> dict[str, Any]:
        """Bookkeeping-only status transition; never deletes or mutates rows."""

    @abstractmethod
    def set_epoch_pre_adopt_snapshot(self, epoch_id: str, snapshot: dict[str, Any]) -> None:
        """Record the exact pre-adopt state an adopt is about to retire."""

    @abstractmethod
    def set_active_epoch(self, scope_key: str, epoch_id: str, *, now: datetime) -> None:
        """The pointer flip — adopt or roll back a scope's HEAD."""

    @abstractmethod
    def record_epoch_run(self, epoch_id: str, run_uuid: str, *, now: datetime) -> None:
        """Attribute one receipted run to the epoch it wrote into."""

    @abstractmethod
    def epoch_run_uuids(self, epoch_id: str) -> list[str]:
        """Every run that contributed to *epoch_id*, oldest first."""

    @abstractmethod
    def epoch_ancestry(self, epoch_id: str) -> tuple[str, ...]:
        """*epoch_id* plus every ancestor up to the root, self first."""

    @abstractmethod
    def registry_state_digest(self, scope_key: str) -> str:
        """Deterministic digest of a scope's canonicalization-registry state,
        epoch-ancestry filtered; the registry half of T6 rollback verification.
        Deliberately kept separate from :meth:`graph_state_hash`."""

    @abstractmethod
    def predicate_alias_row(self, scope_key: str, predicate: str) -> dict[str, Any] | None:
        """The full predicate-canonicalization registry row for one surface, or None."""

    @abstractmethod
    def predicate_alias_rows_for_scope(self, scope_key: str) -> list[dict[str, Any]]:
        """Every predicate-registry row for a scope, registration order."""

    @abstractmethod
    def overlay_predicate_row(self, scope_key: str, record: dict[str, Any], *, epoch_id: str) -> None:
        """Administrative upsert of a full predicate-registry row (no first-wins)."""

    @abstractmethod
    def delete_predicate_row(self, scope_key: str, predicate_normalized: str) -> None:
        """Remove a predicate-registry row a rollback proves never existed pre-adopt."""

    @abstractmethod
    def overlay_entity_alias_row(self, scope_key: str, record: dict[str, Any], *, epoch_id: str) -> None:
        """Administrative upsert of a full entity-alias registry row (no transition rules)."""

    @abstractmethod
    def delete_entity_alias_row(self, scope_key: str, name_normalized: str) -> None:
        """Remove an entity-alias registry row a rollback proves never existed pre-adopt."""

    # -- WS-17 canonicalization registries (predicate + entity) ----------------

    @abstractmethod
    def canonical_predicate_for(self, scope_key: str, predicate: str) -> str | None:
        """Registered canonical for one predicate surface, or None when unmapped."""

    @abstractmethod
    def canonical_predicates_for_scope(self, scope_key: str) -> list[str]:
        """Distinct canonical predicates for a scope, in registration order."""

    @abstractmethod
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

    @abstractmethod
    def canonical_entity_name_for(self, scope_key: str, name: str) -> str | None:
        """ACTIVE-alias canonical name for one entity surface, or None when unmapped."""

    @abstractmethod
    def entity_alias_row(self, scope_key: str, name: str) -> dict[str, Any] | None:
        """The full entity registry row for one surface name regardless of status."""

    @abstractmethod
    def entity_alias_rows_for_scope(self, scope_key: str, status: str | None = None) -> list[dict[str, Any]]:
        """Entity registry rows for a scope in deterministic (proposed_at, name) order."""

    @abstractmethod
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

    @abstractmethod
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

    @abstractmethod
    def update_entity_alias_evidence(
        self,
        scope_key: str,
        name_normalized: str,
        *,
        link_score: float,
        link_signals: dict[str, Any],
    ) -> None:
        """Mutate one row's living link evidence (score + signals) in place."""

    # -- WS-24 raw / quarantine store ------------------------------------------

    @abstractmethod
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

    @abstractmethod
    def quarantined_candidate(self, candidate_uuid: str) -> QuarantinedCandidate:
        """One stored candidate by uuid, or raise ``ValueError``."""

    @abstractmethod
    def quarantined_candidate_payload(self, candidate_uuid: str) -> str:
        """The stored raw candidate JSON (sealed for a protected scope)."""

    @abstractmethod
    def quarantined_candidates(
        self,
        *,
        scope_key: str | None = None,
        status: QuarantineStatus | str | None = QuarantineStatus.QUARANTINED,
        limit: int | None = None,
    ) -> list[QuarantinedCandidate]:
        """Stored candidates, filterable by scope and status, deterministic order."""

    @abstractmethod
    def quarantined_candidate_content_fields(self, *, scope_key: str) -> list[tuple[str, dict[str, Any]]]:
        """RAW (undecrypted) content-plane values per quarantined candidate."""

    @abstractmethod
    def quarantine_counts(self, *, scope_key: str) -> dict[str, int]:
        """Per-status counts of a scope's stored candidates."""

    @abstractmethod
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

    # -- WS-23 content-plane protection ----------------------------------------

    @abstractmethod
    def scope_content_is_protected(self, scope_key: str) -> bool:
        """True once this scope's content-plane row has ever been sealed."""

    # -- lifecycle -------------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Release engine resources; the instance must not be used afterwards."""


class OperationalStorage(ABC):
    """The Operational Store surface: queues, jobs, events, governance, receipts.

    This is the surface the relational Operational Store implements.  A
    logical transaction anchors here (see the module docstring).  Implementers
    must also provide two attributes:

    ``receipts``
        The :class:`~memotron.receipts.ReceiptLedger` for this store.  The
        ledger rides the Operational Store's transaction domain — a receipt is
        the commit point of the operation it records.

    ``key_manager``
        The :class:`~memotron.crypto.KeyManager` that wraps governance DEKs
        (the KEK never lives inside the store).

    Reserved surface (#17, the dream worker): the work-claim primitives
    (``claim_episodes``, ``claim_scope_work``, ``release_dream_claims``) and
    the worker bookkeeping reads (``record_worker_sweep``, ``worker_sweeps``,
    ``job_last_runs``, ``known_tenant_ids``) land here when the work-claims
    commits rebase over this contract.  They are Operational Store surface:
    claims are job state, not memory.  The conformance suite
    (``tests/test_storage_backend.py``) forces each of them onto this ABC the
    moment the SQLite implementation gains them, and each needs a Postgres
    twin (``FOR UPDATE SKIP LOCKED`` or advisory locks) so the dream worker's
    multi-replica promise holds.
    """

    receipts: ReceiptLedger
    key_manager: KeyManager

    # -- episode queue -----------------------------------------------------

    @abstractmethod
    def add_episode(self, episode: Episode) -> None:
        """Enqueue one incoming episode; duplicate uuids raise ``ValueError``."""

    @abstractmethod
    def get_episode(self, episode_uuid: str) -> Episode:
        """Return the episode or raise ``ValueError`` if absent."""

    @abstractmethod
    def episodes(self) -> list[Episode]:
        """All stored episodes ordered by ``(created_at, uuid)``."""

    @abstractmethod
    def episodes_for_scope(self, scope_key: str) -> list[Episode]:
        """All episodes for one scope (erasure sweep + shred retire)."""

    @abstractmethod
    def episode_event_counts(self, *, scope: MemoryScope, event: str) -> tuple[int, int]:
        """``(total, globally-pending)`` counts for one agent-memory event kind."""

    @abstractmethod
    def is_episode_processed(self, episode_uuid: str, *, consumer_key: str | None = None) -> bool:
        """Whether the episode was processed (optionally by one consumer)."""

    @abstractmethod
    def mark_episode_processed(
        self,
        episode_uuid: str,
        *,
        processed_at: datetime,
        consumer_key: str | None = None,
    ) -> None:
        """Record processing; idempotent per (episode, consumer)."""

    # -- job state ---------------------------------------------------------

    @abstractmethod
    def get_job_last_run(self, job_name: str) -> datetime | None:
        """Last recorded run time for a named job, or None."""

    @abstractmethod
    def set_job_last_run(self, job_name: str, last_run: datetime) -> None:
        """Upsert the last-run marker for a named job."""

    # -- maintenance runs and decisions -------------------------------------

    @abstractmethod
    def record_dream_job_run(self, record: DreamJobRunRecord) -> None:
        """Append one maintenance-run record."""

    @abstractmethod
    def dream_job_runs(self, *, limit: int = 20, job_name: str | None = None) -> list[DreamJobRunRecord]:
        """Most recent maintenance runs, newest first."""

    @abstractmethod
    def record_dream_decision(self, record: DreamDecisionRecord) -> None:
        """Append one maintenance decision record."""

    @abstractmethod
    def dream_decisions(
        self,
        *,
        limit: int = 20,
        job_name: str | None = None,
        agent_id: str | None = None,
        decision_type_prefix: str | None = None,
    ) -> list[DreamDecisionRecord]:
        """Most recent maintenance decisions, newest first, filterable."""

    @abstractmethod
    def dream_decisions_for_scope(self, scope_key: str) -> list[DreamDecisionRecord]:
        """All decisions targeting one scope (erasure sweep)."""

    # -- use / outcome event plane -------------------------------------------

    @abstractmethod
    def record_use_event(self, event: UseEvent) -> UseEvent:
        """Append an immutable use event; idempotent per (scope, idempotency key).

        Cross-plane validation: the referenced relationship must exist in the
        Memory Graph and carry the event's scope.
        """

    @abstractmethod
    def record_outcome_event(self, event: OutcomeEvent) -> OutcomeEvent:
        """Append an outcome; its use event must exist in the same scope/run."""

    @abstractmethod
    def use_events(
        self,
        *,
        scope_key: str | None = None,
        relationship_uuid: str | None = None,
        task_run_id: str | None = None,
    ) -> list[UseEvent]:
        """Stored use events, filterable, ordered by ``(used_at, use_id)``."""

    @abstractmethod
    def outcome_events(
        self,
        *,
        scope_key: str | None = None,
        use_id: str | None = None,
        task_run_id: str | None = None,
    ) -> list[OutcomeEvent]:
        """Stored outcome events, filterable, ordered by ``(judged_at, outcome_id)``."""

    @abstractmethod
    def utility_projection(
        self,
        *,
        scope_key: str,
        relationship_uuid: str | None = None,
        as_of: datetime | None = None,
    ) -> list[MemoryUtilityProjection]:
        """Rebuild the utility read model from the append-only event tables."""

    @abstractmethod
    def utility_projection_from_receipts(
        self,
        *,
        scope_key: str,
        relationship_uuid: str | None = None,
        as_of: datetime | None = None,
    ) -> list[MemoryUtilityProjection]:
        """Rebuild utility solely from hash-linked event receipts (audit path)."""

    @abstractmethod
    def retrieval_negative_space(
        self, *, scope_key: str, task_run_id: str | None = None
    ) -> list[RetrievalNegativeSpaceEntry]:
        """Retrieved impressions that never progressed to selection or use."""

    # -- prune ghosts ---------------------------------------------------------

    @abstractmethod
    def create_prune_ghost(
        self,
        *,
        relationship_uuid: str,
        scope_key: str,
        prune_receipt_uuid: str,
        reason: str,
        pruned_at: datetime,
    ) -> PruneGhost:
        """Record a restorable tombstone for a pruned relationship."""

    @abstractmethod
    def prune_ghost(self, relationship_uuid: str) -> PruneGhost:
        """Return the ghost or raise ``ValueError`` if absent."""

    @abstractmethod
    def prune_ghosts(self, *, scope_key: str, restorable_only: bool = False) -> list[PruneGhost]:
        """Ghosts for one scope ordered by ``(pruned_at, relationship_uuid)``.

        Read-only, and the only ghost method retrieval calls when the
        deployment sets ``DreamConfig.pure_read_retrieval``; by default
        retrieval follows a match with :meth:`restore_prune_ghost`.
        """

    @abstractmethod
    def restore_prune_ghost(self, relationship_uuid: str, *, restored_at: datetime) -> PruneGhost:
        """Reactivate the pruned relationship (idempotent graph write) and
        stamp the ghost; crypto-shredded content is unrestorable.

        Reached from the retrieval path (the default revive-on-read) and from
        the explicit curation action (``Memotron.restore_archived_memory``),
        which is the only caller when ``DreamConfig.pure_read_retrieval`` is on.
        A crypto-shredded ghost is durably demoted to ``restorable = False``
        before ``ContentKeyUnavailableError`` is raised; a ghost already
        demoted raises ``ValueError``.  Both engines must refuse identically.
        """

    # -- live behavior artifacts ----------------------------------------------

    @abstractmethod
    def register_live_artifact(self, artifact: PersistentArtifact) -> PersistentArtifact:
        """Persist a projected skill/file artifact plus an immutable version."""

    @abstractmethod
    def live_artifact_version(self, *, scope: MemoryScope, artifact_id: str) -> tuple[PersistentArtifact, str]:
        """The active registry projection and its canonical version digest."""

    @abstractmethod
    def previous_live_artifact_version(
        self, *, scope: MemoryScope, artifact_id: str, excluding_digest: str
    ) -> tuple[PersistentArtifact, str]:
        """Latest immutable version preceding a repaired projection."""

    @abstractmethod
    def live_artifacts(self, *, scope: MemoryScope, include_quarantined: bool = False) -> list[PersistentArtifact]:
        """Registered artifacts for a scope, newest first."""

    @abstractmethod
    def live_artifact_contribution(
        self, *, scope: MemoryScope, artifact_id: str, minimum_evidence: int
    ) -> ArtifactContributionProjection:
        """Outcome-attributed contribution state for one artifact."""

    @abstractmethod
    def record_live_artifact_outcome(
        self,
        *,
        scope: MemoryScope,
        artifact_id: str,
        positive: bool,
        occurred_at: datetime,
        minimum_evidence: int,
        quarantine_threshold: float,
        task_run_id: str,
        idempotency_key: str,
        payload: dict[str, Any],
    ) -> tuple[ArtifactContributionProjection, bool]:
        """Append one artifact outcome (idempotent) and update quarantine state."""

    # -- coherence repair monitors ---------------------------------------------

    @abstractmethod
    def open_coherence_repair_monitor(
        self,
        *,
        scope: MemoryScope,
        incident_id: str,
        relationship_uuid: str,
        artifact_id: str,
        repaired_artifact_version_digest: str,
        opened_at: datetime,
    ) -> CoherenceRepairMonitor:
        """Start (idempotently) an auditable recovery window after a repair."""

    @abstractmethod
    def coherence_repair_monitor(self, *, scope: MemoryScope, incident_id: str) -> CoherenceRepairMonitor | None:
        """The monitor for one incident, or None."""

    @abstractmethod
    def active_coherence_repair_monitor(
        self, *, scope: MemoryScope, relationship_uuid: str, artifact_id: str
    ) -> CoherenceRepairMonitor | None:
        """The newest still-monitoring monitor for a (relationship, artifact)."""

    @abstractmethod
    def recover_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, recovered_at: datetime
    ) -> CoherenceRepairMonitor:
        """Mark a monitoring window recovered (idempotent once terminal)."""

    @abstractmethod
    def rollback_coherence_repair_monitor(
        self, *, scope: MemoryScope, incident_id: str, reopened_at: datetime
    ) -> CoherenceRepairMonitor:
        """Restore the prior registry projection after a monitored recurrence."""

    # -- policy contracts, shadow stages, and live aliases -----------------------

    @abstractmethod
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

    @abstractmethod
    def policy_contract(self, *, contract_digest: str) -> dict[str, Any]:
        """Return the contract or raise ``ValueError`` if absent."""

    @abstractmethod
    def policy_alias(self, *, scope_key: str, alias: str) -> dict[str, Any] | None:
        """The live alias record, or None."""

    @abstractmethod
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
        """Open a shadow-evaluation window for a certified candidate contract."""

    @abstractmethod
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

    @abstractmethod
    def policy_shadow_stage(self, *, stage_id: str) -> dict[str, Any]:
        """Return the stage or raise ``ValueError`` if absent."""

    @abstractmethod
    def policy_shadow_stages(
        self, *, scope_key: str, alias: str | None = None, limit: int = 20
    ) -> list[dict[str, Any]]:
        """Shadow stages for a scope, newest first."""

    @abstractmethod
    def activate_policy_alias(
        self, *, scope_key: str, alias: str, candidate_contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """Atomically move a live alias to a certified, shadow-clean contract."""

    @abstractmethod
    def initialize_policy_alias(
        self, *, scope_key: str, alias: str, contract_digest: str, now: datetime
    ) -> dict[str, Any]:
        """One-time registration of the already-live baseline for a new alias."""

    @abstractmethod
    def rollback_policy_alias(self, *, scope_key: str, alias: str, now: datetime) -> dict[str, Any]:
        """Atomically restore the alias predecessor without touching memory rows."""

    # -- the per-key registry (DW-026 / DW-030) -----------------------------------

    @abstractmethod
    def bind_key_principal(
        self,
        *,
        key_alias: str,
        principal_id: str,
        tenant_id: str,
        agent_id: str | None = None,
        role: str = "user",
        default_scope_key: str | None = None,
        allowed_scope_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        """Bind a gateway ``key_alias`` to the principal it resolves to.

        ``key_alias`` is unique **globally**, not per tenant, and that is the point.
        DW-026 is safe because the gateway itself enforces global alias uniqueness --
        DW-027 attempted forgery three ways and all three were refused -- so an alias
        identifies exactly one caller. The schema mirrors that guarantee rather than
        restating it in a comment: binding an alias already held by a DIFFERENT tenant
        raises. See DW-030 for why this is not a row in ``tenant_agents``, whose
        uniqueness is tenant-scoped.

        Re-binding the same alias within the same tenant is the ROTATION path and
        updates in place: regenerating a gateway key keeps its alias and changes only
        the key material, so a rotation must not read as a collision.
        """

    @abstractmethod
    def principal_for_key_alias(self, key_alias: str) -> dict[str, Any] | None:
        """The principal a ``key_alias`` resolves to, or ``None`` if unbound.

        **Takes no tenant**, deliberately: at authentication time the caller's tenant is
        exactly what is unknown, and finding it is this method's job. That is the second
        reason ``tenant_agents`` could not host this -- it is keyed ``(tenant_id,
        agent_id)`` and every query against it is tenant-scoped.

        ``None`` rather than a raise: an unrecognised caller is unauthenticated, not a
        storage fault, and the two must stay distinguishable to a caller that fails closed.
        """

    @abstractmethod
    def unbind_key_principal(self, key_alias: str) -> bool:
        """Revoke a binding. ``True`` if one was removed, ``False`` if there was none."""

    # -- tenant identity and configuration ---------------------------------------

    @abstractmethod
    def register_tenant_agent(
        self, *, tenant_id: str, agent_id: str, name: str, source: str = "runtime"
    ) -> dict[str, Any]:
        """Register (or refresh) a tenant-local agent identity; enforces
        tenant-local uniqueness of both agent_id and agent name."""

    @abstractmethod
    def tenant_agent(self, *, tenant_id: str, agent_id: str) -> dict[str, Any] | None:
        """One registered agent, or None."""

    @abstractmethod
    def tenant_agents(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All registered agents for a tenant, oldest first."""

    @abstractmethod
    def set_agent_motive_assignment(
        self, *, tenant_id: str, agent_id: str, motive_name: str, source: str
    ) -> dict[str, str]:
        """Upsert the motive assigned to a registered agent."""

    @abstractmethod
    def agent_motive_assignment(self, *, tenant_id: str, agent_id: str) -> dict[str, str] | None:
        """The motive assignment for one agent, or None."""

    @abstractmethod
    def agent_motive_assignments(self, tenant_id: str) -> tuple[dict[str, str], ...]:
        """All motive assignments for a tenant, by agent id."""

    @abstractmethod
    def set_tenant_prompt_override(
        self,
        *,
        tenant_id: str,
        prompt_profile: str,
        prompt_profile_version: str,
        override: dict[str, Any],
    ) -> dict[str, Any]:
        """Upsert the tenant's prompt-profile override."""

    @abstractmethod
    def tenant_prompt_override(self, tenant_id: str) -> dict[str, Any] | None:
        """The tenant's prompt override, or None."""

    @abstractmethod
    def save_tenant_prompt_version(
        self,
        *,
        tenant_id: str,
        prompt_text: str,
        motive_name: str = "",
        source_profile: str = "",
        source_profile_version: str = "",
    ) -> dict[str, Any]:
        """Append a new tenant prompt version and mark it active."""

    @abstractmethod
    def active_tenant_prompt_version(self, tenant_id: str) -> dict[str, Any] | None:
        """The active tenant prompt version, or None."""

    @abstractmethod
    def tenant_prompt_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All tenant prompt versions, newest first."""

    @abstractmethod
    def save_project_memory_config(
        self, *, tenant_id: str, config: dict[str, Any], configured_by: str
    ) -> dict[str, Any]:
        """Append a new project memory-config version and mark it active."""

    @abstractmethod
    def active_project_memory_config(self, tenant_id: str) -> dict[str, Any] | None:
        """The active project memory config, or None."""

    @abstractmethod
    def project_memory_config_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        """All project memory-config versions, newest first."""

    # -- governance keys (erasure / crypto-shred) ---------------------------------

    @abstractmethod
    def get_or_create_governance_key(self, scope_key: str, subject_key: str = "") -> bytes:
        """Return (or generate) the per-scope content DEK; persisted only in
        KEK-wrapped form.  A crypto-shredded scope raises
        ``ContentKeyUnavailableError``."""

    @abstractmethod
    def shred_governance_key(self, scope_key: str, subject_key: str = "") -> bool:
        """Destroy the wrapped DEK (the cryptographic erase); True if one existed."""

    @abstractmethod
    def get_governance_key(self, scope_key: str, subject_key: str = "") -> bytes | None:
        """The unwrapped DEK, or None if shredded or never created."""

    @abstractmethod
    def governance_key_state(self, scope_key: str, subject_key: str = "") -> dict[str, Any] | None:
        """Key-lifecycle facts for the erasure certificate; never key material."""

    @abstractmethod
    def reveal(self, scope_key: str, value: Any) -> Any:
        """Decrypt-on-read for a stored content field (plaintext passes through;
        shredded scopes resolve to the shredded placeholder)."""

    @abstractmethod
    def reveal_vector(self, scope_key: str, value: Any) -> list[float] | None:
        """Decrypt-on-read for stored embedding vectors (None once shredded)."""

    @abstractmethod
    def attest_formation_contract(
        self, *, contract_digest: str, certification_verdict: str = "uncertified"
    ) -> FormationContractAttestation:
        """Sign a DSSE/in-toto verdict with the store-local wrapped signing key."""

    @abstractmethod
    def formation_signing_key_status(self) -> Any:
        """Can this process's KEK still produce the store's DSSE signer? Never raises.

        Declared here and implemented only on ``SharedGovernancePlaneMixin`` -- the same shape
        as ``seal_receipt_detail`` below. Defining it on an engine plane as well would make
        ``api_surface._mro_shadows`` fail on a name two classes claim.
        """

    # -- tenant LLM credentials ----------------------------------------------------

    @abstractmethod
    def set_tenant_llm_credentials(
        self,
        *,
        tenant_id: str,
        provider: str,
        api_key: str,
        base_url: str = "",
        model: str = "",
        embedding_provider: str | None = None,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
    ) -> dict[str, Any]:
        """Seal and upsert a tenant's LLM credentials.

        The ``embedding_*`` arguments are tri-state: ``None`` PRESERVES the tenant's current
        embedding endpoint, a value sets it, ``""`` clears it. Resolve them with
        ``storage._shared._governance.resolve_embedding_endpoint`` -- the rule is shared, only
        the row read is per-engine.

        THESE THREE ARE PART OF THE CONTRACT, and were not (#163). SQLite grew them in
        WS-17 T18 and Postgres did not, so the two engines answered different signatures while
        this declaration still described the older, smaller one -- which is precisely why
        nothing caught the drift until `_migrate_llm_credentials` raised TypeError against a
        Postgres store. An engine may not extend past this signature; widen it here first.
        """

    @abstractmethod
    def tenant_llm_credentials(self, tenant_id: str) -> dict[str, Any] | None:
        """The unsealed credentials, or None; raises if the key was shredded."""

    @abstractmethod
    def tenant_llm_credential_state(self, tenant_id: str) -> dict[str, Any] | None:
        """Credential metadata without the key material, or None."""

    @abstractmethod
    def clear_tenant_llm_credentials(self, tenant_id: str) -> bool:
        """Delete stored credentials; True if any existed."""

    # -- logical-operation bracketing ---------------------------------------------

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """Bracket one logical operation's Operational Store writes.

        Everything written through this backend inside the bracket commits
        together at the outermost exit (or rolls back together if the block
        raises).  Re-entrant per calling thread: nested brackets join the
        outermost one.  See the module docstring for the rules: never hold a
        bracket across an LLM or network call, and a Memory Graph on a
        different engine does not join it (its writes stay idempotent per the
        cross-store rule).

        This is the contract's only write bracket.  The work-claim primitives
        (see the class docstring's reserved surface) acquire and release their
        claims inside this bracket rather than introducing a parallel
        exclusive-transaction primitive — two bracket mechanisms on one
        connection cannot compose.
        """

    # -- cross-store maintenance -------------------------------------------------

    @abstractmethod
    def purge_tenant_state(
        self,
        tenant_id: str,
        *,
        agent_ids: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Reset generated state for a hosted tenant without deleting raw episodes.

        Cross-store operation: anchors in the Operational Store and drives the
        Memory Graph deletions through the idempotent graph-plane primitives
        (:meth:`MemoryGraphStorage.delete_nodes` /
        :meth:`MemoryGraphStorage.delete_relationships`), per the cross-store
        rule.  Not atomic across the two stores; safe to re-run after a
        partial failure.
        """

    # -- exclusive critical sections -------------------------------------------

    @abstractmethod
    def exclusive_write_transaction(self) -> AbstractContextManager[None]:
        """Hold a store-wide write lock across a read-then-write critical section,
        so "check a condition, then act on it" is atomic against a concurrent
        writer.  Re-entrant, and composes with :meth:`transaction`."""

    # -- WS-17 #17: the dream worker's claim ledger ----------------------------

    @abstractmethod
    def claim_episodes(
        self,
        episode_uuids: Sequence[str],
        *,
        consumer_key: str,
        run_uuid: str,
        now: datetime,
        stale_after_seconds: float = 900.0,
        limit: int | None = None,
    ) -> set[str]:
        """Atomically claim up to *limit* unprocessed episodes for one run."""

    @abstractmethod
    def claim_scope_work(
        self,
        *,
        work_kind: str,
        scope_key: str,
        consumer_key: str,
        run_uuid: str,
        now: datetime,
        stale_after_seconds: float = 900.0,
    ) -> bool:
        """Atomically claim one scope's *work_kind* pass for one run."""

    @abstractmethod
    def release_dream_claims(self, run_uuid: str) -> int:
        """Release every claim held by *run_uuid*; returns the count released."""

    # -- WS-15 event idempotency / lineage reads -------------------------------

    @abstractmethod
    def use_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> UseEvent | None:
        """The use event already stored under (scope, idempotency_key), if any."""

    @abstractmethod
    def outcome_event_for_idempotency_key(self, *, scope_key: str, idempotency_key: str) -> OutcomeEvent | None:
        """The outcome event already stored under (scope, idempotency_key), if any."""

    @abstractmethod
    def use_events_for_task_prefix(self, *, scope_key: str, task_run_id_prefix: str) -> list[UseEvent]:
        """WS-15 T8: indexed, LIKE-escaped prefix read over one scope's use events."""

    # -- WS-19 object-level promotion endorsements -----------------------------

    @abstractmethod
    def record_promotion_endorsement(
        self,
        *,
        candidate_episode_uuid: str,
        agent_id: str,
        rationale: str,
        endorsed_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Record one agent's endorsement of a promotion candidate (idempotent per agent)."""

    @abstractmethod
    def promotion_endorsements_for(self, candidate_episode_uuid: str) -> tuple[dict[str, Any], ...]:
        """Every endorsement for one candidate, in deterministic vote order."""

    @abstractmethod
    def promotion_endorsement_count(self, candidate_episode_uuid: str) -> int:
        """Distinct endorsing agents for one candidate."""

    # -- tenant-state introspection + governance seal --------------------------

    @abstractmethod
    def known_tenant_ids(self) -> tuple[str, ...]:
        """Every tenant id this store holds persisted tenant state for."""

    @abstractmethod
    def deregister_tenant_agent(self, *, tenant_id: str, agent_id: str) -> bool:
        """Detach one agent from a tenant WITHOUT touching its agent-scope memory."""

    @abstractmethod
    def seal_receipt_detail(self, scope_key: str, text: str) -> str | None:
        """WS-23 M1: AES-GCM seal one diverted receipt detail, or None post-shred."""

    # -- lifecycle -------------------------------------------------------------

    @abstractmethod
    def close(self) -> None:
        """Release engine resources; the instance must not be used afterwards."""


class StorageBackend(MemoryGraphStorage, OperationalStorage, ABC):
    """The full store surface: the union of both stores.

    Callers (client, maintenance, retrieval) depend on this type and never on
    an engine class.  A single-engine deployment implements it directly; a
    split deployment satisfies it via
    ``memotron.storage.composite.SplitStorageBackend``.
    """
