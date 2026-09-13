"""#251 unit 20: the pressure signal, node value, and the forced-merge slate.

This is the module whose correctness is least negotiable: it decides which nodes
lose their separate identity when a scope goes over ``N``, and it has to be
provable without an LLM. So every assertion below is arithmetic or ordering --
nothing here is scripted, mocked or approximate.

Two properties carry the weight:

* **The recency term is exactly 0.5 after one half-life.** Asserted with the
  other three weights set to zero, so the number under test is the term rather
  than a sum that happens to land nearby.
* **The slate is disjoint, exactly ``pressure`` long, and every doomed node
  comes from the bottom of the value order.** ``pressure`` merges of two nodes
  each collapse exactly ``pressure`` nodes away, so a slate that repeated a node
  or ran short would leave the bound violated with nothing reporting it.
* **A pair is directed, and the survivor is the higher-value half.** The
  survivor keeps its own name (#251 amendment A), so a slate that named the
  wrong half would rename a live concept after a leftover.
* **The peer order is ledger co-occurrence, then shared turns, then
  similarity.** Asserted with each pair of criteria pointing at different nodes,
  which is the only arrangement that can tell one rung of the ladder from the
  next -- and with counting ``cooccurrence`` and ``similarity`` callables, so
  "the rung below is not reached" is a property of the code rather than of the
  docstring.

Co-occurrence is supplied as a hand-written symmetric table behind a callable,
turn counts as a hand-written mapping and similarity as a hand-written matrix,
which is the whole reason :func:`~memotron.caveman.pressure.merge_slate`
takes two callables and a mapping: the pairing rule is asserted against counts,
turns and similarities chosen to make ties and near-ties happen, not against
whatever a real ledger and embedder produce.

Amendment D's adaptation is asserted here too: the first rung reads
``LedgerStore.cooccurrence`` rather than the edges between two nodes, so a pair
the ledger names together scores whether or not a relational claim ever created
an edge between them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

import pytest

from memotron.caveman.models import Node
from memotron.caveman.motive import ValueWeights, engineering_motive
from memotron.caveman.pressure import (
    DEFAULT_TYPE_VALUE,
    MergePair,
    ValuedNode,
    embedding_similarity,
    merge_slate,
    node_value,
    pair_key,
    pressure,
)
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"
HALF_LIFE = 30.0

EMBED = LocalEmbeddingTransport()

RECENCY_ONLY = ValueWeights(recency=1.0, reads=0.0, degree=0.0, type=0.0)
READS_ONLY = ValueWeights(recency=0.0, reads=1.0, degree=0.0, type=0.0)
DEGREE_ONLY = ValueWeights(recency=0.0, reads=0.0, degree=1.0, type=0.0)
TYPE_ONLY = ValueWeights(recency=0.0, reads=0.0, degree=0.0, type=1.0)


def _node(
    node_id: str,
    *,
    node_type: str = "service",
    read_count: int = 0,
    touched: datetime = NOW,
    embedding_text: str | None = None,
) -> Node:
    """A node with only the fields node value and the slate actually read."""
    embedding = tuple(EMBED.embed(embedding_text)) if embedding_text is not None else ()
    return Node(
        node_id=node_id,
        scope=SCOPE,
        name=node_id,
        type=node_type,
        facts=(),
        embedding=embedding,
        ledger_key=node_id,
        read_count=read_count,
        created_at=NOW - timedelta(days=365),
        last_touched_at=touched,
    )


def _valued(*pairs: tuple[str, float]) -> list[ValuedNode]:
    return [ValuedNode(node=_node(node_id), value=value) for node_id, value in pairs]


def _matrix(scores: Mapping[frozenset[str], float]) -> Callable[[Node, Node], float]:
    """A similarity function from an explicit table. Unlisted pairs score 0.0."""

    def similarity(first: Node, second: Node) -> float:
        return scores.get(frozenset({first.node_id, second.node_id}), 0.0)

    return similarity


def _shared(*pairs: tuple[str, str, int]) -> Callable[[str], Mapping[str, int]]:
    """``LedgerStore.cooccurrence`` from an explicit table. Unlisted pairs score 0.

    Written symmetrically -- both directions are filled from one entry -- because
    that is the ledger's own guarantee: ``cooccurrence(a)[b]`` counts the entries
    naming both, which is the same number as ``cooccurrence(b)[a]``. A one-sided
    table would let the slate pass while depending on which half it happened to
    ask.
    """
    table: dict[str, dict[str, int]] = {}
    for first, second, count in pairs:
        table.setdefault(first, {})[second] = count
        table.setdefault(second, {})[first] = count
    return lambda node_id: table.get(node_id, {})


def _counting_shared(*pairs: tuple[str, str, int]) -> tuple[Callable[[str], Mapping[str, int]], list[str]]:
    """:func:`_shared`, plus the list of node ids it was asked about, in order."""
    inner = _shared(*pairs)
    asked: list[str] = []

    def cooccurrence(node_id: str) -> Mapping[str, int]:
        asked.append(node_id)
        return inner(node_id)

    return cooccurrence, asked


def _turns(*shared: tuple[str, str, int]) -> dict[tuple[str, str], int]:
    """Shared turn counts in the shape ``LedgerStore.turn_cooccurrence`` returns.

    Canonically keyed by :func:`pair_key`, because the orientation is the one
    thing a hand-written mapping can get wrong -- and a pair keyed the other way
    round would silently score 0 rather than fail, which is a test that proves
    nothing.
    """
    return {(min(a, b), max(a, b)): count for a, b, count in shared}


# ============================================================ pressure(count, N)


@pytest.mark.parametrize(
    ("count", "max_nodes", "expected"),
    [
        (503, 500, 3),
        (4, 500, 0),
        (500, 500, 0),
        (501, 500, 1),
        (0, 1, 0),
        (8, 6, 2),
    ],
)
def test_pressure_is_the_overage_and_never_negative(count: int, max_nodes: int, expected: int) -> None:
    assert pressure(count, max_nodes) == expected


def test_pressure_rejects_a_negative_count() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        pressure(-1, 500)


def test_pressure_rejects_a_zero_budget() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        pressure(10, 0)


# =================================================================== node_value


def test_the_recency_term_is_exactly_one_half_after_one_half_life() -> None:
    """The load-bearing number: 0.5 at exactly one half-life, not merely near it.

    The other three weights are zero, so the assertion is about the term rather
    than about a sum that happens to land close.
    """
    node = _node("n-001", touched=NOW - timedelta(days=HALF_LIFE))
    value = node_value(
        node,
        degree=0,
        now=NOW,
        weights=RECENCY_ONLY,
        half_life_days=HALF_LIFE,
        type_weight={},
    )
    assert value == pytest.approx(0.5, abs=0.0)


@pytest.mark.parametrize(
    ("half_lives", "expected"),
    [(0.0, 1.0), (1.0, 0.5), (2.0, 0.25), (3.0, 0.125)],
)
def test_the_recency_term_halves_at_every_half_life(half_lives: float, expected: float) -> None:
    node = _node("n-001", touched=NOW - timedelta(days=HALF_LIFE * half_lives))
    assert node_value(
        node, degree=0, now=NOW, weights=RECENCY_ONLY, half_life_days=HALF_LIFE, type_weight={}
    ) == pytest.approx(expected)


def test_value_falls_monotonically_with_age() -> None:
    ages = [0, 5, 30, 90, 365]
    values = [
        node_value(
            _node("n-001", touched=NOW - timedelta(days=age)),
            degree=0,
            now=NOW,
            weights=RECENCY_ONLY,
            half_life_days=HALF_LIFE,
            type_weight={},
        )
        for age in ages
    ]
    assert values == sorted(values, reverse=True)
    assert len(set(values)) == len(values)


def test_value_rises_with_read_count() -> None:
    values = [
        node_value(
            _node("n-001", read_count=reads),
            degree=0,
            now=NOW,
            weights=READS_ONLY,
            half_life_days=HALF_LIFE,
            type_weight={},
        )
        for reads in (0, 1, 2, 10, 200)
    ]
    assert values == sorted(values)
    assert values[0] == 0.0
    assert values[1] == pytest.approx(math.log1p(1))


def test_value_rises_with_degree() -> None:
    values = [
        node_value(
            _node("n-001"),
            degree=degree,
            now=NOW,
            weights=DEGREE_ONLY,
            half_life_days=HALF_LIFE,
            type_weight={},
        )
        for degree in (0, 1, 3, 12)
    ]
    assert values == sorted(values)
    assert values[0] == 0.0


def test_the_reads_term_is_logarithmic_so_it_cannot_swamp_the_others() -> None:
    """A read count of 200 must not make every other term irrelevant.

    The failure this guards is the value function degenerating into "most read
    wins", which would make the motive's weights decorative.
    """
    weights = ValueWeights(recency=1.0, reads=1.0, degree=1.0, type=1.0)
    heavily_read = node_value(
        _node("n-001", read_count=200),
        degree=0,
        now=NOW,
        weights=weights,
        half_life_days=HALF_LIFE,
        type_weight={},
    )
    assert heavily_read < 10.0


def test_an_unranked_type_scores_the_neutral_default_not_zero() -> None:
    ranked = node_value(
        _node("n-001", node_type="policy"),
        degree=0,
        now=NOW,
        weights=TYPE_ONLY,
        half_life_days=HALF_LIFE,
        type_weight={"policy": 1.2},
    )
    unranked = node_value(
        _node("n-002", node_type="freshly-coined"),
        degree=0,
        now=NOW,
        weights=TYPE_ONLY,
        half_life_days=HALF_LIFE,
        type_weight={"policy": 1.2},
    )
    assert ranked == pytest.approx(1.2)
    assert unranked == pytest.approx(DEFAULT_TYPE_VALUE)
    assert unranked > 0.0


def test_the_motives_type_preference_shifts_the_value_order() -> None:
    motive = engineering_motive()
    policy = _node("n-001", node_type="policy")
    artifact = _node("n-002", node_type="artifact")
    kwargs = {
        "degree": 0,
        "now": NOW,
        "weights": motive.value_weights,
        "half_life_days": motive.recency_half_life_days,
        "type_weight": motive.type_weight,
    }
    assert node_value(policy, **kwargs) > node_value(artifact, **kwargs)  # type: ignore[arg-type]


def test_a_node_touched_after_now_is_a_clock_defect() -> None:
    node = _node("n-001", touched=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="cannot be newer than the clock"):
        node_value(node, degree=0, now=NOW, weights=RECENCY_ONLY, half_life_days=HALF_LIFE, type_weight={})


def test_a_non_positive_half_life_is_rejected() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        node_value(_node("n-001"), degree=0, now=NOW, weights=RECENCY_ONLY, half_life_days=0.0, type_weight={})


# ========================================================= embedding_similarity


def test_embedding_similarity_is_cosine_over_the_stored_vectors() -> None:
    first = _node("n-001", embedding_text="the JedAI gateway is a LiteLLM proxy")
    second = _node("n-002", embedding_text="the JedAI gateway is a LiteLLM proxy")
    third = _node("n-003", embedding_text="helm chart deploys the agent-memory MCP server")
    assert embedding_similarity(first, second) == pytest.approx(1.0)
    assert embedding_similarity(first, third) < embedding_similarity(first, second)


def test_an_undreamed_node_has_no_defined_similarity() -> None:
    dreamed = _node("n-001", embedding_text="gateway")
    fresh = _node("n-002")
    with pytest.raises(ValueError, match="n-002"):
        embedding_similarity(dreamed, fresh)
    with pytest.raises(ValueError, match="n-002"):
        embedding_similarity(fresh, dreamed)


# ================================================================== merge_slate


def test_zero_pressure_gives_an_empty_slate_and_asks_the_ledger_nothing() -> None:
    """A free pass must not pay for a scan it will not use -- neither rung runs."""
    calls: list[tuple[str, str]] = []

    def similarity(first: Node, second: Node) -> float:
        calls.append((first.node_id, second.node_id))
        return 1.0

    cooccurrence, asked = _counting_shared(("n-001", "n-002", 9))
    slate = merge_slate(
        _valued(("n-001", 1.0), ("n-002", 2.0)),
        pressure=0,
        cooccurrence=cooccurrence,
        turn_links={},
        similarity=similarity,
    )
    assert slate == ()
    assert calls == []
    assert asked == []


def test_the_ledger_is_asked_once_per_doomed_node_and_never_about_a_peer() -> None:
    """One lookup per fold, and it is the DOOMED node's.

    Co-occurrence is symmetric, so one call weighs the doomed node against the
    whole scope. Asking each candidate as well would make a global pass's ledger
    cost the square of the node count for a number it already holds.
    """
    nodes = _valued(*[(f"n-{index:03d}", float(index)) for index in range(1, 7)])
    cooccurrence, asked = _counting_shared(("n-001", "n-006", 4), ("n-002", "n-005", 4))
    slate = merge_slate(nodes, pressure=2, cooccurrence=cooccurrence, turn_links={}, similarity=_matrix({}))
    assert asked == ["n-001", "n-002"]
    assert [pair.doomed_node_id for pair in slate] == ["n-001", "n-002"]


def test_the_peer_is_the_best_linked_node_in_the_whole_scope() -> None:
    """The rule amendment A reversed: the peer may be the scope's best node.

    ``n-009`` is the most valuable node in the scope by two orders of magnitude
    and four ledger entries name it beside the doomed ``n-001``. It is the
    peer. Under the old bounded pool it could not be, and the merge that
    happened instead put two unrelated leftovers in one node.

    Nothing is taken from ``n-009`` by being named: it is the SURVIVOR, so it
    keeps its name and its id and only gains ``n-001``'s lines and aliases.
    """
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-009", 99.0))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-009", 4), ("n-001", "n-002", 1)),
        turn_links={},
        similarity=_matrix({}),
    )
    assert slate == (
        MergePair(doomed_node_id="n-001", survivor_node_id="n-009", link_weight=4, turn_cooccurrence=0, similarity=0.0),
    )


def test_link_weight_decides_the_peer_even_when_similarity_points_elsewhere() -> None:
    """The two criteria pointing at different nodes: the ledger wins.

    ``#245`` and ``#246`` embed nearly identically and name different defects,
    which is why "described alike" cannot outrank "discussed together".
    """
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-004", 2)),
        turn_links={},
        similarity=_matrix({frozenset({"n-001", "n-002"}): 0.99, frozenset({"n-001", "n-003"}): 0.95}),
    )
    assert slate[0].survivor_node_id == "n-004"
    assert slate[0].link_weight == 2
    assert slate[0].similarity == 0.0


def test_with_nothing_shared_at_all_similarity_chooses_the_peer() -> None:
    """The unshared case: every candidate scores 0, so the tie-break decides."""
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links={},
        similarity=_matrix({frozenset({"n-001", "n-003"}): 0.9, frozenset({"n-001", "n-002"}): 0.4}),
    )
    assert slate[0] == MergePair(
        doomed_node_id="n-001", survivor_node_id="n-003", link_weight=0, turn_cooccurrence=0, similarity=0.9
    )


def test_a_link_weight_tie_is_broken_by_similarity() -> None:
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-002", 3), ("n-001", "n-003", 3), ("n-001", "n-004", 1)),
        turn_links={},
        similarity=_matrix({frozenset({"n-001", "n-003"}): 0.6, frozenset({"n-001", "n-002"}): 0.2}),
    )
    assert slate[0].survivor_node_id == "n-003"
    assert slate[0].link_weight == 3
    assert slate[0].similarity == 0.6


def test_a_tie_on_both_criteria_breaks_on_node_id() -> None:
    """Three peers equally linked and equally similar: the lowest id wins."""
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-002", 2), ("n-001", "n-003", 2), ("n-001", "n-004", 2)),
        turn_links={},
        similarity=_matrix(
            {
                frozenset({"n-001", "n-002"}): 0.5,
                frozenset({"n-001", "n-003"}): 0.5,
                frozenset({"n-001", "n-004"}): 0.5,
            }
        ),
    )
    assert slate[0].survivor_node_id == "n-002"


def test_similarity_is_asked_only_about_the_candidates_tied_at_the_top_weight() -> None:
    """ "Similarity breaks ties" has to be true of the code, not just the docstring.

    Seven candidates, two of them tied at the top co-occurrence count. Exactly those two
    are scored -- which is also what keeps a node elsewhere in the scope that has
    never been dreamed (and whose similarity is therefore undefined) from
    failing a pair the ledger already decided.
    """
    asked: list[str] = []

    def similarity(first: Node, second: Node) -> float:
        asked.append(second.node_id)
        return 0.0

    nodes = _valued(*[(f"n-{index:03d}", float(index)) for index in range(1, 8)])
    merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-004", 5), ("n-001", "n-006", 5), ("n-001", "n-002", 4)),
        turn_links={},
        similarity=similarity,
    )
    assert asked == ["n-004", "n-006"]


def test_the_survivor_is_the_higher_value_half_of_the_pair() -> None:
    """Direction is decided here: the leftover is folded into the live concept."""
    nodes = _valued(("n-001", 0.1), ("n-002", 7.5), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes, pressure=1, cooccurrence=_shared(("n-001", "n-002", 1)), turn_links={}, similarity=_matrix({})
    )
    pair = slate[0]
    values = {valued.node_id: valued.value for valued in nodes}
    assert pair.doomed_node_id == "n-001"
    assert pair.survivor_node_id == "n-002"
    assert values[pair.survivor_node_id] > values[pair.doomed_node_id]
    assert pair.render() == "n-001 INTO n-002"
    assert pair.node_ids == frozenset({"n-001", "n-002"})


def test_every_doomed_node_is_the_lowest_value_node_left_when_its_turn_comes() -> None:
    """The doomed half always comes off the bottom of the value order.

    ``n-001`` takes the well-linked ``n-006`` as its peer, so the second doomed
    node is ``n-002`` -- the lowest value still unconsumed -- and not ``n-006``.
    """
    nodes = _valued(*[(f"n-{index:03d}", float(index)) for index in range(1, 7)])
    slate = merge_slate(
        nodes,
        pressure=2,
        cooccurrence=_shared(("n-001", "n-006", 9), ("n-002", "n-005", 3)),
        turn_links={},
        similarity=_matrix({}),
    )
    assert [pair.doomed_node_id for pair in slate] == ["n-001", "n-002"]
    assert [pair.survivor_node_id for pair in slate] == ["n-006", "n-005"]


def test_a_value_tie_breaks_on_node_id() -> None:
    """Equal values must order by id, so the doomed set is the same on every run."""
    nodes = _valued(("n-004", 1.0), ("n-002", 1.0), ("n-003", 1.0), ("n-001", 1.0), ("n-005", 9.0))
    slate = merge_slate(nodes, pressure=2, cooccurrence=_shared(), turn_links={}, similarity=_matrix({}))
    assert [pair.doomed_node_id for pair in slate] == ["n-001", "n-003"]


def test_no_node_appears_in_two_pairs() -> None:
    nodes = _valued(*[(f"n-{index:03d}", float(index)) for index in range(1, 11)])
    slate = merge_slate(
        nodes,
        pressure=3,
        cooccurrence=_shared(("n-001", "n-002", 4), ("n-001", "n-003", 4), ("n-002", "n-003", 4)),
        turn_links={},
        similarity=_matrix({}),
    )
    named = [node_id for pair in slate for node_id in pair.node_ids]
    assert len(slate) == 3
    assert len(named) == len(set(named)) == 6


def test_the_slate_is_byte_identical_across_two_runs() -> None:
    nodes = _valued(*[(f"n-{index:03d}", float(index % 4)) for index in range(1, 13)])
    edges = _shared(("n-004", "n-008", 2), ("n-001", "n-011", 2), ("n-002", "n-007", 1))
    turns = _turns(("n-003", "n-010", 2), ("n-003", "n-012", 2))
    similarity = _matrix({frozenset({"n-004", "n-008"}): 0.7})
    first = merge_slate(nodes, pressure=4, cooccurrence=edges, turn_links=turns, similarity=similarity)
    second = merge_slate(nodes, pressure=4, cooccurrence=edges, turn_links=turns, similarity=similarity)
    assert first == second


def test_a_scope_too_small_to_pair_is_a_hard_failure() -> None:
    with pytest.raises(ValueError, match="cannot bring this scope under its node budget"):
        merge_slate(
            _valued(("n-001", 1.0), ("n-002", 2.0), ("n-003", 3.0)),
            pressure=2,
            cooccurrence=_shared(),
            turn_links={},
            similarity=_matrix({}),
        )


def test_negative_pressure_is_rejected() -> None:
    with pytest.raises(ValueError, match="cannot be negative"):
        merge_slate(_valued(("n-001", 1.0)), pressure=-1, cooccurrence=_shared(), turn_links={}, similarity=_matrix({}))


def test_the_same_node_given_twice_is_rejected() -> None:
    duplicated = [*_valued(("n-001", 1.0)), *_valued(("n-001", 2.0)), *_valued(("n-002", 3.0))]
    with pytest.raises(ValueError, match="same node twice"):
        merge_slate(duplicated, pressure=1, cooccurrence=_shared(), turn_links={}, similarity=_matrix({}))


def test_a_cooccurrence_naming_a_node_outside_the_slate_is_simply_never_looked_up() -> None:
    """The ledger answers for the whole scope; the slate reads only its own nodes.

    ``n-777`` outscores ``n-002`` nine to one and is not in *nodes*, so it is
    never a candidate -- which is what lets ``dream`` pass
    ``LedgerStore.cooccurrence`` straight through without filtering its result.
    """
    slate = merge_slate(
        _valued(("n-001", 0.1), ("n-002", 0.2)),
        pressure=1,
        cooccurrence=_shared(("n-001", "n-777", 9), ("n-001", "n-002", 1)),
        turn_links={},
        similarity=_matrix({}),
    )
    assert slate[0].node_ids == frozenset({"n-001", "n-002"})
    assert slate[0].link_weight == 1


# ------------------------------------------ the turn tiebreak (amendment B)


def test_with_nothing_shared_a_shared_turn_beats_a_closer_embedding() -> None:
    """Amendment B's case, exactly: no shared entry, turns one way, the vector another.

    ``n-001`` shares a turn with ``n-002`` and embeds closer to ``n-003``. The
    turn wins, because sharing a turn is a record of what was said together
    while similarity is a measurement of what reads alike -- and reading alike is
    how "fix the platform, never downgrade" got folded into ``#245``.
    """
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links=_turns(("n-001", "n-002", 1)),
        similarity=_matrix({frozenset({"n-001", "n-003"}): 0.99, frozenset({"n-001", "n-002"}): 0.05}),
    )
    assert slate[0].survivor_node_id == "n-002"
    assert slate[0].link_weight == 0
    assert slate[0].turn_cooccurrence == 1
    assert slate[0].similarity == pytest.approx(0.05)


def test_a_shared_entry_outranks_a_shared_turn() -> None:
    """Co-occurrence first: one claim naming both beats merely being said together."""
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-003", 1)),
        turn_links=_turns(("n-001", "n-002", 9)),
        similarity=_matrix({}),
    )
    assert slate[0].survivor_node_id == "n-003"
    assert slate[0].link_weight == 1
    # The turn count recorded is the chosen pair's own, not the loser's 9.
    assert slate[0].turn_cooccurrence == 0


def test_a_link_weight_tie_is_broken_by_shared_turns_before_similarity() -> None:
    """Two candidates sharing equally: the turn decides and similarity is not reached."""
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(("n-001", "n-002", 2), ("n-001", "n-003", 2)),
        turn_links=_turns(("n-001", "n-003", 3)),
        similarity=_matrix({frozenset({"n-001", "n-002"}): 0.98}),
    )
    assert slate[0].survivor_node_id == "n-003"
    assert slate[0].turn_cooccurrence == 3


def test_a_turn_tie_falls_through_to_similarity_then_to_the_node_id() -> None:
    """The full ladder, with the top two rungs deliberately level."""
    nodes = _valued(("n-001", 0.1), ("n-002", 0.2), ("n-003", 0.3), ("n-004", 0.4))
    by_similarity = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links=_turns(("n-001", "n-002", 2), ("n-001", "n-003", 2)),
        similarity=_matrix({frozenset({"n-001", "n-003"}): 0.4, frozenset({"n-001", "n-002"}): 0.1}),
    )
    assert by_similarity[0].survivor_node_id == "n-003"

    by_id = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links=_turns(("n-001", "n-002", 2), ("n-001", "n-003", 2)),
        similarity=_matrix({}),
    )
    assert by_id[0].survivor_node_id == "n-002"


def test_similarity_is_asked_only_about_the_candidates_still_tied_on_turns() -> None:
    """The ladder has to be true of the code: a candidate the turns beat is never scored."""
    asked: list[str] = []

    def similarity(first: Node, second: Node) -> float:
        asked.append(second.node_id)
        return 0.0

    nodes = _valued(*[(f"n-{index:03d}", float(index)) for index in range(1, 6)])
    merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links=_turns(("n-001", "n-003", 2), ("n-001", "n-005", 2), ("n-001", "n-002", 1)),
        similarity=similarity,
    )
    assert asked == ["n-003", "n-005"]


def test_a_turn_map_naming_a_node_outside_the_scope_is_never_looked_up() -> None:
    """``dream`` hands over the whole scope's turn counts without filtering them."""
    slate = merge_slate(
        _valued(("n-001", 0.1), ("n-002", 0.2)),
        pressure=1,
        cooccurrence=_shared(),
        turn_links=_turns(("n-001", "n-777", 9)),
        similarity=_matrix({}),
    )
    assert slate[0].node_ids == frozenset({"n-001", "n-002"})
    assert slate[0].turn_cooccurrence == 0


