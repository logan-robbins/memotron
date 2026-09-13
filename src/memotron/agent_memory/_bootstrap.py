"""Standing an agent up: credentials, motive, and the contract it is handed.

Eight methods run once per agent, before any memory operation. `memory_bootstrap`
is the entry point and `integration_contract` is what the agent receives -- the
document whose `required_tools` field drifted from the live MCP registry until
2026-08-28.

Top of the layering: this calls into almost everything and nothing calls back."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._contract import agent_memory_integration_contract
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import AgentMemoryBootstrapResult, AgentMemoryMode

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryBootstrapMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    mode: Any
    tenant_id: Any
    user_scope: Any

    async def memory_bootstrap(
        self,
        *,
        agent_id: str,
        agent_name: str,
        agent_token_budget: int = 1200,
        project_token_budget: int = 800,
        task_run_id: str | None = None,
        source: str = "runtime",
    ) -> AgentMemoryBootstrapResult:
        """Reconnect one stable identity and return everything needed to begin work."""

        registration = self.register_agent(
            agent_id=agent_id,
            agent_name=agent_name,
            source=source,
        )
        started = await self.memory_start(
            agent_id=registration.agent_id,
            agent_token_budget=agent_token_budget,
            project_token_budget=project_token_budget,
            task_run_id=task_run_id,
        )
        durable_owner = "personal" if self.mode == AgentMemoryMode.SIMPLE else "agent-private"
        return AgentMemoryBootstrapResult(
            registration=registration,
            start=started,
            next_actions=(
                "Inject start.rendered_context into the active task context.",
                ("Reuse start.task_run_id for memory_search, memory_publish, and memory_outcome during this task."),
                (
                    "Call memory_search before relying on prior requirements, decisions, "
                    "incidents, preferences, or handoff state."
                ),
                (
                    f"Use memory_remember only for exact durable {durable_owner} facts; "
                    "use memory_publish only for sourced project-memory candidates."
                ),
                (
                    "Review at confirmation, validation, handoff, and task-completion "
                    "milestones; write nothing when no durable fact was established."
                ),
                ("Report memory_outcome only after a named user, test, or workflow actually observes the result."),
            ),
        )

    def configure_llm_credentials(
        self,
        *,
        provider: str,
        api_key: str,
        base_url: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        state = self.client.configure_tenant_llm_credentials(
            tenant_id=self.tenant_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )
        # This platform is bound to exactly one tenant, so making its credential the
        # client's default is correct and is not the #158 leak -- there is no second
        # tenant on this client. The governance MCP server, which serves any tenant a
        # caller names, deliberately does NOT do this.
        self.client.bind_default_tenant(self.tenant_id)
        return state

    def llm_credential_state(self) -> dict[str, Any] | None:
        return self.client.tenant_llm_credential_state(self.tenant_id)

    def clear_llm_credentials(self) -> bool:
        cleared = self.client.clear_tenant_llm_credentials(self.tenant_id)
        if cleared:
            # Re-bind so the running platform stops using the revoked credential
            # immediately; resolution now finds none and falls back to the default.
            self.client.bind_default_tenant(self.tenant_id)
        return cleared

    def configure_agent_motive(self, *, agent_id: str, motive_name: str) -> dict[str, str]:
        # A WRITE that changes how formation behaves for this agent, and it does not
        # reach `_authorize` (verified by call graph). #206 Phase 2.
        self._require_caller_tenant("configure_agent_motive")
        normalized_agent = self._require_agent(agent_id)
        normalized_motive = _normalize_non_blank(motive_name, "motive_name")
        control_plane = self.client.control_plane
        assert control_plane is not None
        tenant = control_plane.tenant(self.tenant_id)
        if tenant.memory_bank is None:
            raise ValueError("agent memory tenant has no MemoryBank")
        tenant.memory_bank.motive(normalized_motive)
        assignment = self.client.graph.set_agent_motive_assignment(
            tenant_id=self.tenant_id,
            agent_id=normalized_agent,
            motive_name=normalized_motive,
            source="operator",
        )
        self._register_agent_policy(
            normalized_agent,
            motive_override=normalized_motive,
        )
        return assignment

    def agent_motive(self, *, agent_id: str) -> dict[str, str]:
        self._require_caller_tenant("agent_motive")
        normalized_agent = self._require_agent(agent_id)
        assignment = self.client.graph.agent_motive_assignment(
            tenant_id=self.tenant_id,
            agent_id=normalized_agent,
        )
        if assignment is None:
            raise RuntimeError(f"agent {normalized_agent!r} has no persisted Motive assignment")
        return assignment

    def agent_motives(self) -> tuple[dict[str, str], ...]:
        return self.client.graph.agent_motive_assignments(self.tenant_id)

    def integration_contract(self, *, platform_api_url: str, mcp_url: str) -> dict[str, Any]:
        return agent_memory_integration_contract(
            tenant_id=self.tenant_id,
            platform_api_url=platform_api_url,
            mcp_url=mcp_url,
            mode=self.mode,
            user_id=self.user_scope.scope_id if self.user_scope is not None else None,
            registered_agents=self.client.graph.tenant_agents(self.tenant_id),
            pure_read_retrieval=self.client.config.pure_read_retrieval,
        )
