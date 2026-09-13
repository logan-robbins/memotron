"""Built-in prompt profiles.

The lazy import of memotron.prompts inside default_prompt_profiles is load-
bearing and stays function-local: memotron.prompts imports config back, and
hoisting it would close the cycle at import time. The comment at that line said so
before this split and still does."""

from __future__ import annotations

from memotron.config._instructions import (
    DreamPromptProfile,
)


def _base_prompt_profiles() -> tuple[DreamPromptProfile, ...]:
    return (
        DreamPromptProfile(
            name="support-memory",
            version="v1",
            goal="Extract durable, support-useful memories that improve future customer, user, or agent behavior.",
            include=(
                "Stable user preferences that should affect future answers.",
                "Customer requirements, constraints, approvals, systems, and support-critical facts.",
                "Reusable agent lessons that should change future operating behavior.",
            ),
            exclude=(
                "One-off wording, transient emotions, and small talk without future operational value.",
                "Graph context by itself is not evidence; do not copy it without independent support in this episode.",
            ),
            rules=(
                "Prefer facts that can be attached to a scoped subject and relationship type.",
                "Preserve explicit valid_from and valid_to windows when the source gives them.",
                "Use configured node properties only when they help later retrieval or graph inspection.",
            ),
        ),
        DreamPromptProfile(
            name="support-memory",
            version="v2",
            goal="Extract support memories with stronger emphasis on contradictions, supersession, and entity properties.",
            include=(
                "Durable preferences, requirements, obligations, and reusable agent lessons.",
                "Corrections that should supersede older active truth in the same scoped graph.",
                "Entity properties such as customer tier, region, system, role, or kind when stated.",
            ),
            exclude=(
                "Unverified speculation, temporary phrasing, and graph-context facts not independently corroborated by this episode.",
                "Personally sensitive details unless they are necessary for the configured support memory type.",
            ),
            rules=(
                "State each memory as an atomic graph relationship.",
                "Emit independently corroborated active facts so the graph can receipt a reinforcement rather than suppressing the observation.",
                "When a fact is temporary, include valid_to instead of letting pruning infer expiration.",
                "When a fact replaces another, keep the same subject and predicate so single-active repair can supersede it.",
            ),
        ),
        DreamPromptProfile(
            name="preference-memory",
            version="v1",
            goal="Extract durable preferences only.",
            include=(
                "Repeated or explicit preferences about answer style, format, workflow, or product behavior.",
                "Stable dislikes or constraints that should guide future interactions.",
            ),
            exclude=(
                "A preference implied by one isolated utterance.",
                "Temporary mood, current task context, or short-lived channel selection.",
            ),
            rules=(
                "Use PREFERS unless the instruction set defines a more specific allowed relationship.",
                "Do not turn requirements or obligations into preferences.",
            ),
        ),
        DreamPromptProfile(
            name="agent-lessons",
            version="v1",
            goal="Extract reusable agent operating lessons from reflections, outcomes, and support workflows.",
            include=(
                "Steps the agent should take in future similar cases.",
                "Known escalation prerequisites, verification checks, and source-selection lessons.",
            ),
            exclude=(
                "Case-specific status updates that do not generalize.",
                "Lessons unsupported by the episode or existing graph context.",
            ),
            rules=(
                "Use SHOULD for durable agent behavior lessons.",
                "Prefer concise object text that can be inserted into a future agent profile.",
            ),
        ),
    )


def default_prompt_profiles() -> tuple[DreamPromptProfile, ...]:
    from memotron.prompts import builtin_profiles  # lazy import avoids circular dependency

    return (*_base_prompt_profiles(), *builtin_profiles())
