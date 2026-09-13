> **Copy of a personal Claude Code skill**, included so the methodology travels with the
> discovery branch rather than living only on one machine. Installed form lives at
> `~/.claude/skills/verifying-memotron/SKILL.md`; this copy is the reference. It encodes
> what the sweeps and probes in `scripts/verify/` are for, the baselines to compare against,
> and the mistakes that cost the most time.

---
name: verifying-memotron
description: Use when testing, sweeping, debugging, or verifying any surface of jedai/memotron — the SDK, either MCP server, the admin HTTP API, or the admin UI — or when confirming a Memotron defect is fixed. Also use before claiming any Memotron behaviour is broken or working.
---

# Verifying Memotron

## Overview

**Verify the memory lifecycle first. It is the product.** Users depend on two things:
context injected at session start and across compaction, and memory that stays relevant —
stale facts superseded, not re-injected forever. The admin UI is an inspection surface;
almost nobody's workflow runs through it.

```bash
scripts/verify/probe_core_loop.py     # 15 assertions, exit 0/1, ~20s
```

Measured 2026-08-26: **the lifecycle works.** Supersession, current-truth retrieval,
profile rendering, cross-compaction injection and token budgeting all pass. The one broken
link is **formation** — new memories do not form (T1-1), so the proven machinery has
nothing to operate on. Fix that and the promise is delivered.

Corollary for triage: a defect on a surface nobody's workflow touches outranks nothing.
Weigh by distance from the lifecycle, not by how alarming the sweep line looks.

Findings live in the repo, not here. **Start with
`docs/findings/README.md`** — the running working-memory file: mental model, index of where
each kind of knowledge lives, the reading tracker, open threads.

- `TAKEOVER-BACKLOG.md` — the defect register, tiered, each item with evidence
- `DOGFOOD-LOG.md` — the raw trail, including dead ends and retractions
- `docs/findings/formation-suppression.md` — the deepest single investigation

**Check the backlog before investigating.** Most surprising behaviour is already logged.

## The three rules that cost the most to learn

### 1. A green response is not a working system

Memotron's MCP tools return `isError=False` and non-empty results while forming zero
memories — `memory_search` returns *pre-existing* facts, which is what makes it convincing.

**Assert a round trip, never a non-empty response:** write a novel fact → refresh → search
for *that* fact → require it. Any probe weaker than this certifies a dead server.

### 2. Hold one variable

Comparing two servers while also changing the graph file proves nothing about the server —
even when the conclusion turns out right, which is the dangerous case.

Memotron has several near-identical-looking runtimes. Changing entry point *and* data at
once is the default mistake:

| runtime | entry point | sets `handler.platform`? |
|---|---|---|
| **A fixture that resets state hides every bug that only bites an EXISTING system.** | I added a table to `_V1_CORE`, an already-applied Postgres migration. All 22 tests passed. It would have reached every FRESH database and no existing one -- `migrate()` skips versions already in `schema_migrations`, so a live store never creates it and the call raises `UndefinedTable` in production. The fixture DROPs and recreates the schema every run, so V1 always applied fresh, and the fresh path is the one path where the bug is invisible. | For anything versioned, migrated or cached, ask *what does this look like on a system that already exists?* and write the UPGRADE test as well as the from-scratch one. Reproduce by REWINDING a real instance -- forget the version, drop what it created, leave the rest applied -- not by building a clean one. Migrations are append-only; a new table needs a new version. |
| **A consumed stream is empty, and `x not in ""` is always True.** | `assert json.loads(_body(exc)) == {...}` followed by `assert "KeyError" not in _body(exc)` -- the second passed because `HTTPError.read()` had already consumed the body, not because the text was absent. Written into the very test meant to catch checks-that-pass-for-the-wrong-reason. | Any *"X is not in the output"* assertion must read the output ONCE into a variable and assert against that -- and pair it with a POSITIVE assertion on the same variable, so an empty value fails loudly instead of satisfying the negative. Applies to `HTTPError.read()`, subprocess pipes, file handles, generators. |
| **`storage_golden.py` truncates its diff too, and has no `api_removals.sh`.** | It printed `STORAGE PROFILE CHANGED: 32 changed` and then `... and 17 more changed` -- so the rendered report showed 15. Blessing from it accepts 17 rows never seen. The same run's `api_surface --check` ended `... and 473 more diff lines`, and a grep over THAT truncated list reported a false "1 true deletion" that does not exist. | Compute the delta from the FILES before every bless, not from the report: parse golden and current into `{nodeid: {method: count}}` and diff. Doing it properly showed all 32 storage rows were one expected method with ZERO unexpected ones, and that the api change was a private signature swap plus class-hash churn. `api_removals.sh` does this for `api_surface`; nothing does it for `storage_golden`. |
| **An absent instrument is indistinguishable from an absent server.** | Three times in one session: `pg_isready` is not installed on this box, so "postgres: down" was the binary missing (it had been up 7 days); a shell with a broken `PATH` returned `conn-fail` for every local endpoint that in fact answered 200; and `getattr(x, "transport", None)` -- the attribute is `_transport` -- made a REPRODUCED credential leak print `LEAK: False`. Each negative was the tool, not the system. | Prove the instrument before believing a negative about an external system: `command -v` the binary, hit a known-good target, or carry a control that MUST come back positive. If your probe cannot produce a success, its failure means nothing. Corollary for introspection: verify the attribute exists (`vars(obj)`) rather than trusting `getattr(..., None)`, which returns the same None for "absent" and "legitimately None". |
| **"We are not affected" is where a fix stops happening.** | Dependabot #23 flagged nltk HIGH with `first_patched_version = NONE`. I analysed reachability THREE times across one session, correctly concluded the vulnerable model-artifact APIs were unreachable, and shipped nothing each time -- until Ryan asked why. Reachability was true and did not answer the actual question. `import memotron` was pulling a full NLP toolkit into the MCP server, admin server and dream worker for ONE stemmer none of them can reach, and no gate could see it: all 17 `check.sh` lanes measure the source tree and none inspects the image. | When an advisory has **no patched version**, reachability decides urgency, not whether to act -- the question becomes *why are we carrying this at all*. Measure what the IMAGE contains, not what the code calls: `docker run --entrypoint python <img> -c "importlib.util.find_spec('X')"`, always with a control package that MUST resolve, since "absent" and "probe broken" print identically. And check whether the import is even reachable from a deployed entrypoint before assuming a dependency is load-bearing. |
| **A grep over a config file answers a different question than the one you asked. Comments match.** | Asked which environments arm `requireGatewayIdentity`, `grep -m1 "requireGatewayIdentity:"` reported **prod = true**. It had matched `# 2. set requireGatewayIdentity: true for prod` -- a line inside a comment block describing what to do LATER. Parsing the YAML gives the opposite: `latest` is the ONLY env that overrides it; stage, load, preview and prod all inherit `false`. I nearly reported a security posture backwards from a one-line grep. | **Parse structured config, never grep it** -- `yaml.safe_load` then walk for the key, which comments cannot satisfy. Applies to every `.helm/values-*.yaml` question. If you must grep, grep for the key at the start of a line with no leading `#`, and treat any hit inside a doc-comment block as a miss. |
| **A probe's CONTROL failing is not a result about the target.** | Ten runs of `probe_gateway_mcp.py` came back exit 2 with `control=0 tools`, i.e. **0/10**. Reporting that as "#246 is not fixed" would have been the exact false finding the control exists to prevent -- and the issue history records that near-miss happening once already. The cause: **the gateway key in `.env` carries no `mcp_access_groups`**, so LiteLLM returns no server outcome at all. A key minted with `object_permission: {mcp_access_groups: ["General"]}` gives control=8, target=31, 10/10. | When the control is silent, the run is **PROBE INVALID** -- discard the target reading and fix the control first. Read the probe's own `_diagnose` line before interpreting anything; it named this cause in one sentence. For any gateway probe, use a key minted WITH the MCP access group, not whatever is in `.env`. |
| **A FILTER that excludes the evidence, then reporting the absence you created.** | Asked whether FastMCP 4 depends on the `mcp` SDK, I read its PyPI `requires_dist` and printed only the lines WITHOUT `; extra ==` -- "core deps". Saw no `mcp`, reported *"no dependency on the mcp SDK at all"*, and built a migration epic on it. The three lines that answered the question were `mcp<3.0.0,>=2.0.0 ; extra == "client" / "mcp" / "server"` -- excluded by my own filter. The tool worked perfectly; the query was wrong, and a wrong query returns a confident empty result that looks exactly like a finding. | Distinct from the absent-instrument rule: there the tool is broken, here it is FINE and you asked it the wrong thing. Before reporting an ABSENCE from filtered data, print what the filter REMOVED and eyeball it -- `[r for r in reqs if r not in kept]`. If a filter cannot be cheaply inverted, do not report an absence from it. And prefer the resolver over the metadata: `uv add` / `uv tree` answers "what does this actually pull" without you choosing which fields count. |
| **Two settings, each harmless alone, can be an open door together — and no single-file review sees it.** | `values-prod.yaml` published the MCP ingress host; `requireGatewayIdentity` defaulted false in `values.yaml`. Alone, neither is a finding: an unset allowlist falls back to localhost-only so ingress traffic 421s, and identity-off is the documented default everywhere except `latest`. Together they mean prod's FIRST BOOT answers the full 39-tool MCP surface unauthenticated -- the middleware passes every request through (`mcp_auth.py:446`) and `authorize_tenant` returns immediately, so the tool that redirects a NAMED tenant's extraction traffic is ungated. stage and load were safe only by the accident of leaving the allowlist unset. | When a values file sets something that removes a refusal, ask **what other setting was the refusal standing in for** -- and check it in the SAME breath, across files, including the chart defaults the env file inherits. Then assert the CONJUNCTION in a test rather than a comment: "no environment may set X unless Y is armed", with a positive control so the rule cannot pass by every environment dropping X. A comment in a values file stops nobody, and the per-file review that reads each setting on its own merits is exactly the review that misses this. |
| **A NEW ROLE deploys a placeholder tag, and `Running` hides it.** | Harness replaces `roles.{api,admin}.deployment.image.tag` at deploy from an inline manifest -- that list names TWO roles. The `agent-memory` role added in #240 was invisible to it and shipped the literal `0.1.0`, which `values-<env>.yaml` itself calls a PLACEHOLDER "not guaranteed to exist in every environment's registry". Observed minutes after rollout: api/admin on `0.1.0-3c64d16`, agent-memory on `0.1.0` -- serving a build nobody merged, Ready the whole time. The chart rendered correctly and the deploy succeeded. | After any deploy that ADDS a role, read the image tag PER ROLE and compare it to the sha that triggered the deploy: `kubectl get pods -o jsonpath=...spec.containers[0].image`. A tag without a `-<sha7>` suffix is the placeholder. More generally: when a deploy-time override enumerates its targets, adding a target silently opts out of it -- and the override lives in another repo, so nothing here can fail the build over it. The chart must derive the value instead (`cronjob-dream-worker.yaml` renders `roles.api.deployment.image.*` for exactly this reason). |
| **A gate that CLASSIFIES can report green over something it never checked.** | mcp-forge's `registry reconcile` printed `0 drift` and `toolcount` printed `10 match` on `latest` -- with `jedai_memotron` absent from one and marked `(3p) Reachable —` in the other. `reconcile.py:88` defines first-party as *"has a card in mcp-forge whose manifest_id starts with mcp_jedai_"*, and Memotron has none, because the server now lives in the memotron repo. `:61` then returns EXTERNAL before any reachability test. Both summary lines were true and neither was about the thing I had just registered. | Read the ROW for your item, never the summary line. When a gate reports per-item status, find yours and confirm it says `checked`, not `skipped`/`external`/`n/a` -- those render like passes. And when a tool decides first-party by "is it declared HERE", moving the code out of that repo silently reclassifies it: the exemption arrives without anyone editing the gate. |
| **A file that looks UNIFORMLY stale is following a policy. Read its own header first.** | #136 asked for a gate failing any register citation that does not resolve against `HEAD`. I measured 69 dead citations, then 17 (the 69 was my resolver trying only three path prefixes -- its failure condition satisfied by its own bug), then resolved all 85 pre-split citations through the deleted blobs and **rewrote 75 of them** -- and only then read lines 3-9 of the file I was editing, which pin every reference to `audit-baseline-2026-08-27` **on purpose** and say rewriting *"makes them wrong about history while looking right"*. Against that tag: 82 of 91 fall inside the file they name, **0** out of range. Reverted; tree matched `HEAD` byte for byte. | Uniform, systematic staleness is evidence of a **convention**, not neglect -- read the document's header before the repair, not after. And before calling a path dead, resolve it by basename across `git ls-files` **and** check the disk: 7 of the 17 paths here (`.helm/values.yaml`, `.mcp.json`, `.claude/settings.json`, ...) exist and are merely gitignored. |
| deployed / standalone | `memotron-admin-server` | **no** — `/api/platform/*` 503s |
| local dev | `memotron-local-platform` | yes |

### 3. Read the label before believing a UI negative

`not configured` on Overview/Integration is the **MCP status row** and is *accurate*. The
same words on Project Memory are a rendered 503 and are *false*. Pull the surrounding DOM
text and identify the subject before calling anything a defect.

## Sweep harnesses — they exist, do not rewrite them

