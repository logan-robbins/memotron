#!/usr/bin/env python3
"""Resolve every unbound module-level name a freshly-extracted mixin still reads.

    python carry.py <pkg_dir>

extract_mixin's `--carry` handles this at cut time, but only if you already knew
which constants a body reads -- and you do not, until pure_move tells you. Running
it after the fact turns three reverts into one pass.

For each unbound name, one of two outcomes:
  * a SIBLING module already defines it  -> add an import (a constant that two
    concerns share belongs in one place, and moving it twice would duplicate it);
  * only `__init__.py` defines it        -> MOVE it into the mixin and re-export
    from `__init__.py`, so `from pkg import NAME` keeps working.

A class-qualified self-reference (`TheComposedClass.method`) is reported and NOT
touched -- that is a body edit, it costs pure_move on that function, and it deserves
a human deciding it rather than a script doing it quietly.
"""

from __future__ import annotations

import ast
import pathlib
import subprocess
import sys

pkg = pathlib.Path(sys.argv[1])
pkg_mod = ".".join(pkg.parts[pkg.parts.index("memotron") :])
init = pkg / "__init__.py"


def module_level(path: pathlib.Path) -> dict[str, ast.stmt]:
    out: dict[str, ast.stmt] = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            out.update({t.id: node for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = node
        elif isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            out[node.name] = node
    return out


# Where every module-level name in the package currently lives.
home: dict[str, str] = {}
for path in sorted(pkg.glob("*.py")):
    if path.name == "__init__.py":
        continue
    for name in module_level(path):
        home.setdefault(name, path.stem)

init_defs = module_level(init)
init_src = init.read_text()
init_lines = init_src.splitlines(keepends=True)

# pure_move is the oracle for "what is actually unbound", because it resolves against
# the compiled body rather than guessing from the source text.
report = subprocess.run(
    ["uv", "run", "--no-sync", "python", "scripts/verify/pure_move.py", "HEAD", "."],
    capture_output=True,
    text=True,
).stdout
unbound: dict[str, set[str]] = {}
for line in report.splitlines():
    if "reads unbound global" not in line:
        continue
    qual, name = line.split(" reads unbound global ")
    mod = qual.strip().split(".")[2].split(":")[0] if qual.strip().startswith(pkg_mod) else None
    mod = qual.strip().removeprefix(pkg_mod + ".").split(".")[0]
    unbound.setdefault(mod, set()).add(name.strip().strip("'"))

drop: set[int] = set()
for mod, names in sorted(unbound.items()):
    target = pkg / f"{mod}.py"
    if not target.exists():
        print(f"  ?? {mod}: no such module")
        continue
    text = target.read_text()
    composed = next((n.name for n in ast.parse(init.read_text()).body if isinstance(n, ast.ClassDef) and n.bases), None)
    from_sibling: dict[str, list[str]] = {}
    carried: list[str] = []
    for name in sorted(names):
        if name == composed:
            print(f"  MANUAL {mod}: reads `{name}` -- class-qualified, needs a body edit")
            continue
        if name in home:
            from_sibling.setdefault(home[name], []).append(name)
        elif name in init_defs:
            node = init_defs[name]
            drop.update(range(node.lineno, node.end_lineno + 1))
            carried.append(name)
        else:
            print(f"  ?? {mod}: `{name}` is defined nowhere in the package")

    block = "".join(
        f"from {pkg_mod}.{sib} import {', '.join(sorted(ns))}\n" for sib, ns in sorted(from_sibling.items())
    )
    body = "".join("".join(init_lines[init_defs[n].lineno - 1 : init_defs[n].end_lineno]) for n in carried)
    anchor = "if TYPE_CHECKING:\n    _Base ="
    assert text.count(anchor) == 1, target
    text = text.replace(anchor, block + ("\n" + body if body else "") + "\n" + anchor, 1)
    target.write_text(text)
    if carried or from_sibling:
        print(f"  {mod}: carried {carried or '-'}, imported {dict(from_sibling) or '-'}")

# Delete the carried definitions from __init__ and re-export them from their new home.
if drop:
    kept = [ln for i, ln in enumerate(init_lines, 1) if i not in drop]
    out = "".join(kept)
    for mod, names in sorted(unbound.items()):
        carried = [n for n in sorted(names) if n in init_defs and n not in home]
        if not carried:
            continue
        marker = f"from {pkg_mod}.{mod} import "
        idx = out.index(marker)
        end = out.index("\n", idx)
        existing = out[idx + len(marker) : end]
        aliases = "".join(f"    {n} as {n},\n" for n in carried)
        out = out[:idx] + f"{marker}(\n{aliases}    {existing},\n)" + out[end:]
    init.write_text(out)
    print(f"  __init__: removed {len(drop)} carried line(s), re-exported them")
