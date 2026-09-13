"""WS-28: compaction survival & context-pollution accounting.

Covers all four units, hermetically (no LLM, no network):

- T1 checkpoint-seeded rehydration (``agent_memory.py`` + ``cli.py``)
- T2 usage-ranked injection (``context.py`` STRUCTURE fill + ``retrieval.py``/
  ``config.py``'s event-volume-gated utility rerank)
- T3 runbook capture (``transcripts.py`` command extraction +
  ``dreaming.py``'s write-side dedup guard)
- T4 ``injected_waste_rate`` context-pollution accounting
  (``client.py``'s ``memory_evolution()`` + ``context.py``'s opt-in
  role-budget response)

The live-gateway compaction-boundary proof (a real Claude Code session
compacting) is out of scope for this hermetic suite by design -- see
``ingest/actionability_goldset.yaml``'s ``compaction_survival`` section
(scored by ``ingest/score_actionability.py``) for the fixture-graph
integration proof of T3's "answered from the profile with zero tool calls"
claim, which this file's ``test_t3_*`` tests mirror directly with pytest.
"""

from __future__ import annotations

import io
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    AgentMemoryMode,
    AgentMemoryPlatform,
    DreamConfig,
    Memotron,
    MemoryScope,
    RetrievalPolicy,
    ScopeKind,
)
from memotron.adoption import (
    AdoptionStorage,
    initialize_claude_code_project,
    load_project_config,
    save_project_config,
)
from memotron.cli import main as cli_main
from memotron.config import default_config
from memotron.context import (
    ContextPolicy,
    get_context,
    responsive_role_budget_shares,
)
from memotron.models import MemoryProfileFact, OutcomeVerdict, RelationshipStatus, UseEventKind
from memotron.receipts import ReceiptDecisionType
from memotron.retrieval import resolve_effective_utility_weight
from memotron.transcripts import TranscriptTurn, parse_transcript_lines, repeated_commands

AGENT_ID = "compaction-survival-agent"


def new_client(tmp_path: Path, name: str, retrieval: RetrievalPolicy | None = None) -> Memotron:
    """Memotron client on a tmp graph, optionally with a retrieval override."""
    config: DreamConfig | None = None
    if retrieval is not None:
        config = default_config().model_copy(update={"retrieval": retrieval})
    if config is None:
        return Memotron(graph_path=tmp_path / name)
    return Memotron(graph_path=tmp_path / name, config=config)


def build_platform(tmp_path: Path, name: str) -> AgentMemoryPlatform:
    return AgentMemoryPlatform.create(
        graph_path=tmp_path / f"{name}.sqlite",
        project_id="compaction-survival-project",
        agent_ids=(AGENT_ID,),
        mode=AgentMemoryMode.SIMPLE,
        user_id="compaction-survival-user",
    )


def bash_turn(command: str) -> TranscriptTurn:
    return TranscriptTurn(
        role="assistant",
        text="",
        tool_names=("Bash",),
        file_paths=(),
        is_meta=False,
        is_compact_summary=False,
        commands=(command,),
    )


# ===========================================================================
# T1 -- checkpoint-seeded rehydration
# ===========================================================================


