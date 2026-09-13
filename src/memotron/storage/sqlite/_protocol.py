"""What a concern mixin may assume the composed backend provides.

Why this exists
---------------
The split put each storage concern in its own mixin and, per R-A2, left every
helper used by more than one of them on the composed class rather than
duplicating it. That is the right structure and it is invisible to mypy, which
checks a mixin in isolation and sees ``self._commit`` on a class that does not
define ``_commit``.

The cost was 200 ``attr-defined`` errors across the seven sqlite mixins, silenced
by a blanket ``disable_error_code`` -- which also silenced any REAL attribute
error in 5,700 lines of storage code. This Protocol replaces that blanket with a
declaration, so a typo in a helper name is caught again.

How a mixin uses it
-------------------
    if TYPE_CHECKING:
        _Base = ComposedSQLiteBackend
    else:
        _Base = object

    class GraphPlaneMixin(_Base):
        ...

At runtime the base is plain ``object``, so the MRO of ``SQLiteStorageBackend``
is byte-for-byte what it was -- ``pure_move`` and the API-surface golden both
confirm. Only the type checker sees the Protocol.

Scope
-----
Deliberately SQLite-only. The Postgres mixins have the same problem but a
different surface -- they share ``_engine`` and a pooled ``transaction`` where
SQLite shares ``_connection`` and a single ``sqlite3.Connection`` -- so one
Protocol cannot describe both honestly. The Postgres twin is the obvious
follow-up and is not attempted here.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol


class ComposedSQLiteBackend(Protocol):
    """The composed :class:`SQLiteStorageBackend` surface, as a mixin sees it.

    Two kinds of member: state and helpers that live on the composer, and public
    methods contributed by a *sibling* mixin. Both are legitimate for a mixin to
    call; neither is visible to mypy without this.
    """

    # -- state owned by the composer -----------------------------------------
    _connection: sqlite3.Connection
    _memory_graph: Any
    key_manager: Any
    receipts: Any

    # -- shared helpers, kept on the composer per R-A2 ------------------------
    def _commit(self) -> None: ...
    def _in_clause_sql(self, template: str, values: Iterable[Any]) -> str: ...
    def _json_dumps(self, value: Any, label: str) -> str: ...
    def _normalize_tenant_id(self, tenant_id: str) -> str: ...
    def _normalize_agent_id(self, agent_id: str) -> str: ...
    def _normalize_non_blank(self, value: str, label: str) -> str: ...
    def _normalize_llm_provider(self, provider: str) -> str: ...
    def _datetime_to_text(self, value: datetime | None) -> str | None: ...
    def _optional_datetime_from_text(self, value: Any) -> datetime | None: ...
    def _datetime_from_text(self, value: str) -> datetime: ...
    def transaction(self) -> AbstractContextManager[None]: ...
    def exclusive_write_transaction(self) -> AbstractContextManager[None]: ...

    # -- contributed by a sibling mixin ---------------------------------------
    # Cross-plane calls are real and intended: materialisation reads the epoch
    # ancestry, governance reveals sealed content for the graph, and so on.
    _ENTITY_ALIAS_STATUSES: tuple[str, ...]

    def _active_epoch_ancestry(self, scope_key: str) -> frozenset[str] | None: ...
    def _delete_by_ids(self, table: str, column: str, values: Iterable[str]) -> int: ...
    def reveal(self, scope_key: str, value: Any) -> Any: ...
    def get_governance_key(self, scope_key: str, subject_key: str = ...) -> bytes | None: ...
    def episodes(self) -> list[Any]: ...
