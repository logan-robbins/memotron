"""WS-24: gateway-routed dream-agent decisions + fail-safe transport failure.

Two contracts are pinned here.

**The transport.** ``OpenAICompatibleDreamAgentTransport`` makes one dream-agent
decision with ONE strict-JSON ``/chat/completions`` call — no nested ``claude``
CLI subprocess and no ``os.environ`` mutation, which is what let the Claude
Agent SDK path fail in production with a bare
``Exception("Claude Code returned an error result: success")``.  It reuses the
transport-agnostic ``build_decision_prompt`` / ``parse_decision`` helpers, so a
decision made through the gateway is validated against exactly the same
contract as one made through the SDK.

**The fail-safe.** A dream-agent transport failure must never take down a
dreaming run.  ``DreamEngine._decide`` now contains any exception from
``decide``, receipts it as ``DREAM_AGENT_TRANSPORT_FAILED`` on the run's hash
chain, marks the ``DreamDecisionRecord`` as a fallback, and continues with the
deterministic decision — except for :data:`_FAIL_CLOSED_DECISION_TYPES`, which
reject.

**The split (WS-24).** A fallback may approve CREATION; it may never approve
REMOVAL, and never an operator-required approval.  Formation and consolidation
therefore fail open; the untrusted-directive gate and the whole pruning family
fail closed.  The same split applies to an unreadable ANSWER
(``DreamAgentContractError``) as to an unreachable transport — the two are
receipted as distinct failure kinds, because one is a prompt problem and the
other is an outage.

**Envelope tolerance (WS-24).** ``parse_decision`` shares the extraction path's
tolerant reader (fences stripped, first JSON object taken, trailing prose
ignored), because the gateway's ``drop_params: true`` discards
``response_format`` and the model answers with fenced JSON plus prose.  Under
the old strict ``json.loads`` those well-formed decisions raised, and a
REJECTION became a fallback APPROVAL.

Every test is hermetic: the HTTP layer is monkeypatched, never called live.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Self
from urllib.error import HTTPError, URLError

import pytest

from memotron import (
    Memotron,
    EpisodeType,
    ErasureBehavior,
    GovernancePolicy,
    MemoryScope,
    OpenAICompatibleDreamAgentTransport,
    PiiSensitivity,
    ScopeKind,
    is_sealed_content,
)
from memotron import agents as agents_module
from memotron import dreaming as dreaming_module
from memotron import gateway as gateway_module
from memotron.agents import (
    DREAM_AGENT_SYSTEM_PROMPT,
    DreamAgentDecisionRequest,
    LocalDreamAgentTransport,
    build_decision_prompt,
    parse_decision,
)
from memotron.config import (
    DreamAgentConfig,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    NodeInstruction,
    PruningPolicy,
    RelationshipInstruction,
    RollupConsolidationPolicy,
    default_config,
)
from memotron.extraction import (
    OpenAICompatibleExtractionTransport,
    RuleBasedExtractionTransport,
)
from memotron.gateway import DEFAULT_GATEWAY_BASE_URL, DEFAULT_GATEWAY_MODEL
from memotron.models import DreamJobKind, RelationshipStatus
from memotron.receipts import ReceiptDecisionType
from memotron.runtime import (
    build_synthesis_transport_from_credentials,
    build_transports_from_credentials,
    build_transports_from_env,
)

USER_SCOPE = MemoryScope(kind=ScopeKind.USER, scope_id="user-42")
AGENT_SCOPE = MemoryScope(kind=ScopeKind.AGENT, scope_id="truth-curator")
RUN_TIME = datetime(2026, 6, 1, tzinfo=UTC)


def decision_request(decision_type: str = "formation_episode_selected") -> DreamAgentDecisionRequest:
    return DreamAgentDecisionRequest(
        agent_id="truth-curator",
        agent_name="Truth Curator",
        decision_policy="Approve only auditable memory maintenance decisions.",
        job_name="formation-agent",
        job_kind=DreamJobKind.FORMATION,
        decision_type=decision_type,
        proposed_action="Process this queued episode for memory formation.",
        subject_id="episode-1",
        subject_name="episode one",
        scope=USER_SCOPE,
        context_facts=("User 42 prefers CSV exports",),
        details={"instruction_set": "default"},
    )


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def completion_body(content: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": content}}]}).encode("utf-8")


def stub_urlopen(monkeypatch: pytest.MonkeyPatch, content: str) -> dict[str, object]:
    """Monkeypatch the transport's urlopen to return *content*; capture the request.

    Patched on ``gateway`` because that is where the ``urlopen`` call now lives
    (``OpenAICompatibleTransport._open``).  ``urllib.request`` is a single shared
    module object, so this was never module-scoped isolation — patching it via
    ``agents`` had exactly the same global reach.  Naming ``gateway`` just points
    at the code that actually runs.
    """
    captured: dict[str, object] = {}

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["authorization"] = request.get_header("Authorization")
        captured["content_type"] = request.get_header("Content-type")
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        captured["timeout"] = timeout
        return _FakeResponse(completion_body(content))

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    return captured


def raise_urlopen(monkeypatch: pytest.MonkeyPatch, make_exc) -> None:
    """Raise a FRESH exception per call — an ``HTTPError``'s body reads once."""

    def fake_urlopen(request, timeout):
        raise make_exc()

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)


