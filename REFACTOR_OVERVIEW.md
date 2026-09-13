# Refactor overview — `jedai/memotron`

Step 0 of a staged, subsystem-by-subsystem refactor. This file is the living map: the
inventory, the ordering, the cross-cutting concerns, the status table, and the log of every
revision to the plan. It is updated at the end of each subsystem pass.

**Baseline: `667e748`** (PR #131 merged 2026-09-02). `src/memotron` 73,307 lines / 156 files ·
`tests/` 47,474 / 89 (1,391 tests) · `scripts/` 12,880 / 57 · `check.sh` 16/16 PASS with the
Postgres DSN set.

> **Behaviour preservation is non-negotiable.** Public APIs, CLI interfaces, data formats,
> side effects and error semantics stay identical unless separately approved. Any "best
> practice" that would change behaviour is flagged, not applied.

---

## 1. This is not a greenfield refactor

Two large refactors already merged. **PR #103** (readiness) and **PR #131** (`667e748`,
268 commits). What they already did — and therefore what this programme must not redo:

| Already done | Detail |
|---|---|
| Seven god modules split into packages | `dreaming.py` 10,533→17 files · `client.py` 8,826→16 · `storage/sqlite.py` 5,709→9 · `config.py` 4,361→15 · `agent_memory.py` 3,821→18 · `admin_server.py` 3,025→9 · `models.py` 1,826→11. Rule: *zero behaviour change, zero import change*. |
| Tooling | uv · `src/` layout · `py.typed` · ruff (24+ rule families, complexity ratchets) · mypy (119–125 strict modules, per-module lenient tier with a justification per suppressed code) · pytest + coverage (134 per-module floors) · pre-commit · Harness CI |
| Verification | `scripts/check.sh`, 16 lanes, exit contract 0 pass / 1 fail / 2 harness / 3 declined |
| Structural work | coupling metric replaced · `mixin_dag` extended to 5/5 packages · six OpenAI transports unified onto one base · `storage/_shared/` de-duplicating 13 twin methods |

### Phase 2 (foundation/tooling) is COMPLETE — it is a verification step, not work

Everything the standard Phase 2 asks for exists and is gating commits today. Re-running it
would be churn. **Phase 2 reduces to one command** (see §7).

The one genuine gap is CI depth, and it is not a code change — see cross-cutting **X1**.

---

## 2. Subsystem inventory

| # | Subsystem | Path | Purpose | Lines | Files | Role (fan-in / fan-out) |
|---|---|---|---|---:|---:|---|
| 1 | `models` | `src/memotron/models/` | Pydantic data models for the memory graph — scopes, relationships, episodes, enums | 2,287 | 11 | **foundation** 21 / 0 |
| 2 | Substrate | `gateway.py` `embedding.py` `synthesis.py` `agents.py` `extraction.py` `multimodal.py` `runtime.py` | LLM transports and the runtime that assembles them | ~3,200 | 7 | foundation-like |
| 3 | `config` | `src/memotron/config/` | `DreamConfig`, `Motive`, governance policy, prompt profiles, tenancy | 4,960 | 15 | **foundation** 14 / 4 |
| 4 | `storage` | `src/memotron/storage/` | Every persisted byte: `StorageBackend` contract, SQLite + Postgres engines, receipt ledger | 17,371 | 34 | 11 / 6 |
| 5 | Provenance | `replay.py` `epochs.py` `migration.py` `erasure.py` `crypto.py` | WS-11/12/13/26 — derivation DAG, replay, crypto-shred erasure, tenant migration | ~4,000 | 5 | mid |
| 6 | `agent_memory` | `src/memotron/agent_memory/` | Agent-facing memory facade over the SDK | 4,477 | 18 | hub 8 / 16 |
| 7 | `dreaming` | `src/memotron/dreaming/` | `DreamEngine` — formation, consolidation, rollups, contradiction resolution | 11,605 | 17 | orchestration 4 / 15 |
| 8 | `client` | `src/memotron/client/` | `Memotron` — the primary SDK client | 9,951 | 16 | orchestration 2 / 22 |
| 9 | Product surfaces | `certification.py` `context.py` `interop.py` `coherence.py` `retrieval.py` `memory_bank.py` `session.py` `transcripts.py` `health.py` `redaction.py` `identity.py` `attestations.py` `platform_client.py` | Feature workstreams layered over the core | ~8,000 | 13 | mixed |
| 10 | `admin_server` | `src/memotron/admin_server/` | Admin HTTP surface and platform API | 3,299 | 9 | 1 / 6 |
| 11 | Entrypoints | `cli.py` `mcp_server.py` `worker.py` `local_platform.py` `agent_memory_mcp.py` `adoption.py` | Process entrypoints | ~4,100 | 6 | **fan-in 0** |
| 12 | `prompts` | `src/memotron/prompts/` | Prompt library | 206 | 2 | leaf |

