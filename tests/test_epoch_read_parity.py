"""T0-10: every epoch-filtered relationship read must agree with every other.

The defect this pins: ``relationships_for_scope`` filtered rows to the scope's
active-epoch ancestry and ``relationships_for_node_uuids`` — the entity-hop
frontier that retrieval's expansion stage uses — did not.  A row outside the
ancestry was therefore denied by the scoped read and *returned* by ``search()``,
so ``redream_rollback`` reverted the audit-visible state while the agent kept
being handed the rolled-back memory, and migrated rows carrying a foreign
``epoch_id`` surfaced from search while ``profile()`` said they did not exist.

PARAMETRISED OVER BOTH ENGINES 2026-08-31.  It was SQLite-only, which is why it never saw
that T0-10 was fixed on SQLite and left LIVE on Postgres -- the production engine.  The
register predicted exactly that ("Postgres needs the same change"), and the test written to
pin the invariant could not check the engine it mattered on.  Third instance of this shape,
after T0-4 (a dropped MENTIONS filter) and T0-7 (a memo where the twin recomputes).

These tests assert the invariant directly rather than driving the full
branch/adopt/rollback machinery (that lives in ``test_redream_end_to_end.py``),
so a regression is attributed to the read method that caused it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from memotron.storage.base import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend

SCOPE = "user:alice"
POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"


def _postgres_dsn_or_skip() -> str:
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"set {POSTGRES_DSN_ENV} to run the Postgres twin")
    return dsn


def _reset_postgres_schema(dsn: str) -> None:
    """Empty database per test, mirroring what ``:memory:`` gives SQLite for free."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        connection.execute("DROP SCHEMA IF EXISTS public CASCADE")
        connection.execute("CREATE SCHEMA public")


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


def _endpoints(store: StorageBackend) -> tuple[str, str]:
    source, _ = store.upsert_node(
        labels=["Entity"], key=f"{SCOPE}|alice", properties={"name": "Alice", "scope_key": SCOPE}
    )
    target, _ = store.upsert_node(
        labels=["Entity"], key=f"{SCOPE}|seattle", properties={"name": "Seattle", "scope_key": SCOPE}
    )
    return source.uuid, target.uuid


def _add_fact(
    store: StorageBackend,
    source_uuid: str,
    target_uuid: str,
    *,
    fact: str,
    epoch_id: str | None,
) -> str:
    properties: dict[str, object] = {"scope_key": SCOPE, "fact": fact, "predicate": "resides_in"}
    if epoch_id is not None:
        properties["epoch_id"] = epoch_id
    return store.add_relationship(
        source_uuid=source_uuid,
        target_uuid=target_uuid,
        relationship_type="MEMORY",
        properties=properties,
    ).uuid


def _uuids_from_both_reads(store: StorageBackend, node_uuids: list[str]) -> tuple[set[str], set[str]]:
    scoped = {r.uuid for r in store.relationships_for_scope(SCOPE)}
    hopped = {r.uuid for r in store.relationships_for_node_uuids(scope_key=SCOPE, node_uuids=node_uuids)}
    return scoped, hopped


