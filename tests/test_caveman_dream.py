"""#251 units 21, 22, 23: the incremental dream, the global pass, and erasure.

Every store here is the REAL one -- ``InMemoryGraph``, ``CavemanLedger(":memory:")``,
``InMemoryReceipts``, ``LocalEmbeddingTransport`` -- and only the chat transport
is scripted, through the frozen ``ScriptedChatTransport``. That is deliberate and
it is what makes the merge and split assertions mean anything: a faked ledger
would let "rekey then record the alias" pass without the entries actually moving,
and the whole point of the merge contract is that they do.

The assertions that carry the weight, in the order they matter:

* **A rejection applies nothing.** Every 3a reject case leaves the node dirty
  with its facts unchanged; every 3b reject case leaves the graph byte-identical.
  A validation that half-applied would be worse than no validation, because the
  graph would be in a state no ledger replay produces.
* **Evidence is per fact, and it is checked against the ledger.** A cited entry
  must be bound to the node, a ``superseded`` fact needs an entry something
  superseded, and a restatement must carry the fact's existing ids. Those three
  are what make ``(x2)`` a count over the append-only store rather than a
  counter that can drift.
* **Restating a fact REINFORCES it.** The union of entry ids is asserted
  directly, along with ``first_seen`` surviving -- the belief keeps its age, which
  is half of what a reader judges it by.
* **``record_alias`` carries exactly ``rekey_node``'s return.** That equality is
  what makes an un-merge a replay rather than a guess about which entries came
  from where.
* **A split's ``entry_ids`` partition the parent's entries exactly**, and a
  part's facts may cite only its own share. Dropped, duplicated and foreign ids
  are three separate rejections, because they are three different mistakes.
* **Both mandates reach the prompt verbatim and bind the answer.** The forced
  merge slate and ``MUST COMPACT EDGE TYPES`` are each computed before the call,
  rendered into the prompt, and enforced against the response -- an
  under-delivered compaction is a rejection for the same reason an
  under-delivered merge is.

One whole section of this file is GONE with #251 amendment D, and its absence is
asserted rather than assumed: the orphan repair. A relation used to be a line
naming a node by NAME, so a merge could leave a third, untouched node pointing
at a name that no longer existed, and the global pass found those afterwards and
marked them dirty. A relation is now an edge with node ids on both ends, which
``merge_nodes`` re-points -- so a dangling target is unrepresentable and
``GlobalOutcome`` has no ``orphaned_node_ids`` to report.

So is ``demoted``. A node answer is its complete fact set, so a fact the answer
omits is gone from the node, and the second list restating which ones left was a
cross-check on a complete answer rather than a decision of its own.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from caveman_fakes import ScriptedChatTransport
from memotron.caveman.dream import (
    DREAM_GLOBAL_SYSTEM,
    DREAM_NODE_SYSTEM,
    CavemanDreamer,
    Evidence,
    compaction_targets,
)
from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import ClaimKind, ClaimMode, Fact, FactKind, LedgerEntry, Node, ReceiptOp
from memotron.caveman.motive import engineering_motive
from memotron.caveman.pressure import embedding_similarity
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.render import (
    BREVITY_RULE,
    RELATION_TARGET_RULE,
    format_prompt_block,
    render_node,
    validate_fact_text,
)
from memotron.embedding import LocalEmbeddingTransport

SCOPE = "repo:jedai/memotron"
T0 = datetime(2026, 9, 8, 16, 40, tzinfo=UTC)
EMBED = LocalEmbeddingTransport()
MOTIVE = engineering_motive()

_SLATE_PAIR = re.compile(r"MUST MERGE: '[^']*' \((n-\d+)\) INTO '[^']*' \((n-\d+)\)")
"""Matches one rendered forced-merge mandate anywhere in a global prompt.

A pattern rather than string surgery on a known line: the slate's rendering has
changed twice now (WP4 gave each pair its own block naming the names it
consumes; amendment A made the pair directional), and what these tests are
actually about is which pairs were mandated and in which direction. The prompt's
contract example writes its ids as ``["n-003", "n-011"]``, with quotes and a
comma, so it cannot match this.
"""


def _slate_pairs(prompt: str) -> list[tuple[str, str]]:
    """Every ``(doomed, survivor)`` pair the prompt mandates, in order."""
    return _SLATE_PAIR.findall(prompt)


_OVER_BUDGET_CHARS = MOTIVE.max_fact_tokens * 4 + 8
"""A body width that cannot fit the motive's ceiling, derived rather than written.

The two over-budget fixtures below were once a literal ``"x" * 80``, chosen to
exceed ``T=15``. When WP4's live calibration moved the engineering preset to
``T=20`` — which is exactly 80 characters — both fixtures became legal facts and
both tests silently stopped testing anything. Deriving the width keeps them
over budget whatever the preset says.
"""


def _entry_of(node_id: str) -> str:
    """The fixture entry bound to this node in :func:`_eight_node_harness`.

    ``n-007`` to ``e-07``. That harness binds one entry per node with matching
    numbering, and a merge has to cite a REAL entry now, so a test reading a node
    id out of a computed slate needs its entry without hard-coding the pairing.
    """
    return f"e-{node_id[-2:]}"


_SIX_NODE_ENTRY = {
    "n-001": "e-01",
    "n-002": "e-03",
    "n-003": "e-04",
    "n-004": "e-05",
    "n-005": "e-06",
    "n-006": "e-07",
}
"""One entry each node of :func:`_six_node_harness` may cite, by node id.

Written out rather than derived, because that harness deliberately binds ``e-01``
and ``e-04`` to two nodes each -- which is what gives it any co-occurrence at all
-- so the node and entry sequences diverge after the first pair. A derived map
would quietly hand a test an entry bound to the node next door, and the evidence
rule would then reject an op the test meant to be valid.
"""


class _StepClock:
    """A ``Clock`` whose reading is set by the test, never by the wall.

    Advanceable rather than frozen because ``clear_dirty`` stamps ``dreamed_at``
    from the same reading the prompt was built at, and
    ``ledger.for_node(since=...)`` is inclusive -- so a frozen clock cannot tell
    "this entry arrived after the last dream" from "this entry is the last
    dream's own input".
    """

    def __init__(self, start: datetime) -> None:
        self.reading = start

    def now(self) -> datetime:
        return self.reading


class _Harness:
    """The four real stores plus a scripted transport, wired into a dreamer."""

    def __init__(self, responses: list[str | Exception], *, start: datetime = T0) -> None:
        self.graph = InMemoryGraph()
        self.ledger = CavemanLedger(":memory:")
        self.receipts = InMemoryReceipts()
        self.transport = ScriptedChatTransport(responses)
        self.clock = _StepClock(start)
        self.dreamer = CavemanDreamer(
            graph=self.graph,
            ledger=self.ledger,
            receipts=self.receipts,
            transport=self.transport,
            embedder=EMBED,
            clock=self.clock,
        )

    def close(self) -> None:
        self.ledger.close()

    # -- fixture builders --------------------------------------------------

    def node(
        self,
        name: str,
        *,
        node_type: str = "service",
        facts: tuple[str, ...] = (),
        fact_entries: tuple[str, ...] = ("e-fixture",),
        created: datetime = T0,
        dirty: bool = True,
        dreamed_at: datetime | None = None,
    ) -> Node:
        """A node holding *facts* as plain attribute text, as a dream would leave it.

        *fact_entries* is the evidence every fixture fact carries, and it
        defaults to an id no ledger here holds. That is deliberate: a held fact's
        evidence only matters to a test that RESTATES its text -- the
        reinforcement rule requires the restatement to carry it -- so those tests
        name real entries and the rest are spared the bookkeeping.

        ``first_seen`` is *created*, not ``T0``, so a reinforcement test can
        assert that a restated fact kept the age it had.
        """
        facts_now = _fixture_facts(facts, entries=fact_entries, seen=created)
        node = self.graph.create_node(
            scope=SCOPE,
            name=name,
            type=node_type,
            facts=facts_now,
            embedding=EMBED.embed(f"{name} {node_type}"),
            now=created,
        )
        node = self.graph.replace_facts(
            node.node_id,
            facts=facts_now,
            type=node_type,
            embedding=EMBED.embed(f"{name} {node_type}"),
            now=created,
        )
        if dreamed_at is not None:
            self.graph.clear_dirty([node.node_id], now=dreamed_at)
        if not dirty:
            self.graph.clear_dirty([node.node_id], now=dreamed_at or created)
        elif dreamed_at is not None:
            self.graph.mark_dirty([node.node_id])
        return self.graph.get_node(node.node_id)

    def entry(
        self,
        entry_id: str,
        *,
        nodes: tuple[str, ...],
        claim: str = "The JedAI Gateway is a LiteLLM proxy that fronts the JedAI models.",
        episode: str = "ep-1",
        kind: ClaimKind = ClaimKind.ATTRIBUTE,
        mode: ClaimMode = ClaimMode.DESCRIPTIVE,
        ts: datetime = T0,
        identifiers: tuple[str, ...] = (),
        supersedes: str | None = None,
        turns: tuple[int, ...] = (1,),
    ) -> LedgerEntry:
        record = LedgerEntry(
            entry_id=entry_id,
            ts=ts,
            episode_id=episode,
            scope=SCOPE,
            claim=claim,
            kind=kind,
            claim_mode=mode,
            subjects=("the gateway",),
            identifiers=identifiers,
            node_ids=nodes,
            motive=MOTIVE.name,
            confidence=0.9,
            supersedes=supersedes,
            turns=turns,
            receipt_id="r-fixture",
        )
        self.ledger.append(record)
        return record

    def relate_from_ledger(self, *node_ids: str) -> None:
        """Upsert one edge per pair the LEDGER ties together, as reconcile would.

        Replaces ``rebuild_weights`` in the fixtures that were asserting on a
        derived weight: the pairs are the ones a claim actually named, so a
        fixture cannot hand ``pressure`` an edge the ledger does not support.
        """
        for node_id in node_ids:
            for other_id in self.ledger.cooccurrence(node_id):
                if node_id < other_id:
                    shared = tuple(
                        entry.entry_id for entry in self.ledger.for_node(node_id) if other_id in entry.node_ids
                    )
                    self.relate(node_id, other_id, entries=shared)

    def relate(
        self,
        source_id: str,
        target_id: str,
        *,
        type: str = "MENTIONED_WITH",
        claim: str = "one claim named both of these",
        entries: tuple[str, ...] = ("e-01",),
    ) -> None:
        """One edge between two nodes, as reconcile would have left it.

        Direction is taken as given rather than normalised, because the edge type
        tests below need a known source: an answer states its relations FROM the
        node being dreamed, so a fixture that silently swapped the ends would
        make a legitimate rewrite look like a re-point.
        """
        self.graph.upsert_relation(
            scope=SCOPE,
            source_id=source_id,
            target_id=target_id,
            type=type,
            claim=claim,
            entry_ids=entries,
            until=None,
            now=self.clock.reading,
        )

    def ops(self, op: ReceiptOp) -> list[str]:
        return [receipt.subject for receipt in self.receipts.all(scope=SCOPE) if receipt.op is op]

    def snapshot(self) -> list[tuple[str, str, str, tuple[str, ...], bool]]:
        """Everything a rejection must leave untouched."""
        return [
            (node.node_id, node.name, node.type, _texts(node), node.dirty)
            for node in self.graph.list_nodes(scope=SCOPE)
        ]

    def edges(self) -> list[tuple[str, str, str, tuple[str, ...]]]:
        """Every edge as ``(source, type, target, entry_ids)``. The other half of a snapshot."""
        return [
            (relation.source_id, relation.type, relation.target_id, relation.entry_ids)
            for relation in self.graph.relations(scope=SCOPE)
        ]


def _fixture_facts(
    texts: tuple[str, ...],
    *,
    entries: tuple[str, ...] = ("e-fixture",),
    seen: datetime = T0,
) -> tuple[Fact, ...]:
    """*texts* as attribute facts, evidenced by *entries* and first seen at *seen*."""
    return tuple(
        Fact(kind=FactKind.ATTRIBUTE, text=text, entry_ids=entries, first_seen=seen, last_seen=seen) for text in texts
    )


def _texts(node: Node) -> tuple[str, ...]:
    """A node's fact TEXT, which is what most assertions in this file are about."""
    return tuple(fact.text for fact in node.facts)


# -- the answer contracts, as the model sends them ---------------------------


def _fact(
    text: str,
    *,
    kind: str = "attribute",
    key: str | None = None,
    entries: tuple[str, ...] = ("e-01",),
) -> dict[str, Any]:
    """One :class:`~memotron.caveman.models.FactSpec` payload."""
    return {"kind": kind, "key": key, "text": text, "entry_ids": list(entries)}


def _facts(*texts: str, entries: tuple[str, ...] = ("e-01",)) -> list[dict[str, Any]]:
    """Several attribute facts sharing one evidence list. The common shape."""
    return [_fact(text, entries=entries) for text in texts]


