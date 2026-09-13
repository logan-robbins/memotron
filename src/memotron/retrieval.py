"""WS-5: Deterministic six-stage retrieval pipeline.

Stages
------
1. Scope + temporal filtering        (client-side visibility checks, unchanged)
2. Lexical + vector candidates       (token overlap + hashed-trigram cosine)
3. Seed-node resolution              (entity nodes matching the query)
4. Bounded weighted expansion        (beam over entity / ROLLUP-member links)
5. Current-truth + governance filter (visibility, rollup_stale, decrypt-on-read)
6. Weighted rerank                   (relevance, confidence, recency, scope,
                                      receipt-derived utility, per-type and
                                      Motive weighting)

Everything in this module is pure and deterministic: no model calls, no
wall-clock reads, no randomness.  The full knob set is pinned into a
RetrievalContract whose digest becomes the ``retrieval_policy_digest`` on use
events and search receipts — a retrieval result set is exactly reproducible
from (graph state, contract, query, as_of anchor).
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from memotron.config import Motive, RetrievalContract, RetrievalPolicy
from memotron.embedding import (
    LOCAL_EMBEDDING_IDENTIFIER,
    LocalEmbeddingTransport,
    cosine_similarity,
)
from memotron.receipts import payload_digest
from memotron.storage import normalize_key

EMBEDDING_IDENTIFIER = LOCAL_EMBEDDING_IDENTIFIER
"""Default identifier pinned into a RetrievalContract.

Re-exported from ``embedding.LOCAL_EMBEDDING_IDENTIFIER`` (the single source of
truth) and equal to ``LocalEmbeddingTransport.identifier``.  Searches under a
non-default transport pin that transport's own ``identifier`` instead, so a
contract digest always names the exact vector space that produced its results.
"""

_EMBEDDER = LocalEmbeddingTransport()


def embed_text(text: str) -> list[float]:
    """Embed text with the pinned deterministic transport."""
    return _EMBEDDER.embed(text)


def query_digest(query: str) -> str:
    return hashlib.sha256(query.encode("utf-8")).hexdigest()


def build_retrieval_contract(
    *,
    operation: str,
    scope_keys: tuple[str, ...],
    query: str,
    limit: int,
    policy: RetrievalPolicy,
    embedding_identifier: str = EMBEDDING_IDENTIFIER,
    motive: str | None = None,
    as_of: datetime | None = None,
) -> RetrievalContract:
    return RetrievalContract(
        operation=operation,
        scope_keys=scope_keys,
        query_digest=query_digest(query),
        limit=limit,
        policy=policy,
        embedding_identifier=embedding_identifier,
        motive=motive,
        as_of=as_of.isoformat() if as_of is not None else None,
    )


def retrieval_contract_digest(contract: RetrievalContract) -> str:
    """Canonical digest of the contract — the retrieval_policy_digest."""
    return payload_digest(contract.model_dump(mode="json"))


@dataclass(frozen=True)
class CandidateOrigin:
    """How a relationship entered the candidate set (explainability)."""

    kind: str
    """``lexical``, ``vector``, ``seed``, ``entity_hop``, ``rollup_member``,
    ``member_rollup``, or ``pinned`` (WS-20 T23 guaranteed-inclusion sweep)."""
    hops: int
    via_uuid: str | None = None
    """The relationship or node uuid this candidate was expanded from."""


@dataclass
class ScoredCandidate:
    relationship_uuid: str
    relevance: float
    origin: CandidateOrigin
    relationship: Any = None
    """The hydrated GraphRelationship (kept untyped to stay store-agnostic)."""

    def merge(self, other: ScoredCandidate) -> ScoredCandidate:
        """Keep the strongest relevance / shortest-hop origin for duplicates."""
        if (other.relevance, -other.origin.hops) > (self.relevance, -self.origin.hops):
            return other
        return self


def lexical_overlap(query_tokens: frozenset[str], searchable_tokens: frozenset[str]) -> float:
    """Normalized token overlap in [0, 1]: |query ∩ text| / |query|."""
    if not query_tokens:
        return 0.0
    return len(query_tokens & searchable_tokens) / len(query_tokens)


def relevance_score(
    policy: RetrievalPolicy,
    *,
    lexical: float,
    vector: float,
) -> float:
    """Stage-2 relevance: weighted mix of lexical and vector signals, in [0, 1].

    Normalized by the weight sum so relevance stays comparable when an operator
    turns one signal off.  Both weights zero → relevance is zero (fail closed).
    """
    weight_sum = policy.lexical_weight + policy.vector_weight
    if weight_sum <= 0.0:
        return 0.0
    mixed = policy.lexical_weight * lexical + policy.vector_weight * vector
    return max(0.0, min(1.0, mixed / weight_sum))


def recency_decay(
    *,
    anchor: datetime,
    valid_from: datetime | None,
    half_life_days: float,
) -> float:
    """Closed-form exponential decay in (0, 1]; unknown age decays fully to 0."""
    if valid_from is None:
        return 0.0
    age_days = max(0.0, (anchor - valid_from).total_seconds() / 86_400.0)
    return math.pow(2.0, -age_days / half_life_days)


def scope_priority(*, scope_rank: int, scope_count: int) -> float:
    """Earlier scopes in the caller's list rank higher, in (0, 1]."""
    if scope_count <= 0:
        return 0.0
    return (scope_count - scope_rank) / scope_count


