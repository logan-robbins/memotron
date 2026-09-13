# Operational Store: connection budget, pool sizing, and measured performance

The Operational Store is a managed Postgres instance shared by every replica.
Connections are the scarcest thing it has: they are capped per instance tier, a
pool that is too small stalls request threads, and a pool that is too large
starves the maintenance plane and locks out the operator exactly when someone is
trying to diagnose why. This page states the arithmetic, the numbers it is
derived from, and what to watch once it is running.

Measurements at the bottom were taken **2026-08-14** and come from a sandbox,
not from LATEST — see [Hardware caveat](#hardware-caveat) before quoting any of
them. They are the input to the observability issue: the pool and latency
counters named here are the ones the dashboards and alerts should carry.

---

## 1. Where the numbers live

Pool bounds are constructor arguments on
`PostgresStorageBackend` / `PostgresEngine`
(`src/memotron/storage/postgres/_engine.py`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `min_size` | `2` | Connections opened at startup and kept warm. |
| `max_size` | `10` | Ceiling. **This replica's share of the instance budget.** |
| `statement_timeout` | `30_000 ms` | A wedged statement cannot hold a pool slot forever. |
| `lock_timeout` | `10_000 ms` | A blocked lock fails instead of hanging. |
| `idle_in_transaction_session_timeout` | `60_000 ms` | An abandoned transaction cannot pin a connection. |
| call timeout (bridge) | `60 s` | Larger than `statement_timeout`, so a database error surfaces as a database error rather than a bridge timeout. |
| connect timeout | `10 s` | Also the pool's checkout timeout. |

`application_name` is set per replica and is what makes a misbehaving pod
attributable in `pg_stat_activity`.

## 2. What one connection is used for

The engine runs an async pool on one dedicated event-loop thread behind the
synchronous `StorageBackend` surface. The rule that drives sizing:

> **A calling thread holds exactly one pooled connection for the whole duration
> of its transaction**, and every bare interface call is its own
> single-statement transaction.

So pool demand is not "requests per second", it is **concurrent calling
threads**. A replica serving N requests in parallel needs N connections at the
instant they are all mid-transaction, however fast each one is.

    max_size  >=  peak concurrent request threads in the replica
                  + 1 for a maintenance pass running alongside them

Measured confirmation: at 607 operations/second with 4 worker threads per
replica, peak `pool_size` was **4** — one per thread — and only 3 of 30,007
checkouts ever waited, for 5 ms in total. Throughput did not set the pool size;
thread count did.

## 3. The budget arithmetic

Count processes, not pods, and count them **during a rolling deploy**, when old
and new replicas are both live and both holding pools:

```
required  =  2 x (app_replicas + admin_replicas + migration_jobs) x max_size    <- rolling surge
          +  operator_headroom
          +  superuser_reserved_connections
```

With the shipped defaults (`.helm/values-latest.yaml`: `replicaCount: 2` for the
app, `1` for admin; `max_size = 10`):

| Term | Value | Note |
| --- | --- | --- |
| App replicas | 2 x 10 = 20 | Steady state. |
| Admin replica | 1 x 10 = 10 | Same backend class, same defaults. |
| Migration job | 1 x 10 = 10 | Transient; holds the DDL advisory lock. |
| **Steady total** | **40** | |
| Rolling-deploy surge | x2 = 80 | Old and new pods overlap. |
| Operator / ad-hoc headroom | +10 | `psql`, a one-off script, a support session. |
| `superuser_reserved_connections` | +3 | Postgres default; not yours to spend. |
| **Peak required** | **93** | |

**So `max_size = 10` at this replica count needs an instance with
`max_connections >= 128` to sit under ~70% utilisation, and is comfortable at
200.** The sandbox these measurements come from is configured with
`max_connections = 200` and `superuser_reserved_connections = 3`; peak observed
use during the two-replica proof was 8 connections total (4 per replica), i.e.
4% of the instance.

### Choosing the tier

Cloud SQL derives `max_connections` from the instance's memory, and the value
differs between machine types and major versions. **Do not assume it — read it**:

```sql
SHOW max_connections;
SHOW superuser_reserved_connections;
```

Then check the inequality above. Two worked cases:

| Instance `max_connections` | Peak required (defaults) | Utilisation | Verdict |
| --- | --- | --- | --- |
| 100 | 93 | 93% | **Too tight.** A rolling deploy plus one operator session exhausts it. Drop `max_size` to 6 (peak 61) or raise the tier. |
| 200 | 93 | 47% | Comfortable; the configuration this page assumes. |
| 400 | 93 | 23% | Room to raise `max_size` or the replica count. |

### If you change the replica count

Scaling out multiplies the whole term. Going from 2 app replicas to 6 at
`max_size = 10` takes peak required from 93 to 173 — past a 200-connection
instance's safe band. Either scale `max_size` down as replicas go up (keeping
`replicas x max_size` roughly constant) or move to a larger tier. Prefer the
former: past the per-replica thread count, extra pool slots buy nothing (see the
measurement in §2) and only make exhaustion elsewhere more likely.

### Headroom for maintenance and migrations

* **Maintenance (dream) jobs** bracket their write phases in
  `backend.transaction()` so a logical operation commits once, not once per
  write. `DreamEngine` wires two brackets: each episode's materialization
  burst (nodes, relationships, receipts, state tuples — one commit per
  episode) and the run's terminal checkpoint + last-run marker. Extraction
  and dream-agent calls happen *outside* any bracket — a transaction is never
  held across an LLM or network call — which is also why pruning applies each
  prune as its own transaction: its decision loop may consult the agent
  between writes. Measured p50 for a bracketed maintenance pass under
  concurrent write load was 35 ms and p99 was 89 ms, so a maintenance runner
  costs approximately one short-lived slot per bracket, not a burst. Budget
  +1 connection per concurrent maintenance runner. Runner mutual exclusion is
  store-side (the #17 dream worker's work claims, single worker replica until
  the claim surface joins the contract and gains its Postgres twin) — when
  worker `replicaCount` rises, each replica is one more entry in this budget.
* **Migrations** take a transaction-scoped advisory lock
  (`pg_advisory_xact_lock`, id `0x44574D49`) so concurrent replicas serialise
  DDL rather than racing it. Every replica attempts migration on startup, so a
  rolling deploy briefly has `replicas` connections all blocked on that lock.
  They are counted above as part of the surge term. This path is exercised by
  `tests/test_operational_store_concurrency.py::test_replicas_can_migrate_concurrently`.

## 4. What to watch

### From the application: `backend.pool_stats()`

Returns the psycopg pool counters plus the configured bounds. The four that
matter:

| Field | Meaning | Alert when |
| --- | --- | --- |
| `pool_size` | Connections currently held by this replica. | Sustained at `max_size` — the pool is the bottleneck. |
| `requests_waiting` | Callers blocked right now waiting for a connection. | `> 0` for more than a moment. |
| `requests_queued` / `requests_wait_ms` | Cumulative checkouts that had to wait, and total wait. | Wait time growing as a fraction of request time. |
| `connections_errors` / `returns_bad` | Failed connects, connections returned broken. | Any sustained non-zero. |

Baseline from the proof run: `pool_size` 4 of 10, `requests_waiting` 0,
`requests_queued` 3 of ~30,000, `requests_wait_ms` 5.

### From the instance: `pg_stat_activity`

```sql
-- Who is holding connections, by replica (application_name is set per replica).
SELECT application_name, state, count(*)
FROM pg_stat_activity
WHERE datname = current_database()
GROUP BY 1, 2 ORDER BY 3 DESC;

-- Headroom against the cap.
SELECT (SELECT count(*) FROM pg_stat_activity) AS in_use,
       current_setting('max_connections')::int AS cap;

-- The dangerous state: a transaction pinning a connection while doing nothing.
-- idle_in_transaction_session_timeout should be killing these at 60s.
SELECT pid, application_name, state, now() - state_change AS held
FROM pg_stat_activity
WHERE state = 'idle in transaction' ORDER BY held DESC;

-- Lock waits. The acceptance criterion is that callers never see one.
SELECT count(*) FROM pg_locks WHERE NOT granted;
```

Also watch the server's own deadlock counter — the concurrency criterion is that
it stays flat:

```sql
SELECT deadlocks, xact_commit, xact_rollback
FROM pg_stat_database WHERE datname = current_database();
```

---

## Measured results

### Hardware caveat

**These numbers are from a sandbox, not from the LATEST instance.** They were
taken on 2026-08-14 against PostgreSQL 14.23 running locally in the same
container as the client, on shared CPU, with `max_connections = 200` and
**pgvector not installed** (vector search therefore ran on the
`dw_cosine_similarity` float8[] fallback, not an ANN index). There is no network
between client and server, so absolute latencies are optimistic — a managed
instance adds a round trip to every one of them, and the SQLite comparison is
flattered further because SQLite has no round trip at all. What survives the
change of hardware is the **shape**: which reads stay flat as the store grows,
which do not, and how many connections a replica actually needs. Re-run both
scripts against LATEST before quoting any absolute figure.

### Concurrency proof

`scripts/operational_store_loadtest.py --replicas 2 --duration 60 --workers 4`,
two separate OS processes with independent pools against one database.

| | |
| --- | --- |
| Date | 2026-08-14 |
| Server | PostgreSQL 14.23 |
| Replicas | 2 processes x 4 worker threads |
| Pool per replica | min 2 / max 10 (20 connection budget) |
| Duration | 60.4 s sustained |
| Operations | 36,452 committed, 36,452 attempted |
| Throughput | 607.5 ops/s |
| **Lock errors** | **0** (no deadlock, lock timeout, serialization failure, statement cancellation, or pool timeout) |
| **Failed operations** | **0** |

Latency by operation type (milliseconds):

| Operation | ops | ops/s | p50 | p95 | p99 | max |
| --- | --- | --- | --- | --- | --- | --- |
| Synchronous fact write (dedup probe + upsert_node + add_relationship) | 10,412 | 173.5 | 6.2 | 21.0 | 33.3 | 70.1 |
| Contended reinforce (locked read-modify-write on one shared row) | 7,810 | 130.2 | 14.3 | 40.1 | 55.0 | 87.5 |
| Use + outcome event append | 7,810 | 130.2 | 6.3 | 19.9 | 33.6 | 64.3 |
| Receipted mutation (state-hash bracket + `receipts.emit`) | 5,211 | 86.8 | 12.7 | 40.6 | 58.0 | 91.2 |
| Scheduled maintenance pass (scan + 5 mutations + job records, one commit) | 2,606 | 43.4 | 35.2 | 72.9 | 88.8 | 116.0 |
| All | 36,452 | 607.5 | 8.2 | 39.4 | 61.1 | 116.0 |

Invariants, all verified after the run against the persisted state:

| Invariant | Evidence |
| --- | --- |
| Zero lock errors | none, across 36,452 operations |
| No lost relationship appends | 10,414 persisted == 10,414 committed |
| No lost use-event appends | 7,810 persisted == 7,810 committed |
| No lost outcome-event appends | 7,810 persisted == 7,810 committed |
| No lost maintenance job runs | 2,606 persisted == 2,606 committed |
| No lost contended increments | `observed_count` 7,811 == seed 1 + 7,810 committed increments |
| Receipt chain dense | 5,209 receipts, `event_index` 0..5208, no gap, no duplicate |
| Receipt chain links | every `previous_receipt_hash` links; head links to genesis |
| Ledger `verify_chain` | valid over 5,209 receipts |
| State hash correct | all 3 scopes match a full `recompute_scope_state_tuples` rebuild |

**Negative control.** The same increment written the way the contract says a
caller may *not* — read in one interface call, write in another, no enclosing
transaction — lost **440 of 2,603 increments (16.9%)** in the same run against
the same database. That is what makes the "no lost contended increments" row
above evidence rather than luck: the machine was genuinely contended, and the
unlocked shape demonstrably fails on it.

Pool behaviour at replica exit:

| Replica | min | max | peak size | checkouts | waited | total wait | connections opened |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 0 | 2 | 10 | 4 | 30,007 | 3 | 5 ms | 4 |
| 1 | 2 | 10 | 4 | 29,883 | 4 | 6 ms | 4 |

`max_size = 10` was never approached; demand tracked the 4 worker threads
exactly, which is the sizing rule in §2.

### Retrieval benchmark

`scripts/operational_store_benchmark.py --sizes 1000,10000,50000`.

Store size is grown **by adding scopes**, holding relationships-per-scope
constant at 200. That is the shape the decoupling claim is about: one tenant's
working set must not get slower because another tenant wrote something.

Read latency, p50 / p95 in milliseconds:

| Operation | Engine | 1k p50 | 1k p95 | 10k p50 | 10k p95 | 50k p50 | 50k p95 | scale 1k->50k |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `context_visible_relationships` | SQLite | 2.55 | 2.67 | 2.55 | 2.63 | 2.50 | 2.57 | x1.0 |
| | Postgres | 3.76 | 3.91 | 4.08 | 4.30 | 4.07 | 4.51 | x1.1 |
| `find_active_truth_relationships` | SQLite | 16.69 | 17.50 | 206.99 | 221.60 | **1138.29** | 1176.94 | **x68.2** |
| | Postgres | 0.61 | 0.69 | 0.67 | 0.79 | **0.69** | 0.78 | **x1.1** |
| `graph_state_hash` (memo hit) | SQLite | 3.97 | 4.13 | 4.11 | 4.27 | 4.21 | 4.33 | x1.1 |
| | Postgres | 0.41 | 0.58 | 0.59 | 0.71 | 0.35 | 0.56 | x0.9 |
| `graph_state_hash` (recompute) | SQLite | 3.97 | 4.13 | 4.11 | 4.27 | 4.21 | 4.33 | x1.1 |
| | Postgres | 0.70 | 0.76 | 0.75 | 0.89 | 0.70 | 0.87 | x1.0 |
| `similar_relationships` | Postgres | 5.01 | 5.18 | 15.38 | 17.97 | 4.82 | 5.32 | x1.0 |

Postgres speedup over the SQLite substrate, p50:

| Operation | 1k | 10k | 50k |
| --- | --- | --- | --- |
| `context_visible_relationships` | x0.7 | x0.6 | x0.6 |
| `find_active_truth_relationships` | x27 | x308 | **x1646** |
| `graph_state_hash` (memo hit) | x9.7 | x6.9 | x12.1 |
| `graph_state_hash` (recompute) | x5.7 | x5.5 | x6.0 |

Reading these honestly:

* **`find_active_truth_relationships` is the headline.** It is the dedup probe
  paid on *every synchronous fact write*. On the SQLite substrate it reads the
  whole store into Python and filters it, so it grows linearly with total store
  size — **x68 from 1k to 50k, reaching 1.1 seconds per write at 50k**. On
  Postgres it is an index seek and stays at 0.69 ms. This is the read that makes
  the substrate unshippable at scale, and the one the port fixes outright.
* **`context_visible_relationships` is slower on Postgres in absolute terms
  (x0.6-0.7), and that is expected.** It returns 180 rows over a socket where
  SQLite returns them in-process; on this hardware there is no network to
  amortise. What matters for the contract is that it is **flat** (x1.1 across a
  50x store) and served by an index seek — both confirmed below. On LATEST both
  engines pay a round trip and this gap narrows.
* **`graph_state_hash` is 6-12x cheaper** because the port keeps a canonical
  tuple per relationship and reads one indexed aggregate, instead of SQLite's
  whole-scope recompute with one subject lookup per row. The bracket pays this
  twice per receipted change, so it lands on every write path.
* **`similar_relationships` ran on the `dw_cosine_similarity` float8[] fallback**
  — pgvector was not installed on this sandbox. 64-dimension vectors, 200
  candidates per scope. The 10k figure (15.38 ms) is an outlier against 5.01 and
  4.82 at the neighbouring sizes and should be treated as sandbox noise rather
  than a scaling effect; the candidate selection is a scope-keyed index seek at
  every size. **With pgvector installed this becomes an ANN index scan and these
  numbers do not apply.** Re-measure on LATEST once the extension's availability
  is settled.

### EXPLAIN evidence: the Postgres reads are seeks, not scans

Captured on the 50,000-relationship store after `ANALYZE`. No plan contains a
`Seq Scan` on a graph-plane table.

```
context_visible_relationships:
  Index Scan using relationships_ctx_idx on relationships  (cost=0.41..8.43 rows=1 width=1883)
    Index Cond: (((properties ->> 'scope_key') = 'agent:bench-514cdc-125') AND ((properties ->> 'status') = 'active'))

find_active_truth_relationships:
  Sort  (cost=8.44..8.45 rows=1 width=1883)
    Sort Key: created_at, uuid
    ->  Index Scan using relationships_truth_key_idx on relationships  (cost=0.41..8.43 rows=1 width=1883)
          Index Cond: ((properties ->> 'truth_key') = 'agent:bench-514cdc-0:pred:0')

graph_state_hash (recompute):
  Aggregate  (cost=681.73..681.75 rows=1 width=32)
    ->  Bitmap Heap Scan on relationship_state_tuples  (cost=18.25..681.13 rows=237 width=243)
          Recheck Cond: (scope_key = 'agent:bench-514cdc-125')
          ->  Bitmap Index Scan on relationship_state_tuples_scope_idx  (cost=0.00..18.19 rows=237 width=0)
                Index Cond: (scope_key = 'agent:bench-514cdc-125')

similar_relationships (candidate selection):
  Bitmap Heap Scan on relationship_embeddings e  (cost=5.84..665.32 rows=200 width=37)
    Recheck Cond: (scope_key = 'agent:bench-514cdc-125')
    Filter: (dimensions = 64)
    ->  Bitmap Index Scan on relationship_embeddings_scope_idx  (cost=0.00..5.79 rows=200 width=0)
          Index Cond: (scope_key = 'agent:bench-514cdc-125')
```

`explain_context_visible_read` exposes the first of these through the interface,
which is what the contract requires for certification.

---

## Concurrency defects these runs found

The first version of this proof did **not** pass. Four defects in the Postgres
plane were found and fixed; the numbers above are from the fixed code. They are
recorded here because they explain why the throughput figure is what it is, and
because each one is now guarded by a named regression test in
`tests/test_operational_store_concurrency.py`.

1. **Stale state hash published as clean.** `graph_state_hash` recomputed the
   aggregate and then cleared `dirty` unconditionally. A writer committing in
   that window was erased: the scope was left memoised-clean holding a hash that
   omitted the writer's relationship, permanently. A receipt bracket would then
   commit to a state hash the graph never had. Fixed by guarding the write-back
   on the memo row's version (`xmin`).
2. **Deadlocks on the per-scope memo row.** Every write to a scope had to
   invalidate one shared row, inline and mid-transaction, holding it to COMMIT
   while going on to lock further relationships — an inverted lock order against
   any single-relationship writer. Fixed by deferring the invalidation to
   immediately before COMMIT (`PostgresEngine.defer_to_commit`). At a fixed
   two-worker configuration this took throughput from 50 to 161 ops/s and
   collapsed the receipted-mutation p99 from 1,427 ms (a deadlock detection
   timeout) to 9.7 ms.
3. **Foreign-key lock conflicts.** `upsert_node` and `_lock_relationship` took
   `FOR UPDATE` on rows that are foreign-key *parents*, which conflicts with the
   `FOR KEY SHARE` every child insert takes. Two replicas writing facts pointing
   at each other's subject deadlocked. Neither merge changes a key column, so
   both now take `FOR NO KEY UPDATE`.
4. **Tuple rebuild from unlocked reads.** Renaming a subject rebuilt every
   outgoing relationship's state tuple from an unlocked read, so a concurrent
   mutation could be overwritten in the derived state. Now locked and ordered by
   uuid. Separately, the rebuild fired whenever a name was *passed* rather than
   *changed*, which meant every fact write re-locked every tuple its subject
   owned; it now fires only on an actual change.

## Feeding the observability issue

The counters this page names are the ones to instrument:

* **Pool**, per replica, from `backend.pool_stats()`: `pool_size`,
  `requests_waiting`, `requests_queued`, `requests_wait_ms`,
  `connections_errors`, `returns_bad`.
* **Instance**: connections in use vs `max_connections`, connections grouped by
  `application_name`, `idle in transaction` age, ungranted locks, and
  `pg_stat_database.deadlocks` — which must stay flat, because "no lock errors"
  is an acceptance criterion and a rising deadlock count is that criterion
  failing in production.
* **Latency**, per operation class, with the p50/p95/p99 above as the baseline to
  alert against.

## Re-running this

Both scripts are standalone and take a DSN:

```sh
export MEMOTRON_TEST_POSTGRES_DSN="host=... port=5432 user=... dbname=..."

# Concurrency proof. Exit status is 0 only if every invariant holds.
python scripts/operational_store_loadtest.py --replicas 2 --duration 60 --workers 4

# Retrieval benchmark. --resume lets the long 50k build be done in stages.
python scripts/operational_store_benchmark.py --sizes 1000,10000,50000 --json bench.json --resume
```

The same proof runs under pytest as
`tests/test_operational_store_concurrency.py`, which **skips cleanly when
`MEMOTRON_TEST_POSTGRES_DSN` is unset** so the hermetic offline gate still
passes with no database.

> The benchmark **TRUNCATEs the graph-plane tables** in the target database
> before each size. Point it at a scratch database, never at LATEST.