def _relation(
    target_id: str,
    *,
    type: str = "BLOCKED",
    claim: str = "the Host header rewrite was refused",
    entries: tuple[str, ...] = ("e-01",),
    relation_id: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """One :class:`~memotron.caveman.models.RelationSpec` payload."""
    return {
        "relation_id": relation_id,
        "type": type,
        "target_id": target_id,
        "claim": claim,
        "entry_ids": list(entries),
        "until": until,
    }


def _node_response(
    facts: list[dict[str, Any]],
    *,
    node_type: str = "service",
    relations: list[dict[str, Any]] | None = None,
    retired: tuple[str, ...] = (),
    reason: str = "supersession applied",
) -> str:
    return json.dumps(
        {
            "type": node_type,
            "facts": facts,
            "relations": relations or [],
            "retired_relation_ids": list(retired),
            "reason": reason,
        }
    )


def _global_response(*ops: dict[str, Any]) -> str:
    return json.dumps({"ops": list(ops)})


def _merge(
    nodes: tuple[str, ...],
    *,
    name: str,
    node_type: str,
    facts: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "op": "merge",
        "nodes": list(nodes),
        "survivor_name": name,
        "survivor_type": node_type,
        "facts": facts,
        "reason": "one concept under two surface names",
    }


def _rewrite(
    node: str,
    *,
    facts: list[dict[str, Any]],
    relations: list[dict[str, Any]] | None = None,
    retired: tuple[str, ...] = (),
    reason: str = "deduped against a neighbour",
) -> dict[str, Any]:
    return {
        "op": "rewrite",
        "node": node,
        "facts": facts,
        "relations": relations or [],
        "retired_relation_ids": list(retired),
        "reason": reason,
    }


def _part(
    name: str,
    node_type: str,
    facts: list[dict[str, Any]],
    entries: tuple[str, ...],
    *,
    relation_ids: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "name": name,
        "type": node_type,
        "facts": facts,
        "entry_ids": list(entries),
        "relation_ids": list(relation_ids),
    }


def _embedding_of(
    name: str,
    node_type: str,
    texts: tuple[str, ...],
    *,
    aliases: tuple[str, ...] = (),
) -> list[float]:
    """What the dreamer must have embedded: name, aliases, type, then the fact text.

    The aliases are part of the embedding text (#251 amendment A, C0), so a
    merge survivor -- the only node in this file that HAS aliases -- embeds
    differently from a node of the same name and facts that absorbed nothing.
    """
    head = f"{name} {' '.join(aliases)} {node_type}" if aliases else f"{name} {node_type}"
    return EMBED.embed("\n".join((head, *texts)))


# ======================================================= unit 21: incremental


@pytest.fixture
def three_dirty() -> _Harness:
    """Three dirty nodes, each with one new claim, and a script for each."""
    # ``dirty()`` is name-ordered, so the script is too: c4, chart, gateway.
    harness = _Harness(
        [
            _node_response(_facts("returns 31 tools after #248", entries=("e-03",)), node_type="cluster"),
            _node_response(_facts("deploys the MCP server, #240", entries=("e-02",)), node_type="artifact"),
            _node_response(_facts("chat default claude-haiku-4-5", entries=("e-01",))),
        ]
    )
    gateway = harness.node("gateway")
    chart = harness.node("chart", node_type="artifact")
    c4 = harness.node("c4", node_type="cluster")
    harness.entry("e-01", nodes=(gateway.node_id,), identifiers=("claude-haiku-4-5",))
    harness.entry("e-02", nodes=(chart.node_id, gateway.node_id), identifiers=("#240",))
    harness.entry("e-03", nodes=(c4.node_id,), identifiers=("#248", "31"))
    return harness


@pytest.mark.asyncio
async def test_incremental_redreams_every_dirty_node_exactly_once(three_dirty: _Harness) -> None:
    harness = three_dirty
    dreamed = await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert harness.transport.call_count == 3
    harness.transport.assert_exhausted()
    assert [node.name for node in dreamed] == ["c4", "chart", "gateway"]
    assert harness.graph.dirty(scope=SCOPE) == []
    assert harness.ops(ReceiptOp.DREAM_NODE_APPLIED) == [node.node_id for node in dreamed]
    for node in harness.graph.list_nodes(scope=SCOPE):
        assert node.dreamed_at == T0
        assert node.dirty is False
    harness.close()


@pytest.mark.asyncio
async def test_the_returned_facts_and_type_are_what_the_node_holds(three_dirty: _Harness) -> None:
    harness = three_dirty
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    gateway = harness.graph.node_by_name(scope=SCOPE, name="gateway")
    chart = harness.graph.node_by_name(scope=SCOPE, name="chart")
    assert gateway is not None and chart is not None
    assert _texts(gateway) == ("chat default claude-haiku-4-5",)
    assert gateway.type == "service"
    assert _texts(chart) == ("deploys the MCP server, #240",)
    assert chart.type == "artifact"
    harness.close()


@pytest.mark.asyncio
async def test_an_answered_facts_kind_key_and_evidence_are_all_stored(three_dirty: _Harness) -> None:
    """The named fields are the point of the contract, so all four are asserted.

    Under the D0 bridge every answered line became an ``attribute`` evidenced by
    every entry on the node. A fact now arrives with its own kind, its own label
    and its own evidence, and each of those has to survive the write or the
    contract bought nothing.
    """
    harness = _Harness(
        [
            _node_response(
                [
                    _fact("real runs always via the gateway", kind="rule", entries=("e-01",)),
                    _fact("claude-haiku-4-5", key="chat default", entries=("e-01",)),
                    _fact("affinity is lost about 1 call in 20", kind="unsure", entries=("e-01",)),
                ]
            )
        ]
    )
    harness.node("gateway")
    harness.entry("e-01", nodes=("n-001",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    facts = harness.graph.get_node("n-001").facts
    assert [fact.kind for fact in facts] == [FactKind.RULE, FactKind.ATTRIBUTE, FactKind.UNSURE]
    assert [fact.key for fact in facts] == [None, "chat default", None]
    assert all(fact.entry_ids == ("e-01",) for fact in facts)
    assert all(fact.first_seen == T0 and fact.last_seen == T0 for fact in facts)
    harness.close()


@pytest.mark.asyncio
async def test_the_embedding_is_recomputed_over_name_type_and_facts(three_dirty: _Harness) -> None:
    harness = three_dirty
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    gateway = harness.graph.node_by_name(scope=SCOPE, name="gateway")
    assert gateway is not None
    assert list(gateway.embedding) == _embedding_of("gateway", "service", _texts(gateway))
    harness.close()


_SCRAMBLED = (
    ("unsure", "session-affinity loss about 1 in 20"),
    ("attribute", "chat default claude-haiku-4-5"),
    ("rule", "real runs always via the gateway, never hermetic"),
    ("is", "LiteLLM proxy fronting the JedAI models"),
    ("attribute", "embedding text-embedding-3, 3072 dims"),
)
"""Five facts in the order a model happened to answer with them, kinds mixed.

Deliberately shuffled across kinds: ``render.sort_facts`` orders rules, then
what the thing IS, then its attributes, then what is unsure, and the only way to
assert that is to answer in an order that is none of those.
"""

_ORDERED = (
    "real runs always via the gateway, never hermetic",
    "LiteLLM proxy fronting the JedAI models",
    "chat default claude-haiku-4-5",
    "embedding text-embedding-3, 3072 dims",
    "session-affinity loss about 1 in 20",
)
"""What the node stores: ``FACT_ORDER``, stable within a kind.

The two attributes keep the model's own sequencing relative to each other --
``chat default`` before ``embedding`` -- because the sort is stable and that is
the half of the model's ordering worth keeping.
"""


@pytest.mark.asyncio
async def test_an_incremental_dream_stores_its_facts_in_kind_order() -> None:
    """A reader skims by kind, so the order is the reader's, not the model's.

    ``render.sort_facts`` runs after validation and before the embedding, so the
    vector covers exactly the text the node stores.
    """
    harness = _Harness([_node_response([_fact(text, kind=kind, entries=("e-01",)) for kind, text in _SCRAMBLED])])
    harness.node("gateway")
    harness.node("chart", node_type="artifact", dirty=False)
    harness.entry("e-01", nodes=("n-001",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    node = harness.graph.get_node("n-001")
    assert _texts(node) == _ORDERED
    assert list(node.embedding) == _embedding_of("gateway", "service", _ORDERED)
    harness.close()


@pytest.mark.asyncio
async def test_a_retyped_node_is_re_embedded_under_its_new_type() -> None:
    """The type is in the embedding text, so a type change must move the vector."""
    harness = _Harness([_node_response(_facts("a rule, never a service"), node_type="policy")])
    harness.node("rule", node_type="service")
    harness.entry("e-01", nodes=("n-001",))
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    node = harness.graph.get_node("n-001")
    assert node.type == "policy"
    assert list(node.embedding) == _embedding_of("rule", "policy", _texts(node))
    assert list(node.embedding) != _embedding_of("rule", "service", _texts(node))
    harness.close()


@pytest.mark.asyncio
async def test_a_fact_the_answer_omits_leaves_the_node_and_stays_in_the_ledger() -> None:
    """What ``demoted`` used to say, said by the contract instead.

    The answer is the node's COMPLETE fact set, so omitting a fact removes it --
    no second list, no cross-check. The claim that supported it is untouched in
    the ledger, which is what makes the removal a compression decision rather
    than a loss.
    """
    harness = _Harness([_node_response(_facts("chat default claude-haiku-4-5"))])
    harness.node(
        "gateway",
        facts=("Host header refused", "chat default claude-haiku-4-5"),
        fact_entries=("e-01",),
    )
    harness.entry("e-01", nodes=("n-001",), claim="Before #245 the gateway refused our Host header outright.")

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    node = harness.graph.get_node("n-001")
    assert _texts(node) == ("chat default claude-haiku-4-5",)
    surviving = harness.ledger.for_node("n-001")
    assert [entry.entry_id for entry in surviving] == ["e-01"]
    assert "Host header" in surviving[0].claim
    harness.close()


# ------------------------------------------------- reinforcement and evidence


@pytest.mark.asyncio
async def test_restating_a_fact_reinforces_it_with_the_union_of_its_evidence() -> None:
    """The reinforcement rule, asserted on the union (#251 amendment D).

    The node already holds this fact on ``e-01``. A second claim arrives, the
    model restates the same text carrying BOTH ids, and the record ends up with
    two entries behind one belief -- which is what ``evidence`` counts and what
    ``(x2)`` renders. ``first_seen`` is the other half: the belief is the same
    belief, so it keeps the age it had rather than looking new again.
    """
    created = T0 - timedelta(days=2)
    harness = _Harness([_node_response(_facts("chat default is claude-haiku-4-5", entries=("e-01", "e-02")))])
    harness.node(
        "gateway",
        facts=("chat default is claude-haiku-4-5",),
        fact_entries=("e-01",),
        created=created,
        dreamed_at=T0 - timedelta(days=1),
    )
    harness.entry("e-01", nodes=("n-001",), ts=created)
    harness.entry("e-02", nodes=("n-001",), ts=T0, claim="Confirmed again: the chat default is claude-haiku-4-5.")

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    (fact,) = harness.graph.get_node("n-001").facts
    assert fact.entry_ids == ("e-01", "e-02")
    assert fact.evidence == 2
    assert fact.first_seen == created
    assert fact.last_seen == T0
    harness.close()


@pytest.mark.asyncio
async def test_a_restatement_that_drops_the_facts_existing_evidence_is_rejected() -> None:
    """Reinforcement is not optional. Dropping the old ids would weaken the record.

    Sending only the new entry would turn a twice-attested belief back into a
    once-attested one -- the record would say less after the second claim than
    before it, which is the opposite of what evidence is for.
    """
    harness = _Harness([_node_response(_facts("chat default is claude-haiku-4-5", entries=("e-02",)))])
    harness.node(
        "gateway",
        facts=("chat default is claude-haiku-4-5",),
        fact_entries=("e-01",),
        dreamed_at=T0 - timedelta(days=1),
    )
    harness.entry("e-01", nodes=("n-001",), ts=T0 - timedelta(days=2))
    harness.entry("e-02", nodes=("n-001",), ts=T0)

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any("dropped e-01" in error for error in raised.value.errors), raised.value.errors
    assert any("reinforces a belief" in error for error in raised.value.errors), raised.value.errors
    assert harness.graph.get_node("n-001").facts[0].entry_ids == ("e-01",)
    harness.close()


@pytest.mark.asyncio
async def test_a_fact_citing_an_entry_bound_to_another_node_is_rejected() -> None:
    """Evidence has to be evidence FOR this node, not just an id that exists.

    ``e-02`` is a real ledger entry about the chart. A gateway fact resting on it
    is unsupported however true it reads, and the prompt lists exactly which ids
    this node may cite -- so this is a misread of the prompt rather than a
    judgement call.
    """
    harness = _Harness([_node_response(_facts("deploys the MCP server, #240", entries=("e-02",)))])
    harness.node("gateway")
    harness.node("chart", node_type="artifact", dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-002",))

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any("cites e-02, which is not a ledger entry this node may cite" in e for e in raised.value.errors), (
        raised.value.errors
    )
    harness.close()


@pytest.mark.asyncio
async def test_a_superseded_fact_is_kept_when_the_ledger_supersedes_its_entry() -> None:
    """History is knowledge when the claim that replaced it is on the record.

    The engineering motive keeps superseded facts for 90 days, and this is the
    case it is for: the old chat default stops a reader re-deriving the wrong
    answer. It is not rendered in a read -- that is ``explain``'s job -- but it is
    on the node, evidenced by the entry that WAS superseded.
    """
    harness = _Harness(
        [
            _node_response(
                [
                    _fact("claude-sonnet-4-6", key="chat default", entries=("e-new",)),
                    _fact("the chat default used to be claude-haiku-4-5", kind="superseded", entries=("e-old",)),
                ]
            )
        ]
    )
    harness.node("gateway", dreamed_at=T0 - timedelta(days=1))
    harness.entry("e-old", nodes=("n-001",), ts=T0 - timedelta(days=2), identifiers=("claude-haiku-4-5",))
    harness.entry(
        "e-new",
        nodes=("n-001",),
        ts=T0,
        mode=ClaimMode.CORRECTION,
        supersedes="e-old",
        claim="The chat default moved to claude-sonnet-4-6 this morning.",
        identifiers=("claude-sonnet-4-6",),
    )

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    facts = {fact.kind: fact for fact in harness.graph.get_node("n-001").facts}
    assert facts[FactKind.ATTRIBUTE].text == "claude-sonnet-4-6"
    assert facts[FactKind.SUPERSEDED].entry_ids == ("e-old",)
    harness.close()


@pytest.mark.asyncio
async def test_history_needs_a_witness_that_something_replaced_the_claim() -> None:
    """Otherwise a live fact can be mislabelled as history and hidden from every read.

    A ``superseded`` fact is not rendered in a bounded read, so marking a current
    fact that way removes it from every answer while leaving it on the node --
    the worst of both, and undetectable without this rule.

    Here the node holds NOTHING and the ledger supersedes nothing, so neither
    witness exists: this claim has never been a live fact on this node, and
    nothing replaced it.
    """
    harness = _Harness(
        [_node_response([_fact("the chat default is claude-haiku-4-5", kind="superseded", entries=("e-01",))])]
    )
    harness.node("gateway")
    harness.entry("e-01", nodes=("n-001",))

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any("which nothing shows was replaced" in error for error in raised.value.errors), raised.value.errors
    harness.close()


@pytest.mark.asyncio
async def test_an_answer_that_still_treats_an_entry_as_LIVE_cannot_also_call_it_history() -> None:
    """The mislabelling this rule exists to catch, in the form it can actually arrive.

    One claim, two facts in one answer: one live, one ``superseded``, both citing
    the same entry. Whatever the wording, that entry supports something the
    answer keeps, so it is not history -- and accepting it would hide the
    ``superseded`` half from every read for no reason a reader could see.
    """
    harness = _Harness(
        [
            _node_response(
                [
                    _fact("chat default is claude-haiku-4-5", entries=("e-01",)),
                    _fact("the default used to be something else", kind="superseded", entries=("e-01",)),
                ]
            )
        ]
    )
    harness.node("gateway", facts=("chat default is claude-haiku-4-5",), fact_entries=("e-01",))
    harness.entry("e-01", nodes=("n-001",))

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any("which nothing shows was replaced" in error for error in raised.value.errors), raised.value.errors
    harness.close()


@pytest.mark.asyncio
async def test_an_answer_that_RETIRES_a_held_fact_may_record_it_as_history() -> None:
    """The second witness, and the case the ledger structurally cannot link.

    ``ClaimSpec.supersedes_claim_index`` points at a SIBLING in one extract
    response, so a claim replaced by a LATER EPISODE is never marked superseded
    in the ledger -- and a chat default that moved yesterday is exactly that.
    Four live runs answered ``superseded`` there, correctly, and were rejected
    (three of them after a prompt fix aimed at talking the model out of it).

    The witness the dreamer CAN provide is its own answer: the entry supported a
    live fact on this node, and this answer no longer keeps it live. That is
    "stated and then replaced", and the dreamer is the only party that can see
    across episodes to say so.
    """
    harness = _Harness(
        [
            _node_response(
                [
                    _fact("chat default is claude-sonnet-4-6", entries=("e-02",)),
                    _fact("chat default was claude-haiku-4-5", kind="superseded", entries=("e-01",)),
                ]
            )
        ]
    )
    harness.node("gateway", facts=("chat default is claude-haiku-4-5",), fact_entries=("e-01",))
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-001",))

    (node,) = await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    kinds = {fact.text: fact.kind for fact in node.facts}
    assert kinds["chat default is claude-sonnet-4-6"] is FactKind.ATTRIBUTE
    assert kinds["chat default was claude-haiku-4-5"] is FactKind.SUPERSEDED
    # And the superseded half is invisible to a read while staying on the node.
    assert "claude-haiku-4-5" not in render_node(node, entries=2)
    harness.close()


@pytest.mark.asyncio
async def test_the_prompt_shows_every_held_fact_with_the_entries_that_evidence_it() -> None:
    """The reinforcement rule is unsatisfiable unless the ids are beside the fact.

    A model cannot carry evidence it was never shown, so a prompt that rendered
    the facts without their entry ids would make every restatement a rejection.
    This is the half of the prompt that makes the contract answerable.
    """
    harness = _Harness([_node_response(_facts("chat default is claude-haiku-4-5", entries=("e-01",)))])
    harness.node("gateway", facts=("chat default is claude-haiku-4-5",), fact_entries=("e-01",))
    harness.entry("e-01", nodes=("n-001",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "attribute: chat default is claude-haiku-4-5  [e-01]" in prompt
    assert "LEDGER ENTRY IDS THIS NODE MAY CITE AS EVIDENCE\n  e-01" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_the_prompt_marks_which_entries_the_ledger_supersedes() -> None:
    """``superseded`` is only legal for a superseded entry, so the prompt says which."""
    harness = _Harness([_node_response(_facts("returns 31 tools after #248", entries=("e-new",)))])
    harness.node("c4", node_type="cluster")
    harness.entry("e-old", nodes=("n-001",), identifiers=("24",))
    harness.entry("e-new", nodes=("n-001",), mode=ClaimMode.CORRECTION, supersedes="e-old", identifiers=("31",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "superseded by a later entry, so a superseded fact may cite it: e-old" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_the_prompt_says_when_NOTHING_is_superseded_rather_than_falling_silent() -> None:
    """The negative, stated. A prompt that omits a rule has not stated it.

    The ledger links a supersession only WITHIN one extract response, so a value
    a later EPISODE replaced is not superseded in the ledger and no fact may be
    that kind. This block used to print the citable ids and stop, leaving the
    model to infer "so none of them is superseded" from an absent sentence -- and
    a live run where a second episode plainly moved an attribute answered with a
    ``superseded`` fact that the validation then refused, losing the whole
    answer.
    """
    harness = _Harness([_node_response(_facts("chat default claude-sonnet-4-6", entries=("e-new",)))])
    # No facts held and nothing superseded in the ledger, so NEITHER witness of a
    # replacement is reachable and the kind is withheld.
    harness.node("gateway")
    harness.entry("e-old", nodes=("n-001",))
    harness.entry("e-new", nodes=("n-001",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "NONE of these is superseded by a later entry, so no fact in this answer may be kind 'superseded'." in prompt
    assert "superseded by a later entry, so a superseded fact may cite it" not in prompt
    # And the shared FACT KINDS block does not OFFER it, which is the half that
    # cost three live runs: a model reading a list of five kinds and a
    # prohibition in prose picks from the list, so the list is the prohibition.
    without_history = tuple(kind for kind in FactKind if kind is not FactKind.SUPERSEDED)
    assert format_prompt_block(max_facts=MOTIVE.max_facts_per_node, kinds=without_history) in prompt
    assert format_prompt_block(max_facts=MOTIVE.max_facts_per_node, kinds=tuple(FactKind)) not in prompt
    # The MOTIVE block still names the kind, and that is deliberate. A motive is
    # a policy over a whole scope -- how long history is kept, what it is worth --
    # and filtering it by one node's ledger state would make the persona depend
    # on the entry. What the answer may SEND is the format block's list.
    assert f"{FactKind.SUPERSEDED.value.upper()} FACTS: keep for" in prompt
    harness.close()


def test_the_usable_kinds_are_derived_from_the_same_field_the_validation_reads() -> None:
    """One source of truth, so the prompt cannot offer a kind the answer is rejected for."""
    nothing_superseded = Evidence(bound=frozenset({"e-01"}), superseded=frozenset(), held={})
    assert nothing_superseded.usable_fact_kinds == (
        FactKind.RULE,
        FactKind.IS,
        FactKind.ATTRIBUTE,
        FactKind.UNSURE,
    )
    with_history = Evidence(bound=frozenset({"e-01", "e-02"}), superseded=frozenset({"e-01"}), held={})
    assert with_history.usable_fact_kinds == tuple(FactKind)
    assert FactKind.SUPERSEDED in with_history.usable_fact_kinds


# ---------------------------------------------- relations on the node answer


@pytest.mark.asyncio
async def test_a_node_answer_creates_a_typed_edge_to_another_node() -> None:
    """A belief between two concepts is an EDGE, and the node pass may write one."""
    harness = _Harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting the JedAI models"),
                relations=[_relation("n-002", type="BLOCKED", until="#245", entries=("e-01",))],
            ),
        ]
    )
    harness.node("gateway")
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    (edge,) = harness.graph.relations(scope=SCOPE)
    assert edge.triple == ("n-001", "BLOCKED", "n-002")
    assert edge.claim == "the Host header rewrite was refused"
    assert edge.until == "#245"
    assert edge.entry_ids == ("e-01",)
    assert edge.evidence == 1
    harness.close()


@pytest.mark.asyncio
async def test_restating_an_edge_reinforces_it_rather_than_duplicating_it() -> None:
    """The same triple stated again unions its evidence and moves ``last_seen``.

    Reinforcement for edges is the store's, through the triple identity -- so a
    second claim about the same relationship makes the record stronger instead of
    making a second record. Two edges saying one thing would make ``evidence``
    count how often somebody wrote it down.
    """
    harness = _Harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting the JedAI models", entries=("e-01",)),
                relations=[_relation("n-002", entries=("e-02",))],
            ),
        ]
    )
    harness.node("gateway", dreamed_at=T0 - timedelta(days=1))
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",), ts=T0 - timedelta(days=2))
    harness.entry("e-02", nodes=("n-001",), ts=T0)
    harness.relate("n-001", "n-002", type="BLOCKED", entries=("e-01",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    (edge,) = harness.graph.relations(scope=SCOPE)
    assert edge.entry_ids == ("e-01", "e-02")
    assert edge.evidence == 2
    assert edge.last_seen == T0
    harness.close()


@pytest.mark.asyncio
async def test_a_relation_id_rewrites_that_edges_claim_and_clears_its_marker() -> None:
    """A named edge is a rewrite of its claim and ``until``, never of its triple.

    Clearing ``until`` has to be expressible -- "it was fixed, and then it
    regressed" -- so ``None`` is applied as given rather than treated as "leave
    it alone".
    """
    harness = _Harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting the JedAI models", entries=("e-01",)),
                relations=[
                    _relation(
                        "n-002",
                        relation_id="r-001",
                        claim="the Host rewrite is refused again since the rollback",
                        entries=("e-01", "e-02"),
                        until=None,
                    )
                ],
            ),
        ]
    )
    harness.node("gateway", dreamed_at=T0 - timedelta(days=1))
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",), ts=T0 - timedelta(days=2))
    harness.entry("e-02", nodes=("n-001",), ts=T0)
    harness.graph.upsert_relation(
        scope=SCOPE,
        source_id="n-001",
        target_id="n-002",
        type="BLOCKED",
        claim="the Host header rewrite was refused",
        entry_ids=("e-01",),
        until="#245",
        now=T0 - timedelta(days=2),
    )

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    (edge,) = harness.graph.relations(scope=SCOPE)
    assert edge.relation_id == "r-001"
    assert edge.claim == "the Host rewrite is refused again since the rollback"
    assert edge.until is None
    assert edge.entry_ids == ("e-01", "e-02")
    harness.close()


@pytest.mark.asyncio
async def test_a_retired_edge_leaves_the_graph_and_is_receipted() -> None:
    """Retirement is what a belief that no longer holds gets instead of an edit."""
    harness = _Harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting the JedAI models"),
                retired=("r-001",),
                reason="#245 fixed the rewrite, so nothing blocks now",
            ),
        ]
    )
    harness.node("gateway")
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    harness.relate("n-001", "n-002", type="BLOCKED", entries=("e-01",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert harness.graph.relations(scope=SCOPE) == []
    assert harness.ops(ReceiptOp.DREAM_RELATION_RETIRED) == ["r-001"]
    harness.close()


@pytest.mark.asyncio
async def test_the_prompt_names_every_edge_by_the_id_an_answer_uses() -> None:
    """An answer rewrites and retires by ``relation_id``, so the prompt leads with it."""
    harness = _Harness([_node_response(_facts("LiteLLM proxy fronting the JedAI models"))])
    harness.node("gateway")
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    harness.relate("n-001", "n-002", type="BLOCKED", entries=("e-01",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "[r-001] BLOCKED agent-memory [n-002]: one claim named both of these  [e-01]" in prompt
    assert "EDGE TYPES IN THIS SCOPE: BLOCKED (1)" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_an_incoming_edge_is_shown_as_context_and_carries_no_id() -> None:
    """A node dream owns the edges running OUT of it and nothing else.

    A ``RelationSpec`` names no source, so a relation in a node answer runs out
    of the node being dreamed -- which makes an INCOMING edge a belief the
    concept at its other end holds, and not this answer's to rewrite or retire.
    The prompt shows it without its ``relation_id``, because a rewritable id next
    to an unrewritable edge is exactly what cost the first live run under this
    contract: the model named the id it was shown and the whole answer was
    rejected.
    """
    harness = _Harness([_node_response(_facts("an MCP server reached through the gateway", entries=("e-02",)))])
    harness.node("gateway", dirty=False)
    harness.node("agent-memory", node_type="artifact")
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-002",))
    # n-001 -> n-002: OUTGOING for the gateway, INCOMING for agent-memory, which
    # is the node being dreamed here.
    harness.relate("n-001", "n-002", type="FRONTS", entries=("e-01",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "THIS NODE'S OWN RELATIONS (0)" in prompt
    assert "WHAT OTHER CONCEPTS BELIEVE ABOUT THIS ONE (1)" in prompt
    assert "[r-001]" not in prompt
    assert "from gateway [n-001] FRONTS: one claim named both of these  [e-01]" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_an_answer_cannot_rewrite_or_retire_an_edge_that_runs_INTO_its_node() -> None:
    """The rejection the prompt split exists to prevent, asserted rather than assumed."""
    for relations, retired, expected in (
        ([_relation("n-001", type="FRONTS", relation_id="r-001", entries=("e-02",))], (), "is not an edge of n-002"),
        ([], ("r-001",), "retired relation r-001 is not an edge of n-002"),
    ):
        harness = _Harness(
            [
                _node_response(
                    _facts("an MCP server reached through the gateway", entries=("e-02",)),
                    relations=relations,
                    retired=retired,
                )
            ]
        )
        harness.node("gateway", dirty=False)
        harness.node("agent-memory", node_type="artifact")
        harness.entry("e-01", nodes=("n-001",))
        harness.entry("e-02", nodes=("n-002",))
        harness.relate("n-001", "n-002", type="FRONTS", entries=("e-01",))
        edges_before = harness.edges()

        with pytest.raises(OutOfContractResponse) as raised:
            await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

        assert any(expected in error for error in raised.value.errors), raised.value.errors
        assert harness.edges() == edges_before
        harness.close()


@pytest.mark.asyncio
async def test_the_prompt_offers_every_other_concept_with_its_node_id() -> None:
    """A relation names its target by ID, so a name-only list makes one unwritable."""
    harness = _Harness(
        [
            _node_response(_facts("returns 31 tools after #248", entries=("e-03",)), node_type="cluster"),
            _node_response(_facts("deploys the MCP server, #240", entries=("e-02",)), node_type="artifact"),
            _node_response(_facts("chat default claude-haiku-4-5", entries=("e-01",))),
        ]
    )
    harness.node("gateway")
    harness.node("chart", node_type="artifact")
    harness.node("c4", node_type="cluster")
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-002",))
    harness.entry("e-03", nodes=("n-003",))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.calls[0].prompt
    assert "  n-001 | gateway | service" in prompt
    assert "  n-002 | chart | artifact" in prompt
    # The node being dreamed is not offered as a target for its own relations.
    assert "  n-003 | c4 | cluster" not in prompt
    # The rule the id list exists to serve, stated where it is TRUE. Reconcile
    # embeds the same format block and names an edge's ends by SURFACE NAME, so
    # this sentence belongs to the dream prompts and to no shared block --
    # ``render.RELATION_TARGET_RULE``.
    assert RELATION_TARGET_RULE in prompt
    harness.close()


@pytest.mark.parametrize(
    ("relations", "retired", "expected"),
    [
        ([_relation("n-001")], (), "points n-001 at itself"),
        ([_relation("n-404")], (), "which is not a node in this scope"),
        ([_relation("n-002", entries=("e-02",))], (), "which is not a ledger entry this node may cite"),
        ([_relation("n-002", relation_id="r-404")], (), "is not an edge of n-001"),
        ([_relation("n-002", type="DEPLOYS", relation_id="r-001")], (), "state the new edge and retire this one"),
        ([], ("r-404",), "retired relation r-404 is not an edge of n-001"),
    ],
    ids=["self-loop", "unknown-target", "unbound-evidence", "unknown-relation-id", "retype-as-rewrite", "retire-alien"],
)
@pytest.mark.asyncio
async def test_a_relation_out_of_contract_rejects_and_applies_nothing(
    relations: list[dict[str, Any]], retired: tuple[str, ...], expected: str
) -> None:
    """Every relation rule, each leaving the graph byte-identical.

    The edges are snapshotted as well as the nodes: a relation rule that
    half-applied would leave an edge no ledger replay produces, which is the one
    outcome worse than a rejection.
    """
    harness = _Harness(
        [_node_response(_facts("LiteLLM proxy fronting the JedAI models"), relations=relations, retired=retired)]
    )
    harness.node("gateway")
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-002",))
    harness.relate("n-001", "n-002", type="BLOCKED", entries=("e-01",))
    nodes_before, edges_before = harness.snapshot(), harness.edges()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any(expected in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == nodes_before
    assert harness.edges() == edges_before
    assert harness.graph.get_node("n-001").dirty is True
    harness.close()


@pytest.mark.asyncio
async def test_an_edge_rewritten_and_retired_in_one_answer_is_refused_by_the_contract() -> None:
    """Two verdicts on one belief, caught by the response model rather than the stage.

    Response-internal, so it is the wire contract's rule: applying both would
    make the outcome depend on which is applied second.
    """
    harness = _Harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting the JedAI models"),
                relations=[_relation("n-002", relation_id="r-001")],
                retired=("r-001",),
            )
        ]
    )
    harness.node("gateway")
    harness.node("agent-memory", dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    harness.relate("n-001", "n-002", type="BLOCKED", entries=("e-01",))

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert any("both rewritten and retired" in error for error in raised.value.errors), raised.value.errors
    assert len(harness.graph.relations(scope=SCOPE)) == 1
    harness.close()


# ------------------------------------------------------------ prompt contents


@pytest.mark.asyncio
async def test_the_prompt_is_built_from_the_entries_since_dreamed_at() -> None:
    """A claim the node has already been dreamed against must not be re-sent.

    ``for_node(since=)`` is inclusive, so the old entry is placed strictly before
    the watermark and the new one strictly after -- which is the only arrangement
    that distinguishes "already seen" from "arrived since".

    The old entry is still CITABLE: the citable list is every entry bound to the
    node, because a fact the node keeps is still evidenced by the claim that
    first stated it. What must not reappear is the claim TEXT.
    """
    harness = _Harness([_node_response(_facts("returns 31 tools after #248", entries=("e-new",)), node_type="cluster")])
    dreamed_at = T0 - timedelta(hours=1)
    harness.node("c4", node_type="cluster", created=T0 - timedelta(days=2), dreamed_at=dreamed_at)
    harness.entry(
        "e-old",
        nodes=("n-001",),
        claim="C4 is returning 24 tools right now, so the surface is basically complete.",
        ts=dreamed_at - timedelta(hours=1),
        identifiers=("24",),
    )
    harness.entry(
        "e-new",
        nodes=("n-001",),
        claim="The C4 memory server returns 31 tools, not 24; 24 was the count before #248.",
        ts=dreamed_at + timedelta(hours=1),
        identifiers=("31", "#248"),
    )

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    new_claims = prompt.split("NEW CLAIMS SINCE THIS NODE WAS LAST DREAMED")[1]
    assert "[e-new]" in new_claims
    assert "[e-old]" not in new_claims
    assert "e-new, e-old" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_the_prompt_carries_the_system_intent_format_motive_and_ids(three_dirty: _Harness) -> None:
    harness = three_dirty
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    call = harness.transport.calls[0]
    assert call.system_prompt == DREAM_NODE_SYSTEM
    assert "HOW A BELIEF IS RECORDED" in call.prompt
    assert f"KEEP AT MOST {MOTIVE.max_facts_per_node} FACTS ON ONE NODE." in call.prompt
    assert f"KEEP AT MOST {MOTIVE.max_edge_types} DISTINCT RELATION TYPES IN THIS SCOPE." in call.prompt
    assert MOTIVE.dream_rubric[0] in call.prompt
    assert "Answer with one JSON object and nothing else." in call.prompt
    # Every scope node's ID is offered as a relation target, so a legitimate
    # relation cannot be rejected for pointing at a node the prompt hid.
    for node_id, name in (("n-001", "gateway"), ("n-002", "chart")):
        assert f"  {node_id} | {name} | " in call.prompt
    harness.close()


@pytest.mark.parametrize(
    ("stage", "system_prompt"),
    [("node", DREAM_NODE_SYSTEM), ("global", DREAM_GLOBAL_SYSTEM)],
)
def test_both_dream_prompts_ask_for_brevity_and_ask_nobody_to_count(stage: str, system_prompt: str) -> None:
    """Brevity is the requirement; a count is not (#251 amendment A).

    Three prompts stated a ceiling -- in characters, then in words, then in
    characters again ending "Count them" -- and each moved the overrun by about
    two characters without removing it. A model cannot count characters, so the
    ask is now the one sentence both prompts share, with no number in it, and
    ``T`` survives only as a paragraph guard the prompt does not state.

    Compared with the newlines collapsed, because where the sentence wraps is
    the formatter's business; and the banned strings are the exact wordings that
    were tried, so this test fails if any of them comes back.
    """
    flat = " ".join(system_prompt.split())
    assert " ".join(BREVITY_RULE.split()) in flat, stage
    assert "Nothing here asks you to count characters or words" in flat, stage
    for banned in ("character ceiling", "Count the characters", "Count them", "token ceiling", "word ceiling"):
        assert banned not in flat, f"{stage} prompt asks the model to count: {banned!r}"


@pytest.mark.parametrize(
    ("stage", "system_prompt"),
    [("node", DREAM_NODE_SYSTEM), ("global", DREAM_GLOBAL_SYSTEM)],
)
def test_both_dream_prompts_state_the_evidence_and_reinforcement_rules(stage: str, system_prompt: str) -> None:
    """Both passes write facts, so both have to be told what evidence is for.

    A prompt that asked for ``entry_ids`` without saying a restatement must carry
    the existing ones would produce a rejection on every reinforcement, and the
    model would never learn why from the prompt it was given.
    """
    flat = " ".join(system_prompt.split())
    assert "NAMES ITS EVIDENCE" in flat, stage
    assert "REINFORCE" in flat, stage
    assert "METADATA IS NEVER A FACT" in flat, stage


@pytest.mark.asyncio
async def test_the_worked_example_in_the_prompt_obeys_the_budget_it_states() -> None:
    """A prompt that demonstrates a violation of its own contract blames the model."""
    harness = _Harness([_node_response(_facts("chat default claude-haiku-4-5"))])
    harness.node("gateway")
    harness.entry("e-01", nodes=("n-001",))
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    contract = next(line for line in prompt.splitlines() if line.startswith('{"type"'))
    payload = json.loads(contract)
    assert set(payload) == {"type", "facts", "relations", "retired_relation_ids", "reason"}
    for fact in payload["facts"]:
        validate_fact_text(fact["text"], max_fact_tokens=MOTIVE.max_fact_tokens)
    harness.close()


@pytest.mark.asyncio
async def test_a_motive_whose_token_ceiling_cannot_express_a_fact_fails_fast() -> None:
    tiny = MOTIVE.model_copy(update={"max_fact_tokens": 4})
    harness = _Harness([_node_response(_facts("x"))])
    harness.node("gateway")
    harness.entry("e-01", nodes=("n-001",))
    with pytest.raises(ValueError, match="cannot demonstrate its own contract"):
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=tiny)
    harness.close()


@pytest.mark.asyncio
async def test_an_empty_dirty_set_makes_no_call() -> None:
    harness = _Harness([])
    harness.node("gateway", dirty=False)
    assert await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE) == []
    assert harness.transport.call_count == 0
    harness.close()


# ------------------------------------------------------ unit 21: reject cases

_ORIGINAL_FACTS = ("chat default claude-haiku-4-5",)


def _rejecting_harness(response: str) -> _Harness:
    harness = _Harness([response])
    harness.node("gateway", facts=_ORIGINAL_FACTS, fact_entries=("e-01",))
    harness.node("chart", node_type="artifact", facts=(), dirty=False)
    harness.entry("e-01", nodes=("n-001",))
    return harness


@pytest.mark.parametrize(
    ("response", "why"),
    [
        (
            _node_response(_facts(*[f"fact number {index} of nine" for index in range(9)])),
            "nine facts at L=8",
        ),
        (
            _node_response(_facts("x" * _OVER_BUDGET_CHARS)),
            f"a body over the T={MOTIVE.max_fact_tokens} token ceiling",
        ),
        (
            _node_response(_facts("the gateway probably refuses the Host header")),
            "a hedge word the unsure kind exists for",
        ),
        (
            _node_response(_facts("conf: 0.97; turns: 6")),
            "the live run's leaked provenance line",
        ),
        (
            _node_response(_facts("entries = e-01, e-02")),
            "ledger entry ids are provenance too",
        ),
        (
            _node_response(_facts("date: 2026-09-08")),
            "a fact whose whole text is a date",
        ),
        (
            _node_response(_facts("chat default claude-haiku-4-5", entries=("e-404",))),
            "an entry id no ledger holds",
        ),
        (
            _node_response(
                [
                    _fact("chat default claude-haiku-4-5", entries=("e-01",)),
                    _fact("the default used to be haiku", kind="superseded", entries=("e-01",)),
                ]
            ),
            "history for an entry the same answer still keeps live",
        ),
    ],
    ids=[
        "too-many-facts",
        "over-token-budget",
        "hedged",
        "metadata-confidence",
        "metadata-entry-ids",
        "metadata-date",
        "unbound-evidence",
        "superseded-without-supersession",
    ],
)
@pytest.mark.asyncio
async def test_a_rejected_node_stays_dirty_and_unchanged(response: str, why: str) -> None:
    harness = _rejecting_harness(response)
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert raised.value.source == "DREAM_NODE"
    assert raised.value.errors, why
    assert harness.snapshot() == before
    node = harness.graph.get_node("n-001")
    assert node.dirty is True
    assert _texts(node) == _ORIGINAL_FACTS
    assert node.dreamed_at is None
    assert harness.ops(ReceiptOp.DREAM_NODE_REJECTED) == ["n-001"]
    assert harness.ops(ReceiptOp.DREAM_NODE_APPLIED) == []
    harness.close()


@pytest.mark.asyncio
async def test_the_provenance_rejection_names_the_fact_and_says_why() -> None:
    """The #246 defect, and the message a reader of the receipt gets.

    The prompt shows every claim with ``conf=`` and ``turns=`` beside it, and the
    live run copied that into a fact. One rejected fact is cheap; a node holding
    ``conf: 0.97; turns: 6`` costs a slot of eight forever.
    """
    harness = _rejecting_harness(_node_response(_facts("conf: 0.97; turns: 6", "chat default claude-haiku-4-5")))

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert raised.value.errors == (
        "facts: 'conf: 0.97; turns: 6' states provenance, not a fact -- confidence, turn numbers, "
        "entry ids and dates are shown beside a claim as bookkeeping and are never a fact",
    )
    assert "METADATA IS NEVER A FACT" in DREAM_NODE_SYSTEM
    harness.close()


@pytest.mark.asyncio
async def test_a_fact_that_merely_mentions_a_date_or_a_count_is_kept() -> None:
    """The control. The rule is anchored, so it rejects provenance, not arithmetic.

    Without the anchor, "31 tools after #248" and "fixed in #245 on 2026-08-24"
    would both go -- and identifiers are the highest-value tokens in the system.
    """
    kept = (
        "returns 31 tools after #248",
        "session-affinity loss about 1 in 20, probe #246 names it",
        "Host header refused pre-#245, fixed 2026-08-24",
    )
    harness = _rejecting_harness(_node_response(_facts(*kept)))
    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    assert set(_texts(harness.graph.get_node("n-001"))) == set(kept)
    harness.close()


@pytest.mark.asyncio
async def test_provenance_in_a_global_op_is_rejected_too() -> None:
    """3b writes facts as well, so the same rule runs on every op's fact set."""
    harness = _six_node_harness([_global_response(_rewrite("n-001", facts=_facts("ts=1757353200", entries=("e-01",))))])
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any("states provenance" in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == before
    assert "METADATA IS NEVER A FACT" in DREAM_GLOBAL_SYSTEM
    harness.close()


@pytest.mark.asyncio
async def test_a_wire_contract_violation_rejects_through_the_same_path() -> None:
    """A payload pydantic refuses must land as the same op and the same exception."""
    harness = _rejecting_harness('{"type": "service", "facts": [], "reason": "empty"}')
    with pytest.raises(OutOfContractResponse):
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    assert harness.ops(ReceiptOp.DREAM_NODE_REJECTED) == ["n-001"]
    assert harness.graph.get_node("n-001").dirty is True
    harness.close()


@pytest.mark.asyncio
async def test_one_fact_sent_twice_is_refused_by_the_wire_contract() -> None:
    """Response-internal, so the record catches it: one text cannot be two facts."""
    harness = _rejecting_harness(
        _node_response(_facts("chat default claude-haiku-4-5", "chat default claude-haiku-4-5"))
    )
    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)
    assert any("sent more than once" in error for error in raised.value.errors), raised.value.errors
    harness.close()


@pytest.mark.asyncio
async def test_nodes_applied_before_a_rejection_stay_applied() -> None:
    """Per-node atomicity, not per-batch: earlier in-contract work is not punished."""
    harness = _Harness(
        [
            _node_response(_facts("returns 31 tools after #248", entries=("e-01",)), node_type="cluster"),
            _node_response(_facts("the gateway definitely probably refuses Host", entries=("e-02",))),
        ]
    )
    harness.node("c4", node_type="cluster")
    harness.node("gateway", facts=_ORIGINAL_FACTS, fact_entries=("e-02",))
    harness.entry("e-01", nodes=("n-001",))
    harness.entry("e-02", nodes=("n-002",))

    with pytest.raises(OutOfContractResponse):
        await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert _texts(harness.graph.get_node("n-001")) == ("returns 31 tools after #248",)
    assert harness.graph.get_node("n-001").dirty is False
    assert _texts(harness.graph.get_node("n-002")) == _ORIGINAL_FACTS
    assert harness.graph.get_node("n-002").dirty is True
    harness.close()


# ------------------------------------------------- unit 21: integration


@pytest.mark.asyncio
async def test_a_reconcile_shaped_dirty_set_dreams_to_a_clean_bounded_graph() -> None:
    """Five nodes straight out of reconcile: zero facts, all dirty, all re-dreamed."""
    responses = [
        _node_response(
            [
                _fact("returns 31 tools after #248", entries=("e-01",)),
                _fact("deployed by the chart, #240", entries=("e-01",)),
            ],
            node_type="cluster",
        ),
        _node_response(_facts("deploys the MCP server, #240", entries=("e-02",)), node_type="artifact"),
        _node_response(
            [
                _fact("real runs always via the gateway, never hermetic", kind="rule", entries=("e-03",)),
                _fact("LiteLLM proxy fronting the JedAI models", kind="is", entries=("e-03",)),
                _fact("claude-haiku-4-5", key="chat default", entries=("e-03",)),
            ],
        ),
        _node_response(_facts("names the affinity failure, committed", entries=("e-05",)), node_type="probe"),
        _node_response(
            [_fact("session-affinity loss about 1 in 20, probe #246", kind="unsure", entries=("e-04",))],
            node_type="defect",
        ),
    ]
    harness = _Harness(responses)
    for name, node_type in (
        ("c4", "cluster"),
        ("chart", "artifact"),
        ("gateway", "service"),
        ("session-affinity", "defect"),
        ("probe-246", "probe"),
    ):
        harness.node(name, node_type=node_type, facts=())
    for index in range(1, 6):
        harness.entry(f"e-{index:02d}", nodes=(f"n-{index:03d}",))

    dreamed = await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert len(dreamed) == 5
    assert harness.graph.dirty(scope=SCOPE) == []
    for node in harness.graph.list_nodes(scope=SCOPE):
        assert 1 <= len(_texts(node)) <= MOTIVE.max_facts_per_node
        for text in _texts(node):
            validate_fact_text(text, max_fact_tokens=MOTIVE.max_fact_tokens)
    harness.close()


# ============================================================ unit 22: global


def _six_node_harness(responses: list[str | Exception]) -> _Harness:
    """Six dreamed nodes, one entry each bound with matching numbering, two edges."""
    harness = _Harness(responses)
    shape = (
        ("gateway", "service", ("LiteLLM proxy fronting JedAI models",)),
        ("litellm-proxy", "service", ("the same proxy under a second name",)),
        ("chart", "artifact", ("deploys the MCP server, #240",)),
        ("c4", "cluster", ("returns 31 tools after #248",)),
        ("probe-246", "probe", ("names the affinity failure, committed",)),
        ("host-header", "defect", ("refused before #245, fixed in #245",)),
    )
    for index, (name, node_type, facts) in enumerate(shape):
        harness.node(
            name,
            node_type=node_type,
            facts=facts,
            fact_entries=(_SIX_NODE_ENTRY[f"n-{index + 1:03d}"],),
            dirty=False,
            dreamed_at=T0 - timedelta(days=index),
        )
    harness.entry("e-01", nodes=("n-001", "n-002"))
    harness.entry("e-02", nodes=("n-001",))
    harness.entry("e-03", nodes=("n-002",))
    harness.entry("e-04", nodes=("n-003", "n-004"))
    harness.entry("e-05", nodes=("n-004",))
    harness.entry("e-06", nodes=("n-005",))
    harness.entry("e-07", nodes=("n-006",))
    # The edges the ledger's own co-occurrence implies: e-01 names n-001 and
    # n-002, e-04 names n-003 and n-004. Written as the relations reconcile
    # would have upserted, which is what a merge re-points.
    harness.relate("n-001", "n-002", entries=("e-01",))
    harness.relate("n-003", "n-004", entries=("e-04",))
    return harness


@pytest.mark.asyncio
async def test_a_merge_rekeys_the_ledger_and_records_the_alias() -> None:
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="gateway",
                    node_type="service",
                    facts=_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)),
                )
            )
        ]
    )
    absorbed_before = [entry.entry_id for entry in harness.ledger.for_node("n-002")]
    assert absorbed_before == ["e-01", "e-03"]

    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert outcome.count_before == 6
    assert outcome.count_after == 5
    assert outcome.ops_applied == 1
    assert harness.graph.get_nodes(["n-002"]) == []
    survivor = harness.graph.get_node("n-001")
    assert survivor.name == "gateway"
    assert _texts(survivor) == ("LiteLLM proxy fronting JedAI models",)
    # The absorbed node's name became a survivor alias, so a query for the name
    # the merge destroyed still lands here -- and the vector covers it too.
    assert survivor.aliases == ("litellm-proxy",)
    assert list(survivor.embedding) == _embedding_of("gateway", "service", _texts(survivor), aliases=survivor.aliases)

    aliases = harness.ledger.aliases("n-001")
    assert len(aliases) == 1
    assert aliases[0].alias_node_id == "n-002"
    # The alias carries EXACTLY what rekey_node moved, which is what makes an
    # un-merge a replay rather than a guess about which entries came from where.
    assert aliases[0].moved_entry_ids == tuple(absorbed_before)
    assert {entry.entry_id for entry in harness.ledger.for_node("n-001")} == {"e-01", "e-02", "e-03"}
    assert harness.ops(ReceiptOp.DREAM_MERGED) == ["n-001"]
    assert aliases[0].receipt_id in {receipt.receipt_id for receipt in harness.receipts.all()}
    harness.close()


