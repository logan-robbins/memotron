"""WS-20 — per-fact pinning (T23), configurable floors (T24), staleness lifecycle (T25).

Closes AUDIT.md §1 gaps: no per-fact guaranteed-retrieval mechanism, a hardcoded
identity floor that excluded ``directive``, and default-config immortality of
uncontradicted facts.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    MemoryScope,
    NodeInstruction,
    ProfilePolicy,
    RelationshipInstruction,
    ScopeKind,
    UseEventKind,
)
from memotron.config import Motive, PruningPolicy, default_config
from memotron.models import DreamJobKind, MemoryType, RelationshipCardinality
from memotron.receipts import ReceiptDecisionType

NOW = datetime(2026, 8, 1, tzinfo=UTC)


def _typed_config(pruning: PruningPolicy | None = None) -> DreamConfig:
    """Default-shaped config plus STATE- and IDENTITY-typed relationship types."""
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Remember stable preferences.",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Remember explicit requirements.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that improve agent behaviour.",
            ),
            RelationshipInstruction(
                type="TRACKS",
                source_label="Entity",
                target_label="Entity",
                query="Track short-lived working state.",
                memory_type=MemoryType.STATE,
            ),
            RelationshipInstruction(
                type="IS_A",
                source_label="Entity",
                target_label="Entity",
                query="Remember durable identity facts.",
                memory_type=MemoryType.ANCHOR,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),
            DreamJob(name="pruning-default", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        ),
        pruning=pruning or PruningPolicy(),
    )


async def _seed_outscored_pin(client: Memotron, scope: MemoryScope) -> str:
    """One low-value fact plus 30 higher-scoring facts; returns the low one's uuid."""
    low = await client.add_memory(
        subject="Safety directive",
        predicate="should",
        object="never disclose park incident data",
        relationship_type="SHOULD",
        confidence=0.3,
        scope=scope,
        valid_from=NOW - timedelta(days=200),
    )
    for i in range(30):
        await client.add_memory(
            subject=f"Runbook {i}",
            predicate="prefers",
            object=f"vendor onboarding step {i} checklist",
            relationship_type="PREFERS",
            confidence=0.95,
            scope=scope,
            valid_from=NOW - timedelta(hours=i + 1),
        )
    return low.relationship_uuid


# ---------------------------------------------------------------------------
# T23 — per-fact pin
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_outscored_pin_guaranteed_in_profile_and_search(tmp_path) -> None:
    """A pinned fact outscored by 30 higher-scoring facts still surfaces on both
    read surfaces: marked in the profile (bypassing count and per-type caps) and
    slot-reserved in search results at a tight limit."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-outscored")
    client = Memotron(graph_path=tmp_path / "pin.sqlite", config=_typed_config())
    pinned_uuid = await _seed_outscored_pin(client, scope)

    # Control: without the pin, the low fact is outscored off both surfaces.
    tight = await client.search(query="vendor onboarding step checklist", scope=scope, limit=5)
    assert pinned_uuid not in [result.relationship_uuid for result in tight]
    control_profile = await client.profile(
        scope=scope,
        policy=ProfilePolicy(max_static_facts=10, max_dynamic_facts=0),
    )
    assert pinned_uuid not in [fact.relationship_uuid for fact in control_profile.static_facts]

    pin = await client.pin_memory(
        relationship_uuid=pinned_uuid,
        scope=scope,
        reason="safety directive must always be in context",
        pinned_by="operator:audit",
    )
    assert pin.pinned is True

    # Search: reserved slot ahead of the ranked fill; flag + origin visible.
    results = await client.search(query="vendor onboarding step checklist", scope=scope, limit=5)
    assert len(results) == 5
    assert results[0].relationship_uuid == pinned_uuid
    assert results[0].pinned is True
    assert results[0].origin == "pinned"
    assert all(result.pinned is False for result in results[1:])

    # Profile: bypasses max_static_facts AND per-type caps; rendered marked.
    profile = await client.profile(
        scope=scope,
        policy=ProfilePolicy(
            max_static_facts=10,
            max_dynamic_facts=0,
            per_type_max_facts={"directive": 0},
        ),
    )
    static_uuids = [fact.relationship_uuid for fact in profile.static_facts]
    assert static_uuids[0] == pinned_uuid
    assert profile.static_facts[0].pinned is True
    assert "[PINNED] [SHOULD] Safety directive should never disclose park incident data" in (profile.rendered_context)


@pytest.mark.asyncio
async def test_budget_starved_pin_degrades_to_reference_not_absent(tmp_path) -> None:
    """When the token budget cannot hold even the pins, the pin renders as a REF
    line (even with reference_mode off) and the overflow receipt covers it."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-budget")
    client = Memotron(graph_path=tmp_path / "pin-budget.sqlite", config=_typed_config())
    pinned_uuid = await _seed_outscored_pin(client, scope)
    await client.pin_memory(
        relationship_uuid=pinned_uuid,
        scope=scope,
        reason="must survive budget starvation",
        pinned_by="operator:audit",
    )

    profile = await client.profile(scope=scope, token_budget=16)
    # The budget cannot hold even the pin line: nothing is inlined for it, but
    # it is never silently dropped — it degrades to a REF line (REF lines ride
    # outside the inline budget, per the documented reference_mode contract).
    assert pinned_uuid not in [fact.relationship_uuid for fact in profile.static_facts]
    assert f"[REF:directive] {pinned_uuid}" in profile.rendered_context
    assert pinned_uuid in [item.relationship_uuid for item in profile.reference_items]

    receipts = await client.memory_receipts(scope=scope)
    overflow = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.RETRIEVAL_BUDGET_OVERFLOW_REFERENCED
    ]
    assert overflow and pinned_uuid in overflow[-1].decision_reason