def http_error(code: int, body: str) -> HTTPError:
    return HTTPError(
        "https://gateway.example.com/v1/chat/completions",
        code,
        "Internal Server Error",
        {},  # type: ignore[arg-type]
        io.BytesIO(body.encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# Request construction, auth, and constructor validation
# ---------------------------------------------------------------------------


async def test_request_construction_is_one_strict_json_chat_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = stub_urlopen(
        monkeypatch,
        json.dumps({"approved": True, "summary": "Approved.", "details": {"k": "v"}}),
    )
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test-dream-agent")
    transport = OpenAICompatibleDreamAgentTransport(
        model="gpt-4o-mini",
        base_url="https://gateway.example.com/v1/",
        api_key_env="STUB_DREAM_AGENT_KEY",
        timeout_seconds=23.0,
        max_output_tokens=256,
    )
    assert transport.identifier == "openai-compatible:gpt-4o-mini"

    request = decision_request()
    await transport.decide(request)

    assert captured["url"] == "https://gateway.example.com/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["authorization"] == "Bearer sk-test-dream-agent"
    assert captured["content_type"] == "application/json"
    assert captured["timeout"] == 23.0

    payload = captured["payload"]
    assert payload["model"] == "gpt-4o-mini"
    assert payload["temperature"] == 0
    assert payload["max_tokens"] == 256
    assert payload["response_format"] == {"type": "json_object"}

    system, user = payload["messages"]
    assert system["role"] == "system"
    # The strict-JSON instruction is the SHARED contract, byte-identical to the
    # one the Claude Agent SDK transport sends.
    assert system["content"] == DREAM_AGENT_SYSTEM_PROMPT
    assert "Return only strict JSON with keys approved, summary, and details." in system["content"]
    assert "no markdown fences" in system["content"] or "code fences" in system["content"]
    assert user["role"] == "user"
    # The user turn is the shared, transport-agnostic prompt builder's output —
    # not a re-specified provider-local prompt.
    assert user["content"] == build_decision_prompt(request)
    assert json.loads(user["content"])["decision"]["type"] == "formation_episode_selected"


async def test_fails_fast_on_missing_or_blank_api_key_env_naming_only_the_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def must_not_be_called(request, timeout):  # pragma: no cover - guard
        raise AssertionError("no network request may happen without a key")

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", must_not_be_called)
    monkeypatch.delenv("STUB_DREAM_AGENT_MISSING", raising=False)
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_MISSING")

    with pytest.raises(ValueError) as missing:
        await transport.decide(decision_request())
    assert str(missing.value) == "missing required environment variable: STUB_DREAM_AGENT_MISSING"

    monkeypatch.setenv("STUB_DREAM_AGENT_MISSING", "   ")
    with pytest.raises(ValueError, match="STUB_DREAM_AGENT_MISSING"):
        await transport.decide(decision_request())


async def test_provider_error_body_is_surfaced_without_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raise_urlopen(
        monkeypatch,
        lambda: http_error(401, "LiteLLM Virtual Key expected to start with 'sk-'"),
    )
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-super-secret-value")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    with pytest.raises(ValueError) as exc:
        await transport.decide(decision_request())
    message = str(exc.value)
    assert "HTTP 401" in message
    assert "LiteLLM Virtual Key" in message
    assert "sk-super-secret-value" not in message


def test_constructor_validation() -> None:
    with pytest.raises(ValueError, match="model cannot be blank"):
        OpenAICompatibleDreamAgentTransport(model="   ")
    with pytest.raises(ValueError, match="base_url cannot be blank"):
        OpenAICompatibleDreamAgentTransport(model="m", base_url="")
    with pytest.raises(ValueError, match="api_key_env cannot be blank"):
        OpenAICompatibleDreamAgentTransport(model="m", api_key_env=" ")
    with pytest.raises(ValueError, match="api_key cannot be blank"):
        OpenAICompatibleDreamAgentTransport(model="m", api_key="  ")
    with pytest.raises(ValueError, match="timeout_seconds"):
        OpenAICompatibleDreamAgentTransport(model="m", timeout_seconds=0.0)
    with pytest.raises(ValueError, match="max_output_tokens"):
        OpenAICompatibleDreamAgentTransport(model="m", max_output_tokens=0)


# ---------------------------------------------------------------------------
# Decision parsing through the shared parse_decision
# ---------------------------------------------------------------------------


async def test_approve_decision_parses_through_the_shared_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    content = json.dumps(
        {
            "approved": True,
            "summary": "  Approved: the episode is in scope and auditable.  ",
            "details": {"risk": "low", "context_facts_reviewed": 1},
        }
    )
    stub_urlopen(monkeypatch, content)
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    decision = await transport.decide(decision_request())

    assert decision.approved is True
    assert decision.summary == "Approved: the episode is in scope and auditable."
    assert decision.details == {"risk": "low", "context_facts_reviewed": 1}
    # Identical to routing the same body through the shared parser directly.
    assert decision == parse_decision(content, source="LLM dream-agent")


async def test_reject_decision_parses_through_the_shared_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_urlopen(
        monkeypatch,
        json.dumps(
            {
                "approved": False,
                "summary": "Rejected: the proposed prune would drop a pinned requirement.",
                "details": {"reason": "pinned_requirement"},
            }
        ),
    )
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    decision = await transport.decide(decision_request("pruning_relationship_pruned"))

    assert decision.approved is False
    assert decision.summary.startswith("Rejected:")
    assert decision.details == {"reason": "pinned_requirement"}


async def test_markdown_fenced_json_is_accepted_like_the_sdk_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_urlopen(
        monkeypatch,
        '```json\n{"approved": true, "summary": "Approved.", "details": {}}\n```',
    )
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    decision = await transport.decide(decision_request())
    assert decision.approved is True


@pytest.mark.parametrize(
    "content",
    [
        "",
        "   ",
        "I approve this action.",
        '{"summary": "no approved key", "details": {}}',
        '{"approved": "yes", "summary": "not a bool", "details": {}}',
        '{"approved": true, "summary": "   ", "details": {}}',
        '{"approved": true, "summary": "fine", "details": "not an object"}',
        '["approved"]',
    ],
)
async def test_out_of_contract_model_output_is_a_transport_error_not_a_silent_approval(
    monkeypatch: pytest.MonkeyPatch, content: str
) -> None:
    stub_urlopen(monkeypatch, content)
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    with pytest.raises(ValueError, match="LLM dream-agent"):
        await transport.decide(decision_request())


async def test_malformed_provider_envelope_is_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")

    def not_json(request, timeout):
        return _FakeResponse(b"<html>gateway timeout</html>")

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", not_json)
    with pytest.raises(ValueError, match="LLM dream-agent response was not valid JSON"):
        await transport.decide(decision_request())

    def no_choices(request, timeout):
        return _FakeResponse(json.dumps({"error": {"message": "bad request"}}).encode("utf-8"))

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", no_choices)
    with pytest.raises(ValueError, match=r"choices\[0\]\.message\.content"):
        await transport.decide(decision_request())


def test_only_one_llm_dream_agent_transport_exists() -> None:
    """One decision contract, one LLM transport, no Anthropic-native path."""
    assert not hasattr(agents_module, "ClaudeAgentDreamTransport")
    assert "_decision_prompt" not in vars(OpenAICompatibleDreamAgentTransport)
    assert "_parse_decision" not in vars(OpenAICompatibleDreamAgentTransport)
    assert agents_module.build_decision_prompt is build_decision_prompt
    assert agents_module.parse_decision is parse_decision


# ---------------------------------------------------------------------------
# runtime.py wiring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["openai", "litellm"])
def test_openai_compatible_credentials_now_yield_a_dream_agent_transport(provider: str) -> None:
    extraction, dream_agent = build_transports_from_credentials(
        provider=provider,
        api_key="sk-tenant-key",
        base_url="https://gateway.example.com/v1",
        model="gpt-4o",
    )
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    # Previously None: the dream agent was the one component that could not
    # ride the OpenAI-compatible path.
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert dream_agent.model == extraction.model == "gpt-4o"
    assert dream_agent.base_url == extraction.base_url == "https://gateway.example.com/v1"
    # Same sealed tenant credential powers both.
    assert dream_agent.api_key == extraction.api_key == "sk-tenant-key"


def test_anthropic_credentials_fail_fast_with_migration_guidance() -> None:
    """The Anthropic-native path is retired: a legacy sealed credential must
    fail loudly and name its replacement, never silently build a transport."""
    with pytest.raises(ValueError, match="provider 'anthropic' is retired"):
        build_transports_from_credentials(provider="anthropic", api_key="sk-ant-key", model="claude-sonnet-4-6")
    with pytest.raises(ValueError, match="provider 'anthropic' is retired"):
        build_synthesis_transport_from_credentials(
            {"provider": "anthropic", "api_key": "sk-ant-key", "model": "claude-sonnet-4-6"}
        )


def test_litellm_without_base_url_defaults_to_the_gateway() -> None:
    extraction, dream_agent = build_transports_from_credentials(provider="litellm", api_key="sk-tenant-key")
    assert extraction.base_url == DEFAULT_GATEWAY_BASE_URL
    assert dream_agent.base_url == DEFAULT_GATEWAY_BASE_URL
    assert extraction.model == dream_agent.model == DEFAULT_GATEWAY_MODEL


def test_env_wiring_matrix(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    for name in (
        "ANTHROPIC_API_KEY",
        "LITELLM_API_KEY",
        "LITELLM_API_BASE",
        "OPENAI_API_KEY",
        "MEMOTRON_LLM_MODEL",
        "MEMOTRON_DREAM_AGENT_MODEL",
        "OPENAI_API_URL",
    ):
        monkeypatch.delenv(name, raising=False)

    # Nothing configured: unchanged — no dream-agent transport, so the engine
    # keeps its deterministic LocalDreamAgentTransport default.
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, RuleBasedExtractionTransport)
    assert dream_agent is None

    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("OPENAI_API_URL", "https://openai-compatible.example.com/v1")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert dream_agent.base_url == "https://openai-compatible.example.com/v1"
    assert dream_agent.api_key_env == "OPENAI_API_KEY"
    assert dream_agent.model == "gpt-4o-mini"

    monkeypatch.setenv("MEMOTRON_DREAM_AGENT_MODEL", "gateway/decision-model")
    _, dream_agent = build_transports_from_env()
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert dream_agent.model == "gateway/decision-model"
    monkeypatch.delenv("MEMOTRON_DREAM_AGENT_MODEL")

    # The gateway key WINS over OPENAI_API_KEY, and defaults to the packaged
    # gateway base URL and undated model alias.
    monkeypatch.setenv("LITELLM_API_KEY", "sk-gateway")
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, OpenAICompatibleExtractionTransport)
    assert isinstance(dream_agent, OpenAICompatibleDreamAgentTransport)
    assert extraction.api_key_env == dream_agent.api_key_env == "LITELLM_API_KEY"
    assert extraction.base_url == dream_agent.base_url == DEFAULT_GATEWAY_BASE_URL
    assert extraction.model == dream_agent.model == DEFAULT_GATEWAY_MODEL

    monkeypatch.setenv("LITELLM_API_BASE", "https://latest.jedai-gateway.example.com/v1")
    monkeypatch.setenv("MEMOTRON_LLM_MODEL", "claude-sonnet-4-6")
    extraction, dream_agent = build_transports_from_env()
    assert extraction.base_url == "https://latest.jedai-gateway.example.com/v1"
    assert extraction.model == dream_agent.model == "claude-sonnet-4-6"

    # An Anthropic key in the environment is inert — it can never select a
    # transport, because no Anthropic-native transport exists.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    anthropic_extraction, anthropic_dream_agent = build_transports_from_env()
    assert isinstance(anthropic_extraction, OpenAICompatibleExtractionTransport)
    assert isinstance(anthropic_dream_agent, OpenAICompatibleDreamAgentTransport)
    assert anthropic_extraction.api_key_env == "LITELLM_API_KEY"

    monkeypatch.delenv("LITELLM_API_KEY")
    monkeypatch.delenv("OPENAI_API_KEY")
    # ANTHROPIC_API_KEY alone now means deterministic rule-based extraction.
    extraction, dream_agent = build_transports_from_env()
    assert isinstance(extraction, RuleBasedExtractionTransport)
    assert dream_agent is None


# ---------------------------------------------------------------------------
# Fail-safe: a transport failure never takes down a dreaming run
# ---------------------------------------------------------------------------


class RaisingDreamAgentTransport:
    """Reproduces the production failure: a bare Exception out of ``decide``."""

    MESSAGE = "Claude Code returned an error result: success"

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, request: DreamAgentDecisionRequest):
        self.calls += 1
        # A fresh instance per call, exactly as the SDK would raise.
        raise Exception(self.MESSAGE)


