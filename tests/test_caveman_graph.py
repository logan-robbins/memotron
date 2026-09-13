"""#251 units 6, 9, 10, 11: the in-memory bounded node graph.

``InMemoryGraph`` is the ONE ``GraphStore`` implementation, so these tests are
also the conformance proof for unit 6's Protocol — by comparing
``inspect.signature`` across ``GraphStore.__protocol_attrs__``, not by
``isinstance``. An ``isinstance`` check against a Protocol verifies only that
the names exist and says nothing about their signatures, which is the half that
actually breaks when a work package renames a keyword.

Embeddings come from the real hermetic ``LocalEmbeddingTransport`` (256-dim,
L2-normalised, zero network calls) rather than from hand-written vectors. Two
reasons: hand-written vectors that happen not to be unit-norm would fail the
store's own guard for the wrong reason, and the exact-text query returning its
own node first is only a meaningful assertion against a real embedder.

The load-bearing assertion in unit 11 moved with #251 amendment D, and the move
is the whole change: it used to be ``degree(survivor) == 0`` immediately after a
merge, because an edge was a derivable weight and the caller rebuilt it from
``ledger.cooccurrence``. An edge now carries its own claim, which the ledger
cannot re-derive, so the assertion is the positive one -- **a merge re-points the
relations it absorbs**, drops the self-loops that creates, and unions what
collides.

The last section covers ``JournaledGraph``: the wrapper that records one
``DreamEvent`` per mutation. It is tested against the same ``InMemoryGraph``
rather than against a fake store, so "every mutation is journalled" is checked
over the real write paths.
"""

from __future__ import annotations

import inspect
import math
from datetime import UTC, datetime, timedelta

import pytest

from memotron.caveman.errors import BudgetExceeded, NodeNotFound
from memotron.caveman.graph import (
    EVENT_ID_PREFIX,
    InMemoryGraph,
    JournaledGraph,
    node_content,
    relation_content,
)
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import DreamOp, Fact, FactKind, FactSpec, Node, ReceiptOp, Relation, SplitSpec
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.seams import Clock, GraphStore
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)
SCOPE = "repo:jedai/memotron"
OTHER_SCOPE = "repo:jedai/other"

EMBED = LocalEmbeddingTransport()


def _vec(text: str) -> tuple[float, ...]:
    return tuple(EMBED.embed(text))


def _graph() -> InMemoryGraph:
    return InMemoryGraph()


def _fact(text: str = "chat default is claude-haiku-4-5", *, kind: FactKind = FactKind.ATTRIBUTE) -> Fact:
    return Fact(kind=kind, text=text, entry_ids=("e-01",), first_seen=NOW, last_seen=NOW)


def _relate(
    graph: GraphStore,
    source: Node,
    target: Node,
    *,
    type: str = "MENTIONED_WITH",
    claim: str = "one claim named both of these",
    entries: tuple[str, ...] = ("e-01",),
    until: str | None = None,
    now: datetime = NOW,
) -> Relation:
    """The one way a test writes an edge, so no test can key a pair by hand."""
    return graph.upsert_relation(
        scope=source.scope,
        source_id=source.node_id,
        target_id=target.node_id,
        type=type,
        claim=claim,
        entry_ids=entries,
        until=until,
        now=now,
    )


def _create(
    graph: InMemoryGraph,
    name: str,
    *,
    scope: str = SCOPE,
    node_type: str = "service",
    facts: tuple[Fact, ...] = (),
    text: str | None = None,
    now: datetime = NOW,
) -> Node:
    return graph.create_node(
        scope=scope,
        name=name,
        type=node_type,
        facts=facts,
        embedding=_vec(text if text is not None else name),
        now=now,
    )


def _split_spec(name: str, *, node_type: str = "artifact", relations: tuple[str, ...] = ()) -> SplitSpec:
    """One part of a split, carrying the one fact the store never reads.

    ``facts`` is a :class:`FactSpec` tuple since #251 amendment D package D-C:
    the store creates every part factless and the dreamer fills it after the
    ledger re-key, so what this field holds is only ever read by ``dream``.
    """
    return SplitSpec(
        name=name,
        type=node_type,
        facts=(FactSpec(kind=FactKind.ATTRIBUTE, text=f"part {name}", entry_ids=(f"e-{name}",)),),
        entry_ids=(f"e-{name}",),
        relation_ids=relations,
    )


# ================================================== unit 6: Protocol conformance


def test_the_graph_satisfies_every_graphstore_member_by_signature() -> None:
    """Names AND signatures. A renamed keyword is the failure this catches."""
    missing: list[str] = []
    mismatched: list[str] = []
    for attribute in sorted(GraphStore.__protocol_attrs__):
        implementation = getattr(InMemoryGraph, attribute, None)
        if implementation is None:
            missing.append(attribute)
            continue
        expected = inspect.signature(getattr(GraphStore, attribute))
        actual = inspect.signature(implementation)
        if expected != actual:
            mismatched.append(f"{attribute}: protocol {expected} != impl {actual}")
    assert not missing, f"InMemoryGraph is missing: {missing}"
    assert not mismatched, "signature drift:\n" + "\n".join(mismatched)


def test_the_protocol_declares_the_twenty_six_designed_operations() -> None:
    """The control: a conformance test against an empty Protocol passes trivially.

    Twenty as designed; plus ``node_by_alias`` and ``add_aliases`` from #251
    amendment A -- the exact-match half of a search, which embedding similarity
    cannot do; then amendment D removed three (``set_link_weights``, ``links``,
    ``degree``, all about a derivable weight) and added seven for typed edges
    (``upsert_relation``, ``retire_relation``, ``get_relation``, ``relations``,
    ``relations_of``, ``edge_types``, ``rename_edge_type``).
    """
    assert len(GraphStore.__protocol_attrs__) == 26


def test_the_deleted_weight_operations_are_gone_from_the_seam() -> None:
    """Not deprecated -- gone. A node's degree is ``len(relations_of(id))``, and two
    ways to count one thing is two numbers that can disagree."""
    assert {"set_link_weights", "links", "degree"}.isdisjoint(GraphStore.__protocol_attrs__)
    for deleted in ("set_link_weights", "links", "degree"):
        assert not hasattr(InMemoryGraph, deleted)


def test_the_graph_is_typed_as_the_protocol() -> None:
    """Callers depend on the seam. mypy checks this statically; this pins it at runtime."""
    store: GraphStore = _graph()
    assert store.count(scope=SCOPE) == 0


# =============================================== unit 9: CRUD and the dirty set


def test_a_created_node_round_trips_through_get_node() -> None:
    graph = _graph()
    created = _create(graph, "gateway")
    assert graph.get_node(created.node_id) == created


def test_a_created_node_is_dirty_and_factless_and_undreamed() -> None:
    """Reconcile may route and mark dirty; the first line set is the dreamer's."""
    node = _create(_graph(), "gateway")
    assert node.dirty is True
    assert node.facts == ()
    assert node.dreamed_at is None
    assert node.read_count == 0


def test_the_store_mints_the_ledger_key_as_the_id_it_just_assigned() -> None:
    """``create_node`` takes no ``ledger_key``: the caller cannot know the id.

    The header ``render.render_header`` renders carries the node id, and the
    only thing a reader can do with that key is hand it back to
    ``LedgerStore.for_node``. A caller-supplied key therefore rendered a pointer
    that resolves to nothing — which is what this asserts is no longer possible.
    ``split_node`` already keyed its parts this way; this is the same rule on
    the other creation path.
    """
    node = _create(_graph(), "gateway")
    assert node.ledger_key == node.node_id


def test_split_parts_key_themselves_by_their_own_minted_ids() -> None:
    """The same rule on the other creation path, so the two cannot diverge."""
    graph = _graph()
    parent = _create(graph, "mixed")
    parts = graph.split_node(
        node_id=parent.node_id,
        parts=(_split_spec("chart"), _split_spec("c4", node_type="cluster")),
        now=NOW,
    )
    assert [part.ledger_key for part in parts] == [part.node_id for part in parts]


def test_node_ids_are_minted_uniquely_and_padded() -> None:
    graph = _graph()
    first = _create(graph, "a")
    second = _create(graph, "b")
    assert first.node_id == "n-001"
    assert second.node_id == "n-002"


def test_node_ids_are_unique_across_scopes_in_one_store() -> None:
    """`get_node` takes no scope, so ids must be store-wide."""
    graph = _graph()
    first = _create(graph, "a", scope=SCOPE)
    second = _create(graph, "a", scope=OTHER_SCOPE)
    assert first.node_id != second.node_id


