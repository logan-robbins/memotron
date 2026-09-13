# JedAI portal knowledge-base ingestion pilot — operator runbook

This kit implements **Phase 0 of `INGEST.md`**: ingest the JedAI documentation
portal into a dedicated Memotron graph and measure whether the result delivers
the three goals INGEST.md is judged on — entity matching, concept matching, and
graph-native wins (multi-hop, temporal truth, evidence precision).

Everything here runs against the **JedAI Gateway**. There is no hermetic or
rule-based fallback: extraction, dream-agent decisions, and theme synthesis all
call `claude-haiku-4-5` over `/chat/completions`, and every vector comes from
`text-embedding-3` (3072 dims). If the gateway is unreachable, the pilot fails
loudly rather than quietly producing deterministic output.

---

## 1. Prerequisites

| Requirement | Check |
|---|---|
| Python 3.12 + `uv` | `uv --version` |
| Gateway virtual key in `.env` as `LITELLM_API_KEY` | `grep -c LITELLM_API_KEY .env` |
| Portal checkout at `/Users/logan.robbins/jedai/portal` | `ls /Users/logan.robbins/jedai/portal/src/content/docs` |
| Portal is a git repo (supplies `reference_time`) | `git -C /Users/logan.robbins/jedai/portal log -1` |

The key is read **by name only**. No command in this kit prints, echoes, or
copies a key value; failures name the variable and nothing else.

```bash
set -a && . ./.env && set +a      # export LITELLM_API_KEY for the session
```

Gateway keys are per-cluster. The packaged base URL targets `preview`
(Integration); a key minted there 401s against `latest`/`stage`/`prod`. Override
with `LITELLM_API_BASE`.

### Isolation

The pilot owns its own graph and tenant and **never** opens
`.memotron/spymaster.sqlite`. Every entry point calls
`kb_config.assert_isolation()` before opening a store, which fails hard if the
resolved path is the protected graph or escapes `ingest/`. Each run prints:

```
  graph      .../ingest/.memotron/portal-kb.sqlite
  protected  .../.memotron/spymaster.sqlite (never opened)
  ASSERT OK  pilot graph != spymaster graph
```

`ingest/.memotron/` and `ingest/.manifest.json` are covered by the repo's
existing `.memotron/` gitignore rule — the pilot graph is never committed.

---

## 2. Files

| File | Purpose |
|---|---|
| `preprocess.py` | MDX → episode records: frontmatter, import stripping, Starlight unwrapping, link rewriting, fence-aware H2/H3 section splitting, `content_sha256`, git `reference_time`. Pure functions; `--stats` prints the distribution. |
| `kb_config.py` | All pilot policy in one place: tenant, scopes, instruction set, Motive, dedup/supersession/consolidation/entity-resolution policy, and the four gateway transports. `--show` prints the effective policy. |
| `sync.py` | The driver: preprocess → sha256 manifest diff → `add_context` for new/changed only → dream → report. `--dry-run`, `--limit`, `--only`, `--scope`, `--reset`. |
| `evaluate.py` | Three-band gold-set harness with a naive whole-page-stuffing baseline and a token-cost scorecard. |
| `verify_goals.py` | INGEST.md's per-goal acceptance tests (G1-1…G3-5), including a real edit-and-resync. |
| `goldset.yaml` | Gold questions written against the actual pilot MDX, each citing `file:line`. |
| `actionability_goldset.yaml` | **The accept/reject standard.** Hand-authored from the portal MDX: which facts an agent must remember, which it must not, and the arguable edge cases — each with `file:line` and a verbatim quote. See §11. |
| `score_actionability.py` | Scores one graph against `actionability_goldset.yaml`: recall / precision / F1 on must-remember, false-store rate on must-not, noise ratio, and miss-vs-quarantined diagnosis. Standalone — imports no `memotron` code. |
| `.manifest.json` | Generated. `slug#anchor → content_sha256`, the incremental-sync ledger. |

---

## 3. Command sequence

```bash
set -a && . ./.env && set +a

# 0. Inspect the corpus before paying for anything
uv run ingest/preprocess.py --stats
uv run ingest/kb_config.py --show

# 1. Plan and price the run — zero gateway calls
uv run ingest/sync.py --dry-run

# 2. Cheap first slice (this is the run whose numbers are reported in §5)
uv run ingest/sync.py \
  --only dscribe/special-offers.mdx --only plandisney/pocket-guides.mdx \
  --only knowledgebase/sources/index.mdx --only platform-decision-log/approved.mdx \
  --only dscribe/index.mdx --only jedai-gateway/index.mdx

# 3. Score it
#    3a. The accept/reject gate — is the memory actionable? (§11)
uv run ingest/score_actionability.py --verify-citations      # gold set still matches the portal
uv run ingest/score_actionability.py --graph portal-kb --show all
#    3b. Retrieval-quality and goal harnesses
uv run ingest/evaluate.py
uv run ingest/verify_goals.py

# 4. Full 77-file pilot when the slice looks right
uv run ingest/sync.py

# Re-run incrementally: unchanged sections cost nothing
uv run ingest/sync.py

# Start over
uv run ingest/sync.py --reset
```

`verify_goals.py` **writes** to the graph (G3-3 re-ingests an edited section,
G3-5 retires one fact as a probe). Pass `--skip-mutating` for a read-only pass,
or `--reset` afterwards to return to a clean state.

---

## 4. How the preprocessor splits pages — two measured deviations from INGEST.md

INGEST.md §2 specifies "one episode = one H2-bounded section". Two corrections
were forced by the actual corpus:

