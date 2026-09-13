"""#251 units 7-8: the live chat transport, and the one strict-JSON exchange.

Unit 8's six cases are the whole contract of ``strict_json_call``, driven through
the REAL ``parse_first_json_object`` and the real pydantic validation rather than
through mocks of either:

============================  ==========================================
(a) clean JSON                parses
(b) fenced JSON               parses -- the gateway discards
                              ``response_format``, so fences are normal
(c) JSON then trailing prose  parses -- the reader is tolerant by design
(d) ``{"wrong": 1}``          rejected: valid JSON, wrong contract
(e) ``not json``              rejected: no JSON object at all
(f) a bare array              rejected -- see the footgun note below
============================  ==========================================

Each of d-f must emit **exactly one** reject receipt whose ``outputs_digest`` is
``sha256(raw)``. "Exactly one" is the assertion that matters: a rejection path
that receipted twice, or not at all, would make the receipt stream unusable as
the diagnosis surface it exists to be.

Unit 7 never touches the network. ``_post_with_retry`` is monkeypatched to a
recorder, so what is asserted is the request this transport BUILDS -- and the
integration case relies on ``tests/conftest.py:120-136`` having scrubbed
``LITELLM_API_KEY``, which is what makes "constructing succeeds, calling fails"
provable rather than dependent on the developer's shell.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from caveman_fakes import ScriptedChatTransport
from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.gateway import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    DEFAULT_TIMEOUT_SECONDS,
    CavemanChatTransport,
)
from memotron.caveman.llm import (
    CONTRACT_CLOSING_LINE,
    DETAIL_LIMIT,
    MAX_RECEIPTED_ERRORS,
    strict_json_call,
)
from memotron.caveman.models import ReceiptOp
from memotron.caveman.receipts import InMemoryReceipts, text_digest
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    DEFAULT_GATEWAY_RETRY_POLICY,
    GATEWAY_API_KEY_ENV,
    GatewayRetryPolicy,
)
from memotron.synthesis import SynthesisTransport

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"


class Answer(BaseModel):
    """A minimal stand-in for the four real response models."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    claims: tuple[str, ...]


# ============================================================ unit 7: transport


class _Recorder:
    """Captures the one call `_synthesize_sync` makes, and answers it."""

    def __init__(self, content: str = '{"claims": ["a"]}') -> None:
        self.path: str | None = None
        self.payload: dict[str, Any] = {}
        self.policy: GatewayRetryPolicy | None = None
        self.api_key: str | None = None
        self.calls = 0
        self._content = content

    def __call__(self, path: str, payload: dict[str, Any], *, policy: Any, api_key: str) -> str:
        self.path = path
        self.payload = payload
        self.policy = policy
        self.api_key = api_key
        self.calls += 1
        return json.dumps({"choices": [{"message": {"content": self._content}}]})


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace the retried POST. Nothing below this line reaches a socket."""
    rec = _Recorder()
    monkeypatch.setattr(
        CavemanChatTransport, "_post_with_retry", lambda _self, path, payload, **kw: rec(path, payload, **kw)
    )
    return rec


async def test_the_transport_posts_to_chat_completions(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("user text", system_prompt="system text")
    assert recorder.path == "/chat/completions"


async def test_the_transport_sends_the_4096_token_output_budget(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The synthesis transport's 300 would truncate a global dream pass to nothing."""
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert recorder.payload["max_tokens"] == 4096
    assert DEFAULT_MAX_OUTPUT_TOKENS == 4096


