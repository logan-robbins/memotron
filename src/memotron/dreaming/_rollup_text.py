"""Human-readable text for a rollup: its label, its summary, and where they came from.

The ROLLUP_TEXT_SOURCE_* constants are a provenance enum in disguise -- every
resolved rollup text records which path produced it, including the three failure
paths (entailment rejected, transport error, crypto-shredded scope) and the
no-transport fallback. They are load-bearing for audit, not diagnostics: a rollup
whose text came from ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED is not the same claim
as one the LLM actually wrote, and the receipt has to say so."""

from __future__ import annotations

import hashlib
import re
from collections import Counter
from typing import NamedTuple

from memotron.dreaming._text import (
    _ROLLUP_LABEL_STOPWORDS,
    _verbatim_guarded_tokens,
    content_tokens,
)
from memotron.graph import normalize_key

ROLLUP_LABEL_MAX_CHARS = 80


_ROLLUP_LABEL_KEYWORD_COUNT = 4


def _rollup_label_clip(text: str, limit: int = ROLLUP_LABEL_MAX_CHARS) -> str:
    """Collapse whitespace and hard-truncate *text* to *limit* with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(1, limit - 1)].rstrip() + "…"


def _rollup_label_fit(stem: str, tail: str, limit: int = ROLLUP_LABEL_MAX_CHARS) -> str:
    """Join a clippable *stem* to a *tail* that must survive the length cap.

    The tail carries the count ("``: 10 values``"), which is the part that makes
    the label honest about what it is summarizing — truncating it would leave a
    sentence fragment that reads like a fact.
    """
    room = limit - len(tail)
    if room < 8:
        return _rollup_label_clip(f"{stem}{tail}", limit)
    return f"{_rollup_label_clip(stem, room)}{tail}"


def _rollup_label_shared_value(values: list[str]) -> str:
    """The one surface form every member shares, or ``""`` when they differ.

    Comparison is ``normalize_key`` (casefold + whitespace collapse); the value
    returned is the FIRST member's surface form, so the result depends only on
    cluster order, never on set/dict iteration order.
    """
    if not values or any(not value for value in values):
        return ""
    if len({normalize_key(value) for value in values}) != 1:
        return ""
    return values[0]


def _rollup_label_common_prefix(texts: list[str]) -> str:
    """The longest leading word-span every text shares, in the first one's surface form.

    Word granularity, not character: a character prefix can cut a token in half
    and invent a word.  Comparison is ``normalize_key`` per token, so the span
    returned is a verbatim leading substring of every input — which is what lets
    the label extend past ``subject predicate`` into the shared part of the
    objects (``Acme prefers group-a operating preference``) without asserting
    anything the members do not already say, and what keeps sibling clusters
    that differ only inside their objects from collapsing to one label.
    """
    if not texts or any(not text.split() for text in texts):
        return ""
    token_lists = [text.split() for text in texts]
    matched = 0
    shortest = min(len(tokens) for tokens in token_lists)
    while matched < shortest:
        candidate = normalize_key(token_lists[0][matched])
        if any(normalize_key(tokens[matched]) != candidate for tokens in token_lists):
            break
        matched += 1
    return " ".join(token_lists[0][:matched])


def _rollup_label_keywords(texts: list[str], *, count: int = _ROLLUP_LABEL_KEYWORD_COUNT) -> list[str]:
    """Frequency-ranked tokens shared by at least half of *texts*.

    Ties break on the token string itself.  The historical implementation left
    equal-frequency tokens in ``Counter`` insertion order, which derives from
    randomized set iteration — the same three facts produced three different
    labels in three interpreter runs.

    A token carried by exactly one member is not shared, so the floor is two
    members (the historical ``max(1, n // 2)`` admitted every token of a
    three-member cluster, which is how one member's proper nouns ended up in a
    label describing all of them).
    """
    if not texts:
        return []
    token_sets = [set(normalize_key(text).split()) - _ROLLUP_LABEL_STOPWORDS for text in texts]
    threshold = 1 if len(token_sets) == 1 else max(2, len(token_sets) // 2)
    frequency = Counter(token for token_set in token_sets for token in token_set)
    shared = sorted(
        (token for token, seen in frequency.items() if seen >= threshold),
        key=lambda token: (-frequency[token], token),
    )
    return shared[:count]


ROLLUP_TEXT_SOURCE_LLM = "llm"


ROLLUP_TEXT_SOURCE_ENTAILMENT_REJECTED = "fallback_entailment_rejected"


ROLLUP_TEXT_SOURCE_TRANSPORT_ERROR = "fallback_transport_error"


ROLLUP_TEXT_SOURCE_CRYPTO_SHRED_SCOPE = "fallback_crypto_shred_scope"


ROLLUP_TEXT_SOURCE_NO_TRANSPORT = "fallback_no_transport"


class _ResolvedRollupText(NamedTuple):
    """Rollup text plus the provenance of the path that produced it.

    ``rollup_model_identifier`` records the model synthesis was ATTEMPTED under,
    which is not the same question as "did a model write this text?".  Keeping
    only the former made a gate-rejected rollup look model-authored in the admin
    console while it actually carried the deterministic label; ``source``
    answers the second question on the row itself.
    """

    text: str
    prompt_digest: str | None
    source: str


ROLLUP_SUMMARY_MAX_CHARS = 240


ROLLUP_SUMMARY_MAX_SENTENCES = 2


_SENTENCE_SPLIT_PATTERN = re.compile(r"[.!?]+(?=\s|$)")


def rollup_summary_entailed(
    summary: str,
    member_facts: list[str],
    *,
    max_chars: int = ROLLUP_SUMMARY_MAX_CHARS,
    max_sentences: int = ROLLUP_SUMMARY_MAX_SENTENCES,
) -> tuple[bool, str]:
    """WS-18 T19: deterministic entailment gate over an LLM rollup summary.

    Pure function of its inputs.  A summary passes only when:

    1. it is non-blank, at most *max_chars* characters, and at most
       *max_sentences* sentences (sentence-final ``.!?`` followed by
       whitespace/end, so decimals and versioned identifiers never split);
    2. each SENTENCE's content tokens (casefolded, punctuation-stripped,
       non-stopword — :data:`CONTENT_STOPWORDS`) are all covered by ONE member
       fact — no new entities, numbers-as-words, or qualifiers, and no
       recombination across members;
    3. every number- or identifier-carrying raw token appears VERBATIM
       (case-sensitive substring) in at least one member fact — exact
       identifiers are preserved, never paraphrased or re-cased.

    WS-23 M3: rule 2 used to check the token UNION across all members, which
    made the gate blind to inversion by recombination — given "production
    gateway requires mTLS" and "sandbox disables mTLS", the fabricated
    "production gateway disables mTLS" drew every token from the union and
    passed.  Per-sentence, single-member coverage closes that: a claim must be
    traceable to one member fact, which is what "only information stated in the
    member facts" always meant.  A legitimate summary that genuinely spans two
    members must therefore say so in two sentences (one per member), which the
    ``max_sentences`` budget allows.

    Returns ``(True, "")`` on pass, else ``(False, reason)`` where the reason
    names the first failing rule and token — safe for receipt
    ``decision_reason`` fields (a single token, never the full summary).
    """
    normalized = summary.strip()
    if not normalized:
        return False, "summary_blank"
    if len(normalized) > max_chars:
        return False, f"length_exceeded:{len(normalized)}>{max_chars}"
    sentences = [segment for segment in _SENTENCE_SPLIT_PATTERN.split(normalized) if segment.strip()]
    if len(sentences) > max_sentences:
        return False, f"sentence_count_exceeded:{len(sentences)}>{max_sentences}"
    member_token_sets = [content_tokens(fact) for fact in member_facts]
    member_token_union: set[str] = set()
    for tokens in member_token_sets:
        member_token_union |= tokens
    for sentence in sentences:
        sentence_tokens = content_tokens(sentence)
        if not sentence_tokens:
            continue
        unentailed = sorted(sentence_tokens - member_token_union)
        if unentailed:
            return False, f"unentailed_token:{unentailed[0]}"
        if not any(sentence_tokens <= tokens for tokens in member_token_sets):
            # Every token exists somewhere, but no SINGLE member states them
            # together — the sentence is a recombination, not a summary.
            return False, f"recombined_across_members:{sorted(sentence_tokens)[0]}"
    for token in _verbatim_guarded_tokens(normalized):
        if not any(token in fact for fact in member_facts):
            return False, f"identifier_not_verbatim:{token}"
    return True, ""


ROLLUP_SYNTHESIS_SYSTEM_PROMPT = (
    "You compress memory clusters into faithful summaries. Output STRICT JSON "
    '{"summary": string}. The summary must be <= 2 sentences and <= 240 chars, '
    "contain ONLY information stated in the member facts, introduce NO new "
    "entities, numbers, or qualifiers, and preserve exact identifiers verbatim. "
    "EVERY sentence must restate exactly ONE member fact on its own. Never blend "
    "two facts into one sentence, and never carry a detail from one fact into a "
    "sentence about another — a merged sentence is rejected even when every word "
    "in it appears somewhere in the cluster. To cover two facts, write two "
    "sentences, one per fact; otherwise write one sentence for the single most "
    "representative fact."
)


def render_rollup_synthesis_prompt(member_facts: list[str]) -> str:
    """The exact user prompt sent for one rollup synthesis call (numbered facts)."""
    lines = ["Member facts:"]
    lines.extend(f"{index}. {fact}" for index, fact in enumerate(member_facts, start=1))
    return "\n".join(lines)


def rollup_synthesis_prompt_digest(*, system_prompt: str, prompt: str) -> str:
    """sha256 over the exact rendered synthesis prompt (system + user)."""
    return hashlib.sha256(f"{system_prompt}\n\n{prompt}".encode()).hexdigest()
