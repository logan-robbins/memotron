# Bringing an environment up to a working Memotron

What it takes to get from *deployed* to *a console showing real memory*, written from
doing it on `latest` on 2026-09-08.

**Everything below marked VERIFIED was run against `latest` and its output read. Nothing
here has been run against `stage`, `load`, `preview` or `prod`** — the steps are expected
to transfer because the chart is shared, but the access facts in §1 are known to differ per
environment and are the most likely thing to be wrong for you. Treat §1 as something to
re-measure, not to trust.

---

## 0. The shape of the problem

A deployed Memotron can be entirely healthy and still show an empty product. Four
things have to be true, and they fail independently:

| | what breaks if it is missing |
|---|---|
| the console addresses a tenant that exists | every tenant-keyed read is empty; `purge` targets the wrong tenant |
| the dream worker has a real LLM transport | it runs, reports success, and forms nothing |
| a gateway key is bound to a principal | every authenticated MCP call is `403` |
| that principal's tenant has memory | the console renders zero over a healthy store |

On `latest` all four were false at once, and each looked like a different problem.

---

## 1. Get to the store — measure your access, do not assume it

**This is the step that cost the most time, and the assumption that cost it was mine.**

The operational-store credential lives in one CSM/Vault secret per environment:

```
secret/apps/jedai/memotron/us-east-1/<env>
keys: host  port  database  user  password  sslmode   (+ MEMOTRON_KEK_B64, LITELLM_API_KEY)
```

`host` is a **private IP** (`10.x.x.x` on `latest`, VERIFIED). So reading the secret is not
sufficient — you also need a network path. Three routes, cheapest first:

### 1a. `kubectl exec` into a running pod — the one that worked

The pods already have `MEMOTRON_OPERATIONAL_STORE_DSN` in their environment and the
`memotron` CLI on their path. No Vault, no proxy, no credential handling.

```sh
kubectl config use-context gke_jennay-latest-1_us-central1_usc1-jennay-latest-v1n1-cluster-1
kubectl get pods -n jedai-memotron
```

> **Two traps here, both of which produced a confident wrong answer.**
>
> * **The namespace is `jedai-memotron`, not `memotron`** (`namespaceOverride` in
>   every `values-<env>.yaml`; `preview` uses `preview-jedai-memotron`).
>   `kubectl auth can-i` answers happily for a namespace that does not exist, so asking it
>   about the wrong one returns a meaningless `yes`.
> * **Check which context is active.** The shell default here was
>   `gke_jennay-stage-1_...`. A `Forbidden` from stage reads exactly like a `Forbidden`
>   from latest, and STATE.md carried "kubectl is refused" for two weeks on that basis.
>   It is refused on **stage/load**; on **latest** it is not.

Prove access before relying on it — note `${=v}` because **zsh does not word-split an
unquoted variable**:

```sh
for v in "list pods" "create pods/exec"; do
  echo "$v -> $(kubectl auth can-i ${=v} -n jedai-memotron)"
done
```

### 1b. Cloud SQL Auth Proxy from a laptop

Only if 1a is unavailable. Needs `roles/cloudsql.client`, which the `svc-gke-deploy`
account does **not** have (VERIFIED: `gcloud sql instances list` is refused for it).

```sh
CN=$(gcloud sql instances describe <instance> --project <project> --format='value(connectionName)')
cloud-sql-proxy "$CN" --port 5433
```

Then point the DSN at the tunnel, **not** at the Vault `host`/`port`:

```
host=127.0.0.1 port=5433 dbname=<database> user=<user> password=<password> sslmode=disable
```

`sslmode=disable` is correct here and is not a weakening: the proxy terminates TLS at the
Cloud SQL end, and the loopback hop has none, so `require` fails.

### 1c. Vault CLI

`VAULT_ADDR` was set nowhere on the operator laptop (VERIFIED: absent from the
environment, shell profiles and the workspace). `csm.wdprapps.disney.com` is a **web UI
only** — every path returns the SPA's `index.html` with HTTP 200, so a 200 from
`/v1/sys/health` there proves nothing.

---

## 2. Point the console at a tenant that exists

`--tenant-id` is supplied through the env map, not the args list (Helm merges maps and
*replaces* lists). Empty means **derive from the graph**:

* zero registered tenants → falls through to `DEMO_TENANT_ID` (`"wdpr-demo"`), which is what
  the demo environments want
