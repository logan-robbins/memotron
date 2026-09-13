> **Complete run.** Supersedes the earlier truncated pass. A `.slice(0, 10)` in the
> orchestration silently capped verification at ten findings, all from `replay` and `coherence`;
> the cap was removed and the run resumed. **32 candidates -> 27 survived, 3 killed**, covering
> all six corpora. See D-71 for the harness bug and D-73 for this run.

# Memotron takeover audit — surviving findings

Repo root: `/Users/ryan.van.valkenburg.-nd/repos/jedai/worktrees/memotron-implementation` (branch `takeover`). Paths below are repo-relative to that root. Read-only audit; no file modified, no git write command run.

27 findings survived an adversarial refutation pass; 3 were killed. Every claim below is labelled **OBSERVED** (executed or read in this audit) or **DERIVED** (reasoned, steps shown). Where the refuter narrowed a claim, the narrowed version is what is written here — the original, broader wording is not preserved anywhere in this register.

Severity is stated as `filed → adjusted` wherever the refutation moved it.

---

## 1. Findings, ranked

### A-01 · tier0 · certification
**`StatefulPolicyComparison.protected_invariants` is the candidate arm's data; a baseline whose own replay violated a protected invariant stages `certification_passed=True`**
`src/memotron/certification.py:1927-1931`, consumed at `src/memotron/client.py:2020-2024`

- **Label:** OBSERVED (reproduced end to end with the repo's own canonical fixture pair, `tests/test_stateful_certification.py:70-72`).
- **Mechanism:** `invariants = tuple(candidate.protected_invariants)` (1931) is the only invariant set that reaches the report. For `role="baseline"`, `client.py:2021` evaluates that candidate-derived field, so the baseline's own failing invariants (`baseline_samples[*].protected_invariants`, computed per sample with each sample's own Motive at `certification.py:1813-1815`) are computed and discarded. Secondary defect on the same line: repetitions 1..N-1 of **both** arms are never gated.
- **Precondition:** `role="baseline"` staging via `client.stage_policy_contract` — the documented one-time migration path (`storage/sqlite.py:2777-2789`). The candidate arm must be invariant-clean (the normal case).
- **Impact:** Observed: baseline failed `retrieval_allocation` in both repetitions, top-level `protected_invariants` all-passed, `certification_passed=True`, alias `b1e4623070f6ccd1` installed and accepted by the runtime gate at `client.py:1085` and the storage gate at `sqlite.py:2788`. `tests/test_stateful_certification.py:55,59` passes while the baseline is failing.
- **Bounding (be ready for this pushback):** alias install is only permitted when no alias yet exists for the scope (`sqlite.py:2789-2792`), and at runtime the contract's Motive substitutes for the same-named motive rather than switching policy wholesale (`client.py:1096-1100`). A defender can also argue the observed `retrieval_allocation` failure is an artifact of replaying the baseline motive against the candidate's fixture corpus. Neither rescues the code: the gate documented at `client.py:1994-1997` reads a different arm's data, and the field is named/typed generically with nothing marking it candidate-only (`certification.py:171`).
- **Fix:** gate on the role's own samples, across all repetitions; rename/split the field so a cross-role read is a type error.

---

### A-02 · tier1 · coherence
**MCP `remediate_coherence` reads `sr.held_count` / `sr.retired_count`, which `ScopeRemediationResult` does not define — the tool only works when there is nothing to remediate**
`src/memotron/mcp_server.py:1124-1134`; model at `src/memotron/models.py:1729-1744`

- **Label:** OBSERVED (repro on SQLite: `incident_count 1` → `AttributeError: 'ScopeRemediationResult' object has no attribute 'held_count'`).
- **Mechanism:** the serializer reads two attributes that do not exist (the model defines `held_relationship_uuids` / `retired_relationship_uuids` and the properties `windup_count` / `contradiction_count`). `grep -rn "held_count|retired_count" src/ tests/ ui/` returns only the two `mcp_server.py` lines and no definition. The comprehension is guarded on `if sr.report.incident_count > 0`, so a clean fleet never evaluates it.
- **Precondition:** the call reaches the MCP tool surface (`mcp_server.py:1098`) **and** at least one swept scope has `incident_count > 0`. Not reachable through the Python SDK client, which is all the test suite covers (`tests/test_coherence.py:379-474`, 20 passing).
- **Impact:** the tool raises a FastMCP `ToolError` — including in `dry_run=True`, so the documented "preview first" workflow is broken too. In apply mode the mutations commit first (holds written; with `retire_held_directives=True`, directives soft-retired) and only then does the serializer raise, so the operator loses the tool response. The audit trail survives: incidents are recorded and retrievable via `coherence_incidents()` / `list_coherence_incidents`, and `coherence_hold` persists on the row — recovery needs a follow-up query, not reconstruction.
- **Fix:** one-line serializer fix, plus an MCP-surface test that exercises a non-empty sweep.

---

### A-03 · tier2 → **tier1** · migration
**Any tenant migration fails verification when the tenant's agents hold episodes and no ACTIVE relationships exist — the normal post-ingestion, pre-formation state**
`src/memotron/migration.py:874` (guard) vs `:952` / `:705-712` (unconditional verify)

- **Label:** OBSERVED (refuter ran `migrate_tenant_memory(store, source_tenant_id="old-tenant", dest_tenant_id="new-tenant", purge_source=False)` — same-store, no `dest_store` — and got `migrated 0 agent-scoped episodes but the preview counted 1`).
- **Mechanism:** `_migrate_agent_scopes` (the only caller of `_migrate_episodes` for agent scopes, `migration.py:741-747`) sits inside `if preview.active_relationship_count + preview.agent_scoped_relationship_count > 0:` (874). Verification is outside the guard and checks `agent_scoped_episodes` against a preview that counts them independently of relationships (`migration.py:227-229`). The `same_store` live-recount branch (900-913) is inside the same guard — **the original "cross-store only" framing was wrong.**
- **Precondition:** any migration (same-store or cross-store) where total ACTIVE relationship count across the tenant scope and all its agent scopes is zero, and at least one registered agent holds episodes. `--keep-source` does not avoid it.
- **Impact:** `RuntimeError` every time — the migration is unperformable, not merely gated — after the destination has already received tenant episodes, agent registrations, motive assignments, LLM credentials, prompt override and version rows (see A-05 for what a retry then does). `cli.py:166` catches only `OSError`/`ValueError`, so the operator gets a raw traceback.
- **Fix:** move the agent-scope leg out of the relationship-count guard, or make verification conditional on the same guard.

---

### A-04 · tier1 · migration
**`migrate_tenant_memory` silently replaces the destination tenant's LLM credentials with the source's; nothing in the preview or result discloses it**
`src/memotron/migration.py:539-555`; upsert at `src/memotron/storage/sqlite.py:5118-5126`

- **Label:** OBSERVED (source read + full-upsert semantics + init-flow ordering all read directly).
- **Mechanism:** `_migrate_llm_credentials` reads the source row and calls `set_tenant_llm_credentials` with no check that the destination already has one; the SQL is `ON CONFLICT(tenant_id) DO UPDATE SET provider=…, api_key_sealed=…, base_url=…, model=…`.
- **Precondition (the durable case only):** the destination is in the default `environment` provider mode. `seed_tenant_llm_credentials_from_env` returns early when a credential row already exists (`runtime.py:398-400`), so the destination silently runs extraction on the **source project's** sealed API key, base_url and model indefinitely.
- **Not durable, and should not be filed as such:** in the explicit-provider path the overwrite is transient — `configure_project_llm` also persists the choice to `.memotron.yaml` (`adoption.py:396-399`) and `_ensure_project_llm_credentials` (`adoption.py:472-513`) rewrites the sealed row on next session start, or fails loudly.
- **Latent, SDK-only sub-limb (do not lead with it):** the embedding tri-state contract (`sqlite.py:5068-5072`, `""` = clear) is violated because `tenant_llm_credentials()` returns `""` for an unconfigured source and migration passes it through — but **no shipped surface can populate a destination embedding endpoint** (verified: every constructor omits `embedding_*`), so "silently re-spaces every vector" is not operator-reachable today.
- **Lesser limb:** `_migrate_prompt_versions` (`migration.py:587-595` → `sqlite.py:4453-4471`) deactivates the destination's active prompt and activates the source's tip. Active-tip switch, not data loss.
- **Fix:** refuse or explicitly report a destination that already holds credentials; pass `embedding_*=None` to preserve.