@pytest.mark.asyncio
async def test_a_merge_survivors_fact_may_rest_on_an_absorbed_nodes_entry() -> None:
    """The re-key moves the evidence, so the union is the right set to judge against.

    ``e-03`` is bound to the absorbed node before the merge and to the survivor
    after it. Judging the survivor's facts against its own pre-merge entries
    would make a merge unable to state the very thing it is collapsing.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="gateway",
                    node_type="service",
                    facts=_facts("one proxy under two surface names", entries=("e-03",)),
                )
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    (fact,) = harness.graph.get_node("n-001").facts
    assert fact.entry_ids == ("e-03",)
    assert "e-03" in {entry.entry_id for entry in harness.ledger.for_node("n-001")}
    harness.close()


@pytest.mark.asyncio
async def test_a_merge_re_points_the_absorbed_edge_and_drops_the_self_loop() -> None:
    """The edge rebuild is GONE, and this is what replaced it (#251 amendment D).

    ``n-003`` and ``n-004`` are related to each other, so merging them makes that
    edge a self-loop -- which the store drops, because nothing relates to
    itself. Nothing rebuilds anything afterwards: an edge carries its own claim,
    which the ledger cannot re-derive, so re-pointing is the only lossless move
    and the survivor's edges are whatever re-pointing leaves.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-003", "n-004"),
                    name="chart",
                    node_type="artifact",
                    facts=_facts("deploys the MCP server, #240", entries=("e-04",)),
                )
            )
        ]
    )
    assert [relation.relation_id for relation in harness.graph.relations_of("n-003")] == ["r-002"]
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    # e-04 bound both, so after the re-key it binds only the survivor: no
    # co-occurrence left, and the edge between them became a self-loop.
    assert harness.ledger.cooccurrence("n-003") == {}
    assert harness.graph.relations_of("n-003") == []
    harness.close()


