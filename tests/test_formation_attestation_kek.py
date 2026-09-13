"""A KEK that changed must not silently fork the attestation chain. (#123 follow-up)

`signer_id` embeds `key_manager.key_id()`, and for `LocalKeyManager` that is a hash of the KEK
itself. So rotating the KEK does not fail a lookup -- it MISSES one, and the next attestation
quietly mints a second Ed25519 identity. Verified by running it before any of this was written:

    ROTATED KEK -> NO CRASH, silently re-keyed
    signer 1 : formation-contract:local:e7abf3239a1f3d14
    signer 2 : formation-contract:local:b6a43cad208b33fe
    same signing identity? False
    signing keys now in table: 2 (was 1)

Every attestation issued before that point verifies against a public key the store will never
present again, and nothing recorded that the two are related. For a provenance claim that is
worse than a crash, and **nothing in the suite noticed** -- which is why this file exists.

The other half, a stored key that will NOT unwrap, needs a key manager whose `key_id()` is
stable while its material changed. `LocalKeyManager` cannot express that (see above), so
`_StableIdKeyManager` below does. That is not a convenience: it is the shape
`memotron.crypto` prescribes for a KMS (#77), and it is the first thing in this repo to
exercise the `unwrap_dek` exception contract at all.
"""

from __future__ import annotations

import logging
import secrets

import pytest

from memotron.attestations import FormationAttestationUnavailableError
from memotron.config import DreamJob
from memotron.crypto import ContentKeyUnavailableError, LocalKeyManager
from memotron.models import DreamJobKind
from memotron.storage._shared._governance import (
    FormationSignerState,
    describe_formation_signer,
    formation_signer_id,
)
from memotron.storage.sqlite import SQLiteStorageBackend

DIGEST = "a" * 64
_FORMATION_JOB = DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1)


class _StableIdKeyManager:
    """A KMS-shaped manager: `key_id()` stays put while the wrapping material changes.

    Exactly what `memotron.crypto` tells a #77 implementation to do -- "A KMS-backed
    implementation should keep ``key_id`` stable across processes, since erasure certificates
    record it" -- and the only way to reach the KEK_MISMATCH branch, because with
    `LocalKeyManager` a changed KEK changes the signer id and mints a new signer instead.
    """

    def __init__(self, kek: bytes, key_id: str = "kms:stable") -> None:
        self._inner = LocalKeyManager(kek)
        self._key_id = key_id

    def wrap_dek(self, dek: bytes) -> str:
        return self._inner.wrap_dek(dek)

    def unwrap_dek(self, wrapped: str) -> bytes:
        return self._inner.unwrap_dek(wrapped)

    def key_id(self) -> str:
        return self._key_id


