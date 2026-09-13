"""Canonical JedAI Gateway (LiteLLM) endpoint constants.

Single source of truth for the OpenAI-compatible endpoint every Memotron
transport talks to.  Memotron has exactly one LLM path — an OpenAI-compatible
``/chat/completions`` + ``/embeddings`` endpoint fronted by the JedAI Gateway.
There is no Anthropic-native path: the transports that spoke the native
Anthropic Messages API were removed, and the Claude models the platform uses
are reached as undated gateway aliases (``claude-haiku-4-5``, ``claude-sonnet-4-6``, ``claude-opus-5``,
…).  Dated Anthropic model ids such as ``claude-haiku-4-5-20251001`` DO NOT
resolve on the gateway.

Environment variables
---------------------
``LITELLM_API_KEY``
    The gateway virtual key.  This is the ONLY key name Memotron seeds,
    documents, or defaults to.  Only the NAME ever appears in code, logs,
    receipts, or errors — never a value.
``LITELLM_API_BASE``
    Optional gateway base URL override (must include the ``/v1`` suffix).
    Unset means :data:`DEFAULT_GATEWAY_BASE_URL`.
``MEMOTRON_LLM_MODEL``
    Optional model override for extraction and synthesis.
``MEMOTRON_DREAM_AGENT_MODEL``
    Optional distinct decision model for the dream agent.

Gateway environments are per-cluster and keys are NOT portable across them: a
key minted for ``preview`` 401s on ``latest``/``stage``/``prod``.  The packaged
default targets ``preview`` (Integration) because that is the environment a
developer key is issued against; deployed pods override
:data:`DEFAULT_GATEWAY_BASE_URL` through ``LITELLM_API_BASE`` in their Helm
values and read a cluster-specific key from Vault.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from http.client import HTTPException
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

GATEWAY_API_KEY_ENV = "LITELLM_API_KEY"
"""Canonical environment variable NAME holding the gateway virtual key."""

GATEWAY_BASE_URL_ENV = "LITELLM_API_BASE"
"""Canonical environment variable NAME overriding the gateway base URL."""

GATEWAY_MODEL_ENV = "MEMOTRON_LLM_MODEL"
"""Canonical environment variable NAME overriding the extraction/synthesis model."""

DREAM_AGENT_MODEL_ENV = "MEMOTRON_DREAM_AGENT_MODEL"
"""Environment variable NAME overriding only the dream-agent decision model."""

DEFAULT_GATEWAY_BASE_URL = "https://preview.jedai-gateway.wdprapps.disney.com/v1"
"""Packaged gateway base URL (preview / Integration), including the ``/v1`` path."""

DEFAULT_GATEWAY_MODEL = "claude-haiku-4-5"
"""Packaged extraction / synthesis / dream-agent model (undated gateway alias)."""

DEFAULT_GATEWAY_JUDGE_MODEL = "gpt-4.1-mini"
"""Packaged public-benchmark answer + judge model (undated gateway alias)."""

DEFAULT_GATEWAY_EMBEDDING_MODEL = "text-embedding-3"
"""Gateway embedding alias (3072 dims).

