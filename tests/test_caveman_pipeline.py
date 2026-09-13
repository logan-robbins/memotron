"""#251 unit 24: the composition root.

Two kinds of test, kept apart deliberately.

The **unit** tests check wiring and nothing else: that a missing seam fails at
the construction site rather than deep inside a stage, that ``ingest`` runs
extract then reconcile in that order, that ``dream`` runs 3a then 3b, that
``read`` makes no LLM call at all, and that the receipt stream for one
ingest-and-dream is exactly the documented op order. None of them re-check a
stage's own rules — those have owners, and a second copy of a rule here would be
a second place for it to drift.

The **integration** test is the whole 10-turn episode through the real
``":memory:"`` ledger, the real ``InMemoryGraph``, the real
``LocalEmbeddingTransport`` and a ``ScriptedChatTransport``: ingest, dream, read,
then a second dream under ``max_nodes=3`` that forces a merge. Every store on
that path is the shipped implementation; only the model is scripted, because a
live model is what ``examples/caveman_demo.py`` is for.

.. rubric:: Why the episode text is duplicated from the demo

``examples/caveman_demo.py`` owns the canonical episode, and this file mirrors
its ten turns. ``examples/`` is not a package and is not on the test path, so
importing it would mean a ``sys.path`` edit in a test — a worse trade than a
copy of ten lines of fixture prose. The copy is fixture DATA, not a second
implementation: nothing here is imported by the demo and nothing there is
imported by this.

.. rubric:: Why the forced-merge slate is computed rather than hard-coded

The slate is a pure function of the graph's own state, so the test asks
``pressure.merge_slate`` for it through the same public API ``dream`` uses, then
scripts the model's answer to that mandate. Hard-coding a node id pair would
make the test a statement about which node happened to rank lowest, which is
``test_caveman_pressure.py``'s subject, not this one's.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caveman_fakes import ScriptedChatTransport
from memotron.caveman.errors import NodeNotFound
from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    ClaimKind,
    ClaimMode,
    Episode,
    Fact,
    FactKind,
    LedgerEntry,
    Node,
    ReceiptOp,
    Turn,
)
from memotron.caveman.motive import CavemanMotive, engineering_motive
from memotron.caveman.pipeline import (
    NO_RELATIONS,
    CavemanMemory,
    DreamOutcome,
    IngestOutcome,
    UtcClock,
)
from memotron.caveman.pressure import (
    MergePair,
    ValuedNode,
    embedding_similarity,
    merge_slate,
    node_value,
    pressure,
)
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.render import (
    FACT_LABEL,
    FACT_ORDER,
    FOOTER_PREFIX,
    render_header,
    validate_fact_text,
)
from memotron.caveman.seams import GraphStore, LedgerStore
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
SCOPE = "repo:jedai/memotron"
EPISODE_ID = "ep-251-01"

EMBED = LocalEmbeddingTransport()


class FixedClock:
    """A ``Clock`` pinned to one instant.

    The composition root is the only thing in the package that reads a clock, so
    pinning time is a constructor argument rather than a patched module — which
    is the whole reason ``Clock`` is a seam.
    """

    def __init__(self, instant: datetime = NOW) -> None:
        self.instant = instant

    def now(self) -> datetime:
        return self.instant


# ==================================================== the canonical 10-turn episode

TURNS: tuple[tuple[str, str], ...] = (
    (
        "Logan",
        "One rule before we touch anything: every real run goes through the JedAI Gateway. "
        "No hermetic mode, no local stub, not even for a quick check. If the platform is broken "
        "we fix the platform — we don't downgrade the run.",
    ),
    (
        "Priya",
        "Agreed. And for whoever reads this later: the JedAI Gateway is a LiteLLM proxy sitting "
        "in front of the JedAI models. It isn't a model itself, it's the thing that routes to them.",
    ),
    (
        "agent",
        "Confirmed against the deploy history. Before #245 the gateway refused our Host header "
        "outright, so the agent-memory MCP server couldn't reach it at all. #245 fixed the Host rewrite.",
    ),
    (
        "Priya",
        "Right, and the same PR fixed the other half — the chart was deploying agent-memory from "
        "the wrong image tag. The chart is where the agent-memory MCP deploy lives in the first "
        "place; that came in with #240.",
    ),
    (
        "Logan",
        "There's still something flaky underneath all that. Roughly one call in twenty loses "
        "session affinity and lands on a cold pod. I've never got it to reproduce on demand.",
    ),
    (
        "agent",
        "That's the failure the probe in #246 names. It asserts the intermittent affinity loss "
        "rather than pretending it's fixed, which is why the probe is committed.",
    ),
    (
        "Priya",
        "On capability — C4 is returning 24 tools right now, so I'd call the surface basically complete.",
    ),
    (
        "agent",
        "Two more worth recording. The chat default on the gateway is claude-haiku-4-5. The "
        "embedding alias is text-embedding-3 at 3072 dimensions, and it has to be selected "
        "explicitly — there's no default embedding model applied for you.",
    ),
    (
        "Logan",
        "Correction on Priya's number. I counted again after #248 landed: the C4 memory server "
        "returns 31 tools, not 24. 24 was the count before #248.",
    ),
    (
        "Priya",
        "Last one, and treat it as a rule: we only ever name undated aliases on the LiteLLM proxy "
        "— claude-haiku-4-5, never a dated pin. A dated alias goes stale and the gateway starts "
        "refusing it.",
    ),
)


def _episode() -> Episode:
    return Episode(
        episode_id=EPISODE_ID,
        scope=SCOPE,
        occurred_at=NOW,
        turns=tuple(Turn(index=index, speaker=speaker, text=text) for index, (speaker, text) in enumerate(TURNS, 1)),
    )


# =========================================================== the scripted answers

GATEWAY = "the JedAI Gateway"
PROXY = "the LiteLLM proxy"
MCP = "the agent-memory MCP server"
C4 = "the C4 memory server"
CHART = "the chart"
PROBE = "the probe in #246"


def _kind_ordered(lines: list[str]) -> list[str]:
    """*lines* in the order a block must render them, read off their labels."""
    ranks = {FACT_LABEL[kind]: order for kind, order in FACT_ORDER.items()}
    return sorted(lines, key=lambda text: ranks.get(text.partition(":")[0], FACT_ORDER[FactKind.ATTRIBUTE]))


def _emitted_facts(rendered: str) -> list[str]:
    """The rendered fact lines of a read: not the headers, not the footer."""
    return [
        line
        for block in rendered.split("\n\n")
        if not block.startswith(FOOTER_PREFIX)
        for line in block.splitlines()[1:]
    ]


def _fact(text: str, kind: FactKind = FactKind.ATTRIBUTE) -> Fact:
    """One fact as the dreamer would have written it, at this module's clock."""
    return Fact(kind=kind, text=text, entry_ids=("e-fixture",), first_seen=NOW, last_seen=NOW)


def _texts(node: Node) -> tuple[str, ...]:
    """A node's fact TEXT, which is what these assertions are about."""
    return tuple(fact.text for fact in node.facts)


