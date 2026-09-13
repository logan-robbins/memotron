from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from memotron.extraction import parse_first_json_object, strip_markdown_fences
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
    OpenAICompatibleChatTransport,
)
from memotron.models import DreamJobKind, MemoryScope

__all__ = [
    "DREAM_AGENT_SYSTEM_PROMPT",
    "DreamAgentContractError",
    "DreamAgentDecision",
    "DreamAgentDecisionRequest",
    "DreamAgentTransport",
    "LocalDreamAgentTransport",
    "OpenAICompatibleDreamAgentTransport",
    "build_decision_prompt",
    "parse_decision",
    "parse_first_json_object",
    "strip_markdown_fences",
]

DREAM_AGENT_SYSTEM_PROMPT = (
    "You are a memory maintenance dream agent. "
    "Evaluate proposed offline graph-memory maintenance actions. "
    "Return only strict JSON with keys approved, summary, and details. "
    "Do not include prose, markdown, or code fences — only the JSON object."
)
"""The one system message every LLM-backed dream-agent transport sends.

Shared so the Claude Agent SDK transport and the OpenAI-compatible transport
put a byte-identical decision contract in front of the model — a decision made
through the gateway is the same decision, not a re-specified one."""


@dataclass(frozen=True)
class DreamAgentDecisionRequest:
    agent_id: str
    agent_name: str
    decision_policy: str
    job_name: str
    job_kind: DreamJobKind
    decision_type: str
    proposed_action: str
    subject_id: str | None = None
    subject_name: str | None = None
    scope: MemoryScope | None = None
    prompt_profile: str | None = None
    prompt_profile_version: str | None = None
    context_facts: tuple[str, ...] = ()
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DreamAgentDecision:
    approved: bool
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


class DreamAgentTransport(Protocol):
    async def decide(self, request: DreamAgentDecisionRequest) -> DreamAgentDecision:
        """Approve or reject an offline dream action and provide an auditable summary."""


class LocalDreamAgentTransport:
    """Deterministic dream-agent transport for local tests and examples."""

    async def decide(self, request: DreamAgentDecisionRequest) -> DreamAgentDecision:
        summary = (
            f"{request.agent_name} approved {request.decision_type} for "
            f"{request.subject_name or request.subject_id or request.job_name}."
        )
        return DreamAgentDecision(
            approved=True,
            summary=summary,
            details={
                "transport": "local",
                "proposed_action": request.proposed_action,
                "context_fact_count": len(request.context_facts),
            },
        )


class DreamAgentContractError(ValueError):
    """The model answered, but the answer is not a usable dream-agent decision.

    Distinct from a transport failure (missing key, HTTP error, connection
    reset, malformed provider envelope), where the model never spoke at all.
    Both are contained by ``DreamEngine._decide``, but only this one means an
    answer EXISTED and could not be read — so an operator debugging a run needs
    to tell them apart: a transport error is an infrastructure problem, a
    contract error is a prompt/model problem.

    Subclasses ``ValueError`` because that is the transports' declared failure
    type; every existing ``except ValueError`` / ``pytest.raises(ValueError)``
    caller keeps working unchanged.
    """


def build_decision_prompt(request: DreamAgentDecisionRequest) -> str:
    """Render the transport-agnostic decision prompt for *request*.

    Deliberately provider-free: the same canonical JSON payload is what the
    Claude Agent SDK transport and the OpenAI-compatible transport send, so
    swapping providers cannot silently change what the agent was asked."""
    payload = {
        "agent": {
            "id": request.agent_id,
            "name": request.agent_name,
            "decision_policy": request.decision_policy,
        },
        "job": {
            "name": request.job_name,
            "kind": request.job_kind.value,
            "prompt_profile": request.prompt_profile,
            "prompt_profile_version": request.prompt_profile_version,
        },
        "decision": {
            "type": request.decision_type,
            "proposed_action": request.proposed_action,
            "subject_id": request.subject_id,
            "subject_name": request.subject_name,
            "scope": request.scope.key if request.scope is not None else None,
            "context_facts": list(request.context_facts),
            "details": request.details,
        },
        "required_response": {
            "approved": "boolean — true to proceed, false to reject",
            "summary": "one concise sentence explaining the decision",
            "details": "JSON object with operational metadata only",
        },
        "instruction": "Respond with only the JSON object. No prose, no markdown fences.",
    }
    return json.dumps(payload, sort_keys=True)


