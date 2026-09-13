#!/usr/bin/env python
"""#251: the live proof that caveman memory works end to end.

Fourteen turns of two conversations go in. What comes out is a bounded graph of
concepts, each holding plain-word FACTS with their evidence, joined by TYPED
EDGES that each carry a claim of their own -- over an append-only claim ledger
and an append-only event journal that can rebuild the whole thing. Every step in
between is a real call to the JedAI Gateway.

.. rubric:: Compact, not cryptic

There is no symbol alphabet anywhere in this system, and no grammar to teach.
The rendering is printable ASCII words, brackets, commas and colons; the
prompts ask for text that is compact and information-dense, with identifiers
verbatim, and ask the model to count nothing. Four characters that used to be
FORMAT -- a separator, a relation arrow, a refutation marker, an approximation
marker -- are gone, and check 15 at the bottom asserts that none of them reaches
a model or an agent. That is the whole of what "caveman" means here: dense, not
decorated.

.. rubric:: There is one mode, and it is live

No ``--live`` flag, no offline branch, no scripted fallback, no relaxed model.
A real run is always live: if the platform is broken the platform gets fixed,
never the run. With ``LITELLM_API_KEY`` absent this exits **2** with a one-line
setup error and makes no call at all; with it present every LLM call below hits
the gateway.

The plumbing is already proved hermetically — ``tests/test_caveman_*.py`` drive
every stage through a scripted transport. What only a live run can prove is
whether the PROMPTS work: whether the model extracts the claims that matter,
whether it recognises that "the JedAI Gateway" and "the LiteLLM proxy" are one
thing, whether it compresses to the grammar without being coaxed, and whether it
delivers a forced merge when the node budget leaves it no choice. That is what
the assertions at the bottom check, and a failure in one of them is a real
finding about a prompt rather than a flaky test.

.. rubric:: The call budget, which is a design property and not an accident

Every chat call is counted and printed, so the budget is observed rather than
claimed:

* EXTRACT — one call, whatever the episode's length.
* RECONCILE — one call for the whole batch. Seeing every surface name at once is
  what lets two names converge on one node.
* DREAM incremental — one call per dirty node.
* DREAM global — one call per pass, the forced-merge slate computed before it.
* READ / BRIEF / NODE / NEIGHBORS / EXPLAIN / REPLAY — **zero**. Retrieval and
  the proof are both deterministic; the agency sits around the call. An
  exact-seeded read does not even embed its query.

.. rubric:: What this demo drives

Only :class:`~memotron.caveman.pipeline.CavemanMemory`, the composition root:
``ingest``, ``dream``, ``read``, ``brief``, ``node``, ``neighbors``, ``explain``
and ``replay``. It never reaches past that seam into a stage function, which is
itself part of what is being shown — the root's surface is enough to run the
system and enough to audit it. The MCP server in
``examples/caveman_mcp_server.py`` exposes exactly these, one tool each.

.. rubric:: Two episodes, because a memory that only grows is not a memory

The second episode (:data:`TURNS_TWO`) goes into the same scope a day later and
restates three things the first one recorded. What must happen is
REINFORCEMENT -- the existing fact and the existing edge gain a ledger entry,
``last_seen`` moves, the render shows ``(x2)``, and ranking weights them
higher -- rather than a second record saying the same thing. Its third turn moves
the chat default, which supersedes an attribute ACROSS episodes: the new value is
what a read shows, and the old one stays recoverable through ``explain`` and
nowhere else.

.. rubric:: The ids are carried back on purpose

Every rendered block's header ends in ``[n-...]``, and three of the calls above
take one: ``node`` re-reads that concept whole, ``neighbors`` lists its typed
edges with the id on the far end of each, and ``explain`` gives the provenance a
bounded read structurally cannot. So a read is not a dead end -- it is an entry
point into a graph an agent can walk, with no further model call. Check 16
asserts every render carries an id.

.. rubric:: Compression is proved, not trusted

Every graph mutation in this run goes through a journal: one ``DreamEvent`` in
the ledger per mutation, with the node and relation content before and after.
The REPLAY PROOF section applies that journal to an empty graph -- no model call,
no embedding -- and compares the two by a digest over what they ASSERT. Equal
digests are the proof; the section prints both, and check 20 fails the run if
they differ.

.. rubric:: Both ceilings, not just N

``N`` bounds how many concepts a scope holds and ``M`` bounds how many kinds of
belief it holds between them. Section 6a sets ``M`` one below the scope's own
vocabulary, which mandates exactly one ``rename_edge_type`` -- the doomed type
decided by arithmetic, what it folds into decided by the model. Section 6b then
forces ``N`` down to three.

That order is not cosmetic. A merge re-points the edges of what it absorbs and
drops the self-loops that makes, so by the time the scope is three nodes its
vocabulary has collapsed to one type and there is nothing left for ``M`` to bite
on. The first run in this order measured it: the compaction ran after the squeeze
and found a one-word vocabulary.

.. rubric:: The section that answers "how would I search this"

**HOW AN AGENT SEARCHES THIS** runs between the free global pass and the forced
one, against the nine-node graph rather than the three-node one, and that
placement is the point. A scope that fits inside one read gives a query nothing
to decide: an earlier run put this section after the squeeze and all four
searches returned the identical three blocks, which showed the plumbing and
nothing about seeding. Five calls, printed verbatim: a session-start ``brief()``
with no query at all, an identifier query, a name query, a task query — each
with its :class:`~memotron.caveman.read.SeedHit` list, so the REASON a node
came back is on the page next to the node, and each with its duplicate count and
its ``READ_EMITTED`` receipt detail — and one ``explain()`` for the deep read a
``more:`` footer names. Two of those queries are answered out of exact indexes
rather than by embedding similarity, which is the half of retrieval an
embedding-only read cannot do: ``#245`` and ``#246`` embed nearly identically
and a searcher who types an identifier means that identifier.

Each seed row also says KEPT or DROPPED. ``read_k`` is a cap and not a filter,
so a query in a scope this size used to fill every slot whatever the
measurements said — six neighbours at 0.13-0.17 for ``read('C4')`` — and 1-hop
expansion from those reached the rest, which is how a query read came back
holding the whole scope. ``motive.knn_min_similarity`` floors the measured half
at 0.25 and the DROPPED rows are what it refused; an exact hit is never floored.
And ``rank.EXACT_FLOOR`` puts the node the query NAMED first, ahead of the
scope's other constraints, so the answer to "tell me about C4" no longer arrives
third (#251 amendment C).

One read is then repeated AFTER the squeeze — ``read('C4')``, on a scope two
merges smaller — because a merge is where a search key is most likely to be
lost. ``merge_nodes`` unions the absorbed node's name and aliases into the
survivor's, so the name a reader knows must still be an EXACT hit on whichever
node now holds the answer.

Run it: ``uv run examples/caveman_demo.py``
"""

from __future__ import annotations

import asyncio
import math
import os
import re
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from memotron.caveman.dream import GlobalOutcome, compaction_targets, merge_reason
from memotron.caveman.errors import CavemanError
from memotron.caveman.explain import event_summary
from memotron.caveman.gateway import CavemanChatTransport
from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    EDGE_TYPE_PATTERN,
    ClaimKind,
    Episode,
    FactKind,
    LedgerEntry,
    Node,
    Receipt,
    ReceiptOp,
    Relation,
    Turn,
)
from memotron.caveman.motive import CavemanMotive, engineering_motive, motive_digest
from memotron.caveman.pipeline import CavemanMemory, UtcClock
from memotron.caveman.pressure import ValuedNode, embedding_similarity, merge_slate, node_value, pressure
from memotron.caveman.rank import CONSTRAINT_FLOOR, EXACT_FLOOR
from memotron.caveman.read import ReadResult, SeedHit
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.render import (
    FACT_LABEL,
    FOOTER_PREFIX,
    render_fact,
    render_header,
    render_relation,
    validate_fact_text,
)
from memotron.caveman.render import footer as render_footer
from memotron.caveman.replay import ReplayProof
from memotron.caveman.replay import replay as replay_scope
from memotron.caveman.tokens import NODE_HEADER_TOKENS, estimate_tokens, scope_budget
from memotron.embedding import OpenAICompatibleEmbeddingTransport
from memotron.gateway import GATEWAY_API_KEY_ENV
from memotron.runtime import load_env_file

CHAT_MODEL = "claude-sonnet-4-6"
"""The undated alias the repo's live simulation names (``simulation.py:71``).

Undated on purpose, and turn 10 of the episode below states the rule: a dated
pin goes stale and the gateway starts refusing it.
"""

EMBEDDING_MODEL = "text-embedding-3"
"""3072 dimensions, and selected EXPLICITLY.

``DEFAULT_GATEWAY_EMBEDDING_MODEL`` is an alias the repo happens to know, not a
default any transport applies — ``OpenAICompatibleEmbeddingTransport`` has no
default model at all. Turn 8 of the episode states this as a fact about the
platform, and this line is that fact being obeyed.
"""

EPISODE_ID = "ep-251-01"
EPISODE_TWO_ID = "ep-251-02"
SCOPE = "repo:jedai/memotron"
OCCURRED_AT = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
OCCURRED_AT_TWO = datetime(2026, 9, 9, 10, 15, tzinfo=UTC)

FORMAT_CHARACTERS_THE_DESIGN_REMOVED: tuple[tuple[str, str], ...] = (
    ("\u00b7", "middle dot"),
    ("\u2192", "rightwards arrow"),
    ("\u00d7", "multiplication sign"),
    ("~", "tilde"),
)
"""Characters the rendering and the prompts must not contain, with their names.

Written as escapes so this file itself stays clean under the amendment's own
``rg -n`` check. All four were FORMAT in an earlier design -- a separator, a
relation arrow, a refutation marker, an approximation marker -- and each cost a
legend the model had to be taught. Amendment D's rule is that the format adds
nothing outside printable ASCII words, brackets, commas and colons.

The check that reads this is over every rendered block this run captured AND
every prompt it sent, which between them are the whole of what a model or an
agent ever receives. The narration in between is for a person, and is written
without any of the four so a reader can grep the transcript and get only real
hits.
"""

FORCED_MAX_NODES = 3
"""``N`` for the forced pass. Small enough that the bound has to bite."""

MAX_FORCED_PASSES = 6
"""How many forced passes step 6 will run before calling the bound unenforceable.

Not a retry budget — every pass is a legitimate periodic reorganisation, and each
one is asserted to strictly shrink the scope, so this can only be reached if a
pass stops making progress. A forced slate is ``pressure`` DISJOINT pairs, so it
needs ``2 * pressure`` distinct nodes and each pair removes exactly one node:
one pass can remove at most ``count // 2``, which makes reaching a small ``N``
from a large scope a halving sequence rather than a single call. Six passes takes
64 nodes to 1.
"""

IDENTIFIER_QUERY = "#246"
"""The identifier query. In the episode verbatim, which is what makes it indexable."""

NAME_QUERY = "C4"
"""The name query: the name I know, which is not the name the dreamer kept."""

TOOL_COUNT = "31"
"""The corrected count from turn 9. What a search for :data:`NAME_QUERY` must reach."""

SEARCHES: tuple[tuple[str, str], ...] = (
    ("b", IDENTIFIER_QUERY),
    ("c", NAME_QUERY),
    ("d", "the gateway refuses my Host header"),
)
"""The three queries a reader actually types, one per retrieval mechanism.

``#246`` is the case an embedding-only read cannot do: ``#245`` and ``#246``
differ by one character and embed nearly identically, so similarity ranks them
almost equally and a searcher who typed one of them means that one. ``C4`` is
the name I know rather than the name the dreamer kept, which is what a node's
recorded aliases are for. The third is neither a name nor an identifier — it is
a task, in my own words, and measured similarity is the right tool for it.
"""

SEARCH_NOTES: dict[str, str] = {
    "b": (
        "An IDENTIFIER. The ledger indexes every identifier its entries carry, so this is an\n"
        "  exact hit on the nodes whose evidence names #246 — not a near-neighbour of it."
    ),
    "c": (
        "A NAME. Reconcile recorded every surface name it routed as an alias, so a search by\n"
        "  the name I happen to know reaches the node the dreamer named something else."
    ),
    "d": (
        "A TASK, in my own words. No identifier, no name — nothing to match exactly, which is\n"
        "  what embedding similarity is for. The seeds below say 'knn' and that is correct."
    ),
}

NODE_ID_IN_BRACKETS = re.compile(r"\[n-\d+\]")
"""A node id as the RENDERING carries it. What the traversal calls take.

A regex over the rendered text rather than a check against the graph, because
what is being asserted is that the id reached the READER: an agent holding a
block with no id can read the answer and cannot follow it anywhere.
"""

LIVE_STAGES: tuple[str, ...] = ("EXTRACT", "RECONCILE", "DREAM-NODE", "DREAM-GLOBAL")
"""Every stage that calls the model. Each one must be reached at least once."""

MINIMUM_LIVE_CALLS = 12
"""The floor on this run's live call count, from the path it takes.

Two episodes (2 extract + 2 reconcile), an incremental dream of each episode's
dirty nodes (4 or more, then 2 or more), a free global pass, at least one forced
pass and one compaction pass. Twelve is comfortably under what the path costs; it
is here so a run that silently stopped calling a stage fails rather than passing
a shorter version of the demo.
"""

MERGE_REASONS: tuple[str, ...] = ("linked x", "same turn x", "nearest by embedding")
"""The three phrases `dream.merge_reason` can produce, in `pressure`'s own order.

A forced mandate must say on what evidence its peer was chosen, because the
three are not equally strong: a shared LINK or a shared turn is a record, and
"nearest by embedding" is only a resemblance. A mandate carrying none of these
is a prompt that stopped telling the model which it had.
"""

RULE = "=" * 100


class DemoAssertionError(AssertionError):
    """An assertion this demo makes about a live run. Exits 1, never 0."""


def _check(condition: bool, message: str) -> None:
    """Assert, loudly and by name. Never ``assert``, which ``-O`` would remove."""
    if not condition:
        raise DemoAssertionError(message)


# ============================================================== the episode


TURNS: tuple[tuple[str, str], ...] = (
    (
        "Logan",
        "One rule before we touch anything: every real run goes through the JedAI Gateway. "
        "No hermetic mode, no local stub, not even for a quick check. If the platform is broken "
        "we fix the platform — we don't downgrade the run.",
    ),
    (
        "Priya",
        "Agreed. And for whoever reads this later: the JedAI Gateway is a LiteLLM proxy sitting "
        "in front of the JedAI models. It isn't a model itself, it's the thing that routes to them.",
    ),
    (
        "agent",
        "Confirmed against the deploy history. Before #245 the gateway refused our Host header "
        "outright, so the agent-memory MCP server couldn't reach it at all. #245 fixed the Host rewrite.",
    ),
    (
        "Priya",
        "Right, and the same PR fixed the other half — the chart was deploying agent-memory from "
        "the wrong image tag. The chart is where the agent-memory MCP deploy lives in the first "
        "place; that came in with #240.",
    ),
    (
        "Logan",
        "There's still something flaky underneath all that. Roughly one call in twenty loses "
        "session affinity and lands on a cold pod. I've never got it to reproduce on demand.",
    ),
    (
        "agent",
        "That's the failure the probe in #246 names. It asserts the intermittent affinity loss "
        "rather than pretending it's fixed, which is why the probe is committed.",
    ),
    (
        "Priya",
        "On capability — C4 is returning 24 tools right now, so I'd call the surface basically complete.",
    ),
    (
        "agent",
        "Two more worth recording. The chat default on the gateway is claude-haiku-4-5. The "
        "embedding alias is text-embedding-3 at 3072 dimensions, and it has to be selected "
        "explicitly — there's no default embedding model applied for you.",
    ),
    (
        "Logan",
        "Correction on Priya's number. I counted again after #248 landed: the C4 memory server "
        "returns 31 tools, not 24. 24 was the count before #248.",
    ),
    (
        "Priya",
        "Last one, and treat it as a rule: we only ever name undated aliases on the LiteLLM proxy "
        "— claude-haiku-4-5, never a dated pin. A dated alias goes stale and the gateway starts "
        "refusing it.",
    ),
)

#: How a surface name is recognised as belonging to one of the two concepts the
#: episode names twice. The live model chooses its own surface strings — it is
#: told to use the episode's own words, not a fixed vocabulary — so the
#: convergence assertion matches on substance rather than on an exact string
#: nobody promised. A group that fails to collapse to one node is a real finding
#: about the reconcile prompt, which is why this is a keyword match and not a
#: lookup that could quietly miss.
CONCEPT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "the gateway": ("gateway", "litellm"),
    "the agent-memory server": ("agent-memory", "agent memory"),
}
"""The concepts the episode names more than once AND states the identity of.

Deliberately narrow, and narrower than the design document's requirement table.
Keywords that were here and should not have been: ``c4`` matched a bare surface
name of "C4", a CLUSTER in this episode and a genuinely different thing from a
server deployed on it; ``proxy`` and ``mcp server`` are similarly generic. An
over-broad set here fails the run and reads as a reconcile defect, which is the
worst failure mode an assertion has.

``memory server`` is also gone, and that one is a PLAN DEVIATION rather than a
sloppy keyword — see :data:`UNSUPPORTED_CONVERGENCE`.
"""

