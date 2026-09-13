"""Stage 2: the matcher. Surface names to node ids, claims to typed edges.

A routing layer and nothing else. It decides which concept a surface name
denotes, binds the entry to it, records the typed edge a relational claim
asserts, and marks both ends for the dreamer. It never writes a node's facts: a
node it creates starts empty and dirty, because the first fact set is a
compression decision and compression has exactly one owner.

.. rubric:: Three decisions, and the third is the one that gets missed

Every answer this stage gives is one of three (#251 amendment D, package D-B):

* a **new node** -- the name denotes a concept the scope does not hold yet;
* a **new edge type** -- the claim asserts a KIND of belief between two
  concepts that the scope has never recorded, so the vocabulary grows by one;
* an **existing type applied** -- the claim asserts a kind of belief the scope
  already has a word for, now holding between this pair, so the type is reused.

The third is what keeps the vocabulary small enough for the dreamer to hold at
``M`` (``CavemanMotive.max_edge_types``) without compacting every pass, and it
is the one a model skips if the prompt does not put the existing vocabulary in
front of it with its counts. So the prompt does:
``EDGE TYPES IN THIS SCOPE: BLOCKED (3), DEPLOYS (2)``.

.. rubric:: Motive-neutrality is the whole point of this stage

There is **one graph per scope**, and a motive is a policy over it rather than a
graph of its own. So this stage renders no motive: no name, no goal, no rubric,
no persona. A unit test asserts the rendered prompt contains neither
``motive.name`` nor ``motive.goal``, and the only thing this module reads off the
motive is ``max_nodes`` — a number, used after the model has answered, to report
pressure.

The same reasoning fixes :data:`CANDIDATE_K` here rather than taking it from the
motive's ``read_k``: how many candidates the router is shown is a property of the
router, and sourcing it from a persona would make the truth layer's *behaviour*
persona-dependent even while its prompt stayed clean.

.. rubric:: Within-batch reconciliation is structural, not incidental

One call sees every distinct surface name in the batch at once, and each
``new_node.name`` group — case-folded — becomes exactly **one** node. Two names
converging on one concept is therefore a first-class outcome rather than luck
about string collisions, which is why the adjudication is one batched call and
not one call per name.

.. rubric:: This stage is the only recorder of a node's aliases

Every other stage sees a node or a claim; only this one sees a **surface name
next to the node it was decided to denote**, which is exactly what an alias is.
So a ``bind`` records the routed name on the node
(``GraphStore.add_aliases``) and a ``new`` group hands the whole converged group
to ``create_node(aliases=…)``. From then on the name a searcher actually types
is an exact-match key (``GraphStore.node_by_alias``) rather than something only
this stage's transcript remembers.

Both calls pass the names **unfiltered**. The alias rule — case-preserving,
case-insensitively deduped, never repeating the node's own ``name`` — lives in
``models.normalize_aliases``, which both seams apply; re-deriving any part of it
here would put one rule in two places and make ``add_aliases`` non-idempotent by
accident. Passing a name that is already the node's name, or an existing alias
under a different spelling, is therefore a no-op by construction rather than by a
check here.

Recording an alias does not re-embed the node, even though a node's embedding
text is ``name + aliases + type + lines``. It does not have to: every touched
node is marked dirty in the same pass, and the dreamer — the only writer of lines
and the only builder of that vector — re-embeds it with the new aliases
included. Re-embedding here would be a second, motive-neutral writer of the same
field.

.. rubric:: A candidate's gloss comes from the ledger, never from its lines

The design requires candidates to be rendered
``id | name | aliases | type | gloss``, with "gloss only, never the node's
lines", so that the truth layer never reads motive-shaped content. Aliases join
that row because they are the names the router itself assigned on earlier
batches: a name it has already routed once is then recognised **on sight**
instead of having to be re-derived from a gloss. They are search keys, not
compressed content, so showing them takes nothing away from the invariant.

But :class:`~memotron.caveman.models.Node` has no ``gloss`` field and
``GraphStore.create_node`` takes none, so the gloss a router proposed for a new
node is not kept anywhere.

The gloss is therefore **derived from the ledger**: a candidate node's earliest
``=`` (identity) claim, or failing that its earliest claim at all, truncated to
:data:`GLOSS_LIMIT` characters. That reads only raw claims — the same kind of
data this stage already reads for the names it is adjudicating — and never a
node's compressed lines, which is exactly the content the invariant is about.

.. rubric:: An edge is written once and reinforced thereafter

A relational claim -- ``kind == relation``, or any claim carrying ``objects`` --
becomes one ``graph.upsert_relation`` call: the model's ``type``, the two ends it
named resolved to node ids, and **the ledger claim verbatim** as the edge's own
text, with that entry as its evidence. The triple ``(source, type, target)`` is
the identity, so a second claim restating the same belief **reinforces** the edge
-- its entry id is appended and ``last_seen`` moves -- instead of storing a
second edge. That is what makes ``Relation.evidence`` a count of how well
attested a belief is rather than a count of how often somebody wrote it down.

A claim naming one concept writes no edge at all. It binds and marks dirty, and
the dreamer writes it as a fact on the node.

Three consequences worth knowing before reading a stored relation:

* **A converged pair writes nothing.** When both names the model gave resolve to
  the SAME node -- which is exactly what happens when a claim states that two
  surface names denote one thing -- there is no edge to write: a relation from a
  thing to itself asserts nothing a fact cannot state better, and ``Relation``
  refuses one. The claim is skipped rather than rejected, because the
  convergence was this stage's own decision and the model's answer was in
  contract.
* **A reinforcement adds evidence and rewrites nothing.** A restated belief
  reaches ``upsert_relation`` with ``claim=None``, which means "keep the edge's
  text, marker included", so an edge the dreamer had compressed and marked
  ``until #245`` gains an entry and keeps both. This stage has no opinion on
  either: it holds the raw ledger claim, which is the right text for a NEW edge
  and the wrong one to overwrite a compression with, and it has no basis at all
  for a marker.
* **Every relational claim is carryable, by construction.** ``LedgerEntry.claim``
  and ``Relation.claim`` share one bound (``MAX_CLAIM_TEXT``) and one line rule,
  so there is no claim the ledger will hold that an edge cannot carry verbatim.
  This stage used to check that up front and refuse the batch; the check went
  when the bounds were made equal, because a precondition that cannot fail is a
  reader's false lead.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.llm import CONTRACT_CLOSING_LINE, strict_json_call
from memotron.caveman.models import (
    EDGE_TYPE_PATTERN,
    BindingSpec,
    ClaimKind,
    FactKind,
    LedgerEntry,
    NewNodeSpec,
    Receipt,
    ReceiptOp,
    ReconcileResponse,
)
from memotron.caveman.motive import CavemanMotive
from memotron.caveman.receipts import canonical_digest, new_receipt_id
from memotron.caveman.render import format_prompt_block
from memotron.caveman.seams import GraphStore, LedgerStore, ReceiptSink
from memotron.embedding import EmbeddingTransport
from memotron.synthesis import SynthesisTransport

RECONCILE_SOURCE = "RECONCILE"
"""Names this stage in the parse error, on the exception, and in the receipt."""

CANDIDATE_K = 8
"""How many kNN candidates one surface name is offered.

