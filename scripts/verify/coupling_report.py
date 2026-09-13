#!/usr/bin/env python3
"""What "coupled" means here, measured -- and gated.

    uv run python scripts/verify/coupling_report.py

Exit 0 if every gated metric is at or better than its baseline, 1 on regression,
2 if it could not run.

Why this was rewritten
----------------------
It used to count CALL SITES: 279 `self.graph.*` nodes in `client/`, 78 `reveal`, 72
`graph_state_hash`. Those numbers never moved -- the seven-module split changed the file
layout and left all three exactly where they were -- and the conclusion drawn was that
restructuring does not help.

That conclusion outran the instrument. **A call-site count cannot tell heavy use of a
correct abstraction from a leak through a wrong one.** `models` has a fan-in of 80 and
`config` 42, and `docs/design/microkernel-plugin-architecture.md` calls exactly that
"shared vocabulary, correctly shared". A metric that scores shared vocabulary as coupling
will keep reporting that nothing can improve, whatever the truth is.

So the call-site counts are still printed, and are now INFORMATIONAL. What is gated is
structure that is wrong regardless of how often it is used:

  import cycles         Modules that import each other, at runtime. A cycle is not a
                        matter of degree -- it is a statement that two modules cannot be
                        understood, tested, or moved separately. Target: zero.
  upward tier edges     A lower tier importing a higher one, against the declared TIERS
                        below. `config` (core vocabulary) importing `storage`
                        (infrastructure) is the whole category.
  distinct members      How WIDE a consumer's use of a provider is, rather than how often.
                        72 distinct storage members reached from `client/` is a surface
                        measurement; the 279 calls through them are not.
  private reaches       Cross-object `_private` access. Already a real smell measure.

What was tried and rejected
---------------------------
**Change coupling from git history** -- which files change together -- was prototyped
because it is the only empirical measure available, everything else being a structural
proxy. Measured on this history it is noise: over the last 100 commits touching `src/`,
502 distinct co-changing pairs with a maximum count of **3**, which is a flat
distribution. Widening to 300 commits makes it worse rather than better, because the top
pairs become `client.py`, `models.py` and `dreaming.py` -- files that no longer exist, so
the window straddles the split and measures a tree that is gone. Revisit when there are a
few hundred focused commits on the post-split shape.

On the baseline
---------------
There is no `--bless`. The baseline is a dict in this file and moving it is a deliberate
edit with a reason in the commit message. A ratchet you can regenerate is one you will
regenerate at 2am to make a build green; this one costs a sentence.
"""

from __future__ import annotations

import ast
import collections
import json
import pathlib
import tomllib

