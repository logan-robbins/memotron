"""The in-memory bounded node graph, and the journal that records every write.

Two classes, composed rather than fused:

* :class:`InMemoryGraph` -- the single ``GraphStore`` implementation for this
  issue: nodes, typed relations, kNN, expansion, the dirty set, merge and split.
  It knows nothing about journalling.
* :class:`JournaledGraph` -- wraps ANY ``GraphStore`` and records one
  :class:`~memotron.caveman.models.DreamEvent` per mutation, with the content
  before and after. Reads pass straight through.

There is deliberately **no second in-memory store**. Every work package's tests
use this one, so there is nothing to diverge -- the alternative, a test-local fake
``GraphStore`` beside a real one, is the duplicate-implementation smell, and the
merge/split tests need the real relation re-pointing for their assertions to
mean anything at all.

.. rubric:: Compose, do not embed

The journal wraps; it does not reach in. That is what makes "every mutation is
recorded" checkable rather than a convention: a store with no journalling code
cannot forget to journal, and a journal that only ever sees the public seam
cannot record something a caller could not also have done. It also means the
property-graph implementation this seam is written for inherits the journal
unchanged.

.. rubric:: What this store enforces, and what it refuses to

**Enforced, fail-fast:**

* Embeddings written through ``create_node`` / ``replace_facts`` /
  ``merge_nodes`` are unit-norm within :data:`~memotron.caveman.tokens.NORM_TOLERANCE`.
  ``embedding.cosine_similarity`` is a bare dot product that *assumes*
  normalisation (``embedding.py:262-269``), so an unnormalised vector would
  silently halve every similarity rather than fail.
* A relation's endpoints exist, are in one scope, and are distinct. A
  self-loop asserts nothing a fact cannot state better, and an edge to a node
  that is not there would corrupt ``neighbours``.
* An edge type matches ``models.EDGE_TYPE_PATTERN`` on every path that can set
  one, ``rename_edge_type`` included -- ``model_copy`` does not re-validate, so
  the check is explicit where a copy would otherwise smuggle a bad label in.

**Deliberately not enforced:**

* ``N`` and ``M``. The store reports ``count`` and ``edge_types``; the dreamer
  enforces both bounds. Two enforcers would be two places the invariant could be
  applied differently.
* ``L`` and the per-fact paragraph guard. ``render.validate_fact_text`` owns
  those, because they need the motive's budgets and the store has no motive.
* Name uniqueness within a scope. Nothing in the design guarantees it --
  ``merge_nodes`` takes a survivor name that could collide -- so instead of
  refusing a write the design permits, :meth:`InMemoryGraph.node_by_name`
  resolves a collision deterministically (lowest node id).

.. rubric:: Why a merge re-points its relations instead of dropping them

Because an edge now carries its own claim. The old store dropped every edge
touching a merge and left the caller to rebuild weights from
``ledger.cooccurrence`` -- which worked precisely because a weight was derivable.
A claim is not: the ledger holds the sentence, but which pair of nodes it holds
between was the matcher's decision, so dropping the edge would destroy a belief
nobody could re-derive. So :meth:`InMemoryGraph.merge_nodes` re-points, drops
the self-loops a merge necessarily creates, and unions two edges that become
one triple.
"""

from __future__ import annotations

import math
import re
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from memotron.caveman.errors import BudgetExceeded, NodeNotFound
from memotron.caveman.models import (
    EDGE_TYPE_PATTERN,
    DreamEvent,
    DreamOp,
    Fact,
    Node,
    Receipt,
    ReceiptOp,
    Relation,
    SplitSpec,
    merged_aliases,
    normalize_aliases,
)
from memotron.caveman.receipts import canonical_digest, new_receipt_id
from memotron.caveman.seams import Clock, GraphStore, LedgerStore, ReceiptSink
from memotron.caveman.tokens import NORM_TOLERANCE
from memotron.embedding import cosine_similarity

NODE_ID_PREFIX = "n-"
"""Node ids are ``n-<seq>``, zero-padded to three digits.

Which is also ``Node.ledger_key`` today, so ``ledger(node.ledger_key)`` is
``for_node(node_id)``. The key is a separate field because a future shared
ledger will key differently.
"""

RELATION_ID_PREFIX = "r-"
"""Relation ids are ``r-<seq>``, zero-padded to three digits.

A separate sequence from the node one, and a separate prefix, so an id in a
rendered relation or in an error message says on sight which kind of thing it
names. ``retire_relation`` takes one of these and nothing else does.
"""

EVENT_ID_PREFIX = "ev-"
"""Journal event ids are ``ev-`` and a random hex suffix.

Random rather than a per-wrapper counter, which is what this was and which
collided. The journal's ids are unique **per ledger**, and a ledger routinely
outlives the wrapper writing to it: two memories composed over one store (an
ingest phase and a dream phase), and a server restarting against a sqlite file
it already wrote, both restart a counter at one and hit the table's UNIQUE
constraint on the second event.

Ordering does not depend on the id -- ``LedgerStore.events`` orders by
``(ts, insertion sequence)`` -- so there is nothing a monotonic id bought, and it
is minted the same way an entry id and a receipt id already are.
"""

_EDGE_TYPE = re.compile(EDGE_TYPE_PATTERN)