**21 files exceed 1,000 lines.** Largest: `certification.py` 2,255 · `admin_server/__init__.py`
2,205 · `storage/postgres/_governance.py` 1,457 · `storage/receipts.py` 1,420 · `mcp_server.py`
1,362 · `dreaming/_rollups.py` 1,329. The first two are the structural outliers — every other
large package was decomposed into `_*.py` modules; these two were examined and deliberately not
split (see §4 priors).

### Dependency graph

```
foundation      models(21/0)   gateway(12/0)   crypto(8/0)   identity(3/0)   redaction(6/0)
                     |             |
                config(14/4) ── embedding(9/1) ── extraction(9/3) ── runtime
                     |                                   |
                 storage(11/6) ─────────────────────────-┘
                     |
mid            provenance: replay · epochs · migration · erasure
                     |
hub              agent_memory(8/16)
                     |
orchestration    dreaming(4/15)      client(2/22)      product surfaces
                     |                    |
delivery         entrypoints: cli · mcp_server · worker · local_platform   (fan-in 0)
```

Verified by `scripts/verify/coupling_report.py`, which **PASSES at baseline**:

* **3 import cycles**, all baselined — the 8-node facade cycle (**only 2 real back-edges**:
  `erasure.py:43`, `replay.py:32` import the package facade), `receipts↔storage` (a compat
  shim), `config↔prompts` (already broken at runtime by a lazy import at `config/_prompts.py:94`).
* **1 upward tier edge** — `config` (core) → `storage` (infra) at `config/_dream_config.py:61`
  and `config/_storage_env.py:14`.
* **Mixin idiom** in six packages; `mixin_dag.py` PASSES. `storage/sqlite` and
  `storage/postgres` each carry one *accepted domain* cycle, recorded edge-by-edge with reasons.

---

## 3. Refactor order

Foundation first, so downstream code is refactored against already-cleaned interfaces; the
most-downstream units last, so they are reworked once rather than twice.

**Revised and approved 2026-09-02** — see §9. Principle: *restore the instrument before using
it* · *decisions before the work they govern* · *deep providers before shallow consumers,
entrypoints last*.