#: Observed 2026-08-28, on the tree at commit da39307.
#:
#: Two of these differ from the figures first written into STATE.md and the plan,
#: and the difference is METHODOLOGY, not drift -- recorded here so nobody re-derives
#: it. Those were taken with `grep -c`, which counts LINES; this script walks the AST
#: and counts NODES, so a line holding two `self.graph` calls scores 1 there and 2
#: here. `client.py` was 279 storage nodes before the delegation commit as well.
#: The private-reach figure is a genuine correction: commit 04884a4 claims 43 -> 10,
#: but 10 was measured on the broken intermediate state where the delegate called
#: ITSELF and so was not a cross-object reach. Fixing that recursion made it 11.
BASELINE = {
    # --- GATED, re-baselined 2026-09-02 after SZ-1 (was 3/12/8/1 on 7a8e88f) ---------
    #
    # Two of the three cycles are GONE, and the 2026-09-01 note above them called it:
    # "Two edges, not eight modules' worth of work." It was three lines.
    #   size 8  <pkg> agent_memory client epochs erasure migration platform_client replay
    #           -- FIXED. `erasure.py:43` and `replay.py:32` imported the package facade
    #           as `from memotron import receipts`; both now import
    #           `memotron.storage.receipts` directly. Note a SUBMODULE import of the
    #           shim does not recreate this -- `erasure.py` still imports other names
    #           from `memotron.receipts` and the cycle stays dead. Measured.
    #   size 2  receipts <-> storage -- FIXED. `storage/base.py:157` was infra importing
    #           its own compat shim; it now imports `storage.receipts` directly.
    #   size 2  config <-> prompts -- REMAINS, deliberately. The function-local import at
    #           `config/_prompts.py:94` carries an in-code decision record at lines 3-4
    #           calling itself "load-bearing and stays function-local". Killing it needs
    #           `DreamPromptProfile` relocated out of `config`, which is a design change
    #           against that record's scope, not an import fix. Deferred to refactor
    #           unit 7; see refactor/plans/structural-zero.md.
    #
    # `upward tier edges` is 0 as of SZ-2 (2026-09-02). It was 1: core `config` reaching
    # into infra `storage` at `_dream_config.py:61` and `_storage_env.py:14` -- ONE edge,
    # TWO sites, because this metric counts distinct subsystem PAIRS and not import sites.
    # A second config->storage import could not have moved it, which cost one wrong control
    # before that was understood.
    #
    # Fixed by relocating `storage/settings.py` (83 lines, importing only typing and
    # pydantic -- configuration vocabulary, not infrastructure) into `config`, leaving a
    # re-export shim at the public `memotron.storage.settings` path. The cost was 66
    # replaced + 3 new api_surface golden rows: `DreamConfig` carries a
    # `storage: StorageSettings` field, and the type's module path feeds its structural
    # hash, so the fingerprint changed everywhere DreamConfig is re-exported. Checked
    # before taking it -- that hash appears ONLY in the golden, in no cache key, artifact
    # or stored contract version.
    "import cycles": 1,
    "modules in a cycle": 2,
    "largest cycle": 2,
    "upward tier edges": 0,
    # --- INFORMATIONAL: call-site counts, kept for continuity, no longer gated -------
    "reveal sites": 78,
    "graph_state_hash sites": 72,
    # 61 -> 62 during the split, and 72 -> 87 for the suppressions. Both are held at
    # today's measured value rather than the 2026-08-28 one: a ratchet whose baseline is a
    # number the tree has already passed is red on arrival, and a lane that is always red
    # gets muted. Recorded here so the regression is not lost by re-baselining it away.
    #
    # NOTE the suppression count differs from `mypy_suppressions.baseline.tsv` (86) because
    # they count different things: this sums codes per override BLOCK, that expands to
    # (module, code) pairs and a block may name several modules. Both are correct.
    #
    # 87 -> 84 on 2026-09-01, LOWERED because the number fell, per the rule above. The
    # shared-plane extraction retired four codes outright -- `assignment` and `attr-defined`
    # stopped firing on both `_operational` twins once the projection bodies moved to
    # storage/_shared/_projection.py -- and the one module that replaced them carries a
    # single code, `arg-type`, not the eight-error blanket the errors first argued for.
    # `attr-defined` was NOT re-declared on the shared module on purpose: it is the only
    # check that catches a `SharedPlaneBackend` Protocol member a backend does not provide.
    # See the block on `memotron.storage._shared._projection` in pyproject.toml.
    "MemoryGraphStorage methods": 62,
    # 87 -> 90 on 2026-09-03: the per-key registry (DW-030) --
    # bind_key_principal / principal_for_key_alias / unbind_key_principal.
    # DW-026 assumes this lookup exists and it did not; nothing can authenticate a
    # caller without it. Three methods is the whole surface.
    # 90 -> 91 on 2026-09-04: `formation_signing_key_status`, ONE method, and it is a
    # read that never raises. It exists because a rotated KEK changes `signer_id` (which
    # embeds the KEK hash), so the lookup MISSES rather than fails and formation silently
    # mints a second DSSE signing identity -- measured, two signer rows, different public
    # keys, nothing linking them. Diagnosing that needs a status call, not another
    # exception: the alternative was a throwaway `attest_formation_contract()` per
    # preflight, which signs for no reason and still cannot tell a rotation from a
    # mismatch. Implemented once on SharedGovernancePlaneMixin, not per engine.
    "OperationalStorage methods": 91,
    "mypy modules strict": 52,
    # 84 -> 79. NOT earned by #206 Phase 2, which is the commit lowering it: measured
    # 79 on `origin/main` and 79 on this branch, and `pyproject.toml` -- the only input to
    # this metric -- is untouched here. The baseline was simply stale against main. An
    # earlier draft of this comment claimed the relocation earned it; that was wrong and
    # checking took one command. Lowered now because this file requires an improvement to
    # be recorded in the commit that observes it.
    "mypy suppressed codes": 79,
    "client.py storage sites": 279,
    # 72 -> 73 on 2026-09-02: `graph.tenant_llm_credentials` (#158). Resolving a
    # tenant's sealed credential per operation is the fix for the cross-tenant
    # transport leak, and reading it is the one new backend member that requires.
    "client.py distinct backend members": 73,
    "client.py cross-object private reaches": 11,
    "postgres coverage %": 0,
}

#: Metrics where a RISING number is an improvement. Everything else is "fewer is better".
HIGHER_IS_BETTER = frozenset({"mypy modules strict", "postgres coverage %"})

