"""Operational-store settings read from the environment.

The only place in this module that touches os.environ. Everything else is a value
an operator passes in; this is the one seam where deployment configuration enters,
which is why it is isolated and why the env var names are constants rather than
string literals at the read site."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

from memotron.config._storage_settings import EngineSettings, StorageSettings

OPERATIONAL_STORE_DSN_ENV = "MEMOTRON_OPERATIONAL_STORE_DSN"


OPERATIONAL_STORE_POOL_MIN_SIZE_ENV = "MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE"


OPERATIONAL_STORE_POOL_MAX_SIZE_ENV = "MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE"


OPERATIONAL_STORE_APPLICATION_NAME_ENV = "MEMOTRON_OPERATIONAL_STORE_APPLICATION_NAME"


DEFAULT_OPERATIONAL_STORE_POOL_MIN_SIZE = 2


DEFAULT_OPERATIONAL_STORE_POOL_MAX_SIZE = 10


def _pool_bound_from_env(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from None


def storage_settings_from_env(env: Mapping[str, str] | None = None) -> StorageSettings | None:
    """Resolve storage settings from the environment, or None for the default.

    Returns ``StorageSettings`` bound to the ``postgres`` engine when
    :data:`OPERATIONAL_STORE_DSN_ENV` is set to a non-blank DSN, carrying the
    per-replica pool bounds and the ``application_name`` that makes a replica
    attributable in ``pg_stat_activity``.  Returns None when the variable is
    absent or blank, which leaves every caller on the default single SQLite
    backend (no credentials, no network) — the local substrate and the
    hermetic test path are unchanged.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    dsn = environ.get(OPERATIONAL_STORE_DSN_ENV, "").strip()
    if not dsn:
        return None
    options: dict[str, Any] = {
        "min_size": _pool_bound_from_env(
            environ,
            OPERATIONAL_STORE_POOL_MIN_SIZE_ENV,
            DEFAULT_OPERATIONAL_STORE_POOL_MIN_SIZE,
        ),
        "max_size": _pool_bound_from_env(
            environ,
            OPERATIONAL_STORE_POOL_MAX_SIZE_ENV,
            DEFAULT_OPERATIONAL_STORE_POOL_MAX_SIZE,
        ),
    }
    application_name = environ.get(OPERATIONAL_STORE_APPLICATION_NAME_ENV, "").strip()
    if application_name:
        options["application_name"] = application_name
    return StorageSettings(backend=EngineSettings(engine="postgres", url=dsn, options=options))
