"""Assembling a control plane and a client from an agent identity.

The two entry points that turn "which agent am I" into a wired Memotron. They sit
at the top of the package dependency order -- everything else is something they
compose -- so they extract last."""

from __future__ import annotations

from pathlib import Path

from memotron.agent_memory._common import (
    _normalize_agent_ids,
    _normalize_non_blank,
)
from memotron.agent_memory._config import (
    DEFAULT_AGENT_MEMORY_MOTIVE,
    GENERAL_AGENT_MEMORY_MOTIVE,
    agent_memory_config,
    default_motive_for_agent_id,
)
from memotron.agent_memory._results import (
    AgentMemoryMode,
)
from memotron.agent_memory._scopes import (
    agent_scope,
    default_user_id,
    project_scope,
    user_scope,
)
from memotron.agents import DreamAgentTransport
from memotron.client import Memotron
from memotron.config import (
    AgentMemoryPolicy,
    DreamAgentConfig,
    DreamConfig,
    MemoryControlPlane,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.embedding import EmbeddingTransport
from memotron.extraction import ExtractionTransport, RuleBasedExtractionTransport
from memotron.storage import StorageBackend
from memotron.synthesis import SynthesisTransport


def build_agent_memory_control_plane(
    *,
    project_id: str,
    agent_ids: list[str] | tuple[str, ...] = (),
    project_name: str | None = None,
    base_config: DreamConfig | None = None,
    default_motive: str = DEFAULT_AGENT_MEMORY_MOTIVE,
    mode: AgentMemoryMode | str = AgentMemoryMode.SIMPLE,
    user_id: str | None = None,
) -> MemoryControlPlane:
    """Build a single-project tenant with initial agent scopes.

    Additional agents are registered on first use by ``AgentMemoryPlatform``.
    """

    normalized_project = _normalize_non_blank(project_id, "project_id")
    normalized_agents = _normalize_agent_ids(agent_ids)
    resolved_mode = AgentMemoryMode(mode)
    global_scope = project_scope(normalized_project)
    personal_scope = user_scope(user_id or default_user_id()) if resolved_mode == AgentMemoryMode.SIMPLE else None
    agent_scopes = tuple(agent_scope(agent_id) for agent_id in normalized_agents)
    agent_motives = tuple(default_motive_for_agent_id(agent_id) for agent_id in normalized_agents)
    config = base_config or agent_memory_config()
    tenant = TenantMemoryPolicy(
        tenant_id=normalized_project,
        name=project_name or normalized_project,
        default_agent_id=normalized_agents[0] if normalized_agents else None,
        default_scope=global_scope,
        default_motive=default_motive,
        memory_bank=config.memory_bank,
        agents=tuple(
            AgentMemoryPolicy(
                agent=DreamAgentConfig(
                    agent_id=agent_id,
                    name=f"{agent_id} agent",
                    scope=scope,
                ),
                default_scope=scope,
                motive=motive,
            )
            for agent_id, scope, motive in zip(normalized_agents, agent_scopes, agent_motives, strict=True)
        ),
        scopes=(
            ScopeMemoryPolicy(scope=global_scope, motive=default_motive),
            *(
                (
                    ScopeMemoryPolicy(
                        scope=personal_scope,
                        motive=GENERAL_AGENT_MEMORY_MOTIVE,
                    ),
                )
                if personal_scope is not None
                else ()
            ),
            *(
                ScopeMemoryPolicy(scope=scope, motive=motive)
                for scope, motive in zip(agent_scopes, agent_motives, strict=True)
            ),
        ),
    )
    return MemoryControlPlane(base_config=config, tenants=(tenant,))


def build_agent_memory_client(
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
) -> Memotron:
    """Create a Memotron client configured for one agent-memory project.

    Takes EITHER a ``graph_path`` (a SQLite file) or an already-open ``storage``
    backend. The second exists so a caller that has an operational store -- Postgres,
    built from ``storage_settings_from_env()`` -- can hand it in rather than being
    forced through a file path it does not have. `Memotron` has always accepted
    ``storage=``; this layer simply never passed it through, which is why the admin
    server could only ever read a SQLite file.

    The caller owns a ``storage`` it supplies and is responsible for closing it. A
    ``graph_path`` is opened by `Memotron` as before, so existing callers are
    unaffected.
    """
    if (graph_path is None) == (storage is None):
        raise ValueError("pass exactly one of graph_path or storage")

    control_plane = build_agent_memory_control_plane(
        project_id=project_id,
        project_name=project_name,
        agent_ids=agent_ids,
        mode=mode,
        user_id=user_id,
    )
    return Memotron(
        **({"storage": storage} if storage is not None else {"graph_path": graph_path}),
        control_plane=control_plane,
        extraction_transport=extraction_transport or RuleBasedExtractionTransport(),
        dream_agent_transport=dream_agent_transport,
        embedding_transport=embedding_transport,
        rollup_synthesis_transport=rollup_synthesis_transport,
    )
