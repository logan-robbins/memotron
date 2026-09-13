"""WS-25: Temporal-authoritative supersession & read ordering.

T1 — ``truth_slot_key`` / ``contradictory_object`` (pure predicates, retrieval.py).
T2 — recency-authoritative auto-close at the write-side supersession gate,
     bypassing the temporary-severity / corroboration gates for a
     config-listed type (default preference/directive/state), never the
     lower-authority gate, with a clamped "newer" check immune to a spoofed
     future ``valid_from``.
T3 — read-time within-slot hard recency tiebreaker (contradiction-aware),
     wired into both ``search_context`` and ``profile()``.
T4 — the bypass is claim-mode agnostic (CORRECTION is a hint, not a gate).
T5 — gold-set regression (``ingest/actionability_goldset.yaml``,
     ``temporal_authority`` section).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    DreamJobKind,
    Memotron,
    MemoryScope,
    MemoryType,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.models import RelationshipStatus
from memotron.retrieval import contradictory_object, semantic_polarity, truth_slot_key

# ---------------------------------------------------------------------------
# T1 — pure predicates
# ---------------------------------------------------------------------------


def test_truth_slot_key_is_object_independent() -> None:
    a = truth_slot_key(scope_key="user:bob", subject="user", predicate="prefers")
    b = truth_slot_key(scope_key="user:bob", subject="User", predicate="Prefers")
    assert a == b


def test_truth_slot_key_differs_by_predicate() -> None:
    prefers = truth_slot_key(scope_key="user:bob", subject="user", predicate="prefers")
    uses = truth_slot_key(scope_key="user:bob", subject="user", predicate="uses")
    assert prefers != uses


def test_truth_slot_key_differs_by_scope() -> None:
    a = truth_slot_key(scope_key="user:bob", subject="user", predicate="prefers")
    b = truth_slot_key(scope_key="user:alice", subject="user", predicate="prefers")
    assert a != b


def test_contradictory_object_negated_restatement_is_contradictory() -> None:
    # "prefers dark mode" vs "prefers not dark mode" -> same slot (by
    # construction, both callers pass the same truth_slot_key), contradictory.
    positive = semantic_polarity("user prefers dark mode")
    negative = semantic_polarity("user prefers not dark mode")
    assert positive == "positive"
    assert negative == "negative"
    assert contradictory_object(
        first_object="dark mode",
        first_polarity=positive,
        second_object="not dark mode",
        second_polarity=negative,
    )


def test_contradictory_object_different_object_is_not_contradictory() -> None:
    # "prefers dark mode" + "prefers window seating" -> same slot, NOT
    # contradictory -- multi-active coexistence.
    assert not contradictory_object(
        first_object="dark mode",
        first_polarity="positive",
        second_object="window seating",
        second_polarity="positive",
    )


def test_contradictory_object_same_polarity_is_never_contradictory() -> None:
    assert not contradictory_object(
        first_object="dark mode",
        first_polarity="positive",
        second_object="dark mode",
        second_polarity="positive",
    )


# ---------------------------------------------------------------------------
# T2 (+ T4) — recency-authoritative auto-close at the write-side gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recency_authoritative_bypass_closes_high_corroboration_incumbent(
    tmp_path,
) -> None:
    """Equal-authority newer preference closes an observed_count=9 incumbent.

    Pre-WS-25 this parks (``insufficient_corroboration``); WS-25 T2 makes it
    auto-close instead, because ``preference`` is a recency-authoritative
    type.  The candidate is plain ``PREFERENCE`` claim mode (default for the
    type), never ``CORRECTION`` -- this is also the T4 proof: correctness
    does not depend on the extractor's claim-mode label.
    """
    client = Memotron(graph_path=tmp_path / "bypass.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="temporal-authority-user")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent_result = None
    for i in range(9):
        incumbent_result = await client.add_memory(
            subject="user",
            predicate="prefers",
            object="dark mode",
            relationship_type="PREFERS",
            scope=scope,
            reference_time=t0 + timedelta(hours=i),
        )
    assert incumbent_result is not None
    incumbent_uuid = incumbent_result.relationship_uuid
    incumbent_before = client.graph.get_relationship(incumbent_uuid)
    assert incumbent_before.properties["observed_count"] == 9
    assert incumbent_before.properties["status"] == RelationshipStatus.ACTIVE.value

    candidate_time = t0 + timedelta(days=1)
    candidate_result = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="not dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=candidate_time,
    )

    assert candidate_result.created_relationships == 1
    assert candidate_result.superseded_relationships == 1

    incumbent_after = client.graph.get_relationship(incumbent_uuid)
    candidate_row = client.graph.get_relationship(candidate_result.relationship_uuid)
    assert incumbent_after.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert incumbent_after.valid_to == candidate_time
    assert incumbent_after.properties["superseded_by_relationship_uuid"] == (candidate_result.relationship_uuid)
    assert candidate_row.properties["status"] == RelationshipStatus.ACTIVE.value
    # T4: the candidate is plain PREFERENCE claim mode, not CORRECTION.
    assert candidate_row.properties["claim_mode"] == "preference"

    receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.superseded_relationship_uuid == incumbent_uuid
    ]
    assert receipts
    assert receipts[0].decision_reason == "recency_authoritative_supersede"


@pytest.mark.asyncio
async def test_recency_authoritative_bypass_applies_to_directive_type(tmp_path) -> None:
    """Same mechanism for ``directive`` (SHOULD), the other default-config
    recency-authoritative multi-active type."""
    client = Memotron(graph_path=tmp_path / "directive-bypass.sqlite")
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="directive-agent")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent_result = None
    for i in range(9):
        incumbent_result = await client.add_memory(
            subject="agent",
            predicate="should",
            object="retry on failure",
            relationship_type="SHOULD",
            scope=scope,
            reference_time=t0 + timedelta(hours=i),
        )
    assert incumbent_result is not None

    candidate_result = await client.add_memory(
        subject="agent",
        predicate="should",
        object="not retry on failure",
        relationship_type="SHOULD",
        scope=scope,
        reference_time=t0 + timedelta(days=1),
    )
    assert candidate_result.superseded_relationships == 1
    incumbent_after = client.graph.get_relationship(incumbent_result.relationship_uuid)
    assert incumbent_after.properties["status"] == RelationshipStatus.SUPERSEDED.value


def _state_config() -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=(),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="TRACKS",
                source_label="Entity",
                target_label="Entity",
                query="Track short-lived working state.",
                memory_type=MemoryType.STATE,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
    )


@pytest.mark.asyncio
async def test_recency_authoritative_bypass_applies_to_state_type(tmp_path) -> None:
    """Same mechanism for ``state`` (config default recency-authoritative type,
    multi-active cardinality by RelationshipInstruction's own default)."""
    client = Memotron(graph_path=tmp_path / "state-bypass.sqlite", config=_state_config())
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="state-agent")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent_result = None
    for i in range(9):
        incumbent_result = await client.add_memory(
            subject="worker",
            predicate="tracks",
            object="task open",
            relationship_type="TRACKS",
            scope=scope,
            reference_time=t0 + timedelta(hours=i),
        )
    assert incumbent_result is not None

    candidate_result = await client.add_memory(
        subject="worker",
        predicate="tracks",
        object="not task open",
        relationship_type="TRACKS",
        scope=scope,
        reference_time=t0 + timedelta(days=1),
    )
    assert candidate_result.superseded_relationships == 1
    incumbent_after = client.graph.get_relationship(incumbent_result.relationship_uuid)
    assert incumbent_after.properties["status"] == RelationshipStatus.SUPERSEDED.value