Never applied implicitly: an embedding endpoint must be selected explicitly
(``MEMOTRON_EMBEDDING_PROVIDER`` or the tenant's ``embedding_provider``) and
its model named explicitly, because changing the model changes the vector space
and invalidates every vector already stored for that tenant.
"""


def gateway_base_url_from_env() -> str:
    """Return the gateway base URL from ``LITELLM_API_BASE`` or the default."""
    return os.environ.get(GATEWAY_BASE_URL_ENV, "").strip() or DEFAULT_GATEWAY_BASE_URL


# ---------------------------------------------------------------------------
# Bounded retry for one gateway HTTP request
#
# Memotron has exactly one LLM path, so it gets exactly ONE retry
# implementation.  This helper is the shared primitive every OpenAI-compatible
# transport in the codebase is expected to route its single POST through, rather
# than each growing its own divergent loop.  It is reached through
# :meth:`OpenAICompatibleTransport._post_with_retry`, and is currently wired into
# the two transports with no failure containment above them --
# ``embedding.OpenAICompatibleEmbeddingTransport`` and
# ``extraction.OpenAICompatibleExtractionTransport``.  Dream-agent and synthesis
# failures are already contained at the run/decision level and stay single-shot
# (``_post``).  When those want retry too, they switch helper, rather than
# growing a second implementation to keep in sync.
# ---------------------------------------------------------------------------

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 409, 425, 429, 500, 502, 503, 504})
"""HTTP statuses that mean "the request never got a verdict — ask again".

Everything else is a DETERMINISTIC answer from the gateway: 401/403 (wrong or
missing virtual key), 400/404/422 (malformed request, unknown model). Repeating
those burns quota to receive the identical refusal, so they fail immediately.
"""


@dataclass(frozen=True)
class GatewayRetryPolicy:
    """Bounded exponential backoff for one gateway request.

    Deliberately deterministic (no jitter): Memotron's ingest path is a
    single writer per graph, so there is no thundering herd to spread, and a
    reproducible delay sequence keeps a failing run's behaviour explainable
    from its receipts.
    """

    max_attempts: int = 3
    initial_backoff_seconds: float = 0.5
    backoff_multiplier: float = 2.0
    max_backoff_seconds: float = 8.0

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.initial_backoff_seconds < 0:
            raise ValueError("initial_backoff_seconds cannot be negative")
        if self.backoff_multiplier < 1.0:
            raise ValueError("backoff_multiplier must be at least 1.0")
        if self.max_backoff_seconds < 0:
            raise ValueError("max_backoff_seconds cannot be negative")


DEFAULT_GATEWAY_RETRY_POLICY = GatewayRetryPolicy()
"""Three attempts, 0.5s → 1.0s of backoff — bounded well inside a job cadence."""


class GatewayRequestError(ValueError):
    """Terminal failure of one gateway request, after any bounded retries.

    Subclasses ``ValueError`` so the documented transport contract ("transient
    failures surface as ``ValueError``") is unchanged for every existing caller.
    ``retryable`` distinguishes an exhausted transient outage (the gateway never
    answered, or kept answering 5xx/429) from a deterministic refusal (401/403,
    malformed request) — callers that want to defer work on an outage but fail
    fast on a misconfiguration branch on this flag, never on the message.
    """

    def __init__(self, message: str, *, attempts: int, retryable: bool, status: int | None = None) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.retryable = retryable
        self.status = status


def gateway_request_with_retry[T](
    perform: Callable[[], T],
    *,
    description: str,
    policy: GatewayRetryPolicy = DEFAULT_GATEWAY_RETRY_POLICY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run one idempotent gateway request, retrying only transient failures.

    *perform* must issue the request and return its decoded body; it is called
    up to ``policy.max_attempts`` times, so it must be safe to repeat.  Both
    gateway endpoints Memotron posts to are pure functions of their payload,
    which is what makes retrying a POST correct here.

    Retried: a connection that was reset, dropped mid-response
    (``http.client.RemoteDisconnected`` — the failure that aborted the pilot),
    refused, or timed out, plus any status in
    :data:`RETRYABLE_HTTP_STATUS_CODES`.  Note that ``RemoteDisconnected`` is
    raised out of ``getresponse()`` and is therefore NOT wrapped in a
    ``URLError`` by urllib — catching only ``HTTPError``/``URLError`` misses it
    entirely, which is why ``OSError`` and ``HTTPException`` are caught here.

    Not retried: every other HTTP status, which is the gateway's considered
    answer and will not change.

    Raises :class:`GatewayRequestError` when attempts are exhausted or the
    failure was deterministic.  Decoding and schema validation of the response
    body belong to the caller and stay OUTSIDE this retry — a malformed body is
    not a transport fault.
    """
    backoff = policy.initial_backoff_seconds
    attempt = 1
    while True:
        try:
            return perform()
        except HTTPError as exc:
            # Read the body eagerly: HTTPError is a one-shot stream, and on the
            # final attempt it is the only diagnostic the caller ever sees.
            body = exc.read().decode("utf-8", errors="replace")
            message = f"{description} failed with HTTP {exc.code}: {body}"
            status: int | None = exc.code
            retryable = exc.code in RETRYABLE_HTTP_STATUS_CODES
            error: BaseException = exc
        except (URLError, HTTPException, OSError) as exc:
            reason = getattr(exc, "reason", None)
            message = f"{description} failed: {reason if reason is not None else exc!r}"
            status = None
            retryable = True
            error = exc
        if not retryable or attempt >= policy.max_attempts:
            if attempt > 1:
                message = f"{message} (after {attempt} attempts)"
            raise GatewayRequestError(message, attempts=attempt, retryable=retryable, status=status) from error
        sleep(backoff)
        backoff = min(policy.max_backoff_seconds, backoff * policy.backoff_multiplier)
        attempt += 1


# ---------------------------------------------------------------------------
# The shared base for the six OpenAI-compatible transports
#
# Two layers, because /embeddings and /chat/completions are different protocols
# that happen to share an envelope:
#
#   OpenAICompatibleTransport      construction, auth, signed POST, JSON decode
#   └── OpenAICompatibleChatTransport   + the /chat/completions body and reply
#
# ---------------------------------------------------------------------------


class OpenAICompatibleTransport:
    """Construction, auth, signed request building and JSON decoding, once.

    SIX transports in this package speak an OpenAI-compatible protocol to the
    same gateway — ``extraction``, ``agents``, ``embedding``, ``context``,
    ``synthesis`` and ``certification.OpenAICompatibleBenchmarkRuntime``.
    Before this class they each carried their own copy of the same blocks: the
    field validation, the fail-fast ``api_key_env`` resolution plus
    ``Authorization``/``Content-Type`` request build, and the ``json.loads`` →
    ``choices[0].message.content`` decode.  A bug in the auth block was six
    edits, in six files, that a fixer had to know all existed.  It lives here so
    it is one edit.

    **What each of the six shares, and what it keeps.**  All six route their
    POST through :meth:`_signed_request` / :meth:`_open`, which is the auth
    block this class exists for.  The benchmark runtime keeps its own field
    validation (its messages are prefixed ``benchmark runtime …`` and its
    ``temperature`` check sits between the ``api_key`` and ``timeout_seconds``
    checks) and its own response decode (it fuses the JSON and shape errors into
    one message and rejects blank content); it hands this class already-
    normalised values, so the checks below are satisfied trivially.  Preserving
    that ordering exactly is worth more than deduplicating five ``if``
    statements.

    **Why the error text is parametrised rather than unified.**  The distinct
    messages these transports raise are the same handful of sentences with a
    different subject, and callers' tests pin the wording verbatim (e.g.
    ``tests/test_dream_agent_transport.py``).  So each subclass passes its own
    *error_label* — ``"LLM synthesis"``, ``"context maintenance"``,
    ``"LLM dream-agent"``, ``"LLM extraction"``, ``"embedding"``,
    ``"benchmark completion"`` — and every message raised here is built from it:

    * ``{request_label} failed with HTTP {code}: {body}``
    * ``{request_label} failed: {reason}``
    * ``{error_label} response was not valid JSON``

    plus, on :class:`OpenAICompatibleChatTransport`:

    * ``{error_label} response did not contain choices[0].message.content``
    * ``{error_label} message content must be {content_kind}``

    *request_label* defaults to ``f"{error_label} request"``, which is what five
    of the six want; the benchmark runtime says ``benchmark completion failed
    with HTTP …`` with no ``request``, so it passes the label explicitly rather
    than having its wording quietly changed by this refactor.  The request label
    is also the retry helper's *description*, which formats those two sentences
    identically — so a transport reads the same whether its POST is single-shot
    or retried.

    **What is deliberately NOT here.**  ``identifier`` stays on the subclasses.
    Three of them return ``openai-compatible:{model}``, embedding returns
    ``openai-embeddings:{model}@v1``, the benchmark runtime appends its
    temperature, and the extraction transport has no ``identifier`` at all;
    defining one here would silently add a public member to extraction's API.
    The two ``/chat/completions`` helpers are not here either — see
    :class:`OpenAICompatibleChatTransport`.  Likewise each subclass keeps its own
    ``__init__`` signature, because the default sets (model, timeout, token
    ceiling, retry policy) are measured per transport and are not
    interchangeable.
    """

    def __init__(
        self,
        *,
        model: str,
        base_url: str,
        api_key_env: str,
        api_key: str | None,
        timeout_seconds: float,
        error_label: str,
        request_label: str | None = None,
    ) -> None:
        self.model = model.strip()
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env.strip()
        self.api_key = api_key.strip() if api_key is not None else None
        self.timeout_seconds = timeout_seconds
        self._error_label = error_label
        self._request_label = f"{error_label} request" if request_label is None else request_label
        if not self.model:
            raise ValueError("model cannot be blank")
        if not self.base_url:
            raise ValueError("base_url cannot be blank")
        if not self.api_key_env:
            raise ValueError("api_key_env cannot be blank")
        if api_key is not None and not self.api_key:
            raise ValueError("api_key cannot be blank")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")

    # -- request -----------------------------------------------------------

    def _resolve_api_key(self) -> str:
        """Return the gateway key, or raise naming ONLY the variable.

        The key is read at CALL time, never at construction, so a process that
        never makes a request never needs one.

        **Call this before building the payload.**  Every one of these
        transports resolved the key as the first statement of its request
        method, ahead of any prompt assembly, and callers' behaviour depends on
        it: a request object that cannot be turned into a payload still reports
        the missing key first.  That is why the key is a required argument to
        :meth:`_post` and :meth:`_post_with_retry` rather than something they
        fetch for themselves — resolving it inside would move it after payload
        construction and silently change which error a misconfigured caller
        sees.
        """
        api_key = self.api_key or os.environ.get(self.api_key_env, "").strip()
        if not api_key:
            raise ValueError(f"missing required environment variable: {self.api_key_env}")
        return api_key

    def _signed_request(self, path: str, payload: dict[str, Any], *, api_key: str) -> urllib_request.Request:
        """Build the signed POST from an already-resolved *api_key*.

        The key never appears in a message, only in the ``Authorization``
        header — the rule the whole package holds for gateway keys.
        """
        return urllib_request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

    def _open(self, http_request: urllib_request.Request) -> str:
        """The one ``urlopen`` these six transports make.  Errors pass through."""
        with urllib_request.urlopen(http_request, timeout=self.timeout_seconds) as response:
            return str(response.read().decode("utf-8"))

    def _post(self, path: str, payload: dict[str, Any], *, api_key: str) -> str:
        """Single-shot signed POST; returns the raw response body.

        No retry: for the transports that use this, a failure is already
        contained at the run or decision level above them.  Transports whose
        failure loses a whole run use :meth:`_post_with_retry` instead.

        *api_key* comes from :meth:`_resolve_api_key`, which the caller must
        have run BEFORE building *payload* — see that method.
        """
        http_request = self._signed_request(path, payload, api_key=api_key)
        try:
            return self._open(http_request)
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ValueError(f"{self._request_label} failed with HTTP {exc.code}: {body}") from exc
        except URLError as exc:
            raise ValueError(f"{self._request_label} failed: {exc.reason}") from exc

    def _post_with_retry(self, path: str, payload: dict[str, Any], *, policy: GatewayRetryPolicy, api_key: str) -> str:
        """Signed POST through the shared bounded retry; returns the raw body.

        Raises :class:`GatewayRequestError` (a ``ValueError``) when attempts are
        exhausted or the gateway's refusal was deterministic.  Response decoding
        stays outside the retry — see :func:`gateway_request_with_retry`.

        *api_key* comes from :meth:`_resolve_api_key`, which the caller must
        have run BEFORE building *payload* — see that method.
        """
        http_request = self._signed_request(path, payload, api_key=api_key)
        return gateway_request_with_retry(
            lambda: self._open(http_request),
            description=self._request_label,
            policy=policy,
        )

    # -- response ----------------------------------------------------------

    def _decode_json(self, response_body: str) -> Any:
        """Parse a response body, or raise this transport's own JSON error."""
        try:
            return json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self._error_label} response was not valid JSON") from exc


class OpenAICompatibleChatTransport(OpenAICompatibleTransport):
    """:class:`OpenAICompatibleTransport` plus the ``/chat/completions`` shape.

    Split out from the transport layer because ``_chat_payload`` and
    ``_chat_content`` are ``/chat/completions`` concepts: a ``messages`` array
    with ``response_format``, answered by ``choices[0].message.content``.
    Neither can ever be valid on the ``/embeddings`` endpoint, which takes
    ``{"model", "input"}`` and answers with ``data[].embedding`` — so
    ``embedding.OpenAICompatibleEmbeddingTransport`` inherits the layer above
    and never sees them.  The four chat transports inherit this one.

    ``certification.OpenAICompatibleBenchmarkRuntime`` posts to
    ``/chat/completions`` but inherits the transport layer too, because its body
    carries a caller-chosen ``temperature`` and omits ``response_format``, and
    its reply decode fuses the JSON and shape failures into one message.  It
    shares the auth block, which is the part that was duplicated six ways; it
    does not share a payload shape it does not have.
    """

    def _chat_payload(self, *, system: str, user: str, max_tokens: int) -> dict[str, Any]:
        """The one ``/chat/completions`` body shape, for all four chat transports.

        ``max_tokens`` is always explicit: omitting it lets the gateway apply its
        own 4096 default, which truncated Claude-family responses to zero-length
        content and aborted three consecutive portal rebuilds.
        ``response_format`` is sent for documentation value even though the
        gateway's ``drop_params: true`` discards it — which is why callers parse
        the content tolerantly rather than assuming a bare JSON body.
        """
        return {
            "model": self.model,
            "temperature": 0,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }

    def _chat_content(self, response_body: str, *, content_kind: str) -> str:
        """Pull ``choices[0].message.content`` out of a chat completion.

        *content_kind* names what the caller expects the string to hold ("a
        string", "a JSON string") and appears verbatim in the type error, so
        each transport keeps the wording its own tests assert on.
        """
        completion = self._decode_json(response_body)
        try:
            content = completion["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"{self._error_label} response did not contain choices[0].message.content") from exc
        if not isinstance(content, str):
            raise ValueError(f"{self._error_label} message content must be {content_kind}")
        return content
