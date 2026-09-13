The audit workflow is running: seven parallel code auditors — one per capability area (priority/freshness, dedup/compression, motives/extraction control, scopes + promotion, hooks, truth-during-dreaming, plus a value-prop evidence check) — each grounding claims in `file:line` citations from source, not the docs. Every gap an auditor claims then goes to an adversarial verifier that tries to disprove it before it reaches the report. I'll deliver the full report — per-area technically dense bullets with yes/partial/no/gap states, then the final value-prop assessment — as soon as it finishes.

The audit is done — 19 agents, every claimed gap adversarially re-verified against source (none refuted; a few softened to "partial"). Here is the report.

---

# Platform gap report

## 1) Priority & freshness of memories — **PARTIAL**

- **Works:** search rank = `(1.0·relevance + 0.3·confidence + 0.2·recency + 0.4·scope_priority) × type_weight` with 30-day half-life decay ([retrieval.py:181](src/memotron/retrieval.py:181)); profile injection ranks by `confidence×observed_count` under token budget with a **type-level** identity floor (identity+requirement guaranteed ≥25% of budget, [client.py:5686](src/memotron/client.py:5686)); overflow facts degrade to `[REF:type] uuid` lines rather than vanishing (profile only).
- **Works:** corrections structurally outrank experience — `correct_memory()` writes authority OPERATOR (rank 40) > USER (30) > AGENT (20) > untrusted (10) > generated_output (0); lower-authority contradictions are parked pre-SUPERSEDED with `requires_operator_review`; correction rows are permanently prune-exempt. Supersession is invalidate-don't-delete (`valid_to`, `superseded_by_relationship_uuid`), excluded at read.
- **Works:** pruning never deletes — `status=PRUNED` + restorable `PruneGhost` that a later matching search auto-restores with a regret-tracked receipt; `use_need = (1−e^(−use_stability))·outcome_quality` (Beta posterior) blended 0.7/0.3 with recency.
- **GAP (verified):** no per-fact pin/always-retrieve anywhere — the floor is per-type, and `directive` isn't in the floor set, so an individual safety directive can be outscored off both surfaces.
- **GAP (verified):** correction only fires on exact `truth_key` (scope:subject:predicate) — a paraphrased predicate ("resides in" vs "lives in") leaves both facts ACTIVE; the only bridge is a prompt instruction begging the extractor to reuse predicates.
- **GAP:** the "experience" half of freshness is unpopulated by default — nothing auto-records `CITED_OR_USED`/outcomes, so `use_need` collapses to 0 and prune ranking degenerates to recency; with default config (no confidence decay, `soft_cap=None`, `active_max_age=None`) an uncontradicted fact is immortal.

## 2) Deduplication / compression — **PARTIAL**

- **Works:** two-pass dedup at materialization — exact object match, else object-embedding cosine ≥ 0.88 (per-type + Motive override) restricted to same truth_prefix + matching authority/claim_mode/stance/polarity; reinforce = `observed_count+=1`, `confidence=max()` ([dreaming.py:3949](src/memotron/dreaming.py:3949)).
- **Works:** consolidation clusters at cosine 0.75/min 3, gated by Motive→THEME; members demoted (`active_in_context=False`, `summarized_by`) behind the two-tier indexed read; themes go stale on member change, re-promote members as fallback, rebuild deterministically; depth-2 themes-of-themes supported.
- **GAP (verified):** THEME text is not intelligent synthesis — the label is a top-6 shared-token bag (`_synthesize_theme_label`, 80-char cap); the LLM summarizer is literally a comment ("a clean seam is left here") and `ConsolidationSynthesisProfile.model_identifier` is never consumed. This is the weakest link in "serve the truth compacted."
- **GAP (verified):** no token-savings measurement exists — `compression_ratio` is episodes-per-visible-fact (a count), `tokens_used ≤ budget` is enforced but never compared to an uncompressed baseline; zero hits for any tokens-saved metric.
- **GAP:** dedup is bucket-local (same subject+predicate only) and the 256-dim trigram embedding is vocabulary-sensitive, so genuinely paraphrased duplicates coexist until thematic clustering softens (not merges) them.

## 3) Control over extraction / motives — **YES (control) / PARTIAL (lifecycle)**

