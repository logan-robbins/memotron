"""#251 amendment D: the ONE place LLM-facing text is shaped.

Replaces ``tests/test_caveman_lines.py``, which tested a sigil grammar that no
longer exists. Every assertion here that survived the move is one that was about
something other than the alphabet: the header's fields, the fixed reading order,
the hedge rule, the paragraph guard, and the property that **every worked example
in a prompt obeys the very rules that prompt states**.

Three properties are the module's whole reason to exist, and each has a test
that fails loudly rather than drifting:

* **printable ASCII.** Nothing the renderer ADDS to content is outside it -- no
  middle dot, no arrow, no multiplication sign. Model-authored text passes
  through verbatim, so the check is over a node built from ASCII content.
* **node ids are rendered**, in every header and in the footer, because an id is
  what turns a block of text into somewhere the agent can go next.
* **one fixed reading order** -- rules, definition, attributes, relations,
  unsure -- so a block a reader skims has its kinds in the same place every time.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest

from memotron.caveman.errors import FactGrammarError
from memotron.caveman.models import Fact, FactKind, Node, Relation
from memotron.caveman.render import (
    ALIAS_PREFIX,
    BREVITY_RULE,
    EXAMPLE_FACT_TEXTS,
    FACT_GLOSS,
    FACT_LABEL,
    FACT_ORDER,
    FOOTER_PREFIX,
    HEDGE_WORDS,
    READ_FACT_KINDS,
    RELATION_ORDER,
    footer,
    format_prompt_block,
    render_fact,
    render_header,
    render_node,
    render_relation,
    sort_facts,
    validate_fact_text,
)
from memotron.caveman.tokens import NODE_HEADER_TOKENS, estimate_tokens

NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
GUARD = 40
"""The engineering preset's paragraph guard, so the tests read at a real budget."""


def fact(
    kind: FactKind = FactKind.ATTRIBUTE,
    text: str = "chat default is claude-haiku-4-5",
    *,
    key: str | None = None,
    entries: tuple[str, ...] = ("e-01",),
) -> Fact:
    return Fact(kind=kind, text=text, key=key, entry_ids=entries, first_seen=NOW, last_seen=NOW)


def node(
    *,
    name: str = "gateway",
    node_id: str = "n-001",
    type: str = "service",
    facts: tuple[Fact, ...] = (),
    aliases: tuple[str, ...] = (),
    touched: datetime = NOW,
) -> Node:
    return Node(
        node_id=node_id,
        scope="repo:jedai/memotron",
        name=name,
        aliases=aliases,
        type=type,
        facts=facts,
        ledger_key=node_id,
        created_at=NOW,
        last_touched_at=touched,
    )


def relation(
    *,
    relation_id: str = "r-001",
    source: str = "n-001",
    target: str = "n-004",
    type: str = "BLOCKED",
    claim: str = "the Host header was refused, #245 fixed the rewrite",
    until: str | None = None,
    entries: tuple[str, ...] = ("e-04",),
) -> Relation:
    return Relation(
        relation_id=relation_id,
        scope="repo:jedai/memotron",
        source_id=source,
        target_id=target,
        type=type,
        claim=claim,
        entry_ids=entries,
        until=until,
        first_seen=NOW,
        last_seen=NOW,
    )


# ------------------------------------------------------------- the header


def test_the_header_carries_the_five_fields_a_reader_needs() -> None:
    """What it is, what kind, how to reach the rest, how stale, how well evidenced."""
    assert render_header(node(), entries=7) == "gateway (service) [n-001] as of 2026-09-11, 7 entries"


def test_the_header_names_the_other_names_this_node_answers_to() -> None:
    """``aka`` is how a reader learns the spelling the dreamer kept."""
    rendered = render_header(node(aliases=("JedAI Gateway", "LiteLLM proxy")), entries=7)
    assert rendered.endswith(f"{ALIAS_PREFIX}JedAI Gateway, LiteLLM proxy")


def test_the_header_omits_the_aka_clause_when_there_is_nothing_to_say() -> None:
    assert ALIAS_PREFIX not in render_header(node(), entries=1)


def test_the_header_dates_itself_from_last_touched_at() -> None:
    """When the CONTENT was last written. Not ``dreamed_at``, which can be None."""
    older = node(touched=NOW - timedelta(days=3))
    assert "as of 2026-09-08" in render_header(older, entries=1)