---

### A-05 · tier1 · migration
**No atomicity and no idempotency: any abort after the copy leaves the destination written, and the documented "re-run the migration" instruction appends a second full copy**
`src/memotron/migration.py:952-958` (verify) vs `:296-298`, `sqlite.py:372-390` (inserts)

- **Label:** OBSERVED (refuter simulated a post-copy failure plus the instructed retry: 4 ACTIVE destination rows for 2 facts, `purged_source=True`, source gone).
- **Mechanism:** episodes (860), relationships/nodes/receipts (889), agents (933), motive assignments (936), credentials (939), prompt override (942), prompt versions (945) and config versions (948) each commit individually; `_verify_migration_counts` (952) and `_verify_source_unchanged` (978) raise afterwards with no compensating delete. Retry duplicates rather than converges: fresh episode uuid every run (`uuid4().hex`, 296-298), `add_relationship` is an unconditional INSERT with a fresh uuid and no truth-key lookup, prompt versions re-append and re-activate. Only `upsert_node` is idempotent.
- **Precondition:** any failure or abort after the first destination write — including the deliberate abort path at 672-679 that tells the operator to re-run.
- **Impact:** duplicate ACTIVE rows feed retrieval, salience and pruning as independent evidence. **Reconciliation is partial, not absent:** `_repair_single_active_conflicts` (`dreaming.py:10328-10396`) supersedes duplicates grouped by `truth_key` for `SINGLE_ACTIVE` rows, given a pruning run, unexhausted `max_repairs` and an approved decision. `MULTI_ACTIVE` duplicates — migration's own fallback default (`migration.py:361-365`) — persist permanently. **Forensic trace exists but nothing consults it:** a partial run leaves `TENANT_MIGRATION_RELATIONSHIP_MATERIALIZED` receipts with no matching `run_checkpoint` (checkpoint only on success, 928-931) — absent entirely if the failure precedes `begin_run` (875).
- **Fix:** a destination-side migration marker checked on entry, plus truth-key-aware upsert or a spanning transaction for the same-store case.

---

### A-06 · tier1 · migration · **DERIVED**
**Migration copies rows but not the use/outcome/prune-ghost/quarantine evidence plane, so migrated `state` facts land stale-prunable with a silent utility reset**
`src/memotron/migration.py:637-639` (the only mention of these tables — the watermark), `:413-422` (properties copied verbatim)

- **Label:** DERIVED. Steps: (1) `grep -n "use_event|outcome_event|prune_ghost|quarantined" src/memotron/migration.py` returns only the three watermark lines — nothing copies them; (2) `add_relationship` mints a new uuid (`sqlite.py:350-390`) and use events key on `(scope_key, relationship_uuid)`, so both halves of the key change; (3) `last_seen_at` is carried over unchanged (414-422 rewrites only scope/truth fields) and is advanced only by re-observation (`dreaming.py:9281`), never by a use event; (4) staleness is gated on use events in a window measured from `last_seen_at` (`dreaming.py:10469-10478`, 10504-10508); (5) default config opts `state` in at 30 days (`config.py:2205-2207`).
- **Precondition:** default `PruningPolicy.stale_after_seconds`; a migrated ACTIVE `state` row with `last_seen_at` older than 30 days kept alive in the source by use events; a pruning dream cycle actually running in the destination against that row with no pin/correction/coherence-hold. With a configured dream-agent transport the prune is proposed, not guaranteed. **`purge_source=True` is NOT a precondition** — `--keep-source` produces an equally evidence-less destination; it only preserves a source-side fallback copy.
- **Impact:** a fact stated 60 days ago and retrieved daily is not stale in the source but is classified `stale_unused` in the destination on the first pruning run — archived to a restorable, receipted `PruneGhost` with a recorded decision (`dreaming.py:9520-9550`). State it as *"silently archived out of active memory days after a successful migration"*, not *"lost"*. **The quieter half is broader:** with outcome events gone, `utility_projection` returns empty for every migrated row, so retention ranking and the retrieval `use_need` term fall back to defaults (`use_stability` 0.0, `outcome_quality` 0.5) — for **all** memory types, avoidable by no flag. Same-file migrations confine this to the project scope (agent scopes keep uuids and use events, `migration.py:970-973`); cross-file loses agent evidence too.

---

### A-07 · tier1 · migration
**`purge_tenant_state` truncates two global tables — a per-tenant surface with database-wide blast radius**
`src/memotron/storage/sqlite.py:5337-5338`; Postgres twin `src/memotron/storage/postgres/_governance.py:1506-1509`

- **Label:** OBSERVED (source read; every other delete in the same transaction is scope- or tenant-scoped — `sqlite.py:5302-5317`, `:5339-5358`).
- **Mechanism:** `DELETE FROM dream_job_runs` and `DELETE FROM job_state` carry no predicate; neither table has a tenant or scope column (`sqlite.py:3077-3092`).
- **Precondition:** more than one tenant in the same graph file — the documented normal case (`cli.py:317-318`, default shared `~/.memotron/memory.sqlite`, `adoption.py:47`). Fires from `migrate` (`migration.py:988`) and from the admin "Purge generated state" action.
- **Impact:** every other tenant in the file loses its dream-job run history (`client.dream_history`, `client.py:1273`). **File this as a documented design choice with an undocumented per-tenant-surface blast radius**, not a missing `WHERE`: the Postgres plane states the intent ("Maintenance bookkeeping is global, not per tenant", `_governance.py:1504-1505`), while `base.py:1185` contracts the method as per-tenant. **Drop two overclaims:** jobs do not "all fire at once" (`job_state` has one row per job_name and all clients use the same three names, so the cursor is already shared — cost is at most one early cycle), and the receipt audit trail is preserved (`memory_receipts` / `run_checkpoints` are scope-keyed, `preserve_receipts=True` on the migrate path).

---

### A-08 · tier1 · coherence
**`remediate_coherence` reports holds it did not apply: the report's predicate omits the attribution-confidence gate the writer enforces**
`src/memotron/client.py:3368-3376` vs `src/memotron/dreaming.py:1823-1837`

- **Label:** OBSERVED (reproduced by both readers; **use the refuter's subjects, not the original filing's**).
- **Mechanism:** the writer has one extra conjunct — `incident.attribution_confidence >= effective_policy.min_attribution_confidence` — that the report's counting predicate lacks. With defaults (`identity_threshold=0.50`, `min_attribution_confidence=0.70`, `config.py:2070-2073`) and confidence `= 0.65·cosine + 0.2333` for a governing SKILL (`coherence.py:436-439`), every incident with cosine in ≈[0.500, 0.718) is detected, counted as held, and deliberately not held.
- **Working repro subjects on this checkout** (against `tests/test_coherence.py` `stale_self_authored_skill`, `cycles=4`): `"evaluate agents"` (cos 0.609 / conf 0.629), `"cost efficiency"` (0.609 / 0.629), `"cost of agents efficiency review"` (0.654 / 0.659). *The original filing's subjects measured above threshold and must not be cited.*
- **Precondition:** a windup incident below `min_attribution_confidence` — cosine below ≈0.718 for a governing SKILL, ≈0.808 for a governing FILE. No unusual configuration.
- **Impact:** confined to the sweep report's counters — `ScopeRemediationResult.held_relationship_uuids` (`models.py:1734`), `CoherenceRemediationReport.total_held` (`models.py:1779-1780`), and the MCP tool's `total_held` (`mcp_server.py:1124`) — which overcount by exactly the number of sub-threshold incidents, in both dry-run and apply mode. The actuator itself is correct: deferral is deliberate and `dreaming.py:1786-1820` records `attribution_action='defer_pending_disambiguating_evidence'` plus a disambiguation request, visible per-incident via `coherence_incidents()`. No data loss (retirement reads holds from the graph). **Aggravator to add:** because no hold is written, the idempotency skip at `coherence.py:328-330` never fires, so every subsequent sweep re-raises the incident and re-reports the same phantom hold indefinitely.

