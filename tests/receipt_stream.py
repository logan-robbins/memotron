"""Record what the suite receipts, so a refactor that changes it cannot land quietly.

Loaded from ``tests/conftest.py``, so it runs on every pytest invocation and costs
no second suite run. Writes ``.receipt_stream.tsv`` (gitignored);
``scripts/verify/receipt_golden.py`` compares that against the committed golden.

Why this exists
---------------
Every refactor in this branch so far has been a pure move, proved by
``scripts/verify/pure_move.py`` comparing compiled code objects. That proof covers
100% of function bodies and it is the reason the splits were safe.

The receipted-write extraction cannot have it. Collapsing ~30 hand-rolled
``hash / write / hash / emit`` brackets into one context manager changes bodies by
construction. Something has to replace the proof, and a passing test suite is not
enough on its own -- the ``_materialize_episode`` precedent is that its two failure
regimes are indistinguishable to every test in the suite, so a wrong decomposition
lands green.

So: pin the OBSERVABLE OUTPUT of the thing being refactored. For a receipt system
that is which receipts are emitted, in what order, of what type, with what result,
and whether the bracketed write actually mutated the graph. Those four are exactly
what a context-manager rewrite can plausibly break, and none of them is visible to
``pure_move``.

What is recorded, and what is deliberately not
----------------------------------------------
Per emit: ``decision_type``, ``decision_result``, and a *mutation* flag derived from
``graph_state_hash_before == graph_state_hash_after``.

NOT the payload digest. Payloads carry uuids and timestamps that differ every run by
design, so digesting them would report "changed" on every run for reasons that have
nothing to do with any refactor -- a golden that reddens unconditionally gets
blessed reflexively, which is worse than no golden.

The mutation flag is the interesting column. The bracket has two variants -- a
mutating write, and a recorder where before and after are the same hash -- and the
highest-risk failure of the extraction is silently converting one into the other.
Nothing in the codebase can state that invariant today; this file makes it a
committed fact.

Measured before relying on it (2026-08-28)
------------------------------------------
* the suite executes **34 of 34** bracket-emitting statements -- the corpus reaches
  everything the extraction will touch, so no bespoke seed corpus is needed;
* **3,539 emits across 506 tests, byte-identical over four runs**, two of them under
  coverage instrumentation;
* two of those four runs *differed* in whether ``_consolidation``'s
  claim-contention branch fired -- a genuine nondeterminism found in Phase A -- and
  the receipt stream was identical anyway. So the known flake does not reach this
  signal.
"""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

#: Written at session end, read by scripts/verify/receipt_golden.py. Gitignored --
#: the golden is the committed artifact, this is the observation.
STREAM_PATH = pathlib.Path(".receipt_stream.tsv")

_ROWS: list[tuple[str, int, str, str, str]] = []
_CURRENT: list[tuple[str, str, str]] = []
_INSTALLED = False


def install() -> None:
    """Wrap ``ReceiptLedger.emit``, the single choke point every receipt goes through."""
    global _INSTALLED
    if _INSTALLED:
        return
    from memotron.storage.receipts import ReceiptLedger

    original = ReceiptLedger.emit

    def emit(self: Any, run: Any, **fields: Any) -> Any:
        before = fields.get("graph_state_hash_before")
        after = fields.get("graph_state_hash_after")
        if before is None and after is None:
            mutated = "-"  # not a bracketed write at all
        elif before == after:
            mutated = "no"  # the recorder variant: receipted, but nothing changed
        else:
            mutated = "yes"
        _CURRENT.append((str(fields.get("decision_type", "")), str(fields.get("decision_result", "")), mutated))
        return original(self, run, **fields)

    ReceiptLedger.emit = emit  # type: ignore[method-assign]
    _INSTALLED = True


@pytest.hookimpl(trylast=True)
def record_setup() -> None:
    _CURRENT.clear()


def record_teardown(nodeid: str) -> None:
    for index, (decision_type, result, mutated) in enumerate(_CURRENT):
        _ROWS.append((nodeid, index, decision_type, result, mutated))
    _CURRENT.clear()


def write(clean: bool) -> None:
    """Emit the stream with a one-line header saying whether the run was clean.

    A run with failures reaches a different set of emit sites, so its stream is an
    observation of a broken tree rather than of the code under test. The header
    lets the comparison say so instead of reporting thousands of spurious diffs on
    top of a test failure that is already being reported by the tests lane.
    """
    lines = ["\t".join((n, str(i), t, r, m)) for n, i, t, r, m in _ROWS]
    header = f"# clean={'yes' if clean else 'no'} rows={len(lines)}"
    STREAM_PATH.write_text("\n".join([header, *lines]) + ("\n" if lines else ""))
