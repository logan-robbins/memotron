"""#251 unit 18: stage 1, one episode plus one motive to appended ledger entries.

Three groups of assertion, and the order they are in is the order they matter in:

1. **The invariant.** ``extract.py`` imports nothing from the node graph, checked
   by walking the module's own ``import`` statements with ``ast``. Enforced, not
   documented -- a stage that could read the graph would extract different claims
   from the same episode depending on what was already known.
2. **The accepted path.** One entry per claim, ``node_ids`` empty on every one,
   ``supersedes`` resolved from a sibling's POSITION to that sibling's minted
   ``entry_id``, the motive stamped, one accepted receipt, and a prompt that
   actually contains the rubric and the turns.
3. **The four rejections.** A forward ``supersedes_claim_index``, an unknown
   sigil, a 401-character claim, and an identifier the episode never contained.
   Each must raise, receipt exactly once as ``EXTRACT_REJECTED``, and leave the
   ledger empty -- a partial accept would put a fabricated identifier in the
   ledger beside real ones.

The integration case runs the 10-turn fixture episode through the scripted
transport into a real ``":memory:"`` ledger: 12 entries, exactly one of them
superseding another.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from caveman_fakes import ScriptedChatTransport
from memotron.caveman import extract as extract_module
from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.extract import EXTRACT_SYSTEM, extract, render_extract_prompt
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import ClaimKind, ClaimMode, Episode, ReceiptOp, Turn
from memotron.caveman.motive import CavemanMotive, assistant_motive, engineering_motive
from memotron.caveman.receipts import InMemoryReceipts

NOW = datetime(2026, 9, 10, 9, 30, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"

# --------------------------------------------------------------- the fixtures

TURNS: tuple[tuple[str, str], ...] = (
    ("Logan", "Every real run goes through the JedAI Gateway. Never hermetic, ever."),
    ("Claude", "Understood. The gateway is the LiteLLM proxy fronting the JedAI models, correct?"),
    ("Logan", "Yes. The LiteLLM proxy and the JedAI Gateway are the same service."),
    ("Logan", "The agent-memory MCP server was deployed to C4 in #240."),
    ("Claude", "And the C4 memory server returns 24 tools after that deploy."),
    ("Logan", "It returns 31 tools, not 24 -- #248 corrected the count."),
    ("Logan", "The gateway refused the Host header until #245 fixed it."),
    ("Claude", "There is also an intermittent session-affinity failure; probe #246 names it."),
    ("Logan", "Embeddings are text-embedding-3 at 3072 dimensions, selected explicitly."),
    ("Logan", "Chat defaults to claude-haiku-4-5 unless a caller asks for something else."),
)
"""Ten turns carrying: two name pairs that denote one thing each, a correction,
an identifier inside a longer word (``3072 dimensions``), and six issue numbers.
"""

EPISODE = Episode(
    episode_id="ep-251-01",
    scope=SCOPE,
    occurred_at=NOW,
    turns=tuple(Turn(index=index, speaker=speaker, text=text) for index, (speaker, text) in enumerate(TURNS, 1)),
)


def _claim(
    claim: str,
    *,
    kind: str = "attribute",
    claim_mode: str = "descriptive",
    subjects: list[str],
    objects: list[str] | None = None,
    identifiers: list[str] | None = None,
    supersedes: int | None = None,
    confidence: float = 0.9,
    turns: list[int],
) -> dict[str, Any]:
    return {
        "claim": claim,
        "kind": kind,
        "claim_mode": claim_mode,
        "subjects": subjects,
        "objects": objects or [],
        "identifiers": identifiers or [],
        "supersedes_claim_index": supersedes,
        "confidence": confidence,
        "turns": turns,
    }


TWELVE_CLAIMS: tuple[dict[str, Any], ...] = (
    _claim(
        "Every real run of Memotron goes through the JedAI Gateway; hermetic mode is never used.",
        kind="rule",
        claim_mode="directive",
        subjects=["the JedAI Gateway"],
        turns=[1],
    ),
    _claim(
        "The JedAI Gateway is the LiteLLM proxy that fronts the JedAI models.",
        kind="is",
        subjects=["the JedAI Gateway"],
        objects=["the LiteLLM proxy"],
        turns=[2],
    ),
    _claim(
        "The LiteLLM proxy and the JedAI Gateway are two surface names for one service.",
        kind="is",
        subjects=["the LiteLLM proxy", "the JedAI Gateway"],
        turns=[3],
    ),
    _claim(
        "The agent-memory MCP server was deployed to the C4 cluster in #240.",
        kind="relation",
        claim_mode="report",
        subjects=["the agent-memory MCP server"],
        objects=["C4"],
        identifiers=["#240"],
        turns=[4],
    ),
    _claim(
        "The C4 memory server returned 24 tools immediately after the #240 deploy.",
        claim_mode="report",
        subjects=["the C4 memory server"],
        identifiers=["24", "#240"],
        turns=[5],
    ),
    _claim(
        "The C4 memory server returns 31 tools; #248 corrected the earlier count of 24.",
        claim_mode="correction",
        subjects=["the C4 memory server"],
        identifiers=["31", "#248"],
        supersedes=4,
        confidence=0.97,
        turns=[6],
    ),
    _claim(
        "The JedAI Gateway refused the Host header until #245 fixed it.",
        claim_mode="report",
        subjects=["the JedAI Gateway"],
        identifiers=["#245"],
        turns=[7],
    ),
    _claim(
        "The JedAI Gateway loses session affinity intermittently; probe #246 names the failure.",
        kind="unsure",
        subjects=["the JedAI Gateway"],
        identifiers=["#246"],
        confidence=0.6,
        turns=[8],
    ),
    _claim(
        "Embeddings use text-embedding-3 at 3072 dimensions, selected explicitly.",
        subjects=["the JedAI Gateway"],
        identifiers=["text-embedding-3", "3072"],
        turns=[9],
    ),
    _claim(
        "Chat defaults to claude-haiku-4-5 unless a caller asks for another model.",
        subjects=["the JedAI Gateway"],
        identifiers=["claude-haiku-4-5"],
        turns=[10],
    ),
    _claim(
        "The agent-memory MCP server and the C4 memory server are the same deployed server.",
        kind="is",
        subjects=["the agent-memory MCP server", "the C4 memory server"],
        turns=[4, 5],
    ),
    _claim(
        "Hermetic runs are never acceptable for real work, so the gateway is not optional.",
        kind="rule",
        claim_mode="requirement",
        subjects=["the JedAI Gateway"],
        turns=[1],
    ),
)
"""Twelve claims over the ten turns, exactly one of them superseding another."""


def _payload(*claims: dict[str, Any]) -> str:
    return json.dumps({"claims": list(claims)})


TWELVE_CLAIM_PAYLOAD = _payload(*TWELVE_CLAIMS)


@pytest.fixture
def ledger() -> Iterator[CavemanLedger]:
    store = CavemanLedger(":memory:")
    yield store
    store.close()


@pytest.fixture
def receipts() -> InMemoryReceipts:
    return InMemoryReceipts()


async def _run(
    raw: str,
    *,
    ledger: CavemanLedger,
    receipts: InMemoryReceipts,
    episode: Episode = EPISODE,
    motive: CavemanMotive | None = None,
) -> tuple[Any, ScriptedChatTransport]:
    transport = ScriptedChatTransport([raw])
    entries = await extract(
        episode=episode,
        motive=motive or engineering_motive(),
        transport=transport,
        ledger=ledger,
        receipts=receipts,
        now=NOW,
    )
    return entries, transport


# ============================================== the invariant: never the graph


def test_extract_imports_nothing_from_the_node_graph() -> None:
    """The "never sees the graph" invariant, enforced by reading this module.

    An ``ast`` walk over every ``import`` and ``from ... import`` in
    ``extract.py``: not one of them may name the graph. Grepping for a string
    would be fooled by a docstring; the syntax tree cannot be.
    """
    source = Path(extract_module.__file__).read_text(encoding="utf-8")
    imported: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.append(module)
            imported += [f"{module}.{alias.name}" for alias in node.names]
    assert imported, "the ast walk found no imports at all -- the check is not running"
    assert not [name for name in imported if "graph" in name.split(".")], imported


def test_extract_imports_no_forbidden_neighbour() -> None:
    """The package is self-contained: its own modules and the declared leaf seams only."""
    source = Path(extract_module.__file__).read_text(encoding="utf-8")
    forbidden = ("memotron.config", "memotron.dreaming", "memotron.client")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith(forbidden), node.module


# ================================================== the accepted path: entries


async def test_one_entry_is_appended_per_claim(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert len(entries) == len(TWELVE_CLAIMS)
    assert len(ledger.for_episode(EPISODE.episode_id)) == len(TWELVE_CLAIMS)


async def test_every_entry_is_appended_unbound(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """``node_ids`` is stage 2's to fill. Stage 1 has never seen a node."""
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert [entry.node_ids for entry in entries] == [()] * len(TWELVE_CLAIMS)
    assert len(ledger.unbound(scope=SCOPE)) == len(TWELVE_CLAIMS)