---

### A-09 · tier1 · coherence
**`CROSS_ARTIFACT_CONTRADICTION` has no idempotency: every scan mints a fresh `incident_id` for the same unchanged pair**
`src/memotron/coherence.py:350-388` (no guard) vs `:328-330` (the windup guard that exists)

- **Label:** OBSERVED for the mechanism and the measured costs; the production-embedding extrapolation is DERIVED.
- **Mechanism:** the only skip in `_detect_contradictions` (line 375) tests `coherence_hold`, which `dreaming.py:1823-1837` writes **only** for `ESCALATION_WINDUP`. `CoherenceIncident.incident_id` defaults to `uuid4` (`models.py:1508-1512`).
- **Precondition:** a contradiction persisting across scans plus repeated scanning — a scheduled COHERENCE DreamJob (`dreaming.py:1844-1853` runs `dry_run=False` every cadence tick) or repeated `remediate_coherence`. A single manual scan does not show it.
- **Impact, three compounding effects:**
  1. **Undrainable queue (OBSERVED).** 4 scans of one unchanged pair → 4 distinct ids; after `resolve_coherence_incident` on the newest, a rescan mints a fifth; 4 of 5 open.
  2. **Window-bounded starvation (OBSERVED, narrowed).** `coherence_incidents()` fetches `max(limit*5, 200)` newest and filters scope *after* the fetch (`client.py:2939, 2964`). Scope A re-scanned 260× hid scope B's single genuine incident at the default `limit=20`; it was still returned at `limit=500`. Correct claim: *hidden from any caller using the default window*, not removed from the log. This falsifies the assumption written at `client.py:2936-2939`.
  3. **Storage (OBSERVED at 256-dim; DERIVED beyond).** ~13.5–16.9 KB per repeated identical scan, insert-only with no trim (`sqlite.py:1446-1462`); the payload embeds both `Directive.embedding` vectors. The 1536-dim / "~4.9 MB/day" projection is unverified.

---

### A-10 · tier1 · coherence
**An artifact with no `updated_at` is treated as unconditionally staler than every memory directive, manufacturing windup incidents that hold and then retire correct memories**
`src/memotron/coherence.py:338-342`

- **Label:** OBSERVED (identical fixture, only `updated_at` varied — A: tomorrow → windup 0, hold None, retired 0; B: `None` → windup 1, hold True, retired 1, with `attribution_confidence` 0.762, above the 0.70 gate).
- **Mechanism:** `stale = other.updated_at is None or (…)`. `PersistentArtifact.updated_at` defaults to `None` (`models.py:1360`) despite its docstring calling it "the staleness signal used for culprit attribution".
- **Precondition:** the artifact reaches the scanner via `Memotron.register_artifact_source` (`client.py:2514`) — the documented path for a live skill registry or file directory — whose source does not populate `updated_at`. That path performs no validation. **The durable path does guard:** `sqlite.py:2264-2265` raises, and the MCP `register_artifact` tool requires `updated_at_iso` (`mcp_server.py:957`). The trigger is a *missing timestamp*, not a recent artifact — a genuinely fresh artifact carrying an mtime raises nothing (case A).
- **Impact:** an ESCALATION_WINDUP incident indistinguishable from a genuine one, with rationale asserting "predating the latest escalation" while rendering the date as "unknown date" (`coherence.py:402, 408`). The `coherence_hold` is written on the ordinary `run_coherence_scan` path (broader than the original filing, which said `remediate_coherence`); soft-retirement additionally needs opt-in `retire_held_directives=True`. The guarantee is enforced on one registration path and silently absent on the other.

---

### A-11 · tier1 · coherence
**A fleet sweep is non-atomic with no partial-result path: one raising `ArtifactSource` aborts after earlier scopes were already held and retired**
`src/memotron/client.py:3353-3409`; projection raise at `src/memotron/coherence.py:106-110` → `src/memotron/storage/sqlite.py:2887-2890`

- **Label:** OBSERVED (two scopes, source raises for the second: sweep raised, `agent:aaa-first` left `coherence_hold=True status='pruned'`, `agent:zzz-second` untouched, no report returned).
- **Mechanism:** the per-scope loop has no `try/except` and no partial return; the accumulated `results` list is discarded. **Not specific to `ArtifactSource`** — any exception in the per-scope body (embedding-transport failure at `coherence.py:148-149/249`, storage error) aborts identically.
- **Precondition:** a multi-scope apply-mode sweep where the failure is not on the first scope in `graph.scopes()` order.
- **Impact:** partial fleet mutation with no returned `CoherenceRemediationReport`. **Two qualifications the original overstated:** completed scopes are audited — `scan_coherence` opens and checkpoints a coherence receipt run (`dreaming.py:1733-1741, 1838-1841`) and records a dream decision per incident, so `coherence_incidents(scope=…)` still shows what was applied; and re-running after fixing the source is idempotent by design (`coherence.py:329`, `client.py:3385-3389`, covered by `tests/test_coherence.py:424`). The defect is *mid-sweep abort yields partial fleet mutation with no returned report*, not unrecoverable or unauditable mutation.

---

### A-12 · tier0 → **tier1** · certification
**`run_public_benchmark` has no accuracy gate: `passed=True` for a run that scored 0.0 on every question**
`src/memotron/certification.py:2153-2172`

- **Label:** OBSERVED (real `client.public_benchmark` over `benchmarks/memotron_kb_v1/corpus.json`, judge returning 0.0, `minimum_stability_score=1.0`: `mean_score=0.0`, `stability_score=1.0`, `failures=()`, `PASSED=True`).
- **Mechanism:** `failures` is built from exactly two conditions — lifecycle invariants and stability. `mean_score` is computed (2140) and reported (2167) but never compared; `StatefulReplayOptions` (114-121) has no minimum-score field, so a caller cannot express an accuracy floor.
- **Scope this as evidence integrity, not a control-plane bypass.** `PublicBenchmarkReport.passed` gates nothing inside the library — the `.passed` checks at `client.py:2021/2024/2163` and `replay.py:1113` are all on `StatefulPolicyComparison`. An all-zero benchmark cannot promote a policy alias.
- **Real consumers, all three:** the exit code at `examples/benchmark_report.py:167`; the committed `"passed": true` in `benchmarks/memotron_kb_v1/report.json`; the exported SDK surface (`__init__.py:430`).
- **Impact:** the machine-readable verdict and the process exit code say success while the accuracy number says total failure. `mean_score` *is* printed (`examples/benchmark_report.py:163`) and *is* in `report.json`, so an attentive reader sees 0.0 — anything consuming the boolean is misled. `tests/test_benchmark_evidence.py:136,147` asserts real floors for the one committed corpus, but enforcement is manual given the filed "no test CI" item. The unmitigated exposure is the operator-supplied public-corpus path (`README.md:906-916`), which gets no gate and cannot configure one.

---

### A-13 · tier0 → **tier1** · interop
**`MemoryRouter`'s cross-tenant guard is fail-open by construction — dead code in exactly the multi-tenant mode the docs prescribe**
`src/memotron/interop.py:903-909` (guard), `:1032-1054` (tenant id from request body), `:870` (`principal=None` default)

