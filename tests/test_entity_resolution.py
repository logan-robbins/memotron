"""WS-17 T16b: semantic entity resolution — name-level alias registry.

Pins the T16b contracts:

- Node identity AND truth keys are name-keyed, so alias resolution BEFORE node
  upsert + truth_identity bridges both planes: facts about "the gateway" and
  "Jedai Gateway" land on ONE node and ONE truth slot (operator synonym map,
  extractor-attested auto-link, or nothing — fail-closed offline).
- Composed link score = w_llm*llm + w_name*name_cosine + w_ctx*context +
  w_nbr*neighborhood; the three offline signals top out at 0.6 < review band,
  so no link forms without extractor attestation or an operator synonym.
- Bands: >= 0.85 auto-links (FORMATION_ENTITY_LINKED, surface preserved on the
  row); [0.65, 0.85) parks a 'proposed' registry row (ENTITY_ALIAS_PROPOSED)
  while the mention materializes under its surface; below writes nothing.
- Identifier hard block: two ID-bearing names with engineered cosine 1.0 never
  link or propose (receipted refusal).
- Adjudication mirrors T14: pending_entity_alias_proposals /
  resolve_entity_alias_proposal; approve activates + backfills
  (resolve_scope_entities) and the existing single-active repair collapses the
  collided slot; reject is sticky at the same score class (+0.05 to re-propose).
- Read side: entity_neighborhood unions the alias group; retrieval stage-3
  seeds cross the alias boundary (origin/hops attribution intact).
- Alias confidence is alive: contradictory single-active truths across the
  pair discount the link and demote it to 'proposed' (ENTITY_ALIAS_DEMOTED);
  formation stops resolving through it.
- Determinism: same inputs -> identical registry rows, scores, receipts
  (modulo uuids/timestamps).  enabled=False -> byte-identical legacy behavior.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    Memotron,
    EntityResolutionPolicy,
    MemoryScope,
    ScopeKind,
)
from memotron.config import default_config
from memotron.models import EpisodeType, RelationshipStatus
from memotron.receipts import ReceiptDecisionType

KB_DS_WDW = "kb_ds_source_plandisney_pocket_guides_wdw_en_us_v1"
KB_DS_DLR = "kb_ds_source_plandisney_pocket_guides_dlr_en_us_v1"

ENTITY_RECEIPT_TYPES = (
    ReceiptDecisionType.FORMATION_ENTITY_LINKED,
    ReceiptDecisionType.ENTITY_ALIAS_PROPOSED,
    ReceiptDecisionType.ENTITY_ALIAS_RESOLVED,
    ReceiptDecisionType.ENTITY_ALIAS_DEMOTED,
)


class EntityNameStubTransport:
    """Deterministic stub with controlled NAME cosines.

    Gateway-flavoured names share axis 0 (cosine 1.0); kb_ds data-store IDs
    share axis 1 (the engineered 0.9+ cosine for the hard-block test); every
    other text lands on a stable hash bucket.  Object texts in these tests are
    chosen to differ so fact-plane dedup never interferes.
    """

    identifier = "entity-name-stub-16@v1"

    def embed(self, text: str) -> list[float]:
        normalized = " ".join(text.casefold().split())
        vector = [0.0] * 16
        if "gateway" in normalized:
            vector[0] = 1.0
        elif "kb_ds" in normalized:
            vector[1] = 1.0
        else:
            vector[2 + (sum(normalized.encode("utf-8")) % 13)] = 1.0
        norm = math.sqrt(sum(component * component for component in vector))
        return [component / norm for component in vector]


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _client(
    tmp_path, *, policy: EntityResolutionPolicy | None = None, name: str = "graph.sqlite", **kwargs
) -> Memotron:
    config = default_config()
    if policy is not None:
        config = config.model_copy(update={"entity_resolution": policy})
    return Memotron(config=config, graph_path=tmp_path / name, **kwargs)


async def _run_formation(client: Memotron) -> None:
    await client.run_dream_job(job_name="formation-default")


async def _queue_json_episode(
    client: Memotron,
    scope: MemoryScope,
    *,
    name: str,
    memory: dict,
    when: datetime,
    slug: str | None = None,
) -> None:
    await client.add_episode(
        name=name,
        episode_body=json.dumps({"memories": [memory]}),
        source=EpisodeType.JSON,
        scope=scope,
        reference_time=when,
        metadata={"slug": slug} if slug else None,
    )


def _entity_receipts(client: Memotron, scope: MemoryScope):
    return [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type in ENTITY_RECEIPT_TYPES
    ]


def _entity_nodes(client: Memotron, scope: MemoryScope) -> dict[str, str]:
    """Revealed (normalized name -> uuid) map of the scope's entity nodes."""
    names: dict[str, str] = {}
    for node in client.graph.nodes_for_scope(scope.key):
        if "Episode" in node.labels:
            continue
        name = str(client.graph.reveal(scope.key, node.properties.get("name", "")))
        names[" ".join(name.casefold().split())] = node.uuid
    return names


