"""#251 amendment D, package D-D: the ten MCP tools.

Every tool is driven for real -- a temporary sqlite ledger, a journalled
in-memory graph, the scripted chat transport ``caveman_fakes`` owns, and the
deterministic local embedder -- through the module's one supported seam,
:func:`~memotron.caveman.mcp.install_runtime_factory`. No tool is patched and
no module global is assigned: a test that reached past the factory would be
testing a wiring the server does not have.

.. rubric:: Why the tests set ``LITELLM_API_KEY``

``tests/conftest.py`` scrubs it from every test, and three tools check for it
before doing anything, because the subsystem is live only. A scripted transport
stands in for the gateway, so these tests set a dummy value to satisfy the same
precondition a deployment satisfies with a real key, and
:func:`test_a_gateway_tool_without_a_key_returns_the_setup_line` is the one test
that leaves it scrubbed.

.. rubric:: Why the graph is seeded directly rather than by ingesting and dreaming

Because what is under test is the tool surface, not the stages. Seeding through
``create_node`` / ``replace_facts`` / ``upsert_relation`` gives the read,
traversal and replay tools a graph with known facts, known evidence counts and a
known edge, with no model call to script -- so a failure here is a defect in this
module rather than in a prompt. ``memory_ingest`` and ``memory_dream`` are
exercised against the scripted transport separately, which is where a prompt
contract belongs.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from caveman_fakes import ScriptedChatTransport
from memotron.caveman import mcp as caveman_mcp
from memotron.caveman.errors import NodeNotFound
from memotron.caveman.graph import InMemoryGraph, JournaledGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    ClaimKind,
    ClaimMode,
    Fact,
    FactKind,
    LedgerEntry,
)
from memotron.caveman.motive import assistant_motive, engineering_motive
from memotron.caveman.pipeline import NO_RELATIONS, CavemanMemory
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.replay import DIGEST_FIELDS, content_digest
from memotron.embedding import LocalEmbeddingTransport
from memotron.gateway import GATEWAY_API_KEY_ENV

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

SCOPE = "jedai-platform"
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
EMBEDDER = LocalEmbeddingTransport()

EXPECTED_TOOLS = frozenset(
    {
        "memory_brief",
        "memory_contract",
        "memory_dream",
        "memory_erase",
        "memory_explain",
        "memory_ingest",
        "memory_neighbors",
        "memory_node",
        "memory_read",
        "memory_replay",
    }
)


class FixedClock:
    """One instant, so a rendered ``as of`` date and a digest are both stable."""

    def now(self) -> datetime:
        return NOW


@dataclass
class Harness:
    """The runtime under test, the transport (so a test can count calls), and the raw store.

    *inner* is the unjournalled ``InMemoryGraph`` the memory was composed over.
    A test never writes through it -- that is what :attr:`graph` is for -- but
    :func:`_rescript` needs it to compose a SECOND memory over the same state
    without journalling every mutation twice.
    """

    runtime: caveman_mcp.CavemanRuntime
    chat: ScriptedChatTransport
    inner: InMemoryGraph

    @property
    def graph(self) -> JournaledGraph:
        """The JOURNALLED store the memory writes through -- ``CavemanMemory.graph``.

        Seeding through this and not through a raw ``InMemoryGraph`` is what keeps
        a seeded scope provable: a write that bypassed the journal would show up
        as ``memory_replay`` reporting unequal digests, which is exactly what the
        proof is for.
        """
        graph = self.runtime.graph
        assert isinstance(graph, JournaledGraph)
        return graph

    @property
    def ledger(self) -> CavemanLedger:
        ledger = self.runtime.ledger
        assert isinstance(ledger, CavemanLedger)
        return ledger


def _harness(tmp_path: pathlib.Path, *, responses: Sequence[str | Exception] = ()) -> Harness:
    """Compose the same six seams :func:`build_runtime_from_env` composes, with fakes.

    The journal is real: ``JournaledGraph`` wrapping ``InMemoryGraph``, so the
    events ``memory_replay`` counts are events something actually recorded.
    """
    clock = FixedClock()
    ledger = CavemanLedger(str(tmp_path / "caveman.sqlite"))
    receipts = InMemoryReceipts()
    chat = ScriptedChatTransport(responses)
    inner = InMemoryGraph()
    memory = CavemanMemory(
        graph=inner,
        ledger=ledger,
        receipts=receipts,
        chat=chat,
        embedder=EMBEDDER,
        clock=clock,
    )
    return Harness(
        runtime=caveman_mcp.CavemanRuntime(
            memory=memory,
            graph=memory.graph,
            ledger=ledger,
            receipts=receipts,
            clock=clock,
            motive=engineering_motive(),
        ),
        chat=chat,
        inner=inner,
    )


def _rescript(harness: Harness, responses: Sequence[str | Exception]) -> Harness:
    """A second runtime over *harness*'s own stores, with a fresh script.

    The stores are shared and only the transport is new, so a two-phase script --
    ingest, then a dream whose answer cites the entry ids the ingest minted --
    runs against one ledger and one graph.
    """
    chat = ScriptedChatTransport(responses)
    # The INNER store, so the second memory wraps it in a journal of its own
    # rather than journalling every mutation twice. Both journals write to one
    # ledger, which is exactly the arrangement ``EVENT_ID_PREFIX`` documents.
    memory = CavemanMemory(
        graph=harness.inner,
        ledger=harness.ledger,
        receipts=harness.runtime.receipts,
        chat=chat,
        embedder=EMBEDDER,
        clock=harness.runtime.clock,
    )
    return Harness(
        runtime=caveman_mcp.CavemanRuntime(
            memory=memory,
            graph=memory.graph,
            ledger=harness.ledger,
            receipts=harness.runtime.receipts,
            clock=harness.runtime.clock,
            motive=harness.runtime.motive,
        ),
        chat=chat,
        inner=harness.inner,
    )


@pytest.fixture
def restore_factory() -> Iterator[None]:
    """Put the env factory back, whatever a test installed over it."""
    yield
    caveman_mcp.install_runtime_factory(caveman_mcp.build_runtime_from_env)


@pytest.fixture
def gateway_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy the live-only precondition the scripted transport cannot.

    See the module docstring. The value never leaves the process: the scripted
    transport holds no credential and opens no socket.
    """
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-caveman-test")


