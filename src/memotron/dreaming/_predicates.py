"""Collapsing the many ways a relationship can be phrased into one canonical form.

Small -- four members -- and called from four other planes, which is why it is its
own file rather than folded into one of them. It is a shared service, not a stage.

rebridge_scope_truth_keys is the expensive half and the reason this matters: when a
predicate is re-canonicalized, every truth key derived from the old form is stale,
and rows keyed the old way stop being found by reads keyed the new way. Nothing
raises when that happens -- recall just quietly degrades. So the rebridge walks the
scope and rewrites the keys rather than leaving the graph half-migrated."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.crypto import (
    ContentKeyUnavailableError,
    is_sealed_content,
)
from memotron.dreaming._common import _ContentProtection
from memotron.dreaming._identity import truth_identity
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.embedding import (
    cosine_similarity,
)
from memotron.extraction import match_relationship_instruction
from memotron.graph import normalize_key
from memotron.models import (
    Episode,
    GraphRelationship,
    RelationshipCardinality,
    RelationshipStatus,
)
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptRun,
)

if TYPE_CHECKING:
    _Base = ComposedDreamEngine
else:
    # Plain object at runtime, so DreamEngine's MRO is unchanged.
    _Base = object


class PredicateCanonicalizationMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    @staticmethod
    def memory_type_for_relationship(properties: dict[str, object]) -> str | None:
        """Read memory_type from relationship properties with a safe fallback.

        New relationships written by _materialize_episode carry an explicit
        ``memory_type`` key in their properties JSON.  Older rows (or any
        relationship that was materialized before WS-0) lack that key; for
        those we derive the type from the relationship type using the canonical
        RELATIONSHIP_TYPE_MEMORY_TYPE_MAP, returning None when neither source
        can resolve a value.

        This helper is intentionally a staticmethod so it can be called from
        client.py and other modules without importing DreamEngine.
        """
        from memotron.config import RELATIONSHIP_TYPE_MEMORY_TYPE_MAP

        raw = properties.get("memory_type")
        if isinstance(raw, str) and raw:
            return raw
        # Fallback: derive from the relationship type stored in the graph
        rel_type = properties.get("relationship_type")
        if not isinstance(rel_type, str):
            return None
        resolved = RELATIONSHIP_TYPE_MEMORY_TYPE_MAP.get(rel_type.strip().upper())
        return resolved.value if resolved is not None else None

    def resolve_canonical_predicate(
        self,
        *,
        scope_key: str,
        predicate: str,
        receipt_run: ReceiptRun | None = None,
        episode: Episode | None = None,
        now: datetime | None = None,
        motive_name: str | None = None,
        decided_by: str = "formation",
    ) -> str:
        """WS-17 T16: resolve one predicate surface to its scope-canonical form.

        Deterministic, first-wins resolution order:

        1. Registry hit — the surface was already mapped for this scope.
        2. Operator synonym map — ``PredicateCanonicalizationPolicy.synonyms``
           (registered so later arrivals take path 1).
        3. Embedding pass — the surface is embedded with the ACTIVE transport and
           compared against the scope's existing canonical predicates, each
           embedded fresh with the same transport (same-identifier vectors by
           construction; never a cross-space cosine).  Best cosine at or above
           ``embedding_threshold`` maps the surface onto that canonical.
        4. Self-canonical — the surface becomes its own canonical.

        Every resolution registers the mapping, so the same surface resolves
        identically forever after.  A NEW non-identity mapping (surface !=
        canonical) is receipted as FORMATION_PREDICATE_CANONICALIZED through the
        run's receipt chain; identity registrations are silent by design.

        Returns the canonical predicate (normalized).  When the policy is
        disabled this returns the surface predicate unchanged and touches
        nothing — byte-identical legacy behavior.
        """
        policy = self._config.predicate_canonicalization
        if not policy.enabled:
            return predicate
        normalized = normalize_key(predicate)
        if not normalized:
            return predicate
        registered = self._graph.canonical_predicate_for(scope_key, normalized)
        if registered is not None:
            return registered

        canonical: str | None = None
        decision_source: str | None = None
        cosine: float | None = None
        embedding_identifier: str | None = None

        mapped = policy.synonyms.get(normalized)
        if mapped is not None:
            canonical = normalize_key(mapped)
            decision_source = f"{decided_by}:synonym_map"
        else:
            existing_canonicals = self._graph.canonical_predicates_for_scope(scope_key)[: policy.max_candidates]
            if existing_canonicals:
                surface_vector = self._embedding_transport.embed(normalized)
                best_canonical: str | None = None
                best_cosine = 0.0
                for candidate in existing_canonicals:
                    try:
                        candidate_cosine = cosine_similarity(surface_vector, self._embedding_transport.embed(candidate))
                    except ValueError:
                        continue
                    # Strict > keeps the earliest-registered canonical on ties
                    # (registration order is the deterministic candidate order).
                    if candidate_cosine > best_cosine:
                        best_cosine = candidate_cosine
                        best_canonical = candidate
                if best_canonical is not None and best_cosine >= policy.embedding_threshold:
                    canonical = best_canonical
                    decision_source = f"{decided_by}:embedding"
                    cosine = best_cosine
                    embedding_identifier = self._embedding_transport.identifier

        if canonical is None:
            canonical = normalized
            decision_source = f"{decided_by}:self"

        self._graph.register_predicate(
            scope_key,
            normalized,
            canonical,
            decided_by=decision_source,
            embedding_identifier=embedding_identifier,
            cosine=cosine,
        )
        if canonical != normalized and receipt_run is not None:
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED,
                decision_reason=(
                    f"{normalized}->{canonical}" + (f":cosine={cosine:.4f}" if cosine is not None else ":synonym_map")
                ),
                decision_result="recorded",
                episode=episode,
                now=now,
                scope_key=scope_key,
                motive_name=motive_name,
                dedup_threshold=policy.embedding_threshold,
                dedup_score=cosine,
            )
        if canonical != normalized and receipt_run is not None and decided_by != "backfill":
            # WS-23 C1: a NEW non-identity mapping re-keys the rows that were
            # already written under the surface, in the same transaction as the
            # registration.  Without this, canonicalization only affects future
            # writes: pre-existing rows keep surface truth keys, two
            # contradictory rows sit ACTIVE on split slots, and the single-active
            # repair (which is keyed on truth_key) can never see them.
            self.rebridge_scope_truth_keys(
                scope_key=scope_key,
                receipt_run=receipt_run,
                decision_type=ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED,
                reason_prefix="canonical_registered",
                now=now or datetime.now(UTC),
                predicate_surfaces=frozenset({normalized}),
                episode=episode,
                motive_name=motive_name,
            )
        return canonical

    @staticmethod
    def _rebridgeable_row(relationship: GraphRelationship) -> bool:
        """Is this row's truth key a LIVE routing key rather than frozen lineage?

        Two classes qualify (WS-23 C4):

        * ACTIVE rows — current truth, routed by ``truth_key`` on every write.
        * Gate-parked challengers — SUPERSEDED rows carrying
          ``requires_operator_review`` with no ``review_resolution`` yet.  They
          were never real lineage: they are pending decisions the corroboration
          scan finds by ``truth_prefix``.  Leaving them on a pre-canonical
          prefix strands them forever (every identical challenger parks
          independently at 1/2, truth never flips, and the incumbent is
          dispute-discounted on each park).

        Everything else — genuinely superseded history, pruned rows, resolved
        reviews — is frozen audit lineage and is never rewritten.
        """
        properties = relationship.properties
        status = properties.get("status")
        if status == RelationshipStatus.ACTIVE.value:
            return True
        return (
            status == RelationshipStatus.SUPERSEDED.value
            and properties.get("requires_operator_review") is True
            and not properties.get("review_resolution")
        )

    def rebridge_scope_truth_keys(
        self,
        *,
        scope_key: str,
        receipt_run: ReceiptRun,
        decision_type: ReceiptDecisionType,
        reason_prefix: str,
        now: datetime,
        register_predicates: bool = False,
        predicate_surfaces: frozenset[str] | None = None,
        entity_names: frozenset[str] | None = None,
        episode: Episode | None = None,
        motive_name: str | None = None,
    ) -> list[dict[str, Any]]:
        """Recompute live truth keys from the CURRENT canonical registries.

        This is the single rebridging implementation.  Both operator backfills
        (``canonicalize_scope_predicates`` / ``resolve_scope_entities``) and the
        registration/activation events themselves call it, which is what makes
        canonicalization reconcile with the rows the other workstreams already
        wrote instead of only affecting future writes (WS-23 C1).

        Every call resolves BOTH halves of the key — the subject/object through
        the ACTIVE entity alias registry and the predicate through the predicate
        registry — so the two backfills COMPOSE in either order (WS-23 C5).
        Previously the predicate pass rebuilt ``truth_identity`` from the raw
        endpoint node name and silently reverted the entity half.

        ``register_predicates=True`` additionally runs the registering resolver
        (embedding pass included), which is what makes the predicate backfill a
        backfill rather than a replay of already-registered mappings.

        ``predicate_surfaces`` / ``entity_names`` narrow the scan to the rows one
        activation event can possibly have changed; ``None`` means every
        rebridgeable row in the scope.

        Content-protected rows keep entity resolution INERT (the registry stores
        plaintext names and never registers aliases for sealed scopes) while
        still recanonicalizing their predicate, whose surface is retained
        lineage.  Truth-key commitments are recomputed under the live DEK; a
        sealed row in a shredded scope fails fast rather than silently skipping.
        """
        entity_enabled = self._config.entity_resolution.enabled
        predicate_enabled = self._config.predicate_canonicalization.enabled
        rewritten: list[dict[str, Any]] = []
        sealed_protection: _ContentProtection | None = None
        for relationship in self._graph.relationships_for_scope(scope_key):
            if not self._rebridgeable_row(relationship):
                continue
            properties = relationship.properties
            stored_truth_key = properties.get("truth_key")
            predicate = properties.get("predicate")
            if not isinstance(stored_truth_key, str) or not isinstance(predicate, str):
                continue
            normalized_predicate = normalize_key(predicate)
            protection: _ContentProtection | None = None
            if is_sealed_content(properties.get("fact")):
                if sealed_protection is None:
                    key = self._graph.get_governance_key(scope_key)
                    if key is None:
                        raise ContentKeyUnavailableError(
                            f"scope {scope_key} has sealed rows but no live content key; "
                            "truth keys cannot be recomputed after crypto-shred"
                        )
                    sealed_protection = _ContentProtection(key=key)
                protection = sealed_protection
            # The MENTION surface is what canonicalization maps: a bridged row
            # keeps its mention on ``subject_surface``/``object_surface`` while
            # its endpoint stays the canonical node (endpoints are never
            # rewritten).  Reading the surface is what lets a row return to its
            # own slot when the alias that bridged it is retired.
            node_subject = str(
                self._graph.reveal(
                    scope_key,
                    self._graph.get_node(relationship.source_uuid).properties["name"],
                )
            )
            node_object = str(self._graph.reveal(scope_key, properties.get("object", "")))
            stored_subject_surface = properties.get("subject_surface")
            stored_object_surface = properties.get("object_surface")
            subject = (
                str(stored_subject_surface)
                if isinstance(stored_subject_surface, str) and stored_subject_surface.strip()
                else node_subject
            )
            object_value = (
                str(stored_object_surface)
                if isinstance(stored_object_surface, str) and stored_object_surface.strip()
                else node_object
            )
            if protection is None and entity_enabled:
                canonical_subject = self._graph.canonical_entity_name_for(scope_key, subject) or subject
                canonical_object = self._graph.canonical_entity_name_for(scope_key, object_value) or object_value
            else:
                canonical_subject, canonical_object = subject, object_value
            if entity_names is not None and not (
                normalize_key(subject) in entity_names or normalize_key(object_value) in entity_names
            ):
                continue
            if predicate_surfaces is not None and normalized_predicate not in predicate_surfaces:
                continue
            if not predicate_enabled:
                canonical_predicate = str(properties.get("predicate_canonical") or predicate)
            elif register_predicates:
                canonical_predicate = self.resolve_canonical_predicate(
                    scope_key=scope_key,
                    predicate=predicate,
                    receipt_run=receipt_run,
                    episode=episode,
                    now=now,
                    motive_name=motive_name,
                    decided_by="backfill",
                )
            else:
                canonical_predicate = self._graph.canonical_predicate_for(scope_key, normalized_predicate) or str(
                    properties.get("predicate_canonical") or predicate
                )
            cardinality = RelationshipCardinality(
                properties.get("truth_cardinality", RelationshipCardinality.MULTI_ACTIVE.value)
            )
            new_truth_key, new_truth_prefix, _ = truth_identity(
                scope_key=scope_key,
                subject=canonical_subject,
                predicate=canonical_predicate,
                object_value=canonical_object,
                cardinality=cardinality,
                protection=protection,
            )
            stamped_canonical = canonical_predicate if predicate_enabled else properties.get("predicate_canonical")
            if (
                new_truth_key == stored_truth_key
                and new_truth_prefix == properties.get("truth_prefix")
                and properties.get("predicate_canonical") == stamped_canonical
            ):
                continue
            updates: dict[str, Any] = {
                "truth_key": new_truth_key,
                "truth_prefix": new_truth_prefix,
            }
            if predicate_enabled:
                updates["predicate_canonical"] = canonical_predicate
            updates["subject_surface"] = subject if canonical_subject != subject else None
            updates["object_surface"] = object_value if canonical_object != object_value else None
            state_before = self._graph.graph_state_hash(scope_key)
            self._graph.update_relationship(relationship.uuid, properties=updates)
            state_after = self._graph.graph_state_hash(scope_key)
            if canonical_subject != subject or canonical_object != object_value:
                moved_from, moved_to = (
                    (subject, canonical_subject) if canonical_subject != subject else (object_value, canonical_object)
                )
            else:
                moved_from, moved_to = predicate, canonical_predicate
            self._emit_receipt(
                receipt_run,
                decision_type=decision_type,
                decision_reason=(f"{reason_prefix}:{normalize_key(moved_from)}->{normalize_key(moved_to)}"),
                decision_result="transformed",
                episode=episode,
                now=now,
                scope_key=scope_key,
                motive_name=motive_name,
                relationship_type=relationship.type,
                memory_type=self.memory_type_for_relationship(dict(properties)),
                relationship_uuid=relationship.uuid,
                truth_key=new_truth_key,
                graph_state_hash_before=state_before,
                graph_state_hash_after=state_after,
            )
            rewritten.append(
                {
                    "relationship_uuid": relationship.uuid,
                    "predicate": predicate,
                    "predicate_canonical": canonical_predicate,
                    "subject": subject,
                    "subject_canonical": canonical_subject,
                    "object": object_value,
                    "object_canonical": canonical_object,
                    "parked_for_review": properties.get("status") == RelationshipStatus.SUPERSEDED.value,
                }
            )
        return rewritten

    def _resolve_memory_type_value(
        self,
        *,
        relationship_type: str,
        instruction_set_name: str,
        subject_label: str = "*",
        object_label: str = "*",
    ) -> str | None:
        """Resolve the memory_type string value for a relationship_type from the instruction set.

        ``subject_label``/``object_label`` default to the ``"*"`` wildcard for
        call sites that have no candidate in hand (e.g. filing an abstained
        candidate that named a relationship type but no endpoints).  Every call
        site that DOES have an ``ExtractedMemory`` must pass its real
        ``subject_label``/``object_label`` — this is the SAME matcher
        materialization uses (:func:`match_relationship_instruction`), and it is
        the only implementation of "which instruction governs this candidate".
        Calling it with the wildcard for a real candidate can silently miss an
        ``open_predicate`` instruction whose endpoints are NOT ``"*"``, which is
        exactly the class of extraction/materialization disagreement that made
        a resolvable candidate raise two layers away from the cause.
        """
        from memotron.config import RELATIONSHIP_TYPE_MEMORY_TYPE_MAP

        try:
            instr_set = self._config.instruction_set(instruction_set_name)
        except ValueError:
            instr_set = None
        if instr_set is not None:
            rel_instr = match_relationship_instruction(
                instructions=instr_set,
                relationship_type=relationship_type,
                subject_label=subject_label,
                object_label=object_label,
            )
            if rel_instr is not None and rel_instr.memory_type is not None:
                return rel_instr.memory_type.value
        # Fallback: canonical map
        resolved = RELATIONSHIP_TYPE_MEMORY_TYPE_MAP.get(relationship_type.strip().upper())
        return resolved.value if resolved is not None else None
