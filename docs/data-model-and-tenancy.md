# The data model, and how multi-tenancy actually works

What a row is bound to, what isolates one tenant from another, and what does not.

> **Read the last line of §5 before trusting anything else here.** Isolation in Memotron is
> application-level filtering on a string inside a JSON document. There is no row-level
> security, no schema-per-tenant, no per-tenant database role. If a `WHERE` clause is omitted,
> the database returns every tenant's rows and reports success.

**Provenance and staleness.** Every load-bearing claim below carries a `file:line`, so you can
check it rather than trust it. Schema claims are read from the migration files, **not** from a
live database — a deployed store can drift from its migrations. Nothing re-derives this page:
the Python examples are checked by `scripts/verify/doc_symbols.py`, but the prose and the line
numbers are not. Measured 2026-09-03.

---

## 1. The vocabulary

| term | what it is | where |
|---|---|---|
| `ScopeKind` | `AGENT` \| `TENANT` \| `USER` \| `CUSTOMER`. Four **flat, unrelated** namespaces — there is no hierarchy. `AGENT` is not "inside" `TENANT`. | `models/_enums.py:15` |
| `MemoryScope` | `(kind, scope_id)` and **nothing else** | `models/_graph.py:22` |
| `scope_key` | the rendered `f"{kind}:{scope_id}"` — e.g. `tenant:acme`, `agent:alpha` | `models/_graph.py:34` |
| `tenant_id` | a bare normalised string. A column on *configuration* tables only | `sqlite/__init__.py:293` |
| `agent_id` | normalised ASCII, 1–64 chars, **tenant-local** | `identity.py:11` |
| `MemoryPrincipal` | `principal_id`, `tenant_id`, `agent_id`, `default_scope`, `allowed_scope_keys`, `role` | `config/_tenancy.py:109` |
| `PrincipalRole` | `USER` \| `OPERATOR` \| `ADMIN` | `config/_tenancy.py:103` |

**The thing most likely to mislead you: `MemoryScope` has no `tenant_id` field.** A tenant
exists in the graph only as a scope — `MemoryScope(kind=ScopeKind.TENANT, scope_id=<tenant_id>)`,
whose key is `f"tenant:{tenant_id}"`. That identity is a convention constructed by hand in at
least five places, including storage itself:

```python
from memotron import MemoryScope, ScopeKind

scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="acme")
assert scope.key == "tenant:acme"
```

`agent_memory/_scopes.py:24`, `sqlite/_governance.py:1045`, `postgres/_governance.py:1424`,
`admin_server/__init__.py:659`.

### Which one is the isolation boundary?

**`scope_key`. Unambiguously.** Every memory row, receipt, use-event, artifact and governance
key is addressed by it; every read filters on it; every authorization check compares a
`scope.key` against a set of scope keys.

`tenant_id` is **policy selection** — it picks a `TenantMemoryPolicy` out of
`MemoryControlPlane.tenants` (`config/_control_plane.py:73`). **It never reaches a WHERE clause
on a memory table.** There is no stored fact saying `agent:alpha` belongs to tenant `acme`; the
two are peers in the same tables. The only thing linking them is a validation in
`_resolve_scope` (`config/_control_plane.py:331`), and on two of three surfaces that validation
never runs (§6).

---

## 2. What a row is bound to

`scope_key` is a **plain JSON property** inside the row's `properties` document, written at
materialisation (`dreaming/_materialization.py:175, 343, 357, 881`). Node identity embeds it
too, so the same entity name in two scopes is two nodes:
`key = f"{scope_key}:{label}:{name}"` (`dreaming/_identity.py:157`).

The two engines expose that property to the planner differently — same access path, different
mechanism:

| | SQLite | Postgres |
|---|---|---|
| mechanism | VIRTUAL **generated columns** `gen_scope_key`, `gen_status`, `gen_context_visible` (`sqlite/_migrations.py:667`) | **expression indexes** over `jsonb`; no generated columns at all (`postgres/_migrations.py:60`) |
| the index | `relationships_ctx_idx(gen_scope_key, gen_status, gen_context_visible)` | `relationships_ctx_idx((properties->>'scope_key'), (properties->>'status'), created_at, uuid)` **PARTIAL** `WHERE (properties->'active_in_context') IS DISTINCT FROM 'false'::jsonb` |
| visibility | an index **key** | the index **predicate** |

