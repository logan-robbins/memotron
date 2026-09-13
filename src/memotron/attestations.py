"""DSSE/in-toto attestations for immutable formation contracts."""

from __future__ import annotations

import base64
import json

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from memotron.models import FormationContractAttestation

FORMATION_CONTRACT_PAYLOAD_TYPE = "application/vnd.in-toto+json"
FORMATION_CONTRACT_PREDICATE_TYPE = "https://memotron.dev/FormationContract/v1"


class FormationAttestationUnavailableError(RuntimeError):
    """The store's DSSE signing key exists and this process's KEK cannot unwrap it.

    Lives here rather than beside ``ContentKeyUnavailableError`` in :mod:`memotron.crypto`,
    and the distance is the point. That exception means "content was lawfully erased, degrade
    gracefully" -- ``reveal_content`` answers it with a placeholder and ``seal_receipt_detail``
    with ``None``. A caller that reflexively caught both together would turn "we cannot prove
    the provenance of what we are about to write" into a shrug, which is the silent degradation
    this error exists to prevent.
    """


def _pae(payload_type: str, payload: bytes) -> bytes:
    """DSSE pre-authentication encoding, per the DSSE specification."""
    return (
        b"DSSEv1 "
        + str(len(payload_type)).encode("ascii")
        + b" "
        + payload_type.encode("utf-8")
        + b" "
        + str(len(payload)).encode("ascii")
        + b" "
        + payload
    )


def attest_formation_contract(
    *,
    contract_digest: str,
    certification_verdict: str,
    private_key_bytes: bytes,
    key_id: str,
) -> FormationContractAttestation:
    if not contract_digest or len(contract_digest) != 64:
        raise ValueError("contract_digest must be a sha256 hex digest")
    if not certification_verdict.strip():
        raise ValueError("certification_verdict must be non-blank")
    if not key_id.strip():
        raise ValueError("key_id must be non-blank")
    private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)
    statement = {
        "_type": "https://in-toto.io/Statement/v1",
        "predicateType": FORMATION_CONTRACT_PREDICATE_TYPE,
        "subject": [{"name": "formation-contract", "digest": {"sha256": contract_digest}}],
        "predicate": {"certification_verdict": certification_verdict},
    }
    payload = json.dumps(statement, sort_keys=True, separators=(",", ":")).encode("utf-8")
    public_key = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return FormationContractAttestation(
        payload_type=FORMATION_CONTRACT_PAYLOAD_TYPE,
        payload=base64.b64encode(payload).decode("ascii"),
        key_id=key_id,
        public_key=base64.b64encode(public_key).decode("ascii"),
        signature=base64.b64encode(private_key.sign(_pae(FORMATION_CONTRACT_PAYLOAD_TYPE, payload))).decode("ascii"),
        contract_digest=contract_digest,
        certification_verdict=certification_verdict,
    )


def verify_formation_contract_attestation(attestation: FormationContractAttestation) -> bool:
    try:
        payload = base64.b64decode(attestation.payload, validate=True)
        public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(attestation.public_key, validate=True))
        public_key.verify(
            base64.b64decode(attestation.signature, validate=True),
            _pae(attestation.payload_type, payload),
        )
        statement = json.loads(payload)
        subject = statement["subject"][0]
        return (
            attestation.payload_type == FORMATION_CONTRACT_PAYLOAD_TYPE
            and statement["predicateType"] == FORMATION_CONTRACT_PREDICATE_TYPE
            and subject["digest"]["sha256"] == attestation.contract_digest
            and statement["predicate"]["certification_verdict"] == attestation.certification_verdict
        )
    except (InvalidSignature, KeyError, TypeError, ValueError):
        return False
