"""WS-22 T28/T29: token-savings measurement and repeat-search telemetry.

Pins the measurement contracts on ``MemoryEvolutionProof``:

- T28 ``tokens_*``: raw-episode baseline vs. the rendered profile, plus the
  demotion dividend (rollup members + cross-prefix duplicates minus their
  rollup/survivor lines) — all counted with the platform's single deterministic
  estimator and the exact profile line format.  Zero (never absent) on an
  empty scope.
- T29 ``repeat_search_rate`` / ``answered_from_profile_rate``: event-plane,
  rebuildable, joined exactly as the WS-15 citation scanner recorded events;
  ``None`` (never a fake 0.0) when the denominator is empty.

Hermetic: rule-based extraction, local embeddings, no network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    AgentMemoryMode,
    AgentMemoryPlatform,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryScope,
    RollupConsolidationPolicy,
    ScopeKind,
)
from memotron.config import default_config
from memotron.models import UseEventKind, session_boundary_for_task_run
from memotron.retrieval import query_digest as retrieval_query_digest
from memotron.transcripts import TranscriptTurn

AGENT_ID = "claude-code"

T0 = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _client(tmp_path: Path, *, name: str = "graph.sqlite") -> Memotron:
    config = default_config().model_copy(
        update={
            "jobs": (
                DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
                DreamJob(
                    name="consolidation",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.0,
                        min_cluster_size=3,
                        max_depth=1,
                    ),
                ),
                DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
            )
        }
    )
    return Memotron(config=config, graph_path=tmp_path / name)


def _requirement_record(obj: str) -> dict:
    return {
        "subject": "Guest Services",
        "predicate": "requires",
        "object": obj,
        "relationship_type": "REQUIRES",
        "confidence": 0.9,
    }


async def test_tokens_saved_vs_raw_positive_after_compression(tmp_path: Path) -> None:
    """N verbose raw episodes collapsing to one fact: raw baseline >> rendered profile."""
    scope = _scope("token-compression")
    client = _client(tmp_path)
    padding = (
        "The guest called again to restate the same onboarding requirement in "
        "different words, with greetings, sign-offs, and unrelated small talk."
    )
    for index in range(3):
        await client.add_episode(
            name=f"restatement-{index}",
            episode_body=json.dumps(
                {
                    "memories": [_requirement_record("a signed vendor agreement before event setup")],
                    # Raw bodies also carry conversational padding the evolved
                    # profile never re-reads — part of the raw baseline cost.
                    "conversation": padding,
                }
            ),
            source=EpisodeType.JSON,
            scope=scope,
            reference_time=T0 + timedelta(minutes=index),
        )
    await client.run_dream_job(job_name="formation", now=T0 + timedelta(minutes=10))

    proof = await client.memory_evolution(scope=scope, as_of=T0 + timedelta(minutes=11))
    # Three raw episodes collapsed into fewer context-visible facts.
    assert proof.episode_count == 3
    assert proof.context_visible_relationship_count < proof.episode_count
    assert proof.tokens_raw_episodes > 0
    assert proof.tokens_rendered_profile > 0
    assert proof.tokens_saved_vs_raw > 0
    assert proof.tokens_saved_vs_raw == proof.tokens_raw_episodes - proof.tokens_rendered_profile
    assert proof.tokens_unbudgeted_facts > 0
    # No demotion happened, so the demotion dividend is measured as zero.
    assert proof.tokens_saved_by_demotion == 0

    # The reference render honours an explicit budget choice.
    budgeted = await client.memory_evolution(scope=scope, as_of=T0 + timedelta(minutes=11), token_budget=24)
    assert budgeted.tokens_rendered_profile <= proof.tokens_rendered_profile
    with pytest.raises(ValueError, match="token_budget"):
        await client.memory_evolution(scope=scope, token_budget=-1)


async def test_rollup_demotion_increases_tokens_saved_by_demotion(tmp_path: Path) -> None:
    """Rollup consolidation demotes members; the proof prices that dividend."""
    scope = _scope("token-demotion")
    client = _client(tmp_path)
    for index, obj in enumerate(
        (
            "shorter queue waits during peak afternoon hours",
            "shorter queue waits on holiday weekends",
            "shorter queue waits for the newest attractions",
        )
    ):
        await client.add_memory(
            subject="Guest Experience",
            predicate="prefers",
            object=obj,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.8 + 0.01 * index,
            reference_time=T0 + timedelta(minutes=index),
        )

    before = await client.memory_evolution(scope=scope, as_of=T0 + timedelta(minutes=30))
    assert before.demoted_relationship_count == 0
    assert before.tokens_saved_by_demotion == 0

    await client.run_dream_job(job_name="consolidation", now=T0 + timedelta(minutes=31))

    after = await client.memory_evolution(scope=scope, as_of=T0 + timedelta(minutes=32))
    assert after.rollup_relationship_count == 1
    assert after.demoted_relationship_count == 3
    # Three member lines were demoted behind one rollup line: the dividend is
    # positive and grew relative to the pre-consolidation proof.
    assert after.tokens_saved_by_demotion > before.tokens_saved_by_demotion
    assert after.tokens_saved_by_demotion > 0


async def test_measurement_fields_zero_or_none_on_empty_scope(tmp_path: Path) -> None:
    """An empty scope measures 0 tokens (never absent) and None rates (never fake 0.0)."""
    client = _client(tmp_path)
    proof = await client.memory_evolution(scope=_scope("empty-scope"))
    assert proof.tokens_raw_episodes == 0
    assert proof.tokens_unbudgeted_facts == 0
    assert proof.tokens_rendered_profile == 0
    assert proof.tokens_saved_vs_raw == 0
    assert proof.tokens_saved_by_demotion == 0
    assert proof.repeat_search_rate is None
    assert proof.answered_from_profile_rate is None
    payload = proof.model_dump(mode="json")
    for field in (
        "tokens_raw_episodes",
        "tokens_unbudgeted_facts",
        "tokens_rendered_profile",
        "tokens_saved_vs_raw",
        "tokens_saved_by_demotion",
        "repeat_search_rate",
        "answered_from_profile_rate",
    ):
        assert field in payload  # serialized (admin /api/evolution), never absent


def _platform(tmp_path: Path, name: str) -> AgentMemoryPlatform:
    return AgentMemoryPlatform.create(
        graph_path=tmp_path / f"{name}.sqlite",
        project_id="measurement-project",
        agent_ids=(AGENT_ID,),
        mode=AgentMemoryMode.SIMPLE,
        user_id="measurement-user",
    )


def _assistant_turn(text: str) -> TranscriptTurn:
    return TranscriptTurn(
        role="assistant",
        text=text,
        tool_names=(),
        file_paths=(),
        is_meta=False,
        is_compact_summary=False,
    )


async def test_repeat_search_rate_reflects_cross_session_repeats(tmp_path: Path) -> None:
    """The same query digest under two session boundaries counts as a repeat."""
    platform = _platform(tmp_path, "repeat-search")
    await platform.memory_remember(
        agent_id=AGENT_ID,
        subject="Deploy pipeline",
        predicate="requires",
        object="manual approval gate",
        relationship_type="REQUIRES",
    )
    repeated_query = "deploy approval gate"
    first = await platform.memory_search(
        agent_id=AGENT_ID,
        query=repeated_query,
        task_run_id="claude:sess-a:search:1",
    )
    assert first.results, "search must hit the remembered fact"
    # The RetrievalContract's query digest rides on every RETRIEVED event and
    # survives the storage round-trip.
    expected_digest = retrieval_query_digest(repeated_query)
    assert {event.query_digest for event in first.use_events} == {expected_digest}
    user_scope = platform.user_scope
    assert user_scope is not None
    stored = [
        event
        for event in platform.client.graph.use_events(scope_key=user_scope.key)
        if event.kind == UseEventKind.RETRIEVED
    ]
    assert stored and all(event.query_digest == expected_digest for event in stored)

    # Same digest again, but inside the SAME session: still no repeat.
    await platform.memory_search(
        agent_id=AGENT_ID,
        query=repeated_query,
        task_run_id="claude:sess-a:search:2",
    )
    same_session = await platform.client.memory_evolution(scope=user_scope)
    assert same_session.repeat_search_rate == 0.0

    # A second session re-discovers the same answer: 1/1 digests repeated.
    await platform.memory_search(
        agent_id=AGENT_ID,
        query=repeated_query,
        task_run_id="claude:sess-b:search:1",
    )
    repeated = await platform.client.memory_evolution(scope=user_scope)
    assert repeated.repeat_search_rate == 1.0

    # A distinct query searched once dilutes the rate to 1/2.
    second = await platform.memory_search(
        agent_id=AGENT_ID,
        query="approval gate for the deploy pipeline release",
        task_run_id="claude:sess-b:search:2",
    )
    assert second.results
    diluted = await platform.client.memory_evolution(scope=user_scope)
    assert diluted.repeat_search_rate == 0.5

    # Session boundaries derive from the WS-15 prefix scheme.
    assert session_boundary_for_task_run("claude:sess-a:search:1") == "claude:sess-a:"
    assert session_boundary_for_task_run("manual-task") == "manual-task"


async def test_answered_from_profile_rate_splits_injected_and_retrieved(
    tmp_path: Path,
) -> None:
    """One cited injected fact + one cited retrieved-only fact -> rate 0.5."""
    platform = _platform(tmp_path, "answered-from")
    session_id = "sess-origin"
    injected = await platform.memory_remember(
        agent_id=AGENT_ID,
        subject="Deploy pipeline",
        predicate="requires",
        object="manual approval gate",
        relationship_type="REQUIRES",
    )
    started = await platform.memory_start(
        agent_id=AGENT_ID,
        task_run_id=f"claude:{session_id}:startup:1",
    )
    assert injected.relationship_uuid in {event.relationship_uuid for event in started.use_events}
    # A second fact created AFTER the profile injection: the session can only
    # have reached it through search.
    retrieved = await platform.memory_remember(
        agent_id=AGENT_ID,
        subject="Rollback runbook",
        predicate="should",
        object="snapshot the config before rollout",
        relationship_type="SHOULD",
    )
    user_scope = platform.user_scope
    assert user_scope is not None
    await platform.client.record_memory_use(
        relationship_uuid=retrieved.relationship_uuid,
        scope=user_scope,
        kind=UseEventKind.RETRIEVED,
        task_run_id=f"claude:{session_id}:search:2",
        idempotency_key=f"claude:{session_id}:search:2:{retrieved.relationship_uuid}:retrieved",
        rank=0,
        retrieval_score=1.0,
        candidate_set_size=1,
        context_budget_competition=0,
        retrieval_policy_digest="test-policy-digest",
        query_digest=retrieval_query_digest("rollback runbook"),
        metadata={"agent_id": AGENT_ID},
    )

    scan = await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=(
            _assistant_turn(
                f"Kept the gate manual (REF {injected.relationship_uuid}) and followed "
                f"the runbook (REF {retrieved.relationship_uuid})."
            ),
        ),
        session_id=session_id,
        task_run_id=f"claude:{session_id}:session-end:3",
    )
    assert scan.cited_count == 2

    proof = await platform.client.memory_evolution(scope=user_scope)
    # One citation resolves to a profile injection, the other to a search
    # retrieval within the same session boundary.
    assert proof.answered_from_profile_rate == 0.5
