#!/usr/bin/env python3
"""Per-module coverage floors — a ratchet, not a cliff.

    uv run pytest -q --cov --cov-report=json:coverage.json
    uv run python scripts/verify/coverage_floors.py

Why per-module rather than one `fail_under`
-------------------------------------------
The single global number is 78.5%, and **2,207 of the 4,663 missed statements are
the Postgres subpackage** — which is not poorly tested, it is *unrunnable in CI*
because `MEMOTRON_TEST_POSTGRES_DSN` is unset. A global threshold would be
measuring the CI wiring gap, and it would jump ~9 points the day that lane is
wired, for no change in code quality. So Postgres is EXEMPT and visibly so,
rather than hidden in an `omit` where it would silently stop counting.

The floors below are each ~1 point under the value measured on 2026-08-27. That
gap is deliberately small: the point is not to certify the codebase as
well-tested, it is to make it **impossible for the module split to quietly drop
coverage while moving 35k lines around**. A refactor that is genuinely a pure
move cannot lower these.

Raising a floor is a normal reviewable commit. Lowering one requires saying why
in the commit message. `--ratchet` prints the floors that are now too slack.
"""

from __future__ import annotations

import argparse
import collections
import json
import sys
from pathlib import Path

# Exempt until the CI Postgres lane is wired (INFRA-BACKLOG). Not `omit`-ed:
# an exemption you can see is one someone will eventually retire.
#: Exempt ONLY when unmeasured. `storage/postgres` needs a live
#: MEMOTRON_TEST_POSTGRES_DSN, and a run without one reports it at 0% -- so a hard
#: floor would false-fail every local SQLite-only run, which is a workflow that has to
#: keep working: **the product must stay genuinely runnable on SQLite locally, while
#: the hosted deployment is Postgres.** That duality is the reason the parity suite
#: exists at all, and the reason this cannot simply be a floor like everything else.
#:
#: So the rule is conditional: if the lane did not run, skip it and say so; if it DID,
#: hold it to POSTGRES_FLOOR. Measured 2026-08-28 on the first run of that lane in
#: this effort: 1919/2211 = 86.8%, against 0% for the whole programme before it.
CONDITIONAL_EXEMPT = {"memotron.storage.postgres"}
POSTGRES_FLOOR = 85.8
#: Modules with no floor at all, and WHY — an exemption you can see is one someone will
#: eventually retire, which is this file's own stated doctrine.
#:
#: `storage.sqlite._protocol` carried a FLOOR of 0.0 until 2026-08-31, found by an
#: independent reviewer of PR #103. A floor of zero cannot fail: it looks like a guard in
#: the table and guards nothing, which is strictly worse than an exemption because it is
#: invisible. The 0% is CORRECT -- the file is a `Protocol` whose method bodies are `...`
#: and never execute -- so the honest encoding is "exempt, because unexecutable", not
#: "floor: 0.0".
#:
#: Its siblings `client._protocol` and `dreaming._protocol` keep 99.0 floors and are NOT
#: exempt: those are imported at runtime (the composed-surface Protocols with
#: `_Base = object`), so their module bodies do execute. Do not "fix" the inconsistency by
#: lowering theirs -- the difference is real.
EXEMPT: dict[str, str] = {
    "memotron.storage.sqlite._protocol": (
        "a Protocol whose method bodies are `...` and never execute; 0% is correct, "
        "and a floor of 0.0 would be a guard that cannot fail"
    ),
}

