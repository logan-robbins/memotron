# Persistence: the StorageBackend contract

Every byte Memotron persists goes through `memotron.storage`. The
contract lives in code — `src/memotron/storage/base.py` is the single
authority, and its docstrings are the normative text — this page is the
orientation for it. Portal companion: *Solution Engineering → Agent Memory →
Engineering → Memory → Persistence → The StorageBackend contract*.

## Why an interface

`PropertyGraphStore` grew as one SQLite class owning every table and all the
SQL. The persistence work needs three implementations: **SQLite** for the fast
local test suite (no credentials, no network), **Postgres** for the
Operational Store, and a **graph engine** for the Memory Graph. That last one
is a query rewrite, not a driver swap — the current store is written
relationally — so the interface is what keeps the rewrite from spilling into
every caller. This contract is step zero.

Note on the graph engine: DW-001 names **FalkorDB** as the designed target and
explicitly rejects Neo4j, and adoption is itself undecided pending the SSPLv1
legal ruling and the FalkorDB assessment. Until that resolves, the plan of
record is Postgres + pgvector through this same interface. Nothing in the repo
should name a specific graph engine as settled; where one is named here it is
as DW-001's designed target, not as a shipped dependency.

## The shape

```
memotron.storage
├── base.py        StorageBackend = MemoryGraphStorage ∪ OperationalStorage (the contract)
├── sqlite.py      SQLiteStorageBackend — the refactored PropertyGraphStore
├── postgres/      PostgresStorageBackend — the Operational Store (pooled, migrated)
│   ├── _engine.py      async pool on a dedicated loop thread + re-entrant transactions
│   ├── _migrations.py  versioned schema; jsonb properties; the ctx-visible index
│   ├── _graph.py       graph plane: race-free writes, pushed-down filters, incremental state hash
│   ├── _operational.py episodes, jobs, dream runs, the event plane, prune ghosts
│   ├── _artifacts.py   live artifacts, versions, coherence repair monitors
│   ├── _policy.py      contracts, shadow stages, alias activation (the DW-016 gate)
│   ├── _governance.py  erasure keys, tenant identity and config, the tenant purge
│   └── _receipts.py    PostgresReceiptLedger — the chain, serialised per run
├── receipts.py    ReceiptLedger — rides the Operational Store's transaction domain
├── settings.py    EngineSettings / StorageSettings (selection by configuration)
├── factory.py     engine registry + create_storage_backend / open_storage
└── composite.py   SplitStorageBackend — two stores, two engines, one surface
```

Callers — client, maintenance (dreaming), retrieval, coherence, adoption,
runtime — depend on `StorageBackend` only. `memotron.graph` and
`memotron.receipts` remain as import shims; `PropertyGraphStore` is now an
alias of `SQLiteStorageBackend`.

## Which store owns which part of the surface

**Memory Graph** (`MemoryGraphStorage`; Neo4j when it lands): entity nodes,
memory relationships, the context-visible read tier, scoped truth lookups, the
scope state hash, export, and the idempotent deletion primitives the erasure
sweep uses. WS-26 adds the derivation-DAG epoch surface here: `graph_epochs` /
`epoch_runs` / `active_epochs` (the per-scope HEAD pointer), plus run-provenance
stamps (`produced_by_run`/`superseded_by_run`/`pruned_by_run`) on every
relationship version and an additive `epoch_id` lineage column on
`entity_canon`/`predicate_canon` for the T9 registry overlay a branched
re-dream writes.

**Operational Store** (`OperationalStorage`; Postgres when it lands): the
queue of incoming episodes and their per-consumer processing marks; job state;
maintenance (dream) runs and the decision log; the use/outcome event plane and
its projections; prune ghosts; live artifacts, versions, and repair monitors;
policy contracts, shadow stages, and live aliases; tenant identity, prompts,
and project config; the governance keys used for erasure (crypto-shred);
tenant LLM credentials; the receipt ledger; and the tenant purge, which
anchors here.

## What callers may assume about transactions and concurrency

- **Per-call atomicity.** Every mutating method fully applies and is durable
  on return, or raises with no partial write visible in that store.
- **Logical-operation bracketing.** `transaction()` widens per-call atomicity
  to one logical operation: everything written inside the bracket commits
  together at the outermost exit, or not at all. Re-entrant per calling
  thread; never held across an LLM or network call; a Memory Graph on a
  different engine does not join it (its writes stay idempotent per the
  cross-store rule). The maintenance plane uses it in two places: an
  episode's whole materialization burst commits once, and a run's checkpoint
  commits together with its last-run marker. Pruning applies each prune as
  its own transaction because its decision loop may consult the dream agent
  between writes. Outside a bracket there is no cross-call atomicity.
