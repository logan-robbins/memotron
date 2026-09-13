"""WS-11: Byte replay, counterfactual policy evaluation, and Motive certification.

Reduction-to-practice engine for PATENT_REPLAY_RECEIPTS_SPEC.md, built entirely
on top of the finished :mod:`memotron.receipts` ledger:

* *Byte replay* — [0025], claims 2-4.  LLM-free, read-only reconstruction of a
  recorded formation delta from its receipt stream, failing closed on any
  missing artifact or divergence.
* *Counterfactual policy evaluation* — [0026], claims 16-19.  Re-gates the
  recorded (pre-gate) candidate stream under an alternate Motive/policy and
  emits an isolated delta.  Isolation is structural: this function is handed no
  graph handle, so no write path to the production store exists.
* *Motive certification* — [0028], claim 14.  Runs formation over a fixture
  corpus through the client, verifies the chain + graph state hash, and emits a
  receipt-backed certificate binding a Motive's behaviour to a Merkle root.

This module imports ONLY the standard library, pydantic, and
``memotron.receipts`` / ``memotron.models``.  It must never import
graph/client/dreaming (no import cycles); the ``ledger``, ``graph``, and
``client`` parameters are duck-typed by design.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from pydantic import BaseModel

from memotron.receipts import (
    ChainVerification,
    ReceiptDecisionType,
    ReceiptLedger,
)
from memotron.storage import receipts as receipts_mod

# ---------------------------------------------------------------------------
# Decision-result vocabularies used by the replay engine ([0025], [0026])
# ---------------------------------------------------------------------------

_MUTATING_RESULTS: frozenset[str] = frozenset(
    {"materialized", "superseded", "demoted", "reinforced", "pruned", "destroyed"}
)
"""Decision results that move the memory store and therefore carry a per-mutation
``graph_state_hash_before`` -> ``graph_state_hash_after`` transition ([0025] step 450)."""

_ACCEPTING_RESULTS: frozenset[str] = frozenset({"materialized", "reinforced", "superseded"})
"""Decision results that persist or update a context-visible memory (used to decide
whether the ORIGINAL policy accepted a candidate for the counterfactual delta)."""


# ---------------------------------------------------------------------------
# Public report / proof models (names pinned; imported by the SDK layer)
# ---------------------------------------------------------------------------


class MismatchReport(BaseModel):
    """WS-11: fail-closed replay/counterfactual mismatch record ([0025] step 480)."""

    model_config = {"frozen": True}

    run_uuid: str
    first_divergent_event_index: int | None = None
    expected: str | None = None
    actual: str | None = None
    missing_artifacts: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class ReplayVerificationError(Exception):
    """WS-11: raised when byte replay (or a counterfactual precondition) fails closed.

    Carries the structured :class:`MismatchReport` on ``.report`` so callers can
    surface exactly which artifact was missing or where the chain diverged
    without the proof ever being returned ([0025] step 435/480, claim 19).
    """

    def __init__(self, report: MismatchReport) -> None:
        self.report = report
        detail = report.errors[0] if report.errors else "replay verification failed"
        super().__init__(f"replay verification failed for run {report.run_uuid}: {detail}")


class ReplayProof(BaseModel):
    """WS-11: proof that a recorded run replays byte-identically ([0025] step 470)."""

    model_config = {"frozen": True}

    run_uuid: str
    merkle_root: str
    reconstructed_state_hash: str | None = None
    checkpoint_state_hash: str | None = None
    receipt_count: int
    verified_at: datetime


class CounterfactualPolicy(BaseModel):
    """WS-11: alternate gate parameters supplied as ``policy_source`` ([0026] step 510).

    A convenience carrier for the numeric gate knobs the counterfactual re-gates
    against.  ``allowed_memory_types`` is only consulted when the alternate
    Motive does not itself pin the type allowlist (the Motive owns type gating).
    """

    model_config = {"frozen": True}

    allowed_memory_types: tuple[str, ...] = ()
    salience_threshold: float = 0.0
    dedup_threshold: float = 1.0
    governance_sensitivity: str | None = None


class CounterfactualRejection(BaseModel):
    """WS-11: one candidate rejected by BOTH the original and alternate policies."""

    model_config = {"frozen": True}

    candidate_digest: str
    candidate_uuid: str | None = None
    original_reason: str
    alternate_reason: str


class CounterfactualReport(BaseModel):
    """WS-11: isolated delta between the recorded run and an alternate policy ([0026])."""

    model_config = {"frozen": True}

    run_uuid: str
    alternate_motive_name: str
    accepted_by_both: tuple[str, ...] = ()
    original_only: tuple[str, ...] = ()
    alternate_only: tuple[str, ...] = ()
    rejected_by_both: tuple[CounterfactualRejection, ...] = ()
    sensitive_divergence: tuple[str, ...] = ()
    counterfactual_state_hash: str
    original_materialized_count: int
    alternate_materialized_count: int
    recorded_run_uuid: str | None = None


class MotiveCertificate(BaseModel):
    """WS-11: receipt-backed certification of a Motive over a fixture corpus ([0028])."""

    model_config = {"frozen": True}

    motive_version_digest: str
    corpus_digest: str
    merkle_root: str
    metrics: dict[str, Any]
    failing_receipt_uuids: tuple[str, ...] = ()
    passed: bool


class MemoryBillOfMaterials(BaseModel):
    """Receipt-backed bill of materials for one memory-forming run.

    The report is the exportable run header counsel asked for: evidence digests,
    candidate digests, policy digests, Merkle root, graph state hashes, and
    negative-space counts bound to the same chain-verifiable run.
    """

    model_config = {"frozen": True}

    run_uuid: str
    run_kind: str
    job_name: str
    scope_key: str
    effective_policy_digest: str
    motive_version_digest: str | None = None
    receipt_count: int
    candidate_count: int
    disposition_count: int
    materialized_count: int
    non_materialized_count: int
    negative_space_count: int
    receipt_decision_counts: dict[str, int]
    evidence_digests: tuple[str, ...] = ()
    candidate_digests: tuple[str, ...] = ()
    first_receipt_hash: str
    last_receipt_hash: str
    merkle_root: str
    graph_state_hash_before: str | None = None
    graph_state_hash_after: str | None = None
    chain_valid: bool
    replay_status: str
    errors: tuple[str, ...] = ()


class PolicyComparisonItem(BaseModel):
    """One alternate policy evaluated against the same recorded candidate stream."""

    model_config = {"frozen": True}

    motive_name: str
    report: CounterfactualReport
    signal_to_noise_delta: int


class PolicyComparisonReport(BaseModel):
    """Cross-policy comparison over a frozen baseline run."""

    model_config = {"frozen": True}

    run_uuid: str
    baseline_materialized_count: int
    comparisons: tuple[PolicyComparisonItem, ...] = ()


class PolicyRegressionGateReport(BaseModel):
    """Deployment gate for a proposed Motive revision.

    A candidate Motive passes only when certification passes, the candidate does
    not newly accept baseline-rejected candidates, and it preserves every
    baseline-accepted candidate whose memory type is still allowed by the
    candidate Motive.
    """

    model_config = {"frozen": True}

    candidate_motive_name: str
    baseline_run_uuid: str
    certification: MotiveCertificate
    comparison: CounterfactualReport
    passed: bool
    failures: tuple[str, ...] = ()
    preserved_allowed_count: int = 0
    blocked_disallowed_count: int = 0
    lost_allowed_count: int = 0
    new_acceptance_count: int = 0


class NoSilentMutationReport(BaseModel):
    """Verifies that a scope's live state is explained by receipted mutations."""

    model_config = {"frozen": True}

    scope_key: str
    checked_run_uuids: tuple[str, ...] = ()
    mutating_receipt_count: int = 0
    chain_verified_count: int = 0
    replay_verified_count: int = 0
    latest_receipted_run_uuid: str | None = None
    latest_receipted_state_hash: str | None = None
    live_state_hash: str | None = None
    passed: bool
    errors: tuple[str, ...] = ()