**4.1 Heading detection must be fence-aware.**
`sources/dscribe/special-offers.mdx` embeds a ```` ```md ```` sample whose body
contains ten `## ` lines (`## Display Name`, `## Bookable Dates`, …). A naive
`^## ` scan splits on those, producing ten junk episodes and destroying the real
section. INGEST.md's corpus-wide figure of **1,561 H2 sections** was almost
certainly produced by such a scan and is therefore an over-count. All heading
scans in `preprocess.py` skip fenced regions.

**4.2 Pages with no H2 fall back to H3.**
**15 of the 77 pilot files have zero fence-free H2 headings.** The most important
of them is `solution_engineering/platform-decision-log/approved.mdx`, which
carries decisions **D001–D011 entirely as H3**. Pure H2 splitting collapses that
13 KB page into a single section, which `add_context` then blind-chunks at 4000
chars — destroying per-decision `slug#anchor` evidence and making INGEST.md's own
G3-3 test ("edit one requirement-bearing section in `platform-decision-log`")
untestable. When a page has no H2, the splitter bounds on H3 instead and records
`heading_level` on every section so the choice stays auditable.

Sections under 300 chars fold into the page preamble with their heading kept as a
bold label, so no text is lost and no sub-300-char episode is paid for.

**4.3 The pilot slice has no RBAC-gated pages.**
INGEST.md §2 calls for "two scopes mirroring the portal's RBAC: external docs vs
the `visibility: jedai` gated pages". Both scopes are built and the routing rule
(`kb_config.scope_for`) is implemented against frontmatter — but **all 60
`visibility: jedai` pages live outside the pilot subtrees** (under `backstage/`,
`internal/`, `platform/authentication`, `products/jedai-mcps`,
`solution_engineering/mcp-platform`). The pilot slice also carries no
`authorship:` or `audience:` frontmatter; those fields exist only on the same 64
out-of-slice pages. Widening the slice needs no code change.

---

## 5. Measured results — the limited run

Command: the `--only` invocation in §3 step 2. Six pages chosen to exercise all
three goals: the data-store false-merge gauntlet (special-offers, pocket-guides),
the cross-page join hub (sources/index, dscribe/index), the entity-fragmentation
gauntlet (jedai-gateway/index), and the temporal band (platform-decision-log).

### Corpus and plan

| | Full pilot slice | Limited run |
|---|---|---|
| files | 77 | 6 |
| sections | 265 | 34 |
| episodes (at 4000 chars) | 340 | 37 |
| estimated cost | **$1.97** | **$0.21** |

INGEST.md §1 estimated **206–230 episodes** for the 77 files; the fence-aware,
H3-fallback preprocessor produces **340** — about 48% more, because per-decision
H3 sections and page preambles both become their own episodes. INGEST.md §7
estimated **$2–3** extraction for the pilot; the measured estimate is **$1.97**,
inside that band despite the higher episode count (the extra episodes are small).

### 5.1 Actual run — measured, 34 sections / 37 episodes

Everything below is from one real end-to-end run against the JedAI Gateway
(`claude-haiku-4-5` + `text-embedding-3`). Vectors verified as
`openai-embeddings:text-embedding-3@v1`, 3072 dims — not the hermetic fallback.

**Formation**

| | |
|---|---|
| episodes queued / processed / pending | 37 / **37** / **0** |
| candidates extracted | 250 |
| facts created | 162 |
| reinforced / superseded | 3 / 2 |
| dropped by salience (`min_salience=0.20`) | 85 (34% of candidates) |
| rejected by schema | **3** |
| formation wall time | 1,184 s (~32 s/episode) |

**Graph**

| | |
|---|---|
| Entity nodes | 191 |
| active facts | 164 |
| THEME facts | 5 |
| demoted into themes | 27 |
| context-visible / active | 137 / 164 |
| compression ratio | 0.270 |
| tokens raw episodes → rendered profile | 12,738 → 842 (**93% saved**) |
| type distribution | identity 121, requirement 8, state 3, decision 1, theme 5 |

**Candidate rejections, by machine violation code** (`CANDIDATE_SCHEMA_REJECTED`)

| code | count |
|---|---|
| `predicate_not_a_verb_phrase` | 2 |
| `property_key_reserved` | 1 |

An earlier run of the *same* corpus with `strict_properties=True` produced **42**
rejections (36 `property_key_not_allowed` + 6 `predicate_not_a_verb_phrase`),
concentrated in the decision-log tables. See §8.

**Other receipted engine activity** — evidence that the post-INGEST.md workstreams
are live: `formation_predicate_canonicalized` 1 (T16),
`consolidation_cross_prefix_duplicate_demoted` 3 (T17),
`formation_semantic_dedup_reinforced` 3, `formation_entity_linked` 1 (alias
registry), `consolidation_cluster_summarized` 5 and
`consolidation_theme_synthesis_rejected` 4 (T19 with its entailment gate
rejecting 4 of 9 candidate summaries and falling back to deterministic labels).

**Cost.** Estimated $0.21 for the slice; total wall time 1,245 s. A re-run with
no content change: **34 unchanged, 0 episodes, $0.00** — the manifest path works.

**Throughput, and why it matters for the full pilot.** Three runs of the same
34 sections, same gateway:

| run | config | rate |
|---|---|---|
| 1 | engine defaults | ~1.7 facts/min, 13 of 37 episodes in 29 min |
| 2 | `max_pair_scan=8` | ~3 facts/min — then **died** on a gateway `RemoteDisconnected` (§9.2) |
| 3 | `ResilientEmbeddingTransport` (retry + memo), `max_pair_scan` back to 512 | **37/37 episodes in 20 min**, no failures |