@pytest.mark.asyncio
async def test_pruning_holds_pinned_rows(tmp_path) -> None:
    """Generic pruning paths must never archive a pinned row: the retention gate
    holds it with reason "pinned" (like operator corrections)."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-prune")
    client = Memotron(
        graph_path=tmp_path / "pin-prune.sqlite",
        config=_typed_config(PruningPolicy(active_max_age_seconds=60)),
    )
    old = await client.add_memory(
        subject="User 9",
        predicate="prefers",
        object="quiet park entrances",
        relationship_type="PREFERS",
        confidence=0.9,
        scope=scope,
        valid_from=NOW - timedelta(days=90),
    )
    await client.pin_memory(
        relationship_uuid=old.relationship_uuid,
        scope=scope,
        reason="operator hold",
        pinned_by="operator:audit",
    )
    await client.run_dream_job(job_name="pruning-default", now=NOW)

    row = client.graph.get_relationship(old.relationship_uuid)
    assert row.properties["status"] == "active"
    held = [
        decision
        for decision in await client.dream_decisions()
        if decision.decision_type == "pruning_retention_held" and decision.details.get("eligibility_reason") == "pinned"
    ]
    assert held, "expected a pruning_retention_held decision with reason 'pinned'"


@pytest.mark.asyncio
async def test_unpin_restores_normal_ranking_and_retention(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-unpin")
    client = Memotron(graph_path=tmp_path / "unpin.sqlite", config=_typed_config())
    pinned_uuid = await _seed_outscored_pin(client, scope)
    await client.pin_memory(
        relationship_uuid=pinned_uuid,
        scope=scope,
        reason="temporary hold",
        pinned_by="operator:audit",
    )
    with_pin = await client.search(query="vendor onboarding step checklist", scope=scope, limit=5)
    assert pinned_uuid in [result.relationship_uuid for result in with_pin]

    result = await client.unpin_memory(
        relationship_uuid=pinned_uuid,
        scope=scope,
        reason="hold released",
        pinned_by="operator:audit",
    )
    assert result.pinned is False
    without_pin = await client.search(query="vendor onboarding step checklist", scope=scope, limit=5)
    assert pinned_uuid not in [item.relationship_uuid for item in without_pin]
    row = client.graph.get_relationship(pinned_uuid)
    assert row.properties.get("pinned") is not True

    receipts = await client.memory_receipts(scope=scope)
    types = [receipt.decision_type for receipt in receipts]
    assert ReceiptDecisionType.MEMORY_PINNED in types
    assert ReceiptDecisionType.MEMORY_UNPINNED in types


@pytest.mark.asyncio
async def test_pin_fail_fasts_and_visibility(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-failfast")
    other_scope = MemoryScope(kind=ScopeKind.USER, scope_id="pin-other")
    client = Memotron(graph_path=tmp_path / "failfast.sqlite", config=_typed_config())
    fact = await client.add_memory(
        subject="Acme",
        predicate="requires",
        object="SOC2 report",
        relationship_type="REQUIRES",
        scope=scope,
        valid_from=NOW - timedelta(days=1),
    )

    with pytest.raises(ValueError, match="does not exist"):
        await client.pin_memory(relationship_uuid="no-such-uuid", scope=scope, reason="r", pinned_by="op")
    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.pin_memory(
            relationship_uuid=fact.relationship_uuid,
            scope=other_scope,
            reason="r",
            pinned_by="op",
        )
    with pytest.raises(ValueError, match="reason cannot be blank"):
        await client.pin_memory(relationship_uuid=fact.relationship_uuid, scope=scope, reason="  ", pinned_by="op")
    with pytest.raises(ValueError, match="pinned_by cannot be blank"):
        await client.pin_memory(relationship_uuid=fact.relationship_uuid, scope=scope, reason="r", pinned_by=" ")
    mentions = next(rel for rel in client.graph.relationships() if rel.type == "MENTIONS")
    with pytest.raises(ValueError, match="MENTIONS relationships cannot be pinned"):
        await client.pin_memory(relationship_uuid=mentions.uuid, scope=scope, reason="r", pinned_by="op")
    with pytest.raises(ValueError, match="is not pinned"):
        await client.unpin_memory(relationship_uuid=fact.relationship_uuid, scope=scope, reason="r", pinned_by="op")

    # A non-visible (superseded) row cannot be pinned, and a pinned row that is
    # later superseded stops surfacing: pins never bypass visibility.
    await client.pin_memory(relationship_uuid=fact.relationship_uuid, scope=scope, reason="hold", pinned_by="op")
    replacement = await client.add_memory(
        subject="Acme",
        predicate="requires",
        object="ISO27001 certification",
        relationship_type="REQUIRES",
        scope=scope,
        valid_from=NOW,
    )
    superseded = client.graph.get_relationship(fact.relationship_uuid)
    assert superseded.properties["status"] == "superseded"
    results = await client.search(query="SOC2 report", scope=scope)
    assert fact.relationship_uuid not in [item.relationship_uuid for item in results]
    profile = await client.profile(scope=scope)
    assert fact.relationship_uuid not in [item.relationship_uuid for item in profile.static_facts]
    with pytest.raises(ValueError, match="not currently visible"):
        await client.pin_memory(
            relationship_uuid=fact.relationship_uuid,
            scope=scope,
            reason="r",
            pinned_by="op",
        )
    assert replacement.relationship_uuid


@pytest.mark.asyncio
async def test_agent_platform_pin_restricted_to_own_writable_scopes(tmp_path) -> None:
    """Platform memory_pin/memory_unpin use memory_forget's authorization: the
    caller's own writable scopes only; project memory is refused."""
    from memotron.agent_memory import AgentMemoryPlatform

    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "platform.sqlite",
        project_id="pin-project",
    )
    registration = platform.register_agent(agent_id="pinner", agent_name="Pinner Agent")
    assert registration.agent_id == "pinner"
    remembered = await platform.memory_remember(
        agent_id="pinner",
        subject="pinner",
        predicate="prefers",
        object="deterministic test data",
        relationship_type="PREFERS",
    )
    pin = await platform.memory_pin(
        agent_id="pinner",
        relationship_uuid=remembered.relationship_uuid,
        reason="agent-critical fact",
    )
    assert pin.pinned is True
    assert pin.pinned_by == "agent:pinner"
    unpin = await platform.memory_unpin(
        agent_id="pinner",
        relationship_uuid=remembered.relationship_uuid,
        reason="no longer critical",
    )
    assert unpin.pinned is False

    with pytest.raises(ValueError, match="cannot pin project memory"):
        await platform.memory_pin(
            agent_id="pinner",
            relationship_uuid=remembered.relationship_uuid,
            reason="r",
            scope="project",
        )
    with pytest.raises(ValueError, match="cannot unpin project memory"):
        await platform.memory_unpin(
            agent_id="pinner",
            relationship_uuid=remembered.relationship_uuid,
            reason="r",
            scope="project",
        )