async def test_the_transport_sends_temperature_zero(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert recorder.payload["temperature"] == 0


async def test_the_transport_sends_system_first_then_user(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("user text", system_prompt="system text")
    messages = recorder.payload["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "system text"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "user text"


async def test_the_transport_passes_the_retry_policy_through(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retry is the reason this is not the synthesis transport."""
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert recorder.policy is DEFAULT_GATEWAY_RETRY_POLICY


async def test_a_custom_retry_policy_reaches_the_post(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    policy = GatewayRetryPolicy(max_attempts=7)
    await CavemanChatTransport(retry_policy=policy).synthesize("u", system_prompt="s")
    assert recorder.policy is policy


async def test_the_transport_resolves_the_key_before_posting(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-secret")
    await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert recorder.api_key == "sk-secret"


async def test_the_transport_makes_exactly_one_post_per_exchange(
    recorder: _Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One call per stage is the design's budget; retry lives below this line."""
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert recorder.calls == 1


async def test_the_transport_returns_the_message_content(recorder: _Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(GATEWAY_API_KEY_ENV, "sk-test")
    answer = await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert answer == '{"claims": ["a"]}'


def test_the_transport_defaults_target_the_gateway() -> None:
    transport = CavemanChatTransport()
    assert transport.model == DEFAULT_GATEWAY_MODEL
    assert transport.base_url == DEFAULT_GATEWAY_BASE_URL
    assert transport.api_key_env == GATEWAY_API_KEY_ENV
    assert transport.timeout_seconds == DEFAULT_TIMEOUT_SECONDS


def test_the_transport_identifier_names_the_transport_and_the_model() -> None:
    """A receipt naming only the model could not tell this from a 300-token summary."""
    assert CavemanChatTransport(model="claude-sonnet-4-6").identifier == "caveman-chat:claude-sonnet-4-6"


def test_a_zero_output_budget_is_refused() -> None:
    with pytest.raises(ValueError, match="max_output_tokens must be greater than zero"):
        CavemanChatTransport(max_output_tokens=0)


def test_the_transport_satisfies_the_synthesis_transport_protocol() -> None:
    """Reused, not redefined: `synthesis.SynthesisTransport` already has this shape."""
    for attribute in SynthesisTransport.__protocol_attrs__:
        assert hasattr(CavemanChatTransport, attribute), attribute
    assert inspect.signature(CavemanChatTransport.synthesize) == inspect.signature(SynthesisTransport.synthesize)


def test_constructing_the_transport_needs_no_key() -> None:
    """The key is read at CALL time, so a process that never calls never needs one."""
    assert CavemanChatTransport().identifier


async def test_synthesize_without_a_key_raises_naming_only_the_variable() -> None:
    """The integration case. `conftest` has already scrubbed the variable.

    The message must name the variable and never a value -- the rule the whole
    gateway package holds.
    """
    with pytest.raises(ValueError, match=f"missing required environment variable: {GATEWAY_API_KEY_ENV}") as caught:
        await CavemanChatTransport().synthesize("u", system_prompt="s")
    assert "sk-" not in str(caught.value)


# ========================================================= unit 8: strict JSON


async def _call(
    raw: str | Exception,
    *,
    receipts: InMemoryReceipts | None = None,
) -> tuple[Answer, InMemoryReceipts, ScriptedChatTransport]:
    sink = receipts or InMemoryReceipts()
    transport = ScriptedChatTransport([raw])
    answer = await strict_json_call(
        transport=transport,
        system_prompt="SYSTEM",
        user_prompt="USER",
        response_model=Answer,
        source="EXTRACT",
        receipts=sink,
        scope=SCOPE,
        subject="ep-251-01",
        reject_op=ReceiptOp.EXTRACT_REJECTED,
        now=NOW,
    )
    return answer, sink, transport


# -- the three that parse ------------------------------------------------------


async def test_a_clean_json_object_parses() -> None:
    answer, sink, _ = await _call('{"claims": ["one", "two"]}')
    assert answer.claims == ("one", "two")
    assert sink.all() == []


async def test_a_fenced_json_object_parses() -> None:
    """The normal case, not an edge case: the gateway's `drop_params` discards
    `response_format`, so models answer with fenced JSON."""
    answer, sink, _ = await _call('```json\n{"claims": ["one"]}\n```')
    assert answer.claims == ("one",)
    assert sink.all() == []


async def test_json_followed_by_trailing_prose_parses() -> None:
    """`parse_first_json_object` is tolerant by design; models append a sentence."""
    answer, sink, _ = await _call('{"claims": ["one"]}\n\nI hope that helps!')
    assert answer.claims == ("one",)
    assert sink.all() == []


async def test_a_fenced_object_with_prose_after_the_fence_parses() -> None:
    answer, _, _ = await _call('```\n{"claims": ["one"]}\n```\nLet me know if you need more.')
    assert answer.claims == ("one",)


async def test_a_passing_call_emits_no_receipt_at_all() -> None:
    """Acceptance receipts belong to the stage, which knows what it accepted."""
    _, sink, _ = await _call('{"claims": []}')
    assert sink.all() == []


# -- the three that reject -----------------------------------------------------


@pytest.mark.parametrize(
    ("label", "raw"),
    [
        ("wrong contract", '{"wrong": 1}'),
        ("not json", "not json"),
        ("bare array", '["one", "two"]'),
    ],
)
async def test_each_bad_response_raises_out_of_contract(label: str, raw: str) -> None:
    with pytest.raises(OutOfContractResponse) as caught:
        await _call(raw)
    assert caught.value.source == "EXTRACT"
    assert caught.value.errors, label


@pytest.mark.parametrize("raw", ['{"wrong": 1}', "not json", '["one", "two"]'])
async def test_each_bad_response_emits_exactly_one_reject_receipt(raw: str) -> None:
    """Exactly one. Twice, or not at all, makes the stream unusable as a diagnosis."""
    sink = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse):
        await _call(raw, receipts=sink)
    assert len(sink.all()) == 1
    assert sink.all()[0].op is ReceiptOp.EXTRACT_REJECTED


@pytest.mark.parametrize("raw", ['{"wrong": 1}', "not json", '["one", "two"]'])
async def test_a_reject_receipts_outputs_digest_is_sha256_of_the_raw_answer(raw: str) -> None:
    sink = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse):
        await _call(raw, receipts=sink)
    assert sink.all()[0].outputs_digest == text_digest(raw)


@pytest.mark.parametrize("raw", ['{"wrong": 1}', "not json", '["one", "two"]'])
async def test_the_exceptions_digest_is_the_first_16_of_the_receipts(raw: str) -> None:
    """So an exception in a log and a receipt in the stream name the same response."""
    sink = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse) as caught:
        await _call(raw, receipts=sink)
    assert caught.value.raw_digest == sink.all()[0].outputs_digest[:16]
    assert len(caught.value.raw_digest) == 16


async def test_a_bare_array_fails_naming_the_missing_field_not_a_parse_error() -> None:
    """The documented footgun, pinned so the docstring cannot go stale.

    `parse_first_json_object` WRAPS a bare array rather than rejecting it, so the
    error reads "claims: Field required" and not "did not contain a valid JSON
    object". Anyone reading that on a non-empty response should read it as "the
    model sent a bare array".
    """
    with pytest.raises(OutOfContractResponse) as caught:
        await _call('["one", "two"]')
    joined = " ".join(caught.value.errors)
    assert "claims" in joined
    assert "did not contain a valid JSON object" not in joined


async def test_unparseable_text_fails_with_the_readers_own_message() -> None:
    with pytest.raises(OutOfContractResponse) as caught:
        await _call("not json")
    assert "EXTRACT content did not contain a valid JSON object" in " ".join(caught.value.errors)


async def test_an_invented_field_is_a_rejection_not_a_dropped_key() -> None:
    """`extra="forbid"` is the fail-fast the stages depend on."""
    with pytest.raises(OutOfContractResponse) as caught:
        await _call('{"claims": ["one"], "confidence": 0.9}')
    assert "confidence" in " ".join(caught.value.errors)


async def test_a_reject_receipt_carries_at_most_three_errors() -> None:
    """Bounded so `detail` stays inside its 600-character ceiling."""
    sink = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse) as caught:
        await _call('{"a": 1, "b": 2, "c": 3, "d": 4, "e": 5}', receipts=sink)
    assert len(caught.value.errors) <= MAX_RECEIPTED_ERRORS
    assert len(sink.all()[0].detail) <= DETAIL_LIMIT


async def test_a_reject_receipt_does_not_carry_the_model_content() -> None:
    """Receipts are content-free by construction; pydantic's repr includes `input_value`."""
    sink = InMemoryReceipts()
    secret = "a-very-distinctive-string-the-model-said"
    with pytest.raises(OutOfContractResponse):
        await _call(f'{{"wrong": "{secret}"}}', receipts=sink)
    receipt = sink.all()[0]
    assert secret not in receipt.detail
    assert secret not in str(receipt)


async def test_a_reject_receipt_is_attributed_to_its_scope_and_subject() -> None:
    sink = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse):
        await _call("not json", receipts=sink)
    receipt = sink.all()[0]
    assert receipt.scope == SCOPE
    assert receipt.subject == "ep-251-01"
    assert receipt.ts == NOW


