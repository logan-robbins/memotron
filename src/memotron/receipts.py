"""Compatibility shim: the receipt ledger moved into the storage backend module.

The canonical, hash-chained, Merkle-committed receipt ledger is part of the
persisted store surface (it rides the Operational Store's transaction domain),
so its implementation lives in ``memotron.storage.receipts``.  This module
re-exports the full public surface so existing ``from memotron.receipts
import ...`` imports keep working.
"""

from __future__ import annotations

from memotron.storage import receipts as _receipts
from memotron.storage.receipts import *  # noqa: F403

# Mirror the module's public names exactly (it defines no __all__), including
# any that a star-import would skip.
__all__ = [name for name in dir(_receipts) if not name.startswith("_")]


def __getattr__(name: str):
    return getattr(_receipts, name)
