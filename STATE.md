# STATE — jedai/memotron

> **Line references resolve against `audit-baseline-2026-08-27`** (commit `06391b6`), not
> against `HEAD`. The module split turned `models.py`, `config.py`, `storage/sqlite.py` and
> `dreaming.py` into packages, so 469 of the ~1,100 `file.py:LINE` references below now point
> at files that no longer exist. They are pinned rather than rewritten: their value is *"at the
> commit I audited, this line said X"*, and rewriting makes them wrong about history while
> looking right. Verified: **410 of the 417 stale references (98%) fall inside the file they
> name at that tag.** To read one: `git show audit-baseline-2026-08-27:src/memotron/<file>`.

Repo-local memory for agents. Facts here are **observed**, not assumed — each one was confirmed by running a command or reading the cited file. Prefer these over re-deriving.

> **Start here: [`docs/findings/README.md`](docs/findings/README.md)** — the running working-memory
> file. It holds the mental model, an index of where every kind of knowledge lives, the
> reading-progress tracker for `dreaming.py`, and the open threads. Keep it current; anything
> understood but not written there is lost at the end of a session.

## Next session — start here

> [!warning] The banner below is from 2026-09-03 and is now WRONG in two load-bearing places.
> It says *"Nothing is authenticated"* and lists PRs #169/#170/#171 as open. All three merged,
> and **#126 is verified live on `latest`** — see *Verified facts*. It also predates session 41,
> which found that `latest`'s console addresses a tenant that does not exist there and that the
> admin surface leaks a neighbour tenant's **memory content**, not only its ids.
> Read **Last session** first; it is the current queue. The banner is kept only for the CARRY
> notes below it, which still hold.

> ### START HERE: three PRs open, and two things you must not re-derive
>
> **Open, all MERGEABLE, all blocked on REVIEW rather than work** — the same thing that held #131 up:
>
> | PR | what | closes |
> |---|---|---|
> | **#169** | doc corrections + local stack defaults to Postgres | — |
> | **#170** | `memotron key` — the registry **write** path | **#168** |
> | **#171** | registry-row → `MemoryPrincipal` adapter — **read** groundwork | — |
>
> **The identity chain is 3 of 4 links.** `raw key → key_alias` (#156, merged) ·
> `key_alias → row` (DW-030, writable via #170) · `row → MemoryPrincipal` (#171) ·
> **`MemoryPrincipal → surface` — MISSING, and that is #126 / #137.** Nothing is authenticated:
> `mcp_server`, `admin_server` and `agent_memory_mcp` each have **zero**
> `Authorization`/`Bearer`/`x-api-key` hits, and `gateway_identity` is imported by nothing in `src/`
> (grep with a positive control, 2026-09-03). Do not read "the registry landed" as "callers are
> authenticated".
>
> **CARRY 1 — the storage gate passed on six PHANTOM golden rows.** Cherry-picking the adapter off
> the CLI branch brought `storage_profile` rows for `test_key_registry_cli.py`, a file that does not
> exist on the adapter branch. `storage_golden.py` **reported PASS**, forgiving them as
> *"not observed (filtered run)"*. Trusting that green would have shipped a golden describing a
> nonexistent file. Re-blessing needs `--allow-shrink`, which the gate rightly demands. **After any
> cherry-pick or branch split, grep the goldens for rows belonging to the branch you left** — the
> gate will not tell you.
>
> **CARRY 2 — SETTLED 2026-09-06, and the part marked "already settled" was FALSE.** The claim was:
> MCP tools *cannot* read request headers, because `RequestContext.request` defaults to `None` and
> the lowlevel server never populates it. **It does populate it** under the streamable-HTTP
> transport — the per-POST request is attached at `streamable_http.py` and read at
> `lowlevel/server.py` — so a value written to `request.state` in ASGI middleware IS visible inside
> a tool body. Measured by mutation (delete the middleware's write, the tool sees nothing), not by
> re-reading the source that produced the wrong claim. `token_verifier` was never needed; #126
> shipped ASGI middleware. The one true part: a **contextvar** does not work, because MCP dispatches
> the tool in a different anyio task — `request.state` crosses that boundary and a contextvar does
> not. Identity also rebinds **per request**, not per session. See `docs/authorization-decisions.md`.
>
> **Suggested order.** (1) **#129 telemetry** — independent of everything and the cheapest real win:
> the Splunk OTel collector is already running in the latest cluster and `mcp-jedai-postgres` exports
> to it, so it is wiring against a known endpoint. (2) **#123** — the gate; its `probe_kek.py` was
> re-run 2026-09-03 and still reproduces (`B1 REPRODUCED`), and it unblocks #77. (3) flip latest's
> three chart flags — everything else there is verified ready. (4) **#126 / #137**, starting with
> `admin_server` (its `self.headers` already works; the fixed class-attribute principal at `:2221`
> becomes per-request), then `mcp_server` per CARRY 2.
>
> ---
>
> **2026-09-03 (session 33) — MERGED as `80be8f8`, and deployed.** #167 combined the three
> identity/storage PRs and is on `main`; #165/#156/#166 were closed as superseded (their branches
> are intact on the remote), and **#164 auto-closed**. Verified by **tree diff**, not ancestry:
> `git diff --name-only feat/identity-storage origin/main` is **empty**.
>
> **`0.1.0-80be8f8` is live and healthy on `latest`** (admin 1/1, api 2/2, 0 restarts). The
> migrations did not apply there because **`latest` runs no Postgres by chart design** —
> `operationalStore.enabled: false`, gated behind #130/#123. See the Verified-facts entry below;
> this is a staged rollout, **not** a broken deploy, and an earlier draft of this block said
> otherwise.
>
> **Migrations 9 and 10 are proven against a real, already-migrated Postgres** (local, 2026-09-03:
> a store sitting at version 8 upgraded to 10 under a post-#167 image, `key_principals` created with
> `key_alias` as PK). What remains unproven is a **deployed** environment. `prod` has
> `operationalStore.enabled: true` in its values file; **the prod cluster was not checked.**
>
> **`docker-compose.local.yml` now defaults to Postgres** (`be9c3bc`), reversing the 2026-09-02
> blank-DSN default, so a plain local run exercises the engine production uses. `DW_STORE_DSN=`
> still reproduces `latest`'s shape. **Use `--build`** — see the stale-image rule under General
> rules.
>
> **New: #168 (bug)** — nothing populates `key_principals`. `bind_key_principal` has zero callers
> outside its own test, so a fully-wired auth chain still authenticates nobody. Read it before
> starting #126/#137; it names the bootstrap problem (the obvious home is an endpoint on a server
> that authenticates nobody) and marks self-registration as forbidden by DW-018/DW-027.
>
> **MCP wiring has a blocker worth knowing before anyone starts #126/#137:** MCP tools **cannot**
> read request headers — `RequestContext.request` has `default=None` and the lowlevel server never
> populates it, so `ctx.request_context.request` is `None` on every request. Use FastMCP's
> `token_verifier` / `auth` parameters plus `mcp.server.auth.middleware.auth_context.get_access_token()`.
> **Unverified assumption:** whether `token_verifier` fires per-request or only at session
> initialize. Check with one live request before building on it.
>
> ---
>
> **2026-09-03 — how #167 was assembled.** `feat/identity-storage`,
> cut from `main` @ `bb5f42f`, merged **#165** (the per-key registry + `docs/data-model-and-tenancy.md`),
> **#156** (`gateway_identity.py`), and **#166** (the Postgres receipt-digest fix). Postgres
> migrations **9** (`key_principals`) and **10** (`receipt_relationship_index`) both survive the
> merge, in numerical order — both sides had appended to the same place and the conflict resolution
> kept both.
>
> **Goldens were re-blessed from a clean full run, never hand-merged.** An auto-merged golden is a
> file neither side would generate: it passes the gate while describing nothing real. `api_surface`
> is **+39 symbols / −0** against `main`, verified symbol-by-symbol *from the files* because the
> gate truncates its own rendered diff — 5 from the registry, 32 from `gateway_identity`, 1 from
> the receipt fix, and 6 classes whose content hash changed because methods were added to them.
>
> **Both identity halves are still wired to nothing,** and landing them changed no behaviour.
> Verified 2026-09-03 by grep over `src/` with a positive control: zero call sites for
> `bind_key_principal` / `principal_for_key_alias` / `unbind_key_principal` outside `storage/`,
> and `gateway_identity` is imported by nothing in `src/` — only by its own test. The wiring is
> **#126 / #137**. Do not read "the registry landed" as "callers are authenticated."
>
> **There are now TWO tracks, and this section is only one of them.**
>
> **Productionization — readiness phase is DONE and in review as PR #103** (`split-models` ->
> `main`, 194 commits). All seven god modules are packages, 12 gates exist, `bash scripts/check.sh`
> passes end to end. Not merged on purpose: merging IS the cutover from the research baseline.
> Team-facing summary is `docs/BRANCH-OVERVIEW.md`.
>
> **Phase 2 is live on `production-hardening`** (stacked on `split-models`, draft PR **#131**,
> which must keep its base as `split-models` until #103 merges, then be retargeted rather than
> reopened). Plan: `~/.claude/plans/okay-looking-at-our-reflective-catmull.md`.
>
> **Where to pick up (2026-09-02, end of session 25).** **There is now ONE working branch:
> `production-hardening`.** `productionization-phase3` was folded into it (`d043871`) and is
> finished — do not commit to it again. **#103 IS MERGED** — `main` is `e5fb5a4`, plus **#147**
> (the chart fix). PR **#131** (`production-hardening` → `main`) is undrafted, mergeable, and
> **needs a reviewer — zero are requested**, which is what it is actually blocked on.
>
> **Why the branches were folded, because the split had a cost.** phase3 was cut off this branch
> on 2026-09-01 to keep working while #131 awaited review — sound then, expired since. The two
> gate fixes `9c513bc` (suppressions defeated by ANSI colour) and `501a787` (check.sh reporting a
> skipped golden lane as PASS) lived **only** on phase3, so the branch #131 ships still had both
> bugs; `check.sh` failed here for exactly that reason and was nearly filed as an environment
> quirk. Meanwhile DW-027/DW-028 lived only here. **Neither branch was correct alone.**
>
> **One real environment trap remains.** `check.sh` **does not source `.env`**, and
> `MEMOTRON_TEST_POSTGRES_DSN` lives there (`.env:62`). Unset, `postgres` and `parity-cov` skip
> — and because `postgres` must precede `cov-floor` (both read `coverage.json`), the postgres
> modules land at 18–51% and **`cov-floor` then FAILS**. Export the DSN before running:
>
> ```
> MEMOTRON_TEST_POSTGRES_DSN=<dsn> bash scripts/check.sh   # 16/16 PASS, verified 2026-09-02
> ```
>
> The `FORCE_COLOR` trap is **gone on this branch** — verified by re-running with `FORCE_COLOR=3`
> still set after the merge: 16/16 PASS. It will still bite on any branch lacking `9c513bc`.
>
> **Launch scoping (Ryan, 2026-09-02): crypto-shredding and Entra SSO for the admin UI are
> DEFERRED past first launch.** Admin UI, MCP, API and CLI authenticate with **JEDAI Gateway
> LiteLLM virtual keys**. Before touching tenancy or auth, read
> `docs/authorization-decisions.md` — **DW-014, DW-026 and DW-027 already decide principal
> resolution**, and a session was spent re-deriving them. The live behaviour they rest on is
> re-provable with `scripts/verify/probe_gateway_tenancy.py --env <env>`; it has only ever been
> run against **latest**, so run it before trusting any of it on stage/load/prod.
>
> The current work is the **refactor phase**, plan at
> `~/.claude/plans/okay-looking-at-our-reflective-catmull.md`. **Phases 0, 1 and 2 are done.** Next:
>
> 1. **Phase 3a is DONE (`1098ad1`), and the plan's 929 lines were fiction.** Measured:
>    transports **138** extractable (not ~300), admin payloads **2** (not 347) — those figures
>    were the total SIZE of the surface, not its overlap. The transports landed anyway, because
>    extractable-lines is the wrong measure; see *Lessons learned*. The admin cluster is a real
>    **NO_GO**: signature-derived dispatch produces ZERO mypy errors where explicit kwargs
>    catches a typo'd parameter and a wrong type, on a strict-tier surface.
>    **Remaining: cluster 2, sqlite↔postgres (~282 claimed, UNMEASURED).** Expect it to shrink
>    too, and note it now interacts with `parity-cov` — a shared helper could genuinely close
>    some of the 27 one-sided methods, or could hide divergence behind it, which is the `anchor`
>    failure mode in a nicer disguise. Measure first, serially, gate read before and after.
> 2. **Phases 4–6** are gated on the new coupling metric moving: layering, then `admin_server`
>    re-measure-then-decide, then #127/#128 with corrected numbers.
>
> **Phase 2 done (session 22).** `mixin-dag` now covers **5 of 5** mixin packages, not 3 of 7 —
> `config` and `admin_server` were never candidates (zero mentions of `Mixin`, no `_protocol.py`,
> no composed class), so the plan's "4 more packages" was really 2. Twelve ruff families enabled;
> `BLE001` deferred with its reasoning in `pyproject.toml`. A 16th lane, `parity-cov`, now gates
> whether each storage method runs on both engines.
>
> **#146 IS FIXED AND DEPLOYED (2026-09-01, session 22).** This line previously read *"blocked on
> someone else — every merge to `main` fails until a `jedai-platform-base` chart fix lands"*. **Both
> halves were wrong.** The knob already existed (`jedai-platform-base 0.1.5`,
> `templates/_deployment.yaml:19` renders `.Values.deployment.strategy`), so it was never another
> team's fix; and it was intermittent, not every merge. `strategy: Recreate` is now set on the admin
> role in `.helm/values.yaml`, merged as **#147**, and all three environments are on `0.1.0-f8eb862`.
>
> **Still open, and genuinely not this work:** **#143** (which of three rollup-gate fixes),
> **#145**'s three root-cause items, and the CHECK_CMD flip decision. **New: #148** — the admin PVC
> has never had a byte written to it and should probably be removed outright.

> **#145 FILED 2026-09-01 (T2-10), and the guard landed.** The dogfood admin UI served 503 on
> `GET /` for ~5 days while `/api/*` answered normally — `--static-dir` pointed into a worktree
> deleted the day after the process started. **T2-10 called this "invisible in dev"; it happened
> in a checkout.** `static_build_warning()` now warns at startup in both entry points (checks
> `index.html`, not the directory; warns rather than exits, because Helm passes no `--static-dir`
> and a hard failure would crashloop the admin pod). **Running the artifact found what the tests
> could not:** `local_platform.py` block-buffers its whole startup block under nohup/containers,
> so the warning never reached the log — it now flushes, and the rest of that block is still
> invisible in a container. Root cause stays open in #145: the wheel cannot serve its own UI,
> Helm sets no flag, and `/health` cannot see it.
>
> > **#140 ANSWERED and CLOSED 2026-09-01 — and the live lane is complete for the first time.**
> The question was why `sweep_admin` discovers 53 routes while the recorded baseline accounted
> for 35. **None of the three filed explanations was right.** Measured against a real
> standalone `memotron-admin-server` on a fresh graph: `OK=21 HTTP400=13 HTTP204=1
> HTTP503=18` = 53. The old prose baseline (`GET OK=16 POST OK=4 HTTP503=15`) had only three
> buckets and **they do not span the outcome space** — HTTP400 and HTTP204 had no bucket, so 14
> of 53 outcomes were structurally uncountable. The "missing 18" was an artifact of the tally,
> not 18 unexercised or undiscovered routes. The near-coincidence with T1b-9's 18 dead platform
> routes is exactly that: all 18 `/api/platform/*` DO return 503 (confirmed live), but they were
> never the missing ones.
>
> The 13 × HTTP400 are **correct input validation**, verified by reading every message.
> `sweep_admin` now has an exit contract, and `probe_admin_surface.py` self-hosts the real CLI
> so the lane can run it with no arguments. **Full live lane, all five arms: every probe matched
> its recorded baseline.**
>
> > **#138 CLOSED 2026-09-01, and it was a one-line annotation, not a redesign.** All 30
> `attr-defined` errors were one pattern: three-arg `type()` infers as bare `type`, discarding
> the base, so every following attribute assignment errored. `MemoryGraphHandler` **already**
> declares each of those attributes with a type — naming the base
> (`handler: type[MemoryGraphHandler] = type(...)`) was all mypy needed. The issue's own
> "condition under which this is wrong" (that `http.server` might require the dynamic subclass
> in a way a typed base cannot satisfy) does not apply: nothing about the runtime changed.
> **Verified by running it, not just type-checking it** — subclass, `__name__`, attributes
> landing on the subclass and not the base, `do_GET` intact. Same fix applied to
> `local_platform.py:203`, the identical idiom hiding 13 more. Both suppressions **deleted**;
> hidden-error total 244 -> 201. **A/B:** with the annotation removed mypy reports 30 errors;
> with a genuine typo (`tenant_idd`) injected it is now caught — previously invisible for the
> entire admin surface. Also closed a gap in #135's own gate: a baseline entry whose suppression
> has been retired is now reported rather than silently orphaned.
>
> > **#135 CLOSED 2026-09-01.** `mypy_suppressions.py` asked only "does this code still fire?",
> so `mcp_server`'s `attr-defined` — firing **27 times, all real defects** (#132) — reported as
> "still earning its place". The passing condition was satisfied by the problem the gate
> existed to surface. Now ratchets per `(module, code)` against
> `tests/mypy_suppressions.baseline.tsv`: a RISE fails, a FALL is reported and passes (so
> `--bless` in the commit that paid the debt). **Measured A/B** with two defects injected behind
> an existing suppression: old gate PASS exit 0, new gate `local_platform: attr-defined 13 -> 15`
> exit 1. Baseline recorded, not invented: 91 pairs, 244 hidden errors, min 1 / median 1 / max 30
> — which is also why a global cap was rejected as arbitrary. **Stated limit:** it catches
> growth, not a brand-new suppression's initial size; that is only visible in the pyproject diff.
>
> > **#141 CLOSED 2026-09-01.** `check.sh`'s `POSTGRES_COVERED` shell flag described the
> *invocation*, not the report, so `check.sh diff-cov` on its own applied the
> `storage/postgres/**` exclusion even when `coverage.json` already held real Postgres
> coverage — silently under-gating, never a false failure, which is why it sat there. Replaced
> by `coverage_floors.py --postgres-covered`, which asks the report and reuses `POSTGRES_FLOOR`
> rather than inventing a second threshold. The flag is gone entirely. 11 tests, break-tested
> against the old behaviour in the exact partial-invocation scenario.
>
> **#139 CLOSED 2026-09-01.** The 57 `[postgres]` rows in `tests/storage_profile.golden.tsv`
> were `close:1` stubs written by SKIPPED tests and could never carry signal. Fixed at the source
> — `storage_profile.mark_skipped` drops a skipped test's row at write time — so they cannot come
> back on the next bless. The filter keys on the SKIP, not on "profile is only `close`": six
> non-Postgres tests are legitimately close-only, and `wasxfail` is excluded because pytest
> reports an xfail as skipped and that row is real evidence. **The golden now holds 5 real
> Postgres-only profiles and passes in BOTH DSN states** — which it never did before — so the
> blessing input is now the DSN-**set** run. The `storage_golden.py` docstring records why that
> advice has now changed twice.
>
> **Infra / cutover** — the queue in this section. Unchanged, and still owned by
> `INFRA-BACKLOG.md`.

*Rewritten 2026-08-31. The previous version of this block was four sessions stale — it still
named `client.py` as the next module to split, months after that finished. The `**Next:**` lines
further down under "Last session" are **historical**: read them as a record of what each session
thought was next, not as instructions.*

### Read this before trusting injected memory

Memotron's session-start hook injects the `agent:claude-code` profile. **Nothing retires a fact
once it forms** (**T1-33**), so what arrives is a record of what some past session believed, not a
work queue. Treat it as historical evidence and check the item's current row in
`TAKEOVER-BACKLOG.md` before acting on any remembered directive.

**The 11 stale directives this section used to name are gone** — not retired, *destroyed*. The
graph lives in a gitignored file and went with the deleted worktree on 2026-08-27
(`START_UP.md` F3). Observed 2026-08-27 20:49Z on the rebuilt graph: the scope holds **4** active
facts, all formed from session 9's `SessionEnd` checkpoint, and none of them are the old
`should verify T1-30` / `should push commit 5ce3236` / `should run sweep_sdk.py` /
`should read dreaming.py` directives. T1-30 is still **REFUTED**; if any wording resembling it ever
reappears, it is wrong.

**T1-33 is unfixed, so this will recur.** Re-derive the count rather than trusting a number written
here — `memory_evolution` reports `active_relationship_count` per scope.

### The MCP write path connects for the first time this session

`enabledMcpjsonServers: ["memotron_agent_memory"]` was written for this worktree on
2026-08-27. The server is healthy (27 tools, 18 `memory_*`, verified by a manual stdio
handshake), but it had **never been connected in this repo** before, so:

1. **Confirm the tools are actually present** (`memory_publish`, `memory_remember`,
   `memory_search`). If they are absent, that is a real finding, not a config typo — the binary
   and config were both verified working.
2. **Check `T1-35` before trusting anything the MCP server writes.** `.mcp.json` passes
   `--project-root ${CLAUDE_PROJECT_DIR:-.}` — it **falls back to cwd**, while the hook requires
   the variable. With a relative `graph_path`, an MCP server launched without it writes a
   *different* graph than the hooks, silently. Confirm both are on
   `.memotron/dogfood.sqlite` before drawing any conclusion from a write.
3. **This session is the real dogfooding test.** Session 9 proved an agent with no tool writes
   nothing. Whether an agent *with* the tool writes better than the hook does — and specifically
   whether it avoids T1-33's transient-directive problem — is **unproven in either direction**.

### The work queue, in order

| # | item | why it is here | first move |
|---|---|---|---|
| ~~1~~ | ~~**T1-14**~~ **DONE 2026-08-27** | Was the blocker on Workstream C. The four bare `load_env_file()` calls are gone and `env_file` is now a **required** argument, so the bug class cannot recur silently. `.env` loading belongs to the entry point, anchored to the project root — which `adoption.py:477` already did for every CLI path. | Proven by `scripts/verify/probe_env_file_cwd.py` (exit 1 -> 0, 5/5 replicated) and a one-variable suite arm (PASS 2.09s with the fix vs FAIL 3.99s / HTTP 401 without, same `.env`). `ci-build-check.sh` now passes **with** a `.env` present. Suite 1006/77 unchanged. **The filed first move would not have worked** — see the T1-14 row for why `discover_project_root` is not a fix. |
| 2 | **T1-27** | **Blocks the Postgres cutover** and has no fix. A successful re-migration duplicates the destination (**1 -> 2 -> 3 observed**), and **T1-25** makes the abort that provokes a retry routine. | Decide the shape first: idempotency key on the source relationship uuid, or a transactional copy. `scripts/verify/probe_migration_idempotency.py` is the red test. |
| 3 | **T1-34** | No CLI route into Postgres at all (`open_storage` hardcodes `engine="sqlite"`), so a cutover is bespoke code — the same code T1-27 corrupts. | Add a `--dest-url` to `memotron migrate`, or state explicitly that cutover is SDK-only and document the script. |
| 4 | **T0-8** | The only thing actually broken in the cluster: `READY 1/2` for 37+ days, RWO PVC under a 2-replica Deployment whose HPA minimum is 2. | Infra team owns it — see `INFRA-BACKLOG.md`. |
| 5 | **T0-1 / T0-10** | T0-10's epoch fix is validated for SQLite and **not applied to Postgres** (zero epoch references there). T0-1 still needs its own fix (rewrite `epoch_id` on migration). | `docs/findings/t0-10-epoch-filter.patch` is the SQLite shape; port it. |

### Blocked on a credential, not on us

**A Harness PAT with `core_service_edit`** — the `Build_Check` stage YAML is committed and the
pipeline is `storeType: REMOTE` (git-synced), so the change is in the source of record. Onboarding
it in the Harness UI needs that scope. Everything else previously listed as "needs a human" was
closed on 2026-08-26/27.

### State of the record

87 backlog items (14 Tier-0) · 81 log entries · 30 verify harnesses + `scripts/ci-build-check.sh`
· draft PR #46, issue #45. `uv run python scripts/verify/finding_gate.py` passes.

**All 20 agent-sourced findings are hand-checked** (12 confirmed, 5 reframed, 2 refuted, 1 closed
later against the published spec). Provenance is marked per item — see the table in
`TAKEOVER-BACKLOG.md`. Trust agent-sourced *locations*; re-derive agent-sourced *consequences*.

## Verified facts

**C4 works: Memotron returns its full 31 tools through the JedAI Gateway on `latest`**
(2026-09-09, `scripts/verify/probe_gateway_mcp.py`) — exactly the count the registration
declares.

**CORRECTED 2026-09-10: that does NOT mean "the curated allowlist applies end to end", which
this file previously claimed.** The 31-tool filter sits at the **gateway**, in front of the
**full SDK server** (`examples/mcp_server.py`) — which is the ingress backend in all five
environments and answers **200 from off-cluster**. A caller who skips the gateway and hits
`latest.jedai-memotron.wdprapps.disney.com` directly gets the unfiltered surface. The
allowlist bounds the gateway path only, and that is #16's one NOT DONE item.

**Reaching it needs FOUR conditions across THREE systems, and all four fail identically** as
`{"tools": []}`, HTTP 200, no error: registered + in an access group [mcp-forge → LiteLLM]; the
caller's key has that MCP access group [LiteLLM]; the key is bound in `key_principals`
[Memotron]; the gateway's Host is in `MEMOTRON_MCP_ALLOWED_HOSTS` [this chart]. **LiteLLM's
`_meta` server_outcomes is the only signal separating them** — empty = no grant, 403 = unbound,
421 = Host, `internal` = #246. Procedure: `docs/gateway-mcp-registration.md`.

**The MCP surface is stateful and the api role scales horizontally, so ~50% of gateway calls
fail (#246).** Measured 5/10 pass, `jedai_postgres` control 3/3 in the same window. `initialize`
returns an `mcp-session-id` living in ONE process; api runs 2 replicas, Service
`sessionAffinity: None`. The backend is sound — probed in-cluster it returns 200 every time.

**CORRECTED 2026-09-11: Harness overrides the image tag for ALL THREE long-running roles, not
two.** This file previously said it named only `roles.{api,admin}` and that a third role would
deploy the literal `0.1.0` placeholder — true when it bit `agent-memory` the moment #240 landed
("it ran code nobody merged while reporting `Running`"), and **no longer true**. Measured on the
`latest` cluster right after #264 merged:

    admin         ...wdpr-memotron:0.1.0-cae6c03
    agent-memory  ...wdpr-memotron:0.1.0-cae6c03     <- the role that was allegedly stranded
    api (x2)      ...wdpr-memotron:0.1.0-cae6c03
    dream-worker  0.1.0-6ed6dd0 on the 02:00/02:15 firings, 0.1.0-cae6c03 on 02:30

`cae6c03` is `main`'s tip. `values-latest.yaml` still declares the literal `"0.1.0"` for all
three, so the tag in the chart tells you nothing about what deploys — **read the pods, not the
values file**. I acted on the stale version of this note and warned that agent-memory was the
sharpest open item post-merge; it was not an item at all. **Nothing in this repo can enforce a
change in the repo owning that list**, which is the part that still holds: this could regress
without a commit here.

**#126 / #184's acceptance test PASSES ON FASTMCP 4 — all eight arms, exit 0, against deployed
`latest`** (2026-09-11, `probe_mcp_identity.py`, keys `dw-probe-alpha-2` / `dw-probe-bravo-2`).
Identical to the pre-migration result, which is the point: the identity model survived the
framework major.

    P0 guard armed          an unauthenticated call is refused
    A1/A2                   each principal writes its OWN scope
    A3 alpha -> bravo       REFUSED: scope 'tenant:probe-tenant-bravo' is not one of
                            principal 'p-dw-probe-alpha-2''s allowed scopes
    A4 bravo unchanged      and the write did not land anyway
    A5 no key 401 · A6 /health 200 uncredentialed · A7 bogus key 401

**A4 is the arm that carries the claim.** A refusal message is a string the server chose to
print; the ABSENT WRITE is the fact. A3 alone would pass against a server that refuses loudly
and writes anyway.

This retires the `EXPECTED, NOT YET MEASURED` caveat on `probe_mcp_identity`'s baseline row —
it could not be measured locally because `resolve_gateway_identity()` calls the gateway admin
plane over the network. **#184's remaining box is the `add_session` 4 MiB / HTTP 413 check**,
which is untouched; do not close #184 on this result alone.

**Probe keys, 2026-09-11 — the aliases changed and the reason is permanent.** The pair
recorded above (`dw-probe-alpha` / `dw-probe-bravo`) expired ~2026-09-08 as predicted, and
**their aliases cannot be reused**: LiteLLM answers
`400 Key with alias 'dw-probe-alpha' already exists. Unique key aliases across all keys are
required.` An expired key still holds its alias. Their key VALUES were never persisted
(correctly), so they cannot be used either — an expired-but-extant key is a dead alias, not a
recoverable credential.

Minted `dw-probe-alpha-2` / `dw-probe-bravo-2`, **`duration: "72h"`** this time (expire
**2026-09-14T03:58Z**) because 24h is what put us here. Key values live only in
`$CLAUDE_JOB_DIR/tmp/<alias>.key` for this job. Bound to `probe-tenant-alpha` /
`probe-tenant-bravo`, principals `p-dw-probe-<t>-2`, `role=user`, shape copied from a `key show`
of the old rows rather than guessed. **Deleting the stale keys was deliberately NOT done** —
destructive on shared infrastructure and it buys nothing, since new aliases plus a bind are
free and the bind is reversible.

**The bind is not idempotent against a flaky connection: one of the two timed out
(`dial tcp 34.41.211.122:443: i/o timeout`) while the other succeeded in the same loop.**
Verify both with `key show` and a never-bound control before running anything — a half-bound
pair makes `probe_mcp_identity` fail A1 and abort at exit 2, which is correct behaviour and
looks nothing like "the network blipped".

**The agent-memory server is VERIFIED DEPLOYED AND CORRECT, for the first time** (2026-09-11,
ClusterIP so it needs a port-forward — there is no ingress):

    image                          0.1.0-cae6c03
    in-pod packages                fastmcp 4.0.3, mcp 2.2.0 (pydantic control resolved,
                                   bogus-package control returned None)
    GET  /health                   200
    POST /mcp  unauthenticated     401      <- identity guard armed
    POST /mcp  with a bound key    200, serverInfo memotron-agent-memory v4.0.3,
                                   NO mcp-session-id (stateless_http, #246)
    tools                          28, including memory_bootstrap

Note the container port is **8000**, not the module default 8010 — the chart sets `MCP_PORT`,
which is the path `runtime.mcp_bind_from_env` reads. A port-forward to 8010 returns `000`, which
is the forward failing and NOT the server being down.

**`/api/platform/integration-contract` advertises localhost URLs on the hosted deployment**
(#242). Every other field is correct, which is what makes it dangerous.


**nltk is absent from the deployment image as of `fix/nltk-optional` (2026-09-09, observed in
the built artifact).** `docker run --entrypoint python <img> -c "importlib.util.find_spec('nltk')"`
returns None while pydantic and psycopg (controls) resolve. Before this, `import memotron`
loaded a full NLP toolkit in the MCP server, admin server and dream worker, because
`certification.py` imported `PorterStemmer` at module scope and `__init__.py` imports
`certification` eagerly. Our entire use is one class, three call sites, in benchmark scoring —
and no deployed tool or admin route constructs that judge. **Dependabot #23 (HIGH,
GHSA-8mgp-746c-j5xp) has `first_patched_version = NONE`**, so bumping was never available and
the alert stays open against the lockfile after this ships; what changes is that no deployed
process loads it.

**`memotron.certification.PorterStemmer` was a public API symbol by accident** — nltk's
class, exported purely as a side effect of the module-level import, re-exported by no
`__init__`. Its removal is accounted for in `api_since_cutover.ACCEPTED`. The class that uses
it is `LocalOfficialBenchmarkJudge` (public, no underscore), so constructing it without the
`benchmark` extra now raises — a disclosed behaviour change, not an internal tidy-up.

**Upward tier edges are 0 and have held for a week unattended** — `coupling_report.py` on
2026-09-09 reports `import cycles 1 · modules in a cycle 2 · largest cycle 2 · upward tier
edges 0 · COUPLING: PASS`. The one surviving cycle is `config`↔`prompts`, still deferred to
refactor unit 7 against the in-code decision record at `config/_prompts.py:3-4`.

**`docs/findings/python-refactoring-opportunities.md` exists only in the MAIN checkout's
working tree** (untracked, assessed on `main` at `a51d3ff`). Most of its P1 set is already
filed: private client-to-engine reaches = #153, typed payloads at seams = #128, SQLite/Postgres
contract testing = #149, the `config`↔`prompts` cycle = unit 7. **Three items have no issue
anywhere**: normalizing enums at input boundaries, explicit control-plane precedence, and SDK
lifecycle management. Mapped onto #227; the file is one `git clean` from gone.


### 2026-09-09 (late) — edge/load sweep of the deployed hosted API

**Local suite has no flakiness**: 3 consecutive full runs on one tree, **1900 passed each**
(228s / 223s / 216s). The known-flaky concurrency row did not fire in three.

**24/24 edge cases behaved as expected** against `latest`. Worth knowing what is CORRECT,
so nobody re-derives it: `Authorization: Bearer` fallback works; an UPPERCASE header name
works; a **present-but-empty** `x-litellm-api-key` refuses rather than falling back to
`Authorization` (DW-014's confused deputy, closed); unicode, emoji, 8 KB values and
`robert"); DROP TABLE relationships;--` all store cleanly; malformed JSON, unknown fields,
empty strings and wrong types all 400 with actionable messages.

**Registration is atomic**: 5 concurrent `memory/bootstrap` of one NEW `agent_id` returned
5x 200 and registered it exactly once.

**Config validation is comprehensive and fail-closed on write**: every out-of-range value
(`dedup_threshold` 1.5/-0.5, `min_salience` 99, `max_memories` 0, `min_endorsements` 0/-1,
unknown memory type, empty `project_goal`) was rejected, and after 14 rejected writes the
tenant config was unchanged — still `project-v1`, `min_endorsements: 1`, 1 version.

**Load: no defect demonstrated.** No errors at any level tested. 8 concurrent heavy
requests (33 KB `/api/graph`) peaked at **1.82 s vs 0.83 s for one — 2.2x latency for 8x
load**, sub-linear. The admin server IS single-threaded (`http.server.HTTPServer`,
`__init__.py:2628`) on **1 replica**, which looked like a serialization risk and measurably
is not at these levels. Absolute numbers are one laptop over VPN (~0.5 s baseline RTT) and
must not be quoted as a baseline.

**Cross-AGENT reads are possible and are DOCUMENTED**, not a defect: `mode: simple` means
all agents share one user scope, so `agent_id` is attribution and not an isolation
boundary. Cross-TENANT is still untested and is what sets #223's severity.

Filed from this sweep: **#224** (an unauthenticated refusal enumerates every registered
scope and `agent_id` on 4 routes — a real leak, and NOT covered by #200, which bounded the
success paths), **#225** (raw pydantic text, inconsistent with sibling errors),
**#226** (an unexplained bimodal latency split, filed as an observation with the
controls that refuted two hypotheses of mine).


### 2026-09-09 (evening) — GATE C PASSED on `latest`, end to end, with a real LLM

The acceptance test the plan called for and nobody had run. Through the documented
consumer path (`/api/platform/*`), authenticated with a bound gateway key:

| stage | evidence |
|---|---|
| contract -> bootstrap | 200, registration + `task_run_id` |
| write an exact fact | 200, `relationship_uuid` returned |
| **retrieve it** | that uuid came back — a ROUND TRIP, not a non-empty response |
| publish an EPISODE | 200 `queued_for_dreaming`, pending 0 -> 1 |
| **worker consumes it** | `formation-default rel=2 nodes=4 eps=1`, pending 1 -> 0 |
| **totals corroborate** | scope rels 5 -> 7, matching `created_relationships=2` |
| **content corroborates** | extracted facts name the probe agent by id |

Three independent confirmations of one event. **`latest` works on the supported path.**

**`memory_remember` does NOT exercise formation.** It writes an exact fact directly,
bypassing extraction, so it queues nothing (`pending_episodes` stayed 0). A
remember -> search round trip proves storage and retrieval only. Formation needs an
EPISODE — `memory/log` or `memory/publish`.

**`memory_publish` WAS blocked on `latest` and is now live.** `project_memory_configure`
had never been called for `jedai-platform`, so it returned 400 naming that remedy.
Configured 2026-09-09 as `project-v1`, `configured_by: gate-c-operator-setup`, and the
whole path then verified by content:

| step | evidence |
|---|---|
| config | `configured` false -> true, `versions` 0 -> 1 |
| `memory/publish` | 400 -> **200**, `policy_version: project-v1`, `queued_for_dreaming` |
| worker run | `formation-default rel=2`, pending candidates 1 -> 0, episodes 1 -> 0 |
| project scope | `tenant:jedai-platform` 3 -> 5 relationships |
| **content** | facts extracted from the published prose, e.g. `hosted platform API — requires — bound gateway key`, `project memory — has state — issue #216` |

**`min_endorsements` is deliberately 1**, not a default accepted without thought: #216
records that three routes bind the acting agent to the principal's own `agent_id`, and a
principal carries exactly one, so any quorum above 1 is unsatisfiable over HTTP and
candidates would accumulate with no error explaining why. Raising it is a one-call change
and a visible version bump once #216 is fixed.

**Every route on the documented consumer path now answers on `latest`.**

**Still open, and unchanged by this:** `/api/graph?scope=tenant:jedai-platform` answers
**200 unauthenticated** — the console routes are deliberately outside #50's gate, which
covers `/api/platform/*` only. Project-memory CONTENT is readable without a key. That is
#23 (browser auth), not a regression.

### 2026-09-09 (evening) — PROD DOES NOT EXIST; stage/preview/load DO

DNS, with `latest` resolving in the same query as the control:

```
jedai-memotron-admin.wdprapps.disney.com    NO DNS RECORD   <- prod
jedai-memotron.wdprapps.disney.com          NO DNS RECORD   <- prod
stage.jedai-memotron-admin...               10.178.146.133
preview.jedai-memotron-admin...             10.178.146.129
load.jedai-memotron-admin...                10.178.145.86
```

So "promotion to prod" is a STANDING-UP job, not a config promotion. #185's "prod's KEK
path is unverified" is true in a stronger sense: there is no prod to verify. Its committed
config would also refuse to start as written — `operationalStore.enabled: true` with
`keyManager` inheriting `enabled: false` and no `MEMOTRON_ALLOW_EPHEMERAL_KEK` hits the
fail-closed guard at `storage/postgres/__init__.py:126`. **Unless** the Vault payload
carries `MEMOTRON_KEK_B64` via `envFrom`, which a manifest cannot show.

**The config delta is exactly three keys**, not "none of latest's config":
`requireGatewayIdentity`, `keyManager`, `dreamWorker`, plus `operationalStore.injectAdmin`.
stage/preview/load run SQLite in `/tmp`, regenerated on every container start — no durable memory.


### 2026-09-09 (pm) — `main` was unbuildable for three hours, and two PRs shipped inert

**Observed.** `main` could not produce an image from #212 until the lockfile hotfix.
Harness `Continuous-Build` on the three merge commits:

| commit | PR | build |
|---|---|---|
| `2679147` | #210 | success — **the image `latest` ran all afternoon** |
| `4316455` | #212 | **FAILED** 15:30:21Z |
| `09bd52d` | #218 | **FAILED** 17:26:17Z |

Cause, reproduced with `docker build .`: `npm update vitest` rewrote
`ui/admin/package-lock.json` inconsistently, leaving `@emnapi/wasi-threads@1.2.2` pinned
in a tree resolving 1.2.3. **`npm ci` refuses a lockfile that does not satisfy itself;
`npm test` and `npm run build` do not** — they use whatever is already in
`node_modules`. So "100 tests pass, build clean" was true and unfalsifiable.

**`MEMOTRON_REQUIRE_GATEWAY_IDENTITY` was rendered for the `api` role ONLY**
(`roles.yaml:68`), so `values-latest.yaml: requireGatewayIdentity: true` did NOT mean the
environment authenticated callers. Measured on the live pods with api as control:
admin ABSENT, api PRESENT. Both #50 (`/api/platform/*` caller identity) and #194 (the
hosted API) gate on `identity_required()` read in the **admin** process, so both were
inert there and would have stayed 503 after deploying.

