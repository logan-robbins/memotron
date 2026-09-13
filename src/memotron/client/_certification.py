"""Certifying a policy before it governs anything, and rolling it back if it should not.

Sixteen members, the widest orchestrator in the client -- it calls into ingestion,
runtime, outcomes and coherence. That fan-out is real and the protocol declares it.

The alias lifecycle is the point: stage a contract, run it in shadow, activate,
roll back. `run_policy_shadow_stage` exists so a policy change is observed against
real traffic before it decides anything, which is the only way to tell a better
policy from a differently-wrong one.

Nineteen of the client's function-local imports live here. They defer
memotron.certification and memotron.replay, and the module docstring calls that
a cycle break. It is only still true for `replay`, which reaches client via
memotron -> agent_memory -> client; the other five deferred modules no longer do.
They stay local regardless -- hoisting changes bytecode and belongs in its own commit."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memotron.client._protocol import ComposedMemotron
from memotron.config import (
    DreamConfig,
    Motive,
)
from memotron.erasure import (
    ErasureCertificate,
)
from memotron.models import (
    DreamJobKind,
    Episode,
    MemoryScope,
)
from memotron.receipts import (
    ReceiptDecisionType,
    payload_digest,
)
from memotron.storage import StorageBackend

if TYPE_CHECKING:
    from collections.abc import Sequence

    # Annotation-only, and deferred because the runtime imports they mirror are
    # function-local. The package docstring used to say every module here imports
    # `client` back; measured 2026-08-28 that is true only for `replay` (via
    # memotron -> agent_memory -> client). The rest are deferred by convention
    # now, not necessity, and hoisting them is its own commit.
    from memotron.certification import (
        BenchmarkAnswerRequest,
        CertificationCorpus,
        PolicyAliasState,
        PolicyContractVersion,
        PolicyShadowStage,
        PublicBenchmarkQuestion,
        PublicBenchmarkReport,
        PublicBenchmarkSuite,
        StatefulPolicyComparison,
        StatefulReplayOptions,
        StatefulReplaySample,
    )
    from memotron.client import Memotron
    from memotron.replay import (
        GovernanceCertificationBundle,
        MemoryBillOfMaterials,
        MotiveCertificate,
        NoSilentMutationReport,
        PolicyComparisonReport,
        PolicyRegressionGateReport,
    )

    _Base = ComposedMemotron
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class CertificationMixin(_Base):
    """Composed into :class:`Memotron`."""

    # Provided by the composing backend.
    config: Any
    graph: StorageBackend

    async def stateful_policy_comparison(
        self,
        *,
        corpus: CertificationCorpus,
        scope: MemoryScope,
        baseline_motive: Motive,
        candidate_motive: Motive,
        options: StatefulReplayOptions,
    ) -> StatefulPolicyComparison:
        """Compare two Motives through isolated chronological full-lifecycle replay.

        The production graph is never passed to the evaluator.  Each baseline
        and candidate repetition receives a new SQLite store but the same
        extraction, dream-decision, and embedding transports.  Model identity
        and temperature are required in ``options`` and are recorded in the
        resulting stability report.
        """
        self._require_authorized_scope(scope)
        from memotron.certification import run_stateful_policy_comparison

        transport = self._extractor._transport
        transport_model = getattr(transport, "model", None)
        if isinstance(transport_model, str) and transport_model and transport_model != options.model_identifier:
            raise ValueError(
                "stateful replay model_identifier does not match the configured extraction transport model"
            )
        transport_temperature = getattr(transport, "temperature", None)
        if transport_temperature is not None and float(transport_temperature) != options.temperature:
            raise ValueError(
                "stateful replay temperature does not match the configured extraction transport temperature"
            )

        def client_factory(config: DreamConfig, graph_path: Path) -> Memotron:
            # Function-local, like the nineteen other deferred imports in this file:
            # certification builds a FRESH composed client per replay, and the composed
            # class lives in the package __init__ that imports this module. At module
            # scope that is a circular ImportError; here it resolves at call time.
            from memotron.client import Memotron

            return Memotron(
                config=config,
                graph_path=graph_path,
                extraction_transport=transport,
                dream_agent_transport=self._dream_agent_transport,
                embedding_transport=self._embedding_transport,
            )

        return await run_stateful_policy_comparison(
            client_factory=client_factory,
            base_config=self.config,
            corpus=corpus,
            scope=scope,
            baseline_motive=baseline_motive,
            candidate_motive=candidate_motive,
            options=options,
        )

    async def public_benchmark(
        self,
        *,
        suite: PublicBenchmarkSuite,
        scope: MemoryScope,
        motive: Motive,
        options: StatefulReplayOptions,
        answer_executor: Callable[[BenchmarkAnswerRequest], Any],
        answer_judge: Callable[[PublicBenchmarkQuestion, str], Any],
        answer_runtime_identifier: str,
        judge_identifier: str,
    ) -> PublicBenchmarkReport:
        """Score a Motive against a public suite in isolated replay stores.

        The supplied executor receives a formed profile and benchmark question,
        never a gold answer.  The judge is separate and receives the gold only
        after the executor responds.  Every scenario/repetition runs against a
        fresh SQLite graph, so this cannot mutate the caller's production graph.
        """
        self._require_authorized_scope(scope)
        from memotron.certification import run_public_benchmark

        transport = self._extractor._transport
        transport_model = getattr(transport, "model", None)
        if isinstance(transport_model, str) and transport_model and transport_model != options.model_identifier:
            raise ValueError(
                "public benchmark model_identifier does not match the configured extraction transport model"
            )
        transport_temperature = getattr(transport, "temperature", None)
        if transport_temperature is not None and float(transport_temperature) != options.temperature:
            raise ValueError(
                "public benchmark temperature does not match the configured extraction transport temperature"
            )

        def client_factory(config: DreamConfig, graph_path: Path) -> Memotron:
            # Function-local, like the nineteen other deferred imports in this file:
            # certification builds a FRESH composed client per replay, and the composed
            # class lives in the package __init__ that imports this module. At module
            # scope that is a circular ImportError; here it resolves at call time.
            from memotron.client import Memotron

            return Memotron(
                config=config,
                graph_path=graph_path,
                extraction_transport=transport,
                dream_agent_transport=self._dream_agent_transport,
                embedding_transport=self._embedding_transport,
            )

        return await run_public_benchmark(
            client_factory=client_factory,
            base_config=self.config,
            suite=suite,
            scope=scope,
            motive=motive,
            options=options,
            answer_executor=answer_executor,
            answer_judge=answer_judge,
            answer_runtime_identifier=answer_runtime_identifier,
            judge_identifier=judge_identifier,
        )

    async def stage_policy_contract(
        self,
        *,
        scope: MemoryScope,
        alias: str,
        motive: Motive,
        certification: StatefulPolicyComparison,
        role: str,
        source_trace: dict[str, str],
    ) -> PolicyContractVersion:
        """Persist an immutable, certified Motive version before rollout.

        ``role`` is deliberately explicit: a baseline is accepted for migration
        only when its lifecycle invariants and replay stability pass, while a
        candidate must satisfy the complete comparison gate (including material
        policy delta).  Both records retain the same comparison evidence.
        """
        self._require_authorized_scope(scope)
        from memotron.certification import StatefulPolicyComparison

        if role not in {"baseline", "candidate"}:
            raise ValueError("policy contract role must be 'baseline' or 'candidate'")
        if not alias.strip():
            raise ValueError("policy alias cannot be blank")
        if not isinstance(certification, StatefulPolicyComparison):
            raise TypeError("certification must be a StatefulPolicyComparison")
        if certification.scope != scope:
            raise ValueError("policy certification scope does not match the staged scope")
        expected_name = (
            certification.baseline_motive_name if role == "baseline" else certification.candidate_motive_name
        )
        if motive.name != expected_name:
            raise ValueError(
                f"staged {role} Motive {motive.name!r} does not match certification Motive {expected_name!r}"
            )
        normalized_trace = {key.strip(): value.strip() for key, value in source_trace.items()}
        if not normalized_trace or any(not key or not value for key, value in normalized_trace.items()):
            raise ValueError("policy source_trace requires non-blank keys and values")

        # T0-14: each side is accepted on ITS OWN evidence, across ALL repetitions.
        #
        # The docstring above is the contract: "a baseline is accepted for migration only when
        # its lifecycle invariants and replay stability pass". Three separate ways the old rule
        # broke that promise, all fixed here.
        #
        # (1) WRONG SIDE'S INVARIANTS. `certification.protected_invariants` is built from the
        #     CANDIDATE only -- `certification.py:1929` reads
        #     `invariants = tuple(candidate.protected_invariants)`. Staging a baseline therefore
        #     evaluated the candidate's replay, so a baseline whose own replay violated a
        #     protected invariant was staged with `certification_passed=True`. No model change was
        #     needed: `baseline_samples` already carries each sample's own `protected_invariants`;
        #     the consumer was reading the wrong side.
        #
        # (2) WRONG SIDE'S STABILITY. `certification.stability_score` is JOINT --
        #     `certification.py:1932` computes `1.0 - max(baseline_flip_rate, candidate_flip_rate)`
        #     -- so a perfectly stable baseline was refused whenever the CANDIDATE flipped. That is
        #     the same contamination as (1) wearing different clothes, and fixing only (1) leaves
        #     the docstring half-honoured. `baseline_flip_rate` is already on the model, so the
        #     baseline's own stability is `1.0 - certification.baseline_flip_rate`.
        #
        # (3) ONLY REPETITION 0 WAS GATED, ON BOTH ARMS. `certification.py:1925-1929` takes
        #     `baseline_samples[0]` / `candidate_samples[0]`, so `certification.passed` reflects
        #     rep 0 alone and repetitions 1..N-1 were never gated. A policy that breaks an
        #     invariant in one repetition and not another is not safe, and `stability_score`
        #     cannot catch it -- `_flip_rate` (`certification.py:1819-1829`) compares
        #     `candidate_dispositions`, not invariants. So both arms are checked across every
        #     sample. This makes the CANDIDATE arm strictly stricter than before; that is the
        #     intended direction, since the candidate is the side that goes live.
        #
        # Both arms fail CLOSED on no evidence: `all(())` is True, so an empty sample tuple would
        # certify vacuously -- the same fail-open shape arriving by a third road. The real
        # producer cannot emit it (`repetitions >= 2` at `certification.py:120`, and
        # `certification.py:1925` indexes `[0]`), but this method accepts any deserialized
        # comparison.
        #
        # `certification_passed` is load-bearing at three storage gates:
        # `sqlite/_policy.py:120` (shadow), `:212` (activate), `:265` (initial alias bootstrap --
        # the baseline path this fix governs), mirrored at `postgres/_policy.py:220,363,419`.
        def _every_sample_invariant_passed(samples: Sequence[StatefulReplaySample]) -> bool:
            return bool(samples) and all(
                invariant.passed for sample in samples for invariant in sample.protected_invariants
            )

        minimum_stability = certification.options.minimum_stability_score
        baseline_safe = (
            _every_sample_invariant_passed(certification.baseline_samples)
            and (1.0 - certification.baseline_flip_rate) >= minimum_stability
        )
        candidate_safe = certification.passed and _every_sample_invariant_passed(certification.candidate_samples)
        certification_passed = candidate_safe if role == "candidate" else baseline_safe
        payload = {
            "scope": scope.model_dump(mode="json"),
            "alias": alias.strip(),
            "motive": motive.model_dump(mode="json"),
            "replay_options": certification.options.model_dump(mode="json"),
            "source_trace": normalized_trace,
            "role": role,
            "compiled_contract_digest": (
                certification.baseline_contract_digest
                if role == "baseline"
                else certification.candidate_contract_digest
            ),
        }
        contract_digest = payload_digest(
            {
                "policy_contract": payload,
                "certification": certification.model_dump(mode="json"),
            }
        )
        staged_at = datetime.now(UTC)
        stored = self.graph.stage_policy_contract(
            contract_digest=contract_digest,
            payload=payload,
            certification=certification.model_dump(mode="json"),
            certification_passed=certification_passed,
            staged_at=staged_at,
        )
        contract = self._policy_contract_version_from_graph(stored)
        run = self._begin_operator_run(job_name="stage_policy_contract", scope_key=scope.key)
        state = self.graph.graph_state_hash(scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.POLICY_CONTRACT_STAGED,
            decision_reason=(
                f"alias={contract.alias};role={role};certification_passed={certification_passed};"
                f"contract={contract.contract_digest}"
            ),
            decision_result="recorded" if certification_passed else "gated",
            now=contract.staged_at,
            scope_key=scope.key,
            source_span_digest=contract.contract_digest,
            event_payload=contract.model_dump_json(),
            event_payload_digest=payload_digest(contract.model_dump(mode="json")),
            graph_state_hash_before=state,
            graph_state_hash_after=state,
        )
        self._checkpoint_operator_run(run)
        return contract

    def initialize_policy_alias(self, *, scope: MemoryScope, alias: str, contract_digest: str) -> PolicyAliasState:
        """Register the pre-existing certified baseline for a named live alias.

        This is the sole migration exception.  Once an alias exists, every
        change is forced through shadow evidence and atomic activation.
        """
        self._require_authorized_scope(scope)
        from memotron.certification import PolicyAliasState

        state = self.graph.initialize_policy_alias(
            scope_key=scope.key,
            alias=alias.strip(),
            contract_digest=contract_digest,
            now=datetime.now(UTC),
        )
        return PolicyAliasState(scope=scope, **state)

    async def run_policy_shadow_stage(
        self,
        *,
        scope: MemoryScope,
        alias: str,
        candidate_contract_digest: str,
        corpus: CertificationCorpus,
        allowed_disposition_delta: float = 0.0,
    ) -> PolicyShadowStage:
        """Compare candidate output against the active alias in an isolated window.

        The supplied corpus is replayed only into temporary stores.  Its fixed
        episode count is durably recorded before the comparison, and no
        production relationship, rollup, receipt, or policy projection is
        altered by either policy evaluation.
        """
        self._require_authorized_scope(scope)
        from memotron.certification import (
            CertificationCorpus,
            StatefulReplayOptions,
        )

        if not isinstance(corpus, CertificationCorpus):
            raise TypeError("shadow corpus must be a CertificationCorpus")
        active_alias = self.graph.policy_alias(scope_key=scope.key, alias=alias.strip())
        if active_alias is None:
            raise ValueError("shadow stage requires an initialized live policy alias")
        active_contract = self._policy_contract_version_from_graph(
            self.graph.policy_contract(contract_digest=active_alias["contract_digest"])
        )
        candidate_contract = self._policy_contract_version_from_graph(
            self.graph.policy_contract(contract_digest=candidate_contract_digest)
        )
        if active_contract.scope != scope or candidate_contract.scope != scope:
            raise ValueError("policy shadow contracts must belong to the requested scope")
        if active_contract.alias != alias.strip() or candidate_contract.alias != alias.strip():
            raise ValueError("policy shadow contracts must belong to the requested alias")
        if (
            active_contract.replay_options.model_identifier != candidate_contract.replay_options.model_identifier
            or active_contract.replay_options.temperature != candidate_contract.replay_options.temperature
        ):
            raise ValueError("shadow comparison requires identical pinned model identifier and temperature")
        started_at = datetime.now(UTC)
        stage = self.graph.begin_policy_shadow_stage(
            scope_key=scope.key,
            alias=alias.strip(),
            active_contract_digest=active_contract.contract_digest,
            candidate_contract_digest=candidate_contract.contract_digest,
            corpus_digest=corpus.corpus_digest,
            required_episode_count=len(corpus.episodes),
            allowed_disposition_delta=allowed_disposition_delta,
            started_at=started_at,
        )
        shadow_options = StatefulReplayOptions(
            model_identifier=candidate_contract.replay_options.model_identifier,
            temperature=candidate_contract.replay_options.temperature,
            repetitions=candidate_contract.replay_options.repetitions,
            minimum_stability_score=candidate_contract.replay_options.minimum_stability_score,
            require_material_policy_delta=False,
        )
        comparison = await self.stateful_policy_comparison(
            corpus=corpus,
            scope=scope,
            baseline_motive=active_contract.motive,
            candidate_motive=candidate_contract.motive,
            options=shadow_options,
        )
        passed = comparison.passed and comparison.policy_delta <= allowed_disposition_delta
        completed = self.graph.complete_policy_shadow_stage(
            stage_id=stage["stage_id"],
            observed_episode_count=len(corpus.episodes),
            report=comparison.model_dump(mode="json"),
            passed=passed,
            completed_at=datetime.now(UTC),
        )
        result = self._policy_shadow_stage_from_graph(completed, scope=scope)
        run = self._begin_operator_run(job_name="policy_shadow_stage", scope_key=scope.key)
        state = self.graph.graph_state_hash(scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.POLICY_SHADOW_STAGE_COMPLETED,
            decision_reason=(
                f"stage={result.stage_id};candidate={candidate_contract.contract_digest};"
                f"delta={comparison.policy_delta:.3f};allowed={allowed_disposition_delta:.3f}"
            ),
            decision_result="observed" if result.status == "passed" else "gated",
            now=result.completed_at or datetime.now(UTC),
            scope_key=scope.key,
            source_span_digest=result.corpus_digest,
            event_payload=result.model_dump_json(),
            event_payload_digest=payload_digest(result.model_dump(mode="json")),
            graph_state_hash_before=state,
            graph_state_hash_after=state,
        )
        self._checkpoint_operator_run(run)
        return result

    def activate_policy_alias(
        self, *, scope: MemoryScope, alias: str, candidate_contract_digest: str
    ) -> PolicyAliasState:
        """Atomically activate only a certified contract with a passing shadow stage."""
        self._require_authorized_scope(scope)
        from memotron.certification import PolicyAliasState

        before = self.graph.graph_state_hash(scope.key)
        state = self.graph.activate_policy_alias(
            scope_key=scope.key,
            alias=alias.strip(),
            candidate_contract_digest=candidate_contract_digest,
            now=datetime.now(UTC),
        )
        result = PolicyAliasState(scope=scope, **state)
        run = self._begin_operator_run(job_name="activate_policy_alias", scope_key=scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.POLICY_ALIAS_ACTIVATED,
            decision_reason=(
                f"alias={result.alias};current={result.contract_digest};"
                f"previous={result.previous_contract_digest or 'none'}"
            ),
            decision_result="transformed",
            now=result.updated_at,
            scope_key=scope.key,
            source_span_digest=result.contract_digest,
            event_payload=result.model_dump_json(),
            event_payload_digest=payload_digest(result.model_dump(mode="json")),
            graph_state_hash_before=before,
            graph_state_hash_after=self.graph.graph_state_hash(scope.key),
        )
        self._checkpoint_operator_run(run)
        return result

    def rollback_policy_alias(self, *, scope: MemoryScope, alias: str) -> PolicyAliasState:
        """Atomically restore the prior live policy version; memory rows stay untouched."""
        self._require_authorized_scope(scope)
        from memotron.certification import PolicyAliasState

        before = self.graph.graph_state_hash(scope.key)
        state = self.graph.rollback_policy_alias(
            scope_key=scope.key,
            alias=alias.strip(),
            now=datetime.now(UTC),
        )
        result = PolicyAliasState(scope=scope, **state)
        run = self._begin_operator_run(job_name="rollback_policy_alias", scope_key=scope.key)
        self._emit_receipt(
            run,
            decision_type=ReceiptDecisionType.POLICY_ALIAS_ROLLED_BACK,
            decision_reason=(
                f"alias={result.alias};restored={result.contract_digest};"
                f"reversible_to={result.previous_contract_digest or 'none'}"
            ),
            decision_result="transformed",
            now=result.updated_at,
            scope_key=scope.key,
            source_span_digest=result.contract_digest,
            event_payload=result.model_dump_json(),
            event_payload_digest=payload_digest(result.model_dump(mode="json")),
            graph_state_hash_before=before,
            graph_state_hash_after=self.graph.graph_state_hash(scope.key),
        )
        self._checkpoint_operator_run(run)
        return result

    def policy_rollout_status(self, *, scope: MemoryScope, alias: str, limit: int = 20) -> dict[str, Any]:
        """Return the read-only admin projection for one live policy alias."""
        self._require_authorized_scope(scope)
        from memotron.certification import PolicyAliasState

        alias_state = self.graph.policy_alias(scope_key=scope.key, alias=alias.strip())
        return {
            "alias": PolicyAliasState(scope=scope, **alias_state) if alias_state is not None else None,
            "active_contract": (
                self._policy_contract_version_from_graph(
                    self.graph.policy_contract(contract_digest=alias_state["contract_digest"])
                )
                if alias_state is not None
                else None
            ),
            "shadow_stages": tuple(
                self._policy_shadow_stage_from_graph(stage, scope=scope)
                for stage in self.graph.policy_shadow_stages(scope_key=scope.key, alias=alias.strip(), limit=limit)
            ),
        }

    def _policy_contract_version_from_graph(self, stored: dict[str, Any]) -> PolicyContractVersion:
        from memotron.certification import PolicyContractVersion, StatefulPolicyComparison, StatefulReplayOptions

        payload = stored["payload"]
        scope = MemoryScope.model_validate(payload["scope"])
        contract = PolicyContractVersion(
            scope=scope,
            alias=str(payload["alias"]),
            contract_digest=str(stored["contract_digest"]),
            motive=Motive.model_validate(payload["motive"]),
            replay_options=StatefulReplayOptions.model_validate(payload["replay_options"]),
            source_trace=dict(payload["source_trace"]),
            certification_passed=bool(stored["certification_passed"]),
            certification=StatefulPolicyComparison.model_validate(stored["certification"]),
            staged_at=stored["staged_at"],
        )
        if contract.scope.key != scope.key:
            raise RuntimeError("persisted policy contract scope is invalid")
        return contract

    def _policy_shadow_stage_from_graph(self, stored: dict[str, Any], *, scope: MemoryScope) -> PolicyShadowStage:
        from memotron.certification import PolicyShadowStage, StatefulPolicyComparison

        comparison = StatefulPolicyComparison.model_validate(stored["report"]) if stored["report"] is not None else None
        return PolicyShadowStage(
            scope=scope,
            stage_id=stored["stage_id"],
            alias=stored["alias"],
            active_contract_digest=stored["active_contract_digest"],
            candidate_contract_digest=stored["candidate_contract_digest"],
            corpus_digest=stored["corpus_digest"],
            required_episode_count=stored["required_episode_count"],
            observed_episode_count=stored["observed_episode_count"],
            allowed_disposition_delta=stored["allowed_disposition_delta"],
            status=stored["status"],
            started_at=stored["started_at"],
            completed_at=stored["completed_at"],
            comparison=comparison,
        )

    async def memory_bill_of_materials(self, *, run_uuid: str) -> MemoryBillOfMaterials:
        """Run-level memory BOM: policy, evidence, candidates, Merkle root, and state hashes."""
        from memotron.replay import build_memory_bill_of_materials

        return build_memory_bill_of_materials(self.graph.receipts, run_uuid=run_uuid)

    async def compare_policies(
        self,
        *,
        run_uuid: str,
        motives: list[Motive | str],
    ) -> PolicyComparisonReport:
        """Evaluate multiple Motives against the same frozen candidate stream."""
        if not motives:
            raise ValueError("compare_policies requires at least one motive")
        from memotron.replay import PolicyComparisonItem, PolicyComparisonReport

        reports = []
        baseline_materialized_count: int | None = None
        for motive in motives:
            report = await self.counterfactual(run_uuid=run_uuid, motive=motive)
            if baseline_materialized_count is None:
                baseline_materialized_count = report.original_materialized_count
            reports.append(
                PolicyComparisonItem(
                    motive_name=report.alternate_motive_name,
                    report=report,
                    signal_to_noise_delta=(report.original_materialized_count - report.alternate_materialized_count),
                )
            )
        return PolicyComparisonReport(
            run_uuid=run_uuid,
            baseline_materialized_count=baseline_materialized_count or 0,
            comparisons=tuple(reports),
        )

    async def policy_regression_gate(
        self,
        *,
        baseline_run_uuid: str,
        candidate_motive: Motive,
        corpus: Any,
        scope: MemoryScope,
    ) -> PolicyRegressionGateReport:
        """Certification + baseline preservation gate for a proposed Motive revision."""
        self._require_authorized_scope(scope)
        from memotron.replay import evaluate_policy_regression_gate

        certification = await self.certify_motive(motive=candidate_motive, corpus=corpus, scope=scope)
        comparison = await self.counterfactual(run_uuid=baseline_run_uuid, motive=candidate_motive)
        receipts = self.graph.receipts.receipts_for_run(baseline_run_uuid)
        return evaluate_policy_regression_gate(
            candidate_motive=candidate_motive,
            certification=certification,
            comparison=comparison,
            baseline_receipts=receipts,
        )

    async def verify_no_silent_mutations(self, *, scope: MemoryScope) -> NoSilentMutationReport:
        """Verify that the scope's live graph state is explained by receipted mutations."""
        self._require_authorized_scope(scope)
        from memotron.replay import verify_no_silent_mutation as _verify_no_silent_mutation

        return await _verify_no_silent_mutation(self.graph.receipts, self.graph, scope_key=scope.key)

    async def certification_bundle(
        self,
        *,
        scope: MemoryScope,
        run_uuid: str,
        policy_certificate: MotiveCertificate | None = None,
        policy_comparison: PolicyComparisonReport | None = None,
        policy_regression_gate: PolicyRegressionGateReport | None = None,
        erasure_certificate: ErasureCertificate | None = None,
    ) -> GovernanceCertificationBundle:
        """Exportable governance bundle for one scope/run.

        The bundle combines the BOM, byte replay proof, no-silent-mutation
        invariant, negative-space summary, coherence incident summary, optional
        policy certification/comparison/regression reports, and optional erasure
        certificate digest into one canonical digest.
        """
        self._require_authorized_scope(scope)
        from memotron.replay import GovernanceCertificationBundle

        bom = await self.memory_bill_of_materials(run_uuid=run_uuid)
        replay_proof = await self.byte_replay(run_uuid=run_uuid)
        no_silent = await self.verify_no_silent_mutations(scope=scope)
        negative_space = await self.negative_space(scope=scope)
        incidents = await self.coherence_incidents(scope=scope, limit=500)
        issued_at = datetime.now(UTC)
        payload = {
            "scope_key": scope.key,
            "run_uuid": run_uuid,
            "issued_at": issued_at.isoformat(),
            "memory_bill_of_materials": bom.model_dump(mode="json"),
            "replay_proof": replay_proof.model_dump(mode="json"),
            "no_silent_mutation": no_silent.model_dump(mode="json"),
            "negative_space_candidate_digests": sorted(
                {entry.candidate_digest for entry in negative_space if entry.candidate_digest is not None}
            ),
            "policy_certificate": (
                policy_certificate.model_dump(mode="json") if policy_certificate is not None else None
            ),
            "policy_comparison": (policy_comparison.model_dump(mode="json") if policy_comparison is not None else None),
            "policy_regression_gate": (
                policy_regression_gate.model_dump(mode="json") if policy_regression_gate is not None else None
            ),
            "coherence_incident_ids": sorted(incident.incident_id for incident in incidents),
            "erasure_certificate_digest": (
                erasure_certificate.certificate_digest if erasure_certificate is not None else None
            ),
        }
        return GovernanceCertificationBundle(
            scope_key=scope.key,
            run_uuid=run_uuid,
            issued_at=issued_at,
            memory_bill_of_materials=bom,
            replay_proof=replay_proof,
            no_silent_mutation=no_silent,
            negative_space_count=len(negative_space),
            negative_space_candidate_digests=tuple(payload["negative_space_candidate_digests"]),
            policy_certificate=policy_certificate,
            policy_comparison=policy_comparison,
            policy_regression_gate=policy_regression_gate,
            coherence_incident_count=len(incidents),
            coherence_incident_ids=tuple(payload["coherence_incident_ids"]),
            erasure_certificate_digest=payload["erasure_certificate_digest"],
            bundle_digest=payload_digest(payload),
        )

    async def run_certification_formation(
        self,
        *,
        motive: Motive | str,
        corpus: Any,
        scope: MemoryScope,
    ) -> str:
        """WS-11: drive ONE receipted formation run over a fixture corpus ([0028] steps 710/720).

        Ingests the corpus episodes under ``scope`` with the Motive hint stamped,
        runs the configured FORMATION job, and returns the formation receipt
        ``run_uuid`` so :meth:`certify_motive` can verify the chain and compute the
        certification metrics.  The Motive must be resolvable by the formation job
        (present in the configured ``memory_bank`` or pinned on the job); corpus
        items must be :class:`~memotron.models.Episode` objects.
        """
        self._require_authorized_scope(scope)
        name = motive.name if isinstance(motive, Motive) else str(motive)
        formation_jobs = [j for j in self.config.jobs if j.kind == DreamJobKind.FORMATION]
        if not formation_jobs:
            raise RuntimeError("run_certification_formation requires a configured FORMATION dream job")
        episodes: list[Episode] = []
        for item in corpus:
            if not isinstance(item, Episode):
                raise RuntimeError(f"certification corpus items must be Episode objects (got {type(item).__name__})")
            metadata = dict(item.metadata)
            metadata.setdefault("motive", name)
            episodes.append(item.model_copy(update={"scope": scope, "metadata": metadata}))
        await self.add_episode_bulk(episodes)
        result = await self.run_dream_job(job_name=formation_jobs[0].name)
        run_uuid = result.job_runs[0].run_uuid if result.job_runs else None
        if not run_uuid:
            raise RuntimeError("certification formation produced no receipt run")
        return run_uuid
