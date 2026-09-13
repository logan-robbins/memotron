"""The pure half of the read path: query tokens, fact ranking, the budget cut.

No I/O, no LLM, no clock, no store. This is the half that must be provable to
the value, so it is separated from :mod:`memotron.caveman.read` exactly the
way ``retrieval.py`` separates its pure scoring from its pipeline. Everything
here is a function of its arguments: the same query, the same candidates and the
same motive always produce the same tokens, the same order and the same cut, on
a laptop and in the container.

:func:`query_tokens` is here rather than in ``read`` for that reason and no
other. Turning a query into exact-match search keys is arithmetic over a string;
looking those keys up needs a graph and a ledger, so ``read`` owns the lookup
and this owns the split.

.. rubric:: Why a constant floor rather than infinity

A ``rule`` fact outranks every other kind by construction — that is what
:data:`~memotron.caveman.motive.WEIGHTED_FACT_KINDS` excluding ``RULE``
means, and it is why a motive cannot express "rank a rule below an attribute".
The mechanism is an **additive** :data:`CONSTRAINT_FLOOR`, not ``math.inf`` and
not a separate sorted bucket:

* ``inf`` collapses every rule to one score, so the rule set would order itself
  alphabetically rather than by similarity — and ``inf - inf`` is a ``nan``
  waiting for whoever later subtracts two scores.
* A separate bucket makes "the highest-ranked rules only" a second sort with its
  own tie-break to keep in step with this one.

With a finite additive floor there is ONE sort key, rules still order among
themselves by measured similarity, and scores stay comparable and reproducible —
which is what makes :func:`cut_to_budget` truncating "by rank" a statement
anyone can check.

.. rubric:: The tie-break is part of the contract

``(-score, node_name, text)``. Two facts with identical scores happen
constantly — a neighbour's rules all inherit one attachment similarity —
and Python's sort is stable, so without an explicit tie-break the order would
follow whatever order the read path happened to assemble candidates in. That
would make ``ReadResult.contract_digest`` a digest of a stable contract over an
unstable rendering.

.. rubric:: A candidate is a fact OR a relation

Both are beliefs a read emits, so both are ranked by one function against one
budget (#251 amendment D). A :class:`RankCandidate` carries the belief RECORD --
:class:`~memotron.caveman.models.Fact` or
:class:`~memotron.caveman.models.Relation` -- and the line
:mod:`memotron.caveman.render` already made of it. The record is what scores
and dedupes; the rendered line is what is emitted, what the budget is measured
over and what the tie-break reads.

Both, rather than one of them, because neither alone is enough. A relation's
rendered line needs the OTHER endpoint's name, which is a map only
:mod:`memotron.caveman.read` holds, so this module cannot render one; and a
rendered line cannot be scored or deduped, because ``(x3)`` is an evidence
marker that a token comparison would read as content and a label is not part of
the claim. So the caller renders, through :meth:`RankCandidate.of_fact` and
:meth:`RankCandidate.of_relation`, and those two constructors are the only way a
candidate is built -- which is what stops a caller's rendering from disagreeing
with the renderer's.

.. rubric:: Reinforcement is a multiplier, not a floor

``score *= 1 + log1p(evidence)``. Restating a belief makes its record stronger
rather than making a second record (``GraphStore.upsert_relation``,
``Fact.entry_ids``), and the read side is where that strength has to show:
evidence 3 outranks evidence 1 of the same kind at the same similarity.

Logarithmic and multiplicative, deliberately. Additive would let a much-repeated
attribute pass a rule, which is the one ordering
:data:`CONSTRAINT_FLOOR` exists to make impossible; linear would let a fact
somebody restated ten times outrank a far more relevant one; and the log keeps
the whole multiplicative term bounded well below the floors, which is what makes
their arithmetic still hold (see :data:`CONSTRAINT_FLOOR`).

.. rubric:: Why dedupe is here and not at dream time

:func:`dedupe_lines` removes facts that say one thing twice. It lives on the
READ side because the duplication is a property of the *emitted set*, not of any
one node: the dreamer writes one node's facts in one call and cannot see that
the superseded fact it just wrote for ``gateway`` is already on ``agent-memory``
(#251 amendment B, measured on the second live run). Two nodes each holding a
true, in-budget fact is not a defect in either node — it is a defect in reading
them together, so it is fixed where they are read together.

It is pure, and it runs on the ranked set BEFORE the cut, so the budget a
duplicate would have spent goes to the next real fact instead.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from memotron.caveman.models import Fact, FactKind, Node, Relation
from memotron.caveman.motive import CavemanMotive
from memotron.caveman.render import render_fact, render_relation
from memotron.caveman.tokens import estimate_tokens

QUERY_TOKEN_PATTERN = re.compile(r"#?[A-Za-z0-9]+(?:[-._/][A-Za-z0-9]+)*")
"""One exact-match search key. Whitespace and punctuation separate them.