- **Label:** OBSERVED (two-tenant control plane, router `scope=None, control_plane=cp`, `route()` with no principal and body `{"_tenant_id": "initech", …}` → victim tenant's memory context injected into the attacker's prompt and returned).
- **Mechanism:** the only cross-tenant check is gated on `self._scope is not None`; with `scope=None` it never fires, and `_resolve_request_policy` takes tenant identity verbatim from the request dict. No downstream gate compensates: `MemoryControlPlane.resolve` (`config.py:4060-4072`) trusts `tenant_id`, and `_resolve_scope` (`config.py:4306-4318`) only checks the scope is registered to the caller-named tenant.
- **Precondition:** router built `scope=None` + `control_plane=` (the documented multi-tenant configuration, `interop.py:820-823`) **and** `route()` called without `principal`. A fixed-scope router is protected (905 fires, plus `_resolve_scope`); passing a `principal` is also safe (1000-1012).
- **Scope honestly: an API-contract and productionization defect, not a live disclosure.** `MemoryRouter` is wired to no inbound surface — every construction site is `tests/`, `examples/fleet_simulation.py:1427`, or `README.md`; `gateway.py` is an outbound client. The documented recipe at `README.md:1404-1414` does pass `principal=`.
- **What is defective:** (a) `interop.py:800-808` and `README.md:1375` promise unconditionally that the class "enforces tenant scope isolation" and that "cross-tenant requests fail fast", while the guard is inert in precisely the multi-tenant mode; (b) authorization is opt-in per call site via a `principal=None` default rather than fail-closed; (c) `tests/test_control_plane.py:241-256` asserts the unauthenticated shape *succeeds*, locking the gap in. This blocks productionizing the router as the transparent proxy it advertises, since an integrator fronting it with HTTP would naturally forward the request body. Distinct from the filed "no auth on either surface" item, which covers the MCP servers and admin API.

---

### A-14 · tier1 · certification
**Public-benchmark stability gate variances only per-repetition MEAN scores, so per-question replay churn is attenuated by 1/√N**
`src/memotron/certification.py:2139-2146`

- **Label:** OBSERVED for the mechanism and both measurements.
- **Mechanism:** `sample_means = [item.mean_score for item in samples]` — only the scalar mean of each repetition enters the variance; `PublicBenchmarkCaseScore.score` per question (2068-2078) is discarded. The sibling stateful path does the opposite (`_flip_rate`, 1823-1833, diffs per-candidate-key dispositions).
- **Lead with the general property, not the corner case.** The contrived "100% disagreement scores 1.0" requires exactly compensating flips. The real and more damaging statement: a judge flipping every answer by coin toss passes the shipped default threshold of 0.95 in **86% of trials at N=500 questions** (32% at N=100, 20% at N=50). The gate gets blinder as the suite grows. The exactly-compensating case does score 1.0 and pass at `minimum_stability_score=1.0` (observed).
- **Precondition:** a non-deterministic answer runtime or judge (`OpenAICompatibleBenchmarkRuntime` / `LongMemEvalOfficialJudge` at temperature > 0). The offline `ProfileTopEvidenceExecutor` + `LocalOfficialBenchmarkJudge` pair used by `examples/benchmark_report.py` is deterministic and unaffected.
- **Impact:** published public-benchmark evidence carries a reproducibility guarantee it does not have — `passed=True` and the failure name `benchmark_score_stability_below_threshold` assert replay agreement that was never measured. **It does not let an uncertified policy reach production:** `stage_policy_contract` / `activate_policy_alias` / `initialize_policy_alias` all consume `StatefulPolicyComparison`, whose `stability_score` comes from `_flip_rate` (1932-1935) and is unaffected; the admin UI readout (`admin_server.py:1302`) reads the same type.

---

### A-15 · tier1 · replay
**`verify_no_silent_mutation` returns `passed=True` for a scope holding live rows with no mutating receipt at all — the exact case it exists to detect**
`src/memotron/replay.py:1072-1087`

- **Label:** OBSERVED (fresh ledger, no receipts, graph stub with a non-empty state hash: `passed=True`, `live=08c8fc9e14f4`, `latest_receipted=None`). Reproduced on SQLite; the defect is in `replay.py` and therefore backend-independent.
- **Mechanism:** the live-vs-receipted comparison at 1073 is guarded on the receipted side (`if latest_hash is not None and live_hash != latest_hash`). `latest_hash` is set only inside `for receipt in mutating:` (1049-1063), so zero mutating receipts skips the comparison; `errors` stays empty. Neither caller compensates — `client.verify_no_silent_mutations` (`client.py:2391-2398`) delegates after an auth check, `client.certification_bundle` (2415-2422) consumes `passed`. A constant zero-row digest was available as a baseline (`storage/sqlite.py:534-537`) and is unused.
- **Precondition (restated — the filing's original three were too weak):** any scope holding live context-visible relationship rows with no mutating receipt, reachable via a writer that bypasses the receipt choke point. **Including a first-party one:** `client.build_session_digest` (`client.py:1374` → `epochs.py:1104`) writes an ACTIVE ROLLUP relationship through `storage.add_relationship` with no receipt emission. Also via a split/restored deployment where graph and receipt stores diverge (cf. T0-5), or an upgrade from a pre-receipt version. **Do not cite** the in-repo migration path (`migrate_tenant_memory` emits per-row brackets; `tests/test_migration.py:664-700` asserts `mutating_receipt_count == 4`) or receipt+checkpoint deletion (`purge_tenant_state`, `sqlite.py:5211-5335` and `postgres/_governance.py:1498`, deletes receipts, checkpoints, relationships and nodes over the same scope-key set in one transaction, leaving an empty scope where `passed=True` is correct; `grep -rn "DELETE FROM memory_receipts" src/` has no other hit). Partial receipt deletion is caught by `verify_chain` (`storage/receipts.py:1350-1362`).
- **Impact:** a fail-open verdict. The scope-level "every live state change is receipted" invariant affirms itself precisely when there is no evidence for it, and that verdict is exported in the `GovernanceCertificationBundle`. The report body carries `mutating_receipt_count=0` and `latest_receipted_state_hash=None`, so a field-inspecting consumer can tell — the machine-readable `passed` cannot.

---

### A-16 · tier1 · replay
**The `run_checkpoints` row carries no self-verifying commitment beyond four fields; `graph_state_hash_before` is never compared to anything and the BOM exports it with `chain_valid=True`**
`src/memotron/replay.py:516-528` (the only anchor), `:1010-1011` (BOM export), `:535` (`ReplayProof.checkpoint_state_hash`)

- **Label:** OBSERVED (raw SQL UPDATE on `run_checkpoints`, no receipt row touched: `verify_chain valid=True`, `byte_replay SUCCEEDED`, `checkpoint_state_hash` reported as `deadbeef`, `replay_status='verified'`, BOM `chain_valid=True before=cafebabe after=deadbeef`. Case E2 confirms the `_before` tamper survives on a **single-scope** run unconditionally.)
- **Mechanism:** `verify_chain` cross-checks only `receipt_count`, `first_receipt_hash`, `last_receipt_hash`, `merkle_root` (`storage/receipts.py:1350-1366`). Of the uncommitted remainder, `graph_state_hash_before` is compared in no code path (its only consumer is the export), and `graph_state_hash_after` only when `len(running) == 1`.
- **Frame it as the broader gap:** `replay_status`, `effective_policy_digest`, `motive_version_digest` and `scope_key` on the same row are equally uncommitted and equally exported (`replay.py:995-997, 1013`); `replay_status` can be forged directly without invoking `byte_replay`.
- **Precondition:** direct DB write access to `run_checkpoints` — same threat model as the filed T0-7. No API path writes these columns. **Drop** the claim that a caller bug can produce the multi-scope non-None case.
- **Impact:** a governance/BOM consumer reads chain-unbacked values as chain-backed. **Do not claim `byte_replay` "degrades to a restatement of `verify_chain`"** — it retains the per-scope state-hash continuity fold (472-493), and the operator-facing path at `client.py:1811` anchors every scope to the live store.
- **Fix:** compare `checkpoint.graph_state_hash_before` to the receipt-derived seed at `replay.py:478`, require `graph_state_hash_after is None` when `len(running) > 1`; better, fold the checkpoint row into the Merkle commitment.

---

### A-17 · tier1 · replay
**`counterfactual_evaluation(record_receipts=True)` writes `"materialized"` receipts into the ORIGIN scope, permanently flipping that scope's no-silent-mutation report to `passed=False`**
`src/memotron/replay.py:763-790`

- **Label:** OBSERVED (baseline `passed=True` → after one recorded run, `passed=False` with 5 missing-artifact errors; counterfactual checkpoint stamped `replay_status='mismatch'`).
- **Mechanism:** `_record_counterfactual_run` emits with `scope_key=origin.scope_key` and `decision_result="materialized" if accepted else "gated"` (779), supplying no `graph_state_hash_before/after` (defaulted to `None`, `storage/receipts.py:1005`) and no `relationship_uuid`. `"materialized"` is in `_MUTATING_RESULTS` (42-44), and no scope-level audit path filters on `run_kind`, so hypothetical decisions read back as real mutations. `verify_no_silent_mutation` then byte-replays every run in the scope (1064-1070), `_collect_missing_artifacts` rejects the counterfactual run, and `_fail` durably stamps `set_replay_status(run_uuid, 'mismatch')` on a chain-valid run.
- **Preconditions (both required):** (1) a caller passes `record_receipts=True`, and (2) **at least one candidate is ACCEPTED by the alternate policy** — rejections emit `"gated"`, which is not in `_MUTATING_RESULTS`, so an all-reject counterfactual writes receipts but breaks nothing.
- **Reachability is SDK-only.** `grep -rn record_receipts` returns exactly two non-definition hits: `tests/test_replay.py:390` and `replay.py:726`. No product path, MCP tool, admin route or client method passes it — `client.py:1858-1859` calls with four kwargs and omits it. Latent defect in documented public SDK surface (`__init__.py:301`, `__all__:614`), not something an operator hits today.
- **Precision corrections:** the ORIGIN run's `replay_status` is **not** damaged (stays `verified`); only the new counterfactual checkpoint is stamped `mismatch`. The live-vs-receipted hash comparison is unaffected (None-hash receipts never update `latest_hash`). **Drop the docstring-contradiction argument** (`replay.py:10-12` is a claim about the memory graph, not the ledger, and the function's own docstring documents that it writes a run) — the defect does not need it.
- **Impact:** irreversible through any API in this module (receipts are append-only, no DELETE path in `src/`), and the poisoned report is embedded in the exportable bundle at `client.py:2422/2432`.