class TestLatestCompactionCheckpoint:
    @pytest.mark.asyncio
    async def test_no_checkpoint_returns_none(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-none")
        assert platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="sess-1") is None

    @pytest.mark.asyncio
    async def test_blank_session_id_returns_none(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-blank")
        assert platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="") is None

    @pytest.mark.asyncio
    async def test_wrong_checkpoint_reason_is_not_a_candidate(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-wrong-reason")
        await platform.memory_log(
            agent_id=AGENT_ID,
            summary="Handoff note, not a compaction checkpoint.",
            checkpoint_reason="handoff",
            task_run_id="claude:sess-2:handoff:1",
        )
        assert platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="sess-2") is None

    @pytest.mark.asyncio
    async def test_returns_the_checkpoint_summary_verbatim(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-one")
        await platform.memory_log(
            agent_id=AGENT_ID,
            summary="Task focus: fixing the retrieval rerank order.",
            checkpoint_reason="context_compaction",
            task_run_id="claude:sess-3:pre-compact:1",
        )
        result = platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="sess-3")
        assert result == "Task focus: fixing the retrieval rerank order."

    @pytest.mark.asyncio
    async def test_picks_the_chronologically_latest_of_two(self, tmp_path: Path) -> None:
        """PreCompact's dying-context capture, then PostCompact's own
        summary -- the LATER one (PostCompact) wins, matching
        ``session_task_run_id``'s stable ``claude:{session_id}:`` prefix
        across both hook firings."""
        platform = build_platform(tmp_path, "t1-latest")
        await platform.memory_log(
            agent_id=AGENT_ID,
            summary="Pre-compact: task focus was X.",
            checkpoint_reason="context_compaction",
            task_run_id="claude:sess-4:pre-compact:1",
        )
        await platform.memory_log(
            agent_id=AGENT_ID,
            summary="Post-compact: Claude Code's own compact summary Y.",
            checkpoint_reason="context_compaction",
            task_run_id="claude:sess-4:post-compact:2",
        )
        result = platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="sess-4")
        assert result == "Post-compact: Claude Code's own compact summary Y."

    @pytest.mark.asyncio
    async def test_different_session_is_not_a_candidate(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-other-session")
        await platform.memory_log(
            agent_id=AGENT_ID,
            summary="Belongs to a different session.",
            checkpoint_reason="context_compaction",
            task_run_id="claude:sess-other:pre-compact:1",
        )
        assert platform.latest_compaction_checkpoint(agent_id=AGENT_ID, session_id="sess-mine") is None


class TestMemoryStartCheckpointSeed:
    @pytest.mark.asyncio
    async def test_no_seed_is_byte_identical_to_today(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-byte-identical")
        await platform.memory_remember(
            agent_id=AGENT_ID,
            subject="Deploy pipeline",
            predicate="requires",
            object="manual approval gate",
            relationship_type="REQUIRES",
        )
        without_param = await platform.memory_start(agent_id=AGENT_ID, task_run_id="task-a")
        with_none = await platform.memory_start(agent_id=AGENT_ID, task_run_id="task-b", checkpoint_seed=None)
        assert without_param.rendered_context == with_none.rendered_context
        assert without_param.checkpoint_seed_applied is False
        assert with_none.checkpoint_seed_applied is False

    @pytest.mark.asyncio
    async def test_blank_seed_is_also_a_no_op(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t1-blank-seed")
        plain = await platform.memory_start(agent_id=AGENT_ID, task_run_id="task-a")
        blank = await platform.memory_start(agent_id=AGENT_ID, task_run_id="task-b", checkpoint_seed="   ")
        assert plain.rendered_context == blank.rendered_context
        assert blank.checkpoint_seed_applied is False

    @pytest.mark.asyncio
    async def test_seed_leads_rendered_context_and_runs_a_search(self, tmp_path: Path) -> None:
        """Unit (NEXT.md T1): fake checkpoint -> rendered context contains
        the task focus and the seeded search ran."""
        platform = build_platform(tmp_path, "t1-seeded")
        remembered = await platform.memory_remember(
            agent_id=AGENT_ID,
            subject="Retrieval reranker",
            predicate="should",
            object="weight utility signals during the final stage",
            relationship_type="SHOULD",
        )
        seed_text = "Task focus: tuning the retrieval reranker's utility weight."
        started = await platform.memory_start(
            agent_id=AGENT_ID, task_run_id="claude:sess-5:compact:1", checkpoint_seed=seed_text
        )
        assert started.checkpoint_seed_applied is True
        assert seed_text in started.rendered_context
        assert started.rendered_context.index(seed_text) < started.rendered_context.index("Memotron project memory")
        # The seeded search actually ran: it recorded a RETRIEVED use event
        # for the fact whose text overlaps the seed query.
        seed_events = [
            event
            for event in started.use_events
            if event.relationship_uuid == remembered.relationship_uuid
            and event.kind == UseEventKind.RETRIEVED
            and event.metadata.get("compaction_seed") is True
        ]
        assert seed_events, "expected a compaction-seed RETRIEVED use event for the matching fact"


class TestSessionStartHookCheckpointSeed:
    def _init_project(self, tmp_path: Path) -> tuple[Path, Path, object]:
        import subprocess

        root = tmp_path / "hook-repo"
        root.mkdir()
        subprocess.run(["git", "init", "--quiet", str(root)], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "remote",
                "add",
                "origin",
                "git@github.example.com:parks/hook-loop.git",
            ],
            check=True,
        )
        initialize_claude_code_project(project_root=root)
        graph_path = tmp_path / "hook-loop.sqlite"
        save_project_config(
            root,
            load_project_config(root).model_copy(update={"storage": AdoptionStorage(graph_path=str(graph_path))}),
        )
        return root, graph_path, load_project_config(root)

    def test_session_start_source_compact_seeds_from_checkpoint(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, graph_path, config = self._init_project(tmp_path)
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id=config.project.id,
            agent_ids=(config.caller.id,),
            mode=config.mode,
            user_id="hook-user",
        )

        import asyncio

        async def _seed_checkpoint() -> None:
            await platform.memory_log(
                agent_id=config.caller.id,
                summary="Task focus: wiring the compaction-seeded rehydration hook.",
                checkpoint_reason="context_compaction",
                task_run_id="claude:sess-hook-1:pre-compact:1",
            )

        asyncio.run(_seed_checkpoint())

        import memotron.cli as cli_module

        monkeypatch.setattr(cli_module, "build_platform_from_project", lambda _root: (platform, config))
        monkeypatch.setattr(
            sys,
            "argv",
            ["memotron", "hook", "session-start", "--project-root", str(root)],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(
                json.dumps(
                    {
                        "session_id": "sess-hook-1",
                        "hook_event_name": "SessionStart",
                        "source": "compact",
                    }
                )
            ),
        )
        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)
        cli_main()
        output = captured.getvalue()
        assert "wiring the compaction-seeded rehydration hook" in output

    def test_session_start_without_compact_source_never_seeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root, graph_path, config = self._init_project(tmp_path)
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id=config.project.id,
            agent_ids=(config.caller.id,),
            mode=config.mode,
            user_id="hook-user",
        )

        import asyncio

        async def _seed_checkpoint() -> None:
            await platform.memory_log(
                agent_id=config.caller.id,
                summary="Task focus: a checkpoint that a NORMAL session-start must ignore.",
                checkpoint_reason="context_compaction",
                task_run_id="claude:sess-hook-2:pre-compact:1",
            )

        asyncio.run(_seed_checkpoint())

        import memotron.cli as cli_module

        monkeypatch.setattr(cli_module, "build_platform_from_project", lambda _root: (platform, config))
        monkeypatch.setattr(
            sys,
            "argv",
            ["memotron", "hook", "session-start", "--project-root", str(root)],
        )
        monkeypatch.setattr(
            sys,
            "stdin",
            io.StringIO(json.dumps({"session_id": "sess-hook-2", "hook_event_name": "SessionStart"})),
        )
        captured = io.StringIO()
        monkeypatch.setattr(sys, "stdout", captured)
        cli_main()
        output = captured.getvalue()
        assert "a checkpoint that a NORMAL session-start must ignore" not in output


