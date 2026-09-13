# Memotron takeover — discovery record

**This branch is not a change to merge. It is a record of what we learned taking the project
over, published so the work is visible and reusable.** When the real productionization work
branches off `main`, pull pieces across from here rather than re-deriving them.

Baseline: `origin/main` = `f28e95c` (PR #44 merged).

---

## Start here

| file | what it holds |
|---|---|
| **`WALKTHROUGH.md`** | **the record in the order worth presenting it** — verdict, the designed/deployed gap, what was fixed, what was retracted, decisions needed |
| **`docs/findings/README.md`** | **the working-memory file — read this first.** Mental model, index of where each kind of knowledge lives, `dreaming.py` reading tracker, open threads |
| `TAKEOVER-BACKLOG.md` | the defect register — 53 items, tiered, each with file:line or a command |
| `UNKNOWNS.md` | 15 unknowns (testable, each with a `CLOSES WITH`) + 13 open design questions that need a human |
| `DOGFOOD-LOG.md` | the raw trail — 52 entries, **including dead ends and retractions** |
| **`INFRA-BACKLOG.md`** | **the infrastructure handoff — self-contained, for the infra team** |
| `STATE.md` | verified facts, general rules, open failures, last-session pointer |
| `scripts/verify/README.md` | the harnesses: what each one gates and how to run it |
| `docs/findings/verifying-memotron-SKILL.md` | the testing methodology, and the mistakes that cost the most time |

---

## What is worth taking

**Three Tier-0 fixes with regression tests.** These are the only source changes here, and the
only findings we considered well-verified enough to encode as assertions:

| defect | fix | test |
|---|---|---|
| **T0-4** Postgres scoped reads leak structural `MENTIONS` edges, breaking `search()` and making `epoch_content_digest` non-deterministic | 1 line, `postgres/_graph.py` | `tests/test_scoped_read_parity.py` |
| **T0-5** the whole agent-facing surface — 27 MCP tools, `AgentMemoryPlatform`, **and every Claude Code hook** — silently ignores the operational-store DSN | 2 lines, `agent_memory.py` | `tests/test_operational_store_wiring.py` |
| **T0-2** Postgres seals with an ephemeral per-process KEK; one restart loses governed-scope keys while status still reports them configured | fail-closed guard, `postgres/__init__.py` | `tests/test_operational_store_wiring.py` |

Measured RED on `main`, then GREEN: **1081 passed** with live Postgres (baseline 1070, +11),
**1005** hermetic (baseline 1002, +3), **zero regressions**. Every `sqlite` arm passes in the
red run, which is the control proving the tests measure the engine rather than being broken.

`tests/conftest.py` opts the suite into the ephemeral KEK via one autouse fixture. Without it
the fail-closed guard breaks **66 tests**, because the suite seals with that key by design —
so the plan's "two lines that permanently retire the failure mode" is right on line count and
wrong on cost.

**Seventeen harnesses** in `scripts/verify/`, several of which are gates that exit non-zero on
a live defect: `parity_postgres.py`, `probe_kek.py`, `probe_storage_wiring.py`,
`finding_gate.py`. Plus `docker-compose.local.yml`, a quasi-GKE local stack.

**`finding_gate.py` is a linter for the record itself**, not the code. It fails on claims with
no way to re-verify them, hedges without an `ASSUMED` label, comparisons that changed two
variables, and design questions answered by inference during discovery.

---

## What we got wrong

Kept deliberately, because the retractions are load-bearing: they say which claims to trust.

- **"`main` has a deterministically failing test"** (D-44 → **D-45**). Real evidence — 3/3
  runs, hermetic and with Postgres, zero tracked changes. All of it blind: every check varied
  something *inside* the repo and none varied the working directory. The same tree passes in a
  fresh worktree. Cause was a cwd-relative `.env` (now **T1-14**).
- **"Without the dream worker, memory serves superseded facts indefinitely"** (T1-15 →
  **D-50**). Came from running two hooks back-to-back. Formation advances on wall clock
  (`cadence_seconds=1`), so with a realistic gap the correct `superseded`/`active` pair is
  already present.
- **"A verbatim blob can never be superseded"** (D-47 → **D-48**). Derived, labelled as such,
  tested, refuted — the truth key is subject + canonical predicate.

Both retractions share one cause: **the harness created conditions that do not occur in use,
and the result was attributed to the product.** The standing rule is in `STATE.md`.

---

## What is still open, and needs a human

- **C0 — confirm the `JedAI_Memotron` Harness trigger list is empty before anything
  merges.** No trigger file exists in-repo and live triggers can be inline, so absence from
  git proves nothing.
- ~~**`kubectl get pods -n jedai-memotron`** (RESTARTS)~~ — **CLOSED 2026-08-27.** Ran it:
  nothing of Memotron is deployed (a June health stub at Helm revision 1, **T0-9**) and that
  Deployment has been `READY 1/2` for 37+ days on a ReadWriteOnce PVC (**T0-8**).
- **13 open design questions** in `UNKNOWNS.md`, deliberately left as questions.
- **Test CI now exists in the source of record, but is not yet enforcing** (B6).
  `scripts/ci-build-check.sh` and a `Build_Check` stage were added on 2026-08-27, and the
  pipeline is `storeType: REMOTE` so the in-repo YAML *is* the record — but the trigger list is
  empty and GitGuardian remains the only PR check, and it is not required. **Onboarding the
  stage needs a Harness PAT with `core_service_edit`, which we do not have.** So the gate is
  written and not yet binding; that remains the constraint on everything else.

---

## Honest limits of this record

`dreaming.py` is 10,533 lines and roughly 15% has been read with comprehension; the tracker in
`docs/findings/README.md` says which regions. Measurements marked **DERIVED** were reasoned,
not observed, and are labelled. Gateway-dependent numbers vary run to run — quote the shape,
not the counts. Nothing here has run on GKE; every measurement is local, against
`postgres:16` in Docker or SQLite.
