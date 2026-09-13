"""WS-3: Built-in Memory Bank persona presets.

Four curated persona presets ship as the starter catalog (§4 of NEXT.md):
  - Customer Support
  - Software Engineering
  - General Assistant
  - Product Manager

Each preset is a small set of Motives wired to type-level salience weights.
Import with ``from memotron.memory_bank import builtin_memory_bank``.

All Motives carry deterministic, hermetic salience rubrics (no LLM, no network).
The allowed_memory_types filter is THE key deterministic lever for offline proof
tests: the RuleBasedExtractionTransport ignores prompts, so the observable
difference between two Motives offline comes from the type-allowlist + rubric +
dedup_threshold, not the prompt text.
"""

from __future__ import annotations

from memotron.config import MemoryBank, Motive, SalienceRubric
from memotron.models import MemoryType

# ---------------------------------------------------------------------------
# Helper: build a SalienceRubric that favours the given types
# ---------------------------------------------------------------------------


def _rubric_favoring(*types: MemoryType, min_salience: float = 0.0) -> SalienceRubric:
    """Return a SalienceRubric with elevated importance weights for the listed types."""
    base_weights: dict[str, float] = {
        MemoryType.ANCHOR.value: 0.4,
        MemoryType.PREFERENCE.value: 0.4,
        MemoryType.REQUIREMENT.value: 0.4,
        MemoryType.DIRECTIVE.value: 0.4,
        MemoryType.STATE.value: 0.4,
        MemoryType.DECISION.value: 0.4,
        MemoryType.INCIDENT.value: 0.4,
    }
    for t in types:
        base_weights[t.value] = 0.9
    return SalienceRubric(
        min_salience=min_salience,
        importance_weights=base_weights,
    )


# ---------------------------------------------------------------------------
# Customer Support persona Motives
# ---------------------------------------------------------------------------

_SUPPORT_BUILD_CUSTOMER_PROFILE = Motive(
    name="build-customer-profile",
    goal="Build a persistent model of who the customer is",
    allowed_memory_types=(MemoryType.ANCHOR,),
    dedup_threshold=0.82,
    salience_rubric=_rubric_favoring(MemoryType.ANCHOR),
    retrieval_budget_share=0.30,
)

_SUPPORT_LEARN_COMPLIANCE_REQUIREMENTS = Motive(
    name="learn-compliance-requirements",
    goal="Surface and retain strict compliance and policy requirements",
    allowed_memory_types=(MemoryType.REQUIREMENT,),
    dedup_threshold=0.92,
    salience_rubric=_rubric_favoring(MemoryType.REQUIREMENT),
    retrieval_budget_share=0.40,
)

_SUPPORT_CAPTURE_PREFERENCES = Motive(
    name="capture-preferences",
    goal="Capture customer preferences to personalize future interactions",
    allowed_memory_types=(MemoryType.PREFERENCE,),
    dedup_threshold=0.85,
    salience_rubric=_rubric_favoring(MemoryType.PREFERENCE),
    retrieval_budget_share=0.20,
)

_SUPPORT_DISTILL_AGENT_LESSONS = Motive(
    name="distill-agent-lessons",
    goal="Extract reusable agent directives from resolved support interactions",
    allowed_memory_types=(MemoryType.DIRECTIVE,),
    dedup_threshold=0.90,
    salience_rubric=_rubric_favoring(MemoryType.DIRECTIVE),
    retrieval_budget_share=0.10,
)


# ---------------------------------------------------------------------------
# Software Engineering persona Motives
# ---------------------------------------------------------------------------

_ENG_LEARN_CODE_CONVENTIONS = Motive(
    name="learn-code-conventions",
    goal="Capture coding standards and conventions the agent should follow",
    allowed_memory_types=(MemoryType.DIRECTIVE,),
    dedup_threshold=0.88,
    salience_rubric=_rubric_favoring(MemoryType.DIRECTIVE),
    retrieval_budget_share=0.25,
)

