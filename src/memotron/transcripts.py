"""Deterministic Claude Code transcript parsing for lifecycle checkpoints.

Claude Code hook events (PreCompact, PostCompact, SessionEnd) deliver a
``transcript_path`` pointing at the session's JSONL transcript.  This module
turns that transcript into *real* checkpoint content at compaction and
session boundaries — the capture the lifecycle hooks persist through
``memory_log`` instead of a constant placeholder.

Everything here is a pure function of the transcript bytes and the explicit
caps passed in: no model calls, no wall-clock reads, no randomness.  Unknown
line shapes are skipped (a transcript is external input and its schema can
grow), but every recognized shape is extracted exactly.

Recognized JSONL line shapes (current Claude Code transcript format):

- ``{"type": "user"|"assistant", "message": {"role": ..., "content": ...}}``
  where ``content`` is either a plain string or a list of blocks:
  ``{"type": "text", "text": ...}`` and
  ``{"type": "tool_use", "name": ..., "input": {...}}``.
- ``isMeta: true`` marks harness-injected turns (excluded from task focus).
- ``isCompactSummary: true`` marks the post-compaction summary entry that
  Claude Code writes back into the transcript after compaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MAX_BYTES = 4_000_000
"""Read at most this many bytes from the transcript tail.

Long sessions produce transcripts far larger than any checkpoint needs; the
tail always contains the most recent (and most checkpoint-relevant) turns.
"""

_FILE_PATH_INPUT_KEYS = ("file_path", "path", "notebook_path")
"""tool_use input keys that name a file the session touched."""

_COMMAND_TOOL_NAMES = ("Bash",)
"""tool_use names whose invocation is captured VERBATIM (WS-28 T3: runbook
capture).  ``Bash`` is Claude Code's built-in shell tool.  A project that
recognizes additional shell-like tools only ever ADDS names here -- the
verbatim-capture contract below never changes."""

_COMMAND_INPUT_KEY = "command"
"""tool_use input key carrying the exact invocation string for a recognized
command tool."""


@dataclass(frozen=True)
class TranscriptTurn:
    """One parsed conversation turn (user or assistant)."""

    role: str
    text: str
    tool_names: tuple[str, ...]
    file_paths: tuple[str, ...]
    is_meta: bool
    is_compact_summary: bool
    commands: tuple[str, ...] = ()
    """WS-28 T3: verbatim command-tool invocations from this turn, in call
    order, NOT deduplicated -- repeat count within and across turns is
    exactly the signal :func:`repeated_commands` needs.  Empty for a turn
    with no recognized command-tool call.  Defaulted so every existing
    ``TranscriptTurn(...)`` call site (tests included) stays valid."""


def _unique_in_order(values: list[str]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            ordered.append(value)
    return tuple(ordered)


def _parse_content_blocks(
    content: object,
) -> tuple[str, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Extract (text, tool_names, file_paths, commands) from a message content payload."""
    if isinstance(content, str):
        return content.strip(), (), (), ()
    texts: list[str] = []
    tool_names: list[str] = []
    file_paths: list[str] = []
    commands: list[str] = []
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text.strip())
            elif block_type == "tool_use":
                name = block.get("name")
                if isinstance(name, str) and name.strip():
                    tool_names.append(name.strip())
                tool_input = block.get("input")
                if isinstance(tool_input, dict):
                    for key in _FILE_PATH_INPUT_KEYS:
                        value = tool_input.get(key)
                        if isinstance(value, str) and value.strip():
                            file_paths.append(value.strip())
                    if isinstance(name, str) and name.strip() in _COMMAND_TOOL_NAMES:
                        command_value = tool_input.get(_COMMAND_INPUT_KEY)
                        if isinstance(command_value, str) and command_value.strip():
                            # WS-28 T3: verbatim -- stripped of surrounding
                            # whitespace only, never normalized/paraphrased.
                            commands.append(command_value.strip())
    return (
        "\n".join(texts).strip(),
        _unique_in_order(tool_names),
        _unique_in_order(file_paths),
        tuple(commands),
    )


def parse_transcript_lines(lines: list[str]) -> tuple[TranscriptTurn, ...]:
    """Parse raw JSONL lines into conversation turns; unknown shapes are skipped."""
    turns: list[TranscriptTurn] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        try:
            entry = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        if entry_type not in ("user", "assistant"):
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role")
        if not isinstance(role, str) or role not in ("user", "assistant"):
            role = str(entry_type)
        text, tool_names, file_paths, commands = _parse_content_blocks(message.get("content"))
        if not text and not tool_names and not file_paths and not commands:
            continue
        turns.append(
            TranscriptTurn(
                role=role,
                text=text,
                tool_names=tool_names,
                file_paths=file_paths,
                is_meta=bool(entry.get("isMeta")),
                is_compact_summary=bool(entry.get("isCompactSummary")),
                commands=commands,
            )
        )
    return tuple(turns)