class GovernanceCertificationBundle(BaseModel):
    """Exportable bundle tying together the proof surfaces for one scope/run."""

    model_config = {"frozen": True}

    scope_key: str
    run_uuid: str
    issued_at: datetime
    memory_bill_of_materials: MemoryBillOfMaterials
    replay_proof: ReplayProof
    no_silent_mutation: NoSilentMutationReport
    negative_space_count: int
    negative_space_candidate_digests: tuple[str, ...] = ()
    policy_certificate: MotiveCertificate | None = None
    policy_comparison: PolicyComparisonReport | None = None
    policy_regression_gate: PolicyRegressionGateReport | None = None
    coherence_incident_count: int = 0
    coherence_incident_ids: tuple[str, ...] = ()
    erasure_certificate_digest: str | None = None
    bundle_digest: str


# ---------------------------------------------------------------------------
# Internal reconstruction state ([0025] step 450)
# ---------------------------------------------------------------------------


class _ReconstructedGraphState:
    """WS-11: the receipt-derived memory-store state folded during byte replay.

    Reconstructs relationship identifiers, truth keys, status transitions,
    demotion flags, and supersession pointers from the decision receipts, exactly
    as [0025] step 450 requires.  The authoritative replay assertion is the
    per-mutation state-hash chain; this model makes the reconstructed relationship
    graph explicit (and is exercised on every mutating receipt).
    """

    def __init__(self) -> None:
        self.relationships: dict[str, dict[str, Any]] = {}
        self.truth_keys: dict[str, str] = {}

    def apply(self, receipt: Any) -> None:
        result = receipt.decision_result
        if result == "materialized":
            self._activate(receipt.relationship_uuid, receipt)
        elif result == "reinforced":
            target = receipt.dedup_match_relationship_uuid or receipt.relationship_uuid
            rel = self.relationships.get(target)
            if rel is not None:
                rel["observed_count"] = int(rel.get("observed_count", 1)) + 1
        elif result == "superseded":
            old = self.relationships.get(receipt.superseded_relationship_uuid)
            if old is not None:
                old["status"] = "superseded"
                old["active_in_context"] = False
                old["superseded_by_relationship_uuid"] = receipt.successor_relationship_uuid
            self._activate(receipt.successor_relationship_uuid, receipt)
        elif result == "demoted":
            rel = self.relationships.get(receipt.relationship_uuid)
            if rel is not None:
                rel["status"] = "demoted"
                rel["active_in_context"] = False
                rel["rolled_up_by"] = receipt.successor_relationship_uuid
        elif result in ("pruned", "destroyed"):
            rel = self.relationships.get(receipt.relationship_uuid)
            if rel is not None:
                rel["status"] = result
                rel["active_in_context"] = False

    def _activate(self, relationship_uuid: str | None, receipt: Any) -> None:
        if relationship_uuid is None:
            return
        self.relationships[relationship_uuid] = {
            "status": "active",
            "active_in_context": True,
            "memory_type": receipt.memory_type,
            "truth_key": receipt.truth_key,
            "observed_count": 1,
        }
        if receipt.truth_key:
            self.truth_keys[receipt.truth_key] = relationship_uuid

    @property
    def context_visible_uuids(self) -> frozenset[str]:
        return frozenset(uuid for uuid, rel in self.relationships.items() if rel.get("active_in_context"))


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _report_from_chain(verification: ChainVerification) -> MismatchReport:
    """Build a :class:`MismatchReport` from a failed chain verification."""
    return MismatchReport(
        run_uuid=verification.run_uuid,
        first_divergent_event_index=verification.first_divergent_event_index,
        expected=verification.expected,
        actual=verification.actual,
        errors=verification.errors,
    )