def _claim(
    claim: str,
    kind: ClaimKind,
    mode: str,
    *,
    subjects: tuple[str, ...],
    objects: tuple[str, ...] = (),
    identifiers: tuple[str, ...] = (),
    turns: tuple[int, ...],
    supersedes: int | None = None,
) -> dict[str, Any]:
    return {
        "claim": claim,
        "kind": kind.value,
        "claim_mode": mode,
        "subjects": list(subjects),
        "objects": list(objects),
        "identifiers": list(identifiers),
        "supersedes_claim_index": supersedes,
        "confidence": 0.9,
        "turns": list(turns),
    }


#: Twelve claims over the ten turns, with exactly one in-episode correction
#: (claim 10 supersedes claim 7's tool count). Hand-written against the episode
#: above, so every ``identifiers`` member appears verbatim in it — the one
#: content rule ``extract`` enforces.
EXTRACT_CLAIMS: tuple[dict[str, Any], ...] = (
    _claim(
        "Every real run goes through the JedAI Gateway; hermetic mode and local stubs are never used.",
        ClaimKind.RULE,
        "directive",
        subjects=(GATEWAY,),
        turns=(1,),
    ),
    _claim(
        "The JedAI Gateway is a LiteLLM proxy fronting the JedAI models rather than a model itself.",
        ClaimKind.IS,
        "descriptive",
        subjects=(GATEWAY,),
        turns=(2,),
    ),
    _claim(
        "Before #245 the JedAI Gateway refused the Host header, so the agent-memory MCP server could not reach it.",
        ClaimKind.RELATION,
        "report",
        subjects=(GATEWAY,),
        objects=(MCP,),
        identifiers=("#245",),
        turns=(3,),
    ),
    _claim(
        "The chart was deploying the agent-memory MCP server from the wrong image tag until #245 fixed it.",
        ClaimKind.RELATION,
        "report",
        subjects=(CHART,),
        objects=(MCP,),
        identifiers=("#245",),
        turns=(4,),
    ),
    _claim(
        "The chart is where the agent-memory MCP server deploy lives, which arrived with #240.",
        ClaimKind.RELATION,
        "descriptive",
        subjects=(CHART,),
        objects=(MCP,),
        identifiers=("#240",),
        turns=(4,),
    ),
    _claim(
        "Roughly one call in twenty loses session affinity and lands on a cold pod, never reproduced on demand.",
        ClaimKind.UNSURE,
        "report",
        subjects=(GATEWAY,),
        turns=(5,),
    ),
    _claim(
        "The probe in #246 asserts the intermittent session-affinity loss rather than treating it as fixed.",
        ClaimKind.ATTRIBUTE,
        "report",
        subjects=(GATEWAY,),
        objects=(PROBE,),
        identifiers=("#246",),
        turns=(6,),
    ),
    _claim(
        "The C4 memory server is returning 24 tools.",
        ClaimKind.ATTRIBUTE,
        "report",
        subjects=(C4,),
        identifiers=("24",),
        turns=(7,),
    ),
    _claim(
        "The chat default on the JedAI Gateway is claude-haiku-4-5.",
        ClaimKind.ATTRIBUTE,
        "descriptive",
        subjects=(GATEWAY,),
        identifiers=("claude-haiku-4-5",),
        turns=(8,),
    ),
    _claim(
        "The embedding alias is text-embedding-3 at 3072 dimensions and must be selected explicitly.",
        ClaimKind.ATTRIBUTE,
        "descriptive",
        subjects=(GATEWAY,),
        identifiers=("text-embedding-3", "3072"),
        turns=(8,),
    ),
    _claim(
        "The C4 memory server returns 31 tools after #248 landed, not the 24 counted before it.",
        ClaimKind.ATTRIBUTE,
        "correction",
        subjects=(C4,),
        identifiers=("31", "#248", "24"),
        turns=(9,),
        supersedes=7,
    ),
    _claim(
        "Only undated aliases such as claude-haiku-4-5 are ever named on the LiteLLM proxy, never a dated pin.",
        ClaimKind.RULE,
        "directive",
        subjects=(PROXY,),
        identifiers=("claude-haiku-4-5",),
        turns=(10,),
    ),
)

EXTRACT_PAYLOAD = json.dumps({"claims": list(EXTRACT_CLAIMS)})

#: Six surface names collapsing to four nodes. Two convergences are scripted
#: rather than incidental — the two gateway names and the two memory-server names
#: share a ``new_node.name`` character for character, which is HOW within-batch
#: reconciliation produces one node from two names.
RECONCILE_PAYLOAD = json.dumps(
    {
        "bindings": [
            {
                "local_name": GATEWAY,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "gateway",
                    "type": "service",
                    "gloss": "the LiteLLM proxy fronting the JedAI models",
                },
                "reason": "no candidate exists in an empty scope",
            },
            {
                "local_name": MCP,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "agent-memory",
                    "type": "service",
                    "gloss": "the agent-memory MCP server deployed on C4",
                },
                "reason": "no candidate exists in an empty scope",
            },
            {
                "local_name": CHART,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "chart",
                    "type": "artifact",
                    "gloss": "the Helm chart that deploys the agent-memory MCP server",
                },
                "reason": "the deployment artifact is its own thing",
            },
            {
                "local_name": PROBE,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "probe",
                    "type": "artifact",
                    "gloss": "the committed probe that asserts the affinity loss",
                },
                "reason": "a committed test artifact, not the service it tests",
            },
            {
                "local_name": C4,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "agent-memory",
                    "type": "service",
                    "gloss": "the agent-memory MCP server deployed on C4",
                },
                "reason": "the same MCP memory server under its cluster's name",
            },
            {
                "local_name": PROXY,
                "decision": "new",
                "node_id": None,
                "new_node": {
                    "name": "gateway",
                    "type": "service",
                    "gloss": "the LiteLLM proxy fronting the JedAI models",
                },
                "reason": "the same service under a second surface name",
            },
        ],
        #: The four relational claims of EXTRACT_CLAIMS, by ``claim_index`` --
        #: positions 2, 3, 4 and 6 of that list, which are the only claims
        #: carrying ``objects`` or a ``relation`` kind (#251 amendment D, D-B).
        #: Two of them state the same belief about the same pair under one type,
        #: so the scope ends with three edges and one of them has evidence two.
        "relations": [
            {"claim_index": 0, "source": GATEWAY, "target": MCP, "type": "BLOCKED"},
            {"claim_index": 1, "source": CHART, "target": MCP, "type": "DEPLOYS"},
            {"claim_index": 2, "source": CHART, "target": MCP, "type": "DEPLOYS"},
            {"claim_index": 3, "source": GATEWAY, "target": PROBE, "type": "ASSERTED_BY"},
        ],
    }
)

