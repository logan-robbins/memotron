"""#251 amendment D: compression is provable, because the journal replays.

Every store here is the REAL one -- ``InMemoryGraph`` behind the REAL
``JournaledGraph``, over the REAL sqlite ``CavemanLedger``. That is the whole
point of the module: "the graph rebuilds from its recorded history" is worth
nothing asserted over a hand-built event list, because what is being proved is
that the events the journal ACTUALLY writes are sufficient.

There is deliberately **no transport of any kind in this module**, scripted or
otherwise. A replay makes no model call and no embedding call, and the way to
assert that is to give it nothing to call: if it ever grew one, these tests
would fail with a missing argument rather than pass quietly.

A replay applies each event through the store's own write seam, so the store
mints fresh ids and a replayed node does NOT carry the journalled node's id.
Every assertion here therefore matches records by content -- usually by name,
which these fixtures keep unique -- and :func:`_by_name` is the helper that does
it. An assertion keyed on ``node_id`` would be asserting a coincidence.

The load-bearing assertions:

* a graph built through the journal -- create, replace_facts, upsert_relation,
  merge, rename_edge_type -- **replays to an equal digest**;
* a graph mutated **behind the journal's back does not**, which is the only way
  the first assertion means anything, asserted six ways through the inner store;
* the digest covers content and ignores ids, vectors, clocks and bookkeeping,
  and changes with every field it does cover;
* a ``node_split`` of a node that held edges **replays exactly**, because the
  event records the post-split relation state and the partition is therefore
  read out of the journal rather than re-derived (see ``replay``'s module
  docstring); a history with a GAP in it is still refused, loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memotron.caveman.graph import InMemoryGraph, JournaledGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    DreamEvent,
    DreamOp,
    Fact,
    FactKind,
    FactSpec,
    Node,
    Relation,
    SplitSpec,
)
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.replay import (
    REPLAY_EMBEDDING,
    ReplayProof,
    content_digest,
    prove,
    replay,
)
from memotron.caveman.seams import Clock
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)
SCOPE = "repo:jedai/memotron"
OTHER_SCOPE = "repo:jedai/other"
EMBED = LocalEmbeddingTransport()


class _FixedClock:
    """A ``Clock`` that never moves, so an event's ``ts`` is never what is asserted."""

    def now(self) -> datetime:
        return NOW


@pytest.fixture
def ledger() -> CavemanLedger:
    store = CavemanLedger(":memory:")
    yield store
    store.close()


def _journalled(ledger: CavemanLedger) -> tuple[JournaledGraph, InMemoryGraph]:
    """The journal, and the inner store it wraps -- which is how a test goes behind it."""
    inner = InMemoryGraph()
    clock: Clock = _FixedClock()
    return JournaledGraph(inner, ledger=ledger, receipts=InMemoryReceipts(), clock=clock), inner


def _fact(text: str, kind: FactKind = FactKind.ATTRIBUTE, *, entries: tuple[str, ...] = ("e-01",)) -> Fact:
    return Fact(kind=kind, text=text, entry_ids=entries, first_seen=NOW, last_seen=NOW)


RULE = _fact("never hermetic, always through the JedAI gateway", FactKind.RULE)


