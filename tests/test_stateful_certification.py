from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from memotron import (
    AgentMemoryPolicy,
    CertificationCorpus,
    DreamAgentConfig,
    Memotron,
    LoCoMoCorpusAdapter,
    LongMemEvalCorpusAdapter,
    MemoryAgentBenchCorpusAdapter,
    MemoryControlPlane,
    MemoryScope,
    MemoryType,
    ScopeKind,
    ScopeMemoryPolicy,
    StatefulReplayOptions,
    TenantMemoryPolicy,
    builtin_motive_fixture_corpora,
    certification_config,
)
from memotron.memory_bank import builtin_memory_bank


@pytest.mark.asyncio
async def test_stateful_policy_comparison_isolated_repeated_and_noise_gated(tmp_path) -> None:
    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    baseline = motives["capture-preferences"]
    candidate = motives["learn-compliance-requirements"]
    client = Memotron(
        config=certification_config(motives=(baseline, candidate)),
        graph_path=tmp_path / "production.sqlite",
    )
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="stateful-certification")
    corpus = builtin_motive_fixture_corpora()[candidate.name]
    before = client.graph.graph_state_hash(scope.key)

    report = await client.stateful_policy_comparison(
        corpus=corpus,
        scope=scope,
        baseline_motive=baseline,
        candidate_motive=candidate,
        options=StatefulReplayOptions(
            model_identifier="rule-based@v1",
            temperature=0.0,
            repetitions=3,
            minimum_stability_score=1.0,
        ),
    )

    assert report.passed is True, [item.model_dump() for item in report.protected_invariants]
    assert report.policy_delta > report.replay_flip_rate
    assert report.stability_score == 1.0
    assert report.candidate_disposition_diffs
    assert report.end_state_graph_diff.change_count > 0
    assert all(invariant.passed for invariant in report.protected_invariants)
    assert len(report.baseline_samples) == 3
    assert len(report.candidate_samples) == 3
    assert client.graph.graph_state_hash(scope.key) == before
    assert client.graph.relationships() == []


