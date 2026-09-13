"""WS-19 — object-level promotion with endorsements + lineage (T20) and the
core-layer scope guard (T21).

Closes AUDIT.md §4 headline gaps: the only upward path was free-text
``memory_publish`` + full re-extraction (no API took a relationship_uuid
upward, no vote counting, no lineage back to the source row), and ACL lived
only in the platform facade (the core client accepted arbitrary scope keys).
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from memotron import (
    AgentMemoryPlatform,
    Memotron,
    MemoryScope,
    ScopeKind,
)
from memotron.agent_memory import (
    AgentMemoryMode,
    build_agent_memory_control_plane,
)
from memotron.config import ErasureBehavior, GovernancePolicy, Motive
from memotron.extraction import RuleBasedExtractionTransport
from memotron.models import MemoryType
from memotron.receipts import ReceiptDecisionType

PROJECT_ID = "jedai-platform"
PROJECT_SCOPE_KEY = f"tenant:{PROJECT_ID}"


class _PoisonedExtractionTransport:
    """Fails the test if formation ever routes an episode through it.

    Promotion candidates MUST take the deterministic rule-based JSON path
    (zero LLM) even when the tenant has a real LLM transport configured —
    this stands in for that transport.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def extract_memories(self, request: Any) -> list[dict[str, Any]]:
        self.calls += 1
        raise AssertionError(
            f"the configured extraction transport must never see a promotion candidate (episode {request.episode.uuid})"
        )


def _platform(
    tmp_path,
    *,
    agent_ids=("alpha", "beta"),
    extraction_transport=None,
    mode=AgentMemoryMode.SIMPLE,
    name="promotion.sqlite",
) -> AgentMemoryPlatform:
    return AgentMemoryPlatform.create(
        graph_path=tmp_path / name,
        project_id=PROJECT_ID,
        agent_ids=agent_ids,
        extraction_transport=extraction_transport,
        mode=mode,
    )


def _configure_project(platform: AgentMemoryPlatform, *, min_endorsements: int = 1):
    return platform.configure_project_memory(
        project_goal="Deliver the platform safely.",
        memory_goal="Keep durable cross-agent decisions and requirements.",
        keep=("Decisions and requirements that affect multiple project agents.",),
        min_endorsements=min_endorsements,
        configured_by="test-operator",
    )


async def _remember_decision(platform: AgentMemoryPlatform, agent_id: str = "alpha"):
    return await platform.memory_remember(
        agent_id=agent_id,
        subject="jedai-platform",
        predicate="decided",
        object="use Memotron as the memory of record",
        relationship_type="DECIDES",
        source_text="ADR-7 sign-off",
    )


async def _run_project_formation(platform: AgentMemoryPlatform, agent_id: str = "alpha"):
    """Run the formation job over the project scope directly (cadence-independent)."""
    return await platform.client.run_dream_job(
        job_name="formation-default",
        tenant_id=PROJECT_ID,
        agent_id=agent_id,
        scope=platform.project_scope,
    )


def _project_rows(platform: AgentMemoryPlatform) -> list[Any]:
    return [
        relationship
        for relationship in platform.client.graph.relationships_for_scope(PROJECT_SCOPE_KEY)
        if relationship.type != "MENTIONS"
    ]


# ---------------------------------------------------------------------------
# T20 — object-level promotion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_promote_materializes_verbatim_with_lineage_zero_llm(
    tmp_path,
) -> None:
    """The headline flow: one call takes an exact governed fact upward.

    The fact travels VERBATIM (never through the configured LLM transport —
    poisoned here to prove it), the default min_endorsements=1 keeps today's
    single-agent effort level, and the materialized project fact carries
    first-class lineage that memory_explain resolves back to the source row.
    """
    poisoned = _PoisonedExtractionTransport()
    platform = _platform(tmp_path, extraction_transport=poisoned)
    _configure_project(platform)
    fact = await _remember_decision(platform)

    promoted = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="every project agent depends on this decision",
        task_run_id="task-77",
    )
    assert promoted.created is True
    assert promoted.source_relationship_uuid == fact.relationship_uuid
    assert promoted.source_scope_key == fact.scope.key
    assert promoted.endorsements == ("alpha",)
    assert promoted.endorsement_count == 1
    assert promoted.min_endorsements == 1
    assert promoted.eligible_for_formation is True
    # The candidate pins the source row's receipted formation decision.
    assert promoted.source_receipt_digest
    assert (
        platform.client.graph.receipts.latest_formation_receipt_digest(fact.relationship_uuid)
        == promoted.source_receipt_digest
    )

    run = await _run_project_formation(platform)
    assert run.processed_episodes == 1
    assert poisoned.calls == 0, "promotion must use the deterministic JSON path"

    rows = _project_rows(platform)
    assert len(rows) == 1
    row = rows[0]
    # Verbatim: same statement, type, and confidence as the source row.
    assert row.type == "DECIDES"
    assert row.properties["fact"] == ("jedai-platform decided use Memotron as the memory of record")
    assert row.properties["confidence"] == 0.9
    # First-class lineage stamped at materialization.
    assert row.properties["promoted_from_relationship_uuid"] == fact.relationship_uuid
    assert row.properties["promoted_from_scope_key"] == fact.scope.key

    explained = await platform.memory_explain(agent_id="beta", relationship_uuid=row.uuid, scope="project")
    lineage = explained.promotion_lineage
    assert lineage is not None
    assert lineage["source_relationship_uuid"] == fact.relationship_uuid
    assert lineage["source_scope_key"] == fact.scope.key
    assert lineage["candidate_episode_uuid"] == promoted.candidate_episode_uuid
    assert lineage["source_receipt_digest"] == promoted.source_receipt_digest
    assert lineage["promoted_by"] == "alpha"
    assert lineage["endorsements"] == ["alpha"]
    # Promotion is an agent act: authority never rises past agent.
    assert row.properties["source_authority"] == "agent"