Repo root: `~/repos/jedai/worktrees/memotron-productionization` (branch `production-hardening`,
stacked on `split-models` / PR #103). **The old `memotron-implementation` worktree is gone** — if a
doc still sends you there, it predates 2026-08-30.
**Full instructions, including how to start each server shape: `scripts/verify/README.md`.**

```bash
cp .memotron/dogfood.sqlite     /tmp/x.sqlite
cp .memotron/dogfood.sqlite.kek /tmp/x.sqlite.kek   # the .kek MUST travel with the graph
export MEMOTRON_GRAPH_PATH=/tmp/x.sqlite
export MEMOTRON_PROJECT_ID=memotron-dogfood MEMOTRON_MODE=simple
set -a; . ./.env; set +a                                # LITELLM_API_KEY / _API_BASE
```

| what | harness |
|---|---|
| **the lifecycle — run this first** | `scripts/verify/probe_core_loop.py` — supersession, staleness, compaction, token budget |
| does compaction form memory (both transports) | `scripts/verify/probe_compaction_extraction.py` |
| formation round trip | `scripts/verify/loop.sh scripts/verify/probe_formation.py` — exits 0 green / 1 red |
| MCP, both servers | `scripts/verify/sweep_mcp.py <gov_url> <agentmem_url>` |
| SDK, ~140 methods | `scripts/verify/sweep_sdk.py` |
| Admin API, 53 routes | `scripts/verify/sweep_admin.py <base_url>` |
| Admin UI, 9 tabs | `ADMIN_BASE_URL=… npx playwright test tests/ui-sweep.spec.ts` (in `ui/admin`) |
| Repeat any probe | `scripts/verify/replicate.sh 5 "label" ENV=val` |

Baselines to compare against — a run that differs from these has found something:
**core loop `14/14 PASS`** (was recorded here as 15/15 — wrong since the day it was written;
the probe has had exactly 14 `check()` calls at every revision, verified by AST at both its
introduction `79e299c` and today. Nothing regressed, the number was simply never re-derived) ·
MCP governance `OK=16 EMPTY=4 ERROR=16 SKIP=3` · MCP agent-memory `OK=16 ERROR=10 SKIP=1` ·
SDK `Memotron OK=40 ERROR=19 SKIP=30`, `AgentMemoryPlatform OK=22 ERROR=9 SKIP=11` ·
Admin `GET OK=16`, `POST OK=4 HTTP503=15` · UI `8 of 9 tabs clean`.

**Classify errors before reporting them.** `ValueError`/`ValidationError` are the system
correctly rejecting your synthetic arguments — a good sign. `AttributeError`/`TypeError` are
code bugs. In the MCP sweep, 19 of 26 errors were correct rejections.

**The admin POST surface names what it rejects** (`unknown field(s): a, b, c`). Parse that,
drop those keys, retry — a self-correcting sweep.

## Gotchas that will waste an hour

- **`uv tool install` cannot serve the admin UI.** `ui/admin/dist` is not in the wheel. Pass
  `--static-dir <checkout>/ui/admin/dist`.
- **`memotron init` writes a machine-global graph** (`~/.memotron/memory.sqlite`) shared
  by every project. Repoint `storage.graph_path` to a project-local file immediately, or a
  later `init` elsewhere will offer to *move* this project's memory — and that move loses it.
- **`MEMOTRON_DREAM_AGENT_MODEL` is silently ignored** once tenant credentials are sealed.
  Verify the resolved model, never the env var.
- **A fresh tenant needs `agent_register` + `project_memory_configure`** before
  `memory_publish` works. `init` does both; programmatic callers must not forget.
- **Gateway key minting only works on the `-admin` host.** The data-plane host returns
  *"Management routes are disabled for this instance."*
- **Gateway base URL already ends in `/v1`.** Call `POST <base>/chat/completions`;
  `<base>/v1/chat/completions` returns 404. Check this before diagnosing a gateway outage.
- **A thin episode legitimately extracts to nothing under an LLM transport.** Sanitization
  strips secrets and emails *before* extraction, so a short fixture can arrive near-empty and
  the model declines. Rule-based always fires. Measure with a realistic episode before calling
  extraction broken — `scripts/verify/probe_compaction_extraction.py` runs both transports.
- **`initialize_claude_code_project` needs a git remote named `origin`** (`adoption.py:214`);
  a bare `git init` fails with `cannot infer project identity`.
- **Formation is suppressed by one unresolved-problem fact in scope.** If nothing forms, check
  `docs/findings/formation-suppression.md` before debugging — and note the queue is durable,
  so it is deferred, not lost.

## Confirming a fix

A fix is confirmed when a probe that was **red before** is **green after**, and nothing else
moved. Never assert a fix from reading the diff.

1. Reproduce red first. `scripts/verify/loop.sh` exits 1 on the formation bug; a sweep line
   showing `ERROR`/`EMPTY` is the red state for other defects.
2. Apply the fix.
3. Re-run the *same* command. Compare against the baselines above, not against memory.
4. **Replicate** — `scripts/verify/replicate.sh 5 "fixed" …`. One green run is not evidence;
   the formation conditions were 5/5 in both directions.
5. Re-run the neighbouring surface too. Wrapper fixes can move SDK behaviour and vice versa.
6. Update `TAKEOVER-BACKLOG.md` — mark the item and say which command proves it.

### The fix is on `main`. That is not the same as the defect being gone.

Twice now a defect has been marked closed because the fix was verifiably merged. Merged code is
evidence about the tree, never about the surface. **Close a security or exposure finding only
from a probe of the deployed thing.**

Two checks, both cheap:

- **Probe the aggregate, not only the endpoint in the report.** A data-exposure fix bounds the
  paths someone tested. On 2026-09-08 `/api/scopes` was correctly bounded and
  `/api/graph?scope=<neighbour>` correctly refused `400` — while `/api/overview`, which nobody
  had probed, still published every scope key and count in the store plus a neighbour tenant's
  memory `subject_name` through its aggregated `evolution` block. Ask: *what other route reads
  the same data by a different call path?* Grep the helper the fix bounded
  (`discover_graph_scopes` takes `tenant_id` and default-denies) for call sites that **don't**
  pass the bound — `scope_payload_from_client` had two.
- **Carry a control that proves the image is current**, or you cannot tell an incomplete fix
  from deploy lag. Pick a *different* change from the same deploy and confirm it IS live in the
  same payload. B2's `graph_tenant_ids` removal did this: absent from the response, so the image
  carried that day's work, so the remaining leak was code and not lag.

### A round trip can skip the subsystem you are testing

Rule 1 says assert a round trip, not a non-empty response. That is necessary and not
sufficient: **check which code path the round trip actually takes.**

`memory_remember` writes an exact fact DIRECTLY, bypassing extraction. A
remember -> search round trip on `latest` returned the written `relationship_uuid` and
proved storage and retrieval — while `pending_episodes` stayed 0, no worker ran, and
formation was completely untouched. Reporting that as "the loop works" would have been
wrong in exactly the way rule 1 exists to prevent, one level up.

**To exercise formation, submit an EPISODE**: `memory/log` (needs
`checkpoint_reason` in `context_compaction|handoff|session_end`) or `memory/publish`
(needs `project_memory_configure` to have been called for the tenant, else 400).
Then confirm THREE ways, because one number can be a coincidence:

* the run's own counters — `formation-default rel=N nodes=N eps=1`
* the scope totals moving by the same N
* the extracted text naming something only your episode contained

### "It is in the chart" is not "it exists". Check DNS.

Both prod hosts return **NO DNS RECORD** while stage, preview, load and latest resolve —
so a week of reasoning about "promoting to prod" was about an environment that has never
been stood up. The chart declares the ingress; nothing had created it.

    for h in <env>.jedai-memotron-admin.wdprapps.disney.com ...; do
      printf '%-56s ' "$h"; dig +short +time=3 "$h" | tr '\n' ' '; echo
    done

Always resolve a KNOWN-GOOD host in the same command. An empty `dig` and a broken `dig`
look identical, and this repo has already paid for that with `kubectl`.

Related: the environment config delta is far smaller than "stage has none of latest's
config" suggests — three keys (`requireGatewayIdentity`, `keyManager`, `dreamWorker`)
plus `operationalStore.injectAdmin`. Diff the parsed overlays before describing a gap:

    python -c "import yaml;[print(e, sorted(yaml.safe_load(open(f'.helm/values-{e}.yaml')))) for e in ['latest','stage','prod','preview','load']]"

### `check.sh` green is compatible with a `main` that cannot build at all

Every lane measures the SOURCE TREE. On 2026-09-09 `main` was unbuildable for three hours
across two merges while `check.sh` reported 16/16 and `ci-build-check.sh` passed, because
neither builds the container. Harness failed at `Dockerfile:37`; `latest` silently kept
serving the last SUCCESSFUL build, two PRs old.

`npm update` had rewritten `ui/admin/package-lock.json` inconsistently. **`npm ci` refuses
a lockfile that does not satisfy itself; `npm test` and `npm run build` do not** — they
use whatever is already in `node_modules`, which is the tree `npm update` just installed
correctly. So the local evidence was true and unfalsifiable.

The `image` lane now runs `docker build`. Before believing any "it deploys" claim, also
check the build actually succeeded:

    gh api /repos/jedai/memotron/commits/<sha>/status --jq '{state,ctx:[.statuses[].context]}'

### The trap you already wrote down is the one you will walk into

The lesson "a chart-only deploy does not change the image tag" was ALREADY in this file on
2026-09-09, and I still read a pod restart three minutes after a merge as that merge
deploying. The pods carried the previous image.

Prose in a skill is read at session start; a gate fires at the moment it is needed. When a
lesson recurs despite being recorded, the fix is to make it mechanical — that is why the
`image` lane exists rather than another paragraph here. **Check the image TAG against the
merge SHA, never the pod start time.**

### A flag can be armed per-environment and off per-role, and reads as armed either way

`values-latest.yaml: requireGatewayIdentity: true` was taken for a whole session to mean
"this environment authenticates its callers". `roles.yaml` rendered the env var for the
**api role only**, so two merged changes that read it in the ADMIN process shipped inert
and would have stayed 503 after deploying.

Reading the values file is not enough. Render the chart and read the container:

    helm template dw .helm -f .helm/values.yaml -f .helm/values-<env>.yaml \
      | grep -A5 'name: MEMOTRON_REQUIRE_GATEWAY_IDENTITY'

    kubectl get pod -n jedai-memotron <pod> \
      -o jsonpath='{.spec.containers[0].env[*].name}'

Use a POSITIVE CONTROL — a role that should have it — or an absent variable is
indistinguishable from a broken query.

**And: your own change can falsify a comment that was correct when written.** The template
said admin "would simply be IGNORED" because `admin_server/` imports nothing from
`mcp_auth` — true of `mcp_auth`, and falsified when the read moved to
`memotron.identity`. After moving a symbol, grep the CHART and the docs for the old
justification, not just the call sites.

### A FAKE that derives one value from another cannot test that they stay apart

The refusal-must-not-echo-the-key test passed on its own construction. The shared stub
resolved a key to `f"alias-{key}"`, so the key appeared in the refusal *via the alias* --
and a real `key_alias` is assigned by the gateway and carries no key material. The
assertion was about the fake, not the code, and it failed the moment the fake was made
realistic.

**Before asserting that two values never co-occur, check whether your fixture derives one
from the other.** Then pair it with a positive assertion on the same string, so an empty
or truncated message cannot satisfy the negative -- here, that the refusal DOES still name
the alias, which is documented as safe to return.

### Asserting a method does not touch an attribute? Check BOTH spellings

`self.platform` and `getattr(self, "platform", None)` are the same read and only one is an
`ast.Attribute`. A tripwire written for the first is blind to the second -- which is the
form `_resolve_dream_request` actually uses, so the gate was blind to a real instance of
the hazard it names, the second time that shape has appeared in this repo.

Better still, do not maintain a name list. Assert the *positive* structure instead: a body
that CALLS the guarded accessor has already passed the check, so exempt on the call rather
than on the name. That version needs no editing when a route is added, and it is what
turned five "offenders" into zero without weakening anything -- `_platform_status_payload`
reads `self.platform` legitimately, one line after calling `self._platform()`.

### A patched-version number is not a fixed-version test

Dependabot names a `first_patched_version`; that is not the same as "any higher number is
safe". Read `security_vulnerability.vulnerable_version_range` and check membership:

    gh api /repos/jedai/memotron/dependabot/alerts \
      --jq '.[] | select(.state=="open") | "\(.dependency.package.name) \(.security_vulnerability.vulnerable_version_range)"'

GHSA-82fw-gwwq-j7x9 named `4.1.11`, and `npm update vitest` landed **5.0.0** -- because
every dependency in `ui/admin/package.json` is declared `"latest"`, so an update resolves
the newest MAJOR rather than the newest patch. The range was `>= 2.1.0, < 4.1.11`, so
5.0.0 does clear it; the version ordering alone would not have told you that, and a major
bump is only acceptable once the suite has actually run (`npm test` in `ui/admin`, plus
`npm run build`, since CI needs `ui/admin/dist`).

### `gce-internal` means "not internet-facing", NOT "in-cluster only"

Do not reason about exposure from an ingress annotation. `kubernetes.io/ingress.class:
"gce-internal"` is an internal load balancer: it resolves to RFC1918 and is reachable from the
whole corporate network. Both `latest` ingresses carry it and both answered `200` to an
unauthenticated `curl` from a laptop off-cluster.

    dig +short latest.jedai-memotron-admin.wdprapps.disney.com   # -> 10.154.184.212
    curl -s -o /dev/null -w '%{http_code}\n' https://<host>/api/overview

Contrast with genuinely in-cluster: mcp-forge's MCP servers (`mcp-jedai-postgres`, `-redis`,
`-validator`) declare **no ingress at all** — a ClusterIP Service the gateway reaches
internally. Memotron declares two ingresses, so fronting its MCP surface with the gateway
would not cover the admin surface. `--no-admin-writes` is not a mitigation for a read leak.

## The code gates — added by the productionization branch (2026-08-27/28)

`finding_gate.py` below lints the RECORD. These lint the CODE, and none of them existed
when the rest of this skill was written. One command runs the lot:

```bash
bash scripts/check.sh        # 16 lanes; `--list` prints them, `check.sh <lane>` runs one
# lint format types suppressions scripts mixin-dag coupling tests receipts storage postgres cov-floor parity-cov diff-cov corpus image
#
# `image` (2026-09-09) is the ONLY lane that builds the artifact the cluster runs.
# Every other lane measures the source tree, and that gap let `main` sit unbuildable
# for three hours across two merges while check.sh reported 16/16 -- an inconsistent
# `package-lock.json` that `npm ci` refuses and `npm test` does not notice.
#
# NOTE the order: `postgres` now runs BEFORE cov-floor and diff-cov, because it contributes to
# the coverage report they read. Until 2026-08-31 it ran after them and contributed nothing.
```

Each lane answers a different question, and the distinctions matter when one goes red:

| gate | question | blind to |
|---|---|---|
| `pure_move.py <ref> .` | did any function body **change**? bytecode identity | class-level statements — pydantic field defaults are invisible to it |
| `api_surface.py --check` | did a public symbol **disappear**? | anything reached through `__getattr__` (the `memotron.graph` shim) |
| `api_removals.sh` | the FULL removal set | nothing — use this, not `--check`'s printed diff, which truncates |
| `ast_identity.py <ref> .` | did any module's **parsed tree** change? the gate for a format-only commit | comments and blank lines — deleting every comment in the repo passes it. Docstrings *are* compared. |
| `mixin_dag.py` | is the mixin call graph **acyclic**, layers as declared? | coupling *density* — it only measures direction |
| `receipt_golden.py` | did the suite **receipt** the same things, in the same order, with the same mutate/no-mutate flag? | code the suite does not execute |
| `storage_golden.py` | did each test drive the storage contract with the same **multiset** of calls? the guard for the deliberate body edits `pure_move` stops covering | ordering (blind on purpose), argument values, the Postgres backend, and one known-flaky concurrency row (`KNOWN_COUNT_DRIFT`) |
| `coverage_floors.py` | did any module lose coverage? | it reads a report — stale JSON fails STALE by design, **and since 2026-09-07 that means stale against `src/` OR `tests/`**. It watched `src/` only, so a TEST-ONLY edit slipped straight through: `mcp_auth` read 95.3% against its 96.3% floor, tests were added for the uncovered branches, and it still read 95.3% — a bare `pytest` collects no coverage at all. That reads as "the new tests did not help", which argues for lowering the floor |
| `parity_coverage.py` | **new lane `parity-cov` (2026-09-01)**: is each storage method exercised on BOTH engines? compares per-method covered-line sets across `storage/sqlite` and `storage/postgres`. Baseline `{sqlite_only: 20, postgres_only: 7}` of 172 shared methods | needs a coverage.json that saw both engines -- **SKIPS without `MEMOTRON_TEST_POSTGRES_DSN`**, because a hermetic report makes every Postgres method read as dark and it would print ~170 false gaps. A gap is NOT a defect: it says the suite does not look there. `cov-floor` and `diff-cov` structurally cannot see this -- code uncovered AND unedited is invisible to both, which is how the `anchor` half-life bug survived a 3,420-line parity suite |
| `coupling_report.py` | **now a GATE and a `check.sh` lane (2026-09-01)**: import cycles, upward tier edges, distinct-member breadth, private reaches — structure that is wrong regardless of how often it is used | the call-site counts it used to lead with (279 / 78 / 72) are still printed but are **informational and cannot fail a build** — a call count cannot tell heavy use of a right abstraction from a leak through a wrong one. No `--bless`: moving the baseline is a hand edit with a reason |
| `mypy_suppressions.py` | is every `disable_error_code` still earning its place? | inline `# type: ignore` (that is `warn_unused_ignores`, already on) |
| `skill_freshness.py` | is THIS skill still in sync and still naming the gates that exist? | a claim that is well-formed and false |
| `script_freshness.py` | do the SCRIPTS still describe the tree? every script imports, every path literal resolves, every `file.py:NNN` citation exists and is in range | whether a citation is *right* — `_graph.py:516` stays well-formed when line 516 moves. Cite a symbol as well as a line. |
| `api_since_cutover.py` | what left the PUBLIC API since before the effort? diffs against a baseline pinned to merge-base `57ae5d4`, **not blessable** | relocations are reported, not failed — and it cannot tell you a name that merely became *harder* to import |
| `doc_symbols.py` | do the DOCS name symbols that exist? every `from memotron… import X` in a fenced python block resolves, and every `Model(kwarg=…)` uses a field that model declares | whether the snippet is *correct* — it can construct cleanly and still teach the wrong thing |
| `live_lane.sh` | do the probes that make REAL calls still match their **recorded** outcomes? `tests/live_lane.baseline.tsv` records an EXPECTED EXIT per probe, not zero — `probe_kek` is *supposed* to exit 1 until #123 lands | it is not a `check.sh` lane and cannot be: it needs a gateway key and a live Postgres, and it REFUSES rather than skipping. Milestone-scoped. |
| `sanity.sh` | does the **product** build, install and run — and identically to `takeover`? | anything past a SQLite, no-LLM, no-control-plane smoke path |

### The two lanes that skip by default, and how to stop them skipping

`postgres` and `corpus` print SKIPPED and are never counted as passing. A green `check.sh` without
them says nothing about either. **Postgres is no longer hard to run** — measured 2026-08-30, first
real run in this worktree: **76 passed in 95s**.

```bash
docker compose -f docker-compose.local.yml up -d postgres     # postgres:16-alpine, host port 55432
export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=memotron"
bash scripts/check.sh postgres                                 # must PASS, not SKIP
```

**The `C` collation is not cosmetic.** The compose file passes
`--encoding=UTF8 --lc-collate=C --lc-ctype=C` because the state-hash SQL orders with
`COLLATE "C"`; a non-byte-ordered collation makes graph state hashes diverge from SQLite. If you
point the DSN at some other Postgres, check `datcollate` first:

```bash
docker exec <container> psql -U memotron -d memotron \
  -c "SELECT datcollate, pg_encoding_to_char(encoding) FROM pg_database WHERE datname='memotron';"
```

Parallel worktrees collide on 55432 — `DW_PG_PORT` exists for that, or reuse a running container
once you have checked its collation.

### The live lane — real gateway, real LLM

Everything above is hermetic or static. **None of it makes a real call**, and `sanity.sh` runs a
rule-based extractor in an offline venv. With no gateway key the system silently resolves
`RuleBasedExtractionTransport` and **no dream agent at all** — so the probes still run, still pass,
and are not testing what they claim.

```bash
set -a; . ./.env; set +a
uv run python -c "from memotron.runtime import build_transports_from_env as b; print([type(x).__name__ for x in b()])"
# live:    ['OpenAICompatibleExtractionTransport', 'OpenAICompatibleDreamAgentTransport']
# NOT live:['RuleBasedExtractionTransport', 'NoneType']
```

Minting a key for `latest`: the master key is `~/repos/jedai/mcp-forge/.local-secrets/litellm-latest-key`,
and `POST /key/generate` goes to the **`-admin`** host — the data-plane host answers *"Management
routes are disabled for this instance."* Give it a `key_alias` and a `duration`; there are already
104 keys on latest and an unlabelled one is untraceable.

****`sanity.sh` is the one to reach for when you doubt a refactor.** Every other gate measures
the source tree; none of them starts the product. It builds a wheel, installs it into a clean
venv, drives the SDK, counts MCP tools — then does the same from a `takeover`-built wheel and
diffs. The A/B half is what makes it useful: the dream pass reports `created=0` on *both*
wheels, so alone it reads as a broken formation path and in the pair it is the documented
no-worker behaviour.

## Measurement traps in this refactor — check these BEFORE reporting a number

Every one of these produced a wrong number that was acted on, or nearly was. They
are listed because the refactor keeps generating measurements and the failure is
always the same shape: **the number was real, and it was measuring the wrong thing.**

