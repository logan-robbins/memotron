"""``as_json`` tolerates a TEXT column holding JSON -- the branch nothing exercised.

Why this file exists
--------------------
``storage/postgres/_common.as_json`` was a ``@staticmethod`` on ``GraphPlaneMixin`` reached from
three other planes; moving it to a module-level function (2026-09-01) took it out of the mixin
call graph. The move surfaced that **the branch its docstring is about had no test**: every
Postgres column it reads is real ``jsonb``, so psycopg hands back a decoded object and the
``isinstance(value, str)`` arm never runs in the suite.

That arm is not dead code. It exists because the SQLite substrate stores the same payloads as
TEXT, and a database migrated from that shape -- or a column added as ``text`` and not yet
altered -- delivers a string. If the arm ever broke, the failure would appear only on a migrated
deployment, which is the worst place to find it.

No database needed: the function is pure, so this is a plain unit test rather than a
``@pytest.mark.postgres`` one that would skip without a DSN.
"""

from __future__ import annotations

import json

import pytest

from memotron.storage.postgres._common import as_json


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param('{"scope": "tenant:acme"}', id="str"),
        pytest.param(b'{"scope": "tenant:acme"}', id="bytes"),
        pytest.param(bytearray(b'{"scope": "tenant:acme"}'), id="bytearray"),
    ],
)
def test_a_text_column_is_decoded(raw: str | bytes | bytearray) -> None:
    """All three types the isinstance check names, not just `str`.

    `bytes` and `bytearray` are in the check because psycopg can return either for a
    `bytea`/`text` column depending on the adapter; testing only `str` would leave two thirds
    of the guard unexercised while looking covered.
    """
    assert as_json(raw) == {"scope": "tenant:acme"}


def test_an_already_decoded_value_passes_straight_through() -> None:
    """The control, and the path that actually runs in production against jsonb.

    Asserts identity, not equality: re-encoding and re-decoding a dict would satisfy `==`
    while doing pointless work on every row read.
    """
    payload = {"scope": "tenant:acme", "nested": [1, 2, 3]}

    assert as_json(payload) is payload


@pytest.mark.parametrize("value", [None, 42, 3.5, True, [1, 2], {"a": 1}])
def test_non_text_values_are_returned_unchanged(value: object) -> None:
    """`None` matters most: a nullable jsonb column reads as None and must not become `"null"`."""
    assert as_json(value) is value


def test_invalid_json_in_a_text_column_raises_rather_than_returning_the_string() -> None:
    """Pins the DIVERGENCE from `_policy._as_json`, which is deliberate and documented.

    `_policy._as_json(value, label)` catches `JSONDecodeError` and re-raises `RuntimeError`
    naming the column, mirroring how the SQLite backend reported corruption. This one is
    lenient and lets the decoder error escape. Unifying them changes what 19 call sites raise
    and needs its own decision -- so the difference is pinned here rather than left as an
    accident someone "fixes" without noticing the call sites.
    """
    with pytest.raises(json.JSONDecodeError):
        as_json("{not json")
