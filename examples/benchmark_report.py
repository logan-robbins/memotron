"""
WDPR Memotron — hermetic benchmark evidence  (WS-22 T30)

Scores the repo-owned synthetic corpus ``benchmarks/memotron_kb_v1/corpus.json``
(fictional Northport Systems enterprise-docs domain; three bands from the
evaluation design — single-fact lookup, cross-page join, temporal supersession)
through the REAL ``client.public_benchmark`` harness, fully offline:

  corpus  → LongMemEvalCorpusAdapter (object root carries the synthetic _meta)
  replay  → one fresh SQLite store per scenario/repetition, chronological
            formation + consolidation + pruning (rule-based extraction,
            local embeddings — no network, no API keys)
  answers → ProfileTopEvidenceExecutor: the top profile evidence line,
            selected by deterministic token overlap (never sees gold)
  judge   → LocalOfficialBenchmarkJudge(source="LoCoMo") category F1
            (the deterministic string/F1 official-protocol variant)

Writes ``benchmarks/memotron_kb_v1/report.json`` — the PublicBenchmarkReport
dump plus run metadata and per-band aggregates.  Scores are deterministic:
running twice produces identical band scores and an identical report subtree.

  uv run examples/benchmark_report.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

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

BANDS: dict[str, str] = {
    "single-": "single_fact_lookup",
    "join-": "cross_page_join",
    "temporal-": "temporal_supersession",
}

KB_RECALL_MOTIVE = Motive(
    name="kb-recall",
    goal="Retain every documented fact from the knowledge base for later recall",
    allowed_memory_types=(),  # allow all types: the corpus mixes the full taxonomy
)


def band_for(question_id: str) -> str:
    for prefix, band in BANDS.items():
        if question_id.startswith(prefix):
            return band
    raise ValueError(f"question id {question_id!r} belongs to no known band")


def band_aggregates(report: PublicBenchmarkReport) -> dict[str, dict[str, float | int]]:
    """Per-band mean scores over the FIRST repetition (all repetitions are
    asserted identical below, so any one of them is the measurement)."""
    scores: dict[str, list[float]] = {}
    for case in report.samples[0].scores:
        scores.setdefault(band_for(case.question_id), []).append(case.score)
    return {
        band: {
            "scenario_count": len(values),
            "mean_score": round(sum(values) / len(values), 6),
        }
        for band, values in sorted(scores.items())
    }


def git_describe() -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), "describe", "--always", "--dirty", "--tags"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


async def run_benchmark() -> tuple[PublicBenchmarkReport, dict[str, dict[str, float | int]]]:
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="memotron-kb-benchmark")
    suite = LongMemEvalCorpusAdapter.load_suite(CORPUS_PATH, scope=scope)
    executor = ProfileTopEvidenceExecutor()
    judge = LocalOfficialBenchmarkJudge(source="LoCoMo")
    with TemporaryDirectory(prefix="memotron-benchmark-report-") as tmp:
        client = Memotron(
            config=certification_config(motives=(KB_RECALL_MOTIVE,)),
            graph_path=Path(tmp) / "benchmark.sqlite",
        )
        try:
            report = await client.public_benchmark(
                suite=suite,
                scope=scope,
                motive=KB_RECALL_MOTIVE,
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
    # Hermetic determinism: every repetition must score identically.
    per_repetition = [tuple((case.question_id, case.score) for case in sample.scores) for sample in report.samples]
    if len(set(per_repetition)) != 1:
        raise RuntimeError("benchmark repetitions disagree — the harness is not deterministic")
    return report, band_aggregates(report)


def main() -> int:
    report, bands = asyncio.run(run_benchmark())
    payload = {
        "run": {
            "command": "uv run examples/benchmark_report.py",
            "corpus_path": str(CORPUS_PATH.relative_to(REPO_ROOT)),
            "generated_at": datetime.now(UTC).isoformat(),
            "git_describe": git_describe(),
            "adapter": "LongMemEvalCorpusAdapter",
            "answer_runtime_identifier": report.answer_runtime_identifier,
            "judge_identifier": report.judge_identifier,
            "corpus": "synthetic (fictional Northport Systems domain); see corpus.json _meta",
        },
        "bands": bands,
        "report": report.model_dump(mode="json"),
    }
    REPORT_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print("Memotron hermetic benchmark — memotron_kb_v1")
    print(f"  suite:  {report.suite_name}  (source: {report.suite_source})")
    print(f"  judge:  {report.judge_identifier}")
    print(f"  runtime: {report.answer_runtime_identifier}")
    print(f"  repetitions: {len(report.samples)}  stability: {report.stability_score:.3f}")
    for band, values in bands.items():
        print(f"  band {band:<24} scenarios={values['scenario_count']:<3} mean={values['mean_score']:.4f}")
    print(f"  aggregate mean score: {report.mean_score:.4f}")
    print(f"  lifecycle invariants passed: {report.lifecycle_invariants_passed}")
    print(f"  passed: {report.passed}")
    print(f"  report written to {REPORT_PATH.relative_to(REPO_ROOT)}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