The memo is the whole difference; see §9.1.

### 5.2 Evaluation scorecard

Baseline = IDF-ranked whole-page stuffing over **the same six ingested pages**.

| band | answered | graph tokens | baseline tokens | ratio |
|---|---|---|---|---|
| 1 — single-fact | **8 / 12** | 2,128 | 38,382 | **5.5%** |
| 2 — cross-page join | **2 / 6** | 1,108 | 42,194 | **2.6%** |
| 2b — paraphrase probe | **2 / 2** | 361 | 7,814 | 4.6% |

The token result is unambiguous: the graph answers at **3–6% of the tokens** a
page-stuffing baseline needs. The recall result is not — Band 1 at 8/12 is *not*
parity with document search, and Band 2 at 2/6 does not clear INGEST.md's bar.

**Band 2b is the headline positive.** Both zero-shared-vocabulary paraphrase
probes — including INGEST.md §5a's verbatim probe, measured at **0.168** cosine
under the trigram transport — now return the right facts. INGEST.md predicted
"Phase 0: expected fail; Phase 1 (T18): pass". T18 has landed, and it passes.

### 5.3 Goal acceptance

| test | result | note |
|---|---|---|
| G1-1 fragmentation index | **1 node** | exactly one `Jedai Gateway` entity node; the 7-way surface-form gauntlet collapsed |
| **G1-2 zero false merges** | **PASS (gate)** | 5 distinct `kb_ds_*` IDs, **0 merge collisions** |
| G1-3 threshold sanity | measure | `semantic_dedup_rate` 0.018, 3 reinforcements |
| G1-4 alias read path | measure | `entity_neighborhood("the gateway")` → 0 edges vs 50 canonical |
| G2-1 theme spans ≥3 slugs | measure | 5 themes, best span **1 slug** |
| G2-2 concept query expansion | measure | 1 slug, no expansion-attributed result |
| G2-3 theme label quality | PASS | 5/5 non-empty and within cap |
| G2-4 demotion economics | PASS | 27 demoted, context-visible 137 < active 164 |
| G3-1 two-hop join | PASS | DLR data-store ID present, 2 slugs |
| G3-2 auth join | measure | 1 slug — the slice has only one gateway page |
| **G3-3 edit-and-resync** | **1 measure / 1 FAIL (gate)** | see below |
| G3-4 evidence precision | PASS | **20/20** facts carry `slug#anchor`; **20/20** verbatim `source_text` quotes verified |
| G3-5 deletion soft-retire | PASS | pruned, evidence preserved, absent from current retrieval |

**G3-3 in detail.** For `b3-01` the edit *did* flip current truth
(`after_has_new=True`, `superseded_total=1`) — supersession works. Both entries
still fail their full assertion because `before_had_old=False`: the pre-edit facts
("integration 99.5%", "CockroachDB non-starter") were **never extracted**, so
there is no old truth for `as_of` to return. This is an *extraction* gap, not a
temporal-engine gap — see §5.4.

### 5.4 The dominant finding: the type split does not materialize

**90% of everything extracted becomes `identity`.** 147 `IDENTIFIES` rows against
9 `REQUIRES`, 6 `HAS_STATE`, and **1** `DECIDES` — from a slice that includes an
11-section decision log. The whole `platform-decision-log` page yielded **4 active
facts**, and none of them is D005's "CockroachDB is a non-starter".

This one behaviour explains most of the misses above:

* **Band 1 / Band 2 recall.** The missing answers are the ones living in dense
  tables and decision prose (`b1-02` D-Scribe path, `b1-07` CockroachDB, `b1-12`
  SCIM). They were never extracted, so no retrieval strategy can find them.
* **G3-3.** Same cause — no base fact to supersede.
* **G1-3.** Per-type dedup floors are meaningless when one type holds everything:
  `identity=0.97` effectively applies corpus-wide, which is why
  `semantic_dedup_rate` is 0.018.
* **G2-1 / G2-2.** THEME clusters form *within* a page rather than across pages,
  because near-identical `identity` phrasings on one page cluster far more readily
  than genuinely related facts on different pages.

The obvious diagnosis — "a broad `IDENTIFIES` query is the path of least
resistance and nothing pushes back" — is **only half right**, and it is the less
important half. See §11.

### 5.5 Predicate families: the collapse, measured

`IDENTIFIES` did not hold 147 identity claims. It held **65 distinct predicates**,
because the model had eleven real relations to express and one type to put them
in (reproduced from the graph):

```
is 19 | has field 14 | publishes feed 10 | manages 6 | documents 6 |
supports 5 | holds role 5 | responsible for 5 | has pillar 4 | handles 4 |
enforces 3 | provides 3 | sends 3 | connects through 3 | ...
```

Facts typed `identity` included *"Jedai Gateway routes and controls model calls,
MCP tool invocations, vector store queries"* and *"provides per-team and
per-use-case cost attribution"*. Those are capabilities. The type system was not
describing the corpus; the corpus was being flattened into the type system.

The knock-on effects are all mechanical:

* `memory_type` selects dedup aggressiveness, retention, salience weight and
  retrieval budget, so 90% `identity` means **one policy for the whole graph**.
* Only `REQUIRES` / `DECIDES` / `HAS_STATE` are `single_active` — **16 of 168
  facts (10%)**. The rest sit in a multi-active type that can never supersede, so
  a re-ingested page accumulates rather than corrects.
* Thematic consolidation clusters on full-fact embeddings, so 14 near-identical
  `Special Offers has field …` strings clustered trivially and produced **2 of
  the 5 themes**, while genuine cross-page concepts never clustered.

