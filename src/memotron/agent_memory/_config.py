"""Turning an agent identity into a DreamConfig the engine will accept.

`agent_memory_config` is why this package stays decoupled from storage: it BUILDS a
config and hands it to Memotron rather than reaching into the graph. Measured
consequence -- agent_memory touches ZERO storage call sites where client.py touches 279.

The five motive constants travel with it. `default_motive_for_agent_id` maps an agent
id onto one of them, so an agent naming no motive still gets one, and which one is a
product decision rather than a fallback."""

from __future__ import annotations

from memotron.config import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    DreamJobKind,
    GrowthPolicy,
    MemoryBank,
    Motive,
    NodeInstruction,
    PruningPolicy,
    RelationshipCardinality,
    RelationshipInstruction,
    RetentionPolicy,
    SalienceRubric,
    storage_settings_from_env,
)
from memotron.identity import normalize_agent_id
from memotron.memory_bank import builtin_memory_bank
from memotron.models import (
    MemoryType,
)

DEFAULT_AGENT_MEMORY_MOTIVE = "agent-memory"


ENGINEERING_AGENT_MEMORY_MOTIVE = "engineering-agent-memory"


PRODUCT_AGENT_MEMORY_MOTIVE = "product-agent-memory"


PROJECT_AGENT_MEMORY_MOTIVE = "project-agent-memory"


GENERAL_AGENT_MEMORY_MOTIVE = "general-agent-memory"


def agent_memory_config() -> DreamConfig:
    """Return the default config for software-agent memory projects."""

    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Durable people, agents, teams, repositories, systems, and concepts.",
                properties=("kind", "role", "tier", "region", "system"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Hard requirements, constraints, approval gates, and blockers.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Stable preferences that should shape future agent behavior.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Reusable operating lessons, conventions, and directives for agents.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="IS",
                source_label="Entity",
                target_label="Entity",
                query="Identity facts: role, ownership, affiliation, and stable status.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.ANCHOR,
            ),
            RelationshipInstruction(
                type="DECIDES",
                source_label="Entity",
                target_label="Entity",
                query="Architectural, product, and operational decisions.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.DECISION,
            ),
            RelationshipInstruction(
                type="HAS_STATE",
                source_label="Entity",
                target_label="Entity",
                query="Current working state, active focus, temporary blockers, and handoff state.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.STATE,
            ),
            RelationshipInstruction(
                type="EXPERIENCED",
                source_label="Entity",
                target_label="Entity",
                query="Incidents, failures, postmortem facts, and recovery lessons.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.INCIDENT,
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        memory_bank=agent_memory_bank(),
        storage=storage_settings_from_env(),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                motive=DEFAULT_AGENT_MEMORY_MOTIVE,
            ),
            DreamJob(
                name="consolidation-default",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=300,
                rollup_consolidation=True,
            ),
            DreamJob(
                name="pruning-default",
                kind=DreamJobKind.PRUNING,
                cadence_seconds=600,
            ),
        ),
        pruning=PruningPolicy(
            superseded_retention_seconds=30 * 24 * 3600,
            relationship_active_max_age_seconds={"HAS_STATE": 30 * 24 * 3600},
            growth=GrowthPolicy(
                soft_cap=300,
                per_type_soft_caps={
                    MemoryType.STATE.value: 80,
                    MemoryType.PREFERENCE.value: 80,
                    MemoryType.DECISION.value: 140,
                    MemoryType.DIRECTIVE.value: 100,
                },
            ),
            retention=RetentionPolicy(
                protected_memory_types=(
                    MemoryType.ANCHOR,
                    MemoryType.REQUIREMENT,
                )
            ),
        ),
    )


def agent_memory_bank() -> MemoryBank:
    """Return the built-in bank plus an all-purpose agent-memory Motive."""

    base = builtin_memory_bank()
    all_types = tuple(MemoryType)

    def role_motive(
        *,
        name: str,
        goal: str,
        favored: tuple[MemoryType, ...],
        dedup_threshold: float,
    ) -> Motive:
        weights = {memory_type.value: 0.6 for memory_type in MemoryType}
        for memory_type in favored:
            weights[memory_type.value] = 0.9
        return Motive(
            name=name,
            goal=goal,
            allowed_memory_types=all_types,
            salience_rubric=SalienceRubric(importance_weights=weights),
            dedup_threshold=dedup_threshold,
            retrieval_budget_share=1.0,
        )

    return MemoryBank(
        motives=(
            *base.motives,
            role_motive(
                name=DEFAULT_AGENT_MEMORY_MOTIVE,
                goal="Curate durable shared memory for autonomous agents and their harnesses.",
                favored=(
                    MemoryType.REQUIREMENT,
                    MemoryType.DIRECTIVE,
                    MemoryType.DECISION,
                    MemoryType.INCIDENT,
                ),
                dedup_threshold=0.86,
            ),
            role_motive(
                name=ENGINEERING_AGENT_MEMORY_MOTIVE,
                goal="Preserve engineering requirements, architecture decisions, incidents, and reusable operating lessons.",
                favored=(
                    MemoryType.REQUIREMENT,
                    MemoryType.DIRECTIVE,
                    MemoryType.DECISION,
                    MemoryType.INCIDENT,
                ),
                dedup_threshold=0.88,
            ),
            role_motive(
                name=PRODUCT_AGENT_MEMORY_MOTIVE,
                goal="Preserve user needs, product decisions, roadmap state, and consequential requirements.",
                favored=(
                    MemoryType.PREFERENCE,
                    MemoryType.REQUIREMENT,
                    MemoryType.STATE,
                    MemoryType.DECISION,
                ),
                dedup_threshold=0.87,
            ),
            role_motive(
                name=PROJECT_AGENT_MEMORY_MOTIVE,
                goal="Preserve delivery commitments, blockers, decisions, incidents, and coordination rules.",
                favored=(
                    MemoryType.REQUIREMENT,
                    MemoryType.DIRECTIVE,
                    MemoryType.STATE,
                    MemoryType.DECISION,
                    MemoryType.INCIDENT,
                ),
                dedup_threshold=0.87,
            ),
            role_motive(
                name=GENERAL_AGENT_MEMORY_MOTIVE,
                goal="Preserve durable identity, preferences, requirements, state, decisions, and lessons.",
                favored=(
                    MemoryType.ANCHOR,
                    MemoryType.PREFERENCE,
                    MemoryType.REQUIREMENT,
                    MemoryType.STATE,
                ),
                dedup_threshold=0.86,
            ),
        )
    )


def default_motive_for_agent_id(agent_id: str) -> str:
    normalized = normalize_agent_id(agent_id).casefold()
    if normalized.endswith("-product-manager") or "product-manager" in normalized:
        return PRODUCT_AGENT_MEMORY_MOTIVE
    if normalized.endswith("-project-manager") or "project-manager" in normalized:
        return PROJECT_AGENT_MEMORY_MOTIVE
    if normalized in {"operator", "pod", "viewer"}:
        return GENERAL_AGENT_MEMORY_MOTIVE
    return ENGINEERING_AGENT_MEMORY_MOTIVE
