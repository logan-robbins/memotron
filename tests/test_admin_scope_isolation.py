"""One tenant's console must not read another tenant's scopes — or their contents.

`--no-admin-writes` (#192) closed the admin surface's WRITES. It gates nothing on the
read side, and the read side authenticates nobody: `principal` is a class attribute, so
every caller that can reach the port is the same ADMIN principal.

Until 2026-09-08 `build_demo_control_plane` registered every scope
`discover_graph_scopes` could find in the store. `_request_policy` — the only gate on
`/api/scopes` and `/api/graph` — consults exactly that registry, so a neighbouring
tenant's scope key, its relationship COUNT, and through `/api/graph?scope=<neighbour>`
its memory CONTENT were all readable. Reproduced against the handler `main()` builds,
not a convenient stand-in:

    /api/scopes -> ['agent:a-tenant-under-test',
                    'tenant:secret-neighbour',      <-- rels=1
                    'tenant:tenant-under-test']

    /api/graph?scope=tenant:secret-neighbour
        -> nodes: ['dark mode', 'Neighbour Team', 'Neighbour Team prefers dark mode']

**Every test here carries a positive control.** A filter that returns nothing satisfies
"the neighbour is absent" trivially, and that failure mode is invisible in an assertion
written only in the negative — which is how the console could be "secured" into
uselessness and pass.

The handler is built by replicating `main()`'s construction rather than by starting a
server: the leak lives in how `main()` wires the control plane, so a harness that wires
it differently would prove something about the harness.
"""

from __future__ import annotations

import asyncio
import pathlib

import pytest

from memotron import Memotron, MemoryScope, PrincipalRole, ScopeKind
from memotron.admin_server import (
    MemoryGraphHandler,
    agent_scope,
    apply_persisted_tenant_policy,
    build_demo_control_plane,
    build_demo_principal,
    discover_graph_scopes,
)

MINE = MemoryScope(kind=ScopeKind.TENANT, scope_id="tenant-under-test")
THEIRS = MemoryScope(kind=ScopeKind.TENANT, scope_id="secret-neighbour")
MY_AGENT = "a-tenant-under-test"


def _handler_as_main_builds_it(client: Memotron, *, default_scope: MemoryScope, tenant_id: str):
    """Replicate `admin_server.main()`'s wiring, then instantiate without a socket."""
    handler = type("ScopeIsolationProbe", (MemoryGraphHandler,), {})
    handler.client = client
    handler.runtime_client = client
    handler.default_scope = default_scope
    handler.tenant_id = tenant_id
    handler.agent_ids = (MY_AGENT,)
    handler.configured_scopes = (default_scope,)
    handler.no_admin_writes = True
    handler.control_plane = build_demo_control_plane(
        client=client,
        default_scope=default_scope,
        tenant_id=tenant_id,
        agent_id=MY_AGENT,
        extra_scopes=(agent_scope(MY_AGENT),),
    )
    client.control_plane = handler.control_plane
    apply_persisted_tenant_policy(client=client, tenant_id=tenant_id)
    handler.control_plane = client.control_plane
    handler.principal = build_demo_principal(
        principal_id="admin",
        tenant_id=tenant_id,
        agent_id=MY_AGENT,
        default_scope=default_scope,
        role=PrincipalRole.ADMIN,
    )
    return object.__new__(handler)


def _two_tenants(tmp_path: pathlib.Path) -> Memotron:
    """Two tenants, each holding real memory, each with a registered agent."""
    client = Memotron(graph_path=tmp_path / "two-tenants.sqlite")

    async def seed() -> None:
        await client.add_memory(
            subject="My Team",
            predicate="prefers",
            object="light mode",
            relationship_type="PREFERS",
            scope=MINE,
            confidence=0.9,
        )
        await client.add_memory(
            subject="Neighbour Team",
            predicate="prefers",
            object="dark mode",
            relationship_type="PREFERS",
            scope=THEIRS,
            confidence=0.9,
        )

    asyncio.run(seed())
    for tenant in ("tenant-under-test", "secret-neighbour"):
        client.graph.register_tenant_agent(tenant_id=tenant, agent_id=f"a-{tenant}", name=f"A {tenant}")
    return client


