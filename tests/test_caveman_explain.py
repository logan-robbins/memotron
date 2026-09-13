"""#251 amendment D: the deep read behind a read's ``more: explain(...)`` footer.

Both stores are the REAL ones -- ``InMemoryGraph`` and ``CavemanLedger`` -- because
what is being asserted is a rendering of what those two actually hold, and a
fake ledger would let the supersession edges be whatever the test wanted. The
history section is driven through the REAL ``JournaledGraph``, for the same
reason: an event list built by hand would prove the renderer and nothing about
what the journal records.

There is deliberately **no transport of any kind in this module**, scripted or
otherwise. ``explain`` makes no model call and no embedding call, and the way to
assert that is to give it nothing to call: if it ever grew one, these tests would
fail with a missing argument rather than pass quietly.

The load-bearing assertions:

* **five sections, always present**, so a reader never has to guess whether an
  absent section means "nothing" or "wrong question";
* **every belief names its evidence entry ids**, and every id named resolves in
  the ``evidence:`` section below it -- an id a reader cannot follow is the same
  dead end a bare ``[n-001]`` header was;
* **``superseded`` facts appear here and only here**, because a bounded read
  spends its budget on what is true now;
* **newest first** in both historical sections, over each store's total order, so
  two renderings of one node are byte-identical;
* **both supersession ends**, because ``SUPERSEDED by`` is the field that stops a
  reader acting on a claim that still looks current;
* the **entry count in the header matches the claims rendered under it** -- the
  header's ``N entries`` and the deep read are two statements about one thing.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest

from memotron.caveman.errors import NodeNotFound
from memotron.caveman.explain import (
    ALIAS_PREFIX,
    EVIDENCE_SECTION,
    FACTS_SECTION,
    FIELD_SEPARATOR,
    HISTORY_SECTION,
    NO_ALIASES,
    NOTHING,
    RELATIONS_SECTION,
    SUPERSEDED_FIELD,
    SUPERSEDES_FIELD,
    event_summary,
    explain,
    render_fact_entry,
    render_relation_entry,
)
from memotron.caveman.graph import InMemoryGraph, JournaledGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    ClaimKind,
    ClaimMode,
    DreamOp,
    Fact,
    FactKind,
    FactSpec,
    LedgerEntry,
    Node,
    Relation,
    SplitSpec,
)
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.render import render_fact, render_header, render_relation
from memotron.caveman.seams import Clock
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"
EMBED = LocalEmbeddingTransport()

SECTIONS = (FACTS_SECTION, RELATIONS_SECTION, EVIDENCE_SECTION, HISTORY_SECTION)
"""The four labelled sections, in render order. Read off the module under test."""

RULE = Fact(
    kind=FactKind.RULE,
    text="never hermetic, always through the JedAI gateway",
    entry_ids=("e-01",),
    first_seen=NOW,
    last_seen=NOW,
)


class _FixedClock:
    """A ``Clock`` that never moves, so an event's ``ts`` is not what is asserted."""

    def now(self) -> datetime:
        return NOW


@pytest.fixture
def ledger() -> CavemanLedger:
    store = CavemanLedger(":memory:")
    yield store
    store.close()


def _node(
    graph: InMemoryGraph | JournaledGraph,
    name: str = "gateway",
    *,
    facts: tuple[Fact, ...] = (RULE,),
    aliases: tuple[str, ...] = (),
    node_type: str = "service",
) -> Node:
    node = graph.create_node(
        scope=SCOPE,
        name=name,
        type=node_type,
        facts=facts,
        embedding=EMBED.embed(" ".join((name, *aliases, node_type, *(fact.text for fact in facts)))),
        now=NOW,
        aliases=aliases,
    )
    graph.clear_dirty([node.node_id], now=NOW)
    return graph.get_node(node.node_id)


def _fact(text: str, kind: FactKind = FactKind.ATTRIBUTE, *, entries: tuple[str, ...] = ("e-01",)) -> Fact:
    return Fact(kind=kind, text=text, entry_ids=entries, first_seen=NOW, last_seen=NOW)


def _split_spec(name: str, *, entry: str) -> SplitSpec:
    """One part of a split, carrying the ``FactSpec`` tuple the wire contract asks for.

    The store creates every part factless and dirty, so what this field holds is
    only ever read by ``dream``; it is required because a split part with no
    facts at all is not representable.
    """
    return SplitSpec(
        name=name,
        type="artifact",
        facts=(FactSpec(kind=FactKind.ATTRIBUTE, text=f"part {name}", entry_ids=(entry,)),),
        entry_ids=(entry,),
    )


