"""Every MCP tool either resolves a caller-named scope, or is declared as not doing so.

#126, first slice. The MCP server has no identity concept: `_scope(scope_kind, scope_id)`
builds a `MemoryScope` straight from caller-supplied strings, and any caller reaching the
ingress can read or write ANY tenant's memory by naming it. The fix threads a principal onto
the request and checks the scope against its allowlist inside `_scope`, which covers 33 of 39
tools because they all funnel through that one helper.

**This file exists for the other six.** A guard installed at a choke point is only as good as
the claim that everything passes through it, and nothing enforced that claim -- a tool added
next month that reads `tenant_id` directly, or one that goes fleet-wide, would escape silently
and no test would notice. So the set of exemptions is written down as `_CROSS_SCOPE_TOOLS` and
asserted here from the SERVER's own tool registry rather than from the source text.

That distinction is deliberate. `tests/test_mcp_tool_registration.py` exists in this repo
precisely because counting registered tools from the running server caught what reading the
source could not, and the same reasoning applies here: a decorator that fails to register, a
rename, or a tool defined in another module are all invisible to grep and visible to
`list_tools()`.
"""

from __future__ import annotations

import pytest

from memotron.mcp_server import _CROSS_SCOPE_TOOLS, _OPTIONAL_SCOPE_TOOLS, mcp


@pytest.fixture(scope="module")
def tool_schemas() -> dict[str, dict]:
    """Every registered tool's input schema, read from the server, not the source."""
    import asyncio

    tools = asyncio.run(mcp._list_tools())
    # `.parameters` under FastMCP 4; it was `.inputSchema` on the SDK's Tool. The rename
    # matters to THIS fixture more than most: every test here derives its expectations
    # from these schemas, so an empty dict per tool would make the whole file pass
    # vacuously -- no tool would appear to take a scope, and the tripwire below asserts
    # on `_CROSS_SCOPE_TOOLS` membership rather than on any schema being non-empty.
    # `test_the_registry_is_not_trivially_empty` is the control for exactly that.
    return {t.name: (t.parameters or {}) for t in tools}


def test_every_tool_is_scoped_or_declared(tool_schemas: dict[str, dict]) -> None:
    """THE TRIPWIRE. A new tool that takes no scope must be a deliberate edit.

    Failing this means one of two things, and both want a human:
      * the tool should take `scope_kind`/`scope_id` and someone forgot -- it is currently
        reachable for every tenant by any caller;
      * it genuinely operates across scopes, in which case add it to `_CROSS_SCOPE_TOOLS`
        AND give it its own authorization, because the scope check will never run for it.
    """
    unscoped = {name for name, schema in tool_schemas.items() if "scope_kind" not in (schema.get("properties") or {})}
    undeclared = unscoped - _CROSS_SCOPE_TOOLS
    assert not undeclared, (
        f"{len(undeclared)} tool(s) take no scope and are not declared cross-scope: "
        f"{sorted(undeclared)}. Either give them scope_kind/scope_id, or add them to "
        "_CROSS_SCOPE_TOOLS and authorize them explicitly -- the scope guard will not."
    )


def test_the_exemption_list_has_no_stale_entries(tool_schemas: dict[str, dict]) -> None:
    """The other direction: an exemption for a tool that no longer needs one.

    Without this, `_CROSS_SCOPE_TOOLS` only ever grows, and a name left behind after a tool
    is renamed or given a scope argument would silently exempt nothing while looking like it
    still guards something.
    """
    registered = set(tool_schemas)
    ghosts = _CROSS_SCOPE_TOOLS - registered
    assert not ghosts, f"declared cross-scope but not registered at all: {sorted(ghosts)}"

    now_scoped = {
        name for name in _CROSS_SCOPE_TOOLS & registered if "scope_kind" in (tool_schemas[name].get("properties") or {})
    }
    assert not now_scoped, (
        f"{sorted(now_scoped)} now take scope_kind, so the exemption is obsolete -- "
        "remove them from _CROSS_SCOPE_TOOLS so the guard covers them."
    )