---

### A-18 · tier1 · replay
**The policy regression gate reads an empty `allowed_memory_types` as "no type allowed" — the inverse of every other reader — so `lost_baseline_allowed_candidates` is unreachable at the default**
`src/memotron/replay.py:1100-1118`

- **Label:** OBSERVED (default Motive, `salience_threshold` raised to 0.99: 2 of 2 baseline-accepted candidates lost, `passed=True`, `failures=()`, `lost_allowed=0`, `blocked_disallowed=2`).
- **Mechanism:** `allowed = {…}` from an empty tuple makes `candidate_type_by_digest.get(digest) in allowed` False for every digest, so `original_only_allowed` is always `[]` and the failure at 1115-1116 cannot fire. The counterfactual reads it the other way (`if gate.allowed_memory_types and memory_type not in …`, 604), as does `config.py:2876-2877` ("Empty = allow all types (no-op filter)"), with `()` the pydantic default at `config.py:2913`.
- **Precondition:** candidate Motive at the default empty allowlist, rejecting baseline-accepted candidates for a non-type reason — raised `salience_rubric.min_salience`, or `governance.pii_sensitivity='high'` against unencrypted sensitive payloads (`replay.py:609-614`, resolved at `client.py:1839-1851`). A non-empty allowlist behaves correctly.
- **Impact:** the type's stated preservation guarantee (`replay.py:211-218`) is void, and `blocked_disallowed_count` actively misattributes the loss as "correctly blocked because the type is disallowed" when no type is disallowed. **Scope it as advisory SDK surface, not a wired pipeline:** consumers are `Memotron.policy_regression_gate` (`client.py:2369`) and the `GovernanceCertificationBundle.policy_regression_gate` field (`replay.py:266`); nothing in this repo enforces the result. Harm is contingent on an operator treating `passed`/`failures` as a release gate — which the type's own name and docstring invite. **Drop "silently kills memory formation."**

---

### A-19 · tier1 · dreaming
**Stale-rollup revival demotes every member unconditionally, silently voiding an operator pin — the exact failure WS-23 C3 claims to have fixed**
`src/memotron/dreaming.py:3949-3953` vs the guarded sibling at `:4212-4217`

- **Label:** OBSERVED (reader static; refuter constructed the fixture and reproduced it).
- **Mechanism:** the revival branch writes `{"active_in_context": False, "rolled_up_by": …}` for every member with no `_context_demotion_held` check. That predicate (`dreaming.py:10090-10109`) documents itself as "the shared predicate BOTH paths consult at the moment they would write `active_in_context=False`" and has only two call sites (3638, 4213); line 3952 is an unguarded third write.
- **Preconditions (two narrowings to the original):**
  1. `rollup_consolidation` enabled; rollup R has a pinned member (so the pin survived the 4213 gate on first demotion).
  2. A member is later **superseded** — via `correct_memory` (`client.py:5937`), operator review approval (`client.py:6481`), or engine-side supersession (`dreaming.py:9817`). **The pruned case does not reach this branch:** `_stale_rollup_for_rebuild` (4690, 4714) requires `sorted(resolved_members) == target` against the current cluster, and a pruned member resolves to itself and is no longer an active candidate.
  3. The successors must pass `_rollup_cluster_is_consistent` (3840) alongside the pinned row. A raw `correct_memory` successor carries `claim_mode="correction"`, which that gate refuses to cluster with an ordinary preference; the repro normalized `claim_mode`/`directive_stance`, mirroring `tests/test_theme_dependencies.py:224-235` ("the same preference claim once its correction has been accepted by the control plane"). **This is the control-plane-approved-correction path, not every supersession.**
  4. The next consolidation pass re-clusters the same resolved member set and synthesizes byte-identical normalized text, so the no-op revival branch (3897-3973) is taken.
- **Impact:** the pinned fact gets `active_in_context=False` and is dropped by `profile()` at `client.py:8272`, *before* the pin is read at 8284 — the operator's guaranteed-context hold is gone with no error and no per-member `CONSOLIDATION_MEMBER_DEMOTED` receipt (the 4212 path emits one at 4229-4239; this path emits only the rollup-level `…_RECOMPUTED_NOOP`). Search still surfaces it via pin injection (`client.py:3864-3892`), so the loss is profile-context only — which is why it is silent. **Secondary symptom (OBSERVED):** the revival path never increments `members_demoted`, so the write is invisible in job stats as well as in receipts.

---

### A-20 · tier1 · migration · **DERIVED (mechanism OBSERVED)**
**`entity_canon` / `predicate_canon` are never carried to the destination scope key; the non-recoverable loss is the human-adjudicated entity decisions**
`src/memotron/migration.py:352-357` (comments only — `grep -n "entity_canon|canon"` finds no registry write in the module)

