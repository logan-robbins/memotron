"""Cookbook-shaped extraction output ([0025]): entities first, as their own
roster, then relations that may ONLY name an entity from it.

This is the structural guarantee against phrase-nodes that label-checking
alone cannot give: a closed label set stops a model from inventing an
untyped node, but nothing before this stopped a relation from naming an
entity that was never itself extracted and described (a comma-separated list
folded into a single made-up "entity", a typo'd reference, a name the model
never bothered to roster). A relation whose ``source``/``target`` is absent
from the SAME response's ``entities[]`` is now rejected before it ever
reaches label/predicate/confidence validation.

The legacy flat ``memories`` shape (each entry self-contained: ``subject``,
``subject_label``, ``subject_properties``, ...) remains fully supported --
see :func:`memotron.extraction.decode_extraction_envelope` -- so every
pre-existing ``EpisodeType.JSON`` test fixture in this repository is
unaffected by this file's tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memotron import Memotron, EpisodeType, MemoryScope, ScopeKind
from memotron.config import default_config
from memotron.extraction import (
    CandidateViolation,
    CookbookEnvelope,
    InstructionalExtractor,
    decode_extraction_envelope,
)
from memotron.receipts import ReceiptDecisionType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _scope(scope_id: str = "cookbook") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def _client(tmp_path: Path) -> Memotron:
    return Memotron(graph_path=tmp_path / "cookbook.sqlite", config=default_config())


def _cookbook_body(entities: list[dict], relations: list[dict]) -> str:
    return json.dumps({"entities": entities, "relations": relations})


def _rejections(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED.value
    ]


def _active_objects(client: Memotron, scope: MemoryScope) -> list[str]:
    return sorted(
        str(relationship.properties["object"])
        for relationship in client.graph.active_relationships(scope=scope)
        if relationship.type != "MENTIONS"
    )


# ---------------------------------------------------------------------------
# 1. decode_extraction_envelope -- envelope-shape detection, pure function
# ---------------------------------------------------------------------------


def test_cookbook_shape_decodes_to_an_envelope() -> None:
    payload = {
        "entities": [{"name": "User 42", "type": "Entity", "description": "a user"}],
        "relations": [
            {"source": "User 42", "predicate": "prefers", "target": "dark mode", "relationship_type": "PREFERS"}
        ],
    }
    result = decode_extraction_envelope(payload, source="test")
    assert isinstance(result, CookbookEnvelope)
    assert result.entities == (payload["entities"][0],)
    assert result.relations == (payload["relations"][0],)


def test_legacy_memories_shape_still_decodes_to_a_flat_list() -> None:
    """Byte-identical to pre-[0025] behavior: nothing about this shape changed."""
    payload = {"memories": [{"subject": "a", "predicate": "b", "object": "c"}]}
    result = decode_extraction_envelope(payload, source="test")
    assert result == payload["memories"]


def test_missing_memories_key_is_lenient_by_default() -> None:
    """RuleBasedExtractionTransport's pre-existing policy: an episode body with
    neither shape means zero candidates, not a fatal envelope error."""
    assert decode_extraction_envelope({}, source="test") == []


def test_missing_memories_key_is_fatal_when_required() -> None:
    """OpenAICompatibleExtractionTransport's pre-existing policy: a real model
    response naming neither shape is untrustworthy, not empty."""
    with pytest.raises(ValueError, match="must contain a memories list"):
        decode_extraction_envelope({}, source="test", require_memories_key=True)


def test_mixing_both_shapes_is_rejected() -> None:
    payload = {"entities": [], "relations": [], "memories": []}
    with pytest.raises(ValueError, match="mixed"):
        decode_extraction_envelope(payload, source="test")


@pytest.mark.parametrize(
    "payload",
    [
        {"entities": "not-a-list", "relations": []},
        {"entities": [], "relations": "not-a-list"},
        {"entities": ["not-an-object"], "relations": []},
        {"entities": [], "relations": ["not-an-object"]},
    ],
)
def test_malformed_cookbook_entries_are_rejected(payload: dict) -> None:
    with pytest.raises(ValueError):
        decode_extraction_envelope(payload, source="test")


# ---------------------------------------------------------------------------
# 2. _expand_cookbook_envelope -- the roster-resolution unit, pure function
# ---------------------------------------------------------------------------


def test_expand_resolves_a_relation_against_its_own_roster() -> None:
    envelope = CookbookEnvelope(
        entities=(
            {"name": "User 42", "type": "Entity", "description": "a support customer"},
            {"name": "dark mode", "type": "Entity", "description": "a UI theme"},
        ),
        relations=(
            {
                "source": "User 42",
                "predicate": "prefers",
                "target": "dark mode",
                "relationship_type": "PREFERS",
                "confidence": 0.9,
            },
        ),
    )
    resolved, rejections = InstructionalExtractor._expand_cookbook_envelope(envelope)
    assert rejections == []
    assert len(resolved) == 1
    flat = resolved[0]
    assert flat["subject"] == "User 42"
    assert flat["object"] == "dark mode"
    assert flat["subject_label"] == "Entity"
    assert flat["object_label"] == "Entity"
    assert flat["subject_properties"]["description"] == "a support customer"
    assert flat["object_properties"]["description"] == "a UI theme"
    assert "source" not in flat and "target" not in flat


def test_expand_merges_entity_properties_alongside_description() -> None:
    envelope = CookbookEnvelope(
        entities=(
            {
                "name": "integration",
                "type": "Entity",
                "description": "the external team's lower environment",
                "properties": {"availability_target": "99.5%", "rpo": "6h"},
            },
            {"name": "production", "type": "Entity", "description": "the live environment"},
        ),
        relations=(
            {
                "source": "integration",
                "predicate": "mirrors",
                "target": "production",
                "relationship_type": "RELATES_TO",
            },
        ),
    )
    resolved, rejections = InstructionalExtractor._expand_cookbook_envelope(envelope)
    assert rejections == []
    subject_properties = resolved[0]["subject_properties"]
    assert subject_properties["availability_target"] == "99.5%"
    assert subject_properties["rpo"] == "6h"
    assert subject_properties["description"] == "the external team's lower environment"


def test_expand_rejects_a_relation_naming_an_unextracted_entity() -> None:
    """The structural guarantee: 'GPT-4o, Claude, Gemini, and additional
    approved AI models' was never listed as its own entity, so a relation
    naming it as a target is rejected -- it can never mint that phrase into a
    node just by mentioning it in a relation."""
    envelope = CookbookEnvelope(
        entities=({"name": "Jedai Gateway", "type": "Entity", "description": "the AI gateway"},),
        relations=(
            {
                "source": "Jedai Gateway",
                "predicate": "routes to",
                "target": "GPT-4o, Claude, Gemini, and additional approved AI models",
                "relationship_type": "RELATES_TO",
            },
        ),
    )
    resolved, rejections = InstructionalExtractor._expand_cookbook_envelope(envelope)
    assert resolved == []
    assert len(rejections) == 1
    rejection = rejections[0]
    assert rejection.violation == CandidateViolation.RELATION_ENTITY_NOT_EXTRACTED
    assert rejection.field == "target"
    assert rejection.index == 0
    assert "GPT-4o, Claude, Gemini" in rejection.reason


def test_expand_indexes_rejections_by_position_in_relations() -> None:
    envelope = CookbookEnvelope(
        entities=({"name": "A", "type": "Entity"},),
        relations=(
            {"source": "A", "predicate": "requires", "target": "unknown-1", "relationship_type": "REQUIRES"},
            {"source": "A", "predicate": "requires", "target": "unknown-2", "relationship_type": "REQUIRES"},
        ),
    )
    _, rejections = InstructionalExtractor._expand_cookbook_envelope(envelope)
    assert [r.index for r in rejections] == [0, 1]


# ---------------------------------------------------------------------------
# 3. End to end: Memotron.add_episode + run_due_dreams via the cookbook
#    envelope, exercised through RuleBasedExtractionTransport (EpisodeType.JSON)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cookbook_relation_materializes_with_roster_derived_properties(tmp_path: Path) -> None:
    client = _client(tmp_path)
    scope = _scope("resolved")
    await client.add_episode(
        name="cookbook-batch",
        episode_body=_cookbook_body(
            entities=[
                {
                    "name": "Acme Parks",
                    "type": "Entity",
                    "description": "the customer account",
                    "properties": {"tier": "enterprise"},
                },
                {"name": "SOC2 report", "type": "Entity", "description": "an audit artifact"},
            ],
            relations=[
                {
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "SOC2 report",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.95,
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    assert _active_objects(client, scope) == ["SOC2 report"]
    assert _rejections(client, scope) == []
    graph = client.export_graph()
    acme_node = next(node for node in graph["nodes"] if node["properties"].get("name") == "Acme Parks")
    report_node = next(node for node in graph["nodes"] if node["properties"].get("name") == "SOC2 report")
    assert acme_node["properties"]["description"] == "the customer account"
    assert acme_node["properties"]["tier"] == "enterprise"
    assert report_node["properties"]["description"] == "an audit artifact"


@pytest.mark.asyncio
async def test_cookbook_relation_naming_unextracted_entity_is_rejected_with_receipt_and_sibling_survives(
    tmp_path: Path,
) -> None:
    client = _client(tmp_path)
    scope = _scope("mixed")
    await client.add_episode(
        name="cookbook-mixed-batch",
        episode_body=_cookbook_body(
            entities=[
                {"name": "Acme Parks", "type": "Entity", "description": "the customer account"},
                {"name": "SOC2 report", "type": "Entity", "description": "an audit artifact"},
            ],
            relations=[
                {
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "SOC2 report",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.95,
                },
                {
                    # "a signed indemnification form" was never extracted as its
                    # own entity -- this relation must be rejected, never
                    # promoted into a phrase-node.
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "a signed indemnification form",
                    "relationship_type": "REQUIRES",
                    "confidence": 0.9,
                },
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    await client.run_due_dreams()

    # The good sibling survives; the phrase-node relation does not exist at all.
    assert _active_objects(client, scope) == ["SOC2 report"]

    rejected = _rejections(client, scope)
    assert len(rejected) == 1
    payload = json.loads(rejected[0].event_payload)
    assert payload["violation"] == CandidateViolation.RELATION_ENTITY_NOT_EXTRACTED.value
    assert payload["field"] == "target"
    assert rejected[0].decision_result == "rejected"
    assert rejected[0].candidate_digest is not None


@pytest.mark.asyncio
async def test_cookbook_episode_with_only_unresolvable_relations_completes_cleanly(
    tmp_path: Path,
) -> None:
    """Mirrors the existing 'all candidates rejected' invariant for the legacy
    shape: the run checkpoints instead of raising, and every drop is receipted."""
    client = _client(tmp_path)
    scope = _scope("all-rejected")
    await client.add_episode(
        name="cookbook-all-rejected",
        episode_body=_cookbook_body(
            entities=[{"name": "Acme Parks", "type": "Entity", "description": "the account"}],
            relations=[
                {
                    "source": "Acme Parks",
                    "predicate": "requires",
                    "target": "an entity nobody rostered",
                    "relationship_type": "REQUIRES",
                }
            ],
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )

    result = await client.run_due_dreams()

    assert result.processed_episodes == 1
    assert _active_objects(client, scope) == []
    assert len(_rejections(client, scope)) == 1
