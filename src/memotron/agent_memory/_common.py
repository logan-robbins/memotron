"""The two normalisers everything else in the package depends on.

Extracted first, and that ordering is the point: `ProjectMemorySettings.normalize_goals`
in _results.py calls `_normalize_non_blank`, so pulling the value types out before
their helper leaves a dangling global that `pure_move` catches and nothing else does.
Dependencies before dependents."""

from __future__ import annotations

from memotron.identity import (
    agent_id_key,
    normalize_agent_id,
)


def _normalize_non_blank(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be blank")
    return normalized


def _normalize_agent_ids(agent_ids: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    normalized = tuple(normalize_agent_id(agent_id) for agent_id in agent_ids)
    if len(normalized) != len({agent_id_key(agent_id) for agent_id in normalized}):
        raise ValueError("agent_id values must be unique")
    return normalized
