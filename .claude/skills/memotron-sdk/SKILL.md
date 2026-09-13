---
name: memotron-sdk
description: Configure or integrate Memotron memory for Claude Code, MCP clients, hosted agents, or Python applications. Use for setup, registration, lifecycle, compaction, scope selection, project-memory publication, outcomes, and debugging.
---

# Memotron integration

Memotron is a typed temporal memory graph. Raw episodes are immutable evidence.
Dreaming forms, supersedes, consolidates, and prunes memory under a Motive and scoped
policy. Retrieval and outcome receipts do not rewrite truth.

## Claude Code: preferred setup

Install the command once, then initialize each repository:

```bash
uv tool install jedai-memotron
memotron init
```

`memotron init` is idempotent. It infers project identity from Git `origin` and
installs:

- `.memotron.yaml`: versioned project identity, mode, storage, and project-memory policy.
- `.mcp.json`: project-scoped stdio MCP server named `memotron_agent_memory`.
- `.claude/settings.json`: `SessionStart`, `PreCompact`, `PostCompact`, and
  `SessionEnd` hooks.
- `.claude/rules/memotron.md`: concise always-loaded operating rules.
- `.claude/skills/memotron-memory/SKILL.md`:
  `/memotron-memory status|why|forget|review`.

Restart Claude Code and approve the project MCP server once. Validate with:

```bash
memotron status
```

The stdio server and hooks use the same repository config and the local graph at
`~/.memotron/memory.sqlite`; no background service or fixed port is required.
Missing Git/project configuration fails with an actionable error.

## Local LLM setup

Check the active transport after initialization:

```bash
memotron llm status
```

Without configured credentials, local Memotron uses deterministic rule-based
extraction. Every LLM call Memotron makes — extraction, theme synthesis,
dream-agent decisions, embeddings — goes through the JedAI Gateway (LiteLLM).
Put the gateway virtual key in the process environment or a gitignored
repository `.env` as `LITELLM_API_KEY`, then configure only non-secret values:

```bash
memotron llm configure \
  --provider litellm \
  --base-url https://preview.jedai-gateway.wdprapps.disney.com/v1 \
  --model claude-haiku-4-5 \
  --api-key-env LITELLM_API_KEY
memotron llm status
```

Gateway model names are **undated aliases** — `claude-haiku-4-5`,
`claude-sonnet-4-6`, `claude-opus-5`, `gpt-4.1-mini`. Dated Anthropic ids such
as `claude-haiku-4-5-20251001` do not resolve. Gateway keys are also
per-environment: a `preview` key returns HTTP 401 on latest/stage/prod, so the
`--base-url` must match the environment the key was minted for.

`--model` is the LiteLLM proxy `model_name`. `--base-url` is the proxy root whose
OpenAI-compatible `/chat/completions` endpoint Memotron calls. The command reads
the named environment variable, seals the key into the local graph, and versions
only provider/model/endpoint/environment-variable name in `.memotron.yaml`.
Never pass keys in shell arguments, repository files, prompts, memories, or logs.

`litellm` and `openai` are the only providers. `openai` exists for any other
OpenAI-compatible endpoint; there is no Anthropic-native provider — Claude is
reached through the gateway:

```bash
memotron llm configure \
  --provider openai \
  --base-url https://api.openai.com/v1 \
  --model gpt-4o-mini \
  --api-key-env OPENAI_API_KEY
```

To use a gateway embedding space instead of the hermetic local 256-dim default,
set `MEMOTRON_EMBEDDING_PROVIDER=litellm` and
`MEMOTRON_EMBEDDING_MODEL=text-embedding-3` (3072 dims) **before the
tenant's first episode** — changing it later invalidates every stored vector.

Use `memotron llm clear` to remove the sealed credential and return to
environment discovery/rule-based fallback. For a remotely hosted platform,
`tenant_llm_status`, `tenant_llm_configure`, and `tenant_llm_clear` expose the
equivalent MCP lifecycle.

When asked to set up Memotron locally:

1. Run `memotron init`, then `memotron llm status`.
2. If rule-based fallback is acceptable, do not request an API key.
3. If an LLM is required, collect only provider, model, endpoint, and key
   environment-variable name. Never ask the user to paste the raw key into chat.
4. Ask the user to export the key or place it in an already-gitignored `.env`.
5. Run `memotron llm configure`, verify `llm_ready: true`, then tell the user
   to restart Claude Code and approve `memotron_agent_memory`.

Claude Code uses the generated stdio MCP entry and needs no background daemon.
Use `memotron-local-platform` only for hosted HTTP/MCP consumers.