async def test_a_reject_receipts_inputs_digest_is_a_function_of_the_prompt() -> None:
    """So two rejections can be told apart by whether the prompt changed."""
    first = InMemoryReceipts()
    second = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse):
        await _call("not json", receipts=first)
    transport = ScriptedChatTransport(["not json"])
    with pytest.raises(OutOfContractResponse):
        await strict_json_call(
            transport=transport,
            system_prompt="A DIFFERENT SYSTEM PROMPT",
            user_prompt="USER",
            response_model=Answer,
            source="EXTRACT",
            receipts=second,
            scope=SCOPE,
            subject="ep-251-01",
            reject_op=ReceiptOp.EXTRACT_REJECTED,
            now=NOW,
        )
    assert first.all()[0].inputs_digest != second.all()[0].inputs_digest
    assert first.all()[0].outputs_digest == second.all()[0].outputs_digest


@pytest.mark.parametrize(
    "reject_op",
    [
        ReceiptOp.EXTRACT_REJECTED,
        ReceiptOp.RECONCILE_REJECTED,
        ReceiptOp.DREAM_NODE_REJECTED,
        ReceiptOp.DREAM_GLOBAL_REJECTED,
    ],
)
async def test_the_caller_chooses_the_reject_op(reject_op: ReceiptOp) -> None:
    """Four stages, four rejection ops, one exchange."""
    sink = InMemoryReceipts()
    transport = ScriptedChatTransport(["not json"])
    with pytest.raises(OutOfContractResponse):
        await strict_json_call(
            transport=transport,
            system_prompt="S",
            user_prompt="U",
            response_model=Answer,
            source="STAGE",
            receipts=sink,
            scope=SCOPE,
            subject="subject",
            reject_op=reject_op,
            now=NOW,
        )
    assert sink.all()[0].op is reject_op