| Order | Unit | Rationale | Honest expected yield |
|---|---|---|---|
| 0 | Repo-wide verify | Confirm Phase 2; capture the 16-lane baseline with the DSN set | n/a — verification, not work |
| 1 | **Structural zero** | Kill all 3 cycles + the tier edge, then re-baseline `coupling_report.BASELINE` to zero. Converts a gate passing at a non-zero baseline into a **real ratchet** for everything after | **Low in lines, high in leverage** |
| 2 | **`storage` twins** | Deepest provider (fan-in 11), 14,253 lines across both engines, and the only surviving mechanical de-duplication | **Medium** — 4–6% of lines, but each unified twin permanently deletes one instance of the `anchor` bug class |
| 3 | **#150 `_policy` pilot** | A **decision step, not a refactor.** Its verdict governs units 4–6 | **Possibly nothing — a legitimate result.** Kill threshold declared before starting |
| 4 | `dreaming` | Largest orchestrator; first unit whose plan depends on unit 3 | **Low to possibly-nothing.** Highest risk of a second #119 — timebox it |
| 5 | `client` | Consumes `storage` and `dreaming`, so after both | **Medium** on the ~40 lazy imports; low elsewhere |
| 6 | `agent_memory` | Depends on `client` | **Possibly nothing.** Flattest DAG, passes every gate |
| 7 | `config` + `prompts` | Mostly consumed by unit 1 | **Low** |
| 8 | Substrate + Provenance | Mid-tier; partly touched by units 1–2. Transports just unified, so audit as *verification of a live change* | Low–medium |
| 9 | Entrypoints (incl. `admin_server`) | Fan-in 0 — **can be pulled forward at zero cost if a unit stalls** | **Medium, as bug-finding** — see X8 |
| 10 | `certification` + product-surface long tail | The last unexamined tier inversion (X9) | **Low** — three type relocations at most |
| — | ~~`models`~~ | **DROPPED.** Fan-in 21 / fan-out 0, splitting ruled out, and the `DreamConfig` measurement confirms no consumer over-reaches | Auditing it is not a good use of a pass |

Four units have an honest expected outcome of *no change needed* (`dreaming`, `agent_memory`,
`admin_server` structurally, `prompts`). Stating that up front is deliberate: an audit that
must find something will.

---

## 4. Priors — the stop-list

Recorded conclusions, several of which cost multiple measurement passes. A candidate whose
**subject** matches one of these stops immediately and reads the record before spending
further effort (protocol in §5).

| Prior | Verdict | Record |
|---|---|---|
| Decomposing `_materialize_episode` (1,075 lines) | *"Not a long function, a mutable state machine. Needs a rewrite, not a refactor."* Cost two passes | **#119**, closed `wontfix` on creation specifically to prevent a third |
| Admin payload signature-dispatch | Claimed 347 extractable lines; **measured 2**. Produces zero mypy errors where explicit kwargs catches a typo | STATE.md |
| Splitting `certification.py` / the admin handler | *"Already made of small pieces that happen to share a file."* | `docs/BRANCH-OVERVIEW.md` |
| Splitting `models` / `config` further | *"Shared vocabulary, correctly shared."* | STATE.md |
| Ports / microkernel over the storage core | Breaks the `transaction()` atomicity contract | `docs/design/microkernel-plugin-architecture.md` — `ASSESSED, DEFERRED`, with checkable revisit preconditions |
| Mixin repartitioning | Crosses 54.8% of call weight; best greedy refit 41.5% at 25 relocations; ceiling ~30% | STATE.md |
| Reorganising `tests/` | 4,234 golden records keyed to test paths | `docs/BRANCH-OVERVIEW.md` |
| **The god-module split did not decouple** | `client→storage` sites 279→279 · distinct members 72→72 · private reaches 11→11 | `coupling_report.py` |

**The last row is the load-bearing one.** 35,000 lines were repartitioned and every coupling
metric was flat. Any claim that a split will decouple something must say why this time differs.

### Killed during this inventory pass — measured 2026-09-02, do not re-audit

| Candidate | The number that killed it |
|---|---|
| *"`DreamConfig` is a god-object leaking into `dreaming`"* | 30 members; `dreaming` reads 22, `client` 15. An engine reading its own config. No narrowing available |
| *"`client/` is a thin pass-through over storage"* | Of **239** client methods, **8** are single-statement storage delegations. The 279 call sites are real orchestration |
| *"Unify the MCP and admin surfaces over their ~20 shared operations"* | `memory_evolution` flattens for an LLM at `mcp_server.py:708`; `admin_server/__init__.py:626` builds an aggregating dashboard payload. Unifying couples two independent wire contracts |
| **#127 at programme scale** | 962 raise sites, **72 catch sites, and ZERO that discriminate by message string** — the premise of the issue. Meanwhile **260** `pytest.raises(ValueError)` assertions pin current behaviour. Safe (the repo already has the right idiom: `GatewayRequestError(ValueError)`) but **not valuable at 962 sites for 72 consumers** |
| *"sqlite is missing `_mark_scope_dirty`"* | It is a Postgres-only deferred-write concurrency memo (`postgres/_graph.py:878`), not a parity gap |

