"""The deep read: one node's beliefs, their evidence and its history. **No LLM call.**

The other half of what a header promises. ``render.render_header`` renders
``[<node_id>]``, and :mod:`memotron.caveman.read` ends every read with
``more: explain(<node_id>), neighbors(<node_id>)`` -- this is what the first of
those calls resolves to. Amendment A names the symptom it fixes: a pointer with
no verb, in a header a reader could otherwise only decode by hand.

.. rubric:: Why this is its own module rather than a second function in ``read``

Because it is the opposite operation on the same data. ``read`` is **bounded and
compressed**: a budget, a rank, a cut, and the beliefs that survived them. This
is **unbounded and raw**: every belief the node holds including the ones a read
never shows, every claim ever appended for it, and every graph mutation that
produced it -- with no budget at all. Folding them together would put a token
budget in the same function as the one operation that deliberately has none, and
one of the two would end up wrong.

They compose the other way round instead: a read hands the reader node ids, and
those ids are what this takes. One responsibility each.

.. rubric:: Five sections, in the order a question about provenance is asked

``facts:`` what the node asserts now, each with its evidence entry ids, and
including the ``superseded`` ones a bounded read never shows. ``relations:``
what it asserts about other concepts, with the ids of the nodes at the other
end. ``evidence:`` every ledger claim behind all of it, newest first, with its
supersession edges both ways. ``history:`` the graph mutations that wrote it.
``aliases:`` the other names it answers to.

The order is the question: *what do you believe*, *about what else*, *on what
evidence*, *how did you come to*, *and what else is this called*. Reading it
top-down is reading a belief back to the claim that made it.

Every section is rendered even when it is empty, as the aliases line always has
been: "this node holds no rule" and "nothing else routes to this node" are both
information, and a section that vanishes when empty makes a reader guess whether
they asked the wrong question.

.. rubric:: Evidence and history are different records and both are here

``Fact.entry_ids`` and ``Relation.entry_ids`` name the claims a belief rests on
-- that is the reinforcement record, and ``(xN)`` in a read is its count. Naming
an id without resolving it would be the same dead end a bare ``[n-001]`` was, so
``evidence:`` lists the claims themselves, in the words the extractor recorded
them in.

``history:`` is the journal: one line per :class:`DreamEvent` this node was part
of (#251 amendment D). That is what makes compression answerable rather than
merely trusted -- ``replay`` proves the whole scope rebuilds from these events,
and this is where one node's share of them is legible.

One gap, stated rather than hidden: an ``edge_type_renamed`` event names no
nodes, because it re-labels however many relations carry a type and naming every
endpoint would record the type's popularity rather than the decision. So a
rename appears in ``replay`` and in ``LedgerStore.events`` but in no single
node's ``history:``.

.. rubric:: What the rendering is for

An LLM, not a terminal. Hence one record per line, dates as ``YYYY-MM-DD``, and
no table drawing: a model reads ``2026-09-08 rule: never hermetic [e-001]``
without any layout to decode, and the kind word is the same vocabulary a node's
own facts use.

Printable ASCII only, like every other rendering in this package (#251 amendment
D). Facts and relations are rendered by :mod:`memotron.caveman.render`, not
here: this module adds the date and the evidence ids around the renderer's line,
so a change to what a belief looks like is still a change to one file.

.. rubric:: Supersession is rendered from both ends

An entry carries ``supersedes``: the entry it replaced. The reverse edge -- "this
one was later replaced" -- is what actually stops a reader from acting on a stale
claim, so it is computed here from the node's own entries and rendered as
``SUPERSEDED by``. Scoped to this node deliberately: a supersession within one
concept is the case that matters and the only one the node's own record can see,
and reaching across nodes would need a store query no seam offers.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memotron.caveman.models import DreamEvent, DreamOp, Fact, LedgerEntry, Node, Relation
from memotron.caveman.render import render_fact, render_header, render_relation, sort_facts
from memotron.caveman.seams import GraphStore, LedgerStore

FIELD_SEPARATOR = " | "
"""What separates one rendered field of a ledger claim from the next.