def dream_agent_config() -> DreamAgentConfig:
    return DreamAgentConfig(
        agent_id="truth-curator",
        name="Truth Curator",
        scope=AGENT_SCOPE,
        decision_policy="Approve only auditable memory maintenance decisions.",
    )


def formation_and_pruning_config() -> DreamConfig:
    base = default_config()
    agent = dream_agent_config()
    return base.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="formation-agent",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    agent=agent,
                ),
                DreamJob(
                    name="pruning-agent",
                    kind=DreamJobKind.PRUNING,
                    cadence_seconds=1,
                    agent=agent,
                ),
            ),
            "pruning": PruningPolicy(min_confidence=0.95),
        }
    )


async def seed_low_confidence_episode(client: Memotron) -> None:
    await client.add_episode(
        name="low-confidence-preference",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=speculative answers; "
            "relationship_type=PREFERS; confidence=0.5"
        ),
        source=EpisodeType.MESSAGE,
        scope=USER_SCOPE,
        reference_time=RUN_TIME,
    )


def transport_failure_receipts(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.DREAM_AGENT_TRANSPORT_FAILED
    ]


async def test_dreaming_run_completes_when_the_dream_agent_transport_raises(
    tmp_path: Path,
) -> None:
    """Regression for the production failure in ``.memotron/spymaster.log``.

    Before WS-24 the bare ``Exception`` raised inside the Claude Agent SDK
    escaped ``_decide`` and aborted the whole maintenance cycle.  The run must
    now COMPLETE, do its formation and pruning work, and leave an audit trail
    naming the failure.
    """
    transport = RaisingDreamAgentTransport()
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=formation_and_pruning_config(),
        dream_agent_transport=transport,
    )
    await seed_low_confidence_episode(client)

    result = await client.run_due_dreams(now=RUN_TIME)

    # The run finished, both jobs ran, and the decisions were made.
    assert {run.job_name for run in result.job_runs} == {"formation-agent", "pruning-agent"}
    assert {run.job_name: run.decision_count for run in result.job_runs} == {
        "formation-agent": 1,
        "pruning-agent": 1,
    }
    assert transport.calls == 2

    # Formation approved (creation is additive) so the memory exists; the PRUNE
    # did NOT happen (WS-24: a fallback may approve creation, never removal).
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    assert relationships
    assert relationships[0]["properties"]["status"] == RelationshipStatus.ACTIVE.value

    # Not a silent swallow: every decision record names its fallback, and the
    # two decision types take OPPOSITE fallbacks in the same run.
    decisions = {
        decision.decision_type: decision
        for decision in await client.dream_decisions(limit=10, agent_id="truth-curator")
    }
    assert set(decisions) == {"formation_episode_selected", "pruning_relationship_pruned"}
    formation = decisions["formation_episode_selected"]
    assert formation.details["approved"] is True
    assert formation.details["transport"] == "fallback-local"
    assert formation.details["dream_agent_fallback"] == "deterministic_approve"
    pruning = decisions["pruning_relationship_pruned"]
    assert pruning.details["approved"] is False
    assert pruning.details["transport"] == "fallback-fail-closed"
    assert pruning.details["dream_agent_fallback"] == "fail_closed_reject"
    assert "NOT approved" in pruning.summary
    for decision in decisions.values():
        assert decision.details["dream_agent_transport_error"] == "Exception"
        assert decision.details["dream_agent_transport"] == "RaisingDreamAgentTransport"
        assert decision.details["dream_agent_failure_kind"] == "transport"
        assert "could not reach the dream-agent transport" in decision.summary

    # And receipted on the run's hash chain with the provider's own reason.
    receipts = transport_failure_receipts(client, USER_SCOPE)
    assert len(receipts) == 2
    by_type = {json.loads(receipt.event_payload)["dream_agent_decision_type"]: receipt for receipt in receipts}
    assert set(by_type) == {"formation_episode_selected", "pruning_relationship_pruned"}
    for receipt in receipts:
        assert receipt.decision_reason.startswith("dream_agent_transport_error:")
        assert "Claude Code returned an error result: success" in receipt.decision_reason
        payload = json.loads(receipt.event_payload)
        assert payload["dream_agent_failure_kind"] == "transport"
        assert payload["dream_agent_transport_error_class"] == "Exception"
    assert by_type["formation_episode_selected"].decision_result == "recorded"
    assert (
        json.loads(by_type["formation_episode_selected"].event_payload)["dream_agent_fallback"]
        == "deterministic_approve"
    )
    assert by_type["pruning_relationship_pruned"].decision_result == "gated"
    assert (
        json.loads(by_type["pruning_relationship_pruned"].event_payload)["dream_agent_fallback"] == "fail_closed_reject"
    )


