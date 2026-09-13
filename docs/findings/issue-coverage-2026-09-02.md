# Issue coverage for the takeover defect register — 2026-09-02

**Question:** which items in `TAKEOVER-BACKLOG.md` and `INFRA-BACKLOG.md` have no GitHub issue?

**Report only.** Nothing was filed, closed, or re-scoped on the basis of this document.

Corpus: all 119 `jedai/memotron` issues, open and closed, title + body. Registers read on branch
`takeover` (`c408753`). Code checks run against `origin/main` = `667e748`.

---

## Read this before acting on any row

**1. A naive ID-string scan is wrong and must not be used.** Grepping the 119 issues for `T2-4`,
`C-3` etc. matches only 24 of 98 register items and reports 66 gaps. Most are false: T2-4 is covered
by #114/#115, T3-4 by #105/#81, T4-13 by #82, C-2 by #88–92, C-3 by #77 — none of which name the ID.
Every verdict below is semantic, matched on symptom, file path, and subject.

**2. "Unfiled" and "still broken" are different claims, and only Tier 0 has both checked
systematically.** The registers snapshot an older commit; `main` has since absorbed PRs #131, #147,
#152. Several items the register still presents as live are fixed in source on `main`. The Tier 0
pass verified every item against `main`. The Tier 1 and Tier 2–4 passes verified coverage
thoroughly but checked `main` only opportunistically.

> **Therefore: before filing any row below, check `origin/main` first.** Treat the UNFILED column as
> "no issue exists," not as "the defect is live." Three Tier-0 items were unfiled *and already
> fixed*; the same pattern is likely in Tiers 1–4 and is not yet measured.

**3. A `takeover`-tree observation is not a `main` observation.** The Tier 1 pass reported the MCP
serializers "still broken on `main`." **That is wrong** — verified here:
`origin/main:src/memotron/mcp_server.py:920` reads `result.corrected_relationship_uuid`, the
fixed form, and none of `result.chunk_count`, `.run_at`, or `held_count` appear anywhere under
`src/` on `main`. The agent was reading the `takeover` working tree, which is behind `main`. Any
similar "still live" claim inherits this risk.

**4. This checkout's register is itself stale.** It has no `T3-8`…`T3-12` and no `T4-14`/`T4-15`,
yet issues #108, #109, #110, #111, #118, #137 were carved from those IDs. The issues came from a
*later* revision of the register than the file in this tree. Counts below are against this tree.

**5. #45 is excluded as coverage.** It carries the register text and so "mentions" nearly
everything. Excluded on #144's own reasoning — a reference artifact, not a change to ship. If you
count it, every UNFILED row becomes covered by an issue nobody will action.

---

## Tier 0 — 14 items, 5 unfiled, **2 live**

This is the only tier where every item was checked against `main`, so it is the only tier whose
"live" column can be acted on directly.

| item | filed? | live on `main`? | action |
|---|---|---|---|
| **T0-1** — `migrate` strands memory in split-brain visibility and purges the source anyway | no | **yes** | **file** |
| **T0-13** — an artifact with no `updated_at` is treated as staler than every memory; the correct memory is retired for it | no | **yes** | **file** |
| T0-10 — entity-hop expansion bypasses epoch isolation | no | fixed | nothing to file |
| T0-12 — `verify_no_silent_mutation` passes on zero mutating receipts | no | fixed | nothing to file |
| T0-14 — staging a baseline evaluates the *candidate's* invariants | no | fixed | #143 is the live residue |

