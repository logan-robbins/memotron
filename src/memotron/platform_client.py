"""HTTP client for consuming a hosted Memotron platform."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from memotron.agent_memory import (
    AgentMemoryBootstrapResult,
    AgentMemoryEvolutionResult,
    AgentMemoryRefreshResult,
    AgentMemorySearchResult,
    AgentMemoryStartResult,
    AgentRegistrationResult,
    ProjectMemoryConfig,
    ProjectMemoryPublishResult,
    PromotionResult,
)
from memotron.identity import normalize_agent_id, normalize_agent_name
from memotron.models import (
    AddEpisodeResult,
    AddMemoryResult,
    MemoryUtilityProjection,
    MemoryVisibilityResult,
    OutcomeEvent,
    OutcomeVerdict,
)


def _normalize_base_url(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("base_url cannot be blank")
    return normalized.rstrip("/") + "/"


class MemotronPlatformClient:
    """Remote SDK client for an agent-memory Memotron platform.

    The client talks to the hosted platform HTTP API. It never opens the graph
    database directly, so local SQLite and future managed storage remain server
    operator details.
    """

    def __init__(
        self,
        *,
        base_url: str,
        agent_id: str,
        agent_name: str,
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.base_url = _normalize_base_url(base_url)
        self.agent_id = normalize_agent_id(agent_id)
        self.agent_name = normalize_agent_name(agent_name)
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.headers = dict(headers or {})

    async def status(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_json, "api/platform/status")

    async def integration_contract(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._get_json, "api/platform/integration-contract")

    async def agent_register(self) -> AgentRegistrationResult:
        payload = await self._post(
            "api/platform/agents/register",
            {
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
            },
        )
        return AgentRegistrationResult.model_validate(payload)

    async def memory_bootstrap(
        self,
        *,
        agent_token_budget: int = 1200,
        project_token_budget: int = 800,
        task_run_id: str = "",
    ) -> AgentMemoryBootstrapResult:
        payload = await self._post(
            "api/platform/memory/bootstrap",
            {
                "agent_id": self.agent_id,
                "agent_name": self.agent_name,
                "agent_token_budget": agent_token_budget,
                "project_token_budget": project_token_budget,
                "task_run_id": task_run_id,
            },
        )
        return AgentMemoryBootstrapResult.model_validate(payload)

    async def memory_start(
        self,
        *,
        agent_token_budget: int = 1200,
        project_token_budget: int = 800,
        task_run_id: str = "",
    ) -> AgentMemoryStartResult:
        payload = await self._post(
            "api/platform/memory/start",
            {
                "agent_id": self.agent_id,
                "agent_token_budget": agent_token_budget,
                "project_token_budget": project_token_budget,
                "task_run_id": task_run_id,
            },
        )
        return AgentMemoryStartResult.model_validate(payload)

    async def memory_search(
        self,
        *,
        query: str,
        include_project: bool = True,
        limit: int = 10,
        task_run_id: str = "",
    ) -> AgentMemorySearchResult:
        payload = await self._post(
            "api/platform/memory/search",
            {
                "agent_id": self.agent_id,
                "query": query,
                "include_project": include_project,
                "limit": limit,
                "task_run_id": task_run_id,
            },
        )
        return AgentMemorySearchResult.model_validate(payload)

    async def memory_remember(
        self,
        *,
        subject: str,
        predicate: str,
        object: str,
        relationship_type: str,
        source_text: str = "",
        confidence: float = 0.9,
    ) -> AddMemoryResult:
        payload = await self._post(
            "api/platform/memory/remember",
            {
                "agent_id": self.agent_id,
                "subject": subject,
                "predicate": predicate,
                "object": object,
                "relationship_type": relationship_type,
                "source_text": source_text,
                "confidence": confidence,
            },
        )
        return AddMemoryResult.model_validate(payload)

    async def memory_publish(
        self,
        *,
        content: str,
        task_run_id: str,
        source_reference: str = "",
    ) -> ProjectMemoryPublishResult:
        payload = await self._post(
            "api/platform/memory/publish",
            {
                "agent_id": self.agent_id,
                "content": content,
                "task_run_id": task_run_id,
                "source_reference": source_reference,
            },
        )
        return ProjectMemoryPublishResult.model_validate(payload)

    async def memory_promote(
        self,
        *,
        relationship_uuid: str,
        rationale: str,
        task_run_id: str = "",
    ) -> PromotionResult:
        """WS-19 T20: promote one exact own-scope fact into project memory."""

        payload = await self._post(
            "api/platform/memory/promote",
            {
                "agent_id": self.agent_id,
                "relationship_uuid": relationship_uuid,
                "rationale": rationale,
                "task_run_id": task_run_id,
            },
        )
        return PromotionResult.model_validate(payload)

    async def memory_endorse_promotion(
        self,
        *,
        candidate_episode_uuid: str,
        rationale: str,
    ) -> PromotionResult:
        """WS-19 T20: add this agent's vote to a pending promotion candidate."""

        payload = await self._post(
            "api/platform/memory/endorse-promotion",
            {
                "agent_id": self.agent_id,
                "candidate_episode_uuid": candidate_episode_uuid,
                "rationale": rationale,
            },
        )
        return PromotionResult.model_validate(payload)

    async def memory_set_visibility(
        self,
        *,
        relationship_uuid: str,
        agents: list[str] | tuple[str, ...] | None,
        reason: str,
        scope: str = "default",
    ) -> MemoryVisibilityResult:
        """WS-19 T22: restrict one own-scope memory to an agent allowlist
        (``agents=None`` clears the restriction)."""

        payload = await self._post(
            "api/platform/memory/set-visibility",
            {
                "agent_id": self.agent_id,
                "relationship_uuid": relationship_uuid,
                "agents": list(agents) if agents is not None else None,
                "reason": reason,
                "scope": scope,
            },
        )
        return MemoryVisibilityResult.model_validate(payload)

    async def project_memory_config(self) -> dict[str, Any]:
        return await asyncio.to_thread(
            self._get_json,
            "api/platform/project-memory/config",
        )

    async def project_memory_configure(
        self,
        *,
        project_goal: str,
        memory_goal: str,
        keep: list[str] | tuple[str, ...],
        configured_by: str,
        exclude: list[str] | tuple[str, ...] = (),
        rules: list[str] | tuple[str, ...] = (),
        allowed_memory_types: list[str] | tuple[str, ...] = (
            "requirement",
            "directive",
            "state",
            "decision",
            "incident",
            "rollup",
        ),
        protected_memory_types: list[str] | tuple[str, ...] = (
            "requirement",
            "decision",
            "incident",
        ),
        min_salience: float = 0.0,
        max_memories_per_candidate: int = 12,
        dedup_threshold: float = 0.87,
        min_endorsements: int = 1,
    ) -> ProjectMemoryConfig:
        payload = await self._post(
            "api/platform/project-memory/config",
            {
                "project_goal": project_goal,
                "memory_goal": memory_goal,
                "keep": list(keep),
                "exclude": list(exclude),
                "rules": list(rules),
                "allowed_memory_types": list(allowed_memory_types),
                "protected_memory_types": list(protected_memory_types),
                "min_salience": min_salience,
                "max_memories_per_candidate": max_memories_per_candidate,
                "dedup_threshold": dedup_threshold,
                "min_endorsements": min_endorsements,
                "configured_by": configured_by,
            },
        )
        return ProjectMemoryConfig.model_validate(payload)

    async def memory_log(
        self,
        *,
        summary: str = "",
        decisions: list[str] | tuple[str, ...] = (),
        incidents: list[str] | tuple[str, ...] = (),
        blockers: list[str] | tuple[str, ...] = (),
        checkpoint_reason: str = "",
        task_run_id: str = "",
    ) -> AddEpisodeResult:
        payload = await self._post(
            "api/platform/memory/log",
            {
                "agent_id": self.agent_id,
                "summary": summary,
                "decisions": list(decisions),
                "incidents": list(incidents),
                "blockers": list(blockers),
                "checkpoint_reason": checkpoint_reason,
                "task_run_id": task_run_id,
            },
        )
        return AddEpisodeResult.model_validate(payload)

    async def memory_outcome(
        self,
        *,
        use_id: str,
        verdict: OutcomeVerdict | str,
        task_run_id: str,
        idempotency_key: str,
        judge_identity: str,
        judge_version: str,
        scope: str = "default",
        attribution_method: str = "cited_full_credit",
    ) -> OutcomeEvent:
        """Record an evaluator-observed outcome against one returned use receipt."""

        payload = await self._post(
            "api/platform/memory/outcome",
            {
                "agent_id": self.agent_id,
                "use_id": use_id,
                "verdict": OutcomeVerdict(verdict).value,
                "task_run_id": task_run_id,
                "idempotency_key": idempotency_key,
                "judge_identity": judge_identity,
                "judge_version": judge_version,
                "scope": scope,
                "attribution_method": attribution_method,
            },
        )
        return OutcomeEvent.model_validate(payload)

    async def memory_utility(
        self,
        *,
        relationship_uuid: str = "",
        scope: str = "default",
    ) -> list[MemoryUtilityProjection]:
        """Return receipt-derived utility without changing memory truth."""

        payload = await self._post(
            "api/platform/memory/utility",
            {
                "agent_id": self.agent_id,
                "relationship_uuid": relationship_uuid,
                "scope": scope,
            },
        )
        items = payload.get("items")
        if not isinstance(items, list):
            raise ValueError("Memotron platform utility response must contain an items array")
        return [MemoryUtilityProjection.model_validate(item) for item in items]

    async def memory_refresh(
        self,
        *,
        include_project: bool = True,
    ) -> AgentMemoryRefreshResult:
        payload = await self._post(
            "api/platform/memory/refresh",
            {
                "agent_id": self.agent_id,
                "include_project": include_project,
            },
        )
        return AgentMemoryRefreshResult.model_validate(payload)

    async def memory_evolution(
        self,
        *,
        include_project: bool = False,
    ) -> AgentMemoryEvolutionResult:
        payload = await self._post(
            "api/platform/memory/evolution",
            {
                "agent_id": self.agent_id,
                "include_project": include_project,
            },
        )
        return AgentMemoryEvolutionResult.model_validate(payload)

    async def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        return await asyncio.to_thread(self._post_json, path, payload)

    def _get_json(self, path: str) -> dict[str, Any]:
        request = Request(
            self._url(path),
            headers={
                "Accept": "application/json",
                **self.headers,
            },
            method="GET",
        )
        return self._open_json(request)

    def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        request = Request(
            self._url(path),
            data=body,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                **self.headers,
            },
            method="POST",
        )
        return self._open_json(request)

    def _open_json(self, request: Request) -> dict[str, Any]:
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"Memotron platform request failed with HTTP {exc.code}: {detail}") from exc
        except URLError as exc:
            raise ValueError(f"Memotron platform request failed: {exc.reason}") from exc
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError("Memotron platform response was not valid JSON") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Memotron platform response JSON must be an object")
        return parsed

    def _url(self, path: str) -> str:
        return urljoin(self.base_url, path.lstrip("/"))
