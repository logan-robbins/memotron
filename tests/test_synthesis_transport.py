"""The synthesis transport had NO tests. Not thin ones -- none at all.

How that was found
------------------
Not by reading. Extracting the shared OpenAI transport base (2026-09-01) rewrote
``_synthesize_sync``, which pulled four long-untested lines into **diff-coverage's** view for
the first time and dropped that lane to 89%. Before the rewrite the lines were equally
uncovered and equally invisible: `cov-floor` measures a per-module percentage, `diff-cov`
measures only lines a diff touches, and code that is uncovered AND unedited is invisible to
both. `grep -rl OpenAICompatibleSynthesisTransport tests/` matched only the two golden files.

That is the same blind spot recorded in #149 for the storage engines, in a fifth place.

What is pinned here
-------------------
The four behaviours the base now provides on this transport's behalf, asserted through the
SYNTHESIS transport rather than the base, because the base's contract is that each caller keeps
its own wording:

* the request it actually puts on the wire -- URL, method, headers, and exact body
* ``error_label="LLM synthesis"`` reaching all three failure messages verbatim
* a missing key failing with the variable NAME and never reaching ``urlopen``
* ``max_output_tokens`` validation, which lives here and not in the base

What is deliberately NOT pinned here: the key-before-payload ORDERING. It is unobservable on
this transport and a break test proved it -- see
``test_a_missing_api_key_fails_before_reaching_the_wire``.

Monkeypatching targets ``gateway.urllib_request``, not ``synthesis``: after the extraction the
one ``urlopen`` lives in ``OpenAICompatibleTransport._open``. ``urllib.request`` is a single
shared module object so the reach is identical either way -- this just names the code that runs.
"""

from __future__ import annotations

import asyncio
import io
import json
import urllib.error
from typing import Self

import pytest

from memotron import gateway as gateway_module
from memotron.synthesis import OpenAICompatibleSynthesisTransport


class _FakeResponse(io.BytesIO):
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _transport(**overrides: object) -> OpenAICompatibleSynthesisTransport:
    kwargs: dict[str, object] = {
        "model": "gpt-4o-mini",
        "base_url": "https://gateway.example.com/v1",
        "api_key_env": "STUB_SYNTH_KEY",  # pragma: allowlist secret -- an env var NAME, not a key
        "timeout_seconds": 11.0,
    }
    kwargs.update(overrides)
    return OpenAICompatibleSynthesisTransport(**kwargs)  # type: ignore[arg-type]


def _content(text: str) -> bytes:
    return json.dumps({"choices": [{"message": {"content": text}}]}).encode("utf-8")


def test_the_request_it_puts_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Asserts the actual bytes, not that a call happened.

    A test that only checked the return value would pass with a wrong model, a missing
    Authorization header, or the system and user prompts swapped.
    """
    seen: dict[str, object] = {}

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        seen["url"] = request.full_url  # type: ignore[attr-defined]
        seen["method"] = request.get_method()  # type: ignore[attr-defined]
        seen["headers"] = dict(request.headers)  # type: ignore[attr-defined]
        seen["body"] = json.loads(request.data.decode("utf-8"))  # type: ignore[attr-defined]
        seen["timeout"] = timeout
        return _FakeResponse(_content("a synthesised rollup"))

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", fake_urlopen)
    monkeypatch.setenv("STUB_SYNTH_KEY", "sk-test-synth")

    transport = _transport(max_output_tokens=321)
    result = asyncio.run(transport.synthesize("the prompt", system_prompt="the system prompt"))

    assert result == "a synthesised rollup"
    assert transport.identifier == "openai-compatible:gpt-4o-mini"
    assert seen["url"] == "https://gateway.example.com/v1/chat/completions"
    assert seen["method"] == "POST"
    assert seen["timeout"] == 11.0
    # urllib title-cases header keys.
    assert seen["headers"]["Authorization"] == "Bearer sk-test-synth"
    assert seen["headers"]["Content-type"] == "application/json"
    assert seen["body"] == {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": "the system prompt"},
            {"role": "user", "content": "the prompt"},
        ],
        "max_tokens": 321,
        "temperature": 0,
        # Sent for documentation value even though the gateway's `drop_params: true`
        # discards it -- gateway.py:_chat_payload explains why callers still parse
        # the content tolerantly.
        "response_format": {"type": "json_object"},
    }


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        pytest.param(b"{not json", "LLM synthesis response was not valid JSON", id="malformed"),
        pytest.param(
            b'{"choices": []}', "LLM synthesis response did not contain choices\\[0\\].message.content", id="no-choices"
        ),
        pytest.param(
            json.dumps({"choices": [{"message": {"content": 17}}]}).encode("utf-8"),
            "LLM synthesis message content must be a string",
            id="non-string",
        ),
    ],
)
def test_every_failure_message_carries_this_transport_s_own_wording(
    monkeypatch: pytest.MonkeyPatch, response: bytes, expected: str
) -> None:
    """The base is shared; the WORDING is not, and that is the constraint it was built under.

    Each of the five transports has its own label, and tests elsewhere assert those verbatim.
    If the base ever unified the wording these three assertions are what fails.
    """
    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", lambda request, timeout=None: _FakeResponse(response))
    monkeypatch.setenv("STUB_SYNTH_KEY", "sk-test-synth")

    with pytest.raises(ValueError, match=expected):
        asyncio.run(_transport().synthesize("p", system_prompt="s"))


def test_a_transport_error_is_reported_as_a_synthesis_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request: object, timeout: float | None = None) -> _FakeResponse:
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(gateway_module.urllib_request, "urlopen", boom)
    monkeypatch.setenv("STUB_SYNTH_KEY", "sk-test-synth")

    with pytest.raises(ValueError, match="LLM synthesis request failed"):
        asyncio.run(_transport().synthesize("p", system_prompt="s"))


def test_a_missing_api_key_fails_before_reaching_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    """Named for what it actually proves, after a break test showed the first name was a lie.

    This was written as ``test_the_key_is_resolved_before_the_payload_is_built``, to pin the
    ordering the extraction reversed. **It could not fail.** Reversing the order in
    ``_synthesize_sync`` -- verified applied by exact-match assert, not assumed -- left all
    eight tests green, because synthesis's ``_chat_payload`` only reads attributes and builds a
    dict. It cannot raise, so nothing downstream of it is observable.

    The ordering is only testable where payload construction CAN fail: ``agents``, whose
    ``build_decision_prompt`` calls ``json.dumps`` on caller-supplied ``details``. That is where
    an ordering test belongs, and this is not it.

    What remains is still worth pinning: a missing key raises with the variable NAME, never a
    value, and never reaches ``urlopen``.
    """
    monkeypatch.delenv("STUB_SYNTH_KEY", raising=False)
    monkeypatch.setattr(
        gateway_module.urllib_request,
        "urlopen",
        lambda request, timeout=None: pytest.fail("must not reach the wire without a key"),
    )

    with pytest.raises(ValueError, match="missing required environment variable: STUB_SYNTH_KEY"):
        asyncio.run(_transport().synthesize("p", system_prompt="s"))


@pytest.mark.parametrize("bad", [0, -1])
def test_max_output_tokens_must_be_positive(bad: int) -> None:
    """Lives on this class, not the base -- each transport has its own default and its own check."""
    with pytest.raises(ValueError, match="max_output_tokens must be greater than zero"):
        _transport(max_output_tokens=bad)
