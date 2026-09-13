"""The Dream Worker skeleton: what it promises now, and what it explicitly does not.

The form factor is an open decision, so these tests pin only the obligations that hold under
ALL THREE shapes the portal lists (in-process loop, CronJob, dedicated Deployment). Nothing
here asserts where the worker is deployed, because that is not decided.

The one that matters most is containment: a worker whose cycle can raise takes itself down and
the queue stops draining, which is the failure the portal's observability page describes as
"no user-visible failure at first".
"""

from __future__ import annotations

from typing import Any

import pytest

from memotron.worker import DreamWorker


class _Run:
    def __init__(self, job_runs: int) -> None:
        self.job_runs = tuple(range(job_runs))


def _worker(behaviour: dict[str, Any], tenants: tuple[str, ...], **kw: Any) -> DreamWorker:
    calls: list[str] = []

    async def run_due_dreams(*, tenant_id: str) -> Any:
        calls.append(tenant_id)
        outcome = behaviour[tenant_id]
        if isinstance(outcome, Exception):
            raise outcome
        return _Run(outcome)

    worker = DreamWorker(run_due_dreams=run_due_dreams, tenant_ids=tenants, **kw)
    worker.calls = calls  # type: ignore[attr-defined]
    return worker


@pytest.mark.asyncio
async def test_a_cycle_runs_every_configured_tenant() -> None:
    worker = _worker({"a": 2, "b": 1}, ("a", "b"))
    report = await worker.run_cycle()
    assert worker.calls == ["a", "b"]  # type: ignore[attr-defined]
    assert report.job_runs == 3
    assert report.failed == ()


@pytest.mark.asyncio
async def test_one_tenants_failure_does_not_stop_the_others() -> None:
    """Containment is PER TENANT, not per cycle.

    One tenant's bad credential or unreachable gateway must not stop every other tenant's
    cadence. This is the weakest useful form of the portal's fair-scheduling obligation and the
    only part of it the skeleton delivers — see the module docstring, which says so.
    """
    worker = _worker({"a": RuntimeError("gateway down"), "b": 2}, ("a", "b"))

    report = await worker.run_cycle()

    assert worker.calls == ["a", "b"], "a failing tenant stopped the loop"  # type: ignore[attr-defined]
    assert report.job_runs == 2, "the healthy tenant's work was lost"
    assert [t.tenant_id for t in report.failed] == ["a"]
    assert "gateway down" in (report.failed[0].error or "")


@pytest.mark.asyncio
async def test_a_cycle_never_raises() -> None:
    """A worker whose cycle raises takes itself down and the queue silently stops draining."""
    worker = _worker({"a": RuntimeError("boom")}, ("a",))
    report = await worker.run_cycle()  # must not raise
    assert report.failed
    assert report.consecutive_failures == 1


@pytest.mark.asyncio
async def test_consecutive_failures_accumulate_and_reset() -> None:
    """The counter is what an alert would fire on; a single blip must not latch."""
    outcomes: list[Any] = [RuntimeError("1"), RuntimeError("2"), 1]
    seen: list[str] = []

    async def run_due_dreams(*, tenant_id: str) -> Any:
        seen.append(tenant_id)
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _Run(outcome)

    worker = DreamWorker(run_due_dreams=run_due_dreams, tenant_ids=("a",), interval_seconds=0)
    assert (await worker.run_cycle()).consecutive_failures == 1
    assert (await worker.run_cycle()).consecutive_failures == 2
    assert (await worker.run_cycle()).consecutive_failures == 0, "a successful cycle must clear the counter"


@pytest.mark.asyncio
async def test_run_forever_is_bounded_by_max_cycles() -> None:
    """``max_cycles`` exists so this test can exist. Production passes None."""
    worker = _worker({"a": 1}, ("a",), interval_seconds=0, jitter_fraction=0)
    reports = await worker.run_forever(max_cycles=3)
    assert len(reports) == 3
    assert worker.calls == ["a", "a", "a"]  # type: ignore[attr-defined]


