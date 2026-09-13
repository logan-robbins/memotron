# What Memotron may and may not claim at MVP

**Read this before writing anything customer-facing** — a datasheet, a landing page, a security
questionnaire answer, a slide, a tool description, a README paragraph.

Closes the ownership gap in `docs/design/16-encryption-and-erasure-for-mvp.md` (accepted
2026-09-04), which states the constraint but assigns it to nobody (#236).

Every row below was **re-verified on `main` and against deployed `latest`** on 2026-09-11, with
controls, rather than carried over from the design doc. The commands are included so the next
person re-measures instead of trusting this file — it will go stale, and the whole point is that
a claim needs evidence at the moment it is made.

---

## The three sentences that matter

1. **We do not delete. We archive with exclusion.**
2. **Content is encrypted at rest by CMEK, whole-instance. Not per-tenant, not per-scope, not shreddable.**
3. **Backups defeat deletion for the retention window, and any attestation has to say so.**

---

## Say this / Do not say this

| ✅ Accurate | ❌ Must not claim | Why |
|---|---|---|
| "archive with exclusion" · "soft-retire" · "excluded from retrieval" | "erasure" · "deletion" · "wiped" · "forgotten" · "purged" | `memory_forget` resolves to `mark_relationship(status=PRUNED)` — an **UPDATE**. The row stays, the plaintext stays on disk, and `memory_restore` reverses it |
| "encrypted at rest (CMEK, whole-instance) and in transit (TLS)" | "per-tenant encryption" · "content-level encryption" · "encryption you can revoke" | Sealing happens only under `CRYPTO_SHRED`. `erasure_behavior` defaults to `SOFT_RETIRE` and **there is no deployed path to change it** |
| "retrieval excludes retired memories" | "verifiable erasure" · "erasure certificates" | The machinery exists in `erasure.py` and is **reachable from nothing**: 0 call sites on the MCP and admin surfaces |
| "deletion requests are a manual procedure with a recoverability window" | "deleted immediately" · "unrecoverable" | Every instance has `point_in_time_recovery = true`, 4 retained backups non-prod / 8 prod. A restore predating the deletion brings the data back |

## The evidence, and how to re-check it

**Erasure verbs are unreachable.** `crypto_shred`, `issue_erasure_certificate` and
`verify_erasure_certificate` have **0** call sites across `mcp_server.py`,
`agent_memory_mcp.py` and `admin_server/`. Control: `memory_forget`, which *is* on the surface,
returns 2 from the same query — so the zero is a real absence and not a broken grep.

```bash
git grep -c crypto_shred -- src/memotron/mcp_server.py \
  src/memotron/agent_memory_mcp.py src/memotron/admin_server/   # 0
git grep -c memory_forget -- src/memotron/agent_memory_mcp.py      # 2  <- the control
```

**`memory_forget` does not delete, and says so in its own module.**
`agent_memory/_curation.py:4` — *"none of them DELETES"*. Its MCP description already reads
**"Soft-retire one exact memory"**, which is accurate; do not "improve" it to "forget" or
"delete".

**No deployed path turns sealing on.** `config/_governance.py:85` is
`erasure_behavior: ErasureBehavior = ErasureBehavior.SOFT_RETIRE`, and `.helm/` contains **0**
references to `erasure_behavior` or `CRYPTO_SHRED` — no chart value, no env var, no admin route.

**Measured on deployed `latest`, 2026-09-11** — a `search` through the gateway returned
**0 sealed-value markers (`dwcm1$`)** and readable plaintext:

```
{"subject": "hosted platform API", "predicate": "requires", "object": "bound gateway key", ...}
```

## What IS true and is worth saying

CMEK at rest and TLS in transit are real controls. Access is bounded by authentication —
`MEMOTRON_REQUIRE_GATEWAY_IDENTITY`, armed on `latest` and verified by
`probe_mcp_identity.py` passing all eight arms, including a cross-tenant write refused **and
absent**. Retired memories are genuinely excluded from retrieval. None of that requires
overstating it as erasure.

> **Do not substitute the Host allowlist for authentication in any claim.** It is a
> DNS-rebinding control, not an access control: `DEFAULT_HOSTS` is prepended on every request
> regardless of configuration, and `Host: localhost:8000` was accepted even under the previous
> SDK. Pinned in `tests/test_mcp_transport_security.py::TestTheAllowlistIsNotAnAccessControl`.

## Out of scope

Building a real erasure verb, or enabling `CRYPTO_SHRED`. Design 16 accepted leaving both off
for MVP. This file is only about not *claiming* what was deliberately not built.

## This file is enforced

`tests/test_mvp_claims_are_not_overstated.py` fails if the forbidden vocabulary appears in a
user-facing surface — README, `docs/*.md`, the admin UI, or an MCP tool description. It cannot
police a slide deck; it can stop the repo from contradicting this page.
