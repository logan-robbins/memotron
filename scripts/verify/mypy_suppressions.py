#!/usr/bin/env python3
"""Is every `disable_error_code` in pyproject.toml still earning its place?

    uv run python scripts/verify/mypy_suppressions.py

mypy has `warn_unused_ignores`, which is on here — but it only covers INLINE
``# type: ignore`` comments. A ``disable_error_code`` in ``[[tool.mypy.overrides]]``
has no equivalent: once a module stops producing that error, the suppression stays,
mypy keeps silently ignoring the whole category, and nothing anywhere says so.

Why that matters more than it sounds
------------------------------------
mypy is the gate that catches the errors a REFACTOR makes. During the client.py split
it was the only thing that caught a blanket rewrite pointing two calls at a sibling
mixin that did not define the method — the package imported fine and the full suite was
green. Every suppressed code is a hole in exactly that gate, in exactly the module
someone is most likely to be moving code around in.

Measured 2026-08-30, the first time anyone looked: **17 of 109 declared codes were
already dead**, and three modules (`storage.sqlite`, `agent_memory`, `config`) needed
none at all. Their suppressions had outlived the debt they were written for by an
unknown number of commits, because nothing could tell anyone.

How it works
------------
Rewrites every ``disable_error_code`` to ``[]`` in a temp copy of the config, runs mypy
once, and compares what actually fires against what each module declares. A declared
code that never fires is dead. The original file is always restored.

This is a RATCHET, the same shape as ``coverage_floors.py --ratchet``: suppressions may
be added deliberately when real debt appears, and may not linger once it is paid.

A suppression that FIRES is not thereby justified (#135)
-------------------------------------------------------
The dead-suppression half above was necessary and not sufficient, and the gap was
expensive. On 2026-08-31 ``memotron.mcp_server`` declared ``attr-defined``; it fired
**27 times**, so this gate reported it as *"still earning its place"* and the lane passed
green. All 27 were real defects -- nine MCP tools reading fields their models do not
define, including ``run_due_dreams``, which raised ``TypeError`` on **every** call (#132).

So the passing condition was satisfied by the very problem the gate should have surfaced.
That is the shape this branch keeps finding, and this file was an instance of it.

The counting half fixes that: ``tests/mypy_suppressions.baseline.tsv`` records how many
errors each ``(module, code)`` pair hides, and a RISE fails. Adding debt behind an
existing suppression is now as visible as adding a new suppression.

Why a ratchet and not a cap
---------------------------
A cap ("more than N hidden errors is a finding") was the alternative. Measured 2026-09-01
across all 91 declared pairs: **244 hidden errors, min 1, median 1, max 30.** Against that
distribution any single N is invented -- it would fire on ``admin_server`` (30, already
filed as #138) and say nothing about the long tail of 1s, where a rise from 1 to 6 is the
more likely way real debt arrives unnoticed. Per-pair, measured, exact.

The honest limit, stated because a gate whose limits are unwritten gets over-trusted: a
ratchet catches GROWTH, not debt that lands all at once inside a brand-new suppression. A
new suppression is at least visible in the pyproject diff; its initial size is not gated.

Cost: one extra mypy run (~1-2s on this tree). Exit 0 clean, 1 stale, 2 could not run.
"""

from __future__ import annotations

import collections
import os
import pathlib
import re
import subprocess
import sys
import tomllib

PYPROJECT = pathlib.Path("pyproject.toml")
BASELINE = pathlib.Path("tests/mypy_suppressions.baseline.tsv")
_ERROR = re.compile(r"^src/([\w/]+)\.py:\d+: error: .*\[([a-z-]+)\]$")
_DISABLE = re.compile(r"^disable_error_code = \[[^\]]*\]$", re.MULTILINE)
#: Belt to the env-var braces above -- see the comment in _errors_with_nothing_suppressed.
_ANSI = re.compile(r"\x1b\[[0-9;]*m|\x1b\([AB]")


def _declared() -> list[tuple[str, set[str]]]:
    overrides = tomllib.loads(PYPROJECT.read_text())["tool"]["mypy"].get("overrides", [])
    out: list[tuple[str, set[str]]] = []
    for entry in overrides:
        codes = entry.get("disable_error_code")
        if not codes:
            continue
        modules = entry["module"]
        for module in [modules] if isinstance(modules, str) else modules:
            out.append((module, set(codes)))
    return out