def test_the_header_states_a_zero_entry_count_rather_than_omitting_it() -> None:
    """A node with no evidence is a real state after an erasure, and worth saying."""
    assert "0 entries" in render_header(node(), entries=0)


def test_the_header_refuses_a_negative_entry_count() -> None:
    with pytest.raises(ValueError, match="cannot have -1 ledger entries"):
        render_header(node(), entries=-1)


def test_the_header_renders_the_node_id_the_footer_calls_will_take() -> None:
    """Amendment D carries ids back so the agent can traverse; the id must be the
    one ``explain`` and ``neighbors`` accept, not a storage key that equals it today."""
    rendered = render_header(node(node_id="n-041"), entries=2)
    assert "[n-041]" in rendered
    assert f"{FOOTER_PREFIX}explain(n-041), neighbors(n-041)" == footer(["n-041"])


def test_the_header_costs_the_budgeted_header_tokens() -> None:
    """``scope_budget`` prices a scope at this number per node, aliases excluded."""
    longest = render_header(node(name="agent-memory", node_id="n-041", type="deployment"), entries=12)
    assert estimate_tokens(longest) == NODE_HEADER_TOKENS


# --------------------------------------------------------------- one fact


def test_a_fact_renders_under_its_kind_word() -> None:
    assert render_fact(fact(FactKind.RULE, "never hermetic")) == "rule: never hermetic"
    assert render_fact(fact(FactKind.IS, "a LiteLLM proxy")) == "is: a LiteLLM proxy"
    assert render_fact(fact(FactKind.UNSURE, "affinity is lost")) == "unsure: affinity is lost"


def test_an_attributes_key_replaces_its_kind_word_as_the_label() -> None:
    """``chat default: claude-haiku-4-5`` reads as the attribute it is."""
    labelled = fact(key="chat default", text="claude-haiku-4-5")
    assert render_fact(labelled) == "chat default: claude-haiku-4-5"


def test_a_keyless_attribute_falls_back_to_its_kind_word() -> None:
    assert render_fact(fact()) == "attribute: chat default is claude-haiku-4-5"


def test_evidence_is_rendered_only_when_more_than_one_entry_supports_a_fact() -> None:
    """``(x1)`` on every line would spend a token per fact to say nothing."""
    assert render_fact(fact(entries=("e-01",))).endswith("claude-haiku-4-5")
    assert render_fact(fact(entries=("e-01", "e-02"))).endswith(" (x2)")
    assert render_fact(fact(entries=("e-01", "e-02", "e-03"))).endswith(" (x3)")


def test_the_label_of_every_kind_is_its_own_word() -> None:
    """Written out rather than derived: a wire rename must not silently rename a reading."""
    assert set(FACT_LABEL) == set(FactKind)
    for kind, label in FACT_LABEL.items():
        assert label == kind.value


# ----------------------------------------------------------- one relation


def test_an_outgoing_relation_leads_with_its_type_then_the_other_end() -> None:
    rendered = render_relation(relation(), node_id="n-001", names={"n-004": "agent-memory"})
    assert rendered == "BLOCKED agent-memory [n-004]: the Host header was refused, #245 fixed the rewrite"


def test_an_incoming_relation_says_from_so_the_direction_is_a_word() -> None:
    """Direction is part of the belief: the node that blocked is not the one blocked."""
    rendered = render_relation(relation(), node_id="n-004", names={"n-001": "gateway"})
    assert rendered.startswith("from gateway [n-001] BLOCKED: ")


def test_an_until_marker_is_rendered_on_both_directions() -> None:
    marked = relation(until="#245")
    assert " until #245: " in render_relation(marked, node_id="n-001", names={"n-004": "agent-memory"})
    assert " until #245: " in render_relation(marked, node_id="n-004", names={"n-001": "gateway"})


def test_a_relation_to_a_node_outside_the_read_still_renders_its_id() -> None:
    """The normal case: a read emits a bounded set and an edge can point outside it.
    The id is the thing the agent can act on, so a rendering that omitted it would
    be a dead end."""
    assert render_relation(relation(), node_id="n-001", names={}) == (
        "BLOCKED [n-004]: the Host header was refused, #245 fixed the rewrite"
    )


def test_a_relation_carries_its_own_evidence_marker() -> None:
    reinforced = relation(entries=("e-04", "e-07"))
    assert render_relation(reinforced, node_id="n-001").endswith(" (x2)")


