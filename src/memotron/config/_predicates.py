"""Predicate shape screening.

Rejects a candidate whose predicate is a sentence rather than a relation. The
canonical predicate is what keys a truth slot, so an unbounded one splits a fact
that should have superseded its predecessor -- this is the cheap syntactic guard
in front of that."""

from __future__ import annotations

DEFAULT_MAX_PREDICATE_WORDS = 4


def _predicate_tokens(value: str) -> tuple[str, ...]:
    """Casefolded, punctuation-stripped word tokens of a predicate/object.

    The same normalisation ``graph.normalize_key`` applies to truth keys (case
    and whitespace folding) plus edge-punctuation stripping, so ``"DynamoDB,"``
    and ``"dynamodb"`` compare equal.
    """
    tokens = []
    for raw in value.casefold().split():
        token = raw.strip("\"'`.,;:!?()[]{}")
        if token:
            tokens.append(token)
    return tuple(tokens)


def _contains_subsequence(haystack: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    if not needle or len(needle) > len(haystack):
        return False
    span = len(needle)
    return any(haystack[start : start + span] == needle for start in range(len(haystack) - span + 1))


def predicate_shape_violation(
    *,
    predicate: str,
    object_text: str,
    max_words: int = DEFAULT_MAX_PREDICATE_WORDS,
) -> str | None:
    """Return why *predicate* is not a bare relation phrase, or ``None``.

    Truth keys are ``scope:subject:predicate`` (``:object`` as well only for
    ``multi_active`` slots), so the predicate IS the truth slot.  An extractor
    that folds the object into the predicate — ``predicate="decided to use
    DynamoDB"``, ``object="DynamoDB"`` instead of ``predicate="decided"``,
    ``object="use DynamoDB for the reservation ledger"`` — silently sends every
    later restatement to a DIFFERENT slot, so supersession, reinforcement, and
    rollup clustering all stop firing with no error anywhere.  The shape check
    is therefore a correctness gate, not style policing.

    Three independent signals, each targeting one shape of the fold, and each
    admitting every predicate the repository's own fixtures, tests, examples and
    benchmark corpora use:

    1. **Word ceiling** — a relation is short.  The longest legitimate predicate
       in this repository is four words (``requires before contract signing``);
       the measured fold was eight (``decided to use DynamoDB for the
       reservation ledger``).  Configurable per instruction set.
    2. **Infinitive continuation** — a ``to`` token anywhere but the last
       position means the predicate ran on into the object's own verb
       (``decided`` **``to use``** ``DynamoDB``).  A *final* ``to`` is a
       preposition the relation genuinely needs and is allowed: the shipped
       benchmark corpus uses ``publishes to``, ``replicates to``, and ``logs
       sessions to``, whose objects are the queue/site/lake that follows.
    3. **Object containment** — the object's words appearing inside a longer
       predicate is the fold by definition.  Only this direction is checked;
       an object that happens to repeat the relation verb is redundant, not
       slot-shattering.

    Deliberately NOT checked: capitalisation (truth keys casefold, so it cannot
    split a slot) and part of speech (``password location`` is a legitimate
    fixture predicate that no verb test would admit).
    """
    tokens = _predicate_tokens(predicate)
    if not tokens:
        return "predicate is empty"
    if len(tokens) > max_words:
        return (
            f"predicate {predicate!r} is {len(tokens)} words; a predicate names the "
            f"relation only and may be at most {max_words} words. Move everything "
            "after the relation verb into object."
        )
    if "to" in tokens[:-1]:
        return (
            f"predicate {predicate!r} continues past 'to' into the object's own verb "
            'phrase. Name the relation alone ("decided", not "decided to use") and '
            "put the rest in object."
        )
    object_tokens = _predicate_tokens(object_text)
    if len(tokens) > len(object_tokens) and _contains_subsequence(tokens, object_tokens):
        return (
            f"predicate {predicate!r} contains the object {object_text!r}. The predicate "
            "is the truth slot and must name the relation only; the object belongs in "
            "the object field."
        )
    return None