@pytest.mark.asyncio
async def test_world_fact_type_keeps_full_corroboration_gate_no_bypass(tmp_path) -> None:
    """Non-goal guard: ``requirement`` (a world-fact, single-active type) is
    NOT in the default recency_authoritative_types set, so an equal-authority
    newer challenger against a high-observed_count incumbent still PARKS --
    exactly the pre-WS-25 behavior.  This is the "not global last-writer-wins"
    non-goal (NEXT.md WS-25 §9)."""
    client = Memotron(graph_path=tmp_path / "worldfact.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="release-controller")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    incumbent_result = None
    for i in range(9):
        incumbent_result = await client.add_memory(
            subject="production changes",
            predicate="require",
            object="two approvals",
            relationship_type="REQUIRES",
            scope=scope,
            reference_time=t0 + timedelta(hours=i),
        )
    assert incumbent_result is not None

    challenger_result = await client.add_memory(
        subject="production changes",
        predicate="require",
        object="one approval",
        relationship_type="REQUIRES",
        scope=scope,
        reference_time=t0 + timedelta(days=30),
    )
    incumbent_after = client.graph.get_relationship(incumbent_result.relationship_uuid)
    challenger_row = client.graph.get_relationship(challenger_result.relationship_uuid)
    assert incumbent_after.properties["status"] == RelationshipStatus.ACTIVE.value
    assert challenger_row.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert challenger_row.properties["requires_operator_review"] is True
    assert challenger_row.properties["supersession_gate_reason"].startswith("insufficient_corroboration")


@pytest.mark.asyncio
async def test_spoofed_future_valid_from_is_clamped_and_does_not_outrank_genuine_incumbent(
    tmp_path,
) -> None:
    """A candidate whose claimed valid_from postdates its episode's
    reference_time is clamped to that reference_time for the T2 "newer" check
    -- so it cannot deterministically out-rank a genuinely newer, corroboration
    -eligible incumbent by asserting a fantastical future event time.

    Sequence: incumbent B ("not dark mode") is built up to observed_count=9 at
    t0+20d (itself the product of an earlier, legitimate T2 bypass against an
    even older incumbent A).  A challenger C is then submitted whose EPISODE
    reference_time (t0+15d) predates B -- i.e. C's observation genuinely
    happened before B existed -- but whose claimed valid_from is 1000 days out.
    Unclamped, C would appear newer than B and wrongly auto-close it.  Clamped,
    C's effective time (t0+15d) is OLDER than B's valid_from (t0+20d), so the
    bypass does not fire and C falls through to the ordinary gate, where B's
    observed_count (9) >= corroboration_margin and this is C's first
    observation (corroboration_total=1 < required=2) -- C parks.
    """
    client = Memotron(graph_path=tmp_path / "clamp.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="clamp-user")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    # A: the original incumbent.
    fact_a = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0,
    )

    # B: genuinely newer, equal authority -> T2 bypass closes A (A's
    # observed_count is 1, well under the corroboration margin, so this step
    # would supersede A even without T2; it is just how B comes to exist).
    fact_b = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="not dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0 + timedelta(days=20),
    )
    a_after = client.graph.get_relationship(fact_a.relationship_uuid)
    assert a_after.properties["status"] == RelationshipStatus.SUPERSEDED.value

    # Reinforce B up to observed_count=9 (corroboration-eligible).
    for i in range(8):
        await client.add_memory(
            subject="user",
            predicate="prefers",
            object="not dark mode",
            relationship_type="PREFERS",
            scope=scope,
            reference_time=t0 + timedelta(days=20, hours=i),
        )
    b_before = client.graph.get_relationship(fact_b.relationship_uuid)
    assert b_before.properties["observed_count"] == 9
    assert b_before.properties["status"] == RelationshipStatus.ACTIVE.value
    assert b_before.valid_from == t0 + timedelta(days=20)

    # C: episode.reference_time is BEFORE B existed; valid_from is spoofed
    # 1000 days into the future.
    spoofed_valid_from = t0 + timedelta(days=1000)
    honest_reference_time = t0 + timedelta(days=15)
    candidate_result = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=honest_reference_time,
        valid_from=spoofed_valid_from,
    )

    b_after = client.graph.get_relationship(fact_b.relationship_uuid)
    candidate_row = client.graph.get_relationship(candidate_result.relationship_uuid)

    # B must still be the active truth -- the spoof did not out-rank it.
    assert b_after.properties["status"] == RelationshipStatus.ACTIVE.value
    assert b_after.valid_to is None
    # The candidate parked instead of superseding.
    assert candidate_result.superseded_relationships == 0
    assert candidate_row.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert candidate_row.properties["requires_operator_review"] is True
    assert candidate_row.properties["supersession_gate_reason"].startswith("insufficient_corroboration")


