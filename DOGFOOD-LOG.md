# Dogfood log — running list of what we hit

Working notes from actually running Memotron against itself. Append-only, newest
section at the bottom. Findings that survive get promoted to `STATE.md`; this file is
the raw trail, including dead ends, because the dead ends are half the value.

**Setup:** branch `pr44-local` (= PR #44), `.memotron/dogfood.sqlite` (project-local,
deliberately NOT the packaged `~/.memotron/memory.sqlite` — see D-02), gateway key
`memotron-dogfood-latest` in gitignored `.env`.

---

## Setup findings

**D-01 — `memotron` must be a `uv tool`, not a venv entry point.**
`.mcp.json` and all four hooks invoke a bare `memotron`. From a venv it isn't on PATH
and the hooks fail silently at session boundaries — the worst place for a memory system
to fail quietly. Fixed with `uv tool install --force .`.

**D-02 — the packaged graph path is machine-global and that is the real hazard.**
`init` writes `graph_path: ~/.memotron/memory.sqlite` — **one graph shared by every
project on the machine**. That is what makes `init` in a second repo enumerate *other
projects'* tenant scopes and offer to move one. Repointed to a project-local file; that
single change makes the migration offer structurally impossible here.

**D-03 — init artifacts are not gitignored.** `.mcp.json`, `.claude/settings.json`,
`.memotron.yaml` are all tracked by default. Added to the worktree's exclude file.

**D-04 — gateway management routes are split by host.** `POST /key/generate` on
`latest.jedai-gateway…` returns *"Management routes are disabled for this instance."*
Key minting only works on `latest.jedai-gateway-**admin**…`. This resolves the ambiguity
flagged in the architecture report: the two documented hostnames are **not**
interchangeable.

**D-05 — the key model is sound.** `.memotron.yaml` stores `api_key_env` (the variable
*name*) and never a value; the secret is sealed into the tenant graph. A key can be
provisioned without it ever entering an agent transcript.

---

## API friction (each cost a round trip)

**D-06 — `register_agent` is sync while its neighbours are async.** Every other
`AgentMemoryPlatform.memory_*` method is `async def`; this one isn't, so the obvious
`await` raises `TypeError: object AgentRegistrationResult can't be used in 'await'`.

**D-07 — a fresh tenant needs two undocumented setup calls before `memory_publish` works.**
`agent_register` → *"agent_id 'claude-code' is not registered for tenant …"*, then
`project_memory_configure` → *"project memory is not configured; an operator must call
project_memory_configure before agents can publish candidates."* Neither is mentioned in
the SKILL.md quickstart. `init` does both, so this only bites programmatic callers.

**D-08 — SDK signatures differ from the documented shapes.** `add_episode` takes
`episode_body` + `name` + `source`, not `body`. `dream_history`/`dream_decisions` take no
`scope` argument. `add_memory` rejects invented `relationship_type`s — the `default`
instruction set allows only `PREFERS`, `REQUIRES`, `SHOULD`.

---

## Correctness findings

**D-09 — two search paths disagree on the same data.**
`Memotron.search(query="KEK")` → **0 hits**, while
`AgentMemoryPlatform.memory_search(query="why is the Postgres cutover blocked")` →
returns the KEK fact correctly. Same graph, same scope. The raw client path missed a
literal substring the platform path found. **Unexplained — still open.**

**D-10 — dreaming bills the gateway but forms zero memories. [UNDER INVESTIGATION]**
`memory_publish(prose)` → `queued_for_dreaming: True` → `memory_refresh()` → gateway spend
rose to $0.0037 (60× a single smoke call), and the graph gained **no** facts. All facts
present are `created_by: client-managed-memory` (hand-written); none `created_by:
dream-agent:*`.

Feedback loop: `scratchpad/loop.sh scratchpad/probe.py` — 6s, deterministic, currently RED.
Seeds from a copy of the configured dogfood graph so tenant/agent/LLM setup is already done.

Narrowed so far:
- The signal is `formation: episodes=0` — **the episode is never consumed**, so the model
  output was never the problem. This reframed the whole bug.
- Episode scope is `tenant:memotron-dogfood`; the formation job that ran was
  `formation-default […/tenant:memotron-dogfood/balanced]`. **Scopes match.**
- `job_state` shows that job's `last_run` updated at the same second as the publish.
- Episode is **not** in `processed_episodes` → never consumed, not consumed-and-discarded.
- Ruled out: endorsement gate (`promotion_endorsements` = 0), quarantine
  (`quarantined_candidates` = 0), consumer idempotency (`episode_processing` empty).
- Episode payload carries `instruction_set: default` and no queue flag — pending state is
  derived from absence in `processed_episodes`, not from a stored boolean.

**ROOT CAUSE — the dream agent is rejecting every episode, and it is answering the wrong
question.**

Mechanism, confirmed by tracing `run_job`:

```
[DEBUG] passing 5 episodes to run_job
[DEBUG] match ep=873a06c7 -> True      # matching is FINE
[DEBUG] match ep=0581d7f1 -> True
[DEBUG] claim_episodes(in=2) -> claimed 2   # claiming is FINE
  processed_episodes: 0 | decision_count: 2
```

`dreaming.py` formation loop calls the dream agent per episode and drops the episode on a
denial:

```python
decision = await self._decide(..., decision_type="formation_episode_selected",
                              proposed_action="Process this queued episode for memory formation.")
run.decision_count += 1
if not decision.approved:
    continue          # never processed, never marked processed
```

That `_decide` call is an LLM round trip — **that is where the gateway spend came from.**
The model was billed to say "no" twice.

Its own recorded summaries show it is judging the *content's* readiness rather than
whether the statement is worth remembering:

> "Reject formation episode: deployment infrastructure lacks required durability
> guarantees (ephemeral KEK, in-memory graph store, and permission constraints)"

> "Reject memory formation due to unresolved infrastructure dependencies: non-ephemeral
> KEK requirement for Postgres cutover, UV_CACHE_DIR permission issues…"

The episode said *"we decided the Postgres cutover is blocked until a real key manager
exists."* That is a decision worth remembering **because** the infrastructure isn't ready.
The agent instead reasoned "the infrastructure isn't ready, so don't form the memory" —
it conflated the meta-decision (should this be recorded?) with the object-level one
(is the thing described in good shape?).

Two contributing factors, both visible in the decision `details`:
- `prompt_profile_key: 'support-memory@v1'` — a **support-desk** prompt profile judging
  engineering content.
- `active_motive: 'agent-memory'` while the episode carries `motive:
  'project-memory-policy'`. `resolve_motive` precedence is job.motive > episode metadata,
  so the job's motive wins and the episode's intended motive is **ignored**.

**D-11 — a formation denial leaves no negative-space entry and no receipt.**
After a run that claimed 2 episodes and rejected both: `negative_space(scope)` → **0
entries**, and the only receipts are the 3 hand-written materializations plus
retrieval/injection events. The ledger whose stated purpose is *"provenance of what was
deliberately not remembered"* does not cover formation-level denial. From outside, money
was spent and nothing happened, with no way to ask why except reading `dream_decisions`.
This is the finding with the most product weight: it is the difference between "memory
just isn't very good" and a diagnosable failure.

**D-12 — CONFIRMED ROOT CAUSE: accumulated memory suppresses new memory formation.**

The prompt/motive hypothesis was **wrong**. Controlled sweep, one variable at a time,
same episode text each run except where noted:

| # | motive | prompt profile | graph context | result |
|---|---|---|---|---|
| baseline | `agent-memory` | `support-memory` | 3 facts | **RED** 0 processed |
| A | `engineering-agent-memory` | `support-memory` | 3 facts | **RED** 0 processed |
| B | `agent-memory` | `agent-lessons` | 3 facts | **RED** 0 processed |
| D | `agent-memory` | `support-memory` | 3 facts, **benign prose** | **RED** 0 processed |
| **E** | `agent-memory` | `support-memory` | **wiped (0 facts)** | **GREEN** 1 processed, 4 facts |

**Test D is what cracked it.** With completely unrelated prose (dark mode, British
English, standup times) the rejection summaries *still* cited "non-ephemeral KEK
requirement for Postgres cutover" and "ephemeral KEK, in-memory graph store" — content
that appears nowhere in that episode. The agent was never reading the episode. It was
reading `graph_context_count: 3` — the pre-existing facts injected as decision context.

Test E confirms by removal: wipe the relationships and the identical episode is approved —
*"Episode queued for memory formation meets standard criteria with valid subject
identification and default instruction set."*

**Why this matters more than a prompt bug: the failure scales with success.** The formation
gate feeds existing memories to the dream agent as context, and the agent treats
unresolved/negative content in that context as grounds to refuse new formation. So the
more a scope remembers — particularly incidents, blockers and open requirements, exactly
what an engineering scope accumulates — the less able it becomes to remember anything new.
A memory system that stops learning as it fills up is self-limiting, and it degrades
silently: no error, no receipt, no negative-space entry, gateway billed per refusal.

Our own graph reached this state after **three** facts.

**D-12a — it is sentiment, not volume (test F).** Seeded with three *neutral* facts
(British English in docs, standups at 9am, dark mode) instead of three problem statements:
**GREEN**, 2 processed, 5 facts — and the verdict language inverts completely: *"Episode
qualifies for memory formation with **sufficient context facts**."* Same field, same
count, opposite conclusion. Better than volume (which would degrade every scope) but it
targets exactly what an engineering scope accumulates.

**D-12b — model capability is ruled out (test G).** The failing case with
`claude-sonnet-4-6` as the dream agent refuses identically, with the same reasoning. This
is the prompt and the design, not a weak model.

**D-14 — `MEMOTRON_DREAM_AGENT_MODEL` is silently ineffective.** `gateway.py` documents
it as the dream-agent model override, but once `memotron llm configure` seals a model
into `tenant_llm_credentials` the sealed value wins and the env var is ignored with **no
warning** — the run proceeds normally on the old model. Verified directly: with the var
set to `claude-sonnet-4-6` the transport still reports `model: claude-haiku-4-5`.
**This cost us a false negative** — the first "Sonnet" experiment had in fact run on Haiku
twice, and would have been reported as evidence that the model doesn't matter. Row G was
re-run by constructing the transport explicitly (verified `model now: claude-sonnet-4-6`).

**D-12c — third independent confirmation, and a usable workaround (test H).**
Leave the negative facts in the graph but set `context_policy.enabled=False` on the
formation job → **GREEN**, 2 processed, 3 facts. So the variable is specifically *negative
context reaching the decision prompt*, not the facts existing, not volume, not motive, not
prompt profile, not model. Three routes to green now: remove the facts (E), replace them
with benign ones (F), or stop injecting them (H).

*Workaround caveat:* context presumably also serves dedup and supersession awareness, so
disabling it is not free. Untested what it costs those paths.

**D-12d — the threshold is ONE. There is no runway.**
Using `context_policy.max_relationships` to vary how many negative facts reach the prompt,
graph unchanged:

| negative facts in context | result |
|---|---|
| 1 | **RED** — 0 processed |
| 2 | **RED** — 0 processed |
| 3 | **RED** — 0 processed |
| **1 BENIGN fact (control)** | **GREEN** — 1 processed, 3 facts |

A single unresolved-problem fact is enough. The first time a scope records a blocker, it
stops forming new memory. The benign control at the same count proves this is content,
not count.

**D-15 — RETRACTED. The gate is deterministic; I claimed otherwise from n=1.**

I recorded "the gate is non-deterministic" after seeing one episode approved and another
rejected in a single run. Replication says that is wrong — the split reproduces exactly:

| condition | runs | result |
|---|---|---|
| negative context | 5 | **RED 5/5**, processed=0 every run |
| benign context (3 facts) | 5 | **GREEN 5/5**, processed=**2** every run |
| benign context capped at 1 | 5 | **GREEN 5/5**, processed=**1** every run |

Same verdict, same count, every time. The earlier "mixed" run was the capped-context
condition behaving exactly as it always does.

**D-16 — replication: every earlier single-run finding holds.** This matters because the
whole sweep (D-12, D-12a–d) was n=1 and would have been noise if the gate wandered. It
does not. The RED and GREEN conditions are 100% reproducible over 5 runs each.

**D-17 — approvals scale with the *amount* of benign context.** 0 benign facts → 1 of 2
episodes processed; 1 benign fact → 1; 3 benign facts → 2. Consistent with the approving
verdict's own wording, *"qualifies … with **sufficient context facts**"*: the agent appears
to refuse both when context is negative **and** when it judges context insufficient. So
"context-driven refusal" is the general shape, and negative content is one trigger of it,
not the only one. Only three data points and a small graph — the monotonicity is a
hypothesis, not established.

**D-18 — suppression is scope-local, and it hits agent memory as well as project memory.**
Identical three negative facts, seeded into one scope at a time, same episodes both runs:

| negative facts seeded in | agent formation | project formation |
|---|---|---|
| **tenant** scope | processed=1, created=1 | processed=1, **created=0** |
| **agent** scope | **processed=0**, created=0 | processed=1, created=1 |

The damage lands in whichever scope holds the negative facts, and the other scope keeps
working. So this is not a project-memory quirk: **session-continuity memory
(`agent:claude-code`) degrades the same way**, independently. Blast radius is every scope
that accumulates problem statements — which, for engineering work, is all of them
eventually.

**D-19 — there are TWO silent-loss modes, not one.** They look identical from outside
(nothing was remembered, no error) but have different causes:
1. `processed=0` — the dream agent refused at the formation gate (D-12).
2. `processed=1, created=0` — extraction ran, then **everything it produced was dropped**.
   Observed live: `WS-3 motive type filter [project-memory-policy]: dropped 1 memories with
   disallowed types … ['Ryan']`. Project memory's `allowed_memory_types` excludes
   `preference` and `identity`, so personally-flavoured statements are discarded after the
   model has already been paid to extract them.

Mode 2 is arguably working as designed, but it is indistinguishable from mode 1 without
reading logs, and it still produces no negative-space entry.

**CORRECTION to D-12 notes — `project-memory-policy` IS a registered motive.** I recorded
that it was absent because it does not appear in `config.memory_bank.motives`. It is
applied from the project-memory config at refresh time, and it is visibly active in the
WS-3 filter line above. The earlier note was wrong.

**D-20 — a suppressed scope DOES recover, and the backlog drains. (Mitigating.)**
Seed 1 negative fact into `agent:claude-code` → agent formation `(processed=0, created=0)`.
`forget_memory(uuid)` that fact, refresh again → **`(processed=2, created=1)`**. The
episodes blocked during suppression were never marked processed, so they stayed in the
durable queue and formed once the blocker was removed. **Nothing is permanently lost —
memory is deferred, not destroyed.** That materially lowers the severity: this is a
liveness failure, not a data-loss one.

**D-21 — the `context_policy.enabled=False` workaround is NOT free.** `graph_context` has
two consumers in the formation path, not one:
- `dreaming.py:1959` — `context_facts` for the decision gate (the suppression source)
- `dreaming.py:2074` — `graph_context=` passed into `episode_extractor.extract(...)`, i.e.
  the **extraction prompt itself**

So disabling it stops the suppression but also blinds the extractor to what the scope
already knows. Two things survive: **dedup** (embedding-similarity at materialization,
`dreaming.py:2416-2433`, independent of context) and **entity resolution**
(`entity_inventory_for_scope`, passed separately). What is lost is the extractor's
awareness of existing facts — and consistent with that, the context-off runs (E/H)
produced near-duplicate and meaning-inverted facts side by side, e.g. `"Postgres cutover
requires a non-ephemeral KEK"` alongside `"the backend defaults to a non-ephemeral KEK"`.

Net: the workaround trades a liveness failure for a quality regression. Usable as a
stopgap, not a fix.

**D-13 — extraction quality is poor even when it runs.** The 4 facts formed in test E
include `"Ryan prefers the latest environment"` and `"the container cannot run the latest
environment"` — both garbled from the source prose, and one duplicates another with a
contradictory subject (`"the backend defaults to a non-ephemeral KEK"` vs the correct
`"requires a non-ephemeral KEK"`). Separate from D-12 and not yet investigated.

**Dead ends worth recording (each cost a probe):**
- Endorsement gate — `promotion_endorsements` = 0, but the episode has no `promotion`
  flag, so the gate never applies. *An empty table was not evidence either way; I briefly
  read it as confirming, then as refuting. It was neither.*
- Stale claims — `dream_claims` empty.
- Consumer idempotency / scope mismatch / instruction-set mismatch — all pass.
- `approved=None` — **my probe's artifact, not a defect.** `DreamDecisionRecord` has no
  `approved` field, so `.get("approved")` was always None. `parse_decision` *raises* on a
  non-bool, so a silently-unparsed approval is not possible on this path.

---

## D-22 — the fix is validated (not applied)

Withholding graph context from the **formation gate** while leaving it intact for
**extraction** fixes the suppression. Simulated by patching `DreamEngine._decide` for
`decision_type == "formation_episode_selected"` only; the real change is one line at
`src/memotron/dreaming.py:1959` (`context_facts=()`).

| condition | before | after |
|---|---|---|
| negative context | **RED 5/5**, processed=0 | **GREEN 5/5**, processed=2 |
| benign context | GREEN, processed=2 | GREEN, processed=2 — no regression |

Extraction quality is also **better than the `context_policy.enabled=False` workaround**,
as the design predicts — context still reaches the extractor, so the run produced
`"Postgres cutover requires a non-ephemeral key manager"` (correct) where the context-off
run produced the inverted `"the backend defaults to a non-ephemeral KEK"`.

**Deliberately not applied.** The bug is on `main`; this worktree is on `pr44-local`, and
putting an unrelated fix on the PR author's branch would muddle their diff. The exact diff
is in `docs/findings/formation-suppression.md`.

**What this does not settle:** the gate loses context for *every* purpose, including any
intended use nobody has written a test for. And it treats the call site — whether an LLM
veto belongs in front of extraction at all remains open.

---

## D-23 / D-24 — found by opening the UI, after a whole session of not opening it

**D-23 — the published package cannot serve its own admin console.**
`uv tool install .` then `memotron-admin-server` →
`{"error":"React admin build not found at ~/.local/share/uv/tools/jedai-memotron/lib/python3.12/ui/admin/dist"}`.
`pyproject.toml` sets `packages = ["src/memotron"]`, so `ui/admin/dist` is never in the
wheel, while `admin_server.py` defaults the static dir to a path relative to the installed
package. It resolves in a checkout and inside the image (Dockerfile copies the built UI to
`/app`, and `WORKDIR /app` makes the relative default work) — so it fails **only** for the
`uv tool` path that `SKILL.md` documents. Workaround: `--static-dir <checkout>/ui/admin/dist`.
The error message itself is good: names the missing path and the fix.

**D-24 — an empty admin console is indistinguishable from a working one.**
Ryan opened `127.0.0.1:8765` and saw all zeros. Correct output — that container mirrors the
deployed Helm command (`--tenant-id wdpr-demo`, demo fixture), while every dogfood write went
to `tenant:memotron-dogfood` in a different graph. But nothing on the page says so:
Readiness reports "Scopes ready / Platform API ready" (both true), and no panel names the
graph file or distinguishes "no data" from "wrong graph". Given the deployed API runs
`:memory:`, the production console will show this screen permanently.

**Process note.** Both of these were found within ten minutes of a human opening the UI,
after I had spent the entire session driving the system through the SDK and CLI and never
once looking at what an operator would see. Same blind spot as reasoning about a system
without running it — one level up. **Open the operator surface early.**

---

## D-25 — the MCP tools false-pass while the system forms nothing

Driven over **stdio, exactly as Claude Code invokes it** (`memotron mcp --project-root .`),
against the dogfood graph that already holds the three suppressing facts:

| probe | answer | reality |
|---|---|---|
| `tools/list` returns tools? | 27 | — |
| every tool has a description? | 0 missing | — |
| `memory_bootstrap` | `isError=False` | — |
| `memory_publish` | `isError=False` | queued into a black hole |
| `memory_refresh` | `isError=False` | formed nothing, billed the gateway |
| `memory_search` non-empty? | **returns results** | **pre-existing hand-written facts** |

