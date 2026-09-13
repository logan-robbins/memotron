"""T0-14: each side of a policy comparison must be accepted on ITS OWN evidence.

``stage_policy_contract``'s docstring is the contract:

    a baseline is accepted for migration only when ITS lifecycle invariants and replay
    stability pass, while a candidate must satisfy the complete comparison gate

The old acceptance rule broke that promise three separate ways, and this file covers all three.
Each has a paired control, because a gate that refuses everything satisfies the headline
assertion while being a worse defect than the one it replaced.

1. **The wrong side's invariants.** ``StatefulPolicyComparison.protected_invariants`` is built
   from the CANDIDATE only — ``certification.py:1929`` reads
   ``invariants = tuple(candidate.protected_invariants)``. The rule was
   ``baseline_safe = all(invariant.passed for invariant in certification.protected_invariants)``,
   so staging a baseline evaluated the candidate's replay and a baseline whose own replay
   violated a protected invariant was staged with ``certification_passed=True``.

2. **The wrong side's stability.** ``stability_score`` is JOINT — ``certification.py:1932``
   computes ``1.0 - max(baseline_flip_rate, candidate_flip_rate)`` — so a perfectly reproducible
   baseline was refused whenever the CANDIDATE flipped. The same contamination as (1), in the
   other half of the same expression, and easy to miss when fixing only (1).

3. **Only repetition 0 was gated, on both arms.** ``certification.py:1925-1929`` takes
   ``baseline_samples[0]`` / ``candidate_samples[0]``, so ``certification.passed`` reflects
   repetition 0 alone. A policy that holds an invariant on the first replay and breaks it on the
   second passed. ``stability_score`` cannot backstop this: ``_flip_rate``
   (``certification.py:1819-1829``) compares ``candidate_dispositions``, not invariants.

All three fail OPEN, which is why they went unnoticed. ``certification_passed`` is load-bearing
at three storage gates — ``sqlite/_policy.py:120`` (shadow), ``:212`` (activate), ``:265``
(initial alias bootstrap, the baseline path) — mirrored at ``postgres/_policy.py:220,363,419``.

Why this is a unit test of the acceptance rule
----------------------------------------------
The comparison is constructed directly rather than produced by a real certification run. That
is deliberate: the bug is in which side's evidence the ACCEPTANCE RULE reads, and driving a full
replay to manufacture a failing baseline would make the test slow, and would couple it to the
machinery that produces invariants rather than the rule that consumes them.

No model change was needed. ``StatefulPolicyComparison`` already carries ``baseline_samples``,
``candidate_samples`` and ``baseline_flip_rate`` — the consumer was reading the wrong fields.

The end-to-end consequence is pinned separately in ``tests/test_stateful_certification.py``,
where fixing (1) revealed that 4 of the 15 builtin motives violate their own
``retrieval_allocation`` invariant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memotron import Memotron, MemoryScope, Motive, ScopeKind
from memotron.certification import (
    EndStateGraphDiff,
    LifecycleInvariantResult,
    StatefulPolicyComparison,
    StatefulReplayOptions,
    StatefulReplaySample,
)

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="t0-14")
DIGEST_A = "a" * 64
DIGEST_B = "b" * 64


def _sample(policy_name: str, *, invariants_pass: bool) -> StatefulReplaySample:
    return StatefulReplaySample(
        policy_name=policy_name,
        repetition=0,
        model_identifier="test-model",
        temperature=0.0,
        candidate_dispositions={},
        graph_rows=(),
        protected_invariants=(
            LifecycleInvariantResult(
                name="supersession_preserves_history",
                passed=invariants_pass,
                detail="ok" if invariants_pass else "history was dropped during replay",
            ),
        ),
        run_uuids=(),
        graph_state_hash="0" * 64,
    )


def _comparison(
    *,
    baseline_ok: bool,
    candidate_ok: bool,
    baseline_samples: tuple[StatefulReplaySample, ...] | None = None,
    candidate_samples: tuple[StatefulReplaySample, ...] | None = None,
    baseline_flip_rate: float = 0.0,
    candidate_flip_rate: float = 0.0,
) -> StatefulPolicyComparison:
    """A comparison whose two sides can disagree about their invariants and their stability.

    ``protected_invariants`` mirrors the candidate, exactly as ``certification.py:1929`` builds
    it — the point of the test is that the baseline path must NOT consult that field.

    The flip rates are separately settable because ``stability_score`` is JOINT
    (``certification.py:1932``: ``1.0 - max(baseline_flip_rate, candidate_flip_rate)``). An
    earlier version of this fixture pinned ``stability_score=1.0`` unconditionally, which made
    the stability half of the acceptance rule untestable — the tests could not have caught a
    baseline being refused for the candidate's instability. Derive it here instead of pinning it.
    """
    candidate_sample = _sample("candidate", invariants_pass=candidate_ok)
    replay_flip_rate = max(baseline_flip_rate, candidate_flip_rate)
    return StatefulPolicyComparison(
        corpus_name="t0-14",
        corpus_digest=DIGEST_A,
        scope=SCOPE,
        baseline_motive_name="baseline-motive",
        candidate_motive_name="candidate-motive",
        baseline_contract_digest=DIGEST_A,
        candidate_contract_digest=DIGEST_B,
        options=StatefulReplayOptions(model_identifier="test-model", temperature=0.0),
        candidate_disposition_diffs=(),
        end_state_graph_diff=EndStateGraphDiff(),
        protected_invariants=candidate_sample.protected_invariants,
        baseline_flip_rate=baseline_flip_rate,
        candidate_flip_rate=candidate_flip_rate,
        replay_flip_rate=replay_flip_rate,
        stability_score=1.0 - replay_flip_rate,
        policy_delta=0.0,
        passed=candidate_ok,
        baseline_samples=(
            baseline_samples if baseline_samples is not None else (_sample("baseline", invariants_pass=baseline_ok),)
        ),
        candidate_samples=(candidate_samples if candidate_samples is not None else (candidate_sample,)),
    )


def _client(tmp_path: Path) -> Memotron:
    return Memotron(graph_path=tmp_path / "t0-14.sqlite")


def _motive(name: str) -> Motive:
    return Motive(name=name, scope=SCOPE, goal="t0-14 fixture goal")


async def _stage(client: Memotron, comparison: StatefulPolicyComparison, role: str) -> Any:
    return await client.stage_policy_contract(
        scope=SCOPE,
        alias="production",
        motive=_motive(comparison.baseline_motive_name if role == "baseline" else comparison.candidate_motive_name),
        certification=comparison,
        role=role,
        source_trace={"change": "t0-14"},
    )


# ----------------------------------------------------------------- the defect (T0-14)
@pytest.mark.asyncio
async def test_a_baseline_that_violated_its_own_invariant_is_not_certified(tmp_path: Path) -> None:
    """RED before the fix.

    The baseline's replay broke a protected invariant; the candidate's did not. Reading the
    candidate's invariants — as the acceptance rule did — certifies the baseline anyway.
    """
    client = _client(tmp_path)
    comparison = _comparison(baseline_ok=False, candidate_ok=True)

    contract = await _stage(client, comparison, "baseline")

    assert contract.certification_passed is False, (
        "a baseline whose own replay violated a protected invariant was staged as certified. "
        "The acceptance rule is reading the CANDIDATE's invariants (T0-14)."
    )


@pytest.mark.asyncio
async def test_a_clean_baseline_is_still_certified(tmp_path: Path) -> None:
    """The control, and it must pass BEFORE the fix as well as after.

    Without it, the assertion above could be satisfied by refusing every baseline — which
    would look like a fix and be a worse defect.
    """
    client = _client(tmp_path)
    contract = await _stage(client, _comparison(baseline_ok=True, candidate_ok=True), "baseline")
    assert contract.certification_passed is True, "a clean baseline must still certify"


@pytest.mark.asyncio
async def test_a_failing_candidate_still_does_not_certify(tmp_path: Path) -> None:
    """The candidate still requires ``comparison.passed`` — the full gate, not just invariants.

    Pinned because the candidate must satisfy the *complete* comparison, including material
    policy delta, which is what ``passed`` carries and what per-sample invariants do not. The
    all-repetitions check added for defect (3) is an ADDITIONAL requirement on this arm, never a
    replacement: a fix that swapped ``passed`` for per-sample invariants would pass every other
    test in this file and silently drop the policy-delta gate.
    """
    client = _client(tmp_path)
    contract = await _stage(client, _comparison(baseline_ok=True, candidate_ok=False), "candidate")
    assert contract.certification_passed is False, "a failing candidate must not certify"


@pytest.mark.asyncio
async def test_a_baseline_is_judged_independently_of_the_candidate(tmp_path: Path) -> None:
    """The inverse of the headline case, and the one that proves the sides are truly separate.

    A clean baseline beside a FAILING candidate must still certify: the candidate's problems
    are not the baseline's, and a fix that merely swapped which side is read would pass the
    first test and fail this one.
    """
    client = _client(tmp_path)
    contract = await _stage(client, _comparison(baseline_ok=True, candidate_ok=False), "baseline")
    assert contract.certification_passed is True, (
        "a clean baseline was refused because the CANDIDATE failed; the two sides must be judged on their own evidence"
    )


@pytest.mark.asyncio
async def test_a_baseline_with_no_samples_fails_closed(tmp_path: Path) -> None:
    """No evidence must not read as good evidence.

    ``all(())`` is True, so reading the baseline's own samples introduces a second way to
    certify vacuously if the tuple is empty. The real producer cannot emit that
    (``repetitions >= 2``, and ``certification.py:1925`` indexes ``baseline_samples[0]``), but
    ``stage_policy_contract`` accepts any deserialized comparison, and this flag gates the
    initial alias bootstrap at ``sqlite/_policy.py:265`` — the baseline path — so it must fail
    closed rather than open.
    """
    client = _client(tmp_path)
    comparison = _comparison(baseline_ok=True, candidate_ok=True, baseline_samples=())

    contract = await _stage(client, comparison, "baseline")

    assert contract.certification_passed is False, (
        "a baseline with zero replay samples was certified; `all(())` is True, so absent evidence must be rejected"
    )


# --------------------------------------------- the second contamination: joint stability
@pytest.mark.asyncio
async def test_a_stable_baseline_is_not_refused_for_the_candidates_instability(tmp_path: Path) -> None:
    """The same defect as the headline case, in the other half of the expression.

    ``stability_score`` is JOINT — ``certification.py:1932`` computes
    ``1.0 - max(baseline_flip_rate, candidate_flip_rate)``. Comparing the baseline against it
    means a perfectly reproducible baseline is refused whenever the CANDIDATE flips, which is
    exactly the cross-contamination this fix exists to remove. The baseline's own stability is
    ``1.0 - baseline_flip_rate``.

    This was invisible until the fixture stopped pinning ``stability_score=1.0``.
    """
    client = _client(tmp_path)
    comparison = _comparison(
        baseline_ok=True,
        candidate_ok=True,
        baseline_flip_rate=0.0,  # the baseline never flipped
        candidate_flip_rate=0.5,  # the candidate did
    )
    assert comparison.stability_score == 0.5, "fixture must model the JOINT score, or it proves nothing"
    assert comparison.options.minimum_stability_score > 0.5, "fixture requires a threshold the joint score fails"

    contract = await _stage(client, comparison, "baseline")

    assert contract.certification_passed is True, (
        "a baseline that never flipped was refused because the CANDIDATE was unstable; "
        "the baseline's own stability is 1.0 - baseline_flip_rate"
    )


@pytest.mark.asyncio
async def test_an_unstable_baseline_is_still_refused(tmp_path: Path) -> None:
    """The control for the test above — the stability gate must still bite on the right side.

    Without this, the fix could satisfy the previous assertion by dropping the baseline
    stability check altogether, which would be a worse defect than the one being fixed.
    """
    client = _client(tmp_path)
    comparison = _comparison(baseline_ok=True, candidate_ok=True, baseline_flip_rate=0.5, candidate_flip_rate=0.0)

    contract = await _stage(client, comparison, "baseline")

    assert contract.certification_passed is False, "an unstable baseline must not certify"


# ------------------------------------- the third contamination: only repetition 0 was gated
@pytest.mark.asyncio
async def test_a_candidate_that_breaks_an_invariant_after_repetition_zero_is_not_certified(
    tmp_path: Path,
) -> None:
    """``certification.passed`` reflects repetition 0 alone.

    ``certification.py:1926`` takes ``candidate = candidate_samples[0]`` and ``:1929`` reads only
    that sample's invariants, so repetitions 1..N-1 were never gated on either arm. A policy that
    holds an invariant on the first replay and breaks it on the second is not safe to activate,
    and ``stability_score`` cannot catch it — ``_flip_rate`` (``certification.py:1819-1829``)
    compares ``candidate_dispositions``, not invariants.

    This is the arm that actually goes live, so it is the one where the gap mattered most. Named
    as the secondary defect in T0-14's own register entry.
    """
    client = _client(tmp_path)
    clean_rep0 = _sample("candidate", invariants_pass=True)
    broken_rep1 = _sample("candidate", invariants_pass=False)
    comparison = _comparison(
        baseline_ok=True,
        candidate_ok=True,  # `passed` is True: it was computed from repetition 0
        candidate_samples=(clean_rep0, broken_rep1),
    )
    assert comparison.passed is True, "fixture must model a comparison that rep 0 alone declared passing"

    contract = await _stage(client, comparison, "candidate")

    assert contract.certification_passed is False, (
        "a candidate that violated a protected invariant in repetition 1 was certified because "
        "only repetition 0 was gated"
    )


@pytest.mark.asyncio
async def test_a_candidate_clean_in_every_repetition_is_still_certified(tmp_path: Path) -> None:
    """The control: tightening the candidate arm must not refuse a candidate that is genuinely clean."""
    client = _client(tmp_path)
    comparison = _comparison(
        baseline_ok=True,
        candidate_ok=True,
        candidate_samples=(_sample("candidate", invariants_pass=True), _sample("candidate", invariants_pass=True)),
    )

    contract = await _stage(client, comparison, "candidate")

    assert contract.certification_passed is True, "a candidate clean in every repetition must still certify"


@pytest.mark.asyncio
async def test_a_candidate_with_no_samples_fails_closed(tmp_path: Path) -> None:
    """``all(())`` is True on this arm too — absent candidate evidence must not read as good evidence."""
    client = _client(tmp_path)
    comparison = _comparison(baseline_ok=True, candidate_ok=True, candidate_samples=())

    contract = await _stage(client, comparison, "candidate")

    assert contract.certification_passed is False, "a candidate with zero replay samples was certified"