def test_api_scopes_does_not_list_another_tenants_scope(tmp_path: pathlib.Path) -> None:
    client = _two_tenants(tmp_path)
    try:
        probe = _handler_as_main_builds_it(client, default_scope=MINE, tenant_id="tenant-under-test")
        keys = [item["key"] for item in probe._scope_payload()]

        assert "tenant:tenant-under-test" in keys, (
            "positive control: the console lost its OWN scope, so the assertion below "
            f"would pass on an empty list and prove nothing. Got {keys}"
        )
        assert THEIRS.key not in keys, f"a neighbouring tenant's scope is listed: {keys}"
    finally:
        client.graph.close()


def test_the_listed_scope_still_carries_its_real_relationship_count(tmp_path: pathlib.Path) -> None:
    """The count is what makes the listing useful, and what made the leak worse than
    ids alone — a neighbour's row count is itself information. Pin that OUR count
    survives, so the fix cannot be "return an empty shell for everyone"."""
    client = _two_tenants(tmp_path)
    try:
        probe = _handler_as_main_builds_it(client, default_scope=MINE, tenant_id="tenant-under-test")
        mine = [item for item in probe._scope_payload() if item["key"] == MINE.key]
        assert mine and mine[0]["relationship_count"] == 1, (
            f"the console's own scope lost its relationship count: {mine}"
        )
    finally:
        client.graph.close()


def test_api_graph_REFUSES_another_tenants_content(tmp_path: pathlib.Path) -> None:
    """The severe half. Listing the key is disclosure; returning the memory is worse.

    `--no-admin-writes` does not help here — it gates writes, and this is a read.
    """
    client = _two_tenants(tmp_path)
    try:
        probe = _handler_as_main_builds_it(client, default_scope=MINE, tenant_id="tenant-under-test")

        own = probe._graph_payload({"scope": [MINE.key]})
        own_names = {str(node.get("name") or node.get("label")) for node in own.get("nodes", [])}
        assert any("light mode" in name for name in own_names), (
            "positive control: the console cannot read its OWN graph, so the refusal "
            f"below proves nothing. Got {own_names}"
        )

        with pytest.raises(ValueError) as excinfo:
            probe._graph_payload({"scope": [THEIRS.key]})
        assert "not registered" in str(excinfo.value), (
            f"refused, but for an unexpected reason — check it is the scope guard: {excinfo.value}"
        )
    finally:
        client.graph.close()


def test_discover_graph_scopes_without_a_tenant_id_is_still_STORE_WIDE(tmp_path: pathlib.Path) -> None:
    """The permissive behaviour is kept and NAMED, not deleted.

    An operator tool that genuinely wants every scope still has one; what changed is
    that serving a tenant now requires saying which tenant. If this ever starts
    filtering by default, a caller that relies on the whole store goes quietly blind
    rather than failing.
    """
    client = _two_tenants(tmp_path)
    try:
        keys = {scope.key for scope in discover_graph_scopes(client, MINE)}
        assert THEIRS.key in keys, f"the unbounded form stopped being store-wide: {keys}"

        bounded = {scope.key for scope in discover_graph_scopes(client, MINE, tenant_id="tenant-under-test")}
        assert THEIRS.key not in bounded and MINE.key in bounded, f"bounded form is wrong: {bounded}"
    finally:
        client.graph.close()


def test_a_demo_console_with_NO_registered_tenants_still_sees_its_scope(tmp_path: pathlib.Path) -> None:
    """The blast radius, pinned. preview/stage/load launch with
    `--scope customer:wdw:pinnacle-events` against a demo graph that registers NO
    tenants at all, so a filter keyed on tenant ownership could blank them entirely.

    It does not, because `default_scope` is included unconditionally — and a
    `customer:` scope is never attributable to a tenant, so it would otherwise be
    denied. Measured 2026-09-08: the demo graph holds exactly that one scope.
    """
    demo_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:pinnacle-events")
    client = Memotron(graph_path=tmp_path / "demo.sqlite")
    try:

        async def seed() -> None:
            await client.add_memory(
                subject="Pinnacle Guest",
                predicate="prefers",
                object="early dining",
                relationship_type="PREFERS",
                scope=demo_scope,
                confidence=0.9,
            )

        asyncio.run(seed())
        assert client.graph.known_tenant_ids() == (), "the demo graph is supposed to register no tenants"

        keys = {scope.key for scope in discover_graph_scopes(client, demo_scope, tenant_id="wdpr-demo")}
        assert demo_scope.key in keys, (
            f"the demo console lost its only scope — preview/stage/load would render an empty graph. Got {keys}"
        )
    finally:
        client.graph.close()