def _install(harness: Harness) -> Harness:
    caveman_mcp.install_runtime_factory(lambda: harness.runtime)
    return harness


def _entry(entry_id: str, *, claim: str, kind: ClaimKind, subjects: Sequence[str]) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        ts=NOW,
        episode_id="ep-251-01",
        scope=SCOPE,
        claim=claim,
        kind=kind,
        claim_mode=ClaimMode.DESCRIPTIVE,
        subjects=list(subjects),
        motive="engineering",
        confidence=0.9,
        turns=[1],
        receipt_id="r-seed",
    )


def _fact(kind: FactKind, text: str, *entry_ids: str, key: str | None = None) -> Fact:
    return Fact(kind=kind, text=text, key=key, entry_ids=list(entry_ids), first_seen=NOW, last_seen=NOW)


def _seed(harness: Harness) -> tuple[str, str]:
    """Two nodes, one typed edge, one reinforced fact. Returns the two node ids.

    The shape mirrors the design's own worked example: a service with rules and
    attributes, a consumer, and a ``BLOCKED`` edge carrying an ``until`` marker.
    The ``embedding`` attribute is supported by two entries, so a render has to
    show ``(x2)`` somewhere.
    """
    graph, ledger = harness.graph, harness.ledger
    entries = (
        _entry("e-001", claim="every real run goes through the gateway", kind=ClaimKind.RULE, subjects=["gateway"]),
        _entry("e-002", claim="the gateway is a LiteLLM proxy", kind=ClaimKind.IS, subjects=["gateway"]),
        _entry("e-003", claim="embeddings use text-embedding-3", kind=ClaimKind.ATTRIBUTE, subjects=["gateway"]),
        _entry(
            "e-004",
            claim="text-embedding-3 must be selected explicitly",
            kind=ClaimKind.ATTRIBUTE,
            subjects=["gateway"],
        ),
        _entry(
            "e-005",
            claim="agent-memory could not reach the gateway",
            kind=ClaimKind.RELATION,
            subjects=["agent-memory"],
        ),
        # Routed onto the gateway and NOT yet compressed into a fact, which is
        # the real state between an ingest and a dream -- and the one that makes
        # the ledger's own tally differ from a sum over the node's facts.
        _entry(
            "e-006",
            claim="the gateway rate limit is per virtual key",
            kind=ClaimKind.ATTRIBUTE,
            subjects=["gateway"],
        ),
    )
    for entry in entries:
        ledger.append(entry)

    gateway = graph.create_node(
        scope=SCOPE,
        name="gateway",
        type="service",
        facts=(),
        embedding=EMBEDDER.embed("gateway LiteLLM proxy"),
        now=NOW,
        aliases=("JedAI Gateway", "LiteLLM proxy"),
    )
    consumer = graph.create_node(
        scope=SCOPE,
        name="agent-memory",
        type="service",
        facts=(),
        embedding=EMBEDDER.embed("agent-memory MCP server"),
        now=NOW,
    )
    for entry in (*entries[:4], entries[5]):
        ledger.bind_nodes(entry.entry_id, [gateway.node_id])
    ledger.bind_nodes("e-005", [consumer.node_id, gateway.node_id])

    graph.replace_facts(
        gateway.node_id,
        facts=(
            _fact(FactKind.RULE, "real runs always via the gateway, never hermetic or local stub", "e-001"),
            _fact(FactKind.IS, "LiteLLM proxy fronting the JedAI models, not a model itself", "e-002"),
            _fact(
                FactKind.ATTRIBUTE, "text-embedding-3, must be selected explicitly", "e-003", "e-004", key="embedding"
            ),
            _fact(FactKind.SUPERSEDED, "chat default was claude-haiku-4-5", "e-002"),
        ),
        type="service",
        embedding=EMBEDDER.embed("gateway rules and attributes"),
        now=NOW,
    )
    graph.replace_facts(
        consumer.node_id,
        facts=(_fact(FactKind.IS, "the MCP server that reads Memotron agent memory", "e-005"),),
        type="service",
        embedding=EMBEDDER.embed("agent-memory MCP server"),
        now=NOW,
    )
    graph.upsert_relation(
        scope=SCOPE,
        source_id=consumer.node_id,
        target_id=gateway.node_id,
        type="BLOCKED",
        claim="Host header refused, #245 fixed the rewrite",
        entry_ids=("e-005",),
        until="#245",
        now=NOW,
    )
    return gateway.node_id, consumer.node_id


