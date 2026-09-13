"""#251 unit 4: the records that cross a seam, and the four LLM wire contracts.

Two properties are structural rather than incidental and are asserted by
enumerating the module rather than by listing names, so a record added later
cannot quietly opt out:

* ``extra="forbid"`` on every model -- the fail-fast the stages depend on. A
  model that invents a field has misread the contract.
* ``frozen=True`` on every model -- the stores own their state, so a ``Node``
  handed back by ``GraphStore`` is a value and mutating it must not be
  mistakable for a write.

The response-schema tests pin the rules that live HERE rather than in a stage:
a rule the response can check on its own. Rules needing the episode, the graph,
the ledger or the motive belong to the stage that has them and are deliberately
not half-checked here.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, ValidationError

from memotron.caveman import models as models_module
from memotron.caveman.models import (
    MAX_CLAIM_TEXT,
    MAX_FACT_TEXT,
    MAX_NODE_NAME,
    MAX_REASON_TEXT,
    NODE_TYPE_PATTERN,
    ClaimKind,
    ClaimMode,
    ClaimSpec,
    DreamEvent,
    DreamGlobalResponse,
    DreamNodeResponse,
    DreamOp,
    Episode,
    ExtractResponse,
    Fact,
    FactKind,
    FactSpec,
    LedgerEntry,
    MergeOp,
    NewNodeSpec,
    Node,
    NodeAlias,
    Receipt,
    ReceiptOp,
    ReconcileResponse,
    Relation,
    RelationSpec,
    RenameEdgeTypeOp,
    RetireRelationOp,
    RetypeOp,
    RewriteOp,
    SplitOp,
    SplitSpec,
    merged_aliases,
    normalize_aliases,
)

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)


def _every_model() -> list[type[BaseModel]]:
    """Every pydantic model defined in the module, private base included."""
    return [
        member
        for _, member in inspect.getmembers(models_module, inspect.isclass)
        if issubclass(member, BaseModel) and member.__module__ == models_module.__name__
    ]


def _claim(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "claim": "Every real run of Memotron goes through the JedAI Gateway.",
        "kind": "rule",
        "claim_mode": "directive",
        "subjects": ["the JedAI Gateway"],
        "objects": [],
        "identifiers": [],
        "supersedes_claim_index": None,
        "confidence": 0.95,
        "turns": [1],
    }
    fields.update(overrides)
    return fields


def _entry(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "entry_id": "e-01",
        "ts": NOW,
        "episode_id": "ep-251-01",
        "scope": "repo:jedai/memotron",
        "claim": "The JedAI Gateway is a LiteLLM proxy in front of the JedAI models.",
        "kind": "is",
        "claim_mode": "descriptive",
        "subjects": ["the JedAI Gateway"],
        "motive": "engineering",
        "confidence": 0.9,
        "turns": [2],
        "receipt_id": "r-01",
    }
    fields.update(overrides)
    return fields


def _node(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "node_id": "n-041",
        "scope": "repo:jedai/memotron",
        "name": "gateway",
        "type": "service",
        "ledger_key": "n-041",
        "created_at": NOW,
        "last_touched_at": NOW,
    }
    fields.update(overrides)
    return fields


def _binding(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "local_name": "the JedAI Gateway",
        "decision": "bind",
        "node_id": "n-003",
        "new_node": None,
        "reason": "same LiteLLM proxy the node's gloss describes",
    }
    fields.update(overrides)
    return fields


# ------------------------------------------------------- module-wide invariants


def test_the_module_defines_every_record_the_design_names() -> None:
    """The control on the enumeration below: it must actually have found things."""
    found = {model.__name__ for model in _every_model()}
    assert {
        "Turn",
        "Episode",
        "LedgerEntry",
        "Node",
        "Fact",
        "Relation",
        "DreamEvent",
        "NodeAlias",
        "Receipt",
        "ExtractResponse",
        "ReconcileResponse",
        "DreamNodeResponse",
        "DreamGlobalResponse",
    } <= found


@pytest.mark.parametrize("model", _every_model(), ids=lambda m: m.__name__)
def test_every_model_forbids_extra_fields(model: type[BaseModel]) -> None:
    assert model.model_config.get("extra") == "forbid", f"{model.__name__} would silently accept an invented field"


@pytest.mark.parametrize("model", _every_model(), ids=lambda m: m.__name__)
def test_every_model_is_frozen(model: type[BaseModel]) -> None:
    assert model.model_config.get("frozen") is True, f"{model.__name__} is mutable"


def test_an_invented_field_is_rejected_not_dropped() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        Node.model_validate(_node(surprise="value"))


def test_a_node_cannot_be_mutated_in_place() -> None:
    """The one thing that makes 'the dreamer is the only writer' real."""
    node = Node.model_validate(_node())
    with pytest.raises(ValidationError, match="frozen"):
        node.facts = ()  # type: ignore[misc]


# ------------------------------------------------------------------------ enums


def test_the_claim_kinds_are_exactly_the_six_designed_words() -> None:
    """Words, not symbols (#251 amendment D). ``relation`` and ``correction``
    have no fact counterpart: one becomes an edge, the other a dream decision."""
    assert [(k.name, k.value) for k in ClaimKind] == [
        ("RULE", "rule"),
        ("IS", "is"),
        ("ATTRIBUTE", "attribute"),
        ("UNSURE", "unsure"),
        ("RELATION", "relation"),
        ("CORRECTION", "correction"),
    ]


def test_the_fact_kinds_are_the_five_a_node_can_hold() -> None:
    assert [(k.name, k.value) for k in FactKind] == [
        ("RULE", "rule"),
        ("IS", "is"),
        ("ATTRIBUTE", "attribute"),
        ("UNSURE", "unsure"),
        ("SUPERSEDED", "superseded"),
    ]


def test_every_kind_word_is_plain_ascii_and_one_word() -> None:
    """The whole point of the rename: a prompt states a word, never an alphabet."""
    for kind in (*ClaimKind, *FactKind):
        assert kind.value.isascii()
        assert kind.value.isalpha()
        assert kind.value == kind.value.lower()


def test_the_dream_ops_name_one_graph_mutation_each() -> None:
    """The journal's vocabulary. Bookkeeping writes are deliberately absent."""
    assert {op.value for op in DreamOp} == {
        "node_created",
        "node_rewritten",
        "node_merged",
        "node_split",
        "node_retyped",
        "node_deleted",
        "aliases_added",
        "relation_upserted",
        "relation_retired",
        "edge_type_renamed",
    }


def test_claim_mode_covers_the_six_designed_speech_acts() -> None:
    assert {m.value for m in ClaimMode} == {
        "descriptive",
        "requirement",
        "preference",
        "directive",
        "correction",
        "report",
    }


def test_receipt_op_receipts_both_halves_of_every_stage() -> None:
    """A rejection that is not receipted is a prompt regression nobody can see."""
    names = {op.name for op in ReceiptOp}
    for stage in ("EXTRACT", "RECONCILE"):
        assert f"{stage}_REJECTED" in names
    assert {"DREAM_NODE_APPLIED", "DREAM_NODE_REJECTED", "DREAM_GLOBAL_REJECTED"} <= names
    assert {"READ_EMITTED", "READ_BUDGET_SATURATED", "ERASURE_APPLIED"} <= names


def test_receipt_op_values_are_stable_snake_case() -> None:
    """Receipt ops are persisted. A renamed value is a broken receipt stream."""
    for op in ReceiptOp:
        assert op.value == op.name.lower()


# ---------------------------------------------------------------------- episode


def test_episode_as_prompt_text_renders_one_numbered_turn_per_line() -> None:
    episode = Episode.model_validate(
        {
            "episode_id": "ep-251-01",
            "scope": "repo:jedai/memotron",
            "occurred_at": NOW,
            "turns": [{"index": index, "speaker": "Logan", "text": f"turn {index}"} for index in range(1, 11)],
        }
    )
    rendered = episode.as_prompt_text()
    lines = rendered.splitlines()
    assert len(lines) == 10
    assert lines[0] == "1. Logan: turn 1"
    assert lines[9] == "10. Logan: turn 10"


def test_episode_as_prompt_text_is_the_only_rendering() -> None:
    """The identifier check reads this exact string, so an identifier in a turn is findable."""
    episode = Episode.model_validate(
        {
            "episode_id": "ep",
            "scope": "s",
            "occurred_at": NOW,
            "turns": [{"index": 1, "speaker": "agent", "text": "#245 fixed the Host rewrite"}],
        }
    )
    assert "#245" in episode.as_prompt_text()


def test_episode_turn_indices_are_what_the_turns_claim() -> None:
    episode = Episode.model_validate(
        {
            "episode_id": "ep",
            "scope": "s",
            "occurred_at": NOW,
            "turns": [{"index": 3, "speaker": "a", "text": "x"}, {"index": 7, "speaker": "b", "text": "y"}],
        }
    )
    assert episode.turn_indices() == frozenset({3, 7})


def test_an_episode_needs_at_least_one_turn() -> None:
    with pytest.raises(ValidationError):
        Episode.model_validate({"episode_id": "ep", "scope": "s", "occurred_at": NOW, "turns": []})


def test_a_turn_index_is_one_based() -> None:
    """`as_prompt_text` numbers turns as a reader counts them."""
    with pytest.raises(ValidationError):
        Episode.model_validate(
            {
                "episode_id": "ep",
                "scope": "s",
                "occurred_at": NOW,
                "turns": [{"index": 0, "speaker": "a", "text": "x"}],
            }
        )


# ----------------------------------------------------------------- ledger entry


def test_a_reason_is_bounded_by_what_a_receipt_can_carry() -> None:
    """A paragraph guard, not a cap, and set where the record stops reading.

    Nothing in any prompt states this number and a model cannot count, so at 300
    it was a cap rather than a guard: a live run lost a whole node answer to
    ``reason: String should have at most 300 characters``, a rejection about the
    justification and not about a single belief in it. 600 is
    :attr:`Receipt.detail`'s own bound, which is where a reason goes.
    """
    assert MAX_REASON_TEXT == 600
    long_reason = "because " * 70
    assert 300 < len(long_reason) <= MAX_REASON_TEXT
    answer = {"type": "service", "facts": [_fact_spec()], "reason": long_reason}
    assert DreamNodeResponse.model_validate(answer).reason == long_reason
    with pytest.raises(ValidationError, match=f"at most {MAX_REASON_TEXT} characters"):
        DreamNodeResponse.model_validate({**answer, "reason": "x" * (MAX_REASON_TEXT + 1)})


def test_a_ledger_entry_accepts_a_claim_at_the_shared_ceiling() -> None:
    """``MAX_CLAIM_TEXT``, the SAME bound ``Relation.claim`` takes.

    Which is what makes "a provisional relation carries the ledger claim
    verbatim" true of every claim rather than of the short ones (#251 amendment
    D): reconcile no longer has a precondition refusing the batch in between.
    """
    entry = LedgerEntry.model_validate(_entry(claim="x" * MAX_CLAIM_TEXT))
    assert len(entry.claim) == MAX_CLAIM_TEXT
    assert MAX_CLAIM_TEXT > MAX_FACT_TEXT


def test_a_ledger_entry_rejects_a_claim_one_character_over_the_ceiling() -> None:
    """The claim is a full sentence, not a paragraph. The ceiling is what keeps it one."""
    with pytest.raises(ValidationError, match=f"at most {MAX_CLAIM_TEXT} characters"):
        LedgerEntry.model_validate(_entry(claim="x" * (MAX_CLAIM_TEXT + 1)))


@pytest.mark.parametrize(
    "claim",
    [
        "  The gateway fronts the JedAI models.  ",
        "The gateway fronts\nthe JedAI models.",
    ],
)
def test_a_ledger_entry_rejects_a_padded_or_multi_line_claim(claim: str) -> None:
    """The rule a relation applies, applied here, because an edge carries this text.

    Every rendering of an entry puts it on one line -- a reconcile prompt, a
    dream prompt, an ``explain``. So padding and a newline break a rendering
    rather than a record, and the two claim records now refuse the same strings.
    """
    with pytest.raises(ValidationError, match="ledger claim must be one line with no padding"):
        LedgerEntry.model_validate(_entry(claim=claim))


def test_a_ledger_entry_rejects_a_claim_too_short_to_be_a_sentence() -> None:
    with pytest.raises(ValidationError, match="at least 8 characters"):
        LedgerEntry.model_validate(_entry(claim="short"))


def test_a_ledger_entry_needs_at_least_one_subject() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry.model_validate(_entry(subjects=[]))


def test_a_ledger_entry_needs_at_least_one_turn() -> None:
    with pytest.raises(ValidationError):
        LedgerEntry.model_validate(_entry(turns=[]))


def test_a_fresh_ledger_entry_has_no_node_ids() -> None:
    """Filled by reconcile and by nothing else -- extract has never seen the graph."""
    assert LedgerEntry.model_validate(_entry()).node_ids == ()


def test_a_ledger_entry_coerces_its_sequences_to_tuples() -> None:
    """Immutability all the way down; a shared list on an append-only record is a trap."""
    entry = LedgerEntry.model_validate(_entry(subjects=["a", "b"], identifiers=["#245"]))
    assert isinstance(entry.subjects, tuple)
    assert isinstance(entry.identifiers, tuple)


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_a_ledger_entry_confidence_is_a_probability(confidence: float) -> None:
    with pytest.raises(ValidationError):
        LedgerEntry.model_validate(_entry(confidence=confidence))


# ------------------------------------------------------------------ node + link


def test_a_node_starts_factless_undreamed_and_unread() -> None:
    """Reconcile creates a node with zero facts and dirty; the dreamer writes fact one."""
    node = Node.model_validate(_node())
    assert node.facts == ()
    assert node.embedding == ()
    assert node.dreamed_at is None
    assert node.read_count == 0
    assert node.dirty is False


def test_a_node_name_is_capped_at_the_shared_ceiling() -> None:
    Node.model_validate(_node(name="x" * MAX_NODE_NAME))
    with pytest.raises(ValidationError):
        Node.model_validate(_node(name="x" * (MAX_NODE_NAME + 1)))


def test_a_node_type_is_capped_to_match_the_type_pattern() -> None:
    """24 characters, the same ceiling `NODE_TYPE_PATTERN` allows a response to send."""
    Node.model_validate(_node(type="x" * 24))
    with pytest.raises(ValidationError):
        Node.model_validate(_node(type="x" * 25))


def _fact(**overrides: object) -> dict[str, object]:
    """An ``attribute`` fact with one entry of evidence."""
    fields: dict[str, object] = {
        "kind": FactKind.ATTRIBUTE,
        "text": "chat default is claude-haiku-4-5",
        "entry_ids": ("e-01",),
        "first_seen": NOW,
        "last_seen": NOW,
    }
    fields.update(overrides)
    return fields


def _relation(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "relation_id": "r-001",
        "scope": "repo:jedai/memotron",
        "source_id": "n-001",
        "target_id": "n-004",
        "type": "BLOCKED",
        "claim": "the Host header was refused until #245 fixed the rewrite",
        "entry_ids": ("e-01",),
        "until": "#245",
        "first_seen": NOW,
        "last_seen": NOW,
    }
    fields.update(overrides)
    return fields


def test_a_fact_needs_at_least_one_entry_of_evidence() -> None:
    """Evidence is not optional: a belief nothing in the ledger supports is not one."""
    with pytest.raises(ValidationError):
        Fact.model_validate(_fact(entry_ids=()))


def test_a_fact_counts_its_evidence() -> None:
    """What ``(xN)`` renders from and what a reinforcement increments."""
    assert Fact.model_validate(_fact()).evidence == 1
    assert Fact.model_validate(_fact(entry_ids=("e-01", "e-02", "e-09"))).evidence == 3


def test_a_fact_refuses_the_same_entry_twice() -> None:
    """A duplicated entry id would inflate evidence and make one claim look like two."""
    with pytest.raises(ValidationError, match="names the same ledger entry twice"):
        Fact.model_validate(_fact(entry_ids=("e-01", "e-01")))


def test_a_fact_refuses_a_blank_entry_id() -> None:
    with pytest.raises(ValidationError, match="blank ledger entry id"):
        Fact.model_validate(_fact(entry_ids=("e-01", "  ")))


def test_only_an_attribute_fact_may_carry_a_key() -> None:
    """Every other kind renders under its kind word, so a key there is ignored --
    and a field that is silently ignored is a field two callers will disagree about."""
    assert Fact.model_validate(_fact(key="chat default")).key == "chat default"
    with pytest.raises(ValidationError, match="only an 'attribute' fact has a label"):
        Fact.model_validate(_fact(kind=FactKind.RULE, key="rule label"))


def test_a_fact_cannot_be_last_seen_before_it_was_first_seen() -> None:
    with pytest.raises(ValidationError, match="before it was first seen"):
        Fact.model_validate(_fact(last_seen=NOW - timedelta(days=1)))


def test_a_fact_is_one_unpadded_line() -> None:
    """The renderer emits it as a line, so a newline would silently split a block."""
    for bad in ("two\nlines", " padded", "padded "):
        with pytest.raises(ValidationError, match="one line with no leading or trailing space"):
            Fact.model_validate(_fact(text=bad))


def test_a_fact_text_is_capped_at_the_shared_ceiling() -> None:
    Fact.model_validate(_fact(text="x" * MAX_FACT_TEXT))
    with pytest.raises(ValidationError):
        Fact.model_validate(_fact(text="x" * (MAX_FACT_TEXT + 1)))


def test_a_relation_carries_its_own_claim_and_evidence() -> None:
    """The change amendment D is about: an edge asserts something, and says on what."""
    relation = Relation.model_validate(_relation())
    assert relation.claim.startswith("the Host header")
    assert relation.evidence == 1
    assert relation.triple == ("n-001", "BLOCKED", "n-004")


@pytest.mark.parametrize("bad", ["blocked", "B", "Blocked", "BLOCKED-BY", "X" * 33, "1BLOCKED"])
def test_a_relation_type_is_upper_snake_two_to_thirty_two_characters(bad: str) -> None:
    with pytest.raises(ValidationError):
        Relation.model_validate(_relation(type=bad))


@pytest.mark.parametrize("good", ["BLOCKED", "DEPLOYS", "IS_FRONTED_BY", "V2", "A" * 32])
def test_a_relation_type_accepts_the_shapes_the_pattern_names(good: str) -> None:
    assert Relation.model_validate(_relation(type=good)).type == good


def test_a_relation_cannot_point_a_node_at_itself() -> None:
    """A merge produces exactly this if nobody stops it, and it asserts nothing."""
    with pytest.raises(ValidationError, match="at itself"):
        Relation.model_validate(_relation(target_id="n-001"))


def test_a_relation_needs_evidence_and_refuses_a_repeat() -> None:
    with pytest.raises(ValidationError):
        Relation.model_validate(_relation(entry_ids=()))
    with pytest.raises(ValidationError, match="names the same ledger entry twice"):
        Relation.model_validate(_relation(entry_ids=("e-01", "e-01")))


def test_a_relations_until_marker_is_free_text_and_optional() -> None:
    """``#245`` is what the ledger says; inventing a datetime for it would assert
    a precision nobody stated."""
    assert Relation.model_validate(_relation(until=None)).until is None
    assert Relation.model_validate(_relation(until="v0.4.1")).until == "v0.4.1"


def test_a_relation_claim_is_one_unpadded_line() -> None:
    with pytest.raises(ValidationError, match="one line with no padding"):
        Relation.model_validate(_relation(claim="two\nlines"))


def test_a_dream_event_records_content_and_a_receipt() -> None:
    """The journal row. ``node_ids`` may be empty: an edge-type rename is about a
    label across however many relations carry it, not about particular nodes."""
    event = DreamEvent.model_validate(
        {
            "event_id": "ev-0001",
            "ts": NOW,
            "scope": "repo:jedai/memotron",
            "op": DreamOp.EDGE_TYPE_RENAMED,
            "before": {"type": "FRONTS"},
            "after": {"type": "IS_FRONTED_BY"},
            "receipt_id": "rc-1",
        }
    )
    assert event.node_ids == ()
    assert event.before == {"type": "FRONTS"}
    assert event.op is DreamOp.EDGE_TYPE_RENAMED


def test_a_dream_event_needs_a_receipt_id() -> None:
    """An event naming no receipt is a decision with no audit row behind it."""
    with pytest.raises(ValidationError):
        DreamEvent.model_validate(
            {
                "event_id": "ev-0001",
                "ts": NOW,
                "scope": "s",
                "op": DreamOp.NODE_CREATED,
                "receipt_id": "",
            }
        )


# ------------------------------------------------------------------- aliases


def test_a_node_has_no_aliases_until_a_name_routes_to_it() -> None:
    assert Node.model_validate(_node()).aliases == ()


def test_a_node_keeps_every_surface_name_it_was_given() -> None:
    """Case-preserving: the spelling a person typed is the one worth showing back."""
    node = Node.model_validate(_node(aliases=["C4 memory server", "LiteLLM proxy"]))
    assert node.aliases == ("C4 memory server", "LiteLLM proxy")


def test_a_node_rejects_an_alias_that_repeats_its_own_name() -> None:
    """The name is already an exact-match key; an alias repeating it buys nothing."""
    with pytest.raises(ValidationError, match="normalises to"):
        Node.model_validate(_node(name="gateway", aliases=["gateway"]))


def test_a_node_rejects_an_alias_that_repeats_its_name_in_another_case() -> None:
    with pytest.raises(ValidationError, match="normalises to"):
        Node.model_validate(_node(name="gateway", aliases=["Gateway"]))


def test_a_node_rejects_two_aliases_that_differ_only_in_case() -> None:
    with pytest.raises(ValidationError, match="normalises to"):
        Node.model_validate(_node(aliases=["C4", "c4"]))


def test_a_node_rejects_a_blank_alias() -> None:
    """An empty search key has nothing to repair it to, so it is refused."""
    with pytest.raises(ValidationError, match="cannot be blank"):
        Node.model_validate(_node(aliases=["   "]))


def test_normalize_aliases_dedupes_case_insensitively_keeping_the_first_spelling() -> None:
    assert normalize_aliases(name="gateway", aliases=["C4", "c4", "C4 "]) == ("C4", "C4 ")


def test_normalize_aliases_drops_the_name_in_any_case() -> None:
    assert normalize_aliases(name="gateway", aliases=["GATEWAY", "LiteLLM proxy"]) == ("LiteLLM proxy",)


def test_normalize_aliases_raises_on_a_blank_alias() -> None:
    with pytest.raises(ValueError, match="cannot be blank"):
        normalize_aliases(name="gateway", aliases=["ok", ""])


def test_normalize_aliases_of_nothing_is_nothing() -> None:
    assert normalize_aliases(name="gateway", aliases=()) == ()


def test_a_node_built_from_normalize_aliases_output_always_validates() -> None:
    """The record enforces exactly what the function produces -- no third rule.

    This is the property that lets the store normalise and the record assert:
    if the two could disagree, every store write would be a coin flip.
    """
    messy = ["Gateway", "C4", "c4", "LiteLLM proxy"]
    canonical = normalize_aliases(name="gateway", aliases=messy)
    assert Node.model_validate(_node(name="gateway", aliases=list(canonical))).aliases == canonical


def test_merged_aliases_unions_the_absorbed_name_and_its_aliases() -> None:
    """The absorbed NAME is the one string a merge would otherwise destroy."""
    survivor = Node.model_validate(_node(node_id="n-001", name="gateway", aliases=["LiteLLM proxy"]))
    absorbed = Node.model_validate(_node(node_id="n-002", name="litellm-gw", aliases=["proxy"]))
    assert merged_aliases(name="gateway", survivor=survivor, absorbed=[absorbed]) == (
        "LiteLLM proxy",
        "litellm-gw",
        "proxy",
    )


def test_merged_aliases_never_keeps_the_surviving_name_as_an_alias() -> None:
    survivor = Node.model_validate(_node(node_id="n-001", name="gateway"))
    absorbed = Node.model_validate(_node(node_id="n-002", name="Gateway", aliases=["c4"]))
    assert merged_aliases(name="gateway", survivor=survivor, absorbed=[absorbed]) == ("c4",)


def test_merged_aliases_of_a_merge_that_absorbs_nothing_is_what_the_survivor_had() -> None:
    survivor = Node.model_validate(_node(name="gateway", aliases=["c4"]))
    assert merged_aliases(name="gateway", survivor=survivor, absorbed=[]) == ("c4",)


def test_a_node_alias_records_exactly_what_moved() -> None:
    """`moved_entry_ids` IS `rekey_node`'s return, so an un-merge is a replay."""
    alias = NodeAlias.model_validate(
        {
            "alias_node_id": "n-011",
            "survivor_node_id": "n-003",
            "moved_entry_ids": ["e-04", "e-09"],
            "receipt_id": "r-12",
            "ts": NOW,
        }
    )
    assert alias.moved_entry_ids == ("e-04", "e-09")


# --------------------------------------------------------------------- receipts


def test_a_receipt_detail_is_bounded() -> None:
    """Receipts are content-free by construction; 600 chars is the bound that keeps them so."""
    Receipt.model_validate(_receipt(detail="x" * 600))
    with pytest.raises(ValidationError, match="at most 600 characters"):
        Receipt.model_validate(_receipt(detail="x" * 601))


def test_a_receipt_detail_defaults_to_empty() -> None:
    assert Receipt.model_validate(_receipt()).detail == ""


def _receipt(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "receipt_id": "r-01",
        "op": ReceiptOp.EXTRACT_ACCEPTED,
        "ts": NOW,
        "scope": "repo:jedai/memotron",
        "subject": "ep-251-01",
        "inputs_digest": "a" * 64,
        "outputs_digest": "b" * 64,
    }
    fields.update(overrides)
    return fields


# ------------------------------------------------------------ EXTRACT contract


def test_extract_response_accepts_the_designs_worked_example() -> None:
    response = ExtractResponse.model_validate({"claims": [_claim()]})
    assert response.claims[0].kind is ClaimKind.RULE
    assert response.claims[0].claim_mode is ClaimMode.DIRECTIVE


def test_extract_response_needs_at_least_one_claim() -> None:
    with pytest.raises(ValidationError):
        ExtractResponse.model_validate({"claims": []})


def test_extract_response_caps_a_batch_at_64_claims() -> None:
    ExtractResponse.model_validate({"claims": [_claim() for _ in range(64)]})
    with pytest.raises(ValidationError):
        ExtractResponse.model_validate({"claims": [_claim() for _ in range(65)]})


def test_a_claim_carries_at_most_four_subjects() -> None:
    ExtractResponse.model_validate({"claims": [_claim(subjects=["a", "b", "c", "d"])]})
    with pytest.raises(ValidationError):
        ExtractResponse.model_validate({"claims": [_claim(subjects=["a", "b", "c", "d", "e"])]})


def test_an_unknown_sigil_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractResponse.model_validate({"claims": [_claim(kind="?")]})


def test_an_unknown_claim_mode_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ExtractResponse.model_validate({"claims": [_claim(claim_mode="vibes")]})


def test_a_backward_supersedes_reference_is_accepted() -> None:
    """The in-episode correction the demo exists to show: turn 9 corrects turn 7."""
    response = ExtractResponse.model_validate(
        {"claims": [_claim(), _claim(supersedes_claim_index=0, claim_mode="correction")]}
    )
    assert response.claims[1].supersedes_claim_index == 0


def test_a_forward_supersedes_reference_is_rejected() -> None:
    """A claim may only supersede one stated EARLIER; forward is a supersession cycle."""
    with pytest.raises(ValidationError, match="not a backward reference"):
        ExtractResponse.model_validate({"claims": [_claim(supersedes_claim_index=1), _claim()]})


def test_a_self_supersedes_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a backward reference"):
        ExtractResponse.model_validate({"claims": [_claim(supersedes_claim_index=0)]})


def test_a_negative_supersedes_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a backward reference"):
        ExtractResponse.model_validate({"claims": [_claim(), _claim(supersedes_claim_index=-1)]})


def test_an_out_of_range_supersedes_reference_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not a backward reference"):
        ExtractResponse.model_validate({"claims": [_claim(), _claim(supersedes_claim_index=7)]})


def test_a_claim_spec_turn_membership_is_NOT_checked_here() -> None:
    """Deliberate: it needs the episode. `extract` owns it -- one owner per rule."""
    spec = ClaimSpec.model_validate(_claim(turns=[99]))
    assert spec.turns == (99,)


@pytest.mark.parametrize(
    "claim",
    [
        "  The gateway fronts the JedAI models.  ",
        "The gateway fronts\nthe JedAI models.",
    ],
)
def test_a_claim_spec_rejects_a_padded_or_multi_line_claim(claim: str) -> None:
    """On the WIRE, so a model's formatting is an out-of-contract answer, not an internal error.

    :class:`LedgerEntry` applies the same rule. Discovering it there would
    surface as a validation failure inside the stage, several calls from the
    answer that caused it, instead of a rejection the stage can receipt.
    """
    with pytest.raises(ValidationError, match="a claim must be one line with no padding"):
        ClaimSpec.model_validate(_claim(claim=claim))


# ---------------------------------------------------------- RECONCILE contract


def test_reconcile_response_accepts_the_designs_worked_example() -> None:
    response = ReconcileResponse.model_validate(
        {
            "bindings": [
                _binding(),
                _binding(local_name="the LiteLLM proxy", reason="same service, second surface name"),
                _binding(
                    local_name="the chart",
                    decision="new",
                    node_id=None,
                    new_node={"name": "chart", "type": "artifact", "gloss": "the Helm chart"},
                    reason="no candidate describes the deployment chart",
                ),
            ]
        }
    )
    assert response.local_names() == ("the JedAI Gateway", "the LiteLLM proxy", "the chart")


def test_a_bind_without_a_node_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="decided 'bind' without a node_id"):
        ReconcileResponse.model_validate({"bindings": [_binding(node_id=None)]})