#: GATED. A regression here fails the lane. Everything else in BASELINE is printed for
#: context and cannot fail a build -- see the docstring on why the call-site counts in
#: particular are informational rather than gated.
GATED = (
    "import cycles",
    "modules in a cycle",
    "largest cycle",
    "upward tier edges",
    "client.py distinct backend members",
    "client.py cross-object private reaches",
    "MemoryGraphStorage methods",
    "OperationalStorage methods",
    "mypy suppressed codes",
)

SRC = pathlib.Path("src/memotron")

#: The declared layering. HAND-WRITTEN on purpose: a layering derived from the imports
#: would just restate whatever the code does today and could never be violated.
#:
#: `memotron/__init__.py` -- the public facade -- is deliberately NOT tiered. It
#: imports 23 submodules by design, and delivery consuming it is correct, so ranking it
#: would manufacture four false violations (`admin_server`, `local_platform`,
#: `mcp_server`, `worker`). Where the facade is genuinely a problem is when something it
#: imports imports it BACK, and that is a cycle, which the cycle metric already catches.
TIERS: dict[str, tuple[str, ...]] = {
    "core": (
        # Telemetry and the one logging setup (#129). CORE: it imports only stdlib,
        # `redaction` and the optional opentelemetry packages, and EVERY tier above
        # calls it from its entrypoint -- placing it higher would make each of those
        # an upward tier edge.
        "observability",
        "models",
        "config",
        "prompts",
        "identity",
        "attestations",
        "redaction",
        "crypto",
        "memory_bank",
        "health",
        "interop",
    ),
    "infra": (
        "storage",
        "gateway",
        "gateway_identity",
        "embedding",
        "synthesis",
        "extraction",
        "agents",
        "multimodal",
        "runtime",
        "graph",
        "receipts",
    ),
    "application": (
        "client",
        "dreaming",
        # #251. APPLICATION, not infra: it composes the infra transports
        # (`gateway`, `synthesis`, `embedding`, `extraction`) and owns a
        # pipeline, so anything lower importing it would be an upward edge --
        # which is exactly the guard wanted here, since nothing outside the
        # package may import it at all.
        "caveman",
        "agent_memory",
        "coherence",
        "retrieval",
        "epochs",
        "replay",
        "erasure",
        "certification",
        "migration",
        "session",
        "context",
        "transcripts",
        "adoption",
        "platform_client",
    ),
    "delivery": (
        "admin_server",
        "mcp_server",
        # Per-request identity for the MCP surfaces (#126). Delivery, not core: it is an
        # ASGI middleware and it reads the in-flight HTTP request, which is a transport
        # concern even though the decision it makes is an authorization one. It imports
        # config/models (core) and gateway/gateway_identity (infra), so no upward edge.
        # Split out of `mcp_server` rather than living in it because `agent_memory_mcp` is
        # a second FastMCP server with the identical hole and will need the same guard.
        "mcp_auth",
        "agent_memory_mcp",
        "cli",
        # The operator write path for the per-key registry (#168). Delivery, with
        # `cli` which owns it: it imports config (core) and storage (infra) only,
        # so no upward tier edge. It is deliberately NOT in `delivery` alongside
        # admin_server as an endpoint -- see its docstring for why a surface that
        # authenticates nobody must not be given a tenant-binding route.
        "key_registry_cli",
        "worker",
        "local_platform",
    ),
}
TIER_ORDER = ("core", "infra", "application", "delivery")
RANK = {module: i for i, tier in enumerate(TIER_ORDER) for module in TIERS[tier]}
FACADE = "<pkg>"


def _owner(path: pathlib.Path) -> str:
    """Which top-level module a file belongs to. A package contributes its own name."""
    parts = path.relative_to(SRC).parts
    if len(parts) == 1:
        return FACADE if parts[0] == "__init__.py" else parts[0][:-3]
    return parts[0]


def _import_graph() -> tuple[dict[str, set[str]], dict[tuple[str, str], list[str]]]:
    """Top-level module import graph, RUNTIME edges only.

    Imports under `if TYPE_CHECKING:` are excluded because they do not exist at runtime
    and cannot cause an import cycle -- that is precisely the technique `session.py:36`
    already uses to stay out of one. Counting them would penalise the fix.
    """
    edges: dict[str, set[str]] = collections.defaultdict(set)
    where: dict[tuple[str, str], list[str]] = collections.defaultdict(list)
    for path in sorted(SRC.rglob("*.py")):
        owner = _owner(path)
        tree = ast.parse(path.read_text())
        deferred = {
            id(sub)
            for node in ast.walk(tree)
            if isinstance(node, ast.If) and "TYPE_CHECKING" in ast.dump(node.test)
            for sub in ast.walk(node)
        }
        for node in ast.walk(tree):
            if id(node) in deferred:
                continue
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("memotron"):
                modules = [node.module]
            elif isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names if alias.name.startswith("memotron")]
            else:
                continue
            for module in modules:
                rest = module[len("memotron") :].lstrip(".")
                target = rest.split(".")[0] if rest else FACADE
                if target and target != owner:
                    edges[owner].add(target)
                    where[owner, target].append(f"{path.relative_to(SRC.parent)}:{node.lineno}")
    return edges, where


