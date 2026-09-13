# Memotron Patent Angle Review

This is an engineering patentability worksheet, not legal advice. It identifies
technical areas that appear worth attorney review based on the repository docs,
implementation, tests, and a current prior-art scan.

## Task Status

1. Inventory local documentation and implementation seams. [done]
2. Compare against external commercial, academic, and patent prior art. [done]
3. Identify candidate patent families and novelty risks. [done]
4. Update README pointer to this review. [done]
5. Run local uv-based verification. [done]

## Executive Assessment

The best patent story is not "agent memory" or "dreaming." Those are crowded.
Memotron's stronger angle is a policy-bound memory lifecycle: named Motives
drive what an agent is allowed to remember, which memory types may be written,
how semantic dedup and salience gates behave, how tenant/scope governance applies,
how context-visible memory is compressed into themes, and how the system proves
memory health over time.

Recommended primary filing theme:

> Motive-governed, typed, auditable autonomous memory formation and lifecycle
> management in a scoped temporal property graph.

Use recursive theme demotion, typed retrieval budgeting, governance gates,
crypto-shred lineage preservation, memory-health proof metrics, and interop
policy routing as dependent claims or continuation material.

## Prior-Art Boundary

Avoid broad independent claims on these areas:

- Background memory consolidation or "dreaming."
- Agent memory stores.
- Semantic extraction from conversation.
- Temporal knowledge graphs for agent memory.
- Reflection trees or higher-level memory synthesis.
- OpenAI-compatible memory proxying.
- Multimodal normalization into text before extraction.

Why these are crowded:

- Anthropic Dreams reads an existing memory store and past transcripts, then
  produces a reorganized memory store with duplicates merged, stale or contradicted
  entries replaced, and reviewable output. Source:
  https://platform.claude.com/docs/en/managed-agents/dreams
- Anthropic Managed Agents memory stores preserve cross-session preferences,
  project conventions, prior mistakes, and domain context. Source:
  https://platform.claude.com/docs/en/managed-agents/memory
- Anthropic's memory tool exposes file-backed persistent create/read/update/delete
  memory across conversations. Source:
  https://platform.claude.com/docs/en/agents-and-tools/tool-use/memory-tool
- OpenAI's ChatGPT dreaming memory updates stale memories over time and improves
  preference recall. Source:
  https://openai.com/index/chatgpt-memory-dreaming/
- Sleep-time Compute provides academic support for offline context processing
  before future queries. Source: https://arxiv.org/abs/2504.13171
- Generative Agents stores experience records, synthesizes higher-level
  reflections over time, and retrieves memories dynamically. Source:
  https://arxiv.org/abs/2304.03442
- Zep/Graphiti describes temporally aware knowledge graph memory with historical
  relationships for enterprise agents. Source: https://arxiv.org/abs/2501.13956
- Mem0 describes dynamic extraction, consolidation, retrieval, and graph-based
  memory representations. Source: https://arxiv.org/abs/2504.19413
- Oracle AI Agent Memory publicly positions a governed unified memory substrate
  with working, semantic, episodic, and procedural memory, tenant isolation,
  vector search, graph traversal, auditing, and encryption. Source:
  https://blogs.oracle.com/developers/oracle-ai-agent-memory-a-governed-unified-memory-core-for-enterprise-ai-agents
- GitHub Copilot agentic memory is repo-scoped, validated against the current
  codebase, shared across Copilot features, and expires after 28 days. Source:
  https://github.blog/changelog/2026-01-15-agentic-memory-for-github-copilot-is-in-public-preview/
- Google patent publication US20250088357A1 discloses batch summarization and
  updating of an agent memory snippets database. Source:
  https://patents.google.com/patent/US20250088357A1
- Google patent publication US20250259042A1 discloses ranking and deduplication
  across specialized memory channels in multi-agent systems. Source:
  https://patents.google.com/patent/US20250259042A1

## Candidate Claim Families

### 1. Motive-Governed Memory Formation

Strength: high.

Memotron's `Motive` is a named policy object, not just prompt instructions.
It bundles a goal, allowed memory types, prompt profile/override, salience rubric,
dedup threshold, retrieval budget share, and optional governance policy.

Local anchors:

- `src/memotron/config/`: `Motive`, `MemoryBank`,
  `DreamConfig.resolve_motive`.
- `src/memotron/memory_bank.py`: built-in persona Motives.
- `src/memotron/dreaming/`: formation resolves the active Motive per job or
  episode, then applies prompt, rubric, type filter, governance, and dedup behavior.

Claim shape:

