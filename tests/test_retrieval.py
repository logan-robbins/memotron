"""WS-5: Deterministic six-stage retrieval pipeline tests.

Covers the RetrievalContract digest, lexical/vector candidate generation,
relationship-type filtering, Motive rerank boosting, entity-hop and
ROLLUP-link expansion, use-event digest propagation, determinism, the new
index-backed graph helpers, and the direct-candidate cap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    AgentMemoryPlatform,
    DreamConfig,
    DreamJob,
    DreamJobKind,
    Memotron,
    MemoryScope,
    MemoryType,
    Motive,
    RetrievalPolicy,
    ScopeKind,
)
from memotron.config import RollupConsolidationPolicy, default_config
from memotron.models import OutcomeVerdict, UseEventKind
from memotron.retrieval import (
    EMBEDDING_IDENTIFIER,
    build_retrieval_contract,
    retrieval_contract_digest,
    use_need,
)

DIRECT_ORIGINS = {"lexical", "vector"}


def new_client(tmp_path: Path, name: str, retrieval: RetrievalPolicy | None = None) -> Memotron:
    """Memotron client on a tmp graph, optionally with a retrieval override."""
    config: DreamConfig | None = None
    if retrieval is not None:
        config = default_config().model_copy(update={"retrieval": retrieval})
    if config is None:
        return Memotron(graph_path=tmp_path / name)
    return Memotron(graph_path=tmp_path / name, config=config)


def rollup_config() -> DreamConfig:
    """WS-4 consolidation-only config (mirrors the test_memotron.py setup)."""
    return default_config().model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="consolidation-rollup",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.0,
                        min_cluster_size=3,
                        max_depth=1,
                    ),
                ),
            ),
        }
    )


def test_retrieval_contract_digest_is_deterministic_and_input_sensitive() -> None:
    policy = RetrievalPolicy()
    kwargs = {
        "operation": "search_context",
        "scope_keys": ("customer:acme",),
        "query": "customer sso requirement",
        "limit": 10,
        "policy": policy,
        "embedding_identifier": EMBEDDING_IDENTIFIER,
    }

    digest_one = retrieval_contract_digest(build_retrieval_contract(**kwargs))
    digest_two = retrieval_contract_digest(build_retrieval_contract(**kwargs))
    assert digest_one == digest_two
    assert digest_one

    # Any policy knob change must produce a different digest.
    reweighted = retrieval_contract_digest(
        build_retrieval_contract(**{**kwargs, "policy": RetrievalPolicy(entity_edge_weight=0.9)})
    )
    assert reweighted != digest_one

    # A different query must produce a different digest.
    requeried = retrieval_contract_digest(build_retrieval_contract(**{**kwargs, "query": "another query entirely"}))
    assert requeried != digest_one


@pytest.mark.asyncio
async def test_vector_candidates_surface_trigram_similar_facts(tmp_path: Path) -> None:
    client = new_client(tmp_path, "vector.sqlite")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="vector")

    await client.add_memory(
        subject="Customer",
        predicate="requires",
        object="SSO login",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    await client.add_memory(
        subject="Melody",
        predicate="prefers",
        object="window seating",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )

    # "requirement" shares trigrams (not tokens) with "requires": the fact
    # still surfaces through the hashed-trigram vector signal.
    results = await client.search(query="customer sso requirement", scope=scope, limit=10)

    assert results, "trigram-similar fact should surface as a candidate"
    assert results[0].relationship_type == "REQUIRES"
    assert results[0].object == "SSO login"
    assert results[0].relevance > 0
    for result in results:
        if result.origin in DIRECT_ORIGINS:
            assert result.hops == 0


@pytest.mark.asyncio
async def test_relationship_types_filter_restricts_results(tmp_path: Path) -> None:
    client = new_client(tmp_path, "types.sqlite")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="types")

    await client.add_memory(
        subject="Customer",
        predicate="requires",
        object="SOC2 report",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    await client.add_memory(
        subject="Customer",
        predicate="prefers",
        object="SOC2 summaries",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )

    unfiltered = await client.search(query="customer soc2", scope=scope, limit=10)
    assert {result.relationship_type for result in unfiltered} == {"REQUIRES", "PREFERS"}

    filtered = await client.search(
        query="customer soc2",
        scope=scope,
        limit=10,
        relationship_types={"REQUIRES"},
    )
    assert filtered, "the REQUIRES fact must still match"
    assert all(result.relationship_type == "REQUIRES" for result in filtered)
    assert filtered[0].object == "SOC2 report"


@pytest.mark.asyncio
async def test_motive_budget_share_boosts_allowed_memory_types_at_rerank(tmp_path: Path) -> None:
    client = new_client(tmp_path, "motive.sqlite")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="motive")

    requires = await client.add_memory(
        subject="Customer",
        predicate="requires",
        object="detailed release notes",
        relationship_type="REQUIRES",  # → memory_type "requirement"
        scope=scope,
        confidence=0.9,
    )
    prefers = await client.add_memory(
        subject="Customer",
        predicate="prefers",
        object="detailed release notes",
        relationship_type="PREFERS",  # → memory_type "preference"
        scope=scope,
        confidence=0.9,
    )

    query = "customer detailed release notes"
    baseline = await client.search(query=query, scope=scope, limit=10)
    baseline_scores = {result.relationship_uuid: result.score for result in baseline}
    assert {requires.relationship_uuid, prefers.relationship_uuid} <= set(baseline_scores)

    motive = Motive(
        name="boost-requirements",
        goal="Rank requirement memories first for compliance work.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
        retrieval_budget_share=0.9,
    )
    boosted = await client.search(query=query, scope=scope, limit=10, motive=motive)
    boosted_scores = {result.relationship_uuid: result.score for result in boosted}

    # The Motive-allowed type ranks first under the boost.
    assert boosted[0].relationship_uuid == requires.relationship_uuid
    assert boosted[0].relationship_type == "REQUIRES"
    assert boosted_scores[requires.relationship_uuid] > boosted_scores[prefers.relationship_uuid]
    # The boost is exactly the Motive's read-side lever: the allowed type's
    # score rises, the other type's score is untouched.
    assert boosted_scores[requires.relationship_uuid] > baseline_scores[requires.relationship_uuid]
    assert boosted_scores[prefers.relationship_uuid] == pytest.approx(baseline_scores[prefers.relationship_uuid])


@pytest.mark.asyncio
async def test_entity_hop_expansion_surfaces_shared_entity_fact(tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="hops")

    async def seed(client: Memotron) -> tuple[str, str]:
        fact_a = await client.add_memory(
            subject="Ballroom Team",
            predicate="uses",
            object="SQLite",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
        fact_b = await client.add_memory(
            subject="Ballroom Team",
            predicate="owns",
            object="the deterministic retrieval pipeline roadmap",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
        return fact_a.relationship_uuid, fact_b.relationship_uuid

    # "ballroomteam" is trigram-similar to the entity "Ballroom Team" but
    # shares no whole token with fact B, so fact B has no direct lexical or
    # vector match and can only arrive through the entity-hop expansion.
    # (A plain "ballroom team sqlite" query would surface fact B directly:
    # the subject entity's name is part of every incident fact's searchable
    # text, so token-level entity queries produce origin "lexical" instead.)
    query = "ballroomteam sqlite"

    client = new_client(tmp_path, "hops.sqlite")
    a_uuid, b_uuid = await seed(client)
    results = await client.search(query=query, scope=scope, limit=10)
    by_uuid = {result.relationship_uuid: result for result in results}

    assert a_uuid in by_uuid, "fact A must match directly"
    assert by_uuid[a_uuid].hops == 0
    assert by_uuid[a_uuid].origin in DIRECT_ORIGINS

    assert b_uuid in by_uuid, "fact B must be reached through the shared entity"
    assert by_uuid[b_uuid].origin == "entity_hop"
    assert by_uuid[b_uuid].hops >= 1
    assert by_uuid[b_uuid].score < by_uuid[a_uuid].score

    # max_hops=0 disables expansion entirely: fact B disappears.
    flat_client = new_client(tmp_path, "hops-flat.sqlite", retrieval=RetrievalPolicy(max_hops=0))
    flat_a_uuid, flat_b_uuid = await seed(flat_client)
    flat_results = await flat_client.search(query=query, scope=scope, limit=10)
    flat_uuids = {result.relationship_uuid for result in flat_results}
    assert flat_a_uuid in flat_uuids
    assert flat_b_uuid not in flat_uuids


@pytest.mark.asyncio
async def test_rollup_expansion_links_rollup_and_demoted_members_both_ways(tmp_path: Path) -> None:
    client = Memotron(graph_path=tmp_path / "rollup.sqlite", config=rollup_config())
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollup")

    # The deterministic rollup label is the members' shared leading span plus a
    # count of what varies ("Acme Parks prefers secure document portal: 3
    # values"), so the distinguishing suffixes (SOC2 / ISO27001 / W9) stay out
    # of the ROLLUP's fact text — which is what makes the member-expansion
    # assertions below meaningful.
    for suffix in ("SOC2", "ISO27001", "W9"):
        await client.add_memory(
            subject="Acme Parks",
            predicate="prefers",
            object=f"secure document portal {suffix}",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )

    await client.run_dream_job(job_name="consolidation-rollup", now=datetime(2026, 6, 1, tzinfo=UTC))

    scoped = [
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key and relationship.type != "MENTIONS"
    ]
    rollup_uuids = {
        relationship.uuid
        for relationship in scoped
        if relationship.properties.get("memory_type") == MemoryType.ROLLUP.value
    }
    demoted_uuids = {
        relationship.uuid for relationship in scoped if relationship.properties.get("active_in_context") is False
    }
    assert len(rollup_uuids) == 1
    assert len(demoted_uuids) == 3

    # ROLLUP → member: a query matching only the ROLLUP's fact text pulls the
    # demoted members back in through the derived_from expansion.
    rollup_hit_results = await client.search(query="rollup summarizes", scope=scope, limit=10)
    assert any(result.relationship_uuid in rollup_uuids and result.hops == 0 for result in rollup_hit_results)
    member_hits = [result for result in rollup_hit_results if result.origin == "rollup_member"]
    assert member_hits, "demoted members must surface through the ROLLUP"
    assert all(result.hops >= 1 for result in member_hits)
    assert any(result.relationship_uuid in demoted_uuids for result in member_hits)

    # Member → ROLLUP: a query matching one demoted member directly surfaces
    # the covering ROLLUP through the rolled_up_by link.
    member_hit_results = await client.search(query="ISO27001", scope=scope, limit=10)
    assert member_hit_results[0].relationship_uuid in demoted_uuids
    assert member_hit_results[0].hops == 0
    rollup_hits = [result for result in member_hit_results if result.origin == "member_rollup"]
    assert rollup_hits, "the covering ROLLUP must surface through its member"
    assert all(result.hops >= 1 for result in rollup_hits)
    assert any(result.relationship_uuid in rollup_uuids for result in rollup_hits)


@pytest.mark.asyncio
async def test_memory_search_use_events_carry_retrieval_policy_digest(tmp_path: Path) -> None:
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "agent-memory.sqlite",
        project_id="jedai-platform",
        agent_ids=("platform",),
    )
    await platform.memory_remember(
        agent_id="platform",
        subject="platform agent",
        predicate="should",
        object="check worktree status before editing",
        relationship_type="SHOULD",
        source_text="operator preference",
    )

    search = await platform.memory_search(agent_id="platform", query="worktree status")

    assert search.results, "the remembered fact must be retrievable"
    assert len(search.use_events) == len(search.results)
    for use_event, result in zip(search.use_events, search.results, strict=False):
        assert result.retrieval_policy_digest
        assert use_event.retrieval_policy_digest == result.retrieval_policy_digest
        assert use_event.relationship_uuid == result.relationship_uuid


@pytest.mark.asyncio
async def test_search_is_deterministic_across_runs(tmp_path: Path) -> None:
    client = new_client(tmp_path, "deterministic.sqlite")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="deterministic")
    valid_from = datetime(2026, 6, 1, tzinfo=UTC)

    for obj in ("window seating", "aisle seating", "extra legroom seating"):
        await client.add_memory(
            subject="Melody",
            predicate="prefers",
            object=obj,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
            valid_from=valid_from,
        )

    # A fixed as_of pins the recency anchor so two runs are byte-identical.
    as_of = datetime(2026, 7, 1, tzinfo=UTC)
    first = await client.search(query="melody window seating", scope=scope, limit=10, as_of=as_of)
    second = await client.search(query="melody window seating", scope=scope, limit=10, as_of=as_of)

    assert first, "the query must match"
    assert [(result.relationship_uuid, result.score) for result in first] == [
        (result.relationship_uuid, result.score) for result in second
    ]


@pytest.mark.asyncio
async def test_graph_helpers_handle_empty_unknown_and_mentions_rows(tmp_path: Path) -> None:
    client = new_client(tmp_path, "helpers.sqlite")
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="helpers")

    fact_a = await client.add_memory(
        subject="Ballroom Team",
        predicate="uses",
        object="SQLite",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )
    fact_b = await client.add_memory(
        subject="Ballroom Team",
        predicate="requires",
        object="deterministic retrieval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    graph = client.graph

    # relationships_by_uuids: empty input and unknown uuids.
    assert graph.relationships_by_uuids([]) == []
    assert graph.relationships_by_uuids(["not-a-real-uuid"]) == []
    partial = graph.relationships_by_uuids([fact_a.relationship_uuid, "not-a-real-uuid"])
    assert [relationship.uuid for relationship in partial] == [fact_a.relationship_uuid]

    # relationships_for_node_uuids: empty input.
    assert graph.relationships_for_node_uuids(scope_key=scope.key, node_uuids=[]) == []

    # relationships_for_scope: both memory rows, never structural MENTIONS.
    scoped = graph.relationships_for_scope(scope.key)
    scoped_uuids = {relationship.uuid for relationship in scoped}
    assert {fact_a.relationship_uuid, fact_b.relationship_uuid} <= scoped_uuids
    assert all(relationship.type != "MENTIONS" for relationship in scoped)
    assert any(relationship.type == "MENTIONS" for relationship in graph.relationships()), (
        "materialization stores MENTIONS rows that the scoped read must strip"
    )


@pytest.mark.asyncio
async def test_max_candidates_caps_direct_candidates_before_expansion(tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="cap")

    async def seed(client: Memotron) -> None:
        for obj in ("window seating", "aisle seating", "extra legroom seating"):
            await client.add_memory(
                subject="Melody",
                predicate="prefers",
                object=obj,
                relationship_type="PREFERS",
                scope=scope,
                confidence=0.9,
            )

    # The cap bounds DIRECT candidates only: exactly one hop-0 result (the
    # strongest direct match) survives; anything else present must have been
    # rediscovered by expansion.
    capped = new_client(tmp_path, "cap.sqlite", retrieval=RetrievalPolicy(max_candidates=1))
    await seed(capped)
    results = await capped.search(query="melody window seating", scope=scope, limit=10)
    direct_hits = [result for result in results if result.hops == 0]
    assert len(direct_hits) == 1
    assert direct_hits[0].object == "window seating"
    assert direct_hits[0].origin in DIRECT_ORIGINS
    for result in results:
        if result.hops > 0:
            assert result.origin == "entity_hop"

    # With expansion disabled too, the cap is the whole result set.
    flat = new_client(
        tmp_path,
        "cap-flat.sqlite",
        retrieval=RetrievalPolicy(max_candidates=1, max_hops=0),
    )
    await seed(flat)
    flat_results = await flat.search(query="melody window seating", scope=scope, limit=10)
    assert len(flat_results) == 1
    assert flat_results[0].object == "window seating"


# ---------------------------------------------------------------------------
# WS-15 T11: stage-6 rerank reads the receipt-derived utility projection
# ---------------------------------------------------------------------------

UTILITY_ANCHOR = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


async def _seed_equal_pair(client: Memotron, scope: MemoryScope) -> tuple[str, str]:
    """Two rows identical in confidence, validity, and lexical relevance."""
    plain = await client.add_memory(
        subject="Team Alpha",
        predicate="requires",
        object="SSO login",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        valid_from=UTILITY_ANCHOR - timedelta(days=10),
    )
    cited = await client.add_memory(
        subject="Team Beta",
        predicate="requires",
        object="SSO login",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        valid_from=UTILITY_ANCHOR - timedelta(days=10),
    )
    return plain.relationship_uuid, cited.relationship_uuid


async def _record_cited_positive(client: Memotron, scope: MemoryScope, relationship_uuid: str) -> None:
    use = await client.record_memory_use(
        relationship_uuid=relationship_uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="task-utility",
        idempotency_key=f"task-utility:cited:{relationship_uuid}",
        used_at=UTILITY_ANCHOR - timedelta(days=1),
    )
    await client.record_memory_outcome(
        use_id=use.use_id,
        scope=scope,
        verdict=OutcomeVerdict.POSITIVE,
        task_run_id="task-utility",
        idempotency_key=f"task-utility:outcome:{relationship_uuid}",
        judge_identity="test-evaluator",
        judge_version="v1",
        judged_at=UTILITY_ANCHOR - timedelta(days=1),
    )


@pytest.mark.asyncio
async def test_utility_weight_default_zero_keeps_ranking_byte_identical(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Default policy: the utility term contributes nothing AND the event
    plane is never read — even when cited/positive history exists."""
    client = new_client(tmp_path, "utility-default.sqlite", retrieval=RetrievalPolicy(vector_weight=0.0))
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="utility-default")
    plain_uuid, cited_uuid = await _seed_equal_pair(client, scope)
    await _record_cited_positive(client, scope, cited_uuid)

    def _forbidden(**_kwargs):  # pragma: no cover - failing is the assertion
        raise AssertionError("utility projection must not be read at utility_weight=0")

    monkeypatch.setattr(client.graph, "utility_projection", _forbidden)
    results = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
    by_uuid = {result.relationship_uuid: result for result in results}
    assert set(by_uuid) == {plain_uuid, cited_uuid}
    # Equal inputs, zero utility weight: byte-identical scores.
    assert by_uuid[plain_uuid].score == by_uuid[cited_uuid].score