The shape is chosen by what a searcher actually types, which is why it is not
``\\w+``:

* ``#245`` keeps its ``#``, but only leading. The hash is part of the identifier
  as the ledger recorded it, and ``identifiers`` keys are verbatim, so a token
  that dropped it would miss the very index this exists to hit.
* ``claude-haiku-4-5``, ``text-embedding-3``, ``agent-memory``, ``0.4.1`` and
  ``jedai/memotron`` stay whole. A hyphen, a dot and a slash join identifier
  parts; splitting on them turns one high-value key into three worthless ones
  (``claude``, ``haiku``, ``4``) and is exactly how ``#245``-style precision is
  lost.
* Everything else separates, so ``"the gateway refuses my Host header?"`` yields
  six keys and not one, and a possessive yields ``gateway`` rather than
  ``gateway's`` — which is the spelling a node is actually stored under.

Case is preserved: both lookups the keys feed are case-insensitive, and the
spelling the searcher used is the one worth reporting back on a
``SeedHit``.
"""


def query_tokens(query: str) -> tuple[str, ...]:
    """The exact-match search keys in *query*, in order, deduped case-insensitively.

    First spelling wins on a repeat, because the only thing a second spelling of
    one key could change is the rendering of the hit that reports it.

    A blank query yields no tokens rather than raising: ``read`` refuses a blank
    query on its own behalf with a message about the query, and a tokeniser that
    also refused it would put that rule in two places.
    """
    seen: set[str] = set()
    tokens: list[str] = []
    for match in QUERY_TOKEN_PATTERN.finditer(query):
        token = match.group(0)
        folded = token.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        tokens.append(token)
    return tuple(tokens)


CONSTRAINT_FLOOR = 1e6
"""Additive term that lifts a ``rule`` fact above every other kind.

Finite on purpose -- see the module docstring. Large enough that the whole
multiplicative term cannot reach it: a similarity in ``[-1, 1]``, a motive's
kind and type weights, and :func:`reinforcement`, which is ``1 + log1p(n)`` and
so reaches 15 only at about three million supporting entries. That product is
bounded by a handful of units in every configuration this package ships, which
is the property that makes "constraints first" true rather than usually true.
"""

EXACT_FLOOR = 2e6
"""Additive term that lifts every fact of an EXACTLY-NAMED node above the rest.

Twice :data:`CONSTRAINT_FLOOR`, which is the whole arithmetic: a fact on a node
the query named exactly scores at least ``EXACT_FLOOR``, and the best a fact on
any other node can reach is ``CONSTRAINT_FLOOR`` plus a product of terms bounded
well below ``1e6``. So every exact-hit fact precedes every other fact, and
inside the exact set a ``rule`` still precedes an ``attribute`` because it
carries ``EXACT_FLOOR + CONSTRAINT_FLOOR``. Two floors, one sort key, no
buckets.

.. rubric:: Why a query read needs this and a brief does not

Block order follows the rank of each node's best kept fact
(``read._render``), so before this floor existed an exact hit for ``C4``
came back THIRD: two other nodes in the scope held rules, and
``CONSTRAINT_FLOOR`` put their blocks first (#251 amendment C, measured on the
third live run). That is the right answer to "what must I never violate here",
which is :func:`~memotron.caveman.read.brief`'s question, and the wrong
answer to "tell me about C4". A searcher who names a thing is telling you what
the read is about; the scope's other constraints are context, and context reads
second.