def test_rendering_a_relation_from_a_node_it_does_not_touch_raises() -> None:
    """Silently inverting the direction it reports would be worse than a loud caller defect."""
    with pytest.raises(ValueError, match="cannot be rendered from n-009"):
        render_relation(relation(), node_id="n-009")


# ------------------------------------------------------------ the ordering


def test_facts_sort_into_the_fixed_reading_order() -> None:
    """Rules, definition, attributes, unsure, history. Not the model's order."""
    scrambled = [
        fact(FactKind.UNSURE, "unsure one"),
        fact(FactKind.ATTRIBUTE, "attribute one"),
        fact(FactKind.RULE, "rule one"),
        fact(FactKind.SUPERSEDED, "old one"),
        fact(FactKind.IS, "is one"),
    ]
    assert [item.kind for item in sort_facts(scrambled)] == [
        FactKind.RULE,
        FactKind.IS,
        FactKind.ATTRIBUTE,
        FactKind.UNSURE,
        FactKind.SUPERSEDED,
    ]


def test_sorting_is_stable_within_one_kind() -> None:
    """The dreamer's own sequencing of two attributes is worth keeping."""
    first = fact(text="first")
    second = fact(text="second")
    assert sort_facts([first, second]) == (first, second)
    assert sort_facts([second, first]) == (second, first)


def test_sorting_nothing_is_nothing_and_sorting_twice_changes_nothing() -> None:
    assert sort_facts([]) == ()
    facts = [fact(FactKind.UNSURE, "u"), fact(FactKind.RULE, "r")]
    assert sort_facts(sort_facts(facts)) == sort_facts(facts)


def test_the_order_map_covers_every_kind_and_leaves_room_for_relations() -> None:
    """The hole at 3 is deliberate: an edge reads after a node's own attributes
    and before what it only suspects."""
    assert set(FACT_ORDER) == set(FactKind)
    assert FACT_ORDER[FactKind.ATTRIBUTE] < RELATION_ORDER < FACT_ORDER[FactKind.UNSURE]
    assert RELATION_ORDER not in set(FACT_ORDER.values())


def test_a_read_renders_every_kind_but_history() -> None:
    """Derived by subtraction, so a sixth kind is included the day it is declared."""
    assert frozenset(FactKind) - {FactKind.SUPERSEDED} == READ_FACT_KINDS


# -------------------------------------------------------------- whole node


def test_a_rendered_node_is_the_header_then_facts_then_relations_then_unsure() -> None:
    """The design document's worked example, rendered by the code that owns it."""
    rendered = render_node(
        node(
            aliases=("JedAI Gateway", "LiteLLM proxy"),
            facts=(
                fact(FactKind.UNSURE, "session affinity is lost about 1 call in 20, never reproduced"),
                fact(key="embedding", text="text-embedding-3, 3072 dims, selected explicitly", entries=("e-1", "e-2")),
                fact(FactKind.RULE, "real runs always via the gateway, never hermetic or a local stub"),
                fact(FactKind.IS, "a LiteLLM proxy fronting the JedAI models, not a model itself"),
                fact(key="chat default", text="claude-haiku-4-5"),
            ),
        ),
        [relation(until="#245")],
        entries=7,
        names={"n-004": "agent-memory"},
    )
    assert rendered.splitlines() == [
        "gateway (service) [n-001] as of 2026-09-11, 7 entries, aka JedAI Gateway, LiteLLM proxy",
        "rule: real runs always via the gateway, never hermetic or a local stub",
        "is: a LiteLLM proxy fronting the JedAI models, not a model itself",
        "embedding: text-embedding-3, 3072 dims, selected explicitly (x2)",
        "chat default: claude-haiku-4-5",
        "BLOCKED agent-memory [n-004] until #245: the Host header was refused, #245 fixed the rewrite",
        "unsure: session affinity is lost about 1 call in 20, never reproduced",
    ]


def test_a_rendered_node_never_shows_a_superseded_fact() -> None:
    """History is what the deep read is for; in a bounded block it would cost a
    slot a live fact could have had."""
    rendered = render_node(
        node(facts=(fact(FactKind.SUPERSEDED, "C4 returned 24 tools"), fact(FactKind.RULE, "never hermetic"))),
        entries=2,
    )
    assert "24 tools" not in rendered
    assert "rule: never hermetic" in rendered


