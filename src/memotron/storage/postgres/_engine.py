"""Connection, transaction, and migration machinery for the Operational Store.

Why an async pool behind a synchronous surface
----------------------------------------------
``StorageBackend`` is a synchronous contract with ~110 methods, and DW-002 keeps
the hermetic SQLite substrate in parity behind that same contract.  Converting
the interface to ``async def`` would rewrite every call site in ``dreaming.py``
and ``client.py`` and would strand the offline proof gate, so the interface stays
synchronous.

The pool underneath is ``psycopg_pool.AsyncConnectionPool`` running on one
dedicated event-loop thread owned by this engine.  Every interface call marshals
its coroutine onto that loop with ``run_coroutine_threadsafe`` and blocks for the
result.  The pool is therefore genuinely asynchronous — it multiplexes waiters,
opens connections concurrently, and enforces its own sizing — while callers keep
a blocking API.  See DW-020.

Transactions
------------
:meth:`PostgresEngine.transaction` is re-entrant per calling thread.  The
outermost entry acquires one pooled connection and issues ``BEGIN``; nested
entries join it; the outermost exit issues a single ``COMMIT`` (or ``ROLLBACK``).
Pool connections run with ``autocommit=True`` so transaction boundaries are
explicit and can span many separate coroutine submissions — that is what lets a
whole maintenance run, or a cross-store operation, commit exactly once.

Each calling thread gets its own connection and its own transaction, so replicas
and worker threads never share a transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Statement timeout applied to every pooled connection.  A wedged statement must
# not hold a pool slot forever; the maintenance plane retries.
DEFAULT_STATEMENT_TIMEOUT_MS = 30_000

# How long a synchronous caller waits for its coroutine to finish on the loop
# thread.  Deliberately larger than the statement timeout so the database error
# surfaces as a database error rather than as a bridge timeout.
DEFAULT_CALL_TIMEOUT_SECONDS = 60.0

# Deferred commit-time work may register further work; this bounds the settling
# loop so a mistake becomes a loud error instead of a hang.
_MAX_FLUSH_ROUNDS = 8


class PostgresEngineError(RuntimeError):
    """Raised when the engine itself (loop thread, pool, migrations) fails."""


class PostgresEngine:
    """Owns the event-loop thread, the async pool, and the migration runner.

    Parameters
    ----------
    dsn:
        libpq connection string for the Operational Store.
    min_size / max_size:
        Pool sizing.  ``max_size`` is this replica's share of the instance
        connection budget; see ``docs/operational-store-sizing.md``.
    application_name:
        Surfaces in ``pg_stat_activity`` so a misbehaving replica is
        attributable.
    """

    def __init__(
        self,
        dsn: str,
        *,
        min_size: int = 2,
        max_size: int = 10,
        application_name: str = "memotron",
        statement_timeout_ms: int = DEFAULT_STATEMENT_TIMEOUT_MS,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        connect_timeout_seconds: float = 10.0,
    ) -> None:
        if not dsn or not dsn.strip():
            raise ValueError("Postgres DSN cannot be blank")
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("invalid pool sizing: require 0 <= min_size <= max_size, max_size >= 1")

        self._dsn = dsn
        self._min_size = min_size
        self._max_size = max_size
        self._application_name = application_name
        self._statement_timeout_ms = statement_timeout_ms
        self._call_timeout = call_timeout_seconds
        self._connect_timeout = connect_timeout_seconds

        self._closed = False
        # Per-calling-thread transaction state: the pooled connection currently
        # checked out by this thread and its re-entrancy depth.
        self._local = threading.local()

        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._run_loop,
            name=f"{application_name}-pg-loop",
            daemon=True,
        )
        self._thread.start()
        self._pool = self._submit(self._open_pool())

    # ------------------------------------------------------------------ loop thread

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def _submit(self, coro: Any, *, timeout: float | None = None) -> Any:
        """Run *coro* on the engine loop thread and block for its result."""
        if self._closed:
            raise PostgresEngineError("storage engine is closed")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(timeout if timeout is not None else self._call_timeout)
        except TimeoutError as exc:
            future.cancel()
            raise PostgresEngineError(f"Operational Store call exceeded {self._call_timeout}s") from exc

    async def _open_pool(self) -> Any:
        from psycopg import AsyncConnection
        from psycopg.rows import dict_row
        from psycopg_pool import AsyncConnectionPool

        statement_timeout = self._statement_timeout_ms

        async def configure(conn: AsyncConnection) -> None:
            # Explicit BEGIN/COMMIT are issued by ``transaction()``; autocommit
            # keeps psycopg from opening an implicit transaction of its own.
            await conn.set_autocommit(True)
            conn.row_factory = dict_row
            await conn.execute(f"SET statement_timeout = {statement_timeout}")
            # Deadlocks are broken by the engine rather than left to hang.
            await conn.execute("SET lock_timeout = 10000")
            await conn.execute("SET idle_in_transaction_session_timeout = 60000")

        pool = AsyncConnectionPool(
            conninfo=self._dsn,
            min_size=self._min_size,
            max_size=self._max_size,
            timeout=self._connect_timeout,
            kwargs={"application_name": self._application_name},
            configure=configure,
            open=False,
        )
        await pool.open(wait=True, timeout=self._connect_timeout * 3)
        return pool

    # ------------------------------------------------------------------ transactions

    @property
    def _depth(self) -> int:
        return getattr(self._local, "depth", 0)

    @property
    def in_transaction(self) -> bool:
        """True when this thread is already inside a :meth:`transaction` scope.

        Lets a caller tell "I own this transaction and will commit in a moment"
        from "I am nested inside somebody else's unit of work" — which decides
        whether it is safe to take a contended row lock; see
        :meth:`defer_to_commit`.
        """
        return self._depth > 0

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """One transaction per logical operation; re-entrant per thread.

        The outermost caller owns the ``BEGIN``/``COMMIT``.  Nested interface
        calls join that transaction, so a maintenance run that wraps its work in
        ``with backend.transaction():`` commits once instead of once per write.
        Work registered with :meth:`defer_to_commit` runs inside the transaction,
        immediately before the ``COMMIT``.
        """
        if self._closed:
            raise PostgresEngineError("storage engine is closed")

        if self._depth > 0:
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return

        conn = self._submit(self._acquire())
        self._local.conn = conn
        self._local.depth = 1
        self._local.pending = {}
        try:
            self._submit(self._execute(conn, "BEGIN"))
            yield
            # Inside the try, so a failure here still rolls back rather than
            # committing a transaction whose deferred half never ran.
            self._flush_pending()
        except BaseException:
            try:
                self._submit(self._execute(conn, "ROLLBACK"))
            except Exception:  # pragma: no cover - rollback of a dead connection
                logger.exception("Operational Store rollback failed")
            raise
        else:
            self._submit(self._execute(conn, "COMMIT"))
        finally:
            self._local.depth = 0
            self._local.conn = None
            self._local.pending = {}
            self._submit(self._release(conn))

    # ------------------------------------------------------------------ deferred work

    def defer_to_commit(self, key: Any, callback: Callable[[], None]) -> None:
        """Run *callback* inside this transaction, immediately before ``COMMIT``.

        This exists for **cache-invalidation writes that every writer must make
        to the same row** — in practice the per-scope state-hash memo.  Issuing
        those inline is a deadlock generator: a transaction that touches two
        relationships in one scope takes the scope row lock while working on the
        first and holds it to ``COMMIT``, then waits for the second
        relationship's row; a concurrent single-relationship writer holds that
        relationship's row and waits for the same scope row.  The lock order is
        inverted and Postgres breaks the cycle with ``deadlock_detected``.

        Deferring collapses the window: the shared row is locked only in the
        final moment before ``COMMIT``, when the transaction is waiting on
        nothing else, so it can never be the middle of a cycle.  *key*
        deduplicates, so a scope touched fifty times in one pass is written
        once; keys are flushed in sorted order so a transaction spanning several
        scopes takes those rows in a stable global order.

        Outside a transaction the callback runs immediately — a bare call is its
        own single-statement transaction, so there is nothing to defer to.
        """
        if not self.in_transaction:
            callback()
            return
        pending = getattr(self._local, "pending", None)
        if pending is None:
            pending = {}
            self._local.pending = pending
        pending[key] = callback

    def has_pending_commit_key(self, key: Any) -> bool:
        """True when *key* is registered for this transaction and not yet flushed.

        A reader inside the same transaction must treat its own deferred
        invalidation as already applied, otherwise it would trust a memo its own
        uncommitted writes have invalidated.
        """
        return key in (getattr(self._local, "pending", None) or {})

    def _flush_pending(self) -> None:
        for _ in range(_MAX_FLUSH_ROUNDS):
            pending = getattr(self._local, "pending", None)
            if not pending:
                return
            self._local.pending = {}
            for _key, callback in sorted(pending.items(), key=lambda item: repr(item[0])):
                callback()
        raise PostgresEngineError("deferred commit work did not settle; a deferred callback keeps registering more")

    async def _acquire(self) -> Any:
        return await self._pool.getconn()

    async def _release(self, conn: Any) -> None:
        await self._pool.putconn(conn)

    @staticmethod
    async def _execute(conn: Any, sql: str, params: Sequence[Any] | None = None) -> None:
        await conn.execute(sql, params)

    # ------------------------------------------------------------------ statement helpers

    def execute(self, sql: str, params: Sequence[Any] | None = None) -> int:
        """Run a statement, returning affected row count. Joins the current transaction."""
        return int(self._with_conn(lambda conn: self._execute_rowcount(conn, sql, params)))

    def fetchone(self, sql: str, params: Sequence[Any] | None = None) -> dict[str, Any] | None:
        return self._with_conn(lambda conn: self._fetch(conn, sql, params, one=True))

    def fetchall(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]:
        return self._with_conn(lambda conn: self._fetch(conn, sql, params, one=False))

    def fetchvalue(self, sql: str, params: Sequence[Any] | None = None) -> Any:
        row = self.fetchone(sql, params)
        if row is None:
            return None
        return next(iter(row.values()))

    def _with_conn(self, factory: Any) -> Any:
        """Resolve this thread's connection, then run ``factory(conn)`` on the loop.

        The coroutine is built only once the connection is known, because the
        coroutine body executes on the loop thread and cannot see the calling
        thread's transaction state.  Outside an explicit ``transaction()`` block
        this opens a single-statement transaction, so every bare call still has
        per-call atomicity per the interface contract.
        """
        if self._depth > 0:
            return self._submit(factory(self._local.conn))
        with self.transaction():
            return self._submit(factory(self._local.conn))

    @staticmethod
    async def _execute_rowcount(conn: Any, sql: str, params: Sequence[Any] | None) -> int:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            return cur.rowcount

    @staticmethod
    async def _fetch(conn: Any, sql: str, params: Sequence[Any] | None, *, one: bool) -> Any:
        async with conn.cursor() as cur:
            await cur.execute(sql, params)
            if one:
                return await cur.fetchone()
            return await cur.fetchall()

    def executemany(self, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        if not rows:
            return
        self._with_conn(lambda conn: self._executemany(conn, sql, rows))

    @staticmethod
    async def _executemany(conn: Any, sql: str, rows: Sequence[Sequence[Any]]) -> None:
        async with conn.cursor() as cur:
            await cur.executemany(sql, rows)

    # ------------------------------------------------------------------ introspection

    def pool_stats(self) -> dict[str, Any]:
        """Pool counters for the observability issue (sizing evidence)."""
        stats = dict(self._pool.get_stats())
        stats["max_size"] = self._max_size
        stats["min_size"] = self._min_size
        return stats

    def server_version(self) -> str:
        return str(self.fetchvalue("SELECT version()"))

    def has_extension(self, name: str) -> bool:
        return bool(
            self.fetchvalue(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = %s)",
                (name,),
            )
        )

    def database_collation(self) -> str:
        return str(self.fetchvalue("SELECT datcollate FROM pg_database WHERE datname = current_database()"))

    def is_byte_ordered(self) -> bool:
        """True when text ordering matches SQLite's BINARY collation.

        Timestamps, uuids, and digests are stored as ``text`` so they stay
        byte-comparable with the SQLite substrate.  Under a linguistic collation
        (``en_US.UTF-8`` and friends) Postgres orders text by locale rules
        instead, so list orderings drift from the SQLite backend even though
        every row is identical — a parity failure that only shows up on the
        managed instance.  Sites where the order is load-bearing (the state-hash
        aggregate, the receipt chain) pin ``COLLATE "C"`` explicitly; this check
        catches everything else by making the deployment surface it.
        """
        return self.database_collation() in {"C", "C.UTF-8", "C.utf8", "POSIX"}

    # ------------------------------------------------------------------ migrations

    # Advisory lock id (arbitrary but fixed) so concurrent replicas serialise DDL
    # instead of racing CREATE statements on startup.
    _MIGRATION_LOCK_ID = 0x44_57_4D_49  # "DWMI"

    def migrate(self, migrations: Sequence[tuple[int, str, str]]) -> list[int]:
        """Apply pending migrations in one transaction under an advisory lock.

        *migrations* is an ordered sequence of ``(version, name, sql)``.  Returns
        the versions actually applied by this call.  Safe to run concurrently
        from every replica: the loser waits, then sees the work already recorded.
        """
        applied: list[int] = []
        with self.transaction():
            self.execute("SELECT pg_advisory_xact_lock(%s)", (self._MIGRATION_LOCK_ID,))
            self.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version     integer PRIMARY KEY,
                    name        text NOT NULL,
                    applied_at  timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            rows = self.fetchall("SELECT version FROM schema_migrations")
            done = {int(row["version"]) for row in rows}
            for version, name, sql in migrations:
                if version in done:
                    continue
                logger.info("applying Operational Store migration %s (%s)", version, name)
                self.execute(sql)
                self.execute(
                    "INSERT INTO schema_migrations (version, name) VALUES (%s, %s)",
                    (version, name),
                )
                applied.append(version)
        return applied

    def schema_version(self) -> int:
        value = self.fetchvalue("SELECT COALESCE(MAX(version), 0) FROM schema_migrations")
        return int(value or 0)

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._pool.close(), self._loop)
            future.result(timeout=self._call_timeout)
        except Exception:  # pragma: no cover - best effort teardown
            logger.exception("Operational Store pool close failed")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=self._call_timeout)
        self._loop.close()


# ---------------------------------------------------------------------------
# Encoding helpers.  These must agree byte-for-byte with the SQLite backend so
# the parity suite compares like with like.


def json_dumps(value: Any, label: str) -> str:
    try:
        return json.dumps(value, sort_keys=True)
    except TypeError as exc:
        raise ValueError(f"{label} must be JSON serializable") from exc


def datetime_to_text(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def datetime_from_text(value: str) -> datetime:
    return datetime.fromisoformat(value)


def optional_datetime_from_text(value: Any) -> datetime | None:
    return None if value is None else datetime_from_text(str(value))
