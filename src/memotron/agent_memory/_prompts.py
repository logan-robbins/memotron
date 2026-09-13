"""Installing a tenant's prompt override onto the client, and taking it off again.

Five methods. `clear_tenant_prompt_from_client` exists because the install is
mutating -- these write onto a live Memotron rather than returning a config, so
an override with no removal path is an override you cannot back out."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memotron.agent_memory._config import DEFAULT_AGENT_MEMORY_MOTIVE
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._registry import (
    TENANT_ACTIVE_PROMPT_PACK,
    TENANT_ACTIVE_PROMPT_PROFILE,
    TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
    AgentRegistryMixin,
)
from memotron.client import Memotron
from memotron.config import (
    DreamPromptOverride,
    DreamPromptProfile,
    MemoryControlPlane,
)

TENANT_FULL_PROMPT_PROFILE = "tenant-full-prompt"

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryPromptMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    tenant_id: Any

    def configure_tenant_prompt_override(
        self,
        *,
        prompt_profile: str = TENANT_ACTIVE_PROMPT_PROFILE,
        prompt_profile_version: str = TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
        goal: str = "",
        include: list[str] | tuple[str, ...] = (),
        exclude: list[str] | tuple[str, ...] = (),
        rules: list[str] | tuple[str, ...] = (),
        examples: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        override = DreamPromptOverride(
            goal=goal.strip() or None,
            include=tuple(include),
            exclude=tuple(exclude),
            rules=tuple(rules),
            examples=tuple(examples),
        )
        self.client.config.prompt_profile(prompt_profile, prompt_profile_version)
        state = self.client.graph.set_tenant_prompt_override(
            tenant_id=self.tenant_id,
            prompt_profile=prompt_profile,
            prompt_profile_version=prompt_profile_version,
            override=override.model_dump(mode="json"),
        )
        self.apply_tenant_prompt_override_to_client(client=self.client, tenant_id=self.tenant_id)
        self.apply_project_memory_config_to_client(
            client=self.client,
            tenant_id=self.tenant_id,
        )
        return state

    def configure_tenant_prompt(
        self,
        *,
        prompt_text: str,
        motive_name: str = DEFAULT_AGENT_MEMORY_MOTIVE,
        source_profile: str = TENANT_ACTIVE_PROMPT_PROFILE,
        source_profile_version: str = TENANT_ACTIVE_PROMPT_PROFILE_VERSION,
    ) -> dict[str, Any]:
        state = self.client.graph.save_tenant_prompt_version(
            tenant_id=self.tenant_id,
            prompt_text=prompt_text,
            motive_name=motive_name,
            source_profile=source_profile,
            source_profile_version=source_profile_version,
        )
        self.apply_tenant_prompt_to_client(client=self.client, tenant_id=self.tenant_id)
        self.apply_project_memory_config_to_client(
            client=self.client,
            tenant_id=self.tenant_id,
        )
        return state

    @staticmethod
    def apply_tenant_prompt_override_to_client(*, client: Memotron, tenant_id: str) -> None:
        AgentMemoryPromptMixin.apply_tenant_prompt_to_client(client=client, tenant_id=tenant_id)

    @staticmethod
    def apply_tenant_prompt_to_client(*, client: Memotron, tenant_id: str) -> None:
        if client.control_plane is None:
            return
        active_version = client.graph.active_tenant_prompt_version(tenant_id)
        if active_version is not None:
            version = str(active_version["version"])
            prompt_text = str(active_version["prompt_text"])
            custom_profile = DreamPromptProfile(
                name=TENANT_FULL_PROMPT_PROFILE,
                version=version,
                goal="Tenant-edited full prompt text.",
                prompt_text=prompt_text,
            )
            AgentRegistryMixin._apply_prompt_pack_to_client(
                client=client,
                tenant_id=tenant_id,
                prompt_profile=TENANT_FULL_PROMPT_PROFILE,
                prompt_profile_version=version,
                override=DreamPromptOverride(),
                custom_profile=custom_profile,
                motive_name=str(active_version.get("motive_name") or "") or None,
            )
            return
        state = client.graph.tenant_prompt_override(tenant_id)
        if state is None:
            return
        AgentRegistryMixin._apply_prompt_pack_to_client(
            client=client,
            tenant_id=tenant_id,
            prompt_profile=str(state["prompt_profile"]),
            prompt_profile_version=str(state["prompt_profile_version"]),
            override=DreamPromptOverride.model_validate(state["override"]),
        )

    @staticmethod
    def clear_tenant_prompt_from_client(*, client: Memotron, tenant_id: str) -> None:
        control_plane = client.control_plane
        if control_plane is None:
            return
        try:
            tenant = control_plane.tenant(tenant_id)
        except ValueError:
            return
        prompt_packs = tuple(pack for pack in tenant.prompt_packs if pack.name != TENANT_ACTIVE_PROMPT_PACK)
        updated_tenant = tenant.model_copy(
            update={
                "default_prompt_pack": (
                    None if tenant.default_prompt_pack == TENANT_ACTIVE_PROMPT_PACK else tenant.default_prompt_pack
                ),
                "prompt_packs": prompt_packs,
            }
        )
        client.control_plane = MemoryControlPlane(
            base_config=control_plane.base_config,
            tenants=tuple(
                updated_tenant if candidate.tenant_id == tenant.tenant_id else candidate
                for candidate in control_plane.tenants
            ),
        )
