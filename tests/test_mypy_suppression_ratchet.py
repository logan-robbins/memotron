"""#135: a suppression that FIRES is not thereby justified.

The defect
----------
``scripts/verify/mypy_suppressions.py`` kept only a *set* of error codes per module, so its
only question was "does this code still fire anywhere?". On 2026-08-31
``memotron.mcp_server`` declared ``attr-defined``; it fired **27 times**, the gate reported
*"still earning its place"*, and the lane passed green. All 27 were real defects -- nine MCP
tools reading fields their models do not define, including ``run_due_dreams``, which raised
``TypeError`` on every call (#132).

The passing condition was satisfied by the very problem the gate should have surfaced.

Measured A/B, 2026-09-01, same tree and same injected defects (two `attr-defined` errors added
to ``local_platform``, whose suppression already hid 13):

    old gate  ->  "MYPY SUPPRESSIONS: PASS -- all 91 are still earning their place", exit 0
    new gate  ->  "local_platform: attr-defined  13 -> 15", exit 1

Why these tests do not run mypy
-------------------------------
The decision under test is "given these counts and this baseline, pass or fail?". Driving a
real mypy run to produce the counts would test mypy, take ~2s per case, temporarily rewrite
``pyproject.toml``, and could not produce the fall or missing-baseline cases at all. The counts
are the input, so they are supplied directly and ``main()``'s real decision path runs.

The end-to-end path -- that a real defect behind a real suppression is caught -- is covered by
the break test recorded in the commit message and reproducible with the recipe above.
"""

from __future__ import annotations

import collections
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "verify"))

import mypy_suppressions as ms

MODULE = "memotron.example"
CODE = "attr-defined"


@pytest.fixture
def gate(monkeypatch, tmp_path: Path):
    """Point the gate at a temp baseline and let each test choose the observed counts.

    Returns a callable: `gate(hidden=N, baseline=M | None) -> (exit_code, stdout)`.
    """

    def run(*, hidden: int, baseline: int | None, capsys) -> tuple[int, str]:
        monkeypatch.setattr(ms, "BASELINE", tmp_path / "baseline.tsv")
        monkeypatch.setattr(ms, "_declared", lambda: [(MODULE, {CODE})])
        monkeypatch.setattr(
            ms,
            "_errors_with_nothing_suppressed",
            lambda: {MODULE: collections.Counter({CODE: hidden})},
        )
        if baseline is not None:
            ms._write_baseline({(MODULE, CODE): baseline})
        code = ms.main([])
        return code, capsys.readouterr().out

    return run


# ------------------------------------------------------------------------ the defect (#135)
def test_a_rise_behind_an_existing_suppression_fails(gate, capsys) -> None:
    """The headline. The code still fires, so the OLD gate passed; the count rose, so this must not."""
    code, out = gate(hidden=15, baseline=13, capsys=capsys)

    assert code == 1
    assert "hide MORE errors than the baseline" in out
    assert "13 -> 15" in out


def test_a_brand_new_suppression_with_no_baseline_entry_fails(gate, capsys) -> None:
    """A pair absent from the baseline is treated as 0, so any firing count is a rise.

    That is the conservative reading and it is deliberate: it means adding a suppression
    requires a deliberate bless rather than arriving silently with whatever debt it hides.
    """
    code, out = gate(hidden=4, baseline=None, capsys=capsys)
    assert code == 1
    # No baseline file at all is its own, louder failure -- check that path explicitly.
    assert "is missing" in out

    code, out = gate(hidden=4, baseline=0, capsys=capsys)
    assert code == 1
    assert "no baseline entry -- new suppression" in out


# ----------------------------------------------------------------------------- the controls
def test_an_unchanged_count_passes(gate, capsys) -> None:
    """Must pass BEFORE and after the fix, or the gate is just "always red"."""
    code, out = gate(hidden=13, baseline=13, capsys=capsys)
    assert code == 0
    assert "none hides more than its baseline" in out


