from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from memotron import (
    ContentKeyUnavailableError,
    DreamJob,
    DreamJobKind,
    Memotron,
    ErasureBehavior,
    GovernancePolicy,
    GrowthPolicy,
    MemoryScope,
    PiiSensitivity,
    ScopeKind,
)
from memotron.config import PruningPolicy, RetentionPolicy, default_config
from memotron.models import ArchivedMatchDisposition, RelationshipStatus
from memotron.receipts import ReceiptDecisionType


def _config_with_pruning(
    pruning: PruningPolicy,
    *,
    crypto_shred: bool = False,
    pure_read_retrieval: bool = False,
):
    base = default_config()
    update = {
        "jobs": (DreamJob(name="prune", kind=DreamJobKind.PRUNING, cadence_seconds=1),),
        "pruning": pruning,
        "pure_read_retrieval": pure_read_retrieval,
    }
    if crypto_shred:
        update["governance"] = GovernancePolicy(
            pii_sensitivity=PiiSensitivity.NONE,
            erasure_behavior=ErasureBehavior.CRYPTO_SHRED,
        )
    return base.model_copy(update=update)


async def _client_with_one_archived_memory(
    tmp_path,
    *,
    scope: MemoryScope,
    pure_read_retrieval: bool = False,
    crypto_shred: bool = False,
):
    """A client whose scope holds one active row and one archived (ghosted) row.

    The archived row's fact contains "vendor invoices"; the surviving row does
    not.  Shared by every archive-tier test in this module so the two retrieval
    modes are asserted against an identical starting state.
    """
    client = Memotron(
        graph_path=tmp_path / "graph.sqlite",
        config=_config_with_pruning(
            PruningPolicy(
                min_confidence=0.0,
                growth=GrowthPolicy(soft_cap=1),
                retention=RetentionPolicy(protected_memory_types=()),
            ),
            crypto_shred=crypto_shred,
            pure_read_retrieval=pure_read_retrieval,
        ),
    )
    archived = await client.add_memory(
        subject="Runbook",
        predicate="prefers",
        object="reconcile vendor invoices monthly",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
    )
    await client.add_memory(
        subject="Runbook",
        predicate="prefers",
        object="summarize status each morning",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 14, tzinfo=UTC),
    )
    await client.run_dream_job(job_name="prune", now=datetime(2026, 7, 15, tzinfo=UTC))
    return client, archived


@pytest.mark.asyncio
async def test_raw_secret_is_rejected_before_persistence_and_reference_survives_cap(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="secret-user")
    client = Memotron(
        graph_path=tmp_path / "graph.sqlite",
        config=_config_with_pruning(
            PruningPolicy(
                min_confidence=0.0,
                growth=GrowthPolicy(soft_cap=1),
                retention=RetentionPolicy(protected_memory_types=()),
            )
        ),
    )

    with pytest.raises(ValueError, match="credential"):
        await client.add_memory(
            subject="Production service",
            predicate="password",
            object="hunter2",
            relationship_type="PREFERS",
            scope=scope,
        )
    assert client.graph.episodes() == []
    rejection = await client.memory_receipts(scope=scope)
    assert rejection[-1].decision_type == ReceiptDecisionType.FORMATION_RAW_SECRET_REJECTED
    assert rejection[-1].sensitive_payload is None

    secret = await client.add_memory(
        subject="Production service",
        predicate="password location",
        object="vault://production/service-password",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 15, tzinfo=UTC),
    )
    ordinary = await client.add_memory(
        subject="Production service",
        predicate="prefers",
        object="short incident reports",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 15, tzinfo=UTC),
    )
    await client.run_dream_job(job_name="prune", now=datetime(2026, 7, 15, tzinfo=UTC))

    secret_relationship = client.graph.get_relationship(secret.relationship_uuid)
    ordinary_relationship = client.graph.get_relationship(ordinary.relationship_uuid)
    assert secret_relationship.properties["secret_reference"] == "vault://production/service-password"
    assert secret_relationship.properties["secret_lifecycle"] == "active"
    assert secret_relationship.properties["status"] == "active"
    assert ordinary_relationship.properties["status"] == "pruned"


