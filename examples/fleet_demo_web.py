"""
WDPR Memotron — Narrative Web Demo  (smart-memory story for a stakeholder pitch)

Drives ONE dated customer relationship (Pinnacle Events / Priya / WDW Group Sales)
through the REAL Memotron engine and snapshots the actual memory state after each
interaction.  The story is about how INTELLIGENT the memory is — it FORMS, UPDATES
(supersede / semantic-merge / expire), DREAMS (offline distillation of scattered
observations into a durable rollup + a synthesized insight), and EVOLVES (the agent
ends up smarter, anticipating the customer).

The captured timeline is embedded as JSON in a single self-contained, fully offline
HTML file (no CDN, no fetch, no build step) written to examples/fleet_demo.html.

  uv run examples/fleet_demo_web.py

Honesty: dialogue and the dream's synthesized insight SENTENCE are authored for
impact, but every structural memory fact, per-type count, supersession, semantic
merge, expiry, rollup, and demotion is REAL — captured live from the engine after
each step.  Cards and the hero chart render the real active facts and types.
"""

from __future__ import annotations

import asyncio
import json
import webbrowser
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

from memotron import (
    AgentMemoryPolicy,
    DedupPolicy,
    DreamAgentConfig,
    DreamConfig,
    DreamContextPolicy,
    DreamInstructionSet,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryBank,
    MemoryControlPlane,
    MemoryPrincipal,
    MemoryScope,
    Motive,
    NodeInstruction,
    PrincipalRole,
    ProfilePolicy,
    PromptPack,
    PruningPolicy,
    RelationshipInstruction,
    ScopeKind,
    ScopeMemoryPolicy,
    TenantMemoryPolicy,
)
from memotron.config import (
    RelationshipCardinality,
    RollupConsolidationPolicy,
    default_prompt_profiles,
)
from memotron.embedding import LocalEmbeddingTransport, cosine_similarity
from memotron.models import MemoryType, RelationshipStatus

# ── Tenant / scope / deterministic timeline ──────────────────────────────────────

TENANT = "Walt Disney World · Corporate & Group Sales"
TEST_TENANT_ID = "wdpr-demo"
TEST_AGENT_ID = "group-sales-agent"
TEST_PRINCIPAL_ID = "sales-rep-001"
TEST_PROMPT_PACK = "wdpr-support-pack"
TEST_MOTIVE = "wdpr-sales-memory"
SCOPE = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:pinnacle-events")
BLOCKED_SCOPE = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="wdw:confidential-board-retreat")

# A fixed authored timeline (no wall-clock; captured data is fully deterministic).
# The engine's reference times march forward in minutes; the human-facing dates per
# chapter are what the viewer sees.
T0 = datetime(2026, 4, 20, 9, 0, tzinfo=UTC)


def at(minutes: int) -> datetime:
    return T0 + timedelta(minutes=minutes)


# Memory-type order for the stacked-area chart bands (raw observation bands first,
# rollup last so it reads as "everything distilling up into the rollup").
TYPE_ORDER = [
    "anchor",
    "requirement",
    "preference",
    "directive",
    "state",
    "decision",
    "incident",
    "rollup",
]

# Stakeholder-friendly band labels.
TYPE_LABEL = {
    "anchor": "Anchor",
    "requirement": "Requirement",
    "preference": "Preference",
    "directive": "Directive",
    "state": "State",
    "decision": "Decision",
    "incident": "Incident",
    "rollup": "Rollup (distilled)",
}


# ── Engine config: one customer scope, full type taxonomy, rollup consolidation ──


def build_config() -> DreamConfig:
    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Durable people, companies, events, documents, and concepts.",
                properties=("kind", "role", "tier", "region", "system"),
                strict_properties=True,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Compliance requirements and approval gates.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Communication, format, and workflow preferences.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Operating lessons / behavioural observations the agent should apply.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="IS",
                source_label="Entity",
                target_label="Entity",
                query="Durable identity facts about the customer.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
                memory_type=MemoryType.ANCHOR,
            ),
            RelationshipInstruction(
                type="TRACKS",
                source_label="Entity",
                target_label="Entity",
                query="Episodic working state: bookings, visits, open items.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
                memory_type=MemoryType.STATE,
            ),
            RelationshipInstruction(
                type="DECIDED",
                source_label="Entity",
                target_label="Entity",
                query="Account decisions with supersession lineage.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.DECISION,
            ),
            RelationshipInstruction(
                type="HIT",
                source_label="Entity",
                target_label="Entity",
                query="Incidents and operational issues with root-cause lineage.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
                memory_type=MemoryType.INCIDENT,
            ),
        ),
    )

    agent = DreamAgentConfig(
        agent_id="group-sales-dream-agent",
        name="Group Sales Dream Agent",
        scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="wdw:group-sales-agent"),
        decision_policy="Approve formation, consolidation, and pruning that preserve evidence and temporal accuracy.",
    )

    return DreamConfig(
        instruction_sets=(instructions,),
        prompt_profiles=default_prompt_profiles(),
        jobs=(
            DreamJob(
                name="formation",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                agent=agent,
                context_policy=DreamContextPolicy(max_relationships=40),
            ),
            DreamJob(
                name="dream-consolidation",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=999_999,  # forced manually at the dream moment
                scope=SCOPE,
                agent=agent,
                rollup_consolidation=True,
                rollup_consolidation_policy=RollupConsolidationPolicy(
                    # 0.70 isolates the tightly-worded "tightens compliance before signing"
                    # observation cluster (min obs-obs ≈ 0.84) and excludes everything else
                    # (max obs-other ≈ 0.58) — a clean, single-cluster distillation.
                    cluster_threshold=0.70,
                    min_cluster_size=3,
                    max_depth=1,
                ),
            ),
            DreamJob(
                name="pruning",
                kind=DreamJobKind.PRUNING,
                cadence_seconds=1,
                agent=agent,
            ),
        ),
        # Default 0.88: tight paraphrases (object cosine ≈ 0.93) reinforce one row;
        # genuinely different facts (cosine ≈ 0.4) stay separate.
        dedup=DedupPolicy(cosine_threshold=0.88),
        pruning=PruningPolicy(min_confidence=0.05),
        profile=ProfilePolicy(max_static_facts=40, max_dynamic_facts=20),
    )


def build_simulation_memory_bank() -> MemoryBank:
    return MemoryBank(
        motives=(
            Motive(
                name=TEST_MOTIVE,
                goal=(
                    "Build complete WDPR group-sales account memory across customer facts, "
                    "requirements, preferences, decisions, incidents, working state, and learned lessons."
                ),
                allowed_memory_types=(),
                dedup_threshold=0.88,
                retrieval_budget_share=1.0,
            ),
        )
    )


def build_simulation_control_plane(config: DreamConfig) -> MemoryControlPlane:
    return MemoryControlPlane(
        base_config=config,
        tenants=(
            TenantMemoryPolicy(
                tenant_id=TEST_TENANT_ID,
                name=TENANT,
                default_agent_id=TEST_AGENT_ID,
                default_scope=SCOPE,
                default_dream_mode="balanced",
                default_prompt_pack=TEST_PROMPT_PACK,
                default_motive=TEST_MOTIVE,
                memory_bank=build_simulation_memory_bank(),
                prompt_packs=(PromptPack(name=TEST_PROMPT_PACK, prompt_profile="support-memory"),),
                agents=(
                    AgentMemoryPolicy(
                        agent=DreamAgentConfig(
                            agent_id=TEST_AGENT_ID,
                            name="WDPR Group Sales Agent",
                            scope=MemoryScope(kind=ScopeKind.AGENT, scope_id="wdw:group-sales-agent"),
                            decision_policy=(
                                "Approve memory work that preserves source evidence, scope isolation, "
                                "temporal truth, and operator auditability."
                            ),
                        ),
                        default_scope=SCOPE,
                    ),
                ),
                scopes=(
                    ScopeMemoryPolicy(
                        scope=SCOPE,
                        dream_mode="balanced",
                        prompt_pack=TEST_PROMPT_PACK,
                        motive=TEST_MOTIVE,
                    ),
                    ScopeMemoryPolicy(
                        scope=BLOCKED_SCOPE,
                        dream_mode="audit",
                        prompt_pack=TEST_PROMPT_PACK,
                        motive=TEST_MOTIVE,
                    ),
                ),
            ),
        ),
    )


def build_simulation_principal() -> MemoryPrincipal:
    return MemoryPrincipal(
        principal_id=TEST_PRINCIPAL_ID,
        tenant_id=TEST_TENANT_ID,
        agent_id=TEST_AGENT_ID,
        default_scope=SCOPE,
        allowed_scope_keys={SCOPE.key},
        role=PrincipalRole.USER,
    )