1. Receive an episode for autonomous memory formation in a tenant/scope.
2. Resolve a named Motive from a tenant, agent, job, scope, or request policy.
3. Use the Motive to determine allowable memory types, salience thresholds,
   semantic dedup thresholds, retrieval-budget shares, and governance controls.
4. Extract candidate memories.
5. Drop or transform candidates whose types or salience do not match the Motive.
6. Materialize accepted memories into a scoped temporal graph with audit records
   explaining selected, dropped, reinforced, superseded, or gated writes.

Novelty thesis:

Competitors expose memory instructions, profiles, or generic extraction policies,
but the stronger distinction is the single reusable, named, auditable policy object
that governs autonomous writes across type, salience, dedup, retrieval, and
governance. Frame this as goal-conditioned memory formation with auditable write
policy, not as prompt steering.

Risk:

Oracle governance and Anthropic Dreams instructions are nearby. Claims should
require the combination of named Motive plus type filters plus write-side decisions
plus persisted audit lineage.

### 2. Typed Temporal Truth Lifecycle

Strength: medium-high.

Memotron has first-class memory types: identity, preference, requirement,
directive, state, decision, incident, and theme. These are not only labels; they
drive cardinality, dedup aggressiveness, retrieval budget allocation, governance,
and pruning behavior.

Local anchors:

- `src/memotron/models/`: `MemoryType`.
- `src/memotron/dreaming/`: truth keys, reinforcement, supersession, valid
  time, transaction-time audit fields, and invalidate-don't-delete behavior.
- `src/memotron/client.py`: current and historical visibility rules.

Claim shape:

1. Classify extracted memory into a memory type.
2. Resolve cardinality and lifecycle policy for that type.
3. Compute a truth key that is narrower for single-active memory and broader for
   multi-active memory.
4. Reinforce matching or semantically equivalent current facts.
5. Supersede contradictory single-active facts while retaining historical validity
   and evidence.
6. Use memory type to control retrieval priority, pruning, and governance.

Novelty thesis:

CoALA provides a taxonomy and Oracle uses the four broad memory classes, but
Memotron's narrower enterprise types are operational lifecycle controls. The
best claims should tie type classification to truth-key structure, supersession,
retrieval allocation, and governance, not merely to category labels.

Risk:

Temporal KG and memory taxonomy are known. Avoid claiming generic bi-temporal KG
or generic memory type classification.

### 3. Recursive Theme Consolidation With Context Demotion

Strength: medium-high.

The system clusters context-visible active facts, synthesizes a `THEME`
relationship, records evidence pointers, and demotes child facts from default
profile context while keeping them active, searchable, and auditable.

Local anchors:

- `src/memotron/dreaming/`: thematic pass, cluster synthesis,
  `derived_from`, `theme_depth`, `active_in_context=False`, and
  `summarized_by`.
- `src/memotron/client.py`: profile excludes demoted members by default while
  evidence and historical search remain available.

Claim shape:

1. Select active, context-visible graph facts for a scope.
2. Cluster facts by semantic similarity.
3. Generate a theme fact that summarizes a cluster.
4. Store the theme as a normal typed graph memory with evidence pointers to
   member facts.
5. Mark member facts as not active in context without deleting, pruning, or
   losing evidence.
6. On later passes, allow recursive theme formation subject to a depth cap.

Novelty thesis:

Reflection and consolidation are known. The defensible feature is the split
between total queryable memory and context-visible memory: raw facts are retained
for evidence and `as_of` search but excluded from default context through a
demotion flag linked to a theme.

Risk:

Generative Agents and Anthropic Dreams are conceptually close. Claims need the
active-context demotion plus audit/evidence retention mechanics.

### 4. Budget-Aware Typed Retrieval With Identity Floor and JIT References

Strength: medium.

Memotron renders profiles under a token budget, groups by memory type, orders
themes first, applies per-type caps, reserves an identity/requirement floor, and
can return lightweight references for overflow facts that can be expanded with
evidence tools.

Local anchors:

- `src/memotron/client.py`: `profile(token_budget=...)`,
  `_budget_shares_for_types`, `_apply_token_budget`,
  `_render_profile_context_typed`, and reference rendering.
- `src/memotron/config/`: `ProfilePolicy`.

Claim shape:

1. Retrieve scoped candidate memories.
2. Group candidates by memory type and rank by value.
3. Allocate a token budget across types using Motive retrieval shares.
4. Enforce a floor for identity/requirement facts.
5. Render in type order with theme facts first.
6. Return overflow as expandable references rather than silently dropping facts.

Novelty thesis:

Context budgeting and just-in-time retrieval are known, but tying retrieval shares
to the same named Motive used for autonomous write formation is a useful dependent
claim.

Risk:

Anthropic has strong context-engineering prior art around JIT references and tight
context. Keep this dependent on Motives and typed memory lifecycle.

### 5. Memory Health Proof and Soft-Cap Governance

Strength: medium.

Memotron calculates health signals such as compression ratio, semantic dedup
rate, per-type active counts, context-visible count, theme count, and demoted count.
It can then apply a soft-cap backstop after dedup and thematic consolidation by
pruning the lowest-value rows using salience, confidence, recency, and observation
count, with dream-agent approval.

Local anchors:

- `src/memotron/client.py`: `memory_evolution`.
- `src/memotron/dreaming/`: `_apply_soft_cap`.
- `src/memotron/models/`: `MemoryEvolutionProof`.

Claim shape:

1. Compute context-visible memory count separately from total queryable memory.
2. Compute compression and semantic-dedup metrics across a scope.
3. Detect type-specific or global growth above a configured soft cap.
4. Rank candidate facts by a multi-factor value score.
5. Prune only after higher-level compression mechanisms have run.
6. Record an approval/audit decision for each cap-triggered prune.

Novelty thesis:

This is less about a cap and more about a proof/control loop for autonomous memory
quality: raw episodes can grow while context-visible memory remains bounded and
auditable.

Risk:

Memory metrics and pruning are natural engineering choices. Stronger as dependent
claims attached to theme demotion and Motive-driven lifecycle policy.

### 6. Governed Autonomous Write Gates

Strength: medium.

Memotron connects governance to memory formation: high-sensitivity policy
redacts before extraction and again after extraction; untrusted directive and
requirement writes require dream-agent approval; crypto-shred destroys content
keys while preserving audit lineage.

Local anchors:

- `src/memotron/dreaming/`: pre/post extraction redaction and untrusted write
  gate.
- `src/memotron/client.py`: governance key provisioning, crypto-shred, and
  redact-version.
- `src/memotron/redaction.py`: redact, mask-last-4, and SHA-256 hash strategies.

Claim shape:

1. Resolve a governance policy from Motive or tenant/scope configuration.
2. Redact sensitive text before memory extraction.
3. Redact extracted memory fields before materialization.
4. Detect untrusted-source directive or requirement writes.
5. Require approval before those high-risk writes become active memory.
6. Preserve audit skeletons while making sensitive content unrecoverable via key
   destruction.

Novelty thesis:

The strongest point is not redaction or crypto-shredding alone. It is applying
governance at autonomous memory-write time, with Motive/type/scope determining
which gates apply.

Risk:

Crypto-shredding, redaction, retention, and enterprise audit are known. Treat as
dependent claims.

### 7. Interop Router as Policy Enforcement Point

Strength: low-medium.

Memotron's router wraps an OpenAI-compatible upstream, resolves tenant/agent/
scope/Motive policy, injects memory context, strips private metadata, and rejects
cross-scope requests.

Local anchors:

- `src/memotron/interop.py`: `MemoryRouter`.
- `src/memotron/config/`: `MemoryControlPlane` and `EffectiveMemoryPolicy`.

Claim shape:

1. Receive an OpenAI-style request with private memory metadata.
2. Resolve an effective policy for tenant, agent, scope, dream mode, prompt pack,
   and Motive.
3. Reject requests whose resolved scope does not match router authority.
4. Inject scoped memory context before forwarding upstream.
5. Strip private memory metadata from the forwarded request.
6. Return injection metadata for audit.

Novelty thesis:

The router is more than convenience proxying when it is a policy enforcement point
for scoped, Motive-driven memory.

Risk:

supermemory advertises OpenAI-compatible memory routing, and policy gateway
patterns are common. Keep this as a dependent or implementation claim.

### 8. Cross-Artifact Behavioral Coherence (escalation-windup detection)

Strength: high. **New white-space — strongest net-new angle since the Motive family.**

Every family above, and the replayable-receipts provisional, reconciles *within
the memory store*. But an agent's behavior is governed by a heterogeneous set of
persistent artifacts — memory, self-/user-authored skills, and files — authored on
different cadences and carrying different execution precedence (a procedural skill
is executed and dominates advisory memory). When a stale skill silently overrides
repeated feedback, the memory system faithfully *escalates* the feedback memory
cycle after cycle and never converges, because the contradiction lives in an
artifact the reconciler cannot see. This is a real, observed operator incident
(see the feedback that motivated WS-10), and it is distinct from — and deeper than
— the founding 271-fact escalation, which was a memory-internal hygiene problem.

