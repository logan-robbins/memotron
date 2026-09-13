# Jedai docs portal → Memotron: will this ingestion demonstrably deliver state-of-the-art 2026 memory-as-graph?

This is the goal-oriented rewrite of the portal-ingestion feasibility report. The organizing question is no longer "can we ingest it?" (yes — every primitive is wired) but "**will the resulting graph demonstrably deliver 2026-grade memory-as-graph behavior**", judged on three goals:

1. **Entity matching** — one real-world entity mentioned across many pages → ONE graph node with edges from all pages. No fragmentation ("Jedai Gateway" / "the gateway" / "gateway v2" / "JedAI GW"), no false merges (two distinct Vertex data stores must never merge).
2. **Concept matching** — semantically related facts across pages connect (THEME clusters / graph adjacency), so a concept-level query ("special offers ingestion pipeline") surfaces cross-page knowledge, not keyword hits from one page.
3. **Graph-native wins** — multi-hop joins (entity_neighborhood / beam expansion), temporal supersession (`as_of` truth after doc updates), evidence precision (fact → `slug#anchor`).

> **Citation contract.** Every code claim below cites `file:line` in the committed state at `33336e5` ("WS-16 core", HEAD at time of writing). Paths are `src/memotron/` unless noted. Do not diff these line numbers against a dirty working tree. Measured cosine numbers were computed with the committed `LocalEmbeddingTransport` against strings taken verbatim from the portal corpus.

## 0. Verdict at a glance

| Goal | Today's engine + Phase-0 driver/config | After WS-17/WS-18 land | Requires NEW engine work |
|---|---|---|---|
| 1. Entity matching | Partial: case/whitespace variants merge; alias forms fragment; prompt canonicalization mitigates but cannot guarantee | T17 sweeps duplicate fact rows; T18 improves detection signal — **node identity still exact-string** | **Yes — WS-A entity-alias registry (§4c). No audit workstream covers node-level entity resolution.** |
| 2. Concept matching | Partial: vocabulary-level THEME clusters work (docs repeat terminology); synonymy/paraphrase recall measurably fails (0.168 cosine on the probe pair) | **Hard-depends on T18 (real embeddings) + T19 (real theme labels)** — this is where concept matching becomes real | No — the machinery (clustering, two-tier read, theme↔member expansion) exists; only the vector space and label synthesis lag |
| 3. Graph-native wins | Mostly yes today: beam expansion with auditable `origin`, valid-time supersession + `as_of`, per-fact `slug#anchor` evidence | T16 makes paraphrased-predicate corrections supersede (edit-and-resync robustness); T14 gives operators the parked-row queue | Minor: `hops` param on `entity_neighborhood` (driver can chain today) |

Memotron's differentiators are real and none of the competition has the combination: **receipted deterministic formation with byte replay, contract-pinned deterministic retrieval, motive-gated formation, corroboration-gated evidence-weighted supersession, crypto-shred RTBF**. Its lags are equally real: **hermetic trigram embeddings, no entity resolution beyond normalized string keys, token-bag theme labels, predicate-string truth slots**. The pilot is designed to measure exactly the lags, on the goals, for ~$3.

---

## 1. The corpus (numbers re-verified against the portal checkout)

`../portal` is **jedai-astro-docs** — the Jedai platform documentation portal (Astro 5 + Starlight + MDX, SSR behind Entra ID SSO with per-route RBAC). Not a database-backed CMS: all content is MDX in-repo under `src/content/docs/`.

- **357** MDX/MD files, **2.39 MB** actual bytes (previous report said 2.5 MB — on-disk rounding; corrected), **1,561** H2 sections, ~1,100 internal links, 127 mermaid diagrams. ✔ re-verified.
- Second content plane: `src/data/openapi.json` — **346 HTTP method entries, of which 344 carry `operationId`s** (the previous "344 operations" = the operationId count; 2 method entries lack IDs — the deterministic OpenAPI emitter should key on `method+path` and treat `operationId` as optional). Corrected for precision.
- Stable identity = file path/slug. No `updated_at` frontmatter, but full git history supplies commit timestamps and change detection. Zero user-generated content; 60 pages are `visibility: jedai` (RBAC-gated); an `authorship` frontmatter field scores human-curated vs AI-drafted percentage.
- A Decap CMS exists but only manages release notes via PRs.
- **Pilot slice** (`products/jedai-gateway` + `platform/knowledgebase` + `solution_engineering/platform-decision-log`): **77 files** ✔ re-verified → ~206–230 episodes → ~$2–3 extraction → expected ~700–1,200 active facts.

The pilot slice is adversarially good for the three goals, verified by direct inspection:

- The gateway is named at least seven ways in pilot pages: `jedai-gateway` (82×, slug form), `Jedai Gateway` (68×), `the gateway` (64×), `The gateway` (25×), `JedAI gateway` (4×), `the Gateway` (3×), `Jedai gateway` (1×) — the Goal-1 fragmentation gauntlet.
- The knowledgebase pages enumerate a family of **distinct** Vertex AI Search data stores differing by one token: `kb_ds_source_dscribe_special_offers_wdw_en_us_v1` / `..._dlr_...` / `..._aulani_...`, `kb_ds_source_plandisney_pocket_guides_wdw_en_us_v1` / `..._dlr_...`, plus `kb_ds_source_zendesk_call_center_en_us_v1`, `kb_ds_source_dscribe_photopass_page_aulani_en_us_v1`, `kb_ds_dx0000_tester_1_v1` — the Goal-1 false-merge gauntlet.
- `platform/knowledgebase/sources/dscribe/special-offers.mdx` + `sources/index.mdx` + vector-store pages spread the "special offers ingestion" concept across D-Scribe (source), pipeline, and data-store pages — the Goal-2/Goal-3 join gauntlet. D-Scribe and GCX both appear across multiple `sources/dscribe/*` pages.

## 2. The ingestion design (mechanics unchanged, three corrections)

One episode = one H2-bounded section (~900–1,000 episodes corpus-wide; median page ~5.6 KB → 1–2 chunks). A preprocessor strips frontmatter/imports, unwraps Starlight components, rewrites internal links to `label (→ /path/)`, keeps tables/mermaid inline. Whole sections fit the existing chunker: `add_context` accepts `max_chars_per_episode=4000` and snaps chunk boundaries to paragraph/sentence breaks past the half-window mark (`client.py:4918-4943`).

