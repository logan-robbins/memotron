"""Rebuild a scope's graph from its journal, and prove it matches. **No LLM, no vectors.**

This is what makes compression **provable** rather than merely trusted (#251
amendment D). The ledger holds the claims; the journal holds every mutation
taken over them, with the node and relation content before and after. So a
scope's readable state is not only regenerable in principle -- by asking a model
to dream it again, which would produce different words -- it is rebuildable by
replaying a recorded sequence, and comparable to the live graph by digest.

::

    ledger.events(scope) -> replay -> InMemoryGraph -> content_digest -> replayed_digest
    live GraphStore --------------------------------> content_digest -> live_digest
    ReplayProof = (scope, live_digest, replayed_digest, equal, event_count)

Zero model calls and zero embedding calls: every byte a replay writes came out
of an event. That is the property worth asserting -- a "replay" that had to ask
a model anything would be a re-derivation, and two re-derivations of one scope
do not agree.

.. rubric:: A replay calls the store's own write seam, and that is the point

Every event is applied by the public method that recorded it: ``NODE_MERGED``
replays through ``merge_nodes``, ``EDGE_TYPE_RENAMED`` through
``rename_edge_type``, ``NODE_DELETED`` through ``delete_nodes``. So the store's
own rules do the work -- an edge that became a self-loop is dropped, two edges
that collided are folded, an absorbed node's names are unioned into its
survivor -- rather than this module re-deriving them from the recorded ``after``
state.

Re-deriving them was the alternative and it is worse twice over. It would be a
second implementation of rules that live in ``graph.py``, free to drift from
them; and it would mean writing records into the store's private state, which
``tests/test_storage_backend.py`` rejects for every module outside
``memotron.storage``. Depending on the seam is not a preference here, it is
enforced.

Two consequences follow, and both are properties of the seam rather than
compromises:

* **ids are not preserved.** ``create_node`` and ``upsert_relation`` mint their
  own, so a replayed node carries a fresh id and this module keeps a journalled
  id to replayed id map for the events that name one. That is why
  :func:`content_digest` identifies records by CONTENT and never by id -- see
  below.
* **every replayed node carries** :data:`REPLAY_EMBEDDING`. The seam requires a
  unit-norm vector and the journal deliberately records none: a vector is
  derived from the content, so journalling it would double the ledger and a
  replay that had to reproduce one would need the embedding transport this
  module exists to do without.

.. rubric:: What the digest covers, and what it must not

Names, types, aliases, facts and relations -- what the graph ASSERTS. Not
embeddings, which are derived from exactly that content. Not timestamps, which
are what the clock said rather than what is true: requiring those to match would
make the proof a proof about a clock. Not ``read_count`` or ``dirty``, which are
bookkeeping about how a node has been USED.

**And not record ids.** A relation is identified by the CONTENT of the nodes it
joins rather than by their ids, so the digest is a statement about what the
scope believes and not about where a store happened to put it. Two nodes with
byte-identical content assert the same thing, so a graph that swapped them
asserts the same thing too -- which is the equality this proof is about.

.. rubric:: How a split replays, edges and all

Three of the store's operations do edge work their arguments do not state: a
merge re-points what it absorbs, a split hands each part the edges its
``relation_ids`` claimed and drops the rest, and a rename folds collisions. So
all three journal the post-mutation relation state in ``after``, and a split
replays as ``delete_nodes`` (which drops the parent's edges), ``create_node``
per part, then a restoring upsert per recorded edge. The partition is read out of
the event rather than re-derived here, because re-deriving it would be a second
implementation of a rule that lives in ``graph.py``.

A journal with a GAP in it is still refused rather than half-applied --
:meth:`_Replay.node` raises ``ValueError`` naming the event. ``ValueError``
rather than one of ``caveman.errors``: those name an LLM answering out of
contract, a missing credential and a budget, and this is none of them -- it is
the same fail-fast shape ``read`` uses when two seam methods disagree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.models import DreamEvent, DreamOp, Fact, Node, Relation
from memotron.caveman.receipts import canonical_digest
from memotron.caveman.seams import GraphStore, LedgerStore

REPLAY_EMBEDDING: tuple[float, ...] = (1.0,)
"""The vector every replayed node is written with: one unit-norm placeholder.

The write seam refuses a vector that is not unit-norm, and the journal records
no vector at all, so a replay has to pass something and there is nothing true to
pass. Every replayed node therefore carries the SAME meaningless vector, which
makes the replayed graph readable, renderable and digestible but **not usefully
searchable**: ``knn`` over it would score every node identically, and its one
dimension will not match a real query vector's.

