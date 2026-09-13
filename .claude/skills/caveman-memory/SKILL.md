---
name: caveman-memory
description: Read and write caveman memory, the bounded compressed node graph over an append-only ledger. Use when the user runs /caveman-memory, asks what this scope remembers, asks why something is remembered or where it came from, wants to traverse the memory graph by node id, or wants a session ingested, compressed, proved, or erased.
---

# Caveman memory

Ten tools on the `caveman-memory` MCP server. Every one returns plain text to be
read as it arrives. `memory_contract()` returns the full agent contract; this
skill is the routing table for the `/caveman-memory` command.

Default scope: the repository or project the session is working in. Ask once if
it is not obvious, then reuse the same string for the whole session, because a
scope is a separate graph.

## Routing

| Command | Call | When |
|---|---|---|
| `/caveman-memory brief` | `memory_brief(scope)` | session start, and again after every compaction |
| `/caveman-memory read <query>` | `memory_read(scope, query)` | before relying on a remembered fact |
| `/caveman-memory node <id>` | `memory_node(node_id)` | re-read one block by id |
| `/caveman-memory neighbors <id>` | `memory_neighbors(node_id)` | walk that node's edges |
| `/caveman-memory explain <id>` | `memory_explain(node_id)` | provenance: claims, dates, entry ids, supersession |
| `/caveman-memory ingest` | `memory_ingest(scope, episode_id, turns_json)` | at compaction and at session end |
| `/caveman-memory dream` | `memory_dream(scope)` | after an ingest, to compress |
| `/caveman-memory replay` | `memory_replay(scope)` | to prove the compression |

With no argument, run `brief`.

`ingest` takes the turns worth remembering as `turns_json`, a JSON array of
objects with exactly the keys `speaker` and `text`, in order; turn numbers come
from the array order. Choose a unique `episode_id` per ingest. Ingest a
requirement, a decision and its reason, a confirmed root cause, a blocker, or a
correction. Never ingest a secret, a transcript, scratch work, or speculation,
and never ingest at all when the session confirmed nothing durable: zero writes
is a correct session.

`memory_erase(scope, episode_id)` deletes one episode's claims. Only on an
explicit request, and say which episode you are about to erase before you do it;
follow it with `dream`.

`memory_read`, `memory_ingest`, and `memory_dream` reach the JedAI Gateway. If
they answer with one line naming an unset environment variable, report that line
and stop: there is no offline mode.

## What comes back

One block per node, in this format.

    gateway (service) [n-001] as of 2026-09-11, 7 entries, aka JedAI Gateway, LiteLLM proxy
    rule: real runs always via the gateway, never hermetic or local stub
    rule: only undated aliases (claude-haiku-4-5), dated pins go stale
    is: LiteLLM proxy fronting the JedAI models, not a model itself
    chat default: claude-haiku-4-5
    embedding: text-embedding-3, 3072 dims, must be selected explicitly (x2)
    BLOCKED agent-memory [n-004] until #245: Host header refused, #245 fixed the rewrite
    unsure: session affinity lost about 1 in 20 calls, never reproduced
    more: explain(n-001), neighbors(n-001)

- header: `name (type) [id] as of DATE, N entries, aka other, names`
- `rule:` a hard constraint, binding
- `is:` the definition
- `key: value` an attribute under its own label
- `TYPE name [id] until X: claim` an outgoing typed edge; incoming reads
  `from name [id] TYPE: claim`
- `unsure:` believed, not established; do not report it as fact
- `(x2)` how many ledger claims support that line; absent means one
- `more: explain(n-001), neighbors(n-001)` the calls to make next

A read never shows a `superseded` fact. Those live in `memory_explain`.

## Using the ids

`[n-001]` is an address. Carry it, quote it to the user when you say where
something came from, and traverse with it: `neighbors` for what a node connects
to, `node` to read one of those, `explain` for the evidence. An id that no longer
resolves means the graph has been compressed since you read it, so read again
rather than guessing.

## Reporting to the user

Quote the memory's own lines rather than paraphrasing them, and name the node id
beside anything load bearing. Say `unsure` out loud when a line is marked
`unsure`. If a read contradicts what the user just said, call `explain` on that
node and show the dates before taking a side.
