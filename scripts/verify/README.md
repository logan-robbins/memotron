# Verification harnesses

Sweep every Memotron surface and classify `OK / EMPTY / ERROR / SKIP`.
**Always run against a disposable graph copy** — destructive tools are included by design.

```bash
cp .memotron/dogfood.sqlite     /tmp/x.sqlite
cp .memotron/dogfood.sqlite.kek /tmp/x.sqlite.kek     # the .kek MUST travel with the graph
export MEMOTRON_GRAPH_PATH=/tmp/x.sqlite
export MEMOTRON_PROJECT_ID=memotron-dogfood MEMOTRON_MODE=simple
set -a; . ./.env; set +a                                  # LITELLM_API_KEY / _API_BASE
```

| harness | what it sweeps | run |
|---|---|---|
| `sweep_mcp.py` | both MCP servers, every tool — **carries a control** | `uv run python scripts/verify/sweep_mcp.py <gov_url> <agentmem_url>` |
| `probe_mcp_identity.py` | cross-tenant refusal, 7 arms — **A1/A2 gate the rest** | `uv run python scripts/verify/probe_mcp_identity.py --url <url> --alpha-key K1 --bravo-key K2` |
| `sweep_sdk.py` | `Memotron` + `AgentMemoryPlatform`, every public method | `uv run python scripts/verify/sweep_sdk.py` |
| `sweep_admin.py` | all admin GET + POST routes | `uv run python scripts/verify/sweep_admin.py http://127.0.0.1:8030` |
| `../../ui/admin/tests/ui-sweep.spec.ts` | all 9 UI tabs | `ADMIN_BASE_URL=… npx playwright test tests/ui-sweep.spec.ts` |
| `loop.sh` + `probe_formation.py` | the formation round trip (red/green) | `scripts/verify/loop.sh scripts/verify/probe_formation.py` |
| `replicate.sh` | run any probe N times, tally | `scripts/verify/replicate.sh 5 "label" ENV=val` |
| `probe_entity_collision.py` | do two distinct entities sharing a proper name fuse? (two controls) | `uv run python scripts/verify/probe_entity_collision.py` |
| `pure_move.py` | **P3** — a refactor commit moved code without changing it | `uv run python scripts/verify/pure_move.py HEAD` |
| `api_surface.py` | **P1** — nothing disappeared from the importable surface | `uv run python scripts/verify/api_surface.py --check` |

## The two MCP probes speak the SESSIONLESS handshake (#257)

Both were rewritten for FastMCP 4. What changed, and why it is not cosmetic:

* **`fastmcp.Client`, not `streamablehttp_client` + `ClientSession`.** mcp 2.x renamed that
  factory *and dropped its `headers=` argument* — which is how `probe_mcp_identity.py`
  authenticates, so a rename alone would not have worked. It uses
  `StreamableHttpTransport(url, headers=...)` now.
* **`raise_on_error=False` is load-bearing** in both. fastmcp's client raises on a tool error
  by default; these probes must RECEIVE errors to classify or assert them. Letting it raise
  turns every expected refusal into a crashed probe.
* **`.input_schema`, not `.inputSchema`.** Three names for one field: the client-side protocol
  `Tool` uses `.input_schema` (`.inputSchema` still resolves but warns), while the server-side
  `FunctionTool` uses `.parameters`, which is what `tests/` reads. Picking wrong here is an
  `AttributeError`, which is the only reason it was caught — neither script is in a test suite.
* **`.is_error`, read as an attribute.** It was `isError`. `sweep_mcp.py` had
  `getattr(res, "isError", False)`, which after the rename would have returned `False` for
  every result and classified every ERROR as OK — the sweep would have gone green *because*
  it was broken.
