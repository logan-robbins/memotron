#!/usr/bin/env python3
"""P3 -- prove a refactor commit moved code without changing it.

    uv run python scripts/verify/pure_move.py <ref-a> [<ref-b>]

``<ref-b>`` defaults to the working tree.  Exit 0 means every function that
exists in both refs is *bytecode-identical*, no moved function lost a global it
depends on, and no file lost its ``from __future__ import annotations``.

Why bytecode and not source
---------------------------
The split contract is "zero behaviour change, zero import change".  A test suite
proves that only for the paths it executes; ``dreaming.py`` and ``client.py`` sit
at 89% statement coverage, so ~11% of the code being moved has no runtime witness
at all.  Comparing compiled code objects is a *static* proof over 100% of it, and
it is immune to reindentation, reformatting and reordering -- the three things a
mixin extraction does constantly.

What each check catches
-----------------------
``bytecode``  A body that changed while being moved: a swapped argument, a
              dropped ``await``, an inverted condition, a hoisted local import
              (which moves ``IMPORT_NAME`` out of the function).
``globals``   THE bug of this refactor class: a moved function still references a
              module-level constant, regex or helper that stayed behind.  Purely
              static, so it fires on code paths the suite never runs.
``future``    An extracted file missing ``from __future__ import annotations``,
              which silently starts *evaluating* annotations that were strings.

Functions are matched across refs by unqualified name, comparing the multiset of
fingerprints for each name.  Deliberately NOT by ``module.Class.name``: the whole
point of the refactor is that a method moves from ``DreamEngine`` in
``dreaming.py`` to ``FormationMixin`` in ``dreaming/_formation.py``, so any key
containing the module or class would report every correct move as a difference.

Names present in only one ref are reported as ``added``/``removed`` counts, not
failures -- a commit that legitimately introduces a mixin class adds names. Use
the API-surface golden (tests/test_api_surface.py) for "nothing disappeared";
this tool answers the narrower question "nothing *changed*".
"""

from __future__ import annotations

import argparse
import ast
import builtins
import dis
import hashlib
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, NamedTuple

PACKAGE_ROOT = "src/memotron"
WORKTREE = "."

_BUILTIN_NAMES = frozenset(dir(builtins))


class Fingerprint(NamedTuple):
    """Everything about a function that must not change when it moves."""

    argcount: int
    posonly: int
    kwonly: int
    flags: int
    varnames: tuple[str, ...]
    names: tuple[str, ...]
    consts: tuple[Any, ...]
    code: bytes
    decorators: tuple[str, ...]

    def digest(self) -> str:
        """Stable content hash, used for ordering and equality.

        Fingerprints cannot be compared with ``<`` directly -- ``co_consts`` mixes
        ``str``, ``int``, ``None`` and tuples -- and they must not be compared by
        ``repr`` alone either, because a ``frozenset``'s iteration order varies
        with ``PYTHONHASHSEED``.  ``_const`` normalises sets to sorted tuples, so
        this repr is deterministic across processes.
        """
        return hashlib.sha256(repr(tuple(self)).encode("utf-8")).hexdigest()

    def summary(self) -> str:
        return f"args={self.argcount} vars={len(self.varnames)} code={len(self.code)}B"


class FunctionRecord(NamedTuple):
    name: str
    module: str
    owner: str | None
    lineno: int
    fingerprint: Fingerprint


# --------------------------------------------------------------------------- io


def _run(*args: str) -> str:
    return subprocess.run(args, capture_output=True, text=True, check=True).stdout


def _module_name(path: str) -> str:
    return path[len("src/") :].removesuffix(".py").replace("/", ".").removesuffix(".__init__")


def load_tree(ref: str) -> dict[str, str]:
    """Map ``src/memotron/**.py`` -> source text at *ref* (``.`` = working tree)."""
    if ref == WORKTREE:
        return {str(p): p.read_text(encoding="utf-8") for p in sorted(Path(PACKAGE_ROOT).rglob("*.py"))}
    listing = _run("git", "ls-tree", "-r", "--name-only", ref, "--", PACKAGE_ROOT)
    paths = [line for line in listing.splitlines() if line.endswith(".py")]
    return {path: _run("git", "show", f"{ref}:{path}") for path in sorted(paths)}