# -- the surface itself ---------------------------------------------------------


def test_the_registered_tools_are_exactly_the_documented_ones() -> None:
    """The names FastMCP registered equal the names ``AGENTS.md`` lists, exactly.

    Both directions. A tool the contract does not mention is a tool no agent will
    call; a tool the contract promises and the server does not have is a call
    that fails at the worst moment. Parsed out of the document rather than
    restated here, so the assertion cannot pass against a stale copy of the list.
    """
    import re

    from memotron.caveman import guidance

    registered = {tool.name for tool in asyncio.run(caveman_mcp.mcp.list_tools())}
    documented = set(re.findall(r"(?m)^\s*(memory_[a-z_]+)\(", guidance.agents_text()))
    assert registered == EXPECTED_TOOLS
    assert documented == registered


def test_every_tool_has_a_description_the_agent_can_route_on() -> None:
    """A registered tool with no description is one an agent has to guess about."""
    for tool in asyncio.run(caveman_mcp.mcp.list_tools()):
        assert tool.description, tool.name
        assert len(tool.description) > 40, tool.name


def test_the_server_object_carries_no_bind_or_allowlist_state() -> None:
    """FastMCP 4 owns no host, port or transport security on the object.

    Binding and the ``Host`` allowlist are decided together inside
    ``memotron.runtime.serve_mcp_http`` from one call; the module that builds
    the tools must not try to carry them, because the construct-then-mutate
    pattern that once bound ``0.0.0.0`` while allowing only localhost ``Host``
    headers is exactly what carrying them made possible.
    """
    source = pathlib.Path(caveman_mcp.__file__).read_text(encoding="utf-8")
    assert "transport_security" not in source
    assert "host=os.environ" not in source
    assert "port=int(os.environ" not in source
    assert not hasattr(caveman_mcp.mcp, "settings")


def test_no_tool_body_reads_a_store() -> None:
    """Every tool is one call into ``CavemanMemory`` and nothing else.

    ``memory_node``, ``memory_neighbors`` and ``memory_replay`` briefly had local
    implementations here, written against the graph and the ledger while the
    pipeline lacked the three methods. They are gone, and this asserts the module
    kept no second rendering of a node, an edge or a digest -- which is what stops
    this server and the demo disagreeing about what a node looks like.
    """
    source = pathlib.Path(caveman_mcp.__file__).read_text(encoding="utf-8")
    for gone in ("_render_node_block", "_render_neighbors", "_replay_proof", "content_digest", "PIPELINE_HANDOVER"):
        assert gone not in source, gone
    for method in ("memory.node(", "memory.neighbors(", "memory.replay("):
        assert method in source, method


# -- the read and traversal tools ----------------------------------------------


@pytest.mark.asyncio
async def test_memory_contract_is_the_shipped_agents_document() -> None:
    """Byte-equal to ``guidance.agents_text()``. One authorship, two readers."""
    from memotron.caveman import guidance

    assert await caveman_mcp.memory_contract() == guidance.agents_text()


