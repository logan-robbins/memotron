#!/usr/bin/env bash
# Every gate, one command, run locally:
#
#     bash scripts/check.sh            # every lane
#     bash scripts/check.sh lint tests # named lanes
#     bash scripts/check.sh --list     # lane names
#     bash scripts/check.sh --fix      # ruff --fix + ruff format, then the lanes
#
# There is no CI pipeline for this repository; this script IS the gate. Lanes that
# need infrastructure this machine may not have (postgres via
# MEMOTRON_TEST_POSTGRES_DSN, corpus via MEMOTRON_PUBLIC_CORPUS_DIR) report a
# SKIP with the reason rather than a silent pass. Order matters in one place:
# `postgres` must precede `cov-floor` and `diff-cov`, because those read the
# coverage file the earlier lanes write.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 2

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
note() { printf '  %s\n' "$*"; }

FAIL=0
declare -a SUMMARY=()

# Order matters in one place: `postgres` must precede `cov-floor` and `diff-cov`,
# which read the coverage report it contributes to. This list is the DEFAULT set and
# the --list output; execution order is the physical order of the blocks below.
ALL_LANES=(lint format types suppressions scripts mixin-dag coupling tests receipts storage postgres cov-floor parity-cov diff-cov corpus)
FIX=0
declare -a WANT=()

for arg in "$@"; do
  case "$arg" in
    --fix)  FIX=1 ;;
    --list) printf '%s\n' "${ALL_LANES[@]}"; exit 0 ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    -*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *)  WANT+=("$arg") ;;
  esac
