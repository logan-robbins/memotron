"""#251 unit 17: the read pipeline — embed, kNN, 1-hop, rank, render, receipt.

Every seam here is the REAL one. ``InMemoryGraph`` is the only ``GraphStore``
this issue ships, ``InMemoryReceipts`` the only sink, and embeddings come from
the hermetic ``LocalEmbeddingTransport`` (256-dim, L2-normalised, zero network
calls) rather than from hand-written vectors — an exact-text query returning its
own node first is only a meaningful assertion against a real embedder.

There is deliberately **no ``ScriptedChatTransport`` in this module**. A read
makes no model call, and the way to assert that is to give it nothing to call:
if ``read`` ever grew an LLM step, these tests would fail with a missing
argument rather than pass quietly.

The load-bearing assertions:

* a 1-hop neighbour contributes only its ``rule`` facts and the edges that
  touch a seed -- its own attributes, definitions and edges to nodes the read
  never seeded stay out, or a 100-line budget becomes a breadth-first crawl;
* a seed contributes its facts AND every edge that touches it, rendered from its
  end, so an edge is a belief the read emits rather than something only
  ``neighbors(<node_id>)`` can reach;
* one edge is offered by both of its endpoints and folded to one line by
  ``rank.dedupe_lines``, so the reader is told the belief once and the fold is
  reported rather than hidden;
* ``record_read`` counts nodes that were EMITTED, not nodes that were retrieved,
  because ``read_count`` feeds the pressure value function and a node nobody
  read must not earn survival value;
* ``contract_digest`` changes with the query, the motive and the budgets, and
  with nothing else — including not with the graph's contents, since it
  identifies the contract rather than the answer.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import ClaimKind, ClaimMode, Fact, FactKind, LedgerEntry, Node, ReceiptOp, Relation
from memotron.caveman.motive import CavemanMotive, ValueWeights, assistant_motive, engineering_motive
from memotron.caveman.pressure import node_value
from memotron.caveman.rank import CONSTRAINT_FLOOR, EXACT_FLOOR
from memotron.caveman.read import (
    BLOCK_SEPARATOR,
    EXACT_SIMILARITY,
    HOP_DISCOUNT,
    ReadResult,
    SeedHit,
    brief,
    brief_contract_digest,
    read,
    read_contract_digest,
)
from memotron.caveman.receipts import InMemoryReceipts, text_digest
from memotron.caveman.render import (
    FACT_LABEL,
    FACT_ORDER,
    FOOTER_PREFIX,
    LABEL_SEPARATOR,
    RELATION_ORDER,
    footer,
    render_fact,
    render_header,
    validate_fact_text,
)
from memotron.caveman.tokens import estimate_tokens
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"
MOTIVE = engineering_motive()
T = MOTIVE.max_fact_tokens

EMBED = LocalEmbeddingTransport()


def _read(
    graph: InMemoryGraph,
    query: str,
    *,
    motive: CavemanMotive = MOTIVE,
    receipts: InMemoryReceipts | None = None,
    scope: str = SCOPE,
    ledger: CavemanLedger | None = None,
) -> ReadResult:
    """A read against the real stores. The ledger is only read for entry counts.

    An empty ledger is the honest default for a graph built by ``_create``:
    nothing appended a claim, so every header states ``0 entries``. The tests
    that care about the count append entries themselves.
    """
    return read(
        query=query,
        scope=scope,
        motive=motive,
        graph=graph,
        ledger=ledger if ledger is not None else CavemanLedger(":memory:"),
        embedder=EMBED,
        receipts=receipts if receipts is not None else InMemoryReceipts(),
        now=NOW,
    )


def _entry(
    entry_id: str,
    *,
    node_id: str,
    scope: str = SCOPE,
    identifiers: tuple[str, ...] = (),
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        ts=NOW,
        episode_id="ep-251-01",
        scope=scope,
        claim=f"A claim about {node_id}, stated as a whole sentence.",
        kind=ClaimKind.ATTRIBUTE,
        claim_mode=ClaimMode.DESCRIPTIVE,
        subjects=("gateway",),
        identifiers=identifiers,
        node_ids=(node_id,),
        motive="engineering",
        confidence=0.9,
        turns=(1,),
        receipt_id="r-01",
    )


def _fact(text: str, kind: FactKind = FactKind.ATTRIBUTE, *, entries: tuple[str, ...] = ("e-01",)) -> Fact:
    """One fact, stamped at the fixed clock these tests read at."""
    return Fact(kind=kind, text=text, entry_ids=entries, first_seen=NOW, last_seen=NOW)


def _relate(
    graph: InMemoryGraph,
    source: Node,
    target: Node,
    *,
    type: str = "MENTIONED_WITH",
    claim: str = "one claim named both of these",
) -> Relation:
    """The one way these tests write an edge, so none of them can key a pair by hand."""
    return graph.upsert_relation(
        scope=source.scope,
        source_id=source.node_id,
        target_id=target.node_id,
        type=type,
        claim=claim,
        entry_ids=("e-01",),
        until=None,
        now=NOW,
    )


def _create(
    graph: InMemoryGraph,
    name: str,
    *,
    node_type: str = "service",
    facts: tuple[Fact, ...] = (),
    text: str | None = None,
    scope: str = SCOPE,
    aliases: tuple[str, ...] = (),
) -> Node:
    """A dreamt node: facts written, embedding over ``name + aliases + type + facts``."""
    embedded = text if text is not None else " ".join((name, *aliases, node_type, *(f.text for f in facts)))
    node = graph.create_node(
        scope=scope,
        name=name,
        type=node_type,
        facts=facts,
        embedding=EMBED.embed(embedded),
        now=NOW,
        aliases=aliases,
    )
    graph.clear_dirty([node.node_id], now=NOW)
    return graph.get_node(node.node_id)


def _blocks(result: ReadResult) -> list[str]:
    """The node blocks, without the trailing ``deeper:`` footer.

    Every structural assertion about a block — its header, its lines, its
    parseability — is about a NODE block, and the footer is not one. Splitting
    it off here rather than in nine tests is what stops the footer from being
    asserted as a malformed node.
    """
    blocks = result.rendered.split(BLOCK_SEPARATOR)
    assert blocks[-1].startswith(FOOTER_PREFIX), f"a rendered read must end with a footer, got {blocks[-1]!r}"
    return blocks[:-1]


def _footer(result: ReadResult) -> str:
    """The one footer line a rendered read ends with."""
    return result.rendered.split(BLOCK_SEPARATOR)[-1]


def _kind_ordered(lines: Sequence[str]) -> list[str]:
    """*lines* sorted by the kind their label names, stably.

    Reads the rendered LABEL rather than a stored belief, because what is
    asserted is a property of the rendering. Three cases, in the order they are
    told apart:

    * a label carrying ``[`` is a relation head -- ``BLOCKED name [n-004]`` or
      ``from name [n-001] BLOCKED`` -- and takes :data:`RELATION_ORDER`;
    * a label that is a kind word takes that kind's order;
    * anything else is an attribute rendering under its own ``key``, which keeps
      the attributes' position.
    """
    ranks = {FACT_LABEL[kind]: order for kind, order in FACT_ORDER.items()}

    def order(text: str) -> int:
        label = text.partition(LABEL_SEPARATOR)[0]
        if "[" in label:
            return RELATION_ORDER
        return ranks.get(label, FACT_ORDER[FactKind.ATTRIBUTE])

    return sorted(lines, key=order)


# ============================================================ the fixture graph

GATEWAY_FACTS = (
    _fact("never hermetic, always through the JedAI gateway", FactKind.RULE),
    _fact("a LiteLLM proxy fronting the JedAI models", FactKind.IS),
    _fact("chat default is claude-haiku-4-5"),
    _fact("session affinity is lost about 1 call in 20", FactKind.UNSURE),
)
AGENT_MEMORY_FACTS = (
    _fact("agent-memory must reach the gateway to serve", FactKind.RULE),
    _fact("C4 returns 31 tools after #248"),
    _fact("an MCP server exposing memotron memory", FactKind.IS),
)
CHART_FACTS = (
    _fact("chart version 0.4.1 in the C4 cluster"),
    _fact("a Helm chart deploying memotron", FactKind.IS),
)


def _fixture() -> tuple[InMemoryGraph, dict[str, Node]]:
    """gateway — agent-memory — chart, linked, plus one unlinked node.

    ``probe`` is linked to nothing, so it can only ever arrive as a seed. That
    is what makes "the neighbour rules applied to a non-neighbour" a testable
    absence rather than an assumption.
    """
    graph = InMemoryGraph()
    gateway = _create(graph, "gateway", node_type="service", facts=GATEWAY_FACTS)
    agent_memory = _create(graph, "agent-memory", node_type="artifact", facts=AGENT_MEMORY_FACTS)
    chart = _create(graph, "chart", node_type="artifact", facts=CHART_FACTS)
    probe = _create(graph, "probe", node_type="artifact", facts=(_fact("probe #246 names the affinity loss"),))

    _relate(graph, agent_memory, gateway, type="BLOCKED", claim="the Host header was refused pre-#245")
    _relate(graph, chart, agent_memory, type="DEPLOYS", claim="the chart deploys it, #240")

    nodes = {
        "gateway": graph.get_node(gateway.node_id),
        "agent-memory": graph.get_node(agent_memory.node_id),
        "chart": graph.get_node(chart.node_id),
        "probe": graph.get_node(probe.node_id),
    }
    return graph, nodes


def test_the_fixture_facts_are_all_ones_the_dreamer_could_have_written() -> None:
    """A guard on the fixture: a fact the dreamer could not have written proves nothing."""
    for facts in (GATEWAY_FACTS, AGENT_MEMORY_FACTS, CHART_FACTS):
        assert len(facts) <= MOTIVE.max_facts_per_node
        for fact in facts:
            validate_fact_text(fact.text, max_fact_tokens=T)


# ======================================================================= seeds


def test_the_seed_is_the_node_the_query_matches() -> None:
    graph, nodes = _fixture()
    result = _read(graph, "what do I know about the gateway")
    assert result.nodes[0].node_id == nodes["gateway"].node_id


def test_seeds_come_from_knn_and_are_capped_at_read_k() -> None:
    """``read_k=1`` means one seed, so only its lines and its neighbours' can appear."""
    graph, nodes = _fixture()
    one_seed = MOTIVE.model_copy(update={"read_k": 1})
    result = _read(graph, "gateway LiteLLM proxy", motive=one_seed)

    seeded = graph.knn(scope=SCOPE, vector=EMBED.embed("gateway LiteLLM proxy"), k=1)
    assert [node.node_id for node in result.nodes][:1] == [seeded[0][0].node_id]
    assert nodes["probe"].node_id not in {node.node_id for node in result.nodes}


def test_a_seed_contributes_every_kind_of_line_it_holds() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy claude-haiku", motive=MOTIVE.model_copy(update={"read_k": 1}))
    emitted = set(result.rendered.splitlines())
    assert {render_fact(fact) for fact in GATEWAY_FACTS} <= emitted


def test_a_read_of_an_empty_scope_is_an_empty_answer_not_an_error() -> None:
    """ "I know nothing about X" is a real answer to the question."""
    receipts = InMemoryReceipts()
    result = _read(InMemoryGraph(), "anything at all", receipts=receipts)
    assert result.rendered == ""
    assert result.nodes == ()
    assert result.line_count == 0
    assert result.token_count == 0
    assert result.saturated is False
    assert [receipt.op for receipt in receipts.all()] == [ReceiptOp.READ_EMITTED]


def test_a_read_is_scoped() -> None:
    graph, _ = _fixture()
    _create(graph, "other-gateway", facts=(_fact("other scope constraint", FactKind.RULE),), scope="repo:jedai/other")
    result = _read(graph, "gateway")
    assert all(node.scope == SCOPE for node in result.nodes)


@pytest.mark.parametrize("query", ["", "   ", "\n"])
def test_a_blank_query_is_refused(query: str) -> None:
    """Fail fast: embedding whitespace returns a zero vector and ranks the scope at random."""
    graph, _ = _fixture()
    with pytest.raises(ValueError, match="read query cannot be blank"):
        _read(graph, query)


# ============================================================== the 1-hop rule


def test_a_seed_emits_the_edges_that_touch_it_rendered_from_its_own_end() -> None:
    """The edge is a belief the read emits, not something only ``neighbors`` reaches.

    ``gateway`` is the TARGET of the ``BLOCKED`` edge, so its block renders the
    incoming form -- ``from <source> [id] TYPE: claim`` -- and the node id of the
    other end is in the line, which is what lets the reader traverse.
    """
    graph, nodes = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    memory_id = nodes["agent-memory"].node_id
    assert f"from agent-memory [{memory_id}] BLOCKED: the Host header was refused pre-#245" in (
        result.rendered.splitlines()
    )


def test_a_neighbour_contributes_the_edges_that_touch_a_seed() -> None:
    """Plus its rules, and nothing else. One edge, folded to whichever end ranked higher."""
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    emitted = result.rendered.splitlines()
    assert render_fact(AGENT_MEMORY_FACTS[0]) in emitted
    assert sum(1 for line in emitted if "BLOCKED" in line) == 1
    assert result.duplicates_dropped == 1


def test_a_neighbour_contributes_all_of_its_constraint_lines() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert render_fact(AGENT_MEMORY_FACTS[0]) in result.rendered.splitlines()


def test_a_neighbour_contributes_nothing_else() -> None:
    """Its identity and attribute lines are facts about IT, not about the query."""
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    emitted = set(result.rendered.splitlines())
    assert ": C4 returns 31 tools after #248" not in emitted
    assert "= MCP server exposing memotron memory" not in emitted


def test_a_neighbours_edge_to_a_non_seed_is_not_offered() -> None:
    """``chart`` is 2 hops from ``gateway``, so the ``DEPLOYS`` edge reaches no seed.

    The bound that keeps 1-hop from becoming a crawl: ``agent-memory`` is a
    neighbour and carries two edges, and only the one touching the seed is a
    candidate.
    """
    graph, nodes = _fixture()
    assert {relation.type for relation in graph.relations_of(nodes["agent-memory"].node_id)} == {
        "BLOCKED",
        "DEPLOYS",
    }
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert not any("DEPLOYS" in line for line in result.rendered.splitlines())


def test_a_neighbour_line_ranks_below_the_same_kind_on_its_seed() -> None:
    """The hop discount, observable: seed first, then what it touches."""
    graph, _ = _fixture()
    hub = _create(
        graph,
        "hub",
        node_type="untyped",
        facts=(_fact("hub definition line", FactKind.IS),),
        text="unrelated hub text",
    )
    spoke = _create(
        graph,
        "spoke",
        node_type="untyped",
        facts=(_fact("the spoke defers to the hub", FactKind.RULE),),
        text="unrelated spoke text",
    )
    _relate(graph, spoke, hub)

    result = _read(graph, "unrelated hub text", motive=MOTIVE.model_copy(update={"read_k": 1}))
    lines = result.rendered.splitlines()
    assert lines.index("is: hub definition line") < lines.index("rule: the spoke defers to the hub")
    assert HOP_DISCOUNT < 1.0


def test_a_neighbour_returned_without_a_link_is_a_store_defect_and_raises() -> None:
    """``neighbours()`` and ``relations()`` must agree. If they do not, the read says so.

    Not a fallback to similarity 0.0: a store whose expansion disagrees with its
    own edges would silently emit lines whose rank nobody can account for.
    """

    class DisagreeingGraph(InMemoryGraph):
        def neighbours(self, node_ids: Sequence[str]) -> list[Node]:
            seeds = set(node_ids)
            return [node for node in self.list_nodes(scope=SCOPE) if node.node_id not in seeds]

    graph = DisagreeingGraph()
    _create(graph, "gateway", facts=GATEWAY_FACTS)
    _create(graph, "probe", facts=(_fact("probe #246 names the affinity loss"),))

    with pytest.raises(ValueError, match="neighbours\\(\\) and relations\\(\\) disagree"):
        _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))


