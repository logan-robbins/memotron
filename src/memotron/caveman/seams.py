"""The Protocols this package DEFINES. Nothing it merely uses.

The chat and embedding seams are deliberately **not** redeclared here:
``memotron.synthesis.SynthesisTransport`` (``synthesis.py:39``) already
declares exactly the right chat shape -- ``identifier`` plus ``async
synthesize(prompt, *, system_prompt) -> str``, with a docstring stating that
content validation is the caller's job -- and
``memotron.embedding.EmbeddingTransport`` (``embedding.py:95``) declares the
embedding shape. Defining a fifth chat Protocol would be a near-duplicate. This
module holds only what has no existing owner.

Four Protocols, and one deliberate absence:

* :class:`GraphStore` -- the bounded node graph, with typed relations.
* :class:`LedgerStore` -- the append-only claim ledger, with the event journal.
* :class:`ReceiptSink` -- where decisions are recorded.
* :class:`Clock` -- tz-aware now, so no module reaches for ``datetime.now()``.

No concrete ``Clock`` lives here. ``seams`` declares shapes; the composition
root supplies instances, and every store operation in this package takes its
``now`` as an explicit argument rather than reading a clock, so only the root
needs one at all.

.. rubric:: Why ``knn`` is on the Protocol

Because ``storage/base.py:329-355`` already states the rule for
``similar_relationships``: executed by the engine, "never as a caller-side loop
over the store". The in-memory implementation is a brute-force cosine scan over
``<= N`` vectors -- it IS the engine here -- and the seam does not leak that, so a
pgvector or property-graph implementation replaces the scan without any caller
changing.

.. rubric:: A belief between two concepts is an EDGE (#251 amendment D)

There used to be a textless ``Link`` carrying a co-occurrence weight, rebuilt
from ``ledger.cooccurrence`` after every write. It is gone. What replaced it is
:class:`~memotron.caveman.models.Relation`: a **typed, directed edge with its
own claim text, its own evidence and an optional ``until`` marker**, upserted on
a ``(source, type, target)`` identity so a restatement reinforces rather than
duplicates.

Three consequences worth stating, because each removes a rule the old seam
needed:

* ``set_link_weights`` / ``links`` / ``degree`` are deleted. A node's degree is
  ``len(relations_of(node_id))``, which is the same number without a second
  method that could disagree with it.
* **nothing rebuilds an edge from the ledger after a write.** ``merge_nodes``
  re-points the relations it absorbs, so a dangling endpoint is unrepresentable
  rather than repaired. That is what removed the "rebuild weights AFTER the
  graph write and the ledger re-key" sequencing rule a caller had to remember.
* the edge type is the vocabulary the dreamer bounds at ``M``
  (``CavemanMotive.max_edge_types``), which is what :meth:`GraphStore.edge_types`
  and :meth:`GraphStore.rename_edge_type` exist for.

Evidence is still provably derived: a relation's ``entry_ids`` are ledger entry
ids, so ``evidence`` is a count over the append-only store and not an
incremental counter that can drift.

.. rubric:: Why the journal is a ledger method and not a graph one

:meth:`LedgerStore.record_event` and :meth:`LedgerStore.events` put every graph
mutation in the **durable** store, beside the claims it was decided from. The
graph is in-memory and bounded; the point of a journal is that it outlives and
out-sizes the thing it describes, so a scope's readable state can be rebuilt by
replaying recorded decisions rather than only re-derived by asking a model
again. ``graph.JournaledGraph`` is the writer; the ledger is where it writes.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from memotron.caveman.models import DreamEvent, Fact, LedgerEntry, Node, NodeAlias, Receipt, Relation, SplitSpec


class GraphStore(Protocol):
    """The bounded node graph. At most ``N`` nodes and ``M`` edge types per scope.

    Deliberately NOT ``runtime_checkable``. An ``isinstance`` check against a
    Protocol verifies only that the names exist and says nothing about their
    signatures -- which is the half that actually breaks -- so the conformance
    test compares ``inspect.signature`` across ``__protocol_attrs__`` instead,
    and that works on a plain Protocol. Marking these runtime-checkable would
    only offer a check weak enough to be misleading.

    Three writes reject an embedding whose L2 norm is not 1.0 within
    ``tokens.NORM_TOLERANCE``: ``create_node``, ``replace_facts`` and
    ``merge_nodes``. ``embedding.cosine_similarity`` is a bare dot product that
    *assumes* normalisation, so an unnormalised vector would silently corrupt
    every ranking rather than fail. Both live transports already return unit
    vectors; this is the fail-fast, not a fallback.
    """

    def count(self, *, scope: str) -> int:
        """Nodes in *scope*. The number ``pressure`` is computed against."""
        ...

    def get_node(self, node_id: str) -> Node:
        """One node, or raise ``NodeNotFound``.

        A caller holding an id that does not resolve has stale state, which is a
        defect rather than an empty result.
        """
        ...

    def get_nodes(self, node_ids: Sequence[str]) -> list[Node]:
        """The nodes that exist, in the order asked for. Unknown ids are skipped."""
        ...

    def list_nodes(self, *, scope: str) -> list[Node]:
        """Every node in *scope*, in a stable order."""
        ...

    def node_by_name(self, *, scope: str, name: str) -> Node | None:
        """The node with this exact name, or ``None``."""
        ...

    def node_by_alias(self, *, scope: str, name: str) -> Node | None:
        """The node this surface name routes to, case-insensitively, or ``None``.

        Searches ``name`` and ``aliases`` together, because a searcher asks by
        the name they know and not by the name the dreamer kept. Case-insensitive
        where :meth:`node_by_name` is exact: this is the READ path's exact-match
        seed, and ``c4`` and ``C4`` are one search key even though only one of
        them is the stored spelling.
        """
        ...

    def add_aliases(self, node_id: str, names: Sequence[str]) -> Node:
        """Record surface names that route to this node; returns the node.

        Additive and idempotent -- the union is normalised by
        ``models.normalize_aliases``, so re-recording a name the node already
        has, or one that only differs in case, is a no-op rather than an error.
        **Only ``reconcile`` may call this**: routing a surface name is stage
        2's decision, and a merge's alias union is ``merge_nodes``' own.

        Takes no ``now``: an alias is a routing key, not content, so it is not a
        touch and must not move the ``as of`` date a header renders.
        """
        ...

    def type_vocabulary(self, *, scope: str) -> list[str]:
        """The scope's existing node types, most frequent first.

        Offered to the model as a suggestion, never imposed: types are open
        text, and ``retype`` is the intended repair when the vocabulary drifts.
        """
        ...

    def create_node(
        self,
        *,
        scope: str,
        name: str,
        type: str,
        facts: Sequence[Fact],
        embedding: Sequence[float],
        now: datetime,
        aliases: Sequence[str] = (),
    ) -> Node:
        """Create a node, dirty, awaiting its first dream.

        *aliases* are the other surface names this concept arrived under in the
        batch that created it -- reconcile groups by name, so every name in a
        group but the one chosen is an alias, and each is an exact-match search
        key from the first read onward. Normalised by
        ``models.normalize_aliases`` before the record is built.

        ``reconcile`` calls this with ``facts=()``: it may route, mark dirty and
        create a provisional relation, but it may not write fact text, because
        the first fact set is a compression decision and compression has exactly
        one owner.

        **The store mints ``ledger_key``, not the caller.** ``Node.ledger_key``
        is documented as equalling ``node_id`` today, and the id is minted here,
        so the value is unknowable at call time and no caller could ever supply
        the right one.
        """
        ...

    def replace_facts(
        self,
        node_id: str,
        *,
        facts: Sequence[Fact],
        type: str,
        embedding: Sequence[float],
        now: datetime,
    ) -> Node:
        """Replace a node's whole fact set. **Only ``dream`` may call this.**

        Bumps ``last_touched_at`` and preserves ``created_at``. Replaces
        ``replace_lines`` (#251 amendment D): a fact is a record with a kind and
        evidence, not a sigil-prefixed string.
        """
        ...

    def knn(self, *, scope: str, vector: Sequence[float], k: int) -> list[tuple[Node, float]]:
        """The *k* nearest nodes with their similarities, most similar first.

        Engine-side by contract, not a caller-side loop. Raises on a
        dimension mismatch rather than scoring zero: a query embedded in a
        different space is a wiring defect, and a silent 0.0 would read as
        "nothing is similar".
        """
        ...

    def neighbours(self, node_ids: Sequence[str]) -> list[Node]:
        """Nodes connected to a seed by ANY relation, seeds excluded, each once.

        Direction-blind on purpose: an edge is a belief about a pair, and a
        reader expanding from one end wants the other end whichever way the edge
        was written.
        """
        ...

    def upsert_relation(
        self,
        *,
        scope: str,
        source_id: str,
        target_id: str,
        type: str,
        claim: str | None,
        entry_ids: Sequence[str],
        until: str | None,
        now: datetime,
    ) -> Relation:
        """Create or **reinforce** one typed edge. Returns the stored relation.

        Identity is the triple ``(source_id, type, target_id)``. When it already
        exists the edge is reinforced rather than duplicated: *entry_ids* are
        unioned into its evidence, ``last_seen`` moves to *now*, and its claim is
        kept unless a new one is given. That is the whole reinforcement
        mechanism -- restating a belief makes the record stronger instead of
        making a second record.

        *claim* is ``None`` to mean "keep whatever is there", which is only valid
        on a reinforcement: creating an edge with no claim would store an edge
        that asserts nothing, so it raises.

        **A claim and its** *until* **marker move together**, because they are one
        statement about one edge -- ``Host header refused`` and ``until #245`` are
        two halves of the same sentence. A caller that gives a *claim* is
        restating the edge, so its *until* is applied as given, ``None``
        included, and clearing a marker ("it was fixed, then it regressed") stays
        expressible. A caller that passes ``claim=None`` is adding evidence to a
        belief it has no text for, and keeps the marker with the text.

        That second half is what a reinforcement through ``reconcile`` needs:
        that stage holds the raw ledger claim, which is the right text for a NEW
        edge and the wrong text to overwrite a dreamer's compression with, and it
        has no basis at all for a marker. Splitting the two fields would let every
        restatement of a belief silently erase the compressed version of it.
        """
        ...

    def get_relation(self, relation_id: str) -> Relation:
        """One edge, or raise ``NodeNotFound``.

        On the seam for one caller: ``graph.JournaledGraph`` has to read a
        relation before :meth:`retire_relation` removes it, because an event
        recording only the id would journal the loss of a belief without the
        belief. Nothing else needs it -- a relation is normally reached through
        :meth:`relations_of` or :meth:`relations`.
        """
        ...

    def retire_relation(self, relation_id: str) -> None:
        """Remove one edge. Raises on an unknown id.

        Retirement rather than deletion in name only: the edge leaves the graph
        and its record stays in the journal, so what it asserted is still
        answerable from the ledger.
        """
        ...

    def relations(self, *, scope: str) -> list[Relation]:
        """Every edge in *scope*, in a stable order."""
        ...

    def relations_of(self, node_id: str) -> list[Relation]:
        """Every edge touching this node, both directions, in a stable order.

        Also the node's degree: ``len(relations_of(node_id))``. There is no
        separate ``degree`` method, because two ways to count one thing is two
        numbers that can disagree.
        """
        ...

    def edge_types(self, *, scope: str) -> dict[str, int]:
        """The scope's relation types to how many edges carry each, most used first.

        The vocabulary the dreamer holds at ``M``, and the list the matcher is
        shown so it reuses a type instead of coining one.
        """
        ...

    def rename_edge_type(self, *, scope: str, old: str, new: str) -> int:
        """Re-label every edge of one type; returns how many were re-labelled.

        How the vocabulary is compacted when it exceeds ``M``. A rename that
        collides with an edge the target type already holds between the same pair
        reinforces that edge instead of creating a duplicate, for the same reason
        :meth:`upsert_relation` does.
        """
        ...

    def merge_nodes(
        self,
        *,
        survivor_id: str,
        absorbed_ids: Sequence[str],
        name: str,
        type: str,
        facts: Sequence[Fact],
        embedding: Sequence[float],
        now: datetime,
    ) -> Node:
        """Collapse nodes into one. **Only ``dream`` may call this.**

        Deletes the absorbed nodes and **re-points their relations at the
        survivor**: an edge between two nodes being merged becomes a self-loop
        and is dropped, and two edges that become the same triple are unioned
        into one with the union of their evidence.

        Re-pointing rather than rebuilding is the change amendment D makes. The
        old store dropped every edge and left the caller to rebuild weights from
        ``ledger.cooccurrence``; an edge now carries its own claim, which the
        ledger cannot re-derive, so dropping it would destroy a belief.

        **Unions every absorbed node's ``name`` and ``aliases`` into the
        survivor's ``aliases``** (``models.merged_aliases``). A merge is where a
        searchable name would otherwise be destroyed. The caller computes the
        same union to build *embedding*, which is why the rule is a public
        function rather than a private step in here.
        """
        ...

    def split_node(self, *, node_id: str, parts: Sequence[SplitSpec], now: datetime) -> list[Node]:
        """Replace one node with several. **Only ``dream`` may call this.**

        Deletes the parent. Each part takes the relations its ``relation_ids``
        names, re-pointed at it; an edge no part claimed goes with the parent,
        because a split removes the endpoint it hung off. The caller partitions
        the parent's ledger entries across the parts.
        """
        ...

    def mark_dirty(self, node_ids: Sequence[str]) -> None:
        """Flag nodes as needing an incremental dream. Unknown ids are ignored."""
        ...

    def dirty(self, *, scope: str) -> list[Node]:
        """The nodes awaiting an incremental dream, in a stable order."""
        ...

    def clear_dirty(self, node_ids: Sequence[str], *, now: datetime) -> None:
        """Clear the dirty flag and stamp ``dreamed_at``.

        The stamp is what ``ledger.for_node(since=node.dreamed_at)`` reads, so
        the next incremental dream sees only what arrived after this one.
        """
        ...

    def record_read(self, node_ids: Sequence[str]) -> None:
        """Increment ``read_count``. A term in node value; never changes facts."""
        ...

    def delete_nodes(self, node_ids: Sequence[str]) -> None:
        """Remove nodes and every relation touching them.

        Used by erasure, when a node's every supporting entry is gone: there is
        nothing left to re-dream it from, so re-dreaming would invent content.
        """
        ...


class LedgerStore(Protocol):
    """The append-only claim ledger and the graph's event journal.

    Unbounded, durable, and outside the graph. The graph is fully regenerable
    from this, which is what makes erasure "delete the episode's entries, then
    re-dream" rather than a graph surgery problem -- and, with
    :meth:`record_event`, what makes a scope's readable state **replayable** from
    recorded decisions rather than only re-derivable by asking a model again.

    Nothing here rewrites a claim; the only mutations are binding node ids,
    re-keying on a merge, reassigning on a split, and deleting an episode.
    """

    def append(self, entry: LedgerEntry) -> str:
        """Append one entry; returns its ``entry_id``."""
        ...

    def get(self, entry_id: str) -> LedgerEntry:
        """One entry, or raise. An unknown id is a defect, not an empty result."""
        ...

    def for_node(self, node_id: str, *, since: datetime | None = None, limit: int | None = None) -> list[LedgerEntry]:
        """This node's entries, oldest first.

        ``since`` is **inclusive**: it selects ``ts >= since``. Deliberately, and
        the asymmetry is the point. The dreamer emits a node's COMPLETE new fact
        set rather than a diff, so re-seeing an entry it already saw is
        idempotent and costs one line of prompt -- while missing an entry
        silently loses a claim, which is unrecoverable without a re-dream nobody
        knows to run. An exclusive bound also makes ``for_node(since=dreamed_at)``
        return nothing whenever a caller stamps both from one clock reading,
        which is exactly what a fixed-clock test does.
        """
        ...

    def for_episode(self, episode_id: str) -> list[LedgerEntry]:
        """Every entry this episode produced. The erasure unit."""
        ...

    def unbound(self, *, scope: str) -> list[LedgerEntry]:
        """Entries whose ``node_ids`` is still empty. Stage 2's input queue."""
        ...

    def bind_nodes(self, entry_id: str, node_ids: Sequence[str]) -> None:
        """Attach resolved node ids to an entry. **Only ``reconcile`` may call this.**"""
        ...

    def identifiers(self, *, scope: str) -> dict[str, tuple[str, ...]]:
        """Every ``LedgerEntry.identifiers`` member in *scope* to the node ids carrying it.

        Bound entries only, keys verbatim, both sides sorted. The read path's
        exact-match seed for an identifier query: embedding similarity cannot
        separate ``#245`` from ``#246``, and an identifier is exactly the token a
        searcher does know exactly.
        """
        ...

    def entry_counts(self, *, scope: str) -> dict[str, int]:
        """Entries per node id in *scope*. The ``N entries`` in a node header.

        One statement for the whole read rather than one ``for_node`` per
        emitted node.
        """
        ...

    def cooccurrence(self, node_id: str) -> dict[str, int]:
        """Other node ids sharing an entry with this one, counted.

        Derived on demand and never cached: a cached derivation is a second
        source of truth. No longer written into the graph as an edge weight
        (#251 amendment D) -- ``pressure`` reads it, through a relation's own
        evidence, to choose which node a doomed one is folded into.
        """
        ...

    def turn_cooccurrence(self, *, scope: str) -> dict[tuple[str, str], int]:
        """Unordered node pairs to the number of conversational turns they share.

        The key is ``(a, b)`` with ``a < b`` lexically -- the same orientation
        :func:`pressure.pair_key` uses, so a caller cannot key a pair one way
        here and the other way there. The value is the number of distinct
        ``(episode_id, turn)`` pairs for which BOTH nodes have a bound entry; an
        entry bound to both counts its turns once, and a pair sharing no turn is
        absent rather than present at zero. Bound entries only, for the same
        reason :meth:`identifiers` skips an unbound entry: a node id is what the
        key is made of.

        .. rubric:: Why the graph needs this and ``cooccurrence`` is not enough

        ``cooccurrence`` counts entries naming both nodes, which is what extract
        produced when one *claim* was about both. Two things said in the same
        breath but written down as two claims share no entry at all, so the
        ledger links them with weight 0 -- and ``pressure`` then fell straight
        through to embedding similarity, which is how "fix the platform, never
        downgrade" was folded into ``#245``: nothing in the graph recorded that
        they were said in the same turn. The turn is the weakest real provenance
        the ledger holds, and it ranks above similarity precisely because it is
        provenance rather than resemblance.

        Computed per call and never cached, like every other derivation here.
        """
        ...

    def rekey_node(self, *, from_node_id: str, to_node_id: str) -> list[str]:
        """Move every entry from one node to another; returns the moved entry ids.

        The return value is what a ``NodeAlias`` records, so an un-merge is a
        replay rather than a guess about which entries came from where.
        """
        ...

    def reassign(self, *, entry_ids: Sequence[str], from_node_id: str, node_id: str) -> None:
        """Move specific entries from one node to another. The split half of re-keying.

        .. rubric:: ``from_node_id`` is added to the design document's signature

        The document writes ``reassign(*, entry_ids, node_id)``, and that
        signature cannot express a split without losing data either way:

        * As "replace the entry's node set with ``[node_id]``" it destroys the
          entry's bindings to nodes that were NOT split -- an entry whose
          subjects resolved to both ``chart`` and ``gateway`` would lose the
          gateway binding when ``chart`` splits.
        * As "add ``node_id`` to the entry's set" it leaves the parent binding
          behind. The parent no longer exists in the graph, so the entry would
          count on two parts at once, which the design's own rule ("no entry
          dropped, none duplicated") forbids.

        With the ``from_node_id``, the operation is exactly "these entries left
        that node for this one", which is lossless and is the same operation
        :meth:`rekey_node` performs for *every* entry rather than a subset.
        """
        ...

    def record_alias(self, alias: NodeAlias) -> None:
        """Record a merge so the survivor's history names what it absorbed."""
        ...

    def aliases(self, survivor_node_id: str) -> list[NodeAlias]:
        """Everything this node has absorbed, oldest first."""
        ...

    def record_event(self, event: DreamEvent) -> str:
        """Append one graph mutation to the journal; returns its ``event_id``.

        Called by ``graph.JournaledGraph`` and by nothing else. The journal is
        append-only for the same reason the claims are: a history that can be
        edited proves nothing, and the whole point of recording the mutations is
        that the readable state is reproducible from them.
        """
        ...

    def events(self, *, scope: str, since: datetime | None = None) -> list[DreamEvent]:
        """This scope's mutations, oldest first, in applied order.

        Ordered by time then insertion sequence, the same total order
        :meth:`for_node` uses, because a replay has to apply them in the order
        they happened and a whole pass routinely shares one clock reading.
        ``since`` is inclusive, matching :meth:`for_node`.
        """
        ...

    def delete_episode(self, episode_id: str) -> list[str]:
        """Erase an episode's entries; returns the node ids that need re-dreaming.

        The union of the deleted entries' ``node_ids``. A node whose every entry
        is gone is deleted rather than re-dreamed -- re-dreaming from nothing
        would invent content.
        """
        ...

    def close(self) -> None:
        """Release the underlying connection. Idempotent."""
        ...


class ReceiptSink(Protocol):
    """Where every decision is recorded, accepted and rejected alike."""

    def emit(self, receipt: Receipt) -> str:
        """Record one receipt; returns its ``receipt_id``."""
        ...

    def all(self, *, scope: str | None = None) -> list[Receipt]:
        """Every receipt, in emission order. ``scope=None`` means every scope."""
        ...


class Clock(Protocol):
    """The only source of "now" in this package.

    A seam rather than a call to ``datetime.now`` so a test can pin time
    without patching a module, and so the DTZ lint rule has one place to be
    satisfied. Every store operation takes its timestamp as an argument, so only
    the composition root holds an instance.
    """

    def now(self) -> datetime:
        """A timezone-aware ``datetime``. Naive is never valid here."""
        ...
