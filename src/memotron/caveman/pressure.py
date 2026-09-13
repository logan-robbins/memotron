"""Pure node value, the pressure signal, and the deterministic forced-merge slate.

Which nodes die when a scope is over ``N`` is the most consequential decision in
the whole subsystem, so it is made **here** -- by arithmetic over stored counts
and timestamps, with no LLM anywhere near it -- and merely *told* to the model.
The global dream pass decides what the surviving node's lines and type say; it
decides neither who goes nor what the result is called.

Everything in this module is a pure function of its arguments. There is no
store, no clock, no transport: ``now`` arrives as an argument, the degree
arrives as an argument, and both co-occurrence and similarity arrive as
*callables* -- so the pairing rule can be proved against a hand-written
co-occurrence table and similarity matrix, and the same rule then runs over the
real ledger and real embeddings unchanged.

.. rubric:: The pairing rule, and why the peer comes from the whole scope

``pressure = max(0, count - N)``. Each merge of a pair collapses two nodes into
one, so exactly ``pressure`` disjoint pairs bring a scope back to exactly ``N``
-- which is why no node may appear in two pairs, and why the slate is a set of
*pairs* rather than a ranked kill list.

A doomed node is the lowest-value node still unconsumed. Its peer is the node it
shares the most **evidence** with, over the **whole scope** -- any node this pass
has not already consumed, however valuable. Evidence is the ledger's own
co-occurrence: how many entries name both nodes, read straight from
``LedgerStore.cooccurrence``.

That pool was once the lowest-value ``2 * pressure`` nodes, on the reasoning
that a merge is a loss of identity and a high-value node should not be
conscripted into one it did not earn. The first live run showed what the bound
actually costs (#251 amendment A): pairing two doomed nodes with nothing in
common produced ``chart-models``, one node holding the chart, the JedAI models,
session affinity and #246 -- so a search for any of those four landed on a node
named for none of them. The bottom of the value order is not a set of related
concepts; it is a set of unrelated leftovers.

Linking to the whole scope makes the merge *absorptive* instead: the doomed node
is folded into the concept it already co-occurs with, which survives under its
own name and gains the doomed node's name as an alias. A high-value node in a
pair therefore loses nothing -- it keeps its name, its id, its ``created_at``
and its ledger key, and only gains lines and aliases. Which is why drawing from
the whole scope is now the safe choice and the bounded pool was the expensive
one.

.. rubric:: Why co-occurrence comes from the ledger and not from the graph

A pair's weight is the number of ledger entries that name both nodes. It is read
from ``LedgerStore.cooccurrence`` -- the append-only store -- and not from the
edges between them, and that is the amendment D adaptation this module needed
(#251 amendment D): the textless ``LINK`` whose weight was rebuilt from
``ledger.cooccurrence`` after every write is gone, so the ladder now asks the
ledger the question the link used to cache the answer to.

Asking the relations instead would narrow the rung without saying so. An edge
exists only where a *relational* claim was made, so two concepts named together
by a unary claim -- which is most of them -- would score zero, and the mandate a
reader is shown (``linked xN``, "N ledger claims name both") would stop meaning
what it says.

.. rubric:: Why the peer order is co-occurrence, then shared turns, then similarity

Sharing an entry is evidence that these two things were said about each other
rather than merely resembling one another. Embedding similarity is
evidence that they are *described* alike -- which is exactly how ``#245`` and
``#246`` come out near-identical while naming different defects. Co-occurrence
answers "does this belong there"; similarity only answers "does this read like
that", so it ranks last and decides only when nothing in the ledger separates
the candidates.

Between them sits the *shared turn* (#251 amendment B). An edge needs a claim
about both nodes, and the second live run showed what the graph therefore cannot
see: two facts stated in one breath are extracted as two claims, share no entry,
and score 0 -- so turn 1's "fix the platform, never downgrade" fell through to
similarity and was folded into ``#245``, and ``chart`` into ``gateway``. Sharing
an ``(episode_id, turn)`` is the weakest provenance the ledger holds, and it
still outranks similarity for the reason co-occurrence does: it is a record of
what was said together rather than a measurement of what reads alike.

.. rubric:: Why the reads and degree terms are logarithmic

``recency`` is a decay in ``[0, 1]``. A raw ``read_count`` is unbounded, so a
node read two hundred times would make every other term arithmetically
irrelevant -- the value function would degenerate into "most read wins" and the
motive's weights would stop meaning anything. ``log1p`` keeps the three signals
on comparable scales while staying strictly monotone, so "more reads is more
value" and "more edges is more value" both still hold exactly.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from memotron.caveman.models import Node
from memotron.caveman.motive import ValueWeights
from memotron.embedding import cosine_similarity

SECONDS_PER_DAY = 86_400.0
"""Days are the unit the motive's half-life is stated in."""