Fixed here rather than read off the motive: the candidate count is a property of
the routing layer, and taking it from a persona's ``read_k`` would make the
persona-independent stage behave differently per persona. Eight is the same
neighbourhood size the read path defaults to, which is the number the in-memory
kNN scan was sized against.
"""

GLOSS_LIMIT = 120
"""Characters of a candidate's derived gloss. About 30 tokens.

Bounded because the candidate table is ``O(names x k)`` lines and a claim may be
400 characters: an unbounded gloss would let one batch's candidate table dominate
the prompt.
"""

GLOSS_ENTRY_SCAN = 8
"""How many of a candidate's earliest entries are read to find its gloss.

Enough to find an identity claim if the node has one, bounded so a node with 400
entries costs the same as a node with four.
"""

NO_ALIASES = "-"
"""What the candidate table's alias column holds for a node with no aliases.

A visible placeholder rather than an empty cell: the row is pipe-delimited, and
two adjacent delimiters read as a rendering bug to a model asked to parse the
column, which is the sort of thing that turns into a spurious ``new``.
"""

NO_EDGE_TYPES = "none yet"
"""What the edge-type line says for a scope holding no relations.

Words rather than an empty value, for the reason :data:`NO_ALIASES` is: a
vocabulary line that renders as nothing at all reads as a defect, and a model
that suspects the list was truncated will coin a type instead of reusing one.
"""


class RelationSpec(BaseModel):
    """One relational claim's typed edge, as the matcher answers it.

    ``claim_index`` is a position in the batch's RELATIONAL claims -- the list
    the prompt numbers -- and not a ledger entry id, because an entry id is a
    token a model can transpose and a position is one it can point at. The two
    ends are **surface names spelled as the batch gave them**, not node ids: a
    name answered ``new`` has no node id yet, so names are the only vocabulary
    both halves of one response can share.

    ``type`` is validated against
    :data:`~memotron.caveman.models.EDGE_TYPE_PATTERN` here, so a type that is
    not UPPER_SNAKE is a parse-time rejection with zero writes rather than a
    ``ValueError`` raised from the store halfway through the write phase.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_index: int = Field(ge=0)
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    type: str = Field(pattern=EDGE_TYPE_PATTERN)


class MatchResponse(ReconcileResponse):
    """Stage 2's whole answer: the bindings, plus one relation per relational claim.

    Extends the routing contract rather than restating it, so the two rules that
    make within-batch reconciliation work -- no duplicate ``local_name``, and one
    shape per ``new_node.name`` group -- are enforced in the one place that owns
    them.

    It lives here rather than beside
    :class:`~memotron.caveman.models.ReconcileResponse` because ``relations``
    is this stage's own half of the contract and package D-B owns this module;
    the field is the matcher's answer, and the matcher is what this module is.

    ``relations`` defaults to empty so that a batch with no relational claim
    validates without the model having to send an empty array, and so that a
    model that omitted the array on a batch that HAS relational claims is
    rejected by the request-versus-response check with an error naming the
    claims it left untyped, rather than by a bare "field required".
    """

    relations: tuple[RelationSpec, ...] = ()


