"""WS-17 T16: predicate canonicalization — truth-slot bridging.

Pins the T16 contracts:

- Truth keys are built on the CANONICAL predicate, so a correction stated with a
  paraphrased predicate ("resides in") SUPERSEDES the incumbent stated with the
  original surface ("lives in") — the AUDIT §1 headline case.
- Resolution is deterministic and first-wins: registry hit, then operator
  synonym map, then embedding cosine against the scope's existing canonicals
  (active transport, embedded fresh — never cross-space), else self-canonical.
- A NEW non-identity mapping is receipted (FORMATION_PREDICATE_CANONICALIZED).
- ``enabled=False`` restores byte-identical legacy behavior.
- ``canonicalize_scope_predicates`` backfills ACTIVE rows only (receipted,
  hash-bracketed per row); the existing single-active repair collapses the
  now-colliding slots on its next pruning run.
- The hermetic local transport does NOT clear the 0.85 threshold for the
  morphological pair ("lives in"/"living in") — verified honestly, not faked;
  the embedding path is therefore exercised with a deterministic stub.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    DreamJob,
    DreamJobKind,
    Memotron,
    LocalEmbeddingTransport,
    MemoryScope,
    PredicateCanonicalizationPolicy,
    ScopeKind,
)
from memotron.config import default_config
from memotron.embedding import cosine_similarity
from memotron.models import RelationshipStatus
from memotron.receipts import ReceiptDecisionType


class PredicateBucketTransport:
    """Deterministic stub: paraphrase-family predicates share one axis.

    Residence-flavoured predicates ("lives in", "resides in", "living in") all
    land on axis 0 (cosine 1.0 between any two — controlled, above any
    threshold); other texts land on hash buckets of the remaining axes.
    """

    identifier = "predicate-bucket-8@v1"

    _RESIDENCE_MARKERS = ("lives", "living", "resides", "residence")

    def embed(self, text: str) -> list[float]:
        normalized = text.casefold()
        vector = [0.0] * 8
        if any(marker in normalized for marker in self._RESIDENCE_MARKERS):
            vector[0] = 1.0
        else:
            bucket = 1 + (sum(normalized.encode("utf-8")) % 7)
            vector[bucket] = 1.0
        norm = math.sqrt(sum(x * x for x in vector))
        return [x / norm for x in vector]


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _client(tmp_path, *, policy: PredicateCanonicalizationPolicy | None = None, **kwargs) -> Memotron:
    config = default_config()
    if policy is not None:
        config = config.model_copy(update={"predicate_canonicalization": policy})
    return Memotron(config=config, graph_path=tmp_path / "graph.sqlite", **kwargs)


async def _residence_fact(client: Memotron, scope: MemoryScope, *, predicate: str, city: str, when: datetime):
    return await client.add_memory(
        subject="Priya",
        predicate=predicate,
        object=city,
        relationship_type="REQUIRES",  # single_active: one residence truth slot
        scope=scope,
        confidence=0.9,
        reference_time=when,
    )


async def test_synonym_map_bridges_paraphrased_correction(tmp_path) -> None:
    """AUDIT §1 headline: 'resides in' supersedes the 'lives in' incumbent."""
    scope = _scope("headline")
    client = _client(
        tmp_path,
        policy=PredicateCanonicalizationPolicy(synonyms={"resides in": "lives in"}),
    )
    start = datetime(2026, 7, 1, tzinfo=UTC)
    incumbent = await _residence_fact(client, scope, predicate="lives in", city="Seattle", when=start)
    correction = await _residence_fact(
        client, scope, predicate="resides in", city="Portland", when=start + timedelta(days=3)
    )

    old = client.graph.get_relationship(incumbent.relationship_uuid)
    new = client.graph.get_relationship(correction.relationship_uuid)
    assert old.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert old.properties["superseded_by_relationship_uuid"] == correction.relationship_uuid
    assert new.properties["status"] == RelationshipStatus.ACTIVE.value
    # Same truth slot (bridged), surface predicates preserved.
    assert new.properties["truth_key"] == old.properties["truth_key"]
    assert new.properties["predicate"] == "resides in"
    assert new.properties["predicate_canonical"] == "lives in"
    assert old.properties["predicate"] == "lives in"

    current = await client.search(query="Priya", scope=scope)
    assert [(item.predicate, item.object) for item in current] == [("resides in", "Portland")]

    # The non-identity mapping is receipted with surface->canonical.
    receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED
    ]
    assert len(receipts) == 1
    assert receipts[0].decision_reason.startswith("resides in->lives in")
    assert receipts[0].decision_result == "recorded"


async def test_embedding_path_maps_paraphrase_and_receipts(tmp_path) -> None:
    """No synonym map: the stub transport's controlled cosine does the bridging."""
    scope = _scope("embedding-path")
    client = _client(
        tmp_path,
        policy=PredicateCanonicalizationPolicy(embedding_threshold=0.85),
        embedding_transport=PredicateBucketTransport(),
    )
    start = datetime(2026, 7, 1, tzinfo=UTC)
    incumbent = await _residence_fact(client, scope, predicate="lives in", city="Seattle", when=start)
    correction = await _residence_fact(
        client, scope, predicate="resides in", city="Portland", when=start + timedelta(days=3)
    )

    old = client.graph.get_relationship(incumbent.relationship_uuid)
    new = client.graph.get_relationship(correction.relationship_uuid)
    assert old.properties["status"] == RelationshipStatus.SUPERSEDED.value
    assert new.properties["predicate_canonical"] == "lives in"
    assert client.graph.canonical_predicate_for(scope.key, "resides in") == "lives in"

    receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED
    ]
    assert len(receipts) == 1
    assert receipts[0].decision_reason.startswith("resides in->lives in:cosine=1.0000")
    assert receipts[0].dedup_score == pytest.approx(1.0)
    assert receipts[0].embedding_identifier == "predicate-bucket-8@v1"