Covered: T0-2 → #123/#130 · T0-3, T0-4 → #120 · T0-5, T0-6 → #121 · T0-7 → #122 · T0-8 → #148
(+#79, #146) · T0-9 → #16/#21 (partial, neither names it) · T0-11 → #124.

**T0-1 — verified twice, independently.** Unfiled: seven term searches across all 119 bodies
(`migrate_tenant`, `migration.py`, `purge_source`, `split-brain`, `tenant migration`, …) return
**zero** hits; the only `migrat` matches are chart/PVC/Neo4j prose. Live: `migration.py` exists on
`main` with **0 occurrences of `epoch_id`** — it copies properties wholesale, never rewriting epoch —
and `purge_source: bool = True` at `src/memotron/migration.py:743`. Copy-without-epoch-rewrite
plus destroy-by-default is exactly the mechanism the register describes.

**T0-13 — live**, `src/memotron/coherence.py:333` still reads
`stale = other.updated_at is None or (...)`; the `is None` disjunct is the defect.

The three "fixed" verdicts also verified: T0-10 carries fix comments in both storage engines plus
`tests/test_epoch_read_parity.py`; T0-12 is `replay.py:1054`; T0-14 is `_certification.py:244-285`.

---

## Tier 1 / 1b — 33 T1 rows + 9 T1b rows

Not 37 T1 as assumed — `T1-8`…`T1-11` are unallocated and `T1-5` is struck through (RETRACTED,
correctly absent from the corpus).

### The T1b premise was wrong, in a way that removes seven false gaps

**T1b is not nine field-name mismatches — it is seven.** `T1b-1`…`T1b-7` are the mismatch table.
`T1b-8` (`semantic_search` returns 0 over MCP, 3 from the SDK) and `T1b-9` (18 dead `/api/platform`
routes) are unrelated defects filed under the same tier heading.

**#132 subsumes T1b-1 … T1b-7 exactly** — established by reading fix commit `3cf0139`, whose diff
corrects every attribute pair in the register's table one-for-one, and which also fixes two further
serializers (`run_due_dreams`, `run_dream_job`) that the register never recorded. So #132 is a strict
superset. **Do not file T1b-1 … T1b-7 — seven apparent gaps that are not gaps.**

**T1b-8 and T1b-9 are genuinely unfiled** — different mechanisms, absent from `3cf0139`'s diff.
T1b-9 is *referenced* by #140 as a reason to defer baselining the admin surface, and filed nowhere.

### 25 T1 items with no issue

Grouped by mechanism. **Every one still needs a `main` check before filing** (see caveat 2).

- **Migration cluster (compounding, and the register calls T1-27 a Postgres-cutover blocker):**
  T1-25 (migration refuses in the normal post-ingestion state), **T1-27** (re-run duplicates the
  destination 1→2→3), T1-34 (no CLI path moves memory into Postgres — `open_storage` hardcodes
  SQLite), T1-26 (migrating a tenant overwrites the destination's LLM credentials).
  Same family as **T0-1** and **T2-11**; worth filing as one epic rather than five tickets.
- **Credential / path resolution:** T1-35, T1-36 (a `.env` redirects the operator's real key to an
  attacker host over plaintext HTTP), T1-37 (T1-14's fix is not retroactive; already-sealed rows are
  unowned).
- **Audit / certification fail-open:** T1-20, T1-21, T1-22, T1-24, T1-29, T1-31.
- **Governance / coherence:** T1-18, T1-19, T1-28 (a per-tenant purge runs `DELETE` with no `WHERE`
  on `dream_job_runs` and `job_state` — needs a decision, not only a fix), T1-32.
- **Deployment / wiring:** T1-3 (chart `:memory:` under an HPA of 2–6), T1-4 (admin console can
  never target Postgres), T1-16 (plain SDK path forms zero memories with a valid key).
- **Product behaviour:** T1-15 *(file the NARROWED version only — the retracted "serves superseded
  facts indefinitely" framing must not be re-filed)*, T1-33.

Covered: T1-1, T1-6 → #124 · T1-2 → #125 (+#17, #142) · T1-7 → #107 · T1-12, T1-17 → #132 ·
T1-13 → #88–92 · T1-14 fixed, no issue needed · T1-30 **REFUTED and correctly absent**.

**Provenance, per the register's own rule:** T1-18…T1-32 are tagged *"CONFIRMED — hand-checked, was
AGENT-SOURCED"*. Quote mechanism and `file:line` freely; treat severity and blast radius as
provisional. T1-35/36/37 are agent-run reproductions with commands recorded — **not** hand-checked,
and must be tagged that way if filed. T1-28 is the worked example of a filing whose blast radius was
materially overstated.

---

## Tier 2 / 3 / 4 — 35 items, 20 unfiled

**Unfiled:** T2-1, T2-2, T2-5, T2-11, T2-12, T2-14, T2-15 · T3-5 (`POST /api/tenant-config/llm`
accepts a provider key as plaintext JSON — #126 authenticates the surface but never the payload),
T3-7 · T4-1, T4-2, T4-3, T4-4, T4-5, T4-6, T4-7, T4-9, T4-10, T4-11, T4-12.

**Correctly absent:** T2-3 and T2-13 are REFUTED, and no issue exists for either.

**Covered:** T2-4 → #114/#115 · T2-6 → #75/#47–50 · T2-7 → #51–55/#102 · T2-8 → #77 · T2-10 → #145 ·
T3-1, T3-2, T3-3 → #126 · T3-4 → #105/#81 · T3-6 → #15/#11 · T4-8 → #129 · T4-13 → #82.

Two Tier-4 rows verified live on `main` here: **T4-9** — `ui/admin/package.json` still carries
**15** `"latest"` pins, which is what makes any future lockfile refresh unsafe; #82 was explicitly a
lockfile-only change. **T4-13/I-6 are met** — the lockfile resolves `nanoid-3.3.18.tgz`, so
`INFRA-BACKLOG.md`'s "still open on `main`" line is stale.

---

## Infra — C-1…C-8, I-1…I-6

**Unfiled: C-5 only** — the infra half of test CI: Harness registration, `@jedai/automation` write
access, a required status check, PyPI egress, and a Linux runner for npm lockfile work. #114/#115
are the app half, and #114 explicitly declines to wire `Build_Check`.

**Closed but not actually met** — the case `INFRA-BACKLOG.md` warns about, *"a closed ticket is
evidence that someone finished their task, never that your requirement is met"*:

- **I-5 / #87.** The register's warning is correct and independently confirmed by #87's own body:
  *"distinct from #77 (app-level envelope-encryption KMS) — this is instance-at-rest encryption."*
  The CMEK key's only IAM member is the Cloud SQL service agent, so the app cannot call it. I-5's
  real coverage is **#77 (open)**, gated behind **#123 (open)**.
- **C-4 / T2-9 via #56–60, #24.** AppRole and KV tickets all closed, and the chart now carries a real
  role_id — but `vaultSecret.enabled: false` in `values-latest/stage/preview/load.yaml` on `main`
  (prod alone is `true`). No secret is delivered in four of the five environments.

---

## Two findings outside the coverage question

**1. I-4's stated answer is falsified.** I-4 concludes the Harness trigger list is EMPTY and
"merging is unblocked." **#146 records the opposite**: merging #103 fired
`Memotron_CICD_Continuous` seq 27 via `WEBHOOK / Main_Merge` within 13 seconds, deployed, and
failed. I-4's own contingency — *"a fail-closed guard that deliberately refuses to start would turn a
silent bug into crashing pods"* — is a live risk, not hypothetical.

**2. `values-prod.yaml` on `main` enables the operational store while no durable key manager
exists.** Verified: `vaultSecret.enabled: true` (`:56`) and `operationalStore.enabled: true` (`:61`).
That is precisely the interaction **#130** was raised to prevent, and it contradicts
`INFRA-BACKLOG.md`'s claim that `operationalStore.enabled: false` in all five overlays.
**Check before any prod deploy.**

---

## Suggested order, if these get filed

1. **T0-1** — unfiled, live, data-destructive, and the head of the migration cluster.
2. **T0-13** — unfiled, live, hand-verified, retires correct memory.
3. **The migration epic** — T1-25, T1-26, T1-27, T1-34, T2-11 under one parent with T0-1.
4. **T3-5** — plaintext provider key on an unauthenticated route; small and self-contained.
5. **C-5** — unblocks the CI half that #114/#115 deliberately left open.

Everything else is real but lower-order, and roughly a third of it may already be fixed on `main`.

## What would make this document wrong

- The register is a snapshot of an older commit. Any UNFILED row that is also already fixed on `main`
  should be recorded as discharged, not filed. Only Tier 0 has been checked that way.
- Coverage was judged from issue **text**. An issue whose title and body do not describe the
  mechanism will read as non-coverage even if someone intended it to cover the item.
- The register in this checkout predates the revision that #108–#137 were carved from, so tier
  membership and item counts here may not match the register someone else is holding.