def test_get_node_of_an_unknown_id_raises() -> None:
    """Stale state is a defect, not an empty result."""
    with pytest.raises(NodeNotFound, match="no such node: n-999"):
        _graph().get_node("n-999")


def test_get_nodes_preserves_the_order_asked_for_and_skips_unknowns() -> None:
    graph = _graph()
    first = _create(graph, "zebra")
    second = _create(graph, "aardvark")
    found = graph.get_nodes([second.node_id, "n-999", first.node_id])
    assert [node.name for node in found] == ["aardvark", "zebra"]


def test_count_is_per_scope() -> None:
    graph = _graph()
    _create(graph, "a", scope=SCOPE)
    _create(graph, "b", scope=SCOPE)
    _create(graph, "c", scope=OTHER_SCOPE)
    assert graph.count(scope=SCOPE) == 2
    assert graph.count(scope=OTHER_SCOPE) == 1
    assert graph.count(scope="nothing-here") == 0


def test_replace_facts_bumps_last_touched_and_preserves_created_at() -> None:
    graph = _graph()
    node = _create(graph, "gateway", now=NOW)
    updated = graph.replace_facts(
        node.node_id,
        facts=(_fact("never hermetic", kind=FactKind.RULE),),
        type="policy",
        embedding=_vec("gateway policy never hermetic"),
        now=LATER,
    )
    assert updated.created_at == NOW
    assert updated.last_touched_at == LATER
    assert updated.facts == (_fact("never hermetic", kind=FactKind.RULE),)
    assert updated.type == "policy"


def test_replace_facts_does_not_clear_the_dirty_flag() -> None:
    """`clear_dirty` stamps the watermark separately.

    A dream that wrote facts and then failed its own validation must not look as
    though it completed.
    """
    graph = _graph()
    node = _create(graph, "gateway")
    updated = graph.replace_facts(node.node_id, facts=(": x",), type="service", embedding=_vec("x"), now=LATER)
    assert updated.dirty is True


def test_replace_facts_on_an_unknown_node_raises() -> None:
    with pytest.raises(NodeNotFound):
        _graph().replace_facts("n-999", facts=(_fact("x"),), type="service", embedding=_vec("x"), now=NOW)


def test_dirty_lists_only_dirty_nodes_of_the_scope() -> None:
    graph = _graph()
    first = _create(graph, "aaa")
    _create(graph, "bbb")
    _create(graph, "ccc", scope=OTHER_SCOPE)
    graph.clear_dirty([first.node_id], now=NOW)
    assert [node.name for node in graph.dirty(scope=SCOPE)] == ["bbb"]


def test_clear_dirty_stamps_dreamed_at() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    graph.clear_dirty([node.node_id], now=LATER)
    reread = graph.get_node(node.node_id)
    assert reread.dirty is False
    assert reread.dreamed_at == LATER


def test_mark_dirty_makes_a_clean_node_dirty_again() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    graph.clear_dirty([node.node_id], now=NOW)
    graph.mark_dirty([node.node_id])
    assert graph.get_node(node.node_id).dirty is True


def test_mark_dirty_does_not_reset_the_dreamed_at_watermark() -> None:
    """`for_node(since=dreamed_at)` must still say what arrived after the last dream."""
    graph = _graph()
    node = _create(graph, "gateway")
    graph.clear_dirty([node.node_id], now=NOW)
    graph.mark_dirty([node.node_id])
    assert graph.get_node(node.node_id).dreamed_at == NOW


@pytest.mark.parametrize("operation", ["mark_dirty", "clear_dirty", "record_read", "delete_nodes"])
def test_the_forgiving_operations_ignore_an_unknown_id(operation: str) -> None:
    """Erasure works from ledger node ids, some of which an earlier merge removed.

    Asking the caller to pre-filter would put the same existence check in two
    places, so these four absorb it. `get_node` and `replace_facts` still raise.
    """
    graph = _graph()
    if operation == "clear_dirty":
        graph.clear_dirty(["n-999"], now=NOW)
    else:
        getattr(graph, operation)(["n-999"])
    assert graph.count(scope=SCOPE) == 0


def test_record_read_increments_and_never_touches_facts() -> None:
    graph = _graph()
    node = _create(graph, "gateway", facts=(_fact("x"),))
    graph.record_read([node.node_id])
    graph.record_read([node.node_id])
    reread = graph.get_node(node.node_id)
    assert reread.read_count == 2
    assert reread.facts == (_fact("x"),)
    assert reread.dirty is node.dirty


def test_list_nodes_is_name_ordered_and_stable() -> None:
    graph = _graph()
    for name in ("zebra", "aardvark", "monkey"):
        _create(graph, name)
    first = [node.name for node in graph.list_nodes(scope=SCOPE)]
    assert first == ["aardvark", "monkey", "zebra"]
    assert [node.name for node in graph.list_nodes(scope=SCOPE)] == first


def test_list_nodes_breaks_a_name_tie_on_node_id() -> None:
    graph = _graph()
    first = _create(graph, "same")
    second = _create(graph, "same")
    assert [node.node_id for node in graph.list_nodes(scope=SCOPE)] == [first.node_id, second.node_id]


def test_node_by_name_finds_an_exact_match_in_its_own_scope() -> None:
    graph = _graph()
    node = _create(graph, "gateway", scope=SCOPE)
    _create(graph, "gateway", scope=OTHER_SCOPE)
    assert graph.node_by_name(scope=SCOPE, name="gateway") == node


def test_node_by_name_is_exact_not_fuzzy() -> None:
    """A `→` target either names a node or it does not; near-misses are rejections."""
    graph = _graph()
    _create(graph, "gateway")
    assert graph.node_by_name(scope=SCOPE, name="Gateway") is None
    assert graph.node_by_name(scope=SCOPE, name="gatewa") is None
    assert graph.node_by_name(scope=SCOPE, name="gateway ") is None


def test_node_by_name_resolves_a_collision_deterministically() -> None:
    """Name uniqueness is not an invariant here -- `merge_nodes` can collide.

    So the resolution is pinned rather than refused: lowest node id wins, and a
    `→` target always resolves to the same node.
    """
    graph = _graph()
    first = _create(graph, "same")
    _create(graph, "same")
    assert graph.node_by_name(scope=SCOPE, name="same") == first


# ------------------------------------------------------------------- aliases


def test_a_created_node_has_no_aliases_unless_it_is_given_some() -> None:
    assert _create(_graph(), "gateway").aliases == ()


def test_create_node_keeps_the_other_surface_names_of_its_group() -> None:
    """Reconcile groups by name; every name in the group but the chosen one is an alias."""
    graph = _graph()
    node = graph.create_node(
        scope=SCOPE,
        name="gateway",
        type="service",
        facts=(),
        embedding=_vec("gateway"),
        now=NOW,
        aliases=["the JedAI Gateway", "LiteLLM proxy"],
    )
    assert node.aliases == ("the JedAI Gateway", "LiteLLM proxy")


def test_create_node_normalises_the_aliases_it_is_handed() -> None:
    """A caller may pass the whole group -- chosen name and all -- and get the canonical set."""
    graph = _graph()
    node = graph.create_node(
        scope=SCOPE,
        name="gateway",
        type="service",
        facts=(),
        embedding=_vec("gateway"),
        now=NOW,
        aliases=["gateway", "C4", "c4"],
    )
    assert node.aliases == ("C4",)


def test_add_aliases_unions_and_returns_the_node() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    updated = graph.add_aliases(node.node_id, ["C4"])
    assert updated.aliases == ("C4",)
    assert graph.add_aliases(node.node_id, ["LiteLLM proxy"]).aliases == ("C4", "LiteLLM proxy")
    assert graph.get_node(node.node_id).aliases == ("C4", "LiteLLM proxy")


def test_add_aliases_is_idempotent_including_across_case() -> None:
    """Reconcile re-binds a name it has seen before on every ingest that mentions it."""
    graph = _graph()
    node = _create(graph, "gateway")
    graph.add_aliases(node.node_id, ["C4"])
    assert graph.add_aliases(node.node_id, ["C4", "c4", "gateway"]).aliases == ("C4",)


def test_add_aliases_does_not_move_the_last_touched_date() -> None:
    """An alias is a routing key, and the header's ``as of`` date reports content."""
    graph = _graph()
    node = _create(graph, "gateway")
    assert graph.add_aliases(node.node_id, ["C4"]).last_touched_at == node.last_touched_at


def test_add_aliases_refuses_an_unknown_node() -> None:
    with pytest.raises(NodeNotFound):
        _graph().add_aliases("n-404", ["C4"])


def test_add_aliases_refuses_a_blank_name() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    with pytest.raises(ValueError, match="cannot be blank"):
        graph.add_aliases(node.node_id, [" "])


