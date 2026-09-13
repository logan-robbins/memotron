"""#251 unit 5: the motive contract, its two presets, and its prompt rendering.

The motive is what makes this "a policy over one graph" rather than "a graph per
persona", so the tests that matter are the ones pinning that separation:

* the extract block carries the extract rubric and NOT the dream rubric --
  telling an extractor to compress is how a claim arrives pre-summarised and
  unsupportable;
* the digest is a function of the motive's values, so a receipt can show that a
  run behaved differently because the policy changed;
* the two presets differ in what they KEEP, which is the whole argument for
  making refutation policy per-motive.

Stage 2's motive-neutrality is asserted in ``test_caveman_reconcile.py``, where
the prompt that must not contain a motive is actually built.
"""

from __future__ import annotations

import re

import pytest

from memotron.caveman.models import FactKind
from memotron.caveman.motive import (
    WEIGHTED_FACT_KINDS,
    CavemanMotive,
    ValueWeights,
    assistant_motive,
    engineering_motive,
    motive_digest,
    render_motive_block,
)
from memotron.caveman.render import (
    BREVITY_RULE,
    EXAMPLE_FACT_TEXTS,
    FACT_GLOSS,
    format_prompt_block,
    validate_fact_text,
)
from memotron.caveman.tokens import NODE_HEADER_TOKENS, estimate_tokens, scope_budget

_PRESETS = (engineering_motive, assistant_motive)


def _weights(**overrides: float) -> dict[FactKind, float]:
    base = {
        FactKind.IS: 0.9,
        FactKind.ATTRIBUTE: 0.7,
        FactKind.UNSURE: 0.5,
        FactKind.SUPERSEDED: 0.3,
    }
    for name, value in overrides.items():
        base[FactKind[name]] = value
    return base


def _motive(**overrides: object) -> CavemanMotive:
    fields: dict[str, object] = {
        "name": "test",
        "goal": "know the thing",
        "extract_rubric": ("keep the rules",),
        "dream_rubric": ("never drop a constraint",),
        "fact_kind_weight": _weights(),
        "value_weights": ValueWeights(),
    }
    fields.update(overrides)
    return CavemanMotive.model_validate(fields)


# ------------------------------------------------------------------- the presets


def test_the_engineering_preset_is_500_by_8_by_40() -> None:
    """``T`` is 40 because it is a paragraph guard, not a line budget.

    The number moved twice for one reason. The design's 15 rejected the
    document's own worked ``!`` line (80 characters, 20 estimator tokens); WP4's
    live run raised it to 20 and still lost a run to a 66-character line against
    a 60-character cap while the prompt stated the ceiling in characters three
    times. A model cannot count characters, so amendment A stopped asking: the
    prompt states ``BREVITY_RULE`` and ``T`` is set where only a paragraph trips
    it.
    """
    motive = engineering_motive()
    assert motive.max_nodes == 500
    assert motive.max_facts_per_node == 8
    assert motive.max_fact_tokens == 40


def test_the_engineering_preset_reads_a_1500_token_budget_over_100_lines() -> None:
    """The two read budgets are independent now that ``T`` no longer prices lines."""
    motive = engineering_motive()
    assert motive.read_line_budget == 100
    assert motive.read_token_budget == 1500


def test_the_read_token_budget_is_not_the_old_derived_product() -> None:
    """The regression this field exists to prevent.

    ``read_line_budget * max_fact_tokens`` was the token budget. With ``T`` at
    40 that product is 4,000 tokens for 100 lines -- three times what the design
    measured a 100-line read to cost -- so it would have stopped bounding
    anything a caller could plan a context window around.
    """
    motive = engineering_motive()
    assert motive.read_token_budget < motive.read_line_budget * motive.max_fact_tokens