#: One answer per dirty node, keyed by node NAME -- ``graph.dirty`` is
#: name-ordered, so this is also the script order: agent-memory, chart, gateway,
#: probe. The value is the node's new type, its fact texts, and the ``reason``.
#:
#: Every fact here is an ``attribute``, which is the fixture's own choice and not
#: a limitation: a labelled fact is what the read assertions below are written
#: against, and the named kinds have their own coverage in
#: ``test_caveman_dream.py``.
DREAM_NODE_ANSWERS: dict[str, tuple[str, tuple[str, ...], str]] = {
    "agent-memory": (
        "service",
        (
            "MCP memory server deployed on C4",
            "returns 31 tools after #248, was 24",
            "deploys it, #240; image fixed #245",
            "Host header refused pre-#245",
        ),
        "supersession applied: 31 tools replaces 24",
    ),
    "chart": (
        "artifact",
        (
            "Helm chart deploying agent-memory MCP",
            "deploy arrived with #240",
            "wrong image tag until #245",
        ),
        "one edge rather than two restatements",
    ),
    "gateway": (
        "service",
        (
            "every real run goes through it; never hermetic",
            "only undated aliases named; never a dated pin",
            "LiteLLM proxy fronting the JedAI models",
            "chat default claude-haiku-4-5",
            "embeddings text-embedding-3, 3072d, explicit",
            "1 call in 20 loses session affinity",
            "#246 asserts the affinity loss",
            "Host refused pre-#245, fixed",
        ),
        "both constraints kept; identifiers verbatim",
    ),
    "probe": (
        "artifact",
        (
            "committed probe asserting affinity loss",
            "names the intermittent failure, #246",
        ),
        "the probe is defined by what it asserts",
    ),
}


def dream_node_payloads(*, graph: GraphStore, ledger: LedgerStore) -> tuple[str, ...]:
    """The 3a script for this scope's dirty nodes, each citing that node's own entries.

    A function rather than a constant since #251 amendment D package D-C: every
    fact names the ledger entry ids that evidence it, the ledger mints those ids
    itself, and ``dream`` rejects a fact citing an entry that is not bound to the
    node -- so the answer cannot be written before the ingest that creates the
    ids.

    Which means the tests below construct TWO roots over the same stores: one
    scripted for the ingest, one for the dream. ``ScriptedChatTransport`` is
    frozen and takes its whole script at construction, which is the right shape
    for a fake whose point is that an unplanned call fails loudly -- so the
    two-phase script is two transports rather than a mutable one.

    Each fact cites every entry bound to the node. That is the honest claim for a
    fixture ("this node's evidence supports this") and it exercises the union
    rule: a re-dream restating one of these texts has to carry the same ids back.
    """
    payloads: list[str] = []
    for node in graph.dirty(scope=SCOPE):
        node_type, texts, reason = DREAM_NODE_ANSWERS[node.name]
        entry_ids = [entry.entry_id for entry in ledger.for_node(node.node_id)]
        payloads.append(
            json.dumps(
                {
                    "type": node_type,
                    "facts": [
                        {"kind": FactKind.ATTRIBUTE.value, "key": None, "text": text, "entry_ids": entry_ids}
                        for text in texts
                    ],
                    "relations": [],
                    "retired_relation_ids": [],
                    "reason": reason,
                }
            )
        )
    return tuple(payloads)


#: A free pass at ``N=500``. Zero ops is a valid and common outcome: four nodes
#: under a budget of five hundred need no reorganising.
DREAM_GLOBAL_FREE_PAYLOAD = json.dumps({"ops": []})


def _forced_merge_payload(
    doomed_node_id: str,
    survivor_node_id: str,
    *,
    survivor_name: str,
    entry_ids: Sequence[str],
) -> str:
    """The global answer to a one-pair mandate.

    *survivor_name* is the survivor's OWN current name: a merge keeps it, and any
    other name is rejected (#251 amendment A).

    *entry_ids* is the evidence the survivor's one fact cites. A merge's facts are
    judged against the UNION of every merged node's entries -- the re-key moves
    them all onto the survivor -- so either half's ids are valid, and the caller
    passes what the ledger actually holds (#251 amendment D, package D-C).

    No ``relations``: ``merge_nodes`` re-points the absorbed node's edges by
    itself, so restating them would write the same beliefs twice.
    """
    return json.dumps(
        {
            "ops": [
                {
                    "op": "merge",
                    "nodes": [doomed_node_id, survivor_node_id],
                    "survivor_name": survivor_name,
                    "survivor_type": "artifact",
                    "facts": [
                        {
                            "kind": FactKind.ATTRIBUTE.value,
                            "key": None,
                            "text": "merged under the node budget at N=3",
                            "entry_ids": list(entry_ids),
                        }
                    ],
                    "reason": "the two lowest-value nodes collapse under pressure",
                }
            ]
        }
    )


# ================================================================== construction


def _memory(
    *,
    graph: GraphStore,
    ledger: LedgerStore,
    receipts: InMemoryReceipts,
    responses: tuple[str, ...],
) -> tuple[CavemanMemory, ScriptedChatTransport]:
    transport = ScriptedChatTransport(list(responses))
    memory = CavemanMemory(
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        chat=transport,
        embedder=EMBED,
        clock=FixedClock(),
    )
    return memory, transport


def test_the_utc_clock_is_timezone_aware_and_utc() -> None:
    """The package's one concrete ``Clock``. Naive is never valid here.

    A naive datetime compared against an aware one raises, and every ``since=``
    query in the ledger is such a comparison — so tz-awareness is a correctness
    property, not a style rule.
    """
    instant = UtcClock().now()
    assert instant.tzinfo is not None
    assert instant.utcoffset() == datetime(2026, 1, 1, tzinfo=UTC).utcoffset()


@pytest.mark.parametrize(
    "omitted",
    ["graph", "ledger", "receipts", "chat", "embedder", "clock"],
)
def test_construction_without_a_seam_fails_at_the_call_site(omitted: str) -> None:
    """A ``TypeError`` naming the argument, not an ``AttributeError`` two stages later.

    Every seam is keyword-only and required, so the mistake surfaces where it was
    made. A default would let a caller run against an in-memory store believing
    it had a durable one.
    """
    seams: dict[str, Any] = {
        "graph": InMemoryGraph(),
        "ledger": CavemanLedger(":memory:"),
        "receipts": InMemoryReceipts(),
        "chat": ScriptedChatTransport([]),
        "embedder": EMBED,
        "clock": FixedClock(),
    }
    ledger = seams["ledger"]
    del seams[omitted]
    try:
        with pytest.raises(TypeError, match=omitted):
            CavemanMemory(**seams)
    finally:
        ledger.close()