def _relate(
    graph: InMemoryGraph | JournaledGraph,
    source: Node,
    target: Node,
    *,
    type: str = "BLOCKED",
    claim: str = "the Host header was refused pre-#245",
    entries: tuple[str, ...] = ("e-01",),
    until: str | None = None,
) -> Relation:
    return graph.upsert_relation(
        scope=SCOPE,
        source_id=source.node_id,
        target_id=target.node_id,
        type=type,
        claim=claim,
        entry_ids=entries,
        until=until,
        now=NOW,
    )


def _entry(
    entry_id: str,
    *,
    node_id: str,
    claim: str,
    ts: datetime = NOW,
    kind: ClaimKind = ClaimKind.ATTRIBUTE,
    supersedes: str | None = None,
    claim_mode: ClaimMode = ClaimMode.DESCRIPTIVE,
) -> LedgerEntry:
    return LedgerEntry(
        entry_id=entry_id,
        ts=ts,
        episode_id="ep-251-01",
        scope=SCOPE,
        claim=claim,
        kind=kind,
        claim_mode=claim_mode,
        subjects=("gateway",),
        node_ids=(node_id,),
        motive="engineering",
        confidence=0.9,
        supersedes=supersedes,
        turns=(1,),
        receipt_id="r-01",
    )


def _section_of(rendered: str, label: str) -> list[str]:
    """The lines under *label*, up to the next section label or the aliases line.

    Keyed off the module's own constants rather than off line numbers, which is
    what lets a section grow without every assertion below it moving.
    """
    lines = rendered.splitlines()
    assert label in lines, f"{label!r} is not in the rendered deep read"
    start = lines.index(label) + 1
    stops = {*SECTIONS}
    body: list[str] = []
    for line in lines[start:]:
        if line in stops or line.startswith(ALIAS_PREFIX):
            break
        body.append(line)
    return body


def _claims(lines: Sequence[str]) -> list[str]:
    """The claim field of each rendered evidence line."""
    return [line.split(FIELD_SEPARATOR)[3] for line in lines]


def _journalled(ledger: CavemanLedger) -> tuple[JournaledGraph, InMemoryGraph]:
    """A journal over the REAL store, so the history section renders real events."""
    inner = InMemoryGraph()
    clock: Clock = _FixedClock()
    return JournaledGraph(inner, ledger=ledger, receipts=InMemoryReceipts(), clock=clock), inner


# ================================================================== the header


def test_the_deep_read_begins_with_the_same_header_a_read_renders(ledger: CavemanLedger) -> None:
    """One header shape, so a reader recognises the block they came from."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="The gateway is a LiteLLM proxy."))

    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert rendered.splitlines()[0] == render_header(node, entries=1)


def test_the_headers_entry_count_matches_the_claims_rendered_under_it(ledger: CavemanLedger) -> None:
    """Two statements about one thing, so they must not be able to disagree."""
    graph = InMemoryGraph()
    node = _node(graph)
    for index in range(4):
        ledger.append(_entry(f"e-0{index}", node_id=node.node_id, claim=f"Claim number {index} about the gateway."))

    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert rendered.splitlines()[0].endswith("4 entries")
    assert len(_section_of(rendered, EVIDENCE_SECTION)) == 4


# ================================================================ the sections


def test_every_section_is_rendered_in_the_documented_order(ledger: CavemanLedger) -> None:
    """Five sections, and a reader who knows the order can skim to the one they want."""
    graph = InMemoryGraph()
    node = _node(graph)
    lines = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW).splitlines()
    assert [line for line in lines if line in set(SECTIONS)] == list(SECTIONS)
    assert lines[-1].startswith(ALIAS_PREFIX)


def test_an_empty_section_says_none_rather_than_vanishing(ledger: CavemanLedger) -> None:
    """A section that disappears makes a reader guess whether they asked wrongly."""
    graph = InMemoryGraph()
    node = _node(graph, facts=())
    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    for label in SECTIONS:
        assert _section_of(rendered, label) == [NOTHING]
    assert rendered.splitlines()[-1] == NO_ALIASES


def test_a_node_with_no_entries_still_renders_its_facts(ledger: CavemanLedger) -> None:
    """A real state: erasure deletes an episode's claims and leaves the node."""
    graph = InMemoryGraph()
    node = _node(graph)
    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert _section_of(rendered, FACTS_SECTION) == [render_fact_entry(RULE)]
    assert _section_of(rendered, EVIDENCE_SECTION) == [NOTHING]


