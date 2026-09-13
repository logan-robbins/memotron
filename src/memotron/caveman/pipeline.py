"""The composition root: :class:`CavemanMemory` wiring the four stages and the three reads.

The one module in the package that imports every other, which is why it lands
last: nothing depends on it, so it can be written once the stages it threads
are settled.

.. rubric:: What this owns, and what it deliberately does not

It owns **wiring and nothing else**. Every policy decision already has an
owner — the rendering is :mod:`~memotron.caveman.render`'s, the budgets are
:class:`~memotron.caveman.motive.CavemanMotive`'s, which nodes die under
pressure is :mod:`~memotron.caveman.pressure`'s, and every line written to a
node is :mod:`~memotron.caveman.dream`'s. If this module ever grows a
decision of its own, that decision has been taken away from the module that
should have made it. So there is no retry here, no repair pass, no conditional
stage, and no branch on a motive field: the stages read the motive, this only
hands it to them.

.. rubric:: Why the seams are keyword-only and required

Six seams, all required, none defaulted. A default would let a caller
accidentally run against an in-memory store believing it had a durable one, and
a missing seam would then surface as an ``AttributeError`` deep inside a stage,
several calls away from the mistake. Keyword-only and required makes it a
``TypeError`` at the construction site naming the argument — the same
arrangement :class:`~memotron.caveman.dream.CavemanDreamer` uses for the same
reason.

.. rubric:: Why ``ingest`` reconciles ``ledger.unbound`` rather than its own entries

The data flow has stage 2 fed by *unbound entries* rather than by extract's
return value, and the difference is not cosmetic. ``unbound(scope=…)`` is
exactly "every claim not yet routed onto a node", so a reconcile that was
rejected out of contract leaves its entries in the queue and the next ingest
picks them up — no stranded claims and no retry path here to be the second
implementation of one. Passing only this episode's entries would silently orphan
them instead, and orphaned claims are invisible: they are in the ledger, so
nothing looks lost, but no node is ever supported by them.

That also means the batch reconcile adjudicates can be *wider* than the episode
just ingested, so :class:`IngestOutcome` reports both.

.. rubric:: Why the journal is composed HERE and not by the caller

``__init__`` wraps the ``GraphStore`` it is given in
:class:`~memotron.caveman.graph.JournaledGraph`, so "every mutation of this
memory is a ``DreamEvent`` in the ledger" is a property of the memory rather
than of whoever built it. A caller that had to remember the wrapper is a caller
that can forget it, and a memory whose compression is only sometimes provable
cannot answer :meth:`replay` at all.

It also means the wrapping happens exactly once. Wrapping a wrapped store would
journal every mutation twice, and the second copy would replay as a second
mutation -- so the ``graph`` argument is the raw store and
:attr:`CavemanMemory.graph` is the journalled one every stage writes through.

.. rubric:: The one concrete Clock

:class:`UtcClock` is here rather than in ``seams`` because ``seams`` declares
shapes and this is an instance. Every store operation in the package takes its
``now`` as an argument, so the composition root is the only place that needs to
read a clock at all — which is what makes a fixed-clock test a constructor
argument rather than a patched module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from memotron.caveman.dream import CavemanDreamer, ErasureOutcome, GlobalOutcome
from memotron.caveman.explain import explain as explain_node  # aliased: the method below is also ``explain``
from memotron.caveman.extract import extract
from memotron.caveman.graph import JournaledGraph
from memotron.caveman.models import Episode, LedgerEntry, Node, Relation
from memotron.caveman.motive import CavemanMotive
from memotron.caveman.read import ReadResult
from memotron.caveman.read import brief as brief_scope  # aliased: the method below is also ``brief``
from memotron.caveman.read import read as read_scope  # aliased: the method below is also ``read``
from memotron.caveman.reconcile import ReconcileOutcome, reconcile
from memotron.caveman.render import footer, render_node, render_relation
from memotron.caveman.replay import ReplayProof
from memotron.caveman.replay import prove as prove_scope  # aliased: the method below is also ``replay``
from memotron.caveman.seams import Clock, GraphStore, LedgerStore, ReceiptSink
from memotron.embedding import EmbeddingTransport
from memotron.synthesis import SynthesisTransport


class UtcClock:
    """The one canonical clock: timezone-aware UTC.

    ``seams.Clock`` requires tz-awareness because a naive datetime compared
    against an aware one raises, and every ``since=`` query in the ledger is such
    a comparison. ``datetime.now(UTC)`` is also what the repo's ``DTZ`` lint rule
    demands, so there is one place to satisfy it.
    """

    def now(self) -> datetime:
        """The current instant, tz-aware, in UTC."""
        return datetime.now(UTC)


NO_RELATIONS = "no relations"
"""What :meth:`CavemanMemory.neighbors` answers for a node nothing connects to.

