"""#251 unit 16: pure line ranking and the budget cut.

Everything in ``rank.py`` is a function of its arguments, so these tests assert
the two things a pure ranker can be wrong about: the ORDER and the CUT.

The load-bearing assertions are the ones about determinism. A ranker that puts
the right lines first but in an order that depends on how the caller assembled
its candidates makes ``ReadResult.contract_digest`` meaningless — the digest
would describe a stable contract over an unstable rendering. So the same
candidates shuffled every which way must produce byte-identical output, and
that is asserted over permutations rather than over one hopeful ordering.

Facts here are real :class:`~memotron.caveman.models.Fact`
values under the engineering motive's own ``T``, not hand-built dataclasses:
a fixture line that could not survive ``parse_line`` would be ranking something
the dreamer could never have written.
"""

from __future__ import annotations

import itertools
import math
import re
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest

from memotron.caveman.models import EDGE_TYPE_PATTERN, Fact, FactKind, Node, Relation
from memotron.caveman.motive import CavemanMotive, engineering_motive
from memotron.caveman.rank import (
    CONSTRAINT_FLOOR,
    DEDUPE_STOPWORDS,
    DEFAULT_TYPE_WEIGHT,
    DUPLICATE_JACCARD,
    EXACT_FLOOR,
    IDENTIFIER_JOINERS,
    QUERY_TOKEN_PATTERN,
    RELATION_WEIGHT,
    RankCandidate,
    RankedLine,
    content_tokens,
    cut_to_budget,
    dedupe_lines,
    is_identifier_token,
    line_score,
    query_tokens,
    rank_lines,
    reinforcement,
)
from memotron.caveman.render import render_fact, render_relation, validate_fact_text
from memotron.caveman.tokens import estimate_tokens

MOTIVE = engineering_motive()

T = MOTIVE.max_fact_tokens

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

SCOPE = "project:memotron"

ONE_PER_KIND: dict[FactKind, str] = {
    FactKind.RULE: "never hermetic, always through the gateway",
    FactKind.IS: "a LiteLLM proxy fronting the JedAI models",
    FactKind.ATTRIBUTE: "chat default is claude-haiku-4-5",
    FactKind.UNSURE: "session affinity is lost about 1 call in 20",
    FactKind.SUPERSEDED: "C4 returned 24 tools before #248",
}
"""One worked fact per kind, so a kind-ordering test names its fixture by kind."""

_KIND_OF_EXAMPLE = {text: kind for kind, text in ONE_PER_KIND.items()}


def _fact(text: str, *, kind: FactKind | None = None, entries: tuple[str, ...] = ("e-01",)) -> Fact:
    """A fact whose KIND is inferred from :data:`ONE_PER_KIND` when it is one of them.

    Inferring rather than requiring it keeps a kind-ordering test reading as
    ``_candidate(ONE_PER_KIND[FactKind.RULE])`` while an ad-hoc text -- most of
    the dedupe fixtures -- stays an attribute without saying so forty times.
    """
    resolved = kind if kind is not None else _KIND_OF_EXAMPLE.get(text, FactKind.ATTRIBUTE)
    return Fact(kind=resolved, text=text, entry_ids=entries, first_seen=NOW, last_seen=NOW)


def _node(*, node_id: str = "n-001", name: str = "gateway", type: str = "service") -> Node:
    """A node with no facts of its own. Only its id, name and type are ranked over.

    :class:`RankCandidate` takes the node and the belief separately -- a
    relation's two endpoints are two nodes and only one of them renders it -- so
    a fixture node here never needs the fact it is offered alongside.
    """
    return Node(
        node_id=node_id,
        scope=SCOPE,
        name=name,
        type=type,
        ledger_key=node_id,
        created_at=NOW,
        last_touched_at=NOW,
    )


def _candidate(
    text: str,
    *,
    kind: FactKind | None = None,
    entries: tuple[str, ...] = ("e-01",),
    similarity: float = 0.5,
    node_id: str = "n-001",
    node_name: str = "gateway",
    node_type: str = "service",
) -> RankCandidate:
    """One fact candidate, built the only way this module offers: through the renderer."""
    return RankCandidate.of_fact(
        _node(node_id=node_id, name=node_name, type=node_type),
        _fact(text, kind=kind, entries=entries),
        similarity=similarity,
    )


def _relation(
    *,
    claim: str = "Host header refused before #245, which fixed the rewrite",
    type: str = "BLOCKED",
    relation_id: str = "r-01",
    source_id: str = "n-001",
    target_id: str = "n-002",
    entries: tuple[str, ...] = ("e-01",),
    until: str | None = None,
) -> Relation:
    return Relation(
        relation_id=relation_id,
        scope=SCOPE,
        source_id=source_id,
        target_id=target_id,
        type=type,
        claim=claim,
        entry_ids=entries,
        until=until,
        first_seen=NOW,
        last_seen=NOW,
    )


def _relation_candidate(
    *,
    similarity: float = 0.5,
    node_id: str = "n-001",
    node_name: str = "gateway",
    node_type: str = "service",
    names: dict[str, str] | None = None,
    **relation_kwargs: object,
) -> RankCandidate:
    """One relation candidate, rendered from *node_id*'s end."""
    relation = _relation(**relation_kwargs)  # type: ignore[arg-type]
    return RankCandidate.of_relation(
        _node(node_id=node_id, name=node_name, type=node_type),
        relation,
        similarity=similarity,
        names=names,
    )


def _texts(ranked: Sequence[RankedLine]) -> list[str]:
    return [line.text for line in ranked]


def _jaccard_of(left: RankCandidate, right: RankCandidate) -> float:
    """Two candidates' content-token overlap, computed from the public tokeniser.

    Asserted in the dedupe tests so each one states WHERE its pair sits relative
    to :data:`~memotron.caveman.rank.DUPLICATE_JACCARD`. A test claiming "the
    same-kind rule is the only thing keeping both of these" is worth nothing
    unless it also shows the measurement would otherwise have merged them.
    """
    first, second = content_tokens(left.belief), content_tokens(right.belief)
    return len(first & second) / len(first | second)


# =============================================================== the fixtures


def test_every_fact_kind_has_a_fixture_the_helper_infers() -> None:
    """A guard on the fixtures themselves, iterating the enum rather than a list.

    The inference is what lets a kind-ordering test name its fixture by kind, so
    a sixth kind with no worked fact fails here rather than silently ranking as
    an attribute.
    """
    assert set(ONE_PER_KIND) == set(FactKind)
    for kind, text in ONE_PER_KIND.items():
        assert _fact(text).kind is kind