async def test_supersedes_resolves_a_sibling_index_to_that_siblings_entry_id(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """The one translation stage 1 performs: a position becomes a durable id.

    The model has no entry ids to point at, so it points at a position in its own
    answer; the ledger needs an id. Claim 5 supersedes claim 4.
    """
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert entries[5].supersedes == entries[4].entry_id
    assert [index for index, entry in enumerate(entries) if entry.supersedes is not None] == [5]


async def test_a_superseding_entry_reads_back_from_the_ledger_pointing_at_its_sibling(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """The resolution has to survive the round trip, not just the return value."""
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert ledger.get(entries[5].entry_id).supersedes == entries[4].entry_id


async def test_the_motive_is_stamped_on_every_entry(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """Which policy selected a claim is part of the claim's record."""
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts, motive=assistant_motive())
    assert {entry.motive for entry in entries} == {"assistant"}


async def test_the_episode_and_scope_are_stamped_on_every_entry(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert {entry.episode_id for entry in entries} == {EPISODE.episode_id}
    assert {entry.scope for entry in entries} == {SCOPE}
    assert {entry.ts for entry in entries} == {NOW}


async def test_the_claim_fields_arrive_unaltered(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """Stage 1 does not normalise. Surface words go in as the episode said them."""
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    first = entries[0]
    assert first.claim == TWELVE_CLAIMS[0]["claim"]
    assert first.kind is ClaimKind.RULE
    assert first.subjects == ("the JedAI Gateway",)
    assert entries[3].objects == ("C4",)
    assert entries[8].identifiers == ("text-embedding-3", "3072")


async def test_entry_ids_are_unique_across_the_response(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert len({entry.entry_id for entry in entries}) == len(entries)


async def test_extract_makes_exactly_one_llm_call(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """One call per episode is the design's budget for this stage."""
    _, transport = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert transport.call_count == 1
    transport.assert_exhausted()


# ================================================== the accepted path: receipt


async def test_one_accepted_receipt_is_emitted_per_call(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    emitted = receipts.all(scope=SCOPE)
    assert len(emitted) == 1
    assert emitted[0].op is ReceiptOp.EXTRACT_ACCEPTED


async def test_the_accepted_receipt_is_attributed_to_the_episode(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    receipt = receipts.all()[0]
    assert receipt.subject == EPISODE.episode_id
    assert receipt.scope == SCOPE
    assert receipt.ts == NOW


async def test_every_entry_cites_the_accepted_receipt(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """The entries and the receipt name each other, so a claim is attributable."""
    entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert {entry.receipt_id for entry in entries} == {receipts.all()[0].receipt_id}


async def test_the_accepted_receipt_counts_the_claims_and_the_supersessions(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    detail = receipts.all()[0].detail
    assert "12 claim(s) appended" in detail
    assert "1 superseding" in detail


async def test_the_accepted_receipt_carries_no_claim_text(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """Receipts are content-free by construction: counts and digests, never claims."""
    await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    receipt = receipts.all()[0]
    assert "LiteLLM" not in str(receipt)
    assert "#245" not in str(receipt)


async def test_the_accepted_receipts_digests_are_a_function_of_the_prompt_and_the_answer(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """Two runs of the same episode under two motives must be distinguishable."""
    second = InMemoryReceipts()
    await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    await _run(
        TWELVE_CLAIM_PAYLOAD,
        ledger=CavemanLedger(":memory:"),
        receipts=second,
        motive=assistant_motive(),
    )
    assert receipts.all()[0].inputs_digest != second.all()[0].inputs_digest
    assert receipts.all()[0].outputs_digest == second.all()[0].outputs_digest


# ==================================================================== the prompt


def test_the_prompt_contains_the_motives_extract_rubric() -> None:
    motive = engineering_motive()
    rendered = render_extract_prompt(EPISODE, motive)
    for bullet in motive.extract_rubric:
        assert bullet in rendered
    for bullet in motive.extract_exclusions:
        assert bullet in rendered


def test_the_prompt_does_not_carry_the_dream_rubric() -> None:
    """Telling the extractor to compress is how a claim arrives unsupportable."""
    motive = engineering_motive()
    rendered = render_extract_prompt(EPISODE, motive)
    for bullet in motive.dream_rubric:
        assert bullet not in rendered


def test_the_prompt_contains_every_turn_of_the_episode() -> None:
    rendered = render_extract_prompt(EPISODE, engineering_motive())
    for turn in EPISODE.turns:
        assert turn.text in rendered
        assert f"{turn.index}. {turn.speaker}" in rendered


def test_the_prompt_renders_the_episode_through_as_prompt_text() -> None:
    """The identifier check reads this same string, so the two cannot disagree."""
    assert EPISODE.as_prompt_text() in render_extract_prompt(EPISODE, engineering_motive())


def test_the_prompt_contains_the_format_block_and_the_closing_line() -> None:
    rendered = render_extract_prompt(EPISODE, engineering_motive())
    assert "HOW A BELIEF IS RECORDED" in rendered
    assert rendered.rstrip().endswith("Answer with one JSON object and nothing else.")


def test_the_prompt_names_every_claim_kind_and_every_claim_mode() -> None:
    """Generated from the enums, so a new member cannot be invisible to the model."""
    rendered = render_extract_prompt(EPISODE, engineering_motive())
    for kind in ClaimKind:
        assert kind.value in rendered
    for mode in ClaimMode:
        assert mode.value in rendered


def test_the_prompt_states_the_motives_own_fact_budget() -> None:
    """L and T come from the motive, so two personas get two different budgets."""
    rendered = render_extract_prompt(EPISODE, assistant_motive())
    assert "At most 6 facts on one node" in rendered


def test_the_system_prompt_forbids_normalising_and_guessing_at_a_graph() -> None:
    """The two instructions the invariant depends on being stated, not implied."""
    assert "never seen the memory graph" in EXTRACT_SYSTEM
    assert "OWN surface words" in EXTRACT_SYSTEM


async def test_the_system_prompt_is_sent_as_the_system_prompt(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    _, transport = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)
    assert transport.last.system_prompt == EXTRACT_SYSTEM
    assert transport.last.prompt == render_extract_prompt(EPISODE, engineering_motive())


# ================================================================ the rejections

_LONG_CLAIM = "x" * 401


@pytest.mark.parametrize(
    ("label", "payload"),
    [
        (
            "a forward supersedes_claim_index",
            _payload(
                _claim("The gateway is the proxy under another name.", subjects=["gateway"], supersedes=1, turns=[2]),
                _claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[2]),
            ),
        ),
        (
            "a self-referential supersedes_claim_index",
            _payload(
                _claim("The gateway is the proxy under another name.", subjects=["gateway"], supersedes=0, turns=[2]),
            ),
        ),
        (
            "an unknown sigil",
            _payload(_claim("The gateway fronts the JedAI models.", kind="?", subjects=["gateway"], turns=[2])),
        ),
        (
            "a 401-character claim",
            _payload(_claim(_LONG_CLAIM, subjects=["gateway"], turns=[2])),
        ),
        (
            "an identifier the episode never contained",
            _payload(
                _claim(
                    "The gateway lost session affinity, as issue #999 records.",
                    subjects=["gateway"],
                    identifiers=["#999"],
                    turns=[8],
                )
            ),
        ),
        (
            "a blank identifier",
            _payload(_claim("The gateway fronts the models.", subjects=["gateway"], identifiers=[""], turns=[2])),
        ),
        (
            "a turn the episode does not have",
            _payload(_claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[99])),
        ),
        (
            "five subjects where four is the ceiling",
            _payload(_claim("Five things are one thing.", subjects=["a", "b", "c", "d", "e"], turns=[1])),
        ),
        (
            "no claims at all",
            '{"claims": []}',
        ),
    ],
)
async def test_each_out_of_contract_response_raises(
    label: str, payload: str, ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(payload, ledger=ledger, receipts=receipts)
    assert caught.value.source == "EXTRACT", label
    assert caught.value.errors, label


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            _claim("The gateway is the proxy under another name.", subjects=["gateway"], supersedes=1, turns=[2]),
            _claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[2]),
        ),
        _payload(_claim("The gateway fronts the JedAI models.", kind="?", subjects=["gateway"], turns=[2])),
        _payload(_claim(_LONG_CLAIM, subjects=["gateway"], turns=[2])),
        _payload(
            _claim(
                "The gateway lost session affinity, as issue #999 records.",
                subjects=["gateway"],
                identifiers=["#999"],
                turns=[8],
            )
        ),
    ],
)
async def test_each_rejection_appends_nothing(payload: str, ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """All or nothing. A partial accept puts a fabrication beside real claims."""
    with pytest.raises(OutOfContractResponse):
        await _run(payload, ledger=ledger, receipts=receipts)
    assert ledger.for_episode(EPISODE.episode_id) == []
    assert ledger.unbound(scope=SCOPE) == []


@pytest.mark.parametrize(
    "payload",
    [
        _payload(
            _claim("The gateway is the proxy under another name.", subjects=["gateway"], supersedes=1, turns=[2]),
            _claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[2]),
        ),
        _payload(_claim("The gateway fronts the JedAI models.", kind="?", subjects=["gateway"], turns=[2])),
        _payload(_claim(_LONG_CLAIM, subjects=["gateway"], turns=[2])),
        _payload(
            _claim(
                "The gateway lost session affinity, as issue #999 records.",
                subjects=["gateway"],
                identifiers=["#999"],
                turns=[8],
            )
        ),
        _payload(_claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[99])),
    ],
)
async def test_each_rejection_emits_exactly_one_reject_receipt(
    payload: str, ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """Exactly one, and no accepted receipt beside it."""
    with pytest.raises(OutOfContractResponse):
        await _run(payload, ledger=ledger, receipts=receipts)
    emitted = receipts.all()
    assert len(emitted) == 1
    assert emitted[0].op is ReceiptOp.EXTRACT_REJECTED


async def test_the_identifier_rejection_names_the_offending_identifier_on_the_exception(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """Developer-facing, at the moment of failure. This is where the value belongs."""
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(
                _claim(
                    "The gateway lost session affinity, as issue #999 records.",
                    subjects=["gateway"],
                    identifiers=["#999"],
                    turns=[8],
                )
            ),
            ledger=ledger,
            receipts=receipts,
        )
    joined = " ".join(caught.value.errors)
    assert "#999" in joined
    assert "does not appear verbatim in the episode" in joined


async def test_the_identifier_rejection_receipt_names_the_position_not_the_identifier(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """A durable audit row must not carry the model's content -- only where it was."""
    with pytest.raises(OutOfContractResponse):
        await _run(
            _payload(
                _claim(
                    "The gateway lost session affinity, as issue #999 records.",
                    subjects=["gateway"],
                    identifiers=["#999"],
                    turns=[8],
                )
            ),
            ledger=ledger,
            receipts=receipts,
        )
    detail = receipts.all()[0].detail
    assert "#999" not in detail
    assert detail == "1 identifier(s) not verbatim in the episode at claims [0]"


async def test_the_turn_rejection_receipt_names_the_claim_position(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    with pytest.raises(OutOfContractResponse):
        await _run(
            _payload(_claim("The gateway fronts the JedAI models.", subjects=["gateway"], turns=[99])),
            ledger=ledger,
            receipts=receipts,
        )
    assert receipts.all()[0].detail == "1 turn reference(s) outside the episode at claims [0]"


async def test_both_episode_rules_are_reported_together(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """One rejection, both reasons: a second call would not tell you more."""
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(
                _claim(
                    "The gateway lost affinity, as #999 records.",
                    subjects=["gateway"],
                    identifiers=["#999"],
                    turns=[99],
                )
            ),
            ledger=ledger,
            receipts=receipts,
        )
    assert len(caught.value.errors) == 2
    detail = receipts.all()[0].detail
    assert "turn reference(s)" in detail
    assert "identifier(s) not verbatim" in detail


# ----------------------------------------- what the identifier rule must ACCEPT


@pytest.mark.parametrize(
    ("label", "identifier"),
    [
        ("a number inside a longer phrase", "3072"),
        ("a model alias", "claude-haiku-4-5"),
        ("an issue number", "#248"),
        ("an embedding model name", "text-embedding-3"),
        ("a bare count", "31"),
    ],
)
async def test_an_identifier_quoted_out_of_the_episode_passes(
    label: str, identifier: str, ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """Substring, not token match: ``3072`` out of ``3072 dimensions`` is correct."""
    entries, _ = await _run(
        _payload(
            _claim(
                "The gateway records a measured fact with its identifier.",
                subjects=["gateway"],
                identifiers=[identifier],
                turns=[9],
            )
        ),
        ledger=ledger,
        receipts=receipts,
    )
    assert entries[0].identifiers == (identifier,), label


async def test_the_identifier_rule_is_case_sensitive(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """Case is part of an identifier's identity, and the dreamer copies it verbatim."""
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(
                _claim(
                    "Chat defaults to a haiku model.",
                    subjects=["gateway"],
                    identifiers=["CLAUDE-HAIKU-4-5"],
                    turns=[10],
                )
            ),
            ledger=ledger,
            receipts=receipts,
        )
    assert "CLAUDE-HAIKU-4-5" in " ".join(caught.value.errors)


# ------------------------------------------------- transport and repair absence


async def test_a_transport_failure_is_not_receipted_as_a_rejection(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """ "The gateway was down" and "the prompt is wrong" are different facts."""
    transport = ScriptedChatTransport([ValueError("caveman chat request failed: connection reset")])
    with pytest.raises(ValueError, match="connection reset"):
        await extract(
            episode=EPISODE,
            motive=engineering_motive(),
            transport=transport,
            ledger=ledger,
            receipts=receipts,
            now=NOW,
        )
    assert receipts.all() == []
    assert ledger.for_episode(EPISODE.episode_id) == []


async def test_a_rejected_response_is_not_re_prompted(ledger: CavemanLedger, receipts: InMemoryReceipts) -> None:
    """A second scripted answer proves there is no repair pass: it stays unused."""
    transport = ScriptedChatTransport(["not json", TWELVE_CLAIM_PAYLOAD])
    with pytest.raises(OutOfContractResponse):
        await extract(
            episode=EPISODE,
            motive=engineering_motive(),
            transport=transport,
            ledger=ledger,
            receipts=receipts,
            now=NOW,
        )
    assert transport.call_count == 1
    assert transport.remaining == 1


# ============================================================== integration


async def test_the_ten_turn_episode_lands_twelve_entries_with_one_supersession(
    ledger: CavemanLedger, receipts: InMemoryReceipts
) -> None:
    """The integration case: a scripted transport into a real ``":memory:"`` ledger.

    Nothing is mocked below the transport -- the ledger is sqlite, the response is
    validated by the real pydantic contract, and the assertions read the entries
    back out of the database rather than trusting the return value.
    """
    entries, transport = await _run(TWELVE_CLAIM_PAYLOAD, ledger=ledger, receipts=receipts)

    stored = ledger.for_episode(EPISODE.episode_id)
    assert len(stored) == 12
    assert [entry.entry_id for entry in stored] == [entry.entry_id for entry in entries]
    assert [entry.supersedes for entry in stored].count(None) == 11

    superseding = [entry for entry in stored if entry.supersedes is not None]
    assert len(superseding) == 1
    assert ledger.get(superseding[0].supersedes or "").claim.endswith("after the #240 deploy.")

    assert transport.call_count == 1
    assert len(receipts.all()) == 1
    assert receipts.all()[0].op is ReceiptOp.EXTRACT_ACCEPTED


async def test_a_second_ledger_on_the_same_file_reads_the_entries_back(
    tmp_path: Path, receipts: InMemoryReceipts
) -> None:
    """The regenerability precondition: the ledger, not the graph, is the record."""
    path = str(tmp_path / "caveman.sqlite")
    writer = CavemanLedger(path)
    try:
        entries, _ = await _run(TWELVE_CLAIM_PAYLOAD, ledger=writer, receipts=receipts)
    finally:
        writer.close()
    reader = CavemanLedger(path)
    try:
        assert len(reader.for_episode(EPISODE.episode_id)) == len(entries)
    finally:
        reader.close()