# -- what must NOT happen ------------------------------------------------------


async def test_there_is_no_repair_pass() -> None:
    """One call, even on rejection.

    A second scripted response proves it: if `strict_json_call` re-prompted, the
    good answer would be consumed and the call would succeed. It must not.
    """
    sink = InMemoryReceipts()
    transport = ScriptedChatTransport(["not json", '{"claims": ["recovered"]}'])
    with pytest.raises(OutOfContractResponse):
        await strict_json_call(
            transport=transport,
            system_prompt="S",
            user_prompt="U",
            response_model=Answer,
            source="EXTRACT",
            receipts=sink,
            scope=SCOPE,
            subject="s",
            reject_op=ReceiptOp.EXTRACT_REJECTED,
            now=NOW,
        )
    assert transport.call_count == 1
    assert transport.remaining == 1


async def test_a_transport_failure_is_not_receipted_as_a_rejection() -> None:
    """ "The gateway was down" and "the prompt is wrong" are different facts.

    The transport contract says transport failures raise `ValueError`; the
    gateway layer has already exhausted its retries by then, so turning it into
    a rejection receipt would put an outage in the prompt-defect bucket.
    """
    sink = InMemoryReceipts()
    transport = ScriptedChatTransport([ValueError("caveman chat request failed: connection reset")])
    with pytest.raises(ValueError, match="connection reset"):
        await strict_json_call(
            transport=transport,
            system_prompt="S",
            user_prompt="U",
            response_model=Answer,
            source="EXTRACT",
            receipts=sink,
            scope=SCOPE,
            subject="s",
            reject_op=ReceiptOp.EXTRACT_REJECTED,
            now=NOW,
        )
    assert sink.all() == []