class TestEpochReadParity:
    def test_entity_hop_read_excludes_rows_outside_the_active_epoch_ancestry(self, backend: StorageBackend) -> None:
        """The T0-10 regression itself: the hop read must not leak a foreign epoch."""
        backend.ensure_root_epoch(SCOPE, now=datetime.now(UTC))
        source_uuid, target_uuid = _endpoints(backend)
        base = _add_fact(backend, source_uuid, target_uuid, fact="Alice lives in Seattle", epoch_id=None)
        _add_fact(
            backend,
            source_uuid,
            target_uuid,
            fact="Alice lives in Denver",
            epoch_id="epoch-never-adopted",
        )

        scoped, hopped = _uuids_from_both_reads(backend, [source_uuid, target_uuid])

        assert scoped == {base}, "scoped read must hide the un-adopted branch"
        assert hopped == {base}, (
            "entity-hop read leaked a row outside the active-epoch ancestry -- "
            "this is T0-10: retrieval expansion re-admits rolled-back memory"
        )

    def test_the_two_reads_agree_on_an_epoch_inside_the_ancestry(self, backend: StorageBackend) -> None:
        """A row on the active epoch, or any ancestor, is visible to both reads."""
        now = datetime.now(UTC)
        root = backend.ensure_root_epoch(SCOPE, now=now)
        child = backend.create_epoch(scope_key=SCOPE, parent_epoch_id=root, now=now)
        backend.set_active_epoch(SCOPE, child, now=now)
        source_uuid, target_uuid = _endpoints(backend)
        on_root = _add_fact(backend, source_uuid, target_uuid, fact="root layer", epoch_id=root)
        on_child = _add_fact(backend, source_uuid, target_uuid, fact="child layer", epoch_id=child)
        _add_fact(backend, source_uuid, target_uuid, fact="sibling branch", epoch_id="epoch-sibling")

        scoped, hopped = _uuids_from_both_reads(backend, [source_uuid, target_uuid])

        assert scoped == {on_root, on_child}
        assert hopped == scoped

    def test_an_empty_epoch_id_is_the_base_layer_on_both_engines(self, backend: StorageBackend) -> None:
        """T0-3: ``epoch_id == ""`` is the base layer, and both engines must say so.

        The defect: one visibility rule, five hand-written copies, and one of them drifted by
        a token.  ``sqlite/_graph.py`` tested ``epoch_id is None`` while
        ``sqlite/_epochs.py``, both Postgres copies and the Postgres SQL all test falsiness,
        so a row with ``epoch_id == ""`` was hidden on SQLite and visible on Postgres.

        SQLite therefore disagreed with *itself* inside ``graph_state_hash`` +
        ``registry_state_digest`` -- the pair composed into the rollback signature.  Ruled
        2026-09-02: the empty string is the base layer, matching the four copies that already
        said so.

        Unreachable by first-party writes (every writer routes through ``ensure_root_epoch``,
        which mints ``epoch-<hex>``), which is why it stayed latent -- but
        ``add_relationship(properties=...)`` is a public seam with no validation, so a caller
        can produce one.  Fourth instance of this file's own shape, after T0-10, T0-4 and T0-7.
        """
        backend.ensure_root_epoch(SCOPE, now=datetime.now(UTC))
        source_uuid, target_uuid = _endpoints(backend)
        absent = _add_fact(backend, source_uuid, target_uuid, fact="no epoch key", epoch_id=None)
        empty = _add_fact(backend, source_uuid, target_uuid, fact="empty epoch id", epoch_id="")
        _add_fact(backend, source_uuid, target_uuid, fact="foreign branch", epoch_id="epoch-never-adopted")

        scoped, hopped = _uuids_from_both_reads(backend, [source_uuid, target_uuid])

        # Positive control: the foreign row must be hidden and the absent row visible on BOTH
        # engines.  Without it, a read that returned nothing -- or everything -- would satisfy
        # the real assertion below for the wrong reason.
        assert absent in scoped, "control failed: a row with no epoch_id must be the base layer"
        assert not {r for r in scoped if r not in {absent, empty}}, (
            "control failed: a foreign epoch leaked into the scoped read"
        )

        assert empty in scoped, (
            "T0-3: a relationship with epoch_id == '' was hidden by the scoped read. "
            "The empty string is the base layer -- as sqlite/_epochs.py, both postgres "
            "copies and _STATE_HASH_EPOCH_SQL already agree."
        )
        assert hopped == scoped, "the entity-hop read must agree with the scoped read"

    def test_a_scope_with_no_active_epoch_filters_nothing_on_either_read(self, backend: StorageBackend) -> None:
        """No ``ensure_root_epoch`` means pre-WS-26 behaviour, byte-for-byte, on both."""
        source_uuid, target_uuid = _endpoints(backend)
        base = _add_fact(backend, source_uuid, target_uuid, fact="untouched", epoch_id=None)
        tagged = _add_fact(backend, source_uuid, target_uuid, fact="tagged", epoch_id="epoch-whatever")

        scoped, hopped = _uuids_from_both_reads(backend, [source_uuid, target_uuid])

        assert scoped == {base, tagged}
        assert hopped == scoped