# ------------------------------------------------------------------ fingerprints


def _const(value: Any) -> Any:
    """Normalise a co_consts entry so nested code objects compare structurally."""
    if isinstance(value, type((lambda: None).__code__)):
        return ("<code>", value.co_argcount, value.co_code, tuple(_const(c) for c in value.co_consts))
    if isinstance(value, tuple):
        return tuple(_const(v) for v in value)
    if isinstance(value, frozenset | set):
        # Sorted by repr, never left as a set: set iteration order depends on
        # PYTHONHASHSEED, which would make the digest differ between processes.
        return ("<set>", tuple(sorted(repr(_const(v)) for v in value)))
    return value


def _compile_isolated(node: ast.FunctionDef | ast.AsyncFunctionDef, *, in_class: bool, futures: bool):
    """Compile one function alone and return its code object.

    A method is wrapped in a class with a FIXED name so ``__qualname__`` and the
    ``__class__`` cell that zero-arg ``super()`` needs behave identically in both
    refs regardless of what the real (renamed) class is called.

    ``futures`` mirrors whether the source file had ``from __future__ import
    annotations``.  Without it the compiler *evaluates* annotations instead of
    storing strings, which changes the bytecode of nearly every function here.
    """
    body: list[ast.stmt] = []
    if futures:
        body.append(ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0))
    stripped = _strip_decorators(node)
    if in_class:
        body.append(
            ast.ClassDef(
                name="_C",
                bases=[],
                keywords=[],
                body=[stripped],
                decorator_list=[],
                type_params=[],
            )
        )
    else:
        body.append(stripped)
    module = ast.fix_missing_locations(ast.Module(body=body, type_ignores=[]))
    compiled = compile(module, "<pure_move>", "exec")
    return _find_code(compiled, node.name)


def _strip_decorators(node: ast.FunctionDef | ast.AsyncFunctionDef):
    """Compile the function without its decorators; they are fingerprinted as text.

    Decorator *expressions* compile into the enclosing scope, not the function's
    own code object, so leaving them in would make the fingerprint depend on code
    this tool is not comparing. Their text is carried separately -- which is what
    makes a silently-dropped ``@mcp.tool()`` visible.
    """
    clone = type(node)(**{field: getattr(node, field) for field in node._fields})
    clone.decorator_list = []
    return ast.copy_location(clone, node)


def _find_code(code: Any, name: str) -> Any:
    code_type = type((lambda: None).__code__)
    for const in code.co_consts:
        if isinstance(const, code_type):
            if const.co_name == name:
                return const
            found = _find_code(const, name)
            if found is not None:
                return found
    return None


def fingerprint(node: ast.FunctionDef | ast.AsyncFunctionDef, *, in_class: bool, futures: bool) -> Fingerprint:
    code = _compile_isolated(node, in_class=in_class, futures=futures)
    if code is None:  # pragma: no cover - defensive; every def yields a code object
        raise RuntimeError(f"could not compile {node.name}")
    return Fingerprint(
        argcount=code.co_argcount,
        posonly=code.co_posonlyargcount,
        kwonly=code.co_kwonlyargcount,
        flags=code.co_flags,
        varnames=tuple(code.co_varnames),
        names=tuple(code.co_names),
        consts=tuple(_const(c) for c in code.co_consts),
        code=code.co_code,
        decorators=tuple(ast.unparse(d) for d in node.decorator_list),
    )


def _has_future_annotations(tree: ast.Module) -> bool:
    return any(
        isinstance(stmt, ast.ImportFrom)
        and stmt.module == "__future__"
        and any(alias.name == "annotations" for alias in stmt.names)
        for stmt in tree.body
    )