def test_node_by_alias_finds_a_node_by_an_alias() -> None:
    """The symptom this exists for: ``#245`` and ``#246`` embed nearly identically."""
    graph = _graph()
    node = _create(graph, "c4")
    graph.add_aliases(node.node_id, ["C4 memory server"])
    assert graph.node_by_alias(scope=SCOPE, name="C4 memory server") == graph.get_node(node.node_id)


def test_node_by_alias_finds_a_node_by_its_own_name() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    assert graph.node_by_alias(scope=SCOPE, name="gateway") == node


def test_node_by_alias_is_case_insensitive_on_both_the_name_and_the_aliases() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    graph.add_aliases(node.node_id, ["C4"])
    assert graph.node_by_alias(scope=SCOPE, name="GATEWAY") is not None
    assert graph.node_by_alias(scope=SCOPE, name="c4") is not None


def test_node_by_alias_is_still_exact_apart_from_case() -> None:
    graph = _graph()
    _create(graph, "gateway")
    assert graph.node_by_alias(scope=SCOPE, name="gatewa") is None
    assert graph.node_by_alias(scope=SCOPE, name="gateway ") is None
    assert graph.node_by_alias(scope=SCOPE, name="the gateway") is None


def test_node_by_alias_stays_inside_its_scope() -> None:
    graph = _graph()
    _create(graph, "gateway", scope=OTHER_SCOPE)
    assert graph.node_by_alias(scope=SCOPE, name="gateway") is None


def test_node_by_alias_resolves_a_collision_deterministically() -> None:
    """Two nodes can hold the same surface name; a seed that varied would vary the read."""
    graph = _graph()
    first = _create(graph, "gateway")
    second = _create(graph, "proxy")
    graph.add_aliases(second.node_id, ["Gateway"])
    assert graph.node_by_alias(scope=SCOPE, name="gateway") == graph.get_node(first.node_id)


def test_merge_unions_the_absorbed_names_and_aliases_into_the_survivor() -> None:
    """A merge is where a searchable name would otherwise be destroyed."""
    graph = _graph()
    survivor = _create(graph, "gateway")
    graph.add_aliases(survivor.node_id, ["LiteLLM proxy"])
    absorbed = _create(graph, "litellm-gw")
    graph.add_aliases(absorbed.node_id, ["the proxy"])

    merged = graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    assert merged.aliases == ("LiteLLM proxy", "litellm-gw", "the proxy")
    assert graph.node_by_alias(scope=SCOPE, name="litellm-gw") == merged


def test_a_merge_that_takes_an_absorbed_nodes_name_keeps_no_alias_for_it() -> None:
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "c4")
    merged = graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="c4",
        type="cluster",
        facts=(_fact("x"),),
        embedding=_vec("c4"),
        now=LATER,
    )
    assert merged.aliases == ()


def test_split_parts_start_with_no_aliases() -> None:
    """``SplitSpec`` carries none, and the parts are two new concepts."""
    graph = _graph()
    parent = _create(graph, "chart-models")
    graph.add_aliases(parent.node_id, ["the chart"])
    parts = graph.split_node(node_id=parent.node_id, parts=[_split_spec("chart"), _split_spec("models")], now=LATER)
    assert [part.aliases for part in parts] == [(), ()]


def test_type_vocabulary_orders_by_frequency_then_name() -> None:
    graph = _graph()
    for node_type in ("service", "service", "service", "policy", "policy", "artifact", "cluster"):
        _create(graph, f"n-{node_type}-{graph.count(scope=SCOPE)}", node_type=node_type)
    assert graph.type_vocabulary(scope=SCOPE) == ["service", "policy", "artifact", "cluster"]


def test_type_vocabulary_is_stable_across_calls() -> None:
    """`Counter.most_common` is insertion-ordered for ties, which follows dict order.

    Without the alphabetical tie-break the vocabulary offered to the model would
    reorder between runs and change the prompt without changing the policy.
    """
    graph = _graph()
    for name, node_type in (("a", "zeta"), ("b", "alpha"), ("c", "mu")):
        _create(graph, name, node_type=node_type)
    assert graph.type_vocabulary(scope=SCOPE) == ["alpha", "mu", "zeta"]


def test_type_vocabulary_is_per_scope() -> None:
    graph = _graph()
    _create(graph, "a", node_type="service", scope=SCOPE)
    _create(graph, "b", node_type="cluster", scope=OTHER_SCOPE)
    assert graph.type_vocabulary(scope=SCOPE) == ["service"]


# -- the unit-norm guard -------------------------------------------------------


@pytest.mark.parametrize("norm", [0.5, 2.0, 0.0])
def test_an_unnormalised_embedding_is_refused_on_create(norm: float) -> None:
    """`cosine_similarity` is a bare dot product; an unnormalised vector mis-ranks silently."""
    graph = _graph()
    scaled = tuple(value * norm for value in _vec("gateway"))
    with pytest.raises(BudgetExceeded, match="must be unit-norm"):
        graph.create_node(scope=SCOPE, name="gateway", type="service", facts=(), embedding=scaled, now=NOW)


def test_an_unnormalised_embedding_is_refused_on_replace_facts() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    halved = tuple(value * 0.5 for value in _vec("gateway"))
    with pytest.raises(BudgetExceeded, match="must be unit-norm"):
        graph.replace_facts(node.node_id, facts=(": x",), type="service", embedding=halved, now=LATER)


def test_a_refused_embedding_leaves_the_node_untouched() -> None:
    graph = _graph()
    node = _create(graph, "gateway", facts=(_fact("original"),))
    with pytest.raises(BudgetExceeded):
        graph.replace_facts(node.node_id, facts=(": new",), type="policy", embedding=(0.5, 0.5), now=LATER)
    assert graph.get_node(node.node_id).facts == (_fact("original"),)


def test_an_empty_embedding_is_refused_on_create() -> None:
    """Norm 0. A node that cannot be found by kNN is not a node anyone can read."""
    graph = _graph()
    with pytest.raises(BudgetExceeded, match="must be unit-norm"):
        graph.create_node(scope=SCOPE, name="g", type="service", facts=(), embedding=(), now=NOW)


def test_a_real_local_embedding_passes_the_guard() -> None:
    """The control. A guard nothing can satisfy is worse than no guard."""
    node = _create(_graph(), "gateway")
    assert abs(math.sqrt(sum(value * value for value in node.embedding)) - 1.0) < 1e-9


# -- unit 9's integration case -------------------------------------------------


def test_five_hundred_nodes_count_and_list_stably() -> None:
    """N=500 is the shipped bound, so this is the store at full occupancy."""
    graph = _graph()
    for index in range(500):
        _create(graph, f"node-{index:03d}")
    assert graph.count(scope=SCOPE) == 500
    listed = graph.list_nodes(scope=SCOPE)
    assert len(listed) == 500
    assert [node.name for node in listed] == sorted(node.name for node in listed)
    assert [node.node_id for node in graph.list_nodes(scope=SCOPE)] == [node.node_id for node in listed]


# ================================================ unit 10: kNN and 1-hop


_FIXTURE_TEXTS = (
    "the JedAI Gateway is a LiteLLM proxy",
    "the agent-memory MCP server",
    "the Helm chart that deploys agent-memory",
    "the C4 cluster",
    "session affinity loss on a cold pod",
    "probe #246 names the intermittent failure",
    "chat default claude-haiku-4-5",
    "embedding text-embedding-3 at 3072 dimensions",
    "issue #245 fixed the Host rewrite",
    "issue #240 added the agent-memory deploy",
    "issue #248 raised the tool count to 31",
    "the receipt ledger hash chain",
)


def _twelve_node_fixture() -> tuple[InMemoryGraph, list[Node]]:
    graph = _graph()
    nodes = [_create(graph, f"node-{index:02d}", text=text) for index, text in enumerate(_FIXTURE_TEXTS)]
    return graph, nodes


def test_knn_returns_the_exact_text_match_first() -> None:
    """The only assertion that shows the vector signal is real rather than plumbed."""
    graph, nodes = _twelve_node_fixture()
    target = nodes[4]
    ranked = graph.knn(scope=SCOPE, vector=_vec(_FIXTURE_TEXTS[4]), k=3)
    assert ranked[0][0].node_id == target.node_id
    assert ranked[0][1] == pytest.approx(1.0, abs=1e-9)


def test_knn_is_ordered_by_descending_similarity() -> None:
    graph, _ = _twelve_node_fixture()
    ranked = graph.knn(scope=SCOPE, vector=_vec("the gateway proxy"), k=12)
    scores = [score for _, score in ranked]
    assert scores == sorted(scores, reverse=True)