## Minimal agent contract

For a generic MCP or hosted client:

1. Call `memory_bootstrap(agent_id, agent_name, task_run_id)` once at session start.
2. Inject `start.rendered_context` and reuse `start.task_run_id` throughout the task.
3. Call `memory_search` before relying on prior work.
4. Use `memory_remember` for one exact durable personal/agent fact and
   `memory_publish` for sourced project evidence.
5. Report `memory_outcome` only after a named evaluator observes the result.
6. Signal compaction/handoff with `memory_log`; call `memory_start` afterward.

Claude Code hooks perform bootstrap and boundary handling automatically. Use
`memory_contract` when implementing another harness or diagnosing configuration.

## Adoption modes

`simple` is the default:

- Claude Code is a registered caller and source attribution, not a durable memory owner.
- `memory_remember` writes user-owned `user:<derived-user-id>` memory, shared across
  this user's repositories.
- `memory_publish` queues governed `tenant:<project-id>` evidence for repository memory.
- `agent:claude-code` is internal session/compaction continuity and is rendered as
  “session continuity,” not a user-facing memory scope.
- `memory_start` and `memory_search` use personal + project + continuity memory.

`multi-agent` preserves explicit agent ownership:

- `memory_remember` writes `agent:<agent-id>`.
- `memory_publish` queues shared `tenant:<project-id>` evidence.
- `memory_start` and `memory_search` use agent + project memory.

Configure the mode in `.memotron.yaml` or idempotently:

```bash
memotron init --mode simple
memotron init --mode multi-agent
```

Changing mode changes active routing; it does not rewrite or delete existing scopes.

## Repository configuration

The generated YAML is authoritative:

```yaml
version: 1
mode: simple
project:
  id: github.example.com/team/repository
  name: repository
caller:
  id: claude-code
  name: Claude Code
storage:
  graph_path: ~/.memotron/memory.sqlite
llm:
  provider: litellm
  model: memotron-memory
  base_url: http://127.0.0.1:4000
  api_key_env: LITELLM_API_KEY
agent_guidance:
  cadence: event-driven
  search_when:
    - At a new task or topic, after compaction, or before relying on prior work.
  remember_when:
    - An exact durable personal fact is explicitly stated or confirmed.
  publish_when:
    - A sourced requirement, decision, incident, mitigation, blocker, or handoff is confirmed.
  review_when:
    - At confirmation, validation, handoff, and task-completion milestones.
  refresh_when:
    - When a meaningful batch must be available immediately.
project_memory:
  project_goal: Help engineers evolve repository safely and efficiently.
  memory_goal: Preserve the current shared understanding needed for correct decisions.
  keep:
    - Current requirements, architectural decisions, and operating constraints.
  exclude:
    - Secrets, transient output, and unverified model speculation.
  rules:
    - Prefer newer adequately authoritative facts while preserving history.
  allowed_memory_types:
    - requirement
    - directive
    - state
    - decision
    - incident
    - theme
  protected_memory_types:
    - requirement
    - decision
    - incident
  min_salience: 0.0
  max_memories_per_candidate: 12
  dedup_threshold: 0.87
```

Changing project-memory fields creates a new active policy version on next startup.
Do not put user identity or secrets in the repository config. Memotron derives a
non-identifying user scope from Git identity and stores data locally.

`agent_guidance` controls when the caller searches, reviews, remembers, publishes,
and refreshes. Keep it event-driven. Hooks own lifecycle boundaries; they never
auto-promote transcript content.

## Registration

Every process uses `memory_bootstrap(agent_id, agent_name)` before memory tools.
Low-level hosts may call `agent_register` and `memory_start` separately.

- IDs are 1–64 ASCII letters/digits/`.`/`_`/`-`, with alphanumeric ends.
- Names are whitespace-normalized, nonblank, and at most 128 characters.
- ID and name are each case-insensitively unique within the project tenant.
- The same normalized pair reconnects idempotently.
- Reusing either half with a different counterpart fails; never silently share identity.
- Do not generate an ID per task, request, session, or context compaction.

Claude Code initialization fixes the pair to `claude-code` / `Claude Code`.
Multi-agent hosts choose one stable pair per logical agent.

## Runtime lifecycle

Call `memory_contract` when integrating or debugging; it is the machine-readable
source of mode, ownership, scope, registration, tools, outcome, and compaction rules.

The Claude Code hooks automate:

1. `SessionStart`: build the repository platform, register Claude Code, call
   `memory_start`, and inject `rendered_context`.
2. `PreCompact`: record the compaction boundary and run due dreams without ingesting
   the raw transcript.