async def test_operator_synonym_map_bridges_one_node_one_slot(tmp_path) -> None:
    """Facts about the alias and the canonical share ONE node and ONE truth slot;
    a correction via the alias supersedes the canonical incumbent."""
    scope = _scope("synonym")
    client = _client(
        tmp_path,
        policy=EntityResolutionPolicy(synonyms={"the gateway": "jedai gateway"}),
    )
    start = datetime(2026, 7, 1, tzinfo=UTC)
    incumbent = await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",  # single_active
        scope=scope,
        confidence=0.9,
        reference_time=start,
    )
    correction = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start + timedelta(days=1),
    )

    old = client.graph.get_relationship(incumbent.relationship_uuid)
    new = client.graph.get_relationship(correction.relationship_uuid)
    assert old.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert old.properties["superseded_by_relationship_uuid"] == correction.relationship_uuid
    assert new.properties["status"] == RelationshipStatus.ACTIVE.value
    assert new.properties["truth_key"] == old.properties["truth_key"]
    # ONE gateway node: the alias mention landed on the canonical node.
    assert new.source_uuid == old.source_uuid
    nodes = _entity_nodes(client, scope)
    assert "jedai gateway" in nodes
    assert "the gateway" not in nodes
    # Surface form preserved on the row.
    assert new.properties["subject_surface"] == "the gateway"
    assert "subject_surface" not in old.properties

    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row is not None
    assert row["status"] == "active"
    assert client.graph.canonical_entity_name_for(scope.key, "the gateway") == "Jedai Gateway"

    linked = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_ENTITY_LINKED
    ]
    assert len(linked) == 1
    assert linked[0].decision_reason == "the gateway->jedai gateway:synonym_map"
    assert linked[0].decision_result == "recorded"


