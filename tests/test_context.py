"""get_context(): the incremental, LLM-maintained orientation artifact.

Covers `memotron.context.get_context` directly (unit level — a fake
transport, `MemoryProfileFact` objects constructed by hand, a real
`PropertyGraphStore` only for the persistence side) plus one integration test
proving `Memotron.profile(maintain_context=True)` wires the whole thing
end to end through `add_memory()` (client-managed, immediate materialization
— no dream job needed to get real facts into the graph).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import Memotron, MemoryScope, ScopeKind
from memotron.config import DreamInstructionSet, DreamJob, NodeInstruction, RelationshipInstruction, default_config
from memotron.context import (
    ContextPolicy,
    ContextRole,
    get_context,
    role_for_memory_type,
)
from memotron.graph import PropertyGraphStore
from memotron.models import DreamJobKind, MemoryProfileFact, MemoryType, RelationshipStatus

NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


class StubContextTransport:
    """Deterministic in-test ContextTransport: records calls, replays responses.

    Mirrors `tests/test_theme_synthesis.py`'s `StubSynthesisTransport` shape
    so the same reviewer mental model applies to both transport-adjacent
    fakes in this codebase.
    """

    def __init__(self, responses: list[str]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str]] = []

    @property
    def identifier(self) -> str:
        return "stub-context:test@v1"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        self.calls.append((prompt, system_prompt))
        if not self.responses:
            raise AssertionError("stub context transport exhausted")
        return self.responses.pop(0)


class BoomTransport:
    """Always raises — simulates a gateway outage / malformed envelope."""

    identifier = "boom:test"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        raise ValueError("gateway unavailable")


def make_scope(scope_id: str = "alice") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def make_fact(
    scope: MemoryScope,
    *,
    uuid: str,
    text: str,
    memory_type: str,
    confidence: float = 0.9,
    observed_count: int = 1,
    pinned: bool = False,
    last_seen_at: datetime | None = None,
) -> MemoryProfileFact:
    return MemoryProfileFact(
        relationship_uuid=uuid,
        relationship_type="FACT",
        fact=text,
        scope=scope,
        subject="subject",
        predicate="predicate",
        object="object",
        confidence=confidence,
        status=RelationshipStatus.ACTIVE,
        created_by="test",
        memory_type=memory_type,
        pinned=pinned,
        observed_count=observed_count,
        last_seen_at=last_seen_at,
        valid_from=last_seen_at,
        created_at=last_seen_at,
    )


def sectioned_response(*, standing=(), recent=(), structure=()) -> str:
    return json.dumps({"standing": list(standing), "recent": list(recent), "structure": list(structure)})


# ---------------------------------------------------------------------------
# ContextPolicy validation
# ---------------------------------------------------------------------------


def test_context_policy_defaults_sum_to_one() -> None:
    policy = ContextPolicy()
    assert sum(policy.role_budget_shares.values()) == pytest.approx(1.0)
    assert set(policy.role_budget_shares) == {role.value for role in ContextRole}


def test_context_policy_rejects_shares_not_summing_to_one() -> None:
    with pytest.raises(ValueError, match=r"sum to 1\.0"):
        ContextPolicy(role_budget_shares={"standing": 0.5, "recent": 0.5, "structure": 0.5})


def test_context_policy_rejects_missing_role() -> None:
    with pytest.raises(ValueError, match="exactly the keys"):
        ContextPolicy(role_budget_shares={"standing": 0.5, "recent": 0.5})


def test_context_policy_rejects_non_positive_token_budget() -> None:
    with pytest.raises(ValueError, match="token_budget"):
        ContextPolicy(token_budget=0)


def test_role_for_memory_type_mapping() -> None:
    assert role_for_memory_type("directive") is ContextRole.STANDING
    assert role_for_memory_type("preference") is ContextRole.STANDING
    assert role_for_memory_type("requirement") is ContextRole.STANDING
    assert role_for_memory_type("anchor") is ContextRole.STANDING
    assert role_for_memory_type("state") is ContextRole.RECENT
    assert role_for_memory_type("decision") is ContextRole.STRUCTURE
    assert role_for_memory_type("incident") is ContextRole.STRUCTURE
    assert role_for_memory_type(None) is ContextRole.STRUCTURE
    assert role_for_memory_type("something-unclassified") is ContextRole.STRUCTURE


# ---------------------------------------------------------------------------
# get_context: the seven required scenarios
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_scope_builds_trivial_artifact_with_zero_llm_calls(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    transport = StubContextTransport([])

    artifact = await get_context(graph=graph, scope=scope, facts=[], transport=transport, now=NOW)

    assert transport.calls == []
    assert artifact.version == 1
    assert artifact.degraded is False
    assert "no facts recorded" in artifact.rendered_text

    # Repeating the empty call must also cost zero LLM calls and return the
    # SAME persisted artifact (not silently re-create a v2).
    transport2 = StubContextTransport([])
    again = await get_context(graph=graph, scope=scope, facts=[], transport=transport2, now=NOW)
    assert transport2.calls == []
    assert again.version == 1
    assert again.watermark == artifact.watermark


@pytest.mark.asyncio
async def test_no_change_returns_stored_verbatim_with_zero_transport_calls(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive = make_fact(scope, uuid="u-directive", text="Always use uv, never bare python", memory_type="directive")

    first_transport = StubContextTransport([sectioned_response(standing=["Always use uv, never bare python"])])
    built = await get_context(graph=graph, scope=scope, facts=[directive], transport=first_transport, now=NOW)
    assert len(first_transport.calls) == 1
    assert "Always use uv, never bare python" in built.rendered_text

    # Same facts, second call: MUST be zero transport calls, and MUST return
    # the identical stored artifact (same version, same text) — this is the
    # continuity property resume-after-compaction depends on.
    no_op_transport = StubContextTransport([])
    served = await get_context(
        graph=graph, scope=scope, facts=[directive], transport=no_op_transport, now=NOW + timedelta(minutes=5)
    )
    assert no_op_transport.calls == []
    assert served.version == built.version
    assert served.rendered_text == built.rendered_text
    assert served.watermark == built.watermark


@pytest.mark.asyncio
async def test_new_fact_triggers_one_update_and_appears_in_context(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive = make_fact(scope, uuid="u1", text="Use uv exclusively", memory_type="directive")

    t1 = StubContextTransport([sectioned_response(standing=["Use uv exclusively"])])
    v1 = await get_context(graph=graph, scope=scope, facts=[directive], transport=t1, now=NOW)
    assert v1.version == 1

    new_state = make_fact(
        scope, uuid="u2", text="Currently debugging the graph explorer", memory_type="state", last_seen_at=NOW
    )
    t2 = StubContextTransport(
        [sectioned_response(standing=["Use uv exclusively"], recent=["Currently debugging the graph explorer"])]
    )
    v2 = await get_context(
        graph=graph, scope=scope, facts=[directive, new_state], transport=t2, now=NOW + timedelta(minutes=1)
    )

    assert len(t2.calls) == 1
    assert v2.version == 2
    # The delta prompt must actually describe the new fact so the LLM can act on it.
    prompt, _system = t2.calls[0]
    assert "Currently debugging the graph explorer" in prompt
    assert "NEW:" in prompt
    assert "Currently debugging the graph explorer" in v2.rendered_text
    assert "Use uv exclusively" in v2.rendered_text


@pytest.mark.asyncio
async def test_superseded_fact_drops_out_of_context(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    old_preference = make_fact(scope, uuid="u-old", text="Prefers dark roast coffee", memory_type="preference")

    t1 = StubContextTransport([sectioned_response(standing=["Prefers dark roast coffee"])])
    v1 = await get_context(graph=graph, scope=scope, facts=[old_preference], transport=t1, now=NOW)
    assert "dark roast" in v1.rendered_text

    # Supersession/demotion/pruning all show up the same way at this layer:
    # the fact's relationship_uuid simply no longer appears in the current
    # context-visible fact list `profile()` hands in.
    replacement = make_fact(scope, uuid="u-new", text="Prefers light roast coffee", memory_type="preference")
    t2 = StubContextTransport([sectioned_response(standing=["Prefers light roast coffee"])])
    v2 = await get_context(graph=graph, scope=scope, facts=[replacement], transport=t2, now=NOW + timedelta(hours=1))

    assert len(t2.calls) == 1
    prompt, _system = t2.calls[0]
    assert "REMOVED" in prompt
    assert "dark roast" in prompt  # the LLM is told what disappeared, to edit around it
    assert "dark roast" not in v2.rendered_text
    assert "light roast" in v2.rendered_text


@pytest.mark.asyncio
async def test_standing_directives_survive_a_flood_of_state_facts(tmp_path: Path) -> None:
    """The functional guarantee `ContextPolicy.role_budget_shares` exists for:
    a burst of `state` facts must not be able to evict a `directive` from the
    rendered artifact, even when the (stubbed) LLM response naively echoes an
    oversized "recent" section back. This is enforced in CODE
    (`_enforce_role_budgets`), not by trusting the model's cooperation."""
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive_text = "Never commit directly to main"
    directive = make_fact(scope, uuid="u-directive", text=directive_text, memory_type="directive")

    # A tight budget makes the enforcement boundary easy to observe.
    tight_policy = ContextPolicy(token_budget=200)
    t1 = StubContextTransport([sectioned_response(standing=[directive_text])])
    v1 = await get_context(graph=graph, scope=scope, facts=[directive], policy=tight_policy, transport=t1, now=NOW)
    assert directive_text in v1.rendered_text

    # Flood: 50 new state facts arrive at once. The stubbed transport returns
    # them ALL in "recent" (simulating an LLM that did not itself trim), plus
    # the still-correct standing directive.
    flood = [
        make_fact(
            scope,
            uuid=f"u-state-{i}",
            text=f"Working on task {i}: a reasonably long status update about ongoing work",
            memory_type="state",
            last_seen_at=NOW + timedelta(minutes=i),
        )
        for i in range(50)
    ]
    flood_recent_lines = [f.fact for f in flood]
    t2 = StubContextTransport([sectioned_response(standing=[directive_text], recent=flood_recent_lines)])
    v2 = await get_context(
        graph=graph,
        scope=scope,
        facts=[directive, *flood],
        policy=tight_policy,
        transport=t2,
        now=NOW + timedelta(hours=1),
    )

    assert directive_text in v2.rendered_text, "a standing directive must survive a flood of recent state facts"
    # The flood must have been truncated by the fixed RECENT budget share —
    # not all 50 lines fit in a 200-token artifact.
    kept_recent = len(v2.sections[ContextRole.RECENT.value])
    assert 0 < kept_recent < len(flood_recent_lines)
    # And the standing section was NOT shrunk by the recent flood — it still
    # holds its one directive line.
    assert v2.sections[ContextRole.STANDING.value] == [directive_text]