@pytest.mark.asyncio
async def test_memory_brief_renders_the_scope_with_no_model_call(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """A brief needs neither a query nor the gateway, and carries the node ids."""
    harness = _install(_harness(tmp_path))
    gateway_id, consumer_id = _seed(harness)

    text = await caveman_mcp.memory_brief(SCOPE)

    assert f"[{gateway_id}]" in text
    assert f"[{consumer_id}]" in text
    assert "rule: real runs always via the gateway" in text
    assert "more: explain(" in text
    assert harness.chat.call_count == 0


@pytest.mark.asyncio
async def test_a_brief_of_an_empty_scope_says_so(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """An empty tool result reads as a broken call, so the empty scope is a sentence."""
    _install(_harness(tmp_path))
    assert await caveman_mcp.memory_brief("nothing-here") == caveman_mcp.EMPTY_READ


@pytest.mark.asyncio
async def test_memory_read_answers_a_query_and_names_the_ids(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """A read seeds on the name, renders the block and ends with the calls to make."""
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    text = await caveman_mcp.memory_read(SCOPE, "gateway")

    assert f"[{gateway_id}]" in text
    assert f"more: explain({gateway_id}" in text
    assert harness.chat.call_count == 0


@pytest.mark.asyncio
async def test_a_read_never_shows_a_superseded_fact(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """History belongs to ``memory_explain``; a bounded block spends nothing on it."""
    harness = _install(_harness(tmp_path))
    _seed(harness)

    assert "chat default was claude-haiku-4-5" not in await caveman_mcp.memory_read(SCOPE, "gateway")
    assert "chat default was claude-haiku-4-5" not in await caveman_mcp.memory_brief(SCOPE)


@pytest.mark.asyncio
async def test_memory_node_renders_one_block_with_its_edge_and_the_next_calls(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """The header, the facts in fixed order, the incoming edge, then the footer."""
    harness = _install(_harness(tmp_path))
    gateway_id, consumer_id = _seed(harness)

    text = await caveman_mcp.memory_node(gateway_id)
    lines = text.splitlines()

    assert lines[0].startswith(f"gateway (service) [{gateway_id}] as of 2026-09-11,")
    assert ", aka JedAI Gateway, LiteLLM proxy" in lines[0]
    assert "rule: real runs always via the gateway, never hermetic or local stub" in lines
    assert "is: LiteLLM proxy fronting the JedAI models, not a model itself" in lines
    assert f"from agent-memory [{consumer_id}] BLOCKED until #245: Host header refused, #245 fixed the rewrite" in lines
    assert lines[-1] == f"more: explain({gateway_id}), neighbors({gateway_id})"


@pytest.mark.asyncio
async def test_a_rendered_block_shows_the_evidence_count(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """Two entries behind one fact renders ``(x2)``. Reinforcement made visible."""
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    text = await caveman_mcp.memory_node(gateway_id)

    assert "embedding: text-embedding-3, must be selected explicitly (x2)" in text


@pytest.mark.asyncio
async def test_a_rendered_block_is_printable_ascii(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """The format adds nothing outside ASCII. The content here is ASCII, so the block is."""
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    for text in (
        await caveman_mcp.memory_node(gateway_id),
        await caveman_mcp.memory_neighbors(gateway_id),
        await caveman_mcp.memory_brief(SCOPE),
        await caveman_mcp.memory_explain(gateway_id),
        await caveman_mcp.memory_replay(SCOPE),
    ):
        assert text.isascii()
        for symbol in ("·", "→", "×"):
            assert symbol not in text


@pytest.mark.asyncio
async def test_memory_neighbors_lists_the_edges_and_footers_the_far_ends(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """One line per edge, the far node's id on it, and the footer names the far node."""
    harness = _install(_harness(tmp_path))
    gateway_id, consumer_id = _seed(harness)

    text = await caveman_mcp.memory_neighbors(consumer_id)
    lines = text.splitlines()

    assert lines[0] == f"agent-memory (service) [{consumer_id}], 1 relations"
    assert lines[1] == f"BLOCKED gateway [{gateway_id}] until #245: Host header refused, #245 fixed the rewrite"
    assert lines[-1] == f"more: explain({gateway_id}), neighbors({gateway_id})"


@pytest.mark.asyncio
async def test_memory_neighbors_renders_direction_as_a_word(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """The same edge from the other end reads ``from``, so direction is never a guess."""
    harness = _install(_harness(tmp_path))
    gateway_id, consumer_id = _seed(harness)

    text = await caveman_mcp.memory_neighbors(gateway_id)

    assert f"from agent-memory [{consumer_id}] BLOCKED until #245:" in text
    assert text.splitlines()[-1] == f"more: explain({consumer_id}), neighbors({consumer_id})"


@pytest.mark.asyncio
async def test_a_node_with_no_edges_says_so(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """A concept that stands alone is an answer; an empty result is a broken call."""
    harness = _install(_harness(tmp_path))
    graph = harness.graph
    harness.ledger.append(_entry("e-100", claim="a lone concept", kind=ClaimKind.IS, subjects=["lone"]))
    node = graph.create_node(
        scope=SCOPE,
        name="lone",
        type="concept",
        facts=(),
        embedding=EMBEDDER.embed("lone"),
        now=NOW,
    )

    text = await caveman_mcp.memory_neighbors(node.node_id)

    assert text.splitlines()[-1] == NO_RELATIONS
    assert "0 relations" in text


@pytest.mark.asyncio
async def test_memory_explain_returns_every_claim_with_its_dates(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """The deep read: unbounded, and it is where the superseded claim is visible."""
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    text = await caveman_mcp.memory_explain(gateway_id)

    assert f"[{gateway_id}]" in text
    assert "2026-09-11 | rule | every real run goes through the gateway" in text
    assert "aliases: JedAI Gateway, LiteLLM proxy" in text


@pytest.mark.asyncio
async def test_memory_replay_reports_the_digest_and_the_journal_length(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """The proof: the journal re-run onto a fresh graph, and both digests compared.

    The seeding goes through ``memory.graph`` -- the journal -- so the events are
    a complete account of the scope and the two digests agree. That equality is
    the assertion: a digest reported on its own would prove nothing, and a seed
    written behind the journal's back would report unequal.
    """
    harness = _install(_harness(tmp_path))
    _seed(harness)

    text = await caveman_mcp.memory_replay(SCOPE)

    events = harness.ledger.events(scope=SCOPE)
    assert len(events) > 0, "the journalled graph recorded nothing"
    proof = harness.runtime.memory.replay(scope=SCOPE)
    assert proof.equal is True
    assert text == proof.rendered
    assert f"journalled events replayed: {len(events)}" in text
    assert f"live content digest: {proof.live_digest}" in text
    assert f"replayed content digest: {proof.live_digest}" in text
    assert "digests equal: true" in text
    assert DIGEST_FIELDS in text


def test_the_content_digest_ignores_timestamps(tmp_path: pathlib.Path) -> None:
    """Two graphs asserting the same thing digest equal however they were stored.

    The projection drops ``first_seen``/``last_seen`` on both facts and
    relations, which is what makes the digest a statement about content rather
    than about a run. Asserted here as well as in ``test_caveman_replay`` because
    it is the property ``memory_replay``'s equality verdict rests on, and the
    tool only reports the value.
    """
    later = datetime(2026, 9, 12, 9, 30, tzinfo=UTC)
    graph = InMemoryGraph()
    node = graph.create_node(
        scope=SCOPE,
        name="solo",
        type="service",
        facts=(),
        embedding=EMBEDDER.embed("solo"),
        now=NOW,
    )
    graph.replace_facts(
        node.node_id,
        facts=(Fact(kind=FactKind.IS, text="one fact", entry_ids=["e-1"], first_seen=NOW, last_seen=NOW),),
        type="service",
        embedding=EMBEDDER.embed("solo"),
        now=NOW,
    )
    early = content_digest(graph, SCOPE)

    graph.replace_facts(
        node.node_id,
        facts=(Fact(kind=FactKind.IS, text="one fact", entry_ids=["e-1"], first_seen=later, last_seen=later),),
        type="service",
        embedding=EMBEDDER.embed("solo"),
        now=later,
    )
    assert content_digest(graph, SCOPE) == early


@pytest.mark.asyncio
async def test_memory_erase_deletes_the_episode_and_reports_what_went(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """Erasure is a ledger delete plus a re-dream, and it makes no model call."""
    harness = _install(_harness(tmp_path))
    gateway_id, consumer_id = _seed(harness)

    text = await caveman_mcp.memory_erase(SCOPE, "ep-251-01")

    assert "claims deleted: 6" in text
    assert "next: memory_dream" in text
    assert harness.chat.call_count == 0
    assert harness.ledger.for_episode("ep-251-01") == []
    remaining = {node.node_id for node in harness.graph.list_nodes(scope=SCOPE)}
    assert gateway_id not in remaining and consumer_id not in remaining


# -- the gateway-backed tools ---------------------------------------------------


@pytest.mark.asyncio
async def test_a_gateway_tool_without_a_key_returns_the_setup_line(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """``LITELLM_API_KEY`` scrubbed: one line naming the variable, from each of the three.

    ``conftest.py`` removes the variable from every test, so this is the ambient
    state and the other tests are the ones that set it. No offline branch: the
    tool does not answer from a cache or a local model, it says what to set.
    """
    _install(_harness(tmp_path))

    assert await caveman_mcp.memory_ingest(SCOPE, "ep-x", "[]") == caveman_mcp.GATEWAY_SETUP_ERROR
    assert await caveman_mcp.memory_read(SCOPE, "gateway") == caveman_mcp.GATEWAY_SETUP_ERROR
    assert await caveman_mcp.memory_dream(SCOPE) == caveman_mcp.GATEWAY_SETUP_ERROR
    assert GATEWAY_API_KEY_ENV in caveman_mcp.GATEWAY_SETUP_ERROR
    assert "\n" not in caveman_mcp.GATEWAY_SETUP_ERROR


@pytest.mark.asyncio
async def test_a_tool_that_needs_no_gateway_works_without_a_key(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """Brief, node, neighbors, explain, replay, erase and contract need no credential."""
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    for text in (
        await caveman_mcp.memory_contract(),
        await caveman_mcp.memory_brief(SCOPE),
        await caveman_mcp.memory_node(gateway_id),
        await caveman_mcp.memory_neighbors(gateway_id),
        await caveman_mcp.memory_explain(gateway_id),
        await caveman_mcp.memory_replay(SCOPE),
    ):
        assert text and text != caveman_mcp.GATEWAY_SETUP_ERROR


EXTRACT_RESPONSE = json.dumps(
    {
        "claims": [
            {
                "claim": "every real run goes through the JedAI Gateway",
                "kind": "rule",
                "claim_mode": "requirement",
                "subjects": ["the JedAI Gateway"],
                "identifiers": [],
                "confidence": 0.95,
                "turns": [1],
            },
            {
                "claim": "#245 fixed the Host header rewrite for agent-memory",
                "kind": "attribute",
                "claim_mode": "report",
                "subjects": ["agent-memory"],
                "identifiers": ["#245"],
                "confidence": 0.9,
                "turns": [2],
            },
        ]
    }
)

RECONCILE_RESPONSE = json.dumps(
    {
        "bindings": [
            {
                "local_name": "the JedAI Gateway",
                "decision": "new",
                "node_id": None,
                "new_node": {"name": "gateway", "type": "service", "gloss": "the LiteLLM proxy every run goes through"},
                "reason": "the scope offered nothing",
            },
            {
                "local_name": "agent-memory",
                "decision": "new",
                "node_id": None,
                "new_node": {"name": "agent-memory", "type": "service", "gloss": "the MCP server reading agent memory"},
                "reason": "the scope offered nothing",
            },
        ]
    }
)

TURNS_JSON = json.dumps(
    [
        {"speaker": "Priya", "text": "every real run goes through the JedAI Gateway, never a local stub"},
        {"speaker": "agent", "text": "confirmed from the deploy logs: #245 fixed the Host header rewrite"},
    ]
)


@pytest.mark.asyncio
async def test_memory_ingest_extracts_routes_and_reports_the_node_ids(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """Two model calls, whatever the size, and the summary carries the ids it created."""
    harness = _install(_harness(tmp_path, responses=(EXTRACT_RESPONSE, RECONCILE_RESPONSE)))

    text = await caveman_mcp.memory_ingest(SCOPE, "ep-251-99", TURNS_JSON)

    assert harness.chat.call_count == 2
    harness.chat.assert_exhausted()
    assert "episode: ep-251-99" in text
    assert "claims appended: 2" in text
    assert "nodes created: 2" in text
    assert "next: memory_dream" in text
    created = {node.node_id for node in harness.graph.list_nodes(scope=SCOPE)}
    assert len(created) == 2
    for node_id in created:
        assert f"[{node_id}]" in text
        assert f"[{node_id}] (new)" in text


@pytest.mark.asyncio
async def test_the_ingested_turns_are_numbered_from_the_array_order(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """The agent sends speaker and text; the server numbers them. One shape, no mis-numbering."""
    harness = _install(_harness(tmp_path, responses=(EXTRACT_RESPONSE, RECONCILE_RESPONSE)))

    await caveman_mcp.memory_ingest(SCOPE, "ep-251-99", TURNS_JSON)

    extract_prompt = harness.chat.prompts()[0]
    assert "1. Priya: every real run goes through the JedAI Gateway" in extract_prompt
    assert "2. agent: confirmed from the deploy logs" in extract_prompt


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "turns_json",
    [
        "[]",
        "{}",
        '["just a string"]',
        '[{"speaker": "a", "text": "b", "index": 1}]',
        '[{"speaker": "a"}]',
    ],
)
async def test_ingest_refuses_any_other_turn_shape(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None, turns_json: str
) -> None:
    """Fail fast with the shape stated. An agent can correct itself from one sentence."""
    _install(_harness(tmp_path))

    with pytest.raises(ValueError, match="turns_json"):
        await caveman_mcp.memory_ingest(SCOPE, "ep-bad", turns_json)


DREAM_NODE_FACTS: dict[str, str] = {
    "gateway": "real runs always via the gateway, never hermetic or local stub",
    "agent-memory": "reaches the gateway since #245 fixed the Host header rewrite",
}
"""One fact per node the ingest script creates, keyed by the name it created.

Keyed by NAME and not by node id because the ids are minted by the ingest this
script is the answer to.
"""


def _dream_node_responses(harness: Harness) -> tuple[str, ...]:
    """The 3a script for the dirty nodes, each fact citing that node's OWN entries.

    A function and not a constant since #251 amendment D package D-C: a fact
    names the ledger entries that evidence it, the ledger mints those ids at
    ingest, and ``dream`` rejects a fact citing an entry not bound to the node.
    So the dream script cannot be written before the ingest runs -- which is why
    these two tests script the ingest, run it, then re-script for the dream over
    the same stores (:func:`_rescript`).
    """
    responses: list[str] = []
    for node in harness.graph.dirty(scope=SCOPE):
        entry_ids = [entry.entry_id for entry in harness.ledger.for_node(node.node_id)]
        responses.append(
            json.dumps(
                {
                    "type": node.type,
                    "facts": [
                        {
                            "kind": FactKind.RULE.value,
                            "key": None,
                            "text": DREAM_NODE_FACTS[node.name],
                            "entry_ids": entry_ids,
                        }
                    ],
                    "relations": [],
                    "retired_relation_ids": [],
                    "reason": "the one rule the entries state",
                }
            )
        )
    return tuple(responses)


DREAM_GLOBAL_RESPONSE = json.dumps({"ops": []})


def _dreamt(harness: Harness) -> Harness:
    """Re-script *harness*'s stores for a dream pass, and install the result.

    ``ScriptedChatTransport`` is frozen and takes its whole script at
    construction -- the right shape for a fake whose point is that an unplanned
    call fails loudly -- so a two-phase script is two transports over one set of
    stores rather than one mutable transport.
    """
    return _install(_rescript(harness, (*_dream_node_responses(harness), DREAM_GLOBAL_RESPONSE)))


@pytest.mark.asyncio
async def test_memory_dream_compresses_and_reports_the_nodes_it_wrote(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """One call per dirty node plus one for the scope, and the summary names the ids."""
    ingested = _install(_harness(tmp_path, responses=(EXTRACT_RESPONSE, RECONCILE_RESPONSE)))
    await caveman_mcp.memory_ingest(SCOPE, "ep-251-99", TURNS_JSON)
    ingested.chat.assert_exhausted()
    harness = _dreamt(ingested)

    text = await caveman_mcp.memory_dream(SCOPE)

    assert harness.chat.call_count == 3
    harness.chat.assert_exhausted()
    assert "nodes recompressed: 2" in text
    assert "scope pass: 0 ops applied" in text
    assert "next: memory_replay" in text
    for node in harness.graph.list_nodes(scope=SCOPE):
        assert f"[{node.node_id}] {node.name}" in text
        assert node.facts, f"{node.node_id} was not given any facts"


@pytest.mark.asyncio
async def test_a_dream_journals_every_mutation_so_replay_can_count_them(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """Compression is provable because the journal grows with it, not because it says so."""
    ingested = _install(_harness(tmp_path, responses=(EXTRACT_RESPONSE, RECONCILE_RESPONSE)))
    await caveman_mcp.memory_ingest(SCOPE, "ep-251-99", TURNS_JSON)
    harness = _dreamt(ingested)
    before = len(harness.ledger.events(scope=SCOPE))

    await caveman_mcp.memory_dream(SCOPE)

    after = len(harness.ledger.events(scope=SCOPE))
    assert after > before
    text = await caveman_mcp.memory_replay(SCOPE)
    assert f"journalled events replayed: {after}" in text
    # And the journal ACCOUNTS for what the dream wrote, which is the point of
    # counting the events at all.
    assert "digests equal: true" in text


# -- construction --------------------------------------------------------------


def test_the_factory_builds_once_and_the_seam_drops_what_it_built(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """Lazy, cached, and :func:`install_runtime_factory` discards the cached runtime."""
    calls: list[int] = []
    first = _harness(tmp_path)

    def factory() -> caveman_mcp.CavemanRuntime:
        calls.append(1)
        return first.runtime

    caveman_mcp.install_runtime_factory(factory)
    assert caveman_mcp.runtime() is first.runtime
    assert caveman_mcp.runtime() is first.runtime
    assert len(calls) == 1

    second = _harness(tmp_path)
    caveman_mcp.install_runtime_factory(lambda: second.runtime)
    assert caveman_mcp.runtime() is second.runtime


def test_the_env_factory_journals_its_graph_and_reads_the_documented_variables(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, restore_factory: None
) -> None:
    """Built from the environment: the ledger path, the motive, and a journalled graph.

    No credential is needed to CONSTRUCT it -- the transports resolve the key at
    call time -- which is what lets a read-only tool work on a server with no key.
    """
    monkeypatch.setenv(caveman_mcp.LEDGER_PATH_ENV, str(tmp_path / "env.sqlite"))
    monkeypatch.setenv(caveman_mcp.MOTIVE_ENV, "assistant")

    live = caveman_mcp.build_runtime_from_env()

    assert isinstance(live.graph, JournaledGraph)
    assert isinstance(live.ledger, CavemanLedger)
    assert live.motive.name == assistant_motive().name
    assert live.motive.max_nodes == assistant_motive().max_nodes
    assert (tmp_path / "env.sqlite").exists()
    live.ledger.close()


def test_an_unknown_motive_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo must not silently reshape a written graph under default budgets."""
    monkeypatch.setenv(caveman_mcp.MOTIVE_ENV, "archaeologist")

    with pytest.raises(ValueError, match="archaeologist"):
        caveman_mcp.build_runtime_from_env()


def test_the_two_motive_presets_are_the_only_ones() -> None:
    """One canonical mapping, and the default is a member of it."""
    assert set(caveman_mcp.MOTIVES) == {"engineering", "assistant"}
    assert caveman_mcp.DEFAULT_MOTIVE in caveman_mcp.MOTIVES


@pytest.mark.asyncio
async def test_health_answers_outside_the_mcp_middleware() -> None:
    """``GET /health`` is a constant, so a liveness probe needs no MCP session.

    Deliberately outside the transport-security middleware, like the repo's other
    two servers -- which is also why a 200 here proves nothing about whether MCP
    itself is reachable; that is ``serve_mcp_http``'s job.
    """
    response = await caveman_mcp.health(None)  # type: ignore[arg-type]
    assert response.status_code == 200
    assert response.body == b"ok"


def test_build_runtime_from_env_creates_the_ledger_directory(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh checkout has no ``.memotron/``; the composition root makes it.

    ``sqlite3.connect`` answers a missing directory with "unable to open database
    file", and because the runtime is built lazily that surfaced on the first tool
    call of a freshly cloned server rather than at startup. The directory is the
    composition root's to create because the composition root chose the path.
    No credential is needed to build: the transports resolve the key at call time.
    """
    target = tmp_path / "fresh" / "nested" / "caveman.sqlite"
    monkeypatch.setenv(caveman_mcp.LEDGER_PATH_ENV, str(target))
    monkeypatch.delenv(GATEWAY_API_KEY_ENV, raising=False)
    assert not target.parent.exists()

    built = caveman_mcp.build_runtime_from_env()

    assert target.parent.is_dir()
    assert target.is_file()
    assert built.ledger.entry_counts(scope="anything") == {}


ENTRY_POINT = REPO_ROOT / "examples" / "caveman_mcp_server.py"


def test_the_entry_point_serves_through_the_one_supported_path() -> None:
    """The entry point binds and allowlists through ``serve_mcp_http`` and nothing else.

    Bind address and ``Host`` allowlist are arguments to one call there, so they
    cannot disagree; a hand-rolled ``mcp.run`` or a ``settings`` assignment is how
    they once did (421 on every ingress request while ``/health`` stayed 200).
    """
    source = ENTRY_POINT.read_text(encoding="utf-8")
    assert "serve_mcp_http(" in source
    assert "mcp_bind_from_env(" in source
    assert "mcp.run(" not in source
    assert "mcp.settings" not in source


def test_the_entry_point_loads_the_repo_root_env_file_before_serving() -> None:
    """``main()`` loads the checkout's own ``.env`` through ``runtime.load_env_file``.

    Anchored to the entry point's file, not to the cwd (T1-14), and before the
    bind: an MCP client that launches or dials this server without exporting the
    gateway key -- Codex, Claude, a ``nohup`` from elsewhere -- reaches the same
    gateway the demo does. ``load_env_file`` never overrides a value already in
    the environment, so an exported key still wins.
    """
    source = ENTRY_POINT.read_text(encoding="utf-8")
    assert "REPO_ROOT = Path(__file__).resolve().parents[1]" in source
    assert 'load_env_file(REPO_ROOT / ".env")' in source
    assert source.index('load_env_file(REPO_ROOT / ".env")') < source.index("mcp_bind_from_env(default_host")


def test_the_entry_point_documents_every_variable_it_serves() -> None:
    """Every environment variable the served module reads is named in the docstring.

    Derived from the module's own constants rather than restated, so a new
    variable cannot be added to the server and left out of the one place an
    operator reads.
    """
    source = ENTRY_POINT.read_text(encoding="utf-8")
    declared = (
        caveman_mcp.LEDGER_PATH_ENV,
        caveman_mcp.MOTIVE_ENV,
        caveman_mcp.CHAT_MODEL_ENV,
        caveman_mcp.EMBEDDING_MODEL_ENV,
        caveman_mcp.GATEWAY_BASE_URL_ENV,
        "LOG_LEVEL",
        GATEWAY_API_KEY_ENV,
        "MCP_HOST",
        "MCP_PORT",
        "MEMOTRON_MCP_ALLOWED_HOSTS",
    )
    for name in declared:
        assert name in source, f"the entry point does not document {name}"
    assert "/mcp" in source
    assert "/health" in source


@pytest.mark.asyncio
async def test_a_node_block_is_the_same_block_a_read_emits(
    tmp_path: pathlib.Path, restore_factory: None, gateway_key: None
) -> None:
    """``memory_node`` and ``memory_read`` must not disagree about the same node.

    The header is the half that can drift: it carries the entry count, and there
    are two plausible sources for it -- the ledger's own tally and a sum over the
    node's facts. They differ whenever one entry supports two facts, and a
    traversal that reported a different number from the read that handed over the
    id would make the agent doubt both.
    """
    harness = _install(_harness(tmp_path))
    gateway_id, _ = _seed(harness)

    node_header = (await caveman_mcp.memory_node(gateway_id)).splitlines()[0]
    read_lines = (await caveman_mcp.memory_read(SCOPE, "gateway")).splitlines()

    assert node_header in read_lines
    assert node_header == (await caveman_mcp.memory_explain(gateway_id)).splitlines()[0]
    assert f"as of 2026-09-11, {len(harness.ledger.for_node(gateway_id))} entries" in node_header


@pytest.mark.asyncio
async def test_an_unresolvable_node_id_raises_rather_than_answering_emptily(
    tmp_path: pathlib.Path, restore_factory: None
) -> None:
    """A stale id is a caller defect, and the same one ``CavemanMemory.explain`` raises.

    A read's footer names ids that existed when it ran, and compression can merge
    a node away. Answering with an empty block would let an agent conclude "this
    concept holds nothing" when the truth is "your state is older than the
    graph" -- so all three id-taking tools raise ``NodeNotFound``, and
    ``AGENTS.md`` tells the agent to read again rather than guess.
    """
    _install(_harness(tmp_path))

    for call in (caveman_mcp.memory_node, caveman_mcp.memory_neighbors, caveman_mcp.memory_explain):
        with pytest.raises(NodeNotFound, match="n-404"):
            await call("n-404")


@pytest.mark.asyncio
async def test_replay_of_an_empty_scope_still_answers(tmp_path: pathlib.Path, restore_factory: None) -> None:
    """A scope with no nodes has a digest and zero events, not an error."""
    _install(_harness(tmp_path))

    text = await caveman_mcp.memory_replay("nothing-here")

    assert "journalled events replayed: 0" in text
    assert "live content digest: " in text
    assert "digests equal: true" in text
