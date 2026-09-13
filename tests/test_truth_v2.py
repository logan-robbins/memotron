"""WS-16 Truth v2 (core): evidence-weighted supersession, principled confidence,
and multi-active polarity-conflict handling.

Pins the T12/T13/T15 contracts:

- T12 — one equal-authority observation cannot flip an incumbent whose
  ``observed_count`` reached ``SupersessionPolicy.corroboration_margin``; the
  challenger is parked for review until ``corroboration_required`` distinct
  observations of the same challenger statement exist, at which point the flip
  lands and the parked siblings resolve as ``corroborated``.
- T13 — reinforcement uses bounded evidence accumulation
  (``c' = min(ceiling, c + (1 - c) * gain * c_obs)``), a gate-parked challenger
  discounts the surviving incumbent
  (``c' = max(floor, c * (1 - discount * c_challenger))``), and the optional
  ``half_life_days`` knob decays ONLY the pruning min-confidence evaluation.
- T15 — a negated restatement of a MULTI_ACTIVE fact supersedes (or is parked
  against) the conflicting row instead of silently coexisting.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    ConfidencePolicy,
    DedupPolicy,
    Memotron,
    MemoryScope,
    PruningPolicy,
    ScopeKind,
)
from memotron.config import default_config
from memotron.models import RelationshipStatus
from memotron.receipts import ReceiptDecisionType

REINFORCEMENT_GAIN = 0.25
CONTRADICTION_DISCOUNT = 0.25
CONFIDENCE_CEILING = 0.99
CONFIDENCE_FLOOR = 0.05


def _accumulate(confidence: float, observation_confidence: float) -> float:
    """Mirror of the T13 reinforcement update, in the exact operation order."""
    return min(
        CONFIDENCE_CEILING,
        confidence + (1.0 - confidence) * REINFORCEMENT_GAIN * observation_confidence,
    )


def _discount(confidence: float, challenger_confidence: float) -> float:
    """Mirror of the T13 dispute discount, in the exact operation order."""
    return max(
        CONFIDENCE_FLOOR,
        confidence * (1.0 - CONTRADICTION_DISCOUNT * challenger_confidence),
    )


async def _reinforce_requirement(
    client: Memotron,
    scope: MemoryScope,
    *,
    times: int,
    start: datetime,
    object_value: str = "two approvals",
    confidence: float = 0.8,
):
    """Observe the identical single-active fact *times* times (exact-match reinforce)."""
    result = None
    for index in range(times):
        result = await client.add_memory(
            subject="production changes",
            predicate="require",
            object=object_value,
            relationship_type="REQUIRES",
            scope=scope,
            confidence=confidence,
            reference_time=start + timedelta(days=index),
        )
    assert result is not None
    return result


@pytest.mark.asyncio
async def test_equal_authority_single_observation_cannot_flip_corroborated_incumbent(
    tmp_path,
) -> None:
    client = Memotron(graph_path=tmp_path / "corroboration-park.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent = await _reinforce_requirement(client, scope, times=3, start=start)
    challenger = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=3),
    )

    active = client.graph.get_relationship(incumbent.relationship_uuid)
    parked = client.graph.get_relationship(challenger.relationship_uuid)
    current = await client.search(query="approvals", scope=scope)

    assert challenger.relationship_uuid != incumbent.relationship_uuid
    assert active.properties["status"] == RelationshipStatus.ACTIVE.value
    assert active.properties["observed_count"] == 3
    assert parked.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert parked.properties["requires_operator_review"] is True
    assert parked.properties["supersession_gate_reason"] == (
        "insufficient_corroboration:incumbent_observed_count=3:corroborations=1/2"
    )
    assert parked.properties["superseded_by_relationship_uuid"] == incumbent.relationship_uuid
    assert [item.relationship_uuid for item in current] == [incumbent.relationship_uuid]

    # T13: created at 0.8, reinforced twice by 0.8 observations, then discounted
    # exactly once by the parked 0.8-confidence challenger.
    expected = 0.8
    expected = _accumulate(expected, 0.8)
    expected = _accumulate(expected, 0.8)
    expected = _discount(expected, 0.8)
    assert active.properties["confidence"] == pytest.approx(expected)
    assert active.properties["disputed_count"] == 1


@pytest.mark.asyncio
async def test_corroboration_escape_flips_truth_and_resolves_parked_siblings(
    tmp_path,
) -> None:
    client = Memotron(graph_path=tmp_path / "corroboration-flip.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent = await _reinforce_requirement(client, scope, times=3, start=start)
    first_challenge = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=3),
    )
    second_challenge = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=4),
    )

    superseded = client.graph.get_relationship(incumbent.relationship_uuid)
    resolved_sibling = client.graph.get_relationship(first_challenge.relationship_uuid)
    winner = client.graph.get_relationship(second_challenge.relationship_uuid)
    current = await client.search(query="approvals", scope=scope)

    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    assert superseded.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert superseded.properties["superseded_by_relationship_uuid"] == winner.uuid
    assert resolved_sibling.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert resolved_sibling.properties["review_resolution"] == "corroborated"
    assert resolved_sibling.properties["requires_operator_review"] is False
    assert resolved_sibling.properties["superseded_by_relationship_uuid"] == winner.uuid
    assert [item.relationship_uuid for item in current] == [winner.uuid]

    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    flip_receipts = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED
        and receipt.decision_reason == "corroborated_challenger_flip"
    ]
    assert len(flip_receipts) == 1
    assert flip_receipts[0].superseded_relationship_uuid == resolved_sibling.uuid
    assert flip_receipts[0].successor_relationship_uuid == winner.uuid
    run_uuids = {receipt.run_uuid for receipt in receipts}
    assert all(client.graph.receipts.verify_chain(run_uuid).valid for run_uuid in run_uuids)


@pytest.mark.asyncio
async def test_higher_authority_single_observation_flips_regardless_of_observed_count(
    tmp_path,
) -> None:
    client = Memotron(graph_path=tmp_path / "authority-flip.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent = await _reinforce_requirement(client, scope, times=5, start=start)
    correction = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=5),
        metadata={"_verified_source_authority": "operator"},
    )

    superseded = client.graph.get_relationship(incumbent.relationship_uuid)
    winner = client.graph.get_relationship(correction.relationship_uuid)
    current = await client.search(query="approvals", scope=scope)

    assert superseded.properties["observed_count"] == 5
    assert superseded.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert superseded.properties["superseded_by_relationship_uuid"] == winner.uuid
    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    assert winner.properties.get("requires_operator_review") is None
    assert winner.properties.get("supersession_gate_reason") is None
    assert [item.relationship_uuid for item in current] == [winner.uuid]


@pytest.mark.asyncio
async def test_below_margin_incumbent_is_superseded_by_single_equal_authority_contradiction(
    tmp_path,
) -> None:
    client = Memotron(graph_path=tmp_path / "below-margin.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent = await _reinforce_requirement(client, scope, times=2, start=start)
    challenger = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=2),
    )

    superseded = client.graph.get_relationship(incumbent.relationship_uuid)
    winner = client.graph.get_relationship(challenger.relationship_uuid)
    current = await client.search(query="approvals", scope=scope)

    assert superseded.properties["observed_count"] == 2
    assert superseded.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert superseded.properties["superseded_by_relationship_uuid"] == winner.uuid
    assert superseded.properties.get("disputed_count") is None
    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    assert winner.properties.get("requires_operator_review") is None
    assert [item.relationship_uuid for item in current] == [winner.uuid]


@pytest.mark.asyncio
async def test_reinforcement_accumulates_bounded_evidence(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "accumulation.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    first = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.6,
        reference_time=start,
    )
    second = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.8,
        reference_time=start + timedelta(days=1),
    )

    relationship = client.graph.get_relationship(second.relationship_uuid)

    assert second.relationship_uuid == first.relationship_uuid
    assert second.reinforced_relationships == 1
    assert relationship.properties["observed_count"] == 2
    assert relationship.properties["confidence"] == pytest.approx(_accumulate(0.6, 0.8))
    assert relationship.properties["confidence_strategy"] == "evidence_accumulation"


@pytest.mark.asyncio
async def test_half_life_none_leaves_pruning_unchanged(tmp_path) -> None:
    config = default_config().model_copy(update={"pruning": PruningPolicy(min_confidence=0.4)})
    client = Memotron(config=config, graph_path=tmp_path / "no-decay.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    created = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.8,
        reference_time=start,
    )
    await client.run_due_dreams(now=start + timedelta(days=60))

    relationship = client.graph.get_relationship(created.relationship_uuid)
    assert relationship.properties["status"] == RelationshipStatus.ACTIVE.value
    assert relationship.properties["confidence"] == 0.8


@pytest.mark.asyncio
async def test_half_life_decay_prunes_by_effective_confidence_without_mutating_stored(
    tmp_path,
) -> None:
    config = default_config().model_copy(
        update={
            "pruning": PruningPolicy(min_confidence=0.4),
            "confidence": ConfidencePolicy(half_life_days=7.0),
        }
    )
    client = Memotron(config=config, graph_path=tmp_path / "decay.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    created = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.8,
        reference_time=start,
    )
    # 60 days at a 7-day half-life: 0.8 * 2**(-60/7) ≈ 0.0021 < 0.4.
    await client.run_due_dreams(now=start + timedelta(days=60))

    archived = client.graph.get_relationship(created.relationship_uuid)
    assert archived.properties["status"] == RelationshipStatus.PRUNED.value
    assert archived.properties["pruned_reason"] == "below_min_confidence"
    # The decay knob never mutates the stored confidence.
    assert archived.properties["confidence"] == 0.8


@pytest.mark.asyncio
async def test_multi_active_polarity_conflict_supersedes_conflicting_row_only(
    tmp_path,
) -> None:
    # Lower the preference dedup threshold so the negated paraphrase matches the
    # positive statement through the object-embedding cosine path.
    config = default_config().model_copy(update={"dedup": DedupPolicy(memory_type_thresholds={"preference": 0.55})})
    client = Memotron(config=config, graph_path=tmp_path / "polarity.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    liked = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=start,
    )
    negated = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="does not like dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=start + timedelta(days=1),
    )
    unrelated = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="compact layout summaries",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=start + timedelta(days=2),
    )

    superseded = client.graph.get_relationship(liked.relationship_uuid)
    winner = client.graph.get_relationship(negated.relationship_uuid)
    coexisting = client.graph.get_relationship(unrelated.relationship_uuid)
    current = await client.search(query="dark mode", scope=scope)

    assert negated.relationship_uuid != liked.relationship_uuid
    assert negated.superseded_relationships == 1
    assert superseded.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert superseded.properties["superseded_by_relationship_uuid"] == winner.uuid
    assert superseded.properties["semantic_polarity"] == "positive"
    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    assert winner.properties["semantic_polarity"] == "negative"
    assert coexisting.properties["status"] == RelationshipStatus.ACTIVE.value
    assert coexisting.properties.get("superseded_by_relationship_uuid") is None
    assert liked.relationship_uuid not in {item.relationship_uuid for item in current}

    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    polarity_receipts = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED
        and receipt.decision_reason == "polarity_conflict"
    ]
    assert len(polarity_receipts) == 1
    assert polarity_receipts[0].superseded_relationship_uuid == superseded.uuid
    assert polarity_receipts[0].successor_relationship_uuid == winner.uuid
    run_uuids = {receipt.run_uuid for receipt in receipts}
    assert all(client.graph.receipts.verify_chain(run_uuid).valid for run_uuid in run_uuids)


@pytest.mark.asyncio
async def test_multi_active_lower_authority_negation_is_parked_not_superseding(
    tmp_path,
) -> None:
    client = Memotron(graph_path=tmp_path / "polarity-gate.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    start = datetime(2026, 7, 1, tzinfo=UTC)

    liked = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=start,
    )
    # "not dark mode" strips its negation marker to exactly "dark mode", so the
    # conflict is detected through the exact-match-after-stripping path even at
    # the default (conservative) dedup threshold.
    negated = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="not dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=start + timedelta(days=1),
        metadata={"_verified_source_authority": "agent"},
    )

    incumbent = client.graph.get_relationship(liked.relationship_uuid)
    parked = client.graph.get_relationship(negated.relationship_uuid)
    current = await client.search(query="dark mode", scope=scope)

    assert incumbent.properties["status"] == RelationshipStatus.ACTIVE.value
    assert parked.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert parked.properties["requires_operator_review"] is True
    assert parked.properties["supersession_gate_reason"] == "lower_authority:agent<user"
    assert parked.properties["superseded_by_relationship_uuid"] == incumbent.uuid
    assert [item.relationship_uuid for item in current] == [incumbent.uuid]
    # T13: the parked dispute discounts the surviving incumbent exactly once.
    assert incumbent.properties["confidence"] == pytest.approx(_discount(1.0, 1.0))
    assert incumbent.properties["disputed_count"] == 1