RECONCILE_SYSTEM = """\
You are a routing layer. You decide WHICH THING each name refers to. You do not
judge whether anything is important, interesting or worth keeping.

For each surface name below, decide whether it denotes the same real-world thing
as one of the candidate nodes offered for that name.

FIRST, BEFORE YOU ANSWER ANYTHING: read the whole list of surface names AND the
claims listed under each of them, then group the names that denote the same
real-world thing. Do the grouping first; then answer name by name, giving every
member of a group the SAME new_node.name.

Group on TWO things, and the second is the one that gets missed:

  How the names look. Names differing only by a qualifier, an abbreviation, or
  the phrase they happened to appear in are usually one thing -- "agent-memory",
  "the agent-memory MCP server" and "the agent-memory MCP deploy" are one server
  named three ways.

  WHAT THE CLAIMS SAY. Two names whose claims describe the same component -- its
  behaviour, its version, its configuration, its deployment, its failures -- are
  one thing EVEN WHEN THE NAMES SHARE ALMOST NO WORDS. You are shown every name's
  claims in this one request precisely so you can compare them. A component named
  by WHAT IT IS and the same component named by WHERE IT RUNS is the commonest
  case: one set of claims talks about the service's own behaviour, the other about
  the same service on the host or cluster it runs on, and they are one node.

TWO CAUTIONS, AND THEY POINT IN OPPOSITE DIRECTIONS. Do not apply one to the
other's question.

  BIND versus NEW -- when unsure, choose "new". A wrong bind welds two different
  things together and is nearly undetectable afterwards; a spurious new node is
  a merge that gets found and fixed later.

  GROUPING TWO NEW NAMES -- when unsure, GROUP THEM. This is the opposite advice
  and the reason is structural: a node that turns out to hold two topics is
  SPLIT by the dreamer, which is an operation this system performs routinely,
  while two nodes that should have been one sit there until some later pass
  happens to merge them. Splitting is cheap and designed for; failing to group
  is the expensive mistake.

"bind" MEANS ONE THING: this name denotes the same thing as one of the CANDIDATE
NODES offered for it, and you give that candidate's node_id. If no candidate was
offered for a name -- the list for it is empty, which is always true of the first
episode in a scope -- then "bind" is not available for that name at all and the
answer is "new".

TWO NAMES IN THIS BATCH THAT MEAN THE SAME THING IS NOT A BIND. A bind points at
a node that already exists; these names have no node yet. Answer "new" for BOTH
and give them the SAME new_node.name, character for character, with the same
type and the same gloss. That is how two names for one concept become one node,
and it is the whole reason you are shown every name at once.

Choose "type" from the scope vocabulary when one of them fits. Only propose a new
type when none does, and then use one lowercase word.

You must answer for EVERY surface name listed, exactly once each, using the name
spelled exactly as it is given.

YOU ALSO TYPE THE EDGES. Some of the claims state a belief BETWEEN two concepts,
and those are listed again on their own under RELATIONAL CLAIMS TO TYPE. Each one
becomes a typed edge, and every decision you make about one is one of three:

  A NEW NODE -- a name in this batch denotes a concept the scope does not hold
  yet. That is the "new" decision you have already made above.

  A NEW EDGE TYPE -- the claim asserts a KIND of belief that this scope has
  never recorded, so it needs a word of its own.

  AN EXISTING TYPE APPLIED -- the claim asserts a kind of belief the scope
  already has a word for, now holding between this pair. Reuse that word.

THE THIRD IS THE COMMONEST AND THE ONE THAT GETS MISSED. A scope with a hundred
edges and six types is one a reader can hold in their head; a scope with a
hundred types is a hundred words for the same handful of beliefs, and the ones
that say the same thing get merged away later anyway. So when a listed type
fits, reuse it. Coin a type only when none of them says what this claim says.\
"""


@dataclass(frozen=True)
class ReconcileOutcome:
    """What one batched adjudication did.

    ``bindings`` maps every distinct surface name in the batch to the node id it
    resolved to, in the order the names first appeared in the entries — so two
    names that converged are visible as two keys holding one value.
    """

    bindings: Mapping[str, str]
    created: tuple[str, ...]
    bound: tuple[str, ...]
    dirty: tuple[str, ...]
    pressure: int

    adjudications: tuple[BindingSpec, ...] = ()
    """The validated response, in the order the names were offered.

    Carried because a receipt is content-free by construction — digests plus a
    bounded ``detail`` — so an adjudication's ``reason``, and a new node's
    proposed ``gloss``, are recoverable from nowhere else. A reviewer asking
    "why did these two names converge" needs the model's own answer, and the
    alternative to returning it is putting claim-shaped text in a receipt.
    """

    offered: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    """Per surface name, the candidate node ids kNN put in front of the model.

    An empty tuple means the scope had nothing to offer, which is why a first
    ingest into a fresh scope is all ``new`` and says nothing about the model's
    willingness to bind. Distinguishing "offered nothing" from "offered and the
    model declined" is the difference between a working reconcile and one whose
    prompt is broken.
    """

    relations_created: tuple[str, ...] = ()
    """Relation ids for the edges this batch wrote for the first time."""

    relations_reinforced: tuple[str, ...] = ()
    """Relation ids for the edges this batch restated rather than duplicated.

    The number a reader of this outcome actually wants: a batch that reinforced
    four edges and created none is a scope whose beliefs are being confirmed,
    and a batch that creates on every ingest is a matcher that is not matching.
    Separate from ``relations_created`` rather than derivable from it, because
    ``upsert_relation`` returns the same record either way -- only this stage
    knows whether the triple existed before the call.

    **The two tuples are not disjoint.** A batch stating one belief twice creates
    the edge on the first claim and reinforces it on the second, so its id is in
    both: the fields classify what this batch's calls DID, and collapsing them
    into one membership test would lose the fact that a belief arrived and was
    confirmed in the same ingest.
    """

    edge_types_new: tuple[str, ...] = ()
    """The types this batch put in the scope's vocabulary for the first time.

    What the dreamer's ``M`` ceiling is measured against, and the direct read on
    whether the vocabulary line in the prompt is working: a batch answering with
    four new types against a scope that already had a fitting one is a prompt
    finding, not a graph finding.
    """


