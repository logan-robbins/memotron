"""
WDPR Memotron — Corporate Support Memory Simulation

Covers the full public SDK surface across six weeks of a realistic WDPR
corporate account lifecycle.

  Preload Client-managed exact memory
          add_memory · immediate materialization · no dream queue
  Week 1  Initial qualification call
          add_session · add_episode · formation · profile
  Week 2  Compliance update + travel week
          add_session · add_episode · formation · pruning · truth_timeline
  Week 3  Requirements finalized
          add_session · formation · full truth_timeline
  Week 4  Shadow document + reinforcement
          add_context · add_episode_bulk · reinforcement (observed_count)
  Week 5  Streaming session + consolidation
          open_session · run_dream_job (consolidation) · derived memories
  Week 6  Operator actions
          correct_memory · forget_memory · memory_evidence
  Final   Full graph inspection
          memory_evolution · entity_neighborhood · search_context · dream_history · export_graph

Run modes:

  uv run examples/simulation.py             # offline — no API keys required
  uv run examples/simulation.py --live      # real LLM extraction through the
                                            # JedAI Gateway (claude-sonnet-4-6)
                                            # requires LITELLM_API_KEY in .env
                                            # or the process environment
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from memotron import (
    ConversationTurn,
    DreamAgentConfig,
    DreamConfig,
    DreamContextPolicy,
    DreamInstructionSet,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryScope,
    NodeInstruction,
    ProfilePolicy,
    PruningPolicy,
    RelationshipInstruction,
    ScopeKind,
)
from memotron.agents import OpenAICompatibleDreamAgentTransport
from memotron.config import RelationshipCardinality, default_prompt_profiles
from memotron.extraction import (
    ExtractionRequest,
    OpenAICompatibleExtractionTransport,
    RuleBasedExtractionTransport,
)
from memotron.gateway import GATEWAY_API_KEY_ENV
from memotron.models import Episode, RelationshipStatus

# Undated gateway alias. Dated Anthropic model ids do not resolve on the gateway.
LIVE_MODEL = "claude-sonnet-4-6"

# ── Layout ──────────────────────────────────────────────────────────────────────

WIDTH = 68
BAR = "━" * WIDTH
THIN = "─" * WIDTH


def header(text: str) -> None:
    print(f"\n{BAR}")
    print(f"  {text}")
    print(BAR)


def section(label: str) -> None:
    print(f"\n{THIN}")
    print(f"  {label}")
    print(THIN)


def bullet(text: str, indent: int = 2) -> None:
    print(f"{' ' * indent}{text}")


def blank() -> None:
    print()


def sdk_label(method: str) -> str:
    return f"[SDK: {method}]"


# ── Assertion harness ────────────────────────────────────────────────────────────


class Checker:
    """Records pass/fail for every invariant check across the simulation.

    In offline mode every check is strict (exact text, exact counts).
    In live mode structural checks are strict; text-content checks are
    advisory (they print but never count as failures) because the LLM
    may phrase facts differently on every run.
    """

    def __init__(self, live: bool) -> None:
        self._live = live
        self._results: list[tuple[bool, str, bool]] = []  # (passed, label, strict)

    def check(self, condition: bool, label: str, *, advisory: bool = False) -> bool:
        """Record one check. advisory=True → never fails in live mode."""
        strict = not (advisory and self._live)
        passed = condition or not strict
        self._results.append((condition, label, strict))
        icon = "✓" if condition else ("⚠ (advisory)" if not strict else "✗ FAIL")
        bullet(f"  {icon}  {label}", indent=4)
        return passed

    @property
    def n_passed(self) -> int:
        return sum(1 for ok, _, strict in self._results if ok or not strict)

    @property
    def n_failed(self) -> int:
        return sum(1 for ok, _, strict in self._results if not ok and strict)

    def summary(self) -> None:
        section("Validation summary")
        blank()
        total = len(self._results)
        passed = sum(1 for ok, _, _ in self._results if ok)
        failed = self.n_failed
        advis = sum(1 for ok, _, strict in self._results if not ok and not strict)
        bullet(f"  {passed}/{total} checks passed   {failed} failures   {advis} advisory mismatches")
        if failed:
            blank()
            bullet("Failed checks:")
            for ok, label, strict in self._results:
                if not ok and strict:
                    bullet(f"    ✗  {label}", indent=0)
        blank()


# ── Scopes and timeline ─────────────────────────────────────────────────────────

CUSTOMER_SCOPE = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="pinnacle-events")
USER_SCOPE = MemoryScope(kind=ScopeKind.USER, scope_id="priya-sharma")
AGENT_SCOPE = MemoryScope(kind=ScopeKind.AGENT, scope_id="group-sales-agent")

WEEK1 = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
WEEK2 = datetime(2026, 5, 8, 10, 0, tzinfo=UTC)
WEEK3 = datetime(2026, 5, 15, 10, 0, tzinfo=UTC)
WEEK4 = datetime(2026, 5, 22, 10, 0, tzinfo=UTC)
WEEK5 = datetime(2026, 5, 29, 10, 0, tzinfo=UTC)
WEEK6 = datetime(2026, 6, 2, 10, 0, tzinfo=UTC)


# ── Config ───────────────────────────────────────────────────────────────────────


def build_config(live: bool) -> DreamConfig:
    dream_agent = DreamAgentConfig(
        agent_id="support-dream-agent",
        name="Support Dream Agent",
        scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="support-dream-agent"),
        decision_policy=(
            "Approve memory formation, consolidation, and pruning actions that "
            "preserve scoped evidence and temporal accuracy."
        ),
    )
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Durable people, companies, agents, systems, and concepts relevant to WDW corporate sales.",
                properties=("kind", "role", "tier", "region", "system"),
                # Non-strict so live LLM extraction can add domain-specific properties
                # (e.g. headcount_min, threshold) without hard-failing.
                strict_properties=not live,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Compliance requirements, document obligations, and approval gates.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                temporal_semantics="Valid from the stated date. Supersedes previous active requirements for the same subject–predicate slot.",
            ),
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Stable communication, format, and workflow preferences.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
                temporal_semantics=(
                    "Valid from the stated date. When a preference is stated as "
                    'temporary or with an expiry ("until", "through", "for the '
                    'next N weeks", "expires on ..."), set its validity end so it '
                    "stops being current truth after that point."
                ),
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Operating lessons the agent should apply in future similar cases.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
        ),
    )
    prompt_profile = "research-temporal" if live else "support-memory"
    return DreamConfig(
        instruction_sets=(instructions,),
        prompt_profiles=default_prompt_profiles(),
        jobs=(
            DreamJob(
                name="formation",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                prompt_profile=prompt_profile,
                prompt_profile_version="v1",
                agent=dream_agent,
                context_policy=DreamContextPolicy(max_relationships=10),
            ),
            DreamJob(
                name="consolidation",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=999_999,  # never runs automatically — forced manually in Week 5
                scope=AGENT_SCOPE,
                prompt_profile="agent-lessons",
                prompt_profile_version="v1",
                agent=dream_agent,
                context_policy=DreamContextPolicy(
                    max_relationships=6,
                    relationship_types=("SHOULD",),
                ),
            ),
            DreamJob(
                name="pruning",
                kind=DreamJobKind.PRUNING,
                cadence_seconds=1,
                agent=dream_agent,
            ),
        ),
        pruning=PruningPolicy(min_confidence=0.60, superseded_retention_seconds=86400 * 7),
        profile=ProfilePolicy(max_static_facts=12, max_dynamic_facts=6),
    )


# ── Offline extraction transport ────────────────────────────────────────────────


class OfflineExtractionTransport:
    """
    Rule-based for formation/shadow, pre-baked response for consolidation.
    Lets the simulation run with no API keys.
    """

    def __init__(self) -> None:
        self._rule = RuleBasedExtractionTransport()

    async def extract_memories(self, request: ExtractionRequest) -> list[dict]:
        if request.episode.metadata.get("dream_job_kind") == DreamJobKind.CONSOLIDATION.value:
            return self._consolidation_memories(request)
        return await self._rule.extract_memories(request)

    def _consolidation_memories(self, request: ExtractionRequest) -> list[dict]:
        facts = " ".join(m.fact for m in request.graph_context).lower()
        if "headcount" in facts or "confirm" in facts:
            return [
                {
                    "subject": "Group Sales Agent",
                    "predicate": "should",
                    "object": "verify headcount, current compliance docs, and W9 before committing to any booking",
                    "relationship_type": "SHOULD",
                    "confidence": 0.92,
                    "metadata": {"derived_from": "consolidation", "source": "agent_reflection"},
                }
            ]
        return []


# ── Episode content ──────────────────────────────────────────────────────────────

# ── Week 1 ──

WEEK1_CUSTOMER_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Pinnacle Events Co",
                "predicate": "requires",
                "object": "signed indemnification form before any WDW site visit",
                "relationship_type": "REQUIRES",
                "confidence": 0.96,
                "valid_from": WEEK1.isoformat(),
                "subject_properties": {"kind": "corporate client", "tier": "enterprise", "region": "NA"},
                "source_text": "Our legal team needs the indemnification form signed before any site visits.",
            },
            {
                "subject": "Pinnacle Events Co",
                "predicate": "prefers",
                "object": "consolidated PDF for all cost estimates",
                "relationship_type": "PREFERS",
                "confidence": 0.91,
                "valid_from": WEEK1.isoformat(),
                "source_text": "Please send everything in one PDF.",
            },
        ]
    }
)

WEEK1_USER_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Priya Sharma",
                "predicate": "prefers",
                "object": "email for all contract correspondence",
                "relationship_type": "PREFERS",
                "confidence": 0.89,
                "valid_from": WEEK1.isoformat(),
                "source_text": "Always email me directly — I need a paper trail.",
            },
        ]
    }
)

WEEK1_AGENT_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Group Sales Agent",
                "predicate": "should",
                "object": "confirm headcount before quoting group rates",
                "relationship_type": "SHOULD",
                "confidence": 0.87,
                "valid_from": WEEK1.isoformat(),
                "source_text": "Rates vary significantly by group size.",
            },
        ]
    }
)

WEEK1_TURNS = [
    ConversationTurn(
        role="user",
        content="Hi, I'm Priya from Pinnacle Events. We're looking at a corporate retreat for about 400 people at WDW in September. Our legal team needs a signed indemnification form before we can do any site visits.",
        timestamp=WEEK1,
    ),
    ConversationTurn(
        role="assistant",
        content="Absolutely. I'll have the indemnification form ready. What headcount are you planning so I can give you accurate group rates?",
        timestamp=WEEK1 + timedelta(minutes=2),
    ),
    ConversationTurn(
        role="user",
        content="Around 400 — could be up to 420. Please send all cost estimates in one consolidated PDF; our CFO reviews them all at once.",
        timestamp=WEEK1 + timedelta(minutes=4),
    ),
    ConversationTurn(
        role="assistant",
        content="Got it — 400-420 guests, consolidated PDF for all estimates. What's the best way to reach you?",
        timestamp=WEEK1 + timedelta(minutes=6),
    ),
    ConversationTurn(
        role="user",
        content="Always email me directly at priya@pinnacleevents.com — I need a paper trail for everything.",
        timestamp=WEEK1 + timedelta(minutes=8),
    ),
]

# ── Week 2 ──

WEEK2_CUSTOMER_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Pinnacle Events Co",
                "predicate": "requires",
                "object": "signed indemnification form AND venue liability waiver before any WDW site visit",
                "relationship_type": "REQUIRES",
                "confidence": 0.97,
                "valid_from": WEEK2.isoformat(),
                "subject_properties": {"kind": "corporate client", "tier": "enterprise", "region": "NA"},
                "source_text": "Our risk team just added a venue liability waiver on top of the indemnification form.",
            },
        ]
    }
)

WEEK2_USER_TEMP_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Priya Sharma",
                "predicate": "prefers",
                "object": "phone calls this week — on limited email access",
                "relationship_type": "PREFERS",
                "confidence": 0.88,
                "valid_from": WEEK2.isoformat(),
                "valid_to": (WEEK2 + timedelta(days=5)).isoformat(),
                "source_text": "I'm traveling Mon-Fri so please call instead of email this week.",
            },
        ]
    }
)

WEEK2_TURNS = [
    ConversationTurn(
        role="user",
        content="Quick update — our risk team just added a venue liability waiver on top of the indemnification form. We need both signed before any site visits.",
        timestamp=WEEK2,
    ),
    ConversationTurn(
        role="assistant",
        content="Noted — both the indemnification form and venue liability waiver are required before scheduling your site visit.",
        timestamp=WEEK2 + timedelta(minutes=3),
    ),
    ConversationTurn(
        role="user",
        content="Also — I'm traveling all week so please call instead of email. I'll be back to normal communication Monday.",
        timestamp=WEEK2 + timedelta(minutes=5),
    ),
]

# ── Week 3 ──

WEEK3_CUSTOMER_OFFLINE = json.dumps(
    {
        "memories": [
            {
                "subject": "Pinnacle Events Co",
                "predicate": "requires",
                "object": "venue liability waiver and W9 form before any WDW site visit",
                "relationship_type": "REQUIRES",
                "confidence": 0.99,
                "valid_from": WEEK3.isoformat(),
                "metadata": {"confirmed_by": "compliance_officer", "correction": True},
                "source_text": "Compliance confirmed: just the venue liability waiver plus a W9. That's it.",
            },
        ]
    }
)

WEEK3_TURNS = [
    ConversationTurn(
        role="user",
        content="Good news — our compliance officer reviewed everything. We don't need the indemnification form. Just the venue liability waiver plus a W9.",
        timestamp=WEEK3,
    ),
    ConversationTurn(
        role="assistant",
        content="Perfect — I'll update the file. Venue liability waiver and W9 before site visits, no indemnification form.",
        timestamp=WEEK3 + timedelta(minutes=2),
    ),
]

# ── Week 4 — shadow document + bulk episodes ──

SHADOW_DOCUMENT = """\
Pinnacle Events Co — Account Settings (updated May 22, 2026)