# module -> floor %. Measured 2026-08-27 on 1011 passed / 77 skipped, minus ~1pt.
FLOORS: dict[str, float] = {
    # --- the split's targets. These are the ones that matter. -----------------
    "memotron.dreaming": 87.5,
    # The 10 mixins and 5 module-level files the dreaming split produced. Each
    # floor is ~1pt under measured, same rule as everything above. Worth reading as
    # a map of where the suite is thin: _dedup at 80.1% is the lowest in the
    # package, and _weaker_duplicate lives there -- the 21-line method that decides
    # which of two equivalent claims gets dropped.
    "memotron.dreaming._candidates": 99.0,
    "memotron.dreaming._common": 99.0,
    # 88.8, not the ~90.1 the measured 91.1% would suggest, and the gap is a
    # FINDING rather than slack. Two lines -- the "another run holds its claim"
    # contention branch at _consolidation.py:81,85 -- are NONDETERMINISTIC across
    # runs: measured 91.14 / 91.14 / 89.87 over three identical invocations at the
    # same commit. Whichever test reaches that branch does so by run ORDER, which
    # means its assertions are order-dependent too. Floor set under the observed
    # minimum so this cannot redden at random; the flake itself is recorded in
    # STATE.md and is worth fixing at the test, not here.
    "memotron.dreaming._consolidation": 88.8,
    "memotron.dreaming._contradiction": 86.9,
    "memotron.dreaming._decisions": 87.1,
    "memotron.dreaming._dedup": 79.1,
    "memotron.dreaming._entities": 87.4,
    # Phase A moved members between these two. The floors are re-baselined, NOT
    # relaxed: dreaming/ as a whole went 89.527% -> 89.573% across the three
    # commits (measured both sides). _predicates fell 89.7 -> 87.9 only because it
    # absorbed _resolve_memory_type_value, which is thinner than its own average;
    # _formation rose 94.7 -> 95.2 for the mirror-image reason. The denominator
    # moved, the tested lines did not.
    "memotron.dreaming._formation": 94.2,
    "memotron.dreaming._identity": 97.1,
    "memotron.dreaming._lifecycle": 82.7,
    "memotron.dreaming._materialization": 94.0,
    "memotron.dreaming._predicates": 86.9,
    "memotron.dreaming._protocol": 99.0,
    "memotron.dreaming._rollup_text": 93.4,
    "memotron.dreaming._rollups": 90.7,
    "memotron.dreaming._text": 95.8,
    # client.py -> client/, 2026-08-29. The composer floor RISES 88.0 -> 93.8: the
    # same tests now cover 1,068 lines instead of 8,914, so the old floor would have
    # let 6pt of real coverage rot away unnoticed. Same measured-minus-1pt rule.
    #
    # That map is now out of date in the good direction, kept because the history is
    # the useful part. It used to read: "_crypto 66.0% and _governance 74.4% are the two
    # thinnest and both are security-adjacent, which is the wrong place to be thin."
    # T3-10 did that pass. **_crypto 66.0 -> 98.0** (the only reveal/redact/re-seal
    # round-trip in the client had ZERO behavioural tests) and _governance 74.4 -> 79.1.
    # The two lines left in _crypto are blank-argument guards.
    #
    # _governance at 79.1% is now the thinnest security-adjacent module in the package;
    # 619-684 is the largest remaining unexercised block.
    "memotron.client": 93.8,
    "memotron.client._archive": 92.2,
    "memotron.client._artifacts": 88.4,
    "memotron.client._certification": 88.8,
    "memotron.client._common": 99.0,
    "memotron.client._crypto": 97.0,
    "memotron.client._governance": 78.1,
    "memotron.client._ingest": 87.7,
    "memotron.client._lifecycle": 88.2,
    "memotron.client._outcomes": 84.8,
    "memotron.client._profile": 95.8,
    "memotron.client._protocol": 99.0,
    "memotron.client._retrieval": 93.0,
    "memotron.client._runtime": 94.0,
    "memotron.client._timeline": 89.1,
    "memotron.client._views": 91.5,
    # --- #251 caveman: a bounded graph of concepts and typed beliefs over an
    # append-only claim ledger and an append-only event journal.
    #
    # Twenty-three modules, re-floored after amendment D (typed edges, plain-ASCII
    # rendering, the journal and the replay proof, evidence, the MCP surface).
    # `lines` is gone -- the sigil grammar went with it, and its ordering and
    # header logic moved to `render` as word-based equivalents.
    #
    # Every one of these measures 100% except `models`, and that is a property of
    # the decomposition rather than of effort: the modules carrying the bulk of
    # the logic -- `tokens`, `render`, `rank`, `pressure`, `explain`, `replay` --
    # are pure, with no I/O and no LLM in them, so they are cheap to cover to the
    # VALUE rather than to the line. Same measured-minus-1pt rule as everything
    # above, capped at 99.0.
    #
    # `models` trails by a few statements, all of them `__get_pydantic_core_schema__`
    # style plumbing and defensive branches pydantic itself makes unreachable from
    # a test. It is the one module here where the gap is not a decision.
    #
    # The four modules amendment D added:
    #
    #   `render` -- the ONE place LLM-facing text is shaped. Pure, so 100%.
    #   `replay` -- the journal fold and the content digest. Pure, and no
    #   transport of any kind, which `test_caveman_replay.py` enforces by giving
    #   it nothing to call.
    #   `mcp` -- ten tools over one `CavemanMemory`. Fully covered because no tool
    #   body reads a store: each is one delegating call, driven through the
    #   module's single `install_runtime_factory` seam.
    #   `guidance` -- two `importlib.resources` reads of the shipped `AGENTS.md`
    #   and `SKILL.md`.
    #
    # `memotron.caveman` is the package `__init__`, which carries a docstring
    # and an empty `__all__` by design -- the design document forbids re-exports
    # so that `tests/api_surface.golden.txt` churn stays confined to the modules
    # that actually change. Two statements, both executed on import.
    "memotron.caveman": 99.0,
    "memotron.caveman.dream": 99.0,
    "memotron.caveman.errors": 99.0,
    "memotron.caveman.explain": 99.0,
    "memotron.caveman.extract": 99.0,
    "memotron.caveman.gateway": 99.0,
    "memotron.caveman.graph": 99.0,
    "memotron.caveman.guidance": 99.0,
    "memotron.caveman.ledger": 99.0,
    "memotron.caveman.llm": 99.0,
    "memotron.caveman.mcp": 99.0,
    "memotron.caveman.models": 97.9,
    "memotron.caveman.motive": 99.0,
    "memotron.caveman.pipeline": 99.0,
    "memotron.caveman.pressure": 99.0,
    "memotron.caveman.rank": 99.0,
    "memotron.caveman.read": 99.0,
    "memotron.caveman.receipts": 99.0,
    "memotron.caveman.reconcile": 99.0,
    "memotron.caveman.render": 99.0,
    "memotron.caveman.replay": 99.0,
    "memotron.caveman.seams": 99.0,
    "memotron.caveman.tokens": 99.0,
    "memotron.storage.sqlite": 94.8,
    # --- sqlite/, split into concern mixins 2026-08-27, mirroring storage/postgres/.
    # Higher than the config/ floors because the parity suite exercises this
    # surface hard; _epochs and _artifacts trail because branch/adopt/rollback and
    # live-artifact outcomes have paths the hermetic run does not reach.
    "memotron.storage.sqlite._artifacts": 87.1,
    "memotron.storage.sqlite._epochs": 87.3,
    "memotron.storage.sqlite._governance": 95.7,
    "memotron.storage.sqlite._graph": 96.1,
    "memotron.storage.sqlite._migrations": 88.6,
    "memotron.storage.sqlite._operational": 92.7,
    "memotron.storage.sqlite._policy": 91.1,
    # --- storage/_shared/, the engine-agnostic planes, added 2026-09-01. Same
    # ~1pt-under-measured rule as everything else. These are the 13 members that
    # were two byte-identical copies until now, so each floor here replaces a pair
    # of floors that were being met separately by the same assertions run twice.
    #
    # `_claims` at 82.7 and `_projection` at 85.6 are the two that trail, and both
    # for the same reason they trailed inside `_operational`: the blank-argument
    # rejection branches and the receipt-replay error paths are driven by the
    # parity suite, not the hermetic one. `_protocol` is NOT exempt like
    # `sqlite/_protocol` is -- it is imported at runtime (`_Base = object`), so its
    # module body executes, which is the same distinction recorded for
    # `client._protocol` above.
    "memotron.storage._shared": 99.0,
    "memotron.storage._shared._claims": 82.7,
    "memotron.storage._shared._governance": 99.0,
    "memotron.storage._shared._graph": 99.0,
    "memotron.storage._shared._projection": 85.6,
    "memotron.storage._shared._protocol": 99.0,
    "memotron.storage._shared._rows": 91.6,
    # 0.0% and correctly so: a Protocol body is `...` stubs that never execute.
    # Floored visibly rather than hidden in coverage `omit`, so the zero is a
    # stated fact about type-checking-only code, not a gap someone should chase.
    "memotron.config": 99.0,
    # --- config/, split out 2026-08-27. Wider spread than models/ because these
    # are behaviour policies with branches, not value objects: _storage_env reads
    # env vars the hermetic suite never sets, and _control_plane is the operator
    # surface, which the suite exercises only partially.
    "memotron.config._actionability": 69.5,
    "memotron.config._claim_mode": 99.0,
    "memotron.config._control_plane": 75.1,
    "memotron.config._dream_config": 93.6,
    "memotron.config._governance": 92.2,
    "memotron.config._instructions": 91.2,
    "memotron.config._metadata": 79.5,
    "memotron.config._motive": 93.9,
    "memotron.config._policies": 86.8,
    "memotron.config._predicates": 91.3,
    "memotron.config._prompts": 99.0,
    "memotron.config._retrieval": 90.8,
    "memotron.config._storage_env": 57.6,
    "memotron.config._storage_settings": 92.3,  # measured 93.3% (30 stmts) -- moved from storage.settings (SZ-2)
    "memotron.config._tenancy": 89.7,
    "memotron.models": 98.0,
    # --- models/, split out 2026-08-27. Value objects, so coverage is high and
    # the floors are tight; a drop here means a model stopped being constructed.
    "memotron.models._artifacts": 98.1,
    "memotron.models._coherence": 97.2,
    "memotron.models._enums": 99.0,
    "memotron.models._events": 95.7,
    "memotron.models._graph": 96.6,
    "memotron.models._health": 98.2,
    "memotron.models._mutations": 99.0,
    "memotron.models._results": 98.3,
    "memotron.models._runs": 98.0,
    "memotron.models._views": 99.0,
    "memotron.agent_memory": 97.1,
    # The 17 modules the agent_memory split produced. Read as a map of where the
    # suite is thin: _prompts at 72.2% is the lowest, and it holds the mutating
    # apply/clear pair that writes a tenant override onto a live client -- the one
    # place here where a half-applied change persists.
    "memotron.agent_memory._bootstrap": 94.6,
    "memotron.agent_memory._builders": 99.0,
    "memotron.agent_memory._common": 90.7,
    "memotron.agent_memory._config": 99.0,
    "memotron.agent_memory._contract": 99.0,
    "memotron.agent_memory._curation": 93.3,
    "memotron.agent_memory._project": 95.5,
    "memotron.agent_memory._prompts": 71.2,
    "memotron.agent_memory._protocol": 99.0,
    "memotron.agent_memory._publish": 95.3,
    "memotron.agent_memory._registry": 92.9,
    "memotron.agent_memory._render": 92.5,
    "memotron.agent_memory._reporting": 94.6,
    "memotron.agent_memory._results": 94.3,
    "memotron.agent_memory._scopes": 93.1,
    "memotron.agent_memory._session": 96.1,
    "memotron.agent_memory._transcripts": 92.8,
    # Re-baselined, NOT relaxed. The composer kept its thinner-covered residue while
    # the well-covered parts moved into submodules, so this number fell while the
    # PACKAGE improved: 73.94% -> 75.26% measured on both sides of the split.
    "memotron.admin_server": 70.8,
    # The 8 modules admin_server's Shape B half produced. _parsing at 61.0% is the
    # thinnest in the whole tree AND the most exposed: it is the only thing between an
    # HTTP query string and the rest of the admin server, and it raises rather than
    # defaulting precisely because a silently-defaulted filter returns a
    # plausible-looking wrong answer over someone else's memory. Lowest coverage on the
    # highest-consequence input path is the first thing to fix here.
    "memotron.admin_server._demo": 99.0,
    "memotron.admin_server._errors": 99.0,
    "memotron.admin_server._motive": 81.3,
    "memotron.admin_server._parsing": 60.0,
    "memotron.admin_server._payloads": 95.6,
    "memotron.admin_server._policy": 99.0,
    "memotron.admin_server._snippets": 99.0,
    "memotron.admin_server._tenant": 87.0,
    # --- everything else, alphabetical ---------------------------------------
    "memotron": 91.0,
    "memotron.adoption": 84.0,
    "memotron.agent_memory_mcp": 82.0,
    "memotron.agents": 98.0,
    "memotron.attestations": 83.5,
    "memotron.certification": 77.5,
    "memotron.cli": 78.5,
    "memotron.coherence": 88.0,
    "memotron.context": 82.5,
    "memotron.crypto": 84.5,
    # Lowered 89.5 -> 87.5 on 2026-09-01, and NOT because coverage was lost. The shared
    # OpenAI transport base moved 26 COVERED statements out of this module into
    # gateway.py, where they are still exercised: coverage.parser reports 102 -> 76
    # statements. The uncovered set is byte-identical -- the same eight validation
    # branches, none of them touched by that diff -- so the percentage fell purely
    # because the denominator shrank. Deduplicating covered code lowering a coverage
    # percentage is the standing perverse incentive of a ratio floor; re-derived rather
    # than papered over with tests for branches that were already dark.
    "memotron.embedding": 87.5,
    "memotron.epochs": 86.0,
    "memotron.erasure": 86.0,
    "memotron.extraction": 91.5,
    "memotron.gateway": 99.0,
    "memotron.gateway_identity": 97.1,  # measured 98.1% (104 stmts) -- #126 T3-1
    "memotron.graph": 99.0,
    "memotron.health": 86.5,
    "memotron.identity": 85.0,
    # Observability (#129). The `otel` extra is in the DEV group so these paths execute:
    # without it `observability.otel` measures 20% -- everything past its
    # `try: import opentelemetry` guard is unreachable -- and a floor at that number would
    # be a guard that cannot fail, which this file's own header calls out as worse than an
    # exemption. With the extra installed AND the absent-package paths tested by
    # forcing ImportError via sys.modules, it is 95.4%; the 3 remaining lines are
    # flush_telemetry's own ImportError arm.
    "memotron.observability": 99.0,  # measured 100.0% (13 stmts)
    "memotron.observability._logging": 99.0,  # measured 100.0% (51 stmts)
    "memotron.observability.otel": 94.4,  # measured 95.4% (65 stmts) -- vendored
    "memotron.interop": 87.5,
    "memotron.key_registry_cli": 92.1,  # measured 93.1% (87 stmts) -- #168 operator write path
    # #126. The per-request identity guard. A high floor is deliberate here rather than
    # conventional: every uncovered line in this module is a branch that decides whether an
    # unauthenticated or wrong-tenant caller is refused, and an untested branch in it fails
    # in the permissive direction without raising anything.
    "memotron.mcp_auth": 96.3,  # measured 97.3% (110 stmts)
    "memotron.memory_bank": 99.0,
    "memotron.migration": 95.0,
    "memotron.multimodal": 92.0,
    "memotron.platform_client": 79.0,
    "memotron.receipts": 99.0,
    "memotron.redaction": 94.0,
    "memotron.replay": 89.0,
    "memotron.retrieval": 94.0,
    "memotron.runtime": 82.5,
    "memotron.session": 93.5,
    "memotron.storage": 99.0,
    "memotron.storage.base": 99.0,
    "memotron.storage.composite": 84.5,
    "memotron.storage.factory": 83.5,
    "memotron.storage.receipts": 92.0,
    "memotron.storage.settings": 92.0,
    # The uncovered third is `main()`: argparse, client construction, and the resident
    # loop. Verified by RUNNING the entry point rather than by unit-testing argparse --
    # `memotron-dream-worker --once` against a seeded store took REQUIRES rows 0 -> 1,
    # and the first run of it caught a real design error (tenant-scoped runs need a
    # control plane). A mocked-argparse test would raise this number and prove less.
    "memotron.worker": 66.0,
    "memotron.transcripts": 96.0,
    # --- acknowledged debt. Low on purpose; these are entry points and the ----
    # --- HTTP surface, and admin_server is where the two exception leaks live.
    "memotron.local_platform": 18.0,
    "memotron.mcp_server": 45.5,
    "memotron.synthesis": 44.5,
}