# ---------------------------------------------------------------------------
# WS-24 — per-type budget CAPS replace the T24 per-type floors
#
# The floor guaranteed a named type a minimum share of the profile budget.  That
# is a claim on context attached to a TYPE, and it is what turned a
# classification error into a retrieval failure: 148 field-inventory facts landed
# in `identity` and held the share `identity` was promised.  The cap is the
# opposite lever — it can only ever shrink a dominant type's claim.
# ---------------------------------------------------------------------------


def test_dominant_type_share_is_capped(tmp_path) -> None:
    """A Motive that would hand one type 90% of the budget is capped back."""
    client = Memotron(graph_path=tmp_path / "cap.sqlite", config=_typed_config())
    motive = Motive(
        name="preference-heavy",
        goal="Prefer preferences",
        allowed_memory_types=(MemoryType.PREFERENCE,),
        retrieval_budget_share=0.9,
    )
    shares = client._budget_shares_for_types(["preference", "directive", "state"], motive, ProfilePolicy())
    assert shares["preference"] <= 0.40 + 1e-9, shares
    assert abs(sum(shares.values()) - 1.0) < 1e-9
    # The excess went to the OTHER types rather than being discarded, and no
    # type was starved to fund a floor.
    assert shares["directive"] > 0.05 and shares["state"] > 0.05, shares


