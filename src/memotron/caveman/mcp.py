"""The MCP surface: ten tools over one :class:`~memotron.caveman.pipeline.CavemanMemory`.

The whole subsystem as an agent sees it (#251 amendment D, package D-D). Every
tool returns **plain text the agent reads as it arrives** -- no JSON envelope, no
second schema to teach. The text is the product: a rendered block already carries
the node's id, its evidence counts and the calls to make next, so wrapping it in
a structure would only give the agent something to unwrap before reading the same
words.

.. rubric:: Data flow

    memory_ingest   turns -> extract -> reconcile -> ledger claims + node bindings
    memory_dream    dirty nodes -> facts and typed edges, every mutation journalled
    memory_brief    scope -> ranked blocks, no query, no model call
    memory_read     query -> seeds -> one hop -> ranked blocks
    memory_node     node id -> one block
    memory_neighbors node id -> one line per edge, with the id on the other end
    memory_explain  node id -> every claim, dated, with supersession
    memory_replay   scope -> the content digest and the journalled event count
    memory_erase    episode id -> claims deleted, orphan nodes deleted
    memory_contract  -> AGENTS.md

.. rubric:: One lazily built runtime, and one seam for a test to replace it

:func:`runtime` builds once, on first use, from the environment. Lazily because
importing this module must not require a gateway key or touch the filesystem --
``tests/api_surface.golden.txt`` imports every module in the package, and a
server whose import opened a SQLite file would make that a write.

The one supported way to put a different memory behind the tools is
:func:`install_runtime_factory`: a FACTORY, not an instance, so the lazy
construction stays the only construction path and a test cannot leave a
half-built runtime behind. Nothing here reads a module global that a caller is
expected to assign.

.. rubric:: Live only

``memory_read``, ``memory_ingest`` and ``memory_dream`` reach the JedAI Gateway
-- read embeds its query, the other two call the model. With
``LITELLM_API_KEY`` unset each returns :data:`GATEWAY_SETUP_ERROR`, one line
naming the variable. That is a precondition, not an offline mode: there is no
second code path, no local model and no cached answer, and every other tool
works because it genuinely needs no gateway at all.

.. rubric:: Every tool is one call into ``CavemanMemory``, and nothing else

No tool body reads a store. ``memory_node``, ``memory_neighbors`` and
``memory_replay`` briefly had local implementations here, written against the
graph and the ledger while ``CavemanMemory`` lacked the three methods; they are
gone, and the rendering an agent reads is now the same text the pipeline hands
every other caller. One implementation, so this server and the demo cannot
disagree about what a node looks like.

.. rubric:: Why the graph handed to the memory is the RAW store

``CavemanMemory.__init__`` wraps it in ``JournaledGraph`` itself, so every
mutation is a ``DreamEvent`` in the ledger with the content before and after --
which is what makes ``memory_replay`` a proof rather than a claim. Wrapping it
here as well would journal every mutation twice, so what this module composes is
the plain store and what it holds afterwards for seeding and inspection is
``memory.graph``, the wrapper.

The graph is in memory and the ledger is on disk, which is the subsystem's own
arrangement: the graph is a bounded compression of the ledger and regenerable
from it, so the durable thing is the claim stream and the journal, not the
projection.

.. rubric:: The transport boundary is not in this module

This module builds the ``FastMCP`` object and its tools and nothing else. Binding
a host and port, the ``Host`` allowlist and the uvicorn server are
``memotron.runtime.serve_mcp_http``'s -- the repo's single way of serving an
MCP server over HTTP -- and ``examples/caveman_mcp_server.py`` calls it. FastMCP 4
has no instance ``.settings`` and ``FastMCP(host=...)`` raises, so the
construct-then-mutate defect that once bound ``0.0.0.0`` while allowing only
localhost ``Host`` headers cannot be expressed here at all.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from memotron.caveman import guidance
from memotron.caveman.gateway import CavemanChatTransport
from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import Episode, Turn
from memotron.caveman.motive import CavemanMotive, assistant_motive, engineering_motive
from memotron.caveman.pipeline import CavemanMemory, UtcClock
from memotron.caveman.read import ReadResult
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.seams import Clock, GraphStore, LedgerStore, ReceiptSink
from memotron.embedding import OpenAICompatibleEmbeddingTransport
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_EMBEDDING_MODEL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
)

LEDGER_PATH_ENV = "CAVEMAN_LEDGER_PATH"
"""Where the claim ledger and the event journal live. One sqlite file."""

DEFAULT_LEDGER_PATH = ".memotron/caveman.sqlite"
"""Beside the other Memotron stores, and gitignored like them."""

MOTIVE_ENV = "CAVEMAN_MOTIVE"
"""Which persona's budgets and rubric this server runs under."""