# How far above its floor a module must sit before we nag about tightening it.
RATCHET_SLACK = 3.0


def module_key(path: str) -> str:
    key = path.removeprefix("src/").removesuffix(".py").replace("/", ".").removesuffix(".__init__")
    if key.startswith("memotron.storage.postgres"):
        return "memotron.storage.postgres"
    return key


def measured(report: Path) -> dict[str, tuple[int, float]]:
    data = json.loads(report.read_text())
    totals: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
    for path, info in data["files"].items():
        bucket = totals[module_key(path)]
        bucket[0] += info["summary"]["num_statements"]
        bucket[1] += info["summary"]["covered_lines"]
    return {key: (stmts, (100.0 * covered / stmts) if stmts else 100.0) for key, (stmts, covered) in totals.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("report", nargs="?", default="coverage.json", type=Path)
    parser.add_argument("--ratchet", action="store_true", help="also list floors that could be raised")
    parser.add_argument(
        "--postgres-covered",
        action="store_true",
        help=(
            "exit 0 if THIS REPORT reflects a real Postgres run (storage.postgres at or above "
            "POSTGRES_FLOOR), 1 otherwise. Answers the question from the report rather than from "
            "what the caller remembers running. Prints one line; see #141."
        ),
    )
    args = parser.parse_args(argv)

    if not args.report.exists():
        if args.postgres_covered:
            # No report cannot mean "covered". Fail closed, quietly -- the caller is a
            # shell conditional, not a human, and its own lane already reports the absence.
            print(f"  postgres-covered: NO — {args.report} does not exist")
            return 1
        print(f"coverage report not found: {args.report}")
        print("run: uv run pytest -q --cov --cov-report=json:coverage.json")
        return 2

    # A stale report is worse than no report: it reads as PASS. Observed during the
    # config/ split -- 14 brand-new modules were entirely absent from an older
    # coverage.json, so the unguarded-module check had nothing to complain about and
    # the gate went green over a tree it had never measured.
    # TESTS COUNT TOO, and until 2026-09-07 they did not. Coverage is a function of the tests
    # as much as the source, so a test-only edit is precisely the case that moves the number
    # while leaving `src/` untouched -- and it slipped straight through a guard that watched
    # only `src/`. Observed: `mcp_auth` was reported at 95.3% against its 96.3% floor, tests
    # were added for the uncovered branches, and it still read 95.3%, because a bare `pytest`
    # collects NO coverage (`addopts` carries no `--cov`) and the report was from an earlier
    # run. That reads identically to "the new tests did not help", which is the worst possible
    # way to be wrong about a ratchet: it argues for lowering the floor.
    #
    # `parity_coverage.py` already watches both, having hit the same trap on 2026-09-06.
    newest_input = max(
        (p.stat().st_mtime for p in (*Path("src").rglob("*.py"), *Path("tests").glob("test_*.py"))),
        default=0.0,
    )
    if newest_input > args.report.stat().st_mtime:
        print(f"STALE: {args.report} predates the newest file under src/ or tests/.")
        print("It cannot know about modules or tests added since. Re-run:")
        print("  uv run pytest -q --cov --cov-report=json:coverage.json")
        return 2

    if args.postgres_covered:
        # #141: `check.sh` used to track this in a shell variable set when the Postgres
        # lane ran IN THAT INVOCATION. The flag therefore described the invocation, not the
        # report -- so `check.sh diff-cov postgres`, with the tests lane absent, read as
        # "not covered" even when coverage.xml on disk already held real Postgres coverage,
        # and diff-cover silently applied the storage/postgres/** exclusion. That direction
        # is under-gating: a Postgres-only diff goes un-diff-covered and nothing says so.
        #
        # Deriving it here keeps ONE source of truth about what the report contains, and
        # reuses POSTGRES_FLOOR rather than inventing a second threshold. Below the floor
        # the answer is NO on purpose: either the lane did not run, or it ran and regressed,
        # and in the second case `cov-floor` is already failing the run.
        postgres = {module: value for module, value in measured(args.report).items() if module in CONDITIONAL_EXEMPT}
        if not postgres:
            print("  postgres-covered: NO — no memotron.storage.postgres module in the report")
            return 1
        module, (stmts, pct) = next(iter(postgres.items()))
        if pct >= POSTGRES_FLOOR:
            print(f"  postgres-covered: YES — {module} at {pct:.1f}% ({stmts} stmts), floor {POSTGRES_FLOOR}%")
            return 0
        print(f"  postgres-covered: NO — {module} at {pct:.1f}% ({stmts} stmts) is below floor {POSTGRES_FLOOR}%")
        return 1

    actual = measured(args.report)
    breaches: list[str] = []
    slack: list[str] = []
    unknown = sorted(set(actual) - set(FLOORS) - set(EXEMPT) - CONDITIONAL_EXEMPT)
    missing = sorted(set(FLOORS) - set(actual))

    for module, floor in sorted(FLOORS.items()):
        if module not in actual:
            continue
        stmts, pct = actual[module]
        if pct + 1e-9 < floor:
            breaches.append(f"{module:48} {pct:5.1f}%  <  floor {floor:5.1f}%   ({stmts} stmts)")
        elif pct - floor > RATCHET_SLACK:
            slack.append(f"{module:48} {pct:5.1f}%  floor {floor:5.1f}%  (+{pct - floor:.1f})")

    for module in sorted(set(EXEMPT) & set(actual)):
        stmts, pct = actual[module]
        print(f"  EXEMPT   {module} at {pct:.1f}% ({stmts} stmts) -- {EXEMPT[module]}")

    # Conditional: unmeasured means the lane did not run, and a floor would punish a
    # local SQLite-only run. Measured means hold it, because from that point a
    # regression is real.
    conditional_breaches: list[str] = []
    for module in sorted(CONDITIONAL_EXEMPT & set(actual)):
        stmts, pct = actual[module]
        if pct <= 0.0:
            print(
                f"  SKIPPED  {module} unmeasured ({stmts} stmts) -- the lane needs "
                "MEMOTRON_TEST_POSTGRES_DSN. Not a pass; nothing was checked."
            )
        elif pct < POSTGRES_FLOOR:
            conditional_breaches.append(f"{module}  {pct:.1f}%  <  floor  {POSTGRES_FLOOR}%   ({stmts} stmts)")
        else:
            print(f"  MEASURED {module} at {pct:.1f}% ({stmts} stmts), floor {POSTGRES_FLOOR}%")

    if unknown:
        # A new module with no floor is how coverage silently erodes: the split
        # creates ~50 of them, and an unlisted module is an unguarded one.
        print(f"\n  FAIL     {len(unknown)} module(s) have no floor -- add them to FLOORS:")
        for module in unknown:
            stmts, pct = actual[module]
            print(f'           "{module}": {max(0.0, pct - 1.0):.1f},   # measured {pct:.1f}% ({stmts} stmts)')

    if missing:
        print(
            f"\n  note: {len(missing)} floor(s) reference modules not in the report "
            "(renamed or deleted?): " + ", ".join(missing)
        )

    breaches += conditional_breaches
    if breaches:
        print(f"\n  FAIL     {len(breaches)} module(s) below floor:")
        for line in breaches:
            print(f"           {line}")

    if args.ratchet and slack:
        print(f"\n  ratchet: {len(slack)} floor(s) could be raised:")
        for line in slack:
            print(f"           {line}")

    print()
    if breaches or unknown:
        print(f"COVERAGE FLOORS: FAIL ({len(breaches)} below floor, {len(unknown)} unguarded)")
        return 1
    print(f"COVERAGE FLOORS: PASS ({len(FLOORS)} modules guarded, {len(EXEMPT)} exempt)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