_Triple = tuple[str, str, str, str]
"""``(scope, source_id, type, target_id)`` -- a relation's identity for an upsert."""


def _triple_of(relation: Relation) -> _Triple:
    """The upsert identity of a stored relation. One spelling, used by every lookup."""
    return (relation.scope, relation.source_id, relation.type, relation.target_id)


def _union(first: Sequence[str], second: Sequence[str]) -> tuple[str, ...]:
    """*first* then whatever of *second* it does not already hold, order preserved.

    Evidence accumulates in arrival order: the entry that first stated a belief
    stays at the front, and a reinforcement appends. Order-preserving rather than
    sorted because "when did this become known" is readable from the sequence and
    is not readable from a sorted set.
    """
    merged = list(first)
    seen = set(first)
    for value in second:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
    return tuple(merged)


class InMemoryGraph:
    """A bounded node graph held in dictionaries. Satisfies ``GraphStore``.

    Node and relation ids are minted per store, not per scope, so
    ``get_node(node_id)`` needs no scope argument and one store can hold several
    scopes without collisions.
    """

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._relations: dict[str, Relation] = {}
        self._sequence = 0
        self._relation_sequence = 0

    # -- ids ---------------------------------------------------------------

    def _next_node_id(self) -> str:
        self._sequence += 1
        return f"{NODE_ID_PREFIX}{self._sequence:03d}"

    def _next_relation_id(self) -> str:
        self._relation_sequence += 1
        return f"{RELATION_ID_PREFIX}{self._relation_sequence:03d}"

    # -- validation --------------------------------------------------------

    @staticmethod
    def _validated_embedding(embedding: Sequence[float], *, what: str) -> tuple[float, ...]:
        """Return the embedding as a tuple, or raise if it is not unit-length."""
        vector = tuple(float(value) for value in embedding)
        norm = math.sqrt(sum(value * value for value in vector))
        if abs(norm - 1.0) > NORM_TOLERANCE:
            raise BudgetExceeded(
                f"{what} embedding must be unit-norm within {NORM_TOLERANCE}, got norm {norm:.6f} "
                f"over {len(vector)} dimensions -- cosine_similarity is a bare dot product and "
                f"would silently mis-rank this node"
            )
        return vector

    @staticmethod
    def _validated_edge_type(value: str) -> str:
        """Return *value* if it is a legal edge type, else raise.

        Explicit rather than left to :class:`~memotron.caveman.models.Relation`
        because ``model_copy`` does not re-validate: every path that SETS a type
        goes through here, so a rename cannot introduce a label a fresh
        construction would have refused.
        """
        if _EDGE_TYPE.fullmatch(value) is None:
            raise ValueError(f"relation type {value!r} must match {EDGE_TYPE_PATTERN} -- UPPER_SNAKE, 2 to 32 chars")
        return value

    def _require(self, node_id: str) -> Node:
        node = self._nodes.get(node_id)
        if node is None:
            raise NodeNotFound(f"no such node: {node_id}")
        return node

    def get_relation(self, relation_id: str) -> Relation:
        """One relation, or raise ``NodeNotFound``.

        On the seam because the journal needs to read what it is about to lose:
        ``retire_relation`` takes an id and no scope, and an event recording only
        that id would journal the deletion of a belief without the belief.
        """
        relation = self._relations.get(relation_id)
        if relation is None:
            raise NodeNotFound(f"no such relation: {relation_id}")
        return relation

    # -- reads -------------------------------------------------------------

    def count(self, *, scope: str) -> int:
        return sum(1 for node in self._nodes.values() if node.scope == scope)

    def get_node(self, node_id: str) -> Node:
        return self._require(node_id)

    def get_nodes(self, node_ids: Sequence[str]) -> list[Node]:
        return [self._nodes[node_id] for node_id in node_ids if node_id in self._nodes]

    def list_nodes(self, *, scope: str) -> list[Node]:
        """Name-ordered, then id-ordered so two nodes sharing a name are still stable."""
        return sorted(
            (node for node in self._nodes.values() if node.scope == scope),
            key=lambda node: (node.name, node.node_id),
        )

    def node_by_name(self, *, scope: str, name: str) -> Node | None:
        """Exact-name lookup. On a collision the lowest node id wins, deterministically.

        Name uniqueness is not an invariant of this design, so a name that
        matches two nodes has to resolve *somewhere*; resolving it the same way
        every time is what stops a read from varying run to run.
        """
        matches = [node for node in self._nodes.values() if node.scope == scope and node.name == name]
        if not matches:
            return None
        return min(matches, key=lambda node: node.node_id)

    def node_by_alias(self, *, scope: str, name: str) -> Node | None:
        """Case-insensitive lookup over ``name`` + ``aliases``. Lowest node id wins.

        The read path's exact-match seed. Case-insensitive because a searcher
        types the identifier, not the stored spelling, and deterministic on a
        collision for the same reason :meth:`node_by_name` is.
        """
        wanted = name.casefold()
        matches = [
            node
            for node in self._nodes.values()
            if node.scope == scope
            and (node.name.casefold() == wanted or any(alias.casefold() == wanted for alias in node.aliases))
        ]
        if not matches:
            return None
        return min(matches, key=lambda node: node.node_id)

    def type_vocabulary(self, *, scope: str) -> list[str]:
        """Existing node types, most frequent first, then alphabetically.

        The alphabetical tie-break matters: ``Counter.most_common`` is insertion
        ordered for ties, and insertion order here follows dict iteration, so
        without it the vocabulary offered to the model would reorder between runs
        and change the prompt without changing the policy.
        """
        counts = Counter(node.type for node in self._nodes.values() if node.scope == scope)
        return [node_type for node_type, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))]

    # -- node writes -------------------------------------------------------

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

        ``ledger_key`` is the id minted here, mirroring :meth:`split_node`'s
        treatment of its parts. It is not a caller argument: the caller cannot
        know the id.

        *aliases* go through ``models.normalize_aliases``, so a caller may pass
        the whole group of surface names that converged here -- including the one
        it chose as *name*, and including two spellings of one name -- and get
        the canonical set.
        """
        vector = self._validated_embedding(embedding, what=f"new node {name!r}")
        node_id = self._next_node_id()
        node = Node(
            node_id=node_id,
            scope=scope,
            name=name,
            aliases=normalize_aliases(name=name, aliases=aliases),
            type=type,
            facts=tuple(facts),
            embedding=vector,
            ledger_key=node_id,
            dirty=True,
            created_at=now,
            last_touched_at=now,
        )
        self._nodes[node_id] = node
        return node

    def add_aliases(self, node_id: str, names: Sequence[str]) -> Node:
        """Union surface names into this node's aliases. **Only ``reconcile``.**

        Does not touch ``last_touched_at``: an alias is a routing key rather than
        content, and the header's ``as of`` date reports when the node's content
        was last written.
        """
        node = self._require(node_id)
        updated = node.model_copy(
            update={"aliases": normalize_aliases(name=node.name, aliases=(*node.aliases, *names))}
        )
        self._nodes[node_id] = updated
        return updated

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

        Does NOT clear the dirty flag: ``clear_dirty`` is a separate call because
        it stamps ``dreamed_at``, and a dream that wrote facts but then failed
        its own validation must not look as though it completed.
        """
        node = self._require(node_id)
        vector = self._validated_embedding(embedding, what=f"node {node_id}")
        updated = node.model_copy(
            update={
                "facts": tuple(facts),
                "type": type,
                "embedding": vector,
                "last_touched_at": now,
            }
        )
        self._nodes[node_id] = updated
        return updated

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
        """Collapse nodes into one, re-pointing their relations. **Only ``dream``.**

        Every absorbed node's ``name`` and ``aliases`` are unioned into the
        survivor's aliases by ``models.merged_aliases`` -- the same public rule the
        caller used to build *embedding*, so the stored aliases and the vector
        cannot disagree.

        Relations follow the nodes: see :meth:`_repoint`. An edge between two
        nodes of the merge becomes a self-loop and is dropped, which is exactly
        the case a live run produced (a survivor inheriting an edge to the node it
        had just absorbed).
        """
        survivor = self._require(survivor_id)
        absorbed = [self._require(node_id) for node_id in absorbed_ids]
        if survivor_id in absorbed_ids:
            raise ValueError(f"cannot absorb the survivor into itself: {survivor_id}")
        foreign = [node.node_id for node in absorbed if node.scope != survivor.scope]
        if foreign:
            raise ValueError(f"cannot merge across scopes: {', '.join(foreign)} are not in {survivor.scope}")

        vector = self._validated_embedding(embedding, what=f"merged node {name!r}")
        for node in absorbed:
            del self._nodes[node.node_id]

        merged = survivor.model_copy(
            update={
                "name": name,
                "aliases": merged_aliases(name=name, survivor=survivor, absorbed=absorbed),
                "type": type,
                "facts": tuple(facts),
                "embedding": vector,
                "last_touched_at": now,
            }
        )
        self._nodes[survivor_id] = merged
        self._repoint({node.node_id for node in absorbed}, onto=survivor_id, now=now)
        return merged

    def split_node(self, *, node_id: str, parts: Sequence[SplitSpec], now: datetime) -> list[Node]:
        """Replace one node with several. **Only ``dream`` may call this.**

        The parts are created **without embeddings** and dirty, because
        ``SplitSpec`` carries no vector: the design's seam does not pass one, and
        the incremental dream that follows recomputes each part's embedding. Until
        then a part is present in ``list_nodes`` and ``neighbours`` but not yet
        reachable by ``knn`` -- see :meth:`knn`.

        Each part takes the relations its ``relation_ids`` names, re-pointed at
        it. An edge no part claimed goes with the parent, because a split deletes
        the endpoint it hung off.

        Raises:
            ValueError: on zero parts, on a ``relation_ids`` naming an edge that
                does not touch this node, or on two parts claiming one edge. An
                ambiguous claim has no defined resolution, and guessing one would
                make which part keeps a belief depend on iteration order.
        """
        if not parts:
            raise ValueError(f"cannot split {node_id} into zero parts")
        parent = self._require(node_id)

        own = {relation.relation_id for relation in self.relations_of(node_id)}
        claimed: dict[str, str] = {}
        for part in parts:
            for relation_id in part.relation_ids:
                if relation_id not in own:
                    raise ValueError(f"split {node_id}: relation {relation_id} does not touch this node")
                if relation_id in claimed:
                    raise ValueError(
                        f"split {node_id}: relation {relation_id} is claimed by both {claimed[relation_id]!r} "
                        f"and {part.name!r}"
                    )
                claimed[relation_id] = part.name

        del self._nodes[node_id]

        created: list[Node] = []
        for part in parts:
            part_id = self._next_node_id()
            created.append(
                Node(
                    node_id=part_id,
                    scope=parent.scope,
                    name=part.name,
                    type=part.type,
                    facts=(),
                    embedding=(),
                    ledger_key=part_id,
                    dirty=True,
                    created_at=now,
                    last_touched_at=now,
                )
            )
            self._nodes[part_id] = created[-1]
            self._repoint({node_id}, onto=part_id, now=now, only=frozenset(part.relation_ids))
        self._drop_relations_touching({node_id})
        return created

    def delete_nodes(self, node_ids: Sequence[str]) -> None:
        """Remove nodes and every relation touching them. Unknown ids are ignored.

        Ignored rather than refused because erasure's caller works from ledger
        node ids that may already have been deleted by an earlier merge -- asking
        it to pre-filter would put the same check in two places.
        """
        removed = {node_id for node_id in node_ids if self._nodes.pop(node_id, None) is not None}
        if removed:
            self._drop_relations_touching(removed)

    # -- similarity and expansion -----------------------------------------

    def knn(self, *, scope: str, vector: Sequence[float], k: int) -> list[tuple[Node, float]]:
        """The *k* most similar nodes, descending, then by node id.

        Engine-side by contract (``storage/base.py:329-355`` states the rule for
        ``similar_relationships``): a brute-force cosine scan over ``<= N``
        vectors IS the engine here, and the seam does not leak that, so a
        pgvector implementation replaces the scan with no caller changing.

        A node with no embedding is skipped -- that is only ever a freshly split
        part awaiting its first dream, and scoring it 0.0 would rank it against
        real nodes as though it had been measured. Raises on a dimension
        mismatch rather than scoring 0.0: a query embedded in a different vector
        space is a wiring defect, and a silent zero reads as "nothing is
        similar".
        """
        if k < 1:
            raise ValueError(f"k must be at least 1, got {k}")
        query = tuple(float(value) for value in vector)
        scored: list[tuple[Node, float]] = []
        for node in self._nodes.values():
            if node.scope != scope or not node.embedding:
                continue
            if len(node.embedding) != len(query):
                raise ValueError(
                    f"embedding dimension mismatch: query has {len(query)}, node {node.node_id} "
                    f"has {len(node.embedding)} -- these are different vector spaces"
                )
            scored.append((node, cosine_similarity(list(query), list(node.embedding))))
        scored.sort(key=lambda item: (-item[1], item[0].node_id))
        return scored[:k]

    def neighbours(self, node_ids: Sequence[str]) -> list[Node]:
        """Nodes a relation connects to a seed, seeds excluded, each returned once."""
        seeds = set(node_ids)
        found: set[str] = set()
        for relation in self._relations.values():
            for endpoint, other in (
                (relation.source_id, relation.target_id),
                (relation.target_id, relation.source_id),
            ):
                if endpoint in seeds and other not in seeds:
                    found.add(other)
        return sorted(
            (self._nodes[node_id] for node_id in found if node_id in self._nodes),
            key=lambda node: (node.name, node.node_id),
        )

    # -- relations ---------------------------------------------------------

    def _by_triple(self, triple: _Triple) -> Relation | None:
        """The stored relation with this identity, or ``None``.

        A scan rather than an index: a scope holds at most a few hundred edges,
        and an index is a second source of truth that a re-point or a rename has
        to remember to update. Deterministic on the (impossible) tie by taking
        the lowest relation id.
        """
        matches = [relation for relation in self._relations.values() if _triple_of(relation) == triple]
        if not matches:
            return None
        return min(matches, key=lambda relation: relation.relation_id)

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
        """Create or reinforce one typed edge. See ``seams.GraphStore.upsert_relation``.

        Reinforcement is the whole point: the same triple stated again unions its
        evidence and moves ``last_seen`` rather than storing a second edge, so
        ``Relation.evidence`` counts how well attested a belief is instead of how
        many times somebody wrote it down.

        ``claim`` is what says whether the caller is RESTATING the edge. A caller
        that gives one is restating it and its ``until`` is applied as given,
        ``None`` included; a caller that passes ``claim=None`` has no opinion on
        the edge's text and keeps the marker with it.
        """
        self._validated_edge_type(type)
        if not entry_ids:
            raise ValueError(f"relation {source_id} -{type}-> {target_id} needs at least one ledger entry id")
        source = self._require(source_id)
        target = self._require(target_id)
        if source_id == target_id:
            raise ValueError(f"a relation cannot point {source_id} at itself")
        if source.scope != scope or target.scope != scope:
            raise ValueError(
                f"cannot relate across scopes: {source_id} is in {source.scope!r}, {target_id} is in "
                f"{target.scope!r}, and the relation is for {scope!r}"
            )

        existing = self._by_triple((scope, source_id, type, target_id))
        if existing is not None:
            # One statement, two fields: a claim and its marker are what the edge
            # SAYS ("Host header refused, until #245"). So they move together --
            # a caller restating the text states the marker with it, and a caller
            # that declines to restate the text keeps both.
            restating = claim is not None
            updated = existing.model_copy(
                update={
                    "claim": existing.claim if claim is None else claim,
                    "entry_ids": _union(existing.entry_ids, tuple(entry_ids)),
                    "until": until if restating else existing.until,
                    "last_seen": now,
                }
            )
            self._relations[existing.relation_id] = updated
            return updated

        if claim is None:
            raise ValueError(
                f"a new relation {source_id} -{type}-> {target_id} needs a claim -- an edge with no claim "
                f"asserts nothing, and only a reinforcement may pass claim=None"
            )
        relation = Relation(
            relation_id=self._next_relation_id(),
            scope=scope,
            source_id=source_id,
            target_id=target_id,
            type=type,
            claim=claim,
            entry_ids=tuple(entry_ids),
            until=until,
            first_seen=now,
            last_seen=now,
        )
        self._relations[relation.relation_id] = relation
        return relation

    def retire_relation(self, relation_id: str) -> None:
        """Remove one edge. Raises ``NodeNotFound`` on an unknown id."""
        self.get_relation(relation_id)
        del self._relations[relation_id]

    def relations(self, *, scope: str) -> list[Relation]:
        """Every edge in *scope*, ordered by ``(source, type, target)`` then id."""
        return sorted(
            (relation for relation in self._relations.values() if relation.scope == scope),
            key=lambda relation: (*_triple_of(relation)[1:], relation.relation_id),
        )

    def relations_of(self, node_id: str) -> list[Relation]:
        """Every edge touching this node, both directions, in one stable order."""
        return sorted(
            (relation for relation in self._relations.values() if node_id in (relation.source_id, relation.target_id)),
            key=lambda relation: (*_triple_of(relation)[1:], relation.relation_id),
        )

    def edge_types(self, *, scope: str) -> dict[str, int]:
        """Relation types to edge counts, most used first, then alphabetically.

        Ordered for the same reason :meth:`type_vocabulary` is: it is rendered
        into a prompt, and a vocabulary that reorders between runs changes the
        prompt without changing the policy.
        """
        counts = Counter(relation.type for relation in self._relations.values() if relation.scope == scope)
        return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))

    def rename_edge_type(self, *, scope: str, old: str, new: str) -> int:
        """Re-label every ``old`` edge in *scope* as ``new``; returns how many.

        A rename that collides with an edge the pair already holds under *new*
        **folds into it** -- unioning the evidence and keeping the surviving
        edge's claim -- because two edges with one triple is exactly what
        :meth:`upsert_relation` exists to prevent, and a compaction that created
        one would break the invariant it was run to restore.
        """
        self._validated_edge_type(new)
        if old == new:
            raise ValueError(f"cannot rename edge type {old!r} to itself")
        renamed = 0
        for relation in self.relations(scope=scope):
            if relation.type != old:
                continue
            target = self._by_triple((scope, relation.source_id, new, relation.target_id))
            del self._relations[relation.relation_id]
            if target is None:
                self._relations[relation.relation_id] = relation.model_copy(update={"type": new})
            else:
                self._relations[target.relation_id] = target.model_copy(
                    update={
                        "entry_ids": _union(target.entry_ids, relation.entry_ids),
                        "first_seen": min(target.first_seen, relation.first_seen),
                        "last_seen": max(target.last_seen, relation.last_seen),
                    }
                )
            renamed += 1
        return renamed

    def _drop_relations_touching(self, node_ids: Iterable[str]) -> None:
        touched = set(node_ids)
        for relation_id in [
            relation_id
            for relation_id, relation in self._relations.items()
            if relation.source_id in touched or relation.target_id in touched
        ]:
            del self._relations[relation_id]

    def _repoint(self, moved: set[str], *, onto: str, now: datetime, only: frozenset[str] | None = None) -> None:
        """Re-point every edge of *moved* at *onto*, folding what collides.

        Three cases, in the order they arise:

        * the edge becomes a self-loop -- both ends are inside the merge -- and is
          dropped. Nothing relates to itself, and this is the case a real merge
          produces every time it collapses two nodes that pointed at each other.
        * the re-pointed triple already exists, and the two edges are unioned:
          the surviving edge keeps its claim and gains the other's evidence and
          the earlier ``first_seen``.
        * otherwise the edge keeps its id and changes its endpoint, so a
          ``relation_id`` a reader was handed still resolves after a merge.

        *only* restricts the operation to named relation ids, which is how a
        split gives each part the edges it claimed. ``None`` means every edge of
        *moved*.
        """
        for relation in sorted(self._relations.values(), key=lambda item: item.relation_id):
            if only is not None and relation.relation_id not in only:
                continue
            if relation.source_id not in moved and relation.target_id not in moved:
                continue
            source = onto if relation.source_id in moved else relation.source_id
            target = onto if relation.target_id in moved else relation.target_id
            del self._relations[relation.relation_id]
            if source == target:
                continue
            existing = self._by_triple((relation.scope, source, relation.type, target))
            if existing is None:
                self._relations[relation.relation_id] = relation.model_copy(
                    update={"source_id": source, "target_id": target, "last_seen": now}
                )
                continue
            self._relations[existing.relation_id] = existing.model_copy(
                update={
                    "entry_ids": _union(existing.entry_ids, relation.entry_ids),
                    "first_seen": min(existing.first_seen, relation.first_seen),
                    "last_seen": max(existing.last_seen, relation.last_seen, now),
                }
            )

    # -- the dirty set -----------------------------------------------------

    def mark_dirty(self, node_ids: Sequence[str]) -> None:
        """Flag nodes for an incremental dream. Unknown ids are ignored.

        Ignored for the same reason as :meth:`delete_nodes`: erasure marks the
        node ids its deleted entries named, and some of those may already be
        gone.
        """
        for node_id in node_ids:
            node = self._nodes.get(node_id)
            if node is not None and not node.dirty:
                self._nodes[node_id] = node.model_copy(update={"dirty": True})

    def dirty(self, *, scope: str) -> list[Node]:
        return [node for node in self.list_nodes(scope=scope) if node.dirty]

    def clear_dirty(self, node_ids: Sequence[str], *, now: datetime) -> None:
        """Clear the flag and stamp ``dreamed_at``. Unknown ids are ignored.

        The stamp is what ``ledger.for_node(since=node.dreamed_at)`` reads, so it
        is written here rather than by ``replace_facts``: a dream that wrote
        facts and then failed its own validation must not advance the watermark.
        """
        for node_id in node_ids:
            node = self._nodes.get(node_id)
            if node is not None:
                self._nodes[node_id] = node.model_copy(update={"dirty": False, "dreamed_at": now})

    def record_read(self, node_ids: Sequence[str]) -> None:
        """Increment ``read_count``. Never touches facts, so a read is not a write of content."""
        for node_id in node_ids:
            node = self._nodes.get(node_id)
            if node is not None:
                self._nodes[node_id] = node.model_copy(update={"read_count": node.read_count + 1})


