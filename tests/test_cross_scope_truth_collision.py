"""A write in one scope must never touch a memory in another scope.

**This is a proven cross-tenant destructive write, not a hypothetical.** Reproduced twice
against `Memotron(graph_path=":memory:")` through the public SDK, with no privileged
access, no crafted credential, and no LLM:

    distinct scopes? True | tenant:acme / tenant:acme:roadmap
    victim BEFORE : active
    victim AFTER  : superseded | superseded_by= 8666ba39
    ACTIVE rows left in tenant:acme = 0

The mechanism is a truth key built by UNESCAPED concatenation. `dreaming/_identity.py`
composes `f"{scope_key}:{subject}:{predicate}"`, and `normalize_key` (`storage/base.py`)
casefolds the result. So the tuple is not recoverable from the string:

    scope "tenant:acme"          + subject "roadmap:q3"  ->  tenant:acme:roadmap:q3:...
    scope "tenant:acme:roadmap"  + subject "q3"          ->  tenant:acme:roadmap:q3:...

`scope_id` permits this because `MemoryScope.normalize_scope_id` only `.strip()`s it, while
`normalize_agent_id` properly validates a charset. And the three reads that consume the key --
`find_active_truth_relationships`, `find_active_relationships_by_truth_prefix`,
`relationships_for_truth_prefix` -- take NO scope argument, so nothing downstream can recover
the isolation the key lost.

**The second variant needs no crafted input at all.** `casefold()` makes the truth plane
case-insensitive while scope matching is case-sensitive everywhere else, so two tenants
onboarded as `Acme` and `acme` are distinct scopes sharing one truth slot. That is not an
attack; it is data loss waiting for an onboarding coincidence.

It is a WRITE, not a read leak. The supersession path retires the foreign row and sets its
`valid_to`; the reinforce path mutates the foreign row's counters and then raises, losing the
caller's own write. Worse for auditing: the receipt brackets the mutation with
`graph_state_hash` for the CALLER's scope, so a receipt records a mutation with a null state
delta -- exactly what widening that hash tuple was meant to prevent.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memotron import Memotron, MemoryScope, ScopeKind


def _tenant(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.TENANT, scope_id=scope_id)


def _active_in(client: Memotron, scope: MemoryScope) -> list:
    return [
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.properties.get("status") == "active" and relationship.type != "MENTIONS"
    ]


@pytest.mark.asyncio
async def test_a_write_in_one_scope_cannot_supersede_a_memory_in_another(tmp_path: Path) -> None:
    """THE REGRESSION. Two legal scopes, two legal subjects, ordinary `add_memory` calls.

    `REQUIRES` is used because it is SINGLE_ACTIVE in the default instruction set, which is
    what makes the supersession path run at all.
    """
    client = Memotron(graph_path=tmp_path / "collision.sqlite")
    victim_scope = _tenant("acme")
    attacker_scope = _tenant("acme:roadmap")
    assert victim_scope.key != attacker_scope.key, "the two scopes must be distinct to prove anything"

    victim = await client.add_memory(
        scope=victim_scope,
        subject="roadmap:q3",
        predicate="REQUIRES",
        object="ryan approval",
        relationship_type="REQUIRES",
    )
    assert _active_in(client, victim_scope), "precondition: the victim's memory must start active"

    await client.add_memory(
        scope=attacker_scope,
        subject="q3",
        predicate="REQUIRES",
        object="mallory approval",
        relationship_type="REQUIRES",
    )

    after = client.graph.get_relationship(victim.relationship_uuid)
    assert after.properties.get("status") == "active", (
        f"a write in {attacker_scope.key!r} retired a memory in {victim_scope.key!r} "
        f"(status={after.properties.get('status')!r}, "
        f"superseded_by={after.properties.get('superseded_by_relationship_uuid')!r})"
    )
    assert after.properties.get("superseded_by_relationship_uuid") is None
    assert _active_in(client, victim_scope), (
        f"{victim_scope.key} has no active memories left after a write in another scope"
    )


@pytest.mark.asyncio
async def test_two_scopes_differing_only_by_CASE_do_not_share_a_truth_slot(tmp_path: Path) -> None:
    """The variant that needs no crafted input.

    `normalize_key` casefolds the truth key; scope matching is case-sensitive everywhere else.
    Two tenants onboarded as `Acme` and `acme` are distinct scopes -- and today they destroy
    each other's memory by accident, with ordinary subjects and no colons anywhere.
    """
    client = Memotron(graph_path=tmp_path / "casefold.sqlite")
    upper, lower = _tenant("Acme"), _tenant("acme")
    assert upper.key != lower.key

    victim = await client.add_memory(
        scope=upper,
        subject="budget",
        predicate="REQUIRES",
        object="cfo approval",
        relationship_type="REQUIRES",
    )
    await client.add_memory(
        scope=lower,
        subject="budget",
        predicate="REQUIRES",
        object="nobody",
        relationship_type="REQUIRES",
    )

    after = client.graph.get_relationship(victim.relationship_uuid)
    assert after.properties.get("status") == "active", (
        f"{lower.key!r} retired a memory in {upper.key!r} — the two differ only by case"
    )
    assert _active_in(client, upper)


@pytest.mark.asyncio
async def test_a_write_in_one_scope_cannot_REINFORCE_a_memory_in_another(tmp_path: Path) -> None:
    """The other write path, and the one that also loses the caller's data.

    `_find_reinforce_target` returns the foreign row as an EXACT_UPDATE target, `_apply_reinforce`
    mutates its counters, and then the client raises because the disposition landed in a scope it
    does not own. So the victim's row is tampered with AND the attacker's memory vanishes.
    """
    client = Memotron(graph_path=tmp_path / "reinforce.sqlite")
    victim_scope, other_scope = _tenant("acme"), _tenant("acme:roadmap")

    victim = await client.add_memory(
        scope=victim_scope,
        subject="roadmap:q3",
        predicate="REQUIRES",
        object="ryan approval",
        relationship_type="REQUIRES",
    )
    before = client.graph.get_relationship(victim.relationship_uuid)
    observed_before = before.properties.get("observed_count")

    # Same object value, so the dedup plane treats it as a reinforcement rather than a conflict.
    try:
        await client.add_memory(
            scope=other_scope,
            subject="q3",
            predicate="REQUIRES",
            object="ryan approval",
            relationship_type="REQUIRES",
        )
    except RuntimeError:
        pytest.fail(
            "the write raised because its disposition landed in ANOTHER scope; "
            "the caller's memory was lost and the foreign row was already mutated"
        )

    after = client.graph.get_relationship(victim.relationship_uuid)
    assert after.properties.get("observed_count") == observed_before, (
        f"a write in {other_scope.key!r} mutated observed_count on a row in {victim_scope.key!r}"
    )


@pytest.mark.asyncio
async def test_the_SAME_scope_still_supersedes_normally(tmp_path: Path) -> None:
    """THE POSITIVE CONTROL, and it is not optional.

    Every assertion above is "this must NOT happen". A fix that simply stopped the
    supersession machinery from ever finding an incumbent would satisfy all of them and break
    the core of the memory model. This proves the machinery still works within one scope.
    """
    client = Memotron(graph_path=tmp_path / "control.sqlite")
    scope = _tenant("acme")

    first = await client.add_memory(
        scope=scope,
        subject="roadmap",
        predicate="REQUIRES",
        object="ryan approval",
        relationship_type="REQUIRES",
    )
    await client.add_memory(
        scope=scope,
        subject="roadmap",
        predicate="REQUIRES",
        object="sam approval",
        relationship_type="REQUIRES",
    )

    after = client.graph.get_relationship(first.relationship_uuid)
    assert after.properties.get("status") == "superseded", (
        "within ONE scope a contradicting write must still supersede the incumbent — "
        "if this fails, the fix disabled the dedup plane rather than scoping it"
    )
    assert len(_active_in(client, scope)) == 1


def test_a_colon_in_a_scope_id_is_STILL_LEGAL_and_that_is_deliberate() -> None:
    """The fix is scope-filtered reads, NOT a charset restriction — and this pins the difference.

    The obvious-looking companion fix is to reject `:` in `scope_id`, since the colon is how
    two scopes reach one truth key. It was considered and **rejected on evidence**: colons in
    `scope_id` are a documented, load-bearing shape here. `customer:wdw:pinnacle-events` is the
    demo scope in the README, `docker-compose.local.yml`, `examples/memory_graph_demo.py` and
    `docs/design/23-sso-web-ui.md`, and `test_principal_from_registry_row.py` pins that
    `from_key` splits only on the FIRST colon precisely so the id may contain the rest.

    Rejecting the colon would have broken all of that to fix a defect the scope predicate
    already fixes. Sharing a truth key across scopes is now harmless, so the collision does not
    need to be prevented — only the unscoped read did.
    """
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:pinnacle-events")
    assert scope.scope_id == "wdw:pinnacle-events"
    assert scope.key == "customer:wdw:pinnacle-events"
    assert MemoryScope.from_key(scope.key) == scope