class TestThePureRule:
    """`describe_formation_signer` engine-free, so the hermetic lane covers the decision.

    The Postgres lane skips without a DSN, and `check.sh` says so in its own words: a green run
    without it "says nothing about the Postgres backend". Both engines route through this
    function, so testing it here covers the verdict on both even when that lane is dark.
    """

    def test_no_row_and_no_others_is_FIRST_USE(self) -> None:
        status = describe_formation_signer(signer_id="s", wrapped=None, unwrap=lambda _: b"")
        assert status.state is FormationSignerState.FIRST_USE
        assert not status.is_fatal

    def test_no_row_but_OTHER_signers_means_the_kek_changed(self) -> None:
        """The silent case. A miss, not an error -- which is the whole problem."""
        status = describe_formation_signer(
            signer_id="s",
            wrapped=None,
            unwrap=lambda _: b"",
            other_signers=(("older", "2026-01-01"), ("newer", "2026-02-02")),
        )
        assert status.state is FormationSignerState.NEW_SIGNER_UNDER_A_DIFFERENT_KEK
        assert status.other_signer_count == 2
        assert status.other_signer_latest_created_at == "2026-02-02"
        assert not status.is_fatal, "a rotation must not stop a laptop or the test suite"

    def test_a_row_that_unwraps_is_READY(self) -> None:
        status = describe_formation_signer(signer_id="s", wrapped="x", unwrap=lambda _: b"k")
        assert status.state is FormationSignerState.READY

    @pytest.mark.parametrize(
        "exc",
        [ValueError("wrapped DEK failed authentication"), ContentKeyUnavailableError("gone")],
        ids=["wrong-kek", "key-unavailable"],
    )
    def test_a_row_that_will_not_unwrap_is_a_FATAL_mismatch(self, exc: Exception) -> None:
        def _boom(_: str) -> bytes:
            raise exc

        status = describe_formation_signer(signer_id="s", wrapped="x", unwrap=_boom)
        assert status.state is FormationSignerState.KEK_MISMATCH
        assert status.is_fatal
        assert type(exc).__name__ in status.underlying

    def test_a_STORAGE_fault_is_NOT_swallowed_as_a_key_problem(self) -> None:
        """Only the two documented key exceptions are caught. A database fault must propagate
        so it is not dressed up as a KEK problem -- that sends the operator to the wrong
        runbook, a mistake this module already made once in `llm_credential_is_readable`."""

        def _db_is_on_fire(_: str) -> bytes:
            raise RuntimeError("connection reset")

        with pytest.raises(RuntimeError, match="connection reset"):
            describe_formation_signer(signer_id="s", wrapped="x", unwrap=_db_is_on_fire)

    def test_the_status_never_carries_key_material(self) -> None:
        secret = "dwsc1$super$secret"  # pragma: allowlist secret
        status = describe_formation_signer(signer_id="s", wrapped=secret, unwrap=lambda _: b"k")
        assert secret not in repr(status)


class TestAgainstARealStore:
    def test_the_same_kek_keeps_one_signing_identity(self, tmp_path) -> None:
        """The control. Without it, a test asserting only the failure cases would pass against
        an implementation that reported every store as broken."""
        kek = secrets.token_bytes(32)
        path = str(tmp_path / "g.db")

        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(kek))
        try:
            first = store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(kek))
        try:
            second = store.attest_formation_contract(contract_digest=DIGEST)
            status = store.formation_signing_key_status()
        finally:
            store.close()

        assert first.key_id == second.key_id
        assert first.public_key == second.public_key, "same KEK must mean the same signer"
        assert status.state is FormationSignerState.READY

    def test_a_rotated_kek_is_REPORTED_rather_than_silent(self, tmp_path) -> None:
        """The defect. It must stay non-fatal, and it must stop being invisible."""
        path = str(tmp_path / "g.db")

        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(secrets.token_bytes(32)))
        try:
            first = store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(secrets.token_bytes(32)))
        try:
            status = store.formation_signing_key_status()
            second = store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        assert status.state is FormationSignerState.NEW_SIGNER_UNDER_A_DIFFERENT_KEK
        assert status.other_signer_count == 1
        assert not status.is_fatal
        assert first.public_key != second.public_key, "this is the fact nothing used to surface"

    def test_a_stored_key_that_will_not_unwrap_is_FATAL(self, tmp_path) -> None:
        """Stable signer id, changed material -- the KMS shape, and the only route here."""
        path = str(tmp_path / "g.db")

        store = SQLiteStorageBackend(path, key_manager=_StableIdKeyManager(secrets.token_bytes(32)))
        try:
            store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        store = SQLiteStorageBackend(path, key_manager=_StableIdKeyManager(secrets.token_bytes(32)))
        try:
            status = store.formation_signing_key_status()
        finally:
            store.close()

        assert status.state is FormationSignerState.KEK_MISMATCH
        assert status.is_fatal

    def test_a_storage_fault_reports_UNKNOWN_and_does_not_accuse_the_kek(self, tmp_path) -> None:
        """A status call must never become another way to take the surface down."""
        store = SQLiteStorageBackend(str(tmp_path / "g.db"), key_manager=LocalKeyManager(secrets.token_bytes(32)))
        store._connection.execute("DROP TABLE formation_contract_signing_keys")
        try:
            status = store.formation_signing_key_status()
        finally:
            store.close()

        assert status.state is FormationSignerState.STORAGE_FAULT
        assert not status.is_fatal, "a database fault is not a KEK verdict"

    def test_both_engines_spell_the_signer_id_the_same_way(self) -> None:
        """They built this string separately before. `#163`/`#164` were both that shape."""
        km = LocalKeyManager(secrets.token_bytes(32))
        assert formation_signer_id(km) == f"formation-contract:{km.key_id()}"


