#!/usr/bin/env python3
"""Move a named set of top-level definitions out of a package __init__ into a submodule.

    python extract.py <package_dir> <submodule> "<docstring>" Name1 Name2 ...

Moves each definition VERBATIM (including the comment block immediately above it),
deletes it from __init__.py, and adds a redundant-alias re-export so the old import
path keeps resolving. Never reformats, never reorders within a block -- pure_move.py
has to keep passing.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

pkg = pathlib.Path(sys.argv[1])
submodule = sys.argv[2]
docstring = sys.argv[3]
wanted = sys.argv[4:]

init = pkg / "__init__.py"
src = init.read_text()
lines = src.splitlines(keepends=True)
tree = ast.parse(src)

defs: dict[str, ast.stmt] = {}
for stmt in tree.body:
    if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
        defs[stmt.name] = stmt
    elif isinstance(stmt, ast.Assign):
        for t in stmt.targets:
            if isinstance(t, ast.Name):
                defs[t.id] = stmt
    elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
        defs[stmt.target.id] = stmt

missing = [n for n in wanted if n not in defs]
if missing:
    sys.exit(f"not found in {init}: {', '.join(missing)}")


def span(node: ast.stmt) -> tuple[int, int]:
    """1-indexed inclusive line range, absorbing any comment block directly above."""
    start = node.lineno
    for dec in getattr(node, "decorator_list", []):
        start = min(start, dec.lineno)
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno


spans = sorted(span(defs[n]) for n in wanted)
moved = "".join("".join(lines[a - 1 : b]).rstrip("\n") + "\n\n\n" for a, b in spans).rstrip("\n") + "\n"

# Imports the new file needs. Two sources, and forgetting the second is what broke
# the first attempt at this: ruff reported F821 x4, mypy agreed, and 531 tests failed.
#   1. the parent's own third-party/stdlib imports (ruff --fix prunes the unused)
#   2. anything the moved code references that already lives in a SIBLING submodule
pkg_mod = ".".join(pkg.parts[pkg.parts.index("memotron") :])
# AST spans, not a line regex: config.py has multi-line `from X import (\n...)`
# blocks, and matching only the opening line emitted an unclosed paren --
# "invalid-syntax: Expected one or more symbol names after import". models.py had
# no multi-line imports, so a line-based filter survived that whole split by luck.
_import_chunks = []
for stmt in tree.body:
    if not isinstance(stmt, ast.Import | ast.ImportFrom):
        continue
    text = "".join(lines[stmt.lineno - 1 : stmt.end_lineno])
    if f"{pkg_mod}._" in text:  # a sibling re-export block, not a real import
        continue
    _import_chunks.append(text)
parent_imports = "".join(_import_chunks)

moved_tree = ast.parse(moved)
free_names = {n.id for n in ast.walk(moved_tree) if isinstance(n, ast.Name)} | {
    a.attr for a in ast.walk(moved_tree) if isinstance(a, ast.Attribute)
}
# annotations are strings under `from __future__ import annotations`; parse them too
for node in ast.walk(moved_tree):
    ann = getattr(node, "annotation", None) or getattr(node, "returns", None)
    if isinstance(ann, ast.Name):
        free_names.add(ann.id)
    elif ann is not None:
        free_names |= {n.id for n in ast.walk(ann) if isinstance(n, ast.Name)}

sibling_imports = ""
for sib in sorted(pkg.glob("_*.py")):
    if sib.name == "__init__.py":  # a dunder, not a sibling submodule
        continue
    # Classes, functions AND module-level constants. Omitting constants is what
    # broke the _instructions extraction: it referenced
    # RELATIONSHIP_TYPE_MEMORY_TYPE_MAP and DEFAULT_MAX_PREDICATE_WORDS, both
    # already living in siblings, and got two F821s.
    sib_defs: set[str] = set()
    for s2 in ast.parse(sib.read_text()).body:
        if isinstance(s2, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            sib_defs.add(s2.name)
        elif isinstance(s2, ast.Assign):
            sib_defs.update(t.id for t in s2.targets if isinstance(t, ast.Name))
        elif isinstance(s2, ast.AnnAssign) and isinstance(s2.target, ast.Name):
            sib_defs.add(s2.target.id)
    needed = sorted(sib_defs & free_names)
    if needed:
        sibling_imports += f"from {pkg_mod}.{sib.stem} import (\n" + "".join(f"    {n},\n" for n in needed) + ")\n"

header = f'"""{docstring}"""\n\nfrom __future__ import annotations\n\n{parent_imports}{sibling_imports}\n'
target = pkg / f"{submodule}.py"
target.write_text(header + "\n" + moved)

keep = list(lines)
for a, b in sorted(spans, reverse=True):
    del keep[a - 1 : b]
init.write_text("".join(keep))

# Re-export in redundant-alias form: required because this package is in the mypy
# strict tier, where no_implicit_reexport is on.
pkg_mod = ".".join(pkg.parts[pkg.parts.index("memotron") :])
block = f"\nfrom {pkg_mod}.{submodule} import (\n" + "".join(f"    {n} as {n},\n" for n in sorted(wanted)) + ")\n"
s = init.read_text()
anchor = re.search(r"^(from|import) [^\n]*\n(?:(?:from|import) [^\n]*\n|\s*\n|[^\n]*,\n|\)\n)*", s, re.MULTILINE)
insert_at = anchor.end() if anchor else 0
init.write_text(s[:insert_at] + block + s[insert_at:])

subprocess.run(["uvx", "ruff", "check", str(pkg), "--fix", "-q"], check=False)
print(
    f"{submodule}.py <- {len(wanted)} definitions, {sum(b - a + 1 for a, b in spans)} lines; __init__ now {len(keep)}"
)
