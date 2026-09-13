# Unknowns — what we have NOT established

Companion to `TAKEOVER-BACKLOG.md`. That file lists what we know is broken; this one lists
what we have not tested, so nobody mistakes an unswept area for a clean one.

**Why this file exists.** Every finding is only as good as its boundaries. The lifecycle
passes 14/14 — on a graph of four facts. Stating the boundary is the difference between
"the memory system works" and "the memory system works at the scale we tested."

**Two different things live here, and conflating them is a mistake:**

- **Unknowns** (U-n) — testable. We simply have not run the test. These close with evidence.
- **Open questions** (Q-n) — design and intent. These do **not** close with a test; they need
  someone who knows what the system was meant to do. **Do not answer them by inference.**

We are in discovery. Collecting good questions is the deliverable; resolving them is a later
phase. A question recorded honestly is worth more than an answer guessed confidently.

Unknowns ranked by **what would change the plan if we learned them.**

Legend — `CLOSES WITH`: the specific action. `COST`: rough effort.
Status: `OPEN` · `IN PROGRESS` · `CLOSED — <finding>`

---

## U-1 · Does retrieval return *relevant* memory, or just *current* memory? — OPEN

**The product's central claim is relevance.** We have proven it returns **current** truth
(stale facts superseded and excluded). We have never tested whether, given hundreds of
facts, the *right* ones surface for a given task. Every lifecycle assertion ran on a graph
of 3–9 facts, where "return everything" and "return the relevant thing" are
indistinguishable.

**Why it matters:** if relevance degrades with volume, the product fails exactly when it
starts being useful — and no amount of infrastructure work fixes that.

**CLOSES WITH:** seed a graph with 200–500 facts across several subjects, then assert that
a task-specific query surfaces the task-relevant facts and that `profile` respects its
per-type caps and token budget. Compare `search` vs `semantic_search` vs `profile` ranking.
**COST:** half a day. **Blocked by:** nothing.

## U-2 · Does the lifecycle behave the same for *formed* memories as for written ones? — OPEN

Every lifecycle check used `add_memory` (client-managed, direct write) because formation is
suppressed (T1-1). Dream-formed memories carry different provenance, confidence, salience
and motive metadata. Supersession, dedup and pruning may all behave differently for them.

**Why it matters:** it is the difference between "supersession works" and "supersession
works for the path users don't actually use."

**CLOSES WITH:** apply the T1-1 fix locally, form memories via the LLM, then re-run
`scripts/verify/probe_core_loop.py` against a graph of formed rather than written facts.
**COST:** 1–2 hours. **Blocked by:** nothing — the fix is validated, just unapplied.

## U-3 · What is the formation gate actually *for*? — **CLOSED**

The proposed T1-1 fix withholds graph context from the gate. **No test covers what the gate
was designed to do**, so we cannot tell whether we would break an intended behaviour — e.g.
refusing an episode already fully represented in memory.

**Why it matters:** we are about to change the product's central control point based on
observed misbehaviour, without knowing its purpose.

**CLOSED 2026-08-26 — the fix is aligned with documented intent, and the current behaviour
contradicts it.** `dreaming.py:156-217` states the rule for all six dream-agent decision
points:

> *a fallback may approve CREATION; it may never approve REMOVAL.*

`formation_episode_selected` is deliberately **fail-OPEN** — it is not in
`_FAIL_CLOSED_DECISION_TYPES`. Its own entry says:

> *"admitting an immutable episode to formation. **Purely additive, and approving it decides
> nothing about what may be written**: redaction, salience, dedup, Motive type filters, the
> supersession authority gate, and the fail-closed untrusted-directive gate all still run
> downstream on every candidate the extractor produces. **Failing closed here would silently
> stall the system's core function for the whole duration of an outage.**"*

Three consequences:

1. **The gate is an admission step, not a quality filter.** Every real filter runs
   downstream. Blinding it to graph context removes no safety property — the design says so
   in as many words.
2. **The design anticipated this exact harm** ("silently stall the system's core function")
   and guarded the *transport-failure* path. It did not guard the path we hit: the model
   answering, confidently, **no**. That is strictly worse than the outage it was designed
   against, because no fallback fires.
3. **The observed behaviour contradicts documented intent.** This is not a feature we are
   removing; it is a control point behaving contrary to its own specification.

This closes the *factual* question — what the gate is documented to do. It raises design
questions it does not settle; those are **Q-1** and **Q-2** below, and are deliberately left
unanswered.

