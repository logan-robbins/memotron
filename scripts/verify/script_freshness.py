#!/usr/bin/env python3
"""Do the scripts still describe the tree they measure?

    uv run python scripts/verify/script_freshness.py

Why this exists
---------------
On 2026-08-31 the answer to "are our scripts out of date after the refactor?" turned out to
be yes, and nothing could have told us. Found by hand that day:

  coupling_report.py   dead since `client.py` became a package. TWO hardcoded paths. Its own
                       docstring calls it "the instrument the pass is steered by", and it had
                       raised FileNotFoundError for the whole of the pass it steers.
  sweep_admin.py       dead the same way -- read `src/memotron/admin_server.py` to
                       discover routes, so one of the four live-lane sweeps could not run and
                       its recorded baseline was unreproducible.
  probe_kek.py         reported "the probe itself is wrong" about a real product behaviour,
                       and cited two lines that no longer existed.
  13 citations         `file.py:NNN` references to the four god files the split dissolved,
                       across six probes.

Every one of these is the same shape: **a tool that measures the tree, killed by a change to
the tree, silent because nothing runs it.** That shape has now appeared five times in this
repo -- coverage floors that stopped meaning anything, a skill asserting a closed bug was
live, 17 dead mypy suppressions, an empty Tier 0 table, and this. Each was fixed by adding a
ratchet, so this is the ratchet for scripts.

The three checks, and why these three
-------------------------------------
1. IMPORTABILITY. A script whose module body raises cannot run at all. Cheap, total, and it
   is what "the tool is dead" actually looks like from outside.
2. PATH LITERALS. Any string that looks like a repo file must resolve. This is the exact
   failure in coupling_report and sweep_admin.
3. CITATIONS. `file.py:NNN` must name a file that exists, and NNN must be within it. This
   catches probe_kek's class without pretending to check that the line still says what the
   prose claims -- see the limit below.

What this deliberately does NOT check
-------------------------------------
That a citation is *correct*. `_graph.py:516` stays syntactically perfect when line 516 moves
to 530, and no mechanical check can read the intent. This gate proves the reference is
resolvable, not true. The human half is the reason citations should name a SYMBOL as well as a
line, which is the convention the 2026-08-31 repair applied throughout.

Exit 0 fresh, 1 stale.
"""

from __future__ import annotations

import ast
import contextlib
import importlib.util
import io
import pathlib
import re
import sys

SCRIPTS = pathlib.Path("scripts")
SRC = pathlib.Path("src")

#: Literals that are examples in prose, not references. Kept tiny and explicit: an
#: allow-list is the thing this repo has repeatedly been bitten by, so anything added here
#: needs a reason that stays true.
PLACEHOLDERS = frozenset({"file.py"})

#: A line carrying this marker is exempt from the path checks. Deliberately LOCAL: the
#: exemption lives on the line it excuses, so it is visible in review and cannot rot
#: separately from the thing it applies to -- which is precisely how the coverage floors, the
#: mypy suppressions and the scope-guard list all went wrong. Two legitimate uses exist:
#: prose that quotes a path which used to exist, and a deliberate probe for the OLD shape of
#: a module so a script keeps working across a split.
EXEMPT_MARKER = "freshness-ok"

#: Scripts whose module body legitimately requires argv or environment, so an import-time
#: failure is correct behaviour rather than staleness. Derived by REASON, not by name: each
#: reads a required input at module scope. Verified 2026-08-31.
NEEDS_ARGS = frozenset(
    {
        "verify/sweep_admin.py",
        "verify/sweep_mcp.py",
        "verify/sweep_sdk.py",
        # The split tools are one-shot CLIs that take the target module as argv[1].
        "split/carry.py",
        "split/extract.py",
        "split/extract_mixin.py",
        "split/move_members.py",
        "split/surface.py",
        "split/wire.py",
    }
)


def _all_scripts() -> list[pathlib.Path]:
    return sorted(p for p in SCRIPTS.rglob("*.py") if p.name != "script_freshness.py")


def _file_index() -> dict[str, list[pathlib.Path]]:
    """Every .py in the tree, indexed by each of its path suffixes.

    So `_graph.py`, `sqlite/_graph.py` and `storage/sqlite/_graph.py` all resolve, and a
    basename that exists in five packages is reported as AMBIGUOUS rather than silently
    matching the first one -- an ambiguous citation is not much better than a dead one.
    """
    index: dict[str, list[pathlib.Path]] = {}
    for root in (SRC, pathlib.Path("tests"), SCRIPTS):
        for p in root.rglob("*.py"):
            parts = p.parts
            for i in range(len(parts)):
                index.setdefault("/".join(parts[i:]), []).append(p)
    return index


def check_importable() -> list[str]:
    problems: list[str] = []
    for p in _all_scripts():
        if str(p.relative_to(SCRIPTS)) in NEEDS_ARGS:
            continue
        spec = importlib.util.spec_from_file_location(f"_freshness_{p.stem}", p)
        if spec is None or spec.loader is None:  # pragma: no cover - defensive
            problems.append(f"{p}: could not build an import spec")
            continue
        module = importlib.util.module_from_spec(spec)
        # Registered in sys.modules BEFORE exec, because that is what real execution does.
        # Without it, `dataclasses` raises AttributeError from `_is_type` -- it looks up
        # `sys.modules[cls.__module__].__dict__` -- so every script defining a dataclass was
        # reported as broken. Two were, on this gate's first run: a false positive in the
        # freshness checker is exactly the failure mode it exists to catch elsewhere.
        sys.modules[spec.name] = module
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                spec.loader.exec_module(module)
        except SystemExit:
            pass  # argparse-style early exit is fine
        except Exception as exc:
            problems.append(f"{p}: {type(exc).__name__}: {exc}")
        finally:
            sys.modules.pop(spec.name, None)
    return problems


