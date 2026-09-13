"""Ordering must survive a partially-populated bitemporal window.

Found by ruff DTZ901, not by the suite. `valid_from` and `valid_to` are
``datetime | None`` on every result model, while every stored datetime is
timezone-aware (``models.py`` enforces it).  Three sort comparators fell back to
a *naive* ``datetime.min`` / ``datetime.max`` when the value was absent, and
Python refuses to order a naive datetime against an aware one:

    TypeError: can't compare offset-naive and offset-aware datetimes

The fallback only evaluates when a bound is missing, so a result set where every
row has both bounds sorts fine -- which is why 1011 tests never touched it. The
failure needs one row missing a bound and another having it, in the same set.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memotron.client import _AWARE_MAX, _AWARE_MIN

AWARE = datetime(2026, 1, 1, tzinfo=UTC)


class TestSortSentinelsAreAware:
    def test_sentinels_order_against_an_aware_datetime(self) -> None:
        """The property the comparators depend on, asserted directly."""
        assert _AWARE_MIN < AWARE < _AWARE_MAX

    @pytest.mark.parametrize("sentinel", [_AWARE_MIN, _AWARE_MAX], ids=["min", "max"])
    def test_sentinels_are_timezone_aware(self, sentinel: datetime) -> None:
        assert sentinel.tzinfo is not None, "a naive sentinel is the whole defect"

    def test_a_naive_sentinel_would_still_raise(self) -> None:
        """The control: prove the failure mode is real, so the test above has teeth.

        Without this, a future change back to ``datetime.min`` would be caught
        only by the assertions above, and it would be fair to ask whether they
        were testing anything.  This pins the actual TypeError.
        """
        with pytest.raises(TypeError, match="offset-naive and offset-aware"):
            # DTZ901 is the rule that found the bug; the naive value is the point
            # of this assertion, so the lint is suppressed rather than "fixed".
            sorted([AWARE, datetime.min])  # noqa: DTZ901

    def test_mixed_present_and_absent_bounds_sort(self) -> None:
        """The shape that crashed: one row missing a bound, one row having it."""
        rows: list[tuple[datetime | None, datetime | None, str]] = [
            (None, None, "b"),
            (AWARE, AWARE, "a"),
            (None, AWARE, "c"),
        ]
        ordered = sorted(rows, key=lambda r: (r[0] or _AWARE_MIN, r[1] or _AWARE_MAX, r[2]))
        assert [r[2] for r in ordered] == ["c", "b", "a"]