# ================================================ a candidate is fact or relation


def test_a_fact_candidate_carries_the_renderers_own_line() -> None:
    """``of_fact`` is the only construction, so the text cannot be a caller's paraphrase."""
    fact = _fact(ONE_PER_KIND[FactKind.ATTRIBUTE])
    candidate = RankCandidate.of_fact(_node(), fact, similarity=0.5)
    assert candidate.text == render_fact(fact)
    assert candidate.belief is fact
    assert candidate.content == fact.text
    assert candidate.node_id == "n-001"
    assert candidate.node_name == "gateway"
    assert candidate.node_type == "service"


def test_a_relation_candidate_renders_outgoing_from_its_source() -> None:
    relation = _relation()
    candidate = RankCandidate.of_relation(
        _node(node_id="n-001"),
        relation,
        similarity=0.5,
        names={"n-002": "agent-memory"},
    )
    assert candidate.text == "BLOCKED agent-memory [n-002]: Host header refused before #245, which fixed the rewrite"
    assert candidate.text == render_relation(relation, node_id="n-001", names={"n-002": "agent-memory"})
    assert candidate.content == relation.claim


def test_a_relation_candidate_renders_incoming_from_its_target() -> None:
    """The same edge, read from the other end, is a different line -- direction is the belief."""
    relation = _relation()
    candidate = RankCandidate.of_relation(
        _node(node_id="n-002", name="agent-memory", type="artifact"),
        relation,
        similarity=0.5,
        names={"n-001": "gateway"},
    )
    assert candidate.text.startswith("from gateway [n-001] BLOCKED: ")
    assert candidate.node_id == "n-002"


def test_a_relation_candidate_of_a_node_the_edge_does_not_touch_is_refused() -> None:
    """The renderer's fail-fast, reached through the constructor rather than around it."""
    with pytest.raises(ValueError, match="cannot be rendered from n-009"):
        RankCandidate.of_relation(_node(node_id="n-009"), _relation(), similarity=0.5)


def test_a_relation_candidates_kind_is_its_edge_type() -> None:
    assert _relation_candidate(type="DEPLOYS").kind == "DEPLOYS"


@pytest.mark.parametrize("kind", sorted(FactKind, key=lambda member: member.value))
def test_a_fact_candidates_kind_is_its_kind_word(kind: FactKind) -> None:
    candidate = _candidate(ONE_PER_KIND[kind])
    assert candidate.kind == kind
    assert candidate.kind == kind.value


def test_no_edge_type_can_ever_spell_a_fact_kind() -> None:
    """What makes one ``kind`` field safe for two vocabularies, asserted over both.

    Fact kinds are lowercase words and ``EDGE_TYPE_PATTERN`` demands a leading
    capital, so the two sets are disjoint by construction rather than by
    convention -- which is what stops a fact deduping against a relation.
    """
    pattern = re.compile(EDGE_TYPE_PATTERN)
    for kind in FactKind:
        assert pattern.fullmatch(kind.value) is None
        assert pattern.fullmatch(kind.value.upper()) is not None


def test_the_evidence_a_candidate_reports_is_the_beliefs_own_entry_count() -> None:
    assert _candidate(ONE_PER_KIND[FactKind.IS], entries=("e-01", "e-02", "e-03")).evidence == 3
    assert _relation_candidate(entries=("e-01", "e-02")).evidence == 2


# ================================================================ reinforcement


def test_reinforcement_is_one_plus_log1p_and_never_below_one() -> None:
    assert reinforcement(0) == pytest.approx(1.0)
    assert reinforcement(1) == pytest.approx(1.0 + math.log1p(1))
    assert reinforcement(3) == pytest.approx(1.0 + math.log1p(3))


def test_reinforcement_is_strictly_increasing_in_evidence() -> None:
    factors = [reinforcement(count) for count in range(40)]
    assert factors == sorted(factors)
    assert len(set(factors)) == len(factors)


def test_a_negative_evidence_count_is_a_caller_defect_and_raises() -> None:
    with pytest.raises(ValueError, match="evidence cannot be negative"):
        reinforcement(-1)


def test_a_reinforced_fact_outranks_its_unreinforced_twin_at_one_similarity() -> None:
    """The whole point: restating a belief makes the record stronger, and a read shows it."""
    once = _candidate(ONE_PER_KIND[FactKind.IS], entries=("e-01",), node_id="n-001", node_name="a-once")
    thrice = _candidate(
        ONE_PER_KIND[FactKind.IS],
        entries=("e-01", "e-02", "e-03"),
        node_id="n-002",
        node_name="z-thrice",
    )
    ranked = rank_lines([once, thrice], MOTIVE)
    assert [line.candidate.node_name for line in ranked] == ["z-thrice", "a-once"]
    assert line_score(thrice, MOTIVE) == pytest.approx(line_score(once, MOTIVE) * reinforcement(3) / reinforcement(1))


def test_no_amount_of_evidence_lifts_an_attribute_over_a_rule() -> None:
    """The floors are added after the multiplication, which is why this holds at all.

    A thousand entries is far past anything a real scope reaches, and the rule
    here is at similarity 0.0 on an unweighted type -- the worst case available.
    """
    heavy = _candidate(
        ONE_PER_KIND[FactKind.ATTRIBUTE],
        entries=tuple(f"e-{index:04d}" for index in range(1000)),
        similarity=1.0,
        node_id="n-002",
        node_name="heavy",
    )
    rule = _candidate(
        ONE_PER_KIND[FactKind.RULE],
        similarity=0.0,
        node_id="n-001",
        node_name="rule-holder",
        node_type="unheard-of",
    )
    ranked = rank_lines([heavy, rule], MOTIVE)
    assert [line.candidate.node_name for line in ranked] == ["rule-holder", "heavy"]
    assert line_score(heavy, MOTIVE) < CONSTRAINT_FLOOR


