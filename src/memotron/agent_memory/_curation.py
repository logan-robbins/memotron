"""The operator verbs: forget, pin, unpin, restore, hide, explain.

Eight methods that let a human correct what the system concluded. They are grouped
because they share a property worth stating: none of them DELETES. `memory_forget`
archives, `memory_set_visibility` hides, and `memory_restore` reverses either --
which is why restore can exist at all. The graph is append-and-supersede, and these
are its user interface."""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.models import (
    RestoreArchivedMemoryResult,
)

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryCurationMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    project_scope: Any

    async def memory_utility(
        self,
        *,
        agent_id: str,
        relationship_uuid: str = "",
        scope: str = "default",
    ):
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        return await self.client.memory_utility(
            scope=target_scope,
            relationship_uuid=relationship_uuid.strip() or None,
            reader_agent_id=agent_id,
        )

    async def memory_explain(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        scope: str = "default",
    ):
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        return await self.client.memory_evidence(
            relationship_uuid=_normalize_non_blank(
                relationship_uuid,
                "relationship_uuid",
            ),
            scope=target_scope,
            reader_agent_id=agent_id,
        )

    async def memory_forget(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        reason: str,
        scope: str = "default",
    ):
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        if target_scope == self.project_scope:
            raise ValueError(
                "agents cannot forget project memory directly; use the governed "
                "operator correction or retirement workflow"
            )
        self._authorize_row(
            agent_id=agent_id,
            relationship_uuid=_normalize_non_blank(relationship_uuid, "relationship_uuid"),
            scope=target_scope,
            action="forget",
        )
        return await self.client.forget_memory(
            relationship_uuid=_normalize_non_blank(
                relationship_uuid,
                "relationship_uuid",
            ),
            scope=target_scope,
            reason=_normalize_non_blank(reason, "reason"),
        )

    async def memory_pin(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        reason: str,
        scope: str = "default",
    ):
        """WS-20 T23: pin one exact memory for guaranteed retrieval.

        Same authorization contract as ``memory_forget``: restricted to the
        caller's OWN writable scopes; project memory is governed and refused.
        """
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        if target_scope == self.project_scope:
            raise ValueError("agents cannot pin project memory directly; use the governed operator pinning workflow")
        self._authorize_row(
            agent_id=agent_id,
            relationship_uuid=_normalize_non_blank(relationship_uuid, "relationship_uuid"),
            scope=target_scope,
            action="pin",
        )
        return await self.client.pin_memory(
            relationship_uuid=_normalize_non_blank(
                relationship_uuid,
                "relationship_uuid",
            ),
            scope=target_scope,
            reason=_normalize_non_blank(reason, "reason"),
            pinned_by=f"agent:{agent_id}",
        )

    async def memory_unpin(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        reason: str,
        scope: str = "default",
    ):
        """WS-20 T23: remove a pin so the memory returns to normal ranking."""
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        if target_scope == self.project_scope:
            raise ValueError("agents cannot unpin project memory directly; use the governed operator pinning workflow")
        self._authorize_row(
            agent_id=agent_id,
            relationship_uuid=_normalize_non_blank(relationship_uuid, "relationship_uuid"),
            scope=target_scope,
            action="unpin",
        )
        return await self.client.unpin_memory(
            relationship_uuid=_normalize_non_blank(
                relationship_uuid,
                "relationship_uuid",
            ),
            scope=target_scope,
            reason=_normalize_non_blank(reason, "reason"),
            pinned_by=f"agent:{agent_id}",
        )

    async def memory_restore(
        self,
        *,
        agent_id: str,
        relationship_uuids: str | Sequence[str],
        reason: str,
        scope: str = "default",
    ) -> list[RestoreArchivedMemoryResult]:
        """Revive one or more archived memories — the explicit curation action.

        Available in both retrieval modes.  ``memory_search`` reports archived
        matches on ``result.archived``; by default it also revives them, and
        with ``DreamConfig.pure_read_retrieval`` on it does not — this is how an
        agent asks for a specific archived memory back either way.  Each restore
        emits the same ``PRUNING_GHOST_RESTORED`` receipt and
        ``pruning_ghost_regret`` dream decision the read path emits.

        Project scope is refused for the same reason ``memory_forget`` refuses
        it: agents never write shared truth directly.
        """
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        if target_scope == self.project_scope:
            raise ValueError(
                "agents cannot restore project memory directly; use the governed operator curation workflow"
            )
        requested = [relationship_uuids] if isinstance(relationship_uuids, str) else list(relationship_uuids)
        normalized_uuids = [_normalize_non_blank(candidate, "relationship_uuid") for candidate in requested]
        if not normalized_uuids:
            raise ValueError("at least one relationship_uuid is required")
        normalized_reason = _normalize_non_blank(reason, "reason")
        return [
            await self.client.restore_archived_memory(
                relationship_uuid=candidate,
                scope=target_scope,
                reason=normalized_reason,
                requested_by=f"agent:{agent_id}",
            )
            for candidate in normalized_uuids
        ]

    async def memory_set_visibility(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        agents: list[str] | tuple[str, ...] | None,
        reason: str,
        scope: str = "default",
    ):
        """WS-19 T22: restrict one OWN memory to an explicit agent allowlist.

        Same authorization contract as ``memory_pin``: rows in the caller's
        OWN writable scopes only (own agent scope, or the shared user scope in
        simple mode); project rows are refused — project visibility belongs to
        operators via the admin surface.  ``agents=None`` clears the
        restriction.  Enforcement is agent-plane and fail-closed: restricted
        rows disappear from other agents' search/profile/explain/utility
        reads, while operator reads (reader ``None``) are unaffected.
        """
        agent_id = self._require_agent(agent_id)
        target_scope = self._target_scope(agent_id=agent_id, scope=scope)
        self._authorize(agent_id=agent_id, scope=target_scope)
        if target_scope == self.project_scope:
            raise ValueError(
                "agents cannot set project memory visibility directly; use the governed operator visibility workflow"
            )
        self._authorize_row(
            agent_id=agent_id,
            relationship_uuid=_normalize_non_blank(relationship_uuid, "relationship_uuid"),
            scope=target_scope,
            action="change the visibility of",
            require_on_allowlist=True,
        )
        return await self.client.set_memory_visibility(
            relationship_uuid=_normalize_non_blank(
                relationship_uuid,
                "relationship_uuid",
            ),
            scope=target_scope,
            agents=agents,
            reason=_normalize_non_blank(reason, "reason"),
            set_by=f"agent:{agent_id}",
        )

    def project_memory_candidates(
        self,
        *,
        agent_id: str,
        pending_only: bool = True,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        normalized_agent = self._require_agent(agent_id)
        self._authorize(agent_id=normalized_agent, scope=self.project_scope)
        if limit <= 0 or limit > 200:
            raise ValueError("limit must be between 1 and 200")
        config = self.project_memory_config()
        min_endorsements = config.min_endorsements if config is not None else 1
        candidates: list[dict[str, Any]] = []
        for episode in reversed(self.client.graph.episodes_for_scope(self.project_scope.key)):
            if episode.metadata.get("agent_memory_event") != "project_memory_candidate":
                continue
            processed = self.client.graph.is_episode_processed(episode.uuid)
            if pending_only and processed:
                continue
            entry = {
                "episode_uuid": episode.uuid,
                "content": episode.body,
                "created_at": episode.created_at.isoformat(),
                "processed": processed,
                "agent_id": str(episode.metadata.get("agent_id", "")),
                "task_run_id": str(episode.metadata.get("task_run_id", "")),
                "source_reference": str(episode.metadata.get("source_reference", "")),
                "policy_version": str(episode.metadata.get("project_memory_policy_version", "")),
            }
            # WS-19 T20: promotion candidates surface their vote state so
            # reviewers see how far each one is from the formation threshold.
            if episode.metadata.get("promotion") is True:
                endorsements = self.client.graph.promotion_endorsements_for(episode.uuid)
                endorser_ids = [str(row["agent_id"]) for row in endorsements]
                entry.update(
                    {
                        "promotion": True,
                        "source_relationship_uuid": str(episode.metadata.get("source_relationship_uuid", "")),
                        "source_scope_key": str(episode.metadata.get("source_scope_key", "")),
                        "endorsement_count": len(endorser_ids),
                        "endorsers": endorser_ids,
                        "min_endorsements": min_endorsements,
                        "eligible_for_formation": (len(endorser_ids) >= min_endorsements),
                    }
                )
            candidates.append(entry)
            if len(candidates) >= limit:
                break
        return candidates
