> **Local working note.** What `dreaming.py` actually does, from reading it rather than
> observing its outputs. Read 2026-08-26. States plainly what is still unread.

# dreaming.py — the engine, read properly

**10,533 lines · 153 methods on `DreamEngine` · 17 module-level functions · no module docstring.**

It is the integration point for everything: coherence, config (24 symbols), crypto,
embedding, extraction, gateway, health, storage, models (21), receipts (10), retrieval,
synthesis, redaction.

## The five biggest methods

| lines | at | method |
|---|---|---|
| **1,131** | `:7382` | `_materialize_episode` — a candidate becomes a graph row |
| 617 | `:1855` | `_run_formation` |
| 553 | `:3718` | `_rollup_pass_for_scope` |
| 349 | `:6883` | `resolve_canonical_entity` |
| 252 | `:6162` | `rebridge_scope_truth_keys` |

## The bi-temporality contract (documented, Zep-aligned)

Two independent axes on every relationship:

- **Valid time** — `valid_from` / `valid_to`. When the fact was true in the world. This is
  the axis `as_of` search operates on.
- **Transaction time** — `created_at`, `reinforced_at`, `superseded_at`, `pruned_at`.
  Immutable once written; audit lineage only.

Supersession is **invalidate-don't-delete**: a contradicting fact sets the incumbent's
`valid_to` to the challenger's `valid_from`. **The graph never deletes rows.**

Only the single `as_of` axis ships. A second `as_known_at` (transaction-time travel) is
deliberately deferred per `NEXT.md` §8.

## The truth slot — how "one fact, one row" is enforced

- `truth_key` is keyed on the **canonical predicate**, never the surface form, so
  "resides in" and "lives in" cannot split one truth into two coexisting rows. The surface
  predicate is stored unchanged.
- Under crypto-shred governance the truth key is a **keyed commitment**, so truth-slot
  equality and prefix seeks still work **over ciphertext**. This is what lets an encrypted
  scope keep working rather than degrading to a scan.
- `truth_identity()` and `node_identity_key()` are **shared with `migration.py`** (WS-13) —
  directly relevant to T0-1, since migration reuses the identity derivation.

## Supersession has at least FIVE distinct paths, not one

My earlier 14/14 lifecycle test exercised exactly **one** of them. Named here so nobody
repeats that mistake:

1. **Historical successor** — a fact dated *earlier* than an existing one. The **new** row
   is inserted already `SUPERSEDED`, with `valid_to` = the successor's `valid_from`. Filing
   old news does not overwrite newer truth. *(This is the path my probe hit in reverse.)*
2. **Ordinary contradiction supersede** — the incumbent is closed at the challenger's
   `valid_from`. **The only path my probe verified.**
3. **Gate-parked dispute** — the contradiction gate refuses to pick a winner. The
   *challenger* lands `SUPERSEDED` carrying `supersession_gate_reason` and
   `requires_operator_review: True`. The slot then **holds contradictory truths** — a
   standing dispute, not a clean supersession. This is what the
   `pending_supersession_reviews` / `resolve_supersession_review` tools exist to drain.
4. **Polarity conflict** (WS-16 T15) — multi-active rows superseded on a polarity clash.
5. **Recency-authoritative bypass** (WS-25 T2) — incumbents auto-closed at
   `recency_effective_from`, receipted distinctly as `recency_authoritative_supersede`.

## Two design details worth knowing

**Anti-hallucination clamp (WS-25 T2).** `recency_effective_from = min(observed_at,
episode.reference_time)`. The extractor's claimed `valid_from` can never postdate the
episode that carries the observation, so *"a hallucinated or spoofed future `valid_from`
cannot deterministically out-rank a fact dated closer to the true observation instant."*
The clamp applies **only** to the T2 bypass; unclamped `observed_at` keeps its meaning
everywhere else. Adversarial input was considered.

**Disputes discount the alias that bridged them (WS-17 T16b).** When two sides of a
contradiction were stated under *different surface forms* of the same canonical subject,
that dispute is treated as live evidence **against the bridging alias**, and the alias is
discounted. The system treats its own entity-resolution as falsifiable by the conflicts it
produces.

## Two write paths with deliberately different failure semantics

`_materialize_episode` takes `raw_candidates_stored`:

- **`True`** (`_run_formation`, `regovern_scope`) — an unresolvable relationship/memory type
  **quarantines that one candidate and continues**. Never aborts the run.
- **`False`** (operator path: `add_memory`, `correct_memory`) — **fail-fast**, unchanged.

The stated reason: *"a caller who names an unresolvable relationship type made a mistake in
a single deliberate write, not a model that produced 999 good candidates and one bad one."*

## Receipts emitted by materialization

Only four call sites: `FORMATION_RELATIONSHIP_CREATED` (with graph-state hash before/after),
`FORMATION_SEMANTIC_DEDUP_REINFORCED`, and `FORMATION_TRUTH_KEY_SUPERSEDED` (×2).

## Dedup and reinforce — how memories merge instead of accumulating

- **Dedup compares OBJECT-ONLY embeddings**, not full-fact. Stated reason: candidates in a
  slot already share subject+predicate, so *"object is the discriminating signal."*
  Comparing whole facts would be dominated by the shared prefix.
- **Two write verbs, one receipt.** `EXACT_UPDATE` (string match, cosine recorded as 1.0)
  and `SEMANTIC_UPDATE` (embedding ≥ threshold) both emit
  `FORMATION_SEMANTIC_DEDUP_REINFORCED` — deliberately, to *"close the exact-update audit
  gap."*
