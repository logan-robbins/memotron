"""WS-17 T17: consolidation-time cross-prefix duplicate sweep.

Pins the T17 contracts:

- Two paraphrased restatements of one fact on DIFFERENT truth prefixes (a
  different subject or predicate surface) demote the weaker row from context
  when their full-fact embedding cosine reaches
  ``cross_prefix_duplicate_threshold`` and the WS-16 statement-compatibility
  gates pass — ``active_in_context=False`` + ``duplicate_of`` +
  ``duplicate_cosine``, receipted (CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED,
  hash-bracketed), never deleted: out of the default profile, still searchable.
- Incompatible polarity or claim-mode pairs NEVER demote.
- ``cross_prefix_duplicate_threshold=None`` disables the sweep entirely.
- Supersession/prune of the stronger row re-promotes its duplicates
  (CONSOLIDATION_DUPLICATE_REPROMOTED) instead of orphaning them invisibly.
- ``max_duplicate_demotions_per_run`` caps demotions per run.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    DreamJob,
    DreamJobKind,
    Memotron,
    MemoryScope,
    RollupConsolidationPolicy,
    ScopeKind,
)
from memotron.config import default_config
from memotron.receipts import ReceiptDecisionType

DUPLICATE_OBJECT = "dark roast coffee in the morning"


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _client(
    tmp_path,
    *,
    cross_prefix_duplicate_threshold: float | None = 0.80,
    max_duplicate_demotions_per_run: int = 32,
    name: str = "graph.sqlite",
) -> Memotron:
    config = default_config().model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="consolidation",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cross_prefix_duplicate_threshold=cross_prefix_duplicate_threshold,
                        max_duplicate_demotions_per_run=max_duplicate_demotions_per_run,
                    ),
                ),
                DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),
            )
        }
    )
    return Memotron(config=config, graph_path=tmp_path / name)


async def _duplicate_pair(client: Memotron, scope: MemoryScope, *, when: datetime):
    """One fact restated on two truth prefixes: different subject AND predicate.

    The stronger restatement has the higher confidence; both are multi-active
    preferences so no single-active machinery interferes.
    """
    stronger = await client.add_memory(
        subject="Priya",
        predicate="prefers",
        object=DUPLICATE_OBJECT,
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    weaker = await client.add_memory(
        subject="Priya Sharma",
        predicate="likes",
        object=DUPLICATE_OBJECT,
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.7,
        reference_time=when + timedelta(minutes=5),
    )
    strong_row = client.graph.get_relationship(stronger.relationship_uuid)
    weak_row = client.graph.get_relationship(weaker.relationship_uuid)
    assert strong_row.properties["truth_prefix"] != weak_row.properties["truth_prefix"]
    return stronger, weaker


async def test_cross_prefix_duplicate_demotes_weaker_row(tmp_path) -> None:
    scope = _scope("dup-demote")
    client = _client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    stronger, weaker = await _duplicate_pair(client, scope, when=when)

    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))

    strong_row = client.graph.get_relationship(stronger.relationship_uuid)
    weak_row = client.graph.get_relationship(weaker.relationship_uuid)
    # Weaker row (lower confidence) demoted from context, never deleted.
    assert strong_row.properties.get("active_in_context") is not False
    assert weak_row.properties["status"] == "active"
    assert weak_row.properties["active_in_context"] is False
    assert weak_row.properties["duplicate_of"] == stronger.relationship_uuid
    assert weak_row.properties["duplicate_cosine"] >= 0.80

    # Profile (working context) excludes the demoted duplicate; search (the
    # audit surface) still finds it — identical to rollup-member demotion.
    profile = await client.profile(scope=scope)
    assert "Priya prefers" in profile.rendered_context
    assert "Priya Sharma likes" not in profile.rendered_context
    found = await client.search(query="dark roast coffee", scope=scope)
    assert {item.relationship_uuid for item in found} == {
        stronger.relationship_uuid,
        weaker.relationship_uuid,
    }

    receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED
    ]
    assert len(receipts) == 1
    receipt = receipts[0]
    assert receipt.decision_result == "demoted"
    assert receipt.relationship_uuid == weaker.relationship_uuid
    assert receipt.successor_relationship_uuid == stronger.relationship_uuid
    assert receipt.dedup_threshold == pytest.approx(0.80)
    assert receipt.dedup_score == pytest.approx(weak_row.properties["duplicate_cosine"])
    assert receipt.graph_state_hash_before is not None
    assert receipt.graph_state_hash_after is not None
    assert receipt.graph_state_hash_before != receipt.graph_state_hash_after

    decisions = await client.dream_decisions(job_name="consolidation")
    assert any(decision.decision_type == "consolidation_cross_prefix_duplicate_demoted" for decision in decisions)


async def test_incompatible_polarity_or_claim_pairs_never_demote(tmp_path) -> None:
    scope = _scope("dup-incompatible")
    # Threshold 0.5 guarantees the pairs WOULD demote if compatibility were ignored.
    client = _client(tmp_path, cross_prefix_duplicate_threshold=0.5)
    when = datetime(2026, 7, 20, tzinfo=UTC)

    # Polarity conflict: the restatement is negated.
    positive = await client.add_memory(
        subject="Priya",
        predicate="prefers",
        object=DUPLICATE_OBJECT,
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    negated = await client.add_memory(
        subject="Priya Sharma",
        predicate="likes",
        object=f"not {DUPLICATE_OBJECT}",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.7,
        reference_time=when + timedelta(minutes=5),
    )

    # Claim-mode conflict: an operator CORRECTION row vs a plain assertion with
    # near-identical wording on a different truth prefix.
    seed = await client.add_memory(
        subject="deploys",
        predicate="require",
        object="one approval for production changes",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    corrected = await client.correct_memory(
        relationship_uuid=seed.relationship_uuid,
        corrected_object="two approvals for production changes",
        scope=scope,
        now=when + timedelta(minutes=10),
    )
    assertion = await client.add_memory(
        subject="deploys today",
        predicate="require",
        object="two approvals for production changes",
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.7,
        reference_time=when + timedelta(minutes=15),
    )
    correction_row = client.graph.get_relationship(corrected.corrected_relationship_uuid)
    assertion_row = client.graph.get_relationship(assertion.relationship_uuid)
    assert correction_row.properties["claim_mode"] != assertion_row.properties["claim_mode"]
    assert correction_row.properties["truth_prefix"] != assertion_row.properties["truth_prefix"]

    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))

    for uuid in (
        positive.relationship_uuid,
        negated.relationship_uuid,
        corrected.corrected_relationship_uuid,
        assertion.relationship_uuid,
    ):
        row = client.graph.get_relationship(uuid)
        assert row.properties.get("duplicate_of") is None
        assert row.properties.get("active_in_context") is not False
    assert not [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED
    ]


async def test_threshold_none_disables_the_sweep(tmp_path) -> None:
    scope = _scope("dup-disabled")
    client = _client(tmp_path, cross_prefix_duplicate_threshold=None)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    stronger, weaker = await _duplicate_pair(client, scope, when=when)

    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))

    for uuid in (stronger.relationship_uuid, weaker.relationship_uuid):
        row = client.graph.get_relationship(uuid)
        assert row.properties.get("duplicate_of") is None
        assert row.properties.get("active_in_context") is not False
    assert not [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED
    ]


async def test_repromotion_when_stronger_row_is_superseded(tmp_path) -> None:
    scope = _scope("dup-repromote")
    client = _client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    stronger, weaker = await _duplicate_pair(client, scope, when=when)
    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))
    demoted = client.graph.get_relationship(weaker.relationship_uuid)
    assert demoted.properties["duplicate_of"] == stronger.relationship_uuid

    # Superseding the stronger row (operator correction) re-promotes the duplicate.
    await client.correct_memory(
        relationship_uuid=stronger.relationship_uuid,
        corrected_object="oat milk lattes",
        scope=scope,
        now=when + timedelta(hours=2),
    )

    restored = client.graph.get_relationship(weaker.relationship_uuid)
    assert restored.properties["active_in_context"] is True
    assert restored.properties["duplicate_of"] is None
    assert restored.properties["duplicate_cosine"] is None
    assert restored.properties["repromoted_from_duplicate_of"] == stronger.relationship_uuid
    repromote_receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_DUPLICATE_REPROMOTED
    ]
    assert len(repromote_receipts) == 1
    assert repromote_receipts[0].relationship_uuid == weaker.relationship_uuid
    assert repromote_receipts[0].decision_result == "materialized"
    assert repromote_receipts[0].graph_state_hash_before is not None
    assert repromote_receipts[0].graph_state_hash_after is not None

    profile = await client.profile(scope=scope)
    assert "Priya Sharma likes" in profile.rendered_context


async def test_repromotion_when_stronger_row_is_pruned(tmp_path) -> None:
    scope = _scope("dup-repromote-prune")
    client = _client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    stronger, weaker = await _duplicate_pair(client, scope, when=when)
    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))
    assert (
        client.graph.get_relationship(weaker.relationship_uuid).properties["duplicate_of"] == stronger.relationship_uuid
    )

    await client.forget_memory(
        relationship_uuid=stronger.relationship_uuid,
        scope=scope,
        reason="operator_removed_duplicate_target",
        now=when + timedelta(hours=2),
    )

    restored = client.graph.get_relationship(weaker.relationship_uuid)
    assert restored.properties["active_in_context"] is True
    assert restored.properties["duplicate_of"] is None
    assert any(
        receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_DUPLICATE_REPROMOTED
        and receipt.relationship_uuid == weaker.relationship_uuid
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
    )


async def test_per_run_demotion_cap_respected(tmp_path) -> None:
    scope = _scope("dup-cap")
    client = _client(tmp_path, max_duplicate_demotions_per_run=1)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    rows = []
    for index, (subject, predicate) in enumerate(
        (("Priya", "prefers"), ("Priya Sharma", "likes"), ("Ms Priya Sharma", "enjoys"))
    ):
        result = await client.add_memory(
            subject=subject,
            predicate=predicate,
            object=DUPLICATE_OBJECT,
            relationship_type="PREFERS",
            scope=scope,
            confidence=0.9 - index * 0.1,
            reference_time=when + timedelta(minutes=index),
        )
        rows.append(result)

    await client.run_dream_job(job_name="consolidation", now=when + timedelta(hours=1))

    demoted = [
        row
        for row in (client.graph.get_relationship(item.relationship_uuid) for item in rows)
        if row.properties.get("active_in_context") is False
    ]
    assert len(demoted) == 1
    receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_CROSS_PREFIX_DUPLICATE_DEMOTED
    ]
    assert len(receipts) == 1


# ---------------------------------------------------------------------------
# WS-17 T17b: dedup identifier guard — distinct identifier-bearing objects
# never merge semantically, however high the cosine.
# ---------------------------------------------------------------------------

# The REAL pair from INGEST.md §4a: two DISTINCT Vertex AI Search data stores
# whose trigram cosine (0.895, measured) clears the 0.88 default threshold.
KB_DS_WDW = "kb_ds_source_plandisney_pocket_guides_wdw_en_us_v1"
KB_DS_DLR = "kb_ds_source_plandisney_pocket_guides_dlr_en_us_v1"


def _dedup_client(tmp_path, *, cosine_threshold: float | None = None, name: str = "graph.sqlite") -> Memotron:
    from memotron import DedupPolicy

    updates: dict = {
        "jobs": (DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),),
    }
    if cosine_threshold is not None:
        updates["dedup"] = DedupPolicy(cosine_threshold=cosine_threshold)
    return Memotron(config=default_config().model_copy(update=updates), graph_path=tmp_path / name)


async def test_identifier_guard_canary_distinct_kb_ds_ids_never_reinforce(tmp_path) -> None:
    """The measured kb_ds WDW/DLR pair stays two ACTIVE rows at the default 0.88."""
    from memotron.embedding import LocalEmbeddingTransport, cosine_similarity

    # Honest precondition: the hermetic transport really does put this real
    # pair ABOVE the default threshold — the guard, not the threshold, is what
    # keeps them apart.  If the transport ever changes, re-verify the canary.
    local = LocalEmbeddingTransport()
    observed = cosine_similarity(local.embed(KB_DS_WDW), local.embed(KB_DS_DLR))
    assert observed > 0.88, "canary pair no longer clears the default threshold"

    scope = _scope("kb-ds-canary")
    client = _dedup_client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    first = await client.add_memory(
        subject="Pocket Guides",
        predicate="is backed by",
        object=KB_DS_WDW,
        relationship_type="PREFERS",  # multi_active: object rides in the truth_key
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    second = await client.add_memory(
        subject="Pocket Guides",
        predicate="is backed by",
        object=KB_DS_DLR,
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(minutes=5),
    )

    first_row = client.graph.get_relationship(first.relationship_uuid)
    second_row = client.graph.get_relationship(second.relationship_uuid)
    assert first_row.uuid != second_row.uuid
    assert first_row.properties["status"] == "active"
    assert second_row.properties["status"] == "active"
    assert first_row.properties["observed_count"] == 1
    assert second_row.properties["observed_count"] == 1

    receipts = client.graph.receipts.receipts_for_scope(scope.key)
    assert not [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.FORMATION_SEMANTIC_DEDUP_REINFORCED
    ]
    # The refusal is surfaced on the second create disposition.
    refused = [
        receipt
        for receipt in receipts
        if receipt.decision_type == ReceiptDecisionType.FORMATION_RELATIONSHIP_CREATED
        and receipt.decision_reason == "materialized:identifier_token_mismatch"
    ]
    assert len(refused) == 1
    assert refused[0].relationship_uuid == second.relationship_uuid
    assert refused[0].dedup_match_relationship_uuid == first.relationship_uuid
    assert refused[0].dedup_score == pytest.approx(observed)
    assert refused[0].dedup_threshold == pytest.approx(0.88)


async def test_identifier_free_paraphrase_still_reinforces(tmp_path) -> None:
    """Positive control: the guard does not touch identifier-free paraphrases."""
    scope = _scope("paraphrase-control")
    client = _dedup_client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    first = await client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="dark roast coffee in the morning",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    second = await client.add_memory(
        subject="Priya",
        predicate="prefers",
        object="dark roast coffee in the mornings",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(minutes=5),
    )
    assert second.reinforced_relationships == 1
    row = client.graph.get_relationship(first.relationship_uuid)
    assert row.properties["observed_count"] == 2


async def test_same_identifier_token_pair_still_reinforces(tmp_path) -> None:
    """Identical identifier sets are corroboration: the semantic merge proceeds."""
    scope = _scope("same-id-control")
    client = _dedup_client(tmp_path)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    first = await client.add_memory(
        subject="Pocket Guides",
        predicate="is backed by",
        object=f"{KB_DS_WDW} primary data store",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    second = await client.add_memory(
        subject="Pocket Guides",
        predicate="is backed by",
        object=f"{KB_DS_WDW} the primary data store",
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(minutes=5),
    )
    assert second.reinforced_relationships == 1
    row = client.graph.get_relationship(first.relationship_uuid)
    assert row.properties["observed_count"] == 2


async def test_identifier_guard_blocks_corroboration_sibling_matching(tmp_path) -> None:
    """A parked challenger with a DIFFERENT identifier never corroborates (WS-16 path)."""
    from memotron.embedding import LocalEmbeddingTransport, cosine_similarity

    incumbent_object = "manual supervisor approval before rollout"
    challenger_a = "kb_ds_source_zendesk_call_center_en_us_v1"
    challenger_b = "kb_ds_source_dscribe_photopass_page_aulani_en_us_v1"
    local = LocalEmbeddingTransport()
    # Honest preconditions at the 0.4 test threshold: the two challengers WOULD
    # cross-corroborate on cosine alone; neither reinforces the incumbent.
    assert cosine_similarity(local.embed(challenger_a), local.embed(challenger_b)) >= 0.4
    assert cosine_similarity(local.embed(incumbent_object), local.embed(challenger_a)) < 0.4
    assert cosine_similarity(local.embed(incumbent_object), local.embed(challenger_b)) < 0.4

    scope = _scope("corroboration-guard")
    client = _dedup_client(tmp_path, cosine_threshold=0.4)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    incumbent = None
    for observation in range(3):  # observed_count reaches corroboration_margin=3
        result = await client.add_memory(
            subject="Release process",
            predicate="requires",
            object=incumbent_object,
            relationship_type="REQUIRES",  # single_active
            scope=scope,
            confidence=0.9,
            reference_time=when + timedelta(minutes=observation),
        )
        incumbent = incumbent or result
    parked_a = await client.add_memory(
        subject="Release process",
        predicate="requires",
        object=challenger_a,
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(hours=1),
    )
    parked_b = await client.add_memory(
        subject="Release process",
        predicate="requires",
        object=challenger_b,
        relationship_type="REQUIRES",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(hours=2),
    )

    # Without the guard, B would count A as its corroborating sibling
    # (2 >= corroboration_required=2) and flip the incumbent.  With the guard,
    # BOTH challengers stay parked and current truth survives.
    incumbent_row = client.graph.get_relationship(incumbent.relationship_uuid)
    assert incumbent_row.properties["status"] == "active"
    for parked in (parked_a, parked_b):
        row = client.graph.get_relationship(parked.relationship_uuid)
        assert row.properties["status"] == "superseded"
        assert row.properties["requires_operator_review"] is True
        assert str(row.properties["supersession_gate_reason"]).startswith("insufficient_corroboration")


async def test_identifier_guard_blocks_polarity_conflict_cosine_path(tmp_path) -> None:
    """A negated statement about a DIFFERENT identifier is not a polarity conflict."""
    from memotron.embedding import LocalEmbeddingTransport, cosine_similarity

    wdw_offers = "kb_ds_source_dscribe_special_offers_wdw_en_us_v1"
    negated_dlr = "not kb_ds_source_dscribe_special_offers_dlr_en_us_v1"
    local = LocalEmbeddingTransport()
    # Honest precondition: at the 0.5 test threshold the cosine path WOULD see
    # a conflict — only the identifier guard separates the two data stores.
    assert cosine_similarity(local.embed(wdw_offers), local.embed(negated_dlr)) >= 0.5

    scope = _scope("polarity-guard")
    client = _dedup_client(tmp_path, cosine_threshold=0.5)
    when = datetime(2026, 7, 20, tzinfo=UTC)
    positive = await client.add_memory(
        subject="Special offers",
        predicate="flows into",
        object=wdw_offers,
        relationship_type="PREFERS",  # multi_active
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )
    negated = await client.add_memory(
        subject="Special offers",
        predicate="flows into",
        object=negated_dlr,
        relationship_type="PREFERS",
        scope=scope,
        confidence=0.9,
        reference_time=when + timedelta(minutes=5),
    )
    # Different identifiers: NOT the same statement — both coexist unchanged.
    assert client.graph.get_relationship(positive.relationship_uuid).properties["status"] == "active"
    assert client.graph.get_relationship(negated.relationship_uuid).properties["status"] == "active"

    # Control: the SAME identifier negated IS a polarity conflict and supersedes.
    control_scope = _scope("polarity-guard-control")
    control_positive = await client.add_memory(
        subject="Special offers",
        predicate="flows into",
        object=wdw_offers,
        relationship_type="PREFERS",
        scope=control_scope,
        confidence=0.9,
        reference_time=when,
    )
    await client.add_memory(
        subject="Special offers",
        predicate="flows into",
        object=f"not {wdw_offers}",
        relationship_type="PREFERS",
        scope=control_scope,
        confidence=0.9,
        reference_time=when + timedelta(minutes=5),
    )
    control_row = client.graph.get_relationship(control_positive.relationship_uuid)
    assert control_row.properties["status"] == "superseded"