def test_the_exemption_set_is_exactly_what_we_measured() -> None:
    """Pin the membership so growing OR shrinking it is a deliberate, reviewed edit.

    Measured against origin/main on 2026-09-06 by `list_tools()`: 39 tools registered,
    33 resolve a scope, 6 never did. **It is 5 since B3**, which gave
    `remediate_coherence` a scope and moved it to `_OPTIONAL_SCOPE_TOOLS` -- the one
    direction this set is meant to move, and the reason it is pinned rather than derived.

    It was 6 and not 5 before that because this file's first run disagreed with the
    hand-built constant. `clear_tenant_llm` -- which DELETES a caller-named tenant's LLM
    credential -- was missing from it, because that list was assembled by parsing the
    source and the parser lost it. That is the whole argument for reading the registry
    instead of the source, made by the guard on its first execution.
    """
    assert (
        frozenset(
            {
                "configure_tenant_llm",
                "tenant_llm_status",
                "clear_tenant_llm",
                "dream_history",
                "dream_decisions",
            }
        )
        == _CROSS_SCOPE_TOOLS
    )


def test_a_declared_scope_that_is_OPTIONAL_is_also_declared(tool_schemas: dict[str, dict]) -> None:
    """The gap the first tripwire leaves open: declaring a scope is not reaching the guard.

    `run_due_dreams`, `run_dream_job` and `coherence_incidents` default `scope_kind` to `""`
    and read the empty pair as *every scope in the store*. They satisfy
    `test_every_tool_is_scoped_or_declared` -- `scope_kind` is right there in the schema --
    and skip `_scope` completely by omitting it. A tripwire whose passing condition is met
    by the exact move that defeats it is not a tripwire, so the optional set is enumerated
    too, and `mcp_auth.require_explicit_scope` refuses the omission under the guard.
    """
    optional = {
        name
        for name, schema in tool_schemas.items()
        if "scope_kind" in (schema.get("properties") or {}) and "scope_kind" not in set(schema.get("required") or [])
    }
    assert optional == _OPTIONAL_SCOPE_TOOLS, (
        f"tools with an OPTIONAL scope changed: {sorted(optional)} vs declared "
        f"{sorted(_OPTIONAL_SCOPE_TOOLS)}. Each one goes fleet-wide when the caller omits "
        "the scope; it needs a require_explicit_scope call in its body."
    )


def _called_names(node) -> set[str]:
    """Every plain-name function called anywhere inside `node`."""
    import ast

    return {
        child.func.id for child in ast.walk(node) if isinstance(child, ast.Call) and isinstance(child.func, ast.Name)
    }


def _tool_bodies() -> dict:
    import ast
    import inspect

    import memotron.mcp_server as server

    tree = ast.parse(inspect.getsource(server))
    return {node.name: node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)}


def test_every_scoped_tool_actually_ROUTES_THROUGH_scope(tool_schemas: dict[str, dict]) -> None:
    """THE FOURTH CATEGORY, and the one the other tripwires cannot see.

    Declaring `scope_kind` is not the same as reaching the guard, and the two tests above
    only ever ask about the DECLARATION. A tool with a *required* `scope_kind` that builds
    its own `MemoryScope(kind=..., scope_id=...)` instead of calling `_scope` satisfies
    every one of them and never authorizes anything.

    That is not hypothetical. An independent reviewer added exactly such a tool to this
    server and all six of the original tests passed. The tripwire's passing condition --
    "the schema mentions a scope" -- was met by the precise move that defeats the guard,
    which is the failure this whole file was written to prevent, reproduced inside it.

    So the claim is made against the BODY: every scoped tool must contain a `_scope` call.
    `_scope` is the only place a `MemoryScope` may be constructed in this module, and
    `test_scope_is_the_only_place_a_MemoryScope_is_built` holds that line from the other
    side -- together they close the route.
    """
    bodies = _tool_bodies()
    scoped = {name for name, schema in tool_schemas.items() if "scope_kind" in (schema.get("properties") or {})}

    missing = sorted(name for name in scoped if name in bodies and "_scope" not in _called_names(bodies[name]))
    assert not missing, (
        f"{len(missing)} tool(s) declare a scope and never call _scope: {missing}. They are "
        "reachable for every tenant by any caller despite looking guarded -- route them "
        "through _scope rather than building a MemoryScope directly."
    )


