"""Agent-oriented memory facade built on the Memotron SDK.

This module keeps Memotron as the stateful memory platform while giving
agent runtimes a small workflow vocabulary: start, search, remember, log, and
refresh, plus governed publication into shared project memory. It is intentionally
SDK-first so Codex, Claude, and custom harnesses can share the same project/tenant
and scoped graph.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from pathlib import Path
from threading import Lock

from memotron.agent_memory._bootstrap import AgentMemoryBootstrapMixin
from memotron.agent_memory._builders import (
    build_agent_memory_client as build_agent_memory_client,
)
from memotron.agent_memory._builders import (
    build_agent_memory_control_plane as build_agent_memory_control_plane,
)
from memotron.agent_memory._common import (
    _normalize_agent_ids as _normalize_agent_ids,
)
from memotron.agent_memory._common import (
    _normalize_non_blank as _normalize_non_blank,
)
from memotron.agent_memory._config import (
    DEFAULT_AGENT_MEMORY_MOTIVE as DEFAULT_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._config import (
    ENGINEERING_AGENT_MEMORY_MOTIVE as ENGINEERING_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._config import (
    GENERAL_AGENT_MEMORY_MOTIVE as GENERAL_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._config import (
    PRODUCT_AGENT_MEMORY_MOTIVE as PRODUCT_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._config import (
    PROJECT_AGENT_MEMORY_MOTIVE as PROJECT_AGENT_MEMORY_MOTIVE,
)
from memotron.agent_memory._config import (
    agent_memory_bank as agent_memory_bank,
)
from memotron.agent_memory._config import (
    agent_memory_config as agent_memory_config,
)
from memotron.agent_memory._config import (
    default_motive_for_agent_id as default_motive_for_agent_id,
)
from memotron.agent_memory._contract import (
    AGENT_MEMORY_CHECKPOINT_REASONS as AGENT_MEMORY_CHECKPOINT_REASONS,
)
from memotron.agent_memory._contract import (
    AGENT_MEMORY_MCP_SERVICE_NAME as AGENT_MEMORY_MCP_SERVICE_NAME,
)
from memotron.agent_memory._contract import (
    AGENT_MEMORY_REQUIRED_TOOLS as AGENT_MEMORY_REQUIRED_TOOLS,
)
from memotron.agent_memory._contract import (
    RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME as RECOMMENDED_AGENT_MEMORY_MCP_SERVER_NAME,
)
from memotron.agent_memory._contract import (
    _mcp_health_url as _mcp_health_url,
)
from memotron.agent_memory._contract import (
    _normalized_base_url as _normalized_base_url,
)
from memotron.agent_memory._contract import (
    _platform_url as _platform_url,
)
from memotron.agent_memory._contract import (
    agent_memory_integration_contract as agent_memory_integration_contract,
)
from memotron.agent_memory._curation import AgentMemoryCurationMixin
from memotron.agent_memory._project import (
    PROJECT_MEMORY_PROMPT_PACK as PROJECT_MEMORY_PROMPT_PACK,
)
from memotron.agent_memory._project import (
    PROJECT_MEMORY_PROMPT_PROFILE as PROJECT_MEMORY_PROMPT_PROFILE,
)
from memotron.agent_memory._project import (
    PROJECT_MEMORY_PROMPT_PROFILE_VERSION as PROJECT_MEMORY_PROMPT_PROFILE_VERSION,
)
from memotron.agent_memory._project import (
    AgentMemoryProjectMixin,
)
from memotron.agent_memory._prompts import (
    TENANT_FULL_PROMPT_PROFILE as TENANT_FULL_PROMPT_PROFILE,
)
from memotron.agent_memory._prompts import (
    AgentMemoryPromptMixin,
)
from memotron.agent_memory._publish import (
    PROJECT_MEMORY_POLICY_MOTIVE as PROJECT_MEMORY_POLICY_MOTIVE,
)
from memotron.agent_memory._publish import (
    AgentMemoryPublishMixin,
)
from memotron.agent_memory._registry import (
    TENANT_ACTIVE_PROMPT_PACK as TENANT_ACTIVE_PROMPT_PACK,
)
from memotron.agent_memory._registry import (
    TENANT_ACTIVE_PROMPT_PROFILE as TENANT_ACTIVE_PROMPT_PROFILE,
)
from memotron.agent_memory._registry import (
    TENANT_ACTIVE_PROMPT_PROFILE_VERSION as TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
)
from memotron.agent_memory._registry import (
    AgentRegistryMixin,
)
from memotron.agent_memory._render import AgentMemoryRenderMixin
from memotron.agent_memory._reporting import AgentMemoryReportingMixin
from memotron.agent_memory._results import (
    AgentMemoryBootstrapResult as AgentMemoryBootstrapResult,
)
from memotron.agent_memory._results import (
    AgentMemoryEvolutionResult as AgentMemoryEvolutionResult,
)
from memotron.agent_memory._results import (
    AgentMemoryMode as AgentMemoryMode,
)
from memotron.agent_memory._results import (
    AgentMemoryRefreshResult as AgentMemoryRefreshResult,
)
from memotron.agent_memory._results import (
    AgentMemorySearchResult as AgentMemorySearchResult,
)
from memotron.agent_memory._results import (
    AgentMemoryStartResult as AgentMemoryStartResult,
)
from memotron.agent_memory._results import (
    AgentRegistrationResult as AgentRegistrationResult,
)
from memotron.agent_memory._results import (
    CitationScanResult as CitationScanResult,
)
from memotron.agent_memory._results import (
    OutcomeJudgeResult as OutcomeJudgeResult,
)
from memotron.agent_memory._results import (
    ProjectMemoryConfig as ProjectMemoryConfig,
)
from memotron.agent_memory._results import (
    ProjectMemoryPublishResult as ProjectMemoryPublishResult,
)
from memotron.agent_memory._results import (
    ProjectMemorySettings as ProjectMemorySettings,
)
from memotron.agent_memory._results import (
    PromotionResult as PromotionResult,
)
from memotron.agent_memory._results import (
    RunbookCaptureResult as RunbookCaptureResult,
)
from memotron.agent_memory._scopes import (
    agent_scope as agent_scope,
)
from memotron.agent_memory._scopes import (
    default_user_id as default_user_id,
)
from memotron.agent_memory._scopes import (
    project_scope as project_scope,
)
from memotron.agent_memory._scopes import (
    user_scope as user_scope,
)
from memotron.agent_memory._session import AgentMemorySessionMixin
from memotron.agent_memory._transcripts import (
    _CITATION_MIN_CONTENT_TOKENS as _CITATION_MIN_CONTENT_TOKENS,
)
from memotron.agent_memory._transcripts import (
    _SESSION_JUDGE_MAX_ASSISTANT_TURNS as _SESSION_JUDGE_MAX_ASSISTANT_TURNS,
)
from memotron.agent_memory._transcripts import (
    _SESSION_JUDGE_MAX_CITED as _SESSION_JUDGE_MAX_CITED,
)
from memotron.agent_memory._transcripts import (
    SESSION_JUDGE_SYSTEM_PROMPT as SESSION_JUDGE_SYSTEM_PROMPT,
)
from memotron.agent_memory._transcripts import (
    SESSION_JUDGE_VERSION as SESSION_JUDGE_VERSION,
)
from memotron.agent_memory._transcripts import (
    AgentMemoryTranscriptMixin,
)
from memotron.agents import DreamAgentTransport
from memotron.client import Memotron
from memotron.config import MemoryPrincipal
from memotron.embedding import EmbeddingTransport
from memotron.extraction import ExtractionTransport
from memotron.models import MemoryScope
from memotron.storage import StorageBackend
from memotron.synthesis import SynthesisTransport

"""WS-15 T8: minimum content tokens a fact needs before token-containment can
cite it — one- or two-token facts would match almost any transcript."""
"""WS-15 T10: newest cited rows offered per judge call, sized so a full strict
JSON verdict list fits the synthesis transport's modest output budget."""


