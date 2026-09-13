"""The predicate contract: the truth slot cannot be allowed to absorb the object.

Truth keys are ``scope:subject:predicate`` (``:object`` too, for ``multi_active``
slots).  The predicate therefore IS the slot.  When an extractor folds the object
into the predicate — ``predicate="decided to use DynamoDB"`` /
``object="DynamoDB"`` instead of ``predicate="decided"`` / ``object="use DynamoDB
for the reservation ledger"`` — every later restatement of the same fact lands in
a DIFFERENT slot, so supersession, reinforcement, and rollup clustering all
stop firing and nothing anywhere reports an error.  The graph just quietly
shatters.

Measured against ``claude-haiku-4-5`` on the JedAI Gateway at temperature 0, the
fold was deterministic (3/3 trials) with Memotron's own rendered prompt and
absent (3/3) with a minimal hand-written one.  The difference was not model
variance: the rendered prompt's only substantive mention of ``predicate`` was the
field list, so the model invented its own granularity.

Two halves, pinned here:

1. ``DreamInstructionSet.render_prompt`` states the contract — short verb phrase,
   never containing the object, with the correct/incorrect pair.
2. ``InstructionalExtractor._validate_memory`` enforces it, rejecting (never
   rewriting — a shortened predicate would fabricate a truth key the episode
   never stated) with ``CandidateViolation.PREDICATE_NOT_A_VERB_PHRASE`` and one
   ``CANDIDATE_SCHEMA_REJECTED`` receipt.

And the invariant that makes the rule safe to ship: every predicate this
repository's own fixtures, tests, examples, and committed benchmark corpus use
still passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    MemoryScope,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.config import (
    DEFAULT_MAX_PREDICATE_WORDS,
    default_config,
    predicate_shape_violation,
)
from memotron.extraction import CandidateViolation
from memotron.models import DreamJobKind
from memotron.receipts import ReceiptDecisionType

# ---------------------------------------------------------------------------
# The corpus of predicates this repository actually uses.
#
# Harvested from tests/, examples/, demo/, src/ and benchmarks/, including the
# committed `benchmarks/memotron_kb_v1/corpus.json` whose scores must not
# move.  Every one of these must PASS: a validator that rejects the project's
# own vocabulary is a worse defect than the one it fixes.
# ---------------------------------------------------------------------------

LEGITIMATE_PREDICATES: tuple[str, ...] = (
    # one word — the overwhelming majority
    "requires",
    "require",
    "prefers",
    "should",
    "must",
    "decided",
    "learned",
    "likes",
    "favours",
    "mandates",
    "enforces",
    "owns",
    "runs",
    "uses",
    "tracks",
    "retains",
    "blocks",
    "hit",
    "suffers",
    "reported",
    "summarizes",
    "experienced",
    "is",
    "password",
    # two words — copulas, prepositional relations, and a bare noun phrase
    "is a",
    "has state",
    "lives in",
    "living in",
    "resides in",
    "is stored in",
    "password location",
    "authenticates through",
    "flows into",
    "reads from",
    "currently pins",
    "currently runs",
    "currently spans",
    "currently tracks",
    "currently reports",
    # trailing prepositions the relation genuinely needs — these come straight
    # out of the committed benchmark corpus and must never be treated as folds
    "publishes to",
    "replicates to",
    "logs sessions to",
    # three words
    "is backed by",
    "is drained by",
    "is fronted by",
    "is hosted in",
    "is licensed for",
    "is encrypted with",
    "provided by",
    "signs builds with",
    "pulls weather from",
    "currently settles with",
    "requires retention of",
    "should always escalate",
    "requires before onboarding",
    # four words — the longest legitimate predicate in the repository
    "requires before contract signing",
    "requires before vendor onboarding",
)

# The two shapes measured live on the gateway with Memotron's own prompt.
MEASURED_FOLD_PREDICATE = "decided to use DynamoDB for the reservation ledger"
MEASURED_FOLD_OBJECT = "DynamoDB"


def _scope(scope_id: str = "predicate") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _client(tmp_path: Path, config: DreamConfig | None = None) -> Memotron:
    return Memotron(
        graph_path=tmp_path / "predicate.sqlite",
        config=config if config is not None else default_config(),
    )


def _batch(*memories: dict) -> str:
    return json.dumps({"memories": list(memories)})


def _rejections(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED.value
    ]


def _quarantined(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_QUARANTINED.value
    ]


def _wide_predicate_config(max_predicate_words: int) -> DreamConfig:
    """``default_config`` with the ceiling knob raised."""
    instructions = DreamInstructionSet(
        name="default",
        max_predicate_words=max_predicate_words,
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                cardinality=RelationshipInstruction.model_fields["cardinality"].default,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 1. Every legitimate predicate in this repository passes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("predicate", LEGITIMATE_PREDICATES)
def test_repository_predicates_are_admitted(predicate: str) -> None:
    """A rule that rejects the project's own vocabulary is not shippable."""
    assert (
        predicate_shape_violation(
            predicate=predicate,
            object_text="a signed indemnification form before any WDW site visit",
        )
        is None
    ), predicate


