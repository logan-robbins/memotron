#!/usr/bin/env python3
"""Move methods out of a god-class into a mixin submodule (Shape A).

    python extract_mixin.py <pkg_dir> <Class> <submodule> <MixinName> "<doc>" m1 m2 ...

Verbatim: each method keeps its own indentation, so the block that was at method
level in the class is at method level in the mixin. pure_move.py has to keep
passing, which means no reformatting, no reordering, no signature touch-ups.

Follows storage/postgres/: concern mixins in private `_*.py`, composed by plain
multiple inheritance in __init__.py, composed state declared as bare class-level
annotations, and any helper used by more than one mixin left on the composed
class rather than duplicated.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

pkg = pathlib.Path(sys.argv[1])
class_name, submodule, mixin_name, docstring = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
raw = sys.argv[6:]
# Module-level definitions the moved methods reference. Without these the mixin
# takes F821s: sqlite's _graph methods use ScopeStateHashTracker (a module-level
# class) and _CONTEXT_VISIBLE_SELECT (a module-level SQL constant), and neither is
# an import, so copying the parent's import block does not bring them.
carry: list[str] = []
if "--carry" in raw:
    i = raw.index("--carry")
    carry = raw[i + 1].split(",")
    raw = raw[:i] + raw[i + 2 :]
wanted = raw

init = pkg / "__init__.py"
src = init.read_text()
lines = src.splitlines(keepends=True)
tree = ast.parse(src)

cls = next(s for s in tree.body if isinstance(s, ast.ClassDef) and s.name == class_name)
# Not just methods. DreamEngine also carries nested dataclasses
# (_EntityCandidate, _MentionLinkContext -- the latter constructed directly by
# tests/test_entity_resolution.py:895) and class-level constants, and a mixin that
# takes the methods without them leaves an F821 behind.
members: dict[str, ast.stmt] = {}
for m in cls.body:
    if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        members[m.name] = m
    elif isinstance(m, ast.Assign):
        members.update({t.id: m for t in m.targets if isinstance(t, ast.Name)})
    elif isinstance(m, ast.AnnAssign) and isinstance(m.target, ast.Name):
        members[m.target.id] = m
methods = {n: m for n, m in members.items() if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)}
missing = [n for n in wanted if n not in members]
if missing:
    sys.exit(f"not members of {class_name}: {', '.join(missing)}")


def span(node: ast.stmt) -> tuple[int, int]:
    start = node.lineno
    for dec in getattr(node, "decorator_list", []):
        start = min(start, dec.lineno)
    while start > 1 and lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno


spans = sorted(span(members[n]) for n in wanted)
moved = "".join("".join(lines[a - 1 : b]).rstrip("\n") + "\n\n" for a, b in spans).rstrip("\n") + "\n"

# ---- what state does the moved code touch that the COMPOSER owns? ------------
# Instance attributes assigned in __init__, e.g. `self._connection = ...`. These
# are declared on the mixin as bare annotations -- the storage/postgres convention
# (see _graph.py: `_engine: Any`).
init_m = methods.get("__init__")
owned_attrs: set[str] = set()
if init_m is not None:
    for node in ast.walk(init_m):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            owned_attrs.add(node.attr)
# `moved` is method source at class indentation, so it cannot be parsed alone --
# wrap it in a throwaway class first.
touched = {
    n.attr
    for n in ast.walk(ast.parse("class _T:\n" + moved))
    if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "self"
}
state = sorted(owned_attrs & touched)

# ---- imports: parent's, as AST spans (never a line regex) --------------------
pkg_mod = ".".join(pkg.parts[pkg.parts.index("memotron") :])
import_chunks = [
    "".join(lines[s.lineno - 1 : s.end_lineno])
    for s in tree.body
    if isinstance(s, ast.Import | ast.ImportFrom) and f"{pkg_mod}._" not in "".join(lines[s.lineno - 1 : s.end_lineno])
]

body = f'"""{docstring}"""\n\nfrom __future__ import annotations\n\n'
body += "".join(import_chunks)
body += f"\n\nclass {mixin_name}:\n"
body += f'    """Composed into :class:`{class_name}`."""\n\n'
if state:
    body += "    # Provided by the composing backend.\n"
    body += "".join(f"    {a}: Any\n" for a in state) + "\n"
carried_src = ""
carried_spans: list[tuple[int, int]] = []
if carry:
    top = {}
    for st in tree.body:
        if isinstance(st, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            top[st.name] = st
        elif isinstance(st, ast.Assign):
            for t in st.targets:
                if isinstance(t, ast.Name):
                    top[t.id] = st
        elif isinstance(st, ast.AnnAssign) and isinstance(st.target, ast.Name):
            top[st.target.id] = st
    absent = [c for c in carry if c not in top]
    if absent:
        sys.exit(f"--carry names not at module level: {', '.join(absent)}")
    carried_spans = sorted(span(top[c]) for c in carry)
    carried_src = "".join("".join(lines[a - 1 : b]).rstrip("\n") + "\n\n\n" for a, b in carried_spans)

# carried module-level defs go ABOVE the mixin class, which needs them at import time
head, _, tail = body.partition(f"\n\nclass {mixin_name}:\n")
body = head + "\n\n" + carried_src + f"\nclass {mixin_name}:\n" + tail
body += moved
(pkg / f"{submodule}.py").write_text(body)

# ---- remove from the class, add the mixin to its bases -----------------------
keep = list(lines)
for a, b in sorted([*spans, *carried_spans], reverse=True):
    del keep[a - 1 : b]
out = "".join(keep)

base_pat = re.compile(rf"^class {class_name}\(", re.MULTILINE)
m = base_pat.search(out)
if m:
    out = out[: m.end()] + f"\n    {mixin_name}," + out[m.end() :]
else:  # `class X:` with no bases yet
    out = re.sub(
        rf"^class {class_name}:", f"class {class_name}(\n    {mixin_name},\n):", out, count=1, flags=re.MULTILINE
    )
# Carried module-level names MUST be re-exported. They leave the package
# surface otherwise, and nothing catches it: the API golden cannot see names
# provided by a __getattr__ shim, and an F401 sweep never lists them because
# they were never imports. Cost me two silent breaks (ScopeStateHashTracker,
# LLM_CREDENTIAL_SUBJECT_KEY) and one loud one (DREAM_CLAIM_STALE_SECONDS).
if carry:
    imp = (
        f"from {pkg_mod}.{submodule} import (\n"
        + "".join(f"    {c} as {c},\n" for c in sorted(carry))
        + f"    {mixin_name},\n)\n"
    )
else:
    imp = f"from {pkg_mod}.{submodule} import {mixin_name}\n"
# Insert after the LAST top-level import, located by AST. A regex anchor put the
# import inside a module docstring here -- prose that happens to begin with "from"
# at column 0 is indistinguishable from an import to a line matcher.
out_tree = ast.parse(out)
out_lines = out.splitlines(keepends=True)
last_import_end = 0
for st in out_tree.body:
    if isinstance(st, ast.Import | ast.ImportFrom):
        last_import_end = max(last_import_end, st.end_lineno)
if last_import_end:
    out = "".join(out_lines[:last_import_end]) + imp + "".join(out_lines[last_import_end:])
else:
    out = imp + out
init.write_text(out)

subprocess.run(["uvx", "ruff", "check", str(pkg), "--fix", "-q"], check=False)
print(
    f"{submodule}.py <- {mixin_name}: {len(wanted)} methods, "
    f"{sum(b - a + 1 for a, b in spans)} lines; state={state or 'none'}; __init__ now {len(keep)}"
)
