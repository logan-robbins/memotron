"""Compatibility shim: persistence moved behind the StorageBackend contract.

``PropertyGraphStore`` was refactored into the SQLite implementation of
:class:`memotron.storage.StorageBackend` and now lives in
``memotron.storage.sqlite``.  Import the interface (and engine selection)
from :mod:`memotron.storage`; this module remains only so existing
``from memotron.graph import PropertyGraphStore, normalize_key`` imports
keep working.
"""

from __future__ import annotations

import memotron.storage.sqlite as _sqlite
from memotron.storage.base import normalize_key
from memotron.storage.sqlite import PropertyGraphStore, SQLiteStorageBackend

__all__ = ["PropertyGraphStore", "SQLiteStorageBackend", "normalize_key"]


def __getattr__(name: str):
    """Every other pre-refactor ``memotron.graph`` name — module constants
    such as ``DREAM_CLAIM_STALE_SECONDS``, ``ScopeStateHashTracker``, and any
    helper — now lives on the SQLite backend module; forward to it so legacy
    ``from memotron.graph import ...`` sites keep resolving."""
    return getattr(_sqlite, name)