def test_no_amount_of_evidence_lifts_a_non_exact_hit_over_an_exact_one() -> None:
    heavy = _candidate(
        ONE_PER_KIND[FactKind.RULE],
        entries=tuple(f"e-{index:04d}" for index in range(1000)),
        similarity=1.0,
        node_id="n-other",
        node_name="a-heavy",
    )
    exact = _candidate(
        ONE_PER_KIND[FactKind.UNSURE],
        similarity=0.0,
        node_id="n-exact",
        node_name="z-exact",
    )
    ranked = rank_lines([heavy, exact], MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    assert [line.candidate.node_id for line in ranked] == ["n-exact", "n-other"]


def test_a_relation_takes_the_neutral_kind_weight_and_no_motive_preference() -> None:
    """A motive weights fact kinds; an edge type is not one, so there is nothing to weight."""
    candidate = _relation_candidate(similarity=0.4, node_type="service")
    expected = 0.4 * RELATION_WEIGHT * MOTIVE.type_weight["service"] * reinforcement(1)
    assert line_score(candidate, MOTIVE) == pytest.approx(expected)
    assert RELATION_WEIGHT == 1.0


def test_a_relation_never_takes_the_constraint_floor() -> None:
    """Only a ``rule`` fact does. An edge is a belief about a pair, not a prohibition."""
    assert line_score(_relation_candidate(type="RULE"), MOTIVE) < CONSTRAINT_FLOOR


def test_a_rule_outranks_a_relation_however_well_evidenced_the_relation_is() -> None:
    relation = _relation_candidate(
        entries=tuple(f"e-{index:04d}" for index in range(50)),
        similarity=1.0,
        node_id="n-002",
        node_name="a-edge",
    )
    rule = _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.0, node_id="n-001", node_name="z-rule")
    ranked = rank_lines([relation, rule], MOTIVE)
    assert [line.candidate.node_id for line in ranked] == ["n-001", "n-002"]


def test_a_relation_and_a_fact_rank_in_one_order_under_one_budget() -> None:
    """One sort key over both kinds of belief, which is the whole of amendment D here."""
    candidates = [
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.2, node_id="n-001", node_name="gateway"),
        _relation_candidate(similarity=0.9, node_id="n-001", node_name="gateway"),
        _candidate(ONE_PER_KIND[FactKind.UNSURE], similarity=0.1, node_id="n-001", node_name="gateway"),
    ]
    ranked = rank_lines(candidates, MOTIVE)
    assert [line.candidate.kind for line in ranked] == ["rule", "BLOCKED", "unsure"]


def test_a_relations_rendered_head_is_inside_the_token_cost_the_budget_reads() -> None:
    """``RankedLine.tokens`` is over the rendered line, so an edge's head is paid for."""
    candidate = _relation_candidate(names={"n-002": "agent-memory"})
    line = rank_lines([candidate], MOTIVE)[0]
    assert line.tokens == estimate_tokens(candidate.text)
    assert line.tokens > estimate_tokens(candidate.content)


# ==================================================================== line_score


def test_a_constraint_carries_the_floor_and_no_kind_weight() -> None:
    """A rule is absent from ``fact_kind_weight`` by design, so it multiplies by 1.0."""
    candidate = _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.5, node_type="service")
    expected = CONSTRAINT_FLOOR + 0.5 * MOTIVE.type_weight["service"] * reinforcement(1)
    assert line_score(candidate, MOTIVE) == pytest.approx(expected)


def test_a_weighted_kind_multiplies_similarity_by_its_motive_weight() -> None:
    candidate = _candidate(ONE_PER_KIND[FactKind.IS], similarity=0.4, node_type="service")
    expected = 0.4 * MOTIVE.fact_kind_weight[FactKind.IS] * MOTIVE.type_weight["service"] * reinforcement(1)
    assert line_score(candidate, MOTIVE) == pytest.approx(expected)


def test_a_type_the_motive_never_mentions_scores_neutrally_not_zero() -> None:
    """An unmentioned type means "no opinion" -- zero would delete the node from every read."""
    candidate = _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=0.6, node_type="unheard-of")
    expected = 0.6 * MOTIVE.fact_kind_weight[FactKind.ATTRIBUTE] * DEFAULT_TYPE_WEIGHT * reinforcement(1)
    assert line_score(candidate, MOTIVE) == pytest.approx(expected)
    assert DEFAULT_TYPE_WEIGHT == 1.0


def test_the_floor_is_finite_so_two_constraints_stay_comparable() -> None:
    """``inf`` would collapse the whole rule set to one score and one nan-producing subtraction."""
    strong = _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.9)
    weak = _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.1)
    difference = line_score(strong, MOTIVE) - line_score(weak, MOTIVE)
    assert difference > 0.0
    assert difference == pytest.approx(0.8 * MOTIVE.type_weight["service"] * reinforcement(1))


# ===================================================== order: a rule outranks all


@pytest.mark.parametrize("other", sorted(set(FactKind) - {FactKind.RULE}, key=lambda s: s.value))
def test_a_rule_outranks_every_other_kind_at_the_worst_possible_similarity(other: FactKind) -> None:
    """Similarity 0.0 for the constraint, 1.0 for its rival, and the constraint still wins."""
    candidates = [
        _candidate(ONE_PER_KIND[other], similarity=1.0, node_id="n-002", node_name="agent-memory"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.0, node_id="n-001", node_name="gateway"),
    ]
    ranked = rank_lines(candidates, MOTIVE)
    assert ranked[0].candidate.belief.kind is FactKind.RULE


def test_every_constraint_precedes_every_non_constraint_in_a_mixed_set() -> None:
    candidates = [
        _candidate(ONE_PER_KIND[FactKind.IS], similarity=0.99, node_name="a-node"),
        _candidate("keep identifiers verbatim on every line", kind=FactKind.RULE, similarity=0.01, node_name="z-node"),
        _candidate(ONE_PER_KIND[FactKind.SUPERSEDED], similarity=0.98, node_name="b-node"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.02, node_name="y-node"),
    ]
    ranked = rank_lines(candidates, MOTIVE)
    kinds = [line.candidate.belief.kind for line in ranked]
    assert kinds[:2] == [FactKind.RULE, FactKind.RULE]
    assert FactKind.RULE not in kinds[2:]


def test_the_constraint_set_still_orders_itself_by_similarity() -> None:
    candidates = [
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.2, node_name="low"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.8, node_name="high"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.5, node_name="mid"),
    ]
    ranked = rank_lines(candidates, MOTIVE)
    assert [line.candidate.node_name for line in ranked] == ["high", "mid", "low"]


# ====================================================== order: the exact floor


def test_the_exact_floor_is_above_the_constraint_floor() -> None:
    """The whole arithmetic of amendment C, asserted as a relation between constants.

    Stated here rather than trusted, because every ordering below follows from
    it: if the two floors were ever set the other way round, an exact hit would
    rank under somebody else's constraint and every assertion in this section
    would be describing an accident.
    """
    assert EXACT_FLOOR > CONSTRAINT_FLOOR
    assert EXACT_FLOOR - CONSTRAINT_FLOOR > 1.0