# ---------------------------------------------------------------------------
# T3 — read-time within-slot hard recency tiebreaker
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t3_two_active_non_contradictory_facts_both_coexist(tmp_path) -> None:
    """The load-bearing multi-active regression case: two coexisting,
    non-contradictory same-slot preferences must BOTH be returned by search
    and by profile() -- a bare per-slot winner-take-all would silently demote
    one of them."""
    client = Memotron(graph_path=tmp_path / "coexist.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="coexist-user")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    dark_mode = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0,
    )
    window_seating = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="window seating",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0 + timedelta(days=1),
    )

    results = await client.search(query="user prefers", scope=scope, limit=10)
    result_uuids = {result.relationship_uuid for result in results}
    assert dark_mode.relationship_uuid in result_uuids
    assert window_seating.relationship_uuid in result_uuids

    profile = await client.profile(scope=scope)
    profile_uuids = {fact.relationship_uuid for fact in (profile.static_facts + profile.dynamic_facts)}
    assert dark_mode.relationship_uuid in profile_uuids
    assert window_seating.relationship_uuid in profile_uuids


@pytest.mark.asyncio
async def test_t3_demotes_stale_member_of_an_active_contradiction_cluster(tmp_path) -> None:
    """Read-time safety net independent of how a slot ended up holding two
    ACTIVE contradictory rows (legacy/pre-WS-16 data, a bulk migration, or any
    write path that does not run the WS-16/WS-25 write-side gate).  Two
    normal writes are used to get two well-formed rows, the newer one is
    reactivated directly (the WS-25 write-side gate already prevents this
    from happening through the client) to simulate that state, and T3 must
    still resolve it deterministically at read time: only the newest member of
    the contradiction cluster is returned."""
    client = Memotron(graph_path=tmp_path / "t3.sqlite")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="t3-user")
    t0 = datetime(2026, 7, 1, tzinfo=UTC)

    older = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0,
    )
    newer = await client.add_memory(
        subject="user",
        predicate="prefers",
        object="not dark mode",
        relationship_type="PREFERS",
        scope=scope,
        reference_time=t0 + timedelta(days=5),
    )
    older_after = client.graph.get_relationship(older.relationship_uuid)
    assert older_after.properties["status"] == RelationshipStatus.SUPERSEDED.value

    # Simulate legacy data: force the older, contradicted row back to ACTIVE.
    client.graph.update_relationship(
        older.relationship_uuid,
        properties={"status": RelationshipStatus.ACTIVE.value},
        clear_valid_to=True,
    )
    assert (
        client.graph.get_relationship(older.relationship_uuid).properties["status"] == RelationshipStatus.ACTIVE.value
    )

    results = await client.search(query="dark mode", scope=scope, limit=10)
    result_uuids = {result.relationship_uuid for result in results}
    assert newer.relationship_uuid in result_uuids
    assert older.relationship_uuid not in result_uuids

    profile = await client.profile(scope=scope)
    profile_uuids = {fact.relationship_uuid for fact in (profile.static_facts + profile.dynamic_facts)}
    assert newer.relationship_uuid in profile_uuids
    assert older.relationship_uuid not in profile_uuids
