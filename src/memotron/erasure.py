"""WS-12: Machine-verifiable erasure certificates for crypto-shredded scopes.

Reduction to practice of verifiable-erasure for replayable receipts: after a
scope's per-scope DEK is destroyed, :func:`issue_erasure_certificate` binds
FOUR verifications into one canonical, re-executable artifact:

1. **Key destruction** — the scope's wrapped DEK is zeroed and the
   destruction is itself a receipted event in the hash chain
   (``CRYPTO_SHRED_KEY_DESTROYED``).
2. **Ciphertext-only sweep** — every content-plane field persisted for the
   scope (relationship fact/object/source_text/embeddings, node names and
   identity keys, raw episode bodies and names, receipt sensitive payloads
   and truth keys) is verified to be a sealed AES-256-GCM token or a keyed
   HMAC commitment; any plaintext remnant is a named violation.
3. **Chain survival** — every receipt run that touched the scope still
   verifies end-to-end (payload digests, chain hashes, Merkle roots) after
   key destruction, because every hash was computed over the stored
   (ciphertext) representation.
4. **Replay survival** — every checkpointed run with memory-store mutations
   still byte-replays (checkpoint-anchored, LLM-free) after key destruction,
   reconstructing the scope's structural memory delta from the receipts.

Issuance FAILS CLOSED: a certificate exists only when all four legs verify.
The certificate digest covers the assembled body, and the certificate is
appended to the same receipt chain (``ERASURE_CERTIFICATE_ISSUED``, emitted
by the SDK client).  :func:`verify_erasure_certificate` re-executes every leg
against the live store at any later time — the certificate is a durable,
re-checkable proof, not a point-in-time attestation.

This module imports only the standard library, pydantic, and
``memotron.crypto`` / ``memotron.receipts`` / ``memotron.replay``;
the ``graph`` parameter is duck-typed (no import cycles).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from memotron.crypto import is_content_commitment, is_sealed_content
from memotron.receipts import ReceiptDecisionType, is_content_free_reason
from memotron.replay import ReplayVerificationError, byte_replay
from memotron.storage import receipts as receipts_mod

RELATIONSHIP_SEALED_FIELDS: tuple[str, ...] = (
    "fact",
    "object",
    "source_text",
    "embedding",
    "object_embedding",
    # Added 2026-09-08, DEFENSIVELY -- these are not a live remnant. They carry the caller's
    # ORIGINAL entity text and are written by `_materialization` only when alias resolution
    # rewrote the canonical name, which cannot happen while content is sealed: resolution is
    # inert under seal, so the keys are never written there. Measured, not assumed.
    #
    # Enumerated regardless, because a field absent from this tuple is invisible BY
    # CONSTRUCTION -- the sweep can only fail on what it lists, so an incomplete set reads
    # exactly like a clean store. If resolution is ever made seal-aware, these start being
    # written and this tuple must already know about them. Audit this tuple whenever a new
    # content-bearing relationship property is added.
    "subject_surface",
    "object_surface",
)
RELATIONSHIP_COMMITTED_FIELDS: tuple[str, ...] = (
    "truth_key",
    "truth_prefix",
    "fact_commitment",
    "object_commitment",
)
NODE_SEALED_FIELDS: tuple[str, ...] = ("name",)
NODE_COMMITTED_FIELDS: tuple[str, ...] = ("graph_key",)
EPISODE_SEALED_FIELDS: tuple[str, ...] = ("name", "body")

QUARANTINE_SEALED_FIELDS: tuple[str, ...] = (
    "candidate_payload",
    "detail",
    "saves_step",
    "subject",
    "predicate",
    "object",
)
"""WS-24: the quarantine store's content plane.

A quarantined candidate holds model-authored text about the subject exactly as
a relationship does, so it seals under the same DEK and must vanish under the
same shred.  Enumerated here because the sweep is the only thing that makes the
erasure certificate's "ciphertext only" claim true — a new content store that
the sweep does not know about is a silent hole in the certificate."""

COVERED_STORES: dict[str, tuple[str, ...]] = {
    "relationships": RELATIONSHIP_SEALED_FIELDS + RELATIONSHIP_COMMITTED_FIELDS,
    "nodes": NODE_SEALED_FIELDS + NODE_COMMITTED_FIELDS,
    "episodes": EPISODE_SEALED_FIELDS,
    "memory_receipts": ("sensitive_payload", "truth_key", "decision_reason"),
    "dream_decisions": ("fact",),
    "quarantined_candidates": QUARANTINE_SEALED_FIELDS,
}
"""Auditable enumeration of every store/field the sweep verifies.  Provenance
metadata (scope ids, timestamps, relationship types) is the lineage plane and
is intentionally retained — that is what makes the audit chain and byte replay
survive the erasure.