def test_knn_breaks_a_score_tie_on_node_id() -> None:
    """Two nodes with identical text must not reorder between runs."""
    graph = _graph()
    first = _create(graph, "aaa", text="identical text")
    second = _create(graph, "bbb", text="identical text")
    ranked = graph.knn(scope=SCOPE, vector=_vec("identical text"), k=2)
    assert [node.node_id for node, _ in ranked] == sorted([first.node_id, second.node_id])


def test_knn_with_k_larger_than_the_scope_returns_every_node() -> None:
    graph, nodes = _twelve_node_fixture()
    ranked = graph.knn(scope=SCOPE, vector=_vec("anything"), k=100)
    assert len(ranked) == len(nodes)


def test_knn_of_an_empty_scope_is_empty() -> None:
    assert _graph().knn(scope=SCOPE, vector=_vec("anything"), k=8) == []


def test_knn_is_scoped() -> None:
    graph = _graph()
    _create(graph, "here", scope=SCOPE, text="the gateway")
    _create(graph, "there", scope=OTHER_SCOPE, text="the gateway")
    ranked = graph.knn(scope=SCOPE, vector=_vec("the gateway"), k=10)
    assert [node.name for node, _ in ranked] == ["here"]


def test_a_foreign_dimension_query_raises() -> None:
    """A silent 0.0 would read as "nothing is similar" rather than "wrong space"."""
    graph, _ = _twelve_node_fixture()
    with pytest.raises(ValueError, match="different vector spaces"):
        graph.knn(scope=SCOPE, vector=(1.0,), k=3)


def test_a_zero_k_is_refused() -> None:
    graph, _ = _twelve_node_fixture()
    with pytest.raises(ValueError, match="k must be at least 1"):
        graph.knn(scope=SCOPE, vector=_vec("x"), k=0)


def test_knn_skips_a_freshly_split_part_that_has_no_embedding_yet() -> None:
    """Scoring it 0.0 would rank it against real nodes as though it had been measured."""
    graph, _ = _twelve_node_fixture()
    parent = _create(graph, "parent", text="something to split")
    parts = graph.split_node(node_id=parent.node_id, parts=[_split_spec("x"), _split_spec("y")], now=LATER)
    ranked = graph.knn(scope=SCOPE, vector=_vec("something to split"), k=20)
    returned = {node.node_id for node, _ in ranked}
    assert returned.isdisjoint({part.node_id for part in parts})
    assert len(ranked) == 12


def test_neighbours_excludes_the_seeds_and_returns_each_node_once() -> None:
    graph = _graph()
    hub = _create(graph, "hub")
    spokes = [_create(graph, f"spoke-{index}") for index in range(3)]
    for spoke in spokes:
        _relate(graph, hub, spoke)
    found = graph.neighbours([hub.node_id])
    assert [node.name for node in found] == ["spoke-0", "spoke-1", "spoke-2"]


def test_neighbours_of_a_multi_seed_set_returns_each_linked_node_exactly_once() -> None:
    """Two seeds sharing a neighbour must not return it twice -- a read would double-count."""
    graph = _graph()
    seeds = [_create(graph, f"seed-{index}") for index in range(3)]
    shared = _create(graph, "shared")
    for seed in seeds:
        _relate(graph, seed, shared)
    found = graph.neighbours([seed.node_id for seed in seeds])
    assert [node.node_id for node in found] == [shared.node_id]


def test_neighbours_is_direction_blind() -> None:
    """A relation is DIRECTED, and expansion is not.

    An edge is a belief about a pair, and a reader expanding from one end wants
    the other end whichever way the edge was written -- so both branches of the
    endpoint test are exercised here rather than only the one every other test
    happens to hit.
    """
    graph = _graph()
    source = _create(graph, "created-first")
    target = _create(graph, "created-second")
    _relate(graph, source, target)

    assert [node.node_id for node in graph.neighbours([source.node_id])] == [target.node_id]
    assert [node.node_id for node in graph.neighbours([target.node_id])] == [source.node_id]


def test_neighbours_does_not_walk_two_hops() -> None:
    graph = _graph()
    a, b, c = (_create(graph, name) for name in ("aaa", "bbb", "ccc"))
    _relate(graph, a, b)
    _relate(graph, c, b)
    assert [node.name for node in graph.neighbours([a.node_id])] == ["bbb"]


def test_neighbours_of_an_unlinked_node_is_empty() -> None:
    graph = _graph()
    node = _create(graph, "lonely")
    assert graph.neighbours([node.node_id]) == []


def test_neighbours_is_name_ordered() -> None:
    graph = _graph()
    hub = _create(graph, "hub")
    for name in ("zebra", "aardvark", "monkey"):
        _create(graph, name)
    for node in graph.list_nodes(scope=SCOPE):
        if node.name != "hub":
            _relate(graph, hub, node)
    assert [node.name for node in graph.neighbours([hub.node_id])] == ["aardvark", "monkey", "zebra"]


# -- unit 10's integration case ------------------------------------------------


def test_a_two_hundred_node_scope_serves_knn_and_one_hop_end_to_end() -> None:
    graph = _graph()
    nodes = [_create(graph, f"node-{index:03d}", text=f"concept number {index} of the system") for index in range(200)]
    ranked = graph.knn(scope=SCOPE, vector=_vec("concept number 137 of the system"), k=8)
    assert len(ranked) == 8
    assert ranked[0][0].node_id == nodes[137].node_id

    seeds = nodes[:3]
    targets = nodes[100:104]
    for seed in seeds:
        for target in targets:
            _relate(graph, seed, target)
    found = graph.neighbours([seed.node_id for seed in seeds])
    assert [node.node_id for node in found] == sorted(target.node_id for target in targets)


# ============================================= unit 11: merge and split


def test_merge_deletes_the_absorbed_nodes() -> None:
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "litellm proxy")
    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("never hermetic", kind=FactKind.RULE),),
        embedding=_vec("gateway litellm proxy"),
        now=LATER,
    )
    with pytest.raises(NodeNotFound):
        graph.get_node(absorbed.node_id)


def test_merge_gives_the_survivor_the_supplied_name_type_and_facts() -> None:
    graph = _graph()
    survivor = _create(graph, "gateway", node_type="service")
    absorbed = _create(graph, "proxy")
    merged = graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="jedai gateway",
        type="policy",
        facts=(_fact("never hermetic", kind=FactKind.RULE), _fact("a LiteLLM proxy", kind=FactKind.IS)),
        embedding=_vec("jedai gateway policy"),
        now=LATER,
    )
    assert merged.name == "jedai gateway"
    assert merged.type == "policy"
    assert merged.facts == (_fact("never hermetic", kind=FactKind.RULE), _fact("a LiteLLM proxy", kind=FactKind.IS))
    assert merged.node_id == survivor.node_id
    assert merged.created_at == survivor.created_at
    assert merged.last_touched_at == LATER


def test_merge_leaves_the_count_correct() -> None:
    graph = _graph()
    nodes = [_create(graph, f"n{index}") for index in range(5)]
    graph.merge_nodes(
        survivor_id=nodes[0].node_id,
        absorbed_ids=[nodes[1].node_id, nodes[2].node_id],
        name="merged",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("merged"),
        now=LATER,
    )
    assert graph.count(scope=SCOPE) == 3


def test_a_merge_re_points_the_absorbed_nodes_relations_at_the_survivor() -> None:
    """THE load-bearing assertion of unit 11, inverted by #251 amendment D.

    It used to be ``degree(survivor) == 0``: an edge was a derivable weight, so
    dropping every edge and letting the caller rebuild from
    ``ledger.cooccurrence`` cost nothing. An edge now carries its own CLAIM,
    which the ledger cannot re-derive -- the sentence is there, but which pair of
    nodes it holds between was the matcher's decision -- so dropping it would
    destroy a belief nobody could get back.
    """
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "proxy")
    bystander = _create(graph, "chart")
    _relate(graph, survivor, bystander, type="FRONTS", claim="the gateway fronts the chart")
    _relate(graph, absorbed, bystander, type="DEPLOYS", claim="the proxy is deployed by the chart")

    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    kept = graph.relations_of(survivor.node_id)
    assert {relation.type for relation in kept} == {"FRONTS", "DEPLOYS"}
    assert all(relation.source_id == survivor.node_id for relation in kept)
    assert {relation.claim for relation in kept} == {
        "the gateway fronts the chart",
        "the proxy is deployed by the chart",
    }
    assert len(graph.relations_of(bystander.node_id)) == 2


