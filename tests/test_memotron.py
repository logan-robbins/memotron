from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from threading import Thread
from typing import Any

import pytest
from pydantic import ValidationError

from memotron import (
    ConversationTurn,
    DreamAgentConfig,
    DreamAgentDecisionRequest,
    DreamConfig,
    DreamContextPolicy,
    DreamEpisodeFilter,
    DreamJob,
    DreamJobKind,
    DreamPromptOverride,
    Memotron,
    EpisodeType,
    MemoryIngestionMode,
    MemoryScope,
    OpenAICompatibleDreamAgentTransport,
    OpenAICompatibleExtractionTransport,
    ProfilePolicy,
    PruningPolicy,
    RelationshipCardinality,
    ScopeKind,
    SessionIngester,
)
from memotron.config import default_config
from memotron.graph import normalize_key
from memotron.models import OutcomeVerdict, RelationshipStatus, UseEventKind


def new_client(tmp_path: Path) -> Memotron:
    return Memotron(graph_path=tmp_path / "memotron.sqlite")


def strict_properties_config() -> DreamConfig:
    """``default_config()`` with the Entity allow-list made STRICT again.

    T0-11 changed the default to non-strict, because leaving it strict meant a real
    extractor's one invented property key quarantined the whole candidate and
    ``default_config()`` formed nothing. The strict mechanism still exists and is
    still worth pinning — it is just no longer the default, so the tests that
    exercise it now say so instead of inheriting it.
    """
    base = default_config()
    return base.model_copy(
        update={
            "instruction_sets": tuple(
                iset.model_copy(
                    update={
                        "node_instructions": tuple(
                            node.model_copy(update={"strict_properties": True}) for node in iset.node_instructions
                        )
                    }
                )
                for iset in base.instruction_sets
            )
        }
    )


def config_with_jobs(
    *jobs: DreamJob,
    pruning: PruningPolicy | None = None,
    pure_read_retrieval: bool = False,
) -> DreamConfig:
    base = default_config()
    return base.model_copy(
        update={
            "jobs": jobs,
            "pruning": pruning or base.pruning,
            "pure_read_retrieval": pure_read_retrieval,
        }
    )


def openai_compatible_test_server(response_content: dict[str, Any]) -> tuple[HTTPServer, list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(content_length).decode("utf-8"))
            received.append(
                {
                    "path": self.path,
                    "authorization": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            response = {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(response_content),
                        }
                    }
                ]
            }
            response_body = json.dumps(response).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response_body)))
            self.end_headers()
            self.wfile.write(response_body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, received


class RecordingExtractionTransport:
    def __init__(self, response_memories: list[dict[str, Any]] | None = None) -> None:
        self.response_memories = response_memories or []
        self.requests: list[Any] = []

    async def extract_memories(self, request: Any) -> list[dict[str, Any]]:
        self.requests.append(request)
        return list(self.response_memories)


@pytest.mark.asyncio
async def test_customer_memory_supersedes_older_truth(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme",
                        "predicate": "requires",
                        "object": "SOC2 report",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=first_time,
    )
    await client.add_episode(
        name="second",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme",
                        "predicate": "requires",
                        "object": "SOC2 and ISO27001 reports",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.95,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=second_time,
    )

    result = await client.run_due_dreams(now=second_time + timedelta(seconds=2))
    matches = await client.search(query="ISO27001", scope=scope)
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]

    assert result.processed_episodes == 2
    assert len(matches) == 1
    assert matches[0].object == "SOC2 and ISO27001 reports"
    assert {relationship["properties"]["status"] for relationship in relationships} == {
        RelationshipStatus.ACTIVE.value,
        RelationshipStatus.PRUNED.value,
    }


@pytest.mark.asyncio
async def test_multi_active_preferences_accumulate_without_superseding(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="first-preference",
        episode_body="Memory: subject=User 42; predicate=prefers; object=concise answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.add_episode(
        name="second-preference",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.91",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )

    await client.run_due_dreams()
    matches = await client.search(query="answers", scope=scope, limit=10)
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]

    assert {match.object for match in matches} == {"concise answers", "sourced answers"}
    assert {relationship["properties"]["status"] for relationship in relationships} == {
        RelationshipStatus.ACTIVE.value,
    }
    assert {relationship["properties"]["truth_cardinality"] for relationship in relationships} == {
        RelationshipCardinality.MULTI_ACTIVE.value,
    }


@pytest.mark.asyncio
async def test_repeated_multi_active_fact_reinforces_existing_relationship(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.8",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.92",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 2, tzinfo=UTC),
    )

    result = await client.run_due_dreams(now=datetime(2026, 6, 3, tzinfo=UTC))
    matches = await client.search(query="sourced answers", scope=scope)
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]

    assert result.created_relationships == 1
    assert result.job_runs[0].reinforced_relationships == 1
    assert len(relationships) == 1
    # WS-16 T13 evidence accumulation: created at 0.8, reinforced by a 0.92
    # observation contributing gain=0.25 of the remaining headroom.
    assert relationships[0]["properties"]["confidence"] == pytest.approx(min(0.99, 0.8 + (1.0 - 0.8) * 0.25 * 0.92))
    assert relationships[0]["properties"]["observed_count"] == 2
    assert len(relationships[0]["properties"]["episode_uuids"]) == 2
    assert relationships[0]["properties"]["first_seen_at"] == "2026-06-01T00:00:00+00:00"
    assert relationships[0]["properties"]["last_seen_at"] == "2026-06-02T00:00:00+00:00"
    assert matches[0].observed_count == 2
    assert len(matches[0].episode_uuids) == 2


@pytest.mark.asyncio
async def test_repeated_single_active_fact_reinforces_without_superseding(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.88",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )

    result = await client.run_due_dreams()
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]

    assert result.created_relationships == 1
    assert result.job_runs[0].reinforced_relationships == 1
    assert result.job_runs[0].superseded_relationships == 0
    assert len(relationships) == 1
    assert relationships[0]["properties"]["status"] == RelationshipStatus.ACTIVE.value
    assert relationships[0]["properties"]["observed_count"] == 2


