"""#251 unit 14: the in-memory receipt sink, its id minter and its digest rule.

Small module, three properties that matter:

* **Insertion order.** The receipt sequence for one ingest-and-dream IS the
  audit -- what the stages did, in the order they did it. Sorting or grouping
  would destroy the only thing that makes the stream readable.
* **Unique ids.** Two decisions sharing one id makes the stream unciteable, so
  a duplicate raises rather than overwriting or silently appending.
* **One digest rule.** ``inputs_digest`` and ``outputs_digest`` are only useful
  if they are comparable across stages, which they are not if four packages each
  write their own ``json.dumps``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memotron.caveman.models import Receipt, ReceiptOp
from memotron.caveman.receipts import (
    InMemoryReceipts,
    canonical_digest,
    new_receipt_id,
    text_digest,
)

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
HEX = set("0123456789abcdef")


def _receipt(
    *,
    receipt_id: str | None = None,
    op: ReceiptOp = ReceiptOp.EXTRACT_ACCEPTED,
    scope: str = "repo:jedai/memotron",
    subject: str = "ep-251-01",
    detail: str = "",
) -> Receipt:
    return Receipt(
        receipt_id=receipt_id or new_receipt_id(),
        op=op,
        ts=NOW,
        scope=scope,
        subject=subject,
        inputs_digest=canonical_digest({"a": 1}),
        outputs_digest=text_digest("answer"),
        detail=detail,
    )


# ------------------------------------------------------------------ the id minter


def test_receipt_ids_are_unique() -> None:
    minted = {new_receipt_id() for _ in range(1000)}
    assert len(minted) == 1000


def test_a_receipt_id_is_lowercase_hex() -> None:
    receipt_id = new_receipt_id()
    assert len(receipt_id) == 32
    assert set(receipt_id) <= HEX


# --------------------------------------------------------------------- digests


def test_both_digests_are_64_hex() -> None:
    assert len(canonical_digest({"a": 1})) == 64
    assert len(text_digest("x")) == 64
    assert set(canonical_digest({"a": 1})) <= HEX
    assert set(text_digest("x")) <= HEX


def test_a_canonical_digest_ignores_key_order() -> None:
    """A function of the payload's VALUES, so two stages assembling the same
    inputs in a different order produce the same digest."""
    assert canonical_digest({"a": 1, "b": 2}) == canonical_digest({"b": 2, "a": 1})


def test_a_canonical_digest_changes_with_a_value() -> None:
    assert canonical_digest({"a": 1}) != canonical_digest({"a": 2})


def test_a_canonical_digest_handles_a_nested_payload() -> None:
    """Prompts nest: system plus user plus a rendered episode."""
    assert canonical_digest({"system": "s", "user": {"turns": [1, 2]}}) == canonical_digest(
        {"user": {"turns": [1, 2]}, "system": "s"}
    )


def test_a_canonical_digest_survives_a_datetime() -> None:
    """`default=str` rather than raising: a receipt payload may legitimately carry a ts."""
    assert len(canonical_digest({"ts": NOW})) == 64


def test_a_text_digest_is_byte_exact() -> None:
    """A model's raw answer is not JSON until parsed, which is why it is digested raw."""
    assert text_digest("a") != text_digest("a ")
    assert text_digest("") == text_digest("")


def test_the_two_digest_helpers_are_not_interchangeable() -> None:
    """The control. Digesting `"x"` as text and as JSON must not collide."""
    assert text_digest("x") != canonical_digest("x")


# ------------------------------------------------------------------- the sink


def test_emit_returns_the_id_it_was_given_unchanged() -> None:
    """The sink does not mint. `LedgerEntry.receipt_id` refers to the caller's id."""
    sink = InMemoryReceipts()
    receipt = _receipt(receipt_id="r-01")
    assert sink.emit(receipt) == "r-01"


def test_all_returns_receipts_in_emission_order() -> None:
    """The order IS the audit."""
    sink = InMemoryReceipts()
    for index in range(5):
        sink.emit(_receipt(receipt_id=f"r-{index}", subject=f"s-{index}"))
    assert [receipt.receipt_id for receipt in sink.all()] == ["r-0", "r-1", "r-2", "r-3", "r-4"]


def test_emission_order_is_kept_even_when_ids_sort_differently() -> None:
    """The control on the order test: ids that sort backwards must still read forwards."""
    sink = InMemoryReceipts()
    for receipt_id in ("r-z", "r-m", "r-a"):
        sink.emit(_receipt(receipt_id=receipt_id))
    assert [receipt.receipt_id for receipt in sink.all()] == ["r-z", "r-m", "r-a"]


def test_all_filters_by_scope() -> None:
    sink = InMemoryReceipts()
    sink.emit(_receipt(receipt_id="r-1", scope="scope-a"))
    sink.emit(_receipt(receipt_id="r-2", scope="scope-b"))
    sink.emit(_receipt(receipt_id="r-3", scope="scope-a"))
    assert [r.receipt_id for r in sink.all(scope="scope-a")] == ["r-1", "r-3"]
    assert [r.receipt_id for r in sink.all(scope="scope-b")] == ["r-2"]


def test_all_with_no_scope_returns_every_scope() -> None:
    sink = InMemoryReceipts()
    sink.emit(_receipt(receipt_id="r-1", scope="scope-a"))
    sink.emit(_receipt(receipt_id="r-2", scope="scope-b"))
    assert len(sink.all()) == 2


def test_all_of_an_unknown_scope_is_empty() -> None:
    sink = InMemoryReceipts()
    sink.emit(_receipt(scope="scope-a"))
    assert sink.all(scope="scope-z") == []


def test_a_fresh_sink_is_empty() -> None:
    assert InMemoryReceipts().all() == []


def test_a_duplicate_id_is_refused() -> None:
    """Two decisions sharing one id makes the stream unciteable."""
    sink = InMemoryReceipts()
    sink.emit(_receipt(receipt_id="r-01"))
    with pytest.raises(ValueError, match="receipt id already emitted: r-01"):
        sink.emit(_receipt(receipt_id="r-01"))


def test_a_refused_duplicate_does_not_land() -> None:
    """A rejected emit must leave the stream exactly as it was."""
    sink = InMemoryReceipts()
    sink.emit(_receipt(receipt_id="r-01", subject="first"))
    with pytest.raises(ValueError, match="already emitted"):
        sink.emit(_receipt(receipt_id="r-01", subject="second"))
    assert len(sink.all()) == 1
    assert sink.all()[0].subject == "first"


def test_all_returns_a_copy_a_caller_cannot_corrupt() -> None:
    """The demo prints this list; a caller mutating it must not edit the audit."""
    sink = InMemoryReceipts()
    sink.emit(_receipt(receipt_id="r-01"))
    snapshot = sink.all()
    snapshot.clear()
    assert len(sink.all()) == 1


def test_two_sinks_do_not_share_state() -> None:
    """The control on `__init__`: class-level mutable state is the classic version of this bug."""
    first, second = InMemoryReceipts(), InMemoryReceipts()
    first.emit(_receipt(receipt_id="r-01"))
    assert second.all() == []


def test_minted_ids_pass_the_sinks_uniqueness_rule() -> None:
    """The minter and the sink have to agree, or every real run raises."""
    sink = InMemoryReceipts()
    for _ in range(200):
        sink.emit(_receipt())
    assert len(sink.all()) == 200