def test_an_exact_nodes_attribute_outranks_another_nodes_constraint() -> None:
    """The measured symptom: ``read('C4')`` led with somebody else's ``!`` line.

    Worst case on purpose — the exact node's line is the motive's third-ranked
    kind at the lowest similarity, and the rival is a ``!`` at 1.0 on the
    motive's best-weighted type.
    """
    exact = _candidate(
        ONE_PER_KIND[FactKind.ATTRIBUTE],
        similarity=0.0,
        node_id="n-exact",
        node_name="z-C4 memory server",
        node_type="artifact",
    )
    other = _candidate(
        ONE_PER_KIND[FactKind.RULE],
        similarity=1.0,
        node_id="n-other",
        node_name="a-gateway",
        node_type="policy",
    )
    ranked = rank_lines([other, exact], MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    assert [line.candidate.node_id for line in ranked] == ["n-exact", "n-other"]


def test_within_an_exact_node_a_constraint_still_precedes_an_attribute() -> None:
    """The exact floor lifts a whole node; it does not flatten the node's own order."""
    attribute = _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=0.9, node_id="n-exact")
    constraint = _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.1, node_id="n-exact")
    ranked = rank_lines([attribute, constraint], MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    assert [line.candidate.belief.kind for line in ranked] == [FactKind.RULE, FactKind.ATTRIBUTE]


def test_the_exact_set_leads_then_the_scopes_other_constraints_then_the_rest() -> None:
    """Three tiers in one sort, which is what one additive key buys."""
    candidates = [
        _candidate(ONE_PER_KIND[FactKind.IS], similarity=0.9, node_id="n-other", node_name="other"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.9, node_id="n-other", node_name="other"),
        _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=0.1, node_id="n-exact", node_name="exact"),
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.1, node_id="n-exact", node_name="exact"),
    ]
    ranked = rank_lines(candidates, MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    assert [(line.candidate.node_id, line.candidate.belief.kind) for line in ranked] == [
        ("n-exact", FactKind.RULE),
        ("n-exact", FactKind.ATTRIBUTE),
        ("n-other", FactKind.RULE),
        ("n-other", FactKind.IS),
    ]


def test_every_line_of_an_exact_node_is_lifted_not_only_its_best() -> None:
    """A node is exact, not a line: all six of its kinds precede everything else."""
    candidates = [
        _candidate(text, similarity=0.5, node_id="n-other", node_name="other") for text in ONE_PER_KIND.values()
    ]
    candidates += [
        _candidate(text, similarity=0.5, node_id="n-exact", node_name="exact") for text in ONE_PER_KIND.values()
    ]
    ranked = rank_lines(candidates, MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    node_ids = [line.candidate.node_id for line in ranked]
    assert node_ids == ["n-exact"] * len(ONE_PER_KIND) + ["n-other"] * len(ONE_PER_KIND)


def test_two_exact_nodes_order_among_themselves_by_the_ordinary_score() -> None:
    """The floor is a constant, so it cancels: inside the exact set nothing changed."""
    high = _candidate(ONE_PER_KIND[FactKind.IS], similarity=0.9, node_id="n-a", node_name="a")
    low = _candidate(ONE_PER_KIND[FactKind.IS], similarity=0.2, node_id="n-b", node_name="b")
    ranked = rank_lines([low, high], MOTIVE, exact_node_ids=frozenset({"n-a", "n-b"}))
    assert [line.candidate.node_id for line in ranked] == ["n-a", "n-b"]


def test_an_exact_id_naming_no_candidate_changes_nothing() -> None:
    """A seed whose every line lost the candidate set is not an ordering event."""
    candidates = [_candidate(text, similarity=0.5) for text in ONE_PER_KIND.values()]
    assert rank_lines(candidates, MOTIVE, exact_node_ids=frozenset({"n-absent"})) == rank_lines(candidates, MOTIVE)


def test_a_brief_style_call_passing_no_exact_ids_is_unchanged() -> None:
    """The default is empty, so ``brief`` gets byte-identical output to before amendment C.

    Asserted over the scores as well as the order: an implementation that added
    the floor to everything would keep the order and silently move every number
    a receipt digest is taken over.
    """
    candidates = [
        _candidate(text, similarity=0.4 + index / 10, node_id=f"n-{index:03d}", node_name=f"node-{index}")
        for index, text in enumerate(ONE_PER_KIND.values())
    ]
    default = rank_lines(candidates, MOTIVE)
    explicit = rank_lines(candidates, MOTIVE, exact_node_ids=frozenset())
    assert default == explicit
    assert [line.score for line in default] == [line_score(line.candidate, MOTIVE) for line in default]
    assert max(line.score for line in default) < EXACT_FLOOR


def test_line_score_takes_the_exact_flag_so_the_score_is_computed_once() -> None:
    """``rank_lines`` adds no arithmetic of its own; it only decides who is exact."""
    candidate = _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=0.5, node_id="n-exact")
    assert line_score(candidate, MOTIVE, exact=True) == pytest.approx(line_score(candidate, MOTIVE) + EXACT_FLOOR)
    ranked = rank_lines([candidate], MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    assert ranked[0].score == pytest.approx(line_score(candidate, MOTIVE, exact=True))


def test_the_exact_floor_survives_every_permutation_of_a_mixed_set() -> None:
    """Determinism, asserted the way this module asserts it everywhere else."""
    candidates = [
        _candidate(ONE_PER_KIND[FactKind.RULE], similarity=0.7, node_id="n-other", node_name="other"),
        _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=0.7, node_id="n-exact", node_name="exact"),
        _candidate(ONE_PER_KIND[FactKind.UNSURE], similarity=0.7, node_id="n-exact", node_name="exact"),
    ]
    expected = rank_lines(candidates, MOTIVE, exact_node_ids=frozenset({"n-exact"}))
    for permutation in itertools.permutations(candidates):
        assert rank_lines(list(permutation), MOTIVE, exact_node_ids=frozenset({"n-exact"})) == expected


# ============================================================ order: kind weight


def test_kind_weight_orders_equal_similarity_facts_as_the_motive_ranks_them() -> None:
    """Engineering weights: is 0.9 > attribute 0.7 > unsure 0.5 > superseded 0.3."""
    candidates = [
        _candidate(ONE_PER_KIND[kind], similarity=0.5, node_type="artifact")
        for kind in (FactKind.SUPERSEDED, FactKind.ATTRIBUTE, FactKind.IS, FactKind.UNSURE)
    ]
    ranked = rank_lines(candidates, MOTIVE)
    assert [line.candidate.belief.kind for line in ranked] == [
        FactKind.IS,
        FactKind.ATTRIBUTE,
        FactKind.UNSURE,
        FactKind.SUPERSEDED,
    ]


def test_the_two_presets_rank_the_same_candidates_differently() -> None:
    """A motive is a policy, and a policy that changed nothing would not be one.

    The assistant motive weights ``is`` 1.0 / ``attribute`` 0.8 / ``unsure`` 0.4
    / ``superseded`` 0.1; engineering weights ``unsure`` 0.5 / ``superseded``
    0.3. So the gap between what is unproven and what is history widens under the
    engineer, which is a difference in policy showing up as a difference in
    output rather than as a comment.
    """
    from memotron.caveman.motive import assistant_motive

    unsure = _candidate(ONE_PER_KIND[FactKind.UNSURE], similarity=0.5, node_type="untyped")
    history = _candidate(ONE_PER_KIND[FactKind.SUPERSEDED], similarity=0.5, node_type="untyped")
    engineering_gap = line_score(unsure, MOTIVE) - line_score(history, MOTIVE)
    assistant_gap = line_score(unsure, assistant_motive()) - line_score(history, assistant_motive())
    assert engineering_gap < assistant_gap
    assert [line.candidate.belief.kind for line in rank_lines([history, unsure], MOTIVE)] == [
        FactKind.UNSURE,
        FactKind.SUPERSEDED,
    ]


def _tied_candidates() -> list[RankCandidate]:
    """Six candidates whose scores tie exactly, in three name groups."""
    return [
        _candidate("beta line about the proxy", similarity=0.5, node_name="beta", node_type="untyped"),
        _candidate("alpha line about the proxy", similarity=0.5, node_name="alpha", node_type="untyped"),
        _candidate("zulu line about the proxy", similarity=0.5, node_name="alpha", node_type="untyped"),
        _candidate("alpha line about the chart", similarity=0.5, node_name="zulu", node_type="untyped"),
        _candidate("mike line about the chart", similarity=0.5, node_name="beta", node_type="untyped"),
        _candidate("echo line about the chart", similarity=0.5, node_name="zulu", node_type="untyped"),
    ]


def test_ties_break_on_node_name_then_line_text() -> None:
    ranked = rank_lines(_tied_candidates(), MOTIVE)
    assert [(line.candidate.node_name, line.text) for line in ranked] == [
        ("alpha", "attribute: alpha line about the proxy"),
        ("alpha", "attribute: zulu line about the proxy"),
        ("beta", "attribute: beta line about the proxy"),
        ("beta", "attribute: mike line about the chart"),
        ("zulu", "attribute: alpha line about the chart"),
        ("zulu", "attribute: echo line about the chart"),
    ]


def test_every_permutation_of_a_tied_set_ranks_identically() -> None:
    """720 orderings, one output. Stable sort alone would not give this."""
    expected = _texts(rank_lines(_tied_candidates(), MOTIVE))
    for permutation in itertools.permutations(_tied_candidates()):
        assert _texts(rank_lines(list(permutation), MOTIVE)) == expected


def test_ranking_an_empty_candidate_set_is_empty_not_an_error() -> None:
    """An empty scope is a legitimate read, not a defect."""
    assert rank_lines([], MOTIVE) == ()


# =================================================================== the cut


def test_the_line_budget_truncates_and_reports_saturation() -> None:
    candidates = [
        _candidate(f"attribute number {index} of the batch", similarity=0.9 - index / 100, node_name=f"n{index}")
        for index in range(10)
    ]
    ranked = rank_lines(candidates, MOTIVE)
    kept, saturated = cut_to_budget(ranked, line_budget=4, token_budget=10_000)
    assert len(kept) == 4
    assert saturated is True
    assert _texts(kept) == _texts(ranked)[:4]


def test_a_budget_that_fits_everything_is_not_saturated() -> None:
    ranked = rank_lines(_tied_candidates(), MOTIVE)
    kept, saturated = cut_to_budget(ranked, line_budget=100, token_budget=10_000)
    assert len(kept) == len(ranked)
    assert saturated is False


def test_a_budget_smaller_than_the_constraint_set_keeps_only_the_top_constraints() -> None:
    """The exact case the design calls out: two ``!`` sets and a small budget."""
    constraints = [
        _candidate("never hermetic; always via the gateway", kind=FactKind.RULE, similarity=0.9, node_name="gateway"),
        _candidate("only undated aliases on the proxy", kind=FactKind.RULE, similarity=0.7, node_name="proxy"),
        _candidate("fix the platform; never downgrade a run", kind=FactKind.RULE, similarity=0.3, node_name="policy"),
    ]
    others = [
        _candidate(ONE_PER_KIND[FactKind.IS], similarity=1.0, node_name="aaa"),
        _candidate(ONE_PER_KIND[FactKind.ATTRIBUTE], similarity=1.0, node_name="aab"),
    ]
    ranked = rank_lines([*others, *constraints], MOTIVE)
    kept, saturated = cut_to_budget(ranked, line_budget=2, token_budget=10_000)
    assert saturated is True
    assert [line.candidate.node_name for line in kept] == ["gateway", "proxy"]
    assert all(line.candidate.belief.kind is FactKind.RULE for line in kept)


def test_the_token_budget_is_respected_to_the_token() -> None:
    """Budget set to the exact sum of the first three lines: three in, nothing over."""
    candidates = [
        _candidate(f"attribute number {index} of the batch", similarity=0.9 - index / 100, node_name=f"n{index}")
        for index in range(8)
    ]
    ranked = rank_lines(candidates, MOTIVE)
    exact = sum(line.tokens for line in ranked[:3])
    kept, saturated = cut_to_budget(ranked, line_budget=100, token_budget=exact)
    assert len(kept) == 3
    assert sum(line.tokens for line in kept) == exact
    assert saturated is True

    one_short, _ = cut_to_budget(ranked, line_budget=100, token_budget=exact - 1)
    assert len(one_short) == 2


def test_the_token_cut_stops_rather_than_skipping_to_a_smaller_line() -> None:
    """The kept set is always a PREFIX of the ranked set. Asserted, not assumed."""
    ranked = rank_lines(
        [
            _candidate("a long attribute line that costs plenty", similarity=0.9, node_name="aaa"),
            _candidate("tiny", similarity=0.8, node_name="bbb"),
        ],
        MOTIVE,
    )
    first, second = ranked
    assert first.tokens > second.tokens
    kept, saturated = cut_to_budget(ranked, line_budget=100, token_budget=first.tokens - 1)
    assert kept == ()
    assert saturated is True


def test_tokens_count_the_rendered_line_including_its_label() -> None:
    """The label and its separator are characters the estimator buckets by.

    Budgeting the bare fact text would under-count every line by its label,
    which on a 100-line read is several lines' worth of drift.
    """
    text = "chat default claude-haiku-4-5 v1"
    assert len(text) == 32
    ranked = rank_lines([_candidate(text)], MOTIVE)
    line = ranked[0]
    assert line.text == f"attribute: {text}"
    assert line.tokens == estimate_tokens(line.text)
    assert line.tokens > estimate_tokens(text)


def test_a_motive_missing_a_kind_weight_cannot_be_constructed() -> None:
    """Why ``line_score`` may index ``line_kind_weight`` directly instead of defaulting.

    A gap would silently score that kind at zero and drop a whole class of line
    out of every read, so the motive refuses to exist rather than the ranker
    papering over it.
    """
    with pytest.raises(ValueError, match="fact_kind_weight is missing a weight for"):
        CavemanMotive(
            name="broken",
            goal="rank with a hole in the policy",
            extract_rubric=("anything",),
            dream_rubric=("anything",),
            fact_kind_weight={FactKind.IS: 1.0},
            value_weights=MOTIVE.value_weights,
        )


@pytest.mark.parametrize(("line_budget", "token_budget"), [(0, 40), (40, 0), (-1, 40), (40, -1)])
def test_a_budget_below_one_is_a_misconfiguration_and_raises(line_budget: int, token_budget: int) -> None:
    """Answering a zero budget with "nothing, saturated" would read as an empty scope."""
    ranked = rank_lines([_candidate(ONE_PER_KIND[FactKind.IS])], MOTIVE)
    with pytest.raises(ValueError, match="must be at least 1"):
        cut_to_budget(ranked, line_budget=line_budget, token_budget=token_budget)


# ================================================================== integration


def _forty_candidates() -> list[RankCandidate]:
    """40 facts over 6 nodes, every kind represented, all inside the motive's ``T``."""
    nodes = (
        ("n-001", "gateway", "service"),
        ("n-002", "agent-memory", "artifact"),
        ("n-003", "chart", "artifact"),
        ("n-004", "c4", "cluster"),
        ("n-005", "alias-rule", "policy"),
        ("n-006", "probe", "artifact"),
    )
    bodies = (
        (FactKind.RULE, "never hermetic, always through the gateway"),
        (FactKind.IS, "a LiteLLM proxy fronting the JedAI models"),
        (FactKind.ATTRIBUTE, "chat default is claude-haiku-4-5"),
        (FactKind.UNSURE, "session affinity is lost about 1 call in 20"),
        (FactKind.SUPERSEDED, "C4 returned 24 tools before #248"),
        (FactKind.ATTRIBUTE, "embedding is text-embedding-3 at 3072 dims"),
    )
    candidates: list[RankCandidate] = []
    for index in range(40):
        node_id, node_name, node_type = nodes[index % len(nodes)]
        kind, body = bodies[index % len(bodies)]
        candidates.append(
            _candidate(
                body,
                kind=kind,
                similarity=1.0 - index / 50,
                node_id=node_id,
                node_name=node_name,
                node_type=node_type,
            )
        )
    return candidates


def test_forty_candidates_over_six_nodes_cut_to_ten_lines() -> None:
    """The plan's integration case: 40 candidates, ``line_budget=10``, byte-identical twice."""
    candidates = _forty_candidates()
    assert len({candidate.node_id for candidate in candidates}) == 6

    token_budget = 10 * T
    ranked = rank_lines(candidates, MOTIVE)
    kept, saturated = cut_to_budget(ranked, line_budget=10, token_budget=token_budget)

    assert len(kept) == 10
    assert sum(line.tokens for line in kept) <= token_budget
    assert saturated is True

    again, again_saturated = cut_to_budget(rank_lines(candidates, MOTIVE), line_budget=10, token_budget=token_budget)
    assert _texts(again) == _texts(kept)
    assert again_saturated is saturated
    assert [line.score for line in again] == [line.score for line in kept]


def test_the_cut_of_forty_candidates_leads_with_constraints() -> None:
    candidates = _forty_candidates()
    kept, _ = cut_to_budget(rank_lines(candidates, MOTIVE), line_budget=10, token_budget=10 * T)
    constraints = sum(1 for candidate in candidates if candidate.belief.kind is FactKind.RULE)
    assert constraints > 0
    assert all(line.candidate.belief.kind is FactKind.RULE for line in kept[:constraints])


def test_every_kept_fact_still_validates_under_the_guard_that_ranked_it() -> None:
    kept, _ = cut_to_budget(rank_lines(_forty_candidates(), MOTIVE), line_budget=10, token_budget=10 * T)
    for line in kept:
        validate_fact_text(line.candidate.belief.text, max_fact_tokens=T)
        assert line.text == render_fact(line.candidate.belief)


# ================================================================ query tokens


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("the gateway", ("the", "gateway")),
        ("what do I know about #246", ("what", "do", "I", "know", "about", "#246")),
        # The whole point of the pattern: one high-value key, not three worthless ones.
        ("claude-haiku-4-5", ("claude-haiku-4-5",)),
        ("text-embedding-3 at 3072d", ("text-embedding-3", "at", "3072d")),
        ("chart 0.4.1", ("chart", "0.4.1")),
        ("repo:jedai/memotron", ("repo", "jedai/memotron")),
        ("the gateway refuses my Host header?", ("the", "gateway", "refuses", "my", "Host", "header")),
        ("agent-memory, the C4 server", ("agent-memory", "the", "C4", "server")),
    ],
)
def test_a_query_tokenises_into_exact_match_search_keys(query: str, expected: tuple[str, ...]) -> None:
    assert query_tokens(query) == expected