- **Read-your-writes** within one backend instance.
- **Concurrency.** Concurrent readers are safe; write concurrency is
  whatever the engine provides (SQLite: WAL + busy timeout). Mutual exclusion
  between maintenance runners is the store's job: the #17 dream-worker design
  arbitrates runners with store-side work claims (`claim_scope_work` and
  friends), which join the `OperationalStorage` contract when that work
  rebases over this layout — the reserved surface is documented in `base.py`.
  Until then, one maintenance runner per store at a time. Committed follow-on:
  Postgres implementations of the claim primitives (`FOR UPDATE SKIP LOCKED`
  or advisory locks) so raising the worker's `replicaCount` needs no worker
  code change.
- **Idempotent event appends.** `record_use_event`, `record_outcome_event`,
  and `record_live_artifact_outcome` are idempotent per
  `(scope_key, idempotency_key)`; replaying a *different* payload under a
  used key is an error.

## The rule that spans both stores

A logical transaction **anchors in the Operational Store** — the receipt (or
queue/job/event row) is the commit point and the source of truth for whether
an operation happened. Writes to the **Memory Graph are safe to repeat**:
every graph mutation in the contract is an idempotent upsert or property-set,
so recovery after a crash is re-driving graph writes from the anchored
operational record. Consequently there are **no foreign keys across the store
boundary**; referential integrity between operational rows and graph rows is
enforced by the write paths. Nothing cross-store is atomic; cross-store
operations (e.g. `purge_tenant_state`) are safe to re-run.

## The index the visible-in-context read depends on

`context_visible_relationships` is the default working-context tier and MUST
be served by an index (or equivalent access path) over
`(scope_key, status, context-visibility)`, so its cost is bounded by the
scope's context-visible set and decoupled from total store size. SQLite seeks
the `relationships_ctx_idx` generated-column index; Neo4j satisfies the same
clause with a relationship property index. `explain_context_visible_read`
returns engine-native plan evidence, and
`tests/test_storage_backend.py::test_context_visible_read_is_index_backed`
holds implementations to it. Evidence, search, and historical (`as_of`) reads
intentionally read the full store.

## Retrieval writes by default; a pure read is one flag away

Search (`search`, `search_context`, `semantic_search`) revives any archived row
whose prune ghost matches the query — inline, inside the read — emitting a
`PRUNING_GHOST_RESTORED` receipt, recording a dream decision, and returning the
revived row in the results. **That is still the default, unchanged.** Nothing a
caller written against it does behaves differently
(`tests/test_retention_safety.py::test_keyword_retrieval_restores_prune_ghost_with_receipt`).

The cost of that default is that a read is a writer: reviving takes row locks on
the memory graph, which contends — and serialises retrieval — as soon as more
than one replica serves reads against the same Operational Store. Deployments in
that position set `DreamConfig.pure_read_retrieval = True`, and retrieval becomes
a pure read: nothing is reactivated, and the scope's `graph_state_hash` is
unchanged across a search that matches archived rows
(`tests/test_retention_safety.py::test_keyword_retrieval_reports_prune_ghost_without_restoring_it`).
The trade is deliberate — with the flag on, an archived memory comes back only
when something explicitly asks for it.

Both modes **report** what they matched. A search returns a `SearchResults` — a
`list[SearchResult]` subclass, so every existing caller is unaffected — carrying
an additive `archived` report: a count plus the relationship uuid, scope, fact,
prune reason, and prune receipt of each archived row the query matched. Its
`disposition` field distinguishes the modes without ambiguity: `revived` (the
default — these rows were brought back by this read and are in `results` too) or
`available_to_restore` (pure read — these rows were left alone and are not in
`results`). `Memotron.archived_matches(query=..., scope=...)` returns the same
report on its own and is always a pure read.

Reviving on demand is an explicit curation action, available in **both** modes:
`Memotron.restore_archived_memory(relationship_uuid=..., scope=...)`, exposed
as `memory_restore` on the agent-memory MCP curation surface (next to
`memory_forget`) and `restore_archived_memory` on the operator MCP surface. It
emits the same receipt and the same dream decision the read path emits, keeps the
crypto-shred guard (a shredded ghost raises `ContentKeyUnavailableError` and is
durably demoted to non-restorable) and the "not restorable" refusal, and surfaces
both refusals to the caller instead of swallowing them as the read path does.
With `pure_read_retrieval` on it is the only way back: callers that relied on a
search reactivating an archived memory call it for the exact uuid reported in
`results.archived`, then read again. The flag lives above the storage layer, so
both modes hold identically on both engines
(`tests/test_storage_backend_parity.py::test_retrieval_archive_tier_behaviour_in_both_modes_on_both_engines`).