@pytest.mark.parametrize(
    ("make_exc", "expected_reason_fragment"),
    [
        (
            lambda: http_error(500, '{"error":"upstream model unavailable"}'),
            "HTTP 500",
        ),
        (lambda: URLError("Connection refused"), "Connection refused"),
    ],
    ids=["http_500", "connection_error"],
)
async def test_real_transport_http_and_connection_failures_take_the_fail_safe_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_exc,
    expected_reason_fragment: str,
) -> None:
    """A 500 and a connection error go through the REAL transport, not a stub."""
    raise_urlopen(monkeypatch, make_exc)
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=formation_and_pruning_config(),
        dream_agent_transport=OpenAICompatibleDreamAgentTransport(
            model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY"
        ),
    )
    await seed_low_confidence_episode(client)

    result = await client.run_due_dreams(now=RUN_TIME)

    assert {run.job_name: run.decision_count for run in result.job_runs} == {
        "formation-agent": 1,
        "pruning-agent": 1,
    }
    receipts = transport_failure_receipts(client, USER_SCOPE)
    assert len(receipts) == 2
    for receipt in receipts:
        payload = json.loads(receipt.event_payload)
        assert payload["dream_agent_transport_error_class"] == "ValueError"
        assert payload["dream_agent_transport"] == "OpenAICompatibleDreamAgentTransport"
        # A real network failure is a TRANSPORT failure, not a contract failure.
        assert payload["dream_agent_failure_kind"] == "transport"
        assert receipt.decision_reason.startswith("dream_agent_transport_error:")
        assert expected_reason_fragment in receipt.decision_reason
    decisions = {
        decision.decision_type: decision
        for decision in await client.dream_decisions(limit=10, agent_id="truth-curator")
    }
    assert decisions["formation_episode_selected"].details["dream_agent_fallback"] == "deterministic_approve"
    assert decisions["pruning_relationship_pruned"].details["dream_agent_fallback"] == "fail_closed_reject"


