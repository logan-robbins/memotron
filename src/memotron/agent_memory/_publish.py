"""Moving a memory from one agent's scope into the project's, with someone accountable.

Ten members, and the promotion half is where the care is. Publishing to a shared
scope is the one operation here that changes what OTHER agents see, so it is gated
rather than merely validated: `_promotion_authority` decides who may, and
`memory_endorse_promotion` is the second signature when one is required.

`_own_readable_source_scope` is the quiet one -- it refuses to promote a row the
requesting agent could not itself read. Without it, promotion is an exfiltration
path from a scope the agent was never granted."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import (
    AgentMemoryMode,
    ProjectMemoryConfig,
    ProjectMemoryPublishResult,
    PromotionResult,
)
from memotron.crypto import SHREDDED_CONTENT_PLACEHOLDER
from memotron.models import (
    AddMemoryResult,
    Episode,
    EpisodeType,
    MemoryAuthority,
    MemoryProfile,
    MemoryScope,
    RelationshipStatus,
    UseEvent,
    UseEventKind,
)
from memotron.receipts import payload_digest
from memotron.redaction import contains_raw_credentials

PROJECT_MEMORY_POLICY_MOTIVE = "project-memory-policy"

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryPublishMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    mode: Any
    project_scope: Any
    tenant_id: Any
    user_scope: Any

    async def _record_profile_injections(
        self,
        *,
        profile: MemoryProfile,
        task_run_id: str,
        token_budget: int,
    ) -> list[UseEvent]:
        items = profile.injected_items
        policy_digest = payload_digest(
            {
                "operation": "agent_memory_start",
                "scope_key": profile.scope.key,
                "token_budget": token_budget,
                "tokens_used": profile.tokens_used,
                "injected_count": len(items),
                "reference_count": len(profile.reference_items),
            }
        )
        return [
            await self.client.record_memory_use(
                relationship_uuid=item.relationship_uuid,
                scope=profile.scope,
                kind=UseEventKind.INJECTED,
                task_run_id=task_run_id,
                idempotency_key=(f"{task_run_id}:{profile.scope.key}:{item.relationship_uuid}:injected"),
                rank=rank,
                retrieval_score=1.0 / (rank + 1),
                candidate_set_size=len(items) + len(profile.reference_items),
                context_budget_competition=len(profile.reference_items),
                retrieval_policy_digest=policy_digest,
                metadata={"mode": item.mode},
            )
            for rank, item in enumerate(items)
        ]

    async def memory_remember(
        self,
        *,
        agent_id: str,
        subject: str,
        predicate: str,
        object: str,
        relationship_type: str,
        source_text: str = "",
        confidence: float = 0.9,
    ) -> AddMemoryResult:
        agent_id = self._require_agent(agent_id)
        target_scope = self.user_scope or self.agent_scope(agent_id)
        self._authorize(agent_id=agent_id, scope=target_scope)
        policy = self.client.resolve_policy(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            scope=target_scope,
        )
        return await self.client.add_memory(
            subject=subject,
            predicate=predicate,
            object=object,
            relationship_type=relationship_type,
            scope=target_scope,
            confidence=confidence,
            source_text=source_text or None,
            source_description="agent-memory exact fact",
            metadata={
                "agent_memory": True,
                "tenant_id": self.tenant_id,
                "agent_id": agent_id,
                "memory_scope": ("personal" if self.mode == AgentMemoryMode.SIMPLE else "agent"),
                "_verified_source_authority": "agent",
            },
            motive=policy.motive,
        )

    async def memory_publish(
        self,
        *,
        agent_id: str,
        content: str,
        task_run_id: str,
        source_reference: str = "",
    ) -> ProjectMemoryPublishResult:
        agent_id = self._require_agent(agent_id)
        self._authorize(agent_id=agent_id, scope=self.project_scope)
        config = self._require_project_memory_config()
        normalized_content = _normalize_non_blank(content, "content")
        if contains_raw_credentials(normalized_content):
            raise ValueError(
                "project memory candidate contains a raw credential; publish a governed secret reference instead"
            )
        normalized_task_run_id = _normalize_non_blank(task_run_id, "task_run_id")
        normalized_source_reference = source_reference.strip()
        queued = await self.client.add_episode(
            name=(f"project-memory-candidate:{agent_id}:{datetime.now(UTC).isoformat()}"),
            episode_body=normalized_content,
            source=EpisodeType.TEXT,
            source_description="project memory candidate",
            scope=self.project_scope,
            metadata={
                "agent_memory": True,
                "agent_memory_event": "project_memory_candidate",
                "tenant_id": self.tenant_id,
                "agent_id": agent_id,
                "task_run_id": normalized_task_run_id,
                "project_memory_policy_version": config.version,
                "source_reference": normalized_source_reference,
                "_verified_source_authority": "agent",
            },
            motive=PROJECT_MEMORY_POLICY_MOTIVE,
        )
        return ProjectMemoryPublishResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project_scope=self.project_scope,
            policy_version=config.version,
            task_run_id=normalized_task_run_id,
            episode_uuid=queued.episode_uuid,
            queued_for_dreaming=queued.queued_for_dreaming,
        )

    def _require_project_memory_config(self) -> ProjectMemoryConfig:
        config = self.project_memory_config()
        if config is None:
            raise ValueError(
                "project memory is not configured; an operator must call "
                "project_memory_configure before agents can publish candidates"
            )
        return config

    def _own_readable_source_scope(self, *, agent_id: str, relationship: Any) -> MemoryScope:
        """WS-19 T20: resolve + authorize the source row's scope for promotion.

        Promotable rows live in the caller's OWN readable scopes only — the
        agent's private scope and (simple mode) the shared user scope.
        Project rows cannot be re-promoted; any other scope is refused.
        """
        properties = relationship.properties
        scope_kind = properties.get("scope_kind")
        scope_id = properties.get("scope_id")
        if not scope_kind or not scope_id:
            raise ValueError(f"relationship {relationship.uuid} carries no scope identity and cannot be promoted")
        source_scope = MemoryScope(kind=str(scope_kind), scope_id=str(scope_id))
        if source_scope.key == self.project_scope.key:
            raise ValueError(
                "project-scope rows cannot be re-promoted; memory_promote takes a "
                "fact from the caller's own user/agent scope into project memory"
            )
        own_scope_keys = {self.agent_scope(agent_id).key}
        if self.user_scope is not None:
            own_scope_keys.add(self.user_scope.key)
        if source_scope.key not in own_scope_keys:
            raise ValueError(
                f"relationship {relationship.uuid} lives in scope "
                f"{source_scope.key!r}, outside the caller's own readable scopes; "
                "only user/agent scope facts can be promoted"
            )
        self._authorize(agent_id=agent_id, scope=source_scope)
        return source_scope

    def _promotion_authority(self, relationship: Any) -> MemoryAuthority:
        """WS-23 H3: the authority a promoted fact may enter project memory at.

        ``min(source authority, agent)`` under the configured authority ranks:
        promotion re-publishes an existing governed fact, so it can lower a rank
        (an operator/system fact becomes an agent-rank project fact — a vote is
        an agent act) but never raise one.  A source row below agent rank —
        ``untrusted`` or ``generated_output`` — is refused: it would otherwise
        bypass the untrusted-directive gate by laundering through the promotion
        channel, which is exactly what a prompt-injected agent would reach for.
        """
        supersession = self.client.config.supersession
        # The engine owns "what authority is this row" — one reader, so a
        # promoted fact is ranked exactly as the truth gate would rank it.
        source_authority = self.client._engine._relationship_authority(relationship)
        agent_rank = supersession.authority_rank(MemoryAuthority.AGENT)
        source_rank = supersession.authority_rank(source_authority)
        if source_rank < agent_rank:
            raise ValueError(
                f"relationship {relationship.uuid} carries "
                f"{source_authority.value!r} authority, below agent rank; "
                "promotion re-publishes a governed fact and cannot raise its "
                "authority — correct or re-source the fact first"
            )
        return source_authority if source_rank <= agent_rank else MemoryAuthority.AGENT

    def _pending_promotion_candidate_for(self, source_relationship_uuid: str) -> Episode | None:
        """The newest UNPROCESSED promotion candidate for one source row.

        Deterministic dedup key: ``source_relationship_uuid``.  While such a
        candidate is pending, re-promoting the same row endorses it instead of
        creating a duplicate.
        """
        for episode in reversed(self.client.graph.episodes_for_scope(self.project_scope.key)):
            metadata = episode.metadata
            if metadata.get("agent_memory_event") != "project_memory_candidate":
                continue
            if metadata.get("promotion") is not True:
                continue
            if str(metadata.get("source_relationship_uuid", "")) != source_relationship_uuid:
                continue
            if self.client.graph.is_episode_processed(episode.uuid):
                continue
            return episode
        return None

    def _promotion_result(
        self,
        *,
        agent_id: str,
        episode: Episode,
        config: ProjectMemoryConfig,
        created: bool,
        task_run_id: str = "",
    ) -> PromotionResult:
        endorsements = self.client.graph.promotion_endorsements_for(episode.uuid)
        endorser_ids = tuple(str(row["agent_id"]) for row in endorsements)
        return PromotionResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            project_scope=self.project_scope,
            policy_version=str(episode.metadata.get("project_memory_policy_version", config.version)),
            candidate_episode_uuid=episode.uuid,
            source_relationship_uuid=str(episode.metadata.get("source_relationship_uuid", "")),
            source_scope_key=str(episode.metadata.get("source_scope_key", "")),
            source_receipt_digest=str(episode.metadata.get("source_receipt_digest", "")),
            created=created,
            endorsement_count=len(endorser_ids),
            endorsements=endorser_ids,
            min_endorsements=config.min_endorsements,
            eligible_for_formation=len(endorser_ids) >= config.min_endorsements,
            task_run_id=task_run_id,
        )

    async def memory_promote(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        rationale: str,
        task_run_id: str = "",
    ) -> PromotionResult:
        """WS-19 T20: promote one exact governed fact into project memory.

        Object-level "vote up": the source row (from the caller's OWN
        user/agent scope) is carried VERBATIM — subject, predicate, object,
        relationship type, confidence, valid_from, metadata — into ONE
        structured project-scope candidate episode whose JSON body feeds the
        deterministic rule-based extraction path (zero LLM; the fact is never
        paraphrased).  Full lineage rides on the candidate:
        ``source_relationship_uuid``, ``source_scope_key``, and the source
        row's latest FORMATION_* receipt digest.  The promoting agent's
        endorsement is recorded immediately; formation consumes the candidate
        only once endorsements reach the project policy's ``min_endorsements``
        (until then it stays pending, never consumed).  Once eligible, the
        normal project pipeline still applies — the project Motive's type and
        salience gates can reject a promotion, receipted as usual — and the
        materialized fact carries ``promoted_from_relationship_uuid`` /
        ``promoted_from_scope_key`` first-class.

        Re-promoting a source row while its candidate is pending endorses that
        candidate instead of creating a duplicate (deterministic dedup by
        ``source_relationship_uuid``).

        Authority note: the candidate episode carries
        ``_verified_source_authority="agent"`` — promotion is an agent act.
        Endorsements are recorded in the ledger and gate ELIGIBILITY only;
        they never raise the promoted fact's authority class.
        """
        agent_id = self._require_agent(agent_id)
        self._authorize(agent_id=agent_id, scope=self.project_scope)
        config = self._require_project_memory_config()
        normalized_uuid = _normalize_non_blank(relationship_uuid, "relationship_uuid")
        normalized_rationale = _normalize_non_blank(rationale, "rationale")
        normalized_task_run_id = task_run_id.strip()
        relationship = self.client.graph.get_relationship(normalized_uuid)
        if relationship.type == "MENTIONS":
            raise ValueError("MENTIONS relationships cannot be promoted")
        source_scope = self._own_readable_source_scope(agent_id=agent_id, relationship=relationship)
        # WS-23 C2/H2: promotion is a uuid-taking mutation like pin/forget — the
        # per-memory allowlist gates it at the same choke point, so another
        # agent's restricted row cannot be laundered verbatim into the shared
        # project candidate body.
        self._authorize_row(
            agent_id=agent_id,
            relationship_uuid=normalized_uuid,
            scope=source_scope,
            action="promote",
        )

        pending = self._pending_promotion_candidate_for(normalized_uuid)
        if pending is not None:
            self.client.graph.record_promotion_endorsement(
                candidate_episode_uuid=pending.uuid,
                agent_id=agent_id,
                rationale=normalized_rationale,
            )
            return self._promotion_result(
                agent_id=agent_id,
                episode=pending,
                config=config,
                created=False,
                task_run_id=normalized_task_run_id,
            )

        # Decrypt-on-read source snapshot (fail-fasts: non-visible row, scope
        # mismatch); a crypto-shredded source reveals only placeholders.
        evidence = await self.client.memory_evidence(
            relationship_uuid=normalized_uuid,
            scope=source_scope,
            reader_agent_id=agent_id,
        )
        now = datetime.now(UTC)
        if (
            evidence.status != RelationshipStatus.ACTIVE
            or (evidence.valid_from is not None and evidence.valid_from > now)
            or (evidence.valid_to is not None and evidence.valid_to <= now)
        ):
            raise ValueError(
                f"relationship {normalized_uuid} is not currently visible "
                f"(status={evidence.status.value}); only visible active facts "
                "can be promoted"
            )
        if any(
            value == SHREDDED_CONTENT_PLACEHOLDER
            for value in (
                evidence.subject,
                evidence.predicate,
                evidence.object,
                evidence.fact,
            )
        ):
            raise ValueError(
                f"relationship {normalized_uuid} belongs to crypto-shredded scope "
                f"{source_scope.key}; its content is unrecoverable and cannot be "
                "promoted"
            )
        if contains_raw_credentials(" ".join((evidence.fact, evidence.source_text or ""))):
            raise ValueError("promotion source contains a raw credential; promote a governed secret reference instead")

        # WS-23 H3: promotion carries the SOURCE row's authority forward,
        # capped at agent.  Stamping "agent" unconditionally laundered
        # authority: an untrusted or generated_output fact entered project
        # memory at agent rank, above the untrusted-directive gate (which keys
        # on the episode's trust) and above the supersession gate's
        # lower-authority parking.  Promotion can never RAISE a fact's rank
        # (that is what the min(..., agent) cap means) and rows below agent rank
        # are refused outright — an agent vote is not a laundering channel.
        promoted_authority = self._promotion_authority(relationship)
        source_metadata = {
            key: value
            for key, value in evidence.metadata.items()
            if key not in {"_verified_source_authority", "agent_memory_event"}
        }
        memory_payload: dict[str, Any] = {
            "subject": evidence.subject,
            "predicate": evidence.predicate,
            "object": evidence.object,
            "relationship_type": evidence.relationship_type,
            "confidence": evidence.confidence,
            "metadata": source_metadata,
        }
        if evidence.source_text:
            memory_payload["source_text"] = evidence.source_text
        if evidence.valid_from is not None:
            memory_payload["valid_from"] = evidence.valid_from.isoformat()

        source_receipt_digest = self.client.graph.receipts.latest_formation_receipt_digest(normalized_uuid) or ""
        queued = await self.client.add_episode(
            name=f"project-memory-promotion:{agent_id}:{now.isoformat()}",
            episode_body=json.dumps({"memories": [memory_payload]}, sort_keys=True),
            source=EpisodeType.JSON,
            source_description="project memory promotion candidate",
            scope=self.project_scope,
            metadata={
                "agent_memory": True,
                "agent_memory_event": "project_memory_candidate",
                "promotion": True,
                "tenant_id": self.tenant_id,
                "agent_id": agent_id,
                "promoted_by": agent_id,
                "task_run_id": normalized_task_run_id,
                "project_memory_policy_version": config.version,
                "source_reference": (f"promotion:{source_scope.key}:{normalized_uuid}"),
                "source_relationship_uuid": normalized_uuid,
                "source_scope_key": source_scope.key,
                "source_receipt_digest": source_receipt_digest,
                "_verified_source_authority": promoted_authority.value,
            },
            motive=PROJECT_MEMORY_POLICY_MOTIVE,
        )
        self.client.graph.record_promotion_endorsement(
            candidate_episode_uuid=queued.episode_uuid,
            agent_id=agent_id,
            rationale=normalized_rationale,
        )
        episode = self.client.graph.get_episode(queued.episode_uuid)
        return self._promotion_result(
            agent_id=agent_id,
            episode=episode,
            config=config,
            created=True,
            task_run_id=normalized_task_run_id,
        )

    async def memory_endorse_promotion(
        self,
        *,
        agent_id: str,
        candidate_episode_uuid: str,
        rationale: str,
    ) -> PromotionResult:
        """WS-19 T20: add one agent's vote to a pending promotion candidate.

        One vote per agent — re-endorsing is idempotent and returns the
        candidate's current state.  Endorsements gate formation ELIGIBILITY
        against the project policy's ``min_endorsements``; they never raise
        the candidate's authority (see memory_promote).

        WS-23 M4: over stdio MCP the ``agent_id`` is caller-declared, so a
        threshold above 1 is ADVISORY on that surface; the hosted HTTP endpoint
        binds it to the authenticated principal.  See
        ``ProjectMemoryConfig.min_endorsements``.
        """
        agent_id = self._require_agent(agent_id)
        self._authorize(agent_id=agent_id, scope=self.project_scope)
        config = self._require_project_memory_config()
        normalized_uuid = _normalize_non_blank(candidate_episode_uuid, "candidate_episode_uuid")
        normalized_rationale = _normalize_non_blank(rationale, "rationale")
        episode = self.client.graph.get_episode(normalized_uuid)
        metadata = episode.metadata
        if (
            episode.scope.key != self.project_scope.key
            or metadata.get("agent_memory_event") != "project_memory_candidate"
            or metadata.get("promotion") is not True
        ):
            raise ValueError(
                f"episode {normalized_uuid} is not a promotion candidate in project scope {self.project_scope.key}"
            )
        if self.client.graph.is_episode_processed(episode.uuid):
            raise ValueError(
                f"promotion candidate {normalized_uuid} was already consumed by formation; endorsement is closed"
            )
        self.client.graph.record_promotion_endorsement(
            candidate_episode_uuid=episode.uuid,
            agent_id=agent_id,
            rationale=normalized_rationale,
        )
        return self._promotion_result(
            agent_id=agent_id,
            episode=episode,
            config=config,
            created=False,
        )
