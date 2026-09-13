"""WS-10: Cross-artifact behavioral coherence.

Ordinary dreaming reconciles contradictions *within the memory store* (dedup,
supersession, rollup demotion).  But an autonomous agent does not learn from
memory alone — its effective behavior is governed by a heterogeneous set of
persistent artifacts (memory, skills, files) that are authored on different
cadences and carry different *execution precedence*.  A procedural skill is run
step-by-step and dominates a declarative memory that is merely injected as
advisory context.  When a stale skill silently overrides repeated feedback, the
memory system — working exactly as designed — strengthens the feedback memory
across cycle after cycle (the "feedback-futility escalation"), and never
converges, because the real contradiction lives in an artifact the reconciler
cannot see.

This module is the *pure logic* of a coherence cycle that extends reconciliation
across artifact classes.  It projects every governing artifact into a common
``Directive`` representation, matches directives by semantic identity (topic
embedding cosine), and raises two kinds of incident:

* ``ESCALATION_WINDUP`` — a memory directive strengthened across >= K cycles for
  which a *higher-precedence, staler* non-memory artifact governs the same
  subject.  This is the anti-windup detector: the escalation itself is the
  observable signature of an invisible cross-artifact contradiction.
* ``CROSS_ARTIFACT_CONTRADICTION`` — two artifact classes assert conflicting
  stances on the same subject.

For each incident it performs *culprit attribution* (which artifact actually
governs behavior, by execution precedence + staleness) and proposes a repair of
the governing artifact rather than yet another stricter memory.

Side effects (recording auditable decisions, marking a directive
``coherence_hold``) are performed by :class:`dreaming.DreamEngine`; this module
only reads the graph and computes incidents, so it stays deterministic and
hermetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from memotron.config import CoherencePolicy
from memotron.embedding import (
    EmbeddingTransport,
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.models import (
    ArtifactClass,
    CoherenceIncident,
    CoherenceIncidentKind,
    CoherenceReport,
    Directive,
    DirectiveStance,
    MemoryScope,
    PersistentArtifact,
)
from memotron.storage import StorageBackend

# Stance pairs that are mutually contradictory when asserted on the same subject.
# REQUIRE-vs-ASSERT is the central case: feedback REQUIREs more rigor while a
# skill ASSERTs the behavior is already sufficient.
_CONFLICTING_STANCES: frozenset[frozenset[DirectiveStance]] = frozenset(
    {
        frozenset({DirectiveStance.REQUIRE, DirectiveStance.FORBID}),
        frozenset({DirectiveStance.REQUIRE, DirectiveStance.ASSERT}),
        frozenset({DirectiveStance.FORBID, DirectiveStance.GUIDE}),
        frozenset({DirectiveStance.FORBID, DirectiveStance.ASSERT}),
    }
)


def stances_conflict(a: DirectiveStance, b: DirectiveStance) -> bool:
    if a == b:
        return False
    return frozenset({a, b}) in _CONFLICTING_STANCES


class ArtifactSource(Protocol):
    """A provider of non-memory persistent artifacts for a scope.

    Implementations read the agent's live skill registry / file directories.
    The hermetic default is :class:`StaticArtifactSource`.
    """

    def artifacts(self, scope: MemoryScope) -> list[PersistentArtifact]: ...


class StaticArtifactSource:
    """Concrete :class:`ArtifactSource` over an in-memory list of artifacts."""

    def __init__(self, artifacts: list[PersistentArtifact]) -> None:
        self._artifacts = list(artifacts)

    def artifacts(self, scope: MemoryScope) -> list[PersistentArtifact]:
        return [artifact for artifact in self._artifacts if artifact.scope.key == scope.key]


class GraphArtifactSource:
    """Fail-safe projection of the durable live skill/file registry."""

    def __init__(self, graph: StorageBackend) -> None:
        self._graph = graph

    def artifacts(self, scope: MemoryScope) -> list[PersistentArtifact]:
        # ``live_artifacts`` validates persisted payloads and excludes quarantined
        # entries by default.  A malformed projection raises rather than leaking
        # an ambiguous directive into the behavioral authority plane.
        return self._graph.live_artifacts(scope=scope)


def _parse_dt(value: object) -> datetime | None:
    """Parse a timestamp into a timezone-aware datetime (assume UTC if naive)."""
    parsed: datetime | None = None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    if parsed is not None and parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class CoherenceScanner:
    """Pure cross-artifact coherence analysis over one scope.

    Reads memory directives from the graph and non-memory directives from the
    registered artifact sources, then detects incidents.  Performs no writes.
    """

    def __init__(
        self,
        *,
        graph: StorageBackend,
        embedding_transport: EmbeddingTransport,
        policy: CoherencePolicy,
    ) -> None:
        self._graph = graph
        self._embedding = embedding_transport
        self._policy = policy

    # ------------------------------------------------------------------ projection

    def _embed(self, text: str) -> list[float]:
        return self._embedding.embed(text)

    def project_memory_directives(self, scope: MemoryScope) -> list[Directive]:
        """Project active behavioral memory relationships into directives."""
        participating = set(self._policy.participating_memory_types)
        directives: list[Directive] = []
        for relationship in self._graph.active_relationships(scope=scope):
            props = relationship.properties
            memory_type = props.get("memory_type")
            if memory_type not in participating:
                continue
            # WS-12: sealed content reveals under the live scope DEK; a row whose
            # content is unrecoverable (shredded scope) is OMITTED rather than
            # guessed — the same fail-safe rule as unprojectable artifacts.
            from memotron.crypto import SHREDDED_CONTENT_PLACEHOLDER

            subject = str(self._graph.reveal(scope.key, props.get("object", ""))).strip()
            if not subject or subject == SHREDDED_CONTENT_PLACEHOLDER:
                continue
            instruction = str(self._graph.reveal(scope.key, props.get("fact")) or subject)
            # WS-17 T18 vector-space guard: a stored topic vector participates
            # only when stamped for the active transport; otherwise the revealed
            # subject is re-embedded in the ACTIVE space (the existing fallback).
            embedding = (
                self._graph.reveal_vector(scope.key, props.get("object_embedding"))
                if stored_vector_in_active_space(props, active_identifier=self._embedding.identifier)
                else None
            )
            if not isinstance(embedding, list) or not embedding:
                embedding = self._embed(subject)
            updated_at = (
                _parse_dt(props.get("reinforced_at")) or _parse_dt(props.get("last_seen_at")) or relationship.valid_from
            )
            raw_stance = props.get("directive_stance")
            try:
                stance = DirectiveStance(str(raw_stance))
            except ValueError:
                # Pre-claim-mode rows used REQUIRE for all behavioral memories.
                # Retaining that interpretation keeps legacy rows auditable while
                # every new row carries an explicit deterministic stance.
                stance = DirectiveStance.REQUIRE
            artifact_class = (
                ArtifactClass.GENERATED_OUTPUT
                if props.get("authority_class") == "non_normative"
                else ArtifactClass.MEMORY
            )
            directives.append(
                Directive(
                    scope=scope,
                    subject=subject,
                    instruction=instruction,
                    stance=stance,
                    artifact_class=artifact_class,
                    artifact_id=relationship.uuid,
                    author=str(props.get("created_by", "dream")),
                    precedence=self._policy.precedence_for(artifact_class),
                    updated_at=updated_at,
                    observed_count=int(props.get("observed_count", 1)),
                    confidence=float(props.get("confidence", 1.0)),
                    relationship_uuid=relationship.uuid,
                    embedding=list(embedding),
                    metadata={
                        "memory_type": memory_type,
                        "authority_class": props.get("authority_class", "memory"),
                        # Already under an open anti-windup hold from a prior scan — carried
                        # through so detection is idempotent (a held directive is not re-raised).
                        "coherence_hold": bool(props.get("coherence_hold")),
                    },
                )
            )
        return directives

    def project_artifact_directives(
        self, scope: MemoryScope, artifact_sources: list[ArtifactSource]
    ) -> list[Directive]:
        """Project registered skills/files into directives."""
        directives: list[Directive] = []
        for source in artifact_sources:
            for artifact in source.artifacts(scope):
                if artifact.scope.key != scope.key:
                    continue
                precedence = self._policy.precedence_for(artifact.artifact_class)
                directives.extend(
                    Directive(
                        scope=scope,
                        subject=declared.subject,
                        instruction=declared.instruction,
                        stance=declared.stance,
                        artifact_class=artifact.artifact_class,
                        artifact_id=artifact.artifact_id,
                        artifact_location=artifact.location,
                        author=artifact.author,
                        self_authored=artifact.self_authored,
                        precedence=precedence,
                        updated_at=artifact.updated_at,
                        embedding=self._embed(declared.subject),
                        metadata=dict(declared.metadata),
                    )
                    for declared in artifact.directives
                )
        return directives

    # ------------------------------------------------------------------ detection

    def _identity(self, a: Directive, b: Directive) -> float:
        if not a.embedding or not b.embedding:
            return 0.0
        try:
            return cosine_similarity(a.embedding, b.embedding)
        except ValueError:
            return 0.0

    def scan(
        self,
        *,
        scope: MemoryScope,
        artifact_sources: list[ArtifactSource],
        now: datetime,
    ) -> CoherenceReport:
        memory_dirs = self.project_memory_directives(scope)
        artifact_dirs = self.project_artifact_directives(scope, artifact_sources)
        directives = memory_dirs + artifact_dirs

        per_class: dict[str, int] = {}
        for directive in directives:
            per_class[directive.artifact_class.value] = per_class.get(directive.artifact_class.value, 0) + 1

        report = CoherenceReport(
            scope=scope,
            ran_at=now,
            artifact_count=len({(d.artifact_class, d.artifact_id) for d in artifact_dirs}),
            directive_count=len(directives),
            per_class_directive_counts=per_class,
        )

        # Pairs (by directive_id) already explained by an incident — so a windup and a
        # contradiction are not both raised for the same pair.
        covered: set[frozenset[str]] = set()

        if self._policy.detect_escalation_windup:
            for incident in self._detect_escalation_windup(scope, memory_dirs, artifact_dirs):
                report.incidents.append(incident)
                report.escalation_windup_count += 1
                if incident.escalating_directive and incident.governing_directive:
                    covered.add(
                        frozenset(
                            {
                                incident.escalating_directive.directive_id,
                                incident.governing_directive.directive_id,
                            }
                        )
                    )

        if self._policy.detect_contradictions:
            for incident in self._detect_contradictions(scope, directives, covered):
                report.incidents.append(incident)
                report.contradiction_count += 1

        return report

    def _detect_escalation_windup(
        self,
        scope: MemoryScope,
        memory_dirs: list[Directive],
        artifact_dirs: list[Directive],
    ) -> list[CoherenceIncident]:
        threshold = self._policy.identity_threshold
        k = self._policy.escalation_cycles_threshold
        memory_precedence = self._policy.precedence_for(ArtifactClass.MEMORY)
        incidents: list[CoherenceIncident] = []

        for memory in memory_dirs:
            if memory.observed_count < k:
                continue
            # Idempotency: a directive already under an open hold from a prior scan is not
            # re-raised — the incident already exists and the windup is already arrested.
            if memory.metadata.get("coherence_hold"):
                continue
            candidates: list[tuple[Directive, float]] = []
            for other in artifact_dirs:
                if other.precedence <= memory_precedence:
                    continue
                cosine = self._identity(memory, other)
                if cosine < threshold:
                    continue
                stale = other.updated_at is None or (
                    memory.updated_at is not None and other.updated_at < memory.updated_at
                )
                if not stale:
                    continue
                candidates.append((other, cosine))
            if not candidates:
                continue
            governing, cosine = max(candidates, key=lambda pair: (pair[0].precedence, pair[1]))
            incidents.append(self._build_windup_incident(scope, memory, governing, cosine))
        return incidents

    def _detect_contradictions(
        self,
        scope: MemoryScope,
        directives: list[Directive],
        covered: set[frozenset[str]],
    ) -> list[CoherenceIncident]:
        threshold = self._policy.identity_threshold
        incidents: list[CoherenceIncident] = []
        for i, first in enumerate(directives):
            for second in directives[i + 1 :]:
                # Generated outputs/tool traces are records of what happened,
                # never standing instructions.  They may inform investigation,
                # but cannot become a governing or conflicting directive merely
                # by being frequent in context.
                if ArtifactClass.GENERATED_OUTPUT in {
                    first.artifact_class,
                    second.artifact_class,
                }:
                    continue
                if first.artifact_class == second.artifact_class and first.artifact_class != ArtifactClass.MEMORY:
                    continue
                # Idempotency: skip pairs whose memory directive is already held.
                if first.metadata.get("coherence_hold") or second.metadata.get("coherence_hold"):
                    continue
                if not stances_conflict(first.stance, second.stance):
                    continue
                cosine = self._identity(first, second)
                if cosine < threshold:
                    continue
                if frozenset({first.directive_id, second.directive_id}) in covered:
                    continue
                governing, contender = (first, second) if first.precedence >= second.precedence else (second, first)
                incidents.append(self._build_contradiction_incident(scope, governing, contender, cosine))
        return incidents

    # ------------------------------------------------------------------ incident text

    @staticmethod
    def _author_label(directive: Directive) -> str:
        if directive.self_authored:
            return f"{directive.author} (self-authored)"
        return directive.author

    def _build_windup_incident(
        self, scope: MemoryScope, memory: Directive, governing: Directive, cosine: float
    ) -> CoherenceIncident:
        gap = governing.precedence - memory.precedence
        updated = governing.updated_at.date().isoformat() if governing.updated_at else "unknown date"
        rationale = (
            f"Memory directive '{memory.instruction}' has been strengthened across "
            f"{memory.observed_count} reinforcement cycles without resolution. A higher-precedence "
            f"{governing.artifact_class.value.upper()} artifact '{governing.artifact_id}' "
            f"(execution precedence {governing.precedence} > memory {memory.precedence}; "
            f"gap {gap}), authored by {self._author_label(governing)} and last updated {updated} "
            f"— predating the latest escalation — governs the same subject "
            f"(identity cosine={cosine:.3f}). The repeated feedback is being absorbed into "
            f"memory while the artifact silently overrides it at execution time, so the memory "
            f"escalates but behavior never changes."
        )
        repair = (
            f"Update or quarantine {governing.artifact_class.value} artifact "
            f"'{governing.artifact_id}'"
            + (f" at {governing.artifact_location}" if governing.artifact_location else "")
            + f" to incorporate the directive '{memory.instruction}', and suppress further "
            f"blind escalation of memory {memory.relationship_uuid} pending that repair."
        )
        summary = (
            f"Escalation windup on '{memory.subject}': {memory.observed_count} feedback cycles "
            f"overridden by stale {governing.artifact_class.value} '{governing.artifact_id}'."
        )
        return CoherenceIncident(
            scope=scope,
            kind=CoherenceIncidentKind.ESCALATION_WINDUP,
            subject=memory.subject,
            summary=summary,
            escalating_directive=memory,
            governing_directive=governing,
            conflicting_directives=[memory, governing],
            identity_cosine=cosine,
            escalation_cycles=memory.observed_count,
            precedence_gap=gap,
            attribution_confidence=min(
                1.0,
                0.65 * cosine + 0.35 * min(1.0, gap / max(governing.precedence, 1)),
            ),
            attribution_rationale=rationale,
            proposed_repair=repair,
        )

    def _build_contradiction_incident(
        self, scope: MemoryScope, governing: Directive, contender: Directive, cosine: float
    ) -> CoherenceIncident:
        gap = governing.precedence - contender.precedence
        rationale = (
            f"{governing.artifact_class.value.upper()} artifact '{governing.artifact_id}' "
            f"(stance={governing.stance.value}, precedence {governing.precedence}) and "
            f"{contender.artifact_class.value.upper()} artifact '{contender.artifact_id}' "
            f"(stance={contender.stance.value}, precedence {contender.precedence}) assert "
            f"conflicting directives on the same subject (identity cosine={cosine:.3f}). "
            f"The {governing.artifact_class.value} governs at execution time."
        )
        repair = (
            f"Reconcile {contender.artifact_class.value} '{contender.artifact_id}' against the "
            f"governing {governing.artifact_class.value} '{governing.artifact_id}', or update the "
            f"governing artifact if the {contender.artifact_class.value} is the intended truth."
        )
        summary = (
            f"Cross-artifact contradiction on '{governing.subject}': "
            f"{governing.artifact_class.value} '{governing.artifact_id}' vs "
            f"{contender.artifact_class.value} '{contender.artifact_id}'."
        )
        return CoherenceIncident(
            scope=scope,
            kind=CoherenceIncidentKind.CROSS_ARTIFACT_CONTRADICTION,
            subject=governing.subject,
            summary=summary,
            escalating_directive=contender,
            governing_directive=governing,
            conflicting_directives=[governing, contender],
            identity_cosine=cosine,
            precedence_gap=gap,
            attribution_confidence=min(
                1.0,
                0.75 * cosine + 0.25 * (1.0 if gap > 0 else 0.5),
            ),
            attribution_rationale=rationale,
            proposed_repair=repair,
        )