## U-11 · Four of five supersession paths are unverified — OPEN

Reading `dreaming.py` showed supersession is **five distinct mechanisms**, not one
(`docs/findings/dreaming-architecture.md`). The 14/14 lifecycle probe exercises exactly one
— ordinary contradiction supersede. Unverified: historical-successor back-dating,
**gate-parked disputes** (`requires_operator_review`), polarity conflict (WS-16 T15), and
the recency-authoritative bypass (WS-25 T2).

**Why it matters:** "supersession works" is the load-bearing claim under "non-stale memory".
It is currently evidence for one fifth of the mechanism. The gate-parked path is the most
interesting — it leaves the slot **holding contradictory truths** pending operator review,
which is a state no probe has ever produced.

**CLOSES WITH:** extend `scripts/verify/probe_core_loop.py` with one fixture per path —
back-dated fact; two same-authority contradictions to trip the gate; a polarity flip; a
future-dated `valid_from` to exercise the WS-25 clamp. Assert the receipt kind for each.
**COST:** half a day. **Blocked by:** nothing.

## U-4 · Consolidation, pruning and coherence are barely exercised — OPEN

`run_coherence_scan` returned one incident; nobody checked it was the *right* incident.
Themes (rollup consolidation) have never been observed forming. Pruning has never been
observed removing anything. These are three of the four dream job kinds.

**CLOSES WITH:** a probe per job kind with a designed fixture — for coherence, a known
contradiction that *should* be detected plus a near-miss that should not; for consolidation,
a cluster that should roll up; for pruning, low-salience rows past the retention window.
**COST:** a day. **Blocked by:** U-2 (needs formed memories to be realistic).

## U-5 · No real editor session has ever run against this — OPEN

Hooks were driven with synthetic JSON payloads. A live Claude Code session with the MCP
server attached, hitting a real compaction boundary, has never happened.

**Why it matters:** the hook contract (payload shape, timing, the 60s timeout, what happens
when a hook fails) is only verified against payloads we invented.

**CLOSES WITH:** restart Claude Code in this worktree so `.mcp.json` loads, work until a
real compaction fires, then inspect the graph and receipts for what was captured.
**COST:** ~free, but needs a real working session. **Blocked by:** human.

## U-6 · Four of six known PR-#44 defects were never independently reproduced — OPEN

We verified the migration `epoch_id` copy and the `_epoch_visible` backend divergence. The
reinforce-rollback non-determinism, the missing polarity guard, and the unconditional
audit-read tiebreaker are carried from the reviewer's analysis, not re-verified here. They
are now on `main`.

**PARTIAL — finding #2's mechanism is now confirmed by reading** (`dreaming.py:9222` has no
`epoch_id`; `storage/sqlite.py:1133` merges rather than replaces, so a reinforced row keeps
its old epoch). The **consequence** — two ACTIVE versions, state hash not restored — is still
unreproduced. Findings #3 (polarity guard) and #4 (audit-read tiebreaker) remain untouched.

**CLOSES WITH:** one focused probe each, following the reviewer's stated repro.
**COST:** half a day. **Blocked by:** nothing.

## U-7 · Do the deployed pods start at all? — OPEN

T2-1 + T2-2 predict they cannot. Never confirmed against the cluster.

**CLOSES WITH:** `kubectl get pods -n jedai-memotron` — look at RESTARTS.
**COST:** one minute. **Blocked by:** cluster access (human).

## U-8 · Does a pipeline trigger exist? — OPEN

No trigger file exists in-repo, but live triggers can be inline in Harness. If one exists,
the #44 merge may already have built over the running image tag (T2-5).

**CLOSES WITH:** Harness UI → `JedAI_Memotron` → Triggers; and check whether `0.1.1` in
GAR has a new digest. **COST:** two minutes. **Blocked by:** Harness access (human).

## U-14 · Does supersession still work on Postgres? — CLOSED 2026-08-27

D-48 verified the truth slot correctly supersedes a contradicting compaction summary, but
**every arm of that probe was SQLite and rule-based.** T0-4 makes Postgres `relationships_for_scope`
return structural MENTIONS rows (no `fact`, no `truth_key`, `scope_kind=None`), and
`_materialize_episode` consumes exactly that call — so the slot-matching input differs between
backends on the one code path that decides whether a fact supersedes or coexists.

