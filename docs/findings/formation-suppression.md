> **Local working note — not filed anywhere.** Draft write-up of a dogfood finding,
> kept in-repo for review. Raw investigation trail is in `DOGFOOD-LOG.md`.

# Accumulated memory suppresses new memory formation (formation gate reads graph context, not the episode)

**Found by dogfooding `main`-line behaviour against a live JedAI Gateway key. Not a PR #44 regression** — the gate (`formation_episode_selected`) and the graph-context injection that feeds it are both on `origin/main`, present since the initial commit.

## Symptom

`memory_publish(prose)` → `queued_for_dreaming: True` → `memory_refresh()` → **zero memories formed**, gateway billed, no error, no receipt, no negative-space entry.

Our dogfood graph reached this state after **three** facts.

## Mechanism

`dreaming.py` asks the dream agent to approve each episode *before* extraction, and passes the scope's existing memories as decision context:

```python
graph_context = self._graph_context_for_episode(episode=episode, policy=job.context_policy)
decision = await self._decide(..., decision_type="formation_episode_selected",
                              context_facts=tuple(m.fact for m in graph_context), ...)
run.decision_count += 1
if not decision.approved:
    continue          # never processed, never marked processed
```

The agent then judges **the state of the world described in that context** rather than whether the episode is worth recording. Its own summaries:

> "Reject formation episode: deployment infrastructure lacks required durability guarantees (ephemeral KEK, in-memory graph store, and permission constraints)"

> "Rejection due to **conflicting context facts** regarding KEK configuration…"

The episode said *"we decided the Postgres cutover is blocked until a real key manager exists."* That is a decision worth remembering **because** the infrastructure isn't ready. The agent concluded the opposite.

## Evidence — controlled sweep, one variable at a time

Same episode text every run except row D. Fresh graph copy per run.

| # | motive | prompt profile | graph context | processed | facts formed |
|---|---|---|---|---|---|
| baseline | `agent-memory` | `support-memory` | 3 facts | 0 | 0 |
| A | `engineering-agent-memory` | `support-memory` | 3 facts | 0 | 0 |
| B | `agent-memory` | `agent-lessons` | 3 facts | 0 | 0 |
| D | `agent-memory` | `support-memory` | 3 facts, **unrelated prose** | 0 | 0 |
| **E** | `agent-memory` | `support-memory` | **wiped (0 facts)** | **1** | **4** |
| **F** | `agent-memory` | `support-memory` | **3 BENIGN facts** | **2** | **5** |
| G | `agent-memory` | `support-memory` | 3 facts (negative), **dream agent = `claude-sonnet-4-6`** | 0 | 0 |
| **H** | `agent-memory` | `support-memory` | 3 negative facts present but **injection disabled** | **2** | **3** |
| threshold | — | — | **1** negative fact | 0 | 0 |
| threshold | — | — | **2** negative facts | 0 | 0 |
| **control** | — | — | **1 BENIGN fact** | **1** | **3** |

**Row D is the decisive one.** The episode was about dark mode, British English and standup times. The rejections still cited *"non-ephemeral KEK requirement for Postgres cutover"* — text that appears nowhere in that episode. The agent was not reading the episode at all.

**Row E confirms by removal.** Delete the relationships, and the identical episode is approved: *"Episode queued for memory formation meets standard criteria with valid subject identification and default instruction set."*

**Row H is the third independent route to green, and a workaround.** Leave the negative facts in the graph and set `context_policy.enabled=False` on the formation job — formation succeeds. So the variable is specifically *negative context reaching the decision prompt*: not the facts existing, not volume, not motive, not prompt profile, not model. (Caveat: context presumably also serves dedup and supersession, so disabling it is not free; that cost is untested.)

**The threshold is one, and there is no runway.** Varying `context_policy.max_relationships` against an unchanged graph, 1, 2 and 3 negative facts all suppress formation, while a single *benign* fact at the same limit does not. The first time a scope records a blocker, it stops forming new memory.

Motive, prompt profile, model, and volume are all ruled out. Only the *content* of injected context matters.

