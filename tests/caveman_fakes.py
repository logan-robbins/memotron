"""#251 unit 15: the ONE scripted chat transport every caveman test uses.

This is a deliberate deviation from the repo's per-file-local-fake convention.
Four independently authored work packages have to script the *same* transport
contract, and one module is what stops four copies of it diverging — a divergent
fake is how two packages end up disagreeing about what a stage does without
either test failing.

**Written once in WP0 and frozen.** WP1-4 consume it and do not edit it. That is
why the surface below is a little wider than any single caller needs: a capability
missing at the point somebody needs it would force an edit to a frozen file, and
the alternative is a second fake, which is the thing this file exists to prevent.

``tests/`` is not a package, and pytest puts a conftest's own directory on
``sys.path``, so ``from caveman_fakes import ScriptedChatTransport`` resolves.
``tests/conftest.py:71-73`` already imports siblings this way.

.. rubric:: Why running out of script is an AssertionError

Because the alternative — returning ``""`` or looping the last response — makes an
unplanned LLM call *pass*. The whole design turns on call counts: extract is one
call, reconcile is one call for the whole batch, the global dream pass is one
call, and read makes none at all. A stage that quietly grew a second call would
be a real regression that no assertion would catch, so the fake fails loudly
instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class ScriptedCall:
    """One exchange as the transport saw it.

    Both halves are recorded because the tests that matter assert on the prompt:
    that reconcile's prompt contains neither the motive name nor the goal, that
    extract's contains the rubric and the episode's turn text, that a dream
    prompt was built from the entries since ``dreamed_at``.
    """

    system_prompt: str
    prompt: str


class ScriptedChatTransport:
    """A ``SynthesisTransport`` that answers from a fixed script and records calls.

    Satisfies ``memotron.synthesis.SynthesisTransport`` structurally:
    ``identifier`` plus ``async synthesize(prompt, *, system_prompt) -> str``.
    It is never registered against that Protocol at runtime — the Protocol is not
    ``runtime_checkable`` — so ``test_caveman_llm.py`` asserts conformance by
    comparing signatures.

    A scripted entry may be an ``Exception`` instead of a string, in which case
    it is raised. That is how a stage's handling of a transport-level failure
    (``ValueError`` per the transport contract) is tested without a second fake.

    Makes zero network calls and holds no credential. ``tests/conftest.py``
    scrubs ``LITELLM_API_KEY`` from every test, so nothing here could reach a
    gateway even by mistake.
    """

    def __init__(
        self,
        responses: Sequence[str | Exception],
        *,
        identifier: str = "scripted-chat:test",
    ) -> None:
        self._responses: list[str | Exception] = list(responses)
        self._identifier = identifier
        self.calls: list[ScriptedCall] = []

    @property
    def identifier(self) -> str:
        return self._identifier

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        """Record the exchange and answer with the next scripted response."""
        self.calls.append(ScriptedCall(system_prompt=system_prompt, prompt=prompt))
        if not self._responses:
            raise AssertionError(
                f"ScriptedChatTransport ran out of script on call {len(self.calls)}: "
                f"the code under test made an LLM call the test did not plan for. "
                f"System prompt began: {system_prompt[:120]!r}"
            )
        answer = self._responses.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    # -- what the tests assert on ------------------------------------------

    @property
    def call_count(self) -> int:
        """How many exchanges happened. The design's call budgets are per stage."""
        return len(self.calls)

    @property
    def remaining(self) -> int:
        """Scripted responses not yet consumed."""
        return len(self._responses)

    @property
    def last(self) -> ScriptedCall:
        """The most recent exchange, or raise if there was none."""
        if not self.calls:
            raise AssertionError("ScriptedChatTransport was never called")
        return self.calls[-1]

    def prompts(self) -> list[str]:
        """Just the user prompts, in call order."""
        return [call.prompt for call in self.calls]

    def system_prompts(self) -> list[str]:
        """Just the system prompts, in call order."""
        return [call.system_prompt for call in self.calls]

    def assert_exhausted(self) -> None:
        """Fail if any scripted response went unused.

        The mirror image of running out: a test that scripted three per-node
        dream responses and saw two calls has found a node that was silently
        skipped, which is exactly as much of a defect as an extra call.
        """
        if self._responses:
            raise AssertionError(
                f"ScriptedChatTransport has {self.remaining} unused scripted response(s) "
                f"after {self.call_count} call(s): the code under test made fewer LLM calls "
                f"than the test planned for."
            )