def test_a_merge_drops_the_self_loop_it_creates() -> None:
    """The case a live run produced every time: the nodes a merge picks are the
    ones already pointing at each other, and nothing relates to itself."""
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "proxy")
    _relate(graph, survivor, absorbed, type="FRONTS", claim="the gateway fronts the proxy")
    assert len(graph.relations(scope=SCOPE)) == 1

    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    assert graph.relations(scope=SCOPE) == []


def test_a_merge_unions_two_edges_that_become_one_triple() -> None:
    """Two edges of one type onto one bystander become one, with both evidences.

    Leaving two would break the ``(source, type, target)`` identity
    ``upsert_relation`` exists to keep, so the next reinforcement would find only
    one of them.
    """
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "proxy")
    bystander = _create(graph, "chart")
    _relate(graph, survivor, bystander, type="DEPLOYS", claim="kept", entries=("e-01",))
    _relate(graph, absorbed, bystander, type="DEPLOYS", claim="folded", entries=("e-02",))

    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    kept = graph.relations_of(survivor.node_id)
    assert len(kept) == 1
    assert kept[0].claim == "kept"
    assert set(kept[0].entry_ids) == {"e-01", "e-02"}


def test_a_re_pointed_relation_keeps_its_own_id() -> None:
    """A reader handed ``r-001`` by a rendering must still be able to resolve it."""
    graph = _graph()
    survivor = _create(graph, "gateway")
    absorbed = _create(graph, "proxy")
    bystander = _create(graph, "chart")
    moved = _relate(graph, absorbed, bystander, type="DEPLOYS", claim="deployed")

    graph.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    assert graph.get_relation(moved.relation_id).source_id == survivor.node_id


def test_merging_a_node_into_itself_is_refused() -> None:
    graph = _graph()
    node = _create(graph, "gateway")
    with pytest.raises(ValueError, match="cannot absorb the survivor into itself"):
        graph.merge_nodes(
            survivor_id=node.node_id,
            absorbed_ids=[node.node_id],
            name="g",
            type="service",
            facts=(_fact("x"),),
            embedding=_vec("g"),
            now=LATER,
        )


def test_merging_across_scopes_is_refused() -> None:
    """One graph per scope is the design; a cross-scope merge would silently break it."""
    graph = _graph()
    survivor = _create(graph, "here", scope=SCOPE)
    foreign = _create(graph, "there", scope=OTHER_SCOPE)
    with pytest.raises(ValueError, match="cannot merge across scopes"):
        graph.merge_nodes(
            survivor_id=survivor.node_id,
            absorbed_ids=[foreign.node_id],
            name="g",
            type="service",
            facts=(_fact("x"),),
            embedding=_vec("g"),
            now=LATER,
        )


def test_a_refused_merge_deletes_nothing() -> None:
    graph = _graph()
    survivor = _create(graph, "here", scope=SCOPE)
    foreign = _create(graph, "there", scope=OTHER_SCOPE)
    with pytest.raises(ValueError):
        graph.merge_nodes(
            survivor_id=survivor.node_id,
            absorbed_ids=[foreign.node_id],
            name="g",
            type="service",
            facts=(_fact("x"),),
            embedding=_vec("g"),
            now=LATER,
        )
    assert graph.get_node(foreign.node_id) == foreign


def test_merging_an_unknown_node_raises_before_anything_is_deleted() -> None:
    graph = _graph()
    survivor = _create(graph, "gateway")
    real = _create(graph, "proxy")
    with pytest.raises(NodeNotFound):
        graph.merge_nodes(
            survivor_id=survivor.node_id,
            absorbed_ids=[real.node_id, "n-999"],
            name="g",
            type="service",
            facts=(_fact("x"),),
            embedding=_vec("g"),
            now=LATER,
        )
    assert graph.get_node(real.node_id) == real


def test_split_returns_one_node_per_part_and_deletes_the_parent() -> None:
    graph = _graph()
    parent = _create(graph, "mixed")
    parts = graph.split_node(
        node_id=parent.node_id,
        parts=[_split_spec("chart"), _split_spec("c4", node_type="cluster")],
        now=LATER,
    )
    assert len(parts) == 2
    assert [part.name for part in parts] == ["chart", "c4"]
    assert [part.type for part in parts] == ["artifact", "cluster"]
    with pytest.raises(NodeNotFound):
        graph.get_node(parent.node_id)


def test_split_leaves_the_count_correct() -> None:
    graph = _graph()
    for index in range(3):
        _create(graph, f"n{index}")
    parent = graph.list_nodes(scope=SCOPE)[0]
    graph.split_node(node_id=parent.node_id, parts=[_split_spec("a"), _split_spec("b")], now=LATER)
    assert graph.count(scope=SCOPE) == 4


def test_split_parts_inherit_the_parents_scope_and_start_factless() -> None:
    graph = _graph()
    parent = _create(graph, "mixed", scope=OTHER_SCOPE)
    parts = graph.split_node(node_id=parent.node_id, parts=[_split_spec("a"), _split_spec("b")], now=LATER)
    assert {part.scope for part in parts} == {OTHER_SCOPE}
    assert parts[0].facts == ()


def test_split_parts_are_dirty_and_embeddingless_awaiting_their_first_dream() -> None:
    """`SplitSpec` carries no vector, so the next incremental dream computes one."""
    graph = _graph()
    parent = _create(graph, "mixed")
    parts = graph.split_node(node_id=parent.node_id, parts=[_split_spec("a"), _split_spec("b")], now=LATER)
    for part in parts:
        assert part.dirty is True
        assert part.embedding == ()
        assert part.dreamed_at is None
    assert [node.name for node in graph.dirty(scope=SCOPE)] == ["a", "b"]


def test_a_split_part_takes_the_relations_it_claimed() -> None:
    """``relation_ids`` is how a part keeps an edge the parent held."""
    graph = _graph()
    parent = _create(graph, "mixed")
    bystander = _create(graph, "chart")
    edge = _relate(graph, parent, bystander, type="DEPLOYS", claim="deployed by the chart")
    parts = graph.split_node(
        node_id=parent.node_id,
        parts=[_split_spec("a", relations=(edge.relation_id,)), _split_spec("b")],
        now=LATER,
    )
    assert graph.get_relation(edge.relation_id).source_id == parts[0].node_id
    assert graph.relations_of(parts[1].node_id) == []


def test_a_split_drops_an_edge_no_part_claimed() -> None:
    """A split deletes the parent, so an edge nobody claimed has no endpoint left.

    The same rule the old store applied to EVERY edge of a split parent, with the
    difference that a part can now keep one.
    """
    graph = _graph()
    parent = _create(graph, "mixed")
    bystander = _create(graph, "chart")
    _relate(graph, parent, bystander)
    graph.split_node(node_id=parent.node_id, parts=[_split_spec("a"), _split_spec("b")], now=LATER)
    assert graph.relations(scope=SCOPE) == []
    assert graph.relations_of(bystander.node_id) == []


def test_a_split_refuses_a_relation_id_it_does_not_own() -> None:
    graph = _graph()
    parent = _create(graph, "mixed")
    other = _create(graph, "elsewhere")
    bystander = _create(graph, "chart")
    foreign = _relate(graph, other, bystander)
    with pytest.raises(ValueError, match="does not touch this node"):
        graph.split_node(
            node_id=parent.node_id,
            parts=[_split_spec("a", relations=(foreign.relation_id,)), _split_spec("b")],
            now=LATER,
        )
    assert graph.get_node(parent.node_id) == parent


def test_a_split_refuses_two_parts_claiming_one_relation() -> None:
    """An ambiguous claim has no defined resolution, and guessing one would make
    which part keeps a belief depend on iteration order."""
    graph = _graph()
    parent = _create(graph, "mixed")
    bystander = _create(graph, "chart")
    edge = _relate(graph, parent, bystander)
    with pytest.raises(ValueError, match="claimed by both"):
        graph.split_node(
            node_id=parent.node_id,
            parts=[_split_spec("a", relations=(edge.relation_id,)), _split_spec("b", relations=(edge.relation_id,))],
            now=LATER,
        )
    assert graph.get_node(parent.node_id) == parent


def test_splitting_an_unknown_node_raises() -> None:
    with pytest.raises(NodeNotFound):
        _graph().split_node(node_id="n-999", parts=[_split_spec("a"), _split_spec("b")], now=LATER)


def test_splitting_into_zero_parts_is_refused() -> None:
    graph = _graph()
    parent = _create(graph, "mixed")
    with pytest.raises(ValueError, match="into zero parts"):
        graph.split_node(node_id=parent.node_id, parts=[], now=LATER)
    assert graph.get_node(parent.node_id) == parent


# ============================================ typed relations (#251 amendment D)


