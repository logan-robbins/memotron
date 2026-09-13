from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from memotron import DreamJob, DreamJobKind, Memotron, MemoryScope, ScopeKind
from memotron.config import RollupConsolidationPolicy, default_config
from memotron.dreaming import ROLLUP_TEXT_SOURCE_LLM
from memotron.receipts import ReceiptDecisionType


class _GroupedEmbeddingTransport:
    """Three separable raw clusters; tests later make parent rollups co-cluster."""

    # WS-17 T18: the EmbeddingTransport protocol requires a vector-space
    # identifier; rows this transport writes are stamped with it, so its own
    # reads pass the space guard.
    identifier = "grouped-13@v1"

    def embed(self, text: str) -> list[float]:
        normalized = text.lower()
        if "corrected" in normalized:
            return [0.0] * 12 + [1.0]
        group_offset = 0 if "group-a" in normalized else 4 if "group-b" in normalized else 8
        if "revised" in normalized:
            # Similar enough to remain in its original rollup cluster, but not
            # similar enough to semantically dedup into the old assertion.
            revised_index = next((index for index in range(2) if f" {index}" in normalized), 0)
            vector = [0.0] * 12
            vector[group_offset] = 0.8
            vector[4 + revised_index] = 0.6
            return vector
        item_index = next((index for index in range(3) if f" {index}" in normalized), 0)
        vector = [0.0] * 12
        vector[group_offset] = 0.8
        vector[group_offset + item_index + 1] = 0.6
        return vector


def _rollup_client(tmp_path, *, synthesis_transport=None) -> Memotron:
    base = default_config()
    config = base.model_copy(
        update={
            "jobs": (
                DreamJob(
                    name="rollups",
                    kind=DreamJobKind.CONSOLIDATION,
                    cadence_seconds=1,
                    rollup_consolidation=True,
                    rollup_consolidation_policy=RollupConsolidationPolicy(
                        cluster_threshold=0.6,
                        min_cluster_size=3,
                        max_depth=2,
                    ),
                ),
            )
        }
    )
    return Memotron(
        graph_path=tmp_path / "graph.sqlite",
        config=config,
        embedding_transport=_GroupedEmbeddingTransport(),
        rollup_synthesis_transport=synthesis_transport,
    )


@pytest.mark.asyncio
async def test_recursive_rollups_create_depth_two_and_child_correction_invalidates_parent(tmp_path) -> None:
    client = _rollup_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollup-dependencies")
    when = datetime(2026, 7, 15, tzinfo=UTC)
    raw_ids: list[str] = []
    for group in ("group-a", "group-b", "group-c"):
        for index in range(3):
            result = await client.add_memory(
                subject="Acme",
                predicate="prefers",
                object=f"{group} operating preference {index}",
                relationship_type="PREFERS",
                scope=scope,
                valid_from=when,
            )
            raw_ids.append(result.relationship_uuid)

    await client.run_dream_job(job_name="rollups", now=when)
    depth_one = [
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 1
    ]
    assert len(depth_one) == 3
    for rollup in depth_one:
        # Make the three depth-1 views eligible for a shared depth-2 cluster.
        client.graph.update_relationship(
            rollup.uuid,
            properties={"embedding": [1.0] + [0.0] * 11, "object_embedding": [1.0] + [0.0] * 11},
        )

    await client.run_dream_job(job_name="rollups", now=when)
    depth_two = [
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 2
    ]
    assert len(depth_two) == 1
    assert depth_two[0].properties["rollup_dependency_digest"]
    assert depth_two[0].properties["rollup_synthesis_profile"] == "rollup-synthesis@v1"

    correction = await client.correct_memory(
        relationship_uuid=raw_ids[0],
        corrected_object="group-a corrected operating preference",
        reason="operator correction",
        now=when,
    )
    direct_parent = next(rollup for rollup in depth_one if raw_ids[0] in rollup.properties["derived_from"])
    parent = client.graph.get_relationship(direct_parent.uuid)
    raw = client.graph.get_relationship(raw_ids[0])
    corrected = client.graph.get_relationship(correction.corrected_relationship_uuid)
    assert parent.properties["rollup_stale"] is True
    assert parent.properties["active_in_context"] is False
    assert raw.properties["active_in_context"] is False
    assert corrected.properties["active_in_context"] is True
    assert corrected.properties["repromoted_from_stale_rollup"] == parent.uuid


