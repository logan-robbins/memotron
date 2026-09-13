"""Compat re-export: storage settings are defined in `config` (core tier).

These are pure pydantic models with no storage dependency -- they describe HOW to
configure a backend, not the backend itself -- so `config` owns them and this module
re-exports for the `memotron.storage.settings` import path, which is public.
Moved 2026-09-02 (SZ-2) to remove the one upward tier edge: core `config` was
reaching into infra `storage` at `_dream_config.py:61` and `_storage_env.py:14`.
See refactor/plans/structural-zero.md.
"""

from __future__ import annotations

from memotron.config._storage_settings import EngineSettings, StorageSettings

__all__ = ["EngineSettings", "StorageSettings"]