## Honest about graph engines

The Memory Graph surface is expressed in graph vocabulary only — nodes,
relationships, scoped reads. No rows, cursors, SQL fragments, or connections
cross the boundary (guarded by
`test_interface_admits_a_graph_engine_without_relational_vocabulary`), and
nothing in the surface requires engine-side joins or multi-entity ACID
transactions a graph engine cannot give: every mutation is a single node or
single relationship write. Conversely the Operational Store surface is free
to stay relational; a graph engine is never asked to host queues, ledgers, or
key tables.

## Backend selection by configuration

```python
from memotron import Memotron, EngineSettings, StorageSettings
from memotron.config import default_config

# Default — one SQLite store, what the test suite and simulation use:
client = Memotron(graph_path=".memotron/graph.sqlite")

# By configuration:
config = default_config().model_copy(update={
    "storage": StorageSettings(backend=EngineSettings(engine="sqlite", path="ops.sqlite")),
})
client = Memotron(config=config)

# The two stores on different engines at once:
config = default_config().model_copy(update={
    "storage": StorageSettings(
        operational_store=EngineSettings(engine="sqlite", path="operational.sqlite"),
        memory_graph=EngineSettings(engine="sqlite", path="memory-graph.sqlite"),
    ),
})
```

New engines register with the factory —
`register_storage_engine("neo4j", builder)` — and no caller changes. In a
split deployment the factory builds the Memory Graph plane first, hands the
Operational Store a reference to it (cross-plane operations read/write the
graph through the interface), and composes both behind `SplitStorageBackend`.

## Erasure

Crypto-shred destroys the wrapped per-scope DEK in the Operational Store;
sealed content everywhere becomes permanently unreadable with no graph
rewrite, and `graph_state_hash` is stable across the shred (it binds stored
write-time digests, never plaintext). Graph deletions for the erasure sweep
go through the idempotent `delete_nodes` / `delete_relationships` primitives.

## Acceptance guards

`tests/test_storage_backend.py` enforces the ticket's acceptance criteria:
no module outside `memotron/storage/` references SQLite; the SQLite
backend implements the full contract with nothing left abstract; the contract
covers the backend's entire public surface (so nothing can silently bypass
routing in a split deployment); selection is configuration-driven; and a
split deployment routes each surface to its store, anchors cross-plane writes
in the Operational Store, purges through the idempotent graph-plane
primitives, and refuses mis-wired planes at construction. `composite.py`
additionally proves at import time that the ownership maps plus explicit
overrides route every contract method exactly once.

**Full Postgres parity reached (2026-08-25).** `sqlite_only_pending_postgres_parity`
in that same test is now the **empty set**: every public SQLite method is on the
`StorageBackend` contract and implemented on `PostgresStorageBackend`. The whole
WS-16..26 surface was ported — the canonicalization registries (entity + predicate),
the WS-24 quarantine store, the WS-19 promotion-endorsement store, the WS-17 #17
dream-worker claim ledger (`claim_episodes`/`claim_scope_work`/`release_dream_claims`,
serialised by `exclusive_write_transaction` = a `pg_advisory_xact_lock`, the twin of
SQLite's `BEGIN IMMEDIATE`), the incremental `scope_state_hash_tracker`, the event
idempotency/lineage reads, the batch/truth-prefix relationship reads, and the
18-method WS-26 derivation-DAG epoch surface (`ensure_root_epoch`,
`active_epoch_for_scope`, `create_epoch`, `epoch(s)_for_scope`, `set_epoch_status`,
`set_epoch_pre_adopt_snapshot`, `set_active_epoch`, `record_epoch_run`,
`epoch_run_uuids`, `epoch_ancestry`, the `predicate_alias_row` lookup plus the
entity/predicate overlay/delete methods, and `registry_state_digest`). The WS-26
T2/T9 read-side epoch-ancestry filter (`graph_state_hash` +
`relationships_for_scope`/`active_relationships`/`context_visible_relationships`)
runs on Postgres too, with the state-hash memo invalidated on the HEAD pointer flip.
Formation and the full re-dream cycle (branch → adopt → rollback, byte-for-byte
`graph_state_hash` + `registry_state_digest` restore) run end-to-end against a
Postgres live store, proven by the backend parity suite and
`tests/test_redream_end_to_end.py::TestRedreamOnPostgresLiveStore`. Any NEW public
SQLite method not on the contract now fails the test loudly — a deliberate decision
to add a name, never a silent gap. Run the Postgres suites with a
`MEMOTRON_TEST_POSTGRES_DSN` pointed at a `LC_COLLATE=C` database.