DEFAULT_MOTIVE = "engineering"

MOTIVES: Mapping[str, Callable[[], CavemanMotive]] = {
    "engineering": engineering_motive,
    "assistant": assistant_motive,
}
"""The two presets, by name. A name outside this map is a hard failure.

Not a free-form motive: a motive is a policy over what a scope KEEPS, so a
typo that silently fell back to a default would quietly re-shape a graph that is
already written, and the damage would only be visible several dreams later.
"""

CHAT_MODEL_ENV = "CAVEMAN_CHAT_MODEL"
EMBEDDING_MODEL_ENV = "CAVEMAN_EMBEDDING_MODEL"
GATEWAY_BASE_URL_ENV = "LITELLM_API_BASE"

GATEWAY_SETUP_ERROR = (
    f"Not configured: set {GATEWAY_API_KEY_ENV} to a JedAI Gateway virtual key. "
    "This memory is live only -- ingest, dream and read all reach the gateway, "
    "and there is no offline mode."
)
"""What a gateway-backed tool answers when the key is not set. One line.

A returned string rather than a raised error because the caller is an agent, and
an agent that reads "set this variable" can say so to the human it is working
with, while a transport stack trace arriving as an MCP error is something it can
only report as a failure. Names the variable and nothing else -- never the value,
never a base URL that might carry one.
"""

TURN_KEYS = frozenset({"speaker", "text"})
"""Exactly the keys one element of ``turns_json`` carries.

``index`` is deliberately NOT among them: turn numbers come from the array order,
so an agent cannot mis-number them and there is no second way to express the same
sequence. A caller that sends anything else is told what the shape is.
"""

EMPTY_READ = "nothing recorded for this scope yet"
"""What a read or a brief answers when the scope has emitted no blocks."""

mcp = FastMCP(
    "caveman-memory",
    instructions=(
        "Caveman memory: a bounded compressed node graph over an append-only claim "
        "ledger and an append-only event journal. Call memory_contract once for the "
        "full contract. Call memory_brief(scope) at session start and again after "
        "every compaction; call memory_read(scope, query) before relying on a "
        "remembered fact. Every block carries its node id in brackets -- traverse with "
        "memory_neighbors and memory_node, and call memory_explain for provenance, "
        "dates and supersession. Call memory_ingest at compaction and at session end "
        "with the turns worth remembering, then memory_dream to compress; zero writes "
        "is a correct session and a secret must never be ingested. memory_replay "
        "proves the compression from the journal. Every tool returns plain text to be "
        "read as it arrives."
    ),
)


@dataclass(frozen=True)
class CavemanRuntime:
    """One composed memory plus the seams it was composed from.

    The memory is what every tool calls. The seams are carried alongside it for
    the things a tool does not do -- ``clock`` stamps an ingested episode's
    ``occurred_at``, and ``graph`` / ``ledger`` / ``receipts`` are what a
    deployment or a test inspects and seeds through. ``graph`` is
    ``memory.graph``, the JOURNALLED store, so a write through it is recorded
    like any other and a seeded scope still proves under ``memory_replay``.

    Frozen, and the motive is one of the fields: a server runs one persona, and
    a per-call motive would mean two personas writing one graph under different
    budgets.
    """

    memory: CavemanMemory
    graph: GraphStore
    ledger: LedgerStore
    receipts: ReceiptSink
    clock: Clock
    motive: CavemanMotive