@pytest.mark.asyncio
async def test_rollup_reinforcement_updates_evidence_without_rebuilding_until_material(tmp_path) -> None:
    client = _rollup_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollup-materiality")
    when = datetime(2026, 7, 15, tzinfo=UTC)
    for index in range(3):
        await client.add_memory(
            subject="Acme",
            predicate="prefers",
            object=f"group-a operating preference {index}",
            relationship_type="PREFERS",
            scope=scope,
            valid_from=when,
        )
    await client.run_dream_job(job_name="rollups", now=when)
    rollup = next(
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 1
    )
    child_uuid = rollup.properties["derived_from"][0]
    child = client.graph.get_relationship(child_uuid)
    object_value = str(child.properties["object"])

    # Three new corroborations cross the default materiality threshold.  Until
    # then the rollup remains a live view and only its evidence aggregate moves.
    for _ in range(2):
        await client.add_memory(
            subject="Acme",
            predicate="prefers",
            object=object_value,
            relationship_type="PREFERS",
            scope=scope,
            valid_from=when,
        )
    current = client.graph.get_relationship(rollup.uuid)
    assert current.properties.get("rollup_stale") is not True
    assert current.properties["active_in_context"] is True
    assert current.properties["rollup_evidence_aggregates"]["observed_count_total"] == 5

    await client.add_memory(
        subject="Acme",
        predicate="prefers",
        object=object_value,
        relationship_type="PREFERS",
        scope=scope,
        valid_from=when,
    )
    current = client.graph.get_relationship(rollup.uuid)
    assert current.properties["rollup_stale"] is True
    assert current.properties["active_in_context"] is False
    receipt_types = {receipt.decision_type for receipt in client.graph.receipts.receipts_for_scope(scope.key)}
    assert ReceiptDecisionType.CONSOLIDATION_ROLLUP_EVIDENCE_UPDATED in receipt_types
    assert ReceiptDecisionType.CONSOLIDATION_ROLLUP_MATERIAL_REINFORCEMENT in receipt_types


@pytest.mark.asyncio
async def test_batched_child_corrections_coalesce_to_one_dependency_ordered_rollup_rebuild(tmp_path) -> None:
    client = _rollup_client(tmp_path)
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollup-coalescing")
    when = datetime(2026, 7, 15, tzinfo=UTC)
    raw_ids: list[str] = []
    for index in range(3):
        result = await client.add_memory(
            subject="Acme",
            predicate="prefers",
            object=f"group-a operating preference {index}",
            relationship_type="PREFERS",
            scope=scope,
            valid_from=when,
        )
        raw_ids.append(result.relationship_uuid)
    await client.run_dream_job(job_name="rollups", now=when)
    prior = next(
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 1
    )

    # Both changes arrive before one maintenance pass.  They resolve to the
    # same logical child set, so the stale parent has one replacement, not two.
    for index in (0, 1):
        original = client.graph.get_relationship(raw_ids[index])
        correction = await client.correct_memory(
            relationship_uuid=raw_ids[index],
            corrected_object=f"group-a operating preference revised {index}",
            reason="operator correction",
            now=when,
        )
        # The correctness gate intentionally refuses to collapse a correction
        # with an ordinary preference into a false consensus.  This fixture is
        # specifically about the dependency scheduler, so model the reviewed
        # successor as the same preference claim once its correction has been
        # accepted by the control plane.
        client.graph.update_relationship(
            correction.corrected_relationship_uuid,
            properties={
                "claim_mode": original.properties["claim_mode"],
                "directive_stance": original.properties["directive_stance"],
            },
        )
    corrected_children = {
        relationship.uuid
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") != "rollup"
        and relationship.properties.get("status") == "active"
        and relationship.type != "MENTIONS"
    }
    await client.run_dream_job(job_name="rollups", now=when)

    # The deterministic rollup text is STRUCTURAL — "Acme prefers: 3 values" —
    # so two object-level corrections leave it byte-identical and the batch
    # lands on the content-unchanged branch: the stale parent is revived in
    # place rather than superseded by a twin.  What this test pins either way
    # is the coalescing invariant: one batch of child corrections resolves to
    # ONE rebuild identity, never one per corrected child, and the revived view
    # points at the corrected children.
    prior_after = client.graph.get_relationship(prior.uuid)
    assert prior_after.properties["status"] == "active"
    assert prior_after.properties["rollup_stale"] is False
    assert prior_after.properties["active_in_context"] is True
    assert prior_after.properties["rollup_version"] == 2
    assert set(prior_after.properties["derived_from"]) == corrected_children
    depth_one = [
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 1
    ]
    assert [relationship.uuid for relationship in depth_one] == [prior.uuid]
    rebuilds = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_ROLLUP_RECOMPUTED_NOOP
        and receipt.relationship_uuid == prior.uuid
    ]
    assert len(rebuilds) == 1


