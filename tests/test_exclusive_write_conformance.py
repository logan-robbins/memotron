"""The exactly-once claim contract, on BOTH engines (#134).

Why this file exists
--------------------
``tests/test_dream_concurrency.py`` pins the properties that decide whether Memotron can
run with more than one replica — a queued episode extracted exactly once, a crashed run's
claim expiring, no double-synthesized rollup, admin single-flight. Measured 2026-08-31:
**all 5 of those tests run on SQLite only**, 0 collected under ``-m postgres``.

That is not a formality, because the two engines implement the guarantee with different
primitives:

  SQLite    ``BEGIN IMMEDIATE`` takes the database's RESERVED lock, serialising every writer
            (``storage/sqlite/__init__.py:157``)
  Postgres  one fixed transaction-scoped ``pg_advisory_xact_lock``
            (``storage/postgres/__init__.py:212``)

The call sites are engine-agnostic — both ``claim_episodes`` and ``claim_scope_work`` are
written against ``exclusive_write_transaction()`` — so what is untested is the *contract of
that primitive* on the engine that will actually run in production. SQLite is per-pod by
construction, so its whole-database lock is trivially correct for a single writer. The
advisory-lock path is the one that has to hold when two pods contend, and nothing exercised
it.

Same shape as T0-4, where ``postgres/_graph.py`` dropped a filter ``sqlite/_graph.py``
applied and a 3,420-line parity suite passed anyway: two implementations of one contract,
only one of them driven.

What this pins, and what it does not
------------------------------------
It drives the real product path (``claim_scope_work``) rather than an invented counter table,
from two INDEPENDENT backend instances against ONE store — which for Postgres means two pool
connections, the closest single-process analogue of two replicas.

It is **not** a two-process test. Threads in one process share an interpreter, and for SQLite
they even share a file lock manager. That is why
:func:`test_the_two_handles_really_share_one_store` exists: without it, two backends that
silently addressed *different* stores would let every claim succeed, and the suite would look
green while proving nothing. The parity suites' own fixture opens SQLite as ``:memory:``,
where exactly that mistake is one line away.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from memotron.storage.base import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
CONSUMER = "consolidation-default"

BackendFactory = Callable[..., StorageBackend]


@pytest.fixture
def scope_key() -> str:
    """A scope nobody else has claimed.

    SQLite gets a fresh ``tmp_path`` per test, but the Postgres DSN points at ONE shared
    database, so a fixed key let the first test's claim block the third test's first claim --
    which surfaced as `assert False is True` on a "fresh" store and looks exactly like an
    engine divergence. It was leaked state. Isolating by key is cheaper than dropping the
    schema between tests and makes both arms behave the same way.
    """
    return f"tenant:conformance-{uuid4().hex[:12]}"


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get("MEMOTRON_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMOTRON_TEST_POSTGRES_DSN to run the Postgres twin")
    return dsn


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def store_address(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[str, str]:
    """(engine, address) — everything a SEPARATE PROCESS needs to open the same store.

    The in-process fixture below hands out a factory, which a child process cannot inherit.
    A child gets an address and opens its own handle, which is what a replica actually does.
    """
    if request.param == "sqlite":
        return ("sqlite", str(tmp_path / "twoproc.sqlite"))
    return ("postgres", _postgres_dsn_or_skip())


#: Run in a CHILD PROCESS. Opens its own backend, waits for a wall-clock start shared with its
#: sibling, then claims. Wall-clock rather than a pipe or a barrier because the two children
#: share no parent objects -- which is the entire point of running them as processes.
_CHILD = r"""
import json, sys, time
from datetime import UTC, datetime
engine, address, scope_key, run_uuid, start_at = sys.argv[1:6]
if engine == "sqlite":
    from memotron.storage.sqlite import SQLiteStorageBackend as B
    backend = B(address)
else:
    from memotron.storage.postgres import PostgresStorageBackend as B
    backend = B(address, min_size=1, max_size=2)
now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
time.sleep(max(0.0, float(start_at) - time.time()))
try:
    won = backend.claim_scope_work(
        work_kind="consolidation", scope_key=scope_key,
        consumer_key="consolidation-default", run_uuid=run_uuid, now=now,
    )
    print("__R__" + json.dumps({"won": bool(won)}))
except Exception as exc:
    print("__R__" + json.dumps({"won": False, "error": f"{type(exc).__name__}: {exc}"}))
finally:
    backend.close()