@pytest.mark.asyncio
async def test_a_merge_may_absorb_more_than_two_nodes() -> None:
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002", "n-005"),
                    name="gateway",
                    node_type="service",
                    facts=_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)),
                )
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert outcome.count_after == 4
    assert [alias.alias_node_id for alias in harness.ledger.aliases("n-001")] == ["n-002", "n-005"]
    assert {entry.entry_id for entry in harness.ledger.for_node("n-001")} == {"e-01", "e-02", "e-03", "e-06"}
    harness.close()


@pytest.mark.asyncio
async def test_the_node_whose_name_is_kept_survives_and_keeps_its_whole_identity() -> None:
    """``survivor_name`` picks the RECORD, not just the label (#251 amendment A).

    ``litellm-proxy`` is ``n-002``, the higher id, so the rule this replaced --
    lowest id survives -- would have kept ``n-001`` and renamed it. Then a merge
    could rename a live concept, which is the whole defect: the survivor keeps
    its own name, so the record that keeps the name has to be the record that
    lives.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="litellm-proxy",
                    node_type="service",
                    facts=_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)),
                )
            )
        ]
    )
    named = harness.graph.get_node("n-002")
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert harness.graph.get_nodes(["n-001"]) == []
    survivor = harness.graph.get_node("n-002")
    assert survivor.name == "litellm-proxy"
    assert survivor.created_at == named.created_at
    assert survivor.ledger_key == named.ledger_key
    assert survivor.read_count == named.read_count
    # The absorbed node's name is not lost -- it routes here now.
    assert survivor.aliases == ("gateway",)
    assert harness.graph.node_by_alias(scope=SCOPE, name="gateway") == survivor
    # The ledger moved the other way round too: n-001's entries are n-002's now.
    assert [alias.alias_node_id for alias in harness.ledger.aliases("n-002")] == ["n-001"]
    assert {entry.entry_id for entry in harness.ledger.for_node("n-002")} == {"e-01", "e-02", "e-03"}
    harness.close()


@pytest.mark.asyncio
async def test_an_invented_survivor_name_is_rejected_and_applies_nothing() -> None:
    """The ``chart-models`` case: a name belonging to neither concept.

    A compound of two names is the shape the live run produced, and it is the
    reason the rule exists -- a search for either half lands on a node named for
    neither. Rejected before anything is applied, so the pass is byte-identical.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="gateway-proxy",
                    node_type="service",
                    facts=_facts("merged", entries=("e-01",)),
                )
            )
        ]
    )
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any("a merge keeps its survivor's name" in error for error in raised.value.errors), raised.value.errors
    assert any("n-001='gateway'" in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == before
    assert harness.ops(ReceiptOp.DREAM_MERGED) == []
    harness.close()


@pytest.mark.asyncio
async def test_a_survivor_name_may_identify_a_node_by_one_of_its_aliases() -> None:
    """An alias is a name this scope has actually seen, so it identifies a node.

    What it does NOT do is rename the survivor: the node keeps the spelling it
    already holds, and the alias stays an alias.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="the-gateway",
                    node_type="service",
                    facts=_facts("merged", entries=("e-01",)),
                )
            )
        ]
    )
    harness.graph.add_aliases("n-001", ["the-gateway"])

    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    survivor = harness.graph.get_node("n-001")
    assert survivor.name == "gateway"
    assert set(survivor.aliases) == {"the-gateway", "litellm-proxy"}
    harness.close()


@pytest.mark.asyncio
async def test_a_survivor_name_in_another_case_identifies_the_node_without_recasing_it() -> None:
    """Case-insensitive to identify, exact to store: ``GATEWAY`` is not a rename."""
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="GATEWAY",
                    node_type="service",
                    facts=_facts("merged", entries=("e-01",)),
                )
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert harness.graph.get_node("n-001").name == "gateway"
    harness.close()


@pytest.mark.asyncio
async def test_every_global_op_stores_its_facts_in_kind_order() -> None:
    """The same order on both writing paths, so a read never depends on which ran.

    A merge and a rewrite both replace a whole fact set, and 3b is as much a
    writer of facts as 3a is.
    """
    scrambled = [_fact(text, kind=kind, entries=("e-01",)) for kind, text in _SCRAMBLED]
    harness = _six_node_harness(
        [
            _global_response(
                _merge(("n-001", "n-002"), name="gateway", node_type="service", facts=scrambled),
                _rewrite(
                    "n-003",
                    facts=[_fact(text, kind=kind, entries=("e-04",)) for kind, text in _SCRAMBLED],
                    reason="reordered",
                ),
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    survivor = harness.graph.get_node("n-001")
    assert _texts(survivor) == _ORDERED
    assert list(survivor.embedding) == _embedding_of("gateway", "service", _ORDERED, aliases=("litellm-proxy",))
    assert _texts(harness.graph.get_node("n-003")) == _ORDERED
    harness.close()


@pytest.mark.asyncio
async def test_a_split_partitions_the_parents_entries_and_writes_both_parts() -> None:
    harness = _six_node_harness(
        [
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part(
                            "c4-cluster",
                            "cluster",
                            _facts("the cluster the memory server runs on", entries=("e-04",)),
                            ("e-04",),
                        ),
                        _part(
                            "c4-tool-count",
                            "metric",
                            _facts("returns 31 tools after #248", entries=("e-05",)),
                            ("e-05",),
                        ),
                    ],
                    "reason": "a cluster and a measured count are separate concepts",
                }
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert outcome.count_after == 7
    assert harness.graph.get_nodes(["n-004"]) == []
    parts = {node.name: node for node in harness.graph.list_nodes(scope=SCOPE) if node.name.startswith("c4-")}
    assert set(parts) == {"c4-cluster", "c4-tool-count"}
    cluster, count = parts["c4-cluster"], parts["c4-tool-count"]
    assert [entry.entry_id for entry in harness.ledger.for_node(cluster.node_id)] == ["e-04"]
    assert [entry.entry_id for entry in harness.ledger.for_node(count.node_id)] == ["e-05"]
    # e-04 still binds n-003, so the LEDGER still ties the cluster part to it --
    # but the parent's edge went with the parent, because no part claimed it.
    assert harness.ledger.cooccurrence(cluster.node_id) == {"n-003": 1}
    assert harness.graph.relations_of(cluster.node_id) == []
    assert harness.graph.relations_of(count.node_id) == []
    # The parts are dirty, and the dreamer wrote their facts after the re-key --
    # a fact carries the entry ids that evidence it, so it cannot be built until
    # the entries have moved.
    assert cluster.dirty is True
    assert _texts(cluster) == ("the cluster the memory server runs on",)
    assert harness.graph.get_node(cluster.node_id).facts[0].entry_ids == ("e-04",)
    assert harness.ops(ReceiptOp.DREAM_SPLIT) == ["n-004"]
    harness.close()


@pytest.mark.asyncio
async def test_a_split_part_may_keep_one_of_the_parents_edges() -> None:
    """``relation_ids`` is how a belief survives the node it hung off.

    An edge no part claims goes with the parent, because a split deletes the
    endpoint. A part that names it keeps it, re-pointed -- which is what stops a
    split from being a lossy operation for the graph's edges.
    """
    harness = _six_node_harness(
        [
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part(
                            "c4-cluster",
                            "cluster",
                            _facts("the cluster the memory server runs on", entries=("e-04",)),
                            ("e-04",),
                            relation_ids=("r-002",),
                        ),
                        _part(
                            "c4-tool-count",
                            "metric",
                            _facts("returns 31 tools after #248", entries=("e-05",)),
                            ("e-05",),
                        ),
                    ],
                    "reason": "the cluster keeps the chart relationship",
                }
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    kept = harness.graph.node_by_name(scope=SCOPE, name="c4-cluster")
    assert kept is not None
    (edge,) = harness.graph.relations_of(kept.node_id)
    assert {edge.source_id, edge.target_id} == {"n-003", kept.node_id}
    harness.close()


@pytest.mark.asyncio
async def test_a_split_part_citing_the_other_parts_evidence_is_rejected() -> None:
    """A part's facts may rest only on the entries that part takes.

    The point of a split is that two topics were sharing one node; a part whose
    fact rests on the other part's evidence has not been separated from it, and
    after the re-key that entry is not even bound to it any more.
    """
    harness = _six_node_harness(
        [
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part("a", "cluster", _facts("part a", entries=("e-05",)), ("e-04",)),
                        _part("b", "metric", _facts("part b", entries=("e-05",)), ("e-05",)),
                    ],
                    "reason": "part a cites part b's entry",
                }
            )
        ]
    )
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any("split part a" in error and "e-05" in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == before
    harness.close()


@pytest.mark.asyncio
async def test_a_retype_keeps_the_facts_and_moves_the_vector() -> None:
    harness = _six_node_harness(
        [_global_response({"op": "retype", "node": "n-001", "type": "policy", "reason": "a rule, not a service"})]
    )
    before = harness.graph.get_node("n-001")
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    after = harness.graph.get_node("n-001")
    assert after.type == "policy"
    assert _texts(after) == _texts(before)
    assert list(after.embedding) == _embedding_of("gateway", "policy", _texts(after))
    assert harness.ops(ReceiptOp.DREAM_RETYPED) == ["n-001"]
    harness.close()


@pytest.mark.asyncio
async def test_a_rewrite_replaces_the_fact_set_and_keeps_the_type() -> None:
    harness = _six_node_harness(
        [
            _global_response(
                _rewrite(
                    "n-001",
                    facts=_facts(
                        "LiteLLM proxy fronting JedAI models",
                        "chat default claude-haiku-4-5",
                        entries=("e-01",),
                    ),
                    reason="deduped against the litellm-proxy node",
                )
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    node = harness.graph.get_node("n-001")
    assert _texts(node) == ("LiteLLM proxy fronting JedAI models", "chat default claude-haiku-4-5")
    assert node.type == "service"
    assert harness.ops(ReceiptOp.DREAM_NODE_APPLIED) == ["n-001"]
    harness.close()


@pytest.mark.asyncio
async def test_a_rewrite_may_turn_two_nodes_facts_into_one_edge_between_them() -> None:
    """The op the global view exists for, and the only one carrying ``relations``.

    Two nodes each holding half of one relationship is exactly what a whole-scope
    read can see and a per-node dream cannot, so the repair -- write the edge,
    drop the facts -- has to be expressible in one op.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _rewrite(
                    "n-001",
                    facts=_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)),
                    relations=[
                        _relation(
                            "n-003",
                            type="DEPLOYED_BY",
                            claim="the chart deploys it",
                            entries=("e-02",),
                        )
                    ],
                    reason="the pairing was two facts on two nodes",
                )
            )
        ]
    )
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    edge = next(relation for relation in harness.graph.relations(scope=SCOPE) if relation.type == "DEPLOYED_BY")
    assert edge.triple == ("n-001", "DEPLOYED_BY", "n-003")
    assert edge.entry_ids == ("e-02",)
    harness.close()