- **Works:** all 8 Motive levers verified in code with receipts: prompt profile/override + motive-goal injection, salience rubric (precedence Motive > job > instruction-set), `allowed_memory_types` formation gate (receipted `FORMATION_MOTIVE_TYPE_FILTERED`, also fail-fasts direct `remember()`), `dedup_threshold`, governance (PII redaction pre-extraction), retention override (`policy_source="motive:<name>"`), THEME gate, and `retrieval_budget_share` (profile token share + ×(1+share) search boost).
- **Works:** every episode binds a frozen, DSSE-attested `FormationContract` before any decision; motive changes go through a certified pipeline (certify → stage → shadow → activate) that refuses uncertified contracts; 15 builtin motives ship in the bank.
- **GAP (verified):** Motives cannot touch the extraction **system prompt** — only the user-message prompt; the temporal contract/confidence-calibration guidance is per instruction-set (per job), not per Motive.
- **GAP (verified):** no per-memory motive version pinning — raw facts store only `motive_name` (THEMEs do pin `theme_motive_digest`; receipts carry per-decision `motive_version_digest` but the join is unindexed); editing a same-named Motive retroactively governs old memories, and **deleting/renaming one crashes pruning** for its orphaned rows (`ValueError`, no fallback, [dreaming.py:4780](src/memotron/dreaming.py:4780)).

## 4) Multiple scopes + voting up — **PARTIAL, headline gap**

- **Works:** AGENT/TENANT/USER scope kinds (project = `tenant:<project_id>`), index-backed (`gen_scope_key`); `memory_start` splits budgets project 800 / personal 900 / continuity 300; dreaming (formation/consolidation/pruning) is strictly per-scope; facade ACL: an agent principal gets exactly {own agent, project, user} scopes — agent A can't read agent B (test-asserted).
- **Caveat:** in *search*, project scope actually ranks **lowest** — scope order is [user, agent, project] and `scope_priority` rewards earlier scopes; "project is highest" holds for profile ordering, not search rerank.
- **GAP (verified):** no object-level promotion/"vote up" — the only path is `memory_publish(content: str)`, which re-serializes the fact as free text into a project-scope candidate episode that must survive full re-extraction under the project Motive. No API takes a `relationship_uuid` upward, no vote counting or endorsement threshold, and dedup-reinforcement is the only implicit convergence signal.
- **GAP (verified):** no lineage from a promoted project fact back to the source scoped memory — only free-text `source_reference` + a forensic join through use events.
- **GAP:** ACL lives only in the platform facade; the core client/store accept arbitrary `scope_keys`, and there is no per-memory visibility finer than scope equality.

## 5) Retrieval hooks — **PARTIAL, one broken path**

- **Works:** `memotron init` installs SessionStart/PreCompact/PostCompact/SessionEnd hooks + always-load MCP server + rules/skill files; SessionStart prints the three typed profiles into context and records INJECTED use events; every hook ends in `memory_refresh` → `run_due_dreams` over agent/user/project scopes.
- **Works:** compaction survival is real but indirect — SessionStart is matcher-less, so Claude Code re-fires it with `source=compact` after compaction, re-injecting memory with a fresh task_run_id.
- **GAP (verified, worse than expected):** the PostCompact checkpoint path is **dead in the real harness** — `compact_summary` is not a documented hook-stdin field, so `sanitize_checkpoint("")` raises and the hook exits 2, storing no checkpoint and skipping its refresh; only the synthetic test fabricates the field ([cli.py:471](src/memotron/cli.py:471)).
- **GAP (verified):** PreCompact captures nothing from the dying context — it logs a hardcoded placeholder string and never reads `transcript_path` or any conversation content; agent-initiated `memory_log` compliance is the only real capture.
- **Partial:** portability exists as callable surfaces (HTTP platform API, streamable MCP, Anthropic memory-tool backend, OpenAI-compatible profile-injecting proxy) but no lifecycle installer for any non-Claude-Code harness.

## 6) Truth determination during dreaming — **PARTIAL**