@pytest.mark.asyncio
async def test_min_endorsements_gates_formation_until_threshold(tmp_path) -> None:
    """Under-endorsed candidates stay PENDING (never consumed); more votes —
    including a deduplicated re-promote of the same source row — make them
    eligible on a later refresh."""
    platform = _platform(tmp_path)
    _configure_project(platform, min_endorsements=2)
    fact = await _remember_decision(platform)

    first = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="needed by every agent",
    )
    assert first.created is True
    assert first.eligible_for_formation is False

    run = await _run_project_formation(platform)
    assert run.processed_episodes == 0
    assert _project_rows(platform) == []
    pending = platform.project_memory_candidates(agent_id="alpha")
    assert len(pending) == 1
    assert pending[0]["episode_uuid"] == first.candidate_episode_uuid
    assert pending[0]["promotion"] is True
    assert pending[0]["endorsement_count"] == 1
    assert pending[0]["endorsers"] == ["alpha"]
    assert pending[0]["min_endorsements"] == 2
    assert pending[0]["eligible_for_formation"] is False
    assert pending[0]["processed"] is False

    # Same agent re-promoting is idempotent — still one vote, no duplicate.
    duplicate = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="still needed",
    )
    assert duplicate.created is False
    assert duplicate.candidate_episode_uuid == first.candidate_episode_uuid
    assert duplicate.endorsement_count == 1

    # A second agent re-promoting the same row ENDORSES the pending candidate.
    second = await platform.memory_promote(
        agent_id="beta",
        relationship_uuid=fact.relationship_uuid,
        rationale="beta relies on it too",
    )
    assert second.created is False
    assert second.candidate_episode_uuid == first.candidate_episode_uuid
    assert second.endorsements == ("alpha", "beta")
    assert second.eligible_for_formation is True

    run = await _run_project_formation(platform)
    assert run.processed_episodes == 1
    rows = _project_rows(platform)
    assert len(rows) == 1
    assert rows[0].properties["promoted_from_relationship_uuid"] == fact.relationship_uuid

    # The full refresh surface agrees the queue is drained.
    time.sleep(1.05)  # formation-default cadence
    refreshed = await platform.memory_refresh(agent_id="alpha", include_project=True)
    assert refreshed.project_run is not None
    assert refreshed.project_run.processed_episodes == 0


