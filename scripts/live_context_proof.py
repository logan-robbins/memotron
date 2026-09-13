"""Live proof: memotron.context.get_context() against the real JedAI Gateway.

Every one of get_context()'s 17 existing tests (tests/test_context.py) uses a
fake ContextTransport -- get_context() has never made a real LLM call. This
script exercises it against:

  - the real graph at ingest/.memotron/portal-kb.sqlite (copied into this
    worktree from the main checkout; both gitignored), scope
    tenant:jedai-portal-kb (24 entities / 25 facts, memory types decision 10
    / state 8 / anchor 7 as of the corpus this was measured against)
  - the real gateway transport (memotron.context.OpenAICompatibleContextTransport,
    model claude-haiku-4-5, via LITELLM_API_KEY from .env)

It proves, in order:
  1. get_context() via Memotron.profile(maintain_context=True) renders a
     real artifact from the real graph -- printed verbatim.
  2. Calling it again with nothing changed costs ZERO transport calls (the
     transport is wrapped in a call counter, not inferred from timing).
  3. Adding one fact triggers exactly one delta-only update: the prompt sent
     to the LLM is printed to show it describes only the NEW fact, not the
     whole graph.
  4. Correcting (superseding) that fact removes the old text from the
     rendered artifact and shows the new text in its place.
  5. Flooding the scope with `state` facts cannot evict the STANDING-role
     directive from Steps 3-4 -- the fixed per-role token budget
     (ContextPolicy.role_budget_shares) is a hard, code-enforced guarantee,
     not a request to the model.

Run (from anywhere, since paths below are absolute to this worktree):
    uv run --project /Users/logan.robbins/jedai/wdpr-memotron/.claude/worktrees/agent-a9a0178bd1c3c9956 \
        python /Users/logan.robbins/jedai/wdpr-memotron/.claude/worktrees/agent-a9a0178bd1c3c9956/scripts/live_context_proof.py

Requires (both gitignored, copied by hand into this worktree -- see the task
that produced this script):
  - <worktree>/.env                                    (LITELLM_API_KEY)
  - <worktree>/ingest/.memotron/portal-kb.sqlite      (the real graph)
  - <worktree>/ingest/.memotron/portal-kb.sqlite.kek  (its content DEK's KEK sidecar)
"""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

WORKTREE_ROOT = Path(__file__).resolve().parent.parent

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.config import RelationshipInstruction, default_config
from memotron.context import (
    ContextPolicy,
    ContextTransport,
    OpenAICompatibleContextTransport,
    _estimate_tokens,
    _load_artifact,
)
from memotron.models import MemoryType
from memotron.runtime import load_env_file

SOURCE_GRAPH = WORKTREE_ROOT / "ingest" / ".memotron" / "portal-kb.sqlite"
# A private WORKING COPY, not the pristine graph above: this script writes
# test facts (add_memory / correct_memory), and re-running it must start from
# the same known-good state every time rather than accumulating script runs
# on top of each other. SOURCE_GRAPH itself is never opened for writing.
WORKING_COPY = WORKTREE_ROOT / "ingest" / ".memotron" / "live-proof-working-copy.sqlite"

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="jedai-portal-kb")


class CountingTransport:
    """Wraps a real ContextTransport, counting calls and recording the last
    prompt sent -- the task requires the zero-call claim be INSTRUMENTED,
    not inferred from timing."""

    def __init__(self, inner: ContextTransport) -> None:
        self._inner = inner
        self.call_count = 0
        self.last_prompt: str | None = None
        self.last_system_prompt: str | None = None

    @property
    def identifier(self) -> str:
        return self._inner.identifier

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        self.call_count += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        return await self._inner.synthesize(prompt, system_prompt=system_prompt)


def banner(title: str) -> None:
    print()
    print("=" * 88)
    print(title)
    print("=" * 88)


def build_config():
    """default_config()'s 'default' instruction set, extended with one
    relationship type mapped to MemoryType.STATE (RECENT role) -- needed for
    the flood step. Nothing here needs to match how the real KB facts were
    extracted: memory_type is read back from each relationship's own stored
    properties (dreaming.DreamEngine.memory_type_for_relationship), not
    re-derived from whichever config happens to open the graph."""
    base = default_config()
    default_instructions = base.instruction_set("default")
    tracks = RelationshipInstruction(
        type="TRACKS",
        source_label="Entity",
        target_label="Entity",
        query="Track short-lived working state for an agent session.",
        memory_type=MemoryType.STATE,
    )
    extended = default_instructions.model_copy(
        update={"relationship_instructions": (*default_instructions.relationship_instructions, tracks)}
    )
    return base.model_copy(update={"instruction_sets": (extended,)})