---

## 6. Incremental sync

`.manifest.json` maps `slug#anchor → content_sha256`.

* **unchanged** → skipped entirely. No episode, no LLM call, no cost.
* **changed** → re-ingested under the *same* `custom_id`, so facts reinforce or
  supersede in place rather than duplicating.
* **new** → ingested normally.
* **deleted** → `forget_memory` soft-retire, with a guard (see below).

Because unchanged sections are never re-extracted, routine syncs do **not**
inflate `observed_count`. What inflates it is cross-page repetition — the same
`truth_key` asserted on N pages (INGEST.md §2.3 / §8.4).

### The deletion guard

INGEST.md §2 specifies `forget_memory` per fact for a deleted section. It does not
state the safety condition, which is required for correctness: a fact stated on N
pages reinforces into **one** row whose `episode_uuids` spans all N. Retiring that
row because one of its pages was deleted would silently drop a fact the surviving
pages still assert.

`sync.retire_deleted` therefore retires a row only when **every** episode backing
it belongs to a deleted section. Rows with surviving evidence are reported as
`retained`, naming the sections that still assert them. This is implemented and
active, not reporting-only.

---

## 7. Reading the evaluation output

`evaluate.py` prints three bands and a scorecard.

* **`found`** — an expected answer string appeared in a returned fact. For Band 2
  it additionally requires `min_slugs` distinct expected evidence slugs, so a
  single-page hit cannot pass a cross-page join.
* **`gtok` / `btok`** — injected tokens for the graph answer vs the naive
  baseline. The baseline is *generous to the baseline*: pages are ranked by
  IDF-weighted term overlap (a Pagefind stand-in) and the whole top page is
  charged. For Band 2 the baseline is charged every page needed to actually cover
  the answer, because one page cannot answer a join.
* **`origins`** — how each result arrived. `direct` is lexical/vector; `entity_hop`,
  `theme_member`, `member_theme`, and `seed` are graph expansion. **A Band-2 pass
  requires at least one non-`direct` origin** — that is the graph paying rent, and
  it is auditable per result.
* **Band 2b (paraphrase probe)** is a *measurement, never a gate*. These are
  INGEST.md §5a's zero-shared-vocabulary probes (measured 0.168 trigram cosine).
  Under the hermetic transport they are expected to fail; under `text-embedding-3`
  they are the before/after evidence that the production vector space works.

`verify_goals.py` prints PASS / FAIL / MEASURE per test. Only two are **hard
gates**, matching INGEST.md §7 "Go/no-go 1":

* **G1-2 — zero false merges.** A false merge inside the `kb_ds_*` family kills
  trust in the KB. The `..._wdw_...`/`..._dlr_...` pocket-guides pair (measured
  0.895 trigram cosine) is the canary.
* **G3-3 — temporal supersession.** A doc edit must flip current truth in one sync
  cycle while the old truth stays `as_of`-queryable.

G1-1 (fragmentation index) and G2-2b (paraphrase) are measurements that
parameterize expectations, not gates.

---

## 8. Policy choices worth knowing

Run `uv run ingest/kb_config.py --show` for the live values.

* **Dedup floors are set from measured cosines, not defaults.** The engine ships
  `cosine_threshold=0.88` with an **empty** per-type map. Real distinct data-store
  ID pairs reach **0.895**, so 0.88 (and the previously proposed 0.90) would
  false-merge live pairs. Identifier-bearing types get **0.97** (margin +0.075).
* **`theme` is in the Motive's `allowed_memory_types`.** A Motive that omits it
  makes consolidation skip thematic clustering wholesale with a
  `motive_theme_not_allowed` receipt — gating Goal 2 off entirely. This is
  INGEST.md correction §8.3.
* **`corroboration_required=1` on this tenant.** For a KB whose ground truth *is*
  the document, a single-page edit must flip truth immediately. The default (2)
  parks an equal-authority correction as `insufficient_corroboration` once the old
  fact is repeated on ≥3 pages. Keep the default in agent-facing tenants.
* **`strict_properties=True` is kept on the LLM path.** INGEST.md §4c permits
  flipping it to `False`. Keeping it `True` turns key invention into *countable*
  `property_key_not_allowed` receipts instead of silent stripping — which is the
  measurement the pilot exists to produce. Flip it to `False` for production
  ingest once the noise level is known.

### The dream-agent decision policy gates whole episodes — keep it permissive

The single most expensive mistake made while building this kit, worth stating
loudly because nothing in INGEST.md warns about it.

`DreamAgentConfig.decision_policy` is evaluated **per episode**, not per candidate.
A rejection discards the entire section, and the episode stays `pending` forever —
the run still reports success, so the loss is silent.

The first version of this pilot's policy read: *"…approve durable factual
memories. Reject speculation, example/sample payload values, and navigational
filler."* Reasonable-sounding, and it destroyed the pilot: the agent rejected
**17 of 37 episodes**, with rationales recorded in `dream_decisions`:

> "Rejected: source material is a platform decision log entry, not authoritative
> product documentation suitable for encyclopedic knowledge base curation."
>
> "Rejected: source material is navigational/structural metadata (portal index)
> rather than authoritative factual documentation."

That is all 11 `platform-decision-log` sections — the entire temporal band, and
INGEST.md's own G3-3 test subject — plus all 6 index/overview sections, which are
the cross-page join hubs Goal 2 depends on.

