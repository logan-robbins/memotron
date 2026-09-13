"""#251 unit 19: stage 2, the matcher. Names to node ids, claims to typed edges.

Six things are load-bearing here and each gets its own group of assertions:

1. **Motive-neutrality.** The rendered prompt contains neither ``motive.name``
   nor ``motive.goal``, and no candidate node's FACTS -- only a gloss. The
   fixture entries deliberately carry ``motive="engineering"`` in their own
   ``motive`` field, so a rendering that leaked the entry's policy would fail the
   same assertion.
2. **Within-batch reconciliation.** Two surface names sharing one
   ``new_node.name`` create exactly ONE node, and both entries carry that one id.
   Structural, not luck about string collisions.
3. **Every write, from the ledger.** ``bind_nodes`` once per entry,
   ``mark_dirty`` over every touched node, and one ``upsert_relation`` per
   relational claim the model typed. The spies below record the calls rather than
   replacing the stores, so the real sqlite ledger and the real in-memory graph
   are still what runs.
4. **The three matcher decisions** (#251 amendment D, package D-B). A new node,
   a new edge TYPE, or an existing type applied to another pair. The prompt shows
   the scope's edge-type vocabulary with counts so the third is available at all;
   ``ReconcileOutcome.edge_types_new`` says which words the batch added, and
   ``relations_created`` / ``relations_reinforced`` say whether a belief was
   recorded or confirmed. A restatement REINFORCES: one relation, evidence two.
5. **Rejection is all-or-nothing.** A missing name, an extra name, a bind to a
   node not offered for that name, two same-named proposals disagreeing on type,
   an untyped relational claim, a claim typed twice, a relation end that is not a
   surface name, an end pointed at itself, and a type that is not UPPER_SNAKE
   each leave the graph and the ledger untouched.
6. **Aliases are recorded here or nowhere** (amendment A, WP-C). A ``bind``
   records the routed surface name on the node and a ``new`` group records the
   whole converged group, so the name a searcher types becomes an exact-match
   key. The candidate table then shows those names back —
   ``id | name | aliases | type | gloss`` — which is what lets a name the router
   has already routed be recognised on sight instead of re-derived from a gloss.

There are three integration cases. The 10-turn episode's 12 entries against an
EMPTY graph: four surface names in two synonym pairs must resolve to exactly two
node ids, and its two relational claims must produce exactly one edge because
the other one's two names converged. A SECOND batch naming one of the aliases the
first batch recorded: it must be offered that node, with the name it is searching
under visible in the alias column, and bind to it without creating anything. And
a second batch restating the first batch's edge: one relation, evidence two, and
the type reused from the vocabulary the prompt showed.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from typing import Any

import pytest

from caveman_fakes import ScriptedChatTransport
from memotron.caveman.errors import OutOfContractResponse
from memotron.caveman.graph import InMemoryGraph
from memotron.caveman.ledger import CavemanLedger
from memotron.caveman.models import (
    MAX_CLAIM_TEXT,
    MAX_FACT_TEXT,
    ClaimKind,
    ClaimMode,
    Fact,
    FactKind,
    LedgerEntry,
    ReceiptOp,
    Relation,
)
from memotron.caveman.motive import CavemanMotive, engineering_motive
from memotron.caveman.receipts import InMemoryReceipts
from memotron.caveman.reconcile import (
    CANDIDATE_K,
    NO_ALIASES,
    NO_EDGE_TYPES,
    RECONCILE_SYSTEM,
    ReconcileOutcome,
    reconcile,
)
from memotron.caveman.reconcile import _query_text as query_text
from memotron.caveman.render import RELATION_TARGET_RULE
from memotron.embedding import LocalEmbeddingTransport

NOW = datetime(2026, 9, 10, 11, 15, tzinfo=UTC)
LATER = datetime(2026, 9, 11, 9, 0, tzinfo=UTC)
"""A second batch's clock. Distinct from ``NOW`` so a stamp that moved is visible."""

SCOPE = "repo:jedai/memotron"
EMBEDDER = LocalEmbeddingTransport()

GATEWAY = "the JedAI Gateway"
PROXY = "the LiteLLM proxy"
MCP = "the agent-memory MCP server"
C4 = "the C4 memory server"

FIXTURE_FACTS: tuple[Fact, ...] = (
    Fact(
        kind=FactKind.RULE,
        text="never route a real call around the front door",
        entry_ids=("e-fixture",),
        first_seen=NOW,
        last_seen=NOW,
    ),
    Fact(
        kind=FactKind.IS,
        text="the single entry point every model call takes",
        entry_ids=("e-fixture",),
        first_seen=NOW,
        last_seen=NOW,
    ),
    Fact(
        kind=FactKind.ATTRIBUTE,
        text="timeout 180s, retry policy comes from the default",
        entry_ids=("e-fixture",),
        first_seen=NOW,
        last_seen=NOW,
    ),
)
"""Lines on the fixture nodes. Stage 2 must never render any of these.

Deliberately NOT the grammar block's own ``EXAMPLE_LINES``: every prompt embeds
those, so a fixture reusing them would make the "never renders a node's lines"
assertion unfalsifiable.
"""


# ------------------------------------------------------------------- the spies


class _SpyLedger(CavemanLedger):
    """The real sqlite ledger, recording the two calls stage 2 must make on it."""

    def __init__(self, path: str = ":memory:") -> None:
        super().__init__(path)
        self.bind_calls: list[tuple[str, tuple[str, ...]]] = []
        self.cooccurrence_returns: list[tuple[str, dict[str, int]]] = []

    def bind_nodes(self, entry_id: str, node_ids: Sequence[str]) -> None:
        self.bind_calls.append((entry_id, tuple(node_ids)))
        super().bind_nodes(entry_id, node_ids)

    def cooccurrence(self, node_id: str) -> dict[str, int]:
        weights = super().cooccurrence(node_id)
        self.cooccurrence_returns.append((node_id, dict(weights)))
        return weights


class _SpyGraph(InMemoryGraph):
    """The real in-memory graph, recording the routing writes."""

    def __init__(self) -> None:
        super().__init__()
        self.dirty_calls: list[tuple[str, ...]] = []
        self.relation_calls: list[tuple[str, str, str, tuple[str, ...]]] = []

    def mark_dirty(self, node_ids: Sequence[str]) -> None:
        self.dirty_calls.append(tuple(node_ids))
        super().mark_dirty(node_ids)

    def upsert_relation(
        self,
        *,
        scope: str,
        source_id: str,
        target_id: str,
        type: str,
        claim: str | None,
        entry_ids: Sequence[str],
        until: str | None,
        now: datetime,
    ) -> Relation:
        self.relation_calls.append((source_id, target_id, type, tuple(entry_ids)))
        return super().upsert_relation(
            scope=scope,
            source_id=source_id,
            target_id=target_id,
            type=type,
            claim=claim,
            entry_ids=entry_ids,
            until=until,
            now=now,
        )


# ---------------------------------------------------------------- the fixtures


def _entry(
    entry_id: str,
    claim: str,
    *,
    kind: ClaimKind = ClaimKind.ATTRIBUTE,
    claim_mode: ClaimMode = ClaimMode.DESCRIPTIVE,
    subjects: tuple[str, ...],
    objects: tuple[str, ...] = (),
    identifiers: tuple[str, ...] = (),
    episode_id: str = "ep-251-01",
) -> LedgerEntry:
    """One entry, carrying ``motive="engineering"`` on purpose.

    The motive-neutrality test asserts the string ``"engineering"`` never reaches
    the prompt, so every entry holding it makes that assertion mean something.
    """
    return LedgerEntry(
        entry_id=entry_id,
        ts=NOW,
        episode_id=episode_id,
        scope=SCOPE,
        claim=claim,
        kind=kind,
        claim_mode=claim_mode,
        subjects=subjects,
        objects=objects,
        identifiers=identifiers,
        motive="engineering",
        confidence=0.9,
        turns=(1,),
        receipt_id="r-fixture",
    )


TWO_ENTRIES: tuple[LedgerEntry, ...] = (
    _entry(
        "e-01",
        "The JedAI Gateway is the LiteLLM proxy that fronts the JedAI models.",
        kind=ClaimKind.IS,
        subjects=(GATEWAY,),
    ),
    _entry(
        "e-02",
        "The agent-memory MCP server was deployed to the C4 cluster in #240.",
        subjects=(MCP,),
        identifiers=("#240",),
    ),
)

RELATIONAL_ENTRIES: tuple[LedgerEntry, ...] = (
    _entry(
        "e-01",
        "The agent-memory MCP server is reached through the JedAI Gateway.",
        kind=ClaimKind.RELATION,
        subjects=(MCP,),
        objects=(GATEWAY,),
    ),
    _entry(
        "e-02",
        "The JedAI Gateway also fronts the agent-memory MCP server for reads.",
        subjects=(GATEWAY,),
        objects=(MCP,),
    ),
)
"""Two claims about one pair of concepts, so both are ``claim_index`` targets.

The first is relational by its ``kind`` and the second only by carrying
``objects``, which is deliberate: the rule is either, and an ``attribute`` whose
claim names a second concept is as much an edge as one the extractor labelled.
"""