def test_an_upsert_creates_an_edge_with_its_claim_its_evidence_and_its_marker() -> None:
    """A belief between two concepts, not a weight. The amendment's central change."""
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    relation = _relate(
        graph,
        source,
        target,
        type="BLOCKED",
        claim="the Host header was refused",
        entries=("e-03",),
        until="#245",
    )
    assert relation.triple == (source.node_id, "BLOCKED", target.node_id)
    assert relation.claim == "the Host header was refused"
    assert relation.entry_ids == ("e-03",)
    assert relation.until == "#245"
    assert relation.evidence == 1


def test_relation_ids_are_minted_uniquely_under_their_own_prefix() -> None:
    """A separate sequence and prefix from nodes, so an id in a rendering or an
    error says on sight which kind of thing it names."""
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    third = _create(graph, "ccc")
    ids = [_relate(graph, first, second).relation_id, _relate(graph, first, third).relation_id]
    assert ids == ["r-001", "r-002"]
    assert first.node_id == "n-001"


def test_restating_the_same_triple_REINFORCES_it_rather_than_duplicating() -> None:
    """The reinforcement mechanism, and the reason evidence is a field.

    A second claim about one pair unions its entry id in and moves ``last_seen``:
    the record gets stronger instead of there being two records saying one thing.
    """
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, type="BLOCKED", claim="first wording", entries=("e-01",))
    again = _relate(graph, source, target, type="BLOCKED", claim=None, entries=("e-09",), now=LATER)

    assert len(graph.relations(scope=SCOPE)) == 1
    assert again.entry_ids == ("e-01", "e-09")
    assert again.evidence == 2
    assert again.claim == "first wording"
    assert again.last_seen == LATER
    assert again.first_seen == NOW


def test_a_reinforcement_may_replace_the_claim_when_it_is_given_a_new_one() -> None:
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, claim="first wording", entries=("e-01",))
    updated = _relate(graph, source, target, claim="clearer wording", entries=("e-02",))
    assert updated.claim == "clearer wording"


def test_an_upsert_with_no_claim_on_a_NEW_edge_is_refused() -> None:
    """An edge that asserts nothing is not a belief, and only a reinforcement may
    say "keep whatever is there"."""
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    with pytest.raises(ValueError, match="needs a claim"):
        _relate(graph, source, target, claim=None)
    assert graph.relations(scope=SCOPE) == []


def test_a_reinforcement_with_no_claim_keeps_the_marker_with_the_text() -> None:
    """A claim and its marker are one statement, so they move together.

    ``claim=None`` says the caller has no text for this belief, which is
    ``reconcile`` on a restated claim: it holds the raw ledger claim -- the wrong
    text to overwrite a dreamer's compression with -- and no basis at all for a
    marker. If the marker cleared here, every restatement of a belief would erase
    the compressed version of it.
    """
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, claim="Host header refused", entries=("e-01",), until="#245")
    kept = _relate(graph, source, target, claim=None, entries=("e-02",), until=None)
    assert kept.claim == "Host header refused"
    assert kept.until == "#245"
    assert kept.entry_ids == ("e-01", "e-02")


def test_a_restatement_applies_the_marker_it_IS_given() -> None:
    """A caller giving a claim is restating the edge, marker included."""
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, claim="Host header refused", entries=("e-01",), until="#245")
    rewritten = _relate(graph, source, target, claim="the rewrite lands", entries=("e-02",), until="#248")
    assert rewritten.claim == "the rewrite lands"
    assert rewritten.until == "#248"


def test_a_restatement_clears_the_marker_by_not_giving_one() -> None:
    """ "It was fixed, and then it regressed" -- the dreamer's case, and it stays expressible.

    The dreamer always states a claim, so ``until=None`` beside one reads as
    "this edge no longer carries a marker" rather than as "I have no opinion".
    """
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, claim="Host header refused", entries=("e-01",), until="#245")
    regressed = _relate(graph, source, target, claim="Host header refused again", entries=("e-02",), until=None)
    assert regressed.until is None
    assert regressed.claim == "Host header refused again"


def test_two_types_between_one_pair_are_two_edges() -> None:
    """The triple is the identity, so a different type is a different belief."""
    graph = _graph()
    source = _create(graph, "gateway")
    target = _create(graph, "agent-memory")
    _relate(graph, source, target, type="BLOCKED", claim="refused the Host header")
    _relate(graph, source, target, type="FRONTS", claim="fronts the models it routes to")
    assert {relation.type for relation in graph.relations(scope=SCOPE)} == {"BLOCKED", "FRONTS"}


def test_the_direction_is_part_of_the_identity() -> None:
    """``a BLOCKED b`` and ``b BLOCKED a`` are different claims about the world."""
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    _relate(graph, first, second, type="BLOCKED", claim="one way")
    _relate(graph, second, first, type="BLOCKED", claim="the other way")
    assert len(graph.relations(scope=SCOPE)) == 2


@pytest.mark.parametrize("bad", ["blocked", "B", "BLOCKED-BY", "1X"])
def test_an_illegal_edge_type_is_refused_at_the_store(bad: str) -> None:
    """Validated where a type is SET, not only where a record is constructed:
    ``model_copy`` does not re-validate, so a rename could otherwise smuggle one in."""
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    with pytest.raises(ValueError, match="must match"):
        _relate(graph, first, second, type=bad)


def test_an_edge_with_no_evidence_is_refused() -> None:
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    with pytest.raises(ValueError, match="needs at least one ledger entry id"):
        _relate(graph, first, second, entries=())


def test_a_self_relation_is_refused() -> None:
    graph = _graph()
    node = _create(graph, "aaa")
    with pytest.raises(ValueError, match=r"cannot point .* at itself"):
        _relate(graph, node, node)


def test_relating_to_an_unknown_node_is_refused() -> None:
    """A phantom endpoint would corrupt ``neighbours``."""
    graph = _graph()
    node = _create(graph, "aaa")
    with pytest.raises(NodeNotFound):
        graph.upsert_relation(
            scope=SCOPE,
            source_id=node.node_id,
            target_id="n-999",
            type="BLOCKED",
            claim="x",
            entry_ids=("e-01",),
            until=None,
            now=NOW,
        )


def test_relating_across_scopes_is_refused() -> None:
    graph = _graph()
    here = _create(graph, "here", scope=SCOPE)
    there = _create(graph, "there", scope=OTHER_SCOPE)
    with pytest.raises(ValueError, match="cannot relate across scopes"):
        graph.upsert_relation(
            scope=SCOPE,
            source_id=here.node_id,
            target_id=there.node_id,
            type="BLOCKED",
            claim="x",
            entry_ids=("e-01",),
            until=None,
            now=NOW,
        )


def test_relations_are_per_scope_and_stably_ordered() -> None:
    graph = _graph()
    here_a = _create(graph, "here-a", scope=SCOPE)
    here_b = _create(graph, "here-b", scope=SCOPE)
    there_a = _create(graph, "there-a", scope=OTHER_SCOPE)
    there_b = _create(graph, "there-b", scope=OTHER_SCOPE)
    _relate(graph, here_a, here_b, type="ZED", claim="z")
    _relate(graph, here_b, here_a, type="ALPHA", claim="a")
    _relate(graph, there_a, there_b)
    assert [relation.type for relation in graph.relations(scope=SCOPE)] == ["ZED", "ALPHA"]
    assert len(graph.relations(scope=OTHER_SCOPE)) == 1
    assert graph.relations(scope=SCOPE) == graph.relations(scope=SCOPE)


def test_relations_of_returns_both_directions_and_is_the_nodes_degree() -> None:
    """There is no ``degree`` method: two ways to count one thing is two numbers."""
    graph = _graph()
    hub = _create(graph, "hub")
    spokes = [_create(graph, f"spoke-{index}") for index in range(3)]
    _relate(graph, hub, spokes[0])
    _relate(graph, spokes[1], hub)
    _relate(graph, spokes[1], spokes[2])
    assert len(graph.relations_of(hub.node_id)) == 2
    assert len(graph.relations_of(spokes[1].node_id)) == 2
    assert len(graph.relations_of(spokes[2].node_id)) == 1


def test_relations_of_an_unrelated_node_is_empty() -> None:
    graph = _graph()
    assert graph.relations_of(_create(graph, "lonely").node_id) == []


def test_retiring_a_relation_removes_it() -> None:
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    relation = _relate(graph, first, second)
    graph.retire_relation(relation.relation_id)
    assert graph.relations(scope=SCOPE) == []


def test_retiring_an_unknown_relation_raises() -> None:
    with pytest.raises(NodeNotFound, match="no such relation"):
        _graph().retire_relation("r-999")


