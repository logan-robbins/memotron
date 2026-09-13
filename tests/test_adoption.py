from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from memotron import AgentMemoryMode, ScopeKind, agent_scope
from memotron.adoption import (
    AdoptionStorage,
    build_platform_from_project,
    initialize_claude_code_project,
    load_project_config,
    sanitize_checkpoint,
    save_project_config,
)
from memotron.cli import main as cli_main
from memotron.extraction import OpenAICompatibleExtractionTransport


def _git_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(("git", "init", str(path)), check=True, capture_output=True)
    subprocess.run(
        (
            "git",
            "-C",
            str(path),
            "remote",
            "add",
            "origin",
            "git@github.example.com:parks/sample-repo.git",
        ),
        check=True,
    )
    subprocess.run(
        ("git", "-C", str(path), "config", "user.email", "engineer@example.com"),
        check=True,
    )
    return path


def test_claude_code_init_defaults_to_simple_and_is_idempotent(tmp_path: Path) -> None:
    root = _git_repo(tmp_path / "sample-repo")
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"existing": {"command": "existing"}}}),
        encoding="utf-8",
    )
    settings = root / ".claude/settings.json"
    settings.parent.mkdir()
    settings.write_text(
        json.dumps({"permissions": {"allow": ["Bash(git status)"]}}),
        encoding="utf-8",
    )
    legacy_skill = root / ".claude/skills/memory/SKILL.md"
    legacy_skill.parent.mkdir(parents=True)
    legacy_skill.write_text(
        "---\nname: memory\n---\n# Memotron memory\n",
        encoding="utf-8",
    )

    first = initialize_claude_code_project(project_root=root)
    second = initialize_claude_code_project(project_root=root)

    config = load_project_config(root)
    assert config.mode == AgentMemoryMode.SIMPLE
    assert config.agent_guidance.cadence == "event-driven"
    assert config.project.id == "github.example.com/parks/sample-repo"
    assert config.project.name == "sample-repo"
    assert first["user_id"] == second["user_id"]
    assert first["user_id"].startswith("git-")

    mcp = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["existing"]["command"] == "existing"
    memotron = mcp["mcpServers"]["memotron_agent_memory"]
    assert memotron["type"] == "stdio"
    assert memotron["command"] == "memotron"
    assert memotron["alwaysLoad"] is True

    installed_settings = json.loads(settings.read_text(encoding="utf-8"))
    assert installed_settings["permissions"]["allow"] == ["Bash(git status)"]
    assert set(installed_settings["hooks"]) == {
        "SessionStart",
        "PreCompact",
        "PostCompact",
        "SessionEnd",
    }
    assert all(len(installed_settings["hooks"][event]) == 1 for event in installed_settings["hooks"])
    assert not legacy_skill.exists()
    skill = (root / ".claude/skills/memotron-memory/SKILL.md").read_text(encoding="utf-8")
    assert "simple` mode" in skill
    assert "/memotron-memory status" in skill
    rule = (root / ".claude/rules/memotron.md").read_text(encoding="utf-8")
    assert "hooks register the caller" in rule
    assert "Treat loaded memory as historical evidence" in rule
    assert "Write zero memories when no durable fact was confirmed" in rule
    assert "## Publish project candidates" in rule
    assert first["next_steps"] == second["next_steps"]
    assert first["next_steps"][-1].startswith("Run /memotron-memory status")
    assert str(root / ".claude/rules/memotron.md") in first["files"]

    customized = config.model_copy(
        update={
            "agent_guidance": config.agent_guidance.model_copy(
                update={"publish_when": ("A confirmed protocol change affects future sessions.",)}
            )
        }
    )
    save_project_config(root, customized)
    initialize_claude_code_project(project_root=root)
    customized_rule = (root / ".claude/rules/memotron.md").read_text(encoding="utf-8")
    assert "A confirmed protocol change affects future sessions." in customized_rule