def test_cap_is_configurable_and_disablable(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "cap2.sqlite", config=_typed_config())
    motive = Motive(
        name="directive-heavy",
        goal="Prefer directives",
        allowed_memory_types=(MemoryType.DIRECTIVE,),
        retrieval_budget_share=0.9,
    )
    tight = client._budget_shares_for_types(
        ["preference", "directive", "state"],
        motive,
        ProfilePolicy(max_type_budget_share=0.5),
    )
    assert tight["directive"] <= 0.5 + 1e-9, tight
    uncapped = client._budget_shares_for_types(
        ["preference", "directive", "state"],
        motive,
        ProfilePolicy(max_type_budget_share=None),
    )
    assert uncapped["directive"] > 0.85, uncapped


def test_cap_is_inert_when_it_cannot_be_satisfied(tmp_path) -> None:
    """A cap that no assignment can satisfy leaves the shares alone.

    With one type present there is nowhere to move the excess, and with a cap
    below ``1/len(types)`` every type is over it — silently collapsing to the
    even split in either case would be a lie about what the cap did.
    """
    client = Memotron(graph_path=tmp_path / "cap3.sqlite", config=_typed_config())
    single = client._budget_shares_for_types(["anchor"], None, ProfilePolicy())
    assert single == {"anchor": 1.0}
    impossible = client._budget_shares_for_types(
        ["anchor", "directive", "state"], None, ProfilePolicy(max_type_budget_share=0.2)
    )
    assert abs(sum(impossible.values()) - 1.0) < 1e-9
    assert all(abs(value - 1 / 3) < 1e-9 for value in impossible.values()), impossible


def test_cap_validators_fail_fast() -> None:
    with pytest.raises(ValueError):
        ProfilePolicy(max_type_budget_share=0.0)
    with pytest.raises(ValueError):
        ProfilePolicy(max_type_budget_share=1.5)
    assert ProfilePolicy().max_type_budget_share == 0.40
    assert ProfilePolicy(max_type_budget_share=None).max_type_budget_share is None


# ---------------------------------------------------------------------------
# T25 — staleness lifecycle defaults
# ---------------------------------------------------------------------------


async def _add_state_row(
    client: Memotron,
    scope: MemoryScope,
    *,
    subject: str,
    object_value: str,
    age_days: int,
) -> str:
    result = await client.add_memory(
        subject=subject,
        predicate="tracks",
        object=object_value,
        relationship_type="TRACKS",
        scope=scope,
        valid_from=NOW - timedelta(days=age_days),
    )
    return result.relationship_uuid


