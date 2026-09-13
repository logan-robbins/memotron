"""WS-11: Replayable receipt-backed memory formation — receipts core.

Implements the receipt data model, canonicalization + hashing, digest helpers,
and the SQLite-backed :class:`ReceiptLedger` per PATENT_REPLAY_RECEIPTS_SPEC.md:

* Receipt data model and decision-type vocabulary — [0019]–[0021].
* Run checkpoints with per-run Merkle roots — [0022].
* Canonicalization and the ``payload_digest`` / ``receipt_hash`` formulas — [0023].
* "No candidate is silently dropped" formation semantics — [0024].
* Negative-space memory query over non-materializing decisions — [0027].

Receipts never store raw sensitive text: the canonical payload is built from
the STORED (redacted or encrypted) representation, so every hash verifies
without any governance key ([0019], claim 10).  This module imports only the
standard library, pydantic, and ``memotron.models`` — it must never import
graph/config/dreaming/client (no circular imports).
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from memotron.models import ExtractedMemory

RECEIPT_SCHEMA_VERSION = 1

GENESIS_RECEIPT_HASH = "0" * 64
"""Genesis sentinel for ``previous_receipt_hash`` at ``event_index`` 0 ([0023])."""

RUN_KINDS = frozenset(
    {
        "formation",
        "consolidation",
        "pruning",
        "operator",
        "coherence",
        "counterfactual",
        "migration",
        "regovern",
    }
)
"""``regovern`` (WS-25): a stage-3-only pass over an already-stored raw graph —
:meth:`~memotron.dreaming.DreamEngine.regovern_scope`.  Zero extraction/LLM
cost by construction, so it gets its own run kind rather than borrowing
``formation``'s: a byte-replay or audit reading run history must be able to
tell "this run called the model" from "this run only re-applied policy"."""

DECISION_RESULTS = frozenset(
    {
        "materialized",
        "transformed",
        "reinforced",
        "superseded",
        "demoted",
        "gated",
        "rejected",
        "pruned",
        "observed",
        "referenced",
        "destroyed",
        "recorded",
        "quarantined",
    }
)

NON_MATERIALIZING_RESULTS = frozenset(
    {
        "gated",
        "rejected",
        "transformed",
        "reinforced",
        "superseded",
        "demoted",
        "pruned",
        "quarantined",
    }
)
"""Decision results that leave no context-visible memory row ([0027] step 610)."""

REPLAY_STATUSES = frozenset({"unverified", "verified", "mismatch"})

_HEX_DIGITS = frozenset("0123456789abcdef")


class ReceiptDecisionType(StrEnum):
    """WS-11: Receipt decision-type vocabulary, exactly per spec [0021].

    ``PRUNING_RELATIONSHIP_PRUNED`` covers expiry/confidence/staleness prunes
    with the specific reason in ``decision_reason``; the soft cap keeps its
    dedicated type.  ``COHERENCE_INCIDENT_RECORDED`` implements coherence-spec
    claim 7.  ``PRUNING_SINGLE_ACTIVE_REPAIRED`` is additive ([0021]'s list is
    representative).
    """

    CANDIDATE_EXTRACTED = "candidate_extracted"
    CANDIDATE_SCHEMA_REJECTED = "candidate_schema_rejected"
    CANDIDATE_CLAIM_MODE_COERCED = "candidate_claim_mode_coerced"
    CANDIDATE_LOW_SALIENCE = "candidate_low_salience"
    CANDIDATE_QUARANTINED = "candidate_quarantined"
    QUARANTINE_CANDIDATE_PROMOTED = "quarantine_candidate_promoted"
    QUARANTINE_CANDIDATE_DISCARDED = "quarantine_candidate_discarded"
    FORMATION_HEALTH_GATE_TRIPPED = "formation_health_gate_tripped"
    FORMATION_MOTIVE_TYPE_FILTERED = "formation_motive_type_filtered"
    FORMATION_GOVERNANCE_REDACTED = "formation_governance_redacted"
    FORMATION_UNTRUSTED_DIRECTIVE_GATED = "formation_untrusted_directive_gated"
    # S105 matches on the member NAME; this is a receipt decision type, and the
    # decision it names is that a raw secret was REFUSED entry.
    FORMATION_RAW_SECRET_REJECTED = "formation_raw_secret_rejected"  # noqa: S105
    FORMATION_NON_NORMATIVE_EVIDENCE_GATED = "formation_non_normative_evidence_gated"
    FORMATION_SEMANTIC_DEDUP_REINFORCED = "formation_semantic_dedup_reinforced"
    FORMATION_TRUTH_KEY_SUPERSEDED = "formation_truth_key_superseded"
    FORMATION_RELATIONSHIP_CREATED = "formation_relationship_created"
    FORMATION_INCUMBENT_CONFIDENCE_DISCOUNTED = "formation_incumbent_confidence_discounted"
    FORMATION_PREDICATE_CANONICALIZED = "formation_predicate_canonicalized"
    EMBEDDING_TRANSPORT_DOWNGRADED = "embedding_transport_downgraded"
    EMBEDDING_TRANSPORT_UNAVAILABLE = "embedding_transport_unavailable"
    FORMATION_ENTITY_LINKED = "formation_entity_linked"
    ENTITY_ALIAS_PROPOSED = "entity_alias_proposed"
    ENTITY_ALIAS_RESOLVED = "entity_alias_resolved"
    ENTITY_ALIAS_DEMOTED = "entity_alias_demoted"
    OPERATOR_REVIEW_RESOLVED = "operator_review_resolved"
    MEMORY_PINNED = "memory_pinned"
    MEMORY_UNPINNED = "memory_unpinned"
    MEMORY_VISIBILITY_SET = "memory_visibility_set"
    CONSOLIDATION_MOTIVE_ROLLUP_GATED = "consolidation_motive_rollup_gated"
    CONSOLIDATION_ROLLUP_CREATED = "consolidation_rollup_created"
    CONSOLIDATION_MEMBER_DEMOTED = "consolidation_member_demoted"
    CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED = "consolidation_cross_prefix_duplicate_demoted"
    CONSOLIDATION_DUPLICATE_REPROMOTED = "consolidation_duplicate_repromoted"
    CONSOLIDATION_ROLLUP_INVALIDATED = "consolidation_rollup_invalidated"
    CONSOLIDATION_ROLLUP_ERASURE_PURGED = "consolidation_rollup_erasure_purged"
    CONSOLIDATION_ROLLUP_RECOMPUTED_NOOP = "consolidation_rollup_recomputed_noop"
    CONSOLIDATION_ROLLUP_EVIDENCE_UPDATED = "consolidation_rollup_evidence_updated"
    CONSOLIDATION_ROLLUP_MATERIAL_REINFORCEMENT = "consolidation_rollup_material_reinforcement"
    CONSOLIDATION_ROLLUP_RECOMPUTED = "consolidation_rollup_recomputed"
    CONSOLIDATION_ROLLUP_SYNTHESIS_REJECTED = "consolidation_rollup_synthesis_rejected"
    DREAM_AGENT_TRANSPORT_FAILED = "dream_agent_transport_failed"
    PRUNING_SOFT_CAP_PRUNED = "pruning_soft_cap_pruned"
    PRUNING_RELATIONSHIP_PRUNED = "pruning_relationship_pruned"
    PRUNING_SINGLE_ACTIVE_REPAIRED = "pruning_single_active_repaired"
    PRUNING_TEMPORARY_PREDECESSOR_RESTORED = "pruning_temporary_predecessor_restored"
    PRUNING_GHOST_RESTORED = "pruning_ghost_restored"
    REDACTION_RELATIONSHIP_VERSION_REDACTED = "redaction_relationship_version_redacted"
    CRYPTO_SHRED_KEY_DESTROYED = "crypto_shred_key_destroyed"
    ERASURE_CERTIFICATE_ISSUED = "erasure_certificate_issued"
    RETRIEVAL_BUDGET_OVERFLOW_REFERENCED = "retrieval_budget_overflow_referenced"
    USE_EVENT_RECORDED = "use_event_recorded"
    OUTCOME_EVENT_RECORDED = "outcome_event_recorded"
    SESSION_OUTCOME_JUDGE_REJECTED = "session_outcome_judge_rejected"
    COHERENCE_INCIDENT_RECORDED = "coherence_incident_recorded"
    COHERENCE_ARTIFACT_REGISTERED = "coherence_artifact_registered"
    COHERENCE_ARTIFACT_OUTCOME_RECORDED = "coherence_artifact_outcome_recorded"
    COHERENCE_ARTIFACT_QUARANTINED = "coherence_artifact_quarantined"
    COHERENCE_GOVERNOR_REPAIR_RECORDED = "coherence_governor_repair_recorded"
    COHERENCE_HOLD_RELEASED = "coherence_hold_released"
    COHERENCE_REPAIR_MONITOR_OPENED = "coherence_repair_monitor_opened"
    COHERENCE_INCIDENT_REOPENED = "coherence_incident_reopened"
    COHERENCE_INCIDENT_STATUS_CHANGED = "coherence_incident_status_changed"
    COHERENCE_DISAMBIGUATION_RESOLVED = "coherence_disambiguation_resolved"
    COHERENCE_GOVERNOR_ROLLED_BACK = "coherence_governor_rolled_back"
    COHERENCE_FREQUENCY_AUTHORITY_EVALUATED = "coherence_frequency_authority_evaluated"
    POLICY_CONTRACT_STAGED = "policy_contract_staged"
    POLICY_SHADOW_STAGE_COMPLETED = "policy_shadow_stage_completed"
    POLICY_ALIAS_ACTIVATED = "policy_alias_activated"
    POLICY_ALIAS_ROLLED_BACK = "policy_alias_rolled_back"
    COUNTERFACTUAL_CANDIDATE_ACCEPTED = "counterfactual_candidate_accepted"
    COUNTERFACTUAL_CANDIDATE_REJECTED = "counterfactual_candidate_rejected"
    TENANT_MIGRATION_RELATIONSHIP_MATERIALIZED = "tenant_migration_relationship_materialized"
    RETRIEVAL_UTILITY_AUTO_ENABLED = "retrieval_utility_auto_enabled"
    """WS-28 T2: a search's utility_weight flipped from the unset 0.0 default
    to ``RetrievalPolicy.utility_weight_when_unlocked`` because the scope's
    measured event volume reached ``utility_weight_auto_floor_events``. Fires
    per occurrence (like ``USE_EVENT_RECORDED``), not once per scope."""


# ---------------------------------------------------------------------------
# Canonicalization + hashing ([0023])
# ---------------------------------------------------------------------------


def _canonical_datetime_text(value: datetime) -> str:
    """ISO-8601 UTC, microsecond precision, ``+00:00`` suffix; naive is a hard error."""
    if value.tzinfo is None:
        raise ValueError("naive datetime in canonical payload: timestamps must be timezone-aware UTC ([0023])")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        # str() collapses str subclasses (e.g. StrEnum members) to their value.
        return str(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("NaN/Inf float in canonical payload is a hard error ([0023])")
        # Fixed-precision float encoding: scores are recorded, never recomputed.
        return repr(round(value, 10))
    if isinstance(value, datetime):
        return _canonical_datetime_text(value)
    if isinstance(value, BaseModel):
        return _normalize(value.model_dump(mode="json"))
    if isinstance(value, dict):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"canonical payload dict keys must be str, got {type(key).__name__}")
            normalized[str(key)] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        # List order is preserved: event ordering is semantic ([0023]).
        return [_normalize(item) for item in value]
    raise ValueError(f"unsupported type in canonical payload: {type(value).__name__} (fail fast; no coercion)")


def canonicalize_payload(payload: dict[str, Any]) -> bytes:
    """Convert a receipt payload into its stable canonical byte representation ([0023]).

    JSON with sorted keys, compact separators, ASCII-only; timestamps as
    ISO-8601 UTC with microsecond precision; floats as ``repr(round(x, 10))``;
    list order preserved.  Naive datetimes and NaN/Inf are hard errors.
    """
    if not isinstance(payload, dict):
        raise ValueError("canonical payload must be a dict")
    normalized = _normalize(payload)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


def payload_digest(payload: dict[str, Any]) -> str:
    """The ONE canonical digest function: sha256 over :func:`canonicalize_payload`."""
    return hashlib.sha256(canonicalize_payload(payload)).hexdigest()


MAX_CONTENT_FREE_REASON_CHARS = 200
"""Upper bound on a ``decision_reason`` that may stay in the clear inside a
content-protected scope (WS-23 M1)."""


def is_content_free_reason(value: str) -> bool:
    """WS-23 M1: may this ``decision_reason`` stay plaintext in a sealed scope?

    The write path (:meth:`ReceiptLedger.emit`) enforces this predicate and the
    erasure sweep re-verifies it, so the invariant is checked with exactly one
    definition on both sides.  A reason qualifies when it is a bounded machine
    code: no whitespace (which is what free-form prose, extraction fragments,
    validation errors and provider HTTP bodies always carry), no control
    characters, and at most :data:`MAX_CONTENT_FREE_REASON_CHARS` characters.
    Structured codes — ``dependency_crypto_shredded``,
    ``erasure_certificate_digest:<hex>``, ``cross_prefix_duplicate_of=<uuid>``,
    ``unentailed_token:<token>`` — all pass unchanged.
    """
    if not isinstance(value, str):
        return False
    if len(value) > MAX_CONTENT_FREE_REASON_CHARS:
        return False
    return not any(character.isspace() or ord(character) < 0x20 for character in value)


def receipt_hash(
    *,
    schema_version: int,
    previous_receipt_hash: str,
    payload_digest: str,
    run_uuid: str,
    event_index: int,
) -> str:
    """Chain hash per [0023]: sha256 over the exact ``"|"``-joined concatenation."""
    material = "|".join([str(schema_version), previous_receipt_hash, payload_digest, run_uuid, str(event_index)])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def merkle_root(leaf_hashes: Sequence[str]) -> str:
    """Per-run Merkle root over receipt hashes ordered by event_index ([0023]).

    Pairwise ``sha256(left + right)`` over the hex strings; an odd node is
    promoted unchanged to the next level (duplicate-last is NOT used).  An
    empty run is a hard error — a run with zero receipts must not checkpoint.
    """
    level = list(leaf_hashes)
    if not level:
        raise ValueError("merkle_root requires at least one leaf hash: a run with zero receipts must not checkpoint")
    for leaf in level:
        if not isinstance(leaf, str) or len(leaf) != 64 or not set(leaf) <= _HEX_DIGITS:
            raise ValueError(f"merkle leaf is not a lowercase sha256 hex digest: {leaf!r}")
    while len(level) > 1:
        next_level = [
            hashlib.sha256((level[i] + level[i + 1]).encode("utf-8")).hexdigest() for i in range(0, len(level) - 1, 2)
        ]
        if len(level) % 2 == 1:
            next_level.append(level[-1])
        level = next_level
    return level[0]


# ---------------------------------------------------------------------------
# Digests ([0018], [0020], claim 7)
# ---------------------------------------------------------------------------


def evidence_digest(body: str, source_metadata: dict[str, Any], scope_key: str) -> str:
    """Evidence digest over the immutable stored episode body + metadata + scope ([0018]).

    When governance redacts the body before extraction, this digest is still
    computed over the ORIGINAL immutable episode body; the redaction transform
    gets its own before/after digests on the redaction receipt.
    """
    if not isinstance(body, str):
        raise ValueError("evidence body must be a string")
    if not isinstance(source_metadata, dict):
        raise ValueError("evidence source_metadata must be a dict")
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("evidence scope_key must be a non-blank string")
    return payload_digest({"body": body, "metadata": source_metadata, "scope": scope_key})


def candidate_digest(
    candidate: ExtractedMemory,
    *,
    memory_type: str | None,
    episode_uuid: str,
    content_key: bytes | None = None,
) -> str:
    """Candidate digest computed at extraction time BEFORE any policy gate ([0020]).

    Covers the stored (post-redaction) candidate representation so a later
    counterfactual evaluation can re-gate the identical candidate stream
    deterministically without re-invoking the extractor (claim 18).

    WS-12: for a crypto-shred scope the digest is keyed
    (``HMAC-SHA256(DEK, canonical_candidate)``) rather than an unkeyed sha256,
    so once the scope's DEK is destroyed the recorded digest can no longer be
    dictionary-tested against guessed candidate content.  Keyed digests remain
    deterministic while the DEK lives, so counterfactual re-gating is unchanged.
    """
    if not isinstance(candidate, ExtractedMemory):
        raise ValueError("candidate must be an ExtractedMemory")
    if not isinstance(episode_uuid, str) or not episode_uuid.strip():
        raise ValueError("candidate episode_uuid must be a non-blank string")
    canonical = canonicalize_payload(
        {
            "subject": candidate.subject,
            "predicate": candidate.predicate,
            "object": candidate.object,
            "relationship_type": candidate.relationship_type,
            "memory_type": memory_type,
            "confidence": candidate.confidence,
            "valid_from": candidate.valid_from,
            "valid_to": candidate.valid_to,
            "scope_key": candidate.scope.key if candidate.scope is not None else None,
            "episode_uuid": episode_uuid,
            "source_text": candidate.source_text,
            "claim_mode": candidate.claim_mode.value if candidate.claim_mode is not None else None,
        }
    )
    if content_key is not None:
        import hmac as _hmac

        return _hmac.new(content_key, canonical, hashlib.sha256).hexdigest()
    return hashlib.sha256(canonical).hexdigest()


def rejected_candidate_digest(
    raw: dict[str, Any],
    *,
    episode_uuid: str,
    content_key: bytes | None = None,
) -> str:
    """Digest of a candidate REJECTED before it could become a typed memory ([0024]).

    Per-candidate schema rejection happens *before* the candidate is an
    :class:`ExtractedMemory`, so :func:`candidate_digest` cannot describe it —
    yet the ``CANDIDATE_SCHEMA_REJECTED`` receipt still has to identify exactly
    which candidate was dropped.  The digest is therefore taken over the raw
    candidate as the transport delivered it, JSON-normalized so any provider
    shape canonicalizes identically.

    Keyed with the scope DEK under crypto-shred governance for the same reason
    :func:`candidate_digest` is: once the key is destroyed the recorded digest
    must no longer be dictionary-testable against guessed candidate content.
    """
    if not isinstance(raw, dict):
        raise ValueError("rejected candidate must be a dict")
    if not isinstance(episode_uuid, str) or not episode_uuid.strip():
        raise ValueError("rejected candidate episode_uuid must be a non-blank string")
    try:
        normalized = json.loads(json.dumps(raw, default=str))
    except (TypeError, ValueError) as exc:
        raise ValueError("rejected candidate is not JSON-representable") from exc
    canonical = canonicalize_payload({"rejected_candidate": normalized, "episode_uuid": episode_uuid})
    if content_key is not None:
        import hmac as _hmac

        return _hmac.new(content_key, canonical, hashlib.sha256).hexdigest()
    return hashlib.sha256(canonical).hexdigest()


def motive_version_digest(motive: BaseModel, resolved_inputs: dict[str, Any]) -> str:
    """Motive version digest: Motive dump + ALL inherited policy inputs resolved for the run ([0018])."""
    if not isinstance(motive, BaseModel):
        raise ValueError("motive must be a pydantic BaseModel")
    if not isinstance(resolved_inputs, dict):
        raise ValueError("resolved_inputs must be a dict")
    return payload_digest({"motive": motive.model_dump(mode="json"), "resolved_inputs": resolved_inputs})


def effective_policy_digest(policy: BaseModel) -> str:
    """Effective-policy digest over the FULL resolved policy object (claim 7).

    Thin builder over :func:`payload_digest` for the control-plane path: the
    canonical dict is ``policy.model_dump(mode="json")`` — including
    ``source_trace``, excluding nothing — so any minor change at any policy
    layer changes the digest.
    """
    if not isinstance(policy, BaseModel):
        raise ValueError("policy must be a pydantic BaseModel (use config_effective_policy_digest for dicts)")
    return payload_digest(policy.model_dump(mode="json"))


def config_effective_policy_digest(resolved_inputs: dict[str, Any]) -> str:
    """Effective-policy digest for the plain-DreamConfig path (no control plane).

    Thin builder over :func:`payload_digest`: the caller supplies the prebuilt
    canonical resolved-inputs dict (tenant/agent/scope/dream mode/prompt pack/
    governance/dedup/pruning/job/motive).
    """
    if not isinstance(resolved_inputs, dict):
        raise ValueError("resolved_inputs must be a dict")
    return payload_digest(resolved_inputs)


def governance_policy_digest(policy: BaseModel | None) -> str | None:
    """Digest of the governance policy in force, or None when no policy applies."""
    if policy is None:
        return None
    if not isinstance(policy, BaseModel):
        raise ValueError("governance policy must be a pydantic BaseModel or None")
    return payload_digest(policy.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Models ([0019], [0022])
# ---------------------------------------------------------------------------


class MemoryReceipt(BaseModel):
    """WS-11: one canonical, schema-versioned, hash-linked decision event ([0019]).

    All digests are lowercase hex sha256.  Optional fields are ``None`` when
    not applicable and are INCLUDED in the canonical payload as nulls (stable
    shape).  ``sensitive_payload`` holds redacted-or-encrypted candidate text
    only — never plaintext; hashes are computed over this stored
    representation so chains verify without the governance key (claim 10).
    """

    model_config = {"frozen": True}

    receipt_uuid: str
    schema_version: int = RECEIPT_SCHEMA_VERSION
    tenant_id: str | None = None
    agent_id: str | None = None
    scope_key: str
    run_uuid: str
    run_kind: str
    episode_uuid: str | None = None
    episode_digest: str | None = None
    event_index: int
    decision_type: ReceiptDecisionType
    candidate_uuid: str | None = None
    candidate_digest: str | None = None
    source_span_digest: str | None = None
    motive_name: str | None = None
    motive_version_digest: str | None = None
    effective_policy_digest: str
    prompt_profile: str | None = None
    model_identifier: str | None = None
    extractor_identifier: str | None = None
    embedding_identifier: str | None = None
    formation_contract_digest: str | None = None
    formation_contract_source_trace: str | None = None
    formation_contract_attestation: str | None = None
    use_event_id: str | None = None
    outcome_event_id: str | None = None
    event_payload: str | None = None
    event_payload_digest: str | None = None
    retention_components: str | None = None
    memory_type: str | None = None
    claim_mode: str | None = None
    directive_stance: str | None = None
    relationship_type: str | None = None
    truth_key: str | None = None
    salience_score: float | None = None
    salience_threshold: float | None = None
    dedup_threshold: float | None = None
    dedup_match_relationship_uuid: str | None = None
    dedup_score: float | None = None
    governance_policy_digest: str | None = None
    redaction_digest_before: str | None = None
    redaction_digest_after: str | None = None
    decision_reason: str
    decision_result: str
    relationship_uuid: str | None = None
    superseded_relationship_uuid: str | None = None
    successor_relationship_uuid: str | None = None
    graph_state_hash_before: str | None = None
    graph_state_hash_after: str | None = None
    sensitive_payload: str | None = None
    sensitive_payload_encrypted: bool = False
    canonical_payload: str
    payload_digest: str
    previous_receipt_hash: str
    receipt_hash: str
    created_at: datetime


_DERIVED_FIELDS: tuple[str, ...] = (
    "canonical_payload",
    "payload_digest",
    "previous_receipt_hash",
    "receipt_hash",
)
_ALL_FIELDS: tuple[str, ...] = tuple(MemoryReceipt.model_fields)
_CANONICAL_FIELDS: tuple[str, ...] = tuple(field for field in _ALL_FIELDS if field not in _DERIVED_FIELDS)
_RUN_CONTROLLED_FIELDS: tuple[str, ...] = (
    "receipt_uuid",
    "schema_version",
    "run_uuid",
    "run_kind",
    "event_index",
)
_EMIT_FIELDS: frozenset[str] = frozenset(_CANONICAL_FIELDS) - frozenset(_RUN_CONTROLLED_FIELDS)
_FLOAT_FIELDS: tuple[str, ...] = (
    "salience_score",
    "salience_threshold",
    "dedup_threshold",
    "dedup_score",
)
_STRING_EMIT_FIELDS: frozenset[str] = _EMIT_FIELDS - frozenset(
    (*_FLOAT_FIELDS, "decision_type", "created_at", "sensitive_payload_encrypted")
)
_RUN_DEFAULT_FIELDS: tuple[str, ...] = (
    "tenant_id",
    "agent_id",
    "scope_key",
    "motive_version_digest",
    "effective_policy_digest",
    "formation_contract_digest",
    "formation_contract_source_trace",
    "formation_contract_attestation",
)


class RunCheckpoint(BaseModel):
    """WS-11: per-run tamper-evidence commitment ([0022])."""

    model_config = {"frozen": True}

    run_uuid: str
    run_kind: str
    job_name: str
    tenant_id: str | None = None
    agent_id: str | None = None
    scope_key: str
    motive_version_digest: str | None = None
    effective_policy_digest: str
    first_receipt_hash: str
    last_receipt_hash: str
    receipt_count: int
    merkle_root: str
    graph_state_hash_before: str | None = None
    graph_state_hash_after: str | None = None
    # WS-26 T8: when this run was a re-dream recompute, the cheapest-sufficient
    # tier it ran at (A/B/C) and why — so a receipt reader can tell a
    # governance-only re-dream from a full re-extract without joining to the
    # epoch row.  None for an ordinary (non-re-dream) run.
    redream_tier: str | None = None
    redream_tier_reason: str | None = None
    replay_status: str = "unverified"
    created_at: datetime


class ChainVerification(BaseModel):
    """WS-11: structured result of chain + Merkle verification (never raises on mismatch).

    ``expected``/``actual`` describe the first divergence (a hash or digest
    recomputed from the persisted row vs. the stored value).
    """

    model_config = {"frozen": True}

    run_uuid: str
    valid: bool
    receipt_count: int
    computed_merkle_root: str | None = None
    checkpoint_merkle_root: str | None = None
    first_divergent_event_index: int | None = None
    expected: str | None = None
    actual: str | None = None
    errors: tuple[str, ...] = ()


class NegativeSpaceEntry(BaseModel):
    """WS-11: one explained, evidence-backed non-materializing decision ([0027]).

    Redaction-safe by construction: ``sensitive_payload`` is the stored
    (already redacted or encrypted) representation only.
    """

    model_config = {"frozen": True}

    receipt_uuid: str
    run_uuid: str
    run_kind: str
    scope_key: str
    tenant_id: str | None = None
    agent_id: str | None = None
    episode_uuid: str | None = None
    episode_digest: str | None = None
    candidate_uuid: str | None = None
    candidate_digest: str | None = None
    motive_name: str | None = None
    motive_version_digest: str | None = None
    effective_policy_digest: str
    governance_policy_digest: str | None = None
    memory_type: str | None = None
    relationship_type: str | None = None
    truth_key: str | None = None
    decision_type: ReceiptDecisionType
    decision_reason: str
    decision_result: str
    salience_score: float | None = None
    salience_threshold: float | None = None
    dedup_threshold: float | None = None
    dedup_score: float | None = None
    dedup_match_relationship_uuid: str | None = None
    relationship_uuid: str | None = None
    sensitive_payload: str | None = None
    sensitive_payload_encrypted: bool = False
    created_at: datetime


class ReceiptRun(BaseModel):
    """WS-11: mutable handle for one receipted run — run identity + chain cursor.

    Created by :meth:`ReceiptLedger.begin_run`; carries the next dense
    ``event_index`` and the previous receipt hash so :meth:`ReceiptLedger.emit`
    can chain every receipt ([0023]).
    """

    run_uuid: str
    run_kind: str
    job_name: str
    tenant_id: str | None = None
    agent_id: str | None = None
    scope_key: str
    motive_version_digest: str | None = None
    effective_policy_digest: str
    formation_contract_digest: str | None = None
    formation_contract_source_trace: str | None = None
    formation_contract_attestation: str | None = None
    graph_state_hash_before: str | None = None
    next_event_index: int = 0
    previous_receipt_hash: str = GENESIS_RECEIPT_HASH


# ---------------------------------------------------------------------------
# Ledger persistence (shares the graph's SQLite connection; [0033] permits this)
# ---------------------------------------------------------------------------

_RECEIPTS_DDL = """
CREATE TABLE IF NOT EXISTS memory_receipts (
    receipt_uuid TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    tenant_id TEXT,
    agent_id TEXT,
    scope_key TEXT NOT NULL,
    run_uuid TEXT NOT NULL,
    run_kind TEXT NOT NULL,
    episode_uuid TEXT,
    episode_digest TEXT,
    event_index INTEGER NOT NULL,
    decision_type TEXT NOT NULL,
    candidate_uuid TEXT,
    candidate_digest TEXT,
    source_span_digest TEXT,
    motive_name TEXT,
    motive_version_digest TEXT,
    effective_policy_digest TEXT NOT NULL,
    prompt_profile TEXT,
    model_identifier TEXT,
    extractor_identifier TEXT,
    embedding_identifier TEXT,
    formation_contract_digest TEXT,
    formation_contract_source_trace TEXT,
    formation_contract_attestation TEXT,
    use_event_id TEXT,
    outcome_event_id TEXT,
    event_payload TEXT,
    event_payload_digest TEXT,
    retention_components TEXT,
    memory_type TEXT,
    claim_mode TEXT,
    directive_stance TEXT,
    relationship_type TEXT,
    truth_key TEXT,
    salience_score REAL,
    salience_threshold REAL,
    dedup_threshold REAL,
    dedup_match_relationship_uuid TEXT,
    dedup_score REAL,
    governance_policy_digest TEXT,
    redaction_digest_before TEXT,
    redaction_digest_after TEXT,
    decision_reason TEXT NOT NULL,
    decision_result TEXT NOT NULL,
    relationship_uuid TEXT,
    superseded_relationship_uuid TEXT,
    successor_relationship_uuid TEXT,
    graph_state_hash_before TEXT,
    graph_state_hash_after TEXT,
    sensitive_payload TEXT,
    sensitive_payload_encrypted INTEGER NOT NULL,
    canonical_payload TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    previous_receipt_hash TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS memory_receipts_run_event_idx
    ON memory_receipts(run_uuid, event_index);
CREATE INDEX IF NOT EXISTS memory_receipts_scope_key_idx ON memory_receipts(scope_key);
CREATE INDEX IF NOT EXISTS memory_receipts_episode_uuid_idx ON memory_receipts(episode_uuid);
CREATE INDEX IF NOT EXISTS memory_receipts_decision_type_idx ON memory_receipts(decision_type);
CREATE INDEX IF NOT EXISTS memory_receipts_decision_result_idx ON memory_receipts(decision_result);
CREATE INDEX IF NOT EXISTS memory_receipts_created_at_idx ON memory_receipts(created_at);
CREATE INDEX IF NOT EXISTS memory_receipts_candidate_digest_idx ON memory_receipts(candidate_digest);
CREATE INDEX IF NOT EXISTS memory_receipts_relationship_uuid_idx ON memory_receipts(relationship_uuid);

CREATE TABLE IF NOT EXISTS run_checkpoints (
    run_uuid TEXT PRIMARY KEY,
    run_kind TEXT NOT NULL,
    job_name TEXT NOT NULL,
    tenant_id TEXT,
    agent_id TEXT,
    scope_key TEXT NOT NULL,
    motive_version_digest TEXT,
    effective_policy_digest TEXT NOT NULL,
    first_receipt_hash TEXT NOT NULL,
    last_receipt_hash TEXT NOT NULL,
    receipt_count INTEGER NOT NULL,
    merkle_root TEXT NOT NULL,
    graph_state_hash_before TEXT,
    graph_state_hash_after TEXT,
    redream_tier TEXT,
    redream_tier_reason TEXT,
    replay_status TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _memory_receipt_row_values(receipt: MemoryReceipt) -> list[Any]:
    """Typed column values for one persisted ``memory_receipts`` row."""
    row_values: list[Any] = []
    for field in _ALL_FIELDS:
        value = getattr(receipt, field)
        if isinstance(value, datetime):
            value = _canonical_datetime_text(value)
        elif isinstance(value, ReceiptDecisionType):
            value = value.value
        elif isinstance(value, bool):
            value = int(value)
        row_values.append(value)
    return row_values


def _row_canonical_payload(row: sqlite3.Row) -> dict[str, Any]:
    """Rebuild the canonical payload dict from a persisted row's typed columns.

    Datetimes are stored as their canonical text (so they round-trip
    byte-identically); ``sensitive_payload_encrypted`` is stored as 0/1 and
    must be restored to bool so the canonical JSON emits true/false.
    """
    payload: dict[str, Any] = {}
    for field in _CANONICAL_FIELDS:
        value = row[field]
        if field == "sensitive_payload_encrypted":
            value = bool(value)
        payload[field] = value
    return payload


class ReceiptLedger:
    """WS-11: append-only receipt + checkpoint ledger on the shared graph SQLite DB.

    :meth:`emit` is the AUTHORITATIVE choke point — every receipt in the
    system goes through it, which is what makes the chain ([0023]) and the
    "no candidate silently dropped" property ([0024]) enforceable.
    """

    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        commit: Callable[[], None] | None = None,
    ) -> None:
        if not isinstance(connection, sqlite3.Connection):
            raise ValueError("ReceiptLedger requires a sqlite3.Connection")
        self._connection = connection
        # WS-23 M1: installed by SQLiteStorageBackend so `emit` can tell whether a
        # scope's content plane is sealed and, if so, seal a diverted reason.
        self._content_protection_probe: Any = None
        self._content_protection_sealer: Any = None
        # The owning backend routes commits through its transaction() bracket,
        # so a receipt emitted inside a logical operation commits with it.
        self._commit = commit if commit is not None else connection.commit
        self._migrate()

    def bind_content_protection(self, *, probe: Any, sealer: Any) -> None:
        """Install the WS-23 M1 content-protection hooks (owner: the graph store).

        ``probe(scope_key) -> bool`` answers whether the scope's content plane
        is (or ever was) sealed; ``sealer(scope_key, text) -> str | None``
        returns AES-GCM ciphertext for a diverted reason, or ``None`` when the
        key is already destroyed and the detail must simply be dropped.
        """
        self._content_protection_probe = probe
        self._content_protection_sealer = sealer

    def _enforce_content_free_reason(self, values: dict[str, Any]) -> None:
        """WS-23 M1: keep raw content out of ``decision_reason`` in sealed scopes.

        ``decision_reason`` is a plaintext lineage column that the ciphertext-only
        erasure sweep now covers.  Some reasons legitimately fold uncontrolled
        text (a pydantic ``ValidationError`` carries ``input_value``, a provider
        error carries its HTTP body), which would survive a crypto-shred as
        readable PII and still let the certificate issue with ``violations=()``.

        At this choke point — the one every receipt in the system passes
        through — a reason for a content-protected scope that is not a bounded,
        whitespace-free machine code is replaced by
        ``"<decision_type>:reason_withheld"`` and the original text is moved
        into ``sensitive_payload``, which is sealed and already swept.  When the
        receipt already carries a sensitive payload (candidate text), or the
        scope's key is destroyed, the detail is dropped rather than stored in
        the clear.  Unprotected scopes are untouched — byte-identical behaviour.
        """
        probe = self._content_protection_probe
        if probe is None:
            return
        reason = str(values["decision_reason"])
        if is_content_free_reason(reason):
            return
        if not probe(str(values["scope_key"])):
            return
        values["decision_reason"] = f"{ReceiptDecisionType(values['decision_type']).value}:reason_withheld"
        if values.get("sensitive_payload") is not None:
            return
        sealer = self._content_protection_sealer
        sealed = sealer(str(values["scope_key"]), reason) if sealer is not None else None
        if sealed is None:
            return
        values["sensitive_payload"] = sealed
        values["sensitive_payload_encrypted"] = True

    def _migrate(self) -> None:
        # Idempotent DDL, mirroring PropertyGraphStore._migrate's executescript pattern.
        self._connection.executescript(_RECEIPTS_DDL)
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(memory_receipts)").fetchall()}
        additive_columns = {
            "formation_contract_digest": "TEXT",
            "formation_contract_source_trace": "TEXT",
            "formation_contract_attestation": "TEXT",
            "claim_mode": "TEXT",
            "directive_stance": "TEXT",
            "use_event_id": "TEXT",
            "outcome_event_id": "TEXT",
            "event_payload": "TEXT",
            "event_payload_digest": "TEXT",
            "retention_components": "TEXT",
        }
        for column, sql_type in additive_columns.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE memory_receipts ADD COLUMN {column} {sql_type}")
        # WS-26 T8: additive re-dream tier columns on run_checkpoints.
        checkpoint_columns = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(run_checkpoints)").fetchall()
        }
        for column in ("redream_tier", "redream_tier_reason"):
            if column not in checkpoint_columns:
                self._connection.execute(f"ALTER TABLE run_checkpoints ADD COLUMN {column} TEXT")
        self._commit()

    def _rows(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        # Per-cursor row factory: never mutate the shared connection's row_factory.
        cursor = self._connection.cursor()
        cursor.row_factory = sqlite3.Row
        try:
            return cursor.execute(sql, parameters).fetchall()
        finally:
            cursor.close()

    # ------------------------------------------------------------------
    # Run lifecycle
    # ------------------------------------------------------------------

    def begin_run(
        self,
        *,
        run_kind: str,
        job_name: str,
        scope_key: str,
        effective_policy_digest: str,
        tenant_id: str | None = None,
        agent_id: str | None = None,
        motive_version_digest: str | None = None,
        graph_state_hash_before: str | None = None,
    ) -> ReceiptRun:
        """Begin a receipted run and return the chain-cursor handle.

        Nothing is persisted here; the checkpoint row is written by
        :meth:`checkpoint` at the end of the run ([0022]).
        """
        if run_kind not in RUN_KINDS:
            raise ValueError(f"run_kind {run_kind!r} is not one of {sorted(RUN_KINDS)}")
        if not isinstance(job_name, str) or not job_name.strip():
            raise ValueError("job_name must be a non-blank string")
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("scope_key must be a non-blank string")
        if not isinstance(effective_policy_digest, str) or not effective_policy_digest.strip():
            raise ValueError("effective_policy_digest must be a non-blank string")
        if motive_version_digest is not None and (
            not isinstance(motive_version_digest, str) or not motive_version_digest.strip()
        ):
            raise ValueError("motive_version_digest must be None or a non-blank string")
        return ReceiptRun(
            run_uuid=uuid4().hex,
            run_kind=run_kind,
            job_name=job_name,
            tenant_id=tenant_id,
            agent_id=agent_id,
            scope_key=scope_key,
            motive_version_digest=motive_version_digest,
            effective_policy_digest=effective_policy_digest,
            graph_state_hash_before=graph_state_hash_before,
        )

    def emit(self, run: ReceiptRun, **receipt_fields: Any) -> MemoryReceipt:
        """Build, chain, and persist one receipt; bump the run's chain cursor.

        Requires ``decision_type``, ``decision_reason``, and ``decision_result``.
        ``tenant_id``/``agent_id``/``scope_key``/``motive_version_digest``/
        ``effective_policy_digest`` default from the run and may be overridden
        per receipt.  Unknown fields are a hard error.
        """
        if not isinstance(run, ReceiptRun):
            raise ValueError("emit requires the ReceiptRun handle returned by begin_run")
        unknown = sorted(set(receipt_fields) - _EMIT_FIELDS)
        if unknown:
            raise ValueError(f"unknown receipt fields: {unknown}")
        for required in ("decision_type", "decision_reason", "decision_result"):
            if required not in receipt_fields:
                raise ValueError(f"emit requires {required}")

        values: dict[str, Any] = dict.fromkeys(_EMIT_FIELDS)
        for field in _RUN_DEFAULT_FIELDS:
            values[field] = getattr(run, field)
        values["sensitive_payload_encrypted"] = False
        values.update(receipt_fields)

        values["decision_type"] = ReceiptDecisionType(values["decision_type"])
        if values["decision_result"] not in DECISION_RESULTS:
            raise ValueError(f"decision_result {values['decision_result']!r} is not one of {sorted(DECISION_RESULTS)}")
        if not isinstance(values["decision_reason"], str) or not values["decision_reason"].strip():
            raise ValueError("decision_reason must be a non-blank string")
        created_at = values["created_at"] if values["created_at"] is not None else datetime.now(UTC)
        if not isinstance(created_at, datetime):
            raise ValueError("created_at must be a datetime")
        if created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware (naive datetimes are a hard error)")
        values["created_at"] = created_at
        for field in _FLOAT_FIELDS:
            if values[field] is not None:
                try:
                    values[field] = float(values[field])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{field} must be a float or None") from exc
        if not isinstance(values["sensitive_payload_encrypted"], bool):
            raise ValueError("sensitive_payload_encrypted must be a bool")
        for field in sorted(_STRING_EMIT_FIELDS):
            value = values[field]
            if field == "decision_result" or value is None:
                continue
            if not isinstance(value, str):
                raise ValueError(f"{field} must be a string or None, got {type(value).__name__}")
        if not values["scope_key"] or not values["scope_key"].strip():
            raise ValueError("scope_key must be a non-blank string")
        if not values["effective_policy_digest"] or not values["effective_policy_digest"].strip():
            raise ValueError("effective_policy_digest must be a non-blank string")
        self._enforce_content_free_reason(values)

        payload: dict[str, Any] = {
            "receipt_uuid": uuid4().hex,
            "schema_version": RECEIPT_SCHEMA_VERSION,
            "run_uuid": run.run_uuid,
            "run_kind": run.run_kind,
            "event_index": run.next_event_index,
            **values,
        }
        canonical_bytes = canonicalize_payload(payload)
        digest = hashlib.sha256(canonical_bytes).hexdigest()
        chained_hash = receipt_hash(
            schema_version=RECEIPT_SCHEMA_VERSION,
            previous_receipt_hash=run.previous_receipt_hash,
            payload_digest=digest,
            run_uuid=run.run_uuid,
            event_index=run.next_event_index,
        )
        receipt = MemoryReceipt(
            **payload,
            canonical_payload=canonical_bytes.decode("utf-8"),
            payload_digest=digest,
            previous_receipt_hash=run.previous_receipt_hash,
            receipt_hash=chained_hash,
        )

        columns = ", ".join(_ALL_FIELDS)
        placeholders = ", ".join("?" for _ in _ALL_FIELDS)
        row_values = _memory_receipt_row_values(receipt)
        try:
            self._connection.execute(
                f"INSERT INTO memory_receipts ({columns}) VALUES ({placeholders})",
                row_values,
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(
                f"receipt event_index collision for run {run.run_uuid}: "
                "a run must be driven by exactly one ReceiptRun handle"
            ) from exc
        self._commit()

        run.previous_receipt_hash = chained_hash
        run.next_event_index += 1
        return receipt

    def checkpoint(
        self,
        run: ReceiptRun,
        *,
        graph_state_hash_after: str | None = None,
        redream_tier: str | None = None,
        redream_tier_reason: str | None = None,
    ) -> RunCheckpoint:
        """Commit the run's Merkle root and chain endpoints ([0022]).

        Empty runs are a hard error: a run with zero receipts must not
        checkpoint.  Receipt hashes are read back from the persisted rows (the
        single source of truth), not from in-memory state.

        WS-26 T8: a re-dream recompute passes ``redream_tier`` /
        ``redream_tier_reason`` so the run receipt itself names the tier it ran
        at and why.
        """
        if not isinstance(run, ReceiptRun):
            raise ValueError("checkpoint requires the ReceiptRun handle returned by begin_run")
        rows = self._rows(
            "SELECT receipt_hash FROM memory_receipts WHERE run_uuid = ? ORDER BY event_index",
            (run.run_uuid,),
        )
        if not rows:
            raise RuntimeError(f"cannot checkpoint run {run.run_uuid}: the run emitted zero receipts")
        if len(rows) != run.next_event_index:
            raise RuntimeError(
                f"cannot checkpoint run {run.run_uuid}: persisted receipt count {len(rows)} "
                f"does not match the run handle's event count {run.next_event_index}"
            )
        hashes = [str(row["receipt_hash"]) for row in rows]
        checkpoint = RunCheckpoint(
            run_uuid=run.run_uuid,
            run_kind=run.run_kind,
            job_name=run.job_name,
            tenant_id=run.tenant_id,
            agent_id=run.agent_id,
            scope_key=run.scope_key,
            motive_version_digest=run.motive_version_digest,
            effective_policy_digest=run.effective_policy_digest,
            first_receipt_hash=hashes[0],
            last_receipt_hash=hashes[-1],
            receipt_count=len(hashes),
            merkle_root=merkle_root(hashes),
            graph_state_hash_before=run.graph_state_hash_before,
            graph_state_hash_after=graph_state_hash_after,
            redream_tier=redream_tier,
            redream_tier_reason=redream_tier_reason,
            replay_status="unverified",
            created_at=datetime.now(UTC),
        )
        try:
            self._connection.execute(
                """
                INSERT INTO run_checkpoints (
                    run_uuid, run_kind, job_name, tenant_id, agent_id, scope_key,
                    motive_version_digest, effective_policy_digest, first_receipt_hash,
                    last_receipt_hash, receipt_count, merkle_root, graph_state_hash_before,
                    graph_state_hash_after, redream_tier, redream_tier_reason,
                    replay_status, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint.run_uuid,
                    checkpoint.run_kind,
                    checkpoint.job_name,
                    checkpoint.tenant_id,
                    checkpoint.agent_id,
                    checkpoint.scope_key,
                    checkpoint.motive_version_digest,
                    checkpoint.effective_policy_digest,
                    checkpoint.first_receipt_hash,
                    checkpoint.last_receipt_hash,
                    checkpoint.receipt_count,
                    checkpoint.merkle_root,
                    checkpoint.graph_state_hash_before,
                    checkpoint.graph_state_hash_after,
                    checkpoint.redream_tier,
                    checkpoint.redream_tier_reason,
                    checkpoint.replay_status,
                    _canonical_datetime_text(checkpoint.created_at),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RuntimeError(f"checkpoint already exists for run {run.run_uuid}") from exc
        self._commit()
        return checkpoint

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def receipts_for_run(self, run_uuid: str) -> list[MemoryReceipt]:
        rows = self._rows(
            "SELECT * FROM memory_receipts WHERE run_uuid = ? ORDER BY event_index",
            (run_uuid,),
        )
        return [MemoryReceipt.model_validate(dict(row)) for row in rows]

    def receipts_for_scope(self, scope_key: str) -> list[MemoryReceipt]:
        """WS-12: every receipt recorded for a scope, in stable chain order
        (erasure-certificate sweep + chain/replay enumeration)."""
        if not isinstance(scope_key, str) or not scope_key.strip():
            raise ValueError("receipts_for_scope requires a non-blank scope_key")
        rows = self._rows(
            "SELECT * FROM memory_receipts WHERE scope_key = ? ORDER BY created_at, run_uuid, event_index",
            (scope_key,),
        )
        return [MemoryReceipt.model_validate(dict(row)) for row in rows]

    def latest_formation_receipt_digest(self, relationship_uuid: str) -> str | None:
        """WS-19 T20: the newest FORMATION_* receipt payload_digest for one row.

        Seeks ``memory_receipts_relationship_uuid_idx``; promotion candidates
        pin this digest so a promoted fact's lineage binds the exact receipted
        formation decision that produced its source row.  None when the row
        has no formation receipt (e.g. rows migrated from a pre-receipt store).
        """
        if not isinstance(relationship_uuid, str) or not relationship_uuid.strip():
            raise ValueError("latest_formation_receipt_digest requires a non-blank relationship_uuid")
        rows = self._rows(
            "SELECT payload_digest FROM memory_receipts "
            "WHERE relationship_uuid = ? AND decision_type LIKE 'formation_%' "
            "ORDER BY created_at DESC, run_uuid DESC, event_index DESC LIMIT 1",
            (relationship_uuid.strip(),),
        )
        return str(rows[0]["payload_digest"]) if rows else None

    def checkpoint_for_run(self, run_uuid: str) -> RunCheckpoint:
        rows = self._rows("SELECT * FROM run_checkpoints WHERE run_uuid = ?", (run_uuid,))
        if not rows:
            raise ValueError(f"no checkpoint recorded for run {run_uuid}")
        return RunCheckpoint.model_validate(dict(rows[0]))

    def run_checkpoints(self, *, scope_key: str | None = None, limit: int = 20) -> list[RunCheckpoint]:
        """Per-run Merkle checkpoints, newest first ([0022]).

        The listing lives on the ledger so callers never reach through the
        backend into engine internals (#8 acceptance).
        """
        if limit <= 0:
            raise ValueError("limit must be greater than zero")
        if scope_key is not None:
            rows = self._rows(
                "SELECT * FROM run_checkpoints WHERE scope_key = ? ORDER BY created_at DESC, run_uuid DESC LIMIT ?",
                (scope_key, limit),
            )
        else:
            rows = self._rows(
                "SELECT * FROM run_checkpoints ORDER BY created_at DESC, run_uuid DESC LIMIT ?",
                (limit,),
            )
        return [RunCheckpoint.model_validate(dict(row)) for row in rows]

    def set_replay_status(self, run_uuid: str, status: str) -> None:
        """Record byte-replay outcome on the checkpoint ([0025] steps 470/480)."""
        if status not in REPLAY_STATUSES:
            raise ValueError(f"replay status {status!r} is not one of {sorted(REPLAY_STATUSES)}")
        cursor = self._connection.execute(
            "UPDATE run_checkpoints SET replay_status = ? WHERE run_uuid = ?",
            (status, run_uuid),
        )
        if cursor.rowcount == 0:
            raise ValueError(f"no checkpoint recorded for run {run_uuid}")
        self._commit()

    # ------------------------------------------------------------------
    # Verification ([0023], [0025] step 410)
    # ------------------------------------------------------------------

    def verify_chain(self, run_uuid: str) -> ChainVerification:
        """Recompute payload digests, chain hashes, and the Merkle root from persisted rows.

        Never raises on mismatch — returns a structured result.  Each canonical
        payload is REBUILT from the row's typed columns, so tampering with any
        single column (not just the stored canonical text) is detected.
        """
        rows = self._rows(
            "SELECT * FROM memory_receipts WHERE run_uuid = ? ORDER BY event_index",
            (run_uuid,),
        )
        checkpoint_rows = self._rows("SELECT * FROM run_checkpoints WHERE run_uuid = ?", (run_uuid,))
        checkpoint_row = checkpoint_rows[0] if checkpoint_rows else None
        checkpoint_root = str(checkpoint_row["merkle_root"]) if checkpoint_row is not None else None

        if not rows:
            return ChainVerification(
                run_uuid=run_uuid,
                valid=False,
                receipt_count=0,
                checkpoint_merkle_root=checkpoint_root,
                errors=(f"no receipts recorded for run {run_uuid}",),
            )

        errors: list[str] = []
        first_divergent: int | None = None
        expected: str | None = None
        actual: str | None = None
        previous = GENESIS_RECEIPT_HASH
        computed_hashes: list[str] = []

        for position, row in enumerate(rows):
            stored_index = int(row["event_index"])
            if stored_index != position:
                first_divergent, expected, actual = position, str(position), str(stored_index)
                errors.append(
                    f"event_index chain is not dense at position {position}: "
                    f"expected {position}, found {stored_index} (missing or reordered receipt row)"
                )
                break
            if str(row["previous_receipt_hash"]) != previous:
                first_divergent, expected, actual = (
                    position,
                    previous,
                    str(row["previous_receipt_hash"]),
                )
                errors.append(f"previous_receipt_hash does not chain at event_index {position}")
                break
            rebuilt = canonicalize_payload(_row_canonical_payload(row)).decode("utf-8")
            stored_canonical = str(row["canonical_payload"])
            if rebuilt != stored_canonical:
                first_divergent = position
                expected = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
                actual = hashlib.sha256(stored_canonical.encode("utf-8")).hexdigest()
                errors.append(
                    f"canonical payload rebuilt from columns diverges at event_index {position} "
                    "(a persisted receipt field was tampered)"
                )
                break
            digest = hashlib.sha256(rebuilt.encode("utf-8")).hexdigest()
            if digest != str(row["payload_digest"]):
                first_divergent, expected, actual = position, digest, str(row["payload_digest"])
                errors.append(f"payload_digest mismatch at event_index {position}")
                break
            chained = receipt_hash(
                schema_version=int(row["schema_version"]),
                previous_receipt_hash=previous,
                payload_digest=digest,
                run_uuid=run_uuid,
                event_index=position,
            )
            if chained != str(row["receipt_hash"]):
                first_divergent, expected, actual = position, chained, str(row["receipt_hash"])
                errors.append(f"receipt_hash mismatch at event_index {position}")
                break
            computed_hashes.append(chained)
            previous = chained

        computed_root: str | None = None
        if first_divergent is None:
            computed_root = merkle_root(computed_hashes)
            if checkpoint_row is not None:
                if int(checkpoint_row["receipt_count"]) != len(computed_hashes):
                    errors.append(
                        f"checkpoint receipt_count {int(checkpoint_row['receipt_count'])} does not "
                        f"match persisted receipt count {len(computed_hashes)}"
                    )
                    if expected is None:
                        expected = str(int(checkpoint_row["receipt_count"]))
                        actual = str(len(computed_hashes))
                if str(checkpoint_row["first_receipt_hash"]) != computed_hashes[0]:
                    errors.append("checkpoint first_receipt_hash does not match the chain")
                if str(checkpoint_row["last_receipt_hash"]) != computed_hashes[-1]:
                    errors.append("checkpoint last_receipt_hash does not match the chain")
                if checkpoint_root != computed_root:
                    errors.append("checkpoint merkle_root does not match the recomputed root")
                    if expected is None:
                        expected, actual = computed_root, checkpoint_root

        return ChainVerification(
            run_uuid=run_uuid,
            valid=not errors,
            receipt_count=len(rows),
            computed_merkle_root=computed_root,
            checkpoint_merkle_root=checkpoint_root,
            first_divergent_event_index=first_divergent,
            expected=expected,
            actual=actual,
            errors=tuple(errors),
        )

    # ------------------------------------------------------------------
    # Negative space ([0027])
    # ------------------------------------------------------------------

    def negative_space(
        self,
        *,
        scope_key: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        episode_uuid: str | None = None,
        motive_name: str | None = None,
        memory_type: str | None = None,
        decision_reason: str | None = None,
        decision_type: ReceiptDecisionType | str | None = None,
    ) -> list[NegativeSpaceEntry]:
        """Query what the system chose NOT to remember ([0027]).

        Selects only receipts whose ``decision_result`` is non-materializing
        (gated/rejected/transformed/reinforced/superseded/demoted/pruned),
        joined to candidate digest, evidence pointer, policy digests, and
        decision reason.  The time window is half-open: ``since <= created_at
        < until``.  Redaction-safe: returns stored (already redacted or
        encrypted) text only.
        """
        for label, value in (
            ("scope_key", scope_key),
            ("episode_uuid", episode_uuid),
            ("motive_name", motive_name),
            ("memory_type", memory_type),
            ("decision_reason", decision_reason),
        ):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"{label} filter must be None or a non-blank string")

        results_placeholder = ", ".join("?" for _ in NON_MATERIALIZING_RESULTS)
        filters = [f"decision_result IN ({results_placeholder})"]
        parameters: list[Any] = sorted(NON_MATERIALIZING_RESULTS)
        if scope_key is not None:
            filters.append("scope_key = ?")
            parameters.append(scope_key)
        if since is not None:
            filters.append("created_at >= ?")
            parameters.append(_canonical_datetime_text(since))
        if until is not None:
            filters.append("created_at < ?")
            parameters.append(_canonical_datetime_text(until))
        if episode_uuid is not None:
            filters.append("episode_uuid = ?")
            parameters.append(episode_uuid)
        if motive_name is not None:
            filters.append("motive_name = ?")
            parameters.append(motive_name)
        if memory_type is not None:
            filters.append("memory_type = ?")
            parameters.append(memory_type)
        if decision_reason is not None:
            filters.append("decision_reason = ?")
            parameters.append(decision_reason)
        if decision_type is not None:
            filters.append("decision_type = ?")
            parameters.append(ReceiptDecisionType(decision_type).value)

        rows = self._rows(
            f"""
            SELECT * FROM memory_receipts
            WHERE {" AND ".join(filters)}
            ORDER BY created_at, run_uuid, event_index
            """,
            parameters,
        )
        return [NegativeSpaceEntry.model_validate(dict(row)) for row in rows]