@dataclass(frozen=True)
class _Surface:
    """One distinct surface name, its claims, and the candidates offered for it."""

    name: str
    entries: tuple[LedgerEntry, ...]
    offered: tuple[str, ...]


@dataclass(frozen=True)
class _Candidate:
    """One node as the router is shown it: every search key, none of its lines.

    A record rather than a tuple because the row has five columns and the whole
    point of the alias column is that a reader — model or human — can tell which
    one is which.
    """

    name: str
    aliases: tuple[str, ...]
    type: str
    gloss: str

    def render(self, node_id: str) -> str:
        """One ``id | name | aliases | type | gloss`` row.

        Aliases are rendered in full. Truncating them would defeat the reason
        they are here: a name cut off mid-column is a name the router cannot
        recognise, which is the exact failure the column exists to prevent.
        """
        aliases = ", ".join(self.aliases) if self.aliases else NO_ALIASES
        return f"  {node_id} | {self.name} | {aliases} | {self.type} | {self.gloss}"


def _surface_names(entries: Sequence[LedgerEntry]) -> tuple[tuple[str, tuple[LedgerEntry, ...]], ...]:
    """Every distinct surface name in the batch, first-appearance ordered.

    Both ``subjects`` and ``objects`` are adjudicated. ``objects`` has no other
    consumer in the whole design, and it must be routed for the relation it
    states to become an edge at all: a LINK weight is the count of entries
    touching *two* nodes, so an entry whose object never resolved could never
    contribute one.
    """
    grouped: dict[str, dict[str, LedgerEntry]] = {}
    for entry in entries:
        for name in (*entry.subjects, *entry.objects):
            grouped.setdefault(name, {})[entry.entry_id] = entry
    return tuple((name, tuple(found.values())) for name, found in grouped.items())


def _relational(entries: Sequence[LedgerEntry]) -> tuple[LedgerEntry, ...]:
    """The claims that become edges, in batch order. ``claim_index`` is a position here.

    A claim is relational when the extractor called it one (``kind ==
    relation``) or when it carries ``objects`` -- an object is the other end of
    a stated relation and has no other consumer in the design, so a claim with
    one is about two concepts whatever kind word it arrived under.

    Batch order rather than any sort, because the position IS the wire
    identifier: the prompt numbers this list and the response points into it, so
    a reordering between the two halves would retype somebody else's claim.
    """
    return tuple(entry for entry in entries if entry.kind is ClaimKind.RELATION or entry.objects)


def _query_text(name: str, entries: Sequence[LedgerEntry]) -> str:
    """What a surface name is embedded as: the name, then the claims naming it.

    The claims are included because a bare surface name is often two words and
    embeds against almost nothing; they are the only description of the name that
    exists before it has been routed anywhere.
    """
    return f"{name}: " + " ".join(entry.claim for entry in entries)


def _gloss(ledger: LedgerStore, node_id: str) -> str:
    """A candidate's one-line description, derived from its earliest claims.

    An ``is`` claim is preferred because that is the kind of claim that says
    what a thing IS, which is exactly the question the router is answering.
    """
    entries = ledger.for_node(node_id, limit=GLOSS_ENTRY_SCAN)
    if not entries:
        return "no claims recorded yet"
    identity = next((entry for entry in entries if entry.kind is ClaimKind.IS), entries[0])
    return identity.claim[:GLOSS_LIMIT]


def _render_claims(entries: Sequence[LedgerEntry]) -> str:
    """The claims naming one surface name: kind, claim, and its identifiers.

    Deliberately not the entry's ``motive`` field, its confidence or its episode.
    The router needs to know what was said, not which persona selected it or how
    sure the extractor was — and rendering the motive here would break this
    stage's neutrality through the back door.
    """
    rendered: list[str] = []
    for entry in entries:
        rendered.append(f"     {entry.kind.value} {entry.claim}")
        if entry.identifiers:
            rendered.append(f"       identifiers: {', '.join(entry.identifiers)}")
    return "\n".join(rendered)


def _render_edge_types(counts: Mapping[str, int]) -> str:
    """``BLOCKED (3), DEPLOYS (2)`` -- the scope's vocabulary, most used first.

    The counts are the whole point of showing them. A type carrying thirty edges
    is this scope's established word for that belief and a type carrying one is a
    coinage that has not caught on, and a model told only the words cannot tell
    the two apart.
    """
    return ", ".join(f"{edge_type} ({count})" for edge_type, count in counts.items()) or NO_EDGE_TYPES


def _render_relational_claims(claims: Sequence[LedgerEntry]) -> str:
    """One block per claim that becomes an edge, labelled with its ``claim_index``.

    The names are repeated here rather than left to be found in the surface-name
    section above: that section is keyed by name and splits one claim across
    every name in it, and a model answering per claim needs the claim's own two
    ends beside the claim's own index.
    """
    if not claims:
        return "  (none -- no claim in this batch names two concepts)\n"
    blocks: list[str] = []
    for index, entry in enumerate(claims):
        names = ", ".join(f'"{name}"' for name in (*entry.subjects, *entry.objects))
        blocks.append(f"  claim_index {index}\n    CLAIM: {entry.claim}\n    NAMES IN THIS CLAIM: {names}\n")
    return "\n".join(blocks)