A spaced pipe rather than a tab, which a model sees as whitespace it may
collapse, and rather than a bare comma, which a claim's own prose contains. The
node header no longer uses pipes at all, so there is nothing here to confuse it
with.

Only the ``evidence:`` section uses it. A fact and a relation are rendered by
:mod:`memotron.caveman.render`, whose separator is a colon, and a second
delimiter inside those lines would be this module overruling the renderer.
"""

FACTS_SECTION = "facts:"
RELATIONS_SECTION = "relations:"
EVIDENCE_SECTION = "evidence:"
HISTORY_SECTION = "history:"
"""The four section labels, in render order. Plain words with a colon.

Named constants rather than inline strings because a caller that wants to slice
one section out of the text -- the MCP surface, a test -- should key off the same
string this writes, not a copy of it.
"""

ALIAS_PREFIX = "aliases: "
"""How the final line begins. ``aliases: none`` when the node has none.

Rendered even when empty, because "this node answers to nothing else" is
information: it is the difference between a search that could have found this by
another name and one that could not.
"""

NOTHING = "none"
"""What an empty section renders as. See the module docstring on why it renders at all."""

NO_ALIASES = f"{ALIAS_PREFIX}{NOTHING}"
"""The final line of a node nothing else routes to."""

SUPERSEDES_FIELD = "supersedes "
"""Marks the entry this claim replaced."""

SUPERSEDED_FIELD = "SUPERSEDED by "
"""Marks that a later claim replaced this one. Capitalised on purpose.

The one field in this rendering that says "do not act on this line", so it is
the one field a skimming reader has to see. Everything else here is history; a
superseded claim is history that still looks current.
"""

EVIDENCE_OPEN = " ["
EVIDENCE_CLOSE = "]"
"""How a belief's evidence ids are bracketed: ``rule: never hermetic [e-001, e-014]``.

