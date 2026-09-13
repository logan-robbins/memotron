# Memotron takeover — the walkthrough

**Living document.** Drafted 2026-08-27 for the 2026-08-28 review; updated through the day.
Changes are logged in [§11](#11-change-log) so anyone who read an earlier version can see what moved.

Baseline: `origin/main` = `f28e95c`. Everything here is verified locally against `postgres:16`
or SQLite. **Nothing has been measured on GKE.**

---

## 1. The 60-second version

> Memotron is a genuinely good piece of engineering that was deployed as a demo and never
> converted into a service. The audit design — hash-chained receipts, byte-for-byte replay that
> survives a crypto-shred — is stronger than most production memory systems have. **The
> engineering isn't the problem; the gap between what's written and what's deployed is.**
>
> Three facts define the work: the deployed API stores memory in `:memory:` and loses it on
> every restart; nothing in the cloud ever runs the job that forms memory; and no CI anywhere
> runs the tests, so every quality claim about this codebase is currently unfalsifiable by
> anyone but its author.
>
> We fixed the three worst defects with regression tests, split the infrastructure work out for
> the platform team, and found one thing nobody expected: **a capacity problem that a better
> database does not solve.**

If the room only remembers one sentence, make it the last one.

---

## 2. What it is, in plain terms

An agent accumulates facts across sessions. Those facts contradict each other, go stale, leak
PII, and get written by untrusted sources — and there's no way to prove why a memory exists or
what it caused. **Memotron's thesis: agent memory should behave like a database with a
policy layer and an audit log, not an append-only notes file.**

Concretely: a scoped, bitemporal property graph where every write is a policy-governed,
receipted, replayable decision. Deliberately *not* vector-RAG — embeddings feed dedup and
salience, never truth.

**The three terms you need in the room:**

- **Scope** — the tenancy boundary (`agent` / `tenant` / `user` / `customer`). Every read and
  write is scoped.
- **Truth slot** — one fact occupies one slot, keyed on the *canonical* predicate so
  paraphrases can't split it. A contradiction **closes** the incumbent rather than deleting it.
- **Receipt** — one hash-chained decision record per write, computed over the *stored*
  (redacted or sealed) form, never raw text.

---

## 3. Lead with what's good — because it's true

Say this first. It's accurate, and it's why the system is worth productionizing rather than
replacing.

| | |
|---|---|
| **Receipts survive erasure — now VERIFIED, not read** | Destroyed the key on a sealed scope and measured it: content unreadable (0 hits, 0 leaking plaintext), **every hash chain still verifies**, **no receipt lost**, **every Merkle root unchanged**, erasure certificate verifies. An immutable audit ledger coexisting with right-to-erasure is the strongest idea in the codebase, and it holds. `scripts/verify/probe_shred_audit.py`. |
| **Truth maintenance actually works** | Verified on both backends: two contradicting facts land in one slot, the incumbent goes `superseded`, the old row is retained. Keyed on subject + canonical predicate, so paraphrases don't fragment a fact. |
| **Encrypted scopes still work** | Truth keys become *keyed commitments*, so slot equality and prefix seeks work **over ciphertext** — an encrypted scope doesn't degrade to a scan. |
| **Retrieval is deterministic** | Six stages, no model calls, no clock reads, no randomness, pinned to a contract whose digest lands on the receipt. A result set is reproducible from (graph state, contract, query, `as_of`). |
| **Tamper detection — see §3b, this is the reversal** | `byte_replay` reconstructs a run from its receipts and requires the result to equal the live graph. **Say it as "current-state attestation", not "replay any past run"** — see §9. **And say the limit:** on Postgres the state hash is memoised, so raw-SQL row changes go undetected (**T0-7**, 4/4 caught on SQLite vs 1/4 on Postgres); gate denials are recorded outside the chain entirely (**T4-4**). |
| **Adversarial input was considered** | An anti-hallucination clamp stops a spoofed future `valid_from` out-ranking a real observation. Contradictions **discount the alias that bridged them** — the system treats its own entity resolution as falsifiable. |

**68,742 lines of Python. 1,081 tests passing. Zero run in CI.** That last number is the whole
problem in miniature.

> **If someone quotes different figures:** the earlier assessment artifact says *35,643 lines,
> 353 tests*. Both were correct — they were measured **before PR #44 merged**, which roughly
> doubled the codebase. Same repo, different baseline. Worth saying before it reads as an
> inconsistency.

---

## 3b. The reversal: the audit layer is the weakest part, not the strongest

**This changed on 2026-08-27 and it changes the story.** Earlier drafts of this document led with
the audit design as the system's best idea. **Crypto-shred still is** — verified end to end:
content destroyed, every chain verifies, no receipt lost, every Merkle root unchanged, erasure
certificate verifies.

**The verification layer built on top of it is a different matter.** Nine findings, all observed:

### The pattern: three gates return a verdict with no evidence behind it

| | |
|---|---|
| **T0-14** — ~~staging a **baseline** evaluates the **candidate's** invariants~~ **FIXED 2026-08-31 on `production-hardening`** | It was **three** fail-open defects on the same two lines, not one: the wrong side's *invariants*, the wrong side's *stability* (`stability_score` is joint — `1.0 - max(baseline, candidate)`), and only repetition 0 gated on **both** arms. Each side is now judged on its own evidence across every sample. `tests/test_baseline_staging_invariants.py`, 10 tests, each part break-tested separately with a paired control. Fixing it exposed a separate, still-open defect it had been hiding — see below. |
| **T0-12** — ~~`passed=True` for a scope with live rows and zero receipts~~ **FIXED** | `replay.py:1054` now branches on `latest_hash is None` and compares the live hash against the empty-scope digest, so zero mutating receipts is only consistent with an empty scope. Pinned by `tests/test_no_silent_mutation_fail_closed.py`; break-tested 2026-09-01 -- disabling the branch fails both tests. The register row was stale for a while after the fix, which is its own lesson. |
| **T1-29** — ~~no accuracy gate at all~~ **FIXED 2026-09-01** | `mean_score` was computed and reported but **never compared to anything**, and stability was the only numeric gate. Measured: a suite scoring **0.0 every repetition** has stddev 0 and therefore a **perfect 1.0 stability score** and passed, while a genuinely capable but variable run (mean 0.70) scored 0.568 and failed — *consistent total failure was the best-scoring input to the only gate there was*. Now: `mean_score <= 0` fails unconditionally, plus a tunable `minimum_mean_score` defaulting to 0.0 because inventing a quality number without a measured per-suite baseline would repeat the very anti-pattern this table is about. |

**Say it as one sentence:** *three separate gates answer "did this pass?" with "yes" in states
where nothing was checked.* **All three are now fixed** -- T0-14 and T0-12 on 2026-08-31,
T1-29 on 2026-09-01. The pattern was real and it is closed; what it cost to close is the
part worth carrying forward, because two of the three turned out to be more than one defect.

**And the reason it is worth fixing rather than noting** — the moment T0-14 started reading the
baseline's own evidence, it reported a real defect that had been sitting underneath it. Four of
the 15 builtin motives form a `ROLLUP` row during replay even though their `allowed_memory_types`
excludes it, so they violate their own `retrieval_allocation` invariant. The WS-3 motive→rollup
gate at `dreaming/_consolidation.py:191` never fires on the replay path (**observed**: zero gate
log lines, while the extraction-level type filter logs normally). It is pinned as a strict xfail,
`test_motive_rollup_gate_is_not_enforced_during_replay`, filed as **#143**, and **not fixed** — where the check
belongs is a design call, and one of the three plausible answers would weaken a protected
invariant. A gate reading the wrong side does not just fail to catch things; it hides what the
right side was saying the whole time.

### And the guarantees that are narrower than they look

| | |
|---|---|
| **T0-10** | Epoch isolation leaks through retrieval's expansion stage, so **`rollback_epoch` does not hide what it rolled back** — `search()` still returns it. |
| **T0-7** | On **Postgres**, raw-SQL row tampering is detected **1 time in 4**; SQLite catches 4 of 4. The memoised state hash is what makes it blind. |
| **T3-7** | `byte_replay` is a **current-state attestation**, not historical replay: only the newest run in a scope can pass. |
| **T4-4** | Gate denials are recorded **outside** the hash chain, in a table with no hash columns that `verify_chain` never reads. |
| **T1-22** | `certify_motive`'s `policy_regression_gate` performs no state-hash or replay verification despite documenting it. |
| **T1-31** | The stability gate variances only per-repetition **means**, so a judge that flips every individual answer scores `stability=1.0`. |

**How to say it:** *the erasure guarantee is real and well built; the verification guarantees
around it are weaker than the design implies, and several fail open rather than closed.* For a
system whose selling point is receipt-backed certification, that is the finding that most changes
what "productionization ready" means.

**Do not soften this into "some audit bugs".** And do not let it be heard as "the system is
badly built" either — the same codebase contains keyed commitments over ciphertext, an
anti-hallucination clamp, and truth slots that work. **The engine is good; the things that
*attest* to the engine are not.**

### The register is not one thing, and we should say so

The register jumped 62 -> 85 in an afternoon when a 17-agent pass read the ~30k lines nobody
had read. **Those items are not the same quality as the rest**, and they are now marked
`AGENT-SOURCED, not hand-checked` in the register itself.

| provenance | count | what backs it |
|---|---|---|
| hand-verified | 24 | I read the code and ran a repro myself |
| agent-sourced, **now all hand-checked** | 0 | every one was followed up — results below |
| the original register | 62 | source-provable, clean-room reproduced, or explicitly UNVERIFIED |

**All 20 have now been hand-checked. Final tally: 12 confirmed as filed, 5 confirmed but
reframed or resized, 2 refuted, 1 unverifiable from this repo.**

The **reframings** are the ones to know about, because each changes what the fix is:

- **T1-24** is not an inert gate, it is an **inverted** one — the same field is read in opposite
  directions by two modules. Fix: make the readers agree.
- **T1-27** is worse than filed — re-running a *successful* migration duplicates the destination
  (observed 1 -> 2 -> 3 identical rows). And **T1-25** is what provokes the retry.
- **T1-25** is narrower and gentler than filed: fail-closed, refuses to purge the source. Costs
  availability, not data.
- **T1-29** was understated — a benchmark scoring zero every time earns a *perfect* stability
  score, because that gate measures consistency and nothing measures accuracy.

**The honest summary of the agent pass:** every `file:line` it cited was real, including in the
two items that turned out to be wrong. What it could not reliably do was follow a value one hop
further to judge severity. So: trust its locations, re-derive its consequences.

## 4. The gap: designed versus deployed

**Verified against the live cluster on 2026-08-27, not inferred from the repo.** This section
changed materially that day — the earlier version described the *chart*, which turns out never
to have shipped.

### What is actually running in `latest`

```
deployment  jedai-memotron-api   READY 1/2   revision 1   age 76d
pod  ...9tlqf   1/1 Running             0 restarts  37d
pod  ...9iqq    0/1 ContainerCreating   0 restarts  37d
image     wdpr-memotron:0.1.0
command   ["python", "examples/health_server.py"]
env       OPENAI_API_URL only
```

**Nothing of Memotron is deployed.** `examples/health_server.py` is 672 bytes — *"Minimal HTTP
health server for Kubernetes liveness/readiness probes"*. It answers `/health` and nothing else.
No `MEMOTRON_GRAPH_PATH`, no MCP server, no admin Deployment, no Ingress.

`deployment.kubernetes.io/revision: "1"` — **never updated in 76 days**. It *is* Helm/Harness
managed, so one release happened, in June, of a placeholder. **`.helm/values-latest.yaml` has
never shipped.**

### And the half that is deployed is wedged

`jedai-memotron-data` is **ReadWriteOnce**; every pod mounts it at `/data`; `replicas: 2`.
RWO attaches to one node, the pods are on different nodes, so the second **can never start**.
The HPA is min 2 / max 6 — **it cannot meet its own minimum.** Degraded **1/2 for 37 days**,
unnoticed, which is exactly what "no metrics, no alerting" buys you.

### What the chart *would* do if it shipped — still the cutover blockers

| designed | what the chart would actually do |
|---|---|
| durable graph across replicas | `MEMOTRON_GRAPH_PATH=":memory:"` with an HPA of 2–6, each replica holding its own |
| continuous memory formation | nothing scheduled — dreaming is reachable on demand only, never on a timer |
| per-principal authorization | no request-level auth; admin runs `--principal-role admin` |
| sealed content per scope | Postgres `enabled: false`; Vault a `REPLACE_ME` placeholder |
| continuous proof of correctness | no CI runs the tests |

**How to frame it:** this is not a broken deployment. It is an **empty** one — a placeholder that
answers health checks, half of it stuck, with the real chart sitting unshipped in the repo. That
is a cleaner problem than a misbehaving service, and a much bigger gap than "it loses memory on
restart."

## 5. What we fixed, and why you can trust it

Three Tier-0 defects, each with a regression test that was **RED before the fix and GREEN
after**. Same discipline we asked of the last PR: the test lands first.

| | Defect | Why it matters |
|---|---|---|
| **T0-4** | Postgres scoped reads leak structural `MENTIONS` edges (SQLite filters them; the Postgres docstring *claims* it matches) | `search()` raises `KeyError`, and `epoch_content_digest` becomes **non-deterministic** — SQLite 3/3 identical, Postgres 4/4 different for identical content. That digest exists so two independently recomputed epochs can be compared. **Replay verification is void on Postgres.** |
| **T0-5** | The agent-facing surface silently ignores the operational-store DSN | 27 MCP tools, the SDK facade, **and every Claude Code hook**. Enabling Postgres would leave the compaction/context path on a per-pod SQLite file while the governance server correctly used Postgres. Two halves of one deployment disagreeing about where memory lives. |
| **T0-1** | `migrate` strands memory in a split-brain visibility state and purges the source anyway | Reproduced: `relationships_migrated=3`, exit 0, source purged to **0**, destination **raw=4 / visible=1**. The rows are not deleted — `search()` returns them while `profile()` shows **0 of 3**. The path that loses them is the one that builds an agent's injected context. Only triggers when the destination already owns an epoch, i.e. adopting into a tenant already in use. |
| **T0-2** | Postgres seals with a random per-process key | A **single restart** makes sealed content unreadable — verified with the same `graph_path` and the same `.kek` file, so it isn't a file-sharing artefact. And it's **silent**: status keeps reporting the credential as configured. |

**The measurement:** 1,081 passed with live Postgres (baseline 1,070, **+11**), 1,005 hermetic
(baseline 1,002, **+3**), **zero regressions**. Every `sqlite` arm passes in the red run — the
control proving the tests measure the engine rather than being broken.

**One cost worth surfacing before someone finds it:** the fail-closed KEK guard breaks **66
tests** without an opt-out, because the suite seals with the ephemeral key by design. The plan
called this *"two lines that permanently retire the failure mode"* — right on line count,
wrong on cost. It's handled with one autouse fixture in `tests/conftest.py`.

---

## 6. The finding nobody expects

**Write cost grows with everything ever remembered.** Measured:

```
rows      ms/write    search    ctx/episode
  33          7.9      3.0ms       1.3ms
 303         34.1     22.8ms      11.6ms
1503        229.8    173.4ms      72.0ms
```

Reads scale with **total rows, not scope size**. Writes are ~O(n) each, so **O(n²) to build a
graph** — seeding 500 facts took 115 seconds.

**Cause:** `relationships()` is an unfiltered `SELECT *`, and `dreaming.py` calls it **11
times**, including once per episode during formation and inside a `while` loop. An index-backed
scoped read exists and those call sites don't use it.

**Postgres does not fix this.** Its `relationships()` is byte-identical — the scans are
**caller-side**. A backend swap keeps the cost shape and adds a network hop.

**The sentence for the room:** *"Postgres is the right store, and there's a capacity question in
the application layer that a better database won't answer."*

Relevance held at every size (the needle ranked #1) — with the caveat that the needle shared
little vocabulary with the haystack, so that's a weak positive, not a strong one.

**What's missing is a target.** Nobody has said how many facts per scope this must hold. Without
one there's no bar to design against. The fix direction is cheap and known; it is unowned and
in no workstream.

---

## 7. Postgres, and the graph question

Expect *"aren't graphs supposed to be Neo4j?"*

- **The graph is Postgres**, modelled relationally: `nodes` and `relationships` tables,
  adjacency by two foreign keys, labels and properties in `JSONB`, traversal by SQL. 40 tables
  total. Bi-temporality is in the schema (`valid_from`/`valid_to` alongside `created_at`).
- **There is no second engine today.** Both backends default the Memory Graph plane to
  *themselves*; the registry accepts only `postgres` and `sqlite`. **One instance, one backup
  timeline, one PITR restore.** A second engine would make it two and, per `docs/design/11`,
  makes RTO unanswerable.
- **The repo contradicts itself on the intended engine** — the issue text says Neo4j Enterprise;
  DW-001 names FalkorDB and *explicitly rejects* Neo4j pending an SSPLv1 ruling; Postgres +
  pgvector is the working plan of record. Neo4j is licence-blocked, FalkorDB is
  legal-review-blocked. **Recommend closing this on the record** rather than leaving three
  documents disagreeing.
- **pgvector is a real requirement, not a contingency.** With it: an `embedding_vec` column and
  an HNSW index. Without it: `float8[]` and a hand-written cosine function — **no index**, so
  similarity is a scan. The code treats absence as *"expected, not exceptional"* and falls back
  silently. Our local Postgres has no pgvector, so every semantic-retrieval number we have is
  of the un-indexed path.

---

## 8. What the infra team owns

Don't read this out — hand them `INFRA-BACKLOG.md`. It's self-contained.

The boundary: **who can land the change.** `tf-jennay` / Vault / GCP console / Harness config /
gateway keys are theirs; `src/`, `tests/`, `Dockerfile`, `.helm/`, `.harness/` are ours.

**The four things to say out loud:**

1. **KMS is greenfield** — no `google_kms_*` resources in *any* of the four environments. The
   T0-2 fix needs a real key to fall back to.
2. **No Cloud SQL instance exists** for Memotron. And the database has a hard requirement:
   `LC_COLLATE='C'` is a **correctness** requirement — the receipt ledger and graph state hash
   need byte-ordered comparison, and the app asserts it at startup — plus `ENCODING 'UTF8'`
   explicitly, or `initdb` infers `SQL_ASCII`.
3. **Workload Identity is mis-bound on both halves**, and terraform is the correct side (it
   matches the platform's `svc-mcp-jedai-*` convention). One-line confirmation, then we align
   the chart.
4. **The Harness trigger list is unknown and it blocks merging.** Triggers can live inline, so
   absence from git proves nothing. If one is live, merging ships immediately — and our
   fail-closed guard would *correctly* crash pods on a Postgres cluster without a key manager.

---

## 8b. "Is this real, or your setup?" — audited, with numbers

Expect this question. It is the right one to ask, and the honest answer is stronger than a
reassurance.

Every one of the 57 register items was classified by its **decisive** evidence:

| class | count | survives a clean machine? |
|---|---|---|
| **SOURCE** — a `file:line` in committed code | **33** | yes: `git clone` and a text editor |
| **EXPERIMENT** — a controlled run / RED-GREEN | **9** | yes: clone + compose + a gateway key |
| **LOCAL** — depends on our `.env`, our image | **7** | had to be re-tested |
| **EXTERNAL** — `tf-jennay`, Harness, GCP | **4** | yes, not ours to fix |
| **UNVERIFIED** — self-labelled UNCONFIRMED | **4** | **no — never stated as fact** |

**Then we reproduced the Tier-0 findings off-machine** (`scripts/verify/cleanroom.sh`): fresh
clone, no `.env`, no `.memotron/`, no `.claude/` hooks, every `MEMOTRON_*`/`LITELLM_*`
variable stripped, dedicated Postgres container. `src/` swapped between `f28e95c` and our branch
with the probes held identical:

```
T0-4  main exit=1  fixed exit=0     T0-2  main exit=1  fixed exit=1 (no fix yet)
T0-5  main exit=1  fixed exit=0     T0-7  main exit=1  fixed exit=1 (no fix yet)
regression tests   main exit=1  fixed exit=0
```

**Every Tier-0 reproduces on a machine that shares nothing with the one that found them.**

**Exactly one register item turned out to be our environment: T1-13 (pgvector)** — our compose
file pulls a vanilla `postgres:16`. Moved to an "Environment, not product" section, kept visible.
The real finding inside it survives: the fallback is silent and unindexed.

**Say the weak ones before you are asked.** T4-2 quotes non-deterministic LLM output from one
gateway environment. T1-6 is a derived symptom of T1-1, not independent. T4-3/T4-4/T4-5 are
probably real but filed against the wrong evidence. T0-3/T2-3/T3-6 are labelled UNCONFIRMED and
should stay that way.

## 9. What we got wrong

**Say this part.** It's what makes the rest credible, and it tells the room which numbers to
trust.

- **"`main` has a deterministically failing test."** 3/3 runs, hermetic and with Postgres, zero
  tracked changes — and completely wrong. Every check varied something *inside* the repo; none
  varied the working directory. The same tree passes in a fresh worktree. Cause: a
  cwd-relative `.env`. **That became a real finding** — a Memotron process takes LLM
  credentials from whatever directory it runs in, silently turning a free path into a billed one.
- **"Without the worker, memory serves superseded facts indefinitely."** I escalated this as the
  strongest argument for a worker. It came from running two hooks back-to-back. Formation
  advances on wall clock, so with a realistic gap the correct state is already there. **The
  worker is still needed** — for consolidation, pruning and coherence — just not for that reason.
- **"A verbatim blob can never be superseded."** Derived, labelled as derived, tested, refuted.

**Both retractions share one cause: the harness created conditions that don't occur in use, and
I attributed the result to the product.** The standing rule now: *the probe's conditions are
part of the claim.*

- **"Byte-for-byte replay of any recorded run."** I put this in §3 of an earlier draft of this
  document. It is **too strong.** `byte_replay` is *liveness-anchored* by design
  (`replay.py:495`, "each reconstructed per-scope final equals the live store") — it compares
  against the **current** graph, so only the newest run in a scope can pass. Measured: `['OK']`
  -> `['OK','FAIL']` -> `['OK','FAIL','FAIL']` as writes land. **A run stops replaying as soon
  as another write touches its scope.** It is real and useful — tamper detection — and narrower
  than the name implies. `crypto_shred`'s own docstring makes the same over-claim (**T3-7**), so
  expect it to be quoted back at you.

- **"A formation denial is invisible — no record of why a memory wasn't formed."** **Retracted
  the day before this meeting**, by running the grep the reality audit said was missing.
  `_decide` records **every** decision, approved or denied (`dreaming.py:5383`, unconditional),
  with `approved: false` and the model's reason — queryable via `dream_decisions()` and shown in
  the admin UI. The ledger *does* answer "why don't I remember X". **Then I finished the job and
  the finding came back, correctly scoped:** an ordinary denial emits **no `MemoryReceipt`**
  (the only `_emit_receipt` in that path is the transport-*failure* branch, `dreaming.py:5440`),
  and **`dream_decisions` is not tamper-evident** — no hash or previous-hash column, and
  `verify_chain`/`replay.py` reference it zero times. So denials *are* recorded, in a table that
  can be edited without detection, while the hash-chained ledger has no entry for them. Narrower
  than filed, real, and it compounds **T0-7**.

**Numbers you can quote:** the suite counts, the scale table, the three Tier-0 reproductions,
the terraform facts, the crypto-shred result.
**Numbers you can't:** anything gateway-dependent — extraction counts vary run to run. Quote
the *shape* (gateway extraction scales with content; the no-LLM path doesn't extract at all),
never the counts.

---

**T1-30 — we said the router's tenant guard was dead code. It isn't.**
The claim was that `MemoryRouter` compares a caller-supplied tenant against itself. Attacked
rather than re-read, a router fixed to tenant `alpha` **rejected all four cross-tenant
attempts**, through two independent layers — the control plane refuses a scope not registered
to the named tenant, and the router then refuses a resolved scope that isn't its own. The
original reading traced the tenant id to the request body and stopped before the validation.
The residual point is real but much smaller, and moot today: **nothing in `src/` constructs a
`MemoryRouter` at all.** Refuted before the meeting rather than after.

## 10. Decisions we need

**In the room:**

1. **Capacity target** — how many facts per scope, per deployment? Nothing in the plan states
   one, so there's no bar to design against. *(This is the ask that unblocks §6.)*
2. **Does the Postgres cutover proceed before or after the capacity fix?** Our view: the fix is
   cheap enough that it should land first, because a cutover makes the cost shape harder to
   change, not easier.
   **New input (2026-08-27): the cutover is blocked on a defect either way.** Migration is the
   only path that moves existing memory into Postgres, it works across engines — and a
   re-run **duplicates the destination** (observed 1 -> 2 -> 3, each run reporting success,
   **T1-27**). There is also no CLI route (**T1-34**), so a cutover is bespoke code — the same
   code that duplicates. Neither has a fix. This needs a decision before any cutover date.
3. **Close the graph-engine question on the record** — Postgres + pgvector, and retire the Neo4j
   and FalkorDB references.

**Both "human-only" blockers are now closed — we could do them ourselves the whole time:**

4. ~~Confirm the Harness trigger list is empty before anything merges.~~ **DONE — it is empty.**
   Merging ships nothing. The pipeline is `storeType: REMOTE` (git-synced, so the in-repo YAML
   is the source of record) and last ran **2026-06-11, 76 days ago**.
5. ~~`kubectl get pods`.~~ **DONE — see §4.** The pods start; nothing of Memotron is deployed;
   half the Deployment has been wedged for 37 days.
6. **Still open:** who holds a Harness PAT with `core_service_edit` — the read token we used
   cannot create pipeline resources, so CI onboarding still needs one.

**Deferred deliberately** (13 open design questions in `UNKNOWNS.md`), the sharpest being:
should formation admission consult a model at all? Extraction already has typed policy,
salience, dedup and governance behind it.

---

## 11. Change log

Append here as the day goes. Newest last.

- **2026-08-27, initial draft** — built from the register at 54 backlog items (8 handed to
  infra), 6 Tier-0, 5 unknowns closed / 10 open, 13 design questions, 54 log entries,
  17 harnesses.
- **2026-08-27, complete workflow audit** — the first run was silently capped at 10 of 32
  findings (my orchestration bug, see D-71); resumed uncapped it returned **27 survived, 3
  killed** across all six corpora. Register **62 → 86**, Tier-0 **11 → 14**. §3b rewritten: the
  audit-layer findings went from six to nine and now include a **Tier-0 in certification**
  (T0-14), which I verified by reading rather than trusting the agent.
- **2026-08-27, workflow audit + the reversal** — a 17-agent workflow read the ~30k lines never
  examined; 32 candidates, 10 survived an adversarial refutation pass (which corrected three
  material errors, so it was doing real work). Register 62 -> 72, Tier-0 11 -> 13. **New §3b**:
  the audit layer is now the densest defect area, which reverses this document's earlier framing.
  T0-12 hand-verified rather than taken on trust.
- **2026-08-27, C0 closed** — the Harness trigger list is **empty**, verified with a control (a
  bogus pipeline id also returns an empty 200, so existence was confirmed separately). Merging
  is unblocked. `storeType: REMOTE`, last execution 2026-06-11 — which matches the Deployment
  age and the GAR image date, corroborating T0-9 from a third direction. §10 updated: both items
  we had been calling human-only were ours to do all along.
- **2026-08-27, T0-1 reproduced** — the last Tier-0 without one. First attempt passed and was
  worthless: an empty destination has no epoch, so it cannot trigger the bug. With the
  destination pre-seeded it reproduces, and the finding is *sharper* than filed — memory is not
  destroyed, it is split-brained: `search()` returns the migrated facts while `profile()` shows
  none of them, and the source is purged regardless. Added to §5.
- **2026-08-27, the cluster answered I-1 and rewrote §4** — `kubectl` was available all along.
  Nothing of Memotron is deployed: the namespace runs a June health-check stub at revision 1,
  and half of it has been wedged on a ReadWriteOnce volume for 37 days. Two of our own claims
  refuted (T2-3) or narrowed (T1-3), and our headline corrected: the deployed API does not lose
  memory, it has never held any. Filed **T0-8** and **T0-9**.
- **2026-08-27, T4-4 re-scoped** — finished the half D-58 left unverified. A denial emits no
  `MemoryReceipt` (only the transport-failure branch does) and `dream_decisions` carries no hash
  chain, so denials sit outside the verified ledger. The finding returned in a narrower, proven
  form; §3 and §9 updated. The audit story is now the weakest part of the system, not the
  strongest — worth leading with that honestly.
- **2026-08-27, three weak items closed** — the audit named T4-3/4/5 as filed against the wrong
  evidence. Ran the greps: T4-3 is now source-provable (`runtime.py:133-136` vs `:191`),
  **T4-4 is RETRACTED** (denials *are* recorded), T4-5 corrected to a discoverability problem.
  Third instance of the same error shape — an absence on one surface generalised without
  checking the neighbouring surface. New rule: *"I did not find it" is not "it is not there".*
- **2026-08-27, reality audit** — classified all 57 items by evidence and reproduced every
  Tier-0 in a clean room. 33 source-provable, 9 experiments, 1 downgraded to "environment, not
  product" (pgvector). New §8b. Also fixed three stale citations in the register and committed
  two probes that had never been tracked, so the T0-7 and T3-7 measurements are now reproducible
  by someone other than us.
- **2026-08-27, after verifying the headline** — ran the crypto-shred claim rather than
  presenting it on a reading (D-55). **It holds on every count**, so §3 now says VERIFIED. The
  same probe caught that I had over-claimed "byte-for-byte replay": it is liveness-anchored and
  only the newest run in a scope can pass. §3 and §9 corrected; filed **T3-7** against
  `crypto_shred`'s docstring, which makes the same over-claim. Register now 55 items,
  18 harnesses, 55 log entries.

---

## Appendix — where the detail lives

| | |
|---|---|
| `DISCOVERY.md` | front door to the whole record |
| `docs/findings/README.md` | working-memory file: mental model, reading tracker, open threads |
| `TAKEOVER-BACKLOG.md` | 54 defects, tiered, each with `file:line` or a command |
| `INFRA-BACKLOG.md` | the infra handoff, self-contained |
| `UNKNOWNS.md` | 10 open unknowns with closing actions, 13 design questions |
| `DOGFOOD-LOG.md` | 54 entries — the raw trail, dead ends and retractions included |
| PR **#46**, issue **#45** | the published artifact |

**Honest limit to state if pressed:** `dreaming.py` is 10,533 lines and roughly 15% has been
read with comprehension. The tracker in `docs/findings/README.md` says which regions. Claims
marked DERIVED were reasoned, not observed.