@pytest.mark.asyncio
async def test_old_unused_state_row_prunes_to_ghost_and_restores(tmp_path) -> None:
    """Default config: a 40-day-old, never-used state row stale-prunes to a
    restorable ghost; a matching search restores it."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="stale-state")
    client = Memotron(graph_path=tmp_path / "stale.sqlite", config=_typed_config())
    stale_uuid = await _add_state_row(
        client, scope, subject="Sprint board", object_value="vendor migration blocked", age_days=40
    )

    await client.run_dream_job(job_name="pruning-default", now=NOW)

    row = client.graph.get_relationship(stale_uuid)
    assert row.properties["status"] == "pruned"
    assert row.properties["pruned_reason"] == "stale_unused"
    ghosts = client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)
    assert stale_uuid in [ghost.relationship_uuid for ghost in ghosts]
    receipts = await client.memory_receipts(scope=scope)
    stale_receipts = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.PRUNING_RELATIONSHIP_PRUNED
        and receipt.decision_reason == "stale_unused"
    ]
    assert stale_receipts

    # A matching current retrieval restores the ghost.
    results = await client.search(query="vendor migration blocked", scope=scope)
    assert stale_uuid in [item.relationship_uuid for item in results]
    restored = client.graph.get_relationship(stale_uuid)
    assert restored.properties["status"] == "active"
    receipts = await client.memory_receipts(scope=scope)
    assert any(receipt.decision_type == ReceiptDecisionType.PRUNING_GHOST_RESTORED for receipt in receipts)


@pytest.mark.asyncio
async def test_same_age_identity_row_untouched_by_default(tmp_path) -> None:
    """The default staleness map covers ONLY "state" — identity is immortal."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="stale-identity")
    client = Memotron(graph_path=tmp_path / "identity.sqlite", config=_typed_config())
    identity = await client.add_memory(
        subject="User 9",
        predicate="is a",
        object="platinum passholder",
        relationship_type="IS_A",
        scope=scope,
        valid_from=NOW - timedelta(days=40),
    )
    await client.run_dream_job(job_name="pruning-default", now=NOW)
    row = client.graph.get_relationship(identity.relationship_uuid)
    assert row.properties["status"] == "active"


@pytest.mark.asyncio
async def test_use_event_within_window_blocks_staleness(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="stale-used")
    client = Memotron(graph_path=tmp_path / "used.sqlite", config=_typed_config())
    used_uuid = await _add_state_row(
        client, scope, subject="Task board", object_value="release checklist half done", age_days=40
    )
    await client.record_memory_use(
        relationship_uuid=used_uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="task-1",
        idempotency_key="task-1:cite:0",
        used_at=NOW - timedelta(days=2),
    )
    await client.run_dream_job(job_name="pruning-default", now=NOW)
    row = client.graph.get_relationship(used_uuid)
    assert row.properties["status"] == "active"


@pytest.mark.asyncio
async def test_pinned_and_correction_rows_never_stale_pruned(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="stale-protected")
    client = Memotron(graph_path=tmp_path / "protected.sqlite", config=_typed_config())

    pinned_uuid = await _add_state_row(
        client, scope, subject="Pinned board", object_value="permanent working note", age_days=40
    )
    await client.pin_memory(relationship_uuid=pinned_uuid, scope=scope, reason="hold", pinned_by="operator:audit")

    corrected_source = await _add_state_row(
        client, scope, subject="Corrected board", object_value="stale draft state", age_days=45
    )
    correction = await client.correct_memory(
        relationship_uuid=corrected_source,
        corrected_object="verified current state",
        scope=scope,
        valid_from=NOW - timedelta(days=40),
    )

    await client.run_dream_job(job_name="pruning-default", now=NOW)

    assert client.graph.get_relationship(pinned_uuid).properties["status"] == "active"
    assert client.graph.get_relationship(correction.corrected_relationship_uuid).properties["status"] == "active"
    held_reasons = {
        str(decision.details.get("eligibility_reason"))
        for decision in await client.dream_decisions()
        if decision.decision_type == "pruning_retention_held"
    }
    assert "pinned" in held_reasons
    assert "operator_correction" in held_reasons