def test_the_whole_rendering_is_printable_ascii(ledger: CavemanLedger) -> None:
    """Every rendering in this package is (#251 amendment D). Content here is ASCII."""
    graph, _ = _journalled(ledger)
    gateway = _node(graph, aliases=("LiteLLM proxy",))
    memory = _node(graph, "agent-memory", facts=(_fact("C4 returns 31 tools after #248"),), node_type="artifact")
    _relate(graph, memory, gateway, until="#245")
    ledger.append(_entry("e-01", node_id=gateway.node_id, claim="The gateway fronts the JedAI models."))

    rendered = explain(node_id=gateway.node_id, graph=graph, ledger=ledger, now=NOW)
    assert rendered.isascii()
    assert rendered.isprintable() is False  # newlines
    for line in rendered.splitlines():
        assert line.isprintable()


# ================================================================== the facts


def test_a_fact_renders_with_its_date_its_kind_and_its_evidence_ids(ledger: CavemanLedger) -> None:
    """The provenance a bounded read cannot afford: which claims say this."""
    graph = InMemoryGraph()
    fact = _fact("chat default is claude-sonnet-4-6", entries=("e-01", "e-09"))
    node = _node(graph, facts=(fact,))
    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert _section_of(rendered, FACTS_SECTION) == [
        "2026-09-10 attribute: chat default is claude-sonnet-4-6 (x2) [e-01, e-09]"
    ]


def test_a_facts_belief_text_comes_from_the_one_renderer(ledger: CavemanLedger) -> None:
    """This module adds the date and the ids AROUND ``render_fact``, never instead of it."""
    fact = _fact("embedding is text-embedding-3 at 3072 dims", entries=("e-04",))
    assert render_fact_entry(fact) == f"2026-09-10 {render_fact(fact)} [e-04]"


def test_a_facts_date_is_when_a_claim_last_asserted_it() -> None:
    """``last_seen``, which answers "is this still being said" -- see the docstring."""
    fact = Fact(
        kind=FactKind.IS,
        text="a LiteLLM proxy fronting the JedAI models",
        entry_ids=("e-01", "e-07"),
        first_seen=datetime(2026, 9, 1, tzinfo=UTC),
        last_seen=datetime(2026, 9, 9, tzinfo=UTC),
    )
    assert render_fact_entry(fact).startswith("2026-09-09 ")


def test_a_superseded_fact_is_rendered_here_and_marked_by_its_kind_word(ledger: CavemanLedger) -> None:
    """The one place it appears. A read spends its budget on what is true now."""
    graph = InMemoryGraph()
    old = _fact("chat default was claude-haiku-4-5", FactKind.SUPERSEDED, entries=("e-01",))
    new = _fact("chat default is claude-sonnet-4-6", entries=("e-09",))
    node = _node(graph, facts=(new, old))
    facts = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), FACTS_SECTION)
    assert any(line.endswith("superseded: chat default was claude-haiku-4-5 [e-01]") for line in facts)
    assert any("attribute: chat default is claude-sonnet-4-6" in line for line in facts)


def test_the_facts_section_is_in_kind_order_like_every_other_rendering(ledger: CavemanLedger) -> None:
    """``render.sort_facts`` orders it, so history sorts last and a rule sorts first."""
    graph = InMemoryGraph()
    node = _node(
        graph,
        facts=(
            _fact("chat default is claude-sonnet-4-6"),
            _fact("chat default was claude-haiku-4-5", FactKind.SUPERSEDED),
            RULE,
            _fact("a LiteLLM proxy fronting the JedAI models", FactKind.IS),
        ),
    )
    kinds = [
        line.split(" ", 1)[1].partition(":")[0]
        for line in _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), FACTS_SECTION)
    ]
    assert kinds == ["rule", "is", "attribute", "superseded"]


def test_every_evidence_id_a_fact_names_resolves_in_the_evidence_section(ledger: CavemanLedger) -> None:
    """An id a reader cannot follow is the dead end this section exists to close."""
    graph = InMemoryGraph()
    node = _node(graph, facts=(_fact("C4 returns 31 tools after #248", entries=("e-01", "e-02")),))
    ledger.append(_entry("e-01", node_id=node.node_id, claim="C4 returned 24 tools before the fix."))
    ledger.append(_entry("e-02", node_id=node.node_id, claim="C4 returns 31 tools after #248."))

    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    named = {"e-01", "e-02"}
    resolved = {line.split(FIELD_SEPARATOR)[0] for line in _section_of(rendered, EVIDENCE_SECTION)}
    assert named <= resolved