def effective_type_weight(
    policy: RetrievalPolicy,
    *,
    memory_type: str | None,
    motive: Motive | None,
) -> float:
    """Per-type multiplier: policy.type_weights overlaid with the Motive boost.

    A Motive with ``retrieval_budget_share`` set boosts its
    ``allowed_memory_types`` by ``1 + retrieval_budget_share`` — the same
    budget-share signal profile() uses for token allocation, applied here as a
    rank preference.  Types outside the Motive's allowed set keep their policy
    weight; an empty allowed set means the Motive expresses no type preference.
    """
    weight = 1.0
    if memory_type is not None:
        weight = policy.type_weights.get(memory_type, 1.0)
    if (
        motive is not None
        and motive.retrieval_budget_share is not None
        and motive.allowed_memory_types
        and memory_type is not None
        and any(allowed.value == memory_type for allowed in motive.allowed_memory_types)
    ):
        weight *= 1.0 + motive.retrieval_budget_share
    return weight


def rerank_score(
    policy: RetrievalPolicy,
    *,
    relevance: float,
    confidence: float,
    recency: float,
    scope_priority_value: float,
    type_weight: float,
    utility: float = 0.0,
) -> float:
    """Stage-6 final score.

    ``(w_rel·relevance + w_conf·confidence + w_rec·recency + w_scope·scope
    + w_util·utility) × type_weight`` — every term deterministic and bounded,
    so equal inputs always produce equal ranks.  ``utility`` is the candidate's
    receipt-derived ``use_need`` (WS-15 T11); with the default
    ``utility_weight=0.0`` the term vanishes and ranking is byte-identical to
    the pre-utility pipeline.
    """
    base = (
        policy.relevance_weight * relevance
        + policy.confidence_weight * confidence
        + policy.recency_weight * recency
        + policy.scope_priority_weight * scope_priority_value
        + policy.utility_weight * utility
    )
    return base * type_weight


def vector_similarity(query_vector: list[float], text: str) -> float:
    """Cosine of the query against text, clamped to [0, 1]."""
    return max(0.0, cosine_similarity(query_vector, embed_text(text)))


def resolve_effective_utility_weight(policy: RetrievalPolicy, *, event_volume: int) -> float:
    """WS-28 T2: the ``utility_weight`` actually used for one search.

    An explicit non-zero ``policy.utility_weight`` always wins (byte-identical
    to the WS-15 T11 contract).  Otherwise, when
    ``policy.utility_weight_auto_floor_events`` is configured (opt-in;
    ``None`` is the shipped default) and *event_volume* — the scope's summed
    ``MemoryUtilityProjection.impression_count`` from the SAME anchored batch
    read the rerank already needs — reaches that floor,
    ``policy.utility_weight_when_unlocked`` activates.  Below the floor, or
    with no floor configured, returns exactly ``0.0``.

    Pure and deterministic: the same ``(policy, event_volume)`` always
    produce the same result.  This function does not decide WHETHER to read
    the event plane — callers must apply the ``policy.utility_weight == 0.0
    and policy.utility_weight_auto_floor_events is None`` short-circuit
    themselves before spending a projection read, so a vanilla default
    policy never touches the event plane at all.
    """
    if policy.utility_weight != 0.0:
        return policy.utility_weight
    if policy.utility_weight_auto_floor_events is None:
        return 0.0
    if event_volume >= policy.utility_weight_auto_floor_events:
        return policy.utility_weight_when_unlocked
    return 0.0


def use_need(*, use_stability: float, outcome_quality: float) -> float:
    """WS-15: probability-of-future-need estimand from the utility projection.

    ``(1 − e^(−use_stability)) × outcome_quality`` — the single formula shared
    by retention's expected-loss ranking and the stage-6 rerank utility term
    (T11), so the read side and the retention side value usefulness identically.
    Bounded in [0, 1]; a row with no use events has stability 0 → need 0.
    """
    return (1.0 - math.exp(-max(0.0, use_stability))) * outcome_quality


# ---------------------------------------------------------------------------
# WS-25 T1: negation/polarity primitives and the object-independent truth-slot
# contradiction predicate.
#
# These live here — not in ``dreaming.py`` — so both the write-side
# polarity-conflict gate (WS-16 T15) and the read-side within-slot recency
# tiebreaker (T3, below) share ONE implementation.  ``dreaming.py`` already
# imports ``use_need`` from this module, so this module must stay free of any
# import of ``dreaming.py`` — these primitives could not live there without
# creating an import cycle.
# ---------------------------------------------------------------------------

NEGATION_MARKERS: tuple[str, ...] = (
    " not ",
    " never ",
    " no longer ",
    " mustn't ",
    " must not ",
    " cannot ",
    " can't ",
    " forbidden ",
    " prohibited ",
    " disallowed ",
)
"""Semantic-polarity negation markers shared by :func:`semantic_polarity` and
:func:`strip_negation_markers`."""


