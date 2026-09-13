"""Validation gate for discovery-phase findings.

Findings in this repo have repeatedly been over-claimed: two-variable comparisons
reported as controlled, UI strings matched to "defect" without reading their subject,
and assertions made against the wrong method. This gate is a linter for the record
itself — it fails on the shapes those mistakes take.

Run BEFORE recording a batch of findings, and again before handing off.

    uv run python scripts/verify/finding_gate.py

exit 0 = record is clean · 1 = violations · 2 = harness error
"""

from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

BACKLOG = ROOT / "TAKEOVER-BACKLOG.md"
UNKNOWNS = ROOT / "UNKNOWNS.md"
LOG = ROOT / "DOGFOOD-LOG.md"

# Evidence looks like: file.py:123 · `command` · a number with units · an explicit label
EVIDENCE = re.compile(
    r"(\.(?:py|ts|tsx|yaml|yml|json|sh|tf|md):\d+)"  # file:line
    r"|(`:\d+`)"  # bare line ref inside a scoped table
    r"|(`[^`]*(?:uv run|curl|docker|kubectl|npx|grep|git |gh |psql|sqlite3|pytest|ls |find |sed |awk |scripts/verify)[^`]*`)"  # a command
    r"|(`[A-Za-z_.]+:\s*\"?[^`\"]+\"?`)"  # quoted config evidence: `tag: "0.1.1"`
    r"|(\|\s*(?:D-\d+|DW-\d+|U-\d+|T[0-9b]+-\d+)[^|]*\|)"  # cross-reference to the evidence trail
    r"|(\b\d+\s*(?:/\s*\d+|%|ms|s\b|tools|routes|methods|tabs|facts|items|runs))"
    r"|(verified|reproduced|measured|observed|confirmed)",
    re.IGNORECASE,
)

HEDGE = re.compile(r"\b(probably|presumably|i think|might be|could be|seems to|appears to)\b", re.IGNORECASE)
LABELLED = re.compile(r"\b(ASSUMED|DERIVED|UNCONFIRMED|not yet confirmed|unverified|untested)\b", re.IGNORECASE)

# Discovery discipline: a Question must not contain an answer.
ANSWERY = re.compile(
    r"\b(therefore|so the fix|the answer is|we should|the right (?:answer|call) is|conclusion:)\b", re.IGNORECASE
)

violations: list[str] = []


def fail(where: str, why: str, snippet: str = "") -> None:
    violations.append(f"  {where}\n      {why}" + (f"\n      → {snippet[:100]}" if snippet else ""))


def check_backlog() -> None:
    if not BACKLOG.exists():
        return
    for i, line in enumerate(BACKLOG.read_text().splitlines(), 1):
        if not re.match(r"^\|\s*\*\*T[0-9b]+-\d+", line):
            continue
        item = re.search(r"\*\*(T[0-9b]+-\d+)\*\*", line)
        tag = item.group(1) if item else f"line {i}"
        if not EVIDENCE.search(line):
            fail(f"{BACKLOG.name} {tag}", "no evidence — needs file:line, a command, or a measurement", line)
        if HEDGE.search(line) and not LABELLED.search(line):
            fail(f"{BACKLOG.name} {tag}", "hedged claim with no ASSUMED/UNCONFIRMED label", line)


def check_unknowns() -> None:
    if not UNKNOWNS.exists():
        return
    text = UNKNOWNS.read_text()

    # every unknown needs a closing move
    for block in re.split(r"\n## (?=U-\d)", text)[1:]:
        tag = block.split("·")[0].strip()[:6]
        if "CLOSES WITH" not in block and "CLOSED" not in block:
            fail(f"{UNKNOWNS.name} {tag}", "unknown has no CLOSES WITH action")
        if "CLOSED" in block.split("\n")[0] and not EVIDENCE.search(block):
            fail(f"{UNKNOWNS.name} {tag}", "marked CLOSED without evidence")

    # discovery discipline: questions must stay questions
    qsec = text.split("# Open design questions")
    if len(qsec) > 1:
        body = qsec[1].split("## Closing order")[0]
        for block in re.split(r"\n\*\*(?=Q-\d)", body)[1:]:
            tag = block[:4]
            if ANSWERY.search(block):
                m = ANSWERY.search(block)
                fail(
                    f"{UNKNOWNS.name} {tag}",
                    "question contains an answer — discovery phase records questions",
                    block[max(0, m.start() - 40) : m.end() + 40],
                )
            if "?" not in block:
                fail(f"{UNKNOWNS.name} {tag}", "recorded as a question but asks nothing")