So the floor is applied by the CALLER, per read, rather than by the motive:
:func:`~memotron.caveman.read.read` passes its exact-hit node ids and
``brief`` passes none. A motive cannot switch it on, because it is not a
preference — it is which question is being asked.
"""

RELATION_WEIGHT = 1.0
"""Kind weight for a relation candidate. Neutral, and not a motive field.

A motive's ``fact_kind_weight`` is a preference over
:class:`~memotron.caveman.models.FactKind`, and a relation has no fact kind
-- it has an edge TYPE, drawn from a per-scope vocabulary the motive has never
seen. So there is nothing for a motive to express a preference about, and
inventing a per-type weight map would ask a persona to rank labels the dreamer
coins at run time.

Neutral rather than boosted or discounted, for the same reason
:data:`DEFAULT_TYPE_WEIGHT` is: no opinion. What differentiates one relation
from another is :func:`reinforcement` over its own evidence and the similarity
of the node it is read from, both of which are measured.
"""

DEFAULT_TYPE_WEIGHT = 1.0
"""Weight for a node type the motive does not mention.

Node types are open text, so a motive's ``type_weight`` is a preference over the
types it knows and is silent about the rest. Neutral rather than zero: an
unmentioned type means "no opinion", and scoring it zero would delete a whole
node from every read because nobody had named its type in a policy.
"""


@dataclass(frozen=True)
class RankCandidate:
    """One belief offered to the ranker, with the node context that scores it.

    The belief is a :class:`~memotron.caveman.models.Fact` the node holds or a
    :class:`~memotron.caveman.models.Relation` that touches it, and *text* is
    the line :mod:`memotron.caveman.render` already made of it. See the module
    docstring for why both are carried.

    Build one with :meth:`of_fact` or :meth:`of_relation`. They are the only two
    constructions this module offers, so *text* is always the renderer's own
    output rather than a caller's paraphrase of it.
    """

    node_id: str
    node_name: str
    node_type: str
    belief: Fact | Relation
    text: str
    """The belief as :mod:`memotron.caveman.render` emits it.

    What the budget is measured over, what a block renders, and what the
    tie-break reads. Never what :func:`content_tokens` compares: a rendered line
    carries a label the claim does not and an ``(x3)`` marker whose digit a
    token comparison would read as an identifier.
    """

    similarity: float
    """How similar the belief's node is to the query. Measured, never assumed.

    A seed's own kNN similarity, or a 1-hop neighbour's discounted attachment
    similarity -- :mod:`memotron.caveman.read` decides which, because deciding
    it needs the graph and this module has none.
    """

    @classmethod
    def of_fact(cls, node: Node, fact: Fact, *, similarity: float) -> RankCandidate:
        """One of *node*'s facts, rendered by :func:`~memotron.caveman.render.render_fact`."""
        return cls(
            node_id=node.node_id,
            node_name=node.name,
            node_type=node.type,
            belief=fact,
            text=render_fact(fact),
            similarity=similarity,
        )

    @classmethod
    def of_relation(
        cls,
        node: Node,
        relation: Relation,
        *,
        similarity: float,
        names: Mapping[str, str] | None = None,
    ) -> RankCandidate:
        """One edge touching *node*, rendered from *node*'s end.

        *names* maps node ids to names for the OTHER endpoint, exactly as
        :func:`~memotron.caveman.render.render_relation` takes it. The same map
        must be used when the block is rendered, or the line the budget was
        measured over is not the line the reader gets.

        Raises:
            ValueError: via the renderer, if the relation does not touch *node*.
        """
        return cls(
            node_id=node.node_id,
            node_name=node.name,
            node_type=node.type,
            belief=relation,
            text=render_relation(relation, node_id=node.node_id, names=names),
            similarity=similarity,
        )

    @property
    def kind(self) -> str:
        """What kind of belief this is: a fact's kind word, or a relation's edge type.

        One field for two vocabularies, and they cannot collide:
        :class:`~memotron.caveman.models.FactKind`'s values are lowercase
        words and an edge type matches
        :data:`~memotron.caveman.models.EDGE_TYPE_PATTERN`, which requires a
        leading capital. So ``candidate.kind == FactKind.RULE`` is a safe test on
        any candidate -- ``FactKind`` is a ``StrEnum`` and compares equal to its
        value -- and a fact can never dedupe against a relation.
        """
        belief = self.belief
        return belief.kind.value if isinstance(belief, Fact) else belief.type

    @property
    def evidence(self) -> int:
        """How many ledger entries support this belief. What :func:`reinforcement` scales."""
        return self.belief.evidence

    @property
    def content(self) -> str:
        """The belief's own sentence: a fact's ``text`` or a relation's ``claim``.

        What :func:`content_tokens` compares, and deliberately not :attr:`text`.
        """
        belief = self.belief
        return belief.text if isinstance(belief, Fact) else belief.claim