WS-23 M1: ``decision_reason`` is covered too.  It is retained in the clear, but
only as a bounded machine code — some reasons used to fold uncontrolled text
(pydantic ``ValidationError`` payloads carry ``input_value``, provider errors
carry their HTTP body), which survived a shred as readable content while the
certificate still issued with ``violations=()``.  ``ReceiptLedger.emit``
enforces :func:`~memotron.receipts.is_content_free_reason` on the write side
for protected scopes and diverts anything else into the sealed
``sensitive_payload``; the sweep re-verifies the same predicate here."""

_REPLAYABLE_RESULTS = frozenset({"materialized", "superseded", "demoted", "reinforced", "pruned", "destroyed"})


class ErasureSweepResult(BaseModel):
    """WS-12: outcome of the ciphertext-only sweep over a scope's persisted stores."""

    model_config = {"frozen": True}

    scope_key: str
    relationships_scanned: int
    nodes_scanned: int
    episodes_scanned: int
    receipts_scanned: int
    decision_records_scanned: int
    quarantined_candidates_scanned: int = 0
    sealed_field_count: int
    committed_field_count: int
    violations: tuple[str, ...] = ()


class ErasureCertificate(BaseModel):
    """WS-12: canonical, re-executable proof of verifiable erasure for one scope.

    ``certificate_digest`` is the sha256 payload digest over the certificate
    BODY (everything except ``certificate_uuid``/``issued_at``), computed with
    the same canonicalizer as every receipt, so the digest recorded on the
    ``ERASURE_CERTIFICATE_ISSUED`` receipt binds the certificate content into
    the tamper-evident chain.
    """

    model_config = {"frozen": True}

    certificate_uuid: str
    scope_key: str
    issued_at: datetime
    erasure_method: str
    key_state: dict[str, Any]
    shred_receipt_hash: str
    shred_run_uuid: str
    covered_stores: dict[str, tuple[str, ...]]
    sweep: ErasureSweepResult
    chain_verified_runs: tuple[str, ...]
    replay_verified_runs: tuple[str, ...]
    derived_artifact_purge_receipt_hashes: tuple[str, ...]
    certificate_digest: str


class ErasureVerificationError(Exception):
    """WS-12: raised when certificate issuance or re-verification fails closed.

    Carries the structured failure detail on ``.failures`` so callers can see
    exactly which leg failed (missing key destruction, named plaintext
    remnants, a diverging chain, or a non-replaying run) without a
    certificate ever being returned.
    """

    def __init__(self, scope_key: str, failures: tuple[str, ...]) -> None:
        self.scope_key = scope_key
        self.failures = failures
        detail = failures[0] if failures else "erasure verification failed"
        super().__init__(f"erasure verification failed for scope {scope_key}: {detail}")


def _certificate_body(
    *,
    scope_key: str,
    erasure_method: str,
    key_state: dict[str, Any],
    shred_receipt_hash: str,
    shred_run_uuid: str,
    sweep: ErasureSweepResult,
    chain_verified_runs: tuple[str, ...],
    replay_verified_runs: tuple[str, ...],
    derived_artifact_purge_receipt_hashes: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "scope_key": scope_key,
        "erasure_method": erasure_method,
        "key_state": key_state,
        "shred_receipt_hash": shred_receipt_hash,
        "shred_run_uuid": shred_run_uuid,
        "covered_stores": {store: list(fields) for store, fields in COVERED_STORES.items()},
        "sweep": sweep.model_dump(mode="json"),
        "chain_verified_runs": list(chain_verified_runs),
        "replay_verified_runs": list(replay_verified_runs),
        "derived_artifact_purge_receipt_hashes": list(derived_artifact_purge_receipt_hashes),
    }


def _derived_artifact_purge_receipt_hashes(graph: Any, *, scope_key: str) -> tuple[str, ...]:
    """Return the receipted invalidations caused by this scope's crypto shred.

    Rollups are derived artifacts.  Their content may not remain retrievable
    after an erased child disappears, so the certificate binds the exact
    synchronous purge/invalidation transitions alongside key destruction.
    """
    return tuple(
        sorted(
            receipt.receipt_hash
            for receipt in graph.receipts.receipts_for_scope(scope_key)
            if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_ROLLUP_ERASURE_PURGED
            and receipt.decision_reason == "dependency_crypto_shredded"
        )
    )


