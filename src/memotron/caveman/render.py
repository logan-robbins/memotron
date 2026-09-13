"""The ONE place LLM-facing text is shaped. Plain ASCII words and structure.

Everything a model or an agent reads out of this subsystem is rendered here: a
node's header, its facts, its relations, the footer that names what to call
next, and the format block every prompt embeds. One module, so there is exactly
one answer to "what does memory look like when it arrives", and a change to that
answer is a change to one file.

This replaces ``lines.py`` and the sigil grammar it owned (#251 amendment D).
The sigils were six invented characters -- a legend in every prompt, a parser on
every read, and the cause of most of the live rejections the branch's history
records. What replaced them is words:

* a fact renders as ``<label>: <text>`` where the label is the fact's kind, or
  its ``key`` when it has one;
* a relation renders as ``<TYPE> <target name> [<target id>]: <claim>``;
* a header renders as ``name (type) [id] as of DATE, N entries, aka ...``.

.. rubric:: Printable ASCII, and why that is a rule rather than a preference

Nothing this module adds to content is outside printable ASCII -- no middle dot,
no arrow, no multiplication sign. Two reasons, and the second is the one that
matters:

1. every one of those characters is a token a tokeniser spends and a convention
   a model has to be taught;
2. a symbol in a rendering is a symbol in a prompt, and a prompt that teaches an
   alphabet is a prompt that can be misread. The branch lost live runs to
   exactly that.

``test_caveman_render.py`` renders a node built from ASCII content and asserts
``rendered.isascii()``, so the rule is checked rather than stated. Model-authored
text is passed through verbatim -- if a claim contains an em dash, that is the
claim, and rewriting a record's content to satisfy a rendering rule would be
falsifying it.

.. rubric:: Node ids are rendered on purpose

``[n-001]`` next to every name, and again in the footer. An agent that can see a
node id can traverse from it -- ``explain(n-001)`` for the whole record,
``neighbors(n-001)`` for the edges -- so the id is what turns a block of text
into somewhere to go next. A header carrying only a name is a dead end.

.. rubric:: Order inside a block is fixed, not ranked

Rules, then what the thing IS, then its attributes, then its relations, then what
is unsure. A reader skims, and a block whose kinds are always in the same place
can be skimmed without being read. Rank decides which facts are worth a budget
and which block comes first; it does not decide where a rule sits inside a
block.

``SUPERSEDED`` facts are not rendered here at all. They are what the deep read
is for: "this used to be true" stops a reader re-deriving a wrong answer, and it
is worth nothing in a bounded block that could hold a live fact instead.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

from memotron.caveman.errors import FactGrammarError
from memotron.caveman.models import Fact, FactKind, Node, Relation
from memotron.caveman.tokens import estimate_tokens

BREVITY_RULE = "Write each fact as compact, information-dense text: drop articles and filler, fragments are fine, grammar is optional, identifiers and numbers verbatim, one fact per record."
"""The length instruction every prompt states, in one wording, with no number.

Public because ``motive.render_motive_block`` states the same sentence: one
string rather than two paraphrases, so the format block and the motive block
cannot ask for different things.