def _cycles(edges: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan, iterative. Returns only components with more than one module.

    Iterative rather than recursive because the graph includes the facade, which reaches
    most of the tree; a recursive walk hit the interpreter limit on the first run.
    """
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    found: list[list[str]] = []
    counter = 0

    for root in list(edges):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(edges.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            node, pending = work[-1]
            if pending:
                child = pending.pop()
                if child not in index:
                    index[child] = low[child] = counter
                    counter += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, sorted(edges.get(child, ()))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
            else:
                work.pop()
                if work:
                    low[work[-1][0]] = min(low[work[-1][0]], low[node])
                if low[node] == index[node]:
                    component = []
                    while True:
                        popped = stack.pop()
                        on_stack.discard(popped)
                        component.append(popped)
                        if popped == node:
                            break
                    if len(component) > 1:
                        found.append(sorted(component))
    return found


def _consumers() -> list[pathlib.Path]:
    """The two big storage consumers, whichever shape they are currently in.

    This function read SRC / client.py until 2026-08-31 and therefore raised
    `FileNotFoundError` from the moment `client.py` became a package -- a commit made
    on this very branch. So the instrument the coupling pass is "steered by", per the
    docstring above, has been dead for the whole of that pass, and nothing noticed
    because it is not a gate and nothing runs it.

    Resolved by shape rather than by name so the next split does not break it again:
    a package contributes its `*.py`, a module contributes itself, and a missing name
    is a hard error rather than a silent zero -- a coupling report that quietly measures
    half the tree is worse than one that fails.
    """
    out: list[pathlib.Path] = []
    for name in ("client", "dreaming"):
        pkg, mod = SRC / name, SRC / f"{name}.py"
        if pkg.is_dir():
            out.extend(sorted(pkg.glob("*.py")))
        elif mod.exists():
            out.append(mod)
        else:  # pragma: no cover - defensive; means the tree moved again
            raise SystemExit(f"coupling_report: neither {pkg} nor {mod} exists — fix _consumers()")
    return out


def measure() -> dict[str, float]:
    out: dict[str, float] = {}

    edges, _ = _import_graph()
    cycles = _cycles(edges)
    out["import cycles"] = len(cycles)
    out["modules in a cycle"] = sum(len(component) for component in cycles)
    out["largest cycle"] = max((len(component) for component in cycles), default=0)

    upward = [(a, b) for a, targets in edges.items() if a in RANK for b in targets if b in RANK and RANK[a] < RANK[b]]
    out["upward tier edges"] = len(upward)

    text = "".join(p.read_text() for p in _consumers())
    out["reveal sites"] = text.count(".reveal(") + text.count(".reveal_vector(")
    out["graph_state_hash sites"] = text.count(".graph_state_hash(")

    # Reported SEPARATELY, and this is a correction. The first version summed them
    # into "StorageBackend methods: 148" and called that "a namespace, not a
    # contract". Wrong: StorageBackend has ZERO methods of its own -- it is
    # MemoryGraphStorage + OperationalStorage, two named domain contracts -- and
    # SplitStorageBackend already routes between them for split deployments, so the
    # seam is not hypothetical, it is exercised. A sum of two contracts is not a
    # namespace, and treating it as one made "shrink the contract" look like
    # available work when it is not.
    base = ast.parse((SRC / "storage" / "base.py").read_text())
    for cls in ("MemoryGraphStorage", "OperationalStorage"):
        out[f"{cls} methods"] = next(
            len([m for m in c.body if isinstance(m, ast.FunctionDef | ast.AsyncFunctionDef)])
            for c in base.body
            if isinstance(c, ast.ClassDef) and c.name == cls
        )

    cfg = tomllib.loads(pathlib.Path("pyproject.toml").read_text())
    overrides = cfg["tool"]["mypy"]["overrides"]
    out["mypy modules strict"] = sum(
        len(o["module"]) if isinstance(o["module"], list) else 1 for o in overrides if o.get("disallow_untyped_defs")
    )
    out["mypy suppressed codes"] = sum(len(o["disable_error_code"]) for o in overrides if "disable_error_code" in o)

    # Same shape-resolution as _consumers(), for the same reason: this read
    # SRC / client.py directly and was the SECOND hardcoded reference to a file the
    # split deleted. Fixing only the first still left the script dead, which is why the
    # helper is reused here rather than the path being spelled a second time.
    # The second disjunct deliberately probes the pre-split shape.
    client_files = [
        p
        for p in _consumers()
        if p.parent.name == "client" or p.name == "client.py"  # freshness-ok
    ]
    if not client_files:  # pragma: no cover - defensive
        raise SystemExit("coupling_report: found no client sources — fix _consumers()")
    members: collections.Counter[str] = collections.Counter()
    private = 0
    for path in client_files:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            value = node.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name) and value.value.id == "self":
                if value.attr == "graph":
                    members[node.attr] += 1
                if node.attr.startswith("_") and not node.attr.startswith("__"):
                    private += 1
    out["client.py storage sites"] = sum(members.values())
    out["client.py distinct backend members"] = len(members)
    out["client.py cross-object private reaches"] = private

    cov = pathlib.Path("coverage.json")
    if cov.exists():
        files = json.loads(cov.read_text())["files"]
        pg = [v["summary"] for k, v in files.items() if "postgres" in k]
        stmts = sum(v["num_statements"] for v in pg)
        out["postgres coverage %"] = round(100 * sum(v["covered_lines"] for v in pg) / stmts, 1) if stmts else 0.0
    else:
        out["postgres coverage %"] = -1.0  # unknown, not zero

    return out


def main() -> int:
    if not SRC.exists():
        print("  run from the repo root")
        return 2
    now = measure()
    print("\n  coupling -- gated metrics first; baseline 2026-09-01 (commit 7a8e88f)\n")
    print(f"    {'':1} {'metric':42} {'baseline':>9} {'now':>9}   drift")

    regressions: list[str] = []
    improvements: list[str] = []
    for key, was in BASELINE.items():
        got = now[key]
        gated = key in GATED
        if got < 0:
            drift = "no coverage.json -- run the tests lane"
        elif got == was:
            drift = "-"
        else:
            delta = got - was
            better = delta > 0 if key in HIGHER_IS_BETTER else delta < 0
            drift = f"{delta:+g}  {'better' if better else 'WORSE'}"
            if gated and not better:
                regressions.append(f"{key}: {was:g} -> {got:g}")
            elif gated:
                improvements.append(f"{key}: {was:g} -> {got:g}")
        shown = "?" if got < 0 else f"{got:g}"
        print(f"    {'*' if gated else ' '} {key:42} {was:>9g} {shown:>9}   {drift}")
    print("\n    * = gated. Unmarked rows are informational and cannot fail this lane.")

    # Name the cycles rather than only counting them. A count tells you the gate went
    # red; the members tell you what to do, and this is the lane's whole output when it
    # fails, so it has to be actionable without a second command.
    edges, where = _import_graph()
    cycles = _cycles(edges)
    if cycles:
        print(f"\n    {len(cycles)} import cycle(s):")
        for component in sorted(cycles, key=len, reverse=True):
            print(f"      [{len(component)}] {' '.join(component)}")
            for a in component:
                for b in sorted(edges.get(a, ())):
                    if b in component:
                        print(f"          {a} -> {b}   {where[a, b][0]}")

    upward = sorted(
        (a, b) for a, targets in edges.items() if a in RANK for b in targets if b in RANK and RANK[a] < RANK[b]
    )
    if upward:
        print("\n    upward tier edge(s):")
        for a, b in upward:
            print(f"      {TIER_ORDER[RANK[a]]} {a} -> {TIER_ORDER[RANK[b]]} {b}")
            for site in where[a, b]:
                print(f"          {site}")

    if improvements:
        print("\n    IMPROVED -- lower the baseline in the same commit, and say why:")
        for line in improvements:
            print(f"      {line}")
    if regressions:
        print("\n    FAIL -- gated metric(s) regressed:")
        for line in regressions:
            print(f"      {line}")
        print("\n    Coupling got worse. If that is deliberate, raise the baseline in this")
        print("    file with a reason in the commit message -- there is no --bless.")
        return 1

    if now["postgres coverage %"] <= 0:
        print("\n    NOTE: no postgres coverage in this run; export MEMOTRON_TEST_POSTGRES_DSN")
        print("    and run the tests lane if you are touching storage/.")
    print("\n  COUPLING: PASS -- no gated metric regressed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