def test_outgoing_relations_render_before_incoming_ones() -> None:
    rendered = render_node(
        node(),
        [
            relation(relation_id="r-002", source="n-008", target="n-001", type="DEPLOYS", claim="deploys it"),
            relation(),
        ],
        entries=1,
    )
    assert rendered.index("BLOCKED") < rendered.index("from [n-008] DEPLOYS")


def test_a_rendered_node_with_nothing_on_it_is_just_its_header() -> None:
    """A node reconcile created and the dreamer has not reached yet."""
    assert render_node(node(), entries=0) == render_header(node(), entries=0)


def test_relations_default_to_none_so_a_caller_holding_only_a_node_can_render_it() -> None:
    rendered = render_node(node(facts=(fact(FactKind.RULE, "never hermetic"),)), entries=1)
    assert rendered.endswith("rule: never hermetic")


# --------------------------------------------------------- printable ASCII


def test_a_node_built_from_ascii_content_renders_as_printable_ascii() -> None:
    """The rule the module exists to keep. Every symbol in a rendering is a symbol
    in a prompt, and a prompt that teaches an alphabet is one that can be misread."""
    rendered = render_node(
        node(
            aliases=("JedAI Gateway",),
            facts=(fact(FactKind.RULE, "never hermetic"), fact(key="chat default", text="claude-haiku-4-5")),
        ),
        [relation(until="#245")],
        entries=3,
        names={"n-004": "agent-memory"},
    )
    assert rendered.isascii()
    assert rendered.isprintable() or "\n" in rendered
    assert all(line.isprintable() for line in rendered.splitlines())


def test_the_footer_and_the_format_block_are_printable_ascii_too() -> None:
    assert footer(["n-001", "n-004"]).isascii()
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    assert block.isascii()
    assert all(line.isprintable() for line in block.splitlines())


@pytest.mark.parametrize("banned", ["→", "×", "·", "—"])
def test_no_invented_symbol_survives_anywhere_in_a_rendering(banned: str) -> None:
    """The four characters the old grammar and the old separators used."""
    rendered = render_node(
        node(facts=(fact(FactKind.RULE, "never hermetic"),)),
        [relation()],
        entries=1,
    )
    assert banned not in rendered
    assert banned not in format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    assert banned not in footer(["n-001"])


# --------------------------------------------------------------- the footer


def test_the_footer_names_both_calls_over_every_emitted_node() -> None:
    """One line for the whole read: the reader needs the verbs once, the ids vary."""
    assert footer(["n-001", "n-004"]) == "more: explain(n-001, n-004), neighbors(n-001, n-004)"


def test_the_footer_of_an_empty_read_still_names_its_calls() -> None:
    """Degenerate but well defined -- ``read`` does not render a footer at all when
    nothing was emitted, so this pins the function rather than the policy."""
    assert footer([]) == "more: explain(), neighbors()"


# ------------------------------------------------------- the text validator


def test_usable_fact_text_validates_silently() -> None:
    validate_fact_text("real runs always go through the gateway", max_fact_tokens=GUARD)


@pytest.mark.parametrize("text", ["", "   ", "\t"])
def test_blank_fact_text_is_refused(text: str) -> None:
    with pytest.raises(FactGrammarError, match="cannot be blank"):
        validate_fact_text(text, max_fact_tokens=GUARD)


@pytest.mark.parametrize("text", [" padded", "padded "])
def test_padded_fact_text_is_refused(text: str) -> None:
    with pytest.raises(FactGrammarError, match="leading or trailing whitespace"):
        validate_fact_text(text, max_fact_tokens=GUARD)


@pytest.mark.parametrize("text", ["two\nlines", "two\rlines"])
def test_a_newline_in_fact_text_is_refused(text: str) -> None:
    """The renderer emits one line per fact, so a newline would silently split a block."""
    with pytest.raises(FactGrammarError, match="cannot contain a newline"):
        validate_fact_text(text, max_fact_tokens=GUARD)


def test_a_paragraph_where_a_fact_belongs_is_refused() -> None:
    """The one failure a loose guard still has to stop."""
    with pytest.raises(FactGrammarError, match="over the 40-token guard"):
        validate_fact_text("x" * 200, max_fact_tokens=GUARD)