def test_every_preset_takes_the_same_knn_similarity_floor() -> None:
    """0.25 in both, and the default, because it is a property of the vector space.

    Not a persona preference: two motives reading one scope must not disagree
    about which embedding neighbours are neighbours, so the presets do not set
    it and the field's default is the whole policy.
    """
    assert engineering_motive().knn_min_similarity == 0.25
    assert assistant_motive().knn_min_similarity == 0.25
    assert _motive().knn_min_similarity == 0.25


def test_the_knn_floor_sits_between_the_two_measured_bands() -> None:
    """The measurement the default is set from, asserted rather than only documented.

    2026-09-11 live run, four query reads over this repo's own episode against
    ``text-embedding-3``: a node the query was about scored 0.2534-0.3902, a
    node it was not scored 0.1003-0.2435. The gap is 0.0099 wide and the default
    is inside it. A floor outside that gap is either admitting noise or dropping
    the nodes a query is about -- both are the defect amendment C names -- so the
    gap is a test, and moving the default without a fresh measurement fails
    here.
    """
    unrelated_high, relevant_low = 0.2435, 0.2534
    assert unrelated_high < _motive().knn_min_similarity < relevant_low


@pytest.mark.parametrize("value", [-1.0, 0.0, 0.25, 1.0])
def test_the_knn_floor_accepts_the_whole_cosine_range(value: float) -> None:
    """Cosine similarity lives in ``[-1, 1]``, so a floor may be set anywhere in it.

    ``-1.0`` is the honest way to switch the filter off -- no vector can score
    below it -- and ``1.0`` admits only an exact-text match. Both are
    configurations, not errors.
    """
    assert _motive(knn_min_similarity=value).knn_min_similarity == value


@pytest.mark.parametrize("value", [-1.01, 1.01, 2.0])
def test_a_knn_floor_outside_the_cosine_range_is_rejected(value: float) -> None:
    """A floor no similarity can reach, or one every similarity clears, is a typo."""
    with pytest.raises(ValueError, match="validation error"):
        _motive(knn_min_similarity=value)


def test_the_digest_changes_when_the_knn_floor_changes() -> None:
    """The floor decides what a read retrieves, so it is part of the read's policy."""
    motive = engineering_motive()
    assert motive_digest(motive.model_copy(update={"knn_min_similarity": 0.3})) != motive_digest(motive)


def test_every_format_example_fits_the_engineering_preset() -> None:
    """The property the calibration exists to protect.

    A prompt that demonstrates a violation of its own contract is how a model
    gets blamed for a defect in the prompt, so every example fact the preset
    shows the model must validate under the preset's own ceiling.
    """
    motive = engineering_motive()
    for text in EXAMPLE_FACT_TEXTS:
        validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)


def test_the_engineering_preset_scope_budget_is_a_number_a_caller_can_assert() -> None:
    """Unit 5's integration check. Fixed, whatever the preset's own numbers are.

    The value has moved three times -- WP4's live calibration (``T`` 15 -> 20),
    #251 amendment A (header 8 -> 14, ``T`` a paragraph guard at 40) and
    amendment D (header 14 -> 16, plain ASCII with a rendered node id). The
    property did not: what the bound buys is that a whole scope has a stated
    worst-case cost at all, rather than one that grows with how much evidence
    exists. So the arithmetic is asserted against the preset and the constant,
    and the concrete number is pinned alongside it.
    """
    motive = engineering_motive()
    budget = scope_budget(motive.max_nodes, motive.max_facts_per_node, motive.max_fact_tokens)
    assert budget == motive.max_nodes * (NODE_HEADER_TOKENS + motive.max_facts_per_node * motive.max_fact_tokens)
    assert budget == 168000


def test_the_engineering_preset_keeps_refutations_for_90_days() -> None:
    motive = engineering_motive()
    assert motive.keep_superseded is True
    assert motive.superseded_ttl_days == 90


def test_the_assistant_preset_is_200_by_6() -> None:
    motive = assistant_motive()
    assert motive.max_nodes == 200
    assert motive.max_facts_per_node == 6
    assert motive.max_fact_tokens == 30


