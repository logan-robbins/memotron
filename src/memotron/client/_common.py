"""Timezone-aware sort sentinels.

Two constants, and they exist because of a real crash. Sorting bitemporal windows
with `valid_from or datetime.min` compares a NAIVE sentinel against required-aware
datetimes, which raises the moment one row lacks a bound and another has one --
fixed in fafa26f. These are the aware replacements.

Extracted SECOND rather than last: _retrieval and _views both read them, and a mixin
cannot import them back from the package `__init__` without the circular ImportError
two of this refactor's tools have already produced.

tests/test_bitemporal_sort_sentinels.py imports both from `memotron.client`
directly, so the re-export is load-bearing despite the leading underscore."""

from __future__ import annotations

from datetime import UTC, datetime

# Sort sentinels for the Optional halves of a bitemporal window. Aware, because
# every stored datetime in this system is timezone-aware (models.py enforces it)
# and Python refuses to order an aware value against a naive one.
_AWARE_MIN = datetime.min.replace(tzinfo=UTC)


_AWARE_MAX = datetime.max.replace(tzinfo=UTC)