def test_scope_is_the_only_place_a_MemoryScope_is_built() -> None:
    """The other half of the same claim, and the one that survives a rename.

    The test above asks "does this tool call `_scope`". This one asks "can a tool get a
    scope any other way at all" -- which is the question that stays answered if someone
    adds a `_scope_v2`, a module-level helper, or an inline construction inside a tool that
    also happens to call `_scope` somewhere else.
    """
    import ast
    import inspect

    import memotron.mcp_server as server

    tree = ast.parse(inspect.getsource(server))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) or node.name == "_scope":
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "MemoryScope":
                offenders.append(f"{node.name}:{child.lineno}")
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr == "from_key"
                and isinstance(child.func.value, ast.Name)
                and child.func.value.id == "MemoryScope"
            ):
                offenders.append(f"{node.name}:{child.lineno} (from_key)")

    assert not offenders, (
        f"MemoryScope is constructed outside _scope at {offenders}. _scope is the "
        "authorization choke point; a scope built anywhere else bypasses it silently."
    )


#: How each optional-scope tool guards the OMISSION of its scope. Two forms are
#: acceptable, and WHICH ONE is recorded per tool rather than left to whichever guard
#: happens to appear in the body -- otherwise "it calls something" passes for "it is
#: guarded", which is the shape of every hole this file exists to catch.
#:
#: * ``require_explicit_scope`` -- refuse the omission outright once identity is armed.
#: * ``require_fleet_wide_read`` + ``require_fleet_wide_write`` -- RESTRICT the omission
#:   instead, for tools where a fleet-wide run is a legitimate operator action. Both are
#:   required together: the read arm alone leaves the apply path unguarded, and the write
#:   arm is the stricter of the two because it refuses even when identity is DISARMED,
#:   where ``require_explicit_scope`` returns early.
_OMISSION_GUARDS: dict[str, frozenset[str]] = {
    "run_due_dreams": frozenset({"require_explicit_scope"}),
    "run_dream_job": frozenset({"require_explicit_scope"}),
    "coherence_incidents": frozenset({"require_explicit_scope"}),
    # B3 gave this a scope and moved it out of `_CROSS_SCOPE_TOOLS`. A bounded call is
    # authorized by the ordinary scope guard; the unscoped sweep keeps the stricter pair.
    "remediate_coherence": frozenset({"require_fleet_wide_read", "require_fleet_wide_write"}),
}


def test_every_optional_scope_tool_actually_guards_the_omission() -> None:
    """The declaration above is a list; this asserts the list was acted on.

    Reading the source is the right instrument here and the wrong one for the registry
    questions: whether a specific call appears in a specific function body is something
    `list_tools()` cannot answer at all. Without it, adding a name to
    `_OPTIONAL_SCOPE_TOOLS` and nothing else would make the tripwires green while the tool
    stayed fleet-wide.

    KNOWN LIMIT: this only asks that the calls appear *somewhere* in the function, so one
    misplaced inside the `if scope_kind and scope_id:` branch would satisfy it while
    guarding nothing. The real proof is the parametrized HTTP-level test in
    `test_mcp_identity_middleware.py`, which calls each of them by name with the scope
    omitted and asserts the refusal.
    """
    assert set(_OMISSION_GUARDS) == _OPTIONAL_SCOPE_TOOLS, (
        f"every optional-scope tool needs its omission guard recorded: "
        f"{sorted(_OPTIONAL_SCOPE_TOOLS - set(_OMISSION_GUARDS))} unaccounted for, "
        f"{sorted(set(_OMISSION_GUARDS) - _OPTIONAL_SCOPE_TOOLS)} stale"
    )
    assert all(_OMISSION_GUARDS.values()), "an empty guard set would let a tool pass unguarded"

    bodies = _tool_bodies()
    missing = []
    for name, required in sorted(_OMISSION_GUARDS.items()):
        node = bodies.get(name)
        if node is None:
            missing.append(f"{name} (no such function)")
            continue
        absent = required - _called_names(node)
        if absent:
            missing.append(f"{name} (missing {sorted(absent)})")
    assert not missing, f"declared optional-scope but never guards the omission: {missing}"


def test_the_registry_is_not_trivially_empty(tool_schemas: dict[str, dict]) -> None:
    """The positive control.

    Every assertion above is a `not` over a set difference, and all of them pass vacuously
    if `list_tools()` returns nothing -- an import error, a renamed decorator, a server that
    registered no tools. Without this, a completely broken server is indistinguishable from
    a perfectly guarded one.
    """
    assert len(tool_schemas) >= 35, f"only {len(tool_schemas)} tools registered; expected ~39"
    scoped = [n for n, s in tool_schemas.items() if "scope_kind" in (s.get("properties") or {})]
    assert len(scoped) >= 30, f"only {len(scoped)} tools take a scope; the guard would cover almost nothing"
