#!/usr/bin/env python3
"""Is the mixin call graph acyclic, and do the layers match what we declared?

    uv run python scripts/verify/mixin_dag.py [package ...]

`layer.py` measures a module's DEFINITION graph -- what must exist before what, at
import time. This measures the CALL graph -- which mixin reaches into which at run
time. They answer different questions and a package can pass one and fail the other.

Why it exists
-------------
Splitting `DreamEngine` into ten mixins made it navigable and did not decouple it:
`dreaming/_protocol.py` records that 61 of 152 members are called across a mixin
boundary. Chasing that number down turned out to be the wrong goal -- the best
achievable repartition still crosses ~30% of call weight, because the concerns
genuinely share the bitemporal relationship plane.

The thing that WAS wrong, and was invisible to every other measurement, is
direction. Ten mixins, 29 edges, and seven of them in a single strongly-connected
component: `_consolidation`, `_decisions`, `_dedup`, `_formation`, `_lifecycle`,
`_materialization`, `_rollups`. A cycle that size cannot be read in any order --
understanding `_dedup` required understanding `_lifecycle`, which required
`_rollups`, which required `_formation`, which required `_dedup` again.

Six members and ~195 lines broke it. This script is what stops it coming back.

Be honest about what it does NOT buy: breaking the cycle raised mixin-to-mixin call
weight ~9%, because helpers that were internal to `_formation` became external
calls. The protocol did not shrink. The gain is that the package can now be read
and changed bottom-up, and that is not a number.

Composer calls are excluded on purpose
--------------------------------------
Every mixin calls the composer (`__init__.py`) -- that is R-A2 working as designed:
a helper used by two or more mixins lives there rather than being duplicated. Those
edges are not cycles, they are the shared kernel, and counting them would make
every package look like one big SCC.

Exit codes: 0 clean · 1 a cycle or a layer mismatch · 2 the package is not laid out
the way this script assumes (which is a bug in the script, not a finding).
"""

from __future__ import annotations

import ast
import pathlib
import sys
from collections import defaultdict