def _walk_fact_fields(value: Any, path: str, violations: list[str]) -> None:
    """Flag any plaintext ``fact`` field nested inside a decision payload."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "fact" and isinstance(item, str) and item and not is_sealed_content(item):
                violations.append(f"{path}.fact")
            else:
                _walk_fact_fields(item, f"{path}.{key}", violations)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _walk_fact_fields(item, f"{path}[{index}]", violations)


def sweep_scope(graph: Any, *, scope_key: str) -> ErasureSweepResult:
    """Verify the scope's persisted stores hold only sealed/committed content.

    Scans the covered stores enumerated in :data:`COVERED_STORES` and returns
    counts plus a named violation for every plaintext content field found —
    each violation identifies the store, row, and field, never the content.
    """
    violations: list[str] = []
    sealed_count = 0
    committed_count = 0

    relationships = [
        relationship for relationship in graph.relationships() if relationship.properties.get("scope_key") == scope_key
    ]
    for relationship in relationships:
        props = relationship.properties
        for field in RELATIONSHIP_SEALED_FIELDS:
            value = props.get(field)
            if value is None:
                continue
            if is_sealed_content(value):
                sealed_count += 1
            else:
                violations.append(f"relationships:{relationship.uuid}:{field}")
        for field in RELATIONSHIP_COMMITTED_FIELDS:
            value = props.get(field)
            if value is None:
                continue
            if is_content_commitment(value):
                committed_count += 1
            else:
                violations.append(f"relationships:{relationship.uuid}:{field}")

    nodes = graph.nodes_for_scope(scope_key)
    for node in nodes:
        for field in NODE_SEALED_FIELDS:
            value = node.properties.get(field)
            if value is None:
                continue
            if is_sealed_content(value):
                sealed_count += 1
            else:
                violations.append(f"nodes:{node.uuid}:{field}")
        # Episode nodes carry a structural identity key (``episode:<uuid>``) —
        # content-free by construction; only entity identity keys embed content
        # and must be keyed commitments.
        if "Episode" in node.labels:
            continue
        for field in NODE_COMMITTED_FIELDS:
            value = node.properties.get(field)
            if value is None:
                continue
            if is_content_commitment(value):
                committed_count += 1
            else:
                violations.append(f"nodes:{node.uuid}:{field}")

    episodes = graph.episodes_for_scope(scope_key)
    for episode in episodes:
        for field in EPISODE_SEALED_FIELDS:
            value = getattr(episode, field, None)
            if value is None:
                continue
            if is_sealed_content(value):
                sealed_count += 1
            else:
                violations.append(f"episodes:{episode.uuid}:{field}")

    receipts = graph.receipts.receipts_for_scope(scope_key)
    for receipt in receipts:
        if receipt.sensitive_payload is not None:
            if receipt.sensitive_payload_encrypted and is_sealed_content(receipt.sensitive_payload):
                sealed_count += 1
            else:
                violations.append(f"memory_receipts:{receipt.receipt_uuid}:sensitive_payload")
        if receipt.truth_key is not None:
            if is_content_commitment(receipt.truth_key):
                committed_count += 1
            else:
                violations.append(f"memory_receipts:{receipt.receipt_uuid}:truth_key")
        # WS-23 M1: the retained plaintext reason must be a bounded machine
        # code — the same predicate the ledger enforces on the write side.
        if not is_content_free_reason(receipt.decision_reason):
            violations.append(f"memory_receipts:{receipt.receipt_uuid}:decision_reason")

    decision_records = graph.dream_decisions_for_scope(scope_key)
    for record in decision_records:
        payload = record.model_dump(mode="json") if isinstance(record, BaseModel) else dict(record)
        _walk_fact_fields(payload, f"dream_decisions:{payload.get('uuid', '?')}", violations)

    # WS-24: the quarantine store is content plane too.  A candidate the
    # actionability gate refused still quotes the subject's documents, so it
    # seals under the same DEK and must be unreadable after the same shred.
    quarantined = graph.quarantined_candidate_content_fields(scope_key=scope_key)
    for candidate_uuid, fields in quarantined:
        for field in QUARANTINE_SEALED_FIELDS:
            value = fields.get(field)
            if value is None or value == "":
                continue
            if is_sealed_content(value):
                sealed_count += 1
            else:
                violations.append(f"quarantined_candidates:{candidate_uuid}:{field}")

    return ErasureSweepResult(
        scope_key=scope_key,
        relationships_scanned=len(relationships),
        nodes_scanned=len(nodes),
        episodes_scanned=len(episodes),
        receipts_scanned=len(receipts),
        decision_records_scanned=len(decision_records),
        quarantined_candidates_scanned=len(quarantined),
        sealed_field_count=sealed_count,
        committed_field_count=committed_count,
        violations=tuple(violations),
    )


def _key_and_shred_facts(graph: Any, *, scope_key: str, failures: list[str]) -> tuple[dict[str, Any], str, str]:
    key_state = graph.governance_key_state(scope_key)
    if key_state is None:
        failures.append("no governance key was ever provisioned for this scope")
        return {}, "", ""
    if not key_state["shredded"]:
        failures.append("the scope's DEK has not been destroyed (crypto_shred has not run)")
        return key_state, "", ""

    shred_receipts = [
        receipt
        for receipt in graph.receipts.receipts_for_scope(scope_key)
        if receipt.decision_type == ReceiptDecisionType.CRYPTO_SHRED_KEY_DESTROYED
    ]
    if not shred_receipts:
        failures.append("no CRYPTO_SHRED_KEY_DESTROYED receipt exists for this scope")
        return key_state, "", ""
    shred_receipt = shred_receipts[-1]
    return key_state, shred_receipt.receipt_hash, shred_receipt.run_uuid


async def _verify_runs(graph: Any, *, scope_key: str, failures: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Leg 3 + leg 4: chain-verify every scope run; byte-replay the checkpointed
    runs that mutate the memory store (checkpoint-anchored point-in-time replay)."""
    ledger = graph.receipts
    receipts = ledger.receipts_for_scope(scope_key)
    run_uuids: list[str] = []
    replayable: set[str] = set()
    for receipt in receipts:
        if receipt.run_uuid not in run_uuids:
            run_uuids.append(receipt.run_uuid)
        if receipt.decision_result in _REPLAYABLE_RESULTS:
            replayable.add(receipt.run_uuid)

    chain_verified: list[str] = []
    for run_uuid in run_uuids:
        verification = ledger.verify_chain(run_uuid)
        if verification.valid:
            chain_verified.append(run_uuid)
        else:
            failures.append(f"receipt chain for run {run_uuid} does not verify: " + "; ".join(verification.errors))

    replay_verified: list[str] = []
    for run_uuid in run_uuids:
        if run_uuid not in replayable:
            continue
        try:
            ledger.checkpoint_for_run(run_uuid)
        except ValueError:
            failures.append(f"run {run_uuid} has mutating receipts but no checkpoint")
            continue
        try:
            await byte_replay(ledger, run_uuid=run_uuid, graph=None)
        except ReplayVerificationError as exc:
            failures.append(f"run {run_uuid} does not byte-replay: {exc.report.errors}")
            continue
        replay_verified.append(run_uuid)

    return tuple(chain_verified), tuple(replay_verified)


