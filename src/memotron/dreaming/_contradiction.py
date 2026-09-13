"""Deciding what a new claim does to the claims already stored.

Five outcomes, and the whole file exists to keep them distinguishable: the claim
reinforces an incumbent, supersedes it, is superseded BY it, sits in unresolved
dispute with it, or is simply about something else. Everything here is the
machinery for telling those apart -- polarity, object identity, statement
compatibility, temporal successor lookup and the corroboration counts that let a
parked challenger eventually win.

Nothing here deletes. Supersession sets valid_to and writes a successor; the
predecessor stays readable at its own truth time. That is the invariant the
bitemporal reads depend on, and it is why _earliest_valid_to and
_historical_successor_relationship are small and dull rather than clever."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from memotron.coherence import stances_conflict
from memotron.config import (
    DreamJob,
)
from memotron.crypto import (
    is_sealed_content,
)
from memotron.dreaming._common import _ContentProtection
from memotron.dreaming._identity import _TruthGateDecision
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.dreaming._text import identifier_tokens_conflict
from memotron.embedding import (
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.graph import normalize_key
from memotron.models import (
    ClaimMode,
    DirectiveStance,
    DreamDecisionRecord,
    Episode,
    ExtractedMemory,
    GraphRelationship,
    MemoryAuthority,
    MemoryScope,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    payload_digest,
)
from memotron.retrieval import (
    contradictory_object,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class ContradictionMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    def _add_mentions(
        self,
        episode_node_uuid: str,
        entity_node_uuid: str,
        reference_time: datetime,
        scope_key: str,
        created_by: str,
    ) -> None:
        self._graph.add_relationship(
            source_uuid=episode_node_uuid,
            target_uuid=entity_node_uuid,
            relationship_type="MENTIONS",
            properties={
                "status": RelationshipStatus.ACTIVE.value,
                "scope_key": scope_key,
                "created_by": created_by,
            },
            valid_from=reference_time,
        )

    def _same_stored_object(
        self,
        relationship: GraphRelationship,
        *,
        new_object: str,
        new_object_commitment: str | None,
    ) -> bool:
        """Object-slot equality against a stored row — over ciphertext when protected.

        WS-12: a crypto-shred row carries an ``object_commitment`` (keyed HMAC blind
        index); equality is commitment equality, never a decrypt.  Plaintext rows
        keep the normalized string comparison.
        """
        stored_commitment = relationship.properties.get("object_commitment")
        if isinstance(stored_commitment, str) and stored_commitment:
            return new_object_commitment is not None and stored_commitment == new_object_commitment
        return normalize_key(str(relationship.properties.get("object", ""))) == normalize_key(new_object)

    def _supersede_target_relationships(
        self,
        *,
        targets: list[GraphRelationship],
        valid_to: datetime,
        superseded_by_relationship_uuid: str,
        scope_key: str,
        superseded_by_run: str | None = None,
    ) -> list[tuple[str, str, str]]:
        """Supersede exactly *targets*; return (uuid, state_before, state_after) per row.

        Shared by the single-active truth-key sweep and the WS-16 T15
        polarity-conflict path (which supersedes only the conflicting rows of a
        multi-active slot).  Each row is bracketed by its own per-mutation
        graph-state hashes for the contiguous replay chain (claim 4).

        ``superseded_by_run`` (WS-26 T1): the run that made this row give way,
        so "everything run R did" is traversable from the run outward, not
        only from a fact back to the run that created it.
        """
        superseded: list[tuple[str, str, str]] = []
        for relationship in targets:
            state_before = self._graph.graph_state_hash(scope_key)
            self._graph.mark_relationship(
                relationship.uuid,
                status=RelationshipStatus.SUPERSEDED,
                valid_to=valid_to,
                properties={
                    "superseded_at": datetime.now(UTC).isoformat(),
                    "superseded_by_relationship_uuid": superseded_by_relationship_uuid,
                    **({"superseded_by_run": superseded_by_run} if superseded_by_run is not None else {}),
                },
            )
            state_after = self._graph.graph_state_hash(scope_key)
            superseded.append((relationship.uuid, state_before, state_after))
        return superseded

    def _historical_successor_relationship(
        self,
        *,
        scope_key: str,
        truth_key: str,
        new_object: str,
        new_object_commitment: str | None,
        observed_at: datetime,
    ) -> GraphRelationship | None:
        candidates: list[GraphRelationship] = []
        for relationship in self._graph.find_active_truth_relationships(truth_key, scope_key=scope_key):
            if self._same_stored_object(
                relationship, new_object=new_object, new_object_commitment=new_object_commitment
            ):
                continue
            if relationship.valid_from is None:
                continue
            if relationship.valid_from > observed_at:
                candidates.append(relationship)
        if not candidates:
            return None
        candidates.sort(key=lambda relationship: relationship.valid_from or relationship.created_at)
        return candidates[0]

    def _earliest_valid_to(
        self, current_valid_to: datetime | None, successor_valid_from: datetime | None
    ) -> datetime | None:
        if current_valid_to is None:
            return successor_valid_from
        if successor_valid_from is None:
            return current_valid_to
        return min(current_valid_to, successor_valid_from)

    def _find_reinforce_target(
        self,
        *,
        scope_key: str,
        truth_prefix: str,
        truth_key: str,
        new_object: str,
        new_object_commitment: str | None,
        new_embedding: list[float],
        memory_type: str | None,
        dedup_threshold_override: float | None = None,
        protection: _ContentProtection | None = None,
        authority_class: str = "memory",
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
        source_authority: MemoryAuthority,
        force_identifier_conflict: bool = False,
    ) -> tuple[GraphRelationship | None, str, float, str | None, list[dict[str, object]]]:
        """Find the existing active relationship this fact should reinforce (pure find).

        WS-1 semantic dedup strategy:
        1. First try an exact object match against the narrow truth_key set —
           commitment equality for a WS-12 protected row, normalized-string
           equality otherwise (cosine ≈ 1.0 always satisfies the threshold).
        2. If no exact match, broaden the candidate set to all active rows sharing
           the same truth_prefix (scope:subject:predicate).  Compute cosine between
           the new object embedding and each candidate's stored embedding —
           revealed under the live scope DEK when stored sealed.  Pick the
           highest-cosine candidate at or above the per-type threshold.

        WS-28 T3: ``force_identifier_conflict`` (default ``False``, byte-identical
        for every existing caller) makes pass 2 treat EVERY candidate as an
        identifier conflict, regardless of the regex-based token detector —
        i.e. semantic dedup never merges two DIFFERENT statements into one
        row.  Pass 1's exact-object match is untouched, so a genuine repeat of
        the SAME statement still reinforces normally.  Set by the runbook
        capture write path (``memory.metadata["runbook_capture"]``) so a
        verbatim command differing by even a flag or path segment — which may
        not carry the token shape :func:`identifier_tokens` recognizes — can
        never be paraphrase-merged into a different command's truth row: a
        runbook line is an identifier by policy, not only when the regex says so.

        WS-17 T17b identifier guard (pass 2 only): a candidate whose stored
        object text and the incoming object text BOTH carry identifier-like
        tokens with DIFFERING sets is REFUSED regardless of cosine — the
        measured ``kb_ds_source_..._wdw_...`` vs ``..._dlr_...`` pair reaches
        0.895 under the hermetic transport, above the 0.88 default threshold,
        yet names two distinct data stores.  The exact-object path (pass 1) is
        unaffected: an exact match carries the identical identifiers by
        definition.  Refusals that would otherwise have cleared the threshold
        ride back to the caller for receipt surfacing.

        The mutation itself is applied by the caller (:meth:`_apply_reinforce`)
        so it can be bracketed by per-mutation graph-state hashes (claim 4).

        Returns (target_relationship | None, write_verb, cosine_score,
        matched_uuid, guard_refusals):
            write_verb ∈ {"EXACT_UPDATE", "SEMANTIC_UPDATE", "ADD"};
            guard_refusals — above-threshold candidates the identifier guard
            refused, each ``{"relationship_uuid", "cosine"}``.

        WS-3: dedup_threshold_override — when set (from a Motive), overrides the
        per-type DedupPolicy threshold for this materialization call.
        """
        # WS-3: Motive threshold takes precedence over per-type DedupPolicy threshold.
        if dedup_threshold_override is not None:
            threshold = dedup_threshold_override
        else:
            threshold = self._config.dedup.threshold_for(memory_type)

        # --- Pass 1: exact object match (same as pre-WS-1; ciphertext-safe) ---
        for relationship in self._graph.find_active_truth_relationships(truth_key, scope_key=scope_key):
            if relationship.properties.get("authority_class", "memory") != authority_class:
                continue
            if not self._same_stored_object(
                relationship, new_object=new_object, new_object_commitment=new_object_commitment
            ):
                continue
            return relationship, "EXACT_UPDATE", 1.0, relationship.uuid, []

        # --- Pass 2: semantic similarity across truth_prefix candidates ---
        # Compare new_embedding (object-only) against stored object_embedding on each
        # candidate row.  Candidates share subject+predicate (same truth_prefix), so
        # the object text is the discriminating signal for semantic dedup.
        best_cosine = 0.0
        best_candidate: GraphRelationship | None = None
        guard_refusals: list[dict[str, object]] = []
        for relationship in self._graph.find_active_relationships_by_truth_prefix(truth_prefix, scope_key=scope_key):
            if relationship.properties.get("authority_class", "memory") != authority_class:
                continue
            if not self._statement_compatible(
                relationship,
                claim_mode=claim_mode,
                directive_stance=directive_stance,
                semantic_polarity=semantic_polarity,
            ):
                continue
            existing_authority = self._relationship_authority(relationship)
            if existing_authority != source_authority:
                continue
            stored_embedding = self._stored_object_embedding(relationship, protection=protection)
            if stored_embedding is None:
                # Older row without an object_embedding — skip semantic check
                continue
            try:
                cosine = cosine_similarity(new_embedding, stored_embedding)
            except ValueError:
                # Dimension mismatch (e.g. transport changed) — skip silently
                continue
            # WS-17 T17b: differing identifier tokens refuse the semantic
            # match regardless of cosine (correctness invariant, not a knob).
            # WS-28 T3: force_identifier_conflict widens this to EVERY
            # candidate for a runbook-capture write (see docstring above).
            if force_identifier_conflict or self._stored_object_identifier_conflict(
                relationship, new_object=new_object
            ):
                if cosine >= threshold:
                    guard_refusals.append({"relationship_uuid": relationship.uuid, "cosine": cosine})
                continue
            if cosine > best_cosine:
                best_cosine = cosine
                best_candidate = relationship

        if best_candidate is not None and best_cosine >= threshold:
            return best_candidate, "SEMANTIC_UPDATE", best_cosine, best_candidate.uuid, guard_refusals

        return None, "ADD", 0.0, None, guard_refusals

    def _stored_object_identifier_conflict(self, relationship: GraphRelationship, *, new_object: str) -> bool:
        """WS-17 T17b: do the stored and incoming object texts carry DIFFERING
        identifier-like token sets?

        The stored object decrypts on read under a live DEK; a crypto-shredded
        row reveals only the placeholder (no identifier tokens — no conflict),
        which is safe because such rows also fail every text comparison.
        """
        stored_object = self._graph.reveal(
            str(relationship.properties.get("scope_key")),
            relationship.properties.get("object", ""),
        )
        if not isinstance(stored_object, str) or not stored_object:
            return False
        return identifier_tokens_conflict(new_object, stored_object)

    def _statement_compatible(
        self,
        relationship: GraphRelationship,
        *,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
    ) -> bool:
        """True when a stored row states a compatible claim (same claim mode,
        non-conflicting stance, same whole-fact polarity).

        Shared by semantic dedup (pass 2 of :meth:`_find_reinforce_target`) and
        the WS-16 T12 corroboration scan so a DIFFERENT challenger statement can
        never reinforce — or corroborate — this one.
        """
        existing_claim_mode = relationship.properties.get("claim_mode")
        if existing_claim_mode is not None and existing_claim_mode != claim_mode.value:
            return False
        existing_stance = relationship.properties.get("directive_stance")
        if isinstance(existing_stance, str):
            try:
                if stances_conflict(DirectiveStance(existing_stance), directive_stance):
                    return False
            except ValueError:
                return False
        return self._stored_semantic_polarity(relationship) == semantic_polarity

    def _stored_semantic_polarity(self, relationship: GraphRelationship) -> str:
        """Stored ``semantic_polarity``, recomputed from the (revealed) fact text
        for pre-polarity rows — decrypt-on-read under a live DEK (WS-12)."""
        existing_polarity = relationship.properties.get("semantic_polarity")
        if isinstance(existing_polarity, str):
            return existing_polarity
        existing_fact = str(
            self._graph.reveal(
                str(relationship.properties.get("scope_key")),
                relationship.properties.get(
                    "fact",
                    " ".join(str(relationship.properties.get(key, "")) for key in ("subject", "predicate", "object")),
                ),
            )
        )
        return self._semantic_polarity(existing_fact)

    def _stored_object_embedding(
        self,
        relationship: GraphRelationship,
        *,
        protection: _ContentProtection | None,
    ) -> list[float] | None:
        """Stored object-only embedding, revealed under the live scope DEK when
        sealed (WS-12); None when absent, unrevealable, or malformed.

        WS-17 T18 vector-space guard: the stored vector participates only when
        its stamped ``embedding_identifier`` matches the active transport — a
        mismatched or unstamped (legacy) vector is treated as absent, so
        semantic dedup degrades to exact-match-only instead of ever computing a
        cross-space cosine."""
        if not stored_vector_in_active_space(
            relationship.properties,
            active_identifier=self._embedding_transport.identifier,
        ):
            return None
        stored_embedding = relationship.properties.get("object_embedding")
        if is_sealed_content(stored_embedding) and protection is not None:
            # WS-12: sealed content-derived vector — reveal under the live DEK.
            from memotron.crypto import open_json

            stored_embedding = open_json(stored_embedding, protection.key)
        if not isinstance(stored_embedding, list) or not stored_embedding:
            return None
        return stored_embedding

    def _contradiction_decision(
        self,
        *,
        scope_key: str,
        contradictions: list[GraphRelationship],
        candidate_authority: MemoryAuthority,
        severity_score: int,
        temporary: bool,
        truth_prefix: str,
        new_object: str,
        new_object_commitment: str | None,
        new_object_embedding: list[float],
        memory_type: str | None,
        dedup_threshold_override: float | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
        protection: _ContentProtection | None,
        effective_from: datetime,
    ) -> _TruthGateDecision:
        """WS-16 T12 / WS-25 T2: run the supersession gate over one contradiction set.

        The corroboration scan (a graph read) runs only when at least one
        contradiction is corroboration-eligible — an EQUAL-rank incumbent whose
        ``observed_count`` has reached ``corroboration_margin``.  Gate precedence
        per incumbent is unchanged: lower authority, then temporary high
        severity, then insufficient corroboration — EXCEPT that a
        ``temporary_high_severity``/``insufficient_corroboration`` reason is
        overridden per-incumbent by the WS-25 T2 recency-authoritative bypass
        (never the ``lower_authority`` reason).  When nothing gates and the
        corroboration escape supplied the deciding votes, the counted parked
        siblings ride back on the decision for post-insert resolution; bypassed
        incumbents ride back separately so the caller can give them their own
        clamped ``valid_to`` and ``recency_authoritative_supersede`` receipt.
        """
        policy = self._config.supersession
        candidate_rank = policy.authority_rank(candidate_authority)
        corroboration_siblings: list[GraphRelationship] = []
        corroboration_total: int | None = None
        if any(
            policy.authority_rank(self._relationship_authority(incumbent)) == candidate_rank
            and int(incumbent.properties.get("observed_count", 1)) >= policy.corroboration_margin
            for incumbent in contradictions
        ):
            corroboration_siblings = self._corroborating_parked_challengers(
                scope_key=scope_key,
                truth_prefix=truth_prefix,
                new_object=new_object,
                new_object_commitment=new_object_commitment,
                new_object_embedding=new_object_embedding,
                memory_type=memory_type,
                dedup_threshold_override=dedup_threshold_override,
                claim_mode=claim_mode,
                directive_stance=directive_stance,
                semantic_polarity=semantic_polarity,
                protection=protection,
            )
            corroboration_total = len(corroboration_siblings) + 1
        gated_incumbents: list[tuple[GraphRelationship, str]] = []
        recency_bypassed: list[GraphRelationship] = []
        for incumbent in contradictions:
            reason = self._supersession_gate_reason(
                incumbent=incumbent,
                candidate_authority=candidate_authority,
                severity_score=severity_score,
                temporary=temporary,
                corroboration_total=corroboration_total,
            )
            if reason is None:
                continue
            if self._recency_authoritative_bypass(
                memory_type=memory_type,
                candidate_authority=candidate_authority,
                incumbent=incumbent,
                effective_from=effective_from,
            ):
                recency_bypassed.append(incumbent)
                continue
            gated_incumbents.append((incumbent, reason))
        if gated_incumbents:
            gated_incumbents.sort(
                key=lambda item: (
                    policy.authority_rank(self._relationship_authority(item[0])),
                    item[0].valid_from or item[0].created_at,
                ),
                reverse=True,
            )
            incumbent, reason = gated_incumbents[0]
            return _TruthGateDecision(
                gated=True,
                gate_reason=reason,
                gated_incumbent=incumbent,
            )
        resolved = (
            tuple(corroboration_siblings)
            if corroboration_total is not None and corroboration_total >= policy.corroboration_required
            else ()
        )
        return _TruthGateDecision(
            gated=False,
            gate_reason=None,
            gated_incumbent=None,
            corroborated_parked_siblings=resolved,
            recency_bypassed_incumbents=tuple(recency_bypassed),
        )

    def _corroborating_parked_challengers(
        self,
        *,
        scope_key: str,
        truth_prefix: str,
        new_object: str,
        new_object_commitment: str | None,
        new_object_embedding: list[float],
        memory_type: str | None,
        dedup_threshold_override: float | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        semantic_polarity: str,
        protection: _ContentProtection | None,
    ) -> list[GraphRelationship]:
        """WS-16 T12: previously parked observations of the SAME challenger statement.

        A corroborating row lives on the same truth slot (its ``truth_prefix``;
        equal to the ``truth_key`` for single-active slots), was inserted
        pre-SUPERSEDED by the corroboration gate (``requires_operator_review``
        with an ``insufficient_corroboration`` gate reason), and states the same
        challenger object — commitment/normalized equality (crypto-shred safe via
        :meth:`_same_stored_object`) or an object-embedding cosine at/above the
        effective dedup threshold with compatible claim mode, stance, and
        polarity so a DIFFERENT challenger never counts.
        """
        if dedup_threshold_override is not None:
            threshold = dedup_threshold_override
        else:
            threshold = self._config.dedup.threshold_for(memory_type)
        parked: list[GraphRelationship] = []
        for relationship in self._graph.relationships_for_truth_prefix(truth_prefix, scope_key=scope_key):
            if relationship.properties.get("status") != RelationshipStatus.SUPERSEDED.value:
                continue
            if relationship.properties.get("requires_operator_review") is not True:
                continue
            gate_reason = relationship.properties.get("supersession_gate_reason")
            if not isinstance(gate_reason, str) or not gate_reason.startswith("insufficient_corroboration"):
                continue
            if self._same_stored_object(
                relationship, new_object=new_object, new_object_commitment=new_object_commitment
            ):
                parked.append(relationship)
                continue
            if not self._statement_compatible(
                relationship,
                claim_mode=claim_mode,
                directive_stance=directive_stance,
                semantic_polarity=semantic_polarity,
            ):
                continue
            # WS-17 T17b: a parked row whose object carries DIFFERENT identifier
            # tokens states a different challenger — it can never corroborate
            # this one, however similar the surrounding text (exact-object
            # matches above already passed by identity).
            if self._stored_object_identifier_conflict(relationship, new_object=new_object):
                continue
            stored_embedding = self._stored_object_embedding(relationship, protection=protection)
            if stored_embedding is None:
                continue
            try:
                cosine = cosine_similarity(new_object_embedding, stored_embedding)
            except ValueError:
                continue
            if cosine >= threshold:
                parked.append(relationship)
        return parked

    def _find_polarity_conflicts(
        self,
        *,
        scope_key: str,
        truth_prefix: str,
        new_object: str,
        new_object_commitment: str | None,
        new_object_embedding: list[float],
        semantic_polarity: str,
        memory_type: str | None,
        dedup_threshold_override: float | None,
        protection: _ContentProtection | None,
    ) -> list[GraphRelationship]:
        """WS-16 T15: active truth-slot rows stating the SAME statement with the
        OPPOSITE whole-fact polarity.

        Conflict requires the same normalized subject+predicate (same
        ``truth_prefix`` by construction), opposite ``semantic_polarity``, and
        object similarity — an exact stored-object match after stripping
        negation markers, or an object-embedding cosine at/above the effective
        dedup threshold (same cosine machinery as :meth:`_find_reinforce_target`,
        including the crypto-shred reveal path).
        """
        if dedup_threshold_override is not None:
            threshold = dedup_threshold_override
        else:
            threshold = self._config.dedup.threshold_for(memory_type)
        conflicts: list[GraphRelationship] = []
        for relationship in self._graph.find_active_relationships_by_truth_prefix(truth_prefix, scope_key=scope_key):
            if self._same_stored_object(
                relationship, new_object=new_object, new_object_commitment=new_object_commitment
            ):
                continue
            stored_polarity = self._stored_semantic_polarity(relationship)
            if stored_polarity == semantic_polarity:
                continue
            stored_object = self._graph.reveal(
                str(relationship.properties.get("scope_key")),
                relationship.properties.get("object", ""),
            )
            # WS-25 T1: the shared same-slot contradiction predicate (also the
            # read-side within-slot tiebreaker's clustering key) — "prefers
            # dark mode" vs "prefers not dark mode" strip to the same object
            # with opposite polarity.
            if (
                isinstance(stored_object, str)
                and stored_object
                and contradictory_object(
                    first_object=new_object,
                    first_polarity=semantic_polarity,
                    second_object=stored_object,
                    second_polarity=stored_polarity,
                )
            ):
                conflicts.append(relationship)
                continue
            # WS-17 T17b: two identifier-bearing objects with DIFFERING token
            # sets are different statements — never a polarity conflict of the
            # SAME statement, however high the cosine (the negation-stripped
            # exact match above is unaffected: it matches by identity).
            if (
                isinstance(stored_object, str)
                and stored_object
                and identifier_tokens_conflict(new_object, stored_object)
            ):
                continue
            stored_embedding = self._stored_object_embedding(relationship, protection=protection)
            if stored_embedding is None:
                continue
            try:
                cosine = cosine_similarity(new_object_embedding, stored_embedding)
            except ValueError:
                continue
            if cosine >= threshold:
                conflicts.append(relationship)
        return conflicts

    def _resolve_corroborated_challengers(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        now: datetime | None,
        siblings: tuple[GraphRelationship, ...],
        winner_uuid: str,
        scope_key: str,
        truth_key: str,
        memory_type: str | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        relationship_type: str,
        governance_policy_digest: str | None,
    ) -> None:
        """WS-16 T12: resolve parked siblings counted by the corroboration escape.

        Each counted parked row is repointed at the winning active row with
        ``review_resolution="corroborated"`` and its operator-review flag
        cleared; every mutation is bracketed by its own graph-state hashes and
        receipted so the per-scope replay chain stays contiguous.
        """
        for sibling in siblings:
            state_before = self._graph.graph_state_hash(scope_key)
            self._graph.update_relationship(
                sibling.uuid,
                properties={
                    "superseded_by_relationship_uuid": winner_uuid,
                    "requires_operator_review": False,
                    "review_resolution": "corroborated",
                },
            )
            state_after = self._graph.graph_state_hash(scope_key)
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.FORMATION_TRUTH_KEY_SUPERSEDED,
                decision_reason="corroborated_challenger_flip",
                decision_result="superseded",
                episode=episode,
                now=now,
                scope_key=scope_key,
                memory_type=memory_type,
                claim_mode=claim_mode.value,
                directive_stance=directive_stance.value,
                relationship_type=relationship_type,
                truth_key=truth_key,
                relationship_uuid=sibling.uuid,
                superseded_relationship_uuid=sibling.uuid,
                successor_relationship_uuid=winner_uuid,
                graph_state_hash_before=state_before,
                graph_state_hash_after=state_after,
                governance_policy_digest=governance_policy_digest,
            )

    def _apply_incumbent_dispute_discount(
        self,
        *,
        receipt_run: ReceiptRun,
        episode: Episode,
        now: datetime | None,
        incumbent_uuid: str,
        challenger_relationship_uuid: str,
        challenger_confidence: float,
        scope_key: str,
        truth_key: str,
        memory_type: str | None,
        claim_mode: ClaimMode,
        directive_stance: DirectiveStance,
        relationship_type: str,
        governance_policy_digest: str | None,
    ) -> None:
        """WS-16 T13: discount a surviving ACTIVE incumbent disputed by a parked challenger.

        ``c' = max(floor, c * (1 - contradiction_discount * c_challenger))``, once
        per parked challenger, with ``disputed_count`` incremented so the dispute
        history lives on the row.  Skipped ENTIRELY under ``coherence_hold``
        (WS-10 anti-windup freezes the confidence plane in both directions).
        The mutation is hash-bracketed and receipted with decision_result
        ``reinforced`` — an evidence-plane update to an existing row — so byte
        replay folds its state transition like every other mutation.
        """
        incumbent = self._graph.get_relationship(incumbent_uuid)
        if incumbent.properties.get("coherence_hold"):
            return
        policy = self._config.confidence
        confidence_before = float(incumbent.properties.get("confidence", 0.0))
        confidence_after = max(
            policy.floor,
            confidence_before * (1.0 - policy.contradiction_discount * float(challenger_confidence)),
        )
        disputed_count = int(incumbent.properties.get("disputed_count", 0)) + 1
        state_before = self._graph.graph_state_hash(scope_key)
        self._graph.update_relationship(
            incumbent_uuid,
            properties={
                "confidence": confidence_after,
                "disputed_count": disputed_count,
            },
        )
        state_after = self._graph.graph_state_hash(scope_key)
        dispute_payload = {
            "confidence_before": confidence_before,
            "confidence_after": confidence_after,
            "challenger_relationship_uuid": challenger_relationship_uuid,
            "challenger_confidence": float(challenger_confidence),
            "disputed_count": disputed_count,
        }
        self._emit_receipt(
            receipt_run,
            decision_type=ReceiptDecisionType.FORMATION_INCUMBENT_CONFIDENCE_DISCOUNTED,
            decision_reason="incumbent_confidence_disputed",
            decision_result="reinforced",
            episode=episode,
            now=now,
            scope_key=scope_key,
            memory_type=memory_type,
            claim_mode=claim_mode.value,
            directive_stance=directive_stance.value,
            relationship_type=relationship_type,
            truth_key=truth_key,
            relationship_uuid=incumbent_uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            governance_policy_digest=governance_policy_digest,
            event_payload=json.dumps(dispute_payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(dispute_payload),
        )

    def _apply_reinforce(
        self,
        relationship: GraphRelationship,
        memory: ExtractedMemory,
        episode: Episode,
        observed_at: datetime,
    ) -> GraphRelationship:
        """Apply the reinforce update to an existing relationship row."""
        existing_episode_uuids = relationship.properties.get("episode_uuids")
        if not isinstance(existing_episode_uuids, list):
            existing_episode_uuids = [relationship.properties["episode_uuid"]]
        episode_uuids = list(dict.fromkeys([*existing_episode_uuids, episode.uuid]))
        observed_count = int(relationship.properties.get("observed_count", len(existing_episode_uuids))) + 1
        first_seen_at = relationship.properties.get("first_seen_at")
        if not isinstance(first_seen_at, str):
            first_seen_at = (relationship.valid_from or observed_at).isoformat()
        last_seen_at = relationship.properties.get("last_seen_at")
        if not isinstance(last_seen_at, str):
            last_seen_at = (relationship.valid_from or observed_at).isoformat()
        first_seen = min(datetime.fromisoformat(first_seen_at), observed_at)
        last_seen = max(datetime.fromisoformat(last_seen_at), observed_at)
        existing_confidence = float(relationship.properties.get("confidence", 0.0))
        # WS-10 anti-windup actuator: if a coherence scan has flagged this directive
        # as the target of an unresolved cross-artifact contradiction (coherence_hold),
        # the repeated feedback is still recorded as evidence (observed_count, episode
        # lineage) but the directive is NOT strengthened — confidence is frozen — so the
        # system stops escalating a memory whose real conflict lives in another artifact.
        # Inert by default: coherence_hold is absent on every ordinary relationship.
        held = bool(relationship.properties.get("coherence_hold"))
        # WS-16 T13: bounded evidence accumulation replaces monotone max().  Each
        # corroborating observation contributes reinforcement_gain of the remaining
        # headroom, scaled by its own confidence, capped at the policy ceiling.
        confidence_policy = self._config.confidence
        confidence = (
            existing_confidence
            if held
            else min(
                confidence_policy.ceiling,
                existing_confidence
                + (1.0 - existing_confidence) * confidence_policy.reinforcement_gain * float(memory.confidence),
            )
        )
        extra: dict[str, object] = {}
        if held:
            extra["escalation_suppressed_at"] = datetime.now(UTC).isoformat()
            extra["escalation_suppressed_count"] = (
                int(relationship.properties.get("escalation_suppressed_count", 0)) + 1
            )
        return self._graph.update_relationship(
            relationship.uuid,
            properties={
                "confidence": confidence,
                "confidence_strategy": "evidence_accumulation",
                "episode_uuid": episode.uuid,
                "episode_uuids": episode_uuids,
                "observed_count": observed_count,
                "first_seen_at": first_seen.isoformat(),
                "last_seen_at": last_seen.isoformat(),
                "reinforced_at": datetime.now(UTC).isoformat(),
                **extra,
            },
            valid_from=first_seen,
        )

    def _record_semantic_reinforce(
        self,
        *,
        job: DreamJob,
        now: datetime,
        fact: str,
        scope: MemoryScope,
        episode_uuid: str,
        reinforced_uuid: str,
        cosine: float,
        matched_uuid: str | None,
    ) -> None:
        """Record a synchronous dream-agent decision for a WS-1 semantic merge.

        Uses Mem0's ADD/UPDATE/DELETE/NOOP vocabulary: semantic dedup is an UPDATE.
        The decision is recorded directly to the graph without agent approval —
        semantic merging is an automated invariant, not a discretionary action.
        """
        self._graph.record_dream_decision(
            DreamDecisionRecord(
                ran_at=now,
                job_name=job.name,
                job_kind=job.kind,
                agent_id=job.agent.agent_id,
                agent_name=job.agent.name,
                agent_scope=job.agent.scope,
                decision_type="formation_semantic_reinforce",
                summary=(
                    f"{job.agent.name} reinforced existing relationship via semantic similarity "
                    f"(cosine={cosine:.4f}) instead of creating a duplicate active row."
                ),
                subject_id=reinforced_uuid,
                subject_name=fact,
                scope=scope,
                prompt_profile=job.prompt_profile,
                prompt_profile_version=job.prompt_profile_version,
                details={
                    "write_verb": "UPDATE",
                    "cosine_score": cosine,
                    "reinforced_relationship_uuid": reinforced_uuid,
                    "matched_relationship_uuid": matched_uuid,
                    "episode_uuid": episode_uuid,
                    "approved": True,
                    "reason": "semantic_dedup_cosine_threshold",
                },
            )
        )
