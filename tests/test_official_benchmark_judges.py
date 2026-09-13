from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Self

import pytest

from memotron import (
    BenchmarkAnswerRequest,
    LocalOfficialBenchmarkJudge,
    LongMemEvalOfficialJudge,
    OpenAICompatibleBenchmarkRuntime,
    PublicBenchmarkQuestion,
)


def _question(
    *,
    category: str,
    expected_answers: tuple[str, ...],
    metadata: dict[str, object] | None = None,
) -> PublicBenchmarkQuestion:
    return PublicBenchmarkQuestion(
        question_id="question-1",
        question="What is the verified answer?",
        expected_answers=expected_answers,
        category=category,
        asked_at=datetime(2026, 7, 15, tzinfo=UTC),
        metadata=metadata or {},
    )


def test_local_official_benchmark_judges_match_source_metric_rules() -> None:
    locomo = LocalOfficialBenchmarkJudge(source="LoCoMo")
    assert (
        locomo(
            _question(category="2", expected_answers=("The customer prefers concise status updates",)),
            "Customer prefers concise status updates.",
        )
        == 1.0
    )
    assert (
        locomo(
            _question(category="1", expected_answers=("security review, SOC2 report",)),
            "SOC2 report, security review",
        )
        == 1.0
    )
    assert (
        locomo(
            _question(category="5", expected_answers=("a misleading answer",)),
            "That is not mentioned in the conversation.",
        )
        == 1.0
    )

    memory_agent_bench = LocalOfficialBenchmarkJudge(source="MemoryAgentBench")
    assert (
        memory_agent_bench(
            _question(
                category="accurate-retrieval",
                expected_answers=("SOC2 report",),
                metadata={"official_metric": "substring_exact_match"},
            ),
            "The required document is a SOC2 report before launch.",
        )
        == 1.0
    )
    assert (
        memory_agent_bench(
            _question(
                category="test-time-learning",
                expected_answers=("A vendor questionnaire",),
                metadata={"official_metric": "exact_match"},
            ),
            "vendor questionnaire",
        )
        == 1.0
    )


@pytest.mark.asyncio
async def test_openai_runtime_and_longmemeval_judge_keep_gold_out_of_answer_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_payloads: list[dict[str, object]] = []
    completion_contents = iter(("SOC2 report", "yes"))

    class Response:
        def __init__(self, content: str) -> None:
            self._content = content

        def read(self) -> bytes:
            return json.dumps({"choices": [{"message": {"content": self._content}}]}).encode("utf-8")

        def __enter__(self) -> Self:
            return self

        def __exit__(self, *_: object) -> None:
            return None

    def fake_urlopen(request, *, timeout: float):
        assert timeout == 15.0
        assert request.get_header("Authorization") == "Bearer test-key"
        captured_payloads.append(json.loads(request.data.decode("utf-8")))
        return Response(next(completion_contents))

    monkeypatch.setenv("MEMOTRON_BENCHMARK_TEST_KEY", "test-key")
    # `gateway`, not `certification`: the urlopen call lives in
    # OpenAICompatibleTransport._open. urllib.request is one shared module
    # object, so the reach is identical either way.
    monkeypatch.setattr("memotron.gateway.urllib_request.urlopen", fake_urlopen)
    runtime = OpenAICompatibleBenchmarkRuntime(
        model="benchmark-answer-model",
        base_url="http://benchmark.test/v1",
        api_key_env="MEMOTRON_BENCHMARK_TEST_KEY",
        timeout_seconds=15,
    )
    request = BenchmarkAnswerRequest(
        source="LongMemEval",
        suite_digest="a" * 64,
        scenario_id="scenario-1",
        scenario_digest="b" * 64,
        question_id="temporal-1",
        category="temporal-reasoning",
        question="How long has the requirement been active?",
        asked_at=datetime(2026, 7, 15, tzinfo=UTC),
        motive_name="learn-compliance-requirements",
        model_identifier="benchmark-answer-model",
        profile_context="Acme requires a SOC2 report before launch.",
        profile_relationship_uuids=("relationship-1",),
    )

    answer = await runtime(request)
    assert answer == "SOC2 report"
    assert "Correct Answer" not in captured_payloads[0]["messages"][1]["content"]
    assert "SOC2 report before launch" in captured_payloads[0]["messages"][1]["content"]

    judge = LongMemEvalOfficialJudge(runtime)
    question = _question(category="temporal-reasoning", expected_answers=("18 days",))
    assert await judge(question, answer) is True
    judge_prompt = captured_payloads[1]["messages"][1]["content"]
    assert "off-by-one" in judge_prompt
    assert "Correct Answer: 18 days" in judge_prompt
    assert "Model Response: SOC2 report" in judge_prompt
    assert "longmemeval-official-llm-judge" in judge.identifier