- **Mechanism (OBSERVED):** both registries are scope-keyed (`sqlite.py:3376-3406`) and resolution reads them per scope (`dreaming.py:6044-6046, 6059-6061`). Migration recomputes the destination truth key from the row's stored `predicate_canonical` (`migration.py:373-380`) but writes no registry row.
- **The claim that survives:** `entity_canon` is the sole record of operator adjudication — `resolve_entity_alias_proposal` (`client.py:6182-6246`) records active/rejected decisions only there, and `resolve_canonical_entity` consumes it both to resolve through an approved link (`dreaming.py:6928-6955`) and to block re-proposing a rejected pair unless the new score beats the recorded rejection by ≥ 0.05 (`dreaming.py:7115-7126`). After a move the destination has neither, so **a pair an operator explicitly REJECTED can be silently auto-linked at the destination on the next formation run.** Secondary and order-dependent: if the destination forms a paraphrase surface *before* the migrated corpus is replayed, that surface freezes self-canonical and the migrated row's canonical slot can never be bridged (first-wins registration).
- **Four sub-claims from the original filing are wrong and must not be repeated (all OBSERVED):**
  1. *"Aliased mentions fragment into new nodes"* — entity-resolution candidates come from the scope's entity **nodes** via `nodes_for_scope` (`dreaming.py:6414-6436`), which migration copies (`migration.py:382-411`). A mention clearing `auto_link_threshold` (0.85) re-links onto the migrated entity and re-registers the alias.
  2. *"Loses bridging for EVERY migrated fact"* — migrated episodes arrive with fresh uuids and no processed markers (`migration.py:294-309`), so the destination re-forms them (`dreaming.py:1898-1913`, 2452), re-registering `predicate_canon` in arrival order and triggering `rebridge_scope_truth_keys` (6108-6115, 6280-6300) on any new non-identity mapping. A window, not a permanent state.
  3. The entity half is entirely inert for a content-protected destination (`dreaming.py:6922-6924`) — which is exactly the path `migration.py:373-380/429-446` takes.
  4. *"Source registry rows survive as orphans pointing at deleted nodes"* — registry rows are `(scope_key, normalized_name) → canonical TEXT` and carry no node reference; `purge_tenant_state` is explicitly a "reset generated state without deleting raw episodes" operation (`sqlite.py:5211-5225`), so retaining the registry is what makes a post-purge re-dream land on the same truth keys. SQLite and Postgres agree.
- **One unstated concern worth adding:** `entity_canon` stores **plaintext** entity names (`dreaming.py:7331`) and survives an admin "purge generated state" — for unprotected scopes.

---

### A-21 · tier2 · replay
**`certify_motive` omits the graph-state-hash and replay verification its own docstring promises**
`src/memotron/replay.py:930-964`; helper `_certification_metrics` at `:798-878`

