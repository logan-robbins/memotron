from __future__ import annotations

import json
from collections.abc import (
    Callable as Callable,
)
from datetime import UTC, datetime
from datetime import timedelta as timedelta  # re-export: on the api_surface golden
from pathlib import Path
from typing import (
    TYPE_CHECKING as TYPE_CHECKING,
)
from typing import Any
from typing import (
    ClassVar as ClassVar,
)
from typing import (
    Literal as Literal,
)
from uuid import (
    uuid4 as uuid4,
)

# The `if TYPE_CHECKING:` block that used to sit here is gone: every annotation that
# needed it moved out with its method, so each mixin now carries its own scoped subset.
# Two corrections to what that block's comment claimed, both measured 2026-08-29:
#
#   * It said every module listed imports `client` back. Only ONE does --
#     `replay`, via replay -> memotron -> agent_memory -> client. The other five
#     (certification, context, session, runtime, redaction) are clean, so their
#     function-local runtime imports are deferred by convention, not necessity.
#     Hoisting those five is a legitimate later commit; hoisting `replay` is not,
#     until that chain is broken.
#   * None of these names was ever runtime-importable from `memotron.client`
#     (TYPE_CHECKING is false at runtime), which is why removing them here does not
#     touch the api_surface golden. The names below at module scope DO ship, and the
#     `X as X` form on the unused ones is what keeps them importable -- packages are
#     in the mypy strict tier, where `no_implicit_reexport` drops a plain import.
from memotron.agents import DreamAgentTransport