async def test_auto_link_band_links_and_preserves_surface(tmp_path) -> None:
    """entity_ref + high composed score -> FORMATION_ENTITY_LINKED, canonical node
    used, surface preserved on the row."""
    scope = _scope("auto-link")
    client = _client(tmp_path, embedding_transport=EntityNameStubTransport())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await _queue_json_episode(
        client,
        scope,
        name="ep1",
        memory={
            "subject": "Jedai Gateway",
            "predicate": "prefers",
            "object": "structured JSON logs",
            "relationship_type": "PREFERS",
            "confidence": 0.9,
        },
        when=start,
        slug="products/jedai-gateway",
    )
    await _run_formation(client)
    await _queue_json_episode(
        client,
        scope,
        name="ep2",
        memory={
            "subject": "the gateway",
            "subject_entity_ref": "Jedai Gateway",
            "subject_link_confidence": 1.0,
            "predicate": "prefers",
            "object": "mTLS between services",
            "relationship_type": "PREFERS",
            "confidence": 0.9,
        },
        when=start + timedelta(days=1),
        slug="products/jedai-gateway",
    )
    await _run_formation(client)

    # llm 1.0*0.4 + name 1.0*0.3 + context 1.0*0.15 (same slug) = 0.85 >= auto.
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row is not None
    assert row["status"] == "active"
    assert row["canonical_name"] == "Jedai Gateway"
    assert row["link_score"] == pytest.approx(0.85)
    assert row["link_signals"]["llm_confidence"] == pytest.approx(1.0)
    assert row["link_signals"]["name_cosine"] == pytest.approx(1.0)
    assert row["link_signals"]["context_overlap"] == pytest.approx(1.0)

    nodes = _entity_nodes(client, scope)
    assert "jedai gateway" in nodes and "the gateway" not in nodes
    facts = [
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.properties.get("status") == RelationshipStatus.ACTIVE.value
    ]
    assert len(facts) == 2
    assert len({fact.source_uuid for fact in facts}) == 1  # one canonical node
    surfaces = {fact.properties.get("subject_surface") for fact in facts}
    assert surfaces == {None, "the gateway"}
    prefixes = {fact.properties.get("truth_prefix") for fact in facts}
    assert len(prefixes) == 1  # one truth-prefix family

    linked = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_ENTITY_LINKED
    ]
    assert len(linked) == 1
    assert linked[0].decision_reason.startswith("the gateway->jedai gateway:auto_link")
    assert linked[0].decision_result == "recorded"
    assert linked[0].dedup_score == pytest.approx(0.85)
    assert json.loads(linked[0].event_payload)["name_cosine"] == pytest.approx(1.0)


async def test_review_band_proposal_approve_backfill_collapses_slot(tmp_path) -> None:
    """Mid score -> surface materialization + proposal; approve -> ACTIVE alias +
    backfill + single-active repair composition."""
    scope = _scope("review-approve")
    client = _client(tmp_path, embedding_transport=EntityNameStubTransport())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await _queue_json_episode(
        client,
        scope,
        name="ep1",
        memory={
            "subject": "Jedai Gateway",
            "predicate": "requires",
            "object": "OAuth2 client credentials",
            "relationship_type": "REQUIRES",
            "confidence": 0.9,
        },
        when=start,
        slug="products/jedai-gateway",
    )
    await _run_formation(client)
    await _queue_json_episode(
        client,
        scope,
        name="ep2",
        memory={
            "subject": "the gateway",
            "subject_entity_ref": "Jedai Gateway",
            "subject_link_confidence": 0.9,
            "predicate": "requires",
            "object": "SAML assertions",
            "relationship_type": "REQUIRES",
            "confidence": 0.9,
        },
        when=start + timedelta(days=1),
        slug="platform/decision-log",  # different slug -> context signal 0
    )
    await _run_formation(client)

    # llm 0.9*0.4 + name 1.0*0.3 = 0.66 in [0.65, 0.85) -> proposed.
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row is not None and row["status"] == "proposed"
    nodes = _entity_nodes(client, scope)
    assert "the gateway" in nodes  # surface node exists — nothing was bridged yet
    surface_fact = next(
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.source_uuid == nodes["the gateway"]
    )
    assert surface_fact.properties["status"] == RelationshipStatus.ACTIVE.value
    assert "subject_surface" not in surface_fact.properties

    proposals = await client.pending_entity_alias_proposals(scope=scope)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.name == "the gateway"
    assert proposal.canonical_name == "Jedai Gateway"
    assert proposal.link_score == pytest.approx(0.66)
    assert surface_fact.uuid in proposal.sample_fact_uuids
    proposed_receipts = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.ENTITY_ALIAS_PROPOSED
    ]
    assert len(proposed_receipts) == 1
    assert proposed_receipts[0].decision_result == "recorded"

    resolution = await client.resolve_entity_alias_proposal(
        scope=scope,
        name="the gateway",
        decision="approve",
        reason="same service, documented alias",
        resolved_by="kb-operator",
    )
    assert resolution.status == "active"
    assert resolution.backfill_rewritten_count == 1
    assert client.graph.canonical_entity_name_for(scope.key, "the gateway") == "Jedai Gateway"

    # Backfill rewrote the surviving surface row onto the canonical slot
    # (hash-bracketed receipt), preserving the surface form.
    rewritten = client.graph.get_relationship(surface_fact.uuid)
    assert rewritten.properties["subject_surface"] == "the gateway"
    canonical_fact = next(
        relationship
        for relationship in client.graph.relationships_for_scope(scope.key)
        if relationship.source_uuid == nodes["jedai gateway"]
    )
    assert rewritten.properties["truth_key"] == canonical_fact.properties["truth_key"]
    backfill_receipts = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_ENTITY_LINKED
        and receipt.decision_reason.startswith("backfill:")
    ]
    assert len(backfill_receipts) == 1
    # Hash-bracketed like the predicate backfill: truth keys are not
    # graph_state_hash inputs, so before/after brackets are recorded but equal.
    assert backfill_receipts[0].graph_state_hash_before is not None
    assert backfill_receipts[0].graph_state_hash_after is not None
    resolved_receipts = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.ENTITY_ALIAS_RESOLVED
    ]
    assert len(resolved_receipts) == 1
    assert resolved_receipts[0].decision_reason.startswith("operator_approved:")

    # Composition: the existing single-active repair collapses the collided slot.
    await client.run_dream_job(job_name="pruning-default")
    loser = client.graph.get_relationship(canonical_fact.uuid)
    winner = client.graph.get_relationship(rewritten.uuid)
    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    assert loser.properties["status"] != RelationshipStatus.ACTIVE.value
    assert loser.properties["superseded_reason"] == "single_active_truth_repair"
    assert loser.properties["superseded_by_relationship_uuid"] == rewritten.uuid