def test_a_fall_is_reported_and_does_not_fail(gate, capsys) -> None:
    """Progress must not be punished -- a gate that fails on improvement gets routed around.

    The fall is printed so the baseline gets tightened on purpose rather than drifting slack.
    """
    code, out = gate(hidden=6, baseline=13, capsys=capsys)

    assert code == 0, "paying down debt must not fail the gate"
    assert "now hide FEWER errors" in out
    assert "13 -> 6" in out


def test_a_dead_suppression_still_fails(gate, capsys) -> None:
    """The ORIGINAL half of the gate must survive the change.

    A code that fires nowhere is still dead weight and still a hole in the refactor gate.
    Pinned because the counting half rewrote the loop that computed it.
    """
    code, out = gate(hidden=0, baseline=0, capsys=capsys)

    assert code == 1
    assert "no longer fire" in out


# ------------------------------------------------------------------- fail closed, and I/O
def test_a_missing_baseline_is_not_a_pass(gate, capsys) -> None:
    """An absent baseline means the ratchet cannot run, which is not the same as clean."""
    code, out = gate(hidden=13, baseline=None, capsys=capsys)

    assert code == 1
    assert "cannot run" in out
    assert "--bless" in out


def test_bless_records_what_is_observed_and_then_passes(monkeypatch, tmp_path: Path, capsys) -> None:
    monkeypatch.setattr(ms, "BASELINE", tmp_path / "baseline.tsv")
    monkeypatch.setattr(ms, "_declared", lambda: [(MODULE, {CODE})])
    monkeypatch.setattr(ms, "_errors_with_nothing_suppressed", lambda: {MODULE: collections.Counter({CODE: 9})})

    assert ms.main(["--bless"]) == 0
    assert "blessed" in capsys.readouterr().out
    assert ms._read_baseline() == {(MODULE, CODE): 9}

    assert ms.main([]) == 0, "the blessed state must then be clean"


def test_the_baseline_round_trips_and_ignores_comments(monkeypatch, tmp_path: Path) -> None:
    """The file is hand-readable on purpose, so it must tolerate its own header."""
    monkeypatch.setattr(ms, "BASELINE", tmp_path / "baseline.tsv")
    written = {("a.b", "arg-type"): 3, ("c.d", "no-any-return"): 11}
    ms._write_baseline(written)

    text = (tmp_path / "baseline.tsv").read_text()
    assert text.startswith("#"), "header explains how to regenerate; do not drop it"
    assert ms._read_baseline() == written


def test_package_errors_reported_against_dunder_init_are_counted(monkeypatch) -> None:
    """A package's errors land on `pkg/__init__.py`, which normalises to `pkg.__init__`.

    Both spellings must be summed or every composed package reads as clean -- the same
    normalisation the dead-code check has always needed, now load-bearing for the counts too.
    """
    live = {
        "memotron.client": collections.Counter({CODE: 2}),
        "memotron.client.__init__": collections.Counter({CODE: 5}),
    }
    counts = ms._hidden_counts([("memotron.client", {CODE})], live)
    assert counts[("memotron.client", CODE)] == 7


def test_a_retired_suppression_is_reported_and_does_not_fail(monkeypatch, tmp_path: Path, capsys) -> None:
    """A baseline entry for a suppression that no longer exists is a NOTE, not a failure.

    Retiring a suppression is the outcome this gate exists to produce (#138 removed two,
    together hiding 43 errors), and a retired entry cannot cause a false pass -- it hides
    nothing. But an unreported orphan is how a baseline file rots into decoration, which is
    the failure mode the whole gate was written against, so it is printed.
    """
    monkeypatch.setattr(ms, "BASELINE", tmp_path / "baseline.tsv")
    ms._write_baseline({(MODULE, CODE): 30, ("memotron.still_here", "arg-type"): 2})
    monkeypatch.setattr(ms, "_declared", lambda: [("memotron.still_here", {"arg-type"})])
    monkeypatch.setattr(
        ms,
        "_errors_with_nothing_suppressed",
        lambda: {"memotron.still_here": collections.Counter({"arg-type": 2})},
    )

    code = ms.main([])
    out = capsys.readouterr().out

    assert code == 0, "retiring a suppression must not fail the gate"
    assert "no longer declared" in out
    assert f"{MODULE}: {CODE}  (was hiding 30)" in out