LEARNING_EVENT = {
    "formed": "FORMATION: extract durable typed facts from the new episode.",
    "reinforced": "DEDUP: repeated fact strengthens the existing row instead of adding noise.",
    "superseded": "TRUTH UPDATE: single-active requirement replaces the older version and keeps lineage.",
    "merged": "SEMANTIC MERGE: paraphrase detected as the same fact and reinforced.",
    "expired": "TEMPORAL VALIDITY: stale temporary preference drops out of live context.",
    "dreamed": "DREAMING: offline consolidation distills scattered observations into a rollup.",
    "used": "RETRIEVAL: the agent answers from evolved scoped memory.",
}


def _policy_snapshot(policy, principal: MemoryPrincipal, *, event: str) -> dict:
    return {
        "event": event,
        "principal": {
            "id": principal.principal_id,
            "role": principal.role.value,
            "allowed_scopes": sorted(principal.effective_allowed_scope_keys()),
        },
        "tenant": {
            "id": policy.tenant_id,
            "name": TENANT,
        },
        "agent": {
            "id": policy.agent_id,
            "name": policy.agent.name,
        },
        "scope": {
            "key": policy.scope.key,
            "kind": policy.scope.kind.value,
        },
        "policy": {
            "dream_mode": policy.dream_mode.name,
            "enabled_jobs": [kind.value for kind in policy.enabled_job_kinds],
            "prompt_pack": policy.prompt_pack.name if policy.prompt_pack else "",
            "prompt_profile": f"{policy.prompt_profile}@{policy.prompt_profile_version}",
            "motive": policy.motive_name,
            "motive_goal": policy.motive.goal if policy.motive else "",
            "dedup_threshold": policy.dedup.cosine_threshold,
            "read_only": policy.read_only,
        },
        "source_trace": policy.source_trace,
    }


# ── Ingestion helpers ─────────────────────────────────────────────────────────────


def _memory_record(
    *,
    subject: str,
    predicate: str,
    object: str,
    relationship_type: str,
    confidence: float,
    valid_from: datetime,
    valid_to: datetime | None = None,
    subject_properties: dict | None = None,
) -> dict:
    rec: dict = {
        "subject": subject,
        "predicate": predicate,
        "object": object,
        "relationship_type": relationship_type,
        "confidence": round(confidence, 4),
        "valid_from": valid_from.isoformat(),
        "source_text": f"{subject} {predicate} {object}",
    }
    if valid_to is not None:
        rec["valid_to"] = valid_to.isoformat()
    if subject_properties:
        rec["subject_properties"] = subject_properties
    return rec


def _body(*records: dict) -> str:
    return json.dumps({"memories": list(records)})


# ── Live snapshot of REAL graph state ─────────────────────────────────────────────


def _active_rels(client: Memotron, *, visible_only: bool) -> list:
    out = []
    for rel in client.graph.relationships():
        if rel.type == "MENTIONS":
            continue
        props = rel.properties
        if props.get("scope_key") != SCOPE.key:
            continue
        if props.get("status") != RelationshipStatus.ACTIVE.value:
            continue
        if visible_only and props.get("active_in_context") is False:
            continue
        out.append(rel)
    return out


def _node_name(client: Memotron, uuid: str) -> str:
    try:
        return str(client.graph.get_node(uuid).properties.get("name", ""))
    except (ValueError, KeyError):
        return ""


def _valid_to(rel) -> datetime | None:
    """Validity-window end lives on the relationship attribute, not in properties."""
    return getattr(rel, "valid_to", None)


def _visible_active(client: Memotron, *, as_of: datetime) -> list:
    """Context-visible active rels whose validity window passes at as_of."""
    out = []
    for rel in _active_rels(client, visible_only=True):
        vt = _valid_to(rel)
        if vt is not None and vt <= as_of:
            continue
        out.append(rel)
    return out


async def _snapshot(
    client: Memotron,
    *,
    as_of: datetime,
    badges: dict[str, str] | None = None,
) -> dict:
    """Capture the REAL active memory state into a JSON-able snapshot.

    by_type:   type -> context-visible active count  (drives the stacked chart)
    facts:     context-visible active cards (type/text/status/observed_count/badge)
    history:   recently superseded / expired rows (struck-through "history" area)
    rollups:    active rollup facts
    """
    badges = badges or {}

    by_type: dict[str, int] = dict.fromkeys(TYPE_ORDER, 0)
    facts: list[dict] = []
    rollups: list[dict] = []

    for rel in _visible_active(client, as_of=as_of):
        mt = str(rel.properties.get("memory_type") or "")
        if mt not in by_type:
            by_type[mt] = 0
        by_type[mt] += 1
        obj = _node_name(client, rel.target_uuid) or str(rel.properties.get("object", ""))
        subj = _node_name(client, rel.source_uuid) or str(rel.properties.get("subject", ""))
        observed = int(rel.properties.get("observed_count", 1))
        card = {
            "type": mt,
            "subject": subj,
            "predicate": str(rel.properties.get("predicate", "")),
            "text": obj,
            "status": "active",
            "observed_count": observed,
            "badge": badges.get(rel.uuid, ""),
        }
        if mt == "rollup":
            card["derived_count"] = len(rel.properties.get("derived_from", []) or [])
            rollups.append(card)
        facts.append(card)

    # History area: superseded + expired rows for this scope (most recent first).
    history: list[dict] = []
    for rel in client.graph.relationships():
        if rel.type == "MENTIONS" or rel.properties.get("scope_key") != SCOPE.key:
            continue
        status = rel.properties.get("status")
        vt = _valid_to(rel)
        expired = status == RelationshipStatus.ACTIVE.value and vt is not None and vt <= as_of
        if status == RelationshipStatus.SUPERSEDED.value or expired:
            obj = _node_name(client, rel.target_uuid) or str(rel.properties.get("object", ""))
            history.append(
                {
                    "type": str(rel.properties.get("memory_type") or ""),
                    "text": obj,
                    "status": "expired" if expired else "superseded",
                }
            )

    # Order facts by the chart band order for stable card grouping.
    facts.sort(key=lambda f: TYPE_ORDER.index(f["type"]) if f["type"] in TYPE_ORDER else 99)

    return {
        "by_type": {t: by_type.get(t, 0) for t in TYPE_ORDER},
        "facts": facts,
        "rollups": rollups,
        "history": history[:6],
        "context_visible": len(_visible_active(client, as_of=as_of)),
    }


# ── Step model ──────────────────────────────────────────────────────────────────


@dataclass
class Step:
    chapter: str
    date: str
    persona: str
    scope_label: str
    conversation: list[dict] = field(default_factory=list)
    engine_action: str = "formed"  # formed/superseded/merged/reinforced/expired/dreamed/used
    callout: str = ""
    memory: dict = field(default_factory=dict)
    used: str = ""
    dream_insight: str = ""  # synthesized lesson (authored prose) for dream steps
    dream_distilled: int = 0  # REAL count of observations distilled into the rollup
    finale: dict | None = None  # memoryless-vs-evolved contrast payload
    control: dict = field(default_factory=dict)


# ── Scenario driver ───────────────────────────────────────────────────────────────


