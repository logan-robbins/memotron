"""Configuration-driven storage backend selection.

``StorageSettings`` describes which engine serves which store.  The default —
no settings at all — is one SQLite backend serving both stores, which is what
the fast local test suite and the simulation use (no credentials, no network).

The two stores may run on different engines at once: give ``operational_store``
and ``memory_graph`` different :class:`EngineSettings` and the factory composes
one implementation of each surface (see
:func:`memotron.storage.factory.create_storage_backend`).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


class EngineSettings(BaseModel):
    """One storage engine binding.

    engine
        Registered engine name.  ``"sqlite"`` and ``"postgres"``
        (the Operational Store) ship today; the Memory Graph engine registers
        with the same factory when it lands.
    path
        Filesystem path for file-backed engines (SQLite).  ``":memory:"`` is
        honoured.
    url
        Connection DSN for server engines (Postgres, and the Memory Graph
        engine when it lands).  Unused by SQLite.
    options
        Engine-specific keyword options passed to the backend constructor.
    """

    engine: str = "sqlite"
    path: str | None = None
    url: str | None = None
    options: dict[str, Any] = Field(default_factory=dict)

    @field_validator("engine")
    @classmethod
    def normalize_engine(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("storage engine cannot be blank")
        return normalized


class StorageSettings(BaseModel):
    """Which engine serves which store.

    Exactly one of two shapes:

    * **Single backend** — set ``backend`` (or nothing: the default is SQLite
      at the caller-provided path).  One engine instance serves the whole
      :class:`~memotron.storage.base.StorageBackend` surface.
    * **Split deployment** — set both ``operational_store`` and
      ``memory_graph``.  Each store runs its own engine; cross-store
      operations follow the documented rule (anchor in the Operational Store,
      Memory Graph writes safe to repeat).
    """

    backend: EngineSettings | None = None
    operational_store: EngineSettings | None = None
    memory_graph: EngineSettings | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> StorageSettings:
        split = (self.operational_store, self.memory_graph)
        if self.backend is not None and any(item is not None for item in split):
            raise ValueError(
                "storage settings must use either a single 'backend' or the "
                "'operational_store'/'memory_graph' split, not both"
            )
        if any(item is not None for item in split) and not all(item is not None for item in split):
            raise ValueError("a split storage deployment must configure both 'operational_store' and 'memory_graph'")
        return self

    @property
    def is_split(self) -> bool:
        return self.operational_store is not None and self.memory_graph is not None