Client tier: Enterprise NA
Primary contact: Priya Sharma, priya@pinnacleevents.com
Backup contact: Marcus Webb, marcus@pinnacleevents.com (CFO approvals)

Memory: subject=Pinnacle Events Co; predicate=requires; object=venue liability waiver and W9 form before any WDW site visit; relationship_type=REQUIRES; confidence=0.99

Memory: subject=Pinnacle Events Co; predicate=prefers; object=consolidated PDF for all cost estimates; relationship_type=PREFERS; confidence=0.91

Memory: subject=Group Sales Agent; predicate=should; object=cc Marcus Webb on all pricing proposals above $500k; relationship_type=SHOULD; confidence=0.88
"""

# Additional historical agent lessons ingested in bulk
BULK_AGENT_LESSONS = [
    Episode(
        name="agent-lesson-headcount-policy",
        body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Group Sales Agent",
                        "predicate": "should",
                        "object": "confirm headcount before quoting group rates",
                        "relationship_type": "SHOULD",
                        "confidence": 0.92,
                        "valid_from": WEEK4.isoformat(),
                        "source_text": "Second observation — headcount drives all pricing tiers.",
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=AGENT_SCOPE,
        reference_time=WEEK4,
    ),
    Episode(
        name="agent-lesson-site-visit-checklist",
        body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "Group Sales Agent",
                        "predicate": "should",
                        "object": "send compliance document checklist 48 hours before each site visit",
                        "relationship_type": "SHOULD",
                        "confidence": 0.85,
                        "valid_from": WEEK4.isoformat(),
                        "source_text": "Operations flagged a gap — documents weren't ready on arrival.",
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=AGENT_SCOPE,
        reference_time=WEEK4,
    ),
]

# ── Week 5 — streaming session turns ──

WEEK5_TURNS = [
    ConversationTurn(
        role="user",
        content="Hi Priya here. The September booking is looking good. One addition — the CFO Marcus Webb now needs to be cc'd on all pricing proposals above $500k.",
        timestamp=WEEK5,
    ),
    ConversationTurn(
        role="assistant",
        content="Understood — I'll make sure Marcus Webb is copied on all pricing proposals above $500,000.",
        timestamp=WEEK5 + timedelta(minutes=2),
    ),
    ConversationTurn(
        role="user",
        content="Great. Also, can you confirm the site visit is still on for next Tuesday?",
        timestamp=WEEK5 + timedelta(minutes=4),
    ),
    ConversationTurn(
        role="assistant",
        content="Yes — site visit is confirmed for Tuesday. I'll send over the venue liability waiver and W9 checklist 48 hours before.",
        timestamp=WEEK5 + timedelta(minutes=6),
    ),
]


# ── Print helpers ────────────────────────────────────────────────────────────────


def _requires_slot(client: Memotron, scope: MemoryScope) -> tuple[str, str] | None:
    for rel in client.graph.relationships():
        if rel.type != "REQUIRES" or rel.properties.get("scope_key") != scope.key:
            continue
        try:
            name = str(client.graph.get_node(rel.source_uuid).properties["name"])
            pred = str(rel.properties.get("predicate", "requires"))
            return name, pred
        except (ValueError, KeyError):
            continue
    return None


async def dream(client: Memotron, now: datetime) -> None:
    result = await client.run_due_dreams(now=now)
    for run in result.job_runs:
        if run.job_kind == DreamJobKind.FORMATION:
            bullet(
                f"  formation    processed={run.processed_episodes}  created={run.created_relationships}  "
                f"reinforced={run.reinforced_relationships}  superseded={run.superseded_relationships}",
                indent=0,
            )
        elif run.job_kind == DreamJobKind.CONSOLIDATION:
            bullet(
                f"  consolidation  scopes={run.processed_scopes}  created={run.created_relationships}  "
                f"decisions={run.decision_count}",
                indent=0,
            )
        else:
            bullet(
                f"  pruning      repaired={run.superseded_relationships}  pruned={run.pruned_relationships}", indent=0
            )


async def active_facts(client: Memotron, scope: MemoryScope, as_of: datetime) -> list:
    """Return deduplicated active facts visible at as_of (used by both display and checks)."""
    prof = await client.profile(scope=scope, as_of=as_of)
    seen: dict[str, object] = {}
    for f in prof.static_facts + prof.dynamic_facts:
        seen[f.relationship_uuid] = f
    return list(seen.values())


async def print_memories(client: Memotron, scope: MemoryScope, label: str, as_of: datetime) -> None:
    facts = await active_facts(client, scope, as_of)
    facts_sorted = sorted(facts, key=lambda f: f.relationship_type)  # type: ignore[attr-defined]
    print(f"\n  {label}  [{len(facts)} active]")
    if not facts_sorted:
        bullet("  (none)", indent=4)
    for f in facts_sorted:  # type: ignore[union-attr]
        obs = f"  ×{f.observed_count}" if f.observed_count > 1 else ""
        vt = f"  expires {f.valid_to.date().isoformat()}" if f.valid_to else ""
        bullet(f"  [{f.relationship_type}]  {f.fact}", indent=4)
        bullet(f"  confidence={f.confidence:.2f}{obs}{vt}", indent=10)


async def print_truth_timeline(client: Memotron, scope: MemoryScope) -> None:
    found = _requires_slot(client, scope)
    if found is None:
        bullet("  (no REQUIRES relationships found)", indent=4)
        return
    subject, predicate = found
    timeline = await client.truth_timeline(
        scope=scope, subject=subject, predicate=predicate, relationship_type="REQUIRES"
    )
    print(f"\n  Truth timeline  {subject} → {predicate}")
    for i, e in enumerate(timeline, 1):
        current = " ← CURRENT" if e.is_current else ""
        vf = e.valid_from.date().isoformat() if e.valid_from else "?"
        vt = f" → {e.valid_to.date().isoformat()}" if e.valid_to else ""
        bullet(f"  {i}. [{e.status.value.upper():<12}]  {vf}{vt}", indent=4)
        bullet(f"     {e.object}{current}", indent=4)


async def print_entity_neighborhood(client: Memotron, scope: MemoryScope, entity: str) -> None:
    edges = await client.entity_neighborhood(scope=scope, entity=entity, limit=10)
    print(f"\n  Entity neighborhood  '{entity}'  [{len(edges)} edges]")
    for edge in edges:
        direction = "→" if edge.direction == "outgoing" else "←"
        status_tag = "" if edge.status == RelationshipStatus.ACTIVE else f"  [{edge.status.value}]"
        bullet(f"  {direction}  [{edge.relationship_type}]  {edge.fact}{status_tag}", indent=4)
        bullet(f"  confidence={edge.confidence:.2f}  observed={edge.observed_count}", indent=10)


async def print_search_context(client: Memotron, query: str) -> None:
    results = await client.search_context(
        query=query,
        scopes=[CUSTOMER_SCOPE, USER_SCOPE, AGENT_SCOPE],
        limit=8,
    )
    print(f"\n  search_context('{query}')  [{len(results)} results across 3 scopes]")
    for r in results:
        bullet(f"  [{r.scope.kind.value:<9}]  {r.fact}", indent=4)
        bullet(f"  confidence={r.confidence:.2f}  scope_rank={r.scope_rank}  score={r.score}", indent=10)


async def print_memory_evidence(client: Memotron, relationship_uuid: str, scope: MemoryScope, label: str) -> None:
    ev = await client.memory_evidence(relationship_uuid=relationship_uuid, scope=scope)
    print(f"\n  memory_evidence  {label}")
    bullet(f"  Fact:            {ev.fact}", indent=4)
    bullet(f"  Status:          {ev.status.value}", indent=4)
    bullet(f"  Confidence:      {ev.confidence:.2f}  ×{ev.observed_count}", indent=4)
    bullet(f"  Source episodes: {len(ev.episodes)}", indent=4)
    for ep in ev.episodes:
        bullet(f"  [{ep.source.value}]  {ep.name}  @{ep.reference_time.date()}", indent=6)


async def print_memory_evolution_proof(client: Memotron, scope: MemoryScope, as_of: datetime):
    proof = await client.memory_evolution(scope=scope, as_of=as_of)
    section("Memory evolution proof  (memory_evolution())")
    blank()
    bullet(f"Raw episodes       {proof.episode_count}")
    bullet(f"Processed episodes {proof.processed_episode_count}  pending={proof.pending_episode_count}")
    bullet(f"Dream decisions    {proof.decision_count}")
    bullet(
        "Relationships      "
        f"created={proof.created_relationship_count}  "
        f"reinforced={proof.reinforced_relationship_count}  "
        f"superseded={proof.superseded_relationship_count}  "
        f"pruned={proof.pruned_relationship_count}"
    )
    bullet(
        "Caller profile     "
        f"active={proof.active_relationship_count}  inactive-but-auditable={proof.inactive_relationship_count}"
    )
    blank()
    bullet("Evolution signals")
    for signal in proof.signals:
        icon = "✓" if signal.observed else "·"
        bullet(f"  {icon}  {signal.name:<19} count={signal.count}  {signal.evidence}", indent=4)
    blank()
    bullet("Active caller facts")
    for fact in proof.active_facts[:5]:
        bullet(f"  [{fact.relationship_type}] {fact.fact}  ×{fact.observed_count}", indent=4)
    blank()
    bullet("Inactive audit trail")
    for fact in proof.inactive_facts[:5]:
        reason = f"  reason={fact.pruned_reason}" if fact.pruned_reason else ""
        bullet(f"  [{fact.status.value}] [{fact.relationship_type}] {fact.fact}{reason}", indent=4)
    return proof


async def site_visit_action_plan(client: Memotron, as_of: datetime) -> str:
    customer_facts = await active_facts(client, CUSTOMER_SCOPE, as_of)
    agent_facts = await active_facts(client, AGENT_SCOPE, as_of)
    requirements = [fact.object for fact in customer_facts if fact.relationship_type == "REQUIRES"]
    preferences = [fact.object for fact in customer_facts if fact.relationship_type == "PREFERS"][:2]
    lessons = [fact.object for fact in agent_facts if fact.relationship_type == "SHOULD"][:3]
    lines = ["Use evolved memory:"]
    if requirements:
        lines.append(f"- Require before site visit: {requirements[0]}.")
    if preferences:
        lines.append(f"- Package proposal as: {', '.join(preferences)}.")
    if lessons:
        lines.append(f"- Agent operating checks: {'; '.join(lessons)}.")
    return "\n".join(lines)


async def print_agent_context(client: Memotron, scopes: list[MemoryScope]) -> None:
    section("Agent context prompt  (profile() for each scope)")
    for scope in scopes:
        prof = await client.profile(scope=scope)
        if prof.static_facts or prof.dynamic_facts:
            print(f"\n{prof.rendered_context}\n")


async def print_dream_decisions(client: Memotron, limit: int = 8) -> None:
    section("Dream-agent decisions")
    decisions = await client.dream_decisions(limit=limit, agent_id="support-dream-agent")
    for d in reversed(decisions):
        approved = "✓" if d.details.get("approved") else "✗"
        bullet(f"  {approved}  {d.job_kind.value:<14} {d.decision_type}", indent=0)
        if d.summary:
            bullet(f"     └ {d.summary[:78]}", indent=0)


# ── Main ─────────────────────────────────────────────────────────────────────────


async def main(live: bool) -> bool:
    """Returns True if all strict checks passed."""
    env_file = Path(".env")
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'\""))

    has_key = bool(os.environ.get(GATEWAY_API_KEY_ENV, "").strip())
    if live and not has_key:
        print(
            f"ERROR: --live requires {GATEWAY_API_KEY_ENV} in .env or the process "
            f"environment. Add the JedAI Gateway virtual key as {GATEWAY_API_KEY_ENV} "
            "to the gitignored .env before the live demo.",
            file=sys.stderr,
        )
        return False

    chk = Checker(live=live)

    header("WDPR Memotron · Corporate Support Memory Simulation")
    blank()
    bullet("Scenario   Corporate & Group Sales — Walt Disney World")
    bullet("Customer   Pinnacle Events Co  (enterprise, 400-person retreat)")
    bullet("User       Priya Sharma  (Senior Events Coordinator)")
    bullet("Agent      Group Sales Agent")
    blank()
    if live:
        bullet(f"Extraction   JedAI Gateway  {LIVE_MODEL}  (real LLM)")
        bullet(f"Dream agent  JedAI Gateway  {LIVE_MODEL}")
    else:
        bullet("Extraction   offline  (structured episodes, no API keys required)")
        bullet("Dream agent  local deterministic")
    blank()
    bullet("SDK methods exercised across this run:")
    for m in [
        "add_memory",
        "add_session",
        "open_session",
        "add_episode",
        "add_episode_bulk",
        "add_context",
        "run_due_dreams",
        "run_dream_job",
        "dream_status",
        "search",
        "search_context",
        "profile",
        "truth_timeline",
        "entity_neighborhood",
        "memory_evidence",
        "correct_memory",
        "forget_memory",
        "memory_evolution",
        "dream_history",
        "dream_decisions",
        "export_graph",
    ]:
        bullet(f"  {m}()", indent=4)

    config = build_config(live)
    # Live mode rides the same gateway endpoint for extraction AND dream-agent
    # decisions; one key, one base URL, no Anthropic-native path.
    extraction_transport = (
        OpenAICompatibleExtractionTransport(model=LIVE_MODEL) if live else OfflineExtractionTransport()
    )
    dream_agent_transport = OpenAICompatibleDreamAgentTransport(model=LIVE_MODEL) if live else None

    with TemporaryDirectory() as tmp:
        client = Memotron(
            graph_path=Path(tmp) / "simulation.sqlite",
            config=config,
            extraction_transport=extraction_transport,
            dream_agent_transport=dream_agent_transport,
        )

        # ── PRELOAD: Client-managed exact memory ─────────────────────────────────

        section(f"PRELOAD · Client-managed exact memory  (Apr 30)  {sdk_label('add_memory')}")
        direct_memory = await client.add_memory(
            subject="Pinnacle Events Co",
            predicate="prefers",
            object="executive-summary proposal briefs",
            relationship_type="PREFERS",
            confidence=0.94,
            scope=CUSTOMER_SCOPE,
            reference_time=WEEK1 - timedelta(days=1),
            source_text="CRM account setting: Proposal briefs should start with an executive summary.",
            metadata={"source": "crm_account_setting", "mode": "client_managed"},
        )
        preload_statuses = await client.dream_status(now=WEEK1 - timedelta(hours=1))
        preload_pending = next(s.pending_episodes for s in preload_statuses if s.job_kind == DreamJobKind.FORMATION)
        preload_evidence = await client.memory_evidence(
            relationship_uuid=direct_memory.relationship_uuid,
            scope=CUSTOMER_SCOPE,
        )
        bullet(f"▸ add_memory: {direct_memory.fact}")
        bullet(f"  relationship={direct_memory.relationship_uuid[:8]}…  episode={direct_memory.episode_uuid[:8]}…")
        bullet(
            f"  queued_for_dreaming={direct_memory.queued_for_dreaming}  memory_mode={direct_memory.memory_mode.value}"
        )
        bullet(f"  dream_status → formation pending_episodes={preload_pending}")
        bullet(f"  evidence source_description={preload_evidence.episodes[0].source_description!r}")

        blank()
        section("Preload checks")
        chk.check(
            direct_memory.queued_for_dreaming is False,
            "add_memory materialized immediately and did not queue dream extraction",
        )
        chk.check(direct_memory.created_relationships == 1, "add_memory created exactly 1 graph relationship")
        chk.check(preload_pending == 0, "dream_status shows 0 pending episodes after direct memory preload")
        chk.check(
            client.graph.is_episode_processed(direct_memory.episode_uuid),
            "client-managed evidence episode is marked processed",
        )
        chk.check(
            preload_evidence.created_by == "client-managed-memory",
            "memory_evidence attributes direct fact to client-managed-memory",
        )

        # ── WEEK 1: Initial qualification ─────────────────────────────────────────

        section(f"WEEK 1 · Initial qualification call  (May 1)  {sdk_label('add_session · add_episode')}")
        if live:
            result = await client.add_session(
                name="week1-qualification",
                turns=WEEK1_TURNS,
                scope=CUSTOMER_SCOPE,
                turns_per_episode=len(WEEK1_TURNS),
                time_gap_seconds=None,
            )
            bullet(
                f"▸ add_session: {result.turns_ingested} turns → {result.episodes_created} episode(s)  session_id={result.session_id[:8]}…"
            )
            await client.add_episode(
                name="week1-agent-lesson",
                episode_body=WEEK1_AGENT_OFFLINE,
                source=EpisodeType.JSON,
                scope=AGENT_SCOPE,
                reference_time=WEEK1,
            )
        else:
            for name, body, scope in [
                ("week1-customer", WEEK1_CUSTOMER_OFFLINE, CUSTOMER_SCOPE),
                ("week1-user", WEEK1_USER_OFFLINE, USER_SCOPE),
                ("week1-agent", WEEK1_AGENT_OFFLINE, AGENT_SCOPE),
            ]:
                await client.add_episode(
                    name=name, episode_body=body, source=EpisodeType.JSON, scope=scope, reference_time=WEEK1
                )
            bullet("▸ add_episode ×3  (customer, user, agent)")

        blank()
        statuses = await client.dream_status(now=WEEK1 + timedelta(seconds=30))
        pending = next(s.pending_episodes for s in statuses if s.job_kind == DreamJobKind.FORMATION)
        bullet(f"  dream_status → formation due=True  pending_episodes={pending}")
        bullet("Dreaming...")
        await dream(client, now=WEEK1 + timedelta(hours=1))

        await print_memories(client, CUSTOMER_SCOPE, "Pinnacle Events Co", as_of=WEEK1 + timedelta(hours=1))
        await print_memories(client, USER_SCOPE, "Priya Sharma", as_of=WEEK1 + timedelta(hours=1))
        await print_memories(client, AGENT_SCOPE, "Group Sales Agent", as_of=WEEK1 + timedelta(hours=1))

        blank()
        section("Week 1 checks")
        w1_cust = await active_facts(client, CUSTOMER_SCOPE, WEEK1 + timedelta(hours=1))
        w1_user = await active_facts(client, USER_SCOPE, WEEK1 + timedelta(hours=1))
        w1_agent = await active_facts(client, AGENT_SCOPE, WEEK1 + timedelta(hours=1))
        w1_tl = (
            await client.truth_timeline(
                scope=CUSTOMER_SCOPE,
                subject=w1_cust[0].subject if w1_cust else "?",
                predicate="requires",
                relationship_type="REQUIRES",
            )
            if w1_cust
            else []
        )
        chk.check(len(w1_cust) >= 2, "customer has ≥2 active memories (REQUIRES + PREFERS)")
        # Advisory: LLM may assign Priya's email preference to the customer scope rather than user
        chk.check(len(w1_user) >= 1, "user has ≥1 active memory (PREFERS)", advisory=True)
        chk.check(len(w1_agent) >= 1, "agent has ≥1 active memory (SHOULD)")
        chk.check(
            any(f.relationship_type == "REQUIRES" for f in w1_cust), "customer has an active REQUIRES relationship"
        )
        chk.check(
            any("indemnification" in f.fact.lower() for f in w1_cust),
            "customer REQUIRES mentions indemnification form",
            advisory=True,
        )
        # LLM may extract multiple REQUIRES from one session (both get ≥1 timeline entries)
        chk.check(len(w1_tl) >= 1, "truth_timeline has ≥1 entry after Week 1")
        chk.check(any(e.is_current for e in w1_tl), "truth_timeline has a current entry after Week 1")

        # ── WEEK 2: Compliance update + travel ────────────────────────────────────

        section(f"WEEK 2 · Compliance update + travel week  (May 8)  {sdk_label('add_session · add_episode')}")
        if live:
            r2 = await client.add_session(
                name="week2-update",
                turns=WEEK2_TURNS,
                scope=CUSTOMER_SCOPE,
                turns_per_episode=len(WEEK2_TURNS),
                time_gap_seconds=None,
            )
            bullet(f"▸ add_session: {r2.turns_ingested} turns → {r2.episodes_created} episode(s)")
            await client.add_episode(
                name="week2-temp-pref",
                episode_body=WEEK2_USER_TEMP_OFFLINE,
                source=EpisodeType.JSON,
                scope=USER_SCOPE,
                reference_time=WEEK2,
            )
        else:
            for name, body, scope in [
                ("week2-customer", WEEK2_CUSTOMER_OFFLINE, CUSTOMER_SCOPE),
                ("week2-user-pref", WEEK2_USER_TEMP_OFFLINE, USER_SCOPE),
            ]:
                await client.add_episode(
                    name=name, episode_body=body, source=EpisodeType.JSON, scope=scope, reference_time=WEEK2
                )
            bullet("▸ add_episode ×2  (compliance update, temporary preference)")

        blank()
        bullet("Dreaming...")
        await dream(client, now=WEEK2 + timedelta(hours=1))

        await print_memories(client, CUSTOMER_SCOPE, "Pinnacle Events Co", as_of=WEEK2 + timedelta(hours=1))
        await print_memories(client, USER_SCOPE, "Priya Sharma", as_of=WEEK2 + timedelta(hours=1))
        blank()
        bullet("Note: Priya's phone preference carries an expiry — it will vanish from current search after May 13.")
        await print_truth_timeline(client, CUSTOMER_SCOPE)

        blank()
        section("Week 2 checks")
        w2_cust = await active_facts(client, CUSTOMER_SCOPE, WEEK2 + timedelta(hours=1))
        w2_user = await active_facts(client, USER_SCOPE, WEEK2 + timedelta(hours=1))
        found2 = _requires_slot(client, CUSTOMER_SCOPE)
        w2_tl = (
            await client.truth_timeline(
                scope=CUSTOMER_SCOPE, subject=found2[0], predicate=found2[1], relationship_type="REQUIRES"
            )
            if found2
            else []
        )
        # Live: LLM may extract multiple REQUIRES in Week 1 so Week 2 timeline can have >2 entries
        chk.check(len(w2_tl) >= 2, "truth_timeline has ≥2 entries after Week 2 (superseded + active)", advisory=True)
        chk.check(any(e.is_current for e in w2_tl), "truth_timeline has a current entry")
        chk.check(any(not e.is_current for e in w2_tl), "truth_timeline has a superseded entry", advisory=True)
        chk.check(
            any(f.valid_to is not None for f in w2_user),
            "Priya has a temporary preference with valid_to set",
            advisory=True,
        )
        chk.check(
            any("liability waiver" in f.fact.lower() for f in w2_cust),
            "customer REQUIRES now mentions liability waiver",
            advisory=True,
        )
        req_count = sum(1 for f in w2_cust if f.relationship_type == "REQUIRES")
        chk.check(req_count == 1, "customer still has exactly 1 active REQUIRES (old superseded)")

        # ── WEEK 3: Requirements finalized ────────────────────────────────────────

        section(f"WEEK 3 · Requirements finalized  (May 15)  {sdk_label('add_session')}")
        if live:
            r3 = await client.add_session(
                name="week3-correction",
                turns=WEEK3_TURNS,
                scope=CUSTOMER_SCOPE,
                turns_per_episode=len(WEEK3_TURNS),
                time_gap_seconds=None,
            )
            bullet(f"▸ add_session: {r3.turns_ingested} turns → {r3.episodes_created} episode(s)")
        else:
            await client.add_episode(
                name="week3-customer",
                episode_body=WEEK3_CUSTOMER_OFFLINE,
                source=EpisodeType.JSON,
                scope=CUSTOMER_SCOPE,
                reference_time=WEEK3,
            )
            bullet("▸ add_episode (compliance finalized)")

        blank()
        bullet("Dreaming...")
        await dream(client, now=WEEK3 + timedelta(hours=1))

        await print_memories(client, CUSTOMER_SCOPE, "Pinnacle Events Co", as_of=WEEK3 + timedelta(hours=1))
        await print_memories(client, USER_SCOPE, "Priya Sharma (travel week expired)", as_of=WEEK3 + timedelta(hours=1))

        blank()
        bullet("Full compliance requirement history (three versions):")
        await print_truth_timeline(client, CUSTOMER_SCOPE)

        blank()
        section("Week 3 checks")
        w3_user = await active_facts(client, USER_SCOPE, WEEK3 + timedelta(hours=1))
        found3 = _requires_slot(client, CUSTOMER_SCOPE)
        w3_tl = (
            await client.truth_timeline(
                scope=CUSTOMER_SCOPE, subject=found3[0], predicate=found3[1], relationship_type="REQUIRES"
            )
            if found3
            else []
        )
        chk.check(len(w3_tl) >= 3, "truth_timeline has ≥3 entries after Week 3 (full lineage)", advisory=True)
        chk.check(sum(1 for e in w3_tl if e.is_current) == 1, "exactly 1 current entry in timeline")
        chk.check(
            not any("phone" in f.fact.lower() for f in w3_user),
            "Priya's phone preference has expired from current view after WEEK3",
        )
        chk.check(
            any("W9" in f.fact for f in await active_facts(client, CUSTOMER_SCOPE, WEEK3 + timedelta(hours=1))),
            "customer REQUIRES now mentions W9",
            advisory=True,
        )

        # ── WEEK 4: Shadow document + bulk episodes + reinforcement ───────────────

        section(f"WEEK 4 · Shadow document + reinforcement  (May 22)  {sdk_label('add_context · add_episode_bulk')}")

        ctx_result = await client.add_context(
            name="pinnacle-account-settings-v3",
            content=SHADOW_DOCUMENT,
            scopes=[CUSTOMER_SCOPE, AGENT_SCOPE],
            custom_id="pinnacle-settings-2026-05-22",
            source_description="shadow account settings",
            metadata={"source": "crm_export", "version": "3"},
            reference_time=WEEK4,
            max_chars_per_episode=600,
        )
        bullet(
            f"▸ add_context: {ctx_result.chunks_created} chunk(s)  {ctx_result.episodes_created} episode(s)  "
            f"scopes={ctx_result.scope_keys}  id={ctx_result.document_id[:8]}…"
        )

        bulk_results = await client.add_episode_bulk(BULK_AGENT_LESSONS)
        bullet(
            f"▸ add_episode_bulk: {len(bulk_results)} episodes queued  "
            f"({', '.join(r.episode_uuid[:6] + '…' for r in bulk_results)})"
        )

        blank()
        bullet("Dreaming...")
        await dream(client, now=WEEK4 + timedelta(hours=1))

        await print_memories(client, AGENT_SCOPE, "Group Sales Agent (post-bulk)", as_of=WEEK4 + timedelta(hours=1))
        await print_memories(
            client, CUSTOMER_SCOPE, "Pinnacle Events Co (post-context)", as_of=WEEK4 + timedelta(hours=1)
        )

        blank()
        bullet("Reinforcement check — headcount fact observed twice, confidence and count updated:")
        agent_results = await client.search(query="headcount", scope=AGENT_SCOPE, limit=5)
        if agent_results:
            r = agent_results[0]
            bullet(f"  observed_count={r.observed_count}  confidence={r.confidence:.2f}  fact={r.fact}", indent=4)

        blank()
        section("Week 4 checks")
        chk.check(ctx_result.chunks_created >= 1, "add_context produced ≥1 chunk")
        chk.check(ctx_result.episodes_created >= 2, "add_context created ≥2 episodes (multi-scope)")
        chk.check(len(ctx_result.scope_keys) == 2, "add_context covered exactly 2 scopes")
        chk.check(len(bulk_results) == 2, "add_episode_bulk queued exactly 2 episodes")
        chk.check(all(r.queued_for_dreaming for r in bulk_results), "all bulk episodes queued for dreaming")
        headcount_facts = [r for r in agent_results if "headcount" in r.fact.lower()]
        chk.check(bool(headcount_facts), "headcount SHOULD fact found in agent scope")
        if headcount_facts:
            # Advisory in live: LLM may phrase the headcount SHOULD differently from the bulk
            # JSON body so the truth_keys don't match and reinforcement doesn't fire.
            chk.check(
                headcount_facts[0].observed_count >= 2,
                f"headcount fact reinforced: observed_count={headcount_facts[0].observed_count} ≥ 2",
                advisory=True,
            )

        # ── WEEK 5: Streaming session + forced consolidation ──────────────────────

        section(f"WEEK 5 · Streaming session + consolidation  (May 29)  {sdk_label('open_session · run_dream_job')}")

        async with client.open_session(
            name="week5-streaming",
            scope=CUSTOMER_SCOPE,
            turns_per_episode=10,
            time_gap_seconds=None,
            metadata={"channel": "phone", "week": 5},
        ) as session:
            for turn in WEEK5_TURNS:
                await session.add_turn(turn.role, turn.content, timestamp=turn.timestamp)
            # Manual flush mid-session to demonstrate the streaming API
            mid_flush = await session.flush()
            bullet(f"▸ open_session: mid-session flush → {len(mid_flush)} episode(s) emitted")
        bullet("  session closed (context manager exit) — remaining turns auto-flushed")

        blank()
        bullet("Dreaming (formation + pruning, cadence-triggered)...")
        await dream(client, now=WEEK5 + timedelta(hours=1))

        blank()
        bullet(
            f"Forcing consolidation  {sdk_label('run_dream_job')}  (cadence=999,999s — never triggers automatically)"
        )
        consol_result = await client.run_dream_job(job_name="consolidation", now=WEEK5 + timedelta(hours=2))
        for run in consol_result.job_runs:
            bullet(
                f"  consolidation  scopes={run.processed_scopes}  created={run.created_relationships}  decisions={run.decision_count}"
            )

        await print_memories(
            client, AGENT_SCOPE, "Group Sales Agent (post-consolidation)", as_of=WEEK5 + timedelta(hours=3)
        )

        blank()
        section("Week 5 checks")
        chk.check(len(mid_flush) == 1, "open_session mid-flush emitted exactly 1 episode")
        chk.check(consol_result.processed_scopes >= 1, "forced consolidation processed ≥1 scope")
        chk.check(consol_result.decision_count >= 1, "consolidation recorded ≥1 dream-agent decision")
        w5_agent = await active_facts(client, AGENT_SCOPE, WEEK5 + timedelta(hours=3))
        chk.check(len(w5_agent) >= 2, "agent has ≥2 active SHOULD facts after consolidation")

        # ── WEEK 6: Operator corrections ─────────────────────────────────────────

        section(f"WEEK 6 · Operator actions  (Jun 2)  {sdk_label('correct_memory · forget_memory · memory_evidence')}")

        # Find the current REQUIRES fact to correct
        req_results = await client.search(query="site visit", scope=CUSTOMER_SCOPE, limit=1)
        if not req_results:
            req_results = await client.search(query="waiver", scope=CUSTOMER_SCOPE, limit=1)

        if req_results:
            blank()
            bullet("correct_memory: update current REQUIRES fact")
            bullet(f"  before: {req_results[0].fact}", indent=4)
            correction = await client.correct_memory(
                relationship_uuid=req_results[0].relationship_uuid,
                corrected_object="venue liability waiver, W9 form, and signed master services agreement before any WDW site visit",
                scope=CUSTOMER_SCOPE,
                confidence=0.99,
                reason="legal_added_msa_requirement",
                source_text="Legal confirmed the MSA must also be signed before any site visit.",
                metadata={"reviewed_by": "legal_counsel"},
                now=WEEK6,
            )
            bullet(f"  after:  {correction.corrected_fact}", indent=4)
            bullet(f"  lineage: corrected_relationship_uuid={correction.corrected_relationship_uuid[:8]}…", indent=4)
            bullet(f"           correction_episode_uuid={correction.correction_episode_uuid[:8]}…", indent=4)

            blank()
            await print_memory_evidence(
                client,
                correction.corrected_relationship_uuid,
                CUSTOMER_SCOPE,
                "corrected REQUIRES fact",
            )

        # Forget the headcount REQUIRES fact (headcount is now auto-confirmed)
        headcount_results = await client.search(query="headcount", scope=AGENT_SCOPE, limit=1)
        if headcount_results:
            blank()
            bullet("forget_memory: retire 'headcount confirmation' (now auto-confirmed via CRM)")
            bullet(f"  retiring: {headcount_results[0].fact}", indent=4)
            forgot = await client.forget_memory(
                relationship_uuid=headcount_results[0].relationship_uuid,
                scope=AGENT_SCOPE,
                reason="headcount_auto_confirmed_via_crm",
                now=WEEK6,
            )
            bullet(f"  status: {forgot.previous_status.value} → {forgot.status.value}", indent=4)
            bullet(f"  valid_to set: {forgot.valid_to}", indent=4)
            bullet("  (evidence episodes still accessible via memory_evidence())", indent=4)

        blank()
        section("Week 6 checks")
        if req_results:
            chk.check(correction.created_relationships >= 1, "correct_memory created ≥1 new relationship")
            chk.check(
                correction.superseded_relationships >= 1
                or correction.corrected_relationship_uuid != req_results[0].relationship_uuid,
                "correct_memory superseded or replaced the original REQUIRES",
            )
            chk.check(
                "MSA" in correction.corrected_fact or "master services" in correction.corrected_fact.lower() or live,
                "corrected fact contains MSA requirement",
                advisory=True,
            )
            ev = await client.memory_evidence(
                relationship_uuid=correction.corrected_relationship_uuid,
                scope=CUSTOMER_SCOPE,
            )
            chk.check(len(ev.episodes) >= 1, "memory_evidence for corrected fact has ≥1 source episode")
            chk.check(ev.status == RelationshipStatus.ACTIVE, "corrected fact is active in the graph", advisory=True)
        if headcount_results:
            chk.check(forgot.status == RelationshipStatus.PRUNED, "forget_memory marked headcount fact as pruned")
            chk.check(forgot.valid_to is not None, "forgot fact has valid_to set to forget time")
            forgotten_search = await client.search(query="headcount", scope=AGENT_SCOPE)
            chk.check(
                not any("headcount" in r.fact.lower() and "confirm" in r.fact.lower() for r in forgotten_search),
                "forgotten fact no longer appears in current search",
            )
        all_decisions = await client.dream_decisions(limit=50, agent_id="support-dream-agent")
        chk.check(len(all_decisions) >= 5, f"≥5 dream-agent decisions recorded ({len(all_decisions)} total)")

        # ── Final inspection ──────────────────────────────────────────────────────

        section(
            f"Final inspection  {sdk_label('memory_evolution · entity_neighborhood · search_context · dream_history · export_graph')}"
        )

        # Entity neighborhood
        found_slot = _requires_slot(client, CUSTOMER_SCOPE)
        entity_name = found_slot[0] if found_slot else "Pinnacle Events Co"
        await print_entity_neighborhood(client, CUSTOMER_SCOPE, entity_name)

        # Multi-scope search_context
        blank()
        await print_search_context(client, "site visit compliance documents")

        # Time-travel across all 6 weeks
        section("Time-travel search  (search with as_of across all weeks)")
        blank()
        query_entity = found_slot[0] if found_slot else "site visit"
        for label, as_of in [
            ("May 1  — initial", WEEK1 + timedelta(hours=2)),
            ("May 8  — compliance updated", WEEK2 + timedelta(hours=2)),
            ("May 15 — requirements finalized", WEEK3 + timedelta(hours=2)),
            ("Jun 2  — after MSA correction", WEEK6 + timedelta(hours=2)),
        ]:
            results = await client.search(query=query_entity, scope=CUSTOMER_SCOPE, as_of=as_of, include_statuses=None)
            req = next((r for r in results if "require" in r.predicate.lower()), None) or (
                results[0] if results else None
            )
            fact = req.object if req else "(none visible)"
            bullet(f"  {label:<38}  {fact}")

        # Memory evolution proof: cleaning/evolution vs raw recording
        proof_as_of = WEEK6 + timedelta(hours=3)
        proof = await print_memory_evolution_proof(client, CUSTOMER_SCOPE, proof_as_of)
        agent_proof = await client.memory_evolution(scope=AGENT_SCOPE, as_of=proof_as_of)
        blank()
        bullet(f"Agent-scope consolidation-derived memories: {agent_proof.consolidation_relationship_count}")

        section("Caller outcome proof  (before memory vs evolved memory)")
        blank()
        baseline_plan = (
            "Without memory:\n"
            "- Ask Priya to resend the current site-visit requirements.\n"
            "- Use a generic proposal package until customer preferences are rediscovered."
        )
        evolved_plan = await site_visit_action_plan(client, proof_as_of)
        print(baseline_plan)
        blank()
        print(evolved_plan)

        # Agent context
        await print_agent_context(client, [CUSTOMER_SCOPE, USER_SCOPE, AGENT_SCOPE])

        # Dream decisions
        await print_dream_decisions(client, limit=10)

        # Dream history
        section("Dream history  (dream_history())")
        history = await client.dream_history(limit=20)
        for record in history:
            bullet(
                f"  {record.job_name:<22} {record.job_kind.value:<14} "
                f"ep={record.processed_episodes}  created={record.created_relationships}  "
                f"superseded={record.superseded_relationships}  pruned={record.pruned_relationships}  "
                f"decisions={record.decision_count}"
            )

        # Graph summary
        section("Graph summary  (export_graph())")
        graph = client.export_graph()
        nodes = graph["nodes"]
        rels = graph["relationships"]
        status_counts: dict[str, int] = {}
        rel_type_counts: dict[str, int] = {}
        for r in rels:
            status_counts[r["properties"].get("status", "active")] = (
                status_counts.get(r["properties"].get("status", "active"), 0) + 1
            )
            rel_type_counts[r["type"]] = rel_type_counts.get(r["type"], 0) + 1
        blank()
        bullet(f"  Nodes          {len(nodes)}")
        bullet(f"  Relationships  {len(rels)} total")
        for k, v in sorted(status_counts.items()):
            bullet(f"    {k:<12} {v}", indent=4)
        bullet("  By type:")
        for k, v in sorted(rel_type_counts.items()):
            bullet(f"    {k:<12} {v}", indent=4)
        total_decisions = sum(r.decision_count for r in history)
        bullet(f"  Dream runs     {len(history)}")
        bullet(f"  Dream decisions  {total_decisions}")
        blank()

        # Final structural checks
        section("Final checks")
        neighborhood_edges = await client.entity_neighborhood(scope=CUSTOMER_SCOPE, entity=entity_name, limit=20)
        context_results = await client.search_context(
            query="site visit compliance", scopes=[CUSTOMER_SCOPE, USER_SCOPE, AGENT_SCOPE], limit=10
        )
        scope_kinds_hit = {r.scope.kind for r in context_results}

        chk.check(len(neighborhood_edges) >= 1, f"entity_neighborhood returned ≥1 edge for '{entity_name}'")
        chk.check(
            len(scope_kinds_hit) >= 2,
            f"search_context spans ≥2 scope kinds (got {[k.value for k in scope_kinds_hit]})",
            advisory=True,
        )
        chk.check(len(nodes) > 0, f"export_graph has nodes ({len(nodes)})")
        chk.check(len(rels) > 0, f"export_graph has relationships ({len(rels)})")
        chk.check(len(history) >= 6, f"dream_history has ≥6 run records ({len(history)})")
        chk.check(total_decisions >= 5, f"≥5 dream decisions recorded ({total_decisions})")
        active_rels = status_counts.get("active", 0)
        pruned_rels = status_counts.get("pruned", 0)
        chk.check(active_rels >= 3, f"graph has ≥3 active relationships ({active_rels})")
        chk.check(pruned_rels >= 1, f"graph has ≥1 pruned relationship ({pruned_rels}) — pruning fired")
        proof_signals = {signal.name: signal for signal in proof.signals}
        chk.check(
            proof.episode_count > proof.active_relationship_count,
            "memory_evolution shows raw episodes compressed into fewer active caller facts",
        )
        chk.check(
            proof.reinforced_relationship_count >= 1,
            "memory_evolution shows reinforcement instead of duplicate active facts",
            advisory=True,
        )
        chk.check(
            proof.superseded_relationship_count >= 1,
            "memory_evolution shows older truth superseded by current truth",
            advisory=True,
        )
        chk.check(
            proof.pruned_relationship_count >= 1,
            "memory_evolution shows expired/stale facts pruned from current retrieval",
        )
        chk.check(
            proof.inactive_relationship_count >= 1 and proof_signals["retrieval_filtering"].observed,
            "memory_evolution keeps inactive facts auditable but out of the caller profile",
        )
        chk.check(
            agent_proof.consolidation_relationship_count >= 1,
            "memory_evolution detects consolidation-derived agent lessons",
            advisory=True,
        )

        # WS-6 proof ledger checks — additive, guaranteed to pass in this simulation.
        # These extend the proof ledger with compression_ratio,
        # semantic_dedup_rate, per_type_active_counts, and rollup signal presence.
        proof_signal_names = {s.name for s in proof.signals}
        chk.check(
            "compression_ratio" in proof_signal_names,
            "WS-6 signal 'compression_ratio' present in memory_evolution proof",
        )
        chk.check(
            "semantic_dedup" in proof_signal_names,
            "WS-6 signal 'semantic_dedup' present in memory_evolution proof",
        )
        chk.check(
            "rollup_coverage" in proof_signal_names,
            "WS-6 signal 'rollup_coverage' present in memory_evolution proof",
        )
        chk.check(
            proof.compression_ratio >= 1.0,
            f"compression_ratio ≥ 1.0 (raw episodes / context-visible = {proof.compression_ratio:.2f}x)",
        )
        chk.check(
            isinstance(proof.per_type_active_counts, dict) and len(proof.per_type_active_counts) > 0,
            f"per_type_active_counts is populated ({proof.per_type_active_counts})",
        )
        chk.check(
            proof.context_visible_relationship_count == proof.active_relationship_count,
            (
                "context_visible_relationship_count equals active_relationship_count "
                "(no WS-4 demotions in this simulation — backward-compat invariant)"
            ),
        )

        chk.check(
            "master services" not in baseline_plan.lower(), "baseline no-memory plan lacks corrected MSA requirement"
        )
        chk.check(
            "master services" in evolved_plan.lower(),
            "evolved memory plan includes corrected MSA requirement",
            advisory=True,
        )
        chk.check(
            "indemnification" not in evolved_plan.lower(),
            "evolved memory plan excludes superseded indemnification-only requirement",
            advisory=True,
        )

        # 4-point time-travel coherence: each as_of should return a *different* REQUIRES fact
        tt_results = []
        for as_of in [
            WEEK1 + timedelta(hours=2),
            WEEK2 + timedelta(hours=2),
            WEEK3 + timedelta(hours=2),
            WEEK6 + timedelta(hours=2),
        ]:
            rs = await client.search(query=query_entity, scope=CUSTOMER_SCOPE, as_of=as_of, include_statuses=None)
            req = next((r for r in rs if "require" in r.predicate.lower()), None)
            tt_results.append(req.object if req else None)
        unique_snapshots = len({o for o in tt_results if o is not None})
        chk.check(
            unique_snapshots >= 3,
            f"time-travel search returns ≥3 distinct REQUIRES facts across 4 snapshots ({unique_snapshots} unique)",
            advisory=True,
        )

        chk.summary()
        return chk.n_failed == 0


if __name__ == "__main__":
    live_mode = "--live" in sys.argv
    ok = asyncio.run(main(live=live_mode))
    sys.exit(0 if ok else 1)
