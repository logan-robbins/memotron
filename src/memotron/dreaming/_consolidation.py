"""The consolidation pass: which scopes and episodes a run touches, and what it hands
to the workers.

Deliberately thin -- nine members, ~400 lines -- because it is an ORCHESTRATOR. It
picks the scopes, resolves the episode, assembles the graph context and then calls
into _dedup and _rollups. Those two calls are the only edges the measured partition
found between the three files, which is what makes this shape worth keeping: if
this file starts growing rollup or dedup logic of its own, the boundary is gone and
the call graph will say so.

_graph_context_for_episode is the largest member and the one to watch. It decides
what a consolidation pass can SEE, so a bug here does not corrupt anything -- it
just quietly narrows the input, and every downstream decision is then correct about
a smaller world than it should have had.

_formation_consumer_key and _consolidation_consumer_key sit together on purpose:
they are the two halves of the same idempotency scheme, and they must not collide."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from memotron.config import (
    DreamContextPolicy,
    DreamJob,
    metadata_matches_filter,
)
from memotron.dreaming._common import _log
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.graph import normalize_key
from memotron.models import (
    DreamContextMemory,
    DreamJobRun,
    Episode,
    EpisodeType,
    MemoryScope,
    MemoryType,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
    payload_digest,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class ConsolidationMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    async def _run_consolidation(self, *, job: DreamJob, now: datetime, receipt_run: ReceiptRun) -> DreamJobRun:
        run = DreamJobRun(job_name=job.name, job_kind=job.kind)
        selected_scopes: list[MemoryScope] = []
        consolidation_consumer = self._consolidation_consumer_key(job)
        for scope in self._consolidation_scopes(job):
            if run.processed_scopes >= job.max_items_per_run:
                break
            # Atomic scope claim: rollup synthesis is a non-idempotent
            # ``add_relationship`` over a cluster selection read earlier (and,
            # with a synthesis transport, an LLM call apart), so two concurrent
            # consolidation runs for the same consumer would both materialize
            # the same cluster as two ACTIVE ROLLUP rows.  The claim makes the
            # second run skip the scope; the next cadence retries it.  Distinct
            # Motives stay independent consumers, mirroring formation.
            if not self._graph.claim_scope_work(
                work_kind="consolidation",
                scope_key=scope.key,
                consumer_key=consolidation_consumer,
                run_uuid=receipt_run.run_uuid,
                now=now,
            ):
                _log.info(
                    "consolidation scope %s skipped: another run holds its claim",
                    scope.key,
                )
                continue
            graph_context = [
                relationship
                for relationship in self._graph.context_visible_relationships(scope=scope)
                if relationship.type != "MENTIONS"
            ][: job.context_policy.max_relationships]
            if not graph_context:
                await self._record_decision(
                    job=job,
                    now=now,
                    decision_type="consolidation_scope_skipped",
                    subject_id=scope.key,
                    subject_name=f"{job.name} consolidation for {scope.key}",
                    scope=scope,
                    summary=f"{job.agent.name} skipped consolidation for {scope.key} because no graph context matched.",
                    details={
                        "approved": False,
                        "reason": "no_graph_context",
                        "context_policy": job.context_policy.model_dump(mode="json"),
                    },
                )
                run.decision_count += 1
                continue
            decision = await self._decide(
                job=job,
                now=now,
                receipt_run=receipt_run,
                decision_type="consolidation_scope_selected",
                proposed_action="Review same-scope graph context and create justified derived memories.",
                subject_id=scope.key,
                subject_name=f"{job.name} consolidation for {scope.key}",
                scope=scope,
                context_facts=tuple(
                    str(self._graph.reveal(scope.key, memory.properties.get("fact", ""))) for memory in graph_context
                ),
                details={
                    "graph_context_count": len(graph_context),
                    "consolidation_profile_key": self._config.consolidation_profile_for_job(job).key,
                    "mechanism": "dependency_tracked_rollup_reducer",
                },
            )
            run.decision_count += 1
            if not decision.approved:
                continue
            run.processed_scopes += 1
            selected_scopes.append(scope)
        if selected_scopes:
            rollup_created, _rollup_demoted, rollup_decisions = await self._run_rollup_consolidation(
                job=job,
                now=now,
                receipt_run=receipt_run,
                scopes=selected_scopes,
            )
            run.created_relationships += rollup_created
            run.decision_count += rollup_decisions
        return run

    async def _run_rollup_consolidation(
        self,
        *,
        job: DreamJob,
        now: datetime,
        receipt_run: ReceiptRun,
        scopes: list[MemoryScope],
    ) -> tuple[int, int, int]:
        """WS-4: Rollup (clustering) consolidation pass.

        Clusters context-visible active memories by embedding cosine similarity,
        synthesizes a ROLLUP relationship for each qualifying cluster, and demotes
        members by setting active_in_context=False + rolled_up_by=<rollup_uuid>.

        Recursion: runs up to policy.max_depth passes.  On each pass only
        relationships at depth < max_depth are eligible (so rollups can themselves
        cluster into higher rollups).

        Returns (rollups_created, members_demoted, decision_count).
        """
        from memotron.config import RollupConsolidationPolicy

        policy: RollupConsolidationPolicy = job.rollup_consolidation_policy
        created_by = job.agent.created_by(job.kind)
        total_rollups = 0
        total_demoted = 0
        total_decisions = 0

        # WS-17 T17: cross-prefix duplicate sweep runs BEFORE rollup clustering
        # (and independently of the Motive→ROLLUP gate — it is a dedup mechanism,
        # not rollup synthesis), so redundant restatements of one fact never
        # cluster as if they were independent evidence.
        for scope in scopes:
            duplicate_demoted, duplicate_decisions = await self._cross_prefix_duplicate_sweep(
                job=job,
                scope=scope,
                policy=policy,
                now=now,
                receipt_run=receipt_run,
            )
            total_demoted += duplicate_demoted
            total_decisions += duplicate_decisions

        # WS-3 × WS-4 interlock: the SAME named Motive that gates which memory TYPES may be
        # written at formation (see _apply_motive_type_filter) also governs admissibility of
        # the ROLLUP memory type produced by consolidation.  When an active Motive constrains
        # allowed_memory_types and ROLLUP is not among them, no rollups may be formed for this
        # job, and the gate is recorded as an auditable decision (no silent skips).  This is a
        # no-op when no Motive is active or its allowed_memory_types is empty (allow all),
        # preserving full backward compatibility with all pre-WS-3 rollup runs.
        motive = self._config.resolve_motive(job=job, episode_metadata={})
        if motive is not None and motive.allowed_memory_types and MemoryType.ROLLUP not in motive.allowed_memory_types:
            allowed_values = sorted(t.value for t in motive.allowed_memory_types)
            _log.info(
                "WS-3 motive rollup gate [%s]: rollup consolidation skipped — motive %s does "
                "not allow the %r memory_type (allowed: %s)",
                job.name,
                motive.name,
                MemoryType.ROLLUP.value,
                allowed_values,
            )
            # WS-11: Motive→ROLLUP consolidation gate ([0021]).
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.CONSOLIDATION_MOTIVE_ROLLUP_GATED,
                decision_reason="motive_rollup_not_allowed",
                decision_result="gated",
                now=now,
                scope_key=receipt_run.scope_key,
                motive_name=motive.name,
                memory_type=MemoryType.ROLLUP.value,
            )
            await self._record_decision(
                job=job,
                now=now,
                decision_type="consolidation_motive_rollup_gated",
                subject_id=job.name,
                subject_name=f"{job.name} rollup consolidation",
                scope=None,
                summary=(
                    f"{job.agent.name} skipped rollup consolidation: motive {motive.name!r} "
                    f"does not allow the {MemoryType.ROLLUP.value!r} memory_type. "
                    f"Allowed: {allowed_values}."
                ),
                details={
                    "approved": False,
                    "reason": "motive_rollup_not_allowed",
                    "motive_name": motive.name,
                    "allowed_memory_types": allowed_values,
                },
            )
            # The duplicate sweep above still ran; only ROLLUP synthesis is gated.
            return 0, total_demoted, total_decisions + 1

        for scope in scopes:
            # A dependency can change repeatedly before this maintenance run.
            # Remember every stale view rebuilt in this scope so the reducer
            # coalesces those changes into one synthesis per logical view.
            rebuilt_stale_rollup_uuids: set[str] = set()
            for depth in range(1, policy.max_depth + 1):
                rollups, demoted, decisions = await self._rollup_pass_for_scope(
                    job=job,
                    scope=scope,
                    policy=policy,
                    depth=depth,
                    created_by=created_by,
                    now=now,
                    receipt_run=receipt_run,
                    rebuilt_stale_rollup_uuids=rebuilt_stale_rollup_uuids,
                )
                total_rollups += rollups
                total_demoted += demoted
                total_decisions += decisions
                # Existing depth-(d-1) rollups can become eligible for a higher
                # pass on a later consolidation run even when this pass created
                # nothing new.  Do not let a depth-1 no-op suppress recursion.

        return total_rollups, total_demoted, total_decisions

    def _consolidation_scopes(self, job: DreamJob) -> list[MemoryScope]:
        if job.scope is not None:
            scopes = {job.scope.key: job.scope}
        else:
            scopes = {}
            for relationship in self._graph.relationships():
                if relationship.type == "MENTIONS":
                    continue
                scope_kind = relationship.properties.get("scope_kind")
                scope_id = relationship.properties.get("scope_id")
                if not isinstance(scope_kind, str) or not isinstance(scope_id, str):
                    continue
                scope = MemoryScope(kind=scope_kind, scope_id=scope_id)
                scopes[scope.key] = scope
        # WS-12: a crypto-shredded scope is erased — its content is unrecoverable
        # and it accepts no further dream work.  Excluded from all consolidation.
        result: list[MemoryScope] = []
        for key in sorted(scopes):
            key_state = self._graph.governance_key_state(key)
            if key_state is not None and key_state["shredded"]:
                continue
            result.append(scopes[key])
        return result

    def _consolidation_episode(self, *, job: DreamJob, scope: MemoryScope, now: datetime) -> Episode:
        return Episode(
            name=f"{job.name} consolidation for {scope.key}",
            body=(
                "Offline consolidation dream. Review the existing graph context for this scope and create only "
                "new durable memories, inferred lessons, or corrected truths that are justified by that context."
            ),
            source=EpisodeType.TEXT,
            source_description="offline graph consolidation",
            scope=scope,
            reference_time=now,
            metadata={
                "generated_by_dream_job": job.name,
                "dream_job_kind": job.kind.value,
                "consolidation_scope_key": scope.key,
            },
            instruction_set=job.instruction_set,
        )

    def _graph_context_for_episode(
        self,
        *,
        episode: Episode,
        policy: DreamContextPolicy,
    ) -> list[DreamContextMemory]:
        if not policy.enabled or policy.max_relationships == 0:
            return []
        allowed_relationship_types = set(policy.relationship_types) if policy.relationship_types is not None else None
        episode_tokens = set(normalize_key(f"{episode.name} {episode.source_description} {episode.body}").split())
        # WS-17 T16b: context selection resolves subjects through the ACTIVE
        # alias registry — a fact about the canonical becomes selectable when
        # the episode mentions any of its alias surfaces (and vice versa), so
        # the extractor sees the existing truth instead of re-fragmenting it.
        alias_membership: dict[str, frozenset[str]] = {}
        if self._config.entity_resolution.enabled:
            alias_groups: dict[str, set[str]] = {}
            for row in self._graph.entity_alias_rows_for_scope(episode.scope.key, status="active"):
                canonical_normalized = normalize_key(str(row["canonical_name"]))
                group = alias_groups.setdefault(canonical_normalized, {canonical_normalized})
                group.add(str(row["name_normalized"]))
            for group in alias_groups.values():
                frozen = frozenset(group)
                for member in group:
                    alias_membership[member] = frozen
        candidates: list[tuple[tuple[int, float, datetime], DreamContextMemory]] = []
        for relationship in self._graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            if relationship.properties.get("scope_key") != episode.scope.key:
                continue
            if allowed_relationship_types is not None and relationship.type not in allowed_relationship_types:
                continue
            if not metadata_matches_filter(
                relationship.properties.get("metadata", {}),
                policy.metadata_filter,
            ):
                continue
            status = self._relationship_status(relationship.properties.get("status"))
            if status not in policy.include_statuses:
                continue
            if policy.as_of_episode_time and not self._relationship_visible_at_episode_time(relationship, episode):
                continue
            # WS-12: decrypt-on-read — sealed content resolves to plaintext while the
            # scope DEK lives (extraction prompts and ranking need text) and to the
            # shredded placeholder afterwards.
            scope_key = episode.scope.key
            subject = self._graph.reveal(scope_key, self._graph.get_node(relationship.source_uuid).properties["name"])
            object_value = self._graph.reveal(
                scope_key, self._graph.get_node(relationship.target_uuid).properties["name"]
            )
            fact = str(
                self._graph.reveal(
                    scope_key,
                    relationship.properties.get("fact", f"{subject} {relationship.type} {object_value}"),
                )
            )
            context_memory = DreamContextMemory(
                relationship_uuid=relationship.uuid,
                relationship_type=relationship.type,
                fact=fact,
                scope=episode.scope,
                status=status,
                subject=str(subject),
                predicate=str(relationship.properties.get("predicate", "")),
                object=str(object_value),
                confidence=float(relationship.properties.get("confidence", 0.0)),
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
                observed_count=int(relationship.properties.get("observed_count", 1)),
                episode_uuids=self._relationship_episode_uuids(relationship),
                metadata=dict(relationship.properties.get("metadata", {})) if policy.include_metadata else {},
                memory_type=self.memory_type_for_relationship(dict(relationship.properties)),
            )
            context_tokens = set(
                normalize_key(
                    f"{context_memory.fact} {context_memory.subject} {context_memory.predicate} {context_memory.object}"
                ).split()
            )
            # WS-17 T16b: a fact whose subject sits in an alias group also
            # matches episode text that uses any OTHER member of that group.
            alias_group = alias_membership.get(normalize_key(context_memory.subject))
            if alias_group:
                for member in alias_group:
                    context_tokens.update(member.split())
            relevance = len(episode_tokens.intersection(context_tokens))
            valid_from = context_memory.valid_from or relationship.created_at
            candidates.append(((relevance, context_memory.confidence, valid_from), context_memory))
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [context_memory for _, context_memory in candidates[: policy.max_relationships]]

    def episode_matches_job(
        self,
        *,
        episode: Episode,
        job: DreamJob,
        consumer_key: str | None = None,
    ) -> bool:
        effective_consumer = consumer_key or self._formation_consumer_key(job)
        if self._graph.is_episode_processed(episode.uuid, consumer_key=effective_consumer):
            return False
        if job.scope is not None and episode.scope != job.scope:
            return False
        if episode.instruction_set != job.instruction_set:
            return False
        if not self._promotion_candidate_eligible(episode):
            return False
        return job.episode_filter.matches(episode)

    def _promotion_candidate_eligible(self, episode: Episode) -> bool:
        """WS-19 T20: endorsement threshold gate for promotion candidates.

        An episode carrying ``promotion=True`` metadata is eligible for
        formation only once its endorsement count reaches the tenant's ACTIVE
        ``min_endorsements`` (read live from the project-memory config, so an
        operator lowering the bar takes effect on the next refresh).  An
        under-endorsed candidate is simply not matched — it is never consumed
        or marked processed, so it becomes eligible on a later refresh after
        more endorsements arrive.  Non-promotion episodes always pass.
        """
        if episode.metadata.get("promotion") is not True:
            return True
        min_endorsements = 1
        tenant_id = str(episode.metadata.get("tenant_id", "")).strip()
        if tenant_id:
            config = self._graph.active_project_memory_config(tenant_id)
            if config is not None:
                min_endorsements = max(1, int(config.get("min_endorsements", 1)))
        return self._graph.promotion_endorsement_count(episode.uuid) >= min_endorsements

    @staticmethod
    def _formation_consumer_key(job: DreamJob) -> str:
        # Processing identity follows the formation policy that can produce a
        # distinct memory projection. Job names, scope wrappers, filters, and
        # cadence are scheduling details; including them would replay old raw
        # episodes whenever an operator renamed or narrowed a job. Distinct
        # Motives remain independent consumers of the same immutable evidence.
        return "formation:" + payload_digest(
            {
                "instruction_set": job.instruction_set,
                "motive": job.motive,
            }
        )

    @staticmethod
    def _consolidation_consumer_key(job: DreamJob) -> str:
        # Claim identity for the rollup pass mirrors the formation consumer:
        # the same instruction set + Motive is one projection whose rollup
        # synthesis must be single-flight per scope, while two genuinely
        # different Motives remain independent consumers of the same facts.
        return "consolidation:" + payload_digest(
            {
                "instruction_set": job.instruction_set,
                "motive": job.motive,
            }
        )