def _payload(
    node_id: str,
    *,
    name: str = "gateway",
    type: str = "service",
    aliases: list[str] | None = None,
    facts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One journalled node payload, in ``graph.node_content``'s shape.

    For the handful of tests that write an event the journal cannot produce -- a
    history with a gap in it, a payload that is not a valid record -- which is
    exactly why those guards are asserted rather than assumed.
    """
    return {
        "node_id": node_id,
        "scope": SCOPE,
        "name": name,
        "aliases": aliases if aliases is not None else [],
        "type": type,
        "facts": facts if facts is not None else [],
    }


def _create(
    graph: JournaledGraph | InMemoryGraph,
    name: str,
    *,
    facts: tuple[Fact, ...] = (RULE,),
    node_type: str = "service",
    aliases: tuple[str, ...] = (),
    scope: str = SCOPE,
) -> Node:
    return graph.create_node(
        scope=scope,
        name=name,
        type=node_type,
        facts=facts,
        embedding=EMBED.embed(" ".join((name, *aliases, node_type, *(fact.text for fact in facts)))),
        now=NOW,
        aliases=aliases,
    )


def _relate(
    graph: JournaledGraph | InMemoryGraph,
    source: Node,
    target: Node,
    *,
    type: str = "BLOCKED",
    claim: str | None = "the Host header was refused pre-#245",
    entries: tuple[str, ...] = ("e-01",),
    until: str | None = None,
) -> Relation:
    return graph.upsert_relation(
        scope=source.scope,
        source_id=source.node_id,
        target_id=target.node_id,
        type=type,
        claim=claim,
        entry_ids=entries,
        until=until,
        now=NOW,
    )


def _split_spec(name: str, *, entry: str, relations: tuple[str, ...] = ()) -> SplitSpec:
    """One part of a split. ``facts`` is a :class:`FactSpec` tuple, not the deleted ``lines``.

    The store creates every part factless and dirty -- the dreamer fills it after
    the ledger re-key -- so this field is only ever read by ``dream``. It is here
    because ``SplitSpec`` requires it, and a split fixture that could omit it
    would be asserting against a shape the wire does not have.
    """
    return SplitSpec(
        name=name,
        type="service",
        facts=(FactSpec(kind=FactKind.ATTRIBUTE, text=f"part {name}", entry_ids=(entry,)),),
        entry_ids=(entry,),
        relation_ids=relations,
    )


def _names(graph: InMemoryGraph, *, scope: str = SCOPE) -> set[str]:
    """The names in *scope*. What a replay preserves, unlike the ids."""
    return {node.name for node in graph.list_nodes(scope=scope)}


def _by_name(graph: InMemoryGraph, name: str, *, scope: str = SCOPE) -> Node:
    """One node by name. A replay mints its own ids, so a name is the handle a test has."""
    found = [node for node in graph.list_nodes(scope=scope) if node.name == name]
    assert len(found) == 1, f"expected exactly one node named {name!r} in {scope}, found {len(found)}"
    return found[0]


def _edges(graph: InMemoryGraph, *, scope: str = SCOPE) -> set[tuple[str, str, str, str | None, tuple[str, ...]]]:
    """Every edge as ``(source name, type, target name, until, entry ids)``.

    The content identity of an edge, which is what a replay preserves and what
    ``content_digest`` compares.
    """
    names = {node.node_id: node.name for node in graph.list_nodes(scope=scope)}
    return {
        (names[relation.source_id], relation.type, names[relation.target_id], relation.until, relation.entry_ids)
        for relation in graph.relations(scope=scope)
    }


def _worked_scope(ledger: CavemanLedger) -> tuple[JournaledGraph, InMemoryGraph]:
    """The five mutations the package's plan names, applied in order through the journal.

    create, replace_facts, upsert_relation, merge, rename_edge_type -- every one
    of them through the journal, so the events under test are the ones a real
    ingest-and-dream writes rather than ones this fixture chose.
    """
    graph, inner = _journalled(ledger)
    gateway = _create(graph, "gateway", aliases=("the JedAI Gateway",))
    memory = _create(
        graph,
        "agent-memory",
        facts=(_fact("C4 returns 31 tools after #248"),),
        node_type="artifact",
    )
    proxy = _create(graph, "the LiteLLM proxy", facts=(_fact("same concept, other name", FactKind.IS),))
    chart = _create(graph, "chart", facts=(_fact("chart 0.4.1 in the C4 cluster"),), node_type="artifact")

    graph.replace_facts(
        gateway.node_id,
        facts=(RULE, _fact("chat default is claude-sonnet-4-6", entries=("e-01", "e-09"))),
        type="service",
        embedding=EMBED.embed("gateway dreamt"),
        now=NOW,
    )
    _relate(graph, memory, gateway, until="#245")
    _relate(graph, chart, memory, type="DEPLOYS", claim="the chart deploys it, #240")
    graph.merge_nodes(
        survivor_id=gateway.node_id,
        absorbed_ids=[proxy.node_id],
        name="gateway",
        type="service",
        facts=(RULE, _fact("a LiteLLM proxy fronting the JedAI models", FactKind.IS)),
        embedding=EMBED.embed("gateway merged"),
        now=NOW,
    )
    assert graph.rename_edge_type(scope=SCOPE, old="DEPLOYS", new="BLOCKED") == 1
    return graph, inner


# ============================================================== the round trip


def test_a_graph_built_through_the_journal_replays_to_an_equal_digest(ledger: CavemanLedger) -> None:
    """The proof the amendment asks for, over the five mutations the plan names."""
    graph, _ = _worked_scope(ledger)
    proof = prove(scope=SCOPE, graph=graph, ledger=ledger)
    assert proof.equal is True
    assert proof.live_digest == proof.replayed_digest
    assert proof.scope == SCOPE
    assert proof.event_count == len(ledger.events(scope=SCOPE))
    assert proof.event_count > 5


def test_the_replayed_graph_asserts_the_same_beliefs_about_the_same_concepts(ledger: CavemanLedger) -> None:
    """The digest is one number; this is what it is a number about."""
    _, inner = _worked_scope(ledger)
    replayed = replay(scope=SCOPE, ledger=ledger)

    assert _names(replayed) == _names(inner)
    for name in _names(inner):
        live, rebuilt = _by_name(inner, name), _by_name(replayed, name)
        assert rebuilt.type == live.type
        assert rebuilt.aliases == live.aliases
        assert rebuilt.facts == live.facts
    assert _edges(replayed) == _edges(inner)


def test_a_replay_mints_its_own_ids_and_the_digest_does_not_care(ledger: CavemanLedger) -> None:
    """The consequence of replaying through the write seam, asserted rather than assumed.

    The store mints ids, so the journal cannot hand them back. The proof is
    therefore about what the scope BELIEVES, and a replayed node carrying a
    different id is not a difference in belief. The node written behind the
    journal is what makes the replayed ids actually differ.
    """
    graph, inner = _journalled(ledger)
    _create(inner, "created-before-the-journal-was-watching")
    gateway = _create(graph, "gateway")

    replayed = replay(scope=SCOPE, ledger=ledger)
    rebuilt = _by_name(replayed, "gateway")
    assert rebuilt.node_id != gateway.node_id
    assert rebuilt.name == gateway.name
    assert rebuilt.facts == gateway.facts


def test_a_merge_replays_with_the_absorbed_node_gone_and_its_edges_repointed(ledger: CavemanLedger) -> None:
    """A merge is the mutation most likely to replay wrong, so it is asserted on its own.

    It replays through ``merge_nodes``, so the store's own rules re-point the
    edges and union the aliases -- this module does not re-derive either.
    """
    graph, _ = _journalled(ledger)
    survivor = _create(graph, "gateway", aliases=("the JedAI Gateway",))
    doomed = _create(graph, "the LiteLLM proxy", facts=(_fact("same concept", FactKind.IS),))
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, doomed)

    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[doomed.node_id],
        name="gateway",
        type="service",
        facts=(RULE,),
        embedding=EMBED.embed("gateway merged"),
        now=NOW,
    )

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _names(replayed) == {"gateway", "agent-memory"}
    assert _edges(replayed) == {("agent-memory", "BLOCKED", "gateway", None, ("e-01",))}
    assert "the LiteLLM proxy" in _by_name(replayed, "gateway").aliases
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_rename_that_folded_two_edges_into_one_replays_as_one(ledger: CavemanLedger) -> None:
    """A collision unions the evidence and destroys a relation, and the replay agrees."""
    graph, _ = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway, type="BLOCKED", entries=("e-01",))
    _relate(graph, memory, gateway, type="REFUSED", claim="refused the Host header", entries=("e-09",))
    assert len(graph.relations(scope=SCOPE)) == 2

    assert graph.rename_edge_type(scope=SCOPE, old="REFUSED", new="BLOCKED") == 1
    surviving = graph.relations(scope=SCOPE)
    assert len(surviving) == 1 and surviving[0].evidence == 2

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _edges(replayed) == {("agent-memory", "BLOCKED", "gateway", None, ("e-01", "e-09"))}
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_reinforced_edge_replays_with_its_whole_union_of_evidence(ledger: CavemanLedger) -> None:
    """Reinforcement is the amendment's evidence mechanism, so the proof must carry it."""
    graph, _ = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway, entries=("e-01",))
    reinforced = _relate(graph, memory, gateway, entries=("e-09",), claim=None)
    assert reinforced.evidence == 2

    assert _edges(replay(scope=SCOPE, ledger=ledger)) == _edges(graph)
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_retired_relation_is_gone_from_the_replay(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    relation = _relate(graph, memory, gateway)
    graph.retire_relation(relation.relation_id)

    assert replay(scope=SCOPE, ledger=ledger).relations(scope=SCOPE) == []
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_deleted_node_takes_its_edges_out_of_the_replay(ledger: CavemanLedger) -> None:
    """What erasure does: the node goes, and every belief that hung off it goes with it."""
    graph, _ = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway)
    graph.delete_nodes([memory.node_id])

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _names(replayed) == {"gateway"}
    assert replayed.relations(scope=SCOPE) == []
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_retype_and_an_alias_addition_both_replay(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    node = _create(graph, "gateway")
    graph.add_aliases(node.node_id, ["the LiteLLM proxy", "C4"])
    graph.replace_facts(node.node_id, facts=(RULE,), type="policy", embedding=EMBED.embed("gw"), now=NOW)

    rebuilt = _by_name(replay(scope=SCOPE, ledger=ledger), "gateway")
    assert rebuilt.type == "policy"
    assert rebuilt.aliases == ("the LiteLLM proxy", "C4")
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_an_empty_journal_replays_to_an_empty_graph_that_still_proves(ledger: CavemanLedger) -> None:
    """A scope nothing has written is a real state, not an error."""
    graph, _ = _journalled(ledger)
    proof = prove(scope=SCOPE, graph=graph, ledger=ledger)
    assert proof.equal is True
    assert proof.event_count == 0
    assert replay(scope=SCOPE, ledger=ledger).list_nodes(scope=SCOPE) == []


def test_only_the_named_scopes_events_are_replayed(ledger: CavemanLedger) -> None:
    """Two scopes in one store and one ledger, and a replay of either sees only its own."""
    graph, _ = _journalled(ledger)
    _create(graph, "gateway")
    _create(graph, "somebody-elses-node", scope=OTHER_SCOPE)

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _names(replayed) == {"gateway"}
    assert replayed.list_nodes(scope=OTHER_SCOPE) == []
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True
    assert prove(scope=OTHER_SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_replay_is_byte_identical_twice(ledger: CavemanLedger) -> None:
    """Two replays of one journal digest the same, or the proof proves nothing."""
    _worked_scope(ledger)
    first = content_digest(replay(scope=SCOPE, ledger=ledger), SCOPE)
    assert first == content_digest(replay(scope=SCOPE, ledger=ledger), SCOPE)


# ============================================ the negative, which is the point


def test_a_graph_mutated_behind_the_journals_back_does_not_replay_equal(ledger: CavemanLedger) -> None:
    """Without this, "the digests matched" could just mean the digest measures nothing."""
    graph, inner = _worked_scope(ledger)
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True

    before = len(ledger.events(scope=SCOPE))
    _create(inner, "smuggled-in-behind-the-journal")
    assert len(ledger.events(scope=SCOPE)) == before

    proof = prove(scope=SCOPE, graph=graph, ledger=ledger)
    assert proof.equal is False
    assert proof.live_digest != proof.replayed_digest
    assert "smuggled-in-behind-the-journal" not in _names(replay(scope=SCOPE, ledger=ledger))


def test_a_fact_rewritten_behind_the_journal_is_caught(ledger: CavemanLedger) -> None:
    """The subtler case: the same concepts, one different belief."""
    graph, inner = _worked_scope(ledger)
    node = _by_name(inner, "gateway")
    inner.replace_facts(
        node.node_id,
        facts=(_fact("a claim nobody journalled", FactKind.IS),),
        type=node.type,
        embedding=EMBED.embed("smuggled"),
        now=LATER,
    )
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is False


def test_an_alias_added_behind_the_journal_is_caught(ledger: CavemanLedger) -> None:
    """Aliases are search keys, so a smuggled one changes what the scope can be found by."""
    graph, inner = _worked_scope(ledger)
    inner.add_aliases(_by_name(inner, "gateway").node_id, ["a name nobody journalled"])
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is False


def test_an_edge_written_behind_the_journal_is_caught(ledger: CavemanLedger) -> None:
    graph, inner = _worked_scope(ledger)
    _relate(
        inner,
        _by_name(inner, "gateway"),
        _by_name(inner, "chart"),
        type="SMUGGLED",
        claim="an edge nobody journalled",
    )
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is False


def test_an_edge_retired_behind_the_journal_is_caught(ledger: CavemanLedger) -> None:
    """A belief removed without a record is exactly what a proof of history must catch."""
    graph, inner = _worked_scope(ledger)
    inner.retire_relation(inner.relations(scope=SCOPE)[0].relation_id)
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is False


def test_a_node_deleted_behind_the_journal_is_caught(ledger: CavemanLedger) -> None:
    graph, inner = _worked_scope(ledger)
    inner.delete_nodes([_by_name(inner, "chart").node_id])
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is False


# ================================================================== the digest


def test_the_digest_ignores_embeddings(ledger: CavemanLedger) -> None:
    """It has to: the journal records no vector, so a replay can reproduce none."""
    graph, inner = _journalled(ledger)
    node = _create(graph, "gateway")
    before = content_digest(graph, SCOPE)

    inner.replace_facts(
        node.node_id,
        facts=node.facts,
        type=node.type,
        embedding=EMBED.embed("an entirely different text"),
        now=LATER,
    )
    assert content_digest(graph, SCOPE) == before
    assert _by_name(replay(scope=SCOPE, ledger=ledger), "gateway").embedding == REPLAY_EMBEDDING


def test_the_digest_ignores_timestamps(ledger: CavemanLedger) -> None:
    """Otherwise the proof would be a proof about a clock rather than about content."""
    graph, inner = _journalled(ledger)
    node = _create(graph, "gateway")
    before = content_digest(graph, SCOPE)

    inner.replace_facts(node.node_id, facts=node.facts, type=node.type, embedding=node.embedding, now=LATER)
    assert inner.get_node(node.node_id).last_touched_at == LATER
    assert content_digest(graph, SCOPE) == before


def test_the_digest_ignores_read_counts_and_the_dirty_flag(ledger: CavemanLedger) -> None:
    """Bookkeeping about how a node was USED is not part of what it asserts."""
    graph, _ = _journalled(ledger)
    node = _create(graph, "gateway")
    before = content_digest(graph, SCOPE)
    graph.record_read([node.node_id])
    graph.mark_dirty([node.node_id])
    assert content_digest(graph, SCOPE) == before


def test_the_digest_changes_with_every_node_field_it_covers(ledger: CavemanLedger) -> None:
    """Type, facts and aliases, one at a time, through the public write seam."""
    graph, inner = _journalled(ledger)
    node = _create(graph, "gateway")
    baseline = content_digest(graph, SCOPE)

    inner.replace_facts(node.node_id, facts=node.facts, type="policy", embedding=node.embedding, now=NOW)
    assert content_digest(graph, SCOPE) != baseline, "the digest ignores a node's type"
    inner.replace_facts(node.node_id, facts=node.facts, type=node.type, embedding=node.embedding, now=NOW)
    assert content_digest(graph, SCOPE) == baseline

    inner.replace_facts(
        node.node_id,
        facts=(_fact("a different belief", FactKind.IS),),
        type=node.type,
        embedding=node.embedding,
        now=NOW,
    )
    assert content_digest(graph, SCOPE) != baseline, "the digest ignores a node's facts"
    inner.replace_facts(node.node_id, facts=node.facts, type=node.type, embedding=node.embedding, now=NOW)
    assert content_digest(graph, SCOPE) == baseline

    inner.add_aliases(node.node_id, ["another name"])
    assert content_digest(graph, SCOPE) != baseline, "the digest ignores a node's aliases"


def test_the_digest_changes_when_a_node_goes(ledger: CavemanLedger) -> None:
    """A name is content, and losing one is a change in what the scope holds."""
    graph, inner = _journalled(ledger)
    _create(graph, "gateway")
    doomed = _create(graph, "doomed", facts=(_fact("about to go", FactKind.IS),))
    baseline = content_digest(graph, SCOPE)

    inner.delete_nodes([doomed.node_id])
    assert content_digest(graph, SCOPE) != baseline
    assert _names(inner) == {"gateway"}


def test_the_digest_changes_with_every_relation_field_it_covers(ledger: CavemanLedger) -> None:
    """Claim, until, evidence and type, one at a time, through ``upsert_relation``."""
    graph, inner = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    original = _relate(graph, memory, gateway)
    baseline = content_digest(graph, SCOPE)

    for label, kwargs in (
        ("claim", {"claim": "a different claim"}),
        ("until", {"until": "#999"}),
        ("evidence", {"entries": ("e-01", "e-77")}),
        ("type", {"type": "REFUSED"}),
    ):
        _relate(inner, memory, gateway, **kwargs)  # type: ignore[arg-type]
        assert content_digest(graph, SCOPE) != baseline, f"the digest ignores a relation's {label}"
        for relation in inner.relations(scope=SCOPE):
            inner.retire_relation(relation.relation_id)
        _relate(
            inner,
            memory,
            gateway,
            type=original.type,
            claim=original.claim,
            entries=original.entry_ids,
            until=original.until,
        )
        assert content_digest(graph, SCOPE) == baseline


def test_the_digest_of_two_scopes_in_one_store_is_per_scope(ledger: CavemanLedger) -> None:
    graph, _ = _journalled(ledger)
    _create(graph, "gateway")
    _create(graph, "gateway", scope=OTHER_SCOPE, node_type="artifact")
    assert content_digest(graph, SCOPE) != content_digest(graph, OTHER_SCOPE)


def test_the_digest_does_not_depend_on_which_order_a_store_returns_records(ledger: CavemanLedger) -> None:
    """Both planes are ordered by their own content, so another store digests the same.

    Asserted with a store that deliberately reverses both listings -- the one
    thing a second ``GraphStore`` implementation is most likely to differ on.
    """

    class ReversingGraph(InMemoryGraph):
        def list_nodes(self, *, scope: str) -> list[Node]:
            return list(reversed(super().list_nodes(scope=scope)))

        def relations(self, *, scope: str) -> list[Relation]:
            return list(reversed(super().relations(scope=scope)))

    _, inner = _worked_scope(ledger)
    forwards = content_digest(inner, SCOPE)

    reversing = ReversingGraph()
    for node in inner.list_nodes(scope=SCOPE):
        _create(reversing, node.name, facts=node.facts, node_type=node.type, aliases=node.aliases)
    live_names = {node.node_id: node.name for node in inner.list_nodes(scope=SCOPE)}
    for relation in inner.relations(scope=SCOPE):
        _relate(
            reversing,
            _by_name(reversing, live_names[relation.source_id]),
            _by_name(reversing, live_names[relation.target_id]),
            type=relation.type,
            claim=relation.claim,
            entries=relation.entry_ids,
            until=relation.until,
        )
    assert content_digest(reversing, SCOPE) == forwards


def test_a_dangling_edge_is_refused_rather_than_digested(ledger: CavemanLedger) -> None:
    """A store whose edge names a node it does not hold is broken, and says so.

    Unreachable through the seam -- ``upsert_relation`` requires both endpoints
    and every deletion path drops the edges touching what it removed -- which is
    exactly why the guard is asserted rather than assumed.
    """

    class DanglingGraph(InMemoryGraph):
        def list_nodes(self, *, scope: str) -> list[Node]:
            return [node for node in super().list_nodes(scope=scope) if node.name != "gateway"]

    graph = DanglingGraph()
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    _relate(graph, memory, gateway)

    with pytest.raises(ValueError, match="holds a dangling edge"):
        content_digest(graph, SCOPE)


# ==================================================================== the split


def test_a_split_of_a_node_with_no_edges_replays(ledger: CavemanLedger) -> None:
    """The simple case: the parent goes, the parts arrive, nothing had edges."""
    graph, _ = _journalled(ledger)
    parent = _create(graph, "parent", facts=(_fact("about to be split", FactKind.IS),))
    graph.split_node(
        node_id=parent.node_id,
        parts=[
            _split_spec("left", entry="e-01"),
            _split_spec("right", entry="e-02"),
        ],
        now=NOW,
    )

    assert _names(replay(scope=SCOPE, ledger=ledger)) == {"left", "right"}
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_split_of_a_node_that_held_edges_replays_exactly(ledger: CavemanLedger) -> None:
    """The partition is read out of the event, never re-derived here.

    ``split_node`` hands each part the edges its ``relation_ids`` claimed and
    drops the rest, so the arguments do not determine the outcome and the event
    records the post-split relation state in ``after``. One of two edges is
    claimed here and the other is not, which is the only arrangement that can
    tell "the journal recorded the partition" apart from "the replay kept
    everything" and from "the replay dropped everything".
    """
    graph, _ = _journalled(ledger)
    parent = _create(graph, "parent", facts=(_fact("about to be split", FactKind.IS),))
    other = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    spare = _create(graph, "spare", facts=(_fact("a bystander", FactKind.IS),), node_type="artifact")
    kept = _relate(graph, other, parent, until="#245")
    _relate(graph, spare, parent, type="DEPLOYS", claim="deploys the parent", entries=("e-03",))
    graph.split_node(
        node_id=parent.node_id,
        parts=[
            _split_spec("left", entry="e-01", relations=(kept.relation_id,)),
            _split_spec("right", entry="e-02"),
        ],
        now=NOW,
    )
    # The claimed edge followed "left"; the unclaimed one died with the parent.
    assert _edges(graph) == {("agent-memory", "BLOCKED", "left", "#245", ("e-01",))}

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _names(replayed) == {"agent-memory", "spare", "left", "right"}
    assert _edges(replayed) == _edges(graph)
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_an_edge_retired_after_a_split_re_points_it_still_replays(ledger: CavemanLedger) -> None:
    """The split re-points an edge WITHOUT changing its id, and a later retire names that id.

    So the replay has to re-map the journalled relation id onto the record its
    restoring upsert minted. Nothing else exercises that: a retire of an edge no
    split touched resolves through the original upsert's mapping.
    """
    graph, _ = _journalled(ledger)
    parent = _create(graph, "parent", facts=(_fact("about to be split", FactKind.IS),))
    other = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    relation = _relate(graph, other, parent)
    graph.split_node(
        node_id=parent.node_id,
        parts=[
            _split_spec("left", entry="e-01", relations=(relation.relation_id,)),
            _split_spec("right", entry="e-02"),
        ],
        now=NOW,
    )
    graph.retire_relation(relation.relation_id)

    replayed = replay(scope=SCOPE, ledger=ledger)
    assert _edges(replayed) == set()
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


# ============================================================ a broken history


def test_a_journal_that_mutates_a_node_it_never_created_is_refused(ledger: CavemanLedger) -> None:
    """A history with a gap is not a history, so it is not silently half-replayed."""
    ledger.record_event(
        DreamEvent(
            event_id="ev-0001",
            ts=NOW,
            scope=SCOPE,
            op=DreamOp.NODE_RETYPED,
            node_ids=("n-404",),
            before={"node": _payload("n-404", type="service")},
            after={"node": _payload("n-404", type="policy")},
            receipt_id="r-01",
        )
    )
    with pytest.raises(ValueError, match="without ever having created it"):
        replay(scope=SCOPE, ledger=ledger)


def test_a_journal_that_retires_an_edge_it_never_created_is_refused(ledger: CavemanLedger) -> None:
    ledger.record_event(
        DreamEvent(
            event_id="ev-0001",
            ts=NOW,
            scope=SCOPE,
            op=DreamOp.RELATION_RETIRED,
            node_ids=("n-001", "n-002"),
            before={
                "relation": {
                    "relation_id": "r-404",
                    "scope": SCOPE,
                    "source_id": "n-001",
                    "target_id": "n-002",
                    "type": "BLOCKED",
                    "claim": "an edge no event created",
                    "entry_ids": ["e-01"],
                    "until": None,
                    "first_seen": NOW.isoformat(),
                    "last_seen": NOW.isoformat(),
                }
            },
            after={},
            receipt_id="r-01",
        )
    )
    with pytest.raises(ValueError, match="which no earlier event created"):
        replay(scope=SCOPE, ledger=ledger)


def test_a_journalled_payload_that_is_not_a_record_is_refused(ledger: CavemanLedger) -> None:
    """A journal that cannot be read back is a corrupted proof, not a partial graph.

    Written straight to the ledger, because ``JournaledGraph`` cannot produce
    one -- which is exactly why the guard is asserted rather than assumed.
    """
    ledger.record_event(
        DreamEvent(
            event_id="ev-0001",
            ts=NOW,
            scope=SCOPE,
            op=DreamOp.NODE_CREATED,
            node_ids=("n-001",),
            before={},
            after={
                "node": _payload(
                    "n-001",
                    facts=[
                        {
                            "kind": "rule",
                            "text": "a rule carrying a key it may not have",
                            "key": "nope",
                            "entry_ids": ["e-01"],
                            "first_seen": NOW.isoformat(),
                            "last_seen": NOW.isoformat(),
                        }
                    ],
                )
            },
            receipt_id="r-01",
        )
    )
    with pytest.raises(ValueError, match="only an 'attribute' fact has a label"):
        replay(scope=SCOPE, ledger=ledger)


# ========================================================== the returned store


def test_a_replayed_node_is_dirty_because_a_replay_creates_it(ledger: CavemanLedger) -> None:
    """Stated rather than surprising: the journal records no dirty flag.

    ``clear_dirty`` is bookkeeping and is deliberately not journalled, and a
    replay creates every node -- so a replayed graph reads as never dreamt. The
    digest ignores it, and a caller must not treat a replayed graph as a work
    queue.
    """
    graph, _ = _worked_scope(ledger)
    replayed = replay(scope=SCOPE, ledger=ledger)
    for node in replayed.list_nodes(scope=SCOPE):
        assert node.dirty is True
        assert node.read_count == 0
        assert node.ledger_key == node.node_id
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


def test_a_replayed_graph_renders_the_same_beliefs_as_the_live_one(ledger: CavemanLedger) -> None:
    """The point of rebuilding content rather than a digest: it is a readable graph.

    The rendered ids differ -- a replay mints its own -- so what is compared is
    every belief the block states, with the header asserted separately.
    """
    from memotron.caveman.render import render_fact, render_node

    _, inner = _worked_scope(ledger)
    replayed = replay(scope=SCOPE, ledger=ledger)
    names = {node.node_id: node.name for node in replayed.list_nodes(scope=SCOPE)}
    for live in inner.list_nodes(scope=SCOPE):
        rebuilt = _by_name(replayed, live.name)
        block = render_node(rebuilt, replayed.relations_of(rebuilt.node_id), entries=3, names=names)
        assert block.startswith(f"{live.name} ({live.type}) [{rebuilt.node_id}]")
        assert block.isascii()
        for fact in live.facts:
            assert render_fact(fact) in block.splitlines()


def test_every_dream_op_is_exercised_and_the_scope_still_proves(ledger: CavemanLedger) -> None:
    """Iterating the enum, so a new op cannot arrive without this module deciding.

    The split here is of an edgeless node, which is the replayable case; the
    refusal for the other one is asserted in its own test above.
    """
    graph, _ = _journalled(ledger)
    gateway = _create(graph, "gateway")
    memory = _create(graph, "agent-memory", facts=(_fact("an MCP server", FactKind.IS),), node_type="artifact")
    doomed = _create(graph, "doomed", facts=(_fact("about to be absorbed", FactKind.IS),))
    spare = _create(graph, "spare", facts=(_fact("about to be deleted", FactKind.IS),))
    parent = _create(graph, "parent", facts=(_fact("about to be split", FactKind.IS),))

    relation = _relate(graph, memory, gateway)
    graph.add_aliases(gateway.node_id, ["the LiteLLM proxy"])
    graph.replace_facts(gateway.node_id, facts=(RULE,), type="service", embedding=EMBED.embed("gw"), now=NOW)
    graph.replace_facts(gateway.node_id, facts=(RULE,), type="policy", embedding=EMBED.embed("gw"), now=NOW)
    graph.merge_nodes(
        survivor_id=memory.node_id,
        absorbed_ids=[doomed.node_id],
        name="agent-memory",
        type="artifact",
        facts=(_fact("an MCP server", FactKind.IS),),
        embedding=EMBED.embed("merged"),
        now=NOW,
    )
    graph.retire_relation(relation.relation_id)
    graph.rename_edge_type(scope=SCOPE, old="MISSING", new="ABSENT")
    graph.delete_nodes([spare.node_id])
    graph.split_node(
        node_id=parent.node_id,
        parts=[
            _split_spec("left", entry="e-01"),
            _split_spec("right", entry="e-02"),
        ],
        now=NOW,
    )

    assert {event.op for event in ledger.events(scope=SCOPE)} == set(DreamOp)
    assert prove(scope=SCOPE, graph=graph, ledger=ledger).equal is True


# =========================================================== the record itself


def test_the_proof_carries_both_digests_so_a_caller_can_check_it(ledger: CavemanLedger) -> None:
    """A proof a caller cannot verify is an assertion."""
    graph, _ = _worked_scope(ledger)
    proof = prove(scope=SCOPE, graph=graph, ledger=ledger)
    assert isinstance(proof, ReplayProof)
    assert proof.live_digest == content_digest(graph, SCOPE)
    assert proof.replayed_digest == content_digest(replay(scope=SCOPE, ledger=ledger), SCOPE)
    assert len(proof.live_digest) == 64
    assert proof.equal == (proof.live_digest == proof.replayed_digest)