### Measured this pass — the twins are not copy-paste

Of **158** same-named method pairs across the two engines, exactly **1** has an AST-identical
body and **0** differ only in string literals. **`storage/_shared/` is finished at its declared
bar.** But one widening remains: normalising only the *query seam* —
`self._connection.execute(..., "?")` vs `self._engine.fetchone(..., "%s")` — collapses **21 twin
pairs to byte-equality** (12 of them in `_epochs`), with **35 more at 0.93–0.997** similarity.
Both engines are fully synchronous there, so no async bridge is needed. This is the only
remaining mechanical de-duplication in the repo, and 21 is an **upper bound** from a normaliser
that deliberately conflated `execute`/`fetchone` — each must be re-verified individually.

---

## 5. Audit method — how a finding earns its place

The failure mode here is not missed findings; it is **plausible findings that collapse later**.
Every documented refutation has one of three shapes:

1. **Size measured, change assumed** — the extent of the surface reported as the extent of the improvement (347→2, ~300→138, 279→279).
2. **A number invented for the decision** — the 150-line transports threshold, which killed work that then landed and found a sixth transport.
3. **The instrument answered on the wrong input** — SKIP-as-PASS, parity-cov fed another tree's `coverage.json`, `mypy_suppressions` defeated by ANSI colour. **None errored; all three answered.**

So: measure the delta not the surface · the falsifier must be *sourced*, never chosen · no
reading is admissible until the instrument is shown able to say something else.

### Candidate lifecycle

| Stage | Cost | Requirement |
|---|---|---|
| **A — Subject** | minutes | State the finding as a claim that can be **false**. Then match it against the prior stop-list (§4) before spending anything |
| **B — Reading + control** | minutes | `N_now` from an instrument that already exists, plus its **denominator** (proof it looked at something) and a **control** — one deliberate perturbation that moves the reading, then reverted. **No control, no admission** |
| **C — Probe** | ≤1 hour, discarded | `N_after` is **measured, never forecast**. Do the smallest real instance in a throwaway worktree, re-read, discard. Measure *both* sides — #119's first pass measured only inputs and missed what killed it |
| **D — Falsifier** | — | Compare Δ against a **sourced** falsifier |

**Falsifier sources, in descending strength — there is no fifth:**
1. A gate constant the change would itself violate (`max-args = 20`, `max-statements = 191`, `max-branches = 39`, `max-complexity = 38` in `pyproject.toml`). This is what killed #119.
2. An existing committed baseline (`coupling_report.BASELINE`, the 134 `coverage_floors`, `mypy_suppressions.baseline.tsv`, `parity_coverage.BASELINE`, `api_surface.golden.txt`).
3. A distribution measured during this audit, with its shape reported.
4. Sign only — *"does any gated number move at all?"* Direction, never magnitude.

### Falsifying measurement per category