@pytest.mark.asyncio
async def test_keyword_retrieval_restores_prune_ghost_with_receipt(tmp_path) -> None:
    """The default: a matching keyword retrieval revives the ghost, as it always has.

    This is the regression guard for the opt-in pure read added alongside it —
    with no configuration, a caller written before that work sees exactly this.
    The pure-read half lives in
    ``test_keyword_retrieval_reports_prune_ghost_without_restoring_it``.
    """
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user")
    client, first = await _client_with_one_archived_memory(tmp_path, scope=scope)

    ghosts = client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)
    assert len(ghosts) == 1

    results = await client.search(query="vendor invoices", scope=scope)

    # Historical behaviour, unchanged: the archived row is revived by the read
    # and returned in the results.
    assert [item.relationship_uuid for item in results] == [first.relationship_uuid]
    restored = client.graph.get_relationship(first.relationship_uuid)
    assert restored.properties["status"] == "active"
    receipts = await client.memory_receipts(scope=scope)
    assert any(receipt.decision_type == ReceiptDecisionType.PRUNING_GHOST_RESTORED for receipt in receipts)

    # Same receipt content as before the flag existed.
    restore_receipt = next(
        receipt for receipt in receipts if receipt.decision_type == ReceiptDecisionType.PRUNING_GHOST_RESTORED
    )
    assert restore_receipt.decision_reason == "retrieval_matched_prune_ghost"
    components = json.loads(restore_receipt.retention_components)
    assert components["retrieval_mode"] == "keyword"
    assert components["ghost_prune_receipt_uuid"] == ghosts[0].prune_receipt_uuid
    assert components["ghost_regret_rate"] == pytest.approx(1.0)

    # Same dream decision, attributed to retrieval.
    decisions = await client.dream_decisions(limit=1000)
    regret = [decision for decision in decisions if decision.decision_type == "pruning_ghost_regret"]
    assert len(regret) == 1
    assert regret[0].agent_id == "runtime-retrieval"
    assert regret[0].details["restore_receipt_uuid"] == restore_receipt.receipt_uuid

    # Additive: the read reports what it revived.
    assert results.archived.disposition == ArchivedMatchDisposition.REVIVED
    assert [match.relationship_uuid for match in results.archived.matches] == [first.relationship_uuid]
    assert results.archived.matches[0].revived is True
    assert results.archived.matches[0].retrieval_mode == "keyword"

    # The ghost is spent, so a second read revives nothing and reports nothing.
    again = await client.search(query="vendor invoices", scope=scope)
    assert [item.relationship_uuid for item in again] == [first.relationship_uuid]
    assert again.archived.count == 0


@pytest.mark.asyncio
async def test_keyword_retrieval_reports_prune_ghost_without_restoring_it(tmp_path) -> None:
    """With ``pure_read_retrieval`` on, retrieval reports the match and writes nothing.

    The opt-in mode for deployments that serve retrieval from more than one
    replica, where revive-on-read's row locks contend.  Revival then only
    happens through ``test_explicit_curation_restores_prune_ghost_with_receipt``.
    """
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user")
    client, first = await _client_with_one_archived_memory(tmp_path, scope=scope, pure_read_retrieval=True)

    ghosts = client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)
    assert len(ghosts) == 1

    state_before = client.graph.graph_state_hash(scope.key)
    receipts_before = await client.memory_receipts(scope=scope)
    decisions_before = await client.dream_decisions(limit=1000)

    results = await client.search(query="vendor invoices", scope=scope)

    # The archived row is reported, not returned and not revived.
    assert [item.relationship_uuid for item in results] == []
    assert results.archived.count == 1
    match = results.archived.matches[0]
    assert match.relationship_uuid == first.relationship_uuid
    assert match.retrieval_mode == "keyword"
    assert match.prune_receipt_uuid == ghosts[0].prune_receipt_uuid
    assert "vendor invoices" in match.fact
    assert match.revived is False
    assert results.archived.disposition == ArchivedMatchDisposition.AVAILABLE_TO_RESTORE
    assert results.archived.restore_action == "restore_archived_memory"

    # The store is byte-identical across a search that matched an archived row.
    assert client.graph.graph_state_hash(scope.key) == state_before
    still_archived = client.graph.get_relationship(first.relationship_uuid)
    assert still_archived.properties["status"] == "pruned"
    assert client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True) == ghosts
    assert await client.memory_receipts(scope=scope) == receipts_before
    assert await client.dream_decisions(limit=1000) == decisions_before