def test_a_bind_that_also_proposes_a_new_node_is_rejected() -> None:
    with pytest.raises(ValidationError, match="also proposed a new_node"):
        ReconcileResponse.model_validate(
            {"bindings": [_binding(new_node={"name": "x", "type": "service", "gloss": "g"})]}
        )


def test_a_new_without_a_new_node_is_rejected() -> None:
    with pytest.raises(ValidationError, match="decided 'new' without a new_node"):
        ReconcileResponse.model_validate({"bindings": [_binding(decision="new", node_id=None)]})


def test_a_new_that_also_names_a_node_id_is_rejected() -> None:
    with pytest.raises(ValidationError, match="also named a node_id"):
        ReconcileResponse.model_validate(
            {
                "bindings": [
                    _binding(decision="new", new_node={"name": "x", "type": "service", "gloss": "g"}),
                ]
            }
        )


def test_an_unknown_decision_is_rejected() -> None:
    with pytest.raises(ValidationError):
        ReconcileResponse.model_validate({"bindings": [_binding(decision="probably")]})


def test_one_local_name_adjudicated_twice_is_rejected() -> None:
    with pytest.raises(ValidationError, match="adjudicated more than once"):
        ReconcileResponse.model_validate({"bindings": [_binding(), _binding()]})


def test_two_names_converging_on_one_new_node_is_accepted() -> None:
    """The mechanism, not a coincidence: this group becomes exactly one `create_node`."""
    shape = {"name": "gateway", "type": "service", "gloss": "the LiteLLM proxy"}
    response = ReconcileResponse.model_validate(
        {
            "bindings": [
                _binding(local_name="the JedAI Gateway", decision="new", node_id=None, new_node=shape),
                _binding(local_name="the LiteLLM proxy", decision="new", node_id=None, new_node=dict(shape)),
            ]
        }
    )
    assert {b.new_node.name for b in response.bindings if b.new_node} == {"gateway"}