async def test_read_makes_no_llm_call_at_all() -> None:
    """Zero, not "few". The agency sits around the call, never inside the rerank.

    Scripted with an empty script, so any call at all raises rather than being
    counted afterwards.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, transport = _memory(graph=InMemoryGraph(), ledger=ledger, receipts=InMemoryReceipts(), responses=())
        result = memory.read(query="what do I know about the gateway", scope=SCOPE, motive=engineering_motive())
        assert transport.call_count == 0
        assert result.rendered == ""
    finally:
        ledger.close()


async def test_brief_makes_no_llm_call_and_needs_no_query_at_all() -> None:
    """The session-start read: zero calls, and ``query`` comes back empty.

    Scripted empty, so a call raises rather than being counted afterwards. The
    empty ``query`` is the wiring assertion that matters: ``read.read`` REFUSES a
    blank query, so a root that delegated to it instead of to ``read.brief``
    could not produce this result at all.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, transport = _memory(graph=InMemoryGraph(), ledger=ledger, receipts=InMemoryReceipts(), responses=())
        result = memory.brief(scope=SCOPE, motive=engineering_motive())
        assert transport.call_count == 0
        assert result.query == ""
        assert result.scope == SCOPE
        assert result.seeds == ()
    finally:
        ledger.close()


async def test_explain_makes_no_llm_call_and_reads_the_roots_own_clock() -> None:
    """The deep read behind a footer: zero calls, and bounded by the fixed clock.

    The clock is the wiring under test. ``explain`` refuses an entry dated after
    ``now``, so an entry stamped one day past :data:`NOW` proves the root passed
    its own ``Clock`` rather than reading the wall clock — under a live clock
    that entry would be in the past and the call would succeed.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        memory, transport = _memory(graph=graph, ledger=ledger, receipts=InMemoryReceipts(), responses=())
        node = graph.create_node(
            scope=SCOPE,
            name="gateway",
            type="service",
            facts=(_fact("a LiteLLM proxy fronting the JedAI models", FactKind.IS),),
            embedding=EMBED.embed("gateway"),
            now=NOW,
            aliases=(PROXY,),
        )
        ledger.append(_entry("e-1", node.node_id, ts=NOW, claim="The JedAI Gateway is a LiteLLM proxy."))

        rendered = memory.explain(node_id=node.node_id)
        assert transport.call_count == 0
        assert rendered.splitlines()[0].startswith("gateway (service) [")
        assert "The JedAI Gateway is a LiteLLM proxy." in rendered
        assert rendered.splitlines()[-1] == f"aliases: {PROXY}"

        ledger.append(
            _entry("e-2", node.node_id, ts=NOW + timedelta(days=1), claim="A claim from a second, faster clock.")
        )
        with pytest.raises(ValueError, match="after now="):
            memory.explain(node_id=node.node_id)
    finally:
        ledger.close()


async def test_explain_refuses_a_node_id_the_graph_does_not_hold() -> None:
    """A footer names ids that existed when it ran. One that no longer does is a defect.

    Not an empty string: a caller holding an unresolvable id has stale state, and
    handing it "no evidence" would read as "this node has none".
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, _ = _memory(graph=InMemoryGraph(), ledger=ledger, receipts=InMemoryReceipts(), responses=())
        with pytest.raises(NodeNotFound):
            memory.explain(node_id="n-404")
    finally:
        ledger.close()


def _entry(entry_id: str, node_id: str, *, ts: datetime, claim: str) -> LedgerEntry:
    """One hand-built ledger entry, already bound to *node_id*.

    Hand-built rather than ingested because the two ``explain`` unit tests are
    about wiring — the clock and the id — and running two live-shaped stages to
    obtain one entry would make them tests of those stages instead.
    """
    return LedgerEntry(
        entry_id=entry_id,
        ts=ts,
        episode_id=EPISODE_ID,
        scope=SCOPE,
        claim=claim,
        kind=ClaimKind.IS,
        claim_mode=ClaimMode.DESCRIPTIVE,
        subjects=(GATEWAY,),
        node_ids=(node_id,),
        motive="engineering",
        confidence=0.9,
        turns=(1,),
        receipt_id="r-1",
    )


async def test_ingest_makes_exactly_two_calls_extract_then_reconcile() -> None:
    """The stage order is load-bearing: reconcile routes what extract appended.

    Asserted through the system prompts rather than a spy, so the test cannot
    pass against a pipeline that calls the right functions in the wrong order.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, transport = _memory(
            graph=InMemoryGraph(),
            ledger=ledger,
            receipts=InMemoryReceipts(),
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        outcome = await memory.ingest(_episode(), engineering_motive())
        assert transport.call_count == 2
        first, second = transport.system_prompts()
        assert "claims worth knowing" in first.lower() or "episode" in first.lower()
        assert "routing" in second.lower()
        assert isinstance(outcome, IngestOutcome)
        transport.assert_exhausted()
    finally:
        ledger.close()


async def test_ingest_appends_every_claim_unbound_then_binds_every_one_of_them() -> None:
    """Extract never binds; reconcile binds everything it routed.

    The property that makes the ledger stage 2's input queue: an entry is
    ``unbound`` exactly until it has been routed.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, _ = _memory(
            graph=InMemoryGraph(),
            ledger=ledger,
            receipts=InMemoryReceipts(),
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        outcome = await memory.ingest(_episode(), engineering_motive())
        assert outcome.entry_count == len(EXTRACT_CLAIMS)
        assert all(entry.node_ids == () for entry in outcome.entries)
        assert outcome.routed == outcome.entries
        assert ledger.unbound(scope=SCOPE) == []
        assert sum(1 for entry in outcome.entries if entry.supersedes is not None) == 1
    finally:
        ledger.close()


async def test_ingest_reconciles_the_unbound_queue_not_just_this_episodes_entries() -> None:
    """A claim stranded by a rejected reconcile is picked up by the next ingest.

    The queue is ``ledger.unbound``, so the recovery path is the ordinary path —
    there is no retry here to be a second implementation of one.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, _ = _memory(
            graph=InMemoryGraph(),
            ledger=ledger,
            receipts=InMemoryReceipts(),
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        stranded = _stranded_entry(ledger)
        outcome = await memory.ingest(_episode(), engineering_motive())
        assert stranded not in {entry.entry_id for entry in outcome.entries}
        assert stranded in {entry.entry_id for entry in outcome.routed}
        assert ledger.get(stranded).node_ids != ()
    finally:
        ledger.close()


def _stranded_entry(ledger: CavemanLedger) -> str:
    """Append one unbound entry under a surface name the scripted batch adjudicates."""
    entry = LedgerEntry(
        entry_id="e-stranded",
        ts=NOW,
        episode_id="ep-earlier",
        scope=SCOPE,
        claim="An earlier reconcile was rejected and left this claim unrouted.",
        kind=ClaimKind.ATTRIBUTE,
        claim_mode=ClaimMode.REPORT,
        subjects=(GATEWAY,),
        motive="engineering",
        confidence=0.8,
        turns=(1,),
        receipt_id="r-stranded",
    )
    ledger.append(entry)
    return entry.entry_id


async def test_dream_without_a_global_pass_runs_only_the_incremental_half() -> None:
    """``glob is None`` distinguishes "not asked for" from "ran and found nothing"."""
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        ingest_memory, ingest_transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        await ingest_memory.ingest(_episode(), engineering_motive())
        ingest_transport.assert_exhausted()

        memory, transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=dream_node_payloads(graph=graph, ledger=ledger),
        )
        outcome = await memory.dream(scope=SCOPE, motive=engineering_motive(), global_pass=False)
        assert isinstance(outcome, DreamOutcome)
        assert outcome.incremental_count == 4
        assert outcome.glob is None
        assert graph.dirty(scope=SCOPE) == []
        transport.assert_exhausted()
    finally:
        ledger.close()


async def test_a_free_global_pass_that_returns_no_ops_is_not_the_same_as_no_pass() -> None:
    """Zero ops under a budget of five hundred is a real answer, and it is receipted."""
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        ingest_memory, _ = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        await ingest_memory.ingest(_episode(), engineering_motive())

        memory, _ = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(*dream_node_payloads(graph=graph, ledger=ledger), DREAM_GLOBAL_FREE_PAYLOAD),
        )
        outcome = await memory.dream(scope=SCOPE, motive=engineering_motive(), global_pass=True)
        assert outcome.glob is not None
        assert outcome.glob.pressure == 0
        assert outcome.glob.slate == ()
        assert outcome.glob.edge_type_pressure == ()
        assert outcome.glob.ops_applied == 0
        assert outcome.glob.count_before == outcome.glob.count_after == 4
    finally:
        ledger.close()


async def test_the_receipt_stream_for_one_ingest_and_dream_is_the_documented_order() -> None:
    """Extract, reconcile's writes, its adjudications, then a write and a verdict per node.

    The stream IS the audit: it is what a prompt regression is diagnosed from, so
    its order is a contract rather than an artefact of iteration.

    Every ``graph_mutated`` in it comes from the journal ``CavemanMemory`` wraps
    its store in, and its position says which stage made the write. Reconcile
    writes its whole route before receipting any of it -- four node creations and
    four edge upserts, one of them a reinforcement -- because a raise mid-route
    must not leave half an adjudication receipted. The dreamer receipts per node,
    so each node's write lands immediately before the verdict on it.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        ingest_memory, _ = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        await ingest_memory.ingest(_episode(), engineering_motive())

        memory, _ = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(*dream_node_payloads(graph=graph, ledger=ledger), DREAM_GLOBAL_FREE_PAYLOAD),
        )
        await memory.dream(scope=SCOPE, motive=engineering_motive(), global_pass=True)
        assert [receipt.op for receipt in receipts.all(scope=SCOPE)] == [
            ReceiptOp.EXTRACT_ACCEPTED,
            # reconcile's route: four nodes created, then four edge upserts
            # (three creations and one reinforcement of the DEPLOYS triple).
            *[ReceiptOp.GRAPH_MUTATED] * 8,
            *[ReceiptOp.RECONCILE_NEW] * 6,
            # one incremental dream per node: the write, then the verdict.
            *[ReceiptOp.GRAPH_MUTATED, ReceiptOp.DREAM_NODE_APPLIED] * 4,
        ]
        mutations = [receipt for receipt in receipts.all(scope=SCOPE) if receipt.op is ReceiptOp.GRAPH_MUTATED]
        assert len(mutations) == len(ledger.events(scope=SCOPE)), (
            "every GRAPH_MUTATED receipt is carried by exactly one journalled event"
        )
    finally:
        ledger.close()