def _contract_block() -> str:
    """The literal JSON contract with one worked example, per the design."""
    return (
        "ANSWER SHAPE A -- candidates WERE offered. n-003 was offered for both names:\n"
        '{"bindings": [\n'
        '  {"local_name": "the JedAI Gateway", "decision": "bind", "node_id": "n-003", "new_node": null,\n'
        '   "reason": "same LiteLLM proxy the node\'s gloss describes"},\n'
        '  {"local_name": "the LiteLLM proxy", "decision": "bind", "node_id": "n-003", "new_node": null,\n'
        '   "reason": "same service under a second surface name"},\n'
        '  {"local_name": "the chart", "decision": "new", "node_id": null,\n'
        '   "new_node": {"name": "chart", "type": "artifact",\n'
        '                "gloss": "the Helm chart that deploys Memotron"},\n'
        '   "reason": "no candidate describes the deployment chart"}\n'
        "],\n"
        ' "relations": [\n'
        '  {"claim_index": 0, "source": "the chart", "target": "the JedAI Gateway",\n'
        '   "type": "DEPLOYS"}\n'
        "]}\n"
        "DEPLOYS was already in EDGE TYPES IN THIS SCOPE, so it is reused rather than\n"
        "coined. That is the commonest of the three decisions.\n"
        "\n"
        "ANSWER SHAPE B -- NO candidates were offered for any name, so nothing can be\n"
        "bound. The first two names still mean one thing, and they say so by sharing\n"
        "one new_node.name, type and gloss. No claim in that batch named two\n"
        "concepts, so relations is empty. This is the shape for a fresh scope:\n"
        '{"bindings": [\n'
        '  {"local_name": "the JedAI Gateway", "decision": "new", "node_id": null,\n'
        '   "new_node": {"name": "gateway", "type": "service",\n'
        '                "gloss": "the LiteLLM proxy fronting the JedAI models"},\n'
        '   "reason": "nothing was offered; this is the proxy itself"},\n'
        '  {"local_name": "the LiteLLM proxy", "decision": "new", "node_id": null,\n'
        '   "new_node": {"name": "gateway", "type": "service",\n'
        '                "gloss": "the LiteLLM proxy fronting the JedAI models"},\n'
        '   "reason": "the same service as the JedAI Gateway, second surface name"},\n'
        '  {"local_name": "the chart", "decision": "new", "node_id": null,\n'
        '   "new_node": {"name": "chart", "type": "artifact",\n'
        '                "gloss": "the Helm chart that deploys Memotron"},\n'
        '   "reason": "a separate thing from the proxy"}\n'
        '], "relations": []}\n'
        "Note the first two: identical name, identical type, identical gloss, and\n"
        'decision "new" on both. Never "bind" with a null node_id -- that is rejected.\n'
        "\n"
        "FIELD RULES\n"
        "  bindings     exactly one per surface name listed, no name twice.\n"
        "  local_name   the surface name, spelled exactly as given.\n"
        '  decision     "bind" or "new".\n'
        '  node_id      "bind": a candidate id offered FOR THAT NAME. "new": null.\n'
        '  new_node     "new": name, type, gloss. "bind": null.\n'
        "  name         at most 40 characters. Identical across names that converge.\n"
        "  type         one lowercase word, digits and hyphens allowed, 24 characters.\n"
        "  gloss        one line saying what the thing IS. At most 200 characters.\n"
        "  reason       one line, at most 300 characters.\n"
        "\n"
        "  relations    exactly one per claim_index listed under RELATIONAL CLAIMS TO\n"
        "               TYPE, no claim_index twice. Empty when that list is empty.\n"
        "  claim_index  the number shown beside the claim.\n"
        "  source       the surface name the belief is ABOUT.\n"
        "  target       the surface name at the other end.\n"
        "               Both are surface names from the list above, spelled exactly\n"
        "               as given -- NOT node ids, which a new name does not have\n"
        "               yet -- and they must be two different names.\n"
        "  type         UPPER_SNAKE: capital letters, digits and underscores, like\n"
        "               BLOCKED, DEPLOYS, FRONTS. Reuse one from EDGE TYPES IN THIS\n"
        "               SCOPE when it says what this claim says; coin one only when\n"
        "               none of them does.\n"
    )