# =============================================================== the relations


def test_an_outgoing_edge_renders_from_this_node_with_the_other_ends_id(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway, until="#245")

    relations = _section_of(explain(node_id=memory.node_id, graph=graph, ledger=ledger, now=NOW), RELATIONS_SECTION)
    assert relations == [
        f"2026-09-10 BLOCKED gateway [{gateway.node_id}] until #245: the Host header was refused pre-#245 [e-01]"
    ]


def test_an_incoming_edge_leads_with_from_so_direction_is_a_word(ledger: CavemanLedger) -> None:
    """A node that BLOCKED another is not the node that was blocked."""
    graph = InMemoryGraph()
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway)

    relations = _section_of(explain(node_id=gateway.node_id, graph=graph, ledger=ledger, now=NOW), RELATIONS_SECTION)
    assert relations[0].startswith(f"2026-09-10 from agent-memory [{memory.node_id}] BLOCKED: ")


def test_a_relations_belief_text_comes_from_the_one_renderer() -> None:
    relation = Relation(
        relation_id="r-01",
        scope=SCOPE,
        source_id="n-001",
        target_id="n-002",
        type="DEPLOYS",
        claim="the chart deploys it, #240",
        entry_ids=("e-03",),
        until=None,
        first_seen=NOW,
        last_seen=NOW,
    )
    names = {"n-002": "agent-memory"}
    assert render_relation_entry(relation, node_id="n-001", names=names) == (
        f"2026-09-10 {render_relation(relation, node_id='n-001', names=names)} [e-03]"
    )


def test_a_reinforced_edge_reports_every_entry_that_restated_it(ledger: CavemanLedger) -> None:
    """Reinforcement is the union of the evidence, and the deep read names all of it."""
    graph = InMemoryGraph()
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway, entries=("e-01",))
    reinforced = _relate(graph, memory, gateway, entries=("e-09",), claim=None)  # type: ignore[arg-type]
    assert reinforced.evidence == 2

    relations = _section_of(explain(node_id=memory.node_id, graph=graph, ledger=ledger, now=NOW), RELATIONS_SECTION)
    assert relations[0].endswith("(x2) [e-01, e-09]")


def test_an_edge_to_a_node_that_is_gone_renders_its_bare_id(ledger: CavemanLedger) -> None:
    """The id is the thing a reader can act on, so a missing name is not a dead end.

    Reached by deleting one end through the inner store, which is the state a
    caller can hold after another writer moved on.
    """
    graph = InMemoryGraph()
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    relation = _relate(graph, memory, gateway)
    graph._nodes.pop(gateway.node_id)

    relations = _section_of(explain(node_id=memory.node_id, graph=graph, ledger=ledger, now=NOW), RELATIONS_SECTION)
    assert relations[0].startswith(f"2026-09-10 BLOCKED [{relation.target_id}]: ")


# ================================================================ the evidence


def test_each_claim_renders_as_id_date_kind_claim(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(
        _entry(
            "e-01",
            node_id=node.node_id,
            claim="The gateway must never be bypassed for a real run.",
            kind=ClaimKind.RULE,
            ts=datetime(2026, 9, 8, 9, 30, tzinfo=UTC),
        )
    )

    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert _section_of(rendered, EVIDENCE_SECTION) == [
        FIELD_SEPARATOR.join(
            ("e-01", "2026-09-08", ClaimKind.RULE.value, "The gateway must never be bypassed for a real run.")
        )
    ]


def test_claims_render_newest_first(ledger: CavemanLedger) -> None:
    """The newest claim is the one that is true now, so it is the one read first."""
    graph = InMemoryGraph()
    node = _node(graph)
    for day, entry_id in ((1, "e-01"), (5, "e-02"), (9, "e-03")):
        ledger.append(
            _entry(
                entry_id,
                node_id=node.node_id,
                claim=f"A claim recorded on day {day} of the month.",
                ts=datetime(2026, 9, day, 8, 0, tzinfo=UTC),
            )
        )

    lines = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)
    assert [line.split(FIELD_SEPARATOR)[1] for line in lines] == ["2026-09-09", "2026-09-05", "2026-09-01"]


