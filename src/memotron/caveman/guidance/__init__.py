"""The two documents that tell an agent how to use this memory, shipped as package data.

``AGENTS.md`` is the contract -- what the memory is, the lifecycle, every tool
with its arguments, the rendering the agent will actually see, and what the node
ids are for. ``SKILL.md`` is the Claude Code skill that routes
``/caveman-memory`` to those tools.

.. rubric:: Why they are files rather than module constants

Because they have two readers each and only one of them is Python.
``AGENTS.md`` is what :func:`memotron.caveman.mcp.memory_contract` returns to
a calling agent AND what a human reviews in a diff; ``SKILL.md`` is loaded by
Claude Code from ``.claude/skills/caveman-memory/SKILL.md``, which is a byte
copy of this one (``test_caveman_guidance.py`` asserts the equality, so the copy
cannot drift). A Python string literal would make the review diff a diff of
escaped text and would make the skill copy a second authorship of the same
words.

.. rubric:: Why ``importlib.resources`` rather than ``Path(__file__).parent``

``files()`` is the API that keeps working when the package is not a directory on
disk -- a zip import, a frozen bundle -- and it is the one that does not depend
on ``__file__`` existing. ``[tool.hatch.build.targets.wheel] packages =
["src/memotron"]`` ships every file under the package directory, so these two
are in the wheel with no ``pyproject.toml`` change; ``test_caveman_guidance.py``
reads them back through this API so a packaging regression fails a test rather
than a deployment.

.. rubric:: Plain ASCII, checked

Both documents are printable ASCII, like every other LLM-facing string in this
package (see :mod:`memotron.caveman.render`). No middle dot, no arrow, no
multiplication sign, no dash that is not a hyphen: each is a token spent to
teach a convention, and this package lost live runs to exactly that. The test
asserts it rather than trusting it.
"""

from __future__ import annotations

from importlib.resources import files

AGENTS_FILENAME = "AGENTS.md"
"""The agent contract. What :func:`memotron.caveman.mcp.memory_contract` returns."""

SKILL_FILENAME = "SKILL.md"
"""The Claude Code skill. Copied byte for byte to ``.claude/skills/caveman-memory/``."""


def _read(filename: str) -> str:
    """Read one shipped document out of this package.

    Not cached. Both documents are a few kilobytes, every caller is a tool
    invocation or a test, and a cache would mean an editor's save during a
    development session did not reach a running server -- which is the one time
    the freshness matters.
    """
    return (files(__package__).joinpath(filename)).read_text(encoding="utf-8")


def agents_text() -> str:
    """``AGENTS.md`` verbatim: the whole contract an agent needs, in one string."""
    return _read(AGENTS_FILENAME)


def skill_text() -> str:
    """``SKILL.md`` verbatim, frontmatter included, as Claude Code loads it."""
    return _read(SKILL_FILENAME)