# ==================================================================== pair_key


def test_pair_key_puts_the_lower_id_first_whichever_way_it_is_asked() -> None:
    """One orientation, so a caller and the slate cannot key a pair two ways.

    Undirected on purpose: which way a pair is written is not a fact about how
    strongly the ledger ties the two concepts together, and a fold is symmetric.
    This is the orientation ``LedgerStore.turn_cooccurrence`` keys its own result
    with, so a pair keyed the other way round reads as zero rather than failing.
    """
    assert pair_key("n-002", "n-001") == ("n-001", "n-002")
    assert pair_key("n-001", "n-002") == ("n-001", "n-002")
    assert pair_key("n-010", "n-009") == ("n-009", "n-010")


def test_a_turn_count_keyed_the_canonical_way_is_the_one_the_slate_reads() -> None:
    """The lookup and :func:`pair_key` agree, asserted rather than assumed."""
    nodes = _valued(("n-002", 0.1), ("n-001", 0.2))
    slate = merge_slate(
        nodes,
        pressure=1,
        cooccurrence=_shared(),
        turn_links={pair_key("n-002", "n-001"): 4},
        similarity=_matrix({}),
    )
    assert slate[0].turn_cooccurrence == 4


# ================================================= integration: 505 nodes at N=500


def test_five_hundred_and_five_nodes_at_n_500_yields_five_directed_pairs() -> None:
    """The plan's integration case, with real embeddings and real value arithmetic.

    Varied read counts and ages across 505 nodes, ``N=500``: the slate must be 5
    pairs naming 10 distinct nodes, every doomed node from the bottom of the
    value order, and every survivor the higher-value half.

    The co-occurrence table is built AFTER the values are computed, from the five
    lowest-value nodes to the five highest -- which is the arrangement the bounded
    pool made unreachable and amendment A exists to allow. So the assertion that
    carries the change is the last one: every survivor is a top-decile node.
    """
    motive = engineering_motive()
    types = ("service", "policy", "artifact", "cluster", "freshly-coined")
    nodes: list[Node] = [
        _node(
            f"n-{index:04d}",
            node_type=types[index % len(types)],
            read_count=index % 17,
            touched=NOW - timedelta(days=(index * 7) % 400),
            embedding_text=f"concept {index % 23} about the gateway and the chart",
        )
        for index in range(505)
    ]

    valued = [
        ValuedNode(
            node=node,
            value=node_value(
                node,
                degree=index % 5,
                now=NOW,
                weights=motive.value_weights,
                half_life_days=motive.recency_half_life_days,
                type_weight=motive.type_weight,
            ),
        )
        for index, node in enumerate(nodes)
    ]

    signal = pressure(len(nodes), motive.max_nodes)
    assert signal == 5

    by_value = sorted(valued, key=lambda item: (item.value, item.node_id))
    bottom = [item.node_id for item in by_value[:5]]
    top = [item.node_id for item in by_value[-5:]]
    edges = _shared(*[(doomed, survivor, 3) for doomed, survivor in zip(bottom, reversed(top), strict=True)])

    # Every doomed node here shares three entries with its peer, so the turn map is never
    # consulted -- which is the point: the primary criterion still decides alone.
    slate = merge_slate(valued, pressure=signal, cooccurrence=edges, turn_links={}, similarity=embedding_similarity)
    named = [node_id for pair in slate for node_id in pair.node_ids]
    values = {item.node_id: item.value for item in valued}

    assert len(slate) == 5
    assert len(named) == len(set(named)) == 10
    assert [pair.doomed_node_id for pair in slate] == bottom
    for pair in slate:
        assert values[pair.survivor_node_id] > values[pair.doomed_node_id]
        assert pair.link_weight == 3

    top_decile = {item.node_id for item in by_value[-51:]}
    assert {pair.survivor_node_id for pair in slate} <= top_decile

    # Merging away exactly `pressure` nodes lands the scope exactly on N.
    assert len(nodes) - len(slate) == motive.max_nodes
