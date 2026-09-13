"""Turning memory into the prose an agent actually reads.

Two methods, and they are the last mile of the product: everything else in this
package decides WHAT an agent should know, and these decide how it arrives in the
context window. `_render_start_context` is what a session opens with;
`_render_log_body` is what a write looks like read back.

A leaf -- nothing here calls another concern."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import AgentMemoryMode
from memotron.models import (
    MemoryProfile,
)

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryRenderMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    @staticmethod
    def _render_start_context(
        *,
        project_profile: MemoryProfile,
        agent_profile: MemoryProfile,
        user_profile: MemoryProfile | None,
        mode: AgentMemoryMode,
    ) -> str:
        if mode == AgentMemoryMode.SIMPLE:
            assert user_profile is not None
            sections = [
                f"Memotron project memory ({project_profile.scope.key})",
                project_profile.rendered_context,
                f"Memotron personal memory ({user_profile.scope.key})",
                user_profile.rendered_context,
                "Memotron session continuity",
                agent_profile.rendered_context,
            ]
        else:
            sections = [
                f"Memotron project memory ({project_profile.scope.key})",
                project_profile.rendered_context,
                f"Memotron agent memory ({agent_profile.scope.key})",
                agent_profile.rendered_context,
            ]
        return "\n\n".join(sections)

    @staticmethod
    def _render_log_body(
        *,
        agent_id: str,
        summary: str,
        decisions: tuple[str, ...],
        incidents: tuple[str, ...],
        blockers: tuple[str, ...],
    ) -> str:
        cleaned_summary = summary.strip()
        cleaned_decisions = tuple(item.strip() for item in decisions if item.strip())
        cleaned_incidents = tuple(item.strip() for item in incidents if item.strip())
        cleaned_blockers = tuple(item.strip() for item in blockers if item.strip())
        if not any((cleaned_summary, cleaned_decisions, cleaned_incidents, cleaned_blockers)):
            raise ValueError("memory_log requires summary, decisions, incidents, or blockers")
        memories: list[dict[str, Any]] = []
        if cleaned_summary:
            memories.append(
                {
                    "subject": agent_id,
                    "predicate": "has state",
                    "object": cleaned_summary,
                    "relationship_type": "HAS_STATE",
                    "confidence": 0.85,
                    "source_text": cleaned_summary,
                    "claim_mode": "report_of_behavior",
                }
            )
        memories.extend(
            {
                "subject": agent_id,
                "predicate": "decided",
                "object": decision,
                "relationship_type": "DECIDES",
                "confidence": 0.9,
                "source_text": decision,
                "claim_mode": "descriptive_assertion",
            }
            for decision in cleaned_decisions
        )
        memories.extend(
            {
                "subject": agent_id,
                "predicate": "experienced",
                "object": incident,
                "relationship_type": "EXPERIENCED",
                "confidence": 0.9,
                "source_text": incident,
                "claim_mode": "report_of_behavior",
            }
            for incident in cleaned_incidents
        )
        memories.extend(
            {
                "subject": agent_id,
                "predicate": "has state",
                "object": f"blocked by {blocker}",
                "relationship_type": "HAS_STATE",
                "confidence": 0.8,
                "source_text": blocker,
                "claim_mode": "descriptive_assertion",
            }
            for blocker in cleaned_blockers
        )
        payload = {
            "agent_id": agent_id,
            "summary": cleaned_summary,
            "decisions": list(cleaned_decisions),
            "incidents": list(cleaned_incidents),
            "blockers": list(cleaned_blockers),
            "memories": memories,
        }
        return json.dumps(payload, sort_keys=True)