DEFAULT_TYPE_VALUE = 1.0
"""Value of a node whose type the motive expresses no preference about.

Neutral rather than zero: ``type_weight`` is a *preference* over an open-text
vocabulary, and a type nobody has ranked yet must not be scored as worthless --
that would make every newly coined type the first thing pressure destroys, which
is the opposite of what an open vocabulary is for.
"""


@dataclass(frozen=True)
class ValuedNode:
    """A node with its computed survival value.

    :func:`merge_slate` consumes these rather than recomputing values, for two
    reasons: the caller needs the same numbers anyway (the demo prints each
    slate node's value, which is the only way the forced merge is auditable),
    and recomputing inside the slate would need the degree lookup and the clock
    that this module deliberately does not have.
    """

    node: Node
    value: float

    @property
    def node_id(self) -> str:
        """The node's id, for ordering and for the pair records."""
        return self.node.node_id


@dataclass(frozen=True)
class MergePair:
    """One mandatory merge, with a direction: *doomed* is folded into *survivor*.

    The direction is decided **here** and not by the model. *survivor* is the
    higher-``node_value`` half of the pair, and it keeps its own name, id,
    ``created_at`` and ledger key; *doomed* loses its separate identity and its
    name becomes one of the survivor's aliases. ``dream`` still decides what the
    survivor's lines and type say -- it does not decide who goes or what the
    result is called.

    All three criteria are recorded, not just the deciding one: *link_weight* is
    the number of ledger entries naming both nodes, *turn_cooccurrence* the
    number of conversational turns they share, and *similarity* the cosine
    between their embeddings. A forced merge is only reviewable if the pair says
    on what evidence it was formed, and "weight 0, turns 0, similarity 0.91" and
    "weight 4, turns 3, similarity 0.12" are two very different mandates -- the
    first is a resemblance and the second is a record.
    """

    doomed_node_id: str
    survivor_node_id: str
    link_weight: int
    turn_cooccurrence: int
    similarity: float

    @property
    def node_ids(self) -> frozenset[str]:
        """Both ids, for the "every slate pair appears in some merge" check."""
        return frozenset({self.doomed_node_id, self.survivor_node_id})

    def render(self) -> str:
        """``n-014 INTO n-031`` -- the mandate as an error message names it.

        Ids rather than names, and directional: an op is identified by the ids it
        claims, and "which of the two survives" is the half of the mandate a
        response can get wrong. ``dream`` renders the same pair with both names
        for the model; this is the form a rejection quotes.
        """
        return f"{self.doomed_node_id} INTO {self.survivor_node_id}"


def pressure(count: int, max_nodes: int) -> int:
    """``max(0, count - max_nodes)`` -- how many nodes must be merged away.

    An invariant, not a soft cap: the number returned here is the number of
    merges the global pass is *required* to deliver, and an under-delivered
    forced merge is out of contract rather than a nudge to try again.
    """
    if count < 0:
        raise ValueError(f"node count cannot be negative, got {count}")
    if max_nodes < 1:
        raise ValueError(f"max_nodes must be at least 1, got {max_nodes}")
    return max(0, count - max_nodes)