@pytest.mark.asyncio
async def test_a_rewrite_relation_out_of_contract_applies_nothing() -> None:
    """A rewrite's edges face the same rules the node pass applies to its own."""
    harness = _six_node_harness(
        [
            _global_response(
                _rewrite(
                    "n-001",
                    facts=_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)),
                    relations=[_relation("n-404", type="DEPLOYED_BY", entries=("e-01",))],
                )
            )
        ]
    )
    nodes_before, edges_before = harness.snapshot(), harness.edges()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any("not a node in this scope" in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == nodes_before
    assert harness.edges() == edges_before
    harness.close()


@pytest.mark.asyncio
async def test_zero_ops_is_a_valid_pass() -> None:
    harness = _six_node_harness([_global_response()])
    before = harness.snapshot()
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert outcome == type(outcome)(
        count_before=6, count_after=6, ops_applied=0, pressure=0, slate=(), edge_type_pressure=()
    )
    assert harness.snapshot() == before
    harness.close()


@pytest.mark.asyncio
async def test_an_empty_scope_makes_no_call() -> None:
    harness = _Harness([])
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert outcome.ops_applied == 0
    assert outcome.edge_type_pressure == ()
    assert harness.transport.call_count == 0
    harness.close()


@pytest.mark.asyncio
async def test_the_global_prompt_carries_the_budgets_inventory_and_at_risk_detail() -> None:
    harness = _six_node_harness([_global_response()])
    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    call = harness.transport.last
    assert call.system_prompt == DREAM_GLOBAL_SYSTEM
    assert f"NODE BUDGET: 6 of {MOTIVE.max_nodes} used. PRESSURE: 0" in call.prompt
    assert f"EDGE TYPE BUDGET: 1 of {MOTIVE.max_edge_types} used" in call.prompt
    assert RELATION_TARGET_RULE in call.prompt
    assert "EDGE TYPES IN THIS SCOPE: MENTIONED_WITH (2)" in call.prompt
    assert "FORCED MERGE SLATE -- MANDATORY, AND ALREADY DECIDED\n(none -- there is no pressure)" in call.prompt
    assert _slate_pairs(call.prompt) == []
    # Nothing is over M, so the compaction block is absent rather than empty.
    assert "MUST COMPACT EDGE TYPES" not in call.prompt
    assert "n-001 | gateway | service | attribute: LiteLLM proxy fronting JedAI models" in call.prompt
    assert "ledger entry ids: e-01, e-02" in call.prompt
    assert "SCOPE TYPE VOCABULARY: service, artifact" in call.prompt
    # The at-risk block shows facts with their evidence and edges with their ids,
    # which is what a merge, a rewrite and a retirement each need to be writable.
    assert "attribute: refused before #245, fixed in #245  [e-07]" in call.prompt
    assert "[r-001] MENTIONED_WITH litellm-proxy [n-002]" in call.prompt
    harness.close()


# ------------------------------------------- the edge-type vocabulary and M


def test_compaction_targets_is_empty_while_the_scope_is_within_budget() -> None:
    """The common case, and what makes the prompt block absent rather than empty."""
    assert compaction_targets({"FRONTS": 3, "DEPLOYS": 1}, max_edge_types=30) == ()
    assert compaction_targets({}, max_edge_types=1) == ()
    assert compaction_targets({"FRONTS": 1, "DEPLOYS": 1}, max_edge_types=2) == ()


def test_compaction_targets_names_the_least_used_types_ties_broken_by_name() -> None:
    """Least used first, and deterministic to the tie.

    The tie-break is on the name rather than on iteration order because the
    mandate is rendered into a prompt and enforced against the answer: two runs
    over one scope that disagreed about which type goes would make the rule
    unreviewable.
    """
    types = {"FRONTS": 9, "DEPLOYS": 2, "ROUTES_TO": 1, "PROXIES": 1}
    assert compaction_targets(types, max_edge_types=2) == ("PROXIES", "ROUTES_TO")
    assert compaction_targets(types, max_edge_types=3) == ("PROXIES",)
    assert compaction_targets(types, max_edge_types=1) == ("PROXIES", "ROUTES_TO", "DEPLOYS")


def test_compaction_targets_rejects_a_budget_below_one() -> None:
    """``M >= 1`` is what guarantees a surviving type to fold into."""
    with pytest.raises(ValueError, match="max_edge_types must be at least 1"):
        compaction_targets({"FRONTS": 1}, max_edge_types=0)


def _three_type_harness(responses: list[str | Exception]) -> _Harness:
    """Four nodes and three edge types, so ``M=2`` forces exactly one rename.

    ``FRONTS`` carries two edges and the other two carry one each, so the least
    used pair is ``PROXIES`` and ``ROUTES_TO`` and the name tie-break picks
    ``PROXIES``. One rename, one mandated type, and a surviving vocabulary of
    two -- which is the smallest case that can tell "compacted" from "emptied".
    """
    harness = _Harness(responses)
    for index, name in enumerate(("gateway", "models", "agent-memory", "chart")):
        harness.node(
            name,
            facts=(f"concept {index + 1}",),
            fact_entries=(f"e-{index + 1:02d}",),
            dirty=False,
            dreamed_at=T0,
        )
        harness.entry(f"e-{index + 1:02d}", nodes=(f"n-{index + 1:03d}",))
    harness.relate("n-001", "n-002", type="FRONTS", claim="the proxy fronts the models", entries=("e-01",))
    harness.relate("n-004", "n-003", type="FRONTS", claim="the chart fronts the server", entries=("e-04",))
    harness.relate("n-001", "n-003", type="PROXIES", claim="it proxies for the memory server", entries=("e-01",))
    harness.relate("n-003", "n-002", type="ROUTES_TO", claim="the server routes to the models", entries=("e-03",))
    return harness


@pytest.mark.asyncio
async def test_the_prompt_mandates_compacting_the_least_used_types_when_m_is_exceeded() -> None:
    """``M`` is a bound, so the prompt states the arithmetic rather than asking for it.

    Three types at ``M=2``: the block names ``PROXIES`` as the one that must go
    and lists the types that are staying, because ``new`` must be one of them and
    a model told only what must go would have to subtract two lists in its head.
    """
    motive = MOTIVE.model_copy(update={"max_edge_types": 2})
    harness = _three_type_harness([_global_response()])

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    prompt = harness.transport.last.prompt
    assert "EDGE TYPE BUDGET: 3 of 2 used" in prompt
    assert "MUST COMPACT EDGE TYPES -- MANDATORY, AND ALREADY DECIDED" in prompt
    assert "MUST COMPACT: PROXIES (1 edge(s))" in prompt
    assert "Types that are staying, and may be renamed INTO: FRONTS, ROUTES_TO" in prompt
    assert "MUST COMPACT: FRONTS" not in prompt
    assert any("PROXIES must be compacted away" in error for error in raised.value.errors), raised.value.errors
    harness.close()


@pytest.mark.asyncio
async def test_a_rename_folds_every_edge_of_one_type_into_another_and_holds_m() -> None:
    """The compaction, end to end: the beliefs survive and only the label changes.

    Both ``PROXIES`` edges and the ``FRONTS`` edges end up under ``FRONTS``, the
    vocabulary lands on ``M``, and the receipt says how many edges moved -- which
    is the number only the store knows, because a rename folds into an edge the
    pair already holds rather than creating a duplicate.
    """
    motive = MOTIVE.model_copy(update={"max_edge_types": 2})
    harness = _three_type_harness(
        [
            _global_response(
                {
                    "op": "rename_edge_type",
                    "old": "PROXIES",
                    "new": "FRONTS",
                    "reason": "one relationship under two labels",
                }
            )
        ]
    )
    before = {relation.claim for relation in harness.graph.relations(scope=SCOPE)}

    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert outcome.edge_type_pressure == ("PROXIES",)
    assert outcome.ops_applied == 1
    edge_types = harness.graph.edge_types(scope=SCOPE)
    assert edge_types == {"FRONTS": 3, "ROUTES_TO": 1}
    assert len(edge_types) <= motive.max_edge_types
    # No belief was lost: every claim is still on an edge, under a new label.
    assert {relation.claim for relation in harness.graph.relations(scope=SCOPE)} == before
    assert harness.ops(ReceiptOp.DREAM_EDGE_TYPE_RENAMED) == ["PROXIES"]
    detail = next(
        receipt.detail
        for receipt in harness.receipts.all(scope=SCOPE)
        if receipt.op is ReceiptOp.DREAM_EDGE_TYPE_RENAMED
    )
    assert "1 edge(s) re-labelled PROXIES to FRONTS" in detail
    harness.close()


@pytest.mark.asyncio
async def test_a_rename_that_collides_with_an_existing_edge_folds_into_it() -> None:
    """Two labels on one pair become one edge with the union of their evidence.

    Which is the same reinforcement rule ``upsert_relation`` applies, reached a
    different way: a compaction that created a duplicate triple would break the
    invariant it was run to restore.
    """
    motive = MOTIVE.model_copy(update={"max_edge_types": 2})
    harness = _three_type_harness(
        [_global_response({"op": "rename_edge_type", "old": "PROXIES", "new": "ROUTES_TO", "reason": "fold"})]
    )
    # Give the PROXIES pair a ROUTES_TO edge as well, so the rename collides.
    harness.relate("n-001", "n-003", type="ROUTES_TO", claim="it routes to the memory server", entries=("e-02",))

    await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    folded = next(
        relation
        for relation in harness.graph.relations(scope=SCOPE)
        if relation.triple == ("n-001", "ROUTES_TO", "n-003")
    )
    # A union, and the surviving edge's own ids come first -- the store keeps the
    # order evidence arrived in, which is the ORDER a reader of the journal sees.
    assert set(folded.entry_ids) == {"e-01", "e-02"}
    assert folded.evidence == 2
    assert set(harness.graph.edge_types(scope=SCOPE)) == {"FRONTS", "ROUTES_TO"}
    harness.close()


@pytest.mark.parametrize(
    ("op", "expected"),
    [
        (
            {"op": "rename_edge_type", "old": "NEVER_SEEN", "new": "FRONTS", "reason": "no"},
            "no edge in this scope has type NEVER_SEEN",
        ),
        (
            {"op": "rename_edge_type", "old": "PROXIES", "new": "BRAND_NEW", "reason": "no"},
            "is not a type this scope holds",
        ),
        (
            {"op": "retire_relation", "relation_id": "r-404", "reason": "no"},
            "no relation r-404 in this scope",
        ),
    ],
    ids=["unknown-old", "coined-new", "unknown-relation"],
)
@pytest.mark.asyncio
async def test_a_vocabulary_op_out_of_contract_applies_nothing(op: dict[str, Any], expected: str) -> None:
    """Each vocabulary rule, each leaving both the nodes and the edges untouched."""
    harness = _three_type_harness([_global_response(op)])
    nodes_before, edges_before = harness.snapshot(), harness.edges()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any(expected in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == nodes_before
    assert harness.edges() == edges_before
    assert harness.ops(ReceiptOp.DREAM_EDGE_TYPE_RENAMED) == []
    harness.close()


@pytest.mark.asyncio
async def test_a_rename_into_a_type_that_is_itself_being_compacted_is_rejected() -> None:
    """Otherwise the end state depends on which of the two renames is applied second.

    At ``M=1`` both ``PROXIES`` and ``ROUTES_TO`` are doomed, so folding one into
    the other leaves the vocabulary over budget unless the second rename happens
    to run afterwards -- an outcome that turns on iteration order.
    """
    motive = MOTIVE.model_copy(update={"max_edge_types": 1})
    harness = _three_type_harness(
        [
            _global_response(
                {"op": "rename_edge_type", "old": "PROXIES", "new": "ROUTES_TO", "reason": "fold the pair"},
                {"op": "rename_edge_type", "old": "ROUTES_TO", "new": "FRONTS", "reason": "then fold that"},
            )
        ]
    )
    before = harness.edges()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert any("is itself being compacted away" in error for error in raised.value.errors), raised.value.errors
    assert harness.edges() == before
    harness.close()


@pytest.mark.asyncio
async def test_one_edge_type_renamed_twice_is_refused_by_the_wire_contract() -> None:
    """Response-internal, like a node named by two ops: two writes, no defined order."""
    harness = _three_type_harness(
        [
            _global_response(
                {"op": "rename_edge_type", "old": "PROXIES", "new": "FRONTS", "reason": "one"},
                {"op": "rename_edge_type", "old": "PROXIES", "new": "ROUTES_TO", "reason": "and also"},
            )
        ]
    )
    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert any("renamed by more than one op" in error for error in raised.value.errors), raised.value.errors
    harness.close()


@pytest.mark.asyncio
async def test_a_retired_relation_leaves_the_graph_and_the_receipt_names_its_claim() -> None:
    """A receipt saying only "r-003 retired" would record a loss without the belief.

    So the claim is read BEFORE the retirement. The edge leaves the graph and
    what it asserted stays answerable, which is the whole difference between
    retiring a belief and deleting one.
    """
    harness = _three_type_harness(
        [_global_response({"op": "retire_relation", "relation_id": "r-003", "reason": "#245 fixed it"})]
    )
    retired = harness.graph.get_relation("r-003")

    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert outcome.ops_applied == 1
    assert "r-003" not in {relation.relation_id for relation in harness.graph.relations(scope=SCOPE)}
    assert harness.ops(ReceiptOp.DREAM_RELATION_RETIRED) == ["r-003"]
    detail = next(
        receipt.detail
        for receipt in harness.receipts.all(scope=SCOPE)
        if receipt.op is ReceiptOp.DREAM_RELATION_RETIRED
    )
    assert retired.claim in detail
    assert "n-001 PROXIES n-003 retired" in detail
    harness.close()


@pytest.mark.asyncio
async def test_a_vocabulary_op_claims_no_node_so_it_may_share_a_pass_with_one() -> None:
    """A rename acts on every edge of a type and a retirement on one edge.

    Neither is a verdict on a node, so neither conflicts with a merge or a
    rewrite in the same pass -- and the "no node in two ops" rule must not
    accidentally forbid the combination.
    """
    motive = MOTIVE.model_copy(update={"max_edge_types": 2})
    harness = _three_type_harness(
        [
            _global_response(
                _rewrite("n-001", facts=_facts("the proxy in front of the models", entries=("e-01",))),
                {"op": "rename_edge_type", "old": "PROXIES", "new": "FRONTS", "reason": "one label"},
                {"op": "retire_relation", "relation_id": "r-004", "reason": "no longer true"},
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert outcome.ops_applied == 3
    assert _texts(harness.graph.get_node("n-001")) == ("the proxy in front of the models",)
    assert harness.graph.edge_types(scope=SCOPE) == {"FRONTS": 3}
    harness.close()


# ------------------------------------------------------ unit 22: reject cases


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part("a", "cluster", _facts("part a", entries=("e-04",)), ("e-04",)),
                        _part("b", "metric", _facts("part b", entries=("e-05",)), ("e-05",)),
                    ],
                    "reason": "no headroom at N=6",
                }
            ),
            "no headroom",
        ),
        (
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part("a", "cluster", _facts("part a", entries=("e-04",)), ("e-04",)),
                        _part("b", "metric", _facts("part b", entries=("e-04",)), ("e-04",)),
                    ],
                    "reason": "e-05 dropped and e-04 duplicated",
                }
            ),
            "assigned to more than one part",
        ),
        (
            _global_response(
                {
                    "op": "split",
                    "node": "n-004",
                    "into": [
                        _part("a", "cluster", _facts("part a", entries=("e-04",)), ("e-04",)),
                        _part("b", "metric", _facts("part b", entries=("e-04",)), ("e-99",)),
                    ],
                    "reason": "e-99 is not this node's entry",
                }
            ),
            "not bound to this node",
        ),
        (
            _global_response(
                _merge(
                    ("n-001", "n-999"),
                    name="gateway",
                    node_type="service",
                    facts=_facts("a proxy", entries=("e-01",)),
                )
            ),
            "no such node in this scope: n-999",
        ),
    ],
    ids=["no-headroom", "duplicated-entry", "foreign-entry", "unknown-node"],
)
@pytest.mark.asyncio
async def test_a_rejected_global_pass_applies_zero_ops(response: str, expected: str) -> None:
    harness = _six_node_harness([response])
    # N=6 with 6 nodes, so a split has no headroom and pressure is 0.
    motive = MOTIVE.model_copy(update={"max_nodes": 6})
    before = harness.snapshot()
    aliases_before = harness.ledger.aliases("n-001")
    entries_before = [entry.entry_id for entry in harness.ledger.for_node("n-002")]

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert raised.value.source == "DREAM_GLOBAL"
    assert any(expected in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == before
    assert harness.ledger.aliases("n-001") == aliases_before
    assert [entry.entry_id for entry in harness.ledger.for_node("n-002")] == entries_before
    assert harness.ops(ReceiptOp.DREAM_GLOBAL_REJECTED) == [SCOPE]
    assert harness.ops(ReceiptOp.DREAM_MERGED) == []
    assert harness.ops(ReceiptOp.DREAM_SPLIT) == []
    harness.close()


@pytest.mark.asyncio
async def test_a_node_named_by_two_ops_is_refused_by_the_wire_contract() -> None:
    harness = _six_node_harness(
        [
            _global_response(
                _merge(("n-001", "n-002"), name="gateway", node_type="service", facts=_facts("a proxy")),
                {"op": "retype", "node": "n-001", "type": "policy", "reason": "and also retype it"},
            )
        ]
    )
    before = harness.snapshot()
    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert any("more than one op" in error for error in raised.value.errors)
    assert harness.snapshot() == before
    harness.close()


@pytest.mark.asyncio
async def test_an_over_budget_fact_in_a_global_op_is_rejected() -> None:
    harness = _six_node_harness(
        [_global_response(_rewrite("n-001", facts=_facts("y" * _OVER_BUDGET_CHARS, entries=("e-01",))))]
    )
    before = harness.snapshot()
    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)
    assert any("token guard" in error for error in raised.value.errors)
    assert harness.snapshot() == before
    harness.close()