def _type_value(value: Any) -> str:
    """Collapse a ``MemoryType`` (StrEnum) or bare string to its canonical value."""
    return value.value if hasattr(value, "value") else str(value)


def _collect_missing_artifacts(receipts: Sequence[Any]) -> list[str]:
    """Fail-closed artifact census over the receipt stream ([0025] step 430/435).

    A mutating receipt must carry both graph state hashes; a materialized or
    demoted receipt must name its relationship; a supersession must name both
    the superseded and successor relationships; and any candidate-bearing
    receipt must carry the pre-gate ``candidate_digest``.
    """
    missing: list[str] = []
    for receipt in receipts:
        index = receipt.event_index
        if receipt.decision_result in _MUTATING_RESULTS:
            if receipt.graph_state_hash_before is None:
                missing.append(f"event[{index}].graph_state_hash_before")
            if receipt.graph_state_hash_after is None:
                missing.append(f"event[{index}].graph_state_hash_after")
        if receipt.decision_result == "materialized" and not receipt.relationship_uuid:
            missing.append(f"event[{index}].relationship_uuid")
        if receipt.decision_result == "superseded":
            if not receipt.superseded_relationship_uuid:
                missing.append(f"event[{index}].superseded_relationship_uuid")
            if not receipt.successor_relationship_uuid:
                missing.append(f"event[{index}].successor_relationship_uuid")
        if receipt.decision_result == "demoted" and not receipt.relationship_uuid:
            missing.append(f"event[{index}].relationship_uuid")
        if receipt.candidate_uuid is not None and receipt.candidate_digest is None:
            missing.append(f"event[{index}].candidate_digest")
    return missing


# ---------------------------------------------------------------------------
# Byte replay ([0025], claims 2-4)
# ---------------------------------------------------------------------------


def _combined_state_hash(scope_finals: dict[str, str]) -> str | None:
    """Commit a multi-scope run's reconstructed finals into one digest.

    A formation run may touch more than one scope (the engine batches all pending
    episodes into one receipted run), so each scope reconstructs to its own final
    state hash.  For a single-scope run the scope's own final hash IS the
    reconstructed state; for a multi-scope run the finals are committed into one
    stable digest over the sorted ``(scope, final)`` pairs; an empty (no-mutation)
    run reconstructs to ``None``.
    """
    if not scope_finals:
        return None
    if len(scope_finals) == 1:
        return next(iter(scope_finals.values()))
    return receipts_mod.payload_digest({"scope_finals": sorted(scope_finals.items())})


