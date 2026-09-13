"""The demo tenant the local console stands itself up with.

Builds a control plane, a principal and a launch tenant so the admin server is usable
with no configuration. DEMO_TENANT_ID and DEMO_PROMPT_PACK travel with it. This is
local-affordance code, not product code -- worth keeping visibly separate so it is
never mistaken for the real tenancy path."""

from __future__ import annotations

from memotron import (
    AgentMemoryPolicy,
    DreamAgentConfig,
    Memotron,
    MemoryControlPlane,
    MemoryPrincipal,
    MemoryScope,
    PrincipalRole,
    PromptPack,
    ScopeKind,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.admin_server._payloads import (
    discover_graph_scopes,
    persisted_agent_motive,
)
from memotron.agent_memory import (
    DEFAULT_AGENT_MEMORY_MOTIVE,
    AgentMemoryPlatform,
    agent_memory_bank,
)

DEMO_TENANT_ID = "wdpr-demo"


DEMO_PROMPT_PACK = "support-pack"


def build_demo_control_plane(
    *,
    client: Memotron,
    default_scope: MemoryScope,
    tenant_id: str,
    agent_id: str,
    extra_scopes: tuple[MemoryScope, ...] = (),
    default_motive: str = DEFAULT_AGENT_MEMORY_MOTIVE,
) -> MemoryControlPlane:
    """Build the admin console's control plane from PERSISTED tenant state.

    The console must answer "which Motive governs formation here?" with the same
    answer a real formation run would produce, so its control plane is seeded from
    the graph rather than from a hardcoded persona preset:

    * the Motive catalog is ``agent_memory_bank()`` (the built-in persona bank plus
      the agent-role Motives), so the Motives that actually govern agent scopes are
      resolvable and selectable;
    * every ``agent:<id>`` scope carries its persisted Motive assignment;
    * ``default_motive`` is the neutral all-types agent-memory Motive — the same
      tenant default ``build_agent_memory_control_plane`` uses — because asserting a
      narrow persona preset (e.g. one that admits only ``requirement``) would be an
      affirmative and usually false claim about what formation retains here.

    The project-memory Motive and any active tenant prompt Motive are layered on
    afterwards by ``apply_persisted_tenant_policy``, exactly as the runtime does.
    """
    # `tenant_id=` is what bounds this console to one tenant. Without it every scope in
    # the store is registered here, and `_request_policy` -- the only gate on
    # `/api/scopes` and `/api/graph` -- then admits a neighbour's scope, its
    # relationship count, and its memory CONTENT to any caller that can reach the port.
    scopes_by_key = {scope.key: scope for scope in discover_graph_scopes(client, default_scope, tenant_id=tenant_id)}
    for scope in extra_scopes:
        scopes_by_key[scope.key] = scope
    scopes = tuple(scopes_by_key[key] for key in sorted(scopes_by_key))
    bank = agent_memory_bank()
    resolved_default_motive = default_motive if default_motive in bank.names else DEFAULT_AGENT_MEMORY_MOTIVE

    def scope_motive(scope: MemoryScope) -> str:
        if scope.kind == ScopeKind.AGENT:
            candidate = persisted_agent_motive(
                client,
                tenant_id=tenant_id,
                agent_id=scope.scope_id,
            )
            if candidate in bank.names:
                return candidate
        return resolved_default_motive

    return MemoryControlPlane(
        tenants=(
            TenantMemoryPolicy(
                tenant_id=tenant_id,
                default_agent_id=agent_id,
                default_prompt_pack=DEMO_PROMPT_PACK,
                default_motive=resolved_default_motive,
                memory_bank=bank,
                prompt_packs=(PromptPack(name=DEMO_PROMPT_PACK, prompt_profile="support-memory"),),
                agents=(
                    AgentMemoryPolicy(
                        agent=DreamAgentConfig(agent_id=agent_id, name="Memory Browser Agent"),
                        default_scope=default_scope,
                    ),
                ),
                scopes=tuple(
                    ScopeMemoryPolicy(
                        scope=scope,
                        dream_mode="balanced",
                        prompt_pack=DEMO_PROMPT_PACK,
                        motive=scope_motive(scope),
                    )
                    for scope in scopes
                ),
            ),
        )
    )


def apply_persisted_tenant_policy(*, client: Memotron, tenant_id: str) -> None:
    """Layer the graph's persisted tenant policy onto ``client.control_plane``.

    Order matches ``AgentMemoryPlatform.__init__``: the active tenant prompt
    version first, then the active ``ProjectMemoryConfig`` — so a live
    project-memory policy owns ``tenant:<tenant_id>`` with the
    ``project-memory-policy`` Motive, which is what a formation run in that scope
    actually uses.
    """
    AgentMemoryPlatform.apply_tenant_prompt_to_client(client=client, tenant_id=tenant_id)
    AgentMemoryPlatform.apply_project_memory_config_to_client(
        client=client,
        tenant_id=tenant_id,
    )


def operator_tenant_detail(client: Memotron, tenant_warnings: tuple[str, ...]) -> tuple[str, ...]:
    """Extra log-only lines naming the tenants, or ``()`` when there is nothing to add.

    The counterpart to the redaction in :func:`resolve_launch_tenant_id`. The warnings it
    returns are served on ``/api/tenant-config`` and ``/api/overview``, which authenticate
    nobody, so they carry a COUNT; the operator still needs the NAMES to act, and the
    process log has a different audience — whoever can read pod logs, rather than whoever
    can reach the port.

    Returns a tuple, and decides the empty case itself, so the caller appends it to the
    warning lines it is already printing rather than growing a branch. ``main()`` is an
    entry point that no test drives, so every line of logic placed there is a line nobody
    checks; keeping the decision here means both cases are covered by a test.
    """
    if not tenant_warnings:
        return ()
    names = ", ".join(client.graph.known_tenant_ids())
    return (f"(operator detail, not served) tenants in store: {names}",)


def resolve_launch_tenant_id(
    *,
    client: Memotron,
    requested_tenant_id: str | None,
    default_scope: MemoryScope,
) -> tuple[str, tuple[str, ...]]:
    """Pick the tenant id the console operates on, and report what is uncertain.

    A wrong tenant id is not a cosmetic defect: every tenant-keyed read (LLM
    credential status, prompts, project-memory policy, agent registry) and the
    destructive ``Purge generated state`` action are addressed by it. So when the
    operator did not name one, derive it from the graph WHENEVER THAT IS
    UNAMBIGUOUS, and warn loudly in every other case — including when an
    explicitly supplied id matches no tenant in the graph.

    These warnings are served to unauthenticated callers, so they COUNT the other
    tenants instead of naming them. They used to interpolate
    ``', '.join(graph_tenants)`` — the full roster — and they are surfaced on
    ``/api/tenant-config`` and ``/api/overview``, which authenticate nobody. That
    made the warning a second copy of exactly the cross-tenant enumeration that
    removing the ``graph_tenant_ids`` field was meant to close, and the fix would
    have looked complete while the leak continued through the message text.

    The operator still gets the names: ``main()`` prints them to the process log at
    startup, where the audience is whoever can read pod logs rather than whoever can
    reach the port. A count is enough for the reader of the console, because the
    remedy — pass ``--tenant-id`` — does not depend on knowing the neighbours.
    """
    graph_tenants = client.graph.known_tenant_ids()
    warnings: list[str] = []
    requested = (requested_tenant_id or "").strip()
    if requested:
        if graph_tenants and requested not in graph_tenants:
            warnings.append(
                f"tenant id {requested!r} matches no tenant in this graph "
                f"({len(graph_tenants)} other tenant(s) present); tenant-scoped reads "
                "(LLM credentials, prompts, project-memory policy, agent registry) "
                "will be empty and purge would target the wrong tenant"
            )
        return requested, tuple(warnings)
    if default_scope.kind == ScopeKind.TENANT and default_scope.scope_id in graph_tenants:
        return default_scope.scope_id, ()
    if len(graph_tenants) == 1:
        return graph_tenants[0], ()
    if graph_tenants:
        warnings.append(
            "--tenant-id was not supplied and this graph holds more than one tenant "
            f"({len(graph_tenants)} present); falling back to {DEMO_TENANT_ID!r}, which "
            "will show empty tenant configuration. Pass --tenant-id explicitly."
        )
    return DEMO_TENANT_ID, tuple(warnings)


def build_demo_principal(
    *,
    principal_id: str,
    tenant_id: str,
    agent_id: str,
    default_scope: MemoryScope,
    role: PrincipalRole,
    allowed_scopes: tuple[MemoryScope, ...] | None = None,
) -> MemoryPrincipal:
    effective_scopes = allowed_scopes or (default_scope,)
    return MemoryPrincipal(
        principal_id=principal_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        default_scope=default_scope,
        allowed_scope_keys={scope.key for scope in effective_scopes},
        role=role,
    )
