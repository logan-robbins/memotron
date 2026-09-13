"""WS-11: Replay engine tests (PATENT_REPLAY_RECEIPTS_SPEC.md [0025], [0026], [0028]).

Covers byte replay (fail-closed reconstruction, claims 2-4), counterfactual
policy evaluation (isolated delta, claims 16-19), and Motive certification
(pure metric helper + guarded driver, claim 14).  Synthetic receipt streams are
built by hand through ``ReceiptLedger.emit`` with chained graph state hashes;
no live formation pipeline is needed.  The ledger is a hermetic tmp_path sqlite.
"""

from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime

import pytest

from memotron.config import Motive
from memotron.models import MemoryType
from memotron.receipts import (
    ReceiptDecisionType,
    ReceiptLedger,
    config_effective_policy_digest,
)
from memotron.replay import (
    CounterfactualPolicy,
    CounterfactualReport,
    MotiveCertificate,
    ReplayProof,
    ReplayVerificationError,
    _certification_metrics,
    byte_replay,
    certify_motive,
    counterfactual_evaluation,
)

CE = ReceiptDecisionType.CANDIDATE_EXTRACTED
FRC = ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED
POLICY = config_effective_policy_digest({"dedup": {"cosine_threshold": 0.88}})
BASE_TIME = datetime(2026, 7, 6, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ledger(tmp_path, name: str = "replay.sqlite") -> ReceiptLedger:
    return ReceiptLedger(sqlite3.connect(tmp_path / name))


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _row_count(ledger: ReceiptLedger, table: str) -> int:
    cursor = ledger._connection.execute(f"SELECT COUNT(*) FROM {table}")
    return int(cursor.fetchone()[0])


def _build_formation_run(
    ledger: ReceiptLedger,
    *,
    scope: str = "user:alice",
    seed: str = "run",
    break_before: str | None = None,
    omit_after: bool = False,
):
    """Emit a two-materialization formation run with a chained graph state hash.

    ``break_before`` overrides the SECOND materialization's graph_state_hash_before
    to force a state-hash-chain divergence; ``omit_after`` drops the FIRST
    materialization's graph_state_hash_after to force a missing-artifact failure.
    """
    h0 = _sha(f"{seed}-state-0")
    h1 = _sha(f"{seed}-state-1")
    h2 = _sha(f"{seed}-state-2")
    run = ledger.begin_run(
        run_kind="formation",
        job_name="formation-default",
        scope_key=scope,
        effective_policy_digest=POLICY,
        graph_state_hash_before=h0,
    )
    cd1 = _sha(f"{seed}-cand-1")
    ledger.emit(
        run,
        decision_type=CE,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_uuid="c1",
        candidate_digest=cd1,
        memory_type="preference",
        salience_score=0.90,
        created_at=BASE_TIME,
    )
    ledger.emit(
        run,
        decision_type=FRC,
        decision_reason="relationship_created",
        decision_result="materialized",
        candidate_uuid="c1",
        candidate_digest=cd1,
        memory_type="preference",
        relationship_uuid="rel-1",
        truth_key="user:alice|prefers",
        graph_state_hash_before=h0,
        graph_state_hash_after=None if omit_after else h1,
        created_at=BASE_TIME,
    )
    cd2 = _sha(f"{seed}-cand-2")
    ledger.emit(
        run,
        decision_type=CE,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_uuid="c2",
        candidate_digest=cd2,
        memory_type="requirement",
        salience_score=0.80,
        created_at=BASE_TIME,
    )
    ledger.emit(
        run,
        decision_type=FRC,
        decision_reason="relationship_created",
        decision_result="materialized",
        candidate_uuid="c2",
        candidate_digest=cd2,
        memory_type="requirement",
        relationship_uuid="rel-2",
        truth_key="user:alice|requires",
        graph_state_hash_before=break_before if break_before is not None else h1,
        graph_state_hash_after=h2,
        created_at=BASE_TIME,
    )
    checkpoint = ledger.checkpoint(run, graph_state_hash_after=h0 if omit_after else h2)
    return run, checkpoint, {"h0": h0, "h1": h1, "h2": h2}


class _GraphStub:
    """Minimal stand-in exposing only ``graph_state_hash(scope_key)``."""

    def __init__(self, value: str) -> None:
        self._value = value
        self.calls: list[str] = []

    def graph_state_hash(self, scope_key: str) -> str:
        self.calls.append(scope_key)
        return self._value


# ---------------------------------------------------------------------------
# Byte replay ([0025], claims 2-4)
# ---------------------------------------------------------------------------


async def test_byte_replay_happy_path_marks_verified(tmp_path):
    ledger = _ledger(tmp_path)
    run, checkpoint, h = _build_formation_run(ledger)

    proof = await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)

    assert isinstance(proof, ReplayProof)
    assert proof.run_uuid == run.run_uuid
    assert proof.merkle_root == checkpoint.merkle_root
    assert proof.reconstructed_state_hash == h["h2"]
    assert proof.checkpoint_state_hash == h["h2"]
    assert proof.receipt_count == 4
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "verified"


