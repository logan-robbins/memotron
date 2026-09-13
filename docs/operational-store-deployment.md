# Operational Store: deployment, credentials, migrations, and rollback

How the Postgres Operational Store is wired into a Memotron environment.
Sizing and the connection budget are a separate page —
[`operational-store-sizing.md`](operational-store-sizing.md) — and this page
assumes its arithmetic when it names pool defaults.

Everything here is templated per environment. LATEST is the configured case
(`.helm/values-latest.yaml`); another environment turns it on by adding the
same `operationalStore` block with its own Vault path.

---

## 1. Which Vault path holds what

One KV path per environment — the environment's single Memotron secret,
shared with `LITELLM_API_KEY` (there is no separate `/operational-store`
sub-secret; CSM provisions exactly one secret per env). The credential lives
in it as parts. LATEST:

```
secret/apps/jedai/memotron/us-east-1/latest
```

| Key | Example | Notes |
| --- | --- | --- |
| `host` | `10.x.x.x` | Private IP or DNS name of the managed instance. |
| `port` | `5432` | |
| `database` | `memotron` | Created with `LC_COLLATE='C'` — the engine warns on a non-byte-ordered collation because text ordering would diverge from the SQLite substrate. |
| `user` | `memotron_app` | The application role. Not the owner, not a superuser. |
| `password` | *(secret)* | Rotated on the schedule in §4. |
| `sslmode` | `require` | `verify-full` only once the server CA is mounted; libpq cannot verify without it. |

No value from this payload exists anywhere in the repository. The chart
declares the *path* and the *key names*
(`.helm/values-latest.yaml` → `operationalStore.vaultSecret`) and nothing else.

Two Vault-side prerequisites, both shared with the existing LLM-credential
secret and both applied out of band:

* The AppRole that `VaultAuth` authenticates with — `vaultSecret.approleRoleId`
  is a `REPLACE_ME_WITH_APPROLE_ROLE_ID` placeholder in the tracked values and
  must be a real role id, with its secret id in the `memotron-vault-approle`
  Secret, before a deploy that has `operationalStore.vaultSecret.enabled: true`
  can sync anything.
* The Vault policy on the AppRole needs read on the path above.

AppRole auth needs no cluster-side token-review objects. The former
`.helm/vault/` manifests (a service-account-token Secret and a
`system:auth-delegator` ClusterRoleBinding) were kubernetes-auth leftovers
that hardcoded namespace `memotron` and were never applied by the chart or
the pipeline; they have been deleted.

## 2. How the credential reaches the process

```
Vault KV path
   │   VaultStaticSecret (.helm/templates/secret-operational-store.yaml)
   ▼
Secret  memotron-operational-store        (created + owned by the operator)
   │   secretKeyRef, one env var per part
   ▼
Pod env MEMOTRON_OPERATIONAL_STORE_{HOST,PORT,DATABASE,USER,PASSWORD,SSLMODE}
   │   kubelet $(VAR) expansion at container start
   ▼
MEMOTRON_OPERATIONAL_STORE_DSN
   │   config.storage_settings_from_env()
   ▼
StorageSettings(backend=EngineSettings(engine="postgres", url=DSN, options={...}))
```

The DSN is **assembled in the pod from the parts**, not stored whole. The
`env` entry for it is the literal string
`host='$(MEMOTRON_OPERATIONAL_STORE_HOST)' … password='$(MEMOTRON_OPERATIONAL_STORE_PASSWORD)' …`;
the kubelet resolves those references against the entries declared earlier in
the same container's `env` list. Consequences worth stating plainly:

* The password is never in a ConfigMap. `environmentVariables` (the ConfigMap
  source) carries only plain settings.
* `kubectl describe pod` prints the unresolved `$(VAR)` template for the DSN
  and `<set to the key 'password' in secret ...>` for the part — never the
  value.
* The password is never in the rendered chart. `helm template` output contains
  key *references* only.
* The `secretKeyRef`s are required, not `optional`. A pod with no credentials
  stays in `CreateContainerConfigError` instead of starting with a broken DSN.

