"""Shaping graph state into the JSON the admin UI reads."""

from __future__ import annotations

from memotron import (
    Memotron,
    MemoryScope,
    ScopeKind,
)
from memotron.agent_memory import (
    default_motive_for_agent_id,
)


def scope_payload_from_client(client: Memotron) -> list[dict]:
    scopes: dict[str, dict] = {}
    for relationship in client.export_graph()["relationships"]:
        if relationship.get("type") == "MENTIONS":
            continue
        props = relationship.get("properties", {})
        scope_kind = props.get("scope_kind")
        scope_id = props.get("scope_id")
        scope_key = props.get("scope_key")
        if not isinstance(scope_kind, str) or not isinstance(scope_id, str) or not isinstance(scope_key, str):
            continue
        entry = scopes.setdefault(
            scope_key,
            {
                "key": scope_key,
                "kind": scope_kind,
                "scope_id": scope_id,
                "relationship_count": 0,
            },
        )
        entry["relationship_count"] += 1
    return sorted(scopes.values(), key=lambda item: item["key"])


async def graph_filter_options(client: Memotron, scope: MemoryScope) -> dict[str, list[str]]:
    types: set[str] = set()
    statuses: set[str] = set()
    subjects: set[str] = set()
    predicates: set[str] = set()
    objects: set[str] = set()
    for relationship in client.export_graph()["relationships"]:
        if relationship.get("type") == "MENTIONS":
            continue
        props = relationship.get("properties", {})
        if props.get("scope_key") != scope.key:
            continue
        value = relationship.get("type")
        if isinstance(value, str) and value:
            types.add(value)
        for key, target in (
            ("status", statuses),
            ("subject", subjects),
            ("predicate", predicates),
            ("object", objects),
        ):
            raw = props.get(key)
            if isinstance(raw, str) and raw.strip():
                target.add(raw.strip())
    view = await client.knowledge_graph(scope=scope, limit=1000, include_demoted=True)
    for node in view.nodes:
        if node.node_type != "memory":
            continue
        if node.relationship_type:
            types.add(node.relationship_type)
        if node.status:
            statuses.add(node.status.value if hasattr(node.status, "value") else str(node.status))
        for key, target in (
            ("subject", subjects),
            ("predicate", predicates),
            ("object", objects),
        ):
            raw = node.properties.get(key)
            if isinstance(raw, str) and raw.strip():
                target.add(raw.strip())
    return {
        "types": sorted(types),
        "statuses": sorted(statuses),
        "subjects": sorted(subjects),
        "predicates": sorted(predicates),
        "objects": sorted(objects),
    }


def scope_belongs_to_tenant(scope: MemoryScope, *, tenant_id: str, agent_ids: frozenset[str]) -> bool:
    """Can this scope be ATTRIBUTED to ``tenant_id`` from what the store records?

    Deliberately default-DENY, because the caller uses it to decide what an
    unauthenticated console may see:

    * ``tenant:<id>``  — owned iff the id matches. This is the one the store answers
      unambiguously, and it is the leak that was reproduced.
    * ``agent:<id>``   — owned iff the id is registered to this tenant in
      ``tenant_agents``. Agent ids are tenant-local, so an id alone proves nothing.
    * ``user:``/``customer:`` — **NOT attributable**. The store records no owner for
      them, so there is no honest way to decide, and guessing in the permissive
      direction is what this function exists to stop. They are reachable only by being
      named: the launch ``--scope`` and anything in ``configured_scopes`` are added by
      the caller regardless of this predicate.

    That last bullet is a real limitation, not an oversight, and it is TRACKED IN #201:
    attributing those kinds needs an owner recorded somewhere, which the scope plane
    does not have (`storage/sqlite/_graph.py` holds no `tenant_id` at all). Until then
    the console shows fewer scopes rather than another tenant's.

    #201 also blocks the END-USER half of #23: signing someone in answers *who is
    calling*, but `MemoryPrincipal.allowed_scope_keys` still has to be filled with
    "this person's `user:` scope and no one else's", and that set is not computable
    from what is stored today. Note the fix cannot be to rename scopes -- `scope_key`
    composes the truth key (`dreaming/_identity.py:178`), so that orphans every
    existing memory.
    """
    if scope.kind == ScopeKind.TENANT:
        return scope.scope_id == tenant_id
    if scope.kind == ScopeKind.AGENT:
        return scope.scope_id in agent_ids
    return False


def discover_graph_scopes(
    client: Memotron,
    default_scope: MemoryScope,
    *,
    tenant_id: str | None = None,
) -> tuple[MemoryScope, ...]:
    """Scopes the console may address. Pass ``tenant_id`` to bound it to one tenant.

    Without ``tenant_id`` this returns EVERY scope in the store, which is what it did
    unconditionally until 2026-09-08. The admin surface authenticates nobody, and
    ``_request_policy`` gates ``/api/scopes`` and ``/api/graph`` on whether the control
    plane knows a scope — so registering everything here published every tenant's scope
    keys, their relationship COUNTS, and, through ``/api/graph?scope=<neighbour>``,
    their memory CONTENT. `--no-admin-writes` gates writes, not reads. Reproduced
    against the handler `main()` builds:

        /api/scopes -> ['agent:a-tenant-under-test',
                        'tenant:secret-neighbour',      <-- rels=1
                        'tenant:tenant-under-test']

    The parameter is optional so the permissive behaviour stays REACHABLE and named
    rather than removed -- an operator tool that genuinely wants the whole store still
    has one -- but every caller that serves a tenant passes it.
    """
    scopes = {default_scope.key: default_scope}
    agent_ids: frozenset[str] = frozenset()
    if tenant_id is not None:
        agent_ids = frozenset(str(item["agent_id"]) for item in client.graph.tenant_agents(tenant_id))
    for item in scope_payload_from_client(client):
        scope = MemoryScope(kind=ScopeKind(item["kind"]), scope_id=item["scope_id"])
        if tenant_id is not None and not scope_belongs_to_tenant(scope, tenant_id=tenant_id, agent_ids=agent_ids):
            continue
        scopes[scope.key] = scope
    return tuple(scopes[key] for key in sorted(scopes))


def persisted_agent_motive(client: Memotron, *, tenant_id: str, agent_id: str) -> str:
    """Return the Motive formation would use for ``agent:<agent_id>``.

    This is the same resolution order ``AgentMemoryPlatform._register_agent_policy``
    applies when it builds the runtime control plane: the persisted
    ``agent_motive_assignments`` row wins, else the role default derived from the
    agent id.
    """
    assignment = client.graph.agent_motive_assignment(
        tenant_id=tenant_id,
        agent_id=agent_id,
    )
    if assignment is not None and str(assignment.get("motive_name") or ""):
        return str(assignment["motive_name"])
    return default_motive_for_agent_id(agent_id)