def test_punctuation_and_whitespace_both_separate_tokens() -> None:
    assert query_tokens("gateway,\tchart\n(probe) 'ledger'") == ("gateway", "chart", "probe", "ledger")


def test_a_possessive_yields_the_name_a_node_is_actually_stored_under() -> None:
    """``gateway's`` is not a node name; ``gateway`` is."""
    assert query_tokens("the gateway's Host header")[1] == "gateway"


def test_a_leading_hash_is_kept_because_the_ledger_keys_identifiers_verbatim() -> None:
    assert query_tokens("#245 and #246") == ("#245", "and", "#246")


def test_a_hash_inside_a_word_does_not_start_a_new_token() -> None:
    """Only a LEADING ``#`` is part of a key, so ``a#b`` is two keys, not one."""
    assert query_tokens("a#b") == ("a", "#b")


def test_tokens_keep_their_spelling_and_their_order() -> None:
    assert query_tokens("Gateway C4 LiteLLM") == ("Gateway", "C4", "LiteLLM")


def test_a_repeated_token_is_deduped_case_insensitively_first_spelling_winning() -> None:
    """Both lookups a key feeds are case-insensitive, so two spellings are one key."""
    assert query_tokens("C4 c4 C4 gateway GATEWAY") == ("C4", "gateway")