- **Works:** fixed, fully receipted gate order — every extracted candidate gets `CANDIDATE_EXTRACTED` *before* gating, then salience → Motive type → untrusted-directive → generated-output-authority (generated output can never gain normative authority) → raw-secret rejection — all bound to the attested contract digest.
- **Works:** authority-ranked supersession with bi-temporal truth (`valid_from/valid_to`), transitive chains with cycle guard; WS-10 coherence fires after every formation mutation, detects escalation windup + cross-artifact stance contradictions, and acts by **hold/flag, never auto-supersede**, releasing holds only after a judged positive outcome under a repaired governor.
- **GAP (verified):** no evidence weighing — once authority ties, one new observation unconditionally supersedes a fact reinforced 50×; `observed_count` is never consulted at supersession.
- **GAP (verified):** no probabilistic truth maintenance — confidence is a monotone `max()`, never decremented, decayed, or combined; the Beta machinery that exists governs retention utility only, and retrieval never reads utility.
- **GAP:** human adjudication is write-only — `requires_operator_review` and disambiguation requests are stamped but no API resolves them; `CoherenceIncidentStatus.RESOLVED` is never set. Multi-active memory types get no contradiction handling at all (supersession is guarded by `SINGLE_ACTIVE`).

---

# Value-prop assessment

**(a) Better coherence/alignment from solid adaptive memories — mechanism YES, proof PARTIAL.** Single-active truth slots, authority supremacy of corrections, time-travel `as_of` coherence, and WS-10 holds are real and structurally asserted end-to-end in the simulations. But the adaptive half of "adaptive memories" is open-loop in practice: outcomes require an external named judge nobody wires up, `outcome_quality` sits at its 0.5 default, retrieval ignores utility entirely, and the public benchmark harness (LoCoMo/LongMemEval judges, executor/judge separation) has **no committed datasets and no scores** since the benchmarks were dropped in a75f144.

**(b) Fewer tool calls / no repeated re-discovery — plausible, entirely unmeasured. Weakest prop.** The chain exists (session-start injection front-loads procedures; six-stage search; `memory_publish` shares them project-wide), but nothing anywhere counts tool calls, repeat searches, or searches avoided — the only artifact is a string-containment check that an evolved plan contains a corrected requirement. Compaction survival — the exact moment "how to do things" gets lost — currently depends solely on the SessionStart re-fire, because the PreCompact/PostCompact capture paths preserve zero real content.

**(c) Fewer tokens via compressed accurate memory — enforcement YES, savings GAP, accuracy PARTIAL.** Budgets are hard-enforced (`tokens_used ≤ budget`, identity floor, REF-line overflow), dedup and demotion demonstrably shrink the visible set (271 episodes → ≤30 visible in the fleet sim). But no code ever measures tokens saved versus an uncompressed baseline, the 4-chars/token estimator is uncalibrated, and the compressed representation an LLM actually reads — the THEME label — is a token bag, not accurate prose, so today's compression trades noise for fidelity.

**Highest-leverage fixes, in order:** (1) fix the PreCompact/PostCompact real-harness path, (2) auto-record use/outcomes so the usefulness loop actually feeds retention and rerank, (3) plug an LLM into the existing theme-synthesis seam, (4) object-level promotion with lineage, (5) predicate canonicalization so paraphrased corrections supersede, (6) token-savings and repeat-search telemetry to make props (b)/(c) measurable, (7) motive-orphan fallback in retention, (8) a per-fact pin.

Summary: all six capabilities are partial — the deterministic substrate (gates, receipts, supersession, budgets, scoping) is genuinely strong, but the intelligent/adaptive layer is consistently stubbed: token-bag themes, an unfed outcome loop, republication instead of promotion, dead compaction capture, and no measurement behind any of the three value propositions.

---

## Closure ledger (2026-08-06)

Everything above this line is the frozen 2026-08-05 audit snapshot and has not been edited. This appendix is the delta: every gap the audit verified, mapped to the commit that closed it on `audit/close-gaps` (closure commits `13fd27f` through `ca2247f` inclusive; task ledger `.tasks/audit_gap_closure.md`). The three measurement gaps are being implemented now (WS-22, uncommitted). Two corrections to the audit record itself are noted at the end.