def test_the_assistant_preset_drops_refutations() -> None:
    """The contrast that justifies making this per-motive at all.

    'That used to be wrong' is worth 90 days to an engineer who would otherwise
    re-derive it and worth nothing to a general assistant. A global rule would
    be wrong for one of them.
    """
    assert assistant_motive().keep_superseded is False


def test_the_two_presets_disagree_about_what_is_worth_keeping() -> None:
    """Not just differently tuned -- differently scoped, with different rubrics."""
    engineering, assistant = engineering_motive(), assistant_motive()
    assert engineering.max_nodes != assistant.max_nodes
    assert engineering.keep_superseded != assistant.keep_superseded
    assert set(engineering.extract_rubric).isdisjoint(assistant.extract_rubric)
    assert motive_digest(engineering) != motive_digest(assistant)


@pytest.mark.parametrize("preset", _PRESETS, ids=lambda p: p.__name__)
def test_every_preset_is_constructible_and_named(preset: object) -> None:
    motive = preset()  # type: ignore[operator]
    assert motive.name
    assert motive.goal
    assert motive.extract_rubric
    assert motive.dream_rubric


@pytest.mark.parametrize("preset", _PRESETS, ids=lambda p: p.__name__)
def test_every_preset_weights_exactly_the_weightable_sigils(preset: object) -> None:
    motive = preset()  # type: ignore[operator]
    assert set(motive.fact_kind_weight) == set(WEIGHTED_FACT_KINDS)


# ---------------------------------------------------------- the weight invariant


def test_the_weightable_sigils_are_every_kind_except_a_constraint() -> None:
    assert frozenset(FactKind) - {FactKind.RULE} == WEIGHTED_FACT_KINDS
    assert FactKind.RULE not in WEIGHTED_FACT_KINDS


def test_weighting_a_constraint_is_rejected() -> None:
    """A persona must not be able to rank a hard constraint below an attribute.

    `rank` gives `!` a constant floor instead, so a weight for it would be
    either ignored or contradictory -- and an ignored policy field is worse than
    an absent one.
    """
    with pytest.raises(ValueError, match="must not weight"):
        _motive(fact_kind_weight={**_weights(), FactKind.RULE: 2.0})


def test_a_missing_fact_kind_weight_is_rejected() -> None:
    """Full coverage so `rank` can index directly.

    A missing kind would silently rank it at zero and drop a whole class of line
    out of every read, with nothing anywhere reporting it.
    """
    partial = _weights()
    del partial[FactKind.SUPERSEDED]
    with pytest.raises(ValueError, match="missing a weight for"):
        _motive(fact_kind_weight=partial)


def test_the_missing_weight_error_names_the_missing_kind() -> None:
    partial = _weights()
    del partial[FactKind.UNSURE]
    with pytest.raises(ValueError, match=r"missing a weight for: unsure"):
        _motive(fact_kind_weight=partial)


def test_value_weights_cannot_all_be_zero() -> None:
    """Every node would score identically, so the forced-merge slate would be arbitrary."""
    with pytest.raises(ValueError, match="cannot all be zero"):
        ValueWeights(recency=0.0, reads=0.0, degree=0.0, type=0.0)


def test_value_weights_default_to_a_usable_neutral() -> None:
    assert ValueWeights() == ValueWeights(recency=1.0, reads=1.0, degree=1.0, type=1.0)


def test_a_motive_forbids_an_invented_field() -> None:
    with pytest.raises(ValueError, match="Extra inputs are not permitted"):
        _motive(surprise="value")


def test_a_motive_is_frozen() -> None:
    """The digest describes something that then cannot change."""
    motive = engineering_motive()
    with pytest.raises(ValueError, match="frozen"):
        motive.max_nodes = 3  # type: ignore[misc]


