"""Dream-run concurrency: atomic episode claims + admin single-flight.

Two UI clicks, the local platform's 15s maintenance loop, and an MCP
``memory_refresh`` are three independent writers that can race the same
durable episode queue.  These tests pin the exactly-once property: a queued
episode is extracted by exactly one run (LLM transport call count equals
episode count), a crashed run's claim expires instead of wedging the queue,
a second concurrent consolidation run cannot double-synthesize a rollup, and
the admin dream-sequence endpoint is single-flight per tenant.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from http.server import HTTPServer
from pathlib import Path
from queue import Queue
from threading import Barrier, Lock, Thread
from typing import Any
from urllib.request import Request, urlopen

import pytest

from memotron import (
    AgentMemoryPlatform,
    DreamConfig,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryScope,
    PrincipalRole,
    ScopeKind,
)
from memotron.admin_server import (
    MemoryGraphHandler,
    build_demo_control_plane,
    build_demo_principal,
)
from memotron.config import RollupConsolidationPolicy, default_config
from memotron.dreaming import DreamEngine
from memotron.extraction import RuleBasedExtractionTransport
from memotron.graph import DREAM_CLAIM_STALE_SECONDS, PropertyGraphStore
from memotron.models import RelationshipStatus


def formation_only_config() -> DreamConfig:
    base = default_config()
    return base.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="formation-default",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                ),
            )
        }
    )


class CountingExtractionTransport:
    """Delegates to the deterministic rule-based parser, counting every call."""

    def __init__(self) -> None:
        self._inner = RuleBasedExtractionTransport()
        self._lock = Lock()
        self.calls = 0

    async def extract_memories(self, request: Any) -> list[dict[str, Any]]:
        with self._lock:
            self.calls += 1
        return await self._inner.extract_memories(request)


class CoClusteringEmbeddingTransport:
    """Pairwise cosine 0.64: above the 0.6 cluster threshold, below the 0.88
    write-side dedup threshold — three distinct rows that form one cluster."""

    identifier = "test-indexed-4@v1"

    def embed(self, text: str) -> list[float]:
        normalized = text.lower()
        index = next((i for i in range(3) if f"preference {i}" in normalized), None)
        vector = [0.8, 0.0, 0.0, 0.0]
        if index is not None:
            vector[1 + index] = 0.6
        return vector


def episode_body(subject: str, obj: str) -> str:
    return json.dumps(
        {
            "memories": [
                {
                    "subject": subject,
                    "predicate": "requires",
                    "object": obj,
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ]
        }
    )


def formation_consumer_key(client: Memotron) -> str:
    job = next(job for job in client.config.jobs if job.kind == DreamJobKind.FORMATION)
    return DreamEngine._formation_consumer_key(job)


async def seed_episodes(client: Memotron, scope: MemoryScope, count: int) -> list[str]:
    uuids: list[str] = []
    for index in range(count):
        result = await client.add_episode(
            name=f"episode-{index}",
            episode_body=episode_body(f"subject-{index}", f"artifact-{index}"),
            source=EpisodeType.JSON,
            scope=scope,
        )
        uuids.append(result.episode_uuid)
    return uuids


@pytest.mark.asyncio
async def test_concurrent_double_run_extracts_each_episode_exactly_once(
    tmp_path: Path,
) -> None:
    """Two runs racing the same queue partition it: extraction call count equals
    episode count, every episode carries exactly one processing marker, and no
    SINGLE_ACTIVE slot ends up with duplicate ACTIVE rows."""
    graph_path = tmp_path / "race.sqlite"
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="race")
    seeder = Memotron(graph_path=graph_path, config=formation_only_config())
    episode_uuids = await seed_episodes(seeder, scope, 8)
    seeder.graph.close()

    barrier = Barrier(2)
    transports: list[CountingExtractionTransport] = []
    errors: list[BaseException] = []
    transports_lock = Lock()

    def run_worker() -> None:
        transport = CountingExtractionTransport()
        with transports_lock:
            transports.append(transport)
        client = Memotron(
            graph_path=graph_path,
            config=formation_only_config(),
            extraction_transport=transport,
        )
        try:
            barrier.wait(timeout=10)
            asyncio.run(client.run_dream_job(job_name="formation-default"))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)
        finally:
            client.graph.close()

    threads = [Thread(target=run_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors

    # Exactly-once: the LLM transport was called once per episode, total.
    assert sum(transport.calls for transport in transports) == len(episode_uuids)

    verifier = Memotron(graph_path=graph_path, config=formation_only_config())
    consumer = formation_consumer_key(verifier)
    for episode_uuid in episode_uuids:
        rows = verifier.graph._connection.execute(
            "SELECT COUNT(*) AS n FROM episode_processing WHERE episode_uuid = ? AND consumer_key = ?",
            (episode_uuid, consumer),
        ).fetchone()
        assert int(rows["n"]) == 1
    # No duplicate ACTIVE rows on any SINGLE_ACTIVE truth slot.
    active_truth_keys: list[str] = []
    for relationship in verifier.graph.relationships():
        props = relationship.properties
        if props.get("scope_key") != scope.key or relationship.type == "MENTIONS":
            continue
        if props.get("status") != RelationshipStatus.ACTIVE.value:
            continue
        active_truth_keys.append(str(props.get("truth_key")))
    assert len(active_truth_keys) == len(episode_uuids)
    assert len(set(active_truth_keys)) == len(active_truth_keys)
    # Claims are released on completion — the table is empty afterwards.
    remaining = verifier.graph._connection.execute("SELECT COUNT(*) AS n FROM dream_claims").fetchone()
    assert int(remaining["n"]) == 0
    verifier.graph.close()


@pytest.mark.asyncio
async def test_live_foreign_claim_blocks_and_stale_claim_is_reclaimed(
    tmp_path: Path,
) -> None:
    """Cadence race + crash recovery.  A live claim held by another run makes a
    second run a clean zero-work run (nothing checkpointed, nothing extracted);
    a claim older than the staleness window is re-claimed and processed."""
    graph_path = tmp_path / "claims.sqlite"
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="claims")
    client = Memotron(graph_path=graph_path, config=formation_only_config())
    episode_uuids = await seed_episodes(client, scope, 2)
    consumer = formation_consumer_key(client)

    # Simulate the other process winning the race: it holds live claims on the
    # whole queue (both due-checks passed; the second run must claim nothing).
    claimed = client.graph.claim_episodes(
        episode_uuids,
        consumer_key=consumer,
        run_uuid="other-process-run",
        now=datetime.now(UTC),
    )
    assert claimed == set(episode_uuids)
    checkpoints_before = len(await client.run_checkpoints())
    result = await client.run_dream_job(job_name="formation-default")
    assert result.processed_episodes == 0
    assert [run.processed_episodes for run in result.job_runs] == [0]
    # Zero-work runs do not checkpoint (existing behaviour, preserved).
    assert len(await client.run_checkpoints()) == checkpoints_before

    # Crash recovery: age the foreign claims past the staleness window; the
    # next run re-claims and processes them.
    stale_at = datetime.now(UTC) - timedelta(seconds=DREAM_CLAIM_STALE_SECONDS + 1)
    client.graph._connection.execute(
        "UPDATE dream_claims SET claimed_at = ?",
        (stale_at.isoformat(),),
    )
    client.graph._connection.commit()
    retried = await client.run_dream_job(job_name="formation-default")
    assert retried.processed_episodes == 2
    client.graph.close()


def test_claim_primitives_partition_and_release(tmp_path: Path) -> None:
    store = PropertyGraphStore(tmp_path / "store.sqlite")
    now = datetime.now(UTC)
    from memotron.models import Episode

    episode_uuids: list[str] = []
    for index in range(3):
        episode = Episode(
            name=f"e{index}",
            body="Memory: subject=a; predicate=requires; object=b; relationship_type=REQUIRES",
            source=EpisodeType.TEXT,
            scope=MemoryScope(kind=ScopeKind.USER, scope_id="claims"),
            reference_time=now,
        )
        store.add_episode(episode)
        episode_uuids.append(episode.uuid)

    first = store.claim_episodes(episode_uuids, consumer_key="formation:test", run_uuid="run-a", now=now, limit=2)
    assert len(first) == 2
    # The racing run claims exactly the remainder; re-claiming by the holder is
    # reentrant.
    second = store.claim_episodes(episode_uuids, consumer_key="formation:test", run_uuid="run-b", now=now)
    assert second == set(episode_uuids) - first
    assert store.claim_episodes(sorted(first), consumer_key="formation:test", run_uuid="run-a", now=now) == first
    # A processed marker supersedes any claim: once processed, never re-claimed.
    processed_uuid = sorted(first)[0]
    store.mark_episode_processed(processed_uuid, processed_at=now, consumer_key="formation:test")
    store.release_dream_claims("run-a")
    assert store.claim_episodes([processed_uuid], consumer_key="formation:test", run_uuid="run-c", now=now) == set()

    # Scope-work claims: held blocks, reentrant passes, release frees, stale expires.
    assert store.claim_scope_work(
        work_kind="consolidation",
        scope_key="user:claims",
        consumer_key="consolidation:test",
        run_uuid="run-a",
        now=now,
    )
    assert not store.claim_scope_work(
        work_kind="consolidation",
        scope_key="user:claims",
        consumer_key="consolidation:test",
        run_uuid="run-b",
        now=now,
    )
    assert store.claim_scope_work(
        work_kind="consolidation",
        scope_key="user:claims",
        consumer_key="consolidation:test",
        run_uuid="run-a",
        now=now,
    )
    assert store.release_dream_claims("run-a") >= 1
    assert store.claim_scope_work(
        work_kind="consolidation",
        scope_key="user:claims",
        consumer_key="consolidation:test",
        run_uuid="run-b",
        now=now,
    )
    # Expiry: a claim from a crashed run stops blocking after the window.
    later = now + timedelta(seconds=DREAM_CLAIM_STALE_SECONDS + 1)
    assert store.claim_scope_work(
        work_kind="consolidation",
        scope_key="user:claims",
        consumer_key="consolidation:test",
        run_uuid="run-c",
        now=later,
    )
    store.close()


def consolidation_config() -> DreamConfig:
    base = default_config()
    return base.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="rollups",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.6,
                        min_cluster_size=3,
                        max_depth=1,
                        cross_prefix_duplicate_threshold=None,
                    ),
                ),
            )
        }
    )


@pytest.mark.asyncio
async def test_concurrent_consolidation_synthesizes_exactly_one_rollup(
    tmp_path: Path,
) -> None:
    """Rollup synthesis is a non-idempotent create; the scope claim makes one of
    two racing consolidation runs skip, so exactly one ACTIVE ROLLUP exists."""
    graph_path = tmp_path / "rollups.sqlite"
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollups")
    seeder = Memotron(
        graph_path=graph_path,
        config=consolidation_config(),
        embedding_transport=CoClusteringEmbeddingTransport(),
    )
    for index in range(3):
        await seeder.add_memory(
            subject="Acme",
            predicate="prefers",
            object=f"operating preference {index}",
            relationship_type="PREFERS",
            scope=scope,
        )
    seeder.graph.close()

    barrier = Barrier(2)
    errors: list[BaseException] = []

    def run_worker() -> None:
        client = Memotron(
            graph_path=graph_path,
            config=consolidation_config(),
            embedding_transport=CoClusteringEmbeddingTransport(),
        )
        try:
            barrier.wait(timeout=10)
            asyncio.run(client.run_dream_job(job_name="rollups"))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion
            errors.append(exc)
        finally:
            client.graph.close()

    threads = [Thread(target=run_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors, errors

    verifier = Memotron(graph_path=graph_path, config=consolidation_config())
    rollups = [
        relationship
        for relationship in verifier.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
    ]
    assert len(rollups) == 1
    verifier.graph.close()


def read_json(target: str | Request) -> dict:
    return json.loads(urlopen(target, timeout=5).read().decode("utf-8"))


def post_json(url: str, payload: dict) -> dict:
    return read_json(
        Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    )


def stub_llm_server() -> HTTPServer:
    """OpenAI-compatible stub satisfying BOTH the extraction envelope (empty
    ``memories``) and the dream-agent decision contract, so the dream-sequence
    worker is hermetic regardless of what the process environment carries."""
    from http.server import BaseHTTPRequestHandler

    content = json.dumps({"memories": [], "approved": True, "summary": "stub approval", "details": {}})

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            body = json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_admin_dream_sequence_is_single_flight_per_tenant(tmp_path: Path) -> None:
    graph_path = tmp_path / "admin-single-flight.sqlite"
    tenant_id = "singleflight-tenant"
    default_scope = MemoryScope(kind=ScopeKind.TENANT, scope_id=tenant_id)
    server_queue: Queue = Queue()
    llm_stub = stub_llm_server()

    def serve() -> None:
        platform = AgentMemoryPlatform.create(
            graph_path=graph_path,
            project_id=tenant_id,
        )
        handler = type("SingleFlightHandler", (MemoryGraphHandler,), {})
        handler.client = platform.client
        handler.runtime_client = platform.client
        handler.platform = platform
        handler.default_scope = default_scope
        handler.graph_path = graph_path
        handler.static_dir = tmp_path / "missing-dist"
        handler.tenant_id = tenant_id
        handler.agent_ids = ()
        handler.configured_scopes = (default_scope,)
        handler.ui_url = "http://127.0.0.1:0/"
        handler.mcp_url = ""
        handler.control_plane = build_demo_control_plane(
            client=platform.client,
            default_scope=default_scope,
            tenant_id=tenant_id,
            agent_id="runtime-sf",
            extra_scopes=handler.configured_scopes,
        )
        handler.principal = build_demo_principal(
            principal_id="sf-admin",
            tenant_id=tenant_id,
            agent_id="runtime-sf",
            default_scope=default_scope,
            role=PrincipalRole.ADMIN,
            allowed_scopes=handler.configured_scopes,
        )
        server = HTTPServer(("127.0.0.1", 0), handler)
        server_queue.put((server, handler))
        try:
            server.serve_forever()
        finally:
            platform.client.graph.close()

    thread = Thread(target=serve, daemon=True)
    thread.start()
    server, handler = server_queue.get(timeout=5)
    base_url = f"http://127.0.0.1:{server.server_port}/"
    try:
        # Seal a tenant credential pointing at the local stub so the worker's
        # transports never fall back to process-environment gateway keys.
        post_json(
            f"{base_url}api/tenant-config/llm",
            {
                "provider": "openai",
                "api_key": "sk-stub-key",
                "base_url": f"http://127.0.0.1:{llm_stub.server_port}/v1",
                "model": "stub-model",
            },
        )
        post_json(
            f"{base_url}api/platform/memory/bootstrap",
            {"agent_id": "runtime-sf", "agent_name": "Single Flight Agent"},
        )
        post_json(
            f"{base_url}api/platform/memory/log",
            {"agent_id": "runtime-sf", "summary": "queue one pending episode"},
        )

        # A live run for the SAME tenant makes the POST idempotent: the caller
        # gets the ACTIVE run's run_id back with status "already_running".
        seeded = {
            "run_id": "seeded-active-run",
            "status": "running",
            "running": True,
            "tenant_id": tenant_id,
            "started_at": datetime.now(UTC).isoformat(),
            "pending_episode_count": 1,
            "events": [],
        }
        with handler.dream_status_lock:
            handler.dream_statuses["seeded-active-run"] = seeded
        deduped = post_json(f"{base_url}api/dream-sequence/run", {"scope": "agent:runtime-sf"})
        assert deduped["status"] == "already_running"
        assert deduped["run_id"] == "seeded-active-run"
        assert "pending_episode_count" in deduped

        # A live run for ANOTHER tenant never blocks this tenant.
        with handler.dream_status_lock:
            handler.dream_statuses["seeded-active-run"]["running"] = False
            handler.dream_statuses["other-tenant-run"] = {
                "run_id": "other-tenant-run",
                "status": "running",
                "running": True,
                "tenant_id": "some-other-tenant",
                "started_at": datetime.now(UTC).isoformat(),
                "events": [],
            }
        first = post_json(f"{base_url}api/dream-sequence/run", {"scope": "agent:runtime-sf"})
        assert first["status"] in {"queued", "running"}
        assert first["run_id"] not in {"seeded-active-run", "other-tenant-run"}
        assert isinstance(first["pending_episode_count"], int)
        for _ in range(100):
            status = read_json(f"{base_url}api/dream-sequence/status?run_id={first['run_id']}")
            if not status["running"]:
                break
            time.sleep(0.05)
        assert status["status"] == "completed"
        # "ran, nothing to do" vs "ran, processed N" is explicit on the payload.
        assert isinstance(status["processed_episodes"], int)
        assert status["no_work"] == (status["processed_episodes"] == 0)
        assert isinstance(status["pending_episode_count"], int)

        # Sequential POSTs after completion start fresh runs.
        second = post_json(f"{base_url}api/dream-sequence/run", {"scope": "agent:runtime-sf"})
        assert second["run_id"] != first["run_id"]
        assert second["status"] in {"queued", "running", "completed"}
        for _ in range(100):
            final = read_json(f"{base_url}api/dream-sequence/status?run_id={second['run_id']}")
            if not final["running"]:
                break
            time.sleep(0.05)
        assert final["status"] == "completed"
    finally:
        with handler.dream_status_lock:
            handler.dream_statuses.pop("seeded-active-run", None)
            handler.dream_statuses.pop("other-tenant-run", None)
        server.shutdown()
        llm_stub.shutdown()
