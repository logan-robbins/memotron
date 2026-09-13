"""Deciding which mentions name the same entity, and undoing that when it was wrong.

Entity resolution is the one plane here that routinely gets it WRONG and has to be
reversible. Two people with the same first name, a product and the company that
makes it, an acronym that collides -- these merge, and the merge has to be
undoable without losing what was written while it held. So this file is roughly
half resolution (candidates, surfaces, provenance tokens, neighbour overlap, link
signals) and half retraction: _discount_entity_alias,
_discount_entity_aliases_on_conflict, _rebridge_on_alias_activation and the
alias-bridged row counting that tells an operator how much a merge touched.

_MentionLinkContext and _EntityCandidate move with the methods. They are nested
dataclasses on DreamEngine, and tests/test_entity_resolution.py:895 constructs
EntityResolutionMixin._MentionLinkContext directly -- that path keeps working through the
MRO, and the API-surface golden pins it."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from memotron.config import (
    DreamInstructionSet,
)
from memotron.crypto import (
    is_sealed_content,
)
from memotron.dreaming._common import _ContentProtection, _log
from memotron.dreaming._identity import _EntityResolution, composed_entity_link_score
from memotron.dreaming._protocol import ComposedDreamEngine
from memotron.dreaming._text import identifier_tokens_conflict
from memotron.embedding import (
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.graph import normalize_key
from memotron.models import (
    Episode,
    GraphRelationship,
    MemoryScope,
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


class EntityResolutionMixin(_Base):
    """Composed into :class:`DreamEngine`."""

    # Provided by the composing backend.

    @dataclass(frozen=True)
    class _EntityCandidate:
        """One canonical entity the resolution pass may link a mention to."""

        surface: str
        normalized: str
        node_uuid: str
        node_properties: dict[str, object]

    @dataclass
    class _MentionLinkContext:
        """Everything in the link score that depends on the MENTION, not the candidate.

        One mention is scored against the whole canonical inventory, so anything
        derived from the mention name or the episode is loop-invariant across
        that scan and belongs here — resolved once, reused for every candidate.

        ``name_embedding`` is the reason this type exists.  It used to be
        computed inside the per-candidate signal function, which made entity
        linking cost O(mentions x inventory) embeddings.  On the hermetic local
        transport that is merely wasteful; on a network transport
        (``OpenAICompatibleEmbeddingTransport`` against the gateway) it is one
        HTTPS round trip per candidate, superlinear as the inventory grows, and
        it dominated ingest wall-clock.  It stays LAZY rather than eager because
        the signal is only consulted when a candidate carries a same-space
        stored ``name_embedding``: a scope whose inventory has no usable stored
        vectors must still cost zero embeddings, exactly as before.
        """

        scope_key: str
        normalized: str
        neighbors: frozenset[str]
        episode_tokens: frozenset[str]
        embed: Callable[[str], list[float]] = field(repr=False)
        _name_embedding: list[float] | None = field(default=None, repr=False)

        @property
        def name_embedding(self) -> list[float]:
            """The mention name's vector — computed at most once per mention."""
            if self._name_embedding is None:
                self._name_embedding = self.embed(self.normalized)
            return self._name_embedding

    _ENTITY_PROVENANCE_KEYS = ("slug", "section", "source")

    _ENTITY_PROVENANCE_FACT_CAP = 32

    def _entity_resolution_candidates(
        self, scope_key: str, *, exclude_normalized: str | None = None
    ) -> list[EntityResolutionMixin._EntityCandidate]:
        """Canonical entity candidates for one scope, deterministic order.

        Sourced from the scope's entity nodes (created_at, uuid order — the
        same order every replay of the same input stream produces), skipping
        Episode nodes, sealed names (crypto scopes are inert for entity
        resolution), names already registered as an ACTIVE alias (they resolve
        to their canonical instead of being canonicals), and names with a
        PENDING proposal (linking through an unadjudicated alias would chain
        aliases).  Capped at ``max_pair_scan``.
        """
        policy = self._config.entity_resolution
        registry = {row["name_normalized"]: row for row in self._graph.entity_alias_rows_for_scope(scope_key)}
        candidates: list[EntityResolutionMixin._EntityCandidate] = []
        seen: set[str] = set()
        for node in self._graph.nodes_for_scope(scope_key):
            if "Episode" in node.labels:
                continue
            name = node.properties.get("name")
            if is_sealed_content(name):
                continue
            if not isinstance(name, str) or not name.strip():
                continue
            normalized = normalize_key(name)
            if not normalized or normalized in seen:
                continue
            if exclude_normalized is not None and normalized == exclude_normalized:
                continue
            row = registry.get(normalized)
            if row is not None and row["status"] in ("active", "proposed"):
                continue
            seen.add(normalized)
            candidates.append(
                EntityResolutionMixin._EntityCandidate(
                    surface=name,
                    normalized=normalized,
                    node_uuid=node.uuid,
                    node_properties=dict(node.properties),
                )
            )
            if len(candidates) >= policy.max_pair_scan:
                break
        return candidates

    def _entity_surface_for(self, scope_key: str, normalized_name: str) -> str | None:
        """The stored surface form of one entity node (by normalized name).

        Used so a resolved canonical keeps the display casing the graph
        already stores — re-upserting a different casing would rewrite the
        node's ``name`` and shift the scope state hash outside any receipted
        relationship mutation.
        """
        for node in self._graph.nodes_for_scope(scope_key):
            if "Episode" in node.labels:
                continue
            name = node.properties.get("name")
            if is_sealed_content(name):
                continue
            if isinstance(name, str) and normalize_key(name) == normalized_name:
                return name
        return None

    def _entity_provenance_tokens(self, metadata: dict[str, object] | None) -> frozenset[str]:
        """Normalized token set of one metadata dict's provenance coordinates."""
        tokens: set[str] = set()
        if isinstance(metadata, dict):
            for key in self._ENTITY_PROVENANCE_KEYS:
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    tokens.update(normalize_key(value).split())
        return frozenset(tokens)

    def _entity_neighbor_uuids(self, scope_key: str, node_uuid: str) -> frozenset[str]:
        """Other-endpoint node uuids of one node's memory relationships."""
        neighbors: set[str] = set()
        for relationship in self._graph.relationships_for_node_uuids(scope_key=scope_key, node_uuids=[node_uuid]):
            other = relationship.target_uuid if relationship.source_uuid == node_uuid else relationship.source_uuid
            neighbors.add(other)
        return frozenset(neighbors)

    def _mention_link_context(
        self,
        *,
        scope_key: str,
        mention_normalized: str,
        episode: Episode | None,
    ) -> EntityResolutionMixin._MentionLinkContext:
        """Resolve one mention's candidate-independent link inputs, once.

        Called immediately before the candidate scan; every field it holds is
        constant for the whole scan (see :class:`_MentionLinkContext`).
        """
        return EntityResolutionMixin._MentionLinkContext(
            scope_key=scope_key,
            normalized=mention_normalized,
            neighbors=self._mention_neighbor_uuids(scope_key, mention_normalized),
            episode_tokens=(self._entity_provenance_tokens(episode.metadata) if episode is not None else frozenset()),
            embed=self._embedding_transport.embed,
        )

    def _entity_link_signals(
        self,
        *,
        candidate: EntityResolutionMixin._EntityCandidate,
        mention_context: EntityResolutionMixin._MentionLinkContext,
    ) -> dict[str, float]:
        """The three offline signals of the composed link score, for ONE candidate.

        - ``name_cosine``: same-space cosine between the mention name's vector
          (resolved once per mention by ``mention_context``, never once per
          candidate) and the candidate node's stored ``name_embedding``.
          0.0 when the stored vector is unstamped or in another space
          (:func:`stored_vector_in_active_space` — never a cross-space cosine).
        - ``context_overlap``: Jaccard of the episode's provenance coordinates
          (metadata ``slug``/``section``/``source``) against the candidate's
          accumulated fact provenance (up to ``_ENTITY_PROVENANCE_FACT_CAP``
          facts in deterministic store order).
        - ``neighborhood_overlap``: shared neighbor nodes over the smaller
          degree, between the mention's EXISTING node (when prior episodes
          already materialized one) and the candidate node.

        A signal that cannot be computed contributes 0.0 — never fabricated.
        """
        scope_key = mention_context.scope_key
        name_cosine = 0.0
        stored_name_vector = candidate.node_properties.get("name_embedding")
        if stored_vector_in_active_space(
            candidate.node_properties, active_identifier=self._embedding_transport.identifier
        ) and isinstance(stored_name_vector, list):
            try:
                name_cosine = max(
                    0.0,
                    cosine_similarity(mention_context.name_embedding, stored_name_vector),
                )
            except ValueError:
                name_cosine = 0.0

        context_overlap = 0.0
        episode_tokens = mention_context.episode_tokens
        if episode_tokens:
            candidate_tokens: set[str] = set()
            fact_rows = self._graph.relationships_for_node_uuids(scope_key=scope_key, node_uuids=[candidate.node_uuid])[
                : self._ENTITY_PROVENANCE_FACT_CAP
            ]
            for relationship in fact_rows:
                candidate_tokens.update(self._entity_provenance_tokens(relationship.properties.get("metadata")))
            if candidate_tokens:
                union = episode_tokens | candidate_tokens
                context_overlap = len(episode_tokens & candidate_tokens) / len(union)

        neighborhood_overlap = 0.0
        mention_neighbors = mention_context.neighbors
        if mention_neighbors:
            candidate_neighbors = self._entity_neighbor_uuids(scope_key, candidate.node_uuid)
            shared = mention_neighbors & candidate_neighbors
            neighborhood_overlap = len(shared) / max(1, min(len(mention_neighbors), len(candidate_neighbors)))

        return {
            "name_cosine": name_cosine,
            "context_overlap": context_overlap,
            "neighborhood_overlap": neighborhood_overlap,
        }

    def _mention_neighbor_uuids(self, scope_key: str, mention_normalized: str) -> frozenset[str]:
        """Neighbor uuids of the mention's EXISTING node — empty when no prior
        episode materialized one (a missing signal, contributing 0.0)."""
        for node in self._graph.nodes_for_scope(scope_key):
            if "Episode" in node.labels:
                continue
            node_name = node.properties.get("name")
            if isinstance(node_name, str) and normalize_key(node_name) == mention_normalized:
                return self._entity_neighbor_uuids(scope_key, node.uuid)
        return frozenset()

    def _corroborate_entity_alias(
        self,
        *,
        scope_key: str,
        row: dict[str, object],
        link_confidence: float | None,
    ) -> None:
        """WS-16-style bounded accumulation on a corroborating alias resolution.

        Every formation-time resolution through an ACTIVE alias is one more
        observation that the link is real: ``s' = min(ceiling, s + (1 - s) *
        reinforcement_gain * c_obs)`` with ``c_obs`` the mention's extractor
        link confidence when supplied, else 1.0.  Evidence lives on the
        registry row (``link_score`` + a ``corroborations`` counter in
        ``link_signals``); registry rows are outside ``graph_state_hash``, so
        no receipt hash-bracketing applies (consistent with predicate_canon).
        """
        confidence_policy = self._config.confidence
        observation = float(link_confidence) if link_confidence is not None else 1.0
        observation = min(1.0, max(0.0, observation))
        current = float(row["link_score"]) if row["link_score"] is not None else 0.0
        updated = min(
            confidence_policy.ceiling,
            current + (1.0 - current) * confidence_policy.reinforcement_gain * observation,
        )
        signals = dict(row["link_signals"] or {})
        signals["corroborations"] = int(signals.get("corroborations", 0)) + 1
        self._graph.update_entity_alias_evidence(
            scope_key,
            str(row["name_normalized"]),
            link_score=updated,
            link_signals=signals,
        )

    def _rebridge_on_alias_activation(
        self,
        *,
        scope_key: str,
        alias_normalized: str,
        receipt_run: ReceiptRun | None,
        episode: Episode | None,
        now: datetime | None,
        motive_name: str | None,
        decided_by: str,
    ) -> None:
        """WS-23 C1: re-key existing rows the moment an alias becomes ACTIVE.

        Registration is the event; re-keying rides on it, in the same
        transaction, through the one rebridging primitive the operator backfills
        use.  Skipped for the backfill caller itself (it is already walking every
        row) and when there is no receipt run to attest the rewrites.
        """
        if receipt_run is None or decided_by == "backfill":
            return
        self.rebridge_scope_truth_keys(
            scope_key=scope_key,
            receipt_run=receipt_run,
            decision_type=ReceiptDecisionType.FORMATION_ENTITY_LINKED,
            reason_prefix="alias_activated",
            now=now or datetime.now(UTC),
            entity_names=frozenset({alias_normalized}),
            episode=episode,
            motive_name=motive_name,
        )

    def _alias_bridged_row_count(self, scope_key: str, alias_normalized: str, canonical_name: str) -> int:
        """WS-23 C1: live truth-slot rows this alias currently routes onto.

        A row counts when it is rebridgeable (ACTIVE or gate-parked) and its
        truth prefix is keyed on the CANONICAL name — those are exactly the
        slots a mention of the alias lands on today, so demoting the alias would
        split them from every future mention of the same surface.
        """
        canonical_normalized = normalize_key(canonical_name)
        if canonical_normalized == alias_normalized:
            return 0
        prefix = normalize_key(f"{scope_key}:{canonical_normalized}:")
        bridged = 0
        for relationship in self._graph.relationships_for_scope(scope_key):
            if not self._rebridgeable_row(relationship):
                continue
            properties = relationship.properties
            if is_sealed_content(properties.get("fact")):
                continue
            truth_prefix = properties.get("truth_prefix")
            if isinstance(truth_prefix, str) and truth_prefix.startswith(prefix):
                bridged += 1
        return bridged

    def _discount_entity_alias(
        self,
        *,
        scope_key: str,
        row: dict[str, object],
        challenger_confidence: float,
        reason: str,
        receipt_run: ReceiptRun | None,
        episode: Episode | None,
        now: datetime | None,
        motive_name: str | None,
    ) -> None:
        """WS-16-style contradiction discount on one ACTIVE alias row.

        ``s' = max(floor, s * (1 - contradiction_discount * c_challenger))``;
        when the discounted score drops below ``auto_link_threshold`` the alias
        is demoted ACTIVE → 'proposed' (receipted ENTITY_ALIAS_DEMOTED) — it
        reappears in the adjudication queue and formation stops resolving
        through it until a human (or a fresh auto-link) re-activates it.

        WS-23 C1 — the chosen semantics, in two parts:

        1. A TRUTH CONTRADICTION never demotes an alias that is currently
           routing live truth-slot rows.  Demoting it was self-defeating: the
           alias bridges a challenger onto the canonical slot, the resulting
           dispute demoted the alias, and every later mention of that surface
           landed back on the surface slot — the mechanism that resolved the
           contradiction was killed BY the contradiction, leaving two ACTIVE
           rows the single-active repair (keyed on ``truth_key``) can never see.
           The discount is still applied and the dispute still counted, so the
           evidence is on the record; only the status change is refused, and the
           refusal is receipted.
        2. An IDENTIFIER-TOKEN CONFLICT still demotes — differing coded
           identifiers must never stay linked, whatever else is true — but the
           demotion now re-keys the rows the alias was bridging BACK onto their
           own surface first, receipted, so the store never holds rows routed
           through a link formation no longer honours.
        """
        policy = self._config.entity_resolution
        confidence_policy = self._config.confidence
        challenger = min(1.0, max(0.0, float(challenger_confidence)))
        current = float(row["link_score"]) if row["link_score"] is not None else 1.0
        updated = max(
            confidence_policy.floor,
            current * (1.0 - confidence_policy.contradiction_discount * challenger),
        )
        signals = dict(row["link_signals"] or {})
        signals["disputes"] = int(signals.get("disputes", 0)) + 1
        signals["last_dispute_reason"] = reason
        anchor = now or datetime.now(UTC)
        bridged = self._alias_bridged_row_count(scope_key, str(row["name_normalized"]), str(row["canonical_name"]))
        if updated < policy.auto_link_threshold and bridged and reason == "contradictory_single_active_truth":
            self._graph.update_entity_alias_evidence(
                scope_key,
                str(row["name_normalized"]),
                link_score=updated,
                link_signals={**signals, "demotion_refused_bridged_rows": bridged},
            )
            if receipt_run is not None:
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.ENTITY_ALIAS_DEMOTED,
                    decision_reason=(
                        f"{row['name_normalized']}->{normalize_key(str(row['canonical_name']))}"
                        f":{reason}:demotion_refused_bridged_rows={bridged}"
                    ),
                    decision_result="gated",
                    episode=episode,
                    now=now,
                    scope_key=scope_key,
                    motive_name=motive_name,
                    dedup_threshold=policy.auto_link_threshold,
                    dedup_score=updated,
                    event_payload=json.dumps(signals, sort_keys=True, separators=(",", ":")),
                )
            return
        if updated < policy.auto_link_threshold:
            self._graph.resolve_entity_alias(
                scope_key,
                str(row["name_normalized"]),
                status="proposed",
                resolved_by="formation:contradiction_demotion",
                resolved_at=anchor,
                link_score=updated,
                link_signals=signals,
            )
            if receipt_run is not None:
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.ENTITY_ALIAS_DEMOTED,
                    decision_reason=(
                        f"{row['name_normalized']}->{normalize_key(str(row['canonical_name']))}"
                        f":{reason}:link_score={updated:.4f}"
                    ),
                    decision_result="demoted",
                    episode=episode,
                    now=now,
                    scope_key=scope_key,
                    motive_name=motive_name,
                    dedup_threshold=policy.auto_link_threshold,
                    dedup_score=updated,
                    event_payload=json.dumps(signals, sort_keys=True, separators=(",", ":")),
                )
            if bridged and receipt_run is not None:
                # The link is gone; the rows it routed return to their own
                # surface through the shared rebridging primitive.
                self.rebridge_scope_truth_keys(
                    scope_key=scope_key,
                    receipt_run=receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_ENTITY_LINKED,
                    reason_prefix="alias_demoted",
                    now=anchor,
                    entity_names=frozenset({str(row["name_normalized"])}),
                    episode=episode,
                    motive_name=motive_name,
                )
        else:
            self._graph.update_entity_alias_evidence(
                scope_key,
                str(row["name_normalized"]),
                link_score=updated,
                link_signals=signals,
            )

    def _discount_entity_aliases_on_conflict(
        self,
        *,
        scope: MemoryScope,
        canonical_subject: str,
        current_surface: str,
        conflicting_incumbents: list[GraphRelationship],
        challenger_confidence: float,
        receipt_run: ReceiptRun | None,
        episode: Episode | None,
        now: datetime | None,
        motive_name: str | None,
    ) -> None:
        """WS-17 T16b: alias confidence is alive — truth conflicts across the
        alias boundary discount the implicated link.

        When the canonical truth slot acquires contradictory truths and the two
        sides of the contradiction were stated under DIFFERENT surface forms of
        the canonical subject, every ACTIVE alias row that bridged one of those
        surfaces onto the canonical is discounted (and demoted below the
        auto-link bar).  A contradiction stated twice under the SAME surface is
        an ordinary truth change and touches no alias.
        """
        if not self._config.entity_resolution.enabled or not conflicting_incumbents:
            return
        canonical_normalized = normalize_key(canonical_subject)
        current_normalized = normalize_key(current_surface)
        implicated_surfaces: set[str] = set()
        for incumbent in conflicting_incumbents:
            incumbent_surface = incumbent.properties.get("subject_surface")
            if not isinstance(incumbent_surface, str) or not incumbent_surface.strip():
                node = self._graph.get_node(incumbent.source_uuid)
                revealed = self._graph.reveal(scope.key, node.properties.get("name", ""))
                incumbent_surface = revealed if isinstance(revealed, str) else ""
            incumbent_normalized = normalize_key(incumbent_surface)
            if incumbent_normalized and incumbent_normalized != current_normalized:
                implicated_surfaces.add(incumbent_normalized)
                implicated_surfaces.add(current_normalized)
        for surface in sorted(implicated_surfaces):
            row = self._graph.entity_alias_row(scope.key, surface)
            if row is None or row["status"] != "active":
                continue
            if normalize_key(str(row["canonical_name"])) != canonical_normalized:
                continue
            self._discount_entity_alias(
                scope_key=scope.key,
                row=row,
                challenger_confidence=challenger_confidence,
                reason="contradictory_single_active_truth",
                receipt_run=receipt_run,
                episode=episode,
                now=now,
                motive_name=motive_name,
            )

    def resolve_canonical_entity(
        self,
        *,
        scope: MemoryScope,
        name: str,
        entity_ref: str | None = None,
        link_confidence: float | None = None,
        receipt_run: ReceiptRun | None = None,
        episode: Episode | None = None,
        now: datetime | None = None,
        motive_name: str | None = None,
        protection: _ContentProtection | None = None,
        decided_by: str = "formation",
    ) -> _EntityResolution:
        """WS-17 T16b: resolve one mention name to its scope-canonical entity.

        Deterministic resolution order (mirrors ``resolve_canonical_predicate``):

        1. ACTIVE registry hit — reinforce the living link score (bounded
           accumulation) and resolve, unless an identifier-token conflict has
           appeared between alias and canonical (then demote instead).
        2. Operator synonym map — register ACTIVE + resolve (receipted).
        3. Composed link score against the scope's canonical candidates:
           the ``entity_ref`` the extractor attested (llm term = its link
           confidence) when it names a known canonical, else the best offline
           candidate (llm term = 0).  Identifier-token conflicts hard-block a
           pair regardless of score (receipted when the pair would otherwise
           have proposed).  Score >= ``auto_link_threshold`` registers an
           ACTIVE alias (FORMATION_ENTITY_LINKED) and resolves; the review
           band registers a 'proposed' row (ENTITY_ALIAS_PROPOSED) while the
           mention materializes under its surface; below the review band
           nothing is written.

        Entity resolution is INERT for content-protected (crypto-shred) scopes:
        the registry stores plaintext names, and entity names are content
        plane — persisting them outside the sealed stores would survive the
        ciphertext-only sweep and break the erasure contract.
        """
        policy = self._config.entity_resolution
        if not policy.enabled or protection is not None:
            return _EntityResolution(canonical=name)
        normalized = normalize_key(name)
        if not normalized:
            return _EntityResolution(canonical=name)
        scope_key = scope.key

        existing_row = self._graph.entity_alias_row(scope_key, normalized)
        if existing_row is not None and existing_row["status"] == "active":
            canonical_name = str(existing_row["canonical_name"])
            if identifier_tokens_conflict(name, canonical_name):
                # An identifier-token conflict on a live link is a standing
                # conflict signal (operator synonyms bypass the hard block):
                # discount/demote and stop resolving through the row.
                self._discount_entity_alias(
                    scope_key=scope_key,
                    row=existing_row,
                    challenger_confidence=1.0,
                    reason="identifier_token_conflict",
                    receipt_run=receipt_run,
                    episode=episode,
                    now=now,
                    motive_name=motive_name,
                )
                refreshed = self._graph.entity_alias_row(scope_key, normalized)
                if refreshed is not None and refreshed["status"] == "active":
                    return _EntityResolution(canonical=str(refreshed["canonical_name"]), alias_row=refreshed)
                return _EntityResolution(canonical=name)
            self._corroborate_entity_alias(scope_key=scope_key, row=existing_row, link_confidence=link_confidence)
            return _EntityResolution(canonical=canonical_name, alias_row=existing_row)
        if existing_row is not None and existing_row["status"] == "proposed":
            # A pending proposal neither bridges nor re-scores: the mention
            # materializes under its surface until adjudication (or a fresh
            # auto-band observation) settles the row.
            return _EntityResolution(canonical=name)

        mapped = policy.synonyms.get(normalized)
        if mapped is not None and existing_row is None:
            # The map target may itself be an ACTIVE alias — resolve one level
            # so the registry never chains (facts must land on the terminal
            # canonical node, not an intermediate alias).
            mapped_normalized = normalize_key(mapped)
            mapped_active = self._graph.canonical_entity_name_for(scope_key, mapped_normalized)
            if mapped_active is not None:
                mapped = mapped_active
                mapped_normalized = normalize_key(mapped_active)
            if mapped_normalized == normalized:
                # A circular operator map ("a"->"b" with "b"->"a") resolves to
                # the mention itself — nothing to bridge; fail safe, not loud.
                return _EntityResolution(canonical=name)
            canonical_surface = self._entity_surface_for(scope_key, mapped_normalized) or mapped
            registered = self._graph.register_entity_alias(
                scope_key,
                normalized,
                canonical_surface,
                status="active",
                decided_by=f"{decided_by}:synonym_map",
                link_score=1.0,
                link_signals={"synonym_map": True},
                now=now,
            )
            if registered["status"] == "active":
                if receipt_run is not None:
                    self._emit_receipt(
                        receipt_run,
                        decision_type=ReceiptDecisionType.FORMATION_ENTITY_LINKED,
                        decision_reason=(
                            f"{normalized}->{normalize_key(str(registered['canonical_name']))}:synonym_map"
                        ),
                        decision_result="recorded",
                        episode=episode,
                        now=now,
                        scope_key=scope_key,
                        motive_name=motive_name,
                        dedup_threshold=policy.auto_link_threshold,
                        dedup_score=1.0,
                        event_payload=json.dumps({"synonym_map": True}, sort_keys=True, separators=(",", ":")),
                    )
                self._rebridge_on_alias_activation(
                    scope_key=scope_key,
                    alias_normalized=normalized,
                    receipt_run=receipt_run,
                    episode=episode,
                    now=now,
                    motive_name=motive_name,
                    decided_by=decided_by,
                )
                return _EntityResolution(canonical=str(registered["canonical_name"]), alias_row=registered)
            return _EntityResolution(canonical=name)

        candidates = self._entity_resolution_candidates(scope_key, exclude_normalized=normalized)
        if not candidates:
            return _EntityResolution(canonical=name)

        selected: EntityResolutionMixin._EntityCandidate | None = None
        llm_term = 0.0
        if entity_ref is not None:
            ref_normalized = normalize_key(entity_ref)
            ref_active = self._graph.canonical_entity_name_for(scope_key, ref_normalized)
            if ref_active is not None:
                ref_normalized = normalize_key(ref_active)
            if ref_normalized == normalized:
                # The extractor says the mention IS the canonical — nothing to bridge.
                return _EntityResolution(canonical=name)
            selected = next(
                (candidate for candidate in candidates if candidate.normalized == ref_normalized),
                None,
            )
            if selected is not None:
                llm_term = float(link_confidence or 0.0)

        scan: list[tuple[EntityResolutionMixin._EntityCandidate, float]] = (
            [(selected, llm_term)] if selected is not None else [(candidate, 0.0) for candidate in candidates]
        )
        best_candidate: EntityResolutionMixin._EntityCandidate | None = None
        best_score = 0.0
        best_signals: dict[str, float] = {}
        blocked_candidate: EntityResolutionMixin._EntityCandidate | None = None
        blocked_score = 0.0
        # Everything the score reads off the MENTION — its neighborhood, the
        # episode's provenance tokens, and the mention name's vector — is
        # constant across this scan, so it is resolved once, not once per
        # candidate (the embedding is a network call on a hosted transport).
        mention_context = self._mention_link_context(
            scope_key=scope_key, mention_normalized=normalized, episode=episode
        )
        for candidate, candidate_llm in scan:
            signals = self._entity_link_signals(candidate=candidate, mention_context=mention_context)
            score = composed_entity_link_score(
                policy=policy,
                llm_confidence=candidate_llm,
                name_cosine=signals["name_cosine"],
                context_overlap=signals["context_overlap"],
                neighborhood_overlap=signals["neighborhood_overlap"],
            )
            if identifier_tokens_conflict(name, candidate.surface):
                # HARD BLOCK: differing identifier tokens can never link or
                # propose — the score is forced to 0 for this pair.
                if score > blocked_score:
                    blocked_score = score
                    blocked_candidate = candidate
                continue
            # Strict > keeps the earliest candidate (store order) on ties.
            if score > best_score:
                best_score = score
                best_candidate = candidate
                best_signals = {**signals, "llm_confidence": candidate_llm}

        if blocked_candidate is not None and blocked_score >= policy.review_threshold and receipt_run is not None:
            # The pair would have proposed (or auto-linked) on similarity alone;
            # receipt the refusal so the negative decision is auditable.
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.ENTITY_ALIAS_PROPOSED,
                decision_reason=(f"identifier_token_mismatch:{normalized}->{blocked_candidate.normalized}"),
                decision_result="rejected",
                episode=episode,
                now=now,
                scope_key=scope_key,
                motive_name=motive_name,
                dedup_threshold=policy.review_threshold,
                dedup_score=blocked_score,
            )

        if best_candidate is None or best_score < policy.review_threshold:
            return _EntityResolution(canonical=name)

        link_signals = {
            "llm_confidence": float(best_signals.get("llm_confidence", 0.0)),
            "name_cosine": float(best_signals.get("name_cosine", 0.0)),
            "context_overlap": float(best_signals.get("context_overlap", 0.0)),
            "neighborhood_overlap": float(best_signals.get("neighborhood_overlap", 0.0)),
            "composed_score": best_score,
        }
        signals_payload = json.dumps(link_signals, sort_keys=True, separators=(",", ":"))

        if existing_row is not None and existing_row["status"] == "rejected":
            # Never re-propose a rejected pair at the same score class: the new
            # score must beat the recorded rejection by >= 0.05.
            previous_signals = dict(existing_row["link_signals"] or {})
            last_rejected = float(
                previous_signals.get(
                    "last_rejected_score",
                    existing_row["link_score"] if existing_row["link_score"] is not None else 0.0,
                )
            )
            same_pair = normalize_key(str(existing_row["canonical_name"])) == best_candidate.normalized
            if same_pair and best_score < last_rejected + 0.05:
                return _EntityResolution(canonical=name)
            link_signals["last_rejected_score"] = last_rejected
            self._graph.resolve_entity_alias(
                scope_key,
                normalized,
                status="proposed",
                resolved_by=f"{decided_by}:re_proposed",
                resolved_at=now or datetime.now(UTC),
                link_score=best_score,
                link_signals=link_signals,
                canonical_name=best_candidate.surface,
            )
            if receipt_run is not None:
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.ENTITY_ALIAS_PROPOSED,
                    decision_reason=(
                        f"{normalized}->{best_candidate.normalized}:re_proposed:link_score={best_score:.4f}"
                    ),
                    decision_result="recorded",
                    episode=episode,
                    now=now,
                    scope_key=scope_key,
                    motive_name=motive_name,
                    dedup_threshold=policy.auto_link_threshold,
                    dedup_score=best_score,
                    event_payload=json.dumps(link_signals, sort_keys=True, separators=(",", ":")),
                )
            return _EntityResolution(canonical=name)

        if best_score >= policy.auto_link_threshold:
            registered = self._graph.register_entity_alias(
                scope_key,
                normalized,
                best_candidate.surface,
                status="active",
                decided_by=f"{decided_by}:auto_link",
                link_score=best_score,
                link_signals=link_signals,
                embedding_identifier=self._embedding_transport.identifier,
                now=now,
            )
            if registered["status"] != "active":
                return _EntityResolution(canonical=name)
            if receipt_run is not None:
                self._emit_receipt(
                    receipt_run,
                    decision_type=ReceiptDecisionType.FORMATION_ENTITY_LINKED,
                    decision_reason=(
                        f"{normalized}->{best_candidate.normalized}:auto_link:link_score={best_score:.4f}"
                    ),
                    decision_result="recorded",
                    episode=episode,
                    now=now,
                    scope_key=scope_key,
                    motive_name=motive_name,
                    dedup_threshold=policy.auto_link_threshold,
                    dedup_score=best_score,
                    event_payload=signals_payload,
                )
            self._rebridge_on_alias_activation(
                scope_key=scope_key,
                alias_normalized=normalized,
                receipt_run=receipt_run,
                episode=episode,
                now=now,
                motive_name=motive_name,
                decided_by=decided_by,
            )
            return _EntityResolution(canonical=str(registered["canonical_name"]), alias_row=registered)

        registered = self._graph.register_entity_alias(
            scope_key,
            normalized,
            best_candidate.surface,
            status="proposed",
            decided_by=f"{decided_by}:proposed",
            link_score=best_score,
            link_signals=link_signals,
            embedding_identifier=self._embedding_transport.identifier,
            now=now,
        )
        if registered["status"] == "proposed" and receipt_run is not None:
            self._emit_receipt(
                receipt_run,
                decision_type=ReceiptDecisionType.ENTITY_ALIAS_PROPOSED,
                decision_reason=(f"{normalized}->{best_candidate.normalized}:proposed:link_score={best_score:.4f}"),
                decision_result="recorded",
                episode=episode,
                now=now,
                scope_key=scope_key,
                motive_name=motive_name,
                dedup_threshold=policy.auto_link_threshold,
                dedup_score=best_score,
                event_payload=signals_payload,
            )
        return _EntityResolution(canonical=name)

    def _stamp_node_name_embedding(
        self,
        *,
        node: object,
        name: str,
        labels: tuple[str, ...],
        valid_from: datetime | None,
        valid_to: datetime | None,
    ) -> None:
        """Stamp ``name_embedding`` + ``embedding_identifier`` onto an entity node.

        Same space-guard discipline as the relationship vectors (WS-17 pt1):
        the stamp names the vector space, and a node whose stored stamp
        mismatches the active transport is re-embedded in the ACTIVE space on
        its next formation touch.  Validity bounds are re-passed unchanged so
        the property-merge upsert cannot move them.
        """
        properties = getattr(node, "properties", {})
        stored = properties.get("name_embedding")
        if isinstance(stored, list) and stored_vector_in_active_space(
            properties, active_identifier=self._embedding_transport.identifier
        ):
            return
        self._graph.upsert_node(
            labels=labels,
            key=str(properties.get("graph_key")),
            properties={
                "name_embedding": self._embedding_transport.embed(normalize_key(name)),
                "embedding_identifier": self._embedding_transport.identifier,
            },
            valid_from=valid_from,
            valid_to=valid_to,
        )

    def entity_inventory_for_scope(self, scope: MemoryScope) -> list[dict[str, str]]:
        """WS-17 T16b: compact canonical entity inventory for the extraction prompt.

        Canonical names + kinds only (cheap — scales where the bounded fact
        context cannot), deterministic node order, capped at ``max_inventory``.
        Empty when entity resolution is disabled or the scope is content
        protected (sealed names are content plane and never leave the store).
        """
        policy = self._config.entity_resolution
        if not policy.enabled:
            return []
        registry = {row["name_normalized"]: row for row in self._graph.entity_alias_rows_for_scope(scope.key)}
        inventory: list[dict[str, str]] = []
        seen: set[str] = set()
        for node in self._graph.nodes_for_scope(scope.key):
            if "Episode" in node.labels:
                continue
            name = node.properties.get("name")
            if is_sealed_content(name):
                return []
            if not isinstance(name, str) or not name.strip():
                continue
            normalized = normalize_key(name)
            if not normalized or normalized in seen:
                continue
            row = registry.get(normalized)
            if row is not None and row["status"] in ("active", "proposed"):
                continue
            seen.add(normalized)
            entry = {"name": name}
            kind = node.properties.get("kind")
            if isinstance(kind, str) and kind.strip():
                entry["kind"] = kind
            inventory.append(entry)
            if len(inventory) >= policy.max_inventory:
                break
        return inventory

    def _register_declared_aliases(
        self,
        *,
        scope: MemoryScope,
        label: str,
        canonical_name: str,
        properties: dict[str, Any],
        instructions: DreamInstructionSet,
        protection: _ContentProtection | None,
        now: datetime | None,
    ) -> None:
        """WS-27 T1: write extractor-declared aliases into ``entity_canon``.

        An entity's ``aliases`` property (universal on every label — see
        :data:`~memotron.config.UNIVERSAL_NODE_PROPERTIES`), plus any of
        this label's extra alias-bearing keys (``NodeInstruction.aliases``),
        name alternate surface forms for the SAME entity ("GCX" for "guest
        content experience").  Each one is registered ACTIVE into the SAME
        per-scope ``entity_canon`` registry :meth:`resolve_canonical_entity`
        (WS-17 T16b) reads and writes, so a later mention of "GCX" resolves
        to the canonical node through the ordinary alias-registry read path —
        no separate lookup table.

        Inert for content-protected (crypto-shred) scopes, mirroring
        :meth:`resolve_canonical_entity`: the registry stores plaintext
        names, which is content plane under crypto-shred governance.
        Best-effort: one malformed alias value costs that one alias, never
        the memory it was declared on (mirrors this file's "one bad candidate
        costs one candidate" contract).
        """
        if protection is not None:
            return
        node_instruction = next(
            (instruction for instruction in instructions.node_instructions if instruction.label == label),
            None,
        )
        alias_keys = {"aliases"}
        if node_instruction is not None:
            alias_keys |= set(node_instruction.aliases)
        raw_values: list[Any] = []
        for key in alias_keys:
            value = properties.get(key)
            if isinstance(value, (list, tuple)):
                raw_values.extend(value)
            elif value is not None:
                raw_values.append(value)
        for raw_alias in raw_values:
            if not isinstance(raw_alias, str):
                continue
            alias = raw_alias.strip()
            if not alias or normalize_key(alias) == normalize_key(canonical_name):
                continue
            try:
                self._graph.register_entity_alias(
                    scope.key,
                    alias,
                    canonical_name,
                    status="active",
                    decided_by="extraction:declared_alias",
                    link_signals={"declared_alias": True},
                    now=now,
                )
            except ValueError as exc:
                _log.warning(
                    "scope %s: declared alias %r -> %r not registered (%s)",
                    scope.key,
                    alias,
                    canonical_name,
                    exc,
                )
