"""The records that cross a caveman seam, and the four LLM wire contracts.

One import for every other module in the package. The response schemas live
here with the durable records because they *are* the wire contract, and
``extra="forbid"`` on them is the fail-fast every stage depends on: a model that
invents a field has misread the contract, which is a real signal, so it is a
rejection rather than a silently dropped key.

**Every model is frozen.** The stores own their state; a ``Node`` handed back by
``GraphStore`` is a value, and mutating it must not be mistakable for a write.
That is what makes "the dreamer is the only writer of fact text" a property of
the code rather than a convention -- the only way to change a node's facts is to
call the store.

**Where a validation rule lives is deliberate.** A rule the response can check
on its own is enforced here (a forward ``supersedes_claim_index``, a duplicate
``local_name``, one node named by two global ops). A rule that needs the
episode, the graph, the ledger or the motive is enforced by the stage that has
them, and is *not* half-checked here -- one owner per rule, so a rejection has
one error shape and one place to read it from.

.. rubric:: Beliefs are typed records, not symbols (#251 amendment D)

There used to be a ``Sigil`` alphabet -- six invented characters, one per line
kind, with a grammar block in every prompt teaching the model to read and write
them. It is gone, along with ``lines.py``. Two records replace it:

* :class:`Fact` -- what a node holds. A named ``kind``, plain text, and the
  ledger entry ids that support it.
* :class:`Relation` -- a belief BETWEEN two concepts, as a typed directed edge
  with its own claim text and its own evidence.

An LLM parses words and JSON natively and has to be taught a symbol alphabet, so
every symbol in a prompt was a legend to render, a rule to state and a thing to
get wrong. The kinds are the same six distinctions; only their spelling changed,
from punctuation to words.

.. rubric:: Evidence is the same field in both records

``entry_ids`` is the evidence, and ``evidence`` is its count. A claim that
restates a fact the node already holds **reinforces** it -- the entry id is
appended and ``last_seen`` moves -- rather than adding a second fact saying the
same thing, so a well-evidenced belief is one record with many entries instead
of many records. The same rule holds for a relation, which is why the two
records carry the same three fields (``entry_ids``, ``first_seen``,
``last_seen``) with the same meaning.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

NODE_TYPE_PATTERN = r"^[a-z][a-z0-9-]{0,23}$"
"""Node types are one lowercase word, optionally hyphenated, at most 24 chars.

Open text rather than a closed vocabulary -- the scope's existing types are
*suggested* to the model, never imposed -- but the shape is fixed so a type
cannot arrive as a sentence, and the ceiling matches ``Node.type``'s own
``max_length`` so a valid response can never fail the record it becomes.
"""

EDGE_TYPE_PATTERN = r"^[A-Z][A-Z0-9_]{1,31}$"
"""Relation types are UPPER_SNAKE, 2 to 32 characters.

Upper case because a relation type is a *label on an edge* and a node type is a
word about a thing: two vocabularies that are read in the same prompt need to be
distinguishable on sight, and the property-graph convention this borrows from
(``BLOCKED``, ``DEPLOYS``) is already what a reader expects.

Bounded at 32 so a type cannot arrive as a sentence, and at two characters
because a single letter names nothing. The per-scope vocabulary is bounded
separately, by ``CavemanMotive.max_edge_types``, which the dreamer enforces.
"""

MAX_NODE_NAME = 40
"""Ceiling on a node name. Shared by ``Node``, ``NewNodeSpec`` and the merge/split ops."""

MAX_FACT_TEXT = 300
"""Ceiling on the text of one :class:`Fact`, and of one :class:`FactSpec`.

A record-level ceiling, not the brevity rule. The motive's ``max_fact_tokens`` is
the paragraph guard a stage applies with its own budget; this is the bound past
which a string is not a belief at all.
"""

MAX_REASON_TEXT = 600
"""Ceiling on a ``reason`` -- the model's own account of why it answered as it did.

A **paragraph guard**, the same kind of bound as ``CavemanMotive.max_fact_tokens``
and set for the same reason: a model cannot count characters, and nothing in any
prompt states this number. At 300 it was not a guard but a cap, and a live run
lost a whole node answer to ``reason: String should have at most 300
characters`` -- a rejection about the justification and not about a single
belief in it.

600 is :attr:`Receipt.detail`'s own bound, which is where a reason goes: both
``dream`` and ``JournaledGraph`` truncate a detail at 600 before emitting it, so
nothing downstream can read more of a reason than this, and a looser field would
only store text no reader ever sees.
"""

MAX_CLAIM_TEXT = 400
"""Ceiling on one CLAIM: a :class:`ClaimSpec`'s, a :class:`LedgerEntry`'s, a :class:`Relation`'s.

