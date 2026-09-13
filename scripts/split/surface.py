"""Every name <ref>'s module DEFINED must still be importable from the package."""

from __future__ import annotations

import ast
import importlib
import subprocess
import sys

ref, pkg = sys.argv[1], sys.argv[2]
path = "src/" + pkg.replace(".", "/") + "/__init__.py"
src = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True, text=True).stdout
if not src:
    src = subprocess.run(
        ["git", "show", f"{ref}:src/{pkg.replace('.', '/')}.py"], capture_output=True, text=True
    ).stdout
names = []
for s in ast.parse(src).body:
    if isinstance(s, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
        names.append(s.name)
    elif isinstance(s, ast.Assign):
        names += [t.id for t in s.targets if isinstance(t, ast.Name)]
    elif isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name):
        names.append(s.target.id)
m = importlib.import_module(pkg)
missing = [n for n in names if not hasattr(m, n)]
print(
    f"{len(names) - len(missing)}/{len(names)} defined names importable"
    + (f" -- MISSING {missing}" if missing else " -- PASS")
)
sys.exit(1 if missing else 0)
