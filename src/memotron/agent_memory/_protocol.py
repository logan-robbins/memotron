"""What an AgentMemoryPlatform concern mixin may assume the composed platform provides.

Fourth of these, after the two storage backends and DreamEngine, and by far the
smallest: **17 cross-boundary members against DreamEngine's 61**, over a class with
63 methods rather than 152. That gap is the finding. `AgentMemoryPlatform` was
better separated before anyone split it, for a structural reason worth naming --
it holds no storage handle. Everything it does goes through `self.client`, so the
concerns never had a shared mutable substrate to entangle around. DreamEngine has
`self._graph`, and 10/10 of its mixins touch it.

Generated from the measured call graph, not hand-listed: a member appears here only
if some OTHER concern calls it. Members a concern defines and only the composer
calls are absent on purpose -- the composer reaches them through the MRO.

One assignment is deliberate rather than positional. `principal_for_agent` sits in
the bootstrap block by line number, but its only caller is `_authorize`, inside the
registry. Left where it lay it closed a four-module cycle (_bootstrap, _publish,
_registry, _session); assigned to _registry the package is an acyclic four layers:

    L0  _project  _registry  _render
    L1  _curation  _prompts  _publish  _reporting
    L2  _session
    L3  _bootstrap  _transcripts

That was measured BEFORE the first cut. The dreaming split found its equivalent
cycle only after the mixins existed and paid three commits to undo it.

Usage, as everywhere else:

    if TYPE_CHECKING:
        _Base = ComposedAgentMemoryPlatform
    else:
        _Base = object

At runtime the base is plain ``object``, so the MRO is unchanged.
"""

from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from memotron.agent_memory._results import (
        AgentMemoryMode,
        AgentMemoryStartResult,
        AgentRegistrationResult,
        ProjectMemoryConfig,
    )
    from memotron.client import Memotron
    from memotron.config import MemoryPrincipal
    from memotron.models import MemoryProfile, MemoryScope, UseEvent, UseEventKind


class ComposedAgentMemoryPlatform(Protocol):
    """The composed :class:`AgentMemoryPlatform` surface, as one mixin sees it."""

    # -- state owned by the composer, set in __init__ (which never moves, R-A1) --
    client: Memotron
    tenant_id: str
    #: Supplied by the transport (#206 Phase 2); None for in-process embedders.
    _principal_provider: Callable[[], MemoryPrincipal | None] | None
    mode: AgentMemoryMode
    project_scope: MemoryScope
    user_scope: MemoryScope | None
    _agent_registration_lock: Lock

    # -- helpers kept on the composer, per R-A2 ------------------
    def agent_scope(self, agent_id: str) -> MemoryScope: ...
    def register_agent(self, *, agent_id: str, agent_name: str, source: str = ...) -> AgentRegistrationResult: ...

    # -- contributed by _project ---------------------------------
    @staticmethod
    def apply_project_memory_config_to_client(*, client: Memotron, tenant_id: str) -> None: ...
    def project_memory_config(self) -> ProjectMemoryConfig | None: ...

    # -- contributed by _prompts ---------------------------------
    @staticmethod
    def apply_tenant_prompt_override_to_client(*, client: Memotron, tenant_id: str) -> None: ...

    # -- contributed by _session ---------------------------------
    def _session_scopes(self, agent_id: str) -> list[MemoryScope]: ...
    def _session_use_events(
        self, *, scopes: list[MemoryScope], session_id: str, kinds: set[UseEventKind]
    ) -> list[tuple[MemoryScope, UseEvent]]: ...
    async def memory_start(
        self,
        *,
        agent_id: str,
        agent_token_budget: int = ...,
        project_token_budget: int = ...,
        task_run_id: str | None = ...,
        checkpoint_seed: str | None = ...,
    ) -> AgentMemoryStartResult: ...

    # -- contributed by _publish ---------------------------------
    async def _record_profile_injections(
        self, *, profile: MemoryProfile, task_run_id: str, token_budget: int
    ) -> list[UseEvent]: ...

    # -- contributed by _registry --------------------------------
    def _authorize(self, *, agent_id: str, scope: MemoryScope) -> None: ...
    def _authorize_row(
        self,
        *,
        agent_id: str,
        relationship_uuid: str,
        scope: MemoryScope,
        action: str,
        require_on_allowlist: bool = ...,
    ) -> Any: ...
    def _load_registered_agents(self) -> None: ...
    def _register_agent_policy(self, agent_id: str, *, motive_override: str | None = ...) -> None: ...
    def _require_agent(self, agent_id: str) -> str: ...
    def _require_caller_tenant(
        self, operation: str, *, provider: Callable[[], MemoryPrincipal | None] | None = ...
    ) -> None: ...
    def _target_scope(self, *, agent_id: str, scope: str) -> MemoryScope: ...

    # -- contributed by _render ----------------------------------
    @staticmethod
    def _render_log_body(
        *,
        agent_id: str,
        summary: str,
        decisions: tuple[str, ...],
        incidents: tuple[str, ...],
        blockers: tuple[str, ...],
    ) -> str: ...
    @staticmethod
    def _render_start_context(
        *,
        project_profile: MemoryProfile,
        agent_profile: MemoryProfile,
        user_profile: MemoryProfile | None,
        mode: AgentMemoryMode,
    ) -> str: ...