def test_convergence_is_case_insensitive_on_the_name() -> None:
    with pytest.raises(ValidationError, match="two different shapes"):
        ReconcileResponse.model_validate(
            {
                "bindings": [
                    _binding(
                        local_name="a",
                        decision="new",
                        node_id=None,
                        new_node={"name": "Gateway", "type": "service", "gloss": "g"},
                    ),
                    _binding(
                        local_name="b",
                        decision="new",
                        node_id=None,
                        new_node={"name": "gateway", "type": "policy", "gloss": "g"},
                    ),
                ]
            }
        )


def test_a_same_named_new_node_group_disagreeing_on_gloss_is_rejected() -> None:
    """Saying 'same concept' and 'different concept' at once. Reject rather than pick."""
    with pytest.raises(ValidationError, match="two different shapes"):
        ReconcileResponse.model_validate(
            {
                "bindings": [
                    _binding(
                        local_name="a",
                        decision="new",
                        node_id=None,
                        new_node={"name": "chart", "type": "artifact", "gloss": "the Helm chart"},
                    ),
                    _binding(
                        local_name="b",
                        decision="new",
                        node_id=None,
                        new_node={"name": "chart", "type": "artifact", "gloss": "a bar chart"},
                    ),
                ]
            }
        )


@pytest.mark.parametrize("bad_type", ["Service", "service name", "1service", "", "x" * 25, "svc_underscore"])
def test_a_new_node_type_must_be_one_lowercase_word(bad_type: str) -> None:
    with pytest.raises(ValidationError):
        NewNodeSpec.model_validate({"name": "chart", "type": bad_type, "gloss": "g"})


