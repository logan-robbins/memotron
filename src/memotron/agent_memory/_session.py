"""What an agent is told at session start, and what it asks for mid-session.

Six members. `memory_start` is the single most consequential method in the package:
it is the context an agent opens with, so anything it omits is invisible to that
agent for the whole session -- no error, just a worse answer. `latest_compaction_
checkpoint` is the same question asked again after a context compaction."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import AgentMemorySearchResult, AgentMemoryStartResult
from memotron.config import (
    ProfilePolicy,
)
from memotron.models import (
    ArchivedMatchReport,
    MemoryScope,
    OutcomeEvent,
    OutcomeVerdict,
    UseEvent,
    UseEventKind,
)
from memotron.retrieval import query_digest as retrieval_query_digest

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemorySessionMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    mode: Any
    project_scope: Any
    tenant_id: Any
    user_scope: Any

    def latest_compaction_checkpoint(self, *, agent_id: str, session_id: str) -> str | None:
        """WS-28 T1: the session's latest ``context_compaction`` checkpoint
        summary text, or ``None`` when no such checkpoint has been queued yet.

        Reads the RAW queued episode directly (not the dreamt memory), so
        rehydration works immediately at ``SessionStart(source=compact)`` —
        before the next formation cycle has necessarily run.  ``memory_log``
        stamps a checkpoint episode's JSON body with a top-level ``summary``
        key holding exactly the cleaned checkpoint text
        (``_render_log_body``); this reads that key back verbatim, never the
        materialized/extracted fact.  Candidates are matched by the
        stable ``claude:{session_id}:`` task-run-id prefix every hook boundary
        shares (see ``adoption.session_task_run_id``), so both PreCompact's
        dying-context capture and PostCompact's compact summary are eligible
        and the chronologically LATEST one (by ``created_at``, then ``uuid``)
        wins.
        """
        if not session_id:
            return None
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        prefix = f"claude:{session_id}:"
        candidates = [
            episode
            for episode in self.client.graph.episodes_for_scope(scope.key)
            if episode.metadata.get("agent_memory_event") == "checkpoint"
            and episode.metadata.get("checkpoint_reason") == "context_compaction"
            and str(episode.metadata.get("task_run_id", "")).startswith(prefix)
        ]
        if not candidates:
            return None
        latest = max(candidates, key=lambda episode: (episode.created_at, episode.uuid))
        try:
            payload = json.loads(latest.body)
        except (json.JSONDecodeError, TypeError):
            return None
        if not isinstance(payload, dict):
            return None
        summary = str(payload.get("summary") or "").strip()
        return summary or None

    async def memory_start(
        self,
        *,
        agent_id: str,
        agent_token_budget: int = 1200,
        project_token_budget: int = 800,
        task_run_id: str | None = None,
        checkpoint_seed: str | None = None,
    ) -> AgentMemoryStartResult:
        """Load prompt-ready context and start a new task run.

        checkpoint_seed:
            WS-28 T1: optional text from the session's latest
            ``context_compaction`` checkpoint (see
            :meth:`latest_compaction_checkpoint`).  When non-blank, this
            method additionally runs one search across the agent's
            authorized scopes using the checkpoint text as the query, records
            its results as RETRIEVED use events, and leads
            ``rendered_context`` with the recovered task focus.  ``None``
            (default) — including every caller that predates this
            parameter — is byte-identical to today's behavior.
        """
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=self.project_scope)
        if self.user_scope is not None:
            self._authorize(agent_id=agent_id, scope=self.user_scope)
        policy = ProfilePolicy(render_mode="typed", reference_mode=True)
        # WS-19 T22: the calling agent's identity rides on every profile read
        # so per-memory visibility allowlists are enforced fail-closed.
        project_profile = await self.client.profile(
            scope=self.project_scope,
            policy=policy,
            token_budget=project_token_budget,
            reader_agent_id=agent_id,
        )
        user_profile = None
        continuity_budget = agent_token_budget
        if self.user_scope is not None:
            personal_budget = max(1, (agent_token_budget * 3) // 4)
            continuity_budget = max(1, agent_token_budget - personal_budget)
            user_profile = await self.client.profile(
                scope=self.user_scope,
                policy=policy,
                token_budget=personal_budget,
                reader_agent_id=agent_id,
            )
        agent_profile = await self.client.profile(
            scope=scope,
            policy=policy,
            token_budget=continuity_budget,
            reader_agent_id=agent_id,
        )
        rendered_context = self._render_start_context(
            project_profile=project_profile,
            agent_profile=agent_profile,
            user_profile=user_profile,
            mode=self.mode,
        )
        effective_task_run_id = (
            _normalize_non_blank(task_run_id, "task_run_id") if task_run_id is not None else str(uuid4())
        )
        project_use_events = await self._record_profile_injections(
            profile=project_profile,
            task_run_id=effective_task_run_id,
            token_budget=project_token_budget,
        )
        agent_use_events = await self._record_profile_injections(
            profile=agent_profile,
            task_run_id=effective_task_run_id,
            token_budget=continuity_budget,
        )
        user_use_events = (
            await self._record_profile_injections(
                profile=user_profile,
                task_run_id=effective_task_run_id,
                token_budget=max(1, (agent_token_budget * 3) // 4),
            )
            if user_profile is not None
            else []
        )
        use_events = [*project_use_events, *user_use_events, *agent_use_events]

        # WS-28 T1: checkpoint-seeded rehydration.  A non-blank seed runs one
        # additional search across this agent's authorized scopes using the
        # dying context's own checkpoint text as the query, and leads the
        # rendered context with that recovered task focus.  No seed (the
        # normal, non-compaction SessionStart, or a caller that predates this
        # parameter) skips this block entirely -- byte-identical to today.
        normalized_seed = (checkpoint_seed or "").strip()
        checkpoint_seed_applied = False
        if normalized_seed:
            seed_scopes = self._session_scopes(agent_id)
            seed_results = await self.client.search_context(
                query=normalized_seed,
                scopes=seed_scopes,
                limit=5,
                reader_agent_id=agent_id,
            )
            seed_query_digest = retrieval_query_digest(normalized_seed)
            seed_use_events = [
                await self.client.record_memory_use(
                    relationship_uuid=result.relationship_uuid,
                    scope=result.scope,
                    kind=UseEventKind.RETRIEVED,
                    task_run_id=effective_task_run_id,
                    idempotency_key=(
                        f"{effective_task_run_id}:{result.scope.key}:{result.relationship_uuid}:compaction_seed"
                    ),
                    rank=rank,
                    retrieval_score=float(result.score),
                    candidate_set_size=len(seed_results),
                    context_budget_competition=max(0, len(seed_results) - 5),
                    retrieval_policy_digest=result.retrieval_policy_digest,
                    query_digest=seed_query_digest,
                    metadata={"agent_id": agent_id, "compaction_seed": True},
                )
                for rank, result in enumerate(seed_results)
            ]
            use_events = [*seed_use_events, *use_events]
            recovery_header = f"Memotron compaction recovery -- resuming prior task focus:\n{normalized_seed}"
            rendered_context = f"{recovery_header}\n\n{rendered_context}"
            checkpoint_seed_applied = True

        return AgentMemoryStartResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            mode=self.mode,
            project_scope=self.project_scope,
            agent_scope=scope,
            project_profile=project_profile,
            agent_profile=agent_profile,
            user_scope=self.user_scope,
            user_profile=user_profile,
            rendered_context=rendered_context,
            tokens_used=(
                project_profile.tokens_used
                + agent_profile.tokens_used
                + (user_profile.tokens_used if user_profile is not None else 0)
            ),
            task_run_id=effective_task_run_id,
            use_events=use_events,
            checkpoint_seed_applied=checkpoint_seed_applied,
        )

    async def memory_search(
        self,
        *,
        agent_id: str,
        query: str,
        include_project: bool = True,
        limit: int = 10,
        task_run_id: str | None = None,
    ) -> AgentMemorySearchResult:
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        scopes = [self.user_scope, scope] if self.user_scope is not None else [scope]
        if include_project:
            scopes.append(self.project_scope)
        for candidate in scopes:
            self._authorize(agent_id=agent_id, scope=candidate)
        # WS-5: the agent's resolved Motive weights the rerank; the pinned
        # RetrievalContract digest rides on every result so the use events
        # below bind the exact policy that produced the ranking.
        policy = self.client.resolve_policy(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            scope=scope,
        )
        results = await self.client.search_context(
            query=query,
            scopes=scopes,
            limit=limit,
            motive=policy.motive,
            # WS-19 T22: per-memory visibility allowlists are enforced against
            # the calling agent on every retrieval stage (incl. pinned sweep).
            reader_agent_id=agent_id,
        )
        effective_task_run_id = (
            _normalize_non_blank(task_run_id, "task_run_id") if task_run_id is not None else str(uuid4())
        )
        # WS-22 T29: stamp the RetrievalContract's query digest on every
        # RETRIEVED event.  ``retrieval.query_digest`` is the same pure
        # function ``build_retrieval_contract`` pinned inside search_context,
        # so the stamped digest is exactly the contract's — repeat-search
        # lineage joins searches by content, never by raw query text.
        search_query_digest = retrieval_query_digest(query)
        use_events = [
            await self.client.record_memory_use(
                relationship_uuid=result.relationship_uuid,
                scope=result.scope,
                kind=UseEventKind.RETRIEVED,
                task_run_id=effective_task_run_id,
                idempotency_key=(f"{effective_task_run_id}:{result.scope.key}:{result.relationship_uuid}:retrieved"),
                rank=rank,
                retrieval_score=float(result.score),
                candidate_set_size=len(results),
                context_budget_competition=max(0, len(results) - limit),
                retrieval_policy_digest=result.retrieval_policy_digest,
                query_digest=search_query_digest,
                metadata={"agent_id": agent_id, "query": query},
            )
            for rank, result in enumerate(results)
        ]
        return AgentMemorySearchResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            query=query,
            results=results,
            task_run_id=effective_task_run_id,
            use_events=use_events,
            # Archived matches this query found: revived by the search itself by
            # default, or (with pure_read_retrieval on) reported so the agent can
            # ask for one back through memory_restore.
            archived=getattr(results, "archived", None) or ArchivedMatchReport(query=query),
        )

    async def memory_outcome(
        self,
        *,
        agent_id: str,
        use_id: str,
        verdict: OutcomeVerdict | str,
        task_run_id: str,
        idempotency_key: str,
        judge_identity: str,
        judge_version: str,
        scope: str = "default",
        attribution_method: str = "cited_full_credit",
    ) -> OutcomeEvent:
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        return await self.client.record_memory_outcome(
            use_id=_normalize_non_blank(use_id, "use_id"),
            scope=target_scope,
            verdict=OutcomeVerdict(verdict),
            task_run_id=_normalize_non_blank(task_run_id, "task_run_id"),
            idempotency_key=_normalize_non_blank(idempotency_key, "idempotency_key"),
            judge_identity=_normalize_non_blank(judge_identity, "judge_identity"),
            judge_version=_normalize_non_blank(judge_version, "judge_version"),
            attribution_method=attribution_method,
            metadata={"agent_id": agent_id},
        )

    def _session_scopes(self, agent_id: str) -> list[MemoryScope]:
        """The agent's authorized scopes in memory_search order (user, agent, project)."""
        scope = self.agent_scope(agent_id)
        scopes = [self.user_scope, scope] if self.user_scope is not None else [scope]
        scopes.append(self.project_scope)
        for candidate in scopes:
            self._authorize(agent_id=scope.scope_id, scope=candidate)
        return scopes

    def _session_use_events(
        self,
        *,
        scopes: list[MemoryScope],
        session_id: str,
        kinds: set[UseEventKind],
    ) -> list[tuple[MemoryScope, UseEvent]]:
        """Every use event of *kinds* any firing of this session recorded.

        ``adoption.session_task_run_id`` builds every hook task-run id as
        ``claude:{session_id}:{source}:{timestamp}`` — the stable
        ``claude:{session_id}:`` prefix is the session's whole event lineage.
        """
        prefix = f"claude:{session_id}:"
        events: list[tuple[MemoryScope, UseEvent]] = []
        for scope in scopes:
            events.extend(
                (scope, event)
                for event in self.client.graph.use_events_for_task_prefix(
                    scope_key=scope.key, task_run_id_prefix=prefix
                )
                if event.kind in kinds
            )
        return events
