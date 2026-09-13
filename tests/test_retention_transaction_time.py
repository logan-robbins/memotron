"""Retention measures TRANSACTION time, never VALID time.

Regression cover for a measured defect: ingesting a decision log whose
entries carry historical approval badges (``<Badge text="2025-12-09">``)
produced facts stamped ``valid_from=2025-12-09`` while ``created_at`` was
the actual ingest instant.  ``_last_observed_at`` fell back to
``valid_from``, so the rows read as eight months old the moment they were
written and the 30-day ``state`` staleness window pruned seven of eight
state facts on the FIRST pruning run -- minutes after extraction.

A fact cannot go stale before we have had it.  ``valid_from`` still decides
whether a fact is in effect; it says nothing about how long we have held it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memotron.dreaming import DreamEngine
from memotron.models import GraphRelationship


def _relationship(*, valid_from: datetime | None, created_at: datetime) -> GraphRelationship:
    return GraphRelationship(
        source_uuid="subject-1",
        target_uuid="object-1",
        type="HAS_STATE",
        properties={
            "fact": "integration mirrors production",
            "memory_type": "state",
            "scope_key": "tenant:jedai-portal-kb",
        },
        valid_from=valid_from,
        created_at=created_at,
    )


def test_observed_at_uses_created_at_not_a_historical_valid_from() -> None:
    """The exact shape that pruned live facts: old valid_from, new created_at."""
    ingested_at = datetime(2026, 8, 13, 18, 54, tzinfo=UTC)
    relationship = _relationship(
        valid_from=datetime(2025, 12, 9, tzinfo=UTC),  # the ADR's approval badge
        created_at=ingested_at,
    )

    observed = DreamEngine._last_observed_at(None, relationship)  # type: ignore[arg-type]

    assert observed == ingested_at, (
        "retention must measure from when WE recorded the fact; falling back to "
        "valid_from makes a freshly-ingested historical fact instantly stale"
    )
    # And the concrete consequence: not 30-day-stale on the day it was written.
    assert observed > ingested_at - timedelta(days=30)


def test_observed_at_prefers_last_seen_at_when_reinforced() -> None:
    """A reinforcement resets the clock; last_seen_at is also transaction time."""
    relationship = _relationship(
        valid_from=datetime(2025, 12, 9, tzinfo=UTC),
        created_at=datetime(2026, 8, 13, tzinfo=UTC),
    )
    reinforced_at = datetime(2026, 8, 20, 9, 30, tzinfo=UTC)
    relationship.properties["last_seen_at"] = reinforced_at.isoformat()

    observed = DreamEngine._last_observed_at(None, relationship)  # type: ignore[arg-type]

    assert observed == reinforced_at


def test_a_future_dated_fact_is_not_treated_as_freshly_observed() -> None:
    """valid_from in the future must not make a row look newer than it is.

    The same corpus produced ``valid_from=2027-01-01`` from "99.99% availability
    target timing is CY 2027".  Reading valid_from would have dated that row a
    year into the future; transaction time keeps it honest.
    """
    ingested_at = datetime(2026, 8, 13, 18, 54, tzinfo=UTC)
    relationship = _relationship(
        valid_from=datetime(2027, 1, 1, tzinfo=UTC),
        created_at=ingested_at,
    )

    assert DreamEngine._last_observed_at(None, relationship) == ingested_at  # type: ignore[arg-type]