`IS DISTINCT FROM` rather than `=` preserves the SQLite semantic that a memory is
context-visible when the flag is true *or absent* (`postgres/_migrations.py:22`).

**A caveat that matters.** Not every read is a scoped query. `active_relationships`
(`sqlite/_graph.py:225`) and `erasure.py:234` iterate the **whole store** and compare
`properties.get("scope_key") != scope.key` in Python. The filter is correct, but it is a Python
`!=` at a call site rather than a predicate the database enforces — so isolation there depends
on the comparison being present everywhere it is needed.

---

## 3. What enforces isolation

Two mechanisms exist. Both are real. Understand what each does *not* cover.

### `authorized_scope_keys` — the core guard

Constructor parameter on `Memotron` (`client/__init__.py:478`), normalised at `:534`.
`None` — the default — means **no guard**, byte-for-byte legacy behaviour.
`_require_authorized_scope` (`:644`) raises for any scope outside the set;
`_require_explicit_authorized_scope` (`:665`) additionally refuses `scope=None` on the four
operations where `None` means *every scope in the store*. **77 call sites** across `client/`.

**It is enabled on zero production surfaces.** No constructor call in `src/` passes it.

### `MemoryControlPlane` — per-principal resolution

`config/_control_plane.py:41`. Resolves tenant → agent → scope and layers policy, recording
provenance in `source_trace`. It enforces two things:

1. `_resolve_scope` (`:331`) — the scope must be in `tenant.registered_scope_keys()`. **This is
   the only place a tenant→scope association is checked anywhere.**
2. `resolve_for_principal` (`:280`) — the resolved scope must be in the principal's
   `effective_allowed_scope_keys()`.

**`PrincipalRole.ADMIN` skips check 2 entirely** (`:297`). ADMIN widens; it does not scope.
And `PrincipalRole.OPERATOR` is defined and **never compared anywhere in `src/`** — an operator
authorises exactly as a user.

### Why the agent-memory facade deliberately leaves the core guard off

`tests/test_promotion.py:617` asserts `platform.client.authorized_scope_keys is None`, and that
is correct, not an oversight. `AgentMemoryPlatform` is one long-lived client serving N agents;
`authorized_scope_keys` is a constructor-time frozen set, while `agent_ids` **grows at runtime**
(`agent_memory/_registry.py:88`). A constructor-time set could never track it. Authorisation is
instead per call — `_authorize` (`_registry.py:265`) builds the caller's principal and resolves
it — plus a row-level gate `_authorize_row` (`:270`) that asserts the row's `scope_key` matches
before any uuid-addressed mutation. Setting the core guard at that boundary fails 35 tests.

The two compose for embedded single-tenant use: `tests/test_promotion.py:628` pairs a control
plane with `authorized_scope_keys={"agent:alpha"}` and proves the tenant scope is refused.

---

## 4. Per-tenant credentials

Envelope encryption, two levels.

**KEK** — `crypto.py:173` defines a KMS-shaped `KeyManager`. `LocalKeyManager` holds a 256-bit
KEK and wraps DEKs with AES-256-GCM (`:233`). Key material lives **outside the database**:
`from_file` uses `<db_path>.kek` mode 0600 (`sqlite/__init__.py:140`); `ephemeral()` is
`secrets.token_bytes(32)` that dies with the process.

**DEK** — `get_or_create_governance_key(scope_key, subject_key)` mints a random 256-bit DEK,
stores it **only wrapped** in `governance_keys` PK `(scope_key, subject_key)`. On Postgres the
insert *is* the election — `ON CONFLICT … DO NOTHING RETURNING` (`postgres/_governance.py:271`)
— and the losing replica re-reads the winner's row and discards its own. Without that, two
replicas would each generate a DEK, one row would survive, and everything sealed under the
loser would be silently unreadable forever.

A tenant's LLM key is sealed under a DEK for the scope `tenant:<tenant_id>`, subject
`llm_credentials` (`sqlite/_governance.py:889`) — the same table and mechanism as content
protection. **So crypto-shredding a tenant scope also destroys its LLM credential.**

`MEMOTRON_ALLOW_EPHEMERAL_KEK` — Postgres **refuses to start** without a durable key manager
unless this is set (`postgres/__init__.py:126`), because an ephemeral KEK would seal content no
other replica, and not even the same process after a restart, could open. **SQLite has no such
guard** and falls back to `ephemeral()` silently — and the deployed MCP server runs `:memory:`.