def _is_protocol_stub(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """A Protocol/overload declaration: a body of exactly `...` (with optional docstring).

    These are type-checking declarations, not code. They share names with the real
    methods they describe -- storage/sqlite/_protocol.py declares _commit,
    _datetime_to_text and 15 others -- so counting them makes every such name look
    like it gained a definition. Seventeen false "count changed" reports on the
    commit that introduced that Protocol, with zero real body changes among them.
    """
    body = [
        st
        for st in node.body
        if not (isinstance(st, ast.Expr) and isinstance(st.value, ast.Constant) and isinstance(st.value.value, str))
    ]
    return (
        len(body) == 1
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and body[0].value.value is Ellipsis
    )


def collect(tree_sources: dict[str, str]) -> tuple[list[FunctionRecord], dict[str, bool]]:
    """Index every top-level function and every method of a top-level class.

    Nested functions are intentionally not indexed separately -- their code lives
    inside the enclosing function's ``co_consts``, so the parent's fingerprint
    already covers them, and indexing both would double-count.
    """
    records: list[FunctionRecord] = []
    futures_by_file: dict[str, bool] = {}
    for path, source in tree_sources.items():
        tree = ast.parse(source)
        futures = _has_future_annotations(tree)
        futures_by_file[path] = futures
        module = _module_name(path)
        for stmt in tree.body:
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and not _is_protocol_stub(stmt):
                records.append(
                    FunctionRecord(
                        stmt.name, module, None, stmt.lineno, fingerprint(stmt, in_class=False, futures=futures)
                    )
                )
            elif isinstance(stmt, ast.ClassDef):
                records.extend(
                    FunctionRecord(
                        member.name,
                        module,
                        stmt.name,
                        member.lineno,
                        fingerprint(member, in_class=True, futures=futures),
                    )
                    for member in stmt.body
                    if isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef) and not _is_protocol_stub(member)
                )
    return records, futures_by_file


# ------------------------------------------------------------- dangling globals


def module_bindings(tree: ast.Module) -> set[str]:
    """Every name bound at module scope: imports, assignments, defs, classes."""
    bound: set[str] = set()
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                bound.add(alias.asname or alias.name)
        elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            bound.add(stmt.name)
        elif isinstance(stmt, ast.Name) and isinstance(stmt.ctx, ast.Store):
            bound.add(stmt.id)
        elif isinstance(stmt, ast.Global):
            bound.update(stmt.names)
    return bound


def dangling_globals(tree_sources: dict[str, str]) -> list[tuple[str, str, str]]:
    """(module, function, name) for every global a function reads that is unbound.

    Uses LOAD_GLOBAL specifically rather than the whole of ``co_names``, which
    also holds attribute names -- ``self.foo`` must not be mistaken for a global.
    """
    problems: list[tuple[str, str, str]] = []
    for path, source in tree_sources.items():
        tree = ast.parse(source)
        bound = module_bindings(tree) | _BUILTIN_NAMES
        futures = _has_future_annotations(tree)
        module = _module_name(path)
        for stmt in tree.body:
            members: list[tuple[ast.AST, bool]] = []
            if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
                members.append((stmt, False))
            elif isinstance(stmt, ast.ClassDef):
                members.extend((m, True) for m in stmt.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef))
            for member, in_class in members:
                code = _compile_isolated(member, in_class=in_class, futures=futures)  # type: ignore[arg-type]
                if code is None:  # pragma: no cover - defensive
                    continue
                problems.extend(
                    (module, member.name, name)  # type: ignore[attr-defined]
                    for name in _loaded_globals(code)
                    if name not in bound
                )
    return problems


def _loaded_globals(code: Any) -> set[str]:
    names = {i.argval for i in dis.get_instructions(code) if i.opname == "LOAD_GLOBAL"}
    code_type = type((lambda: None).__code__)
    for const in code.co_consts:
        if isinstance(const, code_type):
            names |= _loaded_globals(const)
    return names


# ---------------------------------------------------------------------- compare