# ===========================================================================
# T2 -- usage-ranked injection
# ===========================================================================

UTILITY_ANCHOR = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


class TestResolveEffectiveUtilityWeight:
    def test_explicit_override_always_wins(self) -> None:
        policy = RetrievalPolicy(utility_weight=0.8, utility_weight_auto_floor_events=100)
        assert resolve_effective_utility_weight(policy, event_volume=0) == 0.8

    def test_no_floor_configured_stays_zero(self) -> None:
        policy = RetrievalPolicy()
        assert policy.utility_weight_auto_floor_events is None
        assert resolve_effective_utility_weight(policy, event_volume=10_000) == 0.0

    def test_below_floor_stays_zero(self) -> None:
        policy = RetrievalPolicy(utility_weight_auto_floor_events=5, utility_weight_when_unlocked=0.3)
        assert resolve_effective_utility_weight(policy, event_volume=4) == 0.0

    def test_at_or_above_floor_unlocks(self) -> None:
        policy = RetrievalPolicy(utility_weight_auto_floor_events=5, utility_weight_when_unlocked=0.3)
        assert resolve_effective_utility_weight(policy, event_volume=5) == 0.3
        assert resolve_effective_utility_weight(policy, event_volume=50) == 0.3


async def _seed_equal_pair(client: Memotron, scope: MemoryScope) -> tuple[str, str]:
    plain = await client.add_memory(
        subject="Team Alpha",
        predicate="requires",
        object="SSO login",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        valid_from=UTILITY_ANCHOR - timedelta(days=10),
    )
    cited = await client.add_memory(
        subject="Team Beta",
        predicate="requires",
        object="SSO login",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        valid_from=UTILITY_ANCHOR - timedelta(days=10),
    )
    return plain.relationship_uuid, cited.relationship_uuid