async def main() -> None:
    load_env_file(WORKTREE_ROOT / ".env")

    if not SOURCE_GRAPH.exists():
        raise SystemExit(f"missing real graph: {SOURCE_GRAPH}")
    WORKING_COPY.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(SOURCE_GRAPH, WORKING_COPY)
    shutil.copyfile(Path(str(SOURCE_GRAPH) + ".kek"), Path(str(WORKING_COPY) + ".kek"))

    client = Memotron(graph_path=WORKING_COPY, config=build_config())

    # ---------------------------------------------------------------- Step 1
    banner("STEP 1: get_context() against the live gateway, first build")
    transport1 = CountingTransport(OpenAICompatibleContextTransport())
    profile1 = await client.profile(scope=SCOPE, maintain_context=True, context_transport=transport1)
    artifact1 = _load_artifact(client.graph, SCOPE)
    assert artifact1 is not None
    print(profile1.rendered_context)
    print()
    print(f"-- transport calls: {transport1.call_count}")
    print(f"-- artifact version: {artifact1.version}  degraded: {artifact1.degraded}")
    print(f"-- model_identifier: {artifact1.model_identifier}")
    print(f"-- facts folded in: {len(artifact1.fact_marks)}")

    # ---------------------------------------------------------------- Step 2
    banner("STEP 2: repeat call, nothing changed -- must be ZERO transport calls")
    transport2 = CountingTransport(OpenAICompatibleContextTransport())
    profile2 = await client.profile(scope=SCOPE, maintain_context=True, context_transport=transport2)
    print(f"-- transport calls: {transport2.call_count} (expected 0)")
    print(f"-- rendered_context identical to step 1: {profile2.rendered_context == profile1.rendered_context}")
    assert transport2.call_count == 0, "no-change path must never call the transport"
    assert profile2.rendered_context == profile1.rendered_context

    # ---------------------------------------------------------------- Step 3
    banner("STEP 3: add one fact -- delta-only update")
    added = await client.add_memory(
        subject="Live Proof Agent",
        predicate="should",
        object="verify get_context against the live gateway before shipping it",
        relationship_type="SHOULD",
        scope=SCOPE,
        confidence=0.93,
        saves_step="confirms the maintained-context path actually reaches a live LLM",
    )
    transport3 = CountingTransport(OpenAICompatibleContextTransport())
    profile3 = await client.profile(scope=SCOPE, maintain_context=True, context_transport=transport3)
    artifact3 = _load_artifact(client.graph, SCOPE)
    assert artifact3 is not None
    print(f"-- transport calls: {transport3.call_count} (expected 1)")
    print(f"-- artifact version: {artifact3.version} (expected {artifact1.version + 1})")
    print("-- delta prompt sent to the LLM:")
    print(transport3.last_prompt)
    print()
    print("-- rendered context after the update:")
    print(profile3.rendered_context)
    assert transport3.call_count == 1
    assert "verify get_context against the live gateway" in (transport3.last_prompt or "")

    # ---------------------------------------------------------------- Step 4
    banner("STEP 4: correct (supersede) that fact -- old text must leave the context")
    await client.correct_memory(
        relationship_uuid=added.relationship_uuid,
        corrected_object="always verify get_context end to end against the live gateway, not a fake transport",
        scope=SCOPE,
        reason="live-proof-supersede-demo",
    )
    transport4 = CountingTransport(OpenAICompatibleContextTransport())
    profile4 = await client.profile(scope=SCOPE, maintain_context=True, context_transport=transport4)
    artifact4 = _load_artifact(client.graph, SCOPE)
    assert artifact4 is not None
    print(f"-- transport calls: {transport4.call_count} (expected 1)")
    print(f"-- artifact version: {artifact4.version} (expected {artifact3.version + 1})")
    print("-- delta prompt sent to the LLM:")
    print(transport4.last_prompt)
    print()
    print("-- rendered context after the correction:")
    print(profile4.rendered_context)
    old_text_gone = "before shipping it" not in profile4.rendered_context
    new_text_present = "not a fake transport" in profile4.rendered_context
    print(f"-- old fact text left the context: {old_text_gone}")
    print(f"-- corrected fact text present: {new_text_present}")

    # ---------------------------------------------------------------- Step 5
    banner("STEP 5: flood with `state` facts -- STANDING directive must survive")
    # Each flood fact must be semantically DISTINCT, not just numbered: write-side
    # dedup (DedupPolicy.cosine_threshold, default 0.88) reinforces a near-duplicate
    # fact sentence into the SAME relationship rather than creating a new one --
    # confirmed empirically against the real LocalEmbeddingTransport before writing
    # this list (max pairwise cosine 0.71, comfortably under 0.88). A first version
    # of this step used one shared subject/predicate and only a trailing counter,
    # which collapsed all 30 additions into a single relationship reinforced 30
    # times (observed_count=30) instead of 30 competing facts -- a false pass that
    # would have "demonstrated" truncation for the wrong reason.
    flood_subjects = [
        "Alpha",
        "Bravo",
        "Charlie",
        "Delta",
        "Echo",
        "Foxtrot",
        "Golf",
        "Hotel",
        "India",
        "Juliett",
        "Kilo",
        "Lima",
        "Mike",
        "November",
        "Oscar",
        "Papa",
        "Quebec",
        "Romeo",
        "Sierra",
        "Tango",
        "Uniform",
        "Victor",
        "Whiskey",
        "Xray",
        "Yankee",
        "Zulu",
        "Bandit",
        "Cobra",
        "Falcon",
        "Griffin",
    ]
    flood_topics = [
        "a flaky retry in the embedding client",
        "the pruning cadence for stale rows",
        "a timeout in the coherence scan job",
        "the predicate canonicalization threshold",
        "a duplicate demotion in rollup consolidation",
        "the graph_state_hash recompute cost",
        "WAL checkpoint frequency on the sqlite store",
        "the entailment gate on a new rollup",
        "visibility_agents allowlist coverage",
        "token estimator drift on long facts",
        "a slow index seek on relationships_ctx_idx",
        "the KEK sidecar rotation runbook",
        "an unresolved entity alias collision",
        "the salience rubric's effect on formation",
        "a motive gate rejecting a candidate type",
        "the receipt ledger's Merkle checkpoint cadence",
        "a stale rollup dependency invalidation",
        "the confidence reinforcement bounded accumulation curve",
        "a crypto-shred request for a departed tenant",
        "the health gate's max_type_share threshold",
        "an actionability score below the goldset floor",
        "the corroboration margin for a disputed fact",
        "a negative-space entry for a rejected candidate",
        "the dream job scheduler's cadence drift",
        "a coherence-windup escalation counter reset",
        "the two-tier read path's index hit rate",
        "an archive-tier demotion for a cold fact",
        "the embedding identifier guard on mixed spaces",
        "a use-event attribution credit split",
        "the outcome judge's verdict on a cited memory",
    ]
    flood_count = len(flood_subjects)
    assert flood_count == len(flood_topics) == 30
    for i in range(flood_count):
        await client.add_memory(
            subject=f"Live Proof Session {flood_subjects[i]}",
            predicate="tracks",
            object=f"{flood_topics[i]} (batch {i})",
            relationship_type="TRACKS",
            scope=SCOPE,
            confidence=0.8,
        )
    transport5 = CountingTransport(OpenAICompatibleContextTransport())
    profile5 = await client.profile(scope=SCOPE, maintain_context=True, context_transport=transport5)
    artifact5 = _load_artifact(client.graph, SCOPE)
    assert artifact5 is not None
    print(f"-- transport calls: {transport5.call_count} (expected 1)")
    print(f"-- artifact version: {artifact5.version} (expected {artifact4.version + 1})")
    print(f"-- new state facts folded in: {flood_count}")
    print(f"-- STANDING lines kept: {len(artifact5.sections['standing'])}")
    print(
        f"-- RECENT lines kept: {len(artifact5.sections['recent'])} (of {flood_count}+ candidates -- must be truncated by the fixed budget share)"
    )
    print(f"-- STRUCTURE lines kept: {len(artifact5.sections['structure'])}")
    print()
    print("-- rendered context after the flood:")
    print(profile5.rendered_context)
    directive_survived = "not a fake transport" in profile5.rendered_context
    print(f"-- the corrected STANDING directive survived the flood: {directive_survived}")
    recent_all_fit = len(artifact5.sections["recent"]) >= flood_count
    if recent_all_fit:
        print(
            f"-- NOTE: all {flood_count} flood facts fit inside the {ContextPolicy().token_budget}-token "
            "default budget's 30% RECENT share once the LLM compressed each to a short line -- "
            "no truncation was forced at this budget. See STEP 6 for a tight-budget run that DOES "
            "force visible truncation, isolating the code-level `_enforce_role_budgets` guarantee "
            "from how tersely the model happens to write."
        )

    # ---------------------------------------------------------------- Step 6
    banner("STEP 6: tight token budget -- force real, visible RECENT truncation")
    # A fresh scope, so this is not entangled with the KB data or Steps 1-5's
    # cached artifact (whose watermark is keyed only by fact content, not by
    # policy -- switching policy on scope alone would silently keep serving
    # the artifact rendered under the OLD budget until the fact set changes).
    tight_scope = MemoryScope(kind=ScopeKind.USER, scope_id="live-proof-tight-budget")
    tight_policy = ContextPolicy(token_budget=400)

    await client.add_memory(
        subject="Live Proof Agent",
        predicate="should",
        object="never commit directly to main; always open a PR",
        relationship_type="SHOULD",
        scope=tight_scope,
        confidence=0.95,
    )
    transport6a = CountingTransport(OpenAICompatibleContextTransport())
    await client.profile(
        scope=tight_scope, maintain_context=True, context_policy=tight_policy, context_transport=transport6a
    )
    artifact6a = _load_artifact(client.graph, tight_scope)
    assert artifact6a is not None
    print(f"-- tight-budget baseline built, version {artifact6a.version}, transport calls {transport6a.call_count}")

    tight_flood_topics = flood_topics  # reuse the same 30 distinct, low-similarity topics
    tight_flood_subjects = flood_subjects
    for i in range(len(tight_flood_subjects)):
        await client.add_memory(
            subject=f"Tight Budget Session {tight_flood_subjects[i]}",
            predicate="tracks",
            object=f"{tight_flood_topics[i]} (batch {i})",
            relationship_type="TRACKS",
            scope=tight_scope,
            confidence=0.8,
        )
    transport6b = CountingTransport(OpenAICompatibleContextTransport())
    profile6b = await client.profile(
        scope=tight_scope, maintain_context=True, context_policy=tight_policy, context_transport=transport6b
    )
    artifact6b = _load_artifact(client.graph, tight_scope)
    assert artifact6b is not None
    print(f"-- transport calls: {transport6b.call_count} (expected 1)")
    print(f"-- token budget: {tight_policy.token_budget} (vs. default {ContextPolicy().token_budget})")
    print(f"-- STANDING lines kept: {len(artifact6b.sections['standing'])}")
    print(f"-- RECENT lines kept: {len(artifact6b.sections['recent'])} of {len(tight_flood_subjects)} offered")
    print()
    print("-- rendered context under the tight budget:")
    print(profile6b.rendered_context)
    tight_directive_survived = "commit directly to main" in profile6b.rendered_context
    within_budget = _estimate_tokens(profile6b.rendered_context) <= tight_policy.token_budget
    truncated = 0 < len(artifact6b.sections["recent"]) < len(tight_flood_subjects)
    print(f"-- STANDING directive survived: {tight_directive_survived}")
    print(f"-- rendered text within the {tight_policy.token_budget}-token budget: {within_budget}")
    print(f"-- RECENT was actually truncated by the code (0 < kept < offered): {truncated}")

    banner("SUMMARY")
    print(f"step1 zero-call baseline built:        version {artifact1.version}")
    print(f"step2 no-change transport calls:       {transport2.call_count} (must be 0)")
    print(f"step3 delta-add transport calls:       {transport3.call_count} (must be 1)")
    print(f"step4 delta-supersede transport calls: {transport4.call_count} (must be 1)")
    print(f"step5 delta-flood transport calls:     {transport5.call_count} (must be 1)")
    print(f"step4 old text evicted / new text present: {old_text_gone} / {new_text_present}")
    print(f"step5 standing directive survived the flood: {directive_survived}")
    print(f"step5 recent lines kept (of {flood_count} offered): {len(artifact5.sections['recent'])}")
    print(f"step6 tight-budget standing directive survived: {tight_directive_survived}")
    print(f"step6 tight-budget rendered text within budget: {within_budget}")
    print(
        f"step6 tight-budget recent lines kept (of {len(tight_flood_subjects)} offered): {len(artifact6b.sections['recent'])}"
    )
    print(f"step6 tight-budget truncation actually forced: {truncated}")


if __name__ == "__main__":
    asyncio.run(main())