@dataclass(frozen=True)
class RankedLine:
    """A candidate and its score, in rank order once it comes out of :func:`rank_lines`."""

    candidate: RankCandidate
    score: float

    @property
    def text(self) -> str:
        """The rendered belief."""
        return self.candidate.text

    @property
    def node_id(self) -> str:
        """The node this belief was read from. What ``record_read`` is called with.

        For a relation that is the endpoint whose block it renders in, not both
        ends: an edge is read from one side, and the other side is named in the
        line.
        """
        return self.candidate.node_id

    @property
    def tokens(self) -> int:
        """The RENDERED line's token cost under the repo's estimator.

        The whole rendered line, label, edge type, target id and evidence marker
        included: a read emits ``"rule: never hermetic"``, so budgeting the bare
        text would under-count every fact by its label and every relation by its
        head. ``render.validate_fact_text``'s ``T`` guard is over a fact's text
        alone, and that is a different measurement of a different thing.
        """
        return estimate_tokens(self.text)


def reinforcement(evidence: int) -> float:
    """``1 + log1p(evidence)`` -- how much a belief's own evidence scales its score.

    The read side of reinforcement (#251 amendment D). A fact or relation three
    separate ledger entries support is worth more of a budget than one nobody has
    repeated, and because restating a belief unions an entry into the record it
    already has rather than writing a second record, the count IS the repetition.

    ``1 +`` so the term is never below 1 and a single-entry belief is scaled up
    rather than down: evidence is a reason to rank something higher, never a
    reason to rank it lower than a belief with no evidence at all -- which is a
    state no record can be in, since ``entry_ids`` has a minimum length of one.

    Public because it is the whole of the reinforcement policy and a caller
    comparing two scores should be able to compute the factor rather than infer
    it. Monotonic and bounded: 1.69 at one entry, 2.10 at two, 3.40 at ten.

    Raises:
        ValueError: on a negative count. Zero is permitted and returns 1.0 --
            it is what "no evidence" would scale by -- but a negative evidence
            count is a caller arithmetic defect.
    """
    if evidence < 0:
        raise ValueError(f"evidence cannot be negative, got {evidence}")
    return 1.0 + math.log1p(evidence)


def _type_weight(motive: CavemanMotive, node_type: str) -> float:
    """The motive's preference for this node type, or :data:`DEFAULT_TYPE_WEIGHT`."""
    return motive.type_weight.get(node_type, DEFAULT_TYPE_WEIGHT)


def _kind_weight(candidate: RankCandidate, motive: CavemanMotive) -> float:
    """The motive's preference for this belief's kind.

    A rule takes ``1.0`` and no kind weight at all -- ``fact_kind_weight`` is
    validated to EXCLUDE ``rule`` precisely so a persona cannot down-weight a
    hard rule. A relation takes :data:`RELATION_WEIGHT`, because a motive weights
    fact kinds and a relation has none. Everything else takes the motive's
    weight for its kind.
    """
    belief = candidate.belief
    if isinstance(belief, Relation):
        return RELATION_WEIGHT
    if belief.kind is FactKind.RULE:
        return 1.0
    return motive.fact_kind_weight[belief.kind]