@pytest.mark.asyncio
async def test_project_mode_switch_changes_durable_memory_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = _git_repo(tmp_path / "sample-repo")
    initialize_claude_code_project(project_root=root)
    config = load_project_config(root).model_copy(
        update={"storage": AdoptionStorage(graph_path=str(tmp_path / "shared-memory.sqlite"))}
    )
    save_project_config(root, config)

    simple, _ = build_platform_from_project(root)
    try:
        assert simple.mode == AgentMemoryMode.SIMPLE
        assert simple.user_scope is not None
        remembered = await simple.memory_remember(
            agent_id="claude-code",
            subject="engineer",
            predicate="prefers",
            object="small pull requests",
            relationship_type="PREFERS",
        )
        assert remembered.scope.kind == ScopeKind.USER
        explanation = await simple.memory_explain(
            agent_id="claude-code",
            relationship_uuid=remembered.relationship_uuid,
        )
        assert explanation.scope.kind == ScopeKind.USER
        published = await simple.memory_publish(
            agent_id="claude-code",
            content=(
                "Memory: subject=sample repo; predicate=decided; "
                "object=default to simple memory mode; "
                "relationship_type=DECIDES; confidence=0.9"
            ),
            task_run_id="adoption-test",
            source_reference="tests/test_adoption.py",
        )
        candidates = simple.project_memory_candidates(agent_id="claude-code")
        assert candidates[0]["episode_uuid"] == published.episode_uuid
        assert candidates[0]["processed"] is False
        await simple.memory_refresh(
            agent_id="claude-code",
            include_project=True,
        )
        project_relationship = next(
            item
            for item in simple.client.graph.active_relationships(scope=simple.project_scope)
            if item.type == "DECIDES"
        )
        with pytest.raises(ValueError, match="cannot forget project memory directly"):
            await simple.memory_forget(
                agent_id="claude-code",
                relationship_uuid=project_relationship.uuid,
                reason="agent attempted project retirement",
                scope="project",
            )
        forgotten = await simple.memory_forget(
            agent_id="claude-code",
            relationship_uuid=remembered.relationship_uuid,
            reason="test confirmed removal",
        )
        assert forgotten.scope.kind == ScopeKind.USER
        assert forgotten.status.value == "pruned"
        assert len(simple.project_memory_status()["versions"]) == 1
    finally:
        simple.client.graph.close()

    multi_config = config.model_copy(update={"mode": AgentMemoryMode.MULTI_AGENT})
    save_project_config(root, multi_config)
    multi, _ = build_platform_from_project(root)
    try:
        assert multi.mode == AgentMemoryMode.MULTI_AGENT
        assert multi.user_scope is None
        remembered = await multi.memory_remember(
            agent_id="claude-code",
            subject="claude code",
            predicate="should",
            object="keep agent memory isolated",
            relationship_type="SHOULD",
        )
        assert remembered.scope.key == "agent:claude-code"
        assert len(multi.project_memory_status()["versions"]) == 1
        contract = multi.integration_contract(platform_api_url="", mcp_url="")
        assert contract["memory_owners"] == ["agent", "project"]
        assert contract["scope_rules"]["agent"]["meaning"].startswith("Pod-local")
    finally:
        multi.client.graph.close()


def test_compact_summary_checkpoint_is_bounded_and_redacted() -> None:
    checkpoint = sanitize_checkpoint(
        (
            "Implemented simple mode for engineer@example.com. "
            "api_key=sk-secretvalue123456 and Authorization: "
            "Bearer abcdefghijklmnop"
        ),
        max_characters=300,
    )
    assert "engineer@example.com" not in checkpoint
    assert "secretvalue" not in checkpoint
    assert "abcdefghijklmnop" not in checkpoint
    assert checkpoint.count("[REDACTED]") == 3


