# Operational Store — decisions raised by the persistence work

Drafted for the component decision log (*Solution Engineering → Agent Memory →
Decision Log*). Same format as DW-001..DW-019: the choice, the alternative, why
the alternative lost for a reason that stays true, and the cost knowingly
accepted. Paste into the portal; this file is the working copy, not the record.

Three of these were decided during implementation because the issue asked for a
decision (`Decide what happens to the restore-on-read behavior`, `Decide whether
the per-scope state hash is computed incrementally`); the fourth records a
correction to the issue text itself.

---

## DW-020 — The Operational Store pool is asynchronous; the interface is not

The Operational Store runs `psycopg_pool.AsyncConnectionPool` on one dedicated
event-loop thread owned by the storage engine. Every `StorageBackend` call
marshals its coroutine onto that loop and blocks for the result, so the pool is
genuinely asynchronous — it multiplexes waiters, opens connections concurrently,
and enforces its own sizing — while the ~110-method interface stays synchronous.

**Alternatives:** convert `StorageBackend` to `async def` — rejected: it rewrites
every call site in `dreaming.py` and `client.py`, and it strands the hermetic
SQLite substrate DW-002 requires, because the offline proof gate must run with no
keys and no network; a plain threaded pool with no asyncio — rejected as a
weaker reading of the same requirement, though it would satisfy every acceptance
criterion, so this is the decision most open to revisiting.

**Cost:** a thread boundary between callers and the driver. Every call pays one
`run_coroutine_threadsafe` hop, exceptions cross the boundary and lose their
native traceback frame, and a wedged loop thread is a new failure mode that a
plain pool would not have. Transaction state is per calling thread, so a caller
that hands a backend to another thread mid-transaction gets undefined behaviour.

---

## DW-021 — Reviving archived memory becomes a curation act; the pure read is opt-in

Restoring a pruned memory is an explicit curation call, on both the agent
(`memory_restore`) and operator (`restore_archived_memory`) surfaces, and it
emits the same receipt and dream decision the implicit path emits. Search
additionally *reports* the archived rows it matched (`results.archived`, with a
`disposition` of `revived` or `available_to_restore`). Revive-on-read stays the
default; `DreamConfig.pure_read_retrieval` turns retrieval into a pure read for
deployments that need it.

**Alternative:** make the pure read unconditional and drop revive-on-read.
Rejected: retrieval is the hot path and the one operation that must scale across
replicas, so a deployment serving reads from more than one replica genuinely
cannot afford a read that takes row locks and appends to the receipt chain — but
that is an operator's constraint, not every caller's, and paying for it with a
silent behaviour change to every existing caller is not a trade this repo makes.
The audit argument survives the same way: with the flag on, a read that mutates
never happens, so an operator reconstructing why a forgotten memory came back
sees only curation decisions. With the flag off, the restore receipt still
records `retrieval_matched_prune_ghost` and distinguishes itself from a curation
restore by its decision reason.

**Cost:** two retrieval modes to reason about and to test — every archive-tier
assertion is written twice, once per mode. Operators who turn the flag on take a
real behaviour change: anything that relied on a search resurrecting a match must
read `results.archived` and call restore for the specific memory, and agents that
never learn to call it will behave as though archived memory is simply gone.

---

## DW-022 — The per-scope state hash is maintained incrementally, and its value is unchanged