@pytest.mark.parametrize("query", ["", "   ", "\n", "?!,", "-", "--"])
def test_a_query_with_no_search_keys_yields_no_tokens_rather_than_raising(query: str) -> None:
    """``read`` owns the blank-query rejection; putting it here too would be two rules."""
    assert query_tokens(query) == ()


def test_the_pattern_is_the_published_rule_the_tokeniser_applies() -> None:
    """Public so a caller can see WHY a token exists, not only that it does."""
    assert QUERY_TOKEN_PATTERN.findall("#246 claude-haiku-4-5") == ["#246", "claude-haiku-4-5"]


# =============================================================== content tokens


@pytest.mark.parametrize(
    "token",
    [
        "#245",
        "#246",
        "31",
        "0",
        "0.4.1",
        "3072d",
        "claude-haiku-4-5",
        "text-embedding-3",
        "agent-memory",
        "jedai/memotron",
        "v1.2",
    ],
)
def test_a_coded_identifier_is_recognised_as_one(token: str) -> None:
    """A leading ``#``, an embedded digit, or a joiner. Any one is enough."""
    assert is_identifier_token(token) is True


@pytest.mark.parametrize("token", ["gateway", "header", "refused", "hermetic", "the", "LiteLLM"])
def test_an_ordinary_word_is_not_an_identifier(token: str) -> None:
    assert is_identifier_token(token) is False