UNSUPPORTED_CONVERGENCE = ("memory server", "c4 memory")
"""Surface names the design claims converge, which this episode does not support.

The requirement table pairs *the agent-memory MCP server* (turn 3) with *the C4
memory server* (turn 9). The episode never states that they are the same thing:
turn 3 is about Host-header connectivity and #245, turns 7 and 9 about tool
counts and #248, sharing no vocabulary and no identifier. A reader who knows this
repository supplies the link; the router is shown only the claims naming each
name, and it is told -- correctly -- to prefer "new" when unsure. Converging them
would be a guess, and a wrong bind is the one mistake stage 2 exists to avoid.

Three reconcile prompt fixes were tried before concluding this, and the first two
fixed real defects (see the git log for #251). The third -- group on what the
claims SAY, not only on how the names look -- did not move this pair. Teaching
the router that these two particular strings are one thing would fit the prompt
to one episode.

So the run REPORTS this rather than asserting it, every time, and the remedy is
one clause of episode text: turn 9 naming the server, e.g. "the C4 memory server
-- the agent-memory MCP server on C4". The plan says to author that text
verbatim, so amending it is a reviewer's decision.
"""


TURNS_TWO: tuple[tuple[str, str], ...] = (
    (
        "Priya",
        "Quick recap for the new folks: the JedAI Gateway is still the LiteLLM proxy in front of "
        "the JedAI models, and every real run goes through it. Nothing has changed there.",
    ),
    (
        "agent",
        "Confirmed again from today's deploy logs: the agent-memory MCP server reaches the gateway "
        "fine since #245 fixed the Host header rewrite.",
    ),
    (
        "Logan",
        "One update. The chat default on the gateway moved to claude-sonnet-4-6 this morning; "
        "claude-haiku-4-5 is no longer the default, though it still resolves.",
    ),
    (
        "Priya",
        "And the session-affinity flake hit again in the load test, about one in twenty calls, "
        "same as before. Still not reproducible on demand.",
    ),
)
"""The second episode, authored verbatim by the plan (#251 amendment D, D-E addendum).

Four turns, one scope, one day later, and every one of them is here to exercise
something the first episode structurally cannot:

* turns 1 and 2 RESTATE facts and an edge episode one already recorded, so the
  matcher reinforces rather than duplicating -- evidence 2, and ``(x2)`` in the
  render;
* turn 3 supersedes episode one's ``chat default`` ACROSS episodes, so the old
  value has to leave the node and stay reachable through ``explain``;
* turn 4 restates the ``unsure`` affinity flake, which is the reinforcement case
  for a fact nobody has been able to confirm.
"""

CHAT_DEFAULT_WAS = "claude-haiku-4-5"
"""Episode one's chat default. Episode two supersedes it; ``explain`` must still hold it."""

CHAT_DEFAULT_NOW = "claude-sonnet-4-6"
"""Episode two's chat default. A read must show this one and not the old one as the default."""


def build_episode() -> Episode:
    """The ten turns above, as the immutable input to stage 1."""
    return Episode(
        episode_id=EPISODE_ID,
        scope=SCOPE,
        occurred_at=OCCURRED_AT,
        turns=tuple(Turn(index=index, speaker=speaker, text=text) for index, (speaker, text) in enumerate(TURNS, 1)),
    )


def build_episode_two() -> Episode:
    """:data:`TURNS_TWO`, into the SAME scope one day later."""
    return Episode(
        episode_id=EPISODE_TWO_ID,
        scope=SCOPE,
        occurred_at=OCCURRED_AT_TWO,
        turns=tuple(
            Turn(index=index, speaker=speaker, text=text) for index, (speaker, text) in enumerate(TURNS_TWO, 1)
        ),
    )


# ================================================== the counted chat transport


@dataclass
class ChatCall:
    """One live exchange: the budget row, and the prompt text itself.

    The prompts are kept because one of the run's assertions is about them --
    no character the design removed may appear in anything a model reads -- and
    a check over a length cannot see a character.
    """

    stage: str
    seconds: float
    prompt: str
    system_prompt: str

    @property
    def prompt_chars(self) -> int:
        return len(self.prompt)

    response_chars: int = 0


class CountedChat:
    """Wraps :class:`CavemanChatTransport` and counts what it was asked.

    Composition, not a second transport: it holds one and forwards to it. The
    point is that "EXTRACT is one call" and "READ is zero calls" become observed
    facts in the output rather than claims in a docstring — and a stage that
    silently grew a second call would show up here as a number that changed.

    The stage label is derived from the system prompt's own opening, because the
    transport is not told which stage is calling it and should not be.
    """

    def __init__(self, inner: CavemanChatTransport) -> None:
        self._inner = inner
        self.calls: list[ChatCall] = []

    @property
    def identifier(self) -> str:
        return self._inner.identifier

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        started = time.monotonic()
        answer = await self._inner.synthesize(prompt, system_prompt=system_prompt)
        self.calls.append(
            ChatCall(
                stage=_stage_of(system_prompt),
                seconds=time.monotonic() - started,
                prompt=prompt,
                system_prompt=system_prompt,
                response_chars=len(answer),
            )
        )
        return answer

    def since(self, mark: int) -> list[ChatCall]:
        """The calls made after ``mark``, which is a previous ``len(self.calls)``."""
        return self.calls[mark:]


def _stage_of(system_prompt: str) -> str:
    """Which stage a system prompt belongs to, by its own opening words."""
    head = system_prompt[:200].lower()
    if "routing layer" in head:
        return "RECONCILE"
    if "whole scope" in head:
        return "DREAM-GLOBAL"
    if "one node" in head:
        return "DREAM-NODE"
    return "EXTRACT"


class CountedEmbed:
    """Wraps the embedding transport and counts what it was asked to embed.

    The same composition as :class:`CountedChat`, for the same reason and for one
    specific claim: **an exact-seeded read computes no vector at all.**
    ``read._seed_the_read`` skips the embedder entirely once alias and identifier
    hits have filled ``read_k``, and a counter is the only way that shows up in
    the output as an observed fact rather than a docstring's promise.

    It also makes the cost of the other stages visible — reconcile embeds each
    surface name, a dream embeds each node it writes — so "a read is cheap" is a
    number next to those numbers.
    """

    def __init__(self, inner: OpenAICompatibleEmbeddingTransport) -> None:
        self._inner = inner
        self.count = 0

    @property
    def identifier(self) -> str:
        return self._inner.identifier

    def embed(self, text: str) -> list[float]:
        self.count += 1
        return self._inner.embed(text)


# ====================================================================== printing


def rule(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


RULE_WORDINGS: tuple[str, ...] = ("hermetic", "dated")
"""Two lowercase fragments of the two rules the episode states.

Turn 1: "every real run goes through the JedAI Gateway. No hermetic mode." Turn
10: "we only ever name undated aliases ... never a dated pin." A live model
phrases them its own way, so a check on the whole sentence would be a check on
the wording; these two words are the load-bearing part of each, and both appear
in the episode verbatim.

The KIND those facts carry is asserted too, since #251 amendment D package D-C
gave the dreamer named fields to answer with: a rule is a ``rule``, not an
``attribute`` that happens to read like one, and only the kind puts it under
``rank.CONSTRAINT_FLOOR``. See live check 3.
"""


def _texts(node: Node) -> tuple[str, ...]:
    """A node's fact TEXT. What a check reads when it is about content."""
    return tuple(fact.text for fact in node.facts)


def _is_rule(fact_or_line: str) -> bool:
    """Whether a RENDERED line is a rule.

    Reads the label rather than a stored kind, because the checks below run over
    what a READ emitted and a read hands back text. The label is
    ``render.FACT_LABEL``'s, so this and the renderer cannot disagree about what
    a rule looks like.
    """
    return fact_or_line.startswith(f"{FACT_LABEL[FactKind.RULE]}: ")


def _rules_of(node: Node) -> tuple[str, ...]:
    """The node's rule facts, as their rendered lines."""
    return tuple(render_fact(fact) for fact in node.facts if fact.kind is FactKind.RULE)


def _kind_label(kind: ClaimKind) -> str:
    """The claim kind as a word. There is no alphabet to decode any more."""
    return kind.value


def print_calls(chat: CountedChat, mark: int) -> None:
    """The live calls this step made, one line each."""
    made = chat.since(mark)
    if not made:
        print("  LIVE CALLS: none — this step makes no LLM call at all.")
        return
    print(f"  LIVE CALLS: {len(made)}")
    for call in made:
        print(
            f"    {call.stage:<12} {call.seconds:6.2f}s  "
            f"prompt {call.prompt_chars:>6,} chars -> response {call.response_chars:>5,} chars"
        )


def print_entry(entry: LedgerEntry, *, indent: str = "    ") -> None:
    print(
        f"{indent}{entry.entry_id}  {_kind_label(entry.kind):<14} mode={entry.claim_mode.value:<12} "
        f"conf={entry.confidence:.2f}  turns={list(entry.turns)}"
    )
    print(f"{indent}  claim      : {entry.claim}")
    print(f"{indent}  subjects   : {list(entry.subjects)}")
    if entry.objects:
        print(f"{indent}  objects    : {list(entry.objects)}")
    if entry.identifiers:
        print(f"{indent}  identifiers: {list(entry.identifiers)}")
    if entry.supersedes:
        print(f"{indent}  supersedes : {entry.supersedes}   <-- the in-episode correction, resolved to its sibling")
    if entry.node_ids:
        print(f"{indent}  node_ids   : {list(entry.node_ids)}")


def print_node(node: Node, *, entries: int, relations: Sequence[Relation] = (), indent: str = "    ") -> None:
    """One node: its header, its aliases, its facts, then its edges.

    The aliases are printed because they are a SEARCH KEY, not decoration. Every
    surface name reconcile routed here and every name a merge absorbed is one
    more spelling that reaches this node exactly, and a graph dump that hides
    them makes the read section below look like magic.

    The header already carries the node id, the ``as of`` date and the entry
    count, so what is added here is the bookkeeping a reader never sees: the
    dirty flag, the read count, and how many facts the node holds.
    """
    print(
        f"{indent}{render_header(node, entries=entries)}"
        f"   dirty={node.dirty} reads={node.read_count} facts={len(node.facts)}"
    )
    for fact in node.facts:
        print(f"{indent}  {render_fact(fact)}")
    if not node.facts:
        print(f"{indent}  (no facts yet -- reconcile created it, the dreamer writes its first fact set)")
    for relation in relations:
        print(f"{indent}  {render_relation(relation, node_id=node.node_id)}   [{relation.relation_id}]")


def print_graph(graph: InMemoryGraph, ledger: CavemanLedger, *, title: str) -> None:
    print(f"  {title}: {graph.count(scope=SCOPE)} node(s)")
    counts = ledger.entry_counts(scope=SCOPE)
    for node in graph.list_nodes(scope=SCOPE):
        print_node(
            node,
            entries=counts.get(node.node_id, 0),
            relations=[relation for relation in graph.relations_of(node.node_id) if relation.source_id == node.node_id],
        )
    relations = graph.relations(scope=SCOPE)
    edge_types = graph.edge_types(scope=SCOPE)
    print(f"  RELATIONS: {len(relations)} edge(s) over {len(edge_types)} type(s), each carrying its own evidence")
    for relation in relations:
        left = graph.get_node(relation.source_id).name
        right = graph.get_node(relation.target_id).name
        derived = ledger.cooccurrence(relation.source_id).get(relation.target_id)
        print(
            f"    {relation.relation_id}  {relation.source_id} ({left}) -{relation.type}-> "
            f"{relation.target_id} ({right})  evidence={relation.evidence} "
            f"[cooccurrence says {derived}]"
        )
    print(f"  EDGE TYPES: {edge_types or '(none yet)'}")


def _evidence_table(graph: InMemoryGraph) -> dict[str, int]:
    """Every fact and edge in the scope keyed by identity, mapped to its evidence count.

    A fact's identity is its node and its text; an edge's is its triple. Both are
    what a RESTATEMENT lands on, so a table taken before an ingest and compared
    after it is the measurement of reinforcement -- as opposed to "the graph got
    bigger", which duplication also produces.
    """
    table = {
        f"{node.node_id} fact {fact.text}": fact.evidence
        for node in graph.list_nodes(scope=SCOPE)
        for fact in node.facts
    }
    for relation in graph.relations(scope=SCOPE):
        table[f"edge {relation.source_id} {relation.type} {relation.target_id}"] = relation.evidence
    return table


def _reinforcements(graph: InMemoryGraph, before: Mapping[str, int]) -> tuple[str, ...]:
    """One line per fact or edge that was ALREADY held and gained evidence.

    Identity unchanged, count up. A record that appeared for the first time is
    not a reinforcement however many entries it cites, which is why the previous
    table is compared key by key rather than summed.
    """
    now = _evidence_table(graph)
    return tuple(
        f"{key}: evidence {before[key]} -> {count}"
        for key, count in sorted(now.items())
        if before.get(key, count) < count
    )


def _multi_evidence(graph: InMemoryGraph) -> tuple[str, ...]:
    """Every fact and edge in the scope that more than one ledger entry supports.

    What ``(xN)`` renders from and what ranking multiplies by. Printed as the
    rendered line, so the ``(xN)`` a reader will actually see is on the page.
    """
    lines: list[str] = []
    for node in graph.list_nodes(scope=SCOPE):
        lines.extend(
            f"{node.node_id} {render_fact(fact)}   {list(fact.entry_ids)}" for fact in node.facts if fact.evidence > 1
        )
    lines.extend(
        f"{relation.relation_id} {render_relation(relation, node_id=relation.source_id)}   {list(relation.entry_ids)}"
        for relation in graph.relations(scope=SCOPE)
        if relation.evidence > 1
    )
    return tuple(lines)


def print_receipt(receipt: Receipt, *, index: int) -> None:
    print(
        f"    {index:>3}. {receipt.op.value:<24} subject={receipt.subject:<12} "
        f"in={receipt.inputs_digest[:12]} out={receipt.outputs_digest[:12]}"
    )
    print(f"         {receipt.detail}")


@dataclass(frozen=True)
class Rendered:
    """One block of rendered text, exactly as an agent received it.

    Collected for the format assertion, which is over every render and every
    prompt this run produced rather than over a sample of them: a character the
    design removed reaching a reader is the failure, and it only has to happen
    once.
    """

    label: str
    text: str
    about_nodes: bool
    """Whether this render is about CONCEPTS, and so owes a ``[n-...]`` id.

    True of every read, block, walk and deep read -- the whole traversal surface.
    False of exactly one thing, the replay proof, which is about a scope and its
    journal rather than about any node in it; asking it for a node id would be
    asking the wrong question of the right answer.
    """


def print_block(label: str, text: str, *, log: list[Rendered], about_nodes: bool, note: str = "") -> None:
    """Print one rendered block verbatim, and record it.

    Verbatim and pipe-prefixed, the same way a read is printed: what an agent
    gets is the product, so a transcript that paraphrased it would be showing
    something the system does not produce.
    """
    print(f"  {label}{f' -- {note}' if note else ''}")
    print("  ------ rendered verbatim, exactly as an LLM reader receives it -------------")
    for line in text.splitlines():
        print(f"  | {line}")
    print("  ---------------------------------------------------------------------------")
    log.append(Rendered(label=label, text=text, about_nodes=about_nodes))


@dataclass(frozen=True)
class ReadRecord:
    """One read as the assertions at the bottom need it: its label, its fold, its receipt.

    Kept because the reads happen against three different graphs — nine nodes
    before the squeeze, three after — and the final assertions run when only the
    last of those still exists. A record taken at the time is the only way to
    assert something about a read the graph has since moved past.
    """

    label: str
    duplicates_dropped: int
    detail: str


def read_detail(receipts: InMemoryReceipts, result: ReadResult) -> str:
    """The ``READ_EMITTED`` detail for *result*, read back out of the receipt stream.

    Matched on ``contract_digest``, which every read receipts as its ``subject``.
    The LAST match rather than the first: the same query read twice produces the
    same contract digest by design, so the newest receipt is this read's.

    Read back rather than reconstructed, because the point of printing it is that
    the audit stream says what the return value says — a detail computed here
    would prove nothing about the receipt.
    """
    found = [
        receipt
        for receipt in receipts.all(scope=result.scope)
        if receipt.op is ReceiptOp.READ_EMITTED and receipt.subject == result.contract_digest
    ]
    if not found:
        raise DemoAssertionError(
            f"a read emitted no READ_EMITTED receipt for its own contract digest "
            f"{result.contract_digest[:12]}; every read owes one"
        )
    return found[-1].detail


@dataclass(frozen=True)
class ReadSnapshot:
    """One read, plus the blocks it rendered, taken while the graph still matches it.

    The search section runs against the nine-node graph the free pass leaves and
    step 6 then forces that scope to three nodes, so by the time the assertions
    run the graph a read answered from is two merges in the past. A block cannot
    be recovered afterwards: its header carries the node's entry count, and a
    merge moves entries onto the survivor, so the same split a minute later finds
    no block at all. Taking it now is what lets the bottom of the run assert
    something about a read the graph has moved past.
    """

    label: str
    result: ReadResult
    blocks: tuple[tuple[Node, tuple[str, ...]], ...]
    scope_names: tuple[str, ...]
    scope_constraints: tuple[str, ...]
    """The scope's rule facts at read time, as rendered lines. What check 6 orders on."""

    @property
    def order(self) -> tuple[str, ...]:
        """The emitted node names in block order — what an LLM reader receives."""
        return tuple(node.name for node in self.result.nodes)

    @property
    def absent(self) -> tuple[str, ...]:
        """Scope nodes this read did NOT emit. Empty means it returned everything."""
        emitted = {node.name for node in self.result.nodes}
        return tuple(name for name in self.scope_names if name not in emitted)


def snapshot_read(label: str, result: ReadResult, *, graph: InMemoryGraph, ledger: CavemanLedger) -> ReadSnapshot:
    """Freeze one read against the graph and ledger that answered it.

    Blocks are split on the exact header string rather than guessed at by shape:
    a node named ``agent-memory MCP server`` puts the header's first pipe in the
    third word, which is the bug a shape guess produced once already.
    """
    counts = ledger.entry_counts(scope=result.scope)
    split = {}
    for block in result.rendered.split("\n\n"):
        lines = block.splitlines()
        if lines:
            split[lines[0]] = tuple(lines[1:])
    blocks: list[tuple[Node, tuple[str, ...]]] = []
    for node in result.nodes:
        header = render_header(node, entries=counts.get(node.node_id, 0))
        if header not in split:
            raise DemoAssertionError(
                f"{node.node_id} ({node.name}) is in result.nodes but has no block in the rendered read"
            )
        blocks.append((node, split[header]))
    nodes = graph.list_nodes(scope=result.scope)
    return ReadSnapshot(
        label=label,
        result=result,
        blocks=tuple(blocks),
        scope_names=tuple(node.name for node in nodes),
        scope_constraints=tuple(text for node in nodes for text in _texts(node) if _is_rule(text)),
    )


def print_seed(hit: SeedHit, *, floor: float, indent: str = "    ") -> None:
    """One seed candidate, whether it was kept, and the mechanism that found it.

    An exact hit prints the token it matched; a ``knn`` hit has no token, because
    nothing in particular matched it: it was measured against the whole query.

    The KEPT/DROPPED column is the amendment C column. ``read_k`` is a cap and
    not a filter, so before ``knn_min_similarity`` existed every slot was filled
    whatever the measurement said and a query in a small scope came back holding
    the whole scope. A DROPPED row is what kNN offered and the floor refused —
    printed rather than silently absent, because a floor nobody can see is a
    floor nobody can judge.
    """
    reason = {
        "alias": "EXACT — the node's name or one of its recorded aliases",
        "identifier": "EXACT — the ledger's identifier index",
        "knn": "measured — embedding neighbour of the whole query",
    }[hit.kind]
    if not hit.kept:
        reason = f"{reason}, under the {floor} floor"
    token = f"{hit.token!r}" if hit.token is not None else "(whole query)"
    status = "KEPT   " if hit.kept else "DROPPED"
    print(f"{indent}{status}  {hit.node_id}  {hit.kind:<11} sim={hit.similarity:.4f}  on {token:<24} {reason}")


def print_read(
    result: ReadResult,
    motive: CavemanMotive,
    *,
    label: str,
    embedded: int,
    receipts: InMemoryReceipts,
    log: list[ReadRecord],
    rendered_log: list[Rendered],
) -> None:
    """One read, verbatim, with its seeds, its cost, its fold and its receipt.

    *embedded* is how many vectors this call bought. Zero is the interesting
    number: it means the exact indexes answered the query on their own.

    The duplicate count and the ``READ_EMITTED`` detail are printed for EVERY
    read rather than only for the reads that folded something. "0 folded" is a
    statement — nothing in this read said one fact twice — and a reader cannot
    tell that from silence. Every read is appended to *log* so the assertions at
    the bottom can check the receipt against the return value.
    """
    ceiling = motive.read_token_budget + len(result.nodes) * NODE_HEADER_TOKENS
    exact = [hit for hit in result.seeds if hit.kind != "knn"]
    floored = [hit for hit in result.seeds if not hit.kept]
    print(f"  QUERY: {label}")
    print("  ------ rendered block, verbatim, exactly as an LLM reader receives it ------")
    for line in result.rendered.splitlines():
        print(f"  | {line}")
    print("  ---------------------------------------------------------------------------")
    if result.seeds:
        print(
            f"  SEEDS ({len(result.seeds)} candidate(s) of k={motive.read_k}; {len(exact)} exact, "
            f"{len(floored)} dropped by the kNN floor {motive.knn_min_similarity}) — why each node was "
            f"in the read, or was not:"
        )
        for hit in result.seeds:
            print_seed(hit, floor=motive.knn_min_similarity)
        if floored:
            print(
                f"    ^ {len(floored)} candidate(s) below {motive.knn_min_similarity} seeded nothing and "
                f"expanded nothing: an exact hit is never floored, a measured one is."
            )
    else:
        print("  SEEDS: none — a brief() has no query, so nothing was seeded by one.")
    print(f"  nodes emitted   : {len(result.nodes)}  ({', '.join(node.name for node in result.nodes)})")
    print(f"  lines           : {result.line_count} of a {motive.read_line_budget}-line budget")
    print(f"  tokens          : {result.token_count} (block incl. headers) against a ceiling of {ceiling}")
    print(f"  duplicates      : {result.duplicates_dropped} fact(s) folded away", end="")
    if result.duplicates_dropped:
        print("   <-- rank.dedupe_lines: one fact stated twice, kept once, before the budget cut")
    else:
        print("   (nothing in this read stated one fact twice)")
    print(f"  saturated       : {result.saturated}")
    print(f"  vectors bought  : {embedded}", end="")
    if embedded == 0 and exact:
        print("   <-- the exact indexes answered it; the query was never embedded")
    else:
        print()
    print(f"  contract_digest : {result.contract_digest}")
    detail = read_detail(receipts, result)
    print(f"  receipt detail  : {detail}")
    log.append(ReadRecord(label=label, duplicates_dropped=result.duplicates_dropped, detail=detail))
    rendered_log.append(Rendered(label=f"read({label!r})", text=result.rendered, about_nodes=True))


def print_explain(rendered: str, *, node_id: str, rendered_log: list[Rendered]) -> None:
    """``explain()``'s answer, verbatim. Unbounded by design, so counted not budgeted."""
    print_block(
        f"explain({node_id})",
        rendered,
        log=rendered_log,
        about_nodes=True,
        note="the deep read the footer above names",
    )
    print(f"  lines           : {len(rendered.splitlines())} (unbounded — every entry, newest first)")
    print(f"  tokens          : {estimate_tokens(rendered)} (no budget applies; this is the record, not a read)")


# ======================================================== the code-enforced checks


def check_graph_invariants(graph: InMemoryGraph, motive: CavemanMotive, *, stage: str) -> None:
    """The invariants that hold after every stage, whatever the model answered.

    These cannot be flaky: none of them depends on a model's wording. A failure
    here is a defect in this package, not in a prompt.
    """
    count = graph.count(scope=SCOPE)
    _check(
        count <= motive.max_nodes,
        f"[{stage}] the node bound is the whole point: {count} nodes over N={motive.max_nodes}",
    )
    for node in graph.list_nodes(scope=SCOPE):
        _check(
            len(_texts(node)) <= motive.max_facts_per_node,
            f"[{stage}] {node.node_id} holds {len(_texts(node))} facts over L={motive.max_facts_per_node}",
        )
        for text in _texts(node):
            try:
                validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)
            except CavemanError as exc:
                _check(False, f"[{stage}] {node.node_id} holds unusable fact text: {exc}")
        if node.embedding:
            norm = math.sqrt(sum(value * value for value in node.embedding))
            _check(
                abs(norm - 1.0) <= 1e-3,
                f"[{stage}] {node.node_id} embedding is not unit-norm: |v| = {norm:.6f}",
            )