def test_a_motive_is_changed_by_copy_which_is_how_the_demo_forces_pressure() -> None:
    tightened = engineering_motive().model_copy(update={"max_nodes": 3})
    assert tightened.max_nodes == 3
    assert engineering_motive().max_nodes == 500


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_nodes", 0),
        ("max_facts_per_node", 0),
        ("max_fact_tokens", 3),
        ("read_k", 0),
        ("read_line_budget", 0),
        ("recency_half_life_days", 0.0),
        ("superseded_ttl_days", 0),
    ],
)
def test_a_degenerate_budget_is_rejected(field: str, value: object) -> None:
    with pytest.raises(ValueError, match="validation error"):
        _motive(**{field: value})


def test_an_empty_extract_rubric_is_rejected() -> None:
    """A motive with nothing worth knowing cannot govern an extraction."""
    with pytest.raises(ValueError, match="validation error"):
        _motive(extract_rubric=())


def test_an_empty_dream_rubric_is_rejected() -> None:
    with pytest.raises(ValueError, match="validation error"):
        _motive(dream_rubric=())


def test_no_exclusions_is_valid() -> None:
    """A persona may legitimately exclude nothing."""
    assert _motive(extract_exclusions=()).extract_exclusions == ()


# ----------------------------------------------------------------- the rendering


def test_the_extract_block_carries_every_extract_rubric_bullet() -> None:
    motive = engineering_motive()
    block = render_motive_block(motive, stage="extract")
    for bullet in motive.extract_rubric:
        assert bullet in block


def test_the_extract_block_carries_no_dream_rubric_bullet() -> None:
    """Telling the extractor to compress is how a claim arrives unsupportable."""
    motive = engineering_motive()
    block = render_motive_block(motive, stage="extract")
    for bullet in motive.dream_rubric:
        assert bullet not in block


def test_the_extract_block_carries_every_exclusion() -> None:
    motive = engineering_motive()
    block = render_motive_block(motive, stage="extract")
    for bullet in motive.extract_exclusions:
        assert bullet in block


def test_the_dream_block_carries_every_dream_rubric_bullet_and_no_extract_bullet() -> None:
    motive = engineering_motive()
    block = render_motive_block(motive, stage="dream")
    for bullet in motive.dream_rubric:
        assert bullet in block
    for bullet in motive.extract_rubric:
        assert bullet not in block


def test_the_dream_block_states_every_budget_the_dreamer_enforces() -> None:
    """``N``, ``M`` and ``L`` are counts the model must respect and can hold to.

    All three, because the dreamer is the only enforcer of all three and a bound
    it is not told is a bound it will break. Fact LENGTH is a requirement rather
    than a count, so it is asked for and never measured.
    """
    block = render_motive_block(
        _motive(max_facts_per_node=6, max_fact_tokens=11, max_edge_types=7, max_nodes=123),
        stage="dream",
    )
    assert "KEEP AT MOST 6 FACTS ON ONE NODE." in block
    assert "KEEP AT MOST 7 DISTINCT RELATION TYPES IN THIS SCOPE." in block
    assert "KEEP AT MOST 123 NODES IN THIS SCOPE." in block
    assert BREVITY_RULE in block


def test_the_dream_block_asks_the_model_to_count_nothing_about_a_line() -> None:
    """The symptom amendment A names: the model was asked to count words/characters.

    So the numbers that used to be here are asserted ABSENT -- the token cap, its
    character equivalent, and the word budget that stood in for it. ``T`` is a
    paragraph guard the validator applies and the prompt never mentions.
    """
    block = render_motive_block(_motive(max_facts_per_node=6, max_fact_tokens=11), stage="dream")
    assert "11" not in block
    assert "44" not in block
    assert "WORDS" not in block
    assert "characters or words" in block  # says it explicitly, rather than only omitting it


def test_the_extract_block_does_NOT_state_L() -> None:
    """Extract emits claims, not lines. Line budgets are the dreamer's constraint."""
    block = render_motive_block(engineering_motive(), stage="extract")
    assert "KEEP AT MOST" not in block