def _render_prompt(
    *,
    scope: str,
    surfaces: Sequence[_Surface],
    candidates: Mapping[str, _Candidate],
    type_vocabulary: Sequence[str],
    edge_types: Mapping[str, int],
    claims: Sequence[LedgerEntry],
    max_facts: int,
) -> str:
    """The batched matching prompt. No motive, no rubric, no persona, no node facts.

    ``candidates`` is the union across the batch, deduped and id-ordered, so a
    node offered for three names is described once; each name then names the ids
    it was actually offered, which is what makes "bound to a node not offered for
    that name" a checkable rejection.

    ``edge_types`` and ``claims`` are the matcher's second half: the vocabulary a
    relational claim should reuse a word from, and the claims that need one. Both
    are rendered even when empty, because "this scope has no edges yet" and "the
    list was cut short" are different things to a model.

    A relation here names its two ends by SURFACE NAME and not by node id, which
    is the opposite of a dream answer -- a name this batch answers ``new`` has no
    node id yet. The shared format block states neither rule, so this prompt
    states its own once, at the point of use, in the section that asks for it.
    Nothing above it to contradict, and nothing to override: a prompt that
    contradicts itself is the defect class this branch has lost live runs to
    (``render.RELATION_TARGET_RULE`` says where the other rule lives).
    """
    vocabulary = ", ".join(type_vocabulary) if type_vocabulary else "(empty -- this scope has no nodes yet)"
    table = (
        "\n".join(candidate.render(node_id) for node_id, candidate in sorted(candidates.items()))
        or "  (none -- every name in this batch is new)"
    )

    blocks: list[str] = []
    for position, surface in enumerate(surfaces, 1):
        offered = ", ".join(surface.offered) if surface.offered else "none"
        blocks.append(
            f'{position}. "{surface.name}"\n'
            f"   CLAIMS:\n{_render_claims(surface.entries)}\n"
            f"   CANDIDATES OFFERED: {offered}\n"
        )

    return (
        # Every kind: this stage writes no fact text either. See ``extract``.
        format_prompt_block(max_facts=max_facts, kinds=tuple(FactKind)) + "\n"
        f"SCOPE: {scope}\n"
        f"SCOPE TYPE VOCABULARY: {vocabulary}\n"
        f"EDGE TYPES IN THIS SCOPE: {_render_edge_types(edge_types)}\n"
        "\n"
        "CANDIDATE NODES (id | name | aliases | type | gloss)\n"
        f"  aliases are other names ALREADY routed to that node; {NO_ALIASES} means none yet.\n"
        f"{table}\n"
        "\n"
        f"SURFACE NAMES TO ADJUDICATE ({len(surfaces)})\n"
        "\n" + "\n".join(blocks) + "\n"
        f"RELATIONAL CLAIMS TO TYPE ({len(claims)})\n"
        "  Each claim below states a belief between two concepts and becomes one\n"
        "  typed edge. Answer every claim_index exactly once, naming both ends by\n"
        '  SURFACE NAME as spelled above -- a name you answer "new" has no node id\n'
        "  yet, so this stage never names an end by id.\n"
        "\n"
        f"{_render_relational_claims(claims)}"
        "\n"
        f"{_contract_block()}"
        "\n"
        f"{CONTRACT_CLOSING_LINE}\n"
    )


def _batch_subject(entries: Sequence[LedgerEntry]) -> str:
    """What a batch decision is attributed to.

    A batch has no id of its own and may span episodes, so its subject is a
    digest of its entry ids — stable, so re-running the same batch attributes to
    the same subject, and short enough to read in a receipt stream.
    """
    return f"batch:{canonical_digest(sorted(entry.entry_id for entry in entries))[:16]}"


@dataclass(frozen=True)
class _RelationWrites:
    """What the relation half of one batch did. Three tuples of ids and types."""

    created: tuple[str, ...]
    reinforced: tuple[str, ...]
    new_types: tuple[str, ...]


def _relate(
    graph: GraphStore,
    *,
    specs: Sequence[RelationSpec],
    claims: Sequence[LedgerEntry],
    resolved: Mapping[str, str],
    known_types: Mapping[str, int],
    scope: str,
    now: datetime,
) -> _RelationWrites:
    """Write the model's typed edges, and report which were new.

    Directed as the model named it -- ``source`` is the concept the belief is
    about -- rather than canonically oriented like the co-occurrence weight this
    replaced. A typed belief has a subject, and reversing it to make a key sort
    would assert the opposite of the claim.

    Whether a call created or reinforced is known from the triples the scope held
    BEFORE the batch, read once, because ``upsert_relation`` returns the same
    record either way. The set grows as the batch writes, so two claims stating
    one belief create once and reinforce once rather than reporting two
    creations.

    A pair that resolved to one node writes nothing: see the module docstring.
    """
    existing = {relation.triple for relation in graph.relations(scope=scope)}
    vocabulary = set(known_types)
    created: list[str] = []
    reinforced: list[str] = []
    new_types: list[str] = []

    for spec in specs:
        source_id = resolved[spec.source]
        target_id = resolved[spec.target]
        if source_id == target_id:
            continue
        claim = claims[spec.claim_index]
        triple = (source_id, spec.type, target_id)
        was_held = triple in existing
        relation = graph.upsert_relation(
            scope=scope,
            source_id=source_id,
            target_id=target_id,
            type=spec.type,
            # A new edge needs its text and gets the ledger claim verbatim; a
            # restatement passes None for both, which the store reads as "keep
            # what is there". See the module docstring on reinforcement.
            claim=None if was_held else claim.claim,
            entry_ids=(claim.entry_id,),
            until=None,
            now=now,
        )
        if was_held:
            reinforced.append(relation.relation_id)
        else:
            created.append(relation.relation_id)
            existing.add(triple)
        if spec.type not in vocabulary:
            vocabulary.add(spec.type)
            new_types.append(spec.type)

    return _RelationWrites(
        created=_dedupe(created),
        reinforced=_dedupe(reinforced),
        new_types=tuple(new_types),
    )


