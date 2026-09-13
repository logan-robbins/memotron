#!/usr/bin/env python3
"""Does the documentation name symbols that actually exist?

    uv run python scripts/verify/doc_symbols.py

Why this exists
---------------
An independent reviewer of PR #103 found that `README.md`'s own quickstart could not run:

    from memotron import ThematicConsolidationPolicy   # ImportError

The class does not exist anywhere in `src/`. It was renamed to
``RollupConsolidationPolicy``, and the README kept the old name in five places -- plus a
keyword argument, ``thematic_consolidation_policy=``, that ``DreamJob`` does not accept
either. So the snippet was wrong twice and nothing noticed, in a repo whose stated purpose
is "a codebase a team can work on".

Same shape as the five dead scripts, the stale skill, the dead mypy suppressions and the
empty Tier 0 table: **a claim about the tree, invalidated by a change to the tree, silent
because nothing re-derives it.** This is the ratchet for documentation.

What it checks
--------------
1. IMPORTS. Every ``from memotron... import X`` in a fenced Python block resolves.
2. KEYWORDS. Every ``Name(kwarg=...)`` call in a fenced block, where ``Name`` is an
   importable pydantic model, uses fields that model actually declares. This is the half
   that would have caught the second error; an import check alone would not have.

What it deliberately does NOT check
-----------------------------------
That the snippet is *correct* -- that it does what the prose says, or that its values are
sensible. A snippet can construct cleanly and still teach the wrong thing. This proves the
names are real, which is the mechanical half.

Exit 0 fresh, 1 stale.
"""

from __future__ import annotations

import ast
import importlib
import pathlib
import re

DOCS = [
    pathlib.Path("README.md"),
    # A reference page is a claim about the tree like any other. Its Python examples
    # are checked here so an import or a pydantic kwarg cannot rot silently -- which
    # is the exact failure this gate was written for. Prose and file:line citations
    # in that doc are still unchecked; it says so in its own header.
    pathlib.Path("docs/data-model-and-tenancy.md"),
]

#: Fenced blocks tagged as Python. Anything else (bash, json, text) is ignored -- a shell
#: transcript naming a class is prose, not a claim the interpreter can check.
FENCE = re.compile(r"```(?:python|py)\n(.*?)```", re.DOTALL)


def _blocks(text: str) -> list[tuple[int, str]]:
    out = []
    for m in FENCE.finditer(text):
        out.append((text[: m.start()].count("\n") + 1, m.group(1)))
    return out


def _check_block(path: pathlib.Path, line0: int, src: str) -> list[str]:
    problems: list[str] = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        # A fragment (no imports, elided body) is normal in docs and is not a finding.
        return problems

    imported: dict[str, str] = {}

    for node in ast.walk(tree):
        # --- 1. imports resolve -------------------------------------------------
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("memotron"):
            try:
                module = importlib.import_module(node.module)
            except Exception as exc:
                problems.append(f"{path}:{line0 + node.lineno}: cannot import {node.module!r}: {exc}")
                continue
            for alias in node.names:
                if not hasattr(module, alias.name):
                    problems.append(f"{path}:{line0 + node.lineno}: {node.module}.{alias.name} does not exist")
                else:
                    imported[alias.asname or alias.name] = f"{node.module}.{alias.name}"

    # --- 2. keyword arguments exist on the model ---------------------------------
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        target = imported.get(node.func.id)
        if target is None:
            continue
        module_name, _, attr = target.rpartition(".")
        try:
            obj = getattr(importlib.import_module(module_name), attr)
            fields = getattr(obj, "model_fields", None)
        except Exception:
            continue
        if not fields:
            continue
        for kw in node.keywords:
            if kw.arg and kw.arg not in fields:
                problems.append(
                    f"{path}:{line0 + node.lineno}: {attr}(...) has no field {kw.arg!r} "
                    f"(it declares {sorted(fields)[:6]}…)"
                )
    return problems


def main() -> int:
    if not pathlib.Path("src/memotron").exists():
        print("  run from the repo root")
        return 2

    problems: list[str] = []
    blocks = 0
    for doc in DOCS:
        if not doc.exists():
            problems.append(f"{doc}: listed for checking but does not exist")
            continue
        for line0, src in _blocks(doc.read_text()):
            blocks += 1
            problems.extend(_check_block(doc, line0, src))

    print(f"\n  doc symbols — {blocks} python block(s) across {len(DOCS)} file(s)\n")
    if problems:
        for p in problems:
            print(f"      {p}")
        print(f"\nDOC SYMBOLS: STALE — {len(problems)} problem(s)")
        return 1
    print("  ok  every documented import resolves and every keyword argument exists")
    print("\nDOC SYMBOLS: FRESH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