# ==================================================================== rendering


def test_every_block_begins_with_its_render_header() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    blocks = _blocks(result)
    assert len(blocks) == len(result.nodes)
    for block, node in zip(blocks, result.nodes, strict=True):
        assert block.splitlines()[0] == render_header(node, entries=0)


def test_a_header_states_the_nodes_ledger_entry_count() -> None:
    """How well-evidenced the block is -- the second thing a reader needs after staleness."""
    graph, nodes = _fixture()
    ledger = CavemanLedger(":memory:")
    gateway = nodes["gateway"]
    ledger.append(_entry("e-01", node_id=gateway.node_id))
    ledger.append(_entry("e-02", node_id=gateway.node_id))

    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}), ledger=ledger)
    assert result.rendered.splitlines()[0].endswith("2 entries")
    ledger.close()


def test_a_header_states_zero_entries_for_a_node_the_ledger_does_not_know() -> None:
    """Not a lookup failure: erasure deletes an episode's entries and leaves the nodes."""
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert result.rendered.splitlines()[0].endswith("0 entries")


def test_a_header_dates_itself_from_the_nodes_last_touch() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert f"as of {NOW.date().isoformat()}," in result.rendered.splitlines()[0]


def test_a_header_names_the_node_id_the_footers_calls_take() -> None:
    """``[n-001]`` is what a reader passes back to ``explain`` and ``neighbors``.

    The rendered id is the NODE id, not ``ledger_key``: amendment D carries ids
    back so the agent can traverse, and the id is what every call in the footer
    accepts. That the two are equal today is a coincidence of how the store
    mints keys, not something a reader should have to know.
    """
    graph, nodes = _fixture()
    gateway = nodes["gateway"]
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert result.rendered.startswith(f"gateway (service) [{gateway.node_id}] as of ")
    assert f"explain({gateway.node_id}" in _footer(result)


def test_block_order_follows_the_rank_of_each_nodes_best_line() -> None:
    """No second sort, so nothing can disagree with ``rank_lines``.

    A query with no exact hit, so the ordering on show is the motive's own: the
    ``!``-holding node leads because
    :data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` puts its line first.
    ``hermetic proxy affinity`` names nothing in the graph and no identifier the
    (empty) ledger holds, which is what makes it the case to assert this on.
    """
    graph, _ = _fixture()
    result = _read(graph, "hermetic proxy affinity")
    assert [hit.kind for hit in result.seeds] == ["knn"] * len(result.seeds)
    first_lines = [block.splitlines()[1] for block in _blocks(result)]
    assert first_lines[0].startswith("rule: ")


def test_an_exact_hits_block_leads_even_when_another_node_holds_a_constraint() -> None:
    """#251 amendment C: the node the query NAMED reads first.

    ``chart`` is an exact alias hit and holds no ``!`` at all; ``gateway`` and
    ``agent-memory`` both do. Before the exact floor the two constraint-holders
    led and the answer to the question asked came third.
    """
    graph, nodes = _fixture()
    result = _read(graph, "chart Helm deploying memotron")
    assert result.nodes[0].node_id == nodes["chart"].node_id
    assert _blocks(result)[0].splitlines()[1].startswith("is: ")
    assert [node.name for node in result.nodes[1:]].count("gateway") == 1


def test_a_constraint_still_leads_the_exact_nodes_own_block() -> None:
    """The floor lifts a node, it does not reorder the node. ``!`` first, always."""
    graph, nodes = _fixture()
    result = _read(graph, "agent-memory")
    assert result.nodes[0].node_id == nodes["agent-memory"].node_id
    assert _blocks(result)[0].splitlines()[1].startswith("rule: ")


def test_every_emitted_line_is_a_label_then_a_usable_fact() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart probe")
    for block in _blocks(result):
        header, *lines = block.splitlines()
        assert header.endswith("entries")
        for text in lines:
            label, separator, body = text.partition(": ")
            assert separator == ": "
            assert label and body
            validate_fact_text(body, max_fact_tokens=T)


def test_line_count_matches_the_lines_actually_rendered() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart probe")
    rendered_lines = sum(len(block.splitlines()) - 1 for block in _blocks(result))
    assert result.line_count == rendered_lines