async def _record_retrieved_cited_positive(
    client: Memotron, scope: MemoryScope, relationship_uuid: str, *, retrieved_count: int
) -> None:
    for i in range(retrieved_count):
        await client.record_memory_use(
            relationship_uuid=relationship_uuid,
            scope=scope,
            kind=UseEventKind.RETRIEVED,
            task_run_id=f"task-utility-{i}",
            idempotency_key=f"task-utility-{i}:retrieved:{relationship_uuid}",
            used_at=UTILITY_ANCHOR - timedelta(days=2),
            rank=0,
            retrieval_score=0.9,
            candidate_set_size=1,
            context_budget_competition=0,
            retrieval_policy_digest="test-digest",
        )
    use = await client.record_memory_use(
        relationship_uuid=relationship_uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="task-utility-cited",
        idempotency_key=f"task-utility:cited:{relationship_uuid}",
        used_at=UTILITY_ANCHOR - timedelta(days=1),
    )
    await client.record_memory_outcome(
        use_id=use.use_id,
        scope=scope,
        verdict=OutcomeVerdict.POSITIVE,
        task_run_id="task-utility-cited",
        idempotency_key=f"task-utility:outcome:{relationship_uuid}",
        judge_identity="test-evaluator",
        judge_version="v1",
        judged_at=UTILITY_ANCHOR - timedelta(days=1),
    )


