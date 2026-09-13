"""Choosing what happens to a candidate, and writing down why.

_decide is the entry point and _fallback_decision is what runs when the agent
transport cannot be reached -- that pairing is the reason this is one file. A
fallback that silently diverged from the primary path would produce decisions
nobody could tell apart afterwards, and the receipt would look identical.

The scoring half (_score_memories, _filter_scored_memories,
_emit_low_salience_receipt) decides what NEVER enters the graph. That is a quieter
kind of loss than pruning: a memory that was dropped at the gate has no row, no
ghost and no receipt chain to follow, so _emit_low_salience_receipt exists
specifically so the drop is still auditable.

_reject_raw_secret_memory and _secret_reference_metadata are the fail-closed
secret gate. They belong with the decision plane because rejecting a secret IS a
decision and gets receipted like one."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING, Any

from memotron.agents import (
    DreamAgentContractError,
    DreamAgentDecision,
    DreamAgentDecisionRequest,
)
from memotron.config import (
    DreamJob,
    SalienceRubric,
    claim_mode_stance,
)
from memotron.dreaming._common import (
    _DREAM_AGENT_FALLBACK_TRANSPORT,
    _FAIL_CLOSED_DECISION_TYPES,
    _ContentProtection,
    _log,
)
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.embedding import (
    cosine_similarity,
)
from memotron.extraction import (
    CandidateViolation,
    match_relationship_instruction,
)
from memotron.models import (
    ClaimMode,
    DreamDecisionRecord,
    DreamJobKind,
    Episode,
    ExtractedMemory,
    GraphRelationship,
    MemoryScope,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    candidate_digest,
)
from memotron.redaction import (
    RAW_SECRET_ASSIGNMENT_PATTERN,
    RAW_SECRET_TOKEN_PATTERN,
    SECRET_CONTEXT_PATTERN,
    SECRET_POINTER_PATTERN,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class DecisionMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    async def _decide(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        decision_type: str,
        proposed_action: str,
        subject_id: str | None = None,
        subject_name: str | None = None,
        scope: MemoryScope | None = None,
        context_facts: tuple[str, ...] = (),
        details: dict[str, object] | None = None,
    ) -> DreamAgentDecision:
        """Ask the dream agent for one decision; NEVER let its transport kill the run.

        A dream-agent transport is a network/subprocess dependency (the Claude
        Agent SDK spawns a nested CLI; the OpenAI-compatible transport calls a
        gateway).  Before WS-24 any failure inside ``decide`` — a provider 500,
        a connection reset, a malformed completion, an SDK-internal
        ``Exception`` — propagated straight out of ``_decide`` through
        ``run_due_dreams`` and aborted the whole maintenance cycle, losing the
        formation, consolidation, and pruning work that had nothing to do with
        the failing call.

        An unreadable ANSWER (``DreamAgentContractError``) is contained by the
        same path as an unreachable transport, and resolves the same way per
        decision type — but is receipted as its own failure kind, because a
        model answering out of contract is a prompt problem, not an outage.

        The failure is now contained here and made VISIBLE on both audit
        surfaces before the run continues:

        * a ``DREAM_AGENT_TRANSPORT_FAILED`` receipt on the run's hash chain,
          mirroring the rollup-synthesis transport-error path — the reason
          carries the provider's message, and the receipt ledger's content-free
          enforcement diverts it into the sealed payload for a protected scope;
        * the ``DreamDecisionRecord`` itself, whose summary names the fallback
          and whose details carry a content-free failure code, so
          ``dream_decisions()`` and the admin UI show a fallback decision as a
          fallback rather than as a real agent approval;
        * a ``WARNING`` log line with the full exception for operators.

        The decision used is the deterministic one
        (:data:`_DREAM_AGENT_FALLBACK_TRANSPORT`) — except for
        :data:`_FAIL_CLOSED_DECISION_TYPES`, which reject because approving
        them without an answer would remove memory or bypass an
        operator-required approval.  Nothing is ever swallowed silently.
        """
        request = DreamAgentDecisionRequest(
            agent_id=job.agent.agent_id,
            agent_name=job.agent.name,
            decision_policy=job.agent.decision_policy,
            job_name=job.name,
            job_kind=job.kind,
            decision_type=decision_type,
            proposed_action=proposed_action,
            subject_id=subject_id,
            subject_name=subject_name,
            scope=scope,
            prompt_profile=job.prompt_profile if job.kind != DreamJobKind.PRUNING else None,
            prompt_profile_version=job.prompt_profile_version if job.kind != DreamJobKind.PRUNING else None,
            context_facts=context_facts,
            details=dict(details or {}),
        )
        try:
            decision = await self._agent_transport.decide(request)
        except Exception as exc:
            # Deliberately broad.  The production failure this contains was a
            # bare ``Exception("Claude Code returned an error result: success")``
            # raised inside the Agent SDK, so narrowing to ValueError (the
            # transports' own contract) would not have contained it.
            # ``asyncio.CancelledError``/``KeyboardInterrupt``/``SystemExit``
            # are BaseException and still propagate — cancellation must stay
            # cancellation.
            decision = await self._fallback_decision(
                request=request,
                receipt_run=receipt_run,
                now=now,
                exc=exc,
            )
        await self._record_decision(
            job=job,
            now=now,
            decision_type=decision_type,
            subject_id=subject_id,
            subject_name=subject_name,
            scope=scope,
            summary=decision.summary,
            details={
                **dict(details or {}),
                **decision.details,
                "approved": decision.approved,
                "proposed_action": proposed_action,
            },
        )
        return decision

    async def _fallback_decision(
        self,
        *,
        request: DreamAgentDecisionRequest,
        receipt_run: ReceiptRun,
        now: datetime,
        exc: Exception,
    ) -> DreamAgentDecision:
        """Receipt an unusable dream-agent answer and return the fallback decision.

        Two distinct failures land here and are receipted distinctly:

        * ``transport`` — the model never spoke (missing key, HTTP error,
          connection reset, malformed provider envelope, SDK-internal
          ``Exception``).  An infrastructure problem.
        * ``decision_contract`` — the model DID answer and the answer is not a
          usable decision (:class:`~memotron.agents.DreamAgentContractError`).
          A prompt/model problem, and strictly the more alarming of the two,
          because a rejection may be exactly what was lost.

        Which fallback applies is decided by
        :data:`_FAIL_CLOSED_DECISION_TYPES`, i.e. by what an approval would
        AUTHORIZE — identically for both failure kinds.
        """
        fail_closed = request.decision_type in _FAIL_CLOSED_DECISION_TYPES
        contract_failure = isinstance(exc, DreamAgentContractError)
        failure_kind = "decision_contract" if contract_failure else "transport"
        _log.warning(
            "dream-agent %s failure (%s) for decision %r on job %r; falling back (approved=%s)",
            failure_kind,
            type(self._agent_transport).__name__,
            request.decision_type,
            request.job_name,
            not fail_closed,
            exc_info=exc,
        )
        receipt_fields: dict[str, object] = {}
        if request.scope is not None:
            receipt_fields["scope_key"] = request.scope.key
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.DREAM_AGENT_TRANSPORT_FAILED,
            # Mirrors the rollup-synthesis transport-error reason: the provider's
            # own message is the operator-useful part, and the ledger's
            # content-free enforcement seals it for a protected scope.  The
            # prefix names WHICH failure it was, so "the gateway is down" and
            # "the model is answering out of contract" are never one bucket.
            decision_reason=(
                f"dream_agent_{'decision_contract' if contract_failure else 'transport'}_error:{str(exc)[:160]}"
            ),
            decision_result="gated" if fail_closed else "recorded",
            now=now,
            event_payload=json.dumps(
                {
                    "dream_agent_decision_type": request.decision_type,
                    "dream_agent_fallback": ("fail_closed_reject" if fail_closed else "deterministic_approve"),
                    "dream_agent_failure_kind": failure_kind,
                    "dream_agent_transport": type(self._agent_transport).__name__,
                    "dream_agent_transport_error_class": type(exc).__name__,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            **receipt_fields,
        )
        # The decision record is a plaintext operator surface with no sealing of
        # its own, so it carries the content-free failure CLASS only; the
        # provider's message lives in the receipt above, which is sealed.
        failure_details: dict[str, Any] = {
            "dream_agent_transport_error": type(exc).__name__,
            "dream_agent_transport": type(self._agent_transport).__name__,
            "dream_agent_failure_kind": failure_kind,
        }
        lost = (
            "returned an unreadable dream-agent decision"
            if contract_failure
            else "could not reach the dream-agent transport"
        )
        if fail_closed:
            return DreamAgentDecision(
                approved=False,
                summary=(
                    f"{request.agent_name} {lost}; {request.decision_type} is NOT "
                    "approved (fail-closed: a fallback may approve creation, never "
                    "removal, and never an operator-required approval)."
                ),
                details={
                    **failure_details,
                    "transport": "fallback-fail-closed",
                    "dream_agent_fallback": "fail_closed_reject",
                    "proposed_action": request.proposed_action,
                },
            )
        deterministic = await _DREAM_AGENT_FALLBACK_TRANSPORT.decide(request)
        return DreamAgentDecision(
            approved=deterministic.approved,
            summary=(
                f"{request.agent_name} {lost}; applied the deterministic fallback decision for {request.decision_type}."
            ),
            details={
                **deterministic.details,
                **failure_details,
                # Overrides the deterministic transport's own "local" marker so a
                # fallback approval is never mistaken for a configured local run.
                "transport": "fallback-local",
                "dream_agent_fallback": "deterministic_approve",
            },
        )

    async def _pruning_decision(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        relationship: GraphRelationship,
        decision_type: str,
        proposed_action: str,
        reason: str,
        details: dict[str, object] | None = None,
    ) -> DreamAgentDecision:
        relationship_scope = self._relationship_scope(relationship)
        subject = str(self._graph.get_node(relationship.source_uuid).properties["name"])
        object_value = str(self._graph.get_node(relationship.target_uuid).properties["name"])
        fact = str(relationship.properties.get("fact", f"{subject} {relationship.type} {object_value}"))
        return await self._decide(
            job=job,
            now=now,
            receipt_run=receipt_run,
            decision_type=decision_type,
            proposed_action=proposed_action,
            subject_id=relationship.uuid,
            subject_name=fact,
            scope=relationship_scope,
            context_facts=(fact,),
            details={
                "relationship_uuid": relationship.uuid,
                "relationship_type": relationship.type,
                "relationship_status": relationship.properties.get("status"),
                "reason": reason,
                **dict(details or {}),
            },
        )

    async def _record_decision(
        self,
        *,
        job: DreamJob,
        now: datetime,
        decision_type: str,
        summary: str,
        subject_id: str | None = None,
        subject_name: str | None = None,
        scope: MemoryScope | None = None,
        details: dict[str, object] | None = None,
    ) -> None:
        self._graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=now,
                job_name=job.name,
                job_kind=job.kind,
                agent_id=job.agent.agent_id,
                agent_name=job.agent.name,
                agent_scope=job.agent.scope,
                decision_type=decision_type,
                summary=summary,
                subject_id=subject_id,
                subject_name=subject_name,
                scope=scope,
                prompt_profile=job.prompt_profile if job.kind != DreamJobKind.PRUNING else None,
                prompt_profile_version=job.prompt_profile_version if job.kind != DreamJobKind.PRUNING else None,
                details=dict(details or {}),
            )
        )

    def _relationship_status(self, raw_status: object) -> RelationshipStatus:
        if raw_status is None:
            return RelationshipStatus.ACTIVE
        return RelationshipStatus(str(raw_status))

    def _relationship_scope(self, relationship: GraphRelationship) -> MemoryScope:
        scope_kind = relationship.properties.get("scope_kind")
        scope_id = relationship.properties.get("scope_id")
        if not isinstance(scope_kind, str) or not isinstance(scope_id, str):
            raise ValueError(f"relationship missing scope metadata: {relationship.uuid}")
        return MemoryScope(kind=scope_kind, scope_id=scope_id)

    def _relationship_episode_uuids(self, relationship: GraphRelationship) -> list[str]:
        episode_uuids = relationship.properties.get("episode_uuids")
        if isinstance(episode_uuids, list):
            return [str(episode_uuid) for episode_uuid in episode_uuids]
        return [str(relationship.properties["episode_uuid"])]

    def _secret_reference_metadata(self, memory: ExtractedMemory) -> tuple[str | None, str | None]:
        """Return governed secret-reference metadata or reject a raw secret value.

        Secret material is allowed only as a named pointer into an external
        secret manager/keychain.  This gate runs before nodes, facts, embeddings,
        or receipt payload text are written, so a rejected credential cannot leak
        into any content plane.
        """
        metadata = memory.metadata
        raw_reference = metadata.get("secret_reference")
        reference = raw_reference.strip() if isinstance(raw_reference, str) else None
        fields = (memory.subject, memory.predicate, memory.object, memory.source_text or "")
        combined = " ".join(fields)
        pointer_in_object = SECRET_POINTER_PATTERN.fullmatch(memory.object.strip())
        raw_assignment = RAW_SECRET_ASSIGNMENT_PATTERN.search(combined)
        raw_token = RAW_SECRET_TOKEN_PATTERN.search(combined)
        # A requirement *about* needing an API key is not secret material.  A
        # secret-labelled subject/predicate, an explicit assignment, a raw token,
        # or a governed pointer is.  Restricting the gate this way prevents it
        # from rejecting ordinary integration requirements while still blocking
        # values before storage.
        secret_context = (
            bool(SECRET_CONTEXT_PATTERN.search(f"{memory.subject} {memory.predicate}"))
            or reference is not None
            or pointer_in_object is not None
            or raw_assignment is not None
            or raw_token is not None
        )

        if reference is not None and not SECRET_POINTER_PATTERN.fullmatch(reference):
            raise ValueError(
                "secret_reference must be a governed secret://, vault://, keychain://, or awssecrets:// pointer"
            )
        if raw_token is not None:
            raise ValueError("raw credential token detected; store a governed secret reference instead")
        if raw_assignment is not None and not SECRET_POINTER_PATTERN.fullmatch(raw_assignment.group(1)):
            raise ValueError("raw credential assignment detected; store a governed secret reference instead")
        if secret_context and reference is None and pointer_in_object is None:
            raise ValueError("credential memory requires metadata.secret_reference with a governed pointer")
        if reference is None and pointer_in_object is not None:
            reference = memory.object.strip()
        if reference is None:
            return None, None
        lifecycle = metadata.get("secret_lifecycle", "active")
        if not isinstance(lifecycle, str) or lifecycle not in {"active", "rotated", "revoked", "retired"}:
            raise ValueError("secret_lifecycle must be active, rotated, revoked, or retired")
        return reference, lifecycle

    def _reject_raw_secret_memory(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        memory: ExtractedMemory,
        now: datetime | None,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None,
        error: ValueError,
    ) -> None:
        memory_type_value = self._resolve_memory_type_value(
            relationship_type=memory.relationship_type,
            instruction_set_name=episode.instruction_set,
            subject_label=memory.subject_label,
            object_label=memory.object_label,
        )
        claim_mode = (
            self._claim_mode_for(memory=memory, memory_type_value=memory_type_value)
            if memory_type_value is not None
            else None
        )
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.FORMATION_RAW_SECRET_REJECTED,
            decision_reason=str(error),
            decision_result="rejected",
            episode=episode,
            now=now or episode.reference_time,
            candidate_digest=candidate_digest(
                memory,
                memory_type=memory_type_value,
                episode_uuid=episode.uuid,
                content_key=protection.key if protection is not None else None,
            ),
            motive_name=motive_name,
            memory_type=memory_type_value,
            claim_mode=claim_mode.value if claim_mode is not None else None,
            directive_stance=claim_mode_stance(claim_mode).value if claim_mode is not None else None,
            relationship_type=memory.relationship_type,
            governance_policy_digest=governance_policy_digest,
        )

    def _relationship_visible_at_episode_time(self, relationship: GraphRelationship, episode: Episode) -> bool:
        if relationship.valid_from is not None and relationship.valid_from > episode.reference_time:
            return False
        return not (relationship.valid_to is not None and relationship.valid_to <= episode.reference_time)

    def _score_memories(
        self,
        *,
        memories: list[ExtractedMemory],
        episode: Episode,
        rubric: SalienceRubric,
    ) -> list[ExtractedMemory]:
        """WS-2: Attach salience_score to each memory WITHOUT dropping or reordering.

        No-op invariant: when rubric.is_noop is True (default), returns memories
        unchanged (salience_score stays 0.0).  Split out of the old
        score+filter method (WS-11) so the full extracted candidate stream can be
        receipted BEFORE any salience gate ([0024]).
        """
        if rubric.is_noop:
            return memories

        # --- Compute episode-body embedding once (for relevance component) ---
        episode_embedding: list[float] | None = None
        try:
            episode_embedding = self._embed_content(episode.body, scope_key=episode.scope.key, protection=None)
        except Exception:
            episode_embedding = None

        from memotron.config import RELATIONSHIP_TYPE_MEMORY_TYPE_MAP

        scored: list[ExtractedMemory] = []
        for memory in memories:
            # Resolve memory_type for this memory's relationship_type
            memory_type: str | None = None
            # Look up from instruction set
            instr_set = self._config.instruction_set(episode.instruction_set)
            rel_instr = match_relationship_instruction(
                instructions=instr_set,
                relationship_type=memory.relationship_type,
                subject_label=memory.subject_label,
                object_label=memory.object_label,
            )
            if rel_instr is not None and rel_instr.memory_type is not None:
                memory_type = rel_instr.memory_type.value
            else:
                resolved = RELATIONSHIP_TYPE_MEMORY_TYPE_MAP.get(memory.relationship_type)
                if resolved is not None:
                    memory_type = resolved.value

            # Recency: exponential decay from memory.valid_from vs episode.reference_time
            recency = rubric.recency_for(memory.valid_from, episode.reference_time)

            # Importance: deterministic per-type weight, optionally scaled by confidence
            importance = rubric.importance_for(memory_type, memory.confidence)

            # Relevance: cosine(memory fact embedding, episode body embedding)
            relevance = 1.0
            if episode_embedding is not None:
                try:
                    fact_text = f"{memory.subject} {memory.predicate} {memory.object}"
                    fact_embedding = self._embed_content(fact_text, scope_key=episode.scope.key, protection=None)
                    relevance = cosine_similarity(fact_embedding, episode_embedding)
                    # Cosine can be in [0,1] for L2-normalised vectors; clamp to be safe
                    relevance = max(0.0, min(1.0, relevance))
                except Exception:
                    relevance = 1.0

            # Product normalised to [0,1] — all three components are in [0,1]
            salience = recency * importance * relevance

            scored.append(memory.model_copy(update={"salience_score": salience}))
        return scored

    def _emit_low_salience_receipt(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        memory: ExtractedMemory,
        rubric: SalienceRubric,
        now: datetime,
        reason: str,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None = None,
    ) -> None:
        # Real labels (see _resolve_memory_type_value): this candidate already
        # resolved once at CANDIDATE_EXTRACTED time using the same matcher, so
        # this MUST agree.  Tolerate a miss rather than raise — stage 3 never
        # aborts a run — by falling back to "unknown" for the receipt fields
        # that would otherwise require it.
        memory_type_value = self._resolve_memory_type_value(
            relationship_type=memory.relationship_type,
            instruction_set_name=episode.instruction_set,
            subject_label=memory.subject_label,
            object_label=memory.object_label,
        )
        claim_mode = (
            self._claim_mode_for(memory=memory, memory_type_value=memory_type_value)
            if memory_type_value is not None
            else (memory.claim_mode or ClaimMode.DESCRIPTIVE_ASSERTION)
        )
        digest = candidate_digest(
            memory,
            memory_type=memory_type_value,
            episode_uuid=episode.uuid,
            content_key=protection.key if protection is not None else None,
        )
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.CANDIDATE_LOW_SALIENCE,
            decision_reason=reason,
            decision_result="gated",
            episode=episode,
            now=now,
            candidate_digest=digest,
            motive_name=motive_name,
            memory_type=memory_type_value,
            claim_mode=claim_mode.value,
            directive_stance=claim_mode_stance(claim_mode).value,
            relationship_type=memory.relationship_type,
            salience_score=memory.salience_score,
            salience_threshold=rubric.min_salience,
            governance_policy_digest=governance_policy_digest,
        )
        self._mark_raw_candidate_quarantined(
            digest=digest,
            now=now,
            reason=CandidateViolation.MEMORY_TYPE_UNRESOLVED.value
            if memory_type_value is None
            else "below_salience_threshold",
            resolution_note=reason,
        )

    def _filter_scored_memories(
        self,
        *,
        scored: list[ExtractedMemory],
        episode: Episode,
        rubric: SalienceRubric,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        motive_name: str | None = None,
        governance_policy_digest: str | None = None,
        protection: _ContentProtection | None = None,
    ) -> list[ExtractedMemory]:
        """WS-2/WS-11: filter below-salience + cap, receipting every dropped candidate.

        No-op invariant: when rubric.is_noop is True the scored list is returned
        unchanged (no drops, no receipts, no DreamDecisionRecords).  Otherwise:
          1. Filter below min_salience → log + DreamDecisionRecord + CANDIDATE_LOW_SALIENCE.
          2. Sort survivors descending by salience_score.
          3. Truncate to max_memories_per_episode → log + DreamDecisionRecord + receipts.
        """
        if rubric.is_noop:
            return scored

        # --- 3. Filter below min_salience ---
        below_floor: list[ExtractedMemory] = []
        survivors: list[ExtractedMemory] = []
        for m in scored:
            if m.salience_score < rubric.min_salience:
                below_floor.append(m)
            else:
                survivors.append(m)

        for m in below_floor:
            self._emit_low_salience_receipt(
                receipt_run=receipt_run,
                episode=episode,
                memory=m,
                rubric=rubric,
                now=now,
                reason="below_min_salience",
                motive_name=motive_name,
                governance_policy_digest=governance_policy_digest,
                protection=protection,
            )

        if below_floor:
            dropped_subjects = [
                protection.seal(m.subject) if protection is not None else m.subject for m in below_floor
            ]
            _log.info(
                "WS-2 salience filter: dropped %d memories below min_salience=%.4f in episode %s (%s): %s",
                len(below_floor),
                rubric.min_salience,
                episode.uuid,
                episode.name,
                dropped_subjects,
            )
            self._graph.record_dream_decision(
                DreamDecisionRecord(
                    ran_at=now,
                    job_name=job.name,
                    job_kind=job.kind,
                    agent_id=job.agent.agent_id,
                    agent_name=job.agent.name,
                    agent_scope=job.agent.scope,
                    decision_type="formation_salience_filtered",
                    summary=(
                        f"{job.agent.name} dropped {len(below_floor)} memories below "
                        f"min_salience={rubric.min_salience:.4f} in episode {episode.name!r}."
                    ),
                    subject_id=episode.uuid,
                    subject_name=episode.name,
                    scope=episode.scope,
                    prompt_profile=job.prompt_profile,
                    prompt_profile_version=job.prompt_profile_version,
                    details={
                        "drop_reason": "below_min_salience",
                        "dropped_count": len(below_floor),
                        "dropped_subjects": dropped_subjects,
                        "min_salience": rubric.min_salience,
                        "approved": True,
                    },
                )
            )

        # --- 4. Sort survivors descending by salience_score ---
        survivors.sort(key=lambda m: m.salience_score, reverse=True)

        # --- 5. Truncate to max_memories_per_episode ---
        cap = rubric.max_memories_per_episode
        if cap is not None and len(survivors) > cap:
            truncated = survivors[cap:]
            survivors = survivors[:cap]
            truncated_subjects = [
                protection.seal(m.subject) if protection is not None else m.subject for m in truncated
            ]
            for m in truncated:
                self._emit_low_salience_receipt(
                    receipt_run=receipt_run,
                    episode=episode,
                    memory=m,
                    rubric=rubric,
                    now=now,
                    reason="max_memories_per_episode",
                    motive_name=motive_name,
                    governance_policy_digest=governance_policy_digest,
                    protection=protection,
                )
            _log.info(
                "WS-2 salience cap: truncated %d memories to max_memories_per_episode=%d in episode %s (%s): %s",
                len(truncated),
                cap,
                episode.uuid,
                episode.name,
                truncated_subjects,
            )
            self._graph.record_dream_decision(
                DreamDecisionRecord(
                    ran_at=now,
                    job_name=job.name,
                    job_kind=job.kind,
                    agent_id=job.agent.agent_id,
                    agent_name=job.agent.name,
                    agent_scope=job.agent.scope,
                    decision_type="formation_salience_filtered",
                    summary=(
                        f"{job.agent.name} truncated {len(truncated)} memories to cap "
                        f"max_memories_per_episode={cap} in episode {episode.name!r}."
                    ),
                    subject_id=episode.uuid,
                    subject_name=episode.name,
                    scope=episode.scope,
                    prompt_profile=job.prompt_profile,
                    prompt_profile_version=job.prompt_profile_version,
                    details={
                        "drop_reason": "max_memories_per_episode",
                        "dropped_count": len(truncated),
                        "dropped_subjects": truncated_subjects,
                        "max_memories_per_episode": cap,
                        "approved": True,
                    },
                )
            )

        return survivors