def test_every_joiner_makes_a_token_an_identifier() -> None:
    """Iterating the constant rather than a list, so a fifth joiner breaks a test."""
    for joiner in IDENTIFIER_JOINERS:
        assert is_identifier_token(f"left{joiner}right") is True


def test_content_tokens_casefold_and_drop_the_stopwords() -> None:
    assert content_tokens(_fact(": the Host header is refused by the gateway")) == frozenset(
        {"host", "header", "refused", "gateway"}
    )


def test_content_tokens_keep_an_identifier_whole_rather_than_splitting_it() -> None:
    """The whole reason the query pattern is reused: one high-value key, not five."""
    assert content_tokens(_fact(": chat default claude-haiku-4-5 at #245")) == frozenset(
        {"chat", "default", "claude-haiku-4-5", "#245"}
    )


def test_content_tokens_read_the_text_and_never_the_key() -> None:
    """A label is how ONE node chose to file an attribute.

    Two nodes filing one measurement under ``chat default`` and ``default
    model`` are still restating it once too often, so the key is not part of
    what makes two facts one fact.
    """
    labelled = Fact(
        kind=FactKind.ATTRIBUTE,
        text="claude-haiku-4-5 at #245",
        key="chat default",
        entry_ids=("e-01",),
        first_seen=NOW,
        last_seen=NOW,
    )
    assert content_tokens(labelled) == frozenset({"claude-haiku-4-5", "#245"})


def test_no_polarity_or_modality_word_is_a_stopword() -> None:
    """Removing one would let ``never hermetic`` dedupe against ``hermetic``."""
    assert DEDUPE_STOPWORDS.isdisjoint({"not", "no", "never", "none", "only", "must", "always", "cannot"})


def test_the_stopword_set_is_small_and_frozen() -> None:
    """A blacklist is what ``render.py`` deliberately does not have; this is not one."""
    assert isinstance(DEDUPE_STOPWORDS, frozenset)
    assert len(DEDUPE_STOPWORDS) < 30


# ================================================================ dedupe_lines


def test_the_same_line_on_two_nodes_is_kept_once() -> None:
    """The measured symptom: one superseded fact, emitted twice, from two nodes."""
    refutation = "pre-#245 the gateway refused the Host header outright"
    ranked = rank_lines(
        [
            _candidate(refutation, similarity=0.9, node_id="n-001", node_name="gateway"),
            _candidate(refutation, similarity=0.4, node_id="n-002", node_name="agent-memory"),
        ],
        MOTIVE,
    )
    kept, dropped = dedupe_lines(ranked)
    assert [line.node_id for line in kept] == ["n-001"]
    assert [line.node_id for line in dropped] == ["n-002"]


def test_one_edge_reached_from_both_ends_is_folded_to_the_higher_ranked_end() -> None:
    """The commonest fold amendment D produces, and why the seam needs no special case.

    A seed offers every edge that touches it; a 1-hop neighbour offers the edges
    that touch a seed. So one relation is a candidate twice -- rendered outgoing
    from the seed and ``from`` the other end -- and the two rendered LINES differ
    while the belief does not. The fold compares the claim, so the reader is told
    it once.
    """
    relation = _relation(source_id="n-001", target_id="n-002")
    names = {"n-001": "gateway", "n-002": "agent-memory"}
    outgoing = RankCandidate.of_relation(_node(node_id="n-001"), relation, similarity=0.9, names=names)
    incoming = RankCandidate.of_relation(
        _node(node_id="n-002", name="agent-memory", type="artifact"),
        relation,
        similarity=0.45,
        names=names,
    )
    assert outgoing.text != incoming.text

    kept, dropped = dedupe_lines(rank_lines([outgoing, incoming], MOTIVE))
    assert [line.node_id for line in kept] == ["n-001"]
    assert [line.node_id for line in dropped] == ["n-002"]
    assert _jaccard_of(outgoing, incoming) == pytest.approx(1.0)


def test_two_edges_of_different_types_stating_one_claim_are_two_beliefs() -> None:
    """The same-kind clause over edge types: the TYPE is part of what an edge asserts."""
    blocked = _relation_candidate(type="BLOCKED", similarity=0.9, node_id="n-001", node_name="a-gateway")
    deploys = _relation_candidate(
        type="DEPLOYS",
        relation_id="r-02",
        similarity=0.8,
        node_id="n-003",
        node_name="z-chart",
        source_id="n-003",
        target_id="n-004",
    )
    assert _jaccard_of(blocked, deploys) == pytest.approx(1.0)
    kept, dropped = dedupe_lines(rank_lines([blocked, deploys], MOTIVE))
    assert len(kept) == 2
    assert dropped == ()