async def build_timeline(*, graph_path: Path | None = None) -> tuple[list[Step], dict]:
    config = build_config()
    control_plane = build_simulation_control_plane(config)
    principal = build_simulation_principal()
    policy = control_plane.resolve_for_principal(principal=principal, scope=SCOPE)
    blocked_error = ""
    try:
        control_plane.resolve_for_principal(principal=principal, scope=BLOCKED_SCOPE)
    except ValueError as exc:
        blocked_error = str(exc)
    steps: list[Step] = []

    graph_context = TemporaryDirectory() if graph_path is None else nullcontext(None)
    with graph_context as tmp:
        resolved_graph_path = Path(tmp) / "demo.sqlite" if graph_path is None else graph_path
        client = Memotron(
            graph_path=resolved_graph_path,
            control_plane=control_plane,
            embedding_transport=LocalEmbeddingTransport(),
        )

        async def form(name: str, *records: dict, t: int) -> None:
            await client.add_episode(
                name=name,
                episode_body=_body(*records),
                source=EpisodeType.JSON,
                scope=SCOPE,
                reference_time=at(t),
            )
            await client.run_dream_job(
                job_name="formation",
                now=at(t + 1),
                tenant_id=TEST_TENANT_ID,
                scope=SCOPE,
            )

        def find_uuid(needle: str) -> str:
            needle = needle.lower()
            for rel in _active_rels(client, visible_only=True):
                obj = (_node_name(client, rel.target_uuid) or str(rel.properties.get("object", ""))).lower()
                if needle in obj:
                    return rel.uuid
            return ""

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 1 — First contact: memory FORMS from natural conversation
        # ════════════════════════════════════════════════════════════════════════
        await form(
            "ch1-qualify",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="is",
                object="an enterprise corporate-retreat client (~400 guests, NA region)",
                relationship_type="IS",
                confidence=0.9,
                valid_from=at(0),
                subject_properties={"kind": "corporate client", "tier": "enterprise", "region": "NA"},
            ),
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="requires",
                object="a signed indemnification form before any WDW site visit",
                relationship_type="REQUIRES",
                confidence=0.95,
                valid_from=at(0),
            ),
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="prefers",
                object="all cost estimates packaged in one consolidated PDF",
                relationship_type="PREFERS",
                confidence=0.9,
                valid_from=at(0),
            ),
            t=0,
        )
        snap = await _snapshot(
            client,
            as_of=at(2),
            badges={
                u: "new"
                for u in [
                    find_uuid("enterprise corporate-retreat"),
                    find_uuid("indemnification form"),
                    find_uuid("consolidated PDF"),
                ]
                if u
            },
        )
        steps.append(
            Step(
                chapter="Ch. 1 — First contact",
                date="Mon, Apr 20 2026",
                persona="Priya Sharma · Pinnacle Events (Sr. Events Coordinator)",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "10:02",
                        "text": "Hi! I'm Priya from Pinnacle Events. We're planning a corporate retreat — about 400 people at WDW in September.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "10:03",
                        "text": "Wonderful, welcome! I'll set up your account. What should I know up front?",
                    },
                    {
                        "speaker": "Priya",
                        "time": "10:04",
                        "text": "Our legal team needs a signed indemnification form before any site visit. And please send all cost estimates as one consolidated PDF — our CFO reviews them together.",
                    },
                    {"speaker": "Agent", "time": "10:05", "text": "Noted on all of that."},
                ],
                engine_action="formed",
                callout="Formed from plain conversation: one identity fact, one requirement, one preference — each typed automatically.",
                memory=snap,
                used="Agent now knows: Pinnacle is an enterprise retreat client · requires a signed indemnification form before any site visit · prefers a single consolidated PDF.",
            )
        )

        # ── CHAPTER 1b — Priya restates the same preference: REINFORCE (not a duplicate)
        await form(
            "ch1b-reinforce",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="prefers",
                object="all cost estimates packaged in one consolidated PDF",
                relationship_type="PREFERS",
                confidence=0.93,
                valid_from=at(3),
            ),
            t=3,
        )
        pdf_uuid = find_uuid("consolidated PDF")
        pdf_observed = 1
        if pdf_uuid:
            pdf_observed = int(client.graph.get_relationship(pdf_uuid).properties.get("observed_count", 1))
        snap = await _snapshot(client, as_of=at(5), badges={pdf_uuid: "reinforced"} if pdf_uuid else {})
        steps.append(
            Step(
                chapter="Ch. 1 — Said again, more confidently",
                date="Thu, Apr 23 2026",
                persona="Priya Sharma · Pinnacle Events",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "16:20",
                        "text": "Reminder for your file — everything in ONE consolidated PDF, please. Our CFO won't review piecemeal.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "16:21",
                        "text": "Already on file — I'll just note you've reconfirmed it.",
                    },
                ],
                engine_action="reinforced",
                callout=(
                    f"Heard the same preference again — reinforced the existing row (observed ×{pdf_observed}) "
                    "instead of storing a second copy. Repetition makes the memory stronger, not bigger."
                ),
                memory=snap,
                used="Agent holds ONE consolidated-PDF preference, now with higher confidence because Priya has stated it twice.",
            )
        )

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 2 — Requirement genuinely changes: SUPERSEDE (history kept)
        # ════════════════════════════════════════════════════════════════════════
        await form(
            "ch2-risk-update",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="requires",
                object="a signed indemnification form AND a venue liability waiver before any WDW site visit",
                relationship_type="REQUIRES",
                confidence=0.96,
                valid_from=at(10),
            ),
            t=10,
        )
        snap = await _snapshot(
            client,
            as_of=at(12),
            badges={find_uuid("venue liability waiver"): "superseded"} if find_uuid("venue liability waiver") else {},
        )
        steps.append(
            Step(
                chapter="Ch. 2 — Risk team weighs in",
                date="Mon, Apr 27 2026",
                persona="Priya Sharma · Pinnacle Events",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "09:30",
                        "text": "Quick update — our risk team added a venue liability waiver on top of the indemnification form. Both are needed before any site visit.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "09:32",
                        "text": "Got it — I've updated the requirement. Indemnification form AND venue liability waiver before the site visit.",
                    },
                ],
                engine_action="superseded",
                callout="The truth changed, so the old requirement was superseded — not duplicated. The previous version is preserved in history with full lineage.",
                memory=snap,
                used="Agent now holds the CURRENT requirement (indemnification form + venue liability waiver) and can still explain what it replaced and when.",
            )
        )

        # ── CHAPTER 2b — Account DECISION (its own audited memory type)
        await form(
            "ch2b-decision",
            _memory_record(
                subject="Group Sales",
                predicate="decided",
                object="to assign Pinnacle a dedicated compliance concierge for the account",
                relationship_type="DECIDED",
                confidence=0.9,
                valid_from=at(15),
            ),
            t=15,
        )
        snap = await _snapshot(
            client,
            as_of=at(17),
            badges={find_uuid("compliance concierge"): "new"} if find_uuid("compliance concierge") else {},
        )
        steps.append(
            Step(
                chapter="Ch. 2 — A call worth remembering",
                date="Wed, Apr 29 2026",
                persona="Group Sales Agent (internal)",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Agent",
                        "time": "11:00",
                        "text": "Pinnacle's compliance bar is high — I'm assigning them a dedicated compliance concierge.",
                    },
                    {
                        "speaker": "System",
                        "time": "11:00",
                        "text": "Recorded as a decision with its own lineage, separate from customer-stated facts.",
                    },
                ],
                engine_action="formed",
                callout="Stored as a typed DECISION — high-audit, single-active, with full supersession lineage if it's ever revisited.",
                memory=snap,
                used="Agent's memory now distinguishes what the CUSTOMER said from what the TEAM decided — different types, different governance.",
            )
        )

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 3 — Same requirement, reworded: SEMANTIC MERGE (the smart beat)
        # ════════════════════════════════════════════════════════════════════════
        obj_a = "a certificate of insurance reviewed and approved by the risk team"
        obj_b = "a certificate of insurance reviewed and signed off by the risk team"
        embedder = LocalEmbeddingTransport()
        cos = round(cosine_similarity(embedder.embed(obj_a), embedder.embed(obj_b)), 2)
        await form(
            "ch3-coi-a",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="requires before contract signing",
                object=obj_a,
                relationship_type="REQUIRES",
                confidence=0.92,
                valid_from=at(20),
            ),
            t=20,
        )
        await form(
            "ch3-coi-b",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="requires before contract signing",
                object=obj_b,
                relationship_type="REQUIRES",
                confidence=0.93,
                valid_from=at(25),
            ),
            t=25,
        )
        coi_uuid = find_uuid("certificate of insurance")
        coi_observed = 1
        if coi_uuid:
            coi_observed = int(client.graph.get_relationship(coi_uuid).properties.get("observed_count", 1))
        snap = await _snapshot(
            client,
            as_of=at(27),
            badges={coi_uuid: "merged"} if coi_uuid else {},
        )
        steps.append(
            Step(
                chapter="Ch. 3 — Reworded, not new",
                date="Wed, May 6 2026",
                persona="Priya Sharma · Pinnacle Events",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "14:10",
                        "text": "Before we sign, we need the certificate of insurance reviewed and approved by your risk team.",
                    },
                    {"speaker": "Agent", "time": "14:11", "text": "Understood — logging that for contract signing."},
                    {
                        "speaker": "Priya",
                        "time": "14:40",
                        "text": "Sorry, to be precise: the certificate of insurance has to be reviewed and signed off by risk before we sign.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "14:41",
                        "text": "Same requirement, just worded differently — already captured.",
                    },
                ],
                engine_action="merged",
                callout=(
                    f"Recognized as the SAME requirement, reworded (cosine {cos:.2f} ≥ 0.88) — merged into one row, "
                    f"observed ×{coi_observed}. Exact-string memory would now be holding a duplicate."
                ),
                memory=snap,
                used="Agent holds ONE clean certificate-of-insurance requirement (reinforced, not duplicated) — its confidence in the fact rises each time it's restated.",
            )
        )

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 4 — Working state + a transient preference that will EXPIRE
        # ════════════════════════════════════════════════════════════════════════
        await form(
            "ch4-state",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="tracks",
                object="September retreat site visit confirmed for next Tuesday",
                relationship_type="TRACKS",
                confidence=0.82,
                valid_from=at(30),
            ),
            t=30,
        )
        await form(
            "ch4-temp-pref",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="prefers",
                object="phone-only contact this week while Priya is travelling",
                relationship_type="PREFERS",
                confidence=0.85,
                valid_from=at(31),
                valid_to=at(33),
            ),
            t=31,
        )
        # Snapshot BEFORE expiry: the temp preference is still visible.
        snap_before = await _snapshot(
            client,
            as_of=at(32),
            badges={
                u: b
                for u, b in [
                    (find_uuid("site visit confirmed"), "new"),
                    (find_uuid("phone-only contact"), "expiring"),
                ]
                if u
            },
        )
        steps.append(
            Step(
                chapter="Ch. 4 — A note with a shelf-life",
                date="Mon, May 11 2026",
                persona="Priya Sharma · Pinnacle Events",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "08:15",
                        "text": "The September site visit is confirmed for next Tuesday.",
                    },
                    {"speaker": "Agent", "time": "08:16", "text": "Tracked. Anything else this week?"},
                    {
                        "speaker": "Priya",
                        "time": "08:17",
                        "text": "I'm travelling all week — phone only please, no email until I'm back.",
                    },
                    {"speaker": "Agent", "time": "08:18", "text": "Will do — phone only for now."},
                ],
                engine_action="formed",
                callout="A durable working-state fact AND a transient preference — the phone-only note carries an expiry, so it's known to be temporary.",
                memory=snap_before,
                used="Agent knows the site visit is next Tuesday AND that this week is phone-only — and that the phone-only note is temporary.",
            )
        )

        # Now advance past the expiry window and snapshot again — temp pref drops out.
        snap_after = await _snapshot(client, as_of=at(40))
        steps.append(
            Step(
                chapter="Ch. 4 — …and it quietly expires",
                date="Mon, May 18 2026",
                persona="Group Sales Agent (one week later)",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Agent",
                        "time": "09:00",
                        "text": "Priya's back from travel — checking what's still true before I reach out.",
                    },
                    {
                        "speaker": "System",
                        "time": "09:00",
                        "text": "The phone-only preference reached its valid-until date and dropped out of the live profile on its own.",
                    },
                ],
                engine_action="expired",
                callout="The transient preference expired on schedule — no one had to remember to delete it. The agent won't act on stale instructions.",
                memory=snap_after,
                used="Agent's live context no longer includes phone-only — it's back to the customer's standing preferences.",
            )
        )

        # ── CHAPTER 4b — Something went wrong: an INCIDENT is remembered
        await form(
            "ch4b-incident",
            _memory_record(
                subject="Pinnacle Events Co",
                predicate="hit",
                object="documents were not ready on arrival at the last site visit, delaying check-in",
                relationship_type="HIT",
                confidence=0.88,
                valid_from=at(41),
            ),
            t=41,
        )
        snap = await _snapshot(
            client,
            as_of=at(43),
            badges={find_uuid("not ready on arrival"): "new"} if find_uuid("not ready on arrival") else {},
        )
        steps.append(
            Step(
                chapter="Ch. 4 — A miss worth not repeating",
                date="Wed, May 20 2026",
                persona="Group Sales Agent (post-visit note)",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Agent",
                        "time": "15:40",
                        "text": "Logging an issue: their documents weren't ready on arrival last visit and it delayed check-in.",
                    },
                    {
                        "speaker": "System",
                        "time": "15:40",
                        "text": "Captured as an incident — high-audit, kept so the pattern can inform future behaviour.",
                    },
                ],
                engine_action="formed",
                callout="A real operational miss is remembered as a typed INCIDENT — the raw material the agent will later dream over to get smarter.",
                memory=snap,
                used="Agent now remembers a concrete failure mode for this account: documents not ready on arrival.",
            )
        )

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 5 — DREAM (marquee): distill scattered observations → rollup + insight
        # ════════════════════════════════════════════════════════════════════════
        # Over weeks the agent has logged scattered notes that Pinnacle keeps adding
        # compliance docs the week before signing.  These form as 'directive' facts.
        observations = [
            ("documents", "spring"),
            ("requirements", "summer"),
            ("paperwork", "fall"),
            ("checks", "winter"),
            ("rules", "renewal"),
        ]

        async def form_observation(i: int, noun: str, season: str) -> None:
            await client.add_episode(
                name=f"ch5-obs-{i}",
                episode_body=_body(
                    _memory_record(
                        subject="Pinnacle Events Co",
                        predicate="should",
                        object=f"Pinnacle tightened compliance {noun} the week before signing the {season} contract",
                        relationship_type="SHOULD",
                        confidence=0.8 + 0.01 * i,
                        valid_from=at(45 + i),
                    )
                ),
                source=EpisodeType.JSON,
                scope=SCOPE,
                reference_time=at(45 + i),
            )

        # Accumulate the scattered observations across two visible frames so the
        # 'directive' band is seen CLIMBING before the dream collapses it.
        accumulation_frames = [
            (
                slice(0, 2),
                at(47),
                "Ch. 5 — Notes start to accumulate",
                "Mon, May 25 2026",
                [
                    {
                        "speaker": "Agent",
                        "time": "10:05",
                        "text": "Noticed Pinnacle tightened their compliance docs right before the spring signing — logging it.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "10:06",
                        "text": "Same thing happened ahead of the summer signing. Adding that too.",
                    },
                ],
            ),
            (
                slice(2, 5),
                at(52),
                "Ch. 5 — Scattered notes pile up",
                "Fri, May 29 2026",
                [
                    {
                        "speaker": "Agent",
                        "time": "17:30",
                        "text": "Another one — they tightened compliance again right before this signing.",
                    },
                    {
                        "speaker": "Agent",
                        "time": "17:31",
                        "text": "That's happened several times now. These notes are scattered all across the account.",
                    },
                ],
            ),
        ]
        directive_before = 0
        for sl, as_of, chap, date, convo in accumulation_frames:
            for i, (noun, season) in list(enumerate(observations))[sl]:
                await form_observation(i, noun, season)
            await client.run_dream_job(
                job_name="formation",
                now=as_of,
                tenant_id=TEST_TENANT_ID,
                scope=SCOPE,
            )
            snap_acc = await _snapshot(client, as_of=as_of)
            directive_before = snap_acc["by_type"].get("directive", 0)
            steps.append(
                Step(
                    chapter=chap,
                    date=date,
                    persona="Group Sales Agent (reviewing the account)",
                    scope_label="customer · wdw:pinnacle-events",
                    conversation=convo,
                    engine_action="formed",
                    callout=(
                        f"{directive_before} separate observations now sit in memory as raw notes — "
                        "true, but scattered and un-synthesized. No higher-level understanding yet."
                    ),
                    memory=snap_acc,
                    used="Agent can recall each individual note, but hasn't connected the dots into a pattern it can act on.",
                )
            )

        # THE DREAM: offline rollup consolidation distills the cluster into a rollup.
        await client.run_dream_job(
            job_name="dream-consolidation",
            now=at(53),
            tenant_id=TEST_TENANT_ID,
            scope=SCOPE,
        )
        rollup_rels = [
            r
            for r in _active_rels(client, visible_only=True)
            if r.properties.get("memory_type") == MemoryType.ROLLUP.value
        ]
        rollup_uuid = rollup_rels[0].uuid if rollup_rels else ""
        distilled = len(rollup_rels[0].properties.get("derived_from", []) or []) if rollup_rels else 0
        snap_dream = await _snapshot(
            client,
            as_of=at(54),
            badges={rollup_uuid: "insight"} if rollup_uuid else {},
        )
        directive_after = snap_dream["by_type"].get("directive", 0)
        rollup_after = snap_dream["by_type"].get("rollup", 0)
        steps.append(
            Step(
                chapter="Ch. 5 — 💤 The agent dreams",
                date="Sat, May 30 2026 · overnight",
                persona="Memotron · offline consolidation",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {"speaker": "System", "time": "02:14", "text": "💤 Dreaming… reviewing the account offline."},
                    {
                        "speaker": "System",
                        "time": "02:15",
                        "text": f"{distilled} scattered observations cluster around one idea. Collapsing them into a single rollup and synthesizing a lesson.",
                    },
                    {
                        "speaker": "Memotron",
                        "time": "02:16",
                        "text": "Woke up with something new: a pattern it was never explicitly told.",
                    },
                ],
                engine_action="dreamed",
                callout=(
                    f"Dreamt: distilled {distilled} scattered notes into ONE rollup — and synthesized a proactive INSIGHT "
                    "the agent was never directly told. The raw notes stay auditable underneath."
                ),
                memory=snap_dream,
                dream_insight=(
                    "Pinnacle always tightens compliance the week before they sign. "
                    "Don't wait for them — send the full compliance checklist early, before the signing window opens."
                ),
                dream_distilled=distilled,
                used="Agent now carries a single, durable rollup PLUS a new proactive rule it derived for itself: get ahead of Pinnacle's pre-signing compliance crunch.",
            )
        )

        # ════════════════════════════════════════════════════════════════════════
        # CHAPTER 6 — EVOLVE / USE: memoryless agent vs memory-evolved agent
        # ════════════════════════════════════════════════════════════════════════
        final_proof = await client.memory_evolution(scope=SCOPE, as_of=at(60))
        requirements = [f.object for f in final_proof.active_facts if f.memory_type == MemoryType.REQUIREMENT.value]
        preferences = [f.object for f in final_proof.active_facts if f.memory_type == MemoryType.PREFERENCE.value]
        snap_final = await _snapshot(client, as_of=at(60))

        evolved_lines = ["Welcome back, Priya — I've already prepared everything ahead of your signing:"]
        for r in requirements[:3]:
            evolved_lines.append(f"Sent the full compliance checklist early — including {r}.")
        if preferences:
            evolved_lines.append(f"Packaged the estimate the way you like it: {preferences[0]}.")
        evolved_lines.append("Held next Tuesday's site visit and pre-flagged the documents you'll need on arrival.")

        memoryless_lines = [
            "Hi! Thanks for reaching out about your event.",
            "Could you remind me what compliance documents your team requires?",
            "And how would you like estimates delivered — email, PDF, something else?",
            "Once you re-send those details we can get started.",
        ]

        steps.append(
            Step(
                chapter="Ch. 6 — The payoff",
                date="Mon, Jun 1 2026",
                persona="Priya Sharma · new inbound request",
                scope_label="customer · wdw:pinnacle-events",
                conversation=[
                    {
                        "speaker": "Priya",
                        "time": "11:00",
                        "text": "We're ready to move toward signing the September retreat. Where do we stand?",
                    },
                ],
                engine_action="used",
                callout="Same inbound message, two agents. The memory-evolved agent anticipates — applying the dreamed insight, the current requirements, and known preferences.",
                memory=snap_final,
                used="Agent answers from evolved memory: current requirements, the customer's format preference, AND the self-derived 'send the checklist early' rule.",
                finale={
                    "memoryless_title": "Generic agent (no memory)",
                    "memoryless": memoryless_lines,
                    "evolved_title": "Memory-evolved Memotron agent",
                    "evolved": evolved_lines,
                },
            )
        )

        for step in steps:
            step.control = _policy_snapshot(
                policy,
                principal,
                event=LEARNING_EVENT.get(step.engine_action, "MEMORY: scoped policy applied."),
            )

        meta = {
            "tenant": TENANT,
            "scope_label": "customer · wdw:pinnacle-events",
            "type_order": TYPE_ORDER,
            "type_label": TYPE_LABEL,
            "dream_steps": [i for i, s in enumerate(steps) if s.engine_action == "dreamed"],
            "dream_distilled": distilled,
            "directive_before_dream": directive_before,
            "directive_after_dream": directive_after,
            "rollup_after_dream": rollup_after,
            "control": _policy_snapshot(
                policy,
                principal,
                event="READY: logged-in principal resolved to one tenant, one agent, and one authorized customer scope.",
            ),
            "security_demo": {
                "blocked_scope": BLOCKED_SCOPE.key,
                "error": blocked_error,
            },
        }

    return steps, meta