def test_trailing_preposition_is_not_a_fold() -> None:
    """``publishes to`` + ``the Ledger Reconciliation queue`` is the benchmark
    corpus's own shape: the ``to`` is the relation's preposition, and its
    complement is the object.  Only a ``to`` that continues into another verb
    is the fold."""
    assert predicate_shape_violation(predicate="publishes to", object_text="the Ledger Reconciliation queue") is None
    assert predicate_shape_violation(predicate="decided to use", object_text="DynamoDB") is not None


def test_object_containment_is_directional() -> None:
    """Only ``predicate contains object`` is the slot-shattering fold.  An
    object that repeats the relation verb is redundant, not slot-shattering, so
    it is left alone."""
    assert predicate_shape_violation(predicate="uses DynamoDB", object_text="DynamoDB") is not None
    assert (
        predicate_shape_violation(
            predicate="requires",
            object_text="a review that requires data-platform sign-off",
        )
        is None
    )


def test_case_is_not_a_violation() -> None:
    """Truth keys casefold (``graph.normalize_key``), so capitalisation cannot
    split a slot and must not cost a candidate."""
    assert predicate_shape_violation(predicate="Requires", object_text="SOC2") is None


# ---------------------------------------------------------------------------
# 2. The measured fold is rejected, receipted, and sibling-safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("predicate", "object_text"),
    [
        (MEASURED_FOLD_PREDICATE, MEASURED_FOLD_OBJECT),
        ("should use DynamoDB for the reservation ledger", "DynamoDB"),
        ("decided to use DynamoDB", "DynamoDB"),
    ],
)
def test_measured_folds_are_rejected(predicate: str, object_text: str) -> None:
    reason = predicate_shape_violation(predicate=predicate, object_text=object_text)
    assert reason is not None
    assert predicate in reason


