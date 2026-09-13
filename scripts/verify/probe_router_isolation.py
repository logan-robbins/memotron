"""T1-30: can a caller cross tenants by lying in the request body?

Filed claim (AGENT-SOURCED): `MemoryRouter`'s guard at `interop.py:903-909` is "fail-open by
construction -- the tenant id comes from the request body and `principal` defaults to None, so
the check compares a caller-supplied value against itself."

**This probe attacks rather than reads**, because reading the guard is what produced the claim.
It builds two tenants, fixes a router to `alpha`, and has the caller lie four ways.

PRECONDITION THAT BIT ONCE: without `default_agent_id` on each tenant, every arm is rejected
with "tenant 'alpha' has no default_agent_id" -- an unrelated config error that looks exactly
like the attack being blocked. Nine arms "passed" for the wrong reason on the first run. The
assertion below therefore requires the HONEST arm to be ALLOWED, so a run that never reaches
the guard cannot be read as a result.

    uv run python scripts/verify/probe_router_isolation.py

exit 0 = every cross-tenant attempt rejected AND the honest one allowed
exit 1 = a cross-tenant attempt succeeded  ·  2 = the probe never reached the guard
"""

from __future__ import annotations

from memotron import MemoryScope, ScopeKind
from memotron.config import (
    AgentMemoryPolicy,
    DreamAgentConfig,
    MemoryControlPlane,
    TenantMemoryPolicy,
    default_config,
)
from memotron.interop import MemoryRouter

A = MemoryScope(kind=ScopeKind.TENANT, scope_id="alpha")
B = MemoryScope(kind=ScopeKind.TENANT, scope_id="bravo")

base = default_config()
cp = MemoryControlPlane(
    base_config=base,
    tenants=(
        TenantMemoryPolicy(
            tenant_id="alpha",
            default_scope=A,
            default_agent_id="a1",
            agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="a1"), default_scope=A),),
        ),
        TenantMemoryPolicy(
            tenant_id="bravo",
            default_scope=B,
            default_agent_id="b1",
            agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="b1"), default_scope=B),),
        ),
    ),
)


async def _ctx(scope):
    return ""


def attempt(label, router, req):
    try:
        pol = router._resolve_request_policy(req, principal=None)
        # mirror the guard at interop.py:903-909 exactly
        if pol is not None and router._scope is not None and pol.scope.key != router._scope.key:
            return f"REJECTED by router guard (resolved {pol.scope.key})"
        return f"ALLOWED -> {pol.scope.key if pol else router._scope.key}"
    except ValueError as e:
        return f"REJECTED: {str(e)[:88]}"


print("\n  === router FIXED to tenant alpha, caller unauthenticated (principal=None) ===")
fixed = MemoryRouter(scope=A, memory_context_provider=_ctx, control_plane=cp)
for label, req in [
    ("honest: own tenant", {"_tenant_id": "alpha"}),
    ("lie: other tenant id", {"_tenant_id": "bravo"}),
    ("lie: other tenant id + scope", {"_tenant_id": "bravo", "_scope": B}),
    ("lie: own tenant, other scope", {"_tenant_id": "alpha", "_scope": B}),
    (
        "lie: unregistered scope",
        {"_tenant_id": "alpha", "_scope": MemoryScope(kind=ScopeKind.TENANT, scope_id="ghost")},
    ),
]:
    print(f"    {label:32s} {attempt(label, fixed, req)}")

print("\n  === router with NO fixed scope (multi-tenant), principal=None ===")
loose = MemoryRouter(scope=None, memory_context_provider=_ctx, control_plane=cp)
for label, req in [
    ("any tenant it likes", {"_tenant_id": "bravo"}),
    ("any tenant + its own scope", {"_tenant_id": "bravo", "_scope": B}),
    ("cross: alpha id, bravo scope", {"_tenant_id": "alpha", "_scope": B}),
]:
    print(f"    {label:32s} {attempt(label, loose, req)}")

print("\n  === no control plane at all (the plain default) ===")
plain = MemoryRouter(scope=A, memory_context_provider=_ctx)
print(f"    {'lie: other tenant id':32s} {attempt('x', plain, {'_tenant_id': 'bravo', '_scope': B})}")


# ---------------------------------------------------------------- verdict
print()
honest = attempt("h", fixed, {"_tenant_id": "alpha"})
attacks = [
    attempt("a", fixed, {"_tenant_id": "bravo"}),
    attempt("b", fixed, {"_tenant_id": "bravo", "_scope": B}),
    attempt("c", fixed, {"_tenant_id": "alpha", "_scope": B}),
    attempt("d", fixed, {"_tenant_id": "alpha", "_scope": MemoryScope(kind=ScopeKind.TENANT, scope_id="ghost")}),
]
if not honest.startswith("ALLOWED"):
    print("=== INCONCLUSIVE: the honest request was rejected too, so no arm reached the guard ===")
    print(f"    honest arm said: {honest}")
    raise SystemExit(2)
leaked = [a for a in attacks if a.startswith("ALLOWED")]
print(
    f"=== router isolation: {'LEAKS' if leaked else 'all ' + str(len(attacks)) + ' cross-tenant attempts rejected'} ==="
)
for a in leaked:
    print(f"  - {a}")
raise SystemExit(1 if leaked else 0)