def check_relations_resolve(graph: InMemoryGraph, motive: CavemanMotive, *, stage: str) -> None:
    """Every relation points at a node that exists, at every instant.

    Owed at every instant now, which is the change #251 amendment D makes. A
    relation used to be a LINE naming a node by name, so a global pass that
    absorbed a name left the nodes pointing at it dangling until their next
    incremental dream -- the window this function used to document and exempt.

    An edge carries node ids and ``graph.merge_nodes`` re-points the ones it
    absorbs, so a dangling endpoint is unrepresentable rather than repaired. The
    check stays because "unrepresentable" is a claim about the store, and this is
    the run that tests it.
    """
    held = {node.node_id for node in graph.list_nodes(scope=SCOPE)}
    for relation in graph.relations(scope=SCOPE):
        _check(
            relation.source_id in held and relation.target_id in held,
            f"[{stage}] relation {relation.relation_id} joins {relation.source_id} to "
            f"{relation.target_id}, and this scope holds {sorted(held)}",
        )
        _check(
            relation.source_id != relation.target_id,
            f"[{stage}] relation {relation.relation_id} is a self-loop on {relation.source_id}",
        )
    types = graph.edge_types(scope=SCOPE)
    _check(
        len(types) <= motive.max_edge_types,
        f"[{stage}] the scope holds {len(types)} edge type(s) over M={motive.max_edge_types}: {sorted(types)}",
    )


def check_survivors_kept_their_names(
    graph: InMemoryGraph,
    names_before: Mapping[str, str],
    *,
    stage: str,
) -> None:
    """No node that survived a global pass was renamed. **This is the search assertion.**

    The first live run merged four concepts into a node called ``chart-models``,
    and a search for any of chart, models, session affinity or #246 then landed
    on a node named for none of them. Amendment A's fix is structural rather than
    a prompt plea: ``pressure.merge_slate`` names the survivor, the survivor is
    the higher-value half, and ``dream`` writes ``survivor.name`` — its own
    stored spelling — rather than the model's ``survivor_name``.

    So the check is not "the name contains no hyphen", which would fail on a
    legitimately hyphenated name like ``agent-memory``. It is the exact property:
    a node id that existed before the pass and still exists after it holds the
    SAME name. A merge cannot rename its survivor and a split makes new ids, so
    there is no operation for which this may fail.
    """
    renamed = {
        node.node_id: (names_before[node.node_id], node.name)
        for node in graph.list_nodes(scope=SCOPE)
        if node.node_id in names_before and node.name != names_before[node.node_id]
    }
    _check(
        renamed == {},
        f"[{stage}] a surviving node was renamed, so a search by the name it had lands nowhere: "
        f"{renamed}. A merge keeps its survivor's own name",
    )
    survived = [node for node in graph.list_nodes(scope=SCOPE) if node.node_id in names_before]
    print(
        f"  SURVIVOR NAMES: {len(survived)} of the {len(names_before)} node(s) this pass started with are "
        f"still here, each under its own name."
    )


def check_read_invariants(
    result: ReadResult,
    motive: CavemanMotive,
    *,
    graph: InMemoryGraph,
    ledger: CavemanLedger,
) -> None:
    """Every emitted line re-parses, the header identifies itself, and the footer has a verb.

    Three things a reader depends on, checked on every read this demo makes
    rather than on one of them: the header carries ``as of <date>`` and
    ``N entries`` so staleness and evidence are judgeable without a second call;
    the last line is the ``more: explain(...), neighbors(...)`` footer naming every emitted
    node in block order; and every line between them is a grammatical caveman
    line. The footer is excluded from the re-parse deliberately — it is the one
    rendered line with no fact label, because it is a call and not a claim.

    ``read_count`` is checked against the GRAPH, not against ``result.nodes``.
    The result holds the snapshot the read rendered, captured before
    ``graph.record_read`` runs, and every record here is frozen — so a counter
    incremented afterwards cannot appear in it. That ordering is right: the
    result describes the read, not the store's counters after it.
    """
    for node in result.nodes:
        recorded = graph.get_node(node.node_id).read_count
        _check(recorded >= 1, f"read emitted {node.node_id} without recording the read")
    # Exact rather than heuristic: these are the nodes the read rendered, so
    # `node_header` reproduces its header strings verbatim and everything else in
    # the block is a body line. A shape guess got this wrong -- a node named
    # "agent-memory MCP server" puts the header's first pipe in the third word.
    counts = ledger.entry_counts(scope=SCOPE)
    headers = {render_header(node, entries=counts.get(node.node_id, 0)) for node in result.nodes}
    rendered_lines = result.rendered.splitlines()
    _check(
        rendered_lines != [],
        "a read over a dreamt scope emitted nothing at all; every read this demo makes has an answer",
    )
    footer = rendered_lines[-1]
    _check(
        footer.startswith(FOOTER_PREFIX),
        f"a rendered read must end with a {FOOTER_PREFIX!r} footer -- a header's node id is a pointer "
        f"with no verb, and this is the verbs. Last line was {footer!r}",
    )
    for header in headers:
        _check(
            "as of " in header and " entries" in header,
            f"a header must carry the date and the evidence count a reader judges it by: {header!r}",
        )
    body_lines = [line for line in rendered_lines[:-1] if line and line not in headers]
    for text in body_lines:
        label, separator, body = text.partition(": ")
        _check(
            bool(separator and label and body),
            f"every emitted line is a label then a fact, and {text!r} is not",
        )
        validate_fact_text(body, max_fact_tokens=motive.max_fact_tokens)
    _check(
        len(headers) == len(result.nodes),
        "two emitted nodes rendered the same header, so a reader cannot tell them apart",
    )
    _check(
        footer == render_footer([node.node_id for node in result.nodes]),
        f"the footer must name every emitted node, in block order, under both calls: {footer!r} against "
        f"{render_footer([node.node_id for node in result.nodes])!r}",
    )
    # The LINE budget is `read_token_budget`; the headers are a reported cost on
    # top of it, one per emitted node.
    ceiling = motive.read_token_budget + len(result.nodes) * NODE_HEADER_TOKENS
    _check(
        result.token_count <= ceiling,
        f"read block is {result.token_count} tokens, over its {ceiling}-token ceiling",
    )
    _check(
        result.line_count <= motive.read_line_budget,
        f"read emitted {result.line_count} lines over a {motive.read_line_budget}-line budget",
    )


# ================================================================== the live run


