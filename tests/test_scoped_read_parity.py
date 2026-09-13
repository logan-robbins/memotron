"""Scoped relationship reads must return the same rows on both engines.

Why this file exists separately from ``test_storage_backend_parity.py``
----------------------------------------------------------------------
That suite asserts the two engines **store the same bytes** — state hashes,
receipt chains and Merkle roots are compared byte-for-byte across engines.  It
does not assert that a *scoped read answers a caller the same way*, and a
divergence walked straight through all 117 of its parameters:
``PostgresGraphStore.relationships_for_scope`` omits the
``AND type != 'MENTIONS'`` filter that ``SQLiteStorageBackend`` applies, while
its own docstring claims it matches SQLite.

Three consequences, and the tests below pin each one:

* a scoped read returns structural ``MENTIONS`` edges on Postgres and not on
  SQLite — and they sort **first**, so any caller taking row ``[0]`` gets a
  structural edge on one engine and a memory on the other;
* ``Memotron.search`` raises ``KeyError: 'fact'``, because a ``MENTIONS``
  edge carries no ``fact`` property;
* :func:`memotron.epochs.epoch_content_digest` becomes **non-deterministic**.
  It keys on ``truth_key or relationship.uuid`` and ``MENTIONS`` rows have no
  ``truth_key``, so the digest folds in a uuid minted fresh at every
  materialization.  That digest exists precisely so two *independently
  recomputed* shadow epochs can be compared — the comparison
  ``graph_state_hash`` deliberately cannot support — so on Postgres it inherits
  the exact property it was built to avoid.

The digest test is the one that matters most: fixing only the ``search``
symptom would leave replay verification broken and silent.

Running it
----------
Same contract as the parity suite: Postgres parameters read their DSN from
``MEMOTRON_TEST_POSTGRES_DSN`` and skip when it is unset, so the hermetic
offline suite still passes with no database and no network::

    export MEMOTRON_TEST_POSTGRES_DSN="host=127.0.0.1 port=5433 user=dw dbname=dw_parity"
    pytest tests/test_scoped_read_parity.py
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from memotron.client import Memotron
from memotron.epochs import epoch_content_digest
from memotron.models import MemoryScope, ScopeKind
from memotron.storage import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend

POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"
POSTGRES_SKIP_REASON = f"set {POSTGRES_DSN_ENV} to run Postgres parity"

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="scoped-read-parity")

# Three facts sharing no subject, so each occupies its own truth slot and the
# expected memory count is unambiguous.
FACTS = (
    ("the api service", "requires", "a durable store", "REQUIRES"),
    ("the operator", "prefers", "postgres for the operational store", "PREFERS"),
    ("the worker", "requires", "a claim lock before processing", "REQUIRES"),
)


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(POSTGRES_SKIP_REASON)
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    """Give the next Postgres backend an empty database.

    The only engine-specific statements in this file, mirroring
    ``test_storage_backend_parity._reset_postgres_schema``: each test starts
    from nothing so the file is order-independent, exactly as ``:memory:``
    gives SQLite for free.  The backend re-runs its migrations on construction.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


def _open_backend(engine: str) -> StorageBackend:
    if engine == "sqlite":
        return SQLiteStorageBackend(":memory:")
    dsn = _postgres_dsn_or_skip()
    _reset_postgres_schema(dsn)
    # Imported lazily so a hermetic run never needs psycopg installed.
    from memotron.storage.postgres import PostgresStorageBackend

    return PostgresStorageBackend(dsn, min_size=1, max_size=4)


def _engine_name(backend: StorageBackend) -> str:
    return "sqlite" if isinstance(backend, SQLiteStorageBackend) else "postgres"


async def _seed(backend: StorageBackend) -> Memotron:
    """Write the three facts through the client, the way a caller would."""
    client = Memotron(storage=backend)
    for subject, predicate, obj, relationship_type in FACTS:
        await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=obj,
            relationship_type=relationship_type,
            scope=SCOPE,
        )
    return client


# The engine is a fixture PARAMETER, so the postgres marker rides on the param
# rather than the module: `-m "not postgres"` must still run every [sqlite] twin
# of these assertions. Marking the file instead would silently halve the suite.
@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def backend(request: pytest.FixtureRequest) -> Iterator[StorageBackend]:
    store = _open_backend(request.param)
    try:
        yield store
    finally:
        store.close()


