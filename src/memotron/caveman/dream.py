"""Stage 3: the dreamer. **The only writer of fact text, and the only enforcer of N and M.**

``replace_facts``, ``merge_nodes`` and ``split_node`` are called from this module
and nowhere else, which is why the invariant is a property of the file layout
rather than a convention anybody has to remember. ``extract`` only appends to the
ledger; ``reconcile`` only routes, marks dirty and records a provisional
relation; a node's first fact set and every fact set after it is written here.

Three entry points, and they are deliberately different shapes:

``dream_incremental(scope, motive)``
    One LLM call per dirty node. Per-node atomic: a node whose response is out of
    contract stays dirty with its facts unchanged, and the call raises. Nodes
    already applied in the same batch stay applied -- rolling them back would
    discard work that was in contract to punish a later node that was not.

``dream_global(scope, motive)``
    Exactly ONE LLM call. The forced-merge slate and the edge-type compaction
    mandate are both computed *before* the call and passed into the prompt, so
    there is no conditional second "forced" pass and no fallback. All-or-nothing:
    every op is validated against the scope and the ledger before any of them is
    applied, so a rejection leaves the graph byte-identical.

``erase_episode(episode_id, scope)``
    No LLM at all. Delete the episode's entries, delete the nodes that lost
    every entry, mark the rest dirty. The graph is regenerable from the ledger,
    so erasure is a ledger operation plus a re-dream.

.. rubric:: Two bounds, and the dreamer holds both (#251 amendment D)

``N`` bounds the CONCEPTS and ``M`` bounds the VOCABULARY OF RELATIONS between
them. Both are enforced here and both are arithmetic the prompt has already done
before the model is asked anything:

* ``pressure.merge_slate`` names which nodes are folded into which, and the
  prompt states it as a mandate;
* :func:`compaction_targets` names which edge types must be folded away, and the
  prompt states that as a mandate too. The model chooses only which surviving
  type each doomed one folds into -- a judgement about meaning, not about
  counting.

Bounded types, not bounded edges. How many edges a scope holds is how much
evidence it has, and an open edge-type space is the failure an unbounded
vocabulary has: every episode coins ``FRONTS``, ``PROXIES`` and ``ROUTES_TO``
for one relationship, and a reader can no longer ask a question by type.

.. rubric:: Which node survives a merge, and why

**The node whose name the answer keeps.** ``survivor_name`` must be the current
name -- or a known alias -- of one of the nodes being merged, and that node is
the record that survives: it keeps its id, its ``created_at``, its
``ledger_key`` and its ``read_count``, and the other nodes' names become its
aliases (``models.merged_aliases``).

So ``survivor_name`` is an *identifier* here, not a free label. The rule it
replaced survived the lowest node id and let the model name the result whatever
it liked, and the first live run showed what that costs (#251 amendment A): a
forced merge produced ``chart-models``, a name belonging to neither concept, and
a search for the chart, the models, session affinity or #246 -- all four of
which that node then held -- landed on a node named for none of them. A merge
that renames a live concept destroys the only string a searcher was going to
type.

A pair on the forced-merge slate is stronger still: ``pressure`` has already
decided which half survives, so the mandate names it and an answer that keeps
the doomed node's name instead is rejected. The one exception is two nodes that
are already aliases of each other -- they answer to the same surfaces, so which
record survives changes nothing a reader can observe.

.. rubric:: Evidence is per fact, and restating a fact reinforces it

Every fact and every relation the model sends carries its own ``entry_ids``, and
that is what makes evidence a statement the model is accountable for rather than
a guess this module makes on its behalf. Three rules follow, and all three are
enforced here because all three need the ledger:

* **an entry id must be bound to the node the fact is going on.** A fact
  evidenced by a claim about something else is unsupported, whatever it says.
* **restating a fact the node already holds must carry that fact's entry ids as
  well as the new ones.** That is *reinforcement*: the record keeps its
  ``first_seen``, gains an entry, and renders ``(x2)``. A restatement that
  dropped the old ids would silently turn a twice-attested belief back into a
  once-attested one, which is the opposite of what evidence is for.
* **a ``superseded`` fact needs a WITNESS that something replaced the claim**, and
  there are two of them (``Evidence.unwitnessed_history``): the ledger marks the
  entry superseded, or the entry supports a fact this node currently holds and
  this answer no longer keeps live. "This used to be true" is knowledge only when
  something replaced it; otherwise it is a live fact mislabelled as history, and
  a ``superseded`` fact is hidden from every read.

  The second witness is a DEVIATION from the amendment's stated validation, and
  it is here because the first is unreachable for the commonest case.
  ``ClaimSpec.supersedes_claim_index`` points at a SIBLING in one extract
  response, so the ledger cannot link a claim to one from a LATER EPISODE -- and
  a chat default that moved yesterday is exactly that. Four live runs answered
  ``superseded`` there, correctly, and were rejected; three prompt fixes aimed at
  talking the model out of it did not, because the motive's own rubric asks for a
  superseded fact too. The dreamer is the only party that can see across
  episodes, so it is the only party that can witness such a replacement, and its
  answer carrying the new value beside the old one is a stronger witness than a
  ledger field the extractor had no way to set.

.. rubric:: One addition the plan's own contracts require

**Both prompts list ledger entry ids beside the facts they evidence.** The
design document's input list for 3b does not mention them, but its split
contract requires ``entry_ids`` to partition ``ledger.for_node(node)`` exactly,
and every fact now cites the entries that evidence it -- neither of which a model
can do for a set it has not been shown. 3b lists them for the at-risk nodes
only, which is the same set whose full facts are shown; an op on a node whose ids
were not listed fails its own evidence check, so no extra rule is needed to
forbid it.

.. rubric:: What typed edges removed from this module (#251 amendment D)

Three rules and one whole repair pass are gone, and all four were consequences
of a belief between two concepts being a *line on one of them*:

* **relation targets no longer need projecting.** A relation is a
  :class:`~memotron.caveman.models.Relation` with node ids on both ends, so a
  merge re-points it (``graph.merge_nodes``) and a dangling target is
  unrepresentable rather than validated against a projected name set.
* **nothing rebuilds an edge after a write.** The old ``set_link_weights`` two-
  step after every merge and split is gone with the weight it wrote.
* **the orphan repair is gone with the orphan.** A merge used to leave an
  untouched third node pointing at an absorbed name, which this module found
  after the pass and marked dirty (``GlobalOutcome.orphaned_node_ids``). There
  is no such state now, so the field and the pass that computed it are deleted
  rather than left returning an empty tuple nobody can ever observe.

And one more went with the named-field contract: ``demoted``. A node answer is
its complete fact set, so a fact the answer leaves out is gone from the node --
and a second list restating which ones left was a cross-check on a complete
answer rather than a decision of its own.

.. rubric:: Every write goes through the store this dreamer was handed

Nothing here writes a :class:`~memotron.caveman.models.DreamEvent`. The
composition root wraps the graph in ``graph.JournaledGraph``, so each
``replace_facts``, ``merge_nodes``, ``split_node``, ``upsert_relation``,
``retire_relation``, ``rename_edge_type`` and ``delete_nodes`` call below becomes
one journal event with the content before and after it. A stage that also wrote
events would be a second author of the history, and two authors of one history is
a history that can disagree with itself.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from memotron.caveman.errors import CavemanError, OutOfContractResponse
from memotron.caveman.llm import CONTRACT_CLOSING_LINE, strict_json_call
from memotron.caveman.models import (
    DreamGlobalResponse,
    DreamNodeResponse,
    Fact,
    FactKind,
    FactSpec,
    GlobalOp,
    LedgerEntry,
    MergeOp,
    Node,
    NodeAlias,
    Receipt,
    ReceiptOp,
    Relation,
    RelationSpec,
    RenameEdgeTypeOp,
    RetireRelationOp,
    RetypeOp,
    RewriteOp,
    SplitOp,
    merged_aliases,
)
from memotron.caveman.motive import CavemanMotive, render_motive_block
from memotron.caveman.pressure import (
    MergePair,
    ValuedNode,
    embedding_similarity,
    merge_slate,
    node_value,
    pressure,
)
from memotron.caveman.receipts import canonical_digest, new_receipt_id
from memotron.caveman.render import (
    BREVITY_RULE,
    EXAMPLE_FACT_TEXTS,
    RELATION_TARGET_RULE,
    format_prompt_block,
    render_fact,
    render_relation,
    sort_facts,
    validate_fact_text,
)
from memotron.caveman.seams import Clock, GraphStore, LedgerStore, ReceiptSink
from memotron.embedding import EmbeddingTransport
from memotron.synthesis import SynthesisTransport

NODE_SOURCE = "DREAM_NODE"
"""Stage name on a 3a rejection. Reads correctly inside ``parse_first_json_object``'s message."""

GLOBAL_SOURCE = "DREAM_GLOBAL"
"""Stage name on a 3b rejection."""

AT_RISK_NODES = 20
"""How many lowest-value nodes the global prompt shows in full.

The design document's number. Everything else is one inventory line, so the
prompt spends its detail budget where a reorganisation decision is actually
made -- the bottom of the value order, plus whatever the slate names.
"""

EVIDENCE_OPEN = "  ["
"""How a rendered belief in a prompt introduces the entry ids that evidence it.

``rule: real runs go through the gateway  [e-01, e-04]`` -- two spaces, then the
ids in brackets, which is the shape ``explain`` uses for the same thing. The
model has to cite these ids back, so it has to be shown them beside the belief
they belong to rather than only in a list at the bottom of the prompt.
"""

METADATA_ONLY_BODY = re.compile(r"^(conf|confidence|turns?|entry|entries|ts|date)\b.*[:=]", re.IGNORECASE)
"""Fact text that states provenance instead of a fact. **Exactly this rule.**

Every claim in a dream prompt is shown with its bookkeeping --
``[e-07] attribute descriptive conf=0.97 turns=1,3``, plus the node header's
``as of`` date -- and the live run's #246 node came back holding ``conf: 0.97;
turns: 6``. That is a fact about the RECORD, not about the thing, and it costs a
slot of an eight-fact budget to say nothing a reader can act on.

The rule is deliberately deterministic rather than a judgement: text that begins
with one of ``conf confidence turn turns entry entries ts date`` on a word
boundary and goes on to contain ``:`` or ``=``. Anchored at the start, because
that is what makes it "the fact is provenance" rather than "the fact mentions a
date".

Known and accepted: it also rejects a genuine fact phrased with one of those
words first and a separator later -- ``entry point: the /v1 handler``. The
repair is to phrase it the other way round, and a rejection says exactly which
fact and why, which is a cheaper failure than provenance quietly eating a slot.
"""