class AgentMemoryPlatform(
    AgentMemoryBootstrapMixin,
    AgentMemoryProjectMixin,
    AgentMemoryPromptMixin,
    AgentMemorySessionMixin,
    AgentMemoryTranscriptMixin,
    AgentMemoryCurationMixin,
    AgentMemoryPublishMixin,
    AgentMemoryReportingMixin,
    AgentMemoryRenderMixin,
    AgentRegistryMixin,
):
    """Small SDK facade for agent runtimes and harnesses."""

    def __init__(
        self,
        *,
        client: Memotron,
        tenant_id: str,
        agent_ids: list[str] | tuple[str, ...] = (),
        mode: AgentMemoryMode | str = AgentMemoryMode.SIMPLE,
        user_id: str | None = None,
        principal_provider: Callable[[], MemoryPrincipal | None] | None = None,
    ) -> None:
        if client.control_plane is None:
            raise ValueError("AgentMemoryPlatform requires a Memotron client with control_plane")
        self.client = client
        # HOW THE CALLER'S IDENTITY REACHES THIS OBJECT (#206 Phase 2).
        #
        # The two transports carry it differently and neither can be read from here:
        # `mcp_auth.current_principal()` reads `request_ctx` from the MCP server package
        # and returns None for anything that is not an in-flight MCP request, while
        # `admin_server` holds `handler.principal` and imports nothing from `mcp_auth`.
        # A guard that called `current_principal()` directly would work over MCP and see
        # None over HTTP -- then either break every HTTP route or leave them unguarded.
        #
        # So the transport supplies the lookup instead:
        #     agent-memory MCP -> mcp_auth.current_principal
        #     admin_server     -> lambda: handler.principal
        #
        # None keeps the pre-#206 behaviour, so this is additive: an embedder that has no
        # notion of a caller (tests, the local platform, a notebook) is unaffected. A
        # future third transport has to supply one, which is the property worth having.
        self._principal_provider = principal_provider
        self.tenant_id = _normalize_non_blank(tenant_id, "tenant_id")
        self.mode = AgentMemoryMode(mode)
        self.project_scope = project_scope(self.tenant_id)
        self.user_scope = user_scope(user_id or default_user_id()) if self.mode == AgentMemoryMode.SIMPLE else None
        self._agent_registration_lock = Lock()
        self.agent_ids: tuple[str, ...] = ()
        self._load_registered_agents()
        for agent_id in _normalize_agent_ids(agent_ids):
            if (
                self.client.graph.tenant_agent(
                    tenant_id=self.tenant_id,
                    agent_id=agent_id,
                )
                is None
            ):
                self.register_agent(
                    agent_id=agent_id,
                    agent_name=f"{agent_id} agent",
                    source="initial",
                )
            else:
                self._require_agent(agent_id)
        self.apply_tenant_prompt_override_to_client(client=self.client, tenant_id=self.tenant_id)
        self.apply_project_memory_config_to_client(
            client=self.client,
            tenant_id=self.tenant_id,
        )

    @classmethod
    def create(
        cls,
        *,
        graph_path: str | Path | None = None,
        storage: StorageBackend | None = None,
        project_id: str,
        agent_ids: list[str] | tuple[str, ...] = (),
        project_name: str | None = None,
        extraction_transport: ExtractionTransport | None = None,
        dream_agent_transport: DreamAgentTransport | None = None,
        embedding_transport: EmbeddingTransport | None = None,
        rollup_synthesis_transport: SynthesisTransport | None = None,
        mode: AgentMemoryMode | str = AgentMemoryMode.SIMPLE,
        user_id: str | None = None,
        principal_provider: Callable[[], MemoryPrincipal | None] | None = None,
    ) -> AgentMemoryPlatform:
        client = build_agent_memory_client(
            graph_path=graph_path,
            storage=storage,
            project_id=project_id,
            project_name=project_name,
            agent_ids=agent_ids,
            extraction_transport=extraction_transport,
            dream_agent_transport=dream_agent_transport,
            embedding_transport=embedding_transport,
            rollup_synthesis_transport=rollup_synthesis_transport,
            mode=mode,
            user_id=user_id,
        )
        return cls(
            client=client,
            tenant_id=project_id,
            agent_ids=agent_ids,
            mode=mode,
            user_id=user_id,
            principal_provider=principal_provider,
        )

    def agent_scope(self, agent_id: str) -> MemoryScope:
        return agent_scope(self._require_agent(agent_id))

    def register_agent(
        self,
        *,
        agent_id: str,
        agent_name: str,
        source: str = "runtime",
    ) -> AgentRegistrationResult:
        """Claim or reconnect one tenant-local agent identity."""

        # Does NOT reach `_authorize` (verified by call graph), so the caller check is
        # explicit here. It is a TENANT check, and that is the only coherent one: the
        # agent scope being created does not exist yet, so requiring it in the caller's
        # allowlist would make first registration impossible. `memory_bootstrap` calls
        # this, so it is the hot path for every consumer session.
        self._require_caller_tenant("register_agent")

        with self._agent_registration_lock:
            registration = self.client.graph.register_tenant_agent(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                name=agent_name,
                source=source,
            )
            normalized_agent_id = str(registration["agent_id"])
            self._register_agent_policy(normalized_agent_id)
            if normalized_agent_id not in self.agent_ids:
                self.agent_ids = (*self.agent_ids, normalized_agent_id)
        return AgentRegistrationResult.model_validate(registration)
