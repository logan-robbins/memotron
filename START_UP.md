# START_UP — get Memotron dogfooding working, or find out why it isn't

**Read this first if memory did not load, if a hook errored at session start, or if you are a
fresh session with no context.** Written 2026-08-27 after the setup had to be redone several
times. Everything below was **observed** in this repository unless labelled otherwise.

This file is **committed on `takeover`** (`88d6cce`). That protects it from worktree deletion —
the failure that destroyed the graph on 2026-08-27 — but means it **disappears on
`git checkout spike/claude-code-plugin`**, exactly like the config in F1. If you are on another
branch and cannot find this file, that is why. See [Durability](#durability) at the end.

---

## 0. The 30-second correctness check

```bash
cd ~/repos/jedai/memotron          # NOT a subdirectory, NOT the worktree — see F5
git branch --show-current             # must print: takeover
memotron status --project-root "$PWD"
```

`memotron status` must **exit 0** and print JSON containing:

| field | expected |
|---|---|
| `graph_path` | `/Users/…/repos/jedai/memotron/.memotron/dogfood.sqlite` |
| `project.id` | `memotron-dogfood` |
| `mode` | `simple` |

```bash
memotron llm status --project-root "$PWD"   # must show llm_ready: true
```

If all of that holds, the setup is fine and any remaining problem is F6 (nothing forms) or a
plugin hook (F4), not configuration.

**Check exit codes without a pipe.** `cmd | head` makes `$?` report `head`'s status, not `cmd`'s.
That mistake has produced a false "EXIT=0" twice in this project's history.

---

## 1. Correct state, for comparison

| thing | value |
|---|---|
| repo | `/Users/ryan.van.valkenburg.-nd/repos/jedai/memotron` |
| branch | `takeover` |
| graph | `.memotron/dogfood.sqlite` (gitignored, ~508K when fresh) |
| project id | `memotron-dogfood` |
| binary | `memotron` on PATH → symlink into `~/.local/share/uv/tools/jedai-memotron/` |
| LLM | LiteLLM → `https://latest.jedai-gateway.wdprapps.disney.com/v1`, model `claude-haiku-4-5` |
| MCP server | `memotron_agent_memory`, 27 tools, 18 `memory_*` |

Config files that **must exist in the working tree** (all live on `takeover`, none on
`spike/claude-code-plugin`): `.memotron.yaml`, `.mcp.json`, `.claude/settings.json`,
`STATE.md`, `scripts/hooks/session-brief.sh`.

Plus one **untracked, gitignored** file: `.claude/settings.local.json` must contain

```json
"enabledMcpjsonServers": ["memotron_agent_memory"]
```

Without it the MCP server is not approved and will not connect.

---

## 2. Failure modes, in the order they actually happened

### F1 — no memory injected, no session brief, `status` says config is missing

**Symptom:** `memotron: .memotron.yaml is missing from …; run 'memotron init'`

**Cause:** wrong branch. `spike/claude-code-plugin` (and the `worktrees/memotron` checkout)
contain **none** of the Memotron config. This is the single most likely reason a restart
"didn't work".

**Fix:**
```bash
cd ~/repos/jedai/memotron && git checkout takeover
```

> **Do NOT run `memotron init` to fix this.** It infers project identity from git origin, which
> would yield `github.disney.com/jedai/memotron` — **not** the committed `memotron-dogfood`
> — and per the SDK skill it defaults `graph_path` to `~/.memotron/memory.sqlite`. That creates
> a *different project with different scopes* and orphans all existing memory. The committed
> `.memotron.yaml` is authoritative. *(Divergence observed; the init defaults are from the SDK
> skill doc, i.e. **derived**, not run here.)*

### F2 — every memotron command exits 2 demanding a credential

**Symptom:** `memotron: Memotron LLM configuration requires LITELLM_API_KEY; export it or
run 'memotron llm configure' with that variable set`

**Cause:** no sealed credential in the graph. Happens when the graph is new or was deleted.
Note `.memotron.yaml` declares `provider: litellm`, which makes the key a **hard requirement** —
there is no rule-based fallback in this configuration, contrary to the general SDK skill text.

**Fix — reseal. Never read the key into context or paste it into chat:**
```bash
cd ~/repos/jedai/memotron
SEC=~/repos/jedai/mcp-forge/.local-secrets
BASE=https://latest.jedai-gateway.wdprapps.disney.com/v1

# validate first, with a control arm — expect 200 then 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $(cat $SEC/litellm-latest-key)" "$BASE/models"
curl -s -o /dev/null -w '%{http_code}\n' "$BASE/models"

# seal it (one time; survives restarts via .memotron/dogfood.sqlite.kek)
LITELLM_API_KEY="$(cat $SEC/litellm-latest-key)" memotron llm configure \
  --project-root "$PWD" --provider litellm --base-url "$BASE" \
  --model claude-haiku-4-5 --api-key-env LITELLM_API_KEY
```

Then confirm it survives a **fresh process with no env var** — that is the real test:
```bash
memotron llm status --project-root "$PWD"   # llm_ready: true, stored_credentials non-null
```

`litellm-latest-key` is the right file because `latest` in the filename matches `latest` in the
gateway URL. **Do not guess among the other `litellm-*-key` files** — a wrong sealed credential is
durable (T1-26: `seed_tenant_llm_credentials_from_env` returns early when a row exists, so the
repair path never runs). Validate against the endpoint before sealing.

Do **not** solve this by creating a `.env` in the repo: T1-14 makes the test suite red in any
working directory containing one.

### F3 — the graph is empty and previous memory is gone

**Cause:** the graph is a gitignored file. On 2026-08-27 the worktree holding it
(`worktrees/memotron-implementation`) was deleted and the graph went with it —
**unrecoverable**, nothing of it was in version control.

**This is not a bug to fix.** Confirm and move on: `candidate_count: 0` and empty memory profiles
mean a fresh graph. Historical counts quoted in `TAKEOVER-BACKLOG.md` (T1-33) are attested by the
log, not re-checkable.

### F4 — `SessionStart:startup hook error` mentioning `run-hook.cmd`

**Symptom:**
```
Failed with non-blocking status code: /bin/sh: ${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd: No such file or directory
```

**This is NOT Memotron.** It is the `superpowers` plugin. Its `hooks/hooks.json` wraps the
variable in **single quotes**, which stop `/bin/sh` from expanding it. Proof: the identical
command unquoted, and the absolute path directly, both exit 0 with 9427 bytes of output.

**Fix** — in
`~/.claude/plugins/cache/superpowers-marketplace/superpowers/<hash>/hooks/hooks.json`:

```diff
- "command": "'${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd' session-start",
+ "command": "\"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd\" session-start",
```

Double quotes, not no quotes — unquoted breaks on paths containing spaces (measured). **This is a
stopgap**: the cache directory is version-hashed, so a plugin update reinstates the bug. It is
non-blocking; only superpowers' skill-reminder injection is lost.

### F5 — memory seems to vanish, or two graphs exist (T1-35)

**Cause:** `graph_path` is resolved against the **process working directory**, not the project
root. Started from `src/`, config discovery still succeeds and reports the correct `project.id`,
but the graph becomes `src/.memotron/dogfood.sqlite` — a separate store, **exit 0, empty
stderr**, and invisible to `git status` because `.gitignore:3` ignores `.memotron/` at any
depth.

**Rule: always launch Claude Code and every `memotron` command from the repo root.**

**Check for strays:**
```bash
find ~/repos/jedai/memotron -type d -name .memotron -not -path "*/.git/*"
# expect exactly one line: .../memotron/.memotron
```

### F6 — everything is configured, hooks exit 0, but no memory ever forms

**Cause: T1-1, and it is live in the installed binary.** Verified by reading it:
`dreaming.py:1959` in `~/.local/share/uv/tools/jedai-memotron/.../memotron/dreaming.py` is
`context_facts=tuple(memory.fact for memory in graph_context)` — the **unfixed** form. The
validated fix (`context_facts=()`) is not applied on `takeover` either.

Once a scope records **one** unresolved-problem fact, the dream agent reads the negative content
as grounds to refuse and formation stops for that scope. Silent, billed per refusal, no receipt.
This repository's memory content is almost entirely defects, so expect it.

**It is a liveness failure, not data loss** — blocked episodes are never marked processed, so they
stay queued and form once the offending fact clears.

**Do not diagnose this as a setup failure.** Empty profiles *after* a successful hook run point
here, not at configuration.

---

## 3. Drive the hooks by hand

Restarting Claude Code is what wires the hooks in. To test them without restarting:

```bash
cd ~/repos/jedai/memotron
printf '{"session_id":"manual","source":"startup","cwd":"%s","hook_event_name":"SessionStart"}' "$PWD" \
  | memotron hook session-start --project-root "$PWD"
echo "EXIT=$?"      # 0, and it should print the memory profiles

CLAUDE_PROJECT_DIR="$PWD" bash scripts/hooks/session-brief.sh; echo "EXIT=$?"   # 0, ~751 bytes
```

To confirm the MCP server independently of Claude Code, drive it over stdio (`initialize`,
`notifications/initialized`, `tools/list`) and expect **27 tools / 18 `memory_*`** including
`memory_publish`, `memory_remember`, `memory_search`. **A timeout is not a measurement** — make
the probe exit non-zero if the handshake never completes, or "0 tools" will be reported by a run
that never reached its subject.

---

## 4. Things that are true and easy to get wrong

- **The installed binary is `origin/main`, not this branch's `src/`.** It is a frozen
  `uv tool install` copy, so `29624cb`'s three Tier-0 fixes are **not** running. Session 9's
  judgement that this does not affect SQLite dogfooding is **derived** — it holds because all
  three fixes are Postgres/storage-config specific.
- **Hooks capture lifecycle boundaries only.** Raw transcript is never auto-ingested, by design.
  Durable project facts require deliberate `memory_publish` / `memory_remember` calls. A full day
  of hook-only operation put **0 rows** in the project scope (T1-33).
- **Injected memory is evidence, not a task list.** Check the item's current row in
  `TAKEOVER-BACKLOG.md` before acting on any remembered directive; nothing retires completed ones.
- **Never read `.local-secrets/*`, `creds.md`, or `local-creds.md` into context.** Use them via
  `$(cat …)` inside a single command, as F2 does.

---

## 5. Where the real work queue lives

`STATE.md` → **"## Next session — start here"**. That section is authoritative and ordered; the
`**Next:**` lines further down are historical. Current head of queue: **T1-14** (cwd-relative
`load_env_file()` at `runtime.py:177,232,320,385`) — the same bug class as F5.

Also open: a **Dependabot high on `main`** (alert 18), untriaged.

---

## Durability

The graph, the sealed credential, `.env`, and `.claude/settings.local.json` are **all gitignored**.
They survive a branch switch; they do **not** survive deleting the checkout. That is exactly how
two days of dogfood evidence was lost. If something must outlive this working copy, commit it or
copy it out — `~/repos/brain` is the usual home.

**This file is tracked** (`88d6cce` on `takeover`), so `git status` stays clean and worktree
deletion cannot take it. The cost is the F1 failure mode: it is absent on
`spike/claude-code-plugin` and in `worktrees/memotron`, which are precisely the checkouts where
someone confused enough to need it is most likely to be standing. A copy that survives both lives
in the vault at `~/repos/brain/setup/memotron-dogfood-recovery.md`.