def test_two_claims_sharing_a_timestamp_still_render_in_one_stable_order(ledger: CavemanLedger) -> None:
    """A whole episode is stamped from one clock reading, so ``ts`` is not a total order."""
    graph = InMemoryGraph()
    node = _node(graph)
    for entry_id in ("e-01", "e-02", "e-03"):
        ledger.append(_entry(entry_id, node_id=node.node_id, claim=f"A claim identified as {entry_id} exactly."))

    first = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert first == explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert _claims(_section_of(first, EVIDENCE_SECTION)) == [
        "A claim identified as e-03 exactly.",
        "A claim identified as e-02 exactly.",
        "A claim identified as e-01 exactly.",
    ]


def test_a_claim_that_replaced_another_names_what_it_replaced(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="C4 returns 24 tools in the cluster."))
    ledger.append(
        _entry(
            "e-02",
            node_id=node.node_id,
            claim="C4 returns 31 tools after #248.",
            supersedes="e-01",
            claim_mode=ClaimMode.CORRECTION,
        )
    )

    newest = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)[0]
    assert newest.endswith(f"{FIELD_SEPARATOR}{SUPERSEDES_FIELD}e-01")


def test_a_replaced_claim_is_marked_so_a_reader_does_not_act_on_it(ledger: CavemanLedger) -> None:
    """The reverse edge is the one that stops a stale claim from looking current."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="C4 returns 24 tools in the cluster."))
    ledger.append(_entry("e-02", node_id=node.node_id, claim="C4 returns 31 tools after #248.", supersedes="e-01"))

    oldest = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)[1]
    assert oldest.endswith(f"{FIELD_SEPARATOR}{SUPERSEDED_FIELD}e-02")


def test_a_claim_that_both_replaced_and_was_replaced_carries_both_fields(ledger: CavemanLedger) -> None:
    """Exactly the line a reader must not act on, so neither half may be dropped."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="C4 returns 12 tools in the cluster."))
    ledger.append(_entry("e-02", node_id=node.node_id, claim="C4 returns 24 tools in the cluster.", supersedes="e-01"))
    ledger.append(_entry("e-03", node_id=node.node_id, claim="C4 returns 31 tools after #248.", supersedes="e-02"))

    middle = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)[1]
    assert f"{SUPERSEDES_FIELD}e-01" in middle
    assert f"{SUPERSEDED_FIELD}e-03" in middle


