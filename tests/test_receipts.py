"""WS-11: Receipts core unit tests (PATENT_REPLAY_RECEIPTS_SPEC.md [0019]-[0023], [0027]).

Covers deterministic canonicalization, the "|"-joined hash chain, Merkle-root
tamper sensitivity, persisted-row tamper/deletion detection, checkpoint
round-trips, negative-space filtering, policy-digest sensitivity (claim 7),
and sensitive-payload storage (claim 10).  The ledger is synchronous, so
these tests are synchronous.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import BaseModel, Field

from memotron.models import ExtractedMemory, MemoryScope, ScopeKind
from memotron.receipts import (
    DECISION_RESULTS,
    GENESIS_RECEIPT_HASH,
    NON_MATERIALIZING_RESULTS,
    ChainVerification,
    MemoryReceipt,
    ReceiptDecisionType,
    ReceiptLedger,
    ReceiptRun,
    RunCheckpoint,
    candidate_digest,
    canonicalize_payload,
    config_effective_policy_digest,
    effective_policy_digest,
    evidence_digest,
    governance_policy_digest,
    merkle_root,
    motive_version_digest,
    payload_digest,
    receipt_hash,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ledger(tmp_path, name: str = "receipts.sqlite") -> ReceiptLedger:
    connection = sqlite3.connect(tmp_path / name)
    return ReceiptLedger(connection)


def _begin_run(ledger: ReceiptLedger, **overrides) -> ReceiptRun:
    fields = {
        "run_kind": "formation",
        "job_name": "formation-default",
        "scope_key": "user:alice",
        "effective_policy_digest": config_effective_policy_digest({"dedup": {"threshold": 0.88}}),
    }
    fields.update(overrides)
    return ledger.begin_run(**fields)


def _emit(ledger: ReceiptLedger, run: ReceiptRun, **overrides) -> MemoryReceipt:
    fields = {
        "decision_type": ReceiptDecisionType.CANDIDATE_EXTRACTED,
        "decision_reason": "candidate_extracted",
        "decision_result": "observed",
    }
    fields.update(overrides)
    return ledger.emit(run, **fields)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Policy(BaseModel):
    """Stand-in effective policy for the pydantic-based digest builder."""

    model_config = {"frozen": True}

    tenant_id: str
    dedup_threshold: float
    source_trace: dict[str, str] = Field(default_factory=dict)


class _Motive(BaseModel):
    name: str
    allowed_memory_types: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Canonicalization
# ---------------------------------------------------------------------------


def test_canonicalization_is_deterministic_and_key_order_independent():
    when = datetime(2026, 7, 6, 12, 30, 0, 123456, tzinfo=UTC)
    forward = {"alpha": 1, "beta": [1, 2, 3], "gamma": {"x": 0.5, "y": None}, "when": when}
    reversed_keys = {"when": when, "gamma": {"y": None, "x": 0.5}, "beta": [1, 2, 3], "alpha": 1}
    assert canonicalize_payload(forward) == canonicalize_payload(forward)
    assert canonicalize_payload(forward) == canonicalize_payload(reversed_keys)
    # List order is semantic and must NOT be normalized away.
    assert canonicalize_payload({"beta": [3, 2, 1]}) != canonicalize_payload({"beta": [1, 2, 3]})
    # Timestamps: ISO-8601 UTC, microsecond precision, +00:00 suffix.
    assert b"2026-07-06T12:30:00.123456+00:00" in canonicalize_payload({"when": when})


def test_canonicalization_naive_datetime_is_hard_error():
    with pytest.raises(ValueError, match="naive datetime"):
        # Naive on purpose -- rejecting it is what this test asserts.
        canonicalize_payload({"when": datetime(2026, 7, 6, 12, 0, 0)})  # noqa: DTZ001


def test_canonicalization_nan_and_inf_are_hard_errors():
    with pytest.raises(ValueError, match="NaN/Inf"):
        canonicalize_payload({"score": float("nan")})
    with pytest.raises(ValueError, match="NaN/Inf"):
        canonicalize_payload({"score": float("inf")})


def test_canonicalization_rejects_unsupported_types_and_non_string_keys():
    with pytest.raises(ValueError, match="unsupported type"):
        canonicalize_payload({"value": object()})
    with pytest.raises(ValueError, match="unsupported type"):
        canonicalize_payload({"value": {"a", "b"}})
    with pytest.raises(ValueError, match="keys must be str"):
        canonicalize_payload({1: "x"})


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------


def test_hash_chain_formula_and_event_index_density(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    receipts = [_emit(ledger, run) for _ in range(3)]

    assert [receipt.event_index for receipt in receipts] == [0, 1, 2]
    assert receipts[0].previous_receipt_hash == GENESIS_RECEIPT_HASH
    assert receipts[1].previous_receipt_hash == receipts[0].receipt_hash
    assert receipts[2].previous_receipt_hash == receipts[1].receipt_hash

    # Exact "|"-joined sha256 formula ([0023]).
    for index, receipt in enumerate(receipts):
        expected = _sha256(f"1|{receipt.previous_receipt_hash}|{receipt.payload_digest}|{run.run_uuid}|{index}")
        assert receipt.receipt_hash == expected
        assert receipt.receipt_hash == receipt_hash(
            schema_version=1,
            previous_receipt_hash=receipt.previous_receipt_hash,
            payload_digest=receipt.payload_digest,
            run_uuid=run.run_uuid,
            event_index=index,
        )
        assert receipt.payload_digest == _sha256(receipt.canonical_payload)


def test_canonical_payload_excludes_derived_fields_and_includes_stable_nulls(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    receipt = _emit(ledger, run)
    payload = json.loads(receipt.canonical_payload)
    for derived in ("canonical_payload", "payload_digest", "previous_receipt_hash", "receipt_hash"):
        assert derived not in payload
    # Optional fields not applicable are included as nulls (stable shape).
    assert payload["candidate_digest"] is None
    assert payload["receipt_uuid"] == receipt.receipt_uuid
    assert payload["event_index"] == 0


def test_emit_fail_fast_validation(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    with pytest.raises(ValueError, match="unknown receipt fields"):
        _emit(ledger, run, not_a_field="x")
    with pytest.raises(ValueError, match="decision_result"):
        _emit(ledger, run, decision_result="not-a-result")
    with pytest.raises(ValueError, match="timezone-aware"):
        _emit(ledger, run, created_at=datetime(2026, 7, 6, 12, 0, 0))  # noqa: DTZ001
    with pytest.raises(ValueError, match="run_kind"):
        _begin_run(ledger, run_kind="not-a-kind")
    assert "observed" not in NON_MATERIALIZING_RESULTS
    assert NON_MATERIALIZING_RESULTS < DECISION_RESULTS


# ---------------------------------------------------------------------------
# Merkle root
# ---------------------------------------------------------------------------


def test_merkle_root_odd_promotion_and_sensitivity():
    a, b, c, d = _sha256("a"), _sha256("b"), _sha256("c"), _sha256("d")
    # Single leaf: the root is the leaf itself.
    assert merkle_root([a]) == a
    # Even: pairwise sha256 over concatenated hex.
    assert merkle_root([a, b]) == _sha256(a + b)
    assert merkle_root([a, b, c, d]) == _sha256(_sha256(a + b) + _sha256(c + d))
    # Odd node is promoted unchanged (duplicate-last is NOT used).
    assert merkle_root([a, b, c]) == _sha256(_sha256(a + b) + c)
    # Any change to a leaf, the order, or the membership changes the root.
    assert merkle_root([a, b, c]) != merkle_root([a, b, d])
    assert merkle_root([a, b, c]) != merkle_root([a, c, b])
    assert merkle_root([a, b, c]) != merkle_root([a, b])
    with pytest.raises(ValueError, match="zero receipts"):
        merkle_root([])
    with pytest.raises(ValueError, match="hex digest"):
        merkle_root(["not-a-hash"])


def test_merkle_root_changes_when_any_receipt_field_changes(tmp_path):
    ledger_a = _ledger(tmp_path, "a.sqlite")
    run_a = _begin_run(ledger_a)
    _emit(ledger_a, run_a, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    _emit(ledger_a, run_a, created_at=datetime(2026, 1, 2, tzinfo=UTC), memory_type="directive")
    root_a = ledger_a.checkpoint(run_a).merkle_root

    ledger_b = _ledger(tmp_path, "b.sqlite")
    run_b = _begin_run(ledger_b)
    _emit(ledger_b, run_b, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    # One field differs on the second receipt → different leaf → different root.
    _emit(ledger_b, run_b, created_at=datetime(2026, 1, 2, tzinfo=UTC), memory_type="preference")
    assert ledger_b.checkpoint(run_b).merkle_root != root_a


# ---------------------------------------------------------------------------
# Chain verification (tamper evidence)
# ---------------------------------------------------------------------------


def test_verify_chain_passes_for_untampered_run(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    for _ in range(4):
        _emit(ledger, run)
    checkpoint = ledger.checkpoint(run)

    verification = ledger.verify_chain(run.run_uuid)
    assert isinstance(verification, ChainVerification)
    assert verification.valid
    assert verification.receipt_count == 4
    assert verification.computed_merkle_root == checkpoint.merkle_root
    assert verification.checkpoint_merkle_root == checkpoint.merkle_root
    assert verification.errors == ()


def test_verify_chain_detects_tampered_persisted_column(tmp_path):
    connection = sqlite3.connect(tmp_path / "receipts.sqlite")
    ledger = ReceiptLedger(connection)
    run = _begin_run(ledger)
    for _ in range(3):
        _emit(ledger, run)
    ledger.checkpoint(run)

    # Tamper a scalar column via raw SQL, leaving canonical_payload untouched.
    connection.execute(
        "UPDATE memory_receipts SET decision_reason = 'tampered' WHERE run_uuid = ? AND event_index = 1",
        (run.run_uuid,),
    )
    connection.commit()

    verification = ledger.verify_chain(run.run_uuid)
    assert not verification.valid
    assert verification.first_divergent_event_index == 1
    assert verification.expected != verification.actual
    assert any("tampered" in error for error in verification.errors)


def test_verify_chain_detects_deleted_rows(tmp_path):
    connection = sqlite3.connect(tmp_path / "receipts.sqlite")
    ledger = ReceiptLedger(connection)
    run = _begin_run(ledger)
    for _ in range(3):
        _emit(ledger, run)
    ledger.checkpoint(run)

    # Delete a middle row → density break at position 1.
    connection.execute(
        "DELETE FROM memory_receipts WHERE run_uuid = ? AND event_index = 1",
        (run.run_uuid,),
    )
    connection.commit()
    verification = ledger.verify_chain(run.run_uuid)
    assert not verification.valid
    assert verification.first_divergent_event_index == 1
    assert any("not dense" in error for error in verification.errors)

    # Delete the LAST row too → the chain prefix is intact, but the checkpoint
    # receipt_count and merkle_root expose the truncation.
    connection.execute(
        "DELETE FROM memory_receipts WHERE run_uuid = ? AND event_index = 2",
        (run.run_uuid,),
    )
    connection.commit()
    verification = ledger.verify_chain(run.run_uuid)
    assert not verification.valid
    assert any("receipt_count" in error for error in verification.errors)
    assert any("merkle_root" in error for error in verification.errors)


def test_verify_chain_unknown_run_is_structured_not_raised(tmp_path):
    ledger = _ledger(tmp_path)
    verification = ledger.verify_chain("no-such-run")
    assert not verification.valid
    assert verification.receipt_count == 0
    assert any("no receipts" in error for error in verification.errors)


# ---------------------------------------------------------------------------
# Checkpoints
# ---------------------------------------------------------------------------


def test_checkpoint_round_trip_and_replay_status(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(
        ledger,
        tenant_id="disney",
        agent_id="concierge",
        motive_version_digest=_sha256("motive"),
        graph_state_hash_before=_sha256("before"),
    )
    receipts = [_emit(ledger, run) for _ in range(2)]
    checkpoint = ledger.checkpoint(run, graph_state_hash_after=_sha256("after"))

    assert isinstance(checkpoint, RunCheckpoint)
    assert checkpoint.run_uuid == run.run_uuid
    assert checkpoint.first_receipt_hash == receipts[0].receipt_hash
    assert checkpoint.last_receipt_hash == receipts[1].receipt_hash
    assert checkpoint.receipt_count == 2
    assert checkpoint.merkle_root == merkle_root([r.receipt_hash for r in receipts])
    assert checkpoint.graph_state_hash_before == _sha256("before")
    assert checkpoint.graph_state_hash_after == _sha256("after")
    assert checkpoint.replay_status == "unverified"

    loaded = ledger.checkpoint_for_run(run.run_uuid)
    assert loaded == checkpoint

    ledger.set_replay_status(run.run_uuid, "verified")
    assert ledger.checkpoint_for_run(run.run_uuid).replay_status == "verified"
    with pytest.raises(ValueError, match="replay status"):
        ledger.set_replay_status(run.run_uuid, "bogus")
    with pytest.raises(ValueError, match="no checkpoint"):
        ledger.set_replay_status("no-such-run", "verified")
    with pytest.raises(RuntimeError, match="already exists"):
        ledger.checkpoint(run)


def test_checkpoint_of_empty_run_is_hard_error(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    with pytest.raises(RuntimeError, match="zero receipts"):
        ledger.checkpoint(run)
    with pytest.raises(ValueError, match="no checkpoint"):
        ledger.checkpoint_for_run(run.run_uuid)


def test_receipts_for_run_round_trip(tmp_path):
    ledger = _ledger(tmp_path)
    run = _begin_run(ledger)
    emitted = [
        _emit(
            ledger,
            run,
            salience_score=0.75,
            salience_threshold=0.3,
            memory_type="preference",
            episode_uuid="ep-1",
        ),
        _emit(
            ledger,
            run,
            decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
            decision_reason="created",
            decision_result="materialized",
            relationship_uuid="rel-1",
        ),
    ]
    loaded = ledger.receipts_for_run(run.run_uuid)
    assert loaded == emitted
    assert ledger.receipts_for_run("no-such-run") == []


# ---------------------------------------------------------------------------
# Negative space ([0027])
# ---------------------------------------------------------------------------


def _seed_negative_space(ledger: ReceiptLedger) -> tuple[ReceiptRun, ReceiptRun]:
    base = datetime(2026, 7, 1, tzinfo=UTC)
    run_alice = _begin_run(ledger, scope_key="user:alice")
    # Materializing + observed receipts must NOT appear in negative space.
    _emit(
        ledger,
        run_alice,
        created_at=base,
        episode_uuid="ep-1",
        decision_type=ReceiptDecisionType.CANDIDATE_EXTRACTED,
        decision_reason="candidate_extracted",
        decision_result="observed",
        candidate_digest=_sha256("candidate-observed"),
    )
    _emit(
        ledger,
        run_alice,
        created_at=base + timedelta(minutes=1),
        episode_uuid="ep-1",
        decision_type=ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED,
        decision_reason="created",
        decision_result="materialized",
        relationship_uuid="rel-1",
    )
    _emit(
        ledger,
        run_alice,
        created_at=base + timedelta(minutes=2),
        episode_uuid="ep-1",
        decision_type=ReceiptDecisionType.FORMATION_MOTIVE_TYPE_FILTERED,
        decision_reason="motive_type_not_allowed",
        decision_result="gated",
        motive_name="support-quality",
        memory_type="directive",
        candidate_digest=_sha256("candidate-gated"),
    )
    _emit(
        ledger,
        run_alice,
        created_at=base + timedelta(minutes=3),
        episode_uuid="ep-2",
        decision_type=ReceiptDecisionType.CANDIDATE_LOW_SALIENCE,
        decision_reason="salience_below_min",
        decision_result="rejected",
        memory_type="preference",
        salience_score=0.1,
        salience_threshold=0.4,
        candidate_digest=_sha256("candidate-low"),
    )
    ledger.checkpoint(run_alice)

    run_bob = _begin_run(ledger, run_kind="pruning", job_name="pruning-default", scope_key="user:bob")
    _emit(
        ledger,
        run_bob,
        created_at=base + timedelta(minutes=4),
        decision_type=ReceiptDecisionType.PRUNING_RELATIONSHIP_PRUNED,
        decision_reason="valid_to_expired",
        decision_result="pruned",
        memory_type="state",
        relationship_uuid="rel-2",
    )
    ledger.checkpoint(run_bob)
    return run_alice, run_bob


def test_negative_space_returns_only_non_materializing_decisions(tmp_path):
    ledger = _ledger(tmp_path)
    _seed_negative_space(ledger)

    entries = ledger.negative_space()
    assert len(entries) == 3
    assert {entry.decision_result for entry in entries} <= NON_MATERIALIZING_RESULTS
    reasons = {entry.decision_reason for entry in entries}
    assert reasons == {"motive_type_not_allowed", "salience_below_min", "valid_to_expired"}
    # Joined to candidate digest, policy digests, and evidence pointer.
    gated = next(e for e in entries if e.decision_result == "gated")
    assert gated.candidate_digest == _sha256("candidate-gated")
    assert gated.effective_policy_digest
    assert gated.episode_uuid == "ep-1"
    assert gated.motive_name == "support-quality"


def test_negative_space_filters(tmp_path):
    ledger = _ledger(tmp_path)
    _seed_negative_space(ledger)
    base = datetime(2026, 7, 1, tzinfo=UTC)

    assert len(ledger.negative_space(scope_key="user:alice")) == 2
    assert len(ledger.negative_space(scope_key="user:bob")) == 1
    assert len(ledger.negative_space(episode_uuid="ep-2")) == 1
    assert len(ledger.negative_space(motive_name="support-quality")) == 1
    assert len(ledger.negative_space(memory_type="preference")) == 1
    assert len(ledger.negative_space(decision_reason="valid_to_expired")) == 1
    assert len(ledger.negative_space(decision_type=ReceiptDecisionType.CANDIDATE_LOW_SALIENCE)) == 1
    assert len(ledger.negative_space(decision_type="pruning_relationship_pruned")) == 1
    # Half-open time window: since inclusive, until exclusive.
    window = ledger.negative_space(since=base + timedelta(minutes=2), until=base + timedelta(minutes=4))
    assert {entry.decision_reason for entry in window} == {
        "motive_type_not_allowed",
        "salience_below_min",
    }
    assert ledger.negative_space(scope_key="user:alice", memory_type="state") == []
    with pytest.raises(ValueError, match="naive datetime"):
        ledger.negative_space(since=datetime(2026, 7, 1))  # noqa: DTZ001
    with pytest.raises(ValueError, match="non-blank"):
        ledger.negative_space(scope_key="  ")


# ---------------------------------------------------------------------------
# Digests
# ---------------------------------------------------------------------------


def test_effective_policy_digest_sensitivity_to_minor_changes():
    base = {
        "tenant_id": "disney",
        "dedup": {"semantic_threshold": 0.88},
        "pruning": {"max_age_days": 90},
        "source_trace": {"dedup": "tenant-layer"},
    }
    minor_change = {
        **base,
        "dedup": {"semantic_threshold": 0.89},
    }
    assert config_effective_policy_digest(base) != config_effective_policy_digest(minor_change)
    # Key insertion order does not matter — the digest is canonical.
    reordered = dict(reversed(list(base.items())))
    assert config_effective_policy_digest(base) == config_effective_policy_digest(reordered)
    with pytest.raises(ValueError, match="dict"):
        config_effective_policy_digest("not-a-dict")

    policy = _Policy(tenant_id="disney", dedup_threshold=0.88, source_trace={"dedup": "tenant"})
    tweaked = _Policy(tenant_id="disney", dedup_threshold=0.89, source_trace={"dedup": "tenant"})
    traced = _Policy(tenant_id="disney", dedup_threshold=0.88, source_trace={"dedup": "override"})
    assert effective_policy_digest(policy) != effective_policy_digest(tweaked)
    # source_trace is included — a change at any layer changes the digest (claim 7).
    assert effective_policy_digest(policy) != effective_policy_digest(traced)
    assert effective_policy_digest(policy) == effective_policy_digest(policy.model_copy())
    with pytest.raises(ValueError, match="BaseModel"):
        effective_policy_digest({"not": "a-model"})


def test_motive_version_and_governance_digests():
    motive = _Motive(name="support-quality", allowed_memory_types=("requirement",))
    inputs = {"tenant_id": "disney", "scope": "user:alice", "dedup": {"threshold": 0.88}}
    digest = motive_version_digest(motive, inputs)
    assert digest == motive_version_digest(motive, dict(inputs))
    assert digest != motive_version_digest(motive, {**inputs, "dedup": {"threshold": 0.89}})
    assert digest != motive_version_digest(_Motive(name="other"), inputs)
    with pytest.raises(ValueError, match="BaseModel"):
        motive_version_digest({"name": "x"}, inputs)

    assert governance_policy_digest(None) is None
    assert governance_policy_digest(motive) == governance_policy_digest(motive)
    assert governance_policy_digest(motive) != governance_policy_digest(_Motive(name="other"))


def test_evidence_and_candidate_digests():
    metadata = {"source": "chat", "channel": "support"}
    digest = evidence_digest("body text", metadata, "user:alice")
    assert digest == evidence_digest("body text", dict(metadata), "user:alice")
    assert digest != evidence_digest("body text!", metadata, "user:alice")
    assert digest != evidence_digest("body text", metadata, "user:bob")
    with pytest.raises(ValueError, match="scope_key"):
        evidence_digest("body", metadata, " ")

    scope = MemoryScope(kind=ScopeKind.USER, scope_id="alice")
    candidate = ExtractedMemory(
        subject="Alice",
        predicate="prefers",
        object="window seat",
        relationship_type="PREFERS",
        confidence=0.9,
        scope=scope,
        source_text="Alice prefers the window seat.",
    )
    digest = candidate_digest(candidate, memory_type="preference", episode_uuid="ep-1")
    assert digest == candidate_digest(candidate, memory_type="preference", episode_uuid="ep-1")
    assert digest != candidate_digest(candidate, memory_type="directive", episode_uuid="ep-1")
    assert digest != candidate_digest(candidate, memory_type="preference", episode_uuid="ep-2")
    changed = candidate.model_copy(update={"confidence": 0.8})
    assert digest != candidate_digest(changed, memory_type="preference", episode_uuid="ep-1")
    with pytest.raises(ValueError, match="ExtractedMemory"):
        candidate_digest({"subject": "x"}, memory_type=None, episode_uuid="ep-1")

    assert payload_digest({"a": 1}) == _sha256(canonicalize_payload({"a": 1}).decode("utf-8"))


# ---------------------------------------------------------------------------
# Sensitive payload (claim 10)
# ---------------------------------------------------------------------------


def test_sensitive_payload_storage_and_hash_stability(tmp_path):
    connection = sqlite3.connect(tmp_path / "receipts.sqlite")
    ledger = ReceiptLedger(connection)
    run = _begin_run(ledger)
    redacted = _emit(
        ledger,
        run,
        decision_type=ReceiptDecisionType.FORMATION_GOVERNANCE_REDACTED,
        decision_reason="pii_redacted",
        decision_result="transformed",
        sensitive_payload="Alice prefers [REDACTED].",
        sensitive_payload_encrypted=False,
    )
    encrypted = _emit(
        ledger,
        run,
        decision_type=ReceiptDecisionType.FORMATION_UNTRUSTED_DIRECTIVE_GATED,
        decision_reason="untrusted_directive",
        decision_result="gated",
        sensitive_payload="bm90LXJlYWwtY2lwaGVydGV4dA==",
        sensitive_payload_encrypted=True,
    )
    ledger.checkpoint(run)

    loaded = ledger.receipts_for_run(run.run_uuid)
    assert loaded[0].sensitive_payload == "Alice prefers [REDACTED]."
    assert loaded[0].sensitive_payload_encrypted is False
    assert loaded[1].sensitive_payload == "bm90LXJlYWwtY2lwaGVydGV4dA=="
    assert loaded[1].sensitive_payload_encrypted is True
    # The stored representation is inside the canonical payload, so the chain
    # verifies without any governance key (claim 10)...
    assert json.loads(redacted.canonical_payload)["sensitive_payload"] == "Alice prefers [REDACTED]."
    assert json.loads(encrypted.canonical_payload)["sensitive_payload_encrypted"] is True
    assert ledger.verify_chain(run.run_uuid).valid
    # ...and negative space returns only that stored representation.
    entries = ledger.negative_space()
    gated = next(e for e in entries if e.decision_result == "gated")
    assert gated.sensitive_payload == "bm90LXJlYWwtY2lwaGVydGV4dA=="
    assert gated.sensitive_payload_encrypted is True
    # Tampering the sensitive payload column breaks verification.
    connection.execute(
        "UPDATE memory_receipts SET sensitive_payload = 'forged' WHERE receipt_uuid = ?",
        (redacted.receipt_uuid,),
    )
    connection.commit()
    verification = ledger.verify_chain(run.run_uuid)
    assert not verification.valid
    assert verification.first_divergent_event_index == 0