Graph after: `dream-agent` facts **0**, other facts 3, episodes 5 / processed 3 /
**PENDING 2**.

This is the **atlassian false-pass** pattern mcp-forge has already been bitten by, and it is
the sharpest form of T1-1: every reasonable lenient probe passes on a server whose core
function is dead. `memory_search` returning stale facts is what makes it convincing.

**Consequence for ADR 0007:** the `jedai_memotron` validator probe must assert a **round
trip** — publish a novel fact, refresh, search for *that* fact, require it. Recorded in the
ADR.

## D-26 — tool counts, verified live, do not match the documents that cited them

*(Control: both counts taken from live `tools/list`, same branch, same build — only the server differs.)*

Both counts taken from live `tools/list` on the same branch and the same build — only the
server differs. `memotron` = **39**, `memotron-agent-memory` =
**27** (66 total). The architecture report and ADR 0007 both said 30/21/51 — `main`'s
numbers, taken from a source read. Corrected in both. This is the `registry toolcount`
invariant drifting *before* the registration file exists. **Take the count from
`tools/list`, never from a grep.** Both servers expose **0** tools without descriptions.

**Process note.** The entire MCP surface went untouched until Ryan asked whether we should
be using it — after a full session of SDK-only dogfooding. Same blind spot as D-23/D-24,
third instance: I kept testing the layer I found convenient rather than the layer users
actually touch.

---

## D-27 — systematic sweep of all 66 MCP tools

Built `scratchpad/sweep.py`: enumerates every tool on both servers, synthesises plausible
arguments from live graph state (real relationship/episode uuids harvested first), calls
each one against a **disposable graph copy**, and classifies OK / EMPTY / ERROR / SKIP.

```
governance   EMPTY=4  ERROR=16  OK=16  SKIP=3     (39 tools)
agentmem              ERROR=10  OK=16  SKIP=1     (27 tools)
```

**The 26 errors split cleanly, and the split is the finding:**

- **7 are real defects** — `AttributeError` on every call, argument-independent. The MCP
  wrapper reads fields the result models do not have (`chunk_count`→`chunks_created`,
  `run_at`/`created_at`→`ran_at`, `new_relationship_uuid`→`corrected_relationship_uuid`,
  `forgotten_relationship_uuid`→`relationship_uuid`, `held_count`→`held_relationship_uuids`,
  `signal.value`→`count`). Verified against `model_fields` for each. See T1b in the backlog.
- **19 are correct rejections** of my deliberately-sloppy arguments — wrong enum values,
  a uuid from the wrong scope, an unregistered agent, an episode that is not a promotion
  candidate. These are a **good** sign: the tools validate and the messages are precise.

**`semantic_search` returned empty for `"Postgres"` while `search` returned hits** on the
same scope and graph — almost certainly the same defect as D-09.

**One ergonomic trap:** `run_dream_job` rejects the job name that `job_state` stores.
`job_state` holds the *scoped* name (`consolidation-default [tenant/agent/scope/mode]`)
while `run_dream_job` expects the *base* name. The value you can read is not the value you
can pass back. Same root as the `dream_status` scoped-name bug already noted.

**Coverage note:** the only MCP test file is `tests/test_mcp_epochs.py` — the epoch tools
from #44. Nothing covers the other 39, which is exactly why seven dead tools shipped.

---

## D-28 — admin API sweep: 53 routes, and 18 are dead in the deployed config

25 GET + 28 POST, swept against a disposable graph on a standalone
`memotron-admin-server` (the deployed shape).

```
GET   OK=16  HTTP400=4 (correct: missing required params)  HTTP503=3  EMPTY=2 (/health 204, favicon)
POST  OK=4   HTTP400=9 (correct: missing required fields)  HTTP503=15
```

**Every `/api/platform/*` route 503s on the standalone server.** `handler.platform` defaults
to `None` (`admin_server.py:1024`); there is **no CLI flag** to set it; the sole assignment is
`local_platform.py:184`. Proven by differential with the **graph held constant** (see D-32 — an earlier version of
this comparison also varied the graph file and was re-run): standalone `:8030` → 503,
local-platform `:8022` → 200 on the identical route. That is **18 of 53 routes**, and the React UI calls two
of them (`integration-contract`, `project-memory/config`) which back the **Integration** and
**Project Memory** tabs. In the deployed console those nav items lead to broken pages.

**Good news worth recording:** the POST surface does **strict field validation** — a
kitchen-sink body is rejected with `unknown field(s): …` naming each one, rather than
silently ignoring extras. The first POST sweep returned 28/28 HTTP400 for exactly this
reason; re-running with a negotiation loop (parse the rejected names, drop, retry) gave real
coverage. Strictness like this is the opposite of the false-pass problem in D-25.

**Method note:** an error message that names what it rejected turned a useless sweep into a
self-correcting one. Worth copying elsewhere in the codebase.

---

## D-29 — SDK sweep: 140 methods, and the engine is clean

Reflection-driven sweep (`scratchpad/sdk_sweep.py`): enumerate every public method on
`Memotron` and `AgentMemoryPlatform`, synthesise **required** arguments from type hints
and parameter names, call each against a disposable graph, classify.

```
Memotron          total= 97   OK=40  EMPTY=8  ERROR=19  SKIP=30
AgentMemoryPlatform  total= 43   OK=22  EMPTY=1  ERROR= 9  SKIP=11
```

**The headline: zero code bugs in the SDK.** Every one of the 28 errors is a *correct
rejection* of a synthetic argument:

```
ValueError 25 · ValidationError 2 · ErasureVerificationError 1
AttributeError/TypeError/KeyError/IndexError/NameError: 0
```

Contrast the MCP sweep, where **seven** tools failed with `AttributeError` on any input.
That isolates the rot precisely: **the engine works; the wrappers are where the defects
are.** Good news for productionization — the expensive part is sound.

**D-29a — `semantic_search` is broken in the MCP wrapper, not the engine.** True A/B, same
graph file, same query, same moment, MCP server actively serving that file:

```
SDK.semantic_search  -> 3 results
MCP semantic_search  -> 0 results   ([])
search (both layers) -> 1 result
```

Competing explanation ruled out: it is not stale or fake LLM credentials — retested on a
clean graph copy with the real gateway key and it still returns `[]`. This also explains
D-09 (the two search paths that disagreed): the disagreement was never SDK-vs-SDK, it was
MCP-vs-SDK.

**Method note:** the SKIPs (41) are methods whose required arguments I could not synthesise
(mostly ones needing a live epoch id, receipt run uuid, or a pre-existing incident). They
are untested, not passing — worth a second pass with a richer fixture.

---

## D-30 — the UI reports failures as facts (screenshot-confirmed)

Ryan opened the **Project Memory** tab on the standalone admin server (`:8766`, dogfood
graph). It shows the 503 banner correctly — *"Memotron platform API is not enabled for
this server"* — which is honest. Then the Active-policy panel says:

```
Configured: no · Version: not configured · Project scope: none
Configured by: none · Candidates 0 · Pending dream 0
```

Verified against the same graph at the same moment:

```
/api/platform/project-memory/config -> {"error":"Memotron platform API is not enabled..."}
project_memory_status()             -> configured: True, project_scope: tenant:memotron-dogfood
```

**The graph says configured; the UI says `no`.** The fetch 503'd and the UI rendered its
defaults as answers. These are not "unknown" or "—", they are confident negatives about a
question that was never asked.

Worse in practice than a blank page: an operator would conclude project memory was never
set up, and might re-configure it — **overwriting a live policy** — via a form that is
rendered fully enabled with a working-looking *"Save project-memory version"* button that
can only ever 503.

Two cheap fixes: render an explicit unknown state when a fetch fails, and disable inputs
whose backing endpoint is unavailable.

**Third time the UI has out-produced my scripted probes.** My curl sweep found the 503;
only the rendered page showed that the 503 becomes a *lie* by the time it reaches a human.
Testing an API and testing what a user is told are different tests.

---

## D-31 — Playwright sweep of all 9 UI tabs, both server shapes

Committed `ui/admin/tests/ui-sweep.spec.ts` — an **inventory**, not a pass/fail suite: walks
every nav tab, captures console errors, failed requests, error banners, definitive-looking
negatives, enabled-button count, and a full-page screenshot. Re-runnable against any server
via `ADMIN_BASE_URL`.

| tab | standalone `:8766` (deployed shape) | local-platform `:8022` |
|---|---|---|
| **Project Memory** | 503, 1 console error, false negatives, **11** buttons | clean, **13** buttons |
| **Overview** | negatives incl. a false `not configured` | that negative absent |
| other 7 | clean | clean |

**8 of 9 tabs load with zero failed requests and zero console errors on both shapes.** For a
UI nobody has been maintaining, that is a better result than the rest of this log would
predict.

**CORRECTION — I over-claimed, and the browser caught it.** I had written that *two* tabs
lead to broken pages (Project Memory and Integration). Only **one** does. The production UI
calls exactly one platform endpoint — `/api/platform/project-memory/config` (`api.ts:91`
read, `:107` write). My earlier grep counted `integration-contract` hits that live in
`App.test.tsx`, a **test fixture**, not shipped code. So: 18 of 53 routes are dead, but the
UI only depends on 1 of them. T1b-9 corrected in the backlog.

The two-shape differential is the cleanest proof yet of T1b-9's mechanism — same build, same
graph, only the server entry point differs, and the tab flips from broken to clean with two
extra controls appearing.

**Method note:** `npx playwright test` initially failed because the installed Playwright
(1.61 / build 1228) had drifted from the cached browsers (build 1200) — **T4-9 (`"latest"`
pins) biting in practice.** Fixed with `npx playwright install chromium`.

---

## D-32 — the differential was confounded; re-run properly, it holds

Ryan asked what the difference between `:8766` and `:8022` actually was. Answering it
exposed a flaw in my own comparison — **two variables differed, not one**:

| | `:8766` | `:8022` |
|---|---|---|
| entry point | `memotron-admin-server` (standalone, deployed shape) | `memotron-local-platform` (all-in-one dev runner) |
| graph | `.memotron/dogfood.sqlite` | `/tmp/sweep/a.sqlite` — **different file**, mutated by the MCP sweep |

The `503` was always safely attributable to the entry point (it is structural —
`handler.platform` is `None` regardless of data). But the **button counts** and the
**Overview false-negative** could equally have been data.

Re-ran with the graph held constant — standalone on `:8050` against `/tmp/sweep/a.sqlite`,
the same file `:8022` serves. Now only the entry point varies:

| tab | `:8050` standalone | `:8022` local-platform |
|---|---|---|
| Project Memory | 503, console error, **11** btns, `no / not configured / none` | clean, **13** btns, `none` |
| Overview | extra `not configured` | absent |
| Integration | `not configured` | absent |

**Conclusion survives, and is now actually proven.** Also surfaced something new: the
**Integration** tab renders a false `not configured` with **zero failed requests** — it
derives that state from a *successful* call reporting the platform API unavailable, so
T4-12's "failures rendered as facts" is not limited to tabs that visibly error.

**Method note:** I ran a two-variable comparison and reported it as a controlled one. The
result happened to be right, which is the dangerous case — it would have entered the record
as proven when it was not. Hold one variable; a question from someone else is not a
substitute for doing it.

---

## D-33 — CORRECTION: Integration and Overview were reporting the truth

In D-32 I flagged the Integration tab as rendering a false `not configured`. **Wrong.**
Pulled the surrounding DOM text on both server shapes:

```
:8050 standalone      prev="MCP"  LINE="not configured"   <- Integration AND Overview
:8022 local-platform  (absent)
```

The string is the **MCP status row**, and it is **accurate**: a standalone
`memotron-admin-server` has no MCP endpoint to report, while `memotron-local-platform`
runs one on its `--mcp-port`. Both shapes report their own reality correctly. Same for the
`MCP not configured` in Ryan's very first screenshot of the compose admin.

**So the false-negative defect (T4-12) is confined to ONE tab** — Project Memory, where a
`503` is rendered as `Configured: no / not configured / none / 0`, verified false against a
graph reporting `configured: True`. T4-12 narrowed accordingly.

**This is my third UI over-claim in a row:** "two tabs broken" (was one), "Integration
renders a false negative" (it does not), and before that the confounded two-variable
comparison. The common failure: **I matched a negative-looking string to "defect" without
checking what the string referred to.** A UI negative is a claim about a specific subject —
read the label before believing the value. Recorded in STATE General rules.

---

## D-34 — wide sweep, pass 1: CLI, config surface, test coverage, architecture

**CLI is clean.** All 13 command paths respond to `--help`: 6 subcommands
(`init mcp hook status llm migrate`), 3 `llm` subcommands, 4 `hook` events. No gaps.

**Config surface — 2 of 17 env vars are undocumented anywhere.** Method: extract every
`MEMOTRON_[A-Z_]+` the code reads, then count occurrences in `README.md`, the shipped
`SKILL.md`, `.helm/` and `docs/`.

- `MEMOTRON_MCP_URL` — read by code, appears in no doc and no chart
- `MEMOTRON_PLATFORM_API_URL` — same

Both are read in `agent_memory_mcp.py`. Neither is settable through any documented path, so
an operator cannot discover them. (Separately, `MEMOTRON_DREAM_AGENT_MODEL` **is**
documented but is silently ignored once tenant credentials are sealed — D-14. Documented
and broken is a different failure from undocumented and working.)

**Test coverage — 3 modules have no test file and no test references at all:**

| module | LOC | note |
|---|---|---|
| `platform_client.py` | 443 | HTTP client for a hosted platform |
| `session.py` | 205 | `SessionIngester` — conversation turns → episodes, part of the core lifecycle |
| `identity.py` | 47 | agent id normalisation |

`session.py` is the notable one: turns→episodes is the ingestion path the hooks depend on.

**CORRECTION to my own method:** an earlier count also flagged `coherence.py` and
`multimodal.py` as untested. **False negative** — both have test files
(`test_coherence.py`, `test_ws9_multimodal.py`); my grep looked for module-path imports and
the tests import classes directly. Checked before recording.

**Architecture snapshot — the published report is now stale.** Post-merge:

```
             now      report says
src LOC      68,726   35,643
modules      58       33
test LOC     42,561   —
test files   61       25
```

`graph.py` is now a **23-line compatibility shim** — `PropertyGraphStore` moved to
`storage/sqlite.py` (5,709 LOC) behind the `StorageBackend` contract. The report describes
it as a 3,496-LOC module. `dreaming.py` roughly doubled (5,094 → 10,533).

---

## D-35 — wide sweep, pass 2: the Helm chart is in better shape than the rest

`helm template dw .helm -f .helm/values-latest.yaml` **renders cleanly — 13 objects.**
Both Deployments carry **liveness, readiness AND startup probes**, resource limits and a
container `securityContext`. For a chart nobody has been maintaining that is a good result,
and worth recording alongside the defects.

Rendered inventory: 2 Deployments, 2 Services, 2 Ingresses, 2 BackendConfigs, ConfigMap,
PVC, ServiceAccount, HPA, PDB.

**Confirmed in rendered form (previously read from source):**
- `ServiceAccount: memotron` — terraform binds `jedai-memotron` (T2-6)
- **no** `fsGroup` on either Deployment (T2-2)
- **no** NetworkPolicy object at all (T3-4)
- **no** worker Deployment (T1-2)

**REFINEMENT to the /health finding — it is admin-only.** Measured live against both
servers running the same build:

```
admin :8030/health -> 204
api   :8020/health -> 200
```

Both BackendConfigs healthcheck `/health`. GCLB requires 200 for an HTTP health check;
kubelet accepts any 2xx. So the **admin** backend would be marked unhealthy by the load
balancer while its pod reports Ready — a split-brain that presents as 502s from a "healthy"
pod. The API is unaffected. Earlier entries stated this without scoping it to admin.

## D-36 — wide sweep, pass 3: the build path

**The pipeline has 3 stages** — Pipeline Validation, Build, Deploy Helm — and **no test or
lint step** (the single keyword match is incidental, not a step). Confirms T2-4 at the
pipeline level rather than by inference.

**Image tag is derived by text-scraping the values file** (`pipeline.yaml:55`):
`grep 'tag:' "$VALUES_FILE" | head -1 | awk '{print $2}'`. It takes the **first** `tag:` in
the file, so any future key named `tag:` above `memotron.image.tag` silently changes what
gets built. Combined with the tag never being bumped (T2-5), a build overwrites the running
image.

**`.dockerignore` excludes `tests`** — so the image cannot run its own suite. That closes off
the cheapest possible smoke test (`docker run <image> pytest -q`) and is why seven
dead-on-arrival MCP tools could ship. Also excludes `.helm` and `.harness`, which is correct.

**Base images are public upstream** — `ghcr.io/astral-sh/uv:python3.12-bookworm-slim` and
`node:22-bookworm-slim`. The MCP fleet uses an internal Disney base
(`containerregistry.disney.com/...`). Expect a security-review ask to move.

---

## D-37 — wide sweep, pass 4: the intent corpus, and what "relevant" actually means

Corpus surveyed: `PATENT.md` (466), two patent specs (1,175), `NEXT.md` (361),
`DATAFLOW.md` (1,196), `AUDIT.md`, `PERSISTENCE.md`, `INGEST.md`, and `docs/design/`
(8 files, 4,322 lines).

**Q-11 has a documented answer — relevance is not hand-wavy here.** Two distinct
mechanisms, both deterministic:

**1. At write time**, relevance is a term in salience, which is a quality gate applied
post-extraction and pre-materialisation. The corpus names its lineage explicitly — the
Generative Agents formula, `salience = recency × importance × relevance`.

**2. At read time**, `retrieval.py` implements a **deterministic six-stage pipeline**:
scope/temporal filter → lexical + vector candidates → seed-node resolution → bounded
weighted beam expansion over entity and ROLLUP-member links → current-truth + governance
filter → weighted rerank. Its own header states: *"no model calls, no wall-clock reads, no
randomness"*, and the knob set is pinned into a `RetrievalContract` whose digest lands on
search receipts, so **a result set is reproducible from (graph state, contract, query,
as_of)**.

`relevance_score` (`retrieval.py:121`) is a normalised weighted mix of a **lexical** signal
(token overlap) and a **vector** signal (hashed-trigram cosine), and it **fails closed** —
both weights zero yields zero, not one.

Default weights, read from `default_config().retrieval`:

```
relevance_weight 1.0 · lexical_weight 1.0 · vector_weight 1.0
recency_weight   0.2 · recency_half_life_days 30 · confidence_weight 0.3
scope_priority_weight 0.4 · utility_weight 0.0 (off by default)
vector_min_similarity 0.35 · max_candidates 64 · beam_width 16 · max_hops 2
```

So relevance outweighs recency 5:1 by default, and the receipt-derived **utility** term is
**off** unless an operator opts a scope in.

**What this closes and what it does not.** It answers *what the mechanism is* (Q-11 was
"is there one at all?"). It does **not** answer whether the mechanism produces good results
at realistic volume — that is **U-1**, still open and still testable. Knowing there are
`max_candidates: 64` and `beam_width: 16` caps makes U-1 sharper: the question is what
happens to a relevant fact that falls outside the 64-candidate window.

**Recorded, not concluded:** the vector signal is the hashed-trigram local embedder
(256-dim), not a semantic model. Whether trigram cosine is adequate as the "relevance"
half of the mix at scale is a design question, not something this sweep can settle.

---

## D-38 — wide sweep, pass 5: repo hygiene

261 tracked files.

**No secrets are tracked.** Checked for key-shaped strings across all non-markdown tracked
files. Four hits, all benign, verified without printing any value:

- `.helm/templates/secret-operational-store.yaml`, `vault-secret.yaml`,
  `vault/vault-auth-secret.yaml` — Kubernetes Secret **templates**, not secrets.
- `tests/test_adoption.py`, `tests/test_agent_memory.py` — 19–23 char literals inside
  redaction fixtures (`content="api_key=sk-…"`, `monkeypatch.setenv("LITELLM_API_KEY", …)`).
  They are deliberately secret-shaped strings that exist to prove the **redaction code
  catches them**. A real minted gateway key is 25 chars; these are shorter and inline.