@pytest.mark.asyncio
async def test_utility_weight_promotes_cited_positive_row_with_exact_scores(
    tmp_path: Path,
) -> None:
    policy = RetrievalPolicy(vector_weight=0.0, utility_weight=0.8)
    client = new_client(tmp_path, "utility-promotes.sqlite", retrieval=policy)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="utility-promotes")
    plain_uuid, cited_uuid = await _seed_equal_pair(client, scope)
    await _record_cited_positive(client, scope, cited_uuid)

    results = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
    assert next(result.relationship_uuid for result in results) == cited_uuid
    by_uuid = {result.relationship_uuid: result for result in results}

    # Exact expected gap: utility_weight × use_need of the cited row, computed
    # from the SAME projection at the SAME anchor the search used.
    projection = (await client.memory_utility(scope=scope, relationship_uuid=cited_uuid, as_of=UTILITY_ANCHOR))[0]
    expected_gap = policy.utility_weight * use_need(
        use_stability=projection.use_stability,
        outcome_quality=projection.outcome_quality,
    )
    assert projection.use_stability > 0.0
    assert by_uuid[cited_uuid].score - by_uuid[plain_uuid].score == pytest.approx(expected_gap)


@pytest.mark.asyncio
async def test_utility_rerank_is_deterministic_under_pinned_as_of(tmp_path: Path) -> None:
    policy = RetrievalPolicy(utility_weight=0.8)
    client = new_client(tmp_path, "utility-determinism.sqlite", retrieval=policy)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="utility-determinism")
    _, cited_uuid = await _seed_equal_pair(client, scope)
    await _record_cited_positive(client, scope, cited_uuid)

    first = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
    second = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
    assert [(r.relationship_uuid, r.score) for r in first] == [(r.relationship_uuid, r.score) for r in second]
    digests = {r.retrieval_policy_digest for r in (*first, *second)}
    assert len(digests) == 1 and digests != {""}


def test_retrieval_contract_schema_v2_and_utility_weight_digest_sensitivity() -> None:
    kwargs = {
        "operation": "search_context",
        "scope_keys": ("customer:utility",),
        "query": "sso login",
        "limit": 10,
        "embedding_identifier": EMBEDDING_IDENTIFIER,
    }
    baseline = build_retrieval_contract(policy=RetrievalPolicy(), **kwargs)
    weighted = build_retrieval_contract(policy=RetrievalPolicy(utility_weight=0.5), **kwargs)
    assert baseline.model_dump(mode="json")["schema_version"] == 2
    assert weighted.model_dump(mode="json")["schema_version"] == 2
    assert baseline.model_dump(mode="json")["policy"]["utility_weight"] == 0.0
    assert retrieval_contract_digest(baseline) != retrieval_contract_digest(weighted)
