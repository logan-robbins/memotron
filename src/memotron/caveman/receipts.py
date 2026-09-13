"""The in-memory receipt sink, plus the package's one id and digest rule.

Self-contained. Convergence with the WS-11 receipt ledger
(``memotron.storage.receipts``) is a named follow-up, not this issue: the two
share a shape — append-only, episode-keyed, hash-chainable — but not a key
space. WS-11's is a ledger of *decisions* about typed memories; this is a ledger
of decisions about caveman claims and nodes. Folding them now would put this
package behind ``storage/base.py``'s ~60-method ABC and its Postgres parity
gate, which is the coupling this subsystem exists to avoid while there is no
graph database.

The two digest helpers and the id minter live here rather than in each stage
because ``inputs_digest`` and ``outputs_digest`` are only useful if they are
comparable across stages. Four packages each writing their own
``hashlib.sha256(json.dumps(...))`` is four chances to differ on key ordering,
separators or encoding, and the difference would be invisible until someone
tried to match two receipts.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from memotron.caveman.models import Receipt


def new_receipt_id() -> str:
    """A fresh receipt id.

    Random rather than derived from content: two genuinely distinct decisions
    can carry identical content at the same instant — the same node rejected
    twice by the same prompt — and a content-derived id would collide, which
    :meth:`InMemoryReceipts.emit` would then report as a duplicate. The id
    identifies the decision, not what it was about; the digests identify that.
    """
    return uuid.uuid4().hex


def canonical_digest(payload: Any) -> str:
    """sha256 over a JSON-serialisable payload, canonically encoded.

    ``sort_keys`` and compact separators make the digest a function of the
    payload's VALUES, not of dict insertion order, so the same inputs digest
    identically whichever stage assembled them.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def text_digest(text: str) -> str:
    """sha256 over a raw string, byte for byte.

    Separate from :func:`canonical_digest` because a model's raw answer is not
    JSON until it has been parsed — that is the whole point of digesting it. A
    reject receipt's ``outputs_digest`` is this, over the exact text the model
    returned, so an out-of-contract response is identifiable without the receipt
    carrying its content.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class InMemoryReceipts:
    """The canonical :class:`~memotron.caveman.seams.ReceiptSink` for this issue.

    Insertion-ordered, because the receipt sequence for one ingest-and-dream IS
    the audit: what the stages did, in the order they did it. Sorting or
    grouping would destroy the one property that makes the stream readable.

    Ids must be unique. A duplicate is a caller defect — two decisions sharing
    one id makes the stream unciteable — so it raises rather than overwriting or
    silently appending a second row under the same id.
    """

    def __init__(self) -> None:
        self._receipts: list[Receipt] = []
        self._ids: set[str] = set()

    def emit(self, receipt: Receipt) -> str:
        """Record one receipt; returns its id, unchanged."""
        if receipt.receipt_id in self._ids:
            raise ValueError(f"receipt id already emitted: {receipt.receipt_id}")
        self._ids.add(receipt.receipt_id)
        self._receipts.append(receipt)
        return receipt.receipt_id

    def all(self, *, scope: str | None = None) -> list[Receipt]:
        """Every receipt in emission order. ``scope=None`` means every scope."""
        if scope is None:
            return list(self._receipts)
        return [receipt for receipt in self._receipts if receipt.scope == scope]
