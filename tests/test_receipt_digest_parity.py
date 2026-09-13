"""`latest_formation_receipt_digest` must work on the engine that runs in production. (#164)

The defect: the method is defined once on the SQLite ledger (`storage/receipts.py:1178`) and
`PostgresReceiptLedger` defines no override, so the inherited body carried SQLite SQL into a
psycopg cursor. Reproduced 2026-09-03, identical call on both engines:

    sqlite   -> returned None
    postgres -> ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%''

Two SQLite-isms in one statement, and the SECOND is the one that actually raises:

    ?                        psycopg needs %s
    LIKE 'formation_%'       the bare % is parsed as a placeholder; it needs %%

Live caller: `agent_memory/_publish.py:425`, the promotion lineage path, which reads the source
fact's formation digest when promoting a memory into project scope.

Why it survived: the only coverage was `tests/test_promotion.py:147`, SQLite-only. Same shape as
#149 -- a method exercised on the engine that will never run in production and not on the one
that will. This file is the parity coverage that was missing.

Checked while fixing: `PostgresReceiptLedger` inherits exactly three methods
(`_enforce_content_free_reason`, `bind_content_protection`, and this one). The other two carry no
SQL, so this is an instance and not a class -- recorded so nobody re-audits it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from memotron.storage.base import StorageBackend
from memotron.storage.sqlite import SQLiteStorageBackend


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


class TestTheQueryRunsOnBothEngines:
    def test_an_empty_ledger_returns_none_rather_than_raising(self, backend: StorageBackend) -> None:
        """THE REGRESSION, and an empty ledger is enough to prove it.

        The failure is at SQL *parse* time -- psycopg rejects the statement before any row is
        read -- so no fixture data is needed to expose it. On the broken code this raises
        ProgrammingError on postgres and returns None on sqlite.
        """
        assert backend.receipts.latest_formation_receipt_digest("no-such-relationship") is None

    def test_a_blank_uuid_is_refused_the_same_way_on_both(self, backend: StorageBackend) -> None:
        """The guard is engine-independent and must stay that way after an override."""
        with pytest.raises(ValueError, match="non-blank"):
            backend.receipts.latest_formation_receipt_digest("   ")

    def test_the_like_wildcard_actually_MATCHES(self, backend: StorageBackend) -> None:
        """The `%` in `LIKE 'formation_%'` is load-bearing, and this asserts it POSITIVELY.

        An earlier draft of this test only checked that an absent uuid returns None -- which
        passes whether or not the LIKE works at all. Escaping for psycopg (`%%`) is easy to get
        wrong in the other direction: a literal `%%` reaching SQL matches nothing, and this
        method would then silently always return None, reading exactly like "no formation
        receipt". A negative-only assertion cannot tell those apart.

        So: emit a formation receipt AND a non-formation one on the same relationship, and
        assert the formation digest comes back and the pruning one does not.
        """
        run = backend.receipts.begin_run(
            run_kind="formation",
            job_name="formation-default",
            scope_key="tenant:acme",
            effective_policy_digest="policy-digest",
        )
        backend.receipts.emit(
            run,
            decision_type="pruning_relationship_pruned",
            decision_reason="not a formation receipt",
            decision_result="pruned",
            relationship_uuid="REL-1",
        )
        formation = backend.receipts.emit(
            run,
            decision_type="formation_entity_linked",
            decision_reason="the one this method must find",
            decision_result="materialized",
            relationship_uuid="REL-1",
        )

        found = backend.receipts.latest_formation_receipt_digest("REL-1")

        assert found == formation.payload_digest, "the LIKE matched nothing -- %% reached SQL literally"
        assert backend.receipts.latest_formation_receipt_digest("REL-NOPE") is None