Each memory relationship keeps its own canonical tuple row, written when that
relationship (or its subject node's name) changes. `graph_state_hash` is one
indexed aggregate over those rows, memoised per scope and invalidated on write.
The previous implementation recomputed the whole scope and performed one subject
lookup per relationship, twice per receipted change.

The hash **value** is deliberately identical to the SQLite recompute: the tuple
rows are encoded in Python with the same separators and ordering, and the
aggregate pins `COLLATE "C"` so byte order does not depend on the database
locale. The parity suite asserts the two engines produce the same string.

**Alternative:** a genuinely incremental hash function — an XOR-homomorphic or
running accumulator updated per row change, which is O(1) per write instead of
O(scope) per read. Rejected: it changes how the hash is derived, so every
receipt chain already written becomes unverifiable and the replay proof gates
in `PATENT_REPLAY_RECEIPTS_SPEC` no longer describe the system. The cost being
removed was the repeated scan and the N+1, not the final digest.

**Cost:** derived state that can drift. The maintained tuples are only correct
while every write path updates them, so the graph plane carries a
`recompute_scope_state_tuples` repair entry point and the test suite asserts the
maintained hash equals a full rebuild — including after a concurrent load run.
A scope's hash is now also cached, so a bug in invalidation is a wrong answer
rather than a slow one.

---

## DW-023 — Recovery from a lost graph row restores content, not the receipted hash

Under the cross-store rule a logical transaction anchors in the Operational
Store and Memory Graph writes are safe to repeat. Repeating them converges:
`upsert_node` keys on the normalised graph key and relationship updates are
last-write-wins by uuid. But if a graph **row is lost** while its anchor
survives, re-driving the write recovers the memory's content under a *new*
`uuid`, and the canonical state tuple binds the uuid — so the recovered scope
hashes differently from what the receipt committed to.

**Alternative:** exclude the uuid from the state hash so recovery is
hash-identical. Rejected: the uuid is what makes the hash a statement about
*these* memories rather than about a multiset of facts, and removing it would let
two different graphs hash the same. Alternative considered and deferred: let
recovery re-create a relationship with its recorded uuid, which would make replay
after row loss exact — this needs a uuid-accepting write primitive on the graph
plane and is worth doing if graph-row loss ever becomes a real recovery path
rather than a theoretical one.

**Cost:** a scope recovered from graph-row loss must be re-anchored with a fresh
receipted bracket; it cannot be verified against its pre-loss chain. This is a
boundary on byte replay, and it is recorded in the code
(`tests/test_operational_store_cross_store.py`) rather than left to be found
during an incident.

---

## DW-024 — Retrieval asks the engine; the bracket never spans a model call

Two wirings close the gap between what the Operational Store *can* do and what
the callers actually did.

**Retrieval.** `similar_relationships` and `relationships_for_scope` are now
part of the `StorageBackend` contract, implemented by both engines and routed
by the split composite. `semantic_search` asks the engine to score every
candidate with a plaintext stored embedding (pgvector ANN or SQL cosine on the
Operational Store; a scope-bounded in-backend scan on the hermetic substrate),
and `search_context` enumerates candidates per scope through the indexed read.
The caller-side comparison survives in exactly one place, deliberately: rows
the engine cannot score — sealed (WS-12) embeddings while the scope's DEK
lives, and legacy rows that pre-date the embedding substrate — are revealed
and scored by the caller, bounded to the scope. **Alternative:** let the
engine reveal sealed embeddings and score them too. Rejected: that ships
plaintext vectors of sealed content into an index the shred cannot reach,
which breaks the crypto-erase guarantee for a retrieval convenience.

**Bracketing.** `transaction()` is on the contract (re-entrant, one commit at
the outermost exit; the hermetic substrate implements it over its shared
connection, and the receipt ledger commits through the owning backend's
bracket so a receipt lands with the operation it records). `DreamEngine`
brackets each episode's materialization burst and the run's terminal
checkpoint + last-run marker. **Alternative:** bracket the entire run, which
is the literal reading of "a maintenance run commits once". Rejected:
formation and pruning interleave LLM calls with writes, and a transaction held
across a model call pins a pool connection for the model's latency and holds
row locks against every replica — the exact failure the pool sizing page
budgets against (`idle_in_transaction_session_timeout` would kill such a run
at 60 s). One commit per logical operation is what the loadtest's maintenance
actor measures and what the wired brackets deliver.

**Cost:** two retrieval scoring paths (engine and sealed-row fallback) whose
ordering must agree — pinned by the parity suite — and a bracket discipline
("never across a network call") that is a convention the next maintenance
phase must follow rather than something the types enforce.

---

## Correction to the issue text (not a decision)

The issue's Context says the Memory Graph "runs on Neo4j Enterprise". DW-001
names **FalkorDB** and explicitly rejects Neo4j, and records that adoption is
itself undecided pending the SSPLv1 legal ruling and the FalkorDB assessment,
with Postgres + pgvector as the working plan of record. The repo carried the
same stale reference in `storage/base.py`, `storage/factory.py`, and
`storage/settings.py`; those now say "the Memory Graph engine" rather than
naming one, since naming FalkorDB would overstate a decision the legal ruling has
not cleared either.
