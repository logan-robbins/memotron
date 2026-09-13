"""Who is this agent, and is it allowed to touch that row?

Every other concern calls in here -- the most depended-on plane in the package, and
a leaf, so it sits at the bottom of the layering. `_authorize` and `_authorize_row`
are the gate: get them wrong and an agent reads or edits another agent's memory
silently, because a wrong-but-valid scope key returns an empty result rather than
an error.

`principal_for_agent` lives here rather than with bootstrap, where its line number
would put it. Its only caller is `_authorize`, and leaving it there closed a
four-module cycle (_bootstrap, _publish, _registry, _session). Measured before the
cut rather than discovered after.

The three TENANT_ACTIVE_* constants travel with it -- they are the prompt-pack
identity this plane installs, and they are re-exported so
`from memotron.agent_memory import TENANT_ACTIVE_PROMPT_PACK` keeps working."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._config import default_motive_for_agent_id
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._scopes import agent_scope
from memotron.client import Memotron
from memotron.config import (
    AgentMemoryPolicy,
    DreamAgentConfig,
    DreamPromptOverride,
    DreamPromptProfile,
    MemoryControlPlane,
    MemoryPrincipal,
    PrincipalRole,
    PromptPack,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.identity import normalize_agent_id
from memotron.models import (
    MemoryScope,
    ScopeKind,
)

TENANT_ACTIVE_PROMPT_PACK = "tenant-active"


TENANT_ACTIVE_PROMPT_PROFILE = "support-memory"


TENANT_ACTIVE_PROMPT_PROFILE_VERSION = "v1"


if TYPE_CHECKING:
    from collections.abc import Callable

    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentRegistryMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    agent_ids: Any
    client: Any
    project_scope: Any
    tenant_id: Any
    user_scope: Any

    def principal_for_agent(self, agent_id: str) -> MemoryPrincipal:
        normalized_agent_id = self._require_agent(agent_id)
        scope = agent_scope(normalized_agent_id)
        allowed_scope_keys = {scope.key, self.project_scope.key}
        default_scope = scope
        if self.user_scope is not None:
            allowed_scope_keys.add(self.user_scope.key)
            default_scope = self.user_scope
        return MemoryPrincipal(
            principal_id=f"agent:{normalized_agent_id}",
            tenant_id=self.tenant_id,
            agent_id=normalized_agent_id,
            default_scope=default_scope,
            allowed_scope_keys=allowed_scope_keys,
            role=PrincipalRole.USER,
        )

    def _load_registered_agents(self) -> None:
        for item in self.client.graph.tenant_agents(self.tenant_id):
            agent_id = str(item["agent_id"])
            self._register_agent_policy(agent_id)
            if agent_id not in self.agent_ids:
                self.agent_ids = (*self.agent_ids, agent_id)

    def _require_agent(self, agent_id: str) -> str:
        normalized = normalize_agent_id(agent_id)
        with self._agent_registration_lock:
            registration = self.client.graph.tenant_agent(
                tenant_id=self.tenant_id,
                agent_id=normalized,
            )
            if registration is None:
                raise ValueError(
                    f"agent_id {normalized!r} is not registered for tenant "
                    f"{self.tenant_id!r}; call agent_register before memory tools"
                )
            registered_agent_id = str(registration["agent_id"])
            # Another local-platform connection may have registered the identity
            # or changed its persisted Motive assignment.
            self._register_agent_policy(registered_agent_id)
            if registered_agent_id not in self.agent_ids:
                self.agent_ids = (*self.agent_ids, registered_agent_id)
        return registered_agent_id

    def _register_agent_policy(
        self,
        agent_id: str,
        *,
        motive_override: str | None = None,
    ) -> None:
        control_plane = self.client.control_plane
        assert control_plane is not None
        tenant = control_plane.tenant(self.tenant_id)
        scope = agent_scope(agent_id)
        registration = self.client.graph.tenant_agent(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
        )
        if registration is None:
            raise RuntimeError(f"agent {agent_id!r} is missing from the tenant registry")
        assignment = self.client.graph.agent_motive_assignment(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
        )
        motive = (
            motive_override
            or (assignment["motive_name"] if assignment is not None else None)
            or default_motive_for_agent_id(agent_id)
        )
        if tenant.memory_bank is None:
            raise ValueError("agent memory tenant has no MemoryBank")
        tenant.memory_bank.motive(motive)
        if assignment is None or assignment["motive_name"] != motive:
            self.client.graph.set_agent_motive_assignment(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                motive_name=motive,
                source=("operator" if motive_override is not None else "agent_role_default"),
            )
        agent_policy = AgentMemoryPolicy(
            agent=DreamAgentConfig(
                agent_id=agent_id,
                name=str(registration["agent_name"]),
                scope=scope,
            ),
            default_scope=scope,
            motive=motive,
        )
        agents = tuple(agent_policy if existing.agent_id == agent_id else existing for existing in tenant.agents)
        if not any(existing.agent_id == agent_id for existing in tenant.agents):
            agents = (*agents, agent_policy)
        scope_policy = ScopeMemoryPolicy(scope=scope, motive=motive)
        scopes = tuple(scope_policy if existing.scope.key == scope.key else existing for existing in tenant.scopes)
        if not any(existing.scope.key == scope.key for existing in tenant.scopes):
            scopes = (*scopes, scope_policy)
        tenant_payload = tenant.model_dump(mode="python", exclude={"agents", "scopes"})
        updated_tenant = TenantMemoryPolicy(
            **tenant_payload,
            agents=agents,
            scopes=scopes,
        )
        self.client.control_plane = MemoryControlPlane(
            base_config=control_plane.base_config,
            tenants=tuple(
                updated_tenant if candidate.tenant_id == self.tenant_id else candidate
                for candidate in control_plane.tenants
            ),
        )

    @staticmethod
    def _apply_prompt_pack_to_client(
        *,
        client: Memotron,
        tenant_id: str,
        prompt_profile: str,
        prompt_profile_version: str,
        override: DreamPromptOverride,
        custom_profile: DreamPromptProfile | None = None,
        motive_name: str | None = None,
    ) -> None:
        control_plane = client.control_plane
        assert control_plane is not None
        base_config = control_plane.base_config
        if custom_profile is not None:
            base_config = base_config.model_copy(
                update={
                    "prompt_profiles": AgentRegistryMixin._replace_prompt_profile(
                        base_config.prompt_profiles,
                        custom_profile,
                    )
                }
            )
            client.config = client.config.model_copy(
                update={
                    "prompt_profiles": AgentRegistryMixin._replace_prompt_profile(
                        client.config.prompt_profiles,
                        custom_profile,
                    )
                }
            )
        base_config.prompt_profile(prompt_profile, prompt_profile_version)
        tenant = control_plane.tenant(tenant_id)
        pack = PromptPack(
            name=TENANT_ACTIVE_PROMPT_PACK,
            prompt_profile=prompt_profile,
            prompt_profile_version=prompt_profile_version,
            prompt_override=override,
        )
        prompt_packs = tuple(
            pack if existing.name == TENANT_ACTIVE_PROMPT_PACK else existing for existing in tenant.prompt_packs
        )
        if not any(existing.name == TENANT_ACTIVE_PROMPT_PACK for existing in tenant.prompt_packs):
            prompt_packs = (*tenant.prompt_packs, pack)
        scopes = tuple(
            (
                scope.model_copy(update={"motive": motive_name})
                if motive_name is not None and scope.scope.kind == ScopeKind.TENANT
                else scope
            )
            for scope in tenant.scopes
        )
        updated_tenant = tenant.model_copy(
            update={
                "default_prompt_pack": TENANT_ACTIVE_PROMPT_PACK,
                "default_motive": motive_name or tenant.default_motive,
                "prompt_packs": prompt_packs,
                "scopes": scopes,
            }
        )
        client.control_plane = MemoryControlPlane(
            base_config=base_config,
            tenants=tuple(
                updated_tenant if candidate.tenant_id == tenant.tenant_id else candidate
                for candidate in control_plane.tenants
            ),
        )

    @staticmethod
    def _replace_prompt_profile(
        profiles: tuple[DreamPromptProfile, ...],
        profile: DreamPromptProfile,
    ) -> tuple[DreamPromptProfile, ...]:
        replaced = False
        next_profiles: list[DreamPromptProfile] = []
        for existing in profiles:
            if existing.key == profile.key:
                next_profiles.append(profile)
                replaced = True
            else:
                next_profiles.append(existing)
        if not replaced:
            next_profiles.append(profile)
        return tuple(next_profiles)

    def _require_caller_tenant(
        self,
        operation: str,
        *,
        provider: Callable[[], MemoryPrincipal | None] | None = None,
    ) -> None:
        """Refuse a caller that does not belong to this platform's tenant. (#206 Phase 2)

        THE TENANT IS THE BOUNDARY; THE AGENT IS A NAMESPACE INSIDE IT. This deliberately
        does NOT check `agent:<id>` against the caller's allowlist, and the reason is not
        conservatism: ``memory_bootstrap`` calls ``register_agent``, so **registration is
        self-service**. A caller could register agent X and then be authorized for
        ``agent:X`` because it had just created it -- a check that reads as a boundary and
        is decoration. Making agent scopes real boundaries requires privileged
        registration first (``OPERATOR``/``ADMIN`` only, the roles already exist), which is
        out of scope here and noted in #206.

        The tenant check is the one that matters: it is what stops one customer's caller
        reaching another customer's memory, which is what #200 closed on the admin surface.

        Three states, matching the other guards in this codebase:

        * flag disarmed -> return. Same rollout default as ``mcp_auth``; nothing changes
          until ``MEMOTRON_REQUIRE_GATEWAY_IDENTITY`` is set.
        * no provider  -> return. An embedder with no notion of a caller (tests, the local
          platform, a notebook) is not a deployment and has nothing to authenticate.
        * provider returns None under the armed flag -> REFUSE. That is a deployment
          fault -- the middleware did not run -- not an anonymous caller, and it fails
          closed exactly as ``mcp_auth`` does.
        """
        # Imported in-function so these names do not become part of `_registry`'s public
        # surface -- a module-level import added four unintended rows to
        # `api_surface.golden.txt`, which is precisely what that golden exists to catch.
        # The tier is fine either way: `identity` is CORE, so agent_memory -> identity is
        # downward. It was the old `mcp_auth` import that was upward.
        from memotron.identity import (
            IdentityUnavailableError,
            ScopeNotAuthorizedError,
            identity_required,
        )

        # `provider` lets a transport that holds its own identity pass it in rather than
        # importing `mcp_auth` itself -- admin_server does exactly this, and a module-level
        # import there is an upward tier edge `coupling_report.py` fails on. One guard
        # implementation, reached two ways.
        resolve = provider or self._principal_provider
        if not identity_required() or resolve is None:
            return

        principal = resolve()
        if principal is None:
            raise IdentityUnavailableError(
                f"{operation} requires an authenticated caller and none reached this "
                "process; the identity middleware did not run for this request"
            )
        if principal.tenant_id != self.tenant_id:
            raise ScopeNotAuthorizedError(
                f"{operation} is served for tenant {self.tenant_id!r}; principal "
                f"{principal.principal_id!r} belongs to tenant {principal.tenant_id!r}"
            )

    def _authorize(self, *, agent_id: str, scope: MemoryScope) -> None:
        # The caller-identity check goes FIRST and is separate from what follows.
        # `principal_for_agent` builds a principal from the agent_id the caller supplied,
        # so the resolution below is self-attesting: it proves the agent may reach the
        # scope, never that this caller may act as that agent. 23 of the 29 public methods
        # taking `agent_id` reach here transitively (verified by call graph), which is why
        # one line here covers most of the surface.
        self._require_caller_tenant("this operation")
        principal = self.principal_for_agent(agent_id)
        assert self.client.control_plane is not None
        self.client.control_plane.resolve_for_principal(principal=principal, scope=scope)

    def _authorize_row(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        scope: MemoryScope,
        action: str,
        require_on_allowlist: bool = False,
    ) -> Any:
        """WS-23 C2/H2: per-memory visibility on EVERY uuid-taking mutation.

        ``_authorize`` only proves the caller may touch the SCOPE.  In ``simple``
        mode every agent shares one user scope, so scope authorization passes
        for everyone and the per-memory ``visibility_agents`` allowlist — which
        was enforced on read paths only — did nothing on the mutation paths.
        A second agent could pin, unpin, forget, promote, or (worst)
        ``memory_set_visibility(agents=None)`` another agent's restricted row
        and then read it freely: an ungated clear defeats the whole control.

        This is the one facade choke point every row mutation passes through.
        It resolves the row, checks it really lives in the authorized scope, and
        applies the SAME ``_agent_may_view_relationship`` predicate the read
        paths use — fail-closed, with the row's existence never confirmed beyond
        what the caller could already see.

        ``require_on_allowlist=True`` (visibility changes) additionally demands
        that the caller be named on the CURRENT allowlist, so an agent that was
        excluded can never rewrite or clear the list that excludes it.
        """
        relationship = self.client.graph.get_relationship(relationship_uuid)
        properties = relationship.properties
        if properties.get("scope_key") != scope.key:
            raise ValueError(f"relationship {relationship_uuid} does not belong to scope {scope.key}")
        if not self.client._agent_may_view_relationship(properties, agent_id):
            raise PermissionError(
                f"agent {agent_id!r} may not {action} relationship {relationship_uuid}: "
                "the memory is restricted to an explicit agent allowlist"
            )
        if require_on_allowlist:
            allowlist = properties.get("visibility_agents")
            if (
                isinstance(allowlist, (list, tuple))
                and allowlist
                and agent_id not in {str(agent) for agent in allowlist}
            ):
                raise PermissionError(
                    f"agent {agent_id!r} may not {action} relationship "
                    f"{relationship_uuid}: only agents on the current "
                    "visibility allowlist can change it"
                )
        return relationship

    def _target_scope(self, *, agent_id: str, scope: str) -> MemoryScope:
        normalized = _normalize_non_blank(scope, "scope")
        if normalized == "default":
            return self.user_scope or self.agent_scope(agent_id)
        if normalized in {"personal", "user"}:
            self._require_agent(agent_id)
            if self.user_scope is None:
                raise ValueError("personal scope is available only in simple mode")
            return self.user_scope
        if normalized == "agent":
            return self.agent_scope(agent_id)
        if normalized == "project":
            self._require_agent(agent_id)
            return self.project_scope
        raise ValueError("scope must be 'default', 'personal', 'agent', or 'project'")