def parse_transcript_file(path: Path, *, max_bytes: int = _DEFAULT_MAX_BYTES) -> tuple[TranscriptTurn, ...]:
    """Parse a transcript JSONL file tail; missing/unreadable files parse to ()."""
    try:
        raw = path.read_bytes()
    except OSError:
        return ()
    if len(raw) > max_bytes:
        raw = raw[-max_bytes:]
        # Drop the first (almost certainly partial) line of the tail window.
        newline = raw.find(b"\n")
        raw = raw[newline + 1 :] if newline >= 0 else b""
    try:
        decoded = raw.decode("utf-8", errors="replace")
    except Exception:  # pragma: no cover - decode with replace cannot raise
        return ()
    return parse_transcript_lines(decoded.splitlines())


def extract_compact_summary(turns: tuple[TranscriptTurn, ...]) -> str | None:
    """The most recent compaction summary Claude Code wrote into the transcript."""
    for turn in reversed(turns):
        if turn.is_compact_summary and turn.text:
            return turn.text
    return None


def assistant_text_corpus(turns: tuple[TranscriptTurn, ...]) -> str:
    """All assistant-authored text, newline-joined (use-event citation matching)."""
    return "\n".join(turn.text for turn in turns if turn.role == "assistant" and turn.text)


def derive_checkpoint(
    turns: tuple[TranscriptTurn, ...],
    *,
    event: str,
    session_id: str,
    trigger: str = "",
    max_chars: int = 5500,
) -> str:
    """Deterministic checkpoint text for a lifecycle boundary.

    Sections are emitted only when the transcript supports them; the header
    line is always present, so the result is never blank.  ``max_chars`` keeps
    the derived text under the ``sanitize_checkpoint`` bound so redaction —
    which keeps the *tail* — never cuts the header off mid-section by more
    than the transcript content itself requires.
    """
    header = f"[checkpoint:{event}] session={session_id or 'unknown'}"
    if trigger:
        header += f" trigger={trigger}"
    header += f" parsed_turns={len(turns)}"
    lines: list[str] = [header]

    if not turns:
        lines.append("Transcript unavailable at hook time; boundary recorded without conversation capture.")
        return "\n".join(lines)

    user_texts = [t.text for t in turns if t.role == "user" and t.text and not t.is_meta and not t.is_compact_summary]
    if user_texts:
        lines.append(f"Task focus: {user_texts[-1][:600]}")

    assistant_texts = [t.text for t in turns if t.role == "assistant" and t.text]
    lines.extend(f"Recent assistant state: {text[:500]}" for text in assistant_texts[-2:])

    tool_names = _unique_in_order([name for t in turns for name in t.tool_names])
    if tool_names:
        lines.append("Tools used: " + ", ".join(tool_names[:12]))

    file_paths = _unique_in_order([path for t in turns for path in t.file_paths])
    if file_paths:
        lines.append("Files touched: " + ", ".join(file_paths[:20]))

    checkpoint = "\n".join(lines)
    return checkpoint[:max_chars]


DEFAULT_RUNBOOK_MIN_REPEATS = 3
"""WS-28 T3: a command must appear at least this many times, byte-identical,
in the scanned transcript window before it is captured as a runbook fact.
Below this, a one-off command never becomes memory -- capture requires a
demonstrated, stable pattern, not a single observation."""


def repeated_commands(
    turns: tuple[TranscriptTurn, ...],
    *,
    min_repeats: int = DEFAULT_RUNBOOK_MIN_REPEATS,
) -> tuple[str, ...]:
    """WS-28 T3: verbatim commands invoked at least *min_repeats* times.

    Deterministic EXACT-STRING shape matching -- two commands differing by
    even one character (a flag, a path segment) are different shapes,
    counted and later captured separately.  This is intentional: a runbook
    line is an identifier, not a paraphrase target, so two near-identical
    commands must never merge into one captured fact.  No semantic
    selection, no LLM, no normalization beyond the verbatim strip already
    applied at parse time.

    Returns each qualifying command exactly once, in first-seen order, so
    downstream capture is deterministic across repeated calls over the same
    transcript window.
    """
    if min_repeats < 1:
        raise ValueError("min_repeats must be at least 1")
    counts: dict[str, int] = {}
    order: list[str] = []
    for turn in turns:
        for command in turn.commands:
            if command not in counts:
                order.append(command)
            counts[command] = counts.get(command, 0) + 1
    return tuple(command for command in order if counts[command] >= min_repeats)