- **Label:** OBSERVED (`awk 'NR>=798 && NR<=878' replay.py | grep -n checkpoint` → the parameter list only; every computation in the body reads `receipts` or `motive`).
- **Mechanism:** `certify_motive` loads `checkpoint_for_run` (951), passes it to a helper that ignores it, then reads only `checkpoint.motive_version_digest` and `checkpoint.merkle_root`. `byte_replay` is never invoked. `passed` (872-877) is computed purely from receipt-stream self-consistency, while `replay.py:933` and the module docstring at `:14` claim it "verifies the chain + graph state hash".
- **Factual correction to the original filing:** *"It has no graph handle in its signature"* is **wrong** and must be dropped — `certify_motive(client, …)` receives the client, and `_client_ledger` (881-892) already walks `client._graph` / `client.graph` / `client._store` / `client.store`. The fix is a few lines, not a signature change.
- **Precondition:** none for the omission. It bites when a formation run's receipts do not faithfully describe what landed — i.e. (a) mutating receipts with NULL `graph_state_hash_before/after`, (b) a per-scope state-hash discontinuity (an unreceipted write interleaved with receipted ones), or (c) reconstructed final hash disagreeing with `checkpoint.graph_state_hash_after`. Pure row tampering inside `memory_receipts` **is** caught by `verify_chain`.
- **Impact, narrowed:** the uncompensated consumer is `policy_regression_gate` (`client.py:2369-2389` → `replay.py:1113`). `certification_bundle` **is** compensated — `client.py:2420-2422` independently calls `byte_replay` and `verify_no_silent_mutations` and surfaces both as separate bundle fields.
- **Feasibility note (from the repo's own measured D-55, `DOGFOOD-LOG.md:1623-1697`):** a liveness-anchored `byte_replay` only passes for the most-recent run in a scope — which is exactly where `certify_motive` sits, immediately after driving its own formation run. Not a safe general retrofit elsewhere.

---

### A-22 · tier2 · migration
**Row-level derivation provenance is copied verbatim, so migrated rows carry provenance stamps that are false in the destination**
`src/memotron/migration.py:413` (`new_properties = dict(relationship.properties)`; the update dict at 414-459 rewrites only scope, truth, content and episode fields)

- **Label:** OBSERVED.
- **Strongest leg (unconditional, no purge precondition):** `restored_from_relationship_uuid` — `dreaming.py:9641-9665` stamps it on an ACTIVE row pointing at a superseded/temporary predecessor, and migration copies **only ACTIVE rows** (`migration.py:89-94`), so the predecessor is never copied in either the same-store or cross-store case. Same reasoning applies to `superseded_by_…`-style back-pointers.
- **Weaker legs, correctly conditioned:** `produced_by_run` and `promoted_from_relationship_uuid` **still resolve after a same-store rename** — `purge_tenant_state` runs with `preserve_receipts=True` (`migration.py:988`), and promotion sources are agent-/user-scope rows (`agent_memory.py:2962-2995`) whose scope keys embed no tenant id (`agent_memory.py:664-672`) and which survive the purge (`sqlite.py:5227-5245`, asserted at `migration.py:990-999`). They dangle only **cross-store**, where agent rows are re-copied with fresh uuids (`migration.py:715-758`) and user-scope rows are not migrated at all.
- **Impact, narrowed:** *"any replay, attestation, or lineage walk follows these to nothing"* is **not supported** — `produced_by_run` is consumed only as a display field (`epochs.py:693, 722`) and as a state-tuple member (`sqlite.py:609`); `replay.py` resolves runs from the ledger by `run_uuid` (437-452, 644-648) and never joins on it; `restored_from_relationship_uuid` has no production consumer; `promoted_from_relationship_uuid` is emitted as a string by `client.py:4996-5038` and never dereferenced. Nothing fail-closes. The real harm: a false provenance stamp that is shown verbatim in `memory_evidence`/`memory_explain`, shown in epoch diffs, and **cryptographically committed as the destination's derivation provenance via `graph_state_hash`** (`sqlite.py:474, 609`). The module docstring (42-44) claims migration "mints its own honest receipt lineage under the destination tenant" — true of the emitted receipts, false of the row-level provenance.

---

### A-23 · tier2 · coherence
**The engine's `coherence_hold` control flag shares an unnamespaced dict with free-form artifact-supplied metadata; a truthy value suppresses every contradiction pair involving that directive**
`src/memotron/coherence.py:250` (verbatim copy) and `:374-376` (both-sides test)

- **Label:** OBSERVED for the mechanism (identical fixture: no metadata → contradiction 1; `coherence_hold=True` in the artifact's directive metadata → contradiction 0). The exploitation narrative is DERIVED.
- **Mechanism:** `metadata=dict(declared.metadata)` copies caller-supplied `ArtifactDirective.metadata` (free-form, `models.py:1339`) onto the projected Directive; the idempotency skip tests the flag on **both** sides with no `ArtifactClass` check despite its comment saying "memory directive". The artifact-side half can never be set by any first-party writer (`dreaming.py:1833` writes the flag only to memory relationship properties), so caller/artifact data is its only possible source. Persists through `register_live_artifact` and re-projects via the default `GraphArtifactSource`. No validation, no reserved-key rejection, no log line.
- **Precondition:** the artifact's declared directive carries a truthy `"coherence_hold"`. Not reachable via the MCP `register_artifact` tool, which builds `ArtifactDirective` without metadata (`mcp_server.py:973-980`).
- **File this as a control-flag namespace/hardening defect, not an active bypass.** Four narrowings: (1) **`ESCALATION_WINDUP` is unaffected** — the guard at `coherence.py:329` walks memory directives only, so the headline stale-self-skill detector still fires; suppression is limited to `CROSS_ARTIFACT_CONTRADICTION`. (2) "A self-authored skill can opt itself out" is DERIVED — no in-repo code translates artifact content into directive metadata; it requires a caller-implemented `ArtifactSource` that copies content-derived metadata verbatim, an integration the docs invite (`client.py:2520-2523`) but that does not exist here. (3) `PersistentArtifact.metadata` is already a sanctioned caller control channel (`approved_reactivation`, `sqlite.py:2276`), and a caller controlling the projection can evade detection for free by declaring `stance=PREFER` (`coherence.py:64-71`); the added capability is only "evade while keeping a FORBID/ASSERT stance". (4) Not undetectable — registration is receipted and version-captured (`client.py:2537-2547`).
- **Fix:** namespace the internal flag, or ignore it on non-MEMORY directives.

---

### A-24 · tier2 · certification
**MemoryAgentBench's official scoring metric is selected by substring-matching the fixture basename, with one silent-and-lenient case**
`src/memotron/certification.py:1024-1031`

- **Label:** OBSERVED (`accurate_retrieval_test.parquet` → `substring_exact_match`; `long_range_understanding_test.parquet` → `exact_match`; `lru_renamed_accurate_retrieval.parquet` → `substring_exact_match`; `mydata.parquet` → `None`).
- **Mechanism:** basename substrings select the metric with no validation against record content and no explicit-metric parameter on `load_suite` (1476). The result is written into every question's metadata (1563) and the suite's provenance (1582), and `LocalOfficialBenchmarkJudge` dispatches on it (663-676).
- **Narrow the defect window to one case.** The mapping **fails closed** on any unrecognized basename (`None` → `ValueError` at 677-680), so rename/shard/subdirectory cases are loud. The single silent case: a file whose basename contains one task's token while its content is a different task — e.g. a `long_range_understanding` or `test_time_learning` export hand-renamed into the `accurate_retrieval`/`conflict_resolution` naming that `README.md:906` **requires operators to apply manually** to four downloaded exports. **Drop** the directory-named-by-task, shard, and prefix/suffix-copy preconditions.
- **Impact:** in that case the judge silently drops from `exact_match` to the strictly weaker `substring_exact_match`, and the report records the swapped label as provenance under `judge_identifier memoryagentbench-official-metrics@455306d`. With `ProfileTopEvidenceExecutor` (which returns a whole verbatim profile fact line, 757) the difference is roughly "almost always 0" vs "often 1". **Soften the provenance claim:** the report does record `input_path` (1581) and raw `question_types` (1571) next to the metric — what it cannot reveal is that the basename disagrees with the content.
- **Note:** no official MemoryAgentBench export exists in the repo, so this path is untested against real data.

---

### A-25 · tier2 · dreaming
**`_apply_soft_cap` writes an untrimmed `pruning_retention_held` decision record for every ineligible active on every run, before and independent of any cap-breach test**
`src/memotron/dreaming.py:9860-9916` (per-relationship loop) vs `:9918-9925` (the cap test)

- **Label:** OBSERVED.
- **Mechanism:** the per-relationship loop runs unconditionally once any cap is configured and records a held decision at 9901-9907; the cap comparison happens afterwards in a separate loop. Contrast `_apply_generic_prune` (9369-9383), which records only for a row that actually reached a prune trigger. `record_dream_decision` is insert-only with no trim (`storage/sqlite.py:1446-1462`).
- **Precondition:** `GrowthPolicy.soft_cap` or `per_type_soft_caps` configured (the fast path at 9847-9849 returns 0 otherwise, so it is dormant at library defaults). Worst with `job.scope=None`, which spans every scope.
- **Volume, corrected:** the original's "6 of 8 protected types, so most rows are held" comes from `RetentionPolicy()` defaults (`config.py:2158-2165`), but the only shipped config that turns soft caps on narrows `protected_memory_types` to `(ANCHOR, REQUIREMENT)` — 2 of 8 (`agent_memory.py:551-556`). The held population is anchors + requirements plus rows held by the other gates in `_retention_eligibility` (`dreaming.py:10127-10159`: `delay_hold_active`, `protection_lift_grace_active`, `coherence_hold`, `active_secret_reference`, `operator_correction`, `pinned`). At the shipped `agent_memory_config()` cadence that is (ineligible actives across all scopes) × **144 runs/day**, forever.
- **Impact, corrected:** the summary string is literally true, so call this **misleading-by-context, not false** — an operator cannot distinguish a hold that actually prevented an eviction from one recorded against a scope nowhere near its cap. The perf leg (`_retention_components` at 9888 runs before the eligibility branch and does `receipts_for_scope` per row at 10238) is real but is the same class as the filed T1-7 and **should be folded there**, not counted as new impact.
- **Fix:** defer the held record until the scope is known to exceed a cap, or gate it on the already-defined-but-unread `audit_verbosity`.

---

### A-26 · tier2 · dreaming · **DERIVED**
**`max_items_per_run` bounds approved mutations only, so systematically unapproved prune decisions make one run issue unbounded transport calls, receipts and decision records**
`src/memotron/dreaming.py:9440-9442`

- **Label:** DERIVED. Steps: `run.pruned_relationships` increments only on an approved prune (9475/9499/9519/9552/9570; `_apply_generic_prune` returns False on a retention hold or `decision.approved is False`, 9394-9395) → a rejected decision never advances the break counter at 9441 → the loop runs to the end of `self._graph.relationships()`. Each eligible candidate still costs a `_decide` round trip (5367-5383); on transport failure `_fallback_decision` logs a warning (5427) and emits a `DREAM_AGENT_TRANSPORT_FAILED` receipt (5440-5468) before the fail-closed `approved=False` return (5482-5496) for `pruning_relationship_pruned`.
- **Precondition:** a non-deterministic-local dream-agent transport configured for the pruning job that is failing **or** a healthy agent that systematically declines, with more prune-eligible relationships than `max_items_per_run` (default 1000, `config.py:3632`). The soft-cap loops are unaffected — they slice `sorted_by_value[:to_prune_count]` (9929, 9993).
- **Impact, corrected on two points:** N is not "every relationship in the store" but the candidates that both match a prune trigger and pass the retention gate (retention-held candidates cost a `DreamDecisionRecord` at 9370-9383 but no transport call). The scope-claim clause is overstated — claims are released in a `finally` on every exit path (`dreaming.py:941-947`) and expire after `DREAM_CLAIM_STALE_SECONDS` (`sqlite.py:1341`), so cadences firing during the long run are skipped but nothing is wedged afterwards. Net: the knob that looks like the run's work budget does not bound the run when the run is failing.

---

### A-27 · tier1 → **tier2** · interop
**`README.md:1346`'s "zero code changes" claim is false — 3 of 6 `memory_20250818` commands raise on contract-conformant payloads**
`src/memotron/interop.py:400-402` (create), `:487-489` (insert), `:535-536` with `:576-582` (rename)

- **Label:** OBSERVED. Contract verified by an independent path: the Stainless-generated SDK models in the installed `anthropic` package (`types/beta/beta_memory_tool_20250818_{create,insert,rename}_command.py`, generated from the OpenAPI spec) give `create = {command, path, file_text}`, `insert = {command, path, insert_line, insert_text}`, `rename = {command, old_path, new_path}` (no `path` field) — identical across 7 installed builds, 0.72.0–0.84.0. Executing `handle()` with exact contract shapes raised `MemoryToolCommandError` on all three; `view`, `str_replace`, `delete` are fine.
- **Precondition (the original said "none" — that is wrong):** the caller passes the **Anthropic contract's** field names. A caller following the repo's own README example (`README.md:1356` sends `content`) succeeds. The failure fires only on the advertised drop-in path.
- **Why 62 prior findings missed it:** `tests/test_interop.py` encodes Memotron's own dialect (`"content"` at 81/160/193/209/240/249/262/285/306/350; `"new_str"` at 108/124/199; `path`+`new_path` at 136). The suite is self-consistent with the defect.
- **Impact, narrowed to tier2:** `AnthropicMemoryToolBackend` is an in-memory POC consumed in exactly one in-repo place (`examples/fleet_simulation.py:1387`, using Memotron's dialect). No MCP server, HTTP route or Claude Code hook calls `handle()`, so nothing running breaks. Separately, the class is not a `BetaAbstractMemoryTool` subclass and does not implement the SDK's per-command method interface, so **any** real integration already needs a glue layer — which means the "zero code changes" claim is false independently of the field names, and a glue layer that maps three names makes the backend work. Fails loudly on first integration, corrupts nothing. Documentation/interface-contract defect.

---

## 2. Spun off — file separately, do not fold into the above

Both surfaced during refutation and are distinct defects:

- **`mcp_server.py:1070-1084`** — `list_coherence_incidents` accepts a `limit` argument and then drops it, pinning that surface at 20. This is what makes A-09's starvation materially worse on MCP, but it is its own bug.
- **`storage/postgres/_governance.py:1228-1235` / `:1302-1310`** — Postgres `set_tenant_llm_credentials` accepts no `embedding_*` parameters and `tenant_llm_credentials` returns no `embedding_*` keys, so `migration.py:551` raises `KeyError` against a Postgres source and `TypeError` against a Postgres destination. Tenant migration is broken on Postgres, independently of A-04.

---

## 3. Killed by the skeptic — areas checked and found clean

Three candidates were refuted and are **not** in the register. Recorded so the next reader does not re-file them.

| Candidate | Why it was killed |
|---|---|
| `record_coherence_repair` rejects every incident whose lower-precedence side is not a memory row (`client.py:3464`) | Mechanics reproduce, but the guard is a correct fail-closed consequence of design, not a defect. Outcomes are memory-relationship-keyed (`client.py:1519-1521, 1551-1553`) and `open_coherence_repair_monitor` requires a non-null `relationship_uuid` in schema (`sqlite.py:3244-3260`), so a skill-vs-file pair could never close a monitor. The claimed impact is false: `resolve_coherence_incident` **does** emit a `COHERENCE_INCIDENT_STATUS_CHANGED` receipt plus a dream decision (`client.py:3125-3148`), and OPEN → acknowledged → resolved was driven successfully. `record_coherence_repair` is also on no operator surface (absent from `mcp_server.py` and `admin_server.py`), and the "re-minted every scan" limb is already filed as T2-03. |
| LongMemEval judge resolves ambiguous/refusal verdicts to CORRECT and interpolates unescaped (`certification.py:578`, `:526-569`) | Refuted three ways. (1) The bare substring test and 10-token cap are the **pinned upstream protocol** verbatim — verified against `LongMemEval/src/evaluation/evaluate_qa.py`; adding a not-parseable branch would break the comparability the identifier promises (`README.md:900`). (2) The impact direction is backwards: `"yes" in verdict.casefold()` returns **False** for refusals, N/A, blanks and truncations — the conservative direction. (3) The injection limb conflates prompt content with verdict parsing, and its precondition combination (untrusted corpus + verbatim-echo executor + LLM judge) is instantiated nowhere in the repo. |
| `_retention_eligibility` short-circuits every retention hold when the prune reason is `valid_to_elapsed` | Code is as cited; the harm does not occur. A pinned row whose `valid_to` has elapsed is **already invisible** to every live read (`client.py:8782-8827`), so the ACTIVE→PRUNED write relabels an already-dark row. Time-travel reads still surface it post-prune (`client.py:8802-8806`), which is the documented contract (`README.md:2806`). The missing ghost is deliberate — `restore_prune_ghost` sets `clear_valid_to=True` (`sqlite.py:1970-1979`) and retrieval auto-restores (`client.py:7788`), so a ghost would resurrect an expired fact as still true (rationale stated in code at `dreaming.py:9806-9807`). It is neither silent (a decision + receipt carrying `eligibility_reason="valid_time_elapsed"` is recorded, 9384-9406) nor undocumented (`docs/design/15-read-auditing-legal-hold.md:59,105,154,443` already records it as *the* known hold bypass). One of the two "contradicted contracts" was misapplied — the `config.py:2205-2216` quote is scoped to `stale_unused`. |

---

## 4. Coverage and gaps

### Read in full
`replay.py` (1,153 lines) · `coherence.py` (482) · `certification.py` (2,206 — note: **not** 2,068 as the brief stated; verified by `wc -l`) · `migration.py` (1,025) · `interop.py` (1,061) · `tests/test_coherence.py` · the `dreaming.py` pruning/retention/rollup/coherence tail (9336-10409, 1696-1853, 3566-3973, 4195-4239, 4673-4719, 4891-4971, 5302-5578, 156-217).

### Executed (read-only, temp dirs / hermetic tmp SQLite; no repo file modified, no git write)
- **replay**: two repro scripts covering counterfactual poisoning, fail-open no-silent-mutation, the regression gate, and checkpoint-column tampering vs `verify_chain`/`byte_replay`/BOM.
- **coherence**: `uv run pytest tests/test_coherence.py -q` → 20 passed (baseline), plus 8 standalone repros.
- **certification**: 4 repros against the real offline harness, including the end-to-end baseline→alias-live path.
- **interop**: both findings reproduced by execution; the Anthropic contract verified against installed SDK models rather than docs.
- **dreaming-tail**: static reading by the primary reader; A-19 was reproduced by the refuter, A-25/A-26 were not executed.

### Still unread — the honest gaps
1. **Postgres, almost entirely.** Not read: `storage/postgres/` receipt/checkpoint implementation (so A-16's tamper surface there is unverified), `_graph.py` (`update_relationship` / `mark_relationship`, so A-19's backend parity is unknown), `_policy.py` beyond confirming the `certification_passed` column exists, the purge/migration path beyond a grep confirming the `dream_job_runs` delete, and `live_artifacts` / `register_live_artifact` / `active_relationships` (so A-10's `updated_at` guard and the full-table-scan shape are confirmed for **SQLite only**). `storage/composite.py:94`'s `purge_tenant_state` was not read, so split-store routing of A-06/A-07 is unverified.
2. **Migration was never executed by the primary reader** — every migration finding rested on source reading until the refutation pass, which executed A-03 and A-05 only. A-06 and A-20 remain DERIVED.
3. **Admin HTTP surface and MCP exposure** of replay, certification, coherence and purge — not audited, so the caller count inheriting A-07 and A-02's class of defect is unknown.
4. **Benchmarks against real upstream data** — no official LongMemEval / LoCoMo / MemoryAgentBench export exists in the repo. The three adapters were read but never exercised; A-24's impact is derived from the metric function's output, not a real run. *(Answered, no finding: none of the three corpus adapters touch the network — the only `urllib` use is the answer runtime / LLM judge, which raises on a missing API key rather than scoring zero.)*
5. **Not chased:** why the baseline motive violates `retrieval_allocation` at all (lives in the formation engine; A-01 stands either way); `epochs.py:921` shadow-graph `byte_replay`; `erasure.py:402/525` beyond noting `graph=None`; ~1,000 remaining lines of `tests/test_replay.py`; `PATENT_REPLAY_RECEIPTS_SPEC.md` itself (claims were judged against module docstrings, not spec text); `MotiveAsInstructionsAdapter`; header/credential forwarding in a production transport (none exists — only `EchoUpstreamTransport`); the outcome-driven hold-release/rollback paths (`client.py:1530-1700`); `resolve_disambiguation_request`; concurrency of two scans racing on one scope; formation-side decision points.

### Deliberately not filed (checked, judged not worth a register entry)
`AnthropicMemoryToolBackend.mount_mode` unwired to `read_only_scopes` (documented as caller responsibility, `interop.py:205-208`) · `_validate_path` non-normalization of `//` and `.` (dict-backed, no traversal consequence) · inconsistent recursive `view` · `OpenAICompatibleBenchmarkRuntime`'s hard-coded gateway default (repo-wide, fails loudly with 401, overlaps T0-5) · `_lifecycle_invariants` skipping chain verification for receipt-less runs (no constructible case) · `_dispositions` dropping an orphan decision receipt (ordering guarantee at `receipts.py:1197` makes it unreachable) · node `name_embedding` stripped on migration (re-stamped on next formation touch; tradeoff stated in code) · `_ReconstructedGraphState` (`replay.py:278-337`) being dead computation whose `context_visible_uuids` has no reference repo-wide (supporting context for A-16, not a finding) · the `O(actives × receipts)` cost of `_retention_components` (overlaps filed T1-7).