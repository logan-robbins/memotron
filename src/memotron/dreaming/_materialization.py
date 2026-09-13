"""The one method that turns extracted candidates into graph rows.

_materialize_episode is 1,130 lines and moves here as a SINGLE UNIT, unchanged.
That is a deliberate choice, not deferred work:

  * splitting it changes bytecode, which forfeits pure_move -- the only mechanical
    proof this whole refactor has -- on the riskiest code in the package;
  * its `raw_candidates_stored` parameter selects between two DIFFERENT FAILURE
    REGIMES, and no test in the suite distinguishes them, so a decomposition that
    flipped one into the other would land green.

A 1,130-line method in a 1,200-line single-concern file is tractable. The same
method at line 7,030 of a 10,000-line class was not. That is the whole delivery
here; decomposing the body is a separate pass with its own evidence.

The three call sites and the regime each selects
------------------------------------------------
  regovern_scope        (__init__.py)  raw_candidates_stored=True,  self_promote=False
  _run_formation        (__init__.py)  raw_candidates_stored=True,  self_promote=True
  materialize_episode   (__init__.py)  both default False  -- the OPERATOR path

raw_candidates_stored=False is fail-fast: an unresolvable relationship_type or
memory_type raises and aborts the whole call, which is correct for a single
deliberate operator write and is documented on the method as preserved
"byte-for-byte". raw_candidates_stored=True is quarantine-and-continue: the one
bad candidate is filed and skipped so a model that produced 999 good candidates
and one bad one still lands 999. Flipping either direction is silent -- the
operator path would swallow a mistake, or a batch run would abort on one row.

self_promote_raw_candidates is the second axis and is NOT redundant with the first.
regovern_scope sets stored=True but promote=False because a row quarantined
straight from extraction is keyed by a different digest formula (over the raw dict,
before the candidate was a validated ExtractedMemory); regovern holds that real key
and transitions the row itself rather than having this method guess it.

tests/test_staged_pipeline.py:383 and :406 call the method directly with
raw_candidates_stored=True, which is the only place the quarantine regime is
exercised in isolation."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from memotron.config import (
    DreamJob,
    GovernancePolicy,
    claim_mode_stance,
)
from memotron.dreaming._common import _ContentProtection, _log
from memotron.dreaming._identity import assert_dual_representation_invariant, node_identity_key, truth_identity
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.extraction import (
    CandidateViolation,
    match_relationship_instruction,
)
from memotron.models import (
    Episode,
    ExtractedMemory,
    GraphRelationship,
    RelationshipCardinality,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    candidate_digest,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class MaterializationMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    def _materialize_episode(
        self,
        episode: Episode,
        memories: list[ExtractedMemory],
        *,
        created_by: str = "dream-formation",
        receipt_run: ReceiptRun,
        job: DreamJob | None = None,
        now: datetime | None = None,
        dedup_threshold_override: float | None = None,
        governance: GovernancePolicy | None = None,
        motive_name: str | None = None,
        motive_version_digest_value: str | None = None,
        governance_policy_digest: str | None = None,
        raw_candidates_stored: bool = False,
        self_promote_raw_candidates: bool = False,
    ) -> tuple[int, int, int, int]:
        """Materialize extracted memories into the property graph.

        WS-11: each surviving candidate's disposition is receipted through the
        required *receipt_run* — FORMATION_SEMANTIC_DEDUP_REINFORCED for a dedup
        merge (exact or semantic), FORMATION_RELATIONSHIP_CREATED (with
        graph_state_hash before/after — claim 4) for a new row, and
        FORMATION_TRUTH_KEY_SUPERSEDED for every supersession pointer written.
        ``job``/``now`` stay optional so the client's operator-run callers
        (add_memory / correct_memory) work unchanged.

        ``raw_candidates_stored`` (default ``False``) means every *memory*
        passed here already has SOME stage-1 raw row — set by both
        ``_run_formation`` and ``regovern_scope`` — so stage 3 never aborts:
        an unresolvable relationship_type/memory_type at this point
        (previously a bare ``raise`` — one of the three disagreement bugs the
        staged pipeline restructure closes) quarantines the ONE affected
        memory and skips it instead of aborting the whole materialize call.
        The operator write path (``materialize_episode`` / ``add_memory`` /
        ``correct_memory``) never stores a raw row, so it leaves this
        ``False`` and keeps its original fail-fast behaviour byte-for-byte: a
        caller who names an unresolvable relationship type made a mistake in
        a single deliberate write, not a model that produced 999 good
        candidates and one bad one.

        ``self_promote_raw_candidates`` (default ``False``) additionally makes
        a successful reinforce/create SELF-transition its raw row to
        ``PROMOTED``, keyed by the digest this method computes internally
        (``candidate_digest`` over the memory) — correct exactly when the raw
        row was ALSO stored under that same formula, which is true for
        ``_run_formation``'s stage-1 storage.  ``regovern_scope`` sets
        ``raw_candidates_stored=True`` (never abort) but leaves this ``False``
        and performs its own transition afterward, because a row that started
        life quarantined straight from extraction
        (``_file_quarantined_candidates``) is keyed by a different digest
        formula (over the raw dict, computed before the candidate was even a
        validated ``ExtractedMemory``) — regovern already holds that row's
        real key and uses it directly rather than asking this method to guess.

        Bi-temporality contract (Zep-aligned):
        ─────────────────────────────────────────────────────────────────
        Two independent time axes are tracked on every relationship:

        • Valid time  — when the fact was true in the world.
                        Columns: ``valid_from`` / ``valid_to``.
                        Set from the extracted memory (or episode reference_time).
                        ``as_of`` search operates on this axis.

        • Transaction time — when we first recorded the fact (created_at),
                              last reinforced it (reinforced_at), and when it
                              was superseded or pruned (superseded_at /
                              pruned_at, stored in properties JSON).
                              Immutable once written; used for audit lineage.

        Supersession implements "invalidate-don't-delete": a contradicting new
        fact sets the old row's ``valid_to`` to the new fact's ``valid_from``,
        keeping the old row queryable via ``as_of``.  The graph never deletes
        rows — pruned/superseded rows remain for audit and historical search.

        We currently ship a single-axis ``as_of`` (valid time).  Adding a
        second ``as_known_at`` axis (transaction time) is deferred per NEXT.md
        §8 decision 8 until a deal requires full Zep bi-temporal parity.
        """
        created_nodes = 0
        created_relationships = 0
        reinforced_relationships = 0
        superseded_relationships = 0
        episode_node, created = self._graph.upsert_node(
            labels=("Episode",),
            key=f"episode:{episode.uuid}",
            properties={
                "episode_uuid": episode.uuid,
                "name": episode.name,
                "source": episode.source.value,
                "source_description": episode.source_description,
                "scope_kind": episode.scope.kind.value,
                "scope_id": episode.scope.scope_id,
                "scope_key": episode.scope.key,
                "reference_time": episode.reference_time.isoformat(),
                "metadata": episode.metadata,
            },
            valid_from=episode.reference_time,
        )
        created_nodes += int(created)

        # WS-12: per-scope content protection (a memory may override the episode scope).
        protections: dict[str, _ContentProtection | None] = {}
        for memory in memories:
            scope = memory.scope or episode.scope
            if scope.key not in protections:
                protections[scope.key] = self._content_protection(scope_key=scope.key, governance=governance)
            protection = protections[scope.key]
            # Open predicates: exact-typed instruction first, then an
            # open_predicate instruction whose endpoint labels admit this
            # candidate.  MUST use the same matcher extraction used (real
            # labels — see _resolve_memory_type_value), or the memory_type
            # resolved here disagrees with the one extraction/scoring resolved
            # and materialization raises two layers from the cause.  Checked
            # FIRST, before any node upsert or entity resolution for this
            # memory, so a miss costs nothing to unwind.
            matched_instruction = match_relationship_instruction(
                instructions=self._config.instruction_set(episode.instruction_set),
                relationship_type=memory.relationship_type,
                subject_label=memory.subject_label,
                object_label=memory.object_label,
            )
            if matched_instruction is None or matched_instruction.memory_type is None:
                unresolved_reason = (
                    CandidateViolation.RELATIONSHIP_TYPE_NOT_ALLOWED.value
                    if matched_instruction is None
                    else CandidateViolation.MEMORY_TYPE_UNRESOLVED.value
                )
                if not raw_candidates_stored:
                    # The operator write path (add_memory / correct_memory):
                    # one deliberate write with an unresolvable relationship
                    # type is the caller's mistake, not a model's, so it stays
                    # fail-fast exactly as before this restructure.
                    if matched_instruction is None:
                        raise ValueError(
                            f"relationship type {memory.relationship_type!r} with endpoints "
                            f"({memory.subject_label!r} -> {memory.object_label!r}) matches "
                            "no instruction"
                        )
                    raise ValueError(f"relationship type {memory.relationship_type!r} has no resolved memory_type")
                # Stage 3 never aborts a run: mark this ONE memory and move on
                # to its siblings instead of raising.  Best-effort raw-row
                # transition — this path is unreachable for a candidate that
                # flowed through extraction's OWN (identical) real-label
                # matcher, so it exists as defense against a config change
                # mid-run rather than a live disposition.
                self._quarantine_unresolvable_candidate(
                    memory,
                    episode=episode,
                    receipt_run=receipt_run,
                    now=now,
                    motive_name=motive_name,
                    governance_policy_digest=governance_policy_digest,
                    protection=protection,
                    reason=unresolved_reason,
                )
                continue
            relationship_instruction = matched_instruction
            try:
                secret_reference, secret_lifecycle = self._secret_reference_metadata(memory)
            except ValueError as exc:
                self._reject_raw_secret_memory(
                    receipt_run=receipt_run,
                    episode=episode,
                    memory=memory,
                    now=now,
                    motive_name=motive_name,
                    governance_policy_digest=governance_policy_digest,
                    protection=protection,
                    error=exc,
                )
                # The candidate is already REJECTED above -- rejection means it
                # is never stored anywhere, which IS the security contract: a
                # raw credential must not reach the graph, and quarantine would
                # store it, so rejection is the only correct disposition.
                #
                # Re-raising additionally killed the whole run, which serves no
                # security purpose and cost real work: ingesting the gateway
                # docs (119 episodes) aborted on the first candidate that looked
                # like a raw `sk-` assignment, after 3 facts had materialized.
                # A corpus that DOCUMENTS credentials is exactly where this
                # detector fires most, so fatality guaranteed that the corpus
                # most needing ingest was the one that could never complete.
                #
                # One bad candidate costs one candidate. The receipt records
                # the refusal, and the remaining candidates in this episode --
                # and every later episode -- still materialize.
                _log.warning(
                    "episode %s: candidate rejected (raw credential detected); "
                    "it is never stored, and its siblings are unaffected",
                    episode.uuid,
                )
                continue
            # WS-17 T16b: resolve subject and object mention names through the
            # per-scope entity alias registry BEFORE node identity and truth
            # keys are computed — both planes are name-keyed, so name-level
            # aliasing bridges nodes AND truth slots in one place.  The digest
            # identity of the candidate stays the PRE-resolution surface form
            # (the CANDIDATE_EXTRACTED receipt already recorded it), so
            # counterfactual re-gating matches dispositions by digest unchanged.
            digest_memory = memory
            subject_surface = memory.subject
            object_surface = memory.object
            subject_resolution = self.resolve_canonical_entity(
                scope=scope,
                name=memory.subject,
                entity_ref=memory.subject_entity_ref,
                link_confidence=memory.subject_link_confidence,
                receipt_run=receipt_run,
                episode=episode,
                now=now,
                motive_name=motive_name,
                protection=protection,
            )
            object_resolution = self.resolve_canonical_entity(
                scope=scope,
                name=memory.object,
                entity_ref=memory.object_entity_ref,
                link_confidence=memory.object_link_confidence,
                receipt_run=receipt_run,
                episode=episode,
                now=now,
                motive_name=motive_name,
                protection=protection,
            )
            if subject_resolution.canonical != memory.subject or object_resolution.canonical != memory.object:
                memory = memory.model_copy(
                    update={
                        "subject": subject_resolution.canonical,
                        "object": object_resolution.canonical,
                    }
                )
            # The surface form is ALWAYS preserved on the row when it differs -- and it is
            # ENTITY CONTENT, so it seals like `fact` and `object` below.
            #
            # DEFENSIVE, NOT A FIX FOR A LIVE LEAK -- and the distinction is the point.
            # These hold the caller's ORIGINAL text (captured above, before canonicalisation)
            # and were written raw while also being absent from `RELATIONSHIP_SEALED_FIELDS`,
            # which reads exactly like a plaintext remnant that `erasure.sweep_scope` would
            # certify as clean. Measured 2026-09-08: it cannot happen. Entity resolution is
            # INERT while content is sealed (`_entities.py` skips sealed names), so a
            # canonical name is never rewritten under seal, so these keys are never written
            # under seal. They are reachable only under SOFT_RETIRE, where nothing seals.
            #
            # Kept anyway, with both halves moved together: if resolution is ever made
            # seal-aware the field starts being written and must already be both sealed here
            # and enumerated there. `test_alias_resolution_is_INERT_under_seal_so_aliases_do_not_merge`
            # pins the premise, so that change trips a test rather than shipping a remnant.
            entity_surface_properties: dict[str, object] = {}
            if memory.subject != subject_surface:
                entity_surface_properties["subject_surface"] = (
                    protection.seal(subject_surface) if protection is not None else subject_surface
                )
            if memory.object != object_surface:
                entity_surface_properties["object_surface"] = (
                    protection.seal(object_surface) if protection is not None else object_surface
                )

            # WS-12: node identity keys become keyed commitments (blind indexes) so
            # entity dedup keeps working over ciphertext; node names are sealed.
            # WS-13: shared with migration.py via node_identity_key.
            subject_node_key = node_identity_key(
                scope_key=scope.key,
                label=memory.subject_label,
                name=memory.subject,
                protection=protection,
            )
            object_node_key = node_identity_key(
                scope_key=scope.key,
                label=memory.object_label,
                name=memory.object,
                protection=protection,
            )
            subject_node, created = self._graph.upsert_node(
                labels=(memory.subject_label, scope.kind.value.title()),
                key=subject_node_key,
                properties={
                    "name": protection.seal(memory.subject) if protection is not None else memory.subject,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                    **memory.subject_properties,
                },
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
            )
            created_nodes += int(created)
            object_node, created = self._graph.upsert_node(
                labels=(memory.object_label, scope.kind.value.title()),
                key=object_node_key,
                properties={
                    "name": protection.seal(memory.object) if protection is not None else memory.object,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                    **memory.object_properties,
                },
                valid_from=memory.valid_from,
                valid_to=memory.valid_to,
            )
            created_nodes += int(created)
            # WS-17 T16b: stamp same-space name embeddings onto entity nodes
            # (absent or out-of-space only) so the composed link score's
            # name-cosine signal exists for later mentions.  Content-protected
            # scopes are inert (sealed names never feed a plaintext vector).
            if self._config.entity_resolution.enabled and protection is None:
                self._stamp_node_name_embedding(
                    node=subject_node,
                    name=memory.subject,
                    labels=(memory.subject_label, scope.kind.value.title()),
                    valid_from=memory.valid_from,
                    valid_to=memory.valid_to,
                )
                self._stamp_node_name_embedding(
                    node=object_node,
                    name=memory.object,
                    labels=(memory.object_label, scope.kind.value.title()),
                    valid_from=memory.valid_from,
                    valid_to=memory.valid_to,
                )
            # WS-27 T1: extractor-declared aliases ("GCX" for "guest content
            # experience") — first-class, written into the SAME entity_canon
            # registry the T16b resolution engine above reads and writes.
            instruction_set = self._config.instruction_set(episode.instruction_set)
            self._register_declared_aliases(
                scope=scope,
                label=memory.subject_label,
                canonical_name=memory.subject,
                properties=memory.subject_properties,
                instructions=instruction_set,
                protection=protection,
                now=now,
            )
            self._register_declared_aliases(
                scope=scope,
                label=memory.object_label,
                canonical_name=memory.object,
                properties=memory.object_properties,
                instructions=instruction_set,
                protection=protection,
                now=now,
            )

            # relationship_instruction was already resolved (and, on a miss,
            # this memory already skipped to its next sibling) at the top of
            # this loop, using this SAME real-label matcher — one resolution,
            # not a second one that could quietly disagree with it.
            #
            # _claim_mode_for is the ONE implementation of "resolve, and if the
            # extractor's claim_mode conflicts with the deterministic
            # memory_type, coerce instead of raising" — shared with the main
            # formation loop (CANDIDATE_CLAIM_MODE_COERCED) so this call site
            # cannot re-diverge into a raise the way it once did.
            claim_mode = self._claim_mode_for(
                memory=memory, memory_type_value=relationship_instruction.memory_type.value
            )
            directive_stance = claim_mode_stance(claim_mode)
            fact = f"{memory.subject} {memory.predicate} {memory.object}"
            semantic_polarity = self._semantic_polarity(fact)
            source_authority = self._source_authority(
                episode=episode,
                created_by=created_by,
                claim_mode=claim_mode,
            )
            authority_rank = self._config.supersession.authority_rank(source_authority)
            severity_score = self._config.supersession.severity(relationship_instruction.memory_type)
            # WS-1: embed the full fact text for storage (WS-4 clustering and
            # WS-9 multimodal reuse via relationship.properties["embedding"]).
            # We also embed the object-only text for dedup comparison within a
            # truth_prefix bucket: candidates share subject+predicate already,
            # so comparing object embeddings avoids false merges from shared
            # structural frame tokens (e.g. "preference 0" vs "preference 1"
            # share the frame "User 42 prefers preference" with high n-gram
            # overlap but differ in meaning when the object digit changes).
            fact_embedding = self._embed_content(
                fact,
                scope_key=scope.key,
                protection=protection,
                receipt_run=receipt_run,
                now=now,
            )
            object_embedding = self._embed_content(
                memory.object,
                scope_key=scope.key,
                protection=protection,
                receipt_run=receipt_run,
                now=now,
            )
            # WS-17 T16: the truth slot is keyed on the CANONICAL predicate so a
            # paraphrased surface ("resides in" vs "lives in") cannot split one
            # truth into two coexisting rows.  The surface predicate is stored
            # unchanged; only truth_key/truth_prefix use the canonical form.
            canonical_predicate = self.resolve_canonical_predicate(
                scope_key=scope.key,
                predicate=memory.predicate,
                receipt_run=receipt_run,
                episode=episode,
                now=now,
                motive_name=motive_name,
                decided_by="formation",
            )
            # WS-12: truth keys carry subject/predicate/object content — stored as
            # keyed commitments under crypto-shred governance so truth-slot equality
            # and prefix seeks work over ciphertext.
            # WS-13: shared with migration.py via truth_identity.
            stored_truth_key, stored_truth_prefix, new_object_commitment = truth_identity(
                scope_key=scope.key,
                subject=memory.subject,
                predicate=canonical_predicate,
                object_value=memory.object,
                cardinality=relationship_instruction.cardinality,
                protection=protection,
            )
            observed_at = memory.valid_from or episode.reference_time
            # WS-25 T2: clamped effective time for the recency-authoritative
            # supersession bypass.  The extractor's claimed valid_from can
            # never postdate the episode that actually carries the
            # observation, so a hallucinated/spoofed future valid_from cannot
            # deterministically out-rank a fact dated closer to the true
            # observation instant.  Used ONLY by the T2 bypass below — the
            # unclamped observed_at keeps its existing meaning everywhere else
            # (this row's own valid_from, the ordinary supersession valid_to).
            recency_effective_from = min(observed_at, episode.reference_time)
            relationship_status = RelationshipStatus.ACTIVE
            relationship_valid_to = memory.valid_to
            relationship_status_properties: dict[str, object] = {}
            supersede_after_insert = False
            # WS-16 T15: multi-active rows superseded for a polarity conflict.
            polarity_conflict_targets: list[GraphRelationship] = []
            # WS-16 T12: parked siblings to resolve once the corroborated flip lands.
            corroborated_parked_siblings: tuple[GraphRelationship, ...] = ()
            # WS-25 T2: incumbents auto-closed by the recency-authoritative
            # bypass — these get valid_to=recency_effective_from and a
            # recency_authoritative_supersede receipt instead of the ordinary
            # contradiction_superseded/polarity_conflict treatment.
            recency_bypassed_incumbents: tuple[GraphRelationship, ...] = ()
            # WS-16 T13: surviving incumbent to dispute-discount when the
            # challenger is parked by the gate.
            disputed_incumbent: GraphRelationship | None = None
            if relationship_instruction.cardinality == RelationshipCardinality.SINGLE_ACTIVE:
                active_contradictions = [
                    relationship
                    for relationship in self._graph.find_active_truth_relationships(
                        stored_truth_key, scope_key=scope.key
                    )
                    if not self._same_stored_object(
                        relationship,
                        new_object=memory.object,
                        new_object_commitment=new_object_commitment,
                    )
                ]
                historical_successor = self._historical_successor_relationship(
                    scope_key=scope.key,
                    truth_key=stored_truth_key,
                    new_object=memory.object,
                    new_object_commitment=new_object_commitment,
                    observed_at=observed_at,
                )
                if historical_successor is not None:
                    relationship_status = RelationshipStatus.SUPERSEDED
                    relationship_valid_to = self._earliest_valid_to(
                        memory.valid_to,
                        historical_successor.valid_from,
                    )
                    relationship_status_properties = {
                        "superseded_at": datetime.now(UTC).isoformat(),
                        "superseded_by_relationship_uuid": historical_successor.uuid,
                    }
                else:
                    gate_decision = self._contradiction_decision(
                        scope_key=scope.key,
                        contradictions=active_contradictions,
                        candidate_authority=source_authority,
                        severity_score=severity_score,
                        temporary=memory.valid_to is not None,
                        truth_prefix=stored_truth_prefix,
                        new_object=memory.object,
                        new_object_commitment=new_object_commitment,
                        new_object_embedding=object_embedding,
                        memory_type=relationship_instruction.memory_type.value,
                        dedup_threshold_override=dedup_threshold_override,
                        claim_mode=claim_mode,
                        directive_stance=directive_stance,
                        semantic_polarity=semantic_polarity,
                        protection=protection,
                        effective_from=recency_effective_from,
                    )
                    if gate_decision.gated:
                        relationship_status = RelationshipStatus.SUPERSEDED
                        relationship_valid_to = observed_at
                        relationship_status_properties = {
                            "superseded_at": datetime.now(UTC).isoformat(),
                            "superseded_by_relationship_uuid": gate_decision.gated_incumbent.uuid,
                            "supersession_gate_reason": gate_decision.gate_reason,
                            "requires_operator_review": True,
                        }
                        disputed_incumbent = gate_decision.gated_incumbent
                        # WS-17 T16b: the slot now HOLDS contradictory truths (an
                        # active incumbent plus a gate-parked challenger — a
                        # standing dispute, not a clean supersession).  When the
                        # two sides were stated under DIFFERENT surface forms of
                        # the canonical subject, that dispute is live evidence
                        # AGAINST the bridging alias — discount it exactly where
                        # WS-16 discounts the disputed incumbent.
                        self._discount_entity_aliases_on_conflict(
                            scope=scope,
                            canonical_subject=memory.subject,
                            current_surface=subject_surface,
                            conflicting_incumbents=active_contradictions,
                            challenger_confidence=memory.confidence,
                            receipt_run=receipt_run,
                            episode=episode,
                            now=now,
                            motive_name=motive_name,
                        )
                    else:
                        supersede_after_insert = True
                        corroborated_parked_siblings = gate_decision.corroborated_parked_siblings
                        recency_bypassed_incumbents = gate_decision.recency_bypassed_incumbents
                        if memory.valid_to is not None and active_contradictions:
                            relationship_status_properties = {
                                "temporary_predecessor_relationship_uuids": [
                                    relationship.uuid for relationship in active_contradictions
                                ],
                                "temporary_predecessor_valid_to": {
                                    relationship.uuid: (
                                        relationship.valid_to.isoformat() if relationship.valid_to is not None else None
                                    )
                                    for relationship in active_contradictions
                                },
                                "temporary_restore_at": memory.valid_to.isoformat(),
                            }
            # WS-1: try semantic reinforce before creating a new row.
            # new_embedding is the object-only embedding for dedup comparison
            # (candidates share subject+predicate; object is the discriminating signal).
            # Returns (reinforced_relationship, write_verb, cosine_score, matched_uuid).
            # WS-3: dedup_threshold_override lets a Motive set its own threshold.
            dedup_mt = (
                relationship_instruction.memory_type.value if relationship_instruction.memory_type is not None else None
            )
            # WS-11: the effective dedup threshold this candidate was gated against
            # (Motive override > per-type DedupPolicy) — recorded on the receipt.
            threshold_used = (
                dedup_threshold_override
                if dedup_threshold_override is not None
                else self._config.dedup.threshold_for(dedup_mt)
            )
            # WS-17 T16b: the digest identity is the PRE-resolution candidate
            # (digest_memory) — CANDIDATE_EXTRACTED recorded that form, and the
            # replay fold matches dispositions to candidates by this digest.
            candidate_receipt_digest = candidate_digest(
                digest_memory,
                memory_type=dedup_mt,
                episode_uuid=episode.uuid,
                content_key=protection.key if protection is not None else None,
            )
            candidate_authority_class = (
                "non_normative" if episode.metadata.get("artifact_class") == "generated_output" else "memory"
            )
            (
                reinforce_target,
                write_verb,
                semantic_cosine,
                semantic_matched_uuid,
                dedup_guard_refusals,
            ) = self._find_reinforce_target(
                scope_key=scope.key,
                truth_prefix=stored_truth_prefix,
                truth_key=stored_truth_key,
                new_object=memory.object,
                new_object_commitment=new_object_commitment,
                new_embedding=object_embedding,
                memory_type=dedup_mt,
                dedup_threshold_override=dedup_threshold_override,
                protection=protection,
                authority_class=candidate_authority_class,
                claim_mode=claim_mode,
                directive_stance=directive_stance,
                semantic_polarity=semantic_polarity,
                source_authority=source_authority,
                # WS-28 T3: a runbook-capture candidate's verbatim command
                # must never be paraphrase-merged into a different command.
                force_identifier_conflict=bool(memory.metadata.get("runbook_capture")),
            )
            self._add_mentions(
                episode_node.uuid,
                subject_node.uuid,
                episode.reference_time,
                episode.scope.key,
                created_by,
            )
            self._add_mentions(
                episode_node.uuid,
                object_node.uuid,
                episode.reference_time,
                episode.scope.key,
                created_by,
            )
            if reinforce_target is not None:
                # WS-11/WS-12: the reinforce mutates observed_count / last_seen /
                # confidence — all state-hash inputs — so the receipt brackets the
                # mutation with per-mutation graph-state hashes exactly like a create
                # (claim 4; byte replay folds reinforced receipts too).
                reinforce_before = self._graph.graph_state_hash(scope.key)
                reinforced_relationship = self._apply_reinforce(reinforce_target, memory, episode, observed_at)
                reinforce_after = self._graph.graph_state_hash(scope.key)
                reinforced_relationships += 1
                # WS-11: dedup merge disposition — BOTH exact-update and semantic-update
                # paths receipt FORMATION_SEMANTIC_DEDUP_REINFORCED ([0021]; closes the
                # exact-update audit gap).  cosine is 1.0 for an exact-string match.
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_SEMANTIC_DEDUP_REINFORCED,
                    decision_reason=("exact_update" if write_verb == "EXACT_UPDATE" else "semantic_update"),
                    decision_result="reinforced",
                    episode=episode,
                    now=now,
                    scope_key=scope.key,
                    candidate_digest=candidate_receipt_digest,
                    motive_name=motive_name,
                    memory_type=dedup_mt,
                    claim_mode=claim_mode.value,
                    directive_stance=directive_stance.value,
                    relationship_type=memory.relationship_type,
                    truth_key=stored_truth_key,
                    dedup_threshold=threshold_used,
                    dedup_score=semantic_cosine,
                    dedup_match_relationship_uuid=semantic_matched_uuid,
                    relationship_uuid=reinforced_relationship.uuid,
                    graph_state_hash_before=reinforce_before,
                    graph_state_hash_after=reinforce_after,
                    governance_policy_digest=governance_policy_digest,
                )
                # WS-1 auditability: record semantic reinforce decision when the merge
                # was triggered by embedding similarity (not exact string match).
                if write_verb == "SEMANTIC_UPDATE" and job is not None and now is not None:
                    self._record_semantic_reinforce(
                        job=job,
                        now=now,
                        fact=protection.seal(fact) if protection is not None else fact,
                        scope=scope,
                        episode_uuid=episode.uuid,
                        reinforced_uuid=reinforced_relationship.uuid,
                        cosine=semantic_cosine,
                        matched_uuid=semantic_matched_uuid,
                    )
                # Rollups model derived content, but corroboration is still
                # meaningful evidence.  Update their separate evidence plane;
                # only a policy-defined material delta invalidates the view.
                self._record_rollup_reinforcement(
                    relationship=reinforced_relationship,
                    now=now or observed_at,
                    receipt_run=receipt_run,
                )
                if self_promote_raw_candidates:
                    self._mark_raw_candidate_promoted(
                        digest=candidate_receipt_digest,
                        now=now,
                        relationship_uuid=reinforced_relationship.uuid,
                    )
                continue
            # WS-16 T15: a MULTI_ACTIVE candidate that did NOT reinforce may still
            # contradict an active member of its truth slot — a negated restatement
            # ("dark mode" vs "does not like dark mode") must not silently coexist.
            # Run the SAME gate + supersede + corroboration machinery as
            # SINGLE_ACTIVE, scoped to ONLY the polarity-conflicting row(s);
            # non-conflicting multi-active members coexist unchanged.
            if (
                relationship_instruction.cardinality == RelationshipCardinality.MULTI_ACTIVE
                and relationship_status == RelationshipStatus.ACTIVE
            ):
                polarity_conflicts = self._find_polarity_conflicts(
                    scope_key=scope.key,
                    truth_prefix=stored_truth_prefix,
                    new_object=memory.object,
                    new_object_commitment=new_object_commitment,
                    new_object_embedding=object_embedding,
                    semantic_polarity=semantic_polarity,
                    memory_type=relationship_instruction.memory_type.value,
                    dedup_threshold_override=dedup_threshold_override,
                    protection=protection,
                )
                if polarity_conflicts:
                    historical_conflicts = sorted(
                        (
                            conflict
                            for conflict in polarity_conflicts
                            if conflict.valid_from is not None and conflict.valid_from > observed_at
                        ),
                        key=lambda conflict: conflict.valid_from or conflict.created_at,
                    )
                    if historical_conflicts:
                        # Backfilled older statement: historical, cannot displace
                        # its newer polarity successor (mirrors single-active).
                        successor = historical_conflicts[0]
                        relationship_status = RelationshipStatus.SUPERSEDED
                        relationship_valid_to = self._earliest_valid_to(memory.valid_to, successor.valid_from)
                        relationship_status_properties = {
                            "superseded_at": datetime.now(UTC).isoformat(),
                            "superseded_by_relationship_uuid": successor.uuid,
                        }
                    else:
                        gate_decision = self._contradiction_decision(
                            scope_key=scope.key,
                            contradictions=polarity_conflicts,
                            candidate_authority=source_authority,
                            severity_score=severity_score,
                            temporary=memory.valid_to is not None,
                            truth_prefix=stored_truth_prefix,
                            new_object=memory.object,
                            new_object_commitment=new_object_commitment,
                            new_object_embedding=object_embedding,
                            memory_type=relationship_instruction.memory_type.value,
                            dedup_threshold_override=dedup_threshold_override,
                            claim_mode=claim_mode,
                            directive_stance=directive_stance,
                            semantic_polarity=semantic_polarity,
                            protection=protection,
                            effective_from=recency_effective_from,
                        )
                        if gate_decision.gated:
                            relationship_status = RelationshipStatus.SUPERSEDED
                            relationship_valid_to = observed_at
                            relationship_status_properties = {
                                "superseded_at": datetime.now(UTC).isoformat(),
                                "superseded_by_relationship_uuid": gate_decision.gated_incumbent.uuid,
                                "supersession_gate_reason": gate_decision.gate_reason,
                                "requires_operator_review": True,
                            }
                            disputed_incumbent = gate_decision.gated_incumbent
                            # WS-17 T16b: a gate-parked polarity dispute across
                            # the alias boundary discounts the implicated link
                            # (same rule as single-active contradictions).
                            self._discount_entity_aliases_on_conflict(
                                scope=scope,
                                canonical_subject=memory.subject,
                                current_surface=subject_surface,
                                conflicting_incumbents=polarity_conflicts,
                                challenger_confidence=memory.confidence,
                                receipt_run=receipt_run,
                                episode=episode,
                                now=now,
                                motive_name=motive_name,
                            )
                        else:
                            polarity_conflict_targets = polarity_conflicts
                            corroborated_parked_siblings = gate_decision.corroborated_parked_siblings
                            recency_bypassed_incumbents = gate_decision.recency_bypassed_incumbents
            # WS-12: content-plane protection — under crypto-shred governance the
            # fact/object/source_text and content-derived embeddings are stored ONLY
            # as sealed AES-256-GCM ciphertext under the scope DEK; the keyed
            # ``fact_commitment`` binds the plaintext into graph_state_hash without
            # storing it, so state hashes (and byte replay) survive key destruction.
            content_plane: dict[str, object]
            if protection is not None:
                content_plane = {
                    "fact": protection.seal(fact),
                    "object": protection.seal(memory.object),
                    "source_text": protection.seal_optional(memory.source_text),
                    "embedding": protection.seal_vector(fact_embedding),
                    "object_embedding": protection.seal_vector(object_embedding),
                    "fact_commitment": protection.commit("fact", fact),
                    "object_commitment": new_object_commitment,
                }
            else:
                content_plane = {
                    "fact": fact,
                    "object": memory.object,
                    "source_text": memory.source_text,
                    # WS-1: embedding substrate.
                    # "embedding"        — full fact (subject+predicate+object) vector.
                    #                      WS-4 clustering and WS-9 multimodal reuse
                    #                      this via relationship.properties["embedding"].
                    # "object_embedding" — object-only vector used by the semantic
                    #                      dedup path in _find_reinforce_target.
                    "embedding": fact_embedding,
                    "object_embedding": object_embedding,
                }
            # WS-17 T18: name the vector space next to the stored vectors.  The
            # identifier is algorithm provenance (lineage plane, like relationship
            # types and predicates), never content — it stays plaintext under
            # crypto-shred so the read-side space guard works over ciphertext rows.
            content_plane["embedding_identifier"] = self._embedding_transport.identifier

            # WS-27 T3: the dual-representation invariant, checked on the RAW
            # (pre-seal) triple/vector so it applies identically whether or
            # not this scope is content-protected — a sealed value is opaque
            # ciphertext and cannot be inspected for "empty", but the
            # plaintext inputs above already determined what got sealed.
            assert_dual_representation_invariant(
                subject=memory.subject,
                predicate=memory.predicate,
                object_text=memory.object,
                fact_embedding=fact_embedding,
            )

            # WS-26 T1/T2: every version is tagged with the epoch it was
            # produced under and the run that produced it.  A dedicated
            # branched-recompute engine pins ``_epoch_override``; the live
            # engine resolves (and lazily creates) the scope's current HEAD
            # epoch, so an untouched scope always writes into its one epoch.
            write_epoch_id = self._epoch_override or self._graph.ensure_root_epoch(scope.key, now=now or observed_at)
            # WS-11: per-mutation graph-state hashes bracket the create (claim 4).
            state_before = self._graph.graph_state_hash(scope.key)
            sensitive_text, sensitive_encrypted = self._receipt_sensitive_payload(fact=fact, protection=protection)
            relationship = self._graph.add_relationship(
                source_uuid=subject_node.uuid,
                target_uuid=object_node.uuid,
                relationship_type=memory.relationship_type,
                properties={
                    **content_plane,
                    "predicate": memory.predicate,
                    # WS-17 T16: the canonical form the truth keys were built on
                    # (absent when canonicalization is disabled — legacy shape).
                    **(
                        {"predicate_canonical": canonical_predicate}
                        if self._config.predicate_canonicalization.enabled
                        else {}
                    ),
                    # WS-17 T16b: the mention's surface forms, preserved when an
                    # entity-alias resolution changed the materialized names.
                    **entity_surface_properties,
                    "confidence": memory.confidence,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                    "truth_key": stored_truth_key,
                    "truth_prefix": stored_truth_prefix,
                    "truth_cardinality": relationship_instruction.cardinality.value,
                    "status": relationship_status.value,
                    "memory_type": relationship_instruction.memory_type.value,
                    "claim_mode": claim_mode.value,
                    "directive_stance": directive_stance.value,
                    "relationship_type": memory.relationship_type,
                    "episode_uuid": episode.uuid,
                    "episode_uuids": [episode.uuid],
                    "observed_count": 1,
                    "first_seen_at": observed_at.isoformat(),
                    "last_seen_at": observed_at.isoformat(),
                    "confidence_strategy": "evidence_accumulation",
                    "instruction_id": memory.instruction_id,
                    "metadata": {**episode.metadata, **memory.metadata},
                    "created_by": created_by,
                    # WS-26 T1/T2: derivation-DAG provenance — which run
                    # produced this version and which epoch it belongs to.
                    "produced_by_run": receipt_run.run_uuid,
                    "epoch_id": write_epoch_id,
                    "motive_name": motive_name,
                    # Version-pins the exact Motive that formed this row; a
                    # same-named Motive edited later has a different digest.
                    "motive_version_digest": motive_version_digest_value,
                    # WS-19 T20: first-class promotion lineage — a fact formed
                    # from a promotion candidate episode records exactly which
                    # source row (and scope) it was promoted from, queryable
                    # without parsing metadata (mirrors motive_version_digest).
                    **(
                        {
                            "promoted_from_relationship_uuid": str(episode.metadata["source_relationship_uuid"]),
                            "promoted_from_scope_key": str(episode.metadata.get("source_scope_key", "")),
                        }
                        if episode.metadata.get("promotion") is True
                        and episode.metadata.get("source_relationship_uuid")
                        else {}
                    ),
                    "secret_reference": secret_reference,
                    "secret_lifecycle": secret_lifecycle,
                    "authority_class": candidate_authority_class,
                    "source_authority": source_authority.value,
                    "authority_rank": authority_rank,
                    "severity_score": severity_score,
                    "semantic_polarity": semantic_polarity,
                    # WS-2: persist salience_score so WS-6 can report on it.
                    # 0.0 when the no-op path was active (salience was never computed).
                    "salience_score": memory.salience_score,
                    # WS-24: the step this fact saves, as the extractor stated
                    # it.  Kept next to the fact it admitted so the gate's
                    # judgement is auditable at the row, not only in the ledger.
                    # Content plane — sealed for a protected scope like `fact`.
                    **(
                        {
                            "saves_step": (
                                protection.seal(memory.saves_step) if protection is not None else memory.saves_step
                            )
                        }
                        if memory.saves_step
                        else {}
                    ),
                    **relationship_status_properties,
                },
                valid_from=observed_at,
                valid_to=relationship_valid_to,
            )
            if relationship.uuid:
                created_relationships += 1
                state_after = self._graph.graph_state_hash(scope.key)
                # WS-17 T17b: surface an identifier-guard refusal on the create
                # disposition — the row exists BECAUSE the guard refused an
                # above-threshold semantic merge, and the receipt records the
                # refused candidate + its cosine against the effective threshold.
                guard_refusal_fields: dict[str, object] = {}
                created_reason = "materialized"
                if dedup_guard_refusals:
                    strongest_refusal = max(
                        dedup_guard_refusals,
                        key=lambda refusal: (refusal["cosine"], refusal["relationship_uuid"]),
                    )
                    created_reason = "materialized:identifier_token_mismatch"
                    guard_refusal_fields = {
                        "dedup_threshold": threshold_used,
                        "dedup_score": strongest_refusal["cosine"],
                        "dedup_match_relationship_uuid": strongest_refusal["relationship_uuid"],
                    }
                # WS-11: relationship-created disposition (claim 4).
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
                    decision_reason=created_reason,
                    decision_result="materialized",
                    episode=episode,
                    now=now,
                    scope_key=scope.key,
                    candidate_digest=candidate_receipt_digest,
                    motive_name=motive_name,
                    memory_type=relationship_instruction.memory_type.value
                    if relationship_instruction.memory_type is not None
                    else None,
                    claim_mode=claim_mode.value,
                    directive_stance=directive_stance.value,
                    relationship_type=memory.relationship_type,
                    truth_key=stored_truth_key,
                    relationship_uuid=relationship.uuid,
                    graph_state_hash_before=state_before,
                    graph_state_hash_after=state_after,
                    **guard_refusal_fields,
                    governance_policy_digest=governance_policy_digest,
                    sensitive_payload=sensitive_text,
                    sensitive_payload_encrypted=sensitive_encrypted,
                )
                if self_promote_raw_candidates:
                    self._mark_raw_candidate_promoted(
                        digest=candidate_receipt_digest,
                        now=now,
                        relationship_uuid=relationship.uuid,
                    )
                # WS-11: historical-successor supersession — the new row is inserted
                # already SUPERSEDED by a later-valid row ([0021]).  The insert itself
                # was the mutation (bracketed by the create receipt), so this receipt
                # records a zero-delta transition (before == after == state_after) to
                # keep the per-scope state-hash chain contiguous for byte replay.
                historical_successor_uuid = relationship_status_properties.get("superseded_by_relationship_uuid")
                if historical_successor_uuid is not None:
                    gate_reason = relationship_status_properties.get("supersession_gate_reason")
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED,
                        decision_reason=(
                            str(gate_reason) if gate_reason is not None else "historical_successor_insert"
                        ),
                        decision_result="superseded",
                        episode=episode,
                        now=now,
                        scope_key=scope.key,
                        memory_type=relationship_instruction.memory_type.value
                        if relationship_instruction.memory_type is not None
                        else None,
                        claim_mode=claim_mode.value,
                        directive_stance=directive_stance.value,
                        relationship_type=memory.relationship_type,
                        truth_key=stored_truth_key,
                        relationship_uuid=relationship.uuid,
                        superseded_relationship_uuid=relationship.uuid,
                        successor_relationship_uuid=str(historical_successor_uuid),
                        graph_state_hash_before=state_after,
                        graph_state_hash_after=state_after,
                        governance_policy_digest=governance_policy_digest,
                    )
                superseded_transitions: list[tuple[str, str, str]] = []
                reason_by_uuid: dict[str, str] = {}
                supersede_targets: list[GraphRelationship] = []
                normal_reason = "contradiction_superseded"
                if supersede_after_insert:
                    supersede_targets = active_contradictions
                elif polarity_conflict_targets:
                    # WS-16 T15: supersede ONLY the polarity-conflicting rows of
                    # the multi-active slot; other members coexist unchanged.
                    supersede_targets = polarity_conflict_targets
                    normal_reason = "polarity_conflict"
                if supersede_targets:
                    # WS-25 T2: an incumbent the recency-authoritative bypass
                    # overrode gets ITS OWN valid_to (clamped to the
                    # candidate's effective time, not the raw observed_at) and
                    # a recency_authoritative_supersede receipt; every other
                    # target keeps the ordinary treatment unchanged.
                    bypass_uuids = {incumbent.uuid for incumbent in recency_bypassed_incumbents}
                    bypass_targets = [target for target in supersede_targets if target.uuid in bypass_uuids]
                    normal_targets = [target for target in supersede_targets if target.uuid not in bypass_uuids]
                    if bypass_targets:
                        bypass_transitions = self._supersede_target_relationships(
                            targets=bypass_targets,
                            valid_to=recency_effective_from,
                            superseded_by_relationship_uuid=relationship.uuid,
                            scope_key=scope.key,
                            superseded_by_run=receipt_run.run_uuid,
                        )
                        superseded_transitions += bypass_transitions
                        reason_by_uuid.update(
                            {
                                superseded_uuid: "recency_authoritative_supersede"
                                for superseded_uuid, _, _ in bypass_transitions
                            }
                        )
                    if normal_targets:
                        normal_transitions = self._supersede_target_relationships(
                            targets=normal_targets,
                            valid_to=observed_at,
                            superseded_by_relationship_uuid=relationship.uuid,
                            scope_key=scope.key,
                            superseded_by_run=receipt_run.run_uuid,
                        )
                        superseded_transitions += normal_transitions
                        reason_by_uuid.update(
                            {superseded_uuid: normal_reason for superseded_uuid, _, _ in normal_transitions}
                        )
                superseded_relationships += len(superseded_transitions)
                for superseded_uuid, supersede_before, supersede_after in superseded_transitions:
                    supersession_reason = reason_by_uuid.get(superseded_uuid, normal_reason)
                    # WS-11: truth-key supersession disposition ([0021]) — each
                    # superseded row is bracketed by its OWN per-mutation state
                    # hashes so the per-scope chain stays contiguous for replay.
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED,
                        decision_reason=supersession_reason,
                        decision_result="superseded",
                        episode=episode,
                        now=now,
                        scope_key=scope.key,
                        memory_type=relationship_instruction.memory_type.value
                        if relationship_instruction.memory_type is not None
                        else None,
                        claim_mode=claim_mode.value,
                        directive_stance=directive_stance.value,
                        relationship_type=memory.relationship_type,
                        truth_key=stored_truth_key,
                        relationship_uuid=superseded_uuid,
                        superseded_relationship_uuid=superseded_uuid,
                        successor_relationship_uuid=relationship.uuid,
                        graph_state_hash_before=supersede_before,
                        graph_state_hash_after=supersede_after,
                        governance_policy_digest=governance_policy_digest,
                    )
                    self.invalidate_rollups_for_dependency(
                        relationship_uuid=superseded_uuid,
                        reason=supersession_reason,
                        now=now or observed_at,
                        receipt_run=receipt_run,
                    )
                    # WS-17 T17: duplicates parked behind the superseded row
                    # return to context — no invisible orphans.
                    self.repromote_duplicates_for_dependency(
                        relationship_uuid=superseded_uuid,
                        reason=supersession_reason,
                        now=now or observed_at,
                        receipt_run=receipt_run,
                    )
                # WS-16 T12: the corroboration escape flipped the slot — resolve
                # every counted parked sibling onto the new active winner.
                if superseded_transitions and corroborated_parked_siblings:
                    self._resolve_corroborated_challengers(
                        receipt_run=receipt_run,
                        episode=episode,
                        now=now,
                        siblings=corroborated_parked_siblings,
                        winner_uuid=relationship.uuid,
                        scope_key=scope.key,
                        truth_key=stored_truth_key,
                        memory_type=relationship_instruction.memory_type.value,
                        claim_mode=claim_mode,
                        directive_stance=directive_stance,
                        relationship_type=memory.relationship_type,
                        governance_policy_digest=governance_policy_digest,
                    )
                # WS-16 T13: a gate-parked challenger disputes the surviving
                # ACTIVE incumbent — discount its confidence once per park.
                if disputed_incumbent is not None:
                    self._apply_incumbent_dispute_discount(
                        receipt_run=receipt_run,
                        episode=episode,
                        now=now,
                        incumbent_uuid=disputed_incumbent.uuid,
                        challenger_relationship_uuid=relationship.uuid,
                        challenger_confidence=memory.confidence,
                        scope_key=scope.key,
                        truth_key=stored_truth_key,
                        memory_type=relationship_instruction.memory_type.value,
                        claim_mode=claim_mode,
                        directive_stance=directive_stance,
                        relationship_type=memory.relationship_type,
                        governance_policy_digest=governance_policy_digest,
                    )
        return created_nodes, created_relationships, reinforced_relationships, superseded_relationships