done
[ ${#WANT[@]} -eq 0 ] && WANT=("${ALL_LANES[@]}")

wants() { for w in "${WANT[@]}"; do [ "$w" = "$1" ] && return 0; done; return 1; }

# Run a lane, time it, record the verdict. Never aborts the script: the point is
# to report every lane's state in one pass, not to stop at the first red one.
#
# EXIT 3 MEANS "SKIPPED -- NOTHING WAS EVALUATED", and it exists because this
# function used to report that state as PASS.
#
# Both golden lanes refuse to compare when the recorded profile came from a failing
# suite ("clean=no"), which is correct -- a profile taken off a broken tree describes
# a broken tree. But they signalled that refusal with `return 0`, so the summary said
# PASS for a lane that had compared nothing. Observed 2026-09-01/02: two separate
# agents reported `storage PASS` and `receipts PASS` off this table while both lanes
# had actually skipped, and the second time it masked a genuinely broken tests lane.
# Neither agent misread anything; the table was wrong.
#
# That is this repo's recurring defect -- a check whose passing condition is satisfied
# by the very problem it should surface -- living in the runner that reports all the
# others. A lane that evaluated nothing must never be indistinguishable from a lane
# that evaluated everything and found it good.
run_lane() {
  local name="$1"; shift
  local start rc elapsed
  start=$(date +%s)
  "$@"
  rc=$?
  elapsed=$(( $(date +%s) - start ))
  if [ "$rc" -eq 0 ]; then
    SUMMARY+=("${name}|PASS|${elapsed}s")
  elif [ "$rc" -eq 3 ]; then
    # Not a failure: the lane declined to judge and said why on stdout. Not FAIL=1,
    # because the underlying cause is already being reported by whichever lane broke.
    SUMMARY+=("${name}|SKIPPED (nothing evaluated)|${elapsed}s")
  else
    SUMMARY+=("${name}|FAIL (exit $rc)|${elapsed}s"); FAIL=1
  fi
  return 0
}

skip_lane() { SUMMARY+=("$1|SKIPPED ($2)|-"); note "SKIPPED: $2"; }

say "memotron check"
note "repo:   $(pwd)"
note "commit: $(git rev-parse --short HEAD 2>/dev/null || echo '(not a git checkout)')"
note "lanes:  ${WANT[*]}"

# ---------------------------------------------------------------- dependencies
if ! command -v uv >/dev/null 2>&1; then
  echo "  uv is not on PATH — cannot run anything"; exit 2
fi
say "installing dependencies"
# --group lint explicitly: uv does not install non-default groups otherwise, and
# ruff/mypy/diff-cover all live there.
if ! uv sync --extra postgres --group dev --group lint --quiet 2>&1 | tail -3; then
  echo "  uv sync failed"; exit 2
fi

# ---------------------------------------------------------------- optional autofix
if [ "$FIX" -eq 1 ]; then
  say "autofix"
  uv run --no-sync ruff check . --fix
  uv run --no-sync ruff format .
fi

# ---------------------------------------------------------------- lane: lint
if wants lint; then
  say "lane: lint (ruff)"
  run_lane lint uv run --no-sync ruff check .
fi

# ---------------------------------------------------------------- lane: format
if wants format; then
  say "lane: format (ruff format --check)"
  # Live since 2026-08-29. It was inert for the whole module split -- a formatter run
  # mid-refactor collides with every moved block -- and the estimate it carried while
  # inert (138 files / 7,434 lines) had aged badly by the time it ran: 243 files.
  # An estimate parked in a comment does not update itself.
  run_lane format uv run --no-sync ruff format --check .
fi

# ---------------------------------------------------------------- lane: types
if wants types; then
  say "lane: types (mypy)"
  run_lane types uv run --no-sync mypy
fi

# ---------------------------------------------------------------- lane: suppressions
# Costs a second mypy run (~2s), and buys the one thing mypy cannot tell you itself:
# `warn_unused_ignores` covers inline `# type: ignore` and has NO equivalent for
# `disable_error_code` in [[tool.mypy.overrides]]. So a suppression outlives the debt it
# was written for, silently, in the module most likely to be refactored next.
# First time anyone looked: 17 of 109 were already dead.
if wants suppressions; then
  say "lane: mypy suppression ratchet"
  run_lane suppressions uv run --no-sync python scripts/verify/mypy_suppressions.py
fi

# ---------------------------------------------------------------- lane: tests
if wants tests; then
  say "lane: tests (hermetic)"
  # --strict-markers is passed HERE, on the command line, not left to addopts.
  # Measured on pytest 9.0.3: the same flag in [tool.pytest.ini_options] addopts
  # does NOT enforce -- an unregistered mark still only warns. See the comment on
  # addopts in pyproject.toml.
  run_lane tests env MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
    uv run --no-sync pytest -q --strict-markers --strict-config \
      -m "not postgres and not corpus" \
      --cov --cov-report=json:coverage.json --cov-report=xml --cov-report=term-missing:skip-covered
fi

# ---------------------------------------------------------------- lane: scripts
# Static and fast. Added 2026-08-31, after asking "are our scripts out of date after the
# refactor?" and finding that FIVE tools had been silently killed by changes to the tree they
# measure -- coupling_report.py (dead since client.py became a package, two hardcoded paths),
# sweep_admin.py (same, so a live-lane sweep could not run), probe_kek.py, probe_storage_wiring.py,
# and 13 file.py:NNN citations across six probes.
#
# It is a LANE rather than a milestone script on purpose. skill_freshness.py is not a lane, and
# the skill it guards went stale anyway; coupling_report.py is not a gate, and its own docstring
# calls it the instrument the pass is steered by. A checker nothing runs is a checker that rots.
if wants scripts; then
  say "lane: script freshness"
  run_lane scripts uv run --no-sync python scripts/verify/script_freshness.py
  # Same lane, same rot class, different artefact: script_freshness asks whether the SCRIPTS
  # still describe the tree; doc_symbols asks whether the DOCS do. Added 2026-08-31 after an
  # independent reviewer found README.md's own quickstart could not run -- it imported a class
  # that does not exist, and passed a keyword DreamJob does not accept.
  run_lane doc-symbols uv run --no-sync python scripts/verify/doc_symbols.py
  # And the third artefact of the same class: what left the PUBLIC API since before this
  # effort. api_surface.py's golden was blessed ON this branch, so it can only see drift from
  # the tip; this one is pinned to the merge-base SHA and is deliberately not blessable.
  run_lane api-cutover uv run --no-sync python scripts/verify/api_since_cutover.py
fi

# ---------------------------------------------------------------- lane: mixin-dag
# Static and fast (~0.1s). A cycle in the mixin call graph is a structural fact and does
# not need the tests to have passed first.
#
# This comment used to read "so it runs before the suite", which was false: the block sits
# below the tests lane and always has, even though ALL_LANES lists mixin-dag fifth. Caught
# by an independent verifier 2026-08-31 while checking that the postgres reorder was inert.
# Left in its current position rather than moved -- the ordering claim was the error, not
# the ordering, and only postgres/cov-floor/diff-cov have a real dependency.
#
# layer.py checks a module's DEFINITION graph; this checks its CALL graph. dreaming/
# passed the first and failed the second for the whole of the split.
if wants mixin-dag; then
  say "lane: mixin call-graph DAG"
  run_lane mixin-dag uv run --no-sync python scripts/verify/mixin_dag.py
fi

# ---------------------------------------------------------------- lane: coupling
# The structural half of what mixin-dag does for call graphs: import cycles and
# upward tier edges across the whole of src/, not just the three mixin packages.
#
# It was a REPORT until 2026-09-01 -- `main()` always returned 0 and nothing ran it, so
# it sat dead for the entire pass it was supposed to steer (both of its hardcoded paths
# had rotted). It is a gate now, and only the structural metrics are gated; the
# call-site counts it used to lead with are printed and cannot fail a build, because a
# call count cannot tell heavy use of a right abstraction from a leak through a wrong one.
#
# Static and fast (~1s), no coverage.json needed for the gated metrics.
if wants coupling; then
  say "lane: coupling (import cycles, tier violations)"
  run_lane coupling uv run --no-sync python scripts/verify/coupling_report.py
fi

# ---------------------------------------------------------------- lane: receipts
# Free: the stream is a side effect of the tests lane above (tests/receipt_stream.py
# is loaded from conftest), so this compares a file rather than running anything.
# It is what replaces pure_move.py for the receipted-write extraction, which changes
# function bodies by construction and so cannot have a bytecode proof.
if wants receipts; then
  say "lane: receipt stream golden"
  if [ -f .receipt_stream.tsv ]; then
    run_lane receipts uv run --no-sync python scripts/verify/receipt_golden.py
  else
    skip_lane receipts "no .receipt_stream.tsv \u2014 run the tests lane first"
  fi
fi

# ---------------------------------------------------------------- lane: storage
# Free: the profile is a side effect of the tests lane. Guards the edits pure_move
# stops covering -- every split so far needed at least one class-qualified rewrite,
# and this is the mechanical check that the rewrite pointed at the right sibling.
if wants storage; then
  say "lane: storage-call profile"
  if [ -f .storage_profile.tsv ]; then
    run_lane storage uv run --no-sync python scripts/verify/storage_golden.py
  else
    skip_lane storage "no .storage_profile.tsv - run the tests lane first"
  fi
fi

# ---------------------------------------------------------------- lane: postgres
# BEFORE cov-floor and diff-cov, deliberately: both read coverage.json/xml, and
# until 2026-08-31 this lane ran AFTER them and contributed no coverage at all.
# The effect was silent and total -- with the lane green at 76 passed, the report
# still recorded storage/postgres at 0/2211, so `coverage_floors.py` printed
# "SKIPPED ... unmeasured -- the lane needs MEMOTRON_TEST_POSTGRES_DSN" with
# the DSN plainly set, and its MEASURED branch had never once executed. The
# 86.8% in POSTGRES_FLOOR's comment came from a hand-run, which is precisely the
# kind of number this repo has learned not to trust. Re-derived here: 1919/2211.
# The receipt-stream and storage-profile observations are preserved across this ENTIRE lane,
# in every branch. tests/conftest.py installs both writers unconditionally, so a 76-test
# Postgres-only run rewrites them with a partial picture.
#
# The first version of this guard wrapped only the standalone branch, reasoning that full runs
# are safe because receipts/storage execute above postgres. That was wrong by one invocation:
# postgres then poisons the file for the NEXT run, and it surfaces as a red receipts lane on a
# clean tree with no local change to explain it. Observed 2026-08-31 while landing T1-1 -- ten
# phantom "new" rows, every one from a Postgres-only test.
declare -a OBSERVED_ARTIFACTS=(.receipt_stream.tsv .storage_profile.tsv)
preserve_observations() { for f in "${OBSERVED_ARTIFACTS[@]}"; do [ -f "$f" ] && cp "$f" "$f.prepg"; done; return 0; }
restore_observations() {
  for f in "${OBSERVED_ARTIFACTS[@]}"; do
    if [ -f "$f.prepg" ]; then mv "$f.prepg" "$f"; else rm -f "$f"; fi
  done
}
if wants postgres; then
  say "lane: Postgres-backed tests"
  preserve_observations
  if [ -z "${MEMOTRON_TEST_POSTGRES_DSN:-}" ]; then
    note "A green run WITHOUT this lane says nothing about the Postgres backend."
    note "That gap is how T0-4 and T0-5 survived a passing suite."
    skip_lane postgres "MEMOTRON_TEST_POSTGRES_DSN unset"
  else
    # `-m postgres` selects exactly the 76 marked tests. The old `-k "postgres or
    # storage or scoped or wiring"` name match silently dropped files as tests
    # were renamed.
    #
    # --cov-append only when the tests lane ran in THIS invocation: appending to
    # a stale .coverage from an earlier run, or to none at all, would report every
    # non-Postgres module at whatever that file happens to hold and hand cov-floor
    # a fabricated picture. `check.sh postgres` on its own therefore still runs the
    # tests, just without touching the coverage report.
    if wants tests && [ -f coverage.json ]; then
      run_lane postgres env MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
        uv run --no-sync pytest -q --strict-markers -m postgres \
          --cov --cov-append --cov-report=
      # Regenerate both reports from the combined data. Without this the appended
      # lines exist in .coverage and in neither report, which is the original bug
      # wearing a different hat.
      if uv run --no-sync coverage json -o coverage.json -q \
         && uv run --no-sync coverage xml -q; then
        note "coverage.json/xml regenerated including the Postgres lane"
      else
        note "WARNING: coverage report regeneration failed; downstream lanes see hermetic-only data"
      fi
    else
      note "tests lane not in this run — Postgres coverage will not reach the reports"
      run_lane postgres env MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
        uv run --no-sync pytest -q --strict-markers -m postgres
    fi
  fi
fi

if wants postgres; then
  restore_observations
  note "receipt/storage observations restored to the hermetic run's output — the Postgres"
  note "lane must not decide what the receipts and storage gates read, now or next run."
fi

# ---------------------------------------------------------------- lane: cov-floor
if wants cov-floor; then
  say "lane: coverage floors"
  if [ -f coverage.json ]; then
    run_lane cov-floor uv run --no-sync python scripts/verify/coverage_floors.py coverage.json
  else
    skip_lane cov-floor "no coverage.json — run the tests lane first"
  fi
fi

# ---------------------------------------------------------------- lane: parity-cov
# Is each storage method exercised on BOTH engines, or only on the one that never
# ships? cov-floor measures a per-module percentage and diff-cov measures only lines a
# diff touches, so code that is uncovered AND unedited is invisible to both -- which is
# how the `anchor` half-life bug survived a 3,420-line parity suite. See the docstring.
#
# Needs a coverage.json that saw BOTH engines. Without the DSN the postgres lane above
# does not run and every Postgres method reads as dark, so this SKIPS rather than
# reporting ~170 false gaps. The script has the same guard for a manual run.
if wants parity-cov; then
  say "lane: storage parity coverage"
  if [ ! -f coverage.json ]; then
    skip_lane parity-cov "no coverage.json — run the tests lane first"
  elif [ -z "${MEMOTRON_TEST_POSTGRES_DSN:-}" ]; then
    skip_lane parity-cov "MEMOTRON_TEST_POSTGRES_DSN unset"
  else
    run_lane parity-cov uv run --no-sync python scripts/verify/parity_coverage.py
  fi
fi

# ---------------------------------------------------------------- lane: diff-cov
if wants diff-cov; then
  say "lane: diff coverage vs origin/main"
  # Legacy gaps are invisible to diff-cover by construction, so this can demand a
  # high bar on NEW code without blocking on the 21% that predates the effort.
  if [ ! -f coverage.xml ]; then
    skip_lane diff-cov "no coverage.xml — run the tests lane first"
  elif ! git rev-parse --verify --quiet origin/main >/dev/null; then
    # Shallow clones lose history; this is the documented degradation
    # path, not a failure.
    skip_lane diff-cov "origin/main not present (shallow clone?)"
  else
    # The storage/postgres/** exclusion is CONDITIONAL, mirroring the reasoning in
    # coverage_floors.py's CONDITIONAL_EXEMPT rather than inventing a second rule:
    # exclude only when the lane did not run, because then every touched line reads
    # as uncovered and diff-cover would be measuring the CI wiring gap rather than
    # the change. When the lane DID run, the exclusion is the thing hiding the gap,
    # and the code this phase changes most is the code it was hiding.
    #
    # NOTE the leading wildcard is required -- the pattern
    # 'src/memotron/storage/postgres/*' matches nothing and fails silently.
    #
    # #141: ASK THE REPORT, do not remember the invocation. This used to test
    # $POSTGRES_COVERED, a shell variable set only when the Postgres lane ran in THIS
    # invocation -- so `bash scripts/check.sh diff-cov postgres`, with the tests lane
    # absent, read as "not covered" and applied the exclusion even when coverage.xml on
    # disk already held real Postgres coverage. That silently under-gates: a
    # Postgres-only diff goes un-diff-covered and nothing says so.
    #
    # coverage_floors.py owns POSTGRES_FLOOR and already parses this report for the
    # CONDITIONAL_EXEMPT decision, so asking it keeps ONE source of truth rather than
    # adding a second rule here.
    if uv run --no-sync python scripts/verify/coverage_floors.py --postgres-covered coverage.json; then
      note "report reflects a real Postgres run — diff-cover includes storage/postgres/**"
      run_lane diff-cov uv run --no-sync diff-cover coverage.xml \
        --compare-branch=origin/main --fail-under=90
    else
      note "report does not reflect a real Postgres run — storage/postgres/** excluded."
      note "A diff that touches only Postgres code is therefore NOT diff-covered here."
      run_lane diff-cov uv run --no-sync diff-cover coverage.xml \
        --compare-branch=origin/main --fail-under=90 \
        --exclude '*/postgres/*'
    fi
  fi
fi

# ---------------------------------------------------------------- lane: corpus
if wants corpus; then
  say "lane: official corpus"
  if [ -z "${MEMOTRON_PUBLIC_CORPUS_DIR:-}" ]; then
    skip_lane corpus "MEMOTRON_PUBLIC_CORPUS_DIR unset"
  else
    run_lane corpus env MEMOTRON_ALLOW_EPHEMERAL_KEK=1 \
      uv run --no-sync pytest -q --strict-markers -m corpus
  fi
fi

# ---------------------------------------------------------------- verdict
say "summary"
printf '  %-12s %-46s %s\n' "lane" "result" "elapsed"
printf '  %-12s %-46s %s\n' "------------" "----------------------------------------------" "-------"
for row in "${SUMMARY[@]}"; do
  IFS='|' read -r lane result elapsed <<<"$row"
  printf '  %-12s %-46s %s\n' "$lane" "$result" "$elapsed"
done

if [ "$FAIL" -eq 0 ]; then
  say "check PASSED"
else
  say "check FAILED"
fi
exit "$FAIL"