def check_path_literals() -> list[str]:
    """String literals naming a repo file, which must resolve.

    TWO shapes, because the first version caught only one and would therefore have MISSED the
    bug that motivated this gate. `sweep_admin.py` broke on a rooted literal
    (`"src/memotron/admin_server.py"`); `coupling_report.py` broke on a BARE one
    (`SRC / "client.py"`), which has no directory prefix at all. Proven by break-test: with
    only the rooted pattern, injecting a bare dead literal was reported FRESH.

    Bare literals are resolved against the whole-tree index rather than the filesystem, and
    flagged only when the name exists NOWHERE. That is what separates a dead reference from a
    file a script legitimately creates: `wire.py` writes `_protocol.py`, which resolves
    because such files exist; `"client.py"` resolves nowhere, because it was deleted.
    """
    rooted = re.compile(r"""['"]((?:src|tests|scripts|docs|\.helm)/[\w./-]+\.\w+)['"]""")
    bare = re.compile(r"""['"]([\w-]+\.py)['"]""")
    index = _file_index()
    problems: list[str] = []
    for p in _all_scripts():
        text = p.read_text()
        lines = text.splitlines()

        def exempt(offset: int, _text: str = text, _lines: list[str] = lines) -> bool:
            # Loop variables bound as defaults (B023): the closure is rebuilt per file, and
            # late binding would make every file check the LAST file's lines.
            n = _text[:offset].count("\n")
            return EXEMPT_MARKER in _lines[n] if n < len(_lines) else False

        for m in rooted.finditer(text):
            lit = m.group(1)
            if not pathlib.Path(lit).exists() and not exempt(m.start()):
                problems.append(f"{p}:{text[: m.start()].count(chr(10)) + 1}: path literal does not exist: {lit}")
        for m in bare.finditer(text):
            lit = m.group(1)
            if lit in PLACEHOLDERS or lit == "__init__.py" or exempt(m.start()):
                continue
            if lit not in index:
                line = text[: m.start()].count("\n") + 1
                problems.append(f"{p}:{line}: bare filename resolves nowhere in the tree: {lit}")
    return problems


def check_citations() -> tuple[list[str], list[str]]:
    pat = re.compile(r"\b([\w/]+\.py):(\d+)(?:-(\d+))?")
    index = _file_index()
    dead: list[str] = []
    ambiguous: list[str] = []
    for p in _all_scripts():
        text = p.read_text()
        for m in pat.finditer(text):
            rel = m.group(1)
            if rel in PLACEHOLDERS:
                continue
            end = int(m.group(3) or m.group(2))
            line = text[: m.start()].count("\n") + 1
            hits = index.get(rel, [])
            if not hits:
                dead.append(f"{p}:{line}: cites a file that does not exist: {m.group(0)}")
            elif len(hits) > 1:
                ambiguous.append(f"{p}:{line}: {m.group(0)} matches {len(hits)} files — cite a path, not a basename")
            elif end > len(hits[0].read_text().splitlines()):
                dead.append(f"{p}:{line}: {m.group(0)} is past the end of {hits[0]}")
    return dead, ambiguous


def check_self_consistency() -> list[str]:
    """NEEDS_ARGS must stay earned, not inherited.

    A script that gets fixed to not require argv at import should leave this set, or the
    exemption quietly grows into the allow-list rot this whole file is a reaction to.
    """
    problems: list[str] = []
    for name in sorted(NEEDS_ARGS):
        p = SCRIPTS / name
        if not p.exists():
            problems.append(f"NEEDS_ARGS names {name}, which does not exist — remove it")
            continue
        tree = ast.parse(p.read_text())
        reads_input = any(
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr in {"argv", "environ"}
            for node in ast.walk(ast.Module(body=tree.body, type_ignores=[]))
        )
        if not reads_input:
            problems.append(f"NEEDS_ARGS names {name}, but its module body no longer reads argv/environ — remove it")
    return problems


def main() -> int:
    if not SRC.exists():
        print("  run from the repo root")
        return 2

    print(f"\n  script freshness — {len(_all_scripts())} scripts under scripts/\n")
    total = 0

    for label, problems in (
        ("import", check_importable()),
        ("path literal", check_path_literals()),
        ("exemption", check_self_consistency()),
    ):
        if problems:
            print(f"  {len(problems)} {label} problem(s):")
            for line in problems:
                print(f"      {line}")
            total += len(problems)
        else:
            print(f"  ok  no {label} problems")

    dead, ambiguous = check_citations()
    if dead:
        print(f"  {len(dead)} dead citation(s):")
        for line in dead:
            print(f"      {line}")
        total += len(dead)
    else:
        print("  ok  every file.py:NNN citation resolves and is in range")
    if ambiguous:
        # Reported, not failed. An ambiguous basename is a readability problem; failing on it
        # would block on prose style rather than on correctness.
        print(f"  note  {len(ambiguous)} ambiguous citation(s) — resolvable but imprecise:")
        for line in ambiguous:
            print(f"      {line}")

    print()
    print("SCRIPT FRESHNESS: FRESH" if not total else f"SCRIPT FRESHNESS: STALE — {total} problem(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
