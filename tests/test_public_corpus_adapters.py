"""Opt-in integration checks for untouched official benchmark exports.

Set ``MEMOTRON_PUBLIC_CORPUS_DIR`` to a directory containing the official
``longmemeval_oracle.json``, ``locomo10.json``, and
the four MemoryAgentBench task exports.  The test never rewrites a raw file;
it validates real-schema chronology and scored-suite construction, then runs
a small isolated stateful replay slice derived from the LongMemEval source.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memotron import (
    CertificationCorpus,
    Memotron,
    LocalOfficialBenchmarkJudge,
    LoCoMoCorpusAdapter,
    LongMemEvalCorpusAdapter,
    MemoryAgentBenchCorpusAdapter,
    MemoryScope,
    ScopeKind,
    StatefulReplayOptions,
    certification_config,
)
from memotron.memory_bank import builtin_memory_bank

# needs the official evaluation corpus; the marker makes the CI lane selectable with `-m corpus`.
pytestmark = pytest.mark.corpus


def _public_corpus_dir() -> Path:
    configured = os.environ.get("MEMOTRON_PUBLIC_CORPUS_DIR", "").strip()
    if not configured:
        pytest.skip("set MEMOTRON_PUBLIC_CORPUS_DIR to run official corpus integration")
    root = Path(configured)
    if not root.is_dir():
        raise ValueError(f"MEMOTRON_PUBLIC_CORPUS_DIR is not a directory: {root}")
    return root


@pytest.mark.asyncio
async def test_official_public_exports_are_immutable_and_replayable(tmp_path) -> None:
    root = _public_corpus_dir()
    longmem_path = root / "longmemeval_oracle.json"
    locomo_path = root / "locomo10.json"
    memoryagentbench_paths = (
        root / "memoryagentbench_accurate_retrieval.parquet",
        root / "memoryagentbench_conflict_resolution.parquet",
        root / "memoryagentbench_long_range_understanding.parquet",
        root / "memoryagentbench_test_time_learning.parquet",
    )
    if not all(path.is_file() for path in (longmem_path, locomo_path, *memoryagentbench_paths)):
        raise ValueError(
            "MEMOTRON_PUBLIC_CORPUS_DIR must contain longmemeval_oracle.json, "
            "locomo10.json, and all four memoryagentbench_*.parquet exports"
        )
    longmem_raw = longmem_path.read_bytes()
    locomo_raw = locomo_path.read_bytes()
    memoryagentbench_raw = {path: path.read_bytes() for path in memoryagentbench_paths}
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="public-corpus-certification")

    longmem = LongMemEvalCorpusAdapter.load(longmem_path, scope=scope)
    locomo = LoCoMoCorpusAdapter.load(locomo_path, scope=scope)
    memoryagentbench = tuple(MemoryAgentBenchCorpusAdapter.load(path, scope=scope) for path in memoryagentbench_paths)
    longmem_suite = LongMemEvalCorpusAdapter.load_suite(longmem_path, scope=scope)
    locomo_suite = LoCoMoCorpusAdapter.load_suite(locomo_path, scope=scope)
    memoryagentbench_suites = tuple(
        MemoryAgentBenchCorpusAdapter.load_suite(path, scope=scope) for path in memoryagentbench_paths
    )

    assert len(longmem.episodes) >= 500
    assert len(locomo.episodes) >= 100
    assert all(corpus.episodes for corpus in memoryagentbench)
    assert len(longmem_suite.scenarios) >= 500
    assert len(locomo_suite.scenarios) >= 10
    assert all(suite.scenarios for suite in memoryagentbench_suites)
    assert all(scenario.questions for suite in memoryagentbench_suites for scenario in suite.scenarios)
    assert all(len(suite.suite_digest) == 64 for suite in (longmem_suite, locomo_suite, *memoryagentbench_suites))
    assert longmem_suite.metadata["official_metric"] == "llm_as_judge"
    assert locomo_suite.metadata["official_metric"] == "category_f1_or_adversarial_abstention"
    assert [suite.metadata["official_metric"] for suite in memoryagentbench_suites] == [
        "substring_exact_match",
        "substring_exact_match",
        "exact_match",
        "exact_match",
    ]
    locomo_judge = LocalOfficialBenchmarkJudge(source="LoCoMo")
    representative_locomo_questions = {
        question.category: question for scenario in locomo_suite.scenarios for question in scenario.questions
    }
    for category in ("1", "2", "3", "4", "5"):
        question = representative_locomo_questions[category]
        response = "not mentioned" if category == "5" else question.expected_answers[0]
        assert locomo_judge(question, response) == 1.0
    memoryagentbench_judge = LocalOfficialBenchmarkJudge(source="MemoryAgentBench")
    assert all(
        memoryagentbench_judge(scenario.questions[0], scenario.questions[0].expected_answers[0]) == 1.0
        for suite in memoryagentbench_suites
        for scenario in suite.scenarios[:1]
    )
    assert list(longmem.chronological_episodes()) == sorted(
        longmem.episodes,
        key=lambda episode: (episode.reference_time, episode.created_at, episode.uuid),
    )
    assert longmem_path.read_bytes() == longmem_raw
    assert locomo_path.read_bytes() == locomo_raw
    assert all(path.read_bytes() == raw for path, raw in memoryagentbench_raw.items())

    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    baseline = motives["capture-preferences"]
    candidate = motives["learn-compliance-requirements"]
    replay_slice = CertificationCorpus(
        name="LongMemEval-oracle-chronological-smoke",
        source="LongMemEval",
        episodes=longmem.chronological_episodes()[:12],
        metadata={"raw_corpus_digest": longmem.corpus_digest, "raw_data_mutated": False},
    )
    client = Memotron(
        config=certification_config(motives=(baseline, candidate)),
        graph_path=tmp_path / "production.sqlite",
    )
    before = client.graph.graph_state_hash(scope.key)
    comparison = await client.stateful_policy_comparison(
        corpus=replay_slice,
        scope=scope,
        baseline_motive=baseline,
        candidate_motive=candidate,
        options=StatefulReplayOptions(
            model_identifier="rule-based@v1",
            temperature=0.0,
            repetitions=2,
            minimum_stability_score=1.0,
            require_material_policy_delta=False,
        ),
    )
    assert comparison.passed is True
    assert client.graph.graph_state_hash(scope.key) == before
