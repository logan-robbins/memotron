"""The storage backend module: every byte Memotron persists goes through here.

Public surface:

* :class:`StorageBackend` / :class:`MemoryGraphStorage` /
  :class:`OperationalStorage` — the contract (see ``base.py`` for the full
  documented contract: ownership, transactions, the context-visible index,
  and the cross-store rule).
* :func:`create_storage_backend` / :func:`open_storage` /
  :func:`register_storage_engine` — configuration-driven engine selection.
* :class:`SQLiteStorageBackend` — the SQLite engine (the refactored
  ``PropertyGraphStore``); the only place in the tree that speaks SQLite,
  together with the receipt ledger it embeds.

No code outside ``memotron.storage`` may import ``sqlite3`` or reference
engine specifics; callers depend on :class:`StorageBackend` only.
"""

from __future__ import annotations

from memotron.storage.base import (
    MemoryGraphStorage,
    OperationalStorage,
    StorageBackend,
    normalize_key,
)
from memotron.storage.composite import SplitStorageBackend
from memotron.storage.factory import (
    create_storage_backend,
    open_storage,
    register_storage_engine,
    registered_storage_engines,
)
from memotron.storage.settings import EngineSettings, StorageSettings
from memotron.storage.sqlite import SQLiteStorageBackend

__all__ = [
    "EngineSettings",
    "MemoryGraphStorage",
    "OperationalStorage",
    "SQLiteStorageBackend",
    "SplitStorageBackend",
    "StorageBackend",
    "StorageSettings",
    "create_storage_backend",
    "normalize_key",
    "open_storage",
    "register_storage_engine",
    "registered_storage_engines",
]