DREAM_NODE_SYSTEM = f"""You are the dreamer of a bounded concept graph. You hold ONE node and the claims
that have arrived about it since it was last dreamed.

Emit the node's COMPLETE new fact set. Not a diff, not an addition. Every fact
you emit is what the node holds afterwards; every fact you leave out is gone
from the node and stays only in the ledger.

Emit relations SEPARATELY, and they are not complete. A relation is a belief
between this node and another one, so a node's dream sees only one end of it:
state the ones you are creating, reinforcing or rewriting, and name any that no
longer hold in `retired_relation_ids`. A relation you do not mention is left
exactly as it is.

What you are judged on:
  - Every fact must be supported either by one of the new claims below or by a
    fact the node already holds. Do not introduce a fact from your own
    knowledge; you are compressing a record, not answering a question.
  - EVERY FACT AND EVERY RELATION NAMES ITS EVIDENCE. `entry_ids` is the ledger
    entry ids that support it, and every id must be one of the ids listed for
    this node below.
  - RESTATING A FACT THE NODE ALREADY HOLDS REINFORCES IT. Send the same text
    again, carrying that fact's existing entry ids AND the new one. The record
    then says two claims assert it instead of one. Dropping the old ids to send
    only the new one throws that away.
  - A claim that replaces an earlier one REWRITES the fact: keep the new value as
    the live fact and DROP the old one. Dropping it loses nothing -- the old claim
    stays in the ledger forever and the deep read shows it, dated.
  - `superseded` is NOT how you record that. It is legal only for an entry the
    evidence list below marks as superseded, which means the ledger itself links
    the two claims. When that list says none is, no fact in your answer may be
    `superseded`, however plainly a later claim replaced an earlier one: drop the
    old value and let the record hold it.
  - METADATA IS NEVER A FACT. Every claim below is shown with its provenance --
    the entry id in brackets, `conf=`, `turns=`, `supersedes=`, and the dates
    around it. That is bookkeeping about the RECORD, not a fact about the thing
    this node is. A fact whose text is that bookkeeping (`conf: 0.97;
    turns: 6`) is rejected outright. Say what the claim MEANS, or leave it out.
  - {BREVITY_RULE} A fact too big for one
    record is two facts, not one long one. Nothing here asks you to count
    characters or words -- write the shortest fact that still states it,
    and never drop an identifier to make a fact shorter.
  - You may propose a better `type` for the node.

You are not asked to be exhaustive. You are asked to decide what is worth a slot."""

DREAM_GLOBAL_SYSTEM = f"""You are the dreamer of a bounded concept graph. You hold the whole scope's node
inventory and your job is to reorganise it.

Six operations exist and nothing else:
  merge    two or more nodes are one concept. `nodes` MUST name AT LEAST TWO
           ids -- the node that goes and the node that lives on. One id is not a
           merge and is rejected outright. THE SURVIVOR KEEPS ITS OWN NAME:
           `survivor_name` must be the current name of one of the nodes you are
           merging, and that node is the one that lives on. A new name, or a
           compound of two of the old ones, is rejected outright -- it would
           destroy the name a reader was going to search for. You choose which
           node survives, its type, and its complete fact set. One merge may
           absorb more than two nodes. The relations of the nodes you absorb
           move onto the survivor by themselves; do not restate them.
  split    one node holds two unrelated topics. You name the parts AND you
           partition the node's ledger entries across them -- every entry
           exactly once, none dropped, none in two parts. A part's facts may
           cite only the entries that part takes. Allowed only when there is
           headroom under the node budget.
  retype   the node is right and its type is wrong.
  rewrite  the node's facts duplicate a neighbour's, or say less than they
           could. This is also where two facts on two nodes turn out to be one
           belief BETWEEN them: write the relation and drop the facts.
  rename_edge_type
           two relation types mean the same thing, or the vocabulary is over
           budget. Every edge of type `old` is re-labelled `new`. `new` must be
           a type this scope already has and is keeping. No belief is lost --
           only the label changes.
  retire_relation
           one belief between two concepts no longer holds. Name it by its
           `relation_id`.

Zero operations is a valid and common answer. Do not invent work to look busy.

No node may appear in two operations, no edge type may be renamed twice, and no
relation may be retired twice.

EVERY FACT AND EVERY RELATION NAMES ITS EVIDENCE in `entry_ids`, drawn from the
ledger entry ids listed for that node below. Restating a fact a node already
holds REINFORCES it: send the same text with that fact's existing entry ids plus
any new one.

METADATA IS NEVER A FACT. The nodes below are shown with their bookkeeping --
ledger entry ids, survival values, `as of` dates. That is provenance about the
RECORD, not a fact about the thing, and a fact whose text is provenance
(`conf: 0.97; turns: 6`, `entries: e-04, e-05`) is rejected outright in
every op.

When a FORCED MERGE SLATE is present it is MANDATORY. Each line of it reads
`MUST MERGE: <doomed> INTO <survivor> because <why>`, computed arithmetically
before you were asked: the doomed node is the lowest-value node in the scope, and
the survivor is the node the strongest evidence ties it to -- `linked xN` means N
ledger claims name both, `same turn xN` means they were said together N times
without a claim naming both, and `nearest by embedding` means neither, so only
their wording is alike. Write the survivor's facts to the evidence you are shown:
`nearest by embedding` is no warrant for a fact asserting a relationship between
them. You decide the survivor's type and facts. You do NOT decide which nodes go,
and you do NOT decide the surviving name -- the slate has named it, and
`survivor_name` must be that name. Every listed pair must appear inside some
merge's `nodes`, and an under-delivered forced merge is rejected outright rather
than treated as a suggestion.

The slate is also SUFFICIENT. It holds exactly as many pairs as the scope is
over budget and each pair removes exactly one node, so delivering the listed
pairs and nothing else brings the scope to its budget. You do not need a further
merge to satisfy the pressure, and a merge you add because the budget still
looks tight is a node collapsed for no reason.

MUST COMPACT EDGE TYPES works the same way. When it is present, the scope holds
more relation types than its budget allows, and the types listed there are the
least used ones. Each of them needs exactly one `rename_edge_type` op folding it
into a type that is staying. You choose which one it folds into; you do not
choose which types go.

{BREVITY_RULE} A fact too big for one record
is two facts, not one long one. Nothing here asks you to count characters or
words -- and an identifier is never what you drop to make a fact shorter."""


@dataclass(frozen=True)
class GlobalOutcome:
    """What one global pass did. ``count_after <= motive.max_nodes`` always holds.

    *pressure*, *slate* and *edge_type_pressure* are carried alongside the counts
    because a forced decision is only auditable if the mandate that produced it
    is visible next to the result -- "two nodes went" is not reviewable, "these
    two pairs and this type were mandatory, and here is the count before and
    after" is.
    """

    count_before: int
    count_after: int
    ops_applied: int
    pressure: int
    slate: tuple[MergePair, ...]
    edge_type_pressure: tuple[str, ...]


@dataclass(frozen=True)
class ErasureOutcome:
    """What one erasure did to the graph.

    Split in two because the two halves have different futures: a node in
    *deleted_node_ids* is gone and will not be re-dreamed (there is nothing left
    to dream it from), while one in *dirty_node_ids* keeps its remaining entries
    and is re-dreamed by the next incremental pass.
    """

    episode_id: str
    deleted_entry_count: int
    deleted_node_ids: tuple[str, ...]
    dirty_node_ids: tuple[str, ...]


@dataclass(frozen=True)
class Evidence:
    """What one batch of facts and relations may cite, and what it may reinforce.

    One record rather than three keyword arguments threaded through six call
    sites, because every op validates against a *different* set: a node dream
    against the node's own entries, a merge against the union of the entries of
    everything it absorbs, a split part against the slice of entries that part
    takes. Getting one of those wrong is how a fact ends up evidenced by a claim
    about something else, so the set is computed once, named, and handed over.

    ``held`` maps fact TEXT to the fact currently holding it. Text is the
    identity a restatement is recognised by -- it is the only handle the model and
    the store share -- and the match is what carries ``first_seen`` across a
    reinforcement, so the record keeps its age instead of looking new again.
    """

    bound: frozenset[str]
    superseded: frozenset[str]
    held: Mapping[str, Fact]

    @classmethod
    def of(cls, entries: Sequence[LedgerEntry], *, facts: Sequence[Fact] = ()) -> Evidence:
        """The evidence *entries* offer, and the *facts* a restatement may reinforce.

        ``superseded`` is read off the entries themselves: an id appears there
        when some entry in this set names it in ``supersedes``. So "is this claim
        superseded" is answered by the ledger rather than by the model's say-so,
        which is what makes a ``superseded`` fact provable rather than assertable.
        """
        return cls(
            bound=frozenset(entry.entry_id for entry in entries),
            superseded=frozenset(entry.supersedes for entry in entries if entry.supersedes is not None),
            held={fact.text: fact for fact in facts},
        )

    @property
    def usable_fact_kinds(self) -> tuple[FactKind, ...]:
        """The kinds a fact in THIS answer may carry, in the enum's own order.

        Derived from the same record the validation reads, which is the whole
        point: the prompt cannot offer a kind the answer would be rejected for.
        ``SUPERSEDED`` is the only conditional one, and it is available when
        either witness of a replacement is reachable -- the ledger marks an entry
        superseded, or the node already holds facts this answer could retire into
        history. See :meth:`unwitnessed_history`.

        The first dream of a node that holds nothing, out of an episode with no
        correction in it, has nothing that could be history -- so the kind is
        withheld rather than offered and then refused.
        """
        if self.superseded or self.held:
            return tuple(FactKind)
        return tuple(kind for kind in FactKind if kind is not FactKind.SUPERSEDED)

    def unwitnessed_history(self, entry_ids: Sequence[str], *, kept_live: frozenset[str]) -> tuple[str, ...]:
        """Cited ids with nothing showing that something REPLACED them, sorted.

        A ``superseded`` fact is history, and history needs a witness -- otherwise
        it is a live fact mislabelled, and a read would never show it again.
        There are two witnesses, and an id needs either:

        * **the ledger links the replacement.** Some entry on this node names it
          in ``supersedes``. That is the within-episode correction -- turn 9
          restating turn 7's tool count -- and it is the only witness the design
          originally named.
        * **this answer retires it.** The id already supports one of the node's
          CURRENT facts, and no fact this answer keeps live cites it. The claim
          held a live fact here and this answer has stopped treating it as one,
          which IS "stated and then replaced".

        The second witness exists because the first is unreachable for the
        commonest case. ``ClaimSpec.supersedes_claim_index`` points at a SIBLING
        in one extract response, so the ledger cannot link a claim to one from a
        LATER EPISODE -- and a chat default that moved yesterday is exactly that.
        Three live runs answered ``superseded`` there, correctly, and were
        rejected; a fourth was rejected after the prompt stopped offering the
        kind, because the motive's own rubric asks for it too.

        The dreamer is the only party that can see across episodes, so it is the
        only party that can witness such a replacement -- and its answer carrying
        the new value beside the old one is a stronger witness than a ledger
        field the extractor had no way to set.
        """
        witnessed = set(self.superseded)
        for fact in self.held.values():
            witnessed.update(entry_id for entry_id in fact.entry_ids if entry_id not in kept_live)
        return tuple(sorted(set(entry_ids) - witnessed))

    def render(self) -> str:
        """The citable ids, and whether ANY of them is superseded -- stated either way.

        The negative is stated, and that is the fix for a live rejection rather
        than a nicety. With nothing marked this block used to print the ids and
        fall silent, leaving the model to infer "so none of them is superseded"
        from an absent sentence -- and a run where a later episode plainly
        replaced an attribute produced a ``superseded`` fact the validation then
        refused. A prompt that omits a rule is a prompt that has not stated it.
        """
        citable = ", ".join(sorted(self.bound)) or "(none)"
        if not self.superseded:
            return (
                f"  {citable}\n"
                f"  NONE of these is superseded by a later entry, so no fact in this answer may be "
                f"kind {FactKind.SUPERSEDED.value!r}."
            )
        marked = ", ".join(sorted(self.superseded))
        return f"  {citable}\n  superseded by a later entry, so a superseded fact may cite it: {marked}"


