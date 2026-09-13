# Caveman memory

Durable memory for an agent, exposed as ten MCP tools. Read this once at session
start; it is the whole contract.

## What this memory is

A small graph over an append-only ledger.

    your turns -> ledger claims -> nodes and typed edges -> the text you read

- A **node** is one bounded concept: a service, a defect, a person, a rule set.
  It has a name, a type, other names it answers to, and at most a handful of
  facts. The scope holds a bounded number of nodes, on purpose.
- A **fact** is one line on one node. Its kind is a word: `rule`, `is`,
  `attribute`, `unsure`, or `superseded`. A `rule` is a hard constraint you must
  not violate. An `attribute` may carry a short label, and then the label is
  what you see instead of the word `attribute`.
- A **belief about two concepts** is not a fact on either one. It is a typed
  edge: `BLOCKED`, `DEPLOYS`, `FRONTS`. The edge carries its own claim, and it
  may carry an `until` marker naming what ended it.
- The **ledger** is append-only and holds every claim ever extracted, with the
  episode and the turns it came from. The graph is a compression of the ledger,
  so the graph is rebuildable and the ledger is the record.
- The **journal** is the second append-only stream: one event per graph
  mutation, with the content before and after. Compression is therefore
  replayable rather than merely asserted, and `memory_replay` is the proof.
- **Evidence** is the count of ledger claims supporting a fact or an edge.
  Restating something you already told this memory does not duplicate it, it
  reinforces it: the new claim is appended to the same fact and the evidence
  count goes up. A fact with more than one supporting claim renders `(x2)`,
  `(x3)`, and ranks higher in a read.

## Your lifecycle

1. **Session start, and again right after every compaction.** Call
   `memory_brief(scope)`. This is the read for when you have no query yet,
   because the context that knew what to ask for is gone. It costs no model
   call.
2. **Before relying on a fact you half remember.** Call
   `memory_read(scope, query)`. Never answer from a recollection of an earlier
   session; read it.
3. **When a block matters.** Traverse. `memory_node(node_id)` re-reads one
   block, `memory_neighbors(node_id)` lists that node's edges with the node ids
   on the other end, and `memory_explain(node_id)` gives the provenance: every
   claim, its date, its entry ids, what it superseded, what superseded it.
4. **At compaction, and at session end.** Call
   `memory_ingest(scope, episode_id, turns_json)` with the turns worth
   remembering. Two model calls, whatever the size: one to extract the claims,
   one to route them onto nodes. No fact text is written yet.
5. **Then compress.** Call `memory_dream(scope)`. This is the only thing that
   writes fact text, merges concepts, retires edges, and holds the node and
   edge-type ceilings. One model call per changed node plus one for the whole
   scope.
6. **To prove it.** Call `memory_replay(scope)`. It applies the journal to an
   empty graph, with no model call and no embedding, and reports both content
   digests and whether they are equal. `digests equal: true` means the journal
   accounts for the graph belief for belief; `false` means something wrote to
   the graph outside the journal, and the two hashes are how that is diagnosed.
7. **To remove an episode.** Call `memory_erase(scope, episode_id)`, then
   `memory_dream(scope)` to re-compress what is left.

Zero writes is a correct session. Ingest what a later session would be worse off
not knowing: a requirement, a decision and its reason, a confirmed root cause, a
blocker, a correction of something previously believed. Do not ingest a
transcript, scratch work, or your own speculation.

**Never ingest a secret.** No API keys, tokens, passwords, or credentials, in
any turn you pass to `memory_ingest`. The ledger is append-only, so a secret
written into it is not something an edit can take back.

## The tools