@pytest.mark.parametrize("ok_type", ["service", "artifact", "agent-memory", "c4", "x", "x" * 24])
def test_a_reasonable_new_node_type_is_accepted(ok_type: str) -> None:
    assert NewNodeSpec.model_validate({"name": "chart", "type": ok_type, "gloss": "g"}).type == ok_type


def test_the_node_type_pattern_ceiling_matches_the_node_record() -> None:
    """A response that validates must never fail the record it becomes."""
    assert NODE_TYPE_PATTERN == r"^[a-z][a-z0-9-]{0,23}$"
    Node.model_validate(_node(type="a" + "b" * 23))


# --------------------------------------------------------------- the wire specs


def _fact_spec(**overrides: object) -> dict[str, object]:
    """A minimal valid :class:`FactSpec` payload, overridable field by field."""
    return {
        "kind": "attribute",
        "key": None,
        "text": "chat default is claude-haiku-4-5",
        "entry_ids": ["e-01"],
    } | overrides


def _relation_spec(**overrides: object) -> dict[str, object]:
    """A minimal valid :class:`RelationSpec` payload, overridable field by field."""
    return {
        "relation_id": None,
        "type": "BLOCKED",
        "target_id": "n-004",
        "claim": "the Host header rewrite was refused",
        "entry_ids": ["e-02"],
        "until": None,
    } | overrides