async def test_byte_replay_is_deterministic_across_replays(tmp_path):
    # [0025]: one recorded run yields arbitrarily many identical reconstructions.
    ledger = _ledger(tmp_path)
    run, _checkpoint, _h = _build_formation_run(ledger)

    first = await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)
    second = await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)

    assert first.merkle_root == second.merkle_root
    assert first.reconstructed_state_hash == second.reconstructed_state_hash
    assert first.receipt_count == second.receipt_count


async def test_byte_replay_fails_closed_on_tampered_receipt(tmp_path):
    # Proof gate 2: tamper a persisted column → chain verify fails → proof withheld.
    ledger = _ledger(tmp_path)
    run, _checkpoint, _h = _build_formation_run(ledger)
    ledger._connection.execute(
        "UPDATE memory_receipts SET salience_score = 0.01 WHERE run_uuid = ? AND event_index = 0",
        (run.run_uuid,),
    )
    ledger._connection.commit()

    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)

    assert excinfo.value.report.first_divergent_event_index == 0
    assert excinfo.value.report.expected != excinfo.value.report.actual
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "mismatch"


async def test_byte_replay_fails_closed_on_missing_artifact(tmp_path):
    # Proof gate 3: a materialized receipt with no graph_state_hash_after.
    ledger = _ledger(tmp_path)
    run, _checkpoint, _h = _build_formation_run(ledger, omit_after=True)

    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)

    report = excinfo.value.report
    assert report.missing_artifacts
    assert any("graph_state_hash_after" in artifact for artifact in report.missing_artifacts)
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "mismatch"


async def test_byte_replay_fails_closed_on_missing_checkpoint(tmp_path):
    # A verified chain with no checkpoint cannot be replayed.
    ledger = _ledger(tmp_path)
    run = ledger.begin_run(
        run_kind="formation",
        job_name="formation-default",
        scope_key="user:alice",
        effective_policy_digest=POLICY,
        graph_state_hash_before=_sha("s0"),
    )
    ledger.emit(
        run,
        decision_type=CE,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_uuid="c1",
        candidate_digest=_sha("cand"),
        created_at=BASE_TIME,
    )
    # No checkpoint written.
    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)
    assert excinfo.value.report.missing_artifacts == ("checkpoint",)


async def test_byte_replay_detects_state_hash_chain_break(tmp_path):
    ledger = _ledger(tmp_path)
    run, _checkpoint, _h = _build_formation_run(ledger, break_before=_sha("WRONG"))

    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run.run_uuid, graph=None)

    report = excinfo.value.report
    # The break is on the SECOND materialization (event_index 3).
    assert report.first_divergent_event_index == 3
    assert report.missing_artifacts == ()
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "mismatch"


async def test_byte_replay_with_live_graph_stub(tmp_path):
    ledger = _ledger(tmp_path)
    run, _checkpoint, h = _build_formation_run(ledger)

    # Wrong live state hash → fail closed.
    wrong = _GraphStub(_sha("not-the-state"))
    with pytest.raises(ReplayVerificationError) as excinfo:
        await byte_replay(ledger, run_uuid=run.run_uuid, graph=wrong)
    assert excinfo.value.report.actual == wrong._value
    assert wrong.calls == ["user:alice"]

    # Correct live state hash → ReplayProof.
    correct = _GraphStub(h["h2"])
    proof = await byte_replay(ledger, run_uuid=run.run_uuid, graph=correct)
    assert proof.reconstructed_state_hash == h["h2"]
    assert correct.calls == ["user:alice"]
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "verified"


# ---------------------------------------------------------------------------
# Counterfactual policy evaluation ([0026], claims 16-19)
# ---------------------------------------------------------------------------


