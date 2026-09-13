"""WS-12: Content-plane cryptography for verifiable erasure (crypto-shred RTBF).

Implements the erasure substrate for the replay-receipts patent family
(PATENT_REPLAY_RECEIPTS_SPEC.md claim 10 and the implemented erasure proof
claims):

* **Envelope key management** — every crypto-shred scope gets a random
  256-bit data-encryption key (DEK).  The DEK is never persisted raw: it is
  wrapped (encrypted) by a key-encryption key (KEK) held by a
  :class:`KeyManager` and only the wrapped form is stored in the graph's
  ``governance_keys`` table.  Destroying the wrapped DEK is the crypto-shred:
  the KEK cannot regenerate a random DEK, so every sealed field in that scope
  becomes permanently unrecoverable.
* **Sealed content** — AES-256-GCM with a random 96-bit nonce, encoded as a
  self-describing token (``dwsc1$<nonce>$<ciphertext>``).  Sealing is
  non-deterministic; equality over sealed fields is impossible by design.
* **Keyed commitments (blind indexes)** — HMAC-SHA256 tokens
  (``dwcm1$<purpose>$<mac>``) for fields that must support equality matching
  and index seeks over ciphertext (truth keys, node identity keys, dedup
  object equality).  Deterministic under the DEK; unlinkable and
  unverifiable once the DEK is destroyed.
* **Reveal-on-read** — :func:`reveal_content` returns plaintext while the
  DEK lives and :data:`SHREDDED_CONTENT_PLACEHOLDER` after it is destroyed,
  so retrieval surfaces never error on an erased scope.

Production deployments swap :class:`LocalKeyManager` for a cloud KMS
(AWS KMS / GCP CKMS / HashiCorp Vault) implementing the same
:class:`KeyManager` protocol; the DEK/wrapped-DEK contract is unchanged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SHREDDED_CONTENT_PLACEHOLDER = "[CRYPTO-SHREDDED]"

CONTENT_KEY_BYTES = 32
"""AES-256 data-encryption-key length."""

_NONCE_BYTES = 12
"""AES-GCM 96-bit nonce."""

SEALED_PREFIX = "dwsc1$"
"""Self-describing sealed-content token prefix (Memotron sealed content v1)."""

COMMITMENT_PREFIX = "dwcm1$"
"""Self-describing keyed-commitment token prefix (Memotron commitment v1)."""

_COMMITMENT_SEPARATOR = "\x1f"


class ContentKeyUnavailableError(RuntimeError):
    """Raised when an operation requires a live content key that no longer exists."""


def _require_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) != CONTENT_KEY_BYTES:
        raise ValueError(f"content key must be {CONTENT_KEY_BYTES} bytes")
    return key


def seal_content(text: str, key: bytes) -> str:
    """Seal *text* under the scope DEK with AES-256-GCM (random nonce).

    The token is self-describing (``dwsc1$<nonce_b64>$<ct_b64>``) so readers
    can detect sealed fields with :func:`is_sealed_content` and the erasure
    sweep can verify a scope stores only ciphertext.
    """
    if not isinstance(text, str):
        raise ValueError("seal_content requires a string")
    _require_key(key)
    nonce = os.urandom(_NONCE_BYTES)
    ciphertext = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
    return SEALED_PREFIX + base64.b64encode(nonce).decode("ascii") + "$" + base64.b64encode(ciphertext).decode("ascii")


def open_content(token: str, key: bytes) -> str:
    """Open a sealed token with the scope DEK; hard error on tamper or bad key."""
    if not is_sealed_content(token):
        raise ValueError("open_content requires a sealed dwsc1$ token")
    _require_key(key)
    nonce_b64, _, ct_b64 = token[len(SEALED_PREFIX) :].partition("$")
    if not ct_b64:
        raise ValueError("malformed sealed token: missing ciphertext segment")
    nonce = base64.b64decode(nonce_b64)
    ciphertext = base64.b64decode(ct_b64)
    try:
        plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
    except InvalidTag as exc:
        raise ValueError("sealed token failed authentication (wrong key or tampered)") from exc
    return plaintext.decode("utf-8")


def is_sealed_content(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(SEALED_PREFIX)


def reveal_content(value: Any, key: bytes | None) -> Any:
    """Resolve a stored content field for a read surface.

    Plaintext (non-sealed) values pass through unchanged.  Sealed values are
    opened while the DEK lives; after crypto-shred (``key is None``) the
    :data:`SHREDDED_CONTENT_PLACEHOLDER` is returned — reads never recover
    erased content and never error.
    """
    if not is_sealed_content(value):
        return value
    if key is None:
        return SHREDDED_CONTENT_PLACEHOLDER
    return open_content(value, key)


def seal_json(value: Any, key: bytes) -> str:
    """Seal a JSON-serializable value (used for content-derived embedding vectors)."""
    return seal_content(json.dumps(value, separators=(",", ":"), sort_keys=True), key)


def open_json(token: str, key: bytes) -> Any:
    return json.loads(open_content(token, key))


def reveal_json(value: Any, key: bytes | None) -> Any:
    """Resolve a stored JSON content field: plaintext passes through; sealed
    opens under a live DEK; unrecoverable (shredded) resolves to ``None``."""
    if not is_sealed_content(value):
        return value
    if key is None:
        return None
    return open_json(value, key)


def content_commitment(key: bytes, purpose: str, text: str) -> str:
    """Keyed one-way commitment (blind index) for equality matching over ciphertext.

    ``HMAC-SHA256(DEK, purpose || 0x1f || text)``.  Deterministic under the
    DEK so truth-key and node-identity seeks keep working; once the DEK is
    destroyed the commitment can neither be recomputed nor dictionary-tested.
    """
    _require_key(key)
    if not isinstance(purpose, str) or not purpose.strip():
        raise ValueError("commitment purpose must be a non-blank string")
    if not isinstance(text, str):
        raise ValueError("commitment text must be a string")
    material = f"{purpose}{_COMMITMENT_SEPARATOR}{text}".encode()
    mac = hmac.new(key, material, hashlib.sha256).hexdigest()
    return f"{COMMITMENT_PREFIX}{purpose}${mac}"


def is_content_commitment(value: Any) -> bool:
    return isinstance(value, str) and value.startswith(COMMITMENT_PREFIX)


def is_protected_content(value: Any) -> bool:
    """True when a stored field is already key-protected (sealed or committed)."""
    return is_sealed_content(value) or is_content_commitment(value)


# ---------------------------------------------------------------------------
# Envelope key management (KMS pattern)
# ---------------------------------------------------------------------------


@runtime_checkable
class KeyManager(Protocol):
    """KMS-shaped key-encryption-key holder.

    The graph store generates a random per-scope DEK, asks the KeyManager to
    wrap it, and persists only the wrapped form.  A production implementation
    backs :meth:`wrap_dek`/:meth:`unwrap_dek` with AWS KMS, GCP CKMS, or
    HashiCorp Vault; :class:`LocalKeyManager` is the hermetic implementation
    used for local and test deployments.
    """

    def wrap_dek(self, dek: bytes) -> str:
        """Encrypt a DEK under the KEK; returns an opaque wrapped-key token."""
        ...

    def unwrap_dek(self, wrapped: str) -> bytes:
        """Decrypt a wrapped-key token back to the raw DEK.

        **Contract for #77 and any other KMS implementation:** when the key cannot be
        used -- wrong key, tampered token, disabled or deleted CMK, access denied --
        this MUST raise :class:`ValueError` or :class:`ContentKeyUnavailableError`, and
        nothing else. Callers that must distinguish "the key is unavailable" from "the
        database is broken" catch exactly those two (see
        ``storage/*/_governance.py:_llm_credential_is_readable``), so an implementation
        that lets a native ``ClientError`` / ``KmsDisabledException`` / permission error
        escape turns a reportable credential state into a 500. Today this holds only
        because :class:`LocalKeyManager` normalises ``InvalidTag`` to ``ValueError`` --
        which is an accident of one implementation, so it is written down here as a
        requirement before a second implementation exists.
        """
        ...

    def key_id(self) -> str:
        """Stable identifier of the wrapping key (recorded on erasure certificates)."""
        ...


class LocalKeyManager:
    """Hermetic KeyManager holding a 256-bit KEK, wrapped with AES-256-GCM.

    The KEK lives outside the graph database (in memory, or in a 0600 file
    created by :meth:`from_file` next to the SQLite store), so the database
    file alone never contains a usable content key.
    """

    def __init__(self, kek: bytes, *, is_ephemeral: bool = False) -> None:
        if not isinstance(kek, bytes) or len(kek) != CONTENT_KEY_BYTES:
            raise ValueError(f"LocalKeyManager KEK must be {CONTENT_KEY_BYTES} bytes")
        self._kek = kek
        self._key_id = "local:" + hashlib.sha256(kek).hexdigest()[:16]
        self.is_ephemeral = is_ephemeral
        """True only for :meth:`ephemeral`. Lets a diagnostic tell "this KEK dies with the
        process" from "a durable KEK changed" -- two situations that produce the IDENTICAL
        symptom (a new signing identity) and want opposite operator responses. Not on the
        :class:`KeyManager` Protocol, so read it with ``getattr(km, "is_ephemeral", False)``:
        a KMS-backed manager is durable and simply will not carry the attribute."""

    @classmethod
    def from_file(cls, path: str | Path) -> LocalKeyManager:
        """Load (or create, mode 0600) the KEK file for a persistent graph."""
        kek_path = Path(path)
        if kek_path.exists():
            kek = kek_path.read_bytes()
            if len(kek) != CONTENT_KEY_BYTES:
                raise ValueError(
                    f"KEK file {kek_path} is corrupt: expected {CONTENT_KEY_BYTES} bytes, found {len(kek)}"
                )
            return cls(kek)
        kek_path.parent.mkdir(parents=True, exist_ok=True)
        kek = secrets.token_bytes(CONTENT_KEY_BYTES)
        kek_path.touch(mode=0o600, exist_ok=False)
        kek_path.write_bytes(kek)
        return cls(kek)

    @classmethod
    def ephemeral(cls) -> LocalKeyManager:
        """Random in-memory KEK for in-memory graphs (keys die with the process)."""
        return cls(secrets.token_bytes(CONTENT_KEY_BYTES), is_ephemeral=True)

    def wrap_dek(self, dek: bytes) -> str:
        if not isinstance(dek, bytes) or len(dek) != CONTENT_KEY_BYTES:
            raise ValueError(f"DEK must be {CONTENT_KEY_BYTES} bytes")
        nonce = os.urandom(_NONCE_BYTES)
        wrapped = AESGCM(self._kek).encrypt(nonce, dek, b"memotron-dek-v1")
        return base64.b64encode(nonce + wrapped).decode("ascii")

    def unwrap_dek(self, wrapped: str) -> bytes:
        blob = base64.b64decode(wrapped)
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        try:
            dek = AESGCM(self._kek).decrypt(nonce, ciphertext, b"memotron-dek-v1")
        except InvalidTag as exc:
            raise ValueError("wrapped DEK failed authentication (wrong KEK or tampered)") from exc
        if len(dek) != CONTENT_KEY_BYTES:
            raise ValueError("unwrapped DEK has the wrong length")
        return dek

    def key_id(self) -> str:
        return self._key_id


#: Environment variables that supply a DURABLE key-encryption key. (#123)
#:
#: Two forms, because the deployment and the laptop want different things:
#: the base64 variable is what a Vault/VSO-injected Secret produces and is shared by every
#: replica; the file variable suits a mounted secret or a developer keeping one KEK across
#: several local stacks.
KEK_B64_ENV = "MEMOTRON_KEK_B64"
KEK_FILE_ENV = "MEMOTRON_KEK_FILE"


def _looks_like_base64_text(raw: bytes) -> bool:
    """Is this actually base64, or merely the length of some base64?"""
    try:
        base64.b64decode(raw, validate=True)
    except Exception:
        return False
    return True


def key_manager_from_env(env: dict[str, str] | None = None) -> LocalKeyManager | None:
    """Resolve a durable KeyManager from the environment, or ``None`` if none is configured.

    This is the piece #123 was missing. `LocalKeyManager` could already hold a durable KEK;
    nothing ever handed one to the storage backend, so every Postgres store fell to
    :meth:`LocalKeyManager.ephemeral` -- a key that dies with the process.

    **IT REFUSES TO CREATE A KEY, AND THAT IS THE WHOLE POINT.**
    :meth:`LocalKeyManager.from_file` creates the file when absent, which is right for a
    developer's single SQLite store and catastrophic for a Deployment: each replica would
    mint a *different* KEK, seal content under it, and be unable to read its neighbours' --
    the exact failure #123 describes, except now SILENT, because a key manager would be
    present and the fail-closed guard in ``storage/postgres/__init__.py`` would not fire.
    A missing key is an error here, never an invitation to invent one.

    Returns ``None`` when neither variable is set, which leaves the existing guard in charge:
    SQLite keeps working, and Postgres refuses to build unless the operator has explicitly
    accepted an ephemeral KEK. ``None`` therefore means *"no durable key configured"*, not
    *"no key manager"* -- callers must not paper over it.

    **For #77 (KMS key ring):** the contract a cloud implementation must satisfy is the
    :class:`KeyManager` protocol -- ``wrap_dek``/``unwrap_dek``/``key_id``. Substituting one
    means returning it from this function; nothing else in the codebase needs to change,
    because every call site already takes a ``key_manager`` argument. A KMS-backed
    implementation should keep ``key_id`` stable across processes, since erasure certificates
    record it.
    """
    environ = os.environ if env is None else env

    raw_b64 = environ.get(KEK_B64_ENV, "").strip()
    raw_path = environ.get(KEK_FILE_ENV, "").strip()

    # SET-BUT-EMPTY is a misconfiguration, not "unconfigured". A Secret key that does not
    # exist, a `valueFrom` that resolved to nothing, or an unsubstituted template all land
    # here, and treating them as absent is the silent downgrade this function exists to
    # prevent: on SQLite it reverts to the per-replica sibling key with nothing logged.
    # Refusing costs a crash on a broken deployment and saves an unreadable store.
    for name in (KEK_B64_ENV, KEK_FILE_ENV):
        if name in environ and not environ[name].strip():
            raise ValueError(
                f"{name} is set but empty. Leave it UNSET to run without a durable KEK; "
                "an empty value is almost always a Secret that failed to resolve, and "
                "treating it as 'no KEK configured' would silently seal content under a "
                "per-replica key instead."
            )

    if raw_b64 and raw_path:
        raise ValueError(
            f"both {KEK_B64_ENV} and {KEK_FILE_ENV} are set; pass exactly one so it is "
            "unambiguous which key seals content"
        )

    if raw_b64:
        try:
            kek = base64.b64decode(raw_b64, validate=True)
        except Exception as exc:
            raise ValueError(f"{KEK_B64_ENV} is not valid base64: {exc}") from None
        if len(kek) != CONTENT_KEY_BYTES:
            raise ValueError(
                f"{KEK_B64_ENV} decodes to {len(kek)} bytes; a KEK must be exactly "
                f"{CONTENT_KEY_BYTES}. Generate one with: "
                f"python -c 'import os,base64; print(base64.b64encode(os.urandom({CONTENT_KEY_BYTES})).decode())'"
            )
        return LocalKeyManager(kek)

    if raw_path:
        path = Path(raw_path).expanduser()
        if not path.is_file():
            raise ValueError(
                f"{KEK_FILE_ENV}={path} does not exist. This is NOT created for you: in a "
                "multi-replica deployment each pod would create a different key and silently "
                "fail to read the others' sealed content. Mount the same key into every "
                f"replica, or use {KEK_B64_ENV}."
            )
        kek = path.read_bytes()
        if len(kek) != CONTENT_KEY_BYTES:
            # "Corrupt" is usually the wrong diagnosis, so name the two things it actually is
            # nearly always. A Secret mounted as a file carries a trailing newline (33 bytes),
            # and a Secret holding BASE64 text is 44 characters -- both are operator mistakes
            # with obvious fixes, and neither is corruption.
            # Diagnose on CONTENT, not on length alone. Guessing from the byte count is the
            # same mistake this whole change exists to correct, one level down: a 44-byte
            # binary key is not base64 text, and a CRLF-terminated file is the Windows
            # sibling of the newline case and deserves the same answer rather than none.
            stripped = kek.rstrip(b"\r\n")
            hint = ""
            if len(stripped) == CONTENT_KEY_BYTES and len(kek) > CONTENT_KEY_BYTES:
                trailer = kek[CONTENT_KEY_BYTES:]
                hint = (
                    f" -- it is {CONTENT_KEY_BYTES} bytes plus a trailing {trailer!r}, which is what a "
                    "Secret mounted as a file normally contains. Write it without one, or use " + KEK_B64_ENV + "."
                )
            elif (
                _looks_like_base64_text(stripped)
                and len(base64.b64decode(stripped, validate=True)) == CONTENT_KEY_BYTES
            ):
                hint = (
                    " -- this file contains base64 TEXT that decodes to a valid "
                    f"{CONTENT_KEY_BYTES}-byte key. This variable wants the RAW bytes; pass the "
                    "base64 via " + KEK_B64_ENV + " instead."
                )
            raise ValueError(f"KEK file {path} is not a {CONTENT_KEY_BYTES}-byte key: found {len(kek)} bytes{hint}")
        return LocalKeyManager(kek)

    return None