async def run() -> None:
    """Nine steps plus the search section, in order, against the live gateway."""
    chat = CountedChat(CavemanChatTransport(model=CHAT_MODEL))
    embedder = CountedEmbed(OpenAICompatibleEmbeddingTransport(model=EMBEDDING_MODEL))
    graph = InMemoryGraph()
    ledger = CavemanLedger(":memory:")
    receipts = InMemoryReceipts()
    motive = engineering_motive()
    episode = build_episode()
    # Every read this run makes, recorded as it happens: the assertions at the
    # bottom check a read's receipt against its return value, and by then the
    # graph the read ran against is two merges in the past.
    read_log: list[ReadRecord] = []
    # Every block of rendered text this run hands an agent. The format assertion
    # at the bottom reads these together with every prompt above.
    rendered_log: list[Rendered] = []

    memory = CavemanMemory(
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        chat=chat,
        embedder=embedder,
        clock=UtcClock(),
    )

    try:
        # ---------------------------------------------------------- 1: setup
        rule("1. THE EPISODE, THE MOTIVE, THE BUDGETS")
        print(
            f"  episode  : {episode.episode_id}   scope={episode.scope}   occurred_at={episode.occurred_at:%Y-%m-%dT%H:%M:%SZ}"
        )
        print(f"  chat     : {chat.identifier}")
        print(f"  embedding: {embedder.identifier}   (selected explicitly; no transport applies a default)")
        print("\n  TURNS — the exact text stage 1 reads:")
        for line in episode.as_prompt_text().splitlines():
            print(f"    {line}")
        print(f"\n  motive   : {motive.name}   digest={motive_digest(motive)[:16]}")
        print(f"  goal     : {motive.goal}")
        print("\n  BUDGETS")
        print(f"    N (max nodes per scope)   = {motive.max_nodes}")
        print(f"    L (max facts per node)    = {motive.max_facts_per_node}")
        print(
            f"    T (paragraph guard/line)  = {motive.max_fact_tokens} tokens"
            f"  (about {motive.max_fact_tokens * 4} characters; the prompt states brevity, not a count)"
        )
        print(f"    header                    = {NODE_HEADER_TOKENS} tokens (name|type|L:key|as of|N entries)")
        print(
            f"    read budget               = {motive.read_line_budget} lines / "
            f"{motive.read_token_budget} tokens, k={motive.read_k} seeds"
        )
        print(
            f"    scope_budget(N, L, T)     = "
            f"{scope_budget(motive.max_nodes, motive.max_facts_per_node, motive.max_fact_tokens):,} tokens"
        )
        print(f"    superseded facts          = keep={motive.keep_superseded}, ttl={motive.superseded_ttl_days} days")

        # -------------------------------------------- 2 + 3: extract, reconcile
        mark = len(chat.calls)
        ingested = await memory.ingest(episode, motive)

        rule("2. EXTRACT — one live call. The episode becomes claims in the ledger.")
        print_calls(chat, mark)
        print(f"\n  {len(ingested.entries)} claim(s) extracted under motive {motive.name!r}:\n")
        for entry in ingested.entries:
            print_entry(entry)
            print()
        superseding = [entry for entry in ingested.entries if entry.supersedes is not None]
        print(f"  supersession within this one episode: {len(superseding)} entry/entries")
        for entry in superseding:
            older = ledger.get(entry.supersedes) if entry.supersedes else None
            if older is not None:
                print(f"    {entry.entry_id} supersedes {older.entry_id}")
                print(f"      new: {entry.claim}")
                print(f"      old: {older.claim}   <-- stays in the ledger, leaves the node")

        rule("3. RECONCILE — one live call for the WHOLE batch. Names become node ids.")
        print_calls(chat, mark + 1)
        print(
            "\n  CANDIDATES OFFERED per surface name (top-k kNN, rendered to the model as "
            "id | name | aliases | type | gloss — never a node's lines). The aliases column is\n"
            "  what lets the router recognise a name it has routed before, on sight:"
        )
        for name, offered in ingested.reconciled.offered.items():
            if offered:
                rendered = ", ".join(
                    f"{node_id} ({graph.get_node(node_id).name}"
                    f"{' aka ' + ', '.join(graph.get_node(node_id).aliases) if graph.get_node(node_id).aliases else ''})"
                    for node_id in offered
                )
            else:
                rendered = "(none — the scope was empty, so nothing could be offered)"
            print(f"    {name!r}: {rendered}")
        print(f"\n  ADJUDICATIONS — {len(ingested.reconciled.adjudications)} name(s):")
        for item in ingested.reconciled.adjudications:
            node_id = ingested.reconciled.bindings[item.local_name]
            if item.decision == "bind":
                print(f"    {item.local_name!r}  ->  BIND {node_id}")
            else:
                spec = item.new_node
                assert spec is not None
                print(f"    {item.local_name!r}  ->  NEW  {node_id}  {{name={spec.name!r}, type={spec.type!r}}}")
                print(f"        gloss : {spec.gloss}")
            print(f"        reason: {item.reason}")
        print("\n  CONVERGENCE — surface names sharing one node id:")
        by_node: dict[str, list[str]] = {}
        for name, node_id in ingested.reconciled.bindings.items():
            by_node.setdefault(node_id, []).append(name)
        for node_id, names in sorted(by_node.items()):
            marker = "  <-- two surface names, one concept" if len(names) > 1 else ""
            print(f"    {node_id} ({graph.get_node(node_id).name}): {names}{marker}")
        print("\n  ALIASES RECORDED — every surface name the router sent here, so a later search")
        print("  by the name I happen to know is an EXACT hit rather than a similarity guess:")
        for node in graph.list_nodes(scope=SCOPE):
            print(f"    {node.node_id} ({node.name}): {list(node.aliases) or '(none)'}")
        print(f"\n  created={list(ingested.reconciled.created)}  bound={list(ingested.reconciled.bound)}")
        print(f"  dirty={list(ingested.reconciled.dirty)}  pressure={ingested.reconciled.pressure}")
        print("\n  THE MATCHER'S THIRD ANSWER -- a belief BETWEEN two concepts is a typed EDGE.")
        print("  A claim naming one concept binds and marks dirty, and the dreamer writes it as a")
        print("  fact. A claim naming two becomes a relation, with a TYPE from this scope's")
        print("  vocabulary or a new UPPER_SNAKE word the matcher coins, and the ledger claim")
        print("  carried verbatim as its first text. That is the three-way question this stage is")
        print("  actually asked: new concept, new belief about one, or a kind of belief the scope")
        print("  already has a word for, now applied to another pair.")
        print(f"\n  relations created   : {len(ingested.relations_created)} {list(ingested.relations_created)}")
        print(f"  relations REINFORCED: {len(ingested.relations_reinforced)} {list(ingested.relations_reinforced)}")
        print(
            f"  edge types COINED   : {list(ingested.edge_types_new) or 'none'}"
            f"   (the scope was empty, so every type here is a coinage)"
        )
        print(f"  the scope's vocabulary now: {graph.edge_types(scope=SCOPE) or '(none yet)'}")
        print(f"  M for this motive         : {motive.max_edge_types}")
        print(f"  node ids to traverse      : {list(ingested.node_ids)}")
        for relation_id in ingested.relations_created:
            relation = graph.get_relation(relation_id)
            source = graph.get_node(relation.source_id).name
            target = graph.get_node(relation.target_id).name
            print(
                f"    {relation_id}  [{relation.source_id}] {source} -{relation.type}-> [{relation.target_id}] {target}"
            )
            print(f"         claim (the ledger claim, verbatim): {relation.claim}")
            print(f"         evidence: {relation.evidence} {list(relation.entry_ids)}")
        print()
        print_graph(graph, ledger, title="GRAPH AFTER RECONCILE (factless by design)")
        check_graph_invariants(graph, motive, stage="reconcile")

        # --------------------------------------------------- 4: dream, per node
        rule("4. DREAM INCREMENTAL -- one live call per dirty node. The dreamer writes the facts.")
        before = {node.node_id: node for node in graph.dirty(scope=SCOPE)}
        seen = {node_id: ledger.for_node(node_id, since=node.dreamed_at) for node_id, node in before.items()}
        mark = len(chat.calls)
        dreamt = await memory.dream(scope=SCOPE, motive=motive, global_pass=False)
        print_calls(chat, mark)
        print(f"\n  {dreamt.incremental_count} node(s) re-dreamed. The dreamer is the ONLY writer of fact text.\n")
        for node in dreamt.dreamed:
            was = before[node.node_id]
            print(f"    NODE {node.node_id} — {was.name}|{was.type}  ->  {node.name}|{node.type}")
            print(f"      entries it saw ({len(seen[node.node_id])}, via ledger.for_node(since=dreamed_at)):")
            for entry in seen[node.node_id]:
                print(f"        {entry.entry_id} {entry.kind.value} {entry.claim}")
            print(f"      facts BEFORE ({len(_texts(was))}):")
            for text in _texts(was) or ("(none)",):
                print(f"        {text}")
            print(f"      facts AFTER ({len(_texts(node))}):")
            for text in _texts(node):
                print(f"        {text}   [{estimate_tokens(text[2:])} tokens]")
            demoted = [text for text in _texts(was) if text not in _texts(node)]
            print(f"      demoted ({len(demoted)}) — left the node, still in the ledger:")
            for text in demoted or ("(none)",):
                print(f"        {text}")
            print(f"      dreamed_at={node.dreamed_at}  dirty={node.dirty}")
            print()
        print_graph(graph, ledger, title="GRAPH AFTER THE INCREMENTAL DREAM")
        check_graph_invariants(graph, motive, stage="dream-incremental")
        check_relations_resolve(graph, motive, stage="dream-incremental")

        # ------------------------------------------------- 5: global pass, free
        rule(f"5. DREAM GLOBAL, FREE — one live call at N={motive.max_nodes}. No pressure, so nothing is forced.")
        receipts_before = len(receipts.all(scope=SCOPE))
        mark = len(chat.calls)
        free = await memory.dream(scope=SCOPE, motive=motive, global_pass=True)
        print_calls(chat, mark)
        assert free.glob is not None
        print(
            f"\n  count {free.glob.count_before} of N={motive.max_nodes}   "
            f"pressure = max(0, {free.glob.count_before} - {motive.max_nodes}) = {free.glob.pressure}"
        )
        print(f"  forced-merge slate: {[pair.render() for pair in free.glob.slate] or 'empty — a free pass'}")
        print(f"  ops applied       : {free.glob.ops_applied}")
        print(f"  count after       : {free.glob.count_after}")
        print("\n  OPS, read back from the receipt stream (the audit path, not a return value):")
        for index, receipt in enumerate(receipts.all(scope=SCOPE)[receipts_before:], 1):
            print_receipt(receipt, index=index)
        if free.glob.ops_applied == 0:
            print("    (none — four-ish nodes under a budget of five hundred need no reorganising,")
            print("     and zero ops is a valid answer rather than a failure to answer)")
        print()
        print_graph(graph, ledger, title="GRAPH AFTER THE FREE GLOBAL PASS")
        check_graph_invariants(graph, motive, stage="dream-global-free")
        await repair_orphans(memory, graph, ledger, free.glob, chat=chat, motive=motive, stage="free")

        # ------------------- 5b: a SECOND episode -- reinforcement and supersession
        rule("5b. A SECOND EPISODE INTO THE SAME SCOPE -- what a restated belief does")
        print("  Everything above was one conversation. A memory that only ever grows is not a")
        print("  memory, so this is the case that separates the two: four turns, a day later,")
        print("  into the SAME scope, three of them restating something episode one already")
        print("  recorded and one of them replacing it.")
        print()
        print("  What each turn is here to do:")
        print("    1  restates the gateway's identity and the always-through-it rule  -> reinforce")
        print("    2  restates the agent-memory/gateway edge, still fixed by #245     -> reinforce")
        print(f"    3  moves the chat default to {CHAT_DEFAULT_NOW}               -> supersede")
        print("    4  restates the session-affinity flake nobody can reproduce       -> reinforce")
        print()
        print("  A restatement must NOT become a second fact or a second edge. It must land on")
        print("  the record that is already there: one more entry in its evidence, last_seen")
        print("  moved, and (xN) in the render. That is the whole difference between evidence")
        print("  and duplication.")
        episode_two = build_episode_two()
        print()
        print(
            f"  episode  : {episode_two.episode_id}   scope={episode_two.scope}   "
            f"occurred_at={episode_two.occurred_at:%Y-%m-%dT%H:%M:%SZ}"
        )
        print("\n  TURNS -- the exact text stage 1 reads:")
        for line in episode_two.as_prompt_text().splitlines():
            print(f"    {line}")

        evidence_before = _evidence_table(graph)
        mark = len(chat.calls)
        second = await memory.ingest(episode_two, motive)
        print()
        print_calls(chat, mark)
        print(f"\n  {len(second.entries)} claim(s) extracted:\n")
        for entry in second.entries:
            print_entry(entry)
            print()
        print("  WHERE THEY ROUTED -- no new node is the CORRECT answer for a recap:")
        for name, node_id in sorted(second.reconciled.bindings.items()):
            marker = " (new)" if node_id in second.reconciled.created else " (bound to what episode one built)"
            print(f"    {name!r} -> [{node_id}] {graph.get_node(node_id).name}{marker}")
        print(f"\n  nodes created            : {len(second.reconciled.created)} {list(second.reconciled.created)}")
        print(f"  nodes bound              : {len(second.reconciled.bound)} {list(second.reconciled.bound)}")
        print(f"  relations created        : {len(second.relations_created)} {list(second.relations_created)}")
        print(f"  relations REINFORCED     : {len(second.relations_reinforced)} {list(second.relations_reinforced)}")
        print(f"  new edge types           : {list(second.edge_types_new) or 'none -- the vocabulary held'}")
        print(f"  node ids to traverse     : {list(second.node_ids)}")
        for relation_id in second.relations_reinforced:
            relation = graph.get_relation(relation_id)
            print(
                f"    {relation_id} {relation.type} now carries evidence {relation.evidence} "
                f"{list(relation.entry_ids)}, claim unchanged: {relation.claim!r}"
            )
        print("      ^ a reinforcement passes claim=None, so the dreamer's compressed text and its")
        print("        'until' marker survive a restatement instead of being overwritten by the")
        print("        raw ledger claim. Reconcile has no opinion about either.")

        mark = len(chat.calls)
        second_dream = await memory.dream(scope=SCOPE, motive=motive, global_pass=False)
        print()
        print_calls(chat, mark)
        print(f"\n  {second_dream.incremental_count} node(s) re-dreamed over the two episodes' evidence.")
        print()
        print_graph(graph, ledger, title="GRAPH AFTER THE SECOND EPISODE")
        check_graph_invariants(graph, motive, stage="episode-two")
        check_relations_resolve(graph, motive, stage="episode-two")

        reinforced = _reinforcements(graph, evidence_before)
        print("\n  REINFORCEMENT -- every fact and edge whose evidence GREW rather than being copied:")
        for line in reinforced or ("    (none this run -- reported as a FINDING below)",):
            print(f"    {line}" if not line.startswith("    ") else line)
        print()
        print(f"  facts and edges now supported by more than one entry: {len(_multi_evidence(graph))}")
        for rendered in _multi_evidence(graph):
            print(f"    {rendered}")

        # ------------------------------------------- how an agent searches this
        #
        # BEFORE the squeeze, deliberately. The previous run ran this section
        # after step 6 had forced the scope to three nodes, and with read_k=8 a
        # three-node scope hands every read the entire graph: four searches, four
        # identical answers, and nothing on the page a reader could learn about
        # seeding from. The graph the FREE pass leaves is the first scope in this
        # run where a query has anything to decide.
        searched_names = tuple(node.name for node in graph.list_nodes(scope=SCOPE))
        rule("HOW AN AGENT SEARCHES THIS — the five calls a reader actually makes")
        print(f"  Against the {len(searched_names)}-node graph the free global pass just left — NOT against the")
        print(f"  N={FORCED_MAX_NODES} graph step 6 is about to force. A scope that fits inside one read gives a")
        print("  query nothing to decide, and what the query decides is this section's subject.")
        print()
        print("  Written from the reader's seat: what I would type, in the state I would be in.")
        print("  Every call here is deterministic and makes ZERO LLM calls. Each one prints its")
        print("  SEEDS — the mechanism that put each node in the read — because 'why did this")
        print("  come back' is a question a searcher has to be able to answer. The KEPT/DROPPED")
        print(f"  column is the kNN similarity floor at {motive.knn_min_similarity}: a measured neighbour under it")
        print("  seeds nothing and expands nothing, and an exact hit is never floored at all. Each")
        print("  read also prints its duplicate count and its READ_EMITTED detail: two lines that")
        print("  state one fact are folded before the budget cut, and the receipt says how many.")
        print()
        print("  a) brief()                              no query at all — the session-start read")
        print("  b) read('#246')                         an identifier; #245 and #246 embed alike")
        print("  c) read('C4')                           a name I know, which may not be the kept name")
        print("  d) read('the gateway refuses my Host header')   a task, in my own words")
        print("  e) node(<node id>)                      re-read ONE concept, whole")
        print("  f) neighbors(<node id>)                 what it connects to, and by what belief")
        print("  g) explain(<node id>)                   the deep read the footer's first call names")
        print()
        print("  e, f and g are what the [n-...] id in every block header is FOR. A read answers a")
        print("  query; an id turns the answer into a graph an agent can walk, with no further")
        print("  model call and no re-query. That loop -- read, walk, read -- is why the ids are")
        print("  carried back in the rendering at all rather than being an internal detail.")

        searches: dict[str, ReadResult] = {}

        rule("a. brief() — the session-start / post-compaction read, no query")
        print("  The read I have when the context that knew what to ask for is gone. Every rule")
        print("  line in the scope first — rank.CONSTRAINT_FLOOR puts them there — then the")
        print("  highest-value nodes by pressure.node_value, the same function that decides")
        print("  which nodes survive a forced merge. No query, so no vector and no read_k.")
        mark, vectors = len(chat.calls), embedder.count
        summary = memory.brief(scope=SCOPE, motive=motive)
        print()
        print_read(
            summary,
            motive,
            label="(none — brief() takes no query)",
            embedded=embedder.count - vectors,
            receipts=receipts,
            log=read_log,
            rendered_log=rendered_log,
        )
        check_read_invariants(summary, motive, graph=graph, ledger=ledger)
        print_calls(chat, mark)

        for label, query in SEARCHES:
            rule(f"{label} — read({query!r})")
            print(f"  {SEARCH_NOTES[label]}")
            mark, vectors = len(chat.calls), embedder.count
            result = memory.read(query=query, scope=SCOPE, motive=motive)
            searches[label] = result
            print()
            print_read(
                result,
                motive,
                label=query,
                embedded=embedder.count - vectors,
                receipts=receipts,
                log=read_log,
                rendered_log=rendered_log,
            )
            check_read_invariants(result, motive, graph=graph, ledger=ledger)
            print_calls(chat, mark)

        rule("WHAT THE FOUR READS ABOVE ACTUALLY SHOW")
        every_read = (("a", summary), *searches.items())
        # Frozen here, against the graph that answered them: step 6 is about to
        # move entries onto survivors, and a block's header carries an entry
        # count.
        snapshots = tuple(snapshot_read(label, result, graph=graph, ledger=ledger) for label, result in every_read)
        holders = [node for node in graph.list_nodes(scope=SCOPE) if any(_is_rule(t) for t in _texts(node))]
        print("  Block order, per read — the order an LLM reader receives them in:")
        for label, result in every_read:
            print(f"    {label}: {[node.name for node in result.nodes]}")
        orders = {label: tuple(node.name for node in result.nodes) for label, result in every_read}
        distinct = sorted(set(orders.values()))
        print()
        print(f"  {len(distinct)} distinct block order(s) over {len(searched_names)} node(s) in the scope.")
        print("  Membership, per read — how much of the scope each one actually emitted:")
        for label, result in every_read:
            absent = [name for name in searched_names if name not in {node.name for node in result.nodes}]
            note = f"   not emitted: {absent}" if absent else "   the whole scope"
            print(f"    {label}: {len(result.nodes)} of {len(searched_names)}{note}")
        unselective = [snapshot.label for snapshot in snapshots[1:] if not snapshot.absent]
        if unselective:
            print()
            print(f"  FINDING — query read(s) {unselective} returned the WHOLE scope, so on this scope a")
            print("  query decided ORDER and nothing else. This is arithmetic rather than a read-side")
            print(
                f"  defect: read_k={motive.read_k} seeds up to {motive.read_k} of these {len(searched_names)} nodes, 1-hop expansion then reaches"
            )
            print("  any node linked to a seed, and what is left over is at most one unlinked node. Which")
            print("  reads exclude anything is therefore a property of THIS episode's link structure, and")
            print("  it moves between runs — so the run reports it and asserts only what holds: the four")
            print("  block orders differ. Showing a query that genuinely selects needs a scope wider than")
            print("  read_k plus its neighbourhood, which is a longer episode or a smaller read_k — both")
            print("  the reviewer's call. The bound itself is asserted deterministically instead, in")
            print("  tests/test_caveman_pipeline.py::test_a_query_read_excludes_part_of_a_scope_a_brief_carries_whole")
            print("  (twelve unlinked nodes against read_k=8: the read emits eight, the brief all twelve).")
        print()
        print("  Two things decide those two tables, and they are not the same thing:")
        print()
        print("  1. WHAT IS IN A READ is decided by seeding and the budget. read() seeds at most")
        print(f"     read_k={motive.read_k} nodes — exact hits first, kNN filling the rest — then expands 1-hop,")
        print("     so a query can only exclude a node once the scope is bigger than seeds plus")
        print(f"     their neighbours. brief() has no read_k at all: it ranks all {len(searched_names)} nodes and")
        print("     emits them until the budget bites, which is exactly what a session-start read")
        print("     should do. So brief() is the read that carries the most and decides the least.")
        print()
        print("  2. WHAT ORDER IT COMES IN is decided by rank, and there are two floors, not one.")
        print("     Block order follows the rank of each node's best line.")
        print(f"     rank.EXACT_FLOOR ({EXACT_FLOOR:.0e}) lifts every line of a node the query NAMED, so a query")
        print("     read leads with what was asked for. rank.CONSTRAINT_FLOOR")
        print(
            f"     ({CONSTRAINT_FLOOR:.0e}) then floats every rule above the whole measured set, so the "
            f"{len(holders)} node(s)"
        )
        print(f"     carrying one ({', '.join(node.name for node in holders)}) come next,")
        print("     and inside every block a rule is still first. An agent cannot search its way")
        print("     past a rule — but it is no longer made to read past two of somebody else's")
        print("     rules to reach the node it named. brief() passes no exact ids and is unchanged:")
        print("     with no query there is nothing named, so its constraint-holders lead.")
        print()
        print("  What the query DID decide is on the page too — the seeds, which are the answer to")
        print("  'why is this node here at all' and the half of retrieval similarity cannot do:")
        for label, result in every_read:
            exact = [hit for hit in result.seeds if hit.kind != "knn"]
            reason = ", ".join(f"{hit.node_id} {hit.kind} on {hit.token!r}" for hit in exact) or "(none)"
            print(f"    {label}: {len(result.seeds)} seed(s), {len(exact)} exact -> {reason}")

        gateway_node = _node_for_concept(graph, ingested.reconciled.bindings, "the gateway")

        rule("e. node(<node id>) -- re-read ONE concept, exactly as a read would have shown it")
        print("  I have an id from a block header or from an ingest summary, and I want that one")
        print("  concept whole rather than whatever a query would rank into a budget. Same")
        print("  renderer as a read, so the block is the same text -- including the header's entry")
        print("  count, which comes from the ledger and not from adding up the facts' evidence.")
        print("  A pure projection: it records no read, so walking past a node does not move it")
        print("  up the survival ranking.")
        mark, vectors = len(chat.calls), embedder.count
        gateway_block = memory.node(node_id=gateway_node.node_id)
        print()
        print_block(f"node({gateway_node.node_id})", gateway_block, log=rendered_log, about_nodes=True)
        print(f"  vectors bought  : {embedder.count - vectors}")
        print_calls(chat, mark)

        rule("f. neighbors(<node id>) -- the walk: what this concept connects to, and why")
        print("  One line per typed edge, both directions, each carrying the id on the OTHER end.")
        print("  This is the half a flat key-value memory cannot do: the answer to 'what else")
        print("  should I know about before I touch this' is an edge, with its own claim and its")
        print("  own evidence, and the footer hands back the ids to read next.")
        mark, vectors = len(chat.calls), embedder.count
        walk = memory.neighbors(node_id=gateway_node.node_id)
        print()
        print_block(f"neighbors({gateway_node.node_id})", walk, log=rendered_log, about_nodes=True)
        print(f"  vectors bought  : {embedder.count - vectors}")
        print_calls(chat, mark)
        print("\n  AND THEN ONE HOP FURTHER -- every id that footer named, re-read as a block:")
        walked: list[str] = []
        for edge in graph.relations_of(gateway_node.node_id):
            other = edge.target_id if edge.source_id == gateway_node.node_id else edge.source_id
            if other in walked:
                continue
            walked.append(other)
            print()
            print_block(
                f"node({other})",
                memory.node(node_id=other),
                log=rendered_log,
                about_nodes=True,
                note=f"reached from [{gateway_node.node_id}] by {edge.type}",
            )
        if not walked:
            print("    (none -- this concept holds no edges this run)")

        rule("g. explain(<node id>) -- the deep read the footer's first call points at")
        print("  A read is bounded to L lines a node, so 'what is the evidence for this' is a")
        print("  question it structurally cannot answer. This answers it: every ledger entry on")
        print("  the node, newest first, superseded ones included and marked. Zero LLM calls,")
        print("  no embedding, no budget — it is the record, not a read.")
        mark, vectors = len(chat.calls), embedder.count
        deep = memory.explain(node_id=gateway_node.node_id)
        print()
        print_explain(deep, node_id=gateway_node.node_id, rendered_log=rendered_log)
        print(f"  vectors bought  : {embedder.count - vectors}")
        print_calls(chat, mark)

        # ------------------------------------ 6a: the OTHER ceiling -- M edge types
        rule("6a. DREAM GLOBAL, M FORCED -- the edge-type vocabulary is bounded too")
        vocabulary = graph.edge_types(scope=SCOPE)
        print("  N bounds how many CONCEPTS a scope may hold. M bounds how many kinds of BELIEF")
        print("  it may hold between them, and it is the ceiling the amendment added: a scope")
        print("  that coins a new relation type for every pair has a vocabulary, not a schema,")
        print("  and nothing downstream can traverse it by type.")
        print()
        print(f"  the scope's vocabulary now : {vocabulary}")
        print(f"  M at the engineering preset: {motive.max_edge_types}  -- no pressure at all here")
        compaction_ceiling = max(1, len(vocabulary) - 1)
        # N stays free and only M is tightened, so this pass demonstrates ONE
        # thing. It runs BEFORE the node squeeze deliberately: a merge re-points
        # the edges of what it absorbs and drops the self-loops that makes, so by
        # the time the scope is three nodes the vocabulary has collapsed to one
        # type and there is nothing left for M to bite on.
        compacted_motive = motive.model_copy(update={"max_edge_types": compaction_ceiling})
        doomed_types = compaction_targets(vocabulary, max_edge_types=compaction_ceiling)
        print()
        print(f"  So M is set to {compaction_ceiling} for one pass -- one less than the vocabulary -- which forces")
        print("  exactly one rename and nothing else. WHICH type is doomed is arithmetic and not")
        print("  the model's: the least used one, ties broken by name, through the same")
        print("  dream.compaction_targets the prompt and the validation both read. What the model")
        print("  decides is the only part that is a judgement about meaning -- what it folds INTO.")
        print(f"\n  MUST COMPACT: {list(doomed_types)}")
        types_before = dict(vocabulary)
        relations_before = {relation.relation_id: relation.type for relation in graph.relations(scope=SCOPE)}
        receipts_before = len(receipts.all(scope=SCOPE))
        mark = len(chat.calls)
        compacted = await memory.dream(scope=SCOPE, motive=compacted_motive, global_pass=True)
        print()
        print_calls(chat, mark)
        _check(compacted.glob is not None, "the compaction pass returned no global outcome")
        assert compacted.glob is not None
        print(f"\n  edge-type pressure the pass computed: {list(compacted.glob.edge_type_pressure)}")
        print(f"  ops applied                         : {compacted.glob.ops_applied}")
        print("\n  OPS, from the receipt stream:")
        for index, receipt in enumerate(receipts.all(scope=SCOPE)[receipts_before:], 1):
            print_receipt(receipt, index=index)
        types_after = graph.edge_types(scope=SCOPE)
        print(f"\n  vocabulary before : {types_before}")
        print(f"  vocabulary after  : {types_after}")
        gone = [name for name in types_before if name not in types_after]
        arrived = [name for name in types_after if name not in types_before]
        print(f"  types that went   : {gone or '(none)'}")
        print(f"  types that arrived: {arrived or '(none -- a fold into a type that was already held)'}")
        print("\n  THE RENAME, EDGE BY EDGE. The claim and the evidence are untouched: a")
        print("  compaction re-LABELS a belief, it does not restate or re-evidence one.")
        renames = [
            f"{relation.relation_id}: {relations_before[relation.relation_id]} -> {relation.type}   "
            f"claim={relation.claim!r}   evidence={list(relation.entry_ids)}"
            for relation in graph.relations(scope=SCOPE)
            if relation.relation_id in relations_before and relations_before[relation.relation_id] != relation.type
        ]
        for line in renames or ["(no edge changed type -- reported as a FINDING below)"]:
            print(f"    {line}")
        print()
        print_graph(graph, ledger, title=f"GRAPH AFTER THE EDGE-TYPE COMPACTION (M={compaction_ceiling})")
        check_graph_invariants(graph, compacted_motive, stage="edge-type-compaction")
        check_relations_resolve(graph, compacted_motive, stage="edge-type-compaction")
        _check(
            len(types_after) <= compaction_ceiling,
            f"the pass left {len(types_after)} edge type(s) over its own M={compaction_ceiling}: "
            f"{types_after}. M is a ceiling, exactly like N",
        )

        # --------------------------------------------- 6b: global pass, N forced
        squeezed = motive.model_copy(update={"max_nodes": FORCED_MAX_NODES})
        rule(f"6b. DREAM GLOBAL, N FORCED — down to N={FORCED_MAX_NODES}. This is where the bound bites.")
        print("  A forced slate is `pressure` DISJOINT pairs, so it needs 2 x pressure distinct")
        print("  nodes and each pair removes exactly one. One pass can therefore remove at most")
        print(
            f"  count // 2 nodes — so reaching N={FORCED_MAX_NODES} from {graph.count(scope=SCOPE)} takes more than one pass,"
        )
        print("  and the budget is stepped down to what each pass can actually satisfy.")
        mandates: list[str] = []
        forced = None
        for attempt in range(1, MAX_FORCED_PASSES + 1):
            count_now = graph.count(scope=SCOPE)
            if count_now <= FORCED_MAX_NODES:
                break
            # The tightest budget one pass can meet: it may pair at most half the
            # scope, and each pair costs one node.
            reachable = max(FORCED_MAX_NODES, count_now - count_now // 2)
            squeezed = motive.model_copy(update={"max_nodes": reachable})
            signal = pressure(count_now, reachable)
            print(f"\n  ---- PASS {attempt} ----")
            print(
                f"  count {count_now}, budget for this pass N={reachable} "
                f"(target {FORCED_MAX_NODES})   pressure = max(0, {count_now} - {reachable}) = {signal}"
            )
            print("\n  NODE VALUES — recomputed here through pressure.node_value, the same pure")
            print(
                f"  function dream_global uses. Weights: recency={squeezed.value_weights.recency} "
                f"reads={squeezed.value_weights.reads} degree={squeezed.value_weights.degree} "
                f"type={squeezed.value_weights.type}, half-life {squeezed.recency_half_life_days}d."
            )
            now = UtcClock().now()
            valued = sorted(
                (
                    ValuedNode(
                        node=node,
                        value=node_value(
                            node,
                            degree=len(graph.relations_of(node.node_id)),
                            now=now,
                            weights=squeezed.value_weights,
                            half_life_days=squeezed.recency_half_life_days,
                            type_weight=squeezed.type_weight,
                        ),
                    )
                    for node in graph.list_nodes(scope=SCOPE)
                ),
                key=lambda item: (item.value, item.node_id),
            )
            for ranked in valued:
                print(
                    f"    {ranked.node_id} {ranked.node.name:<16} type={ranked.node.type:<10} "
                    f"reads={ranked.node.read_count} degree={len(graph.relations_of(ranked.node_id))}  "
                    f"value={ranked.value:.4f}"
                )
            recomputed = merge_slate(
                valued,
                pressure=signal,
                cooccurrence=ledger.cooccurrence,
                turn_links=ledger.turn_cooccurrence(scope=SCOPE),
                similarity=embedding_similarity,
            )
            print("\n  SLATE, recomputed deterministically. The doomed node is the lowest-value one;")
            print("  its peer is chosen in one order over the WHOLE scope: the most LEDGER ENTRIES")
            print("  naming both, then the most conversational turns the ledger says they share, then")
            print("  embedding similarity. The survivor is the higher-value half and KEEPS ITS OWN NAME;")
            print("  the doomed node's name becomes one of its aliases. Each mandate carries the evidence")
            print("  that chose the peer, in the same words the prompt states it to the model:")
            for pair in recomputed:
                doomed = graph.get_node(pair.doomed_node_id)
                survivor = graph.get_node(pair.survivor_node_id)
                mandate = (
                    f"MUST MERGE: {doomed.name!r} ({pair.doomed_node_id}) INTO "
                    f"{survivor.name!r} ({pair.survivor_node_id}) because {merge_reason(pair)}"
                )
                mandates.append(mandate)
                print(f"    {mandate}")
                print(
                    f"        shared entries={pair.link_weight}  shared turns={pair.turn_cooccurrence}  "
                    f"similarity={pair.similarity:.4f}"
                )
            print("    The model is told WHICH node goes, WHICH name survives and ON WHAT EVIDENCE;")
            print("    it decides only what the survivor's lines and type say. 'nearest by embedding'")
            print("    is the weakest of the three mandates and says so on its face — it is no")
            print("    warrant for a line asserting a relationship the ledger never recorded.")
            names_before = {node.node_id: node.name for node in graph.list_nodes(scope=SCOPE)}

            receipts_before = len(receipts.all(scope=SCOPE))
            mark = len(chat.calls)
            forced = await memory.dream(scope=SCOPE, motive=squeezed, global_pass=True)
            print_calls(chat, mark)
            _check(forced.glob is not None, "global_pass=True returned no global outcome")
            glob = forced.glob
            assert glob is not None
            print(f"\n  pressure the pass computed: {glob.pressure}")
            print(f"  slate the pass mandated   : {[pair.render() for pair in glob.slate]}")
            print(
                f"  same pairs as recomputed  : "
                f"{[pair.render() for pair in glob.slate] == [pair.render() for pair in recomputed]}"
                f"  (pure function, same inputs — a False here is a clock-skew tiebreak, not a defect)"
            )
            print(f"  ops applied               : {glob.ops_applied}")
            print(f"  count {glob.count_before} -> {glob.count_after}   (N={reachable})")
            print("\n  OPS, from the receipt stream:")
            for index, receipt in enumerate(receipts.all(scope=SCOPE)[receipts_before:], 1):
                print_receipt(receipt, index=index)
            print()
            print_graph(graph, ledger, title=f"GRAPH AFTER PASS {attempt} (N={reachable})")
            check_graph_invariants(graph, squeezed, stage=f"dream-global-forced-{attempt}")
            check_survivors_kept_their_names(graph, names_before, stage=f"forced-{attempt}")
            _check(
                glob.count_after <= reachable,
                f"pass {attempt} left {glob.count_after} nodes over its own budget of {reachable}",
            )
            _check(
                glob.count_after < glob.count_before,
                f"pass {attempt} had pressure {glob.pressure} and shrank nothing",
            )
            await repair_orphans(memory, graph, ledger, glob, chat=chat, motive=squeezed, stage=f"forced-{attempt}")

        _check(
            graph.count(scope=SCOPE) <= FORCED_MAX_NODES,
            f"N={FORCED_MAX_NODES} was not enforced after {MAX_FORCED_PASSES} pass(es): "
            f"{graph.count(scope=SCOPE)} nodes remain",
        )
        squeezed = motive.model_copy(update={"max_nodes": FORCED_MAX_NODES})
        print(f"\n  N={FORCED_MAX_NODES} ENFORCED: {graph.count(scope=SCOPE)} node(s) remain.")

        # ------------------------------- the post-squeeze read: a name survives it
        rule(f"AFTER THE SQUEEZE — read({NAME_QUERY!r}) once more, on the N={FORCED_MAX_NODES} graph")
        print(f"  The same query section c asked of the {len(searched_names)}-node graph, asked again now that")
        print("  compression has taken most of those nodes away. This is the one read worth")
        print("  repeating, because a merge is where a search key is most likely to be lost:")
        print("  the node the answer was on may no longer exist. merge_nodes unions the absorbed")
        print("  node's name AND its aliases into the survivor's aliases, so every spelling that")
        print("  ever pointed at the concept still points at it — through the merge, and exactly")
        print("  rather than by resemblance.")
        mark, vectors = len(chat.calls), embedder.count
        post_squeeze = memory.read(query=NAME_QUERY, scope=SCOPE, motive=squeezed)
        print()
        print_read(
            post_squeeze,
            squeezed,
            label=NAME_QUERY,
            embedded=embedder.count - vectors,
            receipts=receipts,
            log=read_log,
            rendered_log=rendered_log,
        )
        check_read_invariants(post_squeeze, squeezed, graph=graph, ledger=ledger)
        print_calls(chat, mark)
        landed = [node for node in post_squeeze.nodes if any(TOOL_COUNT in text for text in _texts(node))]
        print(f"\n  the node carrying the corrected {TOOL_COUNT!r} tool count: {[node.name for node in landed]}")
        for hit in post_squeeze.seeds:
            if hit.kind != "knn" and (hit.token or "").casefold() == NAME_QUERY.casefold():
                node = graph.get_node(hit.node_id)
                print(
                    f"  seeded {hit.node_id} ({node.name}) by {hit.kind} on {hit.token!r} at "
                    f"similarity {hit.similarity:.4f}"
                )
                print(f"    its aliases now: {list(node.aliases)}")

        # --------------------------------------------------- the replay proof
        rule("REPLAY PROOF -- the compression is provable, not merely trusted")
        print("  Every write above went through a journal. Each mutation is a DreamEvent in the")
        print("  ledger carrying the node and relation content BEFORE and AFTER, and nothing")
        print("  else: no vectors, because a vector is derived from the content, and no")
        print("  bookkeeping, because 'somebody read this' is not a belief changing.")
        print()
        print("  So 'this graph is what the record says' can be CHECKED rather than asserted.")
        print("  The journal is applied in order onto a fresh empty graph -- no model call, no")
        print("  embedding -- and the two are compared by a digest over what they ASSERT: names,")
        print("  types, aliases, facts, relations. Not ids, which a replay mints itself, and not")
        print("  timestamps, which record when a claim arrived rather than what it says.")
        mark, vectors = len(chat.calls), embedder.count
        proof = memory.replay(scope=SCOPE)
        print()
        print_block("replay(scope)", proof.rendered, log=rendered_log, about_nodes=False)
        print(f"  vectors bought  : {embedder.count - vectors}")
        print_calls(chat, mark)
        replayed = replay_scope(scope=SCOPE, ledger=ledger)
        print(
            f"\n  the replayed graph holds {replayed.count(scope=SCOPE)} node(s) and "
            f"{len(replayed.relations(scope=SCOPE))} relation(s), rebuilt from the journal alone:"
        )
        for node in replayed.list_nodes(scope=SCOPE):
            print(f"    {node.node_id} {node.name} ({node.type})  facts={len(node.facts)}  aka {list(node.aliases)}")
        print("\n  Its node ids are its OWN -- the store mints them -- which is exactly why the")
        print("  digest is over content. Two stores holding one scope digest identically; a graph")
        print("  somebody wrote to behind the journal's back does not, and that is the case a")
        print("  proof exists for.")
        _check(
            proof.equal,
            f"the journal does NOT account for this graph: live {proof.live_digest} vs replayed "
            f"{proof.replayed_digest} over {proof.event_count} event(s). Compression that cannot be "
            f"replayed is compression nobody can audit",
        )
        print(f"\n  PROVED: {proof.event_count} journalled event(s) rebuild this scope exactly.")

        # ------------------------------------------------------------ 7: read
        rule("7. READ — two queries, ZERO live calls. Deterministic: embed, kNN, 1-hop, rank, cut.")
        queries = (
            "what do I know about the gateway",
            "how many tools does the C4 memory server return",
        )
        results: list[ReadResult] = []
        mark = len(chat.calls)
        for query in queries:
            vectors = embedder.count
            result = memory.read(query=query, scope=SCOPE, motive=squeezed)
            results.append(result)
            print()
            print_read(
                result,
                squeezed,
                label=query,
                embedded=embedder.count - vectors,
                receipts=receipts,
                log=read_log,
                rendered_log=rendered_log,
            )
            check_read_invariants(result, squeezed, graph=graph, ledger=ledger)
        print()
        print_calls(chat, mark)
        repeat = memory.read(query=queries[0], scope=SCOPE, motive=squeezed)
        print(
            f"\n  the same query read twice -> the same contract_digest: "
            f"{results[0].contract_digest == repeat.contract_digest}"
        )
        _check(
            results[0].contract_digest == repeat.contract_digest,
            "two identical reads produced different contract digests; the digest is not a contract",
        )
        _check(
            repeat.contract_digest != memory.read(query=queries[1], scope=SCOPE, motive=squeezed).contract_digest,
            "two different queries produced the same contract digest",
        )

        # ------------------------------------------------------------ 8: deep
        rule("8. DEEP -- 'if needed', the header's node id is handed back to the ledger.")
        deep_node = _node_for_concept(graph, ingested.reconciled.bindings, "the agent-memory server")
        print(
            f"  node {deep_node.node_id}: {render_header(deep_node, entries=len(ledger.for_node(deep_node.node_id)))}"
        )
        print(f"  its {len(_texts(deep_node))} compressed fact(s) are what a reader normally gets:")
        for text in _texts(deep_node):
            print(f"    {text}")
        history = ledger.for_node(deep_node.ledger_key)
        print(f"\n  ledger.for_node({deep_node.ledger_key!r}) — {len(history)} entry/entries, oldest first,")
        print("  including anything superseded. This is the unbounded record behind the bounded node:\n")
        for entry in history:
            print_entry(entry)
            print()
        aliases = ledger.aliases(deep_node.node_id)
        if aliases:
            print(f"  aliases absorbed into this node ({len(aliases)}) — a merge recorded so it can be replayed:")
            for alias in aliases:
                print(
                    f"    {alias.alias_node_id} -> {alias.survivor_node_id}, "
                    f"{len(alias.moved_entry_ids)} entry/entries moved, receipt {alias.receipt_id}"
                )

        # -------------------------------------------- 9: receipts, final state
        rule("9. EVERY RECEIPT, THEN THE FINAL GRAPH AND THE WHOLE LEDGER")
        every = receipts.all(scope=SCOPE)
        print(f"  {len(every)} receipt(s), in emission order. The stream IS the audit:\n")
        for index, receipt in enumerate(every, 1):
            print_receipt(receipt, index=index)
        print()
        print_graph(graph, ledger, title="FINAL GRAPH")

        print("\n  EVERY NODE, AS AN AGENT RECEIVES IT -- the rendered block, verbatim:")
        for node in graph.list_nodes(scope=SCOPE):
            print()
            print_block(f"node({node.node_id})", memory.node(node_id=node.node_id), log=rendered_log, about_nodes=True)

        every_relation = graph.relations(scope=SCOPE)
        print(f"\n  EVERY RELATION -- {len(every_relation)} edge(s) over {len(graph.edge_types(scope=SCOPE))} type(s).")
        print("  A belief BETWEEN two concepts, with its own claim and its own evidence:")
        for relation in every_relation:
            source = graph.get_node(relation.source_id).name
            target = graph.get_node(relation.target_id).name
            marker = f"  until {relation.until}" if relation.until else ""
            print(
                f"    {relation.relation_id}  [{relation.source_id}] {source} -{relation.type}-> "
                f"[{relation.target_id}] {target}{marker}"
            )
            print(f"         claim   : {relation.claim}")
            print(f"         evidence: {relation.evidence} {list(relation.entry_ids)}")

        events = ledger.events(scope=SCOPE)
        print(f"\n  EVERY DREAM EVENT -- {len(events)} journalled mutation(s), oldest first.")
        print("  This is the history the replay proof above was computed from. One line each:")
        print("  the op, the nodes it touched, and what it did, summarised by explain.event_summary")
        print("  -- the same one-liner a node's own 'history:' section shows a reader:\n")
        for index, event in enumerate(events, 1):
            print(f"    {index:>3}. {event.op.value:<22} {list(event.node_ids)}")
            print(f"         {event_summary(event)}")

        whole = ledger.for_episode(EPISODE_ID) + ledger.for_episode(EPISODE_TWO_ID)
        print(f"\n  THE WHOLE LEDGER — {len(whole)} entry/entries over {EPISODE_ID} and {EPISODE_TWO_ID},")
        print("  append-only, unbounded, and the thing the graph is fully regenerable from:\n")
        for entry in whole:
            print_entry(entry)
            print()

        # ------------------------------------------------- live-behaviour checks
        rule("THE ASSERTIONS — a failure here is a finding about a PROMPT, not a flaky test")
        _run_live_checks(
            graph=graph,
            ledger=ledger,
            ingested_entries=ingested.entries,
            second_entries=second.entries,
            bindings=ingested.reconciled.bindings,
            gateway_read=results[0],
            gateway_block=gateway_block,
            gateway_explain=deep,
            snapshots=snapshots,
            searches=searches,
            post_squeeze=post_squeeze,
            mandates=mandates,
            read_log=read_log,
            rendered_log=rendered_log,
            prompts=tuple(chat.calls),
            reinforced=reinforced,
            proof=proof,
            edge_types=types_after,
            edge_type_ceiling=compaction_ceiling,
            renames=tuple(renames),
            motive=squeezed,
        )
        print("\n  ALL ASSERTIONS PASSED.")
        print(f"\n  TOTAL LIVE CHAT CALLS: {len(chat.calls)}")
        for stage in ("EXTRACT", "RECONCILE", "DREAM-NODE", "DREAM-GLOBAL"):
            made = [call for call in chat.calls if call.stage == stage]
            if made:
                print(f"    {stage:<12} {len(made):>2} call(s), {sum(c.seconds for c in made):6.2f}s total")
        print(f"    {'READ':<12}  0 call(s) — deterministic by design")
    finally:
        ledger.close()


async def repair_orphans(
    memory: CavemanMemory,
    graph: InMemoryGraph,
    ledger: CavemanLedger,
    glob: GlobalOutcome | None,
    *,
    chat: CountedChat,
    motive: CavemanMotive,
    stage: str,
) -> None:
    """Report what a global pass left behind, and prove it left nothing dangling.

    This used to be a repair. A merge absorbed a NAME, and a node that took no
    part in any op could be left holding a relation line naming it -- so the
    global pass marked those dirty, reported them as
    ``GlobalOutcome.orphaned_node_ids``, and this function re-dreamed them.

    #251 amendment D removed the failure rather than the repair: an edge carries
    node ids, ``graph.merge_nodes`` re-points the ones it absorbs, and a
    dangling endpoint is unrepresentable. So there is nothing to re-dream, and
    what is left is the check that says so.
    """
    if glob is not None:
        moved = [
            relation
            for relation in graph.relations(scope=SCOPE)
            if relation.last_seen == max(item.last_seen for item in graph.relations(scope=SCOPE))
        ]
        print(f"\n  RELATIONS AFTER THE PASS: {len(graph.relations(scope=SCOPE))} edge(s), none dangling.")
        print("  A merge re-points the edges of the nodes it absorbs and drops the self-loop")
        print("  that makes -- so no third node is left pointing at a name that went away.")
        for relation in moved[:3]:
            print(f"    {relation.relation_id}  {render_relation(relation, node_id=relation.source_id)}")
    check_relations_resolve(graph, motive, stage=f"dream-global-{stage}")
    print_graph(graph, ledger, title=f"GRAPH AFTER THE {stage.upper()} PASS")
    check_graph_invariants(graph, motive, stage=f"post-{stage}")


def _node_for_concept(graph: InMemoryGraph, bindings: Mapping[str, str], concept: str) -> Node:
    """The node the named concept's surface names resolved to.

    Looked up through the concept keywords rather than a node name, because the
    model chooses both the surface names and the node's name and neither is
    promised in advance.

    Two passes, because a forced merge may have absorbed the node reconcile
    created: first the binding whose id still exists, then any surviving node
    whose own name OR one of its aliases carries the keyword.

    The aliases are half of that second pass and not a nicety. ``merge_nodes``
    keeps the SURVIVOR's name and unions the absorbed node's name and aliases
    into the survivor's aliases, so after a squeeze the concept is reachable
    under a node called something else entirely -- which is the whole mechanism
    the search section is about. A run where the gateway absorbed
    ``agent-memory MCP server`` found this by failing here on a correct graph.
    """
    keywords = CONCEPT_KEYWORDS[concept]
    present = {node.node_id: node for node in graph.list_nodes(scope=SCOPE)}
    for name, node_id in bindings.items():
        if node_id in present and any(keyword in name.casefold() for keyword in keywords):
            return present[node_id]
    for node in present.values():
        surfaces = (node.name.casefold(), *(alias.casefold() for alias in node.aliases))
        if any(keyword in surface for keyword in keywords for surface in surfaces):
            return node
    raise DemoAssertionError(
        f"no node resolves for the concept {concept!r}: no binding survives and no surviving node's name or "
        f"aliases carry any of {list(keywords)}. Bindings were {dict(bindings)}"
    )


def _report_unsupported_convergence(bindings: Mapping[str, str], converged: set[str]) -> None:
    """Print, never assert, the convergence the design claims and the episode does not.

    Reported on every run rather than dropped, because a requirement that quietly
    stops being checked is worse than one that is openly not met. See
    :data:`UNSUPPORTED_CONVERGENCE` for the episode text and the one-clause
    remedy.
    """
    matched = {
        name: node_id
        for name, node_id in bindings.items()
        if any(keyword in name.casefold() for keyword in UNSUPPORTED_CONVERGENCE)
    }
    print("\n  FINDING — the design's second claimed convergence, NOT asserted:")
    if not matched:
        print("    no surface name named the C4 memory server at all this run.")
        return
    joined = {name: node_id for name, node_id in matched.items() if node_id in converged}
    print(f"    {sorted(matched)} resolved to {sorted(set(matched.values()))}")
    if joined:
        print("    It DID join the agent-memory node this run. The episode still never")
        print("    states the identity, so this is the model inferring it, not the prompt")
        print("    guaranteeing it — which is why it is reported and not asserted.")
    else:
        print("    It is its own node. The episode never states that the C4 memory server")
        print("    IS the agent-memory MCP server — turn 3 is about #245 and connectivity,")
        print("    turns 7 and 9 about tool counts and #248, sharing no identifier — so the")
        print("    router, which sees only each name's claims and is told to prefer 'new'")
        print("    when unsure, cannot converge them without guessing.")
        print("    REMEDY: one clause in turn 9 naming the server. The plan says to author")
        print("    that text verbatim, so it is a reviewer's decision, not this demo's.")


def _run_live_checks(
    *,
    graph: InMemoryGraph,
    ledger: CavemanLedger,
    ingested_entries: tuple[LedgerEntry, ...],
    second_entries: tuple[LedgerEntry, ...],
    bindings: Mapping[str, str],
    gateway_read: ReadResult,
    gateway_block: str,
    gateway_explain: str,
    snapshots: Sequence[ReadSnapshot],
    searches: Mapping[str, ReadResult],
    post_squeeze: ReadResult,
    mandates: Sequence[str],
    read_log: Sequence[ReadRecord],
    rendered_log: Sequence[Rendered],
    prompts: Sequence[ChatCall],
    reinforced: Sequence[str],
    proof: ReplayProof,
    edge_types: Mapping[str, int],
    edge_type_ceiling: int,
    renames: Sequence[str],
    motive: CavemanMotive,
) -> None:
    """The fourteen assertions only a live model can pass or fail.

    One to five are about what the episode became; six to eight are about
    whether it can be SEARCHED, which is what amendment A added; nine, twelve,
    thirteen and fourteen are amendment B's — that four different searches over
    one scope are four different answers, that a folded duplicate is reported,
    that every forced mandate names the evidence that chose it, and that a name
    still reaches its concept after compression; ten and eleven are amendment
    C's — that a query read leads with the node it named and emits less than the
    whole scope. All fourteen fail the run rather than warning: a prompt that
    stops landing is a defect, and a warning nobody reads is how a requirement
    quietly stops being checked.

    *snapshots* carry the search section's reads together with the nine-node
    graph they answered from, because the graph itself is three nodes by the time
    this runs.
    """
    # 1 — the episode yielded a real claim set, including its own correction.
    _check(
        len(ingested_entries) >= 10,
        f"only {len(ingested_entries)} claim(s) extracted from ten turns; the extract rubric is not landing",
    )
    superseding = [entry for entry in ingested_entries if entry.supersedes is not None]
    _check(
        len(superseding) >= 1,
        "no entry has 'supersedes' set: the model did not notice turn 9 correcting turn 7's tool count",
    )
    print(f"  [1/21] {len(ingested_entries)} claims extracted, {len(superseding)} with supersedes set.  PASS")

    # 2 — every concept the episode names twice AND states the identity of
    #     resolves to exactly one node id.
    node_ids: set[str] = set()
    for concept, keywords in CONCEPT_KEYWORDS.items():
        matched = {name: node_id for name, node_id in bindings.items() if any(k in name.casefold() for k in keywords)}
        _check(matched != {}, f"no surface name matched the concept {concept!r}; bindings were {sorted(bindings)}")
        _check(
            len(matched) >= 2,
            f"{concept!r} was named once, not twice ({sorted(matched)}); the episode names it more "
            f"than once, so extract did not carry the surface words through",
        )
        resolved = set(matched.values())
        _check(
            len(resolved) == 1,
            f"{concept!r} did not converge: {matched} resolved to {len(resolved)} node ids. "
            f"Two names for one thing must bind to one node — this is the reconcile prompt failing",
        )
        node_ids |= resolved
        print(f"  [2/21] {concept!r}: {sorted(matched)} -> one node id {sorted(resolved)[0]}.  PASS")
    _check(
        len(node_ids) == len(CONCEPT_KEYWORDS),
        f"the {len(CONCEPT_KEYWORDS)} concepts collapsed into {len(node_ids)} node id(s)",
    )
    _report_unsupported_convergence(bindings, node_ids)

    # 3 - the rules of turns 1 and 10 survive compression, carry the RULE kind,
    #     and reach a read.
    #
    #     Three clauses, and the kind is the one #251 amendment D package D-C
    #     made assertable: the dreamer now answers in named fields, so a rule
    #     arrives as ``kind="rule"`` rather than as an attribute that happens to
    #     read like one -- and only the kind puts it under
    #     ``rank.CONSTRAINT_FLOOR``, which is what makes "an agent cannot search
    #     its way past a rule" true rather than lucky.
    survived = [
        fact
        for node in graph.list_nodes(scope=SCOPE)
        for fact in node.facts
        if any(wording in fact.text.lower() for wording in RULE_WORDINGS)
    ]
    _check(
        survived != [],
        f"no node holds either rule the episode states ({list(RULE_WORDINGS)}); the dream rubric's "
        f"'never drop a hard rule' is not landing",
    )
    as_rules = [fact for fact in survived if fact.kind is FactKind.RULE]
    _check(
        as_rules != [],
        f"the rule(s) survived as {sorted({fact.kind.value for fact in survived})} and not one of them is a "
        f"{FactKind.RULE.value!r}: {[fact.text for fact in survived]}. Only the kind puts a fact under "
        f"rank.CONSTRAINT_FLOOR, so a rule recorded as an attribute is a rule a search can get past",
    )
    emitted = [
        line for line in gateway_read.rendered.splitlines() if any(wording in line.lower() for wording in RULE_WORDINGS)
    ]
    _check(
        emitted != [],
        f"the gateway read emitted neither rule even though the graph holds {[fact.text for fact in survived]}",
    )
    print(
        f"  [3/21] {len(survived)} rule(s) survived compression, {len(as_rules)} carrying the "
        f"{FactKind.RULE.value!r} kind; the gateway read emits {len(emitted)}.  PASS"
    )
    for fact in survived:
        print(f"        {fact.kind.value}: {fact.text}")

    # 4 - every relation points at something real, and carries its own evidence.
    held = {node.node_id for node in graph.list_nodes(scope=SCOPE)}
    relations = graph.relations(scope=SCOPE)
    for relation in relations:
        _check(
            {relation.source_id, relation.target_id} <= held,
            f"{relation.relation_id} joins {relation.source_id} to {relation.target_id}, and the scope "
            f"holds {sorted(held)}",
        )
        _check(
            relation.entry_ids != (),
            f"{relation.relation_id} asserts {relation.claim!r} on no ledger evidence at all",
        )
    print(
        f"  [4/21] {len(relations)} relation(s) over {len(graph.edge_types(scope=SCOPE))} type(s), "
        f"every endpoint real and every edge evidenced.  PASS"
    )

    # 5 — the superseded text lives in the ledger and not on the node.
    for entry in superseding:
        older_id = entry.supersedes
        _check(older_id is not None, "a superseding entry lost its pointer")
        older = ledger.get(str(older_id))
        reachable = [
            node_id
            for node_id in older.node_ids
            if any(found.entry_id == older.entry_id for found in ledger.for_node(node_id))
        ]
        _check(
            reachable != [],
            f"the superseded claim {older.entry_id} is not reachable through ledger.for_node on any of its "
            f"nodes {list(older.node_ids)}; the deep read cannot recover what the node dropped",
        )
        repeated = [text for node in graph.list_nodes(scope=SCOPE) for text in _texts(node) if older.claim in text]
        _check(
            repeated == [],
            f"a surviving line repeats the superseded claim verbatim: {repeated}",
        )
        print(
            f"  [5/21] superseded {older.entry_id} reachable via ledger.for_node{reachable}, "
            f"repeated on no node's lines.  PASS"
        )
        print(f"        superseded: {older.claim}")
        print(f"        surviving : {entry.claim}")

    # 6 — the two floors, in order: what was NAMED, then the rules, then the rest.
    #
    #     Stated in the form the design can actually keep. Both floors order the
    #     RANKING and a read renders per-node BLOCKS, so "every rule before
    #     any other line in the text" is true only while every constraint sits on
    #     ONE node: with two constraint-holding nodes, the first one's ':' line
    #     necessarily precedes the second one's. Three clauses hold always,
    #     and together they are the property that matters -- an agent cannot
    #     search its way past a rule, and is not made to read past two of
    #     somebody else's rules to reach the node it named:
    #
    #       a. the read's blocks partition into exact hits, then constraint
    #          holders, then the rest -- EXACT_FLOOR above CONSTRAINT_FLOOR above
    #          the measured set. A brief has no exact hits, so for it this is the
    #          old clause unchanged: constraint holders lead;
    #       b. inside a block, the rules come first;
    #       c. a brief, which ranks the whole scope, drops no constraint at all.
    #
    #     Checked over every read the search section made, through the snapshots
    #     taken at the time: these reads answered from the nine-node graph, and
    #     the blocks cannot be re-split now that step 6 has moved entries onto
    #     survivors.
    brief_snapshot = snapshots[0]
    for snapshot in snapshots:
        seeded_exact = {hit.node_id for hit in snapshot.result.seeds if hit.kind != "knn"}
        tiers = tuple(
            0 if node.node_id in seeded_exact else 1 if any(_is_rule(t) for t in _texts(node)) else 2
            for node, _body in snapshot.blocks
        )
        _check(
            list(tiers) == sorted(tiers),
            f"read {snapshot.label} rendered its blocks out of floor order {tiers} ({snapshot.order}): "
            f"rank.EXACT_FLOOR must put every exactly-named node first and rank.CONSTRAINT_FLOOR "
            f"every rule next",
        )
        for node, body in snapshot.blocks:
            leading = tuple(text for text in body if _is_rule(text))
            _check(
                body[: len(leading)] == leading,
                f"read {snapshot.label}: inside {node.name}'s block the rules are not first: {list(body)}",
            )
    _check(
        all(hit.kind == "knn" for hit in brief_snapshot.result.seeds),
        "brief() reported a seed, so it was given a query it does not take",
    )
    rendered_lines = brief_snapshot.result.rendered.splitlines()
    missing = [wording for wording in RULE_WORDINGS if not any(wording in line.lower() for line in rendered_lines)]
    _check(
        missing == [],
        f"brief() dropped {len(missing)} rule it must never drop: {missing}. A brief ranks the whole "
        f"scope, so a rule it does not carry is one nothing will surface",
    )
    print(
        f"  [6/21] {len(snapshots)} read(s): exactly-named nodes lead and the rules are first inside every "
        f"block; brief() has no exact hits and kept both rules in {len(rendered_lines)} rendered line(s).  PASS"
    )
    for wording in RULE_WORDINGS:
        print(f"        {wording}")

    # 7 — the identifier query is an EXACT hit, not a near neighbour.
    #
    #     Two clauses. The ledger MUST index the identifier -- that is extract
    #     carrying identifiers through verbatim, and it is what makes an
    #     identifier searchable at all. And the read MUST seed exactly on the
    #     token, by either exact mechanism: `read._exact_seeds` tries aliases
    #     before the identifier index and dedupes by node id, so a node the
    #     dreamer happens to have NAMED `#246` is claimed as an alias hit and
    #     the identifier index never gets to claim it. Both are exact, both
    #     carry the token, and insisting on one of them would assert an
    #     ordering detail rather than the property that matters: an identifier
    #     is matched, not measured. #245 and #246 embed nearly identically and
    #     similarity alone cannot separate them.
    identifier_read = searches["b"]
    indexed = ledger.identifiers(scope=SCOPE)
    _check(
        IDENTIFIER_QUERY in indexed,
        f"the ledger's identifier index does not hold {IDENTIFIER_QUERY!r}: {sorted(indexed)}. Extract must "
        f"carry an episode's identifiers through verbatim or nothing can be searched by one",
    )
    named = [hit for hit in identifier_read.seeds if (hit.token or "") == IDENTIFIER_QUERY]
    exact = [hit for hit in named if hit.kind != "knn"]
    _check(
        exact != [],
        f"read({IDENTIFIER_QUERY!r}) has no exact seed on that token: "
        f"{[(hit.node_id, hit.kind, hit.token) for hit in identifier_read.seeds]}. The ledger indexes it "
        f"({list(indexed[IDENTIFIER_QUERY])}), so the read owes an exact hit rather than a measured one",
    )
    print(
        f"  [7/21] read({IDENTIFIER_QUERY!r}): indexed on {list(indexed[IDENTIFIER_QUERY])}, "
        f"{len(exact)} exact seed(s) {[(hit.node_id, hit.kind) for hit in exact]}.  PASS"
    )
    if {hit.kind for hit in exact} == {"alias"}:
        print(f"        Claimed as an ALIAS hit: a node is named {IDENTIFIER_QUERY!r} (or holds it as an alias),")
        print("        and aliases are tried before the identifier index, which dedupes by node id.")

    # 8 — the name I know reaches the node that holds the answer, and by which mechanism.
    #
    #     Two clauses, because only one of them is unconditional. The read MUST
    #     land on the node carrying the corrected tool count -- that is what
    #     searching by a name I know is for. Whether the seed is exact is owed
    #     exactly when an exact index holds the token, and that is checkable
    #     from the stores rather than assumed: if reconcile recorded `C4` as an
    #     alias, or extract carried it as an identifier, an exact seed is owed.
    name_read = searches["c"]
    answer = [node for node in name_read.nodes if any(TOOL_COUNT in text for text in _texts(node))]
    _check(
        answer != [],
        f"read({NAME_QUERY!r}) emitted {[node.name for node in name_read.nodes]}, none of which carries the "
        f"corrected tool count {TOOL_COUNT!r}. Searching by the name I know must reach the node that holds "
        f"the answer -- this is the alias contract, or the dream dropped the correction",
    )
    aliased = graph.node_by_alias(scope=SCOPE, name=NAME_QUERY)
    keys = [key for key in indexed if key.casefold() == NAME_QUERY.casefold()]
    named = [hit for hit in name_read.seeds if (hit.token or "").casefold() == NAME_QUERY.casefold()]
    mechanisms = sorted({hit.kind for hit in named}) or sorted({hit.kind for hit in name_read.seeds})
    if aliased is not None or keys:
        where_held = "an alias" if aliased is not None else f"the identifier index {keys}"
        _check(
            named != [] and {hit.kind for hit in named} <= {"alias", "identifier"},
            f"{NAME_QUERY!r} is held as {where_held}, so the read owes an exact seed on it and produced "
            f"{[(hit.node_id, hit.kind, hit.token) for hit in name_read.seeds]}",
        )
    print(
        f"  [8/21] read({NAME_QUERY!r}) reaches {[node.name for node in answer]} "
        f"(holds {TOOL_COUNT!r}) via {mechanisms}.  PASS"
    )
    if aliased is not None:
        print(f"        {NAME_QUERY!r} is a recorded alias of {aliased.name} ({aliased.node_id}) -- an EXACT hit.")
    elif keys:
        print(f"        {NAME_QUERY!r} is in the ledger's identifier index {keys} -- an EXACT hit.")
    else:
        print(f"        FINDING: {NAME_QUERY!r} is neither a recorded alias nor an indexed identifier this run,")
        print(f"        so it was reached by {mechanisms} -- measured similarity. Reconcile records the surface")
        print("        names it ROUTES, and the router never saw a bare 'C4' as a name of its own: the")
        print("        episode says 'the C4 memory server'. The read still lands on the answer, and the")
        print("        remedy is a routing question rather than a read one.")

    # 9 — four searches over one scope are four different answers.
    #
    #     Amendment B's first symptom, in the form the design can keep. The
    #     section used to run after the squeeze, and a three-node scope with
    #     read_k=8 hands every read the whole graph: four queries, one answer,
    #     nothing on the page about seeding. Two clauses, deliberately different
    #     in strength:
    #
    #       a. the four reads do NOT all emit the same block order. Once a scope
    #          fits inside one read, order is the whole of what a query decides;
    #     There is deliberately NO clause here about a read excluding part of
    #     the scope, and the two live runs that produced this file are why. With
    #     `read_k` at 8 and this episode producing 9 nodes, a query seeds eight
    #     of them and 1-hop expansion reaches any ninth that is linked to a
    #     seed: one run had one read exclude one node, the next had none. That
    #     is a property of the EPISODE's link structure, not of the read side,
    #     and an assertion over it would fail on a graph that is behaving
    #     correctly. The section above REPORTS the membership table and names
    #     the finding; the read-side bound is asserted deterministically in
    #     tests/test_caveman_pipeline.py, on twelve unlinked nodes against
    #     read_k=8, where the arithmetic leaves no room for luck.
    scope_size = len(snapshots[0].scope_names)
    distinct = {snapshot.order for snapshot in snapshots}
    _check(
        len(distinct) >= 2,
        f"all {len(snapshots)} searches over the same {scope_size}-node scope returned the identical block "
        f"order {list(snapshots[0].order)}. Seeding and rank must make a query's answer its own -- this is "
        f"the symptom amendment B moved this section before the squeeze to fix",
    )
    selective = [snapshot.label for snapshot in snapshots[1:] if snapshot.absent]
    print(
        f"  [9/21] {len(distinct)} distinct block order(s) from {len(snapshots)} read(s) over a "
        f"{scope_size}-node scope (query read(s) that excluded part of it: {selective or 'none'}).  PASS"
    )
    for snapshot in snapshots:
        left = (
            f"not emitted: {list(snapshot.absent)}"
            if snapshot.absent
            else "the whole scope (brief has no read_k; a query read here is read_k + 1-hop wide)"
        )
        print(f"        {snapshot.label}: {len(snapshot.result.nodes)} of {scope_size} -> {list(snapshot.order)}")
        print(f"           {left}")

    # 10 — the node I named leads the read that named it, and the read is smaller
    #      than the scope.
    #
    #      Amendment C's two symptoms, asserted on the read that showed them.
    #      `read('C4')` used to come back THIRD behind two constraint-holding
    #      nodes and to emit all nine nodes in the scope; now `rank.EXACT_FLOOR`
    #      puts the named node first and `motive.knn_min_similarity` keeps the
    #      0.13-0.17 neighbours out of the seeding altogether.
    #
    #      Both clauses are resolved against the SNAPSHOT and not against the
    #      live graph. The snapshot's nodes are the records the read rendered,
    #      frozen before step 6 merged anything; by the time these checks run,
    #      `graph.node_by_alias(name='C4')` answers about the three-node graph,
    #      where the merge has unioned that alias onto a survivor named
    #      something else. An earlier version of this check asked the live graph
    #      and failed a passing run — the read had led with the right node all
    #      along.
    name_snapshot = next(snapshot for snapshot in snapshots if snapshot.label == "c")
    _check(
        len(name_snapshot.result.nodes) < scope_size,
        f"read({NAME_QUERY!r}) emitted all {scope_size} node(s) in the scope, so the query selected nothing. "
        f"motive.knn_min_similarity={motive.knn_min_similarity} must keep the measured neighbours under it out "
        f"of the seeding, and the seeds report which they were: "
        f"{[(hit.node_id, round(hit.similarity, 4), hit.kept) for hit in name_snapshot.result.seeds]}",
    )
    name_exact = {hit.node_id for hit in name_snapshot.result.seeds if hit.kind != "knn"}
    led_with = name_snapshot.result.nodes[0]
    surfaces = {led_with.name.casefold(), *(alias.casefold() for alias in led_with.aliases)}
    if name_exact:
        _check(
            led_with.node_id in name_exact,
            f"read({NAME_QUERY!r}) led with {led_with.name} ({led_with.node_id}), which is not one of its "
            f"exact hits {sorted(name_exact)}. rank.EXACT_FLOOR must lift an exactly-named node above the "
            f"scope's other constraints",
        )
        _check(
            NAME_QUERY.casefold() in surfaces,
            f"read({NAME_QUERY!r}) led with {led_with.name} ({led_with.node_id}), whose surfaces are "
            f"{sorted(surfaces)} -- none of them the name that was searched for. An exact seed on a token "
            f"means the node carries that token as its name or an alias",
        )
    print(
        f"  [10/21] read({NAME_QUERY!r}) leads with {led_with.name} ({led_with.node_id}), an exact hit "
        f"carrying {NAME_QUERY!r}, and emits {len(name_snapshot.result.nodes)} of {scope_size} node(s).  PASS"
    )
    print(f"        not emitted: {list(name_snapshot.absent)}")
    floored = [hit for hit in name_snapshot.result.seeds if not hit.kept]
    for hit in floored:
        print(f"        dropped by the floor: {hit.node_id} at sim={hit.similarity:.4f}")
    if not name_exact:
        print(f"        FINDING: read({NAME_QUERY!r}) had no exact seed this run, so the leading clause was")
        print("        reported rather than asserted -- see the finding under [8].")

    # 11 — the identifier read leads with an exact hit.
    #
    #      The same floor, on the query where precision matters most. Check 7
    #      asserts that an exact seed EXISTS on the token; this asserts that it
    #      is what the reader sees first. An identifier is the least ambiguous
    #      thing a searcher can type, so a read that buries its exact hit under
    #      another node's rule has answered a question nobody asked.
    identifier_snapshot = next(snapshot for snapshot in snapshots if snapshot.label == "b")
    exact_ids = {hit.node_id for hit in identifier_snapshot.result.seeds if hit.kind != "knn"}
    first = identifier_snapshot.result.nodes[0]
    _check(
        first.node_id in exact_ids,
        f"read({IDENTIFIER_QUERY!r}) led with {first.name} ({first.node_id}), which is not one of its exact "
        f"hits {sorted(exact_ids)}. rank.EXACT_FLOOR must put the nodes the query named exactly first",
    )
    print(
        f"  [11/21] read({IDENTIFIER_QUERY!r}) leads with {first.name} ({first.node_id}), an exact hit of "
        f"{len(exact_ids)}.  PASS"
    )
    print(f"        block order: {list(identifier_snapshot.order)}")

    # 10 — a folded duplicate is in the receipt, and a read that folded nothing
    #      does not claim to have folded something.
    folded = [record for record in read_log if record.duplicates_dropped]
    for record in read_log:
        phrase = f"dropped {record.duplicates_dropped} duplicate fact(s)"
        if record.duplicates_dropped:
            _check(
                phrase in record.detail,
                f"read {record.label!r} folded {record.duplicates_dropped} duplicate fact(s) and its "
                f"READ_EMITTED detail does not say so: {record.detail!r}. A fold the audit stream does not "
                f"carry is a fact the reader was silently not told twice",
            )
        else:
            _check(
                "duplicate fact(s)" not in record.detail,
                f"read {record.label!r} folded nothing but its detail reports a fold: {record.detail!r}",
            )
    print(
        f"  [12/21] {len(read_log)} read(s) receipted; {len(folded)} folded a duplicate and each says so in "
        f"its READ_EMITTED detail.  PASS"
    )
    for record in folded:
        print(f"        {record.label}: {record.detail}")

    # 11 — every forced mandate names the evidence that chose its peer.
    _check(
        list(mandates) != [],
        "the forced passes mandated no merge at all, so N was never actually enforced by one",
    )
    for mandate in mandates:
        _check(
            any(phrase in mandate for phrase in MERGE_REASONS),
            f"a MUST MERGE mandate carries none of {list(MERGE_REASONS)}: {mandate!r}. The model must be told "
            f"whether it is folding together two things the ledger ties to each other or two that read alike",
        )
    print(f"  [13/21] {len(mandates)} MUST MERGE mandate(s), each naming its evidence.  PASS")
    for mandate in mandates:
        print(f"        {mandate}")

    # 12 — the name I know still reaches the concept after compression.
    #
    #      The one read worth repeating after the squeeze. A merge is where a
    #      search key is most likely to be lost: the node the answer was on may
    #      not exist any more. `merge_nodes` unions the absorbed node's name AND
    #      aliases into the survivor's, so the seed must still be EXACT -- a
    #      measured hit here would mean the alias was dropped and the read got
    #      lucky on similarity.
    exact_after = [
        hit for hit in post_squeeze.seeds if hit.kind != "knn" and (hit.token or "").casefold() == NAME_QUERY.casefold()
    ]
    _check(
        exact_after != [],
        f"after the squeeze read({NAME_QUERY!r}) has no exact seed on that token: "
        f"{[(hit.node_id, hit.kind, hit.token) for hit in post_squeeze.seeds]}. A merge must carry the "
        f"absorbed node's aliases onto its survivor, or compression loses the name a reader searches by",
    )
    landed = [node for node in post_squeeze.nodes if any(TOOL_COUNT in text for text in _texts(node))]
    _check(
        landed != [],
        f"after the squeeze read({NAME_QUERY!r}) emitted {[node.name for node in post_squeeze.nodes]}, none of "
        f"which carries the corrected tool count {TOOL_COUNT!r}. Either the alias no longer reaches the "
        f"concept or the merge dropped the fact",
    )
    print(
        f"  [14/21] after the squeeze read({NAME_QUERY!r}) seeds "
        f"{[(hit.node_id, hit.kind) for hit in exact_after]} and lands on "
        f"{[node.name for node in landed]}, which holds {TOOL_COUNT!r}.  PASS"
    )
    for node in landed:
        print(f"        aka {', '.join(node.aliases) or '(none)'}")

    # 15 -- no character the design removed reaches a model or an agent.
    #
    #      Amendment D's rendering rule, checked over the whole of what anything
    #      but a person reads: every block this run rendered AND every prompt it
    #      sent. The four characters were FORMAT once -- a separator, a relation
    #      arrow, a refutation marker, an approximation marker -- and each cost a
    #      legend the prompt had to carry and the model had to apply. Model-authored
    #      fact and claim TEXT may be any Unicode it likes; what may not is the
    #      format around it.
    read_by_a_model: list[tuple[str, str]] = [(f"render {item.label}", item.text) for item in rendered_log]
    read_by_a_model += [(f"prompt {call.stage} #{index}", call.prompt) for index, call in enumerate(prompts, 1)]
    read_by_a_model += [
        (f"system prompt {call.stage} #{index}", call.system_prompt) for index, call in enumerate(prompts, 1)
    ]
    for where, text in read_by_a_model:
        for character, name in FORMAT_CHARACTERS_THE_DESIGN_REMOVED:
            _check(
                character not in text,
                f"{where} contains the {name} ({character!r}), which amendment D removed from the format. "
                f"Every line of it is printable ASCII words, brackets, commas and colons",
            )
    non_ascii = sorted({character for _label, body in read_by_a_model for character in body if not character.isascii()})
    print(
        f"  [15/21] {len(rendered_log)} render(s) and {len(prompts)} prompt(s) carry none of the four "
        f"characters the design removed.  PASS"
    )
    print(f"        non-ASCII characters present at all (model-authored text only): {non_ascii or 'none'}")

    # 16 -- every rendered block carries the node id the traversal calls take.
    #
    #      The reason the ids are in the rendering rather than being an internal
    #      detail: read, walk, read. A block with no id is an answer an agent can
    #      read and cannot follow.
    about_nodes = [item for item in rendered_log if item.about_nodes]
    for item in about_nodes:
        _check(
            NODE_ID_IN_BRACKETS.search(item.text) is not None,
            f"the rendered {item.label} carries no [n-...] node id, so nothing in it can be walked "
            f"to: {item.text.splitlines()[:2]}",
        )
    ids_seen = {found for item in about_nodes for found in NODE_ID_IN_BRACKETS.findall(item.text)}
    print(
        f"  [16/21] every one of {len(about_nodes)} render(s) about concepts carries a node id; "
        f"{len(ids_seen)} distinct.  PASS"
    )
    for item in rendered_log:
        if not item.about_nodes:
            print(f"        not asked for one: {item.label} -- about a scope and its journal, not a node")

    # 17 -- every relation type is in the vocabulary's shape, and M held.
    for relation in graph.relations(scope=SCOPE):
        _check(
            re.fullmatch(EDGE_TYPE_PATTERN, relation.type) is not None,
            f"{relation.relation_id} carries the type {relation.type!r}, which is not UPPER_SNAKE within "
            f"32 characters. A type that is a sentence is not a vocabulary",
        )
    _check(
        len(edge_types) <= edge_type_ceiling,
        f"the scope holds {len(edge_types)} edge type(s) over the M={edge_type_ceiling} the compaction pass "
        f"ran under: {dict(edge_types)}",
    )
    _check(
        list(renames) != [],
        f"the compaction pass was given M={edge_type_ceiling}, one less than the vocabulary it started with, "
        f"and renamed no edge at all. The MUST COMPACT mandate is not landing",
    )
    print(
        f"  [17/21] {len(graph.relations(scope=SCOPE))} relation(s), every type well formed, "
        f"{len(edge_types)} type(s) within M={edge_type_ceiling}, {len(renames)} edge(s) re-labelled.  PASS"
    )
    for line in renames:
        print(f"        {line}")

    # 18 -- a restated belief gained evidence instead of becoming a second record.
    #
    #      The point of the second episode. Turns 1, 2 and 4 restate things episode
    #      one already recorded, so what must grow is the EVIDENCE on records that
    #      already exist -- not the number of records. A run where nothing
    #      reinforced would mean the matcher routed a recap to new nodes and new
    #      edges, which is duplication wearing reinforcement's clothes.
    multi = _multi_evidence(graph)
    _check(
        list(reinforced) != [] or multi != (),
        f"neither episode's claims reinforced anything: no fact or edge in the scope is supported by more "
        f"than one ledger entry, and {len(second_entries)} claim(s) from {EPISODE_TWO_ID} restated things "
        f"{EPISODE_ID} had already recorded. This is the matcher failing to match",
    )
    print(
        f"  [18/21] {len(reinforced)} record(s) gained evidence across the two episodes; "
        f"{len(multi)} fact(s)/edge(s) now carry more than one entry.  PASS"
    )
    for line in (*reinforced, *multi):
        print(f"        {line}")

    # 19 -- an attribute superseded ACROSS episodes leaves the node and stays in the record.
    #
    #      Two clauses, and they are the two halves of what supersession means.
    #      The new value must be what a reader gets; the old one must still be
    #      recoverable, and only through the deep read.
    #
    #      Note what is NOT asserted: that the old value is absent from the render
    #      altogether. Episode one's turn 10 names claude-haiku-4-5 as an EXAMPLE
    #      of an undated alias in a rule about naming, which is a different claim
    #      from "it is the default" and must survive. So the clause is about the
    #      DEFAULT specifically.
    default_lines = [line for line in gateway_block.splitlines() if "default" in line.lower()]
    current = [line for line in default_lines if CHAT_DEFAULT_NOW in line]
    # A line naming BOTH values is a correct compression -- "moved to X; Y no
    # longer the default" records the change, which is more useful than the new
    # value alone. What must not exist is a line naming the OLD value as the
    # default with no mention of the new one, which is the node still holding a
    # value the record replaced.
    stale = [line for line in default_lines if CHAT_DEFAULT_WAS in line and CHAT_DEFAULT_NOW not in line]
    _check(
        current != [],
        f"no line of the gateway's block names {CHAT_DEFAULT_NOW!r} as the chat default, though "
        f"{EPISODE_TWO_ID} turn 3 says it moved there: {default_lines}",
    )
    _check(
        stale == [],
        f"the gateway's block names {CHAT_DEFAULT_WAS!r} as the chat default without naming the value that "
        f"replaced it: {stale}. A replaced value leaves the node, or is kept only beside the one that "
        f"replaced it -- that is what makes the node a compression rather than a log",
    )
    _check(
        CHAT_DEFAULT_WAS in gateway_explain,
        f"explain() does not hold {CHAT_DEFAULT_WAS!r} anywhere, so the superseded value is unrecoverable. "
        f"The bounded node may drop it; the record may not",
    )
    print(f"  [19/21] the chat default reads {CHAT_DEFAULT_NOW!r} and the superseded value survives in explain.  PASS")
    for line in current:
        print(f"        current : {line}")
    for line in gateway_explain.splitlines():
        if CHAT_DEFAULT_WAS in line:
            print(f"        record  : {line.strip()}")

    # 20 -- the journal accounts for the compressed graph, belief for belief.
    _check(
        proof.equal,
        f"the replayed graph does not match the live one: {proof.live_digest} vs {proof.replayed_digest} over "
        f"{proof.event_count} journalled event(s). Compression nobody can replay is compression nobody can audit",
    )
    _check(
        proof.event_count > 0,
        "the scope was ingested, dreamt, squeezed and compacted and journalled zero events",
    )
    print(
        f"  [20/21] {proof.event_count} journalled event(s) replay to the live digest {proof.live_digest[:16]}.  PASS"
    )

    # 21 -- the call budget was actually exercised, stage by stage.
    #
    #      A run that made three calls would pass most of the checks above and
    #      prove very little: two episodes, two incremental dreams, a free pass, a
    #      squeeze and a compaction is more than a dozen calls, and every stage of
    #      the design has to have been reached at least once.
    per_stage = {stage: [call for call in prompts if call.stage == stage] for stage in LIVE_STAGES}
    for stage, made in per_stage.items():
        _check(made != [], f"the run made no {stage} call at all, so that stage was never exercised live")
    _check(
        len(prompts) >= MINIMUM_LIVE_CALLS,
        f"the run made {len(prompts)} live call(s), under the {MINIMUM_LIVE_CALLS} this demo's path requires: "
        f"{ {stage: len(made) for stage, made in per_stage.items()} }",
    )
    print(f"  [21/21] {len(prompts)} live call(s) across {len(per_stage)} stage(s), every stage reached.  PASS")
    for stage, made in per_stage.items():
        print(f"        {stage:<12} {len(made):>2} call(s)")


# ======================================================================== main


def main() -> int:
    """Load the repo-root ``.env``, then refuse to run without a gateway key."""
    load_env_file(Path(__file__).resolve().parents[1] / ".env")
    if not os.environ.get(GATEWAY_API_KEY_ENV, "").strip():
        print(
            f"ERROR: examples/caveman_demo.py is live-only and requires {GATEWAY_API_KEY_ENV} in the "
            f"repo-root .env or the process environment. Add the JedAI Gateway virtual key as "
            f"{GATEWAY_API_KEY_ENV} before running the demo.",
            file=sys.stderr,
        )
        return 2
    try:
        asyncio.run(run())
    except DemoAssertionError as failure:
        print(f"\nDEMO ASSERTION FAILED: {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
