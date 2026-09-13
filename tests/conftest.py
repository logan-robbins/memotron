"""Suite-wide fixtures and the integration-lane gates.

Integration markers
-------------------
Two lanes need external state and are therefore not hermetic: ``postgres``
(a live Operational Store) and ``corpus`` (the official evaluation corpus).
They are *different* axes with different prerequisites, so they are two markers
rather than one ``integration`` marker -- collapsing them would make the CI
lanes unselectable.

The marker exists for selection (``-m postgres`` in CI); the environment check
below exists for safety, so a marked test can never reach a database that is not
there.  Marking is deliberately fine-grained: in the parity suites the engine is
a *fixture parameter*, so the marker rides on the ``postgres`` param and the
``sqlite`` twin of each assertion keeps running.

Marking at collection time also means ``pytest -m postgres --collect-only``
answers "how many Postgres tests exist" without a database -- which the previous
per-file ``os.environ`` checks could not do, and which the CI Postgres-lane
wiring work needs.

Ephemeral KEK opt-in
--------------------
``PostgresStorageBackend`` fails closed when constructed without a key manager,
because an ephemeral KEK is random per process: content sealed by one process is
unreadable after any restart and by every other replica, and the failure
surfaces late, as ``wrapped DEK failed authentication``, while tenant status
still reports the credential as configured.  A deployment must therefore supply
a real key manager, and the backend refuses rather than shredding quietly.

This suite is the legitimate exception.  Every Postgres test builds a throwaway
database, seals into it, asserts, and drops it inside one process — the ephemeral
key is exactly right there, and threading a KMS-backed manager through would
test the mock rather than the storage contract.  Declaring the opt-out once, for
the whole suite, keeps that decision in a single reviewable place instead of
scattered across every file that opens a backend.

Two tests deliberately override this to exercise the guard itself; see
``tests/test_operational_store_wiring.py``.

Offline by default
------------------
The suite must not reach a real LLM, and until 2026-08-31 that was enforced by
each test scrubbing provider credentials for itself.  A hand-maintained list per
test rots per test: ``test_adoption.py`` deletes ``ANTHROPIC_API_KEY`` and
``OPENAI_API_KEY`` in one test and all three in another, and the one it misses is
``LITELLM_API_KEY`` -- which :func:`memotron.runtime._env_endpoint` prefers
over both.  Set it, and ``test_project_mode_switch_changes_durable_memory_owner``
makes a real gateway call: off VPN it blocks for the full 300s extraction
timeout, and on VPN it silently bills a nondeterministic model call inside the
lane the whole verification loop treats as its deterministic baseline.

This is T1-14's twin.  That defect closed the ON-DISK route -- ``load_env_file``
no longer defaults to a cwd-relative ``.env`` -- for exactly this reason,
"promoting deterministic rule-based extraction to a gateway-billed call".  The
PROCESS-ENVIRONMENT route stayed open, and it is the one ``set -a; . ./.env``
uses, which is what the repo's own ``.env`` header tells a developer to run.

So the scrub is declared once, here, and :mod:`tests.test_offline_by_default`
derives the variable names from the resolver's own source so that adding a third
provider fails the guard rather than silently widening the hole.
"""

from __future__ import annotations

import os

import pytest

# Plain import: tests/ is not a package (no __init__.py), and pytest puts a
# conftest's own directory on sys.path.
import receipt_stream
import storage_profile
from memotron.gateway import GATEWAY_API_KEY_ENV

#: Provider credentials scrubbed from every test's environment. ``GATEWAY_API_KEY_ENV``
#: is imported rather than spelled, so renaming it in the product cannot leave a stale
#: string here. ``ANTHROPIC_API_KEY`` is not read by ``_env_endpoint`` today and is
#: scrubbed anyway: it costs nothing and it is what the transports would reach for next.
CREDENTIAL_ENVS: frozenset[str] = frozenset({GATEWAY_API_KEY_ENV, "OPENAI_API_KEY", "ANTHROPIC_API_KEY"})

ALLOW_EPHEMERAL_KEK_ENV = "MEMOTRON_ALLOW_EPHEMERAL_KEK"
POSTGRES_DSN_ENV = "MEMOTRON_TEST_POSTGRES_DSN"
PUBLIC_CORPUS_DIR_ENV = "MEMOTRON_PUBLIC_CORPUS_DIR"

