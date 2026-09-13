"""T1-29: a public benchmark that scored zero must not report ``passed=True``.

The defect
----------
``run_public_benchmark`` (``certification.py:2143-2162``) builds ``failures`` from exactly two
things -- lifecycle invariants and ``stability_score`` -- then returns ``passed=not failures``.
``mean_score`` is computed at ``:2133`` and reported at ``:2157`` and **never compared to
anything**. Before this change ``grep -n 'minimum_' certification.py`` returned only
``minimum_stability_score``, so there was no accuracy threshold even available to configure.

Why it is worse than "no accuracy gate"
---------------------------------------
Stability is ``1.0 - min(1.0, 2 * stddev)`` (``certification.py:2139``), computed over the
per-repetition means. Measured 2026-09-01:

    every repetition scores 0.0  -> stddev 0.000 -> stability 1.000 -> PASSES the only gate
    means (0.9, 0.4, 0.8)        -> stddev 0.216 -> stability 0.568 -> FAILS it

So **consistent total failure is the best-scoring input to the only numeric gate there is**,
and a genuinely capable but variable run is the one that gets rejected. This is the module
that underwrites quality claims about the product.

What the fix does, and deliberately does not do
-----------------------------------------------
Two separate things, because they answer different questions:

* **``mean_score <= 0`` is an unconditional failure.** It is not a tunable policy -- a benchmark
  that answered nothing correctly did not pass, under any configuration. It cannot be switched
  off, which is the point: the filed defect is exactly that this case returned True.
* **``minimum_mean_score`` is the tunable quality bar, and defaults to 0.0** -- i.e. no bar
  beyond the unconditional one. That default is chosen, not lazy. Picking a real number here
  (0.5? 0.8?) without a measured baseline for each suite would be a threshold calibrated
  against nothing, which is the same anti-pattern this branch has now found nine times. The
  repo's own synthetic suite scores 1.0, which is an ideal rather than a baseline.

The consequence worth stating: a run scoring 0.001 on every repetition still passes by default.
That is a real remaining gap and it is a *policy* gap, closed by configuring
``minimum_mean_score`` per suite, not by inventing a global constant here.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from memotron import (
    CertificationCorpus,
    Memotron,
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

MOTIVE_NAME = "learn-compliance-requirements"


def _suite(scope: MemoryScope, *, questions: int = 1) -> PublicBenchmarkSuite:
    """A suite with `questions` questions, so a judge can produce a FRACTIONAL mean score.

    One question can only ever score 0.0 or 1.0, which is enough for the unconditional
    zero check but cannot exercise the tunable threshold at all -- there is no score
    strictly between the floor and the 1.0 maximum to put a bar underneath. Two questions
    give 0.5, which is what makes `test_the_configurable_threshold_rejects...` real.
    """
    source_corpus = builtin_motive_fixture_corpora()[MOTIVE_NAME]
    corpus = CertificationCorpus(
        name="t1-29-history",
        source="test-public-suite",
        episodes=source_corpus.episodes[:2],
    )
    asked_at = corpus.chronological_episodes()[-1].reference_time
    return PublicBenchmarkSuite(
        name="t1-29-suite",
        source="test-public-suite",
        scenarios=(
            PublicBenchmarkScenario(
                scenario_id="requirement-history",
                source="test-public-suite",
                corpus=corpus,
                questions=tuple(
                    PublicBenchmarkQuestion(
                        question_id=f"requirement-{index}",
                        question="What evidence is required before launch?",
                        expected_answers=("SOC2 evidence",),
                        category="knowledge_update",
                        asked_at=asked_at,
                        evidence_identifiers=("builtin-motive-fixture-0",),
                    )
                    for index in range(questions)
                ),
            ),
        ),
    )


async def _run(
    tmp_path,
    *,
    judge: Callable[..., object],
    minimum_mean_score: float | None = None,
    questions: int = 1,
):
    """Run the benchmark with a judge we control, so mean_score is exactly what we choose."""
    motive = {m.name: m for m in builtin_memory_bank().motives}[MOTIVE_NAME]
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="t1-29")
    client = Memotron(
        config=certification_config(motives=(motive,)),
        graph_path=tmp_path / "production.sqlite",
    )

    async def answer_executor(request):
        return "SOC2 evidence"

    option_kwargs: dict[str, object] = {
        "model_identifier": "rule-based@v1",
        "temperature": 0.0,
        "repetitions": 2,
        "minimum_stability_score": 1.0,
        "require_material_policy_delta": False,
    }
    if minimum_mean_score is not None:
        option_kwargs["minimum_mean_score"] = minimum_mean_score

    return await client.public_benchmark(
        suite=_suite(scope, questions=questions),
        scope=scope,
        motive=motive,
        options=StatefulReplayOptions(**option_kwargs),
        answer_executor=answer_executor,
        answer_judge=judge,
        answer_runtime_identifier="test-answer-runtime@v1",
        judge_identifier="controlled@test-v1",
    )


async def _always_wrong(public_question, response) -> bool:
    return False


async def _always_right(public_question, response) -> bool:
    return True


def _alternating_judge() -> Callable[..., object]:
    """Judges `requirement-0` right and `requirement-1` wrong, giving mean_score 0.5.

    Keyed on the question id rather than on call order, deliberately. An order-counting
    judge would alternate ACROSS repetitions too, so the two repetitions would disagree,
    stability would drop, and the STABILITY gate would reject the run -- the test would go
    green for the wrong reason and stop testing the accuracy bar at all.
    """

    async def judge(public_question, response) -> bool:
        return public_question.question_id.endswith("0")

    return judge


# ------------------------------------------------------------------ the defect (T1-29)
@pytest.mark.asyncio
async def test_a_benchmark_that_scored_zero_does_not_pass(tmp_path) -> None:
    """RED before the fix: every answer judged wrong, and the report said ``passed=True``."""
    report = await _run(tmp_path, judge=_always_wrong)

    assert report.mean_score == 0.0, "fixture must actually score zero, or this proves nothing"
    assert report.passed is False, (
        "a benchmark that answered nothing correctly reported passed=True. `mean_score` is "
        "computed and never compared to anything (T1-29)."
    )
    assert "benchmark_mean_score_is_zero" in report.failures


@pytest.mark.asyncio
async def test_scoring_zero_still_produces_a_perfect_stability_score(tmp_path) -> None:
    """Pins the MECHANISM, so a future change cannot quietly make the headline test vacuous.

    The reason zero used to pass is not that stability was lenient -- it is that stability
    measures agreement between repetitions, and total failure is perfectly reproducible. If
    this assertion ever fails, the stability formula changed and the test above may be passing
    for a different reason than the one it documents.
    """
    report = await _run(tmp_path, judge=_always_wrong)

    assert report.mean_score == 0.0
    assert report.stability_score == 1.0, (
        "consistent total failure should still be maximally STABLE -- that is the whole trap"
    )
    assert "benchmark_score_stability_below_threshold" not in report.failures, (
        "the stability gate is not what catches this, and must not be what catches it"
    )


@pytest.mark.asyncio
async def test_a_benchmark_that_scored_perfectly_still_passes(tmp_path) -> None:
    """The control, and it must pass BEFORE the fix as well as after.

    Without it the headline assertion could be satisfied by failing every benchmark, which
    would look like a fix and be a worse defect.
    """
    report = await _run(tmp_path, judge=_always_right)

    assert report.mean_score == 1.0
    assert report.passed is True, "a benchmark that answered everything correctly must still pass"
    assert report.failures == ()


@pytest.mark.asyncio
async def test_the_stability_gate_fires_when_repetitions_disagree(tmp_path) -> None:
    """The POSITIVE case for the stability gate, which nothing drove before 2026-09-01.

    Every other test in this file asserts the gate is *silent*
    (``"benchmark_score_stability_below_threshold" not in report.failures``), and
    ``_alternating_judge``'s docstring goes out of its way to avoid tripping it. So the branch
    that appends the failure had never executed, and a gate that has only ever been observed
    NOT firing is indistinguishable from one that cannot fire -- the exact defect class this
    repo keeps finding, and the reason ``test_a_benchmark_that_scored_zero_does_not_pass``
    exists three tests above.

    Found by diff-coverage, not by reading: repointing an unrelated import shifted this
    function's line numbers, which pulled it into the measured diff for the first time.

    The judge counts CALLS rather than keying on question id, so the two repetitions disagree
    about the same question -- which is precisely the shape ``_alternating_judge`` warns
    against, used deliberately here.
    """
    calls = {"n": 0}

    async def order_counting_judge(public_question, response) -> bool:
        calls["n"] += 1
        return calls["n"] % 2 == 1

    report = await _run(tmp_path, judge=order_counting_judge)

    assert report.stability_score < 1.0, (
        "repetitions that disagree must lower stability, or the gate below is untestable"
    )
    assert "benchmark_score_stability_below_threshold" in report.failures
    assert report.passed is False


# ------------------------------------------------------ the tunable bar, above the hard floor
@pytest.mark.asyncio
async def test_the_configurable_threshold_rejects_a_score_below_it(tmp_path) -> None:
    """The tunable bar must actually bite on a real, fractional score.

    An earlier version of this test only checked the equality boundary and a validation
    error, and a break test caught that: deleting the whole `minimum_mean_score` branch from
    `certification.py` changed nothing, because no test ever produced a score strictly
    between the unconditional floor and the 1.0 maximum. Two questions, one judged right,
    gives 0.5 -- which is the only way to put a bar underneath a passing run.
    """
    half_right = _alternating_judge()
    report = await _run(tmp_path, judge=half_right, questions=2, minimum_mean_score=0.8)

    assert report.mean_score == 0.5, "fixture must score fractionally, or the bar cannot be tested"
    assert report.passed is False
    assert "benchmark_mean_score_below_threshold" in report.failures
    assert "benchmark_mean_score_is_zero" not in report.failures, (
        "0.5 is not zero; the unconditional floor must not be what rejected this"
    )


@pytest.mark.asyncio
async def test_the_same_score_passes_a_bar_it_meets(tmp_path) -> None:
    """The control for the test above: 0.5 against a 0.5 bar passes -- the gate is `<`, not `<=`."""
    report = await _run(tmp_path, judge=_alternating_judge(), questions=2, minimum_mean_score=0.5)

    assert report.mean_score == 0.5
    assert report.passed is True, "mean_score == minimum_mean_score must pass"
    assert report.failures == ()


@pytest.mark.asyncio
async def test_the_default_adds_no_bar_beyond_the_unconditional_zero_check(tmp_path) -> None:
    """Pins the deliberate default, so nobody 'tightens' it to an invented constant unnoticed.

    ``minimum_mean_score`` defaults to 0.0 on purpose -- see this module's docstring. A future
    change that picks 0.5 or 0.8 without a measured per-suite baseline would be a threshold
    calibrated against nothing, and this test is where that argument is recorded.
    """
    assert StatefulReplayOptions(model_identifier="m", temperature=0.0).minimum_mean_score == 0.0

    report = await _run(tmp_path, judge=_always_right)
    assert report.passed is True