Local anchors:

- `src/memotron/coherence.py`: `CoherenceScanner` (directive projection across
  artifact classes, semantic-identity matching, windup + contradiction detection,
  culprit attribution); `ArtifactSource` / `StaticArtifactSource`.
- `src/memotron/models/`: `ArtifactClass`, `DirectiveStance`, `Directive`,
  `PersistentArtifact`, `CoherenceIncident*`, `CoherenceReport`;
  `DreamJobKind.COHERENCE`.
- `src/memotron/config/`: `CoherencePolicy` (identity threshold,
  escalation-cycle threshold, per-class execution precedence, participating types).
- `src/memotron/dreaming/`: `DreamEngine.scan_coherence`, `_run_coherence`
  dispatch, and the anti-windup hold in `_apply_reinforce` (confidence freeze on
  `coherence_hold`).
- `src/memotron/client.py`: `register_artifact_source`, `run_coherence_scan`,
  `coherence_incidents`.

Claim shape (two inventions; see `PATENT_COHERENCE_SPEC.md`):

- *Invention I — cross-artifact coherence:* project directives from a memory
  store and from a non-memory artifact into a common representation with semantic
  identity + execution precedence; detect a contradiction spanning two artifact
  classes; attribute the higher-precedence artifact as governing; record a
  decision proposing repair of the governing artifact.
- *Invention II — escalation-windup detection / anti-windup:* use the monotonic
  strengthening of a memory directive across >= K feedback cycles as the detector;
  identify a higher-precedence, staler non-memory artifact on the same subject;
  raise an incident naming the suspected governing artifact; and apply an
  anti-windup hold that records further feedback as evidence without strengthening
  the directive.

Novelty thesis:

The defensible, non-obvious step is cross-applying cache-coherence (a shared
directive line with per-copy precedence/staleness), truth-maintenance
(dependency-directed culprit attribution), and control theory (anti-windup) to an
agent's *heterogeneous persistent-context artifacts of differing class and
execution priority*. Invention II is the sharpest: **no prior art treats memory
escalation under repeated feedback as a detector of a contradiction residing in a
non-memory artifact.** Frame as behavioral-coherence reconciliation across
artifact classes, not as "memory consolidation."

Risk:

Belief revision/TMS and cache coherence are mature in their own domains. Keep the
independent claims tied to (a) heterogeneous artifact *classes* with execution
precedence, (b) culprit attribution by precedence + staleness, and (c) the
escalation-as-signal + anti-windup hold — not to generic contradiction detection.

## Proposed Filing Strategy

Primary family:

- Independent claim: Motive-governed autonomous memory formation over a scoped
  temporal graph.
- Dependent claims: allowed memory type filtering, per-Motive salience rubric,
  per-Motive dedup threshold, governance resolution, audit decisions for drops and
  merges, typed truth-key cardinality, supersession lineage, and retrieval budget
  coupling.

Second primary family (own application — see `PATENT_COHERENCE_SPEC.md`):

- Independent claim: cross-artifact behavioral coherence — reconciliation across
  heterogeneous persistent-context artifact classes with execution precedence.
- Independent claim: escalation-windup detection + anti-windup hold (memory
  escalation under repeated feedback as a detector of a non-memory contradiction).

Continuation candidates:

- Recursive thematic consolidation with context demotion and evidence retention.
- Memory health proof plus soft-cap pruning after dedup and consolidation.
- Governance gates for untrusted high-risk autonomous writes.
- Policy-enforcing interop router.
- Coherence incidents emitted as replayable, tamper-evident receipts (bridges the
  coherence family to the replay-receipts provisional).

## Claim Language to Avoid

Avoid saying the invention is:

- An AI agent memory store.
- A dreaming/background consolidation process.
- A temporal knowledge graph.
- Semantic deduplication of memories.
- A memory router or proxy.
- Multimodal-to-text memory ingestion.

Those are all too broad and heavily exposed.

## Better Claim Language

Prefer phrases like:

- "named memory-formation policy object"
- "goal-conditioned autonomous memory write policy"
- "type-constrained memory materialization"
- "audit-recorded write-side memory decision"
- "context-visible demotion while preserving historical evidence"
- "scope-bound temporal truth slot"
- "Motive-derived retrieval budget allocation"
- "governance gate for untrusted autonomous memory writes"

## Verification

`nohup uv run pytest -vv` passed locally: 294 tests passed (includes WS-10
cross-artifact coherence, WS-11 replay receipts, and WS-12 verifiable erasure).