def compare(before: list[FunctionRecord], after: list[FunctionRecord]) -> list[str]:
    by_name_before: dict[str, list[FunctionRecord]] = defaultdict(list)
    by_name_after: dict[str, list[FunctionRecord]] = defaultdict(list)
    for record in before:
        by_name_before[record.name].append(record)
    for record in after:
        by_name_after[record.name].append(record)

    failures: list[str] = []
    for name in sorted(set(by_name_before) & set(by_name_after)):
        old = by_name_before[name]
        new = by_name_after[name]
        old_prints = sorted((r.fingerprint.digest(), r.fingerprint) for r in old)
        new_digests = sorted(r.fingerprint.digest() for r in new)
        if [d for d, _ in old_prints] == new_digests:
            continue
        if len(old) != len(new):
            failures.append(
                f"{name}: {len(old)} definition(s) before, {len(new)} after "
                f"(before: {', '.join(_where(r) for r in old)}; "
                f"after: {', '.join(_where(r) for r in new)})"
            )
            continue
        for digest, print_ in old_prints:
            if digest in new_digests:
                new_digests.remove(digest)
                continue
            culprits = ", ".join(_where(r) for r in new)
            failures.append(f"{name}: body changed while moving [{print_.summary()}] -- now at {culprits}")
    return failures


def _where(record: FunctionRecord) -> str:
    owner = f"{record.owner}." if record.owner else ""
    return f"{record.module}:{owner}{record.name}@{record.lineno}"


# ------------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("before", help="git ref to compare from (e.g. HEAD, HEAD~1, a tag)")
    parser.add_argument("after", nargs="?", default=WORKTREE, help="git ref, or '.' for the working tree (default)")
    parser.add_argument("--skip-globals", action="store_true", help="skip the dangling-global check")
    args = parser.parse_args(argv)

    print(f"pure_move: {args.before} -> {args.after}")
    before_sources = load_tree(args.before)
    after_sources = load_tree(args.after)
    before, before_futures = collect(before_sources)
    after, after_futures = collect(after_sources)
    print(
        f"  indexed {len(before)} functions before, {len(after)} after "
        f"({len(before_sources)} -> {len(after_sources)} files)"
    )

    failures: list[str] = []

    bytecode_failures = compare(before, after)
    _report("bytecode identity", bytecode_failures)
    failures += bytecode_failures

    future_failures = [
        f"{path}: lost 'from __future__ import annotations'"
        for path, had in before_futures.items()
        if had and after_futures.get(path) is False
    ] + [
        f"{path}: new file without 'from __future__ import annotations'"
        for path, has in after_futures.items()
        if not has and path not in before_futures
    ]
    _report("__future__ annotations", future_failures)
    failures += future_failures

    if args.skip_globals:
        print("  SKIPPED  dangling globals (--skip-globals)")
    else:
        before_dangling = set(dangling_globals(before_sources))
        after_dangling = set(dangling_globals(after_sources))
        introduced = sorted(after_dangling - before_dangling)
        global_failures = [f"{module}.{func} reads unbound global '{name}'" for module, func, name in introduced]
        _report("dangling globals", global_failures)
        if before_dangling:
            print(f"           ({len(before_dangling)} pre-existing, not counted)")
        failures += global_failures

    names_before = {r.name for r in before}
    names_after = {r.name for r in after}
    added, removed = names_after - names_before, names_before - names_after
    if added or removed:
        print(
            f"  note: {len(added)} name(s) added, {len(removed)} removed "
            "-- not a failure here; the API-surface golden owns that question"
        )
        for name in sorted(removed):
            print(f"           removed: {name}")

    print()
    if failures:
        print(f"PURE MOVE: FAIL -- {len(failures)} problem(s)")
        return 1
    print("PURE MOVE: PASS -- every shared function is bytecode-identical")
    return 0


def _report(label: str, failures: list[str]) -> None:
    if failures:
        print(f"  FAIL     {label} ({len(failures)})")
        for failure in failures[:40]:
            print(f"           {failure}")
        if len(failures) > 40:
            print(f"           ... and {len(failures) - 40} more")
    else:
        print(f"  PASS     {label}")


if __name__ == "__main__":
    sys.exit(main())
