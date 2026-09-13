"""Turning query-string text into typed values, and refusing what will not convert.

Seven small functions and the only thing between an HTTP request and the rest of the
admin server. Deliberately strict: `parse_bool` and `parse_datetime` raise
HttpApiError rather than defaulting, because a silently-defaulted filter returns a
plausible-looking wrong answer over someone else memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memotron import (
    MemoryScope,
)
from memotron.admin_server._errors import HttpApiError
from memotron.models import RelationshipStatus


def parse_scope_key(value: str) -> MemoryScope:
    """Delegates to `MemoryScope.from_key`, which is where this logic now lives.

    Kept as a name because nine call sites and the admin server's re-export use it. The
    body moved down to the model so the same parse is reachable from outside the admin
    HTTP surface -- the per-key registry stores `default_scope_key` as text and the
    principal adapter has to turn it back into a scope. Error messages are unchanged.
    """
    return MemoryScope.from_key(value)


def parse_datetime(value: str | None) -> datetime | None:
    if value is None or not value.strip():
        return None
    raw = value.strip()
    normalized = raw.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if "T" not in raw and " " not in raw:
        return parsed + timedelta(days=1, microseconds=-1)
    return parsed


def parse_statuses(value: str | None) -> set[RelationshipStatus] | None:
    if value is None or not value.strip():
        return None
    statuses: set[RelationshipStatus] = set()
    for raw_status in value.split(","):
        normalized = raw_status.strip()
        if normalized:
            statuses.add(RelationshipStatus(normalized))
    return statuses or None


def parse_csv(value: str | None) -> set[str] | None:
    if value is None or not value.strip():
        return None
    values = {part.strip() for part in value.split(",") if part.strip()}
    return values or None


def parse_bool(value: str | None, *, default: bool) -> bool:
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"invalid boolean value: {value}")


def parse_lines(value: Any, label: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return tuple(item.strip() for item in value if item.strip())
    raise HttpApiError(400, f"{label} must be a string or array of strings")


def first(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key, [])
    return values[0] if values else None