One number for all of them, and that is load-bearing. A provisional relation
carries the ledger claim **verbatim** (#251 amendment D), so an edge bound
tighter than the ledger's would leave claims the extractor is allowed to record
unroutable -- and truncating one would falsify the record while normalising it
would stop being verbatim. Reconcile used to refuse such a batch before calling
the model; with one bound there is nothing left to refuse.

Looser than :data:`MAX_FACT_TEXT` because a claim is the ledger's full
self-contained sentence and a fact is the dreamer's compression of one.
"""


class ClaimKind(StrEnum):
    """What kind of claim one ledger entry states. Stage 1's vocabulary.

    Six words, replacing the six sigils (#251 amendment D). Two of them have no
    :class:`FactKind` counterpart, deliberately:

    * ``RELATION`` -- the claim is about two concepts, so it becomes a
      :class:`Relation` rather than a fact on either of them;
    * ``CORRECTION`` -- the claim replaces an earlier one. What it produces is a
      rewritten fact plus a ``SUPERSEDED`` one, which is a dream decision and
      not something the extractor can state.
    """

    RULE = "rule"
    IS = "is"
    ATTRIBUTE = "attribute"
    UNSURE = "unsure"
    RELATION = "relation"
    CORRECTION = "correction"


class FactKind(StrEnum):
    """What kind of belief one :class:`Fact` is. Stage 3's vocabulary.

    Five words, in reading order: the rule that must never be violated, what the
    thing IS, its properties, what is believed but unestablished, and what was
    stated and later replaced.

    ``SUPERSEDED`` is the one kind a read never renders -- it lives in the deep
    read, where "this used to be true" is what stops a reader re-deriving the
    wrong answer, rather than in a bounded block where it would cost a slot.
    """

    RULE = "rule"
    IS = "is"
    ATTRIBUTE = "attribute"
    UNSURE = "unsure"
    SUPERSEDED = "superseded"


class ClaimMode(StrEnum):
    """What kind of speech act a claim is, as stated in its episode.

    Orthogonal to :class:`ClaimKind`, which is what the claim is ABOUT. A
    correction and a report can both be about an attribute; the mode is what
    tells the dreamer one supersedes and the other accumulates.
    """

    DESCRIPTIVE = "descriptive"
    REQUIREMENT = "requirement"
    PREFERENCE = "preference"
    DIRECTIVE = "directive"
    CORRECTION = "correction"
    REPORT = "report"


class ReceiptOp(StrEnum):
    """Every decision this subsystem receipts.

    Both halves of each stage are present -- ``*_ACCEPTED`` / ``*_APPLIED``
    alongside ``*_REJECTED`` -- because a rejection that is not receipted is a
    prompt regression nobody can see. ``READ_BUDGET_SATURATED`` is here for the
    same reason: a truncated read is a deterministic documented outcome, not a
    silent drop.

    ``GRAPH_MUTATED`` is the journal's own op (#251 amendment D). Every graph
    mutation recorded as a :class:`DreamEvent` carries a receipt id, and the
    journal is not a stage -- it wraps whichever stage is writing -- so it cannot
    borrow one of the stage ops without claiming a decision it did not make.

    ``DREAM_EDGE_TYPE_RENAMED`` and ``DREAM_RELATION_RETIRED`` are the two ops
    the edge-type vocabulary added (#251 amendment D, package D-C): compacting
    the vocabulary to ``M`` and retiring a belief are both decisions a reviewer
    has to be able to find, and neither is a node write.

    ``DREAM_DEMOTED`` is gone with the ``demoted`` field it receipted. The node
    contract is now named-field facts, each carrying its own evidence, and a
    fact the answer leaves out is gone from the node -- so a second list
    restating which facts left it was a cross-check on a complete answer rather
    than a decision of its own.
    """

    EXTRACT_ACCEPTED = "extract_accepted"
    EXTRACT_REJECTED = "extract_rejected"
    RECONCILE_BOUND = "reconcile_bound"
    RECONCILE_NEW = "reconcile_new"
    RECONCILE_REJECTED = "reconcile_rejected"
    DREAM_NODE_APPLIED = "dream_node_applied"
    DREAM_NODE_REJECTED = "dream_node_rejected"
    DREAM_MERGED = "dream_merged"
    DREAM_SPLIT = "dream_split"
    DREAM_RETYPED = "dream_retyped"
    DREAM_EDGE_TYPE_RENAMED = "dream_edge_type_renamed"
    DREAM_RELATION_RETIRED = "dream_relation_retired"
    DREAM_GLOBAL_REJECTED = "dream_global_rejected"
    GRAPH_MUTATED = "graph_mutated"
    READ_EMITTED = "read_emitted"
    READ_BUDGET_SATURATED = "read_budget_saturated"
    ERASURE_APPLIED = "erasure_applied"


class DreamOp(StrEnum):
    """Every graph mutation the journal records, one member per store write.

    The list is exactly the set of :class:`~memotron.caveman.seams.GraphStore`
    operations that change content. ``mark_dirty``, ``clear_dirty`` and
    ``record_read`` are absent because they change bookkeeping rather than what
    the graph asserts, and a journal that recorded them would make a read a
    writer of history.
    """

    NODE_CREATED = "node_created"
    NODE_REWRITTEN = "node_rewritten"
    NODE_MERGED = "node_merged"
    NODE_SPLIT = "node_split"
    NODE_RETYPED = "node_retyped"
    NODE_DELETED = "node_deleted"
    ALIASES_ADDED = "aliases_added"
    RELATION_UPSERTED = "relation_upserted"
    RELATION_RETIRED = "relation_retired"
    EDGE_TYPE_RENAMED = "edge_type_renamed"


class _CavemanRecord(BaseModel):
    """Shared config for every record in this module.

    Private so it never reaches the public API surface. ``extra="forbid"`` is
    the whole point on the response schemas and costs nothing on the durable
    records; ``frozen=True`` is what keeps the stores the only writers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


def _one_line(value: str, *, what: str) -> None:
    """Raise unless *value* is a single line with no leading or trailing space.

    One helper because five records enforce it -- a fact's text, a relation's
    claim, and the wire specs for both -- and a padded or multi-line string
    breaks a *rendering* rather than a record, so it has to be refused at the
    record rather than discovered by a reader.

    *what* is the whole sentence up to the value, so each caller keeps its own
    wording: ``"fact text must be one line with no leading or trailing space"``
    reads differently from the same rule stated about a claim, and the messages
    are what a rejection receipt carries.
    """
    if value != value.strip() or "\n" in value:
        raise ValueError(f"{what}, got {value!r}")


def _no_repeats(values: Sequence[str], *, what: str) -> None:
    """Raise unless *values* names nothing twice. An EMPTY sequence is fine.

    Separate from :func:`_distinct` on exactly that point. Evidence has to exist
    -- a belief with no entry supporting it is not a belief -- while a list of
    what an answer retires is normally empty, so the two rules cannot share one
    function without one of them being wrong for its caller.
    """
    repeated = sorted({value for value in values if values.count(value) > 1})
    if repeated:
        raise ValueError(f"{what} names the same id twice: {', '.join(repeated)}")


def _distinct(ids: Sequence[str], *, what: str) -> None:
    """Raise unless *ids* are distinct and none is blank.

    Evidence is a SET of entry ids wearing a tuple's clothes: the order is the
    order they arrived in, which is worth keeping, but the same entry counted
    twice would inflate ``evidence`` and make a reinforcement look like two.
    Asserted rather than deduped for the reason :class:`Node` asserts its
    aliases: a record that silently rewrote what it was handed lets two callers
    disagree and both appear to succeed.
    """
    if not ids:
        raise ValueError(f"{what} needs at least one ledger entry id -- a belief with no evidence is not a belief")
    blank = [value for value in ids if not value.strip()]
    if blank:
        raise ValueError(f"{what} carries a blank ledger entry id")
    repeated = sorted({value for value in ids if ids.count(value) > 1})
    if repeated:
        raise ValueError(f"{what} names the same ledger entry twice: {', '.join(repeated)}")


# --------------------------------------------------------------------- episode


class Turn(_CavemanRecord):
    """One utterance in an episode. ``index`` is 1-based, as a reader would count."""

    index: int = Field(ge=1)
    speaker: str = Field(min_length=1)
    text: str = Field(min_length=1)


class Episode(_CavemanRecord):
    """Raw, immutable input to stage 1. Never written to, never derived from."""

    episode_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    occurred_at: datetime
    turns: tuple[Turn, ...] = Field(min_length=1)

    def as_prompt_text(self) -> str:
        """The exact string stage 1 sees: ``"1. Logan: ..."``, one turn per line.

        This is the *only* rendering of an episode, so the extract contract's
        "every identifier must appear verbatim in the episode" check and the
        prompt the model reads are provably the same text.

        The separator is ``". "`` and not a middle dot: every character this
        subsystem puts in front of a model is printable ASCII (#251 amendment
        D), and a numbered list is the shape a model already reads without being
        told what the punctuation means.
        """
        return "\n".join(f"{turn.index}. {turn.speaker}: {turn.text}" for turn in self.turns)

    def turn_indices(self) -> frozenset[int]:
        """The real turn indices, for the extract contract's ``turns`` check."""
        return frozenset(turn.index for turn in self.turns)


# ---------------------------------------------------------------------- ledger


class LedgerEntry(_CavemanRecord):
    """One claim, appended once and never edited except to bind node ids.

    ``claim`` is a FULL sentence, self-contained and resolvable without the
    episode -- that is what makes the ledger a usable deep-read target and what
    lets a node be re-dreamed from entries alone after an erasure.

    ``subjects``/``objects`` hold the episode's own surface words, un-normalised,
    because normalising them is stage 2's job and stage 1 has never seen the
    graph. ``node_ids`` is filled by reconcile and by nothing else.
    """

    entry_id: str = Field(min_length=1)
    ts: datetime
    episode_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    claim: str = Field(min_length=8, max_length=MAX_CLAIM_TEXT)
    kind: ClaimKind
    claim_mode: ClaimMode
    subjects: tuple[str, ...] = Field(min_length=1)
    objects: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    node_ids: tuple[str, ...] = ()
    motive: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    supersedes: str | None = None
    turns: tuple[int, ...] = Field(min_length=1)
    receipt_id: str = Field(min_length=1)

    @model_validator(mode="after")
    def _claim_is_one_line(self) -> Self:
        """The same rule :class:`Relation` applies, because an edge carries this text.

        A relational claim becomes an edge's claim verbatim, and every rendering
        of an entry -- a reconcile prompt, a dream prompt, an ``explain`` -- puts
        it on one line. So a padded or multi-line claim breaks a rendering rather
        than a record, and it is refused here instead of discovered by a reader.
        """
        _one_line(self.claim, what="a ledger claim must be one line with no padding")
        return self


class NodeAlias(_CavemanRecord):
    """A merge, recorded so it can be replayed backwards.

    ``moved_entry_ids`` is exactly what ``LedgerStore.rekey_node`` returned, so
    un-merging is a replay rather than a guess about which entries came from
    where.
    """

    alias_node_id: str = Field(min_length=1)
    survivor_node_id: str = Field(min_length=1)
    moved_entry_ids: tuple[str, ...] = ()
    receipt_id: str = Field(min_length=1)
    ts: datetime


class DreamEvent(_CavemanRecord):
    """One graph mutation, with the content it changed, appended to the ledger.

    This is what makes compression **replayable**: the ledger holds the claims
    and the events hold every decision taken over them, so a scope's readable
    state is not merely regenerable in principle but rebuildable by applying a
    recorded sequence -- with no model call and no embedding -- and comparable to
    the live graph by digest.

    *before* and *after* hold node and relation CONTENT -- names, types, aliases,
    facts, claims, evidence -- and never embeddings. A vector is derived from the
    content, so journalling it would double the ledger's size to record something
    the content already determines, and a replay that had to reproduce vectors
    would need the embedding transport that made them.

    One caveat, shared with ``CavemanMotive``'s weight maps: ``frozen=True`` does
    not freeze a dict's contents, so mutating *before* or *after* in place
    silently rewrites recorded history. Build a new event instead.
    """

    event_id: str = Field(min_length=1)
    ts: datetime
    scope: str = Field(min_length=1)
    op: DreamOp
    node_ids: tuple[str, ...] = ()
    """The nodes this mutation touched, in a stable order.

    Empty is valid and means the mutation was not about particular nodes:
    ``EDGE_TYPE_RENAMED`` rewrites a label across however many relations carry
    it, and naming every endpoint would record the type's popularity rather than
    the decision.
    """

    before: dict[str, Any] = Field(default_factory=dict)
    after: dict[str, Any] = Field(default_factory=dict)
    receipt_id: str = Field(min_length=1)


# ----------------------------------------------------------------------- graph


class Fact(_CavemanRecord):
    """One belief a node holds: a kind, plain text, and the evidence for it.

    Plain words rather than a sigil-prefixed string, and a record rather than a
    line, for the reason the module docstring gives: the kind is a field a model
    can emit and a reader can filter on, and the evidence is a field rather than
    something recoverable only by searching the ledger for the claim's wording.

    ``key`` is the attribute's label -- ``chat default`` in ``chat default:
    claude-haiku-4-5``. It is allowed ONLY on an ``ATTRIBUTE``: every other kind
    renders under its own kind word, so a key on a rule is a field the renderer
    would silently ignore, and a field that is silently ignored is a field two
    callers will disagree about.
    """

    kind: FactKind
    text: str = Field(min_length=1, max_length=MAX_FACT_TEXT)
    key: str | None = Field(default=None, min_length=1, max_length=40)
    entry_ids: tuple[str, ...] = Field(min_length=1)
    first_seen: datetime
    last_seen: datetime

    @property
    def evidence(self) -> int:
        """How many ledger entries support this fact. One is the floor, never zero.

        What ``(x3)`` renders from and what ranking reads: a fact three separate
        claims asserted is worth more of a budget than one nobody has repeated.
        """
        return len(self.entry_ids)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _distinct(self.entry_ids, what=f"fact {self.text!r}")
        if self.key is not None and self.kind is not FactKind.ATTRIBUTE:
            raise ValueError(
                f"fact {self.text!r} is a {self.kind.value!r} and carries key={self.key!r} -- only an "
                f"{FactKind.ATTRIBUTE.value!r} fact has a label, every other kind renders under its kind word"
            )
        if self.last_seen < self.first_seen:
            raise ValueError(
                f"fact {self.text!r} was last seen {self.last_seen.isoformat()}, before it was first seen "
                f"{self.first_seen.isoformat()}"
            )
        _one_line(self.text, what="fact text must be one line with no leading or trailing space")
        return self


class Relation(_CavemanRecord):
    """A belief BETWEEN two concepts: a typed, directed edge carrying its own claim.

    The amendment's central change. A textless co-occurrence weight said only
    "these two were mentioned together"; a relation says WHAT holds between them,
    under a type drawn from a bounded per-scope vocabulary, with its own
    evidence. That is what lets the matcher answer the three-way question it is
    actually asked -- new concept, new belief about a concept, or an existing kind
    of belief now applied to another pair.

    ``until`` marks a relation the record says has ended or been fixed -- an
    issue number, a version, a date as text. It is deliberately free text and not
    a timestamp: what the ledger holds is ``#245 fixed the rewrite``, and
    inventing a datetime for it would assert precision nobody stated.

    Directed, and a self-loop is refused: an edge from a thing to itself asserts
    nothing a fact on the node cannot state better, and it is what a merge
    produces if nobody stops it (the survivor inherits an edge to the node it
    just absorbed).
    """

    relation_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    target_id: str = Field(min_length=1)
    type: str = Field(pattern=EDGE_TYPE_PATTERN)
    claim: str = Field(min_length=1, max_length=MAX_CLAIM_TEXT)
    entry_ids: tuple[str, ...] = Field(min_length=1)
    until: str | None = Field(default=None, min_length=1, max_length=40)
    first_seen: datetime
    last_seen: datetime

    @property
    def evidence(self) -> int:
        """How many ledger entries support this edge. The reinforcement count."""
        return len(self.entry_ids)

    @property
    def triple(self) -> tuple[str, str, str]:
        """``(source_id, type, target_id)`` -- the identity an upsert reinforces.

        One place the key is spelled, because the store looks a relation up by it
        and a test asserts on it, and two spellings of one identity is how an
        upsert starts creating duplicates.
        """
        return (self.source_id, self.type, self.target_id)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _distinct(self.entry_ids, what=f"relation {self.relation_id}")
        if self.source_id == self.target_id:
            raise ValueError(
                f"relation {self.relation_id} points {self.source_id} at itself -- an edge from a thing to "
                f"itself asserts nothing a fact on the node cannot state"
            )
        if self.last_seen < self.first_seen:
            raise ValueError(
                f"relation {self.relation_id} was last seen {self.last_seen.isoformat()}, before it was first "
                f"seen {self.first_seen.isoformat()}"
            )
        _one_line(self.claim, what="relation claim must be one line with no padding")
        return self


def normalize_aliases(*, name: str, aliases: Iterable[str]) -> tuple[str, ...]:
    """The canonical alias set for a node called *name*.

    One rule, in one place, because three callers have to agree on it:
    ``GraphStore.create_node``, ``GraphStore.add_aliases`` and the merge that
    unions an absorbed node's surfaces into the survivor. :class:`Node` then
    asserts its own ``aliases`` already ARE this -- the record enforces the
    invariant, the store applies the policy -- so a node whose aliases were built
    some other way fails at construction rather than at search time.

    * **Case-preserving.** ``C4`` and ``c4`` are the same search key but not the
      same string, and the one a person typed is the one worth showing back.
    * **Deduped case-insensitively**, first spelling seen wins.
    * **Never contains ``name``.** The name is already an exact-match key
      (``node_by_alias`` searches ``name`` and ``aliases`` together), so an alias
      repeating it is one more string to keep in sync for no reachability.

    Raises:
        ValueError: on a blank or whitespace-only alias. That is an empty search
            key, which would match a query token that is itself blank; there is
            nothing to repair it to, so it is refused rather than dropped.
    """
    seen = {name.casefold()}
    canonical: list[str] = []
    for alias in aliases:
        if not alias.strip():
            raise ValueError(f"a node alias cannot be blank or whitespace-only, got {alias!r} for {name!r}")
        folded = alias.casefold()
        if folded in seen:
            continue
        seen.add(folded)
        canonical.append(alias)
    return tuple(canonical)


class Node(_CavemanRecord):
    """One bounded concept. At most ``L`` facts, each at most ``T`` tokens.

    ``ledger_key`` equals ``node_id`` today, so ``ledger(node.ledger_key)`` is
    ``for_node(node_id)``. It exists as its own field rather than being derived
    because a future shared ledger will key differently, and the rendered header
    shows the key, not the id.

    Relations are NOT a field here. An edge belongs to neither of its endpoints,
    and storing it on one would make the other's view of it a copy to keep in
    sync; ``GraphStore.relations_of`` is how a node's edges are read.
    """

    node_id: str = Field(min_length=1)
    scope: str = Field(min_length=1)
    name: str = Field(min_length=1, max_length=MAX_NODE_NAME)
    aliases: tuple[str, ...] = ()
    """Every other surface name that routes here. See :func:`normalize_aliases`.

    A searcher asks by the name they know, not by the name the dreamer kept, so
    the names reconcile bound to this node and the names it absorbed in a merge
    are kept as exact-match keys and are part of the node's embedding text.
    """

    type: str = Field(min_length=1, max_length=24)
    facts: tuple[Fact, ...] = ()
    embedding: tuple[float, ...] = ()
    ledger_key: str = Field(min_length=1)
    dirty: bool = False
    read_count: int = Field(default=0, ge=0)
    created_at: datetime
    last_touched_at: datetime
    dreamed_at: datetime | None = None

    @model_validator(mode="after")
    def _aliases_are_canonical(self) -> Self:
        """``aliases`` must already be :func:`normalize_aliases`' output.

        Enforced rather than coerced: a record that silently rewrote what it was
        handed would let two callers disagree about the alias set and both
        appear to succeed. The store normalises before it constructs, so the
        only way to fail this is to build a ``Node`` by hand.
        """
        canonical = normalize_aliases(name=self.name, aliases=self.aliases)
        if canonical != self.aliases:
            raise ValueError(
                f"node {self.name!r} aliases must be case-insensitively distinct from each other and "
                f"from the name: {list(self.aliases)} normalises to {list(canonical)}"
            )
        return self


def merged_aliases(*, name: str, survivor: Node, absorbed: Sequence[Node]) -> tuple[str, ...]:
    """The survivor's alias set after a merge: its own, plus every absorbed surface.

    An absorbed node's ``name`` is unioned in alongside its ``aliases`` -- that
    name is exactly what a searcher who remembers the old concept will type, and
    it is the one string a merge would otherwise destroy.

    Public, and used by both sides of a merge, because ``merge_nodes`` takes an
    embedding the caller computed: the caller has to know the resulting alias set
    to embed it, and computing the union twice from two copies of the rule is how
    the stored aliases and the vector stop agreeing.

    *name* is the survivor's name AFTER the merge, since that is what an alias may
    not duplicate. The survivor's own previous name is deliberately not unioned
    in: a merge keeps the survivor's name (see the design's peer rule), so there
    is no previous name to lose.
    """
    return normalize_aliases(
        name=name,
        aliases=(*survivor.aliases, *(surface for node in absorbed for surface in (node.name, *node.aliases))),
    )


# -------------------------------------------------------------------- receipts


class Receipt(_CavemanRecord):
    """One decision, digested. Content-free by construction.

    ``inputs_digest``/``outputs_digest`` are sha256 over the canonical JSON of
    what went in and came out, so a prompt regression is diagnosable from the
    receipt stream without the receipt holding any claim text. ``detail`` is
    bounded at 600 characters for the same reason.
    """

    receipt_id: str = Field(min_length=1)
    op: ReceiptOp
    ts: datetime
    scope: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    inputs_digest: str = Field(min_length=1)
    outputs_digest: str = Field(min_length=1)
    detail: str = Field(default="", max_length=600)


# -------------------------------------------------------- 1 EXTRACT contract


class ClaimSpec(_CavemanRecord):
    """One claim as the extractor states it, before any ledger id exists.

    ``supersedes_claim_index`` points at a SIBLING in this same response by
    position -- the model has no entry ids to point at. ``extract`` resolves it
    to the sibling's ``entry_id`` once ids are assigned.
    """

    claim: str = Field(min_length=8, max_length=MAX_CLAIM_TEXT)
    kind: ClaimKind
    claim_mode: ClaimMode
    subjects: tuple[str, ...] = Field(min_length=1, max_length=4)
    objects: tuple[str, ...] = ()
    identifiers: tuple[str, ...] = ()
    supersedes_claim_index: int | None = None
    confidence: float = Field(ge=0.0, le=1.0)
    turns: tuple[int, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _claim_is_one_line(self) -> Self:
        """:class:`LedgerEntry`'s rule, enforced on the WIRE so a rejection is in contract.

        The entry built from this spec applies the same rule, and discovering it
        there would surface a model's formatting as an internal validation error
        instead of as an out-of-contract answer the stage can receipt and report.
        """
        _one_line(self.claim, what="a claim must be one line with no padding")
        return self


class ExtractResponse(_CavemanRecord):
    """Stage 1's whole answer. 1-64 claims.

    The backward-reference rule is enforced here because it is the one extract
    rule the response can check without the episode: a claim may only supersede
    a claim stated EARLIER in the same response. A forward or self reference is
    a model that has lost track of its own output, and accepting it would build
    a supersession cycle in the ledger.
    """

    claims: tuple[ClaimSpec, ...] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def _supersedes_points_backwards(self) -> Self:
        for position, spec in enumerate(self.claims):
            target = spec.supersedes_claim_index
            if target is None:
                continue
            if not 0 <= target < position:
                raise ValueError(
                    f"claim {position} supersedes_claim_index={target} is not a backward reference "
                    f"(must be 0 <= index < {position})"
                )
        return self


# ------------------------------------------------------ 2 RECONCILE contract


class NewNodeSpec(_CavemanRecord):
    """A concept stage 2 found no home for. ``gloss`` is one line, never a fact set.

    A new node is created with ZERO facts and marked dirty; the dreamer writes
    its first fact set. The gloss exists only to be offered back as a kNN
    candidate description, which is why the truth layer never reads a node's
    facts: those are motive-shaped and stage 2 is motive-neutral.
    """

    name: str = Field(min_length=1, max_length=MAX_NODE_NAME)
    type: str = Field(pattern=NODE_TYPE_PATTERN)
    gloss: str = Field(min_length=1, max_length=200)


class BindingSpec(_CavemanRecord):
    """One surface name's adjudication: it is an offered node, or it is new."""

    local_name: str = Field(min_length=1)
    decision: Literal["bind", "new"]
    node_id: str | None = None
    new_node: NewNodeSpec | None = None
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)

    @model_validator(mode="after")
    def _decision_matches_its_payload(self) -> Self:
        if self.decision == "bind":
            if self.node_id is None:
                raise ValueError(f"binding for {self.local_name!r} decided 'bind' without a node_id")
            if self.new_node is not None:
                raise ValueError(f"binding for {self.local_name!r} decided 'bind' and also proposed a new_node")
        else:
            if self.new_node is None:
                raise ValueError(f"binding for {self.local_name!r} decided 'new' without a new_node")
            if self.node_id is not None:
                raise ValueError(f"binding for {self.local_name!r} decided 'new' and also named a node_id")
        return self


class ReconcileResponse(_CavemanRecord):
    """Stage 2's whole answer: one binding per distinct surface name in the batch.

    Two rules are enforced here because both are response-internal, and both are
    the mechanism rather than hygiene:

    * **No duplicate ``local_name``.** One name cannot have two adjudications.
    * **Same ``new_node.name`` implies same ``type`` and ``gloss``.** Grouping by
      case-folded name is HOW within-batch reconciliation works -- each group
      becomes exactly one ``create_node``, so two names converging on one
      concept is a first-class outcome. A group that disagrees on type or gloss
      is a model saying "same concept" and "different concept" at once, and the
      right answer is to reject rather than pick one.
    """

    bindings: tuple[BindingSpec, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _names_are_unique(self) -> Self:
        counts = Counter(binding.local_name for binding in self.bindings)
        repeated = sorted(name for name, count in counts.items() if count > 1)
        if repeated:
            raise ValueError(f"local_name adjudicated more than once: {', '.join(repeated)}")
        return self

    @model_validator(mode="after")
    def _new_node_groups_agree(self) -> Self:
        shapes: dict[str, tuple[str, str]] = {}
        for binding in self.bindings:
            proposed = binding.new_node
            if proposed is None:
                continue
            key = proposed.name.casefold()
            shape = (proposed.type, proposed.gloss)
            existing = shapes.setdefault(key, shape)
            if existing != shape:
                raise ValueError(
                    f"new_node {proposed.name!r} was proposed with two different shapes: {existing} and {shape}"
                )
        return self

    def local_names(self) -> tuple[str, ...]:
        """The adjudicated names, in response order, for the request/response set check."""
        return tuple(binding.local_name for binding in self.bindings)


# ----------------------------------------------- 3a DREAM-INCREMENTAL contract


class FactSpec(_CavemanRecord):
    """One fact as the dreamer SENDS it: a kind, a label, text, and its evidence.

    The wire twin of :class:`Fact`, and the reason there are two records rather
    than one: a ``Fact`` carries ``first_seen`` and ``last_seen``, which are the
    store's to set and not the model's to claim. Everything a model may decide is
    here; everything it may not is added by the stage.

    ``entry_ids`` is the half that makes this amendment D rather than a rename.
    The model names which ledger entries support each fact, so evidence is a
    per-fact statement it is accountable for instead of "every entry on the
    node", and **restating a fact the node already holds means carrying that
    fact's entry ids plus the new ones** -- which is reinforcement, and is checked
    by ``dream`` against the node rather than here.

    Whether the ids EXIST and whether they are bound to this node both need the
    ledger, so they are ``dream``'s rules. What is structural here is that a fact
    cannot arrive with no evidence at all.
    """

    kind: FactKind
    key: str | None = Field(default=None, min_length=1, max_length=40)
    text: str = Field(min_length=1, max_length=MAX_FACT_TEXT)
    entry_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        """The two rules :class:`Fact` enforces that a sender can also be held to."""
        _distinct(self.entry_ids, what=f"fact {self.text!r}")
        if self.key is not None and self.kind is not FactKind.ATTRIBUTE:
            raise ValueError(
                f"fact {self.text!r} is a {self.kind.value!r} and carries key={self.key!r} -- only an "
                f"{FactKind.ATTRIBUTE.value!r} fact has a label, every other kind renders under its kind word"
            )
        _one_line(self.text, what="fact text must be one line with no leading or trailing space")
        return self


class RelationSpec(_CavemanRecord):
    """One typed edge as the dreamer sends it, FROM the node being dreamed.

    The source is never named: a relation in a node answer runs out of that node,
    and letting the model name a source would let one node's dream rewrite
    another node's edges. ``target_id`` is a node id and never a name, because
    resolving a name is stage 2's job and a dream that resolved one could write a
    belief about a concept it was never shown.

    ``relation_id`` is the one field that changes the operation:

    * ``None`` -- a new belief, or a restatement of one the scope already holds.
      The store's identity is the triple ``(source, type, target)``, so a
      restatement **reinforces** the existing edge rather than duplicating it.
    * an id -- a rewrite of that specific edge's claim or ``until`` marker.
      ``dream`` checks that the edge exists, runs out of this node, and carries
      the same ``type`` and ``target_id``: re-typing or re-pointing an edge is
      not a rewrite, and is expressed by stating the new edge and retiring the
      old one.

    ``until`` is applied as given, ``None`` included, so clearing a marker -- "it
    was fixed, and then it regressed" -- is expressible rather than sticky. A
    dream answer always carries the ``claim`` too, and ``upsert_relation`` moves
    the two together: the caller that keeps a marker by saying nothing is
    ``reconcile``, which states no claim either.
    """

    relation_id: str | None = Field(default=None, min_length=1)
    type: str = Field(pattern=EDGE_TYPE_PATTERN)
    target_id: str = Field(min_length=1)
    claim: str = Field(min_length=1, max_length=MAX_CLAIM_TEXT)
    entry_ids: tuple[str, ...] = Field(min_length=1)
    until: str | None = Field(default=None, min_length=1, max_length=40)

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        _distinct(self.entry_ids, what=f"relation {self.type} to {self.target_id}")
        _one_line(self.claim, what="relation claim must be one line with no padding")
        return self


def _duplicate_fact_texts(facts: Sequence[FactSpec]) -> tuple[str, ...]:
    """Fact texts sent more than once in one answer, sorted.

    Two facts with identical text are a contradiction the answer can be checked
    for on its own: they cannot both be reinforced, they would collapse to one
    record, and which ``kind`` and evidence survived would depend on iteration
    order. So it is a rejection here rather than a silent last-write-wins.
    """
    counts = Counter(fact.text for fact in facts)
    return tuple(sorted(text for text, count in counts.items() if count > 1))


def _duplicate_relation_triples(relations: Sequence[RelationSpec]) -> tuple[str, ...]:
    """``TYPE target`` pairs sent more than once in one answer, sorted.

    The store's identity for an edge is its triple, and the source is fixed by
    the node being dreamed -- so two specs sharing ``(type, target_id)`` are two
    writes to one edge with no defined order.
    """
    counts = Counter((relation.type, relation.target_id) for relation in relations)
    return tuple(sorted(f"{type_} {target}" for (type_, target), count in counts.items() if count > 1))


class DreamNodeResponse(_CavemanRecord):
    """One node's COMPLETE new fact set, its new edges, and what it retires.

    **The facts are complete and the relations are not**, and the asymmetry is
    what ``retired_relation_ids`` exists for. A fact lives on the node, so a
    fact the answer omits is a fact the node no longer holds. An edge is shared
    between two nodes and a node dream only ever sees one end of it, so a
    complete edge set would make every dream of one node an implicit verdict on
    every belief its neighbours hold about it. Relations are therefore stated
    (created, reinforced or rewritten) and retired explicitly.

    Named fields replace the plain ``lines`` array D0 carried forward (#251
    amendment D, package D-C). The ``<= L`` cap, the per-fact paragraph guard,
    the evidence-is-bound rule, the reinforcement union and the
    ``superseded``-needs-a-superseded-entry rule all need the motive, the node or
    the ledger, so ``dream`` enforces them. What is structural here is that a
    node cannot come back with zero facts, cannot come back with a type that is
    not a type, and cannot contradict itself.
    """

    type: str = Field(pattern=NODE_TYPE_PATTERN)
    facts: tuple[FactSpec, ...] = Field(min_length=1)
    relations: tuple[RelationSpec, ...] = ()
    retired_relation_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)

    @model_validator(mode="after")
    def _internally_consistent(self) -> Self:
        repeated_facts = _duplicate_fact_texts(self.facts)
        if repeated_facts:
            raise ValueError(f"fact sent more than once: {'; '.join(repeated_facts)}")
        repeated_edges = _duplicate_relation_triples(self.relations)
        if repeated_edges:
            raise ValueError(f"relation sent more than once: {'; '.join(repeated_edges)}")
        _no_repeats(self.retired_relation_ids, what="retired_relation_ids")
        # Rewriting an edge and retiring it in one answer is two verdicts on one
        # belief, and applying both would make the outcome depend on which is
        # applied second.
        rewritten = {relation.relation_id for relation in self.relations if relation.relation_id is not None}
        contradicted = sorted(rewritten & set(self.retired_relation_ids))
        if contradicted:
            raise ValueError(f"relation both rewritten and retired: {', '.join(contradicted)}")
        return self


# --------------------------------------------------- 3b DREAM-GLOBAL contract


class SplitSpec(_CavemanRecord):
    """One part of a split: a new node plus the parent state that moves to it.

    Doubles as the argument type of ``GraphStore.split_node``, deliberately --
    the wire shape and the store operation are the same fields, so there is no
    translation layer to drift. ``entry_ids`` must partition the parent's
    entries exactly, which ``dream`` checks against the ledger.

    ``relation_ids`` names the parent's edges this part keeps. Defaulted empty,
    and an edge no part names goes with the parent: a split deletes the parent,
    so an edge nobody claimed has no endpoint left to hang off. That is the same
    rule the old link-weight store applied to every edge of a split parent, with
    the difference that a part can now keep one.

    A part's ``facts`` may only be evidenced by entries in that part's own
    ``entry_ids`` -- ``dream``'s rule, since it needs the ledger -- which is what
    stops a split writing a part a fact its share of the evidence cannot support.
    """

    name: str = Field(min_length=1, max_length=MAX_NODE_NAME)
    type: str = Field(pattern=NODE_TYPE_PATTERN)
    facts: tuple[FactSpec, ...] = Field(min_length=1)
    entry_ids: tuple[str, ...] = Field(min_length=1)
    relation_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _facts_are_distinct(self) -> Self:
        repeated = _duplicate_fact_texts(self.facts)
        if repeated:
            raise ValueError(f"split part {self.name!r} sends a fact more than once: {'; '.join(repeated)}")
        return self


class MergeOp(_CavemanRecord):
    """Two or more nodes are one concept. The survivor's facts are given, not derived.

    No ``relations`` field, deliberately: ``GraphStore.merge_nodes`` re-points
    every absorbed node's edges onto the survivor by itself, unioning what
    collides and dropping the self-loops the collapse creates. A merge that also
    restated its edges would be writing the same beliefs twice, by two rules that
    could disagree.

    The survivor's ``facts`` may be evidenced by any entry bound to any of the
    merged nodes -- they all become the survivor's on the re-key -- which is
    ``dream``'s rule, since it needs the ledger.
    """

    op: Literal["merge"] = "merge"
    nodes: tuple[str, ...] = Field(min_length=2)
    survivor_name: str = Field(min_length=1, max_length=MAX_NODE_NAME)
    survivor_type: str = Field(pattern=NODE_TYPE_PATTERN)
    facts: tuple[FactSpec, ...] = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)

    @model_validator(mode="after")
    def _nodes_and_facts_are_distinct(self) -> Self:
        if len(set(self.nodes)) != len(self.nodes):
            raise ValueError(f"merge names the same node twice: {list(self.nodes)}")
        repeated = _duplicate_fact_texts(self.facts)
        if repeated:
            raise ValueError(f"merge {list(self.nodes)} sends a fact more than once: {'; '.join(repeated)}")
        return self


class SplitOp(_CavemanRecord):
    """One node holds two unrelated topics. Allowed only with headroom under N."""

    op: Literal["split"] = "split"
    node: str = Field(min_length=1)
    into: tuple[SplitSpec, ...] = Field(min_length=2)
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)


class RetypeOp(_CavemanRecord):
    """The node is right and its type is wrong. The intended repair for type drift."""

    op: Literal["retype"] = "retype"
    node: str = Field(min_length=1)
    type: str = Field(pattern=NODE_TYPE_PATTERN)
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)


