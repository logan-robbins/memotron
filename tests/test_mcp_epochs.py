"""WS-26 F3: operator-MCP epoch-diff surface — ``redream_epochs`` / ``redream_diff``.

Hermetic: the client is built directly via ``Memotron(config=..., graph_path=...)``,
exactly like ``tests/test_redream_end_to_end.py``, bypassing
``memotron.mcp_server.build_client()`` (and its env-driven
``build_transports_from_env()``) entirely — so this test makes zero network
calls no matter what LLM environment variables or ``.env`` files are present
on the host. Formation runs through ``RuleBasedExtractionTransport`` only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import memotron.mcp_server as mcp_server
from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    MemoryScope,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.config import ActionabilityPolicy
from memotron.epochs import EpisodeSelector
from memotron.mcp_server import redream_diff, redream_epochs
from memotron.models import DreamJobKind


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _instructions() -> DreamInstructionSet:
    return DreamInstructionSet(
        name="default",
        node_instructions=(NodeInstruction(label="Entity", query="entities", properties=(), strict_properties=False),),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="prefs",
            ),
        ),
    )


def _config() -> DreamConfig:
    return DreamConfig(
        instruction_sets=(_instructions(),),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        actionability=ActionabilityPolicy(enabled=False),
    )


def _line(subject: str, predicate: str, obj: str, confidence: float = 0.9) -> str:
    return (
        f"Memory: subject={subject}; predicate={predicate}; object={obj}; "
        f"relationship_type=PREFERS; confidence={confidence}"
    )


@pytest.mark.asyncio
async def test_redream_epochs_and_diff_tools_over_a_branched_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    monkeypatch.setattr(mcp_server, "_client", client)

    scope = _scope("mcp-epochs-a")
    now = datetime.now(UTC)
    await client.add_episode(
        name="ep1",
        episode_body=_line("Nia", "prefers", "tea"),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="d",
        reference_time=now,
    )
    await client.run_dream_job(job_name="formation-default", now=now)

    # One epoch exists (the root, adopted at formation time).
    root_payload = json.loads(await redream_epochs(scope_kind="user", scope_id=scope.scope_id))
    assert len(root_payload["epochs"]) == 1
    assert root_payload["active_epoch_id"] is not None
    root_epoch_id = root_payload["active_epoch_id"]

    # Branch a new epoch off the same episode.
    episode_uuid = client.graph.episodes_for_scope(scope.key)[0].uuid
    selector = EpisodeSelector(episode_uuids=(episode_uuid,))
    branch_result = await client.redream_branch(scope=scope, selector=selector, now=now + timedelta(seconds=5))

    epochs_payload = json.loads(await redream_epochs(scope_kind="user", scope_id=scope.scope_id))
    assert len(epochs_payload["epochs"]) >= 2
    assert epochs_payload["active_epoch_id"] == root_epoch_id  # branch not adopted yet
    assert any(row["epoch_id"] == branch_result.epoch_id for row in epochs_payload["epochs"])

    diff_payload = json.loads(
        await redream_diff(scope_kind="user", scope_id=scope.scope_id, epoch_id=branch_result.epoch_id)
    )
    assert diff_payload["epoch_id"] == branch_result.epoch_id
    diff = diff_payload["diff"]
    for key in ("added", "removed", "changed", "unchanged"):
        assert key in diff
    for key in (
        "aliases_added",
        "aliases_changed",
        "aliases_removed",
        "canonicals_added",
        "canonicals_changed",
        "canonicals_removed",
    ):
        assert key in diff
    assert [entry["fact"] for entry in diff["unchanged"]] == ["Nia prefers tea"]
    assert diff["added"] == []


@pytest.mark.asyncio
async def test_redream_diff_rejects_a_blank_epoch_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "graph.sqlite")
    monkeypatch.setattr(mcp_server, "_client", client)

    with pytest.raises(ValueError):
        await redream_diff(scope_kind="user", scope_id="mcp-epochs-b", epoch_id="")