**The rule:** content-*selection* criteria belong in the **prompt profile**, which
shapes what gets extracted from a section. The **decision policy** only decides
whether a section is looked at at all, and for an operator-curated corpus the
answer is always yes. Per-candidate quality is already enforced downstream by the
salience rubric, the Motive type filter, and schema validation.

Diagnose it with:

```bash
uv run python -c "
import sys,asyncio; sys.path.insert(0,'ingest')
import kb_config as kb
async def m():
    c = kb.build_client()
    for d in await c.dream_decisions(limit=25):
        print(d.job_name, '|', d.summary[:160])
    c.graph.close()
asyncio.run(m())"
```

If `pending_episode_count` is non-zero after a sync, this is the first thing to
check. `sync.py` drains formation in sweeps and prints the pending count after
each one precisely so this cannot pass unnoticed.

---

## 9. Required engine changes — NOT made by this kit

This kit does not own `src/`. Two engine-level defects were found. Both are
reported here rather than fixed.

### 9.1 Entity resolution re-embeds the mention name once per candidate — O(mentions × inventory) network calls

**This is the top blocker for the full pilot.** It is a ~3-line fix.

**Symptom.** With `EntityResolutionPolicy.enabled=True` and a network embedding
transport, formation throughput is dominated by embedding round-trips and degrades
as the graph grows. Measured on this run: **~1.7 min/episode** over the first 13
episodes while the entity inventory grew from 25 to 57 nodes. A single extraction
call to the gateway is only **3.5 s** and one embedding call is **0.87 s**, so the
LLM is emphatically not the bottleneck — see §5.1 for the final measured rate.

**Mechanism.** `DreamEngine._resolve_entity_name` loops over candidate nodes
(`src/memotron/dreaming.py:5636`) and calls `_entity_link_signals` per
candidate. Inside that function, `src/memotron/dreaming.py:5140`:

```python
name_cosine = max(0.0, cosine_similarity(
    self._embedding_transport.embed(mention_normalized),   # <-- loop-invariant
    stored_name_vector,
))
```

`mention_normalized` depends only on the mention, not on the candidate, so this
embeds the **same string** once for every candidate in `scan`. When the extractor
supplies no `entity_ref` — the common case — `scan` is the full candidate list,
capped at `max_pair_scan` (default **512**).

The authors already hoisted the other candidate-independent signal two lines
above the loop:

```python
# The mention's own neighborhood is candidate-independent — resolve once.
mention_neighbors = self._mention_neighbor_uuids(scope_key, normalized)
```

The name embedding needs exactly the same treatment and did not get it.

**Impact.** Cost is `mentions × inventory` redundant HTTP round-trips per episode,
and the inventory grows with the graph, so ingest is **superlinear in corpus
size**. INGEST.md §7 budgets the 77-file pilot at "1 day, ~$2–3"; the money
estimate holds (redundant embeds are cheap per call) but the *wall time* does not
— see §5.1 for the measured rate and the resulting projection. The full 357-file
corpus is worse than linear on top of that.

**Suggested fix (not applied).** Hoist the embed out of the candidate loop:
compute `mention_vector = self._embedding_transport.embed(mention_normalized)`
once per mention next to `mention_neighbors`, pass it into `_entity_link_signals`,
and use it for every candidate. A per-run memo keyed on the normalized name would
also work and would additionally deduplicate across mentions of the same entity
within an episode. Expected speedup on this corpus: **20–40×**.

**Workaround if you must ingest before the fix lands.** Lower
`EntityResolutionPolicy.max_pair_scan` (e.g. to 8) to bound the fan-out, at the
cost of alias-detection recall; or set `entity_resolution.enabled=False` to
restore pre-alias-registry behaviour and lose Goal-1 canonicalization. Neither is
applied here — the pilot ran with the honest default so the number above is real.

### 9.2 A single transient gateway disconnect aborts a whole formation run, uncheckpointed

**Observed.** The second pilot run died at roughly episode 14 of 37 with:

```
  File ".../memotron/dreaming.py", line 5154, in _entity_link_signals
  File ".../memotron/embedding.py", line 201, in embed
  File ".../memotron/embedding.py", line 226, in embed_batch
http.client.RemoteDisconnected: Remote end closed connection without response
```

**Mechanism.** `OpenAICompatibleEmbeddingTransport` documents the behaviour
plainly: *"No retry framework: transient failures surface as `ValueError` exactly
like extraction transport errors."* The exception propagates out of
`_materialize_episode` → `_run_formation` → `run_job`, so the run aborts. Because
the run never reaches its checkpoint, `processed_episodes` stays empty and every
episode processed in that run must be re-extracted from scratch on the next
attempt — paying for the same LLM calls twice.

Note where it failed: inside `_entity_link_signals`, i.e. on one of the redundant
per-candidate embeds from §9.1. The two defects compound — the redundant call
volume multiplies the exposure to a transient failure that then discards the
whole run.

**Contrast.** The engine is careful about this elsewhere: a *dream-agent*
transport failure is explicitly contained, receipted as
`DREAM_AGENT_TRANSPORT_FAILED`, and the run continues on the deterministic
decision. Theme synthesis has the same containment. The embedding path has none,
even though it is called far more often than either.

**Suggested fix (not applied).** Bounded exponential-backoff retry inside the
transport (matching the extraction transport), plus per-episode checkpointing so a
mid-run failure does not discard completed episodes.

**Workaround used here.** `kb_config.ResilientEmbeddingTransport` wraps the real
gateway transport with retry *and* an in-process LRU memo. It delegates
`identifier` verbatim, so the vector-space guard is untouched and every vector
still comes from `text-embedding-3`. The memo is what makes §9.1's fan-out
affordable: because embedding is a pure function of (model, text), the engine's
repeated identical requests become free, and the pilot can run with the engine's
default `max_pair_scan=512` (full alias-detection recall) instead of a
recall-degrading bound. Each run prints its memo hit rate and retry count.