TWELVE_ENTRIES: tuple[LedgerEntry, ...] = (
    _entry(
        "e-01",
        "Every real run of Memotron goes through the JedAI Gateway; hermetic mode is never used.",
        kind=ClaimKind.RULE,
        claim_mode=ClaimMode.DIRECTIVE,
        subjects=(GATEWAY,),
    ),
    _entry(
        "e-02",
        "The JedAI Gateway is the LiteLLM proxy that fronts the JedAI models.",
        kind=ClaimKind.IS,
        subjects=(GATEWAY,),
        objects=(PROXY,),
    ),
    _entry(
        "e-03",
        "The LiteLLM proxy and the JedAI Gateway are two surface names for one service.",
        kind=ClaimKind.IS,
        subjects=(PROXY, GATEWAY),
    ),
    _entry(
        "e-04",
        "The agent-memory MCP server was deployed to the C4 cluster in #240.",
        claim_mode=ClaimMode.REPORT,
        subjects=(MCP,),
        identifiers=("#240",),
    ),
    _entry(
        "e-05",
        "The C4 memory server returned 24 tools immediately after the #240 deploy.",
        claim_mode=ClaimMode.REPORT,
        subjects=(C4,),
        identifiers=("24", "#240"),
    ),
    _entry(
        "e-06",
        "The C4 memory server returns 31 tools; #248 corrected the earlier count of 24.",
        claim_mode=ClaimMode.CORRECTION,
        subjects=(C4,),
        identifiers=("31", "#248"),
    ),
    _entry(
        "e-07",
        "The JedAI Gateway refused the Host header until #245 fixed it.",
        claim_mode=ClaimMode.REPORT,
        subjects=(GATEWAY,),
        identifiers=("#245",),
    ),
    _entry(
        "e-08",
        "The JedAI Gateway loses session affinity intermittently; probe #246 names the failure.",
        kind=ClaimKind.UNSURE,
        subjects=(GATEWAY,),
        identifiers=("#246",),
    ),
    _entry(
        "e-09",
        "Embeddings use text-embedding-3 at 3072 dimensions, selected explicitly.",
        subjects=(GATEWAY,),
        identifiers=("text-embedding-3", "3072"),
    ),
    _entry(
        "e-10",
        "Chat defaults to claude-haiku-4-5 unless a caller asks for another model.",
        subjects=(GATEWAY,),
        identifiers=("claude-haiku-4-5",),
    ),
    _entry(
        "e-11",
        "The agent-memory MCP server and the C4 memory server are the same deployed server.",
        kind=ClaimKind.IS,
        subjects=(MCP, C4),
    ),
    _entry(
        "e-12",
        "The agent-memory MCP server is reached through the JedAI Gateway.",
        kind=ClaimKind.RELATION,
        subjects=(MCP,),
        objects=(GATEWAY,),
    ),
)
"""Twelve entries over four surface names: two synonym pairs, nothing else."""


@pytest.fixture
def ledger() -> Iterator[_SpyLedger]:
    store = _SpyLedger()
    yield store
    store.close()


@pytest.fixture
def receipts() -> InMemoryReceipts:
    return InMemoryReceipts()


@pytest.fixture
def graph() -> _SpyGraph:
    return _SpyGraph()


def _seed_node(
    graph: InMemoryGraph,
    ledger: CavemanLedger,
    *,
    name: str,
    node_type: str,
    gloss_claim: str,
    facts: tuple[Fact, ...] = FIXTURE_FACTS,
    aliases: Sequence[str] = (),
    now: datetime = NOW,
) -> str:
    """A node as it would look AFTER a dream: facts written, one claim behind it.

    The claim is what stage 2's candidate gloss is derived from, and the facts are
    what it must never render, so both halves of that invariant are present.
    """
    node = graph.create_node(
        scope=SCOPE,
        name=name,
        type=node_type,
        facts=facts,
        embedding=EMBEDDER.embed(f"{name} {node_type} {gloss_claim}"),
        now=now,
        aliases=aliases,
    )
    entry = _entry(
        f"e-seed-{node.node_id}",
        gloss_claim,
        kind=ClaimKind.IS,
        subjects=(name,),
        episode_id="ep-seed",
    )
    ledger.append(entry)
    ledger.bind_nodes(entry.entry_id, [node.node_id])
    return node.node_id


@pytest.fixture
def three_nodes(graph: _SpyGraph, ledger: _SpyLedger) -> tuple[str, str, str]:
    """A three-node scope, each node already dreamed and each with a ledger claim."""
    first = _seed_node(
        graph,
        ledger,
        name="gateway",
        node_type="service",
        gloss_claim="The gateway is the LiteLLM proxy fronting the JedAI models.",
    )
    second = _seed_node(
        graph,
        ledger,
        name="chart",
        node_type="artifact",
        gloss_claim="The chart is the Helm chart that deploys Memotron.",
    )
    third = _seed_node(
        graph,
        ledger,
        name="c4",
        node_type="cluster",
        gloss_claim="C4 is the cluster the memory server is deployed to.",
    )
    graph.dirty_calls.clear()
    graph.relation_calls.clear()
    ledger.bind_calls.clear()
    ledger.cooccurrence_returns.clear()
    return first, second, third


def _binding(
    local_name: str,
    *,
    node_id: str | None = None,
    new_name: str | None = None,
    new_type: str = "service",
    new_gloss: str = "one line saying what it is",
    reason: str = "because",
) -> dict[str, Any]:
    return {
        "local_name": local_name,
        "decision": "bind" if node_id is not None else "new",
        "node_id": node_id,
        "new_node": (None if new_name is None else {"name": new_name, "type": new_type, "gloss": new_gloss}),
        "reason": reason,
    }


def _relation(claim_index: int, source: str, target: str, edge_type: str) -> dict[str, Any]:
    return {"claim_index": claim_index, "source": source, "target": target, "type": edge_type}


def _payload(*bindings: dict[str, Any], relations: Sequence[dict[str, Any]] | None = None) -> str:
    """One scripted answer.

    ``relations=None`` omits the key entirely rather than sending an empty array,
    which is what most of these batches do: their claims name one concept each,
    so there is nothing to type and the contract's default is what should carry
    them. A batch WITH relational claims passes the list.
    """
    body: dict[str, Any] = {"bindings": list(bindings)}
    if relations is not None:
        body["relations"] = list(relations)
    return json.dumps(body)


TWELVE_BINDINGS: tuple[dict[str, Any], ...] = (
    _binding(GATEWAY, new_name="gateway", new_type="service"),
    _binding(PROXY, new_name="gateway", new_type="service"),
    _binding(MCP, new_name="agent-memory", new_type="service"),
    _binding(C4, new_name="agent-memory", new_type="service"),
)
"""The whole 12-entry batch's routing answer: four names, two convergences."""

TWELVE_RELATIONS: tuple[dict[str, Any], ...] = (
    _relation(0, GATEWAY, PROXY, "SAME_AS"),
    _relation(1, MCP, GATEWAY, "REACHED_THROUGH"),
)
"""Its two relational claims, in ``claim_index`` order.

``e-02`` carries ``objects`` and ``e-12`` is a ``relation``; nothing else in the
batch names two concepts, so those two positions are the whole list. The first
one's ends converge onto one node, so only the second becomes an edge -- which is
the case the integration test exists to pin.
"""


async def _run(
    raw: str,
    *,
    graph: _SpyGraph,
    ledger: _SpyLedger,
    receipts: InMemoryReceipts,
    entries: Sequence[LedgerEntry] = TWO_ENTRIES,
    motive: CavemanMotive | None = None,
    append: bool = True,
    now: datetime = NOW,
) -> tuple[ReconcileOutcome, ScriptedChatTransport]:
    if append:
        for entry in entries:
            ledger.append(entry)
        ledger.bind_calls.clear()
    transport = ScriptedChatTransport([raw])
    outcome = await reconcile(
        entries=entries,
        scope=SCOPE,
        motive=motive or engineering_motive(),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        embedder=EMBEDDER,
        transport=transport,
        now=now,
    )
    return outcome, transport


# ============================================================ bind, and create


async def test_a_bind_decision_binds_the_entry_to_the_offered_node(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    gateway, _, c4 = three_nodes
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, node_id=c4)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert outcome.bindings == {GATEWAY: gateway, MCP: c4}
    assert outcome.created == ()
    assert outcome.bound == (gateway, c4)
    assert ledger.get("e-01").node_ids == (gateway,)


