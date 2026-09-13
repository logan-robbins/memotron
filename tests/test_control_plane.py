from __future__ import annotations

import importlib.util
import json
import sys
from datetime import UTC, datetime
from http.server import HTTPServer
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

import memotron.admin_server as admin_server
from memotron import (
    AgentMemoryPolicy,
    DedupPolicy,
    DreamAgentConfig,
    DreamJobKind,
    Memotron,
    EchoUpstreamTransport,
    MemoryBank,
    MemoryControlPlane,
    MemoryPrincipal,
    MemoryRouter,
    MemoryScope,
    Motive,
    PrincipalRole,
    PromptPack,
    ScopeKind,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.models import EpisodeType


class CaptureTransport(EchoUpstreamTransport):
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.payloads.append(payload)
        return await super().chat_completion(payload)


def load_admin_server_module():
    return admin_server


def load_fleet_demo_module():
    module_path = Path(__file__).resolve().parents[1] / "examples" / "fleet_demo_web.py"
    spec = importlib.util.spec_from_file_location("fleet_demo_web", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def read_json(url: str) -> dict:
    try:
        return json.loads(urlopen(url, timeout=5).read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8")
        raise AssertionError(f"HTTP {exc.code} from {url}: {body}") from exc


def test_tenant_policy_rejects_case_insensitive_agent_identity_collisions() -> None:
    with pytest.raises(ValueError, match="agent ids must be unique"):
        TenantMemoryPolicy(
            tenant_id="wdpr",
            agents=(
                AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="planner", name="Planner One")),
                AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="PLANNER", name="Planner Two")),
            ),
        )

    with pytest.raises(ValueError, match="agent names must be unique"):
        TenantMemoryPolicy(
            tenant_id="wdpr",
            agents=(
                AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="planner-a", name="Planner")),
                AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="planner-b", name="PLANNER")),
            ),
        )


def test_scope_keys_differing_only_by_CASE_are_ACCEPTED_and_that_is_deliberate() -> None:
    """Scope-key uniqueness is case-SENSITIVE here, unlike the agent ids above.

    Casefolding it was tried and reverted, and this pins the reversal so it is not
    re-derived. `normalize_key` casefolds `truth_key`, so `tenant:Acme` and `tenant:acme`
    once shared a truth slot and destroyed each other's memory -- but the three truth reads
    take a required `scope_key` now, so they SHARE NO TRUTH SLOT, pinned directly by
    `test_two_scopes_differing_only_by_CASE_do_not_share_a_truth_slot`.

    That is a claim about the TRUTH plane only. Case-differing scopes still share entity
    NODES -- `upsert_node` casefolds `node_identity_key` into the globally-unique `graph_key`
    with no scope predicate. Pre-existing, unchanged by this branch, and casefolding here
    would not have prevented it, since this validator gates policy construction rather than
    node writes.

    Rejecting the pair here would therefore refuse a configuration that is no longer
    dangerous, and it is REACHABLE rather than theoretical: `agent_memory/_registry.py:163`
    dedups the scope list case-sensitively and then constructs this policy, so a
    case-differing pair appends and raises at agent-registration time where it previously
    succeeded. Making that dedup casefold instead would be worse -- it would silently merge
    two distinct scopes into one.
    """
    policy = TenantMemoryPolicy(
        tenant_id="wdpr",
        scopes=(
            ScopeMemoryPolicy(scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="Acme")),
            ScopeMemoryPolicy(scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="acme")),
        ),
    )
    assert {scope.scope.key for scope in policy.scopes} == {"tenant:Acme", "tenant:acme"}

    # The check still fires on a genuine duplicate, so this is not simply disabled.
    with pytest.raises(ValueError, match="scope keys must be unique"):
        TenantMemoryPolicy(
            tenant_id="wdpr",
            scopes=(
                ScopeMemoryPolicy(scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="acme")),
                ScopeMemoryPolicy(scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="acme")),
            ),
        )