Per section: `add_context(name=..., content=section_text, scopes=[kb_scope], custom_id=f"{slug}#{anchor}", reference_time=git_commit_time, metadata={slug, section, tags, audience, authorship, internal_links, content_sha256, "_verified_source_authority": "operator"}, motive="ingest-portal-kb", trusted=...)`. All of that is a first-class signature today (`client.py:346-360`); every chunk episode is stamped with `shadow_document_id`/`shadow_custom_id`/chunk indexes (`client.py:408-419`). `reference_time` becomes the default `valid_from` of extracted facts (valid-time contract, `dreaming.py:3325-3349`), and the **entire episode metadata dict is inherited onto every materialized fact** — relationship properties store `{**episode.metadata, **memory.metadata}` (`dreaming.py:3831`) — which is what makes fact → `slug#anchor` evidence free. `_verified_source_authority` is honored by the authority resolver (`dreaming.py:447`), `trusted=False` flows to the untrusted-directive gate (`dreaming.py:452`).

Scopes: dedicated `tenant:jedai-portal-kb`, plus a separate internal scope mirroring the 60 RBAC-gated pages; both declared in consumers' `read_only_scopes` (`config.py:2301`) so KB writes fail fast from any agent. The OpenAPI plane skips the LLM entirely: 344 operations (§1) emit deterministic `Memory:` episodes at zero cost.

