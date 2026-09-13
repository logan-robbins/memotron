#!/usr/bin/env python3
"""Does each test still drive the storage backend the same way?

    uv run python scripts/verify/storage_golden.py            # compare
    uv run python scripts/verify/storage_golden.py --bless     # accept the change

Reads ``.storage_profile.tsv``, written by ``tests/storage_profile.py`` on every
pytest run, and diffs it against ``tests/storage_profile.golden.tsv``. No second
suite run — the observation is a side effect of the tests lane.

BLESS FROM A FULL RUN WITH THE DSN **SET**, NOT FROM ``check.sh``
-----------------------------------------------------------------
::

    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=memotron \\
        password=local-dev-only dbname=memotron"   # docker-compose.local.yml
    uv run pytest -q --strict-markers --strict-config     # NO -m selector
    uv run python scripts/verify/storage_golden.py --bless

No lane of ``check.sh`` observes the Postgres-only rows: its tests lane runs
``-m "not postgres and not corpus"``, and ``restore_observations`` (``check.sh:277``)
*discards* the Postgres lane's observations rather than merging them. So the input has to be
one full run with no ``-m`` selector.

**Set, because the DSN-set run observes strictly more.** Five tests only execute with a live
DSN and produce real profiles (``test_graph_state_hash_is_identical_across_engines`` and
friends, 6-292 calls each). Blessing from the richer run puts them in the golden; a later
hermetic run reports them as "**5 not observed**", which this tool *tolerates and prints*.
Blessing from the poorer run instead leaves them out, and a later DSN-set run reports them as
"**5 new**" -- which is a HARD FAILURE. Tolerated-when-absent beats failing-when-present.

Measured 2026-09-01 after the stub deletion below, same tree, same commit:

    DSN set    ->  0 changed, 0 new, 0 not observed          -- exact
    DSN unset  ->  PASS, 5 not observed (filtered run)       -- tolerated, and says so

.. note::

   **This advice has been wrong twice, in both directions, so here is why it changed.**
   Until 2026-09-01 the golden held 57 ``[postgres]`` rows that were pure artefacts of
   SKIPPED tests -- ``close:1`` stubs written by fixture teardown, zero real profiles. In
   that state a DSN-set run made all 57 "not observed" and ``--bless`` correctly refused, so
   DSN-**unset** was the only non-destructive input. Deleting the stubs (below) removed that
   constraint and made DSN-**set** correct. If you find older advice saying otherwise, it was
   describing the old golden, not this one.

Two ways to still get this wrong:

* **Bless after a hermetic-only run** (``-m "not postgres and not corpus"``) and the five
  Postgres-only rows read as deletions. ``--allow-shrink`` exists to stop that being silent;
  reaching for it to make a bless go through means you are about to delete real rows.
* **Bless after a run that partially completed.** A hung or interrupted run leaves a truncated
  observation file and every missing row reads as a deletion. A hung run and a slow one look
  identical from outside -- check the observation file's mtime before trusting it.

If a bless reports rows you cannot attribute to your own change, stop and re-run -- do not
accept them because the gate went green.

CLOSED 2026-09-01: the 57 signal-free ``[postgres]`` rows are gone (#139)
------------------------------------------------------------------------
They were the branch's own recurring shape -- a row that cannot fail. Every one was
``close:1``/``close:2`` with **zero** real profile, because that is what a SKIPPED test
writes: ``pytest_runtest_teardown`` fires for skips too, and the fixture's ``close`` got
counted. When those same tests genuinely RUN they drive the Postgres backend, which this
profiler does not instrument, so they produce **no row at all**. Present-but-meaningless or
absent, never informative.

Fixed at the source rather than by deleting rows that would come straight back:
``tests/storage_profile.mark_skipped`` drops a skipped test's row at write time, driven by
``pytest_runtest_logreport`` in ``tests/conftest.py``.

Two things that filter gets right and a naive one would not:

* It keys on the **skip**, not on "the profile is only ``close``". Six non-Postgres tests are
  legitimately close-only -- ``test_default_settings_build_one_sqlite_backend`` and friends
  build a backend and close it, and "this test only opens and closes" is a real fact about
  them. A close-only filter would have deleted all six.
* It excludes ``wasxfail``. pytest reports an xfailed test as ``skipped``, but an xfail DID
  run and its profile is real evidence -- the strict xfail in
  ``tests/test_stateful_certification.py`` drives a full certification replay. Filtering on
  ``report.skipped`` alone would have deleted that row too.

What it is for
--------------
``pure_move.py`` proves a function's bytecode is unchanged. That is a stronger
statement than this one — but it only holds for a genuinely pure move, and **every
split in this branch has needed at least one deliberate body edit**: a
class-qualified `ComposedClass.method` rewritten to the mixin that now owns it, in
`_entities`, `_registry` and `_prompts`. Each time I verified the rewrite was
object-identical by hand, at the REPL.

This is the mechanical version of that check. If a rewrite pointed at the wrong
sibling, or a moved body dropped a call, the storage profile changes and this says
so. It is the guard for exactly the edits `pure_move` stops covering.

Deliberately a multiset, not a sequence
---------------------------------------
The suite makes 155,896 storage calls across 147 methods. An ordered stream would be
a 156,000-line file that reddens on any reordering. The per-test profile is 643 rows
and is blind to order on purpose: swapping two independent reads is not a defect,
dropping one is.

Limits, stated so it is not over-trusted: it sees the SQLite backend only, it counts
contract methods rather than arguments (a call with wrong arguments still counts as
one call), and it covers only what the suite executes.
"""