def test_control_plane_resolves_tenant_agent_scope_precedence() -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:pinnacle")
    bank = MemoryBank(
        motives=(
            Motive(name="tenant-motive", goal="Tenant default goal."),
            Motive(name="agent-motive", goal="Agent default goal."),
            Motive(name="scope-motive", goal="Scope-specific goal."),
        )
    )
    tenant = TenantMemoryPolicy(
        tenant_id="wdpr",
        default_agent_id="sales-agent",
        default_scope=scope,
        default_dream_mode="balanced",
        default_prompt_pack="tenant-pack",
        default_motive="tenant-motive",
        memory_bank=bank,
        prompt_packs=(
            PromptPack(name="tenant-pack", prompt_profile="support-memory"),
            PromptPack(name="agent-pack", prompt_profile="support-memory", prompt_profile_version="v2"),
            PromptPack(name="scope-pack", prompt_profile="support-memory"),
        ),
        agents=(
            AgentMemoryPolicy(
                agent=DreamAgentConfig(agent_id="sales-agent", name="Sales Agent"),
                dream_mode="formation_only",
                prompt_pack="agent-pack",
                motive="agent-motive",
            ),
        ),
        scopes=(
            ScopeMemoryPolicy(
                scope=scope,
                dream_mode="review_required",
                prompt_pack="scope-pack",
                motive="scope-motive",
                dedup=DedupPolicy(cosine_threshold=0.91),
                read_only=True,
            ),
        ),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))

    policy = control_plane.resolve(tenant_id="wdpr")

    assert policy.tenant_id == "wdpr"
    assert policy.agent_id == "sales-agent"
    assert policy.scope == scope
    assert policy.dream_mode.name == "review_required"
    assert policy.enabled_job_kinds == (DreamJobKind.FORMATION,)
    assert policy.require_dream_agent_approval_for_untrusted_directives is True
    assert policy.prompt_pack is not None
    assert policy.prompt_pack.name == "scope-pack"
    assert policy.motive_name == "scope-motive"
    assert policy.motive is not None
    assert policy.motive.goal == "Scope-specific goal."
    assert policy.dedup.cosine_threshold == 0.91
    assert policy.read_only is True
    assert policy.source_trace["dream_mode"] == "scope"
    assert policy.source_trace["prompt_pack"] == "scope"
    assert policy.source_trace["motive"] == "scope"


def test_control_plane_request_overrides_and_fail_fast_scope_registration() -> None:
    registered_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="registered")
    other_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="other")
    bank = MemoryBank(motives=(Motive(name="scope-motive", goal="Scope goal."),))
    tenant = TenantMemoryPolicy(
        tenant_id="tenant-a",
        default_agent_id="agent-a",
        memory_bank=bank,
        prompt_packs=(PromptPack(name="tenant-pack", prompt_profile="support-memory"),),
        agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="agent-a")),),
        scopes=(ScopeMemoryPolicy(scope=registered_scope, motive="scope-motive"),),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))

    policy = control_plane.resolve(
        tenant_id="tenant-a",
        scope=registered_scope,
        dream_mode="audit",
        prompt_pack="tenant-pack",
    )
    assert policy.dream_mode.name == "audit"
    assert policy.enabled_job_kinds == ()
    assert policy.source_trace["dream_mode"] == "request"
    assert policy.source_trace["prompt_pack"] == "request"

    with pytest.raises(ValueError, match="not registered"):
        control_plane.resolve(tenant_id="tenant-a", scope=other_scope)