async def test_malformed_model_output_takes_the_fail_safe_path_in_a_real_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prose with no JSON in it degrades to the fallback, it does not raise out.

    It is receipted as a DECISION-CONTRACT failure, not a transport failure:
    the model answered, the answer was unreadable.  That is a prompt/model
    problem and an operator must be able to tell it from an outage.
    """
    stub_urlopen(monkeypatch, "Sure! I think you should approve this.")
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=formation_and_pruning_config(),
        dream_agent_transport=OpenAICompatibleDreamAgentTransport(
            model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY"
        ),
    )
    await seed_low_confidence_episode(client)

    result = await client.run_due_dreams(now=RUN_TIME)

    assert sum(run.decision_count for run in result.job_runs) == 2
    receipts = transport_failure_receipts(client, USER_SCOPE)
    assert len(receipts) == 2
    for receipt in receipts:
        assert "dream-agent decision was not valid JSON" in receipt.decision_reason
        assert receipt.decision_reason.startswith("dream_agent_decision_contract_error:")
        assert json.loads(receipt.event_payload)["dream_agent_failure_kind"] == "decision_contract"
    # An unreadable answer may still approve creation, never removal: the
    # formed memory survives, the prune it could not authorise did not run.
    relationships = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]
    assert relationships[0]["properties"]["status"] == RelationshipStatus.ACTIVE.value
    decisions = {
        decision.decision_type: decision
        for decision in await client.dream_decisions(limit=10, agent_id="truth-curator")
    }
    assert "returned an unreadable dream-agent decision" in decisions["pruning_relationship_pruned"].summary


async def test_transport_failure_is_logged_with_the_reason(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=formation_and_pruning_config(),
        dream_agent_transport=RaisingDreamAgentTransport(),
    )
    await seed_low_confidence_episode(client)

    with caplog.at_level("WARNING", logger="memotron.dreaming"):
        await client.run_due_dreams(now=RUN_TIME)

    warnings = [record for record in caplog.records if record.levelname == "WARNING"]
    assert warnings
    assert any("dream-agent transport" in record.getMessage() for record in warnings)
    assert any(
        record.exc_info is not None and "Claude Code returned an error result" in str(record.exc_info[1])
        for record in warnings
    )


# ---------------------------------------------------------------------------
# Fail-CLOSED: the untrusted-directive gate must not be laundered by an outage
# ---------------------------------------------------------------------------


def untrusted_gate_config() -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Remember lessons that improve agent behaviour.",
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                agent=dream_agent_config(),
            ),
        ),
        require_dream_agent_approval_for_untrusted_directives=True,
    )


async def test_untrusted_directive_gate_fails_closed_on_transport_failure(
    tmp_path: Path,
) -> None:
    """A model outage may not approve what the operator required an agent to approve."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="gate-test")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=untrusted_gate_config(),
        dream_agent_transport=RaisingDreamAgentTransport(),
    )
    await client.add_episode(
        name="external-directive-episode",
        episode_body=(
            "Memory: subject=agent-7; predicate=should always escalate; "
            "object=compliance issues to legal team; relationship_type=SHOULD; confidence=0.9"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="external-source",
        trusted=False,
    )

    # The run still completes.
    result = await client.run_dream_job(job_name="formation-default")
    assert len(result.job_runs) == 1

    gate_decisions = [
        decision
        for decision in await client.dream_decisions(limit=100)
        if decision.decision_type == "formation_untrusted_write_gated"
    ]
    assert gate_decisions
    for decision in gate_decisions:
        assert decision.details["approved"] is False
        assert decision.details["transport"] == "fallback-fail-closed"
        assert decision.details["dream_agent_fallback"] == "fail_closed_reject"
        assert "NOT approved" in decision.summary

    # The untrusted directive did NOT become memory.
    should_rows = [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "SHOULD"
    ]
    assert should_rows == []

    gate_receipts = [
        receipt
        for receipt in transport_failure_receipts(client, scope)
        if json.loads(receipt.event_payload)["dream_agent_decision_type"] == "formation_untrusted_write_gated"
    ]
    assert gate_receipts
    for receipt in gate_receipts:
        assert receipt.decision_result == "gated"
        assert json.loads(receipt.event_payload)["dream_agent_fallback"] == "fail_closed_reject"

    # The episode-selection decision in the SAME run took the ordinary
    # deterministic fallback — fail-closed is scoped to the gate, not global.
    selection_receipts = [
        receipt
        for receipt in transport_failure_receipts(client, scope)
        if json.loads(receipt.event_payload)["dream_agent_decision_type"] == "formation_episode_selected"
    ]
    assert selection_receipts
    assert all(receipt.decision_result == "recorded" for receipt in selection_receipts)


# ---------------------------------------------------------------------------
# The failure reason is content, so a sealed scope must not keep it in the clear
# ---------------------------------------------------------------------------


def crypto_shred_config() -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind", "role"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Remember stable preferences.",
            ),
        ),
    )
    return DreamConfig(
        instruction_sets=(instructions,),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                agent=dream_agent_config(),
            ),
        ),
        governance=GovernancePolicy(
            pii_sensitivity=PiiSensitivity.NONE,
            erasure_behavior=ErasureBehavior.CRYPTO_SHRED,
        ),
    )