def test_a_relation_and_a_fact_with_identical_wording_are_two_beliefs() -> None:
    """A fact kind can never equal an edge type, so these cannot fold into each other."""
    claim = "Host header refused before #245, which fixed the rewrite"
    fact = _candidate(claim, kind=FactKind.ATTRIBUTE, similarity=0.9, node_id="n-001", node_name="a-gateway")
    edge = _relation_candidate(claim=claim, similarity=0.8, node_id="n-002", node_name="z-memory")
    assert _jaccard_of(fact, edge) == pytest.approx(1.0)
    kept, dropped = dedupe_lines(rank_lines([fact, edge], MOTIVE))
    assert len(kept) == 2
    assert dropped == ()


def test_a_reinforced_belief_folds_against_its_own_restatement() -> None:
    """What comparing the claim rather than the rendered line buys.

    The rendered line of a reinforced belief carries ``(x3)``, whose ``x3`` is a
    token :func:`is_identifier_token` accepts. Comparing rendered lines would put
    that in one side's identifier set and not the other's, and the fold would
    refuse the one pair it most obviously should make.
    """
    text = "chat default is claude-haiku-4-5"
    reinforced = _candidate(text, entries=("e-01", "e-02", "e-03"), similarity=0.9, node_id="n-001", node_name="a")
    once = _candidate(text, entries=("e-09",), similarity=0.4, node_id="n-002", node_name="z")
    assert "(x3)" in reinforced.text
    assert "(x" not in once.text

    kept, dropped = dedupe_lines(rank_lines([reinforced, once], MOTIVE))
    assert [line.node_id for line in kept] == ["n-001"]
    assert [line.node_id for line in dropped] == ["n-002"]


def test_a_rule_and_a_superseded_fact_saying_the_same_words_are_two_facts() -> None:
    """Different KINDS: a standing rule and what used to be true are different claims.

    These two measure above the threshold together, so the kind rule is the only
    thing keeping both -- which is why the measurement is asserted here rather
    than left implied.
    """
    rule = _candidate("the gateway refuses a Host header", kind=FactKind.RULE, similarity=0.9, node_id="n-001")
    history = _candidate("the gateway refused a Host header", kind=FactKind.SUPERSEDED, similarity=0.8, node_id="n-002")
    assert _jaccard_of(rule, history) >= DUPLICATE_JACCARD
    kept, dropped = dedupe_lines(rank_lines([rule, history], MOTIVE))
    assert len(kept) == 2
    assert dropped == ()


def test_two_facts_of_one_kind_saying_different_things_both_survive() -> None:
    """The control on the fold: it must collapse restatements, not neighbours."""
    kept, dropped = dedupe_lines(
        rank_lines(
            [
                _candidate("the Host header was refused before #245", similarity=0.9, node_id="n-001"),
                _candidate("the chart deploys the MCP server, #240", similarity=0.8, node_id="n-001"),
            ],
            MOTIVE,
        )
    )
    assert len(kept) == 2
    assert dropped == ()


def test_three_phrasings_of_one_fact_collapse_to_one_not_to_two_pairs() -> None:
    """Comparing against the KEPT set rather than the previous line is what gives this."""
    kept, dropped = dedupe_lines(
        rank_lines(
            [
                _candidate(
                    "pre-#245 gateway refused Host header outright",
                    kind=FactKind.SUPERSEDED,
                    similarity=0.9,
                    node_id="n-001",
                ),
                _candidate(
                    "gateway refused the Host header before #245",
                    kind=FactKind.SUPERSEDED,
                    similarity=0.8,
                    node_id="n-002",
                ),
                _candidate(
                    "the gateway refused a Host header, pre-#245",
                    kind=FactKind.SUPERSEDED,
                    similarity=0.7,
                    node_id="n-003",
                ),
            ],
            MOTIVE,
        )
    )
    assert [line.node_id for line in kept] == ["n-001"]
    assert [line.node_id for line in dropped] == ["n-002", "n-003"]


def test_two_different_attributes_of_one_node_are_nowhere_near_the_threshold() -> None:
    """A caveman line is almost all content words, so a false positive needs real overlap."""
    lines = [
        _candidate("chat default claude-haiku-4-5", similarity=0.9, node_id="n-001"),
        _candidate("embedding text-embedding-3 at 3072 dimensions", similarity=0.8, node_id="n-001"),
    ]
    assert _jaccard_of(lines[0], lines[1]) < DUPLICATE_JACCARD
    kept, dropped = dedupe_lines(rank_lines(lines, MOTIVE))
    assert len(kept) == 2
    assert dropped == ()


def test_two_lines_with_no_content_tokens_at_all_are_not_duplicates() -> None:
    """``0/0`` is not 1.0: nothing has been SHOWN to be the same, so both are kept."""
    kept, dropped = dedupe_lines(
        rank_lines(
            [
                _candidate("the it is", similarity=0.9, node_id="n-001"),
                _candidate("that was a", similarity=0.8, node_id="n-002"),
            ],
            MOTIVE,
        )
    )
    assert content_tokens(kept[0].candidate.belief) == frozenset()
    assert len(kept) == 2
    assert dropped == ()


def test_the_kept_set_keeps_its_rank_order() -> None:
    """Dedupe removes; it never reorders."""
    ranked = rank_lines(_forty_candidates(), MOTIVE)
    kept, dropped = dedupe_lines(ranked)
    assert _texts(kept) == [line.text for line in ranked if line not in dropped]


def test_kept_and_dropped_partition_the_input_exactly() -> None:
    ranked = rank_lines(_forty_candidates(), MOTIVE)
    kept, dropped = dedupe_lines(ranked)
    assert len(kept) + len(dropped) == len(ranked)
    assert set(kept).isdisjoint(dropped)


def test_deduping_the_same_ranked_set_twice_gives_the_same_answer() -> None:
    """Pure: no clock, no store, no order the caller cannot see."""
    ranked = rank_lines(_forty_candidates(), MOTIVE)
    first_kept, first_dropped = dedupe_lines(ranked)
    second_kept, second_dropped = dedupe_lines(ranked)
    assert _texts(first_kept) == _texts(second_kept)
    assert _texts(first_dropped) == _texts(second_dropped)


def test_deduping_an_empty_ranked_set_is_two_empty_tuples() -> None:
    assert dedupe_lines(()) == ((), ())


def test_the_threshold_is_the_published_number() -> None:
    """0.6, stated once, so a read's receipt count and this test cannot disagree."""
    assert DUPLICATE_JACCARD == 0.6