from __future__ import annotations

import pathlib
import sys

PROFILE = pathlib.Path(".storage_profile.tsv")
GOLDEN = pathlib.Path("tests/storage_profile.golden.tsv")

#: Rows whose call counts genuinely vary run to run, so a difference here is NOISE and
#: must not be reported as a refactor defect.
#:
#: Measured 2026-08-29 during the client.py split. One full-suite run showed this row at
#: 283 calls against a golden of 277 -- every one of its six counters up by exactly 1
#: (active_epoch_for_scope, context_visible_relationships, epoch_ancestry,
#: record_dream_decision, relationships, reveal), which is one extra contention retry,
#: not a misrouted call. Then: 3 consecutive isolated runs at 277, 2 further full-suite
#: runs at 283 and 277, and a 3-way diff of all 695 rows across three consecutive full
#: suites found ZERO other varying rows. So the flake is real, confined to this one
#: concurrency test, and roughly 1 run in 5.
#:
#: It is listed rather than dropped because the row still has value: a change in WHICH
#: methods it calls is a defect even though the COUNTS drift. Only count drift within
#: the same method set is forgiven -- a new or missing method name still fails.
KNOWN_COUNT_DRIFT = frozenset(
    {
        "tests/test_dream_concurrency.py::test_concurrent_consolidation_synthesizes_exactly_one_rollup",
    }
)

#: ...and only by this much per method. The first cut of this allowance forgave drift of
#: ANY magnitude, which an independent review caught: a +7 swing passed, when the
#: measurement only ever justified +1 (one extra contention retry). An allowance wider
#: than its evidence is how a gate stops noticing. If a real run ever exceeds this,
#: re-measure the noise floor and raise it deliberately -- do not widen it to make a red
#: run green.
MAX_DRIFT_PER_METHOD = 1


def _rows(path: pathlib.Path) -> tuple[dict[str, dict[str, int]], str]:
    header = ""
    out: dict[str, dict[str, int]] = {}
    for line in path.read_text().splitlines():
        if line.startswith("#"):
            header = line
            continue
        if not line.strip():
            continue
        nodeid, _total, pairs = line.split("\t", 2)
        out[nodeid] = {m: int(c) for m, _, c in (p.partition(":") for p in pairs.split(",")) if m}
    return out, header


