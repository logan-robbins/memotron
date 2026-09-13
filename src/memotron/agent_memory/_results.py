"""What every agent-memory operation hands back.

Fourteen result and settings types, thirteen of them pure data. They live here rather
than in memotron.models because they are the AGENT PLATFORM's vocabulary, not the
memory graph's: a MemoryScope is a graph concept, an AgentMemoryStartResult is a
session-shaped answer to "what should this agent know right now".

ProjectMemorySettings is the exception -- a settings object with four methods of
resolution logic rather than a result. It stays with its siblings anyway: splitting
one class out of a coherent value-type module to satisfy a rule about method counts
would cost a reader more than it buys."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from memotron.agent_memory._common import (
    _normalize_non_blank,
)
from memotron.models import (
    ArchivedMatchReport,
    DreamRunResult,
    MemoryEvolutionProof,
    MemoryProfile,
    MemoryScope,
    MemoryType,
    SearchResult,
    UseEvent,
)


class AgentMemoryMode(StrEnum):
    """Adoption model exposed to memory clients."""

    SIMPLE = "simple"
    MULTI_AGENT = "multi-agent"


class AgentMemoryStartResult(BaseModel):
    tenant_id: str
    agent_id: str
    mode: AgentMemoryMode
    project_scope: MemoryScope
    agent_scope: MemoryScope
    project_profile: MemoryProfile
    agent_profile: MemoryProfile
    user_scope: MemoryScope | None = None
    user_profile: MemoryProfile | None = None
    rendered_context: str
    tokens_used: int
    task_run_id: str
    use_events: list[UseEvent] = Field(default_factory=list)
    checkpoint_seed_applied: bool = False
    """WS-28 T1: True when a non-blank ``checkpoint_seed`` was supplied and
    the compaction-recovery search actually ran.  False (including every
    pre-WS-28 caller, which never passes the parameter) is byte-identical
    to today's ``memory_start``."""


class AgentRegistrationResult(BaseModel):
    tenant_id: str
    agent_id: str
    agent_name: str
    source: str
    created_at: str
    last_seen_at: str
    created: bool


class AgentMemoryBootstrapResult(BaseModel):
    registration: AgentRegistrationResult
    start: AgentMemoryStartResult
    next_actions: tuple[str, ...]


class AgentMemorySearchResult(BaseModel):
    tenant_id: str
    agent_id: str
    query: str
    results: list[SearchResult] = Field(default_factory=list)
    task_run_id: str
    use_events: list[UseEvent] = Field(default_factory=list)
    archived: ArchivedMatchReport = Field(default_factory=ArchivedMatchReport)
    """Archived memories this query matched.

    By default a matching archived memory is revived by the search that found
    it (``archived.disposition == "revived"``) and also appears in ``results``.
    When the deployment sets ``DreamConfig.pure_read_retrieval`` search writes
    nothing and these are the memories available to restore
    (``archived.disposition == "available_to_restore"``).  In either mode the
    agent can ask for a specific ``archived.matches[i].relationship_uuid`` back
    with the ``memory_restore`` curation tool.
    """


class AgentMemoryRefreshResult(BaseModel):
    tenant_id: str
    agent_id: str
    mode: AgentMemoryMode
    project_scope: MemoryScope
    agent_scope: MemoryScope
    agent_run: DreamRunResult
    user_scope: MemoryScope | None = None
    user_run: DreamRunResult | None = None
    project_run: DreamRunResult | None = None


