"""Record which storage methods each test drives, and how many times.

Loaded from ``tests/conftest.py``, so it rides the tests lane and costs no second
run. Writes ``.storage_profile.tsv`` (gitignored);
``scripts/verify/storage_golden.py`` compares it to the committed golden.

Why a PROFILE and not a sequence
--------------------------------
The receipt golden records an ordered stream because there are 3,539 receipts. The
suite makes **155,896 storage calls across 147 distinct methods** — measured — so an
ordered stream would be a 156,000-line file nobody reads and every reordering would
redden it.

A per-test profile — the multiset ``{method: count}`` — is 643 rows and catches the
thing that actually matters for a refactor: **a moved body that gained or lost a
storage call.** It is deliberately blind to ordering, which is the right trade here:
re-ordering two independent reads is not a defect, and dropping one is.

What it covers that ``pure_move`` does not
------------------------------------------
``pure_move`` proves a function's bytecode is unchanged, which is a stronger
statement — but only for a genuinely pure move. Every split so far has needed at
least one deliberate body edit (a class-qualified `Composed.method` rewritten to the
mixin that now owns it, in `_entities`, `_registry` and `_prompts`), and those are
exactly the edits `pure_move` stops covering. This is the guard for them: the rewrite
is object-identical if and only if the storage profile is unchanged.

Measured before relying on it (2026-08-29): 643 tests with storage activity, and
**zero profiles differed across two runs**.

Corrected 2026-08-29, later the same day: two runs was not enough to justify "zero".
At 695 rows, ONE row does drift --
``test_dream_concurrency.py::test_concurrent_consolidation_synthesizes_exactly_one_rollup``,
seen once in five full-suite runs with all six of its counters up by exactly 1. It is
a concurrency test, so an extra contention retry is legitimate behaviour rather than a
defect. ``storage_golden.KNOWN_COUNT_DRIFT`` forgives count drift on that row alone and
only while its method SET is unchanged; every other row is still exact. The general
lesson is worth more than the row: a determinism claim measured over two runs is a
claim about two runs.
"""

from __future__ import annotations

import collections
import pathlib
from typing import Any

#: Written at session end, read by scripts/verify/storage_golden.py. Gitignored --
#: the golden is the committed artifact, this is the observation.
PROFILE_PATH = pathlib.Path(".storage_profile.tsv")

_CURRENT: collections.Counter[str] = collections.Counter()
_ROWS: dict[str, dict[str, int]] = {}
_INSTALLED = False


def install() -> None:
    """Wrap every StorageBackend method on the SQLite backend.

    The ABC is the source of the name list, not ``dir()`` on the concrete class, so a
    private helper that happens to exist on the implementation is not counted -- the
    profile tracks the CONTRACT surface, which is what a refactor must preserve.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    from memotron.storage.base import MemoryGraphStorage, OperationalStorage
    from memotron.storage.sqlite import SQLiteStorageBackend

    contract = {
        name
        for base in (MemoryGraphStorage, OperationalStorage)
        for name in vars(base)
        if not name.startswith("__") and callable(getattr(base, name, None))
    }
    for name in sorted(contract):
        original = getattr(SQLiteStorageBackend, name, None)
        if not callable(original):
            continue

        def wrap(method_name: str, fn: Any) -> Any:
            def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
                _CURRENT[method_name] += 1
                return fn(self, *args, **kwargs)

            return counted

        try:
            setattr(SQLiteStorageBackend, name, wrap(name, original))
        except (AttributeError, TypeError):  # pragma: no cover - read-only slots
            continue
    _INSTALLED = True


_SKIPPED: set[str] = set()


def record_setup() -> None:
    _CURRENT.clear()


def record_teardown(nodeid: str) -> None:
    if _CURRENT:
        _ROWS[nodeid] = dict(sorted(_CURRENT.items()))
    _CURRENT.clear()


def mark_skipped(nodeid: str) -> None:
    """A SKIPPED test must not leave a profile row. See #139.

    `pytest_runtest_teardown` fires for skipped tests too, and a skipped Postgres test still
    tears its fixture down, so `close` got counted and the test acquired a `close:1` profile.
    57 rows in the golden were stubs of exactly that shape -- and they could never be anything
    else, because when those tests genuinely RUN they drive the Postgres backend, which this
    profiler does not instrument, so they produce no row at all. Present-but-meaningless or
    absent: a row that cannot carry signal, which is the shape this branch keeps finding.

    Filtering on the SKIP rather than on "the profile is only `close`" is deliberate: six
    non-Postgres tests are legitimately close-only (`test_default_settings_build_one_sqlite_backend`
    and friends build a backend and close it, and "this test only opens and closes" is a real
    fact about them worth pinning). A close-only filter would have deleted those six.
    """
    _SKIPPED.add(nodeid)


def write(clean: bool) -> None:
    """One row per test: nodeid, total calls, then method:count pairs.

    Counts are written inline rather than digested so a failure explains itself --
    a digest would say "this test changed" and leave the reader to re-derive what.
    """
    # Drop skipped tests here rather than in the teardown hook: the skip verdict and the
    # teardown fire in an order this module should not have to depend on, and sessionfinish
    # is after both.
    rows = {nodeid: profile for nodeid, profile in _ROWS.items() if nodeid not in _SKIPPED}
    lines = [f"# clean={'yes' if clean else 'no'} tests={len(rows)}"]
    for nodeid, profile in sorted(rows.items()):
        pairs = ",".join(f"{m}:{c}" for m, c in profile.items())
        lines.append(f"{nodeid}\t{sum(profile.values())}\t{pairs}")
    PROFILE_PATH.write_text("\n".join(lines) + "\n")
