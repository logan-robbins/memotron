"""Enabling the operational store must actually move every surface onto it.

Two independent defects live here, both invisible to a test that checks a
return value instead of where the bytes went.

Engine selection
----------------
``storage_settings_from_env()`` is consulted in exactly two places —
``default_config()`` and ``mcp_server``.  ``agent_memory_config()`` builds its
own ``DreamConfig`` and never sets ``storage``, and ``Memotron.__init__``
*raises* when given both ``control_plane`` and ``config``, so ``base_config`` is
the only seam the DSN could reach the agent-memory surface through — and it is
unwired.  The result: with a Postgres DSN injected, ``Memotron`` writes to
Postgres while ``build_platform_from_env`` and ``build_platform_from_project``
write to a local SQLite file.  That covers the 27 agent-memory MCP tools, the
``AgentMemoryPlatform`` facade, and every Claude Code hook — i.e. the whole
compaction/context path.  Flipping ``operationalStore.enabled`` would leave two
halves of one deployment disagreeing about where memory lives.

These tests **attribute a write** rather than trusting the DSN or the backend
class name.  A configured DSN, a plausible return value and a correct-looking
type are all present in the broken case.

Ephemeral KEK
-------------
``PostgresStorageBackend`` falls back to ``LocalKeyManager.ephemeral()`` when no
key manager is supplied, and no caller supplies one.  ``ephemeral()`` is a
random per-process key — "keys die with the process" — so content sealed by one
process is unreadable by the next.  A single restart is enough; it is not a
multi-replica-only problem.  And it fails **silently**: tenant credential status
keeps reporting ``has_api_key: True`` after the key has become unusable.

The guard is fail-closed at construction, with an explicit opt-out for local
development and for this suite, which uses the ephemeral key deliberately.

Running it
----------
Postgres tests read ``MEMOTRON_TEST_POSTGRES_DSN`` and skip when unset, so
the hermetic offline suite still passes with no database and no network.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from memotron.client import Memotron
from memotron.models import MemoryScope, ScopeKind

# all 4 tests need a live Postgres; the marker makes the CI lane selectable with `-m postgres`.
pytestmark = pytest.mark.postgres

POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"
POSTGRES_SKIP_REASON = f"set {POSTGRES_DSN_ENV} to run operational-store wiring tests"

ALLOW_EPHEMERAL_KEK_ENV = "MEMOTRON_ALLOW_EPHEMERAL_KEK"

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="store-wiring")


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(POSTGRES_SKIP_REASON)
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


def _postgres_relationship_count(dsn: str) -> int:
    """Rows in Postgres, read outside the library.

    Deliberately not routed through the storage interface: the whole point is to
    observe where the bytes landed independently of the code under test.  A
    missing table means migrations never ran, which is zero rows.
    """
    import psycopg

    try:
        with psycopg.connect(dsn) as connection:
            row = connection.execute("SELECT count(*) FROM relationships").fetchone()
            return int(row[0]) if row else 0
    except Exception as exc:
        if "relationships" in str(exc) or "UndefinedTable" in type(exc).__name__:
            return 0
        raise


async def _write_one(client: Any) -> None:
    await client.add_memory(
        subject="the operational store",
        predicate="receives",
        object="a write attribution probe",
        relationship_type="REQUIRES",
        scope=SCOPE,
    )


@pytest.fixture
def postgres_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, Path]:
    """A clean Postgres database plus the env a deployment would inject."""
    dsn = _postgres_dsn_or_skip()
    _reset_postgres_schema(dsn)
    graph_path = tmp_path / "unused-sqlite-shim.sqlite"
    monkeypatch.setenv("MEMOTRON_OPERATIONAL_STORE_DSN", dsn)
    monkeypatch.setenv("MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE", "1")
    monkeypatch.setenv("MEMOTRON_OPERATIONAL_STORE_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("MEMOTRON_GRAPH_PATH", str(graph_path))
    monkeypatch.setenv("MEMOTRON_PROJECT_ID", "store-wiring")
    # This suite seals with the ephemeral KEK on purpose; see the guard test.
    monkeypatch.setenv(ALLOW_EPHEMERAL_KEK_ENV, "1")
    return dsn, graph_path


@pytest.mark.asyncio
async def test_sdk_client_writes_to_the_operational_store(
    postgres_env: tuple[str, Path],
) -> None:
    """Baseline: the plain SDK path honours the injected DSN.

    Present so a failure in the next test cannot be blamed on the fixture.
    """
    dsn, _graph_path = postgres_env

    before = _postgres_relationship_count(dsn)
    await _write_one(Memotron(graph_path=os.environ["MEMOTRON_GRAPH_PATH"]))
    after = _postgres_relationship_count(dsn)

    assert after > before, (
        "Memotron(graph_path=...) did not write to the operational store even though "
        f"a DSN was injected: relationships {before} -> {after}"
    )


@pytest.mark.asyncio
async def test_agent_memory_platform_writes_to_the_operational_store(
    postgres_env: tuple[str, Path],
) -> None:
    """The agent-memory surface must honour the DSN too.

    ``build_platform_from_env`` is what the agent-memory MCP server and the SDK
    facade construct.  Verified by attributing the write: in the broken case the
    call succeeds, returns a ``relationship_uuid``, and the row is in a local
    SQLite file instead.
    """
    dsn, graph_path = postgres_env
    from memotron.agent_memory_mcp import build_platform_from_env

    platform = build_platform_from_env()
    client = getattr(platform, "client", None) or getattr(platform, "_client", None)
    assert client is not None, "could not reach the platform's inner client"

    before = _postgres_relationship_count(dsn)
    await _write_one(client)
    after = _postgres_relationship_count(dsn)

    assert after > before, (
        "build_platform_from_env() did not write to the operational store even though a DSN "
        f"was injected: relationships {before} -> {after}. The agent-memory surface — 27 MCP "
        "tools and every Claude Code hook — silently stayed on SQLite "
        f"(graph_path={graph_path.name})."
    )


def test_postgres_backend_refuses_an_ephemeral_kek(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Constructing the Postgres backend without a key manager must fail closed.

    An ephemeral KEK is random per process, so content sealed by this process
    is unreadable after any restart and by every other replica — and the failure
    surfaces as ``wrapped DEK failed authentication`` deep in a governance read,
    long after the data became unrecoverable, while status still reports the
    credential as configured.  Refusing at construction turns a silent
    cluster-wide crypto-shred into a startup error.
    """
    dsn = _postgres_dsn_or_skip()
    monkeypatch.delenv(ALLOW_EPHEMERAL_KEK_ENV, raising=False)
    from memotron.storage.postgres import PostgresStorageBackend

    with pytest.raises(ValueError, match="ephemeral"):
        PostgresStorageBackend(dsn, min_size=1, max_size=4, migrate=False)


def test_ephemeral_kek_opt_out_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """The opt-out must keep local development and this suite working.

    Without an escape hatch the guard would break every hermetic Postgres test
    and every local docker-compose run, which is how a fail-closed guard gets
    reverted instead of fixed.
    """
    dsn = _postgres_dsn_or_skip()
    _reset_postgres_schema(dsn)
    monkeypatch.setenv(ALLOW_EPHEMERAL_KEK_ENV, "1")
    from memotron.storage.postgres import PostgresStorageBackend

    backend = PostgresStorageBackend(dsn, min_size=1, max_size=4)
    try:
        assert backend.key_manager is not None
    finally:
        backend.close()