def test_local_litellm_configuration_uses_env_secret_and_safe_project_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = _git_repo(tmp_path / "sample-repo")
    initialize_claude_code_project(project_root=root)
    config = load_project_config(root).model_copy(
        update={"storage": AdoptionStorage(graph_path=str(tmp_path / "litellm-memory.sqlite"))}
    )
    save_project_config(root, config)
    monkeypatch.setenv("LITELLM_API_KEY", "sk-local-litellm-secret")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memotron",
            "llm",
            "configure",
            "--project-root",
            str(root),
            "--provider",
            "litellm",
            "--base-url",
            "http://127.0.0.1:4000",
            "--model",
            "memotron-memory",
        ],
    )
    cli_main()
    output = capsys.readouterr().out
    assert "sk-local-litellm-secret" not in output

    config = load_project_config(root)
    assert config.llm.provider == "litellm"
    assert config.llm.base_url == "http://127.0.0.1:4000"
    assert config.llm.model == "memotron-memory"
    assert config.llm.api_key_env == "LITELLM_API_KEY"

    monkeypatch.delenv("LITELLM_API_KEY")
    platform, _ = build_platform_from_project(root)
    try:
        transport = platform.client._extractor._transport
        assert isinstance(transport, OpenAICompatibleExtractionTransport)
        assert transport.base_url == "http://127.0.0.1:4000"
        assert transport.model == "memotron-memory"
        status = platform.llm_credential_state()
        assert status is not None
        assert status["provider"] == "litellm"
        assert "sk-local-litellm-secret" not in json.dumps(status)
    finally:
        platform.client.graph.close()


def test_post_compact_hook_persists_only_sanitized_continuity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Hook mechanics + sanitization are asserted against exact continuity text,
    # so the extraction must be the deterministic rule-based path — clear any
    # ambient gateway credential (a developer .env, CI secret) the same way the
    # other lifecycle-hook tests above do.
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("MEMOTRON_LLM_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    root = _git_repo(tmp_path / "sample-repo")
    initialize_claude_code_project(project_root=root)
    config = load_project_config(root).model_copy(
        update={"storage": AdoptionStorage(graph_path=str(tmp_path / "hook-memory.sqlite"))}
    )
    save_project_config(root, config)
    hook_input = {
        "session_id": "session-42",
        "hook_event_name": "PostCompact",
        "compact_summary": ("Implement simple mode. password=do-not-store-this and notify engineer@example.com."),
    }
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "memotron",
            "hook",
            "post-compact",
            "--project-root",
            str(root),
        ],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook_input)))
    cli_main()

    platform, _ = build_platform_from_project(root)
    try:
        episodes = platform.client.graph.episodes_for_scope("agent:claude-code")
        assert len(episodes) == 1
        assert "do-not-store-this" not in episodes[0].body
        assert "engineer@example.com" not in episodes[0].body
        relationships = platform.client.graph.active_relationships(scope=agent_scope("claude-code"))
        state = next(item for item in relationships if item.type == "HAS_STATE")
        # The task focus survives into the state fact; assert the substantive
        # phrase rather than an exact rendering, since rule-based extraction may
        # reshape the imperative summary ("Implement simple mode") into a state
        # noun phrase ("simple mode implementation") — either captures the task.
        assert "simple mode" in state.properties["fact"].lower()
    finally:
        platform.client.graph.close()


def _hook_repo(tmp_path: Path, name: str) -> Path:
    root = _git_repo(tmp_path / "sample-repo")
    initialize_claude_code_project(project_root=root)
    config = load_project_config(root).model_copy(
        update={"storage": AdoptionStorage(graph_path=str(tmp_path / f"{name}.sqlite"))}
    )
    save_project_config(root, config)
    return root


def _write_transcript(path: Path, entries: list[dict]) -> Path:
    path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
    return path


def _run_hook_cli(monkeypatch: pytest.MonkeyPatch, root: Path, event: str, hook_input: dict) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["memotron", "hook", event, "--project-root", str(root)],
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(hook_input)))
    cli_main()