# marker -> (env var, what it needs)
_INTEGRATION_GATES: tuple[tuple[str, str, str], ...] = (
    ("postgres", POSTGRES_DSN_ENV, "needs a live Postgres"),
    ("corpus", PUBLIC_CORPUS_DIR_ENV, "needs the official evaluation corpus"),
)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip integration-marked tests whose environment is not configured.

    Belt to the per-test ``os.environ`` checks' braces: those return the DSN the
    test needs, so they stay.  This makes the *marker itself* sufficient, so a
    newly added Postgres test that carries the marker but forgets the helper
    fails to connect only when a DSN really is present, rather than producing a
    confusing connection error on a laptop.
    """
    for marker, env, why in _INTEGRATION_GATES:
        if os.environ.get(env, "").strip():
            continue
        skip = pytest.mark.skip(reason=f"set {env} to run: {why}")
        for item in items:
            if marker in item.keywords:
                item.add_marker(skip)


@pytest.fixture
def postgres_dsn() -> str:
    """The live Postgres DSN, or skip. Shared replacement for the per-file copies."""
    dsn = os.environ.get(POSTGRES_DSN_ENV, "").strip()
    if not dsn:
        pytest.skip(f"set {POSTGRES_DSN_ENV} to run Postgres integration tests")
    return dsn


@pytest.fixture(autouse=True)
def _offline_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove provider credentials so no test can reach a real LLM by accident.

    ``monkeypatch`` restores the outer value at teardown, so a developer's shell
    keeps its key and only the test process is blind to it.  ``raising=False``
    because the common case is that none of them is set.

    This runs before the test body, so the many tests that exercise credential
    RESOLUTION by calling ``monkeypatch.setenv("LITELLM_API_KEY", "sk-...")``
    are unaffected -- they set their own value on top of the scrub, which is the
    behaviour they were always relying on when the ambient environment happened
    to be empty.  What changes is that the empty ambient environment is now a
    guarantee rather than a coincidence.
    """
    for name in sorted(CREDENTIAL_ENVS):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _allow_ephemeral_kek_in_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opt the suite into the ephemeral KEK, unless a test opted out first.

    ``monkeypatch`` so the value never leaks past the test that used it, and
    ``setdefault`` semantics so an outer environment that already set it — a
    developer running one file, or the local docker-compose stack — is left
    alone.
    """
    if not os.environ.get(ALLOW_EPHEMERAL_KEK_ENV, "").strip():
        monkeypatch.setenv(ALLOW_EPHEMERAL_KEK_ENV, "1")


@pytest.fixture(autouse=True)
def _no_ambient_kek(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scrub the durable-KEK variables, like the provider credentials above.

    ``create_storage_backend`` resolves these, and almost every test in the suite
    builds a store through it -- so a developer with ``MEMOTRON_KEK_FILE``
    exported in their shell (the exact configuration this feature asks operators
    to adopt) would run a materially different suite from CI, and the tests that
    set their own value would fail with "both ... are set". Make the empty
    ambient environment a guarantee rather than a coincidence; tests that want a
    KEK set one explicitly on top of this.
    """
    for name in ("MEMOTRON_KEK_B64", "MEMOTRON_KEK_FILE"):
        monkeypatch.delenv(name, raising=False)


# --------------------------------------------------------------- receipt stream
# Loaded here rather than as a -p plugin so it is always on and costs no second
# suite run. See tests/receipt_stream.py for what it records and why.


def pytest_configure(config: pytest.Config) -> None:
    receipt_stream.install()
    storage_profile.install()


def pytest_runtest_setup(item: pytest.Item) -> None:
    receipt_stream.record_setup()
    storage_profile.record_setup()


def pytest_runtest_teardown(item: pytest.Item) -> None:
    receipt_stream.record_teardown(item.nodeid)
    storage_profile.record_teardown(item.nodeid)


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    # #139: a SKIPPED test must not leave a storage-profile row. `wasxfail` is excluded
    # deliberately -- pytest reports an xfailed test as `skipped`, but an xfail DID run and
    # its profile is real evidence (the strict xfail in test_stateful_certification.py drives
    # a full certification replay). Filtering on `report.skipped` alone would delete it.
    if report.skipped and not hasattr(report, "wasxfail"):
        storage_profile.mark_skipped(report.nodeid)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    # Always write. Whether the run was FILTERED is deliberately not guessed from
    # the options -- check.sh passes `-m "not postgres and not corpus"`, which
    # deselects exactly the tests conftest would have skipped anyway, so an
    # option-based check would call the normal CI run partial. The comparison
    # handles subsets instead; see scripts/verify/receipt_golden.py.
    receipt_stream.write(clean=not session.testsfailed)
    storage_profile.write(clean=not session.testsfailed)
