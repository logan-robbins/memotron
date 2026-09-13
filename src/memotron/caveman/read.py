"""The read pipeline: embed, kNN, 1-hop, rank, dedupe, cut, render, receipt. **No LLM.**

Composes :mod:`memotron.caveman.rank` with a ``GraphStore``, an
``EmbeddingTransport`` and a ``ReceiptSink``. The agency in this subsystem sits
*around* the model call, never inside the rerank: compression was paid once at
dream time, so a read is arithmetic over what the dreamer already wrote. That is
what makes a read cheap enough to run on every compaction, and it is why
``ScriptedChatTransport`` is not in this module's tests — a read that made a
model call would be a defect no assertion about output quality would catch.

A module-level function rather than a class: there is no state between reads, and
every store operation in this package takes its ``now`` as an argument (see
:mod:`memotron.caveman.seams`), so only the composition root needs a clock.

.. rubric:: Two entry points, one emitter

:func:`read` answers a query. :func:`brief` answers the question a session has
before it has a query — right after compaction, when the reader knows nothing
about the scope and cannot name what to ask for. It takes no query and no
embedder: every ``!`` line in the scope, then the highest-value nodes until the
budget, which is "what must never be violated here, and what is this place
mostly about".

They share :func:`_emit` — the dedupe, the cut, the render, the ``record_read``,
both receipts and the result — because everything after ranking is the same
statement, and two copies of it would be two ways for a read to be truncated.
What differs is only how candidates are scored: a measured similarity to a
query, or a node's standing value.

.. rubric:: Dedupe runs before the cut, and is not a truncation

:func:`~memotron.caveman.rank.dedupe_lines` folds away facts that state one
thing twice — the same superseded fact reached from two nodes, one measurement
restated on both. It runs on the ranked set BEFORE
:func:`~memotron.caveman.rank.cut_to_budget`, so the line a duplicate would
have spent goes to the next real fact rather than being lost with it.

A dropped duplicate is deliberately NOT ``saturated``. Saturation means the
budget refused a fact the reader has not been told; a duplicate means the reader
has already been told it, by the line above. So the two are reported separately:
:attr:`ReadResult.duplicates_dropped` and the ``READ_EMITTED`` detail carry the
fold, and ``READ_BUDGET_SATURATED`` stays the loss.

.. rubric:: Where a seed comes from: exact first, then kNN

Seeding is **hybrid**, and the exact half is not a nicety. ``#245`` and ``#246``
are one character apart in a 3072-dimensional space, so a query for one
retrieves the other; a name the dreamer did not keep (``C4``, ``LiteLLM
proxy``) is not in the node's ``name`` at all. Both are exactly the tokens a
searcher DOES know exactly, so both are looked up rather than measured:

* ``graph.node_by_alias`` for the whole query and for every
  :func:`~memotron.caveman.rank.query_tokens` key — a hit on ``name`` or on
  any recorded alias;
* ``ledger.identifiers(scope)`` for every key, case-insensitively over verbatim
  identifier spellings.

Every exact hit is a seed at :data:`EXACT_SIMILARITY`, and kNN fills whatever
``read_k`` slots are left. **Exact hits are never truncated to fit ``read_k``**:
an exact hit is the thing the searcher asked for by name, and dropping it to
make room for a measured neighbour is the failure amendment A exists to fix. So
``read_k`` bounds the kNN half, and a query naming twelve known things seeds
twelve nodes.

:attr:`ReadResult.seeds` reports each hit with the token and the mechanism that
found it, so "why did this node come back" is answerable from the result rather
than by re-running the read with a debugger.

.. rubric:: The kNN half has a floor as well as a cap

``read_k`` is a cap, and a cap alone fills every slot it has: on a nine-node
scope ``read('C4')`` seeded eight nodes, six of them at similarities between
0.13 and 0.17, and 1-hop expansion from those reached the ninth. A read that
returns the whole scope has answered nothing (#251 amendment C).

So ``motive.knn_min_similarity`` floors the MEASURED half: a kNN candidate below
it is dropped **before** 1-hop expansion, so it contributes neither its own
lines nor its neighbours'. Before expansion rather than after, because a seed's
neighbours are the larger cost — the defect was one weak seed pulling in a node
nobody asked about.

**An exact hit is never floored.** It was not measured: a search key the reader
typed is not a resemblance to be second-guessed, and ``EXACT_SIMILARITY`` is a
stand-in for "this is the thing" rather than a measurement that could fall below
a threshold.

A floored candidate still appears on :attr:`ReadResult.seeds`, with
:attr:`SeedHit.kept` ``False``. "kNN offered this and the floor refused it" is a
different answer from "nothing matched", and the first is the one that tells a
reader whether the floor is set right.

.. rubric:: Which nodes lead a query read

:func:`read` passes its exact-hit node ids to
:func:`~memotron.caveman.rank.rank_lines`, which lifts their every line by
:data:`~memotron.caveman.rank.EXACT_FLOOR`. Block order follows the rank of
each node's best kept line, so the nodes the query NAMED come first, then the
scope's other constraints, then the rest.

:func:`brief` passes none, and that asymmetry is the whole point: a brief's
question is "what must I never violate here", so its constraint-holding nodes
lead by :data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` exactly as before.

.. rubric:: What a read emits is facts AND relations

A seed offers every belief it holds: the facts a bounded read renders, and every
:class:`~memotron.caveman.models.Relation` that touches it, rendered from its
end (#251 amendment D). An edge is a belief between two concepts, so leaving it
out of the block would hide the half of the graph that is not a node -- and the
node ids in a rendered edge are what turn a read into somewhere to go next.

.. rubric:: What a 1-hop neighbour is allowed to contribute

**The relations that touch a seed, and its ``rule`` facts.** Nothing else.

A hard rule is the fact most expensive to rediscover, and one that binds a
seed's neighbour usually binds the work the query is about; rules are also the
one kind a motive cannot down-weight. A relation to a seed is a belief ABOUT the
query's neighbourhood, which is what the expansion was for.

A neighbour's ``is``, ``attribute`` and ``unsure`` facts are deliberately NOT
offered, and neither are its edges to nodes the read never seeded. They are
about the neighbour rather than about the query, and admitting them turns a
100-line budget into a breadth-first crawl of the scope.

.. rubric:: One edge, offered twice, folded once

A seed offers the edges that touch it and a neighbour offers the edges that
touch a seed, so the edge between them is a candidate twice -- rendered outgoing
from one end and ``from`` the other. That is not a defect to special-case:
``rank.dedupe_lines`` compares the CLAIM, so the two collapse to one and the
reader is told the belief once, in the block of whichever node ranked higher.
The fold reports it on :attr:`ReadResult.duplicates_dropped` like any other.

.. rubric:: Where a neighbour's similarity comes from

``knn`` measures the seeds. A neighbour was never scored against the query — it
was reached across an edge — so its similarity is derived: :data:`HOP_DISCOUNT`
times the similarity of the *most similar seed it is related to*, read from
``graph.relations``.

Deriving it rather than re-embedding is deliberate. Cosine-scoring the
neighbours caller-side is exactly the loop ``storage/base.py:329-355`` forbids,
and a second ``knn`` call over the whole scope would make every read a full scan
even on a store where kNN is an index. Scoring them all at 0.0 was the other
option and is worse: every non-constraint neighbour line would tie, so their
order would fall through to the alphabet and the motive's kind weights would
stop applying to a third of the emitted set.

The expansion reads ``graph.relations_of`` once per seed rather than
``graph.relations`` once per scope: the attachment similarities, the seeds' own
edges and the seed-touching edges of every neighbour all fall out of the same
walk, and a scope-wide scan to answer a question about eight nodes is the kind
of read the seam exists to avoid.

.. rubric:: A block is rendered by ``render.render_node`` and nowhere else

The cut decides WHICH beliefs survive; the renderer decides what they look like.
So ``_render`` hands ``render_node`` a copy of the node carrying only the facts
that survived, plus the relations that survived, and the fixed reading order --
rules, definition, attributes, relations, unsure -- comes out of the one module
that owns it. A second rendering here would be a second answer to "what does
memory look like when it arrives".
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from memotron.caveman.models import Fact, FactKind, Node, Receipt, ReceiptOp, Relation
from memotron.caveman.motive import CavemanMotive, motive_digest
from memotron.caveman.pressure import node_value
from memotron.caveman.rank import (
    RankCandidate,
    RankedLine,
    cut_to_budget,
    dedupe_lines,
    query_tokens,
    rank_lines,
)
from memotron.caveman.receipts import canonical_digest, new_receipt_id, text_digest
from memotron.caveman.render import READ_FACT_KINDS, footer, render_node
from memotron.caveman.seams import GraphStore, LedgerStore, ReceiptSink
from memotron.caveman.tokens import estimate_tokens
from memotron.embedding import EmbeddingTransport

HOP_DISCOUNT = 0.5
"""How much of a seed's similarity a 1-hop neighbour inherits.