@pytest.mark.asyncio
async def test_stale_opt_in_for_another_type(tmp_path) -> None:
    """Operators can opt other types into staleness; the config validates keys."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="stale-optin")
    pruning = PruningPolicy(
        stale_after_seconds={"state": 30 * 86400, "preference": 30 * 86400},
        retention=default_config().pruning.retention,
    )
    client = Memotron(graph_path=tmp_path / "optin.sqlite", config=_typed_config(pruning))
    preference = await client.add_memory(
        subject="User 9",
        predicate="prefers",
        object="early morning park entry",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=NOW - timedelta(days=40),
    )
    await client.run_dream_job(job_name="pruning-default", now=NOW)
    row = client.graph.get_relationship(preference.relationship_uuid)
    assert row.properties["status"] == "pruned"
    assert row.properties["pruned_reason"] == "stale_unused"

    with pytest.raises(ValueError):
        PruningPolicy(stale_after_seconds={"not-a-type": 60})
    with pytest.raises(ValueError):
        PruningPolicy(stale_after_seconds={"state": 0})


# ---------------------------------------------------------------------------
# WS-19 T22 — per-memory visibility (agent allowlist)
# ---------------------------------------------------------------------------


async def _seed_visibility_rows(client: Memotron, scope: MemoryScope) -> tuple[str, str]:
    """One to-be-restricted fact plus one open fact; returns (restricted, open)."""
    restricted = await client.add_memory(
        subject="Team",
        predicate="prefers",
        object="alpha-only deploy key rotation runbook",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )
    open_fact = await client.add_memory(
        subject="Team",
        predicate="prefers",
        object="shared deploy freeze calendar",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )
    return restricted.relationship_uuid, open_fact.relationship_uuid


@pytest.mark.asyncio
async def test_visibility_restricted_row_fail_closed_for_agents_open_for_operators(
    tmp_path,
) -> None:
    """A row restricted to agent A disappears from agent B's search, semantic
    search, profile, evidence, and utility reads — while agent A and the
    operator context (reader None) still see everything.  Clearing restores."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="vis-core")
    client = Memotron(graph_path=tmp_path / "vis.sqlite", config=_typed_config())
    restricted_uuid, open_uuid = await _seed_visibility_rows(client, scope)

    result = await client.set_memory_visibility(
        relationship_uuid=restricted_uuid,
        scope=scope,
        agents=("agent-a",),
        reason="a-only working note",
        set_by="operator:test",
    )
    assert result.visibility_agents == ("agent-a",)
    row = client.graph.get_relationship(restricted_uuid)
    assert row.properties["visibility_agents"] == ["agent-a"]

    query = "deploy key rotation runbook"
    # search: fail-closed for B, open for A and for the operator context.
    for reader, expected in (("agent-b", False), ("agent-a", True), (None, True)):
        hits = [
            item.relationship_uuid for item in await client.search(query=query, scope=scope, reader_agent_id=reader)
        ]
        assert (restricted_uuid in hits) is expected, reader
        assert open_uuid in hits  # the open row is never affected

    semantic_b = await client.semantic_search(query=query, scope=scope, reader_agent_id="agent-b")
    assert restricted_uuid not in [item.relationship_uuid for item in semantic_b]
    semantic_a = await client.semantic_search(query=query, scope=scope, reader_agent_id="agent-a")
    assert restricted_uuid in [item.relationship_uuid for item in semantic_a]

    profile_b = await client.profile(scope=scope, reader_agent_id="agent-b")
    profile_uuids_b = [fact.relationship_uuid for fact in [*profile_b.static_facts, *profile_b.dynamic_facts]]
    assert restricted_uuid not in profile_uuids_b
    assert open_uuid in profile_uuids_b
    profile_operator = await client.profile(scope=scope)
    assert restricted_uuid in [
        fact.relationship_uuid for fact in [*profile_operator.static_facts, *profile_operator.dynamic_facts]
    ]

    with pytest.raises(ValueError, match="not visible to agent"):
        await client.memory_evidence(relationship_uuid=restricted_uuid, scope=scope, reader_agent_id="agent-b")
    evidence_a = await client.memory_evidence(relationship_uuid=restricted_uuid, scope=scope, reader_agent_id="agent-a")
    assert evidence_a.relationship_uuid == restricted_uuid
    evidence_operator = await client.memory_evidence(relationship_uuid=restricted_uuid, scope=scope)
    assert evidence_operator.relationship_uuid == restricted_uuid

    # utility projections hide the restricted row from other agents too.
    await client.record_memory_use(
        relationship_uuid=restricted_uuid,
        scope=scope,
        kind=UseEventKind.INJECTED,
        task_run_id="vis-task",
        idempotency_key="vis-task:inject:0",
        rank=0,
        retrieval_score=1.0,
        candidate_set_size=1,
        context_budget_competition=0,
        retrieval_policy_digest="digest",
    )
    assert [
        item.relationship_uuid for item in await client.memory_utility(scope=scope, reader_agent_id="agent-b")
    ] == []
    assert restricted_uuid in [
        item.relationship_uuid for item in await client.memory_utility(scope=scope, reader_agent_id="agent-a")
    ]
    assert restricted_uuid in [item.relationship_uuid for item in await client.memory_utility(scope=scope)]

    # Clearing restores scope-default visibility for everyone.
    cleared = await client.set_memory_visibility(
        relationship_uuid=restricted_uuid,
        scope=scope,
        agents=None,
        reason="share again",
        set_by="operator:test",
    )
    assert cleared.visibility_agents is None
    restored = await client.search(query=query, scope=scope, reader_agent_id="agent-b")
    assert restricted_uuid in [item.relationship_uuid for item in restored]


