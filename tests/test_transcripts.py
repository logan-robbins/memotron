"""Deterministic transcript parsing + checkpoint derivation (WS-14).

Closes AUDIT.md §5: PreCompact/SessionEnd must capture real dying-context
state and PostCompact must survive (and use) real harness inputs.
"""

from __future__ import annotations

import json
from pathlib import Path

from memotron.transcripts import (
    assistant_text_corpus,
    derive_checkpoint,
    extract_compact_summary,
    parse_transcript_file,
    parse_transcript_lines,
)


def _entry(
    entry_type: str,
    content,
    *,
    is_meta: bool = False,
    is_compact_summary: bool = False,
) -> str:
    payload = {
        "type": entry_type,
        "message": {"role": entry_type, "content": content},
    }
    if is_meta:
        payload["isMeta"] = True
    if is_compact_summary:
        payload["isCompactSummary"] = True
    return json.dumps(payload)


def test_parse_recognizes_string_and_block_content() -> None:
    lines = [
        _entry("user", "Refactor the retrieval pipeline"),
        _entry(
            "assistant",
            [
                {"type": "text", "text": "Editing the reranker now."},
                {
                    "type": "tool_use",
                    "name": "Edit",
                    "input": {"file_path": "src/memotron/retrieval.py"},
                },
                {
                    "type": "tool_use",
                    "name": "Bash",
                    "input": {"command": "uv run pytest"},
                },
            ],
        ),
    ]
    turns = parse_transcript_lines(lines)
    assert len(turns) == 2
    assert turns[0].role == "user"
    assert turns[0].text == "Refactor the retrieval pipeline"
    assert turns[1].role == "assistant"
    assert turns[1].text == "Editing the reranker now."
    assert turns[1].tool_names == ("Edit", "Bash")
    assert turns[1].file_paths == ("src/memotron/retrieval.py",)


def test_parse_skips_unknown_shapes_and_malformed_lines() -> None:
    lines = [
        "not json at all {",
        json.dumps({"type": "summary", "summary": "sidechain title"}),
        json.dumps({"type": "system", "content": "system noise"}),
        json.dumps({"type": "user"}),  # no message
        json.dumps({"type": "user", "message": {"role": "user", "content": []}}),  # empty
        _entry("assistant", "Real content survives."),
    ]
    turns = parse_transcript_lines(lines)
    assert len(turns) == 1
    assert turns[0].text == "Real content survives."


def test_compact_summary_extraction_prefers_latest() -> None:
    lines = [
        _entry("user", "old summary", is_compact_summary=True),
        _entry("user", "middle turn"),
        _entry("user", "new summary", is_compact_summary=True),
    ]
    turns = parse_transcript_lines(lines)
    assert extract_compact_summary(turns) == "new summary"
    assert extract_compact_summary(parse_transcript_lines([_entry("user", "hi")])) is None


def test_derive_checkpoint_sections_and_exclusions() -> None:
    lines = [
        _entry("user", "harness injected context", is_meta=True),
        _entry("user", "Fix the failing supersession test"),
        _entry("user", "a compact summary", is_compact_summary=True),
        _entry(
            "assistant",
            [
                {"type": "text", "text": "Root cause is the truth-key mismatch."},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}},
                {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}},
                {"type": "tool_use", "name": "Write", "input": {"path": "src/b.py"}},
            ],
        ),
    ]
    turns = parse_transcript_lines(lines)
    checkpoint = derive_checkpoint(turns, event="pre-compact", session_id="s-1", trigger="auto")
    assert checkpoint.startswith("[checkpoint:pre-compact] session=s-1 trigger=auto")
    # Task focus is the last REAL user turn — meta and compact-summary turns excluded.
    assert "Task focus: Fix the failing supersession test" in checkpoint
    assert "harness injected context" not in checkpoint
    assert "Task focus: a compact summary" not in checkpoint
    assert "Recent assistant state: Root cause is the truth-key mismatch." in checkpoint
    assert "Tools used: Edit, Write" in checkpoint
    assert "Files touched: src/a.py, src/b.py" in checkpoint
    # Deterministic: same input, same output.
    assert checkpoint == derive_checkpoint(turns, event="pre-compact", session_id="s-1", trigger="auto")


def test_derive_checkpoint_without_transcript_records_boundary() -> None:
    checkpoint = derive_checkpoint((), event="post-compact", session_id="s-9")
    assert checkpoint.startswith("[checkpoint:post-compact] session=s-9")
    assert "Transcript unavailable at hook time" in checkpoint


def test_parse_transcript_file_missing_and_tail_window(tmp_path: Path) -> None:
    assert parse_transcript_file(tmp_path / "missing.jsonl") == ()

    path = tmp_path / "big.jsonl"
    filler = _entry("user", "early " + "x" * 200)
    tail_line = _entry("assistant", "final state after long session")
    path.write_text("\n".join([filler] * 50 + [tail_line]), encoding="utf-8")
    turns = parse_transcript_file(path, max_bytes=len(tail_line) + 40)
    # The partial first line of the tail window is dropped; the intact final
    # line survives.
    assert turns
    assert turns[-1].text == "final state after long session"
    assert all("early" not in turn.text for turn in turns)


def test_assistant_text_corpus_joins_assistant_turns_only() -> None:
    lines = [
        _entry("user", "question"),
        _entry("assistant", "first answer"),
        _entry("assistant", "second answer"),
    ]
    corpus = assistant_text_corpus(parse_transcript_lines(lines))
    assert corpus == "first answer\nsecond answer"