def test_control_plane_authorizes_logged_in_principal_scopes() -> None:
    allowed_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="allowed")
    blocked_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="blocked")
    tenant = TenantMemoryPolicy(
        tenant_id="auth-tenant",
        default_agent_id="auth-agent",
        agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="auth-agent")),),
        scopes=(
            ScopeMemoryPolicy(scope=allowed_scope),
            ScopeMemoryPolicy(scope=blocked_scope),
        ),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))
    principal = MemoryPrincipal(
        principal_id="user-123",
        tenant_id="auth-tenant",
        agent_id="auth-agent",
        default_scope=allowed_scope,
        allowed_scope_keys={allowed_scope.key},
    )

    policy = control_plane.resolve_for_principal(principal=principal)
    assert policy.scope == allowed_scope
    assert policy.source_trace["principal"] == "user-123"
    assert policy.source_trace["scope_authorization"] == "principal_allowed_scope"

    with pytest.raises(ValueError, match="not authorized"):
        control_plane.resolve_for_principal(principal=principal, scope=blocked_scope)

    admin = principal.model_copy(update={"role": PrincipalRole.ADMIN})
    admin_policy = control_plane.resolve_for_principal(principal=admin, scope=blocked_scope)
    assert admin_policy.scope == blocked_scope
    assert admin_policy.source_trace["scope_authorization"] == "tenant_admin"


@pytest.mark.asyncio
async def test_memory_router_resolves_control_plane_policy_and_strips_private_fields() -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="router-scope")
    bank = MemoryBank(motives=(Motive(name="remember-requirements", goal="Track requirements."),))
    tenant = TenantMemoryPolicy(
        tenant_id="router-tenant",
        default_agent_id="router-agent",
        memory_bank=bank,
        default_motive="remember-requirements",
        default_prompt_pack="tenant-pack",
        prompt_packs=(PromptPack(name="tenant-pack", prompt_profile="support-memory"),),
        agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="router-agent")),),
        scopes=(ScopeMemoryPolicy(scope=scope, dream_mode="formation_only"),),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))
    transport = CaptureTransport()
    provider_calls: list[str] = []

    async def provider(resolved_scope: MemoryScope) -> str:
        provider_calls.append(resolved_scope.key)
        return "Resolved memory context."

    router = MemoryRouter(
        control_plane=control_plane,
        memory_context_provider=provider,
        upstream=transport,
    )
    response = await router.route(
        {
            "_tenant_id": "router-tenant",
            "_agent_id": "router-agent",
            "_scope": scope,
            "_dream_mode": "review_required",
            "_prompt_pack": "tenant-pack",
            "model": "echo",
            "messages": [{"role": "user", "content": "What do we know?"}],
        }
    )

    assert provider_calls == [scope.key]
    assert response["_memotron_injected"]["tenant_id"] == "router-tenant"
    assert response["_memotron_injected"]["agent_id"] == "router-agent"
    assert response["_memotron_injected"]["scope_key"] == scope.key
    assert response["_memotron_injected"]["dream_mode"] == "review_required"
    assert response["_memotron_injected"]["prompt_pack"] == "tenant-pack"
    assert response["_memotron_injected"]["motive_name"] == "remember-requirements"
    assert "Track requirements." in response["choices"][0]["message"]["content"]
    outbound = transport.payloads[-1]
    assert "_tenant_id" not in outbound
    assert "_agent_id" not in outbound
    assert "_scope" not in outbound
    assert "_dream_mode" not in outbound
    assert "_prompt_pack" not in outbound


@pytest.mark.asyncio
async def test_memory_router_uses_authenticated_principal_as_authority() -> None:
    allowed_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="allowed-router")
    blocked_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="blocked-router")
    tenant = TenantMemoryPolicy(
        tenant_id="principal-tenant",
        default_agent_id="principal-agent",
        prompt_packs=(PromptPack(name="tenant-pack", prompt_profile="support-memory"),),
        agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="principal-agent")),),
        scopes=(
            ScopeMemoryPolicy(scope=allowed_scope, prompt_pack="tenant-pack"),
            ScopeMemoryPolicy(scope=blocked_scope, prompt_pack="tenant-pack"),
        ),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))
    principal = MemoryPrincipal(
        principal_id="logged-in-user",
        tenant_id="principal-tenant",
        agent_id="principal-agent",
        default_scope=allowed_scope,
        allowed_scope_keys={allowed_scope.key},
    )

    async def provider(resolved_scope: MemoryScope) -> str:
        return f"context for {resolved_scope.key}"

    router = MemoryRouter(
        control_plane=control_plane,
        memory_context_provider=provider,
        upstream=EchoUpstreamTransport(),
    )
    response = await router.route(
        {"model": "echo", "messages": [{"role": "user", "content": "Hi"}]},
        principal=principal,
    )
    metadata = response["_memotron_injected"]
    assert metadata["principal_id"] == "logged-in-user"
    assert metadata["tenant_id"] == "principal-tenant"
    assert metadata["scope_key"] == allowed_scope.key
    assert metadata["source_trace"]["scope_authorization"] == "principal_allowed_scope"

    with pytest.raises(ValueError, match="not authorized"):
        await router.route(
            {
                "_scope": blocked_scope,
                "model": "echo",
                "messages": [{"role": "user", "content": "Leak blocked scope"}],
            },
            principal=principal,
        )

    with pytest.raises(ValueError, match="does not match authenticated principal tenant"):
        await router.route(
            {
                "_tenant_id": "wrong-tenant",
                "model": "echo",
                "messages": [{"role": "user", "content": "Wrong tenant"}],
            },
            principal=principal,
        )


