from __future__ import annotations

from memotron.models._artifacts import (
    ArtifactContributionProjection as ArtifactContributionProjection,
)
from memotron.models._artifacts import (
    ArtifactDirective as ArtifactDirective,
)
from memotron.models._artifacts import (
    CoherenceRepairMonitor as CoherenceRepairMonitor,
)
from memotron.models._artifacts import (
    Directive as Directive,
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

# Re-exported so `from memotron.models import ScopeKind` keeps resolving
# exactly as it did when these lived in this file. The API-surface golden
# (tests/api_surface.golden.txt) lists every one and reddens if any goes missing.
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
    CoherenceIncidentStatus as CoherenceIncidentStatus,
)
from memotron.models._enums import (
    DirectiveStance as DirectiveStance,
)
from memotron.models._enums import (
    DreamJobKind as DreamJobKind,
)
from memotron.models._enums import (
    EpisodeType as EpisodeType,
)
from memotron.models._enums import (
    MemoryAuthority as MemoryAuthority,
)
from memotron.models._enums import (
    MemoryIngestionMode as MemoryIngestionMode,
)
from memotron.models._enums import (
    MemoryType as MemoryType,
)
from memotron.models._enums import (
    OutcomeAttributionMethod as OutcomeAttributionMethod,
)
from memotron.models._enums import (
    OutcomeVerdict as OutcomeVerdict,
)
from memotron.models._enums import (
    QuarantineStatus as QuarantineStatus,
)
from memotron.models._enums import (
    RelationshipCardinality as RelationshipCardinality,
)
from memotron.models._enums import (
    RelationshipStatus as RelationshipStatus,
)
from memotron.models._enums import (
    ScopeKind as ScopeKind,
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
    PruneGhost as PruneGhost,
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
    Episode as Episode,
)
from memotron.models._graph import (
    ExtractedMemory as ExtractedMemory,
)
from memotron.models._graph import (
    GraphNode as GraphNode,
)
from memotron.models._graph import (
    GraphRelationship as GraphRelationship,
)
from memotron.models._graph import (
    MemoryScope as MemoryScope,
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
    MemoryHealthGateError as MemoryHealthGateError,
)
from memotron.models._health import (
    MemoryHealthGateTrip as MemoryHealthGateTrip,
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
    DreamContextMemory as DreamContextMemory,
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
    DreamJobRun as DreamJobRun,
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
from memotron.models._runs import (
    FormationContractAttestation as FormationContractAttestation,
)
from memotron.models._views import (
    EntityNeighborhoodEdge as EntityNeighborhoodEdge,
)
from memotron.models._views import (
    EvidenceEpisode as EvidenceEpisode,
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