async def test_first_wins_stability(tmp_path) -> None:
    """Arrival order decides the canonical; same inputs produce the same registry."""
    start = datetime(2026, 7, 1, tzinfo=UTC)
    policy = PredicateCanonicalizationPolicy(embedding_threshold=0.85)

    forward_scope = _scope("first-wins")
    forward = Memotron(
        config=default_config().model_copy(update={"predicate_canonicalization": policy}),
        graph_path=tmp_path / "forward.sqlite",
        embedding_transport=PredicateBucketTransport(),
    )
    await _residence_fact(forward, forward_scope, predicate="lives in", city="Seattle", when=start)
    await _residence_fact(
        forward, forward_scope, predicate="resides in", city="Portland", when=start + timedelta(days=1)
    )
    assert forward.graph.canonical_predicate_for(forward_scope.key, "lives in") == "lives in"
    assert forward.graph.canonical_predicate_for(forward_scope.key, "resides in") == "lives in"

    # Reversed arrival order in a fresh store: "resides in" wins the election.
    reverse_scope = _scope("first-wins")
    reverse = Memotron(
        config=default_config().model_copy(update={"predicate_canonicalization": policy}),
        graph_path=tmp_path / "reverse.sqlite",
        embedding_transport=PredicateBucketTransport(),
    )
    await _residence_fact(reverse, reverse_scope, predicate="resides in", city="Portland", when=start)
    await _residence_fact(reverse, reverse_scope, predicate="lives in", city="Seattle", when=start + timedelta(days=1))
    assert reverse.graph.canonical_predicate_for(reverse_scope.key, "resides in") == "resides in"
    assert reverse.graph.canonical_predicate_for(reverse_scope.key, "lives in") == "resides in"

    # Same inputs, same registry: replaying the forward order lands identically.
    replay_scope = _scope("first-wins")
    replay = Memotron(
        config=default_config().model_copy(update={"predicate_canonicalization": policy}),
        graph_path=tmp_path / "replay.sqlite",
        embedding_transport=PredicateBucketTransport(),
    )
    await _residence_fact(replay, replay_scope, predicate="lives in", city="Seattle", when=start)
    await _residence_fact(replay, replay_scope, predicate="resides in", city="Portland", when=start + timedelta(days=1))
    assert replay.graph.canonical_predicate_for(replay_scope.key, "lives in") == "lives in"
    assert replay.graph.canonical_predicate_for(replay_scope.key, "resides in") == "lives in"


async def test_disabled_policy_is_byte_identical_legacy(tmp_path) -> None:
    """enabled=False reproduces the pre-T16 split-slot behavior exactly."""
    scope = _scope("legacy")
    client = _client(tmp_path, policy=PredicateCanonicalizationPolicy(enabled=False))
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first = await _residence_fact(client, scope, predicate="lives in", city="Seattle", when=start)
    second = await _residence_fact(
        client, scope, predicate="resides in", city="Portland", when=start + timedelta(days=3)
    )

    old = client.graph.get_relationship(first.relationship_uuid)
    new = client.graph.get_relationship(second.relationship_uuid)
    # The documented AUDIT gap: both stay ACTIVE on split truth slots.
    assert old.properties["status"] == RelationshipStatus.ACTIVE.value
    assert new.properties["status"] == RelationshipStatus.ACTIVE.value
    assert old.properties["truth_key"] != new.properties["truth_key"]
    # Legacy row shape: no canonical stamp, no registry rows, no receipts.
    assert "predicate_canonical" not in old.properties
    assert "predicate_canonical" not in new.properties
    assert client.graph.canonical_predicate_for(scope.key, "lives in") is None
    assert client.graph.canonical_predicates_for_scope(scope.key) == []
    assert not [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED
    ]


