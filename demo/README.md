# Memotron live demo — operator runbook

A scripted, repeatable demonstration of Memotron memory inside a **real
Claude Code CLI session**. You paste prompts; after each stage the audience
watches the memory graph change, with dreaming happening at two clear moments
you control.

Runs in an isolated tenant and its own graph file. **Your `spymaster` graph is
never opened** — `setup.sh` asserts that and prints both paths.

- **Setup:** ~2 seconds
- **Rehearsal (`selftest.py --reset`):** ~45 seconds against the JedAI Gateway
  (~4 seconds and fully offline with `DEMO_EXTRACTION_MODE = "deterministic"`)
- **Live demo:** 10–15 minutes with talking

---

## 1. What this demo proves

1. **The agent writes rarely and deliberately.** Three confirmed facts across
   five prompts; the speculative question produces *zero* memories. That is
   operator policy (`agent_guidance` + a typed Motive), not luck.
2. **Memory is not written at conversation speed.** Publishes queue as
   immutable evidence. Truth only changes when Memotron dreams.
3. **Corrections supersede, they do not overwrite.** The old fact stays, with a
   closed valid-time interval and a pointer to its successor.
4. **Repetition is evidence, not duplication.** The same requirement stated
   twice is one row with `observed_count = 2` and higher confidence.
5. **The graph gets smaller as it learns more.** Three sibling rules collapse
   into one theme — an LLM-written sentence that must pass a deterministic
   entailment gate before it may become memory — and seven episodes render as
   three context-visible facts.

---

## 2. One-time setup

```bash
cd /Users/logan.robbins/jedai/wdpr-memotron
./demo/setup.sh
```

This creates `demo/workspace/` (a real `git init` repo — adoption requires
one), runs `memotron init` there, applies demo mode, and rewires the MCP
server and hooks onto `demo/bin/dw-demo`. It ends with a READY banner and an
isolation assertion:

```
   resolved graph    .../demo/.memotron/demo-memory.sqlite
   protected graph   .../.memotron/spymaster.sqlite
   ASSERT OK         demo graph != spymaster graph
   spymaster size    146M — separate file, never opened by the demo
```

`_demo.assert_isolated()` re-checks this on every script run, not just setup,
and exits non-zero if the resolved path is ever the spymaster graph, the
default shared `~/.memotron/memory.sqlite`, or anything other than the demo
file. (The spymaster file may change size while you demo — a separate
long-running `memotron-local-platform` process owns it. Different file, no
interaction.)

Optional dry run before you present — proves every number below is real. It
makes live gateway calls (extraction + dream-agent decisions) on the tenant's
virtual key:

```bash
uv run demo/selftest.py --reset
```

Then reset to a clean state for the live run:

```bash
./demo/setup.sh --reset
```

---

## 3. Terminal layout

| | |
|---|---|
| **Terminal 1** | the Claude Code session, in `demo/workspace/` |
| **Terminal 2** | the repo root, running `demo/dream.py` and `demo/inspect.py` |

Make terminal 2 at least **96 columns** wide or the tables wrap.

Start the session:

```bash
cd demo/workspace && claude --strict-mcp-config --mcp-config .mcp.json
```

Approve `memotron_agent_memory` once when prompted.

> **Keep this session open for the whole demo.** The `SessionEnd` hook runs
> due dreams; exiting between stages would dream before you meant to.

---

## 4. The run

| # | Where | Do this | Point at |
|---|---|---|---|
| 1 | T1 | session starts | The `SessionStart` banner: *"Memotron simple memory loaded"*, the task-run id, and the empty memory profile |
| 2 | T1 | paste `demo/prompts/stage1.md`, prompt by prompt | 3 `memory_publish` calls, and **zero** on prompt 1.5 |
| 3 | T2 | `uv run demo/inspect.py --save before-dream-1` | `pending_episode_count: 3`, `context-visible: 0` — published, not yet true |
| 4 | T2 | `uv run demo/dream.py --stage 1` | **DREAM 1.** FORMATION `epis 3 / new 3`; consolidation finds no theme and does not invent one |
| 5 | T2 | `uv run demo/inspect.py --diff before-dream-1 --save after-dream-1` | Three `+ NEW` rows; `compression_ratio 1.000`; `tokens_saved_vs_raw 0` — say *"memory has not paid for itself yet"* |
| 6 | T1 | paste `demo/prompts/stage2.md`, prompt by prompt | 2.1 recalls the formed facts (the graph changed under a live session); 2.6 still says PostgreSQL — queued, not dreamed |
| 7 | T2 | `uv run demo/dream.py --stage 2` | **DREAM 2.** `new 3`, `reinf 1`, `supers 1`, plus a theme |
| 8 | T2 | `uv run demo/inspect.py --diff after-dream-1 --save after-dream-2` | The four beats below |
| 9 | T1 | ask what it knows now | The agent says **DynamoDB** |