**Session artefacts are correctly ignored** — `.env`, `.memotron/`, `.mcp.json`,
`.claude/settings.json` all confirmed ignored, so nothing from the dogfood setup can be
committed by accident.

**1.4 MB of tracked binaries** — two patent PDFs (824 KB + 600 KB) and 8 patent figure PNGs.
Reasonable for a patent-bearing repo; noted for size, not flagged as a problem.

**Doc sprawl at the repo root: 10 markdown files, 6,746 lines**, before `docs/design/`
(8 files, 4,322 lines):

```
README.md 2854 · DATAFLOW.md 1196 · PATENT_REPLAY_RECEIPTS_SPEC.md 606
PATENT_COHERENCE_SPEC.md 569 · PATENT.md 466 · NEXT.md 361
INGEST.md 309 · PERSISTENCE.md 257 · AUDIT.md 118 · LOCAL_PLATFORM_TASK.md 10
```

`README.md` alone is 251 KB. This is plausibly why the gap between what is designed and what is
deployed went unnoticed for so long: the documentation is accurate and unnavigable.

**`LOCAL_PLATFORM_TASK.md` is stale scratch** — 10 lines, every item marked `[complete]`.
PR #44 deleted the `.tasks/` directory and added it to `.gitignore` but left this one
behind. Same genre, same staleness.

## D-39 · Postgres sweep — the divergence is wider than `search()`

**Method.** Ran `scripts/verify/sweep_sdk.py` twice. **Held constant:** the same script, the
same 5-fact seed (`seed_for_sweep.py`), the same method list, the same process. **Only the
storage engine differs** — `MEMOTRON_OPERATIONAL_STORE_DSN` set (Postgres, a fresh
`dwsweep` database) vs unset (SQLite, a fresh file). Diffed the per-method verdict.

**Result:** 140 methods, 4 verdict changes — `correct_memory`, `forget_memory`,
`memory_evidence`, `redact_relationship_version`, all OK → ERROR on Postgres.

**These are NOT four defects.** The harness harvests its target via
`relationships_for_scope(sc.key)[0]`. On Postgres that call returns MENTIONS edges *first*,
so all four were handed a structural edge and refused it correctly
(`ValueError: MENTIONS relationships cannot be corrected directly`). Reporting them as four
Postgres bugs would have been the exact over-claim this log exists to prevent. One defect,
four symptoms.

**Direct verification** (`parity_probe.py`, one `add_memory` into a fresh scope, same script
both sides, only the engine differs):

```
SQLite    relationships_for_scope('tenant:parity') -> 1 row   {REQUIRES: 1}
Postgres  relationships_for_scope('tenant:parity') -> 3 rows  {MENTIONS: 2, REQUIRES: 1}
                                                     MENTIONS first; scope_kind=None
```

**Then the part that matters.** Following the divergent call to its 16 call sites reached
`epoch_content_digest` (`epochs.py:774`). It filters `status == ACTIVE`; MENTIONS rows are
`active`. It keys on `truth_key or relationship.uuid`; MENTIONS rows have no `truth_key`, so
it falls back to a uuid **minted fresh at every materialization**.

Same script, same 3 facts, independent fresh stores:

```
SQLite    55f49afe...   55f49afe...   55f49afe...          3/3 IDENTICAL
Postgres  ad362abf...   44f954b4...   19a7f6f4...  09386848...   4/4 DIFFERENT
```

The digest's own docstring says it exists so that *"two INDEPENDENTLY recomputed shadows"*
can be compared — the thing `graph_state_hash` deliberately cannot do. On Postgres it
inherits the exact property it was built to avoid.

**Retracted mid-investigation.** I first read `profile()` as unaffected because both backends
returned 6,312 chars. Hashing them showed different digests — but the SQLite control run
produced a *different* profile hash from itself on a second run, so profile output is
run-varying (timestamps/uuids) and the cross-backend difference proves nothing. **Not a
finding.** Equal length was not equal content, and unequal hash was not a defect; only the
same-backend control settled it.

**Standing correction earned:** when a sweep verdict changes, check whether the harness fed
it a different input before calling the method broken.

## D-40 · T0-4 fix validated — and a harness confound caught before it became a finding

**Candidate fix:** one line, `postgres/_graph.py:370` — add `AND type <> 'MENTIONS'` so the
Postgres scoped read matches the SQLite one it already claims to match.

**Held constant:** same gate (`scripts/verify/parity_postgres.py`), same seed, same engines,
same databases-per-run policy. Only the presence of the one-line fix differs.

```
RED  (main)      postgres  rows=9  MENTIONS first  search=KeyError:'fact'
                           digests b2369943 / 8873b2a9      (non-deterministic)
GREEN (fix)      postgres  rows=3  REQUIRES first  search=OK:2
                           digests abad85699677 x4          (all 4 runs, both engines)
```

Fix reverted after measuring; `git status src/` clean. The defect is on `main`, so who lands
it and where is the same open question as T1-1.

**The confound, and why the gate earned its keep.** On the first GREEN attempt the digest
still varied (`abad8569` then `cb176ca7`), which read exactly like a second defect hiding
under the first. It was not. **My harness passed the same DSN to both Postgres runs**, so run
2 re-seeded an already-populated database — accumulation, not recomputation. Dumping the
digest's actual input tuples across two genuinely independent databases showed them
byte-identical, which is what exposed the harness rather than the code. Fixed by minting a
per-run database (`_fresh_database`), and the RED numbers above were re-measured after that
fix, so they are clean.

Worth stating plainly: the SQLite side was correct all along because each SQLite run got a
fresh tempdir. **The asymmetry in my own harness is what made a harness bug look like a
product bug** — the same shape as the four "regressed" methods in D-39.

**Standing correction:** when two arms of a comparison get their isolation from different
mechanisms, verify both mechanisms before believing the arm that differs.

## D-41 · MCP sweep on Postgres — and a correction to D-39

**Method.** Seeded an identical 5-fact graph, launched both MCP servers, swept all 66 tools.
**Held constant:** same seed script, same tool list, same harvested-uuid policy, same process
launch. **Only the storage engine differs.** Governance = `examples/mcp_server.py` (deployed
shape), agentmem = `memotron-local-platform`.

**Two harness fixes were needed first, both from the D-39 lesson.** `sweep_mcp.py` carried
*stale hardcoded uuids* from a past session, which make every uuid-taking tool fail for the
wrong reason on both sides. Added `SWEEP_*` env overrides plus `seed_for_mcp_sweep.py`, which
harvests a real relationship and **explicitly excludes MENTIONS** — on Postgres a scoped read
returns MENTIONS first, so harvesting row `[0]` hands the two backends different targets.

### The thing I nearly swept without checking

Before trusting a single result I asserted the round trip: write through MCP, then count rows
in each store. It came back **NOT CONFIRMED** — and after three probe-side false alarms (a
missing `agent_id`, an unregistered agent, and calling `register_agent` when the tool is
`agent_register`) the real answer held:

```
governance  (examples/mcp_server.py)   write -> postgres 15->18, sqlite unchanged   HONOURS DSN
agentmem    (local-platform)           write -> sqlite   0->3,  postgres unchanged  IGNORES DSN
```

Both launched from the same shell with the same exported env. The returned
`relationship_uuid` was present in the SQLite shim and **absent from Postgres**.

Isolated to the construction path, not the server — same process, back to back:

```
A. Memotron(graph_path=)      postgres 18->21   sqlite 0->0   => POSTGRES
B. build_platform_from_env()     postgres 21->21   sqlite 0->3   => SQLITE
```

Root cause: `storage_settings_from_env()` has **two** call sites, `default_config()` and
`mcp_server.py:173`. `agent_memory_config()` never sets `storage`, and `client.py:218` raises
if you pass both `control_plane` and `config`, so `base_config` is the only reachable seam.
Filed **T0-5**; one-line fix measured RED (SQLITE) -> GREEN (POSTGRES, 23->26).

### Correction to D-39

**The SDK sweep's `AgentMemoryPlatform` half was never on Postgres.** It builds its client
via `build_platform_from_env()`, so all 43 methods ran against SQLite in *both* arms. Its
"identical tally, zero verdict changes" was **not evidence of backend parity** — it is what a
comparison of a thing against itself looks like. I reported it as parity. It was a null test.
Only the 97-method `Memotron` half of D-39 was a real backend comparison.

### Sweep result, with both servers genuinely on Postgres (T0-5 fix applied)

Verified during the sweep: Postgres 15 -> 24 rows, SQLite shim stayed at **0**.

66 tools, 10 verdict changes. **Only four are structural**; the rest are state-dependent and
not claimed as defects — **the sweep mutates the store as it runs** (SQLite went 5 -> 18 rows
mid-sweep), so tools late in the sequence see a store that earlier tools changed, and that
mutation differs per backend. That is a real control weakness in `sweep_mcp.py`, not a finding.

| tool | sqlite | postgres | reading |
|---|---|---|---|
| `search` (gov) | OK | `'fact'` | T0-4 |
| `memory_search` (agentmem) | OK | `'fact'` | T0-4 on the **agent-facing** surface -> T0-6 |
| `remediate_coherence` | EMPTY | `AttributeError` | path never ran on SQLite -> T1-12, probably not backend-specific |
| `tenant_llm_configure` | OK | `wrapped DEK failed authentication (wrong KEK or tampered)` | **B1/T0-2 reproducing live** |

Not claimed: `entity_neighborhood`, `memory_utility`, `retrieval_negative_space`,
`tenant_llm_status` (OK->EMPTY) and `memory_evidence`/`run_coherence_scan` (-> better on
Postgres). All sit downstream of the mutation weakness above.

**Open:** the `wrapped DEK` failure is the ephemeral-KEK blocker (B1) showing up in practice
rather than in theory, but I did **not** isolate its mechanism. **CLOSES WITH:** a dedicated
probe that seals a tenant credential, restarts the process, and re-reads it.

## D-42 · B1/T0-2 reproduced — and it is worse than the plan says

**Question:** was the `wrapped DEK failed authentication` in D-41 the ephemeral-KEK design
(B1), or an artifact of my two servers sharing one `.kek` shim file?

**Method** (`scripts/verify/probe_kek.py`). Three arms, **one variable each**, same DSN, same
tenant, same sentinel credential:

| arm | changes | postgres | sqlite |
|---|---|---|---|
| A seal + use, one process | — | OK, 32-byte DEK | OK |
| B fresh process, **same `graph_path`, same `.kek`** | process identity | **FAIL** | OK |
| C fresh process, fresh filesystem | + filesystem | **FAIL** | (new store, not comparable) |

```
ValueError: wrapped DEK failed authentication (wrong KEK or tampered)
  postgres/_governance.py:304  _require_live_key -> key_manager.unwrap_dek(wrapped)
  crypto.py:251
```

**Arm B is the one that settles it.** Same graph path, same `.kek` file on disk, and it still
fails — so the competing "the two servers clobbered one KEK file" explanation is dead. The
Postgres backend never reads that file; `storage/postgres/__init__.py:113` takes
`LocalKeyManager.ephemeral()`, whose own docstring says *"keys die with the process"*.

**The plan understated it in one way and overstated it in another.**

- *Worse:* this is **not multi-replica-only**. A single process restart is enough — arm B had
  one machine, one filesystem, one KEK file. The plan's "and by A after any restart" was
  right; the two-replica framing everywhere else buries it.
- *Narrower:* the blast radius is `get_or_create_governance_key` — governed (crypto-shred)
  scopes and tenant LLM credentials. **Ordinary graph rows are not affected on the default
  path**; `postgres/_graph.py` never references `key_manager`. "All sealed content" is right
  only for scopes that opted into governance.

**And it is silent.** After the unwrap failure, `tenant_llm_credential_state` still returns
`has_api_key: True`. A status page, a health check, or an operator reads *configured* while
the credential cannot be used. That is the part that makes this dangerous rather than merely
broken — the system does not know it has lost the key.

**Probe bug caught mid-run, worth recording.** My first version asserted on
`tenant_llm_credential_state`, which returns **metadata and never unwraps the DEK** — so all
three Postgres arms passed and the probe printed "NOT B1". I only caught it because a second
run against the already-populated database failed in arm A, which made no sense if the first
verdict were true. Same class of error as asserting on `memory_refresh` when `memory_start`
is the context provider: **the call I reached for reported on the thing instead of doing it.**
The corrected probe drives `provision_governance_key` -> `_require_live_key`.

**Consequence for the plan:** D1.1 (`GcpKmsKeyManager` + the fail-closed guard) is no longer
a precaution against a theorised failure. It is a fix for a reproduced one, with a gate that
goes 1 -> 0 when it lands.

## D-43 · All three Tier-0 fixes applied together — what else breaks?

**Why.** Each fix was measured alone. Applying them together is a different question: do they
interact, and what collateral damage does the fail-closed guard cause? I predicted last
session that the guard would break the 117-test parity suite and every local Postgres run,
because both use the ephemeral KEK by design. That prediction is falsifiable — this is the
test of it. **Local only; nothing is being filed or pushed.**

Completed the gate trio first: `probe_storage_wiring.py` promotes the T0-5 store-attribution
probe (which lived in scratch) into `scripts/verify/`, so all three defects now have a
re-runnable local gate.

**Baseline, unmodified `main` — all three gates correctly RED:**

```
T0-4 parity_postgres        exit=1
T0-5 probe_storage_wiring   exit=1   build_platform_from_env() wrote to SQLITE with a Postgres DSN set
T0-2 probe_kek              exit=1
```

**Fixes applied together, all three at once** (`docs/findings/tier0-fixes.patch`, 65 lines,
local-only; reverted after measuring, `src/` clean):

| gate | main | fixes + opt-out |
|---|---|---|
| T0-4 `parity_postgres` | 1 | **0** |
| T0-5 `probe_storage_wiring` | 1 | **0** |
| T0-2 `probe_kek` | 1 | 1 — correct: the opt-out deliberately re-enables the ephemeral KEK |

The guard itself was verified separately: with no opt-out, constructing a Postgres backend
raises `refusing to build a Postgres storage backend with an ephemeral KEK...`.

**Full suite, live postgres:16, one variable at a time:**

| config | result |
|---|---|
| unmodified `main` | 1 failed, 1070 passed, 1 skipped |
| + 3 fixes, **no** opt-out | **3 failed, 1002 passed, 66 errors** |
| + 3 fixes, **with** opt-out | 1 failed, 1070 passed, 1 skipped — **identical to baseline** |

**The prediction held.** I said the fail-closed guard would break the parity suite because it
uses the ephemeral KEK by design. It does: 66 errors, almost all in
`test_storage_backend_parity.py`. So the guard is not a two-line change in practice — it needs
`MEMOTRON_ALLOW_EPHEMERAL_KEK` threaded into the test fixtures and
`docker-compose.local.yml`, and deliberately absent from the chart.

**T0-4 and T0-5 cause zero regressions** across 1,071 tests. That is worth stating precisely:
the 117-test parity suite still passes **after** adding the MENTIONS filter, which means it
never asserted the buggy behaviour — it simply never looked. Independent confirmation, from a
second direction, of the D-39 conclusion that the suite tests stored bytes rather than the
caller's view.

## D-44 · `main` has a deterministically failing test, and nothing would catch it

Found while establishing the baseline above, **not** caused by any fix.

`tests/test_adoption.py::test_post_compact_hook_persists_only_sanitized_continuity` fails on
`HEAD == origin/main == f28e95c` with **zero tracked changes**:

```
state = next(item for item in relationships if item.type == "HAS_STATE")
StopIteration          tests/test_adoption.py:354
```

The post-compact hook stores its episode, but rule-based extraction yields no `HAS_STATE`
row, so the test's `next()` has nothing to take.

**Deterministic, not flaky:** 3/3 consecutive runs fail; fails in isolation, within its own
file, hermetically, and with Postgres. Fails **identically with and without** the three
fixes, so it is pre-existing. `tests/test_adoption.py` was last touched by **f28e95c — PR #44
itself**, the commit just merged.

**This is exactly the failure mode B6 predicts.** There is no test CI anywhere: no GitHub
Actions, no Harness test stage, and the only PR check is GitGuardian and it is not required.
So a merge can put a red test on `main` and nobody learns. I told the PR author in review that
"1068 passed" was unfalsifiable without CI; this is what that costs.

**Not claimed:** that the test passed before the merge. My earlier hermetic run recorded
1003 passed / 0 failed, but I did not preserve its commit, and the pre-merge branch tip and
the merge result are different trees. **ASSUMED, unverified.** What is observed is only that
it fails on `main` today. **CLOSES WITH:** `git stash`-free worktree checkout of `47790d4`
and the PR-branch tip, running that one test on each.

## D-45 · **RETRACTION of D-44 / T0-3b** — `main` is not red. My worktree is.

**D-44 claimed `main` has a deterministically failing test. That claim is wrong.** I am
leaving D-44 in place rather than editing it, so the mistake stays visible.

**What I actually verified, and where the reasoning failed.** D-44's evidence was real: 3/3
deterministic failures, in isolation, hermetic and with Postgres, with and without the three
fixes, on `HEAD == origin/main` with zero tracked changes. Every one of those checks varied
something *inside* the repo. **Not one of them varied the working directory**, so all of them
were blind to the actual cause. "Zero tracked changes" felt like it ruled out local state; it
only ruled out *tracked* local state.

**The check that broke it.** Same test at three commits, in fresh worktrees:

```
9bfc298  pre-merge main       PASS
d8537e1  PR #44 branch tip    PASS
f28e95c  main today           PASS  (in a clean worktree)   <-- FAILS in mine
```

Then the structural fact that ended the code hypothesis outright: `f28e95c^{tree}` and
`d8537e1^{tree}` are **the same hash** (`93be4e5e...`). Identical tree, opposite results. It
was never the code.

**Isolation, one variable at a time, in my worktree:**

| condition | result |
|---|---|
| all present | 1 failed, 11.1s |
| **`.env` moved aside** | **1 passed, 1.2s** |
| `.memotron.yaml` moved aside | 1 failed, 10.6s |
| `.memotron/` moved aside | 1 failed, 8.9s |

Ruled out first: `psycopg` (the only package differing between the two worktrees) — installing
the `postgres` extra in the clean worktree left it passing.

**The real finding, filed as T1-14.** `build_transports_from_env` calls the bare
`load_env_file()`, and `DEFAULT_ENV_FILE = ".env"` is **cwd-relative** (`runtime.py:34,92,177`;
same at `:232`, `:320`, `:385`). It writes into `os.environ`, so #44's `monkeypatch.delenv`
hardening cannot work — deleting the variable does not stop the loader re-reading it from disk.
The 11.1s vs 1.2s runtime is the signature: the `.env` silently promotes deterministic
rule-based extraction to a **gateway-billed network call**.

That is worth more than the bug I thought I had: **a Memotron process takes LLM credentials
from whatever directory it is launched in.** An operator running the CLI from a directory with
an unrelated `.env` starts spending without being asked.

**Observed but NOT explained:** under the gateway path, extraction returned
`relationships = []` — zero memories from a real post-compact episode, where rule-based
produced the expected `HAS_STATE`. Competing explanation I have **not** ruled out: the gateway
call simply failed and the error was swallowed (11s smells like timeout/retry). **CLOSES
WITH:** run the same episode through gateway extraction with the response logged.

**Standing correction:** *repeating a test many times measures flakiness, not causation.*
Three deterministic runs made me more confident and no better informed. The variable I never
touched was the one that mattered — and "deterministic" was the very thing that should have
pointed at stable ambient state rather than at the code.

## D-46 · The D-45 loose end closes: gateway extraction is fine. Not a defect.

**Question:** D-45 observed `relationships = []` from a post-compact episode under the
gateway transport, where rule-based produced `HAS_STATE`. H1 call fails / H2 model returns
nothing / H3 candidates rejected by the validator.

**H1 ruled out at the transport layer.** `POST $LITELLM_API_BASE/chat/completions` -> **200**
with the real key. (`/v1/chat/completions` -> 404: the base URL already carries `/v1`, worth
knowing before anyone debugs a gateway 404.)