That is correct rather than a limitation. A replayed graph exists to be compared
against the live one, not to answer a query, and a placeholder that scored
plausibly would be the dangerous version of this.
"""


def _facts_of(content: Mapping[str, Any]) -> tuple[Fact, ...]:
    """The facts in one journalled node payload, validated back into records.

    ``Fact`` re-validates on construction, so a payload the journal could not
    have written -- a key on a rule, a ``last_seen`` before its ``first_seen`` --
    fails here rather than becoming a node nobody can explain.
    """
    return tuple(Fact.model_validate(fact) for fact in content.get("facts", ()))


@dataclass
class _Replay:
    """One replay in progress: the graph being built and the id maps into it.

    The maps are the whole reason this is a small class rather than a function:
    the store mints its own ids, so every event that names a journalled id has to
    be translated, and threading two dicts through ten branches would put the
    translation in ten places.
    """

    graph: InMemoryGraph
    nodes: dict[str, str] = field(default_factory=dict)
    """Journalled node id to the id the replayed store minted for it."""

    relations: dict[str, str] = field(default_factory=dict)
    """Journalled relation id to the replayed one. Filled by every upsert."""

    def node(self, journalled_id: str) -> str:
        """The replayed id for a journalled one.

        Raises:
            ValueError: if the journal mutates a node it never recorded the
                creation of. The events are a complete history by construction,
                so a gap is a corrupted journal and not a case to skip past.
        """
        replayed = self.nodes.get(journalled_id)
        if replayed is None:
            raise ValueError(
                f"the journal mutates node {journalled_id} without ever having created it -- "
                f"this scope's event history is incomplete and cannot be replayed"
            )
        return replayed

    def create(self, content: Mapping[str, Any], *, event: DreamEvent) -> Node:
        """Create one node from its journalled content and record its new id."""
        created = self.graph.create_node(
            scope=str(content["scope"]),
            name=str(content["name"]),
            type=str(content["type"]),
            facts=_facts_of(content),
            embedding=REPLAY_EMBEDDING,
            now=event.ts,
            aliases=tuple(content.get("aliases", ())),
        )
        self.nodes[str(content["node_id"])] = created.node_id
        return created

    def restore_relation(self, content: Mapping[str, Any], *, event: DreamEvent) -> Relation:
        """Write one journalled relation payload through the store's upsert seam.

        One method for the two events that carry relation content in their
        ``after``: a plain upsert, and a split whose parts inherited the edges
        they claimed. A restored edge keeps the JOURNALLED id in
        :attr:`relations` mapped to the replayed one, so a later
        ``RELATION_RETIRED`` naming it still resolves -- a split re-points an edge
        without changing its id, and after a replay it is a different record with
        the same journalled name.
        """
        stored = self.graph.upsert_relation(
            scope=str(content["scope"]),
            source_id=self.node(str(content["source_id"])),
            target_id=self.node(str(content["target_id"])),
            type=str(content["type"]),
            claim=str(content["claim"]),
            entry_ids=list(content["entry_ids"]),
            until=content["until"],
            now=event.ts,
        )
        self.relations[str(content["relation_id"])] = stored.relation_id
        return stored


def _apply(event: DreamEvent, state: _Replay) -> None:
    """Apply one journalled mutation through the store's own write seam.

    One branch per :class:`~memotron.caveman.models.DreamOp`, and each branch
    passes the recorded AFTER content as the method's arguments. Where a method
    does more than it is told -- a merge re-points edges, a rename folds
    collisions -- that work is the store's and is not repeated here.

    Raises:
        ValueError: on any event that mutates a record no earlier event created.
            The events are a complete history by construction, so a gap is a
            corrupted journal rather than a case to skip past.
    """
    graph = state.graph
    match event.op:
        case DreamOp.NODE_CREATED:
            state.create(event.after["node"], event=event)

        case DreamOp.ALIASES_ADDED:
            content = event.after["node"]
            graph.add_aliases(state.node(str(content["node_id"])), list(content.get("aliases", ())))

        case DreamOp.NODE_REWRITTEN | DreamOp.NODE_RETYPED:
            content = event.after["node"]
            graph.replace_facts(
                state.node(str(content["node_id"])),
                facts=_facts_of(content),
                type=str(content["type"]),
                embedding=REPLAY_EMBEDDING,
                now=event.ts,
            )

        case DreamOp.NODE_MERGED:
            survivor = event.after["survivor"]
            graph.merge_nodes(
                survivor_id=state.node(str(survivor["node_id"])),
                absorbed_ids=[state.node(str(node["node_id"])) for node in event.before.get("absorbed", ())],
                name=str(survivor["name"]),
                type=str(survivor["type"]),
                facts=_facts_of(survivor),
                embedding=REPLAY_EMBEDDING,
                now=event.ts,
            )

        case DreamOp.NODE_SPLIT:
            # Deleting the parent drops every edge that hung off it, including the
            # ones a part claimed -- so the parts are created and then the recorded
            # post-split edges are restored, which is the store's own partition
            # rather than this module's guess at it.
            graph.delete_nodes([state.node(str(event.before["node"]["node_id"]))])
            for part in event.after.get("parts", ()):
                state.create(part, event=event)
            for content in event.after.get("relations", ()):
                state.restore_relation(content, event=event)

        case DreamOp.NODE_DELETED:
            graph.delete_nodes([state.node(str(node["node_id"])) for node in event.before.get("nodes", ())])

        case DreamOp.RELATION_UPSERTED:
            state.restore_relation(event.after["relation"], event=event)

        case DreamOp.RELATION_RETIRED:
            journalled = str(event.before["relation"]["relation_id"])
            replayed = state.relations.get(journalled)
            if replayed is None:
                raise ValueError(
                    f"event {event.event_id} retires relation {journalled}, which no earlier event created -- "
                    f"this scope's event history is incomplete and cannot be replayed"
                )
            graph.retire_relation(replayed)

        case DreamOp.EDGE_TYPE_RENAMED:
            graph.rename_edge_type(scope=event.scope, old=str(event.before["type"]), new=str(event.after["type"]))


def replay(*, scope: str, ledger: LedgerStore) -> InMemoryGraph:
    """Rebuild *scope*'s graph by applying its journal in order. **No LLM, no embedding.**

    ``LedgerStore.events`` returns the scope's mutations oldest first over a
    total order -- ``(ts, insertion sequence)``, not ``ts`` alone, because a whole
    dream pass is routinely stamped from one clock reading and a replay that
    applied a merge before the node it merges would not be a replay.

    The returned graph answers ``list_nodes``, ``relations``, ``edge_types`` and
    every rendering. Its node ids are its own, not the journalled ones, and its
    vectors are placeholders (:data:`REPLAY_EMBEDDING`) -- it is built to be
    compared, not queried.

    An empty journal returns an empty graph rather than raising: a scope nothing
    has written is a real state, and its digest matches a live graph that is also
    empty.

    Raises:
        ValueError: on a ``node_split`` event the journal cannot reproduce, or on
            an event that mutates a record no earlier event created. Both mean
            the history is not a history.
        ValidationError: if a journalled payload is not a valid record. A journal
            that cannot be read back is a corrupted proof, which is worth an
            exception rather than a partial graph.
    """
    state = _Replay(graph=InMemoryGraph())
    for event in ledger.events(scope=scope):
        _apply(event, state)
    return state.graph


def _node_payload(node: Node) -> dict[str, Any]:
    """What one node ASSERTS: its name, its type, its aliases and its facts.

    No id, no vector, no timestamps, no ``dirty`` and no ``read_count`` -- see
    the module docstring on each.
    """
    return {
        "name": node.name,
        "type": node.type,
        "aliases": list(node.aliases),
        "facts": [
            {"kind": fact.kind.value, "key": fact.key, "text": fact.text, "entry_ids": list(fact.entry_ids)}
            for fact in node.facts
        ],
    }


def _relation_payload(relation: Relation, *, endpoints: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
    """What one edge ASSERTS, with both endpoints named by their CONTENT.

    By content rather than by id, because a replay's ids are its own: an edge
    identified by ``n-004`` would compare two stores' bookkeeping, and an edge
    identified by what sits at each end compares the belief.

    Raises:
        ValueError: if an endpoint is not among the scope's nodes. Every path
            that removes a node removes the edges touching it, and
            ``upsert_relation`` refuses an endpoint that does not exist or that
            is in another scope -- so a dangling edge is a store defect, and a
            digest is not the place to paper one over.
    """
    for node_id in (relation.source_id, relation.target_id):
        if node_id not in endpoints:
            raise ValueError(
                f"relation {relation.relation_id} names {node_id}, which is not a node of "
                f"{relation.scope!r} -- this graph holds a dangling edge and cannot be digested"
            )
    return {
        "source": endpoints[relation.source_id],
        "type": relation.type,
        "target": endpoints[relation.target_id],
        "claim": relation.claim,
        "until": relation.until,
        "entry_ids": list(relation.entry_ids),
    }


def _canonically_ordered(payloads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """The payloads sorted by their own canonical encoding.

    Sorting by the encoding rather than by a field, so the order is a function of
    the whole record and no two stores' iteration orders can digest differently.
    """
    return sorted(payloads, key=canonical_digest)


def content_digest(graph: GraphStore, scope: str) -> str:
    """sha256 over everything *scope* ASSERTS: names, types, aliases, facts, relations.

    No ids, no embeddings, no timestamps -- see the module docstring. Both planes
    are ordered by their own content, so the digest is a function of what the
    scope believes rather than of either store's addressing or iteration order,
    and two stores holding one scope digest identically.

    Positional arguments, unlike most of this package, because this is a pure
    function of a store and a scope in the same way ``len`` is of a sequence.

    Raises:
        ValueError: on a dangling edge -- see :func:`_relation_payload`.
    """
    nodes = graph.list_nodes(scope=scope)
    endpoints = {node.node_id: _node_payload(node) for node in nodes}
    relations = [_relation_payload(relation, endpoints=endpoints) for relation in graph.relations(scope=scope)]
    return canonical_digest(
        {
            "scope": scope,
            "nodes": _canonically_ordered(list(endpoints.values())),
            "relations": _canonically_ordered(relations),
        }
    )


DIGEST_FIELDS = "names, types, aliases, facts, relations"
"""What :func:`content_digest` covers, in words, for a proof to state about itself.