@pytest.mark.asyncio
async def test_client_runtime_dreaming_uses_resolved_mode_scope_and_agent(tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="runtime-scope")
    tenant = TenantMemoryPolicy(
        tenant_id="runtime-tenant",
        default_agent_id="runtime-agent",
        agents=(
            AgentMemoryPolicy(
                agent=DreamAgentConfig(agent_id="runtime-agent", name="Runtime Agent"),
                default_scope=scope,
            ),
        ),
        scopes=(ScopeMemoryPolicy(scope=scope, dream_mode="formation_only"),),
    )
    client = Memotron(
        graph_path=tmp_path / "graph.sqlite",
        control_plane=MemoryControlPlane(tenants=(tenant,)),
    )
    await client.add_episode(
        name="runtime fact",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Pinnacle",
                        "predicate": "requires",
                        "object": "quiet ballroom",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.95,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=datetime(2026, 6, 17, tzinfo=UTC),
    )

    result = await client.run_due_dreams(
        now=datetime(2026, 6, 17, 1, tzinfo=UTC),
        tenant_id="runtime-tenant",
    )

    assert len(result.job_runs) == 1
    run = result.job_runs[0]
    assert run.job_kind == DreamJobKind.FORMATION
    assert "runtime-tenant/runtime-agent/customer:runtime-scope/formation_only" in run.job_name
    assert run.processed_episodes == 1
    matches = await client.search(query="quiet ballroom", scope=scope)
    assert [match.object for match in matches] == ["quiet ballroom"]

    audit_result = await client.run_due_dreams(
        now=datetime(2026, 6, 17, 1, 0, 1, tzinfo=UTC),
        tenant_id="runtime-tenant",
        dream_mode="audit",
    )
    assert audit_result.job_runs == []

    with pytest.raises(ValueError, match="disabled by dream mode"):
        await client.run_dream_job(
            job_name="pruning-default",
            now=datetime(2026, 6, 17, 1, 0, 2, tzinfo=UTC),
            tenant_id="runtime-tenant",
        )


@pytest.mark.asyncio
async def test_memory_graph_browser_demo_principal_filters_scopes(tmp_path: Path) -> None:
    module = load_admin_server_module()

    default_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="visible")
    blocked_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="blocked")
    client = Memotron(graph_path=tmp_path / "browser.sqlite")
    await client.add_memory(
        subject="Visible Customer",
        predicate="requires",
        object="visible docs",
        relationship_type="REQUIRES",
        scope=default_scope,
    )
    await client.add_memory(
        subject="Blocked Customer",
        predicate="requires",
        object="blocked docs",
        relationship_type="REQUIRES",
        scope=blocked_scope,
    )

    assert {item["key"] for item in module.scope_payload_from_client(client)} == {
        default_scope.key,
        blocked_scope.key,
    }

    # `extra_scopes` is REQUIRED here, and it is what keeps this test about what it
    # says it is about. Since 2026-09-08 `build_demo_control_plane` registers only
    # scopes attributable to its tenant, and a `customer:` scope is attributable to
    # none — so `blocked_scope` would otherwise not be registered at all, and the
    # refusal below would come from "not registered" instead of "not authorized".
    # It would still be a refusal, and the PRINCIPAL check this test exists to prove
    # would no longer run. Registering it deliberately keeps the scope known to the
    # control plane and unauthorized for this principal, which is the case under test.
    control_plane = module.build_demo_control_plane(
        client=client,
        default_scope=default_scope,
        tenant_id="demo-tenant",
        agent_id="demo-agent",
        extra_scopes=(blocked_scope,),
    )
    principal = module.build_demo_principal(
        principal_id="demo-user",
        tenant_id="demo-tenant",
        agent_id="demo-agent",
        default_scope=default_scope,
        role=PrincipalRole.USER,
    )

    policy = control_plane.resolve_for_principal(principal=principal, scope=default_scope)
    assert policy.scope == default_scope
    with pytest.raises(ValueError, match="not authorized"):
        control_plane.resolve_for_principal(principal=principal, scope=blocked_scope)