# ===================================================== the integration: 10 turns


async def test_the_ten_turn_episode_ingests_dreams_reads_and_then_merges_under_n_of_three() -> None:
    """The whole path, every store real, only the model scripted.

    Six assertions, in the order the design states them: three or more nodes,
    ``count <= N``, every line re-parses, the read for the gateway emits a ``!``
    line, and then ``max_nodes=3`` forces a merge that brings the scope to three.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        motive = engineering_motive()
        memory, transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )

        ingested = await memory.ingest(_episode(), motive)
        assert ingested.entry_count == 12
        # Six surface names, four nodes: both convergences are the scripted
        # ``new_node.name`` groups, which is the mechanism, not a collision.
        bindings = ingested.reconciled.bindings
        assert len(bindings) == 6
        assert bindings[GATEWAY] == bindings[PROXY]
        assert bindings[MCP] == bindings[C4]
        assert len(set(bindings.values())) == 4

        transport.assert_exhausted()
        # A second root over the same stores, scripted from the ids the ingest
        # minted: a fact cites its evidence, so the 3a answers are not writable
        # until the entries exist (#251 amendment D, package D-C).
        dream_memory, dream_transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(*dream_node_payloads(graph=graph, ledger=ledger), DREAM_GLOBAL_FREE_PAYLOAD),
        )
        await dream_memory.dream(scope=SCOPE, motive=motive, global_pass=True)
        dream_transport.assert_exhausted()

        nodes = graph.list_nodes(scope=SCOPE)
        assert len(nodes) >= 3
        assert graph.count(scope=SCOPE) <= motive.max_nodes
        for node in nodes:
            assert 1 <= len(_texts(node)) <= motive.max_facts_per_node
            for text in _texts(node):
                validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)
        # Every relation resolves by construction: an edge carries node ids, and
        # ``merge_nodes`` re-points the ones it absorbs (#251 amendment D).
        held = {node.node_id for node in nodes}
        for relation in graph.relations(scope=SCOPE):
            assert {relation.source_id, relation.target_id} <= held

        result = memory.read(query="what do I know about the gateway", scope=SCOPE, motive=motive)
        # Every fact this fixture answers with is an ``attribute`` -- see
        # :data:`DREAM_NODE_ANSWERS` -- so what a read can be asserted to emit
        # here is a labelled fact rather than a rule. The named kinds and their
        # render order have their own coverage in ``test_caveman_dream.py`` and
        # ``test_caveman_render.py``.
        assert all(": " in text for text in _emitted_facts(result.rendered))
        assert any("never hermetic" in text for text in _emitted_facts(result.rendered))
        assert result.line_count <= motive.read_line_budget
        assert memory.read(query="what do I know about the gateway", scope=SCOPE, motive=motive).contract_digest == (
            result.contract_digest
        )

        # ------ the forced pass: a second root over the same stores, at N=3
        squeezed = motive.model_copy(update={"max_nodes": 3})
        expected = _expected_slate(graph, ledger, squeezed)
        assert len(expected) == 1
        pair = expected[0]
        forced_memory, forced_transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(
                _forced_merge_payload(
                    pair.doomed_node_id,
                    pair.survivor_node_id,
                    survivor_name=graph.get_node(pair.survivor_node_id).name,
                    entry_ids=[entry.entry_id for entry in ledger.for_node(pair.survivor_node_id)],
                ),
            ),
        )
        forced = await forced_memory.dream(scope=SCOPE, motive=squeezed, global_pass=True)
        forced_transport.assert_exhausted()

        assert forced.incremental_count == 0, "nothing was dirty, so 3a must make zero calls"
        assert forced.glob is not None
        assert forced.glob.pressure == 1
        assert {frozenset(item.node_ids) for item in forced.glob.slate} == {frozenset(pair.node_ids)}
        assert forced.glob.count_before == 4
        assert forced.glob.count_after == 3
        assert graph.count(scope=SCOPE) <= squeezed.max_nodes
        for node in graph.list_nodes(scope=SCOPE):
            for text in _texts(node):
                validate_fact_text(text, max_fact_tokens=squeezed.max_fact_tokens)
    finally:
        ledger.close()


async def test_the_search_surface_over_the_dreamt_scope_brief_read_and_explain() -> None:
    """The three reads an agent actually makes, over the same dreamt ten turns.

    ``brief`` for the session that has no query yet, ``read`` for the one that
    does — checked here for the two EXACT seeding mechanisms, which is what
    amendment A added and what an embedding-only read could not do — and
    ``explain`` for the id the read's footer hands back. Zero LLM calls across
    all three, asserted through an exhausted script.

    The ``!``-first claim is asserted in the form the design can actually keep:
    :data:`~memotron.caveman.rank.CONSTRAINT_FLOOR` orders the RANKING, and
    the read renders per-node blocks, so "every ``!`` line before any other
    line" holds only while every constraint sits on one node. What holds always
    is the three clauses below — no constraint dropped, constraint-holding nodes
    first, and ``!`` first inside a block — and those imply the literal reading
    whenever the scope's constraints share a node.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        motive = engineering_motive()
        ingest_memory, ingest_transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )
        await ingest_memory.ingest(_episode(), motive)
        ingest_transport.assert_exhausted()

        memory, transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            responses=(*dream_node_payloads(graph=graph, ledger=ledger), DREAM_GLOBAL_FREE_PAYLOAD),
        )
        await memory.dream(scope=SCOPE, motive=motive, global_pass=True)
        transport.assert_exhausted()
        dreamt_calls = transport.call_count

        # ---- brief: the read with no query at all
        summary = memory.brief(scope=SCOPE, motive=motive)
        assert summary.query == ""
        assert summary.seeds == ()
        # The two hard rules the episode states, as the dreamer wrote them. This
        # fixture answers every fact as an ``attribute`` (see
        # :data:`DREAM_NODE_ANSWERS`), so what a brief can be asserted to keep is
        # the TEXT of those rules, not their kind. A brief that dropped one is
        # still the one thing it may not do.
        rules = ("never hermetic", "never a dated pin")
        rendered_lines = summary.rendered.splitlines()
        for wording in rules:
            assert any(wording in line for line in rendered_lines), (
                "a brief that drops a hard rule is the one thing it may not do"
            )
        for node in summary.nodes:
            assert _block_of(summary.rendered, node) == _kind_ordered(_block_of(summary.rendered, node))

        # ---- read: the two exact mechanisms, each visible as its own seed kind
        identifier_read = memory.read(query="#246", scope=SCOPE, motive=motive)
        by_246 = [hit.kind for hit in identifier_read.seeds if hit.token == "#246"]
        assert by_246 != [] and set(by_246) == {"identifier"}
        alias_read = memory.read(query=PROXY, scope=SCOPE, motive=motive)
        assert ("alias", PROXY) in [(hit.kind, hit.token) for hit in alias_read.seeds]

        # ---- explain: the id the footer named, and the record behind the cut
        for result in (summary, identifier_read, alias_read):
            assert result.rendered.splitlines()[-1].startswith("more: explain(")
        rendered_footer = summary.rendered.splitlines()[-1]
        first_id = rendered_footer.removeprefix("more: explain(").partition(")")[0].split(", ")[0]
        deep = memory.explain(node_id=first_id)
        assert deep.splitlines()[0] == _block_header(first_id, graph=graph, ledger=ledger)
        assert len(ledger.for_node(first_id)) >= 1
        assert len(deep.splitlines()) > len(_block_of(summary.rendered, graph.get_node(first_id))) + 1, (
            "explain must return more than the bounded block it is the deep read for"
        )
        assert transport.call_count == dreamt_calls, "brief, read and explain make no LLM call at all"
    finally:
        ledger.close()


