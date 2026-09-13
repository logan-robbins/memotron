"""A failed ANN index must not disable the pgvector path.

The two DDL statements used to share one transaction. The index is an optimization that
CANNOT succeed against the column it indexes -- HNSW requires a fixed dimension and
`relationship_embeddings` stores `dimensions` PER ROW, because the embedding width follows
the configured transport (256 for the local hasher, 1536+ for a gateway model). So the index
raised, the transaction rolled back the column too, and `_try_enable_pgvector` returned False.

Measured on `latest` 2026-09-04, where the extension was installed and working the whole time:

    extension installed: [{'extname': 'vector', 'extversion': '0.8.5'}]
    ADD COLUMN embedding_vec vector -> OK
    CREATE hnsw INDEX               -> InvalidParameterValue: column does not have dimensions

and every pod logged `pgvector unavailable`. The message blamed the environment for our own
DDL, and someone checked the instance and correctly reported the extension present. Retrieval
silently used `dw_cosine_similarity` over `float8[]` instead of the native `<=>` operator.

These are hermetic on purpose. The Postgres lane skips without `MEMOTRON_TEST_POSTGRES_DSN`,
and `check.sh` says a green run without it "says nothing about the Postgres backend" -- so the
decision this file protects is exercised through a fake engine that runs in every lane.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Any

from memotron.storage.postgres import PostgresStorageBackend
from memotron.storage.postgres._migrations import PGVECTOR_COLUMN, PGVECTOR_INDEX


class _FakeEngine:
    """Records executed SQL and raises for whichever statements the test names."""

    def __init__(self, fail_on: tuple[str, ...] = ()) -> None:
        self.fail_on = fail_on
        self.executed: list[str] = []
        self.committed: list[str] = []
        self._pending: list[str] = []

    @contextmanager
    def transaction(self) -> Any:
        self._pending = []
        try:
            yield
        except Exception:
            # A real rollback discards the whole transaction's work, which is exactly
            # what made the index failure take the column with it.
            self._pending = []
            raise
        self.committed.extend(self._pending)
        self._pending = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.executed.append(sql)
        for needle in self.fail_on:
            if needle in sql:
                raise RuntimeError(f"simulated failure on {needle}")
        self._pending.append(sql)


def _enable(engine: _FakeEngine) -> bool:
    """Call the real method with a fake engine, bypassing __init__ (which connects)."""
    backend = object.__new__(PostgresStorageBackend)
    backend._engine = engine  # type: ignore[attr-defined]
    return PostgresStorageBackend._try_enable_pgvector(backend)


class TestTheIndexFailureMustNotDisableThePath:
    def test_a_failing_ANN_index_still_leaves_pgvector_ENABLED(self, caplog) -> None:
        """THE REGRESSION. This is the exact production shape: the column commits, the
        index raises, and the `<=>` operator is still perfectly usable."""
        engine = _FakeEngine(fail_on=("hnsw",))
        with caplog.at_level(logging.INFO, logger="memotron.storage.postgres"):
            enabled = _enable(engine)

        assert enabled is True, "an ANN index is an optimization; losing it is not losing pgvector"
        assert any("CREATE EXTENSION" in s for s in engine.committed)
        assert any("embedding_vec" in s for s in engine.committed), "the column must survive"
        assert "pgvector IS enabled" in caplog.text
        assert "pgvector unavailable" not in caplog.text, "this is what blamed the environment"

    def test_the_column_and_index_are_in_SEPARATE_transactions(self) -> None:
        """The structural fix. One transaction is what let the index roll back the column."""
        engine = _FakeEngine(fail_on=("hnsw",))
        _enable(engine)
        assert any("embedding_vec" in s for s in engine.committed)
        assert not any("hnsw" in s for s in engine.committed)

    def test_a_missing_EXTENSION_does_disable_the_path(self) -> None:
        """The control. Without it, a test asserting only the case above would pass against
        an implementation that reported pgvector enabled unconditionally."""
        engine = _FakeEngine(fail_on=("CREATE EXTENSION",))
        assert _enable(engine) is False

    def test_a_failing_COLUMN_also_disables_the_path(self) -> None:
        """The column is not optional: `<=>` has nothing to read without it."""
        engine = _FakeEngine(fail_on=("ADD COLUMN",))
        assert _enable(engine) is False

    def test_the_happy_path_commits_both(self) -> None:
        engine = _FakeEngine()
        assert _enable(engine) is True
        assert any("embedding_vec" in s for s in engine.committed)
        assert any("hnsw" in s for s in engine.committed)

    def test_the_two_constants_stayed_split(self) -> None:
        """Re-merging them would silently restore the defect and nothing else would notice.

        The index legitimately NAMES the column -- that is what it indexes. What must not
        recur is the two DDL VERBS travelling together: an `ALTER TABLE ... ADD COLUMN` in
        the same statement block as a `CREATE INDEX`, which is what shared their fate.
        """
        assert "ADD COLUMN" in PGVECTOR_COLUMN
        assert "CREATE INDEX" not in PGVECTOR_COLUMN, "the column statement must not create the index"

        assert "CREATE INDEX" in PGVECTOR_INDEX
        assert "hnsw" in PGVECTOR_INDEX
        assert "ADD COLUMN" not in PGVECTOR_INDEX, "the index statement must not add the column"


class TestTheColumnStaysDimensionless:
    def test_no_fixed_dimension_is_pinned(self) -> None:
        """`relationship_embeddings.dimensions` is per row because the embedding width follows
        the transport. A `vector(N)` column would pin every scope to one of them."""
        assert "vector;" in PGVECTOR_COLUMN.replace("\n", " ").replace("  ", " ")
        assert "vector(" not in PGVECTOR_COLUMN