| trap | what it looks like | the check |
|---|---|---|
| **A baseline you did not MEASURE is a claim, not a baseline.** | Compared per-method coverage across the two storage engines, found 20 methods covered on SQLite and dark on Postgres, filed an issue on that number -- and set the reverse direction's baseline to `0` without ever running it. It is **7**, and it contains `set_tenant_llm_credentials` and `active_relationships`, so it was not the harmless direction either. The real figure is 27 of 172. This happened *inside* a gate whose entire subject is unchecked assumptions about the other engine. | If a comparison has two directions, run BOTH before blessing. Blessing from a measured run costs one command; a guessed zero silently under-reports until someone re-derives it. |
| **A gate that parses another tool's stdout is defeated by COLOUR, silently.** | `FORCE_COLOR=3` in the environment made mypy emit ANSI through a pipe. `mypy_suppressions.py` expects a literal `: error: ` and the escape codes land between the colon and the word, so 237 error lines parsed as ZERO, all 89 suppressions read as dead, and the lane demanded their deletion -- which would have unsuppressed 201 live errors. The repo had not changed; only the environment had. Fixed 2026-09-02 (`9c513bc`). | Force `NO_COLOR=1`/`TERM=dumb` in the subprocess AND strip ANSI before parsing -- a parser that matches nothing looks exactly like a clean run. And when a gate's verdict flips with NO corresponding repo change, suspect the environment first: `env | grep -iE 'FORCE_COLOR|CLICOLOR|TERM='`. |
| **`check.sh`'s summary table can report a lane that evaluated NOTHING as PASS.** | Both golden lanes decline to compare when the recorded profile came from a failing suite, and until 2026-09-02 they signalled that with `return 0` -- which `run_lane` maps to PASS. Two agents reported `storage PASS` off that table while the lane had skipped; the second time it masked a broken tests lane. Fixed in `501a787`: exit 3 now means *declined to judge* and prints `SKIPPED (nothing evaluated)`. | Do not take a lane's verdict from the summary table alone when it matters -- run that one script and read its last line. And when writing a gate, never let "evaluated nothing" share an exit code with "evaluated everything and it was fine". |
| **A ratio floor punishes DEDUPLICATING covered code.** | `cov-floor` failed on `memotron.embedding` (89.5% -> 88.6%) after the transport extraction. No coverage was lost: 26 COVERED statements moved to `gateway.py` where they still run (`coverage.parser`: 102 -> 76 statements), and the uncovered set was byte-identical -- the same eight validation branches, each verified absent from the diff. The numerator held; the denominator shrank. | When a ratio floor fails after a refactor, compare the DENOMINATOR before blaming the numerator: `coverage.parser.PythonParser(...).statements` on the old and new file. Writing tests for branches that were already dark, to restore a percentage, repairs the wrong thing. Re-derive the floor and record WHY it moved. |
| **Coverage records STATEMENT-START lines, not physical lines.** | A multi-line call's inner lines are in neither `executed_lines` nor `missing_lines`. Treating "not executed" as "not covered" reported **3%** where the truth was **100%** — and 3% would have cancelled a whole phase. | Resolve to the enclosing `ast.stmt` and test THAT line number. |
| **`grep -c` counts LINES; an AST walk counts NODES.** | `client.py` read 271 one way and 279 the other. The gap looked like a regression I had just caused. | Never compare two numbers produced by different methods. Re-measure both sides with one tool. |
| **A rendered diff can be truncated.** | `api_surface.py --check` prints a diff for a human and elides it when large: it showed 3 removals against an actual 22, and a commit message repeated the 3. | Compute the set. `scripts/verify/api_removals.sh` exists for exactly this. |
| **A measurement taken on an unverified tree state is not a measurement.** | Reported cross-object private reaches as 43 -> 10. The 10 came from an intermediate state where a delegate called ITSELF — self-recursion is not a cross-object reach, so the bug flattered the metric. It is 11. | Before recording a number, confirm the tree it came from passes. |
| **zsh treats `$VAR:s...` as a history modifier.** | `git show "$BASE:src/..."` silently mangles to `$BASE/src/...` and every result comes back 0. Twice read as "the files do not exist". | Brace it: `"${BASE}:src/..."`. |
| **zsh does NOT word-split an unquoted `$VAR`.** | `FLAGS="-f a.yaml --set x=1"; helm template . $FLAGS` passes the WHOLE string as one argument — helm reported `open  -f a.yaml --set x=1: no such file`, wrote an empty file, and the next `diff` against it printed a 630-line "difference" that was pure artifact. The bash habit is wrong here. | Inline the flags, use an array, or `${=FLAGS}`. And treat *every* line of a diff against a file you just generated as suspect until you check that file is non-empty. |
| **Piping a gate to `tail` REPLACES its exit code.** | `bash scripts/check.sh 2>&1 \| tail -22` reported **exit 0** while printing `check FAILED` — the status belonged to `tail`. The harness then announced the command "completed (exit code 0)". This is the same defect class the gates exist to catch: a passing condition satisfied by something other than what it claims to measure. | Redirect to a file and read `$?` on its own line: `bash scripts/check.sh > out.log 2>&1; echo "EXIT=$?"`. Never take a gate's verdict from a piped tail. |
| **A fix can make an existing test VACUOUS without making it fail.** | Bounding the admin control plane to one tenant changed *why* two security tests passed: their blocked `customer:` scope stopped being registered, so the refusal arrived as `"not registered"` instead of `"not authorized"`. Still closed — but the PRINCIPAL check those tests exist for stopped running, and editing the expected string would have left them passing on the wrong assertion forever. | When a change alters WHY something fails, ask whether the original check still runs at all. No gate reports this: the suite is green either way. Repair the precondition (here `extra_scopes`, so the scope is registered AND unauthorized), never the expectation. |
| **Handing someone a command with a placeholder you never resolved.** | Asked for "exactly what to run", produced three commands in a row that each failed on a value not present on the machine -- `<latest DSN>`, `<ALIAS>`, `<your-vault-host>`. They were run verbatim, because there was nothing to substitute. `VAULT_ADDR` was set nowhere: not in the environment, not in shell profiles, not in the workspace. | Before writing a command that needs a credential, spend ONE command checking the path exists: `env \| grep -i vault`, `ls .local-secrets/`, `grep -r VAULT_ADDR ~/.zshrc`. A placeholder handed to someone else is a QUESTION, not an instruction — resolve it, or say plainly it cannot be resolved from here. |
| **Verifying a refusal without a positive control.** | Confirming a scope guard closed a live leak needs THREE reads, not two: the neighbour refused, the other neighbour refused, and **the caller's own scope still returning 200**. Without the third, a guard that refuses EVERYTHING passes the first two and is a broken console. | Same rule as the positive controls in the tests, applied to production probes: an assertion written only in the negative cannot tell a fix from an outage. Always probe the thing that must still work. |
| **`grep -c` in a poll loop makes the success case unreachable.** | A 24-minute watch for a rollout reported "still listed" every attempt and timed out, while a manual probe in the same window showed the change already live. `n=$(... \| grep -c 'pat' \|\| echo 9)` — `grep -c` prints `0` **and exits 1** on no-match, so the `\|\| echo 9` fires on the SUCCESS case and `n` becomes `0\n9`, never equal to `"0"`. | Use `\|\| true`, or test the output instead of short-circuiting on exit status. More generally: **when two observations of one system disagree, suspect the instrument first** and prove it against a known input — `echo "no match" \| grep -c foo \|\| echo 9` settles it in one command. |
| **A `Forbidden` from the WRONG kube context, and a `can-i` against a namespace that does not exist.** | "kubectl is refused" stood for two weeks and blocked a workstream. It came from a `Forbidden` produced against the **stage** context (the shell default), then was reinforced by `kubectl auth can-i ... -n memotron` returning `yes` — for a namespace that does not exist. The real one is `jedai-memotron` (`namespaceOverride`); `preview` uses `preview-jedai-memotron`. On `latest`, `list pods` and `create pods/exec` are both permitted, and the api pods carry `MEMOTRON_OPERATIONAL_STORE_DSN` plus the `memotron` CLI. | Print `kubectl config current-context` and confirm the namespace EXISTS (`kubectl get ns`) in the same breath as any access verdict. `can-i` does not validate the namespace, so it will cheerfully answer about a fiction. An environment-scoped claim must carry the environment it was measured in. See `docs/environment-bring-up.md`. |
| **Sizing a branch with ancestry in a squash-merge repo — then writing it into STATE.md.** | `feat/next-ws25-ws28` was called "unmerged work gating the C-track" and sized twice (2,150 lines from a filtered docs grep; then 48k lines from `git log main..branch`). Both wrong: the work was ALREADY ON MAIN via PR #44, and STATE.md said so at line ~1590. Under squash merge, `git log main..branch` and `git diff main...branch` report a merged branch as unmerged forever, and the files that look "added by the branch" can be pre-split monoliths `main` now carries as packages. | Settle it with **content**: `git show origin/main:<file the branch adds>`. Two-dot `git diff main branch` also tells the truth where three-dot does not. And **before adding any fact to STATE.md, grep STATE.md for the subject** — a new claim that contradicts an existing entry is a signal to stop, not a newer truth. This one landed in "Do not re-derive", the section future sessions are told to believe. |
| **Blessing a golden mid-investigation, then editing on.** | Happened TWICE in one session: bless `api_surface.golden.txt`, keep changing source, watch the gate go red again. The second cost a full `check.sh` (~20 min) plus a re-run. | Bless from the FINAL tree, as the last step before committing — never mid-investigation. Any source edit after a bless makes it stale, and the gate is the only thing that will say so. |
| **`grep` answers a different question than the call graph.** | Scoping a guard, a textual scan for `_require_agent` reported 16 public methods as unguarded. A 20-line AST walk showed **23 of 29 reach it** — via `self.agent_scope(agent_id)`, which a string search cannot see. Acting on the grep would have scattered redundant checks and missed that the real gap was three lifecycle methods. | For "does X reach Y", walk `self.<attr>` calls transitively. A string match answers *does this method mention Y*, and the gap between those is invisible until someone counts. |
| **Mutating a security fix in one direction only.** | Every guard needs two mutations: revert it (does it still refuse?) and make it refuse UNCONDITIONALLY (does anything still work?). The second is the one that gets skipped, and it catches a "fix" that works by refusing everything — here that would have taken `dream_history` down on four environments while looking like a win. | Run both. And if `diff-cover` flags the guard's CALL SITE as uncovered, add a third: delete the call. Guard logic and guard wiring need separate tests — otherwise deleting `require_fleet_wide_write(...)` from the tool leaves every test green. |
| **git auto-merges two golden blessings, and the result can match NEITHER tree.** | Cherry-picking one re-blessed branch onto another merged `api_surface.golden.txt` with no conflict at all. Goldens are whole-tree artifacts, so a clean textual merge is not evidence the merged golden describes the merged code — and the gates would then pass against a fiction. | After ANY merge or cherry-pick touching a golden, re-bless on the merged tree and run the gate; never accept the auto-merge. Use the GATE, not a hand-rolled diff — the first check here was itself wrong (`grep -v '^$'` stripped blank lines from one side and reported a false mismatch on a trailing newline). |
| **A gate can be blind to the exact hazard it names.** | A chart test written for the `#`-between-continued-flags trap used `sh -n`. Mutation said **MISSED**: `helm lint`, `helm template`, `sh -n` and a text `in script` assertion ALL pass on the broken form — the YAML is valid, a comment IS valid syntax, and the swallowed flags are still in the *text* while absent from *argv*. | Mutation-test every gate before trusting it, and prefer EXECUTING the artifact to inspecting its text. Two follow-on traps in the same fix: dropping `exec` from the probe modelled a loud 127 the real script cannot produce (`exec` replaces the shell, so the true failure is silent), and `stdout.split()` DROPS empty arguments — use NUL delimiters when an empty argv entry is meaningful. |
| **A diagnostic endpoint reports its own misconfiguration in a field nobody reads.** | `/api/tenant-config` on latest carried `warnings: ["tenant id 'wdpr-demo' matches no tenant in this graph (found: jedai-platform) ... purge would target the wrong tenant"]` for months. The endpoint was fetched for #185 and only `operator.store` was read. | When reading a diagnostic endpoint, read the `warnings`/`errors` array FIRST, before the field you came for. A component that reports its own breakage is only as good as the field someone looks at. |
| **Blessing a golden from a FILTERED run silently deletes rows.** | `receipt_golden.py` treats missing rows as non-fatal (a filtered run legitimately observes fewer), so blessing from a hermetic-only run drops every Postgres row from the golden and nothing ever fails on their absence again. | Bless ONLY from one full single-process run reporting `0 not observed` — `pytest -m "not corpus"` with the DSN set, not the two-lane `check.sh` shape. Read the script's pass/fail branches before assuming which deltas are fatal. |
| **A green static gate is not evidence the code runs.** | A blanket string replace rewrote a delegate's own body into infinite recursion. **ruff passed. mypy passed.** Only executing it failed. | Run something. `bash scripts/verify/sanity.sh` builds, installs and A/Bs the wheel. |
| **`pure_move` reads function BODIES only.** | A name used in a DEFAULT ARGUMENT is invisible to it -- `TENANT_ACTIVE_PROMPT_PROFILE` was unbound and pure_move said PASS. ruff's F821 is the only guard covering that position. | Know which gate covers which position: pure_move = bodies, ruff = signatures and module scope, mypy = cross-class resolution. |
| **A `_*.py` glob MATCHES `__init__.py`.** | Emits `from pkg.__init__ import X` -- a hard circular ImportError that ruff and pure_move are both clean on. Hit in the models split, then AGAIN in a different tool two sessions later. | Exclude it explicitly in every tool that globs private modules. |
| **Reverting one file of a multi-file extraction leaves duplicates.** | `git checkout -- __init__.py` while the extracted mixin survives on disk defines the same methods TWICE. The duplicate resolves fine and the leftmost silently wins. | `pure_move`'s "1 definition before, 2 after" is the only thing that reports it. Revert the whole cut, never one side. |
| **A blanket `Class.` -> `Mixin.` rewrite can point at the wrong sibling.** | Two calls targeted a method that lives in a DIFFERENT mixin. The package imported fine; it would have been an AttributeError at call time. | mypy catches it (`"type[X]" has no attribute`). Import-testing does not. |
| **`--bless` on a golden is only as good as the run that fed it, and the bad run is SILENT.** | **This row said the OPPOSITE until 2026-09-01 and the advice was live for a day.** It read *"bless with the DSN UNSET; DSN set -> 52 not observed and bless correctly REFUSES"*, which was true only while the golden held 57 `[postgres]` rows that were pure `close:1` stubs from SKIPPED tests. Those stubs were deleted at source, and the arms inverted. Measured on the same tree, twice, after the deletion: **DSN SET, no `-m` -> 0 not observed** (the rich run, safe to bless); **check.sh's profile -> 5 not observed**, because its tests lane filters `-m "not postgres and not corpus"` and `restore_observations` DISCARDS the Postgres lane's observations. Blessing from check.sh's profile silently drops those five DSN-only rows and nothing fails -- the loss surfaces days later as a "5 new" HARD FAILURE with no traceable cause. Hit for real on 2026-09-01 by the person who wrote the correction. | Bless from ONE full `uv run pytest -q` with the DSN **SET** and **no `-m` selector** -- never from `check.sh`. **Confirm `0 not observed` before blessing**: that is the signal the input is the rich run. Then check the golden's own `git diff` and attribute every added row. `storage_golden.py`'s docstring is the authority; if it and this row ever disagree again, believe the docstring and fix this row. |
| **A suppression that FIRES is not thereby justified.** | `mypy_suppressions.py` used to ask only "does this code still fire?". `mcp_server`'s `attr-defined` fired 27 times, the gate said "still earning its place", and all 27 were real defects (#132) -- including `run_due_dreams`, which raised on every call. Measured A/B 2026-09-01 with two defects injected behind an existing suppression: old gate PASS exit 0, new gate `local_platform: attr-defined 13 -> 15` exit 1. | The gate now ratchets per `(module, code)` against `tests/mypy_suppressions.baseline.tsv`. A RISE fails; a FALL is reported and passes, so re-bless in the commit that paid the debt (`--bless`). It catches GROWTH, not a brand-new suppression's initial size -- that is only visible in the pyproject diff. |
| **A cluster observed AT REST cannot answer whether it deploys.** | T0-8 was refuted correctly -- api `2/2` in three clusters, mounts no volumes, only `admin` holds the PVC at `replicas: 1`, "correct RWO usage". All true, all steady-state. The first real deploy then deadlocked on that PVC: `RollingUpdate` starts the replacement before the incumbent releases a ReadWriteOnce volume. `Multi-Attach error`, every merge, filed as #146. | When refuting a deployment claim, name the lifecycle phase your evidence covers. `kubectl get` describes a system that is not changing. If the claim is about rollout, the evidence has to be a rollout -- or say the claim is untested. |
| **Before killing a long-lived process, diff the mtimes of what it loaded against its start time.** | A 5d15h-old process held the only good copy of a KEK; the on-disk file had been replaced four days after startup, so disk and RAM had diverged silently. SIGTERM destroyed the real key. The evidence was already on screen -- a `.kek` dated later than the process holding it open -- and went uncross-checked. | `ps -o lstart= -p PID` against `stat -f %Sm` on its inputs. Anything newer than the process is state you are about to lose. Snapshot first, and prefer `--backup`-style copies over a plain `cp` for anything with a WAL. |
| **A hung job and a slow job look identical from the outside; only one of them ends.** | Waited 21 minutes on a Postgres suite I had myself measured at 144s earlier the same session. Cause: Docker Desktop manually paused, visible in one `docker ps`. A timing anomaly noted earlier the same day (217s vs 443s, same lane) had been left unchased. | Know the baseline BEFORE starting a long job. At ~2x elapsed, stop waiting and check the output file's mtime plus the external dependency (container, DSN, network). Waiting longer is only correct if something is still moving. |
| **A GUARD TEST whose passing condition is met by the move that defeats the guard.** | #126 added a scope check inside `_scope`, plus a test asserting every tool either declares `scope_kind` or is a listed exemption. An independent reviewer added a tool with a **required** `scope_kind` that built its own `MemoryScope` and never called `_scope` — **all six guard tests passed.** Declaring a scope is not routing through the guard. The same file had already caught two neighbours of this shape (a tool taking `tenant_id` instead; three tools declaring a scope and going fleet-wide by omitting it), so the pattern recurred three times in one change. | For any choke-point guard, write the test against the **BODY**, not the signature or the schema: every subject must be shown to CALL the guard, and the guard's primitive (here `MemoryScope`) must be constructible nowhere else. Then prove it by adding the bypassing tool yourself and watching the test fail. A guard test that has never failed against a real bypass is decoration. |
| **Enumerating what a server exposes by PARSING ITS SOURCE.** | A hand-built list of MCP tools that skip the scope helper said **5**; `await mcp.list_tools()` said **6**. The one the parser lost was `clear_tenant_llm` — the tool that *deletes* a tenant's LLM credential. A decorator that fails to register, a rename, a tool defined in another module, or a signature the parser mis-reads are all invisible to grep and visible to the registry. | Enumerate from the running registry, never from the source text, and make the enumeration a test. `tests/test_mcp_tool_registration.py` and `tests/test_mcp_scope_guard.py` both exist because of this. |
| **Moving a synchronous storage call into `anyio.to_thread` breaks SQLite and is INVISIBLE on Postgres.** | The identity middleware ran its `key_principals` read in a worker thread alongside the blocking gateway call. `SQLiteStorageBackend` connects with the default `check_same_thread=True`, so every request against a SQLite store raised `ProgrammingError`; Postgres has a dedicated loop thread and survives, so `latest` and prod would have been fine and local dev would have 500'd on every call. Every test substituted a fake lookup, so no test covered the one line that matters in production. | Only put the genuinely blocking network call in the thread. Before moving ANY storage call off the loop, check the SQLite side — the engines' thread affinity differs, and the deployed engine is the forgiving one. If a collaborator is faked in every test, say so out loud: it is the line with the least coverage and the most consequence. |
| **A refusal message that quotes an upstream error can return CREDENTIALS.** | The middleware relayed `GatewayRequestError`, whose message is built as `"<description> failed with HTTP <code>: <body>"` — the gateway's body verbatim. Observed live: LiteLLM's 401 body carried `Received API Key = sk-...` **and the key's verification-table hash**, returned to an unauthenticated caller, where any 4xx-body-logging intermediary would capture it. | A refusal is the one response an attacker can always elicit, so it is the last place to relay someone else's error text. Carry two messages — a full one for the log, a fixed one for the caller — and assert the secret's absence from the response body in a test. Keep the actionable exception (an unbound alias needs its name and the bind command) and justify it. |
| **TWO runs at once against the same local Postgres and coverage files poison EACH OTHER.** | A `pytest` run reported **39 failed, 15 errors** on a tree whose only recent change was a bounded cache. A `check.sh` was running concurrently — and *its* Postgres lane failed in the same window, so both runs looked broken and neither was. Run alone: 1715 passed, and `check.sh` 16/16 twice. The failures name application code, not the DB, so nothing in the output points at the real cause. | Before believing a failure burst, check for a second run: `pgrep -fl "pytest\|check.sh"`. This suite shares one `memotron_db`, one `coverage.json` and one storage-profile recorder output, so concurrency is not slow — it is WRONG. Never start a second run "to save time" while one is going, and if a burst appears with no matching change, re-run solo before diagnosing anything. |
| **An EMPTY `kubectl` result is indistinguishable from a broken query — and it reads as a safety clearance.** | Verifying that nothing mounted a PVC before deleting it (irreversible, `reclaimPolicy: Delete`), three separate queries returned empty and each looked like "nothing mounts it". All three were broken: `perl -e 'alarm N; exec @ARGV' kubectl ...` had SIGALRM kill kubectl mid-flight, and zsh passed `$K get pods` as a single command name because **zsh does not word-split an unquoted variable** — "command not found" scrolled past while `grep -c` cheerfully counted `0`. Only a positive control caught it: asking the same query to print volume NAMES, which must be non-empty, showed the query itself returning nothing. | Before acting on an empty `kubectl` result — especially to authorise a deletion — run a control that MUST return rows and confirm it does. Inline the command; never build it in a shell variable. Use `--request-timeout=30s` rather than an external alarm wrapper, which kills the process and leaves stdout empty rather than erroring. Then confirm the destructive act by its own second signal: after deleting a claim whose PV reclaims `Delete`, the PV must go `NotFound` and the workloads must stay `Running`. |
| **A single failed observation + a plausible mechanism reads exactly like a confirmed finding.** | One 404 during a probe. I concluded MCP sessions were pod-local and unusable behind the ingress, and "confirmed" it twice: an A/B (worked pinned to a pod, failed through the ingress) and a config read (`sessionAffinity: None`, 2 replicas). Both were *consistent* with the hypothesis; **neither tested it**. The A/B changed two variables at once, since pinning also bypassed the ingress. The config described a mechanism that COULD produce the symptom, never that it did. I was one command from filing the issue. | **Reproduce the failure before diagnosing it.** Repeating the call succeeded 8/8 and the whole diagnosis collapsed — it was transient rollout convergence. The tell is that every piece of evidence was something you went looking for AFTER forming the hypothesis, and each was a thing the hypothesis PREDICTS rather than a thing that could have contradicted it. Ask "what would I expect to see if I were wrong?", then go get that. If the symptom will not reproduce, there is no finding, however good the mechanism sounds. |
| **A probe's negative arm can crash on the very refusal it exists to confirm.** | `probe_mcp_identity.py` drove its keyless arm through `streamablehttp_client`. A refused request never becomes an MCP session — the middleware 401s before the handshake — so the client's TaskGroup unwinds and raises a **`BaseExceptionGroup`**, which `except Exception` does NOT catch (it derives from `BaseException`). The handler written to record the refusal never ran; the probe died on its first arm and the exit code was masked by a pipe to `tail`. | Measure a refusal where the refusal happens: assert the **HTTP status** of a bare request rather than routing it through a protocol client that assumes success. When catching around anyio/TaskGroup code, `except Exception` is not enough — expect `BaseExceptionGroup`. And never read an exit code through a pipe: `$?` is the LAST command's, so `probe \| tail` reports tail's success. |
| **A fixture that PINS a value makes the rule reading that value untestable.** | T0-14's acceptance rule had two halves, invariants and stability. The invariants half was fixed, 4 tests went RED then GREEN, done. The fixture set `stability_score=1.0` unconditionally -- so the stability half could not fail in any of those tests, and it was broken the same way. A constant in a fixture is a silent `assume`. | For every field the code under test READS, ask whether the fixture varies it. Derive it from the inputs instead (`stability_score = 1.0 - max(baseline_flip_rate, candidate_flip_rate)`) so it cannot disagree with what it models, and assert on it in the test that depends on it. |
| **A fail-open gate is an unknown quantity of SUPPRESSED findings, not just a broken check.** | The moment T0-14 stopped reading the wrong side, it reported that 4 of the 15 builtin motives violate `retrieval_allocation` (#143). That was true and invisible for as long as the gate was wrong. | Prioritise a fail-open gate by what it might be hiding, not by its own blast radius -- and when you fix one, expect new failures immediately and budget for triaging them rather than assuming your fix caused them. |
| **A name referenced by STRING is invisible to grep-based checks.** | The models split pruned `memotron.models.uuid4` after a three-way check found no importers. A test did `monkeypatch.setattr(models_module, "uuid4", ...)` -- the name is in a string, so no grep for `import uuid4` or `models.uuid4` could see it. Undetected until the Postgres lane ran for the first time. | Before pruning a re-export, also grep the bare NAME in quotes. And accept that the API golden is the real backstop. |
| **"Restoring" a pruned name can be worse than the error it fixes.** | The obvious fix above -- put `uuid4` back on the package -- makes `setattr` SUCCEED and intercept nothing, because after the split each submodule binds its own `uuid4`. Both engines would mint random uuids while the test still claimed to pin them. | The `AttributeError` was the honest failure. Patch where the name actually resolves; do not restore a re-export to silence a patcher. |
| **A summed metric can invent work that does not exist.** | I reported "StorageBackend: 148 methods -- a namespace, not a contract". It has ZERO methods of its own: it is `MemoryGraphStorage` (61) + `OperationalStorage` (87), two domain contracts, and `SplitStorageBackend` already routes between them. | Before treating a number as a target, check what it is a number OF. |
| **mypy hides the real error one line above the obvious one.** | `mypy: error: Missing target module, package, files, or command` looks like a broken `files` key. The actual cause is printed above it: a module in two `[[tool.mypy.overrides]]` blocks with different `disable_error_code` values. | Run bare `mypy` and read the FIRST line, not the last. |
| **A gate that reads an artifact can read a STALE one.** | `coverage_floors.py` reported PASS over a tree it had never measured. | It now fails STALE on mtime. Apply the same suspicion to any gate that reads a file rather than producing it. |
| **A permanently red check is one nobody reads.** | The first intended behaviour change put `sanity.sh` red forever. | Declare intended differences (`tests/sanity_expected_diff.txt`) so the gate keeps reporting real drift. |
| **A Shape A split silently takes the Shape B value types with it.** | The client split moved 115 public names -- `AddMemoryResult`, `SearchResults`, `CoherenceIncident`, `ErasureCertificate`, 111 more -- out of `memotron.client` and re-exported none of them. ruff clean, mypy clean, 1052 tests green. | Run `api_removals.sh` after EVERY extraction commit, not only when you suspect one. It is the only gate that sees this, and it takes two seconds. |
| **Re-export from where the name is DEFINED, not from where you found it.** | Re-exporting those 115 through the mixin each happened to be visible in produced 110 `no_implicit_reexport` errors: a strict-tier module cannot re-export what it merely imported. | Resolve `obj.__module__`, confirm that module defines it at top level (AST), and import from there. `X as X` is still required -- plain `import X` does not re-export under strict. |
| **A function-local import is not a module-level binding.** | A script scoping `TYPE_CHECKING` headers treated every `import X` in the AST as binding `X`, so it skipped names that only a *function-local* import provided. Annotations on other functions then named undefined symbols -- and `from __future__ import annotations` means nothing evaluates them, so only mypy saw it. | When asking "is this name bound at module scope?", walk `tree.body` plus the `TYPE_CHECKING` block, never `ast.walk`. |
| **`_mro_shadows` cannot see a guard that moved WHOLESALE.** | It reports a name defined by two sibling mixins. A scope guard relocated into one mixin, with the composer's copy gone, is exactly one definition -- ordinary inheritance from the MRO's point of view, and a silent authorisation bypass in practice. | Assert ownership explicitly. `api_surface.py:GUARD_MEMBERS` names the five and requires `Memotron` itself to define each. |
| **R-A2 needs a mechanical sweep, not an eye.** | `_checkpoint_operator_run` sat in `_runtime` while its twin `_begin_operator_run` sat on the composer; nine mixins reached sideways into a leaf for their audit checkpoint, 35 call sites. Every gate passed. | Walk `self.<attr>` per module against per-module definitions and flag anything a leaf owns that >= 2 siblings call. Ten lines of AST. |
| **`pure_move` is the wrong gate for a REFORMAT, and it fails loudly enough to look real.** | The repo-wide `ruff format` produced 28 "body changed while moving" reports. All 41 function definitions behind them had byte-identical ASTs. Cause: CPython lays out COMPREHENSION jumps by how the source is spread across lines, so collapsing a generator onto one line emits `POP_JUMP_IF_TRUE +1; JUMP_BACKWARD 15` where it emitted `POP_JUMP_IF_FALSE +18`. The outer function's `co_code` is identical every time; only nested comprehension objects differ. | Use `ast_identity.py` for format commits — parsed-tree comparison, position-insensitive. Match the gate's strength to the change: bytecode for a move, AST for a reformat, tests for anything that actually edits logic. |
| **Two files is not a sample.** | I ran the formatter on 2 of 243 files, saw `pure_move` PASS, and read it as clearance for the whole pass. Those two happened to contain no multi-line comprehension — the one construct that triggers the effect above. | Before generalising from a spot-check, ask what the sample could not have contained. Prefer a check that covers the whole set even when it is weaker. |
| **A number parked in a comment ages silently.** | `[tool.ruff.format]` recorded a "measured blast radius: 138 files, 7,434 lines" while the formatter sat deferred. When it finally ran: **243 files**. The comment had been wrong for ~150 commits and nothing could have noticed. | Date any measurement written into a comment, and re-measure before acting on one you did not take today. |
| **A suppression outlives the debt it was written for, and nothing says so.** | mypy's `warn_unused_ignores` covers inline `# type: ignore` and has **no equivalent** for `disable_error_code` in `[[tool.mypy.overrides]]`. First time anyone checked: **17 of 109 declared codes were already dead**, and three modules needed none at all. Each one is a category mypy silently ignores in a module someone is about to refactor — and mypy is the gate that caught the wrong-sibling rewrite nothing else did. | `scripts/verify/mypy_suppressions.py` strips them all, runs mypy once, and fails on any code that never fires. Same ratchet shape as `coverage_floors.py --ratchet`. |
| **THE REGISTER GOES STALE, AND IT IS THE INPUT TO EVERY PLAN.** | Three Explore agents verified 7 Tier-0 claims against current code on 2026-08-30. **Three were already fixed** — T0-4 and T0-5 by commit `29624cb`, an ancestor of the branch that was planning around them. A fourth (T0-3) was real but mis-caused and unreachable. Ten issues had been filed off that list two hours earlier; two were substantially wrong. | Treat every `INHERITED` entry as a hypothesis with an expiry, not a fact. Re-verify before planning around it, and correct the entry when you do. **Base rate measured: 3 stale in 7.** |
| **A PROBE CAN GO STALE AND START MISREPORTING — which is worse than not existing.** | `probe_kek.py` exits 2 with *"INCONCLUSIVE — the baseline arm failed; the probe itself is wrong"*, because the T0-2 fail-closed guard now raises before its try block. It reports the FIX as a harness bug. `probe_storage_wiring.py` and `scripts/verify/README.md` still assert gates fail that now pass. | When a probe reports something surprising, check the probe against current source before believing it. A probe that names its own failure mode ("the probe itself is wrong") is telling you which branch to read first. |
| **A key minted in one environment 401s in another, and it reads as a bad key.** | Minted on `latest.jedai-gateway-admin`, then called `DEFAULT_GATEWAY_BASE_URL` — which is **preview**. `Authentication Error, Invalid proxy server token`. Nothing in the message says "wrong environment". | The env you mint in and the env `LITELLM_API_BASE` points at are configured independently. Set both together, and make one real call before trusting resolution — `build_transports_from_env()` returning a live transport class proves the NAME resolved, not that the key works. |
| **`$?` after a pipe is the last command's status, not yours.** | `cmd \| tail -3; echo $?` reported success for a failed `git fetch`, twice, and again for a `helm` error. Each time it produced a confident wrong statement about whether something had worked. | Check the exit code on its own line with the pipe removed, or use `PIPESTATUS`. This one recurred three times in a single session. |
| **`set -a; . ./.env` executes the file as shell.** | A value with an unquoted space (`MEMOTRON_PROJECT_NAME=Memotron live lane`) becomes a command: `command not found: live`. A DSN with spaces silently parses as extra assignments instead. | Quote every value in `.env`. |
| **`grep sk-` false-positives on ordinary words.** | Auditing for a leaked key, `git grep -l "sk-"` hit three tracked files — all of them the substring in `ta`**`sk-run`**. | Search for the literal secret value, not its prefix, when you actually need to know whether it leaked. |
| **A determinism claim measured over two runs is a claim about two runs.** | `storage_profile.py` asserted "zero profiles differed across two runs". At 695 rows one row does drift -- a concurrency test, ~1 run in 5. | Say how many runs. If a gate is going to be trusted mid-refactor, measure its noise floor before trusting it, and forgive only the axis that is actually noisy (counts, not method identity). |
| **A control that runs a DIFFERENT COMMAND validates nothing.** | Searching user-facing files for erasure claims, `git grep -niE ... -- README.md 'docs/*.md' 'ui/admin/src/**/*.tsx' ...` returned ZERO. I ran a control in the same breath -- but wrote it as `git grep -ci "memory" -- README.md docs/ ui/admin/src/ ...`, a **different pathspec list**. The control passed (416 hits), so I reported "no overclaiming language exists in this repo" to Ryan. It was false: `'ui/admin/src/**/*.tsx'` matched no files, git grep printed nothing, and README had **27** `shred` hits including a `WS-12 machine-verifiable erasure certificates` capability row. A test written minutes later found them immediately. | **The control must be the SAME command with ONE thing varied** -- same pathspec, same flags, same filters, only the pattern changed to something that must match. If you retype the command for the control, you are testing a different command. Cheapest reliable form: run the real query, then re-run it verbatim with the pattern swapped for a known-present string. |
| **A CONTROL can return the right number for the wrong reason.** | Checking that `parity_coverage.py` refuses a hermetic coverage report, I passed `coverage.json` as a positional arg. argparse rejected it and **exited 2** -- exactly the "declined to judge" code I was testing for. The tool's own logic never ran. Separately, a control adding a second `config -> storage` import did not move `upward tier edges`, which looks like a broken gate: the metric counts distinct **(subsystem, subsystem)** pairs, not import sites. | After a control produces the expected result, ask **by what path**. An exit code produced before the tool's logic ran is not evidence about the tool. Design the control against what the metric *counts* -- read the implementation, not the name. Both directions matter: a false PASS hides a broken gate, a false FAIL condemns a working one. |
| **A fabricated input in a real container is still a fabricated input.** | I measured `/health` returning 421 for `Host: 10.154.184.212:8000` in a genuine Docker run and reported it as a deploy-breaking kubelet bug. That address never corresponded to the connection. FastMCP appends `scope["server"][0]` -- the accepted connection's own local address -- to its allowlist, and kubelet connects *to* the pod IP, so a real probe matches and gets 200. Running the artifact was necessary and not sufficient. | Before believing a dramatic negative, ask **what value the real caller puts in that field**, and construct the input from the actual connection rather than from a plausible-looking literal. For a Host-header test specifically: set the connection target and the `Host` separately, and check whether the framework treats the connection address as allowlisted. |
| **A hand-written mirror of an external matcher rots toward PASSING.** | `tests/test_mcp_transport_security.py::_allows` reimplemented the SDK's host check. FastMCP's `_normalize_host` strips the port from BOTH pattern and header, so bare `Host: localhost` matches `localhost:*` -- the mirror required the colon and reported **refused** where the real guard **accepts**. Every negative assertion built on it passed for the wrong reason, and the divergence appeared silently at a dependency bump. | Never re-implement the predicate under test. **Delegate to the real matcher** (`from fastmcp.server.http import _host_matches`) or drive the real app and read the status code. If a mirror is unavoidable, add a test that asserts mirror == real for a table of inputs including the awkward ones (bare host, host:port, IPv6, uppercase). |
| **A `--bless` from a narrower run DELETES rows, and only one of the two gates refuses.** | `receipt_golden.py --bless` off a hermetic run dropped 11 golden rows; only **1** was mine, the other 10 belonged to Postgres-only tests the run never executed. `storage_golden.py` refused the identical shrink and printed the fix. | Compute the delta from the FILES before and after every bless -- never from the report, which truncates -- and re-run the FULL suite (`MEMOTRON_ALLOW_EPHEMERAL_KEK=1 uv run pytest -q` with the DSN) before blessing anything, because `check.sh`'s tests lane deselects markers. |
| **Sourcing `.env` to get the DSN also repoints the tests at the real dogfood graph.** | `set -a; . ./.env; set +a` exports `MEMOTRON_GRAPH_PATH` too, so `test_concurrent_same_agent_registration_is_an_idempotent_reconnect` failed on `agent_name 'Claude Code' is already registered` -- state this session's own hooks had written. It passes in a clean env. | Export the one variable in a subshell: `export MEMOTRON_TEST_POSTGRES_DSN="$(set -a; . ./.env; set +a; printf '%s' "$MEMOTRON_TEST_POSTGRES_DSN")"`. The DSN is **keyword/value** (`host=... port=...`), not a URI, and `cut -d= -f2-` keeps the quotes -- psycopg reports that as `invalid connection option ""host"`. |

**And the rule that catches the ones not yet on this list: prove the guard fails.**
A guard nobody has seen go red is a guard nobody has tested. Flip one row of the
receipt golden by hand and confirm it reports `MUTATION FLAG FLIPPED`; delete one
declaration and confirm `sanity.sh` goes FAIL. Both were done; both took a minute;
one of them found that the match ran backwards.

## The gate — run it before recording findings

```bash
uv run python scripts/verify/finding_gate.py     # exit 0 = record is clean
```

A linter for the record itself, not the code. It fails on the shapes of mistakes actually
made here: claims with no way to re-verify them, hedges whose confidence drifts upward on
restatement, comparisons that changed two variables, and design questions answered by
inference during discovery.

It enforces:
- every backlog item cites **file:line, a command, or a measurement**
- **hedges carry** `ASSUMED` / `UNCONFIRMED`
- every comparison in the log **names what was held constant**
- every `Q-n` **stays a question** — no "therefore", "so the fix", "we should"
- every unknown has a `CLOSES WITH`; anything `CLOSED` cites evidence

**When it flags you, fix the record — not the gate.** Tune the gate only for false
positives (a legitimate evidence form it does not recognise), never to lower the bar. A gate
with false positives is one you learn to ignore, which is worse than no gate.

Its first run found three real problems, not formatting: a stale finding that later evidence
had contradicted, an item carried from a review and never independently reproduced, and 20
verified findings with no recorded source.

## Before reporting a finding

- [ ] Ran it, not just read it
- [ ] One variable changed
- [ ] Error type classified — validation vs code bug
- [ ] For a UI claim: identified what the string refers to
- [ ] For "it works": asserted a round trip, not a non-empty response
- [ ] Checked `TAKEOVER-BACKLOG.md` — is this already known?
- [ ] For a **BLOCKED-ON-CREDENTIALS** claim ("kubectl is refused, so this cannot be checked"):
      asked what the APPLICATION exposes before recording it as blocked. A deployed service
      that reports its own configuration is a verification surface, reachable on a different
      credential and network path than the control plane. *Cost 2026-09-08:* #185 concluded
      stage/load were unverifiable because every `kubectl` read is refused. `GET /api/overview`
      through the admin ingress reports `operator.store` and `graph_path` unauthenticated, so
      that row was measurable from outside the cluster the whole time — and the field that made
      it possible shipped in #187, one day before the conclusion was written down.
- [ ] For an ABSENCE ("nothing calls this", "this is never set", "no code path enables it"):
      grepped the TEST names for the thing about to be added, and read the docstring of the
      parameter about to be set. A deliberate omission usually has a guard beside it, and this
      repo writes the reason down. #137's "no shipped path enables the scope guard" is asserted
      by `test_platform_facade_does_not_set_core_guard` AND stated in the constructor docstring;
      wiring it anyway failed 35 tests.
- [ ] **Ran the gate CI runs, not the one you like.** `check.sh` is NOT the CI gate —
      `Build_Check` runs `scripts/ci-build-check.sh` (`INFRA-BACKLOG.md:281`), which differs in
      two ways that each hide failures: it has **no `MEMOTRON_TEST_POSTGRES_DSN`**, and it
      runs bare `pytest -q` with **no marker filter** where `check.sh` uses
      `-m "not postgres and not corpus"`. `check.sh` 16/16 is compatible with a red CI.
      *Cost 2026-09-07:* a test asserted `status in (200, 404)` for the admin static handler.
      The real set includes **503** when `ui/admin/dist` is absent
      (`admin_server/__init__.py:2150-2154`) — which is CI, and never a dev machine. Three PRs
      went red before anyone read the log. Reproduce with:
      `mv ui/admin/dist /tmp/x && env -u MEMOTRON_TEST_POSTGRES_DSN bash scripts/ci-build-check.sh`
- [ ] **A test that reads an ambient build artifact is environment-dependent, and will differ
      in CI.** `ui/admin/dist` exists locally and not in CI. Control it explicitly
      (`monkeypatch.setattr(MemoryGraphHandler, "static_dir", …)`) and pin BOTH states, rather
      than widening the assertion until it tolerates whatever the machine happens to have.
- [ ] For a BLAST-RADIUS claim ("this only affects X", "reads are unaffected", "no existing
      caller changes"): ENUMERATED the affected surface from source and stated the COUNT.
      A number you derived is a claim; a number you assumed is a guess wearing a claim's
      clothes. *Cost 2026-09-07:* an admin kill switch was described as costing only "the
      console's write features" in an issue, a commit message and a PR body. It refused all
      28 POST routes, and 15 of them -- everything under `/api/platform/` -- are the
      agent-memory HTTP API `README.md` documents, called by an agent at session startup. One
      `ast.walk` over `do_POST` split 28 into 15 + 13 along a prefix nobody had noticed.
      **No gate can catch this**: `check.sh` passed 16/16 and all 61 of its tests asserted the
      refusal the author intended. A suite verifies the thing you built; it cannot tell you
      the thing you built has the wrong scope.
- [ ] For a DESIGN question ("which field should we key on", "should we use X or Y"):
      grepped `docs/*decisions*.md` for the nouns in the question **before** measuring.
      `docs/authorization-decisions.md` holds DW-001..DW-027 and is not indexed anywhere
      obvious. A whole session went into re-deriving DW-014/DW-026 and reached a *worse*
      answer, because the recorded design used a field in a way the measurement could not
      see. A live measurement that contradicts a recorded decision is a finding about one
      of them — not a fresh start.
- [ ] For an assertion on an ERROR MESSAGE: stripped any path out of the message before
      matching. `pytest.raises(match="corrupt")` inside `test_a_corrupt_KEK_FILE_is_refused`
      passes **after the word is removed from the message**, because `tmp_path` is derived
      from the test's own name and `match` searches the whole string — it would pass against
      an empty message. Assert on `str(exc).replace(str(path), "<PATH>")`.
- [ ] For a fix that teaches ONE caller a new behaviour: grepped every call site of the same
      entry point and asked whether they now DISAGREE. Resolving the durable KEK inside
      `Memotron()` left `adoption.py` (×4) and `runtime.py` (×2) opening the same store
      without it, so the CLI sealed under one key and the server read under another — the
      very failure the fix existed to remove, recreated one seam over, with the whole suite
      green. If the behaviour is a property of the RESOURCE, resolve it where the resource is
      built, not where one caller happens to sit.
- [ ] Stated the condition under which the finding would be wrong
- [ ] If a NUMBER decided the outcome, did I choose that number for this decision?
      (A threshold invented minutes earlier is an opinion wearing a measurement's clothes.
      138-vs-150 killed a refactor that was worth doing; the 150 was mine.)
- [ ] For "why did it break NOW": checked the whole population, not the one instance in front of me
- [ ] Searched **closed** issues and read their *scope*, not just their titles
- [ ] For a test named after an ORDERING or INTERACTION: broke that exact thing in the
      source, verified the mutation APPLIED, and confirmed red. A test whose name claims
      more than it checks retires the question.
- [ ] **Read the mutation's failure REASON, not just its exit code.** A mutation that breaks
      compilation, imports, or SQL is **INVALID**, not CAUGHT — it goes red without ever
      reaching the assertion you are trying to validate, and that is indistinguishable from
      success if you only check the return code. Classify three outcomes, not two:
      CAUGHT (the assertion you care about failed, named in the output) / MISSED (still
      green) / INVALID (red for an unrelated reason — discard and redo).
      *Cost 2026-09-07:* deleting a scope predicate from three SQL reads reported CAUGHT ×3;
      the regex carried `count=0` and had stripped bind parameters elsewhere, so every
      failure was `psycopg.ProgrammingError: 4 placeholders but 3 parameters`. Redone
      per-site, removing predicate **and** its bind together so the statement stays valid
      and scoping is the only variable.
- [ ] Mutating one site with a **global** substitution (`replace_all`, `re.sub` with
      `count=0`, `sed -i s///g`) — assert the anchor matches exactly once first. One-site
      mutation is the whole point; a global edit changes the experiment.
- [ ] `finding_gate.py` passes

## Deploys: traps that look like code problems

**The namespace is `jedai-memotron`, not `memotron`** — and querying the wrong one returns
`No resources found in <ns> namespace`, which reads exactly like "nothing is deployed". That is a
FALSE FINDING wearing the costume of a fact: kubectl does not distinguish "this namespace is empty"
from "you asked the wrong question". Take the name from the chart
(`grep namespaceOverride .helm/values*.yaml`), never from the repo name, and treat any empty
kubectl result as unproven until you have confirmed the namespace exists
(`kubectl get ns | grep <name>`). Same shape as every other trap in this file: a check whose
passing condition is satisfied by the mistake it should catch.

**A chart flag can depend on a Secret that a DIFFERENT flag creates.** `keyManager` sources
`secretKeyRef: name: memotron-secrets`, which does not exist until `vaultSecret.enabled: true`
creates the VaultStaticSecret that syncs it. Enabling the first alone fails on a missing SECRET, not
a missing key — so a one-flag-at-a-time cutover, which exists to keep each failure to one cause,
silently produces the ambiguous failure it was designed to prevent. Before writing any cutover
order, list what is actually in the namespace (`kubectl get secret,vaultstaticsecret,vaultauth`) and
check every `secretKeyRef` in the rendered chart resolves to something that will exist AT THAT STEP.

**A deployment claim read from a chart is not a deployment fact.** `values-<env>.yaml` says what
Harness would apply, not what is running. Both are worth knowing and they are different claims —
say which one you measured. (Checking the live cluster is what caught the flag-ordering bug above;
reading the chart could not have.)

**A contract narrower than any of its implementations is not a contract.** `storage/base.py`
declared 5 parameters for `set_tenant_llm_credentials` while SQLite's had grown to 8 and Postgres
stayed at 5 — so the engines answered different signatures and nothing flagged it, because the
abstract described the smaller one. Widen the contract before widening an engine; and when two
engines diverge, read the abstract to see whether it describes either of them.

**A divergence on the write path usually has a sibling on the read path.** Fixing
`set_tenant_llm_credentials` on Postgres left `tenant_llm_credentials` still not returning the
fields — accepted, then silently discarded, which would have dropped a tenant into a different
vector space with nothing raised. Assert the ROUND TRIP, not that the call stopped raising. Same
rule as "a green response is not a working system", applied to engine parity.

**One transaction couples an OPTIONAL step to a REQUIRED one, and the optional one wins.** The
pgvector setup ran `ADD COLUMN embedding_vec vector` and `CREATE INDEX ... USING hnsw` together.
The index can never succeed (HNSW needs a fixed dimension; the column is dimensionless because
`dimensions` is stored per row) — so it raised, rolled the column back with it, and disabled the
whole pgvector path on every Postgres deployment. Ask of any multi-statement transaction: *is every
statement here load-bearing?* An optimization belongs in its own transaction, failing non-fatally.

**An error naming a subsystem you do not control sends someone to go check it.** `pgvector
unavailable` was emitted by a bare `except Exception` around our own invalid DDL, against an
instance where `vector 0.8.5` was installed and working. A colleague checked the instance and
correctly reported it fine — the message cost him the trip. An error must name **what this code
tried and what happened**, or say it cannot tell the difference. Same rule as the KEK/storage-fault
split: never let one catch report two causes as the more alarming one.

**A comment describing a build step is not the build step.** `pyproject.toml` said "the deployment
image installs `--extra otel`". `Dockerfile:16` installed `--extra postgres` only, for weeks, while
the chart set `OTEL_ENABLED=true` — so structured logging worked and nothing exported. When a
comment asserts what another file does, grep that file before believing it.

**A rendered-manifest grep cannot see `envFrom`.** `envFrom: secretRef` injects EVERY key of a
Secret as an env var of the same name, and the manifest never enumerates them. I checked
`grep -c MEMOTRON_KEK_B64` on the rendered chart, got 0 before and 0 after, and concluded the KEK
was inert — while the Vault payload key of that exact name was about to be delivered to every pod.
To know what environment a pod actually has, `kubectl exec ... -- printenv`, or better, run the
application's own resolver in the container. A manifest answers what the chart DECLARES; only the
running container answers what the process SEES.

**A squash-merge silently drops commits pushed after the PR was opened.** A correction committed
as `29b8828` never reached `main`: the PR was squash-merged from the state it had when reviewed. The
wrong text shipped, and nothing reported a problem — the PR said MERGED. **After any squash-merge,
diff `origin/main` against what you meant to land**, not against the PR's commit list.

**A chart-only deploy does not change the image tag.** Harness resolves image and chart
independently, so a values-only change redeploys the same `0.1.0-<sha7>`. Reading an unchanged tag
as "it did not deploy" is wrong; the reliable signal is `.status.startTime` on the pods (and new
ReplicaSet hashes). Conversely, pods rolling does NOT mean YOUR change deployed — check the merge
timestamp against the pod start time. A deploy triggered by the previous merge looked exactly like
mine until I compared the two clocks.

**A chart change on your branch deploys nothing.** Harness reads the chart from
`CHART_BRANCH`, which **defaults to `main`** (`platform-cicd`
`apps/memotron/pipelines/continuous.yaml`, with `APP_REPO: jedai/memotron` and
`CHART_DIR_NAME: .helm`). Pushing a branch changes no environment. Image and chart resolve
*independently*, so a chart-only fix can be re-driven against an already-built image by running the
pipeline with an explicit `IMAGE_TAG` — that skips Build entirely.

There is also **no chart lane in `check.sh`** — `grep -in 'helm\|chart' scripts/check.sh` returns
nothing. Nothing in 15 lanes renders the chart or asserts anything about it, so chart defects reach
production unopposed. Verify a chart change by rendering it yourself:
`helm template dw .helm -f .helm/values.yaml -f .helm/values-<env>.yaml`, for **every** env — the
per-env overlays can disagree.

**`FailedAttachVolume` appears on SUCCESSFUL deploys.** Since `strategy: Recreate` landed
(`bbc2973`), the admin replacement can still log `Multi-Attach error` when it starts before the old
pod's volume finishes detaching. It self-heals. Do not report it as a regression on the string
alone — read what follows within ~20 seconds:

* `SuccessfulAttachVolume` → self-healed, deploy proceeds
* `LoadBalancerNegTimeout` ~10 minutes later → the real deadlock (the #146 failure)

## When a test fails "deterministically", suspect the environment

Repeating a test measures **flakiness, not causation**. Three identical failures make you
confident and no better informed — and determinism is itself evidence for *stable ambient
state* rather than for a code defect.

Before blaming the code, vary these — one at a time:

1. **The working directory.** *(T1-14, **fixed** 2026-08-27 — kept because the symptom is the
   one worth recognising, and because the class of bug recurs.)* `runtime.py` used to call a bare
   `load_env_file()` whose default `".env"` was **cwd-relative**, so a `.env` in the directory you
   launched from was injected into `os.environ` and **overrode credentials a test explicitly
   deleted**. Symptom: the test is slow (seconds, network) when it should be instant (rule-based).
   The builders no longer self-load and `load_env_file` now requires an explicit path;
   `scripts/verify/probe_env_file_cwd.py` goes red if that regresses. **Still vary cwd first** —
   `graph_path` remains cwd-relative (T1-35), which produces a *different* silent divergence.
2. **Untracked local files.** `git status` clean means *tracked* state is clean. `.env`,
   `.memotron/`, `.memotron.yaml` are all untracked and all change behaviour.
3. **The checkout itself.** Run the same commit in a fresh `git worktree add --detach`. If it
   passes there, it is your directory, not the code.
4. **Compare trees, not commit messages.** `git rev-parse <a>^{tree}` vs `<b>^{tree}`. Equal
   tree hashes with opposite results ends the code hypothesis immediately.

This exact sequence retracted a filed "main is red" finding (D-44 -> D-45). The evidence for
it was real; every check varied something *inside* the repo, so all of them were blind.

## Common mistakes

- **Asserting on the call that REPORTS instead of the one that DOES.**
  `tenant_llm_credential_state` returns metadata and never unwraps the DEK, so a KEK probe
  built on it passed while the key was unusable. Drive `provision_governance_key` ->
  `_require_live_key`. Same shape as `memory_refresh` vs `memory_start`.
- **Trusting a backend flag without attributing a write.** Two MCP servers launched from one
  shell with the same DSN used different stores. Count rows in *each* store before and after.
- **Reading a sweep's verdict change as a defect.** Four methods "regressed" on Postgres; all
  four had been handed a MENTIONS edge by the harness and refused it correctly. Check what the
  harness fed the method before blaming the method.

| Mistake | Reality |
|---|---|
| "The tool returned results, so it works" | It returns pre-existing rows. Round-trip or it is unverified. |
| "Both servers differ, so it's the server" | Check whether the graph also differed. |
| "The UI says not configured, so it's broken" | Sometimes true, sometimes the honest MCP row. Read the label. |
| "26 errors means 26 defects" | 19 were correct rejections of bad arguments. |
| "I swept the SDK, so the app works" | The SDK is the clean layer. The defects are in the wrappers. |
| "I swept every surface, so I know the state" | Surfaces are not the product. Run the core loop or you have not tested what users depend on. |
| "This sweep line looks alarming" | Weigh by distance from the memory lifecycle. A dead admin route is not a dead product. |
| "No test covers it, so I'll trust the code" | Seven MCP tools failed on *every* input; any smoke test would have caught them. |
| "The fix is obviously right" | Reproduce red, then green, then replicate. Diffs do not prove behaviour. |
| "I'll write my own sweep script" | Four exist in `scripts/verify/`. Rewriting loses the baselines to compare against. |

## The code gates — added by the productionization branch (2026-08-27/28)

`finding_gate.py` below lints the RECORD. These lint the CODE, and none of them existed
when the rest of this skill was written. One command runs the lot:

```bash
bash scripts/check.sh        # 16 lanes; `--list` prints them, `check.sh <lane>` runs one
# lint format types suppressions scripts mixin-dag coupling tests receipts storage postgres cov-floor parity-cov diff-cov corpus image
#
# `image` (2026-09-09) is the ONLY lane that builds the artifact the cluster runs.
# Every other lane measures the source tree, and that gap let `main` sit unbuildable
# for three hours across two merges while check.sh reported 16/16 -- an inconsistent
# `package-lock.json` that `npm ci` refuses and `npm test` does not notice.
#
# NOTE the order: `postgres` now runs BEFORE cov-floor and diff-cov, because it contributes to
# the coverage report they read. Until 2026-08-31 it ran after them and contributed nothing.
```

Each lane answers a different question, and the distinctions matter when one goes red:

| gate | question | blind to |
|---|---|---|
| `pure_move.py <ref> .` | did any function body **change**? bytecode identity | class-level statements — pydantic field defaults are invisible to it |
| `api_surface.py --check` | did a public symbol **disappear**? | anything reached through `__getattr__` (the `memotron.graph` shim) |
| `api_removals.sh` | the FULL removal set | nothing — use this, not `--check`'s printed diff, which truncates |
| `ast_identity.py <ref> .` | did any module's **parsed tree** change? the gate for a format-only commit | comments and blank lines — deleting every comment in the repo passes it. Docstrings *are* compared. |
| `mixin_dag.py` | is the mixin call graph **acyclic**, layers as declared? | coupling *density* — it only measures direction |
| `receipt_golden.py` | did the suite **receipt** the same things, in the same order, with the same mutate/no-mutate flag? | code the suite does not execute |
| `storage_golden.py` | did each test drive the storage contract with the same **multiset** of calls? the guard for the deliberate body edits `pure_move` stops covering | ordering (blind on purpose), argument values, the Postgres backend, and one known-flaky concurrency row (`KNOWN_COUNT_DRIFT`) |
| `coverage_floors.py` | did any module lose coverage? | it reads a report — stale JSON fails STALE by design, **and since 2026-09-07 that means stale against `src/` OR `tests/`**. It watched `src/` only, so a TEST-ONLY edit slipped straight through: `mcp_auth` read 95.3% against its 96.3% floor, tests were added for the uncovered branches, and it still read 95.3% — a bare `pytest` collects no coverage at all. That reads as "the new tests did not help", which argues for lowering the floor |
| `parity_coverage.py` | **new lane `parity-cov` (2026-09-01)**: is each storage method exercised on BOTH engines? compares per-method covered-line sets across `storage/sqlite` and `storage/postgres`. Baseline `{sqlite_only: 20, postgres_only: 7}` of 172 shared methods | needs a coverage.json that saw both engines -- **SKIPS without `MEMOTRON_TEST_POSTGRES_DSN`**, because a hermetic report makes every Postgres method read as dark and it would print ~170 false gaps. A gap is NOT a defect: it says the suite does not look there. `cov-floor` and `diff-cov` structurally cannot see this -- code uncovered AND unedited is invisible to both, which is how the `anchor` half-life bug survived a 3,420-line parity suite |
| `coupling_report.py` | **now a GATE and a `check.sh` lane (2026-09-01)**: import cycles, upward tier edges, distinct-member breadth, private reaches — structure that is wrong regardless of how often it is used | the call-site counts it used to lead with (279 / 78 / 72) are still printed but are **informational and cannot fail a build** — a call count cannot tell heavy use of a right abstraction from a leak through a wrong one. No `--bless`: moving the baseline is a hand edit with a reason |
| `mypy_suppressions.py` | is every `disable_error_code` still earning its place? | inline `# type: ignore` (that is `warn_unused_ignores`, already on) |
| `skill_freshness.py` | is THIS skill still in sync and still naming the gates that exist? | a claim that is well-formed and false |
| `script_freshness.py` | do the SCRIPTS still describe the tree? every script imports, every path literal resolves, every `file.py:NNN` citation exists and is in range | whether a citation is *right* — `_graph.py:516` stays well-formed when line 516 moves. Cite a symbol as well as a line. |
| `api_since_cutover.py` | what left the PUBLIC API since before the effort? diffs against a baseline pinned to merge-base `57ae5d4`, **not blessable** | relocations are reported, not failed — and it cannot tell you a name that merely became *harder* to import |
| `doc_symbols.py` | do the DOCS name symbols that exist? every `from memotron… import X` in a fenced python block resolves, and every `Model(kwarg=…)` uses a field that model declares | whether the snippet is *correct* — it can construct cleanly and still teach the wrong thing |
| `live_lane.sh` | do the probes that make REAL calls still match their **recorded** outcomes? `tests/live_lane.baseline.tsv` records an EXPECTED EXIT per probe, not zero — `probe_kek` is *supposed* to exit 1 until #123 lands | it is not a `check.sh` lane and cannot be: it needs a gateway key and a live Postgres, and it REFUSES rather than skipping. Milestone-scoped. |
| `sanity.sh` | does the **product** build, install and run — and identically to `takeover`? | anything past a SQLite, no-LLM, no-control-plane smoke path |

### The two lanes that skip by default, and how to stop them skipping

`postgres` and `corpus` print SKIPPED and are never counted as passing. A green `check.sh` without
them says nothing about either. **Postgres is no longer hard to run** — measured 2026-08-30, first
real run in this worktree: **76 passed in 95s**.

```bash
docker compose -f docker-compose.local.yml up -d postgres     # postgres:16-alpine, host port 55432
export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=memotron"
bash scripts/check.sh postgres                                 # must PASS, not SKIP
```

**The `C` collation is not cosmetic.** The compose file passes
`--encoding=UTF8 --lc-collate=C --lc-ctype=C` because the state-hash SQL orders with
`COLLATE "C"`; a non-byte-ordered collation makes graph state hashes diverge from SQLite. If you
point the DSN at some other Postgres, check `datcollate` first:

```bash
docker exec <container> psql -U memotron -d memotron \
  -c "SELECT datcollate, pg_encoding_to_char(encoding) FROM pg_database WHERE datname='memotron';"
```

Parallel worktrees collide on 55432 — `DW_PG_PORT` exists for that, or reuse a running container
once you have checked its collation.

### The live lane — real gateway, real LLM

Everything above is hermetic or static. **None of it makes a real call**, and `sanity.sh` runs a
rule-based extractor in an offline venv. With no gateway key the system silently resolves
`RuleBasedExtractionTransport` and **no dream agent at all** — so the probes still run, still pass,
and are not testing what they claim.

```bash
set -a; . ./.env; set +a
uv run python -c "from memotron.runtime import build_transports_from_env as b; print([type(x).__name__ for x in b()])"
# live:    ['OpenAICompatibleExtractionTransport', 'OpenAICompatibleDreamAgentTransport']
# NOT live:['RuleBasedExtractionTransport', 'NoneType']
```

Minting a key for `latest`: the master key is `~/repos/jedai/mcp-forge/.local-secrets/litellm-latest-key`,
and `POST /key/generate` goes to the **`-admin`** host — the data-plane host answers *"Management
routes are disabled for this instance."* Give it a `key_alias` and a `duration`; there are already
104 keys on latest and an unlabelled one is untraceable.

****`sanity.sh` is the one to reach for when you doubt a refactor.** Every other gate measures
the source tree; none of them starts the product. It builds a wheel, installs it into a clean
venv, drives the SDK, counts MCP tools — then does the same from a `takeover`-built wheel and
diffs. The A/B half is what makes it useful: the dream pass reports `created=0` on *both*
wheels, so alone it reads as a broken formation path and in the pair it is the documented
no-worker behaviour.

## Measurement traps in this refactor — check these BEFORE reporting a number

Every one of these produced a wrong number that was acted on, or nearly was. They
are listed because the refactor keeps generating measurements and the failure is
always the same shape: **the number was real, and it was measuring the wrong thing.**

| trap | what it looks like | the check |
|---|---|---|
| **A baseline you did not MEASURE is a claim, not a baseline.** | Compared per-method coverage across the two storage engines, found 20 methods covered on SQLite and dark on Postgres, filed an issue on that number -- and set the reverse direction's baseline to `0` without ever running it. It is **7**, and it contains `set_tenant_llm_credentials` and `active_relationships`, so it was not the harmless direction either. The real figure is 27 of 172. This happened *inside* a gate whose entire subject is unchecked assumptions about the other engine. | If a comparison has two directions, run BOTH before blessing. Blessing from a measured run costs one command; a guessed zero silently under-reports until someone re-derives it. |
| **A gate that parses another tool's stdout is defeated by COLOUR, silently.** | `FORCE_COLOR=3` in the environment made mypy emit ANSI through a pipe. `mypy_suppressions.py` expects a literal `: error: ` and the escape codes land between the colon and the word, so 237 error lines parsed as ZERO, all 89 suppressions read as dead, and the lane demanded their deletion -- which would have unsuppressed 201 live errors. The repo had not changed; only the environment had. Fixed 2026-09-02 (`9c513bc`). | Force `NO_COLOR=1`/`TERM=dumb` in the subprocess AND strip ANSI before parsing -- a parser that matches nothing looks exactly like a clean run. And when a gate's verdict flips with NO corresponding repo change, suspect the environment first: `env | grep -iE 'FORCE_COLOR|CLICOLOR|TERM='`. |
| **`check.sh`'s summary table can report a lane that evaluated NOTHING as PASS.** | Both golden lanes decline to compare when the recorded profile came from a failing suite, and until 2026-09-02 they signalled that with `return 0` -- which `run_lane` maps to PASS. Two agents reported `storage PASS` off that table while the lane had skipped; the second time it masked a broken tests lane. Fixed in `501a787`: exit 3 now means *declined to judge* and prints `SKIPPED (nothing evaluated)`. | Do not take a lane's verdict from the summary table alone when it matters -- run that one script and read its last line. And when writing a gate, never let "evaluated nothing" share an exit code with "evaluated everything and it was fine". |
| **A ratio floor punishes DEDUPLICATING covered code.** | `cov-floor` failed on `memotron.embedding` (89.5% -> 88.6%) after the transport extraction. No coverage was lost: 26 COVERED statements moved to `gateway.py` where they still run (`coverage.parser`: 102 -> 76 statements), and the uncovered set was byte-identical -- the same eight validation branches, each verified absent from the diff. The numerator held; the denominator shrank. | When a ratio floor fails after a refactor, compare the DENOMINATOR before blaming the numerator: `coverage.parser.PythonParser(...).statements` on the old and new file. Writing tests for branches that were already dark, to restore a percentage, repairs the wrong thing. Re-derive the floor and record WHY it moved. |
| **Coverage records STATEMENT-START lines, not physical lines.** | A multi-line call's inner lines are in neither `executed_lines` nor `missing_lines`. Treating "not executed" as "not covered" reported **3%** where the truth was **100%** — and 3% would have cancelled a whole phase. | Resolve to the enclosing `ast.stmt` and test THAT line number. |
| **`grep -c` counts LINES; an AST walk counts NODES.** | `client.py` read 271 one way and 279 the other. The gap looked like a regression I had just caused. | Never compare two numbers produced by different methods. Re-measure both sides with one tool. |
| **A rendered diff can be truncated.** | `api_surface.py --check` prints a diff for a human and elides it when large: it showed 3 removals against an actual 22, and a commit message repeated the 3. | Compute the set. `scripts/verify/api_removals.sh` exists for exactly this. |
| **A measurement taken on an unverified tree state is not a measurement.** | Reported cross-object private reaches as 43 -> 10. The 10 came from an intermediate state where a delegate called ITSELF — self-recursion is not a cross-object reach, so the bug flattered the metric. It is 11. | Before recording a number, confirm the tree it came from passes. |
| **zsh treats `$VAR:s...` as a history modifier.** | `git show "$BASE:src/..."` silently mangles to `$BASE/src/...` and every result comes back 0. Twice read as "the files do not exist". | Brace it: `"${BASE}:src/..."`. |
| **zsh does NOT word-split an unquoted `$VAR`.** | `FLAGS="-f a.yaml --set x=1"; helm template . $FLAGS` passes the WHOLE string as one argument — helm reported `open  -f a.yaml --set x=1: no such file`, wrote an empty file, and the next `diff` against it printed a 630-line "difference" that was pure artifact. The bash habit is wrong here. | Inline the flags, use an array, or `${=FLAGS}`. And treat *every* line of a diff against a file you just generated as suspect until you check that file is non-empty. |
| **Piping a gate to `tail` REPLACES its exit code.** | `bash scripts/check.sh 2>&1 \| tail -22` reported **exit 0** while printing `check FAILED` — the status belonged to `tail`. The harness then announced the command "completed (exit code 0)". This is the same defect class the gates exist to catch: a passing condition satisfied by something other than what it claims to measure. | Redirect to a file and read `$?` on its own line: `bash scripts/check.sh > out.log 2>&1; echo "EXIT=$?"`. Never take a gate's verdict from a piped tail. |
| **A fix can make an existing test VACUOUS without making it fail.** | Bounding the admin control plane to one tenant changed *why* two security tests passed: their blocked `customer:` scope stopped being registered, so the refusal arrived as `"not registered"` instead of `"not authorized"`. Still closed — but the PRINCIPAL check those tests exist for stopped running, and editing the expected string would have left them passing on the wrong assertion forever. | When a change alters WHY something fails, ask whether the original check still runs at all. No gate reports this: the suite is green either way. Repair the precondition (here `extra_scopes`, so the scope is registered AND unauthorized), never the expectation. |
| **Handing someone a command with a placeholder you never resolved.** | Asked for "exactly what to run", produced three commands in a row that each failed on a value not present on the machine -- `<latest DSN>`, `<ALIAS>`, `<your-vault-host>`. They were run verbatim, because there was nothing to substitute. `VAULT_ADDR` was set nowhere: not in the environment, not in shell profiles, not in the workspace. | Before writing a command that needs a credential, spend ONE command checking the path exists: `env \| grep -i vault`, `ls .local-secrets/`, `grep -r VAULT_ADDR ~/.zshrc`. A placeholder handed to someone else is a QUESTION, not an instruction — resolve it, or say plainly it cannot be resolved from here. |
| **Verifying a refusal without a positive control.** | Confirming a scope guard closed a live leak needs THREE reads, not two: the neighbour refused, the other neighbour refused, and **the caller's own scope still returning 200**. Without the third, a guard that refuses EVERYTHING passes the first two and is a broken console. | Same rule as the positive controls in the tests, applied to production probes: an assertion written only in the negative cannot tell a fix from an outage. Always probe the thing that must still work. |
| **`grep -c` in a poll loop makes the success case unreachable.** | A 24-minute watch for a rollout reported "still listed" every attempt and timed out, while a manual probe in the same window showed the change already live. `n=$(... \| grep -c 'pat' \|\| echo 9)` — `grep -c` prints `0` **and exits 1** on no-match, so the `\|\| echo 9` fires on the SUCCESS case and `n` becomes `0\n9`, never equal to `"0"`. | Use `\|\| true`, or test the output instead of short-circuiting on exit status. More generally: **when two observations of one system disagree, suspect the instrument first** and prove it against a known input — `echo "no match" \| grep -c foo \|\| echo 9` settles it in one command. |
| **A `Forbidden` from the WRONG kube context, and a `can-i` against a namespace that does not exist.** | "kubectl is refused" stood for two weeks and blocked a workstream. It came from a `Forbidden` produced against the **stage** context (the shell default), then was reinforced by `kubectl auth can-i ... -n memotron` returning `yes` — for a namespace that does not exist. The real one is `jedai-memotron` (`namespaceOverride`); `preview` uses `preview-jedai-memotron`. On `latest`, `list pods` and `create pods/exec` are both permitted, and the api pods carry `MEMOTRON_OPERATIONAL_STORE_DSN` plus the `memotron` CLI. | Print `kubectl config current-context` and confirm the namespace EXISTS (`kubectl get ns`) in the same breath as any access verdict. `can-i` does not validate the namespace, so it will cheerfully answer about a fiction. An environment-scoped claim must carry the environment it was measured in. See `docs/environment-bring-up.md`. |
| **Sizing a branch with ancestry in a squash-merge repo — then writing it into STATE.md.** | `feat/next-ws25-ws28` was called "unmerged work gating the C-track" and sized twice (2,150 lines from a filtered docs grep; then 48k lines from `git log main..branch`). Both wrong: the work was ALREADY ON MAIN via PR #44, and STATE.md said so at line ~1590. Under squash merge, `git log main..branch` and `git diff main...branch` report a merged branch as unmerged forever, and the files that look "added by the branch" can be pre-split monoliths `main` now carries as packages. | Settle it with **content**: `git show origin/main:<file the branch adds>`. Two-dot `git diff main branch` also tells the truth where three-dot does not. And **before adding any fact to STATE.md, grep STATE.md for the subject** — a new claim that contradicts an existing entry is a signal to stop, not a newer truth. This one landed in "Do not re-derive", the section future sessions are told to believe. |
| **Blessing a golden mid-investigation, then editing on.** | Happened TWICE in one session: bless `api_surface.golden.txt`, keep changing source, watch the gate go red again. The second cost a full `check.sh` (~20 min) plus a re-run. | Bless from the FINAL tree, as the last step before committing — never mid-investigation. Any source edit after a bless makes it stale, and the gate is the only thing that will say so. |
| **`grep` answers a different question than the call graph.** | Scoping a guard, a textual scan for `_require_agent` reported 16 public methods as unguarded. A 20-line AST walk showed **23 of 29 reach it** — via `self.agent_scope(agent_id)`, which a string search cannot see. Acting on the grep would have scattered redundant checks and missed that the real gap was three lifecycle methods. | For "does X reach Y", walk `self.<attr>` calls transitively. A string match answers *does this method mention Y*, and the gap between those is invisible until someone counts. |
| **Mutating a security fix in one direction only.** | Every guard needs two mutations: revert it (does it still refuse?) and make it refuse UNCONDITIONALLY (does anything still work?). The second is the one that gets skipped, and it catches a "fix" that works by refusing everything — here that would have taken `dream_history` down on four environments while looking like a win. | Run both. And if `diff-cover` flags the guard's CALL SITE as uncovered, add a third: delete the call. Guard logic and guard wiring need separate tests — otherwise deleting `require_fleet_wide_write(...)` from the tool leaves every test green. |
| **git auto-merges two golden blessings, and the result can match NEITHER tree.** | Cherry-picking one re-blessed branch onto another merged `api_surface.golden.txt` with no conflict at all. Goldens are whole-tree artifacts, so a clean textual merge is not evidence the merged golden describes the merged code — and the gates would then pass against a fiction. | After ANY merge or cherry-pick touching a golden, re-bless on the merged tree and run the gate; never accept the auto-merge. Use the GATE, not a hand-rolled diff — the first check here was itself wrong (`grep -v '^$'` stripped blank lines from one side and reported a false mismatch on a trailing newline). |
| **A gate can be blind to the exact hazard it names.** | A chart test written for the `#`-between-continued-flags trap used `sh -n`. Mutation said **MISSED**: `helm lint`, `helm template`, `sh -n` and a text `in script` assertion ALL pass on the broken form — the YAML is valid, a comment IS valid syntax, and the swallowed flags are still in the *text* while absent from *argv*. | Mutation-test every gate before trusting it, and prefer EXECUTING the artifact to inspecting its text. Two follow-on traps in the same fix: dropping `exec` from the probe modelled a loud 127 the real script cannot produce (`exec` replaces the shell, so the true failure is silent), and `stdout.split()` DROPS empty arguments — use NUL delimiters when an empty argv entry is meaningful. |
| **A diagnostic endpoint reports its own misconfiguration in a field nobody reads.** | `/api/tenant-config` on latest carried `warnings: ["tenant id 'wdpr-demo' matches no tenant in this graph (found: jedai-platform) ... purge would target the wrong tenant"]` for months. The endpoint was fetched for #185 and only `operator.store` was read. | When reading a diagnostic endpoint, read the `warnings`/`errors` array FIRST, before the field you came for. A component that reports its own breakage is only as good as the field someone looks at. |
| **Blessing a golden from a FILTERED run silently deletes rows.** | `receipt_golden.py` treats missing rows as non-fatal (a filtered run legitimately observes fewer), so blessing from a hermetic-only run drops every Postgres row from the golden and nothing ever fails on their absence again. | Bless ONLY from one full single-process run reporting `0 not observed` — `pytest -m "not corpus"` with the DSN set, not the two-lane `check.sh` shape. Read the script's pass/fail branches before assuming which deltas are fatal. |
| **A green static gate is not evidence the code runs.** | A blanket string replace rewrote a delegate's own body into infinite recursion. **ruff passed. mypy passed.** Only executing it failed. | Run something. `bash scripts/verify/sanity.sh` builds, installs and A/Bs the wheel. |
| **`pure_move` reads function BODIES only.** | A name used in a DEFAULT ARGUMENT is invisible to it -- `TENANT_ACTIVE_PROMPT_PROFILE` was unbound and pure_move said PASS. ruff's F821 is the only guard covering that position. | Know which gate covers which position: pure_move = bodies, ruff = signatures and module scope, mypy = cross-class resolution. |
| **A `_*.py` glob MATCHES `__init__.py`.** | Emits `from pkg.__init__ import X` -- a hard circular ImportError that ruff and pure_move are both clean on. Hit in the models split, then AGAIN in a different tool two sessions later. | Exclude it explicitly in every tool that globs private modules. |
| **Reverting one file of a multi-file extraction leaves duplicates.** | `git checkout -- __init__.py` while the extracted mixin survives on disk defines the same methods TWICE. The duplicate resolves fine and the leftmost silently wins. | `pure_move`'s "1 definition before, 2 after" is the only thing that reports it. Revert the whole cut, never one side. |
| **A blanket `Class.` -> `Mixin.` rewrite can point at the wrong sibling.** | Two calls targeted a method that lives in a DIFFERENT mixin. The package imported fine; it would have been an AttributeError at call time. | mypy catches it (`"type[X]" has no attribute`). Import-testing does not. |
| **`--bless` on a golden is only as good as the run that fed it, and the bad run is SILENT.** | **This row said the OPPOSITE until 2026-09-01 and the advice was live for a day.** It read *"bless with the DSN UNSET; DSN set -> 52 not observed and bless correctly REFUSES"*, which was true only while the golden held 57 `[postgres]` rows that were pure `close:1` stubs from SKIPPED tests. Those stubs were deleted at source, and the arms inverted. Measured on the same tree, twice, after the deletion: **DSN SET, no `-m` -> 0 not observed** (the rich run, safe to bless); **check.sh's profile -> 5 not observed**, because its tests lane filters `-m "not postgres and not corpus"` and `restore_observations` DISCARDS the Postgres lane's observations. Blessing from check.sh's profile silently drops those five DSN-only rows and nothing fails -- the loss surfaces days later as a "5 new" HARD FAILURE with no traceable cause. Hit for real on 2026-09-01 by the person who wrote the correction. | Bless from ONE full `uv run pytest -q` with the DSN **SET** and **no `-m` selector** -- never from `check.sh`. **Confirm `0 not observed` before blessing**: that is the signal the input is the rich run. Then check the golden's own `git diff` and attribute every added row. `storage_golden.py`'s docstring is the authority; if it and this row ever disagree again, believe the docstring and fix this row. |
| **A suppression that FIRES is not thereby justified.** | `mypy_suppressions.py` used to ask only "does this code still fire?". `mcp_server`'s `attr-defined` fired 27 times, the gate said "still earning its place", and all 27 were real defects (#132) -- including `run_due_dreams`, which raised on every call. Measured A/B 2026-09-01 with two defects injected behind an existing suppression: old gate PASS exit 0, new gate `local_platform: attr-defined 13 -> 15` exit 1. | The gate now ratchets per `(module, code)` against `tests/mypy_suppressions.baseline.tsv`. A RISE fails; a FALL is reported and passes, so re-bless in the commit that paid the debt (`--bless`). It catches GROWTH, not a brand-new suppression's initial size -- that is only visible in the pyproject diff. |
| **A cluster observed AT REST cannot answer whether it deploys.** | T0-8 was refuted correctly -- api `2/2` in three clusters, mounts no volumes, only `admin` holds the PVC at `replicas: 1`, "correct RWO usage". All true, all steady-state. The first real deploy then deadlocked on that PVC: `RollingUpdate` starts the replacement before the incumbent releases a ReadWriteOnce volume. `Multi-Attach error`, every merge, filed as #146. | When refuting a deployment claim, name the lifecycle phase your evidence covers. `kubectl get` describes a system that is not changing. If the claim is about rollout, the evidence has to be a rollout -- or say the claim is untested. |
| **Before killing a long-lived process, diff the mtimes of what it loaded against its start time.** | A 5d15h-old process held the only good copy of a KEK; the on-disk file had been replaced four days after startup, so disk and RAM had diverged silently. SIGTERM destroyed the real key. The evidence was already on screen -- a `.kek` dated later than the process holding it open -- and went uncross-checked. | `ps -o lstart= -p PID` against `stat -f %Sm` on its inputs. Anything newer than the process is state you are about to lose. Snapshot first, and prefer `--backup`-style copies over a plain `cp` for anything with a WAL. |
| **A hung job and a slow job look identical from the outside; only one of them ends.** | Waited 21 minutes on a Postgres suite I had myself measured at 144s earlier the same session. Cause: Docker Desktop manually paused, visible in one `docker ps`. A timing anomaly noted earlier the same day (217s vs 443s, same lane) had been left unchased. | Know the baseline BEFORE starting a long job. At ~2x elapsed, stop waiting and check the output file's mtime plus the external dependency (container, DSN, network). Waiting longer is only correct if something is still moving. |
| **A GUARD TEST whose passing condition is met by the move that defeats the guard.** | #126 added a scope check inside `_scope`, plus a test asserting every tool either declares `scope_kind` or is a listed exemption. An independent reviewer added a tool with a **required** `scope_kind` that built its own `MemoryScope` and never called `_scope` — **all six guard tests passed.** Declaring a scope is not routing through the guard. The same file had already caught two neighbours of this shape (a tool taking `tenant_id` instead; three tools declaring a scope and going fleet-wide by omitting it), so the pattern recurred three times in one change. | For any choke-point guard, write the test against the **BODY**, not the signature or the schema: every subject must be shown to CALL the guard, and the guard's primitive (here `MemoryScope`) must be constructible nowhere else. Then prove it by adding the bypassing tool yourself and watching the test fail. A guard test that has never failed against a real bypass is decoration. |
| **Enumerating what a server exposes by PARSING ITS SOURCE.** | A hand-built list of MCP tools that skip the scope helper said **5**; `await mcp.list_tools()` said **6**. The one the parser lost was `clear_tenant_llm` — the tool that *deletes* a tenant's LLM credential. A decorator that fails to register, a rename, a tool defined in another module, or a signature the parser mis-reads are all invisible to grep and visible to the registry. | Enumerate from the running registry, never from the source text, and make the enumeration a test. `tests/test_mcp_tool_registration.py` and `tests/test_mcp_scope_guard.py` both exist because of this. |
| **Moving a synchronous storage call into `anyio.to_thread` breaks SQLite and is INVISIBLE on Postgres.** | The identity middleware ran its `key_principals` read in a worker thread alongside the blocking gateway call. `SQLiteStorageBackend` connects with the default `check_same_thread=True`, so every request against a SQLite store raised `ProgrammingError`; Postgres has a dedicated loop thread and survives, so `latest` and prod would have been fine and local dev would have 500'd on every call. Every test substituted a fake lookup, so no test covered the one line that matters in production. | Only put the genuinely blocking network call in the thread. Before moving ANY storage call off the loop, check the SQLite side — the engines' thread affinity differs, and the deployed engine is the forgiving one. If a collaborator is faked in every test, say so out loud: it is the line with the least coverage and the most consequence. |
| **A refusal message that quotes an upstream error can return CREDENTIALS.** | The middleware relayed `GatewayRequestError`, whose message is built as `"<description> failed with HTTP <code>: <body>"` — the gateway's body verbatim. Observed live: LiteLLM's 401 body carried `Received API Key = sk-...` **and the key's verification-table hash**, returned to an unauthenticated caller, where any 4xx-body-logging intermediary would capture it. | A refusal is the one response an attacker can always elicit, so it is the last place to relay someone else's error text. Carry two messages — a full one for the log, a fixed one for the caller — and assert the secret's absence from the response body in a test. Keep the actionable exception (an unbound alias needs its name and the bind command) and justify it. |
| **TWO runs at once against the same local Postgres and coverage files poison EACH OTHER.** | A `pytest` run reported **39 failed, 15 errors** on a tree whose only recent change was a bounded cache. A `check.sh` was running concurrently — and *its* Postgres lane failed in the same window, so both runs looked broken and neither was. Run alone: 1715 passed, and `check.sh` 16/16 twice. The failures name application code, not the DB, so nothing in the output points at the real cause. | Before believing a failure burst, check for a second run: `pgrep -fl "pytest\|check.sh"`. This suite shares one `memotron_db`, one `coverage.json` and one storage-profile recorder output, so concurrency is not slow — it is WRONG. Never start a second run "to save time" while one is going, and if a burst appears with no matching change, re-run solo before diagnosing anything. |
| **An EMPTY `kubectl` result is indistinguishable from a broken query — and it reads as a safety clearance.** | Verifying that nothing mounted a PVC before deleting it (irreversible, `reclaimPolicy: Delete`), three separate queries returned empty and each looked like "nothing mounts it". All three were broken: `perl -e 'alarm N; exec @ARGV' kubectl ...` had SIGALRM kill kubectl mid-flight, and zsh passed `$K get pods` as a single command name because **zsh does not word-split an unquoted variable** — "command not found" scrolled past while `grep -c` cheerfully counted `0`. Only a positive control caught it: asking the same query to print volume NAMES, which must be non-empty, showed the query itself returning nothing. | Before acting on an empty `kubectl` result — especially to authorise a deletion — run a control that MUST return rows and confirm it does. Inline the command; never build it in a shell variable. Use `--request-timeout=30s` rather than an external alarm wrapper, which kills the process and leaves stdout empty rather than erroring. Then confirm the destructive act by its own second signal: after deleting a claim whose PV reclaims `Delete`, the PV must go `NotFound` and the workloads must stay `Running`. |
| **A single failed observation + a plausible mechanism reads exactly like a confirmed finding.** | One 404 during a probe. I concluded MCP sessions were pod-local and unusable behind the ingress, and "confirmed" it twice: an A/B (worked pinned to a pod, failed through the ingress) and a config read (`sessionAffinity: None`, 2 replicas). Both were *consistent* with the hypothesis; **neither tested it**. The A/B changed two variables at once, since pinning also bypassed the ingress. The config described a mechanism that COULD produce the symptom, never that it did. I was one command from filing the issue. | **Reproduce the failure before diagnosing it.** Repeating the call succeeded 8/8 and the whole diagnosis collapsed — it was transient rollout convergence. The tell is that every piece of evidence was something you went looking for AFTER forming the hypothesis, and each was a thing the hypothesis PREDICTS rather than a thing that could have contradicted it. Ask "what would I expect to see if I were wrong?", then go get that. If the symptom will not reproduce, there is no finding, however good the mechanism sounds. |
| **A probe's negative arm can crash on the very refusal it exists to confirm.** | `probe_mcp_identity.py` drove its keyless arm through `streamablehttp_client`. A refused request never becomes an MCP session — the middleware 401s before the handshake — so the client's TaskGroup unwinds and raises a **`BaseExceptionGroup`**, which `except Exception` does NOT catch (it derives from `BaseException`). The handler written to record the refusal never ran; the probe died on its first arm and the exit code was masked by a pipe to `tail`. | Measure a refusal where the refusal happens: assert the **HTTP status** of a bare request rather than routing it through a protocol client that assumes success. When catching around anyio/TaskGroup code, `except Exception` is not enough — expect `BaseExceptionGroup`. And never read an exit code through a pipe: `$?` is the LAST command's, so `probe \| tail` reports tail's success. |
| **A fixture that PINS a value makes the rule reading that value untestable.** | T0-14's acceptance rule had two halves, invariants and stability. The invariants half was fixed, 4 tests went RED then GREEN, done. The fixture set `stability_score=1.0` unconditionally -- so the stability half could not fail in any of those tests, and it was broken the same way. A constant in a fixture is a silent `assume`. | For every field the code under test READS, ask whether the fixture varies it. Derive it from the inputs instead (`stability_score = 1.0 - max(baseline_flip_rate, candidate_flip_rate)`) so it cannot disagree with what it models, and assert on it in the test that depends on it. |
| **A fail-open gate is an unknown quantity of SUPPRESSED findings, not just a broken check.** | The moment T0-14 stopped reading the wrong side, it reported that 4 of the 15 builtin motives violate `retrieval_allocation` (#143). That was true and invisible for as long as the gate was wrong. | Prioritise a fail-open gate by what it might be hiding, not by its own blast radius -- and when you fix one, expect new failures immediately and budget for triaging them rather than assuming your fix caused them. |
| **A name referenced by STRING is invisible to grep-based checks.** | The models split pruned `memotron.models.uuid4` after a three-way check found no importers. A test did `monkeypatch.setattr(models_module, "uuid4", ...)` -- the name is in a string, so no grep for `import uuid4` or `models.uuid4` could see it. Undetected until the Postgres lane ran for the first time. | Before pruning a re-export, also grep the bare NAME in quotes. And accept that the API golden is the real backstop. |
| **"Restoring" a pruned name can be worse than the error it fixes.** | The obvious fix above -- put `uuid4` back on the package -- makes `setattr` SUCCEED and intercept nothing, because after the split each submodule binds its own `uuid4`. Both engines would mint random uuids while the test still claimed to pin them. | The `AttributeError` was the honest failure. Patch where the name actually resolves; do not restore a re-export to silence a patcher. |
| **A summed metric can invent work that does not exist.** | I reported "StorageBackend: 148 methods -- a namespace, not a contract". It has ZERO methods of its own: it is `MemoryGraphStorage` (61) + `OperationalStorage` (87), two domain contracts, and `SplitStorageBackend` already routes between them. | Before treating a number as a target, check what it is a number OF. |
| **mypy hides the real error one line above the obvious one.** | `mypy: error: Missing target module, package, files, or command` looks like a broken `files` key. The actual cause is printed above it: a module in two `[[tool.mypy.overrides]]` blocks with different `disable_error_code` values. | Run bare `mypy` and read the FIRST line, not the last. |
| **A gate that reads an artifact can read a STALE one.** | `coverage_floors.py` reported PASS over a tree it had never measured. | It now fails STALE on mtime. Apply the same suspicion to any gate that reads a file rather than producing it. |
| **A permanently red check is one nobody reads.** | The first intended behaviour change put `sanity.sh` red forever. | Declare intended differences (`tests/sanity_expected_diff.txt`) so the gate keeps reporting real drift. |
| **A Shape A split silently takes the Shape B value types with it.** | The client split moved 115 public names -- `AddMemoryResult`, `SearchResults`, `CoherenceIncident`, `ErasureCertificate`, 111 more -- out of `memotron.client` and re-exported none of them. ruff clean, mypy clean, 1052 tests green. | Run `api_removals.sh` after EVERY extraction commit, not only when you suspect one. It is the only gate that sees this, and it takes two seconds. |
| **Re-export from where the name is DEFINED, not from where you found it.** | Re-exporting those 115 through the mixin each happened to be visible in produced 110 `no_implicit_reexport` errors: a strict-tier module cannot re-export what it merely imported. | Resolve `obj.__module__`, confirm that module defines it at top level (AST), and import from there. `X as X` is still required -- plain `import X` does not re-export under strict. |
| **A function-local import is not a module-level binding.** | A script scoping `TYPE_CHECKING` headers treated every `import X` in the AST as binding `X`, so it skipped names that only a *function-local* import provided. Annotations on other functions then named undefined symbols -- and `from __future__ import annotations` means nothing evaluates them, so only mypy saw it. | When asking "is this name bound at module scope?", walk `tree.body` plus the `TYPE_CHECKING` block, never `ast.walk`. |
| **`_mro_shadows` cannot see a guard that moved WHOLESALE.** | It reports a name defined by two sibling mixins. A scope guard relocated into one mixin, with the composer's copy gone, is exactly one definition -- ordinary inheritance from the MRO's point of view, and a silent authorisation bypass in practice. | Assert ownership explicitly. `api_surface.py:GUARD_MEMBERS` names the five and requires `Memotron` itself to define each. |
| **R-A2 needs a mechanical sweep, not an eye.** | `_checkpoint_operator_run` sat in `_runtime` while its twin `_begin_operator_run` sat on the composer; nine mixins reached sideways into a leaf for their audit checkpoint, 35 call sites. Every gate passed. | Walk `self.<attr>` per module against per-module definitions and flag anything a leaf owns that >= 2 siblings call. Ten lines of AST. |
| **`pure_move` is the wrong gate for a REFORMAT, and it fails loudly enough to look real.** | The repo-wide `ruff format` produced 28 "body changed while moving" reports. All 41 function definitions behind them had byte-identical ASTs. Cause: CPython lays out COMPREHENSION jumps by how the source is spread across lines, so collapsing a generator onto one line emits `POP_JUMP_IF_TRUE +1; JUMP_BACKWARD 15` where it emitted `POP_JUMP_IF_FALSE +18`. The outer function's `co_code` is identical every time; only nested comprehension objects differ. | Use `ast_identity.py` for format commits — parsed-tree comparison, position-insensitive. Match the gate's strength to the change: bytecode for a move, AST for a reformat, tests for anything that actually edits logic. |
| **Two files is not a sample.** | I ran the formatter on 2 of 243 files, saw `pure_move` PASS, and read it as clearance for the whole pass. Those two happened to contain no multi-line comprehension — the one construct that triggers the effect above. | Before generalising from a spot-check, ask what the sample could not have contained. Prefer a check that covers the whole set even when it is weaker. |
| **A number parked in a comment ages silently.** | `[tool.ruff.format]` recorded a "measured blast radius: 138 files, 7,434 lines" while the formatter sat deferred. When it finally ran: **243 files**. The comment had been wrong for ~150 commits and nothing could have noticed. | Date any measurement written into a comment, and re-measure before acting on one you did not take today. |
| **A suppression outlives the debt it was written for, and nothing says so.** | mypy's `warn_unused_ignores` covers inline `# type: ignore` and has **no equivalent** for `disable_error_code` in `[[tool.mypy.overrides]]`. First time anyone checked: **17 of 109 declared codes were already dead**, and three modules needed none at all. Each one is a category mypy silently ignores in a module someone is about to refactor — and mypy is the gate that caught the wrong-sibling rewrite nothing else did. | `scripts/verify/mypy_suppressions.py` strips them all, runs mypy once, and fails on any code that never fires. Same ratchet shape as `coverage_floors.py --ratchet`. |
| **THE REGISTER GOES STALE, AND IT IS THE INPUT TO EVERY PLAN.** | Three Explore agents verified 7 Tier-0 claims against current code on 2026-08-30. **Three were already fixed** — T0-4 and T0-5 by commit `29624cb`, an ancestor of the branch that was planning around them. A fourth (T0-3) was real but mis-caused and unreachable. Ten issues had been filed off that list two hours earlier; two were substantially wrong. | Treat every `INHERITED` entry as a hypothesis with an expiry, not a fact. Re-verify before planning around it, and correct the entry when you do. **Base rate measured: 3 stale in 7.** |
| **A PROBE CAN GO STALE AND START MISREPORTING — which is worse than not existing.** | `probe_kek.py` exits 2 with *"INCONCLUSIVE — the baseline arm failed; the probe itself is wrong"*, because the T0-2 fail-closed guard now raises before its try block. It reports the FIX as a harness bug. `probe_storage_wiring.py` and `scripts/verify/README.md` still assert gates fail that now pass. | When a probe reports something surprising, check the probe against current source before believing it. A probe that names its own failure mode ("the probe itself is wrong") is telling you which branch to read first. |
| **A key minted in one environment 401s in another, and it reads as a bad key.** | Minted on `latest.jedai-gateway-admin`, then called `DEFAULT_GATEWAY_BASE_URL` — which is **preview**. `Authentication Error, Invalid proxy server token`. Nothing in the message says "wrong environment". | The env you mint in and the env `LITELLM_API_BASE` points at are configured independently. Set both together, and make one real call before trusting resolution — `build_transports_from_env()` returning a live transport class proves the NAME resolved, not that the key works. |
| **`$?` after a pipe is the last command's status, not yours.** | `cmd \| tail -3; echo $?` reported success for a failed `git fetch`, twice, and again for a `helm` error. Each time it produced a confident wrong statement about whether something had worked. | Check the exit code on its own line with the pipe removed, or use `PIPESTATUS`. This one recurred three times in a single session. |
| **`set -a; . ./.env` executes the file as shell.** | A value with an unquoted space (`MEMOTRON_PROJECT_NAME=Memotron live lane`) becomes a command: `command not found: live`. A DSN with spaces silently parses as extra assignments instead. | Quote every value in `.env`. |
| **`grep sk-` false-positives on ordinary words.** | Auditing for a leaked key, `git grep -l "sk-"` hit three tracked files — all of them the substring in `ta`**`sk-run`**. | Search for the literal secret value, not its prefix, when you actually need to know whether it leaked. |
| **A determinism claim measured over two runs is a claim about two runs.** | `storage_profile.py` asserted "zero profiles differed across two runs". At 695 rows one row does drift -- a concurrency test, ~1 run in 5. | Say how many runs. If a gate is going to be trusted mid-refactor, measure its noise floor before trusting it, and forgive only the axis that is actually noisy (counts, not method identity). |
| **A CONTROL can return the right number for the wrong reason.** | Checking that `parity_coverage.py` refuses a hermetic coverage report, I passed `coverage.json` as a positional arg. argparse rejected it and **exited 2** -- exactly the "declined to judge" code I was testing for. The tool's own logic never ran. Separately, a control adding a second `config -> storage` import did not move `upward tier edges`, which looks like a broken gate: the metric counts distinct **(subsystem, subsystem)** pairs, not import sites. | After a control produces the expected result, ask **by what path**. An exit code produced before the tool's logic ran is not evidence about the tool. Design the control against what the metric *counts* -- read the implementation, not the name. Both directions matter: a false PASS hides a broken gate, a false FAIL condemns a working one. |

**And the rule that catches the ones not yet on this list: prove the guard fails.**
A guard nobody has seen go red is a guard nobody has tested. Flip one row of the
receipt golden by hand and confirm it reports `MUTATION FLAG FLIPPED`; delete one
declaration and confirm `sanity.sh` goes FAIL. Both were done; both took a minute;
one of them found that the match ran backwards.

## The gate — run it before recording findings

```bash
uv run python scripts/verify/finding_gate.py     # exit 0 = record is clean
```

A linter for the record itself, not the code. It fails on the shapes of mistakes actually
made here: claims with no way to re-verify them, hedges whose confidence drifts upward on
restatement, comparisons that changed two variables, and design questions answered by
inference during discovery.

It enforces:
- every backlog item cites **file:line, a command, or a measurement**
- **hedges carry** `ASSUMED` / `UNCONFIRMED`
- every comparison in the log **names what was held constant**
- every `Q-n` **stays a question** — no "therefore", "so the fix", "we should"
- every unknown has a `CLOSES WITH`; anything `CLOSED` cites evidence

**When it flags you, fix the record — not the gate.** Tune the gate only for false
positives (a legitimate evidence form it does not recognise), never to lower the bar. A gate
with false positives is one you learn to ignore, which is worse than no gate.

Its first run found three real problems, not formatting: a stale finding that later evidence
had contradicted, an item carried from a review and never independently reproduced, and 20
verified findings with no recorded source.

## Before reporting a finding

- [ ] Ran it, not just read it
- [ ] One variable changed
- [ ] Error type classified — validation vs code bug
- [ ] For a UI claim: identified what the string refers to
- [ ] For "it works": asserted a round trip, not a non-empty response
- [ ] Checked `TAKEOVER-BACKLOG.md` — is this already known?
- [ ] For a **BLOCKED-ON-CREDENTIALS** claim ("kubectl is refused, so this cannot be checked"):
      asked what the APPLICATION exposes before recording it as blocked. A deployed service
      that reports its own configuration is a verification surface, reachable on a different
      credential and network path than the control plane. *Cost 2026-09-08:* #185 concluded
      stage/load were unverifiable because every `kubectl` read is refused. `GET /api/overview`
      through the admin ingress reports `operator.store` and `graph_path` unauthenticated, so
      that row was measurable from outside the cluster the whole time — and the field that made
      it possible shipped in #187, one day before the conclusion was written down.
- [ ] For an ABSENCE ("nothing calls this", "this is never set", "no code path enables it"):
      grepped the TEST names for the thing about to be added, and read the docstring of the
      parameter about to be set. A deliberate omission usually has a guard beside it, and this
      repo writes the reason down. #137's "no shipped path enables the scope guard" is asserted
      by `test_platform_facade_does_not_set_core_guard` AND stated in the constructor docstring;
      wiring it anyway failed 35 tests.
- [ ] **Ran the gate CI runs, not the one you like.** `check.sh` is NOT the CI gate —
      `Build_Check` runs `scripts/ci-build-check.sh` (`INFRA-BACKLOG.md:281`), which differs in
      two ways that each hide failures: it has **no `MEMOTRON_TEST_POSTGRES_DSN`**, and it
      runs bare `pytest -q` with **no marker filter** where `check.sh` uses
      `-m "not postgres and not corpus"`. `check.sh` 16/16 is compatible with a red CI.
      *Cost 2026-09-07:* a test asserted `status in (200, 404)` for the admin static handler.
      The real set includes **503** when `ui/admin/dist` is absent
      (`admin_server/__init__.py:2150-2154`) — which is CI, and never a dev machine. Three PRs
      went red before anyone read the log. Reproduce with:
      `mv ui/admin/dist /tmp/x && env -u MEMOTRON_TEST_POSTGRES_DSN bash scripts/ci-build-check.sh`
- [ ] **A test that reads an ambient build artifact is environment-dependent, and will differ
      in CI.** `ui/admin/dist` exists locally and not in CI. Control it explicitly
      (`monkeypatch.setattr(MemoryGraphHandler, "static_dir", …)`) and pin BOTH states, rather
      than widening the assertion until it tolerates whatever the machine happens to have.
- [ ] For a BLAST-RADIUS claim ("this only affects X", "reads are unaffected", "no existing
      caller changes"): ENUMERATED the affected surface from source and stated the COUNT.
      A number you derived is a claim; a number you assumed is a guess wearing a claim's
      clothes. *Cost 2026-09-07:* an admin kill switch was described as costing only "the
      console's write features" in an issue, a commit message and a PR body. It refused all
      28 POST routes, and 15 of them -- everything under `/api/platform/` -- are the
      agent-memory HTTP API `README.md` documents, called by an agent at session startup. One
      `ast.walk` over `do_POST` split 28 into 15 + 13 along a prefix nobody had noticed.
      **No gate can catch this**: `check.sh` passed 16/16 and all 61 of its tests asserted the
      refusal the author intended. A suite verifies the thing you built; it cannot tell you
      the thing you built has the wrong scope.
- [ ] For a DESIGN question ("which field should we key on", "should we use X or Y"):
      grepped `docs/*decisions*.md` for the nouns in the question **before** measuring.
      `docs/authorization-decisions.md` holds DW-001..DW-027 and is not indexed anywhere
      obvious. A whole session went into re-deriving DW-014/DW-026 and reached a *worse*
      answer, because the recorded design used a field in a way the measurement could not
      see. A live measurement that contradicts a recorded decision is a finding about one
      of them — not a fresh start.
- [ ] For an assertion on an ERROR MESSAGE: stripped any path out of the message before
      matching. `pytest.raises(match="corrupt")` inside `test_a_corrupt_KEK_FILE_is_refused`
      passes **after the word is removed from the message**, because `tmp_path` is derived
      from the test's own name and `match` searches the whole string — it would pass against
      an empty message. Assert on `str(exc).replace(str(path), "<PATH>")`.
- [ ] For a fix that teaches ONE caller a new behaviour: grepped every call site of the same
      entry point and asked whether they now DISAGREE. Resolving the durable KEK inside
      `Memotron()` left `adoption.py` (×4) and `runtime.py` (×2) opening the same store
      without it, so the CLI sealed under one key and the server read under another — the
      very failure the fix existed to remove, recreated one seam over, with the whole suite
      green. If the behaviour is a property of the RESOURCE, resolve it where the resource is
      built, not where one caller happens to sit.
- [ ] Stated the condition under which the finding would be wrong
- [ ] If a NUMBER decided the outcome, did I choose that number for this decision?
      (A threshold invented minutes earlier is an opinion wearing a measurement's clothes.
      138-vs-150 killed a refactor that was worth doing; the 150 was mine.)
- [ ] For "why did it break NOW": checked the whole population, not the one instance in front of me
- [ ] Searched **closed** issues and read their *scope*, not just their titles
- [ ] For a test named after an ORDERING or INTERACTION: broke that exact thing in the
      source, verified the mutation APPLIED, and confirmed red. A test whose name claims
      more than it checks retires the question.
- [ ] **Read the mutation's failure REASON, not just its exit code.** A mutation that breaks
      compilation, imports, or SQL is **INVALID**, not CAUGHT — it goes red without ever
      reaching the assertion you are trying to validate, and that is indistinguishable from
      success if you only check the return code. Classify three outcomes, not two:
      CAUGHT (the assertion you care about failed, named in the output) / MISSED (still
      green) / INVALID (red for an unrelated reason — discard and redo).
      *Cost 2026-09-07:* deleting a scope predicate from three SQL reads reported CAUGHT ×3;
      the regex carried `count=0` and had stripped bind parameters elsewhere, so every
      failure was `psycopg.ProgrammingError: 4 placeholders but 3 parameters`. Redone
      per-site, removing predicate **and** its bind together so the statement stays valid
      and scoping is the only variable.
- [ ] Mutating one site with a **global** substitution (`replace_all`, `re.sub` with
      `count=0`, `sed -i s///g`) — assert the anchor matches exactly once first. One-site
      mutation is the whole point; a global edit changes the experiment.
- [ ] `finding_gate.py` passes

## Deploys: traps that look like code problems

**The namespace is `jedai-memotron`, not `memotron`** — and querying the wrong one returns
`No resources found in <ns> namespace`, which reads exactly like "nothing is deployed". That is a
FALSE FINDING wearing the costume of a fact: kubectl does not distinguish "this namespace is empty"
from "you asked the wrong question". Take the name from the chart
(`grep namespaceOverride .helm/values*.yaml`), never from the repo name, and treat any empty
kubectl result as unproven until you have confirmed the namespace exists
(`kubectl get ns | grep <name>`). Same shape as every other trap in this file: a check whose
passing condition is satisfied by the mistake it should catch.

**A chart flag can depend on a Secret that a DIFFERENT flag creates.** `keyManager` sources
`secretKeyRef: name: memotron-secrets`, which does not exist until `vaultSecret.enabled: true`
creates the VaultStaticSecret that syncs it. Enabling the first alone fails on a missing SECRET, not
a missing key — so a one-flag-at-a-time cutover, which exists to keep each failure to one cause,
silently produces the ambiguous failure it was designed to prevent. Before writing any cutover
order, list what is actually in the namespace (`kubectl get secret,vaultstaticsecret,vaultauth`) and
check every `secretKeyRef` in the rendered chart resolves to something that will exist AT THAT STEP.

**A deployment claim read from a chart is not a deployment fact.** `values-<env>.yaml` says what
Harness would apply, not what is running. Both are worth knowing and they are different claims —
say which one you measured. (Checking the live cluster is what caught the flag-ordering bug above;
reading the chart could not have.)

**A contract narrower than any of its implementations is not a contract.** `storage/base.py`
declared 5 parameters for `set_tenant_llm_credentials` while SQLite's had grown to 8 and Postgres
stayed at 5 — so the engines answered different signatures and nothing flagged it, because the
abstract described the smaller one. Widen the contract before widening an engine; and when two
engines diverge, read the abstract to see whether it describes either of them.

**A divergence on the write path usually has a sibling on the read path.** Fixing
`set_tenant_llm_credentials` on Postgres left `tenant_llm_credentials` still not returning the
fields — accepted, then silently discarded, which would have dropped a tenant into a different
vector space with nothing raised. Assert the ROUND TRIP, not that the call stopped raising. Same
rule as "a green response is not a working system", applied to engine parity.

**One transaction couples an OPTIONAL step to a REQUIRED one, and the optional one wins.** The
pgvector setup ran `ADD COLUMN embedding_vec vector` and `CREATE INDEX ... USING hnsw` together.
The index can never succeed (HNSW needs a fixed dimension; the column is dimensionless because
`dimensions` is stored per row) — so it raised, rolled the column back with it, and disabled the
whole pgvector path on every Postgres deployment. Ask of any multi-statement transaction: *is every
statement here load-bearing?* An optimization belongs in its own transaction, failing non-fatally.

**An error naming a subsystem you do not control sends someone to go check it.** `pgvector
unavailable` was emitted by a bare `except Exception` around our own invalid DDL, against an
instance where `vector 0.8.5` was installed and working. A colleague checked the instance and
correctly reported it fine — the message cost him the trip. An error must name **what this code
tried and what happened**, or say it cannot tell the difference. Same rule as the KEK/storage-fault
split: never let one catch report two causes as the more alarming one.

**A comment describing a build step is not the build step.** `pyproject.toml` said "the deployment
image installs `--extra otel`". `Dockerfile:16` installed `--extra postgres` only, for weeks, while
the chart set `OTEL_ENABLED=true` — so structured logging worked and nothing exported. When a
comment asserts what another file does, grep that file before believing it.

**A rendered-manifest grep cannot see `envFrom`.** `envFrom: secretRef` injects EVERY key of a
Secret as an env var of the same name, and the manifest never enumerates them. I checked
`grep -c MEMOTRON_KEK_B64` on the rendered chart, got 0 before and 0 after, and concluded the KEK
was inert — while the Vault payload key of that exact name was about to be delivered to every pod.
To know what environment a pod actually has, `kubectl exec ... -- printenv`, or better, run the
application's own resolver in the container. A manifest answers what the chart DECLARES; only the
running container answers what the process SEES.

**A squash-merge silently drops commits pushed after the PR was opened.** A correction committed
as `29b8828` never reached `main`: the PR was squash-merged from the state it had when reviewed. The
wrong text shipped, and nothing reported a problem — the PR said MERGED. **After any squash-merge,
diff `origin/main` against what you meant to land**, not against the PR's commit list.

**A chart-only deploy does not change the image tag.** Harness resolves image and chart
independently, so a values-only change redeploys the same `0.1.0-<sha7>`. Reading an unchanged tag
as "it did not deploy" is wrong; the reliable signal is `.status.startTime` on the pods (and new
ReplicaSet hashes). Conversely, pods rolling does NOT mean YOUR change deployed — check the merge
timestamp against the pod start time. A deploy triggered by the previous merge looked exactly like
mine until I compared the two clocks.

**A chart change on your branch deploys nothing.** Harness reads the chart from
`CHART_BRANCH`, which **defaults to `main`** (`platform-cicd`
`apps/memotron/pipelines/continuous.yaml`, with `APP_REPO: jedai/memotron` and
`CHART_DIR_NAME: .helm`). Pushing a branch changes no environment. Image and chart resolve
*independently*, so a chart-only fix can be re-driven against an already-built image by running the
pipeline with an explicit `IMAGE_TAG` — that skips Build entirely.

There is also **no chart lane in `check.sh`** — `grep -in 'helm\|chart' scripts/check.sh` returns
nothing. Nothing in 15 lanes renders the chart or asserts anything about it, so chart defects reach
production unopposed. Verify a chart change by rendering it yourself:
`helm template dw .helm -f .helm/values.yaml -f .helm/values-<env>.yaml`, for **every** env — the
per-env overlays can disagree.

**`FailedAttachVolume` appears on SUCCESSFUL deploys.** Since `strategy: Recreate` landed
(`bbc2973`), the admin replacement can still log `Multi-Attach error` when it starts before the old
pod's volume finishes detaching. It self-heals. Do not report it as a regression on the string
alone — read what follows within ~20 seconds:

* `SuccessfulAttachVolume` → self-healed, deploy proceeds
* `LoadBalancerNegTimeout` ~10 minutes later → the real deadlock (the #146 failure)

## When a test fails "deterministically", suspect the environment

Repeating a test measures **flakiness, not causation**. Three identical failures make you
confident and no better informed — and determinism is itself evidence for *stable ambient
state* rather than for a code defect.

Before blaming the code, vary these — one at a time:

1. **The working directory.** *(T1-14, **fixed** 2026-08-27 — kept because the symptom is the
   one worth recognising, and because the class of bug recurs.)* `runtime.py` used to call a bare
   `load_env_file()` whose default `".env"` was **cwd-relative**, so a `.env` in the directory you
   launched from was injected into `os.environ` and **overrode credentials a test explicitly
   deleted**. Symptom: the test is slow (seconds, network) when it should be instant (rule-based).
   The builders no longer self-load and `load_env_file` now requires an explicit path;
   `scripts/verify/probe_env_file_cwd.py` goes red if that regresses. **Still vary cwd first** —
   `graph_path` remains cwd-relative (T1-35), which produces a *different* silent divergence.
2. **Untracked local files.** `git status` clean means *tracked* state is clean. `.env`,
   `.memotron/`, `.memotron.yaml` are all untracked and all change behaviour.
3. **The checkout itself.** Run the same commit in a fresh `git worktree add --detach`. If it
   passes there, it is your directory, not the code.
4. **Compare trees, not commit messages.** `git rev-parse <a>^{tree}` vs `<b>^{tree}`. Equal
   tree hashes with opposite results ends the code hypothesis immediately.

This exact sequence retracted a filed "main is red" finding (D-44 -> D-45). The evidence for
it was real; every check varied something *inside* the repo, so all of them were blind.

## Common mistakes

- **Asserting on the call that REPORTS instead of the one that DOES.**
  `tenant_llm_credential_state` returns metadata and never unwraps the DEK, so a KEK probe
  built on it passed while the key was unusable. Drive `provision_governance_key` ->
  `_require_live_key`. Same shape as `memory_refresh` vs `memory_start`.
- **Trusting a backend flag without attributing a write.** Two MCP servers launched from one
  shell with the same DSN used different stores. Count rows in *each* store before and after.
- **Reading a sweep's verdict change as a defect.** Four methods "regressed" on Postgres; all
  four had been handed a MENTIONS edge by the harness and refused it correctly. Check what the
  harness fed the method before blaming the method.

| Mistake | Reality |
|---|---|
| "The tool returned results, so it works" | It returns pre-existing rows. Round-trip or it is unverified. |
| "Both servers differ, so it's the server" | Check whether the graph also differed. |
| "The UI says not configured, so it's broken" | Sometimes true, sometimes the honest MCP row. Read the label. |
| "26 errors means 26 defects" | 19 were correct rejections of bad arguments. |
| "I swept the SDK, so the app works" | The SDK is the clean layer. The defects are in the wrappers. |
| "I swept every surface, so I know the state" | Surfaces are not the product. Run the core loop or you have not tested what users depend on. |
| "This sweep line looks alarming" | Weigh by distance from the memory lifecycle. A dead admin route is not a dead product. |
| "No test covers it, so I'll trust the code" | Seven MCP tools failed on *every* input; any smoke test would have caught them. |
| "The fix is obviously right" | Reproduce red, then green, then replicate. Diffs do not prove behaviour. |
| "I'll write my own sweep script" | Four exist in `scripts/verify/`. Rewriting loses the baselines to compare against. |