def compaction_targets(edge_types: Mapping[str, int], *, max_edge_types: int) -> tuple[str, ...]:
    """The least-used relation types that must be folded away to hold ``M``.

    ``len(edge_types) - M`` of them, least used first, ties broken by name so the
    mandate is the same on every run over the same scope. Empty when the scope is
    within budget, which is the common case and is what makes the prompt's ``MUST
    COMPACT EDGE TYPES`` block absent rather than present and empty.

    Least-used rather than model-chosen for the same reason the merge slate is
    arithmetic: which beliefs lose their own label is consequential, and a type
    carrying two edges is the one whose loss costs a reader least. What the model
    still chooses is the surviving type each doomed one folds INTO, which is a
    judgement about meaning and not about counting.

    There is always at least one type left to fold into, because ``M >= 1`` and
    the doomed set is the overage.

    Public because the prompt states it, the validation enforces it and the demo
    prints it: three readers of one rule, so it is one function rather than three
    sorts that could disagree.
    """
    if max_edge_types < 1:
        raise ValueError(f"max_edge_types must be at least 1, got {max_edge_types}")
    overage = len(edge_types) - max_edge_types
    if overage <= 0:
        return ()
    ranked = sorted(edge_types.items(), key=lambda item: (item[1], item[0]))
    return tuple(name for name, _ in ranked[:overage])


def _states_only_provenance(text: str) -> bool:
    """Whether this fact says something about the RECORD instead of about the thing.

    :data:`METADATA_ONLY_BODY` applied to the fact's own text. There is no target
    prefix to split off any more -- a relation is an edge, not a fact -- so the
    rule reads exactly the string the model sent.
    """
    return METADATA_ONLY_BODY.match(text) is not None


def _provenance_errors(texts: Sequence[str], *, what: str) -> tuple[str, ...]:
    """One error per fact that states provenance. Shared by 3a and 3b, one wording."""
    return tuple(
        f"{what}: {text!r} states provenance, not a fact -- confidence, turn numbers, "
        f"entry ids and dates are shown beside a claim as bookkeeping and are never a fact"
        for text in texts
        if _states_only_provenance(text)
    )


def _fact_text_errors(texts: Sequence[str], *, motive: CavemanMotive, what: str) -> tuple[str, ...]:
    """The count cap and the per-fact text rules. Empty means the text is usable.

    Count first, then each text: reporting the third fact's wording when the
    model returned twelve facts for an eight-fact node buries the real problem.
    One function so 3a and 3b apply the same rules with the same wording -- the
    old pair of ``parse_lines`` call sites had already drifted into two error
    shapes.
    """
    if len(texts) > motive.max_facts_per_node:
        return (f"{what}: {len(texts)} facts exceeds the {motive.max_facts_per_node}-fact ceiling for one node",)
    errors: list[str] = []
    for text in texts:
        try:
            validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)
        except CavemanError as exc:
            errors.append(f"{what}: {exc}")
    return tuple(errors)


def _uncitable(entry_ids: Sequence[str], evidence: Evidence) -> tuple[str, ...]:
    """The cited ids this batch may not cite, sorted. Empty when every id is bound."""
    return tuple(sorted(set(entry_ids) - evidence.bound))


def _evidence_errors(specs: Sequence[FactSpec], *, evidence: Evidence, what: str) -> tuple[str, ...]:
    """The three rules a fact's ``entry_ids`` must satisfy against the ledger.

    Reported together rather than short-circuited: a model that cited an unbound
    id in one fact and dropped a reinforcement's ids in another made two
    mistakes, and seeing one of them is half a diagnosis.

    *kept_live* is every id the answer's NON-superseded facts cite, computed once
    over the whole answer because the history rule is about the answer as a whole:
    an id this answer still treats as live cannot also be history in it. See
    :meth:`Evidence.unwitnessed_history`.
    """
    kept_live = frozenset(
        entry_id for spec in specs if spec.kind is not FactKind.SUPERSEDED for entry_id in spec.entry_ids
    )
    errors: list[str] = []
    for spec in specs:
        uncitable = _uncitable(spec.entry_ids, evidence)
        if uncitable:
            errors.append(
                f"{what}: fact {spec.text!r} cites {', '.join(uncitable)}, which is not a ledger entry "
                f"this node may cite"
            )
        if spec.kind is FactKind.SUPERSEDED:
            live = evidence.unwitnessed_history(spec.entry_ids, kept_live=kept_live)
            if live:
                errors.append(
                    f"{what}: fact {spec.text!r} is {FactKind.SUPERSEDED.value!r} but cites {', '.join(live)}, "
                    f"which nothing shows was replaced -- the ledger supersedes none of them and none of them "
                    f"supports a fact this node holds and this answer drops. A fact becomes history when "
                    f"something replaced it, never on its own say-so"
                )
        restated = evidence.held.get(spec.text)
        if restated is not None:
            dropped = tuple(sorted(set(restated.entry_ids) - set(spec.entry_ids)))
            if dropped:
                errors.append(
                    f"{what}: fact {spec.text!r} restates a fact this node already holds and must carry its "
                    f"evidence too, but dropped {', '.join(dropped)} -- a restatement reinforces a belief "
                    f"rather than replacing what supported it"
                )
    return tuple(errors)


def _fact_errors(specs: Sequence[FactSpec], *, motive: CavemanMotive, evidence: Evidence, what: str) -> tuple[str, ...]:
    """Every rule a fact set must pass: shape, then provenance, then evidence.

    Short-circuits on the shape rules, because text that is not usable as a fact
    cannot meaningfully also be judged for what it states or what supports it.
    """
    texts = [spec.text for spec in specs]
    shape = _fact_text_errors(texts, motive=motive, what=what)
    if shape:
        return shape
    provenance = _provenance_errors(texts, what=what)
    if provenance:
        return provenance
    return _evidence_errors(specs, evidence=evidence, what=what)


def _relation_errors(
    specs: Sequence[RelationSpec],
    *,
    retired: Sequence[str],
    source_id: str,
    evidence: Evidence,
    known_node_ids: frozenset[str],
    own: Mapping[str, Relation],
    what: str,
) -> tuple[str, ...]:
    """Every rule an edge stated from *source_id* must pass. Empty means accept.

    *own* is the edges already touching *source_id*, by id -- what a
    ``relation_id`` resolves against and what a retirement may name.
    *known_node_ids* is the scope's node ids, so a target outside the scope is a
    rejection rather than a store exception raised halfway through applying a
    pass that was supposed to be all-or-nothing.

    A ``relation_id`` is a rewrite of one specific edge, so the type and target
    must match the edge it names: re-typing or re-pointing is a new belief plus a
    retirement, and letting a "rewrite" change the triple would leave the old
    edge behind under its old type with nothing naming it.
    """
    errors: list[str] = []
    for spec in specs:
        if spec.target_id == source_id:
            errors.append(f"{what}: relation {spec.type} points {source_id} at itself")
        elif spec.target_id not in known_node_ids:
            errors.append(f"{what}: relation {spec.type} targets {spec.target_id}, which is not a node in this scope")
        uncitable = _uncitable(spec.entry_ids, evidence)
        if uncitable:
            errors.append(
                f"{what}: relation {spec.type} to {spec.target_id} cites {', '.join(uncitable)}, which is not a "
                f"ledger entry this node may cite"
            )
        if spec.relation_id is None:
            continue
        existing = own.get(spec.relation_id)
        if existing is None:
            errors.append(f"{what}: relation {spec.relation_id} is not an edge of {source_id}")
        elif existing.triple != (source_id, spec.type, spec.target_id):
            errors.append(
                f"{what}: relation {spec.relation_id} is {existing.type} to {existing.target_id} and cannot be "
                f"rewritten as {spec.type} to {spec.target_id} -- state the new edge and retire this one"
            )
    errors.extend(
        f"{what}: retired relation {relation_id} is not an edge of {source_id}"
        for relation_id in retired
        if relation_id not in own
    )
    return tuple(errors)


def _facts_from(specs: Sequence[FactSpec], *, evidence: Evidence, now: datetime) -> tuple[Fact, ...]:
    """The answered specs as :class:`~memotron.caveman.models.Fact` records.

    A spec whose text restates a fact the node already holds keeps that fact's
    ``first_seen`` -- the belief is the same belief, and its age is part of what a
    reader judges it by -- while ``last_seen`` always moves to *now*. That, plus
    the union of entry ids validation already required, IS the reinforcement:
    nothing else in the system records how well attested a fact is.

    Returned in :func:`~memotron.caveman.render.sort_facts` order, so the
    stored order is the rendered order and the embedding covers the same text a
    reader sees.
    """
    built: list[Fact] = []
    for spec in specs:
        restated = evidence.held.get(spec.text)
        built.append(
            Fact(
                kind=spec.kind,
                key=spec.key,
                text=spec.text,
                entry_ids=spec.entry_ids,
                first_seen=restated.first_seen if restated is not None else now,
                last_seen=now,
            )
        )
    return sort_facts(built)


def _embedding_text(*, name: str, aliases: Sequence[str], type: str, facts: Sequence[Fact]) -> str:
    """The text a node's embedding is computed over: name, aliases, type, fact text.

    One function so the incremental pass, a merge and a rewrite all embed the
    same way. A node whose type changed must be re-embedded even though its
    facts did not, which is why the type is in here and not just alongside.

    The aliases are in the vector as well as in the exact-match index: a query
    that paraphrases a name the node was once called ("the LiteLLM proxy") is a
    similarity hit, not an exact one, and only the vector can catch it.
    """
    head = f"{name} {' '.join(aliases)} {type}" if aliases else f"{name} {type}"
    return "\n".join((head, *(fact.text for fact in facts)))


def _contract_example_facts(motive: CavemanMotive) -> tuple[str, ...]:
    """Worked-example fact texts that pass the very validation the answer will face.

    Drawn from :data:`~memotron.caveman.render.EXAMPLE_FACT_TEXTS` and
    filtered to what validates under this motive's ``T`` and fits its ``L``,
    because a prompt that demonstrates a violation of its own contract is how a
    model gets blamed for a defect in the prompt.

    The prompt no longer STATES ``T`` -- brevity is asked for and not measured
    (#251 amendment A) -- but the paragraph guard still rejects, so the examples
    still have to clear it.

    Raises:
        ValueError: when no example fits. A motive whose ``max_fact_tokens`` is
            too small to express a single fact cannot have its contract
            demonstrated, and that is a misconfigured motive rather than
            something to paper over with a shorter example.
    """
    fitting: list[str] = []
    for text in EXAMPLE_FACT_TEXTS:
        try:
            validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)
        except CavemanError:
            continue
        fitting.append(text)
    usable = tuple(fitting[: motive.max_facts_per_node])
    if not usable:
        raise ValueError(
            f"motive {motive.name!r} sets max_fact_tokens={motive.max_fact_tokens}, which is too "
            f"small for any example fact text -- the prompt cannot demonstrate its own contract"
        )
    return usable


_EXAMPLE_KINDS = (FactKind.RULE, FactKind.IS, FactKind.ATTRIBUTE)
"""The kinds the worked examples cycle through, in reading order.

``SUPERSEDED`` and ``UNSURE`` are deliberately absent from the examples: one is
only legal for an entry the ledger marks superseded, and demonstrating it in a
contract example would show a payload most prompts would reject.
"""


def _example_facts(motive: CavemanMotive, *, limit: int) -> list[dict[str, object]]:
    """Up to *limit* worked example facts, as the objects the answer sends.

    Returned as plain dicts and serialised by the one ``json.dumps`` that builds
    each contract block: the contract example is the one part of a prompt that
    has to be valid JSON, and hand-assembling it from f-strings is how a prompt
    ends up demonstrating a payload its own parser would reject.
    """
    return [
        {
            "kind": _EXAMPLE_KINDS[index % len(_EXAMPLE_KINDS)].value,
            "key": None,
            "text": text,
            "entry_ids": [f"e-{index + 1:02d}"],
        }
        for index, text in enumerate(_contract_example_facts(motive)[:limit])
    ]


