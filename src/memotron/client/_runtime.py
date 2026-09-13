"""Sessions, tenant policy, transports, and running dream jobs.

Thirty-two members -- the most of any group, but mostly small: job status, run
records, epoch redream, receipts. `_runtime_policy` is the one to know about; it
raises when no control plane is configured, which is why several SDK paths require
one and several do not."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from memotron.agents import DreamAgentTransport
from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    DreamConfig,
    EffectiveMemoryPolicy,
    MemoryBank,
    Motive,
)
from memotron.dreaming import DreamEngine
from memotron.epochs import (
    AdoptResult,
    EpisodeSelector,
    EpochDiff,
    RedreamOverrides,
    RedreamResult,
    RollbackResult,
    adopt_epoch,
    branch_and_recompute,
    build_session_digest,
    diff_epochs,
    open_shadow_store,
    rollback_epoch,
)
from memotron.extraction import ExtractionTransport, InstructionalExtractor
from memotron.models import (
    DreamDecisionRecord,
    DreamJobRun,
    DreamJobRunRecord,
    DreamJobStatus,
    DreamRunResult,
    GraphRelationship,
    MemoryScope,
)
from memotron.receipts import (
    MemoryReceipt,
    NegativeSpaceEntry,
    RunCheckpoint,
    config_effective_policy_digest,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    # Annotation-only, and deferred because the runtime imports they mirror are
    # function-local. The package docstring used to say every module here imports
    # `client` back; measured 2026-08-28 that is true only for `replay` (via
    # memotron -> agent_memory -> client). The rest are deferred by convention
    # now, not necessity, and hoisting them is its own commit.
    from memotron.config import MemoryPrincipal
    from memotron.session import SessionIngester


if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class RuntimeMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    control_plane: Any
    graph: StorageBackend
    rollup_synthesis_transport: Any
    #: The construction-time transport pair, captured the first time anything overwrites
    #: the default. Set lazily in set_runtime_transports; see transports_for_tenant (#158).
    _base_transports: tuple[Any, Any]

    def open_session(
        self,
        *,
        name: str,
        scope: MemoryScope,
        session_id: str | None = None,
        turns_per_episode: int = 8,
        max_chars_per_episode: int = 4000,
        time_gap_seconds: int | None = 300,
        source_description: str = "conversation session",
        metadata: dict[str, Any] | None = None,
        instruction_set: str = "default",
    ) -> SessionIngester:
        """Return a SessionIngester for streaming turn-by-turn ingestion.

        Use as an async context manager or call flush() / close() manually:

            async with client.open_session(name="chat-123", scope=user_scope) as session:
                await session.add_turn("user", "...")
                await session.add_turn("assistant", "...")
        """
        self._require_authorized_scope(scope)
        from memotron.session import SessionIngester

        return SessionIngester(
            client=self,
            name=name,
            scope=scope,
            session_id=session_id,
            turns_per_episode=turns_per_episode,
            max_chars_per_episode=max_chars_per_episode,
            time_gap_seconds=time_gap_seconds,
            source_description=source_description,
            metadata=metadata,
            instruction_set=instruction_set,
        )

    def resolve_policy(
        self,
        *,
        tenant_id: str,
        agent_id: str | None = None,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> EffectiveMemoryPolicy:
        # T3-8. Before the control_plane check on purpose: an unauthorized scope is
        # refused whether or not a control plane happens to be configured.
        self._require_authorized_scope(scope)
        if self.control_plane is None:
            raise ValueError("control_plane is not configured")
        policy = self.control_plane.resolve(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
        return self._apply_active_policy_alias(policy)

    def resolve_policy_for_principal(
        self,
        *,
        principal: MemoryPrincipal,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> EffectiveMemoryPolicy:
        # T3-8. Guarded ahead of the control_plane check, as in resolve_policy.
        self._require_authorized_scope(scope)
        if self.control_plane is None:
            raise ValueError("control_plane is not configured")
        policy = self.control_plane.resolve_for_principal(
            principal=principal,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
        return self._apply_active_policy_alias(policy)

    def _apply_active_policy_alias(
        self,
        policy: EffectiveMemoryPolicy,
        *,
        alias: str = "production",
    ) -> EffectiveMemoryPolicy:
        alias_state = self.graph.policy_alias(
            scope_key=policy.scope.key,
            alias=alias,
        )
        if alias_state is None:
            return policy
        stored = self.graph.policy_contract(contract_digest=str(alias_state["contract_digest"]))
        if not bool(stored["certification_passed"]):
            raise RuntimeError(f"active policy alias {alias!r} references an uncertified contract")
        payload = stored["payload"]
        contract_scope = MemoryScope.model_validate(payload["scope"])
        if contract_scope != policy.scope:
            raise RuntimeError(f"active policy alias {alias!r} scope does not match runtime scope")
        motive = Motive.model_validate(payload["motive"])
        current_bank = policy.memory_bank or MemoryBank()
        motives = tuple(motive if existing.name == motive.name else existing for existing in current_bank.motives)
        if not any(existing.name == motive.name for existing in current_bank.motives):
            motives = (*motives, motive)
        bank = MemoryBank(motives=motives)
        digest = str(alias_state["contract_digest"])
        return policy.model_copy(
            update={
                "motive_name": motive.name,
                "motive": motive,
                "memory_bank": bank,
                "policy_alias": alias,
                "policy_contract_digest": digest,
                "certification_verdict": "certified",
                "source_trace": {
                    **policy.source_trace,
                    "motive": f"policy_alias:{alias}",
                    "policy_contract": digest,
                    "certification": "passed",
                },
            }
        )

    def set_runtime_transports(
        self,
        *,
        extraction_transport: ExtractionTransport | None = None,
        dream_agent_transport: DreamAgentTransport | None = None,
    ) -> None:
        # Preserve the construction-time pair the FIRST time anything overwrites it.
        # `transports_for_tenant` falls back to this for a tenant with no credential --
        # falling back to the mutable `self._extractor` instead would hand that tenant
        # whatever credential was installed last, which is #158 by another route, and
        # would also make clearing a credential re-install the one just revoked.
        self._capture_base_transports()
        self._extractor = InstructionalExtractor(transport=extraction_transport)
        self._dream_agent_transport = dream_agent_transport
        self._engine = self._engine_for_config(self.config)

    def _capture_base_transports(self) -> None:
        """Remember the construction-time pair, once, before anything overwrites it.

        Its own method because mypy cannot infer ``self._extractor`` inside
        ``set_runtime_transports`` -- the attribute is reassigned later in that same
        scope, so reading it first is a ``[has-type]`` error.
        """
        if not hasattr(self, "_base_transports"):
            self._base_transports = (self._extractor, self._dream_agent_transport)

    def transports_for_tenant(self, tenant_id: str | None) -> tuple[Any, Any]:
        """The ``(extractor, dream_agent_transport)`` pair *tenant_id*'s work must use.

        #158. This used to be a process-global field, mutated by whichever tenant called
        ``configure_tenant_llm`` last -- so a tenant that had configured nothing was served
        by someone else's credential, billed to their gateway key, authenticated upstream
        as them (ADR 0008 binds gateway identity to the key), and sent to whatever
        ``base_url`` they had set.

        Resolution order, per call:

        * ``tenant_id is None`` -> the construction-time default. A tenant-less operation
          behaves exactly as before; configuring a tenant does not touch it.
        * the tenant has a sealed credential -> transports built from *that* credential.
        * otherwise -> the construction-time default. NOT another tenant's.

        **NOT CACHED, deliberately (#159).** The first version of this cached per tenant,
        and a credential is per-GRAPH while a cache is per-CLIENT-OBJECT -- so a rotation
        or revocation through one client left a second client on the same store serving the
        old key indefinitely, and ``local_platform.py:209-210`` ships exactly that topology.
        A storage-level delete (erasure tooling, another process) was never seen at all, and
        ``worker.py`` -- a long-lived client that never calls configure or clear -- served a
        revoked credential until the pod restarted.

        Measured before removing it: the cache saved **0.018 ms** per operation, on a path
        that then makes LLM network calls of ~100 ms. It bought nothing and was the only
        thing that made stale credentials possible. Resolving from storage every time makes
        revocation correct by construction, for every client, process and out-of-band
        deletion -- there is no state to invalidate. It also removes a whole bug class: the
        cache key had to agree with storage's own normalisation, and did not.

        Note the limit of the fallback: it is whatever transport this client was BUILT with.
        Several call sites construct a client from one tenant's sealed credential
        (``local_platform.py:135,171``, ``agent_memory_mcp.py:120``,
        ``admin_server/__init__.py:1366``) and those clients are single-tenant. On a
        MULTI-tenant client the base is ``build_transports_from_env()``
        (``mcp_server.py:106``) or a keyless rule-based extractor. Do not construct a
        multi-tenant client from one tenant's credential.
        """
        if tenant_id is None:
            return (self._extractor, self._dream_agent_transport)

        from memotron.extraction import InstructionalExtractor
        from memotron.runtime import build_transports_from_tenant_credentials

        credentials = self.graph.tenant_llm_credentials(tenant_id)
        if credentials is None:
            return getattr(self, "_base_transports", (self._extractor, self._dream_agent_transport))

        extraction_transport, dream_agent_transport = build_transports_from_tenant_credentials(credentials)
        return (InstructionalExtractor(transport=extraction_transport), dream_agent_transport)

    def bind_default_tenant(self, tenant_id: str) -> None:
        """Install *tenant_id*'s transports as this client's process default.

        For a client that serves EXACTLY ONE tenant -- :class:`AgentMemoryPlatform` is the
        case in this repo, bound to a single ``tenant_id`` at construction -- installing
        that tenant's credential globally is correct: there is no other tenant on this
        client to leak to.

        Do NOT call this from a multi-tenant surface. Doing so re-creates #158: the
        governance MCP server holds one shared client and serves any ``tenant_id`` a caller
        names, so a global install there means whichever tenant configured last owns
        extraction, billing and upstream identity for everyone. That is why
        :meth:`configure_tenant_llm_credentials` no longer does this implicitly -- the
        binding has to be a deliberate statement that the client is single-tenant.
        """
        extractor, agent_transport = self.transports_for_tenant(tenant_id)
        self.set_runtime_transports(
            extraction_transport=getattr(extractor, "_transport", None),
            dream_agent_transport=agent_transport,
        )

    def engine_for_tenant(self, tenant_id: str | None, config: DreamConfig | None = None) -> DreamEngine:
        """A :class:`DreamEngine` carrying *tenant_id*'s transports.

        Not cached, for the same reason as :meth:`transports_for_tenant` (#159): a cached
        engine holds a cached credential. Measured: construction is **0.001 ms**, so there
        was nothing to save and a stale engine to lose.
        """
        if config is not None:
            return self._engine_for_config(config, tenant_id=tenant_id)
        if tenant_id is None:
            return self._engine
        return self._engine_for_config(self.config, tenant_id=tenant_id)

    def configure_tenant_llm_credentials(
        self,
        *,
        tenant_id: str,
        provider: str,
        api_key: str,
        base_url: str = "",
        model: str = "",
    ) -> dict[str, Any]:
        """Seal *tenant_id*'s credential. Does NOT change any other tenant's transports.

        #158: this used to end with ``set_runtime_transports(...)``, installing the caller's
        credential on the shared client for every tenant in the process. It now only seals;
        resolution happens per operation in :meth:`transports_for_tenant`, which reads
        storage every time, so the new credential takes effect for every client on this
        graph with nothing to invalidate (#159).
        """
        return self.graph.set_tenant_llm_credentials(
            tenant_id=tenant_id,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            model=model,
        )

    def tenant_llm_credential_state(self, tenant_id: str) -> dict[str, Any] | None:
        return self.graph.tenant_llm_credential_state(tenant_id)

    def clear_tenant_llm_credentials(self, tenant_id: str) -> bool:
        """Revoke *tenant_id*'s sealed credential and stop serving it immediately.

        #158: this used to rebuild the SHARED transports from the process environment, so
        clearing one tenant repointed every other tenant in the process.

        #159: it then invalidated a per-client cache, which a SECOND client on the same
        graph never saw. There is no cache now -- :meth:`transports_for_tenant` reads
        storage on every call -- so deleting the row IS the revocation, for every client,
        every process, and a storage-level delete that bypasses the client entirely.
        """
        return self.graph.clear_tenant_llm_credentials(tenant_id)

    async def run_due_dreams(
        self,
        *,
        now: datetime | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> DreamRunResult:
        self._require_explicit_authorized_scope(scope, operation="run_due_dreams")
        ran_at = now or datetime.now(UTC)
        result = DreamRunResult(ran_at=ran_at)
        policy = self._runtime_policy(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
        if policy is None:
            engine = self.engine_for_tenant(tenant_id)
            config = self.config
        elif not policy.enabled_job_kinds:
            return result
        else:
            config = policy.to_dream_config(self.config)
            engine = self._engine_for_config(config, tenant_id=tenant_id)
        for job in engine.due_jobs(now=ran_at):
            job_run = await self._run_job_and_record(
                job_name=job.name,
                now=ran_at,
                engine=engine,
                config=config,
                effective_policy=policy,
            )
            result.job_runs.append(job_run)
        return result

    async def run_dream_job(
        self,
        *,
        job_name: str,
        now: datetime | None = None,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        scope: MemoryScope | None = None,
        dream_mode: str | None = None,
        motive: str | None = None,
        prompt_pack: str | None = None,
    ) -> DreamRunResult:
        self._require_explicit_authorized_scope(scope, operation="run_dream_job")
        ran_at = now or datetime.now(UTC)
        policy = self._runtime_policy(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )
        if policy is None:
            engine = self.engine_for_tenant(tenant_id)
            config = self.config
            effective_job_name = job_name
        else:
            base_job = self._dream_job(job_name)
            if base_job.kind not in policy.enabled_job_kinds:
                raise ValueError(
                    f"dream job {job_name!r} kind {base_job.kind.value!r} is disabled by dream mode "
                    f"{policy.dream_mode.name!r}"
                )
            config = policy.to_dream_config(self.config)
            engine = self._engine_for_config(config, tenant_id=tenant_id)
            effective_job_name = policy.scoped_job_name(job_name)
        job_run = await self._run_job_and_record(
            job_name=effective_job_name,
            now=ran_at,
            engine=engine,
            config=config,
            effective_policy=policy,
        )
        return DreamRunResult(ran_at=ran_at, job_runs=[job_run])

    async def dream_status(self, *, now: datetime | None = None) -> list[DreamJobStatus]:
        anchor = now or datetime.now(UTC)
        return [self._dream_job_status(job=job, now=anchor) for job in self.config.jobs]

    async def dream_history(
        self,
        *,
        limit: int = 20,
        job_name: str | None = None,
    ) -> list[DreamJobRunRecord]:
        return self.graph.dream_job_runs(limit=limit, job_name=job_name)

    async def dream_decisions(
        self,
        *,
        limit: int = 20,
        job_name: str | None = None,
        agent_id: str | None = None,
    ) -> list[DreamDecisionRecord]:
        return self.graph.dream_decisions(limit=limit, job_name=job_name, agent_id=agent_id)

    async def redream_epochs(self, *, scope: MemoryScope) -> list[dict[str, Any]]:
        """WS-26 T2: every epoch ever created for *scope*, oldest first."""
        self._require_authorized_scope(scope)
        return self.graph.epochs_for_scope(scope.key)

    async def redream_active_epoch(self, *, scope: MemoryScope) -> str | None:
        """WS-26 T2: the scope's current HEAD epoch id, or None if uninitialized."""
        self._require_authorized_scope(scope)
        return self.graph.active_epoch_for_scope(scope.key)

    async def redream_branch(
        self,
        *,
        scope: MemoryScope,
        selector: EpisodeSelector,
        overrides: RedreamOverrides | None = None,
        now: datetime | None = None,
    ) -> RedreamResult:
        """WS-26 T3/T4/T8/T9: fork a new epoch and recompute it over
        *selector*'s episodes at the cheapest sufficient tier.

        HEAD is never opened for writing until :meth:`redream_adopt` — see
        ``memotron.epochs.branch_and_recompute``. Raises ``ValueError`` for
        a crypto-shred content-protected scope (WS-26 scope limitation) or a
        selector matching zero episodes.
        """
        self._require_authorized_scope(scope)
        return await branch_and_recompute(
            main_storage=self.graph,
            config=self.config,
            scope=scope,
            selector=selector,
            extractor=self._extractor,
            embedding_transport=self._embedding_transport,
            agent_transport=self._dream_agent_transport,
            rollup_synthesis_transport=self.rollup_synthesis_transport,
            overrides=overrides,
            now=now,
        )

    async def redream_diff(self, *, scope: MemoryScope, epoch_id: str) -> EpochDiff:
        """WS-26 T5: diff HEAD's current epoch against a recomputed branch.

        ``{added, removed, changed(by truth_slot_key), unchanged}`` plus the
        T9 registry deltas — surfaced on the admin API
        (``GET /api/epochs/diff``) and available to operator MCP tooling.
        """
        self._require_authorized_scope(scope)
        shadow = open_shadow_store(self.graph, epoch_id)
        try:
            return diff_epochs(main_storage=self.graph, shadow_storage=shadow, scope=scope)
        finally:
            shadow.close()

    async def redream_adopt(self, *, scope: MemoryScope, epoch_id: str, now: datetime | None = None) -> AdoptResult:
        """WS-26 T6: verify (WS-11 byte replay), then adopt — a pointer flip
        that merges exactly the diff's added/changed rows and registry
        overlays. Raises ``ReplayVerificationError`` fail-closed, with HEAD
        completely unmodified, if the branch's receipt chain does not verify.
        """
        self._require_authorized_scope(scope)
        return await adopt_epoch(main_storage=self.graph, scope=scope, epoch_id=epoch_id, now=now)

    async def redream_rollback(self, *, scope: MemoryScope, now: datetime | None = None) -> RollbackResult:
        """WS-26 T6: restore *scope*'s HEAD to its state immediately before
        the last adopt, byte-for-byte (graph_state_hash and
        registry_state_digest both restored exactly)."""
        self._require_authorized_scope(scope)
        return await rollback_epoch(main_storage=self.graph, scope=scope, now=now)

    async def redream_session_digest(
        self, *, scope: MemoryScope, session_id: str, now: datetime | None = None
    ) -> GraphRelationship | None:
        """WS-26 T7: a derived ``rollup`` summarizing one session's episodes
        (NOT a Date/Session content-plane node — NEXT.md §9). None when the
        session produced no active facts."""
        self._require_authorized_scope(scope)
        return build_session_digest(storage=self.graph, scope=scope, session_id=session_id, now=now)

    async def memory_receipts(
        self,
        *,
        run_uuid: str | None = None,
        scope: MemoryScope | None = None,
    ) -> list[MemoryReceipt]:
        """WS-11: canonical decision receipts for a run, or every receipt in a scope ([0019])."""
        self._require_authorized_scope(scope)
        if run_uuid is not None:
            return self.graph.receipts.receipts_for_run(run_uuid)
        if scope is not None:
            # Same chain order the raw query used; the ledger owns the SQL.
            return self.graph.receipts.receipts_for_scope(scope.key)
        raise ValueError("memory_receipts requires either run_uuid or scope")

    async def run_checkpoints(
        self,
        *,
        scope: MemoryScope | None = None,
        limit: int = 20,
    ) -> list[RunCheckpoint]:
        """WS-11: per-run Merkle checkpoints, newest first ([0022])."""
        self._require_explicit_authorized_scope(scope, operation="run_checkpoints")
        return self.graph.receipts.run_checkpoints(scope_key=scope.key if scope is not None else None, limit=limit)

    async def negative_space(
        self,
        *,
        scope: MemoryScope | None = None,
        **filters: Any,
    ) -> list[NegativeSpaceEntry]:
        """WS-11: query what the system chose NOT to remember, with reasons ([0027])."""
        self._require_explicit_authorized_scope(scope, operation="negative_space")
        scope_key = scope.key if scope is not None else None
        return self.graph.receipts.negative_space(scope_key=scope_key, **filters)

    def _operator_effective_policy_digest(self) -> str:
        cfg = self.config
        return config_effective_policy_digest(
            {
                "tenant_id": None,
                "governance": cfg.governance.model_dump(mode="json") if cfg.governance is not None else None,
                "dedup": cfg.dedup.model_dump(mode="json"),
                "pruning": cfg.pruning.model_dump(mode="json"),
                "read_only_scopes": sorted(cfg.read_only_scopes),
                "require_dream_agent_approval_for_untrusted_directives": (
                    cfg.require_dream_agent_approval_for_untrusted_directives
                ),
            }
        )

    def _dream_job(self, job_name: str, *, config: DreamConfig | None = None):
        if not job_name.strip():
            raise ValueError("job_name cannot be blank")
        active_config = config or self.config
        for job in active_config.jobs:
            if job.name == job_name:
                return job
        raise ValueError(f"unknown dream job: {job_name}")

    def _engine_for_config(self, config: DreamConfig, *, tenant_id: str | None = None) -> DreamEngine:
        # #158: the extractor and agent transport come from the OPERATION's tenant, not
        # from a process-global field that the last configure() call happened to set.
        extractor, agent_transport = self.transports_for_tenant(tenant_id)
        return DreamEngine(
            config=config,
            graph=self.graph,
            extractor=extractor,
            agent_transport=agent_transport,
            embedding_transport=self._embedding_transport,
            rollup_synthesis_transport=self.rollup_synthesis_transport,
            artifact_sources=self._artifact_sources,
        )

    def _runtime_policy(
        self,
        *,
        tenant_id: str | None,
        agent_id: str | None,
        scope: MemoryScope | None,
        dream_mode: str | None,
        motive: str | None,
        prompt_pack: str | None,
    ) -> EffectiveMemoryPolicy | None:
        if all(value is None for value in (tenant_id, agent_id, scope, dream_mode, motive, prompt_pack)):
            return None
        if self.control_plane is None:
            raise ValueError("control_plane is required for tenant/agent/scope runtime policy")
        if tenant_id is None:
            raise ValueError("tenant_id is required for tenant/agent/scope runtime policy")
        return self.resolve_policy(
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope=scope,
            dream_mode=dream_mode,
            motive=motive,
            prompt_pack=prompt_pack,
        )

    def _dream_job_status(self, *, job: Any, now: datetime) -> DreamJobStatus:
        last_run = self.graph.get_job_last_run(job.name)
        next_run = None if last_run is None else last_run + timedelta(seconds=job.cadence_seconds)
        seconds_until_due = 0 if next_run is None else max(0, int((next_run - now).total_seconds()))
        pending_episodes = self._pending_episode_count(job)
        eligible_scopes = self._eligible_scope_count(job)
        return DreamJobStatus(
            job_name=job.name,
            job_kind=job.kind,
            due=last_run is None or now >= next_run,
            cadence_seconds=job.cadence_seconds,
            max_items_per_run=job.max_items_per_run,
            last_run=last_run,
            next_run=next_run,
            seconds_until_due=seconds_until_due,
            scope=job.scope,
            instruction_set=job.instruction_set,
            agent_id=job.agent.agent_id,
            agent_name=job.agent.name,
            agent_scope=job.agent.scope,
            prompt_profile=job.prompt_profile,
            prompt_profile_version=job.prompt_profile_version,
            prompt_override_enabled=not job.prompt_override.is_empty,
            pending_episodes=pending_episodes,
            eligible_scopes=eligible_scopes,
        )

    def _pending_episode_count(self, job: Any) -> int:
        if job.kind.value != "formation":
            return 0
        count = 0
        for episode in self.graph.episodes():
            if not self._engine.episode_matches_job(episode=episode, job=job):
                continue
            count += 1
        return count

    def _eligible_scope_count(self, job: Any) -> int:
        if job.kind.value != "consolidation":
            return 0
        if job.scope is not None:
            return 1
        scopes: set[str] = set()
        for relationship in self.graph.relationships():
            if relationship.type == "MENTIONS":
                continue
            scope_key = relationship.properties.get("scope_key")
            if isinstance(scope_key, str) and scope_key.strip():
                scopes.add(scope_key)
        return len(scopes)

    def _dream_job_run_record(self, *, job_run: DreamJobRun, ran_at: datetime) -> DreamJobRunRecord:
        return DreamJobRunRecord(
            ran_at=ran_at,
            job_name=job_run.job_name,
            job_kind=job_run.job_kind,
            run_uuid=job_run.run_uuid,
            processed_episodes=job_run.processed_episodes,
            processed_scopes=job_run.processed_scopes,
            created_nodes=job_run.created_nodes,
            created_relationships=job_run.created_relationships,
            reinforced_relationships=job_run.reinforced_relationships,
            superseded_relationships=job_run.superseded_relationships,
            pruned_relationships=job_run.pruned_relationships,
            decision_count=job_run.decision_count,
        )