class ProjectMemorySettings(BaseModel):
    """User-tunable policy inputs for the shared project-memory dream."""

    project_goal: str = Field(min_length=1, max_length=4000)
    memory_goal: str = Field(min_length=1, max_length=4000)
    keep: tuple[str, ...] = Field(min_length=1)
    exclude: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    allowed_memory_types: tuple[MemoryType, ...] = (
        MemoryType.REQUIREMENT,
        MemoryType.DIRECTIVE,
        MemoryType.STATE,
        MemoryType.DECISION,
        MemoryType.INCIDENT,
        MemoryType.ROLLUP,
    )
    protected_memory_types: tuple[MemoryType, ...] = (
        MemoryType.REQUIREMENT,
        MemoryType.DECISION,
        MemoryType.INCIDENT,
    )
    min_salience: float = Field(default=0.0, ge=0.0, le=1.0)
    max_memories_per_candidate: int = Field(default=12, ge=1, le=100)
    dedup_threshold: float = Field(default=0.87, ge=0.0, le=1.0)
    min_endorsements: int = Field(default=1, ge=1)
    """WS-19 T20: endorsement votes an object-level promotion candidate needs
    before project formation may consume it.  The default (1) is exactly
    today's single-agent publish effort level — the promoting agent's own
    endorsement is recorded at memory_promote time, so nothing changes for
    existing deployments until an operator raises the bar.

    WS-23 M4 — the threshold is only as strong as the identity behind a vote.
    The hosted HTTP surface binds the request's ``agent_id`` to the
    authenticated principal, so a quorum there is a quorum of authenticated
    agents.  Over the stdio MCP surface the caller declares its own
    ``agent_id`` (registration is attribution, not authentication — a local
    stdio server trusts the process it was launched by), so a threshold above 1
    is ADVISORY there: it measures deliberate distinct votes, not
    independently authenticated ones.  Deployments that need an enforceable
    quorum must run the authenticated surface with one principal per agent."""

    @field_validator("project_goal", "memory_goal")
    @classmethod
    def normalize_goals(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank(value, str(info.field_name))

    @field_validator("keep", "exclude", "rules", mode="before")
    @classmethod
    def normalize_guidance(cls, value: Any, info: ValidationInfo) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a list or tuple of strings")
        if any(not isinstance(item, str) for item in value):
            raise ValueError(f"{info.field_name} must contain only strings")
        return tuple(dict.fromkeys(item.strip() for item in value if item.strip()))

    @field_validator(
        "allowed_memory_types",
        "protected_memory_types",
        mode="before",
    )
    @classmethod
    def normalize_memory_types(cls, value: Any, info: ValidationInfo) -> tuple[MemoryType, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a list or tuple")
        return tuple(dict.fromkeys(item if isinstance(item, MemoryType) else MemoryType(str(item)) for item in value))

    @model_validator(mode="after")
    def validate_policy(self) -> ProjectMemorySettings:
        if not self.keep:
            raise ValueError("keep must contain at least one project-memory criterion")
        if not self.allowed_memory_types:
            raise ValueError("allowed_memory_types must contain at least one memory type")
        unexpected = set(self.protected_memory_types) - set(self.allowed_memory_types)
        if unexpected:
            values = ", ".join(sorted(item.value for item in unexpected))
            raise ValueError(f"protected_memory_types must be a subset of allowed_memory_types; unexpected: {values}")
        return self


class ProjectMemoryConfig(ProjectMemorySettings):
    tenant_id: str
    version: str
    configured_by: str
    active: bool = True
    created_at: str
    updated_at: str


class ProjectMemoryPublishResult(BaseModel):
    tenant_id: str
    agent_id: str
    project_scope: MemoryScope
    policy_version: str
    task_run_id: str
    episode_uuid: str
    queued_for_dreaming: bool


class PromotionResult(BaseModel):
    """WS-19 T20: state of one object-level promotion candidate after a
    memory_promote / memory_endorse_promotion call.

    ``created`` is True only when this call created the candidate episode;
    re-promoting a source row with a pending candidate — or endorsing an
    existing candidate — returns the same candidate with ``created=False``.
    ``eligible_for_formation`` reflects the ACTIVE project-memory policy's
    ``min_endorsements`` at call time; formation re-reads the live threshold.
    """

    tenant_id: str
    agent_id: str
    project_scope: MemoryScope
    policy_version: str
    candidate_episode_uuid: str
    source_relationship_uuid: str
    source_scope_key: str
    source_receipt_digest: str = ""
    created: bool
    endorsement_count: int
    endorsements: tuple[str, ...] = ()
    min_endorsements: int
    eligible_for_formation: bool
    task_run_id: str = ""


class AgentMemoryEvolutionResult(BaseModel):
    tenant_id: str
    agent_id: str
    mode: AgentMemoryMode
    agent_scope: MemoryScope
    agent_evolution: MemoryEvolutionProof
    user_scope: MemoryScope | None = None
    user_evolution: MemoryEvolutionProof | None = None
    project_scope: MemoryScope | None = None
    project_evolution: MemoryEvolutionProof | None = None


class CitationScanResult(BaseModel):
    """WS-15 T8/T9: outcome of one deterministic transcript-citation scan."""

    tenant_id: str
    agent_id: str
    session_id: str
    task_run_id: str
    scanned_events: int = 0
    """Candidate INJECTED/RETRIEVED use events found for the session."""
    cited_count: int = 0
    """Distinct relationships whose citation was recorded (or already stood)."""
    use_event_ids: tuple[str, ...] = ()
    """The CITED_OR_USED use-event ids backing this session's citations —
    stable across repeated hook firings (idempotent keys)."""
    cited_relationship_uuids: tuple[str, ...] = ()


class RunbookCaptureResult(BaseModel):
    """WS-28 T3: outcome of one deterministic runbook-capture pass."""

    tenant_id: str
    agent_id: str
    captured_commands: tuple[str, ...] = ()
    """Verbatim commands (>= the shape-matching threshold) queued this call."""
    episode_uuids: tuple[str, ...] = ()
    """The queued episode uuids, positionally matching ``captured_commands``."""


class OutcomeJudgeResult(BaseModel):
    """WS-15 T10: outcome of one session-end outcome-judge invocation."""

    tenant_id: str
    agent_id: str
    session_id: str
    task_run_id: str
    judge_configured: bool = False
    judge_identity: str = ""
    judged_count: int = 0
    outcome_event_ids: tuple[str, ...] = ()
    skipped_reason: str = ""
    """``no_judge_configured`` / ``no_cited_memories`` / ``already_judged``
    when nothing was judged; blank when verdicts were recorded."""
