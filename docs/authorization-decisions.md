# Authorization model — decisions raised by the security front-door work

Drafted for the component decision log (*Solution Engineering → Agent Memory →
Decision Log*). Same format as DW-001..DW-025: the choice, the alternative, why
the alternative lost for a reason that stays true, and the cost knowingly
accepted. Paste into the portal; this file is the working copy, not the record.

This entry closes the last acceptance bullet of
[#27](https://github.disney.com/jedai/memotron/issues/27) — *"recorded as a
decision in the design's decision log"*. It gates #12 and #13.

Kept separate from `docs/operational-store-decisions.md`, which is scoped by its
own opening line to decisions raised by the persistence work.

**Read this before the entry.** An earlier draft of DW-026 recommended the
opposite of what follows: one virtual key plus a gateway-minted RS256 assertion
verified locally, per `docs/design/27-authorization-model.md`. That design doc was
written without access to the portal — it says so in its own risks section — and
it contradicts **DW-014**, which had already decided this question on 2026-07-30,
two weeks earlier. DW-014 is authoritative and the earlier draft is withdrawn.
The design doc has been corrected in the same pass. What survives from that work
is evidence, not a recommendation, and it is recorded below.

Provenance: portal sources read from `~/repos/jedai/portal` at `d680794`
(branch `develop`, 2026-08-27); DW-014, DW-018 and the access-control page all
last changed in `24515dd` (2026-07-30, program#392). Gateway sources read on the
gateway repo's default branch `develop` at `a9cdc23834` (2026-08-25), LiteLLM fork
version 1.95.0. Live gateway measurements are against LATEST on 2026-08-28.

---

## DW-026 — #27 stays on DW-014's key self-lookup; the signed-assertion upgrade path is not taken, and the self-lookup is now validated end to end

Programmatic identity resolves exactly as **DW-014** specifies, and this spike
adopts it rather than replacing it. Memotron holds the caller's forwarded
LiteLLM virtual key, performs a `/key/info` **self-lookup** — a key may query
itself, no admin credential involved — and uses the two returned values against
its own records: the **team id** maps to a tenant through the tenant record
(**DW-018**), and the **key alias** maps to a principal, role and allowed scopes
through the per-key registry on that record. Resolutions are cached by key hash
under `MEMOTRON_IDENTITY_CACHE_TTL` (directional default 60 s) and fail
closed. Claims are never read from gateway team or key metadata, and never from
the request body.

The spike's own question was whether a signed per-call assertion should replace
that round trip. The answer is no, for now: DW-014 already deferred the gateway's
MCP JWT-signing guardrail as *"the named upgrade path if the identity contract
needs hardening, never the baseline for the REST surface"*, and nothing found in
this spike disturbs that. Two things actively reinforce it. The guardrail fires
`pre_mcp_call` only, so it cannot cover the REST plane — confirmed by reading the
module's own contract on `develop`. And the claim mapping such a design needs is
a **specifically rejected alternative**: DW-018 refuses a tenant claim in team
metadata because team admins are the use case's own owners, and refuses one in key
metadata because team members hold key create and update rights, so either would
be self-asserted. An assertion minted from that metadata would carry a claim the
tenant could write about itself.

What this spike contributes is the validation DW-014 asked for. DW-014 records its
own approach as *"not yet validated end to end, which is #12's first task."* Its
central mechanism is now **observed working on LATEST**: a freshly minted, provably
non-admin virtual key — proved non-admin because `/key/list` refused it with 403 —
called `GET /key/info` and received **200** with its own alias and identifiers, no
admin credential anywhere. The same key was refused on `/key/generate` and
`/key/update` with *"Only proxy admin can be used to generate, delete, update info
for new keys/users/teams"*, so a caller holding only its own key cannot mint or
edit one. Alongside that, two implementation constraints DW-014 does not mention.
**First, the self-lookup must target the admin plane.** Management routes are
hard-disabled on the data-plane host: the base chart sets
`DISABLE_ADMIN_ENDPOINTS: "true"` for all roles
(`gateway/.helm/values.yaml:268`), the admin role overrides it to `"false"`
(`gateway/.helm/values-latest.yaml:99`), and the worker role inherits `"true"` —
so the data plane answers `403 "Management routes are disabled for this instance."`
to `/key/info`, `/user/info` and `/team/info` alike, **including the
`?key=<self>` form**. Resolution therefore crosses to a separately scaled,
admin-only deployment, which is a coupling and a blast-radius consideration for
#12 rather than a reason to change the model. **Second, the measured cost of a
cache miss** on that path, persistent connection, 25 samples: **p50 131 ms, p95
461 ms** from a workstation over VPN — note the p95 is roughly three times the
admin key's, so the tail is worse than the median suggests. Every probe carried a
no-key control arm returning 401, without which none of the 200s above would be
attributable to the key.

**Alternatives:** *per-call signed identity assertions via the gateway's
`mcp_jwt_signer` guardrail.* Not taken, consistent with DW-014's deferral, and the
grounds are now firmer than when DW-014 was written: the guardrail is real and
usable — present in the fork at
`gateway/litellm/proxy/guardrails/guardrail_hooks/mcp_jwt_signer/mcp_jwt_signer.py`
(926 lines, with tests), carrying no premium gate, and an enterprise entitlement is
held — so capability is not the objection. The objections are scope and
provenance: `pre_mcp_call` leaves the REST surface unaddressed, and the only place
to put tenant and scope claims is metadata DW-018 rejects as self-asserted. It is
also unconfigured in practice: `GET /.well-known/jwks.json` returned
`{"keys":[]}` with HTTP 200 on both hosts on 2026-08-13 and again, unchanged, on
2026-08-28 — and because the module auto-generates a keypair at startup when
`MCP_JWT_SIGNING_KEY` is unset, an empty key set means the guardrail is not
configured at all. Revisit as an upgrade when the identity contract needs
hardening, exactly as DW-014 frames it, and only with a claim source that is not
tenant-writable. *Launchpad/AuthZ.* Unchanged from DW-014, which retains it as the
fallback: it would take resolution traffic off the gateway, but every client would
carry two credentials. *Gateway-injected identity headers.* Unchanged from
DW-014's rejection — pass-through forwards client headers verbatim, so a header
contract is spoofable. Independently supported here: no `NetworkPolicy` exists in
either chart today (**T3-4**), so the network guarantee DW-004 relies on is not
deployed yet, which makes an unsigned-header contract worse than DW-014 assumed.

**Cost:** the costs DW-014 already accepted, now with numbers against them. The
gateway carries the whole API's traffic plus one extra round trip per cache miss on
the hot path — measured at p50 131 ms, p95 461 ms from a workstation, and
**still unmeasured in-cluster**, which is the number #12 owes. Key revocation
propagates only within the identity cache TTL, so that TTL is a security
parameter; note the gateway's own key cache is per-pod and unshared
(`enable_redis_auth_cache` is set in no environment,
`gateway/litellm/proxy/proxy_server.py:1400` defaults to 60 s), so Memotron
cannot resolve faster or revoke sooner than the gateway itself does. Resolution
couples to the admin-plane deployment, not the data plane. And one cost this spike
added rather than inherited: the repo carried a merged design document
recommending against DW-014 for two weeks, so `docs/design/27-authorization-model.md`
required correction, and any work started from it in that window needs rechecking.

**Still untested, and it is the load-bearing gap:** DW-014 notes the gateway
*strips* a caller `Authorization` header that is the gateway key itself, so the key
must arrive as `x-litellm-api-key` through the MCP server registration's
`extra_headers` allowlist, and DW-014 marks that forwarding *untested*. Every
probe recorded here used `Authorization` directly against the gateway, which
exercises the self-lookup but **not** the forwarding path the design depends on.
That remains #12's first task; this entry narrows it rather than closing it.

---

## DW-027 — DW-026's alias→principal lookup is safe because `key_alias` is globally unique; that premise is now measured, and one DW-026 claim is corrected

DW-026 resolves principal, role and allowed scopes by looking the returned
`key_alias` up in Memotron's own per-key registry. That is a good design — the
alias is a *lookup key* into records Memotron controls, never a claim the caller
asserts — but it rests on an unstated premise: **a caller must not be able to obtain
a key bearing someone else's alias.** A non-admin chooses their own alias freely at
mint time, so if aliases could collide, Alice mints `key_alias` = Bob's and resolves
as Bob, with Bob's role and scopes. DW-026 does not record this premise being tested.

It holds. LiteLLM enforces alias uniqueness **globally**, not per user or per team,
and refuses all three collision routes with `400 "Key with alias '<x>' already
exists. Unique key aliases across all keys are required."`: an admin minting a
duplicate, a team member minting another user's alias, and a team member *renaming*
their existing key onto it via `/key/update`. Since a registered alias is by
definition already held by its owner's key, uniqueness makes it unmintable by anyone
else. **DW-026's principal resolution is sound as written.**

**One DW-026 claim is too strong and should be read narrowly.** DW-026 states that a
caller holding only its own key "cannot mint or edit one", citing a `/key/generate`
refusal. That refusal was obtained with a key whose `user_role` is unset — the
gateway reports `role=unknown` on that path. A **properly constituted team member**
(`internal_user`, team role `user`) is permitted to mint by
`key_generation_settings.team_key_generation.allowed_team_member_roles: ["admin",
"user"]`, and **does**: observed `200` minting a team key on LATEST. The conclusion
DW-026 draws survives, but for a different reason than it gives — not because
members cannot mint, but because what they mint is constrained. Four forgery routes
were attempted by such a member and all refused: a non-member `user_id` (`400`), a
team they do not belong to (`400`), rebinding their own key's `user_id` (`403`
*"Non-admin caller is not allowed to rebind the key"*), and claiming a **co-member's**
`user_id` (`403` *"User can only create keys for themselves"*). The co-member case is
the one that matters, because `team_id_default` puts everyone in one team, so
co-membership is the normal condition rather than an edge case. Reading `/key/info`
or `/key/list` for another user was refused `403` both ways.

**Two further facts the identity cache depends on.** Key regeneration preserves
`user_id`, `team_id` and `key_alias` verbatim and changes **only the token hash** —
so a rotated key keeps its registry identity, and DW-026's cache-by-key-hash simply
misses and re-resolves rather than mis-resolving. And `team_id`, which DW-018 maps to
a tenant, is **degenerate on LATEST today**: across admin-visible keys it is 40%
absent and 50% a single shared value, with one key carrying a `TEAM_DX*`. Tenancy at
launch is therefore effectively per-environment, with real separation happening at
the principal; it sharpens on its own as teams get used, with no migration. This is a
scoping observation, not an objection to DW-018.

**Cost:** one more premise that must be re-checked per environment rather than
assumed. Alias uniqueness, the four refusals, and regeneration survival are all
LiteLLM behaviours, not our own, and they are the kind that move across fork
upgrades. `scripts/verify/probe_gateway_tenancy.py --env <env>` re-proves them in one
command (exit 0 holds / 1 forgeable / 2 inconclusive), carries a positive control so
a dead gateway cannot read as a green boundary, and tears down everything it creates.
Deliberately not in `check.sh`: it needs a live gateway and a master key.

**Still open.** *Alias squatting on unprovisioned entries* — uniqueness is
first-come-first-served, so an alias listed in the registry but not yet minted could
be claimed by whoever mints it first. Registry entries should be created from an
existing key, not ahead of one. *The `x-litellm-api-key` forwarding path* was still
untested at the time of writing; **DW-028 closes it** for the gateway side. *Keys ineligible for Memotron* — the 40% carrying neither `user_id`
nor `team_id` must be rejected rather than defaulted, since a fallback would resolve
a rootless key to somebody's memory.

Provenance: gateway `a9cdc23834`; all measurements against LATEST on 2026-09-02 via
`scripts/verify/probe_gateway_tenancy.py` and the alias-collision probe folded into
it. Cross-repo write-up: brain vault `adr/0008-memotron-tenancy-binds-to-litellm-user-id`,
which defers to DW-014/DW-026 on principal resolution.

---

## DW-028 — the `x-litellm-api-key` forwarding path works; the gap DW-014 opened and DW-026 left open is closed for the gateway side

DW-014 records that the gateway **strips** a caller `Authorization` header that is the
gateway key itself, so a virtual key must instead arrive as `x-litellm-api-key` through
the MCP server registration's `extra_headers` allowlist — and marks that forwarding
*untested*. DW-026 narrowed it and called it "the load-bearing gap": every probe there
used `Authorization` directly against the gateway, exercising the self-lookup but not
the forwarding the design depends on. Everything downstream — DW-027, the tenant/principal
binding, the scope guard in #137 — assumes Memotron receives the caller's key, so
none of it has an input if this does not work.

**It works.** Measured on LATEST against `jedai_gateway`, which is already registered with
`extra_headers: ['authorization', 'x-litellm-api-key']` — the exact shape DW-014
prescribes — and exposes `jedai_whoami`, a tool that reports the identity it resolved
from the forwarded key. Two keys minted with distinct users resolved to **distinct,
correct identities** (`dw-fwd-probe-alpha` and `-bravo`, each `internal_user`), the master
key resolved to `proxy_admin`, and a no-key call was refused `401`. The reading of the
vendored code agrees and explains the mechanism: `server.py:1742-1768` copies any header
named in `server.extra_headers` verbatim from the caller's `raw_headers`, with
`authorization` the sole conditional exception; nothing strips `x-litellm-api-key`.

**Two controls, both of which the first attempt lacked.** A single `whoami` proves
nothing — the upstream holds its own credential, so a broken forwarding path still
returns a perfectly good identity, the *server's*, which reads as success. Hence the
two-key differential. And a positive arm is equally necessary: the first run reported a
confident **FAIL** when both probe keys returned `authenticated:false`. That was wrong.
`jedai_whoami` resolves via `GET /v2/user/info`, and `/key/generate` does **not** create a
user row for a `user_id` it has never seen — so there was nothing to resolve. The master
key, sent identically, authenticated fine, which is only possible if the header arrived.
Same defect shape as everything else in this branch: *a check whose failing condition is
satisfied by something other than the thing it claims to measure.* An earlier run had
already failed differently, on tool permissions (`object_permission.mcp_servers`), and was
also briefly read as a forwarding failure. Identical text is not identical identity.

**The actionable consequence is a registration requirement.** This proves the mechanism,
not our use of it: **Memotron is not registered as an MCP server at all** — 25 servers
on LATEST, none of them Memotron. When it is registered, it **must** carry
`x-litellm-api-key` in its `extra_headers`, or the key silently never arrives and the
whole model fails closed with no obvious cause. Treat that as part of the registration,
not a later hardening step.

**Cost:** one more LiteLLM behaviour we depend on and do not own.
`scripts/verify/probe_mcp_key_forwarding.py --env <env>` re-proves it in one command
(exit 0 forwarded / 1 not / 2 inconclusive), carries both controls, and tears down its
keys and users.

**Still open.** #137 — Memotron reading the forwarded key — is now the only thing
between here and a working authorization path; the gateway half is done. Verified on
LATEST only; re-run per environment before relying on it there.

Provenance: measured 2026-09-02 against LATEST; gateway `a9cdc23834`
(`litellm/proxy/_experimental/mcp_server/server.py:1742-1768`).

## DW-029 — a server that cannot resolve per-tenant policy must SEAL a credential, not APPLY it; single-tenancy becomes an explicit deployment statement

**Status: DRAFT — recommendation, not yet ratified. Tracked as #160.**

### The question

Closing #158 made `configure_tenant_llm` inert on the governance MCP server. What should that
surface do with a sealed credential it cannot attribute to an operation?

### What was measured (2026-09-02)

* `run_due_dreams` (`mcp_server.py:754-757`) and `run_dream_job` (`:782-786`) take `scope_kind` and
  `scope_id` — **no `tenant_id`**. `configure_tenant_llm` (`:209-212`) does take one, so the
  asymmetry is in the tools themselves.
* Plumbing `tenant_id` in raises, observed:
  `ValueError: control_plane is required for tenant/agent/scope runtime policy`. The governance
  server constructs its client with no control plane.
* Before #158 the credential *was* applied — by mutating the process-shared client, which is the
  leak #158 exists to close. The feature and the defect were the same mechanism.

### Decision (recommended)

**Option 1 + Option 3, in that order.**

1. **`configure_tenant_llm` seals; it does not apply, on any surface that cannot name the tenant of
   an operation.** The response must say so, so a caller cannot mistake sealed for active. A server
   that cannot resolve per-tenant policy has no correct way to apply one tenant's key, and the
   incorrect way is precisely #158.
2. **Single-tenancy is declared, never inferred.** Where a deployment genuinely serves one tenant,
   it says so (an explicit tenant id in the environment) and the server calls the existing
   `bind_default_tenant()`. Today's behaviour is preserved for those deployments, and the property
   that makes it safe — that there is only one tenant — becomes a checkable deployment fact instead
   of an accident of whoever configured last.

### Alternative rejected, and why

**Give the governance server a control plane and plumb `tenant_id` through** (Option 2) is the right
*end* state and is not rejected on merit — it is rejected as the immediate step. It requires the
control-plane story settled for a surface that has none, and it would land per-tenant policy
resolution on the same server that today authenticates nobody (#126, #137): a caller can already
name any `tenant_id` in `configure_tenant_llm` and there is no principal to check it against. Making
that server resolve per-tenant policy before it can authenticate a tenant would move a real decision
behind an unauthenticated argument. Order matters: authenticate first (#126/#137), then per-tenant
policy.

### Consequences

* A credential sealed against the governance server has no runtime effect until (2) is implemented
  or a control plane exists. **This is a reduction in function versus pre-#158 behaviour**, and it
  is deliberate: the function it removes was cross-tenant credential misuse.
* `AgentMemoryPlatform` is unaffected — single-tenant by construction, already calls
  `bind_default_tenant()`. Verified: configure → default and engine both carry the new key; clear →
  both revert.
* The docstring at `mcp_server.py:216` must be corrected either way; it currently claims the tool
  applies the credential.

### New evidence, 2026-09-03 — the recommendation is unchanged and much better supported

Re-examined before ratifying, and three things came out.

**1. The governance server can ONLY do global, tenant-less dream runs. Its scoped path already
raises, and did so before any of this work.** Observed on a client with no control plane:

    run_due_dreams()                          -> OK
    run_due_dreams(scope=tenant:acme)         -> ValueError: control_plane is required ...
    run_due_dreams(tenant_id="acme")          -> ValueError: control_plane is required ...

The middle line is what `mcp_server.py:764-766` does whenever a caller passes `scope_kind` +
`scope_id`, which the tool's own docstring advertises as *"restrict to one scope"*. **Verified
against `origin/main`, pre-#158: it raises there too.** So this is pre-existing and unrelated to the
leak fix — but it means the surface has no working tenant- or scope-qualified operation at all.

**2. That makes the server single-tenant BY NECESSITY today.** Not by policy, not by deployment
choice — it is the only mode that works. Option 3 therefore *describes what the server already is*
rather than changing it, which is a far weaker ask than it first appeared. There is also a precedent
in the repo: `agent_memory_mcp.py:119` already reads `MEMOTRON_PROJECT_ID` to bind its tenant.
And the chart passes the api pod no tenant-ish env at all today (`MEMOTRON_GRAPH_PATH`,
`MEMOTRON_MCP_ALLOWED_HOSTS`, `MCP_HOST`, `MCP_PORT`, `UV_CACHE_DIR`), so adding one is additive.

**3. A fourth option was considered and is DEAD.** `ScopeKind` has a `TENANT` member, and the
identity *TENANT-kind `scope_id` == `tenant_id`* is an established convention — storage itself
builds `f"{ScopeKind.TENANT.value}:{normalized_tenant}"` (`sqlite/_governance.py:940`,
`postgres/_governance.py:1321`), and `agent_memory/_scopes.py:25` and
`admin_server/__init__.py:640` do the same. So deriving the tenant from an existing `scope_kind`
= `"tenant"` call looked like a no-new-parameter fix. **It cannot work**: passing a scope raises on
the control-plane check *before* the derived tenant could ever be used (line 2 of the transcript
above). Recorded so it is not proposed again.

### What would overturn this

Evidence that a governance-server deployment genuinely serves exactly one tenant *and* cannot
declare it. That would make Option 3's explicit statement unimplementable and force Option 2 sooner.
No such deployment is known: `latest` runs one tenant's demo scope and could declare it trivially.

Related: **#158** (the leak), **#159** (revocation across clients), **#160** (this decision),
**#126** / **#137** (request authentication), **#157** (the 421 that makes all of this currently
unreachable on `latest`).

## DW-030 — the per-key registry is BUILT, not borrowed: `tenant_agents` cannot answer `key_alias → principal`

**Status: measured 2026-09-03. RATIFIED by Ryan 2026-09-03, and BUILT — `key_principals` ships
on both engines (Postgres migration 9, SQLite DDL) with 23 tests across both. The registry is
written and wired to nothing; see the Consequences below for what that does and does not buy.**

### The question

DW-026 maps a caller's `key_alias` to a principal *"through the per-key registry on that record"*.
That registry does not exist — verified: the storage contract has no `key_hash`, `api_key`,
`virtual_key`, `key_registry` or `key_alias` method (control: `tenant_agents` does exist). Two
options were scoped: **build a registry**, or **reuse `tenant_agents`** treating `key_alias` as an
agent id. This is the second option, tested.

### Reusing `tenant_agents` fails, three ways, and the first is the serious one

**1. The uniqueness scope is wrong, and it is the scope the design rests on.** DW-026 is safe
*because* `key_alias` is **globally** unique — enforced by the gateway, with forgery attempted three
ways in DW-027 and refused each time. `tenant_agents` is unique **per tenant**, on both engines:

    sqlite    UNIQUE INDEX tenant_agents_agent_id_key_unique_idx ON tenant_agents(tenant_id, agent_id_key)
    postgres  UNIQUE INDEX tenant_agents_agent_id_key_unique_idx ON tenant_agents (tenant_id, agent_id_key) WHERE agent_id_key <> ''

So nothing stops tenant A and tenant B both registering agent id `"foo"`, and a `key_alias → principal`
lookup for `"foo"` is then **ambiguous between two tenants**. Borrowing the table silently discards
the property that makes the whole model safe.

**2. The lookup runs the wrong way.** Authentication holds a `key_alias` and **no tenant yet** — that
is the point of the step. `tenant_agents`' primary key is `(tenant_id, agent_id)` and all four
queries against it are scoped `WHERE tenant_id = ?`. There is no tenant-free lookup path, so the
table cannot answer the question being asked.

**3. It does not carry a principal.** `MemoryPrincipal` (`config/_tenancy.py:109`) needs
`principal_id`, `tenant_id`, `agent_id`, `default_scope`, `allowed_scope_keys`, `role`. The table
stores `tenant_id, agent_id, agent_id_key, name, name_key, source, created_at, last_seen_at` —
**missing `principal_id`, `default_scope`, `allowed_scope_keys` and `role`.**

Fixing all three means a globally-unique index, a tenant-free lookup, and four new columns, **on both
engines, with migrations**. That is not the cheap option; it is the build wearing a borrowed table,
and carrying the wrong uniqueness scope while it does it.

### Decision (ratified 2026-09-03)

**Build the registry as its own table.** The sub-question — own table vs columns on `tenant_agents` —
resolves the same way: a dedicated table can carry a **globally** unique index on `key_alias`,
mirroring the gateway's own guarantee, whereas `tenant_agents`' uniqueness is tenant-scoped for good
reasons of its own that should not be changed to suit this.

### Consequences

* Unblocked #156 (`gateway_identity.py`, built and live-verified). Both halves now sit in one
  tree, and **both are still imported by nothing** — verified by grep over `src/` on 2026-09-03.
  What remains is #126 / #137, the wiring, which unblock #12 and #13 and dissolve #160. Do not
  read "the registry landed" as "callers are authenticated": no deployed surface consults it.
* Two engines, two migrations. `storage/base.py` currently declares 149 methods; this adds a small
  number to both implementations, and both must be covered — see #149 on the Postgres test blind spot.
* The registry becomes the authority mapping a gateway credential to a principal, so it is a
  credential-flow change: threat-model pass and independent verifier before it lands.

### What would overturn this

Evidence that `key_alias` is *not* globally unique on some gateway environment — the premise DW-027
measured on LATEST only. Re-run `scripts/verify/probe_gateway_tenancy.py --env <env>`; it carries a
positive control, so a broken gateway reports INCONCLUSIVE rather than a false pass.

### Where this is wrong

The schema claims are read from `_migrations.py` on both engines, not from a live database. If a
deployed store has drifted from its migrations, check the live indexes before relying on §1. Note
this repository has had exactly that class of divergence before — T0-3, where the two engines'
epoch-visibility rules disagreed by one token, fixed 2026-09-02.

Related: **#126**, **#137**, **#156**, **#160**, **#12**, **#13**, DW-014 / DW-026 / DW-027, ADR 0008.

---

## 2026-09-06 — Wiring the registry to the door: the first authenticated MCP caller (#126)

### The state this changes

The previous section closed with a warning that has now been acted on: *"Do not read 'the registry
landed' as 'callers are authenticated': no deployed surface consults it."* Measured on `origin/main`
2026-09-05, that was still exactly true — zero credential reads
(`Authorization|Bearer|x-api-key|x-litellm|headers[`) across `mcp_server.py`, `agent_memory_mcp.py`
and `admin_server/__init__.py`, and `key_principals` holding **0 rows** on `latest`.

`_scope(scope_kind, scope_id)` built a `MemoryScope` straight from two caller-supplied strings. Any
caller reaching the ingress could read or write any tenant's memory by naming it, bounded only by
`ingress.class: gce-internal` — a network boundary standing in for an authorization one.

### Decision

**Authenticate in ASGI middleware; authorize at `_scope`; ship it behind a default-off flag.**

* **Authentication** — `GatewayIdentityMiddleware` (`mcp_auth.py`) reads the caller's virtual key,
  resolves it through `resolve_gateway_identity`, maps the returned `key_alias` through
  `principal_for_key_alias` → `principal_from_registry_row`, and writes the principal to
  `request.state`. `x-litellm-api-key` **wins** over `Authorization`: through the gateway the latter
  may carry the *gateway's* key (DW-014), so the other order would authenticate the wrong party and
  succeed while doing it.
* **Authorization** — `_scope` checks `scope.key in principal.effective_allowed_scope_keys()`. One
  function edited, **33 of 39 tools covered**, no signature changes. Tools keep
  `scope_kind`/`scope_id`: `allowed_scope_keys` is a *set*, so the argument says which scope you
  mean and the principal says which you may name.
* **The ADMIN bypass is NOT replicated.** `config/_control_plane.py` lets admins skip the allowlist;
  deferring a decision about that bypass is not a reason to reproduce it in new code.
* **Rollout flag** `MEMOTRON_REQUIRE_GATEWAY_IDENTITY`, **default off**. Off is byte-for-byte the
  previous behaviour — and is the vulnerability, not a safe resting state.

### Consequences

* **The gateway becomes a hard dependency of every memory operation**, including ones needing no
  LLM. An outage answers **503 + Retry-After**, never 401 — a 401 would send callers to re-check a
  perfectly valid credential. This is the real cost of the slice and it is accepted, not mitigated.
* **Five tools bypass the guard by construction** and are enumerated as `_CROSS_SCOPE_TOOLS`:
  `configure_tenant_llm`, `tenant_llm_status`, `clear_tenant_llm`, `dream_history`,
  `dream_decisions`. Three take a `tenant_id` — one writes a sealed credential and one deletes it —
  and two are fleet-wide. **Authenticating at the door does not authorize any of them.** That is
  the remainder of #126.
  It was SIX. B3 gave `remediate_coherence` a `scope_kind`/`scope_id` pair, so a bounded call now
  routes through `authorize_scope` like any other scoped write, and only the omission keeps the
  stricter fleet-wide rule. `dream_history` and `dream_decisions` have no scoped variant to give
  them: `DreamJobRunRecord` has no scope field at all.
* **Four more declare a scope and skip the guard by omitting it** (`run_due_dreams`,
  `run_dream_job`, `coherence_incidents`, and `remediate_coherence` since B3). The first three
  refuse the omission with `require_explicit_scope`; `remediate_coherence` RESTRICTS it instead,
  with `require_fleet_wide_read`/`_write`, because a legitimate ADMIN fleet-wide sweep still exists.
  `_OPTIONAL_SCOPE_TOOLS` keeps the list honest and `_OMISSION_GUARDS` records which form each uses.
* **`agent_memory_mcp.py` was a second FastMCP server with the identical hole.** No longer:
  #206 Phase 1 wrapped its entry point (`examples/agent_memory_mcp_server.py`) in
  `GatewayIdentityMiddleware`, and #206 Phase 2 made the platform beneath it check the CALLER rather
  than the `agent_id` it was handed. It is still deployed nowhere (#206 Phase 4).
* `authorized_scope_keys` on the SDK client stays uncalled. It is a construction-time set on a
  process-wide client; this slice duplicates the check per-request rather than wiring the existing
  guard, so do not read this as "a built-but-unused piece was finally connected".

### How it was verified, and what that does not prove

`bash scripts/check.sh` 16/16 with `MEMOTRON_TEST_POSTGRES_DSN` set. 66 test functions across
four files (82 collected), of which the load-bearing ones speak MCP over HTTP through the real
middleware and assert that two keys bound to two tenants **disagree**. Both halves were
mutation-checked: deleting the middleware's `request.state` write fails exactly the positive
controls; making `authorize_scope` a no-op fails exactly the negatives.

**That paragraph used to end "all of it is hermetic".** It no longer does. Measured on deployed
`latest` 2026-09-09 — the same route, three states, by round trip:

| request to `/api/platform/status` | response |
|---|---|
| before #194 | `503 {"error":"Memotron platform API is not enabled for this server"}` |
| no key | `401 {"error":"no gateway key on the request"}` |
| `x-litellm-api-key: <bound key>` | `200` with the tenant payload |

The 401 string is emitted by `admin_server._caller_principal`, so that one response proves three
things at once: the platform exists, the caller check runs, and `identity_required()` is true in
that process. The 200 is the positive control — without it, "401 for everyone" is indistinguishable
from a broken surface.

### The order this must ship in — DONE for `latest`, unchanged for every other env

1. Bind real rows (`memotron key bind`).
2. Flip the flag in the env overlay, as its own reversible commit.
3. Run `probe_mcp_identity.py` against that env.

Enabling the flag with zero rows bound refuses every caller. That is a total outage, and it is the
most likely way for this change to cause harm.

**A fourth step this rollout needed and the list did not name.** `roles.yaml` rendered the env var
for the **api role only**, so `requireGatewayIdentity: true` in an overlay did NOT mean the
environment authenticated its callers — the admin surface never received it, and two merged changes
that read the flag in that process shipped inert. Fixed 2026-09-09. When arming a new environment,
render the chart and read the CONTAINER, with a role that should have it as a positive control:

    helm template dw .helm -f .helm/values.yaml -f .helm/values-<env>.yaml
    kubectl get pod -n jedai-memotron <pod> -o jsonpath='{.spec.containers[0].env[*].name}'

**`stage`, `prod`, `preview` and `load` still inherit `requireGatewayIdentity: false`.** Nothing
above has been measured there, and step 1 has not been done for them.

### What would overturn this

A gateway environment where a minted virtual key's `/key/info` self-lookup returns a null
`key_alias`. The alias *is* the lookup key; DW-026 measured a 200-with-alias on LATEST, and DW-027
records ~40% of gateway keys carrying neither `user_id` nor `team_id`, so the alias is the only
field this can hang on. Such a key gets 403 by design, but if it is the common case rather than the
exception, the mapping strategy needs rethinking.

Related: **#126**, **#137**, **#156**, **#12**, **#13**, DW-014 / DW-026 / DW-027 / DW-028 / DW-030.

### What independent review changed, before this landed

Two reviewers with no session context — a verifier against the rubric and a red-team pass — were run
against the diff. Recording the findings because several are the repo's own named defect class, and
one was found *inside the test written to prevent it*.

**A fourth escape category the tripwire could not see.** The guard tests asserted that every tool
either declares `scope_kind` or is a listed exemption. The verifier added a tool with a **required**
`scope_kind` that built its own `MemoryScope` instead of calling `_scope` — and **all six tests
passed**. Declaring a scope is not routing through the guard. Closed with two body-level assertions:
every scoped tool must call `_scope`, and `_scope` must be the only place in the module a
`MemoryScope` is constructed. Both were confirmed to fail against that exact mutant.

**Credential material in a refusal body.** `GatewayRequestError`'s message embeds the gateway's
response verbatim, and the middleware returned it to the caller. Observed live against the preview
gateway: the 401 body carried LiteLLM's `Received API Key = sk-...` **and the key's
`LiteLLM_VerificationTokenTable` hash**, to an unauthenticated caller. A refusal is the one response
an attacker can always elicit. `_RefusalError` now carries a separate `public_detail`; the upstream
body goes to the log only.

**Empty is not absent.** A present-but-empty `x-litellm-api-key` fell through to `Authorization` —
which DW-014 says may carry the *gateway's* own key. That is the exact confused deputy the
precedence rule exists to close, and the two-key probe would not have caught it, because both arms
send a non-empty forwarded header. The header's **presence** is now the assertion that forwarding
ran; empty is a 401.

**Duplicate headers were last-wins.** The middleware's dict comprehension took the last value while
Starlette's `Headers.get` takes the first — so a caller able to place a header after the gateway's
injected one chose the credential, and any later audit line would have named the other key.
Duplicates with differing values are now refused outright; identical repeats still pass.

**The principal lookup ran in a worker thread.** `SQLiteStorageBackend` opens with
`check_same_thread=True`, so arming the flag against any SQLite store raised `ProgrammingError` on
**every** request. Postgres survives it, which is exactly why no deployed environment would have
shown it, and every test substituted a fake lookup so no test would either. Only the gateway call —
the blocking `urlopen` — runs in a thread now.

**An unauthenticated pre-auth DoS.** The gateway lookup used anyio's process-wide
`CapacityLimiter(40)`; 40 concurrent requests with distinct junk keys occupy every worker thread in
the process, and failures are deliberately never cached, so each is a fresh lookup. Measured at 40
concurrent / 2.03 s wall. Quiet, too: `/health` skips the middleware, so probes stay 200 while the
API is wedged. Now a private `CapacityLimiter(8)`.

**Also fixed:** a store exception during lookup became an uncaught 500 rather than a 503; there was
no audit logging at all (now alias, principal and key *fingerprint* — never the key); the Helm
comment justified the api-only decision with a false claim about admin failing closed; and
`examples/mcp_server.py` still carried a rationale for skipping OTEL ASGI wrapping that `_serve()`
had itself falsified — #129 is now a small change rather than a blocked one.

**Accepted, not fixed.** MCP sessions are not bound to the credential that created them: the
library's ownership check only arms when `scope["user"]` is an `AuthenticatedUser`, and this
middleware writes `scope["state"]`. A leaked `mcp-session-id` lets another tenant attach to the
victim's transport. It grants **no scope escalation** — identity rebinds per request, and that is
now asserted — so this is a residual, characterized in tests rather than left implicit. Also
unfixed and pre-existing: object-level refusals name the owning tenant to a caller with no rights
to it.

`IdentityCache` was on that accepted list and then came off it. It was unbounded, and
`identity_cache_ttl_from_env` returned `float("inf")` verbatim because `inf` parses and is `>= 0` —
so a cached identity could never expire and a revoked key would work forever, silently. `nan` was
worse still: every comparison against it is False, so the expiry branch would never fire either.
Both are pre-existing, and both only became reachable when this change put the cache on the live
request path, which is what makes them this change's problem. The TTL now rejects non-finite values
toward the 60 s default (always toward revoking sooner), and the cache evicts — expired entries
first, oldest half as the fallback, because sweeping only expired entries leaves a cache of live
ones exactly as full as it was, which is a bound that cannot bind. Both fixes were confirmed by
mutation.
