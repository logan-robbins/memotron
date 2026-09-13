from __future__ import annotations

import json
from datetime import datetime

from memotron.agents import DreamAgentTransport, LocalDreamAgentTransport
from memotron.attestations import FormationAttestationUnavailableError
from memotron.coherence import ArtifactSource
from memotron.config import (
    DreamAgentConfig,
    DreamConfig,
    DreamJob,
    EffectiveMemoryPolicy,
    FormationContract,
    GovernancePolicy,
    Motive,
    default_claim_mode_for_memory_type,
    validate_claim_mode_for_memory_type,
)
from memotron.crypto import ContentKeyUnavailableError, is_sealed_content, open_content
from memotron.dreaming._candidates import RawCandidateMixin
from memotron.dreaming._common import (
    _DREAM_AGENT_FALLBACK_TRANSPORT as _DREAM_AGENT_FALLBACK_TRANSPORT,
)
from memotron.dreaming._common import (
    _FAIL_CLOSED_DECISION_TYPES as _FAIL_CLOSED_DECISION_TYPES,
)
from memotron.dreaming._common import (
    _ContentProtection as _ContentProtection,
)
from memotron.dreaming._common import (
    _log as _log,
)
from memotron.dreaming._common import (
    _strip_negation_markers as _strip_negation_markers,
)
from memotron.dreaming._consolidation import ConsolidationMixin
from memotron.dreaming._contradiction import ContradictionMixin
from memotron.dreaming._decisions import DecisionMixin
from memotron.dreaming._dedup import DeduplicationMixin
from memotron.dreaming._entities import EntityResolutionMixin
from memotron.dreaming._formation import FormationMixin
from memotron.dreaming._identity import (
    DualRepresentationInvariantError as DualRepresentationInvariantError,
)
from memotron.dreaming._identity import (
    RegovernResult as RegovernResult,
)
from memotron.dreaming._identity import (
    _EntityResolution as _EntityResolution,
)
from memotron.dreaming._identity import (
    _TruthGateDecision as _TruthGateDecision,
)
from memotron.dreaming._identity import (
    assert_dual_representation_invariant as assert_dual_representation_invariant,
)
from memotron.dreaming._identity import (
    composed_entity_link_score as composed_entity_link_score,
)
from memotron.dreaming._identity import (
    node_identity_key as node_identity_key,
)
from memotron.dreaming._identity import (
    truth_identity as truth_identity,
)
from memotron.dreaming._lifecycle import RelationshipLifecycleMixin
from memotron.dreaming._materialization import MaterializationMixin
from memotron.dreaming._predicates import PredicateCanonicalizationMixin
from memotron.dreaming._rollup_text import (
    _ROLLUP_LABEL_KEYWORD_COUNT as _ROLLUP_LABEL_KEYWORD_COUNT,
)
from memotron.dreaming._rollup_text import (
    _SENTENCE_SPLIT_PATTERN as _SENTENCE_SPLIT_PATTERN,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_LABEL_MAX_CHARS as ROLLUP_LABEL_MAX_CHARS,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_SUMMARY_MAX_CHARS as ROLLUP_SUMMARY_MAX_CHARS,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_SUMMARY_MAX_SENTENCES as ROLLUP_SUMMARY_MAX_SENTENCES,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_SYNTHESIS_SYSTEM_PROMPT as ROLLUP_SYNTHESIS_SYSTEM_PROMPT,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE as ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED as ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_TEXT_SOURCE_LLM as ROLLUP_TEXT_SOURCE_LLM,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_TEXT_SOURCE_NO_TRANSPORT as ROLLUP_TEXT_SOURCE_NO_TRANSPORT,
)
from memotron.dreaming._rollup_text import (
    ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR as ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR,
)
from memotron.dreaming._rollup_text import (
    _ResolvedRollupText as _ResolvedRollupText,
)
from memotron.dreaming._rollup_text import (
    _rollup_label_clip as _rollup_label_clip,
)
from memotron.dreaming._rollup_text import (
    _rollup_label_common_prefix as _rollup_label_common_prefix,
)
from memotron.dreaming._rollup_text import (
    _rollup_label_fit as _rollup_label_fit,
)
from memotron.dreaming._rollup_text import (
    _rollup_label_keywords as _rollup_label_keywords,
)
from memotron.dreaming._rollup_text import (
    _rollup_label_shared_value as _rollup_label_shared_value,
)
from memotron.dreaming._rollup_text import (
    render_rollup_synthesis_prompt as render_rollup_synthesis_prompt,
)
from memotron.dreaming._rollup_text import (
    rollup_summary_entailed as rollup_summary_entailed,
)
from memotron.dreaming._rollup_text import (
    rollup_synthesis_prompt_digest as rollup_synthesis_prompt_digest,
)
from memotron.dreaming._rollups import RollupMixin
from memotron.dreaming._text import (
    _CONTENT_TOKEN_PATTERN as _CONTENT_TOKEN_PATTERN,
)
from memotron.dreaming._text import (
    _IDENTIFIER_TOKEN_PATTERN as _IDENTIFIER_TOKEN_PATTERN,
)
from memotron.dreaming._text import (
    _ROLLUP_LABEL_STOPWORDS as _ROLLUP_LABEL_STOPWORDS,
)
from memotron.dreaming._text import (
    _VERBATIM_TOKEN_PATTERN as _VERBATIM_TOKEN_PATTERN,
)
from memotron.dreaming._text import (
    CONTENT_STOPWORDS as CONTENT_STOPWORDS,
)
from memotron.dreaming._text import (
    _is_identifier_token as _is_identifier_token,
)
from memotron.dreaming._text import (
    _verbatim_guarded_tokens as _verbatim_guarded_tokens,
)
from memotron.dreaming._text import (
    content_tokens as content_tokens,
)
from memotron.dreaming._text import (
    identifier_tokens as identifier_tokens,
)
from memotron.dreaming._text import (
    identifier_tokens_conflict as identifier_tokens_conflict,
)
from memotron.embedding import EmbeddingTransport, LocalEmbeddingTransport
from memotron.extraction import InstructionalExtractor, RuleBasedExtractionTransport
from memotron.gateway import GatewayRequestError
from memotron.models import (
    ClaimMode,
    DreamJobKind,
    DreamJobRun,
    Episode,
    ExtractedMemory,
    GraphRelationship,
    MemoryAuthority,
    MemoryHealthGateError,
    MemoryScope,
    MemoryType,
    QuarantineStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    config_effective_policy_digest,
    effective_policy_digest,
    evidence_digest,
    governance_policy_digest,
    motive_version_digest,
    payload_digest,
)
from memotron.retrieval import (
    semantic_polarity as _semantic_polarity_fn,
)
from memotron.storage import StorageBackend
from memotron.storage._shared._governance import FormationSignerState
from memotron.synthesis import SynthesisTransport

"""The deterministic decision used when the configured dream-agent transport fails.

This is the SAME transport the engine already runs when no dream-agent
transport is configured, so a transport outage degrades the run to the
documented default rather than to some third behaviour."""

