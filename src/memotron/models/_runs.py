"""Dream-run bookkeeping -- what a pass did, and what it decided.

A run record is the audit envelope; a decision record is one governed choice
inside it. The formation-contract attestation is what makes a run's policy
inputs verifiable after the fact rather than merely logged."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from memotron.models._enums import (
    DreamJobKind,
)
from memotron.models._graph import (
    MemoryScope,
)
from memotron.models._health import (
    MemoryHealthReport,
)


class DreamJobRun(BaseModel):
    job_name: str
    job_kind: DreamJobKind
    run_uuid: str | None = None
    """WS-11: receipt ledger run correlation id, set by DreamEngine.run_job when a
    receipted run was begun (None for coherence job kinds, which receipt per-scan)."""
    processed_episodes: int = 0
    processed_scopes: int = 0
    created_nodes: int = 0
    created_relationships: int = 0
    reinforced_relationships: int = 0
    superseded_relationships: int = 0
    pruned_relationships: int = 0
    decision_count: int = 0
    quarantined_candidates: int = 0
    """WS-24: candidates this run's actionability gate refused and filed in
    quarantine — retained, receipted, promotable, never in the graph."""
    admitted_candidates: int = 0
    """WS-24: candidates the gate admitted.  With ``quarantined_candidates``
    this is the abstain-rate denominator, stated so it is never ambiguous."""
    health: list[MemoryHealthReport] = Field(default_factory=list)
    """WS-24: one type-distribution health report per scope this run formed
    into, with every threshold trip already receipted."""


class DreamJobRunRecord(BaseModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    ran_at: datetime
    job_name: str
    job_kind: DreamJobKind
    run_uuid: str | None = None
    """WS-11: receipt ledger run correlation id — joins this history row to its
    ``run_checkpoints`` entry / ``memory_receipts`` chain (PATENT_REPLAY_RECEIPTS_SPEC [0022])."""
    processed_episodes: int = 0
    processed_scopes: int = 0
    created_nodes: int = 0
    created_relationships: int = 0
    reinforced_relationships: int = 0
    superseded_relationships: int = 0
    pruned_relationships: int = 0
    decision_count: int = 0


class DreamDecisionRecord(BaseModel):
    uuid: str = Field(default_factory=lambda: str(uuid4()))
    ran_at: datetime
    job_name: str
    job_kind: DreamJobKind
    agent_id: str
    agent_name: str
    agent_scope: MemoryScope
    decision_type: str
    summary: str
    subject_id: str | None = None
    subject_name: str | None = None
    scope: MemoryScope | None = None
    prompt_profile: str | None = None
    prompt_profile_version: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class FormationContractAttestation(BaseModel):
    """Signed DSSE/in-toto statement binding a certification verdict to a contract."""

    model_config = {"frozen": True}

    payload_type: str
    payload: str
    key_id: str
    public_key: str
    signature: str
    contract_digest: str
    certification_verdict: str


class DreamJobStatus(BaseModel):
    job_name: str
    job_kind: DreamJobKind
    due: bool
    cadence_seconds: int
    max_items_per_run: int
    last_run: datetime | None = None
    next_run: datetime | None = None
    seconds_until_due: int = 0
    scope: MemoryScope | None = None
    instruction_set: str
    agent_id: str
    agent_name: str
    agent_scope: MemoryScope
    prompt_profile: str
    prompt_profile_version: str
    prompt_override_enabled: bool = False
    pending_episodes: int = 0
    eligible_scopes: int = 0


class DreamRunResult(BaseModel):
    ran_at: datetime
    job_runs: list[DreamJobRun] = Field(default_factory=list)

    @property
    def processed_episodes(self) -> int:
        return sum(run.processed_episodes for run in self.job_runs)

    @property
    def created_relationships(self) -> int:
        return sum(run.created_relationships for run in self.job_runs)

    @property
    def processed_scopes(self) -> int:
        return sum(run.processed_scopes for run in self.job_runs)

    @property
    def decision_count(self) -> int:
        return sum(run.decision_count for run in self.job_runs)
