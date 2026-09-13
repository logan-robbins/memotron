"""The per-key registry: `key_alias` -> `MemoryPrincipal`, on both engines. (DW-030)

DW-026 maps a caller's gateway `key_alias` to a principal *"through the per-key registry on
that record"*. That registry did not exist -- the storage contract had no `key_hash`,
`api_key`, `virtual_key`, `key_registry` or `key_alias` method at all.

DW-030 settled how to build it, and the reason it is a NEW table rather than a reuse of
`tenant_agents` is the property this file exists to pin:

    the gateway guarantees `key_alias` is unique GLOBALLY -- DW-027 attempted forgery three
    ways, including renaming an existing key onto a colleague's, and all three were refused

    `tenant_agents` guarantees uniqueness only WITHIN a tenant, on both engines:
        sqlite    UNIQUE INDEX ON tenant_agents(tenant_id, agent_id_key)
        postgres  UNIQUE INDEX ON tenant_agents (tenant_id, agent_id_key) WHERE agent_id_key <> ''

So storing aliases in that table would let two tenants claim the same alias and make the
lookup ambiguous -- the database would stop enforcing the exact property the security model
rests on. `test_the_same_alias_cannot_be_claimed_by_two_tenants` is that guarantee.

Two further reasons, also pinned here: the lookup must work with NO tenant in hand (finding
the tenant is the job -- `test_lookup_needs_no_tenant`), and the row must carry everything a
`MemoryPrincipal` needs, which `tenant_agents` does not
(`test_the_row_carries_a_whole_principal`).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from memotron.storage.base import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend

ALIAS = "dw-acme-prod-01"


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get("MEMOTRON_TEST_POSTGRES_DSN", "").strip()
    if not dsn:
        pytest.skip("set MEMOTRON_TEST_POSTGRES_DSN to run: needs a live Postgres")
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DROP SCHEMA IF EXISTS public CASCADE")
        conn.execute("CREATE SCHEMA public")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param("postgres", marks=pytest.mark.postgres),
    ]
)
def backend(request: pytest.FixtureRequest) -> Iterator[StorageBackend]:
    if request.param == "postgres":
        dsn = _postgres_dsn_or_skip()
        _reset_postgres_schema(dsn)
        from memotron.storage.postgres import PostgresStorageBackend

        store: StorageBackend = PostgresStorageBackend(dsn, min_size=1, max_size=2)
    else:
        store = SQLiteStorageBackend(":memory:")
    try:
        yield store
    finally:
        store.close()


def _bind(store: StorageBackend, alias: str, tenant: str, **kw: object) -> dict:
    return store.bind_key_principal(
        key_alias=alias,
        principal_id=kw.pop("principal_id", f"{tenant}-svc"),  # type: ignore[arg-type]
        tenant_id=tenant,
        **kw,  # type: ignore[arg-type]
    )


class TestTheLookupTheRegistryExistsFor:
    def test_lookup_needs_no_tenant(self, backend: StorageBackend) -> None:
        """The reason `tenant_agents` could not host this.

        At the moment a request arrives we hold a `key_alias` and NO tenant -- finding the
        tenant is the job. `tenant_agents` is keyed `(tenant_id, agent_id)` and every query
        against it is scoped `WHERE tenant_id = ?`, so it cannot answer this.
        """
        _bind(backend, ALIAS, "acme")

        found = backend.principal_for_key_alias(ALIAS)

        assert found is not None
        assert found["tenant_id"] == "acme"

    def test_an_unknown_alias_returns_none_rather_than_raising(self, backend: StorageBackend) -> None:
        """Fail closed and quietly: an unknown caller is not an error condition, it is
        simply not authenticated. Raising here would make an ordinary unauthenticated
        request indistinguishable from a storage fault."""
        assert backend.principal_for_key_alias("never-issued") is None

    def test_the_row_carries_a_whole_principal(self, backend: StorageBackend) -> None:
        """`MemoryPrincipal` needs principal_id, tenant_id, agent_id, default_scope,
        allowed_scope_keys and role. `tenant_agents` carries none of the last four."""
        _bind(
            backend,
            ALIAS,
            "acme",
            principal_id="acme-ops",
            agent_id="claude-code",
            role="admin",
            default_scope_key="tenant:acme",
            allowed_scope_keys=("tenant:acme", "customer:acme:wdw"),
        )

        found = backend.principal_for_key_alias(ALIAS)

        assert found == {
            "key_alias": ALIAS,
            "principal_id": "acme-ops",
            "tenant_id": "acme",
            "agent_id": "claude-code",
            "role": "admin",
            "default_scope_key": "tenant:acme",
            "allowed_scope_keys": ("tenant:acme", "customer:acme:wdw"),
        }


class TestGlobalUniquenessIsEnforcedByTheDatabase:
    """DW-030's load-bearing property. A comment cannot enforce it; the schema must."""

    def test_the_same_alias_cannot_be_claimed_by_two_tenants(self, backend: StorageBackend) -> None:
        """THE REGRESSION. If this passes on a tenant-scoped index, the lookup is ambiguous
        and one tenant's caller can resolve to another tenant's principal."""
        _bind(backend, ALIAS, "acme")

        with pytest.raises(ValueError, match="already bound"):
            _bind(backend, ALIAS, "beta-corp")

        # And the original binding is untouched -- a refused claim must not damage it.
        found = backend.principal_for_key_alias(ALIAS)
        assert found is not None
        assert found["tenant_id"] == "acme"

    def test_two_tenants_may_hold_DIFFERENT_aliases(self, backend: StorageBackend) -> None:
        """The control. Uniqueness must constrain the alias, not the tenant -- otherwise the
        refusal above would pass for the wrong reason."""
        _bind(backend, "dw-acme-01", "acme")
        _bind(backend, "dw-beta-01", "beta-corp")

        assert backend.principal_for_key_alias("dw-acme-01")["tenant_id"] == "acme"  # type: ignore[index]
        assert backend.principal_for_key_alias("dw-beta-01")["tenant_id"] == "beta-corp"  # type: ignore[index]

    def test_a_tenant_may_hold_several_aliases(self, backend: StorageBackend) -> None:
        """One tenant, many keys -- the case Ryan raised: a user with several keys, or a
        service account per agent. Nothing about global alias uniqueness forbids it."""
        _bind(backend, "dw-acme-01", "acme", agent_id="agent-one")
        _bind(backend, "dw-acme-02", "acme", agent_id="agent-two")

        assert backend.principal_for_key_alias("dw-acme-01")["agent_id"] == "agent-one"  # type: ignore[index]
        assert backend.principal_for_key_alias("dw-acme-02")["agent_id"] == "agent-two"  # type: ignore[index]


