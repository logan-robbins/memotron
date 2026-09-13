# Primer: Anthropic's knowledge-graph cookbook, read against Memotron

**Source:** <https://platform.claude.com/cookbook/capabilities-knowledge-graph-guide>
**Retrieved:** 2026-09-01. Content below reflects that snapshot; the cookbook is a living page.
**Why it's here:** it is the canonical minimal version of the pipeline Memotron implements.
Useful as a shared reference when arguing about extraction, entity resolution or eval — and useful
mostly for **where we deliberately differ**, which is most places.

> Read this as orientation, not as a spec. Nothing in the cookbook is authoritative for Memotron's
> behaviour; where the two disagree, the repo is right and the cookbook is a simpler system.

---

## The five stages

The guide builds an in-memory graph over six Wikipedia summaries about the Apollo program. No
database, no training data. Every classical ML stage is replaced by a prompt.

### 1. Extraction — one structured-output call per document

Replaces a trained NER model *and* a trained relation classifier. The Pydantic schema is the only
"training" involved.

```python
class Entity(BaseModel):
    name: str
    type: Literal["PERSON", "ORGANIZATION", "LOCATION", "EVENT", "ARTIFACT"]
    description: str          # one line, grounded in THIS doc — used later to disambiguate

class Relation(BaseModel):
    source: str
    predicate: str            # short verb phrase: "commanded", "launched from"
    target: str

class ExtractedGraph(BaseModel):
    entities: list[Entity]
    relations: list[Relation]

response = client.messages.parse(
    model="claude-haiku-4-5",
    max_tokens=2048,
    messages=[{"role": "user", "content": EXTRACTION_PROMPT.format(text=text)}],
    output_format=ExtractedGraph,
)
return response.parsed_output
```

`messages.parse()` + `output_format=` guarantees schema-valid typed output — no regex, no JSON
decode errors, no defensive `isinstance`.

The two prompt instructions doing the real work:

- *"Extract only entities central to what this document is about — skip incidental mentions."*
- *"For each entity, write a one-sentence description grounded in this document."* — this
  description is what makes stage 2 work at all.

### 2. Entity resolution — cluster surface forms with Claude, not string distance

The point the guide makes well: edit distance and Jaccard handle typos but cannot resolve
**"Edwin Aldrin" → "Buzz Aldrin"**, two names with zero character overlap. Claude clusters using the
stage-1 descriptions as disambiguation context, so "Armstrong — first person to walk on the Moon"
does not merge with "Armstrong — jazz trumpeter".

Output is `{canonical, aliases[]}`, flattened into an `alias_to_canonical` map.

**Two failure modes the guide calls out explicitly** — both worth internalising:

- **Silent node loss.** Any raw name Claude omits from every cluster vanishes from the graph, because
  the alias map has no entry. A production resolver must fall back to a single-element cluster.
- **Over-merge.** A specific mission ("Gemini 12") gets folded into the broader programme
  ("Project Gemini") because descriptions overlap. Loses precision rather than nodes.

### 3. Assembly — rewrite endpoints, load into NetworkX

`MultiDiGraph`, because two entities can be joined by several predicates and direction is meaningful
("Armstrong commanded Apollo 11" ≠ the reverse). Nodes carry type, source docs, mention count; edges
carry predicate and source doc.

Diagnostic heuristic worth stealing: **one connected component means resolution worked.** Fragmented
islands mean variants that should have merged didn't.

### 4. Summarisation — turn hub nodes into profiles

For high-degree nodes, pool every mention plus the graph neighbourhood and synthesise a structured
profile (`summary`, `key_facts[]`, `time_range{start,end}`). The guide's framing: this is what turns
"a graph of labels into a graph of knowledge."

### 5. Querying — serialise a subgraph, let Claude reason over it

k-hop neighbourhood → `(subject) --[predicate]--> (object)` lines → prompt. The guide shows the same
question answered with and without graph context. The ungrounded answer is *richer* and probably
correct (Apollo 11 is famous); the grounded answer is **traceable**, citing the specific edges. The
honest conclusion it draws: on a private corpus where the model has no priors, only the grounded
answer works at all.

---

## The ontology question — closed vs open

**This is the load-bearing decision in the whole guide, and it is easy to read past.** The cookbook
never argues for it; it just makes a choice in the type signatures. The choice is asymmetric:

```python
type: Literal["PERSON","ORGANIZATION","LOCATION","EVENT","ARTIFACT"]   # entities: CLOSED
predicate: str    # "commanded", "launched from"                        # relations: OPEN
```

**Closed entity vocabulary, open relation vocabulary.** Five node types fixed in advance; unlimited
edge phrasings invented per document.

