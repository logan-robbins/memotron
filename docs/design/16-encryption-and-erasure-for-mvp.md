# Encryption and erasure for the MVP launch

**Status:** accepted · **Decided:** 2026-09-04 · **Supersedes nothing; scopes** [#123](https://github.disney.com/jedai/memotron/issues/123), [#77](https://github.disney.com/jedai/memotron/issues/77), INFRA-BACKLOG I-5

## Why this exists

We wanted to deprioritize the KEK/DEK "secure erasure" work to launch sooner, and needed to know
what to use instead. **The premise was inverted, and that is the most useful finding here:
crypto-shred is already deprioritized.** There is nothing to remove and nothing to replace. What
there is instead is a dependency nobody had named, in a different part of the system.

This doc records three layers people keep conflating (CMEK, KEK, DEK), what this application is
actually permitted to use, and what the MVP ships.

## The three layers

| Layer | What it protects | Who holds the key | Available to us? |
|---|---|---|---|
| **CMEK** | The Cloud SQL **disk**, at rest | The Cloud SQL service agent | **No.** Not callable by the app — see below |
| **KEK** | Wraps DEKs. The app calls it | The app, via `KeyManager` | **Not via Cloud KMS.** Yes as a static secret |
| **DEK** | One per crypto-shred scope; seals content | Stored wrapped, in `governance_keys` | Only materialises under `CRYPTO_SHRED`, which is off |

### CMEK is not an erasure story, and we cannot call it

Every Memotron instance has a `cmek` block (`tf-jennay@origin/main`,
`latest-1/tfvars/cloudsql_module.tfvars:377-406` and the equivalents for stage/load/prod), plus
`ssl_mode = "ENCRYPTED_ONLY"` and `ipv4_enabled = false`. That is real, and it is the right answer
to *"is data encrypted at rest?"* — whole-instance, Google-managed.

It answers nothing else. The key's IAM policy has exactly one member: the Cloud SQL service agent.
`svc-jedai-memotron` is **not** bound, so the application cannot call the key at all, and
`encryption_key_name` is immutable after instance creation — this key can never *become* the app's
envelope key. `INFRA-BACKLOG.md:449` states the consequence plainly:

> CMEK being present changes nothing about what happens on pod restart.

CMEK gives no per-tenant, per-scope, or per-column granularity, and therefore no erasure capability.

### What the app SA may actually use

Verified against `tf-jennay@origin/main` `*/tfvars/iam_svc.tfvars`. In all four real environments
(`latest-1`, `stage-1`, `load-1`, `prod-1`) `svc-jedai-memotron` belongs to exactly two role
groups:

- `group_cloudsql_client` → `cloudsql.client`
- `svc_secret_manager` → `secretmanager.secretAccessor`, `secretmanager.viewer`

**No `cloudkms` role in any of them.** The only `cloudkms` grant anywhere in the repo is in `poc-1`,
for a different service account. Workload Identity *is* bound (GSA ↔ KSA, per env).

So a KMS-backed KEK is greenfield Terraform — a keyring, a key, and an IAM binding that do not exist
for any app on the platform. That is INFRA-BACKLOG I-5 / #77, and it is not a config flip.

## What the MVP ships

**A static 32-byte KEK delivered through Vault.** The chart's `keyManager` block reads
`MEMOTRON_KEK_B64` from the Vault-synced secret via an explicit `secretKeyRef`
(`.helm/values.yaml`, `templates/_helpers.tpl`). This needs no KMS key ring, no IAM change, and no
infra ticket — only a key written to the environment's existing Vault secret.

**Content sealing stays off.** `GovernancePolicy.erasure_behavior` defaults to `SOFT_RETIRE`
(`config/_governance.py:85`) and sealing only happens under `CRYPTO_SHRED`
(`dreaming/__init__.py:840-846`), so memory content is stored as plaintext, protected at rest by
CMEK and in transit by TLS. This is today's behaviour, not a change.

**So why is a KEK needed at all?** Not for erasure. `_bind_formation_contract` runs unconditionally
once per episode (`dreaming/_formation.py:312`) and wraps/unwraps the Ed25519 DSSE signing key with
`key_manager` (`storage/sqlite/_governance.py:850,855`). That is on the ingestion hot path, gated by
no policy. Tenant BYOK LLM credentials (`set_tenant_llm_credentials`) are the second consumer, but
they are optional — with no tenant configured, transports fall back to the server's
`LITELLM_API_KEY`.

### The KEK is permanent. Treat losing it as data loss.

Not as a credential to cycle. Rotating it strands anything sealed under the old key **and** forks
the attestation chain — see below. There is no re-wrap path in the codebase.

## What "delete my data" means today

**`memory_forget` archives; it does not delete.** It resolves to
`mark_relationship(status=PRUNED)` — an `UPDATE`. The row stays, the plaintext stays on disk, the
memory is excluded from default reads, and `memory_restore` reverses it. `agent_memory/_curation.py`
says so in its own module docstring: *none of them deletes.*

State this honestly in any customer-facing description. It is archive-with-exclusion, not erasure.

**The verifiable-erasure machinery exists but is not reachable.** `crypto_shred`,
`issue_erasure_certificate` and `verify_erasure_certificate` are real and tested (`erasure.py`), but
have **zero MCP and zero admin-server surface** — the only non-test caller is
`examples/fleet_simulation.py`. **MVP messaging must not claim verifiable erasure as a shipped
capability.**

### The DBA path for a genuine deletion request

Until a real erasure verb ships, a deletion request is handled manually:

1. Identify the scope(s) and tenant. Tenant isolation is an application-layer property (see below),
   so this is a `scope_key` / `tenant_id` predicate, not a database or role boundary.
2. `purge_tenant_state` (`storage/postgres/_governance.py:1440-1571`) resets *generated* state —
   relationships, nodes, use/outcome events, prune ghosts, receipts, run checkpoints. **It is
   explicitly not the erasure primitive**: its own docstring says raw episodes are never deleted.
3. Raw episode rows must then be deleted directly, by a DBA, in the same transaction where possible.
4. **Backups defeat all of it.** Every instance has `point_in_time_recovery = true` and retained
   backups (4 in non-prod, 8 in prod). A restore predating the deletion brings the data back. The
   real recoverability window is set by backup retention, and any deletion attestation has to say
   so. This is the same finding as [design 11](11-backup-restore-erasure-reconciliation.md).

## Tenancy and auth, given this substrate

Two platform constraints, both already true, both worth writing down so they are not re-litigated:

- **Password auth only.** `cloudsql.iam_authentication` appears nowhere in `tf-jennay`, for any app.
  Cloud SQL IAM database authentication is not an available option under the current provisioning
  standard; functional users are created by hand and their passwords land in Vault.
- **One database, one app user.** `memotron_db` per instance, `manage = false` (created
  out-of-band with `LC_COLLATE 'C'`), and one functional role. There is no per-tenant database or
  Postgres role.

Therefore **tenant isolation is necessarily enforced above Postgres**, in scope keys and `tenant_id`
predicates — which is how it already works. Postgres RLS would be defence-in-depth on top of that,
not a substitute for it, and is out of scope for MVP.

## The defect this investigation surfaced

`LocalKeyManager.key_id()` is `"local:" + sha256(kek)[:16]` (`crypto.py`), and the DSSE signer id
embeds it (`storage/_shared/_governance.formation_signer_id`). So **rotating the KEK does not fail —
it misses the lookup and silently mints a second signing key.** Measured:

```
ROTATED KEK -> NO CRASH, silently re-keyed
signer 1 : formation-contract:local:e7abf3239a1f3d14
signer 2 : formation-contract:local:b6a43cad208b33fe
same signing identity? False
signing keys now in table: 2 (was 1)
```

Attestations issued before the rotation verify against a public key the store will never present
again, and nothing recorded that the two are related. For a provenance claim that is worse than a
crash, and nothing in the suite noticed.

**Fixed in this change.** `formation_signing_key_status()` reports five states, and formation
preflights before claiming any episode: a stored key that will not unwrap **raises** and writes
nothing; a rotation **warns** and proceeds. Non-fatal on purpose — with an ephemeral KEK a new signer
is the normal state of every test and laptop run, and production Postgres already refuses to
construct with an ephemeral KEK (`storage/postgres/__init__.py:126-140`), so a warning there
genuinely means a durable key changed.

Preflight rather than per-episode degradation because `mark_episode_processed`
(`dreaming/_formation.py:824`) fires inside the loop: an episode formed without an attestation is
never re-formed, so its governing contract would be unprovable permanently. There is no later
repair.

## What changes when #77 / I-5 lands

A KMS-backed `KeyManager` replaces `LocalKeyManager` and nothing else moves — every call site already
takes a `key_manager`. Two things to carry forward:

- **`key_id()` must stay stable across processes**, which `crypto.py` already instructs. That makes
  the `KEK_MISMATCH` state reachable in production for the first time (with `LocalKeyManager` a
  changed key changes the signer id instead), which is exactly why the fatal path exists now.
- **`unwrap_dek` must raise only `ValueError` or `ContentKeyUnavailableError`** for key
  unavailability. That contract is written on the `KeyManager` Protocol. A native `ClientError` or
  `KmsDisabledException` escaping instead would turn a reportable state into a 500.

Only then does crypto-shred become a capability worth exposing — and it will still need an MCP/admin
surface, and an answer to the PITR problem above, before it can be claimed.