@pytest.mark.asyncio
async def test_scoped_read_excludes_structural_mentions_edges(
    backend: StorageBackend,
) -> None:
    """``relationships_for_scope`` returns memories, never ``MENTIONS`` edges.

    ``MENTIONS`` is structural bookkeeping emitted per entity.  A caller asking
    a scope for its relationships is asking for its memories; leaking the
    structural edges changes what 16 call sites in ``client``, ``epochs`` and
    ``dreaming`` iterate over, and they carry neither ``fact`` nor ``truth_key``.
    """
    await _seed(backend)

    rows = backend.relationships_for_scope(SCOPE.key)
    leaked = [row for row in rows if row.type == "MENTIONS"]

    assert not leaked, (
        f"engine {_engine_name(backend)!r} leaked {len(leaked)} MENTIONS edge(s) from a "
        f"scoped read: returned {len(rows)} rows for a scope holding {len(FACTS)} memories. "
        "SQLite filters these SQL-side; Postgres must too."
    )
    assert len(rows) == len(FACTS), (
        f"engine {_engine_name(backend)!r} returned {len(rows)} rows for a scope holding {len(FACTS)} memories"
    )


@pytest.mark.asyncio
async def test_scoped_read_first_row_is_a_memory(backend: StorageBackend) -> None:
    """Row ``[0]`` must be a memory on every engine.

    Ordering matters independently of the count: ``MENTIONS`` edges sort ahead
    of the memories they annotate, so a caller taking the first row silently
    gets a different *kind* of thing depending on the engine.
    """
    await _seed(backend)

    rows = backend.relationships_for_scope(SCOPE.key)

    assert rows, "scoped read returned nothing for a seeded scope"
    assert rows[0].type != "MENTIONS", (
        f"engine {_engine_name(backend)!r} put a structural MENTIONS edge first; a caller "
        "taking row [0] gets a structural edge on one engine and a memory on the other"
    )


@pytest.mark.asyncio
async def test_search_returns_memories(backend: StorageBackend) -> None:
    """``search`` must not raise on a scope whose entities have MENTIONS edges.

    The observable form of the leak: ``client`` reads ``properties["fact"]`` and
    a structural edge has none, so the call fails with ``KeyError: 'fact'``.
    """
    client = await _seed(backend)

    results = await client.search(query="durable store", scope=SCOPE)

    assert results, f"engine {_engine_name(backend)!r} found nothing for a seeded query"
    assert all(getattr(result, "fact", None) for result in results), (
        f"engine {_engine_name(backend)!r} returned a result with no fact"
    )


@pytest.mark.postgres
@pytest.mark.asyncio
async def test_epoch_content_digest_is_stable_across_recomputation() -> None:
    """The digest must be identical for two independently built, equal graphs.

    This is the digest's entire purpose — its docstring contrasts it with
    ``graph_state_hash``, which binds each row's literal uuid and therefore
    *cannot* compare two independent recomputations.  If the digest folds in a
    freshly minted uuid it silently loses that property, and the WS-26
    shadow/re-dream comparison built on it becomes meaningless.

    Runs per engine, twice each, so a failure names the engine that drifted.
    """
    digests: dict[str, list[str]] = {}
    for engine in ("sqlite", "postgres"):
        digests[engine] = []
        for _ in range(2):
            store = _open_backend(engine)
            try:
                await _seed(store)
                digests[engine].append(epoch_content_digest(storage=store, scope=SCOPE))
            finally:
                store.close()

    for engine, pair in digests.items():
        assert pair[0] == pair[1], (
            f"engine {engine!r} produced a different epoch_content_digest for two "
            f"independently built graphs holding identical content: {pair[0]} then {pair[1]}. "
            "The digest is the equality notion for independently recomputed epochs; "
            "if it varies, replay verification cannot be trusted on this engine."
        )

    assert digests["sqlite"][0] == digests["postgres"][0], (
        "epoch_content_digest differs BETWEEN engines for identical content: "
        f"sqlite {digests['sqlite'][0]}, postgres {digests['postgres'][0]}. "
        "A backend migration would invalidate every recorded epoch comparison."
    )