def build_runtime_from_env() -> CavemanRuntime:
    """Compose the runtime this server serves, entirely from the environment.

    Six seams, all concrete, one arrangement:

    * the ledger is sqlite at :data:`LEDGER_PATH_ENV` (both streams, claims and
      events);
    * the graph is a plain ``InMemoryGraph``; ``CavemanMemory`` wraps it in
      ``JournaledGraph`` itself, and ``memory.graph`` is that wrapper -- so every
      mutation is journalled, and journalled exactly once;
    * chat and embeddings are the gateway transports, which resolve the API key
      at CALL time -- construction here therefore needs no credential, which is
      what lets an import-time or a read-only caller exist at all;
    * the motive is the preset named by :data:`MOTIVE_ENV`.

    Raises:
        ValueError: if :data:`MOTIVE_ENV` names something outside
            :data:`MOTIVES`. Fail fast: see that mapping.
    """
    motive_name = os.environ.get(MOTIVE_ENV, DEFAULT_MOTIVE).strip() or DEFAULT_MOTIVE
    if motive_name not in MOTIVES:
        raise ValueError(f"{MOTIVE_ENV}={motive_name!r} is not a known motive; use one of {', '.join(sorted(MOTIVES))}")
    motive = MOTIVES[motive_name]()

    clock = UtcClock()
    ledger = CavemanLedger(os.environ.get(LEDGER_PATH_ENV, DEFAULT_LEDGER_PATH))
    receipts = InMemoryReceipts()
    base_url = os.environ.get(GATEWAY_BASE_URL_ENV, DEFAULT_GATEWAY_BASE_URL)
    chat = CavemanChatTransport(
        model=os.environ.get(CHAT_MODEL_ENV, DEFAULT_GATEWAY_MODEL),
        base_url=base_url,
    )
    embedder = OpenAICompatibleEmbeddingTransport(
        model=os.environ.get(EMBEDDING_MODEL_ENV, DEFAULT_GATEWAY_EMBEDDING_MODEL),
        base_url=base_url,
    )
    memory = CavemanMemory(
        graph=InMemoryGraph(),
        ledger=ledger,
        receipts=receipts,
        chat=chat,
        embedder=embedder,
        clock=clock,
    )
    return CavemanRuntime(
        memory=memory,
        graph=memory.graph,
        ledger=ledger,
        receipts=receipts,
        clock=clock,
        motive=motive,
    )


_factory: Callable[[], CavemanRuntime] = build_runtime_from_env
_runtime: CavemanRuntime | None = None


def install_runtime_factory(factory: Callable[[], CavemanRuntime]) -> None:
    """Replace the builder :func:`runtime` uses, and drop whatever it already built.

    The one supported way to put a different memory behind the tools -- a test
    with a scripted transport and a temporary ledger, or an embedder that makes
    no network call. A factory rather than an instance so that construction
    still happens exactly once, in one place, on first use.

    Restore the default by passing :func:`build_runtime_from_env`.
    """
    global _factory, _runtime
    _factory = factory
    _runtime = None


def runtime() -> CavemanRuntime:
    """The one runtime, built on first use by the installed factory."""
    global _runtime
    if _runtime is None:
        _runtime = _factory()
    return _runtime


# -- what a tool answers with ---------------------------------------------------


def _gateway_configured() -> bool:
    """Whether a gateway key is set, read at call time exactly as a transport reads it."""
    return bool(os.environ.get(GATEWAY_API_KEY_ENV, "").strip())


def _render_read(result: ReadResult) -> str:
    """A read or a brief as text. ``rendered`` already carries the footer."""
    return result.rendered or EMPTY_READ


def _render_bindings(bindings: Mapping[str, str], created: Sequence[str]) -> list[str]:
    """One line per surface name: where it routed, and whether that node is new.

    Two names holding one node id is the within-batch reconciliation working, and
    it is visible here as two lines naming the same id rather than being folded
    away.
    """
    new = set(created)
    return [
        f"  {name} -> [{node_id}]{' (new)' if node_id in new else ''}" for name, node_id in sorted(bindings.items())
    ]


def _parse_turns(turns_json: str) -> tuple[Turn, ...]:
    """``[{"speaker": ..., "text": ...}, ...]`` to numbered turns, in array order.

    Raises:
        ValueError: on anything else -- not a JSON array, an element that is not
            an object, or an element whose keys are not exactly
            :data:`TURN_KEYS`. The message states the shape, because the caller
            is an agent that can correct itself from one sentence.
    """
    parsed: Any = json.loads(turns_json)
    if not isinstance(parsed, list) or not parsed:
        raise ValueError('turns_json must be a non-empty JSON array of {"speaker": ..., "text": ...} objects')
    turns: list[Turn] = []
    for position, element in enumerate(parsed, 1):
        if not isinstance(element, dict) or set(element) != TURN_KEYS:
            raise ValueError(
                f"turns_json element {position} must be an object with exactly the keys "
                f"{', '.join(sorted(TURN_KEYS))}; turn numbers come from the array order"
            )
        turns.append(Turn(index=position, speaker=str(element["speaker"]), text=str(element["text"])))
    return tuple(turns)


# -- the tools ------------------------------------------------------------------


@mcp.tool()
async def memory_contract() -> str:
    """Read the whole contract: what this memory is, the lifecycle, every tool, the format."""
    return guidance.agents_text()


@mcp.tool()
async def memory_brief(scope: str) -> str:
    """Read the scope with no query. Call at session start and after every compaction."""
    live = runtime()
    return _render_read(live.memory.brief(scope=scope, motive=live.motive))


@mcp.tool()
async def memory_read(scope: str, query: str) -> str:
    """Answer one query from the scope. Deterministic; embeds the query, so it needs the gateway."""
    if not _gateway_configured():
        return GATEWAY_SETUP_ERROR
    live = runtime()
    return _render_read(live.memory.read(query=query, scope=scope, motive=live.motive))