DEFAULT_ERASURE_METHOD = (
    "cryptographic erase (NIST SP 800-88 CE): destruction of the wrapped per-scope "
    "AES-256-GCM DEK; content stored only as ciphertext or keyed HMAC commitments"
)


async def issue_erasure_certificate(graph: Any, *, scope_key: str, now: datetime | None = None) -> ErasureCertificate:
    """Issue the erasure certificate for a crypto-shredded scope — FAIL CLOSED.

    Executes all four legs (key destruction, ciphertext-only sweep, chain
    survival, replay survival) and raises :class:`ErasureVerificationError`
    with every failure when any leg does not verify.  The returned
    certificate is self-contained; the SDK client appends its digest to the
    receipt chain as an ``ERASURE_CERTIFICATE_ISSUED`` event.
    """
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("issue_erasure_certificate requires a non-blank scope_key")

    failures: list[str] = []
    key_state, shred_receipt_hash, shred_run_uuid = _key_and_shred_facts(graph, scope_key=scope_key, failures=failures)
    if failures:
        raise ErasureVerificationError(scope_key, tuple(failures))

    sweep = sweep_scope(graph, scope_key=scope_key)
    if sweep.violations:
        failures.extend(f"plaintext content remnant: {violation}" for violation in sweep.violations)

    chain_verified, replay_verified = await _verify_runs(graph, scope_key=scope_key, failures=failures)
    if failures:
        raise ErasureVerificationError(scope_key, tuple(failures))
    derived_artifact_purge_receipt_hashes = _derived_artifact_purge_receipt_hashes(graph, scope_key=scope_key)

    body = _certificate_body(
        scope_key=scope_key,
        erasure_method=DEFAULT_ERASURE_METHOD,
        key_state=key_state,
        shred_receipt_hash=shred_receipt_hash,
        shred_run_uuid=shred_run_uuid,
        sweep=sweep,
        chain_verified_runs=chain_verified,
        replay_verified_runs=replay_verified,
        derived_artifact_purge_receipt_hashes=derived_artifact_purge_receipt_hashes,
    )
    return ErasureCertificate(
        certificate_uuid=uuid4().hex,
        scope_key=scope_key,
        issued_at=now or datetime.now(UTC),
        erasure_method=DEFAULT_ERASURE_METHOD,
        key_state=key_state,
        shred_receipt_hash=shred_receipt_hash,
        shred_run_uuid=shred_run_uuid,
        covered_stores=dict(COVERED_STORES),
        sweep=sweep,
        chain_verified_runs=chain_verified,
        replay_verified_runs=replay_verified,
        derived_artifact_purge_receipt_hashes=derived_artifact_purge_receipt_hashes,
        certificate_digest=receipts_mod.payload_digest(body),
    )