# ============================== the traversal surface: node, neighbors, replay


async def _dreamt_scope(
    ledger: CavemanLedger,
) -> tuple[CavemanMemory, InMemoryGraph, InMemoryReceipts, ScriptedChatTransport]:
    """The ten turns, ingested and dreamt, over real stores and a scripted model.

    Two memories over one raw store, because a dream answer cites the ledger ids
    the ingest minted (#251 amendment D package D-C) and
    ``ScriptedChatTransport`` takes its whole script at construction. Each memory
    wraps the store in a journal of its own; both write to one ledger, which is
    the arrangement ``graph.EVENT_ID_PREFIX`` documents.

    Returns the DREAM memory, since that is the one whose journal the assertions
    are about.
    """
    graph = InMemoryGraph()
    receipts = InMemoryReceipts()
    motive = engineering_motive()
    ingest_memory, ingest_transport = _memory(
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
    )
    await ingest_memory.ingest(_episode(), motive)
    ingest_transport.assert_exhausted()

    memory, transport = _memory(
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        responses=(*dream_node_payloads(graph=graph, ledger=ledger), DREAM_GLOBAL_FREE_PAYLOAD),
    )
    await memory.dream(scope=SCOPE, motive=motive, global_pass=True)
    transport.assert_exhausted()
    return memory, graph, receipts, transport


async def test_ingest_reports_the_node_ids_the_relations_and_the_new_edge_types() -> None:
    """What an agent traverses from, and what the matcher decided, off one call.

    The ids are the handles ``node``, ``neighbors`` and ``explain`` all take, so
    an ingest that did not return them would leave an agent with a summary it
    cannot act on.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        memory, transport = _memory(
            graph=graph,
            ledger=ledger,
            receipts=InMemoryReceipts(),
            responses=(EXTRACT_PAYLOAD, RECONCILE_PAYLOAD),
        )

        outcome = await memory.ingest(_episode(), engineering_motive())

        transport.assert_exhausted()
        live = {node.node_id for node in graph.list_nodes(scope=SCOPE)}
        assert set(outcome.node_ids) == live
        assert len(outcome.node_ids) == len(set(outcome.node_ids)), "an id is reported once"
        assert outcome.node_ids[: len(outcome.reconciled.created)] == outcome.reconciled.created
        # The scripted answer types three relational claims, one of them a
        # restatement of the DEPLOYS triple -- so three edges exist and one of the
        # four upserts was a reinforcement.
        relations = graph.relations(scope=SCOPE)
        assert len(relations) == 3
        assert len(outcome.relations_created) == 3
        assert len(outcome.relations_reinforced) == 1
        assert set(outcome.edge_types_new) == set(graph.edge_types(scope=SCOPE))
    finally:
        ledger.close()


async def test_node_re_reads_one_concept_as_the_block_a_read_emitted() -> None:
    """Byte-identical to the same node inside a read, plus the calls to make next.

    One rendering, so an agent that walks to a node and an agent that found it by
    query are looking at the same text -- including the header's entry count,
    which comes from the ledger and not from summing the facts' evidence.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, graph, _, transport = await _dreamt_scope(ledger)
        before = transport.call_count
        summary = memory.brief(scope=SCOPE, motive=engineering_motive())
        node = summary.nodes[0]

        reads_after_the_brief = graph.get_node(node.node_id).read_count

        block = memory.node(node_id=node.node_id)

        assert block.splitlines()[0] == _block_header(node.node_id, graph=graph, ledger=ledger)
        assert block.splitlines()[-1] == f"more: explain({node.node_id}), neighbors({node.node_id})"
        for line in _block_of(summary.rendered, node):
            assert line in block.splitlines()
        assert transport.call_count == before, "a re-read makes no model call"
        # A pure projection: read-count policy lives in read.py, and counting a
        # traversal here would move a node up the pressure ranking for having
        # been walked past.
        assert graph.get_node(node.node_id).read_count == reads_after_the_brief
    finally:
        ledger.close()