def test_the_guard_is_loose_enough_for_a_real_fact() -> None:
    """A guard a real fact approached would be a fact budget in disguise."""
    for text in EXAMPLE_FACT_TEXTS:
        validate_fact_text(text, max_fact_tokens=GUARD)
        assert estimate_tokens(text) < GUARD


@pytest.mark.parametrize("word", sorted(HEDGE_WORDS))
def test_every_hedge_word_is_actually_detected(word: str) -> None:
    """A hedge is what the ``unsure`` kind is for, so the word is redundant with it."""
    with pytest.raises(FactGrammarError, match="hedges with"):
        validate_fact_text(f"the gateway {word} refuses the Host header", max_fact_tokens=GUARD)


def test_hedge_detection_is_case_insensitive() -> None:
    with pytest.raises(FactGrammarError, match="hedges with"):
        validate_fact_text("Maybe the gateway refuses it", max_fact_tokens=GUARD)


def test_a_hedge_word_inside_a_longer_word_is_not_a_hedge() -> None:
    """Word boundaries, so ``appears`` does not reject ``disappearsance`` style text
    and ``might`` does not reject ``mightily-tested``."""
    validate_fact_text("the mightiest pod holds the appearance of order", max_fact_tokens=GUARD)


def test_the_hedge_error_names_the_alternative() -> None:
    """A rejection that does not say what to do instead is a rejection a model repeats."""
    with pytest.raises(FactGrammarError, match="set kind to 'unsure'"):
        validate_fact_text("the gateway maybe refuses it", max_fact_tokens=GUARD)


# ------------------------------------------------------- the prompt block


def test_the_format_block_names_every_fact_kind() -> None:
    """Generated by iterating the enum, so a sixth kind cannot be added silently."""
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    for kind in FactKind:
        assert f"  {kind.value}" in block
        assert FACT_GLOSS[kind] in block


def test_the_gloss_map_covers_the_enum_exactly() -> None:
    assert set(FACT_GLOSS) == set(FactKind)


def test_the_format_block_states_the_fact_count_it_was_given() -> None:
    assert "At most 4 facts" in format_prompt_block(max_facts=4, kinds=tuple(FactKind))
    assert "At most 12 facts" in format_prompt_block(max_facts=12, kinds=tuple(FactKind))


def test_the_format_block_names_every_hedge_word_it_will_reject() -> None:
    """A rule the validator enforces and the prompt does not state is a trap."""
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    for word in HEDGE_WORDS:
        assert word in block


def test_the_format_block_states_the_brevity_rule_and_no_length_number() -> None:
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    assert BREVITY_RULE in block
    assert re.search(r"[Aa]t most \d+ (tokens|characters|words)", block) is None


def test_the_format_block_states_no_json_shape_at_all() -> None:
    """Two descriptions of one shape is one too many, and the live run proved it.

    The block used to describe a fact as a record with a ``kind`` and an optional
    ``key``, while the global pass's answer contract asks for ``lines`` of plain
    strings (#251 amendment D's D0 bridge). The model read both and sent objects
    where strings were asked for. So the block describes what a belief IS, and
    the stage's own answer contract is the only statement of what to send.
    """
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    for shape in ("`key`", "null", "entry_ids", "UPPER_SNAKE", "[", "{"):
        assert shape not in block, f"the format block states a JSON shape: {shape!r}"


def test_the_format_block_points_at_the_answer_contract_for_the_shape() -> None:
    """Stating the absence is not enough: the model has to be told where to look."""
    block = format_prompt_block(max_facts=8, kinds=tuple(FactKind))
    assert "ANSWER CONTRACT" in block
    assert "Follow it literally" in block
    assert "Nothing above changes" in block


@pytest.mark.parametrize("text", EXAMPLE_FACT_TEXTS)
def test_every_worked_example_in_the_prompt_obeys_the_prompt(text: str) -> None:
    """The property that outlived the grammar. A prompt demonstrating a violation
    of its own contract is how a model gets blamed for a defect in the prompt --
    and two of the old examples were over the ceiling when first written."""
    validate_fact_text(text, max_fact_tokens=GUARD)
    assert text in format_prompt_block(max_facts=8, kinds=tuple(FactKind))


def test_the_examples_cover_more_than_one_kind_of_fact() -> None:
    """A single-shape example set teaches a single shape."""
    assert len(EXAMPLE_FACT_TEXTS) >= 4
    assert len(set(EXAMPLE_FACT_TEXTS)) == len(EXAMPLE_FACT_TEXTS)
