"""A bare top-level array is named by its content, not by assumption.

Regression cover for a measured defect. ``parse_first_json_object`` wrapped
every bare array as ``{"memories": [...]}``.  Once the prompt asked for an
``entities``/``relations`` envelope, a model that answered one episode with a
bare array of ENTITIES had each entity validated as a relation and rejected
``model_validation_failed`` with five missing fields.  Measured on the
jedai-gateway ingest: 41 rejections across 3 of 119 episodes (18, 13, 10),
while the other 116 returned the envelope correctly.
"""

from __future__ import annotations

import pytest

from memotron.extraction import CookbookEnvelope, decode_extraction_envelope, parse_first_json_object


def test_bare_array_of_entities_is_read_as_a_roster_not_as_memories() -> None:
    """The exact payload shape that produced 41 spurious rejections."""
    payload = parse_first_json_object(
        '[{"name": "Jedai Gateway", "type": "Component", "description": "The gateway."},'
        ' {"name": "virtual key", "type": "Credential", "description": "An API credential."}]',
        source="test",
    )

    assert "memories" not in payload, (
        "an array of entity objects must not be presented as relations -- that is "
        "what made every entity fail ExtractedMemory validation"
    )
    assert [entity["name"] for entity in payload["entities"]] == ["Jedai Gateway", "virtual key"]
    assert payload["relations"] == []

    envelope = decode_extraction_envelope(payload, source="test")
    assert isinstance(envelope, CookbookEnvelope)
    assert len(envelope.entities) == 2
    assert envelope.relations == ()


def test_bare_array_of_relations_still_reads_as_memories() -> None:
    """The historical reading is preserved for arrays that carry endpoints."""
    payload = parse_first_json_object(
        '[{"subject": "Jedai Gateway", "predicate": "exposes", "object": "the v1 API",'
        ' "relationship_type": "RELATES_TO", "confidence": 0.9}]',
        source="test",
    )

    assert "entities" not in payload
    assert payload["memories"][0]["predicate"] == "exposes"


@pytest.mark.parametrize(
    "raw",
    [
        "[]",  # empty: nothing to classify, keep the historical reading
        '["Jedai Gateway", "virtual key"]',  # bare strings, not objects
        '[{"name": "Jedai Gateway", "source": "a", "target": "b"}]',  # named but relational
    ],
)
def test_ambiguous_arrays_keep_the_historical_memories_reading(raw: str) -> None:
    """Only an unambiguous roster is renamed; everything else is unchanged."""
    assert "memories" in parse_first_json_object(raw, source="test")


def test_an_object_response_is_never_reinterpreted() -> None:
    """The common case -- a proper envelope -- passes through untouched."""
    payload = parse_first_json_object(
        '{"entities": [{"name": "Jedai Gateway", "type": "Component"}],'
        ' "relations": [{"source": "Jedai Gateway", "predicate": "exposes",'
        ' "target": "the v1 API", "relationship_type": "RELATES_TO", "confidence": 0.9}]}',
        source="test",
    )

    envelope = decode_extraction_envelope(payload, source="test")
    assert isinstance(envelope, CookbookEnvelope)
    assert len(envelope.entities) == 1
    assert len(envelope.relations) == 1