#: Declared layer assignment per package. A mixin may only call INTO a lower layer.
#: Keep this in sync deliberately -- an edit here is a design decision, and the
#: script exists so that decision cannot be made by accident.
LAYERS: dict[str, dict[str, int]] = {
    "src/memotron/dreaming": {
        "_common": 0,
        "_contradiction": 0,
        "_identity": 0,
        "_predicates": 0,
        "_rollup_text": 0,
        "_text": 0,
        "_candidates": 1,
        "_entities": 1,
        "_decisions": 2,
        "_dedup": 3,
        "_rollups": 3,
        "_consolidation": 4,
        "_lifecycle": 4,
        "_materialization": 4,
        "_formation": 5,
    },
    # Declared empty on purpose so the script REPORTS the computed layering and I
    # paste it in -- the same record/bless shape the other goldens use.
    # Predicted BEFORE the first cut and confirmed after: assigning
    # principal_for_agent to _registry rather than its adjacency group collapses a
    # four-module cycle to this.
    "src/memotron/agent_memory": {
        "_builders": 0,
        "_common": 0,
        "_config": 0,
        "_contract": 0,
        "_project": 0,
        "_registry": 0,
        "_render": 0,
        "_results": 0,
        "_scopes": 0,
        "_curation": 1,
        "_prompts": 1,
        "_publish": 1,
        "_reporting": 1,
        "_session": 2,
        "_bootstrap": 3,
        "_transcripts": 3,
    },
    # client/, registered 2026-08-29. It was absent for one commit and the lane still
    # reported PASS -- a green gate that had never looked at the largest mixin package
    # in the repo. Found by an independent verifier, not by the gate.
    #
    # Declared empty on the first pass so the script printed the computed layering,
    # then pasted back, same as agent_memory above. 14 modules, 11 edges, 19 crossing
    # members, no cycles, 3 layers -- which is what `client/_protocol.py:26` asserted in prose
    # and nothing checked. Now it is checked.
    #
    # `_lifecycle` sits at L0 despite being the largest mixin: it is a CALLEE here.
    # `run_coherence_scan` lives there and `_artifacts` and `_outcomes` (both L1) call
    # into it -- the deliberate R-A2 exception, public API mediated by the Protocol.
    "src/memotron/client": {
        "_archive": 0,
        "_common": 0,
        "_crypto": 0,
        "_ingest": 0,
        "_lifecycle": 0,
        "_profile": 0,
        "_runtime": 0,
        "_timeline": 0,
        "_artifacts": 1,
        "_governance": 1,
        "_outcomes": 1,
        "_retrieval": 1,
        "_certification": 2,
        "_views": 2,
    },
    # storage/sqlite and storage/postgres, registered 2026-09-01. Declared empty so the
    # script prints the computed layering and it gets pasted back -- same record/bless
    # shape as agent_memory and client above.
    #
    # These two were the last unguarded mixin packages, and postgres is the LARGEST in the
    # repo at 258 methods across 11 files. The lane has been reporting PASS over three of
    # five mixin packages; the same shape as when client/ was absent for a commit and the
    # gate stayed green over the biggest package in the tree.
    #
    # `config` and `admin_server` are deliberately NOT here: they mention `Mixin` zero times
    # and have no `_protocol.py`, because they have no composed class. A mixin call-graph
    # over a package with no mixins would be a lane that cannot fail.
    "src/memotron/storage/sqlite": {},
    "src/memotron/storage/postgres": {},
    # storage/_shared, registered 2026-09-01 IN THE SAME COMMIT that created it.
    #
    # Registering it is the whole point. This gate globs `<pkg>/*.py`, so a mixin
    # package that is not a LAYERS key is not measured at all -- and the lane still
    # says PASS. That is precisely what happened when `client/` was absent for one
    # commit (see the note above): a green gate that had never looked at the largest
    # mixin package in the repo, found by a reviewer rather than by the gate.
    #
    # The extraction that created this package moved 13 members OUT of storage/sqlite
    # and storage/postgres. None of them was a crossing member in either package, so
    # both engine graphs are unchanged -- 5 edges and 4 edges, same members, verified
    # before and after. `_active_epoch_ancestry` WAS a crossing member, and one of the
    # two declared edges of the postgres `_graph <-> _epochs` domain cycle; extracting
    # it would have deleted that edge, collapsed the accepted cycle, orphaned its
    # recorded justification, and flipped the layer check from SKIP to enforcing the
    # empty dict below. It was left on both engines for exactly that reason.
    #
    # Computed and pasted back, same record/bless shape as agent_memory and client:
    # 5 mixin modules, 0 edges, 1 layer. `_rows` is in the glob and contributes no
    # members (it holds module-level functions, not a class), which is why it is a
    # node with nothing pointing at it. Zero edges is expected and is the design --
    # a shared plane may only call the composed backend through
    # `_shared/_protocol.py`, never a sibling here. An edge appearing in this package
    # means one shared plane started reaching into another, which is the coupling the
    # engine packages already have and the reason this one should not grow it.
    "src/memotron/storage/_shared": {
        "_claims": 0,
        "_governance": 0,
        "_graph": 0,
        "_projection": 0,
        "_rows": 0,
    },
}

