# Working memory — what we understand about Memotron

> **Line references resolve against `audit-baseline-2026-08-27`** (commit `06391b6`), not
> against `HEAD`. The module split turned `models.py`, `config.py`, `storage/sqlite.py` and
> `dreaming.py` into packages, so 469 of the ~1,100 `file.py:LINE` references below now point
> at files that no longer exist. They are pinned rather than rewritten: their value is *"at the
> commit I audited, this line said X"*, and rewriting makes them wrong about history while
> looking right. Verified: **410 of the 417 stale references (98%) fall inside the file they
> name at that tag.** To read one: `git show audit-baseline-2026-08-27:src/memotron/<file>`.

**Running file. Update it as understanding changes; do not let it drift.**

Purpose: keep the mental model out of a chat transcript. Everything below is either verified
here or cited to where it was verified. If you know something about this system that is not
written down somewhere in this index, it will be lost.

Baseline: `origin/main` = `f28e95c` (PR #44 merged). Branch `takeover`.

---

## Where knowledge lives

| file | holds |
|---|---|
| `STATE.md` (repo root) | verified facts, general rules, open failures, last-session pointer |
| `WALKTHROUGH.md` | the presentable narrative + the numbers that are safe to quote |
| `INFRA-BACKLOG.md` | the infra-team handoff: provisioning inventory + defects only they can close |
| `TAKEOVER-BACKLOG.md` | the defect register — 45 items, 5 tiers, provenance-tagged |
| `UNKNOWNS.md` | 11 unknowns (testable, unrun) + 11 open design questions (need a human) |
| `DOGFOOD-LOG.md` | the raw trail — 38 entries, dead ends and retractions included |
| `docs/findings/dreaming-architecture.md` | how the engine works, from reading it |
| `docs/findings/formation-suppression.md` | the deepest single investigation |
| `docs/findings/python-refactoring-opportunities.md` | P1/P2/P3 refactoring assessment, measured on `a51d3ff` — most P1s already filed (#153, #128, #149); see #227 for the mapping |
| `docs/findings/issue-coverage-2026-09-02.md` | which register items have no GitHub issue — a **dated snapshot** (corpus 119 issues, `main` at `667e748`); read its own "Read this before acting on any row" first. Its `values-prod.yaml` finding was **re-verified live on 2026-09-09** |
| `docs/operational-store-decisions.md` DW-025 | the open KEK-provider decision |
| `~/.claude/skills/verifying-memotron` | how to test any surface + the gate |
| `scripts/verify/` | the harnesses; `README.md` there has the run commands |
| `scripts/verify/parity_postgres.py` | cross-backend gate — fails today on T0-4 |
| `scripts/verify/seed_for_mcp_sweep.py` | seeds + harvests a non-MENTIONS uuid for `sweep_mcp.py` |
| `scripts/verify/probe_kek.py` | B1/T0-2 gate — three arms, exits 1 today |
| `scripts/verify/probe_supersession_backend.py` | U-14 — supersession per backend, write-attributed |
| `scripts/verify/probe_compaction_extraction.py` | does a compaction episode form memory? both transports |
| `scripts/verify/probe_storage_wiring.py` | T0-5 gate — attributes a write per construction path |
| `docs/findings/tier0-fixes.patch` | all three Tier-0 fixes, measured, **not applied** |

**Repo vs local:** takeover artefacts stay local (`.git/info/exclude`). The exception, agreed
2026-08-27: a fix whose finding was measured by more than one independent path, with a
demonstrated RED -> GREEN and an attributed write, may land in the repo with its regression
test. Single-probe or timing/environment-dependent findings may not — that is the class both
of this week's retractions came from.

**Before recording anything:** `uv run python scripts/verify/finding_gate.py`

---

## The mental model, in one page

**What it is.** A governed memory substrate for agents: a scoped, bitemporal property graph
where every write is a policy-governed, receipted, replayable decision. Explicitly *not* a
vector-RAG store — embeddings feed dedup, clustering and salience, **never truth**.

**The lifecycle** (what users actually depend on):
`episode → extraction → candidate → materialisation → retrieval → context injection`,
with offline "dreaming" doing formation, consolidation, pruning and coherence.

**Truth maintenance.** One fact occupies one *truth slot*, keyed on the **canonical**
predicate so paraphrases cannot split it. Contradiction supersedes by
**invalidate-don't-delete** — the incumbent's `valid_to` closes at the challenger's
`valid_from`; rows are never deleted. There are **five** supersession paths, not one
(see the architecture note).

**Retrieval** is a deterministic six-stage pipeline — no model calls, no clock reads, no
randomness — pinned to a `RetrievalContract` whose digest lands on receipts, so a result set
is reproducible from (graph state, contract, query, `as_of`). Relevance = normalised mix of
lexical overlap and vector cosine; it outweighs recency 5:1 by default.

**Governance.** Per-scope envelope encryption; truth keys become **keyed commitments** so
slot equality works over ciphertext. Crypto-shred destroys the DEK — content becomes
unreadable while the receipt chain still verifies, because receipts hash the *stored*
(sealed/redacted) representation, never raw text. That is the design's best idea.

**The dream agent** is consulted at six decision points under one rule: *a fallback may
approve CREATION; it may never approve REMOVAL.* Formation admission is deliberately
**fail-open**.

---

## The capacity problem — currently unowned

**Measured, not derived** (`scripts/verify/bench_scale.py`):

```
 rows      ms/write   search   ctx/episode
   33          7.9      3.0ms       1.3ms
  303         34.1     22.8ms      11.6ms
 1503        229.8    173.4ms      72.0ms
```

Reads grow with **total rows, not scope size**. Writes grow **per write** — roughly O(n) each,
so **O(n²) to build a graph**. Seeding 500 facts took 115 seconds.

**Postgres does not fix this, and is not currently safe to cut over to.** Its
`relationships()` is byte-identical to SQLite's unfiltered `SELECT *`; the 11 scans are
**caller-side**, in `dreaming.py`. A backend swap keeps the cost shape and adds a network hop.

Worse, its *scoped* read diverges — `relationships_for_scope` omits SQLite's
`AND type != 'MENTIONS'` filter, so on Postgres a scoped read returns 3x the rows with
MENTIONS **first**. That breaks `search()` and makes `epoch_content_digest`
**non-deterministic**, because MENTIONS rows have no `truth_key` and the digest falls back to
their per-materialization random uuid. Measured: SQLite 3/3 identical, Postgres 4/4 different
for identical content. **Replay verification is void on Postgres.** See **T0-4**, **D-39**,
**D-40**, and the re-runnable gate `scripts/verify/parity_postgres.py`.

**Validated one-line fix, measured RED->GREEN, not applied:** `AND type <> 'MENTIONS'` at
`postgres/_graph.py:370`. It fixes all five gate assertions at once — including restoring the
digest to `abad85699677` across both engines — which is the evidence that it repairs replay
verification rather than merely quieting `search()`.

**Why this matters for the plan:** the productionization plan sequences a durable-store
cutover as the answer to "make it real". It is the answer to durability, concurrency and
multi-replica sharing — **it is not the answer to capacity**, and nothing in the plan
currently is. A memory system whose write cost grows with everything it has ever remembered
has a ceiling, and **nobody has established where it is or what it needs to be.**

The fix direction is cheap and known — use the index-backed `relationships_for_scope()` at
the 11 call sites — but it is unowned, unscoped, and not in any workstream. See **T1-7**,
**U-1**, **Q-12**.

## Extraction: the LLM path extracts; the no-LLM path only captures

Measured 6x2, episode fact-density x transport (D-47, `probe_extraction_density.py`).
**Memories = non-`MENTIONS` relationships** — counting `MENTIONS` inflates the count with
structural edges and is what made D-46's first table wrong.

| distinct facts | rule-based | gateway |
|---|---|---|
| 0 | **1** | 0 |
| 1 / 2 / 4 / 6 / 8 | **1 / 1 / 1 / 1 / 1** | 1 / 2 / 4 / 5 / 6 |

**Rule-based spread 0; gateway spread 6.** The rule-based path is not extracting — it writes
the whole sanitized episode verbatim as one `HAS_STATE` fact (a 670-char episode became a
692-char fact, untruncated), even when the episode carries nothing durable. That is
continuity capture **working as designed** (the adoption test asserts the body is in the
fact), but it means "degrade to no-LLM formation" is not degraded extraction — it is **no
extraction**.

Gateway counts vary run to run (LLM nondeterminism); the shape held, the counts should not be
quoted as measurements. The earlier `relationships = []` was the **input** — sanitization
strips the password and email first — not a defect.

**That derivation was tested and REFUTED (D-48).** The truth key is subject + canonical
predicate (`agent:claude-code:claude-code:has state`), so two compactions hit the same slot:
the incumbent goes `superseded` (observed_count 2), the new summary becomes `active`, the old
row is retained. The no-LLM path is a **bounded rolling latest-state record**, not an
accumulating blob.

**Formation advances on wall clock** (D-50): the formation job's `cadence_seconds=1` means a
hook whose dream is due forms its own episode inline — measured 1-of-3 for back-to-back hooks,
3-of-3 with a 3s gap. An earlier note here said the graph serves superseded facts
*indefinitely* without a worker; **that was my probe's back-to-back timing, and is retracted**
(T1-15 corrected). Residual: an episode whose hook lands inside the 1s window waits for the
next hook. **D4 is still needed** for consolidation/pruning/coherence, which hooks do not drive.

## Verified working

- **Memory lifecycle — 14/14** (`scripts/verify/probe_core_loop.py`): supersession,
  current-truth retrieval, profile rendering, cross-compaction injection, token budgeting.
  **Caveat:** exercises 1 of 5 supersession paths, on a 4-fact graph.
- **SDK — 140 methods swept, zero code bugs.** Every error was a correct rejection.
- **Helm chart** renders clean; full probe coverage on both Deployments.
- **CLI** — all 13 command paths respond.
- **Test suite** — 1,071 pass with live Postgres 16 (37s hermetic without).

## The three Tier-0 fixes have repo tests, RED -> GREEN

`tests/test_scoped_read_parity.py` (T0-4), `tests/test_operational_store_wiring.py`
(T0-5 + T0-2), `tests/conftest.py` (the ephemeral-KEK opt-out that stops the guard breaking 66
tests). Measured RED on `main`, GREEN with the patch: **1081 passed** with Postgres (+11),
**1005** hermetic (+3), zero regressions. Six files, uncommitted, and unlike every other
takeover artefact they are **meant** for the repo.

## The three Tier-0 fixes are drafted and measured

`docs/findings/tier0-fixes.patch` — 65 lines, all three at once, reverted after measuring.
With the opt-out flag the full suite is **byte-identical to the `main` baseline** (1070
passed, 1 pre-existing failure). T0-4 and T0-5 regress nothing; the T0-2 guard needs
`MEMOTRON_ALLOW_EPHEMERAL_KEK` in fixtures and local compose or it breaks 66 tests.

**`main` is green.** An earlier note here said otherwise; that was wrong. A test failed only
because my worktree has a `.env`, which `runtime.py:177` loads **cwd-relative**, overriding
explicitly-cleared credentials and silently switching extraction to the billed gateway path.
See **T1-14** and the retraction in **D-45**.

## Verified broken

Top of the backlog; full detail there.

- **T1-1** formation suppressed by one unresolved-problem fact — validated one-line fix
- **T0-1** `init`/`migrate` can silently destroy another project's memory — **live on main**
- **T0-2** ephemeral KEK — **reproduced**; a single process restart loses governed-scope
  keys and tenant credentials, and status keeps reporting them configured (`probe_kek.py`)
- **8 MCP governance tools** broken (7 dead on any input, + `semantic_search`)
- **18 of 53 admin routes** dead in the deployed shape
- **T2-1/T2-2** the image cannot run the command the chart gives it
- **T0-5** the agent-facing surface **cannot use Postgres at all** — 27 MCP tools, the
  `AgentMemoryPlatform` facade, **and every Claude Code hook** (`build_platform_from_project`,
  `adoption.py:316`); `agent_memory_config()` never wires `storage_settings_from_env()`
- **T0-4** Postgres diverges on scoped reads — `search()` raises, `epoch_content_digest`
  is non-deterministic, 16 call sites affected, and the 3,310-line parity suite passes anyway

---

## Reading progress — `dreaming.py` (10,533 lines)

| region | lines | status |
|---|---|---|
| module header + imports | 1–122 | **read** — no module docstring |
| decision fallback contract | 149–228 | **read** — the six decision points, fail-open/closed split |
| `run_job` | 842–950 | skimmed |
| `_run_formation` | 1855–2472 | **partial** — matching, claiming, the decision gate |
| `scan_coherence` | 1696–1844 | unread |
| `_run_rollup_consolidation` | 3347–3470 | unread |
| `_rollup_pass_for_scope` | 3718–4271 | **unread — next priority** |
| `resolve_canonical_predicate` | 6004–6135 | unread |
| `resolve_canonical_entity` | 6883–7232 | unread |
| `_materialize_episode` | 7382–8513 | **read ~50%** — entry, truth slot, supersession, dedup/reinforce |
| `_apply_reinforce` | 9222+ | **read** — confirms reviewer finding #2's mechanism |
| `_run_pruning` | 9409–9583 | unread |
| `_apply_soft_cap` | 9831–10036 | unread |

**Roughly 1,500 of 10,533 lines read with comprehension (~15%).**

Other unread corpora: `client.py` (8,826), `storage/sqlite.py` (5,709), `config.py` (4,361),
`DATAFLOW.md` (1,196), both patent specs (1,175), `docs/design/` (4,322), `README.md` (2,854).

---

## Open threads

*Refreshed 2026-08-27 (session 9). **U-3, U-9, U-12, U-13, U-14 are CLOSED** and were removed
from this list — see `UNKNOWNS.md` for their evidence. The authoritative work queue is the
"Next session — start here" section at the top of `STATE.md`; this list is the research backlog
behind it.*

1. ~~**T1-14 is now blocking, not optional**~~ — **CLOSED 2026-08-27.** The four bare
   `load_env_file()` calls are deleted and the argument is required; `.env` loading belongs to
   entry points, anchored to a project root. `scripts/verify/probe_env_file_cwd.py` exit 1 -> 0
   (5/5), `ci-build-check.sh` now passes **with** a `.env` present, suite unchanged at 1006/77.
   Original two-arm proof in `DOGFOOD-LOG.md` D-81. **A verifier pass caught one regression the
   fix introduced** — `memotron-local-platform` lost its documented `.env` pickup, restored via
   an explicit `--env-file` at the launcher. Two pre-existing credential findings were raised in
   the same review and are *not* closed: `.env` can set `LITELLM_API_BASE` and redirect a real key
   to an arbitrary host, and a wrong endpoint sealed that way is permanent (the seed path early-
   returns when a row exists). See the T1-14 row in `TAKEOVER-BACKLOG.md`.
2. **T1-27 has no fix and blocks the Postgres cutover** — a successful re-migration duplicates
   the destination (1 -> 2 -> 3 observed, across engines). Red test:
   `scripts/verify/probe_migration_idempotency.py`.
3. **Read `_rollup_pass_for_scope`** (`dreaming.py:3718-4271`) — consolidation is a whole job
   kind never observed running. Still unread.
4. **U-11** — four of five supersession paths unverified; the gate-parked dispute state has
   never been produced by any probe.
5. **U-1** — relevance at scale; `max_candidates: 64` / `beam_width: 16` make the question
   sharp: what happens to a relevant fact outside the 64-candidate window?
6. **U-6** — findings #3 and #4 from the PR review still unreproduced.
7. **The parity suite tests the wrong thing** — 117 tests assert both backends store the same
   bytes, never that they answer a caller the same way. T0-4 walked straight through it. What
   else does that blind spot hide?
8. **T0-10's fix is SQLite-only** — Postgres has zero epoch references, so the same leak is
   unfixed there.
9. **Does the MCP write path produce better memory than the hook?** First chance to find out is
   the next session — the server connects for the first time. T1-33 is the hook's failure mode;
   whether deliberate writes avoid it is **unproven in either direction**.
10. **Q-1/Q-2/Q-3** — should formation admission consult a model at all? Needs a human.

## Harnesses added in session 9

| probe | proves |
|---|---|
| `probe_artifact_staleness.py` | T0-13 — an artifact with no `updated_at` retires correct memory (3-arm control) |
| `probe_phantom_hold.py` | T1-18 — a reported hold the graph never received; window `[0.500, 0.718)` |
| `probe_checkpoint_coverage.py` | T1-20 — tampering with a checkpoint's graph-state hash passes verification |
| `probe_credential_overwrite.py` | T1-26 — migration silently swaps the destination's LLM key |
| `probe_migration_idempotency.py` | T1-25 + T1-27 — refuses in a common state; retries duplicate |
| `probe_router_isolation.py` | T1-30 — **refutes** the filed claim; 4/4 attacks rejected |
| `scripts/ci-build-check.sh` | the Workstream C gate (two lanes; Postgres reported SKIPPED, not PASSED) |

Every one **exits 2 rather than 0 when its control arm fails**, so "no defect" can never be
reported by a run that never reached its subject. Four near-misses in session 8-9 are why.

## Standing corrections to my own habits

Recorded because each cost real time this session:

- A green tool response is not a working system — **assert a round trip**.
- **Hold one variable.** A comparison that changes two proves nothing, even when right.
- **Read the label** before believing a UI negative — the same words are accurate on one tab
  and false on another.
- **Verify the referent** before calling a string a defect. Three over-claims came from this.
- Test the layer users touch, not the layer convenient to script.
