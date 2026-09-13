"""WS-18: LLM synthesis transports for derived-content generation.

``SynthesisTransport`` is the pluggable contract for single-shot
"system prompt + user prompt → text" LLM calls made by platform-internal
synthesis surfaces — rollup rollup summarization (WS-18 T19) and the
session outcome judge (WS-15 T10).  It deliberately mirrors
``extraction.ExtractionTransport`` construction, auth, and error style
(stdlib urllib, fail-fast ``api_key_env``, ``ValueError`` on transport
failure) but returns the raw text content: the CALLER owns strict JSON
parsing and deterministic validation, because synthesis output is always
gated before it can touch the graph.

The single implementation, :class:`OpenAICompatibleSynthesisTransport`, pins
``temperature=0`` and a modest ``max_output_tokens``
(default 300) — synthesis outputs are short, structured, and must be
reproducible-ish; anything longer than the cap is an out-of-contract response
that the caller's strict JSON gate rejects.

``identifier`` names the exact provider+model behind a call.  It is stamped
into synthesis receipts and into the session judge's ``judge_identity``
(``session-judge:{identifier}``), so every LLM-derived artifact names the
model that produced it.
"""

from __future__ import annotations

import asyncio
from typing import Protocol

from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
    OpenAICompatibleChatTransport,
)


class SynthesisTransport(Protocol):
    @property
    def identifier(self) -> str:
        """Short stable string naming the provider and model behind this transport."""
        ...

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        """Return the model's raw text for one system+user exchange.

        Transport-level failures (missing key, HTTP error, malformed provider
        envelope) raise ``ValueError``.  Content-level validation (strict JSON,
        entailment) is the caller's responsibility.
        """
        ...


def strip_markdown_fences(text: str) -> str:
    """Remove a wrapping ``` fence if present (a transport formatting artifact)."""
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return stripped


class OpenAICompatibleSynthesisTransport(OpenAICompatibleChatTransport):
    """OpenAI-compatible chat-completions transport for synthesis calls.

    The only synthesis transport.  Its defaults target the JedAI Gateway
    (:data:`~memotron.gateway.DEFAULT_GATEWAY_BASE_URL` /
    :data:`~memotron.gateway.GATEWAY_API_KEY_ENV`); pass ``base_url`` and
    ``api_key_env`` explicitly to reach any other OpenAI-compatible endpoint.

    Construction, auth, request building and response decoding come from
    :class:`~memotron.gateway.OpenAICompatibleChatTransport`; everything below is
    what makes this transport a SYNTHESIS transport rather than one of the other
    four — its defaults, its ``synthesize`` contract, and its error wording.
    """

    DEFAULT_MODEL = DEFAULT_GATEWAY_MODEL

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_output_tokens: int = 300,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="LLM synthesis",
        )
        self.max_output_tokens = max_output_tokens
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")

    @property
    def identifier(self) -> str:
        return f"openai-compatible:{self.model}"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        return await asyncio.to_thread(self._synthesize_sync, prompt, system_prompt)

    def _synthesize_sync(self, prompt: str, system_prompt: str) -> str:
        # Key first, payload second -- the order this transport has always had.
        api_key = self._resolve_api_key()
        payload = self._chat_payload(system=system_prompt, user=prompt, max_tokens=self.max_output_tokens)
        body = self._post("/chat/completions", payload, api_key=api_key)
        return self._chat_content(body, content_kind="a string")