async def verify_erasure_certificate(graph: Any, *, certificate: ErasureCertificate) -> bool:
    """Re-execute an erasure certificate against the live store — FAIL CLOSED.

    Verifies (a) the certificate is internally consistent (its digest matches
    its body), (b) every assertion it makes is STILL true now: the key remains
    destroyed, the scope still stores only ciphertext/commitments, every run
    the certificate names still chain-verifies, every replayed run still
    byte-replays, and (c) the certificate's issuance receipt is present in the
    chain.  Raises :class:`ErasureVerificationError` on any failure; returns
    True otherwise.  This re-executability is what makes the certificate a
    durable proof rather than a point-in-time attestation.
    """
    scope_key = certificate.scope_key
    failures: list[str] = []

    recomputed_digest = receipts_mod.payload_digest(
        _certificate_body(
            scope_key=scope_key,
            erasure_method=certificate.erasure_method,
            key_state=certificate.key_state,
            shred_receipt_hash=certificate.shred_receipt_hash,
            shred_run_uuid=certificate.shred_run_uuid,
            sweep=certificate.sweep,
            chain_verified_runs=certificate.chain_verified_runs,
            replay_verified_runs=certificate.replay_verified_runs,
            derived_artifact_purge_receipt_hashes=certificate.derived_artifact_purge_receipt_hashes,
        )
    )
    if recomputed_digest != certificate.certificate_digest:
        failures.append("certificate digest does not match the certificate body (tampered)")

    key_state = graph.governance_key_state(scope_key)
    if key_state is None or not key_state["shredded"]:
        failures.append("the scope's DEK is no longer in the destroyed state")

    sweep = sweep_scope(graph, scope_key=scope_key)
    if sweep.violations:
        failures.extend(f"plaintext content remnant: {violation}" for violation in sweep.violations)

    ledger = graph.receipts
    for run_uuid in certificate.chain_verified_runs:
        verification = ledger.verify_chain(run_uuid)
        if not verification.valid:
            failures.append(f"certified run {run_uuid} no longer chain-verifies")
    for run_uuid in certificate.replay_verified_runs:
        try:
            await byte_replay(ledger, run_uuid=run_uuid, graph=None)
        except ReplayVerificationError:
            failures.append(f"certified run {run_uuid} no longer byte-replays")

    issuance = [
        receipt
        for receipt in ledger.receipts_for_scope(scope_key)
        if receipt.decision_type == ReceiptDecisionType.ERASURE_CERTIFICATE_ISSUED
        and certificate.certificate_digest in receipt.decision_reason
    ]
    if not issuance:
        failures.append("no ERASURE_CERTIFICATE_ISSUED receipt binds this certificate to the chain")

    current_purge_hashes = _derived_artifact_purge_receipt_hashes(graph, scope_key=scope_key)
    if current_purge_hashes != certificate.derived_artifact_purge_receipt_hashes:
        failures.append("derived-artifact purge receipts no longer match the certificate")

    if failures:
        raise ErasureVerificationError(scope_key, tuple(failures))
    return True


__all__ = [
    "COVERED_STORES",
    "DEFAULT_ERASURE_METHOD",
    "ErasureCertificate",
    "ErasureSweepResult",
    "ErasureVerificationError",
    "issue_erasure_certificate",
    "sweep_scope",
    "verify_erasure_certificate",
]