### Memotron makes the same call, explicitly and with teeth

This is convergence, not contrast — and worth knowing, because it means the cookbook's shape is
evidence *for* the position we already hold rather than an alternative to it.

`extraction.py:403`, `match_relationship_instruction`:

> *"Exact match wins so a declared structural relation keeps its declared cardinality and memory
> type; the open instruction is the catch-all that makes the relation vocabulary open **without
> loosening the entity vocabulary**."*

- **Entities: closed.** Endpoint labels are validated against the instruction set's label set.
  `_label_admits` allows `"*"` only on an open-predicate instruction, and the comment at
  `extraction.py:880` is explicit that endpoint labels are "validated above against the closed label
  set either way, so an open predicate can **never introduce an untyped entity**."
- **Relations: open.** An `open_predicate` instruction accepts any verb phrase between admitted
  endpoint labels.
- **Unmatched is a hard error, not a silent drop.** `CandidateSchemaError` with
  `CandidateViolation.RELATIONSHIP_TYPE_NOT_ALLOWED`. Contrast the cookbook, where a name the
  resolver omits from every cluster **silently vanishes** — the guide flags this as a bug you must
  fix yourself.

### The layer the cookbook doesn't have

Open predicates only stay safe because every one of them is projected onto a **closed governance
taxonomy** before it can materialise:

| Layer | Open / closed | Where |
|---|---|---|
| Entity labels | **closed** (per instruction set) | `_label_admits`, `extraction.py` |
| Predicate / `relationship_type` | **open** (normalised `UPPER_SNAKE`) | `models/_graph.py:146` |
| `MemoryType` | **closed — 8 values**: anchor, preference, requirement, directive, state, decision, incident, rollup | `models/_enums.py:170` |
| `ClaimMode` | **closed — 6 values** | `models/_enums.py:139` |
| Bridge | `RELATIONSHIP_TYPE_MEMORY_TYPE_MAP` — **only 4 entries** (REQUIRES, PREFERS, SHOULD, ROLLUP); anything else must set `memory_type` explicitly or config validation raises **fail-fast** | `config/_claim_mode.py:19` |

`MemoryType` is not a label — it decides cardinality, retention, dedup aggressiveness and retrieval
budget. So the real rule is: **you may say it any way you like, but it must land in one of eight
governed buckets, and you must declare which.**

### Why the closed layer has to be tight: our own 84% failure

`models/_enums.py:170` records a measured ontology failure in this codebase. `MemoryType.ANCHOR` used
to be called `identity`:

> *"the old name for this type was `identity`, which read as 'facts about what something is' and
> **absorbed 84% of a documentation corpus** (every descriptive sentence is arguably an identity).
> An anchor sends the agent somewhere; prose does not."*

One under-constrained category in an otherwise closed set swallowed five-sixths of the corpus. The
fix was not a threshold or a better model — it was **renaming the category and narrowing its
definition**. That is the strongest evidence available that ontology design, not extraction quality,
is the dominant term. Carry it into any argument about adding a ninth `MemoryType`.

---

## Where Memotron already differs

Verified by reading this repo on branch `production-hardening`, 2026-09-01. Line references are to
files I opened; the "Memotron" column describes what the code shows, not what is deployed.