def test_a_fact_spec_carries_a_kind_a_label_text_and_its_own_evidence() -> None:
    """The named fields that replaced a plain string (#251 amendment D, D-C)."""
    spec = FactSpec.model_validate(_fact_spec(key="chat default", text="claude-haiku-4-5"))
    assert (spec.kind, spec.key, spec.text, spec.entry_ids) == (
        FactKind.ATTRIBUTE,
        "chat default",
        "claude-haiku-4-5",
        ("e-01",),
    )


def test_a_fact_spec_cannot_arrive_with_no_evidence() -> None:
    """Whether the ids are BOUND needs the ledger; that one is never checked here."""
    with pytest.raises(ValidationError):
        FactSpec.model_validate(_fact_spec(entry_ids=[]))


def test_a_fact_spec_refuses_the_same_entry_twice() -> None:
    """Evidence is a set wearing a tuple's clothes: a repeat would inflate the count."""
    with pytest.raises(ValidationError, match="names the same ledger entry twice"):
        FactSpec.model_validate(_fact_spec(entry_ids=["e-01", "e-01"]))


@pytest.mark.parametrize("kind", ["rule", "is", "unsure", "superseded"])
def test_only_an_attribute_spec_may_carry_a_label(kind: str) -> None:
    """Every other kind renders under its kind word, so a key there is ignored."""
    with pytest.raises(ValidationError, match="only an 'attribute' fact has a label"):
        FactSpec.model_validate(_fact_spec(kind=kind, key="chat default"))