def _render_entry(entry: LedgerEntry) -> str:
    """One ledger entry as the dreamer reads it: provenance line, then the claim."""
    head = (
        f"  [{entry.entry_id}] {entry.kind.value} {entry.claim_mode.value} "
        f"conf={entry.confidence:.2f} turns={','.join(str(turn) for turn in entry.turns)}"
    )
    if entry.supersedes is not None:
        head += f" supersedes={entry.supersedes}"
    body = f"      {entry.claim}"
    if entry.identifiers:
        return f"{head}\n{body}\n      identifiers: {', '.join(entry.identifiers)}"
    return f"{head}\n{body}"


def _render_entries(entries: Sequence[LedgerEntry]) -> str:
    if not entries:
        return "  (none -- nothing new has arrived since this node was last dreamed)"
    return "\n".join(_render_entry(entry) for entry in entries)


def _render_evidenced_fact(fact: Fact, *, indent: str = "  ") -> str:
    """A held fact as a reader sees it, plus the entry ids the answer must cite back.

    The ids are the load-bearing half. A restatement reinforces only if it
    carries the fact's existing evidence, and a model cannot carry ids it was
    never shown -- so rendering the fact without them would make the
    reinforcement rule unsatisfiable and every restatement a rejection.
    """
    return f"{indent}{render_fact(fact)}{EVIDENCE_OPEN}{', '.join(fact.entry_ids)}]"


def _render_current_facts(node: Node) -> str:
    """The node's facts, each with the entry ids that evidence it.

    No staleness marking any more, and that absence is the point: a fact points
    at nothing, so it cannot be left pointing at a node a merge has absorbed. The
    marking this replaced existed because a relation used to be a line naming a
    node by name (#251 amendment D).
    """
    if not node.facts:
        return "  (none -- this node has never been dreamed)"
    return "\n".join(_render_evidenced_fact(fact) for fact in sort_facts(node.facts))


def _render_relations(
    relations: Sequence[Relation],
    *,
    node_id: str,
    names: Mapping[str, str],
    indent: str = "  ",
) -> str:
    """This node's OUTGOING edges, each led by the id an answer rewrites or retires it by.

    ``[r-004] BLOCKED agent-memory [n-004] until #245: Host header refused
    [e-02]`` -- the relation id first, because that is what an op names it by;
    then the belief exactly as ``render.render_relation`` shapes it, so the model
    reads an edge in the same form an agent reading memory will; then its
    evidence, for the same reason a fact carries its own.

    Only the outgoing half gets ids. See :func:`_render_incoming`.
    """
    if not relations:
        return f"{indent}(none)"
    return "\n".join(
        f"{indent}[{relation.relation_id}] {render_relation(relation, node_id=node_id, names=names)}"
        f"{EVIDENCE_OPEN}{', '.join(relation.entry_ids)}]"
        for relation in relations
    )


def _render_incoming(
    relations: Sequence[Relation],
    *,
    node_id: str,
    names: Mapping[str, str],
    indent: str = "  ",
) -> str:
    """What OTHER concepts believe about this one. Context, and deliberately id-less.

    An answer's relations run out of the node being dreamed, so an incoming edge
    belongs to the concept at its other end and this dream cannot rewrite or
    retire it. Rendering it without its ``relation_id`` is the prompt saying so
    in the only way a model reliably reads: there is nothing here to name.

    Shown at all because it is real context -- "the chart deploys me" changes what
    is worth writing about this node -- and because hiding it would invite the
    dream to restate the same belief in the other direction.
    """
    if not relations:
        return f"{indent}(none)"
    return "\n".join(
        f"{indent}{render_relation(relation, node_id=node_id, names=names)}"
        f"{EVIDENCE_OPEN}{', '.join(relation.entry_ids)}]"
        for relation in relations
    )


def _render_scope_nodes(nodes: Sequence[Node], *, exclude: str) -> str:
    """Every OTHER concept in the scope as ``id | name | type``.

    The ids are why this is not a name list any more: a relation names its target
    by node id, so a model shown only names cannot write one, and a model that
    guessed an id would assert a belief about a concept it was never shown.
    """
    rows = [f"  {node.node_id} | {node.name} | {node.type}" for node in nodes if node.node_id != exclude]
    return "\n".join(rows) or "  (none -- this is the only concept in the scope)"


def _render_edge_types(edge_types: Mapping[str, int]) -> str:
    """``BLOCKED (3), DEPLOYS (2)`` -- the vocabulary with how much each carries.

    The counts are what make it a vocabulary rather than a list: a type carrying
    one edge is a coinage that has not caught on, and a model deciding whether to
    reuse a type or propose one needs to see which of them are load-bearing.
    """
    return ", ".join(f"{name} ({count})" for name, count in edge_types.items()) or "(none yet)"


def _node_prompt(
    node: Node,
    *,
    entries: Sequence[LedgerEntry],
    evidence: Evidence,
    relations: Sequence[Relation],
    scope_nodes: Sequence[Node],
    edge_types: Mapping[str, int],
    motive: CavemanMotive,
) -> str:
    """The 3a user prompt. The system prompt carries the intent; this carries the case."""
    names = {other.node_id: other.name for other in scope_nodes}
    # Split by direction, because only one of the two halves is this answer's to
    # change -- see ``CavemanDreamer._own_relations``.
    outgoing = [relation for relation in relations if relation.source_id == node.node_id]
    incoming = [relation for relation in relations if relation.source_id != node.node_id]
    contract = json.dumps(
        {
            "type": "service",
            "facts": _example_facts(motive, limit=3),
            "relations": [
                {
                    "relation_id": None,
                    "type": "BLOCKED",
                    "target_id": "n-004",
                    "claim": "the Host header rewrite was refused",
                    "entry_ids": ["e-02"],
                    "until": "#245",
                }
            ],
            "retired_relation_ids": [],
            "reason": "supersession applied; the Host detail moved onto the edge it is about",
        }
    )
    return (
        # The kinds THIS answer may use, from the same field the validation reads.
        # Not the whole enum: three live runs answered with a kind the prompt had
        # listed and then prohibited in prose. See ``render.format_prompt_block``.
        f"{format_prompt_block(max_facts=motive.max_facts_per_node, kinds=evidence.usable_fact_kinds)}\n"
        f"{render_motive_block(motive, stage='dream')}\n"
        f"NODE: {node.name} ({node.type}) [{node.node_id}]\n"
        f"\nCURRENT FACTS ({len(node.facts)} of {motive.max_facts_per_node}), with the entries that evidence them\n"
        f"{_render_current_facts(node)}\n"
        f"\nTHIS NODE'S OWN RELATIONS ({len(outgoing)}) -- edges running OUT of it, each led by\n"
        "the id an answer rewrites or retires it by\n"
        f"{_render_relations(outgoing, node_id=node.node_id, names=names)}\n"
        f"\nWHAT OTHER CONCEPTS BELIEVE ABOUT THIS ONE ({len(incoming)}) -- context only. These\n"
        "edges belong to the node at the other end, so they carry no id here and your\n"
        "answer cannot rewrite or retire one. Do not restate one in the other direction.\n"
        f"{_render_incoming(incoming, node_id=node.node_id, names=names)}\n"
        f"\nEDGE TYPES IN THIS SCOPE: {_render_edge_types(edge_types)}\n"
        f"\nOTHER CONCEPTS IN THIS SCOPE -- id, name, type. A fact belongs to THIS node\n"
        "only. Something true of one of these belongs on that node; something true\n"
        f"BETWEEN it and this node is a relation. {RELATION_TARGET_RULE}\n"
        f"{_render_scope_nodes(scope_nodes, exclude=node.node_id)}\n"
        f"\nNEW CLAIMS SINCE THIS NODE WAS LAST DREAMED ({len(entries)})\n"
        f"{_render_entries(entries)}\n"
        f"\nLEDGER ENTRY IDS THIS NODE MAY CITE AS EVIDENCE\n"
        f"{evidence.render()}\n"
        f"\nANSWER CONTRACT -- one object, these five keys, no others\n"
        f"{contract}\n"
        f"\n{CONTRACT_CLOSING_LINE}\n"
    )


def _render_inventory(nodes: Sequence[Node]) -> str:
    rows: list[str] = []
    for node in nodes:
        ordered = sort_facts(node.facts)
        first = render_fact(ordered[0]) if ordered else "(no facts yet -- awaiting its first dream)"
        rows.append(f"  {node.node_id} | {node.name} | {node.type} | {first}")
    return "\n".join(rows)


def _render_at_risk(
    valued: Sequence[ValuedNode],
    entry_ids: Mapping[str, Sequence[str]],
    relations: Mapping[str, Sequence[Relation]],
    names: Mapping[str, str],
) -> str:
    """The nodes a reorganisation is actually decided over, in full.

    Facts with their evidence, edges with their ids, and the node's whole citable
    entry list -- which is what a merge's facts, a rewrite's relations, a
    retirement and a split's partition each need in order to be expressible at
    all.
    """
    blocks: list[str] = []
    for item in valued:
        node = item.node
        facts = "\n".join(_render_evidenced_fact(fact, indent="      ") for fact in sort_facts(node.facts))
        edges = _render_relations(relations.get(node.node_id, ()), node_id=node.node_id, names=names, indent="      ")
        ids = ", ".join(entry_ids.get(node.node_id, ())) or "(none)"
        blocks.append(
            f"  {node.node_id} | {node.name} | {node.type} | value={item.value:.3f}\n"
            f"{facts or '      (no facts yet)'}\n"
            f"{edges}\n"
            f"      ledger entry ids: {ids}"
        )
    return "\n".join(blocks)


def merge_reason(pair: MergePair) -> str:
    """Why this peer and not another, in the three words the ranking actually used.

    ``linked xN`` / ``same turn xN`` / ``nearest by embedding``, read off the
    numbers :class:`~memotron.caveman.pressure.MergePair` already carries
    rather than from a flag the slate would have to set: the pair records all
    three criteria, and the deciding one is by definition the first that is
    non-zero in ``pressure``'s own order.

    The model is told because the three mandates are not equally strong. "Linked
    x4" says the ledger holds four claims about both of these; "nearest by
    embedding" says nothing survived except that they read alike -- and a
    survivor whose facts were written as if the second case were the first is how
    a merge states a relationship the evidence never had.

    Public because the mandate the model is told and the mandate a reader is
    shown must be the same sentence: ``examples/caveman_demo.py`` prints this
    next to every forced pair, and a demo that re-derived the phrase could
    print a reason the prompt never gave.
    """
    if pair.link_weight:
        return f"linked x{pair.link_weight}"
    if pair.turn_cooccurrence:
        return f"same turn x{pair.turn_cooccurrence}"
    return "nearest by embedding"


