"""What an ENGINE-AGNOSTIC plane may assume the composed backend provides.

The third Protocol in the storage tree, and the one that had to justify itself
hardest. :mod:`memotron.storage.sqlite._protocol` and
:mod:`memotron.storage.postgres._protocol` both open by explaining why one
Protocol cannot describe both backends: SQLite mixins reach for ``_connection``
and a ``sqlite3.Connection``, Postgres mixins reach for ``_engine`` and a pooled
transaction, and a Protocol spanning the two would have to widen every member to
``Any`` -- "the blanket suppression again, wearing a type annotation".

That argument is correct about the FULL surface and does not apply here, because
this Protocol deliberately describes a much smaller one. Every member below is
engine-agnostic by construction: not one of them is ``_connection``, ``_engine``,
or anything typed in terms of either. The planes that use it were selected on
exactly that criterion -- their bodies are already byte-identical on both
engines, which is only possible because every call they make is to a member whose
contract, not whose implementation, they depend on.

So the honest reading is: the two engine Protocols describe an IMPLEMENTATION
surface and cannot be merged; this one describes the slice of the ``StorageBackend``
CONTRACT that a shared plane needs, and merging is the whole point.

If a member ever has to be added here that mentions an engine type, that is the
signal that the plane calling it does not belong in ``_shared`` -- put it back on
both engines rather than widening this to ``Any``.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any, Protocol

from memotron.models import (
    GraphNode,
    GraphRelationship,
    OutcomeEvent,
    UseEvent,
)


class Row(Protocol):
    """A result row addressed by column name.

    ``sqlite3.Row`` and ``dict[str, Any]`` are the two concrete types, and they
    have no common supertype: ``sqlite3.Row`` is not a ``Mapping`` (no ``.get``,
    not registered), so ``Mapping[str, Any]`` would reject it and ``Any`` would
    check nothing. Both DO support ``row["column"]``, which is the entire
    interface the row decoders in :mod:`._rows` use -- so that is what this says.

    The decoders were annotated ``sqlite3.Row`` on one engine and
    ``dict[str, Any]`` on the other for bodies that were already identical; this
    is the type that was implied by both and written by neither.
    """

    def __getitem__(self, key: str, /) -> Any: ...


class SharedPlaneBackend(Protocol):
    """The composed backend surface an engine-agnostic plane may call.

    Eleven members, every one of them part of the ``StorageBackend`` contract or a
    helper both engines implement independently. Read the module docstring before
    adding a twelfth.
    """

    # -- state owned by the composer, identical in kind on both engines --------
    _memory_graph: Any
    receipts: Any
    key_manager: Any

    # -- the exclusive-write seam ---------------------------------------------
    # THE reason `claim_episodes` and `claim_scope_work` can be shared at all.
    # SQLite implements this as a `BEGIN IMMEDIATE` whole-database lock and
    # Postgres as a `pg_advisory_xact_lock`; the callers below neither know nor
    # can tell, which is what makes one implementation honest rather than a
    # coincidence. tests/test_exclusive_write_conformance.py is what holds that.
    def exclusive_write_transaction(self) -> AbstractContextManager[None]: ...

    # -- operational plane ------------------------------------------------------
    def is_episode_processed(self, episode_uuid: str, *, consumer_key: str | None = None) -> bool: ...
    def _claim_is_live(self, claim_key: str, *, run_uuid: str, cutoff: datetime) -> bool: ...
    def _write_claim(self, claim_key: str, *, run_uuid: str, now: datetime) -> None: ...
    def use_events(
        self,
        *,
        scope_key: str | None = None,
        relationship_uuid: str | None = None,
        task_run_id: str | None = None,
    ) -> list[UseEvent]: ...
    def outcome_events(
        self,
        *,
        scope_key: str | None = None,
        use_id: str | None = None,
        task_run_id: str | None = None,
    ) -> list[OutcomeEvent]: ...

    # -- governance plane -------------------------------------------------------
    def get_governance_key(self, scope_key: str, subject_key: str = ...) -> bytes | None: ...

    # Two SELECTs, no decision: the current signer's wrapped key (None when absent) and
    # (signer_id, created_at) for every OTHER signer the store holds. Engine-agnostic by the
    # module's own test -- neither mentions `_connection`, `_engine`, or a row type. The
    # verdict lives in `_shared/_governance.describe_formation_signer`, on purpose.
    def _formation_signer_rows(self, signer_id: str) -> tuple[str | None, tuple[tuple[str, str], ...]]: ...

    # -- graph plane ------------------------------------------------------------
    def nodes(self) -> list[GraphNode]: ...
    def relationships(self) -> list[GraphRelationship]: ...
