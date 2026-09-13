"""WS-17 T18: production embedding transport + vector-space integrity.

Pins the T18 contracts:

- Every ``EmbeddingTransport`` carries a required ``identifier`` naming its
  vector space; ``LocalEmbeddingTransport.identifier`` IS
  ``retrieval.EMBEDDING_IDENTIFIER`` (single source of truth).
- Materialization stamps the active identifier next to every stored vector.
- Reads that compare a stored vector against a freshly embedded query or
  candidate use the stored vector ONLY when its stamp matches the active
  transport; a mismatched or unstamped vector degrades to the existing
  re-embed-on-read fallback (search) or exact-match-only (dedup) — cosine is
  never computed across two vector spaces.
- The ``RetrievalContract`` digest changes when the identifier changes.
- ``OpenAICompatibleEmbeddingTransport`` builds the documented request shape and
  fails fast on a missing key before any network call.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from datetime import UTC, datetime, timedelta
from http.client import RemoteDisconnected
from typing import Self
from urllib.error import HTTPError

import pytest

from memotron import (
    Memotron,
    LocalEmbeddingTransport,
    MemoryScope,
    OpenAICompatibleEmbeddingTransport,
    ScopeKind,
)
from memotron import gateway as gateway_module
from memotron.embedding import (
    LOCAL_EMBEDDING_IDENTIFIER,
    cosine_similarity,
    stored_vector_in_active_space,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    GatewayRequestError,
    GatewayRetryPolicy,
    gateway_request_with_retry,
)
from memotron.graph import (
    CONTENT_PLANE_SUBJECT_KEY,
    LLM_CREDENTIAL_SUBJECT_KEY,
    PropertyGraphStore,
)
from memotron.models import EpisodeType, RelationshipStatus
from memotron.receipts import ReceiptDecisionType
from memotron.retrieval import EMBEDDING_IDENTIFIER


class StubEmbeddingTransport:
    """Deterministic 8-dim topic-bucket transport in a DIFFERENT space.

    Texts sharing a topic keyword land on the same axis (cosine 1.0); texts
    with no shared topic are orthogonal-ish.  Deterministic by construction.
    """

    identifier = "stub-8@v1"

    _TOPICS = ("timeout", "gateway", "tea", "coffee", "approval", "seattle")

    def embed(self, text: str) -> list[float]:
        normalized = text.casefold()
        vector = [0.0] * 8
        for index, topic in enumerate(self._TOPICS):
            if topic in normalized:
                vector[index] = 1.0
        if not any(vector):
            digest = hashlib.sha256(normalized.encode("utf-8")).digest()
            vector[6 + (digest[0] % 2)] = 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector]


class ConstantSpaceTransport:
    """256-dim transport whose vectors COLLIDE with local-space vectors.

    Every input maps to the local embedding of one constant anchor string, so a
    naive cross-space cosine against local-stamped vectors is well-defined
    (same dimensionality) and can reach 1.0 — the adversarial case the space
    guard exists to refuse.
    """

    identifier = "const-256@v1"

    def __init__(self, anchor: str) -> None:
        self._vector = LocalEmbeddingTransport().embed(anchor)

    def embed(self, text: str) -> list[float]:
        return list(self._vector)


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def test_local_transport_identifier_is_the_pinned_retrieval_identifier() -> None:
    assert LocalEmbeddingTransport().identifier == "local-trigram-256@v1"
    assert LocalEmbeddingTransport().identifier == EMBEDDING_IDENTIFIER
    assert EMBEDDING_IDENTIFIER is LOCAL_EMBEDDING_IDENTIFIER


async def test_new_facts_stamp_the_active_identifier(tmp_path) -> None:
    scope = _scope("stamping")
    local_client = Memotron(graph_path=tmp_path / "g.sqlite")
    local_row = await local_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="green tea",
        relationship_type="PREFERS",
        scope=scope,
    )
    stored = local_client.graph.get_relationship(local_row.relationship_uuid)
    assert stored.properties["embedding_identifier"] == "local-trigram-256@v1"
    local_client.graph.close()

    stub_client = Memotron(graph_path=tmp_path / "g.sqlite", embedding_transport=StubEmbeddingTransport())
    stub_row = await stub_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="black coffee",
        relationship_type="PREFERS",
        scope=scope,
    )
    stored_stub = stub_client.graph.get_relationship(stub_row.relationship_uuid)
    assert stored_stub.properties["embedding_identifier"] == "stub-8@v1"
    assert len(stored_stub.properties["embedding"]) == 8


async def test_search_under_stub_finds_local_rows_via_reembed_fallback(tmp_path) -> None:
    """A local-stamped stored vector never meets a stub query vector.

    Without the guard, semantic search would attempt cosine(8-dim query,
    256-dim stored), hit the dimension-mismatch ValueError, and silently DROP
    the row.  With the guard the stored vector is treated as absent and the
    revealed fact text is re-embedded in the ACTIVE (stub) space, so the row is
    still found — at stub-space similarity 1.0 for a query equal to its text.
    """
    scope = _scope("fallback")
    local_client = Memotron(graph_path=tmp_path / "g.sqlite")
    row = await local_client.add_memory(
        subject="Payment gateway",
        predicate="suffers",
        object="timeout after 30 seconds",
        relationship_type="PREFERS",
        scope=scope,
    )
    local_client.graph.close()

    stub = StubEmbeddingTransport()
    stub_client = Memotron(graph_path=tmp_path / "g.sqlite", embedding_transport=stub)
    stored = stub_client.graph.get_relationship(row.relationship_uuid)
    assert stored.properties["embedding_identifier"] == "local-trigram-256@v1"
    assert not stored_vector_in_active_space(stored.properties, active_identifier=stub.identifier)

    fact_text = "Payment gateway suffers timeout after 30 seconds"
    semantic = await stub_client.semantic_search(query=fact_text, scope=scope, min_similarity=0.99)
    assert [item.relationship_uuid for item in semantic] == [row.relationship_uuid]

    keyword = await stub_client.search(query="payment gateway timeout", scope=scope)
    assert row.relationship_uuid in {item.relationship_uuid for item in keyword}


async def test_dedup_refuses_cross_space_cosine_and_falls_to_exact_match(tmp_path) -> None:
    """Same-dimensionality different-space vectors are the dangerous case.

    The incumbent's stored object vector (const-256 space) is byte-comparable
    with local vectors and engineered to reach cosine 1.0 against the local
    embedding of a semantically UNRELATED object.  Without the guard, semantic
    dedup would wrongly reinforce the incumbent; with it, dedup degrades to
    exact-match only and the unrelated fact creates its own row.
    """
    scope = _scope("dedup-guard")
    anchor_object = "quarterly compliance report"
    const_client = Memotron(
        graph_path=tmp_path / "g.sqlite",
        embedding_transport=ConstantSpaceTransport(anchor_object),
    )
    incumbent = await const_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="window seats on long flights",
        relationship_type="PREFERS",
        scope=scope,
    )
    stored = const_client.graph.get_relationship(incumbent.relationship_uuid)
    assert stored.properties["embedding_identifier"] == "const-256@v1"
    # The engineered collision: local embedding of the anchor object has cosine
    # 1.0 against the incumbent's stored object vector.
    assert cosine_similarity(
        LocalEmbeddingTransport().embed(anchor_object),
        stored.properties["object_embedding"],
    ) == pytest.approx(1.0)
    const_client.graph.close()

    local_client = Memotron(graph_path=tmp_path / "g.sqlite")
    unrelated = await local_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object=anchor_object,
        relationship_type="PREFERS",
        scope=scope,
    )
    assert unrelated.relationship_uuid != incumbent.relationship_uuid
    assert unrelated.reinforced_relationships == 0
    assert unrelated.created_relationships == 1
    survivors = {
        relationship.uuid
        for relationship in local_client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
        and relationship.type == "PREFERS"
    }
    assert survivors == {incumbent.relationship_uuid, unrelated.relationship_uuid}

    # Exact-object restatements still reinforce across the space change.
    exact = await local_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="window seats on long flights",
        relationship_type="PREFERS",
        scope=scope,
    )
    assert exact.relationship_uuid == incumbent.relationship_uuid
    assert exact.reinforced_relationships == 1


async def test_retrieval_contract_digest_changes_with_identifier(tmp_path) -> None:
    scope = _scope("contract")
    local_client = Memotron(graph_path=tmp_path / "g.sqlite")
    await local_client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="green tea",
        relationship_type="PREFERS",
        scope=scope,
    )
    local_results = await local_client.search(query="green tea", scope=scope)
    assert local_results
    local_digest = local_results[0].retrieval_policy_digest
    local_client.graph.close()

    stub_client = Memotron(graph_path=tmp_path / "g.sqlite", embedding_transport=StubEmbeddingTransport())
    stub_results = await stub_client.search(query="green tea", scope=scope)
    assert stub_results
    stub_digest = stub_results[0].retrieval_policy_digest
    assert stub_digest != local_digest

    from memotron.retrieval import build_retrieval_contract, retrieval_contract_digest

    expected = retrieval_contract_digest(
        build_retrieval_contract(
            operation="search_context",
            scope_keys=(scope.key,),
            query="green tea",
            limit=10,
            policy=stub_client.config.retrieval,
            embedding_identifier="stub-8@v1",
        )
    )
    assert stub_digest == expected


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


async def test_openai_embedding_transport_request_construction(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["authorization"] = request.get_header("Authorization")
        captured["content_type"] = request.get_header("Content-type")
        payload = json.loads(request.data.decode("utf-8"))
        captured["payload"] = payload
        captured["timeout"] = timeout
        # Out of order + un-normalised on purpose: the transport must re-order
        # by index and L2-normalise.
        raw_vectors = ([3.0, 4.0], [0.0, 2.0])
        data = [{"index": index, "embedding": raw_vectors[index]} for index in reversed(range(len(payload["input"])))]
        return _FakeResponse(json.dumps({"data": data}).encode("utf-8"))

    # `gateway`, not `embedding`: the urlopen call lives in
    # OpenAICompatibleTransport._open. urllib.request is one shared module
    # object, so the reach is identical either way.
    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setenv("STUB_EMBED_KEY", "sk-test-embed")
    transport = OpenAICompatibleEmbeddingTransport(
        model="text-embedding-3-small",
        base_url="https://gateway.example.com/v1/",
        api_key_env="STUB_EMBED_KEY",
        timeout_seconds=17.0,
    )
    assert transport.identifier == "openai-embeddings:text-embedding-3-small@v1"

    vectors = transport.embed_batch(["first text", "second text"])

    assert captured["url"] == "https://gateway.example.com/v1/embeddings"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer sk-test-embed"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 17.0
    assert captured["payload"] == {
        "model": "text-embedding-3-small",
        "input": ["first text", "second text"],
    }
    assert vectors[0] == pytest.approx([0.6, 0.8])
    assert vectors[1] == pytest.approx([0.0, 1.0])

    single = transport.embed("first text")
    assert single == pytest.approx([0.6, 0.8])


async def test_openai_embedding_transport_fails_fast_on_missing_key(monkeypatch) -> None:
    def must_not_be_called(request, timeout):  # pragma: no cover - guard
        raise AssertionError("no network request may happen without a key")

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", must_not_be_called)
    monkeypatch.delenv("STUB_EMBED_MISSING_KEY", raising=False)
    transport = OpenAICompatibleEmbeddingTransport(model="text-embedding-3-small", api_key_env="STUB_EMBED_MISSING_KEY")
    with pytest.raises(ValueError, match="STUB_EMBED_MISSING_KEY"):
        transport.embed("anything")

    monkeypatch.setenv("STUB_EMBED_MISSING_KEY", "   ")
    with pytest.raises(ValueError, match="STUB_EMBED_MISSING_KEY"):
        transport.embed("anything")


def test_transport_without_identifier_fails_fast(tmp_path) -> None:
    class NoIdentifierTransport:
        def embed(self, text: str) -> list[float]:  # pragma: no cover - never reached
            return [1.0]

    with pytest.raises(ValueError, match="identifier"):
        Memotron(graph_path=tmp_path / "g.sqlite", embedding_transport=NoIdentifierTransport())


def test_openai_embedding_transport_constructor_validation() -> None:
    with pytest.raises(ValueError, match="model cannot be blank"):
        OpenAICompatibleEmbeddingTransport(model="   ")
    with pytest.raises(ValueError, match="api_key_env cannot be blank"):
        OpenAICompatibleEmbeddingTransport(model="m", api_key_env=" ")
    with pytest.raises(ValueError, match="api_key cannot be blank"):
        OpenAICompatibleEmbeddingTransport(model="m", api_key="  ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenAICompatibleEmbeddingTransport(model="m", timeout_seconds=0.0)


# ---------------------------------------------------------------------------
# Gateway defaults + embedding-endpoint preservation
# ---------------------------------------------------------------------------


def test_openai_embedding_transport_defaults_to_the_gateway() -> None:
    """Bare construction must reach the JedAI Gateway with the gateway key."""
    from memotron.gateway import DEFAULT_GATEWAY_BASE_URL, GATEWAY_API_KEY_ENV

    transport = OpenAICompatibleEmbeddingTransport(model="text-embedding-3")
    assert transport.base_url == DEFAULT_GATEWAY_BASE_URL
    assert transport.api_key_env == GATEWAY_API_KEY_ENV == "LITELLM_API_KEY"


def test_resealing_an_llm_credential_preserves_the_embedding_endpoint(tmp_path) -> None:
    """Rotating the extraction key must NOT silently move a populated tenant
    into a different vector space by wiping ``embedding_*``."""
    from memotron.gateway import DEFAULT_GATEWAY_BASE_URL
    from memotron.graph import PropertyGraphStore

    store = PropertyGraphStore(tmp_path / "credentials.sqlite")
    try:
        store.set_tenant_llm_credentials(
            tenant_id="kb",
            provider="litellm",
            api_key="sk-one",
            base_url=DEFAULT_GATEWAY_BASE_URL,
            model="claude-haiku-4-5",
            embedding_provider="litellm",
            embedding_model="text-embedding-3",
        )

        # Key rotation with no embedding arguments at all.
        state = store.set_tenant_llm_credentials(
            tenant_id="kb",
            provider="litellm",
            api_key="sk-two",
            base_url=DEFAULT_GATEWAY_BASE_URL,
            model="claude-haiku-4-5",
        )
        assert state["embedding_provider"] == "litellm"
        assert state["embedding_model"] == "text-embedding-3"

        # An explicit empty string still clears it — tri-state, not sticky.
        cleared = store.set_tenant_llm_credentials(
            tenant_id="kb",
            provider="litellm",
            api_key="sk-three",
            base_url=DEFAULT_GATEWAY_BASE_URL,
            model="claude-haiku-4-5",
            embedding_provider="",
            embedding_model="",
        )
        assert cleared["embedding_provider"] == ""
        assert cleared["embedding_model"] == ""
    finally:
        store.close()


def test_gateway_embedding_endpoint_resolves_from_env(tmp_path, monkeypatch) -> None:
    """A NEW tenant can select the 3072-dim gateway space from its first
    episode, with the gateway base URL and key name defaulted."""
    from memotron.gateway import DEFAULT_GATEWAY_BASE_URL, GATEWAY_API_KEY_ENV
    from memotron.runtime import build_embedding_transport_from_env

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MEMOTRON_EMBEDDING_BASE_URL", raising=False)
    monkeypatch.delenv("MEMOTRON_EMBEDDING_API_KEY_ENV", raising=False)
    monkeypatch.setenv("MEMOTRON_EMBEDDING_PROVIDER", "litellm")
    monkeypatch.setenv("MEMOTRON_EMBEDDING_MODEL", "text-embedding-3")

    transport = build_embedding_transport_from_env()
    assert isinstance(transport, OpenAICompatibleEmbeddingTransport)
    assert transport.base_url == DEFAULT_GATEWAY_BASE_URL
    assert transport.api_key_env == GATEWAY_API_KEY_ENV
    assert transport.identifier == "openai-embeddings:text-embedding-3@v1"

    # The model IS the vector space: it stays required, never defaulted.
    monkeypatch.delenv("MEMOTRON_EMBEDDING_MODEL")
    with pytest.raises(ValueError, match="MEMOTRON_EMBEDDING_MODEL is required"):
        build_embedding_transport_from_env()


# ---------------------------------------------------------------------------
# Bounded retry on the embedding transport
#
# The pilot lost a whole uncheckpointed formation run to ONE
# ``RemoteDisconnected``.  urllib does not wrap that exception in a ``URLError``
# (it escapes ``getresponse()`` unwrapped), so the transport's HTTPError/URLError
# handlers never even saw it.
# ---------------------------------------------------------------------------


def _embedding_response(count: int = 1) -> _FakeResponse:
    data = [{"index": index, "embedding": [3.0, 4.0]} for index in range(count)]
    return _FakeResponse(json.dumps({"data": data}).encode("utf-8"))


def _fast_retry(max_attempts: int = 3) -> GatewayRetryPolicy:
    """Same attempt budget as production, no wall-clock cost."""
    return GatewayRetryPolicy(max_attempts=max_attempts, initial_backoff_seconds=0.0, max_backoff_seconds=0.0)


def _retrying_transport(policy: GatewayRetryPolicy | None = None):
    return OpenAICompatibleEmbeddingTransport(
        model="text-embedding-3",
        api_key="sk-test-embed",
        retry_policy=policy or _fast_retry(),
    )


def test_embedding_transport_retries_a_dropped_connection_then_succeeds(monkeypatch) -> None:
    """RemoteDisconnected is a dropped connection, not an answer — ask again."""
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        if len(attempts) < 3:
            raise RemoteDisconnected("Remote end closed connection without response")
        return _embedding_response()

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    vector = _retrying_transport().embed("first text")

    assert len(attempts) == 3
    assert vector == pytest.approx([0.6, 0.8])


def test_embedding_transport_retries_a_429_then_gives_up_with_the_attempt_count(
    monkeypatch,
) -> None:
    """A rate limit is transient; exhausting the budget is still a hard failure."""
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise HTTPError(
            "https://gateway.example/v1/embeddings",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b"slow down"),
        )

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    with pytest.raises(GatewayRequestError) as raised:
        _retrying_transport().embed("first text")

    assert len(attempts) == 3
    assert raised.value.attempts == 3
    assert raised.value.status == 429
    assert raised.value.retryable is True
    assert "after 3 attempts" in str(raised.value)
    # The documented contract is unchanged for every existing caller.
    assert isinstance(raised.value, ValueError)


def test_embedding_transport_never_retries_a_deterministic_refusal(monkeypatch) -> None:
    """401 is the gateway's considered answer: repeating it burns quota to hear
    the identical refusal, and hides a misconfigured key behind a delay."""
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        raise HTTPError(
            "https://gateway.example/v1/embeddings",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b"invalid virtual key"),
        )

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    with pytest.raises(GatewayRequestError) as raised:
        _retrying_transport().embed("first text")

    assert len(attempts) == 1
    assert raised.value.attempts == 1
    assert raised.value.status == 401
    assert raised.value.retryable is False
    assert "after" not in str(raised.value)


def test_embedding_transport_does_not_retry_a_malformed_response(monkeypatch) -> None:
    """Response validation sits OUTSIDE the retry: a body the gateway really
    sent will be re-sent identically, so asking again is pure waste."""
    attempts: list[int] = []

    def fake_urlopen(request, timeout):
        attempts.append(1)
        return _FakeResponse(b"{ not json")

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    with pytest.raises(ValueError, match="not valid JSON"):
        _retrying_transport().embed("first text")

    assert len(attempts) == 1


def test_gateway_retry_backoff_is_bounded_and_exponential() -> None:
    """One shared helper owns the delay schedule, so it is pinned once here
    rather than re-derived by each transport that adopts it."""
    slept: list[float] = []
    calls: list[int] = []

    def perform() -> str:
        calls.append(1)
        raise ConnectionResetError("connection reset by peer")

    policy = GatewayRetryPolicy(
        max_attempts=5,
        initial_backoff_seconds=0.5,
        backoff_multiplier=2.0,
        max_backoff_seconds=2.0,
    )
    with pytest.raises(GatewayRequestError) as raised:
        gateway_request_with_retry(perform, description="embedding request", policy=policy, sleep=slept.append)

    assert len(calls) == 5
    assert slept == [0.5, 1.0, 2.0, 2.0]  # doubling, then clamped by the ceiling
    assert raised.value.retryable is True
    assert raised.value.status is None


def test_gateway_retry_policy_validates_its_bounds() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        GatewayRetryPolicy(max_attempts=0)
    with pytest.raises(ValueError, match="initial_backoff_seconds"):
        GatewayRetryPolicy(initial_backoff_seconds=-1.0)
    with pytest.raises(ValueError, match="backoff_multiplier"):
        GatewayRetryPolicy(backoff_multiplier=0.5)
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        GatewayRetryPolicy(max_backoff_seconds=-1.0)


# ---------------------------------------------------------------------------
# A formation run survives an embedding OUTAGE, and never survives silently
# ---------------------------------------------------------------------------


class _OutageEmbeddingTransport:
    """A network transport that goes away part-way through a run.

    Fails for any text carrying *marker*, exactly the way the real transport
    fails once its bounded retry is exhausted.
    """

    identifier = "outage-stub-8@v1"

    def __init__(self, *, marker: str, retryable: bool = True) -> None:
        self.marker = marker
        self.retryable = retryable
        self.seen: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        if self.marker in text.casefold():
            raise GatewayRequestError(
                "embedding request failed: [Errno 54] Connection reset by peer (after 3 attempts)",
                attempts=3,
                retryable=self.retryable,
            )
        vector = [0.0] * 8
        vector[sum(text.encode("utf-8")) % 8] = 1.0
        return vector


async def _queue_fact_episode(client, scope, *, name, subject, obj, when) -> str:
    result = await client.add_episode(
        name=name,
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": subject,
                        "predicate": "prefers",
                        "object": obj,
                        "relationship_type": "PREFERS",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=when,
    )
    return result.episode_uuid


async def test_formation_survives_an_embedding_outage_and_receipts_the_deferral(
    tmp_path,
) -> None:
    """Embeddings feed dedup and clustering, never truth, so an outage must not
    also throw away the extraction earlier episodes already paid the gateway
    for.  The run stops, keeps and CHECKPOINTS what it completed, receipts the
    deferral, and leaves the failing episode queued for the next cycle."""
    transport = _OutageEmbeddingTransport(marker="quarantine")
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="outage")
    client = Memotron(graph_path=tmp_path / "outage.sqlite", embedding_transport=transport)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first = await _queue_fact_episode(client, scope, name="ep1", subject="Team", obj="structured JSON logs", when=start)
    second = await _queue_fact_episode(
        client,
        scope,
        name="ep2",
        subject="Team",
        obj="quarantine queue depth alerts",
        when=start + timedelta(days=1),
    )

    run = await client.run_dream_job(job_name="formation-default")

    # The paid-for work survived and is attested.
    assert run.processed_episodes == 1
    assert client.graph.is_episode_processed(first) is True
    assert client.graph.is_episode_processed(second) is False
    checkpoints = await client.run_checkpoints()
    assert [checkpoint.run_uuid for checkpoint in checkpoints] == [run.job_runs[0].run_uuid]
    facts = [
        str(relationship.properties.get("fact", "")) for relationship in client.graph.relationships_for_scope(scope.key)
    ]
    assert any("structured JSON logs" in fact for fact in facts)
    assert not any("quarantine" in fact for fact in facts)

    # And it is never silent.
    deferrals = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.EMBEDDING_TRANSPORT_UNAVAILABLE
    ]
    assert len(deferrals) == 1
    assert deferrals[0].decision_result == "gated"
    assert deferrals[0].decision_reason == "episode_deferred:attempts=3:status=none"
    assert deferrals[0].embedding_identifier == transport.identifier

    # The next cycle re-forms the deferred episode in full once the endpoint is
    # back — nothing was written half-embedded.
    transport.marker = "\x00never"
    resumed = await client.run_dream_job(job_name="formation-default")
    assert resumed.processed_episodes == 1
    assert client.graph.is_episode_processed(second) is True


async def test_a_deterministic_embedding_failure_still_aborts_the_run(tmp_path) -> None:
    """A 401 is a misconfiguration, not an outage.  Deferring it would retry the
    identical refusal forever behind a receipt; it stays loud and uncheckpointed."""
    transport = _OutageEmbeddingTransport(marker="quarantine", retryable=False)
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="misconfigured")
    client = Memotron(graph_path=tmp_path / "misconfigured.sqlite", embedding_transport=transport)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await _queue_fact_episode(client, scope, name="ep1", subject="Team", obj="structured JSON logs", when=start)
    await _queue_fact_episode(
        client,
        scope,
        name="ep2",
        subject="Team",
        obj="quarantine queue depth alerts",
        when=start + timedelta(days=1),
    )

    with pytest.raises(GatewayRequestError):
        await client.run_dream_job(job_name="formation-default")

    assert await client.run_checkpoints() == []
    assert [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.EMBEDDING_TRANSPORT_UNAVAILABLE
    ] == []


# ---------------------------------------------------------------------------
# Sealing a tenant credential is not a crypto-shred
#
# ``governance_keys`` holds two row kinds under one (scope_key, subject_key)
# primary key.  The tenant's sealed LLM credential lives at
# ``(tenant:{id}, llm_credentials)`` — byte-identical scope key to the tenant
# MEMORY scope.  Reading that row as a content-protection signal silently forced
# a tenant that had just configured text-embedding-3 (3072-dim) onto the
# hermetic 256-dim local transport, with no error anywhere.
# ---------------------------------------------------------------------------


def _seal_credential(store, *, tenant_id: str, api_key: str = "sk-sealed") -> None:
    store.set_tenant_llm_credentials(
        tenant_id=tenant_id,
        provider="litellm",
        api_key=api_key,
        base_url=DEFAULT_GATEWAY_BASE_URL,
        model="claude-haiku-4-5",
        embedding_provider="litellm",
        embedding_model="text-embedding-3",
    )


def test_sealing_a_tenant_credential_does_not_content_protect_the_scope(tmp_path) -> None:
    store = PropertyGraphStore(tmp_path / "credentials.sqlite")
    try:
        scope_key = MemoryScope(kind=ScopeKind.TENANT, scope_id="wdpr").key
        assert scope_key == "tenant:wdpr"
        assert store.scope_content_is_protected(scope_key) is False

        _seal_credential(store, tenant_id="wdpr")

        # The credential row exists — under the CREDENTIAL subject key.
        assert store.governance_key_state(scope_key, LLM_CREDENTIAL_SUBJECT_KEY) is not None
        # The scope's CONTENT plane was never sealed, so it is not protected.
        assert store.governance_key_state(scope_key, CONTENT_PLANE_SUBJECT_KEY) is None
        assert store.scope_content_is_protected(scope_key) is False
    finally:
        store.close()


async def test_a_sealed_credential_never_downgrades_a_tenant_scope_to_local_vectors(
    tmp_path,
) -> None:
    """The operator-visible symptom: configure a 3072-dim endpoint, get 256-dim
    trigram vectors, and never be told."""
    transport = StubEmbeddingTransport()
    client = Memotron(graph_path=tmp_path / "tenant.sqlite", embedding_transport=transport)
    _seal_credential(client.graph, tenant_id="wdpr")
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="wdpr")

    result = await client.add_memory(
        subject="Platform",
        predicate="requires",
        object="mutual TLS between services",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )

    relationship = client.graph.get_relationship(result.relationship_uuid)
    assert relationship.properties["embedding_identifier"] == transport.identifier
    assert relationship.properties["embedding_identifier"] != LOCAL_EMBEDDING_IDENTIFIER
    assert len(relationship.properties["embedding"]) == 8
    assert client.graph.scope_content_is_protected(scope.key) is False


async def test_crypto_shred_governance_still_forces_the_hermetic_transport(
    tmp_path,
) -> None:
    """The downgrade is a governance GUARANTEE, not the bug.  A genuinely
    crypto-shred-governed scope stays protected and keeps its content off the
    network — including when it is a tenant scope that also holds a sealed
    credential, which is precisely the pair the collision confused."""
    from memotron.config import ErasureBehavior, GovernancePolicy, default_config

    transport = _OutageEmbeddingTransport(marker="\x00never")
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    client = Memotron(
        graph_path=tmp_path / "sealed.sqlite",
        config=config,
        embedding_transport=transport,
    )
    _seal_credential(client.graph, tenant_id="wdpr")
    scope = MemoryScope(kind=ScopeKind.TENANT, scope_id="wdpr")

    await client.add_memory(
        subject="Platform",
        predicate="requires",
        object="escalation contact 415-555-0117",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )

    assert client.graph.scope_content_is_protected(scope.key) is True
    forced = client._engine.content_embedding_transport(scope_key=scope.key)
    assert isinstance(forced, LocalEmbeddingTransport)
    assert forced.identifier == LOCAL_EMBEDDING_IDENTIFIER
    # The content never reached the network endpoint, and the downgrade is
    # receipted rather than assumed.
    assert not any("415-555-0117" in text for text in transport.seen)
    downgrades = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.EMBEDDING_TRANSPORT_DOWNGRADED
    ]
    assert downgrades
    assert downgrades[0].embedding_identifier == LOCAL_EMBEDDING_IDENTIFIER


def test_a_pre_existing_colliding_credential_row_is_corrected_on_next_open(
    tmp_path,
) -> None:
    """Migration: existing graphs already carry the colliding row, and the fix
    corrects them by reading the row kind rather than rewriting any data — so
    reopening a large sealed store is a no-op that simply stops mis-firing."""
    path = tmp_path / "legacy.sqlite"
    store = PropertyGraphStore(path)
    _seal_credential(store, tenant_id="wdpr", api_key="sk-legacy")
    scope_key = "tenant:wdpr"

    # The row really does collide: the OLD scope_key-only predicate matches it.
    collided = store._connection.execute(
        "SELECT 1 FROM governance_keys WHERE scope_key = ? LIMIT 1", (scope_key,)
    ).fetchone()
    assert collided is not None
    assert store.scope_content_is_protected(scope_key) is False
    store.close()

    reopened = PropertyGraphStore(path)
    try:
        # Corrected, with the credential untouched and readable.
        assert reopened.scope_content_is_protected(scope_key) is False
        credentials = reopened.tenant_llm_credentials("wdpr")
        assert credentials["api_key"] == "sk-legacy"
        assert credentials["embedding_model"] == "text-embedding-3"
        # No row was moved, duplicated, or rewritten.
        rows = [
            (str(row["scope_key"]), str(row["subject_key"]))
            for row in reopened._connection.execute("SELECT scope_key, subject_key FROM governance_keys").fetchall()
        ]
        assert rows == [(scope_key, LLM_CREDENTIAL_SUBJECT_KEY)]
        # And a genuine content-plane key on the SAME scope still protects it.
        reopened.get_or_create_governance_key(scope_key)
        assert reopened.scope_content_is_protected(scope_key) is True
    finally:
        reopened.close()
