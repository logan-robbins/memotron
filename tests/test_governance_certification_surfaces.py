"""Patent addendum surfaces: BOMs, policy gates, bundles, repair receipts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memotron import (
    ArtifactClass,
    ArtifactDirective,
    DirectiveStance,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    Memotron,
    EpisodeType,
    MemoryBank,
    MemoryScope,
    MemoryType,
    Motive,
    NodeInstruction,
    OutcomeVerdict,
    PersistentArtifact,
    ReceiptDecisionType,
    RelationshipInstruction,
    ScopeKind,
    UseEventKind,
)
from memotron.models import DreamJobKind, Episode, RelationshipCardinality, RelationshipStatus
from memotron.receipts import payload_digest


def _scope(scope_id: str) -> MemoryScope:
    return MemoryScope(kind=ScopeKind.USER, scope_id=scope_id)


def _instructions() -> DreamInstructionSet:
    return DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Find durable entities worth remembering.",
                properties=("kind",),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="preferences",
            ),
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="requirements",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
        ),
    )


def _config(memory_bank: MemoryBank | None = None) -> DreamConfig:
    return DreamConfig(
        instruction_sets=(_instructions(),),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
            ),
        ),
        memory_bank=memory_bank,
    )


def _line(subject: str, predicate: str, obj: str, rel: str = "PREFERS") -> str:
    return f"Memory: subject={subject}; predicate={predicate}; object={obj}; relationship_type={rel}; confidence=0.9"


async def _run_formation(client: Memotron, scope: MemoryScope, body: str) -> str:
    await client.add_episode(
        name="episode",
        episode_body=body,
        source=EpisodeType.TEXT,
        scope=scope,
        source_description="fixture",
    )
    result = await client.run_dream_job(job_name="formation-default")
    run_uuid = result.job_runs[0].run_uuid
    assert run_uuid is not None
    return run_uuid


def _corpus(scope: MemoryScope) -> list[Episode]:
    ref = datetime(2026, 5, 5, tzinfo=UTC)
    return [
        Episode(
            uuid=f"{scope.scope_id}-req",
            name="requirement",
            body=_line("Dana", "requires", "two-factor auth", rel="REQUIRES"),
            source=EpisodeType.TEXT,
            source_description="fixture",
            scope=scope,
            reference_time=ref,
            metadata={},
            instruction_set="default",
        ),
        Episode(
            uuid=f"{scope.scope_id}-pref",
            name="preference",
            body=_line("Alice", "prefers", "green tea"),
            source=EpisodeType.TEXT,
            source_description="fixture",
            scope=scope,
            reference_time=ref,
            metadata={},
            instruction_set="default",
        ),
    ]


@pytest.mark.asyncio
async def test_bom_policy_comparison_regression_gate_and_bundle(tmp_path) -> None:
    strict = Motive(
        name="requirements-only",
        goal="Persist only explicit requirements.",
        allowed_memory_types=(MemoryType.REQUIREMENT,),
    )
    bank = MemoryBank(motives=[strict])
    client = Memotron(config=_config(memory_bank=bank), graph_path=tmp_path / "bundle.sqlite")
    baseline_scope = _scope("bundle-baseline")
    baseline_run = await _run_formation(
        client,
        baseline_scope,
        "\n".join(
            [
                _line("Alice", "prefers", "green tea"),
                _line("Dana", "requires", "two-factor auth", rel="REQUIRES"),
            ]
        ),
    )

    bom = await client.memory_bill_of_materials(run_uuid=baseline_run)
    assert bom.chain_valid is True
    assert bom.candidate_count == 2
    assert bom.materialized_count == 2
    assert bom.negative_space_count == 0
    assert len(bom.candidate_digests) == 2
    assert len(bom.merkle_root) == 64

    comparison = await client.compare_policies(run_uuid=baseline_run, motives=[strict])
    item = comparison.comparisons[0]
    assert item.motive_name == "requirements-only"
    assert item.signal_to_noise_delta == 1
    assert item.report.original_materialized_count == 2
    assert item.report.alternate_materialized_count == 1
    assert len(item.report.original_only) == 1
    assert len(item.report.accepted_by_both) == 1

    gate = await client.policy_regression_gate(
        baseline_run_uuid=baseline_run,
        candidate_motive=strict,
        corpus=_corpus(_scope("bundle-certification")),
        scope=_scope("bundle-certification"),
    )
    assert gate.passed is True
    assert gate.failures == ()
    assert gate.blocked_disallowed_count == 1
    assert gate.lost_allowed_count == 0
    assert gate.new_acceptance_count == 0

    bundle = await client.certification_bundle(
        scope=baseline_scope,
        run_uuid=baseline_run,
        policy_certificate=gate.certification,
        policy_comparison=comparison,
        policy_regression_gate=gate,
    )
    assert bundle.memory_bill_of_materials.run_uuid == baseline_run
    assert bundle.replay_proof.run_uuid == baseline_run
    assert bundle.no_silent_mutation.passed is True
    assert bundle.policy_regression_gate is not None
    assert bundle.policy_regression_gate.passed is True
    assert len(bundle.bundle_digest) == 64


@pytest.mark.asyncio
async def test_no_silent_mutation_report_detects_unreceipted_graph_change(tmp_path) -> None:
    client = Memotron(config=_config(), graph_path=tmp_path / "silent.sqlite")
    scope = _scope("silent")
    await _run_formation(client, scope, _line("Alice", "prefers", "green tea"))

    clean = await client.verify_no_silent_mutations(scope=scope)
    assert clean.passed is True
    assert clean.mutating_receipt_count >= 1

    relationship = next(
        rel for rel in client.graph.context_visible_relationships(scope=scope) if rel.type != "MENTIONS"
    )
    client.graph.mark_relationship(
        relationship.uuid,
        status=RelationshipStatus.PRUNED,
        properties={"pruned_reason": "direct-unreceipted-test-mutation"},
    )

    report = await client.verify_no_silent_mutations(scope=scope)
    assert report.passed is False
    assert any("live graph state hash" in error for error in report.errors)


@pytest.mark.asyncio
async def test_coherence_governor_repair_is_bound_to_incident_by_receipt(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "repair.sqlite")
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="repair-agent")
    relationship_uuid = ""
    for _ in range(4):
        result = await client.add_memory(
            subject="Agent",
            predicate="should",
            object="evaluate the cost efficiency of other agents thoroughly",
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.7,
            source_text="Be more thorough about cost efficiency.",
        )
        relationship_uuid = result.relationship_uuid

    artifact = PersistentArtifact(
        artifact_id="cost-efficiency-evaluator",
        artifact_class=ArtifactClass.SKILL,
        scope=scope,
        location=".claude/skills/cost-efficiency-evaluator/SKILL.md",
        author="self:agent-session",
        self_authored=True,
        updated_at=datetime.now(UTC) - timedelta(days=30),
        directives=[
            ArtifactDirective(
                subject="evaluate cost efficiency of other agents",
                instruction="A cost-efficiency check passes when total tokens are below budget.",
                stance=DirectiveStance.ASSERT,
            )
        ],
    )
    prior = client.register_live_artifact(artifact)
    report = await client.run_coherence_scan(scope=scope)
    assert report.incident_count == 1
    incident = report.incidents[0]
    assert incident.escalating_directive is not None
    assert incident.escalating_directive.relationship_uuid == relationship_uuid

    repaired = client.register_live_artifact(
        prior.model_copy(
            update={
                "updated_at": datetime.now(UTC),
                "directives": [
                    ArtifactDirective(
                        subject="evaluate cost efficiency of other agents",
                        instruction="Require comparative cost analysis before a pass.",
                        stance=DirectiveStance.REQUIRE,
                    )
                ],
            }
        )
    )
    receipt = await client.record_coherence_repair(
        scope=scope,
        incident_id=incident.incident_id,
        repaired_artifact_id="cost-efficiency-evaluator",
        repair_summary="Updated the skill to require comparative cost analysis before pass.",
        artifact_version_digest=payload_digest(repaired.model_dump(mode="json")),
    )

    assert receipt.decision_type == ReceiptDecisionType.COHERENCE_GOVERNOR_REPAIR_RECORDED
    assert receipt.decision_result == "recorded"
    assert receipt.relationship_uuid == relationship_uuid
    assert client.graph.receipts.verify_chain(receipt.run_uuid).valid is True
    decisions = await client.dream_decisions(limit=20, job_name="coherence-repair")
    assert decisions[0].details["incident_id"] == incident.incident_id
    assert decisions[0].details["receipt_hash"] == receipt.receipt_hash
    monitor = client.coherence_repair_monitor(scope=scope, incident_id=incident.incident_id)
    assert monitor is not None
    assert monitor.status == "monitoring"


@pytest.mark.asyncio
async def test_negative_post_repair_outcome_reopens_incident_and_rolls_back_registry_projection(tmp_path) -> None:
    client = Memotron(graph_path=tmp_path / "post-repair.sqlite")
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="post-repair-agent")
    relationship_uuid = ""
    for _ in range(4):
        result = await client.add_memory(
            subject="Agent",
            predicate="should",
            object="evaluate the cost efficiency of other agents thoroughly",
            relationship_type="SHOULD",
            scope=scope,
            confidence=0.7,
            source_text="Be more thorough about cost efficiency.",
        )
        relationship_uuid = result.relationship_uuid

    prior = client.register_live_artifact(
        PersistentArtifact(
            artifact_id="cost-efficiency-evaluator",
            artifact_class=ArtifactClass.SKILL,
            scope=scope,
            location="skills/cost-efficiency/SKILL.md",
            author="operator@example.test",
            self_authored=True,
            updated_at=datetime.now(UTC) - timedelta(days=30),
            directives=[
                ArtifactDirective(
                    subject="evaluate cost efficiency of other agents",
                    instruction="Pass if total tokens are below budget.",
                    stance=DirectiveStance.ASSERT,
                )
            ],
        )
    )
    report = await client.run_coherence_scan(scope=scope)
    incident = report.incidents[0]
    assert client.graph.get_relationship(relationship_uuid).properties["coherence_hold"] is True

    repaired = client.register_live_artifact(
        prior.model_copy(
            update={
                "updated_at": datetime.now(UTC),
                "directives": [
                    ArtifactDirective(
                        subject="evaluate cost efficiency of other agents",
                        instruction="Require comparative cost analysis before a pass.",
                        stance=DirectiveStance.REQUIRE,
                    )
                ],
            }
        )
    )
    repaired_digest = payload_digest(repaired.model_dump(mode="json"))
    repair = await client.record_coherence_repair(
        scope=scope,
        incident_id=incident.incident_id,
        repaired_artifact_id=repaired.artifact_id,
        repair_summary="Added comparative cost analysis.",
        artifact_version_digest=repaired_digest,
    )
    assert repair.decision_type == ReceiptDecisionType.COHERENCE_GOVERNOR_REPAIR_RECORDED

    use = await client.record_memory_use(
        relationship_uuid=relationship_uuid,
        scope=scope,
        kind=UseEventKind.CITED_OR_USED,
        task_run_id="repaired-governor-run",
        idempotency_key="repaired-governor-use",
    )
    await client.record_memory_outcome(
        use_id=use.use_id,
        scope=scope,
        verdict=OutcomeVerdict.NEGATIVE,
        task_run_id="repaired-governor-run",
        idempotency_key="repaired-governor-negative",
        judge_identity="operator",
        judge_version="v1",
        metadata={
            "repaired_governor": True,
            "repaired_governor_artifact_id": repaired.artifact_id,
        },
    )

    monitor = client.coherence_repair_monitor(scope=scope, incident_id=incident.incident_id)
    assert monitor is not None
    assert monitor.status == "reopened_rolled_back"
    current, current_digest = client.graph.live_artifact_version(scope=scope, artifact_id=repaired.artifact_id)
    assert current.directives == prior.directives
    assert current_digest == monitor.prior_artifact_version_digest
    assert client.graph.get_relationship(relationship_uuid).properties["coherence_hold"] is True
    receipt_types = {receipt.decision_type for receipt in client.graph.receipts.receipts_for_scope(scope.key)}
    assert ReceiptDecisionType.COHERENCE_INCIDENT_REOPENED in receipt_types
    assert ReceiptDecisionType.COHERENCE_GOVERNOR_ROLLED_BACK in receipt_types
