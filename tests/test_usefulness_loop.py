"""WS-15 T8/T9/T10: auto-recorded citations and the session outcome judge.

Hermetic end to end: platforms are built directly with stub transports (no env
or .env reads), transcripts are synthetic ``TranscriptTurn`` tuples, and the
one CLI test monkeypatches the platform builder so the hook path never touches
a live LLM.
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

from memotron import AgentMemoryMode, AgentMemoryPlatform
from memotron.adoption import (
    AdoptionStorage,
    initialize_claude_code_project,
    load_project_config,
    save_project_config,
)
from memotron.cli import main as cli_main
from memotron.graph import PropertyGraphStore
from memotron.models import OutcomeVerdict, UseEventKind
from memotron.receipts import ReceiptDecisionType
from memotron.transcripts import TranscriptTurn

AGENT_ID = "claude-code"
SESSION_ID = "sess-loop-1"


class StubJudgeTransport:
    """SynthesisTransport stub: answers verdicts for the use_ids in the prompt."""

    def __init__(self, *, verdicts: list[str] | None = None, raw: str | None = None) -> None:
        self.verdicts = verdicts or []
        self.raw = raw
        self.calls: list[tuple[str, str]] = []

    @property
    def identifier(self) -> str:
        return "stub-judge:test@v1"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        self.calls.append((prompt, system_prompt))
        if self.raw is not None:
            return self.raw
        offered = re.findall(r"^- ([0-9a-f\-]{36}) \|", prompt, flags=re.MULTILINE)
        entries = [
            {
                "use_id": use_id,
                "verdict": self.verdicts[index % len(self.verdicts)],
                "reason": "transcript shows direct reliance",
            }
            for index, use_id in enumerate(offered)
        ]
        return json.dumps({"verdicts": entries})


def assistant_turn(text: str) -> TranscriptTurn:
    return TranscriptTurn(
        role="assistant",
        text=text,
        tool_names=(),
        file_paths=(),
        is_meta=False,
        is_compact_summary=False,
    )


def build_platform(tmp_path: Path, name: str, *, judge: StubJudgeTransport | None = None):
    return AgentMemoryPlatform.create(
        graph_path=tmp_path / f"{name}.sqlite",
        project_id="loop-project",
        agent_ids=(AGENT_ID,),
        mode=AgentMemoryMode.SIMPLE,
        user_id="loop-user",
        rollup_synthesis_transport=judge,
    )


async def seed_session(platform: AgentMemoryPlatform) -> tuple[str, str, str]:
    """Remember three facts and start a session; returns their relationship uuids."""
    uuid_ref = (
        await platform.memory_remember(
            agent_id=AGENT_ID,
            subject="Deploy pipeline",
            predicate="requires",
            object="manual approval gate",
            relationship_type="REQUIRES",
        )
    ).relationship_uuid
    uuid_tokens = (
        await platform.memory_remember(
            agent_id=AGENT_ID,
            subject="Retrieval reranker",
            predicate="should",
            object="weight utility signals",
            relationship_type="SHOULD",
        )
    ).relationship_uuid
    uuid_uncited = (
        await platform.memory_remember(
            agent_id=AGENT_ID,
            subject="Legacy dashboard",
            predicate="should",
            object="archive quarterly exports",
            relationship_type="SHOULD",
        )
    ).relationship_uuid
    started = await platform.memory_start(
        agent_id=AGENT_ID,
        task_run_id=f"claude:{SESSION_ID}:startup:1",
    )
    injected_uuids = {event.relationship_uuid for event in started.use_events}
    assert {uuid_ref, uuid_tokens, uuid_uncited} <= injected_uuids
    return uuid_ref, uuid_tokens, uuid_uncited


def citing_turns(uuid_ref: str) -> tuple[TranscriptTurn, ...]:
    return (
        assistant_turn(f"Per prior memory (REF {uuid_ref}) the gate stays manual."),
        assistant_turn(
            "Updated the plan: the Retrieval reranker should weight utility signals during the final stage."
        ),
    )


@pytest.mark.asyncio
async def test_transcript_citations_record_cited_or_used_idempotently(tmp_path: Path) -> None:
    platform = build_platform(tmp_path, "citations")
    uuid_ref, uuid_tokens, uuid_uncited = await seed_session(platform)
    turns = citing_turns(uuid_ref)

    result = await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:pre-compact:2",
    )
    assert result.scanned_events >= 3
    assert result.cited_count == 2
    assert set(result.cited_relationship_uuids) == {uuid_ref, uuid_tokens}
    assert len(result.use_event_ids) == 2

    user_scope = platform.user_scope
    assert user_scope is not None
    cited_events = [
        event
        for event in platform.client.graph.use_events(scope_key=user_scope.key)
        if event.kind == UseEventKind.CITED_OR_USED
    ]
    assert {event.relationship_uuid for event in cited_events} == {uuid_ref, uuid_tokens}
    assert {event.idempotency_key for event in cited_events} == {
        f"{SESSION_ID}:cited:{uuid_ref}",
        f"{SESSION_ID}:cited:{uuid_tokens}",
    }
    matched_by = {event.relationship_uuid: event.metadata.get("matched_by") for event in cited_events}
    assert matched_by[uuid_ref] == "uuid"
    assert matched_by[uuid_tokens] == "tokens"
    assert uuid_uncited not in {event.relationship_uuid for event in cited_events}

    # Re-firing the hook (fresh task_run_id) records nothing new and returns
    # the SAME use-event ids.
    rerun = await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:3",
    )
    assert set(rerun.use_event_ids) == set(result.use_event_ids)
    after = [
        event
        for event in platform.client.graph.use_events(scope_key=user_scope.key)
        if event.kind == UseEventKind.CITED_OR_USED
    ]
    assert len(after) == 2

    # T9 end-to-end retention input: cited rows now carry use_stability > 0.
    for uuid in (uuid_ref, uuid_tokens):
        projections = await platform.client.memory_utility(scope=user_scope, relationship_uuid=uuid)
        assert projections and projections[0].use_stability > 0.0
        assert projections[0].cited_or_used_count == 1
    uncited_projection = await platform.client.memory_utility(scope=user_scope, relationship_uuid=uuid_uncited)
    assert uncited_projection[0].use_stability == 0.0  # injected-only: no reward
    assert uncited_projection[0].cited_or_used_count == 0


@pytest.mark.asyncio
async def test_empty_transcript_and_zero_candidates_are_no_ops(tmp_path: Path) -> None:
    platform = build_platform(tmp_path, "noop")
    result = await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=(),
        session_id="sess-without-events",
        task_run_id="claude:sess-without-events:session-end:1",
    )
    assert result.scanned_events == 0
    assert result.cited_count == 0
    assert result.use_event_ids == ()


@pytest.mark.asyncio
async def test_session_judge_records_mixed_verdicts_idempotently(tmp_path: Path) -> None:
    judge = StubJudgeTransport(verdicts=["positive", "inconclusive"])
    platform = build_platform(tmp_path, "judge", judge=judge)
    uuid_ref, _uuid_tokens, _ = await seed_session(platform)
    turns = citing_turns(uuid_ref)
    citation = await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:2",
    )
    assert citation.cited_count == 2

    result = await platform.judge_session_outcomes(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:2",
    )
    assert result.judge_configured is True
    assert result.judge_identity == "session-judge:stub-judge:test@v1"
    assert result.judged_count == 2
    assert result.skipped_reason == ""
    assert len(judge.calls) == 1
    prompt, system_prompt = judge.calls[0]
    assert "STRICT JSON" in system_prompt
    assert "Cited memories (use_id | fact):" in prompt
    assert "[checkpoint:session-end]" in prompt

    user_scope = platform.user_scope
    assert user_scope is not None
    outcomes = platform.client.graph.outcome_events(scope_key=user_scope.key)
    assert len(outcomes) == 2
    assert {outcome.verdict for outcome in outcomes} == {
        OutcomeVerdict.POSITIVE,
        OutcomeVerdict.UNKNOWN,  # "inconclusive" maps to the explicit unknown verdict
    }
    for outcome in outcomes:
        assert outcome.judge_identity == "session-judge:stub-judge:test@v1"
        assert outcome.judge_version == "v1"
        assert outcome.idempotency_key.startswith(f"{SESSION_ID}:outcome:")

    # Idempotent: a re-fire judges nothing new (already judged) and calls no LLM.
    rerun = await platform.judge_session_outcomes(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:3",
    )
    assert rerun.judged_count == 0
    assert rerun.skipped_reason == "already_judged"
    assert len(judge.calls) == 1
    assert len(platform.client.graph.outcome_events(scope_key=user_scope.key)) == 2


@pytest.mark.asyncio
async def test_no_judge_configured_returns_explicit_result_with_zero_events(
    tmp_path: Path,
) -> None:
    platform = build_platform(tmp_path, "no-judge")
    uuid_ref, _, _ = await seed_session(platform)
    turns = citing_turns(uuid_ref)
    await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:2",
    )
    result = await platform.judge_session_outcomes(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:2",
    )
    assert result.judge_configured is False
    assert result.skipped_reason == "no_judge_configured"
    assert result.judged_count == 0
    user_scope = platform.user_scope
    assert user_scope is not None
    assert platform.client.graph.outcome_events(scope_key=user_scope.key) == []


@pytest.mark.parametrize(
    ("stub", "reason_prefix"),
    [
        (StubJudgeTransport(raw="not json"), "verdicts_json_parse_failed"),
        (StubJudgeTransport(raw=json.dumps({"verdicts": "nope"})), "verdicts_json_shape_invalid"),
        (
            StubJudgeTransport(
                raw=json.dumps(
                    {
                        "verdicts": [
                            {
                                "use_id": "00000000-0000-0000-0000-000000000000",
                                "verdict": "positive",
                                "reason": "hallucinated id",
                            }
                        ]
                    }
                )
            ),
            "unknown_use_id:",
        ),
    ],
)
@pytest.mark.asyncio
async def test_out_of_contract_judge_response_is_rejected_and_receipted(
    tmp_path: Path, stub: StubJudgeTransport, reason_prefix: str
) -> None:
    platform = build_platform(tmp_path, "judge-reject", judge=stub)
    uuid_ref, _, _ = await seed_session(platform)
    turns = citing_turns(uuid_ref)
    await platform.record_transcript_citations(
        agent_id=AGENT_ID,
        turns=turns,
        session_id=SESSION_ID,
        task_run_id=f"claude:{SESSION_ID}:session-end:2",
    )
    with pytest.raises(ValueError, match="judge response rejected"):
        await platform.judge_session_outcomes(
            agent_id=AGENT_ID,
            turns=turns,
            session_id=SESSION_ID,
            task_run_id=f"claude:{SESSION_ID}:session-end:2",
        )
    user_scope = platform.user_scope
    assert user_scope is not None
    assert platform.client.graph.outcome_events(scope_key=user_scope.key) == []
    agent_scope = platform.agent_scope(AGENT_ID)
    rejections = [
        receipt
        for receipt in platform.client.graph.receipts.receipts_for_scope(agent_scope.key)
        if receipt.decision_type == ReceiptDecisionType.SESSION_OUTCOME_JUDGE_REJECTED
    ]
    assert len(rejections) == 1
    assert rejections[0].decision_reason.startswith(reason_prefix)
    payload = json.loads(rejections[0].event_payload)
    assert payload["session_id"] == SESSION_ID
    assert payload["judge_identifier"] == "session-judge:stub-judge:test@v1"


def test_session_end_hook_contains_judge_failure_and_still_refreshes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full CLI path: citations recorded, judge rejects, hook exits 0."""
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
    config = load_project_config(root)

    judge = StubJudgeTransport(raw="not json")
    platform = AgentMemoryPlatform.create(
        graph_path=graph_path,
        project_id=config.project.id,
        agent_ids=(config.caller.id,),
        mode=config.mode,
        user_id="hook-user",
        rollup_synthesis_transport=judge,
    )

    async def _seed() -> tuple[str, str]:
        remembered = await platform.memory_remember(
            agent_id=config.caller.id,
            subject="Deploy pipeline",
            predicate="requires",
            object="manual approval gate",
            relationship_type="REQUIRES",
        )
        started = await platform.memory_start(
            agent_id=config.caller.id,
            task_run_id="claude:sess-hook:startup:1",
        )
        assert started.use_events
        scope_key = next(
            event.scope.key for event in started.use_events if event.relationship_uuid == remembered.relationship_uuid
        )
        return remembered.relationship_uuid, scope_key

    import asyncio

    cited_uuid, cited_scope_key = asyncio.run(_seed())

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"Kept the gate manual (REF {cited_uuid})."}],
                },
            }
        ),
        encoding="utf-8",
    )

    import memotron.cli as cli_module

    monkeypatch.setattr(cli_module, "build_platform_from_project", lambda _root: (platform, config))
    monkeypatch.setattr(sys, "argv", ["memotron", "hook", "session-end", "--project-root", str(root)])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "sess-hook",
                    "hook_event_name": "SessionEnd",
                    "reason": "logout",
                    "transcript_path": str(transcript),
                }
            )
        ),
    )
    cli_main()  # must exit 0: no SystemExit despite the rejected judge response

    store = PropertyGraphStore(graph_path)
    try:
        cited = [
            event for event in store.use_events(scope_key=cited_scope_key) if event.kind == UseEventKind.CITED_OR_USED
        ]
        assert [event.relationship_uuid for event in cited] == [cited_uuid]
        assert store.outcome_events(scope_key=cited_scope_key) == []
        agent_scope_key = f"agent:{config.caller.id}"
        rejections = [
            receipt
            for receipt in store.receipts.receipts_for_scope(agent_scope_key)
            if receipt.decision_type == ReceiptDecisionType.SESSION_OUTCOME_JUDGE_REJECTED
        ]
        assert len(rejections) == 1
        checkpoints = [
            episode
            for episode in store.episodes_for_scope(agent_scope_key)
            if episode.metadata.get("checkpoint_reason") == "session_end"
        ]
        assert len(checkpoints) == 1
    finally:
        store.close()
