#!/usr/bin/env python3
"""Did the suite receipt exactly what it receipted before?

    uv run python scripts/verify/receipt_golden.py            # compare
    uv run python scripts/verify/receipt_golden.py --bless     # accept the change

Reads ``.receipt_stream.tsv``, written by ``tests/receipt_stream.py`` on every full
pytest run, and diffs it against ``tests/receipt_stream.golden.tsv``. No second
suite run: the observation is a side effect of the tests lane that already runs.

What it is for
--------------
``pure_move.py`` proves a refactor changed no function body, by comparing compiled
code objects. It has covered every commit in this branch and it is the reason the
splits were safe. The receipted-write extraction forfeits it -- collapsing ~30
hand-rolled ``hash / write / hash / emit`` brackets into one context manager
changes bodies by construction.

This is the replacement, and for that change it is the STRONGER guard. ``pure_move``
cannot see emit ordering, chain-cursor advancement, field defaulting, or the
decision to emit at all; those are the four things a context-manager rewrite most
plausibly breaks, and all four are here.

It is also weaker in one specific way, stated plainly because a guard whose limits
are not written down gets over-trusted: ``pure_move`` is static over 100% of
function bodies, and this only covers code the suite executes. That was measured
before relying on it -- the suite reaches **34 of 34** bracket-emitting statements,
so for the extraction specifically the gap is nil. It is not nil for anything else.

Reading a failure
-----------------
Rows are ``test::nodeid  index  decision_type  decision_result  mutated``. Three
shapes, in descending order of how alarming they are:

  a row whose ``mutated`` flipped   -- a bracketed write started or stopped changing
                                       the graph. This is the failure the extraction
                                       is most likely to cause and the least likely
                                       to be caught by anything else.
  a row that vanished / appeared    -- an emit was lost or added. Chain verification
                                       treats the receipt as the commit anchor, so a
                                       lost emit is a durability defect, not a log line.
  a whole test that vanished        -- usually a renamed or deleted test. Legitimate;
                                       bless it.
"""

from __future__ import annotations

import pathlib
import sys
from collections import Counter

STREAM = pathlib.Path(".receipt_stream.tsv")
GOLDEN = pathlib.Path("tests/receipt_stream.golden.tsv")


def _rows(path: pathlib.Path) -> tuple[dict[tuple[str, str], str], str]:
    """(test, index) -> the rest of the row, plus the header line."""
    header = ""
    out: dict[tuple[str, str], str] = {}
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            header = line
            continue
        if not line.strip():
            continue
        test, index, rest = line.split("\t", 2)
        out[(test, index)] = rest
    return out, header


def main() -> int:
    bless = "--bless" in sys.argv[1:]

    if not STREAM.exists():
        print("  no .receipt_stream.tsv -- run the suite first")
        return 2

    actual, header = _rows(STREAM)
    if "clean=no" in header:
        print("  SKIP  the last run had test failures, so its stream observes a broken")
        print("        tree. The tests lane is already reporting that; fix it first.")
        # 3, not 0 -- see the same note in storage_golden.py. Exit 0 here made
        # check.sh print PASS for a lane that had compared nothing.
        return 3

    if bless:
        # Growing is fine; SHRINKING has to be said out loud. `storage_golden.py` has
        # refused this since it was written and this file did not, which is exactly the
        # asymmetry that bit on 2026-09-10: a bless from a hermetic run silently dropped
        # 11 rows, of which only ONE belonged to the change being made -- the other ten
        # were Postgres-only tests the run never executed. The gate that refuses caught
        # it; the gate that merely reported did not.
        if GOLDEN.exists():
            previous, _ = _rows(GOLDEN)
            dropped = sorted(previous.keys() - actual.keys())
            if dropped and "--allow-shrink" not in sys.argv[1:]:
                print(f"  REFUSING TO BLESS: this would delete {len(dropped)} row(s) from the golden.")
                for test, index in dropped[:10]:
                    print(f"        {test} [{index}]")
                if len(dropped) > 10:
                    print(f"        ... and {len(dropped) - 10} more")
                print()
                print("  Almost always this means the stream came from a narrower run than the")
                print("  one the golden was built from -- check.sh's tests lane deselects the")
                print("  postgres and corpus markers, a bare `pytest` does not. Re-run the full")
                print("  suite and bless that:")
                print("      MEMOTRON_ALLOW_EPHEMERAL_KEK=1 uv run pytest -q")
                print("  Export ONLY the DSN -- sourcing .env also repoints the tests at the")
                print("  dogfood graph:")
                print(
                    '      export MEMOTRON_TEST_POSTGRES_DSN="$(set -a; . ./.env; set +a;'
                    ' printf %s "$MEMOTRON_TEST_POSTGRES_DSN")"'
                )
                print("  If the rows are genuinely gone (tests deleted or renamed), pass")
                print("  --allow-shrink and say why in the commit message.")
                return 1

        GOLDEN.write_text("\n".join(f"{t}\t{i}\t{rest}" for (t, i), rest in sorted(actual.items())) + "\n")
        print(f"  blessed {len(actual)} receipt rows")
        return 0

    if not GOLDEN.exists():
        print("  no golden yet -- record one with --bless")
        return 2

    golden, _ = _rows(GOLDEN)

    shared = golden.keys() & actual.keys()
    changed = sorted(k for k in shared if golden[k] != actual[k])
    added = sorted(actual.keys() - golden.keys())
    missing = sorted(golden.keys() - actual.keys())

    # A filtered run (-k, one file, a marker expression) legitimately observes FEWER
    # rows. That is indistinguishable from a lost emit by inspection, so missing rows
    # are reported and not failed on -- while CHANGED and ADDED rows, which filtering
    # cannot produce, are hard failures. This is what makes the gate immune to how
    # pytest was invoked, rather than trying to infer it from the options.
    if not changed and not added:
        counts = Counter(rest.rsplit("\t", 1)[-1] for rest in actual.values())
        tests = len({t for t, _ in actual})
        note = f"; {len(missing)} golden row(s) not observed (filtered run)" if missing else ""
        print(
            f"  RECEIPT STREAM: PASS -- {len(actual)} emits across {tests} tests "
            f"({counts['yes']} mutating, {counts['no']} recorder-only, "
            f"{counts['-']} unbracketed){note}"
        )
        return 0

    print(f"  RECEIPT STREAM CHANGED: {len(changed)} changed, {len(added)} new, {len(missing)} not observed")

    for k in changed[:20]:
        was = golden[k].split("\t")
        now = actual[k].split("\t")
        flag = "   <-- MUTATION FLAG FLIPPED" if was[-1] != now[-1] else ""
        print(f"    {k[0]} [{k[1]}]{flag}")
        print(f"      was  {' / '.join(was)}")
        print(f"      now  {' / '.join(now)}")
    if len(changed) > 20:
        print(f"    ... and {len(changed) - 20} more changed")

    for k in added[:15]:
        print(f"    new   {k[0]} [{k[1]}] {actual[k]}")
    if len(added) > 15:
        print(f"    new   ... and {len(added) - 15} more")

    print("\n  If every change is intended, re-bless:")
    print("    uv run python scripts/verify/receipt_golden.py --bless")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