At 0.5 a neighbour's fact of a given kind ranks below the same kind on the seed
it hangs off, which is the ordering a reader wants: the thing asked about first,
the things it touches after. Rules are unaffected — they carry
:data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` and float above the whole
set either way.
"""

BLOCK_SEPARATOR = "\n\n"
"""What separates one node's rendered block from the next.

A blank line, because a reader (human or model) has to see where one concept
ends. One character per node against a 1.3k-token budget.
"""


EXACT_SIMILARITY = 1.0
"""Similarity an exact hit is seeded at: the ceiling of the measured scale.

Cosine similarity cannot exceed 1.0, so an exact seed's lines are scored at
least as high as any kNN seed's of the same kind — "an exact hit outranks a
measured one" without a second sort key or a special case in
:mod:`memotron.caveman.rank`. A node whose text embeds identically to the
query reaches 1.0 too and ties, which the ranker's own
``(-score, node_name, line)`` tie-break resolves deterministically.
"""

SeedKind = Literal["alias", "identifier", "knn"]
"""How a seed was found. Three mechanisms, and a reader can tell them apart.

``alias`` is a hit on a node's ``name`` or one of its recorded aliases,
``identifier`` a hit in the ledger's identifier index, ``knn`` a measured
embedding neighbour. The first two are exact and carry the token that matched;
the third is not and does not.
"""


@dataclass(frozen=True)
class SeedHit:
    """One seed candidate, and why it is one — or why it was left out.

    Reported on :attr:`ReadResult.seeds` so the answer to "why did this node
    come back" is in the result. A ``knn`` hit carries ``token=None`` because no
    token matched it — it was measured against the whole query, which the result
    already holds.
    """

    node_id: str
    kind: SeedKind
    token: str | None
    similarity: float
    kept: bool = True
    """Whether this candidate actually seeded the read.

    ``False`` only for a ``knn`` candidate whose ``similarity`` fell below
    ``motive.knn_min_similarity``: it is reported so the reader can see what
    kNN offered and why it was refused, and it contributed no line and no
    1-hop neighbour.

    ``True`` for every exact hit — an exact hit is not measured and is never
    floored — and for every kNN candidate that cleared the floor. Defaulted to
    ``True`` so the common construction states only the exception.
    """


@dataclass(frozen=True)
class ReadResult:
    """One read, and everything a caller needs to decide whether to trust it.

    ``saturated`` is **not optional to check**. A caller that ignores it can
    silently miss a constraint, which is the whole reason
    ``READ_BUDGET_SATURATED`` is also a receipt: the truncation is in the audit
    stream rather than only in a return value somebody dropped.
    """

    query: str
    """What was asked. Empty exactly when this came from :func:`brief`.

    A blank query is refused by :func:`read`, so ``""`` is unambiguous: it means
    there was no query, not that an empty one was answered.
    """

    scope: str
    rendered: str
    """The emitted blocks, then one ``more:`` footer line.

    Per node: ``render.render_header`` — whose ``as of`` date, entry count and
    node id let a reader judge staleness and evidence and then traverse, without
    a second call — then that node's kept facts in
    :data:`~memotron.caveman.render.FACT_ORDER` order. The footer names every
    emitted node so the deep read and the neighbour walk are calls the reader can
    make rather than pointers they have to decode. Empty when nothing was
    emitted: an empty answer has no blocks and nothing to go deeper into.
    """

    nodes: tuple[Node, ...]
    """The nodes that contributed at least one kept belief, in render order.

    The records as the graph holds them -- every fact, not only the kept ones.
    What the read EMITTED is :attr:`rendered`; this is what the emitted blocks
    are about, so a caller can read a node's type, its aliases or its full fact
    set without a second store call.
    """

    node_ids: tuple[str, ...]
    """The emitted node ids, in block order. The same ids the footer names.

    Carried as its own field rather than left to be derived from :attr:`nodes`
    (#251 amendment D) because this is the field a caller acts on: it is what
    ``explain``, ``neighbors`` and ``node`` take, and an agent that wants to
    traverse should not have to know that a ``Node`` record is reachable in order
    to find the id printed in front of its name.
    """

    seeds: tuple[SeedHit, ...]
    """Every seed CANDIDATE, exact hits first, then kNN in similarity order.

    Candidates, not emitted nodes, and not only the ones that survived:

    * a seed whose every line lost the budget cut is still in here, because
      "the query matched this and the budget dropped it" is a different answer
      from "the query did not match it";
    * a kNN candidate the ``knn_min_similarity`` floor refused is in here with
      :attr:`SeedHit.kept` ``False``, so what the embedding space offered is
      visible even when the read declined it.

    1-hop neighbours are never seeds and never appear. Empty for a
    :func:`brief`, which had no query to seed from.
    """

    line_count: int
    """Kept facts, one rendered line each. Bounded by ``motive.read_line_budget``."""

    duplicates_dropped: int
    """Facts folded away by :func:`~memotron.caveman.rank.dedupe_lines`.

    Counted BEFORE the budget cut, so this is every duplicate the read found
    rather than only the ones that would have fitted. ``0`` on a read where
    nothing said anything twice, which is the common case and the one where the
    ``READ_EMITTED`` detail is unchanged.

    Reported separately from :attr:`saturated` because they are different
    events: a duplicate is a fact the reader was already told, a truncation is a
    fact the reader was not told at all.
    """

    token_count: int
    """``estimate_tokens(rendered)`` — headers, separators and the footer included.

    Deliberately a different measurement from the budget
    :func:`~memotron.caveman.rank.cut_to_budget` enforced, which is over the
    FACTS. The header is a fixed per-node cost the reader has to pay to know
    which concept a fact belongs to and how to reach the rest of it, so it is
    reported rather than budgeted, and this number is the one a caller compares
    against its context window.
    """

    contract_digest: str
    """sha256 over the query, the motive digest, the budgets and the vector space.

    Two reads that agree on all four produce the same digest, so a caller can
    prove two contexts were assembled under one policy. It deliberately does NOT
    cover the graph's contents: it identifies the CONTRACT, not the answer —
    ``outputs_digest`` on the receipt identifies the answer.
    """

    saturated: bool
    """``True`` when the budget dropped at least one ranked fact."""


def read_contract_digest(*, query: str, motive: CavemanMotive, embedding_identifier: str) -> str:
    """The digest :attr:`ReadResult.contract_digest` carries.

    Public because a caller that wants to know whether two reads ran under one
    contract should not have to run the read to find out.

    ``embedding_identifier`` is in it because two vector spaces are two different
    kNN results from identical inputs — ``embedding.py:86-92`` states that rule
    for stored vectors, and it is the same rule here.

    ``knn_min_similarity`` is in it for the same reason ``read_k`` is: both
    decide what the read RETRIEVES, so two reads that disagree about the floor
    were assembled under two different policies however alike their answers
    look. Named explicitly rather than left to ``motive_digest``, which covers
    it too — this list is the read's own contract, spelled out.
    """
    return canonical_digest(
        {
            "query": query,
            "motive_digest": motive_digest(motive),
            "read_k": motive.read_k,
            "knn_min_similarity": motive.knn_min_similarity,
            "line_budget": motive.read_line_budget,
            "token_budget": motive.read_token_budget,
            "embedding_identifier": embedding_identifier,
        }
    )


def brief_contract_digest(*, motive: CavemanMotive) -> str:
    """The digest a :func:`brief` carries and receipts.

    ``("brief", motive_digest)`` and nothing else, because nothing else is in
    the contract: there is no query, no embedding space and no ``read_k`` — a
    brief reads the whole scope. The literal ``"brief"`` is what stops it from
    colliding with a :func:`read_contract_digest` over an empty query.

    The budgets are deliberately absent, which is the one asymmetry with
    :func:`read_contract_digest`: they are in ``motive_digest`` already, and a
    brief has no argument that is not the motive.
    """
    return canonical_digest(("brief", motive_digest(motive)))


def _folded_identifiers(index: Mapping[str, Sequence[str]]) -> dict[str, tuple[str, ...]]:
    """The ledger's identifier index, keyed case-insensitively.

    ``LedgerStore.identifiers`` keys verbatim on purpose — the matching policy
    belongs to the reader — and this is that policy: a searcher types ``c4``,
    the ledger recorded ``C4``, and they are one key. Two spellings folding
    together contribute the union of their node ids, sorted, so a hit is
    deterministic whichever spelling the query used.
    """
    folded: dict[str, set[str]] = {}
    for identifier, node_ids in index.items():
        folded.setdefault(identifier.casefold(), set()).update(node_ids)
    return {key: tuple(sorted(node_ids)) for key, node_ids in folded.items()}


def _exact_seeds(*, query: str, scope: str, graph: GraphStore, ledger: LedgerStore) -> list[tuple[Node, SeedHit]]:
    """Every node the query names exactly: alias hits, then identifier hits.

    Alias lookups run over the whole query first and then over each token, so
    ``"LiteLLM proxy"`` matches a two-word alias before its two words are tried
    separately. Identifier lookups are per token only: the ledger keys single
    identifiers, and offering it a sentence asks it a question it cannot answer.

    Deduped by node id, first hit winning, because a node found twice is one
    seed — and the mechanism that found it first is the more specific one.
    """
    tokens = query_tokens(query)
    identifiers = _folded_identifiers(ledger.identifiers(scope=scope))
    hits: list[tuple[Node, SeedHit]] = []
    seen: set[str] = set()

    def take(node: Node, *, kind: SeedKind, token: str) -> None:
        if node.node_id in seen:
            return
        seen.add(node.node_id)
        hits.append((node, SeedHit(node_id=node.node_id, kind=kind, token=token, similarity=EXACT_SIMILARITY)))

    for token in (query.strip(), *tokens):
        named = graph.node_by_alias(scope=scope, name=token)
        if named is not None:
            take(named, kind="alias", token=token)
    for token in tokens:
        for node in graph.get_nodes(list(identifiers.get(token.casefold(), ()))):
            take(node, kind="identifier", token=token)
    return hits


def _seed_the_read(
    *,
    query: str,
    scope: str,
    motive: CavemanMotive,
    graph: GraphStore,
    ledger: LedgerStore,
    embedder: EmbeddingTransport,
) -> tuple[list[tuple[Node, float]], tuple[SeedHit, ...]]:
    """Exact hits, then kNN above the floor for whatever ``read_k`` slots are left.

    Returns the seeded ``(node, similarity)`` pairs the candidate builder wants
    and the :class:`SeedHit` report the result carries, in one pass so the two
    cannot disagree about what was seeded.

    kNN is asked for the full ``read_k`` rather than for the remaining slots,
    because a neighbour the exact half already found does not use a slot: asking
    for exactly the shortfall would come back one short every time an exact hit
    is also a near neighbour, which it usually is.

    A candidate below ``motive.knn_min_similarity`` is reported with
    ``kept=False`` and seeds nothing. It does **not** free its slot for a
    further candidate: the floor drops what the embedding space offered rather
    than reaching deeper into a ranking it has already judged too weak, and
    ``knn`` was asked for ``read_k`` results, so there is nothing deeper to
    reach. A floored read is meant to be smaller.

    **No embedding is computed when the exact half already filled ``read_k``.**
    A read is otherwise the one operation here that makes a network call, and a
    query answered entirely out of two exact indexes should not pay for one.
    """
    exact = _exact_seeds(query=query, scope=scope, graph=graph, ledger=ledger)
    seeded = [(node, hit.similarity) for node, hit in exact]
    reported = [hit for _, hit in exact]

    slots = motive.read_k - len(exact)
    if slots > 0:
        taken = {hit.node_id for hit in reported}
        vector = embedder.embed(query)
        for node, similarity in graph.knn(scope=scope, vector=vector, k=motive.read_k):
            if len(seeded) - len(exact) >= slots:
                break
            if node.node_id in taken:
                continue
            kept = similarity >= motive.knn_min_similarity
            reported.append(SeedHit(node_id=node.node_id, kind="knn", token=None, similarity=similarity, kept=kept))
            if kept:
                seeded.append((node, similarity))
    return seeded, tuple(reported)


@dataclass(frozen=True)
class _Expansion:
    """Everything one walk of the seeds' edges produces. Built by :func:`_expand`.

    Three answers from one pass over ``graph.relations_of``, because they are
    three views of the same edges and computing them separately is three chances
    to disagree about which edge touched which seed.
    """

    seed_relations: dict[str, tuple[Relation, ...]]
    """Per seed, every edge that touches it. What the seed's block renders."""

    neighbour_relations: dict[str, tuple[Relation, ...]]
    """Per non-seed node, the edges that touch a seed. What a neighbour may contribute."""

    attachments: dict[str, float]
    """Per non-seed node, the best discounted similarity of a seed it relates to."""


def _expand(graph: GraphStore, *, seeds: Mapping[str, float]) -> _Expansion:
    """Walk each seed's edges once, and read the 1-hop neighbourhood off them.

    ``relations_of`` is both directions, so one call per seed reaches every edge
    that touches it whichever way it was written. A seed's edge to another SEED
    is kept on both seeds' lists and contributes no attachment: a node the query
    already reached is not a neighbour of itself.
    """
    seed_relations: dict[str, list[Relation]] = {seed_id: [] for seed_id in seeds}
    neighbour_relations: dict[str, list[Relation]] = {}
    attachments: dict[str, float] = {}
    for seed_id, similarity in seeds.items():
        for relation in graph.relations_of(seed_id):
            seed_relations[seed_id].append(relation)
            other = relation.target_id if relation.source_id == seed_id else relation.source_id
            if other in seeds:
                continue
            neighbour_relations.setdefault(other, []).append(relation)
            inherited = HOP_DISCOUNT * similarity
            attachments[other] = max(inherited, attachments.get(other, inherited))
    return _Expansion(
        seed_relations={seed_id: tuple(found) for seed_id, found in seed_relations.items()},
        neighbour_relations={node_id: tuple(found) for node_id, found in neighbour_relations.items()},
        attachments=attachments,
    )


def _candidates_of(
    node: Node,
    *,
    similarity: float,
    kinds: frozenset[FactKind],
    relations: Sequence[Relation] = (),
    names: Mapping[str, str] | None = None,
) -> list[RankCandidate]:
    """This node's facts of the given kinds and the given edges, as ranker candidates.

    One builder for every caller, because what differs between a seed, a
    neighbour and a brief is only WHICH beliefs are offered: a seed offers
    everything a read renders and all of its edges, a neighbour offers its rules
    and the edges that reach a seed. Two builders would be two places the
    superseded-fact exclusion had to be repeated.

    ``superseded`` facts are excluded by the caller's *kinds*
    (:data:`~memotron.caveman.render.READ_FACT_KINDS`) rather than at render
    time: a candidate the renderer would drop would still have spent a budget
    line, so the read would report a count no reader received.

    *names* is the id-to-name map the relation rendering needs, and must be the
    same map :func:`_render` is given -- otherwise the line the budget was
    measured over is not the line the reader gets.
    """
    candidates = [RankCandidate.of_fact(node, fact, similarity=similarity) for fact in node.facts if fact.kind in kinds]
    candidates.extend(
        RankCandidate.of_relation(node, relation, similarity=similarity, names=names) for relation in relations
    )
    return candidates


def _seed_candidates(
    seeded: Sequence[tuple[Node, float]],
    *,
    expansion: _Expansion,
    names: Mapping[str, str],
) -> list[RankCandidate]:
    """Every readable fact and every edge of every seed. A seed is what the query matched."""
    return [
        candidate
        for node, similarity in seeded
        for candidate in _candidates_of(
            node,
            similarity=similarity,
            kinds=READ_FACT_KINDS,
            relations=expansion.seed_relations[node.node_id],
            names=names,
        )
    ]


def _neighbour_candidates(
    neighbours: Sequence[Node],
    *,
    expansion: _Expansion,
    names: Mapping[str, str],
    scope: str,
) -> list[RankCandidate]:
    """A neighbour's ``rule`` facts and the edges that reach a seed. Nothing else.

    Raises:
        ValueError: if the store returned a neighbour no edge connects to a seed.
            Two seam methods disagreeing is a store defect, not a case to default
            around.
    """
    candidates: list[RankCandidate] = []
    for node in neighbours:
        similarity = expansion.attachments.get(node.node_id)
        if similarity is None:
            raise ValueError(
                f"{node.node_id} came back as a 1-hop neighbour of the seeds but no relation in "
                f"{scope!r} connects it to one -- this graph store's neighbours() and relations() disagree"
            )
        candidates.extend(
            _candidates_of(
                node,
                similarity=similarity,
                kinds=frozenset({FactKind.RULE}),
                relations=expansion.neighbour_relations[node.node_id],
                names=names,
            )
        )
    return candidates


def _kept_facts(lines: Sequence[RankedLine]) -> tuple[Fact, ...]:
    """The facts among these kept lines, in the order they were ranked.

    ``render_node`` re-orders them by kind, so the order here only has to be
    deterministic; rank order is the one order that already is.
    """
    return tuple(line.candidate.belief for line in lines if isinstance(line.candidate.belief, Fact))


def _kept_relations(lines: Sequence[RankedLine]) -> tuple[Relation, ...]:
    """The relations among these kept lines, in the order they were ranked."""
    return tuple(line.candidate.belief for line in lines if isinstance(line.candidate.belief, Relation))


def _render(
    kept: tuple[RankedLine, ...],
    nodes: Mapping[str, Node],
    *,
    entry_counts: Mapping[str, int],
    names: Mapping[str, str],
) -> tuple[str, tuple[Node, ...]]:
    """Group the kept beliefs into per-node blocks, then the one ``more:`` footer.

    Block order follows the rank of each node's best kept belief, which falls out
    of first appearance: the node holding the top-ranked line reads first. No
    second sort, so nothing can disagree with :func:`rank_lines`.

    **Each block is rendered by** :func:`~memotron.caveman.render.render_node`,
    over a copy of the node carrying ONLY the facts that survived the cut plus
    the relations that survived it. So the order inside a block is the
    renderer's fixed reading order -- rules, definition, attributes, relations,
    unsure -- rather than rank order. The two are different jobs: rank decides
    which beliefs are worth the budget and which block comes first, and a block a
    reader skims is a block whose kinds are always in the same place.

    The projection is what keeps the budget honest. ``render_node`` renders the
    facts the node it is given holds, so handing it the stored node would emit
    every fact the cut dropped, and the read would report a line count no reader
    received.

    A node missing from *entry_counts* has zero entries, which is a real state
    rather than a lookup failure: erasure deletes an episode's entries and
    leaves the nodes for the next dream.
    """
    order: list[str] = []
    grouped: dict[str, list[RankedLine]] = {}
    for line in kept:
        if line.node_id not in grouped:
            order.append(line.node_id)
            grouped[line.node_id] = []
        grouped[line.node_id].append(line)

    emitted = tuple(nodes[node_id] for node_id in order)
    if not emitted:
        return "", ()
    blocks = [
        render_node(
            node.model_copy(update={"facts": _kept_facts(grouped[node.node_id])}),
            _kept_relations(grouped[node.node_id]),
            entries=entry_counts.get(node.node_id, 0),
            names=names,
        )
        for node in emitted
    ]
    return BLOCK_SEPARATOR.join((*blocks, footer([node.node_id for node in emitted]))), emitted


def _emit(
    *,
    query: str,
    scope: str,
    motive: CavemanMotive,
    ranked: tuple[RankedLine, ...],
    known: Mapping[str, Node],
    names: Mapping[str, str],
    seeds: tuple[SeedHit, ...],
    digest: str,
    source: str,
    graph: GraphStore,
    ledger: LedgerStore,
    receipts: ReceiptSink,
    now: datetime,
) -> ReadResult:
    """Dedupe, cut, render, record and receipt one ranked candidate set. The shared tail.

    Everything after ranking, in one place, so :func:`read` and :func:`brief`
    cannot differ in how a truncation is reported or in whether an emitted node
    counts as read. *source* is the one clause of the receipt detail that is
    the caller's — how the candidates were found — appended to the counts.

    The duplicate fold happens FIRST, so both receipts count the beliefs that
    actually competed for the budget: "3 of 7 ranked facts" over a set whose
    other three were the same belief restated would price the cut against a
    number the reader never had.

    ``record_read`` is called once per EMITTED node, never for a candidate whose
    every belief lost the cut: ``read_count`` feeds
    :mod:`memotron.caveman.pressure`'s value function, and a node nobody read
    must not earn survival value.
    """
    unique, duplicates = dedupe_lines(ranked)
    kept, saturated = cut_to_budget(
        unique,
        line_budget=motive.read_line_budget,
        token_budget=motive.read_token_budget,
    )
    rendered, emitted = _render(kept, known, entry_counts=ledger.entry_counts(scope=scope), names=names)
    graph.record_read([node.node_id for node in emitted])
    folded = f"dropped {len(duplicates)} duplicate fact(s); " if duplicates else ""

    receipts.emit(
        Receipt(
            receipt_id=new_receipt_id(),
            op=ReceiptOp.READ_EMITTED,
            ts=now,
            scope=scope,
            subject=digest,
            inputs_digest=digest,
            outputs_digest=text_digest(rendered),
            detail=(
                f"{len(kept)} of {len(unique)} ranked facts from {len(emitted)} node(s); "
                f"{folded}"
                f"budget {motive.read_line_budget} lines / {motive.read_token_budget} tokens; "
                f"{source}"
            ),
        )
    )
    if saturated:
        receipts.emit(
            Receipt(
                receipt_id=new_receipt_id(),
                op=ReceiptOp.READ_BUDGET_SATURATED,
                ts=now,
                scope=scope,
                subject=digest,
                inputs_digest=digest,
                outputs_digest=text_digest(rendered),
                detail=(
                    f"dropped {len(unique) - len(kept)} of {len(unique)} ranked facts at "
                    f"{motive.read_line_budget} lines / {motive.read_token_budget} tokens"
                ),
            )
        )

    return ReadResult(
        query=query,
        scope=scope,
        rendered=rendered,
        nodes=emitted,
        node_ids=tuple(node.node_id for node in emitted),
        seeds=seeds,
        line_count=len(kept),
        duplicates_dropped=len(duplicates),
        token_count=estimate_tokens(rendered),
        contract_digest=digest,
        saturated=saturated,
    )


def read(
    *,
    query: str,
    scope: str,
    motive: CavemanMotive,
    graph: GraphStore,
    ledger: LedgerStore,
    embedder: EmbeddingTransport,
    receipts: ReceiptSink,
    now: datetime,
) -> ReadResult:
    """Answer *query* from *scope* under *motive*, deterministically.

    Seed exactly (alias and identifier hits), fill the remaining
    ``motive.read_k`` slots by ``knn`` above ``motive.knn_min_similarity``,
    expand 1-hop over ``relations_of``, rank the seeds' facts and edges and the
    neighbours' rules and seed-touching edges with the exact hits floored to the
    front, fold away beliefs that state one thing twice, cut to
    ``motive.read_line_budget`` lines and ``motive.read_token_budget`` tokens,
    render through :mod:`memotron.caveman.render`, receipt.

    The nodes the query named exactly lead the answer: their ids go to
    :func:`~memotron.caveman.rank.rank_lines` as ``exact_node_ids``, which
    lifts their beliefs by
    :data:`~memotron.caveman.rank.EXACT_FLOOR`, above the scope's other
    rules. Rules still come first WITHIN each block.

    Every emitted block carries its node id, and
    :attr:`ReadResult.node_ids` repeats them in block order, so ``explain``,
    ``node`` and ``neighbors`` are calls the reader can make from the answer.

    The *ledger* is read twice and only twice: once for ``identifiers``, which
    is the exact half of the seeding, and once for ``entry_counts``, which is a
    rendered header's ``N entries``. Both are one statement for the whole read
    rather than one per node, and neither is an LLM call — a read still makes
    **none**.

    A ``READ_EMITTED`` receipt is always emitted, carrying the contract digest
    as its ``inputs_digest`` and ``sha256(rendered)`` as its ``outputs_digest``.
    A ``READ_BUDGET_SATURATED`` receipt follows it whenever the budget dropped a
    line, so a truncation is a recorded decision rather than a quiet one.

    An empty scope returns an empty result and still receipts. That is a real
    answer to "what do I know about X" and not an error.

    Raises:
        ValueError: if *query* is blank, or if the store's ``neighbours`` returns
            a node no relation connects to a seed. The second is fail-fast on a
            store defect: two seam methods disagreeing is not a case to default
            around.
    """
    if not query.strip():
        raise ValueError("read query cannot be blank")

    seeded, seed_hits = _seed_the_read(
        query=query,
        scope=scope,
        motive=motive,
        graph=graph,
        ledger=ledger,
        embedder=embedder,
    )
    seeds = {node.node_id: similarity for node, similarity in seeded}

    neighbours = graph.neighbours(list(seeds))
    expansion = _expand(graph, seeds=seeds)

    known = {node.node_id: node for node, _ in seeded}
    known.update({node.node_id: node for node in neighbours})
    names = {node_id: node.name for node_id, node in known.items()}

    candidates = _seed_candidates(seeded, expansion=expansion, names=names)
    candidates.extend(_neighbour_candidates(neighbours, expansion=expansion, names=names, scope=scope))

    exact_ids = frozenset(hit.node_id for hit in seed_hits if hit.kind != "knn")
    floored = sum(1 for hit in seed_hits if not hit.kept)

    return _emit(
        query=query,
        scope=scope,
        motive=motive,
        ranked=rank_lines(candidates, motive, exact_node_ids=exact_ids),
        known=known,
        names=names,
        seeds=seed_hits,
        digest=read_contract_digest(
            query=query,
            motive=motive,
            embedding_identifier=embedder.identifier,
        ),
        source=(
            f"seeds {len(seeded)} of k={motive.read_k} ({len(exact_ids)} exact, "
            f"{floored} below the kNN floor {motive.knn_min_similarity}), neighbours {len(neighbours)}"
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        now=now,
    )


def _value_share(value: float, highest: float) -> float:
    """A node's value as a fraction of the scope's best, in ``(0, 1]``.

    :func:`brief` has no query, so a candidate's ``similarity`` — the thing
    :mod:`memotron.caveman.rank` scales a line by — is the node's standing
    value instead. It is a SHARE rather than the raw value on purpose: the
    ranker documents similarity as living in ``[-1, 1]`` and
    :data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` is sized against that, so
    handing it an unbounded ``node_value`` would eventually let an
    often-read node's attribute outrank somebody's hard constraint.

    ``highest <= 0.0`` means every node in the scope scored zero — a motive that
    weights only reads, in a scope nothing has been read in yet — so no node
    outvalues another and every share is 1.0. Not a fallback: it is what an
    equal share is.
    """
    if highest <= 0.0:
        return 1.0
    return value / highest


def brief(
    *,
    scope: str,
    motive: CavemanMotive,
    graph: GraphStore,
    ledger: LedgerStore,
    receipts: ReceiptSink,
    now: datetime,
) -> ReadResult:
    """The session-start read: every rule, then the scope's best nodes. **No LLM, no query.**

    What a reader needs when they have no query yet -- the first read after a
    compaction, when the context that knew what to ask for is gone. Every hard
    rule in the scope comes first (the ranker's
    :data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` puts it there), and the
    rest of the budget goes to the highest-value nodes, ordered by
    :func:`~memotron.caveman.pressure.node_value` -- the same value function
    that decides which nodes survive pressure, so a brief shows what the scope
    is actually about rather than what happens to sort first.

    Relations are candidates here exactly as they are in :func:`read`, each
    offered by both of its endpoints and folded to one by
    :func:`~memotron.caveman.rank.dedupe_lines` (#251 amendment D). A brief
    reads the whole scope, and a session-start read that showed the nodes but
    none of the edges between them would be the one read that hides half of what
    the scope asserts.

    No embedder argument, because there is nothing to embed: this reads the whole
    scope rather than a neighbourhood of it. That also means no ``read_k`` and no
    1-hop expansion — every node is already in the candidate set, so the budget,
    not the hop rule, is what bounds the answer.

    :attr:`ReadResult.seeds` is empty: a brief had no query, so nothing was
    seeded by one. ``READ_EMITTED`` carries
    :func:`brief_contract_digest` as its ``inputs_digest``.

    The duplicate fold applies here too, and matters more: a brief ranks the
    WHOLE scope flat, so every restatement of one fact anywhere in it competes
    for the same budget, and the first read after a compaction is the one that
    can least afford to spend a line twice.

    Raises:
        ValueError: if a node was last touched after *now* -- a clock defect
            :func:`~memotron.caveman.pressure.node_value` refuses to score.
    """
    nodes = graph.list_nodes(scope=scope)
    names = {node.node_id: node.name for node in nodes}
    values = {
        node.node_id: node_value(
            node,
            degree=len(graph.relations_of(node.node_id)),
            now=now,
            weights=motive.value_weights,
            half_life_days=motive.recency_half_life_days,
            type_weight=motive.type_weight,
        )
        for node in nodes
    }
    highest = max(values.values(), default=0.0)
    candidates = [
        candidate
        for node in nodes
        for candidate in _candidates_of(
            node,
            similarity=_value_share(values[node.node_id], highest),
            kinds=READ_FACT_KINDS,
            relations=graph.relations_of(node.node_id),
            names=names,
        )
    ]

    return _emit(
        query="",
        scope=scope,
        motive=motive,
        ranked=rank_lines(candidates, motive),
        known={node.node_id: node for node in nodes},
        names=names,
        seeds=(),
        digest=brief_contract_digest(motive=motive),
        source=f"brief over {len(nodes)} node(s) by value, no query",
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        now=now,
    )