@pytest.mark.asyncio
async def test_memory_graph_browser_http_enforces_principal_scope(tmp_path: Path) -> None:
    module = load_admin_server_module()
    default_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="visible-http")
    blocked_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="blocked-http")
    graph_path = tmp_path / "browser-http.sqlite"
    client = Memotron(graph_path=graph_path)
    await client.add_memory(
        subject="Visible Customer",
        predicate="requires",
        object="visible docs",
        relationship_type="REQUIRES",
        scope=default_scope,
    )
    await client.add_memory(
        subject="Blocked Customer",
        predicate="requires",
        object="blocked docs",
        relationship_type="REQUIRES",
        scope=blocked_scope,
    )

    server_queue = Queue()

    def serve() -> None:
        handler = module.MemoryGraphHandler
        server_client = Memotron(graph_path=graph_path)
        handler.client = server_client
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        # See the note on the previous test: without `extra_scopes` the blocked scope
        # is never registered, the 403 arrives for the wrong reason, and the principal
        # enforcement this test covers stops being exercised over HTTP.
        handler.control_plane = module.build_demo_control_plane(
            client=server_client,
            default_scope=default_scope,
            tenant_id="demo-tenant",
            agent_id="demo-agent",
            extra_scopes=(blocked_scope,),
        )
        handler.principal = module.build_demo_principal(
            principal_id="demo-user",
            tenant_id="demo-tenant",
            agent_id="demo-agent",
            default_scope=default_scope,
            role=PrincipalRole.USER,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        server_queue.put(server)
        server.serve_forever()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server = server_queue.get(timeout=5)
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        scopes = read_json(f"{base_url}/api/scopes")
        assert [item["key"] for item in scopes["scopes"]] == [default_scope.key]

        health = urlopen(f"{base_url}/health", timeout=5)
        assert health.status == 200

        control = read_json(f"{base_url}/api/control-plane?scope={default_scope.key}")
        assert control["principal"]["principal_id"] == "demo-user"
        assert control["policy"]["scope"]["scope_id"] == "visible-http"
        assert control["policy"]["source_trace"]["scope_authorization"] == "principal_allowed_scope"

        with pytest.raises(HTTPError) as exc:
            urlopen(f"{base_url}/api/control-plane?scope={blocked_scope.key}", timeout=5)
        assert exc.value.code == 400
        assert "not authorized" in exc.value.read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_fleet_demo_web_simulation_surfaces_control_plane_story(tmp_path: Path) -> None:
    module = load_fleet_demo_module()

    steps, meta = await module.build_timeline(graph_path=tmp_path / "fleet-demo.sqlite")

    assert len(steps) >= 10
    assert meta["control"]["principal"]["id"] == module.TEST_PRINCIPAL_ID
    assert meta["control"]["principal"]["allowed_scopes"] == [module.SCOPE.key]
    assert meta["control"]["tenant"]["id"] == module.TEST_TENANT_ID
    assert meta["control"]["agent"]["id"] == module.TEST_AGENT_ID
    assert meta["control"]["scope"]["key"] == module.SCOPE.key
    assert meta["control"]["policy"]["dream_mode"] == "balanced"
    assert meta["control"]["policy"]["prompt_pack"] == module.TEST_PROMPT_PACK
    assert meta["control"]["policy"]["motive"] == module.TEST_MOTIVE
    assert meta["control"]["source_trace"]["scope_authorization"] == "principal_allowed_scope"
    assert meta["security_demo"]["blocked_scope"] == module.BLOCKED_SCOPE.key
    assert "not authorized" in meta["security_demo"]["error"]

    assert all(step.control["principal"]["id"] == module.TEST_PRINCIPAL_ID for step in steps)
    assert all(step.control["tenant"]["id"] == module.TEST_TENANT_ID for step in steps)
    assert all(step.control["event"] for step in steps)
    assert {step.engine_action for step in steps} >= {
        "formed",
        "reinforced",
        "superseded",
        "merged",
        "expired",
        "dreamed",
        "used",
    }
    dreamed_steps = [step for step in steps if step.engine_action == "dreamed"]
    assert dreamed_steps
    assert dreamed_steps[0].dream_distilled == meta["dream_distilled"]
    assert steps[-1].finale is not None

    html = module.render_html(steps, meta)
    assert 'id="controlRail"' in html
    assert module.TEST_PRINCIPAL_ID in html
    assert module.TEST_TENANT_ID in html


def test_an_unregistered_scope_refusal_does_not_enumerate_the_registered_ones() -> None:
    """#224: the refusal must name what you asked for, never what exists.

    `admin_server` renders this ValueError VERBATIM as the 400 body on four routes that
    authenticate nobody -- `/api/graph`, `/api/overview`, `/api/profile`, `/api/archive`.
    The message used to append `registered scopes: {sorted(registered)}`, so a single
    unauthenticated request naming a scope that **need not exist** returned every
    registered `agent_id`, the tenant scope and the user scope. Not a guessing game: one
    request, arbitrary input, complete list.

    It also grew with use. Reproduced on deployed `latest` the morning it was filed and
    again that afternoon, the second response carried two probe agents created by load
    testing in between.

    Not covered by #200 or B2, which bounded the SUCCESS paths -- `/api/scopes` returns
    only the caller's tenant. This is the REFUSAL path, and refusals are where
    enumeration hides, because nobody reads an error body as a data return.

    The negative assertion below reads the message ONCE into a variable and is paired
    with positives on that same variable. A bare `"x" not in body` passes when the body
    is empty for an unrelated reason -- the exact way a check-that-cannot-fail gets
    written, and one this repo has already been bitten by (a consumed `HTTPError.read()`
    satisfying its own negative assertion).
    """
    registered_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:pinnacle-events")
    tenant = TenantMemoryPolicy(
        tenant_id="wdpr",
        default_agent_id="sales-agent",
        default_scope=registered_scope,
        agents=(AgentMemoryPolicy(agent=DreamAgentConfig(agent_id="sales-agent", name="Sales Agent")),),
        scopes=(ScopeMemoryPolicy(scope=registered_scope, dream_mode="balanced"),),
    )
    control_plane = MemoryControlPlane(tenants=(tenant,))

    absent = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="does-not-exist")
    with pytest.raises(ValueError) as excinfo:
        control_plane.resolve(tenant_id="wdpr", scope=absent)

    message = str(excinfo.value)

    # POSITIVE: the refusal is still actionable -- it names the rejected scope and tenant.
    # Without these, the negatives below would pass on an empty or unrelated message.
    assert absent.key in message, f"refusal should name the scope that was rejected: {message!r}"
    assert "wdpr" in message, f"refusal should name the tenant: {message!r}"
    assert "is not registered" in message, f"refusal should say why: {message!r}"

    # NEGATIVE: nothing about what else exists.
    assert registered_scope.key not in message, (
        f"the refusal enumerated a registered scope -- #224 has regressed: {message!r}"
    )
    assert "sales-agent" not in message, f"the refusal leaked a registered agent_id: {message!r}"
    assert "registered scopes" not in message, f"the enumeration prefix is back: {message!r}"
