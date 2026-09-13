# Storing your first memory in Memotron

For an **agent author or service owner** who wants Memotron to remember things across
sessions. It is the consumer counterpart to `environment-bring-up.md`, which is written for
operators standing an environment up.

**Written by doing it**, against deployed `latest` on 2026-09-10. Every command below was run
and its output pasted, not paraphrased. Where something did not work, it says so.

**At the end you will have**: an agent registered, one fact written, and that same fact
retrieved by id — a round trip, not a hopeful-looking response.

---

## 0 · What you need first

**A gateway key that is bound to a tenant.** Not just any LiteLLM key: it has to be bound, or
the platform knows who you are but not what you may touch.

You can tell in one request. With no key at all:

```console
$ curl -s https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/status
{"error":"no gateway key on the request"}
```

That refusal is the good outcome — **it means the API exists and is guarding itself**. Three
responses to tell apart, because they need different fixes:

| response | meaning | what to do |
|---|---|---|
| `503` platform not enabled | the API is off in this environment | operator: enable it |
| `401 no gateway key on the request` | you sent no key | send `x-litellm-api-key` |
| `403` naming `memotron key bind` | your key is not bound | ask an operator to bind it |
| `200` | you are in | continue |

The 403 arm is carried from the verification in #221 and was **not** re-run today — I have no
unbound key to test with. The other three were run today.

## 1 · Confirm who you are

```console
$ curl -s -H "x-litellm-api-key: $KEY" \
    https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/status
{"tenant_id":"jedai-platform","mode":"simple","agent_ids":[...],"registered_agents":[...]}
```

`tenant_id` is the boundary you live inside. You cannot read or write outside it, and that is
enforced beneath both transports rather than at the door.

## 2 · Read the integration contract

```console
$ curl -s -H "x-litellm-api-key: $KEY" \
    https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/integration-contract
```

It tells you the scope keys you own, the required tools, and the recommended call sequence.

> **Do not use its URLs.** On the hosted deployment it advertises
> `platform_api_url: http://127.0.0.1:8765/` and an empty `mcp_url` — local-development
> defaults. Follow those and you connect to nothing. Tracked as **#242**. Everything else in
> the contract is correct; use the hostname you already used to fetch it.

The route is `integration-contract`. There is no `/api/platform/contract` — that returns
`{"error":"not found"}`.

## 3 · Register your agent

One call registers you and starts a run:

```console
$ curl -s -X POST -H "x-litellm-api-key: $KEY" -H 'Content-Type: application/json' \
    -d '{"agent_id":"my-agent","agent_name":"My Agent"}' \
    https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/memory/bootstrap
{"registration":{"agent_id":"my-agent","created":true,...},
 "start":{"project_scope":{"kind":"tenant","scope_id":"jedai-platform"},
          "agent_scope":{"kind":"agent","scope_id":"my-agent"},...}}
```

**Registration is self-service and idempotent.** Calling it again returns `"created": false`
rather than failing. Keep the `task_run_id` from the response and reuse it — that is how your
later calls are attributed to this run.

## 4 · Write a fact

```console
$ curl -s -X POST -H "x-litellm-api-key: $KEY" -H 'Content-Type: application/json' \
    -d '{"agent_id":"my-agent","subject":"the onboarding doc",
         "predicate":"was validated against","object":"deployed latest",
         "relationship_type":"IS"}' \
    https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/memory/remember
{"relationship_uuid":"69f81178-1e38-4683-995b-c04481d4f3b9",
 "fact":"the onboarding doc was validated against deployed latest",
 "created_relationships":1,...}
```

**Keep that `relationship_uuid`.** The next step is only meaningful because you can ask for
*that specific fact* back.

## 5 · Read it back — and check the id

```console
$ curl -s -X POST -H "x-litellm-api-key: $KEY" -H 'Content-Type: application/json' \
    -d '{"agent_id":"my-agent","query":"onboarding doc validated"}' \
    https://latest.jedai-memotron-admin.wdprapps.disney.com/api/platform/memory/search
```

**Assert that your `relationship_uuid` is in the results.** Do not settle for "the response was
not empty" — `memory_search` returns pre-existing facts, so a server that has stopped forming
memory still answers convincingly. The uuid check is the difference between a round trip and a
green-looking dead end. That is the single most useful habit on this API.

---

## Two things that will surprise you

**Your "personal" facts are not private to you.** `memory_remember` writes to a `user:` scope
whose id is derived from the container's OS user, so it is a **constant baked into the image** —
every caller of every deployment shares `user:local-cac319ad1dd105e6`. Confirmed today: my
write landed there. Do not put anything caller-specific in it. Tracked as **#223**.

**Project memory needs configuring once per tenant before it accepts anything.**
`/api/platform/memory/publish` returns 400 until `project-memory/config` has been called for
that tenant — a one-time setup step, not a per-agent one.

**On `latest`/`jedai-platform` this is already done**, so publish returns 200 for you today;
verified just now with a freshly registered agent. You will only meet the 400 as the first
consumer in a *fresh* tenant, and the error does not say which setup step is missing, so it is
worth knowing the shape in advance.

Relatedly, `min_endorsements` above 1 is unsatisfiable over HTTP (**#216**) — one principal
cannot reach a higher quorum, and candidates accumulate with no error explaining why. Leave it
at 1 unless something else changes.

## What is not here yet

**MCP.** The README describes `memory_bootstrap`, `memory_search` and `memory_remember` as MCP
tools. They are — but the MCP server carrying them is not deployed yet (**#206** Phase 4,
PR #240). Until that lands, **the HTTP routes above are the supported path**, and a client
pointed at the deployed `/mcp` will not find those tools.

## If you get stuck

* **421 Invalid Host header** while `/health` returns 200 — you reached the server by a
  hostname its allowlist does not carry. Not an auth problem.
* **400 naming an unknown field** — the API names exactly what it rejected. Drop that key and
  retry; the message is reliable enough to script against.
* **Your fact does not come back** — check the `relationship_uuid`, not the result count.