async def test_review_band_reject_is_sticky_until_score_improves(tmp_path) -> None:
    """Reject keeps the pair separate; the same score never re-proposes; a
    clearly better score re-proposes (never silently auto-links past a human)."""
    scope = _scope("review-reject")
    client = _client(tmp_path, embedding_transport=EntityNameStubTransport())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await _queue_json_episode(
        client,
        scope,
        name="ep1",
        memory={
            "subject": "Jedai Gateway",
            "predicate": "prefers",
            "object": "structured JSON logs",
            "relationship_type": "PREFERS",
            "confidence": 0.9,
        },
        when=start,
        slug="products/jedai-gateway",
    )
    await _run_formation(client)
    review_band_memory = {
        "subject": "the gateway",
        "subject_entity_ref": "Jedai Gateway",
        "subject_link_confidence": 0.9,
        "predicate": "prefers",
        "object": "mTLS between services",
        "relationship_type": "PREFERS",
        "confidence": 0.9,
    }
    await _queue_json_episode(
        client,
        scope,
        name="ep2",
        memory=review_band_memory,
        when=start + timedelta(days=1),
        slug="platform/decision-log",
    )
    await _run_formation(client)
    assert client.graph.entity_alias_row(scope.key, "the gateway")["status"] == "proposed"

    rejection = await client.resolve_entity_alias_proposal(
        scope=scope,
        name="the gateway",
        decision="reject",
        reason="different component",
        resolved_by="kb-operator",
    )
    assert rejection.status == "rejected"
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row["link_signals"]["last_rejected_score"] == pytest.approx(0.66)
    assert await client.pending_entity_alias_proposals(scope=scope) == []

    # Same score class again (0.66 < 0.66 + 0.05): stays rejected, no proposal.
    await _queue_json_episode(
        client,
        scope,
        name="ep3",
        memory={**review_band_memory, "object": "request tracing headers"},
        when=start + timedelta(days=2),
        slug="platform/decision-log",
    )
    await _run_formation(client)
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row["status"] == "rejected"
    assert await client.pending_entity_alias_proposals(scope=scope) == []
    # The fact still lands under the surface name.
    nodes = _entity_nodes(client, scope)
    assert "the gateway" in nodes

    # A materially better score (0.85 >= 0.66 + 0.05) re-proposes — it does NOT
    # auto-link past the recorded human rejection.
    await _queue_json_episode(
        client,
        scope,
        name="ep4",
        memory={
            **review_band_memory,
            "subject_link_confidence": 1.0,
            "object": "circuit breaking on upstream errors",
        },
        when=start + timedelta(days=3),
        slug="products/jedai-gateway",
    )
    await _run_formation(client)
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row["status"] == "proposed"
    assert row["link_score"] == pytest.approx(0.85)
    assert row["link_signals"]["last_rejected_score"] == pytest.approx(0.66)
    re_proposed = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.ENTITY_ALIAS_PROPOSED
        and ":re_proposed:" in receipt.decision_reason
    ]
    assert len(re_proposed) == 1