async def test_node_refuses_an_id_it_cannot_resolve() -> None:
    """An id an agent was handed and cannot resolve is an answer, not an empty block."""
    ledger = CavemanLedger(":memory:")
    try:
        memory, _, _, _ = await _dreamt_scope(ledger)
        with pytest.raises(NodeNotFound):
            memory.node(node_id="n-404")
        with pytest.raises(NodeNotFound):
            memory.neighbors(node_id="n-404")
    finally:
        ledger.close()


async def test_neighbors_lists_every_edge_with_the_id_on_the_other_end() -> None:
    """The traversal step: both directions, and a footer naming where to walk next."""
    ledger = CavemanLedger(":memory:")
    try:
        memory, graph, _, transport = await _dreamt_scope(ledger)
        before = transport.call_count
        related = next(node for node in graph.list_nodes(scope=SCOPE) if graph.relations_of(node.node_id))
        edges = graph.relations_of(related.node_id)

        text = memory.neighbors(node_id=related.node_id)

        lines = text.splitlines()
        assert lines[0] == f"{related.name} ({related.type}) [{related.node_id}], {len(edges)} relations"
        assert len(lines) == len(edges) + 2, "a header, one line per edge, one footer"
        others = sorted({edge.target_id if edge.source_id == related.node_id else edge.source_id for edge in edges})
        for other in others:
            assert f"[{other}]" in text
        assert lines[-1] == f"more: explain({', '.join(others)}), neighbors({', '.join(others)})"
        assert transport.call_count == before
    finally:
        ledger.close()


async def test_neighbors_says_so_when_a_concept_stands_alone() -> None:
    """A sentence, not an empty string: an empty result reads as a broken call."""
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        memory, _ = _memory(graph=graph, ledger=ledger, receipts=InMemoryReceipts(), responses=())
        node = memory.graph.create_node(
            scope=SCOPE,
            name="lone",
            type="concept",
            facts=(),
            embedding=EMBED.embed("lone"),
            now=NOW,
        )

        text = memory.neighbors(node_id=node.node_id)

        assert text.splitlines() == [f"lone (concept) [{node.node_id}], 0 relations", NO_RELATIONS]
    finally:
        ledger.close()


async def test_the_memory_journals_its_own_graph_so_a_dreamt_scope_replays_equal() -> None:
    """The proof, over a scope built by a real ingest and a real dream.

    ``CavemanMemory`` wraps the store it is given, so "every mutation is a
    ``DreamEvent``" is a property of the memory and not of whoever composed it.
    The equality is what that buys: the journal accounts for the compressed graph
    belief for belief, with no model call and no embedding on the replay side.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, _, _, transport = await _dreamt_scope(ledger)
        before = transport.call_count

        proof = memory.replay(scope=SCOPE)

        assert proof.scope == SCOPE
        assert proof.equal is True
        assert proof.live_digest == proof.replayed_digest
        assert proof.event_count == len(ledger.events(scope=SCOPE))
        assert proof.event_count > 0, "a dreamt scope with no journalled events is not journalled"
        assert transport.call_count == before, "a replay consults no model"
        assert "digests equal: true" in proof.rendered
    finally:
        ledger.close()


async def test_a_write_behind_the_memorys_journal_is_what_the_proof_catches() -> None:
    """The only way the equality above means anything.

    The raw store is still reachable by the caller that composed the memory, and
    a write through it is invisible to the journal. That is the case a proof
    exists for, and it comes back as ``equal=False`` with both digests rather
    than as an exception -- the caller asked a question and "no" is the answer.
    """
    ledger = CavemanLedger(":memory:")
    try:
        memory, graph, _, _ = await _dreamt_scope(ledger)
        assert memory.replay(scope=SCOPE).equal is True

        graph.create_node(
            scope=SCOPE,
            name="smuggled",
            type="artifact",
            facts=(),
            embedding=EMBED.embed("smuggled"),
            now=NOW,
        )

        proof = memory.replay(scope=SCOPE)
        assert proof.equal is False
        assert proof.live_digest != proof.replayed_digest
    finally:
        ledger.close()


def _block_of(rendered: str, node: Node) -> list[str]:
    """One node's body lines out of a rendered read, header and footer excluded."""
    for block in rendered.split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0].startswith(f"{node.name} ({node.type}) [{node.node_id}]"):
            return lines[1:]
    raise AssertionError(f"{node.node_id} ({node.name}) has no block in the rendered read")


def _block_header(node_id: str, *, graph: InMemoryGraph, ledger: CavemanLedger) -> str:
    """The header a block carries for *node_id* — the same string ``explain`` opens with.

    One header format, rendered from one function, so a reader who deep-reads a
    block lands on a page that identifies itself the same way the block did.
    """
    return render_header(graph.get_node(node_id), entries=len(ledger.for_node(node_id)))


def _expected_slate(graph: InMemoryGraph, ledger: CavemanLedger, motive: CavemanMotive) -> tuple[MergePair, ...]:
    """The slate ``dream_global`` will compute, through ``pressure``'s own API.

    Asked for rather than hard-coded: which node ranks lowest is
    ``test_caveman_pressure.py``'s subject, and pinning a node id here would make
    this test fail for a reason that has nothing to do with the composition root.
    """
    valued = sorted(
        (_valued(node, graph=graph, motive=motive) for node in graph.list_nodes(scope=SCOPE)),
        key=lambda item: (item.value, item.node_id),
    )
    return merge_slate(
        valued,
        pressure=pressure(graph.count(scope=SCOPE), motive.max_nodes),
        cooccurrence=ledger.cooccurrence,
        turn_links=ledger.turn_cooccurrence(scope=SCOPE),
        similarity=embedding_similarity,
    )


def _valued(node: Node, *, graph: InMemoryGraph, motive: CavemanMotive) -> ValuedNode:
    return ValuedNode(
        node=node,
        value=node_value(
            node,
            degree=len(graph.relations_of(node.node_id)),
            now=NOW,
            weights=motive.value_weights,
            half_life_days=motive.recency_half_life_days,
            type_weight=motive.type_weight,
        ),
    )