def node_value(
    node: Node,
    *,
    degree: int,
    now: datetime,
    weights: ValueWeights,
    half_life_days: float,
    type_weight: Mapping[str, float],
) -> float:
    """A node's survival value under pressure. Higher survives.

    Four weighted terms, each a plain function of stored state:

    ``recency``
        ``0.5 ** (age / half_life)`` over ``last_touched_at``, so a node exactly
        one half-life old scores this term at **exactly 0.5**. ``last_touched_at``
        rather than ``created_at`` because the question pressure is asking is
        "is this concept still live", and a node re-lined by yesterday's dream is
        live however old it is.
    ``reads``
        ``log1p(read_count)`` -- see the module docstring for why logarithmic.
    ``degree``
        ``log1p(degree)``. A connected node is load-bearing for its neighbours'
        1-hop reads, so destroying it costs more than its own lines.
    ``type``
        the motive's preference for this node's type, or
        :data:`DEFAULT_TYPE_VALUE` when it has none.

    Args:
        degree: The node's edge count, from ``GraphStore.degree``. Passed in
            rather than looked up so this module needs no store.
        type_weight: The motive's ``type_weight`` mapping. Separate from
            *weights* because ``ValueWeights.type`` is the *coefficient* on this
            term while this mapping supplies the term's per-type value -- one is
            "how much does type matter", the other is "which types matter".

    Raises:
        ValueError: if *half_life_days* is not positive, or if the node was last
            touched after *now*. A node from the future is a clock defect, and
            clamping it would silently score the whole scope as brand new.
    """
    if half_life_days <= 0.0:
        raise ValueError(f"half_life_days must be positive, got {half_life_days}")
    age_days = (now - node.last_touched_at).total_seconds() / SECONDS_PER_DAY
    if age_days < 0.0:
        raise ValueError(
            f"node {node.node_id} was last touched at {node.last_touched_at.isoformat()}, "
            f"after now={now.isoformat()} -- a node cannot be newer than the clock"
        )
    # ``math.pow`` rather than ``0.5 ** x``: the operator is typed as returning
    # ``Any`` (a negative base with a fractional exponent is complex), and the two
    # agree to the bit for a positive base -- ``math.pow(0.5, 1.0)`` is exactly 0.5.
    recency = math.pow(0.5, age_days / half_life_days)
    return (
        weights.recency * recency
        + weights.reads * math.log1p(node.read_count)
        + weights.degree * math.log1p(degree)
        + weights.type * type_weight.get(node.type, DEFAULT_TYPE_VALUE)
    )


def embedding_similarity(first: Node, second: Node) -> float:
    """Cosine similarity between two nodes' stored embeddings.

    The canonical ``similarity`` argument for :func:`merge_slate`, kept here
    rather than in ``dream`` because it *is* part of the pairing rule: "its
    highest-similarity peer" is only a definition once similarity is one named
    thing. ``dream`` passes this in, and a test passes a matrix instead.

    Raises:
        ValueError: if either node has no embedding. That is only ever a freshly
            split part awaiting its first dream, and treating it as similarity
            0.0 would silently pair it with whichever node happens to sort
            first -- a forced merge decided by iteration order.
    """
    if not first.embedding or not second.embedding:
        missing = first.node_id if not first.embedding else second.node_id
        raise ValueError(
            f"cannot compare node {missing} -- it has no embedding, so it has not been dreamed yet "
            f"and its similarity to anything is undefined"
        )
    return cosine_similarity(list(first.embedding), list(second.embedding))


def _value_order(valued: ValuedNode) -> tuple[float, str]:
    """The one total order over nodes: value ascending, then node id.

    A single function because three things must agree on it -- which nodes are
    doomed, which node of a pair survives, and the order the slate is built in.
    Two of those reading "lowest value" differently is how a slate stops being
    reproducible.
    """
    return (valued.value, valued.node_id)


def pair_key(first: str, second: str) -> tuple[str, str]:
    """The canonical orientation of an unordered node pair: the lower id first.

    Public because it is the orientation ``LedgerStore.turn_cooccurrence`` keys
    its own result with, and a caller that keyed a pair the other way round would
    read every shared-turn count as zero. One function, so the two cannot
    disagree.
    """
    return (first, second) if first < second else (second, first)


def _weight_between(weights: Mapping[tuple[str, str], int], first: str, second: str) -> int:
    """A pair's count in a :func:`pair_key`-keyed table, or 0 when it is absent.

    Zero rather than an error: "these two were never mentioned together" is the
    normal case in a sparse scope, and it is a *ranking* input, so it has to be
    a number. What it must never be is a tie-break of its own -- every unshared
    candidate scores 0 and the next criterion separates them.
    """
    return weights.get(pair_key(first, second), 0)