class RewriteOp(_CavemanRecord):
    """The node's facts duplicate a neighbour's. Replaces the whole fact set.

    The only global op that carries ``relations``, and for the same reason the
    node pass does: a rewrite is where the global view notices that two facts on
    two nodes are really one belief BETWEEN them, and the repair is to write the
    edge. Read exactly as :class:`DreamNodeResponse`'s -- stated from ``node``,
    complete in neither direction, retired by ``retired_relation_ids``.
    """

    op: Literal["rewrite"] = "rewrite"
    node: str = Field(min_length=1)
    facts: tuple[FactSpec, ...] = Field(min_length=1)
    relations: tuple[RelationSpec, ...] = ()
    retired_relation_ids: tuple[str, ...] = ()
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)

    @model_validator(mode="after")
    def _internally_consistent(self) -> Self:
        repeated = _duplicate_fact_texts(self.facts)
        if repeated:
            raise ValueError(f"rewrite {self.node} sends a fact more than once: {'; '.join(repeated)}")
        repeated_edges = _duplicate_relation_triples(self.relations)
        if repeated_edges:
            raise ValueError(f"rewrite {self.node} sends a relation more than once: {'; '.join(repeated_edges)}")
        _no_repeats(self.retired_relation_ids, what=f"rewrite {self.node} retired_relation_ids")
        rewritten = {relation.relation_id for relation in self.relations if relation.relation_id is not None}
        contradicted = sorted(rewritten & set(self.retired_relation_ids))
        if contradicted:
            raise ValueError(f"rewrite {self.node} both rewrites and retires: {', '.join(contradicted)}")
        return self