async def test_transport_failure_reason_is_sealed_in_a_crypto_shred_scope(
    tmp_path: Path,
) -> None:
    """The reason folds an uncontrolled provider message, so it must not survive a shred.

    The ledger's content-free choke point diverts it into the sealed payload —
    the same protection the rollup-synthesis transport-error reason gets — so the
    erasure certificate still issues with zero violations.
    """
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="sealed-dream-agent")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=crypto_shred_config(),
        dream_agent_transport=RaisingDreamAgentTransport(),
    )
    client.provision_governance_key(scope=scope)
    await client.add_episode(
        name="sealed-preference",
        episode_body=(
            "Memory: subject=Priya Sharma; predicate=prefers; object=window seating; "
            "relationship_type=PREFERS; confidence=0.93"
        ),
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="sealed test",
    )

    await client.run_dream_job(job_name="formation-default")

    receipts = transport_failure_receipts(client, scope)
    assert receipts
    for receipt in receipts:
        # The provider's message is NOT in the plaintext lineage column.
        assert receipt.decision_reason == "dream_agent_transport_failed:reason_withheld"
        assert RaisingDreamAgentTransport.MESSAGE not in receipt.decision_reason
        assert receipt.sensitive_payload_encrypted is True
        assert is_sealed_content(receipt.sensitive_payload)

    await client.crypto_shred(scope=scope)
    certificate = await client.erasure_certificate(scope=scope)
    assert certificate.sweep.violations == ()
    assert await client.verify_erasure(scope=scope, certificate=certificate) is True


