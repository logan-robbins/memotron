"""#141: `--postgres-covered` must answer from the REPORT, not from what the caller ran.

The defect
----------
``scripts/check.sh`` tracked Postgres coverage in a shell variable, ``POSTGRES_COVERED``, set
only when the Postgres lane ran **in that invocation**. The ``diff-cov`` lane then used it to
decide whether to exclude ``storage/postgres/**``.

So ``bash scripts/check.sh diff-cov postgres`` -- Postgres named, tests lane absent -- left the
flag at 0 and applied the exclusion **even when coverage.xml on disk already held real Postgres
coverage from an earlier full run**. The direction is under-gating: a Postgres-only diff goes
un-diff-covered, `diff-cover` reports success, and nothing says the exclusion was applied on
stale reasoning. It can never produce a false failure, which is exactly why it could sit there.

Why these tests construct reports by hand
-----------------------------------------
The question under test is "given this report, what is the answer?", so the report is the input
and building one by hand is the point. Driving a real Postgres run to produce one would test the
Postgres lane instead, take minutes, and could not produce the below-floor or missing-module
cases at all.

The floor is not duplicated here -- these import ``POSTGRES_FLOOR`` from the module under test.
Hardcoding 85.8 would mean the tests keep passing after someone changes the floor, which is the
"a check calibrated against the thing it checks" shape this repo keeps finding.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "verify"))

from coverage_floors import POSTGRES_FLOOR

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify" / "coverage_floors.py"
POSTGRES_MODULE = "src/memotron/storage/postgres/__init__.py"


def _report(tmp_path: Path, *, percent: float | None, name: str = "coverage.json") -> Path:
    """A minimal coverage.json holding one storage/postgres file at `percent`.

    `percent is None` omits the module entirely, which is what a report from a tree that
    never imported it looks like.
    """
    # `measured()` derives the percentage as covered_lines / num_statements and IGNORES
    # `percent_covered`. An earlier version of this helper used 100 statements and rounded,
    # so asking for 85.7% produced 86 lines and the fixture silently tested 86.0% instead --
    # the parametrised boundary case passed for the wrong reason until it didn't. 10_000
    # statements makes every percentage in these tests exactly representable.
    statements = 10_000
    files: dict[str, object] = {}
    if percent is not None:
        covered = round(percent * statements / 100)
        assert covered * 100 / statements == pytest.approx(percent), (
            f"fixture cannot represent {percent}% exactly with {statements} statements"
        )
        files[POSTGRES_MODULE] = {
            "summary": {
                "covered_lines": covered,
                "num_statements": statements,
                "percent_covered": percent,
                "missing_lines": statements - covered,
                "excluded_lines": 0,
            }
        }
    report = tmp_path / name
    report.write_text(json.dumps({"files": files, "totals": {"percent_covered": percent or 0.0}}))
    # The script rejects a report older than its newest input, so make this one unambiguously
    # newer rather than racing the checkout's mtimes.
    #
    # That input set is `src/` AND `tests/` as of 2026-09-07 -- it was src/ only, which let a
    # test-only edit slip past the staleness guard, and a test-only edit is exactly when
    # coverage moves without src/ changing. This helper has to track the script's definition or
    # it pins a contract the script no longer has: with `tests/` newer than `src/ + 60`, every
    # case here returned STALE instead of its real verdict.
    newest = max(
        (p.stat().st_mtime for p in (*Path("src").rglob("*.py"), *Path("tests").glob("test_*.py"))),
        default=0.0,
    )
    os.utime(report, (newest + 60, newest + 60))
    return report


def _ask(report: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--postgres-covered", str(report)],
        capture_output=True,
        text=True,
        check=False,
    )


# --------------------------------------------------------------------- the answer is YES
def test_a_report_at_the_floor_answers_yes(tmp_path: Path) -> None:
    """At the floor exactly -- the boundary is inclusive, matching `cov-floor`'s own `<` test."""
    result = _ask(_report(tmp_path, percent=POSTGRES_FLOOR))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "postgres-covered: YES" in result.stdout


def test_a_report_well_above_the_floor_answers_yes(tmp_path: Path) -> None:
    result = _ask(_report(tmp_path, percent=96.5))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "postgres-covered: YES" in result.stdout


# ---------------------------------------------------------------------- the answer is NO
def test_a_hermetic_only_report_answers_no(tmp_path: Path) -> None:
    """The case that matters: Postgres tests deselected, so the module is present but thin.

    22.6% is the value actually observed on 2026-08-31 from a run whose Postgres tests
    errored at setup -- not an invented number.
    """
    result = _ask(_report(tmp_path, percent=22.6))
    assert result.returncode == 1
    assert "postgres-covered: NO" in result.stdout
    assert "below floor" in result.stdout


def test_a_report_without_the_module_answers_no(tmp_path: Path) -> None:
    result = _ask(_report(tmp_path, percent=None))
    assert result.returncode == 1
    assert "no memotron.storage.postgres module" in result.stdout


def test_a_missing_report_answers_no_rather_than_erroring(tmp_path: Path) -> None:
    """Fails CLOSED, and with exit 1 rather than the human-facing exit 2.

    The caller is a shell `if`, so any non-zero would read as "not covered" -- but exit 2 is
    this script's "you invoked me wrong" code, and conflating the two would hide a real
    misconfiguration behind a routine-looking answer.
    """
    result = _ask(tmp_path / "does-not-exist.json")
    assert result.returncode == 1
    assert "postgres-covered: NO" in result.stdout
    assert "does not exist" in result.stdout


def test_a_stale_report_is_rejected_rather_than_answered(tmp_path: Path) -> None:
    """Staleness must win over the coverage question.

    A report predating the newest source file cannot know about modules added since. Answering
    YES from one would reinstate the original defect in a new form -- an answer derived from
    something that is no longer the tree. Exit 2, not 1: this is "re-run", not "no".
    """
    report = _report(tmp_path, percent=96.5, name="stale.json")
    os.utime(report, (0, 0))
    result = _ask(report)
    assert result.returncode == 2
    assert "STALE" in result.stdout


# ------------------------------------------------------------------------- the regression
def test_the_answer_does_not_depend_on_how_the_caller_was_invoked(tmp_path: Path) -> None:
    """The whole point of #141, stated as an assertion.

    The same report must produce the same answer regardless of environment. The old shell flag
    could not satisfy this: it was 0 or 1 depending on which lanes the caller named, with the
    report untouched.
    """
    report = _report(tmp_path, percent=96.5)
    first = _ask(report)

    env_polluted = dict(os.environ, MEMOTRON_TEST_POSTGRES_DSN="")
    second = subprocess.run(
        [sys.executable, str(SCRIPT), "--postgres-covered", str(report)],
        capture_output=True,
        text=True,
        check=False,
        env=env_polluted,
    )

    assert first.returncode == second.returncode == 0
    assert first.stdout == second.stdout


@pytest.mark.parametrize("percent", [0.0, POSTGRES_FLOOR - 0.1, POSTGRES_FLOOR, 100.0])
def test_the_boundary_is_the_floor_and_nothing_else(tmp_path: Path, percent: float) -> None:
    """Table-driven, so the `<` vs `<=` boundary cannot drift unnoticed."""
    expected = 0 if percent >= POSTGRES_FLOOR else 1
    assert _ask(_report(tmp_path, percent=percent)).returncode == expected