`MEMOTRON_OPERATIONAL_STORE_APPLICATION_NAME` is the pod name via
`fieldRef: metadata.name`, which is what makes a replica attributable in
`pg_stat_activity` (sizing doc §4).

`MEMOTRON_GRAPH_PATH` stays set to `:memory:` and is simply unused when a
DSN is present: `Memotron` prefers `config.storage` over `graph_path`.

## 3. Selecting the engine, and keeping SQLite the default

`memotron.config.storage_settings_from_env()` reads the environment:

| Env var | Default | Effect |
| --- | --- | --- |
| `MEMOTRON_OPERATIONAL_STORE_DSN` | unset | **Unset or blank → returns `None` → SQLite.** Set → `engine="postgres"` with this `url`. |
| `MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE` | `2` | `min_size` on the pool. |
| `MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE` | `10` | `max_size` — this replica's share of the instance budget. |
| `MEMOTRON_OPERATIONAL_STORE_APPLICATION_NAME` | engine default | `application_name` in `pg_stat_activity`. |

It is called from `default_config()` and from
`memotron.mcp_server.build_client()` (the LATEST entry point). With the DSN
absent it returns `None`, `DreamConfig.storage` stays `None`, and the client
falls back to the local SQLite backend at `graph_path` — so the local
substrate, the simulation, and the hermetic test suite are unchanged and need
no database.

Pool bounds are Helm values, not code:
`operationalStore.pool.minSize` / `maxSize` in `.helm/values-latest.yaml`,
defaulting to `2` / `10` to match the sizing doc. Changing the replica count
means re-checking `2 x (app + admin + migration) x max_size + headroom`
against the instance's `max_connections` — see sizing §3. Admin is deliberately
excluded (`operationalStore.injectAdmin: false`): it reads the demo SQLite
graph off the PVC and holds no pool.

## 4. Rotating the password

The application role's password is the only rotating part; the path, keys, and
all Helm wiring stay put.

1. Create the new password on the instance:
   `ALTER ROLE memotron_app PASSWORD '<new>';`
   Use an alphanumeric alphabet — the DSN is libpq keyword/value format, so a
   password containing a single quote or a backslash needs escaping and will
   otherwise produce an invalid DSN.
2. Write it to the Vault path, updating only the `password` key.
3. Wait for the sync. `refreshAfter: 10m` bounds it; force it sooner by
   deleting the `VaultStaticSecret`'s destination Secret, or by
   `kubectl annotate vaultstaticsecret jedai-memotron-operational-store
   vso.hashicorp.com/force-sync="$(date +%s)" --overwrite`.
4. The operator's `rolloutRestartTargets` restarts `jedai-memotron-api` when
   the Secret content changes, so replicas pick up the new password on a normal
   rolling restart. Nothing re-reads the env in place — the restart is the
   mechanism.
5. Confirm with `pg_stat_activity` that every `application_name` is a
   post-restart pod name, then retire the old password if you staged two.

Rotating the AppRole secret id is the same shape but affects every synced
secret in the namespace; do it separately from a database rotation.

## 5. Migrations on deploy, and two replicas starting at once

There is no migration Job and no init container. `PostgresStorageBackend`
migrates on construction (`migrate=True` by default), so **every replica
attempts the migration during startup**. That is safe, and the reason is
`PostgresEngine.migrate` in `src/memotron/storage/postgres/_engine.py`:

```python
with self.transaction():
    self.execute("SELECT pg_advisory_xact_lock(%s)", (self._MIGRATION_LOCK_ID,))
    self.execute("CREATE TABLE IF NOT EXISTS schema_migrations (...)")
    rows = self.fetchall("SELECT version FROM schema_migrations")
    done = {int(row["version"]) for row in rows}
    for version, name, sql in migrations:
        if version in done:
            continue
        ...
```

Read in order, that is the whole concurrency argument:

* `transaction()` checks out **one** pooled connection, `BEGIN`s on it, and
  every `execute`/`fetchall` inside the block joins that same connection — so
  the lock and the DDL are in one transaction on one session.
* `pg_advisory_xact_lock` is transaction-scoped and exclusive. It is taken
  *first*, before any DDL. The second replica blocks on it rather than racing
  `CREATE` statements.
