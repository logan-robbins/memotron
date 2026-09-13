"""Is each storage method exercised on BOTH engines, or only on the one that never ships?

    uv run python scripts/verify/parity_coverage.py            # compare to the baseline
    uv run python scripts/verify/parity_coverage.py --bless    # accept the current numbers

Reads ``coverage.json`` and, for every method defined in both ``storage/sqlite`` and
``storage/postgres``, compares the two covered-line sets. A method fully covered on one engine
and not the other is a **one-sided gap**: the contract is tested, but only one of its two
implementations is.

Why the existing gates cannot see this
--------------------------------------
``cov-floor`` measures a per-module percentage against a floor, so a module can sit exactly at
its floor forever with the same lines dark. ``diff-cov`` only measures lines a diff touches.
Neither can answer *"has this method ever executed?"*, so code that is **uncovered AND unedited**
is invisible to both -- which is every gap this script reports.

That blind spot has a track record, not a hypothesis:

* ``postgres/_operational.py:920`` keyed an ``anchor`` half-life on ``"identity"``, so anchor
  memories decayed 4x faster on Postgres (90d instead of 365d) under a docstring claiming
  byte-identical behaviour. A 3,420-line parity suite passed, because it drove
  ``utility_projection`` with two memory types whose half-lives happen to match on both engines.
* T0-10: an epoch filter ``sqlite/_graph.py`` applied and ``postgres/_graph.py`` dropped.
* T0-4, same shape.

Each was found by a person reading code. This finds them by measurement.

How the gap surfaced in the first place
---------------------------------------
By accident, and that is the argument for the script. On 2026-09-01 a refactor repointed
``self._as_json(...)`` to a module-level ``as_json(...)`` in ``_operational.py``; the edit pulled
three long-uncovered lines into diff-coverage's view and the lane dropped below its floor.
Nothing was looking for them. Nothing would have looked.

Reading the number
------------------
**A gap is not a defect.** It says the suite does not look there, not that the code is wrong.
The reason to care is the base rate above: the last three times someone did look at an
unexercised Postgres path, all three were genuinely divergent.

Both directions are reported. `postgres_only` (covered on Postgres, dark on SQLite) is expected
to be small and is not the dangerous direction -- SQLite is the engine every hermetic test drives
-- but a rise in it means the SQLite twin stopped being exercised, which is worth the same look.

Requires a coverage.json from a run that drove BOTH engines: ``check.sh`` qualifies, because its
``postgres`` lane runs before ``cov-floor`` and contributes to the same report. A hermetic-only
run makes every Postgres method look uncovered and this script says so rather than reporting a
cliff of false gaps.

Exit 0 when no direction is worse than its baseline, 1 otherwise, 2 on a harness problem.
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
COVERAGE = ROOT / "coverage.json"
SRC = ROOT / "src" / "memotron" / "storage"

#: Modules that exist under both engines. A module present in only one is not a parity
#: question -- `_engine`, `_receipts` and `_migrations` are engine-specific by design.
TWINS = ("_operational", "_governance", "_graph", "_epochs", "_policy", "_artifacts")

#: Measured 2026-09-01 on a full `check.sh` run with MEMOTRON_TEST_POSTGRES_DSN set.
#: A RATCHET: these may fall and may not rise. Zero is the real target for `sqlite_only`.
# sqlite_only 20 -> 18 on 2026-09-02: two parity tests added for seal_receipt_detail and
# _canonical_visibility_agents, measured BEFORE the shared-plane extraction so the gain is
# attributable to coverage rather than to those methods leaving the compared population.
# postgres_only 7 -> 6 on 2026-09-06 (#126), measured on a full check.sh run with
# MEMOTRON_TEST_POSTGRES_DSN set. The gain is incidental: the new MCP identity tests
# drive real tools (add_memory, coherence_incidents) against the in-memory SQLite backend,
# so a storage member that had only ever run under the Postgres lane now runs under both.
# WHICH member is not recorded here because identifying it needs a second full Postgres run
# on origin/main to diff against, and the ratchet only requires the count to fall.
# sqlite_only 18 -> 13 on 2026-09-07 (#149): five methods whose ENTIRE Postgres body was dark
# now run on both engines -- use_event_for_idempotency_key, outcome_event_for_idempotency_key,
# and the three promotion-endorsement methods. Tests are in test_storage_backend_parity.py, so
# the SQLite side stays a control rather than becoming a second Postgres-only suite.
#
# Of the 13 that remain, TWO ARE NOT ORDINARY GAPS and closing them needs concurrency, not
# another assertion: `set_agent_motive_assignment`'s `except ForeignKeyViolation` arm sits behind
# an explicit `SELECT EXISTS` guard that raises the same ValueError first, and
# `get_or_create_governance_key`'s two lines are the lost-election branch of
# `ON CONFLICT DO NOTHING`. Both are TOCTOU fallbacks, unreachable through the public API. A
# future ratchet to 11 is the honest floor for assertion-only work.
BASELINE = {"sqlite_only": 12, "postgres_only": 6}

#: Methods whose one-sidedness is structural rather than a testing gap. Empty on purpose --
#: an entry here needs a reason that survives "why can this never run on the other engine?"
EXPECTED: dict[str, str] = {}


def _methods(path: pathlib.Path) -> dict[str, tuple[int, int]]:
    tree = ast.parse(path.read_text())
    return {
        node.name: (node.lineno, node.end_lineno or node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _coverage_for(files: dict[str, dict], engine: str, module: str) -> tuple[set[int], set[int]]:
    suffix = f"storage/{engine}/{module}.py"
    for path, data in files.items():
        if path.replace("\\", "/").endswith(suffix):
            return set(data["missing_lines"]), set(data["executed_lines"])
    return set(), set()


def _state(body: set[int], missing: set[int], executed: set[int]) -> str:
    if not body & (missing | executed):
        return "no statements"
    if not body & executed:
        return "never executed"
    return "covered" if not (body & missing) else f"{len(body & missing)} missing"


def measure() -> tuple[dict[str, list[tuple[str, str, str, str]]], int]:
    if not COVERAGE.is_file():
        print(f"  coverage.json not found at {COVERAGE}")
        print("  Re-run:  env -u LITELLM_API_KEY bash scripts/check.sh   (with the Postgres DSN set)")
        raise SystemExit(2)

    # STALENESS, which is a different failure from hermeticity and reads identically to a
    # legitimate "no change". A bare `pytest` run collects NO coverage at all -- `addopts` is
    # `--strict-markers --strict-config`, with no `--cov` -- so writing tests and re-running
    # pytest leaves coverage.json untouched and this script cheerfully reports the OLD number as
    # if it were a verdict on the new tests. Hit for real on 2026-09-07 while closing #149: the
    # report read "18, same" against a coverage.json nine hours older than the tests it was
    # supposedly measuring.
    newest_test = max(
        (p.stat().st_mtime for p in (ROOT / "tests").glob("test_*.py")),
        default=0.0,
    )
    if newest_test > COVERAGE.stat().st_mtime:
        print("  coverage.json is OLDER than the newest test file -- it cannot reflect it.")
        print("  A bare `pytest` run does not write coverage. Re-run:")
        print("      bash scripts/check.sh          (with MEMOTRON_TEST_POSTGRES_DSN set)")
        raise SystemExit(2)

    files = json.loads(COVERAGE.read_text())["files"]

    # A hermetic-only report makes every Postgres method look dark. Say so instead of
    # reporting a cliff of false gaps -- the same failure this script exists to catch.
    _, pg_executed = _coverage_for(files, "postgres", "_operational")
    if not pg_executed:
        print("  coverage.json contains no executed Postgres lines -- it is from a hermetic run.")
        print('  Re-run with:  export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 ..."')
        raise SystemExit(2)

    found: dict[str, list[tuple[str, str, str, str]]] = {"sqlite_only": [], "postgres_only": []}
    shared_total = 0

    for module in TWINS:
        paths = {engine: SRC / engine / f"{module}.py" for engine in ("sqlite", "postgres")}
        if not all(p.is_file() for p in paths.values()):
            continue
        defs = {engine: _methods(path) for engine, path in paths.items()}
        cov = {engine: _coverage_for(files, engine, module) for engine in paths}

        for name in sorted(set(defs["sqlite"]) & set(defs["postgres"])):
            shared_total += 1
            state = {}
            for engine in ("sqlite", "postgres"):
                lo, hi = defs[engine][name]
                state[engine] = _state(set(range(lo, hi + 1)), *cov[engine])
            if f"{module}.{name}" in EXPECTED:
                continue
            if state["sqlite"] == "covered" and state["postgres"] != "covered":
                found["sqlite_only"].append((module, name, state["sqlite"], state["postgres"]))
            elif state["postgres"] == "covered" and state["sqlite"] != "covered":
                found["postgres_only"].append((module, name, state["sqlite"], state["postgres"]))

    return found, shared_total


#: Members of `storage/_shared` that no test executes at all. NOT an engine-parity check --
#: see `_shared_unexercised`'s docstring for exactly what this does and does not prove.
SHARED_BASELINE = 0


def _shared_unexercised(files: dict[str, dict]) -> list[str]:
    """Shared-plane members that NO test executed. Deliberately weaker than it looks.

    When 13 methods moved into `storage/_shared` they left this gate's twin population
    permanently (172 -> 159), and `_shared` can never enter it: the comparison above needs a
    module under BOTH engines, and a shared plane has exactly one copy. So the question "was
    this exercised on Postgres as well as SQLite?" cannot be answered from `coverage.json`,
    which merges every run into one covered-line set per file.

    **What this checks instead:** that every `_shared` member ran at all. That catches the
    realistic regression -- a shared plane added with no test -- and it catches nothing subtler.

    **What it does NOT check, stated plainly so nobody reads more into a PASS:** that both
    engines reached it. Measured 2026-09-02, all 13 members DO run on both, verified by the
    two-arm command below rather than by this gate:

        pytest -q tests/test_storage_backend_parity.py -k sqlite   --cov=memotron.storage._shared
        pytest -q tests/test_storage_backend_parity.py -k postgres --cov=memotron.storage._shared

    That property holds because every shared member is reached through
    `test_storage_backend_parity.py`, whose `backend` fixture is parameterised over both engines
    -- "One live backend per engine; the same assertions run against both." A test written there
    is bi-engine by construction.

    An earlier attempt answered this with `--cov-context=test`. It was reverted: the flag made
    the tests lane die intermittently with `no such table: context` (cause never established --
    two hypotheses tested and refuted), and it needed a guard against `dynamic_context` collapsing
    the two engine params into one context, which would have made every shared member look
    bi-engine. A mechanism whose failure mode is a false PASS is the wrong mechanism for a gate
    whose whole purpose is catching a false PASS.
    """
    shared = ROOT / "src" / "memotron" / "storage" / "_shared"
    if not shared.is_dir():
        return []
    dark: list[str] = []
    for path in sorted(shared.glob("*.py")):
        if path.stem in ("__init__", "_protocol"):
            continue
        executed: set[int] = set()
        for recorded, data in files.items():
            if recorded.replace("\\", "/").endswith(f"_shared/{path.name}"):
                executed = set(data["executed_lines"])
                break
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            if not body & executed:
                dark.append(f"{path.stem}.{node.name}")
    return dark


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bless", action="store_true", help="rewrite BASELINE to the current numbers")
    args = parser.parse_args()

    found, shared_total = measure()
    counts = {key: len(rows) for key, rows in found.items()}
    dark = _shared_unexercised(json.loads(COVERAGE.read_text())["files"])

    print(f"\nstorage parity coverage -- {shared_total} methods defined on both engines")
    for key, label in (
        ("sqlite_only", "covered on SQLite, NOT on Postgres"),
        ("postgres_only", "covered on Postgres, NOT on SQLite"),
    ):
        was, now = BASELINE[key], counts[key]
        arrow = "same" if now == was else ("BETTER" if now < was else "WORSE")
        print(f"\n  {label}: {now} (baseline {was}) {arrow}")
        for module, name, sq, pg in found[key]:
            print(f"      {module:14} {name:44} sqlite={sq:14} postgres={pg}")

    if args.bless:
        text = pathlib.Path(__file__).read_text()
        new = f'BASELINE = {{"sqlite_only": {counts["sqlite_only"]}, "postgres_only": {counts["postgres_only"]}}}'
        text = re.sub(r'BASELINE = \{"sqlite_only": \d+, "postgres_only": \d+\}', new, text, count=1)
        pathlib.Path(__file__).write_text(text)
        print(f"\n  blessed {new}")
        return 0

    print(f"\n  shared planes never executed by ANY test: {len(dark)} (baseline {SHARED_BASELINE})")
    for name in dark:
        print(f"      storage/_shared/{name}")
    if not dark:
        print("      (all shared members ran; see _shared_unexercised for what this does NOT prove)")

    regressions = [key for key, now in counts.items() if now > BASELINE[key]]
    if len(dark) > SHARED_BASELINE:
        regressions.append("shared planes unexercised")
    if regressions:
        print(f"\nPARITY COVERAGE: FAIL -- {', '.join(regressions)} rose above baseline")
        print("  A method tested on one engine and not the other means the contract is tested")
        print("  and only one of its two implementations is. Add the assertion to")
        print("  tests/test_storage_backend_parity.py so it runs on BOTH, or explain the")
        print("  one-sidedness in EXPECTED with a reason that survives 'why can this never run?'.")
        return 1

    improved = [f"{key} {BASELINE[key]} -> {counts[key]}" for key, now in counts.items() if now < BASELINE[key]]
    if improved:
        print(f"\nPARITY COVERAGE: PASS -- improved ({'; '.join(improved)}). Lower the baseline in this commit.")
        return 0
    print("\nPARITY COVERAGE: PASS -- at baseline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