* exactly one → derives it
* **two or more → silently falls back to `wdpr-demo`**, so any environment with a real
  store should PIN its tenant:

```yaml
roles:
  admin:
    deployment:
      env:
        MEMOTRON_CONSOLE_TENANT_ID: "<tenant>"
        MEMOTRON_CONSOLE_SCOPE: "tenant:<tenant>"
```

The server tells you when this is wrong. `/api/tenant-config` carries it in
`tenant.warnings` — read that array **first**, before the field you came for. It named this
exact defect on `latest` for months while nobody looked.

---

## 3. Bind a gateway key to a principal

> **Making that key work through the JedAI Gateway is a different job**, and binding is only
> one of its four conditions — the other three live in LiteLLM and in this chart, and all four
> fail identically as `{"tools": []}` with HTTP 200. See
> **[gateway-mcp-registration.md](gateway-mcp-registration.md)**.


Until this exists, every authenticated MCP call is `403` and the error names the remedy:

```
{"error": "gateway key_alias '<alias>' is not bound to a principal;
           bind it with: memotron key bind --alias <alias> --principal-id <id> --tenant-id <tenant>"}
```

Find the alias for a key without printing the key:

```sh
uv run --no-sync python -c "import os; from memotron.gateway_identity import resolve_gateway_identity; print(resolve_gateway_identity(os.environ['LITELLM_API_KEY']).key_alias)"
```

**`show` before `bind`.** Re-binding is the rotation path *within one tenant*; an alias
bound elsewhere should stop you.

```sh
POD=$(kubectl get pods -n jedai-memotron -l role=api -o jsonpath='{.items[0].metadata.name}')
kubectl exec -n jedai-memotron "$POD" -- uv run --no-sync memotron key show --alias <alias>

kubectl exec -n jedai-memotron "$POD" -- uv run --no-sync memotron key bind \
  --alias <alias> --principal-id <principal> --tenant-id <tenant> --agent-id <agent> \
  --role user --default-scope-key tenant:<tenant> --allowed-scope-key tenant:<tenant>

kubectl exec -n jedai-memotron "$POD" -- uv run --no-sync memotron key show --alias <alias>
```

The final `show` is not ceremony — **the bind's own success message is not evidence**, and
#183 established the same discipline when arming the identity guard.

* **`--role user`, not `admin`.** An ADMIN principal passes negative controls for the wrong
  reason and hides guard failures.
* **One `--allowed-scope-key`.** A principal that can reach one scope cannot become a
  second instance of the cross-tenant read that #200 closed.
* **Never `--migrate`.** Its own help warns against silently migrating a production store
  as a side effect of a lookup.