@pytest.mark.asyncio
async def test_visibility_receipts_and_fail_fasts(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="vis-receipts")
    client = Memotron(graph_path=tmp_path / "vis-receipts.sqlite", config=_typed_config())
    restricted_uuid, _ = await _seed_visibility_rows(client, scope)

    await client.set_memory_visibility(
        relationship_uuid=restricted_uuid,
        scope=scope,
        agents=("agent-a", "agent-c"),
        reason="restricted working set",
        set_by="operator:test",
    )
    receipts = await client.memory_receipts(scope=scope)
    visibility_receipts = [
        receipt for receipt in receipts if receipt.decision_type == ReceiptDecisionType.MEMORY_VISIBILITY_SET
    ]
    assert len(visibility_receipts) == 1
    receipt = visibility_receipts[0]
    assert receipt.relationship_uuid == restricted_uuid
    assert receipt.decision_result == "recorded"
    # Hash-bracketed row mutation.  WS-23 M2: `visibility_agents` is IN the
    # canonical state tuple, so setting an allowlist MOVES the hash — an
    # out-of-band ACL write can no longer leave every chain verifying.
    assert receipt.graph_state_hash_before
    assert receipt.graph_state_hash_after != receipt.graph_state_hash_before
    payload = json.loads(receipt.event_payload)
    assert payload == {
        "set_by": "operator:test",
        "visibility_agents": ["agent-a", "agent-c"],
    }

    # Fail-fasts mirror pin_memory.
    with pytest.raises(ValueError, match="relationship does not exist"):
        await client.set_memory_visibility(
            relationship_uuid="missing",
            scope=scope,
            agents=("a",),
            reason="x",
            set_by="op",
        )
    with pytest.raises(ValueError, match="reason cannot be blank"):
        await client.set_memory_visibility(
            relationship_uuid=restricted_uuid,
            scope=scope,
            agents=("a",),
            reason="  ",
            set_by="op",
        )
    with pytest.raises(ValueError, match="pinned_by cannot be blank"):
        await client.set_memory_visibility(
            relationship_uuid=restricted_uuid,
            scope=scope,
            agents=("a",),
            reason="x",
            set_by="  ",
        )
    with pytest.raises(ValueError, match="at least one non-blank agent id"):
        await client.set_memory_visibility(
            relationship_uuid=restricted_uuid,
            scope=scope,
            agents=(),
            reason="x",
            set_by="op",
        )
    with pytest.raises(ValueError, match="at least one non-blank agent id"):
        await client.set_memory_visibility(
            relationship_uuid=restricted_uuid,
            scope=scope,
            agents=("  ",),
            reason="x",
            set_by="op",
        )
    other_scope = MemoryScope(kind=ScopeKind.USER, scope_id="vis-other")
    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.set_memory_visibility(
            relationship_uuid=restricted_uuid,
            scope=other_scope,
            agents=("a",),
            reason="x",
            set_by="op",
        )
    mentions = next(relationship for relationship in client.graph.relationships() if relationship.type == "MENTIONS")
    with pytest.raises(ValueError, match="MENTIONS relationships cannot be pinned"):
        await client.set_memory_visibility(
            relationship_uuid=mentions.uuid,
            scope=scope,
            agents=("a",),
            reason="x",
            set_by="op",
        )
    # Clearing an unrestricted row is a no-op error, not a silent success.
    open_row = await client.add_memory(
        subject="Team",
        predicate="prefers",
        object="open fact",
        relationship_type="PREFERS",
        scope=scope,
    )
    with pytest.raises(ValueError, match="no visibility restriction to clear"):
        await client.set_memory_visibility(
            relationship_uuid=open_row.relationship_uuid,
            scope=scope,
            agents=None,
            reason="x",
            set_by="op",
        )
    # A retired row cannot be restricted.
    await client.forget_memory(relationship_uuid=open_row.relationship_uuid, reason="gone")
    with pytest.raises(ValueError, match="not currently visible"):
        await client.set_memory_visibility(
            relationship_uuid=open_row.relationship_uuid,
            scope=scope,
            agents=("a",),
            reason="x",
            set_by="op",
        )