def _errors_with_nothing_suppressed() -> dict[str, collections.Counter[str]] | None:
    """Run mypy with every suppression lifted. Restores pyproject.toml unconditionally."""
    original = PYPROJECT.read_text()
    try:
        PYPROJECT.write_text(_DISABLE.sub("disable_error_code = []", original))
        result = subprocess.run(
            ["uv", "run", "--no-sync", "mypy", "--no-pretty"],
            capture_output=True,
            text=True,
            check=False,
            # NO_COLOR/-FORCE_COLOR: mypy honours FORCE_COLOR even when stdout is a pipe, and
            # this session had FORCE_COLOR=3 set. The ANSI codes land between the `:` and the
            # `error:` -- `...py:851: \x1b[1m\x1b[31merror:\x1b(B\x1b[m ...` -- so `_ERROR`
            # matched NOTHING, 237 real error lines parsed as zero, and every suppression looked
            # dead. The gate FAILED asking for 89 live suppressions to be deleted. Nothing about
            # the repo had changed; only the environment had. Belt and braces: force it off here
            # AND strip anything that still gets through, because a parser that silently matches
            # nothing is indistinguishable from a clean run.
            env={**os.environ, "NO_COLOR": "1", "FORCE_COLOR": "0", "MYPY_FORCE_COLOR": "0", "TERM": "dumb"},
        )
    finally:
        # Unconditional: leaving a stripped config behind would turn every later run in
        # this working tree into a lie.
        PYPROJECT.write_text(original)

    # Counter, not set: #135. Discarding the count is what let a suppression hiding 27
    # real defects report as "still earning its place".
    live: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for line in _ANSI.sub("", result.stdout).splitlines():
        match = _ERROR.match(line)
        if match:
            live[match.group(1).replace("/", ".")][match.group(2)] += 1
    return live if result.stdout else None


def _hidden_counts(
    declared: list[tuple[str, set[str]]],
    live: dict[str, collections.Counter[str]],
) -> dict[tuple[str, str], int]:
    """How many errors each declared (module, code) pair is hiding, right now.

    A package's errors are reported against ``pkg/__init__.py``, which normalises to
    ``pkg.__init__`` rather than ``pkg`` -- both are summed or every composer reads as
    clean. That normalisation predates this function and is the reason the dead-code
    check works at all; it is repeated here rather than shared because the two callers
    want different shapes and a helper returning both was less readable than this.
    """
    counts: dict[tuple[str, str], int] = {}
    for module, codes in declared:
        for code in codes:
            counts[(module, code)] = (
                live.get(module, collections.Counter())[code]
                + live.get(f"{module}.__init__", collections.Counter())[code]
            )
    return counts


def _read_baseline() -> dict[tuple[str, str], int] | None:
    if not BASELINE.exists():
        return None
    out: dict[tuple[str, str], int] = {}
    for line in BASELINE.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        module, code, count = line.split("\t")
        out[(module, code)] = int(count)
    return out