def test_a_fact_spec_refuses_padded_or_multiline_text() -> None:
    """A padded string breaks a RENDERING, so it is refused at the record."""
    with pytest.raises(ValidationError, match="one line with no leading or trailing space"):
        FactSpec.model_validate(_fact_spec(text=" padded "))
    with pytest.raises(ValidationError, match="one line with no leading or trailing space"):
        FactSpec.model_validate(_fact_spec(text="two\nlines"))


def test_a_relation_spec_names_its_target_by_id_and_defaults_to_a_new_edge() -> None:
    """``relation_id`` is the one field that changes the operation."""
    spec = RelationSpec.model_validate(_relation_spec(until="#245"))
    assert spec.relation_id is None
    assert (spec.type, spec.target_id, spec.until) == ("BLOCKED", "n-004", "#245")


def test_a_relation_spec_requires_an_upper_snake_type() -> None:
    """A relation type is a label on an edge, and the shape is fixed."""
    assert RelationSpec.model_validate(_relation_spec(type="DEPLOYED_BY")).type == "DEPLOYED_BY"
    for bad in ("blocked", "Blocked", "B", "TOO_LONG_" + "X" * 32):
        with pytest.raises(ValidationError):
            RelationSpec.model_validate(_relation_spec(type=bad))


def test_a_relation_spec_cannot_arrive_with_no_evidence_or_no_claim() -> None:
    """An edge asserting nothing, or resting on nothing, is not a belief."""
    with pytest.raises(ValidationError):
        RelationSpec.model_validate(_relation_spec(entry_ids=[]))
    with pytest.raises(ValidationError):
        RelationSpec.model_validate(_relation_spec(claim=""))


# -------------------------------------------------------- DREAM-NODE contract


def test_dream_node_response_accepts_the_designs_worked_example() -> None:
    response = DreamNodeResponse.model_validate(
        {
            "type": "service",
            "facts": [
                _fact_spec(kind="rule", text="real runs always via the gateway, never hermetic"),
                _fact_spec(kind="is", text="LiteLLM proxy fronting the JedAI models"),
            ],
            "relations": [_relation_spec(until="#245")],
            "retired_relation_ids": [],
            "reason": "supersession applied",
        }
    )
    assert response.type == "service"
    assert [fact.kind for fact in response.facts] == [FactKind.RULE, FactKind.IS]
    assert response.relations[0].target_id == "n-004"


def test_a_node_cannot_come_back_with_zero_facts() -> None:
    """The complete new fact set, never a diff. Empty would silently blank the node."""
    with pytest.raises(ValidationError):
        DreamNodeResponse.model_validate({"type": "service", "facts": [], "reason": "r"})


def test_relations_and_retirements_default_to_nothing() -> None:
    """Most dreams touch no edge, and a node answer is not a verdict on its neighbours."""
    response = DreamNodeResponse.model_validate({"type": "service", "facts": [_fact_spec()], "reason": "r"})
    assert response.relations == ()
    assert response.retired_relation_ids == ()


