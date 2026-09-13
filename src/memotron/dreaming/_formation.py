"""Stage 1: turning an episode into candidate memories, and refusing the ones that
must not become memories.

_run_formation is the pass; everything else here is a gate, and the gates are the
reason this is one file. _gate_untrusted_directive_writes and
_gate_generated_output_authority both answer the same question -- may this text be
allowed to assert something? -- for two different threat models: content that came
from an untrusted party, and content this system generated itself and might
otherwise treat as evidence. Splitting them would let one be strengthened and the
other quietly left behind.

The quarantine members (_store_raw_candidate, _mark_raw_candidate_quarantined,
_mark_raw_candidate_promoted, _quarantine_unresolvable_candidate,
_file_quarantined_candidates) are stage 1s durable record. They exist so a
candidate that never became a memory still leaves a row -- which is what makes the
quarantine-and-continue regime in _materialization auditable rather than just
lossy.

_effective_* are the policy resolvers. They are here rather than on the composer
because formation is where a policy first takes effect."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import uuid4

from memotron.coherence import CoherenceScanner
from memotron.config import (
    ActionabilityPolicy,
    CoherencePolicy,
    DreamJob,
    GovernancePolicy,
    MemoryHealthPolicy,
    Motive,
    PiiSensitivity,
    SalienceRubric,
    claim_mode_stance,
)
from memotron.crypto import (
    ContentKeyUnavailableError,
    is_sealed_content,
    open_content,
)
from memotron.dreaming._common import _ContentProtection, _log
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.health import DEFAULT_ELIGIBLE_TYPES, compute_memory_health
from memotron.models import (
    ClaimMode,
    CoherenceIncidentKind,
    CoherenceReport,
    DreamDecisionRecord,
    DreamJobRun,
    Episode,
    ExtractedMemory,
    MemoryHealthReport,
    MemoryScope,
    MemoryType,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    candidate_digest,
    governance_policy_digest,
    motive_version_digest,
    payload_digest,
    rejected_candidate_digest,
)
from memotron.redaction import Redactor

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class FormationMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    async def scan_coherence(
        self,
        *,
        scope: MemoryScope,
        job: DreamJob,
        now: datetime,
        policy: CoherencePolicy | None = None,
        dry_run: bool = False,
    ) -> CoherenceReport:
        """Run one cross-artifact coherence scan over *scope*.

        Projects memory + registered skill/file artifacts into a common directive
        representation, detects escalation-windup and cross-artifact contradiction
        incidents, records an auditable dream-agent decision for each, and — for a
        windup whose policy permits — marks the fruitlessly escalating memory
        directive ``coherence_hold`` so formation stops blindly strengthening it.

        ``dry_run=True`` performs the detection only and returns the report without
        recording any decision or writing any hold — used by remediation sweeps to
        preview the plan before applying it.
        """
        effective_policy = policy or job.coherence_policy
        scanner = CoherenceScanner(
            graph=self._graph,
            # WS-23 H1: the scanner re-embeds revealed memory text, so a
            # content-protected scope gets the hermetic transport here too.
            embedding_transport=self.content_embedding_transport(scope_key=scope.key),
            policy=effective_policy,
        )
        report = scanner.scan(scope=scope, artifact_sources=self._artifact_sources, now=now)
        if dry_run:
            return report
        # WS-11: one coherence receipt run per non-dry scan, checkpointed, recording
        # every incident (coherence-spec claim 7 / PATENT_REPLAY_RECEIPTS_SPEC [0021]).
        coherence_run: ReceiptRun | None = None
        if report.incidents:
            epd, mvd = self._run_policy_digests(job, None)
            coherence_run = self._graph.receipts.begin_run(
                run_kind="coherence",
                job_name=job.name,
                scope_key=scope.key,
                effective_policy_digest=epd,
                agent_id=job.agent.agent_id,
                motive_version_digest=mvd,
                graph_state_hash_before=self._graph.graph_state_hash(scope.key),
            )
        for incident in report.incidents:
            escalating_uuid = (
                incident.escalating_directive.relationship_uuid if incident.escalating_directive is not None else None
            )
            governing_uuid = (
                incident.governing_directive.relationship_uuid if incident.governing_directive is not None else None
            )
            self._emit_receipt(
                coherence_run,
                decision_type=ReceiptDecisionType.COHERENCE_INCIDENT_RECORDED,
                decision_reason=(
                    f"{incident.kind.value}:{incident.incident_id}:"
                    f"attribution_confidence={incident.attribution_confidence:.3f}"
                ),
                decision_result="recorded",
                now=now,
                scope_key=scope.key,
                relationship_uuid=escalating_uuid,
                successor_relationship_uuid=governing_uuid,
                relationship_type=incident.kind.value,
            )
            await self._record_decision(
                job=job,
                now=now,
                decision_type=f"coherence_{incident.kind.value}",
                summary=incident.summary,
                subject_id=incident.incident_id,
                subject_name=incident.subject,
                scope=incident.scope,
                details={
                    "incident": incident.model_dump(mode="json"),
                    "attribution_rationale": incident.attribution_rationale,
                    "proposed_repair": incident.proposed_repair,
                    "governing_artifact_class": (
                        incident.governing_directive.artifact_class.value if incident.governing_directive else None
                    ),
                    "governing_artifact_id": (
                        incident.governing_directive.artifact_id if incident.governing_directive else None
                    ),
                    "escalation_cycles": incident.escalation_cycles,
                    "identity_cosine": incident.identity_cosine,
                    "attribution_confidence": incident.attribution_confidence,
                    "attribution_action": (
                        "defer_pending_disambiguating_evidence"
                        if incident.attribution_confidence < effective_policy.min_attribution_confidence
                        else "eligible_for_review"
                    ),
                    "disambiguation_request": (
                        {
                            "incident_id": incident.incident_id,
                            "scope_key": incident.scope.key,
                            "subject": incident.subject,
                            "requested_at": now.isoformat(),
                            "attribution_confidence": incident.attribution_confidence,
                            "minimum_confidence": effective_policy.min_attribution_confidence,
                            "governing_artifact_id": (
                                incident.governing_directive.artifact_id
                                if incident.governing_directive is not None
                                else None
                            ),
                            "required_evidence": [
                                "a runtime trace identifying the instruction actually applied",
                                "the current version digest and update time of each competing artifact",
                                "an explicit operator statement of the intended directive",
                            ],
                            "status": "pending",
                        }
                        if incident.attribution_confidence < effective_policy.min_attribution_confidence
                        else None
                    ),
                    "approved": True,
                },
            )
            if (
                incident.kind == CoherenceIncidentKind.ESCALATION_WINDUP
                and effective_policy.hold_escalating_directives
                and incident.attribution_confidence >= effective_policy.min_attribution_confidence
                and incident.escalating_directive is not None
                and incident.escalating_directive.relationship_uuid is not None
            ):
                self._graph.update_relationship(
                    incident.escalating_directive.relationship_uuid,
                    properties={
                        "coherence_hold": True,
                        "coherence_incident_id": incident.incident_id,
                        "coherence_hold_at": now.isoformat(),
                    },
                )
        if coherence_run is not None:
            self._graph.receipts.checkpoint(
                coherence_run, graph_state_hash_after=self._graph.graph_state_hash(scope.key)
            )
        return report

    async def _run_coherence(self, *, job: DreamJob, now: datetime) -> DreamJobRun:
        run = DreamJobRun(job_name=job.name, job_kind=job.kind)
        if job.scope is None:
            raise ValueError(f"coherence job {job.name!r} requires an explicit scope (set DreamJob.scope)")
        report = await self.scan_coherence(scope=job.scope, job=job, now=now)
        run.processed_scopes = 1
        run.decision_count = report.incident_count
        return run

    async def _run_formation(
        self,
        *,
        job: DreamJob,
        episodes: list[Episode],
        now: datetime,
        receipt_run: ReceiptRun,
        run: DreamJobRun,
    ) -> None:
        """Form memories from *episodes*, accumulating counters into *run*.

        *run* is owned by :meth:`run_job` rather than constructed here so that a
        run interrupted by an embedding outage still reports what it completed
        before stopping — an exception out of this method must not take the
        tally of the already-materialized episodes with it.
        """
        instructions = self._config.instruction_set(job.instruction_set)
        # Base prompt profile for the job (may be overridden per-episode by Motive).
        base_prompt_profile = self._config.prompt_profile_for_job(job)
        created_by = job.agent.created_by(job.kind)
        # WS-2: resolve the effective salience rubric for this job.
        # Job-level rubric takes precedence over instruction-set rubric (for WS-3 Motives).
        base_rubric: SalienceRubric = job.salience_rubric or instructions.salience_rubric
        consumer_key = self._formation_consumer_key(job)
        matching_episodes = [
            episode
            for episode in episodes
            if self.episode_matches_job(
                episode=episode,
                job=job,
                consumer_key=consumer_key,
            )
        ]
        # Atomic claim step: mark the episodes THIS run will extract inside one
        # BEGIN IMMEDIATE transaction (with a processed re-check under the
        # writer lock), so no concurrent run — same process or another process
        # writing the same graph — can extract the same episode for the same
        # formation consumer.  A racing run claims the remainder (possibly
        # nothing); the claim limit preserves this run's ``max_items_per_run``
        # window.  Claims are released in ``run_job``'s ``finally`` (processed
        # markers supersede them for completed episodes) and expire after a
        # staleness window if the process dies first.
        claimed_episode_uuids = self._graph.claim_episodes(
            [episode.uuid for episode in matching_episodes],
            consumer_key=consumer_key,
            run_uuid=receipt_run.run_uuid,
            now=now,
            limit=job.max_items_per_run,
        )
        # WS-24: the scopes this run actually formed into, in first-touch order.
        # Health is a property of a scope's distribution, so it is measured once
        # per scope at the end of the run rather than once per episode.
        formation_scopes: dict[str, MemoryScope] = {}
        run_motive: Motive | None = None
        for episode in matching_episodes:
            if run.processed_episodes >= job.max_items_per_run:
                break
            if episode.uuid not in claimed_episode_uuids:
                continue

            # WS-3: resolve the active Motive for this episode.
            # Precedence: job.motive > episode.metadata["motive"] > None (legacy).
            # When no Motive is active, all behaviour is identical to pre-WS-3.
            motive = self._config.resolve_motive(job=job, episode_metadata=episode.metadata)
            run_motive = motive if motive is not None else run_motive

            # WS-3: resolve the effective prompt profile.
            # Motive prompt_profile/override > job profile/override (via base_prompt_profile).
            effective_prompt_profile = self._effective_prompt_profile(
                base_prompt_profile=base_prompt_profile,
                motive=motive,
            )

            # Compile the resolved policy before any formation decision.  This
            # makes a Motive's goal real extraction input and binds all later
            # candidate/disposition receipts to the exact contract.
            governance = self._effective_governance(motive=motive)
            self._bind_formation_contract(
                receipt_run=receipt_run,
                episode=episode,
                job=job,
                instructions=instructions,
                prompt_profile=effective_prompt_profile,
                motive=motive,
                governance=governance,
            )

            # WS-3: resolve the effective salience rubric.
            # Motive rubric > job rubric > instruction-set rubric > defaults.
            effective_rubric: SalienceRubric = (
                motive.salience_rubric if motive is not None and motive.salience_rubric is not None else None
            ) or base_rubric

            graph_context = self._graph_context_for_episode(episode=episode, policy=job.context_policy)
            decision = await self._decide(
                job=job,
                now=now,
                receipt_run=receipt_run,
                decision_type="formation_episode_selected",
                proposed_action="Process this queued episode for memory formation.",
                subject_id=episode.uuid,
                subject_name=episode.name,
                scope=episode.scope,
                # T1-1: the formation gate is asked "is this episode worth recording?", and
                # must not be handed the scope's existing memories to answer it. Measured
                # against a live gateway (docs/findings/formation-suppression.md): with ONE
                # unresolved fact injected here, a real agent judges the state of the world
                # described in that context rather than the episode, and refuses --
                # "Reject formation episode: deployment infrastructure lacks required
                # durability guarantees". 5/5 reproducible; three benign facts at the same
                # count are approved, so the variable is the CONTENT of this argument.
                #
                # The consequence is that the failure scales with success: incidents,
                # blockers and open requirements are exactly what an engineering scope
                # accumulates, so the more a scope remembers the less able it becomes to
                # remember anything new. Silent, billed, and it recovers only if someone
                # forgets the offending fact.
                #
                # Scoped deliberately to THIS gate. `_consolidation.py:115` still passes
                # context, because reasoning over existing memories is what consolidation
                # IS. `graph_context` is still computed and still reported below as
                # `graph_context_count`, so the decision record keeps saying what the scope
                # held at the time -- the context is withheld from the PROMPT, not from the
                # audit trail.
                context_facts=(),
                details={
                    "episode_source": episode.source.value,
                    "source_description": episode.source_description,
                    "instruction_set": job.instruction_set,
                    "graph_context_count": len(graph_context),
                    "prompt_profile_key": effective_prompt_profile.key,
                    "active_motive": motive.name if motive is not None else None,
                },
            )
            run.decision_count += 1
            if not decision.approved:
                continue

            # WS-11: governance + motive metadata stamped on every formation receipt.
            gov_digest = governance_policy_digest(governance)
            motive_name = motive.name if motive is not None else None
            # Per-memory motive version pinning: the digest of the Motive that
            # actually governs THIS episode (job/episode-metadata resolved), not
            # the run-level job motive — a memory row alone must prove which
            # version of a same-named Motive formed it.
            episode_motive_digest = (
                motive_version_digest(motive, self._resolved_inputs_dict(job)) if motive is not None else None
            )

            # WS-12: resolve content-plane protection for this episode's scope
            # (auto-provisions the envelope-wrapped DEK; fails fast on a shredded scope).
            protection = self._content_protection(scope_key=episode.scope.key, governance=governance)

            # WS-12: sealed evidence — a crypto-shred scope stores its episode body
            # sealed at ingest.  Reveal a copy for extraction/salience while the DEK
            # lives; receipts keep digesting the STORED (sealed) representation via
            # the original episode object.
            extraction_episode = episode
            if is_sealed_content(episode.body):
                body_key = self._graph.get_governance_key(episode.scope.key)
                if body_key is None:
                    raise ContentKeyUnavailableError(
                        f"episode {episode.uuid} belongs to crypto-shredded scope "
                        f"{episode.scope.key}; its sealed evidence is unrecoverable"
                    )
                extraction_episode = episode.model_copy(update={"body": open_content(episode.body, body_key)})

            # WS-7: PII redaction — apply to episode body before extraction when
            # pii_sensitivity == "high".  The original episode row in the graph is
            # immutable; we only redact the copy fed to the extractor.
            # WS-21 T27: the Redactor is built FROM the effective governance policy
            # (per-Motive governance already overrides the config-level policy), so
            # strategy, custom patterns, and the allowlist are all operator levers.
            if governance is not None and governance.pii_sensitivity == PiiSensitivity.HIGH:
                redactor = self._redactor_for_governance(governance)
                matched_patterns = redactor.matched_pattern_names(extraction_episode.body)
                if matched_patterns:
                    redacted_body = redactor.redact_text(extraction_episode.body)
                    extraction_episode = extraction_episode.model_copy(update={"body": redacted_body})
                    # WS-11: episode-body governance redaction receipt ([0021]).
                    # WS-21 T27: the receipt names the strategy and the matched
                    # pattern NAMES — never the matched content.
                    redaction_payload = {
                        "redaction_strategy": governance.redaction_strategy.value,
                        "matched_patterns": list(matched_patterns),
                    }
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.FORMATION_GOVERNANCE_REDACTED,
                        decision_reason="episode_body_pii_redacted",
                        decision_result="transformed",
                        episode=episode,
                        now=now,
                        motive_name=motive_name,
                        governance_policy_digest=gov_digest,
                        redaction_digest_before=hashlib.sha256(episode.body.encode("utf-8")).hexdigest(),
                        redaction_digest_after=hashlib.sha256(redacted_body.encode("utf-8")).hexdigest(),
                        event_payload=json.dumps(redaction_payload, sort_keys=True, separators=(",", ":")),
                        event_payload_digest=payload_digest(redaction_payload),
                    )

            # WS-11: schema-rejection ([0024]).  The extractor validates each
            # candidate INDEPENDENTLY and returns the survivors plus a rejection
            # record per drop, so one non-conforming candidate can no longer
            # discard its siblings or abort the run.  Only a malformed response
            # ENVELOPE (invalid JSON, no memories list, non-object entries) is
            # still fatal — that is a transport/contract failure, not a
            # candidate-level one — and it is receipted here before re-raising.
            # WS-19 T20: a promotion candidate episode carries the source fact
            # as deterministic JSON — parse it with the rule-based transport so
            # promotion NEVER round-trips through an LLM.
            episode_extractor = (
                self._promotion_extractor if episode.metadata.get("promotion") is True else self._extractor
            )
            # WS-24: a promotion episode replays a fact that ALREADY passed the
            # gate when it first formed, re-serialized as deterministic JSON.
            # Re-gating it would quarantine memories on the strength of a field
            # the replay never carried, so promotion is exempt by construction —
            # the gate belongs at the boundary where a model proposes a memory,
            # and promotion is not that boundary.
            episode_actionability = (
                None if episode.metadata.get("promotion") is True else self._effective_actionability(motive=motive)
            )
            try:
                extraction = await episode_extractor.extract(
                    episode=extraction_episode,
                    instructions=instructions,
                    graph_context=graph_context,
                    prompt_profile=effective_prompt_profile,
                    # WS-17 T16b: canonical entity names + kinds for this scope so
                    # the extractor can resolve definite references against the
                    # entities the graph already knows (entity_ref + confidence).
                    entity_inventory=self.entity_inventory_for_scope(episode.scope),
                    # WS-21 T26: the resolved Motive can replace the instruction
                    # set's extraction system prompt; None keeps today's message
                    # byte-for-byte.  Already frozen into this episode's
                    # FormationContract via the full Motive dump above.
                    system_prompt_override=(motive.system_prompt_override if motive is not None else None),
                    actionability=episode_actionability,
                )
            except ValueError as exc:
                envelope_payload = {"violation": "extraction_envelope_invalid", "fatal": True}
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED,
                    decision_reason=str(exc),
                    decision_result="rejected",
                    episode=episode,
                    now=now,
                    motive_name=motive_name,
                    governance_policy_digest=gov_digest,
                    event_payload=json.dumps(envelope_payload, sort_keys=True, separators=(",", ":")),
                    event_payload_digest=payload_digest(envelope_payload),
                )
                raise

            memories = extraction.memories
            # One receipt per rejected candidate, before anything else touches
            # the surviving stream ([0024] "no candidate is silently dropped").
            # The detailed reason may quote model-authored text, so it goes in
            # through the same choke point every other reason does: a
            # content-protected scope withholds it and seals it into the
            # sensitive payload, while the machine violation code — which is
            # content-free by construction — always survives on event_payload.
            for rejection in extraction.rejections:
                rejection_payload: dict[str, object] = {
                    "violation": rejection.violation.value,
                    "field": rejection.field,
                    "candidate_index": rejection.index,
                }
                # Only echo a relationship_type the instruction set itself
                # defines; a hallucinated one is model-authored text and stays
                # out of the plaintext lineage columns.
                rejected_relationship_type = rejection.raw.get("relationship_type")
                if rejected_relationship_type not in instructions.allowed_relationship_types:
                    rejected_relationship_type = None
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED,
                    decision_reason=rejection.reason,
                    decision_result="rejected",
                    episode=episode,
                    now=now,
                    candidate_uuid=uuid4().hex,
                    candidate_digest=rejected_candidate_digest(
                        rejection.raw,
                        episode_uuid=episode.uuid,
                        content_key=protection.key if protection is not None else None,
                    ),
                    motive_name=motive_name,
                    relationship_type=rejected_relationship_type,
                    governance_policy_digest=gov_digest,
                    event_payload=json.dumps(rejection_payload, sort_keys=True, separators=(",", ":")),
                    event_payload_digest=payload_digest(rejection_payload),
                )

            # WS-24: the abstain path.  A candidate the actionability gate
            # refused is well-formed and simply not a memory, so it is FILED,
            # not dropped: retained in the quarantine store, receipted here, and
            # promotable by an operator later.  It never reaches the graph, so
            # it is not context-visible, not retrievable, and makes no budget
            # claim.
            self._file_quarantined_candidates(
                extraction.quarantined,
                episode=episode,
                job=job,
                now=now,
                receipt_run=receipt_run,
                motive_name=motive_name,
                governance_policy_digest=gov_digest,
                protection=protection,
            )
            run.quarantined_candidates += len(extraction.quarantined)
            # The abstain-rate denominator is what the GATE saw: survivors plus
            # quarantines.  Later drops (salience, Motive type filter, the
            # untrusted-directive gate) are different decisions with their own
            # receipts and must not be folded into this one's rate.
            run.admitted_candidates += len(extraction.memories)
            formation_scopes[episode.scope.key] = episode.scope

            # WS-7: Belt-and-suspenders — also redact extracted memory text fields
            # when pii_sensitivity == "high", in case the extractor carried PII through.
            # WS-21 T27: same governance-configured Redactor as the episode-body pass.
            if governance is not None and governance.pii_sensitivity == PiiSensitivity.HIGH:
                redactor = self._redactor_for_governance(governance)
                redacted_memories: list[ExtractedMemory] = []
                for mem in memories:
                    updates: dict[str, str] = {}
                    field_patterns: dict[str, tuple[str, ...]] = {}
                    for field_name in ("subject", "predicate", "object"):
                        field_value = getattr(mem, field_name)
                        matched_patterns = redactor.matched_pattern_names(field_value)
                        if matched_patterns:
                            updates[field_name] = redactor.redact_text(field_value)
                            field_patterns[field_name] = matched_patterns
                    if updates:
                        before = f"{mem.subject} {mem.predicate} {mem.object}"
                        redacted_mem = mem.model_copy(update=updates)
                        after = f"{redacted_mem.subject} {redacted_mem.predicate} {redacted_mem.object}"
                        matched_names = tuple(
                            dict.fromkeys(name for names in field_patterns.values() for name in names)
                        )
                        redaction_payload = {
                            "redaction_strategy": governance.redaction_strategy.value,
                            "matched_patterns": list(matched_names),
                        }
                        self._emit_receipt(
                            receipt_run,
                            decision_type=ReceiptDecisionType.FORMATION_GOVERNANCE_REDACTED,
                            decision_reason="memory_field_pii_redacted",
                            decision_result="transformed",
                            episode=episode,
                            now=now,
                            motive_name=motive_name,
                            relationship_type=mem.relationship_type,
                            governance_policy_digest=gov_digest,
                            redaction_digest_before=hashlib.sha256(before.encode("utf-8")).hexdigest(),
                            redaction_digest_after=hashlib.sha256(after.encode("utf-8")).hexdigest(),
                            event_payload=json.dumps(redaction_payload, sort_keys=True, separators=(",", ":")),
                            event_payload_digest=payload_digest(redaction_payload),
                        )
                        redacted_memories.append(redacted_mem)
                    else:
                        redacted_memories.append(mem)
                memories = redacted_memories

            # WS-2/WS-11: score every candidate (no drops yet), then receipt the full
            # extracted candidate stream BEFORE any gate ([0020], [0024] "no candidate
            # is silently dropped").  Scoring is a no-op when effective_rubric.is_noop.
            scored_memories = self._score_memories(
                memories=memories, episode=extraction_episode, rubric=effective_rubric
            )
            governed_scored_memories: list[ExtractedMemory] = []
            for candidate in scored_memories:
                # WS-17/WS-23: the SAME real-label matcher extraction and
                # materialization use.  Resolving this with the "*" wildcard
                # (the pre-restructure shape of this call) could silently miss
                # an open_predicate instruction with non-"*" endpoints, so this
                # candidate would resolve a DIFFERENT memory_type here than at
                # materialization and later raise two layers from the cause —
                # one of the three disagreement bugs this restructure closes.
                candidate_memory_type = self._resolve_memory_type_value(
                    relationship_type=candidate.relationship_type,
                    instruction_set_name=episode.instruction_set,
                    subject_label=candidate.subject_label,
                    object_label=candidate.object_label,
                )
                if candidate_memory_type is None:
                    # Defense in depth, not a live path: extraction already
                    # resolved a relationship_instruction with a non-None
                    # memory_type using this SAME matcher over these SAME real
                    # labels, so a candidate that reached here should always
                    # resolve.  Per the staged pipeline's invariant that stage 3
                    # can never abort a run, an inconsistency here (a config
                    # changed between extraction and this point, say) marks and
                    # skips this ONE candidate instead of raising.
                    self._quarantine_unresolvable_candidate(
                        candidate,
                        episode=episode,
                        receipt_run=receipt_run,
                        now=now,
                        motive_name=motive_name,
                        governance_policy_digest=gov_digest,
                        protection=protection,
                        reason="memory_type_unresolved_at_formation",
                    )
                    run.decision_count += 1
                    continue
                claim_mode = self._claim_mode_for(memory=candidate, memory_type_value=candidate_memory_type)
                sensitive, encrypted = self._receipt_sensitive_payload(
                    fact=f"{candidate.subject} {candidate.predicate} {candidate.object}",
                    protection=protection,
                )
                candidate_event_uuid = uuid4().hex
                candidate_event_digest = candidate_digest(
                    candidate,
                    memory_type=candidate_memory_type,
                    episode_uuid=episode.uuid,
                    content_key=protection.key if protection is not None else None,
                )
                # Stage 1: store the raw candidate — countable and inspectable —
                # BEFORE any stage-3 governance (salience, Motive type filter,
                # the untrusted/generated-output gates, materialization) runs.
                # Keyed by the SAME digest every later receipt at this
                # candidate's disposition uses, so any later stage can find and
                # transition this exact row with no uuid threading.
                self._store_raw_candidate(
                    candidate,
                    episode=episode,
                    memory_type=candidate_memory_type,
                    digest=candidate_event_digest,
                    instruction_set=episode.instruction_set,
                    motive_name=motive_name,
                    now=now,
                    protection=protection,
                )
                governed_scored_memories.append(candidate)
                # WS-11: a claim_mode the extractor supplied but the candidate's
                # deterministic memory_type forbids was COERCED (not dropped) at
                # validation time.  That is a candidate transformation, so it is
                # receipted here — sharing this candidate's uuid/digest — before
                # the CANDIDATE_EXTRACTED receipt that records the stored form.
                # Without it, a directive downgraded to an assertion would be
                # invisible to the ledger and discoverable only in app logs.
                if candidate.claim_mode_coerced_from is not None:
                    coercion_payload = {
                        "claim_mode_before": candidate.claim_mode_coerced_from.value,
                        "claim_mode_after": claim_mode.value,
                        "memory_type": candidate_memory_type,
                    }
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.CANDIDATE_CLAIM_MODE_COERCED,
                        decision_reason=(
                            f"claim_mode_invalid_for_memory_type:"
                            f"{candidate.claim_mode_coerced_from.value}"
                            f"->{claim_mode.value}"
                        ),
                        decision_result="transformed",
                        episode=episode,
                        now=now,
                        candidate_uuid=candidate_event_uuid,
                        candidate_digest=candidate_event_digest,
                        motive_name=motive_name,
                        memory_type=candidate_memory_type,
                        claim_mode=claim_mode.value,
                        directive_stance=claim_mode_stance(claim_mode).value,
                        relationship_type=candidate.relationship_type,
                        governance_policy_digest=gov_digest,
                        event_payload=json.dumps(coercion_payload, sort_keys=True, separators=(",", ":")),
                        event_payload_digest=payload_digest(coercion_payload),
                    )
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CANDIDATE_EXTRACTED,
                    decision_reason="extracted",
                    decision_result="observed",
                    episode=episode,
                    now=now,
                    candidate_uuid=candidate_event_uuid,
                    candidate_digest=candidate_event_digest,
                    motive_name=motive_name,
                    memory_type=candidate_memory_type,
                    claim_mode=claim_mode.value,
                    directive_stance=claim_mode_stance(claim_mode).value,
                    relationship_type=candidate.relationship_type,
                    salience_score=candidate.salience_score,
                    governance_policy_digest=gov_digest,
                    sensitive_payload=sensitive,
                    sensitive_payload_encrypted=encrypted,
                )

            # WS-2: filter below-salience + cap (each drop receipts CANDIDATE_LOW_SALIENCE).
            memories = self._filter_scored_memories(
                scored=governed_scored_memories,
                episode=episode,
                rubric=effective_rubric,
                job=job,
                now=now,
                receipt_run=receipt_run,
                motive_name=motive_name,
                governance_policy_digest=gov_digest,
                protection=protection,
            )

            # WS-3: apply allowed_memory_types filter.
            # When the Motive specifies a non-empty set of allowed types, drop any
            # extracted memory whose resolved memory_type is not in that set.
            # Drops are recorded as dream decisions (no silent drops, per repo standards).
            if motive is not None and motive.allowed_memory_types:
                memories, type_dropped = self._apply_motive_type_filter(
                    memories=memories,
                    motive=motive,
                    episode=episode,
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    governance_policy_digest=gov_digest,
                    protection=protection,
                )
                run.decision_count += type_dropped

            # WS-7: untrusted directive gate.
            # When an episode is marked trusted=False AND the config requires approval
            # for untrusted directive/requirement writes, gate those memory types through
            # _decide before materialization.  Non-approved memories are dropped.
            if (
                episode.metadata.get("trusted") is False
                and self._config.require_dream_agent_approval_for_untrusted_directives
            ):
                memories, gate_decisions = await self._gate_untrusted_directive_writes(
                    memories=memories,
                    episode=episode,
                    job=job,
                    now=now,
                    receipt_run=receipt_run,
                    motive_name=motive_name,
                    governance_policy_digest=gov_digest,
                    protection=protection,
                )
                run.decision_count += gate_decisions

            memories, output_gated = self._gate_generated_output_authority(
                memories=memories,
                episode=episode,
                receipt_run=receipt_run,
                now=now,
                motive_name=motive_name,
                governance_policy_digest=gov_digest,
                protection=protection,
            )
            run.decision_count += output_gated

            # WS-3: apply Motive dedup_threshold override for this episode's materialization.
            # We temporarily patch the config's dedup policy by passing the override threshold
            # through materialize so the existing _find_reinforce_target path picks
            # it up.  We restore the original threshold after materialization.
            dedup_threshold_override = motive.dedup_threshold if motive is not None else None
            # One transaction per logical operation: the episode's whole
            # write burst (nodes, relationships, receipts, state tuples)
            # commits once.  Extraction and gating (the LLM work) already
            # happened above; the bracket holds no connection across a call.
            with self._graph.transaction():
                created_nodes, created_relationships, reinforced, superseded = self._materialize_episode(
                    episode,
                    memories,
                    created_by=created_by,
                    receipt_run=receipt_run,
                    job=job,
                    now=now,
                    dedup_threshold_override=dedup_threshold_override,
                    governance=governance,
                    motive_name=motive_name,
                    motive_version_digest_value=episode_motive_digest,
                    governance_policy_digest=gov_digest,
                    raw_candidates_stored=True,
                    self_promote_raw_candidates=True,
                )
            # Claim modes make multi-active memory conflicts observable at the
            # write boundary.  Scan immediately after a formation mutation so a
            # correction/directive cannot wait for an unrelated manual coherence
            # job before it is diagnosed.
            if memories and job.coherence_policy.detect_contradictions:
                await self.scan_coherence(scope=episode.scope, job=job, now=now)
            run.processed_episodes += 1
            run.created_nodes += created_nodes
            run.created_relationships += created_relationships
            run.reinforced_relationships += reinforced
            run.superseded_relationships += superseded
            self._graph.mark_episode_processed(
                episode.uuid,
                processed_at=datetime.now(UTC),
                consumer_key=consumer_key,
            )

        # WS-24: measure the type distribution this run left behind, per scope,
        # and receipt every threshold it crossed.  Deliberately AFTER the loop
        # and inside the same receipt run: the numbers describe the graph as it
        # now stands, not a mid-run snapshot.  Blocking is raised by
        # ``run_job`` after the checkpoint, so a blocked run still leaves a
        # complete, verifiable ledger of the work that produced it.
        # Formation never reported this and every run recorded `processed_scopes=0`,
        # including runs that formed memory. It is the field an operator reads to answer
        # "is the Dream Worker doing anything", so a permanent zero is worse than a wrong
        # number: on `latest` it made 258 successful runs look like a dead worker, and the
        # first live end-to-end formation (episodes=1, created=2) still reported scopes=0.
        #
        # `formation_scopes` is already the exact answer -- the scopes this run formed into,
        # first-touch order -- and it is final here, after the per-episode loop. Consolidation
        # increments the same field per scope (`_consolidation.py`) and coherence sets 1, so
        # counting DISTINCT scopes is what makes formation agree with its siblings.
        run.processed_scopes = len(formation_scopes)

        run.health = self._evaluate_formation_health(
            scopes=list(formation_scopes.values()),
            motive=run_motive,
            run=run,
            receipt_run=receipt_run,
            now=now,
        )

    def _evaluate_formation_health(
        self,
        *,
        scopes: list[MemoryScope],
        motive: Motive | None,
        run: DreamJobRun,
        receipt_run: ReceiptRun,
        now: datetime,
    ) -> list[MemoryHealthReport]:
        policy = self._effective_health(motive=motive)
        if not policy.enabled or not scopes:
            return []
        eligible = (
            tuple(member.value for member in motive.allowed_memory_types)
            if motive is not None and motive.allowed_memory_types
            else DEFAULT_ELIGIBLE_TYPES
        )
        reports: list[MemoryHealthReport] = []
        for scope in scopes:
            report = compute_memory_health(
                scope=scope,
                per_type_counts=self._context_visible_type_counts(scope=scope, as_of=now),
                policy=policy,
                evaluated_at=now,
                quarantined_count=run.quarantined_candidates,
                admitted_count=run.admitted_candidates,
                eligible_types=eligible,
                # WS-27 T4: a SEPARATE population (nodes by entity label) from
                # per_type_counts above (relationships by memory_type) — see
                # compute_memory_health's docstring.
                entity_label_counts=self._context_visible_label_counts(scope=scope),
            )
            reports.append(report)
            for trip in report.trips:
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_HEALTH_GATE_TRIPPED,
                    # Bounded machine code: gate, severity, and the observed
                    # value, whitespace-free so it survives a protected scope's
                    # content-free-reason check.
                    decision_reason=(f"health_gate:{trip.gate}:{trip.severity}:observed={trip.observed:.4f}"),
                    decision_result="gated",
                    now=now,
                    scope_key=scope.key,
                    motive_name=motive.name if motive is not None else None,
                    event_payload=json.dumps(report.model_dump(mode="json"), sort_keys=True, separators=(",", ":")),
                    event_payload_digest=payload_digest(report.model_dump(mode="json")),
                )
            if report.trips:
                _log.warning(
                    "formation health gate for scope %s: %s",
                    scope.key,
                    "; ".join(f"{t.gate}={t.observed:.3f} ({t.severity})" for t in report.trips),
                )
        return reports

    def _context_visible_type_counts(self, *, scope: MemoryScope, as_of: datetime) -> dict[str, int]:
        """Context-visible active facts per memory type — the population that
        actually competes for an agent's context window.

        Uses the same counting recipe as ``MemoryEvolutionProof``: ACTIVE, valid
        window open, ``active_in_context`` not False, and never ``MENTIONS``.
        """
        counts: dict[str, int] = {}
        for relationship in self._graph.relationships():
            properties = relationship.properties
            if properties.get("scope_key") != scope.key:
                continue
            if properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            if properties.get("active_in_context") is False:
                continue
            if relationship.type == "MENTIONS":
                continue
            valid_to = relationship.valid_to
            if valid_to is not None and valid_to <= as_of:
                continue
            memory_type = str(properties.get("memory_type") or "unknown")
            counts[memory_type] = counts.get(memory_type, 0) + 1
        return counts

    def _context_visible_label_counts(self, *, scope: MemoryScope) -> dict[str, int]:
        """WS-27 T4: entity-NODE counts by their semantic LABEL for one scope.

        A separate population from :meth:`_context_visible_type_counts`
        (which counts RELATIONSHIPS by ``memory_type``): this counts NODES by
        their first (semantic) label — ``upsert_node`` always stamps
        ``(semantic_label, scope_kind_label)``, e.g. ``("Concept",
        "Tenant")``, so ``labels[0]`` is the ontology label the Concept
        catch-all guardrail measures.  The ``Episode`` utility node type
        (single label, no scope-kind label) carries no ontology-share
        meaning and is excluded.  Unlike relationships, nodes are not
        versioned ACTIVE/SUPERSEDED, so every node in scope counts.
        """
        counts: dict[str, int] = {}
        for node in self._graph.nodes():
            if node.properties.get("scope_key") != scope.key:
                continue
            if not node.labels:
                continue
            label = node.labels[0]
            if label == "Episode":
                continue
            counts[label] = counts.get(label, 0) + 1
        return counts

    def _effective_prompt_profile(
        self,
        *,
        base_prompt_profile: object,
        motive: Motive | None,
    ) -> object:
        """WS-3: Resolve the effective prompt profile for a (job, motive) pair.

        Motive prompt_profile/override > job profile/override (base_prompt_profile
        already has the job override applied via with_override()).

        When no Motive is active or the Motive specifies no prompt, returns
        base_prompt_profile unchanged (no-op, legacy behaviour).
        """
        if motive is None:
            return base_prompt_profile
        # If the Motive pins a prompt profile, resolve it from the config.
        if motive.prompt_profile is not None:
            version = motive.prompt_profile_version or "v1"
            profile = self._config.prompt_profile(motive.prompt_profile, version)
        else:
            # No profile override — start from the base profile.
            profile = base_prompt_profile
        # Apply the Motive's prompt_override on top (if any).
        if motive.prompt_override is not None:
            profile = profile.with_override(motive.prompt_override)
        return profile.with_motive_goal(motive.goal)

    def _effective_actionability(self, *, motive: Motive | None) -> ActionabilityPolicy:
        """WS-24: resolve the actionability gate for one episode.

        Motive.actionability > DreamConfig.actionability — the same precedence
        every other formation lever uses, so a Motive whose whole job is
        exhaustive capture can loosen the gate without loosening it tenant-wide.
        """
        if motive is not None and motive.actionability is not None:
            return motive.actionability
        return self._config.actionability

    def _effective_health(self, *, motive: Motive | None) -> MemoryHealthPolicy:
        """WS-24: resolve the distribution health policy for one run."""
        if motive is not None and motive.health is not None:
            return motive.health
        return self._config.health

    @staticmethod
    def _redactor_for_governance(governance: GovernancePolicy) -> Redactor:
        """WS-21 T27: build the Redactor from the EFFECTIVE governance policy.

        Strategy, operator-supplied custom patterns, and the allowlist all come
        from the policy in force — the per-Motive governance override (when set)
        therefore carries its own redaction configuration.  Defaults reproduce
        the legacy builtin-patterns-only REDACT behaviour byte-for-byte.
        """
        return Redactor(
            governance.redaction_strategy,
            custom_patterns=governance.custom_pii_patterns,
            allowlist=governance.redaction_allowlist,
        )

    async def _gate_untrusted_directive_writes(
        self,
        *,
        memories: list[ExtractedMemory],
        episode: Episode,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        motive_name: str | None = None,
        governance_policy_digest: str | None = None,
        protection: _ContentProtection | None = None,
    ) -> tuple[list[ExtractedMemory], int]:
        """WS-7: Gate directive/requirement memories from untrusted episodes.

        When require_dream_agent_approval_for_untrusted_directives is True and the
        episode metadata["trusted"] == False, every DIRECTIVE or REQUIREMENT memory
        must be approved by the dream agent before it can be materialized.

        Non-approved directive/requirement memories are dropped and logged as
        'formation_untrusted_write_gated' decisions.  All other memory types pass
        through unaffected (this gate only targets high-risk write types).

        Returns (survivors, gate_decision_count).
        """
        _GATED_TYPES = {MemoryType.DIRECTIVE.value, MemoryType.REQUIREMENT.value}
        survivors: list[ExtractedMemory] = []
        gate_count = 0
        for memory in memories:
            # Resolve memory_type for this memory (real labels, see
            # _resolve_memory_type_value).
            memory_type_value = self._resolve_memory_type_value(
                relationship_type=memory.relationship_type,
                instruction_set_name=episode.instruction_set,
                subject_label=memory.subject_label,
                object_label=memory.object_label,
            )
            if memory_type_value not in _GATED_TYPES:
                survivors.append(memory)
                continue
            # Ask the dream agent to approve this directive/requirement write.
            fact = f"{memory.subject} {memory.predicate} {memory.object}"
            # WS-12: the persisted decision record must not carry plaintext content
            # for a crypto-shred scope — seal the fact; keep proposed_action content-free.
            recorded_fact = protection.seal(fact) if protection is not None else fact
            proposed_action = (
                f"Approve materialization of an untrusted-source {memory_type_value!r} memory"
                + (f": {fact!r}. " if protection is None else " (content sealed). ")
                + "Episode was marked trusted=False at ingestion."
            )
            decision = await self._decide(
                job=job,
                now=now,
                receipt_run=receipt_run,
                decision_type="formation_untrusted_write_gated",
                proposed_action=proposed_action,
                subject_id=episode.uuid,
                subject_name=episode.name,
                scope=episode.scope,
                context_facts=(fact,),
                details={
                    "memory_type": memory_type_value,
                    "relationship_type": memory.relationship_type,
                    "fact": recorded_fact,
                    "episode_trusted": False,
                },
            )
            gate_count += 1
            if decision.approved:
                survivors.append(memory)
            else:
                claim_mode = self._claim_mode_for(memory=memory, memory_type_value=memory_type_value)
                digest = candidate_digest(
                    memory,
                    memory_type=memory_type_value,
                    episode_uuid=episode.uuid,
                    content_key=protection.key if protection is not None else None,
                )
                # WS-11: gated (dropped) untrusted directive/requirement write ([0021]).
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_UNTRUSTED_DIRECTIVE_GATED,
                    decision_reason="untrusted_directive_write_not_approved",
                    decision_result="gated",
                    episode=episode,
                    now=now,
                    candidate_digest=digest,
                    motive_name=motive_name,
                    memory_type=memory_type_value,
                    claim_mode=claim_mode.value,
                    directive_stance=claim_mode_stance(claim_mode).value,
                    relationship_type=memory.relationship_type,
                    governance_policy_digest=governance_policy_digest,
                )
                self._mark_raw_candidate_quarantined(
                    digest=digest,
                    now=now,
                    reason="untrusted_directive_write_not_approved",
                    resolution_note="untrusted_directive_write_not_approved",
                )
        return survivors, gate_count

    def _gate_generated_output_authority(
        self,
        *,
        memories: list[ExtractedMemory],
        episode: Episode,
        receipt_run: ReceiptRun,
        now: datetime,
        motive_name: str | None,
        governance_policy_digest: str | None,
        protection: _ContentProtection | None,
    ) -> tuple[list[ExtractedMemory], int]:
        """Keep generated outputs/tool traces in the evidence lane.

        Such records may form descriptive state/incident memories, but an
        extraction or consolidation pass cannot launder them into a requirement,
        directive, or correction with execution authority.
        """
        if episode.metadata.get("artifact_class") != "generated_output":
            return memories, 0
        survivors: list[ExtractedMemory] = []
        gated = 0
        for memory in memories:
            memory_type = self._resolve_memory_type_value(
                relationship_type=memory.relationship_type,
                instruction_set_name=episode.instruction_set,
                subject_label=memory.subject_label,
                object_label=memory.object_label,
            )
            claim_mode = self._claim_mode_for(memory=memory, memory_type_value=memory_type)
            if claim_mode not in {
                ClaimMode.REQUIREMENT,
                ClaimMode.DIRECTIVE,
                ClaimMode.CORRECTION,
            }:
                survivors.append(memory)
                continue
            digest = candidate_digest(
                memory,
                memory_type=memory_type,
                episode_uuid=episode.uuid,
                content_key=protection.key if protection is not None else None,
            )
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.FORMATION_NON_NORMATIVE_EVIDENCE_GATED,
                decision_reason="generated_output_cannot_gain_normative_authority",
                decision_result="gated",
                episode=episode,
                now=now,
                candidate_digest=digest,
                motive_name=motive_name,
                memory_type=memory_type,
                claim_mode=claim_mode.value,
                directive_stance=claim_mode_stance(claim_mode).value,
                relationship_type=memory.relationship_type,
                governance_policy_digest=governance_policy_digest,
            )
            self._mark_raw_candidate_quarantined(
                digest=digest,
                now=now,
                reason="generated_output_cannot_gain_normative_authority",
                resolution_note="generated_output_cannot_gain_normative_authority",
            )
            gated += 1
        return survivors, gated

    def _apply_motive_type_filter(
        self,
        *,
        memories: list[ExtractedMemory],
        motive: Motive,
        episode: Episode,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        governance_policy_digest: str | None = None,
        protection: _ContentProtection | None = None,
    ) -> tuple[list[ExtractedMemory], int]:
        """WS-3: Drop extracted memories whose resolved memory_type is not allowed by the Motive.

        When motive.allowed_memory_types is empty this is a no-op (allow all types).
        Every dropped memory is recorded as a 'formation_motive_type_filtered' dream
        decision — no silent drops, per repo standards — and (WS-11) a
        FORMATION_MOTIVE_TYPE_FILTERED receipt ([0021]).

        Returns (survivors, decision_count_added).
        """
        if not motive.allowed_memory_types:
            return memories, 0

        allowed_values = {t.value for t in motive.allowed_memory_types}
        survivors: list[ExtractedMemory] = []
        dropped: list[ExtractedMemory] = []

        for memory in memories:
            # Resolve memory_type for this memory using the instruction set
            # (real labels — see _resolve_memory_type_value).
            memory_type_value = self._resolve_memory_type_value(
                relationship_type=memory.relationship_type,
                instruction_set_name=episode.instruction_set,
                subject_label=memory.subject_label,
                object_label=memory.object_label,
            )
            if memory_type_value is None or memory_type_value in allowed_values:
                survivors.append(memory)
            else:
                dropped.append(memory)
                claim_mode = self._claim_mode_for(memory=memory, memory_type_value=memory_type_value)
                digest = candidate_digest(
                    memory,
                    memory_type=memory_type_value,
                    episode_uuid=episode.uuid,
                    content_key=protection.key if protection is not None else None,
                )
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_MOTIVE_TYPE_FILTERED,
                    decision_reason="memory_type_not_allowed_by_motive",
                    decision_result="gated",
                    episode=episode,
                    now=now,
                    candidate_digest=digest,
                    motive_name=motive.name,
                    memory_type=memory_type_value,
                    claim_mode=claim_mode.value,
                    directive_stance=claim_mode_stance(claim_mode).value,
                    relationship_type=memory.relationship_type,
                    governance_policy_digest=governance_policy_digest,
                )
                self._mark_raw_candidate_quarantined(
                    digest=digest,
                    now=now,
                    reason="memory_type_not_allowed_by_motive",
                    resolution_note=f"memory_type_not_allowed_by_motive:{motive.name}",
                )

        if dropped:
            # WS-12: subject names are content — sealed in the persisted decision
            # record for a crypto-shred scope.
            dropped_subjects = [protection.seal(m.subject) if protection is not None else m.subject for m in dropped]
            _log.info(
                "WS-3 motive type filter [%s]: dropped %d memories with disallowed types in episode %s (%s): %s",
                motive.name,
                len(dropped),
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
                    decision_type="formation_motive_type_filtered",
                    summary=(
                        f"{job.agent.name} dropped {len(dropped)} memories with disallowed "
                        f"memory_types under motive {motive.name!r} in episode {episode.name!r}. "
                        f"Allowed: {sorted(allowed_values)}."
                    ),
                    subject_id=episode.uuid,
                    subject_name=episode.name,
                    scope=episode.scope,
                    prompt_profile=job.prompt_profile,
                    prompt_profile_version=job.prompt_profile_version,
                    details={
                        "motive_name": motive.name,
                        "allowed_memory_types": sorted(allowed_values),
                        "dropped_count": len(dropped),
                        "dropped_subjects": dropped_subjects,
                        "approved": True,
                    },
                )
            )
            return survivors, 1
        return survivors, 0