async def test_a_new_decision_creates_a_node(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    outcome, _ = await _run(
        _payload(
            _binding(GATEWAY, new_name="gateway-2", new_type="service"),
            _binding(MCP, new_name="agent-memory", new_type="service"),
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert len(outcome.created) == 2
    assert graph.count(scope=SCOPE) == 5
    assert {graph.get_node(node_id).name for node_id in outcome.created} == {"gateway-2", "agent-memory"}


async def test_a_created_node_starts_with_no_lines_and_dirty(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Stage 2 routes. The first line set is a compression decision, and
    compression has exactly one owner."""
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    for node_id in outcome.created:
        node = graph.get_node(node_id)
        assert node.facts == ()
        assert node.dirty is True
        assert node.dreamed_at is None


async def test_two_names_sharing_one_new_node_name_create_exactly_one_node(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Within-batch reconciliation, the outcome the whole batched call exists for."""
    entries = (
        _entry("e-01", "The JedAI Gateway is the LiteLLM proxy fronting the models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy refused the Host header until #245.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(PROXY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert len(outcome.created) == 1
    only = outcome.created[0]
    assert outcome.bindings == {GATEWAY: only, PROXY: only}
    assert ledger.get("e-01").node_ids == (only,)
    assert ledger.get("e-02").node_ids == (only,)


async def test_a_new_node_group_matches_on_case_folded_names(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """ "Gateway" and "gateway" are one concept, and the model said so twice."""
    entries = (
        _entry("e-01", "The JedAI Gateway fronts the JedAI models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy fronts the JedAI models.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(
            _binding(GATEWAY, new_name="Gateway", new_gloss="g"),
            _binding(PROXY, new_name="gateway", new_gloss="g"),
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert len(outcome.created) == 1
    assert graph.get_node(outcome.created[0]).name == "Gateway"


async def test_objects_are_adjudicated_as_well_as_subjects(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A relation's other end must route, or the edge it states can never form."""
    entries = (
        _entry(
            "e-01",
            "The agent-memory MCP server is reached through the JedAI Gateway.",
            kind=ClaimKind.RELATION,
            subjects=(MCP,),
            objects=(GATEWAY,),
        ),
    )
    outcome, transport = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert set(outcome.bindings) == {MCP, GATEWAY}
    assert f'"{GATEWAY}"' in transport.last.prompt
    assert len(ledger.get("e-01").node_ids) == 2


# ==================================================== the writes, and their order


async def test_bind_nodes_is_called_once_per_entry(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert [entry_id for entry_id, _ in ledger.bind_calls] == ["e-01", "e-02"]


async def test_an_entry_naming_two_concepts_is_bound_to_both_in_one_call(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """One call per entry, not one per name -- the entry is the unit of binding."""
    entries = (
        _entry(
            "e-01",
            "The agent-memory MCP server and the C4 memory server are the same server.",
            kind=ClaimKind.IS,
            subjects=(MCP, C4),
        ),
    )
    outcome, _ = await _run(
        _payload(_binding(MCP, new_name="agent-memory"), _binding(C4, new_name="c4-server")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert len(ledger.bind_calls) == 1
    assert set(ledger.bind_calls[0][1]) == set(outcome.created)


async def test_mark_dirty_covers_every_touched_node(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    gateway, _, _ = three_nodes
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert graph.dirty_calls == [outcome.dirty]
    assert set(outcome.dirty) == {gateway, *outcome.created}
    assert {node.node_id for node in graph.dirty(scope=SCOPE)} >= set(outcome.dirty)


async def test_one_relation_is_upserted_per_relational_claim_the_model_typed(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The edge is the model's: its type, its direction, the ledger claim verbatim.

    Two claims about one pair, typed as two different beliefs, are two edges --
    the triple ``(source, type, target)`` is the identity, and ``REACHED_THROUGH``
    and ``FRONTS`` are two of them. Each carries its own claim and its own entry
    as evidence, which is what a co-occurrence weight could not say.
    """
    entries = RELATIONAL_ENTRIES
    outcome, _ = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    mcp_node, gateway_node = outcome.created

    # Directed as the model named it, not canonically oriented: a typed belief
    # has a subject, and sorting the ends would assert the opposite claim.
    assert graph.relation_calls == [
        (mcp_node, gateway_node, "REACHED_THROUGH", ("e-01",)),
        (gateway_node, mcp_node, "FRONTS", ("e-02",)),
    ]
    relations = graph.relations(scope=SCOPE)
    assert len(relations) == 2
    assert {relation.triple for relation in relations} == {
        (mcp_node, "REACHED_THROUGH", gateway_node),
        (gateway_node, "FRONTS", mcp_node),
    }
    assert {relation.claim for relation in relations} == {entry.claim for entry in entries}
    assert all(relation.evidence == 1 and relation.until is None for relation in relations)
    assert len(outcome.relations_created) == 2
    assert outcome.relations_reinforced == ()
    assert outcome.edge_types_new == ("REACHED_THROUGH", "FRONTS")


async def test_two_claims_typed_the_same_way_reinforce_one_edge(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Restating a belief makes the record stronger, not a second record.

    ``upsert_relation`` unions on the ``(source, type, target)`` identity, so the
    second claim appends its entry id and moves ``last_seen``. That is what makes
    ``Relation.evidence`` a count of how well attested a belief is rather than a
    count of how often somebody wrote it down -- and the outcome reports the
    second call as a reinforcement rather than a creation.
    """
    outcome, _ = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, MCP, GATEWAY, "REACHED_THROUGH"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=RELATIONAL_ENTRIES,
    )
    relations = graph.relations(scope=SCOPE)
    assert len(relations) == 1
    assert relations[0].evidence == 2
    assert set(relations[0].entry_ids) == {"e-01", "e-02"}
    assert len(graph.relation_calls) == 2

    # The same id in both tuples, on purpose: the belief arrived and was
    # confirmed in one ingest, and the fields classify what the calls DID.
    assert outcome.relations_created == (relations[0].relation_id,)
    assert outcome.relations_reinforced == (relations[0].relation_id,)
    # One word entered the vocabulary, whichever claim first used it.
    assert outcome.edge_types_new == ("REACHED_THROUGH",)


async def test_a_relation_whose_two_names_converged_writes_nothing(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A claim saying two names are one thing has no edge to write.

    The model answered in contract -- two surface names, two different names --
    and this stage then routed both to one node. An edge from that node to itself
    asserts nothing a fact cannot state better and ``Relation`` refuses one, so
    the claim is skipped rather than the batch rejected.
    """
    entries = (
        _entry(
            "e-01",
            "The JedAI Gateway is the LiteLLM proxy that fronts the JedAI models.",
            kind=ClaimKind.IS,
            subjects=(GATEWAY,),
            objects=(PROXY,),
        ),
    )
    outcome, _ = await _run(
        _payload(
            _binding(GATEWAY, new_name="gateway"),
            _binding(PROXY, new_name="gateway"),
            relations=[_relation(0, GATEWAY, PROXY, "SAME_AS")],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert len(outcome.created) == 1
    assert graph.relation_calls == []
    assert graph.relations(scope=SCOPE) == []
    assert outcome.relations_created == ()
    assert outcome.relations_reinforced == ()
    assert outcome.edge_types_new == ()


async def test_a_claim_naming_one_concept_writes_no_relation(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """An edge needs two ends. A unary claim only binds and marks dirty."""
    entries = (_entry("e-01", "The JedAI Gateway is a LiteLLM proxy.", kind=ClaimKind.IS, subjects=(GATEWAY,)),)
    await _run(
        _payload(_binding(GATEWAY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert graph.relation_calls == []
    assert graph.relations(scope=SCOPE) == []


async def test_nothing_is_written_before_the_response_is_validated(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """A rejection must leave the scope byte-identical, so validation runs first."""
    before = graph.list_nodes(scope=SCOPE)
    with pytest.raises(OutOfContractResponse):
        await _run(
            _payload(_binding(GATEWAY, node_id="n-999")),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
        )
    assert graph.list_nodes(scope=SCOPE) == before
    assert graph.dirty_calls == []
    assert graph.relation_calls == []
    assert ledger.bind_calls == []


# ===================================================================== pressure


async def test_pressure_is_the_overshoot_of_the_node_budget(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """``pressure = max(0, count - N)``, computed after this batch's creations."""
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway-2"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        motive=engineering_motive().model_copy(update={"max_nodes": 3}),
    )
    assert graph.count(scope=SCOPE) == 5
    assert outcome.pressure == 2


async def test_pressure_is_zero_with_headroom(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    gateway, _, _ = three_nodes
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, node_id=gateway)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert outcome.pressure == 0


# ============================================================ motive-neutrality


async def test_the_prompt_names_neither_the_motive_nor_its_goal(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """Stage 2 is the persona-independent truth layer: one graph per scope.

    The fixture entries carry ``motive="engineering"`` themselves, so this also
    proves the entry rendering does not leak the policy that selected the claim.
    """
    motive = engineering_motive()
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        motive=motive,
    )
    whole = transport.last.system_prompt + transport.last.prompt
    assert motive.name not in whole
    assert motive.goal not in whole
    for bullet in (*motive.extract_rubric, *motive.dream_rubric, *motive.extract_exclusions):
        assert bullet not in whole


async def test_the_prompt_renders_a_candidates_gloss_and_never_its_facts(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """A node's lines are motive-shaped. Its ledger claim is not."""
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway-2"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    for fact in FIXTURE_FACTS:
        assert fact.text not in prompt
    assert "The gateway is the LiteLLM proxy fronting the JedAI models." in prompt


async def test_a_candidate_with_no_ledger_claim_renders_a_placeholder_gloss(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A node created and not yet dreamed has no claim to describe it."""
    graph.create_node(
        scope=SCOPE,
        name="orphan",
        type="service",
        facts=(),
        embedding=EMBEDDER.embed("orphan"),
        now=NOW,
    )
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert "no claims recorded yet" in transport.last.prompt


async def test_a_long_gloss_is_truncated(graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts) -> None:
    """The candidate table is O(names x k) lines; an unbounded gloss would own it."""
    long_claim = "The gateway is " + "very " * 70 + "long."
    _seed_node(graph, ledger, name="gateway", node_type="service", gloss_claim=long_claim, facts=())
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway-2"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert long_claim not in transport.last.prompt
    assert long_claim[:120] in transport.last.prompt


async def test_the_prompt_carries_the_claims_and_identifiers_of_each_name(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    assert TWO_ENTRIES[0].claim in prompt
    assert "identifiers: #240" in prompt
    assert f'"{GATEWAY}"' in prompt
    assert f'"{MCP}"' in prompt


async def test_the_prompt_offers_the_scope_type_vocabulary(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway-2"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert "SCOPE TYPE VOCABULARY: artifact, cluster, service" in transport.last.prompt


async def test_an_empty_scope_says_so_rather_than_offering_nothing(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    assert "this scope has no nodes yet" in prompt
    assert "every name in this batch is new" in prompt
    assert "CANDIDATES OFFERED: none" in prompt


async def test_the_prompt_lists_the_edge_type_vocabulary_with_its_counts(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """The line that makes "an existing type applied" a decision at all.

    With the counts, because a type carrying three edges is this scope's word for
    that belief and a type carrying one is a coinage that has not caught on, and
    a model shown only the words cannot tell them apart. Most used first, so the
    ordering does not move between runs for the same graph.
    """
    gateway, chart, c4 = three_nodes
    for source, target, edge_type in (
        (chart, gateway, "DEPLOYS"),
        (chart, c4, "DEPLOYS"),
        (gateway, c4, "BLOCKED"),
    ):
        graph.upsert_relation(
            scope=SCOPE,
            source_id=source,
            target_id=target,
            type=edge_type,
            claim="a fixture edge so the vocabulary has something in it",
            entry_ids=("e-fixture",),
            until=None,
            now=NOW,
        )
    graph.relation_calls.clear()
    _, transport = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert "EDGE TYPES IN THIS SCOPE: DEPLOYS (2), BLOCKED (1)" in transport.last.prompt


async def test_a_scope_with_no_edges_says_none_yet_rather_than_nothing(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A vocabulary line rendering as nothing reads as a truncated prompt.

    And a model that suspects the list was cut short coins a type instead of
    reusing one, which is the failure the whole line exists to prevent.
    """
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert f"EDGE TYPES IN THIS SCOPE: {NO_EDGE_TYPES}" in transport.last.prompt


async def test_the_prompt_lists_every_relational_claim_with_its_index_and_ends(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """``claim_index`` is a position in this list, so the list has to be the list.

    The names are repeated beside each claim rather than left to be found in the
    surface-name section, which is keyed by name and splits one claim across
    every name in it.
    """
    _, transport = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=RELATIONAL_ENTRIES,
    )
    prompt = transport.last.prompt
    # This stage names an edge's ends by SURFACE NAME, which is the opposite of a
    # dream answer: a name answered "new" has no node id yet. So the by-id rule
    # belongs to the dream prompts and appears NOWHERE here -- not stated and then
    # overridden, which is the self-contradicting prompt this branch has lost live
    # runs to. The by-name rule is stated once, in the section that asks for it.
    assert RELATION_TARGET_RULE not in prompt
    assert "OVERRIDES" not in prompt
    assert 'SURFACE NAME as spelled above -- a name you answer "new" has no node id' in prompt

    assert "RELATIONAL CLAIMS TO TYPE (2)" in prompt
    assert f"  claim_index 0\n    CLAIM: {RELATIONAL_ENTRIES[0].claim}\n" in prompt
    assert f'    NAMES IN THIS CLAIM: "{MCP}", "{GATEWAY}"\n' in prompt
    assert f"  claim_index 1\n    CLAIM: {RELATIONAL_ENTRIES[1].claim}\n" in prompt


async def test_a_batch_whose_claims_each_name_one_concept_lists_no_relational_claims(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """And says so in words, so an empty section is not read as a cut-off prompt."""
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    assert "RELATIONAL CLAIMS TO TYPE (0)" in prompt
    assert "(none -- no claim in this batch names two concepts)" in prompt
    assert '"relations": []' in prompt


async def test_everything_this_stage_says_to_the_model_is_printable_ascii(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """Plain ASCII words and structure, with the claims as the only variable.

    No arrow, no middle dot, no invented alphabet: each of those is a token a
    tokeniser spends and a convention the model has to be taught, and this branch
    lost live runs to exactly that. The fixture's own text is ASCII, so anything
    non-ASCII in the result was added by the rendering rather than by the record.
    """
    _, transport = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=RELATIONAL_ENTRIES,
    )
    assert RECONCILE_SYSTEM.isascii()
    assert transport.last.prompt.isascii()


def test_the_system_prompt_names_the_three_matcher_decisions() -> None:
    """The user's own framing, and the reason the third is spelled out at length.

    A new node, a new edge type, or an existing type applied to another pair.
    Reuse is the commonest and the one a model working down a list will skip, so
    the prompt says which one that is rather than listing three equals.
    """
    assert "A NEW NODE" in RECONCILE_SYSTEM
    assert "A NEW EDGE TYPE" in RECONCILE_SYSTEM
    assert "AN EXISTING TYPE APPLIED" in RECONCILE_SYSTEM
    assert "THE THIRD IS THE COMMONEST" in RECONCILE_SYSTEM


async def test_the_prompt_ends_with_the_shared_closing_line(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert transport.last.prompt.rstrip().endswith("Answer with one JSON object and nothing else.")
    assert transport.last.system_prompt == RECONCILE_SYSTEM


def test_the_system_prompt_gives_the_two_cautions_their_own_directions() -> None:
    """They point opposite ways, and conflating them cost a live run.

    Bind-versus-new: when unsure choose new, because a wrong bind welds two
    different things together and is nearly undetectable afterwards. Grouping two
    NEW names: when unsure GROUP them, because a node holding two topics is split
    by the dreamer as a matter of routine while two nodes that should have been
    one wait for a merge to notice.

    The prompt used to state only the first, and the model applied it to the
    second — answering "new" three times with three different node names for one
    MCP server. So the prompt must carry both, each pointing its own way.
    """
    assert 'when unsure, choose "new"' in RECONCILE_SYSTEM
    assert "GROUPING TWO NEW NAMES -- when unsure, GROUP THEM" in RECONCILE_SYSTEM
    assert "OPPOSITE DIRECTIONS" in RECONCILE_SYSTEM
    assert "engineering" not in RECONCILE_SYSTEM


def test_the_system_prompt_makes_grouping_a_step_before_answering() -> None:
    """The rule alone was not a procedure a model working down a list would reach."""
    assert "BEFORE YOU ANSWER ANYTHING" in RECONCILE_SYSTEM
    assert "SAME new_node.name" in RECONCILE_SYSTEM


async def test_one_llm_call_serves_the_whole_batch(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Four names, one call. Per-name calls could not reconcile within a batch."""
    _, transport = await _run(
        _payload(*TWELVE_BINDINGS, relations=TWELVE_RELATIONS),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=TWELVE_ENTRIES,
    )
    assert transport.call_count == 1
    transport.assert_exhausted()


async def test_each_name_is_offered_at_most_the_candidate_ceiling(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """kNN, not the whole scope: the candidate list is per name and capped."""
    for index in range(CANDIDATE_K + 2):
        _seed_node(
            graph,
            ledger,
            name=f"node-{index}",
            node_type="service",
            gloss_claim=f"Node {index} is one of the seeded fixtures.",
            facts=(),
        )
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    offered = [
        line.split(":", 1)[1].split(", ")
        for line in transport.last.prompt.splitlines()
        if line.strip().startswith("CANDIDATES OFFERED:")
    ]
    assert len(offered) == 2
    assert all(len(ids) == CANDIDATE_K for ids in offered)


# =================================================================== rejections


def _rejecting_payloads() -> list[tuple[str, str]]:
    """The five rejections unit 19 names, each with the rule it breaks."""
    return [
        ("a local_name missing from the response", _payload(_binding(GATEWAY, new_name="gateway"))),
        (
            "an extra local_name nobody asked about",
            _payload(
                _binding(GATEWAY, new_name="gateway"),
                _binding(MCP, new_name="agent-memory"),
                _binding("the chart", new_name="chart", new_type="artifact"),
            ),
        ),
        (
            "a bind to an id that was never offered",
            _payload(_binding(GATEWAY, node_id="n-999"), _binding(MCP, new_name="agent-memory")),
        ),
        (
            "two same-named new_nodes disagreeing on type",
            _payload(
                _binding(GATEWAY, new_name="gateway", new_type="service"),
                _binding(MCP, new_name="gateway", new_type="cluster"),
            ),
        ),
        (
            "one local_name adjudicated twice",
            _payload(
                _binding(GATEWAY, new_name="gateway"),
                _binding(GATEWAY, new_name="gateway-2"),
                _binding(MCP, new_name="agent-memory"),
            ),
        ),
        (
            "a relation typed for a batch that has no relational claim",
            _payload(
                _binding(GATEWAY, new_name="gateway"),
                _binding(MCP, new_name="agent-memory"),
                relations=[_relation(0, GATEWAY, MCP, "FRONTS")],
            ),
        ),
    ]


def _rejecting_relation_payloads() -> list[tuple[str, str]]:
    """The five ways the relation half of an answer can be wrong.

    Against ``RELATIONAL_ENTRIES``, which has exactly two claims to type, so
    "untyped" and "typed twice" are both expressible in one fixture.
    """
    bindings = (_binding(MCP, new_name="agent-memory"), _binding(GATEWAY, new_name="gateway"))
    return [
        (
            "a relational claim left untyped",
            _payload(*bindings, relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")]),
        ),
        (
            "one claim_index typed twice and the other not at all",
            _payload(
                *bindings,
                relations=[
                    _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                    _relation(0, GATEWAY, MCP, "FRONTS"),
                ],
            ),
        ),
        (
            "a claim_index past the end of the list",
            _payload(
                *bindings,
                relations=[
                    _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                    _relation(1, GATEWAY, MCP, "FRONTS"),
                    _relation(2, GATEWAY, MCP, "FRONTS"),
                ],
            ),
        ),
        (
            "an end that was not a surface name in this batch",
            _payload(
                *bindings,
                relations=[
                    _relation(0, MCP, "the Helm chart", "DEPLOYS"),
                    _relation(1, GATEWAY, MCP, "FRONTS"),
                ],
            ),
        ),
        (
            "one name given as both ends",
            _payload(
                *bindings,
                relations=[
                    _relation(0, MCP, MCP, "REACHED_THROUGH"),
                    _relation(1, GATEWAY, MCP, "FRONTS"),
                ],
            ),
        ),
        (
            "a type that is not UPPER_SNAKE",
            _payload(
                *bindings,
                relations=[
                    _relation(0, MCP, GATEWAY, "reached through"),
                    _relation(1, GATEWAY, MCP, "FRONTS"),
                ],
            ),
        ),
    ]


@pytest.mark.parametrize(("label", "payload"), _rejecting_payloads())
async def test_each_bad_adjudication_raises(
    label: str, payload: str, graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(payload, graph=graph, ledger=ledger, receipts=receipts)
    assert caught.value.source == "RECONCILE", label
    assert caught.value.errors, label


@pytest.mark.parametrize(("label", "payload"), _rejecting_payloads())
async def test_each_bad_adjudication_writes_nothing(
    label: str, payload: str, graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Zero writes. A partial route leaves entries bound to a graph nobody chose."""
    with pytest.raises(OutOfContractResponse):
        await _run(payload, graph=graph, ledger=ledger, receipts=receipts)
    assert graph.list_nodes(scope=SCOPE) == [], label
    assert ledger.unbound(scope=SCOPE) == list(TWO_ENTRIES), label
    assert ledger.bind_calls == []


@pytest.mark.parametrize(("label", "payload"), _rejecting_payloads())
async def test_each_bad_adjudication_emits_exactly_one_reject_receipt(
    label: str, payload: str, graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    with pytest.raises(OutOfContractResponse):
        await _run(payload, graph=graph, ledger=ledger, receipts=receipts)
    emitted = receipts.all()
    assert len(emitted) == 1, label
    assert emitted[0].op is ReceiptOp.RECONCILE_REJECTED


@pytest.mark.parametrize(("label", "payload"), _rejecting_relation_payloads())
async def test_each_bad_relation_answer_raises_and_writes_nothing(
    label: str, payload: str, graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Zero writes on the relation half too: no node, no binding, no edge.

    The relation rules are checked before the write phase precisely so that this
    holds. A relation validated after the nodes were created would leave a routed
    graph whose edges nobody agreed to.
    """
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(payload, graph=graph, ledger=ledger, receipts=receipts, entries=RELATIONAL_ENTRIES)
    assert caught.value.source == "RECONCILE", label
    assert caught.value.errors, label
    assert graph.list_nodes(scope=SCOPE) == [], label
    assert graph.relations(scope=SCOPE) == [], label
    assert graph.relation_calls == [], label
    assert ledger.bind_calls == [], label
    assert len(receipts.all()) == 1, label
    assert receipts.all()[0].op is ReceiptOp.RECONCILE_REJECTED, label


@pytest.mark.parametrize(
    ("relations", "error", "detail"),
    [
        pytest.param(
            [_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
            "relations: claim_index 1 was not typed",
            "1 relational claim(s) untyped",
            id="untyped",
        ),
        pytest.param(
            [_relation(0, MCP, GATEWAY, "REACHED_THROUGH"), _relation(0, GATEWAY, MCP, "FRONTS")],
            "relations: claim_index 0 was typed more than once",
            "1 relational claim(s) untyped; 1 claim_index typed more than once",
            id="twice",
        ),
        pytest.param(
            [
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
                _relation(7, GATEWAY, MCP, "FRONTS"),
            ],
            "relations: claim_index 7 is not a relational claim in this batch",
            "1 claim_index not in this batch",
            id="unknown",
        ),
        pytest.param(
            [_relation(0, MCP, "the Helm chart", "DEPLOYS"), _relation(1, GATEWAY, MCP, "FRONTS")],
            "relations: claim_index 0 names 'the Helm chart', which is not a surface name here",
            "1 relation end(s) not a surface name",
            id="unknown-end",
        ),
        pytest.param(
            [_relation(0, MCP, MCP, "REACHED_THROUGH"), _relation(1, GATEWAY, MCP, "FRONTS")],
            f"relations: claim_index 0 points {MCP!r} at itself",
            "1 relation(s) naming one end twice",
            id="self",
        ),
    ],
)
async def test_a_bad_relation_is_named_on_the_exception_and_counted_on_the_receipt(
    relations: list[dict[str, Any]],
    error: str,
    detail: str,
    graph: _SpyGraph,
    ledger: _SpyLedger,
    receipts: InMemoryReceipts,
) -> None:
    """The value goes to the developer; the durable row keeps only the count.

    Same split as the binding half, and for the same reason: a receipt holding a
    surface name or a claim is a durable row holding the episode's words.
    """
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(
                _binding(MCP, new_name="agent-memory"),
                _binding(GATEWAY, new_name="gateway"),
                relations=relations,
            ),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            entries=RELATIONAL_ENTRIES,
        )
    assert error in " ".join(caught.value.errors)
    emitted = receipts.all()[0]
    assert emitted.detail == detail
    assert MCP not in emitted.detail
    assert RELATIONAL_ENTRIES[0].claim not in str(emitted)


async def test_one_rejection_reports_both_halves_of_a_wrong_answer(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A binding failure must not hide a relation failure, or the fix is two runs.

    One receipt either way, because one call produced one answer and a stage that
    receipted twice for one decision would double-count its own rejections.
    """
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(
                _binding(MCP, new_name="agent-memory"),
                relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
            ),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            entries=RELATIONAL_ENTRIES,
        )
    joined = " ".join(caught.value.errors)
    assert f"bindings: no adjudication for surface name {GATEWAY!r}" in joined
    assert "relations: claim_index 1 was not typed" in joined
    assert receipts.all()[0].detail == "1 surface name(s) unadjudicated; 1 relational claim(s) untyped"
    assert len(receipts.all()) == 1


async def test_a_missing_name_is_named_on_the_exception_and_counted_on_the_receipt(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The value goes to the developer; the durable row keeps only the count."""
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(_payload(_binding(GATEWAY, new_name="gateway")), graph=graph, ledger=ledger, receipts=receipts)
    assert f"no adjudication for surface name {MCP!r}" in " ".join(caught.value.errors)
    detail = receipts.all()[0].detail
    assert detail == "1 surface name(s) unadjudicated"
    assert MCP not in detail


async def test_a_bind_to_an_unoffered_id_is_named_as_such(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(_binding(GATEWAY, node_id="n-999"), _binding(MCP, new_name="agent-memory")),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
        )
    assert "was not offered for that name" in " ".join(caught.value.errors)
    assert receipts.all()[0].detail == "1 bind(s) to a node not offered for that name"


async def test_a_bind_to_a_real_node_outside_this_names_candidates_is_rejected(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The offering is PER NAME, not per batch -- an existing id is not a licence.

    With more nodes than the candidate ceiling, some node is genuinely absent
    from a given name's kNN result; binding to that one must fail.
    """
    for index in range(CANDIDATE_K + 2):
        _seed_node(
            graph,
            ledger,
            name=f"node-{index}",
            node_type="service",
            gloss_claim=f"Node {index} is one of the seeded fixtures.",
            facts=(),
        )
    for entry in TWO_ENTRIES:
        ledger.append(entry)
    named = [entry for entry in TWO_ENTRIES if GATEWAY in entry.subjects]
    offered = {
        node.node_id
        for node, _ in graph.knn(scope=SCOPE, vector=EMBEDDER.embed(query_text(GATEWAY, named)), k=CANDIDATE_K)
    }
    unoffered = sorted({node.node_id for node in graph.list_nodes(scope=SCOPE)} - offered)
    assert unoffered, "the fixture must hold more nodes than the candidate ceiling"

    with pytest.raises(OutOfContractResponse) as caught:
        await _run(
            _payload(_binding(GATEWAY, node_id=unoffered[0]), _binding(MCP, new_name="agent-memory")),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            append=False,
        )
    assert unoffered[0] in " ".join(caught.value.errors)


async def test_a_transport_failure_is_not_receipted_as_a_rejection(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    transport = ScriptedChatTransport([ValueError("caveman chat request failed: connection reset")])
    with pytest.raises(ValueError, match="connection reset"):
        await reconcile(
            entries=TWO_ENTRIES,
            scope=SCOPE,
            motive=engineering_motive(),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            embedder=EMBEDDER,
            transport=transport,
            now=NOW,
        )
    assert receipts.all() == []
    assert graph.list_nodes(scope=SCOPE) == []


async def test_a_rejected_response_is_not_re_prompted(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    transport = ScriptedChatTransport(
        [
            "not json",
            _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        ]
    )
    with pytest.raises(OutOfContractResponse):
        await reconcile(
            entries=TWO_ENTRIES,
            scope=SCOPE,
            motive=engineering_motive(),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            embedder=EMBEDDER,
            transport=transport,
            now=NOW,
        )
    assert transport.call_count == 1
    assert transport.remaining == 1


# ============================================================== preconditions


async def test_an_empty_batch_fails_fast(graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts) -> None:
    transport = ScriptedChatTransport([])
    with pytest.raises(ValueError, match="at least one ledger entry"):
        await reconcile(
            entries=(),
            scope=SCOPE,
            motive=engineering_motive(),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            embedder=EMBEDDER,
            transport=transport,
            now=NOW,
        )
    assert transport.call_count == 0


async def test_an_entry_from_another_scope_fails_fast(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """One graph per scope, so a mixed batch has no single graph to route into."""
    foreign = TWO_ENTRIES[0].model_copy(update={"scope": "repo:jedai/other"})
    transport = ScriptedChatTransport([])
    with pytest.raises(ValueError, match="another scope"):
        await reconcile(
            entries=(foreign, TWO_ENTRIES[1]),
            scope=SCOPE,
            motive=engineering_motive(),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            embedder=EMBEDDER,
            transport=transport,
            now=NOW,
        )
    assert transport.call_count == 0


@pytest.mark.parametrize("field", ["subjects", "objects"])
async def test_a_blank_surface_name_fails_before_the_llm_call(
    field: str, graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """``LedgerEntry`` bounds the tuple's length, not each name inside it.

    A blank name is an empty search key, and the alias seam refuses one. Finding
    that out inside the write phase — after nodes were already created — is the
    one way this stage could leave a partial route behind, so it is a
    precondition checked with the other two, before the model is ever called.
    """
    blank = TWO_ENTRIES[0].model_copy(update={field: ("   ",) if field == "objects" else (GATEWAY, "   ")})
    transport = ScriptedChatTransport([])
    with pytest.raises(ValueError, match="blank surface name"):
        await reconcile(
            entries=(blank, TWO_ENTRIES[1]),
            scope=SCOPE,
            motive=engineering_motive(),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
            embedder=EMBEDDER,
            transport=transport,
            now=NOW,
        )
    assert transport.call_count == 0
    assert graph.list_nodes(scope=SCOPE) == []
    assert receipts.all() == []


async def test_a_relational_claim_longer_than_a_fact_is_carried_verbatim(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """``LedgerEntry.claim`` and ``Relation.claim`` share one bound, so nothing is unroutable.

    331 characters -- past ``MAX_FACT_TEXT`` and inside ``MAX_CLAIM_TEXT``, which
    is the window in which this stage used to refuse the whole batch before
    calling the model. The edge now carries it verbatim, which is what "a
    provisional relation carries the ledger claim verbatim" means.
    """
    claim = "The gateway blocks " + "the server " * 28 + "now."
    assert MAX_FACT_TEXT < len(claim) <= MAX_CLAIM_TEXT
    entry = _entry("e-01", claim, kind=ClaimKind.RELATION, subjects=(MCP,), objects=(GATEWAY,))
    outcome, _ = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=(entry,),
    )
    assert len(outcome.relations_created) == 1
    stored = graph.relations(scope=SCOPE)
    assert len(stored) == 1
    assert stored[0].claim == claim


async def test_a_unary_claim_that_long_is_routed_without_complaint(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A claim naming one concept becomes a FACT, and the dreamer compresses it.

    So a 380-character sentence routes here and never has to fit an edge.
    """
    long_claim = "The gateway is " + "very " * 70 + "long."
    assert len(long_claim) > MAX_FACT_TEXT
    entry = _entry("e-01", long_claim, kind=ClaimKind.IS, subjects=(GATEWAY,))
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=(entry,),
    )
    assert len(outcome.created) == 1
    assert graph.relations(scope=SCOPE) == []


# ================================================================== receipts


async def test_one_receipt_is_emitted_per_adjudicated_name(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    gateway, _, _ = three_nodes
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    emitted = receipts.all()
    assert [receipt.op for receipt in emitted] == [ReceiptOp.RECONCILE_BOUND, ReceiptOp.RECONCILE_NEW]
    assert [receipt.subject for receipt in emitted] == [gateway, outcome.created[0]]


async def test_two_converged_names_receipt_twice_against_one_node(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """One receipt per DECISION. Two names converging is two decisions, one node."""
    entries = (
        _entry("e-01", "The JedAI Gateway fronts the JedAI models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy fronts the JedAI models.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(PROXY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    emitted = receipts.all()
    assert len(emitted) == 2
    assert {receipt.subject for receipt in emitted} == {outcome.created[0]}
    assert all("names_in_group=2" in receipt.detail for receipt in emitted)


async def test_a_receipt_carries_no_surface_name_or_claim_text(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Counts and digests. A durable row must not hold the episode's words."""
    await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    for receipt in receipts.all():
        assert GATEWAY not in str(receipt)
        assert "#240" not in str(receipt)


async def test_a_receipt_records_how_many_candidates_the_name_was_offered(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """So a systematically bad bind is diagnosable from the stream alone."""
    gateway, _, _ = three_nodes
    await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, node_id=gateway)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert all("candidates_offered=3" in receipt.detail for receipt in receipts.all())


async def test_the_receipts_are_attributed_to_the_scope_and_stamped_with_now(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert receipts.all(scope=SCOPE) == receipts.all()
    assert {receipt.ts for receipt in receipts.all()} == {NOW}


async def test_a_rejection_and_an_acceptance_of_one_batch_share_a_subject(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The batch has no id of its own, so its subject is a digest of its entries."""
    with pytest.raises(OutOfContractResponse):
        await _run("not json", graph=graph, ledger=ledger, receipts=receipts)
    first = receipts.all()[0]
    second = InMemoryReceipts()
    with pytest.raises(OutOfContractResponse):
        await _run("also not json", graph=graph, ledger=ledger, receipts=second, append=False)
    assert first.subject.startswith("batch:")
    assert first.subject == second.all()[0].subject


# ================================================================ integration


async def test_twelve_entries_and_four_names_resolve_to_two_nodes(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The integration case, against an EMPTY graph.

    Two synonym pairs -- ``{the JedAI Gateway, the LiteLLM proxy}`` and
    ``{the agent-memory MCP server, the C4 memory server}`` -- must land on
    exactly two node ids, with all twelve entries bound and both nodes waiting on
    their first dream. Its two relational claims must leave exactly ONE edge: the
    other one states that two names denote one thing, and this stage routed them
    onto one node. Nothing is mocked below the transport: sqlite ledger, real
    in-memory graph, real local embedder.
    """
    outcome, transport = await _run(
        _payload(*TWELVE_BINDINGS, relations=TWELVE_RELATIONS),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=TWELVE_ENTRIES,
    )

    assert set(outcome.bindings) == {GATEWAY, PROXY, MCP, C4}
    assert len(set(outcome.bindings.values())) == 2
    assert len(outcome.created) == 2
    assert graph.count(scope=SCOPE) == 2
    assert outcome.bindings[GATEWAY] == outcome.bindings[PROXY]
    assert outcome.bindings[MCP] == outcome.bindings[C4]

    assert ledger.unbound(scope=SCOPE) == []
    assert len(ledger.bind_calls) == 12
    for node_id in outcome.created:
        node = graph.get_node(node_id)
        assert node.facts == ()
        assert node.dirty is True

    gateway_node = outcome.bindings[GATEWAY]
    memory_node = outcome.bindings[MCP]
    assert len(ledger.for_node(gateway_node)) == 8
    assert len(ledger.for_node(memory_node)) == 5

    relations = graph.relations(scope=SCOPE)
    assert len(relations) == 1
    assert relations[0].triple == (memory_node, "REACHED_THROUGH", gateway_node)
    assert relations[0].claim == TWELVE_ENTRIES[11].claim
    assert relations[0].entry_ids == ("e-12",)
    assert outcome.relations_created == (relations[0].relation_id,)
    assert outcome.relations_reinforced == ()
    # SAME_AS never reached the graph, so it never entered the vocabulary: a word
    # the scope has no edge under is not a word the dreamer has to hold at M.
    assert outcome.edge_types_new == ("REACHED_THROUGH",)
    assert graph.edge_types(scope=SCOPE) == {"REACHED_THROUGH": 1}

    assert transport.call_count == 1
    assert len(receipts.all()) == 4
    assert {receipt.op for receipt in receipts.all()} == {ReceiptOp.RECONCILE_NEW}
    assert outcome.pressure == 0


async def test_a_second_batch_restating_an_edge_reinforces_it_across_episodes(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The amendment's reinforcement case, end to end over two ingests.

    Batch one records ``REACHED_THROUGH`` between two new nodes. Batch two is a
    different episode restating the same belief, and three things have to happen:
    the type it should reuse is in front of it in the prompt with its count, the
    answer reusing that type adds NO word to the vocabulary, and the edge ends up
    as ONE relation with evidence two -- carrying the text it already had --
    rather than two relations saying the same thing. Nothing below the transport
    is mocked.
    """
    first = (
        _entry(
            "e-01",
            "The agent-memory MCP server is reached through the JedAI Gateway.",
            kind=ClaimKind.RELATION,
            subjects=(MCP,),
            objects=(GATEWAY,),
        ),
    )
    opening, _ = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=first,
    )
    memory_node, gateway_node = opening.created
    assert opening.edge_types_new == ("REACHED_THROUGH",)
    relation_id = graph.relations(scope=SCOPE)[0].relation_id

    second = (
        _entry(
            "e-02",
            "Confirmed again today: the agent-memory MCP server reaches the JedAI Gateway fine since #245.",
            kind=ClaimKind.RELATION,
            subjects=(MCP,),
            objects=(GATEWAY,),
            identifiers=("#245",),
            episode_id="ep-251-02",
        ),
    )
    outcome, transport = await _run(
        _payload(
            _binding(MCP, node_id=memory_node),
            _binding(GATEWAY, node_id=gateway_node),
            relations=[_relation(0, MCP, GATEWAY, "REACHED_THROUGH")],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=second,
        now=LATER,
    )

    assert "EDGE TYPES IN THIS SCOPE: REACHED_THROUGH (1)" in transport.last.prompt
    assert outcome.created == ()
    assert outcome.relations_created == ()
    assert outcome.relations_reinforced == (relation_id,)
    assert outcome.edge_types_new == ()

    relations = graph.relations(scope=SCOPE)
    assert len(relations) == 1
    assert relations[0].relation_id == relation_id
    assert relations[0].evidence == 2
    assert relations[0].entry_ids == ("e-01", "e-02")
    assert relations[0].first_seen == NOW
    assert relations[0].last_seen == LATER
    # The edge keeps the text it had. A reinforcement is a statement about the
    # EVIDENCE for a belief and not about its wording, so this stage passes
    # claim=None and the store keeps what is there -- which is what stops a
    # restatement erasing the compressed claim and the marker the dreamer wrote.
    assert relations[0].claim == first[0].claim


async def test_a_new_type_is_coined_only_when_no_existing_type_fits(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Reuse against a held type adds nothing; a genuinely new belief adds a word.

    One batch, two relational claims, one answer of each kind -- so the
    distinction is made inside a single call rather than inferred from two runs.
    ``DEPLOYS`` is already in the scope and is reused; ``FRONTS`` says something
    the scope has no word for and enters the vocabulary.
    """
    held_source = _seed_node(
        graph, ledger, name="chart-old", node_type="artifact", gloss_claim="An older chart, kept as a fixture."
    )
    held_target = _seed_node(
        graph, ledger, name="sidecar", node_type="service", gloss_claim="A sidecar, kept as a fixture."
    )
    graph.upsert_relation(
        scope=SCOPE,
        source_id=held_source,
        target_id=held_target,
        type="DEPLOYS",
        claim="the older chart deploys the sidecar",
        entry_ids=("e-fixture",),
        until=None,
        now=NOW,
    )
    graph.relation_calls.clear()
    entries = (
        _entry(
            "e-01",
            "The chart deploys the agent-memory MCP server from the image tag #245 fixed.",
            kind=ClaimKind.RELATION,
            subjects=("the chart",),
            objects=(MCP,),
            identifiers=("#245",),
        ),
        _entry(
            "e-02",
            "The JedAI Gateway fronts the JedAI models for the agent-memory MCP server.",
            kind=ClaimKind.RELATION,
            subjects=(GATEWAY,),
            objects=(MCP,),
        ),
    )
    outcome, transport = await _run(
        _payload(
            _binding("the chart", new_name="chart", new_type="artifact"),
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, "the chart", MCP, "DEPLOYS"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert "EDGE TYPES IN THIS SCOPE: DEPLOYS (1)" in transport.last.prompt
    assert outcome.edge_types_new == ("FRONTS",)
    assert graph.edge_types(scope=SCOPE) == {"DEPLOYS": 2, "FRONTS": 1}
    assert len(outcome.relations_created) == 2
    assert outcome.relations_reinforced == ()


async def test_the_relational_prompt_still_names_neither_the_motive_nor_its_goal(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Motive-neutral as before, on the half of the prompt the amendment added.

    The matcher gained a vocabulary, a claim list and an answer field, and none
    of them may be a way for a persona to reach stage 2: one graph per scope, and
    a motive is a policy over it rather than a graph of its own.
    """
    motive = engineering_motive()
    _, transport = await _run(
        _payload(
            _binding(MCP, new_name="agent-memory"),
            _binding(GATEWAY, new_name="gateway"),
            relations=[
                _relation(0, MCP, GATEWAY, "REACHED_THROUGH"),
                _relation(1, GATEWAY, MCP, "FRONTS"),
            ],
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=RELATIONAL_ENTRIES,
        motive=motive,
    )
    whole = transport.last.system_prompt + transport.last.prompt
    assert "RELATIONAL CLAIMS TO TYPE (2)" in whole
    assert motive.name not in whole
    assert motive.goal not in whole
    for bullet in (*motive.extract_rubric, *motive.dream_rubric, *motive.extract_exclusions):
        assert bullet not in whole


# ============================================ what the outcome reports back


async def test_the_outcome_carries_every_adjudication_with_its_reason(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A receipt is content-free by construction, so the reason lives here or nowhere.

    ``detail`` on a ``RECONCILE_NEW`` receipt is bounded and digest-shaped: it
    records the decision and how many candidates were offered, never the model's
    own justification or a proposed gloss. A reviewer asking "why did these two
    names converge" has to be able to read the answer.
    """
    entries = (
        _entry("e-01", "The JedAI Gateway is the LiteLLM proxy fronting the models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy refused the Host header until #245.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(
            _binding(GATEWAY, new_name="gateway", new_gloss="the LiteLLM proxy", reason="the proxy itself"),
            _binding(PROXY, new_name="gateway", new_gloss="the LiteLLM proxy", reason="a second surface name"),
        ),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert [item.local_name for item in outcome.adjudications] == [GATEWAY, PROXY]
    assert [item.reason for item in outcome.adjudications] == ["the proxy itself", "a second surface name"]
    proposed = [item.new_node for item in outcome.adjudications]
    assert all(spec is not None and spec.gloss == "the LiteLLM proxy" for spec in proposed)


async def test_the_outcome_reports_the_candidates_offered_per_surface_name(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """ "Offered nothing" and "offered, and the model declined" are different findings.

    The first says the scope was empty and the run proves nothing about binding;
    the second says the prompt was given a real choice. Collapsing them would
    make a broken reconcile prompt look like a fresh scope.
    """
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, node_id=three_nodes[0]), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert set(outcome.offered) == {GATEWAY, MCP}
    assert three_nodes[0] in outcome.offered[GATEWAY]
    assert all(node_id in set(three_nodes) for offered in outcome.offered.values() for node_id in offered)


async def test_an_empty_scope_offers_no_candidates_for_any_name(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Which is why a first ingest is all ``new`` and says nothing about binding."""
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert outcome.offered == {GATEWAY: (), MCP: ()}


# ===================================================================== aliases


async def test_a_bind_records_the_routed_surface_name_as_an_alias(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """The whole point: the name a searcher types becomes an exact-match key.

    This stage is the only one that ever sees a surface name beside the node it
    was decided to denote, so an alias is recorded here or nowhere.
    """
    gateway, _, c4 = three_nodes
    await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, node_id=c4)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    assert graph.get_node(gateway).aliases == (GATEWAY,)
    assert graph.get_node(c4).aliases == (MCP,)
    found = graph.node_by_alias(scope=SCOPE, name=GATEWAY)
    assert found is not None
    assert found.node_id == gateway


async def test_binding_a_name_the_node_already_carries_adds_no_alias(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """The node's ``name`` is already a search key, so repeating it buys nothing.

    Not filtered here: ``models.normalize_aliases`` owns that rule and both seams
    apply it, which is what makes ``add_aliases`` idempotent by construction.
    """
    gateway, _, _ = three_nodes
    entries = (_entry("e-01", "The gateway is the LiteLLM proxy fronting the models.", subjects=("gateway",)),)
    await _run(
        _payload(_binding("gateway", node_id=gateway)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert graph.get_node(gateway).aliases == ()


async def test_a_second_spelling_of_a_known_alias_is_not_recorded_twice(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """One search key, and the first spelling seen is the one shown back."""
    gateway = _seed_node(
        graph,
        ledger,
        name="gateway",
        node_type="service",
        gloss_claim="The gateway is the LiteLLM proxy fronting the JedAI models.",
        aliases=(GATEWAY,),
    )
    entries = (_entry("e-01", "the jedai gateway fronts every model call.", subjects=(GATEWAY.lower(),)),)
    await _run(
        _payload(_binding(GATEWAY.lower(), node_id=gateway)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert graph.get_node(gateway).aliases == (GATEWAY,)


async def test_a_new_group_records_every_other_name_in_the_group_as_an_alias(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Two names converged on one node, and both are how it can be found again."""
    entries = (
        _entry("e-01", "The JedAI Gateway is the LiteLLM proxy fronting the models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy refused the Host header until #245.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(PROXY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    node = graph.get_node(outcome.created[0])
    assert node.name == "gateway"
    assert node.aliases == (GATEWAY, PROXY)
    for name in (GATEWAY, PROXY, "gateway"):
        found = graph.node_by_alias(scope=SCOPE, name=name)
        assert found is not None and found.node_id == node.node_id


async def test_a_new_nodes_aliases_never_repeat_its_own_name(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A group member differing from the chosen name only by case is not an alias.

    Which is why the whole group is handed to ``create_node`` unfiltered: an
    exact-string filter here would have kept ``"Gateway"`` beside the name
    ``"gateway"``, and the record would then reject the node it was building.
    """
    entries = (
        _entry("e-01", "Gateway is the LiteLLM proxy fronting the JedAI models.", subjects=("Gateway",)),
        _entry("e-02", "The LiteLLM proxy refused the Host header until #245.", subjects=(PROXY,)),
    )
    outcome, _ = await _run(
        _payload(_binding("Gateway", new_name="gateway"), _binding(PROXY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=entries,
    )
    assert graph.get_node(outcome.created[0]).aliases == (PROXY,)


async def test_recording_an_alias_leaves_the_re_embedding_to_the_dreamer(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A node's embedding text is ``name + aliases + type + lines``, and the
    dreamer is its only builder.

    So an alias write must not stamp ``last_touched_at`` -- the header's "as of"
    date reports when content was written -- and must leave the node dirty,
    which is what gets the vector rebuilt with the new alias in it.
    """
    gateway = _seed_node(
        graph,
        ledger,
        name="gateway",
        node_type="service",
        gloss_claim="The gateway is the LiteLLM proxy fronting the JedAI models.",
    )
    before = graph.get_node(gateway)
    await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        now=LATER,
    )
    after = graph.get_node(gateway)
    assert after.aliases == (GATEWAY,)
    assert after.last_touched_at == NOW
    assert after.embedding == before.embedding
    assert after.dirty is True


async def test_a_rejected_adjudication_records_no_alias(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """All-or-nothing covers aliases too: a partial route names nothing."""
    gateway, _, _ = three_nodes
    with pytest.raises(OutOfContractResponse):
        await _run(
            _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, node_id="n-999")),
            graph=graph,
            ledger=ledger,
            receipts=receipts,
        )
    assert all(node.aliases == () for node in graph.list_nodes(scope=SCOPE))
    assert graph.node_by_alias(scope=SCOPE, name=GATEWAY) is None


# ================================================ the candidate table's columns


async def test_the_candidate_table_shows_a_nodes_aliases(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """A name the router has already routed is recognised on sight.

    Without the column it would have to be re-derived from a gloss every batch,
    which is what sent one live run's ``C4`` to a node of its own.
    """
    gloss = "The gateway is the LiteLLM proxy fronting the JedAI models."
    gateway = _seed_node(
        graph,
        ledger,
        name="gateway",
        node_type="service",
        gloss_claim=gloss,
        aliases=(GATEWAY, PROXY),
    )
    _, transport = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    assert "CANDIDATE NODES (id | name | aliases | type | gloss)" in prompt
    assert f"  {gateway} | gateway | {GATEWAY}, {PROXY} | service | {gloss}" in prompt


async def test_a_candidate_with_no_aliases_renders_a_placeholder(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts, three_nodes: tuple[str, str, str]
) -> None:
    """An empty cell between two pipes reads as a rendering bug, not as "none"."""
    gateway, _, _ = three_nodes
    _, transport = await _run(
        _payload(_binding(GATEWAY, node_id=gateway), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    prompt = transport.last.prompt
    assert f"  {gateway} | gateway | {NO_ALIASES} | service | " in prompt
    assert "aliases are other names ALREADY routed to that node" in prompt


async def test_the_candidate_table_still_never_shows_a_nodes_facts(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """Aliases are search keys. Lines are compressed, motive-shaped content.

    Adding a column must not have widened what the truth layer reads.
    """
    _seed_node(
        graph,
        ledger,
        name="gateway",
        node_type="service",
        gloss_claim="The gateway is the LiteLLM proxy fronting the JedAI models.",
        aliases=(GATEWAY,),
    )
    _, transport = await _run(
        _payload(_binding(GATEWAY, new_name="gateway-2"), _binding(MCP, new_name="agent-memory")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
    )
    for fact in FIXTURE_FACTS:
        assert fact.text not in transport.last.prompt


# ================================================= integration: a second ingest


async def test_a_second_batch_naming_a_known_alias_binds_and_creates_nothing(
    graph: _SpyGraph, ledger: _SpyLedger, receipts: InMemoryReceipts
) -> None:
    """The amendment's reader-seat case, end to end over two batches.

    Batch one converges two surface names onto one node, so the second name is
    recorded as an alias. Batch two arrives naming ONLY that alias -- never the
    node's own name -- and the alias has to do three things: put the node in
    front of the router as a candidate, appear in that candidate's alias column
    so the router recognises the name it is being asked about, and resolve
    through ``node_by_alias`` as an exact-match search key. Nothing below the
    transport is mocked: real sqlite ledger, real in-memory graph, real local
    embedder.
    """
    first = (
        _entry("e-01", "The JedAI Gateway is the LiteLLM proxy fronting the models.", subjects=(GATEWAY,)),
        _entry("e-02", "The LiteLLM proxy refused the Host header until #245.", subjects=(PROXY,)),
    )
    created, _ = await _run(
        _payload(_binding(GATEWAY, new_name="gateway"), _binding(PROXY, new_name="gateway")),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=first,
    )
    node_id = created.created[0]
    assert graph.get_node(node_id).aliases == (GATEWAY, PROXY)

    second = (
        _entry(
            "e-03",
            "The LiteLLM proxy lost session affinity twice during the #246 probe.",
            subjects=(PROXY,),
            identifiers=("#246",),
            episode_id="ep-251-02",
        ),
    )
    outcome, transport = await _run(
        _payload(_binding(PROXY, node_id=node_id)),
        graph=graph,
        ledger=ledger,
        receipts=receipts,
        entries=second,
        now=LATER,
    )

    assert outcome.offered[PROXY] == (node_id,)
    assert f"  {node_id} | gateway | {GATEWAY}, {PROXY} | service | " in transport.last.prompt

    assert outcome.created == ()
    assert outcome.bound == (node_id,)
    assert graph.count(scope=SCOPE) == 1
    assert outcome.bindings == {PROXY: node_id}

    found = graph.node_by_alias(scope=SCOPE, name=PROXY)
    assert found is not None and found.node_id == node_id
    assert graph.get_node(node_id).aliases == (GATEWAY, PROXY)
    assert ledger.get("e-03").node_ids == (node_id,)
    assert ledger.unbound(scope=SCOPE) == []
    assert [receipt.op for receipt in receipts.all()][-1] is ReceiptOp.RECONCILE_BOUND