class _LexicographicSynthesisTransport:
    """Content-derived rollup text: the lexicographically last member fact.

    A verbatim member fact passes the entailment gate by construction, so this
    stands in for the production (gateway) configuration where the rollup text
    tracks its children's content — the condition under which a child
    correction really does change the parent's text.
    """

    identifier = "stub-synthesis:lexicographic@v1"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        facts = [line.split(". ", 1)[1] for line in prompt.splitlines() if line[:1].isdigit() and ". " in line]
        return json.dumps({"summary": max(facts)})


@pytest.mark.asyncio
async def test_batched_child_corrections_supersede_the_parent_when_the_rollup_text_changes(
    tmp_path,
) -> None:
    """The other rebuild branch: changed text ⇒ one superseding replacement.

    Same batch, same coalescing requirement — but with a content-derived rollup
    text the corrections DO change what the parent says, so the stale view is
    superseded with full lineage instead of revived in place.
    """
    client = _rollup_client(tmp_path, synthesis_transport=_LexicographicSynthesisTransport())
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="rollup-coalescing-superseded")
    when = datetime(2026, 7, 15, tzinfo=UTC)
    raw_ids: list[str] = []
    for index in range(3):
        result = await client.add_memory(
            subject="Acme",
            predicate="prefers",
            object=f"group-a operating preference {index}",
            relationship_type="PREFERS",
            scope=scope,
            valid_from=when,
        )
        raw_ids.append(result.relationship_uuid)
    await client.run_dream_job(job_name="rollups", now=when)
    prior = next(
        relationship
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("memory_type") == "rollup"
        and relationship.properties.get("rollup_depth") == 1
    )
    assert prior.properties["rollup_text_source"] == ROLLUP_TEXT_SOURCE_LLM

    for index in (0, 1):
        original = client.graph.get_relationship(raw_ids[index])
        correction = await client.correct_memory(
            relationship_uuid=raw_ids[index],
            corrected_object=f"group-a operating preference revised {index}",
            reason="operator correction",
            now=when,
        )
        client.graph.update_relationship(
            correction.corrected_relationship_uuid,
            properties={
                "claim_mode": original.properties["claim_mode"],
                "directive_stance": original.properties["directive_stance"],
            },
        )
    await client.run_dream_job(job_name="rollups", now=when)

    prior_after = client.graph.get_relationship(prior.uuid)
    assert prior_after.properties["status"] == "superseded"
    replacement_uuid = prior_after.properties["superseded_by_relationship_uuid"]
    replacement = client.graph.get_relationship(replacement_uuid)
    assert replacement.properties["rollup_depth"] == 1
    # The fixture is only meaningful while the text actually moves.
    assert replacement.properties["object"] != prior.properties["object"]
    assert replacement.properties["rollup_text_source"] == ROLLUP_TEXT_SOURCE_LLM
    rebuilds = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.CONSOLIDATION_ROLLUP_RECOMPUTED
        and receipt.relationship_uuid == prior.uuid
    ]
    assert len(rebuilds) == 1
    assert rebuilds[0].successor_relationship_uuid == replacement.uuid