@pytest.mark.asyncio
async def test_pinned_but_restricted_row_does_not_leak(tmp_path) -> None:
    """Pins guarantee retrieval WITHIN visibility: the pinned sweep and the
    profile pin lead both honor the allowlist, so a pinned-but-restricted row
    is guaranteed for its listed agent and invisible to everyone else."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="vis-pin")
    client = Memotron(graph_path=tmp_path / "vis-pin.sqlite", config=_typed_config())
    pinned_uuid = await _seed_outscored_pin(client, scope)
    await client.pin_memory(
        relationship_uuid=pinned_uuid,
        scope=scope,
        reason="must stay in context",
        pinned_by="operator:audit",
    )
    await client.set_memory_visibility(
        relationship_uuid=pinned_uuid,
        scope=scope,
        agents=("agent-a",),
        reason="a-only safety directive",
        set_by="operator:audit",
    )

    query = "vendor onboarding step checklist"
    # Pinned sweep honors the allowlist: reserved slot for A, nothing for B.
    hits_a = await client.search(query=query, scope=scope, limit=5, reader_agent_id="agent-a")
    assert pinned_uuid in [item.relationship_uuid for item in hits_a]
    assert next(item for item in hits_a if item.relationship_uuid == pinned_uuid).pinned is True
    hits_b = await client.search(query=query, scope=scope, limit=5, reader_agent_id="agent-b")
    assert pinned_uuid not in [item.relationship_uuid for item in hits_b]
    assert len(hits_b) == 5  # B still gets a full result page of open rows

    # Profile pin lead honors the allowlist the same way.
    profile_a = await client.profile(
        scope=scope,
        policy=ProfilePolicy(max_static_facts=10, max_dynamic_facts=0),
        reader_agent_id="agent-a",
    )
    assert pinned_uuid in [fact.relationship_uuid for fact in profile_a.static_facts]
    profile_b = await client.profile(
        scope=scope,
        policy=ProfilePolicy(max_static_facts=10, max_dynamic_facts=0),
        reader_agent_id="agent-b",
    )
    assert pinned_uuid not in [fact.relationship_uuid for fact in profile_b.static_facts]
    # Operator context still sees the pin lead.
    profile_operator = await client.profile(scope=scope, policy=ProfilePolicy(max_static_facts=10, max_dynamic_facts=0))
    assert pinned_uuid in [fact.relationship_uuid for fact in profile_operator.static_facts]