### 9.3 Sealing a tenant's gateway credential silently disables its production embeddings

**Symptom.** After `set_tenant_llm_credentials(tenant_id="jedai-portal-kb", …)`,
every embedding for the memory scope `tenant:jedai-portal-kb` is computed with the
hermetic 256-dim `LocalEmbeddingTransport` instead of `text-embedding-3`, even
though an `OpenAICompatibleEmbeddingTransport` was explicitly passed to
`Memotron(...)`. It is receipted **once per run** as
`embedding_transport_downgraded` with reason
`crypto_shred_scope_content_never_sent_to_embedding_endpoint`.

**Mechanism.**

1. `PropertyGraphStore.set_tenant_llm_credentials` (`src/memotron/graph.py:3952`)
   seals the key under
   `get_or_create_governance_key(f"tenant:{tenant_id}", subject_key="llm_credentials")`,
   inserting a `governance_keys` row with `scope_key = "tenant:jedai-portal-kb"`.
2. That string is **byte-identical** to
   `MemoryScope(kind=ScopeKind.TENANT, scope_id="jedai-portal-kb").key`.
3. `PropertyGraphStore.scope_content_is_protected` (`src/memotron/graph.py:171`)
   tests only for the *presence of any* `governance_keys` row for that
   `scope_key` — it does not filter on `subject_key` — so it reports the memory
   scope as crypto-shred content-protected.
4. `DreamEngine.content_embedding_transport` (`src/memotron/dreaming.py:725-756`)
   then forces that scope onto `LocalEmbeddingTransport` for reads *and* writes.

Verified directly on the pilot graph:

```
governance_keys rows:
   ('tenant:jedai-portal-kb', 'llm_credentials')
```

**Impact.** This fires for the documented, natural configuration — a `TENANT`
scope whose `scope_id` equals the `tenant_id`, plus sealed gateway credentials
(what `memotron-local-platform` and the agent-memory MCP server both do). The
tenant silently reverts to trigram vectors: semantic dedup, THEME clustering, and
vector retrieval all quietly degrade to INGEST.md §5a's measured blindness, while
the operator believes `text-embedding-3` is in use. The single per-run receipt
names crypto-shred, which points an investigator away from the real cause.

**Suggested fix (not applied).** Namespace the credential governance key so it
cannot collide with a memory scope key — e.g. seal under
`f"llm-credentials:tenant:{tenant_id}"` — or make `scope_content_is_protected`
filter on the content-plane `subject_key` rather than matching any row. A
migration would need to rewrite existing `governance_keys` rows. Either way the
`embedding_transport_downgraded` receipt should distinguish "this scope is
crypto-shred governed" from "this scope has sealed credentials".

**Workaround used here.** `kb_config.build_client()` does **not** seal. Explicit
transports are passed to `Memotron(...)` and are consulted directly, so sealed
credentials are never read on this path. `kb_config.assert_embeddings_live()`
fails the run loudly if any KB scope is marked content-protected, so the
degradation can never happen silently again on this pilot.

### 9.4 One "credential" candidate aborts a whole formation run, uncheckpointed

**Observed.** The first v2 run that actually extracted from
`platform-decision-log/approved.mdx#d009-standardize-litellm-secrets-storage-on-postgresql`
died with:

```
  File ".../memotron/dreaming.py", line 4676, in _secret_reference_metadata
ValueError: credential memory requires metadata.secret_reference with a governed pointer
```

**Mechanism.** `DreamEngine._secret_reference_metadata` (`dreaming.py:4664`)
classifies a candidate as secret material when `SECRET_CONTEXT_PATTERN`
(`password|passphrase|api key|access token|auth token|secret|credential`) matches
its **subject or predicate**, and then raises unless a governed
`secret://`/`vault://` pointer is supplied. D009 is a decision *about* where
LiteLLM keeps downstream model credentials, so a faithful extraction —
subject `LiteLLM downstream model credentials`, predicate `stored in` — is
indistinguishable from a leaked secret to a regex over two fields.

Two things make this worse than a rejected candidate:

* the `ValueError` propagates through `_materialize_episode` → `_run_formation`
  → `run_job`, so **one candidate discards the entire run**, and
* the run never reaches its checkpoint, so `processed_episodes` stays `0` and
  every episode is re-extracted and re-billed on the next attempt — which then
  reaches the same section and fails again. The run is not slow, it is *stuck*.

Verified against the section: `RAW_SECRET_ASSIGNMENT_PATTERN` and
`RAW_SECRET_TOKEN_PATTERN` find nothing in D009. The trigger is the vocabulary
gate alone, and the object field is not scanned at all.

**Why v1 never hit it.** v1's decision-log candidates were annihilated by the
salience recency bug (§5.4) before they reached this gate. Fixing extraction
uncovered the landmine.

**Suggested fix (not applied).** Treat this like every other per-candidate
quality failure: reject the candidate with a receipted
`CANDIDATE_SCHEMA_REJECTED` violation (`raw_secret_material`) and continue the
run, exactly as `predicate_not_a_verb_phrase` does. A corpus that *documents*
security architecture is a first-class use case, and a run-aborting exception on
a content pattern is not a proportionate response. Secondarily: scan the object
too (a real leak is far likelier there than in a predicate), and checkpoint
per-episode so a mid-run failure cannot discard completed work.

