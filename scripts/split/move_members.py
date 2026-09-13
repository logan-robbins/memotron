#!/usr/bin/env python3
"""Move methods between two already-split mixin files, or onto the composer.

    python move_members.py <pkg> <src_stem> <dst_stem> [--mixin NewMixinName --doc "..."] m1 m2 ...

`extract_mixin.py` cuts a god-class in `__init__.py` into a new mixin. This is the
follow-up operation: the split already happened and a member is in the wrong file.
Which is not a hypothetical -- `mixin_dag.py` found seven of dreaming's ten mixins
in one strongly-connected component, caused by six members sitting one file away
from where they belong.

Verbatim by construction. Each member keeps its own indentation and its preceding
comment block, so the bytes that were at method level in the source class are at
method level in the destination. `pure_move.py` has to keep passing.

Destination may be:
  * an existing mixin file        -- members are appended to its single class
  * `__init__` (the composer)     -- members are appended to the composed class,
                                     which is where R-A2 says a helper used by two
                                     or more mixins belongs
  * a new file, with --mixin/--doc -- creates it and wires the base into the composer
"""

from __future__ import annotations

import ast
import itertools
import pathlib
import re
import sys

argv = sys.argv[1:]
mixin_name = doc = None
if "--mixin" in argv:
    i = argv.index("--mixin")
    mixin_name = argv[i + 1]
    argv = argv[:i] + argv[i + 2 :]
if "--doc" in argv:
    i = argv.index("--doc")
    doc = argv[i + 1]
    argv = argv[:i] + argv[i + 2 :]

pkg = pathlib.Path(argv[0])
src_stem, dst_stem, wanted = argv[1], argv[2], argv[3:]
src_path = pkg / f"{src_stem}.py"
dst_path = pkg / f"{dst_stem}.py"

src_text = src_path.read_text()
src_lines = src_text.splitlines(keepends=True)
src_tree = ast.parse(src_text)

cls = next(n for n in src_tree.body if isinstance(n, ast.ClassDef))
members = {m.name: m for m in cls.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)}
missing = [n for n in wanted if n not in members]
if missing:
    sys.exit(f"not members of {cls.name}: {', '.join(missing)}")


def span(node: ast.stmt) -> tuple[int, int]:
    start = node.lineno
    for dec in getattr(node, "decorator_list", []):
        start = min(start, dec.lineno)
    while start > 1 and src_lines[start - 2].lstrip().startswith("#"):
        start -= 1
    return start, node.end_lineno


spans = sorted(span(members[n]) for n in wanted)
gaps = [(a, b) for (_, b), (a, _) in itertools.pairwise(spans) if a - b > 1]
print(
    f"  moving {len(wanted)} member(s), {sum(b - a + 1 for a, b in spans)} lines"
    f"{' -- CONTIGUOUS' if not gaps else f' -- {len(gaps)} gap(s), not one block'}"
)

moved = "".join("".join(src_lines[a - 1 : b]).rstrip("\n") + "\n\n" for a, b in spans).rstrip("\n") + "\n"

# ---- remove from the source -------------------------------------------------
keep = list(src_lines)
for a, b in sorted(spans, reverse=True):
    del keep[a - 1 : b]
src_path.write_text("".join(keep))

# ---- append to the destination ----------------------------------------------
if dst_path.exists():
    dst_text = dst_path.read_text()
    dst_cls = next(
        n for n in ast.parse(dst_text).body if isinstance(n, ast.ClassDef) and not n.name.startswith("_Composed")
    )
    dst_text = dst_text.rstrip("\n") + "\n\n" + moved
    dst_path.write_text(dst_text)
    print(f"  appended to {dst_path.name}::{dst_cls.name}")
else:
    if not (mixin_name and doc):
        sys.exit("new destination needs --mixin and --doc")
    # the source file's import header, verbatim -- same rule extract_mixin follows
    header = "".join(
        "".join(src_lines[s.lineno - 1 : s.end_lineno])
        for s in src_tree.body
        if isinstance(s, ast.Import | ast.ImportFrom)
    )
    body = (
        f'"""{doc}"""\n\nfrom __future__ import annotations\n\n'
        + header
        + "\n\nif TYPE_CHECKING:\n    _Base = ComposedDreamEngine\nelse:\n"
        + "    # Plain object at runtime, so DreamEngine's MRO is unchanged.\n"
        + "    _Base = object\n\n\n"
        + f"class {mixin_name}(_Base):\n"
        + '    """Composed into :class:`DreamEngine`."""\n\n'
        + moved
    )
    dst_path.write_text(body)
    init = pkg / "__init__.py"
    text = init.read_text()
    text = text.replace(
        f"from memotron.dreaming.{src_stem} import",
        f"from memotron.dreaming.{dst_stem} import {mixin_name}\nfrom memotron.dreaming.{src_stem} import",
        1,
    )
    m = re.search(r"^class DreamEngine\(\n?", text, re.MULTILINE)
    if not m:
        sys.exit("could not find `class DreamEngine(` in __init__.py")
    text = text[: m.end()] + f"    {mixin_name},\n" + text[m.end() :]
    init.write_text(text)
    print(f"  created {dst_path.name}::{mixin_name} and added it to DreamEngine's bases")