@pytest.mark.asyncio
async def test_token_budget_is_respected(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    facts = [
        make_fact(
            scope,
            uuid=f"u{i}",
            text=f"Standing convention number {i} about how this repo works",
            memory_type="requirement",
        )
        for i in range(20)
    ]
    policy = ContextPolicy(token_budget=150)
    transport = StubContextTransport([sectioned_response(standing=[f.fact for f in facts])])

    artifact = await get_context(graph=graph, scope=scope, facts=facts, policy=policy, transport=transport, now=NOW)

    from memotron.context import _estimate_tokens  # the same estimator used to enforce the budget

    assert _estimate_tokens(artifact.rendered_text) <= policy.token_budget
    assert len(artifact.sections[ContextRole.STANDING.value]) < len(facts)


@pytest.mark.asyncio
async def test_transport_error_degrades_to_last_good_artifact(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive = make_fact(scope, uuid="u1", text="Keep secrets out of logs", memory_type="directive")

    good_transport = StubContextTransport([sectioned_response(standing=["Keep secrets out of logs"])])
    good = await get_context(graph=graph, scope=scope, facts=[directive], transport=good_transport, now=NOW)
    assert good.degraded is False

    # A new fact arrives, but the gateway is down.
    new_state = make_fact(scope, uuid="u2", text="Investigating a flaky test", memory_type="state")
    degraded_result = await get_context(
        graph=graph,
        scope=scope,
        facts=[directive, new_state],
        transport=BoomTransport(),
        now=NOW + timedelta(minutes=1),
    )

    # Degrades to the LAST GOOD artifact, unchanged — never raises.
    assert degraded_result.version == good.version
    assert degraded_result.rendered_text == good.rendered_text
    assert degraded_result.watermark == good.watermark

    # The stale watermark means the next call, even with a working
    # transport, retries the LLM rather than treating the miss as settled.
    retry_transport = StubContextTransport(
        [sectioned_response(standing=["Keep secrets out of logs"], recent=["Investigating a flaky test"])]
    )
    recovered = await get_context(
        graph=graph,
        scope=scope,
        facts=[directive, new_state],
        transport=retry_transport,
        now=NOW + timedelta(minutes=2),
    )
    assert len(retry_transport.calls) == 1
    assert recovered.version == good.version + 1
    assert "Investigating a flaky test" in recovered.rendered_text


@pytest.mark.asyncio
async def test_transport_error_with_no_prior_artifact_falls_back_deterministically(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive = make_fact(scope, uuid="u1", text="Always run tests before committing", memory_type="directive")

    result = await get_context(graph=graph, scope=scope, facts=[directive], transport=BoomTransport(), now=NOW)

    assert result.degraded is True
    assert result.watermark == ""
    assert "Always run tests before committing" in result.rendered_text

    # Never persisted: the very next (successful) call must still be a real
    # "first build" — not silently treated as already up to date.
    persisted = PropertyGraphStore(tmp_path / "g.sqlite").dream_decisions(
        job_name=f"context-artifact:{scope.key}", limit=1
    )
    assert persisted == []

    ok_transport = StubContextTransport([sectioned_response(standing=["Always run tests before committing"])])
    recovered = await get_context(graph=graph, scope=scope, facts=[directive], transport=ok_transport, now=NOW)
    assert len(ok_transport.calls) == 1
    assert recovered.version == 1
    assert recovered.degraded is False


@pytest.mark.asyncio
async def test_confidence_reinforcement_noise_does_not_force_an_update(tmp_path: Path) -> None:
    """WS-16 bounded-accumulation reinforcement nudges confidence on every
    corroborating observation; rounding (ContextPolicy.confidence_precision)
    must keep a microscopic confidence tick from defeating the zero-call
    fast path."""
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    fact_v1 = make_fact(scope, uuid="u1", text="Prefers async APIs", memory_type="preference", confidence=0.900001)

    t1 = StubContextTransport([sectioned_response(standing=["Prefers async APIs"])])
    built = await get_context(graph=graph, scope=scope, facts=[fact_v1], transport=t1, now=NOW)

    fact_v2 = make_fact(scope, uuid="u1", text="Prefers async APIs", memory_type="preference", confidence=0.900873)
    t2 = StubContextTransport([])
    served = await get_context(graph=graph, scope=scope, facts=[fact_v2], transport=t2, now=NOW + timedelta(minutes=1))

    assert t2.calls == []
    assert served.version == built.version


@pytest.mark.asyncio
async def test_malformed_llm_response_degrades_like_a_transport_error(tmp_path: Path) -> None:
    graph = PropertyGraphStore(tmp_path / "g.sqlite")
    scope = make_scope()
    directive = make_fact(scope, uuid="u1", text="Never skip failing tests", memory_type="directive")
    good = await get_context(
        graph=graph,
        scope=scope,
        facts=[directive],
        transport=StubContextTransport([sectioned_response(standing=["Never skip failing tests"])]),
        now=NOW,
    )

    new_state = make_fact(scope, uuid="u2", text="Chasing a flaky CI run", memory_type="state")
    malformed_transport = StubContextTransport(["not json at all, sorry"])
    result = await get_context(
        graph=graph,
        scope=scope,
        facts=[directive, new_state],
        transport=malformed_transport,
        now=NOW + timedelta(minutes=1),
    )
    assert result.version == good.version
    assert result.rendered_text == good.rendered_text


# ---------------------------------------------------------------------------
# profile(maintain_context=True) integration — proves the client.py wiring
# ---------------------------------------------------------------------------


def _typed_config() -> object:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=(),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that improve agent behaviour.",
            ),
            RelationshipInstruction(
                type="TRACKS",
                source_label="Entity",
                target_label="Entity",
                query="Track short-lived working state.",
                memory_type=MemoryType.STATE,
            ),
        ),
    )
    return default_config().model_copy(
        update={
            "instruction_sets": (instructions,),
            "jobs": (DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        }
    )


@pytest.mark.asyncio
async def test_profile_maintain_context_serves_the_maintained_artifact(tmp_path: Path) -> None:
    scope = make_scope("bob")
    client = Memotron(graph_path=tmp_path / "profile-ctx.sqlite", config=_typed_config())
    added = await client.add_memory(
        subject="Agent",
        predicate="should",
        object="always run uv run pytest before committing",
        relationship_type="SHOULD",
        confidence=0.95,
        scope=scope,
    )

    transport = StubContextTransport([sectioned_response(standing=["Always run uv run pytest before committing"])])
    profile = await client.profile(scope=scope, maintain_context=True, context_transport=transport)

    assert len(transport.calls) == 1
    assert "Always run uv run pytest before committing" in profile.rendered_context
    assert any(item.relationship_uuid == added.relationship_uuid for item in profile.injected_items)

    # Second call, nothing changed in the graph: zero transport calls, same text.
    no_op = StubContextTransport([])
    profile2 = await client.profile(scope=scope, maintain_context=True, context_transport=no_op)
    assert no_op.calls == []
    assert profile2.rendered_context == profile.rendered_context

    # Legacy call path (maintain_context defaults False) is untouched: the
    # legacy renderer's own format ("Memory profile for", "Static facts:")
    # still comes back when the flag is not set.
    legacy_profile = await client.profile(scope=scope)
    assert legacy_profile.rendered_context.startswith(f"Memory profile for {scope.key}")


@pytest.mark.asyncio
async def test_profile_maintain_context_rejects_as_of_and_reader_agent_id(tmp_path: Path) -> None:
    scope = make_scope("carol")
    client = Memotron(graph_path=tmp_path / "profile-ctx-2.sqlite", config=_typed_config())

    with pytest.raises(ValueError, match="as_of"):
        await client.profile(scope=scope, maintain_context=True, as_of=NOW)

    with pytest.raises(ValueError, match="per-agent visibility"):
        await client.profile(scope=scope, maintain_context=True, reader_agent_id="agent:x")
