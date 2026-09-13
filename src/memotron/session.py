"""Session ingestion — groups conversation turns into episodic windows.

Clients should not need to know what an episode is. They have a conversation.
SessionIngester accepts that conversation as a stream of turns and handles
chunking, reference-time assignment, and episode formatting automatically.

Usage — batch (full transcript after the session):

    result = await client.add_session(
        name="support-chat-20260604",
        turns=[
            ConversationTurn(role="user",      content="We need ISO27001 first.", timestamp=...),
            ConversationTurn(role="assistant", content="Noted — logging that.",   timestamp=...),
        ],
        scope=customer_scope,
    )

Usage — streaming (turns arrive in real time):

    async with client.open_session(name="chat-123", scope=user_scope) as session:
        await session.add_turn("user",      "We need ISO27001 first.")
        await session.add_turn("assistant", "Noted — logging that.")
    # session.close() is called automatically on context-manager exit
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self
from uuid import uuid4

from memotron.models import AddSessionResult, ConversationTurn, Episode, EpisodeType, MemoryScope

if TYPE_CHECKING:
    from memotron.client import Memotron


class SessionIngester:
    """Wraps a conversation session and groups turns into episodic windows.

    Windows are flushed automatically when any of these thresholds is reached:
    - turns_per_episode turns have accumulated
    - the buffer exceeds max_chars_per_episode characters
    - the time gap between the first and latest turn in the buffer exceeds
      time_gap_seconds (only when turns carry timestamps)
    """

    def __init__(
        self,
        *,
        client: Memotron,
        name: str,
        scope: MemoryScope,
        session_id: str | None = None,
        turns_per_episode: int = 8,
        max_chars_per_episode: int = 4000,
        time_gap_seconds: int | None = 300,
        source_description: str = "conversation session",
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
    ) -> None:
        if not name.strip():
            raise ValueError("name cannot be blank")
        if turns_per_episode <= 0:
            raise ValueError("turns_per_episode must be greater than zero")
        if max_chars_per_episode <= 0:
            raise ValueError("max_chars_per_episode must be greater than zero")
        if time_gap_seconds is not None and time_gap_seconds <= 0:
            raise ValueError("time_gap_seconds must be greater than zero if set")
        client.config.instruction_set(instruction_set)

        self._client = client
        self._name = name.strip()
        self._scope = scope
        self._session_id = session_id.strip() if session_id and session_id.strip() else str(uuid4())
        self._turns_per_episode = turns_per_episode
        self._max_chars_per_episode = max_chars_per_episode
        self._time_gap_seconds = time_gap_seconds
        self._source_description = source_description
        self._base_metadata = dict(metadata or {})
        self._instruction_set = instruction_set

        self._buffer: list[ConversationTurn] = []
        self._episode_uuids: list[str] = []
        self._window_index = 0
        self._total_turns = 0
        self._closed = False

    @property
    def session_id(self) -> str:
        return self._session_id

    async def add_turn(
        self,
        role: str,
        content: str,
        *,
        timestamp: datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Add one conversation turn. Flushes the window automatically when full."""
        if self._closed:
            raise ValueError("session is already closed")
        turn = ConversationTurn(
            role=role,
            content=content,
            timestamp=timestamp,
            metadata=metadata or {},
        )
        # Time-gap: flush current buffer BEFORE adding the new turn so the
        # arriving turn starts the next window, not the one it interrupted.
        if self._time_gap_triggered(turn):
            await self.flush()
        self._buffer.append(turn)
        self._total_turns += 1
        # Capacity: flush AFTER adding so the triggering turn is included.
        if self._capacity_exceeded():
            await self.flush()

    async def flush(self) -> list[str]:
        """Emit the current turn buffer as one episode. Returns the episode UUID in a list."""
        if not self._buffer:
            return []
        episode = self._build_episode(self._buffer, self._window_index)
        # WS-12: route through the client's episode choke point so crypto-shred
        # scopes store conversation windows sealed at ingest.
        self._client._store_episode(episode)
        self._episode_uuids.append(episode.uuid)
        self._buffer = []
        self._window_index += 1
        return [episode.uuid]

    async def close(self) -> AddSessionResult:
        """Flush any remaining turns and finalize the session."""
        if self._closed:
            raise ValueError("session is already closed")
        await self.flush()
        self._closed = True
        return AddSessionResult(
            session_id=self._session_id,
            scope_keys=[self._scope.key],
            turns_ingested=self._total_turns,
            windows_created=self._window_index,
            episodes_created=len(self._episode_uuids),
            episode_uuids=list(self._episode_uuids),
            queued_for_dreaming=True,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if not self._closed:
            await self.close()

    def _time_gap_triggered(self, incoming: ConversationTurn) -> bool:
        """True when the incoming turn's timestamp is >= time_gap_seconds after the window start."""
        if self._time_gap_seconds is None or not self._buffer:
            return False
        first_ts = self._buffer[0].timestamp
        incoming_ts = incoming.timestamp
        if first_ts is None or incoming_ts is None:
            return False
        return (incoming_ts - first_ts).total_seconds() >= self._time_gap_seconds

    def _capacity_exceeded(self) -> bool:
        """True when the buffer is at or over the turn-count or char-count limit."""
        if len(self._buffer) >= self._turns_per_episode:
            return True
        buffer_chars = sum(len(t.role) + len(t.content) + 4 for t in self._buffer)
        return buffer_chars >= self._max_chars_per_episode

    def _build_episode(self, turns: list[ConversationTurn], window_index: int) -> Episode:
        return Episode(
            name=f"{self._name} [window {window_index + 1}]",
            body=_format_transcript(turns),
            source=EpisodeType.MESSAGE,
            source_description=self._source_description,
            scope=self._scope,
            reference_time=_turns_reference_time(turns),
            metadata={
                **self._base_metadata,
                "session_id": self._session_id,
                "session_name": self._name,
                "session_window_index": window_index,
                "session_scope_key": self._scope.key,
                "session_turn_count": len(turns),
            },
            instruction_set=self._instruction_set,
        )


def _format_transcript(turns: list[ConversationTurn]) -> str:
    lines: list[str] = []
    for turn in turns:
        prefix = f"[{turn.timestamp.isoformat()}] {turn.role}" if turn.timestamp is not None else turn.role
        lines.append(f"{prefix}: {turn.content}")
    return "\n".join(lines)


def _turns_reference_time(turns: list[ConversationTurn]) -> datetime:
    for turn in reversed(turns):
        if turn.timestamp is not None:
            return turn.timestamp
    return datetime.now(UTC)