**H2 vs H3 settled by the system's own audit trail**, running the real hook flow in a
throwaway project — `scripts/verify/probe_compaction_extraction.py`. A full 2x2 across
transport x episode text, so each pairwise comparison changes exactly one thing: reading down
a column, the transport is **held constant** and only the text differs; reading across a row,
the text is **held constant** and only the transport differs. Same code, same process, same
freshly-initialised project per arm:

| compact_summary | rule-based | gateway |
|---|---|---|
| the test's own one-liner | 3 — `{MENTIONS: 2, HAS_STATE: 1}` | **0** |
| a realistic compaction summary | 3 — `{MENTIONS: 2, HAS_STATE: 1}` | **9** — `{MENTIONS: 6, REQUIRES: 1, HAS_STATE: 2}` |

**Zero rejection receipts in every arm**, so it is H2, not H3 — the model returned nothing;
nothing was rejected.

**Verdict: gateway extraction is not broken. It is better.** On a realistic summary it found
9 relationships to rule-based's 3, including the `REQUIRES` for "the KEK must come from Cloud
KMS" — a genuine requirement rule-based missed entirely.

The zero comes from the **input**, not the transport. The test's `compact_summary` is one
line and most of it — `password=do-not-store-this`, `engineer@example.com` — is stripped by
sanitization *before* extraction runs. What reaches the model is a near-contentless fragment,
and it declines to invent memories from it. The rule-based extractor fires regardless. **That
is defensible behaviour on both sides**; it just makes the test fragile to which transport is
in play, which is T1-14, already filed.

**Second observation, worth more than the thing I was chasing.** Rule-based extraction
returned **exactly 3 relationships on both inputs** — identical shape on a one-line fragment
and on a four-fact paragraph. It looks close to input-insensitive: a fixed skeleton rather
than extraction. **n=2, so this is a signal, not a measurement.** It bears directly on the
plan's open decision #3 ("at the budget ceiling, degrade to no-LLM formation or stop
dreaming?") — if no-LLM formation extracts a constant regardless of content, "degrade
gracefully" is not what would actually happen. Filed as **U-12**.

**Harness note:** `initialize_claude_code_project` requires a git remote named `origin`
(`adoption.py:214`) — a bare `git init` fails with `cannot infer project identity`. Any probe
building a throwaway project must add one.

## D-47 · U-12 closed — the no-LLM path does not extract, it captures

**Held constant:** same code, same hook, a freshly-initialised throwaway project per arm,
same scope. A 6x2 grid crosses episode text against transport, so every pairwise comparison
changes exactly one thing. `scripts/verify/probe_extraction_density.py`.

**Counts MEMORIES, not relationships.** `MENTIONS` edges are structural, emitted per entity,
so a raw relationship count rises with the number of nouns even when nothing was understood.
Counting them is what made D-46's table read "3 vs 3" — that framing was wrong, and the
corrected figures are below.

| distinct facts in episode | rule-based | gateway |
|---|---|---|
| 0 | **1** | 0 |
| 1 | **1** | 1 |
| 2 | **1** | 2 |
| 4 | **1** | 4 |
| 6 | **1** | 5 |
| 8 | **1** | 6 |

**rule-based spread 0. gateway spread 6.**

**What the rule-based path actually writes**, seen directly rather than inferred — the whole
episode body, verbatim, as one `HAS_STATE` fact:

```
0 facts  ( 90 chars): 'claude-code has state Okay. Right, got it. Sounds good, thanks. Let us pick this up later.'
8 facts  (692 chars): 'claude-code has state We decided the Memotron operational store will be Postgres, not
                       SQLite. Ryan requires that the KEK come from Cloud KMS ... how budget problems
                       get discovered late.'
```

The 692-char fact is the 670-char episode plus a `claude-code has state ` prefix. **Nothing
is truncated.**

**Caught mid-analysis:** I was about to report that the fact was truncated mid-sentence at
~110 chars. That truncation was **my own probe's** `str(f)[:110]`, not the system's. Removed
the cap and re-ran before writing anything down. A probe's display limit is not a finding.

**This is NOT a bug — it is continuity capture working as designed.** The adoption test
asserts `"simple mode" in state.properties["fact"]`, i.e. it *expects* the episode body
inside the fact. So the honest statement of U-12's answer is not "rule-based extraction is
broken" but: **the no-LLM path does not extract; it stores the sanitized episode verbatim as
a single state fact.**

**Why that matters for plan open decision #3** ("at the budget ceiling, degrade to no-LLM
formation, or stop dreaming?"). Degrading does not mean *worse extraction*, it means *no
extraction*:

1. One verbatim blob per episode regardless of content — including for an episode carrying
   nothing durable ("Okay. Right, got it."). No salience filter.
2. The blob grows with episode size and becomes one indivisible retrieval unit competing for
   the context budget.
3. ~~**DERIVED, not observed:** a verbatim blob has no canonical predicate, so supersession
   and dedup cannot operate on anything the no-LLM path writes.~~ **REFUTED by D-48** — the
   truth key is subject + canonical predicate (`agent:claude-code:claude-code:has state`), so
   both writes hit the same slot and the incumbent is correctly superseded. Left struck
   through rather than deleted: the derivation was reasonable, testable, and wrong.

**Gateway numbers are not exact.** Repeat runs gave 3 then 4 at 4 facts, and 7 then 6 at 8
facts — LLM nondeterminism. The *shape* (monotone with density, 0 on empty) held across both
runs; the individual counts should not be quoted as measurements.

Worth stating plainly: on the 4-fact episode the gateway wrote `'Ryan requires KEK'` and
`'parity gate must run before Memotron operational store'` — real, typed, individually
supersedable facts. That is the product working.

## D-48 · **Retracts D-47's derived claim** — supersession works on no-LLM writes

**D-47 derived** that a verbatim blob has no canonical predicate for the truth slot to key
on, so nothing the no-LLM path writes could ever be superseded. **That was wrong.** I labelled
it DERIVED and gave it a closing test; this is that test, and it refutes it.

**Held constant:** one project, one graph, one scope, rule-based transport only (credentials
stripped, neutral cwd so T1-14 cannot re-inject them). The only thing that varies between the
two writes is the episode text, and the second contradicts the first.

```
ep1 (505 chars)  contains 'Postgres, not SQLite'
ep2 (559 chars)  contains 'stay SQLite, not Postgres', 'reversed'

before run_due_dreams:  1 row   active      observed_count=1   "...will be Postgres, not SQLite"
after  run_due_dreams:  2 rows  superseded  observed_count=2   "...will be Postgres, not SQLite"
                                active      observed_count=1   "...We reversed that decision..."
```

The truth key is `agent:claude-code:claude-code:has state` — **subject plus canonical
predicate** — so both writes land in the same slot and the incumbent is correctly closed. The
superseded row is retained: invalidate-don't-delete, exactly as designed. My reasoning error
was assuming the *object* had to be a clean fact for the slot to work; the slot keys on
subject+predicate, and the no-LLM path supplies both.

**I nearly filed data loss.** The pre-dream snapshot showed one row, `observed_count=1`,
carrying only the first summary — which reads exactly like "the newer contradicting state was
silently discarded". Before writing that down I checked the nearest competing explanation:
formation is **deferred, not lost** — the same pattern already established for T1-1. Draining
the queue produced the correct result. The probe's own first verdict ("C: deduped") was wrong
for the same reason, and now judges post-dream state only.

**The real finding is the timing, and it sharpens B5.** Nothing forms at hook time. Until
`run_due_dreams()` executes, the graph keeps asserting the **stale** fact — this probe watched
it serve "Postgres" after the user had already said "SQLite". **No dream worker exists on
`main`.** So the consequence of B5 is not "memory goes stale"; it is **memory actively serves
superseded facts, indefinitely, while reporting them as `active`.** That is the strongest
argument yet for D4 (the worker) and it now has a reproduction.

**Observed, unexplained:** the *first* episode formed without an explicit `run_due_dreams`,
the second did not. Plausibly the formation job's `cadence_seconds=1` fired for the first and
the second fell inside the window. **NOT verified. CLOSES WITH:** run three hooks with a
recorded sleep between each and record which form without an explicit drain.