class RenameEdgeTypeOp(_CavemanRecord):
    """Fold one relation type into another. How the vocabulary is held at ``M``.

    The second bound the dreamer enforces (#251 amendment D). ``N`` stops the
    concepts growing without limit; this stops the *vocabulary of relations
    between them* doing the same, which is the failure an open edge-type space
    has -- every episode coining ``FRONTS``, ``PROXIES`` and ``ROUTES_TO`` for one
    relationship until a reader can no longer ask a question by type.

    It renames edges and never deletes one: ``GraphStore.rename_edge_type``
    re-labels every ``old`` edge, folding an edge into one the pair already holds
    under ``new`` rather than creating a duplicate. So the beliefs survive the
    compaction and only their label changes.

    Which types MUST be folded is arithmetic the prompt has already done -- the
    least-used ``len - M`` of them -- and ``new`` is the model's choice among the
    types that are staying. Both are ``dream``'s rules, since they need the
    scope.
    """

    op: Literal["rename_edge_type"] = "rename_edge_type"
    old: str = Field(pattern=EDGE_TYPE_PATTERN)
    new: str = Field(pattern=EDGE_TYPE_PATTERN)
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)

    @model_validator(mode="after")
    def _renames_to_something_else(self) -> Self:
        if self.old == self.new:
            raise ValueError(f"rename_edge_type {self.old!r} to itself changes nothing")
        return self


