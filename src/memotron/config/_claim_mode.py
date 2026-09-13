"""Claim mode -- the typed semantic intent of a memory.

Maps a relationship type to its memory type and a claim mode to its directive
stance, so a REQUIRES edge and a 'should' statement are reconciled on the same
terms rather than by string matching at each call site."""

from __future__ import annotations

from memotron.models import (
    ClaimMode,
    DirectiveStance,
    MemoryType,
)

# Canonical mapping from relationship type → default MemoryType.
# RelationshipInstruction uses this to resolve memory_type when not set explicitly.
# If a custom relationship type is not in this map, the field must be set explicitly
# or config validation will raise a clear ValueError (fail-fast).
RELATIONSHIP_TYPE_MEMORY_TYPE_MAP: dict[str, MemoryType] = {
    "REQUIRES": MemoryType.REQUIREMENT,
    "PREFERS": MemoryType.PREFERENCE,
    "SHOULD": MemoryType.DIRECTIVE,
    "ROLLUP": MemoryType.ROLLUP,
}


_CLAIM_MODE_MEMORY_TYPES: dict[ClaimMode, frozenset[MemoryType]] = {
    ClaimMode.DESCRIPTIVE_ASSERTION: frozenset(
        {MemoryType.ANCHOR, MemoryType.STATE, MemoryType.DECISION, MemoryType.INCIDENT, MemoryType.ROLLUP}
    ),
    ClaimMode.REQUIREMENT: frozenset({MemoryType.REQUIREMENT}),
    ClaimMode.PREFERENCE: frozenset({MemoryType.PREFERENCE}),
    ClaimMode.DIRECTIVE: frozenset({MemoryType.DIRECTIVE}),
    ClaimMode.CORRECTION: frozenset(
        {
            MemoryType.ANCHOR,
            MemoryType.PREFERENCE,
            MemoryType.REQUIREMENT,
            MemoryType.DIRECTIVE,
            MemoryType.STATE,
            MemoryType.DECISION,
            MemoryType.INCIDENT,
        }
    ),
    ClaimMode.REPORT_OF_BEHAVIOR: frozenset({MemoryType.STATE, MemoryType.INCIDENT}),
}


_CLAIM_MODE_STANCES: dict[ClaimMode, DirectiveStance] = {
    ClaimMode.DESCRIPTIVE_ASSERTION: DirectiveStance.ASSERT,
    ClaimMode.REQUIREMENT: DirectiveStance.REQUIRE,
    ClaimMode.PREFERENCE: DirectiveStance.PREFER,
    ClaimMode.DIRECTIVE: DirectiveStance.REQUIRE,
    ClaimMode.CORRECTION: DirectiveStance.ASSERT,
    ClaimMode.REPORT_OF_BEHAVIOR: DirectiveStance.ASSERT,
}


def default_claim_mode_for_memory_type(memory_type: MemoryType) -> ClaimMode:
    """Return the one deterministic default at legacy/public write boundaries."""
    return {
        MemoryType.REQUIREMENT: ClaimMode.REQUIREMENT,
        MemoryType.PREFERENCE: ClaimMode.PREFERENCE,
        MemoryType.DIRECTIVE: ClaimMode.DIRECTIVE,
    }.get(memory_type, ClaimMode.DESCRIPTIVE_ASSERTION)


def claim_mode_stance(claim_mode: ClaimMode) -> DirectiveStance:
    return _CLAIM_MODE_STANCES[ClaimMode(claim_mode)]


def validate_claim_mode_for_memory_type(*, claim_mode: ClaimMode, memory_type: MemoryType) -> None:
    allowed = _CLAIM_MODE_MEMORY_TYPES[ClaimMode(claim_mode)]
    if memory_type not in allowed:
        raise ValueError(
            f"claim_mode {claim_mode.value!r} cannot materialize memory_type {memory_type.value!r}; "
            f"allowed types are {sorted(item.value for item in allowed)}"
        )
