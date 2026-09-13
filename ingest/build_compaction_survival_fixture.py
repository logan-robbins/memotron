"""WS-28 T3 (+ T1 companion): build the compaction-survival gold-set fixture graph.

Writes a tiny, deterministic, hermetic SQLite graph exercising the exact
regression the ``compaction_survival`` section of
``ingest/actionability_goldset.yaml`` scores against:

  1. A shell command invoked 3x with a STABLE (byte-identical) shape must
     become a durable, verbatim, context-visible runbook fact
     (``AgentMemoryPlatform.record_runbook_commands``, WS-28 T3).
  2. A DIFFERENT command (same prefix, different flag) also invoked 3x must
     become its OWN separate current fact -- never merged into (1) by
     write-side semantic dedup, which is exactly what
     ``dreaming._find_reinforce_target``'s ``force_identifier_conflict``
     (set from ``metadata["runbook_capture"]``) exists to guarantee: the two
     command strings are lexically close enough (same "uv run
     examples/simulation.py" prefix) that the hermetic embedding transport's
     cosine similarity clears the default semantic-dedup threshold, so this
     fixture would collapse to ONE fact pre-guard.
  3. A command invoked only 2x (below ``repeated_commands``'s default
     ``min_repeats=3`` threshold) must never be captured at all -- one-off
     commands are not memory.
  4. A ``context_compaction`` checkpoint (the same shape ``PreCompact``
     queues) must, once dreamt, become a durable, context-visible fact
     carrying its task-focus text -- the T1 companion: the checkpoint that
     seeds immediate rehydration (read raw, pre-dream, by
     ``latest_compaction_checkpoint``) is ALSO ordinary durable memory once
     the next dream cycle runs.

Standalone, hermetic, and zero network calls: ``AgentMemoryPlatform.create``
with no LLM transport configured uses the deterministic rule-based
extraction fallback throughout.

Usage
-----
    uv run ingest/build_compaction_survival_fixture.py

Rebuilds ``ingest/.memotron/compaction_survival_fixture.sqlite`` from
scratch every time (deletes any existing file first) so the fixture can
never silently drift from this script.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from memotron import AgentMemoryMode, AgentMemoryPlatform
from memotron.transcripts import TranscriptTurn

INGEST_DIR = Path(__file__).resolve().parent
GRAPH_DIR = INGEST_DIR / ".memotron"
FIXTURE_PATH = GRAPH_DIR / "compaction_survival_fixture.sqlite"

AGENT_ID = "compaction-fixture-agent"
LIVE_SIM_COMMAND = "uv run examples/simulation.py --live"
LIVE_FAST_COMMAND = "uv run examples/simulation.py --live-fast"
BELOW_THRESHOLD_COMMAND = "uv run pytest tests/test_context.py"
CHECKPOINT_SUMMARY = "Task focus: implementing WS-28 compaction survival end to end."


def _bash_turn(command: str) -> TranscriptTurn:
    return TranscriptTurn(
        role="assistant",
        text="",
        tool_names=("Bash",),
        file_paths=(),
        is_meta=False,
        is_compact_summary=False,
        commands=(command,),
    )


async def build() -> None:
    GRAPH_DIR.mkdir(parents=True, exist_ok=True)
    if FIXTURE_PATH.exists():
        FIXTURE_PATH.unlink()

    platform = AgentMemoryPlatform.create(
        graph_path=FIXTURE_PATH,
        project_id="compaction-survival-fixture",
        agent_ids=(AGENT_ID,),
        mode=AgentMemoryMode.SIMPLE,
        user_id="compaction-fixture-user",
    )

    # Step 1-3: a transcript window with two >=3-repeat commands (different
    # shapes) and one 2-repeat command (below threshold).
    turns = (
        *([_bash_turn(LIVE_SIM_COMMAND)] * 3),
        *([_bash_turn(LIVE_FAST_COMMAND)] * 3),
        *([_bash_turn(BELOW_THRESHOLD_COMMAND)] * 2),
    )
    capture = await platform.record_runbook_commands(
        agent_id=AGENT_ID,
        turns=turns,
        task_run_id="claude:compaction-fixture-session:runbook:1",
    )
    assert set(capture.captured_commands) == {LIVE_SIM_COMMAND, LIVE_FAST_COMMAND}, (
        "fixture invariant broken: expected exactly the two >=3-repeat "
        f"commands captured, got {capture.captured_commands!r}"
    )

    # Step 4: a context_compaction checkpoint, queued the same shape
    # PreCompact queues one.
    await platform.memory_log(
        agent_id=AGENT_ID,
        summary=CHECKPOINT_SUMMARY,
        checkpoint_reason="context_compaction",
        task_run_id="claude:compaction-fixture-session:checkpoint:1",
    )

    refreshed = await platform.memory_refresh(agent_id=AGENT_ID, include_project=False)
    print(f"wrote {FIXTURE_PATH}")
    print(f"  captured commands: {capture.captured_commands!r}")
    print(f"  processed episodes: {refreshed.agent_run.processed_episodes}")
    print(f"  created relationships: {refreshed.agent_run.created_relationships}")


def main() -> int:
    asyncio.run(build())
    return 0


if __name__ == "__main__":
    sys.exit(main())
