"""WS-24: memory-health measurement and the gates that BLOCK on it.

A memory graph whose types have collapsed into one bucket does not fail loudly.
It answers every question, ranks every result, reports every count — and quietly
returns the wrong facts, because one over-populated type holds the context every
other type needed.  The measured instance: a documentation corpus produced 148
of 165 facts in a single memory type, all field inventory, while the genuinely
actionable facts (per-environment endpoints, key-type requirements) were
extracted correctly and then buried at equal priority.

This module is pure: four measurements over a type distribution plus the
threshold comparison that turns them into warnings and blocks.  It takes counts
and a policy and returns a report — no store, no clock, no I/O — so the
formation path and the read-side evolution proof compute health the same way,
from one definition.
"""

from __future__ import annotations

import math
from datetime import datetime

from memotron.config import MemoryHealthPolicy
from memotron.models import (
    MemoryHealthGateTrip,
    MemoryHealthReport,
    MemoryScope,
    MemoryType,
)

DEFAULT_ELIGIBLE_TYPES: tuple[str, ...] = tuple(member.value for member in MemoryType)
"""The full type vocabulary — the denominator of normalized entropy and the
population that ``dead_types`` is drawn from, unless a Motive narrows it."""


def normalized_type_entropy(counts: dict[str, int], *, eligible: int) -> float:
    """Shannon entropy of the type distribution divided by ``ln(eligible)``.

    Normalizing by the size of the ELIGIBLE vocabulary, not the observed one, is
    what makes the number comparable and honest: a graph holding exactly one
    type would otherwise divide by ``ln 1 = 0`` and, under an observed-K
    normalization, score a perfect 1.0 for total collapse.  With an eligible
    vocabulary of 8, one type scores 0.0 and a uniform spread scores 1.0.

    Returns 1.0 (no signal, no gate) when the vocabulary has fewer than two
    members or nothing has been observed.
    """
    total = sum(counts.values())
    if total <= 0 or eligible < 2:
        return 1.0
    entropy = 0.0
    for value in counts.values():
        if value <= 0:
            continue
        share = value / total
        entropy -= share * math.log(share)
    return entropy / math.log(eligible)