def parse_decision(response_text: str, *, source: str) -> DreamAgentDecision:
    """Validate one raw model response into a :class:`DreamAgentDecision`.

    **Tolerant about envelope, strict about contract.**  Envelope tolerance and
    contract strictness are different things, and conflating them is what made
    a well-formed decision look like a failure: this parser used to run bare
    ``json.loads`` while the extraction path used the tolerant
    :func:`~memotron.extraction.parse_first_json_object`.  Gateways that
    proxy Claude models (LiteLLM with ``drop_params: true``) silently discard
    ``response_format={"type":"json_object"}``, so the model answers with
    fenced JSON — often with a closing fence AND a sentence of prose after it,
    which the fence stripper alone cannot remove.  ``json.loads`` then raised
    (observed live as ``Expecting ',' delimiter``) and a perfectly good
    decision was receipted as a transport failure and replaced by a fallback.

    Both paths now share ONE tolerant envelope reader — fences stripped, then
    the first complete JSON object taken and any trailing prose ignored — so
    extraction and decisioning cannot drift in what they accept off the wire.

    The contract itself stays exactly as strict: a blank response, a body with
    no JSON object in it, or an out-of-contract payload raises
    :class:`DreamAgentContractError`, never a silently coerced approval.
    *source* names the transport in the message so an operator can tell which
    path produced it."""
    if not response_text.strip():
        raise DreamAgentContractError(f"{source} returned an empty dream-agent decision")
    cleaned = strip_markdown_fences(response_text.strip())
    try:
        payload = parse_first_json_object(cleaned, source=source)
    except ValueError as exc:
        raise DreamAgentContractError(f"{source} dream-agent decision was not valid JSON") from exc
    approved = payload.get("approved")
    summary = payload.get("summary")
    details = payload.get("details", {})
    if not isinstance(approved, bool):
        raise DreamAgentContractError(f"{source} dream-agent decision must include boolean approved")
    if not isinstance(summary, str) or not summary.strip():
        raise DreamAgentContractError(f"{source} dream-agent decision must include non-blank summary")
    if not isinstance(details, dict):
        raise DreamAgentContractError(f"{source} dream-agent decision details must be an object")
    return DreamAgentDecision(approved=approved, summary=summary.strip(), details=details)


class OpenAICompatibleDreamAgentTransport(OpenAICompatibleChatTransport):
    """OpenAI-compatible chat-completions transport for dream-agent decisions.

    One decision is ONE strict-JSON ``POST {base_url}/chat/completions`` call at
    ``temperature=0``: no nested ``claude`` CLI subprocess, no ``os.environ``
    mutation around the call, and no dependency on an Anthropic-specific SDK.
    That makes dream-agent decisioning ride the same OpenAI-compatible path
    (JedAI Gateway / LiteLLM proxy / OpenAI) that extraction, synthesis, and
    embeddings already use, instead of being the one component that cannot.

    **What it replaced.** The retired ``ClaudeAgentDreamTransport`` drove the
    Claude Agent SDK with ``allowed_tools=[]`` and consumed a single
    prompt→JSON turn, so none of the SDK's agentic machinery (tool use,
    multi-turn planning, subagents, file access) was ever exercised by a
    dream-agent decision.  It also spawned a nested ``claude`` CLI, which fails
    when the caller is itself a ``claude`` session.  This transport sends the
    same :data:`DREAM_AGENT_SYSTEM_PROMPT` and the same
    :func:`build_decision_prompt` payload and validates through the same
    :func:`parse_decision`: the decision contract is identical, only the wire
    protocol and process model changed.  Claude models are still reachable —
    as undated gateway aliases such as ``claude-haiku-4-5``.

    Note that this transport sets ``response_format={"type":"json_object"}``
    and the gateway's ``drop_params: true`` discards it, so the model answers
    with fenced JSON and sometimes trailing prose regardless; that is why
    :func:`parse_decision` reads the envelope tolerantly.

    Construction, auth, and error handling are
    :class:`~memotron.gateway.OpenAICompatibleChatTransport`'s, shared with the
    other four transports: ``api_key_env`` names the environment variable
    holding the key and is resolved fail-fast at decision time (the error names
    only the variable, never a value), provider failures surface as
    ``ValueError`` carrying the provider's own status/body, and the API key is
    never echoed into a message.
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
        max_output_tokens: int = 512,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="LLM dream-agent",
        )
        self.max_output_tokens = max_output_tokens
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")

    @property
    def identifier(self) -> str:
        """Short stable string naming the provider and model behind this transport."""
        return f"openai-compatible:{self.model}"

    async def decide(self, request: DreamAgentDecisionRequest) -> DreamAgentDecision:
        return await asyncio.to_thread(self._decide_sync, request)

    def _decide_sync(self, request: DreamAgentDecisionRequest) -> DreamAgentDecision:
        # Key first, payload second -- the order this transport has always had.
        # build_decision_prompt() can raise on a malformed request, and a caller
        # with no key configured must still be told that first.
        api_key = self._resolve_api_key()
        payload = self._chat_payload(
            system=DREAM_AGENT_SYSTEM_PROMPT,
            user=build_decision_prompt(request),
            max_tokens=self.max_output_tokens,
        )
        body = self._post("/chat/completions", payload, api_key=api_key)
        content = self._chat_content(body, content_kind="a JSON string")
        return parse_decision(content, source="LLM dream-agent")