@pytest.mark.asyncio
async def test_memory_endorse_promotion_votes_and_fail_fasts(tmp_path) -> None:
    platform = _platform(tmp_path)
    _configure_project(platform, min_endorsements=2)
    fact = await _remember_decision(platform)
    promoted = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="shared decision",
    )

    endorsed = await platform.memory_endorse_promotion(
        agent_id="beta",
        candidate_episode_uuid=promoted.candidate_episode_uuid,
        rationale="beta agrees",
    )
    assert endorsed.created is False
    assert endorsed.endorsements == ("alpha", "beta")
    assert endorsed.eligible_for_formation is True

    # One vote per agent: re-endorsing returns current state unchanged.
    again = await platform.memory_endorse_promotion(
        agent_id="beta",
        candidate_episode_uuid=promoted.candidate_episode_uuid,
        rationale="beta agrees twice",
    )
    assert again.endorsement_count == 2
    ledger = platform.client.graph.promotion_endorsements_for(promoted.candidate_episode_uuid)
    beta_row = next(row for row in ledger if row["agent_id"] == "beta")
    assert beta_row["rationale"] == "beta agrees"  # first rationale stands

    with pytest.raises(ValueError, match="episode does not exist"):
        await platform.memory_endorse_promotion(
            agent_id="beta",
            candidate_episode_uuid="no-such-episode",
            rationale="x",
        )
    with pytest.raises(ValueError, match="rationale cannot be blank"):
        await platform.memory_endorse_promotion(
            agent_id="beta",
            candidate_episode_uuid=promoted.candidate_episode_uuid,
            rationale="   ",
        )

    # A free-text publish candidate is NOT endorsable — promotion only.
    published = await platform.memory_publish(
        agent_id="alpha",
        content="Memory: subject=x; predicate=decided; object=y; relationship_type=DECIDES; confidence=0.9",
        task_run_id="task-1",
    )
    with pytest.raises(ValueError, match="not a promotion candidate"):
        await platform.memory_endorse_promotion(
            agent_id="beta",
            candidate_episode_uuid=published.episode_uuid,
            rationale="x",
        )

    # Once formation consumed the candidate, endorsement is closed.
    await _run_project_formation(platform)
    with pytest.raises(ValueError, match="already consumed by formation"):
        await platform.memory_endorse_promotion(
            agent_id="alpha",
            candidate_episode_uuid=promoted.candidate_episode_uuid,
            rationale="late vote",
        )


@pytest.mark.asyncio
async def test_memory_promote_fail_fasts(tmp_path) -> None:
    platform = _platform(tmp_path)
    fact = await _remember_decision(platform)

    # Project memory must be configured first (same gate as memory_publish).
    with pytest.raises(ValueError, match="project memory is not configured"):
        await platform.memory_promote(
            agent_id="alpha",
            relationship_uuid=fact.relationship_uuid,
            rationale="x",
        )
    _configure_project(platform)

    with pytest.raises(ValueError, match="relationship does not exist"):
        await platform.memory_promote(agent_id="alpha", relationship_uuid="missing", rationale="x")
    with pytest.raises(ValueError, match="rationale cannot be blank"):
        await platform.memory_promote(
            agent_id="alpha",
            relationship_uuid=fact.relationship_uuid,
            rationale="  ",
        )

    mentions = next(
        relationship for relationship in platform.client.graph.relationships() if relationship.type == "MENTIONS"
    )
    with pytest.raises(ValueError, match="MENTIONS relationships cannot be promoted"):
        await platform.memory_promote(agent_id="alpha", relationship_uuid=mentions.uuid, rationale="x")

    # A promoted (project-scope) row cannot be re-promoted.
    await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="promote once",
    )
    await _run_project_formation(platform)
    project_row = _project_rows(platform)[0]
    with pytest.raises(ValueError, match="project-scope rows cannot be re-promoted"):
        await platform.memory_promote(agent_id="alpha", relationship_uuid=project_row.uuid, rationale="x")

    # A retired (non-visible) row cannot be promoted.
    gone = await platform.memory_remember(
        agent_id="alpha",
        subject="alpha",
        predicate="prefers",
        object="a fact about to be forgotten",
        relationship_type="PREFERS",
    )
    await platform.memory_forget(
        agent_id="alpha",
        relationship_uuid=gone.relationship_uuid,
        reason="stale",
    )
    with pytest.raises(ValueError, match="not currently visible"):
        await platform.memory_promote(
            agent_id="alpha",
            relationship_uuid=gone.relationship_uuid,
            rationale="x",
        )


@pytest.mark.asyncio
async def test_memory_promote_refuses_rows_outside_own_scopes(tmp_path) -> None:
    """Multi-agent mode: agent B cannot promote agent A's private fact."""
    platform = _platform(
        tmp_path,
        mode=AgentMemoryMode.MULTI_AGENT,
        name="promotion-multi.sqlite",
    )
    _configure_project(platform)
    fact = await platform.memory_remember(
        agent_id="alpha",
        subject="alpha",
        predicate="decided",
        object="alpha-private rollout order",
        relationship_type="DECIDES",
    )
    assert fact.scope.key == "agent:alpha"
    with pytest.raises(ValueError, match="outside the caller's own readable scopes"):
        await platform.memory_promote(
            agent_id="beta",
            relationship_uuid=fact.relationship_uuid,
            rationale="beta wants it",
        )
    # The owner can promote it.
    owned = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="share the rollout order",
    )
    assert owned.created is True
    assert owned.source_scope_key == "agent:alpha"