"""Decision types whose fallback is REJECT, not the deterministic approval.

The engine consults the dream agent at exactly six decision points, and a
missing answer at each one is resolved by ONE rule:

    **a fallback may approve CREATION; it may never approve REMOVAL.**

That is the operative reading of "an outage must not become a governance
bypass".  Approving a write the model never saw costs an additive, receipted,
fully reversible row.  Approving a *removal* the model never saw costs a fact
that is no longer in the graph's active plane — and unlike the write, nothing
in a later healthy run brings it back on its own.

**Fail closed (this set).**

* ``formation_untrusted_write_gated`` — the gate exists only because
  ``require_dream_agent_approval_for_untrusted_directives`` is on: the operator
  has stated that an untrusted-source directive/requirement may not become
  memory unless an agent approves it.  Answering "approved" because the agent
  was unreachable would turn a model outage into a governance bypass.  (Note
  this is the one member that fails closed against a *write* — because the
  approval requirement itself, not the mutation, is what the operator bought.)
* ``pruning_relationship_pruned`` — approval archives/expires a fact out of the
  active plane.
* ``pruning_context_budget_exceeded`` — approval prunes the lowest-value facts
  to satisfy a soft cap.
* ``pruning_single_active_repair`` — approval closes an older active row's
  validity interval in favour of the deterministic winner.

Every one of those is idempotent and re-evaluated on the next pruning cadence,
so failing closed costs one cycle of delay: the row survives a little longer
and is re-proposed the moment the transport recovers.  Failing open costs a
removal that nobody approved.  The asymmetry is the whole argument.

**Fail open (the deterministic approval).**

* ``formation_episode_selected`` — admitting an immutable episode to formation.
  Purely additive, and approving it decides nothing about what may be written:
  redaction, salience, dedup, Motive type filters, the supersession authority
  gate, and the fail-closed untrusted-directive gate all still run downstream
  on every candidate the extractor produces.  Failing closed here would
  silently stall the system's core function for the whole duration of an
  outage.
* ``consolidation_scope_selected`` — synthesizing rollups and demoting their
  members.  Demotion is ``active_in_context=False``: the member leaves default
  profile context but stays searchable, stays evidence, is re-promoted if the
  rollup is superseded, and is never deleted.  Additive and reversible.

The split is by what an approval AUTHORIZES, not by why the answer went
missing, so a decision-contract failure (the model answered out of contract)
and a transport failure (the model never answered) resolve identically here.
They are still receipted distinctly — see
:meth:`DreamEngine._fallback_decision` — because one is a prompt/model problem
and the other is an infrastructure problem."""


"""Token split for identifier detection: runs of word characters keeping the
``_``/``-`` joiners intact so a coded ID (``kb_ds_source_..._wdw_en_us_v1``)
stays ONE token."""


"""Exact stopword set of the deterministic rollup-label synthesizer.

Byte-for-byte the historical in-method set — changing it changes every
deterministic rollup label, so it is frozen here and only EXTENDED (never
edited) by the entailment gate's superset below."""

"""Structural glue words ignored by content-token matching (WS-18 gate +
WS-15 transcript-citation containment).  Everything else is a content token
that must be grounded in source text."""

"""Content tokenization: casefolded alphanumeric runs — punctuation (including
``_``/``-`` joiners) is a separator, so coverage checks compare word material
only.  Identifier fidelity is enforced separately and VERBATIM."""

"""Raw-token split for the verbatim pass: keeps ``_``/``-``/``.``/``/`` joiners
so coded identifiers, versions, and decimals stay whole."""


"""Hard cap on a deterministic rollup label (it becomes a node key and the
ROLLUP row's object)."""

"""How many shared keywords the last-resort label lists.  They are rendered as
a comma-separated parenthetical, never as a pseudo-sentence: a keyword list
cannot be misread as a claim about the members."""


"""``rollup_text_source``: a gate-passing LLM summary IS the rollup text."""

"""``rollup_text_source``: the model answered and the deterministic contract
refused the answer (unentailed token, cross-member recombination, non-verbatim
identifier, over-length, or a malformed JSON envelope).  The text on the row is
the deterministic structural label, NOT the model's words."""

"""``rollup_text_source``: the synthesis call itself failed, so there was never
an answer to refuse."""

"""``rollup_text_source``: a sealed scope's content is never sent to a model, so
synthesis was skipped by governance rather than attempted."""

"""``rollup_text_source``: no ``SynthesisTransport`` is configured — the hermetic
default."""


"""Hard system prompt of the WS-18 rollup synthesizer.  Its constraints are
re-enforced deterministically by :func:`rollup_summary_entailed` — the prompt
asks, the gate verifies.

The per-sentence single-member rule is stated because the gate ENFORCES it
(WS-23 M3).  WS-23 tightened coverage from the member token union to one
member per sentence without telling the model, so a model doing the obvious
thing — writing one blended sentence over the cluster — was rejected every
time: the call happened, cost tokens, and could never land.  The prompt and
the gate must describe the same contract, or the LLM path is inert in a second
way (asked, and structurally guaranteed to fail)."""


