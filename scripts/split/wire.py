#!/usr/bin/env python3
"""Post-extraction wiring for a dreaming/ mixin.

    python wire.py <pkg_dir> <submodule> <MixinName> <ComposedProtocolName>

extract_mixin.py copies the parent's import block and declares composed state as
bare `X: Any` annotations. For dreaming/ that is not enough and not right:

  * `_log` no longer lives in __init__.py -- it moved to _common.py in e288d05, so
    a moved body referencing it takes an F821. Import it from there.
  * the bare `X: Any` block is exactly the blanket suppression the protocol exists
    to replace. Swap it for `_Base = ComposedDreamEngine` under TYPE_CHECKING and
    plain `object` at runtime, so the MRO is untouched.

Idempotent: running twice is a no-op.
"""

from __future__ import annotations

import pathlib
import re
import sys

pkg, submodule, mixin, proto = (sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4])
pkg_mod = ".".join(pathlib.Path(pkg).parts[pathlib.Path(pkg).parts.index("memotron") :])
path = pathlib.Path(pkg) / f"{submodule}.py"
s = path.read_text()

if proto in s:
    sys.exit(f"{path} already wired")

# Names the Shape B cut moved out of __init__.py. A moved body referencing one
# takes an F821, because extract_mixin copies the parent's IMPORT block and these
# are definitions, not imports. Resolve them against the four modules by reading
# what each actually defines, rather than keeping a list here that goes stale.
import ast

shape_b: dict[str, str] = {}
for p in sorted(pathlib.Path(pkg).glob("_*.py")):
    # `_*.py` MATCHES `__init__.py`. Leaving it in generates
    # `from pkg.__init__ import X`, which is a hard circular ImportError -- and
    # ruff and pure_move are both clean on it, so only running the package fails.
    # Same glob bug the models split hit; it recurred here in a different tool.
    if p.name in ("__init__.py", "_protocol.py", f"{submodule}.py"):
        continue
    text = p.read_text()
    if "_Base" in text:  # a mixin, not a Shape B value/helper module
        continue
    mod = p.stem
    for node in ast.parse(text).body:
        if isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            shape_b[node.name] = mod
        elif isinstance(node, ast.Assign):
            shape_b.update({t.id: mod for t in node.targets if isinstance(t, ast.Name)})
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            shape_b[node.target.id] = mod

referenced = {n.id for n in ast.walk(ast.parse(s)) if isinstance(n, ast.Name)}
bound = {
    a.asname or a.name.split(".")[0]
    for node in ast.walk(ast.parse(s))
    if isinstance(node, ast.Import | ast.ImportFrom)
    for a in node.names
}
wanted: dict[str, list[str]] = {}
for name in sorted(referenced - bound):
    if name in shape_b:
        wanted.setdefault(shape_b[name], []).append(name)

imports = "".join(f"from {pkg_mod}.{mod} import {', '.join(names)}\n" for mod, names in sorted(wanted.items()))
imports += f"from {pkg_mod}._protocol import {proto}\n"

# Anchor on the first first-party import so isort has the least to do; fall back
# to the __future__ line for a mixin that imports nothing from memotron.
m = re.search(rf"^from {re.escape(pkg_mod.split('.')[0])}\.", s, re.MULTILINE)
if m:
    s = s[: m.start()] + imports + s[m.start() :]
else:
    s = s.replace("from __future__ import annotations\n", f"from __future__ import annotations\n\n{imports}", 1)

if "from typing import TYPE_CHECKING" not in s:
    s = s.replace(
        "from __future__ import annotations\n",
        "from __future__ import annotations\n\nfrom typing import TYPE_CHECKING\n",
        1,
    )

base = (
    "if TYPE_CHECKING:\n"
    f"    _Base = {proto}\n"
    "else:\n"
    "    # Plain object at runtime, so the composed class's MRO is unchanged.\n"
    "    _Base = object\n"
    "\n"
    "\n"
    f"class {mixin}(_Base):\n"
)
if f"class {mixin}:\n" not in s:
    sys.exit(f"could not find `class {mixin}:` in {path}")
s = s.replace(f"class {mixin}:\n", base, 1)

# Drop the bare composed-state annotations the extractor emitted; the Protocol
# declares them with real types now.
s = re.sub(r"\n(?:    _[a-z_]+: Any\n)+", "\n", s, count=1)

path.write_text(s)
print(f"  wired {path.name}: shape-B imports={wanted or None}")