@pytest.mark.asyncio
async def test_memory_promote_crypto_shredded_source_fails(tmp_path) -> None:
    """A crypto-shredded source reveals only placeholders — promotion must
    fail fast instead of publishing placeholder text into project memory."""
    platform = _platform(tmp_path, name="promotion-shred.sqlite")
    _configure_project(platform)
    shred_motive = Motive(
        name="shred-writes",
        goal="Store user facts under crypto-shred governance.",
        governance=GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED),
    )
    sealed = await platform.client.add_memory(
        subject="guest",
        predicate="prefers",
        object="wheelchair-accessible entrances",
        relationship_type="PREFERS",
        scope=platform.user_scope,
        confidence=0.9,
        metadata={"agent_memory": True, "_verified_source_authority": "agent"},
        motive=shred_motive,
    )
    shredded = await platform.client.crypto_shred(scope=platform.user_scope)
    assert shredded["shredded"] is True
    with pytest.raises(ValueError, match="crypto-shredded"):
        await platform.memory_promote(
            agent_id="alpha",
            relationship_uuid=sealed.relationship_uuid,
            rationale="promote the sealed fact",
        )


@pytest.mark.asyncio
async def test_promotion_still_gated_by_project_motive_type_filter(tmp_path) -> None:
    """Eligibility is necessary, not sufficient: the project Motive's
    allowed-type gate still applies to an endorsed promotion, receipted as
    usual (default project policy does not allow ``preference``)."""
    platform = _platform(tmp_path, name="promotion-gated.sqlite")
    _configure_project(platform)  # default allowed types exclude preference
    fact = await platform.memory_remember(
        agent_id="alpha",
        subject="alpha",
        predicate="prefers",
        object="tabs over spaces",
        relationship_type="PREFERS",
    )
    promoted = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=fact.relationship_uuid,
        rationale="everyone should know",
    )
    assert promoted.eligible_for_formation is True

    run = await _run_project_formation(platform)
    assert run.processed_episodes == 1  # consumed, not left pending
    assert _project_rows(platform) == []  # but gated out — no project fact
    receipts = platform.client.graph.receipts.receipts_for_scope(PROJECT_SCOPE_KEY)
    assert any(
        receipt.decision_type == ReceiptDecisionType.FORMATION_MOTIVE_TYPE_FILTERED.value
        and receipt.memory_type == MemoryType.PREFERENCE.value
        for receipt in receipts
    )


# ---------------------------------------------------------------------------
# T21 — core-layer scope guard
# ---------------------------------------------------------------------------

ALLOWED = MemoryScope(kind=ScopeKind.USER, scope_id="guard-ok")
DENIED = MemoryScope(kind=ScopeKind.USER, scope_id="guard-denied")


async def _guarded_client(tmp_path, name="guard.sqlite") -> Memotron:
    client = Memotron(
        graph_path=tmp_path / name,
        authorized_scope_keys={ALLOWED.key},
    )
    await client.add_memory(
        subject="me",
        predicate="prefers",
        object="green tea",
        relationship_type="PREFERS",
        scope=ALLOWED,
    )
    return client


@pytest.mark.asyncio
async def test_scope_guard_rejects_out_of_set_reads_and_writes_uniformly(
    tmp_path,
) -> None:
    client = await _guarded_client(tmp_path)
    attempts = [
        (
            "write add_memory",
            client.add_memory(
                subject="x",
                predicate="prefers",
                object="y",
                relationship_type="PREFERS",
                scope=DENIED,
            ),
        ),
        ("read search", client.search(query="tea", scope=DENIED)),
        ("read semantic_search", client.semantic_search(query="tea", scope=DENIED)),
        ("read profile", client.profile(scope=DENIED)),
        (
            "read truth_timeline",
            client.truth_timeline(
                scope=DENIED,
                subject="me",
                predicate="prefers",
            ),
        ),
        ("read memory_evolution", client.memory_evolution(scope=DENIED)),
        (
            "read entity_neighborhood",
            client.entity_neighborhood(
                scope=DENIED,
                entity="me",
            ),
        ),
        ("read memory_utility", client.memory_utility(scope=DENIED)),
        ("backfill canonicalize", client.canonicalize_scope_predicates(scope=DENIED)),
        ("backfill entities", client.resolve_scope_entities(scope=DENIED)),
        ("adjudication reviews", client.pending_supersession_reviews(scope=DENIED)),
        ("alias proposals", client.pending_entity_alias_proposals(scope=DENIED)),
        ("governance crypto_shred", client.crypto_shred(scope=DENIED)),
        ("dream run", client.run_due_dreams(scope=DENIED)),
    ]
    for label, coroutine in attempts:
        with pytest.raises(ValueError, match="authorized_scope_keys") as exc_info:
            await coroutine
        assert DENIED.key in str(exc_info.value), label

    # In-set traffic is unaffected.
    results = await client.search(query="green tea", scope=ALLOWED)
    assert results and "green tea" in results[0].fact
    profile = await client.profile(scope=ALLOWED)
    assert "green tea" in profile.rendered_context