A sentence rather than an empty string: "this concept stands alone" is an answer
about the graph, and an empty tool result reads to an agent as a broken call.
"""


def _other_end(relation: Relation, *, node_id: str) -> str:
    """The node id at the far end of *relation*, seen from *node_id*."""
    return relation.target_id if relation.source_id == node_id else relation.source_id


@dataclass(frozen=True)
class IngestOutcome:
    """What one episode did to the ledger and the graph.

    *entries* is what THIS episode contributed; *routed* is the whole unbound
    batch stage 2 adjudicated, which may be wider when a previous reconcile was
    rejected. Reporting both is what makes "12 claims in, 14 routed" readable
    rather than looking like a miscount.
    """

    episode_id: str
    scope: str
    entries: tuple[LedgerEntry, ...]
    routed: tuple[LedgerEntry, ...]
    reconciled: ReconcileOutcome

    @property
    def entry_count(self) -> int:
        """Claims this episode appended."""
        return len(self.entries)

    @property
    def node_ids(self) -> tuple[str, ...]:
        """Every node this ingest created or bound a claim onto, created first.

        The handles an agent traverses from: ``node``, ``neighbors`` and
        ``explain`` all take one of these. Created before bound because that is
        the order a reader cares about -- what is new in this scope, then what was
        already here and got confirmed.

        Deduped, because one node can be both: two surface names in one batch
        can converge onto a node the batch itself created.
        """
        seen: dict[str, None] = dict.fromkeys(self.reconciled.created)
        seen.update(dict.fromkeys(self.reconciled.bound))
        return tuple(seen)

    @property
    def relations_created(self) -> tuple[str, ...]:
        """Relation ids for the typed edges this ingest wrote for the first time."""
        return self.reconciled.relations_created

    @property
    def relations_reinforced(self) -> tuple[str, ...]:
        """Relation ids for the edges this ingest restated rather than duplicated.

        The reinforcement count, and the number worth watching: a scope whose
        every ingest only creates has a matcher that is not matching. Not
        disjoint from :attr:`relations_created` -- see
        :class:`~memotron.caveman.reconcile.ReconcileOutcome`.
        """
        return self.reconciled.relations_reinforced

    @property
    def edge_types_new(self) -> tuple[str, ...]:
        """Types this ingest put in the scope's vocabulary for the first time.

        What the dreamer's ``M`` ceiling is measured against.
        """
        return self.reconciled.edge_types_new


@dataclass(frozen=True)
class DreamOutcome:
    """What one dream did. Both halves, so a caller need not guess which ran.

    ``glob`` is ``None`` exactly when ``global_pass=False`` was asked for —
    distinguishable from a global pass that ran and found nothing to do, which
    returns a :class:`~memotron.caveman.dream.GlobalOutcome` with
    ``ops_applied=0``. A caller checking "did the scope get reorganised" must be
    able to tell those apart.
    """

    scope: str
    dreamed: tuple[Node, ...]
    glob: GlobalOutcome | None

    @property
    def incremental_count(self) -> int:
        """Nodes re-lined by the incremental pass. One LLM call each."""
        return len(self.dreamed)


class CavemanMemory:
    """Caveman memory, composed from six seams and owning no state of its own.

    Nine operations, and the LLM call budget of each is a property of the design
    rather than an implementation detail:

    * :meth:`ingest` — one extract call plus one reconcile call, whatever the
      episode's size.
    * :meth:`dream` — one call per dirty node, plus one for the global pass.
    * :meth:`read` — **zero**. The agency sits around the call, never inside the
      rerank.
    * :meth:`brief` — zero, and no embedding either: it reads the whole scope
      rather than a neighbourhood of it.
    * :meth:`node`, :meth:`neighbors`, :meth:`explain` — zero. Store reads and a
      string join.
    * :meth:`replay` — zero, and no embedding either: it rebuilds the scope from
      the journal and compares digests.
    * :meth:`erase` — zero. Erasure is a ledger delete plus a re-dream.

    .. rubric:: The search surface: two entry points and three traversals

    A searcher arrives in one of two states -- holding a query (:meth:`read`) or
    holding nothing at all (:meth:`brief`, the session-start and post-compaction
    read). Every block either returns carries its node id in brackets, and the
    three id-taking calls are what that id is FOR: :meth:`node` re-reads one
    concept in full, :meth:`neighbors` lists its typed edges with the id on the
    other end of each, and :meth:`explain` gives the provenance a bounded read
    structurally cannot. That is the traversal loop -- read, walk, read -- and it
    costs no model call at any step.
    """

    def __init__(
        self,
        *,
        graph: GraphStore,
        ledger: LedgerStore,
        receipts: ReceiptSink,
        chat: SynthesisTransport,
        embedder: EmbeddingTransport,
        clock: Clock,
    ) -> None:
        journalled: GraphStore = JournaledGraph(graph, ledger=ledger, receipts=receipts, clock=clock)
        self._graph = journalled
        self._ledger = ledger
        self._receipts = receipts
        self._chat = chat
        self._embedder = embedder
        self._clock = clock
        self._dreamer = CavemanDreamer(
            graph=journalled,
            ledger=ledger,
            receipts=receipts,
            transport=chat,
            embedder=embedder,
            clock=clock,
        )

    @property
    def graph(self) -> GraphStore:
        """The JOURNALLED store every stage of this memory writes through.

        Exposed for a caller that seeds a scope or reads it back -- a demo
        rendering the final graph, a test asserting on what a dream wrote. It is
        the wrapper and not the store handed to ``__init__``, deliberately: a
        write through this is journalled like any other, so a seeded scope still
        proves under :meth:`replay`, while a write through the caller's own
        reference to the raw store does not and is exactly how a proof stops
        being one.
        """
        return self._graph

    # -- 1 + 2: ingest -----------------------------------------------------

    async def ingest(self, episode: Episode, motive: CavemanMotive) -> IngestOutcome:
        """Extract *episode*'s claims, then route them onto nodes. Two LLM calls.

        Stage 1 appends to the ledger and never sees the graph; stage 2 routes
        and marks dirty and never writes fact text. No node's facts change here —
        :meth:`dream` is the only thing that writes them, so a freshly ingested
        scope holds dirty, lineless nodes until it is dreamt.

        Raises:
            OutOfContractResponse: from either stage. Extract's rejection appends
                nothing; reconcile's writes nothing. Neither leaves half a
                response behind, so a rejected ingest is safe to re-run once the
                prompt is fixed.
        """
        now = self._clock.now()
        entries = await extract(
            episode=episode,
            motive=motive,
            transport=self._chat,
            ledger=self._ledger,
            receipts=self._receipts,
            now=now,
        )
        routed = tuple(self._ledger.unbound(scope=episode.scope))
        reconciled = await reconcile(
            entries=routed,
            scope=episode.scope,
            motive=motive,
            graph=self._graph,
            ledger=self._ledger,
            receipts=self._receipts,
            embedder=self._embedder,
            transport=self._chat,
            now=now,
        )
        return IngestOutcome(
            episode_id=episode.episode_id,
            scope=episode.scope,
            entries=entries,
            routed=routed,
            reconciled=reconciled,
        )

    # -- 3: dream ----------------------------------------------------------

    async def dream(self, *, scope: str, motive: CavemanMotive, global_pass: bool = True) -> DreamOutcome:
        """Re-line every dirty node, then optionally reorganise the whole scope.

        Incremental first, deliberately: the global pass reasons over the
        inventory, and a node still holding no facts has nothing for it to reason
        about. Running them the other way round would ask the model to merge
        concepts it cannot read.

        ``global_pass=False`` runs 3a alone — which is what an ingest-time dream
        wants, since the periodic reorganisation is not per-episode work.

        Raises:
            OutOfContractResponse: from the first node whose response is out of
                contract, or from the global pass. A rejected node stays dirty
                and unchanged; a rejected global pass applies zero ops.
        """
        dreamed = tuple(await self._dreamer.dream_incremental(scope=scope, motive=motive))
        glob = await self._dreamer.dream_global(scope=scope, motive=motive) if global_pass else None
        return DreamOutcome(scope=scope, dreamed=dreamed, glob=glob)

    # -- read: the three states a searcher arrives in -----------------------

    def read(self, *, query: str, scope: str, motive: CavemanMotive) -> ReadResult:
        """Answer *query* from *scope*. Deterministic, and **no LLM call**.

        Synchronous for the same reason: there is nothing to await. A caller that
        has to ``await`` a read would reasonably assume a model was consulted.

        ``ReadResult.saturated`` is not optional to check — a caller that ignores
        it can silently miss a hard constraint the budget dropped.
        """
        return read_scope(
            query=query,
            scope=scope,
            motive=motive,
            graph=self._graph,
            ledger=self._ledger,
            embedder=self._embedder,
            receipts=self._receipts,
            now=self._clock.now(),
        )

    def brief(self, *, scope: str, motive: CavemanMotive) -> ReadResult:
        """What *scope* is about when there is no query yet. **No LLM call, and no embedding.**

        The read a session opens with, and the read it has right after a
        compaction — the moment the context that knew what to ask for is gone.
        Every ``!`` line in the scope first, then its highest-value nodes until
        the budget runs out.

        Deliberately not ``read(query=scope)`` or a read with some default query:
        there is nothing to embed, so this pays for no vector at all and no
        ``read_k`` bounds it. :attr:`ReadResult.query` comes back ``""``, which is
        how a caller tells a brief from a read after the fact — and
        :func:`read` refuses a blank query, so the empty string is unambiguous.

        ``ReadResult.saturated`` matters more here than anywhere: a brief is the
        read most likely to be cut, because it ranks the whole scope.
        """
        return brief_scope(
            scope=scope,
            motive=motive,
            graph=self._graph,
            ledger=self._ledger,
            receipts=self._receipts,
            now=self._clock.now(),
        )

    def node(self, *, node_id: str) -> str:
        """Re-read ONE concept as the block a read would have emitted. **No LLM call.**

        The first of the three calls a ``[n-001]`` id is for. Rendered through
        :func:`~memotron.caveman.render.render_node`, which is the one place
        this subsystem shapes text, so the block is byte-identical to the same
        node inside a read -- including its header, whose entry count comes from
        ``LedgerStore.entry_counts`` exactly as ``read`` takes it. Summing the
        node's own facts' evidence instead would double-count an entry that
        supports two facts, and this would then contradict :meth:`read` about one
        node.

        **A pure projection: no ``record_read``.** Read-count policy belongs to
        :mod:`~memotron.caveman.read`, and counting a traversal here would move
        a node up the pressure ranking for having been walked past.

        Raises:
            NodeNotFound: if *node_id* is not in the graph. An id an agent was
                handed and cannot resolve is a real answer, not an empty block.
        """
        node = self._graph.get_node(node_id)
        relations = self._graph.relations_of(node_id)
        entries = self._ledger.entry_counts(scope=node.scope).get(node_id, 0)
        block = render_node(node, relations, entries=entries, names=self._scope_names(node.scope))
        return "\n".join((block, footer([node_id])))

    def neighbors(self, *, node_id: str) -> str:
        """One line per typed edge on this node, with the id on the other end. **No LLM call.**

        The traversal step. Both directions, in the two renderings
        :func:`~memotron.caveman.render.render_relation` gives them, so a
        reader never has to look an id up to tell which way an edge points.

        The footer names the NEIGHBOURS and not the subject, because that is the
        walk: an agent that asked what this concept connects to is about to read
        one of them.

        Raises:
            NodeNotFound: if *node_id* is not in the graph.
        """
        node = self._graph.get_node(node_id)
        relations = self._graph.relations_of(node_id)
        names = self._scope_names(node.scope)
        header = f"{node.name} ({node.type}) [{node_id}], {len(relations)} relations"
        if not relations:
            return f"{header}\n{NO_RELATIONS}"
        lines = [render_relation(relation, node_id=node_id, names=names) for relation in relations]
        others = sorted({_other_end(relation, node_id=node_id) for relation in relations})
        return "\n".join((header, *lines, footer(others)))

    def replay(self, *, scope: str) -> ReplayProof:
        """Rebuild *scope* from its journal and compare it to the live graph. **No LLM, no embedding.**

        The proof the amendment asks for: compression maintains a history that
        can be replayed and recalled. Cheap enough to run after every dream pass,
        because it is a fold over the recorded events and two digests.

        A mismatch comes back as ``equal=False`` with both digests rather than
        raising. "Did the journal account for the graph" is a question, and "no"
        is an answer the caller needs the two hashes to diagnose.

        Raises:
            ValueError: on a journal with a gap in it, or a graph holding an edge
                whose endpoint it does not -- see
                :mod:`~memotron.caveman.replay`.
        """
        return prove_scope(scope=scope, graph=self._graph, ledger=self._ledger)

    def _scope_names(self, scope: str) -> dict[str, str]:
        """Node id to name, for the far end of a rendered edge.

        Whole-scope rather than per-block because a scope is bounded at ``N``
        nodes: one listing is cheaper than resolving ids one at a time, and an
        edge pointing outside the blocks being rendered still gets a name.
        """
        return {node.node_id: node.name for node in self._graph.list_nodes(scope=scope)}

    def explain(self, *, node_id: str) -> str:
        """Everything the ledger holds about one node. **No LLM call, unbounded.**

        The call a read's ``deeper: explain(<node_id>)`` footer names. A read is
        bounded to ``L`` facts a node, so "what is the evidence for this" is a
        question it structurally cannot answer; this answers it with every
        entry, newest first, superseded ones included.

        No *motive* argument, and that is the point: a motive is a policy over
        what to KEEP under a budget, and there is no budget here. Two motives
        would get the same bytes back, so asking for one would imply a choice
        this does not make.

        Raises:
            NodeNotFound: if *node_id* is not in the graph.
            ValueError: if an entry is dated after the clock — two writers, and a
                reader judging staleness from these dates would be misled.
        """
        return explain_node(
            node_id=node_id,
            graph=self._graph,
            ledger=self._ledger,
            now=self._clock.now(),
        )

    # -- erasure -----------------------------------------------------------

    def erase(self, *, episode_id: str, scope: str) -> ErasureOutcome:
        """Erase an episode's claims and leave the graph regenerable. No LLM call.

        Deletes the entries, deletes any node that lost its every support, and
        marks the rest dirty. The re-dream is a separate :meth:`dream` call
        rather than something this triggers: erasure must succeed even when the
        gateway is unreachable, and folding an LLM call into it would make a
        deletion depend on a model being up.
        """
        return self._dreamer.erase_episode(episode_id=episode_id, scope=scope)