async def test_identifier_hard_block_never_links_or_proposes(tmp_path) -> None:
    """Two ID-bearing names at engineered name-cosine 1.0 never link or propose;
    the refusal is receipted."""
    scope = _scope("hard-block")
    client = _client(tmp_path, embedding_transport=EntityNameStubTransport())
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await _queue_json_episode(
        client,
        scope,
        name="ep1",
        memory={
            "subject": KB_DS_WDW,
            "predicate": "prefers",
            "object": "nightly incremental sync",
            "relationship_type": "PREFERS",
            "confidence": 0.9,
        },
        when=start,
        slug="platform/knowledgebase",
    )
    await _run_formation(client)
    await _queue_json_episode(
        client,
        scope,
        name="ep2",
        memory={
            "subject": KB_DS_DLR,
            "subject_entity_ref": KB_DS_WDW,
            "subject_link_confidence": 1.0,
            "predicate": "prefers",
            "object": "weekly full re-index",
            "relationship_type": "PREFERS",
            "confidence": 0.9,
        },
        when=start + timedelta(days=1),
        slug="platform/knowledgebase",
    )
    await _run_formation(client)

    # Would-be score 0.85 (llm 0.4 + name 0.3 + context 0.15) — hard-blocked.
    assert client.graph.entity_alias_rows_for_scope(scope.key) == []
    nodes = _entity_nodes(client, scope)
    assert KB_DS_WDW in nodes and KB_DS_DLR in nodes  # two distinct nodes
    prefixes = {
        relationship.properties.get("truth_prefix") for relationship in client.graph.relationships_for_scope(scope.key)
    }
    assert len(prefixes) == 2  # two distinct truth slots
    refusals = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.ENTITY_ALIAS_PROPOSED
    ]
    assert len(refusals) == 1
    assert refusals[0].decision_result == "rejected"
    assert refusals[0].decision_reason == f"identifier_token_mismatch:{KB_DS_DLR}->{KB_DS_WDW}"
    assert refusals[0].dedup_score == pytest.approx(0.85)


async def test_read_side_union_crosses_alias_boundary(tmp_path) -> None:
    """entity_neighborhood unions the alias group; retrieval finds facts recorded
    under the alias when querying the canonical, with origin/hops intact."""
    scope = _scope("read-union")
    client = _client(tmp_path)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start,
    )
    # A lexically DISJOINT alias surface — nothing but the registry bridges it.
    await client.add_memory(
        subject="JW",
        predicate="prefers",
        object="structured JSON logs",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=start + timedelta(hours=1),
    )
    client.graph.register_entity_alias(
        scope.key,
        "JW",
        "Jedai Gateway",
        status="active",
        decided_by="test-operator",
        link_score=0.9,
    )

    canonical_edges = await client.entity_neighborhood(scope=scope, entity="Jedai Gateway")
    alias_edges = await client.entity_neighborhood(scope=scope, entity="JW")
    assert {edge.relationship_uuid for edge in canonical_edges} == {edge.relationship_uuid for edge in alias_edges}
    assert {(edge.subject, edge.object) for edge in canonical_edges} == {
        ("Jedai Gateway", "OAuth2 client credentials"),
        ("JW", "structured JSON logs"),
    }

    results = await client.search(query="Jedai Gateway", scope=scope)
    by_subject = {result.subject: result for result in results}
    assert "JW" in by_subject, "alias-recorded fact must surface for a canonical query"
    assert by_subject["JW"].origin == "entity_hop"
    assert by_subject["JW"].hops == 1


