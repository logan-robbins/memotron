# Making Memotron's MCP reachable through the JedAI Gateway

Operator-side. Written from doing it on `latest` on 2026-09-10, including every wrong turn,
because the wrong turns are most of the value here.

**The problem in one line:** four conditions must hold, they are owned by three different
systems, and **all four fail identically — `tools/list` returns `{"tools": []}` with HTTP
200 and no error.** A green-looking zero is the default failure mode of this integration.

---

## The four conditions

| # | condition | owned by | symptom when missing |
|---|---|---|---|
| 1 | the server is registered, and in an access group | mcp-forge → LiteLLM | 0 tools, `_meta` **empty** |
| 2 | the caller's key has that MCP access group | LiteLLM | 0 tools, `_meta` **empty** |
| 3 | the key is bound in `key_principals` | **Memotron** | 0 tools, `_meta` **403 forbidden** |
| 4 | the gateway's Host is in `MEMOTRON_MCP_ALLOWED_HOSTS` | **Memotron's chart** | 0 tools, `_meta` **421** |

`_meta` (`litellm.ai/server_outcomes`) is the **only** signal that separates them. Read it.
The tool list is identical in all four states.

Conditions 1–2 live in one repo, 3 in a database, 4 in a Helm value. Nothing checks them
together except the probe below.

## Verify first, then fix what it names

```bash
uv run python scripts/verify/probe_gateway_mcp.py \
    --gateway https://latest.jedai-gateway.wdprapps.disney.com \
    --key "$YOUR_KEY"
```

It does the full MCP handshake, probes a **known-good control server first**, and refuses to
report a verdict (exit 2) if the control returns nothing — a probe that cannot produce a
success cannot interpret a failure.

**Do not skip the handshake.** A bare `tools/list` POST returns 0 tools for *every* server,
including working ones. MCP Streamable HTTP needs
`initialize` → `notifications/initialized` → `tools/list`, carrying the `mcp-session-id` the
initialize response returns **as a header**. This cost real time: the silent zero was nearly
filed as a Memotron defect before a control produced the identical zero.

## The procedure

### 1 · Register the server (mcp-forge)

The spec is `deploy/litellm/registrations/jedai_memotron.yaml`. From that repo:

```bash
export LITELLM_API_KEY="$(cat .local-secrets/litellm-<env>-key)"
uv run mcp-validate registry plan  --env latest        # read-only, shows the actions
uv run mcp-validate registry apply --env latest --yes   # WRITES. never pass --allow-delete
```

**Run `apply` twice.** The first creates the registration; the access-group membership shows
up as a *second* finding afterwards (`ADD_SERVERS latest/General`) and needs its own apply.
Verify with `registry drift --env latest` — reads lag writes by ~90s.

### 2 · Mint a key with the MCP access group (LiteLLM)

Minting works **only on the `-admin` host**; the data-plane host answers *"Management routes
are disabled for this instance."*

```bash
curl -X POST https://latest.jedai-gateway-admin.wdprapps.disney.com/key/generate \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"key_alias":"<alias>","object_permission":{"mcp_access_groups":["General"]}}'
```

The response echoes `object_permission: {}` even on success — **do not read that as failure.**
Verify by behaviour: run the probe and check the *control* server returns tools.

### 3 · Bind the key in Memotron

The gateway knowing your key is not Memotron knowing it. Binding is by **`key_alias`**, not
the key value, and needs the operational-store DSN — easiest from a pod that already has it:

```bash
kubectl exec -n jedai-memotron <admin-pod> -- \
  memotron key bind --alias <alias> --principal-id p-jedai-platform \
    --tenant-id jedai-platform --agent-id <agent> --role user \
    --default-scope-key tenant:jedai-platform --allowed-scope-key tenant:jedai-platform
```

`--principal-id` is required. Match an existing row: `memotron key show --alias <known>`.

### 4 · Put the gateway's Host in the allowlist

**The step everyone misses**, because it is invisible until 1–3 are all correct.

LiteLLM dials the pod **in-cluster**, so the Host header is the *Service DNS*, not the ingress
hostname. Measured on the running pod, both controls included:

```
Host: latest.jedai-memotron.wdprapps.disney.com          -> 200 OK
Host: jedai-memotron-api...svc.cluster.local:8000        -> 421 Invalid Host header
Host: not-allowed.example.com                               -> 421 Invalid Host header
```

The gateway's own Host was refused exactly like a bogus one. Both the Service DNS **and** the
ingress host belong in `MEMOTRON_MCP_ALLOWED_HOSTS` for that role, per environment.

**Two things hide this:**

* at `/mcp/` **every** Host returns **307** — the redirect to `/mcp` runs *before* the Host
  check, so probing the documented path tells you nothing;
* without a bound key everything **401**s first.

It is only visible at `/mcp`, with a bound key. That combination had never existed before C4,
which is why #206 could predict this 421 and still never see it fire.

## What the mcp-forge gates will NOT tell you

`registry reconcile` reports `jedai_memotron (3p) Reachable — external` and `toolcount`
omits it entirely. Its first-party test requires a **card in mcp-forge** whose `manifest_id`
starts `mcp_jedai_`, and Memotron's MCP server lives in *this* repo — so the classifier
exempts it and both gates print green having checked nothing.

**Read the row for `jedai_memotron`, never the summary line.** `external` renders like a
pass. This is why the probe lives here rather than relying on those gates.

## Cleaning up a probe key

Probe keys are real credentials. Delete when done:

```bash
curl -X POST https://latest.jedai-gateway-admin.wdprapps.disney.com/key/delete \
  -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' \
  -d '{"key_aliases":["<alias>"]}'
```

Then `memotron key unbind --alias <alias>` so `key_principals` does not keep a row for a
key that no longer exists.
