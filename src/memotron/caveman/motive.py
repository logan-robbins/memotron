"""``CavemanMotive``: what this subsystem is *for*, as a policy over one graph.

A contract, not a stage. Stages 1 and 3 render it into their prompts, ``rank``
reads its weights, ``pressure`` reads its value weights, and ``read`` reads its
budgets — so putting it in any one of them would make the others depend on a
stage.

**Motive is a policy over one graph, never a per-motive graph.** Stage 2
(reconcile) never sees it: the node graph is the persona-independent truth
layer, and what a persona changes is which claims are worth extracting, which
facts are worth a slot, and how much room the whole thing gets.

Why a new model rather than adapting ``memotron.config.Motive``
(``config/_motive.py:44``): that one is typed around ``allowed_memory_types:
tuple[MemoryType, ...]`` and pulls in ``SalienceRubric``, ``GovernancePolicy``,
``RetentionPolicy``, ``ActionabilityPolicy``, ``MemoryHealthPolicy`` and
``DreamPromptOverride`` — six policies this subsystem has no concept for, and a
``MemoryType`` taxonomy it deliberately replaces with open-text node types. It
is also threaded through ``client``, ``retrieval``, ``interop``,
``agent_memory``, ``admin_server`` and ``config/_tenancy``, so widening it for
caveman would touch all of them. A projection from one to the other is a later
adapter in one file.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memotron.caveman.models import FactKind
from memotron.caveman.render import BREVITY_RULE, FACT_GLOSS

#: Fact kinds that carry a motive-set rank weight. ``rule`` is deliberately
#: absent: a hard rule outranks everything by construction (``rank`` adds a
#: constant floor to it), so giving it a weight would imply a persona could
#: rank a rule below an attribute, which is the one thing that must not be
#: expressible.
WEIGHTED_FACT_KINDS: frozenset[FactKind] = frozenset(FactKind) - {FactKind.RULE}

_STAGES = ("extract", "dream")


class ValueWeights(BaseModel):
    """How a node's survival value is composed, for the forced-merge slate.

    Which nodes die under pressure is the most consequential decision in the
    system, so the weights are explicit policy rather than a tuned constant
    buried in ``pressure.py``. Every field defaults, so ``ValueWeights()`` is a
    usable neutral starting point.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    recency: float = Field(default=1.0, ge=0.0)
    reads: float = Field(default=1.0, ge=0.0)
    degree: float = Field(default=1.0, ge=0.0)
    type: float = Field(default=1.0, ge=0.0)

    @model_validator(mode="after")
    def _at_least_one_signal_counts(self) -> Self:
        if self.recency == self.reads == self.degree == self.type == 0.0:
            raise ValueError("value weights cannot all be zero -- every node would score identically")
        return self