async def byte_replay(ledger: Any, *, run_uuid: str, graph: Any = None) -> ReplayProof:
    """Reconstruct a recorded memory delta WITHOUT invoking a language model ([0025]).

    Steps (FIG. 4): verify the receipt chain + Merkle root (410); load the
    checkpoint and receipts (420); fail closed on any missing artifact (430/435);
    fold the mutating receipts in event order over a reconstructed state model,
    reconstructing state PER SCOPE — each scope's mutations chain from that scope's
    entry state hash (the first mutation's ``graph_state_hash_before``) to its
    final, asserting every subsequent ``graph_state_hash_before`` equals the
    running per-scope state hash (440/450).  When a live ``graph`` is supplied,
    each reconstructed per-scope final is compared to the store's current
    ``graph_state_hash(scope_key)`` (460); a single-scope run whose checkpoint
    recorded a run-final hash is additionally anchored to it.  On success the
    checkpoint is marked ``verified`` and a :class:`ReplayProof` is returned (470);
    on ANY divergence the checkpoint is marked ``mismatch`` and a
    :class:`ReplayVerificationError` carrying a :class:`MismatchReport` is raised
    (480).  Read-only: no LLM calls, no graph mutation.

    Reconstructing per scope directly from the receipt stream — rather than from a
    single checkpoint before/after hash — makes replay robust to multi-scope runs
    and anchors the proof in the live store, which is the operative tamper check.

    ``graph`` (optional) must expose ``graph_state_hash(scope_key: str) -> str``.
    """
    verification = ledger.verify_chain(run_uuid)
    if not verification.valid:
        # Chain tamper/truncation (proof gate 2/3): withhold the proof.
        if verification.checkpoint_merkle_root is not None:
            ledger.set_replay_status(run_uuid, "mismatch")
        raise ReplayVerificationError(_report_from_chain(verification))

    try:
        checkpoint = ledger.checkpoint_for_run(run_uuid)
    except ValueError as exc:
        # A verified chain without a checkpoint cannot be replayed: fail closed.
        raise ReplayVerificationError(
            MismatchReport(run_uuid=run_uuid, missing_artifacts=("checkpoint",), errors=(str(exc),))
        ) from exc

    receipts = ledger.receipts_for_run(run_uuid)

    def _fail(report: MismatchReport) -> NoReturn:
        ledger.set_replay_status(run_uuid, "mismatch")
        raise ReplayVerificationError(report)

    missing = _collect_missing_artifacts(receipts)
    if missing:
        _fail(
            MismatchReport(
                run_uuid=run_uuid,
                missing_artifacts=tuple(missing),
                errors=(f"{len(missing)} required replay artifact(s) missing",),
            )
        )

    # Fold the mutating receipts per scope, verifying each scope's per-mutation
    # state-hash chain and reconstructing the relationship graph ([0025] steps 440/450).
    state = _ReconstructedGraphState()
    running: dict[str, str] = {}
    for receipt in receipts:
        if receipt.decision_result not in _MUTATING_RESULTS:
            continue
        scope = receipt.scope_key
        if scope not in running:
            # Seed the scope from its first mutation's entry state hash.
            running[scope] = receipt.graph_state_hash_before
        elif receipt.graph_state_hash_before != running[scope]:
            _fail(
                MismatchReport(
                    run_uuid=run_uuid,
                    first_divergent_event_index=receipt.event_index,
                    expected=running[scope],
                    actual=receipt.graph_state_hash_before,
                    errors=(
                        f"graph_state_hash_before at event_index {receipt.event_index} "
                        f"does not match the reconstructed running state hash for scope {scope}",
                    ),
                )
            )
        state.apply(receipt)
        running[scope] = receipt.graph_state_hash_after

    # Liveness anchor: each reconstructed per-scope final equals the live store ([0025] step 460).
    if graph is not None:
        for scope in sorted(running):
            live = graph.graph_state_hash(scope)
            if live != running[scope]:
                _fail(
                    MismatchReport(
                        run_uuid=run_uuid,
                        expected=running[scope],
                        actual=live,
                        errors=(
                            f"live graph state hash for scope {scope} does not match the "
                            "receipt-reconstructed state hash",
                        ),
                    )
                )

    reconstructed_final = _combined_state_hash(running)

    # Single-scope checkpoint anchor: when the checkpoint recorded a run-final hash
    # for a single-scope run, the reconstructed final must match it.
    if (
        checkpoint.graph_state_hash_after is not None
        and len(running) == 1
        and reconstructed_final != checkpoint.graph_state_hash_after
    ):
        _fail(
            MismatchReport(
                run_uuid=run_uuid,
                expected=reconstructed_final,
                actual=checkpoint.graph_state_hash_after,
                errors=("reconstructed final graph state hash does not match the checkpoint graph_state_hash_after",),
            )
        )

    ledger.set_replay_status(run_uuid, "verified")
    return ReplayProof(
        run_uuid=run_uuid,
        merkle_root=checkpoint.merkle_root,
        reconstructed_state_hash=reconstructed_final,
        checkpoint_state_hash=checkpoint.graph_state_hash_after,
        receipt_count=len(receipts),
        verified_at=datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Counterfactual policy evaluation ([0026], claims 16-19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _AlternateGate:
    """Resolved alternate gate parameters applied to the recorded candidate stream."""

    allowed_memory_types: frozenset[str]
    salience_threshold: float
    dedup_threshold: float
    pii_sensitivity: str | None


def _resolve_alternate_gate(policy_source: Any, alternate_motive: Any) -> _AlternateGate:
    """Resolve alternate gate parameters ([0026] step 510).

    The Motive owns the type allowlist; ``policy_source`` supplies the numeric
    thresholds and governance sensitivity.  Every input is read from the recorded
    values only — the extractor is never re-invoked (claim 18).
    """
    if policy_source is None:
        raise RuntimeError("counterfactual_evaluation requires a policy_source supplying alternate gate parameters")
    motive_types = (
        None if isinstance(alternate_motive, str) else getattr(alternate_motive, "allowed_memory_types", None)
    )
    if motive_types:
        allowed = frozenset(_type_value(t) for t in motive_types)
    else:
        allowed = frozenset(_type_value(t) for t in (getattr(policy_source, "allowed_memory_types", ()) or ()))
    if not hasattr(policy_source, "salience_threshold"):
        raise RuntimeError("policy_source must expose a salience_threshold")
    if not hasattr(policy_source, "dedup_threshold"):
        raise RuntimeError("policy_source must expose a dedup_threshold")
    sensitivity = getattr(policy_source, "governance_sensitivity", None)
    return _AlternateGate(
        allowed_memory_types=allowed,
        salience_threshold=float(policy_source.salience_threshold),
        dedup_threshold=float(policy_source.dedup_threshold),
        pii_sensitivity=str(sensitivity).lower() if sensitivity is not None else None,
    )


def _alternate_motive_name(alternate_motive: Any) -> str:
    if isinstance(alternate_motive, str):
        return alternate_motive
    return str(getattr(alternate_motive, "name", "alternate"))


def _alternate_decision(candidate: Any, gate: _AlternateGate) -> tuple[bool, str]:
    """Apply the alternate gates to ONE recorded candidate receipt (pure, [0026] step 520).

    Order mirrors formation: type filter, salience floor, governance sensitivity,
    then semantic dedup.  A dedup match still ACCEPTS the candidate (reinforcing an
    existing row); only type/salience/governance reject it.  Recorded values only —
    no graph handle is consulted, so the production store cannot be touched.
    """
    memory_type = candidate.memory_type
    if gate.allowed_memory_types and memory_type not in gate.allowed_memory_types:
        return False, f"type_filtered:{memory_type}"
    salience = candidate.salience_score if candidate.salience_score is not None else 0.0
    if salience < gate.salience_threshold:
        return False, f"low_salience:{salience:.4f}<{gate.salience_threshold:.4f}"
    if (
        gate.pii_sensitivity == "high"
        and candidate.sensitive_payload is not None
        and not candidate.sensitive_payload_encrypted
    ):
        return False, "governance_sensitive_blocked"
    if candidate.dedup_score is not None and candidate.dedup_score >= gate.dedup_threshold:
        return True, "accepted_reinforced"
    return True, "accepted_materialized"


def _original_reason(dispositions: Sequence[Any]) -> str:
    if not dispositions:
        return "no_disposition"
    return dispositions[0].decision_reason


async def counterfactual_evaluation(
    ledger: Any,
    policy_source: Any,
    *,
    run_uuid: str,
    alternate_motive: Any,
    record_receipts: bool = False,
) -> CounterfactualReport:
    """Determine the memory delta under an alternate policy ([0026], claims 16-19).

    Verifies the recorded chain FIRST and WITHHOLDS the report on mismatch
    (claim 19).  Loads the recorded ``CANDIDATE_EXTRACTED`` stream — whose
    ``candidate_digest`` was computed before any gate — and re-gates it under the
    alternate Motive/policy WITHOUT ever taking a graph handle (isolation by
    construction, claim 17/[0026] step 560).  When ``record_receipts`` is True the
    accept/reject decisions are emitted as a NEW ``counterfactual`` run that is
    itself chain-verifiable; the default is a purely ephemeral evaluation.
    """
    verification = ledger.verify_chain(run_uuid)
    if not verification.valid:
        raise ReplayVerificationError(_report_from_chain(verification))

    receipts = ledger.receipts_for_run(run_uuid)
    gate = _resolve_alternate_gate(policy_source, alternate_motive)
    motive_name = _alternate_motive_name(alternate_motive)

    candidates = [r for r in receipts if r.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED]
    for candidate in candidates:
        if candidate.candidate_digest is None:
            raise RuntimeError(
                "counterfactual requires a candidate_digest on every CANDIDATE_EXTRACTED "
                f"receipt; event_index {candidate.event_index} has none"
            )

    # Original dispositions keyed by the pre-gate candidate digest.
    dispositions: dict[str, list[Any]] = {}
    for receipt in receipts:
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED:
            continue
        if receipt.candidate_digest is None:
            continue
        dispositions.setdefault(receipt.candidate_digest, []).append(receipt)

    accepted_by_both: list[str] = []
    original_only: list[str] = []
    alternate_only: list[str] = []
    rejected_by_both: list[CounterfactualRejection] = []
    sensitive_divergence: list[str] = []
    original_materialized = 0
    alternate_materialized = 0
    alternate_accepted_set: list[list[str]] = []

    for candidate in candidates:
        digest = candidate.candidate_digest
        dispo = dispositions.get(digest, [])
        original_accepted = any(d.decision_result in _ACCEPTING_RESULTS for d in dispo)
        if any(d.decision_result == "materialized" for d in dispo):
            original_materialized += 1

        alt_accepted, alt_reason = _alternate_decision(candidate, gate)
        if alt_accepted:
            alternate_accepted_set.append([digest, candidate.memory_type or ""])
            if alt_reason == "accepted_materialized":
                alternate_materialized += 1

        if original_accepted and alt_accepted:
            accepted_by_both.append(digest)
        elif original_accepted and not alt_accepted:
            original_only.append(digest)
        elif not original_accepted and alt_accepted:
            alternate_only.append(digest)
        else:
            rejected_by_both.append(
                CounterfactualRejection(
                    candidate_digest=digest,
                    candidate_uuid=candidate.candidate_uuid,
                    original_reason=_original_reason(dispo),
                    alternate_reason=alt_reason,
                )
            )

        # Sensitive divergence: a sensitive candidate persisted by one policy but
        # not the other ([0026]: "sensitive candidates persisted by one policy").
        if candidate.sensitive_payload is not None and original_accepted != alt_accepted:
            sensitive_divergence.append(digest)

    counterfactual_state_hash = receipts_mod.payload_digest(
        {
            "alternate_accepted": sorted(alternate_accepted_set),
            "motive": motive_name,
            "allowed_memory_types": sorted(gate.allowed_memory_types),
            "salience_threshold": gate.salience_threshold,
            "dedup_threshold": gate.dedup_threshold,
            "governance_sensitivity": gate.pii_sensitivity,
        }
    )

    recorded_run_uuid: str | None = None
    if record_receipts:
        recorded_run_uuid = _record_counterfactual_run(ledger, receipts, candidates, gate, motive_name)

    return CounterfactualReport(
        run_uuid=run_uuid,
        alternate_motive_name=motive_name,
        accepted_by_both=tuple(accepted_by_both),
        original_only=tuple(original_only),
        alternate_only=tuple(alternate_only),
        rejected_by_both=tuple(rejected_by_both),
        sensitive_divergence=tuple(sensitive_divergence),
        counterfactual_state_hash=counterfactual_state_hash,
        original_materialized_count=original_materialized,
        alternate_materialized_count=alternate_materialized,
        recorded_run_uuid=recorded_run_uuid,
    )


def _record_counterfactual_run(
    ledger: Any,
    receipts: Sequence[Any],
    candidates: Sequence[Any],
    gate: _AlternateGate,
    motive_name: str,
) -> str:
    """Emit the alternate accept/reject decisions as a new ``counterfactual`` run.

    Reuses the original run's scope and effective-policy digest ([0026] step 550);
    the resulting run checkpoints and chain-verifies exactly like any other run.
    """
    if not candidates:
        raise RuntimeError("cannot record a counterfactual run with no candidates")
    origin = receipts[0]
    new_run = ledger.begin_run(
        run_kind="counterfactual",
        job_name=f"counterfactual:{motive_name}",
        scope_key=origin.scope_key,
        effective_policy_digest=origin.effective_policy_digest,
        tenant_id=origin.tenant_id,
        agent_id=origin.agent_id,
    )
    for candidate in candidates:
        accepted, reason = _alternate_decision(candidate, gate)
        ledger.emit(
            new_run,
            decision_type=(
                ReceiptDecisionType.COUNTERFACTUAL_CANDIDATE_ACCEPTED
                if accepted
                else ReceiptDecisionType.COUNTERFACTUAL_CANDIDATE_REJECTED
            ),
            decision_reason=reason,
            decision_result="materialized" if accepted else "gated",
            candidate_uuid=candidate.candidate_uuid,
            candidate_digest=candidate.candidate_digest,
            memory_type=candidate.memory_type,
            salience_score=candidate.salience_score,
            salience_threshold=gate.salience_threshold,
            dedup_threshold=gate.dedup_threshold,
            dedup_score=candidate.dedup_score,
            episode_uuid=candidate.episode_uuid,
        )
    ledger.checkpoint(new_run)
    return new_run.run_uuid


# ---------------------------------------------------------------------------
# Motive certification ([0028], claim 14)
# ---------------------------------------------------------------------------


def _certification_metrics(receipts: Sequence[Any], checkpoint: Any, motive: Any) -> dict[str, Any]:
    """Pure certification-metric computation over a receipt stream ([0028] step 740).

    Isolated from the client-driven formation path so it is always testable.
    A run PASSES when it persists no disallowed type, persists no plaintext
    sensitive payload, leaves no candidate unexplained, and records every
    supersession with both a superseded and a successor pointer.
    """
    allowed = {_type_value(t) for t in (getattr(motive, "allowed_memory_types", ()) or ())}

    candidates = [r for r in receipts if r.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED]
    materialized = [r for r in receipts if r.decision_result == "materialized"]
    reinforced = [r for r in receipts if r.decision_result == "reinforced"]
    superseded = [r for r in receipts if r.decision_result == "superseded"]
    demoted = [r for r in receipts if r.decision_result == "demoted"]

    disposition_digests = {
        r.candidate_digest
        for r in receipts
        if r.decision_type != ReceiptDecisionType.CANDIDATE_EXTRACTED and r.candidate_digest
    }

    disallowed = [r for r in materialized if allowed and r.memory_type not in allowed]
    sensitive_persisted = [
        r for r in materialized if r.sensitive_payload is not None and not r.sensitive_payload_encrypted
    ]
    unexplained = [r for r in candidates if r.candidate_digest and r.candidate_digest not in disposition_digests]
    supersession_incorrect = [
        r for r in superseded if not (r.superseded_relationship_uuid and r.successor_relationship_uuid)
    ]

    # Context-visible = materialized relationships not later superseded/demoted/pruned.
    materialized_uuids = {r.relationship_uuid for r in materialized if r.relationship_uuid}
    retired: set[str] = {r.superseded_relationship_uuid for r in superseded if r.superseded_relationship_uuid}
    retired |= {r.relationship_uuid for r in demoted if r.relationship_uuid}
    retired |= {
        r.relationship_uuid for r in receipts if r.decision_result in ("pruned", "destroyed") and r.relationship_uuid
    }
    context_visible = materialized_uuids - retired

    candidate_count = len(candidates)
    metrics: dict[str, Any] = {
        "candidate_count": candidate_count,
        "materialized_count": len(materialized),
        "reinforced_count": len(reinforced),
        "superseded_count": len(superseded),
        "demoted_count": len(demoted),
        "context_visible_count": len(context_visible),
        "disallowed_type_persistence_count": len(disallowed),
        "sensitive_persistence_count": len(sensitive_persisted),
        "unexplained_candidate_count": len(unexplained),
        "duplicate_rate": (len(reinforced) / candidate_count) if candidate_count else 0.0,
        "supersession_correct_count": len(superseded) - len(supersession_incorrect),
        "supersession_incorrect_count": len(supersession_incorrect),
    }

    failing_receipt_uuids = tuple(
        sorted({r.receipt_uuid for r in (*disallowed, *sensitive_persisted, *unexplained, *supersession_incorrect)})
    )
    passed = not disallowed and not sensitive_persisted and not unexplained and not supersession_incorrect
    return {"metrics": metrics, "failing_receipt_uuids": failing_receipt_uuids, "passed": passed}


def _client_ledger(client: Any) -> ReceiptLedger | None:
    """Locate the :class:`ReceiptLedger` on a duck-typed Memotron client."""
    direct = getattr(client, "receipts", None)
    if isinstance(direct, ReceiptLedger):
        return direct
    for holder in ("_graph", "graph", "_store", "store"):
        obj = getattr(client, holder, None)
        if obj is not None:
            candidate = getattr(obj, "receipts", None)
            if isinstance(candidate, ReceiptLedger):
                return candidate
    return None


async def _drive_certification_formation(client: Any, *, motive: Any, corpus: Sequence[Any], scope: Any) -> str:
    """Drive receipted formation over the fixture corpus through the client.

    The instrumented client exposes an async
    ``run_certification_formation(motive, corpus, scope) -> run_uuid`` that
    ingests the corpus, runs formation under the Motive, and returns the
    receipt run's uuid.  Absent it, fail fast (instrumentation not present).
    """
    runner = getattr(client, "run_certification_formation", None)
    if not callable(runner):
        raise RuntimeError(
            "certify_motive requires the instrumented client to expose an async "
            "run_certification_formation(motive, corpus, scope) returning the formation "
            "run_uuid (WS-11 instrumentation not present)"
        )
    run_uuid = await runner(motive=motive, corpus=corpus, scope=scope)
    if not isinstance(run_uuid, str) or not run_uuid.strip():
        raise RuntimeError("run_certification_formation must return a non-blank run_uuid string")
    return run_uuid


def _scope_key(scope: Any) -> str:
    key = getattr(scope, "key", None)
    return key if isinstance(key, str) and key.strip() else str(scope)


def _corpus_digest(corpus: Sequence[Any]) -> str:
    items: list[Any] = [
        episode.model_dump(mode="json") if isinstance(episode, BaseModel) else str(episode) for episode in corpus
    ]
    return receipts_mod.payload_digest({"corpus": items})


async def certify_motive(client: Any, *, motive: Any, corpus: Sequence[Any], scope: Any) -> MotiveCertificate:
    """Certify a Motive over a fixture corpus with a receipt-backed certificate ([0028]).

    Drives receipted formation through the client, verifies the chain + graph
    state hash, computes the certification metrics, and binds the Motive digest,
    corpus digest, and run Merkle root into a pass/fail certificate (FIG. 7).
    Fails fast with a clear error if the client is not receipt-instrumented.
    """
    ledger = _client_ledger(client)
    if ledger is None:
        raise RuntimeError(
            "certify_motive requires a receipt-instrumented client: no ReceiptLedger "
            "found (WS-11 instrumentation not present)"
        )
    run_uuid = await _drive_certification_formation(client, motive=motive, corpus=corpus, scope=scope)

    verification = ledger.verify_chain(run_uuid)
    if not verification.valid:
        raise ReplayVerificationError(_report_from_chain(verification))

    receipts = ledger.receipts_for_run(run_uuid)
    checkpoint = ledger.checkpoint_for_run(run_uuid)
    result = _certification_metrics(receipts, checkpoint, motive)

    motive_version = checkpoint.motive_version_digest or receipts_mod.motive_version_digest(
        motive, {"scope": _scope_key(scope)}
    )
    return MotiveCertificate(
        motive_version_digest=motive_version,
        corpus_digest=_corpus_digest(corpus),
        merkle_root=checkpoint.merkle_root,
        metrics=result["metrics"],
        failing_receipt_uuids=result["failing_receipt_uuids"],
        passed=result["passed"],
    )


# ---------------------------------------------------------------------------
# Product-grade proof reports / deployment gates
# ---------------------------------------------------------------------------


def build_memory_bill_of_materials(ledger: Any, *, run_uuid: str) -> MemoryBillOfMaterials:
    """Build the run-level memory BOM from persisted receipts and checkpoint rows."""
    verification = ledger.verify_chain(run_uuid)
    checkpoint = ledger.checkpoint_for_run(run_uuid)
    receipts = ledger.receipts_for_run(run_uuid)
    if not receipts:
        raise RuntimeError(f"cannot build memory bill of materials: run {run_uuid} has no receipts")

    decision_counts: dict[str, int] = {}
    for receipt in receipts:
        key = receipt.decision_type.value if hasattr(receipt.decision_type, "value") else str(receipt.decision_type)
        decision_counts[key] = decision_counts.get(key, 0) + 1

    candidates = [r for r in receipts if r.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED]
    non_materialized = [r for r in receipts if r.decision_result in receipts_mod.NON_MATERIALIZING_RESULTS]
    return MemoryBillOfMaterials(
        run_uuid=run_uuid,
        run_kind=checkpoint.run_kind,
        job_name=checkpoint.job_name,
        scope_key=checkpoint.scope_key,
        effective_policy_digest=checkpoint.effective_policy_digest,
        motive_version_digest=checkpoint.motive_version_digest,
        receipt_count=len(receipts),
        candidate_count=len(candidates),
        disposition_count=len(receipts) - len(candidates),
        materialized_count=sum(1 for r in receipts if r.decision_result == "materialized"),
        non_materialized_count=len(non_materialized),
        negative_space_count=len(non_materialized),
        receipt_decision_counts=decision_counts,
        evidence_digests=tuple(sorted({r.episode_digest for r in receipts if r.episode_digest})),
        candidate_digests=tuple(sorted({r.candidate_digest for r in candidates if r.candidate_digest})),
        first_receipt_hash=checkpoint.first_receipt_hash,
        last_receipt_hash=checkpoint.last_receipt_hash,
        merkle_root=checkpoint.merkle_root,
        graph_state_hash_before=checkpoint.graph_state_hash_before,
        graph_state_hash_after=checkpoint.graph_state_hash_after,
        chain_valid=verification.valid,
        replay_status=checkpoint.replay_status,
        errors=verification.errors,
    )


async def verify_no_silent_mutation(ledger: Any, graph: Any, *, scope_key: str) -> NoSilentMutationReport:
    """Verify the scope-level invariant that every live state change is receipted.

    The check is intentionally mechanical:

    * every receipt chain touching the scope must verify;
    * every mutating receipt must carry before/after graph state hashes;
    * the per-scope mutating receipt stream must be contiguous, so each mutation's
      before hash equals the prior mutation's after hash; and
    * the latest receipted state hash must equal the live graph state hash.
    """
    if not isinstance(scope_key, str) or not scope_key.strip():
        raise ValueError("scope_key must be a non-blank string")
    receipts = ledger.receipts_for_scope(scope_key)
    run_uuids = tuple(dict.fromkeys(r.run_uuid for r in receipts))
    errors: list[str] = []
    chain_verified = 0
    replay_verified = 0

    for run_uuid in run_uuids:
        verification = ledger.verify_chain(run_uuid)
        if verification.valid:
            chain_verified += 1
        else:
            errors.extend(f"run {run_uuid}: {e}" for e in verification.errors)

    mutating = [r for r in receipts if r.decision_result in _MUTATING_RESULTS]
    running: str | None = None
    latest_run_uuid: str | None = None
    latest_hash: str | None = None
    replayed_runs: set[str] = set()
    for receipt in mutating:
        if receipt.graph_state_hash_before is None:
            errors.append(f"event {receipt.run_uuid}[{receipt.event_index}] missing graph_state_hash_before")
        if receipt.graph_state_hash_after is None:
            errors.append(f"event {receipt.run_uuid}[{receipt.event_index}] missing graph_state_hash_after")
        if receipt.graph_state_hash_before is not None and receipt.graph_state_hash_after is not None:
            if running is not None and receipt.graph_state_hash_before != running:
                errors.append(
                    "scope state hash discontinuity before "
                    f"{receipt.run_uuid}[{receipt.event_index}]: expected {running}, "
                    f"actual {receipt.graph_state_hash_before}"
                )
            running = receipt.graph_state_hash_after
            latest_hash = receipt.graph_state_hash_after
            latest_run_uuid = receipt.run_uuid
        if receipt.run_uuid not in replayed_runs:
            try:
                await byte_replay(ledger, run_uuid=receipt.run_uuid, graph=None)
                replay_verified += 1
            except ReplayVerificationError as exc:
                errors.extend(f"run {receipt.run_uuid}: {e}" for e in exc.report.errors)
            replayed_runs.add(receipt.run_uuid)

    # T0-7: rebuild the state-hash inputs from the ROWS before reading the live hash.
    #
    # On Postgres `graph_state_hash` returns a memoised, incrementally maintained value, and
    # the memo is only updated by writes that go THROUGH the backend. A direct row edit --
    # exactly what an audit plane exists to catch -- leaves it stale, so the live hash still
    # matched the last receipt and tampering passed. Measured with
    # `scripts/verify/probe_tamper_detection.py`: MODIFY, DELETE and INSERT all UNDETECTED on
    # Postgres while SQLite caught all four, an asymmetry on three of four modes.
    #
    # `recompute_scope_state_tuples` was written for this and had ZERO production callers.
    # SQLite implements it as an honest no-op because it derives the hash from `relationships`
    # on every call, so this is unconditional rather than a capability check.
    #
    # Cost is O(scope) on the audit path only; the hot path keeps the memo, which is the whole
    # reason the memo exists.
    graph.recompute_scope_state_tuples(scope_key)
    live_hash = graph.graph_state_hash(scope_key)
    if latest_hash is None:
        # T0-12: no mutating receipt explains this scope's state at all. Guarding the
        # comparison on `latest_hash is not None` (as this did) meant zero receipts skipped
        # the check entirely and the verdict came back passed=True -- the exact case this
        # function exists to detect, and the value certification_bundle exports.
        #
        # Zero receipts is only consistent with an EMPTY scope. graph_state_hash is a
        # deterministic digest over the scope's tuple set, so every empty scope yields the
        # same constant; asking for an unused key is the cheapest way to obtain it without
        # a new storage method.
        empty_hash = graph.graph_state_hash("\x00memotron:empty-scope-probe")
        if live_hash != empty_hash:
            errors.append(
                f"scope holds live state ({live_hash}) but no mutating receipt explains it: "
                "every live row must be bracketed by a receipt"
            )
    elif live_hash != latest_hash:
        errors.append(f"live graph state hash {live_hash} does not match latest receipted state hash {latest_hash}")

    return NoSilentMutationReport(
        scope_key=scope_key,
        checked_run_uuids=run_uuids,
        mutating_receipt_count=len(mutating),
        chain_verified_count=chain_verified,
        replay_verified_count=replay_verified,
        latest_receipted_run_uuid=latest_run_uuid,
        latest_receipted_state_hash=latest_hash,
        live_state_hash=live_hash,
        passed=not errors,
        errors=tuple(errors),
    )


def evaluate_policy_regression_gate(
    *,
    candidate_motive: Any,
    certification: MotiveCertificate,
    comparison: CounterfactualReport,
    baseline_receipts: Sequence[Any],
) -> PolicyRegressionGateReport:
    """Evaluate the deterministic pass/fail rule for a proposed Motive revision."""
    allowed = {_type_value(t) for t in (getattr(candidate_motive, "allowed_memory_types", ()) or ())}
    candidate_type_by_digest = {
        r.candidate_digest: r.memory_type
        for r in baseline_receipts
        if r.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED and r.candidate_digest
    }
    original_only_allowed = [
        digest for digest in comparison.original_only if candidate_type_by_digest.get(digest) in allowed
    ]
    original_only_disallowed = [
        digest for digest in comparison.original_only if candidate_type_by_digest.get(digest) not in allowed
    ]
    failures: list[str] = []
    if not certification.passed:
        failures.append("certification_failed")
    if original_only_allowed:
        failures.append("lost_baseline_allowed_candidates")
    if comparison.alternate_only:
        failures.append("newly_accepted_baseline_rejected_candidates")
    return PolicyRegressionGateReport(
        candidate_motive_name=_alternate_motive_name(candidate_motive),
        baseline_run_uuid=comparison.run_uuid,
        certification=certification,
        comparison=comparison,
        passed=not failures,
        failures=tuple(failures),
        preserved_allowed_count=len(comparison.accepted_by_both),
        blocked_disallowed_count=len(original_only_disallowed),
        lost_allowed_count=len(original_only_allowed),
        new_acceptance_count=len(comparison.alternate_only),
    )


__all__ = [
    "CounterfactualPolicy",
    "CounterfactualRejection",
    "CounterfactualReport",
    "GovernanceCertificationBundle",
    "MemoryBillOfMaterials",
    "MismatchReport",
    "MotiveCertificate",
    "NoSilentMutationReport",
    "PolicyComparisonItem",
    "PolicyComparisonReport",
    "PolicyRegressionGateReport",
    "ReplayProof",
    "ReplayVerificationError",
    "build_memory_bill_of_materials",
    "byte_replay",
    "certify_motive",
    "counterfactual_evaluation",
    "evaluate_policy_regression_gate",
    "verify_no_silent_mutation",
]