def merge_slate(
    nodes: Sequence[ValuedNode],
    *,
    pressure: int,
    cooccurrence: Callable[[str], Mapping[str, int]],
    turn_links: Mapping[tuple[str, str], int],
    similarity: Callable[[Node, Node], float],
) -> tuple[MergePair, ...]:
    """Exactly *pressure* disjoint, directed pairs over the lowest-value nodes.

    Deterministic to the tie. Nodes are ordered by :func:`_value_order`, and then
    repeatedly:

    1. the **doomed** node is the lowest-value node not yet consumed;
    2. its **peer** is the unconsumed node sharing the most ledger entries with
       it, over the whole scope -- ties (and the all-zero case, where nothing the
       ledger holds names it beside anything) broken by the highest number of
       shared conversational turns, then by highest embedding similarity, then by
       lowest node id;
    3. the pair's **survivor** is its higher-value half, which the scan order
       makes the peer. It keeps its name; see :class:`MergePair`.

    Each criterion is consulted only for the candidates the one above it left
    tied, which is what makes the order true of the implementation and not just
    of the docstring: *turn_links* is read for the candidates at the top
    co-occurrence count, and *similarity* is called only for those still tied on
    turns. ``pressure == 0`` returns an empty slate and calls neither
    *cooccurrence* nor *similarity* -- a free pass must not pay for a scan it
    will not use.

    Args:
        cooccurrence: ``LedgerStore.cooccurrence`` -- given a node id, the other
            node ids sharing an entry with it, counted. Called once per doomed
            node and never for a candidate peer: the relation is symmetric, so
            one lookup weighs that node against the whole scope, which is why
            this is the callable and *turn_links* is a table. A node id the
            result does not mention scores 0.
        turn_links: Shared turn counts, from
            ``LedgerStore.turn_cooccurrence(scope=...)``, keyed by
            :func:`pair_key`. One whole-scope statement rather than a callable,
            because that is the shape the ledger computes it in. Required rather
            than defaulted to empty: an absent argument would silently restore
            the behaviour amendment B exists to remove, and a caller with no
            ledger to ask is a caller that cannot compute this slate.

    Raises:
        ValueError: if *pressure* is negative, if two entries name the same node,
            or if the scope holds fewer than ``2 * pressure`` nodes. The last one
            is the honest failure: a scope that cannot supply enough distinct
            nodes cannot be brought back under ``N`` by merging, and silently
            returning a shorter slate would leave the bound violated with nothing
            reporting it.
    """
    if pressure < 0:
        raise ValueError(f"pressure cannot be negative, got {pressure}")
    if pressure == 0:
        return ()

    identifiers = [valued.node_id for valued in nodes]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("merge_slate was given the same node twice")
    required = 2 * pressure
    if len(nodes) < required:
        raise ValueError(
            f"pressure {pressure} needs {required} distinct nodes to pair, but the scope holds "
            f"{len(nodes)} -- merging cannot bring this scope under its node budget"
        )

    remaining = sorted(nodes, key=_value_order)
    pairs: list[MergePair] = []
    while len(pairs) < pressure:
        doomed = remaining.pop(0)
        shared_entries = cooccurrence(doomed.node_id)
        weighed = [(candidate, shared_entries.get(candidate.node_id, 0)) for candidate in remaining]
        top = max(weight for _, weight in weighed)
        turned = [
            (candidate, _weight_between(turn_links, doomed.node_id, candidate.node_id))
            for candidate, weight in weighed
            if weight == top
        ]
        shared = max(turns for _, turns in turned)
        tied = [(candidate, similarity(doomed.node, candidate.node)) for candidate, turns in turned if turns == shared]
        peer, score = min(tied, key=lambda item: (-item[1], item[0].node_id))
        remaining.remove(peer)
        lower, higher = sorted((doomed, peer), key=_value_order)
        pairs.append(
            MergePair(
                doomed_node_id=lower.node_id,
                survivor_node_id=higher.node_id,
                link_weight=top,
                turn_cooccurrence=shared,
                similarity=score,
            )
        )
    return tuple(pairs)