class CavemanMotive(BaseModel):
    """One persona's policy over one scope's graph.

    Frozen, so :func:`motive_digest` describes something that cannot then
    change. One caveat worth stating: ``fact_kind_weight`` and ``type_weight``
    are ``dict``, and freezing the model does not freeze a dict's contents, so
    mutating one in place will silently invalidate an already-recorded digest.
    Use ``model_copy(update=...)`` — which is how the demo drops ``max_nodes``
    to 3 to make ``N`` enforcement visible.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    goal: str = Field(min_length=1)

    extract_rubric: tuple[str, ...] = Field(min_length=1)
    """"Worth knowing" bullets, rendered verbatim into the extract prompt."""

    extract_exclusions: tuple[str, ...] = ()
    """"Not worth knowing" bullets. Empty is valid: a persona may exclude nothing."""

    dream_rubric: tuple[str, ...] = Field(min_length=1)
    """Preserve-vs-compress bullets, rendered verbatim into both dream prompts."""

    max_nodes: int = Field(default=500, ge=1)
    """``N``. ``pressure = max(0, count - N)``, and it is an invariant, not a soft cap."""

    max_edge_types: int = Field(default=30, ge=1)
    """``M``: how many DISTINCT relation types one scope may hold.

    The second bound the dreamer enforces (#251 amendment D). ``N`` stops the
    concepts from growing without limit; ``M`` stops the *vocabulary of relations
    between them* from doing the same, which is the failure an open edge-type
    space has: every episode coins ``FRONTS``, ``PROXIES``, ``ROUTES_TO`` for one
    relationship and a reader can no longer ask a question by type.

    Bounded types, not bounded edges. How many edges a scope holds is how much
    evidence it has, and bounding evidence is what the ledger exists to make
    unnecessary. 30 is room for a real domain's verbs and far short of one type
    per pair.
    """

    max_facts_per_node: int = Field(default=8, ge=1)
    """``L``: how many facts one node may hold."""

    max_fact_tokens: int = Field(default=40, ge=4)
    """``T``, a PARAGRAPH GUARD over a fact's text -- never a target the prompt states.

    40 tokens is ~160 characters under this repo's ``(len + 3) // 4`` estimator:
    wide enough that a real fact never approaches it, narrow enough that a
    paragraph arriving where a fact belongs is still rejected.

    It was 15, stated to the model in characters and then in words, and that is
    the history worth keeping: a model cannot count characters, so a tight cap
    produced rejections whose cause was the prompt rather than the answer
    (#251 amendment A). Brevity is asked for by
    :data:`~memotron.caveman.render.BREVITY_RULE` and is not measured; this
    number is only the guard.
    """

    fact_kind_weight: dict[FactKind, float]
    """Rank and dream priority per fact kind. Must cover :data:`WEIGHTED_FACT_KINDS` exactly."""

    type_weight: dict[str, float] = Field(default_factory=dict)
    """Node-type preference. Open-text keys, because node types are open text."""

    read_k: int = Field(default=8, ge=1)
    """kNN seed count for a read. The CAP on the measured half of the seeding.

    Exact hits are never counted against it (``read._seed_the_read``): a node
    the query named is not competing for a slot with a node that merely
    resembles it.
    """

    knn_min_similarity: float = Field(default=0.25, ge=-1.0, le=1.0)
    """How similar a kNN neighbour must be to seed a read at all. The FLOOR.

    ``read_k`` alone is a cap and not a filter, so a query in a small scope
    fills every slot whatever the measurements say, and 1-hop expansion then
    pulls in each of those seeds' neighbours as well. That is how ``read('C4')``
    on a nine-node scope came back holding all nine (#251 amendment C).

    0.25 is measured, not chosen. Over the four query reads of the 2026-09-11
    live run against this repo's own episode (``text-embedding-3``, 3072d), the
    two bands came out either side of it and did not overlap:

    ===================================  ===============
    what the candidate was               cosine
    ===================================  ===============
    a node the query is actually about   0.2534 - 0.3902
    a node it is not                     0.1003 - 0.2435
    ===================================  ===============

    The gap is 0.2435 to 0.2534 and 0.25 is inside it, which is as tight as a
    measured boundary gets on one run. The tightest pair is from
    ``read('#246')``: ``agent-memory`` at 0.2534 (the deploy the issue is about)
    and ``gateway`` at 0.2435 (which the read still emitted, reached 1-hop from
    a seed -- so the floor bounds the SEEDING, not the graph).

    Where a retained error is preferable it is a dropped neighbour rather than a
    kept one: a neighbour wrongly dropped is usually reached across an edge
    anyway, and a neighbour wrongly kept costs the reader a whole block.

    Applied to the kNN half only. **An exact hit is never floored** — it was not
    measured, and a search key the reader typed is not a resemblance to be
    second-guessed. A floored candidate is still reported on
    ``ReadResult.seeds`` with ``kept=False``, so what kNN offered and why it was
    left out stays answerable from the result.

    Every preset takes this default. It is a property of the vector space rather
    than of a persona, and two personas reading one scope should not disagree
    about which neighbours are neighbours.
    """

    read_line_budget: int = Field(default=100, ge=1)
    """Rendered lines one read may emit. About 1.3k tokens at the measured mean length."""

    read_token_budget: int = Field(default=1500, ge=1)
    """Tokens one read's rendered facts may cost. The other half of the read cut.

    Its own field rather than ``read_line_budget * max_fact_tokens``, which is
    what it used to be: ``T`` is now a loose paragraph guard, so the derived
    product priced 100 lines at 4,000 tokens and stopped bounding anything a
    caller could plan a context window around. 1,500 is the design's own
    measured figure -- 100 lines at the ~13-token mean body -- with headroom.

    Two settable budgets can disagree, and that is the trade taken deliberately:
    the read cuts at whichever binds first and receipts
    ``READ_BUDGET_SATURATED`` when either does.
    """

    keep_superseded: bool = True
    """Whether a superseded fact is knowledge. Genuinely persona-dependent.

    Renamed with the kind it governs (#251 amendment D): the old refuted sigil
    became :attr:`~memotron.caveman.models.FactKind.SUPERSEDED`, and a policy
    field named for a kind that no longer exists is a field whose meaning has to
    be looked up.
    """

    superseded_ttl_days: int | None = Field(default=90, ge=1)
    """How long a superseded fact survives. ``None`` never expires it; ignored when not keeping them."""

    value_weights: ValueWeights
    """Composition of node survival value, consumed by ``pressure``."""

    recency_half_life_days: float = Field(default=30.0, gt=0.0)
    """Half-life of the recency term in node value."""

    @model_validator(mode="after")
    def _fact_kind_weight_covers_every_weighted_kind(self) -> Self:
        """Exactly the four weightable kinds -- no gaps, and never ``rule``.

        Full coverage is required rather than defaulted so ``rank`` can index
        the mapping directly: a missing kind would silently rank that kind at
        zero and quietly drop a whole class of fact out of every read.
        """
        provided = set(self.fact_kind_weight)
        if FactKind.RULE in provided:
            raise ValueError(
                f"fact_kind_weight must not weight {FactKind.RULE.value!r} -- a hard rule "
                "outranks every other kind by construction"
            )
        missing = WEIGHTED_FACT_KINDS - provided
        if missing:
            names = ", ".join(sorted(kind.value for kind in missing))
            raise ValueError(f"fact_kind_weight is missing a weight for: {names}")
        return self


def engineering_motive() -> CavemanMotive:
    """The engineering persona. ``N=500 M=30 L=8``, superseded facts kept 90 days.

    What the demo runs. A superseded fact is worth holding for an engineer who
    would otherwise re-derive it -- "the gateway does *not* refuse the Host
    header any more" saves a bisect -- which is exactly why the policy is
    per-motive.

    .. rubric:: Why ``T`` is 40 here

    Because it is a paragraph guard rather than a fact budget. The number moved
    twice, and both moves are one finding: the design's 15 rejected the
    document's own worked rule (80 characters, 20 estimator tokens); WP4's live
    run then raised it to 20 and STILL lost a run to a 66-character line against
    a 60-character cap, with the prompt stating the ceiling in characters three
    times over, ending "Count them". A model cannot count characters, and a
    prompt that asks it to is the defect.

    So amendment A stopped asking. The prompt states
    :data:`~memotron.caveman.render.BREVITY_RULE`, and ``T`` is set where only
    a paragraph trips it: 40 tokens, ~160 characters, about twice the widest fact
    the design document itself writes. ``assistant_motive`` sits at 30 -- a
    shorter fact for a persona whose facts are preferences, not measurements.
    """
    return CavemanMotive(
        name="engineering",
        goal=(
            "Know how this system actually behaves, so a later session does not re-derive it: "
            "the rules, the definitions, the measured facts with their identifiers, and what is "
            "still unreliable."
        ),
        extract_rubric=(
            "Hard rules and prohibitions -- anything stated as 'always', 'never', or 'the rule is'.",
            "Definitions: what a named thing IS, especially when two names denote one thing.",
            "Measured facts carrying an identifier: issue numbers, model aliases, counts, dimensions, versions.",
            "Relations between two named things, including which one deploys, fronts or depends on the other.",
            "Known-unreliable behaviour: intermittent failures, unreproduced bugs, and what names them.",
            "Corrections of anything stated earlier, including earlier in this same conversation.",
        ),
        extract_exclusions=(
            "Pleasantries, agreement, and restatements that add no new fact.",
            "Anything true only inside this conversation -- 'let me check', 'one second'.",
            "Speculation nobody committed to. If it was hedged, either it is uncertain or it is nothing.",
        ),
        dream_rubric=(
            "Never drop a hard rule. It is the fact most expensive to rediscover.",
            "Keep identifiers verbatim. A fact without its issue number costs a search to re-find.",
            "When a claim supersedes another, rewrite the fact and let the ledger keep the old text.",
            "Prefer one relation between two nodes over two facts restating both sides of it.",
            "Keep a superseded fact as a warning when someone would otherwise re-derive the wrong answer.",
            "Drop a fact whose only content is that something is normal.",
        ),
        max_nodes=500,
        max_edge_types=30,
        max_facts_per_node=8,
        max_fact_tokens=40,
        fact_kind_weight={
            FactKind.IS: 0.9,
            FactKind.ATTRIBUTE: 0.7,
            FactKind.UNSURE: 0.5,
            FactKind.SUPERSEDED: 0.3,
        },
        type_weight={"service": 1.1, "policy": 1.2, "artifact": 1.0, "cluster": 1.0},
        read_k=8,
        read_line_budget=100,
        keep_superseded=True,
        superseded_ttl_days=90,
        value_weights=ValueWeights(recency=0.8, reads=1.2, degree=1.0, type=0.6),
        recency_half_life_days=30.0,
    )


def assistant_motive() -> CavemanMotive:
    """The general-assistant persona. ``N=200 M=12 L=6 T=30``, superseded facts dropped.

    A smaller graph, a narrower relation vocabulary and no history, because
    "that used to be wrong" is worth nothing to a general assistant and worth 90
    days to an engineer. The contrast is the point: the same claims under two
    motives keep different things.
    """
    return CavemanMotive(
        name="assistant",
        goal=(
            "Know the person and their standing preferences well enough to act without asking again: "
            "who they are, what they want by default, and what they have decided."
        ),
        extract_rubric=(
            "Standing preferences: how they want things done unless told otherwise.",
            "Durable personal facts: role, tools, timezone, constraints on their time.",
            "Decisions they have made, and what they decided against.",
            "Corrections of anything previously assumed about them.",
        ),
        extract_exclusions=(
            "One-off requests. A task is not a preference.",
            "Anything about the assistant's own behaviour in this session.",
            "Third parties' preferences stated in passing.",
        ),
        dream_rubric=(
            "Keep the current preference and let the ledger hold what it replaced.",
            "Never keep a superseded preference on the node -- acting on a stale one is the failure mode.",
            "Collapse two phrasings of one preference into the clearer single fact.",
            "Prefer the fact that says what to DO over the fact that says what was observed.",
        ),
        max_nodes=200,
        max_edge_types=12,
        max_facts_per_node=6,
        max_fact_tokens=30,
        fact_kind_weight={
            FactKind.IS: 1.0,
            FactKind.ATTRIBUTE: 0.8,
            FactKind.UNSURE: 0.4,
            FactKind.SUPERSEDED: 0.1,
        },
        read_k=6,
        read_line_budget=60,
        keep_superseded=False,
        superseded_ttl_days=None,
        value_weights=ValueWeights(recency=1.2, reads=1.0, degree=0.6, type=0.4),
        recency_half_life_days=60.0,
    )


def _ranked_fact_kinds(motive: CavemanMotive) -> str:
    """The weighted kinds, most valuable first, each with its renderer gloss.

    Sorted by descending weight and then by the kind's own declaration order, so
    equal weights render in one stable order rather than in dict-insertion order
    -- a prompt that varies between runs makes a digest meaningless.
    """
    order = {kind: index for index, kind in enumerate(FactKind)}
    ranked = sorted(motive.fact_kind_weight.items(), key=lambda item: (-item[1], order[item[0]]))
    return "\n".join(f"  {kind.value:<11} {FACT_GLOSS[kind]}" for kind, _ in ranked)


def _bullets(header: str, items: tuple[str, ...]) -> str:
    """A header and its bullets, or nothing at all when there are no bullets."""
    if not items:
        return ""
    lines = "\n".join(f"  - {item}" for item in items)
    return f"{header}\n{lines}\n"


def render_motive_block(motive: CavemanMotive, *, stage: Literal["extract", "dream"]) -> str:
    """Render the motive for one stage's prompt.

    The two stages get genuinely different blocks, not one block with a
    different header: extract is told what is worth knowing, dream is told what
    is worth a slot. Rendering the dream rubric into the extract prompt would
    tell the extractor to compress, which is not its job and is how a claim
    arrives pre-summarised and unsupportable.

    Stage 3b appends its own ``NODE BUDGET`` / ``PRESSURE`` line, because those
    are facts about the scope rather than about the motive.
    """
    if stage not in _STAGES:
        raise ValueError(f"stage must be one of {_STAGES}, got {stage!r}")

    head = f"MOTIVE: {motive.name}\nGOAL: {motive.goal}\n"
    kinds = f"FACT KINDS, MOST VALUABLE FIRST:\n{_ranked_fact_kinds(motive)}\n"

    if stage == "extract":
        return (
            head
            + _bullets("WORTH KNOWING:", motive.extract_rubric)
            + _bullets("NOT WORTH KNOWING:", motive.extract_exclusions)
            + kinds
        )

    kept = FactKind.SUPERSEDED.value.upper()
    if not motive.keep_superseded:
        superseded = f"{kept} FACTS: drop them.\n"
    elif motive.superseded_ttl_days is None:
        superseded = f"{kept} FACTS: keep indefinitely.\n"
    else:
        superseded = f"{kept} FACTS: keep for {motive.superseded_ttl_days} days.\n"

    return (
        head
        + _bullets("DREAM POLICY:", motive.dream_rubric)
        + f"KEEP AT MOST {motive.max_facts_per_node} FACTS ON ONE NODE.\n"
        + f"KEEP AT MOST {motive.max_edge_types} DISTINCT RELATION TYPES IN THIS SCOPE.\n"
        + f"KEEP AT MOST {motive.max_nodes} NODES IN THIS SCOPE.\n"
        + f"{BREVITY_RULE}\n"
        + "A fact too big for one record is two facts. Nothing here asks you to count\n"
        + "characters or words: write the shortest fact that still states it.\n"
        + kinds
        + superseded
    )


def motive_digest(motive: CavemanMotive) -> str:
    """sha256 over the motive's canonical JSON. Rides every receipt.

    ``sort_keys`` and the compact separators make the digest a function of the
    motive's VALUES and not of field or dict insertion order, so the same policy
    constructed twice digests identically — which is what lets a receipt stream
    show that a run's behaviour changed because the policy changed.
    """
    canonical = json.dumps(motive.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