class RetireRelationOp(_CavemanRecord):
    """One belief between two concepts no longer holds. The edge leaves the graph.

    Retirement rather than deletion in name only: the edge goes, and the
    :class:`DreamEvent` the journal records carries what it asserted, so what was
    believed and when it stopped being believed are both still answerable from
    the ledger.

    Named by ``relation_id`` rather than by its triple because the triple is what
    a rename can change, and an op that identified its target by a mutable key
    could retire an edge a rename in the same pass had just re-labelled.
    """

    op: Literal["retire_relation"] = "retire_relation"
    relation_id: str = Field(min_length=1)
    reason: str = Field(min_length=1, max_length=MAX_REASON_TEXT)


DreamGlobalOp = Annotated[
    MergeOp | SplitOp | RetypeOp | RewriteOp | RenameEdgeTypeOp | RetireRelationOp,
    Field(discriminator="op"),
]
"""One global-pass operation, discriminated on ``op``.

A discriminated union rather than a shape guess: a model that says ``"op":
"merge"`` and then sends a split's fields gets one clear error naming the
variant, instead of six "does not match any member" reports.
"""

GlobalOp = MergeOp | SplitOp | RetypeOp | RewriteOp | RenameEdgeTypeOp | RetireRelationOp
"""The same six ops as a plain union, for annotating the code that applies them.

The ``Annotated`` alias above carries pydantic's discriminator and is what the
response field is typed with; this is what a function signature wants, and having
one name for it stops six-member unions being spelled out at every call site --
which is how the fifth and sixth members get forgotten at one of them.
"""


