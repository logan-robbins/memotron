"""Search: candidates, expansion, visibility filtering, ranking.

Twelve members and the largest group at 938 lines. `search_context` (260) is the
whole pipeline; the private helpers are its stages. `semantic_search` is here rather
than with ingestion despite sitting in that block by line -- it is a retrieval method
that ended up there by accident of authorship.

The visibility filtering matters more than the ranking: `_retrieval_row_visible` and
`_retrieval_searchable` decide what a caller is ALLOWED to see, and a bug there
returns a plausible result set that quietly includes or omits the wrong rows."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._common import _AWARE_MIN
from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    Motive,
    RetrievalPolicy,
)
from memotron.crypto import is_sealed_content
from memotron.embedding import (
    EmbeddingTransport,
    LocalEmbeddingTransport,
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.models import (
    MemoryScope,
    MemoryType,
    RelationshipStatus,
    SearchResult,
    SearchResults,
)
from memotron.retrieval import (
    CandidateOrigin,
    ScoredCandidate,
    TemporalAuthorityCandidate,
    build_retrieval_contract,
    demote_contradicted_same_slot,
    effective_type_weight,
    lexical_overlap,
    recency_decay,
    relevance_score,
    rerank_score,
    resolve_effective_utility_weight,
    retrieval_contract_digest,
    scope_priority,
    use_need,
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


class RetrievalMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def semantic_search(
        self,
        *,
        query: str,
        scope: MemoryScope,
        limit: int = 10,
        as_of: datetime | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
        min_similarity: float = 0.0,
        reader_agent_id: str | None = None,
    ) -> SearchResults:
        """Semantic (embedding-cosine) search over active graph relationships.

        Uses the WS-1 embedding substrate stored on each relationship
        (``properties["embedding"]``).  Computes cosine similarity between
        the query embedding and every eligible relationship embedding.

        This is a linear scan (consistent with the existing ``relationships()``
        scan used by keyword search).  A full ANN index is out of scope for WS-9;
        this implementation is correct and adds recall on top of keyword search.

        The default ``search()`` keyword behaviour is byte-for-byte unchanged.
        Semantic search is opt-in via this method or ``search(semantic=True)``.

        Parameters
        ----------
        query:
            Natural-language query string.
        scope:
            Memory scope to search.
        limit:
            Maximum number of results.
        as_of:
            Historical search instant.
        include_statuses:
            Status filter (default: active only).
        min_similarity:
            Minimum cosine similarity threshold (0.0 = no floor).
        reader_agent_id:
            WS-19 T22: the calling agent for the per-memory
            ``visibility_agents`` allowlist (fail-closed for agents; ``None``
            operator context sees everything).
        """
        self._require_authorized_scope(scope)
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if not query.strip():
            return SearchResults([], archived=self._empty_archived_report(query=query, scopes=[scope], semantic=True))

        archived_report = (
            await self._retrieval_archived_report(
                query=query, scopes=[scope], semantic=True, reader_agent_id=reader_agent_id
            )
            if as_of is None
            else self._empty_archived_report(query=query, scopes=[scope], semantic=True)
        )

        # Embed the query in the SCOPE's vector space.  WS-23 H1: a
        # content-protected scope is pinned to the hermetic local transport on
        # both sides, so its sealed content is never re-embedded through a
        # network endpoint and stored/query vectors stay same-space.
        embedding_transport = self._engine.content_embedding_transport(scope_key=scope.key)
        query_embedding: list[float] = embedding_transport.embed(query)

        results: list[SearchResult] = []
        for relationship in self.graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            if relationship.properties.get("scope_key") != scope.key:
                continue
            # WS-19 T22: per-memory agent allowlist — fail-closed for agents,
            # open for the operator / SDK-owner context (reader None).
            if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
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
            # A dependency-invalidated rollup is never a current retrieval
            # candidate.  Its children (or resolved successors) are the safe
            # fallback until the canonical reducer recomputes it.  Historical
            # queries may still inspect the prior view.
            if as_of is None and relationship.properties.get("rollup_stale") is True:
                continue

            # Retrieve stored embedding from relationship properties
            # (WS-12: revealed under the live scope DEK when sealed).
            # WS-17 T18 vector-space guard: a stored vector participates only
            # when its stamped identifier matches the active transport; a
            # mismatched or unstamped (legacy) vector is treated as absent and
            # the revealed fact text is re-embedded in the ACTIVE space.
            stored_embedding = (
                self.graph.reveal_vector(scope.key, relationship.properties.get("embedding"))
                if stored_vector_in_active_space(
                    relationship.properties,
                    active_identifier=embedding_transport.identifier,
                )
                else None
            )
            if stored_embedding is None:
                # Relationship pre-dates WS-1, has no embedding, or its stored
                # vector lives in a different space; compute on-the-fly
                # from the fact text so semantic search still works.
                source_node = self.graph.get_node(relationship.source_uuid)
                target_node = self.graph.get_node(relationship.target_uuid)
                fact_text = " ".join(
                    [
                        str(self.graph.reveal(scope.key, source_node.properties.get("name", ""))),
                        str(relationship.properties.get("predicate", "")),
                        str(self.graph.reveal(scope.key, target_node.properties.get("name", ""))),
                        str(self.graph.reveal(scope.key, relationship.properties.get("fact", ""))),
                    ]
                )
                stored_embedding = embedding_transport.embed(fact_text)

            try:
                similarity = cosine_similarity(query_embedding, list(stored_embedding))
            except ValueError:
                # Dimension mismatch (different transport at materialisation time).
                continue

            if similarity < min_similarity:
                continue

            source_node = self.graph.get_node(relationship.source_uuid)
            target_node = self.graph.get_node(relationship.target_uuid)
            subject = self.graph.reveal(scope.key, source_node.properties["name"])
            object_value = self.graph.reveal(scope.key, target_node.properties["name"])

            results.append(
                SearchResult(
                    relationship_uuid=relationship.uuid,
                    fact=str(self.graph.reveal(scope.key, relationship.properties["fact"])),
                    scope=scope,
                    scope_rank=0,
                    subject=str(subject),
                    predicate=str(relationship.properties["predicate"]),
                    object=str(object_value),
                    confidence=float(relationship.properties["confidence"]),
                    status=status,
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    episode_uuid=str(relationship.properties["episode_uuid"]),
                    episode_uuids=self._relationship_episode_uuids(relationship.properties),
                    observed_count=int(relationship.properties.get("observed_count", 1)),
                    # Use similarity * 1000 scaled to int for score field (for sorting).
                    score=int(similarity * 1000),
                )
            )

        results.sort(
            key=lambda result: (
                result.score,
                result.confidence,
                # Aware sentinel: every stored datetime is timezone-aware, and
                # `valid_from` is Optional, so a naive datetime.min here raises
                # "can't compare offset-naive and offset-aware datetimes" as soon
                # as one result lacks valid_from and another has it.
                result.valid_from or _AWARE_MIN,
            ),
            reverse=True,
        )
        return SearchResults(results[:limit], archived=archived_report)

    async def search(
        self,
        *,
        query: str,
        scope: MemoryScope,
        limit: int = 10,
        as_of: datetime | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
        relationship_types: set[str] | None = None,
        motive: Motive | str | None = None,
        reader_agent_id: str | None = None,
    ) -> SearchResults:
        """Keyword search over one scope (see ``search_context`` for the contract)."""
        return await self.search_context(
            query=query,
            scopes=[scope],
            limit=limit,
            as_of=as_of,
            include_statuses=include_statuses,
            relationship_types=relationship_types,
            motive=motive,
            reader_agent_id=reader_agent_id,
        )

    async def search_context(
        self,
        *,
        query: str,
        scopes: list[MemoryScope],
        limit: int = 10,
        as_of: datetime | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
        relationship_types: set[str] | None = None,
        motive: Motive | str | None = None,
        reader_agent_id: str | None = None,
    ) -> SearchResults:
        """WS-5: Deterministic six-stage retrieval pipeline.

        1. Scope + temporal filtering — index-backed per-scope read; status,
           validity-window, and ``rollup_stale`` checks (demoted members stay
           directly searchable: search is the audit surface, profile() is the
           two-tier working-context surface).
        2. Candidate generation — normalized lexical overlap mixed with
           hashed-trigram embedding cosine (stored vectors, decrypt-on-read).
        3. Seed-node resolution — entity nodes matching the query.
        4. Bounded weighted expansion — beam over entity hops plus
           ROLLUP→member (``derived_from``) and member→ROLLUP (``rolled_up_by``)
           links; relevance decays by the policy edge weights per hop.
        5. Current-truth + governance filtering — every expanded candidate
           passes the same stage-1 visibility rules.
        6. Weighted rerank — relevance, confidence, recency decay, scope
           priority, and (WS-15 T11, when ``RetrievalPolicy.utility_weight``
           > 0) the receipt-derived utility term ``use_need`` batch-read from
           the rebuildable projection for the final candidate set, decayed
           against the SAME anchor as recency — multiplied by per-type and
           Motive weights.  WS-20 T23:
           pinned rows of the searched scopes that pass the stage-1/5
           visibility rules are ALWAYS in the final result list — reserved
           slots ahead of the ranked fill (pins rerank-ordered among
           themselves, then the top non-pinned results up to ``limit``; when
           pins alone exceed the limit, pins win in that deterministic order).
           Pins are row state, not policy — the RetrievalContract is unchanged.

        The resolved policy is pinned into a RetrievalContract; its digest is
        carried on every SearchResult (``retrieval_policy_digest``) so callers
        stamp it onto use events — retrieval stays replayable and certifiable.

        WS-19 T22: ``reader_agent_id`` identifies the calling agent for the
        per-memory ``visibility_agents`` allowlist — restricted rows are
        fail-closed against it in every stage (candidates, expansion, pinned
        sweep).  ``None`` (default) is the operator / SDK-owner context and
        sees everything; the RetrievalContract is unchanged (visibility is row
        state, not ranking policy).
        """
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if not scopes:
            raise ValueError("at least one scope is required")
        self._require_authorized_scope(*scopes)
        policy = self.config.retrieval
        resolved_motive: Motive | None = None
        if motive is not None:
            if isinstance(motive, str):
                if self.config.memory_bank is None:
                    raise ValueError("cannot resolve a motive by name: config.memory_bank is not set")
                resolved_motive = self.config.memory_bank.motive(motive)
            else:
                resolved_motive = motive
        normalized_types = self._normalized_relationship_types(relationship_types)
        embedding_transport = self._engine._embedding_transport or LocalEmbeddingTransport()
        contract = build_retrieval_contract(
            operation="search_context",
            scope_keys=tuple(scope.key for scope in scopes),
            query=query,
            limit=limit,
            policy=policy,
            # WS-17 T18: pin the ACTIVE transport's protocol identifier — the
            # contract digest names the exact vector space that produced the
            # result set, and changes whenever the space changes.
            embedding_identifier=embedding_transport.identifier,
            motive=resolved_motive.name if resolved_motive is not None else None,
            as_of=as_of,
        )
        contract_digest = retrieval_contract_digest(contract)
        query_tokens = frozenset(normalize_key(query).split())
        query_vector = embedding_transport.embed(normalize_key(query))
        archived_report = (
            await self._retrieval_archived_report(
                query=query, scopes=list(scopes), semantic=False, reader_agent_id=reader_agent_id
            )
            if as_of is None
            else self._empty_archived_report(query=query, scopes=list(scopes), semantic=False)
        )
        anchor = as_of or datetime.now(UTC)
        results: list[SearchResult] = []
        for scope_rank, scope in enumerate(scopes):
            # WS-23 H1: one vector space per scope.  A content-protected scope
            # is pinned to the hermetic local transport on both sides, so its
            # sealed text is never re-embedded through a network endpoint and
            # its stored vectors still compare against the query.
            scope_transport = self._engine.content_embedding_transport(scope_key=scope.key)
            scope_query_vector = (
                query_vector if scope_transport is embedding_transport else scope_transport.embed(normalize_key(query))
            )
            candidates = self._retrieval_candidates(
                scope=scope,
                query_tokens=query_tokens,
                query_vector=scope_query_vector,
                embedding_transport=scope_transport,
                policy=policy,
                as_of=as_of,
                include_statuses=include_statuses,
                relationship_types=normalized_types,
                reader_agent_id=reader_agent_id,
            )
            self._expand_retrieval_candidates(
                candidates,
                scope=scope,
                query_tokens=query_tokens,
                query_vector=scope_query_vector,
                embedding_transport=scope_transport,
                policy=policy,
                as_of=as_of,
                include_statuses=include_statuses,
                relationship_types=normalized_types,
                reader_agent_id=reader_agent_id,
            )
            # WS-20 T23: guaranteed-inclusion sweep — every visible pinned row
            # of the scope enters the candidate set even when candidate
            # generation, the max_candidates cap, or a zero relevance score
            # would have excluded it.  Pins already present keep their origin.
            self._add_pinned_candidates(
                candidates,
                scope=scope,
                query_tokens=query_tokens,
                query_vector=scope_query_vector,
                embedding_transport=scope_transport,
                policy=policy,
                as_of=as_of,
                include_statuses=include_statuses,
                relationship_types=normalized_types,
                reader_agent_id=reader_agent_id,
            )
            # WS-25 T3: within-slot hard recency tiebreaker — a NEW stage
            # between the current-truth/visibility filter (stage 5, just
            # above) and the stage-6 rerank below.  Demotes every
            # contradiction-cluster loser out of the candidate set; a
            # candidate sharing no contradiction with anything else in its
            # slot (e.g. two coexisting multi-active preferences) is untouched.
            self._apply_temporal_authority_tiebreaker(candidates, scope=scope)
            priority = scope_priority(scope_rank=scope_rank, scope_count=len(scopes))
            # WS-15 T11 / WS-28 T2: utility term for the final (post stage-5,
            # bounded) candidate set.  One batch projection read per scope,
            # decayed against the SAME anchor as recency.  A vanilla default
            # policy (utility_weight == 0.0, utility_weight_auto_floor_events
            # is None — the shipped default) short-circuits BEFORE this and
            # never touches the event plane, keeping ranking byte-identical.
            # Only when an operator opts a scope into the floor AND this
            # scope's measured event volume crosses it does the effective
            # weight flip to utility_weight_when_unlocked — receipted below —
            # so the pinned RetrievalContract still names the config that
            # decides the flip even though the flip itself is graph-state
            # dependent, exactly reproducible from (contract, graph state,
            # anchor).
            scope_policy = policy
            use_need_by_uuid: dict[str, float] = {}
            if candidates and (policy.utility_weight > 0.0 or policy.utility_weight_auto_floor_events is not None):
                scope_projections = self.graph.utility_projection(scope_key=scope.key, as_of=anchor)
                event_volume = sum(projection.impression_count for projection in scope_projections)
                effective_utility_weight = resolve_effective_utility_weight(policy, event_volume=event_volume)
                if effective_utility_weight > 0.0:
                    use_need_by_uuid = {
                        projection.relationship_uuid: use_need(
                            use_stability=projection.use_stability,
                            outcome_quality=projection.outcome_quality,
                        )
                        for projection in scope_projections
                    }
                if effective_utility_weight != policy.utility_weight:
                    scope_policy = policy.model_copy(update={"utility_weight": effective_utility_weight})
                    self._receipt_utility_auto_enabled(
                        scope=scope,
                        event_volume=event_volume,
                        floor=policy.utility_weight_auto_floor_events,
                        resolved_weight=effective_utility_weight,
                        now=anchor,
                    )
            for candidate in candidates.values():
                relationship = candidate.relationship
                properties = relationship.properties
                final_score = rerank_score(
                    scope_policy,
                    relevance=candidate.relevance,
                    confidence=float(properties.get("confidence", 0.0)),
                    recency=recency_decay(
                        anchor=anchor,
                        valid_from=relationship.valid_from,
                        half_life_days=scope_policy.recency_half_life_days,
                    ),
                    scope_priority_value=priority,
                    type_weight=effective_type_weight(
                        scope_policy,
                        memory_type=properties.get("memory_type"),
                        motive=resolved_motive,
                    ),
                    utility=use_need_by_uuid.get(relationship.uuid, 0.0),
                )
                results.append(
                    self._retrieval_result(
                        relationship,
                        scope=scope,
                        scope_rank=scope_rank,
                        candidate=candidate,
                        score=final_score,
                        contract_digest=contract_digest,
                    )
                )

        # WS-5: scope is a WEIGHTED term by default, so a strong later-scope
        # match can outrank a weak earlier-scope one (see
        # RetrievalPolicy.scope_priority_weight).  strict_scope_tiering restores
        # the hard partition the pre-rerank pipeline had: scope rank leads the
        # sort key, so rank 0 exhausts before rank 1 appears, and the weighted
        # score only orders results WITHIN a rank.  Negated because the sort is
        # descending and rank 0 is the highest-priority scope.
        def _order_key(result: SearchResult) -> tuple[Any, ...]:
            ordering = (
                result.score,
                result.confidence,
                result.valid_from or datetime.min.replace(tzinfo=UTC),
                result.relationship_uuid,
            )
            if policy.strict_scope_tiering:
                return (-result.scope_rank, *ordering)
            return ordering

        results.sort(key=_order_key, reverse=True)
        # WS-20 T23: reserved slots — pins first (rerank-ordered among
        # themselves by the sort above), then the top non-pinned results.
        # When pins alone exceed the limit, pins win in deterministic order.
        pinned_results = [result for result in results if result.pinned]
        if not pinned_results:
            return SearchResults(results[:limit], archived=archived_report)
        unpinned_results = [result for result in results if not result.pinned]
        if len(pinned_results) >= limit:
            return SearchResults(pinned_results[:limit], archived=archived_report)
        return SearchResults(
            [*pinned_results, *unpinned_results[: limit - len(pinned_results)]],
            archived=archived_report,
        )

    def _add_pinned_candidates(
        self,
        candidates: dict[str, ScoredCandidate],
        *,
        scope: MemoryScope,
        query_tokens: frozenset[str],
        query_vector: list[float],
        embedding_transport: EmbeddingTransport,
        policy: RetrievalPolicy,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
        relationship_types: set[str] | None,
        reader_agent_id: str | None = None,
    ) -> None:
        """WS-20 T23: force pinned rows into the candidate set (mutates in place).

        A pinned row that passes the stage-1/5 visibility rules is a candidate
        regardless of its lexical/vector relevance (which is still computed so
        pins rerank deterministically among themselves).  Rows already present
        keep their existing origin and relevance.
        """
        for relationship in self.graph.relationships_for_scope(scope.key):
            if relationship.uuid in candidates:
                continue
            if relationship.properties.get("pinned") is not True:
                continue
            if not self._retrieval_row_visible(
                relationship,
                as_of=as_of,
                include_statuses=include_statuses,
                relationship_types=relationship_types,
                reader_agent_id=reader_agent_id,
            ):
                continue
            searchable = self._retrieval_searchable(relationship, scope=scope)
            lexical = lexical_overlap(query_tokens, frozenset(searchable.split()))
            vector = self._retrieval_vector_similarity(
                relationship,
                scope=scope,
                query_vector=query_vector,
                searchable=searchable,
                embedding_transport=embedding_transport,
                policy=policy,
            )
            candidates[relationship.uuid] = ScoredCandidate(
                relationship_uuid=relationship.uuid,
                relevance=relevance_score(policy, lexical=lexical, vector=vector),
                origin=CandidateOrigin(kind="pinned", hops=0),
                relationship=relationship,
            )

    def _apply_temporal_authority_tiebreaker(
        self, candidates: dict[str, ScoredCandidate], *, scope: MemoryScope
    ) -> None:
        """WS-25 T3: demote same-slot contradiction-cluster losers (mutates
        *candidates* in place).

        Runs AFTER the stage-1/5 visibility filter (every candidate here is
        already current-truth-visible) and BEFORE the stage-6 rerank, so a
        stale contradicted fact never even competes on relevance/recency —
        it is simply not a candidate.  A pinned row (WS-20 T23: a guaranteed-
        retrieval operator hold) is excluded from clustering entirely — never
        demoted, and never counted against another candidate's cluster —
        matching the existing pin invariant that a pin always survives to the
        final result list.
        """
        items: list[TemporalAuthorityCandidate] = []
        for candidate in candidates.values():
            relationship = candidate.relationship
            if relationship.properties.get("pinned") is True:
                continue
            object_text = str(self.graph.reveal(scope.key, relationship.properties.get("object", "")))
            items.append(
                TemporalAuthorityCandidate(
                    uuid=candidate.relationship_uuid,
                    truth_slot=relationship.properties.get("truth_prefix"),
                    object_text=object_text,
                    polarity=str(relationship.properties.get("semantic_polarity", "positive")),
                    valid_from=relationship.valid_from,
                    created_at=relationship.created_at,
                )
            )
        for demoted_uuid in demote_contradicted_same_slot(items):
            candidates.pop(demoted_uuid, None)

    def _retrieval_candidates(
        self,
        *,
        scope: MemoryScope,
        query_tokens: frozenset[str],
        query_vector: list[float],
        embedding_transport: EmbeddingTransport,
        policy: RetrievalPolicy,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
        relationship_types: set[str] | None,
        reader_agent_id: str | None = None,
    ) -> dict[str, ScoredCandidate]:
        """Stages 1–2: visibility-filtered lexical + vector candidate generation."""
        candidates: dict[str, ScoredCandidate] = {}
        for relationship in self.graph.relationships_for_scope(scope.key):
            if not self._retrieval_row_visible(
                relationship,
                as_of=as_of,
                include_statuses=include_statuses,
                relationship_types=relationship_types,
                reader_agent_id=reader_agent_id,
            ):
                continue
            searchable = self._retrieval_searchable(relationship, scope=scope)
            lexical = lexical_overlap(query_tokens, frozenset(searchable.split()))
            vector = self._retrieval_vector_similarity(
                relationship,
                scope=scope,
                query_vector=query_vector,
                searchable=searchable,
                embedding_transport=embedding_transport,
                policy=policy,
            )
            relevance = relevance_score(policy, lexical=lexical, vector=vector)
            if relevance <= 0.0:
                continue
            candidates[relationship.uuid] = ScoredCandidate(
                relationship_uuid=relationship.uuid,
                relevance=relevance,
                origin=CandidateOrigin(kind="lexical" if lexical >= vector else "vector", hops=0),
                relationship=relationship,
            )
        if len(candidates) > policy.max_candidates:
            strongest = sorted(
                candidates.values(),
                key=lambda candidate: (candidate.relevance, candidate.relationship_uuid),
                reverse=True,
            )[: policy.max_candidates]
            candidates = {candidate.relationship_uuid: candidate for candidate in strongest}
        return candidates

    def _expand_retrieval_candidates(
        self,
        candidates: dict[str, ScoredCandidate],
        *,
        scope: MemoryScope,
        query_tokens: frozenset[str],
        query_vector: list[float],
        embedding_transport: EmbeddingTransport,
        policy: RetrievalPolicy,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
        relationship_types: set[str] | None,
        reader_agent_id: str | None = None,
    ) -> None:
        """Stages 3–5: seed-node resolution + bounded weighted beam expansion.

        Mutates ``candidates`` in place.  Every expanded candidate passes the
        same stage-1 visibility rules (stage 5); duplicates keep their
        strongest relevance / shortest hop.
        """
        if policy.max_hops <= 0:
            return
        frontier_nodes = self._retrieval_seed_nodes(
            scope=scope,
            query_tokens=query_tokens,
            query_vector=query_vector,
            embedding_transport=embedding_transport,
            policy=policy,
        )
        seen_nodes = set(frontier_nodes)
        pending_links = list(candidates.values())
        for hop in range(1, policy.max_hops + 1):
            discovered: list[ScoredCandidate] = []
            # (a) Entity hops: memory relationships incident to frontier nodes.
            if frontier_nodes:
                beam = sorted(
                    frontier_nodes.items(),
                    key=lambda item: (item[1], item[0]),
                    reverse=True,
                )[: policy.beam_width]
                node_relevance = dict(beam)
                incident = self.graph.relationships_for_node_uuids(
                    scope_key=scope.key,
                    node_uuids=[node_uuid for node_uuid, _ in beam],
                )
                for relationship in incident:
                    base = max(
                        node_relevance.get(relationship.source_uuid, 0.0),
                        node_relevance.get(relationship.target_uuid, 0.0),
                    )
                    relevance = base * policy.entity_edge_weight
                    if relevance <= 0.0:
                        continue
                    discovered.append(
                        ScoredCandidate(
                            relationship_uuid=relationship.uuid,
                            relevance=relevance,
                            origin=CandidateOrigin(kind="entity_hop", hops=hop),
                            relationship=relationship,
                        )
                    )
            # (b) ROLLUP links: ROLLUP→member (derived_from) and member→ROLLUP
            # (rolled_up_by).  This is the read-side of the two-tier design:
            # demoted members surface exactly when their rollup is relevant.
            for candidate in pending_links:
                properties = candidate.relationship.properties
                if properties.get("memory_type") == MemoryType.ROLLUP.value:
                    member_uuids = [str(uuid) for uuid in (properties.get("derived_from") or [])]
                    discovered.extend(
                        ScoredCandidate(
                            relationship_uuid=member.uuid,
                            relevance=candidate.relevance * policy.rollup_member_edge_weight,
                            origin=CandidateOrigin(
                                kind="rollup_member",
                                hops=hop,
                                via_uuid=candidate.relationship_uuid,
                            ),
                            relationship=member,
                        )
                        for member in self.graph.relationships_by_uuids(member_uuids)
                    )
                rolled_up_by = properties.get("rolled_up_by")
                if rolled_up_by:
                    discovered.extend(
                        ScoredCandidate(
                            relationship_uuid=rollup.uuid,
                            relevance=candidate.relevance * policy.member_rollup_edge_weight,
                            origin=CandidateOrigin(
                                kind="member_rollup",
                                hops=hop,
                                via_uuid=candidate.relationship_uuid,
                            ),
                            relationship=rollup,
                        )
                        for rollup in self.graph.relationships_by_uuids([str(rolled_up_by)])
                    )
            next_frontier: dict[str, float] = {}
            next_links: list[ScoredCandidate] = []
            for candidate in discovered:
                relationship = candidate.relationship
                if relationship.properties.get("scope_key") != scope.key:
                    continue
                if not self._retrieval_row_visible(
                    relationship,
                    as_of=as_of,
                    include_statuses=include_statuses,
                    relationship_types=relationship_types,
                    reader_agent_id=reader_agent_id,
                ):
                    continue
                existing = candidates.get(candidate.relationship_uuid)
                if existing is None:
                    candidates[candidate.relationship_uuid] = candidate
                    next_links.append(candidate)
                    for node_uuid in (relationship.source_uuid, relationship.target_uuid):
                        if node_uuid not in seen_nodes:
                            next_frontier[node_uuid] = max(next_frontier.get(node_uuid, 0.0), candidate.relevance)
                else:
                    candidates[candidate.relationship_uuid] = existing.merge(candidate)
            seen_nodes.update(next_frontier)
            frontier_nodes = next_frontier
            pending_links = next_links
            if not frontier_nodes and not pending_links:
                break

    def _retrieval_seed_nodes(
        self,
        *,
        scope: MemoryScope,
        query_tokens: frozenset[str],
        query_vector: list[float],
        embedding_transport: EmbeddingTransport,
        policy: RetrievalPolicy,
    ) -> dict[str, float]:
        """Stage 3: resolve entity nodes matching the query as expansion seeds.

        WS-17 T16b: when the scope carries ACTIVE entity aliases, a matching
        node also seeds every OTHER node in its alias group (canonical seeds
        its aliases and vice versa) at the same relevance — beam expansion
        crosses the alias boundary without any endpoint rewrite.
        """
        if policy.max_seed_nodes <= 0:
            return {}
        alias_membership: dict[str, frozenset[str]] = {}
        nodes_by_name: dict[str, list[str]] = {}
        if self.config.entity_resolution.enabled:
            alias_groups: dict[str, set[str]] = {}
            for row in self.graph.entity_alias_rows_for_scope(scope.key, status="active"):
                canonical_normalized = normalize_key(str(row["canonical_name"]))
                group = alias_groups.setdefault(canonical_normalized, {canonical_normalized})
                group.add(str(row["name_normalized"]))
            for group in alias_groups.values():
                frozen = frozenset(group)
                for member in group:
                    alias_membership[member] = frozen
        seeds: dict[str, float] = {}
        matched_groups: dict[frozenset[str], float] = {}
        for node in self.graph.nodes_for_scope(scope.key):
            if "Episode" in node.labels:
                continue
            name = str(self.graph.reveal(scope.key, node.properties.get("name", "")))
            if alias_membership:
                nodes_by_name.setdefault(normalize_key(name), []).append(node.uuid)
            text = normalize_key(" ".join([name, self._node_search_text(node.properties)]))
            if not text:
                continue
            lexical = lexical_overlap(query_tokens, frozenset(text.split()))
            vector = 0.0
            if lexical <= 0.0:
                try:
                    vector = max(
                        0.0,
                        cosine_similarity(query_vector, embedding_transport.embed(text)),
                    )
                except ValueError:
                    vector = 0.0
                if vector < policy.vector_min_similarity:
                    vector = 0.0
            relevance = relevance_score(policy, lexical=lexical, vector=vector)
            if relevance > 0.0:
                seeds[node.uuid] = max(seeds.get(node.uuid, 0.0), relevance)
                if alias_membership:
                    group = alias_membership.get(normalize_key(name))
                    if group:
                        matched_groups[group] = max(matched_groups.get(group, 0.0), relevance)
        # Alias union AFTER the scan so every group member is seedable
        # regardless of node iteration order.
        for group, relevance in matched_groups.items():
            for member in group:
                for member_uuid in nodes_by_name.get(member, []):
                    seeds[member_uuid] = max(seeds.get(member_uuid, 0.0), relevance)
        if len(seeds) > policy.max_seed_nodes:
            strongest = sorted(seeds.items(), key=lambda item: (item[1], item[0]), reverse=True)[
                : policy.max_seed_nodes
            ]
            seeds = dict(strongest)
        return seeds

    def _retrieval_row_visible(
        self,
        relationship: Any,
        *,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
        relationship_types: set[str] | None,
        reader_agent_id: str | None = None,
    ) -> bool:
        """Stages 1 and 5: status, validity-window, staleness, and type gates.

        WS-19 T22: also the retrieval-side agent-allowlist gate — one choke
        point covering direct candidates, beam/rollup-member expansion, AND the
        pinned-row sweep (a pinned-but-restricted row never leaks to other
        agents).
        """
        if relationship_types is not None and relationship.type not in relationship_types:
            return False
        if not self._agent_may_view_relationship(relationship.properties, reader_agent_id):
            return False
        status = self._relationship_status(relationship.properties.get("status"))
        if not self._relationship_is_visible(
            status=status,
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            as_of=as_of,
            include_statuses=include_statuses,
        ):
            return False
        return not (as_of is None and relationship.properties.get("rollup_stale") is True)

    def _retrieval_searchable(self, relationship: Any, *, scope: MemoryScope) -> str:
        """Decrypt-on-read searchable text: subject, predicate, object, fact,
        and entity node search text.  A shredded scope yields only placeholders
        (no matches) — governance filtering by construction."""
        source_node = self.graph.get_node(relationship.source_uuid)
        target_node = self.graph.get_node(relationship.target_uuid)
        subject = str(self.graph.reveal(scope.key, source_node.properties["name"]))
        object_value = str(self.graph.reveal(scope.key, target_node.properties["name"]))
        return normalize_key(
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

    def _retrieval_vector_similarity(
        self,
        relationship: Any,
        *,
        scope: MemoryScope,
        query_vector: list[float],
        searchable: str,
        embedding_transport: EmbeddingTransport,
        policy: RetrievalPolicy,
    ) -> float:
        """Stage-2 vector signal from the stored fact embedding.

        Prefers the materialization-time ``embedding`` property (revealed via
        the canonical ``reveal_vector`` path, which degrades to None on a
        shredded scope); rows without one — e.g. pre-WS-1 rows — fall back to
        embedding the revealed searchable text.  Similarities below the policy
        floor contribute nothing.

        WS-17 T18 vector-space guard: a stored vector participates only when its
        stamped ``embedding_identifier`` matches the active transport; a
        mismatched or unstamped (legacy) vector is treated as absent and the
        revealed text is re-embedded in the ACTIVE space (crypto-shredded text
        stays no-signal, as before).
        """
        stored = (
            self.graph.reveal_vector(scope.key, relationship.properties.get("embedding"))
            if stored_vector_in_active_space(
                relationship.properties,
                active_identifier=embedding_transport.identifier,
            )
            else None
        )
        if stored is None:
            if is_sealed_content(str(relationship.properties.get("fact", ""))):
                return 0.0
            stored = embedding_transport.embed(searchable)
        try:
            similarity = max(0.0, cosine_similarity(query_vector, stored))
        except ValueError:
            return 0.0
        return similarity if similarity >= policy.vector_min_similarity else 0.0

    def _retrieval_result(
        self,
        relationship: Any,
        *,
        scope: MemoryScope,
        scope_rank: int,
        candidate: ScoredCandidate,
        score: float,
        contract_digest: str,
    ) -> SearchResult:
        properties = relationship.properties
        source_node = self.graph.get_node(relationship.source_uuid)
        target_node = self.graph.get_node(relationship.target_uuid)
        return SearchResult(
            relationship_uuid=relationship.uuid,
            fact=str(self.graph.reveal(scope.key, properties["fact"])),
            scope=scope,
            scope_rank=scope_rank,
            subject=str(self.graph.reveal(scope.key, source_node.properties["name"])),
            predicate=str(properties["predicate"]),
            object=str(self.graph.reveal(scope.key, target_node.properties["name"])),
            relationship_type=relationship.type,
            confidence=float(properties["confidence"]),
            status=self._relationship_status(properties.get("status")),
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            episode_uuid=str(properties["episode_uuid"]),
            episode_uuids=self._relationship_episode_uuids(properties),
            observed_count=int(properties.get("observed_count", 1)),
            score=score,
            relevance=candidate.relevance,
            origin=candidate.origin.kind,
            hops=candidate.origin.hops,
            retrieval_policy_digest=contract_digest,
            pinned=properties.get("pinned") is True,
        )