def _render_slate(slate: Sequence[MergePair], names: Mapping[str, str]) -> str:
    """The mandate, directional, with both names and both ids spelled out.

    Rendered as bare ids, a pair left the model to cross-reference the inventory
    for the names involved and to work out which of them stops existing. It did
    not: a live forced pass wrote its survivor a relation pointing at ``chart``,
    one of the names that very merge was consuming.

    The direction is the load-bearing half. ``pressure`` decides which node
    survives, and the survivor keeps its own name, so exactly ONE name
    disappears per pair -- the doomed node's, which becomes an alias of the
    survivor. Telling the model "both stop existing and your survivor_name
    replaces them" was how ``chart-models`` got its name: a compound of two
    names, belonging to neither concept.

    Each mandate also carries :func:`merge_reason` -- the evidence that chose
    this peer -- so the model can see whether it is folding together two things
    the ledger ties to each other or two things that merely read alike.
    """
    if not slate:
        return "(none -- there is no pressure)"
    rows: list[str] = []
    for pair in slate:
        doomed = names[pair.doomed_node_id]
        survivor = names[pair.survivor_node_id]
        rows.append(
            f"  MUST MERGE: {doomed!r} ({pair.doomed_node_id}) INTO {survivor!r} ({pair.survivor_node_id})"
            f" because {merge_reason(pair)}\n"
            f"      Send survivor_name {survivor!r}: the survivor KEEPS ITS OWN NAME, and any\n"
            f"      other name -- including a compound of the two -- is rejected. The name\n"
            f"      {doomed!r} stops existing; it is kept as an alias of {pair.survivor_node_id}."
        )
    return "\n".join(rows)


def _render_compaction(doomed: Sequence[str], edge_types: Mapping[str, int]) -> str:
    """The edge-type mandate: which types go, and which are available to fold into.

    Absent rather than empty when the scope is within ``M``. A block saying
    "nothing must be compacted" is a rule the model reads and then discards, and
    every discarded rule is budget spent teaching the prompt's own arithmetic.

    The surviving types are listed beside the doomed ones because ``new`` must be
    one of them, and a model told only what must go would have to subtract two
    lists in its head to find out what it may arrive at.
    """
    if not doomed:
        return ""
    surviving = ", ".join(name for name in edge_types if name not in set(doomed))
    rows = "\n".join(f"  MUST COMPACT: {name} ({edge_types[name]} edge(s))" for name in doomed)
    return (
        "\nMUST COMPACT EDGE TYPES -- MANDATORY, AND ALREADY DECIDED\n"
        f"This scope holds {len(edge_types)} relation types and may keep "
        f"{len(edge_types) - len(doomed)}. These are the least used ones, and each\n"
        "needs exactly one rename_edge_type op folding it into a type that is staying.\n"
        f"{rows}\n"
        f"  Types that are staying, and may be renamed INTO: {surviving}\n"
    )


def _global_contract(motive: CavemanMotive) -> str:
    """The 3b answer contract: one worked op of each of the six kinds.

    All six, every time, rather than only the ones this pass could use. An op the
    example omits is an op the model has read a one-line description of and never
    seen the shape of, and the two vocabulary ops are exactly the ones a prompt
    would be tempted to leave out because most passes do not need them.
    """
    return json.dumps(
        {
            "ops": [
                {
                    "op": "merge",
                    "nodes": ["n-003", "n-011"],
                    "survivor_name": "gateway",
                    "survivor_type": "service",
                    "facts": _example_facts(motive, limit=2),
                    "reason": "one service under two surface names",
                },
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        {
                            "name": "chart",
                            "type": "artifact",
                            "facts": [
                                {
                                    "kind": "attribute",
                                    "key": None,
                                    "text": "deploys the agent-memory MCP server, #240",
                                    "entry_ids": ["e-07"],
                                }
                            ],
                            "entry_ids": ["e-07"],
                            "relation_ids": [],
                        },
                        {
                            "name": "c4",
                            "type": "cluster",
                            "facts": [
                                {
                                    "kind": "attribute",
                                    "key": None,
                                    "text": "the memory server returns 31 tools after #248",
                                    "entry_ids": ["e-09", "e-12"],
                                }
                            ],
                            "entry_ids": ["e-09", "e-12"],
                            "relation_ids": [],
                        },
                    ],
                    "reason": "a deployment artifact and a cluster are separate concepts",
                },
                {"op": "retype", "node": "n-002", "type": "policy", "reason": "a rule, not a service"},
                {
                    "op": "rewrite",
                    "node": "n-005",
                    "facts": [
                        {"kind": "attribute", "key": "chat default", "text": "claude-haiku-4-5", "entry_ids": ["e-01"]}
                    ],
                    "relations": [
                        {
                            "relation_id": None,
                            "type": "FRONTS",
                            "target_id": "n-003",
                            "claim": "it is the proxy in front of the models",
                            "entry_ids": ["e-01"],
                            "until": None,
                        }
                    ],
                    "retired_relation_ids": [],
                    "reason": "the pairing was two facts on two nodes; it is one edge",
                },
                {
                    "op": "rename_edge_type",
                    "old": "PROXIES",
                    "new": "FRONTS",
                    "reason": "one relationship under two labels",
                },
                {"op": "retire_relation", "relation_id": "r-009", "reason": "#245 fixed it and nothing blocks now"},
            ]
        }
    )


def _global_prompt(
    *,
    nodes: Sequence[Node],
    at_risk: Sequence[ValuedNode],
    entry_ids: Mapping[str, Sequence[str]],
    relations: Mapping[str, Sequence[Relation]],
    slate: Sequence[MergePair],
    doomed_edge_types: Sequence[str],
    motive: CavemanMotive,
    count: int,
    signal: int,
    type_vocabulary: Sequence[str],
    edge_types: Mapping[str, int],
) -> str:
    """The 3b user prompt. One call, and both mandates are already decided."""
    names = {node.node_id: node.name for node in nodes}
    vocabulary = ", ".join(type_vocabulary) if type_vocabulary else "(none yet)"
    return (
        # Every kind: this pass rewrites many nodes and each op's facts are
        # validated against that op's OWN evidence, so a union computed here
        # would be the same list for a reader and a weaker rule for the model.
        f"{format_prompt_block(max_facts=motive.max_facts_per_node, kinds=tuple(FactKind))}\n"
        f"{render_motive_block(motive, stage='dream')}\n"
        f"NODE BUDGET: {count} of {motive.max_nodes} used. PRESSURE: {signal}\n"
        f"SCOPE TYPE VOCABULARY: {vocabulary}\n"
        f"EDGE TYPES IN THIS SCOPE: {_render_edge_types(edge_types)}\n"
        f"EDGE TYPE BUDGET: {len(edge_types)} of {motive.max_edge_types} used\n"
        f"{RELATION_TARGET_RULE}\n"
        f"\nINVENTORY ({count} nodes) -- id | name | type | first fact\n"
        f"{_render_inventory(nodes)}\n"
        f"\nFULL FACTS, RELATIONS AND LEDGER ENTRY IDS OF THE {len(at_risk)} NODES MOST AT RISK\n"
        f"(lowest survival value first; an op may cite only entry ids listed here)\n"
        f"{_render_at_risk(at_risk, entry_ids, relations, names)}\n"
        f"\nFORCED MERGE SLATE -- MANDATORY, AND ALREADY DECIDED\n{_render_slate(slate, names)}\n"
        f"{_render_compaction(doomed_edge_types, edge_types)}"
        f"\nANSWER CONTRACT -- one object with an `ops` array; every op needs a `reason`\n"
        f"{_global_contract(motive)}\n"
        f"\n{CONTRACT_CLOSING_LINE}\n"
    )


def _op_node_ids(op: GlobalOp) -> tuple[str, ...]:
    """Every node id an op claims. The two vocabulary ops claim none."""
    if isinstance(op, MergeOp):
        return op.nodes
    if isinstance(op, RenameEdgeTypeOp | RetireRelationOp):
        return ()
    return (op.node,)


def _surfaces(node: Node) -> frozenset[str]:
    """Every case-folded surface that routes to *node*: its name and its aliases.

    One helper because three rules ask the same question -- does this op name
    this node, does the slate's doomed half already answer to the survivor's
    name, and is ``survivor_name`` a name this scope has actually seen.
    """
    return frozenset({node.name.casefold(), *(alias.casefold() for alias in node.aliases)})


def _merge_survivor(op: MergeOp, nodes: Mapping[str, Node]) -> str | None:
    """The id of the node that keeps its identity, or ``None`` if the op names none.

    ``survivor_name`` identifies the survivor rather than labelling it (see the
    module docstring), so this is a lookup: the op's node whose **name** is
    *survivor_name* case-insensitively, else the one whose **aliases** contain
    it. ``None`` means the answer invented a name, which
    :meth:`CavemanDreamer._resolve_ops` turns into a rejection -- nothing is
    applied on a ``None``.

    Ties break on the lowest node id. Two nodes in one scope sharing a name is
    not something ``graph`` forbids, and a merge is not the place to discover it:
    an arbitrary-but-fixed choice keeps the pass deterministic.
    """
    wanted = op.survivor_name.casefold()
    named = sorted(node_id for node_id in op.nodes if nodes[node_id].name.casefold() == wanted)
    if named:
        return named[0]
    aliased = sorted(node_id for node_id in op.nodes if wanted in _surfaces(nodes[node_id]))
    return aliased[0] if aliased else None


def _absorbed_ids(op: MergeOp, survivor_id: str) -> tuple[str, ...]:
    """The op's other nodes, id-ordered. They lose their records; their names live on as aliases."""
    return tuple(sorted(set(op.nodes) - {survivor_id}))