def _build_counterfactual_run(ledger: ReceiptLedger):
    """Three materialized candidates: preference, requirement, sensitive-requirement."""
    hashes = [_sha("cf-state-0")]
    run = ledger.begin_run(
        run_kind="formation",
        job_name="formation-default",
        scope_key="user:bob",
        effective_policy_digest=POLICY,
        graph_state_hash_before=hashes[0],
    )

    def candidate(digest: str, memory_type: str, salience: float, sensitive: str | None = None):
        ledger.emit(
            run,
            decision_type=CE,
            decision_reason="candidate_extracted",
            decision_result="observed",
            candidate_uuid=digest,
            candidate_digest=digest,
            memory_type=memory_type,
            salience_score=salience,
            sensitive_payload=sensitive,
            episode_uuid="ep-1",
            created_at=BASE_TIME,
        )

    def materialize(digest: str, memory_type: str, rel: str):
        before = hashes[-1]
        after = _sha(f"cf-state-{rel}")
        hashes.append(after)
        ledger.emit(
            run,
            decision_type=FRC,
            decision_reason="relationship_created",
            decision_result="materialized",
            candidate_uuid=digest,
            candidate_digest=digest,
            memory_type=memory_type,
            relationship_uuid=rel,
            truth_key=f"tk-{rel}",
            graph_state_hash_before=before,
            graph_state_hash_after=after,
            created_at=BASE_TIME,
        )

    cd_pref, cd_req, cd_sens = _sha("cf-pref"), _sha("cf-req"), _sha("cf-sens")
    candidate(cd_pref, "preference", 0.90)
    materialize(cd_pref, "preference", "rel-pref")
    candidate(cd_req, "requirement", 0.90)
    materialize(cd_req, "requirement", "rel-req")
    candidate(cd_sens, "requirement", 0.90, sensitive="[REDACTED-PHONE]")
    materialize(cd_sens, "requirement", "rel-sens")
    checkpoint = ledger.checkpoint(run, graph_state_hash_after=hashes[-1])
    return run, checkpoint, {"pref": cd_pref, "req": cd_req, "sens": cd_sens}


async def test_counterfactual_flips_accepted_set_and_leaves_store_untouched(tmp_path):
    ledger = _ledger(tmp_path)
    run, _checkpoint, cd = _build_counterfactual_run(ledger)
    receipts_before = _row_count(ledger, "memory_receipts")
    checkpoints_before = _row_count(ledger, "run_checkpoints")

    # Alternate Motive allows ONLY requirement; alternate governance is high.
    alternate_motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    policy = CounterfactualPolicy(salience_threshold=0.0, dedup_threshold=1.0, governance_sensitivity="high")

    report = await counterfactual_evaluation(ledger, policy, run_uuid=run.run_uuid, alternate_motive=alternate_motive)

    assert isinstance(report, CounterfactualReport)
    assert report.alternate_motive_name == "requirements-only"
    # requirement (non-sensitive) survives both policies.
    assert report.accepted_by_both == (cd["req"],)
    # preference (type-filtered) + sensitive-requirement (governance-blocked) flip out.
    assert set(report.original_only) == {cd["pref"], cd["sens"]}
    assert report.alternate_only == ()
    assert report.sensitive_divergence == (cd["sens"],)
    assert report.original_materialized_count == 3
    assert report.alternate_materialized_count == 1
    assert report.recorded_run_uuid is None

    # Isolation by construction: nothing was written (ephemeral evaluation).
    assert _row_count(ledger, "memory_receipts") == receipts_before
    assert _row_count(ledger, "run_checkpoints") == checkpoints_before


async def test_counterfactual_record_receipts_writes_verifiable_run(tmp_path):
    ledger = _ledger(tmp_path)
    run, _checkpoint, _cd = _build_counterfactual_run(ledger)
    receipts_before = _row_count(ledger, "memory_receipts")

    alternate_motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    policy = CounterfactualPolicy(salience_threshold=0.0, dedup_threshold=1.0)

    report = await counterfactual_evaluation(
        ledger,
        policy,
        run_uuid=run.run_uuid,
        alternate_motive=alternate_motive,
        record_receipts=True,
    )

    assert report.recorded_run_uuid is not None
    # A new counterfactual run of exactly three decisions was recorded.
    assert _row_count(ledger, "memory_receipts") == receipts_before + 3
    recorded = ledger.checkpoint_for_run(report.recorded_run_uuid)
    assert recorded.run_kind == "counterfactual"
    # The recorded run is itself chain-verifiable.
    assert ledger.verify_chain(report.recorded_run_uuid).valid


async def test_counterfactual_withheld_on_tampered_chain(tmp_path):
    ledger = _ledger(tmp_path)
    run, _checkpoint, _cd = _build_counterfactual_run(ledger)
    ledger._connection.execute(
        "UPDATE memory_receipts SET memory_type = 'directive' WHERE run_uuid = ? AND event_index = 0",
        (run.run_uuid,),
    )
    ledger._connection.commit()

    policy = CounterfactualPolicy(salience_threshold=0.0, dedup_threshold=1.0)
    with pytest.raises(ReplayVerificationError):
        await counterfactual_evaluation(ledger, policy, run_uuid=run.run_uuid, alternate_motive="requirements-only")


# ---------------------------------------------------------------------------
# Motive certification ([0028], claim 14)
# ---------------------------------------------------------------------------