class TestRotationAndRevocation:
    def test_rebinding_the_same_alias_to_the_same_tenant_updates_it(self, backend: StorageBackend) -> None:
        """Key regeneration keeps the alias (DW-027: alias survives, key material does not),
        so re-binding is the rotation path and must not be refused as a collision."""
        _bind(backend, ALIAS, "acme", role="user")

        _bind(backend, ALIAS, "acme", role="admin", agent_id="promoted")

        found = backend.principal_for_key_alias(ALIAS)
        assert found is not None
        assert found["role"] == "admin"
        assert found["agent_id"] == "promoted"

    def test_unbinding_stops_the_lookup_resolving(self, backend: StorageBackend) -> None:
        _bind(backend, ALIAS, "acme")
        assert backend.principal_for_key_alias(ALIAS) is not None

        assert backend.unbind_key_principal(ALIAS) is True
        assert backend.principal_for_key_alias(ALIAS) is None

        # Idempotent: revoking twice is not an error, and the second call says so.
        assert backend.unbind_key_principal(ALIAS) is False

    def test_unbinding_one_alias_leaves_the_others(self, backend: StorageBackend) -> None:
        _bind(backend, "dw-acme-01", "acme")
        _bind(backend, "dw-acme-02", "acme")

        backend.unbind_key_principal("dw-acme-01")

        assert backend.principal_for_key_alias("dw-acme-01") is None
        assert backend.principal_for_key_alias("dw-acme-02") is not None


class TestInputIsNormalisedTheWayStorageNormalisesElsewhere:
    def test_a_blank_alias_is_refused(self, backend: StorageBackend) -> None:
        with pytest.raises(ValueError):
            _bind(backend, "   ", "acme")

    def test_alias_whitespace_is_stripped_on_both_write_and_read(self, backend: StorageBackend) -> None:
        """#158 F2: the client cached on a raw tenant_id while storage stripped it, and a
        whitespace variant pinned a revoked credential forever. Normalise on both sides here
        so the same divergence cannot open."""
        _bind(backend, "  dw-acme-01  ", "acme")

        assert backend.principal_for_key_alias("dw-acme-01") is not None
        assert backend.principal_for_key_alias("  dw-acme-01  ") is not None


@pytest.mark.postgres
class TestTheTableReachesAnExistingDatabase:
    """The registry must appear on a database that has ALREADY been migrated.

    The first version of this work declared `key_principals` inside `_V1_CORE`. Every
    test passed and the table would have reached **no existing deployment**:
    `PostgresEngine.migrate` skips any version already recorded in `schema_migrations`,
    so a store that had applied V1 before the edit would never create the table and
    `bind_key_principal` would raise `UndefinedTable` at runtime.

    The whole suite missed it because the fixture drops and recreates the schema on every
    run -- so V1 was always applied fresh, and the fresh-database path is the one path
    where the bug is invisible. This test exercises the UPGRADE path instead: it is the
    only one here that would have failed.

    SQLite is unaffected (one idempotent `CREATE TABLE IF NOT EXISTS` script re-run on
    every open), which is why this is postgres-only.
    """

    def test_a_store_already_at_an_older_version_gains_the_table(self) -> None:
        import psycopg

        from memotron.storage.postgres import PostgresStorageBackend

        dsn = _postgres_dsn_or_skip()
        _reset_postgres_schema(dsn)
        PostgresStorageBackend(dsn, min_size=1, max_size=2).close()

        # Rewind to "migrated, but before the registry landed": forget version 9 and drop
        # what it created. Everything else stays applied, exactly like a live store.
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DROP TABLE IF EXISTS key_principals")
            conn.execute("DELETE FROM schema_migrations WHERE version = 9")
            still_applied = conn.execute("SELECT count(*) FROM schema_migrations").fetchone()
        assert still_applied is not None and still_applied[0] > 0, (
            "precondition: the store must still look migrated, or this proves nothing"
        )

        store = PostgresStorageBackend(dsn, min_size=1, max_size=2)
        try:
            store.bind_key_principal(key_alias=ALIAS, principal_id="p", tenant_id="acme")
            found = store.principal_for_key_alias(ALIAS)
        finally:
            store.close()

        assert found is not None, (
            "the registry table never reached an already-migrated database -- the DDL is "
            "in a migration version that store has already recorded as applied"
        )
        assert found["tenant_id"] == "acme"