async def test_a_read_folds_a_fact_stated_twice_and_reports_the_fold_in_its_receipt() -> None:
    """Two nodes, one fact, one line — and the receipt says one was folded.

    The pieces are `rank.dedupe_lines`' and the receipt detail is `read.py`'s;
    what only the root can show is that they COMPOSE: a reader who gets the
    rendered block and a reviewer who gets the receipt stream are told the same
    thing about the same read. The two lines here differ in wording and agree in
    sigil, identifier set and content tokens, which is exactly the case the
    amendment B transcript found (`× pre-#245 gateway refused Host header` on
    two nodes at once).

    Nodes are seeded through the store rather than dreamt, because a scripted
    dream that happened to write two near-identical lines would make this a test
    of the payload rather than of the fold.

    The query names BOTH nodes, so both are exact alias hits. Their embeddings
    are over their names alone, so a query naming only one of them leaves the
    other under ``knn_min_similarity`` and there is no second copy of the fact
    to fold -- which would make this a test of the kNN floor instead.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        motive = engineering_motive()
        memory, transport = _memory(graph=graph, ledger=ledger, receipts=receipts, responses=())
        twice = (
            "pre-#245 gateway refused Host header outright",
            "pre-#245 the gateway refused the Host header outright",
        )
        for name, refutation in (("gateway", twice[0]), ("agent-memory", twice[1])):
            graph.create_node(
                scope=SCOPE,
                name=name,
                type="service",
                facts=[_fact(refutation), _fact(f"{name} in the deploy path", FactKind.IS)],
                embedding=EMBED.embed(name),
                now=NOW,
            )

        result = memory.read(query="gateway agent-memory Host header", scope=SCOPE, motive=motive)

        assert result.duplicates_dropped == 1, "two lines stating one fact must be emitted once"
        kept = [text for text in result.rendered.splitlines() if text.startswith("attribute: pre-#245")]
        assert len(kept) == 1
        assert kept[0].removeprefix("attribute: ") in twice
        emitted = [
            receipt
            for receipt in receipts.all(scope=SCOPE)
            if receipt.op is ReceiptOp.READ_EMITTED and receipt.subject == result.contract_digest
        ]
        assert emitted != [], "every read owes a READ_EMITTED receipt under its own contract digest"
        assert "dropped 1 duplicate fact(s)" in emitted[-1].detail
        transport.assert_exhausted()
    finally:
        ledger.close()


async def test_a_named_node_leads_its_read_and_the_weak_neighbours_are_left_out() -> None:
    """Amendment C end to end, at the composition root.

    Nine unlinked nodes, one of them holding the alias the query uses and two of
    them holding a ``!``. The two things the live run showed a reader are both
    asserted here:

    * the block order. Before ``EXACT_FLOOR`` the two constraint-holders led and
      the node the query NAMED came third;
    * the node count. Before ``knn_min_similarity`` ``read_k=8`` filled every
      slot with neighbours measuring 0.13-0.17 and the read came back holding
      the whole scope.

    ``brief`` over the same scope is asserted alongside, unchanged: it still
    carries every node and still leads with a constraint, which is the asymmetry
    the two floors are there to create.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        motive = engineering_motive()
        memory, transport = _memory(graph=graph, ledger=ledger, receipts=receipts, responses=())

        target = graph.create_node(
            scope=SCOPE,
            name="C4 memory server",
            type="artifact",
            facts=[_fact("C4 returns 31 tools after #248")],
            embedding=EMBED.embed("C4 memory server C4 artifact C4 returns 31 tools after #248"),
            now=NOW,
            aliases=["C4"],
        )
        for index in range(8):
            name = f"unrelated-{index:02d}"
            kind = FactKind.RULE if index < 2 else FactKind.IS
            graph.create_node(
                scope=SCOPE,
                name=name,
                type="service",
                facts=[_fact(f"{name} states one fact about something else entirely", kind)],
                embedding=EMBED.embed(name),
                now=NOW,
            )
        assert graph.count(scope=SCOPE) == 9 > motive.read_k

        answered = memory.read(query="C4", scope=SCOPE, motive=motive)
        summary = memory.brief(scope=SCOPE, motive=motive)

        assert [node.node_id for node in answered.nodes] == [target.node_id]
        assert len(answered.nodes) < graph.count(scope=SCOPE)
        assert [hit.node_id for hit in answered.seeds if hit.kept] == [target.node_id]
        assert all(hit.similarity < motive.knn_min_similarity for hit in answered.seeds if not hit.kept), (
            "a candidate is only ever reported unkept because the floor refused it"
        )

        assert {node.name for node in summary.nodes} == {node.name for node in graph.list_nodes(scope=SCOPE)}
        assert summary.rendered.splitlines()[1].startswith("rule: "), "a brief still leads with a hard rule"
        assert summary.seeds == ()
        transport.assert_exhausted()
    finally:
        ledger.close()


async def test_a_query_read_excludes_part_of_a_scope_a_brief_carries_whole() -> None:
    """`read` selects, `brief` covers — the difference `read_k` makes, at the root.

    The scope is deliberately wider than one read: twelve unlinked nodes against
    ``read_k=8``, so a query can seed at most eight and 1-hop expansion has no
    edge to travel. ``brief`` has no ``read_k`` at all and ranks the whole scope,
    so it emits all twelve within the budget — and that asymmetry is the reason
    the two calls both exist rather than one being the other with a default
    query.

    This is the property `examples/caveman_demo.py`'s search section asserts
    live, stated here on a scope big enough that it cannot be an accident of how
    many nodes the model happened to create.
    """
    ledger = CavemanLedger(":memory:")
    try:
        graph = InMemoryGraph()
        receipts = InMemoryReceipts()
        motive = engineering_motive()
        memory, transport = _memory(graph=graph, ledger=ledger, receipts=receipts, responses=())
        names = tuple(f"component-{index:02d}" for index in range(12))
        for name in names:
            graph.create_node(
                scope=SCOPE,
                name=name,
                type="service",
                facts=[_fact(f"{name} is one bounded piece of the platform", FactKind.IS)],
                embedding=EMBED.embed(name),
                now=NOW,
            )
        assert graph.count(scope=SCOPE) == len(names) > motive.read_k

        answered = memory.read(query=names[0], scope=SCOPE, motive=motive)
        summary = memory.brief(scope=SCOPE, motive=motive)

        assert len(answered.seeds) <= motive.read_k
        assert len(answered.nodes) <= motive.read_k, "a query read is bounded by its seeds plus their 1-hop"
        emitted = {node.name for node in answered.nodes}
        assert names[0] in emitted, "the exact name asked for must be in the read that asked for it"
        assert set(names) - emitted != set(), "a query over a scope wider than one read must exclude something"
        assert {node.name for node in summary.nodes} == set(names), "a brief ranks the whole scope, not a neighbourhood"
        assert not summary.saturated
        transport.assert_exhausted()
    finally:
        ledger.close()