@pytest.mark.asyncio
async def test_folded_predicate_is_dropped_and_receipted(tmp_path: Path) -> None:
    """One fold costs one candidate — never the episode, never a sibling — and
    the drop names the violation in the ledger.

    A folded predicate is a stage-3 GOVERNANCE judgement, not a stage-1
    structural one: the candidate is a perfectly valid entity/relation
    (closed-vocabulary labels, legal endpoints), only its truth-key shape is
    bad.  It is therefore quarantined — retained, receipted, promotable, and
    re-governable — rather than rejected outright."""
    client = _client(tmp_path)
    scope = _scope("fold")
    good = {
        "subject": "checkout-api",
        "predicate": "requires",
        "object": "reservation lookup p95 latency under 200 ms",
        "relationship_type": "REQUIRES",
        "confidence": 0.9,
    }
    folded = {
        "subject": "checkout-api",
        "predicate": MEASURED_FOLD_PREDICATE,
        "object": MEASURED_FOLD_OBJECT,
        "relationship_type": "SHOULD",
        "confidence": 0.9,
    }
    await client.add_episode(
        name="folded-batch",
        episode_body=_batch(good, folded),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    surviving = [
        relationship
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    ]
    assert [item.properties["predicate"] for item in surviving] == ["requires"]

    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    payload = json.loads(quarantined[0].event_payload)
    assert payload["violation"] == CandidateViolation.PREDICATE_NOT_A_VERB_PHRASE.value
    assert quarantined[0].decision_result == "quarantined"
    assert quarantined[0].candidate_digest is not None
    # Retained and inspectable: the folded candidate is still queryable raw.
    stored = client.graph.quarantined_candidate(quarantined[0].candidate_uuid)
    assert stored.reason == CandidateViolation.PREDICATE_NOT_A_VERB_PHRASE.value
    assert stored.predicate == MEASURED_FOLD_PREDICATE


@pytest.mark.asyncio
async def test_operator_write_path_still_fails_fast(tmp_path: Path) -> None:
    """``add_memory`` is one deliberate fact with nobody to receipt a drop for,
    so the contract violation stays a hard error the caller sees."""
    client = _client(tmp_path)
    with pytest.raises(ValueError, match="predicate"):
        await client.add_memory(
            subject="checkout-api",
            predicate=MEASURED_FOLD_PREDICATE,
            object=MEASURED_FOLD_OBJECT,
            relationship_type="SHOULD",
            confidence=0.9,
            scope=_scope("operator"),
        )


@pytest.mark.asyncio
async def test_unfolded_restatement_lands_in_one_truth_slot(tmp_path: Path) -> None:
    """The whole point: with the fold rejected, a restatement of the same fact
    reaches the SAME ``scope:subject:predicate`` slot, so the supersession gate
    gets to see it at all.  Folded, each restatement would have carried its own
    object inside the predicate and opened its own slot, and the gate would
    never have been consulted."""
    client = _client(tmp_path)
    scope = _scope("slot")
    for object_text in (
        "use PostgreSQL for the reservation ledger",
        "use DynamoDB for the reservation ledger",
    ):
        await client.add_episode(
            name=f"decision-{object_text[:12]}",
            episode_body=_batch(
                {
                    "subject": "checkout-api",
                    "predicate": "requires",
                    "object": object_text,
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ),
            source=EpisodeType.JSON,
            scope=scope,
        )
        await client.run_dream_job(job_name="formation-default")

    rows = [
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.type == "REQUIRES"
    ]
    assert len(rows) == 2
    # ONE slot for both statements — this is the property the fold destroys.
    assert {item.properties["truth_prefix"] for item in rows} == {f"{scope.key}:checkout-api:requires"}
    assert {(item.properties["status"], item.properties["object"]) for item in rows} == {
        ("superseded", "use PostgreSQL for the reservation ledger"),
        ("active", "use DynamoDB for the reservation ledger"),
    }

    # Contrast: the folded shapes the gateway produced for these same two facts
    # would each have carried their object inside the predicate — two different
    # slots, no gate, two coexisting "current" truths.  They no longer survive
    # validation at all.
    for folded in (
        "requires use of PostgreSQL for the reservation ledger",
        "requires use of DynamoDB for the reservation ledger",
    ):
        assert predicate_shape_violation(predicate=folded, object_text="PostgreSQL") is not None


# ---------------------------------------------------------------------------
# 3. The rendered prompt carries the contract
# ---------------------------------------------------------------------------


def test_rendered_prompt_states_the_predicate_contract() -> None:
    prompt = default_config().instruction_set("default").render_prompt()

    assert "Schema contract (a memory that breaks any of these is discarded):" in prompt
    assert "predicate is a SHORT VERB PHRASE naming the RELATION ONLY" in prompt
    assert "1-3 words, lowercase" in prompt
    assert f"at most {DEFAULT_MAX_PREDICATE_WORDS}" in prompt
    # The correct/incorrect pair that made the minimal prompt succeed.
    assert '- correct:   predicate="decided", object="use DynamoDB for the reservation ledger"' in prompt
    assert '- incorrect: predicate="decided to use DynamoDB", object="DynamoDB"' in prompt
    assert "must NOT contain the object" in prompt
    assert "must NOT run past 'to' into another verb" in prompt


def test_relationship_query_no_longer_feeds_the_rendered_prompt() -> None:
    """[0025] ``RelationshipInstruction.query`` is GOVERNANCE metadata (what
    selects a candidate into this type for validation), not extraction prose.
    MEASURED: rendering it as "Use for: <query>" let a query written as a
    selection criterion ("state the GOVERNED thing as the object") read to the
    model as a dictated predicate, producing the same verb five times over.
    The query text must never reach the model again; only the structural facts
    validation actually enforces (endpoints, cardinality) still render."""
    prompt = default_config().instruction_set("default").render_prompt()
    assert "Remember lessons that help an agent perform its role better" not in prompt
    assert "Use for:" not in prompt
    assert "- SHOULD (Entity -> Entity): Temporal rule: " in prompt
    assert "Truth cardinality: multi_active." in prompt


# ---------------------------------------------------------------------------
# 4. The ceiling is a knob, and the prompt tells the model the knob's value
# ---------------------------------------------------------------------------


def test_default_ceiling_admits_four_words_and_rejects_five() -> None:
    assert DEFAULT_MAX_PREDICATE_WORDS == 4
    assert predicate_shape_violation(predicate="requires before contract signing", object_text="a signed form") is None
    assert (
        predicate_shape_violation(
            predicate="requires prior written data platform approval",
            object_text="a signed form",
        )
        is not None
    )


def test_operator_can_raise_the_ceiling() -> None:
    long_predicate = "requires prior written data platform approval"
    assert predicate_shape_violation(predicate=long_predicate, object_text="a signed form") is not None
    assert predicate_shape_violation(predicate=long_predicate, object_text="a signed form", max_words=6) is None


@pytest.mark.asyncio
async def test_raised_ceiling_admits_the_candidate_end_to_end(tmp_path: Path) -> None:
    """The knob is real at the instruction set, not just in the pure function:
    the same candidate is dropped at the default ceiling and materialized at 6."""
    long_predicate = "requires prior written data platform approval"
    candidate = {
        "subject": "checkout-api",
        "predicate": long_predicate,
        "object": "every reservation ledger migration",
        "relationship_type": "REQUIRES",
        "confidence": 0.9,
    }

    strict = _client(tmp_path / "strict")
    scope = _scope("ceiling")
    await strict.add_episode(
        name="long-predicate",
        episode_body=_batch(candidate),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await strict.run_due_dreams()
    assert len(_quarantined(strict, scope)) == 1
    assert not [
        relationship
        for relationship in strict.graph.active_relationships(scope=scope)
        if relationship.type == "REQUIRES"
    ]

    wide = _client(tmp_path / "wide", config=_wide_predicate_config(6))
    await wide.add_episode(
        name="long-predicate",
        episode_body=_batch(candidate),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await wide.run_due_dreams()
    assert not _rejections(wide, scope)
    assert [
        relationship.properties["predicate"]
        for relationship in wide.graph.active_relationships(scope=scope)
        if relationship.type == "REQUIRES"
    ] == [long_predicate]


def test_raised_ceiling_is_rendered_into_the_prompt() -> None:
    """The model is never told one ceiling and judged by another."""
    prompt = _wide_predicate_config(6).instruction_set("default").render_prompt()
    assert "at most 6" in prompt
    assert f"at most {DEFAULT_MAX_PREDICATE_WORDS}." not in prompt