class TestSearchContextAutoGatedUtilityRerank:
    @pytest.mark.asyncio
    async def test_below_floor_ranks_byte_identically(self, tmp_path: Path) -> None:
        policy = RetrievalPolicy(
            vector_weight=0.0, utility_weight_auto_floor_events=5, utility_weight_when_unlocked=0.8
        )
        client = new_client(tmp_path, "t2-below-floor.sqlite", retrieval=policy)
        scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="t2-below-floor")
        plain_uuid, cited_uuid = await _seed_equal_pair(client, scope)
        await _record_retrieved_cited_positive(client, scope, cited_uuid, retrieved_count=2)

        results = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
        by_uuid = {result.relationship_uuid: result for result in results}
        assert by_uuid[plain_uuid].score == by_uuid[cited_uuid].score

    @pytest.mark.asyncio
    async def test_at_floor_unlocks_and_receipts_the_flip(self, tmp_path: Path) -> None:
        """Unit (NEXT.md T2): a twice-cited/retrieved command outranks a
        never-retrieved peer once the scope crosses the event floor."""
        policy = RetrievalPolicy(
            vector_weight=0.0, utility_weight_auto_floor_events=2, utility_weight_when_unlocked=0.8
        )
        client = new_client(tmp_path, "t2-at-floor.sqlite", retrieval=policy)
        scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="t2-at-floor")
        plain_uuid, cited_uuid = await _seed_equal_pair(client, scope)
        await _record_retrieved_cited_positive(client, scope, cited_uuid, retrieved_count=2)

        results = await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)
        by_uuid = {result.relationship_uuid: result for result in results}
        assert by_uuid[cited_uuid].score > by_uuid[plain_uuid].score

        flips = [
            receipt
            for receipt in client.graph.receipts.receipts_for_scope(scope.key)
            if receipt.decision_type == ReceiptDecisionType.RETRIEVAL_UTILITY_AUTO_ENABLED
        ]
        assert flips, "expected a receipted auto-enable flip"

    @pytest.mark.asyncio
    async def test_default_policy_never_reads_the_event_plane(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The hard invariant this whole gating design is built to preserve:
        a vanilla default RetrievalPolicy (utility_weight_auto_floor_events
        is None) never calls utility_projection, even with rich cited
        history in the scope."""
        client = new_client(tmp_path, "t2-default.sqlite")
        assert client.config.retrieval.utility_weight_auto_floor_events is None
        scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="t2-default")
        _plain_uuid, cited_uuid = await _seed_equal_pair(client, scope)
        await _record_retrieved_cited_positive(client, scope, cited_uuid, retrieved_count=5)

        def _forbidden(**_kwargs):  # pragma: no cover - failing is the assertion
            raise AssertionError("utility projection must not be read at the shipped default")

        monkeypatch.setattr(client.graph, "utility_projection", _forbidden)
        await client.search(query="sso login", scope=scope, as_of=UTILITY_ANCHOR)


def make_fact(
    scope: MemoryScope,
    *,
    uuid: str,
    text: str,
    memory_type: str,
    confidence: float = 0.9,
    observed_count: int = 1,
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
        pinned=False,
        observed_count=observed_count,
    )


class BoomTransport:
    identifier = "boom:test"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        raise ValueError("gateway unavailable")


class TestStructureRoleUsageRanking:
    @pytest.mark.asyncio
    async def test_never_cited_command_falls_behind_a_used_one(self, tmp_path: Path) -> None:
        """Unit (NEXT.md T2): a twice-cited command outranks a never-cited
        peer in STRUCTURE (the deterministic fallback render, which is the
        code-level ordering guarantee -- no LLM to trust).  Seeded through
        the real client so record_memory_use's cross-plane validation
        (the relationship must actually exist) is satisfied."""
        client = new_client(tmp_path, "t2-structure.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="alice")
        never_cited = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="memotron status",
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.9,
        )
        twice_cited = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="memotron llm status",
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.9,
        )
        for i in range(2):
            use = await client.record_memory_use(
                relationship_uuid=twice_cited.relationship_uuid,
                scope=scope,
                kind=UseEventKind.CITED_OR_USED,
                task_run_id=f"task-{i}",
                idempotency_key=f"task-{i}:cited:{twice_cited.relationship_uuid}",
                used_at=UTILITY_ANCHOR - timedelta(days=1),
            )
            await client.record_memory_outcome(
                use_id=use.use_id,
                scope=scope,
                verdict=OutcomeVerdict.POSITIVE,
                task_run_id=f"task-{i}",
                idempotency_key=f"task-{i}:outcome:{twice_cited.relationship_uuid}",
                judge_identity="test",
                judge_version="v1",
                judged_at=UTILITY_ANCHOR - timedelta(days=1),
            )

        never_fact = make_fact(
            scope,
            uuid=never_cited.relationship_uuid,
            text="agent runs memotron status",
            memory_type="decision",
        )
        cited_fact = make_fact(
            scope,
            uuid=twice_cited.relationship_uuid,
            text="agent runs memotron llm status",
            memory_type="decision",
        )
        artifact = await get_context(
            graph=client.graph,
            scope=scope,
            facts=[never_fact, cited_fact],
            transport=BoomTransport(),
            now=UTILITY_ANCHOR,
        )
        assert artifact.degraded is True
        structure_lines = artifact.sections["structure"]
        assert len(structure_lines) == 2
        assert structure_lines[0] == "agent runs memotron llm status"
        assert structure_lines[1] == "agent runs memotron status"

    @pytest.mark.asyncio
    async def test_no_usage_data_is_byte_identical_to_the_uuid_order(self, tmp_path: Path) -> None:
        client = new_client(tmp_path, "t2-no-usage.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="alice")
        fact_a = make_fact(scope, uuid="a", text="fact A", memory_type="decision")
        fact_b = make_fact(scope, uuid="b", text="fact B", memory_type="decision")
        artifact = await get_context(
            graph=client.graph,
            scope=scope,
            facts=[fact_a, fact_b],
            transport=BoomTransport(),
            now=UTILITY_ANCHOR,
        )
        # Equal confidence/observed_count/use_need (all zero) -- falls back
        # to the pre-WS-28 (confidence, observed_count) order, stable.
        assert artifact.sections["structure"] == ["fact A", "fact B"]


class TestResponsiveRoleBudgetShares:
    def test_no_measurement_returns_shares_unchanged(self) -> None:
        base = ContextPolicy().role_budget_shares
        assert responsive_role_budget_shares(base, injected_waste_rate_by_role={}) == base

    def test_wasteful_role_shrinks_and_others_absorb_the_reclaim(self) -> None:
        base = ContextPolicy().role_budget_shares  # standing 0.5, recent 0.3, structure 0.2
        adjusted = responsive_role_budget_shares(base, injected_waste_rate_by_role={"structure": 1.0})
        assert adjusted["structure"] < base["structure"]
        assert adjusted["standing"] > base["standing"]
        assert adjusted["recent"] > base["recent"]
        assert sum(adjusted.values()) == pytest.approx(1.0)

    def test_result_always_satisfies_context_policy_invariants(self) -> None:
        base = ContextPolicy().role_budget_shares
        adjusted = responsive_role_budget_shares(base, injected_waste_rate_by_role={"structure": 0.9, "recent": 0.4})
        # Constructing a ContextPolicy re-validates sum-to-1.0 and non-negativity.
        ContextPolicy(role_budget_shares=adjusted)

    def test_context_policy_responsive_to_wraps_the_pure_function(self) -> None:
        policy = ContextPolicy()
        adjusted_policy = policy.responsive_to({"structure": 1.0})
        assert adjusted_policy.role_budget_shares["structure"] < policy.role_budget_shares["structure"]
        assert adjusted_policy.token_budget == policy.token_budget

    def test_invalid_damping_rejected(self) -> None:
        with pytest.raises(ValueError, match="damping"):
            responsive_role_budget_shares(
                ContextPolicy().role_budget_shares,
                injected_waste_rate_by_role={"structure": 1.0},
                damping=1.5,
            )


# ===========================================================================
# T3 -- runbook capture
# ===========================================================================


class TestRepeatedCommands:
    def test_three_repeats_captured(self) -> None:
        turns = tuple(bash_turn("uv run examples/simulation.py --live") for _ in range(3))
        assert repeated_commands(turns) == ("uv run examples/simulation.py --live",)

    def test_two_repeats_not_captured(self) -> None:
        turns = tuple(bash_turn("uv run pytest tests/test_context.py") for _ in range(2))
        assert repeated_commands(turns) == ()

    def test_different_shapes_counted_separately(self) -> None:
        turns = (
            *(bash_turn("uv run examples/simulation.py --live") for _ in range(3)),
            *(bash_turn("uv run examples/simulation.py --live-fast") for _ in range(3)),
        )
        captured = repeated_commands(turns)
        assert set(captured) == {
            "uv run examples/simulation.py --live",
            "uv run examples/simulation.py --live-fast",
        }
        assert len(captured) == 2

    def test_first_seen_order_is_deterministic(self) -> None:
        turns = (
            *(bash_turn("second") for _ in range(3)),
            *(bash_turn("first") for _ in range(3)),
        )
        # "second" appears (in full) before "first" in the turn sequence.
        assert repeated_commands(turns) == ("second", "first")

    def test_custom_min_repeats(self) -> None:
        turns = tuple(bash_turn("uv run x") for _ in range(2))
        assert repeated_commands(turns, min_repeats=2) == ("uv run x",)

    def test_min_repeats_below_one_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_repeats"):
            repeated_commands((), min_repeats=0)


class TestTranscriptCommandExtraction:
    def test_bash_tool_use_command_is_captured_verbatim(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "uv run pytest -q  "},
                            }
                        ],
                    },
                }
            )
        ]
        turns = parse_transcript_lines(lines)
        assert len(turns) == 1
        # Verbatim modulo the surrounding-whitespace strip only.
        assert turns[0].commands == ("uv run pytest -q",)

    def test_non_bash_tool_use_captures_no_command(self) -> None:
        lines = [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/x"}}],
                    },
                }
            )
        ]
        turns = parse_transcript_lines(lines)
        assert turns[0].commands == ()
        assert turns[0].file_paths == ("/x",)

    def test_repeated_bash_calls_in_one_turn_are_not_deduplicated(self) -> None:
        content = [
            {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q"}},
            {"type": "tool_use", "name": "Bash", "input": {"command": "uv run pytest -q"}},
        ]
        lines = [json.dumps({"type": "assistant", "message": {"role": "assistant", "content": content}})]
        turns = parse_transcript_lines(lines)
        assert turns[0].commands == ("uv run pytest -q", "uv run pytest -q")


class TestRecordRunbookCommands:
    @pytest.mark.asyncio
    async def test_zero_qualifying_commands_is_a_no_op(self, tmp_path: Path) -> None:
        platform = build_platform(tmp_path, "t3-no-op")
        result = await platform.record_runbook_commands(
            agent_id=AGENT_ID,
            turns=(bash_turn("uv run once"),),
            task_run_id="claude:sess-6:pre-compact:1",
        )
        assert result.captured_commands == ()
        assert result.episode_uuids == ()

    @pytest.mark.asyncio
    async def test_repeated_command_becomes_a_verbatim_directive_fact(self, tmp_path: Path) -> None:
        """Unit (NEXT.md T3): a transcript with 3x
        `uv run examples/simulation.py --live` -> one fact carrying that
        exact string."""
        platform = build_platform(tmp_path, "t3-capture")
        command = "uv run examples/simulation.py --live"
        turns = tuple(bash_turn(command) for _ in range(3))
        capture = await platform.record_runbook_commands(
            agent_id=AGENT_ID, turns=turns, task_run_id="claude:sess-7:pre-compact:1"
        )
        assert capture.captured_commands == (command,)
        assert len(capture.episode_uuids) == 1

        refreshed = await platform.memory_refresh(agent_id=AGENT_ID, include_project=False)
        assert refreshed.agent_run.created_relationships == 1

        scope = platform.agent_scope(AGENT_ID)
        relationships = platform.client.graph.relationships_for_scope(scope.key)
        should_facts = [r for r in relationships if r.type == "SHOULD"]
        assert len(should_facts) == 1
        assert should_facts[0].properties["object"] == command
        assert should_facts[0].properties["metadata"]["verbatim_command"] == command
        assert should_facts[0].properties["metadata"]["runbook_capture"] is True

    @pytest.mark.asyncio
    async def test_integration_zero_tool_calls_after_compaction(self, tmp_path: Path) -> None:
        """Integration (NEXT.md T3 / GOAL): post-compaction, "how do I run
        the live sim" is answered from memory_start's injected context with
        zero tool calls -- the captured command is already in the profile
        the very next session, with no search needed."""
        platform = build_platform(tmp_path, "t3-integration")
        command = "uv run examples/simulation.py --live"
        turns = tuple(bash_turn(command) for _ in range(3))
        await platform.record_runbook_commands(
            agent_id=AGENT_ID, turns=turns, task_run_id="claude:sess-8:session-end:1"
        )
        await platform.memory_refresh(agent_id=AGENT_ID, include_project=False)

        # The NEXT session's memory_start (zero search calls) already
        # contains the verbatim runbook command.
        started = await platform.memory_start(agent_id=AGENT_ID, task_run_id="claude:sess-9:startup:1")
        assert command in started.rendered_context


class TestRunbookWriteGuardPreventsMerge:
    @pytest.mark.asyncio
    async def test_close_commands_do_not_merge_when_flagged(self, tmp_path: Path) -> None:
        """Two lexically-close commands (cosine 0.926 under the hermetic
        transport, above the default 0.88 dedup threshold) must land as TWO
        distinct facts when tagged runbook_capture, mirroring the
        `compaction_survival` gold-set fixture's regression case."""
        platform = build_platform(tmp_path, "t3-no-merge")
        live = "uv run examples/simulation.py --live"
        live_fast = "uv run examples/simulation.py --live-fast"
        turns = (
            *(bash_turn(live) for _ in range(3)),
            *(bash_turn(live_fast) for _ in range(3)),
        )
        await platform.record_runbook_commands(
            agent_id=AGENT_ID, turns=turns, task_run_id="claude:sess-10:pre-compact:1"
        )
        refreshed = await platform.memory_refresh(agent_id=AGENT_ID, include_project=False)
        assert refreshed.agent_run.created_relationships == 2

        scope = platform.agent_scope(AGENT_ID)
        objects = {
            r.properties["object"]
            for r in platform.client.graph.relationships_for_scope(scope.key)
            if r.type == "SHOULD"
        }
        assert objects == {live, live_fast}

    @pytest.mark.asyncio
    async def test_without_the_runbook_flag_the_same_pair_merges(self, tmp_path: Path) -> None:
        """Negative control proving the guard is load-bearing, not
        decorative: the SAME two command strings, submitted as an ordinary
        (non-runbook) episode without metadata["runbook_capture"], DO
        collapse to one reinforced row via semantic dedup."""
        from memotron.models import EpisodeType

        client = new_client(tmp_path, "t3-merge-control.sqlite")
        scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="control")
        live = "uv run examples/simulation.py --live"
        live_fast = "uv run examples/simulation.py --live-fast"
        for command in (live, live_fast):
            await client.add_episode(
                name=f"control-{command}",
                episode_body=json.dumps(
                    {
                        "memories": [
                            {
                                "subject": "agent",
                                "predicate": "runs",
                                "object": command,
                                "relationship_type": "SHOULD",
                                "confidence": 0.9,
                                "source_text": command,
                                "claim_mode": "directive",
                            }
                        ]
                    }
                ),
                source=EpisodeType.JSON,
                scope=scope,
            )
        await client.run_due_dreams()
        should_facts = [r for r in client.graph.relationships_for_scope(scope.key) if r.type == "SHOULD"]
        assert len(should_facts) == 1, (
            "expected the un-flagged pair to merge via semantic dedup -- if this "
            "fails, the regression this guard fixes may no longer reproduce"
        )