* The lock id is fixed (`_MIGRATION_LOCK_ID = 0x44_57_4D_49`, "DWMI"), so every
  replica of every process contends on the same lock.
* The `schema_migrations` read happens **after** the lock is acquired, so the
  waiter's view of what is already applied is the winner's committed state. It
  skips those versions and applies nothing twice.
* The lock releases at COMMIT or ROLLBACK — no explicit unlock, so a replica
  that crashes mid-migration cannot leave it held; the transaction aborts and
  the DDL rolls back with it.

So a rolling deploy of two replicas produces: one migrating, one blocked for
the duration, then both running, with nothing applied twice. The cost is
`replicas` connections held for that window, which the sizing doc counts in the
surge term. The behaviour is covered by
`tests/test_operational_store_concurrency.py::test_replicas_can_migrate_concurrently`,
which starts four backends against one database at once and asserts none of
them fails and all agree on the schema version (it skips cleanly with no
`MEMOTRON_TEST_POSTGRES_DSN`).

One caveat the code makes real, so state it rather than round it off to "safe":
**the wait is bounded.** `PostgresEngine._open_pool` configures every pooled
connection with `SET lock_timeout = 10000`, and a replica parked on
`pg_advisory_xact_lock` is parked inside that statement. If the winner's
migration runs longer than 10 s, the waiter's statement is cancelled, its
transaction rolls back, `PostgresStorageBackend.__init__` raises, and the pod
restarts — then succeeds on the retry, because by then the migration is
recorded and there is nothing to apply. That is still safe (no partial DDL, no
double apply, the winner is unaffected), but it shows up as a crash-and-restart
on the other replicas rather than a clean wait. **Run any migration expected to
take more than a few seconds against the instance ahead of the deploy**, so the
deploy itself has nothing to apply.

Because the whole migration is one transaction, a failure leaves the schema at
the previous version rather than half-applied.

## 6. Rollback

The store is stateful, so "roll back" means two different things; be explicit
about which one you are doing.

**Rolling back the application.** `helm rollback` (or redeploying the previous
image tag) reverts the code. Migrations are **not** reversed — there is no down
path in `_migrations.py` and none is wanted. This is safe as long as the
schema is additive, which is the rule the migration list follows: a new version
adds tables, columns, or indexes and does not drop or retype anything the
previous release reads. An older replica therefore keeps working against a
newer schema. A migration that would break that rule has to ship as two
releases — add and backfill first, remove one release later.

**Rolling back off Postgres entirely.** Set `operationalStore.enabled: false`
and redeploy. The env injection disappears, `storage_settings_from_env()`
returns `None`, and the replicas come back on the SQLite path at
`MEMOTRON_GRAPH_PATH`. This is a real fallback for an unhealthy instance,
but understand what it costs: **the data written to Postgres is not migrated
back**, and API replicas run `:memory:`, so the fallback is an empty store, not
the previous one. Treat it as "keep serving while the instance is repaired",
never as a data-preserving rollback. Leaving
`operationalStore.vaultSecret.enabled: true` while `enabled` is false keeps the
credentials synced and ready for the way back.

**Cutting off a single bad replica** does not need either: delete the pod. The
advisory lock is transaction-scoped and the pool closes with the process, so a
killed replica leaves nothing held.

---

## Checklist for enabling a new environment

1. Instance provisioned; database created with `LC_COLLATE='C'`; application
   role created. Read `SHOW max_connections;` and check it against sizing §3
   for your replica count and `maxSize`.
2. KV payload written to `secret/apps/jedai/memotron/<region>/<env>/operational-store`
   with the six keys in §1.
3. AppRole policy grants read on that path; `vaultSecret.approleRoleId` and the
   `memotron-vault-approle` Secret are real.
4. `operationalStore` block added to `values-<env>.yaml` with that path.
5. `helm template <env> ./.helm -f .helm/values-<env>.yaml | grep -A3 OPERATIONAL_STORE_PASSWORD`
   — confirm you see a `secretKeyRef`, never a value.
6. Deploy. Watch the first replica log `applying Operational Store migration`
   and the second start without one.