@pytest.mark.asyncio
async def test_policy_alias_requires_certification_shadow_then_rolls_back_without_memory_mutation(
    tmp_path,
) -> None:
    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    # T0-14: the baseline was `capture-preferences`, and this test only passed because staging a
    # baseline read the CANDIDATE's invariants. `capture-preferences` violates its OWN
    # `retrieval_allocation` on this corpus -- a ROLLUP row is formed even though the motive
    # allows only `preference` -- so once the acceptance rule was corrected to read the baseline's
    # own samples, the baseline could no longer be certified and the alias could not be
    # bootstrapped.
    #
    # That rollup is a SEPARATE, real defect, not a reason to weaken the gate: the WS-3
    # motive->rollup gate at `dreaming/_consolidation.py:191` never fires during replay (observed:
    # zero "motive rollup gate" log lines while the extraction-level type filter logs normally),
    # so a rollup is created for a motive that explicitly disallows it. It is pinned by
    # `test_motive_rollup_gate_is_not_enforced_during_replay` below and filed separately.
    #
    # This test's subject is the shadow -> activate -> rollback flow, not baseline strictness, so
    # it uses a baseline that genuinely certifies. `record-decisions` forms no rollup on this
    # corpus (rowtypes == ['decision']) and its own invariants all pass.
    baseline = motives["record-decisions"]
    candidate = motives["learn-compliance-requirements"]
    client = Memotron(
        config=certification_config(motives=(baseline, candidate)),
        graph_path=tmp_path / "production.sqlite",
    )
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="policy-alias")
    corpus = builtin_motive_fixture_corpora()[candidate.name]
    options = StatefulReplayOptions(
        model_identifier="rule-based@v1",
        temperature=0.0,
        repetitions=2,
        minimum_stability_score=1.0,
    )
    comparison = await client.stateful_policy_comparison(
        corpus=corpus,
        scope=scope,
        baseline_motive=baseline,
        candidate_motive=candidate,
        options=options,
    )
    assert comparison.passed is True
    baseline_contract = await client.stage_policy_contract(
        scope=scope,
        alias="production",
        motive=baseline,
        certification=comparison,
        role="baseline",
        source_trace={"change": "baseline migration"},
    )
    candidate_contract = await client.stage_policy_contract(
        scope=scope,
        alias="production",
        motive=candidate,
        certification=comparison,
        role="candidate",
        source_trace={"change": "requirement-policy candidate"},
    )
    initial = client.initialize_policy_alias(
        scope=scope,
        alias="production",
        contract_digest=baseline_contract.contract_digest,
    )
    assert initial.contract_digest == baseline_contract.contract_digest
    production_before = client.graph.graph_state_hash(scope.key)

    blocked = await client.run_policy_shadow_stage(
        scope=scope,
        alias="production",
        candidate_contract_digest=candidate_contract.contract_digest,
        corpus=corpus,
        allowed_disposition_delta=0.0,
    )
    assert blocked.status == "blocked"
    with pytest.raises(ValueError, match="no completed non-divergent shadow stage"):
        client.activate_policy_alias(
            scope=scope,
            alias="production",
            candidate_contract_digest=candidate_contract.contract_digest,
        )

    noop_episode = corpus.episodes[0].model_copy(
        update={
            "uuid": "policy-shadow-noop",
            "name": "policy shadow no-op",
            "body": "Runtime heartbeat observed; no durable evidence was provided.",
            "scope": scope,
        }
    )
    quiet_window = CertificationCorpus(
        name="policy-shadow-noop-window",
        source="test-current-traffic",
        episodes=(noop_episode,),
    )
    passed = await client.run_policy_shadow_stage(
        scope=scope,
        alias="production",
        candidate_contract_digest=candidate_contract.contract_digest,
        corpus=quiet_window,
        allowed_disposition_delta=0.0,
    )
    assert passed.status == "passed"
    activated = client.activate_policy_alias(
        scope=scope,
        alias="production",
        candidate_contract_digest=candidate_contract.contract_digest,
    )
    assert activated.contract_digest == candidate_contract.contract_digest
    assert client.graph.graph_state_hash(scope.key) == production_before

    runtime_agent = DreamAgentConfig(
        agent_id="runtime",
        name="runtime agent",
        scope=scope,
    )
    client.control_plane = MemoryControlPlane(
        base_config=client.config,
        tenants=(
            TenantMemoryPolicy(
                tenant_id="certification-runtime",
                default_agent_id=runtime_agent.agent_id,
                default_scope=scope,
                default_motive=baseline.name,
                memory_bank=client.config.memory_bank,
                agents=(
                    AgentMemoryPolicy(
                        agent=runtime_agent,
                        default_scope=scope,
                        motive=baseline.name,
                    ),
                ),
                scopes=(ScopeMemoryPolicy(scope=scope, motive=baseline.name),),
            ),
        ),
    )
    active_policy = client.resolve_policy(
        tenant_id="certification-runtime",
        agent_id=runtime_agent.agent_id,
        scope=scope,
    )
    assert active_policy.motive_name == candidate.name
    assert active_policy.policy_alias == "production"
    assert active_policy.policy_contract_digest == candidate_contract.contract_digest
    assert active_policy.certification_verdict == "certified"

    reference_time = datetime(2026, 7, 15, tzinfo=UTC)
    await client.add_episode(
        name="live certified policy evidence",
        episode_body=json.dumps(
            {
                "memories": [
                    {
                        "subject": "release",
                        "predicate": "requires",
                        "object": "certified approvals",
                        "relationship_type": "REQUIRES",
                        "confidence": 0.95,
                    }
                ]
            }
        ),
        source="json",
        scope=scope,
        reference_time=reference_time,
        instruction_set="certification",
    )
    runtime_result = await client.run_due_dreams(
        now=reference_time,
        tenant_id="certification-runtime",
        agent_id=runtime_agent.agent_id,
        scope=scope,
    )
    formation_run = next(run for run in runtime_result.job_runs if run.job_kind.value == "formation")
    receipts = await client.memory_receipts(run_uuid=formation_run.run_uuid)
    bound_receipt = next(receipt for receipt in receipts if receipt.formation_contract_attestation is not None)
    source_trace = json.loads(bound_receipt.formation_contract_source_trace or "{}")
    attestation = json.loads(bound_receipt.formation_contract_attestation or "{}")
    assert source_trace["policy_alias"] == "production"
    assert source_trace["policy_contract"] == candidate_contract.contract_digest
    assert source_trace["certification"] == "passed"
    assert attestation["certification_verdict"] == "certified"
    runtime_graph_state = client.graph.graph_state_hash(scope.key)

    rolled_back = client.rollback_policy_alias(scope=scope, alias="production")
    assert rolled_back.contract_digest == baseline_contract.contract_digest
    assert client.graph.graph_state_hash(scope.key) == runtime_graph_state
    rolled_back_policy = client.resolve_policy(
        tenant_id="certification-runtime",
        agent_id=runtime_agent.agent_id,
        scope=scope,
    )
    assert rolled_back_policy.motive_name == baseline.name
    assert rolled_back_policy.policy_contract_digest == baseline_contract.contract_digest
    status = client.policy_rollout_status(scope=scope, alias="production")
    assert status["alias"] == rolled_back
    assert [stage.status for stage in status["shadow_stages"]] == ["passed", "blocked"]
    receipt_types = {receipt.decision_type.value for receipt in client.graph.receipts.receipts_for_scope(scope.key)}
    assert {
        "policy_contract_staged",
        "policy_shadow_stage_completed",
        "policy_alias_activated",
        "policy_alias_rolled_back",
    } <= receipt_types