def test_jitter_stays_within_its_band_and_is_never_negative() -> None:
    """Two workers started by one rollout must not wake in lockstep forever.

    The claim makes lockstep SAFE, not free: it wastes a claim attempt per cycle and makes
    ordinary contention look like a defect in logs.
    """
    worker = DreamWorker(run_due_dreams=None, tenant_ids=("a",), interval_seconds=10, jitter_fraction=0.1)
    samples = [worker.next_sleep() for _ in range(200)]
    assert all(s >= 0 for s in samples)
    assert all(9.0 <= s <= 11.0 for s in samples), "jitter escaped its band"
    assert len(set(samples)) > 1, "jitter produced a constant — two workers would stay in lockstep"


def test_zero_interval_cannot_produce_a_negative_sleep() -> None:
    worker = DreamWorker(run_due_dreams=None, tenant_ids=("a",), interval_seconds=0, jitter_fraction=0.5)
    assert all(worker.next_sleep() >= 0 for _ in range(50))


def test_the_metric_fields_carry_what_an_alert_needs() -> None:
    """Emitting the FIELDS now makes adopting the OTEL chassis (#129) a wiring change.

    The portal's observability page already names ``memotron_episode_lag_seconds`` as the
    early warning that the worker has stopped. This does not export it — nothing exports
    anything yet — but the cycle report carries the counters an exporter would read.
    """
    from memotron.worker import CycleReport, TenantCycleResult

    report = CycleReport(
        started_at=0.0,
        duration_seconds=1.25,
        tenants=(TenantCycleResult("a", job_runs=2), TenantCycleResult("b", error="boom")),
        consecutive_failures=3,
    )
    fields = report.as_log_fields()
    assert fields["memotron_worker_job_runs"] == 2
    assert fields["memotron_worker_tenants"] == 2
    assert fields["memotron_worker_tenant_failures"] == 1
    assert fields["memotron_worker_consecutive_failures"] == 3


# --------------------------------------------------------------------- the boundary
def test_the_worker_is_decoupled_from_the_rest_of_the_package() -> None:
    """The worker imports NOTHING from memotron at module scope. Enforced, not intended.

    This is what makes it a safe place to work alone while the form factor is undecided: the
    scheduling logic depends on a CALLABLE (``run_due_dreams``), not on the client, so it can
    be reasoned about, tested and rewritten without touching — or being touched by — the
    engine, the storage layer, or the MCP surface. Every test in this file runs with no store
    and no client, which is the practical proof.

    The single coupling point is inside ``main()``, the composition root, where a real client
    is constructed. That is deliberate and allowed; a module-scope import is not, because it
    would let the boundary erode one convenience import at a time while still looking fine.

    If a future change genuinely needs a package-level import here, that is a decision worth
    making on purpose: delete this test in the same commit and say why.
    """
    import ast
    import pathlib

    import memotron.worker as worker_module

    tree = ast.parse(pathlib.Path(worker_module.__file__).read_text())
    module_scope: list[str] = []
    for node in tree.body:  # top level only — nested imports inside main() are the escape hatch
        if isinstance(node, ast.Import):
            module_scope += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module_scope.append(node.module or "")

    coupled = sorted(m for m in module_scope if m.split(".")[0] == "memotron")
    assert coupled == [], (
        f"memotron.worker gained module-scope imports from the package: {coupled}. "
        "Construct collaborators in main() and pass them in, or delete this test deliberately."
    )


def test_nothing_in_the_package_imports_the_worker() -> None:
    """Isolation in the OTHER direction, which is the half that is easy to lose.

    If the engine, client or MCP surface starts importing the worker, the worker stops being a
    leaf and changing it stops being safe. Only the entry point in pyproject may reference it.
    """
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src" / "memotron"
    importers = [
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        if path.name != "worker.py" and "memotron.worker" in path.read_text()
    ]
    assert importers == [], f"these modules import the worker, so it is no longer a leaf: {importers}"