async def test_backfill_then_single_active_repair_collapses(tmp_path) -> None:
    """Backfill rewrites ACTIVE truth keys; the next pruning run collapses the slot."""
    scope = _scope("backfill")
    now = datetime.now(UTC)
    legacy = _client(tmp_path, policy=PredicateCanonicalizationPolicy(enabled=False))
    incumbent = await _residence_fact(
        legacy, scope, predicate="lives in", city="Seattle", when=now - timedelta(hours=2)
    )
    # A historical row on the incumbent slot: superseded rows are frozen lineage.
    replaced = await _residence_fact(legacy, scope, predicate="lives in", city="Tacoma", when=now - timedelta(hours=3))
    newer = await _residence_fact(legacy, scope, predicate="resides in", city="Portland", when=now - timedelta(hours=1))
    historical = legacy.graph.get_relationship(replaced.relationship_uuid)
    assert historical.properties["status"] == RelationshipStatus.SUPERSEDED.value
    historical_truth_key = historical.properties["truth_key"]
    legacy.graph.close()

    config = default_config().model_copy(
        update={
            "predicate_canonicalization": PredicateCanonicalizationPolicy(synonyms={"resides in": "lives in"}),
            "jobs": (DreamJob(name="pruning", kind=DreamJobKind.PRUNING, cadence_seconds=1),),
        }
    )
    client = Memotron(config=config, graph_path=tmp_path / "graph.sqlite")
    result = await client.canonicalize_scope_predicates(scope=scope)
    assert result["scanned"] == 2  # ACTIVE rows only — the superseded row is untouched
    assert result["rewritten_count"] == 2

    bridged_incumbent = client.graph.get_relationship(incumbent.relationship_uuid)
    bridged_newer = client.graph.get_relationship(newer.relationship_uuid)
    assert bridged_incumbent.properties["truth_key"] == bridged_newer.properties["truth_key"]
    assert bridged_newer.properties["predicate_canonical"] == "lives in"
    assert bridged_newer.properties["predicate"] == "resides in"
    # Historical rows are NEVER rewritten.
    untouched = client.graph.get_relationship(replaced.relationship_uuid)
    assert untouched.properties["truth_key"] == historical_truth_key
    assert "predicate_canonical" not in untouched.properties

    # Per-row backfill receipts are hash-bracketed.
    backfill_receipts = [
        receipt
        for receipt in client.graph.receipts.receipts_for_scope(scope.key)
        if receipt.decision_type == ReceiptDecisionType.FORMATION_PREDICATE_CANONICALIZED
        and receipt.decision_reason.startswith("backfill:")
    ]
    assert {receipt.relationship_uuid for receipt in backfill_receipts} == {
        incumbent.relationship_uuid,
        newer.relationship_uuid,
    }
    for receipt in backfill_receipts:
        assert receipt.graph_state_hash_before is not None
        assert receipt.graph_state_hash_after is not None

    # Composition: the existing single-active repair collapses the collided slot.
    # (With the default superseded_retention_seconds=0 the repaired loser is
    # additionally retired to the archive tier within the same pruning run.)
    await client.run_dream_job(job_name="pruning")
    repaired = client.graph.get_relationship(incumbent.relationship_uuid)
    winner = client.graph.get_relationship(newer.relationship_uuid)
    assert repaired.properties["status"] != RelationshipStatus.ACTIVE.value
    assert repaired.properties["superseded_reason"] == "single_active_truth_repair"
    assert repaired.properties["superseded_by_relationship_uuid"] == newer.relationship_uuid
    assert winner.properties["status"] == RelationshipStatus.ACTIVE.value
    current = await client.search(query="Portland residence", scope=scope)
    assert [(item.predicate, item.object) for item in current] == [("resides in", "Portland")]


async def test_hermetic_morphological_pair_does_not_fake_the_threshold(tmp_path) -> None:
    """The local trigram transport does NOT clear 0.85 for 'lives in'/'living in'.

    Verified empirically instead of faked: under the hermetic transport the
    morphological paraphrase stays below the threshold, so the two surfaces
    remain separate slots (each self-canonical) — which is exactly why the
    embedding-path test above uses the deterministic stub transport.
    """
    local = LocalEmbeddingTransport()
    observed = cosine_similarity(local.embed("lives in"), local.embed("living in"))
    assert observed < 0.85, (
        "the hermetic transport now clears the threshold; move the morphological "
        "case onto the local-transport bridging path"
    )

    scope = _scope("morphological")
    client = _client(tmp_path, policy=PredicateCanonicalizationPolicy(embedding_threshold=0.85))
    start = datetime(2026, 7, 1, tzinfo=UTC)
    first = await _residence_fact(client, scope, predicate="lives in", city="Seattle", when=start)
    second = await _residence_fact(
        client, scope, predicate="living in", city="Portland", when=start + timedelta(days=1)
    )
    assert client.graph.canonical_predicate_for(scope.key, "lives in") == "lives in"
    assert client.graph.canonical_predicate_for(scope.key, "living in") == "living in"
    old = client.graph.get_relationship(first.relationship_uuid)
    new = client.graph.get_relationship(second.relationship_uuid)
    assert old.properties["status"] == RelationshipStatus.ACTIVE.value
    assert new.properties["status"] == RelationshipStatus.ACTIVE.value
    assert old.properties["truth_key"] != new.properties["truth_key"]
