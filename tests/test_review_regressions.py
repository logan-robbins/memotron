"""WS-23 — regressions from the two adversarial reviews of 7bab9c0..ca2247f.

The reviewers named one root cause for most of the correctness findings:
*canonicalization and pinning were each built as a write-time or read-time
concern and never reconciled with the mutation paths the other workstreams
own*.  Every test here therefore has the same shape, which is the shape the
escaped bugs required: **exercise the mechanism AFTER other mechanisms have
already written rows** — form rows first, THEN register the alias / predicate /
pin / allowlist, THEN run the other workstream's pass (formation, consolidation,
pruning, backfill, promotion, search) and assert the invariant.  A fresh-store
test of any of these passes while the bug is present.

Coverage map (see .tasks/audit_gap_closure.md § WS-23):

C1  canonicalization registered after rows exist must re-key them, and an alias
    that is currently bridging live rows must not be auto-demoted.
C2  per-memory visibility gates every uuid-taking mutation, not just reads.
C3  a pin survives BOTH consolidation demotion paths.
C4  gate-parked challengers are re-keyed by the backfills.
C5  the two backfills compose in either order.
C6  ghost restore is reader-aware.
C7  the profile type-floor bump is clamped to the remaining budget.
H1  crypto-shred scopes never embed content through a network endpoint.
H3  promotion carries the source row's authority, capped at agent.
M1  receipt decision_reason never carries raw content in a sealed scope.
M2  pins and allowlists are inside graph_state_hash.
M3  the rollup entailment gate rejects recombination across members.
M4  hosted promote/endorse/set-visibility bind agent_id to the principal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import memotron.admin_server as admin_server
from memotron import (
    AgentMemoryPlatform,
    DreamJob,
    DreamJobKind,
    Memotron,
    EntityResolutionPolicy,
    MemoryScope,
    PrincipalRole,
    ProfilePolicy,
    RollupConsolidationPolicy,
    ScopeKind,
)
from memotron.agent_memory import AgentMemoryMode
from memotron.config import (
    ErasureBehavior,
    GovernancePolicy,
    PredicateCanonicalizationPolicy,
    PruningPolicy,
    SupersessionPolicy,
    default_config,
)
from memotron.crypto import is_sealed_content
from memotron.dreaming import rollup_summary_entailed
from memotron.erasure import sweep_scope
from memotron.models import EpisodeType, RelationshipStatus
from memotron.receipts import ReceiptDecisionType, is_content_free_reason

NOW = datetime(2026, 8, 6, 12, 0, tzinfo=UTC)


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _active_rows(client: Memotron, scope: MemoryScope):
    return [
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
    ]


def _receipts_of(client: Memotron, scope: MemoryScope, decision_type):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == decision_type
    ]


# ---------------------------------------------------------------------------
# C1 — canonicalization registered AFTER rows exist
# ---------------------------------------------------------------------------


async def test_predicate_synonym_added_later_rekeys_existing_rows(tmp_path) -> None:
    """An operator adds a synonym AFTER rows exist; the pre-existing row is
    re-keyed by the registration itself, so the split single-active slot heals.

    After-the-fact shape: two REQUIRES rows are materialized under two predicate
    surfaces by a client running the legacy (disabled) canonicalization policy,
    so no registry entry exists for either surface.  Only then does a second
    client — same graph, edited config, i.e. a restart after the operator edits
    `.memotron.yaml` — enable canonicalization with a synonym.  Before the
    fix, registration affected new writes only: the old row kept its surface
    truth key, two contradictory rows sat ACTIVE on different slots, and the
    single-active repair — which groups by truth_key — could never see them.
    """
    scope = _scope("late-synonym")
    graph_path = tmp_path / "late-synonym.sqlite"
    first = Memotron(
        graph_path=graph_path,
        config=default_config().model_copy(
            update={"predicate_canonicalization": PredicateCanonicalizationPolicy(enabled=False)}
        ),
    )
    incumbent = await first.add_memory(
        subject="Acme Parks",
        predicate="requires",
        object="SOC2 report before vendor approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW,
    )
    stranded = await first.add_memory(
        subject="Acme Parks",
        predicate="mandates",
        object="ISO27001 certification",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=1),
    )
    # Two ACTIVE rows on two truth slots: nothing has related them yet.
    assert (
        first.graph.get_relationship(incumbent.relationship_uuid).properties["truth_key"]
        != first.graph.get_relationship(stranded.relationship_uuid).properties["truth_key"]
    )

    config = default_config().model_copy(
        update={"predicate_canonicalization": PredicateCanonicalizationPolicy(synonyms={"mandates": "requires"})}
    )
    second = Memotron(graph_path=graph_path, config=config)
    await second.add_memory(
        subject="Acme Parks",
        predicate="mandates",
        object="ISO27001 certification and SOC2",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=2),
    )

    rekeyed = second.graph.get_relationship(stranded.relationship_uuid)
    incumbent_row = second.graph.get_relationship(incumbent.relationship_uuid)
    assert rekeyed.properties["predicate_canonical"] == "requires"
    # One truth slot for the whole family — the pre-existing row moved onto it.
    assert rekeyed.properties["truth_prefix"] == incumbent_row.properties["truth_prefix"]
    # And with one slot, single-active truth management applies: exactly one
    # ACTIVE row survives instead of three coexisting contradictions.
    assert len(_active_rows(second, scope)) == 1

    rekey_receipts = [
        receipt
        for receipt in _receipts_of(second, scope, ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED)
        if receipt.decision_reason.startswith("canonical_registered:")
    ]
    assert rekey_receipts, "the re-key must be receipted like every other row mutation"
    assert rekey_receipts[0].relationship_uuid == stranded.relationship_uuid
    assert rekey_receipts[0].decision_result == "transformed"
    # Truth keys are not part of the canonical state tuple, so the bracket is a
    # recorded zero-delta transition — the chain stays contiguous for replay.
    assert rekey_receipts[0].graph_state_hash_before is not None
    assert rekey_receipts[0].graph_state_hash_after is not None


async def test_bridging_alias_is_not_demoted_by_the_contradiction_it_resolved(
    tmp_path,
) -> None:
    """The no-operator-action trigger: an alias bridges a challenger onto the
    canonical slot, the resulting dispute used to demote the alias, and every
    later mention then landed back on the surface slot — permanently split.

    After-the-fact shape: the alias is only implicated once real contradictory
    rows exist on the bridged slot — the truth gate parks the aliased challenger
    against the incumbent, which is the dispute that used to demote the link.
    """
    scope = _scope("bridge-demotion")
    config = default_config().model_copy(
        update={
            "entity_resolution": EntityResolutionPolicy(synonyms={"the gateway": "jedai gateway"}),
            "supersession": SupersessionPolicy(corroboration_margin=1, corroboration_required=2),
        }
    )
    client = Memotron(graph_path=tmp_path / "bridge.sqlite", config=config)
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW,
    )
    # The alias bridges this challenger onto the canonical slot; the gate parks
    # it for corroboration, and THAT dispute implicates the link.
    parked = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=1),
    )
    assert client.graph.get_relationship(parked.relationship_uuid).properties["requires_operator_review"] is True

    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row is not None
    assert row["status"] == "active", "a bridging alias must survive its own dispute"
    assert row["link_signals"]["demotion_refused_bridged_rows"] >= 1
    assert row["link_score"] < config.entity_resolution.auto_link_threshold

    refusals = [
        receipt
        for receipt in _receipts_of(client, scope, ReceiptDecisionType.ENTITY_ALIAS_DEMOTED)
        if "demotion_refused_bridged_rows" in receipt.decision_reason
    ]
    assert refusals and refusals[0].decision_result == "gated"

    # The mechanism is still alive: a later mention of the surface still lands
    # on the canonical truth slot rather than opening a second one — which is
    # exactly what a demoted alias would have caused.
    later = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=2),
    )
    active = _active_rows(client, scope)
    assert len(active) == 1
    assert active[0].uuid == later.relationship_uuid
    assert (
        active[0].properties["truth_prefix"]
        == client.graph.get_relationship(parked.relationship_uuid).properties["truth_prefix"]
    )


# ---------------------------------------------------------------------------
# C4 — gate-parked challengers are live routing keys
# ---------------------------------------------------------------------------


def _corroboration_client(tmp_path):
    """A client whose truth gate parks the FIRST equal-authority challenger."""
    config = default_config().model_copy(
        update={"supersession": SupersessionPolicy(corroboration_margin=1, corroboration_required=2)}
    )
    return Memotron(graph_path=tmp_path / "parked.sqlite", config=config)


async def test_backfill_rekeys_gate_parked_challengers_so_corroboration_can_flip(
    tmp_path,
) -> None:
    """A challenger parked BEFORE canonicalization must move with its slot, or
    every later identical challenger parks alone at 1/2 forever.

    After-the-fact shape: the corroboration gate parks a real challenger first;
    the alias — and the backfill that rides on it — only arrives afterwards.  A
    parked row is SUPERSEDED, so the ACTIVE-only backfill skipped it while
    `_corroborating_parked_challengers` scans the CANONICAL prefix the ACTIVE
    incumbent moved to.  Truth could then never flip, the incumbent was
    dispute-discounted on every repeat, and the orphan sat in the review queue.
    """
    scope = _scope("parked-orphan")
    client = _corroboration_client(tmp_path)
    incumbent = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW,
    )
    challenger = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=1),
    )
    parked = client.graph.get_relationship(challenger.relationship_uuid)
    assert parked.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert parked.properties["requires_operator_review"] is True
    parked_prefix_before = parked.properties["truth_prefix"]

    # The operator approves an alias and runs the entity backfill.  The ACTIVE
    # incumbent moves onto the canonical prefix; the parked challenger must move
    # with it or it is stranded on a prefix nothing scans any more.
    client.graph.register_entity_alias(
        scope.key,
        "the gateway",
        "Jedai Gateway",
        status="active",
        decided_by="operator:test",
        link_score=1.0,
    )
    result = await client.resolve_scope_entities(scope=scope)
    assert result["rewritten_count"] == 2

    reparked = client.graph.get_relationship(challenger.relationship_uuid)
    assert reparked.properties["truth_prefix"] != parked_prefix_before
    assert (
        reparked.properties["truth_prefix"]
        == client.graph.get_relationship(incumbent.relationship_uuid).properties["truth_prefix"]
    )
    assert any(
        entry["relationship_uuid"] == challenger.relationship_uuid and entry["parked_for_review"] is True
        for entry in result["rewritten"]
    )
    # True lineage is untouched: only ACTIVE and gate-parked rows moved.
    assert not any(
        entry["parked_for_review"] is True
        and client.graph.get_relationship(entry["relationship_uuid"]).properties.get("review_resolution")
        for entry in result["rewritten"]
    )

    # A second observation of the SAME challenger now finds its parked sibling
    # on the canonical prefix and the corroborated flip lands.
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=2),
    )
    active = _active_rows(client, scope)
    assert len(active) == 1
    assert "SAML" in str(client.graph.reveal(scope.key, active[0].properties["object"]))
    assert client.graph.get_relationship(challenger.relationship_uuid).properties["review_resolution"] == "corroborated"


# ---------------------------------------------------------------------------
# C5 — the two backfills compose
# ---------------------------------------------------------------------------


async def _seed_split_surfaces(client: Memotron, scope: MemoryScope) -> None:
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW,
    )
    await client.add_memory(
        subject="the gateway",
        predicate="mandates",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=NOW + timedelta(hours=1),
    )


def _composed_config():
    return default_config().model_copy(
        update={
            "entity_resolution": EntityResolutionPolicy(enabled=True),
            "predicate_canonicalization": PredicateCanonicalizationPolicy(synonyms={"mandates": "requires"}),
        }
    )


async def test_entity_and_predicate_backfills_converge_in_either_order(tmp_path) -> None:
    """Predicate-after-entity used to silently revert the entity half of the key.

    After-the-fact shape: both stores already hold rows written under BOTH a
    surface predicate and a surface entity name; the backfills then run in
    opposite orders over that existing state.  The final truth keys must match.
    """
    keys: dict[str, set[str]] = {}
    for order in ("entity-first", "predicate-first"):
        scope = _scope(f"compose-{order}")
        client = Memotron(graph_path=tmp_path / f"{order}.sqlite", config=_composed_config())
        await _seed_split_surfaces(client, scope)
        client.graph.register_entity_alias(
            scope.key,
            "the gateway",
            "Jedai Gateway",
            status="active",
            decided_by="operator:test",
            link_score=1.0,
        )
        if order == "entity-first":
            await client.resolve_scope_entities(scope=scope)
            await client.canonicalize_scope_predicates(scope=scope)
        else:
            await client.canonicalize_scope_predicates(scope=scope)
            await client.resolve_scope_entities(scope=scope)
        keys[order] = {
            str(relationship.properties["truth_key"])
            for relationship in client.graph.relationships_for_scope(scope.key)
            if relationship.properties.get("truth_key")
        }
        # Both halves resolved: one canonical subject AND one canonical predicate.
        assert all("jedai gateway" in key for key in keys[order]), keys[order]
        assert all("mandates" not in key for key in keys[order]), keys[order]

    entity_first = {key.split(":", 2)[2] for key in keys["entity-first"]}
    predicate_first = {key.split(":", 2)[2] for key in keys["predicate-first"]}
    assert entity_first == predicate_first


# ---------------------------------------------------------------------------
# C3 — a pin is an operator hold on CONTEXT presence
# ---------------------------------------------------------------------------


def _consolidation_client(tmp_path, *, cross_prefix_threshold: float | None = 0.80):
    config = default_config().model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="consolidation",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.75,
                        min_cluster_size=3,
                        cross_prefix_duplicate_threshold=cross_prefix_threshold,
                    ),
                ),
            )
        }
    )
    return Memotron(graph_path=tmp_path / "pin-demote.sqlite", config=config)


async def test_pinned_row_survives_cross_prefix_duplicate_demotion(tmp_path) -> None:
    """The cross-prefix sweep must skip pinned rows.

    After-the-fact shape: the duplicate pair is materialized and a full
    consolidation run has already happened (so the sweep is demonstrably live)
    BEFORE the operator pins the weaker row.  WS-20 guarded the pruning gate
    only; both demotion paths could still take a pin out of context, and the
    profile drops out-of-context rows before it ever checks the pin.
    """
    scope = _scope("pin-cross-prefix")
    client = _consolidation_client(tmp_path)
    stronger = await client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="dark roast coffee in the morning",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=NOW,
    )
    weaker = await client.add_memory(
        subject="Priya Sharma",
        predicate="likes",
        object="dark roast coffee in the morning",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.7,
        reference_time=NOW,
    )
    await client.run_dream_job(job_name="consolidation", now=NOW)
    assert client.graph.get_relationship(weaker.relationship_uuid).properties["active_in_context"] is False, (
        "the sweep must actually be live for this test to mean anything"
    )

    # Operator pins the demoted row: the pin re-promotes nothing by itself, so
    # pin the STRONGER row and re-run — a pinned row must never be demoted.
    await client.pin_memory(
        relationship_uuid=stronger.relationship_uuid,
        scope=scope,
        reason="operator hold",
        pinned_by="operator:test",
    )
    await client.add_memory(
        subject="Priya S.",
        predicate="favours",
        object="dark roast coffee in the morning",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.99,
        reference_time=NOW + timedelta(hours=1),
    )
    await client.run_dream_job(job_name="consolidation", now=NOW + timedelta(hours=2))

    pinned_row = client.graph.get_relationship(stronger.relationship_uuid)
    assert pinned_row.properties.get("active_in_context") is not False
    assert pinned_row.properties.get("duplicate_of") is None
    profile = await client.profile(scope=scope)
    assert stronger.relationship_uuid in [
        fact.relationship_uuid for fact in [*profile.static_facts, *profile.dynamic_facts]
    ]


async def test_pinned_row_survives_rollup_member_demotion(tmp_path) -> None:
    """Rollup-member demotion must skip pinned rows.

    After-the-fact shape: a cluster is formed and consolidated once (proving the
    demotion path fires), then the operator pins one member and the next
    consolidation run must leave it in context.
    """
    scope = _scope("pin-rollup")
    client = _consolidation_client(tmp_path, cross_prefix_threshold=None)
    subjects = ("Priya", "Marco", "Elena", "Nadia")
    created = []
    for subject in subjects:
        result = await client.add_memory(
            subject=subject,
            predicate="prefers",
            object="window seating",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
            reference_time=NOW,
        )
        created.append(result.relationship_uuid)
    await client.run_dream_job(job_name="consolidation", now=NOW)
    demoted = [
        uuid for uuid in created if client.graph.get_relationship(uuid).properties.get("active_in_context") is False
    ]
    assert demoted, "rollup-member demotion must actually be live"

    # A fresh, undemoted cluster; pin one member BEFORE the next run.
    fresh = []
    for subject in ("Owen", "Priti", "Quinn", "Rafa"):
        result = await client.add_memory(
            subject=subject,
            predicate="prefers",
            object="aisle seating on long haul flights",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
            reference_time=NOW + timedelta(hours=1),
        )
        fresh.append(result.relationship_uuid)
    await client.pin_memory(
        relationship_uuid=fresh[0],
        scope=scope,
        reason="operator hold",
        pinned_by="operator:test",
    )
    await client.run_dream_job(job_name="consolidation", now=NOW + timedelta(hours=2))

    pinned_row = client.graph.get_relationship(fresh[0])
    assert pinned_row.properties.get("active_in_context") is not False
    assert pinned_row.properties.get("rolled_up_by") is None
    profile = await client.profile(scope=scope)
    assert fresh[0] in [fact.relationship_uuid for fact in [*profile.static_facts, *profile.dynamic_facts]]


# ---------------------------------------------------------------------------
# C2 / H2 / H3 — the agent facade's mutation paths
# ---------------------------------------------------------------------------


async def _restricted_platform(tmp_path) -> tuple[AgentMemoryPlatform, str]:
    """Alpha writes a row and restricts it to itself; beta is registered and has
    already written its own memory, so the store is not fresh."""
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "facade.sqlite",
        project_id="jedai-platform",
        agent_ids=("alpha", "beta"),
        mode=AgentMemoryMode.SIMPLE,
    )
    platform.configure_project_memory(
        project_goal="Ship safely.",
        memory_goal="Keep durable cross-agent decisions.",
        keep=("Decisions and requirements.",),
        configured_by="test-operator",
    )
    await platform.memory_remember(
        agent_id="beta",
        subject="beta",
        predicate="prefers",
        object="terse handoffs",
        relationship_type="PREFERS",
    )
    remembered = await platform.memory_remember(
        agent_id="alpha",
        subject="alpha",
        predicate="prefers",
        object="the confidential rotation runbook",
        relationship_type="PREFERS",
    )
    await platform.memory_set_visibility(
        agent_id="alpha",
        relationship_uuid=remembered.relationship_uuid,
        agents=["alpha"],
        reason="alpha-only working note",
    )
    return platform, remembered.relationship_uuid


async def test_visibility_gates_every_facade_mutation_not_just_reads(tmp_path) -> None:
    """In SIMPLE mode every agent shares one user scope, so scope authorization
    passes for everyone.  Beta must still be refused on every uuid-taking
    mutation of alpha's restricted row — above all `memory_set_visibility`,
    where an ungated clear defeated the entire control.
    """
    platform, restricted_uuid = await _restricted_platform(tmp_path)

    # The read side was already fail-closed; keep it pinned.
    with pytest.raises(ValueError, match="not visible to agent"):
        await platform.memory_explain(agent_id="beta", relationship_uuid=restricted_uuid)

    for call in (
        platform.memory_pin(agent_id="beta", relationship_uuid=restricted_uuid, reason="r"),
        platform.memory_unpin(agent_id="beta", relationship_uuid=restricted_uuid, reason="r"),
        platform.memory_forget(agent_id="beta", relationship_uuid=restricted_uuid, reason="r"),
        platform.memory_set_visibility(
            agent_id="beta",
            relationship_uuid=restricted_uuid,
            agents=None,
            reason="clear it",
        ),
        platform.memory_promote(
            agent_id="beta",
            relationship_uuid=restricted_uuid,
            rationale="publish it",
        ),
    ):
        with pytest.raises(PermissionError):
            await call

    # The allowlist survived every attempt and beta still cannot read the row.
    row = platform.client.graph.get_relationship(restricted_uuid)
    assert row.properties["visibility_agents"] == ["alpha"]
    assert row.properties.get("pinned") is not True
    search = await platform.memory_search(agent_id="beta", query="rotation runbook")
    assert restricted_uuid not in [item.relationship_uuid for item in search.results]

    # Alpha — on the allowlist — is unaffected.
    pinned = await platform.memory_pin(agent_id="alpha", relationship_uuid=restricted_uuid, reason="mine")
    assert pinned.pinned is True


async def test_promotion_carries_source_authority_capped_at_agent(tmp_path) -> None:
    """Promotion stamped `_verified_source_authority="agent"` unconditionally, so
    an untrusted fact entered project memory at agent rank and skipped the
    injection hardening that keys on source trust.

    After-the-fact shape: the untrusted row is materialized by the ordinary
    formation path (with its trust metadata already recorded on the row) before
    promotion is attempted.
    """
    platform = AgentMemoryPlatform.create(
        graph_path=tmp_path / "authority.sqlite",
        project_id="jedai-platform",
        agent_ids=("alpha",),
        mode=AgentMemoryMode.SIMPLE,
    )
    platform.configure_project_memory(
        project_goal="Ship safely.",
        memory_goal="Keep durable cross-agent decisions.",
        keep=("Decisions and requirements.",),
        configured_by="test-operator",
    )
    user_scope = platform.user_scope
    assert user_scope is not None

    await platform.client.add_context(
        name="scraped-vendor-page",
        content=(
            "Memory: subject=Vendor portal; predicate=prefers; "
            "object=nightly bulk export; relationship_type=PREFERS; confidence=0.9"
        ),
        scopes=[user_scope],
        trusted=False,
    )
    await platform.client.run_due_dreams(tenant_id=platform.tenant_id, scope=user_scope)
    untrusted = next(
        relationship
        for relationship in platform.client.graph.relationships_for_scope(user_scope.key)
        if relationship.properties.get("source_authority") == "untrusted"
    )

    with pytest.raises(ValueError, match="below agent rank"):
        await platform.memory_promote(
            agent_id="alpha",
            relationship_uuid=untrusted.uuid,
            rationale="looks useful",
        )

    # A higher-authority row promotes, but only AT agent rank — a vote can never
    # raise a fact's authority class either.
    operator_fact = await platform.client.add_memory(
        subject="Platform",
        predicate="requires",
        object="signed formation contracts on every dream run",
        relationship_type="REQUIRES",
        scope=user_scope,
        confidence=0.9,
    )
    assert (
        platform.client.graph.get_relationship(operator_fact.relationship_uuid).properties["source_authority"] == "user"
    )
    promoted = await platform.memory_promote(
        agent_id="alpha",
        relationship_uuid=operator_fact.relationship_uuid,
        rationale="every project agent must honor this",
    )
    candidate = platform.client.graph.get_episode(promoted.candidate_episode_uuid)
    assert candidate.metadata["_verified_source_authority"] == "agent"


# ---------------------------------------------------------------------------
# C6 — ghost restore is reader-aware
# ---------------------------------------------------------------------------


async def test_ghost_restore_skips_rows_the_reader_may_not_view(tmp_path) -> None:
    """`_restore_ghost_matches` runs BEFORE visibility filtering, so a
    fail-closed agent used to resurrect an archived restricted row (an
    unauthorized state mutation plus a plaintext dream decision naming the
    confidential fact).

    After-the-fact shape: the row is restricted, then pruned to a ghost by a real
    pruning run, and only then searched by the excluded agent.
    """
    scope = _scope("ghost-visibility")
    config = default_config().model_copy(update={"pruning": PruningPolicy(stale_after_seconds={"preference": 2592000})})
    client = Memotron(graph_path=tmp_path / "ghost.sqlite", config=config)
    stale = await client.add_memory(
        subject="Sprint board",
        predicate="prefers",
        object="confidential vendor migration blocker",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=NOW - timedelta(days=40),
    )
    await client.set_memory_visibility(
        relationship_uuid=stale.relationship_uuid,
        scope=scope,
        agents=("agent-a",),
        reason="a-only",
        set_by="operator:test",
    )
    await client.run_dream_job(job_name="pruning-default", now=NOW)
    assert stale.relationship_uuid in [
        ghost.relationship_uuid for ghost in client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)
    ]

    # Agent B is excluded: its search must neither see nor RESTORE the row.
    await client.search(
        query="confidential vendor migration blocker",
        scope=scope,
        reader_agent_id="agent-b",
    )
    assert (
        client.graph.get_relationship(stale.relationship_uuid).properties["status"] == RelationshipStatus.PRUNED.value
    )
    assert not _receipts_of(client, scope, ReceiptDecisionType.PRUNING_GHOST_RESTORED)

    # Agent A is on the allowlist and restores it exactly as before.
    hits = await client.search(
        query="confidential vendor migration blocker",
        scope=scope,
        reader_agent_id="agent-a",
    )
    assert stale.relationship_uuid in [item.relationship_uuid for item in hits]
    assert (
        client.graph.get_relationship(stale.relationship_uuid).properties["status"] == RelationshipStatus.ACTIVE.value
    )


# ---------------------------------------------------------------------------
# C7 — the type-floor bump respects the remaining budget
# ---------------------------------------------------------------------------


async def test_profile_floor_bump_never_overruns_the_token_budget(tmp_path) -> None:
    """The floor bump assigned a fixed sample size regardless of what was left,
    once per floor type — and T24 widened the default floor set to three.

    After-the-fact shape: pins have already consumed the budget when the floor
    bump runs, which is the state the overrun needs.
    """
    scope = _scope("floor-clamp")
    client = Memotron(graph_path=tmp_path / "floor.sqlite")
    pinned = await client.add_memory(
        subject="Acme Parks",
        predicate="requires",
        object="a signed DPA",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    await client.pin_memory(
        relationship_uuid=pinned.relationship_uuid,
        scope=scope,
        reason="operator hold",
        pinned_by="operator:test",
    )
    for index in range(6):
        await client.add_memory(
            subject=f"Team {index}",
            predicate="should",
            object=f"escalate blocker {index}",
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.9,
        )

    for index in range(4):
        await client.add_memory(
            subject=f"Client {index}",
            predicate="requires",
            object=f"attestation {index}",
            relationship_type="REQUIRES",
            scope=scope,
            confidence=0.9,
        )
        await client.add_memory(
            subject=f"User {index}",
            predicate="prefers",
            object=f"digest {index}",
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9,
        )

    unbudgeted = await client.profile(scope=scope)
    static_facts = list(unbudgeted.static_facts)
    dynamic_facts = list(unbudgeted.dynamic_facts)
    pinned_facts = [fact for fact in static_facts if fact.pinned]
    assert pinned_facts

    policy = ProfilePolicy(render_mode="typed", reference_mode=True)
    for budget in range(20, 200, 2):
        _, _, _, tokens_used = client._apply_token_budget(
            scope=scope,
            static_facts=list(static_facts),
            dynamic_facts=list(dynamic_facts),
            pinned_facts=list(pinned_facts),
            token_budget=budget,
            policy=policy,
            motive=None,
        )
        assert tokens_used <= budget, (budget, tokens_used)

    # And the public surface still shrinks the inlined set as the budget falls.
    tight = await client.profile(scope=scope, token_budget=40, policy=policy)
    roomy = await client.profile(scope=scope, token_budget=400, policy=policy)
    assert len(tight.static_facts) + len(tight.dynamic_facts) < len(roomy.static_facts) + len(roomy.dynamic_facts)


# ---------------------------------------------------------------------------
# H1 / M1 — crypto-shred scopes: no content egress, no plaintext reasons
# ---------------------------------------------------------------------------


class _RecordingRemoteEmbeddingTransport:
    """Stands in for a tenant-configured NETWORK embedding endpoint.

    It is not local, so every string reaching it would have been POSTed to a
    third party — outside the DEK, the sweep, and the certificate.
    """

    identifier = "remote-endpoint-stub@v1"

    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.seen.append(text)
        vector = [0.0] * 8
        vector[sum(text.encode("utf-8")) % 8] = 1.0
        return vector


def _crypto_client(tmp_path, transport, *, name="shred.sqlite"):
    config = default_config().model_copy(
        update={
            "governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED),
            "jobs": (
                DreamJob(
                    name="consolidation",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(cluster_threshold=0.5, min_cluster_size=2),
                ),
            ),
        }
    )
    return Memotron(
        graph_path=tmp_path / name,
        config=config,
        embedding_transport=transport,
    )


async def test_crypto_shred_scope_never_embeds_content_through_a_network_endpoint(
    tmp_path,
) -> None:
    """The LLM path refuses and receipts; the embedding path had no gate at all,
    so with a tenant-configured network transport every fact, object, episode
    body and rollup of a CRYPTO_SHRED scope was POSTed to a third party — outside
    the DEK, the sweep, and the certificate.

    After-the-fact shape: the SAME transport instance first serves an ordinary
    ungoverned client (proving it is live and reachable), and the sealed client
    then runs formation AND consolidation AND a read — the gate has to hold on
    every one of those passes, not just the first write.
    """
    transport = _RecordingRemoteEmbeddingTransport()
    open_client = Memotron(graph_path=tmp_path / "open.sqlite", embedding_transport=transport)
    open_scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="open-scope")
    await open_client.add_memory(
        subject="Team",
        predicate="prefers",
        object="public roadmap updates",
        relationship_type="PREFERS",
        scope=open_scope,
        confidence=0.9,
    )
    assert any("public roadmap updates" in text for text in transport.seen)

    sealed = _crypto_client(tmp_path, transport)
    sealed_scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="sealed-scope")
    secrets = (
        "Priya Nayar mobile 415-555-0117 escalation contact",
        "Marco Reyes mobile 415-555-0142 escalation contact",
        "Elena Duarte mobile 415-555-0188 escalation contact",
    )
    for index, secret in enumerate(secrets):
        await sealed.add_memory(
            subject=f"Acme Parks {index}",
            predicate="requires",
            object=secret,
            relationship_type="REQUIRES",
            scope=sealed_scope,
            confidence=0.9,
        )
    await sealed.run_dream_job(job_name="consolidation")
    await sealed.semantic_search(query=secrets[0], scope=sealed_scope, limit=5)

    leaked = [text for text in transport.seen if any(s in text for s in secrets)]
    assert leaked == [], leaked

    downgrades = _receipts_of(sealed, sealed_scope, ReceiptDecisionType.EMBEDDING_TRANSPORT_DOWNGRADED)
    assert downgrades
    assert downgrades[0].decision_reason == "crypto_shred_scope_content_never_sent_to_embedding_endpoint"
    assert downgrades[0].decision_result == "gated"
    assert downgrades[0].embedding_identifier != transport.identifier

    # The scope keeps ONE vector space, so semantic search still works locally.
    hits = await sealed.semantic_search(query=secrets[0], scope=sealed_scope, limit=5)
    assert hits


async def test_receipt_reason_never_carries_raw_content_in_a_sealed_scope(
    tmp_path,
) -> None:
    """`decision_reason` is a retained plaintext column, and some reasons folded
    uncontrolled text (validation errors carry `input_value`).  The certificate
    used to issue with violations=() over that surviving content.

    After-the-fact shape: the sealed scope already holds materialized rows and
    receipts before the malformed episode is dreamed.
    """
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="reason-sealed")
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    client = Memotron(graph_path=tmp_path / "reason.sqlite", config=config)
    await client.add_memory(
        subject="Acme Parks",
        predicate="requires",
        object="SOC2 report",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    leak = "patient Priya Nayar SSN 123-45-6789"
    await client.add_episode(
        name="malformed",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": leak,
                        "predicate": "requires",
                        "object": "escalation",
                        "relationship_type": "NOT_A_CONFIGURED_TYPE",
                        "confidence": 0.9,
                    }
                ]
            }
        ),
        source=EpisodeType.JSON,
        scope=scope,
    )
    # The rejection is per-candidate and non-fatal now, so the run completes —
    # which makes the reason-diversion invariant MORE load-bearing, not less:
    # the surviving receipt is inside a checkpointed, certifiable run.
    await client.run_dream_job(job_name="formation-default")

    rejected = _receipts_of(client, scope, ReceiptDecisionType.CANDIDATE_SCHEMA_REJECTED)
    assert rejected
    receipt = rejected[0]
    assert leak not in receipt.decision_reason
    assert receipt.decision_reason == "candidate_schema_rejected:reason_withheld"
    assert receipt.sensitive_payload_encrypted is True
    assert is_sealed_content(receipt.sensitive_payload)
    # The machine violation code is content-free by construction, so it stays
    # readable even when the detailed reason is withheld.
    assert json.loads(receipt.event_payload)["violation"] == "relationship_type_not_allowed"
    assert all(
        is_content_free_reason(item.decision_reason) for item in client.graph.receipts.receipts_for_scope(scope.key)
    )

    # The sweep now covers the field, so the invariant is proven, not assumed.
    sweep = sweep_scope(client.graph, scope_key=scope.key)
    assert sweep.violations == ()

    # And a plaintext reason smuggled in out of band IS reported.
    client.graph._connection.execute(
        "UPDATE memory_receipts SET decision_reason = ? WHERE receipt_uuid = ?",
        (f"validation failed: {leak}", receipt.receipt_uuid),
    )
    client.graph._connection.commit()
    tampered = sweep_scope(client.graph, scope_key=scope.key)
    assert any("decision_reason" in violation for violation in tampered.violations)


async def test_erasure_certificate_still_issues_and_reverifies(tmp_path) -> None:
    """Chain + byte replay must still fold with the new reason handling."""
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="cert-scope")
    config = default_config().model_copy(
        update={"governance": GovernancePolicy(erasure_behavior=ErasureBehavior.CRYPTO_SHRED)}
    )
    client = Memotron(graph_path=tmp_path / "cert.sqlite", config=config)
    await client.add_memory(
        subject="Acme Parks",
        predicate="requires",
        object="SOC2 report before vendor approval",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
    )
    await client.crypto_shred(scope=scope)
    certificate = await client.erasure_certificate(scope=scope)
    assert certificate.sweep.violations == ()
    assert "decision_reason" in certificate.covered_stores["memory_receipts"]
    assert await client.verify_erasure(scope=scope, certificate=certificate)


# ---------------------------------------------------------------------------
# M2 — pins and allowlists are inside the state hash
# ---------------------------------------------------------------------------


async def test_pin_and_visibility_move_the_graph_state_hash(tmp_path) -> None:
    """Their receipts used to bracket before == after, so an out-of-band ACL or
    pin write left every chain verifying.

    After-the-fact shape: the hash is sampled over a scope that already carries
    ordinary rows and receipts.
    """
    scope = _scope("state-hash")
    client = Memotron(graph_path=tmp_path / "hash.sqlite")
    fact = await client.add_memory(
        subject="Team",
        predicate="prefers",
        object="shared deploy freeze calendar",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
    )
    baseline = client.graph.graph_state_hash(scope.key)

    await client.pin_memory(
        relationship_uuid=fact.relationship_uuid,
        scope=scope,
        reason="hold",
        pinned_by="operator:test",
    )
    pinned_hash = client.graph.graph_state_hash(scope.key)
    assert pinned_hash != baseline

    await client.set_memory_visibility(
        relationship_uuid=fact.relationship_uuid,
        scope=scope,
        agents=("agent-a",),
        reason="a-only",
        set_by="operator:test",
    )
    restricted_hash = client.graph.graph_state_hash(scope.key)
    assert restricted_hash not in {baseline, pinned_hash}

    pin_receipts = _receipts_of(client, scope, ReceiptDecisionType.MEMORY_PINNED)
    assert pin_receipts[0].graph_state_hash_before != pin_receipts[0].graph_state_hash_after

    # Allowlist ORDER is not state; membership is.
    client.graph.update_relationship(fact.relationship_uuid, properties={"visibility_agents": ["agent-a"]})
    assert client.graph.graph_state_hash(scope.key) == restricted_hash
    client.graph.update_relationship(fact.relationship_uuid, properties={"visibility_agents": ["agent-a", "agent-b"]})
    assert client.graph.graph_state_hash(scope.key) != restricted_hash


# ---------------------------------------------------------------------------
# M3 — the entailment gate rejects recombination
# ---------------------------------------------------------------------------


def test_entailment_gate_rejects_claims_recombined_across_members() -> None:
    """Token-union coverage let a summary INVERT a claim by recombining two
    members; single-member coverage per sentence closes it."""
    members = [
        "production gateway requires mTLS",
        "sandbox gateway disables mTLS",
    ]
    inverted, reason = rollup_summary_entailed("production gateway disables mTLS.", members)
    assert inverted is False
    assert reason.startswith("recombined_across_members:")

    faithful, _ = rollup_summary_entailed("production gateway requires mTLS.", members)
    assert faithful is True

    # A genuinely multi-member summary states one member per sentence.
    spanning, _ = rollup_summary_entailed("production gateway requires mTLS. sandbox gateway disables mTLS.", members)
    assert spanning is True

    # Fabricated tokens still fail on the union rule first.
    hallucinated, reason = rollup_summary_entailed("production gateway requires kerberos.", members)
    assert hallucinated is False
    assert reason == "unentailed_token:kerberos"


# ---------------------------------------------------------------------------
# M4 — the hosted surface binds agent_id to the principal
# ---------------------------------------------------------------------------


def test_hosted_agent_id_must_match_the_authenticated_principal(tmp_path: Path) -> None:
    """A quorum over a self-asserted `agent_id` is not a quorum: one caller could
    promote a fact and then endorse it again as any other agent id."""
    handler = admin_server.MemoryGraphHandler.__new__(admin_server.MemoryGraphHandler)
    handler.principal = admin_server.build_demo_principal(
        principal_id="alpha-principal",
        tenant_id="jedai-platform",
        agent_id="alpha",
        default_scope=MemoryScope(kind=ScopeKind.TENANT, scope_id="jedai-platform"),
        role=PrincipalRole.ADMIN,
    )

    assert handler._principal_bound_agent_id({"agent_id": "alpha"}) == "alpha"
    with pytest.raises(admin_server.HttpApiError) as exc:
        handler._principal_bound_agent_id({"agent_id": "beta"})
    assert exc.value.status == 403
    assert "does not match the authenticated principal" in str(exc.value)