**Net for the no-LLM path** (correcting D-47's framing): it is better behaved than I claimed.
It captures verbatim rather than extracting — that part stands, measured — but the result is a
**bounded rolling latest-state record with proper supersession and retained history**, not an
unsupersedable accumulating blob.

## D-49 · U-14 closed — T0-4 does NOT break supersession. And T0-5 is wider than filed.

**Two results this morning, one reassuring and one worse.**

### The reassuring one: supersession survives T0-4

**Held constant:** one client, one scope, the same two contradicting facts in the same order,
client-managed writes (no LLM). The only variable is the storage engine.

```
sqlite     SQLiteStorageBackend      1 superseded + 1 active, truth_key 'tenant:u14:the operational store:will be'
postgres   PostgresStorageBackend    1 superseded + 1 active, SAME truth_key
           postgres relationships 0 -> 6   (write attributed)
```

**DERIVED mechanism:** the extra MENTIONS rows Postgres leaks into
`relationships_for_scope` carry `truth_key=None`, so they cannot collide with a real slot key.
They are noise in the candidate set, not false matches. That is why a defect loud enough to
raise `KeyError` in `search()` is silent and harmless here.

**So the feared failure mode does not occur:** no arm left two contradictory rows `active`.
This is the reassurance the Postgres cutover needed on the one path that matters most.

**Observed, unexplained:** the superseded row has **no `valid_to` set** on either backend,
though the documented contract is that supersession closes `valid_to` at the challenger's
`valid_from`. Possibly because both writes share a `valid_from` instant, or because the
operator path (`add_memory`) supersedes differently from formation. **NOT a defect claim.**
**CLOSES WITH:** repeat with an explicit `valid_from` gap between the two writes and re-read
`valid_to`.

### The worse one: T0-5 covers the Claude Code hook path

My first attempt at U-14 reused `probe_nollm_supersession.py`, which goes through
`build_platform_from_project`. It reported a clean Postgres pass. **It was a null test.** The
target database had **no `relationships` table at all** afterwards — migrations never ran, so
no Postgres backend was ever constructed. Caught by attributing the write rather than trusting
the DSN, which is now the third time that check has saved a false conclusion (D-41, D-45, here).

`build_platform_from_project` (`adoption.py:316`) calls `AgentMemoryPlatform.create(graph_path=...)`
with **no storage seam** — the same gap as `build_platform_from_env`. That entry point is what
**every session-start and post-compact hook** runs through, so T0-5's blast radius is not just
the 27 MCP tools and the SDK facade: it is **the product's actual Claude Code integration**.
Backlog updated.

**Consequence for the plan:** flipping `operationalStore.enabled` would leave the entire
compaction/context path — the thing users depend on — writing to a per-pod SQLite file, while
the governance MCP server correctly used Postgres. Two halves of one deployment silently
disagreeing about where memory lives is worse than either being wrong.

## D-50 · U-13 closed — and it retracts T1-15's framing, which was mine

**Held constant:** same project, same graph, same scope, rule-based transport, same three
episode shapes, in the same order. `run_due_dreams()` is **never** called. The only variable is
the wall-clock gap between hooks.

```
gap=0s   episodes after each hook: [1, 2, 3]   HAS_STATE after each hook: [1, 1, 1]   -> 1 of 3 formed
gap=3s   episodes after each hook: [1, 2, 3]   HAS_STATE after each hook: [1, 2, 3]   -> 3 of 3 formed
```

**The cadence hypothesis holds.** Formation advances on wall clock via the formation job's
`cadence_seconds=1`: a hook whose dream is due forms its own episode inline, with no worker and
no explicit drain.

### The retraction

Yesterday I filed **T1-15** as *"without the dream worker, memory actively serves superseded
facts indefinitely while labelling them `active`"*, and called it the strongest argument yet for
D4. **That framing was wrong.** Direct test, gap the only variable, no drain:

```
gap=0s  before drain: 1 row  active      (the OLD fact) <- what D-48 saw
gap=3s  before drain: 2 rows superseded + active        <- the CORRECT state, already
```

D-48 ran its two hooks **back-to-back**, so hook 2 fell inside the 1s cadence window and its
episode stalled. I read a property of my own probe's timing as a property of the product, and
then escalated it. Real compactions are minutes apart, so in normal use each hook forms its
own episode immediately.

**What survives:** an episode whose hook fires inside the cadence window stalls until the next
hook, so the last episode of a burst can sit unformed. A short window, not a permanent one.

**What still argues for D4 (unchanged):** consolidation (`cadence_seconds=300`), pruning and
coherence will not fire reliably from hook traffic, and a scope with no hook activity never
dreams at all. The worker is still needed — just not for the reason I gave.

**Pattern worth naming, because this is the second time in two days.** Both retractions
(D-45, this one) came from the same error: my harness created timing or environment conditions
that do not occur in use, and I attributed the result to the product. D-45 was a `.env` in the
cwd; this was zero delay between hooks. **The probe's conditions are part of the claim, and
"realistic" is a property I have to check explicitly, not assume.**

## D-51 · The three Tier-0 fixes are now repo tests, RED then GREEN

**Why this step mattered.** Every gate built during discovery lives in `scripts/verify/`,
which is in `.git/info/exclude`. They proved the defects and could protect nothing. These are
tracked files: they gate a PR.

**Discipline followed deliberately** — the same one I asked PR #44's author for: each defect
lands as a regression test **before** its fix, so the red is a matter of record.

### RED, on unmodified `main`, live postgres:16

```
tests/test_scoped_read_parity.py          4 failed, 3 passed
  FAILED test_scoped_read_excludes_structural_mentions_edges[postgres]
  FAILED test_scoped_read_first_row_is_a_memory[postgres]
  FAILED test_search_returns_memories[postgres]
  FAILED test_epoch_content_digest_is_stable_across_recomputation

tests/test_operational_store_wiring.py    2 failed, 2 passed
  FAILED test_agent_memory_platform_writes_to_the_operational_store
  FAILED test_postgres_backend_refuses_an_ephemeral_kek
```

**Every `sqlite` arm passed in the red run.** That is the control: it proves the tests are
measuring the engine, not simply broken. And hermetic (no DSN) gave `3 passed, 4 skipped`, so
the offline suite stays green with no database and no network.

### GREEN, with `docs/findings/tier0-fixes.patch` applied

`11 passed`, and the full suite:

| run | result | baseline | delta |
|---|---|---|---|
| live Postgres | 1 failed, **1081 passed**, 1 skipped | 1070 passed | **+11, zero regressions** |
| hermetic | 1 failed, **1005 passed**, 77 skipped | 1002 passed | **+3, zero regressions** |

+11 and +3 are exactly the new tests. The single failure is the **pre-existing** T1-14 — this
worktree has a `.env`, which `runtime.py:177` loads cwd-relative; it passes in a clean checkout
(verified D-45).

### The design decision worth reviewing

The fail-closed KEK guard broke **66 tests** when I measured it yesterday, because the suite
seals with the ephemeral key by design. I resisted patching each file and put the opt-out in a
new `tests/conftest.py` as one autouse fixture — a single reviewable place that states *why*
the suite is the legitimate exception (throwaway database, seal, assert, drop, all inside one
process). It uses `monkeypatch` so nothing leaks between tests, and defers to an
already-set outer value so a developer's shell and the local compose stack are untouched. Two
tests in `test_operational_store_wiring.py` override it deliberately to exercise the guard.

### Change set — 6 files, nothing committed

```
M src/memotron/agent_memory.py                  (T0-5, 2 lines)
M src/memotron/storage/postgres/__init__.py     (T0-2 guard, 14 lines)
M src/memotron/storage/postgres/_graph.py       (T0-4, 1 line)
? tests/conftest.py
? tests/test_operational_store_wiring.py
? tests/test_scoped_read_parity.py
```

**Left in the working tree, uncommitted** — Ryan does his own commits. Note these are the
first changes of the takeover that are *meant* for the repo, so unlike every other artefact
they are deliberately **not** in `.git/info/exclude`.

**Still true and unchanged:** none of this is enforceable until Workstream C exists. There is
no test CI, and GitGuardian — the only PR check — is not required. These tests make the fixes
reviewable and regression-proof for anyone who runs them; they do not yet gate a merge.

## D-52 · Decision: the three Tier-0 fixes and their tests stay in the repo

Ryan, 2026-08-27, on the grounds that they are *"the best verified findings we have."* This
moves the discovery-phase boundary, so the boundary is now written down (STATE.md, working
memory) rather than implicit:

- **Eligible for the repo:** measured by more than one independent path, demonstrated
  RED -> GREEN, behaviour **attributed** rather than inferred. T0-4, T0-5, T0-2 clear this.
- **Not eligible:** single-probe findings, or anything resting on timing or ambient
  environment — the class both of this week's retractions (D-45, D-50) came from.

The asymmetry that justifies the bar: a log entry is cheap to retract; a test ships an
assertion *and* a docstring explaining why it is true, and the next reader inherits both.

Change set unchanged and still uncommitted: 3 source fixes + `tests/conftest.py` +
`tests/test_scoped_read_parity.py` + `tests/test_operational_store_wiring.py`.

## D-53 · Discovery record published — issue #45, draft PR #46

The takeover artefacts are no longer local-only. `.git/info/exclude` cleared (with a note
explaining why), 43 files committed in two deliberately separate commits:

- **`29624cb` fix** — the three Tier-0 defects with their regression tests. The only source
  changes, and the commit to cherry-pick when real work branches off `main`.
- **`79e299c` docs** — the register, 17 harnesses, the methodology, `DISCOVERY.md` as a front
  door. Reference only.

`DISCOVERY.md` states plainly that the branch is not meant to merge, indexes where each kind of
knowledge lives, and keeps the three retractions visible — they are what tell a reader which
claims to trust.

**Safety pass before staging**, since this material had never left the machine:

- `.memotron/` (16M of SQLite graphs **plus `.kek` key material**) is covered by the repo's
  own `.gitignore:3` — verified with `git check-ignore`, not assumed
- `.env` gitignored at `.gitignore:1`
- all 43 staged paths scanned for `sk-`/`Bearer`/`AKIA`/PEM headers: **none**; all text, zero
  binaries; committed tree contains **0** `.kek`/`.sqlite`/`.env` files
- the only credential-shaped literals are `POSTGRES_PASSWORD: local-dev-only` (throwaway) and
  `LITELLM_API_KEY: "${LITELLM_API_KEY:-}"` (indirection, no value)

Used `Refs #45` rather than the house `Closes #N`, because this PR is explicitly never merging
and `Closes` would either mislead or silently close the issue.

### The push itself found a defect

The remote reported an open **HIGH** Dependabot alert on the default branch:
`nanoid 3.3.17 < 3.3.18` in `ui/admin/package-lock.json`, transitive via `postcss`. Filed
**T3-6**. Practical exploitability looks low (needs a custom generator called with `size=0`,
which the admin UI does not do) but it is HIGH by advisory and will block any gate reading
Dependabot.

Worth noting *why* it is awkward rather than trivial: **T4-9** pins every direct dependency in
`ui/admin/package.json` to `"latest"`, so the lockfile cannot be refreshed without silently
moving React, Vite and TypeScript in the same commit. Pins first, then the lockfile. Two
inherited defects that only bite together — which is the sort of thing the register exists to
hold.

## D-54 · Infra work split out, and the old INFRA tag was wrong

Ryan: the infra team will own infrastructure, so it needs its own category outside the code
register, modelled on the artifact's Infrastructure inventory section.

**The boundary that made this tractable: who can land the change**, not whether the item is
deployment-related.

- theirs — `tf-jennay`, Vault, a GCP console/API, Harness configuration, gateway virtual keys
- ours — `src/`, `tests/`, `Dockerfile`, `.helm/`, `.harness/pipeline.yaml`

**The old `INFRA` tag conflated those two things**, and three of its ten items are ours:
**T2-1** (Dockerfile uid/cache), **T2-2** (missing `fsGroup` in `.helm/`), **T2-5** (image tag
in values). All three land in our repo via our PR. Retagged.

**The other gap: a defect register is not an inventory.** I had only logged things that are
*wrong*; an infra team also needs what must *exist*. `INFRA-BACKLOG.md` therefore carries both
— a provisioning inventory with EXISTS / PARTIAL / MISSING / UNKNOWN per component, then the
defects only they can close, then a **Coordinated** table for the eight items that need both
sides and fail when each assumes the other has it.

**Verified against `tf-jennay` before handing anything over** (not carried from memory):

| claim | result |
|---|---|
| `grep -rl google_kms tf-jennay` | **nothing, all four envs** — KMS is greenfield |
| memotron Cloud SQL instance/database | **none**; `cloudsql_*` structures exist for other apps |
| memotron VIPs in `adresses.tfvars` | **one** (`:225`, the API host); admin host absent |
| WI binding | the **same single binding** read from each side that declares it, so the subject is held constant and only the source differs: chart says KSA `memotron` + `svc-memotron@` (`values-latest.yaml:212-213`); terraform says GSA `svc-jedai-memotron`, KSA `jedai-memotron` (`iam_svc.tfvars:324-327`). **Both halves disagree** |
| `NetworkPolicy` template | **absent** from `.helm/templates/` |

The WI check produced something actionable I did not have before: terraform's naming already
matches the platform's `svc-mcp-jedai-*` convention and the GSA is listed at
`iam_svc.tfvars:2`, so **the chart is the side that should change** — which turns a two-sided
ambiguity into a one-line confirmation request.

**A register bug fell out of the audit:** `T3-6` had been used **twice** — once for
read-auditing/legal-hold, once for the Dependabot alert. Renumbered the latter to **T4-13**.
Worth noting the finding gate did not catch this; it lints evidence and hedging, not identity
uniqueness. Added to the duplicate check I now run manually:
`grep -oE '^\| \*\*T[0-9b]+-[0-9]+\*\*' TAKEOVER-BACKLOG.md | sort | uniq -d`.

## D-55 · The crypto-shred headline holds. "Byte replay" means something narrower than advertised.

**Why this probe existed.** `WALKTHROUGH.md` leads with the system's strongest claim — *destroy
the key, content becomes unreadable, every hash still verifies* — and I had only ever **read**
that, in the code and the design docs. Presenting it unverified the day before a review would be
the exact mistake already made twice (D-45, D-50). `scripts/verify/probe_shred_audit.py`.

### The headline survives contact — verified, not read

Same graph, `ErasureBehavior.CRYPTO_SHRED`, three sealed facts:

| check | result |
|---|---|
| content readable before | PASS |
| content unreadable after the shred | PASS — 0 hits, 0 leaking plaintext |
| **every run's hash chain still verifies** | **PASS** — 3 runs, 3 receipts |
| no receipt lost to the shred | PASS — 3 -> 3 |
| **every merkle root unchanged** | **PASS** — 0 changed |
| erasure certificate verifies | PASS |
| the shred broke no run that previously replayed | PASS — before=2 after=2, **newly broken=[]** |

**The shred is exonerated on every count.** This is now the only headline claim in the
walkthrough backed by a run rather than a reading.

### Strengthening the probe turned a PASS into a FAIL

The first version verified `runs[0]` only and reported a clean sweep. Two weaknesses in my own
test:

1. **A single-receipt chain has no `previous_receipt_hash` link**, so "the chain verifies" was
   near-vacuous. Now every run is verified and the count of runs with >1 receipt is printed, so
   the strength of the check is visible rather than assumed. It currently reads **0** — worth
   knowing.
2. **The plaintext-on-disk check was vacuous.** Content is sealed from the first write, so
   "absent after the shred" proves nothing about the shred. The probe now says `n/a` and
   explains why instead of banking the free PASS.

Verifying *all* runs surfaced: **2 of 3 failed byte-replay.**

### Attribution, before blaming the shred

The "before" arm had also only replayed one run. Making it symmetric settled it:
`before=2, after=2, newly broken=[]` — **the same two runs already failed before the shred.**
Had I not fixed that asymmetry I would have reported crypto-shred as breaking replay, which is
false.

### What is actually going on — and it is by design

```
ReplayVerificationError: live graph state hash for scope <s> does not match the
receipt-reconstructed state hash
```

`replay.py:495` labels the comparison plainly: *"Liveness anchor: each reconstructed per-scope
final equals the live store ([0025] step 460)."*

Controlled test, one scope, writes added one at a time, replaying every run after each:

```
after write 1   ['OK']
after write 2   ['OK', 'FAIL']            <- the run that just passed now fails
after write 3   ['OK', 'FAIL', 'FAIL']
```

Checkpoints are returned newest-first (timestamps confirm). So **a run stops byte-replaying the
moment another write lands in its scope**, and only the most recent run can ever pass.

**This is not a defect — it is a current-state attestation, not a historical replay.** It proves
"the graph as it stands is exactly what these receipts say it should be", which is tamper
detection. That is a genuinely useful property and a much narrower one than the name suggests.

### Two things that ARE wrong, and one of them is mine

- **`crypto_shred`'s docstring is false.** It promises *"the hash chain still verifies and every
  recorded run still byte-replays"*. The first half holds; **the second is false for any scope
  with more than one run** — and was false before any shred. An auditor reading that docstring
  would draw the wrong conclusion about what the system can prove. Filed **T3-7**.
- **My own walkthrough over-claimed.** "Byte-for-byte replay" implied replaying historical runs.
  Corrected there to state the liveness-anchored semantics.

**CLOSES WITH (open):** whether a genuinely historical replay is possible — reconstructing state
*as of* a past run rather than anchoring to live — is unexamined. `replay.py` is 1,153 lines and
I have read roughly the 60 around the anchor.

## D-56 · Postgres does not detect row tampering. SQLite does.

**Why this probe.** D-55 left the audit story half-verified: crypto-shred holds, and
`byte_replay` turned out narrower than advertised. `verify_no_silent_mutations` is the surface
that actually carries the audit claim — *"every live state change is receipted"* — and it had
never been run. An audit ledger that has never been attacked is a hypothesis.

**Method.** `scripts/verify/probe_tamper_detection.py`. Seed 3 facts, confirm the invariant
passes on clean data, then mutate the store **behind the API's back with raw SQL**, reopen the
client so nothing is cached, and re-check. Four modes, because a reviewer will ask about each:
MODIFY a fact's text, DELETE a row, INSERT an unreceipted row, alter a receipt (LEDGER).
**Held constant:** same probe, same seed, same modes, same assertions — the only variable is the
storage engine.

```
              MODIFY    DELETE    INSERT    LEDGER
sqlite        detected  detected  detected  detected      4/4
postgres      UNDETECT  UNDETECT  UNDETECT  detected      1/4
```

### The mechanism, confirmed in source rather than inferred

- **SQLite** `graph_state_hash` (`sqlite.py:463`) is *"a sha256 over the canonical, uuid-ordered
  list of relationship tuples"* — **recomputed from rows**. Any row change moves it, so the
  "latest receipted == live" comparison fails and the tamper surfaces.
- **Postgres** `graph_state_hash` (`postgres/_graph.py:709`) is *"Memoised, incrementally
  maintained"*, read from `relationship_state_tuples` plus a `dirty` mark. Raw SQL against
  `relationships` updates neither, so the memo hands back the **pre-tamper** hash and the
  invariant reports clean over modified data.

**The optimisation that makes Postgres fast is exactly what makes it blind.** Its own docstring
says the value is *"Identical in value to the SQLite backend's full recompute; the parity suite
asserts that byte-for-byte"* — true for legitimate writes, and precisely why no test catches
this.

### Third instance of the same meta-pattern

`test_storage_backend_parity.py` contains **zero** out-of-band mutations — verified,
`grep -cE 'UPDATE relationships|DELETE FROM relationships|INSERT INTO relationships'` returns 0.
So it structurally cannot detect a divergence in *detection power*; it only compares values
produced by normal operation.

That is now the third time the same blind spot has produced a Tier-0: **T0-4** (the caller's
view of a scoped read), **T0-5** (where a write actually lands), and now **T0-7** (whether a
tamper is noticed). The suite is 3,310 lines and green throughout. **The pattern is worth
stating as a rule: parity of values is not parity of properties.**

### Fairness, and the fix

Exploiting this needs direct database write access, which is privileged — worth saying before
someone accuses the finding of being alarmist. But detecting exactly that is what a hash-chained
ledger is *for*, and the property provably exists on SQLite and is lost on the production
backend. Filed **T0-7**.

Fix direction: make the **verification path** force a full recompute from `relationships`, or
cross-check `relationships` against `relationship_state_tuples`. No force-recompute path exists
today. The cost lands on `verify_*` only, never the hot path — so this does not give back the
performance the memo was added to win.

**Not claimed:** that a tamper is undetectable by *any* means on Postgres. Only that
`verify_no_silent_mutations`, the surface built to detect it, does not. A separate
`relationships` vs `relationship_state_tuples` consistency check would likely catch it and has
not been tried. **CLOSES WITH:** run that comparison after a raw-SQL tamper.

## D-57 · Reality audit — 33 of 57 items survive a bare `git clone`. One is our setup.

**The challenge, and why it was fair.** Ryan: *"are these real raw bugs, not just
misconfigurations on our end?"* Both retractions this week were exactly that failure — a `.env`
in our cwd (D-45) and back-to-back hooks (D-50) — so the base rate of this error in our own work
is non-zero and recent. Answering "most are real" without auditing would be the same move that
produced them.

**Method.** Classify every item by its **decisive** evidence — the thing that, if it evaporated,
would sink the finding — then reproduce the Tier-0 findings somewhere that shares nothing with
this machine.

```
A SOURCE      33   a file:line in committed code/config
B EXPERIMENT   9   a controlled run / RED-GREEN
C LOCAL        7   depends on our .env, .memotron/, our image
D EXTERNAL     4   tf-jennay, gh, Harness, GCP
E UNVERIFIED   4   self-labelled ASSUMED / UNCONFIRMED
```

**Clean room** (`scripts/verify/cleanroom.sh`, now committed): fresh clone, no `.env`, no
`.memotron/`, no `.claude/`, every `MEMOTRON_*`/`LITELLM_*` variable stripped per
invocation, dedicated Postgres on a random free port. `src/` swapped between `f28e95c` and this
branch with the probes **held identical**, so the source tree is the only variable.

```
T0-4 parity_postgres        main exit=1  fixed exit=0   reproduced
T0-5 probe_storage_wiring   main exit=1  fixed exit=0   reproduced
T0-2 probe_kek              main exit=1  fixed exit=1   reproduced (no committed fix)
T0-7 probe_tamper_detection main exit=1  fixed exit=1   reproduced (no committed fix)
regression tests            main exit=1  fixed exit=0   reproduced
```

**Every Tier-0 finding reproduces off-machine. Exactly one register item is our environment:
T1-13 (pgvector)** — a property of our compose file choosing a vanilla `postgres:16`, not of
Memotron. Moved to a new *"Environment, not product"* section, kept visible rather than
deleted. The real finding inside it survives: the fallback is silent and unindexed.

### Two harness bugs on the way, both instructive

1. **`origin/main` in the clone was not `origin/main`.** Cloning from a local worktree makes the
   clone's `origin/main` resolve to that worktree's **local** `main` — which was stale at
   `47790d4`, pre-#44. The RED phase silently reverted `src/` to the wrong tree and every gate
   returned harness errors that *looked* like findings. **Fixed by pinning the baseline to a
   SHA.** A ref name is exactly the kind of thing that resolves differently elsewhere — which is
   the whole subject of this audit.
2. **The clean room stripped the variable the fixed tree requires.** `MEMOTRON_ALLOW_EPHEMERAL_KEK`
   was in the strip list, so on the fixed tree every Postgres gate exited 2 — **our own
   fail-closed guard doing its job**, reading as "did not reproduce". Confirmed directly rather
   than assumed: stripped -> `exit=2, "refusing to build a Postgres storage backend with an
   ephemeral KEK"`; set -> `exit=0, parity PASS`. The guard is still exercised, by the test that
   unsets it internally.

Both are the same shape as the findings this audit was checking: **the environment changed the
result, and the first reading was wrong.** Worth noting that the audit found them in its own
instrument, not in the product.

### Corrections to the register the audit forced

- **T0-4 and T0-5 said "fix applied in the working tree, uncommitted."** False since `29624cb` —
  they are committed. A stale status line that would have misled the next reader.
- **Two probes were never committed** — `probe_tamper_detection.py` (T0-7) and
  `probe_shred_audit.py` (T3-7). Both findings survived on their source anchors, but their
  headline measurements were reproducible by nobody but me. Now tracked.
- **Line rot:** T1-4 cited `admin_server.py:2244`; `required=True` is at `:2926`. T3-4 said 14
  chart templates; there are 15. Exactly what T4-11 already warns about.

### What is still weak, said plainly

**T4-2** quotes non-deterministic LLM output from one gateway environment with no probe and no
rubric — a different person gets different output. **T1-6** is a derived symptom of T1-1, not an
independent finding. **T4-3/T4-4/T4-5** are almost certainly real but filed against the wrong
evidence; each is one `grep` from being source-provable, and that grep has not been run.
**T0-3, T2-3, T3-6** are honestly self-labelled UNCONFIRMED and must stay that way.

## D-58 · Closing the three weak items found one promotion and one retraction

D-57 flagged **T4-3, T4-4, T4-5** as "probably real but filed against the wrong evidence — each
one grep from source-provable, and that grep has not been run." Ran them. The result is not what
I expected, which is the point of running them.

### T4-3 — promoted, observation-only to source-provable

The two transport builders genuinely disagree, and the asymmetry is exact:

```
runtime.py:191   build_transports_from_env
                 model = os.environ.get(DREAM_AGENT_MODEL_ENV, "").strip() or model
                 # comment: the override "still selects a distinct decision model"

runtime.py:133   build_transports_from_credentials
                 model = resolved_model        <- the EXTRACTION model, no env consulted
```

`build_transports_from_tenant_credentials` (`:143-144`) reaches the env path **only when
`credentials is None`**. So the moment a tenant credential is sealed, the override is
unreachable. The earlier live observation (transport reporting haiku with the var set to sonnet)
is now redundant rather than load-bearing. **Class C -> A.**

### T4-4 — RETRACTED. The claim was mine and it was wrong.

Filed as: *"a formation denial writes no negative-space entry and no receipt — the ledger built
for 'why don't I remember X' has no entry for the most common reason."*

**The second half is false.** `_decide` calls `await self._record_decision(...)` at
`dreaming.py:5383`, at the same indentation as the surrounding `try/except` and **outside any
approval test**, passing `"approved": decision.approved` and the model's `summary`. Every gate
decision — approved or denied — is written as a `DreamDecisionRecord`, queryable through
`dream_decisions()` and rendered in the admin UI.

**So the ledger does answer "why don't I remember X".** It even carries the model's stated
reason.

I filed this from a single observation — `negative_space(scope)` returned 0 entries — and
generalised from "not in negative space" to "not recorded anywhere", without checking the
decisions table. The nearest competing explanation was *"it is recorded somewhere else"*, and I
never tested it.

**What survives, much smaller:** a denial produces no **negative-space** entry (that surface is
for rejected *candidates* — low salience, deduped, gated), and whether it also emits a
`MemoryReceipt` on the hash chain is **UNVERIFIED** and should not be claimed either way.

### T4-5 — corrected, same root cause

Filed as *"gate refusal and extraction-then-discard are indistinguishable"*. They are
distinguishable: `dream_decisions()` shows `formation_episode_selected` with `approved: false`
and a reason. I had assumed the same absence that T4-4 asserted.

**What survives:** reading the *same* `DreamRunResult` object for both outcomes — same run, same
surface, the only thing differing is which outcome occurred — the counters alone cannot separate
them: a gate refusal reads `processed=0` and an extraction that yielded nothing reads
`processed=1, created=0`, and neither says why. Those counters are what the MCP tools and the
admin dashboard surface. That is a **discoverability** problem, not missing data. Real, and a
tier less alarming than filed.

### The pattern, third time

D-45, D-50, and now D-58 all have the same shape: **an absence observed on one surface, promoted
to an absence in general, without checking the neighbouring surface.** The standing rule already
says the probe's conditions are part of the claim; this adds a second — *"I did not find it"
is not "it is not there" until you have named where else it could be.*

Worth noting D-57's audit is what surfaced these: classifying T4-3/4/5 as filed-against-the-
wrong-evidence is what prompted the greps. The audit paid for itself by retracting one of my own
findings the day before it would have been presented.

## D-59 · T4-4 re-scoped: denials are recorded, but outside the tamper-evident ledger

D-58 retracted my over-claim and left one half explicitly unverified — *does a denial emit a
`MemoryReceipt`?* I said it "should not be claimed either way". Settled it.

**Source-proven, three parts:**

1. **No `MemoryReceipt` on an ordinary denial.** The only `_emit_receipt` call anywhere in
   `_decide`'s range is `dreaming.py:5440`, inside **`_fallback_decision()`** — the path taken
   when the agent transport *throws*, emitting `DREAM_AGENT_TRANSPORT_FAILED`. A transport that
   succeeds and returns `approved: false` never reaches it.
2. **No negative-space entry** — unchanged from the original observation.
3. **`dream_decisions` is not tamper-evident.** The table carries no hash, previous-hash or
   Merkle column (`_migrations.py:134-139` — indexes only), and `verify_chain` / `replay.py`
   reference it **zero times**. It sits entirely outside the verified chain.

**The correctly-bounded finding:** the most common reason a memory does not exist *is* recorded,
with its reason — but in a table that can be edited without detection, while the hash-chained
ledger built precisely to be tamper-evident has no entry for it.

That is narrower than what I filed and sharper than the retraction. It also **compounds T0-7**:
on Postgres the row-level tamper check is already blind, and this class of record was never in
the verified chain to begin with — so "who decided not to remember this, and why" is the least
defensible part of the audit story, not the most.

**Arc worth keeping visible:** filed over-claimed (one observation, generalised) -> retracted
(D-58, after the audit forced the grep) -> re-scoped here with each part proven separately.
The intermediate retraction was correct at the time; stopping there would have thrown away a
real finding along with the wrong one.

## D-60 · The cluster answered I-1, and it rewrites the headline

Ryan asked whether I could run `kubectl` myself. I could — contexts for every environment were
already configured. Used `--context` explicitly rather than switching the active one, so his
shell state was untouched, and every command was read-only.

**I-1 was the first thing in the infra handoff for a reason. It cost two minutes and it
invalidated more of our register than any probe has.**

### What is actually running in `latest`

```
deployment  jedai-memotron-api   READY 1/2   revision 1   age 76d
pod  ...9tlqf   1/1 Running            0 restarts   37d
pod  ...9iqq    0/1 ContainerCreating  0 restarts   37d
image     wdpr-memotron:0.1.0
command   ["python", "examples/health_server.py"]
env       OPENAI_API_URL only
```

**Nothing of Memotron is deployed.** `examples/health_server.py` is 672 bytes whose docstring
says *"Minimal HTTP health server for Kubernetes liveness/readiness probes"* — it answers
`/health` and nothing else. No `MEMOTRON_GRAPH_PATH`, no MCP server, no admin Deployment, no
Ingress, no admin Service. Filed **T0-9**.

It **is** Helm/Harness-managed (`managed-by: Helm`, `helm.sh/chart: wdpr-memotron-1.0.0`,
`harness.io/release-name: release-8ec921`), and `deployment.kubernetes.io/revision: "1"` means
it has **never been updated in 76 days**. So a release happened once, in June, of a placeholder
— and `.helm/values-latest.yaml` has never shipped.

### The live defect nobody had noticed

`jedai-memotron-data` is **ReadWriteOnce** (`premium-rwo`), every pod mounts it at `/data`,
and `spec.replicas: 2`. RWO attaches to one node; the two pods are on different nodes, so the
second **can never mount** and sits `ContainerCreating` permanently. The HPA is min 2 / max 6 —
**it cannot satisfy its own minimum.** Degraded 1/2 for 37 days, unnoticed, which is precisely
what T4-8 (no OTEL, no metrics, no alerting) predicts. Filed **T0-8**.

### Corrections this forces on our own record

- **T2-3 REFUTED.** We predicted the pods had never started. One has been `Running` with **zero
  restarts for 37 days**. The prediction was wrong *for the reason we gave* — the deployed pod
  never runs `uv run --no-sync`, it runs `python`, so the container defect we analysed is not on
  its path at all.
- **T1-3 NARROWED.** `:memory:` across HPA replicas is true of the **chart** and false of the
  **cluster**. Keep it as a cutover blocker; stop describing it as what is running.
- **Our headline was wrong.** "The deployed API stores memory in `:memory:` and loses it on every
  restart" — it does not lose memory, because **it has never held any**. The honest version is
  starker: *nothing is deployed, half of what is deployed is wedged, and the chart in the repo
  has never shipped.*

### The methodological point, and it is the same one all week

Two Explore agents, a 57-item register, a clean-room reproduction — and the thing that
overturned the most was **two minutes of read-only access to the actual system**. The audit
agent had flagged it precisely: *"whether the cluster is actually running this chart, this
values file, or tag 0.1.1 — CANNOT TELL from the repo."* We had that caveat written down and
kept describing the chart as the deployment anyway.

**Repo evidence tells you what SHOULD run. Only the cluster tells you what DOES.** Every
"deployed shape" claim we made was class A against the chart and unverified against reality.

## D-61 · GCLB is healthy — because it is health-checking a stub

Followed D-60 by checking what else was reachable rather than leaving it marked UNKNOWN.

`gcloud compute backend-services get-health k8s1-9edbf368-...-92d435b7 --region us-central1`:

```
zone us-central1-b   healthState: HEALTHY   10.76.4.22:8000   node ...-22dq
zone us-central1-a   (no endpoints)
```

**The GCLB is happy, and the reason matters.** B3 warned that `/health` returns **204** where
GCLB wants 200, so a pod could read Ready to kubelet while the load balancer served 502s. That
is not happening — because the deployed container is `examples/health_server.py`, which
returns **200** (`self.send_response(200)`), and the `204` lives in `admin_server.py`, which
**is not deployed**.

So the finding is intact as a **pre-deployment** blocker and wrong as a description of today.
Same shape as T1-3: true of the chart, false of the cluster. Marked accordingly in
`INFRA-BACKLOG.md` rather than left UNKNOWN.

**Reachability, for the record.** `kubectl` (all environments) and `gcloud` are both available
to me, so cluster and GCP questions no longer need to wait on a human. **Harness still does** —
there is no CLI on PATH and no token in the environment. A `~/.harness/auth.json` exists, and
per the standing rule I did not read it. **C0 (the trigger list) stays a human task**, and it
still blocks merging.

## D-62 · T0-1 reproduced — and it is a split brain, not a deletion

The last Tier-0 without a reproduction, and a data-loss claim, so it was worth doing properly.

**First attempt PASSED, and the pass was worthless.** Migrating `alpha -> beta` into an empty
destination moved 3 memories, all 3 visible, source purged cleanly. I nearly recorded "T0-1's
consequence does not reproduce".

**The precondition I had failed to create.** T0-1 alleges rows go invisible *"once the
destination mints its own root epoch"* — and an **empty** scope has no epoch at all, which
`_epoch_visible` treats as visible. So a virgin destination structurally cannot trigger the bug.
Pre-seeding the destination with one memory of its own — the realistic case, adopting into a
tenant already in use — is what creates the condition.

**With the precondition, it reproduces:**

```
migrate_tenant_memory reported   relationships_migrated=3   (and exits 0)
destination raw rows             4      <- what VERIFY counts
destination visible              1      <- what the scoped read returns
source                           0      <- purged
```

Migration passed its own count verification, purged the source, and left three memories the
scoped read will not return.

### The correction: they are not gone, they are inconsistently visible

Same store, same scope, same moment, four surfaces:

| surface | sees |
|---|---|
| `graph.relationships()` | 4 |
| `graph.relationships_for_scope()` | **1** |
| `graph.active_relationships()` | **1** |
| `profile()` | **0 of 3 migrated** |
| `search('durable store')` | **2 hits — the migrated facts** |
| `search('claim lock')` | **1 hit — a migrated fact** |

**The epoch-filtered paths deny the memories exist; retrieval hands them back.** That is a
split brain, not a deletion, and it is a more precise and more troubling finding than the one
filed: the surface that *loses* them is `profile()`, which is what builds an agent's injected
context — the product's core function. A user searching finds the memory; the agent that is
supposed to remember it does not.

It also connects to **T0-3** (`_epoch_visible` divergence), which is still UNCONFIRMED and just
became much more interesting: `search` evidently does not apply the epoch filter the other paths
do.

### The lesson, again, and it is the same one

The first probe was a clean PASS that proved nothing, because I had not created the condition
the claim requires. **A negative result is only as good as the precondition behind it** — the
same error shape as D-45 (environment), D-50 (timing) and D-58 (looked on the wrong surface),
now in a fourth costume: *looked under the right rock, in a garden where that rock does not
exist.*

## D-63 · The split brain has a root cause, and it is not about migration

D-62 left a contradiction I could not explain: `search()` returned rows that
`relationships_for_scope()` did not. Both are supposed to read the same scope. Chased it.

**The rows themselves are correct.** All four in the destination carry `scope_key=tenant:beta`
and `status=active`. The difference is the epoch: beta's own row has `epoch-ea2b43f3…`; the
three migrated rows carry **`epoch-07bdebae…` — alpha's, copied verbatim**. So
`relationships_for_scope` filters them out against beta's active-epoch ancestry. That is T0-1's
mechanism, confirmed at the row level.

**But then how does `search` see them?** `search` -> `search_context` -> `_retrieval_candidates`
(`client.py:3686`), which iterates the *same* epoch-filtered `relationships_for_scope`. It
should see 1.

**Because retrieval has a second stage that reads differently.** `search_context` also calls
`_expand_retrieval_candidates` (`client.py:3697`), whose entity-hop frontier read is
`graph.relationships_for_node_uuids`. Read in full (`sqlite.py:1038-1067`):

```sql
SELECT * FROM relationships
WHERE gen_scope_key = ?
  AND type != 'MENTIONS'
  AND (source_uuid IN (...) OR target_uuid IN (...))
ORDER BY created_at, uuid
```

**No epoch predicate. No `_epoch_visible` call.** Rows go straight through
`_row_to_relationship`. Meanwhile `relationships_for_scope` documents the opposite in its own
docstring — *"filtered to the scope's active-epoch ancestry exactly like the other relationship
reads"*. The two reads sit ~40 lines apart in the same file.

**So stage 1 of retrieval enforces epoch isolation and stage 2 does not.** Filed **T0-10**.
T0-1's split brain is a *symptom*; this is the cause, and it has nothing to do with migration —
migration merely produced rows with a foreign epoch, which is one way to reach the hole.

**Why this is worse than the migration case.** Epochs exist to make re-dreaming reversible: you
adopt a shadow epoch, and if it is wrong you roll back. If an entity hop can pull rows from
outside the active ancestry, then **`rollback_epoch` does not fully hide what it rolled back** —
which is the guarantee the whole epoch machinery is for.

**Boundary, stated because it matters:** the leak via *migrated* rows is **observed**, with
default config. The rolled-back-epoch consequence is **DERIVED** — I have not run a rollback and
must not claim it as observed.

**But the derivation is stronger than a guess, and the reason is in the SQL.** The predicate
that excludes a row from `relationships_for_scope` is "its `epoch_id` is outside the scope's
active-epoch ancestry". `relationships_for_node_uuids` has **no epoch predicate at all** — so it
cannot distinguish *why* a row is out-of-ancestry. Migration produces such a row by copying a
foreign `epoch_id`; a rollback produces one by moving the ancestry out from under a row that
kept its own. **The leaking read cannot tell those two apart, because it never looks.** Any
mechanism that puts a row outside the active ancestry reaches the same hole.

**CLOSES WITH:** branch, adopt, roll back, then query with an entity hop and check whether the
rolled-back rows return. Needs a live extractor (`redream_branch` takes one), so it is a
gateway-backed test rather than a hermetic one.

**Method note.** This came from refusing to let a contradiction stand. The probe reported both
"3 stranded" and "migrated memories are searchable — 2 hits", which cannot both describe a
coherent system. Chasing the inconsistency rather than picking the reading I preferred is what
produced the root cause.

## D-64 · C0 answered — the trigger list is empty. Merging will not auto-deploy.

Ryan pointed at `mcp-forge/.local-secrets`, which unblocked the last human-only item. **The PAT
was never read into context** — sourced into an env var inside the shell and referenced as
`$HARNESS_PAT`, so the value never entered the transcript. Only the credential's *filename* and
the non-secret identifiers were displayed.

Identifiers came from the repo, not the secret: `pipeline.yaml:2-5` gives
`JedAI_Memotron` / `RA_Blocks` / `Architecture_Engineering`, and the account id
(`1-wFe3qRQv2mUh1s9244Eg`) is a URL-visible identifier found in mcp-forge.

### The answer

```
GET /pipeline/api/triggers?...&targetIdentifier=JedAI_Memotron
  HTTP 200  status=SUCCESS  totalPages=0  empty=True  content=[]
```

**Zero triggers. Merging will not ship anything.**

### The control that made that result mean something

An empty list is weak evidence on its own, so I checked whether the query could return empty
for the *wrong* reason:

- **Bogus pipeline identifier** (`No_Such_Pipeline_ZZZ`) -> **also HTTP 200 / SUCCESS / empty.**
  So "empty" alone cannot distinguish *no triggers* from *pipeline not found*.
- **Existence confirmed independently** via the summary endpoint: `name: JedAI Memotron`,
  `storeType: REMOTE`, with a real `lastExecutionTs`.

Only the two together establish the claim. Without the bogus-identifier control I would have
reported an ambiguous result as a definite one — and a malformed query *does* fail loudly
(HTTP 400, *"targetIdentifier must not be null"*), which is what made the 200-on-bogus
surprising.

### Two more answers fell out

**`storeType: REMOTE`** — the pipeline is **git-synced**, so `.harness/pipeline.yaml` in this
repo *is* the source of record. That retires the mcp-forge warning about inline pipelines
carrying triggers invisible in git; it does not apply here.

**Last execution: 2026-06-11 20:15 UTC, 76 days ago.** Three independent sources now agree on
that date:

| source | says |
|---|---|
| Harness `lastExecutionTs` | 2026-06-11 |
| k8s Deployment age (D-60) | 76d |
| GAR image `createTime` | 2026-06-11 |

**The pipeline ran once, in June, and has not run since.** That corroborates T0-9 from a third
direction: the deployment is a one-shot June release of a placeholder, never updated.

### Residual risk, stated

The PAT demonstrably has read access to *pipelines* in this project; that does not strictly
prove it has read access to *triggers*. A permissions failure would normally surface as 403
rather than an empty SUCCESS, so the risk is low — but the honest bound is "no triggers visible
to this token", not "no triggers exist". **CLOSES WITH:** anyone with console access eyeballing
the Triggers tab once.

## D-65 · "Nothing is deployed" — now bounded across all five clusters, and an inference corrected

D-60 claimed *"nothing of Memotron is deployed"* on the strength of **one** cluster. That is
the over-generalisation this project keeps producing, so I checked the rest.

```
latest    us-central1    jedai-memotron-api 1/2  76d   (the health stub)
stage     us-central1    namespace does not exist
load      us-central1    namespace does not exist
prod      us-central1    namespace does not exist
prod      europe-west9   namespace does not exist
```

**The claim survives, and is now properly bounded:** `latest` is the only place Memotron has
ever been deployed, and what is there is not Memotron.

### An inference I made and had to correct within the minute

`kubectl get deploy -n jedai-memotron` prints **"No resources found in jedai-memotron
namespace"** — which reads as *the namespace exists and is empty*. I wrote that down. Checking
`get ns` directly returned `Error from server (NotFound)`: **the namespace does not exist at
all.** kubectl emits the same reassuring sentence either way.

Small, but exactly the pattern: a message that *sounds* like a positive observation about a
thing that is not there. Same family as "`negative_space()` returned 0" (D-58) and "the plaintext
is absent from disk" (D-55) — **absence of a result is not a result**, and tools phrase absence
in ways that invite the wrong reading.

### What it means for the plan

The environment ladder is **entirely notional**. There is nothing to promote *from* (the June
stub) and nowhere provisioned to promote *to* (no namespaces beyond `latest`). The plan's
"harden `latest` first, then ladder to stage/load/preview/prod" is not a sequencing preference —
it is the only option, because the other four rungs do not exist yet.

Good news for risk, incidentally: **there is no Memotron in production to break.** Every
Tier-0 in this register is a pre-deployment finding, not a live incident — with the single
exception of **T0-8**, the wedged replica in `latest`.

## D-66 · Chasing a failed precondition found why the SDK path forms nothing

Set out to close T0-10's DERIVED half (does a rolled-back epoch leak through an entity hop?).
Never got there — the probe could not establish its precondition, and chasing *that* was worth
more than the original question.

**Four attempts, each eliminating one explanation:**

1. One-sentence episode -> `created_relationships=0, admitted=0, quarantined=0`. Suspected D-46
   (thin input extracts to nothing). Made the body four substantive sentences.
2. Still zero. So not input length.
3. Checked which extractor the client actually had: **`InstructionalExtractor`**, not the
   gateway one — `Memotron(graph_path=...)` **never calls `build_transports_from_env()`**,
   even with `LITELLM_API_KEY` present. Filed **T1-16**. Wired the gateway transport explicitly.
4. Still zero memories — **but the counters changed**: `quarantined_candidates` went from 0 to
   **3**. Extraction was now producing candidates; formation was rejecting all of them.

**The reason, from the system's own accessor:** `quarantined_candidates()` returns three
entries, every one `reason='property_key_not_allowed'`.

**And the cause is a disagreement between two configs in this repo:**

```
config.py:976          strict_properties: bool = True        <- the default
default_config()       NodeInstruction(..., properties=5)    <- no override, inherits True
agent_memory_config()  strict_properties=False               <- explicitly lenient
```

`default_config()` allows exactly `kind, role, tier, region, system` and quarantines any
candidate carrying another property key. `agent_memory_config()` turns that off — which is
precisely why the hook path formed 9 relationships in D-46 while the plain SDK path forms
zero. **The two entry points disagree about strictness, and only one of them works with a
model.** Filed **T0-11**.

`config.py:1647` shows the design is aware of the trade-off (*"`strict_properties=False`
silently strip an unknown key"*) — so this is a chosen default colliding with real model
output, not an oversight nobody considered.

### The probe fix that earned its keep immediately

The first run printed **"rollback leak: LEAKS"** while its precondition had failed — the
branched memory never existed, so every later assertion passed vacuously and the failure got
counted as the finding. Changed a failed precondition to return **INCONCLUSIVE (exit 2)** rather
than a result. It reported INCONCLUSIVE on the very next run, correctly, three times.

That is the D-62 lesson encoded rather than merely written down: **a probe must distinguish
"the thing did not happen" from "I could not set up the test".**

### Still open

**T0-10's rollback half remains DERIVED after five attempts, and I am stopping here.**

Attempt 5 relaxed `strict_properties` in the config handed to the client — `redream_branch`
passes `config=self.config` into `branch_and_recompute`, so the branch should have inherited it.
It still formed nothing. Eliminated so far: episode length, extractor wiring, and property
strictness. Not eliminated: whether `branch_and_recompute` forms into the shadow epoch at all
under these conditions, and whether `redream_adopt`'s diff-merge is what drops the rows.

**Stopping is the right call, not a failure to record.** T0-10's substance — retrieval's
expansion stage reads `relationships_for_node_uuids`, which has no epoch predicate — is
**observed** and independently reproduced via migration (D-62/D-63). The rollback corollary is
a *second consequence of the same hole*, and its precondition needs the redream pipeline to
work, which is turning into its own investigation.

**CLOSED, and the answer was in the return value I was throwing away.** `RedreamResult` already
carries `created_relationships`, `episodes_selected`, `baseline_relationships_seeded`, `tier`,
`tier_reason` and `shadow_store_path`. The probe read only `epoch_id` off it — discarding exactly
the diagnostic that separates "the branch never formed" from "adopt dropped it". No
instrumentation was needed; I had built an elaborate closing move for a question the API answers
directly.

Printing the whole object:

```
tier                           A
tier_reason                    governance-only (or no) override -- re-govern stored candidates
episodes_selected              1
baseline_relationships_seeded  1
created_relationships          0
```

**Tier A re-governs ALREADY-STORED candidates. It does not re-extract.** `branch_and_recompute`
picks "the cheapest sufficient tier", and with no override there is nothing to re-extract *for*.
My probe added an episode and branched immediately, so no stored candidates existed and there
was nothing to re-govern. **All five attempts failed on the same setup error**, and none of them
were about the product.

**The corrected setup is neat, and T0-11 supplies it.** Under `strict_properties=True` a real
model's candidates are *stored but quarantined* (T0-11: 3 of 3, `property_key_not_allowed`).
That is exactly the input Tier A exists for: branch with a **governance override** that relaxes
strictness, re-govern the quarantined candidates, and the branch admits rows that exist **only**
in the new epoch — which is precisely the precondition the rollback test needs.

**CLOSES WITH:** seed under strict governance so candidates quarantine, branch with a
`RedreamOverrides` that relaxes it, confirm `created_relationships > 0`, adopt, then roll back and
ask whether `search()` still returns the rows the scoped read no longer does.

**Cost of this thread, honestly:** five runs, no answer to the question asked — but it produced
**T1-16** and **T0-11**, the second of which explains why the documented SDK entry point forms
nothing at all. The failed precondition was more valuable than the target.

## D-67 · The answer was in the return value

Worth separating from D-66 because the lesson is different.

Five runs failed to form a memory in a branched epoch. I diagnosed episode length, then extractor
wiring, then property strictness — each a real finding, none the actual cause — and wrote a
closing move proposing to **instrument `branch_and_recompute`** so I could inspect the shadow
store.

That instrumentation was unnecessary. `RedreamResult` — the object the call already returns —
carries `tier`, `tier_reason`, `episodes_selected`, `baseline_relationships_seeded`,
`created_relationships` and `shadow_store_path`. My probe did `getattr(branch, "epoch_id")` and
discarded the rest.

Printed in full it says, in its own words: `tier=A`,
`tier_reason="governance-only (or no) override — re-govern stored candidates"`,
`created_relationships=0`. **Tier A re-governs stored candidates; it does not re-extract.** With
no override and no stored candidates there is nothing for it to do. The API had been explaining
itself the whole time.

**The rule this earns:** *read the whole return value before instrumenting the callee.* Three of
this week's dead ends (D-58's wrong surface, D-62's failed precondition, this) came from
inspecting the system while ignoring what it had already told me. A structured result object is
evidence, and the cheapest kind.

None of the five failures were product defects. **They produced two anyway** — T1-16 and T0-11 —
which is the only reason the detour was affordable.

## D-68 · Rollback does not hide what it rolled back — DERIVED becomes OBSERVED

Six attempts, and the last one answered it. `scripts/verify/probe_rollback_leak.py` now exits 1
with its precondition genuinely met.

**Held constant:** one scope, one client, one episode, one config. The only thing that changed
between the phases is the epoch pointer that `adopt` and `rollback` move.

```
branch     tier=C  tier_reason="extraction_transport override forces full re-extraction"
           created_relationships=2          <- the branch really formed something
adopt      scoped-read=3   search hits=2    <- both branched facts live and retrievable
rollback   scoped-read=1   search hits=2    <- restored... and still retrievable
```

`redream_rollback` did its job on the epoch-filtered path: the scoped read returns to the
baseline count of 1. **And `search()` still returns both rolled-back facts, by name** — "the
frankfurt cutover requires a rotated customer master key", "the operator should verify a rotated
customer master key".

**The guarantee the epoch machinery exists to provide does not hold for retrieval.** An operator
who decides a re-dream was wrong rolls it back; the audit-visible state reverts; the agent keeps
being handed the rolled-back memory. T0-10 now has **both** consequences observed — migration
(D-62) and rollback (here) — and the cause is one missing predicate in `sqlite.py:1057-1067`.

### What unblocked it, after five failures

`epochs.py:170` states it plainly: *"`extraction_transport`/`instruction_set` force Tier C"*.
The default is Tier A, which **re-governs stored candidates and does not re-extract**, so a
freshly queued episode yields `created_relationships=0`. Every earlier attempt was blaming
extraction, wiring, or governance for what was a tier selection working exactly as documented.

**Three lessons compounded to get here**, and all three were already written down:
1. read the whole return value (D-67) — `tier_reason` said it outright;
2. a failed precondition is not a result (D-66) — the INCONCLUSIVE guard stopped four wrong
   verdicts, including one that printed "LEAKS" while proving nothing;
3. name the precondition before trusting a negative (D-62).

**Cost, honestly:** six runs to answer one question. It also produced **T1-16** and **T0-11**
along the way, and the answer itself upgrades a Tier-0 from derived to observed the day before
it gets presented. Worth it — but the efficient version of this was three lines of reading
`epochs.py:165-191` before writing the first probe.

## D-69 · T0-10 has a validated three-line fix, and it draws a clean line under T0-1

Mirrored `relationships_for_scope`'s own filter into the entity-hop read — the two sit forty
lines apart in `sqlite.py` and only one of them had it:

```python
ancestry = self._active_epoch_ancestry(scope_key)
relationships = [self._row_to_relationship(row) for row in rows]
return [r for r in relationships if self._epoch_visible(r, ancestry=ancestry)]
```

**Held constant:** same probes, same seeds, same config. The only variable is those three lines.

| gate | before | after |
|---|---|---|
| `probe_rollback_leak.py` | exit **1** — post-rollback `search` returned **2** rolled-back facts | exit **0** — search returns **0** |
| `probe_migration_visibility.py` | raw=4 visible=1, **search returned 2** (split brain) | raw=4 visible=1, **search returns 0** |
| suite (hermetic) | 1,005 passed / 1 pre-existing failure | **identical — zero regressions** |

### The migration gate still fails, and that is the fix behaving correctly

Before, `search()` and `profile()` disagreed about whether the migrated memories existed. Now
they agree — and agree that the rows are **invisible**. That residue is **T0-1**: migration
copies `epoch_id` verbatim, so the rows genuinely are outside the destination's ancestry. The
T0-10 fix makes the system *consistently* hide them instead of *inconsistently* serving them.

Worth stating because it is easy to misread a still-red gate as a failed fix: **these are two
defects, and this patch closes exactly one.** T0-1 needs its own — rewrite `epoch_id` during
migration, which is the reviewer's original point.

### Postgres has the identical gap

`grep -c "_epoch_visible|ancestry"` inside its `relationships_for_node_uuids` returns **0**. The
same three lines are needed there, so a SQLite-only fix would leave the production backend
leaking. Filed in T0-10; draft at `docs/findings/t0-10-epoch-filter.patch`.

### Where this leaves the Tier-0 set

Four of eleven now have measured RED->GREEN fixes (T0-4, T0-5, T0-2 guard, T0-10). T0-1's is
known but unwritten. The rest are deployment or configuration facts rather than code defects.

## D-70 · A 17-agent workflow over the unread 30k lines — 10 findings, and one I verified myself

Ryan asked whether a workflow would be faster. **For breadth, yes.** For the serial debugging in
D-66/D-68 — five hypotheses each informed by the last — no; that needed accumulated context.

Ran six readers over the corpora never examined (`replay.py`, `coherence.py`,
`certification.py`, `migration.py`, the interop layer, the `dreaming.py` tail), then handed every
non-minor candidate to an **independent skeptic instructed to refute it**, defaulting to
`survives=false` under uncertainty. 32 candidates -> **10 survived, 0 killed**.

**"0 killed" was my first concern** — my own retraction rate this week was four in sixty. But the
correction record shows the skeptics did real work rather than rubber-stamping:

- **T2-04**: *two* claimed reachability paths **disproven** with counter-evidence (migration emits
  per-row receipt brackets, `tests/test_migration.py:664-700`; `purge_tenant_state` deletes
  receipts and rows in one transaction, leaving an empty scope where `passed=True` is correct).
- **T2-02**: cited repro subjects were **factually wrong** — they measured *above* threshold and
  the hold *was* applied. Replaced with correct ones.
- **T2-09**: *"has no graph handle in its signature"* — **wrong**, one is reachable via
  `_client_ledger`; dropped, which changes the fix from a signature change to a few lines.
- **T2-08**: the docstring-contradiction limb **dropped** as the weakest, and the "public SDK
  surface" framing narrowed to SDK-only.

So the pattern was the same one I hit repeatedly myself: **true core, overstated limbs.** The
skeptic pass trimmed the limbs. That is what I wanted from it.

### The one I verified by hand, because it compounds T0-7

**T0-12** (`verify_no_silent_mutation` fails open). I did not take this on the agent's word —
constructed the precondition the same way `build_session_digest` does (a direct
`storage.add_relationship` with no receipt bracket) and ran the check:

```
live rows in scope        : 1     (written with NO receipt)
mutating_receipt_count    : 0
latest_receipted_hash     : None
errors                    : []
>>> passed                : True
```

Confirmed. `replay.py:1072-1087` guards the comparison on the receipted side only —
`if latest_hash is not None and ...` — and `latest_hash` is set only inside the mutating-receipt
loop. **Zero receipts, no comparison, `passed = not errors` = True.** And I verified the
first-party writer independently: `build_session_digest` (`epochs.py:1104`) has **zero**
receipt references in its body.

**Why it matters more than it looks:** `certification_bundle` exports `passed`. An operator
certifying a scope whose entire receipt stream is missing gets an affirmation of the guarantee
exactly where there is no evidence for it. And it compounds **T0-7** — that one is Postgres-only;
this is backend-independent.

### Net

Register goes 62 -> 72 items, Tier-0 11 -> 13. The audit and coherence layers are now the
densest defect areas in the codebase, which is a reversal: I had been calling the audit design
the system's strongest idea. Crypto-shred still is. The *verification* built on top of it is not.

Full report kept at `docs/findings/workflow-audit-2026-08-27.md`, including §2 (what the
skeptics killed or narrowed) and §3 (what is still unread) — that last section matters, because
the workflow read six corpora and several thousand lines remain.

## D-71 · Correcting D-70: those 10 findings were chosen by position, not merit

D-70 and its commit say *"32 candidates, 10 survived, 0 killed"*, which reads as though a
refutation pass reduced 32 to 10. **It did not.** My workflow script had

```js
const worthVerifying = all.filter((f) => f.severity !== 'minor').slice(0, 10)
```

`parallel()` preserves input order, so the first ten non-minor findings came from targets 0 and
1 — `replay` and `coherence`. **Everything from `certification`, `migration`, `interop` and
`dreaming-tail` was dropped before the skeptics ever saw it, silently.** The refutation pass ran
on 10 of 32; the other 22 were never judged, never reported, and never counted as killed.

That is precisely the anti-pattern the tooling warns about — *"if a workflow bounds coverage,
log what was dropped; silent truncation reads as 'covered everything' when it didn't"* — and I
wrote it into the script myself while telling six agents to be rigorous about evidence.

**The synthesiser caught it**, unprompted, and led its report with it: *"None of those findings
appear in the set handed to me... the lead should chase the missing four streams before treating
this as a completed audit."* Giving the final agent the coverage notes as well as the survivors
is what made that possible — it could see readers describing findings that were not in front of
it.

**Specifically named but never verified**, from the dropped streams:

- a `public_benchmark` whose judge always returns 0.0 yet reports `passed=True`
- `stateful_policy_comparison` reporting all-pass while **every** `baseline_sample` fails
  `retrieval_allocation` — carried end-to-end into a live alias via
  `initialize_policy_alias(certification_passed=True)`
- a `stability_score=1.0` for a judge that flips its answer between repetitions
- a migration `purge` / `dream_job_runs` scoping issue
- two interop router/backend items

If the certification ones hold, they are the same *fail-open* shape as T0-12 and they sit in the
module that underwrites quality claims. **They are unverified and must not be quoted** until the
resumed run judges them.

**Fixed and resumed:** the cap is gone, per-source counts are logged, and the run was resumed
from its run id so the six readers replay from cache rather than re-reading 30k lines.

**The lesson is not "workflows are unreliable".** The agents did what they were told. *I* wrote
a silent truncation into the orchestration and then reported the output as if it were complete —
the same failure I have been catching in my own probes all week, one level up: **the harness is
part of the claim.**

## D-72 · T0-12 has a validated fail-closed fix, and the suite never depended on the hole

The guard was `if latest_hash is not None and live_hash != latest_hash:` — so zero mutating
receipts skipped the comparison and `passed = not errors` returned True. The fix makes the
zero-receipt case explicit rather than silent:

```python
if latest_hash is None:
    empty_hash = graph.graph_state_hash("\x00memotron:empty-scope-probe")
    if live_hash != empty_hash:
        errors.append("scope holds live state (...) but no mutating receipt explains it")
elif live_hash != latest_hash:
    ...
```

Zero receipts is only consistent with an **empty** scope. `graph_state_hash` is a deterministic
digest over the scope's tuple set, so every empty scope yields the same constant — asking for an
unused key obtains it without adding a storage method.

**Held constant:** same repro, same client, same config; the only variable is the patch.

| | before | after |
|---|---|---|
| hand-built repro (1 live row, 0 receipts) | `passed=True`, `errors=[]` | **`passed=False`** with a named error |
| suite (hermetic) | 1,005 passed / 1 pre-existing failure | **identical — zero regressions** |

**The zero-regression result is itself a finding.** Not one of 1,005 tests depended on the
fail-open, which means no test ever asserted `passed=True` for a scope in that state — the hole
was untested rather than relied upon. That is the cheap kind of fix: it closes a fail-open
verdict without renegotiating any existing expectation.

**Noted while testing, and worth chasing separately:** `build_session_digest` writes an
unreceipted ACTIVE ROLLUP. In a scope that *also* holds receipted memories, `latest_hash` is set,
so `live_hash != latest_hash` and the check **already** fails — before this patch. So that writer
does not merely enable the fail-open on empty scopes; it makes `verify_no_silent_mutations`
report a violation on any ordinary scope that has ever had a session digest built. **DERIVED. Attempted and the precondition did not hold**, so it stays derived: seeding two
receipted memories and calling `redream_session_digest(session_id="s1")` returned **None** —
*"no active facts for that session"* — so no unreceipted ROLLUP was written and the check still
read `passed=True, receipts=2, errors=0`. The digest only writes when facts were **formed in
that session**, and my memories were not tagged to it.

**CLOSES WITH:** form facts through the dreaming path with an episode carrying
`session_id`, confirm `redream_session_digest` returns a relationship rather than None, and only
then call `verify_no_silent_mutations`. Recognising the failed precondition took one run this
time rather than five (D-66) — the check for "did my setup actually happen?" is becoming
reflexive, which is the point of writing these down.

Five of thirteen Tier-0 items now carry measured RED->GREEN fixes: T0-4, T0-5, T0-2 (guard),
T0-10, T0-12.

## D-73 · The complete run: 27 of 32 survived, 3 killed, and a Tier-0 in certification

The resumed workflow (cap removed, all six streams verified) returned **32 candidates -> 27
survived, 3 killed**. The first run's "10 survived, 0 killed" was 10 of 32 chosen by position;
see D-71.

**The killed count is the useful number.** Three findings died under refutation this time, and
several survivors came back materially narrowed — A-03's "cross-store only" framing was
**wrong** and corrected, and A-04 carries an explicit *"not durable, and should not be filed as
such"* bound on its explicit-provider path. Skeptics that kill nothing are not skeptics; these
were.

### T0-14 — verified by hand, because it is the one that matters

**Staging a BASELINE policy contract evaluates the CANDIDATE's invariants.** Read it myself
rather than trusting the agent:

```python
certification.py:1931   invariants = tuple(candidate.protected_invariants)   # candidate only
certification.py:1956   protected_invariants=invariants                      # what reaches the report

client.py:2020-2023     baseline_safe = all(i.passed for i in certification.protected_invariants) ...
                        certification_passed = certification.passed if role == "candidate" else baseline_safe
```

For `role="baseline"` the gate reads the **other arm's** data. `:1813` computes the baseline's
own invariants per sample, with each sample's own Motive, and then discards them. Nothing at
`:153`/`:171` types or names the field as candidate-only, so a cross-role read is not even a type
error. **Secondary defect on the same lines:** only `baseline_samples[0]` / `candidate_samples[0]`
are read, so repetitions 1..N-1 of both arms are never gated at all.

The audit reproduced the consequence end to end using **the repo's own canonical fixture**
(`tests/test_stateful_certification.py:70-72`): baseline failed `retrieval_allocation` in both
repetitions, top-level invariants reported all-passed, `certification_passed=True`, and the alias
was installed and accepted by both the runtime gate (`client.py:1085`) and the storage gate
(`sqlite.py:2788`). `tests/test_stateful_certification.py:55,59` passes while the baseline is
failing.

**This is the third fail-open in the verification layer**, after T0-12 (`verify_no_silent_mutation`
passing with zero receipts) and T1-29 (`run_public_benchmark` passing with a zero score). They
share one shape: *an affirmative verdict returned in a state where there is no evidence for it.*

### Also notable

- **T1-28** — `purge_tenant_state` truncates two **global** tables from a per-tenant surface, on
  both backends. A tenant-scoped erasure removes other tenants' rows.
- **T1-30** — `MemoryRouter`'s cross-tenant guard takes the tenant id **from the request body**
  and defaults `principal` to `None`, so it compares a caller-supplied value against itself. Dead
  code in the default configuration, on an externally reachable surface.
- **T1-25** — migration raises `RuntimeError` in the *normal* post-ingestion state (agents hold
  episodes, nothing formed yet), after the destination has already been partially populated, and
  **T1-27** says a retry does not converge.

### Register

**85 items, 14 Tier-0.** It was 62 and 11 this morning. (Recounted 2026-08-27: an earlier `grep -c` said 86 because the downgraded `T1-13 pgvector` note repeats a live id in bold. 76 primary ids + 9 `T1b-*` = 85.) The densest defect areas are now
certification, migration and coherence — three modules nobody had read until today.

## D-74 · Verifying T1-28 narrowed it — the blast radius is real but it is not memory

Took the most alarming item from the audit and checked it myself, because a tenant-scoped
erasure destroying other tenants' data is the class where being wrong is expensive both ways.

**The mechanism is exactly as filed.** Both backends run unqualified deletes inside
`purge_tenant_state`:

```
sqlite.py:5337-5338      DELETE FROM dream_job_runs      (no WHERE)
                         DELETE FROM job_state           (no WHERE)
postgres/_governance.py:1506-1509   the same
```

**But the Postgres twin carries a comment that changes the finding:** *"Maintenance bookkeeping
is global, not per tenant: SQLite cleared it wholesale so the next run re-derives from a clean
slate."* This is **documented intent**, not an oversight — which makes it a decision to revisit
rather than a bug to fix.

**And "the blast radius is the store, not the tenant" — my own words when filing it — reads as
memory loss. It is not.** Memory, receipts and checkpoints are correctly scope-filtered a few
lines above. What the other tenants actually lose:

- **`dream_job_runs`** backs `client.dream_history` (`client.py:1267`, read at
  `sqlite.py:1415-1437`) — so every other tenant loses its **dream-run history**, an
  operational-audit surface, though not the hash chain.
- **`job_state`** holds per-job `last_run` (`sqlite.py:1381`) — so **every job's cadence resets
  across the whole store** and the next pass treats everything as due. One tenant's erasure
  triggers a fleet-wide dreaming stampede, and the LLM spend that comes with it.

That second consequence is the one worth raising: it is not data loss, it is an **unbounded cost
and load event fired by an unrelated tenant's RTBF request** — and nothing in the purge result
discloses it.

**Pattern, again.** The agent's finding had a true core and a framing that overstated it; I
repeated the framing when filing, and only caught it by reading the two lines and asking what
those tables feed. That is now four times this week that "read what it actually touches" changed
a severity — and the first time I did it to a finding I had already committed.

## D-75 — the register's own evidence is not uniform, and now says so

**What I did.** Marked 21 of the 24 workflow-sourced backlog items **AGENT-SOURCED, not
hand-checked**, and added a provenance table to `TAKEOVER-BACKLOG.md` splitting the register
into hand-verified (3), agent-sourced (21), and everything else (62).

**Why, specifically.** The register jumped 62 -> 85 in one afternoon because 17 agents read
~30k lines I had not read. I have since hand-checked three of those items. **Two confirmed
(T0-14, T0-12); one needed narrowing (T1-28).** A 1-in-3 correction rate on the sample is not
a rate I can quietly extrapolate away — and I filed T1-28's overstated framing myself, after
the agent handed it to me, by repeating its severity language without checking what the code
touched.

**What the mark does and does not claim.** These items survived an independent skeptic that
was told to refute them and to default to `survives=false`; that pass killed 3 of 32 and
corrected several others. So the *mechanism* and *file:line* have two independent readers
behind them. The *severity and blast radius* have one. T1-28 is the worked example: mechanism
exactly right, blast radius materially wrong.

**Held constant:** no finding was removed, reworded, or re-tiered — only annotated. The
distinction is about who checked it, not about whether I still believe it.

**The pattern this belongs to.** D-45 and D-50 were retracted because the *environment* faked
a finding. This is the same failure one layer up: the *evidence chain* was shorter than the
confidence attached to it. The fix is identical — make the basis visible on the item, so a
reader does not have to trust the register's tone.

## D-76 — T1-30 refuted; the provenance mark paid for itself on the first item

**Claim as filed:** `MemoryRouter`'s cross-tenant guard is "fail-open by construction — the
check compares a caller-supplied value against itself, dead code in the default configuration."

**How I tested it.** Not by re-reading the guard, which is what produced the claim. I built two
tenants in a `MemoryControlPlane`, fixed a router to `alpha`, and had the caller **lie in the
request body** four different ways.

**Result: 4 of 4 rejected**, by two independent layers. `config.py:4319` refuses any `_scope`
not registered to the named tenant; `interop.py:903-909` then refuses a resolved scope that
differs from the router's own. I watched the second one fire on the `_tenant_id=bravo, _scope=B`
arm — the exact attack the finding said would pass. With no control plane at all, the body's
`_tenant_id`/`_scope` are ignored outright.

**Why the reading was wrong.** The agent traced `_tenant_id` to the request body and stopped.
`resolve()` does not echo the caller's scope back — `_resolve_scope` validates it against
`tenant.registered_scope_keys()` first. Reading the guard without reading its input made a
two-layer check look like a tautology.

**Precondition trap, caught mid-run.** My first execution rejected all nine arms with
`tenant 'alpha' has no default_agent_id` — an unrelated config error. Every arm "passed" the
attack for the wrong reason. Had I stopped there I would have refuted the finding on a run that
never reached the guard. Added `default_agent_id` and re-ran. **Fourth time this week that a
failed precondition wore the costume of a result** (D-62, D-68, the rollback probe, this).

**The finding is not zero, but it is not this.** A router built `scope=None` accepts whatever
tenant the body claims unless `principal` is passed. That is the documented contract for a
multi-tenant router — and it is moot today: **`MemoryRouter` is constructed nowhere in `src/`**,
only in `examples/fleet_simulation.py:1427`, with zero `.route(` callers in the package. It is a
hardening note for whoever first exposes it.

**Held constant:** same process, same config objects, same control plane across all nine arms;
only the request body varied.

**What this says about the register.** T1-30 was the **first** AGENT-SOURCED item I hand-checked
after marking them (D-75), and it did not survive. Sample is now 4: two confirmed, one narrowed,
one refuted. Marking them was right, and the mark should stay on until each is checked.

## D-77 — T0-13 confirmed, and the asymmetry is the point

**Claim:** an artifact with no `updated_at` is unconditionally staler than every memory
(`coherence.py:338`), so it holds and soft-retires memory that is correct.

**Confirmed, with a control that matters.** Three arms, only the artifact timestamp varying:
`None` -> 1 incident, updated **yesterday** -> 0, updated 200 days ago -> 1. The third arm is
what makes the first interpretable — without it, "None raises an incident" is equally
consistent with a detector that fires on everything.

**The consequence, measured rather than inferred.** `attribution_confidence` 0.883 against a
0.70 gate, and remediation takes active behavioral rows **1 -> 0**. The user loses a correct
memory because a skill file didn't say when it was written.

**What I'd add to the original framing.** The finding is not "None is falsy-ish, sloppy code".
It is an **asymmetry between the two sides of one comparison**: memory directives get
`updated_at` backfilled three ways (`:181-184`, reinforced_at -> last_seen_at ->
relationship.valid_from) and therefore can never be `None`; artifact directives take
`artifact.updated_at` raw (`:248`). Only one operand can be missing, and missing always loses.
That is also why the fix is obvious — give the artifact side a fallback, or treat unknown as
NOT stale, matching the fail-safe rule the same file already applies to unprojectable artifacts.

**Two preconditions nearly produced a false negative**, both caught before they were read as
results. `escalation_cycles_threshold` defaults to 3 and gates the entire detector on
`observed_count` (`:326`) — and `add_memory` forces `observed_count=1` regardless of supplied
metadata, so every arm would have returned 0. Separately, `add_memory` silently drops
`reinforced_at`, so the memory's `updated_at` falls back to `valid_from` = the real wall clock;
with that unpinned, the "newer" control (NOW-1d) would have been OLDER than the memory and the
experiment would have inverted while still looking sensible. Both knobs are held identical
across all three arms, so neither can explain the difference between them.

**Held constant:** same scope, same memory row, same artifact text and author, same engine and
policy across all arms; only `updated_at` varied.

**Departure from the filed numbers:** filed said confidence 0.762, I measured 0.883. The
difference is setup (observed_count and threshold), not disagreement — both clear the 0.70
gate, which is the part that matters.

**Sample now 5:** three confirmed (T0-14, T0-12, T0-13), one narrowed (T1-28), one refuted (T1-30).

## D-78 — four more hand-checked; two came back sharper than filed

**T1-29 (benchmark has no accuracy gate) — CONFIRMED, and worse than filed.** `failures` takes
exactly two entries, neither of them accuracy; `mean_score` is reported and never compared.
`grep -n 'minimum_' certification.py` returns only `minimum_stability_score`, so there is no
threshold to configure — this is absent, not misconfigured. The part the original missed:
stability is `1.0 - min(1.0, 2*stddev)`, so a suite scoring **0.0 on every repetition** has
zero deviation and therefore a **perfect 1.0** stability score. Consistent total failure is the
best possible input to the only numeric gate the function has.

**T1-32 (rollup demotion) — CONFIRMED as written.** Three demotion sites; `:3638` and
`:4212-4217` both call `_context_demotion_held` and skip pinned members, the latter with a
comment saying a pinned member *"keeps its operator-held place in context"*. `:3948-3952` does
not. Two paths honour the operator's pin, one silently overrides it.

**T1-24 (inert regression gate) — CONFIRMED but REFRAMED, and this is the interesting one.**
Filed as "the gate can never fail for a Motive with the default allow-list". True, but the
*cause* changes what the fix is. `allowed_memory_types` defaults to `()`, and every other
reader in the codebase treats empty as **no restriction**: `dreaming.py:1053` and `:3123` both
skip the filter outright. `replay.py:1100` treats the same empty tuple as **nothing is
allowed**, so its `original_only_allowed` list is unconditionally empty. **The same field is
read in opposite directions by two modules.** So the gate is not merely dormant — it is
inverted, and it is most inert precisely in the permissive default, which is where a lost
baseline candidate is least likely to be caught by anything else.

**T1-26 (credential overwrite on migration) — CONFIRMED by running it, not reading it.** This
one touches credential flow, so a source read was not enough. Seeded both tenants with distinct
keys, migrated, read the destination back: `model` `'dest-model'` -> `'source-model'`,
`base_url` destination -> source. Silent. And durable, because
`seed_tenant_llm_credentials_from_env` returns early when a row exists (`runtime.py:398-400`),
so the usual repair path never runs — the destination bills the source project's key
indefinitely. The probe asserts the destination actually held its own credential first, so an
"overwrite" cannot be reported against a tenant that never had one.

**Held constant:** T1-26 used one process, two stores, distinct credentials differing in every
field; only the migration call happened between the two reads.

**Sample now 9:** seven confirmed (two of them reframed sharper), one narrowed, one refuted.
The reframings are worth as much as the refutation — T1-24's fix is "make the two readers
agree", which is not what the original filing implied.

## D-79 — all 20 agent-sourced findings hand-checked. Final: 17 confirmed, 2 refuted, 1 unverifiable

**Result.** Of the 20 items the 17-agent workflow added and marked AGENT-SOURCED in D-75:

| verdict | n | which |
|---|---|---|
| confirmed as filed | 9 | T0-13, T1-18, T1-20, T1-21, T1-22, T1-23, T1-26, T1-31, T1-32, T2-11, T2-12, T2-14 |
| confirmed but **reframed or resized** | 5 | T1-19, T1-24, T1-25, T1-27, T1-29 |
| **refuted** | 2 | T1-30, T2-13 |
| unverifiable from this repo | 1 | T2-15 |

**The reframings mattered more than the refutations.** Five items had a true mechanism described
in a way that pointed at the wrong fix:

- **T1-24** filed as an inert gate; it is an **inverted** one — `allowed_memory_types=()` means
  "no restriction" at `dreaming.py:1053`/`:3123` and "nothing allowed" at `replay.py:1100`. The
  fix is to make two readers agree, not to add a check.
- **T1-27** filed as "an abort leaves the destination partially populated"; observed is that a
  **successful** re-run duplicates outright — 1 -> 2 -> 3 identical rows with fresh UUIDs.
- **T1-25** filed as "any tenant migration fails"; it needs agent episodes AND zero relationships
  of both kinds, and it is **fail-closed** — it refuses to purge the source. Costs availability,
  not data. Its real cost is provoking T1-27.
- **T1-29** was *understated*: consistent total failure earns a **perfect** stability score.
- **T1-19** filed as "no idempotency"; there is a guard, it just reads a flag that this incident
  kind can never carry.

**A pattern the agents kept missing, and it is one pattern.** T1-19, T1-24, T1-18 and T1-22 are
all the same shape: **a check that exists and cannot fire.** Reading the check tells you it is
there; only following its input tells you it is dead. T2-13 is the same shape inverted — the
agent read the mapping function, saw it could return `None`, and assumed silence; following the
value to `_score_memory_agent_bench` shows it **raises**. Both refutations and three of the five
reframings come from the same discipline: *read the consumer, not just the producer.*

**What the agent pass was actually worth.** Every mechanism and `file:line` it cited was real —
including in the two refuted items, where the code was exactly where the agent said it was. What
it could not reliably do was follow a value one hop further to decide severity. That is a usable
rule for the next sweep: trust agent-sourced *locations*, re-derive agent-sourced *consequences*.

**Probe bugs caught before they became findings, this session:** the phantom-hold control was
manufactured by my own first `run_coherence_scan` applying the hold (fixed by measuring
confidence on a throwaway instance); T1-25 silently succeeded until an agent was actually
registered; T0-13 had two independent preconditions that would each have produced a clean false
negative. Every probe now exits 2 rather than 0 when its control arm fails.

**T2-15 is the one I could not close.** The repo side is confirmed; the Anthropic spec side is
not in this tree, and I will not certify a field-name mismatch from recollection.

## D-80 — we were dogfooding half the product, and the half we were running is the half the rules disown

**The question Ryan asked:** are we tracking this session against live Memotron?

**Answer: partly, and the gap is a finding.** The lifecycle hooks *are* running — the
2026-08-27T18:08Z compaction queued a checkpoint episode and formation extracted **12 memories**
into `agent:claude-code`. That is a real round trip on the mechanism that used to be broken.

**But the deliberate write path has never been connected.** `.mcp.json` configures
`memotron_agent_memory`, and a manual stdio handshake confirms the server is healthy — 27
tools, 18 `memory_*`, including `memory_publish`, `memory_remember` and `memory_search`. It was
simply never approved: `enabledMcpjsonServers` is `[]` in the main checkout and this worktree had
**no `~/.claude.json` entry at all**. So across two days I made zero deliberate writes — not by
judgment, by unreachability.

**Why that matters rather than being a setup nit.** `.claude/rules/memotron.md` says semantic
writes are *"event-driven agent decisions, not hook-driven transcript capture"* and *"never store
transient output"*. We have been running exactly the mechanism the rules disown, as a
**replacement** for the one they prescribe.

**What that produced, measured.** 11 of 11 `directive` rows are `active`, including
`should run sweep_sdk.py` and `should read dreaming.py` — both completed ~16 hours earlier — and
`should verify T1-30 defect`, which instructs a future session to verify a finding **we refuted
today**. Not merely stale: wrong. Nothing retires them, because a completed action has no
contradicting successor, so the truth slot never re-keys and supersession never fires. Four of
six directives from today's compaction are transient actions (`should push commit 5ce3236`).
Meanwhile project scope received **0 rows today**; its 3 facts date from 08-26. None of the day's
actual results — T1-27 duplicating on retry, T1-24 being inverted, two refutations — are in
memory anywhere. Filed as **T1-33**.

**A near-miss worth recording, because it is the fifth this week.** My first attempt to list the
server's tools reported **0 tools advertised** — I had killed the subprocess before it flushed
its response. Reported as-is that would have been a fabricated finding ("the MCP server exposes
no tools") about a server that exposes 27. The fix was the same as every other time: complete the
protocol (send `notifications/initialized`, then read until the response arrives) instead of
sampling whatever had arrived when I stopped listening. **A timeout is not a measurement.**

**Held constant:** same graph file, same binary, same working directory across both handshake
attempts; only the client's protocol completeness differed.

**Action taken:** wrote `enabledMcpjsonServers: ["memotron_agent_memory"]` for this worktree
(atomic replace, 58 -> 59 project entries, other 57 verified intact, backup kept). **It does not
affect this session** — MCP servers attach at startup, so the deliberate write path is available
from the next session onward, not now.

## D-81 — three of the four loose ends closed, and the gate caught a real one on its first run

**T2-15 — CLOSED, CONFIRMED, and bigger than filed.** Read Anthropic's published
`memory_20250818` definition instead of trusting recollection. **4 of 6 commands would fail**,
not 3: `create` takes `file_text` (backend requires `content`), `insert` takes `insert_text`
(requires `new_str`), `rename` takes `old_path` (requires `path`). The one the original filing
missed is the worst: `view` accepts an optional **`view_range`** which the backend does not
implement at all — a *silent* behaviour gap rather than a loud error. Only `str_replace` and
`delete` match. `README.md:1346`'s "zero code changes" is false.

**T1-27 — CUTOVER VERDICT: yes, it blocks.** The question was whether migration is even on the
Postgres path. First reading said no — the CLI takes `--graph-path` file paths and
`open_storage` **hardcodes `engine="sqlite"`**. But `migrate_tenant_memory` accepts a
`dest_store` object, so I built a live `PostgresStorageBackend` and ran it: SQLite source ->
Postgres destination **works**, and duplicates identically — **1 -> 2 -> 3 rows across three
runs**, each reporting success. So migration *is* the cutover path, and it is the path that
duplicates. Filed **T1-34** for the other half: there is **no CLI route**, so a cutover is a
hand-written SDK script, and re-running that script after any hiccup multiplies the destination.

**Workstream C — the gate now exists.** `scripts/ci-build-check.sh` (two lanes) plus a
`Build_Check` stage inserted before `Build` in `.harness/pipeline.yaml`, which now reads
Pipeline_Validation -> **Build_Check** -> Build -> Deploy_Helm. No Harness PAT needed: the
pipeline is `storeType: REMOTE`, so the in-repo YAML is the source of record. The Postgres lane
reports **SKIPPED, not PASSED**, when no DSN is set — a silent skip there is exactly how T0-4
and T0-5 survived a green suite.

**And on its first real run the gate went red — correctly.**
`test_post_compact_hook_persists_only_sanitized_continuity` failed with `StopIteration`. Rather
than assume, I varied **only the working directory**: from the repo (a `.env` present)
**FAILED in 8.70s**; from a directory with no `.env` the same test **PASSED in 1.11s**. Same
commit, same interpreter. **The 8x runtime is the diagnosis** — the failing arm is making real
gateway calls, so no `HAS_STATE` fact forms and `next(...)` raises.

That is **T1-14**, and it upgrades the item: the cwd-relative `load_env_file()` makes the suite
red in *any worktree a developer actually uses*. Fixing it is a prerequisite for Workstream C
being usable locally, not the optional cheap win I had it filed as. The gate **does not
neutralise it** — it detects a cwd `.env` and prints why you are red. Papering over it would
make the gate green while the defect stands.

**Held constant:** T1-14 arms — same commit, same interpreter, same test id, same
`MEMOTRON_ALLOW_EPHEMERAL_KEK`; only cwd varied. T1-27 arms — same source store, same
destination backend, same call; only the number of invocations varied.

**Two harness bugs of my own, caught before they mattered:** `--timeout` passed to a pytest
without `pytest-timeout` installed (both lanes errored at exit 4 and I nearly read that as a
product failure), and my first invocation piped the script through `tail`, so the reported
`EXIT=0` was tail's status, not the gate's. The gate's real exit code is 1 on failure — checked
separately. **A pipeline swallows exit codes; a CI gate that does that is worse than none.**

## D-82 — the handoff docs had T1-33's disease, in prose

**What I found while making the handoff current.** `STATE.md`'s `**Next:**` pointer told the
next session to do three things that are **already done** — close C0, write
`scripts/ci-build-check.sh`, add the `Build_Check` stage — and to treat **T1-14** as an
"optional cheap win" when session 9 upgraded it to a blocker. `DISCOVERY.md` still listed
`kubectl get pods` as an open question (answered 2026-08-26) and asserted "there is no test CI"
(written that morning). `docs/findings/README.md`'s open threads listed five closed unknowns and
had duplicate numbering.

**This is exactly T1-33 in prose.** A completed instruction has nothing to contradict it, so it
persists and reads as current. The memory graph has that problem because supersession keys on
subject + predicate and a finished action has no successor; the docs have it because a `Next:`
line is only ever appended to, never re-evaluated. **Same failure, two substrates** — which
makes T1-33 less a Memotron quirk than a property of any append-only record of intentions.

**Fix applied, and it is structural rather than a one-time cleanup.** `STATE.md` now opens with
**"## Next session — start here"**, dated, stating outright that the `**Next:**` lines below it
are *historical* and that several describe finished work. The two old pointers are relabelled
**SUPERSEDED** in place rather than deleted, so the record of what each session believed
survives without being mistaken for an instruction.

**The handoff also warns about the injected memory itself** — that a future session will be
handed `should verify T1-30 defect` for a finding we refuted, and should check the register row
before acting on any remembered directive. That is the first place the two substrates are
cross-referenced.

**Verified rather than asserted:** every `scripts/`, `docs/`, `src/`, `tests/` path cited in the
new handoff resolves on disk (checked programmatically, 0 missing), and every register id it
references exists in `TAKEOVER-BACKLOG.md` (9 of 9). T1-30's row was re-read to confirm it does
say **REFUTED** before the warning was written against it.

**What would make this wrong:** if the next session reads injected memory *before* `STATE.md`
and acts on a directive immediately, the warning never gets seen. The ordering is not something
the docs can enforce — only the agent's own discipline, which is precisely what T1-33 says
cannot be relied on.

## D-83 — fixing the ordering problem instead of documenting it

**The gap D-82 left open.** The handoff warns that injected memory contains a directive pointing
at a **refuted** finding. But a warning only works if it is read *before* the thing it warns
about, and **nothing enforces read order**. An agent that acts on an injected directive first
never sees it. Writing "the docs cannot enforce this" was accurate and not a fix.

**The mechanism.** Memotron's `SessionStart` hook writes the memory profile to stdout and the
harness surfaces it. Claude Code allows **multiple hooks per event**, so a second `SessionStart`
hook lands its output in the *same payload* as the memory profile. Ordering stops mattering —
not because the reader is disciplined, but because there is no longer an interval in which the
bad data is present and the warning is not.

`scripts/hooks/session-brief.sh`, registered after Memotron's own hook. Verified: prints the
warning subsection, exits 0.

**It reads the section out of `STATE.md` rather than carrying a copy.** A hardcoded duplicate
would rot exactly the way the `**Next:**` pointers did — the specific bug this exists to counter.
Reintroducing that failure inside its own fix would have been the whole problem in miniature.

**Fails open on purpose.** Missing or unreadable `STATE.md` prints nothing and exits 0 (verified
with `CLAUDE_PROJECT_DIR=/tmp`). A briefing that breaks session start is worse than no briefing —
the opposite of the fail-closed choice made for the KEK guard, because the failure modes are not
comparable: one loses a warning, the other loses key material.

**A portability bug caught in test, not in a session.** `sed '${/^$/d}'` is a GNU-ism; BSD sed
rejected it and **swallowed the entire section**, leaving only the footer. The hook still exited
0, so this would have failed **silently** at every session start — a briefing that appears to
work and delivers nothing. Removed. Same shape as the near-misses in D-80/D-81: the exit code
said fine, the output said otherwise.

**What this does NOT fix.** The stale directives are still in the graph, still `active`, still
served to every session. This makes them survivable, not absent. The real fix is retiring them —
`memory_forget` exists as an MCP tool but the CLI has no equivalent (`init|mcp|hook|status|llm|
migrate`), so it was unreachable this session. **Deliberately left for next session**, for two
reasons: it exercises the write path we have never used, and those 11 rows are currently the only
live reproduction of T1-33. Retiring them before the finding is fixed would destroy its evidence.

## D-84 — "are we pointed at the right Memotron?" — audited, and the answer is yes for a reason I had not checked

**Ryan's question was well-aimed.** Three checkouts of this repo exist. A mispointed binary or a
shared graph would invalidate every dogfooding observation from the last two days, including
T1-33.

**The finding I did not expect: the binary is not this worktree's code.** `memotron` on PATH
is a `uv tool install` **frozen copy** in its own `site-packages` — no editable link to any
worktree. Every hook and the MCP server have been running a snapshot the whole time. I had been
reasoning as though they ran `src/`.

**How stale, measured rather than assumed:** `diff -rq` reports **exactly three differing files**,
and they are **exactly our three Tier-0 fixes** — `agent_memory.py`, `storage/postgres/__init__.py`,
`storage/postgres/_graph.py`. Both files I could diff against `f28e95c` match it byte-for-byte.
The installed tool **is** `origin/main`.

**So the observations survive, and here is the honest form of that claim:** all three deltas are
Postgres/storage-config specific and dogfood runs on SQLite, so none is reachable on the path we
exercised. That is **DERIVED** — from three file identities plus the configured backend — not
observed. It breaks if any of those files also alters SQLite behaviour, which reading them says
they do not.

**Graph and concurrency both clean.** `graph_path` is relative and resolved against
`--project-root`, there is no `~/.memotron/memory.sqlite`, and of the three checkouts **only
this one has a `.memotron/` directory at all** — the other two sit at the spike commit with no
Memotron config. Nothing else is writing the dogfood graph.

**One latent bug found on the way.** The hook requires `--project-root ${CLAUDE_PROJECT_DIR}`;
`.mcp.json` uses `${CLAUDE_PROJECT_DIR:-.}` — **falls back to cwd**. Unset that variable and the
MCP server binds its graph to whatever directory launched it. **That is T1-14's exact shape in
the MCP config**, and it will matter from next session, when the MCP path is live for the first
time.

**The lesson, which is the same one as T1-30 and T2-13:** I knew the hooks "ran memotron" and
never asked *which* memotron. Knowing a component is invoked is not knowing what code it
executes — the same gap as reading a producer without following the value to its consumer.