def _agent_episodes(root: Path):
    platform, _ = build_platform_from_project(root)
    try:
        return platform.client.graph.episodes_for_scope("agent:claude-code")
    finally:
        platform.client.graph.close()


def test_pre_compact_hook_captures_dying_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """PreCompact must persist REAL transcript state, never a placeholder."""
    root = _hook_repo(tmp_path, "pre-compact-capture")
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": "harness banner"},
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": "Refactor the retrieval pipeline for utility weighting",
                },
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Reranker updated; running tests."},
                        {
                            "type": "tool_use",
                            "name": "Edit",
                            "input": {"file_path": "src/memotron/retrieval.py"},
                        },
                    ],
                },
            },
        ],
    )
    _run_hook_cli(
        monkeypatch,
        root,
        "pre-compact",
        {
            "session_id": "session-42",
            "hook_event_name": "PreCompact",
            "trigger": "auto",
            "transcript_path": str(transcript),
        },
    )
    episodes = _agent_episodes(root)
    assert len(episodes) == 1
    body = episodes[0].body
    assert "Task focus: Refactor the retrieval pipeline for utility weighting" in body
    assert "Recent assistant state: Reranker updated; running tests." in body
    assert "Files touched: src/memotron/retrieval.py" in body
    assert "compaction started" not in body  # the old constant placeholder
    assert episodes[0].metadata.get("checkpoint_reason") == "context_compaction"


def test_post_compact_hook_derives_summary_from_transcript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Without the synthetic compact_summary field, the transcript's own
    isCompactSummary entry is persisted (the real-harness shape)."""
    root = _hook_repo(tmp_path, "post-compact-transcript")
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            {
                "type": "user",
                "isCompactSummary": True,
                "message": {
                    "role": "user",
                    "content": (
                        "Session summary: implemented utility weighting; "
                        "tests pending for contact engineer@example.com."
                    ),
                },
            },
        ],
    )
    _run_hook_cli(
        monkeypatch,
        root,
        "post-compact",
        {
            "session_id": "session-42",
            "hook_event_name": "PostCompact",
            "transcript_path": str(transcript),
        },
    )
    episodes = _agent_episodes(root)
    assert len(episodes) == 1
    body = episodes[0].body
    assert "Session summary: implemented utility weighting" in body
    assert "engineer@example.com" not in body  # redaction still applies


def test_post_compact_hook_survives_missing_summary_and_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real-harness firing with no summary must exit 0 and still checkpoint
    the boundary (pre-fix behaviour: ValueError -> exit code 2, no refresh)."""
    root = _hook_repo(tmp_path, "post-compact-minimal")
    _run_hook_cli(
        monkeypatch,
        root,
        "post-compact",
        {"session_id": "session-42", "hook_event_name": "PostCompact"},
    )
    episodes = _agent_episodes(root)
    assert len(episodes) == 1
    body = episodes[0].body
    assert "[checkpoint:post-compact] session=session-42" in body
    assert "Transcript unavailable at hook time" in body


def test_session_end_hook_flushes_final_checkpoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _hook_repo(tmp_path, "session-end-flush")
    transcript = _write_transcript(
        tmp_path / "transcript.jsonl",
        [
            {
                "type": "user",
                "message": {"role": "user", "content": "Ship the audit fixes"},
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Batch 0 landed; hooks next."}],
                },
            },
        ],
    )
    _run_hook_cli(
        monkeypatch,
        root,
        "session-end",
        {
            "session_id": "session-9",
            "hook_event_name": "SessionEnd",
            "reason": "logout",
            "transcript_path": str(transcript),
        },
    )
    episodes = _agent_episodes(root)
    assert len(episodes) == 1
    body = episodes[0].body
    assert "[checkpoint:session-end] session=session-9 trigger=logout" in body
    assert "Task focus: Ship the audit fixes" in body
    assert "Recent assistant state: Batch 0 landed; hooks next." in body
    assert episodes[0].metadata.get("checkpoint_reason") == "session_end"