Public and rendered into :attr:`ReplayProof.rendered` because a digest a reader
cannot scope is a number they have to trust: "the hashes differ" means something
different if timestamps were in it. No vectors and no timestamps, so the digest
is over what the graph ASSERTS rather than over how it was stored -- which is
what makes two runs of one compression comparable.
"""


@dataclass(frozen=True)
class ReplayProof:
    """What a replay proves about one scope, with both digests so it can be checked.

    Both digests rather than only :attr:`equal`, because a proof a caller cannot
    verify is an assertion. The two hashes are what a reader compares, and they
    are what goes in an audit record.
    """

    scope: str
    live_digest: str
    """:func:`content_digest` over the graph as it stands."""

    replayed_digest: str
    """:func:`content_digest` over the graph rebuilt from the journal alone."""

    equal: bool
    """Whether the journal accounts for the live graph, belief for belief.

    ``False`` means something wrote to the graph without going through the
    journal -- which is exactly what it is for. It is not an error to compute:
    the caller asked whether the two agree, and "no" is an answer.
    """

    event_count: int
    """How many journalled mutations the replay applied. Zero is a real answer."""

    @property
    def rendered(self) -> str:
        """The proof as plain ASCII lines, for an agent and for a demo transcript.

        Here rather than in a caller because there are two callers -- the MCP
        ``memory_replay`` tool and the demo -- and a proof rendered two ways is a
        proof two readers would compare differently. Both digests, what they
        cover, the verdict, and how many events stood behind it: everything a
        reader needs to redo the comparison themselves.
        """
        return "\n".join(
            (
                f"scope: {self.scope}",
                f"live content digest: {self.live_digest}",
                f"replayed content digest: {self.replayed_digest}",
                f"digests equal: {str(self.equal).lower()}",
                f"digest covers: {DIGEST_FIELDS}",
                f"journalled events replayed: {self.event_count}",
            )
        )


def prove(*, scope: str, graph: GraphStore, ledger: LedgerStore) -> ReplayProof:
    """Replay *scope* from its journal and compare it to *graph* by content digest.

    The proof the amendment asks for: compression maintains a history that can be
    replayed and recalled, and this is the recall. **No model call and no
    embedding call**, so it is cheap enough to run at the end of every dream pass
    and in the demo.

    A mismatch is reported, never raised. A graph that was written behind the
    journal's back is a finding about the caller, and the two digests are how it
    is diagnosed; raising would leave the caller without them.

    Raises:
        ValueError: only from :func:`replay`, on a journalled split the events
            cannot reproduce or a history with a gap in it, and from
            :func:`content_digest` on a dangling edge.
    """
    replayed = replay(scope=scope, ledger=ledger)
    live_digest = content_digest(graph, scope)
    replayed_digest = content_digest(replayed, scope)
    return ReplayProof(
        scope=scope,
        live_digest=live_digest,
        replayed_digest=replayed_digest,
        equal=live_digest == replayed_digest,
        event_count=len(ledger.events(scope=scope)),
    )