This is the highest-stakes untested consequence of T0-4. `search()` raising a `KeyError` is
loud; a truth slot that silently fails to match would leave **two contradictory rows both
`active`**, which is the failure mode the whole design exists to prevent, and nothing would
report it.

**CLOSED — supersession is intact on Postgres** (D-49,
`scripts/verify/probe_supersession_backend.py`). Both backends produced 1 superseded + 1
active with an identical `truth_key`; the Postgres arm's write was attributed (0 -> 6 rows,
`PostgresStorageBackend`). **DERIVED mechanism:** the MENTIONS rows T0-4 leaks carry
`truth_key=None`, so they are noise in the candidate set and cannot produce a false slot match.

**The closing move recorded here was wrong and had to be replaced.** It named
`probe_nollm_supersession.py`, which routes through `build_platform_from_project` — and T0-5
means that path never honours the DSN. Run that way it reported a clean Postgres pass while
never touching Postgres. A new probe that constructs `Memotron` directly, and attributes
its write before trusting the result, was needed. **Lesson: a closing move must be checked for
the defects already on the register before it is trusted.**

## U-13 · Why did the first episode form without an explicit dream drain? — CLOSED 2026-08-27

D-48, observed and unexplained: hook 1's episode materialised with no explicit
`run_due_dreams()`, hook 2's did not. Plausibly the formation job's `cadence_seconds=1` fired
for the first and the second fell inside the window. **ASSUMED, unverified.** It matters
because it sets how long a deployed scope can assert a superseded fact (T1-15).

**CLOSED — the cadence hypothesis holds** (D-50). Three hooks, gap the only variable, no
explicit drain ever called:

```
gap=0s   3 episodes stored, 1 formed   HAS_STATE after each hook: [1, 1, 1]
gap=3s   3 episodes stored, 3 formed   HAS_STATE after each hook: [1, 2, 3]
```

Formation advances on **wall clock**, via the formation job's `cadence_seconds=1`. A hook whose
dream is due forms its own episode inline. **This corrected T1-15**, which I had filed on the
back-to-back timing as "serves superseded facts indefinitely" — with a 3s gap the correct
`superseded`/`active` pair is present before any drain.

## U-12 · Is rule-based extraction input-sensitive at all? — CLOSED 2026-08-26

Measured (D-46): rule-based extraction returned **exactly 3 relationships**
(`{MENTIONS: 2, HAS_STATE: 1}`) for BOTH a one-line fragment and a four-fact paragraph, while
gateway extraction returned 0 and 9 for the same two inputs. That looks like a fixed skeleton
rather than content-driven extraction, but **n=2 — signal, not measurement.**

Why it matters: the plan's open decision #3 asks whether to degrade to no-LLM formation at a
budget ceiling. If the no-LLM path emits a constant regardless of what the episode says, that
is not graceful degradation — it is memory that looks present and carries no information.

**CLOSED — ran it** (`scripts/verify/probe_extraction_density.py`, 6x2 grid, D-47).
Rule-based produced **exactly 1 memory at every density, 0 through 8 facts (spread 0)**;
gateway produced 0,1,2,4,5,6 (spread 6) and correctly wrote **nothing** for an episode with
no durable content.

**The answer is sharper than the question.** The rule-based path is not weakly sensitive to
input — it is not extracting at all. It writes the entire sanitized episode body verbatim as
one `HAS_STATE` fact (measured: a 670-char episode became a 692-char fact, untruncated). That
is continuity capture working as designed — the adoption test asserts the body is inside the
fact — but it means "degrade to no-LLM formation" is not degraded extraction, it is **no
extraction**: one unsupersedable blob per episode, written even when the episode says nothing.

**Opened by this:** whether anything the no-LLM path writes can ever be superseded (DERIVED,
untested — a verbatim blob has no canonical predicate for the truth slot to key on). Tracked
in D-47 with its closing test.

## U-9a · Does anything else diverge between the two backends? — CLOSED 2026-08-26

T0-4 found `relationships_for_scope` diverging (missing MENTIONS filter) in a way the
3,310-line parity suite does not catch. That was found by accident, on the first Postgres
call of the session. **The question is no longer "does Postgres scale" but "how much of the
Postgres backend has ever been exercised through the SDK rather than through parity tests?"**

**CLOSED — ran it.** `sweep_sdk.py` against live postgres:16, seeded identically, diffed
per method against the SQLite baseline. **140 methods swept; 4 verdict changes, and all four
are ONE defect, not four** — the harness harvests `relationships_for_scope(...)[0]` as its
target, and on Postgres that is a MENTIONS edge, so `correct_memory`, `forget_memory`,
`memory_evidence` and `redact_relationship_version` were each handed a structural edge and
**correctly refused it**. Those methods are not broken; the harness was fed a different row.

