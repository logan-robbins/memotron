"""Governance posture: how sensitive a scope's content is, and what erasure means for it.

ErasureBehavior is the consequential one -- CRYPTO_SHRED destroys the scope's DEK,
so content becomes unreadable while the receipt chain still verifies, because
receipts hash the stored representation and never raw text."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from memotron.redaction import RedactionStrategy, compile_custom_patterns


class PiiSensitivity(StrEnum):
    """PII sensitivity level for a memory type or scope."""

    NONE = "none"
    LOW = "low"
    HIGH = "high"


class ErasureBehavior(StrEnum):
    """How memory content is erased under RTBF / operator erasure requests."""

    SOFT_RETIRE = "soft_retire"
    """Mark as PRUNED; content text stays on disk (recoverable via DBA)."""
    CRYPTO_SHRED = "crypto_shred"
    """Destroy the scope's encryption key so ciphertext becomes permanently unrecoverable.

    POC-CRYPTO: In production, use a KMS (AWS KMS, GCP CKMS) to destroy the DEK.
    """


class AuditVerbosity(StrEnum):
    """How much audit trail to emit for memory operations."""

    MINIMAL = "minimal"
    STANDARD = "standard"
    VERBOSE = "verbose"


class GovernancePolicy(BaseModel):
    """WS-7: Per-scope or per-type governance attributes.

    All fields default to the least-restrictive / legacy-behaviour value so that
    constructing a GovernancePolicy with no arguments is a safe no-op.

    Fields
    ------
    retention_ttl_seconds
        When set, memories older than this many seconds (from their valid_from)
        are eligible for pruning. None = no retention TTL (legacy behaviour).
    pii_sensitivity
        ``none`` (default) — no PII redaction.
        ``low``  — flag but do not auto-redact.
        ``high`` — apply Redactor to episode body before extraction.
    erasure_behavior
        ``soft_retire`` (default) — mark as PRUNED; text stays on disk.
        ``crypto_shred`` — destroy encryption key; content becomes unrecoverable.
    audit_verbosity
        Controls how many decisions are emitted to the dream_decisions log.
        ``minimal`` — only hard governance events (RTBF, crypto-shred).
        ``standard`` (default) — same as today's dream-agent decisions.
        ``verbose`` — every governance gate emits a decision record.
    redaction_strategy
        WS-21 T27: how a detected PII span is transformed when
        ``pii_sensitivity == "high"``.  ``redact`` (default, legacy behaviour)
        replaces the span with ``[REDACTED]``; ``mask_last_4`` keeps the last
        four characters; ``sha256_hash`` replaces it with ``[HASH:...]``.
    custom_pii_patterns
        WS-21 T27: operator-supplied named regex patterns (name → regex)
        detected AFTER the builtin email/ssn/cc/phone patterns, sorted by name.
        Each regex is compiled at policy construction — an invalid pattern or a
        blank name fails fast.  Empty (default) = builtin patterns only.
    redaction_allowlist
        WS-21 T27: exact strings that are never redacted even when a pattern
        matches them (final exemption).  Empty (default) = no exemptions.
    """

    retention_ttl_seconds: int | None = None
    pii_sensitivity: PiiSensitivity = PiiSensitivity.NONE
    erasure_behavior: ErasureBehavior = ErasureBehavior.SOFT_RETIRE
    audit_verbosity: AuditVerbosity = AuditVerbosity.STANDARD
    redaction_strategy: RedactionStrategy = RedactionStrategy.REDACT
    custom_pii_patterns: dict[str, str] = Field(default_factory=dict)
    redaction_allowlist: tuple[str, ...] = ()

    @field_validator("custom_pii_patterns")
    @classmethod
    def validate_custom_pii_patterns(cls, value: dict[str, str]) -> dict[str, str]:
        # Fail-fast contract: names non-blank, regexes non-blank and compilable.
        compile_custom_patterns(value)
        return {name.strip(): pattern for name, pattern in value.items()}

    @field_validator("redaction_allowlist", mode="before")
    @classmethod
    def normalize_redaction_allowlist(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise ValueError("redaction_allowlist must be a sequence of strings")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item:
                raise ValueError("redaction_allowlist entries must be non-empty strings")
            normalized.append(item)
        return tuple(dict.fromkeys(normalized))