def test_no_anthropic_native_transport_can_ever_be_selected() -> None:
    """The Anthropic-native path is gone, not demoted.

    No module ships a transport that talks to api.anthropic.com, the package
    exports none, and the credential vocabulary rejects the provider outright —
    so no default, precedence order, or caller can reach one.
    """
    import memotron
    from memotron import extraction as extraction_module
    from memotron import synthesis as synthesis_module
    from memotron.graph import PropertyGraphStore

    for name in (
        "AnthropicExtractionTransport",
        "AnthropicSynthesisTransport",
        "ClaudeAgentDreamTransport",
    ):
        assert not hasattr(memotron, name), name
        assert name not in memotron.__all__, name
    assert not hasattr(extraction_module, "AnthropicExtractionTransport")
    assert not hasattr(synthesis_module, "AnthropicSynthesisTransport")

    source_root = Path(memotron.__file__).parent
    offenders = [
        path.name
        for path in sorted(source_root.rglob("*.py"))
        if "api.anthropic.com" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    assert PropertyGraphStore._normalize_llm_provider(None, "litellm") == "litellm"
    with pytest.raises(ValueError, match="provider 'anthropic' is retired"):
        PropertyGraphStore._normalize_llm_provider(None, "anthropic")


# ---------------------------------------------------------------------------
# WS-24: tolerant decision parsing (fences + trailing prose)
# ---------------------------------------------------------------------------
#
# The gateway proxies Claude models through LiteLLM with ``drop_params: true``,
# which silently discards ``response_format={"type":"json_object"}``.  The model
# therefore answers with fenced JSON and sometimes a sentence after the closing
# fence.  ``parse_decision`` used bare ``json.loads`` while the extraction path
# already used the tolerant reader, so those answers raised (observed live as
# ``Expecting ',' delimiter``), were receipted as transport failures, and were
# replaced by a fallback — which for most decision types meant an APPROVAL.
# An unparseable decision silently becoming an approval is the bug; these pin
# the fix at the parser and at the run.


DECISION_BODY = {
    "approved": False,
    "summary": "Rejected: the proposed prune would drop a corroborated requirement.",
    "details": {"reason": "corroborated_requirement"},
}

TOLERATED_ENVELOPES = {
    # The live repro: closing fence present, prose AFTER it.  The fence stripper
    # alone cannot remove this, because the last line is not the fence.
    "fenced_with_trailing_prose": (
        "```json\n" + json.dumps(DECISION_BODY) + "\n```\n" + "Note: I based this on the context facts you provided."
    ),
    # Fence opened and never closed (a truncated or sloppy formatter).
    "unclosed_fence": "```json\n" + json.dumps(DECISION_BODY),
    # No fence at all, prose appended despite the instruction not to.
    "bare_json_with_trailing_prose": (json.dumps(DECISION_BODY) + "\n\nLet me know if you want me to reconsider."),
    # Leading prose before the object.
    "leading_prose": "Here is my decision:\n" + json.dumps(DECISION_BODY),
}


@pytest.mark.parametrize("envelope", sorted(TOLERATED_ENVELOPES))
def test_decision_envelopes_the_gateway_actually_produces_parse(envelope: str) -> None:
    decision = parse_decision(TOLERATED_ENVELOPES[envelope], source="LLM dream-agent")
    assert decision.approved is False
    assert decision.summary == DECISION_BODY["summary"]
    assert decision.details == DECISION_BODY["details"]


@pytest.mark.parametrize("envelope", sorted(TOLERATED_ENVELOPES))
async def test_tolerated_envelopes_parse_through_the_real_transport(
    monkeypatch: pytest.MonkeyPatch, envelope: str
) -> None:
    stub_urlopen(monkeypatch, TOLERATED_ENVELOPES[envelope])
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    transport = OpenAICompatibleDreamAgentTransport(model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY")
    decision = await transport.decide(decision_request("pruning_relationship_pruned"))
    assert decision.approved is False


def test_envelope_tolerance_never_relaxes_the_decision_contract() -> None:
    """Tolerant about fences and prose; still strict about what a decision IS."""
    for content in (
        '```json\n{"summary": "no approved key"}\n```\ntrailing prose',
        '```json\n{"approved": "yes", "summary": "not a bool"}\n```',
        '{"approved": true, "summary": "   "} and some prose',
        "Here is my answer: I approve.",
    ):
        with pytest.raises(agents_module.DreamAgentContractError):
            parse_decision(content, source="LLM dream-agent")


async def test_a_fenced_rejection_rejects_instead_of_becoming_a_fallback_approval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The end-to-end regression: fenced JSON + trailing prose used to fail OPEN.

    The model rejected the prune.  Strict parsing turned that rejection into a
    ``DREAM_AGENT_TRANSPORT_FAILED`` receipt and a ``deterministic_approve``
    fallback — i.e. the row was pruned anyway.  The decision must now be the
    model's own, with no fallback receipt at all.
    """
    stub_urlopen(monkeypatch, TOLERATED_ENVELOPES["fenced_with_trailing_prose"])
    monkeypatch.setenv("STUB_DREAM_AGENT_KEY", "sk-test")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=formation_and_pruning_config(),
        dream_agent_transport=OpenAICompatibleDreamAgentTransport(
            model="gpt-4o-mini", api_key_env="STUB_DREAM_AGENT_KEY"
        ),
    )
    await seed_low_confidence_episode(client)

    await client.run_due_dreams(now=RUN_TIME)

    # No fallback anywhere: the answers were readable.
    assert transport_failure_receipts(client, USER_SCOPE) == []
    decisions = await client.dream_decisions(limit=10, agent_id="truth-curator")
    assert decisions
    for decision in decisions:
        assert decision.details["approved"] is False
        assert "dream_agent_fallback" not in decision.details
        assert decision.details["reason"] == "corroborated_requirement"
    # The model said no to forming AND to pruning, and both were honoured.
    assert [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ] == []


# ---------------------------------------------------------------------------
# WS-24: the fail-open / fail-closed split, per decision type
# ---------------------------------------------------------------------------


def full_cycle_config() -> DreamConfig:
    """Formation + rollup consolidation + pruning, all agent-gated."""
    base = default_config()
    agent = dream_agent_config()
    return base.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="formation-agent",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    agent=agent,
                ),
                DreamJob(
                    name="consolidation-agent",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    agent=agent,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.0, min_cluster_size=3, max_depth=1
                    ),
                ),
                DreamJob(
                    name="pruning-agent",
                    kind=DreamJobKind.PRUNING,
                    cadence_seconds=1,
                    agent=agent,
                ),
            ),
            "pruning": PruningPolicy(min_confidence=0.95),
        }
    )


async def test_creation_fails_open_and_removal_fails_closed_in_one_run(
    tmp_path: Path,
) -> None:
    """One outage, three decision points, two different answers — by AUTHORITY.

    Consolidation (creates a rollup, demotes members — additive and reversible)
    takes the deterministic approval.  Pruning (removes a fact from the active
    plane) is refused.  A fallback may approve creation, never removal.
    """
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="split")
    transport = RaisingDreamAgentTransport()
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=full_cycle_config(),
        dream_agent_transport=transport,
    )
    for subject in ("Priya", "Marco", "Elena"):
        await client.add_memory(
            subject=subject,
            predicate="prefers",
            object="window seating",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )

    await client.run_due_dreams(now=RUN_TIME)

    fallbacks = {
        decision.decision_type: decision.details["dream_agent_fallback"]
        for decision in await client.dream_decisions(limit=100, agent_id="truth-curator")
        if "dream_agent_fallback" in decision.details
    }
    assert fallbacks["consolidation_scope_selected"] == "deterministic_approve"
    assert fallbacks["pruning_relationship_pruned"] == "fail_closed_reject"

    relationships = client.export_graph()["relationships"]
    # Consolidation ran: the rollup exists and its members were demoted.
    rollups = [r for r in relationships if r["type"] == "ROLLUP"]
    assert len(rollups) == 1
    members = [r for r in relationships if r["type"] == "PREFERS"]
    assert len(members) == 3
    assert all(r["properties"]["active_in_context"] is False for r in members)
    # Pruning did not: every member is still an active, restorable row.
    assert all(r["properties"]["status"] == RelationshipStatus.ACTIVE.value for r in members)


async def test_every_decision_type_reaching_the_agent_is_explicitly_classified(
    tmp_path: Path,
) -> None:
    """Guard: a new agent-gated decision type cannot default into failing open.

    The split is a safety property, so it is enumerated, not inferred.  This
    exercises a full formation/consolidation/pruning cycle and asserts that
    every decision type that actually reached the transport appears in the
    documented classification below.
    """
    fail_open = {"formation_episode_selected", "consolidation_scope_selected"}
    fail_closed = {
        "formation_untrusted_write_gated",
        "pruning_relationship_pruned",
        "pruning_context_budget_exceeded",
        "pruning_single_active_repair",
    }
    assert frozenset(fail_closed) == dreaming_module._FAIL_CLOSED_DECISION_TYPES
    assert not (fail_open & fail_closed)

    seen: list[str] = []

    class RecordingTransport(LocalDreamAgentTransport):
        async def decide(self, request: DreamAgentDecisionRequest):
            seen.append(request.decision_type)
            return await super().decide(request)

    scope = MemoryScope(kind=ScopeKind.USER, scope_id="classified")
    client = Memotron(
        graph_path=tmp_path / "memotron.sqlite",
        config=full_cycle_config(),
        dream_agent_transport=RecordingTransport(),
    )
    for subject in ("Priya", "Marco", "Elena"):
        await client.add_memory(
            subject=subject,
            predicate="prefers",
            object="window seating",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )
    await client.add_episode(
        name="episode-for-formation",
        episode_body=(
            "Memory: subject=User 42; predicate=prefers; object=aisle seating; "
            "relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=scope,
        reference_time=RUN_TIME,
    )

    await client.run_due_dreams(now=RUN_TIME)

    assert seen, "the cycle must actually consult the dream agent"
    unclassified = set(seen) - fail_open - fail_closed
    assert unclassified == set(), (
        "these decision types reach the dream agent but are not classified "
        "fail-open or fail-closed in _FAIL_CLOSED_DECISION_TYPES: "
        f"{sorted(unclassified)}"
    )