def line_score(candidate: RankCandidate, motive: CavemanMotive, *, exact: bool = False) -> float:
    """Score one candidate under one motive.

    ``similarity * kind_weight * type_weight * reinforcement(evidence)``, plus
    :data:`CONSTRAINT_FLOOR` when the belief is a ``rule`` fact, plus
    :data:`EXACT_FLOOR` when *exact* -- the candidate's node is one the query
    named exactly.

    The measured part still orders the rule set by how relevant its nodes are,
    which is what a budget smaller than the rule set needs in order to keep the
    *right* rules. :func:`reinforcement` scales every candidate, a rule
    included: a rule two people stated is the rule to keep when only one fits.

    The floors are added AFTER the multiplication, so no amount of evidence can
    lift an attribute over a rule or a non-exact hit over an exact one. That
    ordering is the whole reason reinforcement is a multiplier on the measured
    term rather than a term of its own.

    *exact* is a keyword on THIS function rather than a second scoring function
    beside it, so the score a read sorts by is computed in exactly one place. It
    defaults to ``False``, which is every caller that has no query to name a node
    with.
    """
    score = (
        candidate.similarity
        * _kind_weight(candidate, motive)
        * _type_weight(motive, candidate.node_type)
        * reinforcement(candidate.evidence)
    )
    if candidate.kind == FactKind.RULE:
        score += CONSTRAINT_FLOOR
    if exact:
        score += EXACT_FLOOR
    return score


def rank_lines(
    candidates: Sequence[RankCandidate],
    motive: CavemanMotive,
    *,
    exact_node_ids: frozenset[str] = frozenset(),
) -> tuple[RankedLine, ...]:
    """Score every candidate and return them in rank order, best first.

    Sorted on ``(-score, node_name, line)`` — the full key, so the same input
    always yields byte-identical output regardless of the order the caller
    assembled the candidates in.

    Every fact whose node is in *exact_node_ids* carries :data:`EXACT_FLOOR`, so
    the nodes the query named come first and the scope's other rules come after
    them. The default is empty: a caller that passes nothing gets
    byte-identical output to a caller that could not pass it, which is what
    keeps :func:`~memotron.caveman.read.brief` unchanged.

    *exact_node_ids* is a set of NODE ids rather than a flag on
    :class:`RankCandidate`, because being an exact hit is a property of the
    query-to-node match and the same node's facts must not be able to disagree
    about it.
    """
    scored = [
        RankedLine(
            candidate=candidate,
            score=line_score(candidate, motive, exact=candidate.node_id in exact_node_ids),
        )
        for candidate in candidates
    ]
    scored.sort(key=lambda ranked: (-ranked.score, ranked.candidate.node_name, ranked.text))
    return tuple(scored)


def cut_to_budget(
    ranked: Sequence[RankedLine],
    *,
    line_budget: int,
    token_budget: int,
) -> tuple[tuple[RankedLine, ...], bool]:
    """Take facts in rank order until a budget is reached. Returns ``(kept, saturated)``.

    Two ceilings, both hard: at most *line_budget* rendered lines, and at most
    *token_budget* tokens summed over their rendered text.

    **The token cut stops at the first fact that does not fit; it does not skip
    ahead to a smaller one.** Rank order is a priority order, so passing over a
    fact to fit a lower-ranked one is a knapsack heuristic that would make the
    emitted set depend on text lengths rather than on relevance — and it would
    break the one statement a caller can rely on, which is that the kept set is
    a *prefix* of the ranked set.

    ``saturated`` is ``True`` whenever anything was dropped. It is not optional
    to check: a caller that ignores it can silently miss a rule, which is
    exactly the fact most expensive to rediscover. :mod:`memotron.caveman.read`
    turns it into a ``READ_BUDGET_SATURATED`` receipt so the truncation is in the
    audit stream rather than only in a return value.

    Raises:
        ValueError: if either budget is below 1. A zero budget is a
            misconfiguration, and answering it with "nothing, saturated" would
            look like an empty scope.
    """
    if line_budget < 1:
        raise ValueError(f"line_budget must be at least 1, got {line_budget}")
    if token_budget < 1:
        raise ValueError(f"token_budget must be at least 1, got {token_budget}")

    kept: list[RankedLine] = []
    spent = 0
    for line in ranked:
        if len(kept) >= line_budget:
            break
        if spent + line.tokens > token_budget:
            break
        kept.append(line)
        spent += line.tokens
    return tuple(kept), len(kept) < len(ranked)


