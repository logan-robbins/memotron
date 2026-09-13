"""What a Postgres concern mixin may assume the composed backend provides.

The twin of :mod:`memotron.storage.sqlite._protocol`, and separate from it on
purpose: the two backends share a *contract* (``StorageBackend``) but not an
*implementation surface*. SQLite mixins reach for ``_connection`` and a single
``sqlite3.Connection``; these reach for ``_engine`` and a pooled transaction. One
Protocol describing both would have to widen every member to ``Any``, which is the
blanket suppression again, wearing a type annotation.

Why it exists
-------------
Each concern mixin is checked in isolation, so mypy sees ``self.reveal`` on a class
that does not define it. That was silenced by a per-module ``disable_error_code``
across all six mixins -- which also silenced any genuine attribute error in ~7,000
lines of Postgres code.

Notably smaller than the SQLite twin: 4 members rather than 20. The Postgres mixins
were written as a package from the start (commit 9bfc298), so they already keep
most helpers local; the SQLite ones inherited a 5,700-line class's worth of shared
helpers that the split had to leave on the composer.

Usage is the same:

    if TYPE_CHECKING:
        _Base = ComposedPostgresBackend
    else:
        _Base = object

    class GraphPlaneMixin(_Base):
        ...

At runtime the base is plain ``object``, so the MRO of ``PostgresStorageBackend``
is unchanged.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any, Protocol


class ComposedPostgresBackend(Protocol):
    """The composed :class:`PostgresStorageBackend` surface, as a mixin sees it."""

    # -- state owned by the composer -----------------------------------------
    _engine: Any
    _memory_graph: Any
    key_manager: Any
    receipts: Any

    # -- helpers kept on the composer, per R-A2 -------------------------------
    def transaction(self) -> Any: ...
    # @contextmanager: the DECORATED callable returns a context manager, not an
    # Iterator. Typing it Iterator[None] cost 5 errors here -- the same mistake I
    # had already made and fixed in the SQLite twin.
    def exclusive_write_transaction(self) -> AbstractContextManager[None]: ...
    @staticmethod
    def _ordered_scope_keys(scope_keys: Iterable[str]) -> tuple[str, ...]: ...

    # -- contributed by a sibling mixin ---------------------------------------
    # Cross-plane calls are real and intended: the graph plane marks a scope dirty
    # for the epoch plane, and reads sealed content through governance.
    def _active_epoch_ancestry(self, scope_key: str) -> frozenset[str] | None: ...
    def _mark_scope_dirty(self, scope_key: str) -> None: ...
    def reveal(self, scope_key: str, value: Any) -> Any: ...
