"""Stable identity for nodes and truths, and the dual-representation invariant.

node_identity_key and truth_identity decide when two things are THE SAME thing, so
changing either silently re-partitions the graph: existing rows keep their old keys
and new writes get new ones, and nothing raises. memotron.migration imports both
by name from memotron.dreaming -- that import path is load-bearing and pinned by
the API-surface golden.

assert_dual_representation_invariant is the fail-closed check that a claim's vector
and graph representations agree; DualRepresentationInvariantError is raised, caught
and asserted on by tests/test_ontology_2026.py, so it is public surface."""

from __future__ import annotations

from dataclasses import dataclass

from memotron.config import (
    EntityResolutionPolicy,
)
from memotron.dreaming._common import (
    _ContentProtection,
)
from memotron.graph import normalize_key
from memotron.models import (
    GraphRelationship,
    RelationshipCardinality,
)


class DualRepresentationInvariantError(RuntimeError):
    """WS-27 T3: a relation was about to materialize missing its triple or its
    embedded fact sentence.  Every relation this graph stores must carry BOTH
    a structured ``(subject, predicate, object)`` triple (for traversal and
    for the LLM reading the graph) AND a stored embedding of the full NL fact
    sentence (for ANN retrieval — see the README's "dual representation"
    principle).  This fails CLOSED: raised before ``add_relationship`` is
    ever called, so a broken invariant never reaches the graph as a
    half-written row.  By construction this should be unreachable — the
    triple's fields are non-blank by ``ExtractedMemory`` field validation and
    ``fact_embedding`` is computed unconditionally above — so reaching this
    means either an ``EmbeddingTransport`` returned an empty/degenerate
    vector (a hosted transport failing open) or a future refactor moved the
    embed call. Either way, better a loud abort than a silently
    unretrievable memory."""


def assert_dual_representation_invariant(
    *, subject: str, predicate: str, object_text: str, fact_embedding: list[float]
) -> None:
    """Fail closed unless a materializing relation carries a real triple AND a
    real embedded fact sentence.  Pure and side-effect-free — see
    :class:`DualRepresentationInvariantError`."""
    missing_triple = [
        field_name
        for field_name, value in (
            ("subject", subject),
            ("predicate", predicate),
            ("object", object_text),
        )
        if not value or not value.strip()
    ]
    if missing_triple:
        raise DualRepresentationInvariantError(
            f"dual-representation invariant violated: relation is missing triple field(s) {missing_triple!r}"
        )
    if not fact_embedding:
        raise DualRepresentationInvariantError(
            "dual-representation invariant violated: relation "
            f"{subject!r} {predicate!r} {object_text!r} has no embedded fact sentence "
            "(the configured EmbeddingTransport returned an empty vector)"
        )


def composed_entity_link_score(
    *,
    policy: EntityResolutionPolicy,
    llm_confidence: float,
    name_cosine: float,
    context_overlap: float,
    neighborhood_overlap: float,
) -> float:
    """WS-17 T16b: pure composed entity-link score.

    ``w_llm*llm + w_name*name + w_ctx*context + w_nbr*neighborhood``, each
    signal clamped to [0, 1] and contributing 0.0 when missing — the caller
    passes 0.0 for a signal it could not compute (mismatched vector space,
    no provenance coordinates, no existing mention node); nothing is ever
    fabricated to reach a band.
    """

    def _clamp(value: float) -> float:
        return min(1.0, max(0.0, value))

    return (
        policy.weight_llm * _clamp(llm_confidence)
        + policy.weight_name * _clamp(name_cosine)
        + policy.weight_context * _clamp(context_overlap)
        + policy.weight_neighborhood * _clamp(neighborhood_overlap)
    )


@dataclass(frozen=True)
class _EntityResolution:
    """WS-17 T16b: outcome of resolving one mention name for one scope.

    ``canonical`` is the name formation should materialize under (the surface
    itself when nothing linked); ``alias_row`` is the ACTIVE registry row the
    resolution travelled through, when one did — the row the truth-slot
    contradiction check discounts.
    """

    canonical: str
    alias_row: dict[str, object] | None = None


