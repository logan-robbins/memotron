"""WS-1: Embedding substrate for write-side semantic dedup.

Protocol
--------
``EmbeddingTransport`` defines the pluggable contract (mirrors ExtractionTransport).

Default implementation
----------------------
``LocalEmbeddingTransport`` is the hermetic default:

    Algorithm — character-trigram + token-unigram bag, hashed into a 256-dim vector,
    L2-normalised.

    Why this works for dedup:
    • Near-duplicate paraphrases share most 3-grams and tokens → high cosine.
    • Genuinely different facts (different objects or predicates) share few 3-grams
      → low cosine.

    Why it's hermetic:
    • Pure Python stdlib (hashlib, math).  No network calls, no dependencies.
    • Deterministic: same input always produces the same vector.
    • Fast: a 256-dim vector is computed in <1 ms even on long strings.

    Limitations vs. sentence-transformers:
    • Sensitive to shared vocabulary, not deep semantics (sufficient for D1 dedup).
    • Threshold tuning (default 0.88) compensates for this; per-type aggressiveness
      further narrows the window.

    WS-4 (clustering) and WS-9 (multimodal) reuse the same substrate: the stored
    ``embedding`` list on each relationship is the 256-float L2-normalised vector
    produced by whatever transport was active at materialisation time.  WS-4 can
    read it from ``relationship.properties["embedding"]``; WS-9 can swap the
    transport to a joint vision-language embedder without changing the storage contract.

Production transport
--------------------
``OpenAICompatibleEmbeddingTransport`` (WS-17 T18) posts to any OpenAI-compatible
``/embeddings`` endpoint using the same urllib + fail-fast ``api_key_env``
contract as ``extraction.OpenAICompatibleExtractionTransport``.  Its defaults
target the JedAI Gateway; ``text-embedding-3`` (3072 dims) is the documented
gateway embedding model.  Selecting an embedding endpoint is always explicit —
switching a tenant that already has stored vectors to a different model changes
the vector space and invalidates every vector it has stored.

Transient failures are retried with bounded exponential backoff through the one
shared ``gateway.gateway_request_with_retry`` helper (connection reset /
``RemoteDisconnected`` / timeout / 429 / 5xx); deterministic refusals (401/403,
malformed request) fail immediately.  A terminal failure still raises
``ValueError`` — specifically ``gateway.GatewayRequestError``, whose
``retryable`` flag lets the dream engine defer an episode on a real outage while
still failing fast on a misconfigured credential.

Vector-space integrity (WS-17 T18)
----------------------------------
Every transport carries a required ``identifier`` — a short stable string naming
its vector space.  Materialization stamps the active identifier next to every
stored vector (``relationship.properties["embedding_identifier"]``); every read
that compares a stored vector against a freshly embedded query or candidate
uses the stored vector ONLY when its stamp equals the active transport's
identifier.  A mismatched or missing stamp degrades to the existing
re-embed-revealed-text-on-read fallback, so cosine is never computed across two
different vector spaces.

Pluggable transport
-------------------
Pass any object that satisfies ``EmbeddingTransport`` as ``embedding_transport`` to
``Memotron(...)`` / ``DreamEngine(...)``.  Fail-fast validation is the caller's
responsibility (mirror ``OpenAICompatibleExtractionTransport``: check key at call time,
raise ``ValueError`` before any side effects).
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_RETRY_POLICY,
    GATEWAY_API_KEY_ENV,
    GatewayRetryPolicy,
    OpenAICompatibleTransport,
)

LOCAL_EMBEDDING_IDENTIFIER = "local-trigram-256@v1"
"""Vector-space identifier of :class:`LocalEmbeddingTransport`.