def test_the_fact_budget_is_NOT_checked_here() -> None:
    """Deliberate: `L` and `T` are motive fields. `dream` owns the budget rules."""
    response = DreamNodeResponse.model_validate(
        {
            "type": "service",
            "facts": [_fact_spec(text=f"fact {index}") for index in range(40)],
            "reason": "r",
        }
    )
    assert len(response.facts) == 40


def test_one_fact_sent_twice_in_one_answer_is_rejected() -> None:
    """Response-internal: two records with one text would collapse, order deciding which."""
    with pytest.raises(ValidationError, match="fact sent more than once"):
        DreamNodeResponse.model_validate({"type": "service", "facts": [_fact_spec(), _fact_spec()], "reason": "r"})


def test_one_edge_stated_twice_in_one_answer_is_rejected() -> None:
    """The source is fixed by the node, so two specs sharing type and target are one edge."""
    with pytest.raises(ValidationError, match="relation sent more than once"):
        DreamNodeResponse.model_validate(
            {
                "type": "service",
                "facts": [_fact_spec()],
                "relations": [_relation_spec(), _relation_spec(claim="and also this")],
                "reason": "r",
            }
        )


def test_an_edge_both_rewritten_and_retired_in_one_answer_is_rejected() -> None:
    """Two verdicts on one belief, and applying both depends on which runs second."""
    with pytest.raises(ValidationError, match="both rewritten and retired"):
        DreamNodeResponse.model_validate(
            {
                "type": "service",
                "facts": [_fact_spec()],
                "relations": [_relation_spec(relation_id="r-001")],
                "retired_relation_ids": ["r-001"],
                "reason": "r",
            }
        )


def test_one_relation_retired_twice_in_one_answer_is_rejected() -> None:
    with pytest.raises(ValidationError, match="names the same id twice"):
        DreamNodeResponse.model_validate(
            {
                "type": "service",
                "facts": [_fact_spec()],
                "retired_relation_ids": ["r-001", "r-001"],
                "reason": "r",
            }
        )


# ------------------------------------------------------ DREAM-GLOBAL contract


def test_dream_global_response_accepts_the_designs_worked_example() -> None:
    response = DreamGlobalResponse.model_validate(
        {
            "ops": [
                {
                    "op": "merge",
                    "nodes": ["n-003", "n-011"],
                    "survivor_name": "gateway",
                    "survivor_type": "service",
                    "facts": [_fact_spec(kind="rule", text="real runs always via the gateway")],
                    "reason": "one service, two names",
                },
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        {
                            "name": "chart",
                            "type": "artifact",
                            "facts": [_fact_spec(text="deploys the MCP server, #240", entry_ids=["e-07"])],
                            "entry_ids": ["e-07"],
                            "relation_ids": [],
                        },
                        {
                            "name": "c4",
                            "type": "cluster",
                            "facts": [_fact_spec(text="returns 31 tools after #248", entry_ids=["e-09"])],
                            "entry_ids": ["e-09", "e-12"],
                        },
                    ],
                    "reason": "separate concepts",
                },
                {"op": "retype", "node": "n-002", "type": "policy", "reason": "a rule, not a service"},
                {
                    "op": "rewrite",
                    "node": "n-005",
                    "facts": [_fact_spec(text="deduped against the gateway node")],
                    "relations": [_relation_spec(type="FRONTS", target_id="n-003")],
                    "reason": "deduped against gateway",
                },
                {"op": "rename_edge_type", "old": "PROXIES", "new": "FRONTS", "reason": "one relationship"},
                {"op": "retire_relation", "relation_id": "r-009", "reason": "#245 fixed it"},
            ]
        }
    )
    assert [type(op).__name__ for op in response.ops] == [
        "MergeOp",
        "SplitOp",
        "RetypeOp",
        "RewriteOp",
        "RenameEdgeTypeOp",
        "RetireRelationOp",
    ]


def test_zero_ops_is_a_valid_global_pass() -> None:
    """The common case at `pressure == 0`: nothing needs reorganising."""
    assert DreamGlobalResponse.model_validate({"ops": []}).ops == ()
    assert DreamGlobalResponse.model_validate({}).ops == ()


def test_the_op_union_is_discriminated_on_op() -> None:
    """One clear error naming the variant, not six 'matches no member' reports."""
    with pytest.raises(ValidationError, match="survivor_name"):
        DreamGlobalResponse.model_validate({"ops": [{"op": "merge", "nodes": ["n-1", "n-2"]}]})


def test_an_unknown_op_name_is_rejected() -> None:
    with pytest.raises(ValidationError):
        DreamGlobalResponse.model_validate({"ops": [{"op": "demote", "node": "n-1", "reason": "r"}]})


def test_a_merge_needs_two_or_more_nodes() -> None:
    with pytest.raises(ValidationError):
        MergeOp.model_validate(
            {
                "nodes": ["n-003"],
                "survivor_name": "g",
                "survivor_type": "service",
                "facts": [_fact_spec()],
                "reason": "r",
            }
        )


def test_a_merge_naming_the_same_node_twice_is_rejected() -> None:
    with pytest.raises(ValidationError, match="names the same node twice"):
        MergeOp.model_validate(
            {
                "nodes": ["n-003", "n-003"],
                "survivor_name": "g",
                "survivor_type": "service",
                "facts": [_fact_spec()],
                "reason": "r",
            }
        )


def test_a_merge_carries_no_relations_because_the_store_re_points_them() -> None:
    """A merge that restated its edges would write the same beliefs twice."""
    assert "relations" not in MergeOp.model_fields
    with pytest.raises(ValidationError):
        MergeOp.model_validate(
            {
                "nodes": ["n-003", "n-011"],
                "survivor_name": "g",
                "survivor_type": "service",
                "facts": [_fact_spec()],
                "relations": [_relation_spec()],
                "reason": "r",
            }
        )


def test_a_node_named_by_two_ops_is_rejected() -> None:
    """The ops are a batch with no defined order, so two ops on one node is undefined."""
    with pytest.raises(ValidationError, match="node named by more than one op"):
        DreamGlobalResponse.model_validate(
            {
                "ops": [
                    {"op": "retype", "node": "n-002", "type": "policy", "reason": "r"},
                    {"op": "rewrite", "node": "n-002", "facts": [_fact_spec()], "reason": "r"},
                ]
            }
        )


def test_a_node_absorbed_by_a_merge_and_also_rewritten_is_rejected() -> None:
    """The same rule reaching into a merge's `nodes` list, not only its survivor."""
    with pytest.raises(ValidationError, match="node named by more than one op"):
        DreamGlobalResponse.model_validate(
            {
                "ops": [
                    {
                        "op": "merge",
                        "nodes": ["n-003", "n-011"],
                        "survivor_name": "g",
                        "survivor_type": "service",
                        "facts": [_fact_spec()],
                        "reason": "r",
                    },
                    {"op": "rewrite", "node": "n-011", "facts": [_fact_spec()], "reason": "r"},
                ]
            }
        )