@mcp.tool()
async def memory_node(node_id: str) -> str:
    """Re-read one node as a block, with its edges. Takes an id a read handed you."""
    return runtime().memory.node(node_id=node_id)


@mcp.tool()
async def memory_neighbors(node_id: str) -> str:
    """List one node's edges, with the node id on the other end of each. Walk from here."""
    return runtime().memory.neighbors(node_id=node_id)


@mcp.tool()
async def memory_explain(node_id: str) -> str:
    """Provenance for one node: every claim, dated, with supersession. Unbounded, no model call."""
    return runtime().memory.explain(node_id=node_id)


@mcp.tool()
async def memory_ingest(scope: str, episode_id: str, turns_json: str) -> str:
    """Write one episode's claims and route them onto nodes. Two model calls; never a secret."""
    if not _gateway_configured():
        return GATEWAY_SETUP_ERROR
    live = runtime()
    episode = Episode(
        episode_id=episode_id,
        scope=scope,
        occurred_at=live.clock.now(),
        turns=_parse_turns(turns_json),
    )
    outcome = await live.memory.ingest(episode, live.motive)
    reconciled = outcome.reconciled
    return "\n".join(
        (
            f"episode: {outcome.episode_id}",
            f"scope: {outcome.scope}",
            f"claims appended: {outcome.entry_count}",
            f"claims routed this pass: {len(outcome.routed)}",
            f"nodes created: {len(reconciled.created)}",
            f"nodes bound: {len(reconciled.bound)}",
            f"nodes awaiting compression: {len(reconciled.dirty)}",
            f"relations created: {len(outcome.relations_created)}",
            f"relations reinforced: {len(outcome.relations_reinforced)}",
            f"new edge types: {', '.join(outcome.edge_types_new) or 'none'}",
            "surface names:",
            *_render_bindings(reconciled.bindings, reconciled.created),
            "node ids to traverse: " + ", ".join(outcome.node_ids),
            "next: memory_dream to write the facts and the edges.",
        )
    )


@mcp.tool()
async def memory_dream(scope: str) -> str:
    """Compress the scope: write facts and typed edges, reinforce, merge, hold the ceilings."""
    if not _gateway_configured():
        return GATEWAY_SETUP_ERROR
    live = runtime()
    outcome = await live.memory.dream(scope=scope, motive=live.motive)
    lines = [
        f"scope: {outcome.scope}",
        f"nodes recompressed: {outcome.incremental_count}",
        *(f"  [{node.node_id}] {node.name}" for node in outcome.dreamed),
    ]
    if outcome.glob is not None:
        lines.extend(
            (
                f"scope pass: {outcome.glob.ops_applied} ops applied",
                f"nodes before: {outcome.glob.count_before}, after: {outcome.glob.count_after}",
                f"pressure: {outcome.glob.pressure}",
            )
        )
    lines.append("next: memory_replay to prove it, memory_brief to read it.")
    return "\n".join(lines)


@mcp.tool()
async def memory_erase(scope: str, episode_id: str) -> str:
    """Delete one episode's claims and any node left unsupported. No model call."""
    live = runtime()
    outcome = live.memory.erase(episode_id=episode_id, scope=scope)
    return "\n".join(
        (
            f"scope: {scope}",
            f"episode: {outcome.episode_id}",
            f"claims deleted: {outcome.deleted_entry_count}",
            f"nodes deleted: {len(outcome.deleted_node_ids)} {list(outcome.deleted_node_ids)}",
            f"nodes awaiting recompression: {len(outcome.dirty_node_ids)} {list(outcome.dirty_node_ids)}",
            "next: memory_dream to recompress what is left.",
        )
    )


@mcp.tool()
async def memory_replay(scope: str) -> str:
    """The compression proof: rebuild the scope from its journal and compare both digests."""
    return runtime().memory.replay(scope=scope).rendered


async def health(request: Request) -> Response:
    """Liveness only. Deliberately outside the MCP middleware, like the other servers."""
    return Response("ok", media_type="text/plain")


# Registered by CALL and not by decorator, and the reason is the strict tier this
# package sits in: ``FastMCP.custom_route`` carries no return annotation, so
# `@mcp.custom_route(...)` is an untyped decorator and mypy reports the handler
# it wraps as untyped. The repo's other two MCP servers sit in the lenient tier
# and use the decorator; a caveman module is born strict and none may be added to
# that tier, so the same registration happens here as a plain call.
mcp.custom_route("/health", methods=["GET"])(health)