def test_two_corrections_of_one_claim_are_both_named(ledger: CavemanLedger) -> None:
    """Nothing in the ledger stops it, and rendering only one would hide a correction."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="The chart deploys agent-memory, version 0.3.0."))
    ledger.append(_entry("e-02", node_id=node.node_id, claim="The chart deploys it at 0.4.0.", supersedes="e-01"))
    ledger.append(_entry("e-03", node_id=node.node_id, claim="The chart deploys it at 0.4.1.", supersedes="e-01"))

    oldest = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)[2]
    assert oldest.endswith(f"{SUPERSEDED_FIELD}e-02, e-03")


def test_an_unsuperseded_claim_carries_no_supersession_field(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="The gateway fronts the JedAI models."))

    only = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)[0]
    assert only.count(FIELD_SEPARATOR) == 3
    assert SUPERSEDES_FIELD not in only
    assert SUPERSEDED_FIELD not in only


def test_another_nodes_claims_are_not_in_this_nodes_deep_read(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    gateway = _node(graph)
    chart = _node(
        graph,
        "chart",
        facts=(_fact("a Helm chart deploying memotron", FactKind.IS),),
        node_type="artifact",
    )
    ledger.append(_entry("e-01", node_id=gateway.node_id, claim="The gateway fronts the JedAI models."))
    ledger.append(_entry("e-02", node_id=chart.node_id, claim="The chart deploys agent-memory, #240."))

    rendered = explain(node_id=gateway.node_id, graph=graph, ledger=ledger, now=NOW)
    assert "The chart deploys agent-memory, #240." not in rendered
    assert rendered.splitlines()[0].endswith("1 entries")


# ================================================================= the history


def test_a_created_node_has_its_creation_in_its_history(ledger: CavemanLedger) -> None:
    """The first event a node has, and the one that makes the rest replayable."""
    graph, _ = _journalled(ledger)
    node = _node(graph)
    history = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
    assert history == ["2026-09-10 node_created: created 'gateway' as service"]


def test_history_renders_newest_first_over_the_journals_own_order(ledger: CavemanLedger) -> None:
    """A whole dream pass is stamped from one clock reading, so ``ts`` is not an order."""
    graph, _ = _journalled(ledger)
    node = _node(graph)
    graph.add_aliases(node.node_id, ["LiteLLM proxy"])
    graph.replace_facts(
        node.node_id,
        facts=(RULE, _fact("chat default is claude-sonnet-4-6")),
        type="service",
        embedding=EMBED.embed("gateway rewritten"),
        now=NOW,
    )

    history = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
    assert [line.split(": ", 1)[0].split(" ", 1)[1] for line in history] == [
        DreamOp.NODE_REWRITTEN.value,
        DreamOp.ALIASES_ADDED.value,
        DreamOp.NODE_CREATED.value,
    ]


def test_only_the_events_naming_this_node_are_in_its_history(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    gateway = _node(graph)
    chart = _node(graph, "chart", facts=(_fact("a Helm chart", FactKind.IS),), node_type="artifact")

    history = _section_of(explain(node_id=gateway.node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
    assert len(history) == 1
    assert chart.node_id not in "\n".join(history)


def test_an_edge_upsert_is_in_the_history_of_both_of_its_ends(ledger: CavemanLedger) -> None:
    """An edge is a belief about a pair, so it is history for both concepts."""
    graph, _ = _journalled(ledger)
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway)

    for node_id in (gateway.node_id, memory.node_id):
        history = _section_of(explain(node_id=node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
        assert any(DreamOp.RELATION_UPSERTED.value in line for line in history)


def test_a_reinforced_edge_says_reinforced_and_names_the_evidence_it_reached(
    ledger: CavemanLedger,
) -> None:
    """The history is where "this was restated" is legible as an event rather than a count."""
    graph, _ = _journalled(ledger)
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway, entries=("e-01",))
    _relate(graph, memory, gateway, entries=("e-09",), claim=None)  # type: ignore[arg-type]

    history = _section_of(explain(node_id=memory.node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
    assert history[0] == (
        f"2026-09-10 relation_upserted: reinforced BLOCKED {memory.node_id} to {gateway.node_id}, evidence 2"
    )
    assert "created BLOCKED" in history[1]


def test_a_rename_of_an_edge_type_is_in_no_single_nodes_history(ledger: CavemanLedger) -> None:
    """The gap the module docstring states rather than hides.

    The event names no nodes -- it re-labels however many relations carry a type
    -- so it is in ``replay`` and in ``events`` and in nobody's ``history:``.
    """
    graph, _ = _journalled(ledger)
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway)
    assert graph.rename_edge_type(scope=SCOPE, old="BLOCKED", new="REFUSED") == 1

    renamed = [event for event in ledger.events(scope=SCOPE) if event.op is DreamOp.EDGE_TYPE_RENAMED]
    assert len(renamed) == 1 and renamed[0].node_ids == ()
    for node_id in (gateway.node_id, memory.node_id):
        history = _section_of(explain(node_id=node_id, graph=graph, ledger=ledger, now=NOW), HISTORY_SECTION)
        assert not any(DreamOp.EDGE_TYPE_RENAMED.value in line for line in history)


# ========================================================== the event summaries


def _only(ledger: CavemanLedger, op: DreamOp) -> str:
    events = [event for event in ledger.events(scope=SCOPE) if event.op is op]
    assert len(events) == 1, f"expected exactly one {op.value} event, got {len(events)}"
    return event_summary(events[0])


def test_every_op_has_a_summary_and_none_of_them_is_blank(ledger: CavemanLedger) -> None:
    """Iterating the enum, so a new op cannot be added without a summary for it."""
    graph, _ = _journalled(ledger)
    gateway = _node(graph)
    memory = _node(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    doomed = _node(graph, "doomed", facts=(_fact("about to be absorbed", FactKind.IS),), node_type="artifact")
    parent = _node(graph, "parent", facts=(_fact("about to be split", FactKind.IS),), node_type="artifact")
    spare = _node(graph, "spare", facts=(_fact("about to be deleted", FactKind.IS),), node_type="artifact")

    relation = _relate(graph, memory, gateway)
    graph.add_aliases(gateway.node_id, ["LiteLLM proxy"])
    graph.replace_facts(gateway.node_id, facts=(RULE,), type="service", embedding=EMBED.embed("gateway again"), now=NOW)
    graph.replace_facts(gateway.node_id, facts=(RULE,), type="policy", embedding=EMBED.embed("gateway again"), now=NOW)
    graph.merge_nodes(
        survivor_id=memory.node_id,
        absorbed_ids=[doomed.node_id],
        name="agent-memory",
        type="artifact",
        facts=(_fact("an MCP server", FactKind.IS),),
        embedding=EMBED.embed("agent-memory merged"),
        now=NOW,
    )
    graph.split_node(
        node_id=parent.node_id,
        parts=[
            _split_spec("left", entry="e-01"),
            _split_spec("right", entry="e-02"),
        ],
        now=NOW,
    )
    graph.retire_relation(relation.relation_id)
    graph.rename_edge_type(scope=SCOPE, old="MISSING", new="ABSENT")
    graph.delete_nodes([spare.node_id])

    recorded = {event.op for event in ledger.events(scope=SCOPE)}
    assert recorded == set(DreamOp)
    for event in ledger.events(scope=SCOPE):
        summary = event_summary(event)
        assert summary and summary.strip() == summary


def test_a_merge_summary_names_what_was_absorbed_and_the_name_that_survived(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    survivor = _node(graph)
    doomed = _node(graph, "the LiteLLM proxy", facts=(_fact("same concept, other name", FactKind.IS),))
    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[doomed.node_id],
        name="gateway",
        type="service",
        facts=(RULE,),
        embedding=EMBED.embed("gateway merged"),
        now=NOW,
    )
    assert _only(ledger, DreamOp.NODE_MERGED) == f"absorbed {doomed.node_id} as 'gateway'"


def test_a_retype_summary_names_both_types_so_the_history_needs_no_diff(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    node = _node(graph)
    graph.replace_facts(node.node_id, facts=(RULE,), type="policy", embedding=EMBED.embed("gw"), now=NOW)
    assert _only(ledger, DreamOp.NODE_RETYPED) == "retyped service to policy"


def test_a_rewrite_summary_counts_the_facts_it_wrote(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    node = _node(graph)
    graph.replace_facts(
        node.node_id,
        facts=(RULE, _fact("chat default is claude-sonnet-4-6")),
        type="service",
        embedding=EMBED.embed("gw"),
        now=NOW,
    )
    assert _only(ledger, DreamOp.NODE_REWRITTEN) == "2 fact(s), type=service"


def test_an_aliases_summary_lists_the_names_the_node_now_answers_to(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    node = _node(graph)
    graph.add_aliases(node.node_id, ["LiteLLM proxy", "C4"])
    assert _only(ledger, DreamOp.ALIASES_ADDED) == "aliases now LiteLLM proxy, C4"


def test_a_summary_of_an_event_carrying_no_content_is_still_a_sentence(ledger: CavemanLedger) -> None:
    """A payload shape the summariser does not recognise is worth the op word, not a crash.

    Reached with a hand-built event rather than through the journal, because the
    journal cannot produce one -- which is exactly why the guard is asserted here
    rather than assumed.
    """
    from memotron.caveman.models import DreamEvent

    event = DreamEvent(
        event_id="ev-9999",
        ts=NOW,
        scope=SCOPE,
        op=DreamOp.NODE_REWRITTEN,
        node_ids=("n-001",),
        before={},
        after={},
        receipt_id="r-01",
    )
    assert event_summary(event) == "0 fact(s), type=None"


# ================================================================== the aliases


def test_the_last_line_lists_every_name_that_routes_to_this_node(ledger: CavemanLedger) -> None:
    """The names a search could have arrived by, which the node's facts do not carry."""
    graph = InMemoryGraph()
    node = _node(graph, "gateway", aliases=("LiteLLM proxy", "C4"))
    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert rendered.splitlines()[-1] == f"{ALIAS_PREFIX}LiteLLM proxy, C4"


