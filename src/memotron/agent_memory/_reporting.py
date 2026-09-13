"""Reading the platform back: the log, the refresh, the evolution proof.

Three methods answering "what does this agent know, and how did it come to know it".
`memory_evolution` returns the proof object, which is the audit surface an operator
actually reads -- the others are convenience on top of it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.agent_memory._contract import AGENT_MEMORY_CHECKPOINT_REASONS
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import AgentMemoryEvolutionResult, AgentMemoryRefreshResult
from memotron.models import (
    AddEpisodeResult,
    EpisodeType,
)
from memotron.redaction import contains_raw_credentials

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryReportingMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    mode: Any
    project_scope: Any
    tenant_id: Any
    user_scope: Any

    async def memory_log(
        self,
        *,
        agent_id: str,
        summary: str = "",
        decisions: list[str] | tuple[str, ...] = (),
        incidents: list[str] | tuple[str, ...] = (),
        blockers: list[str] | tuple[str, ...] = (),
        checkpoint_reason: str = "",
        task_run_id: str = "",
    ) -> AddEpisodeResult:
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        normalized_checkpoint_reason = checkpoint_reason.strip()
        if normalized_checkpoint_reason and normalized_checkpoint_reason not in AGENT_MEMORY_CHECKPOINT_REASONS:
            allowed = ", ".join(AGENT_MEMORY_CHECKPOINT_REASONS)
            raise ValueError(f"checkpoint_reason must be one of: {allowed}")
        normalized_task_run_id = task_run_id.strip()
        if normalized_checkpoint_reason and not normalized_task_run_id:
            raise ValueError("task_run_id is required when checkpoint_reason is set")
        raw_log_text = "\n".join((summary, *decisions, *incidents, *blockers))
        if contains_raw_credentials(raw_log_text):
            raise ValueError("memory log contains a raw credential; store a governed secret reference instead")
        body = self._render_log_body(
            agent_id=agent_id,
            summary=summary,
            decisions=tuple(decisions),
            incidents=tuple(incidents),
            blockers=tuple(blockers),
        )
        policy = self.client.resolve_policy(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            scope=scope,
        )
        event_kind = "checkpoint" if normalized_checkpoint_reason else "log"
        metadata = {
            "agent_memory": True,
            "agent_memory_event": event_kind,
            "tenant_id": self.tenant_id,
            "agent_id": agent_id,
            "_verified_source_authority": "agent",
        }
        if normalized_checkpoint_reason:
            metadata["checkpoint_reason"] = normalized_checkpoint_reason
        if normalized_task_run_id:
            metadata["task_run_id"] = normalized_task_run_id
        return await self.client.add_episode(
            name=f"agent-memory-{event_kind}:{agent_id}:{datetime.now(UTC).isoformat()}",
            episode_body=body,
            source=EpisodeType.JSON,
            source_description=f"agent memory {event_kind}",
            scope=scope,
            metadata=metadata,
            motive=policy.motive_name,
        )

    async def memory_refresh(
        self,
        *,
        agent_id: str,
        include_project: bool = True,
    ) -> AgentMemoryRefreshResult:
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        agent_run = await self.client.run_due_dreams(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            scope=scope,
        )
        user_run = None
        if self.user_scope is not None:
            self._authorize(agent_id=agent_id, scope=self.user_scope)
            user_run = await self.client.run_due_dreams(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                scope=self.user_scope,
            )
        project_run = None
        if include_project and self.project_memory_config() is not None:
            self._authorize(agent_id=agent_id, scope=self.project_scope)
            project_run = await self.client.run_due_dreams(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                scope=self.project_scope,
            )
        return AgentMemoryRefreshResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            mode=self.mode,
            project_scope=self.project_scope,
            agent_scope=scope,
            agent_run=agent_run,
            user_scope=self.user_scope,
            user_run=user_run,
            project_run=project_run,
        )

    async def memory_evolution(
        self,
        *,
        agent_id: str,
        include_project: bool = False,
    ) -> AgentMemoryEvolutionResult:
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        agent_evolution = await self.client.memory_evolution(scope=scope)
        user_evolution = None
        if self.user_scope is not None:
            self._authorize(agent_id=agent_id, scope=self.user_scope)
            user_evolution = await self.client.memory_evolution(scope=self.user_scope)
        project_evolution = None
        project = None
        if include_project:
            self._authorize(agent_id=agent_id, scope=self.project_scope)
            project = self.project_scope
            project_evolution = await self.client.memory_evolution(scope=self.project_scope)
        return AgentMemoryEvolutionResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            mode=self.mode,
            agent_scope=scope,
            agent_evolution=agent_evolution,
            user_scope=self.user_scope,
            user_evolution=user_evolution,
            project_scope=project,
            project_evolution=project_evolution,
        )