@dataclass(frozen=True)
class _TruthGateDecision:
    """WS-16 T12/T15: outcome of the supersession gate over one contradiction set.

    ``gated`` — the challenger must be parked (inserted pre-SUPERSEDED with
    ``requires_operator_review``) instead of superseding current truth;
    ``gated_incumbent`` is the surviving row the park points at (and the
    T13 dispute-discount target).  When not gated,
    ``corroborated_parked_siblings`` carries the previously parked challenger
    rows counted by the corroboration escape, to be resolved once the new
    active row exists.  ``recency_bypassed_incumbents`` (WS-25 T2) carries the
    subset of *this decision's* contradictions that were NOT gated only
    because the recency-authoritative bypass overrode a
    ``temporary_high_severity``/``insufficient_corroboration`` reason — every
    such incumbent is superseded with ``valid_to`` clamped to the candidate's
    effective time and a ``recency_authoritative_supersede`` receipt, instead
    of the ordinary supersession valid_to/reason.
    """

    gated: bool
    gate_reason: str | None
    gated_incumbent: GraphRelationship | None
    corroborated_parked_siblings: tuple[GraphRelationship, ...] = ()
    recency_bypassed_incumbents: tuple[GraphRelationship, ...] = ()


def node_identity_key(
    *,
    scope_key: str,
    label: str,
    name: str,
    protection: _ContentProtection | None,
) -> str:
    """WS-13: scope-dependent identity key for one entity node.

    Extracted out of ``_materialize_episode`` so the tenant-migration write
    path (``migration.py``) can recompute the SAME node identity key for a
    destination scope without duplicating this logic (a migrated fact is not
    re-run through formation's candidate-gating, but it must still land on
    the identical key a fresh formation write would have used).
    """
    key = f"{scope_key}:{label}:{name}"
    if protection is not None:
        return protection.commit("node", normalize_key(key))
    return key


def truth_identity(
    *,
    scope_key: str,
    subject: str,
    predicate: str,
    object_value: str,
    cardinality: RelationshipCardinality,
    protection: _ContentProtection | None,
) -> tuple[str, str, str | None]:
    """WS-13: ``(truth_key, truth_prefix, object_commitment)`` for one memory triple.

    Extracted out of ``_materialize_episode`` for the same reason as
    :func:`node_identity_key` — shared, testable, single source of truth for
    both the formation write path and the migration write path.
    """
    truth_prefix = f"{scope_key}:{subject}:{predicate}"
    if cardinality == RelationshipCardinality.SINGLE_ACTIVE:
        truth_key = f"{scope_key}:{subject}:{predicate}"
    else:
        truth_key = f"{scope_key}:{subject}:{predicate}:{object_value}"
    if protection is not None:
        stored_truth_key = protection.commit("truth_key", normalize_key(truth_key))
        stored_truth_prefix = protection.commit("truth_prefix", normalize_key(truth_prefix))
        object_commitment = protection.commit("object", normalize_key(object_value))
    else:
        stored_truth_key = normalize_key(truth_key)
        stored_truth_prefix = normalize_key(truth_prefix)
        object_commitment = None
    return stored_truth_key, stored_truth_prefix, object_commitment


@dataclass(frozen=True)
class RegovernResult:
    """Outcome of one :meth:`DreamEngine.regovern_scope` pass.

    The re-govern analogue of :class:`~memotron.models.DreamJobRun` — a
    summary of a stage-3-only pass over an already-stored raw graph, with zero
    extraction/LLM cost by construction (see :meth:`DreamEngine.regovern_scope`).
    """

    scope_key: str
    candidates_considered: int
    promoted: int
    still_quarantined: int
    skipped_no_episode: int
    run_uuid: str | None