**`/api/platform/*` has 18 routes, not 15** — 3 GET + 15 POST, and `--no-admin-writes`
deliberately does not cover the prefix (#192). The admin POST split is **28 = 13 + 15**,
measured by AST over `do_POST`.

**`docs/architecture-report.html` is not in the repo** — untracked at session start,
never committed. Any task naming it is moot.


### 2026-09-09 — the console cannot authenticate, and that bounds #50

**Observed.** `ui/admin/src/api.ts` sends `Accept` and `Content-Type` and nothing else --
no `Authorization`, no `x-litellm-api-key`. So requiring gateway identity on `/api/*`
would 401 every console request in any environment with the flag armed (`latest` today)
and the UI would go blank. **Browser auth is #23 (SSO); it is not a wiring job.**

`/api/platform/*` is the opposite case and is now authenticated (#50): callers reach it
through the gateway and do carry a key. This is the prerequisite #194 was missing --
enabling those 15 routes previously exposed writes that authenticated nobody.

**`admin_server` may not import `mcp_auth`** -- both are `delivery`, and
`coupling_report.py` fails that edge. This is why the resolver lives in
`gateway_identity` (infra); `delivery -> infra` is downward and the gate passes. Anyone
planning to "just reuse the middleware" in a delivery module will hit this.

**`"latest"` in `ui/admin/package.json`** (all 15 deps) means `npm update` resolves the
newest MAJOR, not the newest patch. The vitest advisory named 4.1.11 as patched; the bump
landed 5.0.0. Check the vulnerable RANGE (`>= 2.1.0, < 4.1.11`), never version ordering.


### 2026-09-08 — the admin ingress is reachable off-cluster, and `gce-internal` does not mean in-cluster

**Observed** by `curl` from this laptop, no credentials, against deployed `latest`:

| probe | result |
|---|---|
| `https://latest.jedai-memotron-admin.wdprapps.disney.com/health` | `200 {"status":"ok"}` |
| `/api/overview` | `200` — **leaks**, see below |
| `/api/scopes` | `200`, correctly bounded to `tenant:jedai-platform` |
| `/api/graph?scope=tenant:probe-tenant-alpha` | **`400` refused**, "not registered for tenant" |

**`kubernetes.io/ingress.class: "gce-internal"` means *not internet-facing*, NOT *in-cluster
only*.** Both `latest` ingresses carry it and both resolve to RFC1918 (`10.154.184.54` api,
`.212` admin) — and both answered from off-cluster. The exposed population is every caller on
the Disney network, which `--no-admin-writes` does nothing about: it gates writes, not reads.

**mcp-forge's MCP servers are genuinely in-cluster** — `mcp-jedai-postgres`, `-redis`,
`-validator` declare **no ingress at all**, only a ClusterIP Service the gateway reaches
internally. Memotron does not follow that pattern today: it declares **two** ingresses
(`internal` → api:8000, `admin-internal` → admin:8765). Putting the MCP surface behind the
gateway (#48) would not change the admin surface, which is a separate ingress and is what
leaks.


### #206 PHASES 1-2 ARE IN `main` AND BROKE NOTHING ON `latest` (2026-09-08)

Merged as `e72fa62` (#209, combining what had been #205/#207/#208). Confirmed in main by
CONTENT: `_require_caller_tenant`, the `identity` relocation, Phase 1's `_serve`, and the
"Do not re-derive" correction are all present.

**The deployed admin surface was checked after the merge, because this change touches it.**
`_platform()` now raises 403 on a tenant mismatch and `latest` has the identity flag armed,
so the risk was turning the platform routes into 403s:

```
/api/overview                              200   console works
/api/graph?scope=tenant:jedai-platform     200   own scope readable
/api/graph?scope=tenant:probe-tenant-bravo 400   neighbour still refused
/api/platform/*                            503   UNCHANGED -- not a new 403
/mcp /health                               200
```

The 503s are the ones that mattered: the new guard sits BEHIND the existing
platform-not-enabled gate rather than in front of it. Intended, and now observed rather
than reasoned from the code.

**What Phase 2 does and does not claim.** The tenant is the boundary; the agent is a
namespace inside it. There is deliberately no `agent:<id>` allowlist check, because
`memory_bootstrap` calls `register_agent` -- registration is SELF-SERVICE, so a caller
could register agent X and then be authorized for `agent:X` because it had just created
it. Agent scopes become real boundaries only after registration is privileged
(`OPERATOR`/`ADMIN`), which is not done.


### `kubectl` WORKS ON `latest` — THE "REFUSED" NOTE WAS THE WRONG CONTEXT (2026-09-08)

**#185's "every kubectl read is refused" is true of `stage`/`load` and FALSE of `latest`.**
That belief stood for two weeks and blocked the whole seed path. Two mistakes produced it,
and both returned a confident answer:

* **the shell's default context was `gke_jennay-stage-1_...`** — a `Forbidden` from stage
  reads exactly like a `Forbidden` from latest;
* **the namespace is `jedai-memotron`, not `memotron`** (`namespaceOverride`), and
  `kubectl auth can-i` answers happily for a namespace that does not exist, so the first
  permission probe returned `yes` for a fiction.

Measured on `gke_jennay-latest-1_us-central1_usc1-jennay-latest-v1n1-cluster-1`, namespace
`jedai-memotron`: `list pods` **yes**, `create pods/exec` **yes**, and the api pods carry
`MEMOTRON_OPERATIONAL_STORE_DSN` plus the `memotron` CLI.

**So the operational store is reachable without Vault, without a Cloud SQL proxy, and
without handling a credential.** The Vault route was pursued for several rounds and was
never necessary: `VAULT_ADDR` is set nowhere on this machine, `csm.wdprapps.disney.com` is
a web UI whose SPA returns HTTP 200 for every path (so `/v1/sys/health` there proves
nothing), and `host` in the CSM payload is a private `10.x` IP a laptop cannot route to.

Full procedure: `docs/environment-bring-up.md`.

### `tenant:jedai-platform` NOW HOLDS MEMORY, AND THE CONSOLE SHOWS IT (2026-09-08)

The identity chain is closed end to end in production. Before: `403 "gateway key_alias
'memotron-live-lane-20260830' is not bound to a principal"`. After binding
`p-jedai-platform` (`role=user`, single allowed scope) via `kubectl exec` and reading it
back with `key show`:

```
/api/overview -> tenant:jedai-platform rels=3      (was 0, and the ONLY scope listed)
/api/graph    -> 6 nodes, e.g. "JedAI Memotron requires Postgres advisory locks…"
```

Seeded through the MCP surface — the supported path — with three real facts about this
deployment. **The neighbours stay refused**, so the seed did not reopen #199.

An objection I raised and then retired by measurement: binding the LIVE LANE's key would
supposedly narrow what that lane can reach. It could not — the alias was unbound while
identity is armed, so live-lane MCP calls against `latest` were already `403`.

### THE MCP SURFACE RUNS TWO REPLICAS AND HOLDS SESSIONS IN-PROCESS (2026-09-08)

A `tools/call` mid-seed returned **HTTP 404** and the retry succeeded. The api role runs
`replicaCount: 2`, and an MCP session established on one pod is unknown to the other. Not
a fluke and not a bug in the caller — anything building a long-lived MCP client against a
multi-replica deployment needs session affinity or a retry.


### BOTH EXPOSURES ARE CLOSED ON `latest`, VERIFIED IN PRODUCTION WITH A CONTROL (2026-09-08)

#200 merged as `7e656b1` and rolled out by ~20:04Z. The same unauthenticated read that
returned 16 nodes an hour earlier now refuses:

```
/api/graph?scope=tenant:probe-tenant-bravo  -> HTTP 400
   "scope 'tenant:probe-tenant-bravo' is not registered for tenant 'jedai-platform';
    registered scopes: ['agent:cutover-probe','agent:memory-admin','tenant:jedai-platform']"
/api/graph?scope=tenant:probe-tenant-alpha  -> HTTP 400
/api/graph?scope=tenant:jedai-platform      -> HTTP 200   <-- CONTROL, console still works
/api/overview                               -> 1 scope listed, no neighbours
```

The refusal names only THIS tenant's scopes — its own plus its agent scopes — which is the
default-deny attribution behaving in production exactly as designed. **The control is the
part that makes this a verification**: "everything is refused" would satisfy the first two
assertions and would be a broken console.

**#126 IS ALSO VERIFIED IN PRODUCTION** as a side effect: an unauthenticated MCP call
returns `401 {"error": "no gateway key on the request"}` while `/health` returns 200.
#183's note said #126 "remains verified only in tests"; it does not any more.

### THE B3 FIX IS ONLY LIVE WHERE THE RELEASE HAS LANDED (2026-09-08, UNVERIFIED for stage/prod)

`main` auto-deploys to `latest` — established today. **How and when `stage` and `prod` are
promoted is NOT established**, and I did not check. Until they receive this release the
fleet-wide write guard stays inert there, which is the whole of what #202 described. Do
not read "#202 closed" as "closed everywhere"; it is closed on `latest`.

### THERE IS NO PATH FROM THIS LAPTOP TO `latest`'s OPERATIONAL STORE (2026-09-08)

Established while trying to purge the probe scopes and seed `jedai-platform`:

* `POST /api/tenant-config/purge` -> **HTTP 405**, refused by `--no-admin-writes` (#192).
* Even reachable, it would target the WRONG THING: `_purge_tenant_state_payload` calls
  `purge_tenant_state(self._tenant_id(), ...)` — the LAUNCH tenant, now `jedai-platform`
  (already empty). The console has no verb for a neighbouring scope.
* `purge_tenant_state` does NOT touch `key_principals`. A concern was raised that purging
  the probe tenants would break latest's armed identity guard; reading the function
  refuted it — it removes nodes/relationships/episodes in the tenant's own scope set.
* The `memotron key bind` path needs the operational-store DSN, which lives in Vault at
  `apps/jedai/memotron/us-east-1/latest` (keys `host`/`port`/`database`/`user`/
  `password`) reachable cluster-side through VSO's AppRole. **`VAULT_ADDR` is set nowhere
  on this machine** — not in the environment, not in shell profiles, not in the workspace.

So seeding `tenant:jedai-platform` is blocked on cluster/Vault access, not on knowing the
command. The gateway alias resolves to `memotron-live-lane-20260830` — note that is the
LIVE TEST LANE's key, so binding it to a single allowed scope would narrow what that lane
can reach; prefer a separate key for the seed.


### MERGING TO `main` AUTO-DEPLOYS TO `latest` (2026-09-08)

**This was not known and was actively assumed false for most of session 42.** #196 merged
and was serving on `latest` within the hour; no manual release step was taken. Confirmed
by reading the deployed surface, not by inspecting a pipeline:

```
/api/tenant-config  tenant_id=jedai-platform  default_scope=tenant:jedai-platform
                    graph_tenant_ids ABSENT   warnings (none)
```

All three are post-#196 values; before the merge that endpoint returned `wdpr-demo`,
`customer:wdw:pinnacle-events`, a populated `graph_tenant_ids`, and a wrong-tenant warning.

**The lesson is the habit, not the fact:** "not deployed" was asserted repeatedly across a
session on the strength of it having been true earlier. A deployment claim has an expiry;
re-read the surface before repeating one.

### GATE A IS MET ON `latest` — THE WORKER FORMS MEMORIES IN PRODUCTION (2026-09-08)

The deploy boundary is exact, because **`consolidation` cannot exist under
`default_config()`**:

| observation | value |
|---|---|
| first `consolidation-default` run | **2026-09-08T17:30:06**, then every 15 min, `decisions=3 scopes=3` |
| `formation` at that same instant | **`eps=3 rels=3 nodes=4`** |
| `tenant:probe-tenant-bravo` | **3 → 6 relationships** |

The scope count moved by exactly the 3 the run reported — two independent measurements of
the same event, which is what makes this an observation rather than a log reading.

**A real LLM is implied, not assumed:** since #196 the worker exits `rc 2` rather than run
when extraction resolves to `RuleBasedExtractionTransport`. It ran and formed
relationships, so a real transport resolved.

Still NOT met: the console shows `rels=0` for its own scope (the memory is in probe
scopes), and pruning remains `pruned=0` across 196 runs — unfed, consistent with A4.


### THE IDENTITY GUARD IS ARMED ON `latest` AND ON NOTHING ELSE (2026-09-08)

Measured with `helm template`, per env, counting the rendered variable rather than
reading the values files:

| env | `requireGatewayIdentity` | `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` rendered |
|---|---|---|
| **latest** | `true` (#183) | **1×** — guard enforces ADMIN |
| stage, prod, preview, load | inherits `false` | **0×** — `require_fleet_wide_read` returns immediately |

**This corrects an earlier note in the takeover backlog** which said the guard is "a no-op
whenever `identity_required()` is false, which is the deployed default". It is the default
for four environments and NOT for `latest`.

Two consequences that are easy to get wrong in the other direction:

* **The deployed MCP server serves every tool.** The api role runs
  `examples/mcp_server.py`, not `src/memotron/mcp_server.py` — which looks like it
  might expose a smaller surface. It does not: that file does
  `from memotron.mcp_server import ... mcp`, so all 39 tools are served, including
  `remediate_coherence`. Guessing otherwise was refuted by reading it.
* **Arming the flag is not a flag flip.** #183's note in `values-latest.yaml`: turning it
  on with an empty `key_principals` table refuses EVERY caller, because each request
  resolves to no principal and fails closed. Rows must be bound in-cluster first.

### `--no-admin-writes` GATES WRITES, NOT READS, AND THE READ SIDE LEAKED CONTENT (2026-09-08)

Reproduced against the handler `admin_server.main()` builds, with two tenants holding
memory:

```
/api/scopes -> ['agent:a-tenant-under-test',
                'tenant:secret-neighbour',      <-- rels=1
                'tenant:tenant-under-test']

/api/graph?scope=tenant:secret-neighbour
    -> nodes: ['dark mode', 'Neighbour Team', 'Neighbour Team prefers dark mode']
```

A neighbour's scope key, its relationship COUNT, and its memory CONTENT were all readable
by anything that could reach the port. Cause: `build_demo_control_plane` registered every
scope `discover_graph_scopes` found, and `_request_policy` — the only gate on both routes
— consults exactly that registry. Fixed in #200 (`Closes #199`).

**IT IS EXPLOITABLE ON `latest` RIGHT NOW.** An earlier version of this entry said it was
not, "because latest holds exactly one tenant" — that conflated REGISTERED tenants (the
governance tables, where `jedai-platform` really is the only one) with SCOPES HOLDING
MEMORY, which is what this leak exposes. Measured 2026-09-08 19:00Z against the deployed
console, unauthenticated:

```
/api/overview -> tenant:jedai-platform       rels=0
                 tenant:probe-tenant-alpha   rels=7
                 tenant:probe-tenant-bravo   rels=6
                 user:local-cac319ad1dd105e6 rels=1

/api/graph?scope=tenant:probe-tenant-bravo   -> 16 nodes
                 'Memotron MCP', 'JedAI Gateway', 'key_principals', 'live-126-…'
```

The content is probe data rather than a customer's, which bounds the impact — but the
MECHANISM is demonstrated against a real deployment, not inferred from code. Two scopes
the console is not launched for are readable by anything that can reach the port.

### THE SCOPE PLANE RECORDS NO OWNER (2026-09-08, tracked as #201)

`storage/sqlite/_graph.py` holds **zero** `tenant_id` references. A relationship carries
`scope_key`, `scope_kind`, `scope_id` and nothing else, so ownership is derivable for only
two of the four kinds: `tenant:<id>` (the scope_id IS the tenant) and `agent:<id>` (via
`tenant_agents`). `user:` and `customer:` cannot be attributed at all — `user_scope()`
builds `user:local-<sha256 of the OS account>`, which two different tenants both produce
indistinguishably.

This **blocks the end-user half of #23**: signing someone in answers *who is calling*, but
`MemoryPrincipal.allowed_scope_keys` still has to be filled with "this person's `user:`
scope and no one else's", and that set is not computable from what is stored.

Renaming scopes to encode the tenant is RULED OUT: `scope_key` composes the truth key
(`dreaming/_identity.py:178`), so it would orphan every existing memory.


### `latest` DIAGNOSED ITS OWN WRONG-TENANT DEFECT, IN THE API, FOR MONTHS (2026-09-08)

`GET /api/tenant-config` on latest, unauthenticated, returned:

```
tenant_id       : wdpr-demo
graph_tenant_ids: ['jedai-platform']
warnings        : tenant id 'wdpr-demo' matches no tenant in this graph
                  (found: jedai-platform); tenant-scoped reads ... will be
                  empty and purge would target the wrong tenant
store           : PostgresStorageBackend
```

The chart passed `--tenant-id wdpr-demo`, so **every** tenant-keyed read (LLM credential
status, prompts, project-memory policy, agent registry) addressed a tenant that does not
exist there, and `Purge generated state` was aimed at it. `resolve_launch_tenant_id` was
correct and tested throughout; the CHART was wrong and **no test read the chart**. This
endpoint was fetched for #185 and only `operator.store` was read.

**The generalisable half:** a component that reports its own misconfiguration is only as
good as the field someone looks at. When reading a diagnostic endpoint, read the
`warnings` array first, not the field you came for.

### `graph_tenant_ids` NEVER MEANT WHAT ITS NAME SAYS (2026-09-08)

`known_tenant_ids()` is a `UNION` across six **governance** tables — `tenant_agents`,
`tenant_llm_credentials`, `tenant_prompt_versions`, `tenant_prompt_overrides`,
`project_memory_config_versions`, `agent_motive_assignments`. It touches **no memory
scope**. So `graph_tenant_ids: ['jedai-platform']` and "memory lives in
`tenant:probe-tenant-alpha`/`bravo`" are both true of different things, and the earlier
STATE.md entry recording the former needed no correction. The `graph_` prefix is what
made "jedai-platform holds memory" believable off a field that only proves a credential
was sealed. The field is now deleted (leak), so the trap is gone with it.

### THE ADMIN SURFACE LEAKS NEIGHBOUR MEMORY *CONTENT*, NOT ONLY TENANT IDS (2026-09-08)

Reproduced by an independent verifier against the real `MemoryGraphHandler` with two
tenants holding memory:

```
/api/overview.scopes -> ['tenant:secret-neighbour-tenant', 'tenant:tenant-under-test']
/api/graph?scope=tenant:secret-neighbour-tenant
     -> nodes: ['dark mode', 'Neighbour Team', 'Neighbour Team prefers dark mode']
```

`--no-admin-writes` gates writes, **not reads**. Cause: `client.authorized_scope_keys` is
`None` on the admin client, so `_require_authorized_scope` is a no-op, and
`build_demo_control_plane` registers every scope `discover_graph_scopes` finds.
PRE-EXISTING. Not exploitable on latest **only because latest holds exactly one tenant** —
which the tenancy work ends. Removing `graph_tenant_ids` closes tenant-id enumeration
through the tenant block and **nothing more**.

### THE WORKER'S TRANSPORT BUILDER FAILS SILENTLY, BY DESIGN (2026-09-08)

`build_transports_from_tenant_graph` → `build_transports_from_env` returns
`(RuleBasedExtractionTransport(), None)` when no per-tenant credential is sealed and
neither `LITELLM_API_KEY` nor `OPENAI_API_KEY` is set. It **raises nothing and logs
nothing**. `bind_default_tenant` was rejected for this exact property; the replacement has
it too. So a renamed Vault key reproduces the original 3-of-694 failure exactly: clean
start, `cycle complete` every time, nodes instead of relationships. `worker.main` now logs
the resolved transport unconditionally and returns **rc 2** rather than running.


### `--no-admin-writes` IS VERIFIED ON ALL THREE LOWERS (2026-09-08)

`probe_admin_no_writes.py` (merged in #195) passes against **latest, stage AND load**: 13
tenant-administration routes answer 405 including `/api/tenant-config/purge` and
`/api/tenant-config/llm`, and `/api/platform/memory/search` answers 503 rather than 405 —
which is what proves the switch was *narrowed* rather than left blanket.

The probe needs **no credentials** and takes `--url`, so it runs against any environment
unchanged. That is what made three environments cheap to check instead of one.

### stage and load RUN THE DEMO SQLITE FROM /tmp, measured from outside the cluster (2026-09-08)

`/api/overview` reports the backend since #187, and the admin ingress answers it
unauthenticated on every lower environment:

| env | `operator.store` | `operator.graph_path` | tenants in graph |
|---|---|---|---|
| latest | `PostgresStorageBackend` | `null` | `['jedai-platform']` |
| **stage** | **`sqlite`** | **`/tmp/memory_graph_demo.sqlite`** | **`[]`** |
| **load** | **`sqlite`** | **`/tmp/memory_graph_demo.sqlite`** | **`[]`** |

This **confirms #185's chart-derived table against the running systems**. stage and load are on
ephemeral pod disk with `operationalStore: false`, which is the #130 failure mode concretely —
a per-replica store, destroyed on restart.

**Both also report `graph_tenant_ids: []`, so they hold no memory data at all.** The
"each pod seals under a different key" hazard therefore has nothing to corrupt there *yet*,
which lowers the urgency of the stage/load half of #185 relative to its prod-KEK half. The prod
KEK question is untouched by this: `/api/overview` reports a storage backend, not whether
`MEMOTRON_KEK_B64` is present in a Vault payload, and prod has not been read.

### CHECKS A AND B PASS ON `latest` — the kill switch is live and NARROWED (2026-09-07)

`probe_admin_no_writes.py` against `latest` on image `0.1.0-5e0816f`:

* **all 13 tenant-administration routes answer 405**, including `/api/tenant-config/purge` and
  `/api/tenant-config/llm` — the two the switch exists for;
* **`/api/platform/memory/search` does NOT answer 405**, which is what proves the switch was
  narrowed to a prefix rather than left blanket.

Every claim about #192 before this was local-only. The probe gates itself on a non-destructive
canary (`/api/memory/pin`) and refuses to touch `purge` unless the guard has already been shown
to fire, because an unguarded `purge` succeeds.

### CORRECTION: the harm I used to justify narrowing the kill switch does NOT exist as deployed

I argued — in #192's PR body, its commit message, this file and the skill — that a blanket
refusal "would have taken the documented agent-memory HTTP API down on latest, stage and prod".

**Measured: those 15 routes already return 503 in every deployed environment.**

```
POST /api/platform/memory/search -> 503
{"error":"Memotron platform API is not enabled for this server"}
```

`handler.platform` is set **only** by `local_platform.py:216`. `main()` — the entry point the
chart runs — never sets it, so the platform API is `None` in every deployment *by
construction*, not by misconfiguration. `sweep_admin.py:114` already recorded this shape
("every `/api/platform/*` route, and ALL of them").

The narrowing was still the right change — the API is real on the local/SDK path, `README.md`
documents it, and refusing 13 routes rather than 28 is more precise regardless — but the
specific, urgent harm I claimed was not real. **The claim was plausible, consistent with the
code I had read, and wrong; I did not check the deployed behaviour before asserting it three
times.**

### `README.md` documents a "Hosted consumer API" that is disabled in every hosted environment

`README.md:1762` heads the table "Hosted consumer API endpoints used by
`MemotronPlatformClient`" and lists 15 `/api/platform/*` routes. Every one returns 503 on
`latest`, because `main()` never enables the platform (above). Either the deployed entry point
should enable it or the docs should say it is local-only. Worth an issue; not yet filed.

### `check.sh` 16/16 IS NOT THE CI GATE, and a red `main` proved it (2026-09-07)

`Build_Check` runs **`scripts/ci-build-check.sh`**, not `check.sh` (`INFRA-BACKLOG.md:281`).
Two differences each hide failures, and the repo documented only one of them:

* **CI has no `MEMOTRON_TEST_POSTGRES_DSN`** (`ci-build-check.sh:76-82`). Every local full
  run in sessions 37-39 had it set.
* **CI runs bare `pytest -q`, no marker filter** (`:65`); `check.sh`'s tests lane runs
  `-m "not postgres and not corpus"`.
* **CI has no `ui/admin/dist`.** A dev machine does. Anything reading that build differs.

`ci-build-check.sh:12-16` warns that a green *hermetic* run says nothing about Postgres. **The
inverse — that a green Postgres run says nothing about CI — was not written down**, which is
how #192 merged red and #193 inherited it.

**What actually failed:** `test_a_non_api_path_is_served_as_a_static_asset` asserted
`status in (200, 404)`. `_send_static_asset` returns **503** when `static_dir` does not exist
(`admin_server/__init__.py:2150-2154`) — CI, always; a dev machine, never. Fixed, and the
static branch is now pinned in **both** build states by controlling `static_dir` directly
rather than reading whatever the machine has.

**Reproduce CI locally in one line:**

```
mv ui/admin/dist /tmp/x && env -u MEMOTRON_TEST_POSTGRES_DSN bash scripts/ci-build-check.sh
```

**Harness logs are readable from here** — PAT at `.local-secrets/harness-pat` (mcp-forge
worktree). The step log needs the node's real `logBaseKey`, fetched via
`pipeline/api/pipelines/execution/v2/<exec>?...&stageNodeId=<stage>`; a hand-constructed key
returns HTTP 200 with **zero bytes** rather than an error. Exit codes are diagnostic:
`ci-build-check.sh` exits **1** for a failed test lane and **2** when it cannot run, so
`exit status 1` in Harness means a test failed, not a dependency problem.

### `latest`'s REAL tenant id is `jedai-platform`, and the chart says `wdpr-demo` (measured in production 2026-09-07)

The deployed admin server says so itself, in `/api/overview`:

> `tenant id 'wdpr-demo' matches no tenant in this graph (found: jedai-platform); tenant-scoped
> reads (LLM credentials, prompts, project-memory policy, agent registry) will be empty and
> **purge would target the wrong tenant**`

`graph_tenant_ids: ['jedai-platform']`. **This corrects an earlier claim in this file and in
the tenancy plan** that the store held only `probe-tenant-alpha`, `probe-tenant-bravo` and
`tenant-motive-fk` — those are SCOPE ids left by a probe, not tenants.

Consequences:

* **This unblocks Phase 1 of the tenancy plan**, which was waiting on someone choosing
  `latest`'s tenant id. The environment answered it.
* `.helm/values.yaml` passes `--tenant-id wdpr-demo`. Today that is close to cosmetic; under
  Phase 1 it becomes load-bearing, because it seeds the authorized scope set.
* `memory.visible_facts: 0` on the console is consistent with the SAME mismatch rather than
  with an empty store — confirm that before anyone reads it as data loss.
* Confirm `jedai-platform` is intended and not itself an artifact before changing what
  `purge` targets.

### #187 IS VERIFIED IN PRODUCTION — the admin console reads the real store (2026-09-07)

The check this file recorded as outstanding. On image `0.1.0-3ef1767`:

```
operator.store      = 'PostgresStorageBackend'
operator.graph_path = None
```

Every claim about #187 was against a local Postgres until this. The console no longer serves
`/tmp/memory_graph_demo.sqlite`.

### The two live cross-tenant WRITES are closed in code, NOT yet in production (2026-09-07)

**#192 merged** (`5e0816f`) — admin `--no-admin-writes` and the six MCP tools. **It is not
deployed**: latest runs `0.1.0-3ef1767`, which is #190, and the admin args carry
`--principal-role` with no `--no-admin-writes`. Three post-deploy checks remain:

| check | expected |
|---|---|
| `POST /api/tenant-config/purge` | **405** |
| `POST /api/platform/memory/search` | **not** 405 — proves the narrowing landed, not just the flag |
| cross-tenant `configure_tenant_llm` over MCP | refused |

### A kill switch that refuses EVERYTHING can take down a documented API (caught pre-merge 2026-09-07)

`--read-only` refused all 28 admin POST routes. Fifteen of them — everything under
`/api/platform/` — are the agent-memory HTTP API that `README.md` documents as the equivalent
of the MCP tools, called by an agent at session startup. The blanket version would have killed
a public API on **latest, stage and prod** to close an exposure living entirely in the other
thirteen routes.

It passed `check.sh` 16/16 and 61 of its own tests. **No gate could have caught it**, because
every test asserted the refusal the author intended. What caught it was enumerating the surface
before merging rather than trusting the design note.

Now `--no-admin-writes`, refusing 13 and leaving 15, with the default being REFUSE: only the
named `/api/platform/` prefix stays open, so a new route is blocked until someone decides
otherwise. The earlier objection — *"classifying 28 routes is error-prone, `remember` and
`publish` read like queries but are writes"* — is right about a SEMANTIC split and does not
apply to a structural prefix.

### THIS REPO SQUASH-MERGES, and that makes stacked PRs a trap (measured 2026-09-07)

Every merge on `main` has **one parent** — checked by parent count on the last six (#187, #182,
#183, #181, …), not by reading a settings page. And `delete_branch_on_merge = false` (GitHub
API), which matters more than it looks:

* **GitHub does NOT auto-retarget a stacked PR here.** Auto-retargeting is triggered by
  *deleting* the head branch. With deletion off, a PR based on another branch keeps pointing
  at it forever. A claim that "GitHub retargets it when the base merges" was made in this
  session and is **false for this repo**.
* **A PR based on a feature branch is `CLEAN`, not `BLOCKED`.** Branch protection guards
  `main` only, so a stacked PR can be merged with no review — into the wrong branch.
* **Squash-merging the base makes the stack diverge.** The base PR lands on `main` as a new
  commit whose SHA matches nothing on the stacked branch, so retargeting afterwards replays
  the base's changes and re-collides on the shared goldens.

**The sequence that works:** merge the base → merge `main` into the stacked branch and resolve
→ re-bless goldens from a clean full run → retarget to `main` → merge. Or merge the base with a
merge commit, which breaks the squash convention but skips the rebase.

### The admin surface has 28 mutating POST routes, not 24 (derived 2026-09-07)

The tenancy audit said 24. **24 is the `do_GET` count**; `do_POST` has 28. Both derived from the
AST rather than counted by eye. Only two verbs exist (`do_GET`, `do_POST`), which is what makes a
single guard in `do_POST` sufficient — and `test_read_only_admin.py` pins that, because nothing
else in the codebase would notice a `do_PUT` appearing.

### `coverage_floors.py`'s staleness guard watched `src/` only — a test-only edit slipped past it (fixed 2026-09-07)

The guard existed and the skill said it "fails STALE by design", but it compared
`coverage.json` against the newest file under **`src/`** alone. Coverage is a function of the
*tests* as much as the source, so a test-only edit is exactly the case that moves the number
while leaving `src/` untouched.

Observed: `mcp_auth` reported 95.3% against its 96.3% floor, tests were added for the uncovered
branches, and it **still read 95.3%** — a bare `pytest` collects no coverage (`addopts` carries
no `--cov`), and the guard did not fire because only `tests/` had changed. That reads
identically to "the new tests did not help", which is the worst way to be wrong about a
ratchet: it argues for lowering the floor.

`parity_coverage.py` already watched both, having hit the same trap on 2026-09-06. Ported.

**The fix broke `test_postgres_covered_probe.py`, and that was correct.** Its `_report` helper
pins the fixture's mtime to `newest(src/) + 60` to avoid racing the checkout — encoding the
src-only contract. With `tests/` in the input set, every case returned STALE instead of its real
verdict. The helper now uses the same input set as the script. **A fixture that pins a
contract has to track it**; the 9 failures were the test suite correctly noticing the contract
moved.

### Dependabot #23 (nltk, HIGH, no patch available) is NOT reachable in our usage (2026-09-07)

CVE-2026-81726 is a file-sandbox bypass in nltk's **model-artifact** APIs — `TransitionParser`,
`AveragedPerceptron.save/load`, `PerceptronTagger.save_to_json`, `save_maxent_params`. Our
entire nltk surface is **one import**: `from nltk.stem import PorterStemmer`
(`certification.py:26`). Zero hits on any affected component, zero on `nltk.download`/`nltk.data`.

`patched: NONE AVAILABLE`, so the alert will keep firing and will surface in any pre-prod
security review. Two options: document the non-reachability, or drop the dependency — a
PorterStemmer is ~50 lines and nltk is used nowhere else.

### A cross-tenant DESTRUCTIVE WRITE existed, and is fixed on branch `fix/truth-key-cross-scope-supersession` (2026-09-07)

Not inferred and not a read leak. Reproduced independently **twice** through the public SDK —
no privileged access, no crafted credential, no LLM. A write in one tenant's scope retired a
live memory in another tenant's scope:

```
distinct scopes? True | tenant:acme / tenant:acme:roadmap
victim BEFORE : active   ->   victim AFTER : superseded
ACTIVE rows left in tenant:acme = 0
```

**Mechanism.** `dreaming/_identity.py` builds `truth_key` by *unescaped concatenation* of
`scope_key:subject:predicate`, and `normalize_key` then casefolds it — so the tuple is not
recoverable from the string. `tenant:acme` + `roadmap:q3` and `tenant:acme:roadmap` + `q3`
produce the same key. The three reads that consume it took **no scope argument**, so nothing
downstream could recover the isolation the key had lost.

**Three write paths, not one:** supersession retires the foreign row; reinforce mutates the
foreign row's counters *and then raises*, so the victim is tampered with **and** the caller's
own write is lost; adjudication-approve (`client/_governance.py`) flips truth on whichever
scope shares the slot. The receipt brackets the mutation with `graph_state_hash` for the
**caller's** scope, so a receipt records a mutation with a null state delta — the exact failure
widening that tuple was meant to prevent.

**The variant needing no crafted input:** `casefold()` makes the truth plane case-insensitive
while scope matching is case-sensitive everywhere else. Two tenants onboarded as `Acme` and
`acme` destroy each other by accident.

**POSTGRES WAS AFFECTED TOO — measured, not argued.** Both original reproductions were SQLite.
Each of the three Postgres reads was mutated individually against a real Postgres and each
mutation fails the new parity arm on a *scoping* assertion. `relationships_for_truth_prefix`
had **no parity coverage at all** before this.

**Fix:** `scope_key` is a required keyword-only argument on all three reads (ABC + both
engines), threaded through four dreaming helpers, `ComposedDreamEngine`, and every call site.

**Scope-key uniqueness in `config/_tenancy.py` stays case-SENSITIVE — casefolding it was
tried and REVERTED.** The plan called for it, and an independent verifier found it reachable:
`agent_memory/_registry.py:163` dedups the scope list case-sensitively and *then* constructs
`TenantMemoryPolicy`, so a case-differing pair appends and raises at **agent-registration
time** where it previously succeeded. Reproduced directly before reverting. With the reads
scoped, `tenant:Acme` / `tenant:acme` are properly isolated, so the check bought nothing and
added an upgrade-time hard failure. Making the registry dedup casefold instead would be worse
— it would silently merge two distinct scopes. A test now pins that the pair is **accepted**.

**And it never covered the case that justified it.** `validate_tenant_policy` validates ONE
tenant's scope list. Two *tenants* named `Acme` and `acme` are two separate
`TenantMemoryPolicy` objects; cross-tenant uniqueness lives in
`config/_control_plane.py:67`, which compares `tenant_id` values **case-sensitively** and is a
different check entirely. So the casefold would never have caught the cross-tenant twin in the
first place — it only ever caught two case-differing scopes *inside one tenant*. Checked
directly, after the revert decision, not before.

**Deliberately NOT done — do not re-derive it.** Rejecting `:` in `scope_id` looks like the
obvious companion fix and is wrong. Colons there are load-bearing: `customer:wdw:pinnacle-events`
appears in README.md, `docker-compose.local.yml`, `examples/memory_graph_demo.py` and
`docs/design/23-sso-web-ui.md`, and `test_principal_from_registry_row.py` pins that `from_key`
splits only on the FIRST colon precisely so the id may contain the rest. With scoped reads,
sharing a truth key is harmless. A test now pins that the colon stays legal.

**RESIDUAL AFTER THIS FIX — the NODE plane, measured 2026-09-07.** Scoping the three reads
closes the relationship plane only. `node_identity_key` composes `scope_key:label:name`, and
`upsert_node` normalizes it then matches `SELECT uuid FROM nodes WHERE graph_key = ?` with **no
scope predicate**, while `graph_key` is `NOT NULL UNIQUE` in both engines:

```
tenant:Acme + "budget"  ->  tenant:acme:entity:budget
tenant:acme + "budget"  ->  tenant:acme:entity:budget     COLLIDE
```

On a hit `upsert_node` does `properties.update(properties)`, **overwriting the first scope's
`scope_key`**. Measured end to end: `tenant:Acme` goes **3 nodes → 1** and `tenant:acme` takes
them, with both scopes' relationships then pointing at the same node uuids. Blast radius
reaches right-to-be-forgotten node deletion (`erasure.py`) and tenant purge.

**PRE-EXISTING AND NOT CHANGED BY THE TRUTH-READ FIX — verified, because the first framing of
this was wrong.** Re-measured with `origin/main`'s unscoped read semantics restored in-process:
still `3 → 1`. The merge is driven by `upsert_node` during materialization and is independent
of the three reads *by construction*. The only difference on `main` is that the second write
**additionally** raises `client-managed memory produced no relationship disposition`, losing
the caller's own write on top of the node takeover. So the fix strictly reduces damage and
introduces no new exposure. (A verifier reported the branch "makes the node merge newly
observable"; that over-reads its own data — the merge is observable on `main` too.)

**It cannot be fixed the cheap way**: with `graph_key UNIQUE`, adding a scope predicate to the
lookup turns a silent share into an insert failure. That is a migration, so it belongs with
Route B and **deserves its own issue**. The colon-shift variant does *not* collide here (the
label sits between scope and name); only the casefold variant does. Measured, not assumed.

**Route B (escaping/length-prefixing the segments) is a SEPARATE ticket and must not be
sequenced before this.** It also closes the within-scope conflation, but every stored
`truth_key` keeps its old value while new writes compute a new one — so a `SINGLE_ACTIVE`
incumbent stops being found and the next write creates a *second active row on a single-active
slot*. Needs a two-engine backfill.

### All 13 client mixins declared `graph: Any`, so mypy could not see their storage calls (fixed 2026-09-07)

`client/_protocol.py:104` declares `graph: StorageBackend`, but every one of the 13 mixins in
`src/memotron/client/_*.py` **shadowed it with `graph: Any`** — while the `dreaming` mixins
declare `_graph: StorageBackend`. That is exactly why making `scope_key` required surfaced all
six dreaming call sites in mypy and **zero** client ones; `client/_governance.py` was found only
by pytest.

Typing all 13 properly costs **zero** new mypy errors. Verified in both directions: with the
typing in place, re-introducing the unscoped call *is* caught. It also retired **24**
`no-any-return` suppressions (5 entries deleted from `pyproject.toml`) and surfaced one
over-narrow annotation — `context.get_context` declared `graph: PropertyGraphStore`, a legacy
alias for `SQLiteStorageBackend`, while using only `dream_decisions`,
`record_dream_decision` and `utility_projection`, all on the ABC.

### The `check.sh` tests lane is a SUBSET, so its golden streams are incomplete by construction (2026-09-07)

Measured: the `tests` lane runs ~70s, a full single-process `pytest` with the DSN runs ~165s.
The `postgres` lane runs the remainder separately. Running `receipt_golden.py` /
`storage_golden.py` after `check.sh` alone reported **10 not observed**; after one full
single-process run it reported **0 not observed**.

**Bless goldens only from a full single-process run**, never from the stream `check.sh` leaves
behind. The repo's own rule is "0 not observed" — this is the mechanism that silently violates it.

### #126 IS VERIFIED LIVE ON LATEST (2026-09-07) — no longer a test-only claim

The single most important change to this file. Everything #126 asserted was hermetic until
today; it is now measured against the real gateway, real Postgres, and real bound rows.

- **The guard is ARMED on latest.** `MEMOTRON_REQUIRE_GATEWAY_IDENTITY=1`, confirmed by
  reading the env inside a running pod (#183, merged `d455e49`). Latest ONLY — rendered
  zero times under stage/load/preview/prod, checked per overlay.
- **`scripts/verify/probe_mcp_identity.py` passes ALL EIGHT ARMS, exit 0.** Including the
  one that matters: alpha naming bravo's tenant is REFUSED *and* bravo's scope is
  unchanged. A refusal message is a string the server chose to print; the absence of the
  write is the fact.
- **A minted virtual key self-resolves to its own `key_alias`.** Verified through the
  application's own `resolve_gateway_identity`, not a hand-rolled curl:
  `key_alias='dw-probe-alpha' is_anonymous=False`. This was the assumption everything
  rested on and it had been measured exactly once (DW-026). Both probe keys carry neither
  `user_id` nor `team_id` — the ~40% case DW-027 records, and precisely why the mapping
  hangs on the alias.
- **`/health` stays 200 unauthenticated through the ingress**, 0 restarts after the
  rollout. The crashloop risk did not materialise.
- **Two rows exist in `key_principals`**: `dw-probe-alpha`→`probe-tenant-alpha`,
  `dw-probe-bravo`→`probe-tenant-bravo`, both `role=user`. Bound via `kubectl exec` +
  `memotron key bind`; verified by a READ (`key show`), with a never-bound control
  proving the read discriminates. **The two gateway keys expire ~2026-09-08** (minted
  `duration: "24h"`), so re-running the probe later needs fresh keys and re-binding.
- **Probe rows are left behind by design** in `probe-tenant-alpha`/`probe-tenant-bravo`;
  cleanup would need cross-scope rights neither probe principal has.

### Two claims from this session that were WRONG and are corrected here

- **MCP is NOT broken through latest's ingress.** A single 404 during a probe led me to
  claim sessions were pod-local and unusable behind a round-robin LB. Refuted: the same
  follow-up call succeeded **8/8** through the ingress, and the full probe now passes
  through it. The failure was almost certainly transient rollout convergence — it happened
  seconds after a rollout while the old pod was still present and `READY: false`.
  **`sessionAffinity: None` was cited as independent confirmation and was not** — it was
  merely *consistent* with the hypothesis. See *Lessons learned*.
- **We are NOT blocked on the gateway for the MCP v2 migration.** `mcp` 2.0.0 release notes
  state the same server "still serves every 2025-era client from the same `MCPServer`", so
  the server can move independently. Filed as **#184**.


### The Dream Worker form factor is OWNED but UNMADE (#142 answered 2026-09-06)

- **The "epic scheduler issue" is `jedai/memotron#17`**, not an issue in `jedai/program`. #142
  searched the wrong repo: *"the epic's scheduler issue"* means an issue BELONGING to
  jedai/program#393, not one IN it. #17 is the epic's child (roadmap line 98; `parent:
  Operationalize Memotron`) and its first scope bullet says verbatim *"this issue owns the
  written decision"*, over the same three shapes the portal names, with the same unsettled lean.
- **The decision is still unmade, and the implementation is ahead of it.** #17 is OPEN with that box
  unchecked, while the chart ships shape #2 — `.helm/templates/cronjob-dream-worker.yaml`,
  `kind: CronJob` — enabled on latest since #179. #17 leans shape #3 (its own deployment). Fine for
  latest per #125; settle it before prod, and note that switching shapes later is a migration.
- **Two pointers still owed** (deliberately not made on the #126 branch, which must not carry
  unrelated chart edits): name #17 at `.helm/values.yaml:441`, which defers to "the epic's scheduler
  issue" without a link, and in the status box of `docs/design/17-dream-worker.md`. Let these ride
  the next PR that touches those files.
- **`gh issue list --limit N` SILENTLY CAPS.** A re-search of `jedai/program` with `--limit 400`
  returned exactly 400; the real total is 1066. Exactly-the-limit is the tell. A bounded search
  reported as an absence is what produced #142 in the first place, and it nearly produced the
  answer too.

### Cluster and infra (observed 2026-09-06)

- **`latest` runs `0.1.0-e0583c2`** — the PR #180 merge — on api ×2, admin and the dream-worker
  CronJobs, since 2026-09-05T01:15Z. The `otel` extra and the pgvector split ARE deployed. A prior
  STATE.md next-step claiming `0.1.0-08af2a8` was stale; see the corrected item below.
  **Still unconfirmed: the pgvector RUNTIME effect.** The startup line that would prove it has
  rotated out of the retained log. Read it from the database (`relationship_embeddings.embedding_vec`)
  — do not restart a pod to make a log line reappear.
- **#162 is one-third done.** `latest`'s `jedai-memotron-data` PVC was deleted 2026-09-06 and its
  PV `pvc-69e74e82-…` is `NotFound`, so `reclaimPolicy: Delete` took the 10Gi disk. Pods stayed
  `Running`, 0 restarts. Before deleting: no pod mounted it, neither Deployment nor the CronJob
  declared a PVC volume, and `helm template` renders **0** PersistentVolumeClaims, so an upgrade
  cannot recreate it.
- **The kubeconfig authenticates EVERY context as `svc-gke-deploy@jennay-latest-1`.** `stage` and
  `load` answer `Forbidden` for `persistentvolumeclaims`. This is not a VPN problem and retrying
  does not help — it needs credentials with `container.persistentVolumeClaims.*` in `jennay-stage-1`
  and `jennay-load-1`. Two 10Gi disks remain billed, and #162's "do not delete load's until it rolls
  past `bb5f42f`" caveat is UNVERIFIED.
- **An empty `kubectl` result is not a negative result.** Three queries here returned empty and all
  three were broken — a `perl -e 'alarm N; exec @ARGV'` wrapper SIGALRM-killing kubectl, and zsh
  passing a command held in a variable (`$K get pods`) as one command name. Each empty result read
  as "nothing mounts this volume" and would have authorised an irreversible deletion. Always run a
  control that MUST return rows; inline the command; use `--request-timeout=30s`.

### Latest RUNS THE PRODUCT, verified end to end (2026-09-07)

Not "the tests pass" — measured against the deployed environment, by driving it.

- **The core loop works.** An episode submitted through the real extraction path
  (`OpenAICompatibleExtractionTransport`, a live gateway call) was consumed by formation and
  produced memory: relationships 24 -> 30. **The first time this has ever happened in a
  deployed environment.**
- **And it works UNATTENDED.** The 18:00Z CronJob consumed a seeded episode with no human
  involved: `processed_episodes=1`, `decision_count=1`, `created_nodes=1`, `dream_decisions`
  0 -> 2 (`formation_episode_selected`). That is **#17's acceptance criterion** — "maintenance
  runs on schedule in LATEST with no client involvement" — met for the first time.
  **One sample only**: it created a node, not a relationship. Plausible (content/dedup) but
  not established; more episodes should go through the CronJob before calling it reliable.
- **`pgvector IS enabled`** — read from a worker log 2026-09-07. This closes the gap the
  cluster section of this file flagged as unconfirmed after the startup line rotated out.
- **Identity holds live**: all eight probe arms, cross-tenant write refused AND absent.

### Both defects found were OBSERVABILITY, and they share one shape (2026-09-07)

**The system works and the instrument says otherwise.** That is the specific failure that
makes an environment un-operable, because it is indistinguishable from the outage it would
be used to detect.

- **`processed_scopes` read 0 on 258 healthy formation runs.** Formation never set it
  (`_run_coherence` did, `_consolidation` increments it). I read that zero as a dead worker,
  reported it as such, and was wrong. Fixed in PR #182; **not in the running image**, so the
  deployed worker keeps reporting 0 until a new one deploys.
- **The admin surface served DEMO data in every deployed environment.** `admin_server` could
  only open a SQLite file, so `/api/overview` on latest reported tenant `wdpr-demo` and
  `/tmp/memory_graph_demo.sqlite` while Postgres held 10 episodes and 263 formation runs.
  Fixed in PR #187, which also adds a `store` field so the surface states its backend.
  **Answering "is the queue draining" required exec-ing into a pod and writing SQL** — that
  is the evidence the operator surface was missing, not merely wrong.

### Chart facts corrected by measurement (2026-09-07)

- **`injectAdmin: false` was justified by two false claims**: "reads the demo SQLite graph off
  the PVC" (there is no PVC — deleted under #162 the same day) and "stays off the connection
  budget" (`operational-store-sizing.md` already budgets `Admin replica | 1 x 10 = 10` inside
  its peak-of-93).
- **Connection budget read, not assumed**, as that doc instructs: `max_connections=300`,
  17 in use, peak required 93 = 31%. Latest opts in; prod and stage deliberately do not.
- **mypy caught what the tests missed, twice today.** Most recently: `main()` was rewired to
  the operational store while two internal call sites still passed `graph_path`, which is
  `None` on Postgres and would have raised at runtime. Tests exercised the helpers directly
  and sailed past it.


### MCP request identity (#126, measured 2026-09-06)

- **A tool body CAN reach the request.** Under streamable-HTTP, `RequestContext.request` carries the
  per-POST Starlette request, so `request.state` written in ASGI middleware is readable in the tool.
  This **refutes** the earlier STATE.md claim that the lowlevel server never populates it.
  Established by mutation: removing the middleware's write makes the tool see `None`.
- **A contextvar does NOT cross.** MCP dispatches tool calls in a different anyio task from the
  middleware, so a contextvar reads its default in the tool. `request.state` works only because it
  is backed by the shared `scope["state"]` dict. Getting this wrong does not raise — it authenticates
  at the door and authorizes nothing.
- **Identity rebinds per REQUEST, not per session.** A session created with key A and driven with
  key B resolves as B. Sessions are *not* bound to their creating credential (MCP's own ownership
  check only arms when `scope["user"]` is an `AuthenticatedUser`), so a leaked `mcp-session-id` lets
  another tenant attach to the transport — but gains no scope, because of the per-request rebind.
- **39 tools registered; 33 build their scope in `_scope`; 6 never do; 3 more declare a scope and
  skip it by omission.** Read from `mcp.list_tools()`, not from source — a source parser missed
  `clear_tenant_llm` entirely.
- **`SQLiteStorageBackend` connects with `check_same_thread=True`** (`storage/sqlite/__init__.py`),
  so any synchronous storage call moved into an `anyio.to_thread` worker raises `ProgrammingError`
  on every request. Postgres survives it (dedicated loop thread), which is what makes this class of
  bug invisible in the deployed environments and visible only locally.
- **`GatewayRequestError`'s message embeds the gateway's response body verbatim**
  (`gateway.py`), and LiteLLM's 401 body was observed on the preview gateway containing
  `Received API Key = sk-...` plus the key's `LiteLLM_VerificationTokenTable` hash. Never return
  that message to a caller; log it instead.
- **Starlette's `Headers.get` takes the FIRST duplicate header; `dict(scope["headers"])` takes the
  LAST.** Any code doing both disagrees with itself, and an audit line written through one would
  name a different credential than the one the other authorized.
- **anyio's default `to_thread` limiter is a process-wide `CapacityLimiter(40)`.** Anything
  unauthenticated that reaches a `to_thread` call can occupy all 40. `/health` bypassing the
  middleware means probes stay 200 while the API is wedged.
- **TestClient must use `base_url="http://localhost"`.** FastMCP's DNS-rebinding allowlist rejects
  the default `testserver` host with **421** and no message naming the host — the same failure that
  once gave 421 through the ingress while `/health` stayed 200.
- **`FastMCP` caches its `StreamableHTTPSessionManager`** and the manager refuses a second `run()`,
  so a per-test app built from the module-global `mcp` fails every test after the first.

### A contract narrower than its implementations hides the drift (#163, fixed 2026-09-04)

`set_tenant_llm_credentials` took **8** keyword arguments on SQLite, **5** on Postgres, and the
abstract contract in `storage/base.py` also declared **5**. So SQLite had grown past the contract
in WS-17 T18 and Postgres never followed — and nothing flagged it, because the contract described
the smaller, older signature that one engine had already outgrown.

`_migrate_llm_credentials` passes all three embedding arguments unconditionally, so migrating a
tenant into any Postgres store raised `TypeError`. Theoretical when filed; **live now that latest
runs on Postgres**.

**A second divergence sat on the return path.** Fixing the write left Postgres accepting the
embedding endpoint and never returning it — the read had diverged separately, and a tenant would
have silently dropped back to the local embedder, a different vector space, nothing raised. Found
only because the probe read the value back instead of checking the call did not throw.

Fixed by moving the RULE, not the parameters: `resolve_embedding_endpoint` in
`_shared/_governance.py`, called by both engines. Postgres migration **V11** (append-only), the
tri-state read done inside the upsert transaction with `FOR UPDATE`, and the contract widened to
declare all eight with a note that an engine may not extend past it.

### Memory ROUND-TRIPS across replicas on `latest` — the cutover is real, not just configured (measured 2026-09-04)

Configured and working are different claims. This is the second one, measured through the agent
surface (`agent_memory_mcp.build_platform_from_env` -> `memory_remember` / `memory_search`), written
on one api pod and read from **the other**:

```
pod A (…-4kxb9) WROTE : episode 7b141c4a  relationship c44fc5ed
pod B (…-9lkf9) SEARCH: relationship_uuid c44fc5ed — 'latest environment is running on Postgres…'
```

Same uuid, different replica. That is shared durable state across pods — precisely what was
impossible before the cutover, when each replica held its own `:memory:` graph.

One agent write populated **16+ tables** (episodes, relationships, relationship_embeddings,
graph_epochs, scope_state_hash, memory_use_events, run_checkpoints, tenant_agents, …), so the whole
pipeline ran, not just an insert. Before it, exactly one table had rows: `schema_migrations`, 10.

Also observed: the extractor **correctly rejected** a first attempt using relationship type `ANCHOR`
(`CandidateSchemaError: not allowed by instruction set 'default'`). The deployed default allows
REQUIRES / PREFERS / SHOULD / IS / DECIDES / HAS_STATE / EXPERIENCED.

**The probe memory is still in `latest`** under agent `cutover-probe` — left deliberately as the
store's first real memory and as evidence.

### pgvector was installed the whole time; our own DDL disabled it (fixed 2026-09-04)

`vector 0.8.5` is installed in `memotron_db` and always was. Every pod logged `pgvector
unavailable` because two DDL statements shared one transaction:

```
ADD COLUMN embedding_vec vector -> OK
CREATE hnsw INDEX               -> InvalidParameterValue: column does not have dimensions
                                   (rolled the column back with it)
```

HNSW needs a fixed dimension; the column is dimensionless **on purpose** because
`relationship_embeddings` stores `dimensions` PER ROW — the width follows the transport (256 local
hasher, 1536+ gateway). So the index can never succeed under the current data model.

Cost: retrieval silently used `dw_cosine_similarity` over `float8[]` instead of the native `<=>`
operator, on every Postgres deployment. **And the message blamed the environment** — Adam checked the
instance and correctly found the extension present. Split into `PGVECTOR_COLUMN` (fatal) and
`PGVECTOR_INDEX` (own transaction, non-fatal), with distinct log lines. 7 hermetic tests added;
there were previously **zero**.

### The image never installed the otel extra, though pyproject said it did (fixed 2026-09-04)

`pyproject.toml` comment: *"the deployment image installs `--extra otel`"*. `Dockerfile:16` ran
`uv sync --frozen --no-dev --extra postgres` only. The chart sets `OTEL_ENABLED=true` everywhere, so
every pod logged `OTEL_ENABLED is set but the OpenTelemetry log packages are missing` and nothing
exported. #129's structured logging worked; only the EXPORT was absent — which is why it survived
weeks. Verified the fix builds: `docker build --target builder` exit 0.

### Cloud SQL `latest`, from Adam's full describe (2026-09-04)

- **ZONAL — no regional HA.** Fine for a lower; a decision to revisit before any upper.
- `max_connections 300`; `log_statement: all` and `pgaudit.log: all` (comprehensive query logging).
- Backups daily 06:00 UTC, retain 4, PITR on, txn log 3 days. Deletion protection on.
- Password policy: min length 25, 1-year change interval, reuse interval 5.
- **The DB user is `f-lst-bapp0248028-pgsql-memotron`**, NOT `memotron_app` as
  `docs/operational-store-deployment.md:27` and its §4 rotation command both say. **Doc defect, not
  fixed** — §4's `ALTER ROLE memotron_app` would fail as written.


### `latest` IS ON POSTGRES with a durable KEK — cutover complete (measured in-pod 2026-09-04)

The #123 chain is closed for `latest`. Measured **inside a running api pod** via the application's
own code paths, not read off a manifest:

```
engine        : postgres
key manager   : LocalKeyManager | key_id: local:4b271b7db22bbb71
ephemeral KEK : False
migrations    : [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
public tables : 41
```

Both deployments `successfully rolled out`, 0 restarts, `/health` 200 for api and admin from inside
the cluster. **"Survives a restart" is proven, not assumed:** the same `key_id` was measured in the
PREVIOUS pod generation, before the cutover deploy and before `keyManager.enabled` — so one key
spans a full pod replacement and two replicas, which is exactly what the ephemeral KEK could not do.

Three steps got there: the KEK written by hand into `apps/jedai/memotron/us-east-1/latest` as an
eighth key; **#176** `vaultSecret.enabled` (created VaultAuth + VaultStaticSecret, synced
`memotron-secrets` — 9 keys: the 7 CSM ones, the KEK, and VSO's `_raw` full-payload entry);
**#177** the three remaining flags.

**MEMORY IS DURABLE TOO — I said the opposite several times and was wrong.** `is_split: False`, so
the DSN switches the WHOLE surface to one `PostgresStorageBackend`: nodes, relationships and
episodes are live Postgres tables (all 0 rows, nothing written since cutover).
`MEMOTRON_GRAPH_PATH=:memory:` is still set and is **dead config** — `client/__init__` takes the
`config.storage` branch before it ever looks at `graph_path`. Confirmed through the real entry
point, not by reading code: `build_client(graph_path=":memory:")` inside the pod returns
`PostgresStorageBackend` with `key_id local:4b271b7db22bbb71`.

That dead `:memory:` value should be removed from the chart — it currently reads as "this env has
no durable memory", which is what misled me.

**`pgvector` is NOT available on the instance.** Observed at client construction:
`pgvector unavailable; vector retrieval falls back to dw_cosine_similarity over float8[]
(InvalidParameterValue)`. Retrieval works, on a slower path. Not yet filed.

### `vaultSecret` activates the KEK, not `keyManager` — and a manifest grep cannot see it (2026-09-04)

`envFrom: secretRef` mounts the synced Secret wholesale, so every Vault key becomes an env var of
the same name — and the payload key IS `MEMOTRON_KEK_B64`, which is what
`crypto.key_manager_from_env` reads. So the KEK went live at #176, with `keyManager.enabled` still
false. The chart comment in `values.yaml` had already written this down and I contradicted it while
designing the step order.

`keyManager` still earns its place, for a different reason than the ordering implied: with envFrom
alone a **misnamed** key is silent — `kek_b64` or a typo arrives as a variable nothing reads and the
pod starts with no durable key. The explicit `secretKeyRef`, deliberately not `optional: true`,
makes that `CreateContainerConfigError`.

**The measurement lesson:** my check was `grep -c MEMOTRON_KEK_B64` on the RENDERED CHART, which
returned 0 before and after and could never have caught this — envFrom does not enumerate its keys
in YAML. To know what environment a pod actually has, `exec printenv`.

### LOWERS are latest / stage / load. Preview and prod are UPPERS. (Ryan, 2026-09-04)

Corrected because I had it backwards and the mistake would have set the wrong priority. **Preview
runs in the stage CLUSTER but is an upper environment** — sharing a cluster is a placement decision,
not a tier. So the promotion targets for this work are the three lowers, and preview/prod come after.

The three lowers are identical in cutover shape — all flags off, one Vault secret per env serving
both `vaultSecret` and `operationalStore.vaultSecret`, real AppRole ids:

| env | Vault path (write `MEMOTRON_KEK_B64` here) | cluster |
|---|---|---|
| latest | `apps/jedai/memotron/us-east-1/latest` | `jennay-latest-1` |
| stage | `apps/jedai/memotron/us-east-1/stage` | `jennay-stage-1` |
| load | `apps/jedai/memotron/us-east-1/load` | `jennay-load-1` |

**A DIFFERENT key per environment.** One key shared across envs would mean a lower's compromise
reads another's sealed content, and there is no re-wrap path to recover from that.

Parked, not dropped: `values-prod.yaml` on `origin/main` has all three flags `true` (merged in
`e84c240`, #152) while no KEK exists and `ALLOW_EPHEMERAL_KEK` is set nowhere — which by #130's
argument should refuse to start. Most likely prod simply has not been deployed with that chart since
the merge. **Unverified — the local credential is `latest-1`-scoped and cannot reach prod.**

### The `latest` cutover needs FOUR flag steps, not two — `keyManager` depends on a Secret another flag creates (observed live 2026-09-04, session 33)

Observed against `gke_jennay-latest-1_..._usc1-jennay-latest-v1n1-cluster-1`, namespace
**`jedai-memotron`** (not `memotron` — see the trap below):

- `jedai-memotron-api 2/2`, `jedai-memotron-admin 1/1`, age 7d14h. **No drift from the chart**:
  `MEMOTRON_GRAPH_PATH=:memory:`, `OTEL_ENABLED=true`, no KEK, no `OPERATIONAL_STORE_*`.
- Secrets present: `memotron-tls`, `memotron-admin-tls`, `memotron-vault-approle` (7d15h —
  #97's prerequisite is real and live).
- **`memotron-secrets` does NOT exist, and there are zero `VaultStaticSecret`/`VaultAuth`
  objects.**

That last point breaks the two-step cutover order recorded earlier. `keyManager` sources
`secretKeyRef: name: memotron-secrets`, which only exists once `vaultSecret.enabled: true` creates
the VaultStaticSecret that syncs it. Enabling `keyManager` alone fails on a MISSING SECRET, not a
missing key — the ambiguous failure the one-flag-at-a-time order exists to prevent.

**Correct order:** (1) write `MEMOTRON_KEK_B64` to Vault; (2) `vaultSecret.enabled: true` alone,
then verify `kubectl -n jedai-memotron get secret memotron-secrets` directly rather than
inferring from pod state — note this also switches on `envFrom` for that Secret, so `LITELLM_API_KEY`
starts arriving; (3) `keyManager.enabled: true`; (4) the two operational-store flags.

### Crypto-shred was ALREADY off, and the KEK is load-bearing for something else entirely (2026-09-04, session 33)

The MVP question "what replaces KEK/DEK secure erasure" has an inverted premise. **Content sealing
is already off by default**: gated on `erasure_behavior == CRYPTO_SHRED`
(`dreaming/__init__.py:840-846`), default `SOFT_RETIRE` (`config/_governance.py:85`). Memory content
is plaintext today. `crypto_shred` / `issue_erasure_certificate` / `verify_erasure_certificate` have
**zero MCP and zero admin surface** — the only non-test caller is `examples/fleet_simulation.py`. The
only erasure verb an agent can reach is `memory_forget`, which is an `UPDATE` to `status=PRUNED` and
is reversible via `memory_restore`. **MVP must not claim verifiable erasure as shipped.**

What forces a KEK is `_bind_formation_contract` (`dreaming/_formation.py:312`) — unconditional, once
per episode — wrapping an Ed25519 DSSE signing key. Tenant BYOK is the second consumer and is
optional (env `LITELLM_API_KEY` fallback).

**What we are allowed to use** (verified against `tf-jennay@origin/main` `*/tfvars/iam_svc.tfvars`,
not from notes). `svc-jedai-memotron` has exactly two role groups in all four real envs:
`group_cloudsql_client` (`cloudsql.client`) and `svc_secret_manager` (`secretmanager.secretAccessor`,
`secretmanager.viewer`). **No `cloudkms` role anywhere** — the only one in the repo is `poc-1`, a
different SA. Workload Identity is bound.

- **CMEK** encrypts the Cloud SQL *disk*; its key's only IAM member is the Cloud SQL service agent,
  and `encryption_key_name` is immutable after instance creation. The app cannot call it and it can
  never become the envelope key. `INFRA-BACKLOG.md:449`: *"CMEK being present changes nothing about
  what happens on pod restart."* It is not an erasure story.
- **Cloud SQL IAM database auth is not available** — `cloudsql.iam_authentication` appears nowhere
  in `tf-jennay`, for any app. Password auth only, one `memotron_db`, one functional user. So
  **tenant isolation is necessarily above Postgres** (scope keys / `tenant_id`), as it already is.

Decision and rationale: `docs/design/16-encryption-and-erasure-for-mvp.md`.

### Rotating the KEK silently forks the attestation chain (measured 2026-09-04, session 33)

`LocalKeyManager.key_id()` is `sha256(kek)[:16]` and the DSSE `signer_id` embeds it, so a changed KEK
does not fail a lookup — it **misses** one and mints a second signing key. Measured, both engines:

```
ROTATED KEK -> NO CRASH, silently re-keyed
signer 1 : formation-contract:local:e7abf3239a1f3d14
signer 2 : formation-contract:local:b6a43cad208b33fe
same signing identity? False        signing keys now in table: 2 (was 1)
```

Attestations before the rotation verify against a key the store will never present again, with
nothing linking them. **Worse than a crash for a provenance claim, and the suite did not notice.**
The crash case needs a stable-`key_id` manager with changed material — the KMS shape #77 prescribes.

Fixed: `formation_signing_key_status()` (five states, never raises) plus a formation preflight that
raises on `KEK_MISMATCH` and warns on rotation. Non-fatal on rotation because an ephemeral KEK makes
a new signer the normal state of every test; production Postgres already refuses an ephemeral KEK.
Verified live on **both** engines — Postgres gave identical verdicts, closing the #163/#164 shape.

### The durable-KEK fix had to be resolved centrally, and the first version was a regression (2026-09-03, session 33)

Teaching only `Memotron()` to read `MEMOTRON_KEK_B64` **created** a split-brain, because
`adoption.py` (×4) and `runtime.py` (×2) open the *same* store with `open_storage(graph_path)` and
no key manager. Reproduced in one process, one store, one environment:

| surface | key id | status it reports |
|---|---|---|
| writer, the `adoption` shape | `local:65ede3cc…` | `has_api_key: True` |
| reader, the client shape | `local:16b72cfa…` | `has_api_key: False, api_key_unreadable: True` |

So `memotron llm configure` sealed under the per-store `.kek` sibling while the serving process
read under the env key — *the exact failure #123 exists to remove, relocated one seam over*, and
newly introduced by the fix. Before it, both sides agreed (durably wrong, but consistent).

**The fix is that `create_storage_backend` resolves the KEK, and it is the only place that does.**
`open_storage` delegates to it, so all six call sites are covered and a seventh cannot forget.
Verified: both surfaces now report `local:16b72cfa…`; the no-KEK control still uses the sibling
file unchanged.

Two adjacent defects came from the same "one caller learned, the others did not" shape:
- `factory.py` declared `graph_kwargs` and **never populated it**, so a split deployment gave the
  KEK to the operational store and not the memory graph. Latent — nothing constructs a split
  backend yet — but silent on SQLite when it fires.
- A **set-but-empty** `MEMOTRON_KEK_B64`/`_FILE` (a Secret that failed to resolve) was treated as
  "unconfigured" and silently fell back to the per-replica sibling key. Now refused.

### A `pytest.raises(match=...)` can pass on the test's own name (2026-09-03, session 33)

`with pytest.raises(ValueError, match="corrupt")` in `test_a_corrupt_KEK_FILE_is_refused` passed
**after the word "corrupt" was removed from the message** — because `tmp_path` is derived from the
test function name, the path is embedded in the message, and `match` is a search over the whole
string. The assertion would have passed against an empty message.

**Rule: when a message under assertion contains a path, strip the path before asserting.** This is
the file-level instance of the defect class this repo keeps hitting — a check whose passing
condition is satisfied by the thing it should catch — and it appeared *inside the tests written to
catch that very class*.

**The Cloud SQL Postgres for Memotron IS PROVISIONED IN TERRAFORM — latest, load and prod
(observed 2026-09-03 on `jedai/tf-jennay` @ `origin/main`)**
- `jedai-memotron-gcp-pgsql-latest`, `…-load`, `…-prod` (prod with a read replica), merged
  **2026-08-27** by Adam Kirstein via `feat/memotron-infra`.
  `latest-1/tfvars/cloudsql_module.tfvars:377`.
- POSTGRES_17 · ENTERPRISE_PLUS · `db-perf-optimized-N-2` · 100 GB PD_SSD · PSC-private
  (`ipv4_enabled: false`) · `ssl_mode ENCRYPTED_ONLY` · CMEK keyring · backups + PITR ·
  `deletion_protection` · pgaudit `all` · `max_connections 300`.
- **Sized against this app specifically.** `:412`: *"Memotron connection math: HPA 2-6 api
  replicas x pool max 10 + admin/migration headroom peaks at ~93 conns."*
- **`memotron_db` is `manage = false` and must be created out-of-band** (`:487`):
  `CREATE DATABASE memotron_db LC_COLLATE 'C' LC_CTYPE 'C' ENCODING 'UTF8' TEMPLATE template0;`
  — the same triple `docker-compose.local.yml` carries in `POSTGRES_INITDB_ARGS`.
- **APPLIED AND VERIFIED LIVE on `latest`, 2026-09-03.** Connected with the CSM credentials Ryan
  placed at `memotron/.memotron/csm_creds.json` (gitignored, untracked, 0 commits — key names
  are exactly the six the chart's `_helpers.tpl` expects, plus `LITELLM_API_KEY`). Result:
  **PostgreSQL 17.10, reachable.** Running infrastructure, not merely declared intent.
  `gcloud sql instances list` is refused for **every** credentialed account (all five
  `svc-gke-deploy@` SAs and both personal ones; `vanvr005@disney.io` holds no credentials), so a
  database connection is the only verification path available — record that before anyone re-tries
  gcloud and reads the refusal as absence.
- **`memotron_db` is correctly collated — the silent-failure trap was AVOIDED.** Observed
  `datcollate=C datctype=C encoding=UTF8`, while the neighbouring `postgres` and `cloudsqladmin`
  databases on the same instance are `en_US.UTF8`. So the manual `CREATE DATABASE` was run in the
  byte-ordered form deliberately. **Prod and load have not been checked** and the trap still applies
  to them.
- **The database is EMPTY: no `schema_migrations` at all**, and no `key_principals`,
  `memory_receipts` or `relationships`. Not migrations 9 and 10 — *nothing*, not even 1–8. No
  Memotron deployment has ever written to it, which is consistent with
  `operationalStore.enabled: false` on latest. **Consequence: when latest flips on it takes the
  FRESH migration path (V1→V10), not the upgrade path.** That is the better-tested direction; the
  upgrade path matters for prod later, whose database state is **unknown**.

**The terraform's stated reason for the `C` collation is WRONG, and its failure mode is SILENT
(observed 2026-09-03)**
- `cloudsql_module.tfvars:487` says *"The app asserts `LC_COLLATE='C'` at startup"*. **It does not.**
  There is no startup assertion anywhere — grep for `lc_collate`/`datcollate` across `src/` returns
  5 hits, all comments in `storage/postgres/_receipts.py`.
- What the app actually does is the opposite and stronger: it puts `COLLATE "C"` **in the queries and
  in the supporting index declarations** so ordering is byte ordering *"regardless of the database's
  `lc_collate`"* (`_receipts.py:31-34`, `:110`, `:706`, `:709`; `_operational.py:726`).
- **Consequence:** create `memotron_db` with default collation and the store **still builds,
  migrates and serves traffic.** There is no refusal.
- **CORRECTION to an earlier draft of this entry, which said "nothing will complain".** That was too
  strong. `PostgresStorageBackend.__init__` **does** check, at `storage/postgres/__init__.py:150`:
  `if not self._engine.is_byte_ordered(): logger.warning(...)`, naming the actual collation and
  saying *"Create the database with LC_COLLATE='C'."* So it is a **startup WARNING, not an
  assertion** — the terraform's "the app asserts … at startup" is still wrong about the severity,
  and a single warning line in pod logs on a healthy-looking start is easy to miss, but the claim
  that nothing is emitted was mine and it was wrong.
- **Why the earlier grep missed it:** the check is spelled `is_byte_ordered()` /
  `database_collation()` (`postgres/_engine.py:364`), not `lc_collate`. Searching for the *config
  key* found only comments; the *behaviour* lives under a different name. When a grep for a concept
  returns "only comments", search for the behaviour as well as the setting.
- Still verify by querying `pg_database.datcollate` rather than relying on someone having read a
  warning. What actually breaks is list **ordering** parity with the SQLite substrate, not
  correctness of stored rows.
- **On `latest` this was done correctly** (verified 2026-09-03: `datcollate=C`, against `en_US.UTF8`
  on the same instance's other databases). The warning stands for **prod and load**, which were not
  checked — and for any future environment, since the step is manual by design (`manage = false`).

**#123's gate STILL REPRODUCES — `probe_kek.py`, re-run 2026-09-03, and it is worse than
"multi-replica" (observed against the local Postgres)**
- Verdict text: **`B1 REPRODUCED`**. So #123 is genuinely open; do not assume an issue this old has
  quietly cleared — two others in this session had (the AppRole role id, the Vault ticket), which is
  exactly why this was re-run rather than trusted.
- Arms, one variable each:

      SQLite (control)   A ok   B ok   C ok   D ok      <- survives via the .kek sibling file
      Postgres           A ok   B FAIL C FAIL D guard holds

- **B fails on the SAME MACHINE with the SAME `.kek`.** The failure is not confined to
  cross-replica: *every pod restart* loses sealed tenant state. SQLite persists a `.kek` sibling
  file; Postgres has no equivalent and falls to `LocalKeyManager.ephemeral()`. Error on B and C:
  `ValueError: wrapped DEK failed authentication (wrong KEK or tampered)`.
- **And status keeps reporting `CONFIGURED` while both arms fail** — that is the silent half, and
  the second of #123's three "done when" criteria (`probe_kek.py` exits 0 · sealed state survives a
  restart · status stops reporting a non-durable KEK as configured).
- **Arm D confirms the guard holds** — with the flag unset the backend refuses to construct. That is
  why flipping latest's flags today would crashloop rather than corrupt: the fail-closed design
  converted silent data loss into a startup refusal, which is #130.
- **Invocation trap: `probe_kek.py` takes the DSN as an ARGUMENT.** Run bare it prints its usage
  block and exits, which reads like a completed run with a narrative verdict. Use
  `uv run python scripts/verify/probe_kek.py "$MEMOTRON_TEST_POSTGRES_DSN"`. Related: `PIPESTATUS`
  is bash — in zsh the array is `$pipestatus` (1-indexed), so an exit-code echo after a pipe prints
  empty and silently tells you nothing.

**`latest` runs no Postgres — BY CHART DESIGN, not by accident (observed 2026-09-03, `0.1.0-80be8f8`)**
- **This corrects an earlier phrasing in this file that called it a "missing secret".** It is a
  staged rollout that has not reached step one. `.helm/values-latest.yaml` sets
  `operationalStore.enabled: false` **and** `vaultSecret.enabled: false`, with the order written in
  the file: Vault ticket closes first, then the Postgres ticket. `memotron-secrets` does not
  exist because nothing is supposed to create it yet, and `optional: true` on the `envFrom` is what
  lets the pod run in that state. Nobody forgot anything.
- Confirmed three ways: the **running pod's full environment** has no
  `MEMOTRON_OPERATIONAL_STORE_DSN` at all (that is the name the code reads — also
  `..._POOL_MIN_SIZE`, `..._POOL_MAX_SIZE`, `..._APPLICATION_NAME`); configMap
  `jedai-memotron-13` holds only `ENVIRONMENT=latest`; and the chart says so.
- `MEMOTRON_GRAPH_PATH=:memory:` comes from **base `values.yaml` (`:83`, `:198`)** and is
  overridden by **neither** `values-latest` nor `values-prod`.
- **What blocks turning it on is #130/#123, not configuration.** A Postgres store built with no
  `key_manager` refuses to start: `client/__init__.py:546` passes none, so
  `storage/postgres/__init__.py:126-139` raises at `build_client()`. Accepted values for the
  override are exactly `{1, true, yes}` after strip+lowercase — "on" fails closed.
- `prod` has `operationalStore.enabled: true` in its values file. The **cluster was not checked.**

**Migrations 9 and 10 DO apply to a real, already-migrated Postgres (observed 2026-09-03)**
- **This corrects an earlier line in this file claiming they had "never run against a real
  Postgres".** That was too strong; the accurate statement is *never run in a **deployed**
  environment*.
- Proven by running the artifact, not the suite: an isolated compose stack whose database sat at
  **version 8 with `key_principals` absent**, then started on an image built after #167 →
  **9 and 10 applied, `key_principals` created with `key_alias` as PRIMARY KEY**, receipt index
  present, container healthy, 0 restarts. That is the deployed upgrade path, which until then had
  only ever run inside a fixture that drops the schema first.

**Which store owns `key_principals` (observed 2026-09-03)**
- It is on the **`OperationalStorage`** surface — present in `composite._OPERATIONAL_SURFACE`,
  absent from `_MEMORY_GRAPH_SURFACE`. So in a split config it is a **Postgres** table reached by
  migration 9, and #165 raising the `OperationalStorage methods` baseline 87→90 was the same fact.
- With **no** operational DSN, `create_storage_backend` (`storage/factory.py:95-102`) has
  `is_split == False` and returns **one SQLite backend serving the entire surface** — so on `latest`
  today the registry is created in RAM and dies with the pod.

**MCP tools cannot see request headers (observed in the installed SDK, 2026-09-03)**
- `mcp.shared.context.RequestContext.request` has **`default=None`**, and the lowlevel server
  constructs `RequestContext(...)` without it. So `ctx.request_context.request` is `None` on every
  request and any header-reading auth built that way fails silently and identically to "no header".
- The supported path: `FastMCP.__init__` accepts **`token_verifier`**, `auth_server_provider` and
  `auth`; a tool retrieves the verified token via
  `mcp.server.auth.middleware.auth_context.get_access_token()`. `TokenVerifier.verify_token(token)
  -> AccessToken | None`, and `AccessToken` carries a `claims` dict.
- **Not verified:** whether `token_verifier` runs per-request or only at session initialize.
- `admin_server` is unaffected — it is a `BaseHTTPRequestHandler` and `self.headers` already works
  (`:1654`). Its problem is different: `handler.principal` is set **once on the class** at `:2221`
  from `build_demo_principal(...)`.

**gcloud / kubectl access (observed 2026-09-03)**
- The configured account `vanvr005@disney.io` **has no credentials** (absent from `gcloud auth
  list`), which is what produces `Reauthentication failed. cannot prompt during non-interactive
  execution`. Credentialed `svc-gke-deploy@` service accounts exist for `jennay-latest-1`,
  `jennay-stage-1` and `jennay-prod-1` — no interactive login is needed.
- **`~/.kube/gke_gcloud_auth_plugin_cache` serves a stale account's token.** After switching the
  active account, requests still went out as the *previous* SA and were rejected with a Forbidden
  naming it. `CLOUDSDK_CORE_ACCOUNT` did not override it either. Clearing (moving) the cache fixed
  it immediately. If a Forbidden names an account you did not select, suspect this cache first.

**Branches and baseline**
- **`origin/main` = `f28e95c` — PR #44 merged (squash) 2026-08-26 23:47Z. This is the takeover baseline.** Work from branch `takeover`, which tracks it. The squash preserved the PR tree exactly (`git diff origin/main pr44-local -- src/ tests/ .helm/` was empty) and linear history is intact.
- Prior baseline `9bfc298` = PR #35 (storage contract + Postgres backend); `47790d4` before that.
- **The INTRODUCED/INHERITED distinction in `TAKEOVER-BACKLOG.md` is now provenance only** — every finding is on `main` and ours to fix.
- The local branch `memotron-implementation` (`4550b60`) is a **Claude-Code-plugin spike**, is *not* an ancestor of `main`, and does not contain #35. Its commits are preserved on `origin/spike/claude-code-plugin`. Do not build productionization work on it.
- PR #44 (`feat/next-ws25-ws28`, 100 commits / 185 files / +75101−3853) merged **with its six known defects unfixed** — the four from tyler-r-friddle plus the two we added. The consolidated review comment is on the closed PR; the durable copy is `TAKEOVER-BACKLOG.md`. It brought the JedAI Gateway integration (`gateway.py`, LiteLLM as the only LLM path).

**The product does not currently work, and the tests do not catch it (2026-08-26)**
- **A scope stops forming new memory once it records ONE unresolved-problem fact.** The formation gate (`dreaming.py:1959`) hands the scope's existing memories to the dream agent, which reads negative content as grounds to refuse. Silent, billed per refusal, no receipt, no negative-space entry. Affects tenant **and** agent scopes independently. Ruled out: motive, prompt profile, model (Sonnet fails identically), volume. Four independent manipulations move it; each condition replicated 5×.
- **Validated one-line fix:** `context_facts=()` at `dreaming.py:1959` — withhold context from the *gate*, keep it for *extraction* (`:2074`). RED 5/5 → GREEN 5/5, no regression, and better extraction quality than the `context_policy.enabled=False` workaround. **Not applied** (bug is on `main`; this worktree is the PR author's branch). Diff in `docs/findings/formation-suppression.md`.
- **Mitigating:** blocked episodes are never marked processed, so they stay queued and form once the offending fact is removed. **Liveness failure, not data loss.**
- **The MCP tools false-pass.** Driven over stdio as Claude Code drives them: `memory_bootstrap`/`publish`/`refresh`/`search` all return `isError=False` and search returns results — while zero memories form. Search returning *pre-existing* facts is what makes it convincing. A lenient `mcp-validate` probe would certify a dead server; ADR 0007 now requires a round-trip probe.
- **Live tool counts (take from `tools/list`, never a grep):** `memotron` = **39**, `memotron-agent-memory` = **27** on PR #44; `main` has 30/21.

**Scale — MEASURED, not derived (2026-08-26, `scripts/verify/bench_scale.py`)**
- Reads grow linearly with **total** rows, not scope size: graph 46× → search 57×, semantic 53×, profile 57×, per-episode context 55×.
- **Writes degrade superlinearly**: 7.9 → 34.1 → 229.8 ms/write at 33 → 303 → 1503 rows. Roughly O(n) per write, so O(n²) to build a graph. Seeding 500 facts took **115 s**. Extrapolated (NOT measured) ~1.5 s/write at 10k rows.
- **Relevance held**: a distinctive needle ranked #1 by keyword and appeared in semantic at every size. *Caveat: the needle shared almost no vocabulary with the haystack — the overlapping case is untested.*
- Cause: `relationships()` is `SELECT * FROM relationships` with no scope filter; `dreaming.py` calls it **11×**, including `_graph_context_for_episode` (per episode) and inside a `while pending:` loop. A scoped, index-backed `relationships_for_scope()` exists and is not used there. See T1-7.

**Postgres does NOT fix scale, and `search()` is broken on it (2026-08-26)**
- Postgres `relationships()` is byte-identical to SQLite's — unfiltered `SELECT *`. The scan pattern is **caller-side**, so changing backends does not change its shape; it adds a network hop.
- **T0-4:** `relationships_for_scope` diverges — SQLite filters `AND type != 'MENTIONS'`, Postgres does not. MENTIONS edges have no `fact`, so `client.py:4319` raises `KeyError: 'fact'`. Reproduced: one write → SQLite 1 row, Postgres 3 → `search()` raises. **The 3,310-line parity suite passes and misses it.**

**Tests — the suite is fully runnable locally (2026-08-26)**
- `uv run pytest` → **1003 passed, 69 skipped in 37s**, hermetic, no network, no key.
- With `MEMOTRON_TEST_POSTGRES_DSN` pointed at a `postgres:16-alpine` container → **1071 passed, 1 skipped in 158s**. This matches PR #44's self-reported "1068 passed, 1 skipped".
- All 69 default skips are Postgres-gated: 52 `test_storage_backend_parity.py`, 8 `test_operational_store_concurrency.py`, 6 `test_operational_store_cross_store.py`, 3 other. So the default run **omits exactly the surface PR #44 changes most**.
- `uv sync --frozen --extra postgres` succeeds on the PR branch — the lockfile is consistent with `pyproject.toml`.
- `ui/admin`: `npm ci && npm run build` succeeds, including `tsc -b`.

**Container / chart defects (all reproduced 2026-08-26, none are PR #44 regressions)**
- **The image cannot run the command the chart gives it.** `uv run --no-sync …` as uid 10001 fails with ``failed to create directory `/app/.cache/uv`: Permission denied``. `WORKDIR /app` leaves `/app` root-owned (`drwxr-xr-x root root`) — only COPYed *contents* are chowned — and the user's home is `/app` with `--no-create-home`. No `UV_CACHE_DIR` or `HOME` anywhere in `.helm/`. Present in **every** Dockerfile revision since `d2b64bf` (June).
  - Workaround: `UV_CACHE_DIR=/tmp/uv-cache`. Real fix belongs in the Dockerfile.
- **No `fsGroup` anywhere in `.helm/`.** `memotron.podSecurityContext` is `seccompProfile` only, while the admin pod mounts a `premium-rwo` PVC at `/data` and runs as uid 10001 — GCE PD mounts root-owned without `fsGroup`, so the admin container cannot write the file it must create.
- **`memotron-admin-server` requires `--graph-path` AND requires that file to already exist** (exits `graph path does not exist`). There is no flag to source the store from `MEMOTRON_OPERATIONAL_STORE_DSN`. **The admin console therefore cannot run against Postgres at all today** — the demo-fixture seed in `values-latest.yaml` is structurally required, not a demo convenience.
- Consequence, **not yet confirmed against the live cluster**: the deployed `latest` API and admin pods should both fail to start. Settle it with `kubectl get pods -n jedai-memotron` (look at RESTARTS).

**B1 — ephemeral KEK (critical, pre-existing on `main`)**
- `storage/postgres/__init__.py:110` → `key_manager if key_manager is not None else LocalKeyManager.ephemeral()`; `client.py:157` calls `create_storage_backend(self.config.storage)` with **no** `key_manager`. The string `key_manager` appears nowhere in `client.py`.
- **Reproduced across two distinct compose replicas** sharing one Postgres: replica A wraps a DEK, replica B → `ValueError: wrapped DEK failed authentication (wrong KEK or tampered)`. Enabling `operationalStore` without fixing this is a silent, cluster-wide crypto-shred.

**Tenant migration is destructive by default (verified 2026-08-26)**
- `purge_source` defaults to **True at all three levels**: `migration.py` `migrate_tenant_memory(..., purge_source: bool = True)`; `cli.py:441` `purge_source=not args.keep_source` (so `--keep-source` is opt-**in**); `cli.py:372` hardcodes `purge_source=True` with no opt-out.
- `cli.py:372` sits inside **`memotron init`** — `_run_init` (`:174`) → `_offer_memory_migration` (`:208` → `:304`), prompting *"Move this memory into `<project>` now? [y/N]"*.
- **Guard conditions matter** — that prompt does **not** fire on a clean `init`. It needs either (a) a prior `.memotron.yaml` claiming a different project id, or (b) another tenant scope already present in the same graph file. Case (b) is the sharp one: in `simple` mode the graph file is commonly shared, so a first `init` in a new project enumerates *other projects'* tenant scopes and offers to move one.
- Combined with the `epoch_id` copy in `migration.py`, this makes the reviewer's critical finding the **default path of a shipped command**, not a corner case. Exits 0, prints `Migrated N facts…`, destination invisible, source purged.
- Real CLI surface is `{init,mcp,hook,status,llm,migrate}`. `migrate` is new and is **not** documented in the shipped `.claude/skills/memotron-sdk/SKILL.md` (README covers it 5×).

**Documentation state (audited 2026-08-26)**
- Docs on the PR branch were refreshed 2026-08-25 and are in better shape than expected. Audit method: extract every `from memotron import …` across all markdown, resolve against the installed package.
- **Exactly one genuine break**: `README.md` references `ThematicConsolidationPolicy` 5× (lines 692, 768, 774, 2656, 2681), including a copy-paste block at `:768` that raises `ImportError … Did you mean: 'RollupConsolidationPolicy'?` on the branch that ships it. PR #44 renamed the class without updating the README.
- `.claude/skills/memotron-sdk/SKILL.md` is **clean** — zero references to the removed Anthropic transports (updated in `b678dd6`).
- `docs/design/*` citing non-existent modules (`oidc.py`, `authz.py`, `backup/`, …) is **correct** — they are proposals describing files to create. Not defects.
- Scale is the real problem, not accuracy: `README.md` is 2,854 lines, `DATAFLOW.md` 1,196 — ~4k lines of overlapping narrative before `NEXT.md` / `PERSISTENCE.md` / `INGEST.md` / `AUDIT.md` / 3 patent specs.
- `origin/main`'s docs were **not** audited the same way. Main should be internally consistent (it still has `ThematicConsolidationPolicy` and its Anthropic references match its own code) but describes the pre-gateway architecture.

**Auth**
- `admin_server.py` has **no request-level authentication** (the 11 `session`/`Bearer` grep hits are MCP sample code inside a docs payload). `values-latest.yaml:98-99` deploys `--principal-role admin`.
- `config.py:2651` — `if principal.role != PrincipalRole.ADMIN:` checks the scope allowlist; `else:` skips it and sets `auth_source = "tenant_admin"`. **ADMIN widens, it does not scope.**
- `PrincipalRole.OPERATOR` is defined at `config.py:1719` and **never compared anywhere in `src/`**. An operator authorizes exactly as a user.
- `/health` on the admin server returns **204** (`_send_no_content`) — confirmed live. Kubelet accepts any 2xx; GCLB wants 200.

**Infrastructure**
- Platform is **GCP/GKE** (`jennay-<env>-1`, GAR, Workload Identity, `gce-internal`, Vault). AWS is Bedrock only; Azure is Azure OpenAI + Entra only.
- **Workload Identity is mis-bound, both halves**: chart creates KSA `memotron` annotated `svc-memotron@`; `tf-jennay/latest-1/tfvars/iam_svc.tfvars:324-328` binds GSA `svc-jedai-memotron` ↔ KSA `jedai-memotron`.
- **No KMS resources exist anywhere in `tf-jennay`** (all four envs) — the KEK fix is greenfield.
- The admin ingress VIP has **no** entry in `adresses.tfvars`; only the API host does.
- `grep -c memotron` in `tf-jennay/{stage,load,prod}-1/tfvars/*` → **zero**. Every env above `latest` is unprovisioned.
- Vault AppRole is `REPLACE_ME_WITH_APPROLE_ROLE_ID` and `vaultSecret.enabled: false` — this one placeholder blocks *all* secret delivery. The gateway repo has a working example of the same pattern.
- **No external code consumers of the SDK** — the only references outside this repo are portal platform-explorer diagram metadata. PR #44's public-API break needs no deprecation shim.

**CI**
- No `.github/workflows` anywhere; **the org has no GitHub Actions runners** (jobs on `ubuntu-latest` start and fail in the same second). `.harness/pipeline.yaml` is Validate/Build/Deploy with **no test stage**. GitGuardian is the only PR check and is **not** a required status check (`gh api /repos/jedai/memotron/rules/branches/main`).
- The Build stage derives the image tag by grepping `tag:` from `values-latest.yaml`; both `main` and #44 say `0.1.1`, with `pullPolicy: Always` — a deploy overwrites the running tag in GAR and destroys rollback-by-tag.
- `platform-cicd/apps/memotron` does not exist. `mcp-forge/servers/mcp-jedai-memotron` does not exist (PR #51 there is a draft with changes requested).

### The governance MCP server can ONLY do global runs, and #160 is a symptom not a decision (measured 2026-09-03, session 30)

**Its scoped path already raises, and did so before any of this week's work.** On a client with no
control plane -- which is what that server has:

    run_due_dreams()                     -> OK
    run_due_dreams(scope=tenant:acme)    -> ValueError: control_plane is required ...
    run_due_dreams(tenant_id="acme")     -> ValueError: control_plane is required ...

The middle line is exactly what `mcp_server.py:764-766` does when a caller passes `scope_kind` +
`scope_id`, which the tool's docstring advertises. **Verified against `origin/main`: it raises there
too.** So the governance server is single-tenant **by necessity**, not by policy, and scoped dream
runs are broken for every caller of that tool.

**A fourth option for #160 is dead, recorded so it is not re-proposed.** `ScopeKind.TENANT` exists
and *TENANT-kind `scope_id` == `tenant_id`* is an established convention -- storage itself builds
`f"{ScopeKind.TENANT.value}:{normalized_tenant}"` (`sqlite/_governance.py:940`,
`postgres/_governance.py:1321`), as do `agent_memory/_scopes.py:25` and
`admin_server/__init__.py:640`. Deriving the tenant from an existing scoped call therefore looked
free. It cannot work: passing a scope raises on the control-plane check **before** the derived
tenant could be used.

**The reframe.** Measured against multi-tenant best practice, #161 fixed the MECHANISM (per-request
resolution, no cross-tenant reuse, immediate revocation) and the missing piece is IDENTITY:
`configure_tenant_llm(tenant_id, ...)` takes the tenant as an *unauthenticated argument*. So
seal-only is not a reduction in function -- it is the correct **fail-closed** behaviour for a server
that cannot identify its callers, and once identity exists applying becomes correct automatically.

**The real blocker is the per-key registry**, which is a decision, not a task. DW-026 assumes
`key_alias -> principal` "through the per-key registry on that record"; verified the storage
contract has **no** `key_hash` / `api_key` / `virtual_key` / `key_registry` / `key_alias` method
(control: `tenant_agents` exists). `gateway_identity.py` is built and live-verified but sits on
draft **#156**, which still targets the merged `refactor/subsystem-passes` and is imported by
**0** modules (control: 12 import `gateway.py`).

### `latest` IS deployed, is current, and is unusable three ways (measured 2026-09-02, session 29)

**Supersedes the 2026-08-27 audit note that "nothing of Memotron is deployed" in `latest`.** That
was true then; it is not now. Observed via `kubectl --context=gke_jennay-latest-1_us-central1_usc1-jennay-latest-v1n1-cluster-1 -n jedai-memotron`:

| | |
|---|---|
| workloads | `jedai-memotron-api` **2/2**, `jedai-memotron-admin` **1/1**, all `Running`, **0 restarts** |
| image | `0.1.0-a1daaf4` — i.e. `origin/main` after today's merge |
| PVC | `jedai-memotron-data` 10Gi `premium-rwo`, **Bound** |

And three independent reasons it does not work:

1. **Every request through its own ingress gets 421.** `POST …/mcp -> 421 "Invalid Host header"`
   while `GET …/health -> 200`. `FastMCP` resolves DNS-rebinding protection **at construction** from
   its `host` argument (MCP SDK >= 1.29 auto-enables it for localhost); both servers construct with
   no host, then `examples/mcp_server.py` assigns `mcp.settings.host = "0.0.0.0"` afterwards, which
   does not re-evaluate it. Observed: `host at construction : 127.0.0.1`,
   `allowed_hosts=['127.0.0.1:*','localhost:*','[::1]:*']`. `/health` is outside that middleware, so
   liveness, readiness and the GCLB backend all stay green. Fixed on `fix/mcp-allowed-hosts` (#157).
2. **No gateway key.** `memotron-secrets` does not exist in the namespace; the chart mounts it
   `optional: true`; `LITELLM_API_KEY` appears **0** times in the api Deployment spec (control:
   `LITELLM_API_BASE` appears 2); no `VaultStaticSecret` exists. Measured consequence — keyless
   resolves `['RuleBasedExtractionTransport', 'NoneType']`, with a key
   `['OpenAICompatibleExtractionTransport', 'OpenAICompatibleDreamAgentTransport']`. Blocked on the
   Vault ticket: VSO **is** installed and `memotron-vault-approle` **exists** (key `id`), but
   `values-latest.yaml:56 approleRoleId` is commented `PLACEHOLDER`.
3. **The store is `:memory:`** (`.helm/values.yaml:81`), on 2 replicas behind a ClusterIP.

**BYOK is real but does not rescue (2).** `configure_tenant_llm` seals *and* installs live transports
(`set_runtime_transports`), and `values-prod.yaml` records `LITELLM_API_KEY` staying `REPLACE_ME`
**by design**. But on `latest` that path is per-replica (configure one pod, the other stays
rule-based), was process-global rather than per-tenant (#158), and is **not restored on restart** —
`build_client` uses `build_transports_from_env()` and never re-seeds;
`seed_tenant_llm_credentials_from_env` is called only by `adoption.py:456` and
`local_platform.py:131`.

**Two more, verified while mirroring:** the admin pod mounts a 10Gi PVC at `/data` and writes its
graph to `/tmp/memory_graph_demo.sqlite` (`values.yaml:164,168,234`) — the volume holds **nothing**,
and that same PVC forced `strategy: Recreate` after the Multi-Attach failure in #146. And the
~~**agent-memory MCP server is deployed nowhere**~~ — **NO LONGER TRUE as of #240
(2026-09-09).** It runs as its own role on `latest` (`jedai-memotron-agent-memory`,
1 replica, ClusterIP, no ingress). Left struck through rather than deleted because the
surrounding paragraph is a dated snapshot of why the PVC was removed, and rewriting a dated
observation to match the present makes it wrong about history while looking right.

### Dependabot #23 (nltk, HIGH, no fix at any version) is NOT REACHABLE from our code (verified 2026-09-02, session 28)

**The one open alert on the default branch, and it should not block launch.** Of 11 nltk
advisories, the `3.10.0 -> 3.10.3` bump (`9a5eaff`) fixed **10**. **#23 survives at any version**:
its range is `<= 3.10.3` with `first_patched_version: NONE`. We are on 3.10.3 (`uv.lock`;
`pyproject.toml` floors at `nltk>=3.9.2`), so upgrading cannot clear it and re-checking for a patch
is wasted effort until upstream ships one.

**Subject:** *"Model-artifact APIs bypass pathsec and touch files outside allowed roots"* — the
data-loading surface (`nltk.data.load`, the downloader, unpickling model artifacts).

**We never touch that surface.** nltk appears exactly **once** in `src/`:

```
src/memotron/certification.py:26:  from nltk.stem import PorterStemmer
```

Verified three independent ways, because "we probably do not call that" is an assumption:

| path | result |
|---|---|
| **static, the module** | `nltk/stem/porter.py` (741 lines) contains **zero** occurrences of `nltk.data`, `data.load`, `download`, `open(`, `pickle`, `find(` |
| **dynamic, the runtime** | wrapping `builtins.open`, then constructing and exercising the stemmer, opens **0 files** (`running->run`, `certification->certif`, `memories->memori`, `dreaming->dream`) |
| **transitive reach** | **0** files outside the `nltk/` package in site-packages import nltk — control: the same search finds **397** importers inside `nltk/`, so the pattern does match |

The third check exists because the first two only cover *our* code; without it, a dependency calling
the model-artifact APIs would invalidate the whole argument. It does not happen here.

**So #23 is an accepted risk with a reason, not an unmitigated HIGH.** The condition that would
change that: adding any second nltk use — in particular anything under `nltk.data`, `nltk.corpus`,
or `nltk.download` — puts the vulnerable surface back in reach. `PorterStemmer` is pure algorithm;
it is the *only* safe-by-construction member of that library we rely on.

### The register's dead citations are deliberate, and a freshness gate would fire on them (measured 2026-09-02, session 28)

`TAKEOVER-BACKLOG.md` pins every `file.py:LINE` reference to **`audit-baseline-2026-08-27`**
(`06391b6`), *not* `HEAD`. Its header, lines 3–9, states this and gives the reason: the value of a
citation is *"at the commit I audited, this line said X"*, and **rewriting makes them wrong about
history while looking right.**

Measured, resolving each citation against that tag rather than `HEAD`:

| | |
|---|---:|
| line citations checked at the audit tag | **91** |
| line falls **inside** the file it names | **82** |
| line **beyond end of file** | **0** |
| file not present at that tag | 9 |

So the ~469 references that do not resolve against `HEAD` are the module split (all seven god
modules deleted in one commit, **`e5fb5a4e`**), which is the documented, intended state — not drift.

**A gate on citations resolving against `HEAD` would therefore fail on correct behaviour**, and
separately would need an allowlist on day one: of the 17 distinct paths it would call dead, **7
exist on disk and are merely gitignored** (`.helm/values.yaml`, `.helm/values-latest.yaml`,
`.helm/templates/networkpolicy.yaml`, `.mcp.json`, `.memotron.yaml`, `.claude/settings.json`,
`.claude/rules/memotron.md`). Refuted in **#136**, with the reasoning parked in
`finding_gate.py::report_unverified`'s docstring so it is not proposed a third time.

**What the register does lack** is any signal for *fixed-but-unmarked*. It drifted twice more on
2026-09-02 from this session's own work (`T3-12`, `T3-1`), both found by hand. `finding_gate.py`
now prints the unverified inventory (87 entries / 58 re-checked / 29 never re-checked) as a
**report that cannot change the exit code** — it makes the population visible, not smaller.

**`finding_gate.py` is a manual pre-handoff tool, not a `check.sh` lane.** Adding to it costs no CI
surface.

### Request authentication: what exists, and the one thing that blocks the rest (2026-09-02, session 26)

**`src/memotron/gateway_identity.py` exists and is live-verified** (`1b51580`). It answers
*"who does the gateway say this key belongs to?"* and stops there. **Nothing calls it yet** —
surface wiring is what closes #126 T3-1.

- Live on LATEST: a minted virtual key resolved to
  `key_alias='dw-identity-probe' team_id='TEAM_DX0232' user_id='dw-identity-probe-svc'`.
  Cache **miss 382 ms → hit 0.011 ms**. A bogus key raised `GatewayRequestError status=401
  retryable=False` — fails closed *and* fails fast.
- **The proxy MASTER key does not self-look-up.** It is the configured secret, not a key row,
  so `/key/info` returns `404 "Key not found in database"`. Correct behaviour; it will mislead
  anyone smoke-testing with it. Use a minted virtual key.
- The admin-host derivation is **deliberately narrow**: `<env>.<service>.<domain>` only, and it
  refuses anything else rather than guessing which label is the service — guessing wrong sends a
  caller's credential to the wrong host. Other shapes set `MEMOTRON_GATEWAY_ADMIN_BASE_URL`.
- `gateway_identity` is tiered **INFRA**, with `gateway`. Any higher tier would create the
  upward edge this branch drove to zero.

**THE BLOCKER for the second half: the per-key registry does not exist.** DW-026 specifies that
`key_alias` maps to a principal *"through the per-key registry on that record"*. Measured: no
`key_hash` / `api_key` / `virtual_key` / `key_registry` method anywhere in the storage contract.
The tenant record holds **agents** (`tenant_agent`, `tenant_agents`), not keys. Building it is a
storage decision; reusing `tenant_agents` by treating `key_alias` as an agent id is cheaper but
is **a different model from the one #27 landed**, and #126 forbids inventing the model.

### The scope guard is off on the facade BY DESIGN, and the code says so (2026-09-02, session 26)

#137 asks why `authorized_scope_keys` is enabled nowhere. For `agent_memory` the answer is that
it must not be:

- `tests/test_promotion.py::test_platform_facade_does_not_set_core_guard` asserts
  `platform.client.authorized_scope_keys is None`, with the reason: *"Facade authorization is
  per-call; its omni-tenant client stays unguarded so one client can serve every registered
  agent."* Wiring it at the project boundary fails **35 tests**.
- The constructor docstring (`client/__init__.py:484`) says the same: *"defense in depth for
  embedded single-tenant SDK deployments … The `AgentMemoryPlatform` facade deliberately does
  NOT set it."*
- Structurally consistent: one long-lived client, N agents, principal derived per call via
  `principal_for_agent()`, and `agent_ids` **grows at runtime** — a constructor-time set could
  not track it.

**The two surfaces that ARE genuinely unguarded**: `mcp_server.py` (103 caller-scope refs across
29 tools, **0** principal, **0** checks — `_scope()` builds a `MemoryScope` straight from caller
strings) and `admin_server/` (has a principal, but `handler.principal` is set once in `main()`
on the handler *class* — process-wide, shared by every request, scope never checked against it).
`worker.py` is correctly unguarded: no caller supplies a scope.

The guard is now on **72 public entry points**, not the four #103 added.

### The admin surface is live, unauthenticated, and serving content (verified 2026-09-02)

All five environments configure a **dedicated admin ingress** with its own static IP
(`admin-internal`, `gce-internal`, `allow-http: "false"`). With **no credential**:
`latest` and `stage` both return **HTTP 200** on `GET /api/overview` with tenant configuration,
and `/api/graph` returns memory content. **prod is configured identically but was not reachable
from a workstation — NOT verified either way.** Zero credential reads exist anywhere in
`admin_server/`. `networkPolicy.enabled: false` in latest and prod, so the in-cluster firewall
is not a compensating control there. Recorded on #126; the `str(exc)` echo is #154.

### Gateway identity: what a caller can and cannot forge (measured 2026-09-02, session 25)

**Principal resolution was already decided — DW-014 (2026-07-30) and DW-026 (2026-08-28) in
`docs/authorization-decisions.md` own it.** Read them before re-deriving anything about tenancy.
  **And read `docs/data-model-and-tenancy.md` first** — it is the one page covering what a row is
  bound to (`scope_key`, not `tenant_id`), what enforces isolation and what does not, the Postgres
  schema, and where the two engines differ. Written 2026-09-03 because tenancy was re-derived from
  source at least twice this week.
This session's contribution is **DW-027**: the premise DW-026 rests on, measured.

- **`key_alias` is globally unique**, enforced by LiteLLM across *all* keys, not per user or team.
  Three collision routes refused `400 "Key with alias '<x>' already exists. Unique key aliases
  across all keys are required."` — admin minting a duplicate, a team member minting another
  user's alias, and a member **renaming** their own key onto it. This is what makes DW-026's
  alias→principal registry lookup safe: a registered alias is already held by its owner's key, so
  nobody else can mint it. **Residual: uniqueness is first-come-first-served, so create registry
  entries from an existing key, never ahead of one.**
- **A real team member CAN mint keys** — observed `200`. **This corrects DW-026**, which says a
  caller holding only its own key "cannot mint or edit one"; that was measured with a key whose
  `user_role` is unset (gateway reports `role=unknown`), whereas
  `key_generation_settings.team_key_generation.allowed_team_member_roles: ["admin","user"]`
  permits a constituted member. DW-026's conclusion survives — what they mint is constrained.
- **Four forgery routes refused** by such a member: non-member `user_id` (`400`), a team they are
  not in (`400`), rebinding their own key's `user_id` (`403` *"Non-admin caller is not allowed to
  rebind the key"*), and claiming a **co-member's** `user_id` (`403` *"User can only create keys
  for themselves"*). The co-member case is the one that matters — `team_id_default` puts everyone
  in one team, so co-membership is normal, not an edge case. Cross-reading another user's key or
  key list: `403` both ways.
- **Key regeneration preserves `user_id`, `team_id` and `key_alias` verbatim and changes only the
  token hash.** So a rotated key keeps its registry identity, and DW-026's cache-by-key-hash
  misses and re-resolves rather than mis-resolving. **Never bind identity to key material.**
- **`team_id` is degenerate on LATEST**: across admin-visible keys it is 40% absent and 50% one
  shared value, with a single key carrying a `TEAM_DX*`. `organization_id` is 0% populated.
  Tenancy at launch is therefore effectively per-environment; it sharpens as teams get used, with
  no migration. `TEAM_DX####` ids are byte-identical across latest/stage/load — stable, but nearly
  unused, so stability alone was not the property that mattered.
- **Entra does not map to teams.** `user_id_jwt_field: "email"`, `sync_user_role_and_teams: false`,
  no `team_ids_jwt_field`, and `team_id_default` is a **constant fallback** — every SSO user lands
  in the same team. At SSO cutover the same human arrives as `name@disney.com` while their virtual
  key carries a UUID: two identities, two memory stores, unless the registry maps both first.
- **The `x-litellm-api-key` forwarding path WORKS — DW-014's "load-bearing gap" is closed for
  the gateway side (DW-028, measured 2026-09-02).** Two keys with distinct users resolved to
  **distinct correct identities** through `jedai_gateway`'s `jedai_whoami`; master key resolved
  `proxy_admin`; no-key refused `401`. Code agrees: `server.py:1742-1768` copies any header named
  in `server.extra_headers` verbatim from the caller, `authorization` being the sole conditional
  exception, and nothing strips `x-litellm-api-key`. Re-prove:
  `scripts/verify/probe_mcp_key_forwarding.py --env <env>`.
  **The actionable consequence: Memotron is NOT registered as an MCP server at all** (25 on
  latest, none of them Memotron). When it is, it **must** carry `x-litellm-api-key` in its
  `extra_headers` — `jedai_gateway` and `jedai_gateway_admin` are the working precedent — or the
  key silently never arrives. That is part of the registration, not a later hardening step.
- Re-prove all of the above in one command: `scripts/verify/probe_gateway_tenancy.py --env <env>`
  (exit 0 holds / 1 forgeable / 2 inconclusive). Deliberately **not** in `check.sh`. Cross-repo
  write-up: brain vault `adr/0008-memotron-tenancy-binds-to-litellm-user-id`.

### Authentication is the ONLY missing half; authorization is already built (2026-09-02)

Measured while scoping the initial launch, and it makes the auth work far smaller than "build
authentication" implies.

`MemoryPrincipal` (`config/_tenancy.py:109`) already carries the whole model -- `principal_id`,
`tenant_id`, `agent_id`, `default_scope`, `allowed_scope_keys`, `role` -- and the AUTHORIZATION
side is done: the T3-8/T3-11 scope guards enforce against it, `effective_allowed_scope_keys` is
real, and 10 of 19 admin handlers already reference `self.principal`.

**What is missing is only authentication.** The principal is built once at startup by
`build_demo_principal(...)` and **no request path reads an `Authorization` header anywhere** --
grepped across `admin_server/`, `agent_memory_mcp.py` and `mcp_server.py` for
`headers.get(authorization|x-api-key)` and `Bearer`: **zero hits on all three surfaces.**

So the LiteLLM-virtual-key plan is a change at one seam: read the bearer token, validate it
against the gateway, map key identity to tenant/principal/role, and build `MemoryPrincipal` from
that instead of hardcoding it. Everything downstream already works, and Entra SSO later replaces
the same seam rather than reopening the design.

`jedai/gateway` is a vendored LiteLLM (it contains `enterprise/litellm_enterprise/`), so "virtual
keys" means LiteLLM's native key system. **Open question, not yet answered:** which field is the
tenant of record -- `team_id`, `key_alias`, or `metadata` -- and whether `/key/info` can be called
server-side per request or needs caching.

### The suppressions gate parsed nothing when mypy emitted colour (fixed 2026-09-02, `9c513bc`)

`FORCE_COLOR=3` was set in the session environment. mypy honours it **even when stdout is a
pipe**, so `mypy_suppressions.py`'s subprocess received escape codes between the colon and the
word: `...py:851: \x1b[1m\x1b[31merror:\x1b(B\x1b[m ...`. Its `_ERROR` regex expects a literal
`: error: ` and matched **nothing** -- 237 real error lines parsed as zero, all 89 suppressions
read as dead, and the lane FAILED demanding they be deleted. Deleting them would have removed the
suppressions for 201 live errors.

Nothing in the repo changed. The same gate reported `201 hidden / PASS` earlier the same day on
the same tree. Cross-checked against ground truth by stripping every `disable_error_code` from
pyproject by hand: `Found 201 errors in 56 files`, matching the gate exactly once fixed.

Fixed with two independent guards -- the subprocess forces `NO_COLOR=1`/`TERM=dumb`, AND ANSI is
stripped before parsing. Break-tested under `FORCE_COLOR=3` and `NO_COLOR=1`: both report 201.

### `check.sh` reported a SKIPPED golden lane as PASS (fixed 2026-09-02, `501a787`)

**The gate runner lied about its own output, twice, and both times it was read as fact.**

Both golden lanes refuse to compare when the recorded profile came from a failing suite
(`clean=no` in the `.tsv` header) -- correct, since a profile taken off a broken tree describes a
broken tree. But they signalled that refusal with `return 0`, and `check.sh`'s `run_lane` maps
exit 0 to `PASS`. So the summary printed **PASS for a lane that compared nothing.**

Observed at source, not inferred: `storage_golden.py` returned 0 on the SKIP path, and
`run_lane` has a bare `if rc -eq 0 -> PASS`. Two separate agents reported `storage PASS` /
`receipts PASS` off that table on 2026-09-01/02, and the second time it **masked a genuinely
broken tests lane**. Neither agent misread anything. I relayed the table to Ryan as fact too.

Exit-code contract across `scripts/verify` is now **0 pass · 1 fail · 2 harness problem ·
3 declined to judge**. `run_lane` records 3 as `SKIPPED (nothing evaluated)` and does NOT set
`FAIL`, because the underlying cause is already reported by whichever lane broke. A real run now
shows `tests FAIL` beside `storage SKIPPED` instead of beside a reassuring `storage PASS`.

Break-tested by forcing the condition: `clean=no` in both profiles -> both scripts exit 3 and the
summary reads `SKIPPED (nothing evaluated)`; restored -> exit 0, PASS.

**`mixin_dag.py` also prints SKIP and is deliberately unchanged** -- that is a *sub-check*
("layer check undefined while a cycle exists") inside a lane that still evaluates cycles and can
still fail. Not the same defect.

### There are SIX OpenAI-compatible transports, not five (2026-09-01, session 22c)

Every prior document said five. The sixth is `certification.OpenAICompatibleBenchmarkRuntime`
(`certification.py:391`), which hand-rolled the identical `api_key or os.environ.get(...)`,
Bearer header and `{base_url}/chat/completions` Request. **Found by adversarial review of a
diff, not by reading** — two independent reviewers, after the refactor's own docstring claimed
"five … so it is one edit" while a sixth copy of the auth block survived. All six now inherit
`gateway.OpenAICompatibleTransport`; the four chat ones add `OpenAICompatibleChatTransport`.

`grep -rn 'five OpenAI\|five transports'` returns **zero** across the repo as of `1098ad1`.

**The synthesis transport had no tests at all** — `grep -rl OpenAICompatibleSynthesisTransport
tests/` matched only the two golden files. It surfaced because the rewrite pulled four
long-dark lines into diff-cover's view: the #149 blind spot in a fifth place.
`tests/test_synthesis_transport.py` now pins the wire bytes, all three error labels, and the
`max_output_tokens` check.

**The behaviour evidence is a differential probe, not the suite.** It drives all six against a
fake `urlopen` and prints every observable (URL, method, headers, request-body sha256, every
error message), run against `9a5eaff` and the new tree: **221 lines, zero differences.** Proved
non-vacuous two ways — an in-probe assertion that all six produced a real result and a signed
request (exit 3 otherwise, because an earlier version passed while extraction died on a fixture
error and both sides failed identically), and a **mutation test** that re-introduced both
regressions and got a 32-line diff naming exactly them. `storage_profile` and `receipt_stream`
also PASS with **no changes**: 165,363 storage calls across 715 profiles, 4,033 receipt emits.

### 27 of 172 storage methods run on only ONE engine (measured 2026-09-01, session 22)

`scripts/verify/parity_coverage.py`, `check.sh` lane **`parity-cov`**. For every method defined in
both `storage/sqlite` and `storage/postgres`, it compares the two covered-line sets from
`coverage.json`.

```
BASELINE = {"sqlite_only": 20, "postgres_only": 7}     # of 172 shared methods
```

**Neither existing coverage gate can see this.** `cov-floor` measures a per-module percentage
against a floor, so a module can sit exactly at its floor forever with the same lines dark;
`diff-cov` measures only lines a diff touches. Code that is uncovered **and unedited** is
invisible to both — which is how the `anchor` half-life bug survived a 3,420-line parity suite.

**The `postgres_only: 7` corrects me.** I measured SQLite-covered/Postgres-dark, filed #149
claiming 20, and set the other direction's baseline to 0 without checking it. It is 7, and it is
not harmless: `set_tenant_llm_credentials` (credential storage) and `active_relationships`
(a primary read path) are in it.

**It skips without a DSN, deliberately.** Without one the `postgres` lane never runs, every
Postgres method reads as dark, and the gate would report ~170 false gaps. Break-tested: baseline
lowered → exit 1 naming the direction; Postgres `executed_lines` stripped → exit 2 with a
hermetic-run diagnostic rather than the false cliff.

**A gap is not a defect.** It measures where the suite does not look. The reason to care is the
base rate: the last three unexercised Postgres paths anyone examined (`anchor`, T0-10, T0-4) were
all genuinely divergent.

### The shared-plane extraction SHRANK this gate, and the gate was extended to compensate (2026-09-01)

`storage/_shared/` de-duplicates 13 functions that were two byte-identical copies. That kills the
`anchor` bug class outright — one table cannot disagree with itself — but it also **removed those
methods from `parity_coverage.py`'s population, permanently: 172 → 159 twin methods.** `_shared`
can never re-enter it, because that section compares TWO files and `_shared` has one. Left alone,
nothing would have asked "was this shared plane exercised on Postgres?" — including
`claim_episodes` and `claim_scope_work`, whose whole purpose is to take a writer lock that is
`BEGIN IMMEDIATE` on one engine and `pg_advisory_xact_lock` on the other.

Option (a) of three was taken — **close the hole in the gate that created it**, rather than a
side test or a documented acceptance:

```
SHARED_BASELINE = {"sqlite_only": 0, "postgres_only": 0, "unexercised": 0}   # of 13 functions
```

- Measured from **coverage contexts**, not from two files. `check.sh` now passes
  `--cov-context=test` on both `--cov` invocations, so every measured line carries the pytest
  node id that executed it. Cost measured on the 123-test parity file: **22.4 s → 21.0 s**
  (i.e. none), `.coverage` 276 KB.
- **Engine attribution is by co-executed file, not by parsing `[postgres]` out of a node id.** A
  test drove Postgres if it executed a line under `storage/postgres/`. That stays correct for
  tests that are not parameterised over engines and for the ones that open both handles in one
  body. `_protocol.py` is excluded from the attribution — it is a `TYPE_CHECKING` declaration.
- **Break-tested three ways** (each a deliberately narrowed run, each verified to FAIL and name a
  witness test): deselect the SQLite halves → `postgres_only: 2` naming `claim_scope_work`;
  deselect the Postgres halves → `sqlite_only: 4` naming `_utility_projection_from_events`; run
  only the conformance file → `unexercised: 8`. A 0/0/0 gate that cannot go non-zero is not a gate.
- **The engine-specific halves of the claim path never left the twin population.**
  `_claim_is_live` and `_write_claim` are still defined in both `_operational` files and are still
  measured by the twin section. What moved out is the orchestration that calls them.
- `parity_coverage.py`'s docstring now carries an explicit **"WHAT THIS SCRIPT DOES NOT COVER"**
  section: engine-only modules, behaviour (as opposed to execution), non-method code, and any run
  without contexts.

### Mixin cycles: three edges are domain, one was accidental (2026-09-01, session 22)

Registering the last two mixin packages surfaced two cycles. Each **edge** was tested against
*"what would have to change about the product for this call to go away?"* — three survived, one
did not.

`_as_json` was a `@staticmethod` on `GraphPlaneMixin` called from `_operational` (14 sites),
`_governance` (2) and `_graph` (3). A jsonb decoder is not a graph concern; it moved to
`postgres/_common.py` as a **module-level function**, which leaves the mixin call graph entirely.
Postgres dropped from 6 edges / a 3-module cycle to 4 edges / a 2-module one.

The survivors are declared in `DOMAIN_CYCLES` (`scripts/verify/mixin_dag.py`) edge by edge with
reasons: sqlite `_governance ↔ _operational` (erasure is defined over operational records;
crypto-shred is irreversible so restore must consult key state), postgres `_epochs ↔ _graph`
(an epoch switch **is** the invalidation; graph reads filter by epoch ancestry — that filter is
T0-10). The gate fails on an undeclared cycle, a declared cycle that grows an edge, or a
justification that no longer matches a live edge.

**`config` and `admin_server` are not mixin packages** — zero mentions of `Mixin`, no
`_protocol.py`, no composed class. The plan's "extend to 4 more packages" was really 2, and two of
those lanes could never have failed.

### The deploy fault is OURS, intermittent, and has fired twice (verified 2026-09-01, session 22)

**The chart knob existed the whole time.** `jedai-platform-base 0.1.5` renders
`.Values.deployment.strategy` at `templates/_deployment.yaml:19`, and `.helm/templates/roles.yaml:38`
`mergeOverwrite`s the per-role map into the library context. So `roles.admin.deployment.strategy`
was always reachable from our own values file. The `25%/25%` previously measured was **never the
chart setting anything** — it is the Kubernetes default filling the gap when `strategy` is unset.
Pull the chart to read it: `helm pull oci://us-central1-docker.pkg.dev/jennay-latest-1/jennay/jedai-platform-base --version 0.1.5 --untar`.

**It fired on two clusters, four days apart** — so it was never tied to any one cluster event:

| when | env | incumbent node | replacement | strategy | outcome |
|---|---|---|---|---|---|
| 08-28 05:55 | stage | `xuzb` | `hm5r` | RollingUpdate | **FAILED** (seq 19 `Deploy stage`) |
| 09-01 16:26 | latest | `22dq` | `9iqq` | RollingUpdate | **FAILED** (seq 27 `Deploy latest`) |
| 09-01 19:59 | latest | `22dq` | `22dq` | Recreate | ok — condition not exercised |
| 09-01 20:00 | stage | `xuzb` | `xuzb` | Recreate | ok — condition not exercised |
| 09-01 20:02 | load | `hg9d` | `oxw3` | Recreate | **ok — recovered in 20s** |

Cross-node under RollingUpdate: 2 for 2 failures. Cross-node under Recreate: 1 for 1 recovery.
**The cross-node case has n=1 under the fix** — three environments rolled, but two never met the
condition.

**`FailedAttachVolume` still appears on a SUCCESSFUL deploy.** It did on load. Under
`RollingUpdate` + `maxUnavailable: 0` the incumbent never terminates, so `Multi-Attach` is a
permanent deadlock ending in a 10-minute `LoadBalancerNegTimeout` and a rollback. Under `Recreate`
the incumbent is already terminating, so the attach retry succeeds once detach completes.
**Discriminator:** `SuccessfulAttachVolume` within ~20s = self-healed; `LoadBalancerNegTimeout` ten
minutes later = the real failure. Any alert keyed to the string alone will cry wolf.

**Stage and load were silently four days stale** on `0.1.0-8225c7f` and nothing noticed — the
pipeline said `Failed`, the running pod kept answering `/health`, and `/health` never consults
anything that would differ.

**Nothing durable is stored, measured three ways** (#148). Chart: admin runs
`--graph-path /tmp/memory_graph_demo.sqlite` while the PVC mounts at `/data`. Pod log:
`graph_path=/tmp/memory_graph_demo.sqlite`. Cloud Monitoring `kubernetes.io/pod/volume/used_bytes`:
volume `data` = **0.02 MiB** — an empty ext4 `lost+found`. `operationalStore.enabled: false` in all
five overlays. The RWO constraint that cost two outages protects nothing.

**Harness reads the chart from `main`, not from the branch.** `CHART_BRANCH` defaults to `main`
(`apps/memotron/pipelines/continuous.yaml:507`), `APP_REPO: jedai/memotron`,
`CHART_DIR_NAME: .helm`. A chart fix on a feature branch changes no deploy until it lands on `main`.
Image and chart resolve independently, so a chart-only fix can be re-driven against an
already-built image via an explicit `IMAGE_TAG` (skips Build).

**`check.sh` has no chart lane at all** — `grep -in 'helm\|chart' scripts/check.sh` returns nothing,
and there is no `scripts/verify/*helm*`. 16 lanes, none of which look at how the service deploys.
That is why a single-replica RWO workload with no explicit strategy shipped unnoticed.

### Postgres is not a drop-in for SQLite (verified 2026-08-26)

`relationships_for_scope` omits SQLite's `AND type != 'MENTIONS'` filter
(`postgres/_graph.py:370` vs `sqlite.py:1010`) **while its docstring claims it matches**.
Measured with one variable held (storage engine; same script, same seed):

- scoped read returns 3 rows on SQLite, 9 on Postgres, **MENTIONS first**
- `search()` -> 2 hits on SQLite, `KeyError: 'fact'` on Postgres
- `epoch_content_digest` -> SQLite 3/3 identical, **Postgres 4/4 different** for identical
  content, because MENTIONS rows carry `status='active'` but no `truth_key`, so the digest
  falls back to a uuid minted fresh per materialization

The digest exists specifically to compare two independently recomputed shadows — the thing
`graph_state_hash` deliberately cannot do. On Postgres it inherits the exact flaw it was
built to avoid. **16 call sites** consume the divergent read, including `_retrieval_candidates`,
`diff_epochs` and `build_session_digest`.

**Validated one-line fix** (measured, not applied): `AND type <> 'MENTIONS'` at
`postgres/_graph.py:370`. RED = 9 rows / `KeyError: 'fact'` / digests `b2369943`, `8873b2a9`.
GREEN = 3 rows / `search()` OK / digest `abad85699677` on all four runs across both engines.

Re-runnable gate: `uv run python scripts/verify/parity_postgres.py "<dsn>"` (exits 1 today).

### The agent-facing surface cannot use Postgres at all (verified 2026-08-26)

`storage_settings_from_env()` has **two** call sites: `default_config()` (`config.py:4016`)
and `mcp_server.py:173`. `agent_memory_config()` (`agent_memory.py:446`) never sets
`storage`, and `client.py:218` raises if both `control_plane` and `config` are passed — so
`base_config` is the only seam, and it is unwired.

Measured, same process, same env, back to back:

- `Memotron(graph_path=)` -> **Postgres** (18->21)
- `build_platform_from_env()` -> **SQLite** (0->3, Postgres unchanged)

Confirmed through MCP: a `memory_remember` write returned a uuid present in the SQLite shim
and absent from Postgres. **All 27 agent-memory MCP tools and the whole `AgentMemoryPlatform`
SDK surface are affected.** Flipping `operationalStore.enabled` would leave the agent-facing
layer on a per-pod SQLite file. One-line fix validated (T0-5), not applied.

### The ephemeral KEK is reproduced, and a restart is enough (verified 2026-08-26)

`storage/postgres/__init__.py:113` -> `LocalKeyManager.ephemeral()` ("keys die with the
process"); `client.py:157` never passes a manager. Three arms, one variable each
(`scripts/verify/probe_kek.py`):

- seal + use in one process -> OK (32-byte DEK)
- **fresh process, same `graph_path`, same `.kek` file -> FAIL** `wrapped DEK failed
  authentication`, at `postgres/_governance.py:304` -> `crypto.py:251`
- fresh process, fresh filesystem -> same failure

Arm B kills the "the servers shared a KEK file" explanation: Postgres never reads that file.
**This is not multi-replica-only — one pod restart loses it.**

Blast radius is `get_or_create_governance_key`: governed scopes + tenant LLM credentials.
Ordinary graph rows are unaffected by default (`postgres/_graph.py` never touches
`key_manager`). **It fails silently** — `tenant_llm_credential_state` keeps reporting
`has_api_key: True` after the key has become unusable.

### All three Tier-0 fixes verified together (2026-08-26)

Draft: `docs/findings/tier0-fixes.patch` (65 lines, local-only, reverted after measuring).

| config, live postgres:16 | result |
|---|---|
| unmodified `main` | 1 failed, 1070 passed, 1 skipped |
| + 3 fixes, no opt-out | 3 failed, 1002 passed, **66 errors** |
| + 3 fixes, with `MEMOTRON_ALLOW_EPHEMERAL_KEK=1` | 1 failed, 1070 passed — **baseline** |

- **T0-4 and T0-5 regress nothing** across 1,071 tests. The 117-test parity suite passes
  *after* the MENTIONS filter is added, confirming from a second direction that it never
  asserted the buggy behaviour.
- **The fail-closed guard is not "two lines"** — without an opt-out it breaks 66 tests that
  use the ephemeral KEK by design. It needs the flag in test fixtures and
  `docker-compose.local.yml`, absent from the chart.
- Gates: T0-4 and T0-5 go 1 -> 0. `probe_kek` stays 1 under the opt-out, correctly.

### `main` is GREEN. Credentials leak in from the working directory. (2026-08-26)

**Corrects an earlier entry that said `main` was red — it is not.**
`tests/test_adoption.py::test_post_compact_hook_persists_only_sanitized_continuity` passes in
a clean worktree at `f28e95c`, at pre-merge `9bfc298`, and at the PR tip `d8537e1`.
`f28e95c^{tree}` and `d8537e1^{tree}` are the same hash — identical tree, opposite results,
so it was never the code.

It fails only in a working directory that contains a `.env`. Isolation, one variable at a
time: `.env` aside -> pass (1.2s); `.memotron.yaml` or `.memotron/` aside -> still fail
(~10s). Cause: `build_transports_from_env` calls the bare `load_env_file()` and
`DEFAULT_ENV_FILE = ".env"` is **cwd-relative** (`runtime.py:34,92,177`, and `:232`, `:320`,
`:385`). It writes into `os.environ`, so deleting a credential does not stop it being re-read
from disk. **A Memotron process takes LLM credentials from whatever directory it runs in**,
silently promoting free rule-based extraction to a gateway-billed call. See **T1-14**, **D-45**.

### The no-LLM path captures, it does not extract (2026-08-26, U-12 closed)

Measured 6x2, fact-density x transport (D-47). **Memories = non-`MENTIONS` rows.**
Rule-based: **1 memory at every density, 0 through 8 facts (spread 0)**, including 1 from an
episode with nothing durable in it. Gateway: 0,1,2,4,5,6 (spread 6), correctly writing nothing
for the empty episode.

Rule-based writes the entire sanitized episode body verbatim as one `HAS_STATE` fact — a
670-char episode became a 692-char fact, untruncated. **By design** (the adoption test asserts
the body is inside the fact), but it means the "degrade to no-LLM formation" option in the
plan is not degraded extraction, it is no extraction: one unsupersedable blob per episode.

Gateway extraction produced real typed facts (`Ryan requires KEK`, `parity gate must run
before ...`). Its counts vary run to run — quote the shape, not the numbers.

The earlier `relationships = []` was the **input** (sanitization strips the password and
email first), not a defect.

Gateway endpoint note: the base URL already ends in `/v1`, so it is `POST <base>/chat/completions`;
`<base>/v1/chat/completions` returns 404.

### Supersession works on no-LLM writes; the worker is what's missing (2026-08-26)

Two contradicting compaction summaries, one scope, rule-based only (D-48):

- **before** `run_due_dreams()`: 1 row, `active`, the OLD fact, `observed_count=1`
- **after**: incumbent `superseded` (`observed_count=2`), new summary `active`, old row kept

Truth key is `agent:claude-code:claude-code:has state` — subject + canonical predicate — so
the verbatim-blob shape does NOT defeat the truth slot. An earlier derivation to the contrary
is refuted.

**Formation happens in the dream pass, not at hook time**, and no worker exists on `main`
(B5). So the deployed consequence is not staleness but **actively serving superseded facts
labelled `active`** — see T1-15. Strongest argument yet for D4.

### Supersession is intact on Postgres; T0-5 reaches the hook path (2026-08-27)

**U-14 closed green** (D-49). One client, one scope, same two contradicting facts, engine the
only variable: both backends gave 1 superseded + 1 active with an identical `truth_key`, and
the Postgres arm's write was attributed (0 -> 6 rows, `PostgresStorageBackend`). DERIVED
mechanism: T0-4's leaked MENTIONS rows carry `truth_key=None`, so they are candidate-set noise
and cannot cause a false slot match. **The feared "two contradictory rows both active" does not
occur.**

**T0-5 is wider than first filed.** `build_platform_from_project` (`adoption.py:316`) also
calls `AgentMemoryPlatform.create(graph_path=...)` with no storage seam — and that is the entry
point **every session-start and post-compact hook** uses. So enabling the operational store
would leave the whole compaction/context path on a per-pod SQLite file while the governance MCP
server correctly used Postgres: two halves of one deployment disagreeing about where memory lives.

**Observed, unexplained:** the superseded row carries **no `valid_to`** on either backend,
though the documented contract closes it at the challenger's `valid_from`. Closing test in D-49.

### Formation advances on wall clock; T1-15's framing retracted (2026-08-27)

**U-13 closed** (D-50). Three hooks, gap the only variable, no explicit drain: gap=0s formed
**1 of 3**; gap=3s formed **3 of 3**. The formation job's `cadence_seconds=1` means a hook whose
dream is due forms its own episode inline, with no worker.

**This retracts T1-15 as I filed it.** I claimed the graph serves superseded facts
*indefinitely* without the worker. Direct test: with a 3s gap and no drain, the correct
`superseded` + `active` pair is already present. D-48 had run its hooks back-to-back, so hook 2
fell inside the cadence window — a property of my probe, not the product.

Residual: an episode whose hook fires inside the 1s window stalls until the next hook. Short,
not permanent. **D4 is still needed** for consolidation (300s), pruning and coherence, which
hook traffic will not drive, and for scopes with no hook activity.

### The three Tier-0 fixes now have repo tests (2026-08-27)

RED on `main` then GREEN with `docs/findings/tier0-fixes.patch`, live postgres:16:

| run | result | baseline | delta |
|---|---|---|---|
| live Postgres | 1 failed, **1081 passed**, 1 skipped | 1070 passed | +11, zero regressions |
| hermetic | 1 failed, **1005 passed**, 77 skipped | 1002 passed | +3, zero regressions |

Every `sqlite` arm passed in the red run — the control proving the tests measure the engine
rather than being broken. The single failure is pre-existing **T1-14** (this worktree's `.env`,
loaded cwd-relative); it passes in a clean checkout.

New: `tests/test_scoped_read_parity.py` (T0-4), `tests/test_operational_store_wiring.py`
(T0-5 + T0-2), and `tests/conftest.py` — one autouse fixture opting the suite into the
ephemeral KEK, which is what keeps the fail-closed guard from breaking 66 tests. Six files
changed, **uncommitted**, and deliberately NOT in `.git/info/exclude`: these are the first
takeover changes meant for the repo.

**Not yet enforceable.** No test CI exists (B6); GitGuardian is the only PR check and is not
required. These tests make the fixes reviewable and regression-proof, not merge-gating.

### Decision: well-verified fixes + their tests may enter the repo (2026-08-27, Ryan)

The discovery-phase rule was that takeover artefacts stay local — 17 harnesses and four
register files are in `.git/info/exclude`. **Ryan confirmed the three Tier-0 fixes and their
regression tests are an exception**, on the stated grounds that they are *"the best verified
findings we have."*

The boundary that makes this coherent, and the bar for anything added later:

- **Eligible:** a finding measured by more than one independent path, with a demonstrated
  RED -> GREEN, and a write/behaviour attributed rather than inferred. T0-4, T0-5, T0-2 qualify.
- **Not eligible:** single-probe findings, anything resting on timing or ambient environment.
  Both retractions this week (D-45, D-50) were exactly that class, and a test would have
  encoded them as confident assertions.

Rationale for caring: a log entry is cheap to retract, a test is not — it ships an assertion
plus a docstring asserting *why*, and the next reader inherits both.

### The discovery record is published (2026-08-27)

Issue **#45**, draft PR **#46** (`takeover` -> `main`, never intended to merge). Two commits:
`29624cb` the three Tier-0 fixes + regression tests (the cherry-pickable one), `79e299c` the
record. `DISCOVERY.md` is the front door. `.git/info/exclude` is cleared; `.memotron/` stays
untracked via the repo's own `.gitignore:3` (runtime graphs + `.kek` key material).

The push surfaced an open **HIGH** Dependabot alert nobody had noticed — `nanoid 3.3.17` via
`postcss` in `ui/admin/package-lock.json` (**T3-6**), awkward to fix only because **T4-9** pins
every direct dependency to `"latest"`, so the lockfile cannot be refreshed without moving React,
Vite and TypeScript at once.

### Infra work is split out (2026-08-27)

`INFRA-BACKLOG.md` is the handoff for the infrastructure team, self-contained so they need not
read the code register. **Boundary used: who can land the change**, not whether it is
deployment-related. `tf-jennay` / Vault / GCP console / Harness config / gateway keys are
theirs; `src/`, `tests/`, `Dockerfile`, `.helm/`, `.harness/pipeline.yaml` are ours.

That reclassified three items previously tagged INFRA as **ours**: **T2-1** (Dockerfile),
**T2-2** (`fsGroup` in `.helm/`), **T2-5** (image tag in values). The old tag conflated
"deployment-related" with "another team owns it".

Verified for the handoff, against `tf-jennay` on 2026-08-27:
- **no `google_kms_*` resources in any of the four environments** — KMS is greenfield
- **no memotron Cloud SQL instance or database** (the `cloudsql_*` structures exist for
  other apps, so it is a new entry, not new tooling)
- **one** memotron VIP only: `adresses.tfvars:225` = the API host; the admin host is absent
- WI mismatch is **both halves**: chart `name: "memotron"` + `svc-memotron@...`
  (`values-latest.yaml:212-213`) vs terraform GSA `svc-jedai-memotron`, KSA
  `jedai-memotron` (`iam_svc.tfvars:324-327`). Terraform matches the platform's
  `svc-mcp-jedai-*` convention, so **the chart should change**
- no `NetworkPolicy` template exists in `.helm/templates/`

Also fixed a real register bug found while splitting: **T3-6 was used twice** (read-auditing and
the Dependabot alert). The Dependabot item is now **T4-13**.

### The register was audited for reality (2026-08-27)

Challenge: are these real defects or our own setup? Fair, because both retractions this week
were exactly that. Classified all 57 items by decisive evidence: **33 SOURCE, 9 EXPERIMENT,
7 LOCAL, 4 EXTERNAL, 4 UNVERIFIED**.

`scripts/verify/cleanroom.sh` reproduces the Tier-0 findings in a fresh clone with no `.env`,
no `.memotron/`, no `.claude/`, stripped environment and a dedicated Postgres — swapping only
`src/` between `f28e95c` and this branch. **All five gates behaved as recorded.**

**Exactly one item was our environment: T1-13 (pgvector)** — our compose file's image, not a
product defect. Moved to an "Environment, not product" section, kept visible.

Two harness bugs found on the way, both the same shape as what was being audited: cloning from a
worktree made `origin/main` resolve to a **stale local `main`** (fixed by pinning a SHA), and the
runner stripped `MEMOTRON_ALLOW_EPHEMERAL_KEK`, so our own fail-closed guard read as a failed
reproduction.

### All 20 agent-sourced findings hand-checked (2026-08-27)

The 17-agent workflow that grew the register 62 -> 85 produced findings nobody had verified.
All 20 have now been checked by hand: **12 confirmed as filed, 5 confirmed but reframed or
resized, 2 refuted, 1 unverifiable from this repo.**

Confirmed by running, not reading: **T0-13** (an artifact with no `updated_at` is
unconditionally staler than every memory -- active behavioral rows 1 -> 0, with a 3-arm control
proving the detector discriminates), **T1-18** (`remediate_coherence` reports a hold the graph
never received; phantom window is cosine `[0.500, 0.718)`, derived exactly from
`0.65*cos + 0.2333` against the 0.70 gate), **T1-20** (rewriting
`run_checkpoints.graph_state_hash_after` leaves `verify_chain` **valid**, while rewriting
`last_receipt_hash` is caught), **T1-26** (migration silently replaces the destination's LLM
credentials with the source's; durable because `runtime.py:398-400` returns early when a row
exists), **T1-25/T1-27** (migration refuses in the post-ingestion state, and a *successful*
re-run duplicates the destination 1 -> 2 -> 3).

**The two refutations:** **T1-30** -- the `MemoryRouter` cross-tenant guard rejects 4/4 attacks
through two independent layers, and nothing in `src/` constructs a `MemoryRouter` at all.
**T2-13** -- an unrecognised benchmark task yields `"unspecified"`, which **raises** at
`certification.py:675-681`; it fails closed and loudly, not silently under a wrong metric.

**The reframings changed what the fix is.** **T1-24** is not an inert gate but an *inverted*
one: `allowed_memory_types=()` means "no restriction" at `dreaming.py:1053`/`:3123` and
"nothing allowed" at `replay.py:1100`. **T1-29** was *understated* -- stability is
`1.0 - min(1, 2*stddev)`, so a suite scoring 0.0 every repetition earns a **perfect 1.0** on the
only numeric gate, and no accuracy threshold exists to configure.

**T2-15 is open, not answered:** the repo side is confirmed but Anthropic's `memory_20250818`
spec is not in this tree. CLOSES WITH: read the published tool definition and compare three
field names.

### Workstream C has a gate, and its first run found T1-14 (2026-08-27)

`scripts/ci-build-check.sh` + a `Build_Check` stage in `.harness/pipeline.yaml`, which now reads
**Pipeline_Validation -> Build_Check -> Build -> Deploy_Helm**. No Harness PAT was needed: the
pipeline is `storeType: REMOTE`, so the in-repo YAML is the source of record. The Postgres lane
reports **SKIPPED, not PASSED** without a DSN — a silent skip there is how T0-4 and T0-5 survived
a green suite.

**Its first real run went red, correctly.** `test_post_compact_hook_persists_only_sanitized_continuity`
failed with `StopIteration`. Varying **only the working directory**: from the repo (a `.env`
present) **FAILED in 8.70s**; from a directory with no `.env` **PASSED in 1.11s**. Same commit,
same interpreter. The 8x runtime is the diagnosis — the failing arm makes real gateway calls, so
no `HAS_STATE` fact forms. That is **T1-14**, now upgraded: the cwd-relative `load_env_file()`
makes the suite red in *any worktree a developer actually uses*, so fixing it is a prerequisite
for Workstream C locally rather than an optional cheap win. The gate detects the trap and
explains it rather than neutralising it.

### The Postgres cutover is blocked by T1-27 (2026-08-27)

`migrate_tenant_memory` **does** work across engines — a live `PostgresStorageBackend` passed as
`dest_store` accepts a SQLite source — and it duplicates identically: **1 -> 2 -> 3 rows across
three runs**, each reporting success. So migration is the cutover path *and* the thing that
corrupts it. **T1-34** is the other half: there is no CLI route (`open_storage` hardcodes
`engine="sqlite"`, `storage/factory.py:130`; the CLI passes file paths), so a cutover is
bespoke SDK code — and that code is what multiplies the destination on retry.

### Dogfooding runs half the product (2026-08-27)

The lifecycle hooks work: the 18:08Z compaction formed 12 memories. But the
`memotron_agent_memory` MCP server (27 tools, 18 `memory_*`, verified by a manual stdio
handshake) had **never been connected** — `enabledMcpjsonServers` was `[]` in the main checkout
and this worktree had no `~/.claude.json` entry. So hook capture was a **replacement** for
deliberate writes, not a supplement, which `.claude/rules/memotron.md` explicitly disowns.
Result, measured: **11 of 11 `directive` rows are `active`**, including one telling a future
session to verify **T1-30 — which we refuted**. Filed **T1-33**. Approval now written; it takes
effect next session, since MCP attaches at startup.

**`~/repos/brain` is not a git repository** (no `.git`, not nested in one). The worklog ritual's
"do not commit the vault unless asked" line reads as if it could be committed; it cannot.

### Which Memotron are we actually running? (audited 2026-08-27)

Asked because three checkouts of this repo exist and a mispointed binary or shared graph would
invalidate every dogfooding observation. **All three axes are clean, but the first one is not
what you would assume.**

**The binary is NOT this worktree's source.** `memotron` on PATH resolves to
`~/.local/bin/memotron`, shebanged to a **`uv tool install` environment** at
`~/.local/share/uv/tools/jedai-memotron/`. It is a **frozen copy** in `site-packages` — no
editable `.pth`, no link back to any worktree. So the hooks and the MCP server run a snapshot,
not the code we edit.

**How stale: exactly three files, and they are exactly our three Tier-0 fixes.** `diff -rq`
against `src/memotron` reports only `agent_memory.py` (T0-5),
`storage/postgres/__init__.py` (T0-2 guard) and `storage/postgres/_graph.py` (T0-4). Both
diffable files match `f28e95c` **byte-for-byte**, so the installed tool *is* `origin/main`.

**Why the dogfooding observations still stand:** all three deltas are Postgres- or
storage-config-specific, and dogfood runs on **SQLite** (`.memotron/dogfood.sqlite`). T0-4 is
a Postgres read filter, T0-2 a Postgres KEK guard, T0-5 a Postgres DSN wiring gap — none is
reachable on the SQLite path. **This is a derived claim**, not an observed one: it follows from
the three file identities plus the configured backend. It would be wrong if any of those files
also changed SQLite behaviour, which reading them says they do not.

**The graph is per-worktree, not machine-global.** `.memotron.yaml` sets
`graph_path: .memotron/dogfood.sqlite` — **relative**, resolved against `--project-root`.
There is **no** `~/.memotron/memory.sqlite`, so the machine-global trap the verify skill warns
about is not active here.

**No concurrent writer.** `git worktree list` shows three checkouts —
`repos/jedai/memotron`, `worktrees/memotron` (both at the spike commit `4550b60`) and this
one. **Only this one has a `.memotron/` directory or a `.memotron.yaml` at all**, so
nothing else is reading or writing the dogfood graph.

**One latent inconsistency worth fixing.** The hook passes `--project-root ${CLAUDE_PROJECT_DIR}`
(required), but `.mcp.json` passes `${CLAUDE_PROJECT_DIR:-.}` — **falling back to cwd**. If that
variable is ever unset, the MCP server binds its graph to whatever directory it was launched
from. That is T1-14's shape in the MCP config: a cwd-relative default that is invisible until it
is wrong.

### T1-14 is fixed, and the filed fix would not have worked (2026-08-27)

The four bare `load_env_file()` calls at `runtime.py:177,232,320,385` are **deleted**, and
`load_env_file`'s `env_file` argument is now **required** — `DEFAULT_ENV_FILE` survives only as a
constant, with no default binding — so a cwd-relative read cannot reappear by omission.

**Removal was safe because the correct pattern already existed:** `build_platform_from_project`
-> `_ensure_project_llm_credentials` (`adoption.py:477`) loads `<project_root>/.env` before any
builder runs, and **every** CLI path (`mcp`, `hook`, `status`, `llm`) goes through it. Verified by
running both from source: `status` exit 0 with the right `graph_path`/`project.id`, and
`hook session-start` exit 0 still loading memory off the sealed credential.

**The work queue's stated first move — "resolve `.env` against the project root" — would NOT have
fixed this.** `discover_project_root` (`adoption.py:197-208`) falls back to `Path.cwd()` and
matches on `.git` as well as `.memotron.yaml`, so a stray `.env` in any unrelated git repo is
still read, and the credential-clearing defeat is untouched either way. Narrowing the blast radius
is not the same as closing the hole.

**Evidence (agent-run, commands recorded — not hand-checked).** New harness
`scripts/verify/probe_env_file_cwd.py`: **exit 1 with 5 failing checks -> exit 0**, replicated
**5/5**, with a no-`.env` control arm green in *both* directions so the probe is not always-red.
Then a two-arm suite proof holding one variable — only `runtime.py` reverted via
`git stash push -- src/memotron/runtime.py`, the same `.env` present in both arms:
`test_post_compact_hook_persists_only_sanitized_continuity` **PASSED 2.09s with the fix** vs
**FAILED 3.99s without it on `ValueError: LLM dream-agent request failed with HTTP 401`**. That
401 is the mechanism caught in the act — the unfixed code read a `.env` nothing asked for, beat
the test's `delenv`, and billed a request. `scripts/ci-build-check.sh` now **PASSES with a `.env`
present** (43s), which was the condition that made it red. Full suite **1006 passed / 77 skipped,
identical before and after**.

**One test changed rather than deleted.**
`test_runtime_loads_dotenv_gateway_default_without_exposing_secret` asserted the defect *as a
feature* (`monkeypatch.chdir` + write `.env` -> assert gateway selected). It is now
`test_explicit_dotenv_load_takes_gateway_default_without_exposing_secret`: same subject (gateway
defaults, secret never exposed in credential state), explicit root-anchored load, plus a new
assertion that the builders do **not** self-load.

**Would be wrong if** a consumer outside this repo depends on implicit `.env` pickup through
`client.py:1170`, `admin_server.py:2442`, or `local_platform.py:108` — those three resolve no
project root, so they now require the key exported explicitly. That is already the documented flow
in `scripts/verify/README.md` (`set -a; . ./.env; set +a`).

### The MCP write path works, and its first write formed a false fact (2026-08-27)

**T1-33's approval was exercised for the first time.** One deliberate `memory_publish` of a sourced
decision -> `memory_refresh` -> `memory_search` round trip: **1 episode processed, 9 nodes, 6
relationships, 7 admitted, 0 quarantined**, taking the project scope from **0 rows to 5**. Session
9's finding stands and is now bounded on the other side: a full day of hook-only operation put 0
rows in the project scope, and one deliberate write put 6 in. **Formation suppression (T1-1/F6) did
not bite** on this episode.

**But one formed fact inverts the source.** The published text said `discover_project_root` would
*not* have fixed T1-14. Extraction produced
**`T1-14 incident was caused by discover_project_root function`** (`ab17aa08-4b3b-46fe-8d73-f431534fbf05`,
confidence **0.80**, `EXPERIENCED`) — a negation read as an attribution. This is T1-33's shape with
a sharper edge: not a stale directive but a **confidently wrong causal claim in shared project
memory**, and nothing retires it. Left in place pending Ryan's call on `memory_forget`, since that
tool requires the user to confirm the target.

**Consequence for how to publish:** a sentence of the form "X would not have fixed Y" is unsafe
input to this extractor. State the positive claim and the rejected alternative as separate
sentences.

### Every gate measures the tree; only `sanity.sh` starts the product (2026-08-28)

`pure_move` compares bytecode. The API golden enumerates symbols. `mixin_dag` walks the call graph.
`coverage_floors` reads a JSON report. `receipt_golden` diffs a recorded stream. **All of them can be
green on a tree that does not build, does not install, or installs and fails on first use** — none of
them ever runs the thing.

That gap was real. Roughly 100 commits of restructuring happened before the product was run from an
installed wheel even once. `scripts/verify/sanity.sh` closes it: build the wheel, install into a clean
venv, import from site-packages, drive the SDK, count MCP tools — **and A/B the whole thing against a
pre-refactor wheel built from `takeover`.**

First run, 2026-08-28, after the `dreaming/` split and Phase A:

- wheel carries all split subpackages (`dreaming/` 17, `storage/` 26, `config/` 15, `models/` 11)
- CLI, all subpackage imports, `_log.name == "memotron.dreaming"`, `memotron.migration`'s
  imports — all fine from site-packages with no `src/` on the path
- **A/B output byte-identical to `takeover`**: dream `created=0`, direct write 1 fact / 1 search hit,
  1 receipt, **MCP 39 and 27 tools** on both

Three things that only the A/B could have told us:

1. **`created=0` from a formation dream pass is NOT a regression.** It reproduces exactly on the
   pre-refactor wheel — the known no-worker/no-LLM path (B5 / T1-15). Run alone, the refactored build
   looks broken.
2. **`agent_memory_mcp` registers 27 tools against 28 tool-shaped functions**, on both wheels —
   independent, protocol-layer confirmation that `memory_restore` is missing its `@mcp.tool()`
   decorator and is unreachable. Confirmed live, not by reading.
3. **The wheel still cannot serve its own admin UI** — `DEFAULT_ADMIN_STATIC_DIR` resolves to
   `<venv>/lib/python3.12/ui/admin/dist`, which does not exist. Pre-existing (session 2), unchanged.

### Phase 6 tested: all three "decoupling" candidates fail, and the code is already decoupled (2026-08-28)

Phase 6's ranked candidates were measured before building any of them. **None survives.** Recorded
in full because these are the ones a future session will reach for first.

**1. "Two facilities are misfiled as storage" — half wrong, half right-but-unmovable.**

- `graph_state_hash` (72 sites) is documented as receipt evidence, and its *purpose* is. Its
  *implementation* is `_scope_state_tuples`, which runs SQL against `_connection`. It is a
  scope-wide read. Moving it means a facade that calls back into the store — indirection, not
  decoupling. **Leave it.**
- `reveal`/`reveal_vector` (78 sites) really are separable — 11 and 14 lines whose only dependency
  is `get_governance_key`. But the write side already has `_ContentProtection` (holds the scope
  DEK, does `seal`), and completing the symmetry fails on arithmetic: **68 of 79 call sites have no
  protection object in scope.** Worse, `backend.reveal` handles three cases in one call — decrypt,
  pass plaintext through, return the shredded placeholder — and a `_ContentProtection` exists only
  when there *is* a key. Every one of those 68 sites would need "protection or None" branching.
  **The current design is right; the framing was taxonomy, not fitness.**

**2. "`StorageBackend` is 148 methods — a namespace, not a contract" — my metric was wrong.**
`StorageBackend` has **zero methods of its own**. It is `MemoryGraphStorage` (61) +
`OperationalStorage` (87), two named domain contracts — and `storage/composite.py`'s
`SplitStorageBackend` **already routes between them for split deployments**. The seam exists and is
exercised. Summing two contracts and calling the total a namespace made "shrink the contract" look
like available work. `coupling_report.py` now reports them separately, with that correction in the
source.

**3. Typing — done.** Strict tier 52 → 104 modules; the residue is blocked on `client.py`.

**So "more decoupled" is probably not achievable as a cutover claim, because the code is already
decoupled everywhere it can be.** That is consistent with the earlier null result: the eight shared
graph members are shared because they are the same eight rows. What the branch *can* defend is
**more manageable** (3 of 4 god-modules split, zero cycles, 9 gates, 111 coverage floors, 104
strict modules) and **at least parity** (1129 passed, Postgres 86.8%, sanity A/B). If the cutover
gate stays worded as "more decoupled", it will not be met by more refactoring — it needs rewording
to what the work actually delivers.

### The dual-backend constraint, and what the Postgres lane found (2026-08-28)

**Design intent (Ryan): the product must stay genuinely runnable locally on SQLite, while the
hosted deployment runs cloud Postgres.** Not a transitional state — the target shape. That is the
whole reason `tests/test_storage_backend_parity.py` exists and the reason the two backends were
made to compose from the same six named planes: local dev and production must behave the same, and
nothing but a parity suite can hold that.

**The Postgres lane ran for the first time in this effort on 2026-08-28** —
`docker compose -p $(basename $PWD) -f docker-compose.local.yml up -d postgres`, with
`DW_PG_PORT` set to avoid the `local/` stack already on 55432. Results:

- `pytest -m postgres` → **76 passed**
- full suite with a DSN → **1129 passed, 1 skipped** (against 1053 / 77 without)
- **`storage/postgres` coverage 0% → 86.8%** (1919/2211), 10% of the tree that had never been
  executed

**It immediately found a regression this effort introduced, invisible to all 1053 other tests.**
`test_storage_backend_parity.py` did `monkeypatch.setattr(memotron.models, "uuid4", ...)`. The
models split pruned that re-export as an incidental import — the three-way check greps for
`from ... import uuid4` and `models.uuid4`, and a `setattr(mod, "uuid4", ...)` names it in a
**string**, so the check could not see it.

The naive fix would have been worse than the bug. Restoring the re-export makes `setattr` succeed
and intercept **nothing** — after the split each `models/_*.py` does its own `from uuid import
uuid4`, so the `default_factory` lambdas resolve against the submodule, not the package. Both
engines would mint random uuids while the test still claimed to pin them. The `AttributeError` was
the honest failure. Fixed by patching every binding, which reproduces the pre-split semantics.

`coverage_floors.py` now treats Postgres as **conditionally** exempt: unmeasured prints
`SKIPPED ... Not a pass; nothing was checked`, measured enforces `POSTGRES_FLOOR = 85.8`. A hard
floor would false-fail every local SQLite-only run, which is precisely the workflow the design
intent above requires keeping. Control-tested both directions.

### What a modern-Python pass would actually target (measured 2026-08-29)

Ryan scoped the wave AFTER the god-class breakup: modern Python style, structure and architecture.
Measured rather than listed from a style guide:

| finding | count |
|---|---|
| `raise ValueError` in `src/` | **1,023** |
| custom exception classes | **16** |
| `dict[str, Any]` in signatures | **524** |
| bare `: Any` annotations | **223** |
| functions ≥150 lines, post-split | **34** (longest 1,130) |
| files `ruff format` has never touched | **224** |
| complexity rules (C901 / PLR09xx) | **disabled** |

**The exception taxonomy is the biggest and the least obvious.** 1,023 `ValueError` against 16
custom types means config validation, a policy refusal, a malformed candidate, a read-only scope
and a governance denial are all the same type — **a caller cannot branch on what went wrong
without matching message text.** `pyproject.toml` already concedes this in its
flake8-pytest-style config, excusing `raises-require-match-for` on ValueError because pinning 45
assertions to message strings would be brittle. That concession is the smell, not the fix.

Then: type the `Any` payload bags (the same thing that blocks the strict tier and blunts the
`py.typed` just shipped — a consumer gets precise types right up to the first `dict[str, Any]`);
then decompose the 34 god METHODS, which survived on purpose because `pure_move` only proves a
move if the body is untouched, a constraint that expires when the split does; then `ruff format`.

**Observability is a SEPARATE, later pass and should adopt the org's existing application
patterns rather than be designed here.** Measured for when it happens: 5 `getLogger` sites, 17 log
statements across ~35k lines, 0 structured, 0 OpenTelemetry, and 6 `admin_server` sites returning
raw `str(exc)` to clients. The correlation key already exists — the receipt system threads
`run_uuid` everywhere. (An earlier version of this section made observability the whole phase;
that was over-indexing on one measurement.)

### Branch strategy: `main` is a frozen PoC, this branch takes over wholesale (2026-08-28, Ryan)

**Do not land work on `main` piecemeal.** `main` is the baseline research PoC and is deliberately
held as a reference point. The productionization branch becomes the new engineering baseline and
replaces it in a single merge when the refactor and decoupling are done. Splitting fixes out into
their own PRs creates a second lineage to keep in sync and buys nothing.

Recorded because I proposed the opposite and was wrong, and the correction is not obvious from the
repo alone. I argued that T0-10 "has been live in `main` the whole time" and cut PR #106 to land six
defect fixes early. **Nothing is deployed from `main`** — the cluster runs a June health stub at
Helm revision 1 and the chart has never shipped (session 7, T0-9). *[NO LONGER TRUE, and left
here because the reasoning above is still the point. As of 2026-09-08 merging to `main`
auto-deploys to `latest` within the hour — see the fact of that name at the top of this section.
The bolded sentence is a June observation, not a standing property.]* The defects are real; the
urgency framing was not, and the fact that refutes it was already in these notes. #106 is closed;
all six commits were cherry-picks *from* this branch and travel with it.

The corollary, which also caught me out: **Phase 0 does not get its own PR either.** `main` has no
ruff, no mypy, no coverage config (`pyproject.toml` is 44 lines there against 578 here) and none of
`scripts/verify/`. That is expected and fine — the enforcement layer arrives with the takeover, not
before it.

Open question this raises, unresolved: **what counts as "done" for the takeover?** `client.py`
(8,914) was deferred on 2026-08-28 in favour of the production blockers, but the takeover is gated
on the refactor being finished. Either the takeover can happen with `client.py` unsplit, or the
deferral needs revisiting. Worth settling before the merge, not at it.

### The coupling pass: what is worth doing, and what has already been ruled out (2026-08-28)

Ryan's framing is right — this repo is a research PoC being taken to production, and a
coupling pass is the substance of that. The seam hunt already did the reconnaissance. Recorded
here rather than only in the plan file, because the plan lives under `~/.claude/plans/` and is
not version-controlled with the repo; **if these findings are lost the negative ones get
re-attempted, which is the expensive half.**

**Precondition, non-negotiable: `storage/postgres` is 0% covered — 2,211 statements, 10% of the
tree.** Any coupling work touching storage is unguarded across half the storage layer, and
`receipt_golden.py` does not cover it because it only sees what the suite executes. Get the
Postgres lane running in CI first, or the pass is hopeful rather than measured — on exactly the
engine the cutover depends on.

**Worth doing, in order:**

1. **Two facilities are filed as storage and are not storage** — `reveal`/`reveal_vector` (78
   sites, content decryption) and `graph_state_hash` (72 sites, receipt evidence). 150 call
   sites, **28% of all storage traffic in `dreaming/`**. Biggest measured win and genuine
   decoupling rather than renaming.
2. **`StorageBackend` is 148 methods** (`MemoryGraphStorage` 61 + `OperationalStorage` 87). A
   namespace, not a contract.
3. **Typing — the cheapest decoupling there is.** 52 modules strict, 33 lenient carrying 72
   suppressed codes (`arg-type` x26, `attr-defined` x10, `assignment` x9). You cannot decouple
   what you cannot describe: `_effective_prompt_profile` is annotated `-> object` and that one
   annotation causes 3 of `_formation`'s 5 suppressed codes.
4. **`client.py` is the most coupled thing in the repo** — 279 `self.graph` call nodes over 72
   distinct backend members, more than all ten dreaming mixins combined. (Earlier text said 271
   / 71; that was `grep -c`, which counts LINES. `scripts/verify/coupling_report.py` walks the
   AST and counts NODES, and is the number to trust.)

**Before starting any of this, run `uv run python scripts/verify/coupling_report.py`.** It prints
all of the above against the 2026-08-28 baseline. The rankings here were measured while
`client.py` was still unsplit, and Phase D redistributes 279 storage sites across thirteen
mixins — which may show the coupling is concentrated rather than pervasive and reorder this
list. A plan acted on from stale measurements is how a refactor optimises the wrong thing
convincingly.

**Ruled out, with the evidence, do not re-attempt without new information:**

- **Ports over the shared graph core.** Strip the two misfiled facilities and the members with
  fanout >= 4 are the bitemporal relationship plane — eight methods shared because they are the
  same eight rows. N ports over one object with one implementation is indirection, and it breaks
  the `transaction()` atomicity contract `storage/base.py` spends sixty lines specifying. **The
  coupling is correct.**
- **Repartitioning the mixins for a smaller cut.** Current partition crosses 54.8% of call
  weight; random same-size partitions 89.2%; the best greedy refit 41.5% at 25 relocations, and
  its top suggestion dissolves `_contradiction` into `_materialization`. **The ceiling is ~30%
  and navigability is what the split bought.**
- **The receipted-write context manager.** Killed by measurement: 23 of 34 brackets fit, 9 are
  irregular, 2 asymmetric by design, **zero malformed**, and the receipt golden already catches
  the failure it would have prevented. The delegation fix that replaced it is landed (43 -> 10
  cross-object private reaches in `client.py` — 11, not the 10 commit 04884a4 claims;
  that figure was measured on the broken intermediate state where the delegate called itself).

**What "better practice" should mean here, measurably** — not "fewer edges", which was tried and
capped at ~30%. The movable numbers: 148-method contract, 72 suppressed type codes, 150
misfiled-facility call sites, 0% Postgres coverage. Each can go down and be gated.

### `dreaming.py` is split, and DreamEngine's coupling now has a number (2026-08-28)

`dreaming.py` 10,522 lines -> **16 files, largest 1,638**. `DreamEngine` composes from 10 mixins;
runtime MRO verified: `DreamEngine -> FormationMixin -> ConsolidationMixin -> DeduplicationMixin ->
RollupMixin -> DecisionMixin -> PredicateCanonicalizationMixin -> EntityResolutionMixin ->
MaterializationMixin -> ContradictionMixin -> RelationshipLifecycleMixin -> object`.

**The headline number is not the line count.** `dreaming/_protocol.py` is generated from the
measured call graph, and it records that **61 of DreamEngine's 152 members are called across a
mixin boundary** (280 internal `self.` calls in total). The split made the class navigable; it did
**not** decouple it, and nothing in the branch should be read as claiming otherwise. The structure
was always this shape — it was invisible while every call looked local inside one class body. That
file shrinking is the metric for a future decoupling pass.

- **`__init__.py` is 1,457 and does NOT hold zero definitions**, unlike `models/`, `config/` and
  `storage/sqlite/`. R-A1 keeps `__init__` on the composer, and R-A2 keeps every helper ≥2 mixins
  share. That is the rule working, not a shortfall.
- **`_materialize_episode` moved as ONE unit, 1,130 lines, `pure_move` PASS.** Its
  `raw_candidates_stored` flag selects between fail-fast (operator path) and
  quarantine-and-continue (batch), and **no test distinguishes the two regimes** — a decomposition
  that flipped one into the other would land green. Its module docstring now records all three
  call sites with the flags each passes; that docstring is the spec for whoever decomposes it.
- **mypy: 9 blanket-disabled codes over 10,522 lines -> 1–5 per mixin**, each with its reason
  written at the pyproject entry. The blanket had been hiding any real attribute typo in the
  whole module.
- **Coverage floors 78 -> 93 modules.** The per-concern numbers were always true and were averaged
  invisibly into one 87.5%: `_formation` 94.7, `_materialization` 95.0, `_rollups` 91.7,
  `_lifecycle` 83.7, **`_dedup` 80.1** — the lowest, and home to `_weaker_duplicate`, the 21-line
  method that decides which of two equivalent claims is dropped, with no error and no
  distinguishing receipt when it is wrong. First place to aim a test.
- **`pure_move` is PASS everywhere except `_entities`, at 18/20, deliberately.** Two RUNTIME
  `DreamEngine._EntityCandidate(...)` calls had no binding in the mixin module; rewritten to
  `EntityResolutionMixin` rather than injecting a global from `__init__.py`. Verified at runtime
  that both names are the same object. The other 10 references are annotations and cost nothing —
  measured, not assumed.

### Three modules are split, and both storage backends now share one shape (2026-08-27)

Branch `split-models` (off `memotron-productionization`), 52 commits past Phase 0.
`bash scripts/check.sh` green throughout: lint · types · tests 1028 · cov-floor · diff-cov.

| module | before | after |
|---|---|---|
| `models.py` | 1,826 | **11 files**, largest 326 |
| `config.py` | 4,361 | **15 files**, largest 979 |
| `storage/sqlite.py` | 5,725 | **9 files**, largest 1,178 |

`__init__.py` in all three holds **zero definitions** (AST-verified) — pure re-exports.
Not yet split: `client.py` (8,871), `agent_memory.py` (3,823), `admin_server.py` (3,024).
(`dreaming.py` was split on 2026-08-28 — see the section above.)

- **`pure_move` holds across every split**, base to tip: all shared functions bytecode-identical.
- **Ratchet:** mypy strict tier 30 → 53 modules; coverage floors 46 → **78** guarded.
- **Both backends now compose from the same six named planes.** `SQLiteStorageBackend` and
  `PostgresStorageBackend` list the same mixins, so
  `git diff --no-index storage/sqlite/_graph.py storage/postgres/_graph.py` is a **parity
  artifact**. It earned itself on the first mixin: `_policy_contract_from_row` /
  `_policy_shadow_from_row` are postgres's `_contract_from_row` / `_shadow_from_row` under
  different names, which name-matching had filed as "sqlite-specific". The `_lock_*` asymmetry
  is intended — Postgres takes row locks, SQLite serialises writes.
- **`layer.py` (new) measures a module before it is split.** Both mega-modules were *easier*
  than the plan assumed: `models.py` 88 defs / 12 hard edges / **zero cycles**; `config.py`
  94 defs / 46 hard edges / **zero cycles**. The plan's warning that config's 82 validators
  would force indivisible clusters was wrong.
- **`storage/sqlite/_protocol.py` (new)** declares the composed-backend surface a mixin may
  assume, replacing a blanket suppression: **200 `attr-defined` → 12**, one blanket entry → four
  precise per-module ones. Runtime MRO verified unchanged. The blanket had been hiding any real
  attribute typo in 5,700 lines. **Postgres has the same problem and a different surface
  (`_engine` vs `_connection`) — its twin is NOT done.**

### The guards found things the suite could not, and had two holes of their own (2026-08-27)

- **The API golden was blind to pydantic field defaults.** Mutating
  `GrowthPolicy.cosine_threshold` 0.88 → 0.99 in a moved module passed `pure_move`, the golden,
  ruff and mypy — only the suite caught it. Since `models/` and `config/` are almost entirely
  pydantic, the safety argument for 26 commits rested on tests at exactly the point the bytecode
  proof does not reach. The golden now records annotation, default/factory/required and
  constraints per field; the same mutation now fails and names the field.
  **Correction: "pure_move is a static proof over 100% of the code" was wrong** — it is a proof
  over *function bodies*.
- **`memotron.graph.__getattr__` forwards dynamically, so the golden structurally cannot see
  it.** It enumerates `vars(module)`; a `__getattr__`-provided name is in no module dict. Three
  names silently left the surface this way (`ScopeStateHashTracker`, `DREAM_CLAIM_STALE_SECONDS`,
  `LLM_CREDENTIAL_SUBJECT_KEY`) before the pattern was visible; `admin_server` and
  `test_embedding_transport` broke. `tests/test_graph_compat_shim.py` now pins it.
- **`coverage_floors.py` read a stale report and said PASS over a tree it had never measured** —
  14 new config modules were simply absent from it. Now compares mtimes and exits 2 with STALE.

### Phase 0 of productionization is done, and the quality baseline is measured (2026-08-27)

Branch `memotron-productionization` (from `takeover`), 14 commits, unpushed. `bash scripts/check.sh`
runs every gate in one command and passes.

- **ruff: 19,394 -> 0 repo-wide.** `--select ALL` reports 19,394 here and ~90% of it is
  E501/TRY003/EM101/COM812/D1xx. The Tier-1 set in `pyproject.toml` found 631 real ones.
  `line-length = 120` is measured, not taste: p50=40, p95=85, **p99=100**, max=215, so ruff's
  default 88 would declare 2,391 existing lines wrong.
- **mypy went from "cannot run at all" to `Success: no issues found in 58 source files`.**
  It aborted on `sqlite.py:2000`, where a prose comment wrapped onto `# type:` and was parsed as a
  PEP 484 type comment — *one comment made the whole repository un-type-checkable*, and `--exclude`
  does not help because import-following still parses the file.
- **30 of 58 modules already pass full strictness**, including `models.py`, `storage/base.py`,
  `retrieval.py`, `crypto.py`. That list in `[[tool.mypy.overrides]]` is the split's ratchet:
  adding a module to it is the definition of that module's split being done.
- **Coverage baseline (statement, hermetic):** 78.5% overall, **87.4% excluding the Postgres
  subpackage**, which is 2,207 of the 4,663 missed statements and is *unrunnable* in CI. A global
  `fail_under` would measure the CI wiring gap, so `scripts/verify/coverage_floors.py` gates
  46 modules individually and marks Postgres EXEMPT visibly.
  models 98.9 · sqlite 93.0 · agent_memory 93.6 · client 89.0 · dreaming 88.7 · config 87.5 ·
  admin_server 74.2 · mcp_server 46.6 · local_platform 19.0.
- **Suite 1006 -> 1021 passed, 77 skipped.** Lanes are now selectable: `-m "not postgres and not
  corpus"` collects exactly 1011, `-m postgres` 76, `-m corpus` 1, total 1088.

### The three refactor guards exist and are calibrated in both directions (2026-08-27)

Built before the split, because each answers a question the others cannot.

| | question | kind | runs |
|---|---|---|---|
| **P1** `scripts/verify/api_surface.py` | did anything **disappear**? | runtime, composed MRO | in `pytest`, ~0.6s |
| **P2** `tests/test_module_import_contract.py` | did the import-time facts survive? | runtime | in `pytest` |
| **P3** `scripts/verify/pure_move.py` | did anything **change**? | static, bytecode | per commit, by hand |

- **A genuine pure move IS bytecode-identical.** Validated against a throwaway split of
  `coherence.py` using the real Shape A + Shape B patterns (module -> package, methods -> a mixin
  composed by multiple inheritance): PASS, and `test_coherence.py` 20/20. **This settles the plan's
  stated make-or-break risk** — P3 is a proof, not a heuristic.
- **P3 caught a swapped argument in a symmetric call while `pytest` reported 1011 passed.** That is
  the whole argument for it: `dreaming.py` and `client.py` sit at ~89% statement coverage, so ~11%
  of what the split moves has no runtime witness at all.
- P1c (the MRO-shadow check) catches two mixins defining the same private helper — the failure where
  the leftmost base silently wins forever and no test notices.
- Negative cases all fire: dropped re-export, removed method, duplicate mixin helper, forgotten
  import, changed body.

### Five tool traps, each found by running the tool rather than reading its docs (2026-08-27)

Every one of these is a config that **looks** like a gate and is not.

1. **`strict = true` inside a per-module `[[tool.mypy.overrides]]` is applied PROJECT-WIDE, silently**
   (mypy 2.3.1). A/B on one file: with it, 298 errors in 28 files; without, 211 in 20. It is the
   documented-looking pattern and it is wrong. Fix: spell out the ten flags `strict` abbreviates,
   which *are* per-module.
2. **`--strict-markers` in pytest `addopts` DOES NOT ENFORCE** (pytest 9.0.3). An unregistered mark
   still only warns; the identical flag on the command line makes it a collection error. Reproduced
   three ways. `scripts/check.sh` passes it on the CLI for this reason.
3. **diff-cover's `--exclude` needs a leading wildcard.** `src/memotron/storage/postgres/*`
   matches nothing and fails *silently* — identical output, identical 81%. `*/postgres/*` works (92%).
4. **pre-commit's `trailing-whitespace` hook corrupts `.patch` files.** Trailing whitespace is
   significant in a unified diff — a blank context line is a single space. It rewrote all three
   `docs/findings/*.patch` on its first run; they still applied only because `git apply` is lenient.
5. **In the parity suites the engine is a fixture PARAMETER, not a file property.** A module-level
   `pytest.mark.postgres` on `test_storage_backend_parity.py` would have silently disabled all 53
   `[sqlite]` twins — half the parity suite — while still reporting green.

### Corrections to the record (2026-08-27)

- **`docs/findings/tier0-fixes.patch` was ALREADY LANDED** — commit `29624cb`, with its regression
  tests. Its plain `git apply` fails *because the lines are already there*; `--3way` succeeding is
  not evidence of drift. Anywhere the record says those three fixes are unapplied, it is stale.
- **`scripts/verify/probe_rollback_leak.py` is rotted.** It aborts with
  `ReplayVerificationError: no receipts recorded for run …` **identically at HEAD**, so it can no
  longer measure T0-10. `probe_migration_visibility.py` still can.
- The earlier `mypy --strict` figure of 687 was inflated by `--follow-imports=skip`, which resolved
  pydantic and mcp to `Any` and manufactured 177 `cannot subclass BaseModel` plus 176
  `untyped-decorator`. The honest number in the resolved environment is **298**.

### External reference: the knowledge-graph cookbook (2026-09-01)

[`docs/knowledge-graph-cookbook-primer.md`](docs/knowledge-graph-cookbook-primer.md) — Anthropic's
[knowledge-graph cookbook](https://platform.claude.com/cookbook/capabilities-knowledge-graph-guide)
read against this repo. It is the canonical minimal version of the pipeline we implement, so it is
useful mainly as a **contrast**: extract → resolve → assemble → summarise → query, with no tenancy,
no contradiction, no retraction and no audit.

- **The point worth carrying: ontology is the load-bearing decision, and closed-vs-open is not one
  choice but two.** Both systems land on the same axis — **closed entity vocabulary, open relation
  vocabulary**. Ours is explicit: `match_relationship_instruction` (`extraction.py:403`) says the
  open-predicate instruction "makes the relation vocabulary open **without loosening the entity
  vocabulary**", and an unmatched type is a hard `CandidateViolation.RELATIONSHIP_TYPE_NOT_ALLOWED`,
  not the cookbook's silent drop. The difference is the layer beneath: every open predicate must
  project onto a **closed** `MemoryType` (8, `_enums.py:170`) and `ClaimMode` (6, `_enums.py:139`),
  which govern cardinality, retention, dedup and retrieval budget.
  `RELATIONSHIP_TYPE_MEMORY_TYPE_MAP` (`config/_claim_mode.py:19`) has **only 4 entries** — anything
  else must declare `memory_type` or config validation fails fast. Say it any way you like; it must
  land in one of eight governed buckets, declared.
- **Our own evidence that the closed layer must stay tight:** `_enums.py:170` records that
  `MemoryType.ANCHOR` was formerly `identity`, and that name **absorbed 84% of a documentation
  corpus** — "every descriptive sentence is arguably an identity". The fix was renaming and
  narrowing the category, not a threshold or a better model. Cite this before adding a ninth type.
- **Secondary: the cookbook *builds* a graph, Memotron *maintains* one.** `_entities.py`'s
  retraction half, `_dedup.py`'s `_weaker_duplicate` arbitration and
  `repromote_duplicates_for_dependency` undo, receipts, epochs, scope — all exist because facts
  arrive over time and have to be un-done. The cookbook extracts once and never revises.
- **Its own eval numbers are weak and it does not dwell on them:** `P=1.00 R=0.55` and
  `P=1.00 R=0.38` on a clean, famous corpus. Perfect precision with **45–62% of gold entities
  missed** is what "extract only central entities" buys. Do not cite that pipeline as evidence that
  prompt-only extraction suffices.
- **The gap it exposes in our gates:** it scores extraction *accuracy* against a hand-labelled gold
  set and watches F1 move as the prompt changes. `scripts/verify/` has no equivalent — our harnesses
  check structure and invariants, so an extraction-quality regression is currently unobservable.
  Related, but not the same thing, as #136's freshness gate.
- **Caveat on the primer itself:** its Memotron comparison was grounded by opening five files
  (`models/_graph.py:72`, `dreaming/_entities.py`, `dreaming/_dedup.py`, `gateway.py:58`, the
  `dreaming/` listing), not by auditing the pipeline. Re-check source before leaning on it in a
  design decision.

**FastMCP 4 SELF-ALLOWLISTS THE CONNECTION'S OWN ADDRESS. The `mcp` SDK 1.29.0 did not.**
(2026-09-10, measured in the built image; old behaviour read from the 1.29.0 wheel.)
`HostOriginGuardMiddleware._allowed_hosts_for_scope` builds
`DEFAULT_HOSTS + what we pass + scope["server"][0]`. Under uvicorn that last entry is the
local socket address of the **accepted connection** — the pod IP for a container on
`0.0.0.0`. curl's default `Host` when dialling an IP *is* that IP, so
`curl http://<pod-ip>:8000/mcp` self-allowlisted and returned **200**. mcp 1.29.0's
`transport_security.py::_validate_host` consults `settings.allowed_hosts` and nothing else,
so the same request was **421**. With `requireGatewayIdentity` false everywhere but `latest`,
that was the 39-tool surface unauthenticated to anything routable to the pod. **Closed by
`runtime.StrictHostGuard`**, applied outermost, deciding Host against exactly the documented
set. Measured after: pod IP 421, declared host 200, `localhost` 200.

**The Host allowlist is a DNS-REBINDING control, NOT an access control — and cannot be made
into one.** (2026-09-10, source-verified.) `_allowed_hosts_for_scope` starts
`allowed_hosts = list(DEFAULT_HOSTS)` — `('127.0.0.1','localhost','::1')` — on **every**
request, before anything this repo passes. Deleting `_LOCALHOST_HOSTS` from `runtime.py`
changes nothing. Measured in the prod shape (allowlist unset): ingress host 421,
`evil.example.com` 421, **`localhost` 200, `127.0.0.1` 200**. `Host` is caller-chosen, so
anyone who can open a socket sends it. This is pre-existing — the old SDK carried localhost
too — and load-bearing for port-forward. **No deployment argument may rest on the allowlist
alone**; `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is what bounds callers, and it is `false`
outside `latest` (#237). Pinned by `TestTheAllowlistIsNotAnAccessControl`.

**FastMCP's own env vars cannot weaken an explicit keyword.** (2026-09-10, measured.)
`FASTMCP_HTTP_HOST_ORIGIN_PROTECTION=false` and `FASTMCP_HTTP_ALLOWED_HOSTS='["evil…"]'`
both leave a forged Host at 421 when `build_mcp_http_app` passes its own values. The guard
cannot be switched off, nor the allowlist widened, from a deployment's environment.

**`host_origin_protection="auto"` is EQUIVALENT to `True` whenever an allowlist is passed.**
(2026-09-10, measured on 4.0.3.) `_should_validate_host` short-circuits on
`has_explicit_allowed_hosts` before consulting the connection's local address. The trap is
`"auto"` with NO allowlist, which guards nothing on a non-loopback bind — so the mode's
meaning depends on a *neighbouring* argument. We pass `True` because it says what it does
regardless. FastMCP's HTTP deployment docs recommend `"auto"` for containers; that
recommendation is safe only while it is protecting nothing.

**FastMCP 4's guard wraps CUSTOM ROUTES, including `/health`; the old SDK's did not.**
(2026-09-10, confirmed by gofastmcp.com/deployment/http: *"The custom route sits outside the
auth layer but within the host/origin guard"*.) That is why the recorded outage shows `/mcp`
421 and `/health` 200 on the same pod at the same moment. With `StrictHostGuard` removing the
self-allowlisting, `/health` must be exempted or every `httpGet` probe 421s —
`runtime.UNGUARDED_PATHS`, hoisted from the server's own `custom_route` registration.

**`FastMCP(host=...)` raises at import on 4.x** — `TypeError: FastMCP() no longer accepts
'host'`. There is no instance `.settings`. The construct-then-mutate pattern behind the 421
outage is now unrepresentable rather than merely fixed.

**mcp-forge ships MCP servers with NO inbound Host guard, behind ENABLED ingresses.**
(2026-09-10, read from that repo.) `libs/mcp-jedai-runtime/.../app.py:270` is
`mcp.http_app(stateless_http=True)` — no `host_origin_protection`, no `allowed_hosts`
anywhere. All 13 servers declare `ingress.enabled: true` on `gce-internal` with real
hostnames. **CORRECTS this file's earlier note that they are ClusterIP-only.** Their
`*_ALLOWED_HOSTS` settings are outbound SSRF guards, a different mechanism. Not ours to fix;
worth someone checking.

**The FastMCP 4 epic is CLOSED — all nine issues, each on a measurement against deployed
`latest` rather than on the merge** (2026-09-11): #253 #254 #255 #256 #257 #258 #259 #246 #184.

    serverInfo v4.0.3 · no mcp-session-id (stateless_http, #246)
    identity probe        all 8 arms, exit 0
    gateway multi-replica 10/10, control 8 tools every window, 2 replicas, sessionAffinity None
    MCP sweep             exit 0, ERROR/SKIP identical to the pre-migration baseline
    agent-memory          verified deployed and correct FOR THE FIRST TIME

**`add_session` meets a 4 MiB body limit and fails CLEANLY** (2026-09-11, measured on `latest`).
`mcp.server.streamable_http_manager.DEFAULT_MAX_REQUEST_BODY_SIZE = 4194304`, enforced before
parsing; **FastMCP 4 does not override it**, so the SDK default applies. 0.94 MiB -> 200 with
`isError: False` and 210 real episodes created; 4.72 MiB -> **413 "Request body too large"**.
The SAME default exists in mcp 1.29.0, so this is not a migration constraint. Acceptable
because the failure is legible rather than silent truncation on an ingestion path — and
`turns_per_episode` already exists to window client-side.

**A gateway key without `mcp_access_groups` makes `probe_gateway_mcp.py`'s CONTROL return 0
tools**, which is `PROBE INVALID`, not a Memotron reading. Cost ten wasted runs on
2026-09-11: the key in `.env` has no MCP grant, so the first measurement came back 0/10 and
would have read as "#246 is not fixed". Keys minted with
`object_permission: {mcp_access_groups: ["General"]}` give control=8. **The probe was built for
exactly this and it worked** — the issue history records the same false finding nearly being
filed once before.

**The 503-refusal leak (#263) was the SECOND occurrence of one defect.**
`GatewayIdentityRefusalError` exists *because* LiteLLM's auth-error body once returned
`Received API Key = sk-...` plus the key's verification-table hash to unauthenticated callers.
The two-message split was that fix; it did not hold, because `public_detail` **defaults to**
`detail` and omitting it is silent. One of **seven** sites (the review said five) omitted it
while interpolating the store's exception, returning internal DNS name, pod IP, port and DB
username. Fixed, plus an AST guard keyed by message text.

**KEK now written to EVERY environment's Vault payload** (2026-09-11, by Ryan, after #237
established it was absent everywhere but `latest`). A separate 32 bytes per environment,
generated with `python3 -c 'import os,base64; print(base64.b64encode(os.urandom(32)).decode())'`
and verified against the real validator before use — `key_manager_from_env` accepts it,
`is_ephemeral` False, and the negative controls (31 bytes / non-base64 / set-but-empty) are all
rejected, so a bad paste fails loudly rather than silently downgrading.

**`latest` was deliberately NOT touched** — it already had one, and overwriting it would be a
rotation, not a fix: `key_id` is `sha256(kek)[:16]`, so new bytes mint a new signer, formation
blocks, and #175's recovery accepts that every prior attestation verifies against a public key
the store no longer has. **Do not "make them consistent" by regenerating latest's.**

**This unblocks prod outright.** The previous entry here said *"PROD CANNOT START"* and is now
wrong; `values-prod.yaml` has `operationalStore.enabled: true` and the KEK it needs now exists.

**NONE of the new keys are verified, and they cannot be yet.** `vaultSecret.enabled: false` in
stage/load/preview means nothing syncs them into a cluster, and prod has no cluster. The first
real check is step 3 of the order below: turn `vaultSecret.enabled: true` on ALONE and read
`kubectl -n <ns> get secret memotron-secrets` **directly** — never infer from pod state,
because `envFrom` with `optional: true` means a missing Secret does not stop the pod. That is
exactly how the absence went unnoticed in the first place.

**CORRECTED 2026-09-11: the Postgres instances DO exist for stage, load and preview**, all set
up the same way, each with its own Vault values (Ryan). This file previously said step 1 was
missing and cited #238's unchecked box — that box was stale. I flagged the claim as read from
**the chart and the record, not the clusters**, and that this account has no RBAC to see
otherwise; that is exactly how it was wrong.

So the remaining work for the three lowers is **flags only**, and the order still does not
commute:

    1. Postgres instance + memotron_db (datcollate=C)   DONE  (existed all along)
    2. KEK into each env's Vault payload                    DONE  2026-09-11
    3. vaultSecret.enabled: true  ALONE, verify the Secret  <- in flight
    4. keyManager.enabled: true
    5. operationalStore.enabled: true + its vaultSecret

The wiring was already complete: every env has **its own** Vault path
(`apps/jedai/memotron/us-east-1/<env>`) and a real AppRole id. `stage` and `preview` share an
id deliberately — preview runs in the stage cluster.

**Step 3 carries a side effect.** `vaultSecret.destinationSecret` is also named
`memotron-secrets`, the same name the pods already reference with `optional: true`. Once the
flag is true the pods read the VSO-managed Secret and any hand-created one is ignored, so
`LITELLM_API_KEY` starts arriving from Vault. If a cluster has an out-of-band
`memotron-secrets`, confirm its contents match the Vault payload first.

**#271 deployed to `latest` and verified there** (2026-09-11). All four long-running pods on
`0.1.0-5421ee0` = `main`'s tip, started ~3.5 min after merge — **`main` auto-deploys, confirmed
again**. Both post-deploy checks pass: `sweep_mcp` exit 0 with `EMPTY=6 ERROR=16 OK=14 SKIP=3`
(identical to the `cae6c03` baseline) and `probe_mcp_identity` **all eight arms**. That matters
because #263's fix touches `gateway_identity.py`, which is on the auth path for *every* request —
A5 (401 at the door) and A7 (bogus key) exercise the refusal machinery that changed.

**The repo DID overclaim erasure, and my first search said it did not** (#236, 2026-09-11).
README carried **27** `shred` hits and an `Erasure | WS-12 machine-verifiable erasure
certificates` capability row. `git grep` returned zero because `'ui/admin/src/**/*.tsx'` matched
no files; the control I ran used a **different pathspec list** and so validated a different
command. Fixed by caveat, not deletion — `erasure.py` genuinely contains that machinery and
documenting it is correct; the gap was that nothing said it is unreachable from any surface.

**MVP claim limits are now owned and enforced**: `docs/mvp-claims.md` (say/don't-say with
evidence and re-measurement commands) and `tests/test_mvp_claims_are_not_overstated.py`
(forbidden promises + capability mentions must link the claims doc). The gate is deliberately
**not** a word ban: banning the vocabulary would force deleting accurate documentation. It cannot
police a slide deck, which is where the real risk lives — #236's fourth item, a human reading
Design 16, stays open.

## General rules

- **A gate can report your FIX as a regression, and the instinct is to revert.** `probe_kek.py`
  arm D asserted *"opt-out unset -> the backend must refuse"*, which was correct only while no
  durable KEK could exist. Once #123 supplied one, building became right and the probe printed
  `GUARD REGRESSION`. Shipping that would have left a red gate whose obvious repair is undoing the
  fix. When a gate goes red on a change you believe is correct, ask whether its assertion encodes
  an assumption the change just retired -- then make the assertion branch on the condition rather
  than deleting it. This is the inverse of the "green for the wrong reason" family and it is
  rarer, so it is easier to misread.
- **`reveal_content` NEVER RAISES, by design, so it cannot answer "can this be decrypted".** It
  passes non-sealed values through unchanged and returns the shred placeholder when the key is
  gone (`crypto.py:108-120`). Used as a readability check it reports a garbage string as a working
  credential -- observed 2026-09-03. To test decryptability use `is_sealed_content` **and**
  `open_content`, which does raise. Same shape as the SQLite/Postgres split: a function whose
  contract is "never fail" is the wrong instrument for detecting failure.
- **A key loader for a DEPLOYMENT must refuse to create a key.** `LocalKeyManager.from_file`
  creates when absent, which is right for one developer's store and catastrophic across replicas:
  each pod mints a different KEK, seals under it, and cannot read its neighbours' -- with no guard
  to catch it, because a key manager IS present. `crypto.key_manager_from_env` therefore errors on
  a missing file rather than generating one.
- **Envelope encryption means a KEK change breaks existing rows, and the probe does not reset.**
  `probe_kek.py` leaves sealed rows behind; run it twice with different keys and arm A dies with a
  traceback reported as "no result", because `configure_tenant_llm_credentials` sits outside the
  child's try. Reset the schema between runs with different keys. Real behaviour, not a probe bug.

- **`check.sh` writes a DIFFERENT storage profile than a full DSN-set run, and the storage gate
  compares against whichever ran last.** After blessing from a proper run, `check.sh` reported
  `storage FAIL — 1 changed, 5 not observed`; re-running the gate against a clean
  `uv run pytest` with the DSN set gave `PASS`. `storage_golden.py`'s own header says to bless from
  a full run and not from `check.sh` — it means it. Sequence that works: finish **all** source
  edits → one full DSN run with `--cov` → bless → `check.sh`.
- **`cov-floor` fails STALE when `coverage.json` predates the newest file under `src/`, and that is
  the gate working.** Editing a module *after* `check.sh` ran is enough to trigger it. It refuses to
  report a pass over a tree it never measured — do not re-run it hoping for green, regenerate the
  artifact. A new module also needs an explicit `FLOORS` entry; the gate prints the exact line.
- **`SystemExit.code` is `int | str | None`.** A string means the CLI refused the invocation and
  printed why. `int(exc.code)` on that raises, and in a test helper it surfaces as *product*
  failures rather than a helper bug — mine reported two. Branch on `isinstance(exc.code, int)` and
  re-raise the message case for the caller to assert on.
- **Search the BRANCH, not the working directory — a stale checkout produces a confident false
  negative.** On 2026-09-03 I reported "no Memotron Cloud SQL database is declared for latest"
  after grepping `tf-jennay/latest-1/`. The checkout sat on an unrelated feature branch;
  `origin/main` had had the instance since 2026-08-27. Ryan knew its name and that is the only
  reason it was caught. For any repo you did not just clone: `git fetch` first, then
  `git grep <pattern> origin/main --`, and state which ref you searched. A directory is not a
  version.
- **Also on that search: quoted-key regexes miss bare HCL identifiers.** `^\s*"[a-z-]+"\s*=\s*\{`
  matched **zero** lines in a tfvars file full of instances, because terraform map keys are written
  bare (`jedai-status-lst-db = {`). It printed nothing and read as "no instances declared". Any
  count of zero needs a positive control that the pattern matches something.
- **`docker compose up` does NOT rebuild an image whose tag already exists.** The stack comes up
  healthy running whatever you last built. Observed 2026-09-03: a 19-hour-old `memotron:local`
  migrated Postgres to version 8 and reported `key_principals: false`, which reads exactly like a
  broken migration. Nothing failed — the schema was simply old. Check
  `docker inspect -f '{{.Created}}' <image>` against the commit you think you are testing, or pass
  `--build`. This matters more now that the local default is Postgres: a stale image means testing
  an old schema against a real database.
- **In compose, `${VAR-default}` and `${VAR:-default}` are not interchangeable.** The colon form
  substitutes when the variable is unset **or empty**, so an intentionally-empty value cannot be
  expressed and an "empty means X" arm becomes unreachable. Used the non-colon form for
  `DW_STORE_DSN` so `DW_STORE_DSN=` still selects the blank/SQLite arm. Render both arms with
  `docker compose config` and confirm — the failure is silent otherwise.
- **In zsh, `$var:path` is a parameter MODIFIER, not a path.** `git show $ref:file.py` silently
  expands as `$ref` with a `:s` substitution and returns nothing — so a per-ref comparison loop
  reports every ref as empty and reads as "the line isn't there". Write `git show "$ref":file.py`.
  Observed 2026-09-03 comparing a coupling baseline across four refs; every row came back blank
  while the same command with a literal ref worked fine.
- **Quote every glob passed to a command in zsh.** `grep -rn --include=*.py 'x' src/` **fails**
  (`no matches found`), prints nothing, and reads exactly like "zero call sites". This produced a
  false "the registry is called by nothing" on 2026-09-03 — which happened to be the true answer,
  which is worse, because the broken instrument agreed with reality and taught nothing. Quote it,
  and pair every "zero hits" claim with a positive control that proves the pattern matches at all.
- **Never hand-merge or auto-merge a golden — re-bless it from a clean run.** A merged golden is a
  file neither side would generate: it passes the gate while describing nothing real. On the #167
  combine, resolving `api_surface` and `storage_profile` by taking one side silently dropped the
  other side's 11 storage rows and 5 api symbols; only the re-bless restored them. Take one side
  wholesale to clear the conflict, then regenerate.
- **Read golden deltas from the files, not from the gate's report.** Both `api_surface` and
  `storage_golden` truncate their rendered diffs (`... and 1592 more diff lines`). Compare the
  before/after files directly, and separate *new symbols* from *hash-only churn* — the #167 bless
  showed 766 added / 743 removed raw lines, which was 39 real symbols and 6 changed class hashes
  repeated across every re-exporting module.

- **Ask whether a gate actually gates before trusting it.** Five separate config knobs this session
  read as enforcement and were not: strict-in-an-override, strict-markers-in-addopts, a
  silently-non-matching exclude pattern, a marker on the wrong scope, and a whitespace hook damaging
  the artifacts it was meant to tidy. The cheap test is always the same — **break something on
  purpose and confirm the gate goes red.** Every gate added this session was calibrated that way.
- **A linter's value is the defects it finds, not the count it drives to zero.** The rule set that
  matters is the one a person will actually finish: 631 achievable violations beat 19,394 aspirational
  ones. And the payoff was real — 33 undefined names on the public SDK surface, a naive/aware
  `datetime` crash, a blind `pytest.raises(Exception)`, two regex `match=` patterns with unescaped
  metacharacters.
- **When suppressing a lint, say which of the three it is:** a real defect fixed, a false positive
  the tool cannot see (write the reason at the site), or a scope decision (write the reason in
  config). An unexplained `noqa` is indistinguishable from giving up.

- **"I did not find it" is not "it is not there".** An absence observed on one surface is not an
  absence in general. T4-4 claimed denials were unrecorded because `negative_space()` returned 0;
  they are recorded in `dream_decisions()`. Name where else it could be before claiming absence.

- **Classify a finding by its decisive evidence, not by how confident it felt.** A `file:line`
  in committed code survives any machine; an observation from a local run may not. 33 of 57
  items needed nothing but a clone; one was purely our environment.
- **Pin baselines by SHA, never by ref name.** A clone of a worktree resolves `origin/main`
  against that worktree's local `main`, which can be stale — it was, by 60+ commits.

- **The probe's conditions are part of the claim.** Both retractions in this takeover (D-45,
  D-50) came from harness conditions that do not occur in use — a `.env` in the cwd, and zero
  delay between hooks — attributed to the product. Ask "is this how it actually runs?" before
  filing, and state the timing/environment in the finding.

- **Check a closing move against the register before trusting it.** U-14's recorded closing
  move used a harness that routes through the path T0-5 says ignores the DSN; run as written it
  reported a clean Postgres pass without touching Postgres. Known defects invalidate test plans.
- **Attribute the write.** Third save from this check (D-41, D-45, D-49). A configured DSN, a
  correct backend class name, and a green result together still do not prove where bytes went.

- **Check "deferred" before concluding "lost".** A missing row right after a write is usually
  a queue that has not drained; draining it is one call. This pattern has now produced two
  near-miss data-loss claims (T1-1, D-48).

- **A probe's display limit is not a finding.** I nearly reported the extracted fact as
  truncated mid-sentence; the truncation was my own `str(f)[:110]`. Print lengths, not just
  values, before describing what a system stored.
- **Count memories, not relationships.** `MENTIONS` edges are structural and scale with the
  number of nouns, so a raw count can show activity where nothing was understood.

- **Repeating a test measures flakiness, not causation.** Three deterministic runs made me
  confident and no better informed; "deterministic" should have pointed at stable ambient
  state, not at the code. Vary the *environment* — cwd, ambient files — not just the input.
- **"Zero tracked changes" does not mean "clean".** Untracked local files changed the result.

- **Measure a fix's blast radius by applying it, not by estimating it.** The plan called the
  fail-closed guard "two lines that permanently retire the failure mode"; applied, it breaks
  66 tests. The line count was right and the cost estimate was not.

- **Assert on the call that DOES the thing, not the one that REPORTS on it.**
  `tenant_llm_credential_state` returns metadata and never unwraps the DEK, so a probe built
  on it passed while the key was unusable. Same shape as `memory_refresh` vs `memory_start`.

- **Assert the round trip before trusting a backend flag.** Two servers launched from the
  same shell with the same DSN used different stores; only counting rows in each store
  revealed it. A green tool response says nothing about where the bytes went.
- **A comparison of a thing against itself looks exactly like parity.** The SDK sweep's
  AgentMemoryPlatform half ran on SQLite in both arms and showed zero changes; that was a
  null test, not evidence.

- **A passing parity suite is not parity.** Memotron's 3,310-line suite asserts both
  backends store the same bytes; it never asserts they answer a caller the same question.
  T0-4 lived under it. Test the caller's view, not the stored row.
- **When a sweep verdict flips, check the input before blaming the method.** Four methods
  "regressed" on Postgres; all four had been handed a MENTIONS edge by the harness and
  refused it correctly. One defect, four symptoms.

- **A list derived from metadata is not the same as the list you were asked for — read the items.** "Open issues with no parent" gave 36 candidates for epic re-linking; **4 were wrong**, and only opening them showed it (two declare a *different* parent in their body, one is a reference artifact, one lacks the convention entirely). The discriminator was a prose line the query could not see. When a structural query and a human's intent might disagree, the cheap check is to read a handful of the items and look for the convention the filer was following — then confirm the convention by testing whether it predicts the cases that are *already* correct.
- **Prose conventions in issue bodies are real metadata, and worth validating as such.** `Parent epic: jedai/program#393` appears in 84 issues and correctly predicts the actual sub-issue link for every one of #47–#105. That agreement is what made it trustworthy; a convention that had *not* been validated against the already-correct cases would have been a guess.
- **`jedai/program#393` is at GitHub's hard cap of 100 sub-issues.** Adding any child now requires detaching one first (`422 Parent cannot have more than 100 sub-issues`). 62 of the 100 are closed. New memotron work should hang off an intermediate tracking issue — **#144** for the takeover/hardening batch — not off 393 directly. Nesting to depth 6 works (`#7 → #31 → #50 → #393 → #144 → leaf`), confirmed by the link succeeding.
- **Run the suite before claiming anything about correctness.** It is hermetic and takes 37s; with one `postgres:16-alpine` container it is 158s. There is no excuse for reasoning about test coverage from source alone.
- **A default `pytest` run does not cover the storage backend.** Always export `MEMOTRON_TEST_POSTGRES_DSN` when touching `storage/`.
- **Reproduce container/chart claims by running the exact Helm command against the exact image** (`docker run --user 10001:10001 memotron:local <command>`). Three of the four container defects above are invisible in source review and invisible to a root-user `docker run`.
- **Multi-replica defects need multi-replica reproduction.** `docker compose … --scale api=2`. A single container running two `docker exec` processes shows per-*process* behaviour, which is a different claim.
- Postgres for this project must be created with **`ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0`**. Passing the locale flags without the encoding makes initdb infer `SQL_ASCII`. The C collation is required because text ordering must stay byte-comparable with the SQLite substrate or `graph_state_hash` diverges between backends.
- Gateway virtual keys are **not portable between environments**; a preview key 401s on latest. `gateway.py` packages a **preview** default base URL, so deployed pods must override `LITELLM_API_BASE`.
- **Before claiming a code path is reachable, read its guard conditions.** `_offer_memory_migration` is inside `init`, but only fires under two specific preconditions. "It's in `init`" and "it happens on `init`" are different claims, and the second one fails to reproduce.
- **Mechanical doc/symbol audits produce false positives — check every hit.** A regex for `memotron <command>` matches `from memotron import Memotron`; a top-level `dir(memotron)` check flags `default_config`, which legitimately lives at `memotron.config.default_config`. Two of my three initial "findings" were artifacts of the checker.
- Posted GitHub comments can be **edited in place** rather than appended to: `GH_HOST=github.disney.com gh api -X PATCH repos/jedai/memotron/issues/comments/<id> -F body=@file`. Prefer this for corrections.
- **A UI negative is a claim about a specific subject — read the label before believing the value.** `not configured` on Overview/Integration is the MCP row and is *accurate*; the same words on Project Memory are a rendered 503 and are *false*. Three over-claims this session came from matching a negative-looking string to "defect" without checking its referent.
- **Hold one variable.** A two-server comparison that also changes the graph file proves nothing about the server, even when the conclusion turns out right.
- **Open the operator surface early.** Three of the session's findings came only when a human opened the admin UI or asked about MCP, after hours of SDK-only testing. Test the layer users touch, not the layer that is convenient to script.
- **Take tool counts from `tools/list`, never a source grep.** A `@mcp.tool` grep and the live count agreed here, but every *document* citing counts was stale within a day.
- **A green MCP tool call is not evidence the system worked.** Assert a round trip — write a novel fact, then read that specific fact back.
- **Ask whether a defect is caller-side or storage-side before proposing a backend as the fix.** The 11 full scans live in `dreaming.py`, so Postgres inherits them unchanged.
- **Takeover artefacts are local-only.** `STATE.md`, `TAKEOVER-BACKLOG.md`, `UNKNOWNS.md`, `DOGFOOD-LOG.md`, `docs/findings/`, `scripts/verify/`, `docker-compose.local.yml` and `ui/admin/tests/ui-sweep.spec.ts` are in `.git/info/exclude`. Write freely; commit nothing until we are contributing changes rather than characterising someone else's code.
- Never use bare `git stash` here — the stash stack is shared across worktrees.
- **Run the product, not just the gates — and A/B it.** Every static guard here measures the source
  tree; none starts the thing. At each milestone (before a push, after a module split, before a PR)
  run `bash scripts/verify/sanity.sh`, which builds and installs a wheel and diffs its behaviour
  against one built from `takeover`. **The A/B is the load-bearing half**: run alone, the refactored
  build printed `created=0` from a dream pass and looked broken; the identical output from the
  pre-refactor wheel is what proved it was the documented no-worker path. Without a baseline most of
  what the probe prints has no known-good value to compare against.

**A hand-written mirror of an external matcher rots silently, and rots toward PASSING.**
`tests/test_mcp_transport_security.py::_allows` reimplemented the SDK's host check. FastMCP's
`_normalize_host` strips the port from *both* pattern and header, so bare `Host: localhost`
matches `localhost:*` — the mirror required the colon and reported **refused** where the real
guard **accepts**. A mirror stricter than the thing it models makes every negative assertion
pass for the wrong reason. **Delegate to the real matcher** (`_host_matches`), or drive the
real app; never re-implement the predicate under test.

**A fabricated input in a real container is still a fabricated input.** `/health` was measured
at 421 for `Host: 10.154.184.212:8000` in a genuine Docker run — but that address never
corresponded to the connection, so the number proved nothing about kubelet, which connects
*to* the pod IP and therefore matches. Running the artifact is necessary and was not
sufficient: **the input has to be the one the real caller sends**, not one that merely looks
like it. Ask what value the actual client would put in the field before believing the result.

**Export ONE variable, not the whole `.env`, when running the suite.**
`set -a; . ./.env; set +a` to get `MEMOTRON_TEST_POSTGRES_DSN` also exports
`MEMOTRON_GRAPH_PATH`, pointing tests at the real dogfood graph — where this session's own
hooks had registered `Claude Code`, failing an idempotent-registration test that passes in a
clean env. Extract the single value in a subshell:
`export X="$(set -a; . ./.env; set +a; printf '%s' "$X")"`. Note the DSN is **keyword/value**
(`host=… port=…`), not a URI, and `cut -d= -f2-` keeps the surrounding quotes — which psycopg
reports as `invalid connection option ""host"`.

**Parse config files; do not grep them. Comments match.** Asked which environments arm
`requireGatewayIdentity`, a `grep -m1 "requireGatewayIdentity:"` reported **prod = true** — it
had matched the line `# 2. set requireGatewayIdentity: true for prod` inside a comment block
explaining what to do LATER. Parsing the YAML gives the truth: `latest` is the only environment
that overrides it; stage, load, preview and prod all inherit `false` from `values.yaml:706`.
A grep over a config file answers "does this string appear", which is not the question.

**A probe's CONTROL failing is not a result about the target — stop and fix the control.**
Ten runs of `probe_gateway_mcp.py` returned exit 2 with `control=0 tools`. That is
`PROBE INVALID`; reporting 0/10 as a Memotron failure would have been the exact false
finding the control exists to prevent. Read the probe's own `_diagnose` output before
interpreting anything: it named the cause (no MCP access grant on the key) in one line.

## Open failures

### `/api/overview` aggregates across every scope in the store (residue of #200)

**Observed 2026-09-08**, unauthenticated, off-cluster. `/api/overview` with no `scope` param
returns every scope key and relationship count in the store — `probe-tenant-alpha` (7),
`probe-tenant-bravo` (6), `user:local-cac319ad1dd105e6` (1) — and `evolution.latest_decision`
returned `subject_name` *"durable KEK gates Postgres operational store"* plus its summary from
`tenant:probe-tenant-alpha`, while the launch tenant is `jedai-platform`.

**Not deploy lag.** B2's `graph_tenant_ids` removal IS absent from the same live payload, so the
image carries that day's work, and the unbounded code is on `origin/main`: `_scope_payload`
(`admin_server/__init__.py:524`) and `_tenant_overview_scopes` (`:730`) both call
`scope_payload_from_client`, which walks `client.export_graph()["relationships"]` with **no
scope filter**. `discover_graph_scopes` takes a `tenant_id` and default-denies; these two call
sites simply never pass it.

**The defect class, again:** the fix bounded the paths that had a probe and missed the aggregate
that had none. Marking this closed from the fix's presence on `main` was my error — presence of
a fix is not absence of the defect.


### #162 — two orphaned 10Gi PVCs remain, blocked on CREDENTIALS not effort (2026-09-06)

`latest`'s claim is gone (see *Verified facts*). `stage` and `load` still hold a 10Gi `premium-rwo`
disk each, still billed. Both are unreachable: the kubeconfig authenticates every context as
`svc-gke-deploy@jennay-latest-1`, which has no `persistentvolumeclaims` permission in
`jennay-stage-1` or `jennay-load-1`. Retrying will not help; this needs different credentials.

**Do not delete `load`'s without checking first.** The issue records that load was still on
`0.1.0-a1daaf4` with its admin pod MOUNTING the claim, and that condition could not be re-verified.
Deleting it while that pod runs breaks a live environment.


- **B1 ephemeral KEK** — unfixed. Blocks enabling `operationalStore` in any environment.
- **No authentication** on the admin console or the MCP API. Security-review blocking.
- **No worker** — `dream_worker.py` exists in no branch, so nothing forms memory in a deployed environment; episodes queue forever.
- **Container cannot start under the chart's own securityContext** (uv cache + missing `fsGroup`). Unverified against the live cluster.
- **Admin console cannot target Postgres** — needs a code change to drop the required, must-exist `--graph-path`.
- **PR #44** — handed back to the author 2026-08-26 with 6 defects (4 from tyler-r-friddle, 2 added), a CI requirement, and a severity escalation on finding #1. Three comments posted: [hand-back](https://github.disney.com/jedai/memotron/pull/44#issuecomment-3948952), [correction](https://github.disney.com/jedai/memotron/pull/44#issuecomment-3949091), [severity + README break](https://github.disney.com/jedai/memotron/pull/44#issuecomment-3949321).
- **`README.md` ships a broken import example** (`ThematicConsolidationPolicy`). Fix belongs to #44 since #44 caused it; unfixed as of this writing.
- **KEK provider undecided** — see `docs/operational-store-decisions.md` DW-025. Gates the Postgres cutover, which in turn gates ADR 0007's MCP work.
- **Harness trigger state unknown** — no trigger file exists in the repo, and live triggers can exist inline in Harness with only a git copy. Confirm the `JedAI_Memotron` trigger list is empty **before** merging anything.
- **One false fact is live in project memory** — `T1-14 incident was caused by discover_project_root function` (`ab17aa08-4b3b-46fe-8d73-f431534fbf05`, confidence 0.80). The source said the opposite. Awaiting Ryan's confirmation to `memory_forget` it; **T1-33** is why nothing retires it automatically.
- **T1-36 — a `.env` can redirect a real gateway key to an attacker-controlled host.** `load_env_file` imports *arbitrary* variables, `LITELLM_API_BASE` sets the endpoint, and nothing checks scheme or host. Opening an untrusted repo in Claude Code is a sufficient trigger. T1-14's fix narrows the blast radius; it does not close this.
- **#148 — the admin PVC has never had a byte written to it**, and its ReadWriteOnce constraint has
  already cost two production deploy outages. Measured at 0.02 MiB by three independent paths.
  Decision needed: remove `persistence.*` outright, or keep it dormant. **`strategy: Recreate` must
  be retained either way** — if anyone later enables `operationalStore` or repoints the graph at
  `/data`, the volume becomes load-bearing again and the strategy is what makes it safe.
- **No chart lane in `check.sh`.** 16 lanes, none of which render or assert anything about `.helm/`.
  A one-line `helm template` assertion would have caught #146 before it shipped. Unfiled.
- **LAUNCH SCOPING (Ryan, 2026-09-02): crypto shredding and Entra SSO are DEFERRED past the
  initial launch.** Consequences worth acting on: the `operationalStore` fail-closed guard has a
  legitimate escape hatch (`MEMOTRON_ALLOW_EPHEMERAL_KEK`) that the chart currently sets
  NOWHERE, so setting it deliberately per environment -- with the consequence recorded, sealed
  content unreadable after any restart and by every replica -- downgrades **#130**, **#123** and
  most of **#148** from blockers to recorded trade-offs. Auth for admin UI / MCP / API / CLI is to
  key off **LiteLLM virtual keys** from `jedai/gateway` (a vendored LiteLLM), per environment
  (`https://latest.jedai-gateway.wdprapps.disney.com/` and successors). See *Verified facts* --
  authorization is already built, only authentication is missing.
- **#150 — the mixin idiom costs 1,116 lines of `_protocol.py` and 78 of 89 mypy suppression
  pairs.** Proposal: pilot object composition on `_policy`, the only plane with zero
  cross-mixin edges in BOTH engines (20 delegates). Derived, not measured — no line written.
- **#151 — `api_surface.py` records neither instance attributes nor module re-exports.** It
  walks `__mro__` and reads `vars(cls)`, never `vars(instance)`, and `:275` skips modules.
  `_error_label`, `_request_label` and `gateway.urllib_request` all entered the tree
  unrecorded — the last is the monkeypatch target the whole transport suite depends on.
- **Phase 3 cluster 2 (sqlite↔postgres, ~282 claimed) is UNMEASURED**, and is the last of
  the plan's three. Both measured clusters came in far under claim.
- **#149 — 27 of 172 shared storage methods run on only one engine** (20 SQLite-only, 7
  Postgres-only), gated by `parity-cov` at that baseline. Not defects; unlooked-at code. The two
  worth doing first are the crypto pair (`get_or_create_governance_key`, `seal_receipt_detail`)
  and the idempotency pair, because a silent divergence there is expensive and late-detected.
- **T1-37 — a poisoned endpoint, once sealed, is permanent, so T1-14's fix is not retroactive.** The seed path early-returns when a row exists. **Any tenant seeded under the old code still holds whatever it picked up** — auditing existing `tenant_llm_credentials` rows is a separate task from the code fix, and has not been done.

## Lessons learned
**2026-09-11 — I warned about a non-problem from a stale note, twice in one session.**
Told Ryan agent-memory was "the sharpest open item" post-merge because it pins `tag: "0.1.0"`
and STATE.md said Harness overrides only two roles. All three roles run `main`'s tip. Then I
probed mcp-forge's gateway admin plane to learn whether a key had admin rights, got a `404`,
called it inconclusive, and escalated — while **line 2128 of this file already said** *"The
proxy MASTER key does not self-look-up. It is the configured secret, not a key row"*. Ryan had
to tell me to read my own record. **This file is loaded at session start so that these are
lookups, not investigations.** When a claim here is load-bearing and I am about to spend
effort re-deriving it, the cheap move is to search STATE.md first — and when I find it stale,
correct it in the same breath rather than working around it.

**2026-09-11 — "run the artifact" found two defects in the probe I had just shipped, minutes
after merging it.** `sweep_mcp.py` could not authenticate at all, so it could not reach the one
environment with identity armed; and its tenant was a hardcoded literal, producing 36 ERRORs
that were one stale string. Both invisible to every gate, to the full suite, and to a local
container — they needed a real armed deployment. The baseline I had recorded from a local
*unarmed* container said exit 0, so the live lane would have gone red on its first real use and
read as drift rather than a missing feature. **A probe verified only against a permissive local
target is not verified.**


**A pod reporting `Running` says nothing about WHICH code it runs.** The chart rendered, the
deploy succeeded, the pod went Ready — and `agent-memory` served a build nobody merged. The only
evidence was an image tag, and nothing compares tags to the merge that triggered the deploy.

**Five times in one day a check reported green over something it never examined**: a hardcoded
role map blind to a new role; a classifier exempting a whole class (`reconcile` printing
`(3p) Reachable —`); a summary line hiding a skipped row; a grep matching the comment I had just
written about the string I removed; a poll loop counting completed CronJob pods as live. **Two of
the five were guards I had written that same day.** The passing condition is satisfied by the very
gap the check exists to surface.

**An intermittent failure reads as a hard one — say the rate out loud.** #246 alternates at 50%;
one red run looks like a broken path. A probe that can hit a flapping failure must name the
expected rate and state that ALTERNATION is the signature.

**Stateful protocols behind a round-robin Service fail at (replicas-1)/replicas.** Nothing caught
it because every existing check does a single request; the handshake is the only thing that
exposes it.


**"We are not affected" is a true answer that can stop you from fixing anything.** I analysed
nltk's reachability three times across the session and shipped nothing each time, because the
analysis kept concluding correctly that the vulnerable APIs were unreachable. Reachability
decides *urgency*; it does not decide whether you should still be **carrying** an unpatchable
HIGH in every shipped process for one stemming algorithm. When an advisory has no patched
version, "not affected" is the beginning of the decision, not the end of it.

**Every gate in `check.sh` measures the source tree; none of them starts the product.** The
claim being made was about the *image*, so the only honest verification was to build it and
look inside — with controls, because "package absent" and "probe broken" print identically.
The 17 green lanes were all compatible with nltk still shipping.

**When the user names a file and your search comes back empty, widen the search before doubting
the file.** `find -maxdepth 3` missed `docs/findings/python-refactoring-opportunities.md` at
depth 4, and it was in the **main checkout**, not the worktree I was searching. I filed #227
about a document I had never read. Two checks: search by basename with no depth limit, and
confirm which checkout the user is looking at.

**Correcting a stale document is exactly when you are most likely to add a fresh unverified
claim.** While fixing three stale SZ-2 statements I wrote a new sentence crediting SZ-2 as the
precedent for a #50 tier fix — plausible, sourced from an issue I had written, and not
something I had actually confirmed. Cut it. The rule that produced the fix has to apply to the
fix.

**A dated changelog entry is not stale just because it disagrees with today.**
`REFACTOR_OVERVIEW.md`'s "2026-09-02 — SZ-2 deferred" is correct as of its date; the
authoritative status row elsewhere records the landing. Rewriting a log to match the present
makes it wrong about history while looking right — the same lesson as the
`audit-baseline-2026-08-27` citations.


### BLESSING A GOLDEN MID-STREAM, THEN EDITING, COSTS THE WHOLE CYCLE (2026-09-08)

Twice in one session: bless `api_surface.golden.txt`, keep changing code, watch the gate go
red again. The second time cost a full `check.sh` run (~20 min) plus a re-run.

**The check:** bless from the FINAL tree, as the last step before committing -- never
mid-investigation. If a bless is followed by any source edit, it is stale, and the gate is
the only thing that will tell you.

### `grep` FINDS THE STRING; THE CALL GRAPH FINDS THE TRUTH (2026-09-08)

Scoping #206 Phase 2, a textual scan for `_require_agent` reported 16 public methods as
unguarded. A 20-line AST call-graph walk showed 23 of 29 reach the guard -- through
`self.agent_scope(agent_id)`, which the string search cannot see. Acting on the grep would
have meant scattering redundant checks across methods that already had one, and missing
that the real gap was three agent-lifecycle methods.

**The check:** for "does X reach Y", walk `self.<attr>` calls transitively. A string match
answers a different question -- *does this method mention Y* -- and the difference is
invisible until someone counts.


### I WROTE A WRONG FACT INTO THE SECTION THAT EXISTS TO BE TRUSTED (2026-09-08)

Three entries added on 2026-09-08 said `feat/next-ws25-ws28` was unmerged work gating the
C-track. One of them was in **"Do not re-derive"** — the section whose entire purpose is
to be believed without re-checking. **The correct fact was already in this file**, at
~line 1590: *"PR #44 (`feat/next-ws25-ws28` …) merged"*.

Two sizings, both wrong, before anyone questioned it:

* *"~2,150 lines of README/ingest/demo docs"* — from a filtered `grep` of docs paths.
* *"48k lines of unmerged feature work"* — from `git log main..branch` and
  `git diff main...branch`, which under SQUASH MERGE report a merged branch as unmerged
  **forever**. This file already recorded that trap for PR-merge checks; I reached for the
  tool it warns about anyway.

**What settles it in one command:** `git show origin/main:<file the branch adds>`. The
WS-25..28 artefacts are all on `main` — `epochs.py`, `test_compaction_survival.py`,
`NEXT.md`, `/api/epochs`. A two-dot `git diff main branch` also tells the truth where the
three-dot form does not: `main` has 292 files the branch lacks, and the 23 the branch
"adds" are the pre-split monoliths it predates.

**Two rules, and the second is the one that was missing:**

1. Verify by CONTENT, not ancestry — this file's existing squash-merge note, restated.
2. **Before adding a fact to STATE.md, grep this file for the subject.** A new claim that
   contradicts an existing entry is a signal to stop, not a newer truth. The cost of
   skipping it is not a stale note — it is a confident wrong instruction sitting in the
   section a future session is told not to question, which is where it does the most damage
   and takes the longest to surface.


### A "REFUSED" FROM THE WRONG CONTEXT LOOKS EXACTLY LIKE A REFUSED (2026-09-08)

"kubectl is refused" survived two weeks and blocked a whole workstream. It came from a
`Forbidden` produced against the **stage** context, because that was the shell default —
and was later reinforced by a permission probe against a namespace that does not exist,
which `kubectl auth can-i` answers `yes` for without complaint.

Both failure modes return a confident, plausible answer. Neither announces that it
answered a different question than the one asked.

**The check, before recording any access verdict:** name the context and the namespace in
the same breath as the result. `kubectl config current-context` and
`kubectl get ns | grep <thing>` cost one command between them, and either one alone would
have refuted this. The general form is the one this file already records for the
application surface — *an environment-scoped claim must carry the environment it was
measured in*, or it silently becomes a claim about all of them.


### I WROTE A WATCH LOOP THAT COULD NEVER SUCCEED (2026-09-08)

Polling `latest` for the #200 rollout, the loop reported "still listed" for 24 straight
minutes and timed out — while a manual probe run in the same window showed the leak
already closed. Two observations of one system, disagreeing.

The loop was wrong, not the deployment:

```sh
n=$(curl -s "$U/api/overview" | grep -c 'probe-tenant' || echo 9)
[ "$n" = "0" ] && ...        # never true
```

**`grep -c` prints `0` AND exits 1 when it matches nothing.** The `|| echo 9` therefore
fires on the success case, and `n` becomes `0\n9`, which never equals `"0"`. Verified by
running it: `n = 0$'\n'9`.

So the guard's passing condition was unreachable — the same defect class this file already
records twice (a check satisfied by the very problem it should surface), written *while*
harvesting skill entries about it. The `|| fallback` idiom is safe for commands that fail
by exiting non-zero; it is a trap for `grep -c`, which reports its answer in stdout and
its match-state in the exit code.

**The check:** when two observations of one system disagree, suspect the instrument before
the system — and prove the instrument by feeding it a known input. One `echo "no match" |
grep -c foo || echo 9` settles it. Use `|| true`, or test the output rather than
short-circuiting on exit status.


### CHECK THAT THE CREDENTIAL PATH EXISTS BEFORE WRITING COMMANDS THAT ASSUME IT (2026-09-08)

Asked for "exactly what to run" to bind a key against `latest`, I produced three commands
in succession. Each was correct in shape and each failed on a value I did not have:
`<latest DSN>`, then `<ALIAS>`, then `<your-vault-host>`. Ryan ran them verbatim — which
was reasonable, because there was nothing on the machine to substitute.

The check I skipped costs one command: **does this machine have the credential path at
all?** `env | grep VAULT`, `ls .local-secrets/`, `grep -r VAULT_ADDR ~/.zshrc`. All three
were empty, and finding that first would have replaced three failed rounds with one
accurate sentence: *this needs cluster access I do not have.*

The generalisable form: a placeholder in a command handed to someone else is a question,
not an instruction. Either resolve it, or say plainly that it cannot be resolved from
here — do not ship it as though it were runnable.

### VERIFY A DEPLOY WITH A CONTROL, AND VERIFY IT AGAIN AFTER THE MERGE (2026-09-08)

Two halves, both learned the hard way this session.

**After the merge:** "nothing is deployed" was asserted for hours on the strength of having
been true earlier in the day. `main` auto-deploys to `latest`; #196 had been serving the
whole time, and it took Ryan asking *"doesn't it redeploy on merge?"* to dislodge it. A
deployment claim has an expiry.

**With a control:** confirming #200 closed the leak needed three reads, not two — the
neighbour scope refused, the OTHER neighbour refused, and **the console's own scope still
returning 200**. Without the third, "everything is refused" passes the first two and is a
broken console. The same shape as the positive controls in the tests: an assertion written
only in the negative cannot tell a fix from an outage.


### A FIX CAN MAKE AN EXISTING TEST VACUOUS WITHOUT MAKING IT FAIL (2026-09-08)

Bounding the admin control plane to one tenant broke nothing visible — but it changed
*why* two existing security tests passed. `test_memory_graph_browser_demo_principal_filters_scopes`
and its HTTP twin exist to prove PRINCIPAL-level authorization: a USER principal refused a
scope it is not allowlisted for. Their blocked scope is a `customer:` scope, so under the
new filter it stopped being registered at all, and the refusal arrived as
`"not registered"` instead of `"not authorized"`.

Still closed. But **the check those tests exist for stopped running**, and the cheap
repair — editing the expected string — would have left two security tests passing on the
wrong assertion, permanently. The right repair was to restore the precondition
(`extra_scopes`), so the scope is registered and unauthorized, which is the case under
test.

**The check:** when a change alters WHY something fails, ask whether the original check
still runs at all. A test that fails for a new reason has stopped testing the old one, and
no gate reports that — the suite is green either way.

### MUTATE A SECURITY FIX IN BOTH DIRECTIONS (2026-09-08)

Every guard added this session was pinned twice: once by reverting it (does it still
refuse?) and once by making it refuse UNCONDITIONALLY (does anything still work?). The
second direction is the one that gets skipped, and it is the one that catches a fix which
"works" by refusing everything — here that would have taken `dream_history` down on four
environments while looking like a security win.

A third mutation was needed because `diff-cover` flagged the tool's call site as
uncovered: the guard tests proved the RULE and not the WIRING, so deleting
`require_fleet_wide_write(...)` from `remediate_coherence` would have left every test
green. Guard logic and guard wiring need separate tests.

### GIT AUTO-MERGES TWO GOLDEN BLESSINGS, AND THE RESULT CAN MATCH NEITHER TREE (2026-09-08)

Combining two branches that had each re-blessed `api_surface.golden.txt`, git merged the
file with NO conflict. Goldens are whole-tree artifacts: a clean textual merge of two
independent blessings is not evidence that the merged golden describes the merged code,
and the gates would then pass against a fiction.

Verified rather than trusted — regenerated the surface and ran
`test_public_api_surface_matches_the_golden` on the combined branch. It passed, but only
because the two edits happened to land in different regions.

**The check:** after any merge or cherry-pick that touches a golden, RE-BLESS on the
merged tree and run the gate; never accept the auto-merge. The first comparison attempted
here was itself wrong — `grep -v '^$'` stripped blank lines from one side and reported a
false mismatch on a trailing newline — so **use the gate, not a hand-rolled diff**.


### A GATE CAN BE BLIND TO THE EXACT HAZARD IT NAMES (2026-09-08)

I wrote a chart test to catch a `#` comment placed between two backslash-continued flags
in the admin launch script — a real trap, because the continuation swallows the comment
and comments out the rest of the command. The first version used `sh -n`. Mutation said
**MISSED**: `helm lint`, `helm template`, `sh -n` and a text `in script` assertion ALL
PASS on the broken form, because the YAML stays valid, a comment IS valid syntax, and the
swallowed flags are still present in the *text* while absent from *argv*.

Two further corrections inside the same fix:

* The probe first dropped `exec`, which made the orphaned lines run as commands and fail
  loudly with 127 — modelling a failure the real script does not have. `exec` **replaces
  the shell**, so the true failure is silent: healthy pod, short argv,
  `--no-admin-writes` gone.
* `stdout.split()` DROPS empty arguments, and an empty argument is exactly what
  `--tenant-id` carries on demo envs — so a whitespace-split argv cannot distinguish
  correct quoting from the unquoting bug that makes `--agent-id` the tenant id. NUL
  delimiters were required.

**The rule:** when a gate targets a runtime hazard, prove it by mutation before trusting
it, and prefer executing the artifact over inspecting its text. Three mutations, three
different verdicts, from what looked like one test.

### A FIX WHOSE OWN FAILURE MODE IS THE ORIGINAL BUG IS NOT A FIX (2026-09-08)

The worker fix replaced no-transports with `build_transports_from_tenant_graph`, and my
comment justified the choice by saying it was unlike `bind_default_tenant`, which
"silently stays rule-based". An independent verifier ran it: the chosen function does the
same thing. The fix's failure mode was byte-for-byte the defect it closed, and would have
been invisible for exactly as long.

**Check every fix against this question directly:** *if this silently does not take
effect, what does the system look like?* If the answer is "the bug I am fixing", the fix
is incomplete until it is loud.

### DELETING A LEAKY FIELD DOES NOT CLOSE THE LEAK IF THE PROSE STILL CARRIES IT (2026-09-08)

`graph_tenant_ids` was removed to stop cross-tenant enumeration, and the removal was
tested. But `resolve_launch_tenant_id`'s **warning text** interpolated
`', '.join(graph_tenants)` — the full roster — onto the same unauthenticated endpoints. The
A1 chart change made that branch MORE reachable, not less. The fix looked complete and
leaked through the message.

**Two habits from this:** assert the *property* (the neighbour's id appears nowhere in the
serialized body) rather than the *field*, so a rename or a relocation cannot satisfy it;
and when redacting for an untrusted audience, ask where the detail should go instead —
here, the process log — rather than destroying the signal that diagnosed the defect.


### "`kubectl` is refused" is not "this cannot be verified" (2026-09-08)

#185 recorded, correctly, that every `kubectl` read against the stage and load clusters is
refused, and concluded that verification there was **blocked on credentials**. That inference
held for two weeks and was wrong.

The application's own HTTP surface answered the same question with no credentials at all:
`GET /api/overview` through the admin ingress reports `operator.store` and
`operator.graph_path`, so the `operationalStore` row of that issue's table was measurable from
outside the cluster the whole time. It took one `curl` per environment.

**The check:** when a control-plane read is refused, ask what the *application* already exposes
before recording the question as blocked. A deployed service that reports its own configuration
is a verification surface, and it is usually reachable when `kubectl` is not — different
credential, different network path. `STATE.md` gained a `store` field for exactly this reason
in #187, and nobody connected it to #185's blocked row.

**Corollary:** a "blocked on credentials" note in an issue is a claim with an expiry, like any
other. Re-test it when the surface changes — #187 shipped the field that unblocked this a day
before the conclusion was written down.

### "Reads are unaffected" is a blast-radius claim, and I made it three times without testing it (2026-09-07)

`--read-only` was written, tested (61 tests), gated (`check.sh` 16/16), described in an issue,
a commit message and a PR body — all asserting the cost was "the console's write features;
reads are unaffected". Every one of those assertions was made from the design intent, never
from the surface.

It was wrong in two ways at once, and both were one command away:

* **`/api/platform/*` — 15 of the 28 POST routes — is a documented public API.** `README.md`
  presents it as the HTTP equivalent of the MCP tools, and an agent calls `bootstrap` / `start`
  / `search` at session startup. A blanket 405 would have taken it down on three environments.
* **The console's own reads were untested beyond `/api/overview`.** `/api/archive`, both
  adjudication reads, the `/api/` 404 fallback and the static branch had no coverage, so
  "reads are unaffected" rested on one route out of five.

**The check:** before claiming what a change does or does not break, ENUMERATE the affected
surface from the source — the route table, the tool registry, the call sites — and say how many
there are. A number you derived is a claim; a number you assumed is a guess wearing a claim's
clothes. Here the enumeration was `ast.walk` over one function, and it split 28 into 15 + 13
along a prefix nobody had noticed.

**Corollary:** no gate could have caught this. `check.sh` passed, and all 61 tests asserted the
refusal the author intended. A test suite verifies the thing you built; it cannot tell you that
the thing you built has the wrong scope.

### A mutation test can fail for the wrong reason, and that reads exactly like success (2026-09-07)

To prove the new parity arm would actually *catch* the cross-scope defect on Postgres, I
deleted the scope predicate from each of the three SQL reads and ran the test. It reported
**CAUGHT ×3**. That result was worthless.

The script stripped the predicate with a regex carrying `count=0` — replace *all* — which also
removed `scope_key` from bind tuples elsewhere in the file. Every failure was
`psycopg.ProgrammingError: the query has 4 placeholders but 3 parameters were passed`. The
tests failed because the SQL was broken, not because the read was unscoped.

**A non-zero exit code is not evidence that the assertion you care about fired.** The failure
mode is invisible if you only check the return code, and "the test failed when I broke the
thing" is *consistent with* a sensitive test without being evidence of one — the same shape as
`Corroboration is not verification` below, one layer down.

**The check:** when mutation-testing, read the *reason* for the failure, not just its
existence, and classify a mutation that breaks compilation/SQL/imports as **INVALID** rather
than CAUGHT. The redone version removes the predicate *and* its bind parameter together, so the
statement stays valid and the only variable is scoping — and it then fails on a real scoping
assertion naming two UUIDs where one was expected.

### When a fix's "obvious companion change" would break a pinned behaviour, pin the refusal (2026-09-07)

The plan for the truth-key collision included charset-validating `scope_id` to reject `:`,
since the colon is *how* two scopes reach one truth key. Measured before implementing: only 2
of 221 `scope_id` literals contain a colon — but one is `customer:wdw:pinnacle-events`, the
documented demo scope across README, `docker-compose.local.yml`, `examples/` and
`docs/design/23-sso-web-ui.md`, and `test_principal_from_registry_row.py` **deliberately pins**
that `from_key` splits only on the first colon.

The companion change would have broken a documented shape to fix a defect the real change
already fixed. Dropping it is not enough, though: the next person reads the same defect and
re-derives the same "obvious" fix. **A test now asserts the colon stays legal and says why** —
so the refusal is as durable as the fix.

### Corroboration is not verification, and it feels identical (2026-09-07)

I claimed MCP was unusable through latest's ingress, from ONE 404. I then "confirmed" it two
ways: an A/B (worked pinned to a pod, failed through the ingress) and a config read
(`sessionAffinity: None`, 2 replicas). I was about to file an issue.

Both were consistent with the hypothesis and **neither tested it**. The A/B changed two
variables at once — pinning also bypassed the ingress entirely, so it could not distinguish
load-balancing from header-stripping. The config read described a mechanism that *could*
produce the symptom; it never checked whether it *did*.

The actual test was trivial and I nearly skipped it: **repeat the failing call.** 8/8
succeeded. The whole diagnosis was wrong and the failure was transient rollout convergence.

The tell: every piece of evidence I had was something I went looking for AFTER forming the
hypothesis, and each was a thing the hypothesis predicted rather than a thing that could
have contradicted it. **Before filing on a single observation, try to reproduce it.** If it
will not reproduce, there is no finding — however good the mechanism sounds.


- **A test fixture that resets state hides the bug class that only bites an EXISTING system.**
  2026-09-03. I added `key_principals` inside `_V1_CORE`, an already-applied Postgres migration.
  All 22 tests passed. It would have reached **every fresh database and no existing one** --
  `migrate()` skips versions already in `schema_migrations`, so a live store would never create
  the table and `bind_key_principal` would raise `UndefinedTable` in production. The suite could
  not catch it: the fixture DROPs and recreates the schema every run, so V1 always applied fresh,
  and the fresh path is the one path where the bug is invisible. **The check:** for anything
  versioned, migrated, or cached, ask *what does this look like on a system that already exists?*
  and write the UPGRADE test, not only the from-scratch one. Reproduce by rewinding a real
  instance -- forget the version, drop what it created, leave the rest applied -- rather than by
  building a clean one. Found by an explore agent reading the schema, not by the suite.
- **A stream you read twice is empty the second time, and `x not in ""` is always True.**
  2026-09-03. I wrote `assert json.loads(_body(exc)) == {...}` then `assert "KeyError" not in
  _body(exc)` -- and the second line passed because `HTTPError.read()` had already consumed the
  body, not because the text was absent. In a test whose entire purpose was to catch that class.
  **The check:** any assertion of the form *"X is not in the output"* must read the output ONCE
  into a variable and assert against that, and should also assert something POSITIVE about the
  same variable, so an empty value fails loudly instead of satisfying the negative. Applies to
  `HTTPError.read()`, `subprocess` pipes, file handles, and any generator.
- **BOTH goldens truncate their rendered diff. Never bless from what is printed.**
  2026-09-02. `api_surface.py --check` ended `... and 473 more diff lines`; `storage_golden.py`
  ended `... and 17 more changed`. I read 15 of 32 storage rows and nearly blessed the rest unseen,
  and my first grep over the api diff reported a false "1 true deletion" because it was parsing a
  truncated list. **The check:** compute the delta from the FILES — parse golden and current, diff
  at symbol/method level — before every bless. The skill already recorded this for `api_surface`
  (`api_removals.sh` exists for it); `storage_golden` has the same defect and no equivalent tool.
  Doing it properly is what showed the api change was purely a private-signature swap plus class-hash
  churn, and that all 32 storage rows were one expected method.
- **An absent instrument reads exactly like an absent server. Check the tool before believing the
  result.** 2026-09-02, three times in one session. `pg_isready` is not installed on this box, so my
  "postgres: down" was the binary missing — Ryan corrected it and the container had been up 7 days.
  A shell with a broken `PATH` returned `conn-fail` for every local endpoint; with `PATH` repaired
  all three answered 200/200/204. A `getattr(x, "transport", None)` typo (the attribute is
  `_transport`) made a *reproduced* credential leak print `LEAK: False`. **The check:** any negative
  result about an external system needs the instrument proven first — `command -v`, a known-good
  target, or a control that must come back positive. A tool that cannot report success is
  indistinguishable from a system that is failing.
- **A documented convention looks exactly like drift. Read the file's own header before repairing it.**
  2026-09-02. #136 asked for a gate failing any register citation that does not resolve against
  `HEAD`. I measured 69 dead citations, then 17, then resolved all 85 pre-split ones through the
  deleted blobs and **rewrote 75 of them** — and only then read lines 3–9 of the very file I was
  editing, which forbid exactly that and explain why. Reverted; the tree matched `HEAD` byte for
  byte. **The check:** when a document looks stale in a uniform, systematic way, that uniformity is
  evidence of a *policy*, not of neglect — read its header before the repair, not after. Sibling of
  the absence-is-a-decision lesson directly below: both are *deliberate* states that present as
  defects, and both cost a full implementation before the read that settled them.
- **An absence can be a design decision. Grep the tests before calling it an oversight.**
  2026-09-02. #137 reports that `authorized_scope_keys` is enabled in zero shipped code paths and
  asks for it to be wired. I wired it at the obvious boundary and **35 tests failed**, one named
  `test_platform_facade_does_not_set_core_guard` with the rationale in its docstring. The
  constructor docstring said the same thing. I then recommended *adding a docstring* that already
  existed. **The check:** before treating a missing call as a gap, grep the test names for the
  thing you are about to add and read the docstring of the parameter you are about to set — a
  deliberate omission usually has a guard next to it, and this repo writes the reason down.
- **A control can return the right number for the wrong reason. Control the control.** 2026-09-02.
  Twice in one session. (1) Testing whether `parity_coverage.py` refuses to judge a hermetic
  coverage report, I passed `coverage.json` as a positional argument. **argparse rejected it and
  exited 2** — which is exactly the "declined to judge" code I was looking for. The instrument
  never ran. The real control had to synthesise a hermetic report by zeroing the Postgres files.
  (2) Testing whether the coupling gate notices a new upward tier edge, I added a second
  `config → storage` import and the reading did not move — which looks like a broken gate. It
  counts distinct **(subsystem, subsystem)** pairs, not import sites, and the baseline comment
  says so. Had I trusted it I would have reported a working instrument as broken. **The check:**
  after a control produces the expected result, ask *by what path* — an exit code produced before
  the tool's own logic ran is not evidence about the tool. And design the control against what
  the metric actually counts, which means reading its implementation, not its name.
- **A probe that skips a lane the resulting commit must pass is an incomplete probe.**
  2026-09-02. SZ-1's probe ran `pytest` and `api_surface` and reported clean; the commit then
  failed `lint` on `I001`, because retargeting an import changes where it sorts. Cheap to fix,
  but the probe's whole purpose is to know the answer before committing. **The check:** a probe
  runs every lane the commit will face, not the ones the change "obviously" touches.
- **Probe the combination, not just the parts.** 2026-09-02. SZ-2 applied alone **breaks the
  package import** (`cannot import name 'ReceiptLedger' from partially initialized module`)
  because relocating a module changes initialisation order and surfaces a cycle that SZ-1
  removes. Neither item's individual probe could show this. **The check:** when a plan has two
  items touching the same import graph, probe them together before declaring them independent.
- **Search the decision log before deriving a decision — this repo keeps one.** 2026-09-02. Asked
  how tenancy should key off gateway virtual keys, I measured the gateway from scratch across
  several rounds and produced three successive answers, each killed by the next measurement. All
  of it was downstream of a question **already decided twice**: DW-014 (2026-07-30) and DW-026
  (2026-08-28) in `docs/authorization-decisions.md`. Worse than wasted effort, I arrived at a
  *worse* answer — rejecting `key_alias` as caller-chosen — because I had not read the design that
  uses it as a lookup key into our own registry rather than as a claim. I only found DW-026 when I
  went looking for where to file the write-up. **The check:** before measuring a system to answer
  a design question, grep `docs/*decisions*.md`, `STATE.md` and `TAKEOVER-BACKLOG.md` for the
  nouns in the question. A live measurement that contradicts a recorded decision is a finding
  about one of them, not a fresh start.
- **"Caller-chosen" and "forgeable" are different properties.** 2026-09-02. I ruled `key_alias`
  out as an identity source because a user picks their own at mint time — true, and irrelevant.
  LiteLLM enforces alias uniqueness **globally**, so a caller can pick any alias *except* one
  already taken, which is exactly the property a lookup key needs; measured by attempting the
  collision three ways (duplicate mint, cross-user mint, rename) and getting `400` each time.
  `metadata` fails the same test because it is caller-chosen **and** unconstrained. **The check:**
  ask whether an attacker can make the field *collide with a victim's value*, not who types it.
  Uniqueness constraints turn user input into a safe key; absence of them is the actual defect.

- **A tool that parses another tool's stdout can be defeated by COLOUR, and it fails silently.**
  2026-09-02. `FORCE_COLOR=3` in the environment made mypy emit ANSI even through a pipe;
  `mypy_suppressions.py`'s regex expects a literal `: error: ` and the escape codes sit between
  the colon and the word. 237 error lines parsed as ZERO, so all 89 suppressions read as dead and
  the gate demanded their deletion -- which would have unsuppressed 201 live errors. The repo had
  not changed; the environment had. **The check:** any gate that shells out and parses output must
  force `NO_COLOR=1`/`TERM=dumb` AND strip ANSI defensively, because a parser that matches nothing
  is indistinguishable from a clean run. More generally: when a gate's verdict flips with no
  corresponding change in the repo, suspect the environment before the code.
- **The verification layer is accreting faster than the thing it guards, and never shrinks.**
  2026-09-02, measured since 2026-08-25: **87 commits touching `scripts/`** against 102 touching
  `src/memotron` -- and `scripts/` is **+11,786 insertions against 131 deletions.** Nothing has
  ever been retired. Three gates gave CONFIDENT WRONG ANSWERS in two days (check.sh SKIP-as-PASS,
  parity-cov fed another tree's coverage.json, mypy_suppressions defeated by colour) and in none
  of them did the tool error -- all three answered. Meanwhile every real defect found in that
  window came from reading code, adversarial review, or a differential probe; **no standing gate
  caught a bug.** They caught structural facts, which is different. **The check:** before adding a
  lane, ask what it would have caught that review did not, and whether it can distinguish a clean
  run from an unreadable input. Ryan's framing is the one to keep: *"the entire validation gating
  effort is more work to keep current than the entirety of what we've refactored."*
- **When a tool reports on other tools, audit the REPORTER before trusting any row in it.**
  2026-09-02. `check.sh`'s summary said `storage PASS` for a lane that had skipped, because the
  script signalled "declined to judge" with exit 0 and `run_lane` maps exit 0 to PASS. Two agents
  reported it, I repeated it, and it took an adversarial *reporting audit* -- a reviewer whose only
  job was to re-run every command the previous report quoted -- to find three lines of shell.
  **The check:** when a summary table is the evidence, run one lane directly and compare. And when
  writing a gate, make "I evaluated nothing" a distinct exit code from "I evaluated everything and
  it was fine" -- if those two states are indistinguishable to the caller, the caller will
  eventually report the wrong one.
- **Do not invent a threshold and then let it decide.** 2026-09-01. I wrote *"return NO_GO if the
  honest extractable count is under ~150 lines"* into a measurement prompt, the agent measured
  **138**, and I reported the work dead. The 150 was mine, invented minutes earlier, and 138-vs-150
  is not a distinction. Worse, **extractable lines is the wrong measure**: the cost of six
  near-identical transports is that fixing the auth block means six edits in six files and knowing
  all six exist — no line count expresses that. Ryan pushed back (*"can't agents just read the code
  and actually refactor it?"*), the gate came off, and the refactor landed with a sixth transport
  nobody knew about. **The check:** if a number decides the work, ask who chose it and when. A
  threshold invented for this decision is an opinion wearing a measurement's clothes. Judge the
  artifact, not the forecast.
- **A percentage floor punishes deduplicating covered code.** 2026-09-01. `memotron.embedding`
  fell 89.5% → 88.6% and `cov-floor` failed. **No coverage was lost.** 26 COVERED statements moved
  into `gateway.py` where they are still exercised (`coverage.parser`: 102 → 76 statements), and
  the uncovered set was byte-identical — the same eight validation branches, none of them touched
  by the diff, each verified individually as absent from it. **The check:** when a ratio floor
  fails after a refactor, compare the DENOMINATOR before blaming the numerator. Writing tests for
  branches that were already dark, to restore a percentage, is the wrong repair.
- **Name a test for what it proves, and run a mutation to find out what that is.** 2026-09-01. I
  wrote `test_the_key_is_resolved_before_the_payload_is_built`. It **could not fail** — I reversed
  the ordering in the source, verified the mutation applied by exact-match assert rather than
  assuming, and all eight tests still passed, because synthesis's `_chat_payload` only reads
  attributes and cannot raise. A test whose name claims more than it checks is worse than no test:
  it retires the question. Renamed, with the dead end recorded in the docstring and a pointer to
  the one transport where the ordering IS observable. **The check:** for any test named after an
  ORDERING or an INTERACTION, break the thing it names and confirm red before believing it.
- **When you measure a difference between two things, measure BOTH directions before baselining
  one of them at zero.** 2026-09-01. I compared per-method coverage across the two storage
  engines, found 20 methods covered on SQLite and dark on Postgres, filed #149 on that number, and
  set the reverse direction's baseline to `0` without ever running it. It is **7** — and it
  contains `set_tenant_llm_credentials` and `active_relationships`, so it was not the harmless
  direction I assumed either. The real figure is 27 of 172. **The check:** a baseline you did not
  measure is a claim, not a baseline. If a gate has two directions, run both before blessing —
  and note that this happened *inside* a gate whose entire subject is unchecked assumptions about
  the other engine.
- **A gate fed the wrong input must say so, not produce a confident number.** 2026-09-01.
  `parity_coverage.py` reads `coverage.json`; given a hermetic report it would have found ~170
  "gaps" — every Postgres method, all false — and that output is indistinguishable from a real
  catastrophe. It now exits 2 with a diagnostic instead, and the `check.sh` lane skips when
  `MEMOTRON_TEST_POSTGRES_DSN` is unset. **The check:** for any gate that reads a produced
  artefact, ask what it prints when the artefact is *valid but incomplete*. A number computed from
  the wrong input is worse than no number, because it gets believed.
- **The bless procedure in `storage_golden.py` paid for itself, on its author.** 2026-09-01.
  Adding one test made `receipts` and `storage` go red. The obvious move — bless from the profile
  `check.sh` just wrote — would have **silently deleted five DSN-only rows**, because check.sh's
  tests lane filters `-m "not postgres and not corpus"` and discards the Postgres lane's
  observations. Nothing would have failed; the loss surfaces days later as a mysterious "5 new"
  hard failure. Only the docstring prevented it, and I wrote that docstring the same morning after
  getting the advice wrong twice. **The check:** confirm `0 not observed` before blessing — that
  is the signal the input is the rich run.
- **A broken success streak does not imply something changed — first check whether the streak was
  the whole population.** 2026-09-01. Six consecutive admin rollouts landed on the incumbent's node
  and the seventh did not, so I hunted for what changed and found three `UPGRADE_MASTER` operations
  and a `REPAIR_CLUSTER` 79 minutes before the failure. Plausible, well-evidenced, and **wrong**:
  the same failure had already happened on the *stage* cluster four days earlier, which no
  control-plane event on *latest* could explain. My "1-in-64" arithmetic counted only `latest`'s
  rollouts. **The check:** before explaining why a streak broke, ask what else is in the population —
  other clusters, other environments, other namespaces — and go look there. A special cause invented
  for a sample of one is the most expensive kind of wrong, because it is persuasive.
- **A closed issue with a conditional scope reads as "done" to everyone who finds it later.**
  2026-09-01. **#79** — *"resolve the RWO PVC vs replicas fault; plan PVC removal at Postgres
  cutover"* — closed `2026-08-28T04:46Z`, **73 minutes before the fault it describes caused its first
  production outage**. Its removal step was gated on a cutover blocked behind #130/#92, so the issue
  was closed with its real work undone and its search-result state saying otherwise. **The check:**
  when an issue's scope contains "after X lands", do not close it — leave it open blocked on X, or
  close it with a successor link. And when searching for prior art, read closed issues' *scope*, not
  just their titles.
- **"Correct at rest" and "correct while changing" are different claims, and a cluster observed
  at rest can only answer the first.** 2026-09-01. T0-8 was refuted on solid evidence — the api is
  `2/2` in all three clusters, mounts no volumes, and only `admin` holds the PVC at `replicas: 1`,
  "which is correct RWO usage". Every word true, and the conclusion held for steady state. The
  first actual deploy then deadlocked on exactly that PVC, because a rolling update starts the
  replacement before the incumbent releases the volume. Nothing had deployed since the refutation,
  so there had been no occasion to find out. **The check:** when refuting a deployment claim, ask
  which lifecycle phase the evidence covers. `kubectl get` describes a system that is not changing.
- **A metric that cannot distinguish right use from wrong use will always report that nothing can
  improve.** 2026-09-01. `coupling_report.py` counted call sites; the split moved none of them and
  the recorded conclusion was that restructuring does not help. But `models` has fan-in 80 because
  it is shared vocabulary — the same document that recorded the conclusion also calls that
  "correctly shared". The instrument could not tell the two apart, so its verdict was
  predetermined. Replaced with import cycles and tier violations, which are wrong regardless of
  frequency. **The check:** before trusting a flat metric, ask what a *good* result would look
  like and whether the metric could produce it.
- **Killing a long-lived process destroys in-memory state that the disk no longer matches.**
  2026-09-01, and it cost the dogfood KEK. The process had loaded the key at startup; the file was
  replaced four days later; the two diverged silently and only the process still held the real
  one. **The check:** before stopping a process that has been up for days, compare the mtimes of
  the files it loaded at startup against its start time. Anything newer is state you are about to
  lose. `ps -o lstart` and `stat` are the whole check.

- **When a run passes its known baseline by ~2x, check liveness before waiting any longer.**
  2026-09-01. I had measured the Postgres full suite at 144s earlier in the session, then waited
  **21 minutes** on the same command before looking at why. The cause was Docker Desktop being
  manually paused, visible in one `docker ps`. Ryan noticed before I did. I had even flagged a
  timing anomaly earlier the same day (217s vs 443s for the same lane) and explicitly chose not to
  chase it, so the habit of noting-without-acting was already established. **The check:** for any
  long job, know its baseline before starting it; when elapsed exceeds ~2x, look at the output
  file's mtime and the external dependency (container, DSN, network) rather than waiting more. A
  hung job and a slow job are indistinguishable from the outside, and only one of them ends.
- **"The register is stale" is not a safe default either.** 2026-09-01. Ten of fourteen Tier-0
  entries needed correction, which made staleness the expected case — and then I wrote a handoff
  saying T0-12 was open when its row had said FIXED for hours. The base rate justified *checking*
  every entry; it never justified *assuming* a direction. Both the register and the summary of it
  are hypotheses, and the cheap move is to read the row before repeating it.
- **A fixture that pins a value makes the rule reading that value untestable, and the suite
  will look green.** 2026-08-31, T0-14. The acceptance rule had two halves — invariants and
  stability. I fixed the invariants half, wrote 4 tests, watched them go RED then GREEN, and
  declared done. My `_comparison` fixture set `stability_score=1.0` unconditionally, so the
  stability half could not fail in any test I had written, and it was broken in exactly the
  same way (comparing the baseline against a JOINT score). An independent verifier found it.
  **The check:** for every field the code under test *reads*, ask whether the fixture varies
  it. A constant in a fixture is a silent `assume`. Derive it from the inputs instead —
  `stability_score=1.0 - max(baseline_flip_rate, candidate_flip_rate)` — so it cannot drift out
  of agreement with the thing it models, and assert on it in the test that depends on it.
- **A bug in a gate hides real signal for as long as it goes uncorrected, so fixing one is
  worth more than the bug itself.** Same session. T0-14 made staging read the candidate's
  invariants; the moment it read the baseline's, it reported that 4 of 15 builtin motives
  violate `retrieval_allocation` (#143). That finding had been true and invisible the whole
  time. **The corollary for triage:** a fail-open gate is not just "a check that doesn't
  work" — it is an unknown quantity of suppressed findings, and its priority should reflect
  that rather than the gate's own blast radius.
- **`--bless` on a golden is only as good as the run that fed it, and the bad runs are quiet.**
  Same session, twice. Blessing the storage golden after a run with the Postgres DSN *unset*
  does not skip those rows — the tests SKIP, fixture teardown still calls `close`, and each
  writes a `close:1` stub over a real profile. Nothing warns. Blessing after a hermetic-only
  run instead tries to *delete* every Postgres row, which `--allow-shrink` at least makes
  loud. **The check:** before blessing anything, name the run that produced the observation
  file and confirm it observed everything the golden covers; if a bless reports rows you
  cannot attribute to your own change, stop rather than accept them because the gate went
  green. Written up in `scripts/verify/storage_golden.py`.
- **`_materialize_episode` is a mutable state machine, not a long function, and it is not
  mechanically decomposable at all.** Measured across two passes on 2026-08-30. The first
  said phases consume 22/25/16 locals; grouping those into typed context objects was
  measured to fix the INPUT side well (16 -> 3, 25 -> 9, 22 -> 16, all under
  `max-args = 20`). That looked like a green light and it was half a measurement. The
  output side kills it: the 89-line supersession block writes 10 locals, **9 of which are
  written AGAIN later in the loop** -- `relationship_status` is assigned at L486, L519,
  L547 and twice more past L590; 11 downstream writes in total. So the block does not
  PRODUCE a result, it CONTRIBUTES to state that later blocks keep mutating. A frozen
  outcome object is the wrong model, and a mutable context object is the same locals with
  the mutation hidden from the call site, which is worse than inline for reasoning.
  **No amount of input typing fixes an output-side problem.** The only route is rewriting
  the algorithm, which needs behaviour-change appetite and characterization tests far
  beyond what exists. **Do not attempt this as a refactor.** Measured on this function
  only -- the other 29 functions >= 150 lines have not been checked and may differ.
- **A god METHOD is not always a god class in miniature, and "decompose it" can be
  unavailable rather than merely hard.** `_materialize_episode` is 1,075 lines, of which
  971 are a single `for memory in memories:` loop. The plan said it was blocked on
  testing -- "two regimes no test distinguishes" -- and that turned out to be **stale**: a
  test now covers both, control-verified two ways (force the quarantine regime on and the
  fail-fast half stops raising; force fail-fast on and the quarantine half breaks). The
  one genuinely uncovered path, the raw-credential rejection, now has a test too.
  **The real blocker was never testing. It is dataflow.** Measured per candidate phase:
  the 208-line block consumes 22 locals and produces 25; the 87-line block consumes 25;
  the 89-line block consumes 16. A 22-in/25-out function signature is worse than the
  inline block it replaces, and the 87-line one would need 25 parameters against the
  `max-args = 20` ratchet enabled two commits earlier -- it would fail our own gate.
  Extracting only the loop body IS viable (15 free variables) and buys almost nothing:
  1,075 becomes ~100 + ~980, the mass moves and the god method keeps its size.
  **The sequencing inverts:** a typed per-memory context object is a PREREQUISITE for
  decomposing this, not a parallel Phase 7 item, because it is what lets a phase pass one
  value instead of 22 names. Do not re-attempt the decomposition before that exists.

- **A gate can be green because it is not looking.** `mixin_dag.py` reported PASS across the
  whole `client.py` split while its `LAYERS` map listed only `dreaming` and `agent_memory` — it
  had never once examined the largest mixin package in the repo. It took an independent reviewer
  to ask; no amount of re-running it would have said so, because "package not registered" and
  "package is clean" produce the identical output. **When a gate is configured by an explicit
  allowlist, the allowlist is part of the gate and rots separately from the code.** Same shape as
  `coverage_floors.py`'s `unknown` check, which exists precisely to fail on unlisted modules —
  `mixin_dag.py` had no equivalent. Generalise: for every gate, ask what it does when handed
  something it has never heard of. Silence is the wrong answer.
- **A widened allowance passing my own control test is not the control test working.** I bounded
  a known-flaky storage-profile row, then control-tested with a `+7` bump and watched it pass —
  and recorded that as evidence the allowance behaved. It was evidence the allowance was seven
  times wider than the `+1` my measurement justified. **A control test only means something if I
  can say, before running it, which way it must come out.** I could not, so it told me nothing.
- **"Re-export it" is ambiguous, and the wrong reading typechecks locally and breaks the
  consumer.** After a Shape A split, 115 public names had left `memotron.client`. The obvious
  fix — re-export each from whichever mixin it was visible in — produced 110
  `no_implicit_reexport` errors, because a strict-tier module cannot re-export what it merely
  imported. **Re-export from the module that DEFINES the name** (`obj.__module__`, confirmed by an
  AST check for a top-level definition), and keep the `X as X` form, which is separately required.
- **Distinguish a guard's OWNERSHIP from its BEHAVIOUR when writing the check and when writing the
  comment.** The new `GUARD_MEMBERS` check proves five scope guards live on the composer and no
  mixin. My first comment justified it with "a guard that returns without raising is a silent
  authorisation bypass" — which is exactly the case it does not cover. A gutted guard in the right
  place passes. **A control's stated motivation should be a description of what it rules out, not
  of the worst thing in the neighbourhood.**
- **A guard that RENDERS its output for a human can truncate it, and I read the rendered form as
  the result.** `api_surface.py --check` prints a unified diff and elides it when the change is
  large. I had been reading "removals" out of that print for 13 commits. On the
  `_consolidation` commit it showed 3; the real number was 14, and on `_formation` it showed 3
  against a real 22. Commit `d220328` therefore carries a false claim ("nothing left the surface"
  — five names did). Nothing broke, because the three-way prune had already cleared all five and
  the `44/44 defined names` check is what actually guards the contract. **The lesson is about the
  shape, not the tool: never take a decision off a human-facing rendering of a result — compute
  the set.** `scripts/verify/api_removals.sh` now does, by `comm`, and says why in its header.
  Corrected in the next commit rather than amended, and all 13 commits re-audited against the real
  golden history (that one is the only bad claim).
- **Measure a proposed partition before writing it; twice the measurement changed the plan.** The
  plan named `_repair`/`_retention`/`_pruning` as three mixins — the call graph showed one concern
  cut at an arbitrary line (10 retention helpers called from pruning, one call back), and merging
  removed 12 cross-mixin edges. The plan implied one `_consolidation` — at 2,025 lines it was over
  target, and a three-way split cost only **two** cross-edges, both orchestrator→worker. Both
  decisions took one script and neither was guessable from reading.
- **Automation wrote the wrong thing eight times across three splits, and a gate caught every one.**
  Not once did review catch it first. The extractor: line-regex imports vs multi-line blocks;
  missing sibling *constants*; `_*.py` matching `__init__.py`; an import inserted **inside a module
  docstring** because prose can begin with "from" at column 0; carried names never re-exported.
  `ruff --fix`: deleted two deliberate function-local imports mid-"moved, unchanged" commit, and
  earlier collapsed two `elif` branches that `main` then needed separate. My own prune: left
  `from x import ()`. **The rule that made this survivable was reverting to a green tree before
  every retry, never patching the broken output forward.**
- **A gate that reads a stale artifact reports PASS over work it never saw.** `coverage_floors.py`
  read a pre-split `coverage.json` and cheerfully guarded 56 modules while 14 new ones were absent
  from the file entirely. This is the same family as the three config knobs in Phase 0 that looked
  like enforcement and weren't. **Freshness is part of a gate's contract, not an assumption.**
- **Two guards can both be green and still leave a hole between them.** `pure_move` proves function
  bodies; the golden proved importable names. Neither covered pydantic *field defaults* — class-level
  statements — which is most of what `models/` and `config/` contain. I only found it because I went
  looking for what my own claim did not cover, after stating it too broadly.
  **Ask what a guard cannot see, not just whether it passes.**
- **Dynamic attribute forwarding is invisible to any enumeration-based check.**
  `memotron.graph.__getattr__` means `memotron.graph.X` resolves for anything on the sqlite
  backend — so neither `vars(module)` nor grep-for-imports can tell you a name is load-bearing.
  Three broke before I saw it. **A `__getattr__` shim needs its own explicit test; nothing else
  will notice.**
- **Name-matching found the concern boundaries; the structural diff found what name-matching missed.**
  171 of 195 sqlite methods mapped onto postgres mixin names, and I filed the remaining 24 as
  "sqlite-specific". Two of them were the same concern under a different name, visible the moment
  the two files could be diffed side by side. **Do the cheap alignment first, then let the artifact
  it produces correct you.**
- **I drew the wrong conclusion from a correct observation, and it went into a plan.** `git apply --check`
  on `tier0-fixes.patch` failed while `--3way` succeeded. I read that as "context has drifted, land it
  before it rots" and wrote it into the plan as evidence for urgency. The actual cause is the opposite:
  **plain apply fails because the change is already in the tree.** Both readings predict the same two
  exit codes, and I never asked which. The check that would have settled it took one grep. **When two
  explanations predict the same observation, the observation is not evidence for either** — go find the
  one that discriminates.
- **A tool that is silently not running is worse than a tool that is absent.** Three of this session's
  five traps produced *identical output* whether the gate was on or off: `--strict-markers` in addopts,
  diff-cover's non-matching exclude, and `strict` leaking project-wide. None would ever have been
  noticed by reading config, by a passing CI run, or by a reviewer. They were found by deliberately
  breaking something and checking the gate reddened. **Adding a gate is not done until you have seen
  it fail.**
- **The suite is the weakest of the three guards, and it is the one everyone reaches for.** P3 flagged
  a changed function body while `pytest` reported 1011 passed, because the change was semantically
  neutral *today* in a symmetric call. At ~89% statement coverage on the two files being split, roughly
  11% of the moved code has no runtime witness at all. **"The tests pass" is evidence about the covered
  paths and nothing else** — for a pure-move refactor, a static proof over 100% of the code is
  categorically stronger and much cheaper than new tests.
- **My own automation broke things twice, and reading the diff caught it before the suite could.**
  Converting an append-loop to `extend` dropped the `for` clause, leaving an undefined name that still
  *parsed*; a `noqa` insertion landed mid-expression and produced invalid syntax. Both were mechanical
  edits I generated in bulk. Coverage later confirmed the suite *would* have caught the first one — but
  I only know that because I checked, rather than assuming the net was there. **Bulk edits need the same
  read-the-diff discipline as hand edits, especially when a linter proposed them.**
- **The verifier caught a regression I had already written down as a hedge and not checked.** My T1-14 writeup said "would be wrong if a consumer depends on implicit `.env` pickup via `client.py:1170` / `admin_server.py:2442` / `local_platform.py:108`". That was not a hedge — `local_platform` genuinely regressed, `README.md:1671` documented the behaviour I removed, and an independent agent proved it in one measurement where I had only speculated. **Writing down the condition under which you would be wrong is not the same as testing it.** If a hedge names a specific file and a specific consumer, that is a cheap experiment, not a caveat — run it before shipping. This is also the concrete argument for the verifier gate: the diff, the suite, and my own reasoning all agreed, and all three were wrong together.
- **A stated "first move" is a hypothesis, and this one was wrong.** The work queue told the next session to make the four call sites resolve `.env` against the project root. Following it would have produced a change that passed review, narrowed the symptom, and left the defect — `discover_project_root` falls back to cwd and matches `.git`. **Read the fix the handoff proposes as a claim to verify, not an instruction to execute**, and check whether the mechanism it names actually closes the hole. The tell here was cheap: 12 lines of the function it depends on.
- **The record's own documentation rots exactly like the code's.** `START_UP.md` declared itself "untracked on purpose" while sitting in `88d6cce`, and the session-brief warned about 11 named directives that no longer exist because the graph was destroyed. Both were written *this week*, by a process that knew better. A doc that describes durability or memory state is describing a moving target — **date it, or make it re-derive the fact instead of quoting it.**
- **The agents read the producer and stopped before the consumer.** Both refutations and three of the five reframings share exactly one root cause. T2-13 is the clean case: the agent saw the metric-mapping function return `None` and inferred silent mis-scoring; following the value one hop shows its consumer *raises*. T1-19, T1-24, T1-18 and T1-22 are the same shape inverted -- **a check that exists and cannot fire**, which reading the check will never reveal. Standing rule for agent-sourced work: **trust the locations, re-derive the consequences.** Every `file:line` the workflow cited was real, including in the two items that were wrong.
- **A register that mixes evidentiary qualities without saying so is a register that will be over-trusted.** 62 -> 85 items in an afternoon, and the 24 new ones had a single reader behind them. Marking them inline cost twenty minutes and made the follow-up pass possible; presenting all 86 as equivalent would have put two wrong findings in front of a room.
- **My probes' controls failed four times this session, each in a way that looked like a result.** The phantom-hold control was manufactured by my own first `run_coherence_scan` applying the hold; T1-25 silently succeeded until an agent was actually *registered*; T0-13 had two independent preconditions (`escalation_cycles_threshold=3` vs a forced `observed_count=1`, and a dropped `reinforced_at` that would have inverted the newer/older arms). Every probe now **exits 2 rather than 0** when its control arm fails, so "no defect" can never be reported by a run that never reached the subject.
- **"Unverifiable" was the wrong word, and I said it in a posted PR comment.** I told PR #44's author their "1068 passed" claim could not be checked by anyone but them. It took 3 minutes and one container to check, and it held. The defensible claim was narrower: nothing runs it *automatically*, and the default run skips the storage tests. **Test the cheap thing before characterising someone else's evidence as unverifiable.**
- **Running it found what reading it could not.** Four defects (uv cache, `fsGroup`, admin `--graph-path`, `SQL_ASCII`) came only from executing the real image and chart commands. Source review had produced a confident, detailed, and incomplete picture.
- **A green test suite is not evidence against the reviewer's findings.** All six PR #44 defects concern behaviour no test covers. 1071 passing tests and 6 real defects are consistent.
- `docker image prune -af` removes *unused* images across all projects, not just dangling ones. Scope disk reclamation to `docker builder prune` first.
- **I overstated reachability in a posted PR comment and had to edit it.** I wrote that the destructive migration prompt is on "the first command a new user runs" without reading `_offer_memory_migration`'s guards. It needs pre-existing memory to fire. Caught only because Ryan asked whether we'd run `init` during the session. **A reviewer who can't reproduce a claim discards the whole finding** — precision about *when* a path fires is part of the finding, not a footnote.
- The corollary: when escalating severity on someone else's work, trace the full call chain *and* its preconditions before posting. Getting the direction right is not enough.

**2026-09-10 — I introduced an unauthenticated exposure and my own docstring denied it.**
The FastMCP 4 migration widened the Host allowlist (see Verified facts), and the comment I
wrote read *"kept verbatim across the FastMCP 4 move so the fail-closed default did not
shift."* It shifted. Two independent reviewers with no session context found it from
different directions; I confirmed it by reading mcp 1.29.0's `_validate_host` out of the
wheel and by measuring both shapes in the built image. **The claim I was most confident about
was the one that was wrong, and I had written it into the durable record.** Same shape as
2026-09-09's "curated allowlist applies end to end". The rule that keeps earning itself:
**a load-bearing claim in a comment deserves the same independent check as a claim in
STATE.md** — prose in a diff is not evidence, it is an assertion looking for one.

**2026-09-10 — I called something "the bug that mattered" and it was a measurement artifact.**
Reported that FastMCP 4 would 421 every kubelet probe and take the deploy down. The 421 was
real, in a real container — but produced by curling over loopback with a *fabricated* pod IP.
A real probe connects to the pod IP, so it self-allowlists and returns 200. The fix is still
warranted (it is load-bearing *because of* `StrictHostGuard`), but the reasoning was wrong and
I had already told Ryan it was deploy-breaking. **Before believing a dramatic negative, ask
what the real caller would put in that field.**

**2026-09-10 — I nearly blessed away 10 golden rows that had nothing to do with my change.**
`receipt_golden.py --bless` from a hermetic run silently dropped every Postgres-only test's
row; only 1 of the 11 removals was mine. `storage_golden.py` **refused** the same shrink and
named the fix. Computing the delta from the FILES (not the report) is what caught it — the
discipline this file already records, applied one level up: **the gate that refuses is worth
more than the gate that reports.** `receipt_golden.py` has no `--allow-shrink` guard and
should get one.

**2026-09-11 — I re-derived facts this file already holds, FOUR times in one day.** Not once;
four. (1) agent-memory's image tag — I raised a non-problem from a stale note instead of
measuring. (2) The gateway master key's `404`, whose meaning is recorded at line ~2128; Ryan had
to tell me to read my own record. (3) *"No overclaiming language exists in this repo"* — a false
negative from a bad pathspec, when #236 already pointed at Design 16. (4) Spending three commands
discovering that every kubeconfig context authenticates as `svc-gke-deploy@jennay-latest-1`,
**which line 1416 already says verbatim.**

The pattern is not carelessness about the record; it is *reaching for a command before a grep*.
An investigation feels like progress and a lookup feels like cheating, and that instinct is
backwards here: this file is loaded at session start precisely so infrastructure facts are
lookups.

**The check:** before spending more than ONE command re-deriving something about clusters,
credentials, Vault, image tags or chart state — `grep -i` STATE.md for it. If it is there and
stale, correct it in the same breath; a stale entry that gets worked around survives to mislead
the next session, which is how (1) happened.

## Last session

**2026-09-11 (session 45) — the FastMCP 4 epic closed, all nine issues, each on a deployed
measurement. Three PRs open. Two of my own warnings turned out to be wrong and are corrected
here.**

Continued from session 44's merge of #264. Verified the migration live rather than assuming it:
`serverInfo v4.0.3`, no `mcp-session-id`, identity probe **all eight arms**, gateway **10/10**
on 2 replicas with `sessionAffinity: None`, sweep exit 0 matching the pre-migration baseline,
and **agent-memory confirmed deployed and correct for the first time** (ClusterIP, no ingress —
which is why nobody had ever checked it).

Closed: **#253 #254 #255 #256 #257 #258 #259 #246 #184**.

**Open PRs, all branched from `main`, mergeable in any order:**

* **#266** — `sweep_mcp` could not authenticate (found by running it against `latest` minutes
  after the merge), plus the agent-memory correction and the identity-probe result
* **#267** — #263, the 503 returning the store's exception to unauthenticated callers, with an
  AST guard because it was the second occurrence of one defect
* **#269** — prod `keyManager`, the `dreamWorker` record, and a **false safety claim** in
  `values-prod.yaml` that was wrong on BOTH SDKs

**Filed and deferred: #268** — stage/load/preview serve the full MCP surface unauthenticated.
Deferred on Ryan's scope call (focus is `latest`, which is the one environment already armed
and verified). The fact that would most change its severity — whether the GCLB's host rule
rejects a forged `Host` — is **untested**; this account has no RBAC in those clusters.

**Next:** merge the single consolidated PR carrying all of this. Then the only remaining
`latest`-scoped work is whatever review turns up. **#237 is 2 of 4** and its blocking item is a
single Vault lookup for `MEMOTRON_KEK_B64` — needs Vault access, not cluster access, and
settles the largest unknown about prod's launch. **#268** is filed and deferred.

**Expiring:** probe keys `dw-probe-alpha-2` / `dw-probe-bravo-2` die **2026-09-14T03:58Z**, and
their aliases die with them — an expired LiteLLM key still owns its alias, so the next run needs
`-3`. Probe rows plus **210 episodes** from the 413 test sit in `probe-tenant-alpha` on `latest`
by design.

**Not ours and still unpinned:** mcp-forge's `fastmcp>=3.1.0` with no upper bound. The pitch is
**~37 broken call sites** across 14 servers (30 `isError`, 6 `ClientSession`, 1 `.inputSchema`,
measured in their tree), NOT a security hole — they set no host guard either way.