async def test_contradictory_truths_across_pair_discount_but_hold_the_alias(
    tmp_path,
) -> None:
    """Engineered contradictory single-active truths across the pair — a
    gate-parked dispute, not a clean supersession — discount the ACTIVE alias
    and record the dispute (receipted).

    WS-23 C1 changed the OUTCOME of that discount.  Demoting the alias here was
    self-defeating: the alias is what bridged the challenger onto the canonical
    slot, so demoting it sent every later mention of the surface back to its own
    slot and left two contradictory ACTIVE rows the single-active repair (keyed
    on ``truth_key``) could never see — the contradiction killed the mechanism
    that resolved it.  A link that is currently routing live truth-slot rows is
    therefore HELD ACTIVE: the score still falls, the dispute is still counted,
    and the refusal is receipted, but routing stays coherent.  (An
    identifier-token conflict still demotes — and re-keys the bridged rows back
    to their surface first.)

    A CLEAN supersession through the alias touches nothing at all (that is
    ordinary truth evolution — pinned by the synonym-map test above).
    """
    scope = _scope("demotion")
    client = _client(
        tmp_path,
        policy=EntityResolutionPolicy(synonyms={"the gateway": "jedai gateway"}),
    )
    start = datetime(2026, 7, 1, tzinfo=UTC)
    incumbent = None
    for observation in range(3):  # observed_count reaches corroboration_margin=3
        result = await client.add_memory(
            subject="Jedai Gateway",
            predicate="requires",
            object="OAuth1 signatures",
            relationship_type="REQUIRES",
            scope=scope,
            confidence=0.9,
            reference_time=start + timedelta(minutes=observation),
        )
        incumbent = incumbent or result
    conflicting = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start + timedelta(days=1),
    )

    # The challenger PARKED against the corroborated incumbent — the slot now
    # holds contradictory truths across the two surface forms.
    incumbent_row = client.graph.get_relationship(incumbent.relationship_uuid)
    challenger_row = client.graph.get_relationship(conflicting.relationship_uuid)
    assert incumbent_row.properties["status"] == RelationshipStatus.ACTIVE.value
    assert challenger_row.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert challenger_row.properties["requires_operator_review"] is True
    assert str(challenger_row.properties["supersession_gate_reason"]).startswith("insufficient_corroboration")
    assert challenger_row.properties["subject_surface"] == "the gateway"

    # The standing dispute discounted the link below the auto bar
    # (1.0 * (1 - 0.25 * 0.9) = 0.775 < 0.85) but the bridge is load-bearing,
    # so the status change is refused and receipted.
    row = client.graph.entity_alias_row(scope.key, "the gateway")
    assert row["status"] == "active"
    assert row["link_score"] == pytest.approx(0.775)
    assert row["link_signals"]["disputes"] == 1
    assert row["link_signals"]["demotion_refused_bridged_rows"] >= 1
    demoted = [
        receipt
        for receipt in _entity_receipts(client, scope)
        if receipt.decision_type == ReceiptDecisionType.ENTITY_ALIAS_DEMOTED
    ]
    assert len(demoted) == 1
    assert "contradictory_single_active_truth" in demoted[0].decision_reason
    assert "demotion_refused_bridged_rows" in demoted[0].decision_reason
    assert demoted[0].decision_result == "gated"
    # A held alias is NOT re-queued for review — it is still in force.
    proposals = await client.pending_entity_alias_proposals(scope=scope)
    assert [proposal.name for proposal in proposals] == []

    # Formation still resolves through the held alias: the next mention lands on
    # the canonical node and the canonical slot, so truth stays reconcilable.
    third = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="mutual TLS",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start + timedelta(days=2),
    )
    third_row = client.graph.get_relationship(third.relationship_uuid)
    nodes = _entity_nodes(client, scope)
    assert "the gateway" not in nodes
    assert third_row.source_uuid == incumbent_row.source_uuid
    assert third_row.properties["subject_surface"] == "the gateway"
    assert (
        third_row.properties["truth_key"] == incumbent_row.properties["truth_key"]
    )  # ONE canonical slot, not a permanent split