def _relation_failures(
    response: MatchResponse,
    surfaces: Sequence[_Surface],
    claims: Sequence[LedgerEntry],
) -> tuple[list[str], list[str]]:
    """The relation half's rules. ``(exception errors, receipt detail parts)``.

    Four rules, and every one of them exists to keep a bad answer from reaching
    the write phase, where a raise would leave a partial route behind:

    * every relational claim is typed **exactly once** -- an untyped claim is a
      belief silently dropped, and a twice-typed one is two answers to one
      question with no rule for picking;
    * a ``claim_index`` points at a claim in **this** batch;
    * both ends are surface names **this batch adjudicated**, which is what makes
      them resolvable to node ids at all;
    * the two ends are different names, because ``Relation`` refuses a self-loop
      and a model naming one thing twice has not answered the question.

    The fourth is checked on NAMES and not on the ids they resolve to. Two
    different names resolving to one node is this stage's own convergence
    decision and is skipped at write time; one name given twice is the model's.
    """
    names = {surface.name for surface in surfaces}
    answered = Counter(spec.claim_index for spec in response.relations)

    unknown = tuple(sorted(index for index in answered if index >= len(claims)))
    missing = tuple(index for index in range(len(claims)) if index not in answered)
    repeated = tuple(sorted(index for index, count in answered.items() if count > 1))
    unnamed = tuple(
        (spec.claim_index, end) for spec in response.relations for end in (spec.source, spec.target) if end not in names
    )
    self_pointing = tuple(spec for spec in response.relations if spec.source == spec.target)

    errors: list[str] = []
    errors += [f"relations: claim_index {index} was not typed" for index in missing]
    errors += [f"relations: claim_index {index} was typed more than once" for index in repeated]
    errors += [f"relations: claim_index {index} is not a relational claim in this batch" for index in unknown]
    errors += [
        f"relations: claim_index {index} names {end!r}, which is not a surface name here" for index, end in unnamed
    ]
    errors += [f"relations: claim_index {spec.claim_index} points {spec.source!r} at itself" for spec in self_pointing]

    parts: list[str] = []
    if missing:
        parts.append(f"{len(missing)} relational claim(s) untyped")
    if repeated:
        parts.append(f"{len(repeated)} claim_index typed more than once")
    if unknown:
        parts.append(f"{len(unknown)} claim_index not in this batch")
    if unnamed:
        parts.append(f"{len(unnamed)} relation end(s) not a surface name")
    if self_pointing:
        parts.append(f"{len(self_pointing)} relation(s) naming one end twice")
    return errors, parts


def _contract_failures(
    response: MatchResponse,
    surfaces: Sequence[_Surface],
    claims: Sequence[LedgerEntry],
) -> tuple[tuple[str, ...], str] | None:
    """The rules that need the request. ``(exception errors, receipt detail)``.

    Two channels for the same reason as stage 1: the exception names the offending
    surface names for whoever is reading the failure, and the receipt detail
    carries only counts, so the audit stream stays content-free.

    One function for both halves of the answer, so a response that is wrong about
    a binding AND wrong about a relation produces one rejection with one receipt
    rather than a first failure hiding a second.
    """
    requested = {surface.name: surface.offered for surface in surfaces}
    answered = response.local_names()

    missing = tuple(name for name in requested if name not in set(answered))
    extra = tuple(name for name in answered if name not in requested)
    mis_bound = tuple(
        binding
        for binding in response.bindings
        if binding.decision == "bind"
        and binding.local_name in requested
        and binding.node_id not in requested[binding.local_name]
    )
    relation_errors, relation_parts = _relation_failures(response, surfaces, claims)
    if not missing and not extra and not mis_bound and not relation_errors:
        return None

    errors: list[str] = []
    errors += [f"bindings: no adjudication for surface name {name!r}" for name in missing]
    errors += [f"bindings: {name!r} was not a surface name in this batch" for name in extra]
    errors += [
        f"bindings: {binding.local_name!r} bound to {binding.node_id!r}, which was not offered for that name"
        for binding in mis_bound
    ]
    errors += relation_errors

    parts: list[str] = []
    if missing:
        parts.append(f"{len(missing)} surface name(s) unadjudicated")
    if extra:
        parts.append(f"{len(extra)} name(s) not in the batch")
    if mis_bound:
        parts.append(f"{len(mis_bound)} bind(s) to a node not offered for that name")
    parts += relation_parts
    return tuple(errors), "; ".join(parts)


def _dedupe(node_ids: Sequence[str]) -> tuple[str, ...]:
    """Order-preserving dedupe, so every returned id list is stable."""
    return tuple(dict.fromkeys(node_ids))