def _write_baseline(counts: dict[tuple[str, str], int]) -> None:
    lines = [
        "# How many mypy errors each (module, disable_error_code) pair is currently hiding.",
        "# A RISE fails the gate: see scripts/verify/mypy_suppressions.py and #135. A FALL is",
        "# good news and is reported, not failed -- re-bless in the commit that paid the debt.",
        "#",
        "# Do not hand-edit to make a red run green. Regenerate:",
        "#     uv run python scripts/verify/mypy_suppressions.py --bless",
        "#",
        "# module\tcode\thidden_error_count",
    ]
    lines += [f"{module}\t{code}\t{count}" for (module, code), count in sorted(counts.items())]
    BASELINE.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    bless = "--bless" in (argv if argv is not None else sys.argv[1:])
    if not PYPROJECT.exists():
        print("  pyproject.toml not found -- run from the repo root")
        return 2

    declared = _declared()
    if not declared:
        print("  no disable_error_code entries found. That is either very good news or a")
        print("  parsing failure -- it has never been true in this repo, so assume the latter.")
        return 2

    live = _errors_with_nothing_suppressed()
    if live is None:
        print("  mypy produced no output with suppressions lifted; cannot judge. Not a pass.")
        return 2

    total = sum(len(codes) for _, codes in declared)
    counts = _hidden_counts(declared, live)
    stale: list[tuple[str, list[str], list[str]]] = []
    for module, codes in declared:
        dead = sorted(code for code in codes if counts[(module, code)] == 0)
        if dead:
            stale.append((module, dead, sorted(code for code in codes if counts[(module, code)])))

    hidden_total = sum(counts.values())
    print(f"mypy_suppressions: {len(declared)} module(s), {total} declared code(s), {hidden_total} error(s) hidden")

    if bless:
        _write_baseline(counts)
        print(f"  blessed {len(counts)} (module, code) pair(s), {hidden_total} hidden error(s) -> {BASELINE}")
        return 0

    # --- the counting ratchet (#135) --------------------------------------------------
    # Runs BEFORE the dead-suppression report so a rise is never buried under it, and
    # is reported even when dead entries also exist -- they are separate findings.
    baseline = _read_baseline()
    if baseline is None:
        print(f"  FAIL  {BASELINE} is missing -- the counting ratchet cannot run.")
        print("        A suppression that fires is not thereby justified (#135), so an")
        print("        absent baseline is not a pass. Record one:")
        print("            uv run python scripts/verify/mypy_suppressions.py --bless")
        return 1

    risen = sorted(
        (module, code, baseline.get((module, code), 0), now)
        for (module, code), now in counts.items()
        if now > baseline.get((module, code), 0)
    )
    fallen = sorted(
        (module, code, baseline[(module, code)], now)
        for (module, code), now in counts.items()
        if (module, code) in baseline and now < baseline[(module, code)]
    )

    # A pair in the baseline that is no longer DECLARED has been retired -- the suppression
    # was deleted, which is the outcome this gate exists to produce. It cannot cause a false
    # pass (a retired suppression hides nothing), so this is a note, not a failure. Reported
    # because an unreported orphan is how a baseline file rots into decoration, which is the
    # failure mode this whole gate was written against.
    retired = sorted(pair for pair in baseline if pair not in counts)
    if retired:
        print(f"  {len(retired)} baseline entry(s) no longer declared -- suppression retired, re-bless to drop:")
        for module, code in retired:
            print(f"        {module}: {code}  (was hiding {baseline[(module, code)]})")

    if fallen:
        # Good news, and deliberately NOT a failure -- a gate that punishes progress is
        # one people route around. Reported so the baseline gets tightened on purpose.
        print(f"  {len(fallen)} suppression(s) now hide FEWER errors -- re-bless to lock the gain in:")
        for module, code, was, now in fallen:
            print(f"        {module}: {code}  {was} -> {now}")

    if risen:
        print(f"\n  FAIL  {len(risen)} suppression(s) hide MORE errors than the baseline:")
        for module, code, was, now in risen:
            new_label = " (no baseline entry -- new suppression)" if was == 0 else ""
            print(f"        {module}: {code}  {was} -> {now}{new_label}")
        print()
        print("  Each added error is a real mypy finding this module is silently ignoring.")
        print("  Fix them, or -- if the debt is deliberate and understood -- re-bless and say")
        print("  why in the commit message:")
        print("      uv run python scripts/verify/mypy_suppressions.py --bless")
        return 1

    if not stale:
        print(f"  MYPY SUPPRESSIONS: PASS -- all {total} still fire, none hides more than its baseline")
        return 0

    dead_total = sum(len(dead) for _, dead, _ in stale)
    print(f"  FAIL  {dead_total} suppression(s) across {len(stale)} module(s) no longer fire:")
    for module, dead, kept in stale:
        tail = f"  (still needed: {', '.join(kept)})" if kept else "  (the whole entry can go)"
        print(f"        {module}: {', '.join(dead)}{tail}")
    print()
    print("  Each one is a category mypy is silently ignoring in a module where nothing")
    print("  produces it any more -- a hole in the gate that catches refactor mistakes.")
    print("  Delete them from [[tool.mypy.overrides]] in pyproject.toml.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