#: Cycles that are DOMAIN facts, declared edge by edge with the reason each one exists.
#:
#: A cycle is normally a defect: it means no reading order exists. But two planes can be
#: mutually dependent because the DOMAIN is, and pretending otherwise by relocating a method
#: makes the code less honest, not more layered. So the gate accepts a cycle only when every
#: edge in it is named here with a justification, and fails on anything else -- an undeclared
#: cycle, or a declared cycle that grows a new edge nobody justified.
#:
#: The bar for adding an entry is that the coupling survives the question *"what would have to
#: change about the product for this call to go away?"* -- if the answer is "move the method",
#: it is not a domain cycle and it does not belong here. Both entries below were reached that
#: way, and a third candidate FAILED that test and was fixed instead: `_as_json` was a
#: `@staticmethod` on `GraphPlaneMixin` called from `_operational` (14x), `_governance` (2x)
#: and `_graph` (3x). Decoding a jsonb column is not a graph concern; it moved to
#: `postgres/_common.py` as a module-level function, which dropped postgres from a
#: three-module cycle to a two-module one and removed two edges outright.
DOMAIN_CYCLES: dict[str, dict[tuple[str, str], str]] = {
    "src/memotron/storage/sqlite": {
        ("_governance", "_operational"): (
            "purge_tenant_state must enumerate episodes to erase them. Tenant erasure is defined "
            "over operational records; a governance plane that cannot see them cannot erase them."
        ),
        ("_operational", "_governance"): (
            "restore_prune_ghost must refuse to restore sealed content whose DEK was shredded. "
            "Crypto-shred is irreversible, so the operational restore path has to consult the "
            "governance key state or it would advertise a restore it cannot perform."
        ),
    },
    "src/memotron/storage/postgres": {
        ("_epochs", "_graph"): (
            "set_active_epoch marks the scope dirty. Switching the active epoch invalidates the "
            "derived graph projections for that scope -- the write is the invalidation."
        ),
        ("_graph", "_epochs"): (
            "Graph reads filter by active-epoch ancestry (relationships_for_scope, "
            "active_relationships, context_visible_relationships). This IS the epoch-visibility "
            "invariant -- T0-10 was this filter being correct on SQLite and absent on Postgres. "
            "A graph read that cannot see epoch state returns rows from retired epochs."
        ),
    },
}

#: Never part of the mixin graph: the composer, and the type-only Protocol.
EXCLUDED = {"__init__", "_protocol"}


def _members(pkg: pathlib.Path) -> dict[str, str]:
    """method name -> module stem that defines it."""
    owner: dict[str, str] = {}
    for path in sorted(pkg.glob("*.py")):
        if path.stem in EXCLUDED:
            continue
        for node in ast.parse(path.read_text()).body:
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    owner[item.name] = path.stem
    return owner


def _edges(pkg: pathlib.Path, owner: dict[str, str]) -> dict[tuple[str, str], set[str]]:
    """(caller module, callee module) -> the member names crossing that edge."""
    out: dict[tuple[str, str], set[str]] = defaultdict(set)
    for path in sorted(pkg.glob("*.py")):
        if path.stem in EXCLUDED:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Attribute):
                continue
            if not (isinstance(node.value, ast.Name) and node.value.id == "self"):
                continue
            target = owner.get(node.attr)
            if target and target != path.stem:
                out[(path.stem, target)].add(node.attr)
    return out