def node_content(node: Node) -> dict[str, Any]:
    """What the journal records about a node: its content, never its vector.

    Name, aliases, type and facts -- the things the graph ASSERTS. Not the
    embedding, which is derived from exactly this text, so journalling it would
    double the ledger to record something the content already determines and
    would make a replay need the embedding transport. Not ``read_count``,
    ``dirty`` or the timestamps either: those are bookkeeping about how the node
    has been used, not about what it says.

    Public because ``replay`` (#251 amendment D, package D-A) rebuilds nodes from
    exactly these keys, and a second projection of the same content is how a
    replay starts disagreeing with the journal it reads.
    """
    return {
        "node_id": node.node_id,
        "scope": node.scope,
        "name": node.name,
        "aliases": list(node.aliases),
        "type": node.type,
        "facts": [fact.model_dump(mode="json") for fact in node.facts],
    }


def relation_content(relation: Relation) -> dict[str, Any]:
    """What the journal records about an edge: the whole record, which is content.

    A relation has no derived fields, so this is its full dump -- the claim, the
    evidence, the ``until`` marker and both endpoints.
    """
    return dict(relation.model_dump(mode="json"))


class JournaledGraph:
    """Wraps any ``GraphStore`` and records every mutation as a ``DreamEvent``.

    Satisfies ``GraphStore`` itself, so it goes wherever the store goes and the
    stages do not know it is there. Reads delegate untouched; each write
    delegates, then appends one event to the ledger with the content before and
    after and emits a ``GRAPH_MUTATED`` receipt whose id the event carries.

    .. rubric:: Why this is a wrapper and not a feature of the store

    Because "every mutation is recorded" is then a property of the composition
    rather than of 25 method bodies that each have to remember. ``InMemoryGraph``
    contains no journalling code at all, so it cannot forget; and the next
    ``GraphStore`` -- pgvector, a property graph -- inherits the journal by being
    wrapped, with no second implementation of the recording rule.

    .. rubric:: What is NOT journalled, and why

    ``mark_dirty``, ``clear_dirty`` and ``record_read`` change bookkeeping, not
    what the graph asserts. Journalling them would put "somebody read this" in
    the history of what is true, and a replay applying them would have to invent
    a read.

    .. rubric:: Why three ops record the AFTER relation state as well as the nodes

    ``merge_nodes``, ``split_node`` and ``rename_edge_type`` each do edge work
    the caller did not state: a merge re-points what it absorbs, a split hands
    each part the edges its ``relation_ids`` claimed and drops the rest, and a
    rename folds collisions. So the arguments are not enough to reproduce the
    outcome, and each of the three records the post-mutation relations --
    otherwise a replay would have to re-derive the store's own partition rule,
    which is the second implementation this wrapper exists to avoid.
    """

    def __init__(self, inner: GraphStore, *, ledger: LedgerStore, receipts: ReceiptSink, clock: Clock) -> None:
        self._inner = inner
        self._ledger = ledger
        self._receipts = receipts
        self._clock = clock

    # -- journalling -------------------------------------------------------

    def _next_event_id(self) -> str:
        """A fresh event id, unique per LEDGER. See :data:`EVENT_ID_PREFIX`."""
        return f"{EVENT_ID_PREFIX}{uuid.uuid4().hex}"

    def _record(
        self,
        op: DreamOp,
        *,
        scope: str,
        node_ids: Sequence[str],
        before: Mapping[str, Any],
        after: Mapping[str, Any],
        subject: str,
        detail: str,
    ) -> DreamEvent:
        """Emit the receipt, then append the event that carries its id.

        Receipt first because the event references it: an event naming a receipt
        that was never emitted would be a dangling pointer in the one record the
        replay proof rests on.
        """
        now = self._clock.now()
        receipt_id = new_receipt_id()
        self._receipts.emit(
            Receipt(
                receipt_id=receipt_id,
                op=ReceiptOp.GRAPH_MUTATED,
                ts=now,
                scope=scope,
                subject=subject,
                inputs_digest=canonical_digest(dict(before)),
                outputs_digest=canonical_digest(dict(after)),
                detail=detail[:600],
            )
        )
        event = DreamEvent(
            event_id=self._next_event_id(),
            ts=now,
            scope=scope,
            op=op,
            node_ids=tuple(node_ids),
            before=dict(before),
            after=dict(after),
            receipt_id=receipt_id,
        )
        self._ledger.record_event(event)
        return event

    # -- reads, delegated --------------------------------------------------

    def count(self, *, scope: str) -> int:
        return self._inner.count(scope=scope)

    def get_node(self, node_id: str) -> Node:
        return self._inner.get_node(node_id)

    def get_nodes(self, node_ids: Sequence[str]) -> list[Node]:
        return self._inner.get_nodes(node_ids)

    def list_nodes(self, *, scope: str) -> list[Node]:
        return self._inner.list_nodes(scope=scope)

    def node_by_name(self, *, scope: str, name: str) -> Node | None:
        return self._inner.node_by_name(scope=scope, name=name)

    def node_by_alias(self, *, scope: str, name: str) -> Node | None:
        return self._inner.node_by_alias(scope=scope, name=name)

    def type_vocabulary(self, *, scope: str) -> list[str]:
        return self._inner.type_vocabulary(scope=scope)

    def knn(self, *, scope: str, vector: Sequence[float], k: int) -> list[tuple[Node, float]]:
        return self._inner.knn(scope=scope, vector=vector, k=k)

    def neighbours(self, node_ids: Sequence[str]) -> list[Node]:
        return self._inner.neighbours(node_ids)

    def relations(self, *, scope: str) -> list[Relation]:
        return self._inner.relations(scope=scope)

    def relations_of(self, node_id: str) -> list[Relation]:
        return self._inner.relations_of(node_id)

    def edge_types(self, *, scope: str) -> dict[str, int]:
        return self._inner.edge_types(scope=scope)

    def dirty(self, *, scope: str) -> list[Node]:
        return self._inner.dirty(scope=scope)

    # -- bookkeeping, delegated and deliberately unjournalled --------------

    def mark_dirty(self, node_ids: Sequence[str]) -> None:
        self._inner.mark_dirty(node_ids)

    def clear_dirty(self, node_ids: Sequence[str], *, now: datetime) -> None:
        self._inner.clear_dirty(node_ids, now=now)

    def record_read(self, node_ids: Sequence[str]) -> None:
        self._inner.record_read(node_ids)

    # -- writes, journalled ------------------------------------------------

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
        node = self._inner.create_node(
            scope=scope,
            name=name,
            type=type,
            facts=facts,
            embedding=embedding,
            now=now,
            aliases=aliases,
        )
        self._record(
            DreamOp.NODE_CREATED,
            scope=scope,
            node_ids=(node.node_id,),
            before={},
            after={"node": node_content(node)},
            subject=node.node_id,
            detail=f"created {node.name!r} as {node.type}",
        )
        return node

    def add_aliases(self, node_id: str, names: Sequence[str]) -> Node:
        before = self._inner.get_node(node_id)
        after = self._inner.add_aliases(node_id, names)
        self._record(
            DreamOp.ALIASES_ADDED,
            scope=after.scope,
            node_ids=(node_id,),
            before={"node": node_content(before)},
            after={"node": node_content(after)},
            subject=node_id,
            detail=f"aliases now {list(after.aliases)}",
        )
        return after

    def replace_facts(
        self,
        node_id: str,
        *,
        facts: Sequence[Fact],
        type: str,
        embedding: Sequence[float],
        now: datetime,
    ) -> Node:
        """Delegate, then journal as a rewrite -- or as a retype when only the type moved.

        Two ops for one store method because the two are different decisions and
        a reader of the history should not have to diff the facts to tell them
        apart: ``retype`` is the global pass's repair for a drifting vocabulary,
        and it writes the same facts back. A replay applies them identically.
        """
        before = self._inner.get_node(node_id)
        after = self._inner.replace_facts(node_id, facts=facts, type=type, embedding=embedding, now=now)
        retyped = before.facts == after.facts and before.type != after.type
        self._record(
            DreamOp.NODE_RETYPED if retyped else DreamOp.NODE_REWRITTEN,
            scope=after.scope,
            node_ids=(node_id,),
            before={"node": node_content(before)},
            after={"node": node_content(after)},
            subject=node_id,
            detail=(
                f"retyped {before.type} to {after.type}"
                if retyped
                else f"{len(after.facts)} fact(s), type={after.type}"
            ),
        )
        return after

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
        survivor_before = self._inner.get_node(survivor_id)
        absorbed_before = self._inner.get_nodes(absorbed_ids)
        relations_before = [relation_content(relation) for relation in self._relations_touching(absorbed_ids)]
        after = self._inner.merge_nodes(
            survivor_id=survivor_id,
            absorbed_ids=absorbed_ids,
            name=name,
            type=type,
            facts=facts,
            embedding=embedding,
            now=now,
        )
        self._record(
            DreamOp.NODE_MERGED,
            scope=after.scope,
            node_ids=(survivor_id, *absorbed_ids),
            before={
                "survivor": node_content(survivor_before),
                "absorbed": [node_content(node) for node in absorbed_before],
                "relations": relations_before,
            },
            after={
                "survivor": node_content(after),
                "relations": [relation_content(relation) for relation in self._inner.relations_of(survivor_id)],
            },
            subject=survivor_id,
            detail=f"{', '.join(absorbed_ids)} absorbed into {survivor_id} as {after.name!r}",
        )
        return after

    def split_node(self, *, node_id: str, parts: Sequence[SplitSpec], now: datetime) -> list[Node]:
        before = self._inner.get_node(node_id)
        relations_before = [relation_content(relation) for relation in self._inner.relations_of(node_id)]
        created = self._inner.split_node(node_id=node_id, parts=parts, now=now)
        surviving: dict[str, Relation] = {}
        for part in created:
            for relation in self._inner.relations_of(part.node_id):
                surviving[relation.relation_id] = relation
        self._record(
            DreamOp.NODE_SPLIT,
            scope=before.scope,
            node_ids=(node_id, *(part.node_id for part in created)),
            before={"node": node_content(before), "relations": relations_before},
            after={
                "parts": [node_content(part) for part in created],
                "relations": [relation_content(surviving[key]) for key in sorted(surviving)],
            },
            subject=node_id,
            detail=f"split into {', '.join(part.node_id for part in created)}",
        )
        return created

    def delete_nodes(self, node_ids: Sequence[str]) -> None:
        """Delegate, then journal only what was actually removed.

        Unknown ids are ignored by the store (erasure works from ledger ids that
        a merge may already have consumed), so journalling the request rather
        than the effect would record deletions that did not happen.
        """
        present = self._inner.get_nodes(node_ids)
        if not present:
            self._inner.delete_nodes(node_ids)
            return
        relations_before = [relation_content(relation) for relation in self._relations_touching(node_ids)]
        self._inner.delete_nodes(node_ids)
        self._record(
            DreamOp.NODE_DELETED,
            scope=present[0].scope,
            node_ids=tuple(node.node_id for node in present),
            before={"nodes": [node_content(node) for node in present], "relations": relations_before},
            after={},
            subject=present[0].node_id,
            detail=f"deleted {', '.join(node.node_id for node in present)}",
        )

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
        existing = next(
            (
                relation
                for relation in self._inner.relations_of(source_id)
                if (relation.source_id, relation.type, relation.target_id) == (source_id, type, target_id)
            ),
            None,
        )
        after = self._inner.upsert_relation(
            scope=scope,
            source_id=source_id,
            target_id=target_id,
            type=type,
            claim=claim,
            entry_ids=entry_ids,
            until=until,
            now=now,
        )
        self._record(
            DreamOp.RELATION_UPSERTED,
            scope=scope,
            node_ids=(source_id, target_id),
            before={"relation": relation_content(existing)} if existing is not None else {},
            after={"relation": relation_content(after)},
            subject=after.relation_id,
            detail=(
                f"reinforced {type} to evidence {after.evidence}"
                if existing is not None
                else f"created {type} {source_id} to {target_id}"
            ),
        )
        return after

    def retire_relation(self, relation_id: str) -> None:
        before = self._inner.get_relation(relation_id)
        self._inner.retire_relation(relation_id)
        self._record(
            DreamOp.RELATION_RETIRED,
            scope=before.scope,
            node_ids=(before.source_id, before.target_id),
            before={"relation": relation_content(before)},
            after={},
            subject=relation_id,
            detail=f"retired {before.type} {before.source_id} to {before.target_id}",
        )

    def rename_edge_type(self, *, scope: str, old: str, new: str) -> int:
        before = [relation_content(relation) for relation in self._inner.relations(scope=scope) if relation.type == old]
        renamed = self._inner.rename_edge_type(scope=scope, old=old, new=new)
        self._record(
            DreamOp.EDGE_TYPE_RENAMED,
            scope=scope,
            node_ids=(),
            before={"type": old, "relations": before},
            after={
                "type": new,
                "relations": [
                    relation_content(relation)
                    for relation in self._inner.relations(scope=scope)
                    if relation.type == new
                ],
            },
            subject=f"{old}->{new}",
            detail=f"renamed {renamed} relation(s) from {old} to {new}",
        )
        return renamed

    # -- helpers -----------------------------------------------------------

    def get_relation(self, relation_id: str) -> Relation:
        return self._inner.get_relation(relation_id)

    def _relations_touching(self, node_ids: Sequence[str]) -> list[Relation]:
        """Every relation touching any of *node_ids*, deduped, in relation-id order."""
        found: dict[str, Relation] = {}
        for node_id in node_ids:
            for relation in self._inner.relations_of(node_id):
                found[relation.relation_id] = relation
        return sorted(found.values(), key=lambda relation: relation.relation_id)
