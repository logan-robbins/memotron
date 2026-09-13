#!/usr/bin/env python3
"""Drop imports an extraction left dead in dreaming/__init__.py -- but only the
ones nothing imports THROUGH that path.

An extraction takes a method out of the class, and the import it needed goes unused
in the parent. Deleting it is usually right and occasionally wrong: `memotron`
is a published SDK, and a name that is only ever imported rather than defined here
is still importable from here today.

So each candidate gets a three-way check before it goes:
  1. ruff says F401 (unused inside the module)
  2. nothing does `from memotron.dreaming import <name>`
  3. nothing does `dreaming.<name>`
Anything that fails 2 or 3 is KEPT and reported. This is the check that was missing
when ScopeStateHashTracker, DREAM_CLAIM_STALE_SECONDS and LLM_CREDENTIAL_SUBJECT_KEY
left the sqlite surface and broke admin_server and test_embedding_transport.

Note what it does NOT catch: consumers outside this repo. The API-surface golden is
the backstop -- every removal shows up there and has to be blessed deliberately.

AST-driven, because the import forms vary: bare `import re`, single-name
`from x import y`, multi-name, and parenthesised multi-line. String replacement
handles the first three and silently corrupts the fourth.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
import sys

target = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "src/memotron/dreaming/__init__.py")
pkg = ".".join(target.parts[target.parts.index("memotron") : -1])

ruff = subprocess.run(
    ["uvx", "ruff", "check", str(target), "--output-format", "concise"],
    capture_output=True,
    text=True,
)
candidates = sorted(
    {
        m.group(1).rsplit(".", 1)[-1]
        for line in ruff.stdout.splitlines()
        if "F401" in line
        for m in [re.search(r"`([\w.]+)` imported but unused", line)]
        if m
    }
)
if not candidates:
    print("  no F401 candidates")
    raise SystemExit(0)

TREES = ["src", "tests", "scripts", "examples", "demo", "ingest"]
grep = subprocess.run(
    ["grep", "-rn", "--include=*.py", "-E", f"from {pkg} import|{pkg.rsplit('.', 1)[-1]}\\.[A-Za-z_]+", *TREES],
    capture_output=True,
    text=True,
)
external = [line for line in grep.stdout.splitlines() if f"/{pkg.rsplit('.', 1)[-1]}/" not in line]

dead, kept = [], []
for name in candidates:
    hits = [ln for ln in external if re.search(rf"\b{re.escape(name)}\b", ln)]
    (kept if hits else dead).append((name, hits))

for name, hits in kept:
    print(f"  KEEP {name} -- {len(hits)} importer(s), e.g. {hits[0].split(':')[0]}")
if not dead:
    raise SystemExit(0)

dead_names = {n for n, _ in dead}
lines = target.read_text().splitlines(keepends=True)
drop: set[int] = set()
rewrite: dict[int, str] = {}
for s in ast.parse("".join(lines)).body:
    if not isinstance(s, ast.Import | ast.ImportFrom):
        continue
    keep = [a for a in s.names if (a.asname or a.name.split(".")[0]) not in dead_names]
    if len(keep) == len(s.names):
        continue
    if not keep:
        drop.update(range(s.lineno, s.end_lineno + 1))
        continue
    names = ", ".join(a.name + (f" as {a.asname}" if a.asname else "") for a in keep)
    head = f"from {'.' * s.level}{s.module} import" if isinstance(s, ast.ImportFrom) else "import"
    rewrite[s.lineno] = f"{head} {names}\n"
    drop.update(range(s.lineno + 1, s.end_lineno + 1))

target.write_text(
    "".join(rewrite.get(i + 1, ln) for i, ln in enumerate(lines) if (i + 1) not in drop or (i + 1) in rewrite)
)
print(f"  pruned {len(dead_names)}: {', '.join(sorted(dead_names))}")