| Category | Naive measure — do not use | The number that kills the finding |
|---|---|---|
| God module | lines, method count | Zero movement on any gated `coupling_report` row. Legal survivor: **navigability**, priced as navigability and never sold as decoupling |
| God function | lines | Proposed signature ≥ `max-args = 20`, **or** any produced local is re-written later in the enclosing scope |
| Duplication | size of the similar region | Edit fan-out does not fall; **or** the planted-defect test — plant the divergence the duplication permits; if the unified form catches *less*, NO_GO regardless of lines |
| Tight coupling | call-site counts (already refuted in-tree) | Provider is core-tier → *"shared vocabulary, correctly shared"*, void unless it is an upward edge or a cycle. Also void if the fix is a lazy import, which removes the statement not the dependency |
| Missing types | count of missing annotations | Surfaces **zero** new errors and retires **zero** baseline rows → cosmetic |
| Dict-shaped data | 502 `dict[str, Any]` | Zero new errors at consumers **and** the dict never leaves its module |
| Exception taxonomy | 962 `raise ValueError` | The hierarchy still subclasses `ValueError` and **no `except` site is rewritten**; or no consumer can be named that wants to branch |
| Dead code | grep for references | Covered by ≥1 test **or** present in the api-surface golden **or** appears as a string literal anywhere. Any single hit → drop |
| Test gaps | coverage percentage | The proposed test passes against deliberately broken source. Any test named for an ordering must be shown RED first |

**Edit fan-out** is the measure that was missing when line-count wrongly killed the transports
work: *for one named semantic change, how many files must change together, and is a complete
list discoverable?* Admissible only as a counted list of sites, not an impression.

### No new gates

You have said twice that the validation layer is outgrowing the product (87 gate commits vs 102
src commits; `scripts/` +11,786/−131 with nothing retired). Every instrument named above is
already committed and already run by `check.sh`. The only additions are *steps in the audit* —
the control run and the discarded probe. Audit output routes through the **existing**
`scripts/verify/finding_gate.py`, which already fails evidence-free rows and comparisons that
do not name their control.

### Prior reconciliation

Build a **Prior Index** of `(subject, verdict, location)` — identifiers only, no reasoning —
then audit **blind**. On subject match, stop and record one of three verdicts:

* **CONFIRMS** — independent pass reached the same place. Cite and stop. Cost bounded at one pass.
* **CONTRADICTS** — name the specific number in the record that is now false, and **re-run that record's own command** to show today's value.
* **NEW ANGLE** — same subject, different lever; must quote the scope boundary being stepped outside. #119 hands this over explicitly: it measured one function; the other 29 functions ≥150 lines are unchecked.

**Priors rot** — #119 records that its own original blocker had gone stale by the time it was
re-checked. Re-running a prior's load-bearing number costs one command and is not optional.

### "No change needed" is the default verdict

Findings are admitted, not assumed. A unit that admits nothing still produces a plan file,
because the record is the deliverable. It must contain the instruments run *with denominators*
(otherwise the next reader cannot distinguish "nothing found" from "nothing looked"), each
candidate with the number that killed it, checkable revisit preconditions, and a *where this is
wrong* paragraph. Anything that cost more than one pass escalates to a closed-on-creation
`wontfix` record in the #119 style.

### Defects leave the pipeline

Any behaviour defect found during an audit is filed as a bug and fixed **alone**, before
structural work touches that file. The reason is mechanical: `pure_move.py` only proves a move
if the body is unchanged, so a fix riding inside a refactor destroys the only proof available.

---

## 6. Cross-cutting concerns

