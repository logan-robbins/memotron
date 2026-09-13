"""WS-27 -- Ontology hardening for 2026 LLM/agent comprehension.

Covers all six units from NEXT.md WS-27 (the feat/next-ws25-ws28 roadmap):

- T1: required disambiguating `description` + first-class `aliases`.
- T2: required vs optional properties per node label.
- T3: dual-representation invariant (triple + embedded fact sentence).
- T4: the `Concept`/general-label catch-all guardrail.
- T5: temporal-qualifier legibility (informative-only).
- T6: the tenant vocabulary extension contract.

Each unit is proven at the pure-function level where one exists (fast,
hermetic, no LLM) and end to end through `Memotron.add_episode` +
`run_due_dreams()` using `RuleBasedExtractionTransport` (`EpisodeType.JSON`,
the cookbook `entities`/`relations` envelope) -- no LiteLLM key, no network,
fully deterministic, matching every other formation test in this suite.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    MemoryHealthPolicy,
    MemoryScope,
    NodeInstruction,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.config import UNIVERSAL_NODE_PROPERTIES, default_config
from memotron.context import (
    ContextPolicy,
    _qualified_fact_text,
    _temporal_qualifier,
    get_context,
)
from memotron.dreaming import DualRepresentationInvariantError
from memotron.extraction import CandidateViolation
from memotron.health import compute_memory_health
from memotron.models import (
    DreamJobKind,
    MemoryHealthGateError,
    MemoryProfileFact,
    MemoryType,
    RelationshipStatus,
)
from memotron.receipts import ReceiptDecisionType

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared helpers (mirrors tests/test_cookbook_entity_roster.py /
# tests/test_candidate_schema_rejection.py's conventions).
# ---------------------------------------------------------------------------


def _scope(scope_id: str = "ontology") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _client(tmp_path: Path, config: DreamConfig, name: str = "graph.sqlite") -> Memotron:
    return Memotron(graph_path=tmp_path / name, config=config)


def _cookbook_body(entities: list[dict], relations: list[dict]) -> str:
    return json.dumps({"entities": entities, "relations": relations})


def _quarantined(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_QUARANTINED.value
    ]


def _active_relationships(client: Memotron, scope: MemoryScope):
    return [
        relationship
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    ]


def _node_named(client: Memotron, name: str):
    graph = client.export_graph()
    return next(node for node in graph["nodes"] if node["properties"].get("name") == name)


def _single_config(
    *,
    node_instructions: tuple[NodeInstruction, ...],
    relationship_instructions: tuple[RelationshipInstruction, ...],
    health: MemoryHealthPolicy | None = None,
) -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=node_instructions,
        relationship_instructions=relationship_instructions,
    )
    kwargs: dict = {}
    if health is not None:
        kwargs["health"] = health
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(DreamJob(name="formation-default", kind=DreamJobKind.FORMATION, cadence_seconds=1),),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# T1 -- required disambiguating description + first-class aliases
# ---------------------------------------------------------------------------


def _t1_config() -> DreamConfig:
    return _single_config(
        node_instructions=(
            NodeInstruction(
                label="System",
                query="A named platform component.",
                properties=("kind",),
                strict_properties=False,
                require_description=True,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="*",
                target_label="*",
                query="Any requirement between two systems.",
                open_predicate=True,
            ),
        ),
    )


def test_aliases_is_a_universal_node_property() -> None:
    """[T1] `aliases` sits alongside `description` -- every label accepts it
    without having to enumerate it in its own `properties` whitelist."""
    assert "description" in UNIVERSAL_NODE_PROPERTIES
    assert "aliases" in UNIVERSAL_NODE_PROPERTIES


def test_node_instruction_rejects_a_required_property_absent_from_its_own_whitelist() -> None:
    """A NodeInstruction requiring a key it could never actually receive is a
    config mistake caught at construction, not discovered via a mysteriously
    all-quarantined label at run time."""
    with pytest.raises(ValueError, match="not in its own allowed property set"):
        NodeInstruction(
            label="Credential",
            query="x",
            properties=("description",),
            required_properties=("reference",),
        )


@pytest.mark.asyncio
async def test_entity_without_description_is_quarantined(tmp_path: Path) -> None:
    """[T1 unit] An entity with no description, for a require_description
    label, is quarantined with a specific, machine-readable reason -- never
    silently rejected and never silently materialized."""
    client = _client(tmp_path, _t1_config())
    scope = _scope("t1-missing-description")
    await client.add_episode(
        name="no-description",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Jedai Gateway", "type": "System"},  # no description
                {"name": "LiteLLM", "type": "System", "description": "the proxy layer"},
            ],
            relations=[
                {
                    "source": "Jedai Gateway",
                    "predicate": "requires",
                    "target": "LiteLLM",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _active_relationships(client, scope) == []
    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    payload = json.loads(quarantined[0].event_payload)
    assert payload["violation"] == CandidateViolation.ENTITY_DESCRIPTION_MISSING.value
    assert quarantined[0].decision_reason == "quarantined:entity_description_missing"


@pytest.mark.asyncio
async def test_entity_with_description_materializes_normally(tmp_path: Path) -> None:
    """The require_description gate costs nothing to a well-formed candidate."""
    client = _client(tmp_path, _t1_config())
    scope = _scope("t1-good-description")
    await client.add_episode(
        name="with-description",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Jedai Gateway", "type": "System", "description": "the AI gateway"},
                {"name": "LiteLLM", "type": "System", "description": "the proxy layer"},
            ],
            relations=[
                {
                    "source": "Jedai Gateway",
                    "predicate": "requires",
                    "target": "LiteLLM",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _quarantined(client, scope) == []
    relationships = _active_relationships(client, scope)
    assert len(relationships) == 1


@pytest.mark.asyncio
async def test_declared_alias_registers_into_entity_canon(tmp_path: Path) -> None:
    """[T1 unit] An entity's `aliases` property is written into the SAME
    entity_canon registry the WS-17 resolution engine reads -- an alias
    query resolves to the canonical name."""
    client = _client(tmp_path, default_config())
    scope = _scope("t1-alias-registers")
    await client.add_episode(
        name="alias-seed",
        episode_body=_cookbook_body(
            entities=[
                {
                    "name": "guest content experience",
                    "type": "Entity",
                    "description": "the GCX product surface",
                    "properties": {"aliases": ["GCX"]},
                },
                {"name": "content pipeline", "type": "Entity", "description": "publishes GCX content"},
            ],
            relations=[
                {
                    "source": "guest content experience",
                    "predicate": "depends on",
                    "target": "content pipeline",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert client.graph.canonical_entity_name_for(scope.key, "GCX") == "guest content experience"
    row = client.graph.entity_alias_row(scope.key, "GCX")
    assert row is not None
    assert row["status"] == "active"
    assert row["decided_by"] == "extraction:declared_alias"


@pytest.mark.asyncio
async def test_gcx_and_guest_content_experience_resolve_to_one_entity(tmp_path: Path) -> None:
    """[T1 integration] "GCX" and "guest content experience" resolve to ONE
    entity: a fact stated later under the alias "GCX" lands on the SAME node
    the canonical entity already materialized under."""
    client = _client(tmp_path, default_config())
    scope = _scope("t1-bidirectional")

    await client.add_episode(
        name="episode-1-canonical",
        episode_body=_cookbook_body(
            entities=[
                {
                    "name": "guest content experience",
                    "type": "Entity",
                    "description": "the GCX product surface",
                    "properties": {"aliases": ["GCX"]},
                },
                {"name": "content pipeline", "type": "Entity", "description": "publishes GCX content"},
            ],
            relations=[
                {
                    "source": "guest content experience",
                    "predicate": "depends on",
                    "target": "content pipeline",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    # A SECOND, later episode mentions only the bare alias "GCX" -- no
    # entity_ref, no repeated aliases property, exactly what a real document
    # does when it later abbreviates something it already named in full.
    await client.add_episode(
        name="episode-2-alias-mention",
        episode_body=_cookbook_body(
            entities=[
                {"name": "GCX", "type": "Entity", "description": "second mention"},
                {"name": "translation service", "type": "Entity", "description": "localizes content"},
            ],
            relations=[
                {
                    "source": "GCX",
                    "predicate": "depends on",
                    "target": "translation service",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )
    await client.run_due_dreams()

    node = _node_named(client, "guest content experience")
    assert _node_named_missing(client, "GCX")
    # Both relationships attach to the ONE canonical subject node.
    subject_uuids = {relationship.source_uuid for relationship in _active_relationships(client, scope)}
    assert subject_uuids == {node["uuid"]}


def _node_named_missing(client: Memotron, name: str) -> bool:
    graph = client.export_graph()
    return not any(node["properties"].get("name") == name for node in graph["nodes"])


# ---------------------------------------------------------------------------
# T2 -- required vs optional properties per type
# ---------------------------------------------------------------------------


def _t2_config() -> DreamConfig:
    return _single_config(
        node_instructions=(
            NodeInstruction(
                label="Credential",
                query="A credential kind.",
                properties=("description", "reference", "env"),
                strict_properties=False,
                required_properties=("reference",),
            ),
            NodeInstruction(
                label="System",
                query="A named platform component.",
                properties=("description",),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="*",
                target_label="*",
                query="Any requirement.",
                open_predicate=True,
            ),
        ),
    )


@pytest.mark.asyncio
async def test_credential_without_reference_is_quarantined(tmp_path: Path) -> None:
    """[T2 unit] A Credential entity with no `reference` (or a bare value and
    no ref) is quarantined with `required_property_missing`, never stored as
    if it were complete."""
    client = _client(tmp_path, _t2_config())
    scope = _scope("t2-missing-reference")
    await client.add_episode(
        name="credential-no-ref",
        episode_body=_cookbook_body(
            entities=[
                {"name": "virtual key", "type": "Credential", "description": "an API credential"},
                {"name": "Jedai Gateway", "type": "System", "description": "the AI gateway"},
            ],
            relations=[
                {
                    "source": "Jedai Gateway",
                    "predicate": "requires",
                    "target": "virtual key",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _active_relationships(client, scope) == []
    quarantined = _quarantined(client, scope)
    assert len(quarantined) == 1
    payload = json.loads(quarantined[0].event_payload)
    assert payload["violation"] == CandidateViolation.REQUIRED_PROPERTY_MISSING.value


@pytest.mark.asyncio
async def test_credential_with_reference_materializes(tmp_path: Path) -> None:
    client = _client(tmp_path, _t2_config())
    scope = _scope("t2-good-reference")
    await client.add_episode(
        name="credential-with-ref",
        episode_body=_cookbook_body(
            entities=[
                {
                    "name": "virtual key",
                    "type": "Credential",
                    "description": "an API credential",
                    "properties": {"reference": "LITELLM_API_KEY"},
                },
                {"name": "Jedai Gateway", "type": "System", "description": "the AI gateway"},
            ],
            relations=[
                {
                    "source": "Jedai Gateway",
                    "predicate": "requires",
                    "target": "virtual key",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _quarantined(client, scope) == []
    node = _node_named(client, "virtual key")
    assert node["properties"]["reference"] == "LITELLM_API_KEY"
    assert "value" not in node["properties"]


# ---------------------------------------------------------------------------
# T3 -- dual-representation invariant
# ---------------------------------------------------------------------------


class _EmptyEmbeddingTransport:
    """A pathological EmbeddingTransport that always returns an empty vector
    -- simulates a hosted transport failing open, which the invariant must
    catch BEFORE a relation with no retrievable embedding ever reaches the
    graph."""

    identifier = "empty-vector-test-transport"

    def embed(self, text: str) -> list[float]:
        return []


@pytest.mark.asyncio
async def test_materialized_relationship_carries_both_triple_and_embedding(tmp_path: Path) -> None:
    """[T3 unit] Every materialized relation carries a structured triple
    (source/target node names + predicate) AND a stored embedding of the
    full fact sentence."""
    client = _client(tmp_path, default_config())
    scope = _scope("t3-dual-representation")
    await client.add_episode(
        name="dual-rep",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Acme Parks", "type": "Entity", "description": "the customer"},
                {"name": "SOC2 report", "type": "Entity", "description": "an audit artifact"},
            ],
            relations=[
                {
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "SOC2 report",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    relationships = _active_relationships(client, scope)
    assert len(relationships) == 1
    relationship = relationships[0]
    # The triple: two real endpoint nodes plus a predicate.
    assert relationship.properties["predicate"] == "requires"
    assert relationship.properties["object"] == "SOC2 report"
    subject_node = client.graph.get_node(relationship.source_uuid)
    assert subject_node.properties["name"] == "Acme Parks"
    # The embedded fact sentence.
    embedding = relationship.properties["embedding"]
    assert isinstance(embedding, list)
    assert len(embedding) > 0
    assert any(component != 0.0 for component in embedding)


@pytest.mark.asyncio
async def test_empty_embedding_fails_closed_before_materialization(tmp_path: Path) -> None:
    """[T3 fail-closed] A transport that returns an empty vector must never
    let the relation reach the graph missing its embedded fact sentence."""
    client = Memotron(
        graph_path=tmp_path / "empty-embedding.sqlite",
        config=default_config(),
        embedding_transport=_EmptyEmbeddingTransport(),
    )
    scope = _scope("t3-empty-embedding")
    await client.add_episode(
        name="dual-rep-broken",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Acme Parks", "type": "Entity", "description": "the customer"},
                {"name": "SOC2 report", "type": "Entity", "description": "an audit artifact"},
            ],
            relations=[
                {
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "SOC2 report",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    with pytest.raises(DualRepresentationInvariantError, match="no embedded fact sentence"):
        await client.run_due_dreams()

    # Nothing about the broken candidate reached the graph as a relationship.
    assert _active_relationships(client, scope) == []


# ---------------------------------------------------------------------------
# T4 -- the Concept/general-label catch-all guardrail
# ---------------------------------------------------------------------------


def test_general_label_share_trips_the_block_gate() -> None:
    """[T4 unit] A label distribution that puts most of the corpus into a
    general/catch-all label trips the ceiling, on the SAME MemoryHealthReport
    the existing memory_type gates use."""
    report = compute_memory_health(
        scope=_scope("t4-unit"),
        per_type_counts={},
        policy=MemoryHealthPolicy(min_instances=4, max_general_label_share_block=0.5),
        evaluated_at=NOW,
        entity_label_counts={"Concept": 6, "System": 4},
    )
    assert report.label_gates_evaluated is True
    assert report.general_label_share == pytest.approx(0.6)
    assert report.general_labels_observed == ["Concept"]
    trips = [trip for trip in report.trips if trip.gate == "general_label_share"]
    assert len(trips) == 1
    assert trips[0].severity == "block"


def test_general_label_share_below_ceiling_is_inert() -> None:
    report = compute_memory_health(
        scope=_scope("t4-unit-healthy"),
        per_type_counts={},
        policy=MemoryHealthPolicy(min_instances=4, max_general_label_share_block=0.5),
        evaluated_at=NOW,
        entity_label_counts={"Concept": 2, "System": 8},
    )
    assert report.general_label_share == pytest.approx(0.2)
    assert [trip for trip in report.trips if trip.gate == "general_label_share"] == []


def test_no_entity_label_counts_is_a_byte_identical_no_op() -> None:
    """Existing callers of compute_memory_health that never pass
    entity_label_counts get the SAME report shape as before this unit
    landed -- the new fields sit at their inert defaults."""
    report = compute_memory_health(
        scope=_scope("t4-noop"),
        per_type_counts={"requirement": 10},
        policy=MemoryHealthPolicy(),
        evaluated_at=NOW,
    )
    assert report.entity_label_counts == {}
    assert report.general_label_share == 0.0
    assert report.general_labels_observed == []
    assert report.label_gates_evaluated is False
    assert all(trip.gate != "general_label_share" for trip in report.trips)


def _t4_config() -> DreamConfig:
    return _single_config(
        node_instructions=(
            NodeInstruction(
                label="Concept", query="A catch-all term.", properties=("description",), strict_properties=False
            ),
            NodeInstruction(
                label="System",
                query="A named platform component.",
                properties=("description",),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="*",
                target_label="*",
                query="Any relation.",
                open_predicate=True,
            ),
        ),
        health=MemoryHealthPolicy(min_instances=4, max_general_label_share_block=0.5, raise_on_block=True),
    )


@pytest.mark.asyncio
async def test_live_formation_trips_the_concept_guardrail(tmp_path: Path) -> None:
    """[T4 integration] A corpus that pushes most of its entities into the
    catch-all label blocks formation with the SAME MemoryHealthGateError the
    memory_type gates already raise, carrying the general_label_share trip."""
    client = _client(tmp_path, _t4_config())
    scope = _scope("t4-live")
    await client.add_episode(
        name="concept-heavy",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Four Nines", "type": "Concept", "description": "an availability class"},
                {"name": "Nightly Batch", "type": "Concept", "description": "a scheduling term"},
                {"name": "Cold Start", "type": "Concept", "description": "a latency term"},
                {"name": "Jedai Platform", "type": "System", "description": "the platform"},
                {"name": "DLP Platform", "type": "System", "description": "the DLP platform"},
            ],
            relations=[
                {
                    "source": "Four Nines",
                    "predicate": "relates to",
                    "target": "Jedai Platform",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.8,
                },
                {
                    "source": "Nightly Batch",
                    "predicate": "relates to",
                    "target": "Jedai Platform",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.8,
                },
                {
                    "source": "Cold Start",
                    "predicate": "relates to",
                    "target": "DLP Platform",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.8,
                },
                {
                    "source": "Jedai Platform",
                    "predicate": "relates to",
                    "target": "DLP Platform",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.8,
                },
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    with pytest.raises(MemoryHealthGateError) as excinfo:
        await client.run_due_dreams()

    report = excinfo.value.report
    assert report.entity_label_counts == {"Concept": 3, "System": 2}
    assert report.general_label_share == pytest.approx(0.6)
    trips = [trip for trip in report.trips if trip.gate == "general_label_share"]
    assert len(trips) == 1 and trips[0].severity == "block"


# ---------------------------------------------------------------------------
# T5 -- temporal-qualifier legibility (informative-only)
# ---------------------------------------------------------------------------


def _profile_fact(
    *,
    status: RelationshipStatus = RelationshipStatus.ACTIVE,
    valid_from: datetime | None = None,
    valid_to: datetime | None = None,
) -> MemoryProfileFact:
    return MemoryProfileFact(
        relationship_uuid="u1",
        relationship_type="FACT",
        fact="the gateway requires a virtual key",
        scope=_scope("t5"),
        subject="the gateway",
        predicate="requires",
        object="a virtual key",
        confidence=0.9,
        status=status,
        valid_from=valid_from,
        valid_to=valid_to,
        created_by="test",
    )


def test_context_policy_rejects_negative_recency_window() -> None:
    with pytest.raises(ValueError, match="recency_window_seconds cannot be negative"):
        ContextPolicy(recency_window_seconds=-1.0)


def test_stable_current_fact_renders_bare() -> None:
    """[T5 unit] A stable current fact (no valid_from/valid_to signal, active
    status) gets no qualifier -- a qualifier on every line is token spend
    the budget cannot afford."""
    fact = _profile_fact()
    assert _temporal_qualifier(fact, now=NOW, recency_window_seconds=14 * 86400) is None
    assert _qualified_fact_text(fact, now=NOW, recency_window_seconds=14 * 86400) == fact.fact


def test_recently_established_fact_is_qualified() -> None:
    fact = _profile_fact(valid_from=NOW - timedelta(days=2))
    qualifier = _temporal_qualifier(fact, now=NOW, recency_window_seconds=14 * 86400)
    assert qualifier is not None
    assert "as of" in qualifier
    text = _qualified_fact_text(fact, now=NOW, recency_window_seconds=14 * 86400)
    # The original fact text always survives as a contiguous substring.
    assert fact.fact in text
    assert text != fact.fact


def test_old_fact_outside_recency_window_renders_bare() -> None:
    fact = _profile_fact(valid_from=NOW - timedelta(days=90))
    assert _temporal_qualifier(fact, now=NOW, recency_window_seconds=14 * 86400) is None


def test_superseded_fact_is_always_qualified_regardless_of_age() -> None:
    """[T5 unit] A superseded fact in a historical view is ALWAYS marked --
    unconditionally, unlike the recency-window case above."""
    fact = _profile_fact(
        status=RelationshipStatus.SUPERSEDED,
        valid_from=NOW - timedelta(days=400),
        valid_to=NOW - timedelta(days=100),
    )
    qualifier = _temporal_qualifier(fact, now=NOW, recency_window_seconds=14 * 86400)
    assert qualifier is not None
    assert "superseded" in qualifier
    text = _qualified_fact_text(fact, now=NOW, recency_window_seconds=14 * 86400)
    assert fact.fact in text
    assert "superseded" in text


def test_explicit_valid_to_is_always_qualified() -> None:
    fact = _profile_fact(valid_to=NOW + timedelta(days=30))
    qualifier = _temporal_qualifier(fact, now=NOW, recency_window_seconds=14 * 86400)
    assert qualifier is not None
    assert "valid until" in qualifier


def test_qualifiers_disabled_renders_bare_regardless() -> None:
    fact = _profile_fact(valid_from=NOW - timedelta(days=1))
    text = _qualified_fact_text(fact, now=NOW, recency_window_seconds=14 * 86400, enabled=False)
    assert text == fact.fact


class _StubContextTransport:
    identifier = "stub-ontology-test"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:  # pragma: no cover - unused
        raise AssertionError("test never expects the LLM transport to be called")


class _BoomContextTransport:
    identifier = "boom-ontology-test"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        raise ValueError("gateway unavailable")


@pytest.mark.asyncio
async def test_get_context_fallback_qualifies_only_the_recent_fact(tmp_path: Path) -> None:
    """[T5 integration] `get_context()`'s deterministic fallback path (fully
    code-driven, no LLM in the loop) shows currency exactly where it is
    non-obvious: the stable fact renders bare, the recent one is qualified."""
    from memotron.graph import PropertyGraphStore

    graph = PropertyGraphStore(tmp_path / "t5.sqlite")
    scope = _scope("t5-fallback")
    stable = MemoryProfileFact(
        relationship_uuid="u-stable",
        relationship_type="FACT",
        fact="Always use uv, never bare python",
        scope=scope,
        subject="agent",
        predicate="should",
        object="use uv",
        confidence=0.95,
        status=RelationshipStatus.ACTIVE,
        created_by="test",
        memory_type="directive",
    )
    recent = MemoryProfileFact(
        relationship_uuid="u-recent",
        relationship_type="FACT",
        fact="Currently investigating the flaky retrieval test",
        scope=scope,
        subject="agent",
        predicate="is",
        object="investigating",
        confidence=0.8,
        status=RelationshipStatus.ACTIVE,
        valid_from=NOW - timedelta(hours=1),
        created_by="test",
        memory_type="state",
    )

    artifact = await get_context(
        graph=graph,
        scope=scope,
        facts=[stable, recent],
        transport=_BoomContextTransport(),
        now=NOW,
    )

    assert artifact.degraded is True
    assert "Always use uv, never bare python" in artifact.rendered_text
    # The stable fact renders bare -- no parenthetical appended to it.
    assert "Always use uv, never bare python (" not in artifact.rendered_text
    assert "Currently investigating the flaky retrieval test (as of" in artifact.rendered_text


# ---------------------------------------------------------------------------
# T6 -- tenant vocabulary extension contract
# ---------------------------------------------------------------------------


def test_relationship_endpoint_outside_allowed_labels_fails_fast() -> None:
    """[T6] A relationship instruction naming an endpoint label absent from
    this SAME instruction set's node labels is rejected at CONSTRUCTION
    time -- a config typo, not a silent 100%-rejection-rate surprise once a
    real ingest runs."""
    with pytest.raises(ValueError, match="is not one of this instruction set's node labels"):
        DreamInstructionSet(
            name="broken-tenant",
            node_instructions=(NodeInstruction(label="Book", query="A published book.", properties=("description",)),),
            relationship_instructions=(
                RelationshipInstruction(
                    type="WROTE",
                    source_label="Author",  # typo: never declared as a node label
                    target_label="Book",
                    query="An authorship relation.",
                    memory_type=MemoryType.ANCHOR,
                ),
            ),
        )


def test_open_predicate_wildcard_endpoints_are_exempt_from_the_check() -> None:
    """`"*"` (any allowed label) is not itself a label and must not be
    validated as one."""
    instructions = DreamInstructionSet(
        name="wildcard-tenant",
        node_instructions=(NodeInstruction(label="Book", query="A published book.", properties=("description",)),),
        relationship_instructions=(
            RelationshipInstruction(
                type="RELATES_TO",
                source_label="*",
                target_label="*",
                query="Any relation.",
                open_predicate=True,
                memory_type=MemoryType.ANCHOR,
            ),
        ),
    )
    assert instructions.allowed_labels == {"Book"}


@pytest.mark.asyncio
async def test_a_second_tenant_vocabulary_ingests_under_its_own_labels(tmp_path: Path) -> None:
    """[T6 integration] A second sample project, with its OWN closed entity
    vocabulary (nothing borrowed from the built-in `Entity` label or the
    JedAI KB pilot's labels), validates and extracts end to end."""
    config = _single_config(
        node_instructions=(
            NodeInstruction(
                label="Book",
                query="A published book.",
                properties=("description", "isbn"),
                strict_properties=False,
                require_description=True,
            ),
            NodeInstruction(
                label="Author",
                query="A person who wrote a book.",
                properties=("description",),
                strict_properties=False,
                require_description=True,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="WROTE",
                source_label="Author",
                target_label="Book",
                query="An authorship relation.",
                min_confidence=0.5,
                memory_type=MemoryType.ANCHOR,
            ),
        ),
    )
    client = _client(tmp_path, config, name="second-tenant.sqlite")
    scope = _scope("second-tenant")
    await client.add_episode(
        name="library-catalog",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Ursula K. Le Guin", "type": "Author", "description": "a science fiction author"},
                {
                    "name": "The Left Hand of Darkness",
                    "type": "Book",
                    "description": "a 1969 novel",
                    "properties": {"isbn": "978-0441478125"},
                },
            ],
            relations=[
                {
                    "source": "Ursula K. Le Guin",
                    "predicate": "wrote",
                    "target": "The Left Hand of Darkness",
                    "relationship_type": "WROTE",
                    "confidence": 0.95,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _quarantined(client, scope) == []
    relationships = _active_relationships(client, scope)
    assert len(relationships) == 1
    assert relationships[0].properties["object"] == "The Left Hand of Darkness"
    book = _node_named(client, "The Left Hand of Darkness")
    assert book["properties"]["isbn"] == "978-0441478125"
