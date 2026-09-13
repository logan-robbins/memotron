"""The Dream Worker — a process that runs due dream jobs with no client involved.

    *** PENDING FEATURE — SKELETON, NOT PRODUCTION-READY ***

Status: PENDING / SKELETON. It runs, it is safe to deploy to a lower environment, and it deliberately
does not decide the thing that is still open.

Why this exists
---------------
Dream jobs run only when a client asks: MCP ``memory_refresh``, the admin
``POST /api/dream-sequence/run``, or the local dev loop. In a deployed environment nothing
asks, so episodes queue with ``queued_for_dreaming=True`` and stay queued — memory is written
and never formed, consolidated, or pruned. Everything needed to *do* the work already exists
and is tested; the missing piece is a process that calls it on a clock.

The form factor is an OPEN DECISION, and this does not close it
---------------------------------------------------------------
The portal records three viable shapes — an in-process loop in the API pods, a Kubernetes
CronJob, or a dedicated worker Deployment — and leans toward the Deployment because dream load
must scale independently of retrieval traffic. The written decision belongs to the epic's
scheduler issue, not to this module.

So the scheduling SHELL is swappable and the loop is not:

    in-process loop     ``DreamWorker(run_due_dreams=..., tenant_ids=...).run_cycle()``,
                        awaited from whatever loop you already have
    CronJob             ``memotron-dream-worker --once``     (one pass, then exit)
    Deployment/sidecar  ``memotron-dream-worker``            (resident, jittered loop)

All three call the same :meth:`DreamWorker.run_cycle`. Whichever the epic picks, this module does not
change — only what invokes it does. That is the point of shipping a skeleton now.

Obligations, and which are real here
------------------------------------
The portal fixes five obligations regardless of form factor. Three are already provided by the
engine, one is implemented here, and one is NOT — stated rather than implied:

  Exclusive claims      PROVIDED. ``claim_scope_work``/``claim_episodes`` are row locks in the
                        store, and the worker does not reimplement them. Verified 2026-08-31
                        across two real PROCESSES on both engines
                        (``tests/test_exclusive_write_conformance.py``).
  Judged maintenance    PROVIDED. ``run_due_dreams`` already routes every action through the
                        dream-agent transport on the tenant's key.
  Visible history       PROVIDED. Run records and decisions are already persisted and exposed
                        by the Management API.
  Bounded failure       IMPLEMENTED HERE. Per-cycle exception containment, jittered sleep, and
                        a consecutive-failure counter that surfaces in the cycle report.
  Fair scheduling       **NOT IMPLEMENTED.** Serving due jobs across tenants honouring quotas
                        needs a quota concept, and there is none in ``src/`` or ``.helm/``.
                        This iterates the configured tenants in order. With one tenant — every
                        lower environment today — order is not a fairness question. Do not
                        deploy this multi-tenant and assume fairness.

Not shipped, and owned elsewhere: quotas, the dead-letter queue, retry budgets per episode,
webhooks, and the episode-status API.

What gates PRODUCTION, and what does not
----------------------------------------
The portal gates the worker on the persistence foundation, because at production scale claims
must be Postgres row locks. That gate is real and this module does not dodge it.

It does NOT gate a lower environment. The claim contract holds on SQLite — proven across two
processes — so dev/test/stage can run this against the existing store today and get episodes
actually forming, which is the thing currently missing.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import random
import time
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger("memotron.worker")

#: The single source of the pending-feature notice. Referenced by the CLI description, printed
#: at WARNING on every start, and asserted by tests -- so the three cannot drift apart and the
#: marker cannot be dropped from one of them silently.
#:
#: Loud on purpose. An operator who runs this must know, without reading the source, that fair
#: scheduling and quotas are absent and that the deployment form factor is still an open
#: decision owned by the epic's scheduler issue.
WORKER_STATUS = (
    "PENDING FEATURE — this Dream Worker is a SKELETON. Fair scheduling and per-tenant quotas "
    "are NOT implemented; the deployment form factor is an OPEN decision (in-process loop / "
    "CronJob / dedicated Deployment) owned by the epic's scheduler issue. Safe for dev, test "
    "and stage. NOT production-ready: the portal gates production on the persistence "
    "foundation, because at scale claims must be Postgres row locks."
)

#: Default seconds between cycles. Deliberately not 1 (the library default for a job cadence):
#: this is how often the worker WAKES, not how often a job is due. `due_jobs` still decides.
DEFAULT_INTERVAL_SECONDS = 60.0

#: Jitter as a fraction of the interval. Two workers started by the same rollout must not wake
#: in lockstep forever; the claim makes that safe, but lockstep wastes a claim attempt every
#: cycle and makes contention look like a defect in logs.
JITTER_FRACTION = 0.1


@dataclass(frozen=True)
class TenantCycleResult:
    tenant_id: str
    job_runs: int = 0
    error: str | None = None


@dataclass(frozen=True)
class CycleReport:
    """One pass over every configured tenant. Every field is loggable and assertable."""

    started_at: float
    duration_seconds: float
    tenants: tuple[TenantCycleResult, ...] = ()
    consecutive_failures: int = 0

    @property
    def job_runs(self) -> int:
        return sum(t.job_runs for t in self.tenants)

    @property
    def failed(self) -> tuple[TenantCycleResult, ...]:
        return tuple(t for t in self.tenants if t.error is not None)

    def as_log_fields(self) -> dict[str, Any]:
        """The shape an exporter would publish.

        Named for the metric the portal's observability page already specifies —
        ``memotron_episode_lag_seconds`` is the early warning that the worker has stopped,
        and its silence is why that row exists. Emitting the FIELDS now means adopting the org
        OTEL chassis later (#129) is a wiring change rather than a design one.
        """
        return {
            "memotron_worker_cycle_seconds": round(self.duration_seconds, 3),
            "memotron_worker_job_runs": self.job_runs,
            "memotron_worker_tenants": len(self.tenants),
            "memotron_worker_tenant_failures": len(self.failed),
            "memotron_worker_consecutive_failures": self.consecutive_failures,
        }


@dataclass
class DreamWorker:
    """Runs due dream jobs for a fixed set of tenants.

    ``run_due_dreams`` is supplied rather than constructed so a caller can hand in a real
    client, a platform, or a fake. The worker's own logic — containment, jitter, the failure
    counter — is then testable without a store, which is what makes the skeleton assertable
    before the form factor is decided.
    """

    run_due_dreams: Any
    tenant_ids: tuple[str, ...]
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS
    jitter_fraction: float = JITTER_FRACTION
    _consecutive_failures: int = field(default=0, init=False)

    async def run_cycle(self) -> CycleReport:
        """One pass. Never raises: a cycle that dies takes the worker with it.

        Failure is contained PER TENANT, not per cycle, so one tenant's bad credential or
        unreachable gateway cannot stop every other tenant's cadence. That is the weakest
        useful form of the portal's fair-scheduling obligation and the only part of it this
        skeleton honestly delivers.
        """
        started = time.monotonic()
        results: list[TenantCycleResult] = []
        for tenant_id in self.tenant_ids:
            try:
                run = await self.run_due_dreams(tenant_id=tenant_id)
                results.append(TenantCycleResult(tenant_id, job_runs=len(getattr(run, "job_runs", ()) or ())))
            except Exception as exc:
                _log.exception("dream cycle failed for tenant %s", tenant_id)
                results.append(TenantCycleResult(tenant_id, error=f"{type(exc).__name__}: {exc}"))

        report_failed = any(r.error for r in results)
        self._consecutive_failures = self._consecutive_failures + 1 if report_failed else 0
        report = CycleReport(
            started_at=started,
            duration_seconds=time.monotonic() - started,
            tenants=tuple(results),
            consecutive_failures=self._consecutive_failures,
        )
        _log.info("dream worker cycle complete", extra={"metrics": report.as_log_fields()})
        return report

    def next_sleep(self) -> float:
        """Interval with jitter, never negative."""
        spread = self.interval_seconds * self.jitter_fraction
        return max(0.0, self.interval_seconds + random.uniform(-spread, spread))  # noqa: S311 - not crypto

    async def run_forever(self, *, max_cycles: int | None = None) -> list[CycleReport]:
        """Resident loop. ``max_cycles`` exists so a test can bound it; production passes None."""
        reports: list[CycleReport] = []
        cycle = 0
        while max_cycles is None or cycle < max_cycles:
            reports.append(await self.run_cycle())
            cycle += 1
            if max_cycles is not None and cycle >= max_cycles:
                break
            await asyncio.sleep(self.next_sleep())
        return reports


def _tenants_from_env(explicit: str | None) -> tuple[str, ...]:
    raw = explicit or os.environ.get("MEMOTRON_WORKER_TENANTS") or os.environ.get("MEMOTRON_PROJECT_ID") or ""
    return tuple(t.strip() for t in raw.split(",") if t.strip())


#: The extraction transport that does no NLP. Matched by NAME so this module does not
#: import the extraction package just to run `--help`.
RULE_BASED_TRANSPORT = "RuleBasedExtractionTransport"


def extraction_unavailable_message(extraction_transport: object) -> str | None:
    """Return why this worker cannot form memories, or None if it can.

    `build_transports_from_tenant_graph` falls back through `build_transports_from_env`
    to `(RuleBasedExtractionTransport(), None)` when no per-tenant credential is sealed
    AND neither `LITELLM_API_KEY` nor `OPENAI_API_KEY` is set. It raises nothing and logs
    nothing, so a renamed Vault key or an unmounted secret produces a worker that starts
    cleanly, reports success every cycle, and forms nodes instead of relationships --
    which is the EXACT failure this file's transport wiring exists to close (3 runs out
    of 694 did any work). A fix whose own failure mode is the original bug is not a fix,
    so the condition is named here and the caller refuses to run.

    Checked by class name rather than `isinstance` deliberately: importing the extraction
    package at module scope would make `--help` pay for it, matching the deferred imports
    in `main()`.
    """
    name = type(extraction_transport).__name__
    if name != RULE_BASED_TRANSPORT:
        return None
    return (
        f"extraction resolved to {name}, which does no NLP and parses JSON only -- this "
        "worker would run every cycle, report success, and form nothing. No sealed "
        "per-tenant LLM credential was found for this tenant and neither LITELLM_API_KEY "
        "nor OPENAI_API_KEY is set. Seal a credential for the tenant, or set "
        "LITELLM_API_KEY (with LITELLM_API_BASE for a non-default gateway)."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"*** {WORKER_STATUS} ***\n\n{__doc__}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--once", action="store_true", help="run one cycle and exit (CronJob shape)")
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS, help="seconds between cycles")
    parser.add_argument("--graph-path", default=os.environ.get("MEMOTRON_GRAPH_PATH"))
    parser.add_argument("--tenants", default=None, help="comma-separated tenant ids")
    args = parser.parse_args(argv)

    # Imported here, not at module level, matching this file's existing convention (see the
    # `from memotron import Memotron` below): the entry point keeps `--help` cheap by
    # not pulling the package in at import time.
    from memotron.observability import configure_observability

    # Replaces a bare `logging.basicConfig(...)`. Same human-readable output by default, but
    # now honours LOG_LEVEL/LOG_JSON, redacts credentials from every line, and exports to the
    # collector when OTEL_ENABLED is set. The metrics this worker already emits
    # (`report.as_log_fields()` at :189) are what #129 wants shipped, and this is the wiring
    # that lets them leave the pod.
    configure_observability(service_name="memotron-worker")

    # Emitted HERE, before anything that can fail, and not next to the DreamWorker
    # construction where it used to live. Startup can now exit early (no tenants, no
    # store, no real extraction transport), and on every one of those paths an operator
    # reading the logs still has to be told what this component is. WARNING, not INFO:
    # it must be visible without being looked for.
    _log.warning("=" * 100)
    _log.warning("%s", WORKER_STATUS)
    _log.warning("=" * 100)

    tenant_ids = _tenants_from_env(args.tenants)
    if not tenant_ids:
        parser.error("no tenants: pass --tenants, or set MEMOTRON_WORKER_TENANTS / MEMOTRON_PROJECT_ID")
    if not args.graph_path:
        parser.error("no store: pass --graph-path or set MEMOTRON_GRAPH_PATH")

    from memotron import Memotron
    from memotron.agent_memory import agent_memory_config
    from memotron.runtime import build_transports_from_tenant_graph

    # `agent_memory_config()`, NOT the library default. Four things ride on this, and the
    # first three were invisible until someone counted what 694 runs had actually done:
    #
    #   1. THE DEFAULT CONFIG HAS NO CONSOLIDATION JOB. `default_config()` declares exactly
    #      two jobs (`config/_dream_config.py:400-403`), so this worker has never consolidated
    #      or rolled up anything, ever -- and `_eligible_scope_count` returns 0 for any job
    #      that is not consolidation (`client/_runtime.py:685-688`), which is why the
    #      `eligible_scopes: 0` in every status read was a reporting artefact and not a signal.
    #   2. BOTH DEFAULT JOBS ARE `cadence_seconds=1`, so they are permanently due and each
    #      cycle records two run rows whether or not there was work. 694 rows, 3 of which did
    #      anything.
    #   3. THE DEFAULT SCHEMA IS A SUPPORT DESK (`PREFERS`/`REQUIRES`/`SHOULD`), not the
    #      schema the app forms under -- so the worker and the api pod disagreed about what a
    #      memory even is.
    #   4. THE DEFAULT RETENTION IS MORE AGGRESSIVE THAN THE APP'S:
    #      `superseded_retention_seconds=0` against the app's 30 days, and six protected
    #      memory types against the app's two. Unfed that is invisible; fed, this worker would
    #      have retired history the application deliberately keeps.
    client = Memotron(config=agent_memory_config(), graph_path=args.graph_path)

    # Real extraction. Without this the client is built with no transports, so
    # `InstructionalExtractor(transport=None)` falls to `RuleBasedExtractionTransport` and
    # `LocalDreamAgentTransport` approves everything -- the worker ran for days and formed
    # nodes rather than relationships, which is that pair's fingerprint.
    #
    # `build_transports_from_tenant_graph` and NOT `build_transports_from_env`, because it
    # prefers a sealed per-tenant credential (which carries its own `base_url`) and falls back
    # to the environment when there is none. It is also the shape two other entry points
    # already use -- `admin_server/__init__.py` for the dream-sequence worker and
    # `agent_memory_mcp.py` -- so this does not invent a fourth wiring.
    #
    # Note a sealed credential ALONE would not have worked here -- store-wide mode calls
    # `run_due_dreams()` with no tenant id, and `transports_for_tenant(None)` returns the base
    # transports before it ever reads `tenant_llm_credentials` (`client/_runtime.py:275-276`).
    #
    # It is NOT chosen for failing loudly, because it does not: like `bind_default_tenant`
    # it falls back to `RuleBasedExtractionTransport` in silence when no credential is
    # found. An earlier version of this comment claimed otherwise and was wrong. That is
    # what `extraction_unavailable_message` below is for -- the check has to be here,
    # because no transport builder performs it.
    extraction_transport, dream_agent_transport = build_transports_from_tenant_graph(
        store=client.graph, tenant_id=tenant_ids[0]
    )
    # ALWAYS log what was resolved. A worker that reports "cycle complete" while forming
    # nothing is indistinguishable from a healthy one unless the transport is on the record.
    _log.info(
        "dream worker transports: extraction=%s dream_agent=%s",
        type(extraction_transport).__name__,
        type(dream_agent_transport).__name__ if dream_agent_transport is not None else "None",
    )
    unavailable = extraction_unavailable_message(extraction_transport)
    if unavailable is not None:
        _log.error("%s", unavailable)
        return 2
    client.set_runtime_transports(
        extraction_transport=extraction_transport, dream_agent_transport=dream_agent_transport
    )

    # Tenant-scoped runs need a control plane; a plain client has none and
    # `run_due_dreams(tenant_id=...)` raises "control_plane is required for tenant/agent/scope
    # runtime policy". Found by RUNNING this, not by reading it -- the first invocation of the
    # entry point failed on exactly that.
    #
    # So there are two honest modes, and the worker announces which one it is in rather than
    # silently falling back:
    #
    #   control plane present  -> per-tenant runs, and the tenant ids mean something
    #   no control plane       -> ONE store-wide run per cycle, and the tenant ids are labels
    #
    # The second is every lower environment today, and it is what unblocks dev/test/stage.
    # It is also the reason fair scheduling is not merely unimplemented but not yet MEANINGFUL:
    # without a control plane there is one queue, so there is nothing to be fair between.
    control_plane = getattr(client, "control_plane", None)
    store_wide = control_plane is None
    if store_wide and len(tenant_ids) > 1:
        parser.error(
            f"{len(tenant_ids)} tenants requested but this client has no control plane, so every "
            "cycle would run the same store-wide pass once per tenant. Pass one tenant, or build "
            "the client with a control plane."
        )

    async def run_due_dreams(*, tenant_id: str) -> Any:
        if store_wide:
            return await client.run_due_dreams()
        return await client.run_due_dreams(tenant_id=tenant_id)

    worker = DreamWorker(run_due_dreams=run_due_dreams, tenant_ids=tenant_ids, interval_seconds=args.interval)
    _log.info(
        "dream worker starting: scheduling=%s tenants=%s interval=%ss mode=%s",
        "store-wide (no control plane)" if store_wide else "per-tenant",
        ",".join(tenant_ids),
        args.interval,
        "once" if args.once else "resident",
    )
    try:
        if args.once:
            report = asyncio.run(worker.run_cycle())
            return 1 if report.failed else 0
        asyncio.run(worker.run_forever())
    except KeyboardInterrupt:  # pragma: no cover - operator signal
        _log.info("dream worker stopping on interrupt")
    finally:
        with contextlib.suppress(Exception):
            client.graph.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