def test_a_node_nothing_else_routes_to_says_so_rather_than_saying_nothing(ledger: CavemanLedger) -> None:
    """ "Findable by one name only" is information, so the line is always rendered."""
    graph = InMemoryGraph()
    node = _node(graph)
    rendered = explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW)
    assert rendered.splitlines()[-1] == NO_ALIASES == "aliases: none"


# ============================================================== the fail-fasts


def test_an_unknown_node_id_is_a_defect_not_an_empty_answer(ledger: CavemanLedger) -> None:
    graph = InMemoryGraph()
    with pytest.raises(NodeNotFound):
        explain(node_id="n-404", graph=graph, ledger=ledger, now=NOW)


def test_a_claim_dated_after_now_is_refused_rather_than_rendered(ledger: CavemanLedger) -> None:
    """A reader judges staleness from these dates, so one wrong date is not renderable."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="A claim stamped by a clock that ran ahead.", ts=NOW))
    with pytest.raises(ValueError, match="after now="):
        explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW - timedelta(seconds=1))


def test_a_claim_stamped_exactly_now_is_fine(ledger: CavemanLedger) -> None:
    """One clock reading stamps a whole ingest, and a read of it may follow immediately."""
    graph = InMemoryGraph()
    node = _node(graph)
    ledger.append(_entry("e-01", node_id=node.node_id, claim="A claim stamped at this very instant."))
    evidence = _section_of(explain(node_id=node.node_id, graph=graph, ledger=ledger, now=NOW), EVIDENCE_SECTION)
    assert evidence[0].split(FIELD_SEPARATOR)[1] == "2026-09-10"


# ================================================================= integration


def test_the_footer_of_a_read_names_ids_this_deep_read_resolves(ledger: CavemanLedger) -> None:
    """The two halves of amendment A's pointer-with-a-verb, joined.

    A read's footer is not a string a human retypes: every id in it must resolve
    here, or the verb points at nothing. ``ReadResult.node_ids`` is the same list
    as a field, which is what an agent actually acts on (#251 amendment D).
    """
    from memotron.caveman.motive import engineering_motive
    from memotron.caveman.read import read
    from memotron.caveman.render import FOOTER_PREFIX
    from memotron.caveman.render import footer as footer_line

    graph, _ = _journalled(ledger)
    gateway = _node(graph, "gateway", aliases=("LiteLLM proxy",))
    chart = _node(
        graph,
        "chart",
        facts=(_fact("a Helm chart deploying memotron", FactKind.IS),),
        node_type="artifact",
    )
    _relate(graph, chart, gateway, type="DEPLOYS", claim="the chart deploys it, #240")
    ledger.append(_entry("e-01", node_id=gateway.node_id, claim="The gateway fronts the JedAI models."))
    ledger.append(_entry("e-02", node_id=chart.node_id, claim="The chart deploys agent-memory, #240."))

    result = read(
        query="gateway chart",
        scope=SCOPE,
        motive=engineering_motive(),
        graph=graph,
        ledger=ledger,
        embedder=EMBED,
        receipts=InMemoryReceipts(),
        now=NOW,
    )
    footer = result.rendered.splitlines()[-1]
    assert footer == footer_line(result.node_ids)
    assert footer.startswith(FOOTER_PREFIX)

    for node_id in result.node_ids:
        deep = explain(node_id=node_id, graph=graph, ledger=ledger, now=NOW)
        assert deep.splitlines()[0].startswith(graph.get_node(node_id).name)
        assert deep.splitlines()[-1].startswith(ALIAS_PREFIX)
        assert _section_of(deep, HISTORY_SECTION) != [NOTHING]


def test_the_deep_read_is_strictly_more_than_the_bounded_block_it_explains(ledger: CavemanLedger) -> None:
    """The reason it exists: a budget cannot answer "on what evidence".

    Asserted against the read's own block rather than against a line count, so a
    read that grew wider does not make this pass by accident.
    """
    from memotron.caveman.motive import engineering_motive
    from memotron.caveman.read import read

    graph, _ = _journalled(ledger)
    gateway = _node(
        graph,
        facts=(RULE, _fact("chat default was claude-haiku-4-5", FactKind.SUPERSEDED)),
        aliases=("LiteLLM proxy",),
    )
    ledger.append(_entry("e-01", node_id=gateway.node_id, claim="The gateway fronts the JedAI models."))

    result = read(
        query="gateway",
        scope=SCOPE,
        motive=engineering_motive(),
        graph=graph,
        ledger=ledger,
        embedder=EMBED,
        receipts=InMemoryReceipts(),
        now=NOW,
    )
    block = result.rendered.split("\n\n")[0].splitlines()
    deep = explain(node_id=gateway.node_id, graph=graph, ledger=ledger, now=NOW).splitlines()
    assert len(deep) > len(block)
    assert not any("superseded" in line for line in block)
    assert any("superseded: chat default was claude-haiku-4-5" in line for line in deep)
