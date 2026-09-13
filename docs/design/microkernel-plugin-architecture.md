# Microkernel / plugin architecture — assessment

**Status: ASSESSED 2026-08-31, DEFERRED by decision.** Not scheduled. Revisit when the
preconditions at the end are met — they are written to be checkable rather than felt.

Unnumbered on purpose: the files beside it in `docs/design/` are numbered by their driving
issue, and this has none. It is a thought experiment recorded so the next person spends the
reading rather than the measurement.

**Terminology, because this repo uses "plugin" for two unrelated things.** This document is
about *internal* architecture — a small kernel plus swappable capability implementations. It
is **not** about `docs/spikes/claude-plugin/`, which is a Claude Code distribution plugin
(hooks, skills, a marketplace entry). Nothing here affects that spike.

---

## Why this was deferred rather than rejected

The idea is sound and the repo is closer to it than expected. It is deferred because the
sequencing is wrong today, for two reasons stated plainly:

1. **The product does not form memory** (T1-1, #124) and **cannot be deployed** (#130). A
   structural programme that runs ahead of those makes a broken system more elegantly
   broken.
2. **This repo's measured track record on structural rework is poor.** Four planned
   improvements were killed on measurement during the readiness phase: Phase B2, the three
   Phase 6 decoupling candidates, god-method decomposition, and typed-contexts-as-a-route-
   to-it. The measured ceiling on repartitioning `dreaming/` was **~30% of call weight
   against 54.8% today**, for 25 relocations that dissolve a coherent concern. A microkernel
   is the most ambitious version of exactly that class of work, so the prior against it is
   the strongest prior this repo has produced.

Deferral is therefore the recommendation *on this repo's own evidence*, not caution.

---

## The measurement that reframes the question

Re-derive with `uv run python scripts/verify/coupling_report.py`. Observed 2026-08-31 —
note that this tool had itself been dead since `client.py` became a package and was repaired
the same day (`150a16c`), so treat any figure recorded before that date as unmeasured.

| metric | before the split | now | change |
|---|---|---|---|
| client storage call sites | 279 | **279** | none |
| distinct backend members reached | 72 | **72** | none |
| cross-object private reaches | 11 | **11** | none |
| mypy modules strict | 52 | 119 | better |
| mypy suppressed codes | 72 | **90** | **worse** |
| `storage/postgres` coverage | 0% | 86.8% | better |

**The split changed the file layout and did not change the coupling at all.** Sixteen files
where there was one, still reaching 72 distinct storage members 279 times. The five unchanged
numbers are simultaneously:

- an *independent confirmation* of the readiness phase's zero-change property — pure moves
  preserve call sites exactly, measured by a tool that knows nothing about bytecode identity;
- the central fact for this question. **Any argument that further restructuring improves
  matters has to move that 279, and should be measured before it is believed.**

The +18 suppressed error codes are debt the split added through per-mixin
`disable_error_code` overrides. Recorded rather than hidden.

---

## Finding: the Protocol layer already exists. The missing pieces are not the obvious ones.

There are **14 `Protocol`s** in `src/memotron/`. Nine are genuine capability seams:

| Protocol | file | implementations | hermetic fake? | real impl? |
|---|---|---|---|---|
| `ExtractionTransport` | `extraction.py:218` | 3 — `InstructionalExtractor`, `RuleBasedExtractionTransport`, `OpenAICompatibleExtractionTransport` | yes | yes |
| `DreamAgentTransport` | `agents.py:71` | 2 — `LocalDreamAgentTransport`, `OpenAICompatibleDreamAgentTransport` | yes* | yes |
| `EmbeddingTransport` | `embedding.py:98` | 2 — `LocalEmbeddingTransport`, `OpenAICompatibleEmbeddingTransport` | yes | yes |
| `ArtifactSource` | `coherence.py:79` | 2 — `StaticArtifactSource`, `GraphArtifactSource` | yes | yes |
| `MultimodalNormalizer` | `multimodal.py:238` | 1 — `LocalMultimodalNormalizer` | yes | **no** |
| `UpstreamTransport` | `interop.py:728` | 1 — `EchoUpstreamTransport` | yes | **no** |
| `SynthesisTransport` | `synthesis.py:41` | 1 — `OpenAICompatibleSynthesisTransport` | **no** | yes |
| `ContextTransport` | `context.py:510` | 1 — `OpenAICompatibleContextTransport` | **no** | yes |
| `KeyManager` | `crypto.py:174` | 1 — `LocalKeyManager` | yes | **no** |

Plus two storage implementations behind one contract (`sqlite/`, `postgres/`) — the only seam
with two *production* implementations, and the one that diverged.

\* `LocalDreamAgentTransport` is a fake that **approves unconditionally**, which is a fake that
cannot express the interesting case. See gap 2 below.

**The lopsidedness is the finding.** Five of the nine seams have exactly one implementation,
and they fail in two opposite directions:

- `MultimodalNormalizer`, `UpstreamTransport`, `KeyManager` have **only a hermetic
  implementation** — so the capability is unusable in production. `KeyManager` is why #130 is a
  deployment blocker, and `multimodal.py:24` says so out loud: *"MLLM SEAM: in production, swap
  LocalMultimodalNormalizer for a MlmmMultimodalNormalizer"* — which does not exist.
- `SynthesisTransport`, `ContextTransport` have **only a network implementation** — so the
  capability cannot be exercised hermetically at all.

A Protocol with one implementation has never been tested as an abstraction; it is a type
annotation with aspirations. That is the honest state of most of this "plugin layer", and it is
why adding more seams is not the lever.

The remaining five (`Composed*` in `client/`, `dreaming/`, `agent_memory/`, `storage/sqlite/`,
`storage/postgres/`) are **typing artifacts from the split, not extension points**. Do not
count them as seams.

So the interface layer of a plugin architecture is present. Three things are absent, and the
third is the one worth caring about.

### 1. There is no registry — selection happens by ambient environment variable

`runtime._env_endpoint` decides which transport you get by reading `LITELLM_API_KEY` then
`OPENAI_API_KEY` from the process environment. That is the opposite of a registry, and on
2026-08-31 it produced a real defect: whether the *hermetic test suite* ran against a fake or
a real, billed model was decided by whatever happened to be in the developer's shell. Off VPN
it hung; on VPN it would have passed, slowly and nondeterministically, inside the lane every
other gate treats as its deterministic baseline. See `70cf0c9`.

The same precedence probe is **duplicated three times** — `runtime.py:71,77`,
`adoption.py:418,420`, `admin_server/_tenant.py:45,47` — which is what a missing registry
looks like in practice. Explicit selection makes that whole class of bug unrepresentable.

### 2. There is no injection at the fake boundary

`LocalDreamAgentTransport` (`agents.py:79-91`) **approves unconditionally**. This is why T1-1
has a validated one-line fix and *no test can reproduce the defect it fixes*. That is not a
testing gap; it is a plugin-boundary gap — the fake is hardcoded rather than selected, so
there is no way to ask for a refusing transport.

### 3. There is no conformance suite — and this is the real gap

Two implementations of one storage contract diverged, and the suite did not notice.

T0-4: Postgres omitted the `MENTIONS` filter SQLite applied. Consequences included
`search()` raising `KeyError: 'fact'` and `epoch_content_digest` becoming unstable against
itself, voiding replay verification on the production backend. **`test_storage_backend_parity.py`
— measured 2026-08-31 at 3,420 lines and 103 collected tests — passed and covered none of it.**

(The register records that file as 3,310 lines / 117 tests, from 2026-08-27. Both figures are
measured here rather than carried over; the test count has *fallen*, which is consistent with
parametrisation changes and is not itself evidence of anything. Cited so nobody re-derives the
discrepancy and mistakes it for a finding.)

That is the decisive argument about plugin architectures here: **a plugin boundary whose
implementations are not driven through identical contract tests is not isolation, it is
unmonitored divergence.** The repo already has two storage backends and, until
`tests/test_scoped_read_parity.py` landed, had no conformance guarantee between them. Adding
more seams without conformance would multiply that failure mode, not contain it.

---

## Where a microkernel would genuinely pay

Each of these is already demanded by queued work, which is what distinguishes it from
speculation:

- **Key managers (#123, #77, #130).** One Protocol, one implementation, and a
  `GcpKmsKeyManager` is required. The second real implementation is what forces a seam to be
  honest; a Protocol with a single implementation has never been tested as an abstraction.
- **Transports.** Fixing selection fixes testability and observability together, and removes
  the triplicated precedence probe.
- **Instrumentation (#129).** A kernel boundary is where you instrument once instead of at
  200 call sites. Adopting the org OTEL chassis is markedly cheaper with a boundary than
  without one.

## Where it would be motion rather than progress

- **The 279 storage call sites.** A plugin boundary does not reduce them — the client
  legitimately needs the graph. Measured: the split moved this number by zero, and nothing
  about a registry moves it either.
- **`models` (fan-in 80) and `config` (fan-in 42).** Shared vocabulary, correctly shared.
  Splitting these to satisfy a layering diagram is the distributed-monolith trap.
- **Surfaces as plugins** (governance MCP, agent-memory MCP, admin HTTP, SDK). They are not
  varying independently: there is one deployment, and per #130 it cannot currently boot.
  Plugin machinery for one consumer each is cost with no option value.

Fan-in, for the record (`grep` over `src/memotron/`, 2026-08-31): `models` 80, `config` 42,
`crypto` 25, `embedding` 20, `storage` 18, `extraction` 16, `agents` 12, `synthesis` 11,
`runtime` 6, `context` 3.

---

## Recommendation

**Do not rewrite toward a microkernel. When this is picked up, build the two missing pieces
of the one that already exists:**

1. **A capability registry** replacing ambient environment resolution, with explicit
   selection and a single precedence rule instead of three copies.
2. **A per-Protocol conformance suite** — every implementation of a Protocol driven through
   the same contract tests, so a second implementation cannot silently diverge from the first.

Both add *checks* rather than moving code, which is precisely why they survive the prior in
"Why this was deferred". Neither requires a rewrite, and each is independently justified.

## Preconditions for revisiting — checkable, not felt

Revisit when **all** of these hold:

1. **T1-1 is fixed and the product forms memory** (#124). Until then the system's core
   promise is unmet and structure is the wrong variable.
2. **#130 is resolved and a deployment can boot** with `operationalStore` enabled — i.e. a
   durable `KeyManager` implementation exists. This *also* delivers the second implementation
   that would make the `KeyManager` seam real, so it is a precondition and a down payment.
3. **T0-7 is closed** (#122), so the Postgres audit plane can detect tampering. Restructuring
   storage while its tamper detection is blind is unguarded work.
4. **`coupling_report.py` shows the 279 has actually moved** under some cheaper change, or a
   measured estimate exists for how far a registry would move it. If nobody can produce a
   number, the prior stands.

## What would change the answer entirely

This assessment assumes **one deployment and no third party extending Memotron.** If the
goal becomes other teams shipping their own storage backends, extractors or key managers
against a stable published API, the calculus inverts: the plugin boundary becomes the product,
and its cost is justified by consumers rather than by internal tidiness. That is a product
decision, not an architectural one, and it should be made explicitly rather than discovered.

## The condition under which this write-up is wrong

Every structural claim here is read from source or measured by a committed script, and the
commands are named so they can be re-run. But the central recommendation rests on a
*judgement* — that conformance and registry work delivers more per unit risk than
repartitioning — and that judgement is calibrated on this repo's four killed improvements. If
a cheap experiment moved the 279 materially, the prior should be updated and this document
reopened. It is the claim most worth attacking.