# --- surface preservation -------------------------------------------------------
# The 115 names below were importable from `memotron.client` before the split and
# are on the api_surface golden, but every one of them moved out with its mixin. The
# `X as X` form is load-bearing: packages sit in the mypy strict tier, where
# `no_implicit_reexport` makes a plain `import X` non-re-exporting, so the SDK would
# still typecheck here and break at the consumer.
#
# `uuid4` is re-exported for contract parity ONLY. Nothing patches
# `memotron.client.uuid4`, and nothing should: the call site lives in `_ingest`,
# so a monkeypatch here would bind a name no code reads and silently intercept
# nothing. tests/test_storage_backend_parity.py:800 patches the defining submodules
# directly, which is the correct form -- the models split learned this the hard way.
from memotron.client._archive import ArchiveMixin
from memotron.client._artifacts import ArtifactMixin
from memotron.client._certification import CertificationMixin
from memotron.client._common import (
    _AWARE_MAX as _AWARE_MAX,
)
from memotron.client._common import (
    _AWARE_MIN as _AWARE_MIN,
)
from memotron.client._crypto import CryptoGovernanceMixin
from memotron.client._governance import EntityGovernanceMixin
from memotron.client._ingest import IngestMixin
from memotron.client._lifecycle import MemoryLifecycleMixin
from memotron.client._outcomes import OutcomeMixin
from memotron.client._profile import ProfileRenderMixin
from memotron.client._retrieval import RetrievalMixin
from memotron.client._runtime import RuntimeMixin
from memotron.client._timeline import TruthTimelineMixin
from memotron.client._views import GraphViewMixin
from memotron.coherence import ArtifactSource, GraphArtifactSource
from memotron.coherence import (
    CoherenceScanner as CoherenceScanner,
)
from memotron.config import (
    CoherencePolicy,
    DreamConfig,
    EffectiveMemoryPolicy,
    ErasureBehavior,
    MemoryBank,
    MemoryControlPlane,
    default_config,
)
from memotron.config import (
    ProfilePolicy as ProfilePolicy,
)
from memotron.config._dream_config import (
    DreamJob as DreamJob,
)
from memotron.config._governance import (
    GovernancePolicy as GovernancePolicy,
)
from memotron.config._motive import (
    Motive as Motive,
)
from memotron.config._retrieval import (
    RetrievalPolicy as RetrievalPolicy,
)
from memotron.crypto import (
    ContentKeyUnavailableError as ContentKeyUnavailableError,
)
from memotron.crypto import (
    content_commitment as content_commitment,
)
from memotron.crypto import (
    is_sealed_content as is_sealed_content,
)
from memotron.crypto import seal_content
from memotron.dreaming import DreamEngine
from memotron.embedding import EmbeddingTransport
from memotron.embedding import LocalEmbeddingTransport as LocalEmbeddingTransport
from memotron.embedding import (
    cosine_similarity as cosine_similarity,
)
from memotron.embedding import (
    stored_vector_in_active_space as stored_vector_in_active_space,
)
from memotron.epochs import (
    AdoptResult as AdoptResult,
)
from memotron.epochs import EpisodeSelector as EpisodeSelector
from memotron.epochs import (
    EpochDiff as EpochDiff,
)
from memotron.epochs import (
    RedreamOverrides as RedreamOverrides,
)
from memotron.epochs import (
    RedreamResult as RedreamResult,
)
from memotron.epochs import (
    RollbackResult as RollbackResult,
)
from memotron.epochs import adopt_epoch as adopt_epoch
from memotron.epochs import (
    branch_and_recompute as branch_and_recompute,
)
from memotron.epochs import build_session_digest as build_session_digest
from memotron.epochs import diff_epochs as diff_epochs
from memotron.epochs import (
    open_shadow_store as open_shadow_store,
)
from memotron.epochs import rollback_epoch as rollback_epoch
from memotron.erasure import (
    ErasureCertificate as ErasureCertificate,
)
from memotron.erasure import (
    issue_erasure_certificate as issue_erasure_certificate,
)
from memotron.erasure import (
    verify_erasure_certificate as verify_erasure_certificate,
)
from memotron.extraction import ExtractionTransport, InstructionalExtractor
from memotron.health import (
    DEFAULT_ELIGIBLE_TYPES as DEFAULT_ELIGIBLE_TYPES,
)
from memotron.health import (
    compute_memory_health as compute_memory_health,
)
from memotron.models import (
    CoherenceIncidentStatus as CoherenceIncidentStatus,
)
from memotron.models import (
    DreamJobKind,
    DreamJobRun,
    Episode,
    EvidenceEpisode,
    MemoryScope,
    RelationshipStatus,
)
from memotron.models import (
    EpisodeType as EpisodeType,
)
from memotron.models._artifacts import (
    ArtifactContributionProjection as ArtifactContributionProjection,
)
from memotron.models._artifacts import (
    CoherenceRepairMonitor as CoherenceRepairMonitor,
)
from memotron.models._artifacts import (
    FrequencyAuthorityEvaluation as FrequencyAuthorityEvaluation,
)
from memotron.models._artifacts import (
    FrequencyAuthorityTrial as FrequencyAuthorityTrial,
)
from memotron.models._artifacts import (
    PersistentArtifact as PersistentArtifact,
)
from memotron.models._artifacts import (
    ResolvedBehaviorContract as ResolvedBehaviorContract,
)
from memotron.models._artifacts import (
    ResolvedBehaviorDirective as ResolvedBehaviorDirective,
)
from memotron.models._coherence import (
    CoherenceDisambiguationRequest as CoherenceDisambiguationRequest,
)
from memotron.models._coherence import (
    CoherenceIncident as CoherenceIncident,
)
from memotron.models._coherence import (
    CoherenceIncidentResolution as CoherenceIncidentResolution,
)
from memotron.models._coherence import (
    CoherenceRemediationReport as CoherenceRemediationReport,
)
from memotron.models._coherence import (
    CoherenceReport as CoherenceReport,
)
from memotron.models._coherence import (
    EntityAliasProposal as EntityAliasProposal,
)
from memotron.models._coherence import (
    EntityAliasResolution as EntityAliasResolution,
)
from memotron.models._coherence import (
    QuarantinedCandidate as QuarantinedCandidate,
)
from memotron.models._coherence import (
    QuarantineResolution as QuarantineResolution,
)
from memotron.models._coherence import (
    ScopeRemediationResult as ScopeRemediationResult,
)
from memotron.models._coherence import (
    SupersessionReviewItem as SupersessionReviewItem,
)
from memotron.models._coherence import (
    SupersessionReviewResolution as SupersessionReviewResolution,
)
from memotron.models._enums import (
    ArchivedMatchDisposition as ArchivedMatchDisposition,
)
from memotron.models._enums import (
    ArtifactClass as ArtifactClass,
)
from memotron.models._enums import (
    ClaimMode as ClaimMode,
)
from memotron.models._enums import (
    CoherenceIncidentKind as CoherenceIncidentKind,
)
from memotron.models._enums import (
    MemoryType as MemoryType,
)
from memotron.models._enums import (
    OutcomeVerdict as OutcomeVerdict,
)
from memotron.models._enums import (
    QuarantineStatus as QuarantineStatus,
)
from memotron.models._enums import (
    UseEventKind as UseEventKind,
)
from memotron.models._events import (
    MemoryUtilityProjection as MemoryUtilityProjection,
)
from memotron.models._events import (
    OutcomeEvent as OutcomeEvent,
)
from memotron.models._events import (
    RetrievalNegativeSpaceEntry as RetrievalNegativeSpaceEntry,
)
from memotron.models._events import (
    UseEvent as UseEvent,
)
from memotron.models._events import (
    session_boundary_for_task_run as session_boundary_for_task_run,
)
from memotron.models._graph import (
    ExtractedMemory as ExtractedMemory,
)
from memotron.models._graph import (
    GraphRelationship as GraphRelationship,
)
from memotron.models._health import (
    MemoryEvolutionFact as MemoryEvolutionFact,
)
from memotron.models._health import (
    MemoryEvolutionProof as MemoryEvolutionProof,
)
from memotron.models._health import (
    MemoryEvolutionSignal as MemoryEvolutionSignal,
)
from memotron.models._health import (
    MemoryHealthReport as MemoryHealthReport,
)
from memotron.models._mutations import (
    CorrectMemoryResult as CorrectMemoryResult,
)
from memotron.models._mutations import (
    ForgetMemoryResult as ForgetMemoryResult,
)
from memotron.models._mutations import (
    MemoryVisibilityResult as MemoryVisibilityResult,
)
from memotron.models._mutations import (
    PinMemoryResult as PinMemoryResult,
)
from memotron.models._mutations import (
    TruthTimelineEntry as TruthTimelineEntry,
)
from memotron.models._results import (
    AddArtifactResult as AddArtifactResult,
)
from memotron.models._results import (
    AddContextResult as AddContextResult,
)
from memotron.models._results import (
    AddEpisodeResult as AddEpisodeResult,
)
from memotron.models._results import (
    AddMemoryResult as AddMemoryResult,
)
from memotron.models._results import (
    AddSessionResult as AddSessionResult,
)
from memotron.models._results import (
    ArchivedMatchReport as ArchivedMatchReport,
)
from memotron.models._results import (
    ArchivedMemoryMatch as ArchivedMemoryMatch,
)
from memotron.models._results import (
    ConversationTurn as ConversationTurn,
)
from memotron.models._results import (
    RestoreArchivedMemoryResult as RestoreArchivedMemoryResult,
)
from memotron.models._results import (
    SearchResult as SearchResult,
)
from memotron.models._results import (
    SearchResults as SearchResults,
)
from memotron.models._runs import (
    DreamDecisionRecord as DreamDecisionRecord,
)
from memotron.models._runs import (
    DreamJobRunRecord as DreamJobRunRecord,
)
from memotron.models._runs import (
    DreamJobStatus as DreamJobStatus,
)
from memotron.models._runs import (
    DreamRunResult as DreamRunResult,
)
from memotron.models._views import (
    EntityNeighborhoodEdge as EntityNeighborhoodEdge,
)
from memotron.models._views import (
    KnowledgeGraphEdge as KnowledgeGraphEdge,
)
from memotron.models._views import (
    KnowledgeGraphNode as KnowledgeGraphNode,
)
from memotron.models._views import (
    KnowledgeGraphView as KnowledgeGraphView,
)
from memotron.models._views import (
    MemoryContextItem as MemoryContextItem,
)
from memotron.models._views import (
    MemoryEvidence as MemoryEvidence,
)
from memotron.models._views import (
    MemoryProfile as MemoryProfile,
)
from memotron.models._views import (
    MemoryProfileFact as MemoryProfileFact,
)
from memotron.multimodal import (
    Artifact as Artifact,
)
from memotron.multimodal import (
    ArtifactProvenance as ArtifactProvenance,
)
from memotron.multimodal import (
    LocalMultimodalNormalizer as LocalMultimodalNormalizer,
)
from memotron.multimodal import (
    MultimodalNormalizer as MultimodalNormalizer,
)
from memotron.receipts import ReceiptDecisionType, ReceiptRun, payload_digest
from memotron.retrieval import (
    CandidateOrigin as CandidateOrigin,
)
from memotron.retrieval import (
    ScoredCandidate as ScoredCandidate,
)
from memotron.retrieval import (
    TemporalAuthorityCandidate as TemporalAuthorityCandidate,
)
from memotron.retrieval import (
    build_retrieval_contract as build_retrieval_contract,
)
from memotron.retrieval import (
    demote_contradicted_same_slot as demote_contradicted_same_slot,
)
from memotron.retrieval import (
    effective_type_weight as effective_type_weight,
)
from memotron.retrieval import (
    lexical_overlap as lexical_overlap,
)
from memotron.retrieval import (
    recency_decay as recency_decay,
)
from memotron.retrieval import (
    relevance_score as relevance_score,
)
from memotron.retrieval import (
    rerank_score as rerank_score,
)
from memotron.retrieval import (
    resolve_effective_utility_weight as resolve_effective_utility_weight,
)
from memotron.retrieval import (
    retrieval_contract_digest as retrieval_contract_digest,
)
from memotron.retrieval import (
    scope_priority as scope_priority,
)
from memotron.retrieval import (
    use_need as use_need,
)
from memotron.storage import (
    StorageBackend,
    create_storage_backend,
    normalize_key,
    open_storage,
)
from memotron.storage.receipts import (
    MemoryReceipt as MemoryReceipt,
)
from memotron.storage.receipts import (
    NegativeSpaceEntry as NegativeSpaceEntry,
)
from memotron.storage.receipts import (
    RunCheckpoint as RunCheckpoint,
)
from memotron.storage.receipts import (
    config_effective_policy_digest as config_effective_policy_digest,
)
from memotron.synthesis import SynthesisTransport


