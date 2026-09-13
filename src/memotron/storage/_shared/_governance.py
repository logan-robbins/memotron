"""Governance-plane behaviour that is defined by the contract, not the engine.

Three rules now, all here for one reason: a rule with two hand-written copies is how #163 and
#164 both happened. ``llm_credential_is_readable`` decides whether a sealed credential can
still be decrypted; ``describe_formation_signer`` decides what a store's DSSE signing key says
about the current KEK. In both cases the engines differ in how they SELECT the row and must not
differ in what the row means, so the lookups stay per-engine and the verdicts live here.

``seal_receipt_detail`` reads the scope DEK through
``get_governance_key`` — which each engine implements against its own store —
and then does AES-GCM in Python. There is nothing left for an engine to do
differently, and both copies agreed on that.

Its post-shred branch is the load-bearing half: it returns ``None`` rather than
raising, because crypto-shred is irreversible and a receipt writer that raised
here would fail the whole write on a scope that has been lawfully erased. Before
this extraction that branch had never executed on Postgres — all four statements
were dark, and ``scripts/verify/parity_coverage.py`` reported the method as
covered on SQLite only. The parity test that closed that finding
(``test_seal_receipt_detail_round_trips_and_returns_none_after_a_shred``) landed
BEFORE the move, so the gap was closed by coverage rather than by the method
leaving the population the gate measures.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from memotron.crypto import ContentKeyUnavailableError, is_sealed_content, open_content, seal_content

if TYPE_CHECKING:
    from memotron.storage._shared._protocol import SharedPlaneBackend

    _Base = SharedPlaneBackend
else:
    _Base = object


class FormationSignerState(StrEnum):
    """Can this process's KEK still produce the store's DSSE formation signer?"""

    FIRST_USE = "first_use"
    """No signing key exists yet. The next attestation mints one. Normal."""
    READY = "ready"
    """The stored key unwrapped. Attestations continue under the same identity."""
    NEW_SIGNER_UNDER_A_DIFFERENT_KEK = "new_signer_under_a_different_kek"
    """No row for THIS signer, but the store holds others: the KEK changed.

    The silent case, and the one that actually happens in production. ``signer_id`` embeds
    ``key_manager.key_id()``, which for :class:`~memotron.crypto.LocalKeyManager` is a hash
    of the KEK -- so a rotated KEK does not fail a lookup, it MISSES one, and the next
    attestation quietly mints a second Ed25519 identity. Nothing errors, nothing links the two
    public keys, and every attestation issued before the rotation now verifies against a key
    this store will never present again. Reported, never fatal: on SQLite with an ephemeral KEK
    this is every test and every laptop run.
    """
    KEK_MISMATCH = "kek_mismatch"
    """The row for this signer EXISTS and will not unwrap. Fatal.

    Requires a key manager whose ``key_id()`` is stable while its material changed -- the shape
    :func:`memotron.crypto.key_manager_from_env` documents for a KMS (#77) -- or a corrupted
    ``wrapped_private_key``. Distinct from the state above precisely because the remedy is
    opposite: there, accept the new identity; here, find the old key.
    """
    STORAGE_FAULT = "storage_fault"
    """The signing-key table could not be read. Says nothing about the KEK."""


@dataclass(frozen=True)
class FormationSignerStatus:
    """A verdict about the signer, carrying no key material of any kind."""

    state: FormationSignerState
    signer_id: str
    other_signer_count: int = 0
    other_signer_latest_created_at: str = ""
    underlying: str = ""

    @property
    def is_fatal(self) -> bool:
        return self.state is FormationSignerState.KEK_MISMATCH


def formation_signer_id(key_manager: object) -> str:
    """The one definition of the DSSE signer id. (#123 follow-up)

    A module-level function rather than a method so the two engines can call it without the
    mixin's Protocol declaring it -- a Protocol *method* makes the member abstract on the
    concrete backend, which is a real mypy error, not a style point.

    **The identity embedded here is why a rotated KEK is silent.** `LocalKeyManager.key_id()`
    is a hash of the KEK, so changing the KEK changes the signer id, which means the lookup
    MISSES rather than fails, and the next attestation mints a brand-new signing identity.
    Anything that changes this string changes which signer a store appears to have.
    """
    return f"formation-contract:{key_manager.key_id()}"  # type: ignore[attr-defined]


def describe_formation_signer(
    *,
    signer_id: str,
    wrapped: str | None,
    unwrap: Callable[[str], bytes],
    other_signers: tuple[tuple[str, str], ...] = (),
    kek_is_ephemeral: bool = False,
) -> FormationSignerStatus:
    """Decide the signer state. Pure: no store, no key material retained. (#123 follow-up)

    Shared for the reason this module exists -- ``llm_credential_is_readable`` above records it:
    two hand-written copies of one rule is how #163 and #164 both happened. The engines differ
    in how they SELECT the rows; they must not differ in what the rows mean. Keeping this
    callable with plain values also means the five states are unit-testable without a database,
    which matters because the Postgres lane skips without a DSN.
    """
    others = len(other_signers)
    latest = max((created for _, created in other_signers), default="")
    if wrapped is None:
        state = FormationSignerState.NEW_SIGNER_UNDER_A_DIFFERENT_KEK if others else FormationSignerState.FIRST_USE
        return FormationSignerStatus(
            state=state,
            signer_id=signer_id,
            other_signer_count=others,
            other_signer_latest_created_at=latest,
            underlying="ephemeral KEK" if kek_is_ephemeral else "",
        )
    try:
        unwrap(wrapped)
    except (ValueError, ContentKeyUnavailableError) as exc:
        # ONLY key failures, matching the `unwrap_dek` contract in memotron.crypto. A
        # storage fault must not be dressed up as a key problem -- it sends the operator to
        # the wrong runbook, and this module already made that mistake once (see the narrowed
        # catch in `llm_credential_is_readable`).
        return FormationSignerStatus(
            state=FormationSignerState.KEK_MISMATCH,
            signer_id=signer_id,
            other_signer_count=others,
            other_signer_latest_created_at=latest,
            underlying=f"{type(exc).__name__}: {exc}",
        )
    return FormationSignerStatus(
        state=FormationSignerState.READY,
        signer_id=signer_id,
        other_signer_count=others,
        other_signer_latest_created_at=latest,
    )


EMBEDDING_PROVIDERS = frozenset({"openai", "litellm", "local"})
"""The embedding-provider vocabulary. Same set both engines validated against -- or would
have, if Postgres had validated at all."""


def resolve_embedding_endpoint(
    *,
    embedding_provider: str | None,
    embedding_base_url: str | None,
    embedding_model: str | None,
    existing: tuple[str, str, str] | None,
) -> tuple[str, str, str]:
    """Resolve the tri-state embedding endpoint for a credential write. (#163)

    ``None`` PRESERVES whatever the tenant already has, a value sets it, and ``""`` clears
    it. Rotating an extraction key must never silently move a populated tenant into a
    different vector space, which is what an unconditional overwrite did.

    Shared because this is a DECISION, not storage. WS-17 T18 implemented it on SQLite and
    Postgres never followed -- SQLite's method grew three parameters past the abstract
    contract while Postgres kept five, so `_migrate_llm_credentials` raised TypeError against
    every Postgres store. Re-implementing the same rule beside the second engine's SQL is
    exactly how that happened, and how #164 happened, and how the credential-status defect
    happened. The SQL stays per-engine; the rule does not.

    *existing* is ``(provider, base_url, model)`` from the current row, or ``None`` when the
    tenant has no credential yet.
    """
    current = existing if existing is not None else ("", "", "")
    provider = current[0] if embedding_provider is None else embedding_provider
    base_url = current[1] if embedding_base_url is None else embedding_base_url
    model = current[2] if embedding_model is None else embedding_model

    normalized_provider = provider.strip().lower()
    if normalized_provider and normalized_provider not in EMBEDDING_PROVIDERS:
        raise ValueError("embedding_provider must be 'openai', 'litellm', or 'local'")
    if normalized_provider in {"openai", "litellm"} and not model.strip():
        raise ValueError("embedding_model cannot be blank for an OpenAI-compatible embedding provider")
    return normalized_provider, base_url.strip(), model.strip()


class SharedGovernancePlaneMixin(_Base):
    """Governance behaviour with no engine-specific part left in it."""

    def seal_receipt_detail(self, scope_key: str, text: str) -> str | None:
        """WS-23 M1: AES-GCM seal one diverted receipt detail, or None post-shred."""
        key = self.get_governance_key(scope_key)
        if key is None:
            return None
        return seal_content(text, key)

    def formation_signing_key_status(self) -> FormationSignerStatus:
        """Diagnose the DSSE formation signer without signing anything.

        A read, deliberately, so a caller can preflight a dream run for the cost of one SELECT
        instead of a throwaway Ed25519 signature -- and so the answer distinguishes the three
        situations an operator needs to tell apart rather than collapsing them into one
        exception.

        Never raises. A status call that can take the surface down is not a status call.
        """
        signer_id = formation_signer_id(self.key_manager)
        try:
            wrapped, other_signers = self._formation_signer_rows(signer_id)
        except Exception as exc:
            return FormationSignerStatus(
                state=FormationSignerState.STORAGE_FAULT,
                signer_id=signer_id,
                underlying=f"{type(exc).__name__}: {exc}",
            )
        return describe_formation_signer(
            signer_id=signer_id,
            wrapped=wrapped,
            unwrap=self.key_manager.unwrap_dek,
            other_signers=other_signers,
            kek_is_ephemeral=bool(getattr(self.key_manager, "is_ephemeral", False)),
        )


def llm_credential_is_readable(sealed: str, key: bytes | None) -> bool:
    """Can this sealed LLM credential actually be decrypted right now? (#123)

    Shared deliberately. Both engines report `has_api_key`, both previously hardcoded it
    ``True`` whenever a row existed, and both would have needed the identical fix -- which is
    precisely how #163 and #164 happened: one engine changed, the other did not, and only the
    one that never runs in production was covered. The key LOOKUP stays per-engine because it
    reaches the store; the decision does not.

    Returns False rather than raising, for every failure mode: a status call must answer
    "is this usable" without becoming another way to take the surface down. The distinction
    the caller needs -- absent versus unreadable -- is made by the caller, which already knows
    whether a row existed.
    """
    if key is None or not sealed:
        return False
    if not is_sealed_content(sealed):
        # A credential column that is NOT sealed is corrupt, not readable. Worth stating
        # because `reveal_content` would call it readable: that function passes plaintext
        # through unchanged and returns a placeholder for shredded content, by design -- it
        # NEVER errors, which makes it exactly the wrong primitive for "can this be
        # decrypted". Using it here reported a garbage string as a working credential; caught
        # by running the helper, not by reading it.
        return False
    try:
        revealed = open_content(sealed, key)
    except Exception:
        # Wrong KEK, tampered ciphertext, or a corrupt row. All three mean the same thing to
        # an operator: this credential cannot be used, and reporting it configured is a lie.
        return False
    return isinstance(revealed, str) and bool(revealed)