async def test_determinism_same_inputs_same_registry_and_receipts(tmp_path) -> None:
    """Same inputs twice -> identical registry rows, scores, and receipt stream
    (modulo uuids/timestamps)."""

    async def _run(name: str) -> tuple[list[dict], list[tuple[str, str, str, float | None]]]:
        scope = _scope("determinism")
        client = _client(tmp_path, name=name, embedding_transport=EntityNameStubTransport())
        start = datetime(2026, 7, 1, tzinfo=UTC)
        await _queue_json_episode(
            client,
            scope,
            name="ep1",
            memory={
                "subject": "Jedai Gateway",
                "predicate": "prefers",
                "object": "structured JSON logs",
                "relationship_type": "PREFERS",
                "confidence": 0.9,
            },
            when=start,
            slug="products/jedai-gateway",
        )
        await _run_formation(client)
        await _queue_json_episode(
            client,
            scope,
            name="ep2",
            memory={
                "subject": "the gateway",
                "subject_entity_ref": "Jedai Gateway",
                "subject_link_confidence": 1.0,
                "predicate": "prefers",
                "object": "mTLS between services",
                "relationship_type": "PREFERS",
                "confidence": 0.9,
            },
            when=start + timedelta(days=1),
            slug="products/jedai-gateway",
        )
        await _run_formation(client)
        rows = [
            {key: value for key, value in row.items() if key not in ("proposed_at", "resolved_at")}
            for row in client.graph.entity_alias_rows_for_scope(scope.key)
        ]
        receipts = [
            (
                receipt.decision_type.value,
                receipt.decision_reason,
                receipt.decision_result,
                receipt.dedup_score,
            )
            for receipt in _entity_receipts(client, scope)
        ]
        return rows, receipts

    first_rows, first_receipts = await _run("first.sqlite")
    second_rows, second_receipts = await _run("second.sqlite")
    assert first_rows == second_rows
    assert first_receipts == second_receipts
    assert first_rows, "the determinism scenario must actually register a link"


async def test_disabled_policy_is_byte_identical_legacy(tmp_path) -> None:
    """enabled=False -> fragmentation returns, no registry rows, no receipts,
    no node name embeddings, no prompt inventory."""
    scope = _scope("legacy")
    client = _client(tmp_path, policy=EntityResolutionPolicy(enabled=False))
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first = await client.add_memory(
        subject="Jedai Gateway",
        predicate="requires",
        object="OAuth2 client credentials",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start,
    )
    second = await client.add_memory(
        subject="the gateway",
        predicate="requires",
        object="SAML assertions",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=start + timedelta(days=1),
    )

    old = client.graph.get_relationship(first.relationship_uuid)
    new = client.graph.get_relationship(second.relationship_uuid)
    # The documented gap: both stay ACTIVE on split truth slots and split nodes.
    assert old.properties["status"] == RelationshipStatus.ACTIVE.value
    assert new.properties["status"] == RelationshipStatus.ACTIVE.value
    assert old.properties["truth_key"] != new.properties["truth_key"]
    assert old.source_uuid != new.source_uuid
    assert "subject_surface" not in new.properties
    # Legacy shape: no registry, no receipts, no name embeddings, no inventory.
    assert client.graph.entity_alias_rows_for_scope(scope.key) == []
    assert _entity_receipts(client, scope) == []
    for node in client.graph.nodes_for_scope(scope.key):
        assert "name_embedding" not in node.properties
    assert client._engine.entity_inventory_for_scope(scope) == []
    with pytest.raises(ValueError, match="entity resolution is disabled"):
        await client.resolve_scope_entities(scope=scope)