3. `PostCompact`: redact and store Claude Code's generated `compact_summary`, then run
   due dreams. Raw transcript text is never automatically ingested.
4. Claude Code fires `SessionStart(source=compact)` after compaction, reloading memory
   with a fresh task-run ID; its native compact summary covers the current boundary
   until the Memotron checkpoint is available to later starts.
5. `SessionEnd`: flush already-curated memory without copying the raw transcript.

Customize semantic timing and candidates in `.memotron.yaml` under
`agent_guidance`, then rerun `memotron init`. The initializer regenerates the
Memotron rule/skill, replaces only Memotron-owned hook handlers, and preserves
unrelated Claude settings and hooks. Do not edit the generated handlers to ingest
transcripts or publish memories automatically.

During work, Claude must:

1. Call `memory_search` before relying on prior requirements, decisions, incidents,
   preferences, or task state.
2. Keep `use_events` from start/search.
3. Use `memory_remember` only for an exact durable personal/agent fact.
4. Use `memory_publish` only for sourced evidence useful in a later project session.
5. Call `memory_refresh` after important publication batches when immediate formation
   is needed.
6. Report `memory_outcome` only after a named user, test, or workflow actually observes
   the result.

Never infer an outcome from rank, confidence, silence, or lack of complaint.

## Project memory

Project facts never bypass dreaming:

1. `.memotron.yaml` seeds a versioned project goal, memory goal, keep/exclude/rules,
   type gates, protected types, salience cap, and dedup threshold.
2. `memory_publish` stores attributed candidate evidence with agent, task run, source
   reference, and policy version.
3. `memory_refresh(include_project=true)` applies the project Motive and prompt.
4. Accepted current facts become shared; rejected evidence remains auditable.

Use `project_memory_config` to inspect policy and counts.
Use `project_memory_candidates` to review pending or processed candidates.
Do not publish secrets, scratch notes, transient command output, or unsourced guesses.

## Inspect, explain, and forget

- Status: `memory_contract`, `project_memory_config`, `memory_evolution`.
- Find: `memory_search` returns relationship IDs and immutable use receipts.
- Explain: `memory_explain` returns current status, confidence, valid time, source text,
  metadata, and source episodes for an exact relationship.
- Forget: search, show the exact fact/scope/evidence, obtain user confirmation, then
  call `memory_forget` with relationship ID, matching scope, and a specific reason.
  The agent tool refuses project-scope retirement; project correction/retirement is
  an operator-governed workflow.
- Review: `project_memory_candidates` plus `project_memory_config`.

`memory_forget` is a receipted soft retirement. Never mutate SQLite or raw episodes.
In Claude Code, invoke these workflows with
`/memotron-memory status|why|forget|review`; `/memory` is Claude Code's built-in
memory-file command.

## Outcome scope mapping

Pass the exact use ID and task-run ID. Map the use event scope:

- `kind=user` → `scope="personal"`
- `kind=agent` → `scope="agent"`
- `kind=tenant` → `scope="project"`

`memory_utility` is receipt-derived retention evidence, not truth or source authority.

## Hosted agents

When a central service is required, run:

```bash
uv run memotron-local-platform \
  --graph-path .memotron/local-platform.sqlite \
  --tenant-id project-id \
  --mode simple \
  --host 127.0.0.1 \
  --ui-port 8765 \
  --mcp-port 8010
```

`--mode multi-agent` selects explicit agent ownership. Consumers use the hosted MCP
endpoint or `MemotronPlatformClient`; they never open the service SQLite file.
The local launcher provides UI, HTTP API, streamable HTTP MCP, and maintenance.
Use `memory_bootstrap` or `POST /api/platform/memory/bootstrap` as the one-call
hosted startup path.

## Embedded Python SDK

Use the top-level package only:

```python
from memotron import Memotron, MemoryScope, ScopeKind

client = Memotron(graph_path=".memotron/memory.sqlite")
scope = MemoryScope(kind=ScopeKind.USER, scope_id="stable-user-id")
```

- `add_memory`: exact structured fact, immediate materialization.
- `add_episode` / `add_session` / `add_context`: immutable evidence queued for formation.
- `run_due_dreams`: apply due formation/consolidation/pruning jobs.
- `profile` / `search`: retrieve without mutating truth.
- `memory_evidence` / `truth_timeline`: explain current and historical truth.
- `correct_memory` / `forget_memory`: receipted correction or retirement.

Use timezone-aware UTC datetimes. Unknown Motives, unauthorized scopes, unregistered
agents, invalid identity collisions, and missing project prerequisites fail fast.