@pytest.mark.asyncio
async def test_a_merge_that_leaves_the_survivor_over_l_facts_is_rejected() -> None:
    """``L`` binds a merge exactly as it binds an incremental dream.

    The survivor's fact set is the whole fact set, so a merge is the one op that
    can quietly double a node's length by keeping both halves' facts. Whatever
    the model drops is in the ledger and reachable through ``explain``.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="gateway",
                    node_type="service",
                    facts=_facts(*[f"fact number {index} of nine" for index in range(9)], entries=("e-01",)),
                )
            )
        ]
    )
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert any(f"{MOTIVE.max_facts_per_node}-fact ceiling" in error for error in raised.value.errors), (
        raised.value.errors
    )
    assert harness.snapshot() == before
    harness.close()


@pytest.mark.asyncio
async def test_an_under_delivered_forced_merge_is_out_of_contract() -> None:
    """The slate is a mandate. Returning zero ops under pressure is a rejection."""
    harness = _six_node_harness([_global_response()])
    motive = MOTIVE.model_copy(update={"max_nodes": 5})
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert any("does not appear in any merge op" in error for error in raised.value.errors)
    assert harness.snapshot() == before
    harness.close()


# ------------------------------------------------- unit 22: integration at N=6


def _eight_node_harness(responses: list[str | Exception]) -> _Harness:
    harness = _Harness(responses)
    shape = (
        ("gateway", "service", ("LiteLLM proxy fronting JedAI models",), 9),
        ("litellm-proxy", "service", ("the same proxy under a second name",), 0),
        ("chart", "artifact", ("deploys the MCP server, #240",), 5),
        ("c4", "cluster", ("returns 31 tools after #248",), 4),
        ("probe-246", "probe", ("names the affinity failure, committed",), 0),
        ("host-header", "defect", ("refused before #245, fixed in #245",), 0),
        ("session-affinity", "defect", ("loss about 1 in 20, unreproduced on demand",), 0),
        ("cold-pod", "defect", ("a cold pod is where an affinity loss lands",), 0),
    )
    for index, (name, node_type, facts, reads) in enumerate(shape):
        node = harness.node(
            name,
            node_type=node_type,
            facts=facts,
            fact_entries=(f"e-{index + 1:02d}",),
            dirty=False,
            dreamed_at=T0 - timedelta(days=index * 20),
        )
        harness.graph.record_read([node.node_id] * reads)
    for index in range(1, 9):
        harness.entry(f"e-{index:02d}", nodes=(f"n-{index:03d}",))
    return harness


@pytest.mark.asyncio
async def test_eight_nodes_at_n_six_forces_two_merges_and_lands_on_the_budget() -> None:
    """The plan's 3b integration case: pressure 2, the slate verbatim, count <= 6.

    Also the case amendment A rewrote. ``gateway`` is the most-read node in this
    scope and it IS named by the slate -- as a survivor, because the peer is now
    the best-linked node in the whole scope rather than another leftover. Being
    named costs it nothing: it keeps its name, its id and its facts' provenance,
    and gains the doomed node's name as an alias. Under the old bounded pool the
    two lowest-value defects would have been merged into each other, which is how
    a node called ``chart-models`` came to hold four unrelated concepts.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 6})

    # First pass with an empty answer, purely to read back the slate the module
    # computed -- the mandate has to be known before it can be asserted verbatim.
    probe = _eight_node_harness([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await probe.dreamer.dream_global(scope=SCOPE, motive=motive)
    slate_pairs = _slate_pairs(probe.transport.last.prompt)
    # Ids are minted in insertion order, so the second harness's nodes carry the
    # same ids as this one's -- which is what makes reading the names here sound.
    survivors = {survivor: probe.graph.get_node(survivor).name for _, survivor in slate_pairs}
    doomed_names = {probe.graph.get_node(doomed).name for doomed, _ in slate_pairs}
    probe.close()

    assert len(slate_pairs) == 2

    harness = _eight_node_harness(
        [
            _global_response(
                *[
                    _merge(
                        pair,
                        name=survivors[pair[1]],
                        node_type="defect",
                        facts=_facts(f"merged pair {index}", entries=(_entry_of(pair[0]),)),
                    )
                    for index, pair in enumerate(slate_pairs)
                ]
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert outcome.pressure == 2
    assert len(outcome.slate) == 2
    assert [pair.render() for pair in outcome.slate] == [
        f"{doomed} INTO {survivor}" for doomed, survivor in slate_pairs
    ]
    assert outcome.count_before == 8
    assert outcome.count_after == 6
    assert harness.graph.count(scope=SCOPE) <= motive.max_nodes

    names = {node.name for node in harness.graph.list_nodes(scope=SCOPE)}
    for node in harness.graph.list_nodes(scope=SCOPE):
        for text in _texts(node):
            validate_fact_text(text, max_fact_tokens=motive.max_fact_tokens)

    # Every survivor kept its own name; every doomed name survives as an alias.
    assert set(survivors.values()) <= names
    assert names.isdisjoint(doomed_names)
    for survivor_id, name in survivors.items():
        survivor = harness.graph.get_node(survivor_id)
        assert survivor.name == name
        assert doomed_names & set(survivor.aliases)
    # The most-read node was pulled in as a survivor, and still answers to its name.
    assert "gateway" in names
    harness.close()


# =========================================================== unit 23: erasure


def _two_episode_harness(responses: list[str | Exception]) -> _Harness:
    """Two nodes: one supported by both episodes, one only by the erased one."""
    harness = _Harness(responses)
    harness.node(
        "gateway",
        facts=("LiteLLM proxy fronting JedAI models", "C4 returns 24 tools"),
        fact_entries=("e-keep",),
        dirty=False,
        dreamed_at=T0 - timedelta(days=1),
    )
    harness.node(
        "stale-count",
        node_type="metric",
        facts=("24 tools",),
        fact_entries=("e-gone-2",),
        dirty=False,
        dreamed_at=T0,
    )
    harness.entry(
        "e-keep",
        nodes=("n-001",),
        episode="ep-keep",
        claim="The JedAI Gateway is a LiteLLM proxy that fronts the JedAI models.",
    )
    harness.entry(
        "e-gone-1",
        nodes=("n-001", "n-002"),
        episode="ep-gone",
        claim="C4 is returning 24 tools right now, so the surface is basically complete.",
        identifiers=("24",),
    )
    harness.entry(
        "e-gone-2",
        nodes=("n-002",),
        episode="ep-gone",
        claim="The 24-tool count is the number recorded before #248 landed.",
        identifiers=("24", "#248"),
    )
    harness.relate_from_ledger("n-001", "n-002")
    return harness


def test_erasure_deletes_unsupported_nodes_and_marks_the_rest_dirty() -> None:
    harness = _two_episode_harness([])
    assert len(harness.graph.relations_of("n-001")) == 1

    outcome = harness.dreamer.erase_episode(episode_id="ep-gone", scope=SCOPE)

    assert outcome.deleted_entry_count == 2
    assert outcome.deleted_node_ids == ("n-002",)
    assert outcome.dirty_node_ids == ("n-001",)
    assert harness.graph.get_nodes(["n-002"]) == []
    assert harness.graph.get_node("n-001").dirty is True
    assert harness.graph.relations_of("n-001") == []
    assert [entry.entry_id for entry in harness.ledger.for_node("n-001")] == ["e-keep"]
    assert harness.ledger.for_episode("ep-gone") == []
    assert harness.ops(ReceiptOp.ERASURE_APPLIED) == ["ep-gone"]
    assert harness.transport.call_count == 0
    harness.close()


def test_erasing_an_unknown_episode_changes_nothing() -> None:
    harness = _two_episode_harness([])
    before = harness.snapshot()
    outcome = harness.dreamer.erase_episode(episode_id="ep-never-existed", scope=SCOPE)
    assert outcome.deleted_entry_count == 0
    assert outcome.deleted_node_ids == ()
    assert outcome.dirty_node_ids == ()
    assert harness.snapshot() == before
    assert harness.ops(ReceiptOp.ERASURE_APPLIED) == ["ep-never-existed"]
    harness.close()


@pytest.mark.asyncio
async def test_a_re_dream_after_erasure_drops_the_erased_episodes_identifiers() -> None:
    """The plan's 23 integration case: erase one episode, re-dream, nothing left of it.

    The surviving claim is still supported and still on the node; the erased
    episode's ``24`` is gone from the facts and gone from the ledger, so no
    later read can surface it.
    """
    harness = _two_episode_harness(
        [
            _node_response(
                _facts("LiteLLM proxy fronting JedAI models", entries=("e-keep",)),
                reason="the 24-tool claim was erased",
            )
        ]
    )
    harness.dreamer.erase_episode(episode_id="ep-gone", scope=SCOPE)

    harness.clock.reading = T0 + timedelta(hours=1)
    dreamed = await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert [node.node_id for node in dreamed] == ["n-001"]
    node = harness.graph.get_node("n-001")
    assert _texts(node) == ("LiteLLM proxy fronting JedAI models",)
    assert all("24" not in text for text in _texts(node))
    assert harness.graph.dirty(scope=SCOPE) == []

    surviving = harness.ledger.for_node("n-001")
    assert [entry.entry_id for entry in surviving] == ["e-keep"]
    # Every fact the node kept is still supported by a claim that remains.
    assert "LiteLLM proxy" in surviving[0].claim
    assert not [entry for entry in surviving if "24" in entry.identifiers]

    prompt = harness.transport.last.prompt
    assert "[e-keep]" in prompt
    assert "[e-gone-1]" not in prompt
    harness.close()


@pytest.mark.asyncio
async def test_a_superseding_entry_shows_what_it_supersedes_in_the_prompt() -> None:
    """The dreamer cannot rewrite a fact for a supersession it was not shown."""
    harness = _Harness([_node_response(_facts("returns 31 tools after #248", entries=("e-new",)), node_type="cluster")])
    harness.node("c4", node_type="cluster", facts=("C4 returns 24 tools",), fact_entries=("e-old",))
    harness.entry(
        "e-old",
        nodes=("n-001",),
        claim="C4 is returning 24 tools right now, so the surface is basically complete.",
        identifiers=("24",),
    )
    harness.entry(
        "e-new",
        nodes=("n-001",),
        claim="The C4 memory server returns 31 tools, not 24; 24 was the count before #248.",
        mode=ClaimMode.CORRECTION,
        supersedes="e-old",
        identifiers=("31", "#248"),
    )

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    prompt = harness.transport.last.prompt
    assert "supersedes=e-old" in prompt
    assert _texts(harness.graph.get_node("n-001")) == ("returns 31 tools after #248",)
    harness.close()


@pytest.mark.asyncio
async def test_a_dirty_node_with_nothing_new_is_still_dreamed_from_its_own_facts() -> None:
    """Reconcile can mark a node dirty for a claim that landed on a NEIGHBOUR.

    The node then has no entries past its watermark, and the dreamer is asked to
    reconsider what it already holds. Saying "nothing arrived" beats sending an
    empty section the model has to guess the meaning of.
    """
    harness = _Harness([_node_response(_facts("LiteLLM proxy fronting JedAI models", entries=("e-01",)))])
    dreamed_at = T0 - timedelta(days=1)
    harness.node(
        "gateway",
        facts=("LiteLLM proxy fronting JedAI models", "chat default claude-haiku-4-5"),
        fact_entries=("e-01",),
        created=T0 - timedelta(days=3),
        dreamed_at=dreamed_at,
    )
    harness.entry("e-01", nodes=("n-001",), ts=dreamed_at - timedelta(days=1))

    await harness.dreamer.dream_incremental(scope=SCOPE, motive=MOTIVE)

    assert "nothing new has arrived" in harness.transport.last.prompt
    assert _texts(harness.graph.get_node("n-001")) == ("LiteLLM proxy fronting JedAI models",)
    harness.close()


@pytest.mark.asyncio
async def test_a_slate_pair_may_be_delivered_inside_a_larger_merge() -> None:
    """The mandate is "these two become one", not "merge exactly these two".

    A three-node merge that contains the pair satisfies it -- checked as a subset
    of the op's ``nodes``, so absorbing a third concept at the same time is
    allowed rather than a rejection for over-delivering. What it may NOT do is
    keep the third node's name: the mandate named the survivor.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 5})

    probe = _six_node_harness([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await probe.dreamer.dream_global(scope=SCOPE, motive=motive)
    pairs = _slate_pairs(probe.transport.last.prompt)
    survivor_name = probe.graph.get_node(pairs[0][1]).name
    probe.close()

    assert len(pairs) == 1
    doomed, survivor = pairs[0]
    third = next(
        node_id for node_id in (f"n-{index:03d}" for index in range(1, 7)) if node_id not in {doomed, survivor}
    )

    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    (doomed, survivor, third),
                    name=survivor_name,
                    node_type="defect",
                    facts=_facts("three became one", entries=(_SIX_NODE_ENTRY[doomed],)),
                )
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)
    assert harness.graph.get_node(survivor).name == survivor_name

    assert outcome.pressure == 1
    assert outcome.slate[0].node_ids == frozenset({doomed, survivor})
    assert outcome.count_after == 4
    assert harness.graph.count(scope=SCOPE) <= motive.max_nodes
    harness.close()


@pytest.mark.asyncio
async def test_the_mandate_names_both_halves_and_says_the_survivor_keeps_its_name() -> None:
    """What the model is actually told. The direction has to be unmissable.

    Rendered as bare ids a live pass wrote its survivor a relation pointing at a
    name that very merge was consuming, so the block spells out both names, both
    ids, which one stays and which one becomes an alias.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 5})
    harness = _six_node_harness([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    prompt = harness.transport.last.prompt
    (doomed, survivor) = _slate_pairs(prompt)[0]
    doomed_name = harness.graph.get_node(doomed).name
    survivor_name = harness.graph.get_node(survivor).name

    assert f"MUST MERGE: {doomed_name!r} ({doomed}) INTO {survivor_name!r} ({survivor})" in prompt
    assert f"Send survivor_name {survivor_name!r}: the survivor KEEPS ITS OWN NAME" in prompt
    assert f"The name\n      {doomed_name!r} stops existing" in prompt
    assert "THE SURVIVOR KEEPS ITS OWN NAME" in DREAM_GLOBAL_SYSTEM
    harness.close()


@pytest.mark.asyncio
async def test_a_forced_merge_that_keeps_the_doomed_halfs_name_is_rejected() -> None:
    """``pressure`` chose the survivor, so the answer may not invert the pair.

    Delivering the merge but keeping the leftover's name would destroy the live
    concept's name -- the same loss as an invented name, reached the other way
    round.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 5})

    probe = _six_node_harness([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await probe.dreamer.dream_global(scope=SCOPE, motive=motive)
    doomed, survivor = _slate_pairs(probe.transport.last.prompt)[0]
    doomed_name = probe.graph.get_node(doomed).name
    probe.close()

    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    (doomed, survivor),
                    name=doomed_name,
                    node_type="defect",
                    facts=_facts("merged", entries=(_SIX_NODE_ENTRY[doomed],)),
                )
            )
        ]
    )
    before = harness.snapshot()

    with pytest.raises(OutOfContractResponse) as raised:
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert any("must keep" in error for error in raised.value.errors), raised.value.errors
    assert harness.snapshot() == before
    harness.close()


# --------------------------------- the provenance tiebreak (amendment B)


def _reason_harness(
    responses: list[str | Exception],
    *,
    entries: tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...],
) -> _Harness:
    """Two nodes at ``N=1``, carrying exactly the ledger evidence a case is about.

    ``gateway`` is ``n-001`` and read five times; ``platform`` is ``n-002`` and
    never read, so the mandate is ``platform INTO gateway`` in every case below
    and the only thing that varies is what the ledger says about the pair. Each
    *entries* item is ``(entry_id, node_ids, turns)``.
    """
    harness = _Harness(responses)
    gateway = harness.node("gateway", facts=("LiteLLM proxy fronting JedAI models",), dirty=False, dreamed_at=T0)
    platform = harness.node(
        "platform", node_type="policy", facts=("never hermetic, fix the platform",), dirty=False, dreamed_at=T0
    )
    harness.graph.record_read([gateway.node_id] * 5)
    for entry_id, nodes, turns in entries:
        harness.entry(entry_id, nodes=nodes, turns=turns)
    harness.relate_from_ledger(gateway.node_id, platform.node_id)
    return harness


@pytest.mark.parametrize(
    ("entries", "reason"),
    [
        ((("e-01", ("n-001", "n-002"), (1,)),), "linked x1"),
        ((("e-01", ("n-001",), (3,)), ("e-02", ("n-002",), (3,))), "same turn x1"),
        ((("e-01", ("n-001",), (1,)), ("e-02", ("n-002",), (2,))), "nearest by embedding"),
    ],
    ids=["linked", "same-turn", "embedding"],
)
@pytest.mark.asyncio
async def test_the_mandate_says_which_evidence_chose_the_peer(
    entries: tuple[tuple[str, tuple[str, ...], tuple[int, ...]], ...], reason: str
) -> None:
    """The three mandates are not equally strong, so the model is told which it has.

    One claim naming both nodes is co-occurrence and reads ``linked x1``. Two
    claims in one turn name neither pair and read ``same turn x1`` -- the case
    amendment B added. Two claims in different turns leave only the vectors, and
    the mandate says so rather than letting the model write the survivor's facts
    as if the ledger had tied them together.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 1})
    harness = _reason_harness([_global_response()], entries=entries)

    with pytest.raises(OutOfContractResponse):
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    prompt = harness.transport.last.prompt
    assert _slate_pairs(prompt) == [("n-002", "n-001")]
    assert f"MUST MERGE: 'platform' (n-002) INTO 'gateway' (n-001) because {reason}" in prompt
    harness.close()


@pytest.mark.asyncio
async def test_co_occurrence_comes_from_the_ledger_and_not_from_the_edges() -> None:
    """Amendment D's adaptation, asserted where it is observable.

    One claim names both nodes and NO relation exists between them, which is what
    a unary claim bound to two subjects leaves behind. The mandate still reads
    ``linked x1``, because the first rung asks ``LedgerStore.cooccurrence`` rather
    than the evidence under an edge that was never created.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 1})
    harness = _Harness([_global_response()])
    gateway = harness.node("gateway", facts=("LiteLLM proxy fronting JedAI models",), dirty=False, dreamed_at=T0)
    harness.node("platform", node_type="policy", facts=("fix the platform",), dirty=False, dreamed_at=T0)
    harness.graph.record_read([gateway.node_id] * 5)
    harness.entry("e-01", nodes=("n-001", "n-002"), turns=(1,))

    with pytest.raises(OutOfContractResponse):
        await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert harness.graph.relations(scope=SCOPE) == []
    assert "MUST MERGE: 'platform' (n-002) INTO 'gateway' (n-001) because linked x1" in harness.transport.last.prompt
    harness.close()


@pytest.mark.asyncio
async def test_a_shared_turn_beats_a_closer_embedding_and_the_pass_still_applies() -> None:
    """Amendment B end to end: the peer is the turn-sharer, not the nearest vector.

    ``platform`` is the lowest-value node and the ledger ties it to nothing, so
    before amendment B it fell straight to embedding similarity -- which is how
    turn 1's "fix the platform, never downgrade" was folded into the node it
    merely reads like. Here it shares a turn with the OTHER candidate, and the
    nearest-by-embedding node is measured rather than assumed so the test is
    about the ranking and not about what the local embedder happens to do.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 2})
    shape = (("gateway", "service"), ("cold-pod", "defect"))

    probe = _Harness([])
    doomed_node = probe.node("platform", node_type="policy", facts=("fix the platform",), dirty=False, dreamed_at=T0)
    candidates = [probe.node(name, node_type=node_type, dirty=False, dreamed_at=T0) for name, node_type in shape]
    ranked = sorted(candidates, key=lambda node: embedding_similarity(doomed_node, node), reverse=True)
    nearest, sharer = ranked[0].name, ranked[1].name
    probe.close()

    def build(responses: list[str | Exception]) -> tuple[_Harness, dict[str, str]]:
        harness = _Harness(responses)
        doomed = harness.node("platform", node_type="policy", facts=("fix the platform",), dirty=False, dreamed_at=T0)
        ids = {"platform": doomed.node_id}
        for index, (name, node_type) in enumerate(shape, start=1):
            node = harness.node(name, node_type=node_type, facts=(f"concept {index}",), dirty=False, dreamed_at=T0)
            ids[name] = node.node_id
            harness.graph.record_read([node.node_id] * 5)
        # One entry per node, each in its own turn -- except the sharer's, which
        # lands in the doomed node's turn. No entry names two nodes, so every
        # co-occurrence count in this scope is 0.
        harness.entry("e-01", nodes=(ids["platform"],), turns=(1,))
        harness.entry("e-02", nodes=(ids[sharer],), turns=(1,))
        harness.entry("e-03", nodes=(ids[nearest],), turns=(7,))
        return harness, ids

    rejected, ids = build([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await rejected.dreamer.dream_global(scope=SCOPE, motive=motive)
    prompt = rejected.transport.last.prompt
    assert rejected.graph.relations(scope=SCOPE) == []
    assert _slate_pairs(prompt) == [(ids["platform"], ids[sharer])]
    assert f"INTO {sharer!r} ({ids[sharer]}) because same turn x1" in prompt
    rejected.close()

    harness, ids = build(
        [
            _global_response(
                _merge(
                    (ids["platform"], ids[sharer]),
                    name=sharer,
                    node_type="service",
                    facts=_facts("one concept", entries=("e-01",)),
                )
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert outcome.pressure == 1
    assert outcome.slate[0].turn_cooccurrence == 1
    assert outcome.slate[0].link_weight == 0
    assert outcome.ops_applied == 1
    assert outcome.count_after == 2
    survivor = harness.graph.get_node(ids[sharer])
    assert survivor.name == sharer
    assert survivor.aliases == ("platform",)
    # The node it merely reads like is still there, under its own name, unmerged.
    untouched = harness.graph.get_node(ids[nearest])
    assert (untouched.name, untouched.aliases) == (nearest, ())
    harness.close()


def _alias_pair_harness(responses: list[str | Exception]) -> _Harness:
    """Two nodes that already answer to each other's names, and one entry binding both.

    ``gateway`` carries ``litellm-proxy`` as an alias -- which is what reconcile
    records when a second surface name routes to a node it already holds -- and
    the other node IS called ``litellm-proxy``. Reads make ``gateway`` the more
    valuable of the two, so at ``N=1`` the mandate is ``litellm-proxy`` INTO
    ``gateway``.
    """
    harness = _Harness(responses)
    gateway = harness.node(
        "gateway",
        facts=("LiteLLM proxy fronting JedAI models",),
        dirty=False,
        dreamed_at=T0,
    )
    proxy = harness.node(
        "litellm-proxy",
        facts=("the same proxy under a second name",),
        dirty=False,
        dreamed_at=T0,
    )
    harness.graph.add_aliases(gateway.node_id, ["litellm-proxy"])
    harness.graph.record_read([gateway.node_id] * 5)
    harness.entry("e-01", nodes=(gateway.node_id, proxy.node_id))
    harness.relate_from_ledger(gateway.node_id, proxy.node_id)
    return harness


@pytest.mark.asyncio
async def test_two_nodes_already_aliases_of_each_other_may_merge_either_way() -> None:
    """The one exception to the direction rule, and why it is safe.

    Both nodes answer to both surfaces, so whichever record survives, every name
    a reader might search for still resolves to it. Nothing observable turns on
    the direction, and rejecting it would cost a whole forced pass to enforce a
    distinction no reader can see.
    """
    motive = MOTIVE.model_copy(update={"max_nodes": 1})

    probe = _alias_pair_harness([_global_response()])
    with pytest.raises(OutOfContractResponse):
        await probe.dreamer.dream_global(scope=SCOPE, motive=motive)
    doomed, survivor = _slate_pairs(probe.transport.last.prompt)[0]
    assert (probe.graph.get_node(doomed).name, probe.graph.get_node(survivor).name) == ("litellm-proxy", "gateway")
    probe.close()

    # The answer keeps the DOOMED half's name -- accepted, because the survivor
    # already answers to it too.
    harness = _alias_pair_harness(
        [
            _global_response(
                _merge(
                    (doomed, survivor),
                    name="litellm-proxy",
                    node_type="service",
                    facts=_facts("one proxy", entries=("e-01",)),
                )
            )
        ]
    )
    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=motive)

    assert outcome.count_after == 1
    kept = harness.graph.get_node(doomed)
    assert kept.name == "litellm-proxy"
    assert kept.aliases == ("gateway",)
    # Both surfaces still route to the one surviving node, which is the whole
    # reason the direction did not matter here.
    assert harness.graph.node_by_alias(scope=SCOPE, name="gateway") == kept
    assert harness.graph.node_by_alias(scope=SCOPE, name="litellm-proxy") == kept
    harness.close()


# ============================= what a merge does to OTHER nodes' relations


@pytest.mark.asyncio
async def test_a_merge_re_points_a_third_nodes_edge_rather_than_orphaning_it() -> None:
    """The gap that CLOSED ITSELF with #251 amendment D, and the proof of it.

    A relation used to be a line naming a node by NAME, so a merge could leave a
    third, untouched node pointing at a name that no longer existed -- and
    validation never saw it, because it checks the ops' own facts and that line
    was correct when an earlier pass wrote it. A live forced pass left exactly
    that behind. The repair was to mark the pointing node dirty and report it as
    ``GlobalOutcome.orphaned_node_ids``.

    An edge now carries node IDS, and ``graph.merge_nodes`` re-points every edge
    of the nodes it absorbs. A dangling target is therefore unrepresentable
    rather than repaired, so the field and the pass that computed it are deleted
    -- and this test asserts the state that made them unnecessary.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-003", "n-004"),
                    name="chart",
                    node_type="artifact",
                    facts=_facts("merged", entries=("e-04",)),
                )
            )
        ]
    )
    # n-003 and n-004 are related, so the merge makes that edge a self-loop and
    # drops it. Relate a THIRD node to the one being absorbed: that edge is the
    # one an orphan repair used to exist for.
    harness.relate("n-004", "n-005", entries=("e-05",))
    assert [relation.relation_id for relation in harness.graph.relations_of("n-005")] == ["r-003"]

    outcome = await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert not hasattr(outcome, "orphaned_node_ids")
    moved = harness.graph.relations_of("n-005")
    assert len(moved) == 1
    assert {moved[0].source_id, moved[0].target_id} == {"n-003", "n-005"}
    assert harness.graph.get_node("n-005").dirty is False
    harness.close()


@pytest.mark.asyncio
async def test_a_merge_drops_the_edge_between_the_nodes_it_collapses() -> None:
    """Nothing relates to itself, and a merge is where that case arises.

    The nodes a merge picks are the ones already tied to each other, so the
    survivor would otherwise inherit an edge to the node it just absorbed -- the
    mistake the live runs actually made, three rejections in a row.
    """
    harness = _six_node_harness(
        [
            _global_response(
                _merge(
                    ("n-001", "n-002"),
                    name="gateway",
                    node_type="service",
                    facts=_facts("merged", entries=("e-01",)),
                )
            )
        ]
    )
    assert len(harness.graph.relations_of("n-001")) == 1

    await harness.dreamer.dream_global(scope=SCOPE, motive=MOTIVE)

    assert harness.graph.relations_of("n-001") == []
    assert all(relation.source_id != relation.target_id for relation in harness.graph.relations(scope=SCOPE))
    harness.close()