class CavemanDreamer:
    """Stage 3, composed from the seams it needs and owning no state of its own.

    One object rather than three free functions because all three operations
    need the same six seams, and a composition root that has to thread six
    arguments through three call sites is where a fake gets passed to one of
    them and not the others.
    """

    def __init__(
        self,
        *,
        graph: GraphStore,
        ledger: LedgerStore,
        receipts: ReceiptSink,
        transport: SynthesisTransport,
        embedder: EmbeddingTransport,
        clock: Clock,
    ) -> None:
        self._graph = graph
        self._ledger = ledger
        self._receipts = receipts
        self._transport = transport
        self._embedder = embedder
        self._clock = clock

    # -- receipts ----------------------------------------------------------

    def _emit(
        self,
        op: ReceiptOp,
        *,
        scope: str,
        subject: str,
        inputs_digest: str,
        outputs_digest: str,
        detail: str,
        now: datetime,
    ) -> str:
        receipt_id = new_receipt_id()
        self._receipts.emit(
            Receipt(
                receipt_id=receipt_id,
                op=op,
                ts=now,
                scope=scope,
                subject=subject,
                inputs_digest=inputs_digest,
                outputs_digest=outputs_digest,
                detail=detail[:600],
            )
        )
        return receipt_id

    def _reject(
        self,
        op: ReceiptOp,
        *,
        source: str,
        scope: str,
        subject: str,
        prompts: tuple[str, str],
        payload: object,
        errors: Sequence[str],
        now: datetime,
    ) -> OutOfContractResponse:
        """Receipt a rejection this module found, then hand back the exception to raise.

        Same receipt op and same exception type as a wire-contract rejection from
        ``strict_json_call``, deliberately: a caller handling "the model answered
        out of contract" should not have to know whether pydantic or this module
        caught it. The digested payload is the VALIDATED response, since that is
        what failed here -- the raw text was already consumed and digested by the
        exchange that parsed it.
        """
        outputs_digest = canonical_digest(payload)
        self._emit(
            op,
            scope=scope,
            subject=subject,
            inputs_digest=canonical_digest({"system": prompts[0], "user": prompts[1]}),
            outputs_digest=outputs_digest,
            detail="; ".join(errors),
            now=now,
        )
        return OutOfContractResponse(source=source, errors=errors, raw_digest=outputs_digest[:16])

    # -- evidence ----------------------------------------------------------

    def _evidence_for(self, node_ids: Sequence[str], *, facts: Sequence[Fact] = ()) -> Evidence:
        """The :class:`Evidence` a batch of facts about *node_ids* is judged against.

        A merge passes every node it absorbs, because the re-key moves all of
        their entries onto the survivor and the survivor's facts may rest on any
        of them. Everything else passes one id.
        """
        entries = [entry for node_id in node_ids for entry in self._ledger.for_node(node_id)]
        return Evidence.of(entries, facts=facts)

    def _own_relations(self, node_id: str) -> dict[str, Relation]:
        """The edges running OUT of *node_id*, by relation id. What an answer may name.

        Outgoing only, and that is the contract rather than an optimisation. A
        :class:`~memotron.caveman.models.RelationSpec` names no source because
        a relation in a node answer runs out of the node being dreamed, so an
        INCOMING edge is a belief another concept holds about this one and a
        dream of this node has no standing to rewrite or retire it.

        The first live run under the named-field contract failed on exactly that:
        the prompt listed an incoming ``FRONTS`` edge among the ids "an answer
        rewrites or retires them by", the model named it, and the stage rejected
        the whole answer because the spec's implied source was the wrong end.
        The prompt now shows the two directions in two sections and only the
        outgoing one carries ids (:func:`_node_prompt`).
        """
        return {
            relation.relation_id: relation
            for relation in self._graph.relations_of(node_id)
            if relation.source_id == node_id
        }

    def _apply_relations(
        self,
        specs: Sequence[RelationSpec],
        retired: Sequence[str],
        *,
        source_id: str,
        scope: str,
        now: datetime,
    ) -> None:
        """Upsert every stated edge, then retire every named one.

        Upserts first, so a retype expressed as "state the new edge, retire the
        old one" never leaves the pair with no edge between them at any point a
        journal reader could observe. The two sets are disjoint by the response
        contract, so the order changes nothing else.

        ``upsert_relation`` is what makes a restated edge a reinforcement rather
        than a duplicate: its identity is the triple, so the same edge stated
        again unions its evidence and moves ``last_seen``.
        """
        for spec in specs:
            self._graph.upsert_relation(
                scope=scope,
                source_id=source_id,
                target_id=spec.target_id,
                type=spec.type,
                claim=spec.claim,
                entry_ids=spec.entry_ids,
                until=spec.until,
                now=now,
            )
        for relation_id in retired:
            self._graph.retire_relation(relation_id)

    # -- 3a: incremental ---------------------------------------------------

    async def dream_incremental(self, *, scope: str, motive: CavemanMotive) -> list[Node]:
        """Re-dream every dirty node in *scope*. One LLM call per node.

        Returns the re-dreamed nodes in the order they were dreamed. An empty
        dirty set makes zero calls and returns ``[]``.

        Raises:
            OutOfContractResponse: on the first node whose response is out of
                contract. That node stays dirty with its facts unchanged; nodes
                already applied in this batch stay applied.
        """
        dirty = self._graph.dirty(scope=scope)
        if not dirty:
            return []
        # The scope's other concepts WITH their ids: a fact belongs to one node,
        # and a belief between two of them is an edge naming the other's id --
        # neither of which a model can get right without being shown the ids.
        scope_nodes = self._graph.list_nodes(scope=scope)

        return [await self._dream_one(node, motive=motive, scope_nodes=scope_nodes) for node in dirty]

    async def _dream_one(self, node: Node, *, motive: CavemanMotive, scope_nodes: Sequence[Node]) -> Node:
        now = self._clock.now()
        entries = self._ledger.for_node(node.node_id, since=node.dreamed_at)
        evidence = self._evidence_for([node.node_id], facts=node.facts)
        own = self._own_relations(node.node_id)
        user_prompt = _node_prompt(
            node,
            entries=entries,
            evidence=evidence,
            # BOTH directions: ``_node_prompt`` splits them, because only the
            # outgoing half is this answer's to change and the incoming half is
            # context the dream needs in order not to restate it backwards.
            relations=self._graph.relations_of(node.node_id),
            scope_nodes=scope_nodes,
            edge_types=self._graph.edge_types(scope=node.scope),
            motive=motive,
        )
        response = await strict_json_call(
            transport=self._transport,
            system_prompt=DREAM_NODE_SYSTEM,
            user_prompt=user_prompt,
            response_model=DreamNodeResponse,
            source=NODE_SOURCE,
            receipts=self._receipts,
            scope=node.scope,
            subject=node.node_id,
            reject_op=ReceiptOp.DREAM_NODE_REJECTED,
            now=now,
        )

        errors = self._node_errors(
            response,
            node=node,
            motive=motive,
            evidence=evidence,
            own=own,
            known_node_ids=frozenset(other.node_id for other in scope_nodes),
        )
        if errors:
            raise self._reject(
                ReceiptOp.DREAM_NODE_REJECTED,
                source=NODE_SOURCE,
                scope=node.scope,
                subject=node.node_id,
                prompts=(DREAM_NODE_SYSTEM, user_prompt),
                payload=response.model_dump(mode="json"),
                errors=errors,
                now=now,
            )

        # Built AFTER validation and BEFORE the embedding, so the vector covers
        # the same text the node stores. See ``render.sort_facts``: a reader skims
        # by kind, and the model's own ordering is worth less than one the reader
        # can rely on.
        facts = _facts_from(response.facts, evidence=evidence, now=now)
        embedding = self._embedder.embed(
            _embedding_text(name=node.name, aliases=node.aliases, type=response.type, facts=facts)
        )
        applied = self._graph.replace_facts(
            node.node_id,
            facts=facts,
            type=response.type,
            embedding=embedding,
            now=now,
        )
        self._apply_relations(
            response.relations,
            response.retired_relation_ids,
            source_id=node.node_id,
            scope=node.scope,
            now=now,
        )
        self._graph.clear_dirty([node.node_id], now=now)

        inputs_digest = canonical_digest({"system": DREAM_NODE_SYSTEM, "user": user_prompt})
        outputs_digest = canonical_digest(response.model_dump(mode="json"))
        self._emit(
            ReceiptOp.DREAM_NODE_APPLIED,
            scope=node.scope,
            subject=node.node_id,
            inputs_digest=inputs_digest,
            outputs_digest=outputs_digest,
            detail=(
                f"{len(response.facts)} fact(s), {len(response.relations)} relation(s), "
                f"{len(response.retired_relation_ids)} retired, type={response.type}: {response.reason}"
            ),
            now=now,
        )
        for relation_id in response.retired_relation_ids:
            self._emit(
                ReceiptOp.DREAM_RELATION_RETIRED,
                scope=node.scope,
                subject=relation_id,
                inputs_digest=inputs_digest,
                outputs_digest=outputs_digest,
                detail=f"retired from {node.node_id}: {response.reason}",
                now=now,
            )
        return applied

    def _node_errors(
        self,
        response: DreamNodeResponse,
        *,
        node: Node,
        motive: CavemanMotive,
        evidence: Evidence,
        own: Mapping[str, Relation],
        known_node_ids: frozenset[str],
    ) -> tuple[str, ...]:
        """Every 3a rule that needs the motive, the node or the ledger. Empty accepts."""
        errors = list(_fact_errors(response.facts, motive=motive, evidence=evidence, what="facts"))
        errors.extend(
            _relation_errors(
                response.relations,
                retired=response.retired_relation_ids,
                source_id=node.node_id,
                evidence=evidence,
                known_node_ids=known_node_ids,
                own=own,
                what="relations",
            )
        )
        return tuple(errors)

    # -- 3b: global --------------------------------------------------------

    async def dream_global(self, *, scope: str, motive: CavemanMotive) -> GlobalOutcome:
        """Reorganise the whole scope. Exactly one LLM call, all-or-nothing.

        The forced-merge slate and the edge-type compaction mandate are both
        computed before the call and passed into the prompt verbatim, so the
        model is told which nodes go and which types go, and decides only what
        the survivors say and what each doomed type folds into.

        An empty scope makes zero calls: there is no inventory to reorganise, and
        asking a model to reorganise nothing is a call that can only fail.

        Raises:
            OutOfContractResponse: if any op is invalid. **Zero ops are applied**
                -- validation completes against the scope and the ledger before
                the first write.
        """
        nodes = self._graph.list_nodes(scope=scope)
        count_before = self._graph.count(scope=scope)
        if not nodes:
            return GlobalOutcome(
                count_before=0,
                count_after=0,
                ops_applied=0,
                pressure=0,
                slate=(),
                edge_type_pressure=(),
            )

        now = self._clock.now()
        signal = pressure(count_before, motive.max_nodes)
        valued = sorted(
            (
                ValuedNode(
                    node=node,
                    value=node_value(
                        node,
                        degree=len(self._graph.relations_of(node.node_id)),
                        now=now,
                        weights=motive.value_weights,
                        half_life_days=motive.recency_half_life_days,
                        type_weight=motive.type_weight,
                    ),
                )
                for node in nodes
            ),
            key=lambda item: (item.value, item.node_id),
        )
        # Both ledger signals are whole-scope statements rather than per-pair
        # queries: the turn map comes back in one call, and `cooccurrence` is
        # asked once per DOOMED node because the relation is symmetric, so a
        # single lookup weighs that node against the rest of the scope. A
        # per-pair query would make the peer scan the square of the node count.
        slate = merge_slate(
            valued,
            pressure=signal,
            cooccurrence=self._ledger.cooccurrence,
            turn_links=self._ledger.turn_cooccurrence(scope=scope),
            similarity=embedding_similarity,
        )
        edge_types = self._graph.edge_types(scope=scope)
        doomed_edge_types = compaction_targets(edge_types, max_edge_types=motive.max_edge_types)

        slate_ids = {node_id for pair in slate for node_id in pair.node_ids}
        at_risk = [item for item in valued[:AT_RISK_NODES] if item.node_id not in slate_ids]
        at_risk = [item for item in valued if item.node_id in slate_ids] + at_risk
        entry_ids = {
            item.node_id: [entry.entry_id for entry in self._ledger.for_node(item.node_id)] for item in at_risk
        }
        relations = {item.node_id: self._graph.relations_of(item.node_id) for item in at_risk}

        user_prompt = _global_prompt(
            nodes=nodes,
            at_risk=at_risk,
            entry_ids=entry_ids,
            relations=relations,
            slate=slate,
            doomed_edge_types=doomed_edge_types,
            motive=motive,
            count=count_before,
            signal=signal,
            type_vocabulary=self._graph.type_vocabulary(scope=scope),
            edge_types=edge_types,
        )
        response = await strict_json_call(
            transport=self._transport,
            system_prompt=DREAM_GLOBAL_SYSTEM,
            user_prompt=user_prompt,
            response_model=DreamGlobalResponse,
            source=GLOBAL_SOURCE,
            receipts=self._receipts,
            scope=scope,
            subject=scope,
            reject_op=ReceiptOp.DREAM_GLOBAL_REJECTED,
            now=now,
        )

        by_id = {node.node_id: node for node in nodes}
        # The two prerequisite rules run first and short-circuit: an unknown node
        # id or an unresolvable survivor_name makes every rule below it undefined
        # rather than merely also-wrong. See ``_resolve_ops``.
        survivors, gate_errors = self._resolve_ops(response.ops, nodes=by_id)
        errors = gate_errors or self._global_errors(
            response,
            scope=scope,
            nodes=by_id,
            slate=slate,
            count_before=count_before,
            motive=motive,
            survivors=survivors,
            edge_types=edge_types,
            doomed_edge_types=doomed_edge_types,
        )
        if errors:
            raise self._reject(
                ReceiptOp.DREAM_GLOBAL_REJECTED,
                source=GLOBAL_SOURCE,
                scope=scope,
                subject=scope,
                prompts=(DREAM_GLOBAL_SYSTEM, user_prompt),
                payload=response.model_dump(mode="json"),
                errors=errors,
                now=now,
            )

        digests = (
            canonical_digest({"system": DREAM_GLOBAL_SYSTEM, "user": user_prompt}),
            canonical_digest(response.model_dump(mode="json")),
        )
        # A merge is the only op that needs the survivor, so it is handed to
        # ``_apply_merge`` directly rather than threaded through the dispatch of
        # the ops that have no use for it.
        for index, op in enumerate(response.ops):
            if isinstance(op, MergeOp):
                self._apply_merge(op, survivor_id=survivors[index], scope=scope, digests=digests, now=now)
            else:
                self._apply_op(op, scope=scope, digests=digests, now=now)

        return GlobalOutcome(
            count_before=count_before,
            count_after=self._graph.count(scope=scope),
            ops_applied=len(response.ops),
            pressure=signal,
            slate=slate,
            edge_type_pressure=doomed_edge_types,
        )

    def _global_errors(
        self,
        response: DreamGlobalResponse,
        *,
        scope: str,
        nodes: Mapping[str, Node],
        slate: Sequence[MergePair],
        count_before: int,
        motive: CavemanMotive,
        survivors: Mapping[int, str],
        edge_types: Mapping[str, int],
        doomed_edge_types: Sequence[str],
    ) -> tuple[str, ...]:
        """Every 3b rule that needs the scope or the ledger. Empty means accept.

        Runs to completion and reports everything it found, because a global pass
        that broke three rules broke them for one reason and seeing all three is
        the diagnosis.

        *survivors* maps each merge op's index to the node id it keeps, already
        resolved by :meth:`_resolve_ops` -- which the caller runs first, because
        both rules it carries make every rule here undefined rather than merely
        also-wrong.
        """
        errors: list[str] = []
        errors.extend(self._slate_direction_errors(response.ops, nodes=nodes, slate=slate, survivors=survivors))

        delta = 0
        for op in response.ops:
            if isinstance(op, MergeOp):
                delta -= len(op.nodes) - 1
                errors.extend(self._merge_errors(op, motive=motive))
            elif isinstance(op, SplitOp):
                delta += len(op.into) - 1
                errors.extend(self._split_errors(op, count_before=count_before, motive=motive))
            elif isinstance(op, RewriteOp):
                errors.extend(self._rewrite_errors(op, nodes=nodes, motive=motive))

        errors.extend(
            f"slate: the mandatory merge [{pair.render()}] does not appear in any merge op"
            for pair in slate
            if not any(isinstance(op, MergeOp) and pair.node_ids <= set(op.nodes) for op in response.ops)
        )
        errors.extend(
            self._vocabulary_errors(
                response.ops,
                scope=scope,
                edge_types=edge_types,
                doomed_edge_types=doomed_edge_types,
            )
        )

        if count_before + delta > motive.max_nodes:
            errors.append(
                f"ops: this pass would leave {count_before + delta} nodes, over the budget of {motive.max_nodes}"
            )
        return tuple(errors)

    def _merge_errors(self, op: MergeOp, *, motive: CavemanMotive) -> tuple[str, ...]:
        """The survivor's facts, against the UNION of every merged node's evidence.

        The union, because the re-key moves every absorbed node's entries onto
        the survivor: a survivor fact resting on a claim that arrived through a
        node being absorbed is supported after the merge, and rejecting it would
        make a merge unable to state the very thing it is collapsing.

        The survivor's current facts are the reinforcement baseline, so a merge
        that restates one of them keeps its age and must carry its evidence.
        """
        survivor_facts = tuple(fact for node_id in op.nodes for fact in self._graph.get_node(node_id).facts)
        return _fact_errors(
            op.facts,
            motive=motive,
            evidence=self._evidence_for(op.nodes, facts=survivor_facts),
            what=f"merge {list(op.nodes)}",
        )

    def _rewrite_errors(self, op: RewriteOp, *, nodes: Mapping[str, Node], motive: CavemanMotive) -> tuple[str, ...]:
        """A rewrite's facts and its edges, against the node it is rewriting.

        The node's CURRENT facts are the reinforcement baseline, exactly as in
        the incremental pass: a rewrite that restates a fact the node holds is
        reinforcing it and must carry its evidence.
        """
        node = nodes[op.node]
        evidence = self._evidence_for([op.node], facts=node.facts)
        errors = list(_fact_errors(op.facts, motive=motive, evidence=evidence, what=f"rewrite {op.node}"))
        errors.extend(
            _relation_errors(
                op.relations,
                retired=op.retired_relation_ids,
                source_id=op.node,
                evidence=evidence,
                known_node_ids=frozenset(nodes),
                own=self._own_relations(op.node),
                what=f"rewrite {op.node}",
            )
        )
        return tuple(errors)

    def _vocabulary_errors(
        self,
        ops: Sequence[GlobalOp],
        *,
        scope: str,
        edge_types: Mapping[str, int],
        doomed_edge_types: Sequence[str],
    ) -> tuple[str, ...]:
        """The ``M`` bound and the two vocabulary ops' own rules.

        Four rules, and the last is the mandate:

        * ``old`` must be a type the scope actually holds -- renaming a type no
          edge carries changes nothing and is a misread of the vocabulary;
        * ``new`` must be a type the scope holds and is KEEPING. A rename into a
          brand-new type grows the vocabulary this op exists to shrink, and a
          rename into a doomed type makes the end state depend on which op is
          applied second;
        * a retired ``relation_id`` must be an edge of this scope;
        * every doomed type must be renamed. Same rule as the merge slate: an
          under-delivered compaction leaves ``M`` violated with nothing reporting
          it, which is a bound that is not a bound.
        """
        errors: list[str] = []
        doomed = set(doomed_edge_types)
        known_relation_ids = {relation.relation_id for relation in self._graph.relations(scope=scope)}
        renamed: set[str] = set()
        for op in ops:
            if isinstance(op, RenameEdgeTypeOp):
                renamed.add(op.old)
                if op.old not in edge_types:
                    errors.append(f"rename_edge_type: no edge in this scope has type {op.old}")
                if op.new not in edge_types:
                    errors.append(
                        f"rename_edge_type {op.old}: {op.new} is not a type this scope holds -- a rename folds "
                        f"one existing type into another and never coins a new one"
                    )
                elif op.new in doomed:
                    errors.append(
                        f"rename_edge_type {op.old}: {op.new} is itself being compacted away, so folding into "
                        f"it would leave the vocabulary over budget"
                    )
            elif isinstance(op, RetireRelationOp) and op.relation_id not in known_relation_ids:
                errors.append(f"retire_relation: no relation {op.relation_id} in this scope")
        errors.extend(
            f"vocabulary: edge type {name} must be compacted away and no rename_edge_type op names it"
            for name in doomed_edge_types
            if name not in renamed
        )
        return tuple(errors)

    def _resolve_ops(
        self,
        ops: Sequence[GlobalOp],
        *,
        nodes: Mapping[str, Node],
    ) -> tuple[dict[int, str], tuple[str, ...]]:
        """``({merge op index: survivor id}, errors)`` -- the two prerequisite rules.

        Both checks here make every other rule *undefined* rather than merely
        also-wrong, which is why they are one function that returns early:

        1. an op naming a node the scope does not hold -- everything below
           dereferences those ids;
        2. a merge whose ``survivor_name`` names none of its own nodes -- the
           post-pass name set is not knowable until every merge knows which node
           it keeps.

        The survivors are returned rather than recomputed later because the id
        that was validated must be the id that is applied. Resolving twice is how
        those two stop being the same node.
        """
        unknown = sorted({node_id for op in ops for node_id in _op_node_ids(op) if node_id not in nodes})
        if unknown:
            # Reporting "n-999 has no ledger entries" on top of "n-999 does not
            # exist" is noise.
            return {}, (f"ops: no such node in this scope: {', '.join(unknown)}",)

        survivors: dict[int, str] = {}
        errors: list[str] = []
        for index, op in enumerate(ops):
            if not isinstance(op, MergeOp):
                continue
            resolved = _merge_survivor(op, nodes)
            if resolved is None:
                held = ", ".join(f"{node_id}={nodes[node_id].name!r}" for node_id in op.nodes)
                errors.append(
                    f"merge {list(op.nodes)}: a merge keeps its survivor's name, and {op.survivor_name!r} is "
                    f"not the current name or a known alias of any of them ({held})"
                )
                continue
            survivors[index] = resolved
        return survivors, tuple(errors)

    def _slate_direction_errors(
        self,
        ops: Sequence[GlobalOp],
        *,
        nodes: Mapping[str, Node],
        slate: Sequence[MergePair],
        survivors: Mapping[int, str],
    ) -> tuple[str, ...]:
        """A mandated pair must keep the half ``pressure`` chose.

        Coverage -- "the pair appears inside some merge" -- is checked separately,
        so a pair no op contains is reported once, there, and skipped here rather
        than reported twice as two different defects.

        The exception is two nodes that are **already aliases of each other**:
        they answer to the same surfaces, so whichever record survives, every
        name a reader might search for still resolves. Nothing observable turns
        on the direction, and rejecting it would cost a whole forced pass for
        no reader's benefit.
        """
        errors: list[str] = []
        for pair in slate:
            mandated = [
                (index, op)
                for index, op in enumerate(ops)
                if isinstance(op, MergeOp) and pair.node_ids <= set(op.nodes)
            ]
            if not mandated:
                continue
            index, op = mandated[0]
            kept = survivors[index]
            if kept == pair.survivor_node_id:
                continue
            if _surfaces(nodes[pair.doomed_node_id]) & _surfaces(nodes[pair.survivor_node_id]):
                continue
            errors.append(
                f"slate: the mandatory merge [{pair.render()}] must keep "
                f"{nodes[pair.survivor_node_id].name!r} ({pair.survivor_node_id}), but survivor_name "
                f"{op.survivor_name!r} keeps {nodes[kept].name!r} ({kept}) instead"
            )
        return tuple(errors)

    def _split_errors(self, op: SplitOp, *, count_before: int, motive: CavemanMotive) -> tuple[str, ...]:
        """Headroom, the exact partition of the parent's entries, and each part's facts.

        A part's facts are judged against that part's OWN slice of the entries,
        not the parent's whole set: the point of a split is that two topics were
        sharing one node, and a part whose fact rests on the other part's
        evidence has not been separated from it.
        """
        errors: list[str] = []
        projected_count = count_before + len(op.into) - 1
        if projected_count > motive.max_nodes:
            errors.append(
                f"split {op.node}: no headroom -- {count_before} nodes plus {len(op.into)} parts is "
                f"{projected_count}, over the budget of {motive.max_nodes}"
            )
        parent = self._ledger.for_node(op.node)
        parent_entries = {entry.entry_id for entry in parent}
        assigned = [entry_id for part in op.into for entry_id in part.entry_ids]
        duplicated = sorted({entry_id for entry_id in assigned if assigned.count(entry_id) > 1})
        if duplicated:
            errors.append(f"split {op.node}: entry {', '.join(duplicated)} assigned to more than one part")
        dropped = sorted(parent_entries - set(assigned))
        if dropped:
            errors.append(f"split {op.node}: entry {', '.join(dropped)} assigned to no part")
        foreign = sorted(set(assigned) - parent_entries)
        if foreign:
            errors.append(f"split {op.node}: entry {', '.join(foreign)} is not bound to this node")
        by_id = {entry.entry_id: entry for entry in parent}
        for part in op.into:
            mine = [by_id[entry_id] for entry_id in part.entry_ids if entry_id in by_id]
            errors.extend(
                _fact_errors(
                    part.facts,
                    motive=motive,
                    evidence=Evidence.of(mine),
                    what=f"split part {part.name}",
                )
            )
        return tuple(errors)

    def _apply_op(
        self,
        op: SplitOp | RetypeOp | RewriteOp | RenameEdgeTypeOp | RetireRelationOp,
        *,
        scope: str,
        digests: tuple[str, str],
        now: datetime,
    ) -> None:
        """Apply one validated op.

        A merge is not dispatched here: it needs the survivor id validation
        resolved, and threading that through the ops that have no use for it
        would make it an optional argument -- the shape that lets an unresolved
        merge reach a write. ``dream_global`` calls :meth:`_apply_merge` instead.
        """
        if isinstance(op, SplitOp):
            self._apply_split(op, scope=scope, digests=digests, now=now)
        elif isinstance(op, RetypeOp):
            self._apply_retype(op, scope=scope, digests=digests, now=now)
        elif isinstance(op, RewriteOp):
            self._apply_rewrite(op, scope=scope, digests=digests, now=now)
        elif isinstance(op, RenameEdgeTypeOp):
            self._apply_rename_edge_type(op, scope=scope, digests=digests, now=now)
        else:
            self._apply_retire_relation(op, scope=scope, digests=digests, now=now)

    def _apply_merge(
        self,
        op: MergeOp,
        *,
        survivor_id: str,
        scope: str,
        digests: tuple[str, str],
        now: datetime,
    ) -> None:
        """Collapse the op's nodes into *survivor_id*, which validation resolved.

        The survivor keeps the name it already holds -- ``survivor.name``, not
        ``op.survivor_name``. The two agree case-insensitively (that is how the
        survivor was identified, and it may have been identified by an alias),
        and taking the node's own stored spelling is what stops a merge quietly
        re-casing a live name.

        No edge rebuild follows. ``graph.merge_nodes`` re-points the absorbed
        nodes' relations onto the survivor, drops the self-loops that creates and
        unions what collides, so the old ``set_link_weights`` step after the
        ledger re-key has nothing left to do (#251 amendment D).
        """
        absorbed_ids = _absorbed_ids(op, survivor_id)
        survivor = self._graph.get_node(survivor_id)
        name = survivor.name
        # The alias set the merge will store, computed HERE because the embedding
        # is an argument to ``merge_nodes`` and has to cover the same surfaces.
        # ``models.merged_aliases`` is the one rule both sides read.
        aliases = merged_aliases(
            name=name,
            survivor=survivor,
            absorbed=self._graph.get_nodes(absorbed_ids),
        )
        facts = _facts_from(op.facts, evidence=self._evidence_for(op.nodes, facts=survivor.facts), now=now)
        embedding = self._embedder.embed(
            _embedding_text(name=name, aliases=aliases, type=op.survivor_type, facts=facts)
        )
        self._graph.merge_nodes(
            survivor_id=survivor_id,
            absorbed_ids=absorbed_ids,
            name=name,
            type=op.survivor_type,
            facts=facts,
            embedding=embedding,
            now=now,
        )
        for absorbed_id in absorbed_ids:
            moved = self._ledger.rekey_node(from_node_id=absorbed_id, to_node_id=survivor_id)
            receipt_id = self._emit(
                ReceiptOp.DREAM_MERGED,
                scope=scope,
                subject=survivor_id,
                inputs_digest=digests[0],
                outputs_digest=digests[1],
                detail=f"{absorbed_id} absorbed into {survivor_id} as {name!r}: {op.reason}",
                now=now,
            )
            self._ledger.record_alias(
                NodeAlias(
                    alias_node_id=absorbed_id,
                    survivor_node_id=survivor_id,
                    moved_entry_ids=tuple(moved),
                    receipt_id=receipt_id,
                    ts=now,
                )
            )

    def _apply_split(self, op: SplitOp, *, scope: str, digests: tuple[str, str], now: datetime) -> None:
        """Split the parent, re-key its entries, then write each part's facts.

        Three steps in this order, and the order is forced: ``split_node`` mints
        the part ids, ``reassign`` moves the entries onto them, and only then can
        a part's facts be written -- a fact carries the ledger entry ids that
        evidence it, and before the re-key those entries still belong to the
        parent. So the store creates each part empty and the dreamer fills it,
        which keeps "the dreamer is the only writer of fact text" true of a split
        as well.
        """
        parts = self._graph.split_node(node_id=op.node, parts=op.into, now=now)
        for part, spec in zip(parts, op.into, strict=True):
            self._ledger.reassign(entry_ids=spec.entry_ids, from_node_id=op.node, node_id=part.node_id)
        for part, spec in zip(parts, op.into, strict=True):
            facts = _facts_from(spec.facts, evidence=self._evidence_for([part.node_id]), now=now)
            self._graph.replace_facts(
                part.node_id,
                facts=facts,
                type=part.type,
                embedding=self._embedder.embed(
                    _embedding_text(name=part.name, aliases=part.aliases, type=part.type, facts=facts)
                ),
                now=now,
            )
        self._emit(
            ReceiptOp.DREAM_SPLIT,
            scope=scope,
            subject=op.node,
            inputs_digest=digests[0],
            outputs_digest=digests[1],
            detail=f"split into {', '.join(part.node_id for part in parts)}: {op.reason}",
            now=now,
        )

    def _apply_retype(self, op: RetypeOp, *, scope: str, digests: tuple[str, str], now: datetime) -> None:
        node = self._graph.get_node(op.node)
        # Re-embedded rather than reusing the stored vector: the type is part of
        # the embedding text, so keeping the old vector would leave the node
        # findable only under its old type.
        embedding = self._embedder.embed(
            _embedding_text(name=node.name, aliases=node.aliases, type=op.type, facts=node.facts)
        )
        self._graph.replace_facts(op.node, facts=node.facts, type=op.type, embedding=embedding, now=now)
        self._emit(
            ReceiptOp.DREAM_RETYPED,
            scope=scope,
            subject=op.node,
            inputs_digest=digests[0],
            outputs_digest=digests[1],
            detail=f"{node.type} to {op.type}: {op.reason}",
            now=now,
        )

    def _apply_rewrite(self, op: RewriteOp, *, scope: str, digests: tuple[str, str], now: datetime) -> None:
        node = self._graph.get_node(op.node)
        facts = _facts_from(op.facts, evidence=self._evidence_for([op.node], facts=node.facts), now=now)
        embedding = self._embedder.embed(
            _embedding_text(name=node.name, aliases=node.aliases, type=node.type, facts=facts)
        )
        self._graph.replace_facts(op.node, facts=facts, type=node.type, embedding=embedding, now=now)
        self._apply_relations(op.relations, op.retired_relation_ids, source_id=op.node, scope=scope, now=now)
        # ``DREAM_NODE_APPLIED`` and not a rewrite-specific op: the receipt
        # taxonomy has no rewrite member, and what a rewrite does IS apply a
        # node's new fact set. The detail says which pass wrote it.
        self._emit(
            ReceiptOp.DREAM_NODE_APPLIED,
            scope=scope,
            subject=op.node,
            inputs_digest=digests[0],
            outputs_digest=digests[1],
            detail=(
                f"global rewrite, {len(op.facts)} fact(s), {len(op.relations)} relation(s), "
                f"{len(op.retired_relation_ids)} retired: {op.reason}"
            ),
            now=now,
        )

    def _apply_rename_edge_type(
        self,
        op: RenameEdgeTypeOp,
        *,
        scope: str,
        digests: tuple[str, str],
        now: datetime,
    ) -> None:
        """Fold one relation type into another. The ``M`` half of the dreamer's job.

        The count comes back from the store rather than being recomputed here:
        ``rename_edge_type`` folds an edge into one the pair already holds under
        the new type, so "how many edges were re-labelled" is a number only the
        store knows, and this receipt is the record of the compaction.
        """
        renamed = self._graph.rename_edge_type(scope=scope, old=op.old, new=op.new)
        self._emit(
            ReceiptOp.DREAM_EDGE_TYPE_RENAMED,
            scope=scope,
            subject=op.old,
            inputs_digest=digests[0],
            outputs_digest=digests[1],
            detail=f"{renamed} edge(s) re-labelled {op.old} to {op.new}: {op.reason}",
            now=now,
        )

    def _apply_retire_relation(
        self,
        op: RetireRelationOp,
        *,
        scope: str,
        digests: tuple[str, str],
        now: datetime,
    ) -> None:
        """Retire one edge, naming what it asserted in the receipt.

        The claim is read BEFORE the retirement, because afterwards the edge is
        gone from the graph and a receipt saying only "r-009 retired" would
        record the loss of a belief without the belief. The journal's own
        ``DreamEvent`` carries the whole record, for the same reason.
        """
        relation = self._graph.get_relation(op.relation_id)
        self._graph.retire_relation(op.relation_id)
        self._emit(
            ReceiptOp.DREAM_RELATION_RETIRED,
            scope=scope,
            subject=op.relation_id,
            inputs_digest=digests[0],
            outputs_digest=digests[1],
            detail=(
                f"{relation.source_id} {relation.type} {relation.target_id} retired ({relation.claim}): {op.reason}"
            ),
            now=now,
        )

    # -- erasure -----------------------------------------------------------

    def erase_episode(self, *, episode_id: str, scope: str) -> ErasureOutcome:
        """Erase an episode and leave the graph regenerable from what remains.

        No LLM: erasure is a ledger delete plus a re-dream. A node that lost
        every supporting entry is **deleted** rather than marked dirty, because
        re-dreaming a node with no entries would invent content. The rest are
        marked dirty for the next incremental pass.

        Unchanged in shape by amendment D, and its deletions are journal events
        like every other mutation: ``delete_nodes`` takes each node's edges with
        it, and the ``GraphStore`` this dreamer holds is the journalled one.

        *scope* is an addition to the design document's ``erase_episode(episode_id)``:
        a receipt carries a scope, and the ledger cannot supply one for an
        episode whose entries it has just deleted.
        """
        now = self._clock.now()
        entries = self._ledger.for_episode(episode_id)
        touched = self._ledger.delete_episode(episode_id)

        unsupported = tuple(node_id for node_id in touched if not self._ledger.for_node(node_id))
        surviving = tuple(node_id for node_id in touched if node_id not in set(unsupported))
        self._graph.delete_nodes(unsupported)
        self._graph.mark_dirty(surviving)

        outcome = ErasureOutcome(
            episode_id=episode_id,
            deleted_entry_count=len(entries),
            deleted_node_ids=unsupported,
            dirty_node_ids=surviving,
        )
        self._emit(
            ReceiptOp.ERASURE_APPLIED,
            scope=scope,
            subject=episode_id,
            inputs_digest=canonical_digest({"episode_id": episode_id, "scope": scope}),
            outputs_digest=canonical_digest(
                {
                    "deleted_entries": len(entries),
                    "deleted_nodes": list(unsupported),
                    "dirty_nodes": list(surviving),
                }
            ),
            detail=(
                f"{len(entries)} entry/entries erased; {len(unsupported)} node(s) deleted, "
                f"{len(surviving)} marked dirty"
            ),
            now=now,
        )
        return outcome