**The prompt asks the model to count nothing.** Brevity is the requirement;
character and word ceilings were tried, and a model that cannot count characters
overruns them by about the width of a clause however forcefully they are stated
(#251 amendment A). :func:`validate_fact_text`'s ``max_fact_tokens`` is what
remains, as a paragraph guard.
"""

RELATION_TARGET_RULE = "A relation names its target by NODE ID, never by name."
"""Stated by the prompts where it is TRUE -- the two dream prompts -- and nowhere else.

It is not in :func:`format_prompt_block`, and that is deliberate. Reconcile
embeds the same block and is the one stage that names an edge's ends by SURFACE
NAME, because a name it answers ``new`` has no node id yet. A shared block
carrying this sentence made the reconcile prompt state a rule and then override
it three lines later, which is the defect class this branch has lost live runs
to: a prompt that contradicts itself leaves the model to pick.

So the rule lives here, next to the renderer that obeys it, and each prompt
states the rule that holds for its own answer at the point of use.
"""

LABEL_SEPARATOR = ": "
"""What separates a fact's label from its text, and a relation's head from its claim."""

HEDGE_WORDS: frozenset[str] = frozenset(
    {
        "maybe",
        "probably",
        "seems",
        "appears",
        "might",
        "possibly",
        "i think",
    }
)
"""Words that express uncertainty the ``unsure`` kind already carries.

Deliberately short and literal. This is not a hedging *detector* -- it is the
handful of terms that mean "I am marking this uncertain in prose instead of
marking it uncertain in the record", which is the one thing a named kind exists
to stop. The dreamer must either commit the fact or set ``kind`` to ``unsure``.
"""

_HEDGE_PATTERN = re.compile(
    r"\b(?:" + "|".join(re.escape(word) for word in sorted(HEDGE_WORDS)) + r")\b",
    re.IGNORECASE,
)

FACT_LABEL: Mapping[FactKind, str] = {
    FactKind.RULE: "rule",
    FactKind.IS: "is",
    FactKind.ATTRIBUTE: "attribute",
    FactKind.UNSURE: "unsure",
    FactKind.SUPERSEDED: "superseded",
}
"""The word a fact of each kind renders under when it carries no ``key``.

Equal to each member's value today, and written out rather than derived from
``kind.value`` because the two are different contracts: the enum value is what
crosses a wire and a database column, and this is what a reader sees. A rename of
one must not silently rename the other.
"""

FACT_GLOSS: Mapping[FactKind, str] = {
    FactKind.RULE: "a hard rule that must never be violated",
    FactKind.IS: "what this thing IS, its definition",
    FactKind.ATTRIBUTE: "a property or measured fact about it",
    FactKind.UNSURE: "believed but not established; intermittent or unreproduced",
    FactKind.SUPERSEDED: "stated and then replaced; kept as history, never shown in a read",
}
"""One line per kind, for a model rather than for a maintainer.

Public because :func:`~memotron.caveman.motive.render_motive_block` renders
the kinds in the motive's own priority order and needs each kind's gloss, so the
renderer owns the words and both prompts read them from here. One wording, two
prompts.
"""

FACT_ORDER: Mapping[FactKind, int] = {
    FactKind.RULE: 0,
    FactKind.IS: 1,
    FactKind.ATTRIBUTE: 2,
    FactKind.UNSURE: 4,
    FactKind.SUPERSEDED: 5,
}
"""Render rank per kind. Rules, definition, attributes, [relations], unsure, history.

Written out rather than generated from the enum's declaration order because the
sequence has a hole in it: :data:`RELATION_ORDER` sits at 3, between a node's own
attributes and what it is unsure about. An edge is a fact about two things, so it
reads after everything the node asserts on its own and before what it only
suspects.
"""

RELATION_ORDER = 3
"""Where relations sit in a rendered block. See :data:`FACT_ORDER`."""

READ_FACT_KINDS: frozenset[FactKind] = frozenset(FactKind) - {FactKind.SUPERSEDED}
"""The kinds a bounded read renders. Everything but history.

Derived by subtraction so a sixth kind is included the day it is declared, and
excluding history is stated once -- here -- rather than in every caller that
builds candidates.
"""

EVIDENCE_TEMPLATE = " (x{count})"
"""How a reinforced fact says so: ``(x3)`` after the text.

Rendered only when evidence exceeds one. A bare fact is a fact one claim
asserted, which is the common case, and ``(x1)`` on every line would spend a
token per fact to say nothing.
"""

ALIAS_PREFIX = ", aka "
"""How a header introduces the other names this node answers to."""

FOOTER_PREFIX = "more: "
"""How the one footer line a rendered read ends with begins.

``more: explain(n-041), neighbors(n-041)`` -- the calls, with the ids, for the
blocks the read emitted. A header's id is a pointer; the footer is the verb
(#251 amendment A named the symptom, amendment D named the second call).
"""

EXPLAIN_CALL = "explain"
NEIGHBORS_CALL = "neighbors"

EXAMPLE_FACT_TEXTS: tuple[str, ...] = (
    "real runs always go through the gateway, never hermetic and never a local stub",
    "LiteLLM proxy fronting the JedAI models, not a model itself",
    "chat default is claude-haiku-4-5",
    "embedding is text-embedding-3 at 3072 dims and must be selected explicitly",
    "session affinity is sometimes lost and the failure has never been reproduced",
)
"""Worked example fact TEXTS, shown to the model in every prompt.

A named constant rather than inline prose so a test can assert the thing that
actually matters: **every example passes the validation the same prompt states**,
at the default paragraph guard. A prompt that demonstrates a violation of its own
contract is how a model gets blamed for a defect in the prompt.

.. rubric:: Why the last example carries no number

It used to read "lost about 1 call in 20", and the first live run under this
format block lost EXTRACT to exactly that: the episode says "one call in
twenty", the model wrote "1 call in 20" and then listed ``20`` as an identifier
-- which the extract contract rejects, because an identifier the model did not
read is a hallucination. The example was the only place in the prompt where a
spelled-out count appeared as a digit, so it was the example teaching the
normalisation. An example is a demonstration of the FORM; it should not also
demonstrate rewriting the episode's own numbers.
"""


def validate_fact_text(text: str, *, max_fact_tokens: int) -> None:
    """Raise :class:`FactGrammarError` unless *text* is usable as a fact's text.

    Four rules, and the first three are shape:

    * non-blank;
    * one line, with no leading or trailing space;
    * ``estimate_tokens(text) <= max_fact_tokens`` -- a **paragraph guard**, not a
      target. It catches a paragraph arriving where a fact belongs, and it is
      deliberately loose (the presets sit at 30-40 tokens, 120-160 characters).
      Brevity is asked for by :data:`BREVITY_RULE` and never measured: a model
      cannot count characters, and three calibrations of a tight ceiling each
      moved the overrun by a couple of characters and never removed it;
    * no :data:`HEDGE_WORDS` member -- a hedge is what the ``unsure`` kind is for.

    A separate function from :class:`~memotron.caveman.models.Fact`'s own
    validators because these two need the motive: ``max_fact_tokens`` is a per-
    persona budget and the record cannot know it. The record enforces what is
    true of every fact; this enforces what is true of a fact under one policy.
    """
    if "\n" in text or "\r" in text:
        raise FactGrammarError(f"a fact is one line and cannot contain a newline: {text!r}")
    if not text.strip():
        raise FactGrammarError("a fact cannot be blank")
    if text != text.strip():
        raise FactGrammarError(f"fact text has leading or trailing whitespace: {text!r}")
    tokens = estimate_tokens(text)
    if tokens > max_fact_tokens:
        raise FactGrammarError(f"fact text is {tokens} tokens, over the {max_fact_tokens}-token guard: {text!r}")
    hedge = _HEDGE_PATTERN.search(text)
    if hedge is not None:
        raise FactGrammarError(
            f"fact hedges with {hedge.group(0)!r} -- commit the fact or set kind to {FactKind.UNSURE.value!r}: {text!r}"
        )


def _evidence_suffix(evidence: int) -> str:
    """``" (x3)"`` when a belief has more than one supporting entry, else ``""``."""
    return EVIDENCE_TEMPLATE.format(count=evidence) if evidence > 1 else ""


def render_header(node: Node, *, entries: int) -> str:
    """``name (type) [id] as of YYYY-MM-DD, N entries, aka alias, alias``.

    Five things a reader judging whether to trust a block needs: what it is, what
    kind of thing it is, how to reach the rest of it, how stale it is, and how
    well evidenced it is -- plus the other names it answers to, which is how a
    reader learns the spelling the dreamer kept.

    ``as of`` is ``last_touched_at``'s date: when this node's CONTENT was last
    written. Not ``dreamed_at``, which can be ``None``, and not ``created_at``,
    which says nothing about the facts. *entries* is the node's ledger entry
    count, from ``LedgerStore.entry_counts`` -- passed in rather than derived
    here because this module has no store and a header must not do I/O.

    The rendered id is the NODE id, not ``ledger_key``. Amendment D carries ids
    back to the agent so it can traverse, and the id is what every call in the
    footer takes; ``ledger_key`` is a storage detail that happens to equal it
    today.

    Raises:
        ValueError: on a negative entry count, which is a caller arithmetic
            defect rather than a state a node can be in.
    """
    if entries < 0:
        raise ValueError(f"a node cannot have {entries} ledger entries: {node.node_id}")
    as_of = node.last_touched_at.date().isoformat()
    head = f"{node.name} ({node.type}) [{node.node_id}] as of {as_of}, {entries} entries"
    if node.aliases:
        return f"{head}{ALIAS_PREFIX}{', '.join(node.aliases)}"
    return head


def render_fact(fact: Fact) -> str:
    """``<label>: <text>``, plus ``(xN)`` when more than one entry supports it.

    The label is the fact's ``key`` when it has one -- ``chat default:
    claude-haiku-4-5`` reads as the attribute it is -- and otherwise the word for
    its kind. A ``key`` is only ever present on an attribute
    (:class:`~memotron.caveman.models.Fact` enforces that), so no other kind
    can lose its kind word to a label.
    """
    label = fact.key if fact.key is not None else FACT_LABEL[fact.kind]
    return f"{label}{LABEL_SEPARATOR}{fact.text}{_evidence_suffix(fact.evidence)}"


def render_relation(relation: Relation, *, node_id: str, names: Mapping[str, str] | None = None) -> str:
    """One edge, rendered from the point of view of *node_id*.

    Outgoing: ``TYPE target [n-004] until #245: claim``.
    Incoming: ``from source [n-001] TYPE until #245: claim``.

    Two renderings rather than one because direction is part of the belief: a
    node that BLOCKED another is not the node that was blocked, and a reader
    given one form for both would have to look the ids up to tell which. The
    incoming form leads with ``from`` so the asymmetry is a word rather than a
    position.

    *names* maps node ids to names, for the OTHER endpoint. A missing name
    renders the id alone -- ``BLOCKED [n-004]: ...`` -- because an id is the
    thing the agent can act on and a rendering that omitted it would be a dead
    end. That case is normal: a read emits a bounded set of nodes and an edge can
    point outside it.

    Raises:
        ValueError: if *node_id* is neither endpoint. A relation rendered from a
            node it does not touch would silently invert the direction it
            reports, which is worse than a caller defect that says so.
    """
    lookup = names or {}
    until = f" until {relation.until}" if relation.until is not None else ""
    evidence = _evidence_suffix(relation.evidence)
    if node_id == relation.source_id:
        other = relation.target_id
        head = f"{relation.type} {lookup[other]} [{other}]" if other in lookup else f"{relation.type} [{other}]"
        return f"{head}{until}{LABEL_SEPARATOR}{relation.claim}{evidence}"
    if node_id == relation.target_id:
        other = relation.source_id
        head = (
            f"from {lookup[other]} [{other}] {relation.type}" if other in lookup else f"from [{other}] {relation.type}"
        )
        return f"{head}{until}{LABEL_SEPARATOR}{relation.claim}{evidence}"
    raise ValueError(
        f"relation {relation.relation_id} connects {relation.source_id} to {relation.target_id} and cannot "
        f"be rendered from {node_id}"
    )


def sort_facts(facts: Sequence[Fact]) -> tuple[Fact, ...]:
    """Order facts by :data:`FACT_ORDER`, stably. What a node is stored in.

    Deterministic order is worth more than the model's order: a reader skims, and
    a set of eight facts in one fixed kind order can be skimmed by kind without
    being read. Stable within a kind, so the dreamer's own sequencing of two
    attributes survives.
    """
    return tuple(sorted(facts, key=lambda fact: FACT_ORDER[fact.kind]))


def render_node(
    node: Node,
    relations: Sequence[Relation] = (),
    *,
    entries: int,
    names: Mapping[str, str] | None = None,
) -> str:
    """Header, then this node's facts and relations in one fixed reading order.

    Rules, definition, attributes, relations (outgoing before incoming), then
    what is unsure. ``SUPERSEDED`` facts are not rendered -- see the module
    docstring.

    Nothing is re-validated here: the dreamer is the only writer of fact text and
    it validated what it wrote. Re-checking on the read path would make a read
    able to fail on state it cannot repair.

    *relations* defaults to empty so a caller that only holds a node can still
    render it; *names* is the id-to-name map used for the other end of each edge.
    """
    ordered = sort_facts([fact for fact in node.facts if fact.kind in READ_FACT_KINDS])
    body = [render_fact(fact) for fact in ordered if FACT_ORDER[fact.kind] < RELATION_ORDER]
    outgoing = [relation for relation in relations if relation.source_id == node.node_id]
    incoming = [relation for relation in relations if relation.target_id == node.node_id]
    body.extend(render_relation(relation, node_id=node.node_id, names=names) for relation in (*outgoing, *incoming))
    body.extend(render_fact(fact) for fact in ordered if FACT_ORDER[fact.kind] > RELATION_ORDER)
    return "\n".join((render_header(node, entries=entries), *body))


def footer(node_ids: Sequence[str]) -> str:
    """``more: explain(n-001, n-004), neighbors(n-001, n-004)``.

    The two calls an agent can make from a read, over the nodes the read emitted,
    in block order. One line for the whole read rather than one per block: the
    reader needs the verbs once, and the ids are what varies.
    """
    joined = ", ".join(node_ids)
    return f"{FOOTER_PREFIX}{EXPLAIN_CALL}({joined}), {NEIGHBORS_CALL}({joined})"


def format_prompt_block(*, max_facts: int, kinds: Sequence[FactKind]) -> str:
    """The rendering contract, written for the model. Embedded verbatim by every prompt.

    *kinds* is the kinds THIS prompt's answer may use, and it is required rather
    than defaulted for the reason this whole block states no JSON shape: two
    descriptions of one thing in one prompt is one too many. A node dream passes
    :attr:`~memotron.caveman.dream.Evidence.usable_fact_kinds`, which omits
    ``SUPERSEDED`` when the ledger marks nothing superseded; the stages that emit
    no facts of their own pass every member.

    Three live runs were lost to the version of this that iterated the enum
    unconditionally. The prompt listed all five kinds here and prohibited one of
    them in prose further down, and each time the model answered with the
    prohibited kind for a value a LATER EPISODE had replaced -- which the ledger
    cannot link and the validation refuses. A list is read; a prohibition beside
    a list is not. So the list is the prohibition.

    A sixth kind still cannot be added without the prompt learning about it: the
    rows are generated from what the caller passes, and every caller passes
    either the whole enum or a filter over it.

    Takes ``max_facts`` and no token budget: ``L`` is a count the model must
    respect and can hold to, while the per-fact guard is a paragraph guard the
    prompt deliberately does not state -- a number the model cannot count is a
    number it will miss (#251 amendment A).

    .. rubric:: This block states no JSON shape, and that is load-bearing

    It says what a belief IS and what makes a good one. What to SEND is each
    stage's own answer contract, stated beside its worked example, and the block
    closes by saying so.

    The first live run under this block did not: it described a fact as a record
    with a ``kind`` and an optional ``key``, while the global pass's answer
    contract still asks for ``lines`` of plain strings (#251 amendment D's D0
    bridge -- the named-field contract is package D-C). The model read both and
    sent objects where strings were asked for, and the whole answer was rejected.
    Two descriptions of one shape is one too many, so this block describes none.
    """
    rows = "\n".join(f"  {kind.value:<11} {FACT_GLOSS[kind]}" for kind in kinds)
    examples = "\n".join(f"  {text}" for text in EXAMPLE_FACT_TEXTS)
    hedges = ", ".join(sorted(HEDGE_WORDS))
    return (
        "HOW A BELIEF IS RECORDED\n"
        "A node holds FACTS about one concept, in plain words. A belief between\n"
        "two concepts is a RELATION: a typed edge from one node to another,\n"
        "carrying its own claim. There is no symbol alphabet to learn.\n"
        "\n"
        "FACT KINDS -- these and no others\n"
        f"{rows}\n"
        "\n"
        "WHAT MAKES A GOOD FACT\n"
        f"  - {BREVITY_RULE}\n"
        f"  - At most {max_facts} facts on one node, total.\n"
        "  - One line of plain words. No leading or trailing space, no newline.\n"
        f"  - Do not hedge. Words like {hedges} are rejected:\n"
        f"    commit the fact, or record it as {FactKind.UNSURE.value!r}.\n"
        "  - Keep identifiers verbatim -- issue numbers, model aliases, counts,\n"
        "    dimensions. They are the highest-value tokens in the whole system,\n"
        "    and an identifier you did not read in the source is a rejection.\n"
        "  - Articles and short verbs are fine. Brief means one fact per record,\n"
        "    not keywords: do not mangle a fact into a telegram.\n"
        "  - A fact too big for one record is two facts, not one long one.\n"
        "\n"
        "EXAMPLE FACT TEXT\n"
        f"{examples}\n"
        "\n"
        "The ANSWER CONTRACT further down states exactly which JSON keys to send\n"
        "and what type each one takes. Follow it literally. Nothing above changes\n"
        "the shape of your answer.\n"
    )