def test_the_pending_feature_notice_cannot_be_quietly_dropped() -> None:
    """One source of the notice, referenced everywhere, asserted here.

    The CLI description and the startup banner both read ``WORKER_STATUS``, so they cannot
    drift apart. This pins the CONTENT: it must still say it is pending, still name what is
    missing, and still say the form factor is undecided. A future change that quietly softens
    it into "beta" fails here.
    """
    from memotron.worker import WORKER_STATUS

    lowered = WORKER_STATUS.lower()
    for required in ("pending", "skeleton", "not implemented", "open decision", "not production-ready"):
        assert required in lowered, f"the pending notice no longer says {required!r}: {WORKER_STATUS!r}"


def test_it_REFUSES_to_run_without_real_extraction(
    caplog: pytest.LogCaptureFixture, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No credential must be a loud refusal, not a quiet no-op.

    `build_transports_from_tenant_graph` falls back to `RuleBasedExtractionTransport`
    when no per-tenant credential is sealed and no key is in the environment. It raises
    nothing and logs nothing, so before this guard a renamed Vault key produced a worker
    that started cleanly, reported `cycle complete` every time, and formed nothing —
    which is precisely the failure the transport wiring was added to fix (3 of 694 runs
    did any work). A fix whose own failure mode is the original bug is not a fix.

    The banner is asserted too: it is emitted before the refusal, so an operator sees
    what this component is on the failing path and not only on the healthy one.
    """
    import logging

    from memotron.worker import WORKER_STATUS, main

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    graph = tmp_path / "no-credential.sqlite"
    with caplog.at_level(logging.INFO, logger="memotron.worker"):
        rc = main(["--once", "--graph-path", str(graph), "--tenants", "unfunded-tenant"])

    assert rc == 2, f"the worker ran without a real extraction transport (rc={rc})"
    messages = [r.getMessage() for r in caplog.records]
    assert any("RuleBasedExtractionTransport" in m for m in messages), (
        f"the refusal must NAME the transport it resolved, or the operator cannot tell "
        f"this apart from any other startup failure; saw {messages}"
    )
    assert any("LITELLM_API_KEY" in m for m in messages), (
        "the refusal must say what to set; a diagnosis without a remedy sends the reader back to the source"
    )
    assert any(WORKER_STATUS in m for m in messages), (
        "the pending-feature banner must still be emitted on the failing path"
    )


def test_the_notice_is_emitted_at_warning_on_a_real_start(
    caplog: pytest.LogCaptureFixture, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator scanning logs must see it without looking for it.

    Asserted on a REAL run against a real store, at WARNING specifically: demoting it to INFO
    would bury it among ordinary cycle lines, which is the failure this exists to prevent.

    An earlier version of this test asserted the banner was ABSENT for a mis-invocation, which
    pinned the wrong thing entirely -- it would have passed with the banner deleted.
    """
    import logging

    from memotron.worker import WORKER_STATUS, main

    graph = tmp_path / "banner.sqlite"
    # A credential is now REQUIRED for the worker to start: without one, extraction
    # resolves to the rule-based transport and `main` refuses with rc 2 rather than
    # running a cycle that forms nothing. Constructing the transport performs no I/O, and
    # this run has no episodes, so nothing is ever sent anywhere.
    monkeypatch.setenv("LITELLM_API_KEY", "test-key-not-used-no-episodes-exist")
    with caplog.at_level(logging.WARNING, logger="memotron.worker"):
        rc = main(["--once", "--graph-path", str(graph), "--tenants", "banner-tenant"])

    assert rc == 0, "the run itself failed, so this proves nothing about the banner"
    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any(WORKER_STATUS in w for w in warnings), (
        f"the pending-feature notice was not emitted at WARNING on a real start; saw {warnings}"
    )