| Concern | Cookbook | Memotron |
|---|---|---|
| **Ontology** | closed entities / open predicates, **ungoverned** | **same axis** — closed entities, open predicates — but every predicate projects onto a closed `MemoryType`/`ClaimMode`. See above. |
| Triple shape | `source, predicate, target` (3 fields) | `ExtractedMemory` in `src/memotron/models/_graph.py:72` — subject/predicate/object **plus** `relationship_type`, `confidence`, `scope`, `claim_mode`, `valid_from`/`valid_to`, `salience_score`, `saves_step`, `source_text` |
| Entity resolution | one-shot clustering, no undo | `dreaming/_entities.py` — "roughly half resolution and half retraction"; merges are explicitly **reversible** (`_discount_entity_alias`, `_rebridge_on_alias_activation`) |
| Dedup | not addressed | `dreaming/_dedup.py` — a separate destructive plane with `_weaker_duplicate` arbitration, a cross-scope prefix sweep, and `repromote_duplicates_for_dependency` as the undo |
| Multi-tenancy | none | scope on every candidate; cross-scope leakage is tracked as a defect class (#108, #109, #137) |
| Provenance | `source_doc` string on each edge | receipt ledger — even a coerced field emits a receipt (`CANDIDATE_CLAIM_MODE_COERCED`) |
| Temporality | `time_range` on the profile only | `valid_from`/`valid_to` on rows; epoch visibility; replay |
| Storage | NetworkX in memory | SQLite + Postgres backends (`storage/sqlite`, `storage/postgres`); Neo4j Enterprise is the November target (#10) |
| Model | Haiku extract / Sonnet synthesise | `DEFAULT_GATEWAY_MODEL = "claude-haiku-4-5"` (`gateway.py:58`), via the LiteLLM gateway |

**The split-model pattern is the one thing we already agree on**: cheap model for high-volume
schema-constrained extraction, stronger model where evidence has to be weighed. The cookbook uses
`claude-haiku-4-5` / `claude-sonnet-4-6`; our gateway default is `claude-haiku-4-5`.

The structural difference in one line: **the cookbook builds a graph, Memotron maintains one.**
Nearly all our extra machinery — retraction, dedup arbitration, receipts, epochs, scope — exists
because facts arrive over time, contradict each other, and have to be un-done. The cookbook extracts
once from a fixed corpus and never revises.

---

## What's worth taking

- **The eval loop.** Precision/recall/F1 against a small hand-labelled gold set, with an alias map so
  surface variants score as hits. The guide's closing line is the right one: *"change the extraction
  prompt, rerun the scorer, watch the F1 move. That loop is what turns a demo into a production
  system."* We currently have no equivalent for extraction quality — the verification harnesses in
  `scripts/verify/` check structure and invariants, not extraction accuracy.
- **Descriptions as disambiguation context.** Making the extractor write a one-line grounded
  description *for the purpose of* later resolution is a cheap, transferable trick.
- **Blocking before resolution.** The guide's scaling note: never feed 10k entities to one prompt.
  Block by cheap signals (shared tokens, embedding similarity), then let Claude arbitrate within
  blocks of 50–100. The prompt is unchanged; only the batching is.
- **Connected-component count as a resolution smoke test.**

## What not to take

- **The in-memory NetworkX assembly** — we have two storage backends and a third planned.
- **One-shot resolution with no retraction path.** Our `_entities.py` docstring is explicit that
  resolution "routinely gets it WRONG and has to be reversible." The cookbook has no undo.
- **Open predicates with nothing underneath.** Not the open vocabulary itself — we chose that too
  (see *The ontology question*). What doesn't transfer is leaving it ungoverned: our open predicates
  are only safe because each resolves to a closed `MemoryType`/`ClaimMode` that decides cardinality,
  retention and dedup. Adopt the openness and skip the projection and you get an unbounded edge
  vocabulary with no governance.
- **The scoring approach at face value** — see below.

## Caveats on the guide itself

- **Its own numbers are weak, and it doesn't dwell on this.** The shipped eval run scores
  `Apollo 11: raw F1 0.71 (P 1.00, R 0.55)` and `Neil Armstrong: raw F1 0.55 (P 1.00, R 0.38)`. The
  naive pipeline **misses 45–62% of gold entities** on a famous, clean corpus. Perfect precision with
  poor recall is what "extract only central entities" buys you. Do not cite this pipeline as
  evidence that prompt-only extraction is sufficient.
- **Resolution can *lower* measured recall.** If the resolver picks a verbose canonical form
  ("Neil Alden Armstrong") the alias map doesn't cover, a name that matched gold before resolution
  stops matching after. The guide correctly calls this a scoring artifact, not a resolver bug — but
  it means the metric moves for reasons unrelated to quality.
- **Relation scoring ignores predicate wording**, matching only `(source, target)` pairs. The guide
  says so directly: its relation recall is an upper bound.
- **Six documents, in memory, no adversary.** No tenancy, no contradiction, no deletion, no audit —
  i.e. none of the properties that make our version hard.

## Related cookbook pages

- [Extracting structured JSON](https://github.com/anthropics/claude-cookbooks/blob/main/tool_use/extracting_structured_json.ipynb) — same extraction via tool-use, for agentic flows
- [Retrieval augmented generation](https://github.com/anthropics/claude-cookbooks/blob/main/capabilities/retrieval_augmented_generation/guide.ipynb) — the complement: retrieval rather than traversal
- [Contextual embeddings](https://github.com/anthropics/claude-cookbooks/blob/main/capabilities/contextual-embeddings/guide.ipynb)

## Provenance of this document

Written 2026-09-01 by an agent session. The cookbook content was fetched and read in full. The
Memotron column of the comparison table was grounded by opening `models/_graph.py`,
`dreaming/_entities.py`, `dreaming/_dedup.py`, `gateway.py` and the `dreaming/` module listing — it
is **not** a full audit of the pipeline, and any claim here about Memotron should be re-checked
against source before being relied on in a design decision.