def _op_node_ids(op: GlobalOp) -> Iterable[str]:
    """Every node id an op claims. One helper so the set is defined once.

    The two vocabulary ops claim none: a rename acts on every edge of one type
    and a retirement on one edge, so neither is a verdict on a node, and neither
    conflicts with a merge or a rewrite happening in the same pass.
    """
    if isinstance(op, MergeOp):
        return op.nodes
    if isinstance(op, RenameEdgeTypeOp | RetireRelationOp):
        return ()
    return (op.node,)


class DreamGlobalResponse(_CavemanRecord):
    """The global pass's whole answer. Zero ops is a valid, common outcome.

    One node named by two ops is rejected here because it is response-internal
    and because the ops are applied as a batch: two ops on one node have no
    defined order, so "merge n-003 away" plus "rewrite n-003" is a request whose
    result depends on iteration order. Whether the ids EXIST, whether a split
    has headroom, and whether every forced-merge pair was delivered all need the
    scope, so ``dream`` checks those.
    """

    ops: tuple[DreamGlobalOp, ...] = ()

    @model_validator(mode="after")
    def _nothing_is_claimed_by_two_ops(self) -> Self:
        counts = Counter(node_id for op in self.ops for node_id in _op_node_ids(op))
        repeated = sorted(node_id for node_id, count in counts.items() if count > 1)
        if repeated:
            raise ValueError(f"node named by more than one op: {', '.join(repeated)}")

        # The same rule, for the two things a vocabulary op claims. Renaming one
        # type twice and retiring one edge twice are both batches with no defined
        # order, exactly as two ops on one node are.
        renamed = Counter(op.old for op in self.ops if isinstance(op, RenameEdgeTypeOp))
        twice = sorted(name for name, count in renamed.items() if count > 1)
        if twice:
            raise ValueError(f"edge type renamed by more than one op: {', '.join(twice)}")
        retired = Counter(op.relation_id for op in self.ops if isinstance(op, RetireRelationOp))
        again = sorted(name for name, count in retired.items() if count > 1)
        if again:
            raise ValueError(f"relation retired by more than one op: {', '.join(again)}")
        return self