def test_token_count_covers_the_whole_block_headers_included() -> None:
    """The header is a reported cost, not a budgeted one -- but it IS reported."""
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert result.token_count == estimate_tokens(result.rendered)

    lines_only = "\n".join(text for block in _blocks(result) for text in block.splitlines()[1:])
    assert estimate_tokens(lines_only) < result.token_count


# =================================================================== record_read


def test_record_read_is_called_once_per_emitted_node() -> None:
    graph, nodes = _fixture()
    result = _read(graph, "gateway agent-memory chart probe")
    emitted = {node.node_id for node in result.nodes}
    assert emitted == {node.node_id for node in nodes.values()}
    for node in nodes.values():
        assert graph.get_node(node.node_id).read_count == 1


def test_a_retrieved_node_whose_lines_all_lost_the_cut_is_not_counted_as_read() -> None:
    """``read_count`` feeds the pressure value function; an unread node earns nothing."""
    graph, _ = _fixture()
    one_line = MOTIVE.model_copy(update={"read_line_budget": 1})
    result = _read(graph, "gateway agent-memory chart probe", motive=one_line)
    assert len(result.nodes) == 1
    counted = [node for node in graph.list_nodes(scope=SCOPE) if node.read_count > 0]
    assert [node.node_id for node in counted] == [result.nodes[0].node_id]


def test_two_reads_count_twice() -> None:
    graph, _ = _fixture()
    first = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert graph.get_node(first.nodes[0].node_id).read_count == 2


# ============================================================== contract digest


def test_the_digest_is_stable_across_two_identical_reads() -> None:
    graph, _ = _fixture()
    first = _read(graph, "what do I know about the gateway")
    second = _read(graph, "what do I know about the gateway")
    assert first.contract_digest == second.contract_digest
    assert first.rendered == second.rendered


def test_the_digest_changes_with_the_query() -> None:
    graph, _ = _fixture()
    assert _read(graph, "the gateway").contract_digest != _read(graph, "the chart").contract_digest


def test_the_digest_changes_with_the_motive() -> None:
    graph, _ = _fixture()
    engineering = _read(graph, "the gateway").contract_digest
    assistant = _read(graph, "the gateway", motive=assistant_motive()).contract_digest
    assert engineering != assistant


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("read_k", 4),
        ("read_line_budget", 4),
        # Derived from the motive rather than written as a literal: the preset's
        # own ``T`` moved once (WP4's live calibration, 15 -> 20) and a literal
        # that happened to equal it made this assert that a digest changes when
        # nothing changed.
        ("max_fact_tokens", MOTIVE.max_fact_tokens + 5),
    ],
)
def test_the_digest_changes_with_each_budget(field: str, value: int) -> None:
    """``max_fact_tokens`` is raised rather than lowered -- see the next test for why."""
    graph, _ = _fixture()
    assert value != getattr(MOTIVE, field), f"{field} was not actually changed from the preset"
    baseline = _read(graph, "the gateway").contract_digest
    changed = _read(graph, "the gateway", motive=MOTIVE.model_copy(update={field: value})).contract_digest
    assert changed != baseline


def test_a_read_never_fails_on_the_facts_a_wider_motive_wrote() -> None:
    """The fail-fast MOVED with #251 amendment D, and this pins where it went.

    A read used to re-parse every stored line under the reading motive's ``T``
    and raise when the two policies disagreed about what a legal line was. It no
    longer parses anything: a fact is a record with a kind, so there is nothing
    to re-derive, and the paragraph guard is applied once -- by the dreamer, at
    the moment it writes (``render.validate_fact_text``).

    So reading under a narrower guard than the facts were written at is now a
    read, not a rejection. That is strictly better for the reader: a stricter
    persona can read a scope another persona dreamt without losing a rule to an
    exception nobody could repair.
    """
    graph, _ = _fixture()
    strict = _read(graph, "the gateway", motive=MOTIVE.model_copy(update={"max_fact_tokens": 4}))
    assert strict.line_count > 0
    assert render_fact(GATEWAY_FACTS[0]) in strict.rendered.splitlines()


def test_the_digest_does_not_change_with_the_graphs_contents() -> None:
    """It identifies the CONTRACT, not the answer. ``outputs_digest`` identifies the answer."""
    graph, nodes = _fixture()
    before = _read(graph, "the gateway")
    newcomer = _create(graph, "newcomer", facts=(_fact("a brand new constraint arrives", FactKind.RULE),))
    _relate(graph, newcomer, nodes["gateway"])
    after = _read(graph, "the gateway")
    assert after.contract_digest == before.contract_digest
    assert after.rendered != before.rendered
    assert "a brand new constraint arrives" in after.rendered


def test_the_digest_changes_with_the_vector_space() -> None:
    """Two embedding identifiers are two different kNN results from identical inputs."""

    class OtherSpace:
        identifier = "other-space@v1"

        def embed(self, text: str) -> list[float]:
            return EMBED.embed(text)

    graph, _ = _fixture()
    local = _read(graph, "the gateway").contract_digest
    other = read(
        query="the gateway",
        scope=SCOPE,
        motive=MOTIVE,
        graph=graph,
        ledger=CavemanLedger(":memory:"),
        embedder=OtherSpace(),
        receipts=InMemoryReceipts(),
        now=NOW,
    ).contract_digest
    assert other != local


def test_the_public_digest_helper_matches_what_a_read_carries() -> None:
    graph, _ = _fixture()
    result = _read(graph, "the gateway")
    assert result.contract_digest == read_contract_digest(
        query="the gateway",
        motive=MOTIVE,
        embedding_identifier=EMBED.identifier,
    )


# ===================================================================== receipts


def test_a_read_emits_exactly_one_read_emitted_receipt_carrying_the_digest() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    result = _read(graph, "the gateway", receipts=receipts)

    emitted = [receipt for receipt in receipts.all(scope=SCOPE) if receipt.op is ReceiptOp.READ_EMITTED]
    assert len(emitted) == 1
    assert emitted[0].inputs_digest == result.contract_digest
    assert emitted[0].subject == result.contract_digest
    assert emitted[0].outputs_digest == text_digest(result.rendered)
    assert emitted[0].ts == NOW


def test_a_saturated_read_also_emits_a_budget_saturated_receipt() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    result = _read(
        graph, "gateway agent-memory chart", motive=MOTIVE.model_copy(update={"read_line_budget": 2}), receipts=receipts
    )

    assert result.saturated is True
    assert [receipt.op for receipt in receipts.all()] == [
        ReceiptOp.READ_EMITTED,
        ReceiptOp.READ_BUDGET_SATURATED,
    ]
    assert "dropped" in receipts.all()[1].detail


def test_an_unsaturated_read_emits_no_saturation_receipt() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    result = _read(graph, "the gateway", receipts=receipts)
    assert result.saturated is False
    assert [receipt.op for receipt in receipts.all()] == [ReceiptOp.READ_EMITTED]


def test_every_receipt_detail_fits_its_bound() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    _read(
        graph, "gateway agent-memory chart", motive=MOTIVE.model_copy(update={"read_line_budget": 1}), receipts=receipts
    )
    for receipt in receipts.all():
        assert 0 < len(receipt.detail) <= 600


# ====================================================================== budgets


def test_the_line_budget_caps_what_is_emitted() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart probe", motive=MOTIVE.model_copy(update={"read_line_budget": 3}))
    assert result.line_count == 3
    assert result.saturated is True


def test_the_token_budget_comes_from_the_motive_not_from_the_line_ceiling() -> None:
    """``read_token_budget`` is its own field; ``T`` no longer prices a line.

    Set it to one token and the cut has to bite immediately, with a line budget
    wide enough that only the token half can be what stopped it.
    """
    graph, _ = _fixture()
    starved = MOTIVE.model_copy(update={"read_token_budget": 1, "read_line_budget": 100})
    result = _read(graph, "gateway agent-memory chart probe", motive=starved)
    assert result.saturated is True
    assert result.line_count == 0
    assert result.rendered == ""


def test_a_wide_token_budget_lets_the_line_budget_be_what_binds() -> None:
    """The other side of the same seam: whichever budget binds first is the cut."""
    graph, _ = _fixture()
    generous = MOTIVE.model_copy(update={"read_token_budget": 100_000, "read_line_budget": 2})
    result = _read(graph, "gateway agent-memory chart probe", motive=generous)
    assert result.line_count == 2
    assert result.saturated is True


def test_the_digest_changes_with_the_read_token_budget() -> None:
    """It is in the contract digest, so two token budgets are two contracts."""
    graph, _ = _fixture()
    baseline = _read(graph, "the gateway").contract_digest
    changed = _read(graph, "the gateway", motive=MOTIVE.model_copy(update={"read_token_budget": 900})).contract_digest
    assert changed != baseline


def test_a_budget_smaller_than_the_constraint_set_keeps_constraints_first() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory", motive=MOTIVE.model_copy(update={"read_line_budget": 2}))
    for block in _blocks(result):
        for text in block.splitlines()[1:]:
            assert text.startswith("rule: ")
    assert CONSTRAINT_FLOOR > 0.0


# ================================================================== integration


def _two_hundred_node_scope() -> InMemoryGraph:
    """200 dreamt nodes, each at seven facts, chained so every node has neighbours."""
    graph = InMemoryGraph()
    topics = ("gateway", "chart", "cluster", "probe", "ledger", "dreamer", "reader", "motive")
    created: list[Node] = []
    for index in range(200):
        topic = topics[index % len(topics)]
        facts = (
            _fact(f"{topic} {index} must never run hermetically", FactKind.RULE),
            _fact(f"{topic} {index} is a component of memotron", FactKind.IS),
            _fact(f"{topic} {index} version 0.4.{index % 9}"),
            _fact(f"{topic} {index} returns {index} tools"),
            _fact(f"{topic} {index} loses affinity about 1 in 20", FactKind.UNSURE),
            _fact(f"{topic} {index} returned 24 tools once", FactKind.SUPERSEDED),
            _fact(f"{topic} {index} embeds at 3072 dimensions"),
        )
        created.append(_create(graph, f"{topic} {index}", node_type=topic[:8], facts=facts))
    # One write per node is enough now: ``upsert_relation`` ADDS an edge rather
    # than replacing a node's edges wholesale, and ``relations_of`` reads both
    # directions -- so a ring needs one relation per node, not two.
    for index, node in enumerate(created):
        _relate(graph, node, created[(index + 1) % 200])
    return graph