def check_log() -> None:
    """Every comparison claim must say what was held constant."""
    if not LOG.exists():
        return
    text = LOG.read_text()
    for block in re.split(r"\n## (?=D-\d)", text)[1:]:
        tag = block[:5]
        compares = re.search(r"\b(vs\.?|versus|differ|differential|compared)\b", block, re.IGNORECASE)
        if compares and not re.search(
            r"held constant|same (?:graph|file|query|moment|input)|only .{0,24} differs?", block, re.IGNORECASE
        ):
            fail(
                f"{LOG.name} {tag}",
                "comparison without stating what was held constant",
                block[max(0, compares.start() - 50) : compares.end() + 50],
            )


#: An entry that has been looked at again says so, with a verdict word.
RECHECKED = re.compile(
    r"\b(FIXED|REFUTED|CORRECTION|CONFIRMED|VERIFIED|CLOSED|RE-?CHECKED|OVERTAKEN|PARTIALLY)\b",
    re.IGNORECASE,
)
ENTRY_ROW = re.compile(r"^\|\s*\*\*(T\d+-\d+)")


def report_unverified() -> None:
    """Print how many register entries have never been re-checked.

    A **report, not a gate** — it cannot fail the run and deliberately does not
    touch the exit code. The count is a standing hypothesis load, not a defect:
    "31 entries unverified" is a fact about how much of the register is still
    unconfirmed, and the useful thing is that it is *visible* rather than
    discovered by hand while counting for some unrelated question (#136).

    Note what this does NOT check. #136 also proposed failing on any `file.py:NNN`
    citation that does not resolve against HEAD. That check is wrong here and is
    not implemented: the register pins its line references to
    `audit-baseline-2026-08-27` **on purpose** (see its header), so 469 citations
    name files the module split deleted. Measured 2026-09-02: 82 of 91 resolve
    inside the file they name *at that tag*, 0 out of range. A HEAD-resolving gate
    would fail on correct, documented, deliberate behaviour — the exact defect
    shape this gate exists to catch.
    """
    if not BACKLOG.exists():
        return
    rows = [line for line in BACKLOG.read_text().splitlines() if ENTRY_ROW.match(line)]
    never = [ENTRY_ROW.match(r).group(1) for r in rows if not RECHECKED.search(r)]  # type: ignore[union-attr]
    if not rows:
        return
    print(f"\n=== register inventory: {BACKLOG.name} ===")
    print(f"  {len(rows)} entries · {len(rows) - len(never)} re-checked · {len(never)} never re-checked")
    if never:
        by_tier: dict[str, int] = {}
        for tag in never:
            by_tier[tag.split("-")[0]] = by_tier.get(tag.split("-")[0], 0) + 1
        spread = " ".join(f"{t}:{n}" for t, n in sorted(by_tier.items()))
        print(f"  outstanding hypotheses by tier: {spread}")
        print(f"  {', '.join(never)}")
    print("  (report only — does not affect the exit code)")


def main() -> int:
    for fn in (check_backlog, check_unknowns, check_log):
        fn()

    report_unverified()

    print(f"\n=== finding gate: {BACKLOG.name}, {UNKNOWNS.name}, {LOG.name} ===")
    if not violations:
        print("  PASS — every finding cites evidence, every unknown has a closing move,")
        print("         every question stays a question, every comparison names its control.")
        return 0
    print(f"  FAIL — {len(violations)} violation(s):\n")
    for v in violations:
        print(v)
    print("\n  Fix the record, not the gate.")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback

        traceback.print_exc()
        sys.exit(2)