Every tool returns plain text meant to be read as it arrives. No JSON to parse.

    memory_brief(scope)
        The whole scope, ranked, under the read budget. No query and no model
        call. Every rule in the scope first, then its highest-value nodes. Call
        this at session start and after a compaction.

    memory_read(scope, query)
        Answer one query from the scope. Seeds on exact name and identifier
        matches and on nearest neighbours in the vector space, then walks one
        hop along the edges. Deterministic and consults no model, but it does
        embed the query, so it needs the gateway.

    memory_node(node_id)
        One node re-rendered as a block, with its edges. Use it on an id that a
        read, a brief, or a neighbours listing handed you.

    memory_neighbors(node_id)
        One line per edge on that node, with the node id on the other end, so
        you can keep walking. Ends with the calls to make next.

    memory_explain(node_id)
        The deep read: unbounded provenance for one node. Every ledger claim
        newest first, with dates, entry ids, and supersession in both
        directions. This is where a `superseded` fact is visible; a read never
        shows one. Ask this before contradicting something the memory says.

    memory_ingest(scope, episode_id, turns_json)
        Write claims. `episode_id` is your own stable id for this episode; the
        same id twice is two episodes, so make it unique per ingest.
        `turns_json` is a JSON array of objects with exactly the keys `speaker`
        and `text`, in order:
            [{"speaker": "Priya", "text": "every real run goes through the gateway"},
             {"speaker": "agent", "text": "confirmed, #245 fixed the Host rewrite"}]
        Turn numbers are assigned from the array order. Returns the claims
        extracted, the nodes they routed to with their ids, and which of those
        nodes are new.

    memory_dream(scope)
        Compress. Writes the fact text, reinforces what was restated, marks what
        was corrected as superseded, merges and retypes nodes, retires and
        renames edges, and holds the ceilings. Every mutation is journalled.

    memory_erase(scope, episode_id)
        Delete one episode's claims. Any node left with no support at all is
        deleted; the rest are marked for re-compression. No model call, so this
        works when the gateway does not. Follow it with `memory_dream`.

    memory_replay(scope)
        The compression proof: the live content digest, the digest of the graph
        rebuilt from the journal alone, whether they are equal, and how many
        events were replayed.

    memory_contract()
        This document.

`memory_read`, `memory_ingest`, and `memory_dream` reach the JedAI Gateway. If
the gateway key is not configured they return one line naming the environment
variable to set. There is no offline mode.

## What you will read

A block per node. This is the whole format.

    gateway (service) [n-001] as of 2026-09-11, 7 entries, aka JedAI Gateway, LiteLLM proxy
    rule: real runs always via the gateway, never hermetic or local stub
    rule: only undated aliases (claude-haiku-4-5), dated pins go stale
    is: LiteLLM proxy fronting the JedAI models, not a model itself
    chat default: claude-haiku-4-5
    embedding: text-embedding-3, 3072 dims, must be selected explicitly (x2)
    BLOCKED agent-memory [n-004] until #245: Host header refused, #245 fixed the rewrite
    unsure: session affinity lost about 1 in 20 calls, never reproduced
    more: explain(n-001), neighbors(n-001)

Line by line:

- **Header.** `name (type) [id] as of DATE, N entries, aka other, names`. The
  date is when this node's content was last written, so it is how you judge
  staleness. `N entries` is how many ledger claims stand behind the whole node.
  `aka` lists the other names this node answers to, which is the spelling to use
  when you talk about it.
- **`rule:`** a hard constraint. Treat it as binding. Rules render first and are
  the last thing a budget drops.
- **`is:`** what the thing is. Its definition.
- **`key: value`** an attribute under its own label, like `chat default:` and
  `embedding:` above. A fact carrying a label shows the label, never the word
  `attribute`.
- **`TYPE name [id] until X: claim`** an outgoing edge. The type is the belief,
  `[id]` is the node on the other end, `until` names what ended it, and the text
  after the colon is the claim. An incoming edge reads
  `from name [id] TYPE: claim` instead, so direction is always a word and never
  a guess.
- **`unsure:`** believed, not established. Intermittent, unreproduced, or
  reported once. Do not present it as fact.
- **`(x2)`** the evidence count, appended when more than one ledger claim
  supports that line. No suffix means one claim. More evidence is more
  corroboration, not more importance.
- **`more: explain(n-001), neighbors(n-001)`** the last line of a read. These
  are calls, over the exact nodes the read emitted.

A block never shows a `superseded` fact. Those are history, and history lives in
`memory_explain`.

## What the node ids are for

`[n-001]` is an address, not decoration. It is what turns a block of text into
somewhere to go next, and every traversal tool takes one:

    memory_read       ids in every header, and again in the `more:` footer
    memory_node       that one block again
    memory_neighbors  the ids on the other end of each edge
    memory_explain    that node's whole provenance

So the walk is: brief or read to find a foothold, neighbours to find what it
connects to, node to read a neighbour, explain when you need the evidence. Carry
the ids, and quote them when you tell a human where something came from.

Ids are stable while a node lives. Compression can merge two nodes, and the
merged-away id then stops resolving. An id that does not resolve means your
state is older than the graph, so read again rather than guessing.