def test_get_relation_is_on_the_seam_for_the_journal_to_read_before_a_retire() -> None:
    """An event recording only an id would journal the loss of a belief without it."""
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    relation = _relate(graph, first, second, claim="the belief")
    assert graph.get_relation(relation.relation_id).claim == "the belief"
    with pytest.raises(NodeNotFound):
        graph.get_relation("r-999")


def test_delete_nodes_removes_the_relations_too() -> None:
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    _relate(graph, first, second)
    graph.delete_nodes([first.node_id])
    assert graph.relations(scope=SCOPE) == []
    assert graph.relations_of(second.node_id) == []


# ------------------------------------------------------- the edge vocabulary


def test_edge_types_counts_each_type_most_used_first_then_alphabetically() -> None:
    """Rendered into a prompt, so a vocabulary that reorders between runs changes
    the prompt without changing the policy."""
    graph = _graph()
    nodes = [_create(graph, f"n{index}") for index in range(4)]
    _relate(graph, nodes[0], nodes[1], type="DEPLOYS", claim="d")
    _relate(graph, nodes[0], nodes[2], type="DEPLOYS", claim="d")
    _relate(graph, nodes[1], nodes[2], type="BLOCKED", claim="b")
    _relate(graph, nodes[2], nodes[3], type="ALPHA", claim="a")
    assert list(graph.edge_types(scope=SCOPE).items()) == [("DEPLOYS", 2), ("ALPHA", 1), ("BLOCKED", 1)]


def test_edge_types_of_an_empty_scope_is_empty() -> None:
    assert _graph().edge_types(scope=SCOPE) == {}


def test_renaming_an_edge_type_re_labels_every_edge_and_reports_how_many() -> None:
    """How the vocabulary is compacted when it exceeds ``M``."""
    graph = _graph()
    nodes = [_create(graph, f"n{index}") for index in range(3)]
    _relate(graph, nodes[0], nodes[1], type="FRONTS", claim="f1")
    _relate(graph, nodes[1], nodes[2], type="FRONTS", claim="f2")
    assert graph.rename_edge_type(scope=SCOPE, old="FRONTS", new="PROXIES") == 2
    assert graph.edge_types(scope=SCOPE) == {"PROXIES": 2}


def test_a_rename_that_collides_folds_into_the_edge_already_there() -> None:
    """Two edges with one triple is exactly what ``upsert_relation`` prevents, so a
    compaction that created one would break the invariant it was run to restore."""
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    _relate(graph, first, second, type="DEPLOYS", claim="kept", entries=("e-01",))
    _relate(graph, first, second, type="FRONTS", claim="folded", entries=("e-02",))
    assert graph.rename_edge_type(scope=SCOPE, old="FRONTS", new="DEPLOYS") == 1

    remaining = graph.relations(scope=SCOPE)
    assert len(remaining) == 1
    assert remaining[0].claim == "kept"
    assert set(remaining[0].entry_ids) == {"e-01", "e-02"}


def test_renaming_a_type_no_edge_carries_changes_nothing() -> None:
    graph = _graph()
    first = _create(graph, "aaa")
    second = _create(graph, "bbb")
    _relate(graph, first, second, type="DEPLOYS", claim="d")
    assert graph.rename_edge_type(scope=SCOPE, old="ABSENT", new="OTHER") == 0
    assert graph.edge_types(scope=SCOPE) == {"DEPLOYS": 1}


def test_renaming_a_type_to_itself_is_refused() -> None:
    with pytest.raises(ValueError, match="to itself"):
        _graph().rename_edge_type(scope=SCOPE, old="DEPLOYS", new="DEPLOYS")


def test_renaming_to_an_illegal_type_is_refused() -> None:
    """``model_copy`` does not re-validate, so the check is explicit here."""
    with pytest.raises(ValueError, match="must match"):
        _graph().rename_edge_type(scope=SCOPE, old="DEPLOYS", new="deploys")


def test_a_rename_stays_inside_its_scope() -> None:
    graph = _graph()
    here_a = _create(graph, "here-a", scope=SCOPE)
    here_b = _create(graph, "here-b", scope=SCOPE)
    there_a = _create(graph, "there-a", scope=OTHER_SCOPE)
    there_b = _create(graph, "there-b", scope=OTHER_SCOPE)
    _relate(graph, here_a, here_b, type="FRONTS", claim="f")
    _relate(graph, there_a, there_b, type="FRONTS", claim="f")
    assert graph.rename_edge_type(scope=SCOPE, old="FRONTS", new="PROXIES") == 1
    assert graph.edge_types(scope=OTHER_SCOPE) == {"FRONTS": 1}


def test_two_graphs_do_not_share_state() -> None:
    """The control on `__init__`: class-level mutable state is the classic version."""
    first, second = _graph(), _graph()
    _create(first, "gateway")
    assert second.count(scope=SCOPE) == 0


# ============================== the journal (#251 amendment D)


class _FixedClock:
    """A ``Clock`` that never moves, so an event's ``ts`` is not what is asserted."""

    def now(self) -> datetime:
        return LATER


def _journalled() -> tuple[JournaledGraph, InMemoryGraph, CavemanLedger, InMemoryReceipts]:
    """A journal over the REAL store and the REAL ledger, not over fakes.

    "Every mutation is journalled" is only worth asserting over the write paths
    that actually exist, and the sqlite ledger is what the event has to survive
    in -- an in-memory list of events would not prove the row round-trips.
    """
    inner = InMemoryGraph()
    ledger = CavemanLedger(":memory:")
    receipts = InMemoryReceipts()
    clock: Clock = _FixedClock()
    return JournaledGraph(inner, ledger=ledger, receipts=receipts, clock=clock), inner, ledger, receipts


def _ops(ledger: CavemanLedger) -> list[DreamOp]:
    return [event.op for event in ledger.events(scope=SCOPE)]


def test_the_journal_satisfies_every_graphstore_member_by_signature() -> None:
    """It goes wherever the store goes, so the stages cannot tell it is there."""
    mismatched: list[str] = []
    for attribute in sorted(GraphStore.__protocol_attrs__):
        implementation = getattr(JournaledGraph, attribute, None)
        assert implementation is not None, f"JournaledGraph is missing {attribute}"
        expected = inspect.signature(getattr(GraphStore, attribute))
        actual = inspect.signature(implementation)
        if expected != actual:
            mismatched.append(f"{attribute}: protocol {expected} != impl {actual}")
    assert not mismatched, "signature drift:\n" + "\n".join(mismatched)


def test_the_inner_store_contains_no_journalling_code_at_all() -> None:
    """Compose, do not embed. A store with no journalling code cannot forget to
    journal, and the next GraphStore inherits the journal by being wrapped."""
    source = inspect.getsource(InMemoryGraph)
    assert "DreamEvent" not in source
    assert "record_event" not in source


def test_creating_a_node_is_journalled_with_its_content_and_no_before() -> None:
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway", facts=(_fact("x"),))
    events = ledger.events(scope=SCOPE)
    assert [event.op for event in events] == [DreamOp.NODE_CREATED]
    assert events[0].node_ids == (node.node_id,)
    assert events[0].before == {}
    assert events[0].after == {"node": node_content(node)}


def test_a_journalled_event_never_carries_an_embedding() -> None:
    """A vector is derived from exactly the content the event already holds, so
    journalling it would double the ledger and make a replay need the transport."""
    journal, _, ledger, _ = _journalled()
    _create(journal, "gateway", facts=(_fact("x"),))
    payload = repr(ledger.events(scope=SCOPE)[0].model_dump(mode="json"))
    assert "embedding" not in payload


def test_every_event_carries_a_receipt_that_was_actually_emitted() -> None:
    """An event naming a receipt nobody emitted is a dangling pointer in the one
    record the replay proof rests on."""
    journal, _, ledger, receipts = _journalled()
    _create(journal, "gateway", facts=(_fact("x"),))
    emitted = {receipt.receipt_id: receipt for receipt in receipts.all(scope=SCOPE)}
    for event in ledger.events(scope=SCOPE):
        assert event.receipt_id in emitted
        assert emitted[event.receipt_id].op is ReceiptOp.GRAPH_MUTATED


def test_rewriting_a_nodes_facts_is_journalled_before_and_after() -> None:
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway", facts=(_fact("first"),))
    journal.replace_facts(node.node_id, facts=(_fact("second"),), type="policy", embedding=_vec("g"), now=LATER)
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.NODE_REWRITTEN
    assert event.before["node"]["facts"][0]["text"] == "first"
    assert event.after["node"]["facts"][0]["text"] == "second"


