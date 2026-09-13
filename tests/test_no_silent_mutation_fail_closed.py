"""T0-12: ``verify_no_silent_mutation`` must fail closed on an unreceipted scope.

The defect this pins: the live-vs-receipted state-hash comparison was guarded on
``latest_hash is not None``, so a scope holding live rows that **no mutating
receipt explains** skipped the comparison entirely and returned ``passed=True``.
That is precisely the condition the function exists to detect — rows written
around the receipt ledger — and the verdict is exported by
``certification_bundle``, so the bundle attested to an unverifiable scope.

The fail-open case and the genuinely-clean case are both asserted here: a guard
that simply always failed would be useless, so the empty-scope control is as
load-bearing as the regression itself.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from memotron.replay import verify_no_silent_mutation
from memotron.storage.sqlite import SQLiteStorageBackend

SCOPE = "user:alice"


@pytest.fixture
def backend() -> Iterator[SQLiteStorageBackend]:
    store = SQLiteStorageBackend(":memory:")
    try:
        yield store
    finally:
        store.close()


def _write_row_around_the_ledger(store: SQLiteStorageBackend) -> None:
    """Add a relationship straight to storage — no receipt, no run bracket.

    This is the silent mutation: the shape an operator hand-editing the database,
    or a code path that forgot to receipt, actually produces.
    """
    source, _ = store.upsert_node(
        labels=["Entity"], key=f"{SCOPE}|alice", properties={"name": "Alice", "scope_key": SCOPE}
    )
    target, _ = store.upsert_node(
        labels=["Entity"], key=f"{SCOPE}|seattle", properties={"name": "Seattle", "scope_key": SCOPE}
    )
    store.add_relationship(
        source_uuid=source.uuid,
        target_uuid=target.uuid,
        relationship_type="MEMORY",
        properties={"scope_key": SCOPE, "fact": "Alice lives in Seattle", "predicate": "resides_in"},
    )


class TestNoSilentMutationFailsClosed:
    @pytest.mark.asyncio
    async def test_live_rows_with_zero_receipts_are_reported_as_unverified(self, backend: SQLiteStorageBackend) -> None:
        """The T0-12 regression: no receipt explains this state, so it cannot pass."""
        _write_row_around_the_ledger(backend)

        report = await verify_no_silent_mutation(backend.receipts, graph=backend, scope_key=SCOPE)

        assert report.mutating_receipt_count == 0
        assert not report.passed, (
            "a scope holding live rows that no mutating receipt explains was reported "
            "as verified -- this is T0-12: the check fails open on zero receipts"
        )
        assert any("no mutating receipt explains it" in error for error in report.errors)

    @pytest.mark.asyncio
    async def test_an_empty_scope_with_zero_receipts_still_passes(self, backend: SQLiteStorageBackend) -> None:
        """The control. Zero receipts is consistent with an empty scope, and only that."""
        report = await verify_no_silent_mutation(backend.receipts, graph=backend, scope_key=SCOPE)

        assert report.mutating_receipt_count == 0
        assert report.passed, f"an empty, never-written scope must verify cleanly: {report.errors}"
        assert list(report.errors) == []
