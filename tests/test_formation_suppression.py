"""T1-1: accumulated memory must not suppress the formation of new memory.

The defect
----------
``DreamEngine`` asks the dream agent to approve each queued episode BEFORE
extraction, and hands it the scope's existing memories as decision context
(``dreaming/_formation.py:338``).  A real agent then judges *the state of the
world described in that context* rather than whether the episode is worth
recording, and refuses.  Measured in ``docs/findings/formation-suppression.md``
against a live gateway: **one** unresolved fact in a scope is enough, and the
scope stops forming new memory from then on.  Silent, billed, no receipt, no
negative-space entry.

Why this file exists at all
---------------------------
The fix was validated by hand in August 2026 and **not applied for months**,
because nothing here could reproduce it: ``LocalDreamAgentTransport``
(``agents.py:76``) approves unconditionally, so there was no way to ask for a
refusing agent.  That is a plugin-boundary gap rather than a testing gap — the
fake is hardcoded rather than selected — and it is the reason a one-line fix sat
unshipped behind a 5/5-reproducible finding.

The fake, and why it is not a tautology
---------------------------------------
:class:`ContextSentimentDreamAgentTransport` refuses on the CONTENT of
``context_facts``, not on their presence.  That distinction is the whole design:
a fake that refused whenever ``context_facts`` was non-empty would make the fix
(``context_facts=()``) pass trivially, and the test would prove only that an
empty tuple is empty.

The measured behaviour it models is *sentiment*, from the findings doc:

  row F   three BENIGN facts injected  -> both episodes APPROVED, and the
          verdict language inverts to "sufficient context facts"
  row E   context wiped                -> APPROVED
  row H   negative facts present, injection disabled -> APPROVED
  thresh  one negative fact            -> REFUSED
  control one benign fact              -> APPROVED

So the variable is negative context *reaching the decision prompt* — not volume,
not motive, not prompt profile, not model (row G swapped the model and got the
same refusal).  :func:`test_the_fake_discriminates_on_content_not_presence`
pins that property of the fake itself, so this file fails if the fake ever
degenerates into "refuse when non-empty".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memotron import (
    Memotron,
    EpisodeType,
    MemoryScope,
    ScopeKind,
)
from memotron.agents import DreamAgentDecision, DreamAgentDecisionRequest
from memotron.config import DreamAgentConfig, DreamConfig, DreamJob, DreamJobKind, default_config

SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id="formation-suppression")

#: Times are derived from the SEEDED DATA, never from a fixed constant. The first version of
#: this file used a hardcoded ``RUN_TIME`` of 2026-08-31T12:00Z while ``add_memory`` stamps
#: ``valid_from`` with the real wall clock. The default context policy has
#: ``as_of_episode_time=True``, so the seeded fact was NOT YET VALID at the episode's
#: reference time, the formation gate received an empty ``context_facts``, and the defect
#: could not reproduce -- **all five tests passed against unfixed code.** Only
#: ``test_the_fix_is_scoped_to_the_formation_gate`` revealed it, by asserting the gate saw
#: `()` and passing when it should not yet have. A green test that proves nothing is the
#: exact failure this repo keeps finding elsewhere; deriving the clock from the data removes
#: the whole class.

#: Substrings a real dream agent treated as grounds to refuse. Taken from the verbatim
#: refusal summaries in docs/findings/formation-suppression.md ("lacks required durability
#: guarantees", "conflicting context facts"), not invented for this test.
UNRESOLVED_MARKERS = ("blocked", "unresolved", "lacks", "conflicting", "cannot", "missing")

FORMATION_GATE = "formation_episode_selected"


class ContextSentimentDreamAgentTransport:
    """Refuses formation when injected context describes unresolved problems.

    Models what a real agent did, at the one decision point where it was observed
    doing it. Every other decision type is approved, so this fake cannot mask an
    unrelated regression by refusing everything.
    """

    def __init__(self) -> None:
        self.requests: list[DreamAgentDecisionRequest] = []

    @staticmethod
    def _negative(facts: tuple[str, ...]) -> list[str]:
        return [fact for fact in facts if any(marker in fact.lower() for marker in UNRESOLVED_MARKERS)]

    async def decide(self, request: DreamAgentDecisionRequest) -> DreamAgentDecision:
        self.requests.append(request)
        if request.decision_type == FORMATION_GATE:
            offending = self._negative(request.context_facts)
            if offending:
                return DreamAgentDecision(
                    approved=False,
                    summary=(
                        "Reject formation episode: conflicting context facts describe "
                        "unresolved infrastructure requirements."
                    ),
                    details={"transport": "context-sentiment", "offending_context": offending},
                )
        return DreamAgentDecision(
            approved=True,
            summary=f"Approved {request.decision_type}.",
            details={"transport": "context-sentiment", "context_fact_count": len(request.context_facts)},
        )

    def gate_requests(self) -> list[DreamAgentDecisionRequest]:
        return [r for r in self.requests if r.decision_type == FORMATION_GATE]


def formation_config() -> DreamConfig:
    """Formation only. Pruning is deliberately absent: it fails CLOSED under a
    refusing agent (WS-24), which would add a second reason for a missing row and
    make a red test ambiguous about which gate caused it."""
    return default_config().model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="formation-agent",
                    kind=DreamJobKind.FORMATION,
                    cadence_seconds=1,
                    agent=DreamAgentConfig(
                        agent_id="truth-curator",
                        name="Truth Curator",
                        scope=SCOPE,
                        decision_policy="Approve auditable memory maintenance decisions.",
                    ),
                ),
            )
        }
    )


async def seed_fact(client: Memotron, subject: str, predicate: str, obj: str) -> datetime:
    """Seed one fact and return a time strictly AFTER it becomes valid.

    Returned rather than assumed, so the episode is guaranteed to see the fact under
    ``as_of_episode_time``. Asserts the fact is actually context-visible: if it is not, the
    caller's test would silently become vacuous.
    """
    await client.add_memory(
        subject=subject,
        predicate=predicate,
        object=obj,
        relationship_type="REQUIRES",
        scope=SCOPE,
    )
    visible = [r for r in client.graph.context_visible_relationships(scope=SCOPE) if r.type != "MENTIONS"]
    assert visible, "seeded fact is not context-visible; the harness cannot reproduce T1-1"
    latest = max((r.valid_from or r.created_at) for r in visible)
    return latest + timedelta(seconds=1)


async def queue_episode(client: Memotron, reference_time: datetime) -> None:
    """An episode whose content is unambiguously worth remembering, and which shares
    no vocabulary with the seeded context — so a refusal cannot be explained by the
    episode itself."""
    await client.add_episode(
        name="standup-time-decision",
        episode_body=(
            "Memory: subject=Team; predicate=holds standup at; object=9am; relationship_type=PREFERS; confidence=0.9"
        ),
        source=EpisodeType.MESSAGE,
        scope=SCOPE,
        reference_time=reference_time,
    )


def formed_facts(client: Memotron) -> list[dict]:
    return [
        relationship for relationship in client.export_graph()["relationships"] if relationship["type"] == "PREFERS"
    ]


# --------------------------------------------------------------------------- the fake
def test_the_fake_discriminates_on_content_not_presence() -> None:
    """The fake must refuse on SENTIMENT, or every assertion below is vacuous.

    A fake that refused on non-emptiness would make ``context_facts=()`` pass for
    free. This is the guard against that, and it exercises the fake directly rather
    than through the product, so it stays true independently of the fix.
    """
    transport = ContextSentimentDreamAgentTransport()
    negative = ("Postgres cutover is blocked until a real key manager exists",)
    benign = ("Team holds standup at 9am", "Docs use British English")

    assert transport._negative(negative), "the marker set does not match its own example refusal"
    assert not transport._negative(benign), "benign facts must not look negative to the fake"
    assert len(benign) > 1, "the benign case must be NON-EMPTY, or this proves nothing about presence"


@pytest.mark.asyncio
async def test_the_fake_refuses_the_gate_only_when_context_is_negative() -> None:
    transport = ContextSentimentDreamAgentTransport()

    def request(facts: tuple[str, ...], decision_type: str = FORMATION_GATE) -> DreamAgentDecisionRequest:
        return DreamAgentDecisionRequest(
            agent_id="truth-curator",
            agent_name="Truth Curator",
            decision_policy="p",
            job_name="formation-agent",
            job_kind=DreamJobKind.FORMATION,
            decision_type=decision_type,
            proposed_action="Process this queued episode for memory formation.",
            context_facts=facts,
        )

    negative = ("deployment lacks required durability guarantees",)
    assert not (await transport.decide(request(negative))).approved
    assert (await transport.decide(request(("Team holds standup at 9am",)))).approved
    assert (await transport.decide(request(()))).approved
    # A non-formation decision with the same negative context must still be approved,
    # so a red formation test cannot be explained by the fake refusing everything.
    assert (await transport.decide(request(negative, "pruning_relationship_pruned"))).approved


# ------------------------------------------------------------------- the defect (T1-1)
@pytest.mark.asyncio
async def test_one_unresolved_fact_does_not_stop_a_scope_forming_memory(tmp_path: Path) -> None:
    """T1-1. RED before the ``context_facts=()`` fix at ``_formation.py:338``.

    One unresolved fact in the scope, one unrelated episode worth remembering. The
    episode must still be processed and its memory formed.
    """
    transport = ContextSentimentDreamAgentTransport()
    client = Memotron(
        graph_path=tmp_path / "suppression.sqlite",
        config=formation_config(),
        dream_agent_transport=transport,
    )
    episode_time = await seed_fact(client, "Postgres cutover", "is blocked until", "a real key manager exists")
    await queue_episode(client, episode_time)

    result = await client.run_due_dreams(now=episode_time + timedelta(seconds=1))

    assert transport.gate_requests(), "the formation gate never ran — the test proves nothing"
    assert formed_facts(client), (
        "the episode was refused because the scope already contained one unresolved fact. "
        "This is T1-1: the more a scope remembers, the less able it becomes to remember."
    )
    assert sum(run.processed_episodes for run in result.job_runs) == 1


@pytest.mark.asyncio
async def test_a_benign_fact_never_suppressed_formation(tmp_path: Path) -> None:
    """The control, and it must pass BEFORE the fix as well as after.

    Row F of the findings: with benign context the same agent approves. If this ever
    goes red, the failure is in the harness or in formation generally, not in T1-1 —
    which is exactly the distinction a bare red test cannot make.
    """
    transport = ContextSentimentDreamAgentTransport()
    client = Memotron(
        graph_path=tmp_path / "benign.sqlite",
        config=formation_config(),
        dream_agent_transport=transport,
    )
    episode_time = await seed_fact(client, "Docs", "are written in", "British English")
    await queue_episode(client, episode_time)

    await client.run_due_dreams(now=episode_time + timedelta(seconds=1))

    assert formed_facts(client), "formation failed with BENIGN context — the harness is broken, not T1-1"


@pytest.mark.asyncio
async def test_the_fix_is_scoped_to_the_formation_gate(tmp_path: Path) -> None:
    """Pin WHERE the fix applies, so nobody 'fixes' it by disabling context globally.

    The formation gate must receive no ambient context. Consolidation must keep its
    own, because reasoning over existing memories IS consolidation's job — and
    ``_consolidation.py:115`` passes context for that reason.
    """
    transport = ContextSentimentDreamAgentTransport()
    client = Memotron(
        graph_path=tmp_path / "scoped.sqlite",
        config=formation_config(),
        dream_agent_transport=transport,
    )
    episode_time = await seed_fact(client, "Postgres cutover", "is blocked until", "a real key manager exists")
    await queue_episode(client, episode_time)

    await client.run_due_dreams(now=episode_time + timedelta(seconds=1))

    gate = transport.gate_requests()
    assert gate, "the formation gate never ran"
    assert all(request.context_facts == () for request in gate), (
        f"the formation gate is still being handed the scope's existing memories; saw {gate[0].context_facts!r}"
    )


@pytest.mark.asyncio
async def test_a_formation_run_REPORTS_the_scopes_it_formed_into(tmp_path: Path) -> None:
    """Formation never set `processed_scopes`, so every run reported 0 -- including runs
    that formed memory.

    Found on `latest` 2026-09-07, and it cost real time twice over. It is the field an
    operator reads to answer "is the Dream Worker doing anything", and 258 consecutive
    successful runs all recorded `processed_scopes=0`, which reads exactly like a dead
    worker. It is not cosmetic: a permanent zero is worse than a wrong number, because it
    is indistinguishable from the outage it would be used to detect.

    `_run_coherence` set it to 1 and `_consolidation` increments it per scope; formation
    alone never touched it, so the three job kinds disagreed about the same field.

    The fix is one assignment from `formation_scopes` -- a dict the run already maintained
    for exactly this ("the scopes this run actually formed into, in first-touch order").
    """
    client = Memotron(graph_path=tmp_path / "scopes.sqlite", config=formation_config())
    episode_time = await seed_fact(client, "Redis cutover", "is blocked until", "a real key manager exists")
    await queue_episode(client, episode_time)

    result = await client.run_due_dreams(now=episode_time + timedelta(seconds=1))

    formation_runs = [run for run in result.job_runs if run.job_kind is DreamJobKind.FORMATION]
    assert formation_runs, "no formation job ran -- this test would pass vacuously"
    formed = [run for run in formation_runs if run.processed_episodes > 0]
    assert formed, "no formation run processed an episode -- nothing to report scopes for"

    for run in formed:
        assert run.processed_scopes >= 1, (
            f"formation processed {run.processed_episodes} episode(s) and created "
            f"{run.created_relationships} relationship(s) but reported processed_scopes="
            f"{run.processed_scopes}. A run that did work must not report zero scopes."
        )


@pytest.mark.asyncio
async def test_a_formation_run_that_forms_NOTHING_reports_zero_scopes(tmp_path: Path) -> None:
    """The control. Without it, `processed_scopes = 1` unconditionally would satisfy the
    test above while making the counter just as uninformative in the other direction --
    a worker with nothing to do would look busy forever."""
    client = Memotron(graph_path=tmp_path / "idle.sqlite", config=formation_config())
    now = datetime(2026, 6, 1, tzinfo=UTC)

    result = await client.run_due_dreams(now=now)

    for run in result.job_runs:
        if run.job_kind is DreamJobKind.FORMATION and run.processed_episodes == 0:
            assert run.processed_scopes == 0, (
                f"an idle formation run reported processed_scopes={run.processed_scopes}; "
                "it formed into no scopes and must say so"
            )