class DreamEngine(
    FormationMixin,
    ConsolidationMixin,
    DeduplicationMixin,
    RollupMixin,
    DecisionMixin,
    PredicateCanonicalizationMixin,
    EntityResolutionMixin,
    MaterializationMixin,
    ContradictionMixin,
    RelationshipLifecycleMixin,
    RawCandidateMixin,
):
    def __init__(
        self,
        *,
        config: DreamConfig,
        graph: StorageBackend,
        extractor: InstructionalExtractor,
        agent_transport: DreamAgentTransport | None = None,
        embedding_transport: EmbeddingTransport | None = None,
        rollup_synthesis_transport: SynthesisTransport | None = None,
        artifact_sources: list[ArtifactSource] | None = None,
        epoch_override: str | None = None,
        redream_tier: str | None = None,
        redream_tier_reason: str | None = None,
    ) -> None:
        self._config = config
        self._graph = graph
        self._extractor = extractor
        # WS-26 T8: when this engine is the throwaway one a branched re-dream
        # builds (epochs.py), the tier it recomputes at (A/B/C) and why — stamped
        # onto every run checkpoint this engine writes, so the WS-11 receipt
        # names the tier.  None on the live production engine.
        self._redream_tier = redream_tier
        self._redream_tier_reason = redream_tier_reason
        # WS-26 T2/T4: when set, every version this engine materializes is
        # stamped into *this* epoch instead of the scope's current HEAD.  Set
        # only on a dedicated engine instance built for a branched re-dream
        # (see epochs.py) — the live production engine always leaves this
        # None, so a scope's writes land in whatever epoch is currently HEAD
        # (lazily created on first write via ``ensure_root_epoch``) exactly as
        # before epochs existed.
        self._epoch_override = epoch_override
        # WS-19 T20: promotion candidates are re-materialized VERBATIM from an
        # existing governed row, so their extraction is always the
        # deterministic JSON path (zero LLM) regardless of which transport the
        # tenant configured — the LLM must never get a chance to paraphrase a
        # fact that is being promoted, only the project Motive gates apply.
        self._promotion_extractor = InstructionalExtractor(transport=RuleBasedExtractionTransport())
        self._agent_transport = agent_transport or LocalDreamAgentTransport()
        self._embedding_transport: EmbeddingTransport = embedding_transport or LocalEmbeddingTransport()
        # WS-23 H1: hermetic stand-in used for content-protected scopes when the
        # configured transport is a network endpoint, plus the once-per-run
        # receipt marker for that downgrade.
        self._local_embedding_fallback: LocalEmbeddingTransport | None = None
        self._embedding_downgrade_receipted: set[tuple[str, str]] = set()
        # WS-18 T19: optional LLM summarizer for derived rollups.  Configuring a
        # transport IS the opt-in (WS-24) — see _synthesis_model_identifier.
        # None keeps the deterministic structural label.
        self._rollup_synthesis_transport = rollup_synthesis_transport
        # WS-10: non-memory persistent artifacts (skills, files) visible to the
        # cross-artifact coherence cycle.  Empty by default — coherence is opt-in.
        self._artifact_sources: list[ArtifactSource] = list(artifact_sources or [])
        # WS-11: provenance identifiers stamped on every receipt ([0018]-[0020]).
        # Model id lives only on LLM transports (RuleBased has none, so None).
        transport = getattr(self._extractor, "_transport", None)
        self._receipt_model_identifier: str | None = getattr(transport, "model", None)
        self._receipt_extractor_identifier: str | None = type(transport).__name__ if transport is not None else None
        # WS-17 T18: the transport's protocol-required vector-space identifier is
        # the single provenance string for receipts, contracts, and the
        # embedding_identifier stamp written next to every stored vector.
        transport_identifier = getattr(self._embedding_transport, "identifier", None)
        if not isinstance(transport_identifier, str) or not transport_identifier.strip():
            raise ValueError(
                "embedding transport must define a non-blank 'identifier' naming its "
                "vector space (EmbeddingTransport protocol, WS-17 T18)"
            )
        self._receipt_embedding_identifier: str | None = transport_identifier

    def register_artifact_source(self, source: ArtifactSource) -> None:
        """WS-10: register a provider of non-memory persistent artifacts."""
        self._artifact_sources.append(source)

    def due_jobs(self, *, now: datetime) -> list[DreamJob]:
        due: list[DreamJob] = []
        for job in self._config.jobs:
            last_run = self._graph.get_job_last_run(job.name)
            if last_run is None or (now - last_run).total_seconds() >= job.cadence_seconds:
                due.append(job)
        return due

    async def run_job(
        self,
        *,
        job: DreamJob,
        episodes: list[Episode],
        now: datetime,
        effective_policy: EffectiveMemoryPolicy | None = None,
    ) -> DreamJobRun:
        """WS-11: run one dream job under a single hash-chained receipt run.

        For FORMATION/CONSOLIDATION/PRUNING a single :class:`ReceiptRun` is begun
        at the top, threaded through every decision point, and checkpointed at the
        end (PATENT_REPLAY_RECEIPTS_SPEC [0022], [0024]).  Empty runs — and aborted
        runs (schema rejection re-raises before the checkpoint) — are intentionally
        left uncheckpointed, so byte replay of such a run fails closed, which is
        correct.  COHERENCE receipts its incidents under a per-scan coherence run
        managed inside :meth:`scan_coherence`.

        **Formation survives an embedding OUTAGE, and says so.** Embeddings feed
        dedup, clustering, and salience relevance — never truth — so losing the
        embedding endpoint mid-run must not also destroy the extraction work
        earlier episodes already paid the gateway for.  When the embedding
        transport fails terminally with a *transient* classification (after its
        own bounded retry), the run stops at that episode, receipts
        ``EMBEDDING_TRANSPORT_UNAVAILABLE``, and checkpoints everything it did
        complete.  The failing episode is never marked processed, so it stays in
        the durable queue and the next cycle re-forms it in full: nothing is
        written half-embedded, and no vector is silently substituted from
        another transport.  Stopping (rather than pushing on through the rest of
        the queue) is deliberate — during an outage every remaining episode
        would only mount its own retry storm.  A DETERMINISTIC embedding failure
        (401/403, malformed request) is a misconfiguration, not an outage: it
        re-raises uncheckpointed exactly as before, because retrying it later
        would fail identically and quietly.

        **Concurrent runs never double-extract or double-synthesize.** Before
        formation extracts anything, the run atomically CLAIMS the episodes it
        will process (``PropertyGraphStore.claim_episodes``, ``BEGIN
        IMMEDIATE``), so two runs racing the same queue — a double-clicked
        admin button, the local platform's maintenance loop, an MCP
        ``memory_refresh``, in-process or cross-process — partition the pending
        episodes instead of both extracting them.  Consolidation claims each
        scope's rollup pass and pruning claims its run scope the same way,
        because rollup synthesis and temporary-predecessor restoration are
        non-idempotent creations.  A second concurrent run claims whatever
        remains (possibly nothing) and completes cleanly; claims are released
        in this method's ``finally`` and expire after
        ``DREAM_CLAIM_STALE_SECONDS`` so a crashed run cannot wedge the queue.
        Claims are operational bookkeeping — outside the receipt ledger and
        outside ``graph_state_hash`` — exactly like processed-episode markers.
        """
        if job.kind == DreamJobKind.COHERENCE:
            run = await self._run_coherence(job=job, now=now)
            self._graph.set_job_last_run(job.name, now)
            return run
        if job.kind not in (DreamJobKind.FORMATION, DreamJobKind.CONSOLIDATION, DreamJobKind.PRUNING):
            raise ValueError(f"unsupported dream job kind: {job.kind}")

        if job.kind == DreamJobKind.FORMATION:
            # BEFORE anything is claimed or written. `mark_episode_processed` fires inside the
            # per-episode loop, so an episode formed without an attestation is never re-formed
            # and its governing contract is unprovable permanently -- there is no later repair,
            # which is why this refuses up front rather than degrading per episode.
            self._preflight_formation_attestation(job=job)

        receipt_run = self._begin_receipt_run(job=job, run_kind=job.kind.value, effective_policy=effective_policy)
        try:
            if job.kind == DreamJobKind.FORMATION:
                run = DreamJobRun(job_name=job.name, job_kind=job.kind)
                try:
                    await self._run_formation(job=job, episodes=episodes, now=now, receipt_run=receipt_run, run=run)
                except GatewayRequestError as exc:
                    if not exc.retryable:
                        # Deterministic refusal — a credential or request the
                        # operator must fix.  Fail loudly and uncheckpointed.
                        raise
                    self._receipt_embedding_outage(receipt_run=receipt_run, job=job, error=exc, now=now)
            elif job.kind == DreamJobKind.CONSOLIDATION:
                run = await self._run_consolidation(job=job, now=now, receipt_run=receipt_run)
            else:
                run = await self._run_pruning(job=job, now=now, receipt_run=receipt_run)
            run.run_uuid = receipt_run.run_uuid
            # The run's terminal bracket: the checkpoint and the last-run
            # marker commit together, so a crash between them cannot leave a
            # checkpointed run that still looks due.
            with self._graph.transaction():
                self._checkpoint_receipt_run(receipt_run=receipt_run, job=job)
                self._graph.set_job_last_run(job.name, now)
            # WS-24: a blocking health gate fails the run LOUDLY — but only
            # after the checkpoint, so the ledger that proves what happened is
            # complete and replayable before the exception leaves.  A silent
            # degenerate graph is the outcome this refuses to produce; a
            # half-written receipt chain is not an acceptable price for it.
            blocking = next((report for report in run.health if report.blocked), None)
            if blocking is not None:
                policy = self._effective_health(motive=self._config.resolve_motive(job=job, episode_metadata={}))
                if policy.raise_on_block:
                    raise MemoryHealthGateError(blocking)
        finally:
            # Claims are operational bookkeeping, not memory decisions: release
            # this run's dream-work claims on every exit path (processed
            # markers already supersede the claims of completed episodes).  A
            # SIGKILLed process never reaches this, which is what the claim
            # staleness window exists for.
            self._graph.release_dream_claims(receipt_run.run_uuid)
        return run

    async def regovern_scope(
        self,
        *,
        scope: MemoryScope,
        now: datetime,
        resolved_by: str = "regovern",
        limit: int | None = None,
    ) -> RegovernResult:
        """Re-run stage-3 GOVERNANCE over one scope's already-stored raw graph —
        no transport call, no LLM, no re-extraction.

        This is the pipeline restructure's headline capability: every candidate
        this scope ever extracted is sitting in the raw/quarantine store
        (``PENDING`` if never governed, ``QUARANTINED`` if a prior pass
        rejected it).  Re-govern reconstructs each one's original raw JSON
        payload and episode, re-validates it under the CURRENT
        ``DreamInstructionSet``/``ActionabilityPolicy``/``Motive`` (a widened
        property whitelist, a lowered ``min_confidence``, a relaxed
        actionability gate — whatever changed), and either materializes it
        (``PROMOTED``, via the same :meth:`_materialize_episode` formation
        uses) or leaves/returns it to ``QUARANTINED`` with the fresh reason.
        Rows already ``PROMOTED`` or ``DISCARDED`` are untouched — they are
        either already live truth (re-litigating them is supersession's job,
        not governance's) or an operator's permanent decision.

        Cost model: O(candidates re-governed) pure-Python/SQLite work, zero
        model calls — the property this method exists to prove out, since a
        threshold change previously cost a full re-ingest through the LLM.
        """
        candidates = [
            candidate
            for candidate in self._graph.quarantined_candidates(scope_key=scope.key, status=None, limit=limit)
            if candidate.status in (QuarantineStatus.PENDING, QuarantineStatus.QUARANTINED)
        ]
        job = DreamJob(
            name="regovern",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            scope=scope,
            agent=DreamAgentConfig(agent_id="regovern", name="Re-govern"),
        )
        receipt_run = self._begin_receipt_run(job=job, run_kind="regovern", effective_policy=None)
        promoted = 0
        still_quarantined = 0
        skipped_no_episode = 0
        for candidate in candidates:
            if candidate.episode_uuid is None:
                skipped_no_episode += 1
                continue
            try:
                episode = self._graph.get_episode(candidate.episode_uuid)
            except ValueError:
                skipped_no_episode += 1
                continue
            # WS-12: crypto-shred scopes seal the episode body at ingest; reveal
            # a copy for re-governance exactly like formation does, never
            # rewriting the stored (sealed) episode row.
            governance_episode = episode
            if is_sealed_content(episode.body):
                body_key = self._graph.get_governance_key(episode.scope.key)
                if body_key is None:
                    skipped_no_episode += 1
                    continue
                governance_episode = episode.model_copy(update={"body": open_content(episode.body, body_key)})
            try:
                instructions = self._config.instruction_set(candidate.instruction_set or job.instruction_set)
            except ValueError:
                skipped_no_episode += 1
                continue
            motive: Motive | None = None
            if candidate.motive_name is not None and self._config.memory_bank is not None:
                try:
                    motive = self._config.memory_bank.motive(candidate.motive_name)
                except ValueError:
                    motive = None
            actionability = self._effective_actionability(motive=motive)
            governance = self._effective_governance(motive=motive)
            gov_digest = governance_policy_digest(governance)
            protection = self._content_protection(scope_key=scope.key, governance=governance)
            raw_payload_text = self._graph.quarantined_candidate_payload(candidate.candidate_uuid)
            if protection is not None and is_sealed_content(raw_payload_text):
                raw_payload_text = open_content(raw_payload_text, protection.key)
            raw = json.loads(raw_payload_text)

            memory, rejection, abstention = self._extractor.regovern_candidate(
                raw,
                episode=governance_episode,
                instructions=instructions,
                actionability=actionability,
            )
            if memory is not None:
                # WS-3: the Motive type gate is a stage-3 check too — apply it
                # here exactly as formation does before materializing.
                if motive is not None and motive.allowed_memory_types:
                    memory_type_value = self._resolve_memory_type_value(
                        relationship_type=memory.relationship_type,
                        instruction_set_name=instructions.name,
                        subject_label=memory.subject_label,
                        object_label=memory.object_label,
                    )
                    allowed_values = {t.value for t in motive.allowed_memory_types}
                    if memory_type_value is not None and memory_type_value not in allowed_values:
                        self._mark_raw_candidate_quarantined(
                            digest=candidate.candidate_uuid,
                            now=now,
                            reason="memory_type_not_allowed_by_motive",
                            resolution_note=f"memory_type_not_allowed_by_motive:{motive.name}",
                            resolved_by=resolved_by,
                        )
                        still_quarantined += 1
                        continue
                # self_promote_raw_candidates stays False (default) here on
                # purpose: a row filed straight into QUARANTINED by
                # _file_quarantined_candidates (the confidence/property-key/
                # predicate-shape/actionability checks inside
                # extraction._validate_memory) is keyed by
                # rejected_candidate_digest over the RAW dict, not by
                # candidate_digest over a validated ExtractedMemory — a
                # different formula than materialize's internal self-promote
                # would compute.  Rather than teach materialize two keying
                # schemes, regovern transitions the row itself below using
                # candidate.candidate_uuid — the one key that is DEFINITELY
                # correct, since it is exactly what was looked up to get here.
                # raw_candidates_stored=True still applies: this candidate DOES
                # have a stored raw row (this one), so materialize must never
                # abort over it either.
                start_index = receipt_run.next_event_index
                self._materialize_episode(
                    governance_episode,
                    [memory],
                    created_by=resolved_by,
                    receipt_run=receipt_run,
                    job=None,
                    now=now,
                    dedup_threshold_override=motive.dedup_threshold if motive is not None else None,
                    governance=governance,
                    motive_name=candidate.motive_name,
                    motive_version_digest_value=None,
                    governance_policy_digest=gov_digest,
                    raw_candidates_stored=True,
                )
                relationship_uuid = next(
                    (
                        receipt.relationship_uuid
                        for receipt in self._graph.receipts.receipts_for_run(receipt_run.run_uuid)
                        if receipt.event_index >= start_index and receipt.relationship_uuid is not None
                    ),
                    None,
                )
                if relationship_uuid is not None:
                    self._mark_raw_candidate_promoted(
                        digest=candidate.candidate_uuid,
                        now=now,
                        relationship_uuid=relationship_uuid,
                        resolved_by=resolved_by,
                    )
                    promoted += 1
                else:
                    # Materialized with no relationship_uuid on any receipt is
                    # not expected, but stage 3 never aborts: leave the row's
                    # status as-is rather than mis-marking it PROMOTED.
                    still_quarantined += 1
            else:
                violation = rejection.violation if rejection is not None else abstention.violation  # type: ignore[union-attr]
                reason = rejection.reason if rejection is not None else abstention.reason  # type: ignore[union-attr]
                self._mark_raw_candidate_quarantined(
                    digest=candidate.candidate_uuid,
                    now=now,
                    reason=violation.value,
                    resolution_note=f"regoverned:{reason}",
                    resolved_by=resolved_by,
                )
                still_quarantined += 1
        if receipt_run.next_event_index > 0:
            self._checkpoint_receipt_run(receipt_run=receipt_run, job=job)
        return RegovernResult(
            scope_key=scope.key,
            candidates_considered=len(candidates),
            promoted=promoted,
            still_quarantined=still_quarantined,
            skipped_no_episode=skipped_no_episode,
            run_uuid=receipt_run.run_uuid if receipt_run.next_event_index > 0 else None,
        )

    def _receipt_embedding_outage(
        self,
        *,
        receipt_run: ReceiptRun,
        job: DreamJob,
        error: GatewayRequestError,
        now: datetime,
    ) -> None:
        """Attest that a formation run stopped early because embeddings went away.

        The deferral is never silent: it lands in the same hash-chained run as
        the work that DID complete, so an operator reading the checkpoint sees
        exactly why the queue did not drain.  The reason is a bounded,
        whitespace-free machine code (``is_content_free_reason``) carrying only
        the attempt count and the HTTP status — never the provider's response
        body, which would smuggle content into a plaintext lineage column.

        The receipt is filed against the scope the run was last working in, so a
        single-scope run stays single-scope and keeps its replayable
        run-boundary state hashes.
        """
        receipts = self._graph.receipts.receipts_for_run(receipt_run.run_uuid)
        scope_key = (
            receipts[-1].scope_key if receipts else (job.scope.key if job.scope is not None else job.agent.scope.key)
        )
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.EMBEDDING_TRANSPORT_UNAVAILABLE,
            decision_reason=(
                f"episode_deferred:attempts={error.attempts}"
                f":status={error.status if error.status is not None else 'none'}"
            ),
            # "gated" is the ledger's vocabulary for a decision that leaves no
            # context-visible memory row (NON_MATERIALIZING_RESULTS), which is
            # exactly what a deferral is.
            decision_result="gated",
            now=now,
            scope_key=scope_key,
            embedding_identifier=self._embedding_transport.identifier,
        )

    # ------------------------------------------------------------------ WS-11 receipts

    def _resolved_inputs_dict(self, job: DreamJob) -> dict[str, object]:
        """WS-11: canonical resolved policy inputs for the config path ([0018], addendum)."""
        cfg = self._config
        resolved_motive = self._config.resolve_motive(job=job, episode_metadata={})
        return {
            "tenant_id": None,
            "agent_id": job.agent.agent_id,
            "scope": job.scope.key if job.scope is not None else None,
            "dream_mode": None,
            "prompt_pack": None,
            "governance": cfg.governance.model_dump(mode="json") if cfg.governance is not None else None,
            "dedup": cfg.dedup.model_dump(mode="json"),
            "pruning": cfg.pruning.model_dump(mode="json"),
            "require_dream_agent_approval_for_untrusted_directives": (
                cfg.require_dream_agent_approval_for_untrusted_directives
            ),
            "read_only_scopes": sorted(cfg.read_only_scopes),
            "job": {
                "name": job.name,
                "kind": job.kind.value,
                "motive": job.motive,
                "prompt_profile": job.prompt_profile,
                "prompt_profile_version": job.prompt_profile_version,
                "salience_rubric": (
                    job.salience_rubric.model_dump(mode="json") if job.salience_rubric is not None else None
                ),
            },
            "motive": resolved_motive.model_dump(mode="json") if resolved_motive is not None else None,
        }

    def _run_policy_digests(
        self, job: DreamJob, effective_policy: EffectiveMemoryPolicy | None
    ) -> tuple[str, str | None]:
        """WS-11: (effective_policy_digest, motive_version_digest) for a run start ([0018], claim 7)."""
        resolved_inputs = self._resolved_inputs_dict(job)
        resolved_motive = self._config.resolve_motive(job=job, episode_metadata={})
        if effective_policy is not None:
            epd = effective_policy_digest(effective_policy)
            motive_model = effective_policy.motive or resolved_motive
        else:
            epd = config_effective_policy_digest(resolved_inputs)
            motive_model = resolved_motive
        mvd = motive_version_digest(motive_model, resolved_inputs) if motive_model is not None else None
        return epd, mvd

    def _preflight_formation_attestation(self, *, job: DreamJob) -> None:
        """Refuse a formation run whose DSSE signer this process's KEK cannot produce.

        One SELECT, before any claim. Three outcomes, because an operator needs to tell three
        situations apart and the pre-existing code collapsed them into zero signals:

        * the stored key will not unwrap -- raise, nothing is written, the job stays due;
        * no row for this signer but the store holds others -- the KEK CHANGED. Warn loudly and
          proceed. This is the case that actually happens: `signer_id` embeds the KEK's hash, so
          a rotated key misses the lookup and quietly mints a second identity. Not fatal, because
          on SQLite with an ephemeral KEK it is every test and every laptop run -- and on
          production Postgres an ephemeral KEK is already refused at construction, so there it
          genuinely means a durable key changed;
        * first use or a clean match -- silent.
        """
        status = self._graph.formation_signing_key_status()
        if status.state is FormationSignerState.KEK_MISMATCH:
            raise FormationAttestationUnavailableError(
                f"formation contract attestation is unavailable for job {job.name!r}: this "
                f"process's KEK cannot unwrap the DSSE signing key stored under signer "
                f"{status.signer_id!r}. The row exists; the key that wrapped it is not the key "
                "this process holds.\n\n"
                "This is a KEK MISMATCH -- not a missing KEK, and not a database fault. Almost "
                "always one of:\n"
                "  * MEMOTRON_KEK_B64 / MEMOTRON_KEK_FILE now resolves to different bytes "
                "than when this store was first written -- a rotated Secret, a re-created KEK "
                "file, or a database restored alongside the wrong key;\n"
                "  * a KMS key that kept its id but lost or rotated its material, or is "
                "disabled;\n"
                "  * the wrapped_private_key column was altered or truncated.\n\n"
                "No episodes were processed and this job is still due: formation refuses to "
                "write memories whose governing contract it cannot attest, because an episode "
                "is marked processed once and never re-formed, so an unattested memory stays "
                "unattested permanently.\n\n"
                "To recover: restore the KEK this store was written under. If that key is "
                "genuinely gone, deleting the formation_contract_signing_keys row for this "
                "signer_id mints a new signer and unblocks formation -- ACCEPTING that every "
                "attestation issued before that point verifies against a public key this store "
                "can no longer reproduce. That is a deliberate break in the attestation chain, "
                "not a repair.\n\n"
                f"Underlying error: {status.underlying}"
            )
        if status.state is FormationSignerState.NEW_SIGNER_UNDER_A_DIFFERENT_KEK:
            ephemeral = status.underlying == "ephemeral KEK"
            _log.warning(
                "formation is minting a NEW DSSE signing key under signer id %r. This store "
                "already holds %d signing key(s) under other signer ids, most recently created "
                "%s. The KEK changed. Nothing is broken and nothing is lost, but attestations "
                "from this run verify against a different public key than earlier runs', and no "
                "record links the two.%s If this was not an intentional rotation, restore the "
                "previous KEK before more memories form.",
                status.signer_id,
                status.other_signer_count,
                status.other_signer_latest_created_at or "unknown",
                (
                    " This process is running with an EPHEMERAL KEK, so every restart mints "
                    "another signer and that count will keep climbing; set MEMOTRON_KEK_B64 "
                    "or MEMOTRON_KEK_FILE before this store carries memories anyone needs to "
                    "prove the provenance of."
                    if ephemeral
                    else ""
                ),
            )

    def _begin_receipt_run(
        self,
        *,
        job: DreamJob,
        run_kind: str,
        effective_policy: EffectiveMemoryPolicy | None,
    ) -> ReceiptRun:
        epd, mvd = self._run_policy_digests(job, effective_policy)
        run_scope_key = job.scope.key if job.scope is not None else job.agent.scope.key
        return self._graph.receipts.begin_run(
            run_kind=run_kind,
            job_name=job.name,
            scope_key=run_scope_key,
            effective_policy_digest=epd,
            tenant_id=effective_policy.tenant_id if effective_policy is not None else None,
            agent_id=job.agent.agent_id,
            motive_version_digest=mvd,
            graph_state_hash_before=(self._graph.graph_state_hash(job.scope.key) if job.scope is not None else None),
        )

    def _checkpoint_receipt_run(self, *, receipt_run: ReceiptRun, job: DreamJob) -> None:
        # Empty runs must not checkpoint (the ledger enforces this); a run that emitted
        # no receipts is a no-op run with nothing to attest.
        if receipt_run.next_event_index == 0:
            return
        # Derive the run's actual scope from its emitted receipts (a formation job pins
        # no scope — the scope comes from the episodes).  When the run touched exactly
        # one scope, record spec-faithful run-boundary graph state hashes ([0022]) so the
        # run can also be byte-replayed against its checkpoint without the live store; a
        # multi-scope run leaves them None and is reconstructed per scope from the receipts.
        receipts = self._graph.receipts.receipts_for_run(receipt_run.run_uuid)
        scopes = {r.scope_key for r in receipts}
        graph_state_hash_after: str | None = None
        if len(scopes) == 1:
            (run_scope,) = tuple(scopes)
            graph_state_hash_after = self._graph.graph_state_hash(run_scope)
            first_before = next(
                (r.graph_state_hash_before for r in receipts if r.graph_state_hash_before is not None),
                None,
            )
            if first_before is not None:
                receipt_run.graph_state_hash_before = first_before
        self._graph.receipts.checkpoint(
            receipt_run,
            graph_state_hash_after=graph_state_hash_after,
            redream_tier=self._redream_tier,
            redream_tier_reason=self._redream_tier_reason,
        )

    def _episode_digest(self, episode: Episode) -> str:
        """Evidence digest over the ORIGINAL immutable episode body ([0018])."""
        return evidence_digest(episode.body, dict(episode.metadata), episode.scope.key)

    def _content_protection(self, *, scope_key: str, governance: GovernancePolicy | None) -> _ContentProtection | None:
        """WS-12: resolve the content-plane protection for a write into *scope_key*.

        Returns None unless the effective governance says CRYPTO_SHRED.  Under
        CRYPTO_SHRED the scope DEK is auto-provisioned on first write (envelope-
        wrapped; never silently skipped), and writing into an already-shredded
        scope fails fast with :class:`ContentKeyUnavailableError`.
        """
        if governance is None:
            return None
        from memotron.config import ErasureBehavior

        if governance.erasure_behavior != ErasureBehavior.CRYPTO_SHRED:
            return None
        return _ContentProtection(key=self._graph.get_or_create_governance_key(scope_key))

    def content_embedding_transport(
        self,
        *,
        scope_key: str,
        protection: _ContentProtection | None = None,
    ) -> EmbeddingTransport:
        """WS-23 H1: the transport that is allowed to SEE this scope's content.

        A tenant may configure a network embedding endpoint
        (``OpenAICompatibleEmbeddingTransport`` — OpenAI, a LiteLLM proxy, the
        JedAI Gateway).  Under CRYPTO_SHRED governance that would POST every
        fact, object, episode body and rollup text of the scope to a third party:
        outside the DEK, outside the ciphertext-only sweep, and outside the
        erasure certificate — a shred that cannot actually erase.  The LLM path
        already refuses and receipts (``crypto_shred_scope_content_never_sent_to_llm``);
        embeddings had no gate at all.

        Content-protected scopes are therefore forced onto the hermetic
        ``LocalEmbeddingTransport``.  The downgrade is per SCOPE and applies to
        writes and reads alike, so the whole scope lives in one vector space and
        same-space comparisons keep working (semantic dedup, clustering, and
        vector search continue to function; they simply never leave the
        process).  With the default local transport this is a no-op.
        """
        transport = self._embedding_transport
        if isinstance(transport, LocalEmbeddingTransport):
            return transport
        if protection is None and not self._graph.scope_content_is_protected(scope_key):
            return transport
        if self._local_embedding_fallback is None:
            self._local_embedding_fallback = LocalEmbeddingTransport()
        return self._local_embedding_fallback

    def _embed_content(
        self,
        text: str,
        *,
        scope_key: str,
        protection: _ContentProtection | None,
        receipt_run: ReceiptRun | None = None,
        now: datetime | None = None,
    ) -> list[float]:
        """Embed one CONTENT-plane string through the scope's allowed transport.

        The single write-side choke point for WS-23 H1: every formation and
        consolidation embedding of a fact, object, episode body, or rollup text
        goes through here.  A downgrade is receipted once per (run, scope),
        mirroring the synthesis gate's receipted skip.
        """
        transport = self.content_embedding_transport(scope_key=scope_key, protection=protection)
        if transport is not self._embedding_transport and receipt_run is not None:
            marker = (receipt_run.run_uuid, scope_key)
            if marker not in self._embedding_downgrade_receipted:
                # One receipt per (run, scope); the memo keeps only the run in
                # flight so a long-lived platform process cannot accumulate it.
                self._embedding_downgrade_receipted = {
                    item for item in self._embedding_downgrade_receipted if item[0] == receipt_run.run_uuid
                }
                self._embedding_downgrade_receipted.add(marker)
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.EMBEDDING_TRANSPORT_DOWNGRADED,
                    decision_reason="crypto_shred_scope_content_never_sent_to_embedding_endpoint",
                    decision_result="gated",
                    now=now,
                    scope_key=scope_key,
                    embedding_identifier=transport.identifier,
                )
        return transport.embed(text)

    def _receipt_sensitive_payload(
        self, *, fact: str, protection: _ContentProtection | None
    ) -> tuple[str | None, bool]:
        """WS-11/WS-12: stored sensitive representation for a receipt — sealed
        AES-256-GCM ciphertext under crypto-shred governance so hashes verify
        without the key (claim 10), else None (the graph itself stores no
        ciphertext outside crypto-shred scopes)."""
        if protection is None:
            return None, False
        return protection.seal(fact), True

    def _claim_mode_for(self, *, memory: ExtractedMemory, memory_type_value: str):
        memory_type = MemoryType(memory_type_value)
        claim_mode = memory.claim_mode or default_claim_mode_for_memory_type(memory_type)
        try:
            validate_claim_mode_for_memory_type(claim_mode=claim_mode, memory_type=memory_type)
        except ValueError:
            # Coerce, exactly as extraction's _validate_memory does, instead of
            # raising.  These were two contracts for one decision: extraction
            # coerces a mismatched claim_mode and receipts it
            # (CANDIDATE_CLAIM_MODE_COERCED), while this path re-validated and
            # raised -- so a candidate that legitimately survived extraction
            # killed the whole formation run at materialization.  With an OPEN
            # predicate vocabulary the mismatch is routine: the model picks a
            # claim mode from the verb it wrote, not from the memory type the
            # matched instruction assigns.  One divergent field must cost one
            # candidate's field, never the run.
            coerced = default_claim_mode_for_memory_type(memory_type)
            _log.info(
                "claim_mode %r invalid for memory_type %r; coercing to %r",
                getattr(claim_mode, "value", claim_mode),
                memory_type.value,
                getattr(coerced, "value", coerced),
            )
            claim_mode = coerced
        return claim_mode

    @staticmethod
    def _semantic_polarity(text: str) -> str:
        # WS-25 T1: delegates to the single canonical implementation in
        # retrieval.py (shared with the read-side within-slot tiebreaker).
        return _semantic_polarity_fn(text)

    def _source_authority(
        self,
        *,
        episode: Episode,
        created_by: str,
        claim_mode: ClaimMode,
    ) -> MemoryAuthority:
        verified = episode.metadata.get("_verified_source_authority")
        if isinstance(verified, str):
            return MemoryAuthority(verified)
        if episode.metadata.get("artifact_class") == "generated_output":
            return MemoryAuthority.GENERATED_OUTPUT
        if episode.metadata.get("trusted") is False:
            return MemoryAuthority.UNTRUSTED
        if claim_mode == ClaimMode.CORRECTION:
            return MemoryAuthority.OPERATOR
        if episode.metadata.get("agent_memory") is True:
            return MemoryAuthority.AGENT
        if created_by.startswith("system"):
            return MemoryAuthority.SYSTEM
        if created_by.startswith("operator"):
            return MemoryAuthority.OPERATOR
        return MemoryAuthority.USER

    def _relationship_authority(self, relationship: GraphRelationship) -> MemoryAuthority:
        raw = relationship.properties.get("source_authority")
        if isinstance(raw, str):
            try:
                return MemoryAuthority(raw)
            except ValueError:
                pass
        if relationship.properties.get("authority_class") == "non_normative":
            return MemoryAuthority.GENERATED_OUTPUT
        metadata = relationship.properties.get("metadata")
        if isinstance(metadata, dict) and metadata.get("agent_memory") is True:
            return MemoryAuthority.AGENT
        if relationship.properties.get("claim_mode") == ClaimMode.CORRECTION.value:
            return MemoryAuthority.OPERATOR
        return MemoryAuthority.USER

    def _supersession_gate_reason(
        self,
        *,
        incumbent: GraphRelationship,
        candidate_authority: MemoryAuthority,
        severity_score: int,
        temporary: bool,
        corroboration_total: int | None = None,
    ) -> str | None:
        """Single supersession gate: authority, temporary-severity, corroboration.

        ``corroboration_total`` (WS-16 T12) is the number of distinct
        observations of the current challenger statement — this one plus
        previously parked siblings — or ``None`` when no contradiction on the
        slot is corroboration-eligible (scan skipped).  The corroboration gate
        fires only for an EQUAL-rank challenger against an incumbent whose
        ``observed_count`` has reached ``corroboration_margin``; higher-authority
        challengers are never corroboration-gated, and lower-authority
        challengers are already parked by the authority gate above.
        """
        policy = self._config.supersession
        incumbent_authority = self._relationship_authority(incumbent)
        candidate_rank = policy.authority_rank(candidate_authority)
        incumbent_rank = policy.authority_rank(incumbent_authority)
        if policy.gate_lower_authority and candidate_rank < incumbent_rank:
            return f"lower_authority:{candidate_authority.value}<{incumbent_authority.value}"
        if (
            temporary
            and policy.gate_equal_authority_temporary_high_severity
            and severity_score >= policy.temporary_review_severity
            and candidate_rank <= incumbent_rank
        ):
            return (
                "temporary_high_severity_requires_higher_authority:"
                f"{severity_score}>={policy.temporary_review_severity}"
            )
        if corroboration_total is not None and candidate_rank == incumbent_rank:
            incumbent_observed = int(incumbent.properties.get("observed_count", 1))
            if (
                incumbent_observed >= policy.corroboration_margin
                and corroboration_total < policy.corroboration_required
            ):
                return (
                    "insufficient_corroboration:"
                    f"incumbent_observed_count={incumbent_observed}:"
                    f"corroborations={corroboration_total}/{policy.corroboration_required}"
                )
        return None

    def _recency_authoritative_bypass(
        self,
        *,
        memory_type: str | None,
        candidate_authority: MemoryAuthority,
        incumbent: GraphRelationship,
        effective_from: datetime,
    ) -> bool:
        """WS-25 T2: does a same-slot contradictory candidate auto-close
        *incumbent* regardless of the ``temporary_high_severity`` /
        ``insufficient_corroboration`` gates?

        True only when *memory_type* is configured recency-authoritative
        (``SupersessionPolicy.recency_authoritative_types`` — default
        preference/directive/state), the candidate's CLAMPED effective time is
        not older than the incumbent's, and the candidate's authority is at
        least the incumbent's.  Never true for a genuinely lower-authority
        challenger — that stays gated by ``lower_authority`` regardless of
        recency (NEXT.md WS-25 §9: this is not global last-writer-wins).

        *effective_from* must already be clamped by the caller to
        ``min(candidate.valid_from, episode.reference_time)`` — the
        extractor's claimed event time can never postdate the observation
        that carries it, so a spoofed future ``valid_from`` cannot pass this
        check against a genuinely newer incumbent.
        """
        policy = self._config.supersession
        if memory_type is None or memory_type not in policy.recency_authoritative_types:
            return False
        incumbent_effective = incumbent.valid_from or incumbent.created_at
        if incumbent_effective is not None and effective_from < incumbent_effective:
            return False
        candidate_rank = policy.authority_rank(candidate_authority)
        incumbent_rank = policy.authority_rank(self._relationship_authority(incumbent))
        return candidate_rank >= incumbent_rank

    def _bind_formation_contract(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        job: DreamJob,
        instructions: object,
        prompt_profile: object,
        motive: Motive | None,
        governance: GovernancePolicy | None,
    ) -> FormationContract:
        """Compile and DSSE-attest the exact policy governing one episode."""
        profile_source = "job"
        if motive is not None and motive.prompt_profile is not None:
            profile_source = f"motive:{motive.name}"
        elif motive is not None and motive.prompt_override is not None:
            profile_source = f"motive_override:{motive.name}"
        motive_source = (
            "job"
            if job.motive is not None
            else "episode_metadata"
            if episode.metadata.get("motive") is not None
            else "none"
        )
        source_trace = {
            **self._config.runtime_policy_source_trace,
            "instruction_set": f"job:{job.name}",
            "formation_profile": profile_source,
            "formation_motive_binding": motive_source,
            "governance": f"motive:{motive.name}" if motive is not None and motive.governance is not None else "config",
            "dedup": "motive" if motive is not None and motive.dedup_threshold is not None else "config",
            # WS-17 T16b: entity resolution has no per-Motive override — the
            # config policy is the single source, bound like dedup/governance.
            "entity_resolution": "config",
            "actionability": (
                f"motive:{motive.name}" if motive is not None and motive.actionability is not None else "config"
            ),
        }
        if self._config.runtime_policy_alias is not None:
            source_trace["policy_alias"] = self._config.runtime_policy_alias
        if self._config.runtime_policy_contract_digest is not None:
            source_trace["policy_contract"] = self._config.runtime_policy_contract_digest
        contract = FormationContract(
            scope_key=episode.scope.key,
            instruction_set=instructions.model_dump(mode="json"),
            formation_profile=prompt_profile,
            motive=motive,
            governance=governance,
            dedup=self._config.dedup,
            entity_resolution=self._config.entity_resolution,
            actionability=self._effective_actionability(motive=motive),
            source_trace=source_trace,
            model_identifier=self._receipt_model_identifier,
            extractor_identifier=self._receipt_extractor_identifier,
            embedding_identifier=self._receipt_embedding_identifier,
        )
        digest = payload_digest(contract.model_dump(mode="json"))
        try:
            attestation = self._graph.attest_formation_contract(
                contract_digest=digest,
                certification_verdict=self._config.runtime_certification_verdict,
            )
        except (ValueError, ContentKeyUnavailableError) as exc:
            # The backstop for the one window the preflight cannot cover: a key revoked or a
            # connection lost BETWEEN the preflight and this episode. Still fatal -- but now it
            # names the subsystem instead of surfacing a bare "wrapped DEK failed
            # authentication" that reads like an LLM-credential problem.
            raise FormationAttestationUnavailableError(
                "formation contract attestation failed while the run was already in flight: "
                "the DSSE signing key could not be produced. The preflight passed, so the key "
                "became unusable during this run -- a revoked or disabled KMS key, or a lost "
                "connection. Episodes processed before this point are formed and attested; "
                "this run is NOT checkpointed.\n"
                f"Underlying error: {type(exc).__name__}: {exc}"
            ) from exc
        receipt_run.formation_contract_digest = digest
        receipt_run.formation_contract_source_trace = json.dumps(source_trace, sort_keys=True, separators=(",", ":"))
        receipt_run.formation_contract_attestation = json.dumps(
            attestation.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        )
        return contract

    def _emit_receipt(
        self,
        receipt_run: ReceiptRun,
        *,
        decision_type: ReceiptDecisionType,
        decision_reason: str,
        decision_result: str,
        episode: Episode | None = None,
        now: datetime | None = None,
        **fields: object,
    ):
        """WS-11: choke-point wrapper that auto-fills run-invariant provenance fields."""
        payload: dict[str, object] = dict(fields)
        payload.setdefault("model_identifier", self._receipt_model_identifier)
        payload.setdefault("extractor_identifier", self._receipt_extractor_identifier)
        payload.setdefault("embedding_identifier", self._receipt_embedding_identifier)
        if episode is not None:
            payload.setdefault("episode_uuid", episode.uuid)
            payload.setdefault("scope_key", episode.scope.key)
            payload.setdefault("episode_digest", self._episode_digest(episode))
        if now is not None:
            payload.setdefault("created_at", now)
        return self._graph.receipts.emit(
            receipt_run,
            decision_type=decision_type,
            decision_reason=decision_reason,
            decision_result=decision_result,
            **payload,
        )

    def materialize_episode(
        self,
        episode: Episode,
        memories: list[ExtractedMemory],
        *,
        created_by: str,
        receipt_run: ReceiptRun,
        job: DreamJob | None = None,
        now: datetime | None = None,
        dedup_threshold_override: float | None = None,
        governance: GovernancePolicy | None = None,
        motive_name: str | None = None,
    ) -> tuple[int, int, int, int]:
        # WS-12: operator-run writes (add_memory / correct_memory) resolve the
        # config-level governance so crypto-shred scopes are content-protected
        # on EVERY write path, not only dream formation.
        effective_governance = governance if governance is not None else self._effective_governance(motive=None)
        return self._materialize_episode(
            episode,
            memories,
            created_by=created_by,
            receipt_run=receipt_run,
            job=job,
            now=now,
            dedup_threshold_override=dedup_threshold_override,
            governance=effective_governance,
            motive_name=motive_name,
            governance_policy_digest=governance_policy_digest(effective_governance),
        )

    # ------------------------------------------------------------------ WS-10 coherence

    # ------------------------------------------------------------------
    # Staged pipeline: EXTRACT (stage 1, stored raw) -> GOVERN (stage 3, marks
    # not aborts).  Every candidate that reaches ``scored_memories`` in
    # ``_run_formation`` is filed here THE MOMENT it is extracted, before any
    # governance gate runs — the raw/quarantine store is the countable,
    # inspectable stage-1 artifact.  A drop at any later gate (salience, the
    # Motive type filter, the untrusted/generated-output gates, or
    # materialization itself) transitions this SAME row to ``QUARANTINED``
    # rather than leaving it an orphaned receipt with no queryable row; a
    # successful materialize transitions it to ``PROMOTED``.  Every transition
    # is keyed by the row's own ``candidate_digest`` — identical to the digest
    # every other receipt at this candidate's disposition already computes —
    # so no uuid has to be threaded through five function signatures.

    # ------------------------------------------------------------------
    # WS-23 C1/C4/C5: the ONE truth-key rebridging primitive
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-17 T16b: semantic entity resolution (per-scope alias registry)
    # ------------------------------------------------------------------

    @staticmethod
    def _context_demotion_held(relationship: GraphRelationship) -> bool:
        """WS-23 C3: is this row held IN context against every demotion path?

        A pin is an operator hold on context PRESENCE, exactly like the
        retention gate treats it as a hold on retention (``claim_mode ==
        "correction"`` is the same class of hold).  WS-20 guarded the pruning
        gate only, so both consolidation demotion paths — rollup-member demotion
        and the cross-prefix duplicate sweep — could still set
        ``active_in_context=False`` on a pinned row, and the profile drops
        out-of-context rows before it ever looks at the pin.  The result was a
        pin that silently stopped guaranteeing anything.

        This is the shared predicate both paths consult at the moment they would
        write ``active_in_context=False`` — deliberately not when selecting
        candidates, so a pinned row still participates in clustering evidence
        and can still be the SURVIVING side of a duplicate pair.  Pinning does
        not stop supersession by newer truth; it only stops the row being
        demoted out of the caller's context.
        """
        return relationship.properties.get("pinned") is True

    def _effective_governance(self, *, motive: Motive | None) -> GovernancePolicy | None:
        """WS-7: Resolve the effective GovernancePolicy for an episode.

        Resolution order:
            1. Motive.governance  — per-Motive override (highest priority).
            2. config.governance  — config-level default.
            3. None               — no governance (legacy behaviour, opt-in invariant).
        """
        if motive is not None and motive.governance is not None:
            return motive.governance
        return self._config.governance
