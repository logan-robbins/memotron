#!/usr/bin/env python3
"""Derive a module's internal definition layering before splitting it.

    uv run python scripts/verify/layer.py src/memotron/models.py
    uv run python scripts/verify/layer.py src/memotron/config.py --show-cycles

Why this exists
---------------
`config.py` is a DAG, not a bag: 41 pydantic models and 82 validators with direct
(non-string) references to each other, where definition ORDER matters. Splitting
it by reading it is how you discover a cycle at import time, three commits in.

The output is the constraint, not a suggestion: **every strongly-connected
component is one indivisible file.** If two proposed submodules mutually
reference each other, they are one submodule. Do NOT break a cycle by converting
a reference to a string annotation plus `model_rebuild()` -- that changes *when*
validation errors surface, which is a behaviour change wearing a refactor's
clothes.

Hard vs soft references
-----------------------
With `from __future__ import annotations` every annotation is a string, so an
annotation-only reference does NOT constrain definition order -- pydantic
resolves it later. Only HARD references do:

    hard   base classes, decorators, default values, and any expression
           evaluated while the class/function statement itself executes
    soft   annotation-only

Layers are computed from hard references. Soft references are reported
separately because they still determine what each new submodule must IMPORT,
just not what order the files can be created in.
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path


def top_level_names(tree: ast.Module) -> dict[str, ast.stmt]:
    """Every name bound at module scope, mapped to the statement that binds it."""
    names: dict[str, ast.stmt] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            names[stmt.name] = stmt
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    names[target.id] = stmt
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            names[stmt.target.id] = stmt
    return names


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}


def references(stmt: ast.stmt, universe: set[str]) -> tuple[set[str], set[str]]:
    """(hard, soft) references this statement makes to other top-level names."""
    hard: set[str] = set()
    soft: set[str] = set()

    if isinstance(stmt, ast.ClassDef):
        for base in stmt.bases:
            hard |= _names_in(base)
        for kw in stmt.keywords:
            hard |= _names_in(kw.value)
        for dec in stmt.decorator_list:
            hard |= _names_in(dec)
        for member in stmt.body:
            # A class-level annotation is deferred; its DEFAULT is not.
            if isinstance(member, ast.AnnAssign):
                if member.annotation is not None:
                    soft |= _names_in(member.annotation)
                if member.value is not None:
                    hard |= _names_in(member.value)
            elif isinstance(member, ast.FunctionDef | ast.AsyncFunctionDef):
                for dec in member.decorator_list:
                    hard |= _names_in(dec)
                for default in [*member.args.defaults, *(d for d in member.args.kw_defaults if d)]:
                    hard |= _names_in(default)
                # Method BODIES run at call time, not definition time.
                soft |= _names_in(member)
            else:
                hard |= _names_in(member)

    elif isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef):
        for dec in stmt.decorator_list:
            hard |= _names_in(dec)
        for default in [*stmt.args.defaults, *(d for d in stmt.args.kw_defaults if d)]:
            hard |= _names_in(default)
        soft |= _names_in(stmt)

    else:  # module-level assignment: evaluated immediately
        hard |= _names_in(stmt)

    hard &= universe
    soft = (soft & universe) - hard
    return hard, soft


def strongly_connected(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC, iterative so a deep graph cannot blow the stack."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: dict[str, bool] = {}
    stack: list[str] = []
    result: list[list[str]] = []
    counter = 0

    for root in graph:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            node, child_i = work[-1]
            if child_i == 0:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack[node] = True
            recursed = False
            children = sorted(graph[node])
            for i in range(child_i, len(children)):
                child = children[i]
                if child not in index:
                    work[-1] = (node, i + 1)
                    work.append((child, 0))
                    recursed = True
                    break
                if on_stack.get(child):
                    low[node] = min(low[node], index[child])
            if recursed:
                continue
            if low[node] == index[node]:
                component = []
                while True:
                    w = stack.pop()
                    on_stack[w] = False
                    component.append(w)
                    if w == node:
                        break
                result.append(sorted(component))
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", type=Path)
    parser.add_argument("--show-cycles", action="store_true", help="list every multi-name SCC in full")
    parser.add_argument("--show-soft", action="store_true", help="also print annotation-only references")
    args = parser.parse_args(argv)

    tree = ast.parse(args.path.read_text(encoding="utf-8"))
    defs = top_level_names(tree)
    universe = set(defs)
    order = list(defs)  # source order, for stable reporting

    hard_graph: dict[str, set[str]] = {}
    soft_graph: dict[str, set[str]] = {}
    for name, stmt in defs.items():
        h, s = references(stmt, universe)
        hard_graph[name] = h - {name}
        soft_graph[name] = s - {name}

    sccs = strongly_connected(hard_graph)
    comp_of = {n: i for i, c in enumerate(sccs) for n in c}
    cyclic = [c for c in sccs if len(c) > 1]

    # Condensation, then longest-path layering so each layer only depends on earlier ones.
    cond: dict[int, set[int]] = defaultdict(set)
    for name, deps in hard_graph.items():
        for dep in deps:
            if comp_of[dep] != comp_of[name]:
                cond[comp_of[name]].add(comp_of[dep])

    depth: dict[int, int] = {}

    def resolve(c: int, seen: frozenset[int] = frozenset()) -> int:
        if c in depth:
            return depth[c]
        if c in seen:  # cannot happen on a condensation; defensive
            return 0
        d = 1 + max((resolve(p, seen | {c}) for p in cond[c]), default=-1)
        depth[c] = d
        return d

    for c in range(len(sccs)):
        resolve(c)

    print(f"{args.path}: {len(defs)} top-level definitions, {len(sccs)} components")
    print(
        f"  hard edges: {sum(len(v) for v in hard_graph.values())}   "
        f"soft (annotation-only): {sum(len(v) for v in soft_graph.values())}"
    )
    print(f"  CYCLES (indivisible groups): {len(cyclic)}")
    if cyclic and not args.show_cycles:
        print("     re-run with --show-cycles to list them")
    for c in cyclic:
        print(f"     [{len(c)}] {', '.join(c)}")

    layers: dict[int, list[str]] = defaultdict(list)
    for i, comp in enumerate(sccs):
        layers[depth[i]].extend(comp)
    print(f"\n  {len(layers)} layers (layer N may only reference layers < N):")
    for d in sorted(layers):
        members = sorted(layers[d], key=order.index)
        print(f"\n  --- layer {d}  ({len(members)} names) ---")
        for m in members:
            deps = sorted(hard_graph[m], key=order.index)
            extra = f"   <- {', '.join(deps)}" if deps else ""
            print(f"      {m}{extra}")

    if args.show_soft:
        print("\n  annotation-only references (do not constrain order; DO constrain imports):")
        for name in order:
            if soft_graph[name]:
                print(f"      {name} ~ {', '.join(sorted(soft_graph[name], key=order.index))}")

    if cyclic:
        print(
            f"\nNOTE: {len(cyclic)} cycle(s). Each is ONE file. Do not break a cycle with a "
            "string annotation + model_rebuild() -- that moves when validation errors surface."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