class TestThePreflightRefusesBeforeAnythingIsWritten:
    """Failing mid-run is the expensive outcome: `mark_episode_processed` fires inside the
    per-episode loop, so an episode formed without an attestation is never re-formed and its
    governing contract is unprovable permanently. There is no later repair."""

    def _engine(self, store):
        from memotron.config import (
            DreamConfig,
            DreamInstructionSet,
            NodeInstruction,
            RelationshipInstruction,
        )
        from memotron.dreaming import DreamEngine
        from memotron.extraction import InstructionalExtractor, RuleBasedExtractionTransport

        config = DreamConfig(
            instruction_sets=(
                DreamInstructionSet(
                    name="default",
                    node_instructions=(
                        NodeInstruction(
                            label="Entity",
                            query="Find durable entities worth remembering.",
                            properties=("kind", "role"),
                        ),
                    ),
                    relationship_instructions=(
                        RelationshipInstruction(
                            type="REQUIRES",
                            source_label="Entity",
                            target_label="Entity",
                            query="Remember explicit requirements.",
                            min_confidence=0.8,
                        ),
                    ),
                ),
            ),
            jobs=(_FORMATION_JOB,),
        )
        return DreamEngine(
            graph=store,
            config=config,
            extractor=InstructionalExtractor(transport=RuleBasedExtractionTransport()),
        )

    def test_preflight_raises_on_a_mismatch(self, tmp_path) -> None:
        path = str(tmp_path / "g.db")
        store = SQLiteStorageBackend(path, key_manager=_StableIdKeyManager(secrets.token_bytes(32)))
        try:
            store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        store = SQLiteStorageBackend(path, key_manager=_StableIdKeyManager(secrets.token_bytes(32)))
        try:
            engine = self._engine(store)
            job = _FORMATION_JOB
            with pytest.raises(FormationAttestationUnavailableError) as excinfo:
                engine._preflight_formation_attestation(job=job)
        finally:
            store.close()

        message = str(excinfo.value)
        assert "KEK MISMATCH" in message
        assert "No episodes were processed" in message
        assert "not a repair" in message, "the recovery advice must name its own cost"

    def test_preflight_only_WARNS_on_a_rotation(self, tmp_path, caplog) -> None:
        path = str(tmp_path / "g.db")
        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(secrets.token_bytes(32)))
        try:
            store.attest_formation_contract(contract_digest=DIGEST)
        finally:
            store.close()

        store = SQLiteStorageBackend(path, key_manager=LocalKeyManager(secrets.token_bytes(32)))
        try:
            engine = self._engine(store)
            job = _FORMATION_JOB
            with caplog.at_level(logging.WARNING, logger="memotron.dreaming"):
                engine._preflight_formation_attestation(job=job)
        finally:
            store.close()

        assert "minting a NEW DSSE signing key" in caplog.text
        assert "The KEK changed" in caplog.text

    def test_a_clean_store_preflights_silently(self, tmp_path, caplog) -> None:
        """The control for the warning: first use must not cry wolf."""
        store = SQLiteStorageBackend(str(tmp_path / "g.db"), key_manager=LocalKeyManager(secrets.token_bytes(32)))
        try:
            engine = self._engine(store)
            with caplog.at_level(logging.WARNING, logger="memotron.dreaming"):
                engine._preflight_formation_attestation(job=_FORMATION_JOB)
        finally:
            store.close()

        assert "minting a NEW DSSE signing key" not in caplog.text
