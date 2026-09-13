"""What a scope asserted, when, and how the answer changed.

The bitemporal read surface. `truth_timeline` walks valid-time; the evolution
helpers walk transaction-time. Keeping them together is deliberate: confusing the two
axes is the single easiest mistake to make against this data model, and the file is
where that distinction is stated."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.client._common import _AWARE_MAX, _AWARE_MIN
from memotron.client._protocol import ComposedMemotron
from memotron.dreaming import DreamEngine
from memotron.models import (
    MemoryEvolutionFact,
    MemoryEvolutionProof,
    MemoryEvolutionSignal,
    MemoryScope,
    RelationshipStatus,
    TruthTimelineEntry,
)
from memotron.storage import (
    StorageBackend,
    normalize_key,
)

if TYPE_CHECKING:
    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class TruthTimelineMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    authorized_scope_keys: Any
    graph: StorageBackend

    async def truth_timeline(
        self,
        *,
        scope: MemoryScope,
        subject: str,
        predicate: str,
        relationship_type: str | None = None,
        include_statuses: set[RelationshipStatus] | None = None,
    ) -> list[TruthTimelineEntry]:
        self._require_authorized_scope(scope)
        if not subject.strip():
            raise ValueError("subject cannot be blank")
        if not predicate.strip():
            raise ValueError("predicate cannot be blank")
        normalized_subject = normalize_key(subject)
        normalized_predicate = normalize_key(predicate)
        normalized_relationship_type = (
            relationship_type.strip().replace(" ", "_").upper() if relationship_type is not None else None
        )
        if normalized_relationship_type == "":
            raise ValueError("relationship_type cannot be blank")
        allowed_statuses = include_statuses or {
            RelationshipStatus.ACTIVE,
            RelationshipStatus.SUPERSEDED,
            RelationshipStatus.PRUNED,
        }
        entries: list[TruthTimelineEntry] = []
        for relationship in self.graph.relationships_for_scope(scope.key):
            if relationship.type == "MENTIONS":
                continue
            if normalized_relationship_type is not None and relationship.type != normalized_relationship_type:
                continue
            status = self._relationship_status(relationship.properties.get("status"))
            if status not in allowed_statuses:
                continue
            subject_name = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.source_uuid).properties["name"])
            )
            if normalize_key(subject_name) != normalized_subject:
                continue
            predicate_value = str(relationship.properties.get("predicate", ""))
            if normalize_key(predicate_value) != normalized_predicate:
                continue
            object_value = str(
                self.graph.reveal(scope.key, self.graph.get_node(relationship.target_uuid).properties["name"])
            )
            entries.append(
                TruthTimelineEntry(
                    relationship_uuid=relationship.uuid,
                    relationship_type=relationship.type,
                    fact=str(self.graph.reveal(scope.key, relationship.properties["fact"])),
                    scope=scope,
                    subject=subject_name,
                    predicate=predicate_value,
                    object=object_value,
                    confidence=float(relationship.properties["confidence"]),
                    status=status,
                    is_current=status == RelationshipStatus.ACTIVE
                    and self._relationship_window_contains(
                        valid_from=relationship.valid_from,
                        valid_to=relationship.valid_to,
                        instant=datetime.now(UTC),
                    ),
                    valid_from=relationship.valid_from,
                    valid_to=relationship.valid_to,
                    episode_uuid=str(relationship.properties["episode_uuid"]),
                    episode_uuids=self._relationship_episode_uuids(relationship.properties),
                    observed_count=int(relationship.properties.get("observed_count", 1)),
                    created_by=str(relationship.properties.get("created_by", "")),
                    superseded_by_relationship_uuid=self._optional_string(
                        relationship.properties.get("superseded_by_relationship_uuid")
                    ),
                    pruned_reason=self._optional_string(relationship.properties.get("pruned_reason")),
                    metadata=self._relationship_metadata(relationship.properties),
                )
            )
        entries.sort(
            key=lambda entry: (
                # Aware sentinels; see the note in `semantic_search`.
                entry.valid_from or _AWARE_MIN,
                entry.valid_to or _AWARE_MAX,
                entry.relationship_uuid,
            )
        )
        return entries

    def export_graph(self) -> dict[str, Any]:
        """Return the full graph state plus a memory_type distribution summary.

        WS-19 T21: fails closed on a scope-guarded client — a whole-store
        export spans every scope by construction.

        Shape (backward-compatible — existing keys unchanged):
        {
          "nodes": [...],
          "relationships": [...],
          "memory_type_distribution": {
            "requirement": 3,   # active relationships only
            "preference": 2,
            ...
          }
        }

        memory_type_distribution counts only active (non-MENTIONS) relationships
        whose memory_type resolves to a non-null value.  Older rows without an
        explicit memory_type key are resolved via the canonical fallback map.
        """
        if self.authorized_scope_keys is not None:
            raise ValueError(
                "export_graph spans every scope and is not available on a "
                "scope-guarded client (authorized_scope_keys is set)"
            )
        base = self.graph.export()
        distribution: dict[str, int] = {}
        for rel in base["relationships"]:
            if rel.get("type") == "MENTIONS":
                continue
            props = rel.get("properties", {})
            if props.get("status") != "active":
                continue
            mt = DreamEngine.memory_type_for_relationship(props)
            if mt is not None:
                distribution[mt] = distribution.get(mt, 0) + 1
        base["memory_type_distribution"] = distribution
        return base

    def active_relationship_statuses(self) -> list[str]:
        return [
            relationship.properties.get("status", RelationshipStatus.ACTIVE.value)
            for relationship in self.graph.relationships()
        ]

    def _memory_evolution_fact(
        self,
        relationship: Any,
        *,
        scope: MemoryScope,
    ) -> MemoryEvolutionFact:
        source_node = self.graph.get_node(relationship.source_uuid)
        target_node = self.graph.get_node(relationship.target_uuid)
        return MemoryEvolutionFact(
            relationship_uuid=relationship.uuid,
            relationship_type=relationship.type,
            fact=str(self.graph.reveal(scope.key, relationship.properties["fact"])),
            scope=scope,
            subject=str(self.graph.reveal(scope.key, source_node.properties["name"])),
            predicate=str(relationship.properties["predicate"]),
            object=str(self.graph.reveal(scope.key, target_node.properties["name"])),
            confidence=float(relationship.properties["confidence"]),
            status=self._relationship_status(relationship.properties.get("status")),
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            episode_uuids=self._relationship_episode_uuids(relationship.properties),
            observed_count=int(relationship.properties.get("observed_count", 1)),
            created_by=str(relationship.properties.get("created_by", "")),
            superseded_by_relationship_uuid=self._optional_string(
                relationship.properties.get("superseded_by_relationship_uuid")
            ),
            pruned_reason=self._optional_string(relationship.properties.get("pruned_reason")),
            metadata=self._relationship_metadata(relationship.properties),
            memory_type=DreamEngine.memory_type_for_relationship(dict(relationship.properties)),
        )

    def _relationship_is_current_active(self, relationship: Any, *, as_of: datetime) -> bool:
        return self._relationship_is_visible(
            status=self._relationship_status(relationship.properties.get("status")),
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
            as_of=as_of,
            include_statuses={RelationshipStatus.ACTIVE},
        )

    def _memory_evolution_signals(self, proof: MemoryEvolutionProof) -> list[MemoryEvolutionSignal]:
        return [
            MemoryEvolutionSignal(
                name="selection_decisions",
                observed=proof.decision_count > 0,
                count=proof.decision_count,
                evidence="dream-agent formation, consolidation, or pruning decisions were persisted for this scope",
            ),
            MemoryEvolutionSignal(
                name="reinforcement",
                observed=proof.reinforced_relationship_count > 0,
                count=proof.reinforced_relationship_count,
                evidence="duplicate observations updated observed_count/confidence instead of creating duplicate active facts",
            ),
            MemoryEvolutionSignal(
                name="supersession",
                observed=proof.superseded_relationship_count > 0,
                count=proof.superseded_relationship_count,
                evidence="older single-active truths were marked superseded by newer contradictory facts",
            ),
            MemoryEvolutionSignal(
                name="pruning",
                observed=proof.pruned_relationship_count > 0,
                count=proof.pruned_relationship_count,
                evidence="expired, low-confidence, stale, or retention-elapsed facts were removed from current retrieval",
            ),
            MemoryEvolutionSignal(
                name="consolidation",
                observed=proof.consolidation_relationship_count > 0,
                count=proof.consolidation_relationship_count,
                evidence="offline graph context produced derived memories rather than only raw episode extraction",
            ),
            MemoryEvolutionSignal(
                name="retrieval_filtering",
                observed=proof.inactive_relationship_count > 0,
                count=proof.inactive_relationship_count,
                evidence="inactive facts remain auditable but no longer appear in the active caller profile",
            ),
            MemoryEvolutionSignal(
                name="compression",
                observed=proof.episode_count > proof.active_relationship_count,
                count=max(0, proof.episode_count - proof.active_relationship_count),
                evidence="multiple raw episodes resolved to fewer active relationship memories for the caller",
            ),
            # WS-6 signals — APPENDED after the original 7; never renamed or removed.
            MemoryEvolutionSignal(
                name="compression_ratio",
                observed=proof.compression_ratio > 1.0,
                count=int(proof.compression_ratio * 100),
                evidence=(
                    f"episode_count / context_visible_count = "
                    f"{proof.episode_count} / {max(1, proof.context_visible_relationship_count)} "
                    f"= {proof.compression_ratio:.2f}x; >1.0 means raw episodes compressed into fewer active facts"
                ),
            ),
            MemoryEvolutionSignal(
                name="semantic_dedup",
                observed=proof.semantic_dedup_rate > 0.0,
                count=int(proof.semantic_dedup_rate * 100),
                evidence=(
                    f"reinforced_observations / total_observations = {int(proof.semantic_dedup_rate * 100)}%; "
                    "observations that updated an existing row instead of creating a duplicate"
                ),
            ),
            MemoryEvolutionSignal(
                name="rollup_coverage",
                observed=proof.rollup_relationship_count > 0,
                count=proof.rollup_relationship_count,
                evidence=(
                    f"{proof.rollup_relationship_count} rollup node(s) active in context; "
                    f"{proof.demoted_relationship_count} member fact(s) demoted from default retrieval"
                ),
            ),
        ]
