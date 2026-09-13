"""Tokenisation shared by entity resolution, contradiction detection and rollups.

Two token planes that must not be confused: identifier tokens (used to decide
whether two mentions name the same entity) and content tokens (used to compare what
two claims SAY). They have different stopword sets and different patterns, and
swapping them silently degrades matching rather than raising."""

from __future__ import annotations

import re

_IDENTIFIER_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_\-]+")


def _is_identifier_token(token: str) -> bool:
    """WS-17 T17b/T16b: is one raw (case-preserved) token identifier-like?

    A token qualifies when it is >= 8 characters and either embeds a digit or
    is >= 3 segments joined by ``_``/``-`` (coded IDs, slugs, versioned names),
    OR when it is an all-caps alphanumeric of >= 6 characters (acronym-style
    identifiers).  Case matters for the all-caps test, so detection runs on the
    raw token before any casefold.
    """
    if len(token) >= 8:
        if any(character.isdigit() for character in token):
            return True
        if len([segment for segment in re.split(r"[_\-]", token) if segment]) >= 3:
            return True
    return len(token) >= 6 and token.isalnum() and token.isupper() and any(character.isalpha() for character in token)


def identifier_tokens(text: str) -> frozenset[str]:
    """Casefolded identifier-like tokens of *text* (empty set when none).

    Shared by the T17b dedup guard and the T16b entity-link hard block: two
    texts that BOTH carry identifier tokens with DIFFERING sets name distinct
    coded things (the measured ``kb_ds_..._wdw_...`` vs ``..._dlr_...`` pair
    reaches cosine 0.895 under the hermetic transport — similarity can never
    be trusted across differing identifiers).
    """
    return frozenset(
        token.casefold() for token in _IDENTIFIER_TOKEN_PATTERN.findall(text) if _is_identifier_token(token)
    )


def identifier_tokens_conflict(first: str, second: str) -> bool:
    """True when BOTH texts carry identifier tokens and the sets differ.

    One-sided identifiers do not conflict (a paraphrase may drop the code); an
    identical identifier set is corroboration, not conflict.
    """
    first_tokens = identifier_tokens(first)
    second_tokens = identifier_tokens(second)
    return bool(first_tokens) and bool(second_tokens) and first_tokens != second_tokens


_ROLLUP_LABEL_STOPWORDS = frozenset(
    {"the", "a", "an", "and", "or", "in", "of", "to", "for", "is", "are", "was", "were"}
)


CONTENT_STOPWORDS = _ROLLUP_LABEL_STOPWORDS | frozenset(
    {
        "all",
        "also",
        "as",
        "at",
        "be",
        "been",
        "being",
        "both",
        "but",
        "by",
        "each",
        "from",
        "had",
        "has",
        "have",
        "it",
        "its",
        "no",
        "not",
        "on",
        "that",
        "their",
        "these",
        "this",
        "those",
        "when",
        "which",
        "while",
        "will",
        "with",
    }
)


_CONTENT_TOKEN_PATTERN = re.compile(r"[0-9a-z]+")


_VERBATIM_TOKEN_PATTERN = re.compile(r"[\w][\w.\-/]*")


def content_tokens(text: str) -> frozenset[str]:
    """Casefolded, punctuation-stripped, stopword-free content tokens of *text*."""
    return frozenset(_CONTENT_TOKEN_PATTERN.findall(text.casefold())) - CONTENT_STOPWORDS


def _verbatim_guarded_tokens(text: str) -> tuple[str, ...]:
    """Raw tokens of *text* that must be preserved verbatim: any token carrying
    a digit, plus every identifier-like token (T17b detector)."""
    guarded: list[str] = []
    for raw in _VERBATIM_TOKEN_PATTERN.findall(text):
        token = raw.rstrip(".,;:!?")
        if not token:
            continue
        if any(character.isdigit() for character in token) or _is_identifier_token(token):
            guarded.append(token)
    return tuple(guarded)