@pytest.mark.asyncio
async def test_scope_guard_multi_scope_and_row_addressed_paths(tmp_path) -> None:
    client = await _guarded_client(tmp_path, name="guard-multi.sqlite")

    # One bad scope in a multi-scope search rejects the whole call.
    with pytest.raises(ValueError, match="authorized_scope_keys"):
        await client.search_context(query="tea", scopes=[ALLOWED, DENIED])

    # Row-addressed APIs guard the RESOLVED row scope even when the scope
    # argument is omitted — the guard cannot be bypassed via uuid access.
    unguarded = Memotron(graph_path=tmp_path / "guard-multi.sqlite")
    foreign = await unguarded.add_memory(
        subject="them",
        predicate="prefers",
        object="oolong",
        relationship_type="PREFERS",
        scope=DENIED,
    )
    with pytest.raises(ValueError, match="authorized_scope_keys"):
        await client.memory_evidence(relationship_uuid=foreign.relationship_uuid)
    with pytest.raises(ValueError, match="authorized_scope_keys"):
        await client.forget_memory(relationship_uuid=foreign.relationship_uuid, reason="not yours")

    # Cross-scope listings fail closed instead of silently widening.
    with pytest.raises(ValueError, match="requires an explicit scope"):
        await client.run_checkpoints()
    # Whole-store content export spans every scope — fail closed too.
    with pytest.raises(ValueError, match="not available on a scope-guarded client"):
        client.export_graph()


@pytest.mark.asyncio
async def test_scope_guard_default_none_is_byte_identical(tmp_path) -> None:
    """Without the guard the SDK stays omni-scope: cross-scope reads and
    writes work exactly as before."""
    client = Memotron(graph_path=tmp_path / "unguarded.sqlite")
    assert client.authorized_scope_keys is None
    await client.add_memory(
        subject="me",
        predicate="prefers",
        object="green tea",
        relationship_type="PREFERS",
        scope=ALLOWED,
    )
    await client.add_memory(
        subject="them",
        predicate="prefers",
        object="oolong",
        relationship_type="PREFERS",
        scope=DENIED,
    )
    assert await client.search(query="oolong", scope=DENIED)
    assert await client.search(query="green tea", scope=ALLOWED)
    assert (await client.profile(scope=DENIED)).static_facts
    assert await client.run_checkpoints() is not None


@pytest.mark.asyncio
async def test_scope_guard_constructor_validation(tmp_path) -> None:
    with pytest.raises(ValueError, match="non-blank scope keys"):
        Memotron(
            graph_path=tmp_path / "bad-guard.sqlite",
            authorized_scope_keys={"user:ok", "  "},
        )
    with pytest.raises(ValueError, match="non-blank scope keys"):
        Memotron(
            graph_path=tmp_path / "bad-guard-2.sqlite",
            authorized_scope_keys=set(),
        )


@pytest.mark.asyncio
async def test_platform_facade_does_not_set_core_guard(tmp_path) -> None:
    """Facade authorization is per-call; its omni-tenant client stays
    unguarded so one client can serve every registered agent."""
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "facade.sqlite",
        project_id=PROJECT_ID,
        agent_ids=("alpha",),
    )
    assert platform.client.authorized_scope_keys is None


@pytest.mark.asyncio
async def test_guarded_client_composes_with_control_plane(tmp_path) -> None:
    """An embedded deployment can pair the facade's control plane with a
    guarded client for defense in depth on its own scopes."""
    control_plane = build_agent_memory_control_plane(project_id=PROJECT_ID, agent_ids=("alpha",))
    client = Memotron(
        graph_path=tmp_path / "guarded-cp.sqlite",
        control_plane=control_plane,
        extraction_transport=RuleBasedExtractionTransport(),
        authorized_scope_keys={"agent:alpha"},
    )
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="alpha")
    written = await client.add_memory(
        subject="alpha",
        predicate="should",
        object="run the suite before landing",
        relationship_type="SHOULD",
        scope=agent_scope,
        metadata={"agent_memory": True},
    )
    assert written.relationship_uuid
    with pytest.raises(ValueError, match="authorized_scope_keys"):
        await client.profile(scope=MemoryScope(kind=ScopeKind.TENANT, scope_id=PROJECT_ID))
