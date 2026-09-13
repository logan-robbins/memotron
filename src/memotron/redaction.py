from __future__ import annotations

import hashlib
import re
from enum import StrEnum

# PII detection patterns
EMAIL_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}",
    re.IGNORECASE,
)
PHONE_PATTERN = re.compile(
    r"""
    (?:
        \+?1[-.\s]?                         # optional country code
    )?
    (?:
        \(?\d{3}\)?                         # area code (with optional parens)
        [-.\s]?                             # separator
    )
    \d{3}                                   # exchange
    [-.\s]?                                 # separator
    \d{4}                                   # subscriber number
    """,
    re.VERBOSE,
)
SSN_PATTERN = re.compile(
    r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b",
)
CC_PATTERN = re.compile(
    r"\b(?:\d{4}[-\s]?){3}\d{4}\b",
)
SECRET_POINTER_PATTERN = re.compile(
    r"^(?:secret|vault|keychain|awssecrets)://[^\s]+$",
    re.IGNORECASE,
)
SECRET_CONTEXT_PATTERN = re.compile(
    r"(?:password|passphrase|api[ _-]?key|access[ _-]?token|auth[ _-]?token|"
    r"secret(?:[ _-]?key)?|credential)",
    re.IGNORECASE,
)
RAW_SECRET_ASSIGNMENT_PATTERN = re.compile(
    r"(?:password|passphrase|api[ _-]?key|access[ _-]?token|auth[ _-]?token|"
    r"secret(?:[ _-]?key)?|credential)\s*(?:is|=|:)\s*([^\s,;]+)",
    re.IGNORECASE,
)
RAW_SECRET_TOKEN_PATTERN = re.compile(
    r"(?:AKIA[0-9A-Z]{16}|sk-[A-Za-z0-9_-]{16,}|"
    r"ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,})",
    re.IGNORECASE,
)
BEARER_SECRET_PATTERN = re.compile(
    r"\bbearer\s+[A-Za-z0-9._~+/=-]{8,}",
    re.IGNORECASE,
)

# Ordered list of (name, pattern) for iteration in redact_text
_PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", EMAIL_PATTERN),
    ("ssn", SSN_PATTERN),
    ("cc", CC_PATTERN),
    ("phone", PHONE_PATTERN),
]


class RedactionStrategy(StrEnum):
    REDACT = "redact"
    MASK_LAST_4 = "mask_last_4"
    SHA256_HASH = "sha256_hash"


def compile_custom_patterns(patterns: dict[str, str]) -> list[tuple[str, re.Pattern[str]]]:
    """WS-21 T27: compile operator-supplied named PII patterns, fail-fast.

    Names must be non-blank; each value must be a non-blank, valid regular
    expression.  Detection order is deterministic: custom patterns run AFTER
    the builtin patterns, sorted by name.
    """
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for name in sorted(patterns):
        normalized_name = name.strip()
        if not normalized_name:
            raise ValueError("custom PII pattern names cannot be blank")
        raw = patterns[name]
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"custom PII pattern {normalized_name!r} cannot be blank")
        try:
            compiled.append((normalized_name, re.compile(raw)))
        except re.error as exc:
            raise ValueError(
                f"custom PII pattern {normalized_name!r} is not a valid regular expression: {exc}"
            ) from exc
    return compiled


class Redactor:
    """Applies configurable PII redaction strategies to plain text.

    Pure stdlib — no external dependencies.

    Strategies
    ----------
    REDACT       Replace the entire matched PII span with ``[REDACTED]``.
    MASK_LAST_4  Keep the last 4 characters of the match; mask the rest with ``*``.
    SHA256_HASH  Replace with ``[HASH:{sha256_hex}]``.

    WS-21 T27 operator configuration
    --------------------------------
    ``custom_patterns`` adds named regexes evaluated AFTER the builtin patterns
    (builtin order first, then custom sorted by name — deterministic detection
    order).  ``allowlist`` is a final exemption: an exactly-matching span is
    never redacted even when a pattern matches it.  Defaults preserve the
    legacy builtin-only REDACT behaviour byte-for-byte.

    Usage
    -----
    redactor = Redactor(RedactionStrategy.REDACT)
    clean = redactor.redact_text("Call me at 555-867-5309 or email bob@example.com")
    # → "Call me at [REDACTED] or email [REDACTED]"
    """

    def __init__(
        self,
        strategy: RedactionStrategy = RedactionStrategy.REDACT,
        *,
        custom_patterns: dict[str, str] | None = None,
        allowlist: tuple[str, ...] | list[str] = (),
    ) -> None:
        self._strategy = strategy
        self._patterns: list[tuple[str, re.Pattern[str]]] = list(_PII_PATTERNS)
        if custom_patterns:
            self._patterns.extend(compile_custom_patterns(custom_patterns))
        self._allowlist = frozenset(allowlist)

    @property
    def strategy(self) -> RedactionStrategy:
        return self._strategy

    def needs_redaction(self, text: str) -> bool:
        """Return True if any non-allowlisted PII span matches in text."""
        return bool(self.matched_pattern_names(text))

    def matched_pattern_names(self, text: str) -> tuple[str, ...]:
        """Names of patterns with at least one non-allowlisted match, in detection order.

        WS-21 T27: receipts record WHICH patterns fired — never the matched
        content — so redaction receipts stay redaction-safe by construction.
        """
        matched: list[str] = []
        for name, pattern in self._patterns:
            if any(match.group(0) not in self._allowlist for match in pattern.finditer(text)):
                matched.append(name)
        return tuple(matched)

    def redact_text(self, text: str) -> str:
        """Apply the configured redaction strategy to all PII spans in text."""
        result = text
        for _, pattern in self._patterns:
            result = pattern.sub(self._replacer, result)
        return result

    def _replacer(self, match: re.Match[str]) -> str:
        matched = match.group(0)
        if matched in self._allowlist:
            return matched
        if self._strategy == RedactionStrategy.REDACT:
            return "[REDACTED]"
        if self._strategy == RedactionStrategy.MASK_LAST_4:
            if len(matched) <= 4:
                return "*" * len(matched)
            return "*" * (len(matched) - 4) + matched[-4:]
        if self._strategy == RedactionStrategy.SHA256_HASH:
            hex_digest = hashlib.sha256(matched.encode()).hexdigest()
            return f"[HASH:{hex_digest}]"
        # Fallback: REDACT
        return "[REDACTED]"


def redact_sensitive_text(text: str) -> str:
    """Redact supported PII and raw credential forms from checkpoint text."""

    redacted = Redactor().redact_text(text)
    redacted = RAW_SECRET_ASSIGNMENT_PATTERN.sub("[REDACTED]", redacted)
    redacted = BEARER_SECRET_PATTERN.sub("Bearer [REDACTED]", redacted)
    return RAW_SECRET_TOKEN_PATTERN.sub("[REDACTED]", redacted)


def contains_raw_credentials(text: str) -> bool:
    """Return whether text contains a supported raw credential form."""

    if RAW_SECRET_TOKEN_PATTERN.search(text) or BEARER_SECRET_PATTERN.search(text):
        return True
    return any(
        SECRET_POINTER_PATTERN.fullmatch(match.group(1)) is None
        for match in RAW_SECRET_ASSIGNMENT_PATTERN.finditer(text)
    )