# ===========================================================================
# T4 -- injected_waste_rate
# ===========================================================================


_INJECTED_PROPENSITY_KWARGS = {
    "rank": 0,
    "retrieval_score": 0.9,
    "candidate_set_size": 1,
    "context_budget_competition": 0,
    "retrieval_policy_digest": "test-digest",
}
"""INJECTED/RETRIEVED use events require these propensity fields (UseEvent's
model validator) -- a fixed set of harmless values for tests that only care
about kind/idempotency, not the propensity numbers themselves."""


class TestInjectedWasteRate:
    @pytest.mark.asyncio
    async def test_unmeasured_is_none_not_a_fake_zero(self, tmp_path: Path) -> None:
        client = new_client(tmp_path, "t4-unmeasured.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="t4-unmeasured")
        await client.add_memory(subject="agent", predicate="runs", object="x", relationship_type="SHOULD", scope=scope)
        proof = await client.memory_evolution(scope=scope)
        assert proof.injected_waste_rate is None
        assert proof.injected_waste_rate_by_type == {}
        assert proof.injected_waste_rate_by_role == {}

    @pytest.mark.asyncio
    async def test_exact_rate_from_synthetic_events(self, tmp_path: Path) -> None:
        """Unit (NEXT.md T4): synthetic events -> exact rate."""
        client = new_client(tmp_path, "t4-exact.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="t4-exact")
        wasted = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="never cited command",
            relationship_type="SHOULD",
            scope=scope,
        )
        used = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="always cited command",
            relationship_type="SHOULD",
            scope=scope,
        )
        session_1 = "claude:session-1:startup:1"
        session_2 = "claude:session-2:startup:1"
        for session, uuid in ((session_1, wasted.relationship_uuid), (session_2, wasted.relationship_uuid)):
            await client.record_memory_use(
                relationship_uuid=uuid,
                scope=scope,
                kind=UseEventKind.INJECTED,
                task_run_id=session,
                idempotency_key=f"{session}:{uuid}:injected",
                **_INJECTED_PROPENSITY_KWARGS,
            )
        for session in (session_1, session_2):
            await client.record_memory_use(
                relationship_uuid=used.relationship_uuid,
                scope=scope,
                kind=UseEventKind.INJECTED,
                task_run_id=session,
                idempotency_key=f"{session}:{used.relationship_uuid}:injected",
                **_INJECTED_PROPENSITY_KWARGS,
            )
            await client.record_memory_use(
                relationship_uuid=used.relationship_uuid,
                scope=scope,
                kind=UseEventKind.CITED_OR_USED,
                task_run_id=session,
                idempotency_key=f"{session}:{used.relationship_uuid}:cited",
            )

        proof = await client.memory_evolution(scope=scope)
        # 2 injected impressions per fact; the "wasted" fact is never cited
        # in either session, the "used" fact is cited in both -- exactly
        # half the token-weighted injected impressions are wasted.
        assert proof.injected_waste_rate == pytest.approx(0.5)
        assert proof.injected_waste_rate_by_type["directive"] == pytest.approx(0.5)
        # "directive" memory_type maps to ContextRole.STANDING, not STRUCTURE.
        assert "standing" in proof.injected_waste_rate_by_role
        assert proof.injected_waste_rate_by_role["standing"] == pytest.approx(0.5)

    @pytest.mark.asyncio
    async def test_fully_cited_scope_has_zero_waste(self, tmp_path: Path) -> None:
        client = new_client(tmp_path, "t4-zero-waste.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="t4-zero-waste")
        used = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="always cited command",
            relationship_type="SHOULD",
            scope=scope,
        )
        session = "claude:session-1:startup:1"
        await client.record_memory_use(
            relationship_uuid=used.relationship_uuid,
            scope=scope,
            kind=UseEventKind.INJECTED,
            task_run_id=session,
            idempotency_key=f"{session}:{used.relationship_uuid}:injected",
            **_INJECTED_PROPENSITY_KWARGS,
        )
        await client.record_memory_use(
            relationship_uuid=used.relationship_uuid,
            scope=scope,
            kind=UseEventKind.CITED_OR_USED,
            task_run_id=session,
            idempotency_key=f"{session}:{used.relationship_uuid}:cited",
        )
        proof = await client.memory_evolution(scope=scope)
        assert proof.injected_waste_rate == pytest.approx(0.0)

    @pytest.mark.asyncio
    async def test_fully_wasted_scope_has_full_waste(self, tmp_path: Path) -> None:
        client = new_client(tmp_path, "t4-full-waste.sqlite")
        scope = MemoryScope(kind=ScopeKind.USER, scope_id="t4-full-waste")
        wasted = await client.add_memory(
            subject="agent",
            predicate="runs",
            object="never cited command",
            relationship_type="SHOULD",
            scope=scope,
        )
        session = "claude:session-1:startup:1"
        await client.record_memory_use(
            relationship_uuid=wasted.relationship_uuid,
            scope=scope,
            kind=UseEventKind.INJECTED,
            task_run_id=session,
            idempotency_key=f"{session}:{wasted.relationship_uuid}:injected",
            **_INJECTED_PROPENSITY_KWARGS,
        )
        proof = await client.memory_evolution(scope=scope)
        assert proof.injected_waste_rate == pytest.approx(1.0)
        assert proof.injected_waste_rate_by_role.get("standing") == pytest.approx(1.0)