**The real yield was not in the verdict diff.** Following the divergent call
(`relationships_for_scope`, 16 call sites) reached `epoch_content_digest`, which is
**non-deterministic on Postgres** — SQLite 3/3 identical, Postgres 4/4 different for
byte-identical content. Folded into **T0-4**, which is now materially wider than the
`search()` symptom it was filed as.

**What this says about the parity suite:** 3,310 lines and 117 tests assert on raw rows, so
a divergence in what a *scoped read returns to a caller* passes straight through. The suite
tests that both backends store the same bytes, not that both answer the same question.

## U-9 · Postgres migrations 6–8 have never run anywhere — OPEN

Not in CI, not in a cluster. The SQLite path is probe-ALTER; Postgres is an ordered list.
Parity is asserted by tests that only run with a live database.

**CLOSES WITH:** already possible — `MEMOTRON_TEST_POSTGRES_DSN` + `postgres:16-alpine`
runs the 66 gated tests. Do it on every migration change.
**COST:** 3 minutes per run. **Blocked by:** nothing.

## U-10 · Multi-agent mode is untested — OPEN

Everything ran in `simple` mode. `multi-agent` changes memory ownership, scope routing and
visibility. `memory_set_visibility` and `memory_promote` exist for it and were only ever
called with wrong-scope arguments.

**CLOSES WITH:** `memotron init --mode multi-agent` in a scratch project, two registered
agents, and a check that agent A cannot read agent B's private memory.
**COST:** half a day. **Blocked by:** nothing.

---

# Decision drafted for the repo's decision log (NOT yet in the repo)

Written in the house style of `docs/operational-store-decisions.md` (DW-020..DW-024) so it
can be pasted there **by the repo owners** when the decision is actually made. Kept here
because it is our working understanding, not yet a repo commitment.

## DW-025 — The KEK provider is unchosen, and the current default silently destroys data

**Status: OPEN. This is the one entry in this file that is not a decision — it is
a decision that has to be made before `operationalStore.enabled` is set anywhere.**

This document runs DW-020..DW-024 without mentioning key management once, which
is the finding. The Operational Store inherited envelope encryption from the
SQLite substrate without inheriting a key *source* appropriate to a multi-replica
deployment.

`PostgresStorageBackend.__init__` resolves `key_manager if key_manager is not
None else LocalKeyManager.ephemeral()` (`storage/postgres/__init__.py:110`), and
`client.py:157` calls `create_storage_backend(self.config.storage)` with no
`key_manager` — the identifier does not appear anywhere in `client.py`. The
factory accepts one (`storage/factory.py:84`) and nothing supplies it. So each
process mints a random in-memory KEK that, per `crypto.py:234`, "dies with the
process."

**Reproduced 2026-08-26** across two Compose replicas against one Postgres:
replica A wraps a DEK, replica B raises `ValueError: wrapped DEK failed
authentication (wrong KEK or tampered)`. The same failure occurs on any restart
of a single replica. It is invisible to the hermetic suite (one process, one
manager) and invisible to a single-replica smoke test; it surfaces the first time
the HPA adds a pod, as intermittent decryption failures on retrieval. The blast
radius is every sealed field written before the event — an unintentional,
silent, cluster-wide crypto-shred.

**Candidates.** *GCP KMS* — the pod's existing Workload Identity is the auth, so
no second credential and no dependency on the Vault AppRole (still
`REPLACE_ME_WITH_APPROLE_ROLE_ID`) in the crypto path; and it provides
`ScheduleKeyDeletion`, which `client.py:4060` and
`storage/postgres/_governance.py:316` both already name as the production RTBF
step. *Vault transit* — one fewer cloud dependency and consistent with how every
other secret arrives, but it puts the crypto path behind the same Vault whose
AppRole is not yet provisioned, and it has no equivalent scheduled-destruction
primitive. Note there are currently **no `google_kms_*` resources anywhere in
`tf-jennay`** across all four environments, so KMS is greenfield terraform.

**The argument most likely to decide it** is not convenience but restore
semantics: a point-in-time recovery of Cloud SQL can resurrect a wrapped DEK that
was destroyed to satisfy an erasure certificate, un-erasing data already
certified as erased. If the KEK lives outside the database and its per-scope key
has been scheduled for destruction, the resurrected row stays unreadable and the
certificate holds. Any candidate that keeps the wrapping key inside the restored
blast radius fails this test.

