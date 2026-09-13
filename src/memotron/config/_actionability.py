"""Actionability screening -- is this candidate a behaviour rule or an inventory fact?

An inventory statement ('the service has three replicas') is true and useless as a
directive; a behaviour rule ('deploys require two approvals') changes what an agent
does. The screens here keep the first class out of the directive plane, and
MissingJustification records WHY a rejection happened so it is auditable rather
than a silent drop."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from memotron.config._predicates import (
    _predicate_tokens,
)

DEFAULT_INVENTORY_PREDICATES: tuple[str, ...] = (
    "has",
    "have",
    "had",
    "contains",
    "contain",
    "includes",
    "include",
    "defines",
    "define",
    "declares",
    "declare",
    "exposes",
    "expose",
    "specifies",
    "specify",
)


DEFAULT_INVENTORY_NOUNS: tuple[str, ...] = (
    "field",
    "fields",
    "subfield",
    "subfields",
    "property",
    "properties",
    "attribute",
    "attributes",
    "column",
    "columns",
    "parameter",
    "parameters",
    "schema",
    "json path",
)


DEFAULT_BEHAVIOUR_MARKERS: tuple[str, ...] = (
    "gate",
    "gates",
    "gated",
    "gating",
    "control",
    "controls",
    "controlled",
    "determine",
    "determines",
    "govern",
    "governs",
    "enable",
    "enables",
    "disable",
    "disables",
    "toggle",
    "toggles",
    "require",
    "requires",
    "required",
    "must",
    "should",
    "check",
    "before",
    "trust",
    "trusted",
    "authorize",
    "authorizes",
    "block",
    "blocks",
    "allow",
    "allows",
    "decide",
    "decides",
    "switch",
    "switches",
)


DEFAULT_EMPTY_JUSTIFICATIONS: tuple[str, ...] = (
    "n/a",
    "na",
    "none",
    "unknown",
    "nothing",
    "useful",
    "it is useful",
    "important",
    "context",
    "background",
    "reference",
    "good to know",
    "for reference",
    "general knowledge",
)


_FIELD_PATH_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")


def _actionability_tokens(value: str) -> tuple[str, ...]:
    """Casefolded, punctuation-stripped word tokens (same shape as predicates)."""
    return _predicate_tokens(value)


def _has_field_path(object_text: str) -> bool:
    """True when the object names a dotted field path (``data.descriptions.x``)."""
    for raw in object_text.split():
        token = raw.strip("\"'`.,;:!?()[]{}")
        if token and _FIELD_PATH_PATTERN.match(token):
            return True
    return False


def inventory_screen_violation(
    *,
    predicate: str,
    object_text: str,
    subject: str = "",
    saves_step: str | None = None,
    inventory_predicates: tuple[str, ...] = DEFAULT_INVENTORY_PREDICATES,
    inventory_nouns: tuple[str, ...] = DEFAULT_INVENTORY_NOUNS,
    behaviour_markers: tuple[str, ...] = DEFAULT_BEHAVIOUR_MARKERS,
) -> str | None:
    """Return why this candidate reads as a field-inventory entry, or ``None``.

    An inventory entry asserts that a named artifact *possesses* a named part.
    It saves an agent nothing: the agent must open the schema anyway, and a
    renamed field turns the memory into an active lie.  Two signals must BOTH
    fire, so a locational or behavioural fact can never be caught:

    1. a **containment predicate** (``has``, ``contains``, ``defines``, ...);
    2. a **data-shape object** — either a field/property/column/schema noun, or
       a dotted field path (``data.descriptions.offer_intro``).

    ``Virtual keys start with sk-`` survives (``start with`` is not containment);
    ``Special Offers content lives at <path>`` survives (``lives at`` is not
    containment); ``Special Offers has field data.descriptions.offer_intro``
    does not.

    The EXCEPTION stands the screen down: a behaviour marker anywhere in the
    fact or in its stated saved step means the field controls what the agent
    does, which is a rule and not an inventory entry.
    """
    predicate_tokens = _actionability_tokens(predicate)
    if not predicate_tokens:
        return None
    if predicate_tokens[0] not in set(inventory_predicates):
        return None
    haystack = " ".join(_actionability_tokens(f"{predicate} {object_text}"))
    noun_hit = next((noun for noun in inventory_nouns if _phrase_in_tokens(haystack, noun)), None)
    if noun_hit is None and not _has_field_path(object_text):
        return None
    marker_haystack = " ".join(_actionability_tokens(f"{subject} {predicate} {object_text} {saves_step or ''}"))
    if any(_phrase_in_tokens(marker_haystack, marker) for marker in behaviour_markers):
        return None
    return (
        f"candidate {predicate!r} / {object_text!r} is a field-inventory entry: it "
        "records what a data shape contains, which saves an agent no step (the "
        "schema must be opened anyway, and a rename makes the memory misleading). "
        "Keep it only when the field CONTROLS behaviour, and say so in the saved step."
    )


def _phrase_in_tokens(token_text: str, phrase: str) -> bool:
    """Whole-token phrase containment inside a space-joined token string."""
    return f" {phrase} " in f" {token_text} "


def actionability_violation(
    *,
    subject: str,
    predicate: str,
    object_text: str,
    saves_step: str | None,
    policy: ActionabilityPolicy,
) -> tuple[str, str] | None:
    """Return ``(violation_code, reason)`` when a candidate fails the gate.

    The gate is two independent checks, and either one quarantines:

    * **justification** — the candidate must state the step an agent skips by
      knowing this.  Blank, boilerplate, too short, or a restatement of the
      fact itself all mean the candidate could not articulate one.
    * **inventory screen** — a field-inventory entry is quarantined even when a
      justification was supplied, because a model will happily invent one
      ("saves opening the schema") for exactly the fact that does not.

    The two codes are returned rather than raised so the caller decides the
    disposition (quarantine vs. reject) from one place.
    """
    if not policy.enabled:
        return None
    stated = (saves_step or "").strip()
    if not stated:
        if policy.missing_justification == MissingJustification.ALLOW:
            pass
        elif policy.missing_justification == MissingJustification.QUARANTINE:
            return (
                "actionability_justification_missing",
                "candidate states no saved step; a memory must say what work an agent avoids by knowing it",
            )
        # SCREEN: fall through to the inventory screen, which judges the fact
        # itself.  A transport that was never asked for a justification (the
        # deterministic rule-based extractor, a promotion replay) is judged on
        # what it stated, not on a field it never saw.
    else:
        reason = _justification_defect(
            stated=stated,
            fact=f"{subject} {predicate} {object_text}",
            policy=policy,
        )
        if reason is not None:
            return ("actionability_justification_not_substantive", reason)
    if policy.inventory_screen:
        inventory_reason = inventory_screen_violation(
            predicate=predicate,
            object_text=object_text,
            subject=subject,
            saves_step=stated or None,
            inventory_predicates=policy.inventory_predicates,
            inventory_nouns=policy.inventory_nouns,
            behaviour_markers=policy.behaviour_markers,
        )
        if inventory_reason is not None:
            return ("actionability_inventory_entry", inventory_reason)
    return None


def _justification_defect(*, stated: str, fact: str, policy: ActionabilityPolicy) -> str | None:
    normalized = " ".join(_actionability_tokens(stated))
    if normalized in {" ".join(_actionability_tokens(p)) for p in policy.empty_justifications}:
        return f"stated saved step {stated!r} is boilerplate: it names no step an agent skips"
    words = _actionability_tokens(stated)
    if len(words) < policy.min_justification_words:
        return (
            f"stated saved step {stated!r} is {len(words)} words; at least "
            f"{policy.min_justification_words} are required to name a step"
        )
    fact_tokens = set(_actionability_tokens(fact))
    stated_tokens = set(words)
    if stated_tokens and fact_tokens:
        overlap = len(stated_tokens & fact_tokens) / len(stated_tokens | fact_tokens)
        if overlap >= policy.max_restatement_overlap:
            return (
                f"stated saved step {stated!r} restates the fact instead of naming "
                f"the step it saves (token overlap {overlap:.2f} >= "
                f"{policy.max_restatement_overlap})"
            )
    if policy.required_markers and not any(_phrase_in_tokens(normalized, marker) for marker in policy.required_markers):
        return f"stated saved step {stated!r} contains none of the required markers {list(policy.required_markers)}"
    return None


class MissingJustification(StrEnum):
    """What to do with a candidate that states no saved step.

    ``SCREEN`` (default) judges the fact by the deterministic inventory screen
    instead.  It is the honest disposition for a transport that never saw the
    contract — the deterministic rule-based extractor, a promotion replay, a
    pre-authored corpus — where an absent field measures nothing about
    actionability.  ``QUARANTINE`` is the strict reading: no stated step, no
    memory.  ``ALLOW`` disables the justification requirement entirely and
    leaves only the inventory screen.
    """

    SCREEN = "screen"
    QUARANTINE = "quarantine"
    ALLOW = "allow"


class ActionabilityPolicy(BaseModel):
    """WS-24: the gate that decides whether a candidate is worth remembering.

    Memory is a shortcut for the agent's NEXT ACTION, not a description of the
    world.  The test for storing a fact is "what step does knowing this let the
    agent skip?", so the extraction contract makes every candidate answer it —
    and a candidate that cannot does not become a memory.

    The measured failure this exists to stop: ingesting a documentation corpus
    produced 148 of 165 facts in a single memory type, all of them field
    inventory, while the genuinely actionable facts (per-environment endpoints,
    key-type requirements) sat at equal priority and never surfaced.
    """

    enabled: bool = True
    """Default-ON.  ``False`` restores byte-identical pre-gate behaviour."""

    missing_justification: MissingJustification = MissingJustification.SCREEN
    """Disposition for a candidate that states no ``saves_step``."""

    inventory_screen: bool = True
    """Run the deterministic field-inventory screen (and its behaviour-control
    exception) even on candidates that DID state a saved step."""

    min_justification_words: int = Field(default=4, ge=1)
    """A saved step is a short sentence, not a word."""

    max_restatement_overlap: float = Field(default=0.85, gt=0.0, le=1.0)
    """Jaccard token overlap with the fact at or above which the stated step is
    a restatement rather than a justification."""

    empty_justifications: tuple[str, ...] = DEFAULT_EMPTY_JUSTIFICATIONS
    required_markers: tuple[str, ...] = ()
    """Optional tightening: phrases a stated step must contain (e.g. ``skip``,
    ``without``, ``instead of``).  Empty (default) requires no marker."""

    inventory_predicates: tuple[str, ...] = DEFAULT_INVENTORY_PREDICATES
    inventory_nouns: tuple[str, ...] = DEFAULT_INVENTORY_NOUNS
    behaviour_markers: tuple[str, ...] = DEFAULT_BEHAVIOUR_MARKERS

    allow_abstain: bool = True
    """Honour an extractor's explicit "none of these / not actionable" outcome.

    ``False`` makes an abstaining candidate a schema rejection instead — the
    contract then insists every candidate pick a type, which is the failure
    mode this whole gate exists to remove.  Provided as a knob, not a
    recommendation."""

    @field_validator(
        "empty_justifications",
        "required_markers",
        "inventory_predicates",
        "inventory_nouns",
        "behaviour_markers",
        mode="before",
    )
    @classmethod
    def normalize_phrase_tuple(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raise ValueError("actionability phrase lists must be a list or tuple, not a string")
        normalized: list[str] = []
        for item in value:
            phrase = " ".join(str(item).casefold().split())
            if not phrase:
                raise ValueError("actionability phrase entries cannot be blank")
            normalized.append(phrase)
        return tuple(dict.fromkeys(normalized))