async def reconcile(
    *,
    entries: Sequence[LedgerEntry],
    scope: str,
    motive: CavemanMotive,
    graph: GraphStore,
    ledger: LedgerStore,
    receipts: ReceiptSink,
    embedder: EmbeddingTransport,
    transport: SynthesisTransport,
    now: datetime,
) -> ReconcileOutcome:
    """Match one batch of unbound entries onto node ids and typed edges. One LLM
    call, whatever the batch size.

    Order of operations, and it matters: every candidate is gathered and the
    response fully validated **before** anything is written, so a rejection
    leaves the graph and the ledger exactly as they were. Then nodes are created
    carrying their converged group as aliases, each bound name is recorded as an
    alias of the node it routed to, entries are bound, the touched set is marked
    dirty, and each relational claim's edge is created or reinforced. The accept
    receipts are emitted last, so a receipt saying a name was bound is never left
    behind by a write that failed.

    Raises :class:`~memotron.caveman.errors.OutOfContractResponse` with one
    ``RECONCILE_REJECTED`` receipt and **zero writes** when the response's name
    set does not match the request's, when a bind names a node that was not
    offered for that name, or when the relations do not type every relational
    claim exactly once with two known surface names.

    Raises :class:`ValueError` before the LLM call — so also with zero writes —
    on an empty batch, an entry from another scope, a blank surface name, or a
    relational claim no edge can carry verbatim. The last two are preconditions
    rather than dropped claims because the alias seam refuses a blank search key
    and ``Relation`` refuses an over-long claim: discovering either mid-write is
    the one way this stage could leave a partial route behind.

    A created node's ``ledger_key`` is minted by the store, not passed in here:
    see ``GraphStore.create_node``.
    """
    if not entries:
        raise ValueError("reconcile needs at least one ledger entry; an empty batch has nothing to route")
    foreign = _dedupe([entry.entry_id for entry in entries if entry.scope != scope])
    if foreign:
        raise ValueError(f"entries from another scope cannot be reconciled into {scope}: {', '.join(foreign)}")
    blank = _dedupe(
        [entry.entry_id for entry in entries if any(not name.strip() for name in (*entry.subjects, *entry.objects))]
    )
    if blank:
        raise ValueError(
            "a blank surface name has nothing to route and cannot be recorded as an alias; "
            f"offending entries: {', '.join(blank)}"
        )
    claims = _relational(entries)

    vectors: dict[str, tuple[float, ...]] = {}
    surfaces: list[_Surface] = []
    candidates: dict[str, _Candidate] = {}
    for name, named_entries in _surface_names(entries):
        vector = tuple(embedder.embed(_query_text(name, named_entries)))
        vectors[name] = vector
        nearest = graph.knn(scope=scope, vector=vector, k=CANDIDATE_K)
        surfaces.append(_Surface(name=name, entries=named_entries, offered=tuple(node.node_id for node, _ in nearest)))
        for node, _ in nearest:
            if node.node_id not in candidates:
                candidates[node.node_id] = _Candidate(
                    name=node.name,
                    aliases=node.aliases,
                    type=node.type,
                    gloss=_gloss(ledger, node.node_id),
                )

    known_types = graph.edge_types(scope=scope)
    user_prompt = _render_prompt(
        scope=scope,
        surfaces=surfaces,
        candidates=candidates,
        type_vocabulary=graph.type_vocabulary(scope=scope),
        edge_types=known_types,
        claims=claims,
        max_facts=motive.max_facts_per_node,
    )
    subject = _batch_subject(entries)
    response = await strict_json_call(
        transport=transport,
        system_prompt=RECONCILE_SYSTEM,
        user_prompt=user_prompt,
        response_model=MatchResponse,
        source=RECONCILE_SOURCE,
        receipts=receipts,
        scope=scope,
        subject=subject,
        reject_op=ReceiptOp.RECONCILE_REJECTED,
        now=now,
    )

    inputs_digest = canonical_digest({"system": RECONCILE_SYSTEM, "user": user_prompt})
    failures = _contract_failures(response, surfaces, claims)
    if failures is not None:
        errors, detail = failures
        outputs_digest = canonical_digest(response.model_dump(mode="json"))
        receipts.emit(
            Receipt(
                receipt_id=new_receipt_id(),
                op=ReceiptOp.RECONCILE_REJECTED,
                ts=now,
                scope=scope,
                subject=subject,
                inputs_digest=inputs_digest,
                outputs_digest=outputs_digest,
                detail=detail,
            )
        )
        raise OutOfContractResponse(source=RECONCILE_SOURCE, errors=errors, raw_digest=outputs_digest[:16])

    adjudication = {binding.local_name: binding for binding in response.bindings}
    ordered = [adjudication[surface.name] for surface in surfaces]

    # One node per case-folded new_node name group -- this is how two surface
    # names converging on one concept becomes one node rather than two.
    proposals: dict[str, NewNodeSpec] = {}
    grouped_names: dict[str, list[str]] = {}
    for binding in ordered:
        spec = binding.new_node
        if spec is None:
            continue
        key = spec.name.casefold()
        proposals.setdefault(key, spec)
        grouped_names.setdefault(key, []).append(binding.local_name)

    resolved: dict[str, str] = {}
    created: list[str] = []
    for key, spec in proposals.items():
        group = grouped_names[key]
        node = graph.create_node(
            scope=scope,
            name=spec.name,
            type=spec.type,
            facts=(),
            embedding=vectors[group[0]],
            now=now,
            aliases=group,
        )
        created.append(node.node_id)
        for local_name in group:
            resolved[local_name] = node.node_id

    bound: list[str] = []
    for binding in ordered:
        if binding.decision == "bind" and binding.node_id is not None:
            resolved[binding.local_name] = binding.node_id
            bound.append(binding.node_id)
            graph.add_aliases(binding.node_id, [binding.local_name])

    for entry in entries:
        entry_nodes = _dedupe([resolved[name] for name in (*entry.subjects, *entry.objects)])
        ledger.bind_nodes(entry.entry_id, entry_nodes)

    touched = _dedupe([*created, *bound])
    graph.mark_dirty(touched)
    written = _relate(
        graph,
        specs=response.relations,
        claims=claims,
        resolved=resolved,
        known_types=known_types,
        scope=scope,
        now=now,
    )

    offered_counts = {surface.name: len(surface.offered) for surface in surfaces}
    for binding in ordered:
        node_id = resolved[binding.local_name]
        offered_count = offered_counts[binding.local_name]
        is_new = binding.decision == "new"
        receipts.emit(
            Receipt(
                receipt_id=new_receipt_id(),
                op=ReceiptOp.RECONCILE_NEW if is_new else ReceiptOp.RECONCILE_BOUND,
                ts=now,
                scope=scope,
                subject=node_id,
                inputs_digest=inputs_digest,
                outputs_digest=canonical_digest(binding.model_dump(mode="json")),
                detail=(
                    f"decision={binding.decision} candidates_offered={offered_count} "
                    f"names_in_group={sum(1 for name, resolved_id in resolved.items() if resolved_id == node_id)}"
                ),
            )
        )

    return ReconcileOutcome(
        bindings=dict(resolved),
        created=tuple(created),
        bound=_dedupe(bound),
        dirty=touched,
        pressure=max(0, graph.count(scope=scope) - motive.max_nodes),
        adjudications=tuple(ordered),
        offered={surface.name: surface.offered for surface in surfaces},
        relations_created=written.created,
        relations_reinforced=written.reinforced,
        edge_types_new=written.new_types,
    )
