"""The read models the console and SDK consumers actually call.

Six members but 784 lines, because `knowledge_graph` (244) and `memory_evolution`
(321) each assemble a whole view in one pass. They are the widest reads in the
product and the ones most likely to be slow at scale."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._protocol import ComposedMemotron
from memotron.dreaming import DreamEngine
from memotron.health import DEFAULT_ELIGIBLE_TYPES, compute_memory_health
from memotron.models import (
    DreamJobKind,
    EntityNeighborhoodEdge,
    KnowledgeGraphEdge,
    KnowledgeGraphNode,
    KnowledgeGraphView,
    MemoryEvidence,
    MemoryEvolutionFact,
    MemoryEvolutionProof,
    MemoryHealthReport,
    MemoryScope,
    MemoryType,
    RelationshipStatus,
    UseEventKind,
    session_boundary_for_task_run,
)
from memotron.storage import (
    StorageBackend,
    normalize_key,
)

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class GraphViewMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def entity_neighborhood(
        self,
        *,
        scope: MemoryScope,
        entity: str,
        limit: int = 20,
        relationship_types: set[str] | None = None,
        as_of: datetime | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
    ) -> list[EntityNeighborhoodEdge]:
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if not entity.strip():
            raise ValueError("entity cannot be blank")
        # WS-17 T16b: the entity argument resolves through the ACTIVE alias
        # registry, and the neighborhood is the UNION of edges incident to the
        # canonical node and every alias node — non-destructive read-side
        # bridging (relationship endpoints are never rewritten).  Without any
        # ACTIVE alias this is the singleton exact-name match, unchanged.
        entity_match_names = (
            self._entity_alias_group_names(scope.key, entity)
            if self.config.entity_resolution.enabled
            else frozenset({normalize_key(entity)})
        )
        normalized_relationship_types = self._normalized_relationship_types(relationship_types)
        edges: list[EntityNeighborhoodEdge] = []
        for relationship in self.graph.relationships_for_scope(scope.key):
            if relationship.type == "MENTIONS":
                continue
            if normalized_relationship_types is not None and relationship.type not in normalized_relationship_types:
                continue
            status = self._relationship_status(relationship.properties.get("status"))
            if not self._relationship_is_visible(
                status=status,
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
                as_of=as_of,
                include_statuses=include_statuses,
            ):
                continue
            source_node = self.graph.get_node(relationship.source_uuid)
            target_node = self.graph.get_node(relationship.target_uuid)
            subject = str(self.graph.reveal(scope.key, source_node.properties["name"]))
            object_value = str(self.graph.reveal(scope.key, target_node.properties["name"]))
            source_matches = normalize_key(subject) in entity_match_names
            target_matches = normalize_key(object_value) in entity_match_names
            if not source_matches and not target_matches:
                continue
            direction = self._edge_direction(source_matches=source_matches, target_matches=target_matches)
            edges.append(
                EntityNeighborhoodEdge(
                    relationship_uuid=relationship.uuid,
                    relationship_type=relationship.type,
                    direction=direction,
                    fact=str(self.graph.reveal(scope.key, relationship.properties["fact"])),
                    scope=scope,
                    subject=subject,
                    predicate=str(relationship.properties["predicate"]),
                    object=object_value,
                    confidence=float(relationship.properties["confidence"]),
                    status=status,
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    episode_uuid=str(relationship.properties["episode_uuid"]),
                    episode_uuids=self._relationship_episode_uuids(relationship.properties),
                    observed_count=int(relationship.properties.get("observed_count", 1)),
                    created_by=str(relationship.properties.get("created_by", "")),
                    superseded_by_relationship_uuid=self._optional_string(
                        relationship.properties.get("superseded_by_relationship_uuid")
                    ),
                    pruned_reason=self._optional_string(relationship.properties.get("pruned_reason")),
                    metadata=self._relationship_metadata(relationship.properties),
                )
            )
        edges.sort(
            key=lambda edge: (
                self._relationship_status_rank(edge.status),
                -self._datetime_sort_value(edge.valid_from),
                -edge.confidence,
                -edge.observed_count,
                edge.relationship_uuid,
            )
        )
        return edges[:limit]

    async def knowledge_graph(
        self,
        *,
        scope: MemoryScope,
        limit: int = 250,
        relationship_types: set[str] | None = None,
        as_of: datetime | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
        include_demoted: bool = True,
        query: str | None = None,
    ) -> KnowledgeGraphView:
        """Project scoped memory relationships into a UI-friendly graph.

        Memory facts are first-class nodes.  Each scoped graph relationship is
        represented as entity -> memory fact -> entity so selection, evidence,
        truth timelines, supersession, and rollup derivation attach to the fact
        rather than being lost on a plain entity edge.
        """
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        normalized_relationship_types = self._normalized_relationship_types(relationship_types)
        query_tokens = set(normalize_key(query or "").split())
        selected_relationships = []
        for relationship in self.graph.relationships_for_scope(scope.key):
            if relationship.type == "MENTIONS":
                continue
            if normalized_relationship_types is not None and relationship.type not in normalized_relationship_types:
                continue
            if relationship.properties.get("active_in_context") is False and not include_demoted:
                continue
            status = self._relationship_status(relationship.properties.get("status"))
            if not self._relationship_is_visible(
                status=status,
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
                as_of=as_of,
                include_statuses=include_statuses,
            ):
                continue
            source_node = self.graph.get_node(relationship.source_uuid)
            target_node = self.graph.get_node(relationship.target_uuid)
            subject = str(self.graph.reveal(scope.key, source_node.properties["name"]))
            object_value = str(self.graph.reveal(scope.key, target_node.properties["name"]))
            if query_tokens:
                searchable = normalize_key(
                    " ".join(
                        [
                            subject,
                            str(relationship.properties.get("predicate", "")),
                            object_value,
                            str(self.graph.reveal(scope.key, relationship.properties.get("fact", ""))),
                            self._node_search_text(source_node.properties),
                            self._node_search_text(target_node.properties),
                        ]
                    )
                )
                if not query_tokens.intersection(searchable.split()):
                    continue
            selected_relationships.append(relationship)

        selected_relationships.sort(
            key=lambda relationship: (
                self._relationship_status_rank(self._relationship_status(relationship.properties.get("status"))),
                relationship.properties.get("active_in_context") is False,
                -self._datetime_sort_value(relationship.valid_from),
                -float(relationship.properties.get("confidence", 0.0)),
                relationship.uuid,
            )
        )
        selected_relationships = selected_relationships[:limit]

        nodes_by_id: dict[str, KnowledgeGraphNode] = {}
        edges_by_id: dict[str, KnowledgeGraphEdge] = {}
        fact_node_ids_by_relationship_uuid: dict[str, str] = {}
        distribution: dict[str, int] = {}
        status_distribution: dict[str, int] = {}
        context_visible_count = 0
        demoted_count = 0
        rollup_count = 0

        def filtered_properties(properties: dict[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in properties.items() if key != "embedding"}

        def entity_node_id(node_uuid: str) -> str:
            return f"entity:{node_uuid}"

        def memory_node_id(relationship_uuid: str) -> str:
            return f"memory:{relationship_uuid}"

        def add_entity_node(node_uuid: str) -> str:
            graph_node = self.graph.get_node(node_uuid)
            node_id = entity_node_id(node_uuid)
            if node_id not in nodes_by_id:
                nodes_by_id[node_id] = KnowledgeGraphNode(
                    id=node_id,
                    label=str(
                        self.graph.reveal(
                            scope.key,
                            graph_node.properties.get("name", graph_node.properties.get("graph_key", node_uuid)),
                        )
                    ),
                    node_type="entity",
                    graph_uuid=graph_node.uuid,
                    labels=graph_node.labels,
                    valid_from=graph_node.valid_from,
                    valid_to=graph_node.valid_to,
                    properties=filtered_properties(graph_node.properties),
                )
            return node_id

        for relationship in selected_relationships:
            props = relationship.properties
            status = self._relationship_status(props.get("status"))
            source_node = self.graph.get_node(relationship.source_uuid)
            target_node = self.graph.get_node(relationship.target_uuid)
            subject = str(self.graph.reveal(scope.key, source_node.properties["name"]))
            object_value = str(self.graph.reveal(scope.key, target_node.properties["name"]))
            memory_type = DreamEngine.memory_type_for_relationship(dict(props))
            active_in_context = props.get("active_in_context") is not False
            if memory_type:
                distribution[memory_type] = distribution.get(memory_type, 0) + 1
            status_distribution[status.value] = status_distribution.get(status.value, 0) + 1
            if status == RelationshipStatus.ACTIVE and not active_in_context:
                demoted_count += 1
            if (
                status == RelationshipStatus.ACTIVE
                and active_in_context
                and self._relationship_window_contains(
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    instant=as_of or datetime.now(UTC),
                )
            ):
                context_visible_count += 1
                if memory_type == MemoryType.ROLLUP.value:
                    rollup_count += 1

            source_id = add_entity_node(relationship.source_uuid)
            target_id = add_entity_node(relationship.target_uuid)
            fact_id = memory_node_id(relationship.uuid)
            fact_node_ids_by_relationship_uuid[relationship.uuid] = fact_id
            nodes_by_id[fact_id] = KnowledgeGraphNode(
                id=fact_id,
                label=str(
                    self.graph.reveal(
                        scope.key,
                        props.get("fact", f"{subject} {props.get('predicate', '')} {object_value}"),
                    )
                ),
                node_type="memory",
                scope=scope,
                graph_uuid=relationship.uuid,
                relationship_uuid=relationship.uuid,
                relationship_type=relationship.type,
                memory_type=memory_type,
                status=status,
                active_in_context=active_in_context,
                confidence=float(props.get("confidence", 0.0)),
                observed_count=int(props.get("observed_count", 1)),
                valid_from=relationship.valid_from,
                valid_to=relationship.valid_to,
                properties={
                    **filtered_properties(props),
                    "subject": subject,
                    "object": object_value,
                    "episode_uuids": self._relationship_episode_uuids(props),
                },
            )
            edges_by_id[f"edge:{relationship.uuid}:subject"] = KnowledgeGraphEdge(
                id=f"edge:{relationship.uuid}:subject",
                source_id=source_id,
                target_id=fact_id,
                edge_type="subject",
                label="subject",
                relationship_uuid=relationship.uuid,
            )
            edges_by_id[f"edge:{relationship.uuid}:object"] = KnowledgeGraphEdge(
                id=f"edge:{relationship.uuid}:object",
                source_id=fact_id,
                target_id=target_id,
                edge_type="object",
                label=str(props.get("predicate", relationship.type.lower())),
                relationship_uuid=relationship.uuid,
            )

        for relationship in selected_relationships:
            fact_id = fact_node_ids_by_relationship_uuid.get(relationship.uuid)
            if fact_id is None:
                continue
            superseded_by = self._optional_string(relationship.properties.get("superseded_by_relationship_uuid"))
            if superseded_by is not None and superseded_by in fact_node_ids_by_relationship_uuid:
                edge_id = f"edge:{relationship.uuid}:superseded_by:{superseded_by}"
                edges_by_id[edge_id] = KnowledgeGraphEdge(
                    id=edge_id,
                    source_id=fact_id,
                    target_id=fact_node_ids_by_relationship_uuid[superseded_by],
                    edge_type="superseded_by",
                    label="superseded by",
                    relationship_uuid=relationship.uuid,
                )
            derived_from = relationship.properties.get("derived_from", [])
            if isinstance(derived_from, list):
                for raw_member_uuid in derived_from:
                    member_uuid = str(raw_member_uuid)
                    if member_uuid not in fact_node_ids_by_relationship_uuid:
                        continue
                    edge_id = f"edge:{relationship.uuid}:derived_from:{member_uuid}"
                    edges_by_id[edge_id] = KnowledgeGraphEdge(
                        id=edge_id,
                        source_id=fact_id,
                        target_id=fact_node_ids_by_relationship_uuid[member_uuid],
                        edge_type="derived_from",
                        label="derived from",
                        relationship_uuid=relationship.uuid,
                    )

        nodes = sorted(
            nodes_by_id.values(),
            key=lambda node: (
                0 if node.node_type == "entity" else 1,
                node.label.lower(),
                node.id,
            ),
        )
        edges = sorted(edges_by_id.values(), key=lambda edge: edge.id)
        return KnowledgeGraphView(
            scope=scope,
            as_of=as_of,
            nodes=nodes,
            edges=edges,
            relationship_count=len(selected_relationships),
            memory_type_distribution=distribution,
            status_distribution=status_distribution,
            context_visible_relationship_count=context_visible_count,
            demoted_relationship_count=demoted_count,
            rollup_relationship_count=rollup_count,
        )

    async def memory_evidence(
        self,
        *,
        relationship_uuid: str,
        scope: MemoryScope | None = None,
        reader_agent_id: str | None = None,
    ) -> MemoryEvidence:
        """Current fact state, lineage, and source evidence for one memory.

        WS-19 T22: ``reader_agent_id`` enforces the per-memory
        ``visibility_agents`` allowlist — a restricted row fails closed for a
        non-listed agent; ``None`` (operator / SDK-owner context) sees
        everything.  WS-19 T20: a fact materialized from an object-level
        promotion carries a ``promotion_lineage`` block resolving the chain
        fact → candidate episode → source relationship.
        """
        if not relationship_uuid.strip():
            raise ValueError("relationship_uuid cannot be blank")
        relationship = self.graph.get_relationship(relationship_uuid)
        if relationship.type == "MENTIONS":
            raise ValueError("MENTIONS relationships do not have memory evidence")
        relationship_scope = self._relationship_scope(relationship.properties)
        # WS-19 T21: guard the RESOLVED row scope so an out-of-set row cannot
        # be reached by omitting the scope argument.
        self._require_authorized_scope(relationship_scope)
        if scope is not None and relationship_scope != scope:
            raise ValueError(f"relationship scope {relationship_scope.key} does not match requested scope {scope.key}")
        if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
            raise ValueError(
                f"relationship {relationship.uuid} is not visible to agent "
                f"{reader_agent_id!r} (per-memory visibility allowlist)"
            )
        source_node = self.graph.get_node(relationship.source_uuid)
        target_node = self.graph.get_node(relationship.target_uuid)
        episode_uuids = self._relationship_episode_uuids(relationship.properties)
        scope_key = relationship_scope.key
        return MemoryEvidence(
            relationship_uuid=relationship.uuid,
            relationship_type=relationship.type,
            fact=str(self.graph.reveal(scope_key, relationship.properties["fact"])),
            scope=relationship_scope,
            subject=str(self.graph.reveal(scope_key, source_node.properties["name"])),
            predicate=str(relationship.properties["predicate"]),
            object=str(self.graph.reveal(scope_key, target_node.properties["name"])),
            confidence=float(relationship.properties["confidence"]),
            status=self._relationship_status(relationship.properties.get("status")),
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            episode_uuids=episode_uuids,
            observed_count=int(relationship.properties.get("observed_count", 1)),
            created_by=str(relationship.properties.get("created_by", "")),
            source_text=self._optional_string(self.graph.reveal(scope_key, relationship.properties.get("source_text"))),
            metadata=self._relationship_metadata(relationship.properties),
            episodes=[self._evidence_episode(self.graph.get_episode(episode_uuid)) for episode_uuid in episode_uuids],
            promotion_lineage=self._promotion_lineage(relationship, episode_uuids),
        )

    def _promotion_lineage(self, relationship: Any, episode_uuids: list[str]) -> dict[str, Any] | None:
        """WS-19 T20: resolve fact → candidate episode → source relationship.

        Present only for rows carrying the first-class
        ``promoted_from_relationship_uuid`` lineage stamped at materialization.
        The candidate episode is located among the row's evidence episodes by
        its promotion metadata, and the ledger supplies every endorser.
        """
        promoted_from = relationship.properties.get("promoted_from_relationship_uuid")
        if not promoted_from:
            return None
        lineage: dict[str, Any] = {
            "source_relationship_uuid": str(promoted_from),
            "source_scope_key": str(relationship.properties.get("promoted_from_scope_key", "")),
        }
        for episode_uuid in episode_uuids:
            try:
                episode = self.graph.get_episode(episode_uuid)
            except ValueError:
                continue
            metadata = episode.metadata
            if metadata.get("promotion") is not True:
                continue
            if str(metadata.get("source_relationship_uuid", "")) != str(promoted_from):
                continue
            endorsements = self.graph.promotion_endorsements_for(episode.uuid)
            lineage.update(
                {
                    "candidate_episode_uuid": episode.uuid,
                    "source_receipt_digest": str(metadata.get("source_receipt_digest", "")),
                    "promoted_by": str(metadata.get("promoted_by", "")),
                    "endorsements": [row["agent_id"] for row in endorsements],
                    "endorsement_count": len(endorsements),
                }
            )
            break
        return lineage

    async def memory_evolution(
        self,
        *,
        scope: MemoryScope,
        as_of: datetime | None = None,
        history_limit: int = 1000,
        token_budget: int | None = None,
    ) -> MemoryEvolutionProof:
        """Scoped evolution proof: lifecycle counts, facts, signals, and (WS-22)
        token-savings and repeat-search measurements.

        Token accounting (T28) uses the platform's single deterministic
        estimator (:meth:`_estimate_tokens`, ceiling 4 chars/token) and the
        exact profile line format, decrypt-on-read; crypto-shredded content
        contributes its placeholder length.  ``token_budget`` drives ONLY the
        ``tokens_rendered_profile`` reference render — ``None`` (default)
        measures the unbudgeted default render of the CURRENT visible set, so
        the field is meaningful without choosing a budget.  Repeat-search and
        answered-from-profile telemetry (T29) is event-plane and rebuildable:
        it reads the scope's use events exactly like the utility projection
        (``used_at <= anchor``) and is ``None`` — never a fake 0.0 — when the
        denominator is empty.
        """
        self._require_authorized_scope(scope)
        if history_limit <= 0:
            raise ValueError("history_limit must be greater than zero")
        if token_budget is not None and token_budget < 0:
            raise ValueError("token_budget must be non-negative")
        anchor = as_of or datetime.now(UTC)
        scope_episodes = [episode for episode in self.graph.episodes() if episode.scope == scope]
        processed_episode_count = sum(1 for episode in scope_episodes if self.graph.is_episode_processed(episode.uuid))
        active_facts: list[MemoryEvolutionFact] = []
        inactive_facts: list[MemoryEvolutionFact] = []

        # WS-6: Track context-visible subset and demotion counts alongside the main loop.
        # context-visible active = status==ACTIVE AND validity window passes
        #                          AND active_in_context is not False AND type != "MENTIONS"
        context_visible_count = 0
        rollup_count = 0
        demoted_count = 0
        per_type_active: dict[str, int] = {}

        # WS-22 T28: per-row profile-line token accounting.  Every row's line is
        # counted once (line + newline, matching the budget pass) so demotion
        # savings can resolve rollup/survivor replacement lines from the same
        # map even when the replacement is itself superseded (stale rebuild).
        line_tokens_by_uuid: dict[str, int] = {}
        # WS-28 T4: memory_type per row, read off the SAME loop below so the
        # injected_waste_rate partition never needs a second graph pass.
        memory_type_by_uuid: dict[str, str] = {}
        tokens_unbudgeted_facts = 0
        demoted_line_tokens = 0
        demoted_replacement_uuids: set[str] = set()

        for relationship in self._scope_memory_relationships(scope):
            memory_type_by_uuid[relationship.uuid] = str(relationship.properties.get("memory_type", "")) or "unknown"
            fact = self._memory_evolution_fact(relationship, scope=scope)
            line = self._profile_fact_line(
                relationship_type=fact.relationship_type,
                fact=fact.fact,
                confidence=fact.confidence,
                observed_count=fact.observed_count,
                pinned=relationship.properties.get("pinned") is True,
            )
            line_tokens = self._estimate_tokens(line + "\n")
            line_tokens_by_uuid[relationship.uuid] = line_tokens
            if self._relationship_is_current_active(relationship, as_of=anchor):
                active_facts.append(fact)
                # WS-6: Determine context-visibility for this active relationship.
                is_demoted = relationship.properties.get("active_in_context") is False
                if is_demoted:
                    demoted_count += 1
                    # WS-22 T28: demotion savings cover the two demotion
                    # mechanisms — rollup members (rolled_up_by) and
                    # cross-prefix duplicates (duplicate_of).
                    replacement = relationship.properties.get("rolled_up_by") or relationship.properties.get(
                        "duplicate_of"
                    )
                    if replacement:
                        demoted_line_tokens += line_tokens
                        demoted_replacement_uuids.add(str(replacement))
                else:
                    context_visible_count += 1
                    tokens_unbudgeted_facts += line_tokens
                    mem_type = str(relationship.properties.get("memory_type", "")) or "unknown"
                    per_type_active[mem_type] = per_type_active.get(mem_type, 0) + 1
                    if mem_type == MemoryType.ROLLUP.value:
                        rollup_count += 1
            else:
                inactive_facts.append(fact)

        active_facts.sort(
            key=lambda fact: (
                fact.confidence,
                fact.observed_count,
                self._datetime_sort_value(fact.valid_from),
                fact.relationship_uuid,
            ),
            reverse=True,
        )
        inactive_facts.sort(
            key=lambda fact: (
                self._relationship_status_rank(fact.status),
                -self._datetime_sort_value(fact.valid_from),
                fact.relationship_uuid,
            )
        )
        decisions = [
            decision for decision in self.graph.dream_decisions(limit=history_limit) if decision.scope == scope
        ]
        dream_runs = self.graph.dream_job_runs(limit=history_limit)
        reinforced_count = sum(1 for fact in [*active_facts, *inactive_facts] if fact.observed_count > 1)
        superseded_count = sum(1 for fact in inactive_facts if fact.status == RelationshipStatus.SUPERSEDED)
        pruned_count = sum(1 for fact in inactive_facts if fact.status == RelationshipStatus.PRUNED)
        consolidation_count = sum(
            1
            for fact in [*active_facts, *inactive_facts]
            if fact.created_by.endswith(":consolidation")
            or fact.metadata.get("dream_job_kind") == DreamJobKind.CONSOLIDATION.value
        )

        # WS-6: Compute compression_ratio and semantic_dedup_rate.
        # compression_ratio = episode_count / max(1, context_visible_relationship_count).
        episode_count = len(scope_episodes)
        compression_ratio = episode_count / max(1, context_visible_count)

        # semantic_dedup_rate = reinforced_observations / max(1, total_observations).
        # total_observations = sum of observed_count across all facts (each row was
        # observed at least once at creation; extra counts are reinforcements).
        # reinforced_observations = total_observations - unique_fact_count.
        all_facts = [*active_facts, *inactive_facts]
        total_observations = sum(f.observed_count for f in all_facts)
        unique_fact_count = len(all_facts)
        reinforced_observations = max(0, total_observations - unique_fact_count)
        semantic_dedup_rate = reinforced_observations / max(1, total_observations)

        # WS-22 T28: token savings vs. the raw-episode baseline — one estimator
        # everywhere.  Raw episode bodies decrypt-on-read; a crypto-shredded
        # body resolves to the placeholder and contributes its length.
        tokens_raw_episodes = sum(
            self._estimate_tokens(str(self.graph.reveal(scope.key, episode.body))) for episode in scope_episodes
        )
        if context_visible_count == 0:
            # An empty working set renders no context: the reference render is
            # 0, not the header boilerplate — so every field is 0 on an empty
            # scope (never absent).
            tokens_rendered_profile = 0
        else:
            rendered_profile = await self.profile(scope=scope, as_of=as_of, token_budget=token_budget)
            tokens_rendered_profile = self._estimate_tokens(rendered_profile.rendered_context)
        tokens_saved_vs_raw = max(0, tokens_raw_episodes - tokens_rendered_profile)
        replacement_line_tokens = sum(
            line_tokens_by_uuid.get(replacement_uuid, 0) for replacement_uuid in demoted_replacement_uuids
        )
        tokens_saved_by_demotion = max(0, demoted_line_tokens - replacement_line_tokens)

        # WS-22 T29: event-plane telemetry, read exactly like the utility
        # projection reads events (used_at <= anchor) so it is rebuildable.
        scope_use_events = [event for event in self.graph.use_events(scope_key=scope.key) if event.used_at <= anchor]
        digest_boundaries: dict[str, set[str]] = {}
        origin_kinds: dict[tuple[str, str], set[UseEventKind]] = {}
        for event in scope_use_events:
            boundary = session_boundary_for_task_run(event.task_run_id)
            if event.kind == UseEventKind.RETRIEVED and event.query_digest:
                digest_boundaries.setdefault(event.query_digest, set()).add(boundary)
            if event.kind in (UseEventKind.RETRIEVED, UseEventKind.INJECTED):
                origin_kinds.setdefault((boundary, event.relationship_uuid), set()).add(event.kind)
        repeat_search_rate: float | None = None
        if digest_boundaries:
            repeated_digests = sum(1 for boundaries in digest_boundaries.values() if len(boundaries) > 1)
            repeat_search_rate = repeated_digests / len(digest_boundaries)
        answered_from_profile = 0
        answered_from_search = 0
        for event in scope_use_events:
            if event.kind != UseEventKind.CITED_OR_USED:
                continue
            # The citation scanner recorded each hit against the session's own
            # INJECTED/RETRIEVED candidates (same relationship, same
            # ``claude:{session_id}:`` lineage) — this join resolves the
            # originating kind the same way.  INJECTED wins when both exist:
            # the profile already held the fact, so no search was needed.
            originating = origin_kinds.get(
                (session_boundary_for_task_run(event.task_run_id), event.relationship_uuid),
                set(),
            )
            if UseEventKind.INJECTED in originating:
                answered_from_profile += 1
            elif UseEventKind.RETRIEVED in originating:
                answered_from_search += 1
        answered_from_profile_rate: float | None = None
        resolved_citations = answered_from_profile + answered_from_search
        if resolved_citations:
            answered_from_profile_rate = answered_from_profile / resolved_citations

        # WS-28 T4: injected_waste_rate — the direct, measured cost of a
        # bloated context brief.  A (session boundary, relationship) pair
        # counts as INJECTED impression exactly like `answered_from_profile`
        # above joins it; "wasted" is INJECTED with no CITED_OR_USED hit on
        # that same pair.  Token-weighted with the SAME per-uuid line-token
        # estimate `tokens_unbudgeted_facts` already uses, so this reads the
        # profile-line cost of an injection, not a raw fact count.
        import memotron.context as context_module

        cited_pairs = {
            (session_boundary_for_task_run(event.task_run_id), event.relationship_uuid)
            for event in scope_use_events
            if event.kind == UseEventKind.CITED_OR_USED
        }
        injected_pairs = {pair for pair, kinds in origin_kinds.items() if UseEventKind.INJECTED in kinds}
        tokens_injected_total = 0
        tokens_injected_wasted = 0
        tokens_injected_total_by_type: dict[str, int] = {}
        tokens_injected_wasted_by_type: dict[str, int] = {}
        tokens_injected_total_by_role: dict[str, int] = {}
        tokens_injected_wasted_by_role: dict[str, int] = {}
        for _boundary, relationship_uuid in injected_pairs:
            tokens = line_tokens_by_uuid.get(relationship_uuid, 0)
            mem_type = memory_type_by_uuid.get(relationship_uuid, "unknown")
            role = context_module.role_for_memory_type(mem_type).value
            tokens_injected_total += tokens
            tokens_injected_total_by_type[mem_type] = tokens_injected_total_by_type.get(mem_type, 0) + tokens
            tokens_injected_total_by_role[role] = tokens_injected_total_by_role.get(role, 0) + tokens
            if (_boundary, relationship_uuid) not in cited_pairs:
                tokens_injected_wasted += tokens
                tokens_injected_wasted_by_type[mem_type] = tokens_injected_wasted_by_type.get(mem_type, 0) + tokens
                tokens_injected_wasted_by_role[role] = tokens_injected_wasted_by_role.get(role, 0) + tokens
        injected_waste_rate: float | None = None
        if tokens_injected_total > 0:
            injected_waste_rate = tokens_injected_wasted / tokens_injected_total
        injected_waste_rate_by_type = {
            mem_type: tokens_injected_wasted_by_type.get(mem_type, 0) / total
            for mem_type, total in tokens_injected_total_by_type.items()
            if total > 0
        }
        injected_waste_rate_by_role = {
            role: tokens_injected_wasted_by_role.get(role, 0) / total
            for role, total in tokens_injected_total_by_role.items()
            if total > 0
        }

        proof = MemoryEvolutionProof(
            scope=scope,
            as_of=anchor,
            episode_count=episode_count,
            processed_episode_count=processed_episode_count,
            pending_episode_count=episode_count - processed_episode_count,
            dream_run_count=len(dream_runs),
            decision_count=len(decisions),
            created_relationship_count=len(active_facts) + len(inactive_facts),
            reinforced_relationship_count=reinforced_count,
            superseded_relationship_count=superseded_count,
            pruned_relationship_count=pruned_count,
            consolidation_relationship_count=consolidation_count,
            active_relationship_count=len(active_facts),
            inactive_relationship_count=len(inactive_facts),
            active_facts=active_facts,
            inactive_facts=inactive_facts,
            # WS-6 additive fields:
            context_visible_relationship_count=context_visible_count,
            rollup_relationship_count=rollup_count,
            demoted_relationship_count=demoted_count,
            compression_ratio=compression_ratio,
            semantic_dedup_rate=semantic_dedup_rate,
            per_type_active_counts=per_type_active,
            # WS-22 T28 token-savings measurement:
            tokens_raw_episodes=tokens_raw_episodes,
            tokens_unbudgeted_facts=tokens_unbudgeted_facts,
            tokens_rendered_profile=tokens_rendered_profile,
            tokens_saved_vs_raw=tokens_saved_vs_raw,
            tokens_saved_by_demotion=tokens_saved_by_demotion,
            # WS-22 T29 event-plane telemetry:
            repeat_search_rate=repeat_search_rate,
            answered_from_profile_rate=answered_from_profile_rate,
            # WS-28 T4 context-pollution accounting:
            injected_waste_rate=injected_waste_rate,
            injected_waste_rate_by_type=injected_waste_rate_by_type,
            injected_waste_rate_by_role=injected_waste_rate_by_role,
            # WS-24: type-distribution health.  Same measurements the formation
            # gates apply, computed from the same recipe — the read side and the
            # write side never disagree about whether a graph is healthy.
            health=self._scope_health_report(
                scope=scope,
                per_type_counts=per_type_active,
                admitted_count=len(all_facts),
                evaluated_at=anchor,
            ),
        )
        proof.signals.extend(self._memory_evolution_signals(proof))
        return proof

    def _scope_health_report(
        self,
        *,
        scope: MemoryScope,
        per_type_counts: dict[str, int],
        admitted_count: int,
        evaluated_at: datetime,
    ) -> MemoryHealthReport | None:
        """WS-24: the scope's lifetime health, for the evolution proof.

        The abstain rate here uses LIFETIME denominators — every candidate this
        scope ever quarantined over every fact it ever materialized — because
        the proof describes the scope, not one run.  ``DreamJobRun.health``
        carries the per-run rate.
        """
        policy = self.config.health
        if not policy.enabled:
            return None
        quarantine_counts = self.graph.quarantine_counts(scope_key=scope.key)
        return compute_memory_health(
            scope=scope,
            per_type_counts=per_type_counts,
            policy=policy,
            evaluated_at=evaluated_at,
            quarantined_count=sum(quarantine_counts.values()),
            admitted_count=admitted_count,
            eligible_types=DEFAULT_ELIGIBLE_TYPES,
        )
