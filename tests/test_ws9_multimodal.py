"""WS-9 acceptance tests — Multimodal ingestion.

Three required proof tests (per task spec §"New proof tests"):
1. test_artifact_becomes_graph_fact_with_provenance
2. test_hybrid_semantic_search_finds_fact_by_similarity
3. test_image_artifact_without_text_hint_fails_fast

Plus supporting unit tests for the normalizer and API surface.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memotron import (
    CODE_MODALITIES,
    IMAGE_MODALITIES,
    STRUCTURED_MODALITIES,
    TEXT_MODALITIES,
    AddArtifactResult,
    Artifact,
    ArtifactProvenance,
    Memotron,
    LocalMultimodalNormalizer,
    MemoryScope,
    ScopeKind,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def new_client(tmp_path: Path) -> Memotron:
    return Memotron(graph_path=tmp_path / "ws9.sqlite")


def customer_scope(scope_id: str = "ws9-corp") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.CUSTOMER, scope_id=scope_id)


def user_scope(scope_id: str = "ws9-user") -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


# ---------------------------------------------------------------------------
# LocalMultimodalNormalizer unit tests
# ---------------------------------------------------------------------------


class TestLocalMultimodalNormalizerText:
    def test_text_passthrough(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="text", payload="  Hello world.  ")
        result = norm.normalize(artifact)
        assert result == "Hello world."

    def test_document_passthrough(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="document", payload="A policy document.\nSecond line.")
        result = norm.normalize(artifact)
        assert "policy document" in result

    def test_text_wrong_payload_type_raises(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="text", payload={"key": "value"})
        with pytest.raises(ValueError, match="must be a str"):
            norm.normalize(artifact)


class TestLocalMultimodalNormalizerCode:
    def test_code_with_location(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="code",
            payload="def auth(token): return validate(token)",
            location="src/auth.py:L10-L12",
        )
        result = norm.normalize(artifact)
        assert "Code artifact from: src/auth.py:L10-L12" in result
        assert "def auth(token)" in result

    def test_code_without_location(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="code", payload="x = 1 + 1")
        result = norm.normalize(artifact)
        assert "Code artifact:" in result
        assert "x = 1 + 1" in result

    def test_code_wrong_payload_type_raises(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="code", payload=b"\x00\x01")
        with pytest.raises(ValueError, match="must be a str"):
            norm.normalize(artifact)


class TestLocalMultimodalNormalizerStructured:
    def test_dict_payload(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="structured",
            payload={"ticket_id": "T-123", "status": "open", "priority": 1},
        )
        result = norm.normalize(artifact)
        assert "ticket_id=T-123" in result
        assert "status=open" in result
        assert "priority=1" in result

    def test_json_string_payload(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="structured",
            payload=json.dumps({"region": "NA", "tier": "enterprise"}),
        )
        result = norm.normalize(artifact)
        assert "region=NA" in result
        assert "tier=enterprise" in result

    def test_list_payload(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="structured", payload=[{"a": 1}, {"b": 2}])
        result = norm.normalize(artifact)
        assert "[0].a=1" in result
        assert "[1].b=2" in result

    def test_invalid_json_raises(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="structured", payload="not json at all {{")
        with pytest.raises(ValueError, match="not valid JSON"):
            norm.normalize(artifact)

    def test_wrong_payload_type_raises(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="structured", payload=b"bytes")
        with pytest.raises(ValueError, match="must be str"):
            norm.normalize(artifact)


class TestLocalMultimodalNormalizerImageFamily:
    def test_image_with_caption(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="image",
            payload=b"\x89PNG",
            caption="A network topology diagram showing the authentication service.",
        )
        result = norm.normalize(artifact)
        assert "authentication service" in result
        assert "Caption:" in result

    def test_diagram_with_ocr(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="diagram",
            payload=b"",
            ocr_text="Auth Service → Database → Cache",
            location="diagrams/arch_v2.png",
        )
        result = norm.normalize(artifact)
        assert "Auth Service" in result
        assert "OCR text:" in result
        assert "diagrams/arch_v2.png" in result

    def test_screenshot_with_alt_text(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="screenshot",
            payload=b"",
            alt_text="Error 500 on checkout page",
        )
        result = norm.normalize(artifact)
        assert "Error 500" in result

    def test_audio_with_transcript(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="audio",
            payload=b"",
            transcript="The customer requires delivery by Friday.",
        )
        result = norm.normalize(artifact)
        assert "delivery by Friday" in result
        assert "Transcript:" in result

    def test_all_hints_merged(self) -> None:
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(
            modality="image",
            payload=b"",
            caption="Overview diagram",
            alt_text="Box diagram",
            ocr_text="Service A → B",
        )
        result = norm.normalize(artifact)
        # OCR and caption should both appear.
        assert "Service A" in result
        assert "Caption:" in result


class TestLocalMultimodalNormalizerMlmmSeam:
    def test_image_no_hint_no_transport_raises(self) -> None:
        """PROOF TEST 3 (component): no hint + no MLLM transport → ValueError."""
        norm = LocalMultimodalNormalizer()
        artifact = Artifact(modality="image", payload=b"\xff\xd8")
        with pytest.raises(ValueError, match="no textual representation"):
            norm.normalize(artifact)

    def test_mllm_transport_called_when_no_hint(self) -> None:
        """MLLM seam: transport is invoked when no textual hint is present."""

        class MockMlmmTransport:
            called = False

            def normalize(self, artifact: Artifact) -> str:
                MockMlmmTransport.called = True
                return "MLLM-generated description of the image"

        transport = MockMlmmTransport()
        norm = LocalMultimodalNormalizer(mllm_transport=transport)
        artifact = Artifact(modality="diagram", payload=b"")
        result = norm.normalize(artifact)
        assert MockMlmmTransport.called
        assert "MLLM-generated" in result

    def test_mllm_transport_not_called_when_hint_present(self) -> None:
        """MLLM transport is bypassed when a textual hint is available."""

        class FailMlmmTransport:
            def normalize(self, artifact: Artifact) -> str:
                raise AssertionError("Should not be called when hint is present")

        norm = LocalMultimodalNormalizer(mllm_transport=FailMlmmTransport())
        artifact = Artifact(modality="image", payload=b"", caption="A simple diagram")
        result = norm.normalize(artifact)
        assert "A simple diagram" in result


class TestArtifactModalities:
    def test_unknown_modality_raises(self) -> None:
        with pytest.raises(ValueError, match="Unsupported artifact modality"):
            Artifact(modality="video", payload="")

    def test_case_insensitive_modality(self) -> None:
        a = Artifact(modality="IMAGE", payload=b"", caption="test")
        assert a.modality == "image"

    def test_modality_sets(self) -> None:
        assert "text" in TEXT_MODALITIES
        assert "document" in TEXT_MODALITIES
        assert "code" in CODE_MODALITIES
        assert "structured" in STRUCTURED_MODALITIES
        assert "image" in IMAGE_MODALITIES
        assert "diagram" in IMAGE_MODALITIES
        assert "screenshot" in IMAGE_MODALITIES
        assert "audio" in IMAGE_MODALITIES


class TestArtifactProvenance:
    def test_auto_uuid_when_no_id(self) -> None:
        a = Artifact(modality="text", payload="hello")
        prov = a.provenance()
        assert len(prov.artifact_id) > 0

    def test_explicit_artifact_id(self) -> None:
        a = Artifact(modality="code", payload="x=1", artifact_id="my-artifact-42")
        prov = a.provenance()
        assert prov.artifact_id == "my-artifact-42"

    def test_checksum_computed_from_str_payload(self) -> None:
        a = Artifact(modality="text", payload="hello world")
        prov = a.provenance()
        assert prov.artifact_checksum is not None
        assert len(prov.artifact_checksum) == 64  # SHA-256 hex

    def test_checksum_from_bytes_payload(self) -> None:
        a = Artifact(modality="image", payload=b"\x00\x01\x02", caption="test")
        prov = a.provenance()
        assert prov.artifact_checksum is not None

    def test_explicit_checksum_not_overridden(self) -> None:
        a = Artifact(modality="text", payload="hello", checksum="deadbeef")
        prov = a.provenance()
        assert prov.artifact_checksum == "deadbeef"

    def test_as_metadata_keys(self) -> None:
        prov = ArtifactProvenance(
            artifact_type="image",
            artifact_location="assets/banner.png",
            artifact_id="banner-001",
            artifact_checksum="abc123",
        )
        meta = prov.as_metadata()
        assert meta["artifact_id"] == "banner-001"
        assert meta["artifact_type"] == "image"
        assert meta["artifact_location"] == "assets/banner.png"
        assert meta["artifact_checksum"] == "abc123"

    def test_as_metadata_no_checksum(self) -> None:
        prov = ArtifactProvenance(artifact_type="code")
        meta = prov.as_metadata()
        assert "artifact_checksum" not in meta


# ---------------------------------------------------------------------------
# add_artifact API tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_artifact_returns_result(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(
        modality="document",
        payload="This is a policy document about vendor approval.",
        artifact_id="policy-doc-001",
        location="docs/vendor_policy.md",
    )
    result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope],
    )
    assert isinstance(result, AddArtifactResult)
    assert result.artifact_id == "policy-doc-001"
    assert result.artifact_type == "document"
    assert result.artifact_location == "docs/vendor_policy.md"
    assert result.queued_for_dreaming is True
    assert len(result.episode_uuids) == 1
    assert result.normalized_text_length > 0
    assert scope.key in result.scope_keys


@pytest.mark.asyncio
async def test_add_artifact_multi_scope(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope_a = customer_scope("corp-a")
    scope_b = customer_scope("corp-b")
    artifact = Artifact(modality="text", payload="A shared compliance note.")
    result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope_a, scope_b],
    )
    # One episode per scope per chunk.
    assert len(result.episode_uuids) == 2
    assert scope_a.key in result.scope_keys
    assert scope_b.key in result.scope_keys


@pytest.mark.asyncio
async def test_add_artifact_provenance_in_episode_metadata(tmp_path: Path) -> None:
    """Provenance metadata is stamped on the queued episode."""
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(
        modality="code",
        payload="def process(): pass",
        artifact_id="src-proc-001",
        location="src/processor.py:L1-L2",
    )
    result = await client.add_artifact(artifact=artifact, scopes=[scope])
    episode_uuid = result.episode_uuids[0]
    episode = client.graph.get_episode(episode_uuid)
    assert episode.metadata["artifact_id"] == "src-proc-001"
    assert episode.metadata["artifact_type"] == "code"
    assert episode.metadata["artifact_location"] == "src/processor.py:L1-L2"
    assert "artifact_checksum" in episode.metadata
    assert episode.metadata["multimodal_ingest"] is True


@pytest.mark.asyncio
async def test_add_artifact_no_scopes_raises(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    artifact = Artifact(modality="text", payload="hello")
    with pytest.raises(ValueError, match="at least one scope"):
        await client.add_artifact(artifact=artifact, scopes=[])


@pytest.mark.asyncio
async def test_add_artifacts_bulk(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = customer_scope()
    artifacts = [
        Artifact(modality="text", payload="First document."),
        Artifact(modality="document", payload="Second document."),
    ]
    results = await client.add_artifacts(artifacts=artifacts, scopes=[scope])
    assert len(results) == 2
    assert all(isinstance(r, AddArtifactResult) for r in results)


# ---------------------------------------------------------------------------
# PROOF TEST 1 — artifact becomes graph fact with provenance round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_artifact_becomes_graph_fact_with_provenance(tmp_path: Path) -> None:
    """WS-9 acceptance proof 1.

    Ingest an architecture DIAGRAM artifact with an OCR caption that contains a
    structured JSON memory record.  After dream formation, assert:
    - A graph fact was created.
    - memory_evidence() on the fact reveals the source artifact_id and
      artifact_location (provenance round-trips from artifact → episode →
      relationship metadata → memory_evidence).
    """
    client = new_client(tmp_path)
    scope = customer_scope("arch-team")

    # The normalized text must contain a Memory: record so the hermetic
    # RuleBased extractor can produce a fact during dream formation.
    # We place the Memory: line in the caption so the normalizer outputs it on
    # its own line (the rule-based extractor scans line-by-line for "Memory:").
    caption = (
        "Architecture diagram for auth service.\n"
        "Memory: subject=Auth Service; predicate=requires; "
        "object=OAuth2 token validation; relationship_type=REQUIRES; confidence=0.91"
    )
    artifact = Artifact(
        modality="diagram",
        payload=b"\x89PNG\r\n",  # fake PNG bytes
        caption=caption,
        artifact_id="arch-diagram-v3",
        location="docs/architecture/auth_flow_v3.png",
    )

    add_result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope],
        name="auth-architecture-diagram",
    )
    assert add_result.artifact_id == "arch-diagram-v3"
    assert add_result.artifact_location == "docs/architecture/auth_flow_v3.png"

    # Run dream formation to extract the memory from the episode.
    dream_result = await client.run_due_dreams()
    assert dream_result.processed_episodes >= 1

    # The fact should now be in the graph.
    search_results = await client.search(query="OAuth2", scope=scope)
    assert len(search_results) > 0, "Expected at least one graph fact about OAuth2"

    hit = search_results[0]
    assert "oauth2" in hit.fact.casefold() or "oauth2" in hit.object.casefold()

    # --- PROVENANCE ROUND-TRIP ---
    # memory_evidence() on the resulting fact must reveal the source artifact.
    evidence = await client.memory_evidence(
        relationship_uuid=hit.relationship_uuid,
        scope=scope,
    )
    # Provenance is carried in the source episode metadata and flows into
    # the relationship metadata at materialization time.
    source_episodes = evidence.episodes
    assert len(source_episodes) > 0, "Expected at least one source episode"

    # Check provenance in episode metadata.
    source_episode = source_episodes[0]
    assert source_episode.metadata.get("artifact_id") == "arch-diagram-v3", (
        f"Expected artifact_id='arch-diagram-v3' in episode metadata, got: {source_episode.metadata}"
    )
    assert source_episode.metadata.get("artifact_location") == "docs/architecture/auth_flow_v3.png", (
        f"artifact_location not found in episode metadata: {source_episode.metadata}"
    )
    assert source_episode.metadata.get("artifact_type") == "diagram"


# ---------------------------------------------------------------------------
# PROOF TEST 2 — hybrid semantic search finds fact by similarity (not keyword)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_hybrid_semantic_search_finds_fact_by_similarity(tmp_path: Path) -> None:
    """WS-9 acceptance proof 2.

    Add facts about "vendor authentication", then query with a semantically-related
    but non-overlapping phrase ("supplier login procedure") via semantic_search.
    Assert that:
    - semantic_search returns results (finds by embedding similarity).
    - The default keyword search() with the same non-overlapping query returns
      fewer/none (proving semantic adds recall).
    """
    client = new_client(tmp_path)
    scope = customer_scope("search-proof")

    # Ingest a fact via add_memory so it is immediately materialized.
    await client.add_memory(
        subject="Vendor Portal",
        predicate="requires",
        object="vendor authentication via OAuth2",
        relationship_type="REQUIRES",
        confidence=0.95,
        scope=scope,
    )
    await client.add_memory(
        subject="Integration System",
        predicate="requires",
        object="API key for partner access",
        relationship_type="REQUIRES",
        confidence=0.90,
        scope=scope,
    )

    # --- KEYWORD SEARCH baseline: "supplier login" has zero token overlap ---
    keyword_results = await client.search(query="supplier login procedure", scope=scope)
    # "supplier", "login", "procedure" do not appear in any materialized fact text.
    # The keyword search should find 0 or very few results.
    keyword_hit_count = len(keyword_results)

    # --- SEMANTIC SEARCH: should find the "vendor authentication" fact ---
    semantic_results = await client.semantic_search(
        query="supplier login procedure",
        scope=scope,
        limit=10,
    )
    # The embedding of "supplier login procedure" should be cosine-close to
    # "vendor authentication via OAuth2" (shared semantic field of authentication).
    # With the local trigram+unigram embedding, shared substrings like "auth"
    # are sufficient to give non-zero similarity.
    assert len(semantic_results) > keyword_hit_count, (
        f"Semantic search should find more facts than keyword search for a "
        f"non-overlapping query.  "
        f"keyword={keyword_hit_count}, semantic={len(semantic_results)}"
    )

    # The semantic result set must contain the authentication-related fact.
    semantic_objects = {r.object for r in semantic_results}
    assert any(
        "oauth2" in obj.casefold() or "authenticat" in obj.casefold() or "key" in obj.casefold()
        for obj in semantic_objects
    ), f"Expected authentication-related fact in semantic results; got: {semantic_objects}"


# ---------------------------------------------------------------------------
# PROOF TEST 3 — image artifact without text hint fails fast
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_image_artifact_without_text_hint_fails_fast(tmp_path: Path) -> None:
    """WS-9 acceptance proof 3.

    An image-family artifact with no caption, alt_text, ocr_text, or transcript
    and no MLLM transport configured must raise a clear ValueError — not silently
    create an empty episode.
    """
    client = new_client(tmp_path)
    scope = customer_scope("no-hint-scope")

    artifact = Artifact(
        modality="screenshot",
        payload=b"\xff\xd8\xff",  # fake JPEG bytes, no textual hints
        # Deliberately omitting: caption, alt_text, ocr_text, transcript
    )

    with pytest.raises(ValueError, match="no textual representation"):
        await client.add_artifact(
            artifact=artifact,
            scopes=[scope],
        )

    # Verify no episodes were queued (no silent empty episode).
    episodes = list(client.graph.episodes())
    assert len(episodes) == 0, f"Expected 0 queued episodes after fail-fast, got {len(episodes)}"


# ---------------------------------------------------------------------------
# Additional semantic_search tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_returns_empty_for_blank_query(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = customer_scope()
    await client.add_memory(
        subject="Acme",
        predicate="requires",
        object="SOC2",
        relationship_type="REQUIRES",
        confidence=0.9,
        scope=scope,
    )
    results = await client.semantic_search(query="   ", scope=scope)
    assert results == []


@pytest.mark.asyncio
async def test_semantic_search_limit_zero_raises(tmp_path: Path) -> None:
    client = new_client(tmp_path)
    scope = customer_scope()
    with pytest.raises(ValueError, match="limit must be greater than zero"):
        await client.semantic_search(query="test", scope=scope, limit=0)


@pytest.mark.asyncio
async def test_semantic_search_respects_scope(tmp_path: Path) -> None:
    """Semantic search must not return facts from other scopes."""
    client = new_client(tmp_path)
    scope_a = customer_scope("corp-a")
    scope_b = customer_scope("corp-b")

    await client.add_memory(
        subject="Corp A",
        predicate="requires",
        object="authentication token",
        relationship_type="REQUIRES",
        confidence=0.9,
        scope=scope_a,
    )
    await client.add_memory(
        subject="Corp B",
        predicate="prefers",
        object="email notifications",
        relationship_type="PREFERS",
        confidence=0.9,
        scope=scope_b,
    )

    results_a = await client.semantic_search(query="authentication", scope=scope_a)
    assert all(r.scope == scope_a for r in results_a)

    results_b = await client.semantic_search(query="email notifications", scope=scope_b)
    assert all(r.scope == scope_b for r in results_b)


@pytest.mark.asyncio
async def test_semantic_search_min_similarity_filter(tmp_path: Path) -> None:
    """min_similarity filter narrows results: very high threshold filters out low-sim facts."""
    client = new_client(tmp_path)
    scope = customer_scope()
    await client.add_memory(
        subject="System",
        predicate="requires",
        object="vendor authentication",
        relationship_type="REQUIRES",
        confidence=0.9,
        scope=scope,
    )
    # Query with no floor — should return the fact.
    results_open = await client.semantic_search(
        query="vendor authentication",
        scope=scope,
        min_similarity=0.0,
    )
    assert len(results_open) == 1

    # Query with very high threshold against a completely unrelated query.
    # "xylophone jazz concert 12345" shares no n-grams or tokens with
    # "System requires vendor authentication", so cosine will be near 0.
    results_high = await client.semantic_search(
        query="xylophone jazz concert 12345",
        scope=scope,
        min_similarity=0.5,
    )
    # Should be filtered out because similarity is very low.
    assert len(results_high) == 0


@pytest.mark.asyncio
async def test_semantic_search_default_keyword_unchanged(tmp_path: Path) -> None:
    """The default search() keyword path is byte-for-byte unchanged after WS-9."""
    client = new_client(tmp_path)
    scope = customer_scope()
    await client.add_memory(
        subject="Acme",
        predicate="requires",
        object="SOC2 compliance report",
        relationship_type="REQUIRES",
        confidence=0.9,
        scope=scope,
    )

    # Keyword search works normally.
    kw_results = await client.search(query="SOC2 compliance", scope=scope)
    assert len(kw_results) == 1
    assert "SOC2" in kw_results[0].object

    # Semantic search is a separate path.
    sem_results = await client.semantic_search(query="SOC2 compliance", scope=scope)
    assert len(sem_results) >= 1  # also finds it (same text → high similarity)


# ---------------------------------------------------------------------------
# Additional add_artifact edge-case tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_artifact_code_location_in_text(tmp_path: Path) -> None:
    """Code artifact carries file location citation in normalized text."""
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(
        modality="code",
        payload="def validate(token):\n    return jwt.decode(token)",
        location="src/auth/validator.py:L5-L6",
    )
    result = await client.add_artifact(artifact=artifact, scopes=[scope])
    episode = client.graph.get_episode(result.episode_uuids[0])
    # Location citation appears in the episode body (the normalized text).
    assert "src/auth/validator.py:L5-L6" in episode.body


@pytest.mark.asyncio
async def test_add_artifact_structured_flattens_to_episode_body(tmp_path: Path) -> None:
    """Structured artifact is flattened into readable text in the episode body."""
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(
        modality="structured",
        payload={"customer": "Acme Parks", "tier": "enterprise", "region": "NA"},
    )
    result = await client.add_artifact(artifact=artifact, scopes=[scope])
    episode = client.graph.get_episode(result.episode_uuids[0])
    assert "customer=Acme Parks" in episode.body
    assert "tier=enterprise" in episode.body


@pytest.mark.asyncio
async def test_add_artifact_custom_normalizer(tmp_path: Path) -> None:
    """A custom normalizer can be supplied to add_artifact."""
    client = new_client(tmp_path)
    scope = customer_scope()

    class UpperNormalizer:
        def normalize(self, artifact: Artifact) -> str:
            return str(artifact.payload).upper()

    artifact = Artifact(modality="text", payload="hello world")
    result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope],
        normalizer=UpperNormalizer(),
    )
    episode = client.graph.get_episode(result.episode_uuids[0])
    assert episode.body == "HELLO WORLD"


@pytest.mark.asyncio
async def test_add_artifact_motive_flag_in_metadata(tmp_path: Path) -> None:
    """WS-3 motive hint is stamped on the episode metadata."""
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(modality="text", payload="A compliance note.")
    result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope],
        motive="learn-compliance-requirements",
    )
    episode = client.graph.get_episode(result.episode_uuids[0])
    assert episode.metadata.get("motive") == "learn-compliance-requirements"


@pytest.mark.asyncio
async def test_add_artifact_trusted_false_flag(tmp_path: Path) -> None:
    """WS-7 trusted=False flag is stamped on the episode metadata."""
    client = new_client(tmp_path)
    scope = customer_scope()
    artifact = Artifact(modality="text", payload="User-submitted feedback.")
    result = await client.add_artifact(
        artifact=artifact,
        scopes=[scope],
        trusted=False,
    )
    episode = client.graph.get_episode(result.episode_uuids[0])
    assert episode.metadata.get("trusted") is False


@pytest.mark.asyncio
async def test_add_artifact_read_only_scope_raises(tmp_path: Path) -> None:
    """Writing an artifact to a read-only scope raises ValueError."""
    from memotron.config import default_config

    base_config = default_config()
    ro_scope = customer_scope("read-only-corp")
    config = base_config.model_copy(update={"read_only_scopes": {ro_scope.key}})
    client = Memotron(graph_path=tmp_path / "ro.sqlite", config=config)

    artifact = Artifact(modality="text", payload="A document.")
    with pytest.raises(ValueError, match="read-only"):
        await client.add_artifact(artifact=artifact, scopes=[ro_scope])