> Arming `requireGatewayIdentity` with an **empty** `key_principals` table refuses *every*
> caller — each request resolves to no principal and fails closed. Bind rows **before**
> flipping the flag, not after (the note #183 left in `values-latest.yaml`).

---

## 4. Seed the tenant, and read it back

Write through the MCP surface — the supported path — with the bound key. `add_memory`
requires `subject`, `predicate`, `object`, `relationship_type`, `scope_kind`, `scope_id`.

> **Expect a transient `404` mid-session.** VERIFIED on `latest`: the api role runs **two
> replicas** and MCP sessions are held in-process, so a session established on one pod is
> unknown to the other. Retry, or pin session affinity before building a long-lived client
> against this.

Then verify from the *other* surface rather than trusting the write's return value:

```sh
curl -s "https://<env>.jedai-memotron-admin.wdprapps.disney.com/api/overview"
```

`tenant:<tenant> rels=N` with `N > 0`, and **no neighbouring scopes listed**.

---

## 5. Confirm the loop actually runs

The worker is a CronJob. It is working when **`consolidation` runs appear at all** — that
job does not exist under the library default config, so its presence is the tell that the
deployment is on `agent_memory_config()`:

```sh
curl -s "https://<env>.jedai-memotron-admin.wdprapps.disney.com/api/dream-runs?limit=400"
```

Look for `job_name: consolidation-default`, and for a `formation` run with
`processed_episodes > 0` and `created_relationships > 0`.

**Corroborate with a second measurement.** On `latest`, formation reported `rels=3` and the
scope's `relationship_count` moved `3 → 6` — one event, two independent readings. A run
report alone is a log entry, not an observation.

`pruned=0` across hundreds of pruning runs is **not** a defect on a young store: pruning is
*unfed* until memories supersede one another. `decision_count=0` on those runs is what
distinguishes "nothing was prunable" from "pruning is broken".

---

## 6. Verify the guards, with a control

Every refusal check needs a third probe that must still **succeed** — otherwise a guard
that refuses everything looks identical to a working one:

```sh
A="https://<env>.jedai-memotron-admin.wdprapps.disney.com"
curl -s "$A/api/graph?scope=tenant:<a-neighbour>"   # expect 400, not registered
curl -s "$A/api/graph?scope=tenant:<your-tenant>"   # expect 200   <-- the control
curl -s -o /dev/null -w '%{http_code}\n' -X POST "$A/api/tenant-config/purge" -d '{}'   # expect 405
curl -s -X POST "https://<env>.jedai-memotron.wdprapps.disney.com/mcp" \
  -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'  # expect 401
```

The `401` only holds where `requireGatewayIdentity` is true. **VERIFIED: that is `latest`
and nothing else** — `helm template` renders the variable for `latest` and zero times for
stage, prod, preview and load.

> **This line used to say "renders the variable ONCE", and that count was the bug.**
> It rendered once because `roles.yaml` gated on the **api role only**, so the admin
> surface never received it. Two merged changes that read the flag in the admin process
> (#50, #194) therefore shipped inert, and `requireGatewayIdentity: true` did not mean what
> every doc — including this one — took it to mean. Fixed 2026-09-09; it now renders twice,
> api and admin.
>
> **So count per ROLE, not per environment**, and confirm on the running container with a
> role that should have it as a positive control — an absent variable is otherwise
> indistinguishable from a broken query:
>
> ```sh
> kubectl get pod -n jedai-memotron <admin-pod> -o jsonpath='{.spec.containers[0].env[*].name}'
> kubectl get pod -n jedai-memotron <api-pod>   -o jsonpath='{.spec.containers[0].env[*].name}'
> ```

## 7. Drive the hosted platform API

The agent-memory HTTP API (`/api/platform/*`, 18 routes) answers only where the identity
flag is armed — it is created at startup by `build_hosted_platform` and is `None` otherwise,
which is why every route 503s on an environment that cannot attribute its callers. That
gating is deliberate: `--no-admin-writes` does **not** cover this prefix, so enabling it
without identity would publish 15 unauthenticated write routes.

Three states, and knowing which one you are in saves an hour:

```sh
A="https://<env>.jedai-memotron-admin.wdprapps.disney.com"

curl -s "$A/api/platform/status"
#   503 {"error":"Memotron platform API is not enabled for this server"}
#       -> the flag is off for this env, or not reaching the ADMIN role (see §6)
#   401 {"error":"no gateway key on the request"}
#       -> the API is live and refusing you; supply a key

set -a; . ./.env; set +a
curl -s -H "x-litellm-api-key: ${LITELLM_API_KEY}" "$A/api/platform/status"
#   200 -> tenant_id, agent_ids, registered_agents, project_scope   <-- the control
#   403 naming `memotron key bind --alias <alias> ...`
#       -> the key is real but its alias is not bound; do §3 first
```

**Measured on `latest` 2026-09-09**, all three. The `401` body is emitted by
`admin_server._caller_principal`, so seeing it proves the platform exists AND the caller
check runs AND the flag reached this process — three facts from one response.

Run the `200` arm every time. A guard that refuses everything satisfies the 401 and 403
checks and is an outage.

---

## What this does not cover

* **`purge` cannot remove another tenant's data.** It is `405` under `--no-admin-writes`,
  and `purge_tenant_state` targets the *launch* tenant regardless. Removing a neighbouring
  scope's rows needs direct store access, not the admin API.
* **Memory content is not encrypted at rest** under the default governance policy; sealing
  is gated on `CRYPTO_SHRED`, which no deployed path sets.
* **The admin surface authenticates nobody** (#137). #200 bounded it to one tenant; bounded
  and authenticated are different things.
* **`user:` and `customer:` scopes cannot be attributed to a tenant** (#201), so they are
  reachable only by being named explicitly.
* **Consumer onboarding.** This is operator bring-up. What a *consumer* needs — get a
  gateway key, get a principal bound, call `add_memory`, see it in the console — is not
  written down yet (C1/C2/C3).
