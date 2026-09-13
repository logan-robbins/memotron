"""``CavemanChatTransport`` — the live strict-JSON chat transport for this package.

Subclasses :class:`~memotron.gateway.OpenAICompatibleChatTransport` exactly as
``synthesis.py:66`` does, and satisfies the existing
:class:`~memotron.synthesis.SynthesisTransport` Protocol, so every stage
depends on that Protocol and this is merely one implementation of it.

**Not a second copy of the synthesis transport.** Two measured differences make
it its own transport rather than a reuse of
``OpenAICompatibleSynthesisTransport``:

* **Retry.** Synthesis uses single-shot ``_post``, correctly, because a
  synthesis failure is already contained at the run or decision level above it.
  A caveman stage failure loses a whole ingest or a whole global pass, so this
  one routes through ``_post_with_retry`` (``gateway.py:378``) — the shared
  bounded-backoff helper every OpenAI-compatible transport in this repo is
  expected to use rather than growing its own loop.
* **Output budget.** Synthesis pins ~300 output tokens, which is right for a
  two-sentence theme summary and truncates a global dream pass to nothing. 4096
  is the ceiling here.

Everything else — construction, auth, request signing, response decoding — comes
from the base classes untouched. There is no ``response_format`` handling to
write: the gateway's ``drop_params: true`` discards it (``gateway.py:432-434``),
which is exactly why ``llm.strict_json_call`` reads the content tolerantly
rather than assuming a bare JSON body.
"""

from __future__ import annotations

import asyncio

from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    DEFAULT_GATEWAY_RETRY_POLICY,
    GATEWAY_API_KEY_ENV,
    GatewayRetryPolicy,
    OpenAICompatibleChatTransport,
)

DEFAULT_MAX_OUTPUT_TOKENS = 4096
"""Output ceiling for a caveman exchange.

A global dream pass returns an ops array over a whole scope's inventory, which
does not fit the 300 tokens the synthesis transport pins. Always sent
explicitly: omitting ``max_tokens`` lets the gateway apply its own default,
which truncated Claude-family responses to zero-length content and aborted three
consecutive portal rebuilds (``gateway.py``'s ``_chat_payload`` docstring).
"""

DEFAULT_TIMEOUT_SECONDS = 180.0
"""Longer than synthesis's 60s: these prompts carry a whole episode or inventory."""


class CavemanChatTransport(OpenAICompatibleChatTransport):
    """One system+user exchange against the JedAI Gateway, retried and budgeted.

    Defaults target the gateway (``DEFAULT_GATEWAY_BASE_URL`` /
    ``GATEWAY_API_KEY_ENV``); pass ``base_url`` and ``api_key_env`` explicitly to
    reach any other OpenAI-compatible endpoint.

    Content validation is deliberately NOT here — it is
    ``llm.strict_json_call``'s job, per the ``SynthesisTransport`` contract. A
    transport that also validated content would make "the gateway was
    unreachable" and "the model answered out of contract" the same failure, and
    they are handled by different callers and receipted differently.
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_GATEWAY_MODEL,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        retry_policy: GatewayRetryPolicy = DEFAULT_GATEWAY_RETRY_POLICY,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="caveman chat",
        )
        self.max_output_tokens = max_output_tokens
        self.retry_policy = retry_policy
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")

    @property
    def identifier(self) -> str:
        """Names the transport as well as the model.

        Deliberately not ``openai-compatible:{model}``, which the synthesis and
        dream-agent transports return: this one has retry and a 4096-token
        budget, and a receipt naming the model alone could not tell a caveman
        call from a 300-token theme summary made against the same alias.
        """
        return f"caveman-chat:{self.model}"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        """Return the model's raw text. Transport failures raise ``ValueError``."""
        return await asyncio.to_thread(self._synthesize_sync, prompt, system_prompt)

    def _synthesize_sync(self, prompt: str, system_prompt: str) -> str:
        # Key first, payload second. The base class documents why: a request that
        # cannot be turned into a payload must still report the missing key first.
        api_key = self._resolve_api_key()
        payload = self._chat_payload(system=system_prompt, user=prompt, max_tokens=self.max_output_tokens)
        body = self._post_with_retry(
            "/chat/completions",
            payload,
            policy=self.retry_policy,
            api_key=api_key,
        )
        return self._chat_content(body, content_kind="a JSON object")
