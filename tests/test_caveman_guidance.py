"""#251 amendment D, package D-D: the two shipped guidance documents.

What these assert is not prose quality -- it is the three things about these
files that can silently rot:

1. **They ship.** ``[tool.hatch.build.targets.wheel] packages =
   ["src/memotron"]`` includes every file under the package directory, so
   ``AGENTS.md`` and ``SKILL.md`` are in the wheel with no ``pyproject.toml``
   change. Reading them back through ``importlib.resources`` is what makes a
   packaging regression fail here rather than in a deployment.
2. **The skill copy has not drifted.** ``.claude/skills/caveman-memory/SKILL.md``
   is what Claude Code loads and ``guidance/SKILL.md`` is what the package
   ships; they are the same words, so they are asserted byte-identical.
3. **They describe THIS design.** Amendment D replaced the sigil grammar with
   words, and a document still teaching an alphabet would teach an agent an
   alphabet nothing renders. So: printable ASCII, no middle dot, no arrow, no
   multiplication sign, and every element of the rendering an agent will
   actually see is named.
"""

from __future__ import annotations

import pathlib
from importlib.resources import files

from memotron.caveman import guidance

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SKILL_COPY = REPO_ROOT / ".claude" / "skills" / "caveman-memory" / "SKILL.md"

BANNED_SYMBOLS = ("·", "→", "×", "—", "–")
"""Middle dot, rightwards arrow, multiplication sign, em dash, en dash.

The first three are amendment D's named targets -- every one of them was a
rendering convention a prompt had to teach. The two dashes are here because they
are the way non-ASCII creeps back into a document nobody is checking, and
:func:`test_both_documents_are_printable_ascii` would then be the only thing
standing between a legend and a live run.
"""


def test_the_package_data_ships_and_reads_back() -> None:
    """Both documents resolve through ``importlib.resources``, not a path guess."""
    package = files("memotron.caveman.guidance")
    assert package.joinpath(guidance.AGENTS_FILENAME).is_file()
    assert package.joinpath(guidance.SKILL_FILENAME).is_file()
    assert guidance.agents_text().startswith("# Caveman memory")
    assert guidance.skill_text().startswith("---\n")


def test_both_documents_are_printable_ascii() -> None:
    """Nothing outside ASCII, and none of the symbols amendment D deleted."""
    for label, text in (("AGENTS.md", guidance.agents_text()), ("SKILL.md", guidance.skill_text())):
        assert text.isascii(), f"{label} contains a non-ASCII character"
        for symbol in BANNED_SYMBOLS:
            assert symbol not in text, f"{label} contains {symbol!r}"


def test_the_claude_code_skill_copy_is_byte_identical() -> None:
    """The committed copy Claude Code loads equals the packaged one exactly."""
    assert SKILL_COPY.read_bytes() == guidance.skill_text().encode("utf-8")


def test_the_skill_declares_its_frontmatter() -> None:
    """Name and description, in the two lines Claude Code reads them from."""
    lines = guidance.skill_text().splitlines()
    assert lines[0] == "---"
    assert lines[1] == "name: caveman-memory"
    assert lines[2].startswith("description: ")
    assert lines[3] == "---"
    assert len(lines[2]) > len("description: ") + 40


def test_the_skill_routes_every_subcommand() -> None:
    """Each ``/caveman-memory`` verb names the tool it resolves to."""
    text = guidance.skill_text()
    for verb, tool in (
        ("brief", "memory_brief"),
        ("read", "memory_read"),
        ("node", "memory_node"),
        ("neighbors", "memory_neighbors"),
        ("explain", "memory_explain"),
        ("ingest", "memory_ingest"),
        ("dream", "memory_dream"),
        ("replay", "memory_replay"),
    ):
        assert f"/caveman-memory {verb}" in text, f"the skill does not route {verb}"
        assert tool in text, f"the skill routes {verb} to nothing"


def test_the_contract_states_the_rendering_format() -> None:
    """Every element of a rendered block an agent will meet is named and shown.

    The header shape, each fact label, the relation form, the evidence suffix and
    the footer. A document that named the tools but not the format would leave
    the agent guessing at the only text it ever receives.
    """
    text = guidance.agents_text()
    expected = (
        "gateway (service) [n-001] as of 2026-09-11, 7 entries, aka JedAI Gateway, LiteLLM proxy",
        "rule: real runs always via the gateway, never hermetic or local stub",
        "is: LiteLLM proxy fronting the JedAI models, not a model itself",
        "chat default: claude-haiku-4-5",
        "BLOCKED agent-memory [n-004] until #245: Host header refused, #245 fixed the rewrite",
        "unsure: session affinity lost about 1 in 20 calls, never reproduced",
        "(x2)",
        "more: explain(n-001), neighbors(n-001)",
        "name (type) [id] as of DATE, N entries",
        "from name [id] TYPE: claim",
    )
    for fragment in expected:
        assert fragment in text, f"the contract does not show {fragment!r}"


def test_the_skill_quotes_the_same_format() -> None:
    """The skill shows the block too. An agent loading only the skill still knows it."""
    text = guidance.skill_text()
    assert "gateway (service) [n-001] as of 2026-09-11, 7 entries" in text
    assert "more: explain(n-001), neighbors(n-001)" in text
    assert "(x2)" in text


def test_the_contract_states_the_lifecycle_and_the_prohibitions() -> None:
    """Brief at session start and after compaction, read before relying, never a secret."""
    text = guidance.agents_text()
    for fragment in (
        "memory_brief(scope)",
        "after every compaction",
        "Never ingest a secret",
        "Zero writes is a correct session",
        "append-only",
        "reinforces",
        "evidence",
        "journal",
        "memory_replay",
    ):
        assert fragment in text, f"the contract does not state {fragment!r}"


def test_the_contract_explains_what_the_node_ids_are_for() -> None:
    """The ids are an address the agent traverses with, and the document says so."""
    text = guidance.agents_text()
    assert "[n-001]` is an address" in text
    for tool in ("memory_node", "memory_neighbors", "memory_explain"):
        assert tool in text


def test_neither_document_teaches_the_deleted_grammar() -> None:
    """No sigil legend, no ``lines``, no ``LINK`` edge. Amendment D deleted all three."""
    for text in (guidance.agents_text(), guidance.skill_text()):
        assert "sigil" not in text.lower()
        assert "LINK" not in text
        assert "L:" not in text