Single source of truth — ``retrieval.EMBEDDING_IDENTIFIER`` imports this value.
If the local embedding algorithm ever changes, this identifier MUST change with
it so vectors from the old space can never be cosine-compared with the new one.
"""


class EmbeddingTransport(Protocol):
    @property
    def identifier(self) -> str:
        """Short stable string naming this transport's vector space.

        Stamped next to every stored vector at materialization time and pinned
        into every ``RetrievalContract``.  Two transports may share an
        identifier only when their vectors are byte-comparable (same algorithm,
        same model, same dimensionality).
        """
        ...

    def embed(self, text: str) -> list[float]:
        """Return an L2-normalised float vector for *text*.

        The vector dimensionality must be consistent across all calls on a given
        transport instance.  Callers store the vector as-is; dedup uses cosine
        similarity, so L2-normalisation is required (dot product == cosine when
        both vectors are unit-length).
        """
        ...


# Embedding dimension.  256 dims is sufficient for n-gram overlap similarity and
# matches the locality-sensitive-hashing sweet-spot for small-to-mid vocab sizes.
_DIM = 256


def _ngrams(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(len(text) - n + 1)]


def _hash_feature(feature: str, dim: int) -> int:
    """Map a feature string to a dimension index in [0, dim)."""
    digest = hashlib.md5(feature.encode("utf-8"), usedforsecurity=False).digest()
    return int.from_bytes(digest[:2], "little") % dim


def _l2_normalise(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return vec
    inv = 1.0 / norm
    return [x * inv for x in vec]


class LocalEmbeddingTransport:
    """Hermetic, deterministic, dependency-free embedding transport.

    Encodes a string as a bag of character 3-grams plus word unigrams, hashed
    into a 256-dimensional count vector, then L2-normalised.

    Makes zero network calls.  Safe for offline simulation and Docker demo.
    """

    identifier = LOCAL_EMBEDDING_IDENTIFIER

    def embed(self, text: str) -> list[float]:
        normalised = " ".join(text.casefold().split())
        vec: list[float] = [0.0] * _DIM

        # Character trigrams
        for gram in _ngrams(normalised, 3):
            vec[_hash_feature(gram, _DIM)] += 1.0

        # Token unigrams
        for token in normalised.split():
            vec[_hash_feature(token, _DIM)] += 1.0

        return _l2_normalise(vec)


class OpenAICompatibleEmbeddingTransport(OpenAICompatibleTransport):
    """OpenAI-compatible ``/embeddings`` transport for production vector spaces.

    WS-17 T18.  Construction, auth and request building are
    :class:`~memotron.gateway.OpenAICompatibleTransport`'s, shared with the
    four chat transports — stdlib urllib, one POST per call, timeout, fail-fast
    ``api_key_env`` contract (the key is resolved at call time; a missing or
    blank key raises ``ValueError`` before any network request).

    It inherits that TRANSPORT layer only, never
    :class:`~memotron.gateway.OpenAICompatibleChatTransport`: ``_chat_payload``
    and ``_chat_content`` describe a ``messages`` request answered by
    ``choices[0].message.content``, which ``/embeddings`` neither accepts nor
    returns.  Inheriting them would put two permanently-invalid methods on this
    class's surface.  The payload here is ``{"model", "input"}`` and the reply is
    ``data[].index`` / ``data[].embedding``.

    Bounded retry.  ``/embeddings`` is a pure function of its payload, so the
    request is safe to repeat, and a dropped connection or a 429/5xx is retried
    with exponential backoff through the shared
    :func:`~memotron.gateway.gateway_request_with_retry` helper.  A
    deterministic refusal (401/403, malformed request) is NOT retried.  Every
    terminal failure still surfaces as a ``ValueError``
    (:class:`~memotron.gateway.GatewayRequestError`), so the transport
    contract is unchanged for existing callers — they simply stop losing a
    whole formation run to one ``RemoteDisconnected``.

    ``identifier`` is ``openai-embeddings:{model}@v1`` — a different model is a
    different vector space, so stored vectors from one model are never
    cosine-compared against queries embedded with another.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        retry_policy: GatewayRetryPolicy = DEFAULT_GATEWAY_RETRY_POLICY,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="embedding",
        )
        self.retry_policy = retry_policy

    @property
    def identifier(self) -> str:
        return f"openai-embeddings:{self.model}@v1"

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts with one ``/embeddings`` POST.

        Response vectors are re-ordered by the API's ``index`` field and
        L2-normalised so the stored-vector contract (dot product == cosine)
        holds regardless of upstream normalisation.
        """
        if not texts:
            raise ValueError("embed_batch requires at least one text")
        # Key first, payload second -- the order this transport has always had.
        api_key = self._resolve_api_key()
        payload = {"model": self.model, "input": list(texts)}
        response_body = self._post_with_retry("/embeddings", payload, policy=self.retry_policy, api_key=api_key)
        # Everything below is response VALIDATION, deliberately outside the
        # retry: a body the gateway sent but Memotron cannot accept is a
        # contract failure, and asking again would produce the same body.
        completion = self._decode_json(response_body)
        data = completion.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ValueError("embedding response must contain a data list with one entry per input")
        vectors: list[list[float] | None] = [None] * len(texts)
        for entry in data:
            if not isinstance(entry, dict):
                raise ValueError("embedding response data entries must be objects")
            index = entry.get("index")
            vector = entry.get("embedding")
            if not isinstance(index, int) or not (0 <= index < len(texts)):
                raise ValueError("embedding response entry has an invalid index")
            if not isinstance(vector, list) or not vector:
                raise ValueError("embedding response entry has an invalid embedding")
            vectors[index] = _l2_normalise([float(value) for value in vector])
        if any(vector is None for vector in vectors):
            raise ValueError("embedding response did not cover every input index")
        return [vector for vector in vectors if vector is not None]


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two pre-normalised vectors (dot product).

    If either vector is all-zeros (failed normalisation), returns 0.0.
    """
    if len(a) != len(b):
        raise ValueError(f"embedding dimension mismatch: {len(a)} vs {len(b)}")
    return sum(x * y for x, y in zip(a, b, strict=True))


def stored_vector_in_active_space(
    properties: dict[str, object] | None,
    *,
    active_identifier: str,
) -> bool:
    """WS-17 T18 vector-space guard predicate.

    True only when the row's stamped ``embedding_identifier`` equals the active
    transport's identifier.  A missing stamp (legacy row) or a mismatch means
    the stored vector's space cannot be proven — callers must treat the stored
    vector as absent and fall back to their existing re-embed-on-read path.
    """
    if not properties:
        return False
    return properties.get("embedding_identifier") == active_identifier