# -- /api/overview's evolution block (2026-09-08) ----------------------------------
#
# The scope filtering above bounds `/api/scopes` and `/api/graph`. `/api/overview`
# reaches the same data by a DIFFERENT call path: `_overview_payload` asked for
# `dream_decisions(limit=1)` with no scope at all, and `DreamDecisionRecord` carries
# `scope`, `summary` and `subject_name` -- so the newest decision in the STORE was
# returned to any caller, whichever tenant it belonged to.
#
# Found by probing deployed `latest` unauthenticated from off-cluster, twice, and
# getting a different neighbour tenant's memory content each time:
#
#     probe 1 -> scope=probe-tenant-alpha  subject_name="durable KEK gates Postgres..."
#     probe 2 -> scope=probe-tenant-bravo  subject_name="consolidation-default cons..."
#
# It survived #200 because #200 bounded the scope REGISTRY, and this route never
# consults it. `dream_history` is deliberately NOT covered here: `DreamJobRunRecord`
# has no scope field and no scoped variant exists, so it is a different question.


def _decide(client: Memotron, *, scope: MemoryScope, agent_id: str, summary: str, when: str) -> None:
    from datetime import datetime

    from memotron.models import DreamDecisionRecord, DreamJobKind

    client.graph.record_dream_decision(
        DreamDecisionRecord(
            ran_at=datetime.fromisoformat(when),
            job_name="pruning-default",
            job_kind=DreamJobKind.PRUNING,
            agent_id=agent_id,
            agent_name=agent_id,
            agent_scope=scope,
            decision_type="retain",
            summary=summary,
            subject_name=summary,
            scope=scope,
        )
    )


def _overview(client: Memotron) -> dict:
    handler = _handler_as_main_builds_it(client, default_scope=MINE, tenant_id="tenant-under-test")
    return asyncio.run(handler._overview_payload({}))


def test_api_overview_does_not_return_another_tenants_dream_decision(tmp_path: pathlib.Path) -> None:
    """The neighbour's decision is NEWER, so an unscoped `limit=1` must return it."""
    client = _two_tenants(tmp_path)
    _decide(client, scope=MINE, agent_id=MY_AGENT, summary="mine, older", when="2026-01-01T00:00:00+00:00")
    _decide(
        client,
        scope=THEIRS,
        agent_id="a-secret-neighbour",
        summary="THEIRS, newer",
        when="2026-06-01T00:00:00+00:00",
    )

    decision = _overview(client)["evolution"]["latest_decision"]

    assert decision is not None, "positive control: a decision in my own scope must still be reported"
    assert decision["scope"]["scope_id"] == "tenant-under-test", decision["scope"]
    assert "THEIRS" not in decision["summary"]
    assert "THEIRS" not in (decision["subject_name"] or "")


def test_api_overview_still_reports_MY_newest_decision(tmp_path: pathlib.Path) -> None:
    """The positive control, separately: a guard that returned nothing would pass the
    test above and leave the console blank."""
    client = _two_tenants(tmp_path)
    _decide(client, scope=MINE, agent_id=MY_AGENT, summary="mine, older", when="2026-01-01T00:00:00+00:00")
    _decide(client, scope=MINE, agent_id=MY_AGENT, summary="mine, NEWEST", when="2026-07-01T00:00:00+00:00")

    decision = _overview(client)["evolution"]["latest_decision"]

    assert decision is not None
    assert decision["summary"] == "mine, NEWEST"
