"""Backend registry and configuration-driven construction.

Engines register a builder under a name; :func:`create_storage_backend` turns
:class:`~memotron.storage.settings.StorageSettings` into a live
:class:`~memotron.storage.base.StorageBackend`.  SQLite and Postgres (the
Operational Store) are both registered on import; the Memory Graph engine plugs
in here without any caller changing.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from memotron.crypto import key_manager_from_env
from memotron.storage.base import (
    MemoryGraphStorage,
    OperationalStorage,
    StorageBackend,
)
from memotron.storage.composite import SplitStorageBackend
from memotron.storage.settings import EngineSettings, StorageSettings

# A builder receives the EngineSettings plus construction keywords
# (key_manager, and memory_graph for operational-plane instances in a split
# deployment) and returns the backend instance.
EngineBuilder = Callable[..., Any]

_ENGINES: dict[str, EngineBuilder] = {}

DEFAULT_SQLITE_PATH = ".memotron/graph.sqlite"


def register_storage_engine(name: str, builder: EngineBuilder) -> None:
    """Register (or replace) an engine builder under *name*."""
    normalized = name.strip().lower()
    if not normalized:
        raise ValueError("storage engine name cannot be blank")
    _ENGINES[normalized] = builder


def registered_storage_engines() -> tuple[str, ...]:
    return tuple(sorted(_ENGINES))


def _build_engine(settings: EngineSettings, **kwargs: Any) -> Any:
    builder = _ENGINES.get(settings.engine)
    if builder is None:
        raise ValueError(
            f"unknown storage engine {settings.engine!r}; registered engines: "
            f"{', '.join(registered_storage_engines()) or 'none'} "
            "(the Memory Graph engine registers here when that work lands)"
        )
    return builder(settings, **kwargs)


def _build_sqlite(settings: EngineSettings, **kwargs: Any) -> Any:
    from memotron.storage.sqlite import SQLiteStorageBackend

    path = settings.path if settings.path is not None else DEFAULT_SQLITE_PATH
    return SQLiteStorageBackend(path, **{**settings.options, **kwargs})


def _build_postgres(settings: EngineSettings, **kwargs: Any) -> Any:
    # Imported lazily so the hermetic SQLite path never needs psycopg installed.
    from memotron.storage.postgres import PostgresStorageBackend

    if not settings.url:
        raise ValueError(
            "the postgres engine requires a connection URL; set "
            "EngineSettings(engine='postgres', url=...) — in LATEST this is "
            "assembled from the Vault-injected credentials"
        )
    return PostgresStorageBackend(settings.url, **{**settings.options, **kwargs})


register_storage_engine("sqlite", _build_sqlite)
register_storage_engine("postgres", _build_postgres)


def create_storage_backend(
    settings: StorageSettings | None = None,
    *,
    key_manager: Any | None = None,
) -> StorageBackend:
    """Build the configured backend (default: one SQLite store for everything).

    Single-backend settings return one engine instance serving the whole
    surface.  Split settings build the Memory Graph engine first, then the
    Operational Store engine with its ``memory_graph`` reference (so
    cross-plane operations follow the cross-store rule), and compose them.

    When no *key_manager* is passed, the configured durable KEK is resolved
    from the environment here (#123).  This is deliberately the ONE place that
    resolution happens.  An earlier version taught only ``Memotron()`` to
    read the env, which left ``adoption``/``runtime`` -- the surfaces behind
    ``memotron llm configure`` -- still minting the per-store ``.kek``
    sibling.  Both then addressed the same rows under different keys: the CLI
    reported the tenant configured while the serving process could not decrypt
    it.  Resolving centrally means a call site cannot forget, and when no KEK
    is configured the resolver returns ``None`` and behaviour is unchanged.
    """
    if settings is None:
        settings = StorageSettings()
    if key_manager is None:
        key_manager = key_manager_from_env()
    if not settings.is_split:
        engine = settings.backend or EngineSettings()
        kwargs: dict[str, Any] = {}
        if key_manager is not None:
            kwargs["key_manager"] = key_manager
        backend = _build_engine(engine, **kwargs)
        _require_surface(backend, StorageBackend, engine.engine)
        return backend

    assert settings.memory_graph is not None
    assert settings.operational_store is not None
    graph_kwargs: dict[str, Any] = {}
    operational_kwargs: dict[str, Any] = {}
    if key_manager is not None:
        # BOTH engines, not just the operational one.  `graph_kwargs` was
        # declared and never populated, so a split deployment sealed graph
        # content under a per-replica sibling key while the operational store
        # used the configured KEK -- the same split-brain, one seam over.
        graph_kwargs["key_manager"] = key_manager
        operational_kwargs["key_manager"] = key_manager
    memory_graph = _build_engine(settings.memory_graph, **graph_kwargs)
    _require_surface(memory_graph, MemoryGraphStorage, settings.memory_graph.engine)
    operational = _build_engine(settings.operational_store, memory_graph=memory_graph, **operational_kwargs)
    _require_surface(operational, OperationalStorage, settings.operational_store.engine)
    return SplitStorageBackend(operational=operational, memory_graph=memory_graph)


def open_storage(
    path: str | Path,
    *,
    key_manager: Any | None = None,
) -> StorageBackend:
    """Open the default local backend at *path*.

    Convenience for local tooling that addresses a store by filesystem path
    (the local platform, adoption flows, and the test suite).  Engine choice
    beyond the local default belongs in :class:`StorageSettings`.
    """
    return create_storage_backend(
        StorageSettings(backend=EngineSettings(engine="sqlite", path=str(path))),
        key_manager=key_manager,
    )


def _require_surface(backend: Any, surface: type, engine_name: str) -> None:
    if not isinstance(backend, surface):
        raise TypeError(
            f"storage engine {engine_name!r} built {type(backend).__name__}, "
            f"which does not implement {surface.__name__}"
        )