@pytest.mark.parametrize("stage", ["extract", "dream"])
def test_both_blocks_name_the_motive_and_its_goal(stage: str) -> None:
    motive = engineering_motive()
    block = render_motive_block(motive, stage=stage)  # type: ignore[arg-type]
    assert f"MOTIVE: {motive.name}" in block
    assert motive.goal in block


@pytest.mark.parametrize("stage", ["extract", "dream"])
def test_both_blocks_rank_the_fact_kinds_with_their_renderer_gloss(stage: str) -> None:
    """The gloss comes from the renderer, so one wording serves both prompts."""
    motive = engineering_motive()
    block = render_motive_block(motive, stage=stage)  # type: ignore[arg-type]
    assert "FACT KINDS, MOST VALUABLE FIRST:" in block
    for kind in WEIGHTED_FACT_KINDS:
        assert FACT_GLOSS[kind] in block


def test_the_gloss_wording_is_identical_in_the_format_and_the_motive_block() -> None:
    """One source. Two different glosses for one kind in one prompt is a contradiction."""
    grammar = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    motive_block = render_motive_block(engineering_motive(), stage="dream")
    for sigil in WEIGHTED_FACT_KINDS:
        assert FACT_GLOSS[sigil] in grammar
        assert FACT_GLOSS[sigil] in motive_block


def test_line_kinds_render_in_descending_weight_order() -> None:
    motive = _motive(
        fact_kind_weight={
            FactKind.IS: 0.1,
            FactKind.IS: 0.2,
            FactKind.ATTRIBUTE: 0.9,
            FactKind.UNSURE: 0.5,
            FactKind.SUPERSEDED: 0.7,
        }
    )
    block = render_motive_block(motive, stage="extract")
    positions = [block.index(FACT_GLOSS[sigil]) for sigil in (FactKind.ATTRIBUTE, FactKind.SUPERSEDED, FactKind.UNSURE)]
    assert positions == sorted(positions)


def test_equal_weights_render_in_a_stable_declaration_order() -> None:
    """A prompt that varies between runs makes its own digest meaningless."""
    motive = _motive(fact_kind_weight=dict.fromkeys(WEIGHTED_FACT_KINDS, 0.5))
    first = render_motive_block(motive, stage="extract")
    reordered = _motive(fact_kind_weight=dict.fromkeys(sorted(WEIGHTED_FACT_KINDS, reverse=True), 0.5))
    assert render_motive_block(reordered, stage="extract") == first
    identity_at = first.index(FACT_GLOSS[FactKind.IS])
    refuted_at = first.index(FACT_GLOSS[FactKind.SUPERSEDED])
    assert identity_at < refuted_at


def test_the_dream_block_states_the_refutation_ttl_when_kept() -> None:
    block = render_motive_block(_motive(keep_superseded=True, superseded_ttl_days=90), stage="dream")
    assert "keep for 90 days" in block


def test_the_dream_block_says_keep_indefinitely_when_there_is_no_ttl() -> None:
    block = render_motive_block(_motive(keep_superseded=True, superseded_ttl_days=None), stage="dream")
    assert "keep indefinitely" in block


def test_the_dream_block_says_drop_when_refutations_are_not_kept() -> None:
    """`keep_superseded=False` wins over any ttl -- a stale ttl must not leak into the prompt."""
    block = render_motive_block(_motive(keep_superseded=False, superseded_ttl_days=90), stage="dream")
    assert "drop them" in block
    assert "90" not in block


def test_an_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError, match="stage must be one of"):
        render_motive_block(engineering_motive(), stage="reconcile")  # type: ignore[arg-type]


def test_a_block_with_no_exclusions_omits_the_header_entirely() -> None:
    """An empty 'NOT WORTH KNOWING:' header reads as 'everything is worth knowing'."""
    block = render_motive_block(_motive(extract_exclusions=()), stage="extract")
    assert "NOT WORTH KNOWING" not in block


# -------------------------------------------------------------------- the digest


