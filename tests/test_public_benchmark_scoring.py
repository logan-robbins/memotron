from __future__ import annotations

import pytest

from memotron import (
    CertificationCorpus,
    Memotron,
    LocalOfficialBenchmarkJudge,
    MemoryScope,
    PublicBenchmarkQuestion,
    PublicBenchmarkScenario,
    PublicBenchmarkSuite,
    ScopeKind,
    StatefulReplayOptions,
    builtin_motive_fixture_corpora,
    certification_config,
)
from memotron.memory_bank import builtin_memory_bank


@pytest.mark.asyncio
async def test_public_benchmark_separates_gold_from_answer_runtime_and_preserves_production(
    tmp_path,
) -> None:
    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    motive = motives["learn-compliance-requirements"]
    source_corpus = builtin_motive_fixture_corpora()[motive.name]
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="public-benchmark")
    corpus = CertificationCorpus(
        name="public-benchmark-synthetic-history",
        source="test-public-suite",
        episodes=source_corpus.episodes[:2],
    )
    question = PublicBenchmarkQuestion(
        question_id="requirement-1",
        question="What evidence is required before launch?",
        expected_answers=("SOC2 evidence",),
        category="knowledge_update",
        asked_at=corpus.chronological_episodes()[-1].reference_time,
        evidence_identifiers=("builtin-motive-fixture-0",),
    )
    suite = PublicBenchmarkSuite(
        name="synthetic-public-suite",
        source="test-public-suite",
        scenarios=(
            PublicBenchmarkScenario(
                scenario_id="requirement-history",
                source="test-public-suite",
                corpus=corpus,
                questions=(question,),
            ),
        ),
    )
    client = Memotron(
        config=certification_config(motives=(motive,)),
        graph_path=tmp_path / "production.sqlite",
    )
    before = client.graph.graph_state_hash(scope.key)
    requests = []

    async def answer_executor(request):
        assert not hasattr(request, "expected_answers")
        assert "SOC2 evidence before launch" in request.profile_context
        requests.append(request)
        return "SOC2 evidence"

    async def answer_judge(public_question, response):
        return response.casefold() == public_question.expected_answers[0].casefold()

    report = await client.public_benchmark(
        suite=suite,
        scope=scope,
        motive=motive,
        options=StatefulReplayOptions(
            model_identifier="rule-based@v1",
            temperature=0.0,
            repetitions=2,
            minimum_stability_score=1.0,
            require_material_policy_delta=False,
        ),
        answer_executor=answer_executor,
        answer_judge=answer_judge,
        answer_runtime_identifier="test-answer-runtime@v1",
        judge_identifier="exact-match@test-v1",
    )

    assert report.passed is True
    assert report.mean_score == 1.0
    assert report.stability_score == 1.0
    assert report.lifecycle_invariants_passed is True
    assert len(requests) == 2
    assert all(request.profile_relationship_uuids for request in requests)
    assert "SOC2 evidence" not in report.model_dump_json()
    assert client.graph.graph_state_hash(scope.key) == before
    assert client.graph.relationships() == []


@pytest.mark.asyncio
async def test_public_benchmark_uses_memoryagentbench_source_metric_in_isolated_replay(tmp_path) -> None:
    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    motive = motives["learn-compliance-requirements"]
    source_corpus = builtin_motive_fixture_corpora()[motive.name]
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="mab-public-benchmark")
    corpus = CertificationCorpus(
        name="memoryagentbench-synthetic-history",
        source="MemoryAgentBench",
        episodes=(source_corpus.episodes[0],),
    )
    suite = PublicBenchmarkSuite(
        name="memoryagentbench-synthetic-suite",
        source="MemoryAgentBench",
        scenarios=(
            PublicBenchmarkScenario(
                scenario_id="mab-retrieval-history",
                source="MemoryAgentBench",
                corpus=corpus,
                questions=(
                    PublicBenchmarkQuestion(
                        question_id="mab-retrieval-question",
                        question="What evidence is required before launch?",
                        expected_answers=("SOC2 evidence before launch",),
                        category="accurate-retrieval",
                        asked_at=corpus.chronological_episodes()[-1].reference_time,
                        metadata={"official_metric": "substring_exact_match"},
                    ),
                ),
            ),
        ),
    )
    client = Memotron(
        config=certification_config(motives=(motive,)),
        graph_path=tmp_path / "production.sqlite",
    )
    judge = LocalOfficialBenchmarkJudge(source="MemoryAgentBench")

    async def answer_executor(request):
        assert not hasattr(request, "expected_answers")
        return "SOC2 evidence before launch"

    report = await client.public_benchmark(
        suite=suite,
        scope=scope,
        motive=motive,
        options=StatefulReplayOptions(
            model_identifier="rule-based@v1",
            temperature=0.0,
            repetitions=2,
            minimum_stability_score=1.0,
            require_material_policy_delta=False,
        ),
        answer_executor=answer_executor,
        answer_judge=judge,
        answer_runtime_identifier="fixture-answer-runtime@v1",
        judge_identifier=judge.identifier,
    )

    assert report.passed is True
    assert report.mean_score == 1.0
    assert report.judge_identifier == "memoryagentbench-official-metrics@455306d"