def _sccs(nodes: set[str], succ: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative -- these graphs are small but recursion limits are not a
    thing to discover from a CI failure."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    found: list[list[str]] = []
    counter = 0

    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(succ.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                nxt = pending.pop(0)
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on.add(nxt)
                    work.append((nxt, sorted(succ.get(nxt, ()))))
                elif nxt in on:
                    low[node] = min(low[node], index[nxt])
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[node])
            if low[node] == index[node]:
                comp = []
                while True:
                    w = stack.pop()
                    on.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                found.append(comp)
    return found


def check(pkg_path: str) -> int:
    pkg = pathlib.Path(pkg_path)
    if not (pkg / "__init__.py").exists():
        print(f"  {pkg_path}: not a package")
        return 2

    owner = _members(pkg)
    edges = _edges(pkg, owner)
    modules = {p.stem for p in pkg.glob("*.py")} - EXCLUDED
    succ: dict[str, set[str]] = defaultdict(set)
    for src, tgt in edges:
        succ[src].add(tgt)

    weight = sum(len(v) for v in edges.values())
    print(f"\n{pkg_path}")
    print(f"  {len(modules)} mixin modules, {len(edges)} edges, {weight} distinct crossing members")

    failures = 0

    cycles = [c for c in _sccs(modules, succ) if len(c) > 1]
    declared = DOMAIN_CYCLES.get(pkg_path, {})
    seen_declared: set[tuple[str, str]] = set()

    if cycles:
        for comp in cycles:
            live = [(src, tgt) for src in sorted(comp) for tgt in sorted(succ.get(src, ())) if tgt in comp]
            unjustified = [edge for edge in live if edge not in declared]
            seen_declared.update(edge for edge in live if edge in declared)

            if unjustified:
                # Either an undeclared cycle, or a declared one that grew an edge nobody
                # justified. Both are the thing this gate exists to catch.
                failures += 1
                print(f"  FAIL  cycle across {len(comp)} modules: {', '.join(sorted(comp))}")
                for src, tgt in live:
                    mark = " " if (src, tgt) in declared else "  <-- NOT JUSTIFIED"
                    print(f"          {src} -> {tgt}: {', '.join(sorted(edges[(src, tgt)]))}{mark}")
                print(
                    "        Add an entry to DOMAIN_CYCLES only if the coupling survives "
                    "'what would have to change about the PRODUCT for this call to go away?' -- "
                    "if the answer is 'move the method', move the method."
                )
            else:
                print(f"  DOMAIN  accepted cycle across {len(comp)} modules: {', '.join(sorted(comp))}")
                for src, tgt in live:
                    print(f"          {src} -> {tgt}: {', '.join(sorted(edges[(src, tgt)]))}")
                    print(f"              {declared[(src, tgt)]}")
    else:
        print(f"  PASS  no cycles ({len(modules)} modules)")

    # A justification that outlives the call it justifies is how this file rots. Same
    # retired-entry report as the mypy-suppression ratchet.
    retired = sorted(set(declared) - seen_declared)
    if retired:
        failures += 1
        print("  FAIL  DOMAIN_CYCLES entries that no longer describe a live cycle edge -- delete them:")
        for src, tgt in retired:
            print(f"          {src} -> {tgt}")

    if cycles:
        # Layer depth is only defined on a DAG. Reporting a bogus map on top of a
        # cycle would bury the finding that matters under a derived one. This is a SKIP
        # even for an accepted domain cycle: the layering genuinely is undefined.
        print("  SKIP  layer check -- undefined while a cycle exists (accepted or not)")
        return failures

    # The minimal layering: a module sits one above its deepest callee. Computed
    # rather than asserted, so a mismatch can print the map to paste instead of
    # making the reader derive it -- same idiom as coverage_floors.py.
    computed: dict[str, int] = {}

    def depth(node: str, seen: frozenset[str] = frozenset()) -> int:
        if node in computed:
            return computed[node]
        below = [depth(t, seen | {node}) for t in succ.get(node, ()) if t not in seen]
        computed[node] = 1 + max(below) if below else 0
        return computed[node]

    for module in sorted(modules):
        depth(module)

    declared = LAYERS.get(pkg_path.rstrip("/"))
    if declared is None:
        print("  SKIP  no declared layer map for this package")
    elif declared != computed:
        failures += 1
        print("  FAIL  declared layer map does not match the graph. Replace it with:")
        for module in sorted(computed, key=lambda m: (computed[m], m)):
            was = declared.get(module)
            note = "" if was == computed[module] else f"   # was {'L' + str(was) if was is not None else 'absent'}"
            print(f'           "{module}": {computed[module]},{note}')
    else:
        print(f"  PASS  every edge points down, {max(computed.values()) + 1} layers")
        for level in range(max(computed.values()) + 1):
            at = sorted(m for m in computed if computed[m] == level)
            print(f"          L{level}  {'  '.join(at)}")

    return failures


def main() -> int:
    targets = sys.argv[1:] or list(LAYERS)
    total = sum(check(t) for t in targets)
    print()
    print("MIXIN DAG: PASS" if total == 0 else f"MIXIN DAG: FAIL -- {total} problem(s)")
    return 1 if total else 0


if __name__ == "__main__":
    raise SystemExit(main())