Incremental sync: per-section sha256 manifest → unchanged section = no episode, no LLM call; changed → re-ingest under the same `custom_id`; deleted → `forget_memory` per fact (enumerate a section's facts via the inherited `metadata.slug`/`shadow_custom_id` on relationship properties). Unchanged facts that re-extract reinforce (`observed_count`++, bounded confidence accumulation); changed single-active values supersede at the commit time with the old truth staying `as_of`-queryable.

**Corrections to the previous report:**

1. **The dedup thresholds are not defaults.** `DedupPolicy.cosine_threshold` defaults to 0.88 and `memory_type_thresholds` defaults to **empty** (`config.py:776-777`). The previous report's "dedup 0.90, identity 0.95" are config choices the jedai-kb config must set explicitly — and §4 shows 0.90 is not conservative enough for this corpus.
2. **The Motive as previously specified would silently disable themes.** If the consolidation job resolves a Motive whose `allowed_memory_types` excludes `theme`, thematic consolidation is skipped wholesale with a `motive_theme_not_allowed` receipt (`dreaming.py:1573-1618`). The previous spec ("encyclopedic types only: identity/requirement/decision/directive/state — no preference") kills Goal 2. Fix: add `theme` to the Motive's allowed types, or run consolidation as a separate job with no Motive pin.
3. **The WS-16 corroboration interplay was half right.** With the sha256-manifest sync, unchanged sections are never re-extracted, so routine syncs do **not** inflate `observed_count`. What inflates it is **cross-page repetition**: the same `truth_key` stated on N pages reinforces to `observed_count=N` (`dreaming.py:4139-4192` pass 1, `_apply_reinforce` `dreaming.py:4636-4700`). Any fact corroborated on ≥3 pages then hits `corroboration_margin=3` (`config.py:1108`), and a single-page edit at equal authority parks as `insufficient_corroboration` (`dreaming.py:518-528`) until `corroboration_required=2` distinct observations exist (`config.py:1118`, escape at `dreaming.py:4344,4375-4376`). For a KB whose ground truth is the doc itself, the clean override is `SupersessionPolicy(corroboration_required=1)` on this tenant — a doc edit flips truth in one sync cycle while the receipts still record the dispute; keep the default in agent-facing tenants. ("Raise the margin" — the previous suggestion — only works until enough pages repeat a fact.)

---

## 3. What "state of the art 2026" concretely means, and where Memotron stands

The bar, stated as capabilities (verifiable sources; where a vendor claim can't be verified we state it as the design bar, not attributed fact):

- **Temporal knowledge-graph memory** (Zep/Graphiti): bi-temporal facts — valid time *and* transaction/ingestion time, four timestamps (`t_valid`/`t_invalid`, `t_created`/`t_expired`) — with contradiction-triggered **edge invalidation instead of deletion** and point-in-time queries. Graphiti also runs **ingest-time entity resolution**: each extracted entity is resolved against existing nodes via hybrid (semantic + keyword + graph) search before writing, so new mentions attach to existing nodes. Sources: [Zep paper (arXiv 2501.13956)](https://arxiv.org/pdf/2501.13956), [Zep temporal KG overview](https://www.getzep.com/ai-agents/temporal-knowledge-graph/), [Neo4j on Graphiti](https://neo4j.com/blog/developer/graphiti-knowledge-graph-memory/).
- **GraphRAG-style concept retrieval** (Microsoft): entity/relationship extraction → hierarchical (Leiden) community detection → **LLM-written community summaries**, queried in *local* mode (entity fan-out) and *global* mode (community-summary map-reduce). Sources: [GraphRAG paper (arXiv 2404.16130)](https://arxiv.org/pdf/2404.16130), [microsoft.github.io/graphrag](https://microsoft.github.io/graphrag/).
- **Entity resolution / canonicalization** as a first-class ingestion stage (see Graphiti above; GraphRAG merges by entity name+type at index time) — the design bar is: surface-form variants converge to one node with auditable merge decisions.
- **Hybrid retrieval**: lexical + vector + graph expansion fused in one ranked pipeline.

Where Memotron **already meets or exceeds** the bar:

- **Hybrid lexical+vector+graph retrieval** — the six-stage pipeline (scope/temporal filter → lexical+vector candidates → seed-node resolution → bounded weighted beam over entity hops and THEME↔member links → truth/governance filter → weighted rerank) is exactly the bar's shape, and goes past it on auditability: every knob is frozen into a `RetrievalContract` whose digest rides on each result (`retrieval.py:1-18,53-78`; `client.py:2936-3068`), and every result carries `origin`/`hops` explainability (`models.py:394-398`). No 2026 commercial memory product ships **deterministic, byte-replayable retrieval**; this is a differentiator.
- **Temporal supersession** — invalidate-don't-delete with `superseded_by_relationship_uuid` lineage, `as_of` point-in-time search, timeline and evidence APIs (`dreaming.py:3341-3346`; README truth cycle). WS-16 adds evidence-weighted supersession with corroboration parking, bounded confidence accumulation (`c' = min(ceiling, c + (1−c)·gain·c_obs)`, `dreaming.py:4666-4680`) and receipted incumbent dispute discounts (`dreaming.py:4560-4635`) — *more* principled than the bar.
- **Receipted deterministic formation, motive-gated writes, crypto-shred RTBF, FormationContract attestations** — beyond the bar; nobody else binds formation policy into replayable receipts.

Where Memotron **lags the bar**, honestly:

- **Bi-temporality is single-axis.** We track valid time (`valid_from`/`valid_to`) plus audit timestamps, and `as_of` operates on valid time only; a queryable transaction-time axis (`as_known_at`) is explicitly deferred (`dreaming.py:3345-3349`). Zep's four-timestamp contract is the full bar. For a docs KB this is acceptable — valid time is what "what did the doc say on June 1" needs — but state it plainly in any SOTA claim.
- **No entity resolution.** Node identity is an exact normalized-string key (§4a). Graphiti-style ingest-time resolution does not exist anywhere in the engine, and no audit workstream adds it (§4c).
- **Embedding quality.** The hermetic 256-dim char-trigram+unigram transport is deliberate (deterministic, offline) but "sensitive to shared vocabulary, not deep semantics" by its own docstring (`embedding.py:24-27`). WS-17 T18 adds the production transport seam.
- **Theme labels are token-bags** (§5a) vs. GraphRAG's LLM community summaries. WS-18 T19 is exactly that gap. There is also no *global-mode* (corpus-level map-reduce over theme summaries) query surface; `profile(render_mode="typed")` with THEME-first ordering is the nearest analog.

---

## 4. Goal 1 — Entity matching: one entity, one node, no false merges

### 4a. What today's code actually does

**Node identity is an exact normalized-string key.** A node's key is `f"{scope_key}:{label}:{name}"` (`node_identity_key`, `dreaming.py:158-176`), normalized by `normalize_key` = casefold + whitespace collapse (`graph.py:55-56`). `upsert_node` looks up exactly that key (`graph.py:106-118`): hit → property-merge update onto the existing node (`graph.py:120-141`), miss → new node. During formation, subject and object nodes are upserted this way with the extracted `subject_properties`/`object_properties` merged on (`dreaming.py:3395-3420`); the property set is whitelisted per label and **strict by default** — unknown extracted properties raise (`NodeInstruction.strict_properties`, `config.py:305-312`; default Entity whitelist is `kind, role, tier, region, system`, `config.py:2423`).

Consequences, measured on the pilot corpus:

- `"Jedai Gateway"`, `"JedAI gateway"`, `"jedai gateway"` → **one** node (casefold handles case variants).
- `"the gateway"`, `"jedai-gateway"` (slug form), `"gateway v2"`, `"JedAI GW"` → **four more** nodes. Nothing in the engine ever compares node names semantically — node identity uses no embeddings at all.

**Every page's episode links to the entities it mentions.** Formation creates an `Episode` node per episode (`dreaming.py:3355-3371`) and a `MENTIONS` edge from episode to each subject/object entity node (`_add_mentions`, `dreaming.py:4009-4027`). Retrieval's entity-hop expansion deliberately excludes `MENTIONS` and travels only via shared entity nodes on fact edges (`graph.py:406-436`, SQL-side `type != 'MENTIONS'`).

**Fact-level dedup cannot see across subject variants.** `_find_reinforce_target` (`dreaming.py:4139-4229`) has two passes: (1) exact object match within the same `truth_key`; (2) object-embedding cosine over active rows sharing the same `truth_prefix = scope:subject:predicate` (`truth_identity`, `dreaming.py:179-207`), against the per-type threshold `DedupPolicy.threshold_for` (`config.py:792-798`, global default 0.88 at `config.py:776`), with claim-mode/stance/polarity compatibility (`_statement_compatible`, `dreaming.py:4232-4257`) and same source authority required. Because both passes are keyed by the *subject string*, "Jedai Gateway requires OAuth2" and "the gateway requires OAuth2" are different truth slots: **no reinforcement, no supersession, two facts** — fragmentation at the fact plane, not just the node plane.

**False-merge mechanics.** For MULTI_ACTIVE rows the `truth_key` includes the object, so two distinct data-store IDs are distinct rows — but dedup pass 2 can still `SEMANTIC_UPDATE` a *new* object into an *existing* row when their cosine clears the threshold. Measured with the committed transport on real corpus strings:

| pair | trigram cosine |
|---|---|
| `kb_ds_source_plandisney_pocket_guides_wdw_en_us_v1` vs `..._dlr_...` | **0.895** |
| `kb_ds_source_dscribe_special_offers_wdw_en_us_v1` vs `..._dlr_...` | **0.878** |
| `kb_ds_source_dscribe_special_offers_wdw_en_us_v1` vs `..._aulani_...` | 0.875 |
| `Jedai Gateway` vs `jedai-gateway` | 0.641 |
| `Jedai Gateway` vs `JedAI GW` | 0.588 |
| `Jedai Gateway` vs `the gateway` | 0.538 |
| `Jedai Gateway` vs `gateway v2` | 0.526 |

Read that table twice: at the **default 0.88** threshold, the pocket-guides WDW/DLR data stores **would false-merge** (0.895 > 0.88) the moment both appear as objects under the same subject+predicate; the previous report's proposed 0.90 leaves a 0.005 margin on a real pair. Meanwhile every alias pair sits far below any plausible threshold — so trigram cosine can neither cause alias merging (good) nor detect aliases (bad), and coded identifiers are *dangerously similar* to each other (bad).

### 4b. Where it falls short of the goal

1. **No alias/entity-resolution pass exists.** Surface-form variants fragment into parallel nodes and parallel truth slots. Prompt rules can reduce this at extraction time but cannot guarantee it, and nothing repairs fragmentation after the fact at the node level.
2. **Distinct-ID false merges are one config default away.** Identifier-valued objects within one truth slot need a near-exact threshold; the engine has the per-type knob but ships it empty.
3. **Node property whitelist** doesn't include the coordinates a KB needs on entities (`slug`, `system`, `team`) — extractions carrying them would *fail* under `strict_properties=True`.
4. `entity_neighborhood` requires the exact normalized node name (`client.py:3438-3444`) — an alias query ("the gateway") returns nothing even when the canonical node is rich.

### 4c. Implementation plan to close it

**Driver-side (pilot, no engine change):**
- MDX preprocessor emits a per-corpus **canonical-entity table** (seeded from slugs + H1 titles + a hand-curated alias list for the pilot: gateway forms, D-Scribe forms, GCX, data-store IDs).
- Render that table into the Motive's prompt `rules` ("Always name entities canonically: 'Jedai Gateway' — never 'the gateway', 'GW', or the slug form; data-store IDs verbatim, never abbreviated") — prompt profiles accept `rules` today (README `DreamPromptProfile`); budget ~20 lines.
- **Slug-as-entity-coordinate**: pages under `products/jedai-gateway/*` stamp `subject_properties={"system": "jedai-gateway"}` guidance so the entity node accumulates its home slug as a property (searchable via `_node_search_text`, `client.py:5240-5248`, which feeds both seed resolution and candidate text).

**Config-side (pilot):**
- jedai-kb `DreamInstructionSet` with Entity whitelist extended to `("kind", "system", "team", "env", "slug")` (`NodeInstruction.properties`); keep `strict_properties=True` for the local extractor, set `False` only for the LLM transport if key-invention noise shows up (`config.py:305-312`).
- `DedupPolicy(cosine_threshold=0.88, memory_type_thresholds={"identity": 0.97, "state": 0.97, "decision": 0.95})` — **0.97, not 0.95/0.90**, chosen from the measured table: real distinct-ID pairs reach 0.895, so the margin at 0.97 is ~0.075 rather than ~0.005. Verify no legitimate paraphrase pair in the pilot exceeds 0.97 (acceptance test G1-3).
- The `ingest-portal-kb` Motive pins these via its `dedup_threshold` override only if a single value suffices; otherwise rely on per-type policy (Motive override is scalar — `config.py:1528`).

**Engine-side — cross-referencing `.tasks/audit_gap_closure.md`:**
- **T17 (WS-17, cross-prefix duplicate sweep)** helps the *fact* plane: scope-wide near-duplicate detection across different truth_prefixes → receipted merge/link. It would catch "Jedai Gateway requires OAuth2" vs "the gateway requires OAuth2" *as duplicate rows* once embeddings can see it — which needs **T18** (with trigram vectors those two facts sit ~0.5 cosine; no "high threshold" finds them).
- **T18 (WS-17, production embedding transport)** upgrades the *detection signal* for both T17 and the alias registry below.
- **Neither T17 nor anything else in WS-17..WS-22 touches node identity.** This is the one genuinely missing piece, so design it here:

> **WS-A (proposed): entity-alias registry — node-level aliasing with receipts, never destructive merges.**
> - *Detection signal (deterministic first):* candidate same-scope Entity pairs where (i) token-subset / hyphen-slug / initialism match on normalized names ("jedai gateway" ⊇ "gateway"; "jedai-gateway" ↔ "jedai gateway"; "jgw"/"gw" initialisms), or (ii) name-embedding cosine ≥ `alias_threshold` under the T18 transport, or (iii) a driver-supplied alias hint (from the canonical-entity table); **and** co-mention evidence: the two nodes share ≥ k episodes via `MENTIONS` or ≥ m fact-edge neighbors. Signal weights and thresholds pinned in an `AliasPolicy` hashed into the dream-run receipt.
> - *Merge mechanics — aliasing, not merging:* deterministically elect a canonical node (most fact edges, tie-break lexicographic uuid — first-wins election mirroring T16's registry design); write `alias_of=<canonical uuid>` on the alias node, append to `aliases=[...]` on the canonical, add an `ALIAS_OF` edge. **No relationship row is rewritten** — existing fact edges keep their node uuids, so byte replay and graph-state hashes stay valid. Read-side: seed resolution, `entity_neighborhood`, and beam expansion resolve `alias_of` one level and treat the alias cluster as a single frontier node (union of incident edges).
> - *Receipts:* `ENTITY_ALIAS_LINKED` per link with the full detection payload + before/after graph-state hashes; reversible `ENTITY_ALIAS_UNLINKED` through the T14 adjudication surface (T14 is in flight — sequencing dependency, not a blocker).
> - *Determinism:* pure functions over graph state + policy; no LLM required for v1 (LLM adjudication can be a T14-style review queue for sub-threshold candidates).
> - Estimate: ~1–1.5 weeks including tests; independent of the pilot's Phase 0.

**Goal-1 dependency summary:** Phase 0 gets case-folding + prompt canonicalization + safe thresholds (fragmentation reduced, false merges prevented); a **hard pass on the one-node criterion requires WS-A**; T17+T18 additionally repair the fact plane.

### 4d. Acceptance tests (pilot corpus, pass/fail)

- **G1-1 (one node per entity — hard pass needs WS-A; Phase 0 reports the metric):** after full pilot ingest, count Entity nodes in `tenant:jedai-portal-kb` whose revealed `name` matches `(jedai[ -]?gateway|the gateway|gateway( v[0-9])?|jedai gw)` case-insensitively. **Pass = exactly 1 canonical node (or alias cluster) whose union of fact edges + `MENTIONS` spans ≥ 8 distinct `metadata.slug` values.** Phase 0 target: ≤ 2 nodes and the fragmentation index (count of distinct matching nodes) recorded in the eval report.
- **G1-2 (zero false merges — must pass in every phase):** for the 8 known `kb_ds_*` IDs (§1), assert 8 distinct object nodes and 8 distinct fact rows; for every data-store fact, every evidence episode body (via `memory_evidence`) contains the row's exact ID string — i.e., no row's `episode_uuids` mixes WDW/DLR/Aulani variants. The `..._wdw_...`/`..._dlr_...` pocket-guides pair (measured 0.895) is the canary.
- **G1-3 (threshold sanity):** compute pairwise object cosines within each pilot truth_prefix; assert no pair ≥ the configured per-type threshold is a semantically *distinct* pair (manual review of the ≤ ~20 pairs above 0.9), and at least one known paraphrase pair still reinforces (`semantic_dedup_rate > 0` on `memory_evolution`).
- **G1-4 (alias read path, WS-A):** `entity_neighborhood(scope, "the gateway")` returns the same edge set as `entity_neighborhood(scope, "Jedai Gateway")` once `ALIAS_OF` resolution lands; Phase 0 expected-fail, recorded.

---

## 5. Goal 2 — Concept matching: THEME clusters and cross-page adjacency

### 5a. What today's code actually does

**Clustering.** Consolidation runs a greedy agglomerative pass per scope and depth (`dreaming.py:1719-1743`): seed with the first unclustered fact, add any member whose **full-fact embedding** cosine ≥ `cluster_threshold` (default **0.75**, `config.py:820`); clusters below `min_cluster_size` (**3**, `config.py:821`) are dropped (`dreaming.py:1710,1750-1752`); recursion to `max_depth` (**2**, `config.py:822`) clusters depth-1 themes into depth-2 themes (`dreaming.py:1693-1702`). Clusters must agree on assertion/authority/polarity signatures (`_theme_cluster_is_consistent`, `dreaming.py:1758`, body at `2150+`).

**THEME materialization.** Each qualifying cluster synthesizes one THEME relationship: subject `Theme (<scope_id>)`, object = the label, fact "`Theme (…) summarizes <label>`" (`dreaming.py:1789-1791`), truth slot `scope:Theme (…):summarizes[:label]` (`dreaming.py:1910-1911`), confidence = max over members (`dreaming.py:1939-1941`), members demoted from working context via `active_in_context=False` + `summarized_by=<theme uuid>` while the theme records `derived_from` lineage. Rebuilds are dependency-tracked and materiality-gated (`config.py:823-830`).

**The label is a token bag.** `_synthesize_theme_label` (`dreaming.py:2104-2149`) collects fact tokens, drops 14 stop-words, keeps tokens appearing in ≥ half the members, sorts by frequency, joins the top 6, truncates to 80 chars. The LLM seam is explicit — `ConsolidationSynthesisProfile.model_identifier` exists and is unconsumed (`config.py:1624-1652`), with the swap point documented in-line (`dreaming.py:1783-1785`).

**Read side.** Stage-4 expansion propagates relevance THEME→member (`derived_from`, weight 0.8) and member→THEME (`summarized_by`, weight 0.7) (`client.py:3184-3220`; weights `config.py:1465-1467`), so demoted members surface when their theme matches, and a matching member pulls its theme in. Results arriving this way are labeled `origin="theme_member"` / `"member_theme"` (`models.py:394-396`).

**Vector reality check** (measured, committed transport): `"special offers ingestion pipeline"` vs `"D-Scribe special offers are ingested into Vertex AI Search data stores"` → **0.453** (below the 0.75 cluster floor; above the 0.35 stage-2 vector floor, `config.py:1449`, so retrieval sees it lexically/vectorially). Vs `"how promotional discounts flow into the knowledgebase"` (zero shared vocabulary) → **0.168**: invisible to vectors *and* lexical overlap. Trigram clustering is **vocabulary clustering**; on a docs corpus with consistent terminology that's partially effective, on paraphrase it is measurably blind.

### 5b. Where it falls short of the goal

1. **Synonymy under-connects clusters.** At 0.75 on trigram vectors, only near-verbatim cross-page repetition clusters. Concept clusters spanning "special offers" ↔ "promotional discounts" ↔ "offer eligibility" will not form.
2. **Token-bag labels are not concept names.** A cluster over the special-offers pages labels itself something like `offers special data store dscribe wdw` — unranked evidence, not a summary; useless as profile context and mediocre as a retrieval landing point. (The theme's stored *vector* is the member-embedding centroid — `dreaming.py:1861-1868` — which retrieves fine; the *text* is what fails as context.)
3. **Cross-page adjacency without a THEME depends on shared entity nodes** — which is Goal 1: fragmented gateway nodes mean the "authenticate to the gateway" query can't hop from the gateway node to the OAuth facts written against "the gateway".
4. **No global query mode** over theme summaries (GraphRAG global search analog). Out of pilot scope; note it in any SOTA claim.

### 5c. Implementation plan to close it

**Driver-side:** the link-rewriting preprocessor (`label (→ /path/)`) deliberately injects target-page titles into section text — cheap lexical bridging that raises cross-page cosine for linked concepts. Keep section headings in episode text (H2 text carries concept vocabulary). Frontmatter `tags` go into episode metadata (inherited onto facts for evidence/filtering) — note they are **not** searchable text today (`_retrieval_searchable` covers subject/predicate/object/fact + node properties only, `client.py:3322-3343`); putting tag vocabulary into node `subject_properties` (whitelisted, §4c) is the supported way to make it retrievable.

**Config-side:** add the consolidation job the default config lacks (`default_config` ships formation+pruning only, `config.py:2448-2452`): `DreamJob(kind=CONSOLIDATION, thematic_consolidation_policy=ThematicConsolidationPolicy(cluster_threshold=0.75, min_cluster_size=3, max_depth=2))`, either Motive-less or with `theme` added to the ingest Motive's `allowed_memory_types` (correction §2.2). Pilot A/B: `cluster_threshold` 0.75 vs 0.70 — at trigram fidelity, 0.70 risks vocabulary-coincidence clusters; measure precision on the pilot rather than guessing.

**Engine-side (cross-referenced, with hard dependencies):**
- **T18 (WS-17, production embedding transport) — HARD DEPENDENCY for real concept clustering.** Tenant-configurable OpenAI-compatible embeddings; `embedding_identifier` switches; mixed-space cosine refused by the identifier guard. Consequence for the pilot: after T18, stored trigram vectors cannot be compared against new-space vectors — **plan a full re-ingest** (tenant purge preserves raw episodes; re-dream re-embeds) rather than an in-place backfill.
- **T19 (WS-18, LLM theme synthesis) — HARD DEPENDENCY for concept-level labels.** Async LLM ≤2-sentence faithful summary, deterministic entailment gate, receipted rejection falls back to today's deterministic label. Directly the GraphRAG community-summary analog on the existing seam.
- **T16 (WS-17, predicate canonicalization)** helps adjacency indirectly: paraphrased predicates stop splitting slots, so clusters see one row per fact instead of near-duplicate slots diluting `min_cluster_size`.
- Nothing new needed beyond WS-17/WS-18 for this goal: the clustering machinery, dependency tracking, two-tier read, and expansion weights are already built and tested.

**What is achievable before T18/T19 land:** vocabulary-level themes across pages that repeat terminology (the knowledgebase pages do), theme↔member retrieval expansion, and honest measurement of the paraphrase blind spot (G2-2 below is the T18 before/after probe).

### 5d. Acceptance tests

- **G2-1 (cross-page theme forms):** after pilot ingest + consolidation, ≥ 1 active THEME whose `derived_from` members span ≥ 3 distinct `metadata.slug` values, with ≥ 1 theme whose member vocabulary intersects {"offers", "data store", "dscribe"}. Verify via `export_graph` + `derived_from` lineage.
- **G2-2 (concept query, expansion-attributed):** `search(query="special offers ingestion pipeline", limit=10)` returns facts from ≥ 2 distinct slugs with ≥ 1 result whose `origin ∈ {theme_member, member_theme, entity_hop}` (`SearchResult.origin`, `models.py:394-396`). **Phase 0: expected pass** (shared vocabulary). Then re-run with the paraphrase probe `"how promotional discounts flow into the knowledgebase"`: **Phase 0: expected fail (document it); Phase 1 (T18): pass** with ≥ 2 relevant facts from ≥ 2 slugs. This pair is the measured 0.453/0.168 contrast from §5a.
- **G2-3 (label quality gate, T19):** Phase 0: every active THEME label is non-empty, ≤ 80 chars (`dreaming.py:2148`) — mechanical only. Phase 1 (T19): every THEME object is a ≤ 2-sentence summary that passes the entailment validation gate, with zero hallucinated entities (spot-check 10 themes against `derived_from` member facts).
- **G2-4 (demotion economics):** `memory_evolution` shows `theme_relationship_count ≥ 1`, `demoted_relationship_count ≥ 3·theme_relationship_count` floor not required — but `context_visible_relationship_count` must be strictly lower than total active facts, and the demoted members must remain reachable through G2-2's theme_member path.

---

## 6. Goal 3 — Graph-native wins: multi-hop joins, temporal supersession, evidence precision

### 6a. What today's code actually does

**Multi-hop.** Stage 3 resolves seed entity nodes by lexical/vector match on node name + whitelisted properties (`client.py:3251-3295`); stage 4 runs a bounded beam (defaults: `max_hops=2`, `beam_width=16`, `max_seed_nodes=8`, `entity_edge_weight=0.6`; `config.py:1455-1463`) over fact edges incident to frontier nodes (`client.py:3155-3182`, index-backed and `MENTIONS`-stripped via `graph.py:406-436`), interleaved with THEME↔member links, every expanded candidate re-passing visibility (stage 5, `client.py:3223-3238`). Concretely for the auth question: the query token "gateway" gives the `jedai gateway` node a nonzero lexical overlap, so it seeds the beam, and hop 1 reaches every OAuth/endpoint fact written against that node even though `"authenticate to the gateway"` vs `"OAuth2 client credentials token endpoint"` is 0.183 cosine — **this is the graph paying rent**, and it is attributable: those results arrive `origin="entity_hop", hops=1`.
`entity_neighborhood` (`client.py:3403-3470+`) is the direct join surface but is **one-hop and exact-normalized-name only** — it has no `hops` parameter (the previous report's "hops≤2" phrasing was aspirational; corrected). Two-hop joins today = chain neighborhood calls driver-side, or read `origin`/`hops` off search results.

**Temporal supersession.** Single-active slots: a new object value passes the supersession gate (authority ranks → temporary-severity → WS-16 corroboration, single gate at `dreaming.py:480-529`), the incumbent's `valid_to` closes at the challenger's `valid_from` with `superseded_by_relationship_uuid` lineage (invalidate-don't-delete, `dreaming.py:3341-3346`); `as_of` search reconstructs the old truth; backfilled older facts slot in as historical without displacing newer truth. WS-16 (landed at `33336e5`): equal-authority challengers against a ≥`corroboration_margin`-corroborated incumbent park as `insufficient_corroboration` (`dreaming.py:518-528`) until `corroboration_required` distinct observations accumulate (`dreaming.py:4326-4380`), each park discounting the incumbent once, receipted (`dreaming.py:4560-4635`). §2.3 explains why the KB tenant should set `corroboration_required=1`.

**Evidence precision.** Every fact row inherits episode metadata (`{**episode.metadata, **memory.metadata}`, `dreaming.py:3831`) — so `slug`, `anchor`, `content_sha256`, `authorship` ride on the fact and `memory_evidence` resolves to the exact section episodes. The `research-temporal@v2` profile additionally requires a verbatim `source_text` quote per fact with a 0.80 confidence floor (README prompt-profile table), giving quote-level precision inside the section.

### 6b. Where it falls short of the goal

1. **Join reach is hostage to Goal 1.** Beam expansion pivots on shared entity nodes; a fragmented gateway node splits the neighborhood, so the auth query reaches only the facts written against whichever surface form the extractor happened to use.
2. **Predicate paraphrase splits truth slots.** `truth_key` embeds the raw predicate string (`dreaming.py:194-198`). If the June extraction says `requires` and the September re-extraction of the *edited* section says `must provide`, the edit lands in a *new* slot: **no supersession fires**, both "truths" stay active, and `as_of` semantics silently degrade. This is the single biggest robustness risk for edit-and-resync.
3. **Parked corrections need an operator surface.** Until T14, `requires_operator_review` rows are visible only via graph/export inspection.
4. `entity_neighborhood` lacks `hops` (minor; see 6c).

### 6c. Implementation plan to close it

**Driver-side:** pin the extraction prompt hard on predicate stability ("reuse the exact predicate from Existing graph context when restating a known fact" — context injection is same-scope and already in the prompt, README extractor-prompt paragraph); sync driver stamps monotonic `reference_time` from git commits so valid-time ordering is right by construction; deletion path enumerates a section's facts by inherited `metadata.slug`/`shadow_custom_id` and calls `forget_memory` per row (soft-retire preserves evidence + `as_of`).

**Config-side:** `SupersessionPolicy(corroboration_required=1)` for this tenant (§2.3); keep `_verified_source_authority: "operator"` on all sync episodes so doc truth outranks agent chatter if scopes are ever mixed; `RetrievalPolicy` defaults are sane for the pilot (`max_hops=2`, `beam_width=16`) — raise `beam_width` to 32 only if G3-1 shows frontier starvation on hub nodes.

**Engine-side (cross-referenced):**
- **T16 (WS-17, predicate canonicalization) — HARD DEPENDENCY for paraphrased-correction supersession.** Canonical predicate registry (cosine-mapped, first-wins, receipted), `truth_key` built on the canonical, receipted backfill for existing rows. Directly closes 6b.2; its backfill also repairs any split slots the pilot creates before it lands.
- **T17 (WS-17, cross-prefix sweep)** is the safety net for whatever T16's threshold misses (and for subject-form splits pending WS-A): receipted `duplicate_of` link + demotion, never silent.
- **T14 (WS-16, adjudication APIs — in flight)** gives the operator queue for parked `insufficient_corroboration` rows; the KB pilot with `corroboration_required=1` barely parks anything, so T14 is a nice-to-have here, required for agent-facing tenants.
- **Small new item (no WS covers it):** `hops: int = 1` parameter on `entity_neighborhood` reusing the stage-4 frontier walk (~1 day incl. tests). Driver chaining is the interim.

### 6d. Acceptance tests

- **G3-1 (two-hop join):** `search(query="which Vertex data stores back DLR special offers and what pipeline populates them", limit=12)` returns, in one result set: ≥ 1 fact whose object is a `kb_ds_*_dlr_*` ID, ≥ 1 D-Scribe/pipeline fact, from ≥ 2 distinct slugs, with ≥ 1 result `origin="entity_hop"`. Driver-chained neighborhood variant: `entity_neighborhood("D-Scribe")` → collect neighbor entities → neighborhoods of those → the union reaches the GCX team fact, the D-Scribe path/pipeline fact, and ≥ 2 distinct data-store ID facts from different pages within 2 chained hops.
- **G3-2 (auth join with expansion attribution):** `search(query="how do I authenticate to the gateway", limit=10)` returns facts from ≥ 3 distinct slugs with ≥ 1 arriving via `origin ∈ {entity_hop, theme_member, member_theme}`. (Pass depends measurably on Goal-1 canonicalization — run it before and after enabling the prompt alias rules to quantify.)
- **G3-3 (edit-and-resync temporal):** synthetically edit one requirement-bearing section in `platform-decision-log` (change a decision's status/value), commit, run one sync cycle. Assert: old row `superseded` with `superseded_by_relationship_uuid` set; `search(..., as_of=<pre-edit commit time>)` returns the old value; current search returns the new; `truth_timeline` shows both entries in order. **Must pass within one sync cycle** with the §2.3 config even when the old fact was corroborated on ≥ 3 pages. Variant B (predicate paraphrase forced in the edit prompt): Phase 0 expected-fail — document the split slot; Phase 1 (T16) must pass.
- **G3-4 (evidence precision):** for a random 20-fact sample, `memory_evidence` episodes carry `metadata.slug#anchor` of the section that actually states the fact (≥ 18/20 on manual check), and 100% of LLM-extracted facts carry a `source_text` quote found verbatim in the cited section body (programmatic check; `research-temporal@v2` contract).
- **G3-5 (deletion):** delete one section, sync: its facts are excluded from current retrieval, still reachable via `as_of` and `memory_evidence` (forget_memory soft-retire semantics).

---

## 7. Consolidated implementation plan

### Phase 0 — pilot on today's engine (driver + config only)

| Item | Estimate |
|---|---|
| MDX preprocessor (frontmatter/imports strip, Starlight unwrap, link rewrite, H2 split, canonical-entity table emit) | 1.5 days (~200 LoC) |
| Sync driver (git walk, sha256 manifest, add_context/forget_memory, OpenAPI deterministic emitter) | 1.5 days (~200 LoC) |
| jedai-kb config: instruction set (extended Entity whitelist), Motive (+`theme` in allowed types), consolidation job, `DedupPolicy` per-type 0.97/0.95, `SupersessionPolicy(corroboration_required=1)`, two read-only scopes | 1 day |
| Pilot ingest (77 files → ~206–230 episodes) + G1-2/G1-3/G2-1/G2-2(a)/G3-1..G3-5 runs | 1 day, **~$2–3** extraction (haiku-class) |

**Go/no-go 1** (end of Phase 0): G1-2 zero false merges is a hard gate (a false merge on the data-store family kills trust in the KB); G3-3 temporal must pass; G1-1 fragmentation index and G2-2 paraphrase fail are *measurements*, not gates — they parameterize Phase 1 expectations. If triple-ized extraction of dense tables under-performs plain document search on the Band-1 questions (§ Phase 2) by a wide margin, stop: the thesis fails for $3.

### Phase 1 — after WS-17 (T16/T17/T18) and WS-18 (T19) land, plus WS-A

- **What re-runs:** full re-ingest of the pilot (tenant purge preserves raw episodes; re-dream re-extracts and re-embeds them) because T18's identifier guard correctly refuses mixed-space cosine. No re-crawl of the portal is needed, but re-extraction is a full LLM pass again (~$2–3 pilot, ~$5–10 corpus); theme synthesis under T19 adds one LLM call per cluster (~tens of calls, cents).
- **What improves measurably:** G2-2(b) paraphrase probe flips to pass (T18); G2-3 labels become entailment-gated summaries (T19); G3-3 variant B flips to pass (T16); G1-1 hard pass with WS-A (alias registry, ~1–1.5 weeks, the only substantial net-new engine build this plan requires — the `entity_neighborhood` hops param from §6c is a ~1-day rider); T17 sweep count reported as a repair metric.
- **Sequencing note:** T14 (adjudication APIs) is in flight and lands first (same-file conflicts are why it runs sequentially — see `.tasks/audit_gap_closure.md` WS-16); WS-A should reuse its review-queue surface for sub-threshold alias candidates.

**Go/no-go 2:** G1-1 = exactly one gateway node/cluster; G2-2(b) pass; G3-3(B) pass. All three are binary.

### Phase 2 — evaluation harness + 40-question gold set (~3–4 days)

Three bands, unchanged from the original design, now with per-goal attribution:

1. **Single-fact lookups** (~20 q): parity with the portal's own Pagefind document search is the pass bar, measured at a 2,000-token injected-context budget.
2. **Cross-page joins** (~12 q, e.g. "which Vertex data stores back DLR special offers and what pipeline populates them?"): pass = correct multi-page answer with ≥ 1 `origin`-attributed expansion result, at a fraction of the tokens a document-search stuffing baseline needs.
3. **Temporal** (~8 q, after synthetic edit-and-resync): pass = current answer + correct `as_of` historical answer + correct `memory_evidence` slugs.

Score answer accuracy, evidence precision (does `memory_evidence` land on the right `slug#anchor`), and injected-token cost vs. Pagefind. **Overall pass = parity on Band 1, clear win on Bands 2–3 at lower token cost.** If it holds, the full corpus is a **$5–10** ingest (~820–1,000 extraction calls haiku-class; $25–50 Sonnet-class) plus incremental syncs that cost only changed pages; expected ~2.5–4k active facts corpus-wide; SQLite handles it (15–40 MB with vectors), retrieval stays in milliseconds.

### Risks table (updated)

| Risk | Mitigation | Status |
|---|---|---|
| Imperative doc prose forms directives | untrusted-directive gate + `trusted=False` for low-`authorship` pages, or drop DIRECTIVE in v1 | wired (`dreaming.py:452,1197`) |
| RBAC leakage of the 60 gated pages | two-scope split mirroring portal RBAC; `read_only_scopes` on consumers | wired (`config.py:2301`) |
| Extraction hallucination | `research-temporal@v2` verbatim-quote requirement + G3-4 programmatic quote check | wired |
| Entity fragmentation | prompt canonicalization now; **WS-A for the guarantee** | §4c — the honest gap |
| Distinct-ID false merges | per-type dedup **0.97** (measured margin) + G1-2 canary | config, Phase 0 |
| Doc edit parked by corroboration | `corroboration_required=1` on this tenant | config, Phase 0 (§2.3) |
| Predicate paraphrase splits truth | prompt predicate-stability rule now; **T16 for the guarantee** | §6c |

## 8. Corrections ledger (vs. the previous version of this report)

1. Dedup numbers ("0.90 / identity 0.95") were presented as if defaults; the engine default is flat 0.88 with an **empty** per-type map (`config.py:776-777`) — and 0.90/0.95 are **insufficient** against measured distinct-ID cosines (0.895): use 0.97 for id-bearing types.
2. "Entity fragmentation handled via prompt rules + conservative 0.95 identity dedup" conflated two planes: the dedup threshold governs fact-object merging inside one truth slot; **node identity never consults embeddings** (`graph.py:55-56`, `dreaming.py:158-176`). Prompt rules are today's only fragmentation defense; WS-A is the fix.
3. The encyclopedic Motive as specified would have **gated thematic consolidation off** (`dreaming.py:1573-1618`); add `theme` to its allowed types or unpin the consolidation job.
4. The WS-16 sync interplay: incremental syncs don't inflate `observed_count`; cross-page repetition does. The precise override is `corroboration_required=1`, not "raise the margin".
5. `entity_neighborhood` is one-hop, exact-name (`client.py:3403-3444`); multi-hop joins come from search stages 3–4 (or driver chaining). Acceptance tests rephrased accordingly.
6. Corpus size 2.39 MB actual (was "2.5 MB"); OpenAPI 346 method entries / 344 operationIds (was "344 operations").
7. "Feasible with today's code as-is — no engine changes" stands **for feasibility**; for the three goals of this rewrite it does not: Goal 1's hard pass needs WS-A, Goal 2's quality hard-depends on T18+T19, Goal 3's edit robustness hard-depends on T16.

## 9. Tenant vocabulary extension contract (WS-27 T6)

`ingest/kb_config.py`'s `NODE_INSTRUCTIONS` / `RELATIONSHIP_INSTRUCTIONS` (§4a) are this pilot's **worked example** of a tenant vocabulary — every other Memotron-backed project follows the same shape. This section is the contract for extending it, since a second tenant will want its own labels without touching the engine.

**The asymmetry stays fixed.** Entity **labels** are a closed vocabulary (`DreamInstructionSet.allowed_labels`, derived from `node_instructions`); relation **predicates** are open free text. A tenant adds node types and relationship types; it never closes the predicate vocabulary — retrieval ANN-matches the embedded fact *sentence*, not the predicate label, so closing predicates only rejects valid verbs for no retrieval benefit (`config.py`, `RelationshipInstruction.open_predicate`'s docstring).

**Adding a label.** Append a `NodeInstruction(label=..., query=..., properties=(...))` to the tenant's `node_instructions` tuple. Three governance knobs, all opt-in and additive (existing tenants are unaffected until they set them):

| Field | Effect |
|---|---|
| `strict_properties` (default `True`) | Unknown property keys reject (`False`: silently stripped instead — for a noisier LLM transport) |
| `require_description` (default `False`, WS-27 T1) | An entity with no non-blank `description` is quarantined (`entity_description_missing`), never a silent, indistinguishable node |
| `required_properties` (default `()`, WS-27 T2) | Keys that MUST be set, non-blank, on top of `properties`'s allow-list — e.g. this pilot's `Credential.required_properties=("reference",)`. Validated at config-construction time: a key not already in `properties` (or a universal key) fails fast, before any candidate is ever extracted |

Every label may set `description` and `aliases` regardless of its own `properties` list (`UNIVERSAL_NODE_PROPERTIES`) — `aliases` (WS-27 T1) is a list of alternate surface names for ONE entity ("GCX" for "guest content experience"); the engine writes each into the scope's `entity_canon` registry at materialization time, the SAME registry the WS-17 entity-resolution engine reads, so a later mention under any registered alias resolves to the one canonical node.

**Adding a relationship type.** Append a `RelationshipInstruction(type=..., source_label=..., target_label=..., query=..., cardinality=..., memory_type=...)`. `source_label`/`target_label` name a label from this SAME instruction set's `node_instructions`, or `"*"` (any allowed label — the open-predicate wildcard). **Validation rejects an endpoint label that is not `"*"` and not in `allowed_labels`, at construction** (`DreamInstructionSet.validate_instruction_set`, `config.py`) — a typo'd endpoint label fails the moment the config is built, not as a silent, unexplained rejection of every candidate of that type once a real ingest runs.

**The catch-all guardrail (WS-27 T4).** A tenant's vocabulary almost always needs one general/miscellaneous label (this pilot's `Concept`, skos:Concept — "units of thought"). `MemoryHealthPolicy.general_labels` (default `("Concept",)`) names which label(s) count toward the guardrail; `max_general_label_share_block`/`_warn` (default block at 40%) caps the share of context-visible entities that label may hold once the scope is large enough to measure (`MemoryHealthPolicy.min_instances`). Override `general_labels` if a tenant's catch-all is named something else, or has more than one.

**Worked example.** `ingest/kb_config.py`'s 13 labels ARE the worked example this contract describes: every label sets `require_description=True`; `Credential` sets `required_properties=("reference",)` so a credential entity structurally cannot materialize with a bare secret value instead of a pointer to one; `Concept` is the sole `general_labels` member and its guard is stated twice — once in the label's own `query` (governance metadata) and once in the authored `KB_EXTRACTION_PROMPT` ("CHOOSE THE MOST SPECIFIC LABEL... Concept is the LAST RESORT"), because only the authored prompt text is what the model actually reads (`DreamInstructionSet.render_prompt`'s docstring: `query` is validation-time selection criteria, never extraction prose).