**Workaround used here, in two layers.** A prompt rule in
`_canonicalization_rules()` asks for credential vocabulary in the object rather
than the subject or predicate, where the gate does not look — the fact survives
with its meaning intact ("LiteLLM / stores / downstream model credentials in
PostgreSQL, encrypted with a Vault-held master key"). **The rule alone was
measured and it was not enough**: the run crashed again on the same section, which
is the expected result — a hard crash cannot be defended by a request. So
`kb_config.SecretVocabularyGuardTransport` wraps the extraction transport and
drops crash-shaped candidates before the engine sees them, mirroring the engine's
four raise conditions and importing the engine's own patterns so the two cannot
drift. Every drop is counted and printed by `sync.py`. The guard only discards
candidates the engine would itself have refused; it never rewrites one.

### 9.5 The `MemoryType` taxonomy has no slot for descriptive encyclopedic content

**Not a bug — a gap that forces a compromise.** `MemoryType` offers `identity`,
`preference`, `requirement`, `directive`, `state`, `decision`, `incident`,
`theme`. It was designed for agent memory, where every durable claim is about a
user, an obligation, or a behaviour. Platform documentation is ~90% descriptive
prose about systems, and only `identity` and `state` can hold it:

* `preference` forces `ClaimMode.PREFERENCE` / stance `PREFER`, so a capability
  would be recorded as something the system *prefers*, and it is not
  prune-protected.
* `directive` forces stance `REQUIRE` and feeds `CoherencePolicy
  .participating_memory_types` by default, so "the gateway enforces budget caps"
  would enter the escalation-windup detector as an agent obligation.
* `incident` means postmortems and would mislabel a feature list in the admin
  console.

The consequence is visible in §11.2: `IDENTIFIES`, `DELIVERS`, `OWNED_BY` and
`DOCUMENTS` all share `identity` and therefore share one dedup floor, one
retention rule and one retrieval budget, even though their false-merge profiles
differ by 0.3 cosine. `memory_type` is the *only* key `DedupPolicy
.memory_type_thresholds` accepts, so there is no way to separate them from
config.

**Suggested fix (not applied).** Add a descriptive/capability member to
`MemoryType` with `identity`-like governance (prune-protected, descriptive claim
mode, mid supersession severity) and give it a `DEFAULT_IMPORTANCE_WEIGHTS`
entry. Alternatively, and more cheaply, let `DedupPolicy` accept per-relationship-
type overrides alongside per-memory-type ones — the relationship type already
carries exactly the distinction that matters here.

Two smaller engine notes from the same exercise:

* `SalienceRubric.half_life_seconds` defaults to 24 hours and silently deletes
  correctly-dated historical facts in any document corpus (§11.1). The default is
  right for agent memory but there is no warning, and the loss is receipted as
  `below_min_salience`, which reads as a quality judgement rather than a
  temporal-scale mismatch. Reporting the recency component separately in the
  receipt would have made this obvious in minutes rather than after a full run.
* `PruningPolicy.stale_after_seconds` defaults to `{"state": 30 days}` keyed on
  `last_seen_at`. Any ingest path with an idempotence manifest — including this
  one, by design — never re-observes an unchanged fact, so the age measures sync
  cadence rather than staleness. Cleared for this tenant; worth a docstring
  warning upstream.

---

## 10. Cost model

`--dry-run` prices a run before any call is made:

* input tokens ≈ `(section chars + prompt-overhead chars per episode) / 4`
* the prompt overhead is **measured, not assumed** — `sync.prompt_overhead_chars()`
  renders the live instruction set and profile and adds a fixed graph-context
  allowance. It matters: the type split took the rendered prompt from **6,194 to
  ~20,400 chars**, and the old hard-coded `6000` would have under-priced every
  run by roughly 3×. `--dry-run` prints the figure it used.
* output tokens ≈ 750 per episode (a JSON memory list)
* rates default to `claude-haiku-4-5` at $1.00 / $5.00 per Mtok; override with
  `--input-rate` / `--output-rate`
* embeddings (`text-embedding-3`) are billed on input only and are ~2% of the
  total

The gateway is the billing authority; these are estimates for planning.

**The prompt overhead is now the dominant input cost, and it is cacheable.** Of
the ~20,400 chars, 8,524 are the four worked examples and the rest is the
instruction contract — all of it byte-identical on every single episode. Nothing
in this kit uses prompt caching, so the full corpus pays for that prefix 340
times. Sending it as a cached prefix is the single highest-leverage cost change
available and needs no policy change at all.

---

## 11. The actionability gate — the pilot's accept/reject test

Everything above measures whether the graph *retrieves*. This section measures
whether what it retrieved was **worth remembering**.

### 11.1 The standard

> **Memory is a shortcut for the agent's next action.**
> The test for any candidate fact is: *what step does knowing this let the agent
> skip?*

Qualifies:

* *"you're already authenticated to gh CLI via X, so just use it"*
* *"Special Offers content lives at `/content/preview/{locale}/{slug}/MarketingOffers/`, go straight there"*

Does not qualify: a **field inventory** (*"Special Offers has field
`data.descriptions.offer_intro`"*). The agent must open the schema anyway, so the
memory saves nothing — and when the field is renamed upstream the memory is not
merely useless, it is **actively misleading**.

One exception: a field **does** qualify when it *controls behaviour* — e.g. the
`authorship` frontmatter gating whether content is human-certified, or
`wdw_finder_not_searchable` gating whether an offer appears in search.

### 11.2 What the gold set is

`actionability_goldset.yaml` is the reference answer, hand-authored by reading
the pilot MDX under `products/jedai-gateway/`, `platform/knowledgebase/` and
`solution_engineering/platform-decision-log/`. **90 entries in three bands:**

| Band | n | What it holds |
|---|---|---|
| `must_remember` | 46 | Environment endpoints, key formats and key types per environment, model-alias conventions, budget thresholds, ownership/routing, governed procedures, and the platform decisions. Each records `saves:` — the step an agent skips — in one clause. |
| `must_not_remember` | 34 | Field inventories (14), enum member lists, endpoint path catalogues, navigation-card restatements, prose, and sample-payload values. Each records `why_not:` and, where one exists, the `authoritative_source:` that should be consulted instead (for endpoint shapes, `openapi.json`). |
| `edge_cases` | 10 | The genuinely arguable ones — a behaviour-controlling field, a version-pinned constraint, a fact actionable only in combination. Each carries `expected: keep\|drop` and the `reasoning:`. **Scored separately** so they can never inflate the headline. |

Every entry carries a stable id, the source `slug#anchor`, a `file:line`, and a
`quote` that is an **exact substring of that line**. Nothing is inferred from any
graph — the gold set is what the documents say, independent of what any run
produced.

The gold set is an **absolute** standard. A graph is judged on its own terms: did
it store what should be stored, and did it keep out what should not.

### 11.3 Keeping the gold set honest

```bash
uv run ingest/score_actionability.py --verify-citations
```

Re-reads the portal working tree and asserts every quote still appears at its
cited line. If the portal moves a line and nobody updates the citation, this
fails loudly rather than letting the harness score against a sentence that no
longer exists. The portal repo is opened **read-only**; the harness never writes
to it.

### 11.4 Scoring a graph

```bash
uv run ingest/score_actionability.py --graph portal-kb
uv run ingest/score_actionability.py --graph portal-kb --show all --json /tmp/score.json
```

`--graph` takes a graph name under `ingest/.memotron/` or an explicit
`.sqlite` path. It is **required** — there is no default, so the harness can
never quietly score something you did not name. The graph is opened read-only.

`score_actionability.py` imports nothing from `memotron` — only `sqlite3` and
PyYAML. It keeps working while the engine is being rewritten, and it cannot
influence the thing it is measuring.

### 11.5 Reading the scorecard

**A fact counts only if it is `context-visible`.** Stored is not the same as
usable: a fact that is quarantined, demoted out of the context tier, archived,
pruned, superseded, or time-bounded still sits on disk but will never reach a
prompt. For the purpose of "did the memory help", it is absent.

**`must-remember` fails in two ways, and they send you to different code:**

| Outcome | Meaning | Where the defect is |
|---|---|---|
| `hit` | matching fact stored **and** context-visible | — |
| `quarantined` | a matching fact **exists** but is not context-visible | the **gate** misjudged a good candidate |
| `miss` | no matching fact anywhere in the store | the **extractor** never produced the candidate |

Both count against recall. Collapsing them would hide which half of the pipeline
is broken, which is why they are reported separately — and why the quarantine
store is read as well as the graph.

**`must-not-remember`** is correct when the entry is either never extracted or
quarantined. It is a **false store** only when it is visible in context.

**Headline metrics:**

```
recall           = hit / must-remember
precision        = hit / (hit + false stores)
F1               = harmonic mean of the two
false-store rate = false stores / must-not-remember
noise ratio      = visible facts matching neither band / all visible facts
```

Precision is computed over the **gold-labelled** decisions — of the labelled
candidates the store chose to keep, how many should have been kept. It is *not*
corpus-wide precision; the corpus-wide view is the **noise ratio**, which counts
everything visible that the gold set does not account for either way. Read them
together: high recall with a high noise ratio means the gate is letting
everything through, not that it is choosing well.

**Match semantics.** A gold entry declares `subject_any` / `predicate_any` /
`object_all` / `object_any_groups` (and, for one edge case, `forbid_any`).
Matching is normalized-token containment: text is lowercased and reduced to
tokens that keep `. / : _ - { }`, so a URL, a dotted field path, a templated
D-Scribe path and a `kb_ds_*` data-store name each survive as **one** token
rather than shattering into fragments that match everything. A single-token
phrase also matches a hyphen- or underscore-bounded compound (`team` matches
`team-level`, but `id` never matches `identity`). The scorecard additionally
reports how many hits were **structural** — matched against the candidate's
actual subject/object fields rather than only the flat fact string — so a run
that matches merely lexically is visible as the weaker evidence it is.

**Quarantine stores are discovered, not assumed.** The scorer scans
`sqlite_master` for any table whose name mentions quarantine, flattens each row
(including nested JSON) into one text blob, and treats it as a non-visible
candidate. If no quarantine store exists yet, it says so and carries on — an
absent quarantine path is a valid state, not an error.

### 11.6 The gate

The run **rejects** (exit 1) when any threshold is missed:

| Threshold | Default | Flag |
|---|---|---|
| recall | ≥ 0.80 | `--min-recall` |
| false-store rate | ≤ 0.10 | `--max-false-store-rate` |
| noise ratio | ≤ 0.35 | `--max-noise-ratio` |

Use `--no-gate` to report without failing. These defaults are the proposed bar,
not a law of nature — but they are set **before** the run, and moving one is a
deliberate, reviewable act rather than a judgement call made after seeing the
number.

This replaces *"does the graph look reasonable"* as the pilot's accept/reject
test. Eyeballing a fact list cannot fail: 165 facts scroll past, the good ones
are in there somewhere, and the run gets waved through. The scorecard names
which actionable facts are absent, which were extracted and then wrongly
suppressed, and which noise made it into context — and it exits non-zero.