def compute_memory_health(
    *,
    scope: MemoryScope,
    per_type_counts: dict[str, int],
    policy: MemoryHealthPolicy,
    evaluated_at: datetime,
    quarantined_count: int = 0,
    admitted_count: int = 0,
    eligible_types: tuple[str, ...] = DEFAULT_ELIGIBLE_TYPES,
    entity_label_counts: dict[str, int] | None = None,
) -> MemoryHealthReport:
    """Measure one scope's type distribution and apply every configured gate.

    ``per_type_counts`` is the context-visible active population — the facts
    that actually compete for an agent's context.  Demoted members, superseded
    lineage, and quarantined candidates are deliberately excluded: they make no
    budget claim, so they cannot cause the failure being measured.

    ``quarantined_count`` / ``admitted_count`` are the abstain-rate numerator
    and the rest of its denominator.  In a formation run they are that run's own
    candidates; on the evolution proof they are the scope's lifetime totals.
    Both are stated on the report so the denominator is never ambiguous.

    ``entity_label_counts`` (WS-27 T4) is a SEPARATE population from
    ``per_type_counts``: node counts by entity LABEL rather than relationship
    counts by ``memory_type``.  ``None``/empty (the default) is a complete
    no-op for the label-share gate — existing callers get a byte-identical
    report.
    """
    counts = {key: value for key, value in per_type_counts.items() if value > 0}
    total = sum(counts.values())
    eligible = tuple(dict.fromkeys(eligible_types)) or DEFAULT_ELIGIBLE_TYPES

    dominant_type: str | None = None
    max_share = 0.0
    if total > 0:
        dominant_type, dominant_count = max(counts.items(), key=lambda item: (item[1], item[0]))
        max_share = dominant_count / total
    entropy = normalized_type_entropy(counts, eligible=len(eligible))

    evaluated_candidates = quarantined_count + admitted_count
    abstain_rate = quarantined_count / evaluated_candidates if evaluated_candidates else 0.0

    dead: list[str] = []
    if total >= policy.dead_type_min_instances:
        dead = [name for name in eligible if counts.get(name, 0) == 0]

    # WS-27 T4: the Concept/general-label catch-all guardrail.  A SEPARATE
    # measurement over a SEPARATE population (nodes-by-label, not
    # relationships-by-memory_type) — computed here so it shares this
    # function's one ``MemoryHealthReport``/receipt path rather than
    # inventing a second report type callers have to know to also check.
    label_counts = {key: value for key, value in (entity_label_counts or {}).items() if value > 0}
    label_total = sum(label_counts.values())
    general_label_share = 0.0
    general_labels_observed: list[str] = []
    if label_total > 0:
        general_label_count = sum(value for key, value in label_counts.items() if key in policy.general_labels)
        general_label_share = general_label_count / label_total
        general_labels_observed = sorted(key for key in label_counts if key in policy.general_labels)
    label_gates_evaluated = policy.enabled and label_total >= policy.min_instances

    trips: list[MemoryHealthGateTrip] = []
    gates_evaluated = policy.enabled and total >= policy.min_instances
    if gates_evaluated:
        if policy.max_type_share_block is not None and max_share > policy.max_type_share_block:
            trips.append(
                MemoryHealthGateTrip(
                    gate="max_type_share",
                    severity="block",
                    observed=max_share,
                    threshold=policy.max_type_share_block,
                    detail=(f"memory type {dominant_type!r} holds {max_share:.1%} of {total} context-visible facts"),
                )
            )
        elif policy.max_type_share_warn is not None and max_share > policy.max_type_share_warn:
            trips.append(
                MemoryHealthGateTrip(
                    gate="max_type_share",
                    severity="warn",
                    observed=max_share,
                    threshold=policy.max_type_share_warn,
                    detail=(f"memory type {dominant_type!r} holds {max_share:.1%} of {total} context-visible facts"),
                )
            )
        if policy.min_normalized_entropy_block is not None and entropy < policy.min_normalized_entropy_block:
            trips.append(
                MemoryHealthGateTrip(
                    gate="normalized_type_entropy",
                    severity="block",
                    observed=entropy,
                    threshold=policy.min_normalized_entropy_block,
                    detail=(
                        f"type distribution over {len(eligible)} eligible types has collapsed (H/lnK={entropy:.3f})"
                    ),
                )
            )
        elif policy.min_normalized_entropy_warn is not None and entropy < policy.min_normalized_entropy_warn:
            trips.append(
                MemoryHealthGateTrip(
                    gate="normalized_type_entropy",
                    severity="warn",
                    observed=entropy,
                    threshold=policy.min_normalized_entropy_warn,
                    detail=(
                        f"type distribution over {len(eligible)} eligible types is concentrating (H/lnK={entropy:.3f})"
                    ),
                )
            )
        # A zero abstain rate is the signature of a gate acting as a sink: every
        # candidate found a home, which is exactly what a catch-all type does.
        if policy.warn_on_zero_abstain_rate and evaluated_candidates > 0 and abstain_rate == 0.0:
            trips.append(
                MemoryHealthGateTrip(
                    gate="abstain_rate",
                    severity="warn",
                    observed=0.0,
                    threshold=None,
                    detail=(
                        f"all {evaluated_candidates} evaluated candidates were admitted; "
                        "an abstain rate of exactly zero means the gate is a sink"
                    ),
                )
            )
        if dead:
            trips.append(
                MemoryHealthGateTrip(
                    gate="dead_types",
                    severity="block" if policy.dead_types_block else "warn",
                    observed=float(len(dead)),
                    threshold=None,
                    detail=(f"{len(dead)} eligible memory types have zero instances at N={total}: {', '.join(dead)}"),
                )
            )
    if label_gates_evaluated:
        general_labels_text = ", ".join(sorted(policy.general_labels)) or "(none configured)"
        if (
            policy.max_general_label_share_block is not None
            and general_label_share > policy.max_general_label_share_block
        ):
            trips.append(
                MemoryHealthGateTrip(
                    gate="general_label_share",
                    severity="block",
                    observed=general_label_share,
                    threshold=policy.max_general_label_share_block,
                    detail=(
                        f"general/catch-all label(s) [{general_labels_text}] hold "
                        f"{general_label_share:.1%} of {label_total} context-visible entities"
                    ),
                )
            )
        elif (
            policy.max_general_label_share_warn is not None
            and general_label_share > policy.max_general_label_share_warn
        ):
            trips.append(
                MemoryHealthGateTrip(
                    gate="general_label_share",
                    severity="warn",
                    observed=general_label_share,
                    threshold=policy.max_general_label_share_warn,
                    detail=(
                        f"general/catch-all label(s) [{general_labels_text}] hold "
                        f"{general_label_share:.1%} of {label_total} context-visible entities"
                    ),
                )
            )

    return MemoryHealthReport(
        scope=scope,
        evaluated_at=evaluated_at,
        total_instances=total,
        per_type_counts=dict(sorted(counts.items())),
        eligible_types=list(eligible),
        max_type_share=max_share,
        dominant_type=dominant_type,
        normalized_type_entropy=entropy,
        abstain_rate=abstain_rate,
        quarantined_count=quarantined_count,
        admitted_count=admitted_count,
        dead_types=dead,
        gates_evaluated=gates_evaluated,
        trips=trips,
        entity_label_counts=dict(sorted(label_counts.items())),
        general_label_share=general_label_share,
        general_labels_observed=general_labels_observed,
        label_gates_evaluated=label_gates_evaluated,
    )


__all__ = [
    "DEFAULT_ELIGIBLE_TYPES",
    "compute_memory_health",
    "normalized_type_entropy",
]