**Row F answers "sentiment or volume?" — it is sentiment.** Seeded with three *neutral* facts (British English in docs, standups at 9am, dark mode), context is non-empty yet both episodes are approved, and the verdict language inverts completely: *"Episode qualifies for memory formation with **sufficient context facts** and valid operational parameters."* With benign context the agent treats context as supporting formation; with unresolved/negative context it treats the same field as grounds to refuse.

That is the better of the two answers — volume would have meant every scope degrades — but it still targets precisely what an engineering scope accumulates: incidents, blockers, and open requirements.

**Row G rules out model capability.** Re-running the failing case with `claude-sonnet-4-6` as the dream agent produces the same refusal and the same reasoning. This is the prompt and the design, not a weak model.

> Caveat on G: `MEMOTRON_DREAM_AGENT_MODEL` **does not work** for this — see "Also observed" below. The model must be swapped by constructing the transport explicitly, which is how row G was actually run (verified: `dream agent model now: claude-sonnet-4-6`).


## Severity, after replication and a recovery test

Two results move this off "catastrophic":

- **It reproduces exactly.** 5/5 RED in the negative-context condition, 5/5 GREEN in the benign condition, identical counts each run. (An earlier note claiming the gate was non-deterministic was wrong and has been retracted — that observation was a different config behaving consistently.)
- **A suppressed scope recovers.** Blocked episodes are never marked processed, so they stay in the durable queue. Forget the offending fact and the backlog forms on the next refresh (`processed=0` → `processed=2`). **This is a liveness failure, not data loss.**

What remains serious is that it is silent, it is billed, and it targets exactly the content an engineering scope accumulates.

## Why this is worse than a prompt bug

**The failure scales with success.** Negative or unresolved content in a scope's memory suppresses formation of new memory in that scope. Incidents, blockers and open requirements are exactly what an engineering scope accumulates — so the more the system remembers, the less able it becomes to remember anything new. A memory system that stops learning as it fills up is self-limiting, and it degrades silently.

## Secondary: a denial is unobservable

After a run that claimed 2 episodes and refused both:

- `negative_space(scope)` → **0 entries**
- receipts → only unrelated materialisation/retrieval events
- gateway → **billed for every refusal**

The ledger whose stated purpose is *"provenance of what was deliberately not remembered"* does not cover formation-level denial. From outside, money was spent and nothing happened. This is the difference between "memory just isn't very good" and a diagnosable failure, and it is what would make this near-impossible to spot in production — particularly given there is no observability in the service today.

## Reproduction

Deterministic, ~6s, no fixtures beyond a configured tenant:

1. Configure a project (`memotron init`) and a gateway LLM (`memotron llm configure`).
2. Write ≥3 facts to the tenant scope — ideally ones describing unresolved problems.
3. `memory_publish(agent_id=…, content=<any prose>, task_run_id=…)`
4. `memory_refresh(agent_id=…)`
5. Observe: `processed_episodes: 0`, no `created_by: dream-agent:*` rows, non-zero gateway spend.
6. Delete the relationships and repeat step 3–4 → episode is approved and extracted.

## Design intent — the fix is aligned, the behaviour is not

`dreaming.py:156-217` documents the rule governing all six dream-agent decision points:
*a fallback may approve CREATION; it may never approve REMOVAL.* `formation_episode_selected`
is deliberately **fail-open**, and its entry states that approving it *"decides nothing about
what may be written"* because redaction, salience, dedup, Motive type filters, the
supersession authority gate and the untrusted-directive gate **all still run downstream**.

It further warns that *"failing closed here would silently stall the system's core function
for the whole duration of an outage."* That is precisely the harm observed — except it
arrives via the model answering **no**, not via an outage, so no fallback fires. The design
guarded the transport-failure path and left the confident-refusal path open.

**So the gate is an admission step, not a quality filter, and withholding graph context from
it removes no documented safety property.** The change is a correction toward the
specification, not a removal of a feature.

## Suggested direction (not a patch)

The design question is whether the dream agent should gate formation at all. Extraction already has typed policy, salience thresholds, dedup and governance; an LLM veto in front of it adds cost and a failure mode without an obvious guarantee. If the gate stays, three things seem necessary regardless:

1. **The decision prompt must ask about the episode, not the world** — context is useful to the extractor, not as grounds to refuse recording. Note the two consumers are already separable in code: `context_facts` feeds the gate (`dreaming.py:1959`) while `graph_context=` feeds extraction (`:2074`). Withholding context from the *gate* while keeping it for *extraction* would fix this without the quality cost described below.
2. **Every denial must produce a negative-space entry**, so refusals are queryable rather than invisible.
3. **A denial should be distinguishable from a transport failure** in receipts, since both currently end in "nothing was formed".

## Validated fix (tested here, NOT applied)

Withhold graph context from the **gate** while leaving it intact for **extraction**. The
two consumers are already separate call sites, so this is a one-line change at
`src/memotron/dreaming.py:1959`:

```diff
             scope=episode.scope,
-            context_facts=tuple(memory.fact for memory in graph_context),
+            # The gate decides whether to RECORD this episode; it must not be
+            # given the scope's existing memories, because unresolved content
+            # there is read as grounds to refuse. Context still reaches the
+            # extractor below (graph_context=... in episode_extractor.extract).
+            context_facts=(),
             details={
```

`graph_context` is still computed and still passed to `episode_extractor.extract(...)` at
`:2074`, so entity resolution, dedup awareness and extraction quality are unaffected.

**Results, simulated by patching `DreamEngine._decide` for `formation_episode_selected` only:**

| condition | before | after |
|---|---|---|
| negative context | **RED 5/5**, processed=0 | **GREEN 5/5**, processed=2 |
| benign context | GREEN, processed=2 | GREEN, processed=2 (no regression) |

Extraction quality also came out **better than the `context_policy.enabled=False`
workaround**, which is what the design predicts: with context still reaching the extractor,
the run produced `"Postgres cutover requires a non-ephemeral key manager"` (correct
meaning) where the context-off run produced the meaning-inverted `"the backend defaults to
a non-ephemeral KEK"`.

**Caveats.** This removes the gate's ability to use context for *any* purpose — if some
intended behaviour depends on it (e.g. refusing an episode already fully represented in
memory), that behaviour goes too, and no test in the suite covers it. It was validated on
small graphs (1–9 facts) with one prompt-profile family. And it treats the symptom at the
call site; the deeper question — whether an LLM veto belongs in front of extraction at all
— is unchanged.

## The rejected workaround, and its cost

`context_policy.enabled=False` on the formation job stops the suppression (verified: negative facts present, formation succeeds). But `graph_context` feeds **both** the gate and the extraction prompt, so disabling it also blinds the extractor to what the scope already knows. Dedup survives (embedding-based at materialization) and so does entity resolution (passed separately), but context-off runs produced near-duplicate and meaning-inverted facts side by side. It trades a liveness failure for a quality regression — a stopgap, not a fix.

## Also observed (separate, not investigated)

**`MEMOTRON_DREAM_AGENT_MODEL` is silently ineffective once a tenant has stored credentials.** `gateway.py` documents it as "Optional distinct decision model for the dream agent", but after `memotron llm configure` seals a model into `tenant_llm_credentials`, the sealed value wins and the env var is ignored with no warning. Setting it appears to work — the run proceeds normally on the old model. This cost us a false negative mid-investigation: an experiment that appeared to test Sonnet had in fact run on Haiku twice.

**The gate is non-deterministic.** In the benign-context control, one episode was approved while a second, similar infra-flavoured episode was rejected in the same run. So negative content in the *episode itself* can also trigger refusal, and two comparable episodes can get opposite verdicts in one run. Rate not quantified.

**Extraction quality is poor even when the gate passes.** Rows E/F produced `"Ryan prefers the latest environment"`, `"Ryan decides the latest environment"`, and `"the backend defaults to a non-ephemeral KEK"` — the last inverting the source's meaning (*requires* a non-ephemeral KEK). One output also fused two unrelated facts: `"Postgres cutover is blocked by UV_CACHE_DIR set; /app is root-owned"`.

## Confidence and limits

Every row above is one run against small graphs (3–9 facts) on a single gateway environment. The effect is large and consistent — the negative-context condition was reproduced 5+ times across the session, and the two green conditions flipped cleanly — but the sample is small and the prompts were not varied systematically. What is *not* established: the threshold (how much negative content is required), whether it varies by memory type, and whether a scope can recover once suppressed.