def test_a_two_hundred_node_scope_reads_inside_its_token_budget() -> None:
    """The plan's integration case: 200 nodes, ``read_line_budget=100``, ~1.3k tokens."""
    graph = _two_hundred_node_scope()
    assert graph.count(scope=SCOPE) == 200

    receipts = InMemoryReceipts()
    result = _read(graph, "what do I know about the gateway", receipts=receipts)

    assert result.line_count <= MOTIVE.read_line_budget
    assert result.token_count <= 1600
    assert len(receipts.all()) >= 1

    # Not a vacuous pass: the read must actually have expanded 1-hop. Measured
    # 88 lines over 24 nodes at 997 tokens -- 8 seeds plus 16 neighbours.
    seeded = graph.knn(scope=SCOPE, vector=EMBED.embed("what do I know about the gateway"), k=MOTIVE.read_k)
    seed_ids = {node.node_id for node, _ in seeded}
    assert len(seed_ids) == MOTIVE.read_k
    assert len({node.node_id for node in result.nodes} - seed_ids) >= MOTIVE.read_k

    for block in _blocks(result):
        header, *lines = block.splitlines()
        assert header.endswith("entries")
        for text in lines:
            assert ": " in text


def test_two_identical_reads_of_a_large_scope_are_byte_identical() -> None:
    graph = _two_hundred_node_scope()
    first = _read(graph, "how many tools does the cluster return")
    second = _read(graph, "how many tools does the cluster return")
    assert first.contract_digest == second.contract_digest
    assert first.rendered == second.rendered
    assert first.line_count == second.line_count
    assert [node.node_id for node in first.nodes] == [node.node_id for node in second.nodes]


def test_every_fact_of_a_large_read_fits_the_paragraph_guard() -> None:
    graph = _two_hundred_node_scope()
    result = _read(graph, "what do I know about the gateway")
    for block in _blocks(result):
        for text in block.splitlines()[1:]:
            validate_fact_text(text.partition(": ")[2], max_fact_tokens=T)


# ============================================================= hybrid seeding


class CountingEmbedder:
    """``LocalEmbeddingTransport``, plus a call count.

    The only way to assert that a read answered entirely out of the two exact
    indexes is to count the embedding calls it did not make.
    """

    def __init__(self) -> None:
        self.calls = 0

    @property
    def identifier(self) -> str:
        return EMBED.identifier

    def embed(self, text: str) -> list[float]:
        self.calls += 1
        return EMBED.embed(text)


def _ledger_with(*entries: LedgerEntry) -> CavemanLedger:
    ledger = CavemanLedger(":memory:")
    for entry in entries:
        ledger.append(entry)
    return ledger


def test_an_identifier_query_seeds_the_node_the_ledger_binds_it_to() -> None:
    """The headline symptom: ``#245`` and ``#246`` embed nearly identically.

    ``probe`` holds the ``#246`` claim and ``chart`` does not, and neither node's
    LINES mention the number — the binding exists only in the ledger, so a hit
    here can only have come from the identifier index.
    """
    graph, nodes = _fixture()
    ledger = _ledger_with(
        _entry("e-01", node_id=nodes["probe"].node_id, identifiers=("#246",)),
        _entry("e-02", node_id=nodes["chart"].node_id, identifiers=("#245",)),
    )
    try:
        result = _read(graph, "#246", motive=MOTIVE.model_copy(update={"read_k": 1}), ledger=ledger)
        assert result.seeds[0] == SeedHit(
            node_id=nodes["probe"].node_id,
            kind="identifier",
            token="#246",
            similarity=EXACT_SIMILARITY,
        )
        assert nodes["chart"].node_id not in {hit.node_id for hit in result.seeds}
    finally:
        ledger.close()


def test_an_identifier_hit_is_case_insensitive_over_the_ledgers_verbatim_key() -> None:
    """The ledger keys verbatim; the MATCHING policy is the reader's."""
    graph, nodes = _fixture()
    ledger = _ledger_with(_entry("e-01", node_id=nodes["chart"].node_id, identifiers=("C4",)))
    try:
        result = _read(graph, "how many tools does c4 return", ledger=ledger)
        hits = [hit for hit in result.seeds if hit.kind == "identifier"]
        assert [(hit.node_id, hit.token) for hit in hits] == [(nodes["chart"].node_id, "c4")]
    finally:
        ledger.close()


def test_two_spellings_of_one_identifier_fold_into_one_key() -> None:
    """``C4`` and ``c4`` are one search key, so a hit on either seeds both nodes."""
    graph, _ = _fixture()
    upper = _create(graph, "upper", facts=(_fact("the upper node", FactKind.IS),), text="upper")
    lower = _create(graph, "lower", facts=(_fact("the lower node", FactKind.IS),), text="lower")
    ledger = _ledger_with(
        _entry("e-01", node_id=upper.node_id, identifiers=("C4",)),
        _entry("e-02", node_id=lower.node_id, identifiers=("c4",)),
    )
    try:
        result = _read(graph, "C4", ledger=ledger)
        seeded = {hit.node_id for hit in result.seeds if hit.kind == "identifier"}
        assert seeded == {upper.node_id, lower.node_id}
    finally:
        ledger.close()