def test_a_type_only_change_is_journalled_as_a_RETYPE() -> None:
    """Two ops for one store method because they are two different decisions, and
    a reader of the history should not have to diff the facts to tell them apart."""
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway", facts=(_fact("same"),))
    journal.replace_facts(node.node_id, facts=(_fact("same"),), type="policy", embedding=_vec("g"), now=LATER)
    assert _ops(ledger)[-1] is DreamOp.NODE_RETYPED


def test_adding_aliases_is_journalled() -> None:
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway")
    journal.add_aliases(node.node_id, ["LiteLLM proxy"])
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.ALIASES_ADDED
    assert event.after["node"]["aliases"] == ["LiteLLM proxy"]


def test_a_merge_is_journalled_with_both_sides_and_the_relations_it_moved() -> None:
    journal, _, ledger, _ = _journalled()
    survivor = _create(journal, "gateway")
    absorbed = _create(journal, "proxy")
    bystander = _create(journal, "chart")
    _relate(journal, absorbed, bystander, type="DEPLOYS", claim="deployed")
    journal.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.NODE_MERGED
    assert event.node_ids == (survivor.node_id, absorbed.node_id)
    assert event.before["absorbed"][0]["node_id"] == absorbed.node_id
    assert event.before["relations"][0]["source_id"] == absorbed.node_id
    assert event.after["relations"][0]["source_id"] == survivor.node_id


def test_a_split_is_journalled_with_the_parent_and_its_parts() -> None:
    journal, _, ledger, _ = _journalled()
    parent = _create(journal, "mixed")
    parts = journal.split_node(node_id=parent.node_id, parts=[_split_spec("a"), _split_spec("b")], now=LATER)
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.NODE_SPLIT
    assert event.node_ids == (parent.node_id, *(part.node_id for part in parts))
    assert [part["name"] for part in event.after["parts"]] == ["a", "b"]


def test_deleting_nodes_is_journalled_with_what_was_actually_removed() -> None:
    """Unknown ids are ignored by the store -- erasure works from ledger ids a merge
    may already have consumed -- so journalling the request would record deletions
    that did not happen."""
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway")
    journal.delete_nodes([node.node_id, "n-999"])
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.NODE_DELETED
    assert event.node_ids == (node.node_id,)
    assert event.after == {}


def test_deleting_nothing_journals_nothing() -> None:
    journal, _, ledger, _ = _journalled()
    journal.delete_nodes(["n-999"])
    assert ledger.events(scope=SCOPE) == []


def test_an_upsert_is_journalled_as_a_creation_then_as_a_reinforcement() -> None:
    """The two are distinguishable in the history: one has no ``before``."""
    journal, _, ledger, _ = _journalled()
    source = _create(journal, "gateway")
    target = _create(journal, "agent-memory")
    _relate(journal, source, target, entries=("e-01",))
    _relate(journal, source, target, claim=None, entries=("e-02",))

    events = [event for event in ledger.events(scope=SCOPE) if event.op is DreamOp.RELATION_UPSERTED]
    assert events[0].before == {}
    assert events[1].before["relation"]["entry_ids"] == ["e-01"]
    assert events[1].after["relation"]["entry_ids"] == ["e-01", "e-02"]


def test_retiring_a_relation_journals_the_belief_it_removed() -> None:
    journal, _, ledger, _ = _journalled()
    source = _create(journal, "gateway")
    target = _create(journal, "agent-memory")
    relation = _relate(journal, source, target, claim="the belief")
    journal.retire_relation(relation.relation_id)
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.RELATION_RETIRED
    assert event.before["relation"] == relation_content(relation)
    assert event.after == {}


def test_renaming_an_edge_type_is_journalled_without_naming_nodes() -> None:
    """An edge-type rename is about a label across however many relations carry
    it; naming every endpoint would record the type's popularity, not the decision."""
    journal, _, ledger, _ = _journalled()
    first = _create(journal, "aaa")
    second = _create(journal, "bbb")
    _relate(journal, first, second, type="FRONTS", claim="f")
    journal.rename_edge_type(scope=SCOPE, old="FRONTS", new="PROXIES")
    event = ledger.events(scope=SCOPE)[-1]
    assert event.op is DreamOp.EDGE_TYPE_RENAMED
    assert event.node_ids == ()
    assert event.before["type"] == "FRONTS"
    assert event.after["type"] == "PROXIES"


@pytest.mark.parametrize("operation", ["mark_dirty", "clear_dirty", "record_read"])
def test_bookkeeping_writes_are_deliberately_NOT_journalled(operation: str) -> None:
    """They change how a node has been used, not what it asserts. A journal that
    recorded them would put "somebody read this" in the history of what is true,
    and a replay applying them would have to invent a read."""
    journal, _, ledger, _ = _journalled()
    node = _create(journal, "gateway")
    before = len(ledger.events(scope=SCOPE))
    if operation == "clear_dirty":
        journal.clear_dirty([node.node_id], now=LATER)
    else:
        getattr(journal, operation)([node.node_id])
    assert len(ledger.events(scope=SCOPE)) == before


@pytest.mark.parametrize(
    "read",
    ["count", "list_nodes", "type_vocabulary", "relations", "edge_types", "dirty"],
)
def test_every_scoped_read_delegates_untouched_and_journals_nothing(read: str) -> None:
    journal, inner, ledger, _ = _journalled()
    _create(journal, "gateway", facts=(_fact("x"),))
    before = len(ledger.events(scope=SCOPE))
    assert getattr(journal, read)(scope=SCOPE) == getattr(inner, read)(scope=SCOPE)
    assert len(ledger.events(scope=SCOPE)) == before


def test_the_journals_unscoped_reads_delegate_untouched() -> None:
    journal, inner, ledger, _ = _journalled()
    node = _create(journal, "gateway", facts=(_fact("x"),))
    other = _create(journal, "chart")
    relation = _relate(journal, node, other)
    before = len(ledger.events(scope=SCOPE))

    assert journal.get_node(node.node_id) == inner.get_node(node.node_id)
    assert journal.get_nodes([node.node_id]) == inner.get_nodes([node.node_id])
    assert journal.node_by_name(scope=SCOPE, name="gateway") == node
    assert journal.node_by_alias(scope=SCOPE, name="GATEWAY") == node
    assert journal.knn(scope=SCOPE, vector=_vec("gateway"), k=1) == inner.knn(scope=SCOPE, vector=_vec("gateway"), k=1)
    assert journal.neighbours([node.node_id]) == inner.neighbours([node.node_id])
    assert journal.relations_of(node.node_id) == inner.relations_of(node.node_id)
    assert journal.get_relation(relation.relation_id) == relation
    assert len(ledger.events(scope=SCOPE)) == before


def test_event_ids_are_unique_per_ledger_under_their_own_prefix() -> None:
    """Unique per LEDGER, not per wrapper -- a ledger outlives the wrapper writing to it.

    Two memories composed over one store (an ingest phase and a dream phase) and
    a server restarting against a sqlite file it already wrote both used to
    restart a per-instance counter at one and hit the table's UNIQUE constraint
    on their second event. Ordering is the ledger's ``(ts, insertion sequence)``
    and never the id's, so nothing reads these as a sequence.
    """
    journal, _, ledger, _ = _journalled()
    _create(journal, "aaa")
    _create(journal, "bbb")
    second_journal = JournaledGraph(InMemoryGraph(), ledger=ledger, receipts=InMemoryReceipts(), clock=_FixedClock())
    _create(second_journal, "ccc")

    ids = [event.event_id for event in ledger.events(scope=SCOPE)]
    assert len(ids) == 3
    assert len(set(ids)) == 3
    assert all(event_id.startswith(EVENT_ID_PREFIX) for event_id in ids)


def test_the_journal_records_one_event_per_mutation_in_applied_order() -> None:
    """The integration case: a whole pass, and the history reads as the sequence
    that produced the graph -- which is what a replay applies."""
    journal, _, ledger, _ = _journalled()
    survivor = _create(journal, "gateway")
    absorbed = _create(journal, "proxy")
    journal.add_aliases(survivor.node_id, ["JedAI Gateway"])
    _relate(journal, survivor, absorbed, claim="one claim named both")
    journal.merge_nodes(
        survivor_id=survivor.node_id,
        absorbed_ids=[absorbed.node_id],
        name="gateway",
        type="service",
        facts=(_fact("x"),),
        embedding=_vec("gateway"),
        now=LATER,
    )
    assert _ops(ledger) == [
        DreamOp.NODE_CREATED,
        DreamOp.NODE_CREATED,
        DreamOp.ALIASES_ADDED,
        DreamOp.RELATION_UPSERTED,
        DreamOp.NODE_MERGED,
    ]