# ---------------------------------------------------------------------------
# The mention's link inputs are resolved once per mention, not once per candidate
# ---------------------------------------------------------------------------


class _CountingNameStubTransport:
    """``EntityNameStubTransport`` that records every text it is asked to embed.

    Shares the stub's ``identifier`` so vectors it stamps on nodes are in the
    active vector space and the name-cosine signal is actually consulted.
    """

    identifier = EntityNameStubTransport.identifier

    def __init__(self) -> None:
        self._inner = EntityNameStubTransport()
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return self._inner.embed(text)


async def _mention_embeds_for_inventory(tmp_path, *, inventory_size: int, name: str):
    """Seed *inventory_size* canonical entities, then form ONE new mention.

    Returns ``(times the mention name was embedded, candidate node count)``.
    Every seeded subject contains "gateway", so the stub gives the mention a
    name cosine of 1.0 against all of them — the signal is genuinely computed
    for every candidate — while the composed offline score tops out at 0.6,
    below the review band, so no alias forms and nothing else changes.
    """
    scope = _scope(f"link-scan-{inventory_size}")
    transport = _CountingNameStubTransport()
    client = _client(tmp_path, embedding_transport=transport, name=name)
    start = datetime(2026, 7, 1, tzinfo=UTC)
    for index in range(inventory_size):
        await _queue_json_episode(
            client,
            scope,
            name=f"seed-{index}",
            memory={
                "subject": f"Jedai Gateway Region {index}",
                "predicate": "prefers",
                "object": f"regional policy variant {index}",
                "relationship_type": "PREFERS",
                "confidence": 0.9,
            },
            when=start + timedelta(minutes=index),
            slug="products/jedai-gateway",
        )
    await _run_formation(client)
    candidate_count = len(_entity_nodes(client, scope))

    transport.calls.clear()
    await _queue_json_episode(
        client,
        scope,
        name="mention",
        memory={
            "subject": "the gateway",
            "predicate": "requires",
            "object": "mTLS between services",
            "relationship_type": "REQUIRES",
            "confidence": 0.9,
        },
        when=start + timedelta(days=1),
        slug="products/jedai-gateway",
    )
    await _run_formation(client)
    return transport.calls.count("the gateway"), candidate_count


async def test_entity_link_scan_embeds_the_mention_independently_of_inventory(
    tmp_path,
) -> None:
    """The mention name is loop-invariant across the candidate scan.

    It used to be re-embedded inside the per-candidate signal function, so one
    mention cost one embedding PER CANDIDATE — O(mentions x inventory) remote
    calls, growing with the graph.  Quadrupling the inventory must not change
    how many times the mention is embedded.
    """
    small_embeds, small_inventory = await _mention_embeds_for_inventory(tmp_path, inventory_size=4, name="small.sqlite")
    large_embeds, large_inventory = await _mention_embeds_for_inventory(
        tmp_path, inventory_size=16, name="large.sqlite"
    )

    # The scenario has to actually scale, or the assertion below proves nothing.
    assert large_inventory >= small_inventory * 3 >= 12
    assert small_embeds == large_embeds
    # One resolution of the signal, plus the node-name stamp on the mention's
    # own node — bounded by the mention, never by the candidate count.
    assert small_embeds <= 2 < small_inventory


def test_mention_link_context_resolves_the_name_vector_lazily_and_once() -> None:
    """The hoist is structural: at most one embed per mention, and none at all
    when no candidate carries a same-space stored vector to compare against."""
    from memotron.dreaming import DreamEngine

    calls: list[str] = []

    def _embed(text: str) -> list[float]:
        calls.append(text)
        return [1.0]

    context = DreamEngine._MentionLinkContext(
        scope_key="user:demo",
        normalized="the gateway",
        neighbors=frozenset(),
        episode_tokens=frozenset(),
        embed=_embed,
    )
    assert calls == []  # nothing embedded until a candidate actually needs it
    assert context.name_embedding == [1.0]
    assert context.name_embedding == [1.0]
    assert calls == ["the gateway"]
