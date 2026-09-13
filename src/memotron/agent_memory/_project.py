"""Project-scope memory: the settings, and pushing them onto the client.

Four methods. `apply_project_memory_config_to_client` is 107 lines and is called
from __init__, so a failure here is an import-time failure of the whole platform
rather than a runtime one -- which is why it is a staticmethod taking an explicit
client rather than reaching through self."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._publish import PROJECT_MEMORY_POLICY_MOTIVE
from memotron.agent_memory._results import ProjectMemoryConfig, ProjectMemorySettings
from memotron.agent_memory._scopes import project_scope
from memotron.client import Memotron
from memotron.config import (
    DreamPromptOverride,
    MemoryBank,
    MemoryControlPlane,
    Motive,
    PromptPack,
    RetentionPolicy,
    SalienceRubric,
)
from memotron.models import (
    MemoryType,
)

PROJECT_MEMORY_PROMPT_PACK = "project-memory-active"
PROJECT_MEMORY_PROMPT_PROFILE = "support-memory"
PROJECT_MEMORY_PROMPT_PROFILE_VERSION = "v2"

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryProjectMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    project_scope: Any
    tenant_id: Any

    def configure_project_memory(
        self,
        *,
        project_goal: str,
        memory_goal: str,
        keep: list[str] | tuple[str, ...],
        exclude: list[str] | tuple[str, ...] = (),
        rules: list[str] | tuple[str, ...] = (),
        allowed_memory_types: list[MemoryType | str] | tuple[MemoryType | str, ...] = (
            MemoryType.REQUIREMENT,
            MemoryType.DIRECTIVE,
            MemoryType.STATE,
            MemoryType.DECISION,
            MemoryType.INCIDENT,
            MemoryType.ROLLUP,
        ),
        protected_memory_types: list[MemoryType | str] | tuple[MemoryType | str, ...] = (
            MemoryType.REQUIREMENT,
            MemoryType.DECISION,
            MemoryType.INCIDENT,
        ),
        min_salience: float = 0.0,
        max_memories_per_candidate: int = 12,
        dedup_threshold: float = 0.87,
        min_endorsements: int = 1,
        configured_by: str,
    ) -> ProjectMemoryConfig:
        settings = ProjectMemorySettings(
            project_goal=project_goal,
            memory_goal=memory_goal,
            keep=tuple(keep),
            exclude=tuple(exclude),
            rules=tuple(rules),
            allowed_memory_types=tuple(allowed_memory_types),
            protected_memory_types=tuple(protected_memory_types),
            min_salience=min_salience,
            max_memories_per_candidate=max_memories_per_candidate,
            dedup_threshold=dedup_threshold,
            min_endorsements=min_endorsements,
        )
        saved = self.client.graph.save_project_memory_config(
            tenant_id=self.tenant_id,
            config=settings.model_dump(mode="json"),
            configured_by=_normalize_non_blank(configured_by, "configured_by"),
        )
        self.apply_project_memory_config_to_client(
            client=self.client,
            tenant_id=self.tenant_id,
        )
        return ProjectMemoryConfig.model_validate(saved)

    def project_memory_config(self) -> ProjectMemoryConfig | None:
        active = self.client.graph.active_project_memory_config(self.tenant_id)
        return ProjectMemoryConfig.model_validate(active) if active is not None else None

    def project_memory_status(self) -> dict[str, Any]:
        config = self.project_memory_config()
        candidate_count, pending_candidate_count = self.client.graph.episode_event_counts(
            scope=self.project_scope,
            event="project_memory_candidate",
        )
        return {
            "tenant_id": self.tenant_id,
            "project_scope": self.project_scope.model_dump(mode="json"),
            "configured": config is not None,
            "config": (config.model_dump(mode="json") if config is not None else None),
            "versions": [
                ProjectMemoryConfig.model_validate(item).model_dump(mode="json")
                for item in self.client.graph.project_memory_config_versions(self.tenant_id)
            ],
            "candidate_count": candidate_count,
            "pending_candidate_count": pending_candidate_count,
        }

    @staticmethod
    def apply_project_memory_config_to_client(
        *,
        client: Memotron,
        tenant_id: str,
    ) -> None:
        control_plane = client.control_plane
        if control_plane is None:
            return
        active = client.graph.active_project_memory_config(tenant_id)
        if active is None:
            return
        config = ProjectMemoryConfig.model_validate(active)
        tenant = control_plane.tenant(tenant_id)
        memory_bank = tenant.memory_bank or control_plane.base_config.memory_bank
        if memory_bank is None:
            raise ValueError("project memory configuration requires a MemoryBank")
        prompt_override = DreamPromptOverride(
            goal=config.memory_goal,
            include=(
                f"Advance the overall project goal: {config.project_goal}",
                *config.keep,
            ),
            exclude=config.exclude,
            rules=(
                "Materialize only facts that are useful across project agents; "
                "keep personal or scratch context out of project scope.",
                "Preserve source attribution and temporal windows.",
                *config.rules,
            ),
        )
        project_motive = Motive(
            name=PROJECT_MEMORY_POLICY_MOTIVE,
            goal=(
                f"Advance project goal '{config.project_goal}' by curating shared "
                f"memory whose purpose is: {config.memory_goal}"
            ),
            allowed_memory_types=config.allowed_memory_types,
            prompt_profile=PROJECT_MEMORY_PROMPT_PROFILE,
            prompt_profile_version=PROJECT_MEMORY_PROMPT_PROFILE_VERSION,
            prompt_override=prompt_override,
            salience_rubric=SalienceRubric(
                min_salience=config.min_salience,
                max_memories_per_episode=config.max_memories_per_candidate,
                scale_by_confidence=True,
            ),
            dedup_threshold=config.dedup_threshold,
            retention=RetentionPolicy(
                protected_memory_types=config.protected_memory_types,
            ),
        )
        motives = tuple(
            project_motive if motive.name == PROJECT_MEMORY_POLICY_MOTIVE else motive for motive in memory_bank.motives
        )
        if not any(motive.name == PROJECT_MEMORY_POLICY_MOTIVE for motive in memory_bank.motives):
            motives = (*memory_bank.motives, project_motive)
        updated_bank = MemoryBank(motives=motives)
        project_pack = PromptPack(
            name=PROJECT_MEMORY_PROMPT_PACK,
            prompt_profile=PROJECT_MEMORY_PROMPT_PROFILE,
            prompt_profile_version=PROJECT_MEMORY_PROMPT_PROFILE_VERSION,
            prompt_override=prompt_override,
        )
        prompt_packs = tuple(
            project_pack if pack.name == PROJECT_MEMORY_PROMPT_PACK else pack for pack in tenant.prompt_packs
        )
        if not any(pack.name == PROJECT_MEMORY_PROMPT_PACK for pack in tenant.prompt_packs):
            prompt_packs = (*tenant.prompt_packs, project_pack)
        target_scope = project_scope(tenant_id)
        scopes = tuple(
            scope.model_copy(
                update={
                    "motive": PROJECT_MEMORY_POLICY_MOTIVE,
                    "prompt_pack": PROJECT_MEMORY_PROMPT_PACK,
                }
            )
            if scope.scope.key == target_scope.key
            else scope
            for scope in tenant.scopes
        )
        updated_tenant = tenant.model_copy(
            update={
                "memory_bank": updated_bank,
                "prompt_packs": prompt_packs,
                "scopes": scopes,
            }
        )
        client.control_plane = MemoryControlPlane(
            base_config=control_plane.base_config,
            tenants=tuple(
                updated_tenant if candidate.tenant_id == tenant.tenant_id else candidate
                for candidate in control_plane.tenants
            ),
        )