def test_public_corpus_adapters_require_local_json_and_preserve_raw_inputs(tmp_path) -> None:
    scope = MemoryScope(kind=ScopeKind.USER, scope_id="external-corpora")
    longmem_path = tmp_path / "longmemeval.json"
    longmem_payload = [
        {
            "question_id": "q-1",
            "question_date": "2026-01-01T00:00:00Z",
            "haystack_sessions": [[{"speaker": "Ada", "text": "The policy changed yesterday."}]],
        }
    ]
    longmem_path.write_text(json.dumps(longmem_payload), encoding="utf-8")
    original_longmem = longmem_path.read_bytes()
    longmem = LongMemEvalCorpusAdapter.load(longmem_path, scope=scope)
    assert longmem.source == "LongMemEval"
    assert len(longmem.episodes) == 1
    assert longmem_path.read_bytes() == original_longmem

    locomo_path = tmp_path / "locomo.json"
    locomo_path.write_text(
        json.dumps(
            [
                {
                    "conversation_id": "c-1",
                    "conversation": {
                        "session_1": [{"speaker": "A", "text": "Hello"}],
                        "session_1_date_time": "1:56 pm on 8 May, 2023",
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    assert LoCoMoCorpusAdapter.load(locomo_path, scope=scope).episodes[0].name == "LoCoMo c-1 session 1"

    bench_path = tmp_path / "memoryagentbench.json"
    bench_path.write_text(
        json.dumps({"data": [{"sample_id": "s-1", "messages": [{"role": "user", "content": "Remember this."}]}]}),
        encoding="utf-8",
    )
    assert MemoryAgentBenchCorpusAdapter.load(bench_path, scope=scope).episodes[0].name == "MemoryAgentBench s-1"


@pytest.mark.asyncio
@pytest.mark.xfail(
    reason=(
        "Known defect #143, found while fixing T0-14. The WS-3 motive->rollup gate at "
        "dreaming/_consolidation.py:191 does not fire during certification replay, so a ROLLUP "
        "row is formed for a motive whose allowed_memory_types excludes it. Remove the xfail "
        "when the gate is enforced on the replay path."
    ),
    strict=True,
)
async def test_motive_rollup_gate_is_not_enforced_during_replay(tmp_path) -> None:
    """A motive that disallows ROLLUP must not produce a ROLLUP row during replay.

    ``_consolidation.py:191`` states the intent plainly -- when a Motive constrains
    ``allowed_memory_types`` and ROLLUP is not among them, "no rollups may be formed for this
    job, and the gate is recorded as an auditable decision (no silent skips)". During
    certification replay neither half happens: no gate log line is emitted and the rollup is
    formed anyway.

    Observed on 4 of the 15 builtin motives (``capture-preferences``, ``capture-user-preferences``,
    ``synthesize-user-feedback``, ``capture-meeting-notes``) -- exactly the four whose baseline
    replay contains a ROLLUP row, and exactly the four whose ``retrieval_allocation`` invariant
    fails. The correlation is total, and ``retrieval_allocation`` is reporting it correctly.

    This stayed invisible because staging a baseline read the CANDIDATE's invariants (T0-14).
    """
    motives = {motive.name: motive for motive in builtin_memory_bank().motives}
    baseline = motives["capture-preferences"]
    candidate = motives["learn-compliance-requirements"]
    assert MemoryType.ROLLUP not in baseline.allowed_memory_types, "fixture requires a motive that disallows ROLLUP"

    client = Memotron(
        config=certification_config(motives=(baseline, candidate)),
        graph_path=tmp_path / "rollup-gate.sqlite",
    )
    scope = MemoryScope(kind=ScopeKind.AGENT, scope_id="rollup-gate")
    comparison = await client.stateful_policy_comparison(
        corpus=builtin_motive_fixture_corpora()[candidate.name],
        scope=scope,
        baseline_motive=baseline,
        candidate_motive=candidate,
        options=StatefulReplayOptions(
            model_identifier="rule-based@v1",
            temperature=0.0,
            repetitions=2,
            minimum_stability_score=1.0,
        ),
    )

    rollup_rows = [
        row
        for sample in comparison.baseline_samples
        for row in sample.graph_rows
        if row["memory_type"] == MemoryType.ROLLUP.value
    ]
    assert rollup_rows == [], (
        f"motive {baseline.name!r} allows only "
        f"{[t.value for t in baseline.allowed_memory_types]} yet replay formed "
        f"{len(rollup_rows)} ROLLUP row(s); the _consolidation.py:191 gate did not fire"
    )