**Resolution is per request and deliberately not cached** (`client/_runtime.py:235`). A
credential is per-graph while a cache is per-client-object, so a cache let a second client serve
a rotated-away key indefinitely. Measured saving of the cache before it was deleted: **0.018 ms**
on a path that then makes a ~100 ms network call. Deleting the row *is* the revocation, for
every client and process.

---

## 5. Identity: `key_alias` → principal

The gateway can tell us `key_alias`, `team_id`, `user_id` — it cannot tell us a tenant, role or
scopes, because those are ours. The **per-key registry** is the mapping that closes that gap.

`key_principals`, PK **`key_alias`** — so global uniqueness is enforced by the database, which
is the whole of DW-030. `tenant_agents` could not host this: its uniqueness is
`(tenant_id, agent_id_key)`, so two tenants could hold one alias and the lookup would be
ambiguous. Contract at `storage/base.py`; `principal_for_key_alias` takes **no tenant**, because
at authentication time the tenant is exactly what is unknown.

> **Status: the registry is written by a CLI and read by both MCP entry points (2026-09-08).**
>
> **Writing — done (#168).** `memotron key bind|show|unbind` is the operator write path
> (`key_registry_cli.py`). Before it, `bind_key_principal` had zero callers outside its own test,
> so the table would have stayed empty however well the read path was wired, and an empty registry
> is indistinguishable from a broken one. It is a **CLI, not an endpoint**, deliberately: the
> obvious home would be `admin_server`, which authenticates nobody (§6), so a route that binds
> aliases to tenants would let any caller bind its own key to any tenant. Authorization is
> possession of database credentials — whoever can run it can already write these rows by hand.
>
> **Reading — wired on MCP (#126 / #137).** The chain is complete and every link is in `src/`:
>
> ```
> raw key --> key_alias          gateway_identity.py (resolve_gateway_identity)
> key_alias --> key_principals   storage principal_for_key_alias  (takes NO tenant)
> row --> MemoryPrincipal        config/_tenancy.py:143 principal_from_registry_row
> principal --> authorized       mcp_auth.py:524, then the scope guard
> ```
>
> The lookup is a constructor seam (`mcp_auth.py:423`), and **both** MCP entry points bind it to
> the registry — `examples/mcp_server.py:115` and `examples/agent_memory_mcp_server.py:66`.
>
> The `default_scope_key` sharp edge is handled: the registry stores it as a **string** while
> `MemoryPrincipal` needs a `MemoryScope`, and a malformed stored value **fails closed** rather
> than degrading to "no default scope", which would widen access instead of denying it
> (`config/_tenancy.py:157`).
>
> **Two limits.** The `admin_server` HTTP surface is *not* on this path — `principal_lookup` has
> exactly two call sites, both above (§6). And the whole chain is inert unless
> `MEMOTRON_REQUIRE_GATEWAY_IDENTITY` is set; it is armed on `latest` only.

### What a gateway key means

**A gateway key is an identity, not a password.** The `key_principals` row is what turns it into
one, and everything downstream — tenant, role, allowed scopes — is read from that row rather than
asserted by the caller.

The consequence is worth stating plainly, because "personal" invites a stronger reading than the
system offers:

> **Anything written to personal scope belongs to the key, not to the person. Share the key and
> you share that memory.**

That is the *decided* model (#206 Phase 3) and it is **not what runs today**. Today
`default_user_id()` (`agent_memory/_scopes.py:39`) hashes the local OS account, so the user scope
follows whichever machine runs the SDK — on a shared deployment every caller lands in the same
`user:local-<hash>` scope. Do not document or rely on key-derived personal memory until Phase 3
lands; the intermediate state is worse than the target, not merely less specific.

### And the honest bottom line

**There is no database-level isolation of any kind.** Verified by grep across `src/`, `.helm/`
and `docs/` for `ROW LEVEL SECURITY`, `CREATE POLICY`, `FORCE RLS`, `SET ROLE` — zero hits. One
shared table set for every tenant, one connection, one database role, and a discriminator that
is a JSON property rather than a column. **Isolation is 100% application-level filtering.**

---

## 6. Where tenancy is NOT enforced today

On the two deployed surfaces, tenancy is not enforced at all, **because neither authenticates
anybody.** Grepping `admin_server/`, `mcp_server.py` and `agent_memory_mcp.py` for
`Authorization`, `Bearer` or `x-api-key` returns **zero hits on all three**.

**`mcp_server.py`** — one process-global client with no control plane and no scope guard
(`:96`, `:102`). `_scope(scope_kind, scope_id)` (`:247`) builds a scope straight from two
caller-supplied strings, so any caller can name any scope. And:

- `configure_tenant_llm(tenant_id, …)` (`:208`) — **`tenant_id` is an unauthenticated
  argument.** Any caller can seal a credential against any tenant, overwriting the incumbent's.
- `clear_tenant_llm(tenant_id)` (`:235`) — any caller can **revoke** any tenant's credential.
- `run_due_dreams` / `run_dream_job` take `scope_kind`/`scope_id` and **no `tenant_id`**, and
  passing a scope raises `control_plane is required` — so the server has no working
  tenant-qualified operation at all. It is single-tenant *by necessity*, not by policy (DW-029).

**`admin_server/`** — the principal is set **once, on the handler class, at process start** from
CLI args (`:2221`), shared by every request from every caller. `local_platform.py:102` defaults
`--principal-role` to **ADMIN**, which skips the scope allowlist entirely. Verified live
2026-09-02: `latest` and `stage` both returned **HTTP 200 on `GET /api/overview` with no
credential**, and `networkPolicy.enabled: false` means no compensating network control.

**`agent_memory_mcp.py`** — single-tenant per process, so no cross-tenant surface. But
`agent_id` is a caller-supplied string with no authentication, and the facade derives the
principal *from that string* — so a caller who names another agent's id **is** that agent.
`agent_register` is itself unauthenticated. Not deployed anywhere today.

**The path out**, in dependency order: the two identity halves (§5 — the registry and the
gateway lookup, both written, **neither wired**) → **#126 / #137**, the wiring that makes them
load-bearing on `mcp_server` and `admin_server` → #12 / #13. DW-029 explains why authentication
must come before per-tenant policy rather than after.

Until #126 / #137 land, **nothing in this section changes**: the surfaces still authenticate
nobody, and every statement above about them remains true.

---

## Appendix A — the Postgres schema

41 tables. All timestamps are ISO-8601 **`text`**, deliberately, so they stay byte-comparable
with SQLite (`postgres/_migrations.py:494`). Read from `postgres/_migrations.py`.

**Memory graph** — `nodes` (PK `uuid`, `graph_key` UNIQUE), `relationships` (PK `uuid`, FKs to
`nodes`), `episodes`, `relationship_state_tuples` (PK `relationship_uuid`, **ON DELETE
CASCADE**), `scope_state_hash` (PK `scope_key`), `relationship_embeddings` (**ON DELETE
CASCADE**).

**Epochs / registries** — `graph_epochs`, `epoch_runs`, `active_epochs`, `predicate_canon`,
`entity_canon`, `quarantined_candidates`.

**Governance / audit** — `memory_receipts` (56 columns, **no foreign keys at all** — an
append-only ledger), `run_checkpoints`, `governance_keys`,
`formation_contract_signing_keys`, `memory_prune_ghosts`, `promotion_endorsements`,
`dream_job_runs`, `dream_decisions`.

**Tenancy / identity** — `tenant_agents`, **`key_principals`**, `agent_motive_assignments`,
`tenant_llm_credentials`, `tenant_prompt_overrides`, `tenant_prompt_versions`,
`project_memory_config_versions`.

**Operational** — `processed_episodes`, `episode_processing`, `job_state`, `dream_claims`,
`memory_use_events`, `memory_outcome_events`.

**Artifacts / policy** — `live_artifacts`, `live_artifact_outcome_events`,
`live_artifact_versions`, `coherence_repair_monitors`, `policy_contract_versions`,
`policy_aliases`, `policy_shadow_stages`.

### Uniqueness — global vs scoped

This distinction is what DW-030 turned on, so it is worth reading carefully.

**Global** (no tenant or scope term in the key): `nodes.graph_key`, `key_principals.key_alias`,
`memory_receipts_run_event_idx(run_uuid, event_index)`, and every single-column PK.
`nodes.graph_key` deserves a note — scope isolation there depends entirely on callers composing
the key with a scope prefix (`epochs.py:1079`); **the schema does not enforce it**.

**Scoped**: `tenant_agents(tenant_id, agent_id_key)` **PARTIAL** `WHERE agent_id_key <> ''`,
`memory_use_events(scope_key, idempotency_key)`, `governance_keys(scope_key, subject_key)`, and
every composite PK carrying `scope_key` or `tenant_id`.

### Migrations

**Versioned, recorded, advisory-locked, transactional** — unlike SQLite. Ordered list at
`postgres/_migrations.py:MIGRATIONS`; runner `PostgresEngine.migrate()` (`_engine.py:384`) takes
`pg_advisory_xact_lock` so concurrent replicas serialise DDL, records applied versions in
`schema_migrations`, and **skips any version already recorded**. There is **no down path**;
rollback is deploy-forward-only.

> **The rule, and it has already bitten once.** Migrations are append-only. A table added to an
> already-applied version reaches every fresh database and **no existing one** — and every test
> passes, because test fixtures drop and recreate the schema. `key_principals` was written into
> `_V1_CORE` and moved to `_V9_KEY_PRINCIPALS` for exactly this reason;
> `TestTheTableReachesAnExistingDatabase` now exercises the upgrade path.

### Postgres-specific

pgvector is **optional and usually absent** — `_try_enable_pgvector` degrades quietly
(`postgres/__init__.py:165`), falling back to a `dw_cosine_similarity` SQL function over
`double precision[]`. Eight index columns use `COLLATE "C"` for byte ordering matching SQLite's
BINARY, and the engine warns at startup if the database collation is not byte-ordered
(`_engine.py:361`). `ON CONFLICT` is used pervasively as the race arbiter. Three advisory-lock
keys: migrations (`DWMI`), `exclusive_write_transaction` (`DWEX`, the twin of SQLite's
`BEGIN IMMEDIATE`), and per-run receipt serialisation. `_graph.py:202` uses **`FOR NO KEY
UPDATE`** deliberately — plain `FOR UPDATE` conflicts with the locks FK children take.

---

## Appendix B — where the two engines differ

They are hand-written independently and **have diverged before** — T0-3 was a one-token
difference in epoch visibility, fixed 2026-09-02. Known differences:

| # | what | impact |
|---|---|---|
| **D1** | `memory_use_events.query_digest` exists on SQLite only | the value survives inside `payload`, so reads round-trip, but the queryable-lineage property does not exist on Postgres |
| **D2** | `tenant_llm_credentials.embedding_provider` / `_base_url` / `_model` exist on SQLite only, and its `set_tenant_llm_credentials` takes three extra kwargs | **a live cross-engine contract divergence**; WS-17 T18 never reached Postgres |
| **D3** | `nodes`/`relationships.version` (bigint) exists on Postgres only | optimistic concurrency |
| **D4** | JSON columns are `*_json TEXT` on SQLite, unsuffixed `jsonb` on Postgres | naming and type |
| **D5/D6** | booleans are `INTEGER` on SQLite, real `boolean` on Postgres; floats `REAL` vs `double precision` | hash-neutral by design |
| **D7** | `tenant_agents` unique indexes are **total** on SQLite (with a Python backfill and a descriptive `RuntimeError` on duplicates) and **partial** `WHERE … <> ''` on Postgres, with no backfill | a pre-existing Postgres store with casefolded duplicates fails with a raw Postgres error rather than a readable one |
| **D8** | `memory_receipts_relationship_uuid_idx` exists on SQLite only | `latest_formation_receipt_digest` is defined once on the SQLite ledger and **inherited unchanged by Postgres**, where it uses `?` placeholders and seeks an index that does not exist. Its only caller is the promotion lineage path |

**Tables present on Postgres only**: `schema_migrations`, `relationship_state_tuples`,
`scope_state_hash`, `relationship_embeddings`. **No table exists on SQLite that does not exist
on Postgres.**

---

## Related

`docs/authorization-decisions.md` — DW-014 (identity by key self-lookup), DW-026 (the
alias→principal model), DW-027 (why global alias uniqueness makes it safe, measured), DW-028
(key forwarding works), DW-029 (seal vs apply), DW-030 (build the registry, don't borrow it).
`docs/operational-store-deployment.md` for credentials and migrations on deploy;
`docs/operational-store-sizing.md` for the connection budget.
