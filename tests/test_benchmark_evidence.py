"""WS-22 T30: committed benchmark corpus + hermetic scored evidence.

Loads the repo-owned synthetic corpus (``benchmarks/memotron_kb_v1/corpus.json``,
fictional Northport Systems enterprise-docs domain) through the
LongMemEvalCorpusAdapter, replays it end-to-end through the REAL
``client.public_benchmark`` harness — fresh SQLite store per scenario and
repetition, rule-based extraction, local embeddings, the deterministic
profile-top-evidence executor, and the LoCoMo category-F1 official judge —
and regression-guards retrieval quality with the actual scores.

The measured aggregate on this corpus is 1.0 and is deterministic (the
in-test repetition check proves it); the floors below are set from that real
result with a small honesty margin, so any true retrieval/formation/
supersession regression fails CI while cosmetic partial-credit drift cannot
flap the suite.  Sensitivity is real, not assumed: a degenerate
first-fact executor scores ~0.50 on this corpus, and disabling single-active
supersession drops the temporal-current questions to ~0.58.

Fully hermetic: no network, no API keys, no operator-supplied exports.
"""

from __future__ import annotations

import json
from pathlib import Path

from memotron import (
    Memotron,
    LocalOfficialBenchmarkJudge,
    LongMemEvalCorpusAdapter,
    MemoryScope,
    ProfileTopEvidenceExecutor,
    PublicBenchmarkReport,
    ScopeKind,
    StatefulReplayOptions,
    certification_config,
)
from memotron.config import Motive

REPO_ROOT = Path(__file__).resolve().parent.parent
CORPUS_PATH = REPO_ROOT / "benchmarks" / "memotron_kb_v1" / "corpus.json"
REPORT_PATH = REPO_ROOT / "benchmarks" / "memotron_kb_v1" / "report.json"

AGGREGATE_FLOOR = 0.95
BAND_FLOOR = 0.90
EXPECTED_SCENARIOS_PER_BAND = 8


def _band_prefixes() -> dict[str, str]:
    """Band → question-id prefix, read from the committed corpus _meta."""
    meta = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))["_meta"]
    return {band: spec["question_id_prefix"] for band, spec in meta["bands"].items()}


def _band_for(question_id: str, prefixes: dict[str, str]) -> str:
    for band, prefix in prefixes.items():
        if question_id.startswith(prefix):
            return band
    raise AssertionError(f"question id {question_id!r} belongs to no declared band")


async def _run_hermetic_benchmark(tmp_path: Path) -> PublicBenchmarkReport:
    motive = Motive(
        name="kb-recall",
        goal="Retain every documented fact from the knowledge base for later recall",
        allowed_memory_types=(),
    )
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="benchmark-evidence")
    suite = LongMemEvalCorpusAdapter.load_suite(CORPUS_PATH, scope=scope)
    executor = ProfileTopEvidenceExecutor()
    judge = LocalOfficialBenchmarkJudge(source="LoCoMo")
    client = Memotron(
        config=certification_config(motives=(motive,)),
        graph_path=tmp_path / "benchmark-evidence.sqlite",
    )
    try:
        return await client.public_benchmark(
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
            answer_executor=executor,
            answer_judge=judge,
            answer_runtime_identifier=executor.identifier,
            judge_identifier=judge.identifier,
        )
    finally:
        client.graph.close()


def test_committed_corpus_loads_with_synthetic_identity_and_band_counts() -> None:
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="benchmark-evidence")
    suite = LongMemEvalCorpusAdapter.load_suite(CORPUS_PATH, scope=scope)
    # The _meta block keeps the synthetic corpus honestly labeled — it is
    # LongMemEval-SHAPED, never reported under the official benchmark's name.
    assert suite.name == "memotron-kb-v1"
    assert suite.source == "memotron-kb-v1"
    assert suite.metadata["_meta"]["provenance"] == {
        "source": "synthetic",
        "license": "internal",
        "generator": "hand-authored",
    }
    prefixes = _band_prefixes()
    counts: dict[str, int] = dict.fromkeys(prefixes, 0)
    for scenario in suite.scenarios:
        assert scenario.source == "memotron-kb-v1"
        assert len(scenario.questions) == 1
        counts[_band_for(scenario.scenario_id, prefixes)] += 1
    assert counts == {
        "single_fact_lookup": EXPECTED_SCENARIOS_PER_BAND,
        "cross_page_join": EXPECTED_SCENARIOS_PER_BAND,
        "temporal_supersession": EXPECTED_SCENARIOS_PER_BAND,
    }


async def test_hermetic_benchmark_scores_hold_the_committed_floors(tmp_path: Path) -> None:
    report = await _run_hermetic_benchmark(tmp_path)

    # Deterministic across repetitions: every repetition scored identically.
    per_repetition = {tuple((case.question_id, case.score) for case in sample.scores) for sample in report.samples}
    assert len(per_repetition) == 1
    assert report.stability_score == 1.0
    assert report.lifecycle_invariants_passed is True
    assert report.passed is True

    # The honest floors, chosen from the actual measured result (1.0).
    assert report.mean_score >= AGGREGATE_FLOOR
    prefixes = _band_prefixes()
    band_scores: dict[str, list[float]] = {}
    for case in report.samples[0].scores:
        band_scores.setdefault(_band_for(case.question_id, prefixes), []).append(case.score)
    assert {band: len(scores) for band, scores in band_scores.items()} == {
        "single_fact_lookup": EXPECTED_SCENARIOS_PER_BAND,
        "cross_page_join": EXPECTED_SCENARIOS_PER_BAND,
        "temporal_supersession": EXPECTED_SCENARIOS_PER_BAND,
    }
    for band, scores in band_scores.items():
        assert sum(scores) / len(scores) >= BAND_FLOOR, (band, scores)

    # The committed report is live evidence, not a stale artifact: the fresh
    # hermetic run must reproduce its persisted band aggregates and mean.
    committed = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    assert committed["report"]["mean_score"] == report.mean_score
    for band, scores in band_scores.items():
        assert committed["bands"][band]["scenario_count"] == len(scores)
        assert committed["bands"][band]["mean_score"] == round(sum(scores) / len(scores), 6)