_ENG_CAPTURE_ARCHITECTURE_DECISIONS = Motive(
    name="capture-architecture-decisions",
    goal="Record significant architectural decisions and rationale",
    allowed_memory_types=(MemoryType.DECISION,),
    dedup_threshold=0.85,
    salience_rubric=_rubric_favoring(MemoryType.DECISION),
    retrieval_budget_share=0.30,
)

_ENG_RECORD_INCIDENTS_POSTMORTEMS = Motive(
    name="record-incidents-postmortems",
    goal="Track incidents, root causes, and lessons learned",
    allowed_memory_types=(MemoryType.INCIDENT,),
    dedup_threshold=0.80,
    salience_rubric=_rubric_favoring(MemoryType.INCIDENT),
    retrieval_budget_share=0.25,
)

_ENG_DISTILL_AGENT_LESSONS = Motive(
    name="distill-agent-lessons",
    goal="Extract reusable directives from engineering interactions",
    allowed_memory_types=(MemoryType.DIRECTIVE,),
    dedup_threshold=0.90,
    salience_rubric=_rubric_favoring(MemoryType.DIRECTIVE),
    retrieval_budget_share=0.20,
)


# ---------------------------------------------------------------------------
# General Assistant persona Motives
# ---------------------------------------------------------------------------

_ASSISTANT_BUILD_USER_PROFILE = Motive(
    name="build-user-profile",
    goal="Build a persistent model of who the user is",
    allowed_memory_types=(MemoryType.ANCHOR,),
    dedup_threshold=0.82,
    salience_rubric=_rubric_favoring(MemoryType.ANCHOR),
    retrieval_budget_share=0.35,
)

_ASSISTANT_CAPTURE_PREFERENCES = Motive(
    name="capture-preferences",
    goal="Capture user preferences to personalize assistance",
    allowed_memory_types=(MemoryType.PREFERENCE,),
    dedup_threshold=0.85,
    salience_rubric=_rubric_favoring(MemoryType.PREFERENCE),
    retrieval_budget_share=0.35,
)

_ASSISTANT_TRACK_WORKING_STATE = Motive(
    name="track-working-state",
    goal="Track current working state, active tasks, and context",
    allowed_memory_types=(MemoryType.STATE,),
    dedup_threshold=0.75,
    salience_rubric=_rubric_favoring(MemoryType.STATE),
    retrieval_budget_share=0.30,
)


# ---------------------------------------------------------------------------
# Product Manager persona Motives
# ---------------------------------------------------------------------------

_PM_SYNTHESIZE_USER_FEEDBACK = Motive(
    name="synthesize-user-feedback",
    goal="Synthesize user feedback into persistent preference signals",
    allowed_memory_types=(MemoryType.PREFERENCE,),
    dedup_threshold=0.85,
    salience_rubric=_rubric_favoring(MemoryType.PREFERENCE),
    retrieval_budget_share=0.25,
)

_PM_RECORD_DECISIONS = Motive(
    name="record-decisions",
    goal="Record product decisions and their rationale",
    allowed_memory_types=(MemoryType.DECISION,),
    dedup_threshold=0.85,
    salience_rubric=_rubric_favoring(MemoryType.DECISION),
    retrieval_budget_share=0.30,
)

_PM_TRACK_ROADMAP_STATE = Motive(
    name="track-roadmap-state",
    goal="Track roadmap items, priorities, and blockers",
    allowed_memory_types=(MemoryType.STATE,),
    dedup_threshold=0.78,
    salience_rubric=_rubric_favoring(MemoryType.STATE),
    retrieval_budget_share=0.25,
)

_PM_CAPTURE_MEETING_NOTES = Motive(
    name="capture-meeting-notes",
    goal="Distill meeting content into actionable memory",
    allowed_memory_types=(MemoryType.PREFERENCE, MemoryType.DECISION, MemoryType.STATE),
    dedup_threshold=0.80,
    salience_rubric=_rubric_favoring(MemoryType.PREFERENCE, MemoryType.DECISION, MemoryType.STATE),
    retrieval_budget_share=0.20,
)


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------