@pytest.mark.asyncio
async def test_formation_job_respects_max_items_per_run(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(
            name="formation-batch",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            max_items_per_run=1,
        ),
        DreamJob(
            name="pruning-batch",
            kind=DreamJobKind.PRUNING,
            cadence_seconds=1,
            max_items_per_run=10,
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=User 42; predicate=prefers; object=concise answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.91",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )

    first_run = await client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    immediate_run = await client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    second_run = await client.run_due_dreams(now=datetime(2026, 6, 1, 0, 0, 2, tzinfo=UTC))
    matches = await client.search(query="answers", scope=scope, limit=10)

    assert first_run.processed_episodes == 1
    assert immediate_run.job_runs == []
    assert second_run.processed_episodes == 1
    assert {match.object for match in matches} == {"concise answers", "sourced answers"}


@pytest.mark.asyncio
async def test_formation_job_filters_queued_episodes_by_source_and_metadata(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(
            name="formation-trusted",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            episode_filter=DreamEpisodeFilter(
                source_types=(EpisodeType.MESSAGE,),
                source_descriptions=("trusted capture",),
                metadata_filter={"source": "trusted"},
            ),
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)

    trusted = await client.add_episode(
        name="trusted-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=trusted answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        source_description="trusted capture",
        scope=scope,
        reference_time=run_time,
        metadata={"source": "trusted"},
    )
    scratch = await client.add_episode(
        name="scratch-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=scratch answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        source_description="scratch capture",
        scope=scope,
        reference_time=run_time,
        metadata={"source": "scratch"},
    )

    before = await client.dream_status(now=run_time)
    result = await client.run_due_dreams(now=run_time)
    after = await client.dream_status(now=run_time)
    matches = await client.search(query="answers", scope=scope, limit=10)
    before_by_name = {status.job_name: status for status in before}
    after_by_name = {status.job_name: status for status in after}

    assert before_by_name["formation-trusted"].pending_episodes == 1
    assert result.processed_episodes == 1
    assert after_by_name["formation-trusted"].pending_episodes == 0
    assert client.graph.is_episode_processed(trusted.episode_uuid) is True
    assert client.graph.is_episode_processed(scratch.episode_uuid) is False
    assert [match.object for match in matches] == ["trusted answers"]


@pytest.mark.asyncio
async def test_dream_status_reports_due_state_and_pending_work(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)

    await client.add_episode(
        name="preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=scheduler visibility; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=run_time,
    )

    before = await client.dream_status(now=run_time)
    await client.run_due_dreams(now=run_time)
    after = await client.dream_status(now=run_time)
    later = await client.dream_status(now=run_time + timedelta(seconds=1))
    before_by_name = {status.job_name: status for status in before}
    after_by_name = {status.job_name: status for status in after}
    later_by_name = {status.job_name: status for status in later}

    assert before_by_name["formation-default"].due is True
    assert before_by_name["formation-default"].pending_episodes == 1
    assert after_by_name["formation-default"].due is False
    assert after_by_name["formation-default"].last_run == run_time
    assert after_by_name["formation-default"].next_run == run_time + timedelta(seconds=1)
    assert after_by_name["formation-default"].pending_episodes == 0
    assert after_by_name["pruning-default"].due is False
    assert later_by_name["formation-default"].due is True


@pytest.mark.asyncio
async def test_run_dream_job_forces_named_job_and_records_history(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(
            name="formation-manual",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=3600,
        ),
        DreamJob(
            name="pruning-manual",
            kind=DreamJobKind.PRUNING,
            cadence_seconds=3600,
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 1, 0, 0, 10, tzinfo=UTC)

    await client.add_episode(
        name="manual-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=manual dream control; "
            "relationship_type=PREFERS; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )
    due_run = await client.run_due_dreams(now=first_time)
    await client.add_episode(
        name="manual-preference-repeat",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=manual dream control; "
            "relationship_type=PREFERS; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=second_time,
    )
    skipped_due_run = await client.run_due_dreams(now=second_time)
    forced_run = await client.run_dream_job(job_name="formation-manual", now=second_time)
    history = await client.dream_history(limit=10, job_name="formation-manual")
    matches = await client.search(query="manual dream control", scope=scope)

    assert {run.job_name for run in due_run.job_runs} == {"formation-manual", "pruning-manual"}
    assert skipped_due_run.job_runs == []
    assert forced_run.processed_episodes == 1
    assert len(history) == 2
    assert history[0].ran_at == second_time
    assert history[0].reinforced_relationships == 1
    assert matches[0].observed_count == 2
    with pytest.raises(ValueError, match="unknown dream job"):
        await client.run_dream_job(job_name="missing-job", now=second_time)


@pytest.mark.asyncio
async def test_memory_evolution_proof_shows_learning_not_raw_recording(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(
            name="formation-proof",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=3600,
        ),
        DreamJob(
            name="pruning-proof",
            kind=DreamJobKind.PRUNING,
            cadence_seconds=3600,
        ),
        pruning=PruningPolicy(min_confidence=0.1, superseded_retention_seconds=86400),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme-parks")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)
    third_time = datetime(2026, 6, 3, tzinfo=UTC)
    proof_time = datetime(2026, 6, 3, 12, tzinfo=UTC)

    await client.add_episode(
        name="initial-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 report before site visit; "
            "relationship_type=REQUIRES; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )
    await client.add_episode(
        name="duplicate-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 report before site visit; "
            "relationship_type=REQUIRES; confidence=0.93"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=second_time,
    )
    await client.add_episode(
        name="corrected-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=W9 form before site visit; "
            "relationship_type=REQUIRES; confidence=0.97"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=third_time,
    )
    await client.add_episode(
        name="temporary-channel",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "prefers",
                        "object": "phone support during audit week",
                        "relationship_type": "PREFERS",
                        "confidence": 0.92,
                        "valid_from": first_time.isoformat(),
                        "valid_to": second_time.isoformat(),
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=first_time,
    )

    formation = await client.run_dream_job(job_name="formation-proof", now=third_time)
    pruning = await client.run_dream_job(job_name="pruning-proof", now=proof_time)
    proof = await client.memory_evolution(scope=scope, as_of=proof_time)
    current_profile = await client.profile(scope=scope, as_of=proof_time)
    current_results = await client.search(query="site visit", scope=scope, as_of=proof_time)
    historical_results = await client.search(
        query="SOC2 report",
        scope=scope,
        as_of=first_time + timedelta(hours=1),
    )
    signals = {signal.name: signal for signal in proof.signals}

    assert formation.created_relationships == 3
    assert formation.job_runs[0].reinforced_relationships == 1
    assert formation.job_runs[0].superseded_relationships == 1
    assert pruning.job_runs[0].pruned_relationships == 1
    assert proof.episode_count == 4
    assert proof.processed_episode_count == 4
    assert proof.pending_episode_count == 0
    assert proof.decision_count == 5
    assert proof.created_relationship_count == 3
    assert proof.reinforced_relationship_count == 1
    assert proof.superseded_relationship_count == 1
    assert proof.pruned_relationship_count == 1
    assert proof.active_relationship_count == 1
    assert proof.inactive_relationship_count == 2
    assert {fact.object for fact in proof.active_facts} == {"W9 form before site visit"}
    assert {fact.object for fact in proof.inactive_facts} == {
        "SOC2 report before site visit",
        "phone support during audit week",
    }
    assert signals["selection_decisions"].observed is True
    assert signals["reinforcement"].observed is True
    assert signals["supersession"].observed is True
    assert signals["pruning"].observed is True
    assert signals["retrieval_filtering"].observed is True
    assert signals["compression"].observed is True
    assert signals["consolidation"].observed is False
    assert [fact.object for fact in current_profile.static_facts] == ["W9 form before site visit"]
    assert [result.object for result in current_results] == ["W9 form before site visit"]
    assert [result.object for result in historical_results] == ["SOC2 report before site visit"]


@pytest.mark.asyncio
async def test_relationship_specific_pruning_threshold_overrides_global_threshold(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            relationship_min_confidence={"prefers": 0.95},
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")

    await client.add_episode(
        name="preference",
        episode_body="Memory: subject=User 42; predicate=prefers; object=concise answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )
    await client.add_episode(
        name="lesson",
        episode_body="Memory: subject=Support Agent; predicate=should; object=confirm account id; relationship_type=SHOULD; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=agent_scope,
    )

    await client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    relationships = {
        relationship["type"]: relationship
        for relationship in client.export_graph()["relationships"]
        if relationship["type"] in {"PREFERS", "SHOULD"}
    }

    assert relationships["PREFERS"]["properties"]["status"] == RelationshipStatus.PRUNED.value
    assert relationships["PREFERS"]["properties"]["pruned_threshold"] == 0.95
    assert relationships["SHOULD"]["properties"]["status"] == RelationshipStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_pruning_repairs_inconsistent_single_active_truth_slot(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="pruning-repair", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            superseded_retention_seconds=604800,
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)
    truth_key = normalize_key(f"{scope.key}:Acme Parks:requires")
    subject_node, _ = client.graph.upsert_node(
        labels=("Entity", ScopeKind.CUSTOMER.value.title()),
        key=f"{scope.key}:Entity:Acme Parks",
        properties={
            "name": "Acme Parks",
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
        },
        valid_from=first_time,
    )
    old_object_node, _ = client.graph.upsert_node(
        labels=("Entity", ScopeKind.CUSTOMER.value.title()),
        key=f"{scope.key}:Entity:SOC2 report",
        properties={
            "name": "SOC2 report",
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
        },
        valid_from=first_time,
    )
    new_object_node, _ = client.graph.upsert_node(
        labels=("Entity", ScopeKind.CUSTOMER.value.title()),
        key=f"{scope.key}:Entity:SOC2 and ISO27001 reports",
        properties={
            "name": "SOC2 and ISO27001 reports",
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
        },
        valid_from=second_time,
    )
    old_relationship = client.graph.add_relationship(
        source_uuid=subject_node.uuid,
        target_uuid=old_object_node.uuid,
        relationship_type="REQUIRES",
        properties={
            "fact": "Acme Parks requires SOC2 report",
            "predicate": "requires",
            "object": "SOC2 report",
            "confidence": 0.9,
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
            "truth_key": truth_key,
            "truth_cardinality": RelationshipCardinality.SINGLE_ACTIVE.value,
            "status": RelationshipStatus.ACTIVE.value,
            "episode_uuid": "repair-old-episode",
            "episode_uuids": ["repair-old-episode"],
            "observed_count": 1,
            "first_seen_at": first_time.isoformat(),
            "last_seen_at": first_time.isoformat(),
            "metadata": {},
            "created_by": "manual-test-fixture",
        },
        valid_from=first_time,
    )
    new_relationship = client.graph.add_relationship(
        source_uuid=subject_node.uuid,
        target_uuid=new_object_node.uuid,
        relationship_type="REQUIRES",
        properties={
            "fact": "Acme Parks requires SOC2 and ISO27001 reports",
            "predicate": "requires",
            "object": "SOC2 and ISO27001 reports",
            "confidence": 0.95,
            "scope_kind": scope.kind.value,
            "scope_id": scope.scope_id,
            "scope_key": scope.key,
            "truth_key": truth_key,
            "truth_cardinality": RelationshipCardinality.SINGLE_ACTIVE.value,
            "status": RelationshipStatus.ACTIVE.value,
            "episode_uuid": "repair-new-episode",
            "episode_uuids": ["repair-new-episode"],
            "observed_count": 1,
            "first_seen_at": second_time.isoformat(),
            "last_seen_at": second_time.isoformat(),
            "metadata": {},
            "created_by": "manual-test-fixture",
        },
        valid_from=second_time,
    )

    result = await client.run_due_dreams(now=datetime(2026, 6, 3, tzinfo=UTC))
    current = await client.search(query="ISO27001", scope=scope)
    historical = await client.search(
        query="SOC2 report",
        scope=scope,
        as_of=first_time + timedelta(hours=1),
    )
    relationships = {
        relationship["uuid"]: relationship
        for relationship in client.export_graph()["relationships"]
        if relationship["type"] == "REQUIRES"
    }

    assert result.job_runs[0].superseded_relationships == 1
    assert result.job_runs[0].pruned_relationships == 0
    assert [match.relationship_uuid for match in current] == [new_relationship.uuid]
    assert [match.relationship_uuid for match in historical] == [old_relationship.uuid]
    assert relationships[old_relationship.uuid]["properties"]["status"] == RelationshipStatus.SUPERSEDED.value
    assert relationships[old_relationship.uuid]["properties"]["superseded_reason"] == "single_active_truth_repair"
    assert (
        relationships[old_relationship.uuid]["properties"]["superseded_by_relationship_uuid"] == new_relationship.uuid
    )
    assert relationships[old_relationship.uuid]["valid_to"] == "2026-06-02T00:00:00Z"
    assert relationships[new_relationship.uuid]["properties"]["status"] == RelationshipStatus.ACTIVE.value


async def _prune_by_active_max_age(
    tmp_path: Path, *, pure_read_retrieval: bool = False
) -> tuple[Memotron, MemoryScope, str]:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            relationship_active_max_age_seconds={"prefers": 3600},
        ),
        pure_read_retrieval=pure_read_retrieval,
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="old-preference",
        episode_body="Memory: subject=User 42; predicate=prefers; object=short answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )

    await client.run_due_dreams(now=datetime(2026, 6, 1, 2, tzinfo=UTC))
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]

    assert relationships[0]["properties"]["status"] == RelationshipStatus.PRUNED.value
    assert relationships[0]["properties"]["pruned_reason"] == "active_max_age_elapsed"
    assert relationships[0]["properties"]["pruned_active_max_age_seconds"] == 3600
    return client, scope, relationships[0]["uuid"]


@pytest.mark.asyncio
async def test_active_max_age_prunes_stale_relationships(tmp_path: Path) -> None:
    client, scope, _ = await _prune_by_active_max_age(tmp_path)

    # Default: a matching current retrieval brings the archived row back.
    restored = await client.search(query="short answers", scope=scope)
    assert [item.object for item in restored] == ["short answers"]
    restored_relationship = client.graph.get_relationship(restored[0].relationship_uuid)
    assert restored_relationship.properties["status"] == RelationshipStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_active_max_age_pruned_rows_are_reported_not_revived_under_pure_read(
    tmp_path: Path,
) -> None:
    client, scope, pruned_uuid = await _prune_by_active_max_age(tmp_path, pure_read_retrieval=True)

    # With pure_read_retrieval on, searching reports the archived row instead.
    state_before = client.graph.graph_state_hash(scope.key)
    reported = await client.search(query="short answers", scope=scope)
    assert [item.object for item in reported] == []
    assert [match.relationship_uuid for match in reported.archived.matches] == [pruned_uuid]
    assert client.graph.graph_state_hash(scope.key) == state_before
    assert client.graph.get_relationship(pruned_uuid).properties["status"] == (RelationshipStatus.PRUNED.value)

    # Getting it back is then the explicit curation action.
    await client.restore_archived_memory(
        relationship_uuid=pruned_uuid,
        scope=scope,
        reason="operator still wants short answers",
    )
    restored = await client.search(query="short answers", scope=scope)
    assert [item.object for item in restored] == ["short answers"]
    restored_relationship = client.graph.get_relationship(restored[0].relationship_uuid)
    assert restored_relationship.properties["status"] == RelationshipStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_current_retrieval_hides_expired_active_relationships_before_pruning(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme-parks")
    anchor = datetime.now(UTC).replace(microsecond=0)
    valid_from = anchor - timedelta(days=2)
    valid_to = anchor - timedelta(days=1)

    await client.add_episode(
        name="temporary-support-channel",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "prefers",
                        "object": "temporary phone support",
                        "relationship_type": "PREFERS",
                        "confidence": 0.9,
                        "valid_from": valid_from.isoformat(),
                        "valid_to": valid_to.isoformat(),
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=valid_from,
    )

    await client.run_due_dreams(now=anchor)
    current = await client.search(query="temporary phone support", scope=scope)
    historical = await client.search(
        query="temporary phone support",
        scope=scope,
        as_of=valid_from + timedelta(hours=1),
    )
    profile = await client.profile(scope=scope)
    neighborhood = await client.entity_neighborhood(scope=scope, entity="Acme Parks")
    timeline = await client.truth_timeline(
        scope=scope,
        subject="Acme Parks",
        predicate="prefers",
        relationship_type="PREFERS",
    )
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]

    assert relationships[0]["properties"]["status"] == RelationshipStatus.ACTIVE.value
    assert current == []
    assert len(historical) == 1
    assert historical[0].status == RelationshipStatus.ACTIVE
    assert historical[0].valid_to == valid_to
    assert profile.static_facts == []
    assert profile.dynamic_facts == []
    assert neighborhood == []
    assert len(timeline) == 1
    assert timeline[0].is_current is False


@pytest.mark.asyncio
async def test_pruning_marks_elapsed_valid_to_relationships_without_losing_history(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme-parks")
    anchor = datetime.now(UTC).replace(microsecond=0)
    valid_from = anchor - timedelta(days=2)
    valid_to = anchor - timedelta(days=1)

    await client.add_episode(
        name="expired-temporary-support-channel",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "prefers",
                        "object": "temporary phone support",
                        "relationship_type": "PREFERS",
                        "confidence": 0.9,
                        "valid_from": valid_from.isoformat(),
                        "valid_to": valid_to.isoformat(),
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=valid_from,
    )

    result = await client.run_due_dreams(now=anchor)
    current = await client.search(query="temporary phone support", scope=scope)
    historical = await client.search(
        query="temporary phone support",
        scope=scope,
        as_of=valid_from + timedelta(hours=1),
    )
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]

    assert sum(run.pruned_relationships for run in result.job_runs) == 1
    assert relationships[0]["properties"]["status"] == RelationshipStatus.PRUNED.value
    assert relationships[0]["properties"]["pruned_reason"] == "valid_to_elapsed"
    assert relationships[0]["valid_to"] == valid_to.isoformat().replace("+00:00", "Z")
    assert current == []
    assert len(historical) == 1
    assert historical[0].status == RelationshipStatus.PRUNED
    assert historical[0].valid_to == valid_to


@pytest.mark.asyncio
async def test_reinforced_relationship_uses_last_seen_for_active_max_age(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            relationship_active_max_age_seconds={"prefers": 86400},
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.8",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.92",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 3, tzinfo=UTC),
    )

    await client.run_due_dreams(now=datetime(2026, 6, 3, 12, tzinfo=UTC))
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    matches = await client.search(query="sourced answers", scope=scope)

    assert relationships[0]["properties"]["status"] == RelationshipStatus.ACTIVE.value
    assert relationships[0]["properties"]["observed_count"] == 2
    assert relationships[0]["properties"]["last_seen_at"] == "2026-06-03T00:00:00+00:00"
    assert len(matches) == 1
    assert matches[0].observed_count == 2


@pytest.mark.asyncio
async def test_temporal_search_returns_truth_valid_at_as_of(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme",
                        "predicate": "requires",
                        "object": "SOC2 report",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=first_time,
    )
    await client.add_episode(
        name="second",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme",
                        "predicate": "requires",
                        "object": "SOC2 and ISO27001 reports",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.95,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=second_time,
    )
    await client.run_due_dreams(now=second_time + timedelta(seconds=2))

    historical = await client.search(
        query="SOC2 report",
        scope=scope,
        as_of=first_time + timedelta(hours=1),
    )
    current = await client.search(query="ISO27001", scope=scope)

    assert len(historical) == 1
    assert historical[0].object == "SOC2 report"
    assert historical[0].status == RelationshipStatus.PRUNED
    assert historical[0].valid_to == second_time
    assert len(current) == 1
    assert current[0].object == "SOC2 and ISO27001 reports"
    assert current[0].status == RelationshipStatus.ACTIVE


@pytest.mark.asyncio
async def test_backfilled_older_single_active_fact_does_not_replace_newer_truth(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    older_time = datetime(2026, 6, 1, tzinfo=UTC)
    newer_time = datetime(2026, 6, 3, tzinfo=UTC)

    await client.add_episode(
        name="newer-truth-arrives-first",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=newer_time,
    )
    await client.add_episode(
        name="older-truth-backfill",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=older_time,
    )

    await client.run_due_dreams(now=datetime(2026, 6, 4, tzinfo=UTC))
    current = await client.search(query="ISO27001", scope=scope)
    historical = await client.search(
        query="SOC2 report",
        scope=scope,
        as_of=older_time + timedelta(hours=1),
    )
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]

    assert len(current) == 1
    assert current[0].object == "SOC2 and ISO27001 reports"
    assert current[0].status == RelationshipStatus.ACTIVE
    assert len(historical) == 1
    assert historical[0].object == "SOC2 report"
    assert historical[0].valid_to == newer_time
    assert {relationship["properties"]["status"] for relationship in relationships} == {
        RelationshipStatus.ACTIVE.value,
        RelationshipStatus.PRUNED.value,
    }


@pytest.mark.asyncio
async def test_backfilled_repeated_fact_preserves_latest_last_seen(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    older_time = datetime(2026, 6, 1, tzinfo=UTC)
    newer_time = datetime(2026, 6, 3, tzinfo=UTC)

    await client.add_episode(
        name="newer-repeat-arrives-first",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=newer_time,
    )
    await client.add_episode(
        name="older-repeat-backfill",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=older_time,
    )

    result = await client.run_due_dreams(now=datetime(2026, 6, 4, tzinfo=UTC))
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    historical = await client.search(
        query="sourced answers",
        scope=scope,
        as_of=older_time + timedelta(hours=1),
    )

    assert result.created_relationships == 1
    assert result.job_runs[0].reinforced_relationships == 1
    assert len(relationships) == 1
    assert relationships[0]["valid_from"] == "2026-06-01T00:00:00Z"
    assert relationships[0]["properties"]["first_seen_at"] == "2026-06-01T00:00:00+00:00"
    assert relationships[0]["properties"]["last_seen_at"] == "2026-06-03T00:00:00+00:00"
    assert relationships[0]["properties"]["observed_count"] == 2
    assert len(historical) == 1


@pytest.mark.asyncio
async def test_truth_timeline_returns_chronological_lineage_for_superseded_truth(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    first = await client.add_episode(
        name="first",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )
    second = await client.add_episode(
        name="second",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=second_time,
    )

    await client.run_due_dreams(now=second_time + timedelta(seconds=2))
    timeline = await client.truth_timeline(
        scope=scope,
        subject="Acme",
        predicate="requires",
        relationship_type="requires",
    )

    assert [entry.object for entry in timeline] == [
        "SOC2 report",
        "SOC2 and ISO27001 reports",
    ]
    assert [entry.status for entry in timeline] == [
        RelationshipStatus.PRUNED,
        RelationshipStatus.ACTIVE,
    ]
    assert [entry.is_current for entry in timeline] == [False, True]
    assert timeline[0].valid_to == second_time
    assert timeline[0].episode_uuids == [first.episode_uuid]
    assert timeline[1].episode_uuids == [second.episode_uuid]
    assert {entry.created_by for entry in timeline} == {"dream-agent:dream-agent:formation"}


@pytest.mark.asyncio
async def test_truth_timeline_is_scoped_and_includes_backfilled_history(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    older_time = datetime(2026, 6, 1, tzinfo=UTC)
    newer_time = datetime(2026, 6, 3, tzinfo=UTC)

    await client.add_episode(
        name="newer-customer-truth",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
        reference_time=newer_time,
    )
    await client.add_episode(
        name="older-customer-backfill",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
        reference_time=older_time,
    )
    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
        reference_time=older_time,
    )

    await client.run_due_dreams(now=datetime(2026, 6, 4, tzinfo=UTC))
    customer_timeline = await client.truth_timeline(
        scope=customer_scope,
        subject="Acme",
        predicate="requires",
    )
    user_timeline = await client.truth_timeline(
        scope=user_scope,
        subject="Acme",
        predicate="requires",
    )

    assert [entry.object for entry in customer_timeline] == [
        "SOC2 report",
        "SOC2 and ISO27001 reports",
    ]
    assert customer_timeline[0].valid_from == older_time
    assert customer_timeline[0].valid_to == newer_time
    assert customer_timeline[0].superseded_by_relationship_uuid == customer_timeline[1].relationship_uuid
    assert customer_timeline[1].is_current is True
    assert user_timeline == []


@pytest.mark.asyncio
async def test_scope_isolation_in_search(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="customer",
        episode_body="Memory: subject=Acme; predicate=requires; object=vendor questionnaire; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )
    await client.add_episode(
        name="user",
        episode_body="Memory: subject=User 42; predicate=prefers; object=short answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )

    await client.run_due_dreams()

    assert await client.search(query="vendor", scope=user_scope) == []
    assert len(await client.search(query="vendor", scope=customer_scope)) == 1


@pytest.mark.asyncio
async def test_search_context_combines_agent_user_and_customer_scopes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="agent",
        episode_body="Memory: subject=Support Agent; predicate=should; object=confirm account id before escalation; relationship_type=SHOULD; confidence=0.86",
        source=EpisodeType.MESSAGE,
        scope=agent_scope,
    )
    await client.add_episode(
        name="user",
        episode_body="Memory: subject=User 42; predicate=prefers; object=concise escalation summaries; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )
    await client.add_episode(
        name="customer",
        episode_body="Memory: subject=Acme; predicate=requires; object=vendor escalation summary before approval; relationship_type=REQUIRES; confidence=0.94",
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )

    await client.run_due_dreams()
    results = await client.search_context(
        query="escalation summary",
        scopes=[customer_scope, user_scope, agent_scope],
        limit=10,
    )

    assert [result.scope.kind for result in results] == [
        ScopeKind.CUSTOMER,
        ScopeKind.USER,
        ScopeKind.AGENT,
    ]
    assert [result.scope_rank for result in results] == [0, 1, 2]


async def _seed_cross_scope_relevance_gap(client: Memotron) -> tuple[MemoryScope, MemoryScope]:
    """Seed a DESIGNED cross-scope ranking case, not a fixture coincidence.

    The customer scope (rank 0) holds a fact that barely touches the query; the
    agent scope (rank 1) holds one that matches it almost exactly. Which of the
    two wins is then a statement about the ranking policy, not about whichever
    order the fixture happened to produce.
    """
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    # Shares exactly one query token ("refund") — a candidate, but a poor one.
    await client.add_episode(
        name="weak-customer",
        episode_body=(
            "Memory: subject=Acme; predicate=prefers; "
            "object=refund totals printed on the monthly paper statement; "
            "relationship_type=PREFERS; confidence=0.5"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )
    # Restates the query almost verbatim.
    await client.add_episode(
        name="strong-agent",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; "
            "object=verify refund eligibility before issuing; "
            "relationship_type=SHOULD; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=agent_scope,
    )
    await client.run_due_dreams()
    return customer_scope, agent_scope


CROSS_SCOPE_QUERY = "verify refund eligibility before issuing"


@pytest.mark.asyncio
async def test_scope_priority_is_soft_so_a_strong_later_scope_match_can_win(
    tmp_path: Path,
) -> None:
    """WS-5 rerank folds scope into the score instead of partitioning by it.

    The pre-rerank pipeline hard-partitioned by scope rank, so scope 0 ALWAYS
    beat scope 1 regardless of relevance. It does not any more: this asserts the
    soft behaviour directly, with a relevance gap large enough that the outcome
    is designed rather than incidental.
    """
    client = new_client(tmp_path)
    customer_scope, agent_scope = await _seed_cross_scope_relevance_gap(client)

    results = await client.search_context(
        query=CROSS_SCOPE_QUERY,
        scopes=[customer_scope, agent_scope],
        limit=10,
    )

    assert [result.scope.kind for result in results] == [ScopeKind.AGENT, ScopeKind.CUSTOMER]
    assert [result.scope_rank for result in results] == [1, 0]
    assert results[0].score > results[1].score


@pytest.mark.asyncio
async def test_strict_scope_tiering_restores_the_hard_scope_partition(tmp_path: Path) -> None:
    """The same designed case, with strict tiering on: earlier scope wins outright.

    Under ``strict_scope_tiering`` every rank-0 result precedes every rank-1
    result even when the rank-1 row scores higher — the weighted score only
    orders results within a rank.
    """
    base = default_config()
    client = Memotron(
        config=base.model_copy(update={"retrieval": base.retrieval.model_copy(update={"strict_scope_tiering": True})}),
        graph_path=tmp_path / "memotron.sqlite",
    )
    customer_scope, agent_scope = await _seed_cross_scope_relevance_gap(client)

    results = await client.search_context(
        query=CROSS_SCOPE_QUERY,
        scopes=[customer_scope, agent_scope],
        limit=10,
    )

    assert [result.scope.kind for result in results] == [ScopeKind.CUSTOMER, ScopeKind.AGENT]
    assert [result.scope_rank for result in results] == [0, 1]
    # ...and it really is a partition, not a re-scoring: the later-scope row
    # still carries the higher score, it is simply tiered below.
    assert results[1].score > results[0].score


@pytest.mark.asyncio
async def test_strict_scope_tiering_is_pinned_into_the_retrieval_contract(
    tmp_path: Path,
) -> None:
    """A search must record WHICH scope ordering produced it."""
    from memotron.retrieval import build_retrieval_contract, retrieval_contract_digest

    policy = default_config().retrieval
    kwargs: dict[str, Any] = {
        "operation": "search_context",
        "scope_keys": ("customer:acme", "agent:support-agent"),
        "query": "refunds",
        "limit": 5,
    }
    soft = retrieval_contract_digest(build_retrieval_contract(policy=policy, **kwargs))
    strict = retrieval_contract_digest(
        build_retrieval_contract(policy=policy.model_copy(update={"strict_scope_tiering": True}), **kwargs)
    )
    assert soft != strict


@pytest.mark.asyncio
async def test_profile_returns_scoped_static_and_dynamic_context(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="user-style",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=concise technical answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
        reference_time=datetime(2026, 6, 1, 12, tzinfo=UTC),
    )
    await client.add_episode(
        name="user-evidence",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=answers with supporting evidence; "
            "relationship_type=PREFERS; confidence=0.92"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
        reference_time=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )
    await client.add_episode(
        name="customer-requirement",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=vendor packet review; "
            "relationship_type=REQUIRES; confidence=0.94"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
        reference_time=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )

    await client.run_due_dreams(now=datetime(2026, 6, 1, 14, tzinfo=UTC))
    profile = await client.profile(scope=user_scope)

    assert {fact.scope.key for fact in profile.static_facts} == {user_scope.key}
    assert {fact.object for fact in profile.static_facts} == {
        "concise technical answers",
        "answers with supporting evidence",
    }
    assert {fact.object for fact in profile.dynamic_facts} == {
        "concise technical answers",
        "answers with supporting evidence",
    }
    assert "Memory profile for user:user-42" in profile.rendered_context
    assert "Static facts:" in profile.rendered_context
    assert "Recent facts:" in profile.rendered_context
    assert "Acme" not in profile.rendered_context


@pytest.mark.asyncio
async def test_profile_policy_filters_relationship_types_and_dynamic_window(tmp_path: Path) -> None:
    config = default_config().model_copy(
        update={
            "profile": ProfilePolicy(
                max_static_facts=10,
                max_dynamic_facts=10,
                static_relationship_types=("prefers",),
                dynamic_relationship_types=("prefers",),
                dynamic_window_seconds=3600,
            )
        }
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    old_time = datetime(2026, 6, 1, 12, tzinfo=UTC)
    recent_time = datetime(2026, 6, 3, 11, 30, tzinfo=UTC)
    profile_time = datetime(2026, 6, 3, 12, tzinfo=UTC)

    await client.add_episode(
        name="old-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=short answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=old_time,
    )
    await client.add_episode(
        name="recent-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=recent_time,
    )
    await client.add_episode(
        name="excluded-requirement",
        episode_body=(
            "Memory: subject=User 42; predicate=requires; object=account verification; "
            "relationship_type=REQUIRES; confidence=0.96"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=recent_time,
    )

    await client.run_due_dreams(now=profile_time)
    profile = await client.profile(scope=scope, as_of=profile_time)

    assert [fact.relationship_type for fact in profile.static_facts] == ["PREFERS", "PREFERS"]
    assert {fact.object for fact in profile.static_facts} == {"short answers", "sourced answers"}
    assert [fact.object for fact in profile.dynamic_facts] == ["sourced answers"]
    assert "account verification" not in profile.rendered_context


@pytest.mark.asyncio
async def test_entity_neighborhood_returns_scoped_outgoing_and_incoming_edges(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="customer-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=vendor packet review; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )
    await client.add_episode(
        name="customer-preference",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=prefers; object=morning calls; "
            "relationship_type=PREFERS; confidence=0.87"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )
    await client.add_episode(
        name="user-same-entity-name",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=prefers; object=unrelated user note; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )

    await client.run_due_dreams()
    outgoing = await client.entity_neighborhood(
        scope=customer_scope,
        entity="Acme Parks",
        limit=10,
    )
    incoming = await client.entity_neighborhood(
        scope=customer_scope,
        entity="vendor packet review",
    )

    assert {edge.scope.key for edge in outgoing} == {customer_scope.key}
    assert {(edge.relationship_type, edge.direction, edge.object) for edge in outgoing} == {
        ("REQUIRES", "outgoing", "vendor packet review"),
        ("PREFERS", "outgoing", "morning calls"),
    }
    assert {edge.object for edge in outgoing} == {
        "vendor packet review",
        "morning calls",
    }
    assert len(incoming) == 1
    assert incoming[0].direction == "incoming"
    assert incoming[0].subject == "Acme Parks"
    assert incoming[0].relationship_type == "REQUIRES"


@pytest.mark.asyncio
async def test_entity_neighborhood_respects_current_and_historical_truth(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 report; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )
    await client.add_episode(
        name="updated-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=second_time,
    )

    await client.run_due_dreams(now=second_time + timedelta(seconds=2))
    current = await client.entity_neighborhood(
        scope=scope,
        entity="Acme Parks",
        relationship_types={"requires"},
    )
    historical = await client.entity_neighborhood(
        scope=scope,
        entity="Acme Parks",
        relationship_types={"requires"},
        as_of=first_time + timedelta(hours=1),
    )

    assert [edge.object for edge in current] == ["SOC2 and ISO27001 reports"]
    assert current[0].status == RelationshipStatus.ACTIVE
    assert [edge.object for edge in historical] == ["SOC2 report"]
    assert historical[0].status == RelationshipStatus.PRUNED
    assert historical[0].valid_to == second_time
    assert historical[0].superseded_by_relationship_uuid == current[0].relationship_uuid


@pytest.mark.asyncio
async def test_knowledge_graph_projects_scoped_memory_facts_as_nodes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_episode(
        name="customer-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=vendor packet review; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )
    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=prefers; object=unrelated user note; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )
    await client.run_due_dreams()

    view = await client.knowledge_graph(scope=customer_scope)
    memory_nodes = [node for node in view.nodes if node.node_type == "memory"]
    entity_nodes = [node for node in view.nodes if node.node_type == "entity"]

    assert view.scope == customer_scope
    assert view.relationship_count == 1
    assert view.memory_type_distribution == {"requirement": 1}
    assert view.status_distribution == {"active": 1}
    assert view.context_visible_relationship_count == 1
    assert {node.label for node in entity_nodes} == {"Acme Parks", "vendor packet review"}
    assert len(memory_nodes) == 1
    assert memory_nodes[0].relationship_type == "REQUIRES"
    assert memory_nodes[0].memory_type == "requirement"
    assert memory_nodes[0].status == RelationshipStatus.ACTIVE
    assert memory_nodes[0].properties["subject"] == "Acme Parks"
    assert memory_nodes[0].properties["object"] == "vendor packet review"
    assert {edge.edge_type for edge in view.edges} == {"subject", "object"}


@pytest.mark.asyncio
async def test_knowledge_graph_filters_history_and_adds_supersession_edges(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 report; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )
    await client.add_episode(
        name="updated-requirement",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=second_time,
    )
    await client.run_due_dreams(now=second_time + timedelta(seconds=2))

    current = await client.knowledge_graph(scope=scope, relationship_types={"requires"})
    historical = await client.knowledge_graph(
        scope=scope,
        relationship_types={"requires"},
        include_statuses={RelationshipStatus.ACTIVE, RelationshipStatus.PRUNED},
    )

    current_memory_labels = {node.properties["object"] for node in current.nodes if node.node_type == "memory"}
    historical_memory_labels = {node.properties["object"] for node in historical.nodes if node.node_type == "memory"}

    assert current.relationship_count == 1
    assert current_memory_labels == {"SOC2 and ISO27001 reports"}
    assert historical.relationship_count == 2
    assert historical_memory_labels == {"SOC2 report", "SOC2 and ISO27001 reports"}
    assert any(edge.edge_type == "superseded_by" for edge in historical.edges)


@pytest.mark.asyncio
async def test_knowledge_graph_query_and_demoted_filtering(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    first = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise sourced answers",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )
    await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="weekly summaries",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.87,
    )
    client.graph.update_relationship(
        first.relationship_uuid,
        properties={"active_in_context": False},
    )

    with_demoted = await client.knowledge_graph(scope=scope, query="sourced")
    without_demoted = await client.knowledge_graph(
        scope=scope,
        query="sourced",
        include_demoted=False,
    )

    assert with_demoted.relationship_count == 1
    assert with_demoted.demoted_relationship_count == 1
    assert without_demoted.relationship_count == 0


@pytest.mark.asyncio
async def test_memory_evidence_returns_raw_reinforcement_episodes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    first = await client.add_episode(
        name="first-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.88; source_text=User asked for sources"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct chat",
        scope=scope,
        reference_time=first_time,
        metadata={"ticket": "support-1"},
    )
    second = await client.add_episode(
        name="second-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.94; source_text=User repeated source request"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct chat follow-up",
        scope=scope,
        reference_time=second_time,
        metadata={"ticket": "support-2"},
    )

    await client.run_due_dreams(now=datetime(2026, 6, 3, tzinfo=UTC))
    result = await client.search(query="sourced answers", scope=scope)
    evidence = await client.memory_evidence(
        relationship_uuid=result[0].relationship_uuid,
        scope=scope,
    )

    assert evidence.fact == "User 42 prefers sourced answers"
    assert evidence.observed_count == 2
    assert evidence.episode_uuids == [first.episode_uuid, second.episode_uuid]
    assert [episode.uuid for episode in evidence.episodes] == [first.episode_uuid, second.episode_uuid]
    assert [episode.name for episode in evidence.episodes] == ["first-preference", "second-preference"]
    assert [episode.metadata["ticket"] for episode in evidence.episodes] == ["support-1", "support-2"]
    assert evidence.episodes[0].body.startswith("Memory: subject=User 42")
    assert evidence.episodes[1].source_description == "direct chat follow-up"


@pytest.mark.asyncio
async def test_forget_memory_soft_prunes_relationship_and_preserves_evidence(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    reference_time = datetime(2026, 6, 1, tzinfo=UTC)
    forget_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="bad-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=unverified guesses; "
            "relationship_type=PREFERS; confidence=0.91; source_text=User joked about guessing"
        ),
        source=EpisodeType.MESSAGE,
        source_description="direct chat",
        scope=scope,
        reference_time=reference_time,
        metadata={"ticket": "support-1"},
    )

    await client.run_due_dreams(now=reference_time + timedelta(seconds=2))
    before = await client.search(query="unverified guesses", scope=scope)
    forget_result = await client.forget_memory(
        relationship_uuid=before[0].relationship_uuid,
        scope=scope,
        reason="operator_removed_bad_memory",
        now=forget_time,
    )
    current = await client.search(query="unverified guesses", scope=scope)
    historical = await client.search(
        query="unverified guesses",
        scope=scope,
        as_of=reference_time + timedelta(hours=1),
    )
    evidence = await client.memory_evidence(
        relationship_uuid=before[0].relationship_uuid,
        scope=scope,
    )
    timeline = await client.truth_timeline(
        scope=scope,
        subject="User 42",
        predicate="prefers",
        relationship_type="PREFERS",
    )

    assert forget_result.previous_status == RelationshipStatus.ACTIVE
    assert forget_result.status == RelationshipStatus.PRUNED
    assert forget_result.valid_to == forget_time
    assert forget_result.pruned_reason == "operator_removed_bad_memory"
    assert current == []
    assert len(historical) == 1
    assert historical[0].status == RelationshipStatus.PRUNED
    assert historical[0].valid_to == forget_time
    assert evidence.status == RelationshipStatus.PRUNED
    assert evidence.episodes[0].body.startswith("Memory: subject=User 42")
    assert timeline[0].status == RelationshipStatus.PRUNED
    assert timeline[0].pruned_reason == "operator_removed_bad_memory"
    assert timeline[0].is_current is False


@pytest.mark.asyncio
async def test_forget_memory_fails_fast_on_scope_mismatch(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )

    await client.run_due_dreams()
    result = await client.search(query="sourced answers", scope=user_scope)

    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.forget_memory(
            relationship_uuid=result[0].relationship_uuid,
            scope=customer_scope,
        )


@pytest.mark.asyncio
async def test_correct_memory_versions_single_active_truth_and_preserves_evidence(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    correction_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="incorrect-requirement",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=W9 form before vendor approval; "
            "relationship_type=REQUIRES; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        source_description="support chat",
        scope=scope,
        reference_time=first_time,
        metadata={"ticket": "support-1"},
    )

    await client.run_due_dreams(now=first_time + timedelta(seconds=2))
    target = await client.search(query="W9 form", scope=scope)
    correction = await client.correct_memory(
        relationship_uuid=target[0].relationship_uuid,
        corrected_object="SOC2 report before vendor approval",
        scope=scope,
        confidence=0.98,
        reason="operator_corrected_requirement",
        source_text="Ops confirmed the requirement is SOC2, not W9.",
        metadata={"reviewed_by": "ops"},
        now=correction_time,
    )
    current_old = await client.search(query="W9 form", scope=scope)
    current_new = await client.search(query="SOC2 report", scope=scope)
    historical_old = await client.search(
        query="W9 form",
        scope=scope,
        as_of=first_time + timedelta(hours=1),
    )
    corrected_evidence = await client.memory_evidence(
        relationship_uuid=correction.corrected_relationship_uuid,
        scope=scope,
    )
    timeline = await client.truth_timeline(
        scope=scope,
        subject="Acme",
        predicate="requires",
        relationship_type="REQUIRES",
    )

    assert correction.previous_status == RelationshipStatus.ACTIVE
    assert correction.status == RelationshipStatus.SUPERSEDED
    assert correction.valid_to == correction_time
    assert correction.created_relationships == 1
    assert current_old == []
    assert len(current_new) == 1
    assert current_new[0].relationship_uuid == correction.corrected_relationship_uuid
    assert current_new[0].object == "SOC2 report before vendor approval"
    assert len(historical_old) == 1
    assert historical_old[0].status == RelationshipStatus.SUPERSEDED
    assert historical_old[0].valid_to == correction_time
    assert client.graph.is_episode_processed(correction.correction_episode_uuid) is True
    assert corrected_evidence.episodes[0].uuid == correction.correction_episode_uuid
    assert corrected_evidence.episodes[0].source_description == "manual memory correction"
    assert corrected_evidence.metadata["corrected_relationship_uuid"] == target[0].relationship_uuid
    assert corrected_evidence.metadata["reviewed_by"] == "ops"
    assert [entry.object for entry in timeline] == [
        "W9 form before vendor approval",
        "SOC2 report before vendor approval",
    ]
    assert timeline[0].superseded_by_relationship_uuid == correction.corrected_relationship_uuid
    assert timeline[0].metadata["correction_reason"] == "operator_corrected_requirement"
    assert timeline[1].is_current is True


@pytest.mark.asyncio
async def test_correct_memory_can_supersede_multi_active_relationship(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    correction_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="incorrect-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=guess-first answers; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=first_time,
    )

    await client.run_due_dreams(now=first_time + timedelta(seconds=2))
    target = await client.search(query="guess-first", scope=scope)
    correction = await client.correct_memory(
        relationship_uuid=target[0].relationship_uuid,
        corrected_object="evidence-first answers",
        scope=scope,
        reason="operator_corrected_preference",
        now=correction_time,
    )
    old_current = await client.search(query="guess-first", scope=scope)
    new_current = await client.search(query="evidence-first", scope=scope)
    timeline = await client.truth_timeline(
        scope=scope,
        subject="User 42",
        predicate="prefers",
        relationship_type="PREFERS",
    )

    assert old_current == []
    assert len(new_current) == 1
    assert new_current[0].relationship_uuid == correction.corrected_relationship_uuid
    assert [entry.status for entry in timeline] == [
        RelationshipStatus.SUPERSEDED,
        RelationshipStatus.ACTIVE,
    ]
    assert timeline[0].superseded_by_relationship_uuid == correction.corrected_relationship_uuid
    assert timeline[0].metadata["correction_reason"] == "operator_corrected_preference"


@pytest.mark.asyncio
async def test_correct_memory_fails_fast_on_scope_mismatch(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )

    await client.run_due_dreams()
    result = await client.search(query="sourced answers", scope=user_scope)

    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.correct_memory(
            relationship_uuid=result[0].relationship_uuid,
            corrected_object="concise answers",
            scope=customer_scope,
        )


@pytest.mark.asyncio
async def test_memory_evidence_fails_fast_on_scope_mismatch(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )

    await client.run_due_dreams()
    result = await client.search(query="sourced answers", scope=user_scope)

    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.memory_evidence(
            relationship_uuid=result[0].relationship_uuid,
            scope=customer_scope,
        )


@pytest.mark.asyncio
async def test_add_context_chunks_raw_setting_into_internal_episodes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    content = (
        "Support setting: Acme requires vendor packet review before approval.\n\n"
        "Escalation notes should preserve compliance blockers and owner names.\n"
        "Follow-up notes belong to the same customer memory scope."
    )

    result = await client.add_context(
        name="acme-support-setting",
        content=content,
        scopes=[scope],
        custom_id="acme-setting-v1",
        metadata={"source": "shadow_workspace"},
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
        max_chars_per_episode=72,
    )
    queued = client.graph.episodes()

    assert result.document_id == "acme-setting-v1"
    assert result.chunks_created > 1
    assert result.episodes_created == result.chunks_created
    assert result.episode_uuids == [episode.uuid for episode in queued]
    assert "".join(episode.body for episode in queued) == content
    assert {episode.scope.key for episode in queued} == {scope.key}
    assert {episode.metadata["shadow_document_id"] for episode in queued} == {"acme-setting-v1"}
    assert {episode.metadata["shadow_source_name"] for episode in queued} == {"acme-support-setting"}
    assert {episode.metadata["source"] for episode in queued} == {"shadow_workspace"}
    assert [episode.metadata["shadow_chunk_index"] for episode in queued] == list(range(result.chunks_created))


@pytest.mark.asyncio
async def test_shadow_context_dreaming_supersedes_contradictory_setting(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    first_time = datetime(2026, 6, 1, tzinfo=UTC)
    second_time = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_context(
        name="acme-setting-v1",
        content=(
            "Shadow workspace setting captured from a support room.\n"
            "Memory: subject=Acme; predicate=requires; object=SOC2 report; "
            "relationship_type=REQUIRES; confidence=0.9"
        ),
        scopes=[scope],
        custom_id="acme-setting-v1",
        reference_time=first_time,
    )
    await client.add_context(
        name="acme-setting-v2",
        content=(
            "Shadow workspace correction captured after compliance review.\n"
            "Memory: subject=Acme; predicate=requires; object=SOC2 and ISO27001 reports; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        scopes=[scope],
        custom_id="acme-setting-v2",
        reference_time=second_time,
    )

    result = await client.run_due_dreams(now=second_time + timedelta(seconds=2))
    current = await client.search(query="ISO27001", scope=scope)
    historical = await client.search(
        query="SOC2 report",
        scope=scope,
        as_of=first_time + timedelta(hours=1),
    )
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]

    assert result.processed_episodes == 2
    assert result.job_runs[0].superseded_relationships == 1
    assert len(current) == 1
    assert current[0].object == "SOC2 and ISO27001 reports"
    assert len(historical) == 1
    assert historical[0].object == "SOC2 report"
    assert {relationship["properties"]["status"] for relationship in relationships} == {
        RelationshipStatus.ACTIVE.value,
        RelationshipStatus.PRUNED.value,
    }
    assert {relationship["properties"]["metadata"]["shadow_document_id"] for relationship in relationships} == {
        "acme-setting-v1",
        "acme-setting-v2",
    }


@pytest.mark.asyncio
async def test_formation_prompt_includes_scoped_existing_graph_context(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    seed_client = Memotron(graph_path=graph_path)

    await seed_client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=answers with supporting evidence; "
            "relationship_type=PREFERS; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.add_episode(
        name="customer-requirement",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=vendor packet review; "
            "relationship_type=REQUIRES; confidence=0.94"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, 0, 0, 2, tzinfo=UTC))
    seed_client.graph.close()

    recorder = RecordingExtractionTransport()
    client = Memotron(graph_path=graph_path, extraction_transport=recorder)
    await client.add_episode(
        name="new-user-chat",
        episode_body="The user asks how much evidence to include in the answer.",
        source=EpisodeType.TEXT,
        scope=user_scope,
        reference_time=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await client.run_due_dreams(now=datetime(2026, 6, 2, 0, 0, 2, tzinfo=UTC))

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert [memory.fact for memory in request.graph_context] == ["User 42 prefers answers with supporting evidence"]
    assert request.graph_context[0].scope == user_scope
    assert "Existing graph context:" in request.prompt
    assert "User 42 prefers answers with supporting evidence" in request.prompt
    assert "Acme requires vendor packet review" not in request.prompt


@pytest.mark.asyncio
async def test_formation_context_policy_filters_graph_context_by_metadata(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    seed_client = Memotron(graph_path=graph_path)

    await seed_client.add_episode(
        name="trusted-user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=answers with supporting evidence; "
            "relationship_type=PREFERS; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
        metadata={"source": "trusted_doc", "category": "support"},
    )
    await seed_client.add_episode(
        name="scratch-user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=speculative jokes; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, 1, tzinfo=UTC),
        metadata={"source": "scratchpad", "category": "support"},
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, 1, 0, 2, tzinfo=UTC))
    seed_client.graph.close()

    config = config_with_jobs(
        DreamJob(
            name="formation-filtered",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            context_policy=DreamContextPolicy(
                metadata_filter={"source": ("trusted_doc", "runbook")},
                include_metadata=True,
            ),
        ),
    )
    recorder = RecordingExtractionTransport()
    client = Memotron(
        graph_path=graph_path,
        config=config,
        extraction_transport=recorder,
    )
    await client.add_episode(
        name="new-user-chat",
        episode_body="The user asks how much evidence to include in the answer.",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await client.run_due_dreams(now=datetime(2026, 6, 2, 0, 0, 2, tzinfo=UTC))

    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert [memory.fact for memory in request.graph_context] == ["User 42 prefers answers with supporting evidence"]
    assert request.graph_context[0].metadata["source"] == "trusted_doc"
    assert "User 42 prefers answers with supporting evidence" in request.prompt
    assert "User 42 prefers speculative jokes" not in request.prompt


@pytest.mark.asyncio
async def test_formation_context_policy_can_disable_graph_context(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    seed_client = Memotron(graph_path=graph_path)

    await seed_client.add_episode(
        name="existing-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=brief answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, 0, 0, 2, tzinfo=UTC))
    seed_client.graph.close()

    config = config_with_jobs(
        DreamJob(
            name="formation-no-context",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            context_policy=DreamContextPolicy(enabled=False),
        ),
        DreamJob(name="pruning-no-context", kind=DreamJobKind.PRUNING, cadence_seconds=1),
    )
    recorder = RecordingExtractionTransport()
    client = Memotron(
        graph_path=graph_path,
        config=config,
        extraction_transport=recorder,
    )
    await client.add_episode(
        name="new-user-chat",
        episode_body="The user asks for answer style guidance.",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=datetime(2026, 6, 2, tzinfo=UTC),
    )
    await client.run_due_dreams(now=datetime(2026, 6, 2, 0, 0, 2, tzinfo=UTC))

    assert len(recorder.requests) == 1
    assert recorder.requests[0].graph_context == []
    assert "Existing graph context:\n\n[]" in recorder.requests[0].prompt


@pytest.mark.asyncio
async def test_extracted_node_properties_are_persisted_and_searchable(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="customer-property-memory",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "SOC2 report",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.93,
                        "subject_properties": {
                            "tier": "enterprise",
                            "region": "NA",
                        },
                        "object_properties": {
                            "kind": "compliance document",
                            "system": "audit portal",
                        },
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await client.add_episode(
        name="temporary-customer-property-memory",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "prefers",
                        "object": "temporary phone support",
                        "relationship_type": "PREFERS",
                        "confidence": 0.88,
                        "valid_from": "2026-06-01T13:00:00Z",
                        "valid_to": "2026-06-01T18:00:00Z",
                        "subject_properties": {
                            "tier": "enterprise",
                            "region": "NA",
                        },
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=datetime(2026, 6, 1, 13, tzinfo=UTC),
    )

    await client.run_due_dreams(now=datetime(2026, 6, 2, tzinfo=UTC))
    graph = client.export_graph()
    acme_node = next(node for node in graph["nodes"] if node["properties"].get("name") == "Acme Parks")
    report_node = next(node for node in graph["nodes"] if node["properties"].get("name") == "SOC2 report")
    tier_results = await client.search(query="enterprise", scope=scope)
    portal_results = await client.search(query="audit portal", scope=scope)

    assert acme_node["properties"]["tier"] == "enterprise"
    assert acme_node["properties"]["region"] == "NA"
    assert acme_node["valid_to"] is None
    assert report_node["properties"]["kind"] == "compliance document"
    assert report_node["properties"]["system"] == "audit portal"
    assert [result.object for result in tier_results] == ["SOC2 report"]
    assert [result.object for result in portal_results] == ["SOC2 report"]


@pytest.mark.asyncio
async def test_extracted_node_properties_rejected_when_not_configured(tmp_path: Path) -> None:
    """An unconfigured strict property key quarantines that CANDIDATE, not the run.

    This test used to assert the whole dreaming run raised.  Per-candidate
    schema rejection ([0024]) made the drop non-fatal and receipted; the
    knowledge-graph pipeline restructure moved property-key whitelisting from
    stage-1 (structural) to stage-3 (governance) — the candidate is still a
    structurally valid entity/relation, only the property key is policy, so it
    is retained in quarantine (inspectable, promotable, re-governable) instead
    of dropped.  Either way nothing materializes from the offending candidate
    and the run completes.
    """
    from memotron.receipts import ReceiptDecisionType

    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=strict_properties_config())
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="bad-node-property",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Acme Parks",
                        "predicate": "requires",
                        "object": "SOC2 report",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.93,
                        "subject_properties": {
                            "unconfigured_property": "should fail",
                        },
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert not [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]
    quarantined = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_QUARANTINED.value
    ]
    assert len(quarantined) == 1
    assert quarantined[0].decision_result == "quarantined"
    assert json.loads(quarantined[0].event_payload)["violation"] == "property_key_not_allowed"
    stored = client.graph.quarantined_candidate(quarantined[0].candidate_uuid)
    assert "subject_properties key 'unconfigured_property' is not allowed" in stored.detail


@pytest.mark.asyncio
async def test_consolidation_job_uses_canonical_rollup_reducer(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    seed_client = Memotron(graph_path=graph_path)

    await seed_client.add_episode(
        name="agent-order-lesson",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; object=ask for order id before escalation; "
            "relationship_type=SHOULD; confidence=0.86"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.add_episode(
        name="agent-sla-lesson",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; object=check SLA tier before escalation; "
            "relationship_type=SHOULD; confidence=0.87"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=datetime(2026, 6, 1, 1, tzinfo=UTC),
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, 1, 0, 2, tzinfo=UTC))
    seed_client.graph.close()

    from memotron.config import RollupConsolidationPolicy

    recorder = RecordingExtractionTransport()
    config = config_with_jobs(
        DreamJob(
            name="consolidate-agent",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=1,
            scope=scope,
            context_policy=DreamContextPolicy(max_relationships=5),
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.0,
                min_cluster_size=2,
                max_depth=1,
            ),
        )
    )
    client = Memotron(
        graph_path=graph_path,
        config=config,
        extraction_transport=recorder,
    )

    result = await client.run_due_dreams(now=datetime(2026, 6, 2, tzinfo=UTC))
    decisions = await client.dream_decisions(job_name="consolidate-agent")
    proof = await client.memory_evolution(scope=scope, as_of=datetime(2026, 6, 2, tzinfo=UTC))
    derived_relationships = [
        relationship
        for relationship in client.export_graph()["relationships"]
        if relationship["properties"].get("created_by") == "dream-agent:dream-agent:consolidation"
        and relationship["type"] == "ROLLUP"
    ]

    assert result.processed_episodes == 0
    assert result.processed_scopes == 1
    assert result.created_relationships == 1
    assert result.job_runs[0].decision_count == 2
    assert recorder.requests == []
    assert len(derived_relationships) == 1
    assert {decision.decision_type for decision in decisions} == {
        "consolidation_scope_selected",
        "consolidation_rollup_created",
    }
    selected = next(decision for decision in decisions if decision.decision_type == "consolidation_scope_selected")
    assert selected.scope == scope
    assert selected.details["graph_context_count"] == 2
    assert selected.details["mechanism"] == "dependency_tracked_rollup_reducer"
    assert len(derived_relationships[0]["properties"]["derived_from"]) == 2
    assert proof.consolidation_relationship_count == 1
    assert {signal.name: signal.observed for signal in proof.signals}["consolidation"] is True


@pytest.mark.asyncio
async def test_consolidation_job_respects_scope_batch_limit(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    seed_client = Memotron(graph_path=graph_path)

    await seed_client.add_episode(
        name="agent-lesson",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; object=confirm account id; "
            "relationship_type=SHOULD; confidence=0.86"
        ),
        source=EpisodeType.MESSAGE,
        scope=agent_scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.add_episode(
        name="user-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=sourced answers; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=user_scope,
        reference_time=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    seed_client.graph.close()

    recorder = RecordingExtractionTransport()
    config = config_with_jobs(
        DreamJob(
            name="consolidate-one-scope",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=1,
            max_items_per_run=1,
        )
    )
    client = Memotron(
        graph_path=graph_path,
        config=config,
        extraction_transport=recorder,
    )

    result = await client.run_due_dreams(now=datetime(2026, 6, 2, tzinfo=UTC))

    assert result.processed_scopes == 1
    assert recorder.requests == []
    decisions = await client.dream_decisions(job_name="consolidate-one-scope")
    selected = [decision for decision in decisions if decision.decision_type == "consolidation_scope_selected"]
    assert len(selected) == 1
    assert selected[0].scope == agent_scope


@pytest.mark.asyncio
async def test_scope_mismatch_rejects_the_candidate(tmp_path: Path) -> None:
    """A cross-scope candidate is rejected and receipted; it never materializes.

    Previously this aborted the run.  The scope guarantee is unchanged — the
    memory does not cross into the episode's scope — but the enforcement is now
    per-candidate ([0024]) so an unrelated sibling is not collateral damage.
    """
    from memotron.receipts import ReceiptDecisionType

    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="bad-scope",
        episode_body=(
            "Memory: scope=user:user-42; subject=Acme; predicate=requires; "
            "object=vendor questionnaire; relationship_type=REQUIRES; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=customer_scope,
    )

    await client.run_due_dreams()

    assert not [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "REQUIRES"
    ]
    rejected = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(customer_scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED.value
    ]
    assert len(rejected) == 1
    assert "does not match episode scope" in rejected[0].decision_reason
    assert json.loads(rejected[0].event_payload)["violation"] == "scope_mismatch"


@pytest.mark.asyncio
async def test_pruning_low_confidence_relationships(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")

    await client.add_episode(
        name="low-confidence",
        episode_body="Memory: subject=Agent; predicate=should; object=guess next steps; relationship_type=SHOULD; confidence=0.2",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )

    await client.run_due_dreams()
    statuses = [
        relationship["properties"]["status"]
        for relationship in client.export_graph()["relationships"]
        if relationship["type"] == "SHOULD"
    ]

    # Directives are protected from generic low-confidence pruning until an
    # explicit lifecycle/utility policy says otherwise.
    assert statuses == [RelationshipStatus.ACTIVE.value]


@pytest.mark.asyncio
async def test_queued_episode_survives_restart_before_dreaming(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    first_client = Memotron(graph_path=graph_path)
    await first_client.add_episode(
        name="queued",
        episode_body="Memory: subject=User 42; predicate=prefers; object=durable graph memory; relationship_type=PREFERS; confidence=0.91",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    first_client.graph.close()

    restarted_client = Memotron(graph_path=graph_path)
    result = await restarted_client.run_due_dreams()
    matches = await restarted_client.search(query="durable graph", scope=scope)

    assert result.processed_episodes == 1
    assert len(matches) == 1
    assert matches[0].object == "durable graph memory"


@pytest.mark.asyncio
async def test_graph_search_survives_restart_after_dreaming(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    client = Memotron(graph_path=graph_path)

    await client.add_episode(
        name="agent-lesson",
        episode_body="Memory: subject=Support Agent; predicate=should; object=confirm account id; relationship_type=SHOULD; confidence=0.86",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    client.graph.close()

    restarted_client = Memotron(graph_path=graph_path)
    matches = await restarted_client.search(query="account id", scope=scope)
    skipped = await restarted_client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))

    assert len(matches) == 1
    assert matches[0].fact == "Support Agent should confirm account id"
    assert skipped.job_runs == []


@pytest.mark.asyncio
async def test_dream_job_prompt_profile_version_reaches_extraction_prompt(tmp_path: Path) -> None:
    recorder = RecordingExtractionTransport()
    config = config_with_jobs(
        DreamJob(
            name="formation-support-v2",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            prompt_profile="support-memory",
            prompt_profile_version="v2",
        )
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=config,
        extraction_transport=recorder,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)

    await client.add_episode(
        name="support-v2-chat",
        episode_body="The user corrected the approval flow and named the customer tier.",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=run_time,
    )

    status = await client.dream_status(now=run_time)
    await client.run_due_dreams(now=run_time)

    assert status[0].prompt_profile == "support-memory"
    assert status[0].prompt_profile_version == "v2"
    assert status[0].prompt_override_enabled is False
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.prompt_profile.key == "support-memory@v2"
    assert "Dream prompt profile: support-memory@v2" in request.prompt
    assert "stronger emphasis on contradictions, supersession, and entity properties" in request.prompt
    assert "Entity properties such as customer tier" in request.prompt
    assert "Create only these relationship types" in request.prompt


@pytest.mark.asyncio
async def test_dream_job_prompt_override_layers_on_selected_profile(tmp_path: Path) -> None:
    recorder = RecordingExtractionTransport()
    override = DreamPromptOverride(
        goal="Extract only durable admin preference memories for this deployment.",
        include=("Admin override: billing export format preferences.",),
        exclude=("Skip support obligations in this job.",),
        rules=("Use PREFERS only when the preference should survive future sessions.",),
        examples=("User 42 prefers CSV exports for billing reports.",),
    )
    config = config_with_jobs(
        DreamJob(
            name="formation-preferences",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            prompt_profile="preference-memory",
            prompt_profile_version="v1",
            prompt_override=override,
        )
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=config,
        extraction_transport=recorder,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)

    await client.add_episode(
        name="preference-chat",
        episode_body="The user said they always want billing exports as CSV files.",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=run_time,
    )

    status = await client.dream_status(now=run_time)
    await client.run_due_dreams(now=run_time)

    assert status[0].prompt_profile == "preference-memory"
    assert status[0].prompt_profile_version == "v1"
    assert status[0].prompt_override_enabled is True
    assert len(recorder.requests) == 1
    request = recorder.requests[0]
    assert request.prompt_profile.key == "preference-memory@v1"
    assert request.prompt_profile.goal == "Extract only durable admin preference memories for this deployment."
    assert "Repeated or explicit preferences about answer style" in request.prompt
    assert "Admin override: billing export format preferences." in request.prompt
    assert "Skip support obligations in this job." in request.prompt
    assert "Use PREFERS only when the preference should survive future sessions." in request.prompt
    assert "User 42 prefers CSV exports for billing reports." in request.prompt
    assert "REQUIRES" in request.prompt


@pytest.mark.asyncio
async def test_dream_agent_decisions_are_recorded_for_formation_pruning_and_restart(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="truth-curator")
    dream_agent = DreamAgentConfig(
        agent_id="truth-curator",
        name="Truth Curator",
        scope=agent_scope,
        decision_policy="Approve only auditable memory maintenance decisions.",
    )
    config = config_with_jobs(
        DreamJob(
            name="formation-agent",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            agent=dream_agent,
        ),
        DreamJob(
            name="pruning-agent",
            kind=DreamJobKind.PRUNING,
            cadence_seconds=1,
            agent=dream_agent,
        ),
        pruning=PruningPolicy(min_confidence=0.95),
    )
    client = Memotron(graph_path=graph_path, config=config)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)

    await client.add_episode(
        name="low-confidence-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=speculative answers; "
            "relationship_type=PREFERS; confidence=0.5"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=run_time,
    )

    statuses = await client.dream_status(now=run_time)
    result = await client.run_due_dreams(now=run_time)
    decisions = await client.dream_decisions(limit=10, agent_id="truth-curator")
    history = await client.dream_history(limit=10)
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    client.graph.close()

    restarted = Memotron(graph_path=graph_path, config=config)
    restarted_formation_decisions = await restarted.dream_decisions(job_name="formation-agent")

    assert {status.agent_id for status in statuses} == {"truth-curator"}
    assert {run.job_name: run.decision_count for run in result.job_runs} == {
        "formation-agent": 1,
        "pruning-agent": 1,
    }
    assert {record.job_name: record.decision_count for record in history} == {
        "formation-agent": 1,
        "pruning-agent": 1,
    }
    assert {decision.decision_type for decision in decisions} == {
        "formation_episode_selected",
        "pruning_relationship_pruned",
    }
    assert {decision.agent_name for decision in decisions} == {"Truth Curator"}
    assert all(decision.details["approved"] is True for decision in decisions)
    assert all(decision.details["transport"] == "local" for decision in decisions)
    assert relationships[0]["properties"]["created_by"] == "dream-agent:truth-curator:formation"
    assert relationships[0]["properties"]["status"] == RelationshipStatus.PRUNED.value
    assert len(restarted_formation_decisions) == 1
    assert restarted_formation_decisions[0].subject_name == "low-confidence-preference"


@pytest.mark.asyncio
async def test_dream_agent_transport_fails_fast_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMOTRON_MISSING_GATEWAY_KEY", raising=False)
    transport = OpenAICompatibleDreamAgentTransport(
        api_key_env="MEMOTRON_MISSING_GATEWAY_KEY",
    )
    request = DreamAgentDecisionRequest(
        agent_id="truth-curator",
        agent_name="Truth Curator",
        decision_policy="Approve auditable memory maintenance decisions.",
        job_name="formation-agent",
        job_kind=DreamJobKind.FORMATION,
        decision_type="formation_episode_selected",
        proposed_action="Process this queued episode for memory formation.",
        subject_id="episode-1",
        subject_name="episode one",
        scope=MemoryScope(kind=ScopeKind.USER, scope_id="user-42"),
    )

    with pytest.raises(ValueError, match="missing required environment variable"):
        await transport.decide(request)


def test_dream_config_rejects_unknown_prompt_profile() -> None:
    base = default_config()

    with pytest.raises(ValueError, match="dream jobs reference unknown prompt profiles"):
        DreamConfig(
            instruction_sets=base.instruction_sets,
            jobs=(
                DreamJob(
                    name="formation-missing-profile",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    prompt_profile="missing-profile",
                    prompt_profile_version="v1",
                ),
            ),
        )


@pytest.mark.asyncio
async def test_dream_history_survives_restart_and_filters_by_job_name(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    run_time = datetime(2026, 6, 1, tzinfo=UTC)
    client = Memotron(graph_path=graph_path)

    await client.add_episode(
        name="preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=durable dream history; "
            "relationship_type=PREFERS; confidence=0.91"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=run_time,
    )
    first_run = await client.run_due_dreams(now=run_time)
    skipped_run = await client.run_due_dreams(now=run_time)
    live_history = await client.dream_history(limit=10)
    client.graph.close()

    restarted_client = Memotron(graph_path=graph_path)
    formation_history = await restarted_client.dream_history(
        limit=1,
        job_name="formation-default",
    )
    pruning_history = await restarted_client.dream_history(
        limit=1,
        job_name="pruning-default",
    )

    assert first_run.processed_episodes == 1
    assert skipped_run.job_runs == []
    assert {record.job_name for record in live_history} == {
        "formation-default",
        "pruning-default",
    }
    assert len(live_history) == 2
    assert formation_history[0].ran_at == run_time
    assert formation_history[0].processed_episodes == 1
    assert formation_history[0].created_relationships == 1
    assert pruning_history[0].ran_at == run_time
    assert pruning_history[0].pruned_relationships == 0


def test_instruction_prompt_contains_llm_contract(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    prompt = client.config.instruction_set("default").render_prompt()
    profile_prompt = client.config.instruction_set("default").render_prompt(
        prompt_profile=client.config.prompt_profile("support-memory", "v1")
    )

    assert "Create only these node labels" in prompt
    assert "Create only these relationship types" in prompt
    assert "JSON object" in prompt
    assert "Truth cardinality" in prompt
    assert "valid_from" in prompt
    assert "subject_properties" in prompt
    assert "object_properties" in prompt
    # [0025]: the per-node property whitelist ("Allowed properties: kind, role,
    # ...", generated from NodeInstruction.properties) no longer leaks into the
    # prompt -- it is validation-time governance, not extraction guidance.
    assert "Allowed properties" not in prompt
    assert "Dream prompt profile: support-memory@v1" in profile_prompt
    assert "Memory formation goal:" in profile_prompt
    assert "Stable user preferences" in profile_prompt


@pytest.mark.asyncio
async def test_openai_compatible_transport_forms_memory_from_http_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    server, received = openai_compatible_test_server(
        {
            "memories": [
                {
                    "subject": "User 42",
                    "predicate": "prefers",
                    "object": "answers with supporting evidence",
                    "relationship_type": "PREFERS",
                    "confidence": 0.92,
                }
            ]
        }
    )
    monkeypatch.setenv("MEMOTRON_TEST_OPENAI_KEY", "test-key")
    transport = OpenAICompatibleExtractionTransport(
        model="memory-extractor",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key_env="MEMOTRON_TEST_OPENAI_KEY",
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=transport,
    )

    try:
        await client.add_episode(
            name="unstructured-user-chat",
            episode_body="The user asked for answers that cite the evidence behind conclusions.",
            source=EpisodeType.TEXT,
            scope=scope,
            reference_time=datetime(2026, 6, 1, tzinfo=UTC),
        )
        result = await client.run_due_dreams(now=datetime(2026, 6, 1, 0, 0, 2, tzinfo=UTC))
        matches = await client.search(query="supporting evidence", scope=scope)
    finally:
        server.shutdown()
        server.server_close()

    assert result.processed_episodes == 1
    assert len(matches) == 1
    assert matches[0].object == "answers with supporting evidence"
    assert received[0]["path"] == "/v1/chat/completions"
    assert received[0]["authorization"] == "Bearer test-key"
    assert received[0]["body"]["model"] == "memory-extractor"
    assert "Create only these relationship types" in received[0]["body"]["messages"][1]["content"]
    assert "user:user-42" in received[0]["body"]["messages"][1]["content"]


@pytest.mark.asyncio
async def test_llm_corroboration_is_emitted_reinforced_and_contract_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A prompt-capturing LLM transport proves graph context no longer suppresses evidence."""
    from memotron.attestations import verify_formation_contract_attestation

    scope = MemoryScope(kind=ScopeKind.USER, scope_id="corroboration-user")
    candidate = {
        "subject": "User 42",
        "predicate": "prefers",
        "object": "answers with supporting evidence",
        "relationship_type": "PREFERS",
        "confidence": 0.92,
        "claim_mode": "preference",
        "source_text": "I still want answers with supporting evidence.",
    }
    server, received = openai_compatible_test_server({"memories": [candidate]})
    monkeypatch.setenv("MEMOTRON_CORROBORATION_KEY", "test-key")
    transport = OpenAICompatibleExtractionTransport(
        model="prompt-capturing-memory-extractor",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key_env="MEMOTRON_CORROBORATION_KEY",
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=transport,
    )
    t1 = datetime(2026, 7, 15, tzinfo=UTC)
    try:
        await client.add_episode(
            name="initial-preference",
            episode_body="I want answers with supporting evidence.",
            source=EpisodeType.TEXT,
            scope=scope,
            reference_time=t1,
        )
        first = await client.run_due_dreams(now=t1)
        await client.add_episode(
            name="independent-corroboration",
            episode_body="I still want answers with supporting evidence.",
            source=EpisodeType.TEXT,
            scope=scope,
            reference_time=t1 + timedelta(seconds=2),
        )
        second = await client.run_due_dreams(now=t1 + timedelta(seconds=2))
    finally:
        server.shutdown()
        server.server_close()

    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    assert len(relationships) == 1
    assert relationships[0]["properties"]["observed_count"] == 2
    assert relationships[0]["properties"]["claim_mode"] == "preference"
    assert relationships[0]["properties"]["directive_stance"] == "prefer"
    assert "emit it when this episode independently corroborates it" in received[1]["body"]["messages"][0]["content"]
    assert "Existing graph context" in received[1]["body"]["messages"][1]["content"]

    assert first.job_runs[0].run_uuid is not None
    assert second.job_runs[0].run_uuid is not None
    receipts = await client.memory_receipts(run_uuid=second.job_runs[0].run_uuid)
    extracted = [r for r in receipts if r.decision_type.value == "candidate_extracted"]
    reinforced = [r for r in receipts if r.decision_type.value == "formation_semantic_dedup_reinforced"]
    assert len(extracted) == len(reinforced) == 1
    for receipt in [*extracted, *reinforced]:
        assert receipt.formation_contract_digest
        assert receipt.formation_contract_source_trace
        assert receipt.claim_mode == "preference"
        assert receipt.directive_stance == "prefer"
        attestation = json.loads(receipt.formation_contract_attestation or "{}")
        from memotron.models import FormationContractAttestation

        assert verify_formation_contract_attestation(FormationContractAttestation.model_validate(attestation))


@pytest.mark.asyncio
async def test_multi_active_memory_stance_conflict_is_detected_at_formation(tmp_path: Path) -> None:
    from memotron import CoherencePolicy

    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="stance-conflict-agent")
    config = config_with_jobs(
        DreamJob(
            name="formation",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            coherence_policy=CoherencePolicy(identity_threshold=0.0),
        )
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    await client.add_episode(
        name="directive",
        episode_body=(
            "Memory: subject=Agent; predicate=should; object=export invoices as CSV; "
            "relationship_type=SHOULD; confidence=0.9; claim_mode=directive"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
    )
    await client.add_episode(
        name="correction",
        episode_body=(
            "Memory: subject=Agent; predicate=should; object=never export invoices automatically; "
            "relationship_type=SHOULD; confidence=0.95; claim_mode=correction"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
    )

    await client.run_due_dreams()

    decisions = await client.dream_decisions(limit=20)
    conflicts = [
        decision for decision in decisions if decision.decision_type == "coherence_cross_artifact_contradiction"
    ]
    assert len(conflicts) == 1
    incident = conflicts[0].details["incident"]
    assert {directive["stance"] for directive in incident["conflicting_directives"]} == {
        "require",
        "assert",
    }
    assert {directive["artifact_class"] for directive in incident["conflicting_directives"]} == {"memory"}


@pytest.mark.asyncio
async def test_openai_compatible_transport_fails_fast_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMOTRON_MISSING_OPENAI_KEY", raising=False)
    transport = OpenAICompatibleExtractionTransport(
        model="memory-extractor",
        base_url="http://127.0.0.1:9/v1",
        api_key_env="MEMOTRON_MISSING_OPENAI_KEY",
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=transport,
    )
    await client.add_episode(
        name="needs-llm",
        episode_body="Remember that the user wants sourced answers.",
        source=EpisodeType.TEXT,
        scope=MemoryScope(kind=ScopeKind.USER, scope_id="user-42"),
    )

    with pytest.raises(ValueError, match="missing required environment variable"):
        await client.run_due_dreams()


# ── Session ingestion tests ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_memory_materializes_client_managed_fact_without_dreaming(tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)
    recorder = RecordingExtractionTransport(
        response_memories=[
            {
                "subject": "User 42",
                "predicate": "prefers",
                "object": "wrong extractor path",
                "relationship_type": "PREFERS",
                "confidence": 0.99,
            }
        ]
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=recorder,
    )

    result = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="dark mode",
        relationship_type="PREFERS",
        confidence=0.92,
        scope=scope,
        reference_time=t0,
        source_text="User said they prefer dark mode.",
        metadata={"source": "settings_panel"},
    )
    status = await client.dream_status(now=t0)
    dream_result = await client.run_due_dreams(now=t0)
    matches = await client.search(query="dark mode", scope=scope)
    evidence = await client.memory_evidence(relationship_uuid=result.relationship_uuid, scope=scope)

    assert result.memory_mode == MemoryIngestionMode.CLIENT_MANAGED
    assert result.queued_for_dreaming is False
    assert result.created_relationships == 1
    assert result.reinforced_relationships == 0
    assert result.superseded_relationships == 0
    assert result.fact == "User 42 prefers dark mode"
    assert status[0].pending_episodes == 0
    assert dream_result.processed_episodes == 0
    assert recorder.requests == []
    assert len(matches) == 1
    assert matches[0].relationship_uuid == result.relationship_uuid
    assert evidence.created_by == "client-managed-memory"
    assert evidence.source_text == "User said they prefer dark mode."
    assert evidence.metadata["client_managed_memory"] is True
    assert evidence.metadata["memory_mode"] == "client_managed"
    assert evidence.episodes[0].metadata["source"] == "settings_panel"
    assert client.graph.is_episode_processed(result.episode_uuid)


@pytest.mark.asyncio
async def test_add_memory_reinforces_existing_client_managed_fact(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)

    first = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        confidence=0.70,
        scope=scope,
        reference_time=t0,
    )
    second = await client.add_memory(
        subject="User 42",
        predicate="prefers",
        object="concise answers",
        relationship_type="PREFERS",
        confidence=0.95,
        scope=scope,
        reference_time=t0 + timedelta(seconds=1),
    )
    matches = await client.search(query="concise answers", scope=scope)
    evidence = await client.memory_evidence(relationship_uuid=second.relationship_uuid, scope=scope)

    assert first.relationship_uuid == second.relationship_uuid
    assert second.created_relationships == 0
    assert second.reinforced_relationships == 1
    # WS-16 T13 evidence accumulation: created at 0.70, reinforced by a 0.95
    # observation contributing gain=0.25 of the remaining headroom.
    assert matches[0].confidence == pytest.approx(min(0.99, 0.70 + (1.0 - 0.70) * 0.25 * 0.95))
    assert matches[0].observed_count == 2
    assert evidence.episode_uuids == [first.episode_uuid, second.episode_uuid]


@pytest.mark.asyncio
async def test_add_memory_fails_fast_before_storage_for_invalid_relationship_type(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    with pytest.raises(ValueError, match="relationship type 'BLOCKS' is not allowed"):
        await client.add_memory(
            subject="User 42",
            predicate="blocks",
            object="unsupported direct memory",
            relationship_type="BLOCKS",
            confidence=0.9,
            scope=scope,
        )

    assert client.graph.episodes() == []


@pytest.mark.asyncio
async def test_add_session_groups_turns_into_windowed_episodes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)

    turns = [
        ConversationTurn(role="user", content=f"message {i}", timestamp=t0 + timedelta(seconds=i)) for i in range(5)
    ]
    result = await client.add_session(
        name="test-chat",
        turns=turns,
        scope=scope,
        turns_per_episode=3,
        time_gap_seconds=None,
    )

    queued = client.graph.episodes()

    assert result.turns_ingested == 5
    assert result.windows_created == 2
    assert result.episodes_created == 2
    assert len(result.episode_uuids) == 2
    assert result.queued_for_dreaming is True
    assert result.scope_keys == ["user:user-42"]
    assert len(queued) == 2
    assert queued[0].name == "test-chat [window 1]"
    assert queued[1].name == "test-chat [window 2]"
    assert queued[0].metadata["session_turn_count"] == 3
    assert queued[1].metadata["session_turn_count"] == 2
    assert queued[0].metadata["session_window_index"] == 0
    assert queued[1].metadata["session_window_index"] == 1
    assert queued[0].metadata["session_scope_key"] == "user:user-42"


@pytest.mark.asyncio
async def test_add_session_episode_body_is_formatted_transcript(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 6, 4, 10, 0, 5, tzinfo=UTC)

    await client.add_session(
        name="support-chat",
        turns=[
            ConversationTurn(role="user", content="We need ISO27001 first.", timestamp=t0),
            ConversationTurn(role="assistant", content="Noted.", timestamp=t1),
        ],
        scope=scope,
        turns_per_episode=8,
    )

    episode = client.graph.episodes()[0]
    assert f"[{t0.isoformat()}] user: We need ISO27001 first." in episode.body
    assert f"[{t1.isoformat()}] assistant: Noted." in episode.body
    assert episode.reference_time == t1
    assert episode.source.value == "message"


@pytest.mark.asyncio
async def test_add_session_transcript_without_timestamps(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await client.add_session(
        name="timeless-chat",
        turns=[
            ConversationTurn(role="user", content="first message"),
            ConversationTurn(role="assistant", content="first reply"),
        ],
        scope=scope,
    )

    episode = client.graph.episodes()[0]
    assert "user: first message" in episode.body
    assert "assistant: first reply" in episode.body
    assert "[" not in episode.body


@pytest.mark.asyncio
async def test_add_session_windowing_by_char_limit(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    long_content = "x" * 200

    result = await client.add_session(
        name="big-chat",
        turns=[ConversationTurn(role="user", content=long_content) for _ in range(5)],
        scope=scope,
        turns_per_episode=100,
        max_chars_per_episode=400,
        time_gap_seconds=None,
    )

    assert result.windows_created > 1
    assert result.turns_ingested == 5


@pytest.mark.asyncio
async def test_add_session_windowing_by_time_gap(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)

    result = await client.add_session(
        name="gapped-chat",
        turns=[
            ConversationTurn(role="user", content="early message", timestamp=t0),
            ConversationTurn(role="assistant", content="early reply", timestamp=t0 + timedelta(seconds=10)),
            ConversationTurn(role="user", content="late message", timestamp=t0 + timedelta(seconds=400)),
            ConversationTurn(role="assistant", content="late reply", timestamp=t0 + timedelta(seconds=410)),
        ],
        scope=scope,
        turns_per_episode=100,
        max_chars_per_episode=100_000,
        time_gap_seconds=300,
    )

    assert result.windows_created == 2
    episodes = client.graph.episodes()
    assert "early message" in episodes[0].body
    assert "late message" in episodes[1].body
    # window 1 ends at t0+10s (the last turn before the gap)
    assert episodes[0].reference_time == t0 + timedelta(seconds=10)
    # window 2 starts with the turn that crossed the gap
    assert "late reply" in episodes[1].body


@pytest.mark.asyncio
async def test_add_session_single_window_when_under_all_thresholds(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    result = await client.add_session(
        name="short-chat",
        turns=[
            ConversationTurn(role="user", content="quick question"),
            ConversationTurn(role="assistant", content="quick answer"),
        ],
        scope=scope,
    )

    assert result.windows_created == 1
    assert result.episodes_created == 1


@pytest.mark.asyncio
async def test_add_session_empty_turns_creates_no_episodes(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    result = await client.add_session(name="empty-chat", turns=[], scope=scope)

    assert result.turns_ingested == 0
    assert result.windows_created == 0
    assert result.episodes_created == 0
    assert client.graph.episodes() == []


@pytest.mark.asyncio
async def test_add_session_preserves_session_id_across_windows(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    result = await client.add_session(
        name="multi-window",
        turns=[ConversationTurn(role="user", content=f"turn {i}") for i in range(6)],
        scope=scope,
        turns_per_episode=2,
        time_gap_seconds=None,
        session_id="my-session-abc",
    )

    episodes = client.graph.episodes()
    session_ids = {ep.metadata["session_id"] for ep in episodes}

    assert result.session_id == "my-session-abc"
    assert session_ids == {"my-session-abc"}
    assert result.windows_created == 3


@pytest.mark.asyncio
async def test_open_session_streaming_with_context_manager(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)

    async with client.open_session(
        name="streaming-chat",
        scope=scope,
        turns_per_episode=2,
        time_gap_seconds=None,
    ) as session:
        await session.add_turn("user", "first", timestamp=t0)
        await session.add_turn("assistant", "second", timestamp=t0 + timedelta(seconds=1))
        await session.add_turn("user", "third", timestamp=t0 + timedelta(seconds=2))

    episodes = client.graph.episodes()

    assert len(episodes) == 2
    assert "first" in episodes[0].body
    assert "second" in episodes[0].body
    assert "third" in episodes[1].body


@pytest.mark.asyncio
async def test_open_session_returns_session_ingester(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    session = client.open_session(name="type-check", scope=scope)
    assert isinstance(session, SessionIngester)
    await session.close()


@pytest.mark.asyncio
async def test_session_ingester_close_twice_raises(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    session = client.open_session(name="double-close", scope=scope)
    await session.close()

    with pytest.raises(ValueError, match="already closed"):
        await session.close()


@pytest.mark.asyncio
async def test_session_ingester_add_turn_after_close_raises(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    session = client.open_session(name="post-close", scope=scope)
    await session.close()

    with pytest.raises(ValueError, match="already closed"):
        await session.add_turn("user", "too late")


@pytest.mark.asyncio
async def test_add_session_integrates_with_dream_engine(tmp_path: Path) -> None:
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t0 = datetime(2026, 6, 4, 10, 0, 0, tzinfo=UTC)

    recorder = RecordingExtractionTransport(
        response_memories=[
            {
                "subject": "Acme Parks",
                "predicate": "requires",
                "object": "ISO27001 audit",
                "relationship_type": "REQUIRES",
                "confidence": 0.95,
            }
        ]
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=recorder,
    )

    session_result = await client.add_session(
        name="compliance-chat",
        turns=[
            ConversationTurn(role="user", content="We need ISO27001 before any vendor sign-off.", timestamp=t0),
            ConversationTurn(
                role="assistant",
                content="Noted — logging that compliance requirement.",
                timestamp=t0 + timedelta(seconds=5),
            ),
        ],
        scope=scope,
    )
    dream_result = await client.run_due_dreams(now=t0 + timedelta(seconds=10))
    matches = await client.search(query="ISO27001 audit", scope=scope)

    assert session_result.episodes_created == 1
    assert dream_result.processed_episodes == 1
    assert len(recorder.requests) == 1
    assert "We need ISO27001" in recorder.requests[0].prompt
    assert recorder.requests[0].episode.metadata["session_name"] == "compliance-chat"
    assert len(matches) == 1
    assert matches[0].subject == "Acme Parks"
    assert matches[0].object == "ISO27001 audit"


@pytest.mark.asyncio
async def test_add_session_episode_metadata_carries_custom_fields(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    result = await client.add_session(
        name="tagged-chat",
        turns=[ConversationTurn(role="user", content="hello")],
        scope=scope,
        metadata={"ticket": "sup-999", "channel": "web"},
    )

    episode = client.graph.episodes()[0]
    assert episode.metadata["ticket"] == "sup-999"
    assert episode.metadata["channel"] == "web"
    assert episode.metadata["session_name"] == "tagged-chat"
    assert episode.metadata["session_id"] == result.session_id


# ── Config / prompt library tests ─────────────────────────────────────────────


def test_builtin_prompt_library_includes_research_temporal_profiles() -> None:
    from memotron.config import default_prompt_profiles

    profiles = {p.key for p in default_prompt_profiles()}
    assert "research-temporal@v1" in profiles
    assert "research-temporal@v2" in profiles
    assert "support-memory@v1" in profiles
    assert "agent-lessons@v1" in profiles


def test_research_temporal_v1_has_few_shot_examples() -> None:
    from memotron.config import default_prompt_profiles

    profile = next(p for p in default_prompt_profiles() if p.key == "research-temporal@v1")
    assert len(profile.few_shot_examples) >= 3
    for ex in profile.few_shot_examples:
        assert ex.episode_body.strip()
        assert len(ex.expected_memories) >= 1


def test_few_shot_example_render_contains_episode_and_output() -> None:
    from memotron import FewShotExample

    ex = FewShotExample(
        description="test example",
        episode_body="Acme requires SOC2 before vendor approval.",
        reference_time_note="reference_time = 2026-06-01T00:00:00Z",
        expected_memories=(
            {
                "subject": "Acme",
                "predicate": "requires",
                "object": "SOC2 before vendor approval",
                "relationship_type": "REQUIRES",
                "confidence": 0.95,
                "valid_from": "2026-06-01T00:00:00Z",
                "valid_to": None,
            },
        ),
    )
    rendered = ex.render(1)
    assert "--- Example 1 (test example) ---" in rendered
    assert "Acme requires SOC2 before vendor approval." in rendered
    assert "2026-06-01T00:00:00Z" in rendered
    # The example must be shown in the SAME envelope the prompt demands
    # ("Return exactly one JSON object with an entities array and a relations
    # array").  Rendering the stored flat shape put {"memories": [...]} under
    # "Expected output", contradicting the instruction -- and a worked example
    # outranks an instruction, so the model copied it.
    assert '"entities"' in rendered
    assert '"relations"' in rendered
    assert '"memories"' not in rendered
    assert '"source": "Acme"' in rendered
    assert '"name": "Acme"' in rendered


def test_few_shot_examples_appear_in_profile_render() -> None:
    from memotron import DreamPromptProfile, FewShotExample

    profile = DreamPromptProfile(
        name="test-profile",
        version="v1",
        goal="Test extraction.",
        few_shot_examples=(
            FewShotExample(
                description="basic extraction",
                episode_body="User prefers short answers.",
                expected_memories=(
                    {
                        "subject": "User",
                        "predicate": "prefers",
                        "object": "short answers",
                        "relationship_type": "PREFERS",
                        "confidence": 0.9,
                        "valid_from": "2026-01-01T00:00:00Z",
                        "valid_to": None,
                    },
                ),
            ),
        ),
    )
    rendered = profile.render_prompt()
    assert "Few-shot extraction examples:" in rendered
    assert "--- Example 1 (basic extraction) ---" in rendered
    assert "User prefers short answers." in rendered


def test_dream_instruction_set_has_research_grade_system_prompt() -> None:
    from memotron import DEFAULT_EXTRACTION_SYSTEM_PROMPT
    from memotron.config import default_config

    config = default_config()
    instructions = config.instruction_set("default")
    assert instructions.system_prompt == DEFAULT_EXTRACTION_SYSTEM_PROMPT
    assert "TEMPORAL EXTRACTION RULES" in instructions.system_prompt
    assert "CONFIDENCE CALIBRATION" in instructions.system_prompt
    assert "GRAPH CONTEXT HANDLING" in instructions.system_prompt
    assert "OUTPUT FORMAT" in instructions.system_prompt


def test_dream_config_rejects_duplicate_instruction_set_names() -> None:
    from memotron.config import default_config

    base = default_config()
    with pytest.raises(ValueError, match="instruction set names must be unique"):
        DreamConfig(
            instruction_sets=(*base.instruction_sets, *base.instruction_sets),
            jobs=base.jobs,
        )


def test_dream_config_rejects_duplicate_prompt_profile_keys() -> None:
    from memotron.config import _base_prompt_profiles, default_config

    base = default_config()
    duped = _base_prompt_profiles()
    with pytest.raises(ValueError, match="prompt profile name/version pairs must be unique"):
        DreamConfig(
            instruction_sets=base.instruction_sets,
            prompt_profiles=(*duped, duped[0]),
            jobs=base.jobs,
        )


def test_dream_agent_transport_defaults_to_the_gateway() -> None:
    from memotron import OpenAICompatibleDreamAgentTransport
    from memotron.gateway import (
        DEFAULT_GATEWAY_BASE_URL,
        DEFAULT_GATEWAY_MODEL,
        GATEWAY_API_KEY_ENV,
    )

    transport = OpenAICompatibleDreamAgentTransport(model="claude-sonnet-4-6")
    assert transport.model == "claude-sonnet-4-6"
    # Bare construction must reach the JedAI Gateway with the gateway key.
    default_transport = OpenAICompatibleDreamAgentTransport()
    assert default_transport.model == DEFAULT_GATEWAY_MODEL == "claude-haiku-4-5"
    assert default_transport.base_url == DEFAULT_GATEWAY_BASE_URL
    assert default_transport.api_key_env == GATEWAY_API_KEY_ENV == "LITELLM_API_KEY"
    with pytest.raises(ValueError, match="model cannot be blank"):
        OpenAICompatibleDreamAgentTransport(model="   ")


@pytest.mark.asyncio
async def test_gateway_extraction_transport_fails_fast_without_api_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memotron import OpenAICompatibleExtractionTransport

    monkeypatch.delenv("MEMOTRON_MISSING_GATEWAY_KEY", raising=False)
    transport = OpenAICompatibleExtractionTransport(api_key_env="MEMOTRON_MISSING_GATEWAY_KEY")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        extraction_transport=transport,
    )
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-1")
    await client.add_episode(
        name="test",
        episode_body="Test content.",
        source=EpisodeType.TEXT,
        scope=scope,
    )
    with pytest.raises(ValueError, match="missing required environment variable"):
        await client.run_due_dreams()


# ── Ingestion edge cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_add_episode_bulk_queues_all_episodes(tmp_path: Path) -> None:
    from memotron.models import Episode

    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    episodes = [
        Episode(
            name=f"ep-{i}",
            body=f"Memory: subject=User 42; predicate=prefers; object=option {i}; relationship_type=PREFERS; confidence=0.9",
            source=EpisodeType.MESSAGE,
            scope=scope,
        )
        for i in range(3)
    ]

    results = await client.add_episode_bulk(episodes)
    queued = client.graph.episodes()

    assert len(results) == 3
    assert all(r.queued_for_dreaming for r in results)
    assert {r.episode_uuid for r in results} == {ep.uuid for ep in episodes}
    assert len(queued) == 3


@pytest.mark.asyncio
async def test_add_context_to_multiple_scopes_creates_one_episode_per_scope(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    customer_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    content = "Memory: subject=Support Agent; predicate=should; object=check order id; relationship_type=SHOULD; confidence=0.85"

    result = await client.add_context(
        name="shared-setting",
        content=content,
        scopes=[customer_scope, user_scope],
    )
    episodes = client.graph.episodes()

    assert result.chunks_created == 1
    assert result.episodes_created == 2
    assert len(episodes) == 2
    assert {ep.scope.key for ep in episodes} == {customer_scope.key, user_scope.key}
    assert all(ep.metadata["shadow_source_name"] == "shared-setting" for ep in episodes)


@pytest.mark.asyncio
async def test_open_session_manual_flush_emits_partial_window(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    session = client.open_session(name="manual-flush", scope=scope, turns_per_episode=10, time_gap_seconds=None)
    await session.add_turn("user", "first message")
    await session.add_turn("assistant", "first reply")
    flushed = await session.flush()
    await session.add_turn("user", "second message")
    await session.close()

    episodes = client.graph.episodes()

    assert len(flushed) == 1
    assert len(episodes) == 2
    assert "first message" in episodes[0].body
    assert "second message" in episodes[1].body


# ── Retrieval edge cases ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_with_include_statuses_returns_superseded_relationships(tmp_path: Path) -> None:
    # Use a long superseded_retention so pruning does not immediately retire the old fact.
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.1, superseded_retention_seconds=86400),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 and ISO27001; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=t2 + timedelta(seconds=2))

    current_only = await client.search(query="SOC2", scope=scope)
    with_superseded = await client.search(
        query="SOC2",
        scope=scope,
        include_statuses={RelationshipStatus.ACTIVE, RelationshipStatus.SUPERSEDED},
    )

    assert len(current_only) == 1
    assert current_only[0].object == "SOC2 and ISO27001"
    assert len(with_superseded) == 2
    statuses = {r.status for r in with_superseded}
    assert RelationshipStatus.ACTIVE in statuses
    assert RelationshipStatus.SUPERSEDED in statuses


@pytest.mark.asyncio
async def test_profile_with_as_of_returns_historical_active_facts(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 5, tzinfo=UTC)

    await client.add_episode(
        name="old-requirement",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9 form; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="new-requirement",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=t2 + timedelta(seconds=2))

    historical_profile = await client.profile(scope=scope, as_of=t1 + timedelta(hours=1))
    current_profile = await client.profile(scope=scope, as_of=t2 + timedelta(hours=1))
    all_historical_objects = {f.object for f in historical_profile.static_facts + historical_profile.dynamic_facts}
    all_current_objects = {f.object for f in current_profile.static_facts + current_profile.dynamic_facts}

    assert "W9 form" in all_historical_objects
    assert "SOC2 report" not in all_historical_objects
    assert "SOC2 report" in all_current_objects
    assert "W9 form" not in all_current_objects


@pytest.mark.asyncio
async def test_truth_timeline_returns_empty_for_unknown_subject(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="ep",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    timeline = await client.truth_timeline(
        scope=scope,
        subject="NoSuchEntity",
        predicate="requires",
        relationship_type="REQUIRES",
    )

    assert timeline == []


@pytest.mark.asyncio
async def test_truth_timeline_include_statuses_filters_entries(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=t2 + timedelta(seconds=2))

    full_timeline = await client.truth_timeline(
        scope=scope, subject="Acme", predicate="requires", relationship_type="REQUIRES"
    )
    active_only = await client.truth_timeline(
        scope=scope,
        subject="Acme",
        predicate="requires",
        relationship_type="REQUIRES",
        include_statuses={RelationshipStatus.ACTIVE},
    )

    assert len(full_timeline) == 2
    assert len(active_only) == 1
    assert active_only[0].object == "SOC2"
    assert active_only[0].is_current is True


@pytest.mark.asyncio
async def test_entity_neighborhood_limit_is_enforced(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    for i in range(5):
        await client.add_episode(
            name=f"ep-{i}",
            episode_body=f"Memory: subject=User 42; predicate=prefers; object=option {i}; relationship_type=PREFERS; confidence=0.9",
            source=EpisodeType.MESSAGE,
            scope=scope,
        )
    await client.run_due_dreams()

    unlimited = await client.entity_neighborhood(scope=scope, entity="User 42")
    limited = await client.entity_neighborhood(scope=scope, entity="User 42", limit=2)

    assert len(unlimited) == 5
    assert len(limited) == 2


@pytest.mark.asyncio
async def test_search_limit_is_enforced(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    for i in range(6):
        await client.add_episode(
            name=f"ep-{i}",
            episode_body=f"Memory: subject=User 42; predicate=prefers; object=preference {i}; relationship_type=PREFERS; confidence=0.9",
            source=EpisodeType.MESSAGE,
            scope=scope,
        )
    await client.run_due_dreams()

    all_results = await client.search(query="preference", scope=scope, limit=10)
    limited = await client.search(query="preference", scope=scope, limit=3)

    assert len(all_results) == 6
    assert len(limited) == 3


# ── Truth management edge cases ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_correct_memory_fails_on_non_active_relationship(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9 form; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=t2 + timedelta(seconds=2))

    all_results = await client.search(
        query="W9",
        scope=scope,
        include_statuses={RelationshipStatus.SUPERSEDED, RelationshipStatus.PRUNED},
        as_of=t1 + timedelta(hours=1),
    )
    superseded_uuid = all_results[0].relationship_uuid

    with pytest.raises(ValueError, match="only active relationships can be corrected"):
        await client.correct_memory(
            relationship_uuid=superseded_uuid,
            corrected_object="ISO27001 report",
            scope=scope,
        )


@pytest.mark.asyncio
async def test_correct_memory_fails_when_corrected_fact_is_identical(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")

    await client.add_episode(
        name="ep",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    result = await client.search(query="SOC2", scope=scope)

    with pytest.raises(ValueError, match="corrected memory must differ"):
        await client.correct_memory(
            relationship_uuid=result[0].relationship_uuid,
            corrected_object="SOC2 report",
            scope=scope,
        )


@pytest.mark.asyncio
async def test_forget_memory_works_on_superseded_relationship(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.1, superseded_retention_seconds=86400),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9 form; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=t2 + timedelta(seconds=2))

    superseded = await client.search(
        query="W9",
        scope=scope,
        as_of=t1 + timedelta(hours=1),
    )
    assert len(superseded) == 1
    assert superseded[0].status == RelationshipStatus.SUPERSEDED

    result = await client.forget_memory(
        relationship_uuid=superseded[0].relationship_uuid,
        scope=scope,
        reason="operator_removed_stale_superseded",
    )

    assert result.previous_status == RelationshipStatus.SUPERSEDED
    assert result.status == RelationshipStatus.PRUNED


@pytest.mark.asyncio
async def test_correct_memory_valid_from_cannot_predate_target_valid_from(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    ref_time = datetime(2026, 5, 1, tzinfo=UTC)

    await client.add_episode(
        name="ep",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=ref_time,
    )
    await client.run_due_dreams()
    result = await client.search(query="SOC2", scope=scope)

    with pytest.raises(ValueError, match="correction valid_from cannot be before"):
        await client.correct_memory(
            relationship_uuid=result[0].relationship_uuid,
            corrected_object="ISO27001 report",
            scope=scope,
            valid_from=ref_time - timedelta(days=1),
        )


# ── Pruning policy ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pruning_superseded_retention_preserves_history_within_window(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            superseded_retention_seconds=86400,  # keep superseded facts for 24 h
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)
    prune_time = t2 + timedelta(hours=6)  # 6 h after supersession — within the 24 h window

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9 form; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=prune_time)

    relationships = {
        r["properties"]["object"]: r for r in client.export_graph()["relationships"] if r["type"] == "REQUIRES"
    }

    assert relationships["W9 form"]["properties"]["status"] == RelationshipStatus.SUPERSEDED.value
    assert relationships["SOC2 report"]["properties"]["status"] == RelationshipStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_pruning_superseded_retention_removes_history_after_window(tmp_path: Path) -> None:
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.1,
            superseded_retention_seconds=3600,  # retire superseded after 1 h
        ),
    )
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)
    prune_time = t2 + timedelta(hours=2)  # 2 h after supersession — past the 1 h retention

    await client.add_episode(
        name="first",
        episode_body="Memory: subject=Acme; predicate=requires; object=W9 form; relationship_type=REQUIRES; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )
    await client.add_episode(
        name="second",
        episode_body="Memory: subject=Acme; predicate=requires; object=SOC2 report; relationship_type=REQUIRES; confidence=0.95",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t2,
    )
    await client.run_due_dreams(now=prune_time)

    relationships = {
        r["properties"]["object"]: r for r in client.export_graph()["relationships"] if r["type"] == "REQUIRES"
    }

    assert relationships["W9 form"]["properties"]["status"] == RelationshipStatus.PRUNED.value
    assert relationships["W9 form"]["properties"]["pruned_reason"] == "superseded_retention_elapsed"
    assert relationships["SOC2 report"]["properties"]["status"] == RelationshipStatus.ACTIVE.value


# ── Dream scheduling edge cases ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dream_status_consolidation_reports_eligible_scopes(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    seed_client = Memotron(graph_path=graph_path)
    agent_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    user_scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")

    await seed_client.add_episode(
        name="agent-lesson",
        episode_body="Memory: subject=Support Agent; predicate=should; object=check order id; relationship_type=SHOULD; confidence=0.86",
        source=EpisodeType.MESSAGE,
        scope=agent_scope,
    )
    await seed_client.add_episode(
        name="user-preference",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=user_scope,
    )
    await seed_client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    seed_client.graph.close()

    config = config_with_jobs(
        DreamJob(name="consolidate-all", kind=DreamJobKind.CONSOLIDATION, cadence_seconds=1),
    )
    client = Memotron(graph_path=graph_path, config=config)
    statuses = await client.dream_status(now=datetime(2026, 6, 2, tzinfo=UTC))

    consolidation_status = next(s for s in statuses if s.job_kind == DreamJobKind.CONSOLIDATION)
    assert consolidation_status.eligible_scopes == 2
    assert consolidation_status.pending_episodes == 0


@pytest.mark.asyncio
async def test_run_dream_job_consolidation_bypasses_cadence(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    recorder = RecordingExtractionTransport()

    seed_client = Memotron(graph_path=graph_path)
    t_seed = datetime(2026, 5, 1, tzinfo=UTC)
    await seed_client.add_episode(
        name="lesson",
        episode_body="Memory: subject=Support Agent; predicate=should; object=confirm account id; relationship_type=SHOULD; confidence=0.86",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t_seed,
    )
    await seed_client.run_due_dreams(now=t_seed + timedelta(seconds=1))
    seed_client.graph.close()

    config = config_with_jobs(
        DreamJob(
            name="consolidate-agent",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=86400,
            scope=scope,
        )
    )
    client = Memotron(graph_path=graph_path, config=config, extraction_transport=recorder)
    t0 = t_seed + timedelta(days=1)

    first_run = await client.run_due_dreams(now=t0)  # seeds last_run
    skipped = await client.run_due_dreams(now=t0 + timedelta(seconds=10))  # within cadence
    forced = await client.run_dream_job(job_name="consolidate-agent", now=t0 + timedelta(seconds=20))

    assert first_run.processed_scopes == 1
    assert skipped.job_runs == []
    assert forced.processed_scopes == 1
    assert recorder.requests == []  # consolidation is the canonical graph reducer, not extraction
    decisions = await client.dream_decisions(job_name="consolidate-agent")
    selected = [decision for decision in decisions if decision.decision_type == "consolidation_scope_selected"]
    assert len(selected) == 2  # first due run + forced cadence bypass
    assert all(decision.scope == scope for decision in selected)


@pytest.mark.asyncio
async def test_formation_context_policy_as_of_episode_time_excludes_future_relationships(tmp_path: Path) -> None:
    graph_path = tmp_path / "memotron.sqlite"
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    t_old = datetime(2026, 6, 10, tzinfo=UTC)
    t_new = datetime(2026, 6, 1, tzinfo=UTC)

    seed_client = Memotron(graph_path=graph_path)
    await seed_client.add_episode(
        name="recent-fact",
        episode_body="Memory: subject=User 42; predicate=prefers; object=sourced answers; relationship_type=PREFERS; confidence=0.9",
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t_old,
    )
    await seed_client.run_due_dreams(now=t_old + timedelta(seconds=1))
    seed_client.graph.close()

    # as_of_episode_time=True (default): backfilled episode at t_new should NOT see the t_old fact
    recorder_with = RecordingExtractionTransport()
    config_with = config_with_jobs(
        DreamJob(
            name="formation-with",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
            context_policy=DreamContextPolicy(as_of_episode_time=True),
        )
    )
    client_with = Memotron(graph_path=graph_path, config=config_with, extraction_transport=recorder_with)
    await client_with.add_episode(
        name="backfill-episode",
        episode_body="The user mentioned something earlier.",
        source=EpisodeType.TEXT,
        scope=scope,
        reference_time=t_new,
    )
    await client_with.run_due_dreams(now=t_old + timedelta(seconds=2))

    assert recorder_with.requests[0].graph_context == []


@pytest.mark.asyncio
async def test_openai_compatible_transport_sends_instruction_set_system_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
    custom_system_prompt = "CUSTOM SYSTEM PROMPT FOR TEST"
    from memotron import DreamInstructionSet, NodeInstruction
    from memotron.config import RelationshipCardinality, RelationshipInstruction

    custom_instructions = DreamInstructionSet(
        name="custom",
        system_prompt=custom_system_prompt,
        node_instructions=(NodeInstruction(label="Entity", query="entities."),),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="preferences.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
        ),
    )
    from memotron import DreamConfig, DreamJob, DreamJobKind
    from memotron.config import default_prompt_profiles

    config = DreamConfig(
        instruction_sets=(custom_instructions,),
        prompt_profiles=default_prompt_profiles(),
        jobs=(DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1, instruction_set="custom"),),
    )

    server, received = openai_compatible_test_server({"memories": []})
    monkeypatch.setenv("MEMOTRON_TEST_KEY", "test-key")
    transport = OpenAICompatibleExtractionTransport(
        model="test-model",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        api_key_env="MEMOTRON_TEST_KEY",
    )
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=config,
        extraction_transport=transport,
    )
    try:
        await client.add_episode(
            name="test-ep",
            episode_body="User prefers short answers.",
            source=EpisodeType.TEXT,
            scope=scope,
            instruction_set="custom",
        )
        await client.run_due_dreams(now=datetime(2026, 6, 1, tzinfo=UTC))
    finally:
        server.shutdown()
        server.server_close()

    assert len(received) == 1
    system_msg = received[0]["body"]["messages"][0]
    assert system_msg["role"] == "system"
    assert system_msg["content"] == custom_system_prompt


def test_validate_memory_coerces_claim_mode_invalid_for_memory_type(caplog: Any) -> None:
    """An extractor-supplied claim_mode that conflicts with the relationship type's
    deterministic memory_type (e.g. claim_mode=directive tagged on a DECIDES/decision
    candidate) must be coerced to the type-consistent default rather than raising and
    discarding the whole episode's candidate batch."""
    from memotron import NodeInstruction
    from memotron.config import DreamInstructionSet, RelationshipInstruction
    from memotron.extraction import InstructionalExtractor
    from memotron.models import ClaimMode, Episode, MemoryScope, MemoryType, ScopeKind

    instructions = DreamInstructionSet(
        name="custom",
        system_prompt="test",
        node_instructions=(NodeInstruction(label="Entity", query="entities."),),
        relationship_instructions=(
            RelationshipInstruction(
                type="DECIDES",
                source_label="Entity",
                target_label="Entity",
                query="decisions.",
                memory_type=MemoryType.DECISION,
            ),
        ),
    )
    episode = Episode(
        name="test-ep",
        body="The team decided to ship on Friday.",
        source=EpisodeType.TEXT,
        scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="acme"),
    )
    raw_memory = {
        "subject": "team",
        "predicate": "decided",
        "object": "ship on Friday",
        "relationship_type": "DECIDES",
        "confidence": 0.9,
        "claim_mode": "directive",
    }
    extractor = InstructionalExtractor()
    with caplog.at_level("WARNING"):
        validated = extractor.validate_memory(raw_memory, episode=episode, instructions=instructions)
    assert validated.claim_mode == ClaimMode.DESCRIPTIVE_ASSERTION
    # The original value travels with the candidate so formation can receipt it;
    # the log line is operator convenience, not the record.
    assert validated.claim_mode_coerced_from == ClaimMode.DIRECTIVE
    assert "coercing to the type-consistent default" in caplog.text


def _client_with_decisions(tmp_path: Path) -> Memotron:
    """A client whose instruction set has a DECIDES/decision type.

    ``decision`` is the memory type whose permitted claim modes exclude
    ``directive``, so it is the type that makes the coercion path reachable.
    """
    from memotron.config import DreamInstructionSet, NodeInstruction, RelationshipInstruction
    from memotron.models import MemoryType

    base = default_config()
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(NodeInstruction(label="Entity", query="Durable entities."),),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Stable preferences.",
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Lessons that improve agent behaviour.",
            ),
            RelationshipInstruction(
                type="DECIDES",
                source_label="Entity",
                target_label="Entity",
                query="Decisions the team made.",
                memory_type=MemoryType.DECISION,
            ),
        ),
    )
    return Memotron(
        config=base.model_copy(update={"instruction_sets": (instructions,)}),
        graph_path=tmp_path / "memotron.sqlite",
    )


_MIXED_CLAIM_MODE_BATCH = json.dumps(
    {
        "memories": [
            {
                "subject": "Support Agent",
                "predicate": "should",
                "object": "confirm the account id before escalation",
                "relationship_type": "SHOULD",
                "confidence": 0.9,
            },
            {
                # The bad one: a decision candidate tagged as a directive.
                "subject": "Support Team",
                "predicate": "decided",
                "object": "route billing disputes to finance",
                "relationship_type": "DECIDES",
                "confidence": 0.9,
                "claim_mode": "directive",
            },
            {
                "subject": "Support Agent",
                "predicate": "prefers",
                "object": "concise escalation summaries",
                "relationship_type": "PREFERS",
                "confidence": 0.88,
            },
        ]
    }
)


@pytest.mark.asyncio
async def test_one_bad_claim_mode_does_not_discard_its_sibling_candidates(
    tmp_path: Path,
) -> None:
    """The regression the coercion exists for: a BATCH, not a lone candidate.

    The original bug raised on the mismatched field, which aborted the whole
    episode — every other candidate extracted from the same episode was
    discarded along with the bad one. Coercion has to keep the siblings.
    """
    client = _client_with_decisions(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    await client.add_episode(
        name="mixed-batch",
        episode_body=_MIXED_CLAIM_MODE_BATCH,
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    relationships = [
        relationship
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    ]
    objects = sorted(str(r.properties["object"]) for r in relationships)
    # All three survived — the sibling candidates were not collateral damage.
    assert objects == [
        "concise escalation summaries",
        "confirm the account id before escalation",
        "route billing disputes to finance",
    ]

    # ...and the coerced row is stored as the type-consistent assertion, not as
    # the directive the extractor claimed.
    decision = next(
        relationship for relationship in relationships if relationship.properties.get("memory_type") == "decision"
    )
    assert decision.properties["claim_mode"] == "descriptive_assertion"


@pytest.mark.asyncio
async def test_claim_mode_coercion_is_receipted_not_only_logged(tmp_path: Path) -> None:
    """No candidate transformation may be invisible to the receipt ledger.

    A directive silently downgraded to an assertion used to be discoverable
    only by grepping application logs; it must be a receipt, carrying both the
    original and the coerced claim mode, on the same candidate as the
    CANDIDATE_EXTRACTED event.
    """
    from memotron.receipts import ReceiptDecisionType

    client = _client_with_decisions(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    await client.add_episode(
        name="coerced",
        episode_body=_MIXED_CLAIM_MODE_BATCH,
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    coercions = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_CLAIM_MODE_COERCED.value
    ]
    # Exactly one candidate was transformed; the untouched sibling is not receipted as one.
    assert len(coercions) == 1
    coercion = coercions[0]
    assert coercion.decision_result == "transformed"
    assert "directive->descriptive_assertion" in coercion.decision_reason
    assert coercion.claim_mode == "descriptive_assertion"
    assert coercion.memory_type == "decision"
    payload = json.loads(coercion.event_payload)
    assert payload == {
        "claim_mode_before": "directive",
        "claim_mode_after": "descriptive_assertion",
        "memory_type": "decision",
    }
    # Bound to the same candidate the CANDIDATE_EXTRACTED receipt records, so the
    # transformation and the stored form are provably about one candidate.
    extracted = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED.value
        and receipt.candidate_uuid == coercion.candidate_uuid
    ]
    assert len(extracted) == 1
    assert extracted[0].candidate_digest == coercion.candidate_digest


@pytest.mark.asyncio
async def test_untouched_claim_modes_emit_no_coercion_receipt(tmp_path: Path) -> None:
    """The ledger must not accumulate no-op transformation events."""
    from memotron.receipts import ReceiptDecisionType

    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    await client.add_episode(
        name="clean",
        episode_body=(
            "Memory: subject=Support Agent; predicate=should; "
            "object=confirm the account id before escalation; "
            "relationship_type=SHOULD; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    assert not [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_CLAIM_MODE_COERCED.value
    ]


@pytest.mark.asyncio
async def test_semantic_dedup_reinforces_paraphrased_fact(tmp_path: Path) -> None:
    """WS-1 acceptance proof: paraphrased objects within the same truth_prefix reinforce
    one relationship row (observed_count == 2) instead of creating two active rows.

    The test also asserts that a genuinely different directive on the same
    scope/subject/predicate stays as a separate active row — semantic dedup must
    not collapse facts with unrelated objects.

    Paraphrase pair (object-only cosine ≈ 0.94 > 0.88 threshold):
      "ask for order id before escalation"
      "ask for order id before escalating"

    Different directive (object-only cosine ≈ 0.40 < 0.88 threshold):
      "verify payment method before processing order"
    """
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="support-agent")
    t1 = datetime(2026, 6, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 2, tzinfo=UTC)
    t3 = datetime(2026, 6, 3, tzinfo=UTC)

    # Episode 1: first wording of the directive
    await client.add_episode(
        name="directive-ep-1",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Support Agent",
                        "predicate": "should",
                        "object": "ask for order id before escalation",
                        "relationship_type": "SHOULD",
                        "confidence": 0.85,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=t1,
    )

    # Episode 2: paraphrased wording of the SAME directive — should reinforce
    await client.add_episode(
        name="directive-ep-2",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Support Agent",
                        "predicate": "should",
                        "object": "ask for order id before escalating",
                        "relationship_type": "SHOULD",
                        "confidence": 0.90,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=t2,
    )

    # Episode 3: genuinely different directive — should stay as a separate row
    await client.add_episode(
        name="directive-ep-3",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Support Agent",
                        "predicate": "should",
                        "object": "verify payment method before processing order",
                        "relationship_type": "SHOULD",
                        "confidence": 0.88,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=t3,
    )

    run_result = await client.run_due_dreams(now=t3 + timedelta(seconds=1))
    should_relationships = [r for r in client.export_graph()["relationships"] if r["type"] == "SHOULD"]
    active_relationships = [
        r for r in should_relationships if r["properties"]["status"] == RelationshipStatus.ACTIVE.value
    ]

    # Two active rows: one merged (paraphrase) + one distinct directive
    assert len(active_relationships) == 2, (
        f"expected 2 active SHOULD rows (1 merged paraphrase + 1 distinct), "
        f"got {len(active_relationships)}: {[r['properties']['object'] for r in active_relationships]}"
    )

    # The merged paraphrase row must have observed_count == 2
    paraphrase_rows = [r for r in active_relationships if "order id" in r["properties"]["object"]]
    assert len(paraphrase_rows) == 1, (
        f"expected exactly 1 merged paraphrase row, got {len(paraphrase_rows)}: "
        f"{[r['properties']['object'] for r in paraphrase_rows]}"
    )
    merged = paraphrase_rows[0]
    assert merged["properties"]["observed_count"] == 2, (
        f"merged row observed_count should be 2, got {merged['properties']['observed_count']}"
    )
    assert len(merged["properties"]["episode_uuids"]) == 2, (
        f"merged row should reference 2 source episodes, got {len(merged['properties']['episode_uuids'])}"
    )

    # The distinct directive must be its own row with observed_count == 1
    distinct_rows = [r for r in active_relationships if "payment" in r["properties"]["object"]]
    assert len(distinct_rows) == 1, f"expected exactly 1 distinct directive row, got {len(distinct_rows)}"
    distinct = distinct_rows[0]
    assert distinct["properties"]["observed_count"] == 1, (
        f"distinct row observed_count should be 1, got {distinct['properties']['observed_count']}"
    )

    # formation_semantic_reinforce decision must have been recorded for the merge
    decisions = await client.dream_decisions(limit=50)
    semantic_reinforce_decisions = [d for d in decisions if d.decision_type == "formation_semantic_reinforce"]
    assert len(semantic_reinforce_decisions) >= 1, (
        "expected at least one formation_semantic_reinforce decision to be recorded"
    )
    reinforce_decision = semantic_reinforce_decisions[0]
    assert reinforce_decision.details.get("write_verb") == "UPDATE"
    assert isinstance(reinforce_decision.details.get("cosine_score"), float)
    assert reinforce_decision.details["cosine_score"] >= 0.88

    # The run stats must show a reinforced relationship (not two created)
    formation_run = next(r for r in run_result.job_runs if r.job_kind == DreamJobKind.FORMATION)
    assert formation_run.reinforced_relationships >= 1, (
        "run must report at least one reinforced relationship for the semantic merge"
    )
    # Total new rows: 2 (one for the distinct paraphrase-group row + one for distinct directive)
    assert formation_run.created_relationships == 2, (
        f"expected 2 created (first paraphrase + distinct directive), got {formation_run.created_relationships}"
    )


# ---------------------------------------------------------------------------
# WS-2: Extraction rubric + salience + throttle tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_salience_default_config_is_noop(tmp_path: Path) -> None:
    """The default SalienceRubric (min_salience=0.0, max_memories_per_episode=None)
    must be a complete no-op: every validated memory survives, in original order,
    with no reordering or dropping — so the existing 101 tests remain unaffected."""
    from memotron import SalienceRubric

    rubric = SalienceRubric()
    assert rubric.is_noop, "default rubric must be a no-op"
    assert rubric.min_salience == 0.0
    assert rubric.max_memories_per_episode is None

    # End-to-end: feed 4 memories through the default config — all 4 must survive.
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="noop-test")
    body = json.dumps(
        {
            "memories": [
                {
                    "subject": "Corp A",
                    "predicate": "requires",
                    "object": f"item {i}",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
                for i in range(4)
            ]
        }
    )
    await client.add_episode(name="noop-ep", episode_body=body, source=EpisodeType.JSON, scope=scope)
    result = await client.run_due_dreams()
    created = result.created_relationships
    assert created == 4, f"default rubric should not drop anything; expected 4 created, got {created}"


@pytest.mark.asyncio
async def test_salience_rubric_throttles_noisy_episode(tmp_path: Path) -> None:
    """WS-2 proof test: a configured rubric with max_memories_per_episode=3
    and a salience floor must:
    - Retain at most 3 memories (high-salience ones survive)
    - Drop low-salience noise (low-importance types like directive/state when mixed with
      high-importance requirement/identity)
    - Record a formation_salience_filtered decision with the dropped count
    - Attach salience_score to surviving relationships in the graph
    """
    from memotron import SalienceRubric
    from memotron.config import default_config

    # Build a rubric that caps at 3 and floors at 0.05
    rubric = SalienceRubric(
        min_salience=0.05,
        max_memories_per_episode=3,
        half_life_seconds=86400.0,
    )
    assert not rubric.is_noop

    base = default_config()
    # Attach rubric to the instruction set
    updated_instructions = base.instruction_sets[0].model_copy(update={"salience_rubric": rubric})
    config = base.model_copy(update={"instruction_sets": (updated_instructions,)})

    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="throttle-test")

    # Build a noisy episode: 3 high-value REQUIRES + 5 low-value SHOULD (directive) memories = 8 total.
    # With max_memories_per_episode=3, at most 3 should survive.
    # REQUIRES → requirement type (importance=0.9), SHOULD → directive type (importance=0.5).
    # All memories have valid_from = reference_time → recency=1.0 for all.
    # Relevance will vary but both types will be close; importance is the discriminating factor.
    memories = [
        {
            "subject": "Corp B",
            "predicate": "requires",
            "object": f"critical requirement {i}",
            "relationship_type": "REQUIRES",
            "confidence": 0.95,
        }
        for i in range(3)
    ] + [
        {
            "subject": "Agent X",
            "predicate": "should",
            "object": f"low priority directive {i}",
            "relationship_type": "SHOULD",
            "confidence": 0.7,
        }
        for i in range(5)
    ]
    body = json.dumps({"memories": memories})
    await client.add_episode(name="noisy-ep", episode_body=body, source=EpisodeType.JSON, scope=scope)

    result = await client.run_due_dreams()

    # At most 3 relationships should have been created (the cap)
    assert result.created_relationships <= 3, (
        f"max_memories_per_episode=3 should cap materialization; got {result.created_relationships}"
    )
    assert result.created_relationships >= 1, "at least 1 memory must survive"

    # A formation_salience_filtered decision must have been recorded
    decisions = await client.dream_decisions(limit=50)
    salience_decisions = [d for d in decisions if d.decision_type == "formation_salience_filtered"]
    assert len(salience_decisions) >= 1, "expected at least one formation_salience_filtered decision to be recorded"

    # The decision must carry a dropped_count > 0 and dropped_subjects
    drop_decision = salience_decisions[0]
    assert drop_decision.details.get("dropped_count", 0) > 0, (
        "formation_salience_filtered decision must report at least 1 dropped memory"
    )
    assert "dropped_subjects" in drop_decision.details
    assert isinstance(drop_decision.details["dropped_subjects"], list)

    # Surviving relationships must carry a salience_score in their properties
    relationships = [r for r in client.export_graph()["relationships"] if r["type"] in ("REQUIRES", "SHOULD")]
    assert relationships, "no relationships were materialized"
    for rel in relationships:
        assert "salience_score" in rel["properties"], (
            f"relationship {rel['uuid']} is missing salience_score in properties"
        )
        score = rel["properties"]["salience_score"]
        assert isinstance(score, float), f"salience_score must be a float, got {type(score)}"
        assert 0.0 <= score <= 1.0, f"salience_score must be in [0,1], got {score}"


@pytest.mark.asyncio
async def test_salience_rubric_job_override_takes_precedence(tmp_path: Path) -> None:
    """A per-job salience rubric takes precedence over the instruction-set rubric."""
    from memotron import SalienceRubric
    from memotron.config import default_config

    # Instruction-set rubric: no-op (default)
    base = default_config()

    # Job-level rubric: cap at 1 memory
    job_rubric = SalienceRubric(max_memories_per_episode=1)
    assert not job_rubric.is_noop

    job_with_rubric = DreamJob(
        name="formation-rubric-override",
        kind=DreamJobKind.FORMATION,
        cadence_seconds=1,
        salience_rubric=job_rubric,
    )
    pruning_job = DreamJob(name="pruning-default", kind=DreamJobKind.PRUNING, cadence_seconds=1)
    config = base.model_copy(update={"jobs": (job_with_rubric, pruning_job)})
    client = Memotron(graph_path=tmp_path / "memotron.sqlite", config=config)

    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="override-test")
    body = json.dumps(
        {
            "memories": [
                {
                    "subject": "Corp C",
                    "predicate": "requires",
                    "object": f"req {i}",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
                for i in range(5)
            ]
        }
    )
    await client.add_episode(name="override-ep", episode_body=body, source=EpisodeType.JSON, scope=scope)
    result = await client.run_due_dreams()

    # Job cap=1 must override instruction-set no-op; only 1 relationship survives
    assert result.created_relationships == 1, (
        f"job-level rubric cap=1 should limit to 1 created relationship; got {result.created_relationships}"
    )

    # A salience decision must be recorded
    decisions = await client.dream_decisions(limit=50)
    assert any(d.decision_type == "formation_salience_filtered" for d in decisions), (
        "expected formation_salience_filtered decision from job-level rubric"
    )


@pytest.mark.asyncio
async def test_salience_rubric_recency_formula(tmp_path: Path) -> None:
    """SalienceRubric.recency_for implements Generative Agents exp-decay formula correctly."""
    from datetime import UTC, datetime

    from memotron import SalienceRubric

    rubric = SalienceRubric(half_life_seconds=3600.0)  # 1-hour half-life

    ref = datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC)

    # Memory at reference time → recency should be 1.0
    r_same = rubric.recency_for(ref, ref)
    assert r_same == 1.0, f"same-time recency should be 1.0, got {r_same}"

    # Memory 1 hour before reference → recency should be ~0.5 (one half-life)
    one_hour_ago = datetime(2026, 6, 15, 11, 0, 0, tzinfo=UTC)
    r_half = rubric.recency_for(one_hour_ago, ref)
    assert abs(r_half - 0.5) < 0.001, f"one-half-life recency should be ~0.5, got {r_half}"

    # Memory 2 hours before → recency should be ~0.25 (two half-lives)
    two_hours_ago = datetime(2026, 6, 15, 10, 0, 0, tzinfo=UTC)
    r_quarter = rubric.recency_for(two_hours_ago, ref)
    assert abs(r_quarter - 0.25) < 0.001, f"two-half-lives recency should be ~0.25, got {r_quarter}"

    # Memory newer than reference → recency should be 1.0
    future = datetime(2026, 6, 15, 13, 0, 0, tzinfo=UTC)
    r_future = rubric.recency_for(future, ref)
    assert r_future == 1.0, f"future memory recency should be 1.0, got {r_future}"

    # None valid_from → recency should be 1.0
    r_none = rubric.recency_for(None, ref)
    assert r_none == 1.0, f"None valid_from recency should be 1.0, got {r_none}"


@pytest.mark.asyncio
async def test_salience_rubric_importance_by_type(tmp_path: Path) -> None:
    """SalienceRubric.importance_for returns correct deterministic per-type weights."""
    from memotron import SalienceRubric
    from memotron.models import MemoryType

    rubric = SalienceRubric()

    # High-importance types
    assert rubric.importance_for(MemoryType.ANCHOR.value, 1.0) == 0.9
    assert rubric.importance_for(MemoryType.REQUIREMENT.value, 1.0) == 0.9

    # Medium types
    assert rubric.importance_for(MemoryType.PREFERENCE.value, 1.0) == 0.65

    # Low-importance types
    assert rubric.importance_for(MemoryType.DIRECTIVE.value, 1.0) == 0.5
    assert rubric.importance_for(MemoryType.STATE.value, 1.0) == 0.4

    # Unknown type → default 0.5
    assert rubric.importance_for("unknown_type", 1.0) == 0.5

    # None type → default 0.5
    assert rubric.importance_for(None, 1.0) == 0.5

    # scale_by_confidence=True: importance is multiplied by confidence
    rubric_scaled = SalienceRubric(scale_by_confidence=True)
    assert rubric_scaled.importance_for(MemoryType.REQUIREMENT.value, 0.5) == 0.9 * 0.5

    # Custom importance override
    rubric_custom = SalienceRubric(importance_weights={"directive": 0.95})
    assert rubric_custom.importance_for(MemoryType.DIRECTIVE.value, 1.0) == 0.95
    # Other types still use defaults
    assert rubric_custom.importance_for(MemoryType.REQUIREMENT.value, 1.0) == 0.9


# ---------------------------------------------------------------------------
# WS-3: Motives & the Memory Bank tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_episode_under_two_motives_yields_different_graphs(tmp_path: Path) -> None:
    """WS-3 proof: the same episode body ingested under two different Motives
    (one allowing only 'requirement', one only 'directive') produces materially
    different active graphs.

    This test is hermetic — it uses RuleBasedExtractionTransport (the default
    offline extractor) which parses explicit Memory: lines.  The observable
    difference comes from the Motive's allowed_memory_types filter, NOT from
    prompt text (the rule-based extractor ignores prompts).
    """
    from memotron import MemoryBank, Motive
    from memotron.models import MemoryType

    # An episode body that contains both a REQUIRES (requirement) memory and a
    # SHOULD (directive) memory.  The rule-based extractor will extract both.
    mixed_episode_body = (
        "Memory: subject=Acme Corp; predicate=requires; object=ISO27001 certificate; "
        "relationship_type=REQUIRES; confidence=0.95\n"
        "Memory: subject=Support Agent; predicate=should; object=verify compliance docs before escalation; "
        "relationship_type=SHOULD; confidence=0.88"
    )
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="acme-corp")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    # --- Client A: Motive allows only 'requirement' ---
    requirement_bank = MemoryBank(
        motives=[
            Motive(
                name="learn-compliance-requirements",
                goal="Surface and retain strict compliance and policy requirements",
                allowed_memory_types=(MemoryType.REQUIREMENT,),
                dedup_threshold=0.92,
            )
        ]
    )
    from memotron.config import DreamJob, DreamJobKind, default_config

    base_config = default_config()
    config_req = base_config.model_copy(
        update={
            "memory_bank": requirement_bank,
            "jobs": (
                DreamJob(
                    name="formation-default",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    motive="learn-compliance-requirements",
                ),
            ),
        }
    )
    client_req = Memotron(graph_path=tmp_path / "req.sqlite", config=config_req)
    await client_req.add_episode(
        name="mixed-episode",
        episode_body=mixed_episode_body,
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=now,
    )
    await client_req.run_due_dreams(now=now + timedelta(seconds=2))

    req_relationships = [r for r in client_req.export_graph()["relationships"] if r["type"] != "MENTIONS"]
    req_memory_types = {r["properties"].get("memory_type") for r in req_relationships}

    # --- Client B: Motive allows only 'directive' ---
    directive_bank = MemoryBank(
        motives=[
            Motive(
                name="distill-agent-lessons",
                goal="Extract reusable agent directives from resolved interactions",
                allowed_memory_types=(MemoryType.DIRECTIVE,),
                dedup_threshold=0.90,
            )
        ]
    )
    config_dir = base_config.model_copy(
        update={
            "memory_bank": directive_bank,
            "jobs": (
                DreamJob(
                    name="formation-default",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    motive="distill-agent-lessons",
                ),
            ),
        }
    )
    client_dir = Memotron(graph_path=tmp_path / "dir.sqlite", config=config_dir)
    await client_dir.add_episode(
        name="mixed-episode",
        episode_body=mixed_episode_body,
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=now,
    )
    await client_dir.run_due_dreams(now=now + timedelta(seconds=2))

    dir_relationships = [r for r in client_dir.export_graph()["relationships"] if r["type"] != "MENTIONS"]
    dir_memory_types = {r["properties"].get("memory_type") for r in dir_relationships}

    # --- Assertions ---
    # Client A should have requirement facts only (no directive).
    assert "requirement" in req_memory_types, (
        f"Expected 'requirement' in requirement-motive graph, got: {req_memory_types}"
    )
    assert "directive" not in req_memory_types, (
        f"Directive should have been filtered out by requirement Motive, got: {req_memory_types}"
    )

    # Client B should have directive facts only (no requirement).
    assert "directive" in dir_memory_types, f"Expected 'directive' in directive-motive graph, got: {dir_memory_types}"
    assert "requirement" not in dir_memory_types, (
        f"Requirement should have been filtered out by directive Motive, got: {dir_memory_types}"
    )

    # The two graphs must differ materially.
    assert req_memory_types != dir_memory_types, (
        "The two Motive-filtered graphs should differ; both returned the same types."
    )

    # Dream decisions should record the type-filter drops.
    req_decisions = await client_req.dream_decisions(limit=50)
    dir_decisions = await client_dir.dream_decisions(limit=50)
    req_filter_decisions = [d for d in req_decisions if d.decision_type == "formation_motive_type_filtered"]
    dir_filter_decisions = [d for d in dir_decisions if d.decision_type == "formation_motive_type_filtered"]
    assert len(req_filter_decisions) >= 1, "Expected at least one type-filter decision for requirement Motive"
    assert len(dir_filter_decisions) >= 1, "Expected at least one type-filter decision for directive Motive"


def test_unknown_motive_name_fails_fast() -> None:
    """WS-3: Referencing an unknown motive name in a DreamJob raises a ValueError
    at DreamConfig construction time (fail-fast, mirrors the instruction-set and
    prompt-profile unknown-name checks).
    """
    from memotron import DreamConfig, DreamJob, DreamJobKind, MemoryBank, Motive
    from memotron.config import default_config

    bank = MemoryBank(
        motives=[
            Motive(
                name="known-motive",
                goal="A motive that exists",
                allowed_memory_types=(),
            )
        ]
    )
    base = default_config()

    # A job that pins a motive name that is NOT in the bank should raise ValueError
    # when the DreamConfig is constructed via the full constructor (which runs validators).
    with pytest.raises(ValueError, match="unknown motive"):
        DreamConfig(
            instruction_sets=base.instruction_sets,
            prompt_profiles=base.prompt_profiles,
            jobs=(
                DreamJob(
                    name="formation-default",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    motive="nonexistent-motive",
                ),
            ),
            memory_bank=bank,
        )

    # A MemoryBank.motive() lookup with an unknown name should also raise.
    with pytest.raises(ValueError, match="unknown motive"):
        bank.motive("nonexistent-motive")


def test_builtin_memory_bank_loads_all_persona_motives() -> None:
    """WS-3: builtin_memory_bank() loads all four §4 persona preset Motives and
    every named Motive is accessible by name lookup without error.
    """
    from memotron import builtin_memory_bank
    from memotron.memory_bank import (
        assistant_memory_bank,
        engineering_memory_bank,
        pm_memory_bank,
        support_memory_bank,
    )

    # --- Combined bank ---
    bank = builtin_memory_bank()
    assert len(bank.motives) > 0, "builtin_memory_bank() returned an empty bank"

    # Support persona
    support = support_memory_bank()
    for motive in support.motives:
        found = bank.motive(motive.name)
        assert found.name == motive.name

    expected_support_names = {
        "build-customer-profile",
        "learn-compliance-requirements",
        "capture-preferences",
        "distill-agent-lessons",
    }
    support_names = {m.name for m in support.motives}
    assert expected_support_names == support_names, (
        f"Support bank missing motives. Expected {expected_support_names}, got {support_names}"
    )

    # Engineering persona
    eng = engineering_memory_bank()
    expected_eng_names = {
        "learn-code-conventions",
        "capture-architecture-decisions",
        "record-incidents-postmortems",
        "distill-agent-lessons",
    }
    eng_names = {m.name for m in eng.motives}
    assert expected_eng_names == eng_names, (
        f"Engineering bank missing motives. Expected {expected_eng_names}, got {eng_names}"
    )

    # General Assistant persona
    asst = assistant_memory_bank()
    expected_asst_names = {
        "build-user-profile",
        "capture-preferences",
        "track-working-state",
    }
    asst_names = {m.name for m in asst.motives}
    assert expected_asst_names == asst_names, (
        f"Assistant bank missing motives. Expected {expected_asst_names}, got {asst_names}"
    )

    # Product Manager persona
    pm = pm_memory_bank()
    expected_pm_names = {
        "synthesize-user-feedback",
        "record-decisions",
        "track-roadmap-state",
        "capture-meeting-notes",
    }
    pm_names = {m.name for m in pm.motives}
    assert expected_pm_names == pm_names, f"PM bank missing motives. Expected {expected_pm_names}, got {pm_names}"

    # All individual-persona banks have non-empty allowed_memory_types
    for persona_bank in (support, eng, asst, pm):
        for motive in persona_bank.motives:
            assert motive.allowed_memory_types, (
                f"Motive {motive.name!r} has empty allowed_memory_types — each preset should restrict types"
            )

    # All motives have a goal
    for motive in bank.motives:
        assert motive.goal.strip(), f"Motive {motive.name!r} has blank goal"


# ---------------------------------------------------------------------------
# WS-5 — Budget-aware, typed retrieval
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ws5_legacy_profile_output_is_unchanged(tmp_path: Path) -> None:
    """Default profile() output is byte-for-byte legacy — WS-5 invariant proof.

    Verifies that calling profile() with no WS-5 args:
    - still contains the legacy header strings
    - renders a flat (non-grouped) list with "Static facts:" / "Recent facts:"
    - tokens_used == 0 and tokens_available is None (no budget requested)
    """
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ws5-legacy")

    await client.add_episode(
        name="pref-1",
        episode_body=(
            "Memory: subject=User A; predicate=prefers; object=concise answers; "
            "relationship_type=PREFERS; confidence=0.90"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.add_episode(
        name="req-1",
        episode_body=(
            "Memory: subject=User A; predicate=requires; object=two-factor auth; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()
    profile = await client.profile(scope=scope)

    # Legacy format strings must be present.
    assert "Memory profile for user:ws5-legacy" in profile.rendered_context
    assert "Static facts:" in profile.rendered_context
    assert "Recent facts:" in profile.rendered_context
    # No typed grouping headings in legacy mode.
    assert "IDENTITY:" not in profile.rendered_context
    assert "REQUIREMENT:" not in profile.rendered_context
    assert "[typed]" not in profile.rendered_context
    # Budget fields must be zeroed / None (legacy callers unaffected).
    assert profile.tokens_used == 0
    assert profile.tokens_available is None


@pytest.mark.asyncio
async def test_ws5_budgeted_typed_profile_fits_budget_and_keeps_identity(tmp_path: Path) -> None:
    """WS-5 acceptance proof: budget-aware typed profile.

    Scenario (miniature "271 injected"):
      - Many low-value directive facts
      - A handful of high-value identity / requirement facts
      - Small token_budget that cannot hold everything

    Assertions:
      a) rendered_context is grouped by type with headings (typed render_mode)
      b) tokens_used <= token_budget
      c) At least one identity or requirement fact is present (floor guarantee)
      d) Directive facts were trimmed to fit within budget
    """
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws5-budget-proof")

    # A few high-priority identity / requirement facts.
    for i, obj in enumerate(["enterprise tier", "NA region", "Acme Corp name"]):
        await client.add_episode(
            name=f"identity-{i}",
            episode_body=(
                f"Memory: subject=Acme; predicate=is; object={obj}; relationship_type=REQUIRES; confidence=0.97"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
        )

    # Many low-priority directive facts (these should get trimmed).
    for i in range(12):
        await client.add_episode(
            name=f"directive-{i}",
            episode_body=(
                f"Memory: subject=Agent; predicate=should; object=do step {i} before escalating; "
                "relationship_type=SHOULD; confidence=0.70"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
        )

    await client.run_due_dreams()

    # Small token budget — definitely cannot fit all 15 facts.
    SMALL_BUDGET = 120
    profile = await client.profile(
        scope=scope,
        token_budget=SMALL_BUDGET,
        policy=ProfilePolicy(
            render_mode="typed",
            max_static_facts=50,
            max_dynamic_facts=50,
        ),
    )

    ctx = profile.rendered_context

    # (a) Typed headings present.
    assert "[typed]" in ctx, "Expected typed render_mode header in rendered_context"

    # (b) Token budget respected.
    assert profile.tokens_used <= SMALL_BUDGET, f"tokens_used={profile.tokens_used} exceeds token_budget={SMALL_BUDGET}"
    assert profile.tokens_available == SMALL_BUDGET

    # (c) Identity/requirement floor: at least one identity or requirement fact present.
    all_selected = profile.static_facts + profile.dynamic_facts
    floor_types_present = any(f.memory_type in {"anchor", "requirement"} for f in all_selected)
    # requirement-type facts (REQUIRES) should be present even if directives fill budget.
    # memory_type on REQUIRES = "requirement" (per WS-0 mapping).
    # We check rendered output for the REQUIRES tag as well.
    has_requires_in_ctx = "REQUIRES" in ctx or "REQUIREMENT" in ctx
    assert floor_types_present or has_requires_in_ctx, (
        "Identity/requirement facts must be present in the budget profile even when "
        f"directives are plentiful. all_selected types: {[f.memory_type for f in all_selected]}"
    )

    # (d) Directive facts were trimmed (not all 12 fit in SMALL_BUDGET).
    directive_in_ctx = ctx.count("do step")
    assert directive_in_ctx < 12, f"Expected directives to be trimmed to fit budget, but found {directive_in_ctx} of 12"


@pytest.mark.asyncio
async def test_ws5_typed_render_mode_groups_facts_by_type(tmp_path: Path) -> None:
    """Typed render mode produces type-grouped headings, not flat Static/Recent lists."""
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws5-typed-render")

    await client.add_episode(
        name="req",
        episode_body=(
            "Memory: subject=Acme; predicate=requires; object=SOC2 compliance; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.add_episode(
        name="pref",
        episode_body=(
            "Memory: subject=Acme; predicate=prefers; object=weekly status emails; "
            "relationship_type=PREFERS; confidence=0.88"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    profile = await client.profile(
        scope=scope,
        policy=ProfilePolicy(render_mode="typed"),
    )

    ctx = profile.rendered_context
    # Typed mode has grouped header.
    assert "[typed]" in ctx
    # Facts present as grouped renderings (type headings).
    # The exact heading depends on memory_type assigned by WS-0.
    # REQUIRES → requirement, PREFERS → preference.
    assert "SOC2 compliance" in ctx
    assert "weekly status emails" in ctx
    # No legacy flat section headers.
    assert "Static facts:" not in ctx
    assert "Recent facts:" not in ctx


@pytest.mark.asyncio
async def test_ws5_per_type_max_facts_caps_directives(tmp_path: Path) -> None:
    """per_type_max_facts prevents one memory type from crowding out others."""
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="ws5-per-type")

    # Add 5 directive (SHOULD) facts.
    for i in range(5):
        await client.add_episode(
            name=f"should-{i}",
            episode_body=(
                f"Memory: subject=Agent; predicate=should; object=lesson {i}; relationship_type=SHOULD; confidence=0.80"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
        )
    # Add 2 requirement (REQUIRES) facts.
    await client.add_episode(
        name="req-1",
        episode_body=(
            "Memory: subject=Agent; predicate=requires; object=auth before action; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    profile = await client.profile(
        scope=scope,
        policy=ProfilePolicy(
            per_type_max_facts={"directive": 2},
            max_static_facts=50,
        ),
    )

    directive_facts = [f for f in profile.static_facts if f.memory_type == "directive"]
    assert len(directive_facts) <= 2, f"Expected at most 2 directive facts, got {len(directive_facts)}"
    # Requirement fact still present.
    req_facts = [f for f in profile.static_facts if f.relationship_type == "REQUIRES"]
    assert req_facts, "Requirement fact should be present even when directive is capped"


@pytest.mark.asyncio
async def test_ws5_reference_mode_emits_jit_refs_for_overflow(tmp_path: Path) -> None:
    """reference_mode=True emits lightweight reference lines for overflow facts."""
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws5-refs")

    # Create several facts that will overflow the small budget.
    for i in range(6):
        await client.add_episode(
            name=f"should-{i}",
            episode_body=(
                f"Memory: subject=Org; predicate=should; object=process step {i} carefully; "
                "relationship_type=SHOULD; confidence=0.75"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
        )

    await client.run_due_dreams()

    profile = await client.profile(
        scope=scope,
        token_budget=60,  # very small — most facts will overflow
        policy=ProfilePolicy(
            render_mode="typed",
            reference_mode=True,
            max_static_facts=50,
        ),
    )

    ctx = profile.rendered_context
    # If any facts overflowed, the references section should appear.
    all_fact_count = len(profile.static_facts) + len(profile.dynamic_facts)
    if all_fact_count < 6:
        # Some were trimmed — references section must exist.
        assert "References (expand via memory_evidence):" in ctx, (
            "Expected JIT references section when facts overflowed the budget"
        )
        assert "[REF:" in ctx, "Expected [REF:...] reference lines in rendered_context"


@pytest.mark.asyncio
async def test_ws5_negative_token_budget_raises_value_error(tmp_path: Path) -> None:
    """profile() with a negative token_budget fails fast with ValueError."""
    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ws5-neg-budget")
    with pytest.raises(ValueError, match="token_budget must be non-negative"):
        await client.profile(scope=scope, token_budget=-1)


@pytest.mark.asyncio
async def test_ws5_motive_budget_share_drives_allocation(tmp_path: Path) -> None:
    """When a Motive with retrieval_budget_share is passed, its share affects allocation.

    The motive gives a high share to 'requirement' type — identity/requirement facts
    should consume a disproportionate fraction of the budget vs directives.
    """
    from memotron import Motive
    from memotron.models import MemoryType

    client = new_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws5-motive-share")

    # 1 requirement fact.
    await client.add_episode(
        name="req",
        episode_body=(
            "Memory: subject=Corp; predicate=requires; object=vendor audit; relationship_type=REQUIRES; confidence=0.97"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    # 5 directive facts.
    for i in range(5):
        await client.add_episode(
            name=f"should-{i}",
            episode_body=(
                f"Memory: subject=Agent; predicate=should; object=step {i}; relationship_type=SHOULD; confidence=0.70"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
        )
    await client.run_due_dreams()

    compliance_motive = Motive(
        name="compliance",
        goal="Capture compliance requirements",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
        retrieval_budget_share=0.80,  # 80% of budget to requirement type
    )

    profile = await client.profile(
        scope=scope,
        token_budget=200,
        motive=compliance_motive,
        policy=ProfilePolicy(render_mode="typed", max_static_facts=50),
    )

    # Requirement fact must appear.
    all_selected = profile.static_facts + profile.dynamic_facts
    req_present = any(f.relationship_type == "REQUIRES" for f in all_selected)
    assert req_present, "Requirement fact must be present when motive gives high budget share to requirement"

    # tokens_used must respect the budget.
    assert profile.tokens_used <= 200, f"tokens_used={profile.tokens_used} exceeds budget=200"


@pytest.mark.asyncio
async def test_ws5_motive_by_name_resolves_from_memory_bank(tmp_path: Path) -> None:
    """Passing motive as a string name resolves from config.memory_bank."""
    from memotron import MemoryBank, Motive
    from memotron.models import MemoryType

    bank = MemoryBank(
        motives=[
            Motive(
                name="learn-requirements",
                goal="Learn compliance requirements",
                allowed_memory_types=(MemoryType.REQUIREMENT,),
                retrieval_budget_share=0.70,
            )
        ]
    )
    config = default_config().model_copy(update={"memory_bank": bank})
    client = Memotron(
        graph_path=tmp_path / "ws5-name.sqlite",
        config=config,
    )
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws5-name-resolve")

    await client.add_episode(
        name="req",
        episode_body=(
            "Memory: subject=Corp; predicate=requires; object=SOC2 before vendor; "
            "relationship_type=REQUIRES; confidence=0.95"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
    )
    await client.run_due_dreams()

    # Should not raise — motive name resolves from memory_bank.
    profile = await client.profile(
        scope=scope,
        token_budget=300,
        motive="learn-requirements",
        policy=ProfilePolicy(render_mode="typed"),
    )
    assert profile.tokens_available == 300
    assert "SOC2 before vendor" in profile.rendered_context


@pytest.mark.asyncio
async def test_ws5_invalid_render_mode_raises(tmp_path: Path) -> None:
    """ProfilePolicy with an unknown render_mode raises a validation error."""
    with pytest.raises(ValidationError):
        ProfilePolicy(render_mode="freeform")


def test_ws5_token_estimator_is_deterministic() -> None:
    """_estimate_tokens is deterministic: same text always → same count."""
    from memotron.client import Memotron as DW

    texts = [
        "",
        "hello",
        "A" * 100,
        "Memory profile for user:acme [typed]\nIDENTITY:\n- [REQUIRES] fact (confidence=0.95, observed_count=1)\n",
    ]
    for text in texts:
        t1 = DW._estimate_tokens(text)
        t2 = DW._estimate_tokens(text)
        assert t1 == t2, f"Non-deterministic for text of length {len(text)}"
        assert t1 >= 0


def test_ws5_token_estimator_ceiling_division() -> None:
    """_estimate_tokens rounds up (ceiling): 5 chars → 2 tokens, not 1."""
    from memotron.client import Memotron as DW

    assert DW._estimate_tokens("") == 0
    assert DW._estimate_tokens("1234") == 1  # exactly 4 chars → 1 token
    assert DW._estimate_tokens("12345") == 2  # 5 chars → ceil(5/4) = 2 tokens
    assert DW._estimate_tokens("1" * 8) == 2  # 8 chars → 2 tokens
    assert DW._estimate_tokens("1" * 9) == 3  # 9 chars → ceil(9/4) = 3 tokens


@pytest.mark.asyncio
async def test_rollup_consolidation_creates_rollup_and_demotes_members(tmp_path: Path) -> None:
    """WS-4 proof: rollup consolidation synthesizes a ROLLUP and demotes members."""
    from memotron.config import RollupConsolidationPolicy

    config = config_with_jobs(
        DreamJob(
            name="formation",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
        ),
        DreamJob(
            name="consolidation-rollup",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=1,
            rollup_consolidation=True,
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.0,  # cluster everything (all cosines >= 0.0)
                min_cluster_size=3,
                max_depth=1,
            ),
        ),
    )
    client = Memotron(graph_path=tmp_path / "ws4.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws4-test")
    now = datetime(2026, 6, 1, tzinfo=UTC)

    # Seed 4 related active facts using PREFERS (MULTI_ACTIVE) so all 4 remain active.
    facts = [
        ("Acme Parks", "prefers", "SOC2 report", "PREFERS", 0.90),
        ("Acme Parks", "prefers", "ISO27001 cert", "PREFERS", 0.85),
        ("Acme Parks", "prefers", "vendor approval form", "PREFERS", 0.88),
        ("Acme Parks", "prefers", "W9 form", "PREFERS", 0.87),
    ]
    for subject, predicate, obj, rel_type, conf in facts:
        await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=obj,
            relationship_type=rel_type,
            scope=scope,
            confidence=conf,
        )

    # Run consolidation with rollup_consolidation enabled.
    run = await client.run_dream_job(job_name="consolidation-rollup", now=now)

    # Inspect graph relationships.
    all_rels = [r for r in client.graph.relationships() if r.type != "MENTIONS"]
    rollup_rels = [r for r in all_rels if r.properties.get("memory_type") == "rollup"]

    # 1. A ROLLUP relationship was created.
    assert len(rollup_rels) == 1, f"Expected 1 ROLLUP, got {len(rollup_rels)}"
    rollup = rollup_rels[0]

    # 2. ROLLUP has derived_from listing all member uuids.
    member_base_rels = [r for r in all_rels if r.properties.get("active_in_context") is False]
    assert len(member_base_rels) >= 3  # at least min_cluster_size demoted
    derived_from = rollup.properties.get("derived_from", [])
    assert isinstance(derived_from, list) and len(derived_from) >= 3
    # All demoted member uuids are in derived_from.
    for m in member_base_rels:
        assert m.uuid in derived_from, f"member {m.uuid} not in derived_from"

    # 3. Members have active_in_context=False and rolled_up_by set.
    for m in member_base_rels:
        assert m.properties.get("active_in_context") is False
        assert m.properties.get("rolled_up_by") == rollup.uuid

    # 4. A consolidation_rollup_created decision was recorded.
    decisions = client.graph.dream_decisions(limit=100)
    cluster_decisions = [d for d in decisions if d.decision_type == "consolidation_rollup_created"]
    assert len(cluster_decisions) >= 1
    cd = cluster_decisions[0]
    assert cd.details.get("cluster_size") >= 3
    assert set(cd.details.get("member_uuids", [])) == set(derived_from)

    # 5. run.created_relationships includes the rollup.
    assert run.created_relationships >= 1


@pytest.mark.asyncio
async def test_demoted_members_leave_profile_but_remain_auditable(tmp_path: Path) -> None:
    """WS-4 proof: demoted members vanish from profile but are auditable via search/timeline."""
    from memotron.config import RollupConsolidationPolicy

    config = config_with_jobs(
        DreamJob(
            name="formation",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
        ),
        DreamJob(
            name="consolidation-rollup",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=1,
            rollup_consolidation=True,
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.0,
                min_cluster_size=3,
                max_depth=1,
            ),
        ),
    )
    client = Memotron(graph_path=tmp_path / "ws4-audit.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws4-audit")
    now = datetime(2026, 6, 1, tzinfo=UTC)

    # Use PREFERS (MULTI_ACTIVE) so all 3 facts remain active (not superseded).
    facts = [
        ("Acme Parks", "prefers", "SOC2 report", "PREFERS", 0.90),
        ("Acme Parks", "prefers", "ISO27001 cert", "PREFERS", 0.85),
        ("Acme Parks", "prefers", "vendor approval form", "PREFERS", 0.88),
    ]
    for subject, predicate, obj, rel_type, conf in facts:
        await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=obj,
            relationship_type=rel_type,
            scope=scope,
            confidence=conf,
        )

    # Before consolidation: profile shows 3 facts.
    pre_profile = await client.profile(scope=scope)
    pre_context_visible = len(pre_profile.static_facts) + len(pre_profile.dynamic_facts)
    assert pre_context_visible >= 3

    # Run rollup consolidation.
    await client.run_dream_job(job_name="consolidation-rollup", now=now)

    # After consolidation: profile shows only the ROLLUP (1 fact); members are gone from context.
    post_profile = await client.profile(scope=scope)
    post_context_visible = len(post_profile.static_facts) + len(post_profile.dynamic_facts)
    assert post_context_visible < pre_context_visible, (
        f"Expected context to shrink: pre={pre_context_visible}, post={post_context_visible}"
    )
    # The ROLLUP is in the profile.
    all_profile_types = [f.memory_type for f in post_profile.static_facts + post_profile.dynamic_facts]
    assert "rollup" in all_profile_types, f"Expected 'rollup' in profile types, got {all_profile_types}"

    # Demoted members are still auditable via search (search does NOT filter active_in_context).
    search_results = await client.search(query="SOC2", scope=scope)
    # search uses _relationship_is_visible which checks status/time, NOT active_in_context.
    # The demoted members remain ACTIVE status (just active_in_context=False) so they show in search.
    assert any("SOC2" in r.object for r in search_results), (
        f"Expected SOC2 in search results, got {[r.object for r in search_results]}"
    )

    # Total queryable count (all active rels including demoted) = 3 members + 1 rollup = 4.
    all_active = [
        r
        for r in client.graph.relationships()
        if r.type != "MENTIONS"
        and r.properties.get("status") == "active"
        and r.properties.get("scope_key") == scope.key
    ]
    assert len(all_active) == 4, f"Expected 4 active rels (3 members + 1 rollup), got {len(all_active)}"


@pytest.mark.asyncio
async def test_motive_gates_rollup_consolidation_when_rollup_type_disallowed(tmp_path: Path) -> None:
    """WS-3 × WS-4 interlock (Build A): the SAME named Motive that gates which memory TYPES
    may be formed also governs admissibility of the ROLLUP type produced by consolidation.

    A Motive whose allowed_memory_types excludes ROLLUP blocks rollup consolidation entirely
    and records an auditable 'consolidation_motive_rollup_gated' decision; a Motive that allows
    ROLLUP lets rollups form.  (No Motive / empty allowed_memory_types remains a no-op — covered
    by test_rollup_consolidation_creates_rollup_and_demotes_members.)
    """
    from memotron import MemoryBank, Motive
    from memotron.config import (
        DreamJob,
        DreamJobKind,
        RollupConsolidationPolicy,
        default_config,
    )
    from memotron.models import MemoryType

    facts = [
        ("Acme Parks", "prefers", "SOC2 report", "PREFERS", 0.90),
        ("Acme Parks", "prefers", "ISO27001 cert", "PREFERS", 0.85),
        ("Acme Parks", "prefers", "vendor approval form", "PREFERS", 0.88),
        ("Acme Parks", "prefers", "W9 form", "PREFERS", 0.87),
    ]
    now = datetime(2026, 6, 1, tzinfo=UTC)
    policy = RollupConsolidationPolicy(cluster_threshold=0.0, min_cluster_size=3, max_depth=1)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="gate-test")

    def build_client(path: Path, bank: MemoryBank, motive_name: str) -> Memotron:
        config = default_config().model_copy(
            update={
                "memory_bank": bank,
                "jobs": (
                    DreamJob(
                        name="consolidation-rollup",
                        kind=DreamJobKind.CONSOLIDATION,
                        cadence_seconds=1,
                        rollup_consolidation=True,
                        rollup_consolidation_policy=policy,
                        motive=motive_name,
                    ),
                ),
            }
        )
        return Memotron(graph_path=path, config=config)

    async def seed(client: Memotron) -> None:
        for subject, predicate, obj, rel_type, conf in facts:
            await client.add_memory(
                subject=subject,
                predicate=predicate,
                object=obj,
                relationship_type=rel_type,
                scope=scope,
                confidence=conf,
            )

    # --- Case 1: Motive disallows ROLLUP -> gate fires, no rollups, no demotions ---
    pref_only_bank = MemoryBank(
        motives=[
            Motive(
                name="capture-preferences",
                goal="Capture customer preferences only",
                allowed_memory_types=(MemoryType.PREFERENCE,),
            )
        ]
    )
    client_gated = build_client(tmp_path / "gated.sqlite", pref_only_bank, "capture-preferences")
    await seed(client_gated)
    await client_gated.run_dream_job(job_name="consolidation-rollup", now=now)

    rollup_rels = [r for r in client_gated.graph.relationships() if r.properties.get("memory_type") == "rollup"]
    assert rollup_rels == [], "Motive disallowing ROLLUP must block all rollup synthesis"
    demoted = [r for r in client_gated.graph.relationships() if r.properties.get("active_in_context") is False]
    assert demoted == [], "No member may be demoted when rollup synthesis is gated"

    gate_decisions = [
        d
        for d in client_gated.graph.dream_decisions(limit=100)
        if d.decision_type == "consolidation_motive_rollup_gated"
    ]
    assert len(gate_decisions) == 1, "the gate must be recorded as an auditable decision (no silent skip)"
    gd = gate_decisions[0]
    assert gd.details.get("approved") is False
    assert gd.details.get("reason") == "motive_rollup_not_allowed"
    assert gd.details.get("motive_name") == "capture-preferences"
    assert "rollup" not in gd.details.get("allowed_memory_types", [])

    # --- Case 2: Motive explicitly allows ROLLUP -> rollups form, no gate decision ---
    rollup_ok_bank = MemoryBank(
        motives=[
            Motive(
                name="capture-and-rollup",
                goal="Capture preferences and synthesize rollups",
                allowed_memory_types=(MemoryType.PREFERENCE, MemoryType.ROLLUP),
            )
        ]
    )
    client_ok = build_client(tmp_path / "ok.sqlite", rollup_ok_bank, "capture-and-rollup")
    await seed(client_ok)
    run_ok = await client_ok.run_dream_job(job_name="consolidation-rollup", now=now)

    rollup_rels_ok = [r for r in client_ok.graph.relationships() if r.properties.get("memory_type") == "rollup"]
    assert len(rollup_rels_ok) == 1, "Motive allowing ROLLUP must permit rollup synthesis"
    assert run_ok.created_relationships >= 1
    assert not [
        d for d in client_ok.graph.dream_decisions(limit=100) if d.decision_type == "consolidation_motive_rollup_gated"
    ], "no gate decision when ROLLUP is allowed"


def test_context_visible_read_uses_index_and_two_tiers(tmp_path: Path) -> None:
    """§101 scan-avoiding two-tier read (Build B): context_visible_relationships seeks the
    relationships_ctx_idx index and returns only active, non-demoted, in-scope rows (the
    bounded working tier), while the full store still holds demoted, superseded, and
    other-scope rows (the queryable/evidence tier)."""
    from memotron.graph import PropertyGraphStore

    store = PropertyGraphStore(tmp_path / "ctx.sqlite")
    scope_a = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="alpha")
    scope_b = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="beta")

    def node(name: str, scope: MemoryScope):
        created, _ = store.upsert_node(
            labels=("Entity", "Customer"),
            key=f"{scope.key}:Entity:{name}",
            properties={"name": name, "scope_key": scope.key},
        )
        return created

    def rel(subject: str, obj: str, scope: MemoryScope, *, status: str = "active", active_in_context=None):
        source = node(subject, scope)
        target = node(obj, scope)
        props: dict[str, Any] = {
            "fact": f"{subject} prefers {obj}",
            "predicate": "prefers",
            "object": obj,
            "scope_key": scope.key,
            "status": status,
            "memory_type": "preference",
        }
        if active_in_context is not None:
            props["active_in_context"] = active_in_context
        return store.add_relationship(
            source_uuid=source.uuid,
            target_uuid=target.uuid,
            relationship_type="PREFERS",
            properties=props,
        )

    visible_absent = rel("Acme", "SOC2", scope_a)  # visible: flag absent
    visible_true = rel("Acme", "ISO", scope_a, active_in_context=True)  # visible: flag true
    demoted = rel("Acme", "W9", scope_a, active_in_context=False)  # demoted -> excluded
    superseded = rel("Acme", "old-policy", scope_a, status="superseded")  # not active -> excluded
    other_scope = rel("Beta", "doc", scope_b)  # other scope -> excluded

    # Bounded working tier: only active, non-demoted, in-scope rows.
    visible = store.context_visible_relationships(scope=scope_a)
    assert {r.uuid for r in visible} == {visible_absent.uuid, visible_true.uuid}

    # Full store (evidence/queryable tier) still holds everything.
    all_uuids = {r.uuid for r in store.relationships()}
    assert {demoted.uuid, superseded.uuid, other_scope.uuid} <= all_uuids

    # relationship_types filter still applies on the indexed read.
    assert store.context_visible_relationships(scope=scope_a, relationship_types={"MENTIONS"}) == []

    # The read SEEKS the index — it does not full-scan the relationship store.
    plan = " ".join(store.explain_context_visible_read(scope=scope_a)).upper()
    assert "RELATIONSHIPS_CTX_IDX" in plan, f"expected index seek, plan was: {plan}"
    assert "SCAN RELATIONSHIPS" not in plan, f"profile read must not full-scan: {plan}"


@pytest.mark.asyncio
async def test_recursive_rollups_respect_max_depth(tmp_path: Path) -> None:
    """WS-4 proof: rollups can be clustered into higher-level rollups up to max_depth."""
    from memotron.config import RollupConsolidationPolicy

    config = config_with_jobs(
        DreamJob(
            name="formation",
            kind=DreamJobKind.FORMATION,
            cadence_seconds=1,
        ),
        DreamJob(
            name="consolidation-recursive",
            kind=DreamJobKind.CONSOLIDATION,
            cadence_seconds=1,
            rollup_consolidation=True,
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.0,
                min_cluster_size=3,
                max_depth=2,
            ),
        ),
    )
    client = Memotron(graph_path=tmp_path / "ws4-recursive.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ws4-recursive")
    now = datetime(2026, 6, 1, tzinfo=UTC)

    # Seed 9 facts using PREFERS (MULTI_ACTIVE) so all remain active (not superseded).
    # With cluster_threshold=0.0 and min_cluster_size=3, all 9 cluster into 1 rollup at depth=1.
    for i in range(9):
        await client.add_memory(
            subject="Acme Parks",
            predicate="prefers",
            object=f"preference-{i}",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.85,
        )

    # Run rollup consolidation (max_depth=2).
    await client.run_dream_job(job_name="consolidation-recursive", now=now)

    all_rels = [
        r for r in client.graph.relationships() if r.type != "MENTIONS" and r.properties.get("scope_key") == scope.key
    ]
    rollup_rels = [r for r in all_rels if r.properties.get("memory_type") == "rollup"]

    # Check that rollups have bounded depth.
    for rollup in rollup_rels:
        depth = rollup.properties.get("rollup_depth", 0)
        assert depth <= 2, f"rollup_depth {depth} exceeds max_depth=2"

    # At least one rollup was created.
    assert len(rollup_rels) >= 1, "Expected at least one ROLLUP to be created"


# ---------------------------------------------------------------------------
# WS-6: Growth governance & proof tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_cap_prunes_lowest_value_over_ceiling(tmp_path: Path) -> None:
    """WS-6 acceptance proof — soft cap prunes lowest-value rows when ceiling is exceeded.

    Uses add_memory(valid_from=now) to bypass formation/semantic-dedup and populate
    exact active rows, then verifies the soft cap removes the lowest-value rows.

    Proves:
    1. When GrowthPolicy.soft_cap is set, context-visible count is brought back to the cap
       by pruning the LOWEST-VALUE rows (high-value rows survive).
    2. pruning_context_budget_exceeded decisions are recorded for each cap-triggered prune.
    3. With default GrowthPolicy (no cap), normal pruning runs unchanged.
    """
    from memotron import GrowthPolicy
    from memotron.config import RetentionPolicy

    now = datetime(2026, 6, 1, tzinfo=UTC)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="test-cap")

    # --- Part A: Soft cap fires and prunes lowest-value rows ---
    cap = 2  # Allow only 2 context-visible actives.
    config_capped = config_with_jobs(
        DreamJob(name="pruning-cap", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.01,  # Don't prune by confidence floor.
            growth=GrowthPolicy(soft_cap=cap),
            retention=RetentionPolicy(protected_memory_types=()),
        ),
    )
    client_a = Memotron(graph_path=tmp_path / "cap_a.sqlite", config=config_capped)

    # Successful downstream use, not truth confidence, determines which rows
    # survive.  PREFERS is multi-active; valid_from=now ensures visibility.
    high_value_objects = ["urgent-soc2-compliance", "mandatory-iso27001-audit"]
    low_value_objects = ["misc-preference-note", "optional-context-hint"]

    for obj in high_value_objects:
        await client_a.add_memory(
            subject="Acme Parks",
            predicate="prefers",
            object=obj,
            relationship_type="PREFERS",
            confidence=0.95,
            scope=scope,
            valid_from=now,
        )
    for obj in low_value_objects:
        await client_a.add_memory(
            subject="Acme Parks",
            predicate="prefers",
            object=obj,
            relationship_type="PREFERS",
            confidence=0.10,
            scope=scope,
            valid_from=now,
        )

    for relationship in client_a.graph.relationships():
        object_name = str(client_a.graph.get_node(relationship.target_uuid).properties["name"])
        if object_name not in high_value_objects:
            continue
        use = await client_a.record_memory_use(
            relationship_uuid=relationship.uuid,
            scope=scope,
            kind=UseEventKind.CITED_OR_USED,
            task_run_id=f"successful-use:{relationship.uuid}",
            idempotency_key=f"cited:{relationship.uuid}",
            used_at=now,
        )
        await client_a.record_memory_outcome(
            use_id=use.use_id,
            scope=scope,
            verdict=OutcomeVerdict.POSITIVE,
            task_run_id=use.task_run_id,
            idempotency_key=f"outcome:{relationship.uuid}",
            judge_identity="test",
            judge_version="v1",
            judged_at=now,
        )

    # Confirm 4 context-visible actives before pruning.
    rels_before = [
        r
        for r in client_a.graph.relationships()
        if r.type != "MENTIONS"
        and r.properties.get("scope_key") == scope.key
        and r.properties.get("status") == "active"
        and r.properties.get("active_in_context") is not False
    ]
    assert len(rels_before) == 4, f"Expected 4 active before soft-cap pruning, got {len(rels_before)}"

    # Run pruning — soft cap should fire and prune 2 low-value rows.
    await client_a.run_dream_job(job_name="pruning-cap", now=now)

    rels_after = [
        r
        for r in client_a.graph.relationships()
        if r.type != "MENTIONS"
        and r.properties.get("scope_key") == scope.key
        and r.properties.get("status") == "active"
        and r.properties.get("active_in_context") is not False
    ]
    assert len(rels_after) == cap, (
        f"Soft cap of {cap} should leave exactly {cap} context-visible actives, got {len(rels_after)}"
    )

    # The surviving active rows should be the successfully used ones.
    surviving_objects = {str(client_a.graph.get_node(r.target_uuid).properties["name"]) for r in rels_after}
    assert surviving_objects == set(high_value_objects), (
        f"Expected successfully used rows to survive the soft cap prune; got {surviving_objects}"
    )

    # At least one pruning_context_budget_exceeded decision must be recorded.
    decisions = await client_a.dream_decisions(limit=100)
    cap_decisions = [d for d in decisions if d.decision_type == "pruning_context_budget_exceeded"]
    assert len(cap_decisions) >= 1, f"Expected pruning_context_budget_exceeded decisions; got {len(cap_decisions)}"

    # --- Part B: Default GrowthPolicy (no cap) leaves existing pruning unchanged ---
    config_default = config_with_jobs(
        DreamJob(name="pruning-nocap", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.01),
    )
    client_b = Memotron(graph_path=tmp_path / "cap_b.sqlite", config=config_default)
    scope_b = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="test-nocap")
    # Use semantically distinct objects to avoid WS-1 semantic dedup merging them.
    distinct_objects = [
        "apple orchard visit",
        "submarine voyage",
        "telescope stargazing",
        "pottery wheel spinning",
        "glacier hiking expedition",
    ]
    for obj in distinct_objects:
        await client_b.add_memory(
            subject="Entity",
            predicate="prefers",
            object=obj,
            relationship_type="PREFERS",
            confidence=0.80,
            scope=scope_b,
            valid_from=now,
        )
    await client_b.run_dream_job(job_name="pruning-nocap", now=now)

    # No cap-triggered pruning.
    decisions_b = await client_b.dream_decisions(limit=100)
    cap_decisions_b = [d for d in decisions_b if d.decision_type == "pruning_context_budget_exceeded"]
    assert len(cap_decisions_b) == 0, (
        f"Default config should not trigger soft-cap decisions; got {len(cap_decisions_b)}"
    )

    # All 5 facts remain active.
    rels_b = [
        r
        for r in client_b.graph.relationships()
        if r.type != "MENTIONS"
        and r.properties.get("scope_key") == scope_b.key
        and r.properties.get("status") == "active"
    ]
    assert len(rels_b) == 5, f"Default config should leave all 5 facts active; got {len(rels_b)}"

    # GrowthPolicy default: soft_cap=None.
    assert PruningPolicy().growth.soft_cap is None, "Default GrowthPolicy.soft_cap must be None (off)"


@pytest.mark.asyncio
async def test_memory_evolution_reports_compression_and_dedup(tmp_path: Path) -> None:
    """WS-6 acceptance proof — memory_evolution() exposes WS-6 fields.

    Uses add_episode() so the formation pipeline runs normally:
      - Episode 1 and 2: same REQUIRES fact → episode 2 reinforces (observed_count=2).
      - Episode 3: distinct PREFERS fact → new row.
    Result: 3 episodes, 2 active rows, 1 reinforcement.

    Proves:
    - context_visible_relationship_count equals active_relationship_count when no demotions.
    - compression_ratio = episode_count / max(1, context_visible_relationship_count).
    - semantic_dedup_rate > 0 when reinforcement has occurred.
    - per_type_active_counts is populated and sums to context_visible_relationship_count.
    - New WS-6 signals (compression_ratio, semantic_dedup, rollup_coverage) are present.
    """
    config = config_with_jobs(
        DreamJob(name="formation", kind=DreamJobKind.FORMATION, cadence_seconds=1),
        DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.01),
    )
    client = Memotron(graph_path=tmp_path / "evolution.sqlite", config=config)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="evo-test")
    t0 = datetime(2026, 6, 1, tzinfo=UTC)
    t1 = datetime(2026, 6, 2, tzinfo=UTC)

    # 3 episodes: first two repeat the same fact (→ reinforcement), third adds a new one.
    for t in (t0, t1):
        await client.add_episode(
            name=f"ep-{t.isoformat()}",
            episode_body=(
                "Memory: subject=Acme Parks; predicate=requires; object=SOC2 compliance; "
                "relationship_type=REQUIRES; confidence=0.92"
            ),
            source=EpisodeType.MESSAGE,
            scope=scope,
            reference_time=t,
        )
    await client.add_episode(
        name="ep-new-fact",
        episode_body=(
            "Memory: subject=Acme Parks; predicate=prefers; object=email updates; "
            "relationship_type=PREFERS; confidence=0.80"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=t1,
    )

    await client.run_dream_job(job_name="formation", now=t1)
    await client.run_dream_job(job_name="pruning", now=t1)

    proof = await client.memory_evolution(scope=scope, as_of=t1)

    # 3 episodes processed.
    assert proof.episode_count == 3

    # context_visible == active when no demotions.
    assert proof.context_visible_relationship_count == proof.active_relationship_count, (
        "context_visible_relationship_count must equal active_relationship_count when no WS-4 demotion"
    )
    # No rollups, no demotions in this test.
    assert proof.rollup_relationship_count == 0
    assert proof.demoted_relationship_count == 0

    # compression_ratio is exactly episode_count / context_visible_count.
    expected_ratio = proof.episode_count / max(1, proof.context_visible_relationship_count)
    assert abs(proof.compression_ratio - expected_ratio) < 1e-9, (
        f"compression_ratio should be {expected_ratio}, got {proof.compression_ratio}"
    )
    # Must be >= 1.0 since we have 3 episodes and 2 active facts (reinforcement happened).
    assert proof.compression_ratio >= 1.0

    # semantic_dedup_rate > 0 because the REQUIRES fact was reinforced (observed_count=2).
    assert proof.semantic_dedup_rate > 0.0, "semantic_dedup_rate should be > 0 after reinforcement"
    assert 0.0 < proof.semantic_dedup_rate < 1.0

    # per_type_active_counts is populated and sums correctly.
    assert isinstance(proof.per_type_active_counts, dict)
    assert len(proof.per_type_active_counts) > 0, "per_type_active_counts should be non-empty"
    total_from_types = sum(proof.per_type_active_counts.values())
    assert total_from_types == proof.context_visible_relationship_count, (
        "sum of per_type_active_counts must equal context_visible_relationship_count"
    )

    # New WS-6 signals are appended after the original 7.
    signal_names = {s.name for s in proof.signals}
    assert "compression_ratio" in signal_names, "WS-6 signal 'compression_ratio' must be present"
    assert "semantic_dedup" in signal_names, "WS-6 signal 'semantic_dedup' must be present"
    assert "rollup_coverage" in signal_names, "WS-6 signal 'rollup_coverage' must be present"

    # Original 7 signals still present (non-negotiable backward-compat).
    for original in (
        "selection_decisions",
        "reinforcement",
        "supersession",
        "pruning",
        "consolidation",
        "retrieval_filtering",
        "compression",
    ):
        assert original in signal_names, f"Original signal {original!r} must still be present"

    signals = {s.name: s for s in proof.signals}
    # compression_ratio signal observed when ratio > 1.0.
    assert signals["compression_ratio"].observed == (proof.compression_ratio > 1.0)
    # semantic_dedup signal observed when dedup_rate > 0.
    assert signals["semantic_dedup"].observed is True
    # rollup_coverage not observed (no rollups).
    assert signals["rollup_coverage"].observed is False


@pytest.mark.asyncio
async def test_escalation_resolved_compression_ratio_rises(tmp_path: Path) -> None:
    """WS-6 escalation proof — the 271-fact escalation, resolved.

    Uses add_memory(valid_from=now) for deterministic graph population.

    Proves:
    - Compression ratio is >= 1.0 once episodes >= active rows.
    - context_visible count equals active count when no demotions.
    - Soft cap at K+1 does NOT fire (count already within ceiling).
    - Soft cap at K-1 DOES fire and brings count within the cap.
    """
    from memotron import GrowthPolicy
    from memotron.config import RetentionPolicy

    now = datetime(2026, 6, 15, tzinfo=UTC)
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="escalation-proof")

    K = 5
    objects = [
        "verify order id before any escalation call",
        "summarize ticket history at session start",
        "always confirm customer tier before pricing",
        "log incident report within two hours of sla breach",
        "transfer warm with full context in customer notes",
    ]
    assert len(objects) == K

    # --- No cap: K facts, compression_ratio = 1.0 (K episodes / K active). ---
    config_no_cap = config_with_jobs(
        DreamJob(name="pruning-nocap", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.01),
    )
    client = Memotron(graph_path=tmp_path / "esc.sqlite", config=config_no_cap)
    for obj in objects:
        await client.add_memory(
            subject="Support Agent",
            predicate="should",
            object=obj,
            relationship_type="SHOULD",
            confidence=0.80,
            scope=scope,
            valid_from=now,
        )
    await client.run_dream_job(job_name="pruning-nocap", now=now)
    proof = await client.memory_evolution(scope=scope, as_of=now)

    assert proof.context_visible_relationship_count == K
    assert abs(proof.compression_ratio - 1.0) < 1e-9, (
        f"With K episodes and K active rows, ratio should be 1.0, got {proof.compression_ratio}"
    )
    decisions = await client.dream_decisions(limit=200)
    assert not any(d.decision_type == "pruning_context_budget_exceeded" for d in decisions)
    assert proof.per_type_active_counts, "per_type_active_counts must be non-empty"

    # --- Cap at K+1: does NOT fire (count already within ceiling). ---
    config_k1 = config_with_jobs(
        DreamJob(name="pruning-k1", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(min_confidence=0.01, growth=GrowthPolicy(soft_cap=K + 1)),
    )
    client_k1 = Memotron(graph_path=tmp_path / "esc_k1.sqlite", config=config_k1)
    scope_k1 = MemoryScope(kind=ScopeKind.AGENT, scope_id="esc-k1")
    for obj in objects:
        await client_k1.add_memory(
            subject="Support Agent",
            predicate="should",
            object=obj,
            relationship_type="SHOULD",
            confidence=0.80,
            scope=scope_k1,
            valid_from=now,
        )
    await client_k1.run_dream_job(job_name="pruning-k1", now=now)
    proof_k1 = await client_k1.memory_evolution(scope=scope_k1, as_of=now)
    assert proof_k1.context_visible_relationship_count == K, (
        f"Cap at K+1 ({K + 1}) should not prune any rows; got {proof_k1.context_visible_relationship_count}"
    )
    decisions_k1 = await client_k1.dream_decisions(limit=200)
    cap_d_k1 = [d for d in decisions_k1 if d.decision_type == "pruning_context_budget_exceeded"]
    assert len(cap_d_k1) == 0, f"Cap at K+1 should not fire when count == K; got {len(cap_d_k1)} cap decisions"

    # --- Cap at K-1: DOES fire and prunes exactly 1 row. ---
    cap = K - 1
    config_tight = config_with_jobs(
        DreamJob(name="pruning-tight", kind=DreamJobKind.PRUNING, cadence_seconds=1),
        pruning=PruningPolicy(
            min_confidence=0.01,
            growth=GrowthPolicy(soft_cap=cap),
            retention=RetentionPolicy(protected_memory_types=()),
        ),
    )
    client_tight = Memotron(graph_path=tmp_path / "esc_tight.sqlite", config=config_tight)
    scope_tight = MemoryScope(kind=ScopeKind.AGENT, scope_id="esc-tight")
    for obj in objects:
        await client_tight.add_memory(
            subject="Support Agent",
            predicate="should",
            object=obj,
            relationship_type="SHOULD",
            confidence=0.80,
            scope=scope_tight,
            valid_from=now,
        )
    await client_tight.run_dream_job(job_name="pruning-tight", now=now)
    proof_tight = await client_tight.memory_evolution(scope=scope_tight, as_of=now)
    assert proof_tight.context_visible_relationship_count <= cap, (
        f"After tight soft cap ({cap}), context-visible count should be <= {cap}; "
        f"got {proof_tight.context_visible_relationship_count}"
    )
    decisions_tight = await client_tight.dream_decisions(limit=200)
    cap_d_tight = [d for d in decisions_tight if d.decision_type == "pruning_context_budget_exceeded"]
    assert len(cap_d_tight) >= 1, (
        f"Tight soft cap ({cap}) below active count ({K}) should fire; got {len(cap_d_tight)} cap decisions"
    )

    # New WS-6 signals present in all proofs.
    for p, label in [(proof, "no-cap"), (proof_k1, "k+1-cap"), (proof_tight, "tight-cap")]:
        signal_names = {s.name for s in p.signals}
        for sig in ("compression_ratio", "semantic_dedup", "rollup_coverage"):
            assert sig in signal_names, f"WS-6 signal {sig!r} missing in {label} proof"
