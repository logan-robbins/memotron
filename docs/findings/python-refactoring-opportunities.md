# Python refactoring opportunities

Assessed on `main` at `a51d3ff` on 2026-09-09.

## Overall assessment

Memotron already has a strong Python foundation: Python 3.12, a `src/` layout, `uv`,
Ruff, Pydantic-aware mypy, pytest, coverage ratchets, explicit storage contracts, and
coupling checks. The highest-value work is targeted cleanup at existing module seams—not
another broad rewrite.

Observed on this checkout:

- 163 Python source files and approximately 77,800 source lines.
- Ruff passes.
- The coupling gate passes with zero upward tier edges and one remaining two-module cycle.
- 125 modules are in the strict mypy tier; 79 suppressed error-code entries remain.
- An aspirational Ruff pass using complexity 15 and 80 statements found 87 hotspots.
- Mypy was not independently run because its executable was absent from the local environment.

## Recommended sequence

### P1 — Remove private client-to-engine reaches

`client/` and `agent_memory/` still call private members of `DreamEngine`, including
`_secret_reference_metadata`, `_reject_raw_secret_memory`, `_rebridgeable_row`, and
`_supersede_target_relationships`.

This is real coupling rather than a naming concern: an earlier composition experiment broke
259 tests because consumers depended on methods that were not part of an explicit interface.

**Direction:** classify each reach as either a capability that belongs on a narrow internal
interface or a leak that should be removed from the caller. Do not expose every private method
unchanged.

**Done when:** no consumer outside `dreaming/` calls `_engine._private` members.

**Risk:** medium. Authorization and receipt behavior must remain unchanged.

### P1 — Normalize enum values at input boundaries

Several MCP and storage-decoding paths carry validated strings into interfaces typed with
enums such as `ScopeKind`. Runtime coercion works, but mypy suppressions hide mismatches.

**Direction:** convert strings to their domain enum or use existing constructors such as
`MemoryScope.from_key()` at the boundary.

**Done when:** the affected `arg-type` suppressions are removed and invalid values still fail
at the same external boundary.

**Risk:** low.

### P1 — Type payloads at module seams

Dynamic dictionaries are common in receipt fields, MCP payloads, admin request/response
bodies, storage rows, and client results. The problem is not every `dict[str, Any]`; it is
payloads that cross a module interface and cause callers to lose type information.

**Direction:** use `TypedDict` for fixed internal shapes and Pydantic models where runtime
validation is required. Prioritize suppressions for `arg-type` and `no-any-return`. Leave
module-local, genuinely dynamic JSON alone.

**Done when:** each converted payload catches a planted consumer error and its obsolete mypy
suppression is deleted.

**Risk:** medium. Wire formats must remain byte-compatible.

### P1 — Deepen SQLite/Postgres contract testing

Postgres setup helpers are duplicated across approximately nine test files. Important
concurrency and promotion paths still exercise SQLite only, despite production behavior
depending on different Postgres locking and transaction primitives.

**Direction:** extract shared test-only helpers for Postgres DSN validation, schema reset, and
backend construction. Then apply the existing dual-engine parametrization to focused
concurrency and promotion contracts.

**Done when:** duplicated setup is centralized and the parity-coverage ratchet reports fewer
one-engine-only methods.

**Risk:** low to medium. Keep independent expected-value tables independent from production
code.

### P1 — Replace avoidable full-store relationship scans

The `dreaming/` package contains 11 calls to `self._graph.relationships()`. That operation
loads every relationship from every scope. Several callers subsequently filter by a scope
already known to them.

**Direction:** use `relationships_for_scope()` where semantics match. For dependency traversal
or other specialized cases, add purpose-built indexed storage queries rather than moving the
same filtering into another Python helper.

**Done when:** scope-local operations no longer scale with total database size, with behavioral
parity tests on SQLite and Postgres.

**Risk:** medium. Some scans are intentionally fleet-wide and must remain global.

### P2 — Break the `config` and `prompts` import cycle

The remaining import cycle is:

1. `config/_prompts.py` lazily imports `memotron.prompts`.
2. `prompts/__init__.py` imports `DreamPromptProfile` from `memotron.config`.

The lazy import is currently load-bearing; merely hoisting it would break imports.

**Direction:** relocate shared prompt-profile vocabulary to a neutral leaf module, preserve
public re-exports, and then remove the lazy import.

**Done when:** the coupling gate reports zero import cycles and the public-interface golden
remains compatible.

**Risk:** low to medium because model module paths participate in API fingerprints.

### P2 — Make control-plane precedence explicit

`MemoryControlPlane.resolve()` repeatedly overlays tenant, agent, dream-mode, scope, and
request settings while separately updating provenance strings. The method currently measures
38 complexity, 37 branches, and 122 statements.

**Direction:** introduce a typed policy accumulator and explicit ordered layer application.
Keep precedence visible in one sequence rather than distributing it across helpers.

**Done when:** existing precedence tests pass, each layer is independently testable, and the
method's branch count falls materially.

**Risk:** medium. A small ordering change can silently alter effective policy.

### P2 — Add SDK lifecycle management

`Memotron` owns or receives a storage backend but exposes no `close()` method or context
manager. Entrypoints must know to call `client.graph.close()` directly.

**Direction:** add `Memotron.close()` and, if ownership semantics are made explicit, context
manager support. Document whether an injected backend is closed by the client.

**Done when:** callers can release resources without reaching through `graph`, and ownership
tests cover constructed and injected backends.

**Risk:** low if ownership is explicit; otherwise double-close behavior is possible.

### P3 — Enable branch coverage incrementally

Coverage currently measures statements only. Decision-heavy methods can execute every
statement while leaving important branches untested.

**Direction:** measure branch coverage as a separate change, establish honest per-module
baselines, and ratchet upward. Do not impose a repository-wide percentage cliff.

**Done when:** branch coverage is reported and newly changed branches are gated.

**Risk:** medium to high because enabling it changes every baseline simultaneously.

## Work to avoid

- **Splitting files solely because they are large.** Previous splits improved navigation but
  did not reduce measured coupling.
- **Replacing all mixins with composition.** A measured pilot increased code and broke 259
  tests.
- **Mechanically unifying SQLite and Postgres implementations.** Only 9 of 159 same-named
  method pairs normalized cleanly, yielding about 0.4% of engine code.
- **Unifying MCP and admin wire contracts.** They serve different consumers and intentionally
  serialize domain operations differently.
- **Creating a broad exception hierarchy.** No current consumer needs to branch on most of
  those distinctions.
- **Reorganizing the whole test tree.** Golden artifacts are keyed to test paths, so moves
  create substantial churn without changing behavior.
- **Mechanically decomposing `_materialize_episode`.** It is a mutable state machine; the
  existing investigation concluded that extraction would hide rather than reduce complexity.

## Separate defect from refactoring

The migration epoch-visibility issue should be reproduced and, if confirmed, fixed as an
isolated behavior change before structural work touches migration. Combining a defect fix with
a refactor would weaken the repository's behavior-preservation evidence.

## Evidence and limitations

This assessment combines current-tree measurements, source inspection, existing refactor
records, and open issues. Performance impact from the full-store scans was derived from query
shape and call sites; it was not benchmarked. Postgres parity gaps were established from test
selection and source inspection, but no live Postgres suite was run. Re-measure before
committing to any large refactor.