def builtin_memory_bank() -> MemoryBank:
    """Return the built-in Memory Bank containing all four §4 persona presets.

    Note: some Motive names are shared across personas (e.g. ``distill-agent-lessons``
    appears in both Support and Engineering).  Because a MemoryBank requires unique
    names, each persona ships its own bank.  This factory returns a single combined
    bank whose names are unique (the Engineering variant of ``distill-agent-lessons``
    is suffixed ``-eng`` to avoid collision with the Support variant).

    Use ``persona_memory_bank(persona)`` instead when you need a clean single-persona
    bank.  The combined bank is convenient for quick demos and tests that exercise
    multiple personas against a shared config.
    """
    return MemoryBank(
        motives=(
            # Support persona
            _SUPPORT_BUILD_CUSTOMER_PROFILE,
            _SUPPORT_LEARN_COMPLIANCE_REQUIREMENTS,
            _SUPPORT_CAPTURE_PREFERENCES,
            _SUPPORT_DISTILL_AGENT_LESSONS,
            # Engineering persona (distill-agent-lessons suffixed to avoid name collision)
            _ENG_LEARN_CODE_CONVENTIONS,
            _ENG_CAPTURE_ARCHITECTURE_DECISIONS,
            _ENG_RECORD_INCIDENTS_POSTMORTEMS,
            Motive(
                name="distill-agent-lessons-eng",
                goal="Extract reusable directives from engineering interactions",
                allowed_memory_types=(MemoryType.DIRECTIVE,),
                dedup_threshold=0.90,
                salience_rubric=_rubric_favoring(MemoryType.DIRECTIVE),
                retrieval_budget_share=0.20,
            ),
            # General Assistant persona (capture-preferences and build-user-profile
            # are already present under different names; track-working-state is new)
            _ASSISTANT_BUILD_USER_PROFILE,
            # capture-preferences already present under that name (Support persona)
            Motive(
                name="capture-user-preferences",
                goal="Capture user preferences to personalize assistance",
                allowed_memory_types=(MemoryType.PREFERENCE,),
                dedup_threshold=0.85,
                salience_rubric=_rubric_favoring(MemoryType.PREFERENCE),
                retrieval_budget_share=0.35,
            ),
            _ASSISTANT_TRACK_WORKING_STATE,
            # Product Manager persona
            _PM_SYNTHESIZE_USER_FEEDBACK,
            _PM_RECORD_DECISIONS,
            _PM_TRACK_ROADMAP_STATE,
            _PM_CAPTURE_MEETING_NOTES,
        )
    )


def support_memory_bank() -> MemoryBank:
    """Return a MemoryBank with only the Customer Support persona Motives."""
    return MemoryBank(
        motives=(
            _SUPPORT_BUILD_CUSTOMER_PROFILE,
            _SUPPORT_LEARN_COMPLIANCE_REQUIREMENTS,
            _SUPPORT_CAPTURE_PREFERENCES,
            _SUPPORT_DISTILL_AGENT_LESSONS,
        )
    )


def engineering_memory_bank() -> MemoryBank:
    """Return a MemoryBank with only the Engineering persona Motives."""
    return MemoryBank(
        motives=(
            _ENG_LEARN_CODE_CONVENTIONS,
            _ENG_CAPTURE_ARCHITECTURE_DECISIONS,
            _ENG_RECORD_INCIDENTS_POSTMORTEMS,
            _ENG_DISTILL_AGENT_LESSONS,
        )
    )


def assistant_memory_bank() -> MemoryBank:
    """Return a MemoryBank with only the General Assistant persona Motives."""
    return MemoryBank(
        motives=(
            _ASSISTANT_BUILD_USER_PROFILE,
            _ASSISTANT_CAPTURE_PREFERENCES,
            _ASSISTANT_TRACK_WORKING_STATE,
        )
    )


def pm_memory_bank() -> MemoryBank:
    """Return a MemoryBank with only the Product Manager persona Motives."""
    return MemoryBank(
        motives=(
            _PM_SYNTHESIZE_USER_FEEDBACK,
            _PM_RECORD_DECISIONS,
            _PM_TRACK_ROADMAP_STATE,
            _PM_CAPTURE_MEETING_NOTES,
        )
    )