# ── HTML emitter (single self-contained, fully offline file) ──────────────────────


def render_html(steps: list[Step], meta: dict) -> str:
    payload = json.dumps(
        {"steps": [asdict(s) for s in steps], "meta": meta},
        ensure_ascii=False,
        separators=(",", ":"),
    )

    # CSS — clean, modern, projector-friendly; no web-font CDN (system stack only).
    css = """
:root{
  --bg:#f6f7fb; --panel:#ffffff; --panel-2:#ffffff; --ink:#263043; --muted:#768197;
  --line:#e2e6ef; --accent:#155cff; --accent-2:#6f86d8; --good:#07845b; --warn:#b66b00;
  --bad:#c93655; --gold:#b77900;
  --c-identity:#2563eb; --c-requirement:#07845b; --c-preference:#7655c9;
  --c-directive:#c77700; --c-state:#0284c7; --c-decision:#b536c8; --c-incident:#d33b57;
  --c-rollup:#b77900;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{
  background:var(--bg);
  color:var(--ink); font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  -webkit-font-smoothing:antialiased;
}
.app{min-height:100%;padding:0 0 60px}
header.bar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;
  padding:18px 26px;background:#243f96;color:#fff;border-bottom:1px solid rgba(255,255,255,.08);margin-bottom:30px}
.brand{display:flex;align-items:center;gap:12px}
.logo{width:42px;height:42px;border-radius:9px;background:rgba(255,255,255,.12);
  display:flex;align-items:center;justify-content:center;font-size:21px;border:1px solid rgba(255,255,255,.15)}
.brand h1{font-size:18px;margin:0;letter-spacing:.2px}
.brand .tenant{color:#b9c7ea;font-size:12.5px;text-transform:uppercase;letter-spacing:.14em}
.headmeta{text-align:right;font-size:12.5px;color:#c4d0f0}
.headmeta .chap{color:#fff;font-weight:600;font-size:14px}
.headmeta .date{color:#dbe6ff}
.progress{height:5px;border-radius:4px;background:#dfe5f1;overflow:hidden;margin:0 auto 18px;max-width:1180px}
.progress > i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent-2));transition:width .35s ease}

.controlRail{max-width:1180px;margin:0 auto 18px;background:#fff;border:1px solid var(--line);
  border-radius:8px;box-shadow:0 1px 3px rgba(20,30,55,.08),0 8px 24px rgba(20,30,55,.04);overflow:hidden}
.controlTop{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:0;border-bottom:1px solid var(--line)}
.controlTile{padding:12px 14px;border-right:1px solid var(--line);min-width:0}
.controlTile:last-child{border-right:0}
.controlTile .k{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:#9aa3b5;font-weight:800;margin-bottom:4px}
.controlTile .v{font-size:14px;color:#263043;font-weight:700;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.controlTile .s{font-size:12px;color:#768197;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.cueStrip{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;padding:12px 14px;background:#fbfcff}
.cue{display:flex;gap:9px;align-items:flex-start;min-width:0}
.cue .dot{width:24px;height:24px;border-radius:6px;background:#eaf0ff;color:#155cff;
  display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:900;flex:0 0 auto}
.cue.blocked .dot{background:#fff0f3;color:#c93655}
.cue .ct{min-width:0}
.cue .ck{font-size:10.5px;text-transform:uppercase;letter-spacing:.1em;color:#9aa3b5;font-weight:900}
.cue .cv{font-size:12.5px;color:#344055;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.traceChips{display:flex;gap:6px;flex-wrap:wrap;padding:0 14px 12px;background:#fbfcff}
.traceChip{font-size:11px;border:1px solid #d9e1f3;background:#f6f8fd;color:#5b667a;border-radius:999px;padding:2px 7px}

.grid{display:grid;grid-template-columns:1.05fr 1.25fr;gap:16px;max-width:1180px;margin:0 auto}
.card{background:var(--panel);border:1px solid var(--line);
  border-radius:8px;padding:16px;box-shadow:0 1px 3px rgba(20,30,55,.08),0 8px 24px rgba(20,30,55,.04)}
.card h2{margin:0 0 12px;font-size:13px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);font-weight:700}

/* conversation */
.persona{color:var(--muted);font-size:12.5px;margin:-4px 0 12px}
.chat{display:flex;flex-direction:column;gap:10px;min-height:300px}
.bubble{max-width:86%;padding:9px 12px;border-radius:13px;border:1px solid var(--line);
  background:#f9fbff;animation:rise .35s ease both}
.bubble .who{font-size:11px;color:var(--muted);margin-bottom:2px;display:flex;gap:8px}
.bubble.priya{align-self:flex-start;border-color:#ccd6eb}
.bubble.priya .who{color:#31569a}
.bubble.agent{align-self:flex-end;background:#eef4ff;border-color:#c8d6fb}
.bubble.agent .who{color:#4a5faa;justify-content:flex-end}
.bubble.system,.bubble.memotron{align-self:center;max-width:94%;background:#f7f9fc;border-style:dashed;
  color:var(--muted);font-size:13.5px;text-align:center}
.bubble.memotron{color:#8b5f00;border-color:#e7c96b;background:#fff9e7}
@keyframes rise{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}

/* engine badge + callout */
.badgeRow{display:flex;align-items:center;gap:10px;margin:14px 0 6px;flex-wrap:wrap}
.ebadge{font-size:11.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;
  padding:4px 10px;border-radius:999px;border:1px solid var(--line)}
.ebadge.formed{color:#244a91;background:#eef4ff;border-color:#c8d6fb}
.ebadge.superseded{color:#8a4c00;background:#fff2df;border-color:#f0cc9d}
.ebadge.merged{color:#056647;background:#eafaf3;border-color:#bce9d4}
.ebadge.reinforced{color:#056647;background:#eafaf3;border-color:#bce9d4}
.ebadge.expired{color:#6d7485;background:#f2f4f8;border-color:#d9deea}
.ebadge.dreamed{color:#3c2900;background:linear-gradient(135deg,#ffe8a3,#ffd166);border-color:#e7c35c;
  box-shadow:0 0 0 0 rgba(255,209,102,.7);animation:pulseGold 1.6s ease-in-out infinite}
.ebadge.used{color:#263043;background:#edf2ff;border-color:#cad6ff}
@keyframes pulseGold{0%{box-shadow:0 0 0 0 rgba(255,209,102,.55)}70%{box-shadow:0 0 0 10px rgba(255,209,102,0)}100%{box-shadow:0 0 0 0 rgba(255,209,102,0)}}
.callout{font-size:13.5px;color:var(--ink);background:#f9fbff;border:1px solid var(--line);
  border-left:3px solid var(--accent);border-radius:9px;padding:10px 12px;margin-top:8px}
.callout.dreamed{border-left-color:var(--gold)}
.callout.merged{border-left-color:var(--good)}
.callout.superseded{border-left-color:var(--warn)}
.callout.expired{border-left-color:#8893b5}
.callout.used{border-left-color:var(--accent-2)}

/* memory cards */
#memWrap{min-height:300px}
.typegroup{margin-bottom:12px}
.typehead{display:flex;align-items:center;gap:8px;font-size:11.5px;letter-spacing:.06em;
  text-transform:uppercase;color:var(--muted);margin:0 0 6px;font-weight:700}
.swatch{width:10px;height:10px;border-radius:3px;display:inline-block}
.fact{border:1px solid var(--line);border-radius:8px;padding:9px 11px;margin:6px 0;background:#fff;
  position:relative;animation:rise .35s ease both}
.fact .ftext{font-size:14px}
.fact .fmeta{font-size:11.5px;color:var(--muted);margin-top:3px;display:flex;gap:10px;flex-wrap:wrap}
.fact .chip{font-size:10.5px;font-weight:800;text-transform:uppercase;letter-spacing:.06em;
  padding:2px 7px;border-radius:999px;position:absolute;top:-9px;right:9px}
.chip.new{background:#eef4ff;color:#244a91;border:1px solid #c8d6fb}
.chip.merged{background:#eafaf3;color:#056647;border:1px solid #bce9d4;animation:pulseGreen 1.4s ease 2}
.chip.superseded{background:#fff2df;color:#8a4c00;border:1px solid #f0cc9d}
.chip.expiring{background:#f2f4f8;color:#6d7485;border:1px solid #d9deea}
@keyframes pulseGreen{0%{box-shadow:0 0 0 0 rgba(54,211,153,.6)}70%{box-shadow:0 0 0 9px rgba(54,211,153,0)}100%{box-shadow:0 0 0 0 rgba(54,211,153,0)}}
.fact.rollup{border-color:#e2c66d;background:#fff9e8}
.fact.rollup .ftext{color:var(--gold);font-weight:600}

.insight{margin-top:8px;border:1px solid #e2c66d;border-radius:12px;padding:13px 14px;
  background:linear-gradient(135deg,#fff9e7,#fff3cc);box-shadow:0 8px 24px rgba(183,121,0,.12);
  animation:rise .5s ease both}
.insight .ihead{display:flex;align-items:center;gap:8px;color:var(--gold);font-weight:800;
  text-transform:uppercase;letter-spacing:.1em;font-size:11.5px;margin-bottom:6px}
.insight .ibody{font-size:15px;color:#4b3500;line-height:1.45}
.insight .isub{font-size:11.5px;color:#8b6b26;margin-top:6px}

.history{margin-top:10px;border-top:1px dashed var(--line);padding-top:9px}
.history .hh{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin-bottom:5px}
.history .hrow{font-size:12.5px;color:#7d88a8;text-decoration:line-through;opacity:.75;margin:2px 0}
.history .htag{text-decoration:none;display:inline-block;font-size:10px;text-transform:uppercase;
  letter-spacing:.06em;color:#9aa7c7;border:1px solid var(--line);border-radius:999px;padding:0 6px;margin-right:6px;opacity:.9}

/* dream overlay */
#dreamOverlay{position:fixed;inset:0;background:rgba(6,9,20,.78);backdrop-filter:blur(3px);
  display:none;align-items:center;justify-content:center;z-index:50}
#dreamOverlay.show{display:flex;animation:fade .3s ease}
.dreamCard{text-align:center;color:var(--gold)}
.dreamCard .zz{font-size:64px;animation:floatz 2.2s ease-in-out infinite}
.dreamCard .dl{font-size:20px;letter-spacing:.18em;text-transform:uppercase;margin-top:6px}
.dreamCard .dsub{color:#cbb577;font-size:13.5px;margin-top:8px;max-width:420px}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes floatz{0%,100%{transform:translateY(0)}50%{transform:translateY(-10px)}}

/* finale */
.finale{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:6px}
.fcol{border:1px solid var(--line);border-radius:12px;padding:13px}
.fcol.bad{background:#fff7fa;border-color:#f0c5d0}
.fcol.good{background:#f2f6ff;border-color:#c9d8ff;box-shadow:0 8px 24px rgba(21,92,255,.08)}
.fcol .ftitle{font-size:12px;text-transform:uppercase;letter-spacing:.08em;font-weight:800;margin-bottom:9px}
.fcol.bad .ftitle{color:#d49ab5}
.fcol.good .ftitle{color:#8fb6ff}
.fline{font-size:13.5px;margin:6px 0;padding-left:18px;position:relative}
.fcol.good .fline::before{content:"✓";position:absolute;left:0;color:var(--good);font-weight:800}
.fcol.bad .fline::before{content:"?";position:absolute;left:0;color:#d49ab5;font-weight:800}

/* chart */
.chartCard{margin:16px auto 0;max-width:1180px}
.chartCard h2{display:flex;justify-content:space-between;align-items:center}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:4px 0 10px}
.legend .li{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--muted)}
.legend .li.rollup{color:var(--gold);font-weight:700}
#chart{width:100%;height:230px;display:block}
.chartFoot{font-size:11.5px;color:var(--muted);margin-top:8px}
.usedStrip{margin:14px auto 0;max-width:1180px;background:#fff;border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.usedStrip .ul{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted);margin-bottom:5px}
.usedStrip .ub{font-size:14px;color:var(--ink)}

/* controls */
.controls{position:sticky;bottom:0;margin:18px auto 0;max-width:1180px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;
  background:linear-gradient(180deg,rgba(246,247,251,0),var(--bg) 40%);padding:14px 0 4px}
.btn{cursor:pointer;border:1px solid var(--line);background:#fff;color:var(--ink);font-size:14px;
  font-weight:600;padding:9px 16px;border-radius:10px;transition:transform .1s ease,background .15s}
.btn:hover{background:#f3f6ff}
.btn:active{transform:translateY(1px)}
.btn.primary{background:#155cff;color:#fff;border-color:#155cff}
.btn[disabled]{opacity:.4;cursor:not-allowed}
.counter{font-size:13px;color:var(--muted)}
.counter b{color:var(--ink)}
.spacer{flex:1}
.auto{display:flex;align-items:center;gap:7px;font-size:13px;color:var(--muted);cursor:pointer;user-select:none}
.auto input{accent-color:var(--accent)}
.hint{font-size:11.5px;color:var(--muted)}
@media (max-width:1100px){.controlTop{grid-template-columns:repeat(3,minmax(0,1fr))}.cueStrip{grid-template-columns:repeat(2,minmax(0,1fr))}.controlTile:nth-child(3){border-right:0}.controlTile:nth-child(n+4){border-top:1px solid var(--line)}}
@media (max-width:980px){.grid{grid-template-columns:1fr}.finale{grid-template-columns:1fr}.progress,.controlRail,.grid,.chartCard,.usedStrip,.controls{margin-left:14px;margin-right:14px}}
@media (max-width:680px){header.bar{padding:16px}.controlTop,.cueStrip{grid-template-columns:1fr}.controlTile{border-right:0;border-top:1px solid var(--line)}.controlTile:first-child{border-top:0}}
"""

    js = r"""
const DATA = window.__DEMO__;
const STEPS = DATA.steps, META = DATA.meta;
const TYPE_ORDER = META.type_order, TYPE_LABEL = META.type_label;
const COLORS = {identity:'#5b8cff',requirement:'#36d399',preference:'#7c6cff',directive:'#f5a524',
  state:'#38bdf8',decision:'#e879f9',incident:'#ff6b81',rollup:'#ffd166'};
let idx = 0, autoOn = false, autoTimer = null;
const $ = s => document.querySelector(s);
const esc = s => String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));

function speakerClass(sp){
  const s=(sp||'').toLowerCase();
  if(s==='priya'||s==='customer') return 'priya';
  if(s==='agent') return 'agent';
  if(s==='memotron') return 'memotron';
  return 'system';
}

function renderChat(step){
  const wrap=$('#chat'); wrap.innerHTML='';
  step.conversation.forEach((m,i)=>{
    const b=document.createElement('div');
    b.className='bubble '+speakerClass(m.speaker);
    b.style.animationDelay=(i*0.08)+'s';
    b.innerHTML='<div class="who"><span>'+esc(m.speaker)+'</span><span>'+esc(m.time||'')+'</span></div>'+esc(m.text);
    wrap.appendChild(b);
  });
  $('#badgeRow').innerHTML='<span class="ebadge '+esc(step.engine_action)+'">'+esc(step.engine_action)+'</span>';
  $('#callout').className='callout '+esc(step.engine_action);
  $('#callout').textContent=step.callout;
}

function renderMemory(step){
  const wrap=$('#memWrap'); wrap.innerHTML='';
  const mem=step.memory||{};
  const groups={};
  (mem.facts||[]).forEach(f=>{(groups[f.type]=groups[f.type]||[]).push(f);});

  TYPE_ORDER.forEach(t=>{
    const facts=groups[t]; if(!facts||!facts.length) return;
    const g=document.createElement('div'); g.className='typegroup';
    g.innerHTML='<div class="typehead"><span class="swatch" style="background:'+COLORS[t]+'"></span>'+esc(TYPE_LABEL[t]||t)+' · '+facts.length+'</div>';
    facts.forEach(f=>{
      const d=document.createElement('div'); d.className='fact'+(t==='rollup'?' rollup':'');
      let chip='';
      if(f.badge==='new') chip='<span class="chip new">new</span>';
      else if(f.badge==='merged') chip='<span class="chip merged">same fact · merged</span>';
      else if(f.badge==='reinforced') chip='<span class="chip merged">reinforced ×'+f.observed_count+'</span>';
      else if(f.badge==='superseded') chip='<span class="chip superseded">updated</span>';
      else if(f.badge==='expiring') chip='<span class="chip expiring">expires soon</span>';
      const obs=f.observed_count>1?'<span>observed ×'+f.observed_count+'</span>':'';
      const der=(t==='rollup'&&f.derived_count)?'<span>distilled from '+f.derived_count+' notes</span>':'';
      d.innerHTML=chip+'<div class="ftext">'+esc(f.text)+'</div><div class="fmeta">'+obs+der+'</div>';
      g.appendChild(d);
    });
    wrap.appendChild(g);
  });

  if(step.dream_insight){
    const ins=document.createElement('div'); ins.className='insight';
    ins.innerHTML='<div class="ihead">✨ Synthesized insight — never explicitly told</div>'+
      '<div class="ibody">'+esc(step.dream_insight)+'</div>'+
      '<div class="isub">Derived offline by collapsing '+(step.dream_distilled||0)+' scattered observations into one rollup.</div>';
    wrap.appendChild(ins);
  }

  if(step.finale){
    const f=step.finale;
    const fin=document.createElement('div'); fin.className='finale';
    const col=(cls,title,lines)=>'<div class="fcol '+cls+'"><div class="ftitle">'+esc(title)+'</div>'+
      lines.map(l=>'<div class="fline">'+esc(l)+'</div>').join('')+'</div>';
    fin.innerHTML=col('bad',f.memoryless_title,f.memoryless)+col('good',f.evolved_title,f.evolved);
    wrap.appendChild(fin);
  }

  if(mem.history&&mem.history.length){
    const h=document.createElement('div'); h.className='history';
    h.innerHTML='<div class="hh">History (kept for audit · not in live context)</div>'+
      mem.history.map(r=>'<div class="hrow"><span class="htag">'+esc(r.status)+'</span>'+esc(r.text)+'</div>').join('');
    wrap.appendChild(h);
  }
}

// ── Stacked area chart (hand-rolled inline SVG) ──
function renderChart(){
  const svg=$('#chart');
  const W=svg.clientWidth||1000, H=230, padL=34, padR=14, padT=14, padB=26;
  const n=STEPS.length;
  const active=TYPE_ORDER.filter(t=>STEPS.some(s=>(s.memory.by_type||{})[t]>0));
  let maxTotal=1;
  STEPS.forEach(s=>{let tot=0;active.forEach(t=>tot+=(s.memory.by_type||{})[t]||0);maxTotal=Math.max(maxTotal,tot);});
  const x=i=> n<=1?padL:padL+(i*(W-padL-padR)/(n-1));
  const y=v=> padT+(H-padT-padB)*(1-v/maxTotal);
  const stacks=STEPS.map(s=>{let c=0;const o={};active.forEach(t=>{c+=(s.memory.by_type||{})[t]||0;o[t]=c;});return o;});
  let svgStr='';
  const gstep=Math.max(1,Math.ceil(maxTotal/5));
  for(let g=0;g<=maxTotal;g+=gstep){
    svgStr+='<line x1="'+padL+'" y1="'+y(g)+'" x2="'+(W-padR)+'" y2="'+y(g)+'" stroke="#26314f" stroke-width="1"/>'+
      '<text x="'+(padL-6)+'" y="'+(y(g)+4)+'" fill="#9aa7c7" font-size="10" text-anchor="end">'+g+'</text>';
  }
  for(let k=active.length-1;k>=0;k--){
    const t=active[k];
    const lower=i=> k===0?0:stacks[i][active[k-1]];
    let top='', bot='';
    for(let i=0;i<n;i++) top+=(i?' L':'M')+x(i)+' '+y(stacks[i][t]);
    for(let i=n-1;i>=0;i--) bot+=' L'+x(i)+' '+y(lower(i));
    const fill=COLORS[t];
    const op=t==='rollup'?0.95:0.82;
    svgStr+='<path d="'+top+bot+' Z" fill="'+fill+'" fill-opacity="'+op+'" stroke="'+fill+'" stroke-width="1"/>';
  }
  (META.dream_steps||[]).forEach(di=>{
    svgStr+='<line x1="'+x(di)+'" y1="'+padT+'" x2="'+x(di)+'" y2="'+(H-padB)+'" stroke="#ffd166" stroke-width="1.5" stroke-dasharray="4 3"/>'+
      '<text x="'+x(di)+'" y="'+(padT+12)+'" fill="#ffd166" font-size="14" text-anchor="middle">💤</text>';
  });
  svgStr+='<line x1="'+x(idx)+'" y1="'+padT+'" x2="'+x(idx)+'" y2="'+(H-padB)+'" stroke="#263043" stroke-width="1.5" opacity="0.85"/>';
  svgStr+='<circle cx="'+x(idx)+'" cy="'+(H-padB)+'" r="3.5" fill="#263043"/>';
  STEPS.forEach((s,i)=>{
    if(n>14 && i%2!==0 && i!==n-1) return;
    svgStr+='<text x="'+x(i)+'" y="'+(H-8)+'" fill="#9aa7c7" font-size="9" text-anchor="middle">'+(i+1)+'</text>';
  });
  svg.innerHTML=svgStr;
}

function renderLegend(){
  const active=TYPE_ORDER.filter(t=>STEPS.some(s=>(s.memory.by_type||{})[t]>0));
  $('#legend').innerHTML=active.map(t=>'<span class="li'+(t==='rollup'?' rollup':'')+'">'+
    '<span class="swatch" style="background:'+COLORS[t]+'"></span>'+esc(TYPE_LABEL[t]||t)+'</span>').join('');
}

function renderControl(step){
  const c = step.control || META.control || {};
  const principal = c.principal || {};
  const tenant = c.tenant || {};
  const agent = c.agent || {};
  const scope = c.scope || {};
  const policy = c.policy || {};
  const trace = c.source_trace || {};
  const security = META.security_demo || {};
  const jobs = (policy.enabled_jobs || []).join(' + ') || 'none';
  const prompt = [policy.prompt_pack, policy.prompt_profile].filter(Boolean).join(' · ');
  const motive = policy.motive || 'none';
  const blocked = security.error ? 'Blocked '+(security.blocked_scope || 'unauthorized scope') : 'No blocked scope sampled';
  const allowed = (principal.allowed_scopes || []).join(', ');
  $('#controlRail').innerHTML =
    '<div class="controlTop">'+
      '<div class="controlTile"><div class="k">Logged-in user</div><div class="v">'+esc(principal.id||'')+'</div><div class="s">'+esc(principal.role||'')+'</div></div>'+
      '<div class="controlTile"><div class="k">Test tenant</div><div class="v">'+esc(tenant.id||'')+'</div><div class="s">'+esc(tenant.name||'')+'</div></div>'+
      '<div class="controlTile"><div class="k">Authorized scope</div><div class="v">'+esc(scope.key||'')+'</div><div class="s">'+esc(allowed)+'</div></div>'+
      '<div class="controlTile"><div class="k">Active agent / mode</div><div class="v">'+esc(agent.id||'')+'</div><div class="s">'+esc(policy.dream_mode||'')+' · '+esc(jobs)+'</div></div>'+
      '<div class="controlTile"><div class="k">Prompt / Motive</div><div class="v">'+esc(prompt)+'</div><div class="s">'+esc(motive)+'</div></div>'+
    '</div>'+
    '<div class="cueStrip">'+
      '<div class="cue"><div class="dot">1</div><div class="ct"><div class="ck">Auth</div><div class="cv">'+esc(trace.scope_authorization||'resolved')+'</div></div></div>'+
      '<div class="cue"><div class="dot">2</div><div class="ct"><div class="ck">Policy</div><div class="cv">'+esc(policy.dream_mode||'')+' · '+esc(policy.prompt_pack||'')+'</div></div></div>'+
      '<div class="cue"><div class="dot">3</div><div class="ct"><div class="ck">Learning event</div><div class="cv">'+esc(c.event||'')+'</div></div></div>'+
      '<div class="cue blocked"><div class="dot">4</div><div class="ct"><div class="ck">Guardrail</div><div class="cv">'+esc(blocked)+'</div></div></div>'+
    '</div>'+
    '<div class="traceChips">'+
      '<span class="traceChip">dream_mode: '+esc(trace.dream_mode||'default')+'</span>'+
      '<span class="traceChip">prompt_pack: '+esc(trace.prompt_pack||'default')+'</span>'+
      '<span class="traceChip">motive: '+esc(trace.motive||'default')+'</span>'+
      '<span class="traceChip">principal: '+esc(trace.principal||principal.id||'')+'</span>'+
    '</div>';
}

function maybeDream(step){
  if(step.engine_action!=='dreamed') return;
  const ov=$('#dreamOverlay');
  $('#dreamSub').textContent='Distilling '+(step.dream_distilled||0)+' scattered observations into one rollup + a new insight…';
  ov.classList.add('show');
  setTimeout(()=>ov.classList.remove('show'),1500);
}

function render(){
  const step=STEPS[idx];
  $('#chap').textContent=step.chapter;
  $('#date').textContent=step.date;
  $('#stepCounter').innerHTML='Step <b>'+(idx+1)+'</b> / '+STEPS.length;
  $('#stepCounter2').innerHTML='Step <b>'+(idx+1)+'</b> / '+STEPS.length;
  $('#persona').textContent=step.persona+'  ·  '+step.scope_label;
  $('#progBar').style.width=((idx+1)/STEPS.length*100)+'%';
  renderControl(step);
  renderChat(step);
  renderMemory(step);
  $('#usedBody').textContent=step.used||'';
  renderLegend(); renderChart();
  $('#prev').disabled=idx===0;
  $('#next').disabled=idx===STEPS.length-1;
  maybeDream(step);
}

function go(d){const ni=idx+d; if(ni<0||ni>=STEPS.length) return; idx=ni; render();}
function setAuto(on){
  autoOn=on; $('#autoToggle').checked=on;
  if(autoTimer){clearInterval(autoTimer);autoTimer=null;}
  if(on){autoTimer=setInterval(()=>{ if(idx>=STEPS.length-1){setAuto(false);return;} go(1); },2500);}
}

window.addEventListener('keydown',e=>{
  if(e.key==='ArrowRight'||e.key===' '){e.preventDefault();go(1);}
  else if(e.key==='ArrowLeft'){e.preventDefault();go(-1);}
});
window.addEventListener('resize',renderChart);
$('#prev').addEventListener('click',()=>go(-1));
$('#next').addEventListener('click',()=>go(1));
$('#autoToggle').addEventListener('change',e=>setAuto(e.target.checked));
$('#tenant').textContent=META.tenant;
render();
"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<link rel="icon" href="data:,"/>
<title>Memotron — Smart Memory Demo</title>
<style>{css}</style>
</head>
<body>
<div class="app">
  <header class="bar">
    <div class="brand">
      <div class="logo">🧠</div>
      <div>
        <h1>Memotron</h1>
        <div class="tenant" id="tenant"></div>
      </div>
    </div>
    <div class="headmeta">
      <div class="chap" id="chap"></div>
      <div class="date" id="date"></div>
      <div id="stepCounter"></div>
    </div>
  </header>

  <div class="progress"><i id="progBar"></i></div>
  <div class="controlRail" id="controlRail"></div>

  <div class="grid">
    <section class="card">
      <h2>Conversation</h2>
      <div class="persona" id="persona"></div>
      <div class="chat" id="chat"></div>
      <div class="badgeRow" id="badgeRow"></div>
      <div class="callout" id="callout"></div>
    </section>

    <section class="card">
      <h2>Memory — live engine state</h2>
      <div id="memWrap"></div>
    </section>
  </div>

  <section class="card chartCard">
    <h2><span>Memory composition over time</span><span class="hint">knowledge accumulates → then distills</span></h2>
    <div class="legend" id="legend"></div>
    <svg id="chart" viewBox="0 0 1000 230" preserveAspectRatio="none"></svg>
    <div class="chartFoot">Each band is the count of active, context-visible memories of one type. At the 💤 dream step the raw <b>directive</b> notes collapse into the gold <b>rollup</b> band — the memory organizing and distilling itself.</div>
  </section>

  <section class="usedStrip">
    <div class="ul">How memory is used right now</div>
    <div class="ub" id="usedBody"></div>
  </section>

  <div class="controls">
    <button class="btn" id="prev">← Prev</button>
    <button class="btn primary" id="next">Next →</button>
    <span class="counter"><span id="stepCounter2"></span></span>
    <label class="auto"><input type="checkbox" id="autoToggle"/> Autoplay</label>
    <span class="spacer"></span>
    <span class="hint">Space / → next · ← prev</span>
  </div>
</div>

<div id="dreamOverlay">
  <div class="dreamCard">
    <div class="zz">💤</div>
    <div class="dl">Dreaming…</div>
    <div class="dsub" id="dreamSub"></div>
  </div>
</div>

<script>window.__DEMO__ = {payload};</script>
<script>{js}</script>
</body>
</html>
"""


# ── Main ──────────────────────────────────────────────────────────────────────────


async def main() -> None:
    steps, meta = await build_timeline()
    html = render_html(steps, meta)
    out_path = Path(__file__).resolve().parent / "fleet_demo.html"
    out_path.write_text(html, encoding="utf-8")

    chapters = []
    for s in steps:
        base = s.chapter.split(" — ")[0]
        if base not in chapters:
            chapters.append(base)

    print("Memotron narrative web demo written.")
    print(f"  steps captured : {len(steps)}")
    print(f"  chapters       : {len(chapters)}  ({', '.join(chapters)})")
    print(
        f"  dream step(s)  : {[i + 1 for i in meta['dream_steps']]}  "
        f"(distilled {meta['dream_distilled']} observations → 1 rollup)"
    )
    print(
        f"  pre/post dream : directive band {meta['directive_before_dream']} → "
        f"{meta['directive_after_dream']}, rollup band → {meta['rollup_after_dream']}"
    )
    print()
    print(f"  HTML file: {out_path}")
    print(f'  Open in your browser:  open "{out_path}"')

    try:
        webbrowser.open(out_path.as_uri())
    except Exception:
        pass  # headless / sandbox — printing the path is enough


if __name__ == "__main__":
    asyncio.run(main())