def strip_negation_markers(text: str) -> str:
    """Normalized text with every negation marker removed (WS-16 T15).

    Used for the exact-object polarity-conflict comparison: "not dark mode"
    strips to "dark mode" so a negated restatement of a multi-active fact is
    recognized as the SAME statement with opposite polarity.  Compound markers
    are removed longest-first so " must not " does not leave a residue.
    """
    stripped = f" {normalize_key(text)} "
    for marker in sorted(NEGATION_MARKERS, key=len, reverse=True):
        stripped = stripped.replace(marker, " ")
    return " ".join(stripped.split())


def semantic_polarity(text: str) -> str:
    """Whole-statement polarity: ``"negative"`` when *text* carries a negation
    marker, else ``"positive"``.  Canonical implementation shared by
    ``DreamEngine._semantic_polarity`` (write side) and any read-side caller."""
    normalized = f" {normalize_key(text)} "
    return "negative" if any(marker in normalized for marker in NEGATION_MARKERS) else "positive"


def truth_slot_key(*, scope_key: str, subject: str, predicate: str) -> str:
    """WS-25 T1: the object-INDEPENDENT slot two statements share.

    Equal (modulo normalization) to the ``truth_prefix`` every materialized
    relationship already stores (``dreaming.truth_identity``) — exposed
    standalone so read-time code can group by slot without a protection
    context.  Two statements are "same slot" exactly when this key matches,
    regardless of cardinality: "prefers dark mode" and "prefers window
    seating" share a slot (same subject+predicate) but are NOT contradictory
    (different object) — see :func:`contradictory_object`.
    """
    return normalize_key(f"{scope_key}:{subject}:{predicate}")


def contradictory_object(
    *,
    first_object: str,
    first_polarity: str,
    second_object: str,
    second_polarity: str,
) -> bool:
    """WS-25 T1: True when two same-slot statements assert opposite claims
    about the SAME object.

    Requires OPPOSITE ``semantic_polarity`` and a negation-stripped object
    match: "prefers dark mode" (positive) vs "prefers not dark mode"
    (negative) both strip to "dark mode" -> contradictory.  Two objects that
    strip to different text — "dark mode" vs "window seating" — are never
    contradictory regardless of polarity, so they coexist on the same
    multi-active slot.  Two objects with the SAME polarity are never
    contradictory either (paraphrase or corroboration, not a conflict).
    """
    if first_polarity == second_polarity:
        return False
    return strip_negation_markers(first_object) == strip_negation_markers(second_object)


# ---------------------------------------------------------------------------
# WS-25 T3: read-time within-slot hard recency tiebreaker.
#
# A NEW stage inserted between the current-truth/visibility filter (stage 5)
# and the stage-6 rerank: for any truth slot with more than one ACTIVE
# in-window fact, group into contradiction clusters via ``contradictory_object``
# and keep only the newest member of each cluster.  Facts that share no
# contradiction with anything else in their slot — e.g. two coexisting
# multi-active preferences — are untouched; this is the load-bearing
# regression case (a bare per-slot winner-take-all would silently demote
# every coexisting preference but one).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemporalAuthorityCandidate:
    """The minimal shape the T3 tiebreaker needs from one current-truth-visible
    fact, independent of caller shape (``ScoredCandidate`` vs a raw profile
    relationship)."""

    uuid: str
    truth_slot: str | None
    object_text: str
    polarity: str
    valid_from: datetime | None
    created_at: datetime | None


_DATETIME_FLOOR = datetime.min.replace(tzinfo=UTC)


def demote_contradicted_same_slot(
    candidates: list[TemporalAuthorityCandidate],
) -> frozenset[str]:
    """WS-25 T3: uuids to demote out of the current-truth candidate set.

    Groups by ``truth_slot`` (a ``None`` slot never groups — that candidate is
    always kept).  Within one slot, clusters members that pairwise satisfy
    :func:`contradictory_object` — equivalent to, and implemented as, grouping
    by the negation-stripped object text, since contradiction requires
    object-text equality after stripping (two members with DIFFERENT stripped
    text are never in the same cluster regardless of polarity, so
    non-contradictory same-slot facts — e.g. two coexisting preferences —
    always land in singleton clusters and are never demoted).  Within a
    cluster of more than one member, every row except the max
    (``valid_from``, tie-broken by ``created_at``, then ``uuid``) is demoted.
    """
    by_cluster: dict[tuple[str, str], list[TemporalAuthorityCandidate]] = {}
    for candidate in candidates:
        if candidate.truth_slot is None:
            continue
        cluster_key = (candidate.truth_slot, strip_negation_markers(candidate.object_text))
        by_cluster.setdefault(cluster_key, []).append(candidate)
    demoted: set[str] = set()
    for members in by_cluster.values():
        if len(members) < 2:
            continue
        winner = max(
            members,
            key=lambda member: (
                member.valid_from or _DATETIME_FLOOR,
                member.created_at or _DATETIME_FLOOR,
                member.uuid,
            ),
        )
        demoted.update(member.uuid for member in members if member.uuid != winner.uuid)
    return frozenset(demoted)