def test_the_digest_is_stable_across_two_constructions() -> None:
    assert motive_digest(engineering_motive()) == motive_digest(engineering_motive())


def test_the_digest_is_a_full_sha256_hex() -> None:
    digest = motive_digest(engineering_motive())
    assert len(digest) == 64
    assert set(digest) <= set("0123456789abcdef")


def test_the_digest_changes_when_one_rubric_bullet_changes() -> None:
    """What lets a receipt stream show a run behaved differently because policy did."""
    motive = engineering_motive()
    edited = motive.model_copy(update={"dream_rubric": (*motive.dream_rubric[:-1], "a different bullet")})
    assert motive_digest(edited) != motive_digest(motive)


def test_the_digest_changes_when_a_budget_changes() -> None:
    motive = engineering_motive()
    assert motive_digest(motive.model_copy(update={"max_nodes": 3})) != motive_digest(motive)


def test_the_digest_changes_when_a_fact_kind_weight_changes() -> None:
    motive = engineering_motive()
    shifted = motive.model_copy(update={"fact_kind_weight": _weights(SUPERSEDED=0.31)})
    assert motive_digest(shifted) != motive_digest(motive)


def test_the_digest_ignores_dict_insertion_order() -> None:
    """A function of the motive's VALUES, not of how a dict happened to be built."""
    forward = _motive(fact_kind_weight=_weights())
    backward = _motive(fact_kind_weight=dict(reversed(list(_weights().items()))))
    assert motive_digest(forward) == motive_digest(backward)


def test_the_digest_ignores_rubric_wording_it_was_not_given() -> None:
    """The control: two motives differing in nothing digest the same."""
    assert motive_digest(_motive()) == motive_digest(_motive())


# ------------------------------------------------ the brevity rule, in one wording


def test_the_format_block_and_the_motive_block_state_ONE_brevity_rule() -> None:
    """One string, not two paraphrases.

    ``motive`` imports ``BREVITY_RULE`` from ``render`` -- the import that
    replaced the two duplicated ``CHARS_PER_WORD`` integers the old word budget
    needed, so the two blocks can no longer ask the model for different things.
    """
    motive = engineering_motive()
    grammar = format_prompt_block(max_facts=motive.max_facts_per_node, kinds=tuple(FactKind))
    assert BREVITY_RULE in grammar
    assert BREVITY_RULE in render_motive_block(motive, stage="dream")


def test_the_format_block_states_no_per_fact_length_number() -> None:
    """It cannot: ``format_prompt_block`` is not given one any more.

    ``L`` survives, because a fact count is a count the model can hold to and
    the validator rejects a ninth fact. What is gone is every per-fact measure:
    a token cap, its character equivalent, and the word budget that stood in for
    it. The word "tokens" still appears, in the sentence about identifiers being
    the highest-value tokens in the system -- which is why this asserts the
    SHAPE of a length rule rather than the bare word.
    """
    block = format_prompt_block(max_facts=4, kinds=tuple(FactKind))
    assert "At most 4 facts" in block
    assert re.search(r"[Aa]t most \d+ (tokens|characters|words)", block) is None
    assert "characters" not in block
    assert "WORDS" not in block
    assert "Count them" not in block


@pytest.mark.parametrize("preset", _PRESETS, ids=lambda p: p.__name__)
def test_every_format_example_is_inside_the_paragraph_guard(preset: object) -> None:
    """The guard exists for a paragraph, so a real fact must not be near it.

    An example at 90% of the cap would mean the cap is still a fact budget in
    disguise -- and a model shown one would write to that width. Three quarters
    rather than a half, because ``assistant_motive`` sets ``T`` to 30 and the
    examples are this repo's own real facts rather than shortened for the test.
    """
    motive = preset()  # type: ignore[operator]
    for text in EXAMPLE_FACT_TEXTS:
        validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)
        assert estimate_tokens(text) <= motive.max_fact_tokens * 3 // 4, f"{text!r} is close to the paragraph guard"