def _emit_typed_materialization(ledger: ReceiptLedger, run, *, memory_type: str, rel: str, seed: str):
    digest = _sha(f"{seed}-{rel}")
    ledger.emit(
        run,
        decision_type=CE,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_uuid=rel,
        candidate_digest=digest,
        memory_type=memory_type,
        salience_score=0.9,
        created_at=BASE_TIME,
    )
    return ledger.emit(
        run,
        decision_type=FRC,
        decision_reason="relationship_created",
        decision_result="materialized",
        candidate_uuid=rel,
        candidate_digest=digest,
        memory_type=memory_type,
        relationship_uuid=rel,
        truth_key=f"tk-{rel}",
        graph_state_hash_before=_sha(f"{seed}-{rel}-before"),
        graph_state_hash_after=_sha(f"{seed}-{rel}-after"),
        created_at=BASE_TIME,
    )


def test_certification_metrics_flags_disallowed_type(tmp_path):
    ledger = _ledger(tmp_path)
    run = ledger.begin_run(
        run_kind="formation",
        job_name="formation-default",
        scope_key="user:carol",
        effective_policy_digest=POLICY,
        graph_state_hash_before=_sha("s0"),
    )
    _emit_typed_materialization(ledger, run, memory_type="requirement", rel="rel-ok", seed="cert")
    bad = _emit_typed_materialization(ledger, run, memory_type="preference", rel="rel-bad", seed="cert")
    checkpoint = ledger.checkpoint(run, graph_state_hash_after=_sha("s-final"))

    motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    result = _certification_metrics(ledger.receipts_for_run(run.run_uuid), checkpoint, motive)

    assert result["passed"] is False
    assert bad.receipt_uuid in result["failing_receipt_uuids"]
    assert result["metrics"]["disallowed_type_persistence_count"] == 1
    assert result["metrics"]["materialized_count"] == 2
    assert result["metrics"]["unexplained_candidate_count"] == 0


def test_certification_metrics_clean_run_passes(tmp_path):
    ledger = _ledger(tmp_path)
    run = ledger.begin_run(
        run_kind="formation",
        job_name="formation-default",
        scope_key="user:carol",
        effective_policy_digest=POLICY,
        graph_state_hash_before=_sha("s0"),
    )
    _emit_typed_materialization(ledger, run, memory_type="requirement", rel="rel-a", seed="clean")
    _emit_typed_materialization(ledger, run, memory_type="requirement", rel="rel-b", seed="clean")
    checkpoint = ledger.checkpoint(run, graph_state_hash_after=_sha("s-final"))

    motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    result = _certification_metrics(ledger.receipts_for_run(run.run_uuid), checkpoint, motive)

    assert result["passed"] is True
    assert result["failing_receipt_uuids"] == ()
    assert result["metrics"]["context_visible_count"] == 2


async def test_certify_motive_requires_receipt_instrumented_client():
    class _BareClient:
        pass

    motive = Motive(name="m", goal="g")
    with pytest.raises(RuntimeError, match="receipt-instrumented"):
        await certify_motive(_BareClient(), motive=motive, corpus=[], scope="user:x")


async def test_certify_motive_requires_formation_capability(tmp_path):
    class _LedgerOnlyClient:
        def __init__(self, ledger: ReceiptLedger) -> None:
            self.receipts = ledger

    motive = Motive(name="m", goal="g")
    with pytest.raises(RuntimeError, match="run_certification_formation"):
        await certify_motive(_LedgerOnlyClient(_ledger(tmp_path)), motive=motive, corpus=[], scope="user:x")


async def test_certify_motive_end_to_end_with_stub_client(tmp_path):
    # Exercises the full guarded driver against a stub that emits a receipted run.
    ledger = _ledger(tmp_path)

    class _StubClient:
        def __init__(self, ledger: ReceiptLedger) -> None:
            self.receipts = ledger

        async def run_certification_formation(self, *, motive, corpus, scope) -> str:
            run = ledger.begin_run(
                run_kind="formation",
                job_name="certify",
                scope_key=scope if isinstance(scope, str) else scope.key,
                effective_policy_digest=POLICY,
                graph_state_hash_before=_sha("cert-s0"),
                motive_version_digest=_sha("motive-version"),
            )
            _emit_typed_materialization(ledger, run, memory_type="requirement", rel="rel-a", seed="e2e")
            ledger.checkpoint(run, graph_state_hash_after=_sha("cert-final"))
            return run.run_uuid

    motive = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    certificate = await certify_motive(
        _StubClient(ledger), motive=motive, corpus=["episode-1", "episode-2"], scope="user:dave"
    )

    assert isinstance(certificate, MotiveCertificate)
    assert certificate.passed is True
    assert certificate.motive_version_digest == _sha("motive-version")
    assert certificate.metrics["materialized_count"] == 1
    assert len(certificate.merkle_root) == 64
    assert len(certificate.corpus_digest) == 64