def test_a_vocabulary_op_claims_no_node_so_it_never_conflicts_with_one() -> None:
    """A rename acts on a TYPE and a retirement on an EDGE, so neither is a node verdict."""
    response = DreamGlobalResponse.model_validate(
        {
            "ops": [
                {"op": "retype", "node": "n-002", "type": "policy", "reason": "r"},
                {"op": "rename_edge_type", "old": "PROXIES", "new": "FRONTS", "reason": "r"},
                {"op": "retire_relation", "relation_id": "r-009", "reason": "r"},
            ]
        }
    )
    assert len(response.ops) == 3


def test_one_edge_type_renamed_by_two_ops_is_rejected() -> None:
    """The node rule, for the thing a rename claims: two writes with no defined order."""
    with pytest.raises(ValidationError, match="edge type renamed by more than one op"):
        DreamGlobalResponse.model_validate(
            {
                "ops": [
                    {"op": "rename_edge_type", "old": "PROXIES", "new": "FRONTS", "reason": "r"},
                    {"op": "rename_edge_type", "old": "PROXIES", "new": "ROUTES_TO", "reason": "r"},
                ]
            }
        )


def test_one_relation_retired_by_two_ops_is_rejected() -> None:
    with pytest.raises(ValidationError, match="relation retired by more than one op"):
        DreamGlobalResponse.model_validate(
            {
                "ops": [
                    {"op": "retire_relation", "relation_id": "r-009", "reason": "r"},
                    {"op": "retire_relation", "relation_id": "r-009", "reason": "and again"},
                ]
            }
        )


def test_a_rename_to_the_same_type_is_rejected() -> None:
    """Renaming a type to itself changes nothing, and the ops are not a no-op language."""
    with pytest.raises(ValidationError, match="to itself changes nothing"):
        RenameEdgeTypeOp.model_validate({"old": "FRONTS", "new": "FRONTS", "reason": "r"})


def test_a_rename_needs_two_well_shaped_edge_types() -> None:
    assert RenameEdgeTypeOp.model_validate({"old": "PROXIES", "new": "FRONTS", "reason": "r"}).old == "PROXIES"
    with pytest.raises(ValidationError):
        RenameEdgeTypeOp.model_validate({"old": "proxies", "new": "FRONTS", "reason": "r"})


def test_a_retirement_names_one_relation_id() -> None:
    """By id rather than by triple, because a rename in the same pass can change a triple."""
    assert RetireRelationOp.model_validate({"relation_id": "r-009", "reason": "r"}).relation_id == "r-009"
    with pytest.raises(ValidationError):
        RetireRelationOp.model_validate({"relation_id": "", "reason": "r"})


def test_a_split_needs_two_or_more_parts() -> None:
    with pytest.raises(ValidationError):
        SplitOp.model_validate(
            {
                "node": "n-004",
                "into": [
                    {"name": "chart", "type": "artifact", "facts": [_fact_spec()], "entry_ids": ["e-07"]},
                ],
                "reason": "r",
            }
        )


def test_a_split_part_must_claim_at_least_one_entry() -> None:
    """`entry_ids` must partition the parent's entries; a part claiming none cannot."""
    with pytest.raises(ValidationError):
        SplitSpec.model_validate({"name": "chart", "type": "artifact", "facts": [_fact_spec()], "entry_ids": []})


def test_a_split_part_must_carry_at_least_one_fact() -> None:
    with pytest.raises(ValidationError):
        SplitSpec.model_validate({"name": "chart", "type": "artifact", "facts": [], "entry_ids": ["e-07"]})


def test_a_split_part_sending_one_fact_twice_is_rejected() -> None:
    with pytest.raises(ValidationError, match="sends a fact more than once"):
        SplitSpec.model_validate(
            {
                "name": "chart",
                "type": "artifact",
                "facts": [_fact_spec(), _fact_spec()],
                "entry_ids": ["e-07"],
            }
        )


def test_a_split_part_claims_no_relations_by_default() -> None:
    """An edge no part names goes with the parent, because the endpoint is deleted."""
    spec = SplitSpec.model_validate(
        {"name": "chart", "type": "artifact", "facts": [_fact_spec()], "entry_ids": ["e-07"]}
    )
    assert spec.relation_ids == ()


def test_a_rewrite_cannot_blank_a_node() -> None:
    with pytest.raises(ValidationError):
        RewriteOp.model_validate({"node": "n-005", "facts": [], "reason": "r"})


def test_a_rewrite_is_the_only_global_op_carrying_relations() -> None:
    """Where the whole-scope view notices that two facts are one belief between two nodes."""
    op = RewriteOp.model_validate(
        {
            "node": "n-005",
            "facts": [_fact_spec()],
            "relations": [_relation_spec(type="FRONTS", target_id="n-003")],
            "retired_relation_ids": ["r-009"],
            "reason": "r",
        }
    )
    assert op.relations[0].type == "FRONTS"
    assert op.retired_relation_ids == ("r-009",)
    assert "relations" not in SplitSpec.model_fields
    assert "relations" not in RetypeOp.model_fields


def test_a_rewrite_that_both_rewrites_and_retires_one_edge_is_rejected() -> None:
    with pytest.raises(ValidationError, match="both rewrites and retires"):
        RewriteOp.model_validate(
            {
                "node": "n-005",
                "facts": [_fact_spec()],
                "relations": [_relation_spec(relation_id="r-009")],
                "retired_relation_ids": ["r-009"],
                "reason": "r",
            }
        )


def test_a_retype_must_supply_a_well_shaped_type() -> None:
    assert RetypeOp.model_validate({"node": "n-002", "type": "policy", "reason": "r"}).type == "policy"
    with pytest.raises(ValidationError):
        RetypeOp.model_validate({"node": "n-002", "type": "Policy Layer", "reason": "r"})


def test_op_discriminators_default_to_their_own_value() -> None:
    """So a construction in Python does not have to restate `op="merge"`."""
    assert RetypeOp.model_validate({"node": "n", "type": "policy", "reason": "r"}).op == "retype"
    assert RewriteOp.model_validate({"node": "n", "facts": [_fact_spec()], "reason": "r"}).op == "rewrite"
    assert RenameEdgeTypeOp.model_validate({"old": "A_TYPE", "new": "B_TYPE", "reason": "r"}).op == "rename_edge_type"
    assert RetireRelationOp.model_validate({"relation_id": "r-1", "reason": "r"}).op == "retire_relation"