class Memotron(
    IngestMixin,
    RuntimeMixin,
    OutcomeMixin,
    CertificationMixin,
    ArtifactMixin,
    RetrievalMixin,
    GraphViewMixin,
    MemoryLifecycleMixin,
    EntityGovernanceMixin,
    CryptoGovernanceMixin,
    TruthTimelineMixin,
    ArchiveMixin,
    ProfileRenderMixin,
):
    """SDK-style client for direct scoped memories and offline memory formation."""

    def __init__(
        self,
        *,
        config: DreamConfig | None = None,
        graph_path: str | Path = ".memotron/graph.sqlite",
        storage: StorageBackend | None = None,
        extraction_transport: ExtractionTransport | None = None,
        dream_agent_transport: DreamAgentTransport | None = None,
        embedding_transport: EmbeddingTransport | None = None,
        rollup_synthesis_transport: SynthesisTransport | None = None,
        memory_bank: MemoryBank | None = None,
        control_plane: MemoryControlPlane | None = None,
        authorized_scope_keys: set[str] | frozenset[str] | None = None,
    ) -> None:
        """Create a Memotron client.

        Parameters
        ----------
        authorized_scope_keys:
            WS-19 T21 core-layer scope guard (defense in depth for embedded
            single-tenant SDK deployments).  When set, EVERY public read/write
            entry that takes a scope fails fast with a uniform ValueError
            naming the scope whenever ``scope.key`` (or any key of a
            multi-scope call) is outside this set — an in-process caller can
            no longer silently cross scopes even though it holds the raw
            client.  ``None`` (default) preserves SDK behaviour byte-for-byte.
            The ``AgentMemoryPlatform`` facade deliberately does NOT set it on
            its own omni-tenant client: facade authorization is already
            enforced per call against the calling agent's principal.
        rollup_synthesis_transport:
            WS-18 T19: optional LLM transport for derived-content synthesis.
            Configuring it IS the opt-in: consolidation then writes faithful
            LLM rollup summaries, every one of which must pass the deterministic
            entailment gate before it may become rollup text.  The active
            ``ConsolidationSynthesisProfile.model_identifier`` is only an
            override of the recorded provenance string, defaulting to this
            transport's ``identifier``.  The agent platform reuses the same
            transport as the session outcome judge.  ``None`` (default) keeps
            the deterministic structural rollup label and skips the judge.
        storage:
            An already-constructed :class:`~memotron.storage.StorageBackend`.
            When provided it wins over both ``config.storage`` and
            ``graph_path``.  When None, the backend is built from
            ``config.storage`` (engine selection by configuration, including
            running the Operational Store and the Memory Graph on different
            engines), falling back to the default local backend at
            ``graph_path``.
        memory_bank:
            WS-3: Optional MemoryBank to attach to the config.  When provided,
            it overrides any bank already present on the supplied config.  When
            None, the config's bank (if any) is used unchanged.
        control_plane:
            Optional multi-tenant policy resolver.  When provided, runtime
            dreaming methods can resolve tenant/agent/scope policy before
            running jobs.  ``config`` and ``memory_bank`` must be configured on
            the control plane to avoid ambiguous sources of truth.
        """
        if control_plane is not None and config is not None:
            raise ValueError("pass config through control_plane.base_config when using control_plane")
        if control_plane is not None and memory_bank is not None:
            raise ValueError("pass memory_bank through tenant policies when using control_plane")
        base_config = control_plane.base_config if control_plane is not None else (config or default_config())
        if memory_bank is not None:
            # Attach the supplied bank, overriding whatever the config had.
            self.config = base_config.model_copy(update={"memory_bank": memory_bank})
        else:
            self.config = base_config
        self.control_plane = control_plane
        # WS-19 T21: optional core-layer scope guard.  Normalized once; every
        # public scope-taking entry funnels through _require_authorized_scope.
        if authorized_scope_keys is None:
            self.authorized_scope_keys: frozenset[str] | None = None
        else:
            normalized_keys = frozenset(str(key).strip() for key in authorized_scope_keys)
            if not normalized_keys or "" in normalized_keys:
                raise ValueError("authorized_scope_keys must be a non-empty set of non-blank scope keys")
            self.authorized_scope_keys = normalized_keys
        if storage is not None:
            self.graph: StorageBackend = storage
        elif self.config.storage is not None:
            # #123. THIS LINE PASSED NO key_manager, and that was the defect. Every
            # Postgres store therefore fell to `LocalKeyManager.ephemeral()` -- a KEK that
            # dies with the process -- so sealed tenant credentials were unreadable after any
            # restart and by every other replica, while status still reported them configured.
            #
            # `key_manager_from_env` returns None when no durable key is configured, which
            # deliberately leaves the existing fail-closed guard in charge rather than
            # inventing a key: SQLite is unaffected, and Postgres still refuses to build
            # unless the operator has explicitly accepted an ephemeral KEK.
            self.graph = create_storage_backend(self.config.storage)
        else:
            # The env KEK applies here too. It did NOT, and that was a defect found by asking
            # what happens when an operator configures a key on the engine `latest` actually
            # runs today: SQLite quietly minted its own `.kek` sibling file and used a
            # DIFFERENT key, so someone verifying "did my KEK take effect?" on latest would
            # see everything working and conclude it had.
            #
            # Neither call resolves the KEK itself -- `create_storage_backend` does, for every
            # caller. Resolving it HERE was the first attempt and it was wrong: `adoption` and
            # `runtime` open the same store without going through this constructor, so the CLI
            # sealed under the sibling key while this path read under the env key, and status
            # reported a credential the serving process could not decrypt.
            self.graph = open_storage(graph_path)
        self._extractor = InstructionalExtractor(transport=extraction_transport)
        self._dream_agent_transport = dream_agent_transport
        self._embedding_transport = embedding_transport
        # WS-18 T19: public so the agent platform can reuse the same transport
        # for the session outcome judge (one credential path, one identifier).
        self.rollup_synthesis_transport = rollup_synthesis_transport
        # WS-10: non-memory persistent artifacts (skills, files) registered for the
        # cross-artifact coherence cycle.  Shared by reference with every engine.
        self._artifact_sources: list[ArtifactSource] = [GraphArtifactSource(self.graph)]
        self._engine = DreamEngine(
            config=self.config,
            graph=self.graph,
            extractor=self._extractor,
            agent_transport=dream_agent_transport,
            embedding_transport=embedding_transport,
            rollup_synthesis_transport=rollup_synthesis_transport,
            artifact_sources=self._artifact_sources,
        )

    # ------------------------------------------------------------------
    # WS-9: Multimodal artifact ingestion
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-9: Hybrid / semantic search (opt-in, additive)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-26: the derivation DAG — runs, epochs, and re-dream
    #
    # New, additive surface (memotron.epochs) — none of these methods
    # touch any pre-existing client method. Each wraps one epochs.py entry
    # point with this client's own extractor/transports and the WS-19 T21
    # scope guard, exactly like the dream-job methods above it.
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-11: Replayable receipt ledger surface (PATENT_REPLAY_RECEIPTS_SPEC.md)
    # ------------------------------------------------------------------

    @staticmethod
    def _agent_may_view_relationship(properties: dict[str, Any], reader_agent_id: str | None) -> bool:
        """WS-19 T22: per-memory agent allowlist (``visibility_agents``).

        A row WITHOUT the property keeps today's scope-default visibility.  A
        row WITH a non-empty allowlist is visible only to readers whose
        ``reader_agent_id`` is in the list — fail-closed for agents.  A
        ``None`` reader is the operator / SDK-owner context and sees
        everything: the restriction is agent-plane only, operator surfaces
        (admin, raw SDK reads without a reader identity) are unaffected.
        """
        allowlist = properties.get("visibility_agents")
        if not isinstance(allowlist, (list, tuple)) or not allowlist:
            return True
        if reader_agent_id is None:
            return True
        return reader_agent_id in {str(agent) for agent in allowlist}

    # ------------------------------------------------------------------
    # WS-17 T16b: entity alias registry — backfill + human adjudication
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-16 T14: human adjudication of gate-parked supersession challengers
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-24: quarantine — the abstain path's operator surface
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # WS-7: Governance — crypto-shredding, PII redaction, key management
    # ------------------------------------------------------------------

    def _store_episode(self, episode: Episode) -> Episode:
        """WS-12: the single episode-persistence choke point.

        Under CRYPTO_SHRED governance the raw evidence body and the episode name
        (both content) are stored ONLY sealed under the scope DEK — provisioned
        here if needed — so destroying the key renders the stored evidence
        unrecoverable.  Returns the episode exactly as persisted so callers and
        receipts always reference the stored representation.
        """
        governance = self._ingest_governance(dict(episode.metadata))
        if governance is not None and governance.erasure_behavior == ErasureBehavior.CRYPTO_SHRED:
            key = self.graph.get_or_create_governance_key(episode.scope.key)
            episode = episode.model_copy(
                update={
                    "body": seal_content(episode.body, key),
                    "name": seal_content(episode.name, key),
                }
            )
        self.graph.add_episode(episode)
        return episode

    def _require_authorized_scope(self, *scopes: MemoryScope | None) -> None:
        """WS-19 T21: fail fast when a scope is outside ``authorized_scope_keys``.

        One shared choke point for the core-layer scope guard.  ``None``
        entries are skipped (optional-scope parameters); a ``None`` guard set
        (the default) is a no-op, preserving SDK behaviour byte-for-byte.
        Row-addressed APIs (memory_evidence, forget/correct/redact) pass the
        RESOLVED row scope so an out-of-set row cannot be reached by omitting
        the scope argument.
        """
        if self.authorized_scope_keys is None:
            return
        for scope in scopes:
            if scope is None:
                continue
            if scope.key not in self.authorized_scope_keys:
                raise ValueError(
                    f"scope {scope.key!r} is outside this client's authorized_scope_keys; "
                    "this Memotron client is scope-guarded (WS-19 T21)"
                )

    def _require_explicit_authorized_scope(self, scope: MemoryScope | None, *, operation: str) -> None:
        """WS-19 T21: cross-scope reads/runs must name a scope under the guard.

        Some entries treat ``scope=None`` as "every scope in the store"
        (run_checkpoints, coherence listings, remediation, due-dream runs).
        A scope-guarded client fails those closed instead of silently
        widening past its authorized set.
        """
        if self.authorized_scope_keys is None:
            return
        if scope is None:
            raise ValueError(
                f"{operation} requires an explicit scope on a scope-guarded client "
                "(authorized_scope_keys is set; scope=None would span every scope)"
            )
        self._require_authorized_scope(scope)

    def _check_scope_not_read_only(self, scope: MemoryScope) -> None:
        """WS-7: Fail fast if the scope is in config.read_only_scopes.

        Raises ValueError with a clear message.  A no-op when read_only_scopes is empty
        (the default), preserving full backward compatibility.

        WS-19 T21: doubles as the shared write-plane choke point for the
        core-layer scope guard — every ingestion write (add_memory,
        add_context, add_artifact(s), add_episode, add_episode_bulk,
        add_session) already funnels through here.
        """
        self._require_authorized_scope(scope)
        if scope.key in self.config.read_only_scopes:
            raise ValueError(
                f"scope {scope.key!r} is read-only (in config.read_only_scopes). "
                "Writes to this scope are not permitted."
            )

    def _chunk_context(self, content: str, max_chars: int) -> list[str]:
        chunks: list[str] = []
        cursor = 0
        content_length = len(content)
        while cursor < content_length:
            end = min(cursor + max_chars, content_length)
            if end < content_length:
                boundary = self._best_context_boundary(content, cursor=cursor, end=end)
                if boundary > cursor:
                    end = boundary
            chunks.append(content[cursor:end])
            cursor = end
        return chunks

    def _best_context_boundary(self, content: str, *, cursor: int, end: int) -> int:
        minimum_boundary = cursor + max(1, (end - cursor) // 2)
        candidates: list[int] = []
        for delimiter in ("\n\n", "\n", ". ", "? ", "! "):
            index = content.rfind(delimiter, cursor + 1, end)
            boundary = index + len(delimiter)
            if index >= 0 and boundary >= minimum_boundary:
                candidates.append(boundary)
        if candidates:
            return max(candidates)
        return end

    def _context_episode_name(
        self,
        *,
        name: str,
        scope: MemoryScope,
        chunk_index: int,
        chunk_count: int,
    ) -> str:
        return f"{name.strip()} [{scope.key}] chunk {chunk_index + 1}/{chunk_count}"

    async def _run_job_and_record(
        self,
        *,
        job_name: str,
        now: datetime,
        engine: DreamEngine | None = None,
        config: DreamConfig | None = None,
        effective_policy: EffectiveMemoryPolicy | None = None,
    ) -> DreamJobRun:
        effective_engine = engine or self._engine
        job = self._dream_job(job_name, config=config)
        job_run = await effective_engine.run_job(
            job=job,
            episodes=self.graph.episodes(),
            now=now,
            effective_policy=effective_policy,
        )
        self.graph.record_dream_job_run(self._dream_job_run_record(job_run=job_run, ran_at=now))
        return job_run

    def _receipt_utility_auto_enabled(
        self,
        *,
        scope: MemoryScope,
        event_volume: int,
        floor: int,
        resolved_weight: float,
        now: datetime,
    ) -> None:
        """WS-28 T2: receipt the auto-enable flip the moment a scope's
        measured event volume crosses ``utility_weight_auto_floor_events``
        and the stage-6 rerank starts weighing ``use_need`` for that scope's
        searches.  Fires per occurrence (like ``USE_EVENT_RECORDED``), not
        once per scope, so the ledger always names the exact effective
        policy behind a given ranking."""
        run = self._begin_operator_run(job_name="retrieval_utility_auto_enable", scope_key=scope.key)
        payload = {
            "scope_key": scope.key,
            "event_volume": event_volume,
            "floor": floor,
            "resolved_utility_weight": resolved_weight,
        }
        self.graph.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.RETRIEVAL_UTILITY_AUTO_ENABLED,
            decision_reason=f"event_volume={event_volume}:floor={floor}",
            decision_result="recorded",
            scope_key=scope.key,
            created_at=now,
            event_payload=json.dumps(payload, sort_keys=True, separators=(",", ":")),
            event_payload_digest=payload_digest(payload),
        )
        self._checkpoint_operator_run(run)

    def _emit_receipt(
        self,
        receipt_run: ReceiptRun,
        *,
        decision_type: ReceiptDecisionType,
        decision_reason: str,
        decision_result: str,
        episode: Episode | None = None,
        now: datetime | None = None,
        **fields: object,
    ):
        """The one place this class reaches into DreamEngine for receipting.

        `_emit_receipt` lives on the engine because that is where the payload
        defaulting lives, and `Memotron` legitimately needs it: every operator
        write -- forget, pin, correct, crypto_shred, alias resolution -- is a
        receipted decision exactly like a formation one, and duplicating the
        defaulting here would be the worse option.

        What was wrong was doing it 33 times. `self._engine._emit_receipt(...)`
        reaches through one object into another's private method, so a signature
        change on the engine broke 33 call sites and no encapsulation check could
        see any of them. Now it breaks one.

        Not a fix for the coupling -- a fix for its ARITY. This class still depends
        on an engine private; it now says so once, here, instead of scattering the
        dependency through nineteen public methods. Eight further reaches remain and
        are deliberately left visible rather than papered over, because each is its
        own design question: `_secret_reference_metadata` and
        `_reject_raw_secret_memory` (2 each), `_rebridgeable_row` (2),
        `_supersede_target_relationships` (1), `_embedding_transport` (1), and
        `self._extractor._transport` (2).
        """
        return self._engine._emit_receipt(
            receipt_run,
            decision_type=decision_type,
            decision_reason=decision_reason,
            decision_result=decision_result,
            episode=episode,
            now=now,
            **fields,
        )

    def _checkpoint_operator_run(self, run: ReceiptRun) -> None:
        # Empty operator runs (e.g. a materialize that no-op'd) are not checkpointed.
        if run.next_event_index == 0:
            return
        self.graph.receipts.checkpoint(run)

    def _scope_memory_relationships(self, scope: MemoryScope):
        return [
            relationship
            for relationship in self.graph.relationships()
            if relationship.type != "MENTIONS" and relationship.properties.get("scope_key") == scope.key
        ]

    def _begin_operator_run(self, *, job_name: str, scope_key: str) -> ReceiptRun:
        """WS-11: start a 1-receipt operator run on the shared ledger ([0021])."""
        return self.graph.receipts.begin_run(
            run_kind="operator",
            job_name=job_name,
            scope_key=scope_key,
            effective_policy_digest=self._operator_effective_policy_digest(),
        )

    def _coherence_policy_for_scope(self, scope: MemoryScope) -> CoherencePolicy:
        for job in self.config.jobs:
            if job.kind == DreamJobKind.COHERENCE and (job.scope is None or job.scope == scope):
                return job.coherence_policy
        return CoherencePolicy()

    def _relationship_status(self, raw_status: Any) -> RelationshipStatus:
        if raw_status is None:
            return RelationshipStatus.ACTIVE
        return RelationshipStatus(str(raw_status))

    def _relationship_episode_uuids(self, properties: dict[str, Any]) -> list[str]:
        episode_uuids = properties.get("episode_uuids")
        if isinstance(episode_uuids, list):
            return [str(episode_uuid) for episode_uuid in episode_uuids]
        return [str(properties["episode_uuid"])]

    def _relationship_metadata(self, properties: dict[str, Any]) -> dict[str, Any]:
        metadata = properties.get("metadata", {})
        if isinstance(metadata, dict):
            return dict(metadata)
        return {}

    def _node_search_text(self, properties: dict[str, Any]) -> str:
        # name_embedding / embedding_identifier (WS-17 T16b) are vector-plane
        # provenance, not searchable text — a stringified float list would
        # poison lexical overlap for every node it is stamped on.
        reserved = {
            "name",
            "scope_kind",
            "scope_id",
            "scope_key",
            "graph_key",
            "name_embedding",
            "embedding_identifier",
        }
        searchable_values = [
            str(value) for key, value in properties.items() if key not in reserved and value is not None
        ]
        return " ".join(searchable_values)

    def _optional_string(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def _relationship_scope(self, properties: dict[str, Any]) -> MemoryScope:
        scope_kind = properties.get("scope_kind")
        scope_id = properties.get("scope_id")
        if not isinstance(scope_kind, str) or not isinstance(scope_id, str):
            raise ValueError("relationship is missing scope_kind or scope_id")
        return MemoryScope(kind=scope_kind, scope_id=scope_id)

    def _forget_valid_to(
        self,
        *,
        status: RelationshipStatus,
        valid_to: datetime | None,
        pruned_at: datetime,
    ) -> datetime | None:
        if status == RelationshipStatus.ACTIVE:
            if valid_to is None:
                return pruned_at
            return min(valid_to, pruned_at)
        return valid_to

    def _correction_valid_to(
        self,
        *,
        target_valid_to: datetime | None,
        correction_time: datetime,
    ) -> datetime:
        if target_valid_to is None:
            return correction_time
        return min(target_valid_to, correction_time)

    def _relationship_entity_label(self, labels: tuple[str, ...]) -> str:
        for label in labels:
            if label not in {"Episode", "Agent", "User", "Customer"}:
                return label
        return "Entity"

    def _same_fact(
        self,
        *,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        corrected_subject: str,
        corrected_predicate: str,
        corrected_object: str,
        corrected_relationship_type: str,
    ) -> bool:
        return (
            normalize_key(subject) == normalize_key(corrected_subject)
            and normalize_key(predicate) == normalize_key(corrected_predicate)
            and normalize_key(object_value) == normalize_key(corrected_object)
            and relationship_type == corrected_relationship_type
        )

    def _materialized_memory_relationship(
        self,
        *,
        scope: MemoryScope,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        episode_uuid: str,
    ):
        candidates = []
        for relationship in self.graph.relationships():
            if relationship.type != relationship_type:
                continue
            if relationship.properties.get("scope_key") != scope.key:
                continue
            if normalize_key(str(relationship.properties.get("predicate", ""))) != normalize_key(predicate):
                continue
            stored_object = str(self.graph.reveal(scope.key, relationship.properties.get("object", "")))
            if normalize_key(stored_object) != normalize_key(object_value):
                continue
            source_name = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.source_uuid).properties["name"])
            )
            target_name = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.target_uuid).properties["name"])
            )
            if normalize_key(source_name) != normalize_key(subject):
                continue
            if normalize_key(target_name) != normalize_key(object_value):
                continue
            if episode_uuid not in self._relationship_episode_uuids(relationship.properties):
                continue
            candidates.append(relationship)
        if not candidates:
            candidates = [
                relationship
                for relationship in self.graph.relationships()
                if relationship.type == relationship_type
                and relationship.properties.get("scope_key") == scope.key
                and normalize_key(str(relationship.properties.get("predicate", ""))) == normalize_key(predicate)
                and episode_uuid in self._relationship_episode_uuids(relationship.properties)
            ]
        if not candidates:
            raise RuntimeError("client-managed memory produced no relationship disposition")
        candidates.sort(key=lambda relationship: relationship.created_at, reverse=True)
        return candidates[0]

    def _corrected_relationship(
        self,
        *,
        scope: MemoryScope,
        subject: str,
        predicate: str,
        object_value: str,
        relationship_type: str,
        correction_episode_uuid: str,
    ):
        candidates = []
        for relationship in self.graph.relationships():
            if relationship.type != relationship_type:
                continue
            if relationship.properties.get("scope_key") != scope.key:
                continue
            if normalize_key(str(relationship.properties.get("predicate", ""))) != normalize_key(predicate):
                continue
            stored_object = str(self.graph.reveal(scope.key, relationship.properties.get("object", "")))
            if normalize_key(stored_object) != normalize_key(object_value):
                continue
            source_name = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.source_uuid).properties["name"])
            )
            target_name = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.target_uuid).properties["name"])
            )
            if normalize_key(source_name) != normalize_key(subject):
                continue
            if normalize_key(target_name) != normalize_key(object_value):
                continue
            if correction_episode_uuid not in self._relationship_episode_uuids(relationship.properties):
                continue
            candidates.append(relationship)
        if not candidates:
            raise ValueError("corrected relationship was not materialized")
        candidates.sort(key=lambda relationship: relationship.created_at, reverse=True)
        return candidates[0]

    def _evidence_episode(self, episode: Episode) -> EvidenceEpisode:
        # WS-12: decrypt-on-read — sealed evidence reveals while the scope DEK
        # lives and resolves to the shredded placeholder afterwards.
        return EvidenceEpisode(
            uuid=episode.uuid,
            name=str(self.graph.reveal(episode.scope.key, episode.name)),
            body=str(self.graph.reveal(episode.scope.key, episode.body)),
            source=episode.source,
            source_description=episode.source_description,
            scope=episode.scope,
            reference_time=episode.reference_time,
            created_at=episode.created_at,
            metadata=episode.metadata,
            instruction_set=episode.instruction_set,
        )

    def _normalized_relationship_types(self, relationship_types: set[str] | None) -> set[str] | None:
        if relationship_types is None:
            return None
        normalized: set[str] = set()
        for relationship_type in relationship_types:
            normalized.add(self._normalized_relationship_type(relationship_type))
        return normalized

    def _normalized_relationship_type(self, relationship_type: str) -> str:
        normalized_type = relationship_type.strip().replace(" ", "_").upper()
        if not normalized_type:
            raise ValueError("relationship_type cannot be blank")
        return normalized_type

    def _edge_direction(self, *, source_matches: bool, target_matches: bool) -> str:
        if source_matches and target_matches:
            return "self"
        if source_matches:
            return "outgoing"
        return "incoming"

    def _relationship_status_rank(self, status: RelationshipStatus) -> int:
        ranks = {
            RelationshipStatus.ACTIVE: 0,
            RelationshipStatus.SUPERSEDED: 1,
            RelationshipStatus.PRUNED: 2,
        }
        return ranks[status]

    def _datetime_sort_value(self, value: datetime | None) -> float:
        return value.timestamp() if value is not None else float("-inf")

    # ------------------------------------------------------------------
    # WS-5: Budget-aware helpers
    # ------------------------------------------------------------------

    """Canonical priority order for budget allocation — mirrors typed renderer."""

    def _relationship_is_visible(
        self,
        *,
        status: RelationshipStatus,
        valid_from: datetime | None,
        valid_to: datetime | None,
        as_of: datetime | None,
        include_statuses: set[RelationshipStatus] | None,
    ) -> bool:
        if as_of is None:
            allowed_statuses = include_statuses or {RelationshipStatus.ACTIVE}
            if status not in allowed_statuses:
                return False
            if status == RelationshipStatus.ACTIVE:
                return self._relationship_window_contains(
                    valid_from=valid_from,
                    valid_to=valid_to,
                    instant=datetime.now(UTC),
                )
            return True
        allowed_statuses = include_statuses or {
            RelationshipStatus.ACTIVE,
            RelationshipStatus.SUPERSEDED,
            RelationshipStatus.PRUNED,
        }
        if status not in allowed_statuses:
            return False
        return self._relationship_window_contains(
            valid_from=valid_from,
            valid_to=valid_to,
            instant=as_of,
        )

    def _relationship_window_contains(
        self,
        *,
        valid_from: datetime | None,
        valid_to: datetime | None,
        instant: datetime,
    ) -> bool:
        if valid_from is not None and valid_from > instant:
            return False
        return not (valid_to is not None and valid_to <= instant)