Square brackets, the same way a node id is carried, because both are record
identifiers a reader may want to quote back.
"""


def _date(moment: datetime) -> str:
    """``YYYY-MM-DD``. The one date format this package renders."""
    return moment.date().isoformat()


def _evidence(entry_ids: Sequence[str]) -> str:
    """``" [e-001, e-014]"`` -- the claims a belief rests on, in the order it holds them."""
    return f"{EVIDENCE_OPEN}{', '.join(entry_ids)}{EVIDENCE_CLOSE}"


def _superseded_by(entries: Sequence[LedgerEntry]) -> dict[str, tuple[str, ...]]:
    """Per entry id, the later entries of this node that superseded it.

    A tuple rather than one id: nothing in the ledger stops two corrections from
    both naming one earlier claim, and rendering only the first would hide a
    correction. Chronological, following the ledger's own order.
    """
    reverse: dict[str, list[str]] = {}
    for entry in entries:
        if entry.supersedes is not None:
            reverse.setdefault(entry.supersedes, []).append(entry.entry_id)
    return {entry_id: tuple(ids) for entry_id, ids in reverse.items()}


def render_fact_entry(fact: Fact) -> str:
    """``YYYY-MM-DD <kind>: <text> [<entry ids>]`` -- one belief with its provenance.

    The date is ``last_seen``: when a claim last asserted this fact, which is
    what answers "is this still being said". ``first_seen`` is on the record for
    a caller that wants it, and putting both on the line would spend eleven
    characters to answer a question nobody asked of a deep read.

    The belief itself comes from :func:`~memotron.caveman.render.render_fact`,
    so a ``superseded`` fact renders under its kind word and is thereby MARKED
    as history rather than needing a second marker here. That carries the
    renderer's ``(xN)`` evidence count alongside the ids, which is the same
    number twice on purpose: the count is what a reader skimming reads, and the
    ids are what a reader following the provenance needs.
    """
    return f"{_date(fact.last_seen)} {render_fact(fact)}{_evidence(fact.entry_ids)}"


def render_relation_entry(relation: Relation, *, node_id: str, names: Mapping[str, str]) -> str:
    """``YYYY-MM-DD <edge as this node sees it> [<entry ids>]``.

    Rendered from *node_id*'s end by
    :func:`~memotron.caveman.render.render_relation`, so an edge into this
    node reads ``from <source> [<id>]`` and an edge out of it leads with the
    type -- the same asymmetry a bounded read shows, since direction is part of
    the belief.
    """
    return (
        f"{_date(relation.last_seen)} "
        f"{render_relation(relation, node_id=node_id, names=names)}"
        f"{_evidence(relation.entry_ids)}"
    )


def _render_entry(entry: LedgerEntry, *, superseded_by: tuple[str, ...]) -> str:
    """``<id> | YYYY-MM-DD | <kind> | <claim>``, plus whichever supersession fields apply.

    The entry id leads, because this section is what a fact's ``[e-001]`` resolves
    against: a list of claims a reader cannot key by id is a list they have to
    count through.

    Both supersession fields when both apply -- a claim that corrected an earlier
    one and was itself corrected is exactly the line a reader must not act on,
    and dropping either half would leave that invisible.
    """
    fields = [entry.entry_id, _date(entry.ts), entry.kind.value, entry.claim]
    if entry.supersedes is not None:
        fields.append(f"{SUPERSEDES_FIELD}{entry.supersedes}")
    if superseded_by:
        fields.append(f"{SUPERSEDED_FIELD}{', '.join(superseded_by)}")
    return FIELD_SEPARATOR.join(fields)


def _node_of(payload: Mapping[str, Any], key: str) -> Mapping[str, Any]:
    """One node's journalled content out of an event payload, or an empty mapping.

    Journal payloads are plain dicts read back from JSON, so a key a given op
    does not carry is absent rather than ``None``. Empty rather than raising: a
    summary of an event shape this function does not recognise is worth less than
    the event's own op word, and it is never worth an exception on a read.
    """
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def event_summary(event: DreamEvent) -> str:
    """One clause saying what this mutation did, read off the event's own content.

    The journal records the content before and after every mutation, so this
    needs no store and no receipt: what changed is in the event. One clause per
    op, naming the thing a reader of a node's history wants -- which name, which
    type, which peer, how many facts -- rather than a uniform count that would
    make a merge and a rewrite look alike.

    Deliberately NOT the ``detail`` string
    :class:`~memotron.caveman.graph.JournaledGraph` puts on its receipts. That
    is a receipt field and this is a rendering, they are read in different places
    by different readers, and the event does not carry it -- so this is the only
    renderer of a :class:`~memotron.caveman.models.DreamEvent`, not a second
    wording of one.
    """
    before_node = _node_of(event.before, "node")
    after_node = _node_of(event.after, "node")
    relation = _node_of(event.after, "relation") or _node_of(event.before, "relation")
    match event.op:
        case DreamOp.NODE_CREATED:
            node = _node_of(event.after, "node")
            return f"created {node.get('name')!r} as {node.get('type')}"
        case DreamOp.NODE_REWRITTEN:
            return f"{len(after_node.get('facts', ()))} fact(s), type={after_node.get('type')}"
        case DreamOp.NODE_RETYPED:
            return f"retyped {before_node.get('type')} to {after_node.get('type')}"
        case DreamOp.ALIASES_ADDED:
            return f"aliases now {', '.join(after_node.get('aliases', ()))}"
        case DreamOp.NODE_MERGED:
            survivor = _node_of(event.after, "survivor")
            absorbed = [node.get("node_id") for node in event.before.get("absorbed", ())]
            return f"absorbed {', '.join(str(node_id) for node_id in absorbed)} as {survivor.get('name')!r}"
        case DreamOp.NODE_SPLIT:
            parts = [node.get("node_id") for node in event.after.get("parts", ())]
            return f"split into {', '.join(str(node_id) for node_id in parts)}"
        case DreamOp.NODE_DELETED:
            deleted = [node.get("node_id") for node in event.before.get("nodes", ())]
            return f"deleted {', '.join(str(node_id) for node_id in deleted)}"
        case DreamOp.RELATION_UPSERTED:
            verb = "reinforced" if event.before else "created"
            return (
                f"{verb} {relation.get('type')} {relation.get('source_id')} to {relation.get('target_id')}, "
                f"evidence {len(relation.get('entry_ids', ()))}"
            )
        case DreamOp.RELATION_RETIRED:
            return f"retired {relation.get('type')} {relation.get('source_id')} to {relation.get('target_id')}"
        case DreamOp.EDGE_TYPE_RENAMED:
            return f"renamed {event.before.get('type')} to {event.after.get('type')}"


def _render_event(event: DreamEvent) -> str:
    """``YYYY-MM-DD <op>: <summary>``. The op word is the enum's value, verbatim."""
    return f"{_date(event.ts)} {event.op.value}: {event_summary(event)}"