* **No `mcp-session-id`.** The servers run `stateless_http=True` (#246), so none is issued.
  `probe_gateway_mcp.py` used to treat its absence as `probe_error`, i.e. it reported that
  issue's own fix as a fault. It is optional now, and still forwarded when present.

### Every run carries a control

The rule these encode: **a probe that cannot produce a success cannot interpret a failure.**
It exists because a bare `tools/list` once returned 0 tools for *every* server and was nearly
filed as a Memotron defect.

* `sweep_mcp.py` — connects and requires at least one tool enumerated **before** sweeping.
  Fails that and it prints `SWEEP INVALID` and exits **2**, never "every tool is broken".
  It also flags a sweep where every tool ERRORED and none succeeded as *one* server-level
  failure rather than N tool defects.
* `probe_mcp_identity.py` — A1/A2 (each key writing its OWN scope) are a **gate**, not a row.
  If a key cannot write its own scope, every refusal the probe would go on to assert is
  indistinguishable from a broken server, so it stops at exit **2**. P0 already aborted when
  `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is unset; A1/A2 close the other half.

### Both are HARD PRECONDITIONS of the live lane

`scripts/verify/live_lane.sh` now REFUSES to run without a deployed target and two bound
keys. They were opt-in for exactly one commit and that was wrong: a probe recorded at
"expected exit 2 = not exercised" is a row that cannot fail, which is the muting this
repo's baseline header warns about.

```bash
# POINT AT `latest`. It is the only environment with requireGatewayIdentity: true —
# values.yaml defaults it false and stage/load/preview/prod inherit that (#237).
export MEMOTRON_PROBE_MCP_URL=https://latest.jedai-memotron.wdprapps.disney.com/mcp
export MEMOTRON_PROBE_AGENTMEM_URL="$MEMOTRON_PROBE_MCP_URL"
export MEMOTRON_PROBE_ALPHA_KEY=sk-…   # two keys bound to DIFFERENT tenants;
export MEMOTRON_PROBE_BRAVO_KEY=sk-…   # the same key twice cannot show a disagreement

# sweep_mcp needs its OWN two, and exits non-zero without either:
export MEMOTRON_PROBE_KEY=sk-…         # or LITELLM_API_KEY; latest refuses unauthenticated
export SWEEP_TENANT=jedai-platform        # a tenant that key is BOUND to

bash scripts/verify/live_lane.sh
```

**Both `sweep_mcp` requirements were found by running it against deployed `latest`, not by
reading it.** Without the key the CONTROL fails and it exits **2**. With the key but the stale
default tenant it returns **36 ERRORs** reading `scope 'tenant:memotron-dogfood' is not one
of principal 'p-j…'` and exits **1** — which is the authorization guard working correctly,
surfaced as *one* server-level failure rather than 36 tool defects. With both set: exit **0**,
`EMPTY=6 ERROR=16 OK=14 SKIP=3` against latest on 2026-09-11.

Mint the keys on the **`-admin`** gateway host; the data-plane host answers *"Management
routes are disabled for this instance."*

Recorded expected exits, and the honest difference between them:

| probe | expected | basis |
|---|---|---|
| `sweep_mcp` | **0** | **Measured** 2026-09-10 against a live FastMCP 4 container: CONTROL OK, 28 tools, `agentmem OK=16 ERROR=10 SKIP=1` — identical to the pre-migration baseline |
| `probe_mcp_identity` | **0** | **Expected, not yet measured.** `resolve_gateway_identity()` calls the gateway admin plane over the network, so it cannot run locally. 0 is what #184's acceptance requires; the first real run confirms it or reports DRIFT |

`probe_mcp_identity` exits **2 by design** against an unarmed target — P0 aborts rather
than measuring an unguarded server. A 2 means *wrong target or unbound keys*, not a
regression.


## The three refactor guards

The module split's contract is *zero behaviour change, zero import change*. Three
checks enforce it, and they answer different questions — none is redundant:

| | question | kind | runs in |
|---|---|---|---|
| **P1** `api_surface.py` | did anything **disappear**? | runtime, composed MRO | `pytest` (~0.6s) |
| **P2** `tests/test_module_import_contract.py` | did the two import-time facts survive? | runtime | `pytest` |
| **P3** `pure_move.py` | did anything **change**? | static, bytecode | per commit, by hand |

P1 and P3 are complements: a symbol that stops being importable still has
identical bytecode wherever it now lives, so P3 cannot see it; and a body that
changed while moving is still importable, so P1 cannot see it.

Each was calibrated in both directions — a guard that never fires is not a guard:

| guard | negative case | result |
|---|---|---|
| P1 | drop a re-export from `memotron/__init__.py` | caught, named the symbol |
| P1 | remove one method from a class | caught, via the class digest |
| P1 | two mixins define the same private helper | caught, named both bases |
| P2 | (blocks `admin_server.py` becoming a package with a stale `parents[2]`) | assertion is anchored on `pyproject.toml` |
| P3 | forget an import while extracting | caught, named function + unbound name |
| P3 | one swapped argument in a symmetric call | caught, **while `pytest` reported 1011 passed** |

**Blessing P1.** `--bless` rewrites `tests/api_surface.golden.txt`. Read the diff
first: during the split the usual cause of a red golden is a re-export missed in
the new package's `__init__.py`, which is a bug, not a new baseline. The golden
deliberately excludes `__module__`, `__qualname__` and the MRO name list, all of
which change under a *correct* mixin split — a golden that reddens on every
correct commit gets blessed reflexively and stops guarding anything.

## `pure_move.py` — the refactor gate

Run it on every commit of the module split, before the suite:

```bash
uv run python scripts/verify/pure_move.py HEAD        # HEAD vs the working tree
uv run python scripts/verify/pure_move.py HEAD~1 HEAD # or any two refs
```

It compares compiled code objects, so it is a **static** proof over 100% of the moved
code — which matters because `dreaming.py` and `client.py` sit at 89% statement coverage,
so roughly 11% of what is being moved has no runtime witness at all.

Calibrated 2026-08-27 against a throwaway split of `coherence.py` into a package
(module-level values into `_sources.py`, methods into a `DetectionMixin` composed by
multiple inheritance — i.e. the real Shape A + Shape B patterns):

| scenario | expected | observed |
|---|---|---|
| `HEAD` vs `HEAD` | PASS | PASS · 1,962 functions · **0** pre-existing dangling globals |
| same tree, `PYTHONHASHSEED` 1 vs 99991 | identical | identical digests |
| faithful extraction | PASS | PASS, and `test_coherence.py` 20/20 |
| extraction with one import forgotten | FAIL | FAIL — named the function and the unbound name |
| extraction with one swapped argument | FAIL | FAIL — **while `pytest` reported 1011 passed** |

That last row is the reason this tool exists. The swap was `stances_conflict(first, second)`
-> `(second, first)`, and `stances_conflict` is symmetric, so the change is semantically
neutral *today* and no test can see it. A test suite proves behaviour on the paths it
executes; this proves the bytecode did not change at all.

Names present in only one ref are reported but are **not** failures — adding a mixin class
adds names. "Nothing disappeared" is the API-surface golden's question
(`tests/test_api_surface.py`); this tool answers the narrower "nothing changed".

## Starting servers to sweep

```bash
# governance MCP (deployed shape)
MEMOTRON_GRAPH_PATH=/tmp/x.sqlite MCP_HOST=127.0.0.1 MCP_PORT=8020 \
  uv run python examples/mcp_server.py &

# admin, standalone == deployed shape (platform API DISABLED)
memotron-admin-server --host 127.0.0.1 --port 8030 --graph-path /tmp/x.sqlite \
  --static-dir "$PWD/ui/admin/dist" --scope tenant:memotron-dogfood \
  --tenant-id memotron-dogfood --agent-id claude-code \
  --principal-id sweep --principal-role admin &

# all-in-one local dev (platform API ENABLED — different behaviour, see T1b-9)
uv run memotron-local-platform --graph-path /tmp/x.sqlite \
  --tenant-id memotron-dogfood --host 127.0.0.1 --ui-port 8022 --mcp-port 8021 &
```

`--static-dir` is required for the admin UI: `ui/admin/dist` is not in the wheel.

## `parity_postgres.py` — cross-backend gate

**Fails today (exit 1) — that is correct; it is reproducing T0-4.**

```bash
docker compose -p $(basename $PWD) -f docker-compose.local.yml up -d postgres
docker exec memotron-local-postgres-1 psql -U memotron -d postgres \
  -c "CREATE DATABASE dwparity LC_COLLATE='C' LC_CTYPE='C' TEMPLATE template0 ENCODING 'UTF8';"
uv run python scripts/verify/parity_postgres.py \
  "host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=dwparity"
```

Runs the same script against both engines in clean subprocesses with the same seed, so the
storage engine is the only variable. Checks the **caller's view**, which is the gap
`test_storage_backend_parity.py` leaves: its 117 tests assert both backends store the same
bytes, never that a scoped read returns the same rows.

Fails on: scoped-read row divergence, MENTIONS sorting first, `search()` divergence,
`epoch_content_digest` instability within one engine, and digest inequality across engines.

Use it as the exit check for any T0-4 fix — the fix is a one-line `AND type <> 'MENTIONS'`
in `postgres/_graph.py:370`, but the digest assertion is what proves it actually restored
replay verification rather than just quieting `search()`.

## `probe_kek.py` — B1/T0-2 gate

**Fails today (exit 1) — correct; it is reproducing the ephemeral KEK.** Re-run 2026-08-31
against the local Postgres: arm A OK, **arms B and C fail** with `wrapped DEK failed
authentication`, and status still reports the credential CONFIGURED. So T0-2 is live in the
form the register now describes.

**Repaired 2026-08-31.** Between the fail-closed guard landing and that date this probe exited
**2** saying *"the probe itself is wrong"*: it built the `Memotron` above its try blocks, so
a guard raise produced no result line and the parent read the baseline arm as failed. The
construction now sits inside the try, and the guard is a named outcome instead of an absence.

```bash
uv run python scripts/verify/probe_kek.py \
  "host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=dwkek"
```

FOUR arms now, one variable each: same process / fresh process same `.kek` / fresh process
fresh filesystem / **construct with `MEMOTRON_ALLOW_EPHEMERAL_KEK` unset**. **Arm B is the
important one** — it holds the KEK file constant, so a failure there proves the break is
per-process rather than a shared-file artifact. **Arm D** asserts the guard still refuses, and
is reported separately: a guard regression is its own finding, not a footnote to the B1 verdict.

Arms A–C now set the opt-out explicitly rather than inheriting it, in both directions. Inherited,
the probe's answer depended on the operator's shell and said nothing about which case had run.

Drives `provision_governance_key` -> `_require_live_key`, **not**
`tenant_llm_credential_state` — the latter returns metadata without unwrapping the DEK and
passes even when the key is unusable. Any rewrite must keep exercising the live-key path.

Exit check for D1.1 (`GcpKmsKeyManager` + the fail-closed guard): this must go 1 -> 0.

## `probe_storage_wiring.py` — T0-5 gate

**PASSES today (exit 0) — T0-5 is fixed.** Re-run 2026-08-31 against the local Postgres: both
paths attributed their writes to Postgres (`8979->8982`, `8982->8985`) with SQLite at `0->0`.
Fixed by `29624cb` (`agent_memory/_config.py:122`), which is an ancestor of the current branch
and **not** on `origin/main` — so this still fails for anyone running `main`.

This entry said "Fails today (exit 1)" until 2026-08-31, months after the fix. A gate's recorded
verdict is a claim like any other, and nothing re-derived it.

```bash
uv run python scripts/verify/probe_storage_wiring.py \
  "host=127.0.0.1 port=55432 user=memotron password=local-dev-only dbname=dwwiring"
```

Attributes a **write** per construction path (counts rows in each store before and after)
rather than reading the construction code — which is why it can be trusted about a fix as well
as about a defect. Both `Memotron(graph_path=)` and `build_platform_from_env()` now honour
the DSN; before `29624cb` the second silently wrote to SQLite.

## Running all three Tier-0 gates

```bash
export MEMOTRON_ALLOW_EPHEMERAL_KEK=1        # only needed once the T0-2 guard is applied
for g in parity_postgres probe_storage_wiring probe_kek; do
  uv run python scripts/verify/$g.py "$DSN"; echo "$g exit=$?"
done
```

On unmodified `main` all three exit 1. With `docs/findings/tier0-fixes.patch` applied,
`parity_postgres` and `probe_storage_wiring` go to 0; `probe_kek` stays 1 **by design**,
because the opt-out re-enables the ephemeral KEK — verify the guard instead by constructing
a Postgres backend with the flag unset and confirming it raises.

**Note:** `tests/test_adoption.py::test_post_compact_hook_persists_only_sanitized_continuity`
fails on `main` (T0-3b). Expect `1 failed` in any full-suite run; it is not yours.