@pytest.mark.asyncio
async def test_semantic_retrieval_restores_prune_ghost_with_receipt(tmp_path) -> None:
    """The default on the semantic path too: a matching read revives the ghost."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user-semantic")
    client, first = await _client_with_one_archived_memory(tmp_path, scope=scope)
    assert len(client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)) == 1

    results = await client.semantic_search(query="vendor invoices", scope=scope)

    assert first.relationship_uuid in [item.relationship_uuid for item in results]
    assert client.graph.get_relationship(first.relationship_uuid).properties["status"] == "active"
    assert results.archived.disposition == ArchivedMatchDisposition.REVIVED
    assert [match.relationship_uuid for match in results.archived.matches] == [first.relationship_uuid]
    assert results.archived.matches[0].retrieval_mode == "semantic"
    assert results.archived.matches[0].revived is True
    receipts = await client.memory_receipts(scope=scope)
    restore_receipt = next(
        receipt for receipt in receipts if receipt.decision_type == ReceiptDecisionType.PRUNING_GHOST_RESTORED
    )
    assert json.loads(restore_receipt.retention_components)["retrieval_mode"] == "semantic"


@pytest.mark.asyncio
async def test_semantic_retrieval_reports_prune_ghost_without_restoring_it(tmp_path) -> None:
    """The semantic path is a pure read under the flag, for the same reason."""
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user-semantic")
    client, first = await _client_with_one_archived_memory(tmp_path, scope=scope, pure_read_retrieval=True)
    assert len(client.graph.prune_ghosts(scope_key=scope.key, restorable_only=True)) == 1

    state_before = client.graph.graph_state_hash(scope.key)
    results = await client.semantic_search(query="vendor invoices", scope=scope)

    assert first.relationship_uuid not in [item.relationship_uuid for item in results]
    assert results.archived.count == 1
    assert results.archived.matches[0].relationship_uuid == first.relationship_uuid
    assert results.archived.matches[0].retrieval_mode == "semantic"
    assert results.archived.matches[0].revived is False
    assert results.archived.disposition == ArchivedMatchDisposition.AVAILABLE_TO_RESTORE
    assert client.graph.graph_state_hash(scope.key) == state_before
    assert client.graph.get_relationship(first.relationship_uuid).properties["status"] == "pruned"


@pytest.mark.parametrize("pure_read_retrieval", [False, True], ids=["default", "pure_read"])
@pytest.mark.asyncio
async def test_explicit_curation_restores_prune_ghost_with_receipt(tmp_path, pure_read_retrieval: bool) -> None:
    """The explicit curation action behaves identically in both retrieval modes.

    It is the only way back when retrieval is a pure read, and it stays
    available — with the same receipt and dream decision — when retrieval also
    revives on read.
    """
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user-curation")
    client, first = await _client_with_one_archived_memory(
        tmp_path, scope=scope, pure_read_retrieval=pure_read_retrieval
    )

    # archived_matches is a pure read in both modes.
    state_before = client.graph.graph_state_hash(scope.key)
    report = client.archived_matches(query="vendor invoices", scope=scope)
    assert report.disposition == ArchivedMatchDisposition.AVAILABLE_TO_RESTORE
    assert client.graph.graph_state_hash(scope.key) == state_before
    assert [match.relationship_uuid for match in report.matches] == [first.relationship_uuid]

    restored = await client.restore_archived_memory(
        relationship_uuid=first.relationship_uuid,
        scope=scope,
        reason="operator confirmed the runbook is still current",
    )
    assert restored.relationship_uuid == first.relationship_uuid
    assert restored.status == RelationshipStatus.ACTIVE
    assert restored.previous_status == RelationshipStatus.PRUNED
    assert restored.ghost_regret_rate == pytest.approx(1.0)

    relationship = client.graph.get_relationship(first.relationship_uuid)
    assert relationship.properties["status"] == "active"

    receipts = await client.memory_receipts(scope=scope)
    restore_receipts = [
        receipt for receipt in receipts if receipt.decision_type == ReceiptDecisionType.PRUNING_GHOST_RESTORED
    ]
    assert len(restore_receipts) == 1
    assert restore_receipts[0].receipt_uuid == restored.restore_receipt_uuid
    components = json.loads(restore_receipts[0].retention_components)
    assert components["ghost_regret_rate"] == pytest.approx(1.0)
    assert components["restore_mode"] == "explicit_curation"
    assert components["ghost_prune_receipt_uuid"] == restored.prune_receipt_uuid

    decisions = await client.dream_decisions(limit=1000)
    regret = [decision for decision in decisions if decision.decision_type == "pruning_ghost_regret"]
    assert len(regret) == 1
    assert regret[0].details["restore_receipt_uuid"] == restored.restore_receipt_uuid

    # The restored row is retrievable again, and there is no ghost left to act on
    # in either mode.
    results = await client.search(query="vendor invoices", scope=scope)
    assert [item.relationship_uuid for item in results] == [first.relationship_uuid]
    assert results.archived.count == 0


@pytest.mark.parametrize("pure_read_retrieval", [False, True], ids=["default", "pure_read"])
@pytest.mark.asyncio
async def test_restoring_a_shredded_ghost_is_refused_and_demotes_it(tmp_path, pure_read_retrieval: bool) -> None:
    """The crypto-shred guard and the not-restorable refusal hold in both modes.

    The read path swallows both refusals (``continue``); an explicit curation
    request surfaces them to the caller.
    """
    scope = MemoryScope(kind=ScopeKind.CUSTOMER, scope_id="ghost-shred")
    client, first = await _client_with_one_archived_memory(
        tmp_path,
        scope=scope,
        crypto_shred=True,
        pure_read_retrieval=pure_read_retrieval,
    )
    assert client.graph.prune_ghost(first.relationship_uuid).restorable is True

    await client.crypto_shred(scope=scope)

    with pytest.raises(ContentKeyUnavailableError):
        await client.restore_archived_memory(relationship_uuid=first.relationship_uuid, scope=scope)
    assert client.graph.prune_ghost(first.relationship_uuid).restorable is False
    assert client.graph.get_relationship(first.relationship_uuid).properties["status"] == "pruned"

    with pytest.raises(ValueError, match="not restorable"):
        await client.restore_archived_memory(relationship_uuid=first.relationship_uuid, scope=scope)


@pytest.mark.parametrize("pure_read_retrieval", [False, True], ids=["default", "pure_read"])
@pytest.mark.asyncio
async def test_restore_archived_memory_validates_its_target(tmp_path, pure_read_retrieval: bool) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user-validation")
    other = MemoryScope(kind=ScopeKind.USER, scope_id="ghost-user-other")
    client = Memotron(
        graph_path=tmp_path / "graph.sqlite",
        config=_config_with_pruning(
            PruningPolicy(
                min_confidence=0.0,
                growth=GrowthPolicy(soft_cap=1),
                retention=RetentionPolicy(protected_memory_types=()),
            ),
            pure_read_retrieval=pure_read_retrieval,
        ),
    )
    with pytest.raises(ValueError, match="relationship_uuid cannot be blank"):
        await client.restore_archived_memory(relationship_uuid="   ", scope=scope)
    with pytest.raises(ValueError):
        await client.restore_archived_memory(relationship_uuid="does-not-exist", scope=scope)

    first = await client.add_memory(
        subject="Runbook",
        predicate="prefers",
        object="reconcile vendor invoices monthly",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 1, tzinfo=UTC),
    )
    await client.add_memory(
        subject="Runbook",
        predicate="prefers",
        object="summarize status each morning",
        relationship_type="PREFERS",
        scope=scope,
        valid_from=datetime(2026, 7, 14, tzinfo=UTC),
    )
    await client.run_dream_job(job_name="prune", now=datetime(2026, 7, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match="does not match requested scope"):
        await client.restore_archived_memory(relationship_uuid=first.relationship_uuid, scope=other)
    # Retrieval reports nothing for a scope that owns no archived rows.
    assert client.archived_matches(query="vendor invoices", scope=other).count == 0
    with pytest.raises(ValueError, match="exactly one of scope or scopes"):
        client.archived_matches(query="vendor invoices")