DUPLICATE_JACCARD = 0.6
"""How much of two same-kind facts' content must overlap to be one fact.

Jaccard over content-token SETS, so it is symmetric and length-independent: a
six-word fact and a nine-word fact stating the same thing measure the same
whichever is asked about first, which a containment test would not give.

0.6 is where the two failures separate on the measured corpus. ``pre-#245 the
gateway refused the Host header outright`` against itself on a second node is
1.0; the same claim reworded scores in the 0.6-0.9 band; and two genuinely
different attributes of one node sit well below it, because a caveman fact is
almost all content words -- there is no filler for a similarity to accumulate
in.
"""

DEDUPE_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    }
)
"""Words that carry no fact, removed before two facts are compared.

Deliberately tiny, and deliberately NOT a blacklist
:mod:`memotron.caveman.render` refuses to have: this set never rejects a fact
and never changes one. It exists only so that ``the`` and ``is`` cannot push two
different facts over :data:`DUPLICATE_JACCARD` on the strength of their grammar.

**No polarity or modality word is in here.** ``not``, ``never``, ``no``,
``only``, ``must`` and ``always`` are the words that make a line mean the
opposite of a line, so removing them would let ``never hermetic`` dedupe against
``hermetic`` -- the one mistake a fact-level dedupe must not make. An article
cannot invert a claim; a negation IS the claim.
"""

IDENTIFIER_JOINERS = "-._/"
"""Characters that join the parts of one coded identifier.

The same set :data:`QUERY_TOKEN_PATTERN` keeps INSIDE a token, named here
because :func:`is_identifier_token` reads it as a signal rather than as a
separator rule.
"""


def is_identifier_token(token: str) -> bool:
    """Whether *token* is a coded identifier rather than an ordinary word.

    Three signals, any one of which is enough: a leading ``#`` (``#245``), an
    embedded digit (``0.4.1``, ``3072d``, ``31``), or a joiner from
    :data:`IDENTIFIER_JOINERS` (``claude-haiku-4-5``, ``agent-memory``,
    ``jedai/memotron``).

    This is the subsystem's own vocabulary rather than a heuristic invented
    here: ``extract.py``'s contract calls "issue numbers, model aliases, counts,
    dimensions, versions" identifiers and makes them the one thing a claim must
    quote verbatim. A bare count IS an identifier under that rule, which is why
    ``31 tools`` and ``24 tools`` are not two phrasings of one fact.

    Deliberately inclusive at the edges -- a hyphenated ordinary word reads as
    an identifier here. The cost of that error is a dedupe refused, which leaves
    both lines emitted exactly as before; the cost of the opposite error is a
    fact deleted from a read.
    """
    return (
        token.startswith("#")
        or any(character.isdigit() for character in token)
        or any(joiner in token for joiner in IDENTIFIER_JOINERS)
    )


def content_tokens(belief: Fact | Relation) -> frozenset[str]:
    """The casefolded content tokens of one belief, as :func:`dedupe_lines` compares them.

    Tokenised by :data:`QUERY_TOKEN_PATTERN` -- the same rule a query is split
    by, so punctuation separates but an identifier stays whole and ``#245`` and
    ``claude-haiku-4-5`` are one token each rather than five. Then
    :data:`DEDUPE_STOPWORDS` are removed, except that a token
    :func:`is_identifier_token` accepts is ALWAYS kept: an identifier whose
    spelling collides with a stopword is still the highest-value token on the
    line.

    Over the belief's own sentence -- a fact's ``text``, a relation's ``claim``
    -- and over nothing else. Never a fact's ``key``: a label is how one node
    chose to file an attribute, and two nodes filing one measurement under ``chat
    default`` and ``default model`` are still restating it once too often. Never
    a relation's ``type`` or endpoints either: those are the identity
    :func:`dedupe_lines` already compares separately, and never the RENDERED
    line, whose ``(x3)`` marker :func:`is_identifier_token` would read as an
    identifier and whose presence would then stop a reinforced belief from
    folding against its own restatement.

    A set, not a sequence: word order is not part of what makes two beliefs one
    belief, and two orderings of one claim are the commonest paraphrase there is.
    """
    sentence = belief.text if isinstance(belief, Fact) else belief.claim
    return frozenset(
        folded
        for folded in (token.casefold() for token in QUERY_TOKEN_PATTERN.findall(sentence))
        if is_identifier_token(folded) or folded not in DEDUPE_STOPWORDS
    )