| id | Concern | Resolved by |
|---|---|---|
| **X1** | **CI tests neither Postgres nor any `check.sh` lane.** `Memotron-Build_Check` runs `ci-build-check.sh` (hermetic pytest only); its Postgres lane self-reports SKIPPED because no DSN is set. Verified at the Harness account level — zero matches across 355 variables. A green check says nothing about the backend now in production | **Not a subsystem.** A `platform-cicd` change (Postgres service dependency + DSN on the Build Check stage). Raise separately |
| **X2** | ~~#150 mixin idiom~~ — **CLOSED 2026-09-02 as a decision record** (`NOT_PLANNED`). Premise intact and *growing* (`_protocol.py` 1,116→**1,219**, suppressions 89→**93**), but **two real conversions** priced it: 8.1 lines per delegated member, and four of five packages are 2.9–23:1 net-negative | **Resolved — no unit carries this.** The fifth (`dreaming`, 0.55:1) is thin, **unproven** (the conversion failed), and blocked by X11 |
| **X3** | ~~#127 exception taxonomy~~ — **CLOSED 2026-09-02 as a decision record** (`NOT_PLANNED`). 962 raise sites, **72 catch sites, zero discriminating by message**. The taxonomy is safe but has no consumer | **Resolved — no unit carries this.** Retitled to state the claim killed. Reopens on one named consumer, or if #13 needs the distinction for HTTP status mapping. Note **215 of 260** `pytest.raises` use `match=` — recorded on the issue as the fact most likely to overturn it |
| **X4** | **#128 typed payloads** — 502 `dict[str, Any]`, 247 bare `Any` | Applied per unit at module boundaries, targeting `arg-type` (32 suppressions) not `no-any-return` (21) |
| **X5** | **#151 `api_surface.py` blind spots** — records neither instance attributes nor module re-exports | Absence from the golden proves nothing. Constrains the dead-code and public-surface tests everywhere |
| **X6** | **Compat shims are load-bearing, not dead.** `memotron.graph` (25 lines) has 34 references, `memotron.receipts` (21) has 74 | Retiring them is a 108-site migration. Unit 9 decides; do not treat as a cheap win |
| **X7** | **FILED as #154.** The admin server puts `str(exc)` in the response body from two bare `except Exception` handlers (`admin_server/__init__.py:310`, `:376`) — not 6 sites; the other four are the correct `HttpApiError` branch or event logging | **Resolved into an issue.** Survives #126 being fixed: auth controls *who* reaches it, this controls *what an error tells them* |
| **X8** | ~~`mcp_server.py` hand-rolled payloads~~ — **REFUTED, unit 9.** `attr-defined` is live on that module (#132's fix removed the suppression); both shapes of the original `s.value` bug were planted and caught. `--disallow-any-expr` found 1 `Any` in a payload span, and it is a guarded `hasattr` access | **No action.** Converting the 32 payloads is style with wire-contract risk and no correctness benefit |
| **X9** | **`certification.py` is an accidental vocabulary provider.** The production `client/_certification.py` lazily imports `PolicyAliasState`, `PolicyContractVersion`, `PolicyShadowStage` from a module that also carries an OpenAI transport, two LLM judges and three dataset adapters | Unit 9, as a **type relocation to `models`**, not a split. Likely explains most of the 19 lazy imports in `client/_certification.py` |
| **X11** | **FILED as #153.** `client/` and `agent_memory/` reach 6 `DreamEngine` private members across **10 call sites in 5 files** (corrected from "10 files") | **Resolved into an issue.** Direct cause of the #150 conversion failing |
| **X10** | **Verification traps.** `pure_move.py` **cannot** prove the storage twin work (bodies genuinely change) and **will fail import hoisting by design** — its docstring lists a hoisted local import as a bug it catches. Separately, `parity_coverage.py`'s population *shrinks as twin unification succeeds*, so the gate silently weakens unless each commit records the expected population drop | Every unit must name which instrument proves it, and say when `pure_move` is deliberately not used |

---

## 7. Verification

Phase 2 is a verification step, not work:

```bash
export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=memotron \
  password=local-dev-only dbname=memotron"
bash scripts/check.sh
```

**Baseline to hold: 16/16 PASS, 1,391 tests** (verified on `667e748`).

Two environment facts, or every reading is wrong:

* `check.sh` does **not** source `.env`, where the DSN lives (`.env:62`). Unset, `postgres` and
  `parity-cov` skip, Postgres modules land at 18–51%, and **`cov-floor` then fails** for a
  reason that has nothing to do with the code.
* Run under `NO_COLOR=1 TERM=dumb`. `FORCE_COLOR=3` once made 237 mypy error lines parse as zero.

The full suite runs before any unit is called done — interface changes ripple. For pure moves,
`pure_move.py` proves AST/bytecode identity across refs, which is stronger than a passing suite.

---

## 8. Status

| # | Unit | Status | Plan file | Summary |
|---|---|---|---|---|
| 0 | Repo-wide verify | **done** | — | 16/16 PASS, 1,391 tests, at `667e748` |
| 1 | Structural zero | **done** | `refactor/plans/structural-zero.md` | SZ-1 + SZ-2 landed (`c2df472`, `7ec1fb0`, `fa02fb6`): cycles **3→1**, modules **12→2**, largest **8→2**, upward tier edges **1→0**. Both ratchets proved by control. Remaining cycle (`config↔prompts`) is unit 7's by decision |
| 2 | `storage` twins | **done — NO ACTION** | `refactor/plans/storage-twins.md` | Seam yields **9** methods / 54 lines (prior said 21), 0.4% of 14,253. The 9 that unify are simple fetchers; the `anchor` divergence lived in `_operational.py`, which does not unify |
| 3 | #150 composition | **done — #150 CLOSED** | `refactor/plans/150-composition-pilot.md` | Two Tier-C conversions run. `_policy` clean (+81 lines/10 methods). `_decisions` **failed** — 259 tests, cross-package private access. Issue closed as a decision record |
| 4 | `dreaming` | **next** — audit *excluding* the mixin idiom | — | — |
| 5 | `client` | not started | — | — |
| 6 | `agent_memory` | not started | — | — |
| 7 | `config` + `prompts` | not started | — | — |
| 8 | Substrate + Provenance | not started | — | — |
| 9 | Entrypoints (incl. `admin_server`) | **done — NO ACTION + 1 defect** | `refactor/plans/entrypoints.md` | X8 refuted by planted control. Found and filed **#154** (exception echo); live-verified #126's exposure |
| 10 | `certification` + long tail | not started | — | — |
| — | `models` | **skipped** | — | Fan-in 21 / fan-out 0; splitting ruled out and independently reconfirmed. Auditing it is not a good use of a pass |

---

## 9. Plan changes

Every revision to this plan is recorded here with date, what changed, and why. Newest first.

### 2026-09-02 — unit 1 complete (partial), SZ-2 deferred

SZ-1 landed. **SZ-2 deferred by decision** ("SZ-1 alone for now") — not rejected, unscheduled.
Two consequences carried forward:

* **The coupling ratchet is now live but not at zero.** Baseline is cycles 1 / modules 2 /
  largest 2 / upward 1. Units 2–10 are guarded against reintroducing what unit 1 removed —
  proved by control, exit 1 on regression — but `upward tier edges` remains ungated at 1, so a
  *second* upward edge would be caught while the existing one stays.
* **Unit 7 inherits two things**, not one: the `config↔prompts` cycle (needs `DreamPromptProfile`
  relocated, against an in-code decision record) and now SZ-2 as well, since both are
  `config`-boundary questions and both turn on relocating a type out of a package. Consider
  taking them together when unit 7 is planned.

**Method correction applied to all later units:** a probe must run every lane the resulting
commit has to pass. Mine ran `pytest` and `api_surface` but not `ruff`, and `lint` failed on
first execution.

### 2026-09-02 — REVISION APPROVED AND APPLIED

Approved by Ryan. §3 and §8 now carry the revised order. Two adjustments made while applying
it, both mechanical rather than substantive: the proposal covered 9 steps against the
inventory's 12 units, so **Substrate and Provenance were placed at unit 8** (mid-tier, partly
consumed by units 1–2, and the transport plane is audited as *verification of a live change*
since the six-transport unification only just landed); and **`admin_server` was folded into
Entrypoints (unit 9)** rather than standing alone, since everything structural about it is
ruled out and its one live question — serialisation — is `mcp_server`'s (X8), not its own.