Full talk track per prompt lives in `demo/prompts/`:
`stage1.md` → `dream1.md` → `stage2.md` → `dream2.md`.

### The four beats in the step-8 delta

```
~ active->superseded  477d39d3  decision     ...PostgreSQL...  (superseded by baedda9f)
~ obs 1->2            ba2da1f1  requirement  ...p95 latency... (reinforced — no duplicate row)
~ DEMOTED             14a35b7a  directive    ...data-platform review... (absorbed by theme 76d488c0)
+ NEW                 76d488c0  theme        Theme (memotron-demo) summarizes ...
```

---

## 5. The numbers (measured, not aspirational)

Verified end-to-end by `uv run demo/selftest.py --reset` on 2026-08-12, in
`DEMO_EXTRACTION_MODE = "llm"` — every count below came out of real gateway
extraction, real gateway dream-agent decisions, and real gateway theme
synthesis, not the hermetic transports.

| Signal | After Dream 1 | After Dream 2 |
|---|---:|---:|
| raw episodes | 3 | 7 |
| facts created (lifetime) | 3 | 7 |
| reinforcements | 0 | **1** |
| supersessions | 0 | **1** |
| themes | 0 | **1** |
| demoted members | 0 | **3** |
| active facts (incl. demoted) | 3 | 6 |
| **context-visible facts** | **3** | **3** |
| `compression_ratio` | 1.000 | **2.333** |
| `semantic_dedup_rate` | 0.000 | **0.125** |
| `tokens_raw_episodes` | 113 | 263 |
| `tokens_rendered_profile` | 199 | 255 |
| `tokens_saved_vs_raw` | **0** | **8** (3%) |
| `tokens_saved_by_demotion` | 0 | **36** |

The headline: **episodes went 3 → 7, context-visible facts stayed at 3.**

Every count is exactly reproducible — `selftest.py` asserts the structural
ones, and the token figures are now stable run to run too, because the theme
text is a temperature-0 model sentence rather than a token bag whose ordering
depended on row insertion.

**Say the token numbers honestly.** Until the LLM theme summary landed
(2026-08-12) this table read `~68` saved / 26%, because the theme was a 50-char
token bag — cheap, and unreadable. A real summary costs about 170 characters,
so the saving against raw episodes drops to 8 tokens. What you should point at
is `tokens_saved_by_demotion: 36`: three directives worth 120 unbudgeted tokens
are represented by one theme, and *that* ratio is the compaction claim. The
"saving vs. raw episodes" line is small at this corpus size by construction —
seven episodes is not where compaction pays; it is where you can still read
every row and check the machine's work.

---

## 6. "Only Memotron for memory" — what that actually means

Researched, not assumed. Three things are true, and the third is a caveat you
should say out loud rather than paper over.

**What is enforced.**

- `--strict-mcp-config --mcp-config .mcp.json` is a supported Claude Code flag
  that loads **only** the servers in that file. The demo workspace's
  `.mcp.json` contains exactly one server: `memotron_agent_memory`. No
  other memory-bearing MCP server can load.
- The workspace's **`CLAUDE.md` points at Memotron** rather than holding
  project knowledge itself: "durable memory for this project lives in
  Memotron… write it to memory or it does not exist", followed by when to
  search, when to publish, and the one-line candidate format. CLAUDE.md is the
  most-read instruction surface in a repository, so the demo *uses* it instead
  of fighting it. The authoritative, operator-tunable copy of the same rules is
  rendered by `memotron init` into the always-loaded
  `.claude/rules/memotron.md` from `demo_config.py`'s
  `DEMO_AGENT_GUIDANCE` — change the guidance there and both surfaces follow.
- Everything project-*specific* still comes only from Memotron: the
  `SessionStart` hook's injected memory profile and the MCP tools. CLAUDE.md
  carries the contract; it carries no facts.
- The workspace is a fresh path, so Claude Code's own auto-memory for this
  project starts empty.