def _jaccard(left: frozenset[str], right: frozenset[str]) -> float:
    """The intersection size over the union size, and 0.0 when either side is empty.

    An empty side is a fact whose every token was a stopword. ``0/0`` is not
    1.0 here on purpose: two facts with no content tokens between them have not
    been shown to say the same thing, and the safe answer to "is this a
    duplicate" is no.
    """
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def dedupe_lines(ranked: Sequence[RankedLine]) -> tuple[tuple[RankedLine, ...], tuple[RankedLine, ...]]:
    """Collapse facts that state one thing twice. Returns ``(kept, dropped)``.

    Two beliefs are ONE belief when all three hold:

    * **the same kind** -- :attr:`RankCandidate.kind`, so a fact kind or an edge
      type. A superseded fact and a rule are different claims about the world
      however alike their words -- "the gateway refused the Host header" as
      history and as a standing rule are two different things, and a reader needs
      both. A fact and a relation can never match here at all, by the
      construction :attr:`RankCandidate.kind` documents;
    * **the same identifier set** (:func:`is_identifier_token` over
      :func:`content_tokens`). ``#245`` and ``#246`` differ by one character and
      score 0.75 against identical wording, so measurement alone would merge two
      issues into one. Equality rather than "both sides carry some": a fact that
      drops an identifier its twin carries is the vaguer fact, and keeping the
      vaguer one is how a read loses the number a searcher came for;
    * **Jaccard of content tokens at or above** :data:`DUPLICATE_JACCARD`.

    *ranked* is consumed in order and the FIRST of a duplicate group is kept, so
    "keep the higher-ranked line" needs no comparison here:
    :func:`rank_lines` has already put them in a total order, so dropping the
    later one drops the lower-ranked one by construction. A caller handing this
    an unranked sequence gets a well-defined answer in ITS order, which is why
    this takes a ``Sequence`` and not a promise.

    The dropped facts are RETURNED rather than counted, so
    :mod:`memotron.caveman.read` can put the count in a receipt and a caller
    that wants to know which fact was folded away can see it. Nothing is
    deleted: a dropped fact is still on its node and still in the ledger.

    The commonest fold amendment D produces is a relation reached from both ends:
    a seed offers every edge that touches it and a 1-hop neighbour offers the
    edges that touch a seed, so one edge is a candidate twice -- once rendered
    outgoing and once rendered ``from`` the other end. Same type, same claim,
    Jaccard 1.0, so the lower-ranked copy folds and the reader is told the belief
    once, in the block of whichever node ranked higher.

    Pure, and quadratic in the ranked set on purpose -- it compares against the
    beliefs already KEPT, which is what makes three phrasings of one fact
    collapse to one rather than to two pairs, and a ranked set is bounded by a
    read's candidate count rather than by the scope.
    """
    kept: list[RankedLine] = []
    dropped: list[RankedLine] = []
    signatures: list[tuple[str, frozenset[str], frozenset[str]]] = []
    for line in ranked:
        kind = line.candidate.kind
        tokens = content_tokens(line.candidate.belief)
        identifiers = frozenset(token for token in tokens if is_identifier_token(token))
        if any(
            kind == seen_kind and identifiers == seen_identifiers and _jaccard(tokens, seen_tokens) >= DUPLICATE_JACCARD
            for seen_kind, seen_tokens, seen_identifiers in signatures
        ):
            dropped.append(line)
            continue
        kept.append(line)
        signatures.append((kind, tokens, identifiers))
    return tuple(kept), tuple(dropped)