The diff that was approved:

| | Approved order | Proposed order |
|---|---|---|
| 1 | `models` | **Structural zero** (new) — kill all 3 cycles + the tier edge |
| 2 | Substrate | **`storage` twins** (was 4) |
| 3 | `config` | **#150 `_policy` pilot** — a *decision* step, not a refactor (was inside 4) |
| 4 | `storage` | `dreaming` (was 7) |
| 5 | Provenance | `client` (was 8) |
| … | … | `agent_memory` · `config`+`prompts` · Entrypoints · `certification` |
| — | `models` at 1 | **`models` dropped entirely** |

**Why the new step 0 leads.** Fixing all 3 cycles and the tier edge takes cycles 3→0 and tier
edges 1→0, which lets `coupling_report.BASELINE` be re-baselined to zero. **That converts a
gate which currently passes at a non-zero baseline into a real ratchet for every later step** —
a non-zero baseline cannot distinguish "the cycle we accepted" from "the cycle step 4
introduced". It sharpens an existing gate rather than adding one, which is the constraint we
are under.

**Verified individually, and it is not uniformly 4 lines.** Three of the four sites are as
described and trivial:

* `storage/base.py:157` — `from memotron.receipts import ReceiptLedger`. Infra importing its
  own compat shim; retarget at `memotron.storage.receipts`. Inarguable.
