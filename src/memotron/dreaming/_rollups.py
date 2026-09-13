"""Collapsing many related claims into one summary claim, and unwinding it when a
member changes.

A rollup is the only object here that ASSERTS SOMETHING NO SOURCE CLAIM SAID. That
is its value and its whole risk, and most of this file is the guard rail:
_rollup_cluster_is_consistent refuses to summarise a cluster that disagrees with
itself, rollup_summary_entailed (in _rollup_text) refuses text the members do not
entail, and _rollup_state_uncertainty carries forward how much was not known rather
than rounding it away.

The dependency machinery is the other half. A rollup is derived, so it goes stale
when any member does: _rollup_dependency_digest records what it was built from,
_stale_rollup_for_rebuild detects drift, and invalidate_rollups_for_dependency
retracts. Invalidate, never delete -- the rollup stays readable at the truth time
it was correct for, same contract as supersession in _contradiction.py."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import TYPE_CHECKING

from memotron.config import (
    DreamJob,
)
from memotron.dreaming._common import _ContentProtection
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.dreaming._rollup_text import (
    ROLLUP_SYNTHESIS_SYSTEM_PROMPT,
    ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE,
    ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED,
    ROLLUP_TEXT_SOURCE_LLM,
    ROLLUP_TEXT_SOURCE_NO_TRANSPORT,
    ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR,
    _ResolvedRollupText,
    _rollup_label_clip,
    _rollup_label_common_prefix,
    _rollup_label_fit,
    _rollup_label_keywords,
    _rollup_label_shared_value,
    render_rollup_synthesis_prompt,
    rollup_summary_entailed,
    rollup_synthesis_prompt_digest,
)
from memotron.embedding import (
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.graph import normalize_key
from memotron.models import (
    ClaimMode,
    GraphRelationship,
    MemoryScope,
    MemoryType,
    RelationshipCardinality,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    motive_version_digest,
    payload_digest,
)
from memotron.synthesis import strip_markdown_fences

if TYPE_CHECKING:
    # Annotation-only, and imported at runtime inside the two methods that use it
    # (see `_synthesis_model_identifier` / `_resolve_rollup_text`). Declared here
    # so the annotation resolves under typing.get_type_hints() without hoisting
    # the runtime import, which is function-local on purpose.
    from memotron.config import ConsolidationSynthesisProfile

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class RollupMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    async def _rollup_pass_for_scope(
        self,
        *,
        job: DreamJob,
        scope: MemoryScope,
        policy: object,
        depth: int,
        created_by: str,
        now: datetime,
        receipt_run: ReceiptRun,
        rebuilt_stale_rollup_uuids: set[str],
    ) -> tuple[int, int, int]:
        """Run one clustering pass for a single scope at the given rollup depth.

        Returns (rollups_created, members_demoted, decision_count).
        """
        from memotron.config import RollupConsolidationPolicy

        policy_typed: RollupConsolidationPolicy = policy  # type: ignore[assignment]
        synthesis_profile = self._config.consolidation_profile_for_job(job)
        synthesis_profile_digest = payload_digest(synthesis_profile.model_dump(mode="json"))
        rollup_motive = self._config.resolve_motive(job=job, episode_metadata={})
        motive_digest = (
            motive_version_digest(rollup_motive, self._resolved_inputs_dict(job)) if rollup_motive is not None else None
        )

        # WS-12: rollup content is content — synthesized labels/facts in a
        # crypto-shred scope are sealed exactly like formation writes.
        protection = self._content_protection(
            scope_key=scope.key,
            governance=self._effective_governance(motive=rollup_motive),
        )

        # Collect eligible relationships for this scope and depth.
        # Eligible = active AND active_in_context (not already demoted)
        #            AND not already a ROLLUP type
        #            AND not a MENTIONS edge
        #            AND rollup_depth == depth - 1 (raw facts have no rollup_depth = 0 equivalent)
        candidates: list[GraphRelationship] = []
        for relationship in self._graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            if relationship.properties.get("scope_key") != scope.key:
                continue
            if relationship.properties.get("status") != RelationshipStatus.ACTIVE.value:
                continue
            # Depth gating: raw facts have rollup_depth absent/0; rollups have rollup_depth >= 1.
            # At depth=1 we cluster raw facts (rollup_depth missing or 0).
            # At depth=2 we cluster depth-1 rollups (rollup_depth == 1).
            rel_rollup_depth = int(relationship.properties.get("rollup_depth", 0))
            if rel_rollup_depth != depth - 1:
                continue
            is_rollup = relationship.properties.get("memory_type") == MemoryType.ROLLUP.value
            if depth == 1 and is_rollup:
                continue
            if depth > 1 and not is_rollup:
                continue
            # A rollup may be a parent at the next depth.  Demoted raw children
            # are intentionally excluded; their active parent carries the view.
            if relationship.properties.get("active_in_context") is False:
                continue
            candidates.append(relationship)

        if len(candidates) < policy_typed.min_cluster_size:
            return 0, 0, 0

        # Extract embeddings for candidates (revealed under the live DEK when sealed).
        # WS-17 T18 vector-space guard: a stored vector participates in clustering
        # only when its stamped identifier matches the active transport — a
        # mismatched or unstamped (legacy) vector is treated as absent, exactly
        # like a pre-WS-1 row without an embedding.
        embeddings: list[list[float] | None] = []
        for rel in candidates:
            if not stored_vector_in_active_space(
                rel.properties, active_identifier=self._embedding_transport.identifier
            ):
                embeddings.append(None)
                continue
            emb = self._graph.reveal_vector(scope.key, rel.properties.get("embedding"))
            embeddings.append(emb)

        # Greedy agglomerative clustering (deterministic, hermetic, stdlib only).
        # Seed a cluster with the first unclustered member;
        # add any unclustered member whose embedding cosine >= threshold.
        assigned = [False] * len(candidates)
        clusters: list[list[int]] = []

        for i in range(len(candidates)):
            if assigned[i]:
                continue
            cluster = [i]
            assigned[i] = True
            seed_emb = embeddings[i]
            for j in range(i + 1, len(candidates)):
                if assigned[j]:
                    continue
                if seed_emb is None or embeddings[j] is None:
                    continue
                try:
                    cosine = cosine_similarity(seed_emb, embeddings[j])
                except ValueError:
                    continue
                if cosine >= policy_typed.cluster_threshold:
                    cluster.append(j)
                    assigned[j] = True
            clusters.append(cluster)

        # Synthesize a ROLLUP for each qualifying cluster.
        rollups_created = 0
        members_demoted = 0
        decisions = 0

        for cluster_indices in clusters:
            if len(cluster_indices) < policy_typed.min_cluster_size:
                continue

            members = [candidates[i] for i in cluster_indices]
            # A rollup is a compact evidence view, never a vote that may turn
            # incompatible claims into an apparent consensus.  Keep only
            # clusters whose assertion/authority/polarity signatures agree.
            if not self._rollup_cluster_is_consistent(members, scope_key=scope.key):
                continue
            temporal_bounds = self._rollup_temporal_bounds(members)
            if temporal_bounds is None:
                # The members do not share a valid-time interval.  A combined
                # statement would necessarily overstate when it was true.
                continue
            rollup_valid_from, rollup_valid_to = temporal_bounds
            member_uuids = [m.uuid for m in members]
            non_normative_members = sorted(
                member.uuid for member in members if member.properties.get("authority_class") == "non_normative"
            )
            rollup_authority_class = "non_normative" if non_normative_members else "memory"
            dependency_digest = self._rollup_dependency_digest(
                members=members,
                scope_key=scope.key,
                motive_digest=motive_digest,
                synthesis_profile_digest=synthesis_profile_digest,
            )

            # Rollup text synthesis (WS-18 T19).  The deterministic structural
            # label is the no-transport default and the entailment gate's
            # reject path; when a synthesis transport is configured, the LLM
            # summary — validated by the deterministic entailment gate —
            # becomes the rollup text instead.  ``rollup_text_source`` records
            # which of those actually happened for THIS row.
            resolved_rollup_text = await self._resolve_rollup_text(
                members=members,
                scope=scope,
                synthesis_profile=synthesis_profile,
                protection=protection,
                receipt_run=receipt_run,
                now=now,
            )
            rollup_label = resolved_rollup_text.text
            rollup_prompt_digest = resolved_rollup_text.prompt_digest
            rollup_text_source = resolved_rollup_text.source

            # Materialize the ROLLUP as a normal memory relationship.
            # Subject = synthetic "Rollup" entity; Object = rollup_label text.
            rollup_subject = f"Rollup ({scope.scope_id})"
            rollup_object = rollup_label
            rollup_fact = f"{rollup_subject} summarizes {rollup_object}"

            # Early cutoff: if a stale view would synthesize byte-identical
            # normalized content from the same children, revive that version with
            # a new verifying trace instead of creating a duplicate or forcing a
            # needless parent cascade.
            stale_rollup = self._stale_rollup_for_rebuild(
                scope_key=scope.key,
                depth=depth,
                member_uuids=member_uuids,
                now=now,
                excluded_rollup_uuids=rebuilt_stale_rollup_uuids,
            )
            if stale_rollup is not None:
                old_object = str(self._graph.reveal(scope.key, stale_rollup.properties.get("object", "")))
                if normalize_key(old_object) == normalize_key(rollup_object):
                    before = self._graph.graph_state_hash(scope.key)
                    noop_update_properties: dict[str, object] = {}
                    if rollup_prompt_digest is not None:
                        noop_update_properties["rollup_synthesis_prompt_digest"] = rollup_prompt_digest
                    self._graph.update_relationship(
                        stale_rollup.uuid,
                        properties={
                            **noop_update_properties,
                            "active_in_context": True,
                            # The revived row is re-attested by THIS run, so it
                            # reports the path this run's synthesis took even
                            # when the text is unchanged.
                            "rollup_text_source": rollup_text_source,
                            "rollup_stale": False,
                            # The revived view covers the CURRENT children.
                            # Every other evidence field in this block is
                            # recomputed from ``members``; leaving the pointer
                            # list on the superseded predecessors would make the
                            # row's own evidence disagree with its digest — and
                            # a structural label reaches this branch far more
                            # often than a content-derived one, because an
                            # object-level correction does not change the shape
                            # of the cluster.
                            "derived_from": member_uuids,
                            "rollup_recomputed_at": now.isoformat(),
                            "rollup_dependency_digest": dependency_digest,
                            "rollup_synthesis_profile": synthesis_profile.key,
                            "rollup_synthesis_profile_digest": synthesis_profile_digest,
                            "rollup_motive_digest": motive_digest,
                            "rollup_version": int(stale_rollup.properties.get("rollup_version", 1)) + 1,
                            "source_temporal_bounds": self._rollup_source_temporal_bounds(members),
                            "source_state_uncertainty": self._rollup_state_uncertainty(members),
                            "authority_class": rollup_authority_class,
                            "authority_evidence_relationship_uuids": non_normative_members,
                            "rollup_dependency_evidence": self._rollup_dependency_evidence(members),
                            "rollup_evidence_aggregates": self._rollup_evidence_aggregates(members),
                            "rollup_materiality_observed_count_delta": (
                                policy_typed.reinforcement_materiality_observed_count_delta
                            ),
                            "rollup_materiality_confidence_delta": (
                                policy_typed.reinforcement_materiality_confidence_delta
                            ),
                        },
                        valid_from=rollup_valid_from,
                        valid_to=rollup_valid_to,
                        clear_valid_to=rollup_valid_to is None,
                    )
                    for member in members:
                        self._graph.update_relationship(
                            member.uuid,
                            properties={"active_in_context": False, "rolled_up_by": stale_rollup.uuid},
                        )
                    after = self._graph.graph_state_hash(scope.key)
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_RECOMPUTED_NOOP,
                        decision_reason=(f"stale_rollup_content_unchanged:authority_class={rollup_authority_class}"),
                        decision_result="transformed",
                        now=now,
                        scope_key=scope.key,
                        memory_type=MemoryType.ROLLUP.value,
                        relationship_type="ROLLUP",
                        relationship_uuid=stale_rollup.uuid,
                        source_span_digest=dependency_digest,
                        graph_state_hash_before=before,
                        graph_state_hash_after=after,
                    )
                    rebuilt_stale_rollup_uuids.add(stale_rollup.uuid)
                    continue

            # Build rollup embedding from the average of member embeddings.
            valid_embeddings = [e for e in (embeddings[i] for i in cluster_indices) if e is not None]
            if valid_embeddings:
                dim = len(valid_embeddings[0])
                avg_emb = [sum(e[d] for e in valid_embeddings) / len(valid_embeddings) for d in range(dim)]
            else:
                avg_emb = self._embed_content(
                    rollup_fact,
                    scope_key=scope.key,
                    protection=protection,
                    receipt_run=receipt_run,
                    now=now,
                )

            # WS-12: rollup node identity + names are content-protected like formation.
            rollup_subject_key = f"{scope.key}:Entity:{rollup_subject}"
            rollup_object_key = f"{scope.key}:Entity:{rollup_object}"
            if protection is not None:
                rollup_subject_key = protection.commit("node", normalize_key(rollup_subject_key))
                rollup_object_key = protection.commit("node", normalize_key(rollup_object_key))
            # Upsert the rollup subject node.
            subject_node, _ = self._graph.upsert_node(
                labels=("Entity", scope.kind.value.title()),
                key=rollup_subject_key,
                properties={
                    "name": protection.seal(rollup_subject) if protection is not None else rollup_subject,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                },
            )
            # Upsert the rollup object node.
            object_node, _ = self._graph.upsert_node(
                labels=("Entity", scope.kind.value.title()),
                key=rollup_object_key,
                properties={
                    "name": protection.seal(rollup_object) if protection is not None else rollup_object,
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                },
            )

            # The episode_uuid for the rollup: reuse the consolidation episode pattern.
            # We synthesize a minimal episode uuid from the cluster members.
            rollup_episode_uuid = hashlib.sha256(("rollup:" + ":".join(sorted(member_uuids))).encode()).hexdigest()[:32]

            # WS-11: per-mutation graph-state hashes bracket the rollup synthesis (claim 6).
            rollup_state_before = self._graph.graph_state_hash(scope.key)
            rollup_truth_key = normalize_key(f"{scope.key}:{rollup_subject}:summarizes:{rollup_object}")
            rollup_truth_prefix = normalize_key(f"{scope.key}:{rollup_subject}:summarizes")
            if protection is not None:
                rollup_content_plane: dict[str, object] = {
                    "fact": protection.seal(rollup_fact),
                    "object": protection.seal(rollup_object),
                    "embedding": protection.seal_vector(avg_emb),
                    "object_embedding": protection.seal_vector(avg_emb),
                    "fact_commitment": protection.commit("fact", rollup_fact),
                    "object_commitment": protection.commit("object", normalize_key(rollup_object)),
                    "truth_key": protection.commit("truth_key", rollup_truth_key),
                    "truth_prefix": protection.commit("truth_prefix", rollup_truth_prefix),
                }
            else:
                rollup_content_plane = {
                    "fact": rollup_fact,
                    "object": rollup_object,
                    "embedding": avg_emb,
                    "object_embedding": avg_emb,
                    "truth_key": rollup_truth_key,
                    "truth_prefix": rollup_truth_prefix,
                }
            # WS-17 T18: cluster members pass the space guard, so their average
            # (or the fresh fallback embed) lives in the ACTIVE space.
            rollup_content_plane["embedding_identifier"] = self._embedding_transport.identifier
            # WS-18 T19: an LLM-synthesized rollup pins the exact prompt that
            # produced its text; deterministic labels add no key so the
            # model_identifier=None path stays byte-identical.
            if rollup_prompt_digest is not None:
                rollup_content_plane["rollup_synthesis_prompt_digest"] = rollup_prompt_digest
            rollup_relationship = self._graph.add_relationship(
                source_uuid=subject_node.uuid,
                target_uuid=object_node.uuid,
                relationship_type="ROLLUP",
                properties={
                    **rollup_content_plane,
                    "predicate": "summarizes",
                    "confidence": max(float(m.properties.get("confidence", 0.0)) for m in members),
                    "scope_kind": scope.kind.value,
                    "scope_id": scope.scope_id,
                    "scope_key": scope.key,
                    "truth_cardinality": RelationshipCardinality.MULTI_ACTIVE.value,
                    "status": RelationshipStatus.ACTIVE.value,
                    "memory_type": MemoryType.ROLLUP.value,
                    "relationship_type": "ROLLUP",
                    "episode_uuid": rollup_episode_uuid,
                    "episode_uuids": [rollup_episode_uuid],
                    "observed_count": 1,
                    "first_seen_at": now.isoformat(),
                    "last_seen_at": now.isoformat(),
                    "confidence_strategy": "max",
                    "instruction_id": None,
                    "source_text": None,
                    "metadata": {
                        "generated_by_dream_job": job.name,
                        "dream_job_kind": job.kind.value,
                        "rollup_consolidation": True,
                    },
                    "created_by": created_by,
                    "salience_score": 0.0,
                    # WS-4 specific: evidence pointers and depth tracking.
                    "derived_from": member_uuids,
                    "rollup_depth": depth,
                    "rollup_version": 1,
                    "rollup_dependency_digest": dependency_digest,
                    "rollup_synthesis_profile": synthesis_profile.key,
                    "rollup_synthesis_profile_digest": synthesis_profile_digest,
                    "rollup_motive_digest": motive_digest,
                    # The model synthesis was ATTEMPTED under — the transport's
                    # own identifier unless the profile pins an override.  None
                    # when no transport is configured (deterministic label).
                    "rollup_model_identifier": self._synthesis_model_identifier(synthesis_profile),
                    # Which path actually produced the text above.  A rejected
                    # summary keeps the model identifier (it names what was
                    # attempted) but says here that the words are the
                    # deterministic label — the row is self-describing instead
                    # of implying a model wrote them.
                    "rollup_text_source": rollup_text_source,
                    "source_temporal_bounds": self._rollup_source_temporal_bounds(members),
                    "source_state_uncertainty": self._rollup_state_uncertainty(members),
                    "authority_class": rollup_authority_class,
                    "authority_evidence_relationship_uuids": non_normative_members,
                    "rollup_dependency_evidence": self._rollup_dependency_evidence(members),
                    "rollup_evidence_aggregates": self._rollup_evidence_aggregates(members),
                    "rollup_materiality_observed_count_delta": (
                        policy_typed.reinforcement_materiality_observed_count_delta
                    ),
                    "rollup_materiality_confidence_delta": (policy_typed.reinforcement_materiality_confidence_delta),
                    "active_in_context": True,
                },
                valid_from=rollup_valid_from,
                valid_to=rollup_valid_to,
            )

            # A changed stale view is replaced exactly once in this run.  Keep
            # the old row as immutable history, link it to the replacement, and
            # never leave an inactive-but-active "stale" duplicate that could be
            # re-synthesized on a later depth pass.
            if stale_rollup is not None:
                stale_before = self._graph.graph_state_hash(scope.key)
                self._graph.update_relationship(
                    stale_rollup.uuid,
                    properties={
                        "status": RelationshipStatus.SUPERSEDED.value,
                        "superseded_at": now.isoformat(),
                        "superseded_by_relationship_uuid": rollup_relationship.uuid,
                        "rollup_recomputed_at": now.isoformat(),
                        "rollup_rebuild_coalesced": True,
                    },
                    valid_to=now,
                )
                stale_after = self._graph.graph_state_hash(scope.key)
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_RECOMPUTED,
                    decision_reason="dependency_ordered_rebuild",
                    decision_result="superseded",
                    now=now,
                    scope_key=scope.key,
                    memory_type=MemoryType.ROLLUP.value,
                    relationship_type="ROLLUP",
                    relationship_uuid=stale_rollup.uuid,
                    successor_relationship_uuid=rollup_relationship.uuid,
                    source_span_digest=dependency_digest,
                    graph_state_hash_before=stale_before,
                    graph_state_hash_after=stale_after,
                )
                rebuilt_stale_rollup_uuids.add(stale_rollup.uuid)

            rollups_created += 1
            rollup_state_after = self._graph.graph_state_hash(scope.key)

            # WS-11: cluster-summarized disposition (rollup uuid + members + state hashes).
            # WS-18 T19: when the text came from a gate-passing LLM summary, the
            # synthesis receipt names the model and pins the exact prompt digest.
            synthesis_receipt_fields: dict[str, object] = {}
            if rollup_prompt_digest is not None:
                synthesis_model_identifier = self._synthesis_model_identifier(synthesis_profile)
                synthesis_receipt_fields = {
                    "model_identifier": synthesis_model_identifier,
                    "event_payload": json.dumps(
                        {
                            "rollup_model_identifier": synthesis_model_identifier,
                            "rollup_synthesis_prompt_digest": rollup_prompt_digest,
                            "rollup_synthesis_transport_identifier": (
                                self._rollup_synthesis_transport.identifier
                                if self._rollup_synthesis_transport is not None
                                else None
                            ),
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                }
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_CREATED,
                decision_reason=(
                    f"summarized {len(members)} members: {','.join(member_uuids)};"
                    f"authority_class={rollup_authority_class}"
                ),
                decision_result="transformed",
                now=now,
                scope_key=scope.key,
                memory_type=MemoryType.ROLLUP.value,
                relationship_type="ROLLUP",
                relationship_uuid=rollup_relationship.uuid,
                dedup_threshold=policy_typed.cluster_threshold,
                graph_state_hash_before=rollup_state_before,
                graph_state_hash_after=rollup_state_after,
                source_span_digest=dependency_digest,
                **synthesis_receipt_fields,
            )

            # Demote each member: set active_in_context=False + rolled_up_by.
            for member in members:
                if self._context_demotion_held(member):
                    # WS-23 C3: a pinned member is still summarized into the
                    # rollup (the evidence view is unchanged) but keeps its
                    # operator-held place in context.
                    continue
                member_state_before = self._graph.graph_state_hash(scope.key)
                self._graph.update_relationship(
                    member.uuid,
                    properties={
                        "active_in_context": False,
                        "rolled_up_by": rollup_relationship.uuid,
                    },
                )
                members_demoted += 1
                member_state_after = self._graph.graph_state_hash(scope.key)
                # WS-11: member-demoted disposition — member → rollup (claim 6).
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CONSOLIDATION_MEMBER_DEMOTED,
                    decision_reason="demoted_into_rollup",
                    decision_result="demoted",
                    now=now,
                    scope_key=scope.key,
                    memory_type=self.memory_type_for_relationship(dict(member.properties)),
                    relationship_type=member.type,
                    relationship_uuid=member.uuid,
                    successor_relationship_uuid=rollup_relationship.uuid,
                    graph_state_hash_before=member_state_before,
                    graph_state_hash_after=member_state_after,
                )

            # Record an auditable decision.
            await self._record_decision(
                job=job,
                now=now,
                decision_type="consolidation_rollup_created",
                subject_id=rollup_relationship.uuid,
                subject_name=rollup_fact,
                scope=scope,
                summary=(
                    f"{job.agent.name} synthesized rollup {rollup_label!r} from "
                    f"{len(members)} members at depth {depth}."
                ),
                details={
                    "approved": True,
                    "cluster_size": len(members),
                    "member_uuids": member_uuids,
                    "rollup_relationship_uuid": rollup_relationship.uuid,
                    "rollup_depth": depth,
                    "cluster_threshold": policy_typed.cluster_threshold,
                    "rollup_dependency_digest": dependency_digest,
                    "rollup_synthesis_profile": synthesis_profile.key,
                },
            )
            decisions += 1

        return rollups_created, members_demoted, decisions

    def _synthesis_model_identifier(self, synthesis_profile: ConsolidationSynthesisProfile) -> str | None:
        """The model LLM rollup synthesis runs under, or ``None`` for no LLM path.

        **A configured ``SynthesisTransport`` IS the opt-in** (WS-24).  WS-18
        shipped LLM rollup synthesis behind TWO switches — a transport *and* a
        non-``None`` ``ConsolidationSynthesisProfile.model_identifier`` — and
        nothing in the packaged config, the Motive catalog, or any preset ever
        set the second one.  The result was a feature that could not run: a
        tenant with a live gateway seat kept the deterministic label,
        recorded ``rollup_synthesis_prompt_digest=None``, and produced no
        rejection receipt either, because there had been no attempt.  A
        configured model seat that silently never fires is a defect, not a
        default.

        ``model_identifier`` is therefore an OVERRIDE, not a second on-switch:
        unset (the default) it inherits the configured transport's own
        ``identifier``, which is the honest answer to "which model wrote this
        rollup?" anyway.  Set it only to pin a different provenance string than
        the transport reports.

        ``None`` is returned only when no transport is configured, so the
        deterministic label stays the hermetic default and the profile's own
        default value — and therefore every ``synthesis_profile_digest`` and
        every dependency digest built from it — is unchanged.
        """
        if self._rollup_synthesis_transport is None:
            return None
        return synthesis_profile.model_identifier or self._rollup_synthesis_transport.identifier

    async def _resolve_rollup_text(
        self,
        *,
        members: list[GraphRelationship],
        scope: MemoryScope,
        synthesis_profile: ConsolidationSynthesisProfile,
        protection: _ContentProtection | None,
        receipt_run: ReceiptRun,
        now: datetime,
    ) -> _ResolvedRollupText:
        """WS-18 T19: resolve the rollup text for one cluster.

        Returns the text, the prompt digest (non-None only when the text came
        from a gate-passing LLM summary), and the ``rollup_text_source`` that
        names WHICH path produced the text.  The row records that source: a
        rejected summary leaves ``rollup_model_identifier`` set (synthesis WAS
        attempted under that model) while the text is the deterministic label,
        and without the source property the row claims a model wrote words the
        model never wrote.

        The LLM path runs whenever a synthesis transport is configured (see
        :meth:`_synthesis_model_identifier`); the deterministic structural
        label remains the no-transport default exactly as before.  Crypto-shred
        scopes NEVER reach the LLM: their revealed content stays in-process
        (the deterministic label already reveals only locally), and the skip is
        receipted.  Every LLM summary must pass the deterministic entailment
        gate; a rejection is receipted (the failing rule/token only — never the
        hallucinated text in plaintext fields) and falls back to the
        deterministic label, which is the gate's reject path, not an alternate
        mode.
        """
        deterministic_label = self._synthesize_rollup_label(members, scope_key=scope.key)
        model_identifier = self._synthesis_model_identifier(synthesis_profile)
        if model_identifier is None or self._rollup_synthesis_transport is None:
            return _ResolvedRollupText(deterministic_label, None, ROLLUP_TEXT_SOURCE_NO_TRANSPORT)
        if protection is not None:
            # Sealed content plane: local reveal is decrypt-on-read by design,
            # but revealed content must never leave the process for a model
            # call.  Receipt the skip so the gap is auditable, keep the
            # deterministic label.
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED,
                decision_reason="crypto_shred_scope_content_never_sent_to_llm",
                decision_result="gated",
                now=now,
                scope_key=scope.key,
                memory_type=MemoryType.ROLLUP.value,
                relationship_type="ROLLUP",
            )
            return _ResolvedRollupText(deterministic_label, None, ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE)
        member_facts = [str(self._graph.reveal(scope.key, member.properties.get("fact", ""))) for member in members]
        prompt = render_rollup_synthesis_prompt(member_facts)
        prompt_digest = rollup_synthesis_prompt_digest(system_prompt=ROLLUP_SYNTHESIS_SYSTEM_PROMPT, prompt=prompt)

        def _reject(reason: str, source: str) -> _ResolvedRollupText:
            # The rejected summary itself is deliberately NOT stored: the
            # decision_reason names the failing rule and token only, and this
            # path only runs for unsealed scopes (no sensitive-payload channel
            # exists outside crypto-shred governance).
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED,
                decision_reason=reason,
                decision_result="gated",
                now=now,
                scope_key=scope.key,
                memory_type=MemoryType.ROLLUP.value,
                relationship_type="ROLLUP",
                model_identifier=model_identifier,
                source_span_digest=prompt_digest,
                event_payload=json.dumps(
                    {
                        "rollup_model_identifier": model_identifier,
                        "rollup_synthesis_prompt_digest": prompt_digest,
                        "rollup_synthesis_transport_identifier": (self._rollup_synthesis_transport.identifier),
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            return _ResolvedRollupText(deterministic_label, None, source)

        try:
            raw_response = await self._rollup_synthesis_transport.synthesize(
                prompt, system_prompt=ROLLUP_SYNTHESIS_SYSTEM_PROMPT
            )
        except ValueError as exc:
            # Transport-level failure (missing key, HTTP error, provider
            # envelope) — receipted like a gate rejection so consolidation
            # never wedges on a transient model outage, and the deterministic
            # label (the reject path) carries the rollup.  The row says
            # ``fallback_transport_error``, not ``fallback_entailment_rejected``:
            # the model never answered, so nothing of its was refused.
            return _reject(
                f"synthesis_transport_error:{str(exc)[:200]}",
                ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR,
            )
        try:
            parsed = json.loads(strip_markdown_fences(raw_response))
        except json.JSONDecodeError:
            return _reject("summary_json_parse_failed", ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("summary"), str):
            return _reject("summary_json_shape_invalid", ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED)
        summary_text = parsed["summary"].strip()
        entailed, gate_reason = rollup_summary_entailed(summary_text, member_facts)
        if not entailed:
            return _reject(f"entailment_failed:{gate_reason}", ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED)
        return _ResolvedRollupText(summary_text, prompt_digest, ROLLUP_TEXT_SOURCE_LLM)

    def _rollup_member_parts(self, relationship: GraphRelationship, *, scope_key: str) -> tuple[str, str, str, str]:
        """The ``(fact, subject, predicate, object)`` structure of one cluster member.

        The subject is recovered from the member's OWN fact text (facts are
        materialized as ``f"{subject} {predicate} {object}"``) rather than from
        its endpoint node, so every word of a structural label is literally a
        span of a member fact — the same grounding rule the entailment gate
        enforces for an LLM summary.  A fact that does not carry the expected
        shape yields an empty subject, which reads as "not shared" and drops
        the label down the ladder instead of guessing.
        """
        properties = relationship.properties
        fact = str(self._graph.reveal(scope_key, properties.get("fact", ""))).strip()
        object_value = str(self._graph.reveal(scope_key, properties.get("object", ""))).strip()
        predicate = str(properties.get("predicate") or relationship.type or "").strip()
        subject = ""
        suffix = f" {predicate} {object_value}"
        if predicate and object_value and fact.endswith(suffix):
            subject = fact[: len(fact) - len(suffix)].strip()
        return fact, subject, predicate, object_value

    def _synthesize_rollup_label(self, members: list[GraphRelationship], *, scope_key: str) -> str:
        """Deterministic, hermetic rollup label — STRUCTURAL, not a token bag.

        This is the no-transport default AND the entailment gate's reject path
        (WS-18 T19), so it is what an operator actually reads whenever the LLM
        summary is refused.  It used to be the top-6 frequency-ranked shared
        tokens joined by spaces, which produced word salad that *looked* like a
        sentence — a real portal cluster of ten ``Special Offers has field X``
        rows rendered as ``"summarizes special field offers has"``.  Word salad
        is worse than nothing: it reads as a claim nobody made.

        The label is now derived from what the members structurally SHARE, and
        every variant is a statement about the cluster rather than about the
        world.  Ordered from most to least informative:

        ==============================  ============================================
        cluster shares                  label
        ==============================  ============================================
        subject + predicate + object    ``Priya prefers window seating``
        subject + predicate             ``Special Offers has field: 10 values``
        (stem runs as far as the        ``Acme prefers group-a operating
        members agree, word by word)      preference: 3 values``
        predicate + object              ``prefers window seating: 3 subjects``
        subject (or object) only        ``4 related identity facts about D-Scribe``
        predicate only                  ``requires: 6 related requirement facts``
        nothing                         ``6 related decision facts (audit, portal)``
        ==============================  ============================================

        Nothing here can assert a relationship the members do not support: the
        shared stem is a verbatim leading span of EVERY member fact, the counts
        are counts of the cluster itself, and the last-resort keywords are
        rendered as a comma-separated parenthetical that cannot be read as a
        proposition.  The LLM summarizer plugs in through
        :meth:`_resolve_rollup_text`.
        """
        if not members:
            return "clustered memories"

        parts = [self._rollup_member_parts(member, scope_key=scope_key) for member in members]
        facts = [part[0] for part in parts]
        subjects = [part[1] for part in parts]
        predicates = [part[2] for part in parts]
        objects = [part[3] for part in parts]
        member_count = len(members)

        memory_types = {str(member.properties.get("memory_type", "")) for member in members}
        memory_type = memory_types.pop() if len(memory_types) == 1 else ""

        if memory_type == MemoryType.ROLLUP.value:
            # A cluster of rollups shares the synthetic ``Rollup (<scope>)``
            # subject and the ``summarizes`` predicate by construction, so
            # restating them would only nest the boilerplate.  The child labels
            # are the whole content, and what they agree on is the stem.
            child_stem = _rollup_label_common_prefix(objects)
            if child_stem:
                return _rollup_label_fit(child_stem, f": {member_count} rollups")
            noun = f"{member_count} related rollups"
            keywords = _rollup_label_keywords(objects)
            if keywords:
                return _rollup_label_clip(f"{noun} ({', '.join(keywords)})")
            return noun

        noun = f"{member_count} related {memory_type} facts" if memory_type else f"{member_count} related memories"
        shared_subject = _rollup_label_shared_value(subjects)
        shared_predicate = _rollup_label_shared_value(predicates)
        shared_object = _rollup_label_shared_value(objects)

        if shared_subject and shared_predicate and shared_object:
            return _rollup_label_clip(f"{shared_subject} {shared_predicate} {shared_object}")
        if shared_subject and shared_predicate:
            # The stem runs past ``subject predicate`` for as long as the member
            # facts literally agree, so sibling clusters that differ only inside
            # their objects ("group-a" vs "group-b") get different labels
            # instead of collapsing onto one.
            stem = _rollup_label_common_prefix(facts) or f"{shared_subject} {shared_predicate}"
            distinct_objects = len({normalize_key(value) for value in objects if value})
            return _rollup_label_fit(stem, f": {distinct_objects} values")
        if shared_predicate and shared_object:
            distinct_subjects = len({normalize_key(value) for value in subjects if value})
            return _rollup_label_fit(f"{shared_predicate} {shared_object}", f": {distinct_subjects} subjects")
        if shared_subject:
            return _rollup_label_clip(f"{noun} about {shared_subject}")
        if shared_object:
            return _rollup_label_clip(f"{noun} about {shared_object}")
        if shared_predicate:
            return _rollup_label_fit(shared_predicate, f": {noun}")

        keywords = _rollup_label_keywords(facts)
        if keywords:
            return _rollup_label_clip(f"{noun} ({', '.join(keywords)})")
        return noun

    def _rollup_cluster_is_consistent(
        self,
        members: list[GraphRelationship],
        *,
        scope_key: str,
    ) -> bool:
        """Return whether a cluster may safely be represented by one rollup.

        Embedding similarity is not evidence of compatible truth.  We therefore
        require the same memory/claim/authority class and the same predicate
        polarity before synthesizing.  Clusters that fail this deterministic
        gate remain as separately retrievable evidence instead of being
        summarized into a misleading consensus.
        """
        signatures: set[tuple[str, str, str, str, str]] = set()
        for relationship in members:
            props = relationship.properties
            predicate = normalize_key(str(props.get("predicate", relationship.type)))
            object_value = str(self._graph.reveal(scope_key, props.get("object", "")))
            polarity = "negative" if self._rollup_value_is_negative(predicate, object_value) else "positive"
            signatures.add(
                (
                    str(props.get("memory_type", "")),
                    str(props.get("claim_mode", ClaimMode.DESCRIPTIVE_ASSERTION.value)),
                    str(props.get("directive_stance", "")),
                    predicate,
                    polarity,
                )
            )
        return len(signatures) == 1

    @staticmethod
    def _rollup_value_is_negative(predicate: str, object_value: str) -> bool:
        normalized = f"{predicate} {normalize_key(object_value)}"
        return bool(
            re.search(
                r"(?:\bnot\b|\bnever\b|\bno\b|\bwithout\b|\bforbid(?:s|den)?\b|\bprohibit(?:s|ed)?\b|\bdeny|\bban(?:ned)?\b)",
                normalized,
            )
        )

    @staticmethod
    def _rollup_temporal_bounds(
        members: list[GraphRelationship],
    ) -> tuple[datetime, datetime | None] | None:
        valid_from = max(member.valid_from for member in members)
        finite_valid_to = [member.valid_to for member in members if member.valid_to is not None]
        valid_to = min(finite_valid_to) if finite_valid_to else None
        if valid_to is not None and valid_to <= valid_from:
            return None
        return valid_from, valid_to

    @staticmethod
    def _rollup_source_temporal_bounds(members: list[GraphRelationship]) -> list[dict[str, str | None]]:
        return [
            {
                "relationship_uuid": member.uuid,
                "valid_from": member.valid_from.isoformat(),
                "valid_to": member.valid_to.isoformat() if member.valid_to is not None else None,
            }
            for member in sorted(members, key=lambda relationship: relationship.uuid)
        ]

    @staticmethod
    def _rollup_state_uncertainty(members: list[GraphRelationship]) -> dict[str, object]:
        confidences = [float(member.properties.get("confidence", 0.0)) for member in members]
        return {
            "confidence_min": min(confidences),
            "confidence_max": max(confidences),
            "claim_modes": sorted(
                {str(member.properties.get("claim_mode", ClaimMode.DESCRIPTIVE_ASSERTION.value)) for member in members}
            ),
            "directive_stances": sorted({str(member.properties.get("directive_stance", "")) for member in members}),
        }

    @staticmethod
    def _rollup_dependency_evidence(members: list[GraphRelationship]) -> dict[str, dict[str, object]]:
        """Snapshot the evidence level represented by a synthesized rollup.

        The snapshot is deliberately separate from the dependency digest.  A
        corroborating observation is evidence about an existing child, not a
        content/lifecycle change.  Keeping it out of the digest lets a rollup
        accumulate evidence without needless re-synthesis while still making a
        policy-defined material evidence change observable and actionable.
        """
        return {
            member.uuid: {
                "observed_count": int(member.properties.get("observed_count", 1)),
                "confidence": float(member.properties.get("confidence", 0.0)),
                "last_seen_at": str(member.properties.get("last_seen_at", "")),
            }
            for member in sorted(members, key=lambda relationship: relationship.uuid)
        }

    @staticmethod
    def _rollup_evidence_aggregates(members: list[GraphRelationship]) -> dict[str, object]:
        """Return non-normative corroboration aggregates for a rollup read model."""
        if not members:
            return {
                "member_count": 0,
                "observed_count_total": 0,
                "confidence_min": 0.0,
                "confidence_max": 0.0,
                "last_seen_at": None,
            }
        observed_counts = [int(member.properties.get("observed_count", 1)) for member in members]
        confidences = [float(member.properties.get("confidence", 0.0)) for member in members]
        last_seen = [
            str(member.properties["last_seen_at"])
            for member in members
            if isinstance(member.properties.get("last_seen_at"), str)
        ]
        return {
            "member_count": len(members),
            "observed_count_total": sum(observed_counts),
            "confidence_min": min(confidences),
            "confidence_max": max(confidences),
            "last_seen_at": max(last_seen) if last_seen else None,
        }

    def _stale_rollup_for_rebuild(
        self,
        *,
        scope_key: str,
        depth: int,
        member_uuids: list[str],
        now: datetime,
        excluded_rollup_uuids: set[str],
    ) -> GraphRelationship | None:
        """Find the stale logical view represented by the current child set.

        A corrected child has a new relationship UUID.  Matching raw
        ``derived_from`` lists would therefore create a replacement without
        consuming its stale predecessor.  Resolve each old child through its
        supersession chain first; this gives the reducer one stable rebuild
        identity and prevents multiple re-syntheses after batched corrections.
        """
        target = sorted(member_uuids)
        candidates: list[GraphRelationship] = []
        for relationship in self._graph.relationships():
            props = relationship.properties
            if (
                relationship.uuid in excluded_rollup_uuids
                or props.get("memory_type") != MemoryType.ROLLUP.value
                or props.get("scope_key") != scope_key
                or props.get("rollup_stale") is not True
                or int(props.get("rollup_depth", 0)) != depth
            ):
                continue
            derived_from = props.get("derived_from")
            if not isinstance(derived_from, list):
                continue
            resolved_members: list[str] = []
            for raw_member_uuid in derived_from:
                try:
                    member = self._graph.get_relationship(str(raw_member_uuid))
                except ValueError:
                    resolved_members = []
                    break
                successor = self._rollup_active_supersession_successor(member, now=now)
                resolved_members.append((successor or member).uuid)
            if sorted(resolved_members) == target:
                candidates.append(relationship)
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda relationship: (
                int(relationship.properties.get("rollup_version", 1)),
                relationship.created_at,
                relationship.uuid,
            ),
            reverse=True,
        )[0]

    def _record_rollup_reinforcement(
        self,
        *,
        relationship: GraphRelationship,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> None:
        """Update rollup evidence aggregates and invalidate only material changes.

        This runs after the formation receipt has committed the child
        reinforcement.  It is intentionally a separate derived-plane mutation:
        it never changes truth confidence, rollup text, or the dependency digest.
        """
        scope_key = str(relationship.properties.get("scope_key") or receipt_run.scope_key)
        material_parents: list[tuple[str, str]] = []
        for rollup in self._graph.relationships():
            props = rollup.properties
            derived_from = props.get("derived_from")
            if (
                props.get("memory_type") != MemoryType.ROLLUP.value
                or props.get("scope_key") != scope_key
                or props.get("rollup_stale") is True
                or not isinstance(derived_from, list)
                or relationship.uuid not in derived_from
            ):
                continue
            snapshot = props.get("rollup_dependency_evidence")
            baseline = snapshot.get(relationship.uuid) if isinstance(snapshot, dict) else None
            baseline_observed_count = int(baseline.get("observed_count", 1)) if isinstance(baseline, dict) else 1
            baseline_confidence = float(baseline.get("confidence", 0.0)) if isinstance(baseline, dict) else 0.0
            observed_delta = int(relationship.properties.get("observed_count", 1)) - baseline_observed_count
            confidence_delta = float(relationship.properties.get("confidence", 0.0)) - baseline_confidence
            observed_threshold = int(props.get("rollup_materiality_observed_count_delta", 3))
            confidence_threshold = float(props.get("rollup_materiality_confidence_delta", 0.10))
            material = observed_delta >= observed_threshold or (
                confidence_threshold > 0.0 and confidence_delta >= confidence_threshold
            )
            members: list[GraphRelationship] = []
            for child_uuid in derived_from:
                try:
                    members.append(self._graph.get_relationship(str(child_uuid)))
                except ValueError:
                    # A missing child is a lifecycle break and the normal
                    # invalidation path will handle it; do not fabricate an
                    # aggregate from incomplete lineage.
                    members = []
                    break
            if not members:
                continue
            before = self._graph.graph_state_hash(scope_key)
            aggregate = self._rollup_evidence_aggregates(members)
            self._graph.update_relationship(
                rollup.uuid,
                properties={
                    "rollup_evidence_aggregates": aggregate,
                    "rollup_evidence_last_updated_at": now.isoformat(),
                },
            )
            after = self._graph.graph_state_hash(scope_key)
            aggregate_digest = payload_digest(aggregate)
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_EVIDENCE_UPDATED,
                decision_reason=(
                    f"child_reinforced:observed_delta={observed_delta};"
                    f"confidence_delta={confidence_delta:.6f};material={material}"
                ),
                decision_result="recorded",
                now=now,
                scope_key=scope_key,
                memory_type=MemoryType.ROLLUP.value,
                relationship_type="ROLLUP",
                relationship_uuid=rollup.uuid,
                successor_relationship_uuid=relationship.uuid,
                source_span_digest=aggregate_digest,
                graph_state_hash_before=before,
                graph_state_hash_after=after,
            )
            if material:
                reason = (
                    "material_reinforcement:"
                    f"child={relationship.uuid};observed_delta={observed_delta};"
                    f"confidence_delta={confidence_delta:.6f}"
                )
                material_parents.append((rollup.uuid, reason))

        for parent_uuid, reason in material_parents:
            # Walk from the reinforced child.  The shared walker first marks
            # its direct parent stale, then propagates that state to every
            # higher-depth view; repeated calls coalesce because stale parents
            # are not invalidated twice.
            self.invalidate_rollups_for_dependency(
                relationship_uuid=relationship.uuid,
                reason=reason,
                now=now,
                receipt_run=receipt_run,
            )
            state = self._graph.graph_state_hash(scope_key)
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_MATERIAL_REINFORCEMENT,
                decision_reason=reason,
                decision_result="demoted",
                now=now,
                scope_key=scope_key,
                memory_type=MemoryType.ROLLUP.value,
                relationship_type="ROLLUP",
                relationship_uuid=parent_uuid,
                successor_relationship_uuid=relationship.uuid,
                graph_state_hash_before=state,
                graph_state_hash_after=state,
            )

    def _rollup_dependency_digest(
        self,
        *,
        members: list[GraphRelationship],
        scope_key: str,
        motive_digest: str | None,
        synthesis_profile_digest: str,
    ) -> str:
        """Hash the lifecycle projection that can make a derived rollup stale.

        Use/outcome telemetry is deliberately absent: utility changes retention,
        never the truth content a rollup summarizes.  The projection contains only
        content commitment plus lifecycle/temporal state, so replay can decide
        staleness without invoking synthesis again.
        """
        dependencies: list[dict[str, object]] = []
        for member in sorted(members, key=lambda relationship: relationship.uuid):
            props = member.properties
            content_commitment = props.get("fact_commitment")
            if not isinstance(content_commitment, str) or not content_commitment:
                fact = str(self._graph.reveal(scope_key, props.get("fact", "")))
                content_commitment = hashlib.sha256(fact.encode("utf-8")).hexdigest()
            dependencies.append(
                {
                    "relationship_uuid": member.uuid,
                    "content_commitment": content_commitment,
                    "status": props.get("status"),
                    "claim_mode": props.get("claim_mode"),
                    "superseded_by_relationship_uuid": props.get("superseded_by_relationship_uuid"),
                    "valid_from": member.valid_from,
                    "valid_to": member.valid_to,
                }
            )
        return payload_digest(
            {
                "scope_key": scope_key,
                "dependencies": dependencies,
                "motive_digest": motive_digest,
                "synthesis_profile_digest": synthesis_profile_digest,
            }
        )

    def invalidate_rollups_for_dependency(
        self,
        *,
        relationship_uuid: str,
        reason: str,
        now: datetime,
        receipt_run: ReceiptRun,
    ) -> int:
        """Synchronously hide every parent view affected by a child lifecycle change.

        This is intentionally a deterministic dependency walk rather than a
        deferred LLM action.  Each affected rollup leaves default context and
        its direct children are re-promoted as a safe, evidence-preserving
        fallback until the next dependency-ordered rollup pass rebuilds it.
        """
        invalidated = 0
        pending = [relationship_uuid]
        seen: set[str] = set()
        while pending:
            child_uuid = pending.pop()
            if child_uuid in seen:
                continue
            seen.add(child_uuid)
            for rollup in self._graph.relationships():
                if rollup.properties.get("memory_type") != MemoryType.ROLLUP.value:
                    continue
                derived_from = rollup.properties.get("derived_from")
                if not isinstance(derived_from, list) or child_uuid not in derived_from:
                    continue
                if rollup.properties.get("rollup_stale") is True:
                    pending.append(rollup.uuid)
                    continue
                scope_key = str(rollup.properties.get("scope_key"))
                before = self._graph.graph_state_hash(scope_key)
                self._graph.update_relationship(
                    rollup.uuid,
                    properties={
                        "active_in_context": False,
                        "rollup_stale": True,
                        "rollup_stale_reason": reason,
                        "rollup_stale_at": now.isoformat(),
                    },
                )
                fallback_relationship_uuids: list[str] = []
                for raw_member_uuid in derived_from:
                    member = self._graph.get_relationship(str(raw_member_uuid))
                    if (
                        member.properties.get("rolled_up_by") == rollup.uuid
                        and member.properties.get("status") == RelationshipStatus.ACTIVE.value
                        and member.properties.get("rollup_stale") is not True
                        and member.valid_from <= now
                        and (member.valid_to is None or member.valid_to > now)
                    ):
                        self._graph.update_relationship(
                            member.uuid,
                            properties={
                                "active_in_context": True,
                                "rolled_up_by": None,
                                "repromoted_from_stale_rollup": rollup.uuid,
                            },
                        )
                        fallback_relationship_uuids.append(member.uuid)
                        continue
                    successor = self._rollup_active_supersession_successor(member, now=now)
                    if successor is not None and successor.properties.get("rollup_stale") is not True:
                        self._graph.update_relationship(
                            successor.uuid,
                            properties={
                                "active_in_context": True,
                                "rolled_up_by": None,
                                "repromoted_from_stale_rollup": rollup.uuid,
                            },
                        )
                        fallback_relationship_uuids.append(successor.uuid)
                self._graph.update_relationship(
                    rollup.uuid,
                    properties={
                        "rollup_fallback_relationship_uuids": sorted(set(fallback_relationship_uuids)),
                    },
                )
                after = self._graph.graph_state_hash(scope_key)
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.CONSOLIDATION_ROLLUP_INVALIDATED,
                    decision_reason=reason,
                    decision_result="demoted",
                    now=now,
                    scope_key=scope_key,
                    memory_type=MemoryType.ROLLUP.value,
                    relationship_type="ROLLUP",
                    relationship_uuid=rollup.uuid,
                    successor_relationship_uuid=child_uuid,
                    source_span_digest=str(rollup.properties.get("rollup_dependency_digest") or ""),
                    graph_state_hash_before=before,
                    graph_state_hash_after=after,
                )
                invalidated += 1
                pending.append(rollup.uuid)
        return invalidated

    def _rollup_active_supersession_successor(
        self,
        relationship: GraphRelationship,
        *,
        now: datetime,
    ) -> GraphRelationship | None:
        """Resolve a stale rollup member to its current active successor.

        The successor need not itself have been a member of the rollup.  This is
        the important safe fallback after a correction: serve the current fact,
        not an obsolete child simply because it was in the old cluster.
        """
        current = relationship
        seen: set[str] = set()
        while current.uuid not in seen:
            seen.add(current.uuid)
            if (
                current.properties.get("status") == RelationshipStatus.ACTIVE.value
                and current.valid_from <= now
                and (current.valid_to is None or current.valid_to > now)
            ):
                return current
            successor_uuid = current.properties.get("superseded_by_relationship_uuid")
            if not isinstance(successor_uuid, str) or not successor_uuid:
                return None
            try:
                current = self._graph.get_relationship(successor_uuid)
            except ValueError:
                return None
        return None