**Whatever is chosen, one guard is not optional:** construction must fail closed
when the engine is Postgres and the resolved manager is ephemeral. Two lines,
and it retires the failure mode permanently rather than relying on every future
caller remembering to pass a manager.

**Cost accepted either way:** a network round trip on the wrap/unwrap path and a
new hard dependency for pod startup. The SQLite `0600` sibling-file KEK
(`storage/sqlite.py:131`) stays as-is — it is correct for a single-process local
platform and must never be the deployed path.

---

# Open design questions

**Do not answer these by inference.** They need someone with design context, or a decision.
Recorded because the question is the valuable artifact in a discovery phase.

**Q-1 · Should formation admission consult a model at all?**
Its own docs say approving it *"decides nothing about what may be written"* — every real
filter runs downstream. So what does the model call buy? It costs a gateway round trip per
episode and is the single point where the whole product currently stalls.

**Q-2 · What was the formation gate meant to catch that the downstream filters don't?**
If there is a real case, blinding it to context (the proposed fix) may lose it. If there
isn't, the gate is dead weight. Nobody has told us which.

**Q-3 · Is the LLM veto in front of extraction the right architecture?**
Broader than Q-1. Extraction already has typed policy, salience thresholds, dedup,
governance and motive type filters. What is the veto's role in the intended design?

**Q-4 · What is `semantic_search` supposed to do that `search` does not?**
They return different results on the same graph, and one is broken over MCP. What is the
intended distinction? Which of them are the profile and the context injection meant to use?
Is one of them meant to be the primary retrieval path?

**Q-5 · Is the machine-global default graph path intentional?**
`init` writes `~/.memotron/memory.sqlite`, shared by every project on the machine. That
enables cross-project memory — plausibly a feature — and simultaneously creates the
`init`-can-destroy-another-project's-memory path. Which was intended?

**Q-6 · Is `migrate` defaulting to a destructive move intentional?**
`--keep-source` is opt-in; the code calls it "a destructive move" in its own error text. Is
move-by-default the product decision, or an accident?

**Q-7 · Is the standalone admin server meant to be read-only?**
The platform API is only enabled by `local_platform`. That could be a deliberate split
(inspection vs operation) or an oversight. It determines whether T1b-9 is a bug or a
missing flag.

**Q-8 · What are Motives for in practice?**
The memory bank ships 20; the default job uses one (`agent-memory`) with a support-desk
prompt profile applied to engineering content. Are operators expected to select them per
job, per scope, per tenant? Nothing we ran suggested a selection mechanism in use.

**Q-9 · Should the receipt ledger cover formation *denials*?**
It covers materializations and rejections at the candidate stage, but a gate denial produces
no receipt and no negative-space entry. Is that intended scope, or a gap?

**Q-10 · What is the intended relationship between `simple` and `multi-agent` modes?**
Which is the expected default for a team? `memory_promote` / `memory_set_visibility` exist
for multi-agent and we have never seen them used as designed.

**Q-12 · What is the capacity target, and who owns it?**
Writes are O(n²) in graph size (measured). A scope that accumulates a year of engineering
decisions is the normal case, not the extreme one. How many facts per scope, and per
deployment, must this hold? Nothing in the plan or the design docs states a target, so there
is no bar to design against and no way to know whether the current shape is a problem or a
non-issue at the intended volume.

**Q-13 · Is the unfiltered `relationships()` deliberate?**
An index-backed `relationships_for_scope()` exists and the 11 hot call sites do not use it.
Is that an oversight, or is there a correctness reason the engine needs to see rows outside
the scope it is working on — cross-scope rollups, entity resolution, coherence?

**Q-11 · What does "relevant" mean here, operationally?**
The product's claim is relevant, non-stale memory. Staleness has a precise mechanism
(supersession). Relevance does not obviously have one — is it salience, recency, embedding
similarity, motive filtering, or the token budget doing the work?

---

## Closing order

1. **U-3** (what the gate is for) — cheapest, and it gates whether the T1-1 fix is right.
2. **U-2** (formed vs written) — unblocks U-4 and validates the lifecycle for the real path.
3. **U-1** (relevance at scale) — the product's central claim, still unmeasured.
4. **U-7 / U-8** — two minutes each, and they change deployment sequencing. Needs a human.
5. Everything else.