- **Threshold precedence:** Motive override > per-type `DedupPolicy`. The threshold actually
  used is written onto the receipt, so a merge is auditable against the bar it passed.
- **Reinforce is bracketed like a create.** It mutates `observed_count` / `last_seen` /
  `confidence` — all graph-state-hash inputs — so it records before/after hashes and byte
  replay folds reinforced receipts too.
- **Verbatim guard (WS-28 T3):** a `runbook_capture` candidate sets
  `force_identifier_conflict`, so *"a runbook-capture candidate's verbatim command must
  never be paraphrase-merged into a different command."* Two different shell commands cannot
  semantically merge.
- **Authority class:** a candidate from `artifact_class == "generated_output"` is classed
  `non_normative` rather than `memory` — model output does not carry the same weight as
  observed fact.
- **Replay integrity depends on a pre-resolution digest.** The candidate digest is taken over
  the *pre-entity-resolution* form, because `CANDIDATE_EXTRACTED` recorded that shape and
  *"the replay fold matches dispositions to candidates by this digest."*

## Reviewer finding #2 — mechanism confirmed by reading (not by repro)

The PR review claimed re-dream rollback is not byte-for-byte because `_apply_reinforce`
never restamps `epoch_id`. **The stated mechanism is real**, verified along the full chain:

1. `_apply_reinforce` (`dreaming.py:9222`) — **zero** `epoch_id` references in its body; it
   passes a properties dict of `confidence`, `confidence_strategy`, `episode_uuid`,
   `observed_count`, `last_seen_at` and similar.
2. `update_relationship` (`storage/sqlite.py:1133`) — `relationship.properties.update(...)`,
   a **merge, not a replace**.

So a reinforced row **retains whatever `epoch_id` it already carried**. Under adopt-merge
that is the parent epoch, which is precisely what the reviewer said prevents
`rollback_epoch` from hiding it.

**What this does NOT establish:** that the *consequence* (two ACTIVE versions of one truth,
`graph_state_hash` not restored) actually occurs. That still needs a behavioural repro.
Mechanism verified; effect assumed.

## Consolidation (rollups) — recursive clustering

`_rollup_pass_for_scope` (`:3718`) returns `(rollups_created, members_demoted, decision_count)`.

- **Depth-based recursion.** Depth 1 clusters raw facts (`rollup_depth` absent/0); depth 2
  clusters depth-1 rollups. **Themes can roll up into themes.**
- **Eligibility:** `ACTIVE` and `active_in_context` and not already a `ROLLUP` at this depth
  and not a `MENTIONS` edge and `rollup_depth == depth - 1`. Demoted children are excluded
  deliberately — *"their active parent carries the view."*
- **Clustering is greedy agglomerative, deterministic, hermetic, stdlib-only.** Seed with the
  first unclustered member; absorb any member whose cosine ≥ `cluster_threshold`. Single
  pass, O(n²) within the candidate set.
- **Vector-space guard (WS-17 T18):** a stored vector joins clustering **only** if its stamped
  identifier matches the active embedding transport. A mismatched or unstamped legacy vector
  is treated as **absent**, not compared across spaces. This is the guard against the
  "switching embedding models invalidates every stored vector" problem — handled, not ignored.
- Rollup labels and facts are **sealed like formation writes** under crypto-shred governance.

## SCALE: eleven unbounded full table scans in the engine

`PropertyGraphStore.relationships()` is `SELECT * FROM relationships ORDER BY created_at,
uuid` — **no scope filter, no limit** — deserialising every row into a `GraphRelationship`.
A scoped alternative, `relationships_for_scope(scope_key)`, exists at
`storage/sqlite.py:990` and is **not** used by these call sites.

`dreaming.py` calls the unfiltered version **11 times**:

| line | method | why it matters |
|---|---|---|
| `:5158` | `_graph_context_for_episode` | **called per episode during formation** — a full scan for every episode processed, and this is the function that feeds the decision gate behind T1-1 |
| `:4914` | `invalidate_rollups_for_dependency` | **inside a `while pending:` loop** — a full scan *per node* of the rollup dependency walk |
| `:3759` | `_rollup_pass_for_scope` | scans everything, then filters by scope in Python |
| `:9440` `:9860` | `_run_pruning`, `_apply_soft_cap` | every pruning cadence |
| `:2543` `:4692` `:4743` `:5011` `:5082` `:10339` | context counts, stale-rollup rebuild, rollup reinforcement, duplicate re-promotion, consolidation scopes, single-active repair | |

**Observed:** the query text, the 11 call sites, the loop nesting at `:4914`, and that
`_graph_context_for_episode` sits in the per-episode formation path.
**Derived:** cost is therefore O(total rows in the database) per episode and per dependency
node, independent of how small the scope is.
**NOT measured:** no benchmark was run. At the 3–9 row graphs used all session this is
invisible. This is an architectural observation awaiting a timing test — see U-1.

---

## What I have NOT read

Honest boundary. I read the module header, the decision-fallback contract (`:149-217`),
`_materialize_episode`'s docstring and roughly its first 500 lines, and the truth-slot /
supersession core (`:7761-7885`).

**Unread:** ~9,000 lines — including the whole of `_rollup_pass_for_scope` (553),
`_run_pruning` (174), `_apply_soft_cap` (205), `scan_coherence` (148),
`resolve_canonical_entity` (349), `rebridge_scope_truth_keys` (252),
`_cross_prefix_duplicate_sweep` (190), `regovern_scope` (195), and the reinforce/dedup half
of `_materialize_episode` (`:7908` onward).

**The single most useful correction from this read:** supersession is five mechanisms, not
one, and the lifecycle probe covers one of them. "Supersession works" was true and much
narrower than it sounded.