| § | Gap (as verified above) | Closed by | Mechanism |
|---|---|---|---|
| §1 | No per-fact pin; floor is per-type and directive un-floored | 6e125a5 | `pin_memory`/`unpin_memory` (receipted `MEMORY_PINNED`/`MEMORY_UNPINNED`): guaranteed profile lead + reserved search slots + prune-protected retention gate; `ProfilePolicy.floor_types`/`floor_share` makes the floor policy, with directive floored by default |
| §1 | Correction fires only on exact `truth_key`; paraphrased predicate splits truth | bfa1747 + f1116c8 | Truth keys built on the per-scope canonical predicate (registry → operator synonyms → same-space embedding bridge, receipted, backfillable via `canonicalize_scope_predicates`); the entity-surface half closes via the alias registry resolving names before node upsert and truth-key computation (`resolve_scope_entities` backfill) |
| §1 | "Experience" half unpopulated — nothing auto-records `CITED_OR_USED`/outcomes, `use_need` collapses to 0, default-config facts immortal | 48d2609 + 6e125a5 | PreCompact/SessionEnd hooks auto-record idempotent `CITED_OR_USED` from the transcript; the SessionEnd judge records named-evaluator outcomes; `PruningPolicy.stale_after_seconds` (default `state`@30d) prunes never-used rows into restorable ghosts, ending default immortality |
| §2 | THEME text is a token bag; `model_identifier` never consumed | 48d2609 | Theme text LLM-synthesized via the new `SynthesisTransport` when `model_identifier` + transport are configured, behind the deterministic entailment gate; rejections receipted (`CONSOLIDATION_THEME_SYNTHESIS_REJECTED`) and fall to the label; model id + prompt digest ride the receipt |
| §2 | No token-savings measurement | in flight — WS-22 | T28 adds `tokens_baseline`/`tokens_rendered`/`tokens_saved_*` to the evolution proof (uses the existing estimator; calibrating the 4-chars/token estimator itself is not in T28's scope) |
| §2 | Dedup bucket-local; vocabulary-sensitive trigram embedding lets paraphrases coexist | bfa1747 + f1116c8 | Consolidation-time cross-prefix duplicate sweep demotes the weaker row (receipted, reversible on survivor retirement); production `OpenAICompatibleEmbeddingTransport` with space-stamped vectors and read-side space guards; identifier-token guard refuses the measured 0.895-cosine false merge |
| §3 | Motives cannot touch the extraction system prompt | 6e125a5 | `Motive.system_prompt_override` reaches both LLM transports as the system message; frozen into the `FormationContract` digest and `motive_version_digest` |
| §3 | No per-memory motive version pinning; orphaned Motive crashes pruning | 13fd27f | Every formed row stamps `motive_version_digest` (per-episode resolved Motive); `memory_receipts.relationship_uuid` indexed so the join seeks; orphan-motive retention falls back to `config.pruning.retention` with the orphan receipted in `policy_source` |
| §4 | No object-level promotion / vote-up | ca2247f | `memory_promote(relationship_uuid)` copies one exact own-scope fact verbatim into an endorsement-gated project candidate (zero-LLM deterministic extraction); `promotion_endorsements` ledger, one vote per agent, `min_endorsements` gates formation consumption; `memory_endorse_promotion` + enriched `project_memory_candidates` |
| §4 | No lineage from a promoted project fact to its source | ca2247f | Candidate carries `source_relationship_uuid`/`source_scope_key`/source receipt digest; materialization stamps first-class `promoted_from_relationship_uuid`/`promoted_from_scope_key`; `memory_evidence`/`memory_explain` resolve the chain as a `promotion_lineage` block |
| §4 | ACL facade-only; core accepts arbitrary scope keys; no per-memory visibility | ca2247f | `Memotron(authorized_scope_keys=)` fails fast on every public scope-taking entry (resolved row scopes guarded; `scope=None` cross-store listings and `export_graph` fail closed; default `None` byte-identical); per-row `visibility_agents` allowlist enforced fail-closed via `reader_agent_id` across search/profile/evidence/utility, open for operators |
| §4 | *Caveat* (not a GAP): project scope ranks lowest in the search rerank | unchanged — by design | `scope_priority` follows the caller-supplied scope order; "project first" holds for profile ordering. No closure commit targeted it |
| §5 | PostCompact checkpoint path dead in the real harness | 8835571 | Summary precedence: explicit `compact_summary` field > transcript `isCompactSummary` entry > bare boundary record; the hook never exits nonzero and the post-boundary refresh always runs (see correction 2 below) |
| §5 | PreCompact captures nothing from the dying context | 8835571 | Deterministic transcript JSONL parsing (`src/memotron/transcripts.py`): task focus, recent assistant state, tools, files touched — bounded, redacted, no model call |
| §5 | *Partial* (not a GAP): no lifecycle installer for non-Claude-Code harnesses | open — out of closure scope | The callable surfaces (platform HTTP API, streamable MCP, Anthropic memory-tool backend, Memory Router) remain the committed integration path; no installer task existed in the closure plan |
| §6 | No evidence weighing — one observation flips a 50×-reinforced fact | 33336e5 | `SupersessionPolicy.corroboration_margin`/`corroboration_required`: equal-authority challengers park (`insufficient_corroboration`) until enough distinct observations corroborate, then flip with parked siblings resolved `corroborated`; higher authority still flips instantly |
| §6 | No probabilistic truth maintenance — monotone `max()` confidence; retrieval never reads utility | 33336e5 + 48d2609 | Bounded evidence accumulation + per-park contradiction discount (`disputed_count`, receipted) + optional read-only half-life decay at the pruning gate; `RetrievalPolicy.utility_weight` puts `use_need` into the stage-6 rerank (`RetrievalContract` schema_version 2) |
| §6 | Adjudication write-only; `RESOLVED` never set; multi-active contradictions unhandled | 82494b5 + 33336e5 | `pending_supersession_reviews`/`resolve_supersession_review` (approve flips truth through the normal machinery with lineage, receipted; client + admin `/api/adjudication/*` + operator MCP); coherence incidents gain the OPEN→ACKNOWLEDGED→RESOLVED lifecycle and disambiguation resolution; multi-active polarity conflicts route through the same gate/supersede machinery scoped to the conflicting row |

Value-prop assessment, same treatment:

| Prop | Claimed shortfall | Closed by | Mechanism |
|---|---|---|---|
| (a) | Adaptive loop open: judge nobody wires up, `outcome_quality` stuck at 0.5, retrieval ignores utility | 48d2609 | The session judge is the wired named evaluator at SessionEnd; hook citations move `use_stability`; `utility_weight × use_need` enters the rerank |
| (a) | Benchmark harness has no committed datasets and no scores | in flight — WS-22 | T30 commits a hermetic repo-owned corpus plus a runnable `public_benchmark` invocation persisting a scored report under `benchmarks/` |
| (b) | Nothing counts tool calls, repeat searches, or searches avoided | in flight — WS-22 | T29 derives `repeat_search_rate` / `answered_from_profile_rate` from use-event query-digest lineage on the evolution proof |
| (b) | Compaction survival depends solely on the SessionStart re-fire | 8835571 | Real PreCompact/PostCompact/SessionEnd capture from documented hook inputs; re-injection deliberately stays the documented `source=compact` re-fire |
| (c) | No tokens-saved measurement vs an uncompressed baseline | in flight — WS-22 | T28 (see §2 row) |
| (c) | The compressed representation the LLM reads is a token bag | 48d2609 | Entailment-gated LLM theme summaries (see §2 row) |

The audit's own "highest-leverage fixes, in order" all landed or are the in-flight remainder: (1) 8835571, (2) 48d2609, (3) 48d2609, (4) ca2247f, (5) bfa1747, (6) in flight — WS-22, (7) 13fd27f, (8) 6e125a5.

### Corrections to the audit record

Two items were refuted or corrected during closure. The snapshot above is left as written; the record is corrected here:

1. **Hook exec-form schema — REFUTED.** The closure plan carried a hook-schema concern (batch-0 T2): whether the generated hook definition's exec-form shape (`args` array, `statusMessage`) was valid. Checked against the official Claude Code hook documentation: the shape is valid and no change was needed. Recorded in 13fd27f ("hook definition exec-form shape verified valid against current Claude Code docs").
2. **§5 PostCompact framing — CORRECTED.** `PostCompact` **is** a documented Claude Code hook event; what is not documented is the `compact_summary` stdin field the original handler required (nor `additionalContext` support for PostCompact). The dead-path defect itself was real and is closed as above: 8835571 derives the summary from the transcript's `isCompactSummary` entry, honors an explicit `compact_summary` field first, and never exits nonzero.

Each closing commit's message records its full green test suite at the time it landed (most recently 518 passed / 1 skipped at ca2247f). WS-22 (T28–T30) is in implementation now and uncommitted; the three in-flight rows above convert to commit citations when it lands.