**The caveat.** Your **user-level `~/.claude/CLAUDE.md` still loads** in any
directory, and there is no supported flag that disables CLAUDE.md discovery on
its own (`--bare` disables it but also disables hooks, which Memotron needs
for `SessionStart` / `SessionEnd`). So we do not use `--bare`, and we do not
pretend the user file is gone.

**How to show it rather than assert it.** Run this in the session:

```
/memory
```

It lists the loaded memory files. Open the project `CLAUDE.md` on screen: it is
*instructions to use Memotron*, not a store of project facts — the facts all
live in the graph. Then avoid the `#` shortcut during the demo; it appends
facts to CLAUDE.md, which is exactly the habit this demo replaces.

---

## 7. Files in this kit

| File | What it is |
|---|---|
| `setup.sh` | Builds the isolated workspace + graph, runs `memotron init`, applies demo mode, asserts isolation. `--reset` wipes and rebuilds. |
| `demo_config.py` | The **only** place demo policy is set: extraction mode, LLM credential, `agent_guidance`, and the project-memory Motive (types / salience / cap / dedup). `--show` prints the effective policy without changing it. |
| `dream.py` | Runs one dreaming session — FORMATION, CONSOLIDATION, PRUNING — and prints what each did. |
| `inspect.py` | The graph viewer. Active facts, themes and their members, superseded truth, the evolution proof, and the WS-22 token fields. `--save` / `--diff` for BEFORE→AFTER. |
| `selftest.py` | Headless rehearsal that drives the same tool calls and asserts the numbers in section 5. |
| `prompts/` | `stage1.md`, `dream1.md`, `stage2.md`, `dream2.md` — what to paste, what the agent should do, what to look for. |
| `bin/dw-demo` | Launcher the workspace's `.mcp.json` and hooks point at (repo virtualenv + demo transports). |
| `dw_shim.py` | Installs the demo transports for out-of-process launches. |
| `_demo.py` | Shared paths, platform construction, isolation assertion, table rendering. |
| `_demopath.py` | Import guard: `inspect.py` shadows the stdlib `inspect` module, so this un-shadows it. Import it first in every entry script. |
| `workspace/` | The scratch repo the Claude Code session runs in (generated). |
| `snapshots/` | Saved `inspect.py` snapshots (generated). |

`src/memotron/` and `tests/` are untouched by this kit.

---

## 8. Five design decisions, and why

**The demo runs on the platform, not on a stub.** `DEMO_EXTRACTION_MODE =
"llm"` in `demo_config.py` is the default, and it drives all three model-facing
transports off the sealed tenant credential:
`OpenAICompatibleExtractionTransport` for formation,
`OpenAICompatibleDreamAgentTransport` for every dream-agent approval, and
`OpenAICompatibleSynthesisTransport` for theme synthesis — each a real call to
the JedAI Gateway, billed to this tenant's virtual key. `LITELLM_API_KEY` works
against the *preview* (Integration) gateway
`https://preview.jedai-gateway.wdprapps.disney.com/v1`; it returns HTTP 401
`token_not_found_in_db` on latest/stage/prod, so the environment matters.

Flip the constant to `"deterministic"` for an offline, zero-cost, byte-identical
rehearsal: extraction becomes the hermetic `RuleBasedExtractionTransport`, which
parses the structured `Memory: subject=…; predicate=…; object=…` line the demo's
`publish_when` guidance already tells the agent to write, and dream-agent
approval falls back to `LocalDreamAgentTransport`, and the theme text falls back
to the deterministic token-bag label. The *structural* numbers in §5 are the same
either way; the four token figures are not, because the theme text differs (a
model sentence is longer than a token bag). §5 was verified in `"llm"` mode.

**LLM extraction used to shatter the graph, and that was a Memotron bug.**
Until 2026-08-12, `"llm"` mode failed Dream 2 outright: the extractor folded the
object into the predicate (`predicate="decided to use DynamoDB for the
reservation ledger"`, `object="DynamoDB"`), so the Stage-2 correction landed in a
different `scope:subject:predicate` truth slot from the Stage-1 decision and
supersession, reinforcement, and clustering all silently stopped. It looked like
model variance; it was a prompt defect. The rendered extraction prompt never said
what a predicate *is*. It now carries an explicit predicate contract, and
`InstructionalExtractor._validate_memory` rejects a folded candidate
(`PREDICATE_NOT_A_VERB_PHRASE`, receipted, non-fatal). Re-measured on the same
model at temperature 0: `predicate="decided"`, `object="use DynamoDB for the
reservation ledger"`, 3/3.