def test_an_alias_query_seeds_the_node_the_dreamer_named_something_else() -> None:
    """I search by the name I know, not by the name the dreamer kept."""
    graph, _ = _fixture()
    node = _create(
        graph,
        "chart-cluster",
        facts=(_fact("C4 returns 31 tools after #248"),),
        aliases=("C4", "C4 memory server"),
        text="something no query will resemble",
    )
    result = _read(graph, "C4", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert result.seeds[0] == SeedHit(
        node_id=node.node_id,
        kind="alias",
        token="C4",
        similarity=EXACT_SIMILARITY,
    )
    assert render_fact(FLOOR_FACTS[1]) in result.rendered.splitlines()


def test_an_alias_hit_is_case_insensitive() -> None:
    graph, _ = _fixture()
    node = _create(
        graph, "chart-cluster", facts=(_fact("the C4 cluster", FactKind.IS),), aliases=("C4",), text="unrelated"
    )
    result = _read(graph, "what is c4", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert [hit.node_id for hit in result.seeds] == [node.node_id]


def test_the_whole_query_is_tried_as_an_alias_so_a_multi_word_name_matches() -> None:
    """``LiteLLM proxy`` is one name; its two words separately are not it."""
    graph, _ = _fixture()
    node = _create(
        graph,
        "gateway-service",
        facts=(_fact("the proxy fronting JedAI models", FactKind.IS),),
        aliases=("LiteLLM proxy",),
        text="nothing resembling the query text at all",
    )
    result = _read(graph, "LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert result.seeds[0].node_id == node.node_id
    assert result.seeds[0].token == "LiteLLM proxy"


def test_an_exact_seed_outranks_a_knn_seed_of_the_same_kind() -> None:
    """Similarity 1.0 is the ceiling of the measured scale, so no second sort is needed."""
    graph = InMemoryGraph()
    measured = _create(
        graph, "measured", node_type="service", facts=(_fact("the measured node", FactKind.IS),), text="a query about x"
    )
    named = _create(
        graph, "named", node_type="service", facts=(_fact("the named node", FactKind.IS),), text="nothing alike"
    )

    result = _read(graph, "named a query about x", motive=MOTIVE.model_copy(update={"read_k": 2}))
    lines = result.rendered.splitlines()
    assert lines.index("is: the named node") < lines.index("is: the measured node")
    assert [hit.kind for hit in result.seeds] == ["alias", "knn"]
    assert result.seeds[0].node_id == named.node_id
    assert result.seeds[1].node_id == measured.node_id


def test_knn_fills_only_the_slots_the_exact_half_left() -> None:
    graph, _ = _fixture()
    three = MOTIVE.model_copy(update={"read_k": 3})
    result = _read(graph, "gateway", motive=three)
    assert len(result.seeds) == 3
    assert [hit.kind for hit in result.seeds] == ["alias", "knn", "knn"]


def test_exact_hits_are_never_truncated_to_fit_read_k() -> None:
    """Dropping a hit the searcher named is the failure amendment A exists to fix."""
    graph, nodes = _fixture()
    one = MOTIVE.model_copy(update={"read_k": 1})
    result = _read(graph, "gateway chart probe", motive=one)
    assert [hit.kind for hit in result.seeds] == ["alias", "alias", "alias"]
    assert {hit.node_id for hit in result.seeds} == {
        nodes["gateway"].node_id,
        nodes["chart"].node_id,
        nodes["probe"].node_id,
    }


def test_a_read_whose_slots_are_all_exact_never_embeds_the_query() -> None:
    """The one network call in a read, not made when nothing would use it."""
    graph, _ = _fixture()
    embedder = CountingEmbedder()
    result = read(
        query="gateway",
        scope=SCOPE,
        motive=MOTIVE.model_copy(update={"read_k": 1}),
        graph=graph,
        ledger=CavemanLedger(":memory:"),
        embedder=embedder,
        receipts=InMemoryReceipts(),
        now=NOW,
    )
    assert embedder.calls == 0
    assert [hit.kind for hit in result.seeds] == ["alias"]


def test_a_read_with_slots_left_embeds_the_query_exactly_once() -> None:
    graph, _ = _fixture()
    embedder = CountingEmbedder()
    read(
        query="gateway",
        scope=SCOPE,
        motive=MOTIVE.model_copy(update={"read_k": 4}),
        graph=graph,
        ledger=CavemanLedger(":memory:"),
        embedder=embedder,
        receipts=InMemoryReceipts(),
        now=NOW,
    )
    assert embedder.calls == 1


def test_seeds_are_deduped_by_node_id_with_the_first_mechanism_winning() -> None:
    """A node found by its name and by its identifier is one seed, not two."""
    graph, _ = _fixture()
    node = _create(graph, "probe-run", facts=(_fact("probe #246 names it"),), aliases=("#246",), text="unrelated")
    ledger = _ledger_with(_entry("e-01", node_id=node.node_id, identifiers=("#246",)))
    try:
        result = _read(graph, "#246", motive=MOTIVE.model_copy(update={"read_k": 1}), ledger=ledger)
        assert [(hit.node_id, hit.kind) for hit in result.seeds] == [(node.node_id, "alias")]
    finally:
        ledger.close()


def test_one_node_named_twice_by_one_query_is_one_seed() -> None:
    graph, _ = _fixture()
    node = _create(
        graph, "gateway-node", facts=(_fact("one node, two names", FactKind.IS),), aliases=("proxy",), text="unrelated"
    )
    result = _read(graph, "gateway-node proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert [hit.node_id for hit in result.seeds].count(node.node_id) == 1


def test_an_identifier_bound_to_a_node_the_graph_no_longer_holds_seeds_nothing() -> None:
    """Erasure deletes a node and leaves the ledger's identifier binding behind."""
    graph, _ = _fixture()
    ledger = _ledger_with(_entry("e-01", node_id="n-erased", identifiers=("#999",)))
    try:
        result = _read(graph, "#999", motive=MOTIVE.model_copy(update={"read_k": 1}), ledger=ledger)
        assert all(hit.kind == "knn" for hit in result.seeds)
        assert "n-erased" not in {hit.node_id for hit in result.seeds}
    finally:
        ledger.close()


def test_an_identifier_from_another_scope_does_not_seed_this_read() -> None:
    graph, _ = _fixture()
    other = _create(
        graph, "other-gateway", facts=(_fact("other scope constraint", FactKind.RULE),), scope="repo:jedai/other"
    )
    ledger = _ledger_with(
        _entry("e-01", node_id=other.node_id, scope="repo:jedai/other", identifiers=("#246",)),
    )
    try:
        result = _read(graph, "#246", motive=MOTIVE.model_copy(update={"read_k": 1}), ledger=ledger)
        assert other.node_id not in {hit.node_id for hit in result.seeds}
    finally:
        ledger.close()


def test_an_alias_from_another_scope_does_not_seed_this_read() -> None:
    graph, _ = _fixture()
    _create(graph, "other-name", aliases=("C4",), facts=(_fact("elsewhere", FactKind.IS),), scope="repo:jedai/other")
    result = _read(graph, "C4")
    assert all(node.scope == SCOPE for node in result.nodes)
    assert all(hit.kind == "knn" for hit in result.seeds)


def test_a_knn_seed_carries_no_token_and_its_measured_similarity() -> None:
    graph, _ = _fixture()
    result = _read(graph, "what do I know about the JedAI proxy", motive=MOTIVE.model_copy(update={"read_k": 2}))
    knn = [hit for hit in result.seeds if hit.kind == "knn"]
    assert knn
    for hit in knn:
        assert hit.token is None
        assert 0.0 < hit.similarity < EXACT_SIMILARITY


def test_seeds_report_a_seed_whose_every_line_lost_the_cut() -> None:
    """ "The query matched this and the budget dropped it" is its own answer."""
    graph, _ = _fixture()
    result = _read(graph, "gateway chart", motive=MOTIVE.model_copy(update={"read_line_budget": 1}))
    assert len(result.nodes) == 1
    assert len(result.seeds) > len(result.nodes)


def test_the_receipt_detail_names_how_many_seeds_were_exact() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    _read(graph, "gateway", motive=MOTIVE.model_copy(update={"read_k": 2}), receipts=receipts)
    assert "(1 exact, 1 below the kNN floor 0.25)" in receipts.all()[0].detail


def test_an_empty_scope_has_no_seeds() -> None:
    result = _read(InMemoryGraph(), "gateway")
    assert result.seeds == ()


# ========================================================= the kNN similarity floor

FLOOR_FACTS = (
    _fact("agent-memory must reach the gateway to serve", FactKind.RULE),
    _fact("C4 returns 31 tools after #248"),
)
"""The exact-hit node's facts: one rule, one attribute, so block order is checkable."""


def _floor_fixture() -> tuple[InMemoryGraph, dict[str, Node]]:
    """One aliased node, and two nodes linked to nothing that the floor must refuse.

    Measured against ``LocalEmbeddingTransport`` (``local-trigram-256@v1``), the
    numbers this section asserts on:

    ========================  ==========  ===========  ===========
    query                     ``C4`` node ``gateway``  ``weather``
    ========================  ==========  ===========  ===========
    ``"C4"``                  0.2100      0.0488       0.0000
    ``"C4 memory server"``    0.5604      0.2130       0.0597
    ========================  ==========  ===========  ===========

    Re-measured for #251 amendment D: the fixture's facts are plain words now
    rather than sigil-prefixed lines, and a node's embedding is over its fact
    text, so the numbers moved. The PROPERTY did not, and it is the one that
    matters: ``C4`` at 0.2100 means the node the query names by alias scores
    BELOW the 0.25 floor on the very query that names it. An implementation that
    floored the exact half would drop the answer, which is why that is a test
    and not a remark.

    Nothing is linked, so a floored seed's absence is unambiguous: there is no
    1-hop path by which it could arrive anyway.
    """
    graph = InMemoryGraph()
    c4 = _create(graph, "C4 memory server", node_type="artifact", facts=FLOOR_FACTS, aliases=("C4",))
    gateway = _create(graph, "gateway", node_type="service", facts=GATEWAY_FACTS)
    weather = _create(graph, "weather", node_type="topic", facts=(_fact("rain is wet in the afternoon"),))
    return graph, {
        "C4": graph.get_node(c4.node_id),
        "gateway": graph.get_node(gateway.node_id),
        "weather": graph.get_node(weather.node_id),
    }


def test_an_exact_alias_hit_leads_and_the_weak_neighbours_are_not_emitted() -> None:
    """The whole of amendment C's read side, on one read.

    ``read('C4')`` names one node. The other two are unlinked and score 0.0
    against the query, so a read that answered with all three answered nothing
    -- which is what ``read_k=8`` over a nine-node scope did on the live run.
    """
    graph, nodes = _floor_fixture()
    result = _read(graph, "C4")
    assert [node.node_id for node in result.nodes] == [nodes["C4"].node_id]
    assert "rain is wet" not in result.rendered
    assert "LiteLLM proxy" not in result.rendered


def test_the_floor_never_drops_an_exact_seed_however_it_measures() -> None:
    """``C4`` measures 0.2100 against its own node, under a 0.25 floor, and still seeds.

    An exact hit is not a measurement: ``EXACT_SIMILARITY`` stands for "this is
    the thing the reader named", and comparing it against a floor meant for
    resemblances would delete the answer to the most precise query there is.
    """
    graph, nodes = _floor_fixture()
    motive = MOTIVE.model_copy(update={"knn_min_similarity": 0.99})
    result = _read(graph, "C4", motive=motive)
    exact = [hit for hit in result.seeds if hit.kind == "alias"]
    assert [hit.node_id for hit in exact] == [nodes["C4"].node_id]
    assert all(hit.kept for hit in exact)
    assert [node.node_id for node in result.nodes] == [nodes["C4"].node_id]


def test_a_floored_candidate_is_reported_with_kept_false_and_its_similarity() -> None:
    """What kNN offered stays answerable from the result, which is how the floor is judged."""
    graph, nodes = _floor_fixture()
    result = _read(graph, "C4 memory server")
    floored = {hit.node_id: hit for hit in result.seeds if not hit.kept}
    assert set(floored) == {nodes["gateway"].node_id, nodes["weather"].node_id}
    for hit in floored.values():
        assert hit.kind == "knn"
        assert hit.token is None
        assert hit.similarity < MOTIVE.knn_min_similarity
    assert floored[nodes["gateway"].node_id].similarity == pytest.approx(0.2130, abs=1e-4)


def test_a_floored_candidate_contributes_no_line_and_no_neighbour() -> None:
    """Dropped BEFORE the 1-hop expansion, which is where the larger cost was."""
    graph, nodes = _floor_fixture()
    _relate(graph, nodes["gateway"], nodes["weather"])
    result = _read(graph, "C4")
    assert [node.node_id for node in result.nodes] == [nodes["C4"].node_id]
    assert "rain is wet" not in result.rendered


def test_a_candidate_exactly_at_the_floor_is_kept() -> None:
    """``>=``, stated as a test, because a boundary nobody pinned is a boundary that moves.

    The floor is taken from the candidate's OWN measured similarity rather than
    from a literal, so this asserts the comparison and not a rounding: a
    four-decimal literal is never exactly the float the embedder produced, which
    is how a boundary test comes to pass for the wrong reason.
    """
    graph, nodes = _floor_fixture()
    wide = MOTIVE.model_copy(update={"knn_min_similarity": -1.0})
    measured = {hit.node_id: hit.similarity for hit in _read(graph, "C4 memory server", motive=wide).seeds}
    exactly = measured[nodes["gateway"].node_id]

    kept = {
        hit.node_id
        for hit in _read(
            graph, "C4 memory server", motive=wide.model_copy(update={"knn_min_similarity": exactly})
        ).seeds
        if hit.kept
    }
    assert nodes["gateway"].node_id in kept
    assert nodes["weather"].node_id not in kept

    above = {
        hit.node_id
        for hit in _read(
            graph,
            "C4 memory server",
            motive=wide.model_copy(update={"knn_min_similarity": math.nextafter(exactly, 1.0)}),
        ).seeds
        if hit.kept
    }
    assert nodes["gateway"].node_id not in above


def test_lowering_the_floor_to_the_cosine_minimum_admits_everything() -> None:
    """The control: the exclusions above are the floor's work and not the scope's."""
    graph, nodes = _floor_fixture()
    wide = MOTIVE.model_copy(update={"knn_min_similarity": -1.0})
    result = _read(graph, "C4 memory server", motive=wide)
    assert all(hit.kept for hit in result.seeds)
    assert {node.node_id for node in result.nodes} == {node.node_id for node in nodes.values()}


def test_a_floored_seed_does_not_free_its_slot_for_a_further_candidate() -> None:
    """The floor makes a read smaller; it does not reach deeper into the ranking.

    ``read_k=2`` with the only exact hit plus one kNN slot: the slot is offered
    to the best remaining candidate, and if that candidate is floored the read
    stops there rather than trying the next one down.
    """
    graph, nodes = _floor_fixture()
    motive = MOTIVE.model_copy(update={"read_k": 2, "knn_min_similarity": 0.3})
    result = _read(graph, "C4 memory server", motive=motive)
    assert [(hit.kind, hit.kept) for hit in result.seeds] == [("alias", True), ("knn", False)]
    assert [node.node_id for node in result.nodes] == [nodes["C4"].node_id]


def test_every_exact_hit_is_reported_kept() -> None:
    """``kept`` is only ever ``False`` for a floored kNN candidate."""
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart probe")
    assert all(hit.kept for hit in result.seeds if hit.kind != "knn")


def test_the_receipt_detail_counts_the_floored_candidates() -> None:
    """A read that retrieved less than it could have says so in the audit stream."""
    graph, _ = _floor_fixture()
    receipts = InMemoryReceipts()
    _read(graph, "C4 memory server", receipts=receipts)
    assert "2 below the kNN floor 0.25" in receipts.all()[0].detail


def test_the_contract_digest_changes_with_the_floor() -> None:
    """It decides what the read retrieves, so two floors are two policies."""
    graph, _ = _floor_fixture()
    before = _read(graph, "C4 memory server")
    after = _read(graph, "C4 memory server", motive=MOTIVE.model_copy(update={"knn_min_similarity": 0.1}))
    assert after.contract_digest != before.contract_digest
    assert (
        read_contract_digest(
            query="C4 memory server",
            motive=MOTIVE.model_copy(update={"knn_min_similarity": 0.1}),
            embedding_identifier=EMBED.identifier,
        )
        == after.contract_digest
    )


def test_an_exact_hits_lines_carry_the_exact_floor_and_nobody_elses_do() -> None:
    """The arithmetic ``read`` hands the ranker, asserted through the ranker's own constant."""
    graph, nodes = _floor_fixture()
    wide = MOTIVE.model_copy(update={"knn_min_similarity": -1.0})
    result = _read(graph, "C4 memory server", motive=wide)
    assert result.nodes[0].node_id == nodes["C4"].node_id
    assert _blocks(result)[0].splitlines()[1].startswith("rule: ")
    assert EXACT_FLOOR > CONSTRAINT_FLOOR


def test_a_brief_passes_no_exact_ids_so_the_constraint_holder_still_leads() -> None:
    """The asymmetry: a brief's question is what must never be violated here."""
    graph, nodes = _floor_fixture()
    result = brief(
        scope=SCOPE,
        motive=MOTIVE,
        graph=graph,
        ledger=CavemanLedger(":memory:"),
        receipts=InMemoryReceipts(),
        now=NOW,
    )
    assert result.seeds == ()
    assert {node.node_id for node in result.nodes} == {node.node_id for node in nodes.values()}
    assert result.rendered.splitlines()[1].startswith("rule: ")


# ============================================================== node ids carried


def test_a_read_reports_its_emitted_node_ids_in_block_order() -> None:
    """The field an agent acts on: what ``explain``, ``node`` and ``neighbors`` take."""
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert result.node_ids == tuple(node.node_id for node in result.nodes)
    assert len(result.node_ids) == len(set(result.node_ids))
    for node_id, block in zip(result.node_ids, _blocks(result), strict=True):
        assert f"[{node_id}]" in block.splitlines()[0]


def test_the_ids_a_read_reports_are_the_ids_its_footer_names() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert _footer(result) == footer(result.node_ids)


def test_a_brief_reports_its_node_ids_too() -> None:
    graph, _ = _fixture()
    result = _brief(graph)
    assert result.node_ids == tuple(node.node_id for node in result.nodes)
    assert result.node_ids != ()


def test_a_read_that_emitted_nothing_reports_no_node_ids() -> None:
    result = _read(InMemoryGraph(), "anything at all")
    assert result.node_ids == ()
    assert result.nodes == ()


def test_every_rendered_relation_names_the_other_ends_node_id() -> None:
    """What makes a read traversable rather than a dead end at the block boundary."""
    graph, nodes = _fixture()
    result = _read(graph, "gateway agent-memory chart probe")
    edges = [line for line in result.rendered.splitlines() if "BLOCKED" in line or "DEPLOYS" in line]
    assert len(edges) == 2
    known = {node.node_id for node in nodes.values()}
    for line in edges:
        named = {token.strip("[]") for token in line.split() if token.startswith("[n-")}
        assert named and named <= known


# ============================================================ header and footer


def test_a_read_ends_with_one_footer_naming_the_emitted_nodes_in_block_order() -> None:
    """A rendered id was a pointer with no verb. This is the verbs -- both of them.

    Amendment D added ``neighbors`` beside ``explain``, because the edge between
    two concepts is no longer a line in the block: the second call is how the
    reader reaches it.
    """
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert _footer(result) == footer([node.node_id for node in result.nodes])
    assert "explain(" in _footer(result)
    assert "neighbors(" in _footer(result)


def test_the_footer_names_node_ids_not_names_because_that_is_what_explain_takes() -> None:
    graph, nodes = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert nodes["gateway"].node_id in _footer(result)
    assert f"({nodes['gateway'].name}" not in _footer(result)


def test_a_read_that_emitted_nothing_has_no_footer_to_go_deeper_into() -> None:
    result = _read(InMemoryGraph(), "anything at all")
    assert result.rendered == ""


def test_the_footer_names_a_node_once_per_call_however_many_facts_were_kept() -> None:
    graph, _ = _fixture()
    result = _read(graph, "gateway LiteLLM proxy claude-haiku", motive=MOTIVE.model_copy(update={"read_k": 1}))
    rendered_footer = _footer(result)
    for node in result.nodes:
        # Twice: once inside ``explain(...)`` and once inside ``neighbors(...)``.
        # The property is one mention PER CALL, not one per kept fact -- the
        # reader needs each verb once, with each id.
        assert rendered_footer.count(node.node_id) == 2


def test_the_footer_is_not_counted_as_an_emitted_line() -> None:
    """It is a call, not a caveman line: the budget is over the lines."""
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert result.line_count == sum(len(block.splitlines()) - 1 for block in _blocks(result))


def test_the_footer_is_counted_in_the_token_report() -> None:
    """A cost the reader pays is a cost the reader is told about."""
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart")
    assert result.token_count == estimate_tokens(result.rendered)
    assert estimate_tokens(_footer(result)) > 0


def test_beliefs_within_a_block_are_rendered_in_kind_order_not_rank_order() -> None:
    """A block a reader skims is one whose kinds are always in the same place.

    Stated as the whole expected block, because the relation's position is the
    part that cannot be inferred: an edge reads after everything the node
    asserts on its own and before what it only suspects, which puts it between
    ``chat default`` and the affinity ``unsure``.
    """
    graph, nodes = _fixture()
    result = _read(graph, "gateway LiteLLM proxy", motive=MOTIVE.model_copy(update={"read_k": 1}))
    gateway = _blocks(result)[0].splitlines()[1:]
    memory_id = nodes["agent-memory"].node_id
    assert gateway == _kind_ordered(gateway)
    assert gateway == [
        render_fact(GATEWAY_FACTS[0]),
        render_fact(GATEWAY_FACTS[1]),
        render_fact(GATEWAY_FACTS[2]),
        f"from agent-memory [{memory_id}] BLOCKED: the Host header was refused pre-#245",
        render_fact(GATEWAY_FACTS[3]),
    ]


def test_every_block_is_in_kind_order_whatever_the_query() -> None:
    graph, _ = _fixture()
    result = _read(graph, "chart Helm deploying memotron #240")
    for block in _blocks(result):
        lines = block.splitlines()[1:]
        assert lines == _kind_ordered(lines)


def test_the_kind_order_holds_over_a_large_scope() -> None:
    graph = _two_hundred_node_scope()
    result = _read(graph, "what do I know about the gateway")
    for block in _blocks(result):
        lines = block.splitlines()[1:]
        assert lines == _kind_ordered(lines)


# ================================================================== brief


def _brief(
    graph: InMemoryGraph,
    *,
    motive: CavemanMotive = MOTIVE,
    receipts: InMemoryReceipts | None = None,
    scope: str = SCOPE,
    ledger: CavemanLedger | None = None,
    now: datetime = NOW,
) -> ReadResult:
    """A brief against the real stores. No embedder argument, because there is none."""
    return brief(
        scope=scope,
        motive=motive,
        graph=graph,
        ledger=ledger if ledger is not None else CavemanLedger(":memory:"),
        receipts=receipts if receipts is not None else InMemoryReceipts(),
        now=now,
    )


SCOPE_CONSTRAINTS = frozenset({render_fact(GATEWAY_FACTS[0]), render_fact(AGENT_MEMORY_FACTS[0])})


def test_a_brief_emits_every_constraint_line_in_the_scope() -> None:
    """After a compaction there is no query. There are still hard rules."""
    graph, _ = _fixture()
    result = _brief(graph)
    lines = {text for block in _blocks(result) for text in block.splitlines()[1:]}
    assert lines >= SCOPE_CONSTRAINTS


def test_a_briefs_constraints_take_the_budget_before_anything_else() -> None:
    """ "Constraints first" is a claim about the SELECTION, which the cut is where it shows.

    Ranked flat, every ``!`` line in the scope outranks every other line in it —
    so a budget the size of the constraint set keeps exactly the constraint set.
    The rendered order then groups by node, which is why this is asserted at the
    cut rather than over the flat rendering: a block's own ``!`` line comes
    first within it, but the second block's cannot precede the first block's
    other lines without breaking the blocks apart.
    """
    graph, _ = _fixture()
    result = _brief(graph, motive=MOTIVE.model_copy(update={"read_line_budget": len(SCOPE_CONSTRAINTS)}))
    assert {text for block in _blocks(result) for text in block.splitlines()[1:]} == SCOPE_CONSTRAINTS


def test_a_brief_leads_with_the_node_holding_a_constraint() -> None:
    graph, _ = _fixture()
    result = _brief(graph)
    assert _blocks(result)[0].splitlines()[1] in SCOPE_CONSTRAINTS


def test_a_brief_takes_no_query_and_says_so() -> None:
    graph, _ = _fixture()
    assert _brief(graph).query == ""


def test_a_brief_orders_the_rest_of_the_budget_by_node_value() -> None:
    """The same value function pressure kills nodes by, so a brief shows the real scope."""
    graph = InMemoryGraph()
    quiet = _create(graph, "quiet", node_type="service", facts=(_fact("the quiet node", FactKind.IS),))
    loud = _create(graph, "loud", node_type="service", facts=(_fact("the loud node", FactKind.IS),))
    for _ in range(5):
        graph.record_read([loud.node_id])

    result = _brief(graph)
    assert [node.node_id for node in result.nodes] == [loud.node_id, quiet.node_id]
    assert node_value(
        graph.get_node(loud.node_id),
        degree=0,
        now=NOW,
        weights=MOTIVE.value_weights,
        half_life_days=MOTIVE.recency_half_life_days,
        type_weight=MOTIVE.type_weight,
    ) > node_value(
        graph.get_node(quiet.node_id),
        degree=0,
        now=NOW,
        weights=MOTIVE.value_weights,
        half_life_days=MOTIVE.recency_half_life_days,
        type_weight=MOTIVE.type_weight,
    )


def test_a_brief_keeps_a_low_value_nodes_constraint_over_a_high_value_nodes_attribute() -> None:
    """Value orders the budget; it never reorders a hard rule below anything."""
    graph = InMemoryGraph()
    ruled = _create(graph, "ruled", node_type="service", facts=(_fact("the rule nobody may break", FactKind.RULE),))
    read_often = _create(graph, "read-often", node_type="service", facts=(_fact("a much-read attribute"),))
    for _ in range(50):
        graph.record_read([read_often.node_id])

    result = _brief(graph, motive=MOTIVE.model_copy(update={"read_line_budget": 1}))
    assert [node.node_id for node in result.nodes] == [ruled.node_id]
    assert result.saturated is True


def test_a_brief_of_an_empty_scope_is_an_empty_answer_and_still_receipts() -> None:
    receipts = InMemoryReceipts()
    result = _brief(InMemoryGraph(), receipts=receipts)
    assert result.rendered == ""
    assert result.nodes == ()
    assert result.line_count == 0
    assert result.saturated is False
    assert [receipt.op for receipt in receipts.all()] == [ReceiptOp.READ_EMITTED]


def test_a_brief_is_scoped() -> None:
    graph, _ = _fixture()
    _create(
        graph, "elsewhere", facts=(_fact("a constraint in another scope", FactKind.RULE),), scope="repo:jedai/other"
    )
    result = _brief(graph)
    assert all(node.scope == SCOPE for node in result.nodes)
    assert "! a constraint in another scope" not in result.rendered


def test_a_brief_has_no_seeds_because_it_had_no_query() -> None:
    graph, _ = _fixture()
    assert _brief(graph).seeds == ()


def test_a_brief_renders_headers_kind_order_and_the_footer() -> None:
    """The same rendering contract as a read: a brief is a read without a query."""
    graph, nodes = _fixture()
    ledger = _ledger_with(_entry("e-01", node_id=nodes["gateway"].node_id))
    try:
        result = _brief(graph, ledger=ledger)
        for block, node in zip(_blocks(result), result.nodes, strict=True):
            header, *lines = block.splitlines()
            entries = 1 if node.node_id == nodes["gateway"].node_id else 0
            assert header == render_header(node, entries=entries)
            assert lines == _kind_ordered(lines)
        assert _footer(result) == footer([node.node_id for node in result.nodes])
    finally:
        ledger.close()


def test_a_brief_is_bounded_by_both_budgets() -> None:
    graph = _two_hundred_node_scope()
    result = _brief(graph)
    assert result.line_count == MOTIVE.read_line_budget
    assert sum(estimate_tokens(text) for block in _blocks(result) for text in block.splitlines()[1:]) <= (
        MOTIVE.read_token_budget
    )
    assert result.saturated is True


def test_a_briefs_token_cut_can_be_what_binds() -> None:
    graph, _ = _fixture()
    starved = MOTIVE.model_copy(update={"read_token_budget": 1, "read_line_budget": 100})
    result = _brief(graph, motive=starved)
    assert result.line_count == 0
    assert result.rendered == ""
    assert result.saturated is True


def test_a_brief_records_a_read_once_per_emitted_node() -> None:
    graph, nodes = _fixture()
    result = _brief(graph)
    assert {node.node_id for node in result.nodes} == {node.node_id for node in nodes.values()}
    for node in nodes.values():
        assert graph.get_node(node.node_id).read_count == 1


def test_a_brief_does_not_count_a_node_whose_every_line_lost_the_cut_as_read() -> None:
    graph, _ = _fixture()
    result = _brief(graph, motive=MOTIVE.model_copy(update={"read_line_budget": 1}))
    counted = [node for node in graph.list_nodes(scope=SCOPE) if node.read_count > 0]
    assert [node.node_id for node in counted] == [result.nodes[0].node_id]


def test_a_briefs_receipt_carries_the_brief_digest_and_names_its_source() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    result = _brief(graph, receipts=receipts)
    emitted = receipts.all()[0]
    assert emitted.op is ReceiptOp.READ_EMITTED
    assert emitted.inputs_digest == brief_contract_digest(motive=MOTIVE) == result.contract_digest
    assert emitted.outputs_digest == text_digest(result.rendered)
    assert "no query" in emitted.detail


def test_a_saturated_brief_also_emits_a_budget_saturated_receipt() -> None:
    graph, _ = _fixture()
    receipts = InMemoryReceipts()
    _brief(graph, motive=MOTIVE.model_copy(update={"read_line_budget": 2}), receipts=receipts)
    assert [receipt.op for receipt in receipts.all()] == [
        ReceiptOp.READ_EMITTED,
        ReceiptOp.READ_BUDGET_SATURATED,
    ]


def test_the_brief_digest_changes_with_the_motive_and_nothing_else() -> None:
    graph, _ = _fixture()
    engineering = _brief(graph).contract_digest
    assert _brief(graph).contract_digest == engineering
    assert _brief(graph, motive=assistant_motive()).contract_digest != engineering

    _create(graph, "newcomer", facts=(_fact("a brand new constraint arrives", FactKind.RULE),))
    assert _brief(graph).contract_digest == engineering


def test_a_brief_digest_is_not_a_read_of_the_empty_query() -> None:
    """The literal ``"brief"`` is what keeps the two contracts apart."""
    assert brief_contract_digest(motive=MOTIVE) != read_contract_digest(
        query="",
        motive=MOTIVE,
        embedding_identifier=EMBED.identifier,
    )


def test_two_briefs_of_one_scope_are_byte_identical() -> None:
    graph = _two_hundred_node_scope()
    first = _brief(graph)
    second = _brief(graph)
    assert first.rendered == second.rendered


def test_a_brief_refuses_a_node_last_touched_after_now() -> None:
    """A node from the future is a clock defect, and value is not scoreable."""
    graph, _ = _fixture()
    with pytest.raises(ValueError, match="after now="):
        _brief(graph, now=NOW - timedelta(days=1))


def test_a_brief_reads_the_facts_a_wider_motive_wrote_too() -> None:
    """The same move as :func:`test_a_read_never_fails_on_the_facts_a_wider_motive_wrote`.

    A brief is a read without a query, so it inherits the change rather than
    keeping a rejection of its own.
    """
    graph, _ = _fixture()
    strict = _brief(graph, motive=MOTIVE.model_copy(update={"max_fact_tokens": 4}))
    assert strict.line_count > 0


def test_a_brief_renders_the_edges_between_the_nodes_it_shows() -> None:
    """A session-start read that hid the edges would hide half of what the scope asserts."""
    graph, _ = _fixture()
    rendered = _brief(graph).rendered.splitlines()
    assert sum(1 for line in rendered if "BLOCKED" in line) == 1
    assert sum(1 for line in rendered if "DEPLOYS" in line) == 1


def test_a_brief_of_an_edgeless_scope_renders_only_facts() -> None:
    """No edges to offer, so every body line is a fact. Headers still carry their id."""
    result = _brief(_two_unrelated_nodes())
    bodies = [line for block in _blocks(result) for line in block.splitlines()[1:]]
    assert sorted(bodies) == ["is: the alpha node", "is: the zulu node"]


# ========================================================= duplicate line fold

REFUTATION = _fact("pre-#245 the gateway refused the Host header outright")
"""The measured symptom, as an ATTRIBUTE.

It was a refuted line when amendment B found it emitted from two nodes. The
kind it became under amendment D is ``superseded``, which a bounded read never
renders at all -- so the fold has to be exercised on a kind a read DOES emit, or
the test would pass on an empty rendering. What is being asserted is the fold,
not the kind.
"""


def _two_nodes_stating_one_fact() -> InMemoryGraph:
    """Two nodes, each carrying :data:`REFUTATION`, both named by one query.

    The types are what decide which copy survives -- ``policy`` (1.2) outranks
    ``artifact`` (1.0) under the engineering motive -- and the NAMES point the
    other way, so a test that passes because the survivor sorted first
    alphabetically cannot pass here.
    """
    graph = InMemoryGraph()
    _create(graph, "alpha-node", node_type="artifact", facts=(_fact("the alpha node", FactKind.IS), REFUTATION))
    _create(graph, "zulu-node", node_type="policy", facts=(_fact("the zulu node", FactKind.IS), REFUTATION))
    return graph


def test_one_fact_on_two_nodes_is_emitted_once() -> None:
    graph = _two_nodes_stating_one_fact()
    result = _read(graph, "alpha-node zulu-node")
    assert result.rendered.splitlines().count(render_fact(REFUTATION)) == 1
    assert result.duplicates_dropped == 1


def test_the_surviving_copy_is_the_higher_ranked_nodes() -> None:
    """Rank decides, not the alphabet: ``zulu-node`` is a ``policy``."""
    graph = _two_nodes_stating_one_fact()
    result = _read(graph, "alpha-node zulu-node")
    holding = [block for block in _blocks(result) if render_fact(REFUTATION) in block.splitlines()[1:]]
    assert len(holding) == 1
    assert holding[0].startswith("zulu-node (policy)")


def _two_unrelated_nodes() -> InMemoryGraph:
    """Two nodes stating different things, with no edge between them.

    The only shape in which nothing at all is said twice, now that an edge is a
    belief both of its endpoints offer. ``_fixture`` has two edges and therefore
    two folds, which is what the test below this one asserts.
    """
    graph = InMemoryGraph()
    _create(graph, "alpha-node", node_type="artifact", facts=(_fact("the alpha node", FactKind.IS),))
    _create(graph, "zulu-node", node_type="policy", facts=(_fact("the zulu node", FactKind.IS),))
    return graph


def test_a_read_with_nothing_said_twice_drops_nothing() -> None:
    """The common case, and the one where the receipt detail is unchanged."""
    graph = _two_unrelated_nodes()
    receipts = InMemoryReceipts()
    result = _read(graph, "alpha-node zulu-node", receipts=receipts)
    assert result.duplicates_dropped == 0
    assert "duplicate" not in receipts.all()[0].detail


def test_one_edge_offered_by_both_of_its_ends_folds_to_one_line_per_edge() -> None:
    """Not a restatement, and reported the same way as one, which is the point.

    A seed offers every edge that touches it, so a read that seeds both ends of
    an edge has the belief twice. The fold keeps one, the reader is told it once,
    and the count says so rather than the read quietly emitting the same edge in
    two blocks.
    """
    graph, _ = _fixture()
    result = _read(graph, "gateway agent-memory chart probe", receipts=(receipts := InMemoryReceipts()))
    emitted = result.rendered.splitlines()
    assert sum(1 for line in emitted if "BLOCKED" in line) == 1
    assert sum(1 for line in emitted if "DEPLOYS" in line) == 1
    assert result.duplicates_dropped == len(graph.relations(scope=SCOPE)) == 2
    assert "dropped 2 duplicate fact(s)" in receipts.all()[0].detail


def test_the_read_emitted_detail_reports_the_fold() -> None:
    graph = _two_nodes_stating_one_fact()
    receipts = InMemoryReceipts()
    _read(graph, "alpha-node zulu-node", receipts=receipts)
    assert "dropped 1 duplicate fact(s)" in receipts.all()[0].detail


def test_a_folded_duplicate_is_not_a_saturation() -> None:
    """Two different events: already told, versus never told.

    The budget here is wide enough to hold everything that survived the fold, so
    a read that counted the duplicate as a drop would claim it had truncated.
    """
    graph = _two_nodes_stating_one_fact()
    receipts = InMemoryReceipts()
    result = _read(graph, "alpha-node zulu-node", receipts=receipts)
    assert result.duplicates_dropped == 1
    assert result.saturated is False
    assert [receipt.op for receipt in receipts.all()] == [ReceiptOp.READ_EMITTED]


def test_the_fold_gives_its_line_back_to_the_budget() -> None:
    """Dedupe runs BEFORE the cut, so the freed line goes to the next real fact.

    The restated line is a ``!`` so that it provably holds the top of the rank
    (:data:`CONSTRAINT_FLOOR`) and the two copies would otherwise take the whole
    two-line budget between them, emitting the third fact nowhere.
    """
    rule = _fact("never hermetic, always through the gateway", FactKind.RULE)
    graph = InMemoryGraph()
    _create(graph, "alpha-node", node_type="artifact", facts=(rule,))
    _create(graph, "zulu-node", node_type="policy", facts=(rule,))
    _create(graph, "third-node", node_type="policy", facts=(_fact("a third fact nobody else states"),))

    result = _read(graph, "alpha-node zulu-node third-node", motive=MOTIVE.model_copy(update={"read_line_budget": 2}))
    emitted = {text for block in _blocks(result) for text in block.splitlines()[1:]}
    assert emitted == {render_fact(rule), "attribute: a third fact nobody else states"}
    assert result.duplicates_dropped == 1


def test_a_node_whose_only_line_was_a_duplicate_is_not_emitted_at_all() -> None:
    """No block, no footer entry, and no ``record_read``: it contributed nothing."""
    graph = InMemoryGraph()
    kept = _create(graph, "zulu-node", node_type="policy", facts=(REFUTATION,))
    folded = _create(graph, "alpha-node", node_type="artifact", facts=(REFUTATION,))
    result = _read(graph, "alpha-node zulu-node")

    assert [node.node_id for node in result.nodes] == [kept.node_id]
    assert folded.node_id not in _footer(result)
    assert graph.get_node(folded.node_id).read_count == 0
    assert graph.get_node(kept.node_id).read_count == 1


def test_the_folded_line_is_still_on_its_node_and_still_searchable() -> None:
    """Nothing is deleted. The fold is a rendering decision, not a mutation."""
    graph = InMemoryGraph()
    folded = _create(
        graph, "alpha-node", node_type="artifact", facts=(_fact("the alpha node", FactKind.IS), REFUTATION)
    )
    _create(graph, "zulu-node", node_type="policy", facts=(_fact("the zulu node", FactKind.IS), REFUTATION))
    _read(graph, "alpha-node zulu-node")

    assert REFUTATION in graph.get_node(folded.node_id).facts
    alone = _read(graph, "alpha-node", motive=MOTIVE.model_copy(update={"read_k": 1}))
    assert render_fact(REFUTATION) in alone.rendered.splitlines()
    assert alone.duplicates_dropped == 0


def test_a_rule_and_a_superseded_fact_of_the_same_words_are_both_emitted() -> None:
    """A standing rule and what used to be true are different facts, and a reader
    needs both.

    This is what the fold's same-KIND clause keeps apart now that a relation is
    an edge rather than a line (#251 amendment D): the pair it used to keep apart
    was a refutation and a relation.
    """
    graph = InMemoryGraph()
    rule = _fact("pre-#245 the gateway refused the Host header outright", FactKind.RULE)
    _create(graph, "alpha-node", node_type="artifact", facts=(rule,))
    _create(graph, "zulu-node", node_type="policy", facts=(REFUTATION,))
    result = _read(graph, "alpha-node zulu-node")
    assert result.duplicates_dropped == 0
    assert render_fact(rule) in result.rendered.splitlines()
    assert render_fact(REFUTATION) in result.rendered.splitlines()


def test_two_issue_numbers_are_two_facts_however_alike_the_wording() -> None:
    """``#245`` and ``#246`` measure 0.75 together; the identifier rule keeps both."""
    graph = InMemoryGraph()
    _create(graph, "alpha-node", node_type="artifact", facts=(REFUTATION,))
    _create(
        graph, "zulu-node", node_type="policy", facts=(_fact("pre-#246 the gateway refused the Host header outright"),)
    )
    result = _read(graph, "alpha-node zulu-node")
    assert result.duplicates_dropped == 0
    assert len(result.nodes) == 2


def test_a_brief_folds_duplicates_too_and_reports_them() -> None:
    """A brief ranks the whole scope flat, so every restatement competes at once."""
    graph = _two_nodes_stating_one_fact()
    receipts = InMemoryReceipts()
    result = _brief(graph, receipts=receipts)
    assert result.rendered.splitlines().count(render_fact(REFUTATION)) == 1
    assert result.duplicates_dropped == 1
    assert "dropped 1 duplicate fact(s)" in receipts.all()[0].detail


def test_a_brief_of_a_scope_with_nothing_restated_drops_nothing() -> None:
    graph = _two_unrelated_nodes()
    receipts = InMemoryReceipts()
    assert _brief(graph, receipts=receipts).duplicates_dropped == 0
    assert "duplicate" not in receipts.all()[0].detail


def test_a_folded_read_still_reports_every_receipt_detail_inside_its_bound() -> None:
    """The fold clause is extra characters in a field capped at 600."""
    graph = _two_nodes_stating_one_fact()
    receipts = InMemoryReceipts()
    _read(graph, "alpha-node zulu-node", motive=MOTIVE.model_copy(update={"read_line_budget": 1}), receipts=receipts)
    assert len(receipts.all()) == 2
    for receipt in receipts.all():
        assert 0 < len(receipt.detail) <= 600


def test_the_saturation_receipt_counts_only_the_lines_that_competed() -> None:
    """A duplicate never reached the cut, so the cut does not price itself against it."""
    graph = _two_nodes_stating_one_fact()
    receipts = InMemoryReceipts()
    result = _read(
        graph, "alpha-node zulu-node", motive=MOTIVE.model_copy(update={"read_line_budget": 1}), receipts=receipts
    )
    assert result.duplicates_dropped == 1
    assert result.saturated is True
    # Four candidate lines, one folded away, one emitted: two competed and lost.
    assert "dropped 2 of 3 ranked facts" in receipts.all()[1].detail


def test_an_empty_read_drops_no_duplicates() -> None:
    assert _read(InMemoryGraph(), "anything at all").duplicates_dropped == 0


def test_a_scope_where_no_node_outvalues_another_still_ranks_and_emits() -> None:
    """Every share equal is what an equal share IS, not a fallback around zero."""
    graph = InMemoryGraph()
    _create(graph, "first", node_type="untyped", facts=(_fact("the first node", FactKind.IS),))
    _create(graph, "second", node_type="untyped", facts=(_fact("the second node", FactKind.IS),))
    flat = MOTIVE.model_copy(
        update={
            "value_weights": ValueWeights(recency=0.0, reads=1.0, degree=0.0, type=0.0),
            "type_weight": {},
        }
    )
    result = _brief(graph, motive=flat)
    assert result.line_count == 2
    assert [node.name for node in result.nodes] == ["first", "second"]