def main() -> int:
    bless = "--bless" in sys.argv[1:]

    if not PROFILE.exists():
        print("  no .storage_profile.tsv -- run the suite first")
        return 2
    actual, header = _rows(PROFILE)
    if "clean=no" in header:
        print("  SKIP  the last run had failures, so its profile observes a broken tree.")
        # 3, not 0: check.sh's run_lane records exit 0 as PASS, so returning 0 here
        # made a lane that compared NOTHING indistinguishable from one that compared
        # everything and found it good. Two agents reported `storage PASS` off that
        # table on 2026-09-01/02 while this branch had fired.
        return 3

    if bless:
        # Blessing from a NARROWER run silently deletes rows, and nothing downstream
        # notices: `missing` is reported-not-failed on purpose, so the shrunken golden
        # then passes forever over the tests it stopped covering.
        #
        # This is not hypothetical. On 2026-08-30 a bless ran against the profile from
        # `scripts/check.sh`, whose tests lane passes `-m "not postgres and not corpus"`,
        # while the golden had been blessed from a bare `pytest`. The golden went
        # 703 -> 656 rows and the run still said PASS. 53 rows of coverage, gone quietly.
        #
        # So: growing is fine, shrinking needs saying out loud.
        if GOLDEN.exists():
            previous, _ = _rows(GOLDEN)
            dropped = sorted(previous.keys() - actual.keys())
            if dropped and "--allow-shrink" not in sys.argv[1:]:
                print(f"  REFUSING TO BLESS: this would delete {len(dropped)} row(s) from the golden.")
                for row in dropped[:10]:
                    print(f"        {row}")
                if len(dropped) > 10:
                    print(f"        ... and {len(dropped) - 10} more")
                print()
                print("  Almost always this means the profile came from a narrower run than the")
                print("  one the golden was built from -- check.sh's tests lane deselects the")
                print("  postgres and corpus markers, a bare `pytest` does not. Re-run the full")
                print("  suite and bless that:")
                print("      MEMOTRON_ALLOW_EPHEMERAL_KEK=1 uv run pytest -q")
                print("  If the rows are genuinely gone (tests deleted or renamed), pass")
                print("  --allow-shrink and say why in the commit message.")
                return 1

        # Known-flaky rows keep the counts the golden already has. The CHECK path
        # forgives their count drift (KNOWN_COUNT_DRIFT), so whichever sample a bless
        # happened to catch is arbitrary -- but writing it anyway churns the golden, and
        # that churn lands as an unexplainable line in whatever commit blessed next.
        #
        # Observed 2026-09-02 landing T0-3: a one-token storage fix produced a golden diff
        # containing this row at 283 where it had been 277, on a run whose own check said
        # "0 changed". Three re-runs on identical code gave 277/283/277. Nothing was wrong;
        # the reviewer still had to prove that, which is the cost.
        #
        # Only counts are preserved, and only when the METHOD SET is identical. A row that
        # gained or lost a method name is a real change, so it is written through -- the
        # same line the check path draws.
        preserved: list[str] = []
        text = PROFILE.read_text()
        if GOLDEN.exists():
            prior, _ = _rows(GOLDEN)
            golden_lines = {
                line.split("\t", 1)[0]: line
                for line in GOLDEN.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
            out: list[str] = []
            for line in text.splitlines():
                if line.startswith("#") or not line.strip():
                    out.append(line)
                    continue
                nodeid = line.split("\t", 1)[0]
                if (
                    nodeid in KNOWN_COUNT_DRIFT
                    and nodeid in prior
                    and nodeid in actual
                    and prior[nodeid].keys() == actual[nodeid].keys()
                    and prior[nodeid] != actual[nodeid]
                ):
                    out.append(golden_lines[nodeid])
                    preserved.append(nodeid)
                    continue
                out.append(line)
            text = "\n".join(out) + "\n"

        GOLDEN.write_text(text)
        total = sum(sum(v.values()) for v in actual.values())
        print(f"  blessed {len(actual)} test profiles ({total} storage calls)")
        for nodeid in preserved:
            print(f"    kept prior counts for known-flaky row: {nodeid}")
        return 0

    if not GOLDEN.exists():
        print("  no golden yet -- record one with --bless")
        return 2

    golden, _ = _rows(GOLDEN)

    # Zero observations against a non-empty golden is the RECORDER not running, not a
    # filtered run -- and reporting PASS on it would be the "SKIPPED read as passing"
    # failure this repo has already been bitten by twice. Found by a control test that
    # accidentally ran against a stale empty profile and got a green tick.
    if golden and not actual:
        print(f"  FAIL  the profile is empty but the golden has {len(golden)} tests.")
        print("        tests/storage_profile.py did not run. That is not a pass.")
        return 1
    shared = golden.keys() & actual.keys()
    changed = sorted(k for k in shared if golden[k] != actual[k])

    # Forgive count drift on the known-flaky rows, but ONLY when the set of methods is
    # identical. A row that gained or lost a method name is a real change even there.
    drifted = [
        k
        for k in changed
        if k in KNOWN_COUNT_DRIFT
        and golden[k].keys() == actual[k].keys()
        and all(abs(actual[k][m] - golden[k][m]) <= MAX_DRIFT_PER_METHOD for m in golden[k])
    ]
    changed = [k for k in changed if k not in drifted]
    added = sorted(actual.keys() - golden.keys())
    missing = sorted(golden.keys() - actual.keys())

    # Missing rows are reported, not failed: a filtered run legitimately observes
    # fewer tests, and that is indistinguishable from a deleted test by inspection.
    # CHANGED and ADDED cannot be produced by filtering, so those are hard failures.
    if not changed and not added:
        calls = sum(sum(v.values()) for v in actual.values())
        note = f"; {len(missing)} not observed (filtered run)" if missing else ""
        drift = f"; {len(drifted)} known count drift" if drifted else ""
        print(f"  STORAGE PROFILE: PASS -- {len(actual)} tests, {calls} calls{note}{drift}")
        for k in drifted:
            print(f"    known-flaky (counts only, method set unchanged): {k}")
        return 0

    print(f"  STORAGE PROFILE CHANGED: {len(changed)} changed, {len(added)} new, {len(missing)} not observed")
    for k in changed[:15]:
        was, now = golden[k], actual[k]
        delta = {
            m: (was.get(m, 0), now.get(m, 0)) for m in sorted(set(was) | set(now)) if was.get(m, 0) != now.get(m, 0)
        }
        print(f"    {k}")
        for m, (b, a) in delta.items():
            arrow = "LOST" if a == 0 else ("NEW" if b == 0 else "    ")
            print(f"      {arrow} {m}: {b} -> {a}")
    if len(changed) > 15:
        print(f"    ... and {len(changed) - 15} more changed")
    for k in added[:10]:
        print(f"    new   {k} ({sum(actual[k].values())} calls)")

    print("\n  If every change is intended, re-bless:")
    print("    uv run python scripts/verify/storage_golden.py --bless")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