**The theme text used to be a token bag, and that was the same kind of bug.**
Until 2026-08-12 Dream 2's theme read `reservation migration should on
checkout-api ledger`. The synthesis transport was live and the entailment gate
was implemented, but LLM theme synthesis required a *second* opt-in —
`ConsolidationSynthesisProfile.model_identifier` — that no packaged config,
Motive, or preset ever set, so the model was never called, no prompt digest was
stamped, and there was no rejection receipt either, because there had been no
attempt. A configured model seat that silently never fires is a defect, not a
default. Configuring a `SynthesisTransport` is now the whole opt-in;
`model_identifier` only overrides the recorded provenance string, defaulting to
the transport's own identifier. Dream 2's theme now reads:

```
checkout-api should require a data-platform review on every reservation ledger migration.
checkout-api should require a load test on every reservation ledger migration.
```

with `theme_model_identifier = openai-compatible:claude-haiku-4-5` and a
`theme_synthesis_prompt_digest` on both the row and the
`CONSOLIDATION_CLUSTER_SUMMARIZED` receipt. Every sentence had to pass the
deterministic entailment gate — each one restates exactly one member fact, and
a sentence blending two members is rejected as a recombination and falls back
to the token-bag label with a `CONSOLIDATION_THEME_SYNTHESIS_REJECTED` receipt.
(That rule was tightened in the gate before it was stated in the prompt, so the
first live run *was* rejected, `recombined_across_members:api`; the prompt now
states the contract the gate enforces.)

**`dream.py` forces jobs instead of calling `memory_refresh`.** The hooks call
`run_due_dreams`, which honours cadence (formation 1 s, consolidation 300 s,
pruning 600 s) — correct in production, useless on stage, because the second
dreaming session would silently skip consolidation. `run_dream_job` runs a
named job regardless of cadence on the same engine, Motive, policy, and
receipts. The hooks stay wired and still work; you just do not wait five
minutes in front of an audience.

**The correction is designed to land in one cycle.** `SupersessionPolicy` lives
on the packaged `DreamConfig`, not on tenant policy, so the demo runs the
shipped defaults `corroboration_margin=3` / `corroboration_required=2`: an
equal-authority challenger only parks for review once the incumbent has been
observed 3 times. The demo states the corrected fact exactly **once**, so it is
below the margin and flips immediately — no override needed and nothing
pretended. (For a knowledge-base tenant the documented override is
`SupersessionPolicy(corroboration_required=1)`; see `INGEST.md` §2.3.)

---

## 9. Rehearse, reset, troubleshoot

```bash
uv run demo/selftest.py --reset      # full rehearsal + assertions, ~15s
./demo/setup.sh --reset              # clean slate for the live run
uv run demo/demo_config.py --show    # show the effective policy, change nothing
uv run demo/inspect.py --all-scopes  # project + personal + session-continuity
```

| Symptom | Fix |
|---|---|
| Tables wrap | Widen terminal 2 to ≥ 96 columns |
| `no snapshot named 'x'` | Run the `--save` step for that label first, or `./demo/setup.sh --reset` |
| Agent publishes prose instead of the structured line | It ignored `.claude/rules/memotron.md`; re-run `uv run demo/demo_config.py` and restart the session |
| Agent publishes more than once per fact | Same fix; the rule is the lever, not the prompt |
| Facts already exist at Stage 1 | A previous run was not reset — `./demo/setup.sh --reset` |
| Dreaming happened before you ran `dream.py` | The session was exited (or compacted) between stages — the `SessionEnd` hook dreams. Reset and keep the session open |
| `LITELLM_API_KEY is not set` | `setup.sh` reads the gitignored repo `.env`; export the variable or add it there. No script ever prints its value |

### The simulated path

`selftest.py` calls the same `AgentMemoryPlatform` methods the MCP tools call
— `memory_bootstrap`, `memory_search`, `memory_publish` — with the exact
candidate lines the prompt files are written to elicit, then shells out to the
real `dream.py` and `inspect.py`. It cannot decide *when* to publish; that is
what the live session demonstrates. Everything after the publish — dreaming,
supersession, reinforcement, clustering, token accounting — is the identical
code path, which is why the section 5 numbers are measurements rather than
estimates.
