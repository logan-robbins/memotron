#!/usr/bin/env python3
"""Did the PARSED STRUCTURE of any module change between two refs?

    uv run python scripts/verify/ast_identity.py HEAD .
    uv run python scripts/verify/ast_identity.py <ref> <ref>

This is the gate for a **format-only** commit, and it exists because `pure_move.py`
cannot be one.

Why pure_move cannot verify a reformat
--------------------------------------
`pure_move` compares compiled bytecode, which is the stronger check for a move and the
wrong check for a reformat. CPython's compiler lays out COMPREHENSION jumps differently
depending on how the source is spread across lines, so collapsing

    sorted(
        x.lower()
        for x in xs
        if isinstance(x, str)
    )

onto one line emits a different (equivalent) branch -- `POP_JUMP_IF_FALSE +18` becomes
`POP_JUMP_IF_TRUE +1; JUMP_BACKWARD 15`. Measured 2026-08-29 on the repo-wide
`ruff format` pass: 28 functions reported as "body changed", and **all 41 of the
function definitions behind those reports had byte-identical ASTs**. Reduced to a
six-line reproducer: same `ast.dump`, different nested `co_code`, purely from line
layout. So those 28 were false positives, and no amount of re-running pure_move would
have said so.

What this checks instead
------------------------
`ast.dump()` with positions excluded. Two modules that produce the same dump differ
only in whitespace, line breaks and comments -- none of which a Python program can
observe. That makes AST identity the *correct* strength for a formatter: strictly
weaker than bytecode identity, and strictly stronger than "the tests still pass".

It is deliberately position-insensitive, which is the whole point, so state the two
things it therefore cannot see:

  * **Comments and blank lines.** Deleting every comment in the repo passes this gate.
    Review the diff for that; no gate here covers it.
  * **Docstring CONTENT is compared** (it is a `Constant` node), so a formatter that
    reindents or strips a docstring DOES show up. That is intentional -- docstrings
    are `__doc__` at runtime and this repo ships them on a public SDK.

Exit codes: 0 identical · 1 a module's structure changed · 2 the refs could not be read.
"""

from __future__ import annotations

import argparse
import ast
import subprocess
import sys
from pathlib import Path

#: Only the shipped package by default. tests/ and scripts/ are reformatted too, but a
#: structural change there is a test change, which review covers and this gate would
#: only make noisy.
DEFAULT_ROOTS = ("src/memotron",)


def _tracked(ref: str, roots: tuple[str, ...]) -> list[str]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, *roots],
        capture_output=True,
        text=True,
        check=False,
    )
    return [p for p in out.stdout.splitlines() if p.endswith(".py")]


def _read(ref: str, path: str) -> str | None:
    if ref == ".":
        p = Path(path)
        return p.read_text(encoding="utf-8") if p.exists() else None
    out = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True, check=False)
    return out.stdout if out.returncode == 0 else None


def _dump(src: str, path: str) -> str | None:
    try:
        return ast.dump(ast.parse(src, filename=path))
    except SyntaxError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("before", help="git ref")
    ap.add_argument("after", nargs="?", default=".", help="git ref, or '.' for the working tree")
    ap.add_argument("--roots", nargs="*", default=list(DEFAULT_ROOTS))
    args = ap.parse_args(argv)

    roots = tuple(args.roots)
    before_files = _tracked(args.before, roots)
    if not before_files:
        print(f"  could not list {args.before}:{'/'.join(roots)} -- is the ref valid?")
        return 2

    after_files = set(_tracked("HEAD", roots)) if args.after == "." else set(_tracked(args.after, roots))
    if args.after == ".":
        after_files |= {str(p) for r in roots for p in Path(r).rglob("*.py")}

    changed: list[str] = []
    unparsable: list[str] = []
    added = sorted(after_files - set(before_files))
    removed: list[str] = []
    compared = 0

    for path in sorted(before_files):
        old_src = _read(args.before, path)
        new_src = _read(args.after, path)
        if old_src is None:
            unparsable.append(f"{path} (unreadable at {args.before})")
            continue
        if new_src is None:
            removed.append(path)
            continue
        old, new = _dump(old_src, path), _dump(new_src, path)
        if old is None or new is None:
            unparsable.append(f"{path} (syntax error)")
            continue
        compared += 1
        if old != new:
            changed.append(path)

    print(f"ast_identity: {args.before} -> {args.after}")
    print(f"  compared {compared} module(s) in {', '.join(roots)}")

    # A file that moved is not a structural change to the code inside it, and this gate
    # is per-path, so say so rather than letting a rename read as clean.
    if added or removed:
        print(f"  note: {len(added)} added, {len(removed)} removed/renamed -- not compared")

    if unparsable:
        print(f"  SKIPPED  {len(unparsable)} file(s) could not be parsed on both sides:")
        for u in unparsable[:10]:
            print(f"           {u}")

    if changed:
        print(f"  FAIL     {len(changed)} module(s) changed structurally:")
        for c in changed[:40]:
            print(f"           {c}")
        if len(changed) > 40:
            print(f"           ... and {len(changed) - 40} more")
        print()
        print("AST IDENTITY: FAIL -- this is more than a reformat.")
        return 1

    if compared == 0:
        print("  FAIL     nothing was compared. That is not a pass.")
        return 1

    print()
    print("AST IDENTITY: PASS -- every module parses to the same tree; the diff is whitespace only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