* `erasure.py:43` and `replay.py:32` — both `from memotron import receipts as receipts_mod`.
  These are the **only two real back-edges** in the 8-node cycle; retargeting collapses it.

The fourth is **not** trivial and the proposal to make the lazy import eager runs against an
in-code decision record. `config/_prompts.py:3-4` states: *"The lazy import of
`memotron.prompts` inside `default_prompt_profiles` is **load-bearing and stays
function-local**: `memotron.prompts` imports config back."* The reverse edge is
`prompts/__init__.py:15` importing `DreamPromptProfile`. Relocating that type to `models` would
genuinely remove the need for the lazy import — but that is a **design change with a documented
decision against the naive form**, not a one-line fix. It reconciles as **NEW ANGLE** (relocate
the type) versus the record's subject (keep the import lazy), and must be argued as such rather
than applied. If it is refused, cycles go 3→1, not 3→0, and the re-baseline is partial.

**Why `models` is dropped.** Fan-in 21, fan-out 0, splitting already ruled out, and the
`DreamConfig` measurement independently confirms no consumer over-reaches. Auditing it is not
a good use of a pass. My earlier argument — that it is a cheap way to exercise the process —
is weaker than it looked, because Structural zero exercises the process *and* produces value.

**Why the #150 pilot is promoted to a decision step.** Its verdict governs whether mixin work
in `dreaming`/`client`/`agent_memory` is worth planning at all. Run it before them, with the
kill threshold declared in advance (e.g. *"if `_protocol.py` shrinks by fewer than 40 lines per
20 delegates, the idiom stays and #150 closes as a decision record"*).

**One scheduling freedom worth keeping.** Entrypoints have fan-in 0, so that unit can be pulled
forward at any time at zero cost if a step stalls.

### 2026-09-02 — initial inventory

Created at `667e748`. Ordering is dependency-driven per the agreed default: foundation first,
most-downstream last.

**Two figures were re-measured** against the open issues, whose numbers were taken on
`split-models` before both merges: #127 1,023→**962**; #128 500/248→**502/247** (unchanged in
substance); #150 1,116→**1,219** and 89→**94** — *grown*, which is new information the issue
does not carry and an argument for taking #150 earlier rather than later.

**Two inventory corrections** against the first draft: "top-level modules" was a bucket of 33
files, not a subsystem — measured by inter-import it decomposes into four cohesive units
(Substrate, Provenance, Product surfaces, Entrypoints), now units 2/5/9/11. And the two compat
shims that looked like dead-code quick wins have 108 references between them (X6).