async def test_the_exchange_passes_both_prompts_through_unmodified() -> None:
    """`strict_json_call` renders nothing. Prompt assembly belongs to the stages."""
    _, _, transport = await _call('{"claims": []}')
    assert transport.last.system_prompt == "SYSTEM"
    assert transport.last.prompt == "USER"


def test_the_closing_line_is_shared_rather_than_paraphrased() -> None:
    """Four paraphrases of "answer with one JSON object" are four different requests."""
    assert CONTRACT_CLOSING_LINE == "Answer with one JSON object and nothing else."


# ================================================== unit 15: the scripted fake


def test_the_scripted_transport_satisfies_the_synthesis_transport_protocol() -> None:
    for attribute in SynthesisTransport.__protocol_attrs__:
        assert hasattr(ScriptedChatTransport, attribute), attribute
    assert inspect.signature(ScriptedChatTransport.synthesize) == inspect.signature(SynthesisTransport.synthesize)


async def test_the_scripted_transport_records_every_exchange() -> None:
    transport = ScriptedChatTransport(["one", "two"])
    await transport.synthesize("u1", system_prompt="s1")
    await transport.synthesize("u2", system_prompt="s2")
    assert transport.prompts() == ["u1", "u2"]
    assert transport.system_prompts() == ["s1", "s2"]
    assert transport.call_count == 2
    assert transport.remaining == 0


async def test_the_scripted_transport_answers_in_order() -> None:
    transport = ScriptedChatTransport(["first", "second"])
    assert await transport.synthesize("u", system_prompt="s") == "first"
    assert await transport.synthesize("u", system_prompt="s") == "second"


async def test_running_out_of_script_is_a_loud_failure() -> None:
    """An unplanned LLM call must fail, not return "" and pass.

    The whole design turns on call counts: one call for extract, one for the
    whole reconcile batch, one for the global pass, none for read.
    """
    transport = ScriptedChatTransport([])
    with pytest.raises(AssertionError, match="ran out of script"):
        await transport.synthesize("u", system_prompt="s")


async def test_running_out_of_script_still_records_the_call_that_did_it() -> None:
    """So the failure says which call was unplanned."""
    transport = ScriptedChatTransport([])
    with pytest.raises(AssertionError):
        await transport.synthesize("the unplanned prompt", system_prompt="s")
    assert transport.calls[-1].prompt == "the unplanned prompt"


async def test_assert_exhausted_catches_a_skipped_call() -> None:
    """The mirror image: two calls where three were scripted found a skipped node."""
    transport = ScriptedChatTransport(["one", "two"])
    await transport.synthesize("u", system_prompt="s")
    with pytest.raises(AssertionError, match="unused scripted response"):
        transport.assert_exhausted()


async def test_assert_exhausted_passes_when_the_script_was_consumed() -> None:
    transport = ScriptedChatTransport(["one"])
    await transport.synthesize("u", system_prompt="s")
    transport.assert_exhausted()


async def test_a_scripted_exception_is_raised_rather_than_returned() -> None:
    """How a stage's transport-failure handling is tested without a second fake."""
    transport = ScriptedChatTransport([ValueError("boom")])
    with pytest.raises(ValueError, match="boom"):
        await transport.synthesize("u", system_prompt="s")


def test_last_on_an_uncalled_transport_fails_rather_than_returning_none() -> None:
    with pytest.raises(AssertionError, match="never called"):
        _ = ScriptedChatTransport(["x"]).last