"""


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def open_backend(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[BackendFactory]:
    """A factory for INDEPENDENT backends that address the SAME store.

    SQLite is file-backed here, not ``:memory:``. Two ``:memory:`` backends are two
    different databases, so a contention test against them measures nothing — the parity
    suites can use ``:memory:`` because they only ever open one.
    """
    opened: list[StorageBackend] = []

    if request.param == "sqlite":
        path = tmp_path / "conformance.sqlite"

        def factory(track: bool = True) -> StorageBackend:
            backend = SQLiteStorageBackend(str(path))
            if track:
                opened.append(backend)
            return backend
    else:
        dsn = _postgres_dsn_or_skip()
        from memotron.storage.postgres import PostgresStorageBackend

        def factory(track: bool = True) -> StorageBackend:
            backend = PostgresStorageBackend(dsn, min_size=1, max_size=4)
            if track:
                opened.append(backend)
            return backend

    try:
        yield factory
    finally:
        for backend in opened:
            backend.close()


def _claim(backend: StorageBackend, scope: str, run_uuid: str, *, now: datetime = NOW) -> bool:
    return backend.claim_scope_work(
        work_kind="consolidation",
        scope_key=scope,
        consumer_key=CONSUMER,
        run_uuid=run_uuid,
        now=now,
    )


def test_the_two_handles_really_share_one_store(open_backend: BackendFactory, scope_key: str) -> None:
    """Precondition. Without it every assertion below can pass for the wrong reason.

    If the two handles addressed different stores, both claims would succeed and the
    exactly-once test would fail — but the failure would look like a locking bug rather than
    a fixture bug, which is the expensive kind of red.
    """
    a, b = open_backend(), open_backend()
    assert _claim(a, scope_key, "run-a") is True, "the first claim on a fresh store must succeed"
    assert _claim(b, scope_key, "run-b") is False, (
        "the second handle claimed the same scope, so the two handles are NOT looking at the "
        "same store — fix the fixture before reading any other result in this file"
    )


def test_only_one_of_two_concurrent_writers_claims_the_same_scope(
    open_backend: BackendFactory,
    scope_key: str,
) -> None:
    """The multi-replica guarantee, contended rather than sequential.

    Two independent handles race the same claim behind a barrier. Exactly one may win. On
    Postgres these are two pool connections, which is the closest single-process analogue of
    two pods; on SQLite it is two connections to one file.
    """
    barrier = threading.Barrier(2)
    open_lock = threading.Lock()
    results: dict[str, bool | str] = {}

    def attempt(name: str) -> None:
        # The backend is opened INSIDE the thread, which is both required and more faithful.
        # Required: a SQLite connection is thread-affine, and opening it on the main thread
        # then using it here raises "SQLite objects created in a thread can only be used in
        # that same thread" -- the first version of this test did exactly that and reported
        # zero winners, which reads like a locking bug and is not one.
        # Faithful: a replica opens its own connection; it does not inherit one.
        backend = None
        try:
            # Opening is SERIALISED; only the claim is raced. Opening a SQLite backend runs
            # migrations, which take the write lock, so two simultaneous opens gave
            # "database is locked" during SETUP -- one thread died before the barrier and the
            # other timed out on it. That is contention in the fixture, not in the contract,
            # and reading it as a claim failure would have been a false finding.
            with open_lock:
                backend = open_backend(track=False)
            barrier.wait(timeout=10)
            results[name] = _claim(backend, scope_key, f"run-{name}")
        except Exception as exc:  # a lock timeout is a RESULT, not a harness failure
            results[name] = f"{type(exc).__name__}: {exc}"
        finally:
            # Closed HERE, not by the fixture: a SQLite handle is thread-affine on close too.
            if backend is not None:
                # Teardown noise must not mask the RESULT this test exists to report.
                with contextlib.suppress(Exception):
                    backend.close()

    threads = [
        threading.Thread(target=attempt, args=("a",)),
        threading.Thread(target=attempt, args=("b",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive(), "a claimant deadlocked — the critical section never released"

    winners = [name for name, outcome in results.items() if outcome is True]
    errors = {name: outcome for name, outcome in results.items() if isinstance(outcome, str)}
    assert len(winners) == 1, (
        f"exactly one claimant may win; got winners={winners} results={results}. "
        "Two winners means the read-then-write interleaved and two replicas would both "
        "consolidate the same scope; zero means the claim path refused both."
    )
    assert not errors, f"a claimant raised instead of losing cleanly: {errors}"


def test_a_stale_claim_is_reclaimable_by_a_later_run(open_backend: BackendFactory, scope_key: str) -> None:
    """A crashed replica must not wedge the queue forever.

    Time semantics are the half most likely to differ between engines — the lock is released
    at COMMIT on Postgres and at transaction end on SQLite, but claim EXPIRY is computed from
    the caller's clock in both.
    """
    a, b = open_backend(), open_backend()
    assert _claim(a, scope_key, "run-crashed") is True
    assert _claim(b, scope_key, "run-next") is False, "an unexpired claim must block a second run"

    later = NOW + timedelta(hours=6)
    assert _claim(b, scope_key, "run-next", now=later) is True, (
        "a claim older than the staleness window must be reclaimable, or one crashed run wedges the scope permanently"
    )


def _run_child(engine: str, address: str, scope: str, run_uuid: str, start_at: float) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, engine, address, scope, run_uuid, f"{start_at:.3f}"],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "MEMOTRON_ALLOW_EPHEMERAL_KEK": "1"},
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__R__"):
            return json.loads(line[5:])
    return {"won": False, "error": f"no result: {proc.stdout[-200:]} {proc.stderr[-300:]}"}


def test_only_one_of_two_PROCESSES_claims_the_same_scope(store_address: tuple[str, str], scope_key: str) -> None:
    """The multi-replica guarantee across PROCESSES, not threads.

    Threads share an interpreter, and for SQLite a file lock manager, so a thread test can
    pass while two pods still both win. ``probe_kek``'s equivalent arm C -- a fresh process on
    a fresh filesystem -- is where a real defect was found for the KEK question, which is why
    this arm is not ceremony.

    Two children open their own handles and claim behind a shared wall-clock start, because
    they share no parent objects to synchronise on.
    """
    engine, address = store_address
    if engine == "sqlite":
        # The first open MIGRATES, which takes the write lock. Two children opening at once
        # contend in SETUP rather than on the claim -- the same trap the thread test hit.
        # Migrate once here so the children race the claim and nothing else.
        SQLiteStorageBackend(address).close()

    start_at = time.time() + 2.0
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_run_child, engine, address, scope_key, f"run-{name}", start_at) for name in ("a", "b")]
        results = [f.result() for f in futures]

    winners = [r for r in results if r.get("won")]
    errors = [r["error"] for r in results if r.get("error")]
    assert not errors, f"a child raised instead of losing cleanly: {errors}"
    assert len(winners) == 1, (
        f"exactly one PROCESS may claim the scope; got {len(winners)} winners from {results}. "
        "Two winners means two replicas would both consolidate the same scope."
    )