def _section(label: str, lines: Sequence[str]) -> list[str]:
    """A label and its lines, or the label and :data:`NOTHING` when there are none."""
    return [label, *(lines or (NOTHING,))]


def _names_for(relations: Sequence[Relation], *, node: Node, graph: GraphStore) -> dict[str, str]:
    """Id-to-name for the OTHER end of each of this node's edges.

    One ``get_nodes`` call for the whole set rather than one per edge. A missing
    end renders as its bare id, which
    :func:`~memotron.caveman.render.render_relation` documents and which is
    the right answer anyway: the id is the thing a reader can act on.
    """
    others = {
        relation.target_id if relation.source_id == node.node_id else relation.source_id for relation in relations
    }
    return {other.node_id: other.name for other in graph.get_nodes(sorted(others))}


def explain(*, node_id: str, graph: GraphStore, ledger: LedgerStore, now: datetime) -> str:
    """One node's beliefs, their evidence, its journal history, and its aliases.

    The deep read behind a read's ``more: explain(...)`` footer. **Zero LLM calls
    and no embedding**: this is a handful of store reads and a string join, so it
    costs nothing but the rows and can be called on every block of a read the
    caller cares about.

    Unbounded by design -- every belief and every claim, not a page of them. A
    node holds at most ``L`` facts however much evidence backs it and a read
    shows fewer still, so "what is the evidence" and "how did this come to say
    this" are questions the bounded read cannot answer at all, and answering
    them with a budget would put the reader back where they started.

    ``superseded`` facts appear here and only here: a read spends its budget on
    what is true now, and "this used to be true" is what stops a reader
    re-deriving a wrong answer once they are looking at the record.

    The ledger is read twice: ``for_node`` for the claims, ``events`` for the
    history. The history is filtered to the events naming THIS node, newest
    first, which is the same "newest is the one that matters" order the claims
    are in.

    *now* is the bound the rendered dates are checked against, not a field: an
    entry dated after it means two clocks wrote this ledger, and a reader who
    judges staleness from these dates would be misled by even one of them. Same
    fail-fast rule, and the same reason, as
    :func:`~memotron.caveman.pressure.node_value`'s.

    Raises:
        NodeNotFound: if *node_id* is not in the graph. A caller holding an id
            that does not resolve has stale state -- a read's footer names ids
            that existed when it ran -- which is a defect rather than an empty
            answer.
        ValueError: if an entry is dated after *now*.
    """
    node = graph.get_node(node_id)
    entries = ledger.for_node(node_id)
    for entry in entries:
        if entry.ts > now:
            raise ValueError(
                f"ledger entry {entry.entry_id} on node {node_id} is dated {entry.ts.isoformat()}, "
                f"after now={now.isoformat()} -- an entry cannot be newer than the clock"
            )

    relations = graph.relations_of(node_id)
    names = _names_for(relations, node=node, graph=graph)
    reverse = _superseded_by(entries)
    history = [event for event in ledger.events(scope=node.scope) if node_id in event.node_ids]

    return "\n".join(
        (
            render_header(node, entries=len(entries)),
            *_section(FACTS_SECTION, [render_fact_entry(fact) for fact in sort_facts(node.facts)]),
            *_section(
                RELATIONS_SECTION,
                [render_relation_entry(relation, node_id=node_id, names=names) for relation in relations],
            ),
            *_section(
                EVIDENCE_SECTION,
                [_render_entry(entry, superseded_by=reverse.get(entry.entry_id, ())) for entry in reversed(entries)],
            ),
            *_section(HISTORY_SECTION, [_render_event(event) for event in reversed(history)]),
            f"{ALIAS_PREFIX}{', '.join(node.aliases)}" if node.aliases else NO_ALIASES,
        )
    )
