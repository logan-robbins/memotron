"""Stateful Motive certification, fixture corpora, and public-suite adapters.

Certification is deliberately an isolated replay.  It never points a candidate
policy at the production graph: every repetition receives a fresh SQLite store
and processes the evidence chronologically, one episode/job cycle at a time.
That makes path-dependent formation, consolidation, pruning, receipts, and
utility events part of the evaluated behavior rather than an after-the-fact
candidate-stream approximation.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import string
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from memotron.config import (
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    MemoryBank,
    Motive,
    NodeInstruction,
    RelationshipInstruction,
    RollupConsolidationPolicy,
    default_config,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_JUDGE_MODEL,
    GATEWAY_API_KEY_ENV,
    OpenAICompatibleTransport,
)
from memotron.models import (
    Episode,
    EpisodeType,
    MemoryScope,
    MemoryType,
    OutcomeVerdict,
    RelationshipCardinality,
    RelationshipStatus,
    UseEventKind,
)
from memotron.receipts import ReceiptDecisionType, payload_digest


class CertificationUseOutcome(BaseModel):
    """A deterministic use/outcome history attached to a fixture corpus."""

    memory_type: MemoryType
    verdict: OutcomeVerdict
    task_run_id: str = Field(min_length=1)
    judged_after_seconds: int = Field(default=1, ge=0)


class CertificationCorpus(BaseModel):
    """An immutable chronological evidence corpus for isolated policy replay."""

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    episodes: tuple[Episode, ...]
    use_outcomes: tuple[CertificationUseOutcome, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("episodes", mode="before")
    @classmethod
    def _episodes_tuple(cls, value: Any) -> tuple[Episode, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("certification corpus episodes must be a list or tuple")
        return tuple(Episode.model_validate(item) if isinstance(item, dict) else item for item in value)

    @model_validator(mode="after")
    def _validate_sequence(self) -> CertificationCorpus:
        if not self.episodes:
            raise ValueError("certification corpus requires at least one episode")
        ids = [episode.uuid for episode in self.episodes]
        if len(ids) != len(set(ids)):
            raise ValueError("certification corpus episode UUIDs must be unique")
        if any(episode.reference_time.tzinfo is None for episode in self.episodes):
            raise ValueError("certification corpus episode reference times must be timezone-aware")
        return self

    @property
    def corpus_digest(self) -> str:
        return payload_digest(
            {
                "name": self.name,
                "source": self.source,
                "episodes": [episode.model_dump(mode="json") for episode in self.episodes],
                "use_outcomes": [item.model_dump(mode="json") for item in self.use_outcomes],
                "metadata": self.metadata,
            }
        )

    def chronological_episodes(self) -> tuple[Episode, ...]:
        return tuple(
            sorted(self.episodes, key=lambda episode: (episode.reference_time, episode.created_at, episode.uuid))
        )


class StatefulReplayOptions(BaseModel):
    """Pinned execution metadata and hard noise gates for policy certification."""

    model_identifier: str = Field(min_length=1)
    temperature: float = Field(ge=0.0, le=2.0)
    repetitions: int = Field(default=3, ge=2)
    minimum_stability_score: float = Field(default=0.95, ge=0.0, le=1.0)
    #: T1-29. The tunable ACCURACY bar for `run_public_benchmark`, distinct from the
    #: stability bar above -- stability measures agreement BETWEEN repetitions and is
    #: therefore maximised by consistent total failure, so it cannot serve as a quality
    #: gate on its own.
    #:
    #: Defaults to 0.0 deliberately, meaning "no bar beyond the unconditional zero-score
    #: check in `run_public_benchmark`". Choosing a real number here (0.5? 0.8?) without a
    #: measured baseline per suite would be a threshold calibrated against nothing, which is
    #: the anti-pattern this branch keeps finding. Configure it per suite once you have one.
    minimum_mean_score: float = Field(default=0.0, ge=0.0, le=1.0)
    require_material_policy_delta: bool = True


class CandidateDispositionDiff(BaseModel):
    candidate_key: str
    baseline_disposition: str | None = None
    candidate_disposition: str | None = None


class EndStateGraphDiff(BaseModel):
    added_rows: tuple[dict[str, Any], ...] = ()
    removed_rows: tuple[dict[str, Any], ...] = ()
    changed_rows: tuple[dict[str, Any], ...] = ()

    @property
    def change_count(self) -> int:
        return len(self.added_rows) + len(self.removed_rows) + len(self.changed_rows)


class LifecycleInvariantResult(BaseModel):
    name: str
    passed: bool
    detail: str


class StatefulReplaySample(BaseModel):
    policy_name: str
    repetition: int = Field(ge=0)
    model_identifier: str
    temperature: float
    candidate_dispositions: dict[str, str]
    graph_rows: tuple[dict[str, Any], ...]
    protected_invariants: tuple[LifecycleInvariantResult, ...]
    run_uuids: tuple[str, ...]
    graph_state_hash: str


class StatefulPolicyComparison(BaseModel):
    """Three-layer stateful comparison and its replay-noise analysis."""

    corpus_name: str
    corpus_digest: str = Field(min_length=64, max_length=64)
    scope: MemoryScope
    baseline_motive_name: str
    candidate_motive_name: str
    baseline_contract_digest: str = Field(min_length=64, max_length=64)
    candidate_contract_digest: str = Field(min_length=64, max_length=64)
    options: StatefulReplayOptions
    candidate_disposition_diffs: tuple[CandidateDispositionDiff, ...]
    end_state_graph_diff: EndStateGraphDiff
    protected_invariants: tuple[LifecycleInvariantResult, ...]
    baseline_flip_rate: float = Field(ge=0.0, le=1.0)
    candidate_flip_rate: float = Field(ge=0.0, le=1.0)
    replay_flip_rate: float = Field(ge=0.0, le=1.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    policy_delta: float = Field(ge=0.0, le=1.0)
    passed: bool
    failures: tuple[str, ...] = ()
    baseline_samples: tuple[StatefulReplaySample, ...]
    candidate_samples: tuple[StatefulReplaySample, ...]


class PolicyContractVersion(BaseModel):
    """One immutable compiled Motive contract staged for shadow/activation."""

    scope: MemoryScope
    alias: str = Field(min_length=1)
    contract_digest: str = Field(min_length=64, max_length=64)
    motive: Motive
    replay_options: StatefulReplayOptions
    source_trace: dict[str, str]
    certification_passed: bool
    certification: StatefulPolicyComparison
    staged_at: datetime


class PolicyAliasState(BaseModel):
    scope: MemoryScope
    alias: str
    contract_digest: str
    previous_contract_digest: str | None = None
    updated_at: datetime


class PolicyShadowStage(BaseModel):
    stage_id: str
    scope: MemoryScope
    alias: str
    active_contract_digest: str
    candidate_contract_digest: str
    corpus_digest: str
    required_episode_count: int
    observed_episode_count: int
    allowed_disposition_delta: float
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    comparison: StatefulPolicyComparison | None = None


class PublicBenchmarkQuestion(BaseModel):
    """One scored public-suite question, kept separate from runtime input."""

    question_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expected_answers: tuple[str, ...]
    category: str = Field(min_length=1)
    asked_at: datetime
    evidence_identifiers: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("expected_answers", mode="before")
    @classmethod
    def _expected_answers_tuple(cls, value: Any) -> tuple[str, ...]:
        values = (value,) if isinstance(value, str) else value
        if not isinstance(values, (list, tuple)):
            raise ValueError("benchmark expected_answers must be a string or a list of strings")
        normalized = tuple(item.strip() for item in values if isinstance(item, str) and item.strip())
        if len(normalized) != len(values) or not normalized:
            raise ValueError("benchmark expected_answers must contain non-blank strings only")
        return normalized

    @field_validator("asked_at")
    @classmethod
    def _require_aware_question_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("benchmark question asked_at must be timezone-aware")
        return value.astimezone(UTC)


class PublicBenchmarkScenario(BaseModel):
    """Independent history and questions from an official public benchmark."""

    scenario_id: str = Field(min_length=1)
    source: str = Field(min_length=1)
    corpus: CertificationCorpus
    questions: tuple[PublicBenchmarkQuestion, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("questions", mode="before")
    @classmethod
    def _questions_tuple(cls, value: Any) -> tuple[PublicBenchmarkQuestion, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("benchmark scenario questions must be a list or tuple")
        return tuple(PublicBenchmarkQuestion.model_validate(item) if isinstance(item, dict) else item for item in value)

    @model_validator(mode="after")
    def _validate_questions(self) -> PublicBenchmarkScenario:
        if not self.questions:
            raise ValueError("benchmark scenario requires at least one question")
        question_ids = [item.question_id for item in self.questions]
        if len(question_ids) != len(set(question_ids)):
            raise ValueError("benchmark scenario question IDs must be unique")
        final_episode_time = self.corpus.chronological_episodes()[-1].reference_time
        if any(item.asked_at < final_episode_time for item in self.questions):
            raise ValueError("benchmark questions must be asked after their scenario history")
        return self

    @property
    def scenario_digest(self) -> str:
        return payload_digest(
            {
                "scenario_id": self.scenario_id,
                "source": self.source,
                "corpus_digest": self.corpus.corpus_digest,
                "questions": [item.model_dump(mode="json") for item in self.questions],
                "metadata": self.metadata,
            }
        )


class PublicBenchmarkSuite(BaseModel):
    """Immutable public-suite scenarios ready for isolated stateful evaluation."""

    name: str = Field(min_length=1)
    source: str = Field(min_length=1)
    scenarios: tuple[PublicBenchmarkScenario, ...]
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("scenarios", mode="before")
    @classmethod
    def _scenarios_tuple(cls, value: Any) -> tuple[PublicBenchmarkScenario, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("public benchmark scenarios must be a list or tuple")
        return tuple(PublicBenchmarkScenario.model_validate(item) if isinstance(item, dict) else item for item in value)

    @model_validator(mode="after")
    def _validate_scenarios(self) -> PublicBenchmarkSuite:
        if not self.scenarios:
            raise ValueError("public benchmark suite requires at least one scenario")
        scenario_ids = [item.scenario_id for item in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("public benchmark scenario IDs must be unique")
        if any(item.source != self.source for item in self.scenarios):
            raise ValueError("all public benchmark scenarios must have the suite source")
        return self

    @property
    def suite_digest(self) -> str:
        return payload_digest(
            {
                "name": self.name,
                "source": self.source,
                "scenarios": [item.scenario_digest for item in self.scenarios],
                "metadata": self.metadata,
            }
        )


class BenchmarkAnswerRequest(BaseModel):
    """Runtime input for one public question; it intentionally contains no gold answer."""

    source: str
    suite_digest: str = Field(min_length=64, max_length=64)
    scenario_id: str
    scenario_digest: str = Field(min_length=64, max_length=64)
    question_id: str
    category: str
    question: str
    asked_at: datetime
    motive_name: str
    model_identifier: str
    profile_context: str
    profile_relationship_uuids: tuple[str, ...]


class PublicBenchmarkCaseScore(BaseModel):
    scenario_id: str
    question_id: str
    category: str
    score: float = Field(ge=0.0, le=1.0)
    response_digest: str = Field(min_length=64, max_length=64)
    profile_relationship_uuids: tuple[str, ...]
    lifecycle_invariants: tuple[LifecycleInvariantResult, ...]


class PublicBenchmarkSample(BaseModel):
    repetition: int = Field(ge=0)
    scores: tuple[PublicBenchmarkCaseScore, ...]
    mean_score: float = Field(ge=0.0, le=1.0)


class PublicBenchmarkReport(BaseModel):
    """Externally judged benchmark result for one Motive under pinned replay options."""

    suite_name: str
    suite_source: str
    suite_digest: str = Field(min_length=64, max_length=64)
    motive_name: str
    options: StatefulReplayOptions
    answer_runtime_identifier: str = Field(min_length=1)
    judge_identifier: str = Field(min_length=1)
    samples: tuple[PublicBenchmarkSample, ...]
    mean_score: float = Field(ge=0.0, le=1.0)
    score_standard_deviation: float = Field(ge=0.0)
    stability_score: float = Field(ge=0.0, le=1.0)
    lifecycle_invariants_passed: bool
    passed: bool
    failures: tuple[str, ...] = ()


class OpenAICompatibleBenchmarkRuntime(OpenAICompatibleTransport):
    """Pinned OpenAI-compatible answer runtime for public benchmark scoring.

    The runtime sees a :class:`BenchmarkAnswerRequest`, which deliberately
    omits gold answers.  It can also issue a separate, bounded completion for
    the LongMemEval official judge protocol after that response has been
    produced.  API keys are resolved at call time and never serialized into a
    certification report.

    This is the SIXTH OpenAI-compatible transport in the package, and it shares
    :class:`~memotron.gateway.OpenAICompatibleTransport`'s auth block — key
    resolution, ``Authorization``/``Content-Type``, the ``/chat/completions``
    Request, the single ``urlopen`` — with the other five.  That block was
    duplicated here verbatim, which made every fix to it a sixth edit nobody
    knew to make.

    Three things stay local, and are the reason it inherits the TRANSPORT layer
    rather than :class:`~memotron.gateway.OpenAICompatibleChatTransport`:

    * **The payload.**  It carries a caller-chosen ``temperature`` (the
      identifier pins it, so a benchmark score is reproducible) and deliberately
      omits ``response_format``, because a benchmark answer is prose, not JSON.
      ``_chat_payload`` hard-codes ``temperature: 0`` and a JSON response
      format; it is not this runtime's body.
    * **The field validation.**  Its messages are prefixed
      ``benchmark runtime …``, and its ``temperature`` range check sits between
      the ``api_key`` and ``timeout_seconds`` checks.  Both are observable, so
      the checks run here first and hand the base already-normalised values.
    * **The response decode.**  It fuses the JSON error and the shape error into
      one message and rejects blank content, where the chat layer raises three
      distinct messages and accepts ``""``.
    """

    ANSWER_SYSTEM_PROMPT = (
        "Answer the benchmark question using only the supplied memory profile. "
        "If the profile does not establish an answer, say that the information "
        "is unavailable. Return only the concise answer, without commentary."
    )

    def __init__(
        self,
        *,
        model: str = DEFAULT_GATEWAY_JUDGE_MODEL,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        temperature: float = 0.0,
        timeout_seconds: float = 60.0,
    ) -> None:
        # Normalise and validate HERE, in this exact order, then hand the base
        # values it can only agree with. Both the wording ("benchmark runtime
        # model cannot be blank", not "model cannot be blank") and the position
        # of the temperature check (before timeout_seconds, unlike the base's
        # five-check sequence) are observable, and this refactor is not allowed
        # to move them. The base's own checks then pass trivially -- five
        # redundant `if`s is the price of not changing an error message.
        stripped_model = model.strip()
        stripped_base_url = base_url.strip().rstrip("/")
        stripped_api_key_env = api_key_env.strip()
        stripped_api_key = api_key.strip() if api_key is not None else None
        self.temperature = float(temperature)
        numeric_timeout = float(timeout_seconds)
        if not stripped_model:
            raise ValueError("benchmark runtime model cannot be blank")
        if not stripped_base_url:
            raise ValueError("benchmark runtime base_url cannot be blank")
        if not stripped_api_key_env:
            raise ValueError("benchmark runtime api_key_env cannot be blank")
        if api_key is not None and not stripped_api_key:
            raise ValueError("benchmark runtime api_key cannot be blank")
        if not 0.0 <= self.temperature <= 2.0:
            raise ValueError("benchmark runtime temperature must be between zero and two")
        if numeric_timeout <= 0:
            raise ValueError("benchmark runtime timeout_seconds must be greater than zero")
        super().__init__(
            model=stripped_model,
            base_url=stripped_base_url,
            api_key_env=stripped_api_key_env,
            api_key=stripped_api_key,
            timeout_seconds=numeric_timeout,
            error_label="benchmark completion",
            # NOT "benchmark completion request": this runtime says "benchmark
            # completion failed with HTTP ...", and the wording is its own.
            request_label="benchmark completion",
        )

    @property
    def identifier(self) -> str:
        return f"openai-compatible:{self.model}@temperature={self.temperature:g}"

    async def __call__(self, request: BenchmarkAnswerRequest) -> str:
        return await self.complete(
            system_prompt=self.ANSWER_SYSTEM_PROMPT,
            user_prompt=(
                f"Source: {request.source}\n"
                f"Question ID: {request.question_id}\n"
                f"Question: {request.question}\n\n"
                f"Memory profile:\n{request.profile_context}\n\n"
                "Answer:"
            ),
            max_tokens=512,
        )

    async def complete(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("benchmark completion system_prompt must be non-blank")
        if not isinstance(user_prompt, str) or not user_prompt.strip():
            raise ValueError("benchmark completion user_prompt must be non-blank")
        if not isinstance(max_tokens, int) or max_tokens <= 0:
            raise ValueError("benchmark completion max_tokens must be a positive integer")
        return await asyncio.to_thread(
            self._complete_sync,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            max_tokens=max_tokens,
        )

    def _complete_sync(self, *, system_prompt: str, user_prompt: str, max_tokens: int) -> str:
        # Key first, payload second -- the order this runtime has always had.
        api_key = self._resolve_api_key()
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        response_body = self._post("/chat/completions", payload, api_key=api_key)
        # Local decode: one message for "no usable content", whatever the cause,
        # and blank content is a failure. The chat layer raises three distinct
        # messages and accepts "", which is why this is not `_chat_content`.
        try:
            completion = json.loads(response_body)
            content = completion["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ValueError("benchmark completion did not contain choices[0].message.content") from exc
        if not isinstance(content, str) or not content.strip():
            raise ValueError("benchmark completion content must be non-blank text")
        return content.strip()


class LongMemEvalOfficialJudge:
    """The official LongMemEval yes/no LLM judge protocol.

    The prompt wording is sourced from LongMemEval's ``evaluate_qa.py`` at
    commit ``9e0b455``.  It is intentionally separate from the answer runtime:
    only this post-answer judge receives reference answers.
    """

    def __init__(self, runtime: OpenAICompatibleBenchmarkRuntime) -> None:
        self.runtime = runtime

    @property
    def identifier(self) -> str:
        return f"longmemeval-official-llm-judge@9e0b455:{self.runtime.identifier}"

    async def __call__(self, question: PublicBenchmarkQuestion, response: str) -> bool:
        if not isinstance(response, str) or not response.strip():
            raise ValueError("LongMemEval judge response must be non-blank text")
        answer = question.expected_answers[0]
        if "_abs" in question.question_id:
            prompt = (
                "I will give you an unanswerable question, an explanation, and a response from a model. "
                "Please answer yes if the model correctly identifies the question as unanswerable. "
                "The model could say that the information is incomplete, or some other information is given "
                "but the asked information is not.\n\n"
                f"Question: {question.question}\n\nExplanation: {answer}\n\n"
                f"Model Response: {response}\n\n"
                "Does the model correctly identify the question as unanswerable? Answer yes or no only."
            )
        elif question.category == "temporal-reasoning":
            prompt = (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response is equivalent to the correct answer or contains all the intermediate steps "
                "to get the correct answer, you should also answer yes. If the response only contains a subset "
                "of the information required by the answer, answer no. In addition, do not penalize off-by-one "
                "errors for the number of days. If the question asks for the number of days/weeks/months, etc., "
                "and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the "
                "model's response is still correct.\n\n"
                f"Question: {question.question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only."
            )
        elif question.category == "knowledge-update":
            prompt = (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response contains some previous information along with an updated answer, the response "
                "should be considered as correct as long as the updated answer is the required answer.\n\n"
                f"Question: {question.question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only."
            )
        elif question.category == "single-session-preference":
            prompt = (
                "I will give you a question, a rubric for desired personalized response, and a response from a "
                "model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
                "The model does not need to reflect all the points in the rubric. The response is correct as long "
                "as it recalls and utilizes the user's personal information correctly.\n\n"
                f"Question: {question.question}\n\nRubric: {answer}\n\n"
                f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only."
            )
        elif question.category in {"single-session-user", "single-session-assistant", "multi-session"}:
            prompt = (
                "I will give you a question, a correct answer, and a response from a model. "
                "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
                "If the response is equivalent to the correct answer or contains all the intermediate steps to "
                "get the correct answer, you should also answer yes. If the response only contains a subset of "
                "the information required by the answer, answer no.\n\n"
                f"Question: {question.question}\n\nCorrect Answer: {answer}\n\n"
                f"Model Response: {response}\n\nIs the model response correct? Answer yes or no only."
            )
        else:
            raise ValueError(f"unsupported LongMemEval question category: {question.category!r}")
        verdict = await self.runtime.complete(
            system_prompt="You are a strict benchmark evaluator. Follow the user's answer format exactly.",
            user_prompt=prompt,
            max_tokens=10,
        )
        return "yes" in verdict.casefold()


class LocalOfficialBenchmarkJudge:
    """Deterministic public metrics from the LoCoMo and MemoryAgentBench repos."""

    _LOCOMO_COMMIT = "3eb6f2c"
    _MEMORY_AGENT_BENCH_COMMIT = "455306d"

    def __init__(self, *, source: str) -> None:
        normalized = source.strip()
        if normalized not in {"LoCoMo", "MemoryAgentBench"}:
            raise ValueError("local official judge source must be 'LoCoMo' or 'MemoryAgentBench'")
        self.source = normalized
        # nltk is imported HERE, not at module scope, so that `import memotron` does not
        # pull a full NLP toolkit into every process. It is used for exactly one thing --
        # Porter stemming of LoCoMo tokens in `_locomo_f1` -- and only this class needs it,
        # so it ships as the `benchmark` extra rather than a required dependency. The
        # deployed image (`uv sync --no-dev --extra postgres --extra otel`) therefore does
        # not carry nltk at all, which is the point: no deployed MCP tool or admin route
        # constructs this class.
        try:
            from nltk.stem import PorterStemmer
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised by the dev group
            raise ModuleNotFoundError(
                "the local official judge needs nltk, which is an optional dependency; "
                "install it with `uv sync --extra benchmark` (or `pip install "
                "'jedai-memotron[benchmark]'`). Custom judges do not require it."
            ) from exc

        self._porter = PorterStemmer()

    @property
    def identifier(self) -> str:
        if self.source == "LoCoMo":
            return f"locomo-official-f1@{self._LOCOMO_COMMIT}"
        return f"memoryagentbench-official-metrics@{self._MEMORY_AGENT_BENCH_COMMIT}"

    def __call__(self, question: PublicBenchmarkQuestion, response: str) -> float:
        if not isinstance(response, str) or not response.strip():
            raise ValueError("local official judge response must be non-blank text")
        if self.source == "LoCoMo":
            return self._score_locomo(question, response)
        return self._score_memory_agent_bench(question, response)

    @staticmethod
    def _locomo_normalize(value: str) -> str:
        without_commas = value.replace(",", "")
        without_articles = re.sub(r"\b(a|an|the|and)\b", " ", without_commas, flags=re.IGNORECASE)
        without_punctuation = "".join(char for char in without_articles if char not in string.punctuation)
        return " ".join(without_punctuation.casefold().split())

    @staticmethod
    def _memory_agent_bench_normalize(value: str) -> str:
        lowered = value.casefold()
        without_punctuation = "".join(char for char in lowered if char not in string.punctuation)
        without_articles = re.sub(r"\b(a|an|the)\b", " ", without_punctuation)
        return " ".join(without_articles.split())

    def _locomo_f1(self, prediction: str, reference: str) -> float:
        prediction_tokens = [self._porter.stem(token) for token in self._locomo_normalize(prediction).split()]
        reference_tokens = [self._porter.stem(token) for token in self._locomo_normalize(reference).split()]
        if not prediction_tokens or not reference_tokens:
            return 0.0
        common = Counter(prediction_tokens) & Counter(reference_tokens)
        matched = sum(common.values())
        if not matched:
            return 0.0
        precision = matched / len(prediction_tokens)
        recall = matched / len(reference_tokens)
        return (2.0 * precision * recall) / (precision + recall)

    def _score_locomo(self, question: PublicBenchmarkQuestion, response: str) -> float:
        category = question.category
        if category == "5":
            response_lower = response.casefold()
            return 1.0 if "no information available" in response_lower or "not mentioned" in response_lower else 0.0
        if category in {"2", "3", "4"}:
            return max(self._locomo_f1(response, answer) for answer in question.expected_answers)
        if category == "1":
            prediction_parts = [part.strip() for part in response.split(",") if part.strip()]
            if not prediction_parts:
                return 0.0
            answer_scores: list[float] = []
            for answer in question.expected_answers:
                reference_parts = [part.strip() for part in answer.split(",") if part.strip()]
                if not reference_parts:
                    continue
                answer_scores.append(
                    sum(
                        max(self._locomo_f1(prediction, reference) for prediction in prediction_parts)
                        for reference in reference_parts
                    )
                    / len(reference_parts)
                )
            return max(answer_scores, default=0.0)
        raise ValueError(f"unsupported LoCoMo question category: {category!r}")

    def _score_memory_agent_bench(self, question: PublicBenchmarkQuestion, response: str) -> float:
        metric = str(question.metadata.get("official_metric", "")).strip()
        normalized_response = self._memory_agent_bench_normalize(response)
        if metric == "substring_exact_match":
            return float(
                any(
                    self._memory_agent_bench_normalize(answer) in normalized_response
                    for answer in question.expected_answers
                )
            )
        if metric == "exact_match":
            return float(
                any(
                    self._memory_agent_bench_normalize(answer) == normalized_response
                    for answer in question.expected_answers
                )
            )
        raise ValueError(
            "MemoryAgentBench question lacks a supported official_metric; "
            "use a source export with explicit task provenance"
        )


_PROFILE_FACT_LINE_PATTERN = re.compile(
    r"^- (?:\[PINNED\] )?\[[A-Z_]+\] (?P<fact>.+) "
    r"\(confidence=\d+\.\d{2}, observed_count=\d+\)$"
)
"""One rendered profile fact line (``Memotron._profile_fact_line`` format)."""

_EXECUTOR_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "into",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "their",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "whom",
        "why",
        "will",
        "with",
    }
)


class ProfileTopEvidenceExecutor:
    """Deterministic, fully offline benchmark answer runtime (WS-22 T30).

    Answers each question from the formed memory profile ONLY — no model, no
    network, and (by the ``run_public_benchmark`` contract) no gold answer.
    The inlined fact lines are parsed back out of the rendered profile context;
    each fact is scored by content-token overlap with the question (casefolded,
    punctuation-stripped, stopwords removed) and the best fact's text is
    returned VERBATIM as the answer — so the judge scores retrieval selection,
    never span extraction or paraphrase quality.  Ties break on (higher
    overlap, earlier profile position); zero overlap answers
    ``"no information available"`` rather than inventing anything.  Multi-hop
    questions (LoCoMo category ``"1"``, scored per official protocol as
    comma-separated answer parts) return the top TWO facts comma-joined so
    both join legs are visible to the per-part metric.  Everything here is
    pure and deterministic: same profile + question always → same answer.
    """

    identifier = "profile-top-evidence@v1"

    NO_ANSWER = "no information available"

    @staticmethod
    def _content_tokens(text: str) -> frozenset[str]:
        cleaned = "".join(char if char not in string.punctuation else " " for char in text.casefold())
        return frozenset(token for token in cleaned.split() if token and token not in _EXECUTOR_STOPWORDS)

    @classmethod
    def _profile_facts(cls, profile_context: str) -> list[str]:
        facts: list[str] = []
        seen: set[str] = set()
        for line in profile_context.splitlines():
            match = _PROFILE_FACT_LINE_PATTERN.match(line.strip())
            if match is None:
                continue
            fact = match.group("fact")
            if fact not in seen:
                seen.add(fact)
                facts.append(fact)
        return facts

    def __call__(self, request: BenchmarkAnswerRequest) -> str:
        question_tokens = self._content_tokens(request.question)
        ranked: list[tuple[int, int, str]] = []
        for index, fact in enumerate(self._profile_facts(request.profile_context)):
            overlap = len(question_tokens & self._content_tokens(fact))
            if overlap > 0:
                ranked.append((-overlap, index, fact))
        ranked.sort()
        if not ranked:
            return self.NO_ANSWER
        if request.category == "1" and len(ranked) > 1:
            return f"{ranked[0][2]}, {ranked[1][2]}"
        return ranked[0][2]


_TYPE_RELATIONSHIPS: dict[MemoryType, tuple[str, RelationshipCardinality]] = {
    MemoryType.ANCHOR: ("IDENTIFIES", RelationshipCardinality.MULTI_ACTIVE),
    MemoryType.PREFERENCE: ("PREFERS", RelationshipCardinality.MULTI_ACTIVE),
    MemoryType.REQUIREMENT: ("REQUIRES", RelationshipCardinality.SINGLE_ACTIVE),
    MemoryType.DIRECTIVE: ("SHOULD", RelationshipCardinality.MULTI_ACTIVE),
    MemoryType.STATE: ("HAS_STATE", RelationshipCardinality.SINGLE_ACTIVE),
    MemoryType.DECISION: ("DECIDES", RelationshipCardinality.MULTI_ACTIVE),
    MemoryType.INCIDENT: ("REPORTS_INCIDENT", RelationshipCardinality.MULTI_ACTIVE),
}


def certification_config(*, motives: Sequence[Motive]) -> DreamConfig:
    """Return the canonical all-type configuration used by built-in fixtures.

    The normal application configuration may intentionally expose only a small
    relationship vocabulary.  Certification fixtures cover every built-in
    Motive, so they use this explicit all-type instruction contract instead of
    silently dropping unsupported fixture evidence.
    """
    if not motives:
        raise ValueError("certification_config requires at least one motive")
    base = default_config()
    instructions = DreamInstructionSet(
        name="certification",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Extract named entities in certification evidence.",
                properties=(),
                strict_properties=False,
            ),
        ),
        relationship_instructions=tuple(
            RelationshipInstruction(
                type=relationship_type,
                source_label="Entity",
                target_label="Entity",
                query=f"Certification fixture for {memory_type.value} memory.",
                cardinality=cardinality,
                memory_type=memory_type,
            )
            for memory_type, (relationship_type, cardinality) in _TYPE_RELATIONSHIPS.items()
        ),
    )
    jobs = (
        DreamJob(
            name="certification-formation",
            kind="formation",
            cadence_seconds=1,
            instruction_set=instructions.name,
            prompt_profile="support-memory",
        ),
        DreamJob(
            name="certification-consolidation",
            kind="consolidation",
            cadence_seconds=1,
            instruction_set=instructions.name,
            rollup_consolidation=True,
            rollup_consolidation_policy=RollupConsolidationPolicy(
                cluster_threshold=0.65,
                min_cluster_size=3,
                max_depth=2,
            ),
        ),
        DreamJob(
            name="certification-pruning",
            kind="pruning",
            cadence_seconds=1,
            instruction_set=instructions.name,
        ),
    )
    return base.model_copy(
        update={
            "instruction_sets": (instructions,),
            "jobs": jobs,
            "memory_bank": MemoryBank(motives=tuple(motives)),
        }
    )


def builtin_motive_fixture_corpora() -> dict[str, CertificationCorpus]:
    """Return a complete mixed-lifecycle corpus for every shipped Motive.

    Each corpus contains mixed types, a temporal state replacement, a governed
    secret reference, repeated corroboration, and use/outcome history.  The
    named Motive determines which portions are allowed to materialize; the raw
    evidence remains identical across policies.
    """
    from memotron.memory_bank import builtin_memory_bank

    anchor = datetime(2026, 1, 1, tzinfo=UTC)
    fixture_scope = MemoryScope(kind="agent", scope_id="certification-fixture")
    relationships = _TYPE_RELATIONSHIPS
    episode_payloads = (
        (
            "baseline-mixed-evidence",
            anchor,
            [
                (MemoryType.ANCHOR, "Customer", "anchor", "priority customer"),
                (MemoryType.PREFERENCE, "Customer", "prefers", "concise status updates"),
                (MemoryType.REQUIREMENT, "Customer", "requires", "SOC2 evidence before launch"),
                (MemoryType.DIRECTIVE, "Agent", "should", "verify evidence before escalation"),
                (MemoryType.STATE, "Release", "current_state", "planning"),
                (MemoryType.DECISION, "Team", "decided", "use canonical receipts"),
                (MemoryType.INCIDENT, "Service", "incident", "timeout in checkout"),
            ],
        ),
        (
            "repeated-and-temporal-update",
            anchor + timedelta(days=1),
            [
                (MemoryType.PREFERENCE, "Customer", "prefers", "concise status updates"),
                (MemoryType.STATE, "Release", "current_state", "active"),
                (MemoryType.REQUIREMENT, "Vault", "secret_reference", "vault://customer/api-key"),
                (MemoryType.DIRECTIVE, "Agent", "should", "verify evidence before escalation"),
            ],
        ),
        (
            "rollup-and-contradiction-evidence",
            anchor + timedelta(days=2),
            [
                (MemoryType.PREFERENCE, "Customer", "prefers", "concise operational updates"),
                (MemoryType.PREFERENCE, "Customer", "prefers", "concise incident updates"),
                (MemoryType.INCIDENT, "Service", "incident", "checkout timeout mitigated"),
                (MemoryType.DECISION, "Team", "decided", "retain incident receipts"),
            ],
        ),
    )
    episodes: list[Episode] = []
    for index, (name, when, memories) in enumerate(episode_payloads):
        lines: list[str] = []
        for memory_type, subject, predicate, object_value in memories:
            relationship_type, _ = relationships[memory_type]
            lines.append(
                "Memory: "
                f"subject={subject}; predicate={predicate}; object={object_value}; "
                f"relationship_type={relationship_type}; confidence=0.9"
            )
        episodes.append(
            Episode(
                uuid=f"builtin-motive-fixture-{index}",
                name=name,
                body="\n".join(lines),
                source=EpisodeType.TEXT,
                source_description="built-in certification fixture",
                scope=fixture_scope,
                reference_time=when,
                metadata={"certification_fixture": True},
                instruction_set="certification",
            )
        )
    common = CertificationCorpus(
        name="builtin-motive-lifecycle-v1",
        source="memotron-built-in",
        episodes=tuple(episodes),
        use_outcomes=(
            CertificationUseOutcome(
                memory_type=MemoryType.REQUIREMENT,
                verdict=OutcomeVerdict.POSITIVE,
                task_run_id="fixture-requirement-success",
            ),
            CertificationUseOutcome(
                memory_type=MemoryType.DIRECTIVE,
                verdict=OutcomeVerdict.NEGATIVE,
                task_run_id="fixture-directive-negative",
            ),
        ),
        metadata={
            "coverage": [
                "mixed_types",
                "temporal_updates",
                "sensitive_secret_reference",
                "contradictions",
                "repeated_evidence",
                "use_outcome_history",
            ]
        },
    )
    return {motive.name: common for motive in builtin_memory_bank().motives}


def _read_json_file(path: str | Path, *, label: str) -> Any:
    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError(f"{label} fixture path does not exist or is not a file: {resolved}")
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} fixture is not valid JSON: {resolved}") from exc


def _text_from_turns(value: Any, *, label: str) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a non-empty string or a list of turns")
    lines: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            lines.append(item.strip())
            continue
        if not isinstance(item, dict):
            raise ValueError(f"{label} turn must be a string or object")
        speaker = item.get("speaker") or item.get("role") or item.get("name") or "speaker"
        text = item.get("text") or item.get("content") or item.get("message")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"{label} turn is missing non-blank text/content/message")
        lines.append(f"{speaker}: {text.strip()}")
    if not lines:
        raise ValueError(f"{label} has no usable turns")
    return "\n".join(lines)


def _expected_answer_strings(value: Any, *, label: str) -> tuple[str, ...]:
    """Normalize a benchmark answer field without inventing an answer.

    Official exports use strings for most answers but LongMemEval also has
    numeric answers.  A nested list is accepted for Parquet-derived values,
    then flattened one level; objects and booleans fail rather than being
    coerced into arbitrary textual representations.
    """
    values: list[Any]
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = []
        for item in value:
            if isinstance(item, (list, tuple)):
                values.extend(item)
            else:
                values.append(item)
    else:
        raise ValueError(f"{label} answer must be a string, number, or list of those values")
    normalized = tuple(
        str(item).strip()
        for item in values
        if isinstance(item, (str, int, float)) and not isinstance(item, bool) and str(item).strip()
    )
    if len(normalized) != len(values) or not normalized:
        raise ValueError(f"{label} answer must contain non-blank strings or numbers only")
    return normalized


def _memory_agent_bench_items(path: str | Path) -> list[dict[str, Any]]:
    """Read one untouched official JSON or Parquet export into records."""
    resolved = Path(path)
    if not resolved.is_file():
        raise ValueError(f"MemoryAgentBench fixture path does not exist or is not a file: {resolved}")
    if resolved.suffix.lower() == ".parquet":
        try:
            import pyarrow.parquet as parquet
        except ImportError as exc:  # pragma: no cover - guarded by project dependency
            raise RuntimeError("MemoryAgentBench Parquet loading requires pyarrow") from exc
        rows = parquet.read_table(resolved).to_pylist()
        if not all(isinstance(item, dict) for item in rows):
            raise ValueError("MemoryAgentBench Parquet rows must decode to objects")
        return rows
    data = _read_json_file(resolved, label="MemoryAgentBench")
    items = data.get("data") if isinstance(data, dict) and "data" in data else data
    if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
        raise ValueError("MemoryAgentBench fixture root must be an array or an object with a data array")
    return items


def _memory_agent_bench_official_metric(path: str | Path) -> str | None:
    """Map each released MAB task export to its documented metric."""
    filename = Path(path).name.casefold()
    if "accurate_retrieval" in filename or "conflict_resolution" in filename:
        return "substring_exact_match"
    if "long_range_understanding" in filename or "test_time_learning" in filename:
        return "exact_match"
    return None


def _parse_timestamp(value: Any, *, fallback: datetime, label: str) -> datetime:
    if value is None:
        return fallback
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} timestamp must be a non-blank ISO-8601 string")
    text = value.strip()
    try:
        # No `.replace("Z", "+00:00")`: `requires-python = ">=3.12"` and fromisoformat has
        # parsed a `Z` suffix natively since 3.11. The replacement was also broader than it
        # looked -- str.replace hits EVERY "Z", not just a trailing one.
        parsed = datetime.fromisoformat(text)
    except ValueError:
        parsed = None
        for timestamp_format in (
            "%Y/%m/%d (%a) %H:%M",  # LongMemEval, e.g. 2023/04/10 (Mon) 17:50
            "%Y/%m/%d %H:%M",
            "%I:%M %p on %d %B, %Y",  # LoCoMo, e.g. 1:56 pm on 8 May, 2023
        ):
            try:
                # These corpus formats carry no zone, so %z is impossible; the
                # return below normalises a naive result to UTC explicitly.
                parsed = datetime.strptime(text, timestamp_format)  # noqa: DTZ007
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"{label} timestamp must be ISO-8601, LongMemEval, or LoCoMo timestamp text") from None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


class LongMemEvalCorpusAdapter:
    """Read a local LongMemEval JSON export without downloading or mutating it.

    Accepts the official list root, or — like the sibling LoCoMo and
    MemoryAgentBench adapters — an object root ``{"_meta": {...}, "data":
    [...]}`` for repo-owned corpora authored in the LongMemEval item shape
    that must carry their provenance inline (source, license, generator).  A
    ``_meta`` block may override the suite ``suite_name`` / ``source`` /
    ``official_metric`` / ``official_judge_protocol`` labels so a synthetic
    corpus is never reported under the official benchmark's name; the classic
    list root is byte-identical legacy behaviour.
    """

    @staticmethod
    def _root_items(data: Any) -> tuple[dict[str, Any] | None, list]:
        if isinstance(data, dict) and "data" in data:
            meta = data.get("_meta")
            if meta is not None and not isinstance(meta, dict):
                raise ValueError("LongMemEval fixture _meta must be an object")
            items = data["data"]
        else:
            meta, items = None, data
        if not isinstance(items, list):
            raise ValueError("LongMemEval fixture root must be a JSON array or an object with a data array")
        return meta, items

    @staticmethod
    def _labels(meta: dict[str, Any] | None) -> tuple[str, str, str, str]:
        """(suite_name, source, official_metric, official_judge_protocol)."""
        meta = meta or {}

        def label(key: str, default: str) -> str:
            value = meta.get(key)
            if value is None:
                return default
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"LongMemEval fixture _meta.{key} must be non-blank text")
            return value.strip()

        return (
            label("suite_name", "LongMemEval-local"),
            label("source", "LongMemEval"),
            label("official_metric", "llm_as_judge"),
            label("official_judge_protocol", "longmemeval-evaluate_qa@9e0b455"),
        )

    @staticmethod
    def load(path: str | Path, *, scope: MemoryScope) -> CertificationCorpus:
        meta, data = LongMemEvalCorpusAdapter._root_items(_read_json_file(path, label="LongMemEval"))
        suite_name, source, _, _ = LongMemEvalCorpusAdapter._labels(meta)
        episodes: list[Episode] = []
        anchor = datetime(2025, 1, 1, tzinfo=UTC)
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"LongMemEval item {index} must be an object")
            item_id = item.get("question_id") or item.get("id") or f"longmemeval-{index}"
            sessions = item.get("haystack_sessions") or item.get("sessions") or item.get("conversation")
            if sessions is None:
                raise ValueError(f"LongMemEval item {item_id!r} lacks haystack_sessions/sessions/conversation")
            session_items = sessions if isinstance(sessions, list) else [sessions]
            raw_dates = item.get("haystack_dates")
            session_dates = raw_dates if isinstance(raw_dates, list) else []
            chronological_sessions: list[tuple[datetime, int, Any]] = []
            for session_index, session in enumerate(session_items):
                session_date = (
                    session_dates[session_index]
                    if session_index < len(session_dates)
                    else item.get("question_date") or item.get("date")
                )
                chronological_sessions.append(
                    (
                        _parse_timestamp(
                            session_date,
                            fallback=anchor + timedelta(minutes=len(episodes) + session_index),
                            label=f"LongMemEval item {item_id!r} session {session_index}",
                        ),
                        session_index,
                        session,
                    )
                )
            for reference_time, session_index, session in sorted(chronological_sessions):
                text = _text_from_turns(session, label=f"LongMemEval item {item_id!r} session {session_index}")
                episodes.append(
                    Episode(
                        uuid=f"longmemeval-{item_id}-{session_index}",
                        name=f"LongMemEval {item_id} session {session_index}",
                        body=text,
                        source=EpisodeType.TEXT,
                        source_description=f"{source} local corpus",
                        scope=scope,
                        reference_time=reference_time,
                        metadata={"external_suite": source, "question_id": str(item_id)},
                        instruction_set="certification",
                    )
                )
        corpus_metadata: dict[str, Any] = {"input_path": str(Path(path)), "raw_data_mutated": False}
        if meta is not None:
            corpus_metadata["_meta"] = meta
        return CertificationCorpus(
            name=suite_name,
            source=source,
            episodes=tuple(episodes),
            metadata=corpus_metadata,
        )

    @staticmethod
    def load_suite(path: str | Path, *, scope: MemoryScope) -> PublicBenchmarkSuite:
        """Build one scored scenario per official LongMemEval question."""
        meta, data = LongMemEvalCorpusAdapter._root_items(_read_json_file(path, label="LongMemEval"))
        suite_name, source, official_metric, official_judge_protocol = LongMemEvalCorpusAdapter._labels(meta)
        corpus = LongMemEvalCorpusAdapter.load(path, scope=scope)
        episodes_by_question: dict[str, list[Episode]] = {}
        for episode in corpus.episodes:
            question_id = str(episode.metadata["question_id"])
            episodes_by_question.setdefault(question_id, []).append(episode)
        scenarios: list[PublicBenchmarkScenario] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ValueError(f"LongMemEval item {index} must be an object")
            question_id = str(item.get("question_id") or item.get("id") or f"longmemeval-{index}")
            question = item.get("question")
            if not isinstance(question, str) or not question.strip():
                raise ValueError(f"LongMemEval item {question_id!r} lacks a non-blank question")
            all_scenario_episodes = tuple(episodes_by_question.get(question_id, ()))
            if not all_scenario_episodes:
                raise ValueError(f"LongMemEval item {question_id!r} has no loaded history episodes")
            source_question_date = _parse_timestamp(
                item.get("question_date"),
                fallback=max(episode.reference_time for episode in all_scenario_episodes) + timedelta(seconds=1),
                label=f"LongMemEval item {question_id!r} question_date",
            )
            # LongMemEval's supplied oracle haystacks can contain session
            # timestamps after ``question_date``.  The official task still
            # scores against the complete supplied haystack, so replay keeps
            # it intact.  ``asked_at`` is the internal post-replay query time;
            # the unmodified source timestamp is retained in metadata.
            scenario_episodes = all_scenario_episodes
            final_episode_time = max(episode.reference_time for episode in scenario_episodes)
            asked_at = max(source_question_date, final_episode_time)
            scenario_corpus = CertificationCorpus(
                name=f"{suite_name}-{question_id}",
                source=source,
                episodes=scenario_episodes,
                metadata={
                    "question_id": question_id,
                    "source_question_date": source_question_date.isoformat(),
                    "source_question_date_precedes_supplied_history": source_question_date < final_episode_time,
                    "raw_data_mutated": False,
                },
            )
            evidence = item.get("answer_session_ids", ())
            if isinstance(evidence, (str, int)):
                evidence = (str(evidence),)
            if not isinstance(evidence, (list, tuple)):
                raise ValueError(f"LongMemEval item {question_id!r} answer_session_ids must be a list")
            scenarios.append(
                PublicBenchmarkScenario(
                    scenario_id=question_id,
                    source=source,
                    corpus=scenario_corpus,
                    questions=(
                        PublicBenchmarkQuestion(
                            question_id=question_id,
                            question=question.strip(),
                            expected_answers=_expected_answer_strings(
                                item.get("answer"), label=f"LongMemEval item {question_id!r}"
                            ),
                            category=str(item.get("question_type") or "unknown"),
                            asked_at=asked_at,
                            evidence_identifiers=tuple(str(value) for value in evidence),
                            metadata={
                                "question_type": item.get("question_type"),
                                "official_metric": official_metric,
                                "official_judge_protocol": official_judge_protocol,
                            },
                        ),
                    ),
                    metadata={"raw_data_mutated": False},
                )
            )
        suite_metadata: dict[str, Any] = {
            "input_path": str(Path(path)),
            "official_metric": official_metric,
            "official_judge_protocol": official_judge_protocol,
            "raw_data_mutated": False,
        }
        if meta is not None:
            suite_metadata["_meta"] = meta
        return PublicBenchmarkSuite(
            name=suite_name,
            source=source,
            scenarios=tuple(scenarios),
            metadata=suite_metadata,
        )


class LoCoMoCorpusAdapter:
    """Read a local LoCoMo JSON export as chronological conversation episodes."""

    @staticmethod
    def load(path: str | Path, *, scope: MemoryScope) -> CertificationCorpus:
        data = _read_json_file(path, label="LoCoMo")
        conversations = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(conversations, list):
            raise ValueError("LoCoMo fixture root must be an array or an object with a data array")
        episodes: list[Episode] = []
        anchor = datetime(2025, 1, 1, tzinfo=UTC)
        for index, item in enumerate(conversations):
            if not isinstance(item, dict):
                raise ValueError(f"LoCoMo conversation {index} must be an object")
            conversation_id = item.get("conversation_id") or item.get("id") or f"locomo-{index}"
            conversation = item.get("conversation") or item.get("messages") or item.get("dialogue")
            if conversation is None:
                raise ValueError(f"LoCoMo conversation {conversation_id!r} lacks conversation/messages/dialogue")
            if isinstance(conversation, dict):
                session_entries = []
                for key, turns in conversation.items():
                    match = re.fullmatch(r"session_(\d+)", str(key))
                    if match is not None:
                        session_entries.append((int(match.group(1)), str(key), turns))
                if not session_entries:
                    raise ValueError(f"LoCoMo conversation {conversation_id!r} has no session_N turn arrays")
                for session_number, session_key, turns in sorted(session_entries):
                    episodes.append(
                        Episode(
                            uuid=f"locomo-{conversation_id}-{session_key}",
                            name=f"LoCoMo {conversation_id} session {session_number}",
                            body=_text_from_turns(
                                turns,
                                label=f"LoCoMo conversation {conversation_id!r} {session_key}",
                            ),
                            source=EpisodeType.TEXT,
                            source_description="LoCoMo local corpus",
                            scope=scope,
                            reference_time=_parse_timestamp(
                                conversation.get(f"{session_key}_date_time")
                                or item.get("date")
                                or item.get("timestamp"),
                                fallback=anchor + timedelta(minutes=len(episodes)),
                                label=f"LoCoMo conversation {conversation_id!r} {session_key}",
                            ),
                            metadata={
                                "external_suite": "LoCoMo",
                                "conversation_id": str(conversation_id),
                                "session_number": session_number,
                            },
                            instruction_set="certification",
                        )
                    )
            else:
                episodes.append(
                    Episode(
                        uuid=f"locomo-{conversation_id}",
                        name=f"LoCoMo {conversation_id}",
                        body=_text_from_turns(conversation, label=f"LoCoMo conversation {conversation_id!r}"),
                        source=EpisodeType.TEXT,
                        source_description="LoCoMo local corpus",
                        scope=scope,
                        reference_time=_parse_timestamp(
                            item.get("date") or item.get("timestamp"),
                            fallback=anchor + timedelta(minutes=index),
                            label=f"LoCoMo conversation {conversation_id!r}",
                        ),
                        metadata={"external_suite": "LoCoMo", "conversation_id": str(conversation_id)},
                        instruction_set="certification",
                    )
                )
        return CertificationCorpus(
            name="LoCoMo-local",
            source="LoCoMo",
            episodes=tuple(episodes),
            metadata={"input_path": str(Path(path)), "raw_data_mutated": False},
        )

    @staticmethod
    def load_suite(path: str | Path, *, scope: MemoryScope) -> PublicBenchmarkSuite:
        """Build one scored scenario per official LoCoMo conversation."""
        data = _read_json_file(path, label="LoCoMo")
        conversations = data.get("data") if isinstance(data, dict) and "data" in data else data
        if not isinstance(conversations, list):
            raise ValueError("LoCoMo fixture root must be an array or an object with a data array")
        corpus = LoCoMoCorpusAdapter.load(path, scope=scope)
        episodes_by_conversation: dict[str, list[Episode]] = {}
        for episode in corpus.episodes:
            conversation_id = str(episode.metadata["conversation_id"])
            episodes_by_conversation.setdefault(conversation_id, []).append(episode)
        scenarios: list[PublicBenchmarkScenario] = []
        for index, item in enumerate(conversations):
            if not isinstance(item, dict):
                raise ValueError(f"LoCoMo conversation {index} must be an object")
            conversation_id = str(item.get("conversation_id") or item.get("id") or f"locomo-{index}")
            qa_entries = item.get("qa")
            if not isinstance(qa_entries, list) or not qa_entries:
                raise ValueError(f"LoCoMo conversation {conversation_id!r} lacks a non-empty qa array")
            scenario_episodes = tuple(episodes_by_conversation.get(conversation_id, ()))
            if not scenario_episodes:
                raise ValueError(f"LoCoMo conversation {conversation_id!r} has no loaded history episodes")
            scenario_corpus = CertificationCorpus(
                name=f"LoCoMo-{conversation_id}",
                source="LoCoMo",
                episodes=scenario_episodes,
                metadata={"conversation_id": conversation_id, "raw_data_mutated": False},
            )
            final_episode_time = scenario_corpus.chronological_episodes()[-1].reference_time
            questions: list[PublicBenchmarkQuestion] = []
            for qa_index, qa in enumerate(qa_entries):
                if not isinstance(qa, dict):
                    raise ValueError(f"LoCoMo conversation {conversation_id!r} qa {qa_index} must be an object")
                question = qa.get("question")
                if not isinstance(question, str) or not question.strip():
                    raise ValueError(f"LoCoMo conversation {conversation_id!r} qa {qa_index} lacks a question")
                evidence = qa.get("evidence", ())
                if isinstance(evidence, (str, int)):
                    evidence = (str(evidence),)
                if not isinstance(evidence, (list, tuple)):
                    raise ValueError(f"LoCoMo conversation {conversation_id!r} qa {qa_index} evidence must be a list")
                questions.append(
                    PublicBenchmarkQuestion(
                        question_id=f"{conversation_id}:{qa_index}",
                        question=question.strip(),
                        expected_answers=_expected_answer_strings(
                            qa.get("answer") if "answer" in qa else qa.get("adversarial_answer"),
                            label=f"LoCoMo conversation {conversation_id!r} qa {qa_index}",
                        ),
                        category=str(qa.get("category") or "unknown"),
                        asked_at=final_episode_time + timedelta(seconds=1),
                        evidence_identifiers=tuple(str(value) for value in evidence),
                        metadata={
                            "qa_index": qa_index,
                            "answer_variant": "answer" if "answer" in qa else "adversarial_answer",
                            "official_metric": "f1" if str(qa.get("category")) != "5" else "adversarial_abstention",
                        },
                    )
                )
            scenarios.append(
                PublicBenchmarkScenario(
                    scenario_id=conversation_id,
                    source="LoCoMo",
                    corpus=scenario_corpus,
                    questions=tuple(questions),
                    metadata={"raw_data_mutated": False},
                )
            )
        return PublicBenchmarkSuite(
            name="LoCoMo-local",
            source="LoCoMo",
            scenarios=tuple(scenarios),
            metadata={
                "input_path": str(Path(path)),
                "official_metric": "category_f1_or_adversarial_abstention",
                "official_judge_protocol": "locomo-task_eval-evaluation@3eb6f2c",
                "raw_data_mutated": False,
            },
        )


class MemoryAgentBenchCorpusAdapter:
    """Read a local MemoryAgentBench JSON or official Parquet export."""

    @staticmethod
    def load(path: str | Path, *, scope: MemoryScope) -> CertificationCorpus:
        items = _memory_agent_bench_items(path)
        episodes: list[Episode] = []
        anchor = datetime(2025, 1, 1, tzinfo=UTC)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                raise ValueError(f"MemoryAgentBench item {index} must be an object")
            metadata = item.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"MemoryAgentBench item {index} metadata must be an object")
            item_id = item.get("id") or item.get("sample_id") or f"memoryagentbench-{index}"
            evidence = item.get("messages") or item.get("conversation") or item.get("sessions") or item.get("context")
            if evidence is None:
                raise ValueError(f"MemoryAgentBench item {item_id!r} lacks messages/conversation/sessions/context")
            episodes.append(
                Episode(
                    uuid=f"memoryagentbench-{item_id}",
                    name=f"MemoryAgentBench {item_id}",
                    body=_text_from_turns(evidence, label=f"MemoryAgentBench item {item_id!r}"),
                    source=EpisodeType.TEXT,
                    source_description="MemoryAgentBench local corpus",
                    scope=scope,
                    reference_time=_parse_timestamp(
                        item.get("timestamp") or item.get("date"),
                        fallback=anchor + timedelta(minutes=index),
                        label=f"MemoryAgentBench item {item_id!r}",
                    ),
                    metadata={"external_suite": "MemoryAgentBench", "item_id": str(item_id)},
                    instruction_set="certification",
                )
            )
        return CertificationCorpus(
            name="MemoryAgentBench-local",
            source="MemoryAgentBench",
            episodes=tuple(episodes),
            metadata={"input_path": str(Path(path)), "raw_data_mutated": False},
        )

    @staticmethod
    def load_suite(path: str | Path, *, scope: MemoryScope) -> PublicBenchmarkSuite:
        """Build one scored scenario per MemoryAgentBench row.

        The official Parquet records have one preassembled context and aligned
        ``questions``/``answers`` arrays.  JSON fixtures use the same fields.
        ``question_dates`` are retained as metadata rather than used to admit
        future context: a supplied context is always replayed before scoring.
        """
        items = _memory_agent_bench_items(path)
        corpus = MemoryAgentBenchCorpusAdapter.load(path, scope=scope)
        official_metric = _memory_agent_bench_official_metric(path)
        episodes_by_item = {str(episode.metadata["item_id"]): episode for episode in corpus.episodes}
        scenarios: list[PublicBenchmarkScenario] = []
        for index, item in enumerate(items):
            metadata = item.get("metadata")
            if metadata is not None and not isinstance(metadata, dict):
                raise ValueError(f"MemoryAgentBench item {index} metadata must be an object")
            item_id = str(item.get("id") or item.get("sample_id") or f"memoryagentbench-{index}")
            episode = episodes_by_item.get(item_id)
            if episode is None:
                raise ValueError(f"MemoryAgentBench item {item_id!r} has no loaded history episode")
            questions_raw = item.get("questions")
            answers_raw = item.get("answers")
            if not isinstance(questions_raw, list) or not questions_raw:
                raise ValueError(f"MemoryAgentBench item {item_id!r} lacks a non-empty questions array")
            if not isinstance(answers_raw, list) or len(answers_raw) != len(questions_raw):
                raise ValueError(f"MemoryAgentBench item {item_id!r} answers must align one-for-one with questions")
            source_metadata = metadata or {}
            question_ids = source_metadata.get("question_ids", ())
            question_types = source_metadata.get("question_types", ())
            keypoints = source_metadata.get("keypoints", ())
            if question_ids and (not isinstance(question_ids, list) or len(question_ids) != len(questions_raw)):
                raise ValueError(f"MemoryAgentBench item {item_id!r} question_ids must align with questions")
            if question_types and (not isinstance(question_types, list) or len(question_types) != len(questions_raw)):
                raise ValueError(f"MemoryAgentBench item {item_id!r} question_types must align with questions")
            if keypoints and not isinstance(keypoints, list):
                raise ValueError(f"MemoryAgentBench item {item_id!r} keypoints must be a list")
            keypoints_align_with_questions = bool(keypoints) and len(keypoints) == len(questions_raw)
            scenario_corpus = CertificationCorpus(
                name=f"MemoryAgentBench-{item_id}",
                source="MemoryAgentBench",
                episodes=(episode,),
                metadata={"item_id": item_id, "raw_data_mutated": False},
            )
            asked_at = episode.reference_time + timedelta(seconds=1)
            questions: list[PublicBenchmarkQuestion] = []
            for question_index, (question, answer) in enumerate(zip(questions_raw, answers_raw, strict=True)):
                if not isinstance(question, str) or not question.strip():
                    raise ValueError(
                        f"MemoryAgentBench item {item_id!r} question {question_index} must be non-blank text"
                    )
                source_question_id = question_ids[question_index] if question_ids else f"{item_id}:{question_index}"
                source_type = question_types[question_index] if question_types else "unknown"
                # Accurate Retrieval and Conflict Resolution provide
                # question-aligned evidence keypoints.  Long-Range
                # Understanding uses a context-level keypoint list, which is
                # preserved in scenario metadata but cannot be attributed to
                # a single question without inventing evidence alignment.
                source_keypoints = keypoints[question_index] if keypoints_align_with_questions else ()
                if isinstance(source_keypoints, (str, int)):
                    source_keypoints = (str(source_keypoints),)
                if not isinstance(source_keypoints, (list, tuple)):
                    raise ValueError(f"MemoryAgentBench item {item_id!r} keypoints {question_index} must be a list")
                questions.append(
                    PublicBenchmarkQuestion(
                        question_id=str(source_question_id),
                        question=question.strip(),
                        expected_answers=_expected_answer_strings(
                            answer, label=f"MemoryAgentBench item {item_id!r} question {question_index}"
                        ),
                        category=str(source_type or "unknown"),
                        asked_at=asked_at,
                        evidence_identifiers=tuple(str(value) for value in source_keypoints),
                        metadata={
                            "source_question_index": question_index,
                            "keypoints_question_aligned": keypoints_align_with_questions,
                            "official_metric": official_metric or "unspecified",
                        },
                    )
                )
            scenarios.append(
                PublicBenchmarkScenario(
                    scenario_id=item_id,
                    source="MemoryAgentBench",
                    corpus=scenario_corpus,
                    questions=tuple(questions),
                    metadata={"raw_data_mutated": False, "metadata": source_metadata},
                )
            )
        return PublicBenchmarkSuite(
            name="MemoryAgentBench-local",
            source="MemoryAgentBench",
            scenarios=tuple(scenarios),
            metadata={
                "input_path": str(Path(path)),
                "official_metric": official_metric or "unspecified",
                "official_judge_protocol": "memoryagentbench-eval_other_utils@455306d",
                "raw_data_mutated": False,
            },
        )


def _config_for_motive(base_config: DreamConfig, motive: Motive) -> DreamConfig:
    formation_jobs = [job for job in base_config.jobs if job.kind.value == "formation"]
    if not formation_jobs:
        raise ValueError("stateful certification requires a configuration with a FORMATION dream job")
    motives_by_name = {item.name: item for item in (base_config.memory_bank.motives if base_config.memory_bank else ())}
    motives_by_name[motive.name] = motive
    jobs = tuple(
        job.model_copy(update={"motive": motive.name}) if job.kind.value == "formation" else job
        for job in base_config.jobs
    )
    return base_config.model_copy(
        update={"memory_bank": MemoryBank(motives=tuple(motives_by_name.values())), "jobs": jobs}
    )


def _dispositions(receipts: Sequence[Any]) -> dict[str, str]:
    candidates: dict[str, str] = {}
    for receipt in receipts:
        digest = receipt.candidate_digest
        if not digest:
            continue
        key = f"{receipt.episode_uuid or ''}:{digest}"
        if receipt.decision_type == ReceiptDecisionType.CANDIDATE_EXTRACTED:
            candidates.setdefault(key, "candidate_extracted")
        elif key in candidates:
            candidates[key] = f"{receipt.decision_type.value}:{receipt.decision_result}"
    return dict(sorted(candidates.items()))


def _graph_rows(client: Any, *, scope: MemoryScope, config: DreamConfig) -> tuple[dict[str, Any], ...]:
    protected_types = {item.value for item in config.pruning.retention.protected_memory_types}
    rows: list[dict[str, Any]] = []
    for relationship in client.graph.relationships():
        props = relationship.properties
        if relationship.type == "MENTIONS" or props.get("scope_key") != scope.key:
            continue
        fact = str(client.graph.reveal(scope.key, props.get("fact", "")))
        fingerprint = str(props.get("truth_key") or payload_digest({"fact": fact, "type": relationship.type}))
        memory_type = str(props.get("memory_type", ""))
        rows.append(
            {
                "fingerprint": fingerprint,
                "relationship_type": relationship.type,
                "memory_type": memory_type,
                "fact_digest": props.get("fact_commitment") or payload_digest({"fact": fact}),
                "status": props.get("status"),
                "confidence": round(float(props.get("confidence", 0.0)), 8),
                "protected": memory_type in protected_types or bool(props.get("coherence_hold")),
                "active_in_context": props.get("active_in_context") is not False,
                "rollup_stale": bool(props.get("rollup_stale")),
                "observed_count": int(props.get("observed_count", 1)),
            }
        )
    return tuple(sorted(rows, key=lambda item: (str(item["fingerprint"]), str(item["fact_digest"]))))


def _lifecycle_invariants(
    client: Any,
    *,
    scope: MemoryScope,
    motive: Motive,
    config: DreamConfig,
    run_uuids: Sequence[str],
) -> tuple[LifecycleInvariantResult, ...]:
    allowed = {item.value for item in motive.allowed_memory_types}
    rows = _graph_rows(client, scope=scope, config=config)
    active = [row for row in rows if row["status"] == RelationshipStatus.ACTIVE.value]
    disallowed = [row for row in active if allowed and row["memory_type"] not in allowed]
    chain_failure_details: dict[str, tuple[str, ...]] = {}
    for run_uuid in run_uuids:
        # A no-op consolidation/pruning run may have a scheduler run UUID but
        # no ledger run.  There is no mutation or chain to verify in that case.
        if not client.graph.receipts.receipts_for_run(run_uuid):
            continue
        verification = client.graph.receipts.verify_chain(run_uuid)
        if not verification.valid:
            chain_failure_details[run_uuid] = verification.errors
    stale_live_rollups = [
        row
        for row in rows
        if row["memory_type"] == MemoryType.ROLLUP.value and row["rollup_stale"] and row["active_in_context"]
    ]
    protected_pruned = [row for row in rows if row["protected"] and row["status"] == RelationshipStatus.PRUNED.value]
    held_without_incident = [
        relationship.uuid
        for relationship in client.graph.relationships()
        if relationship.properties.get("scope_key") == scope.key
        and relationship.properties.get("coherence_hold") is True
        and not relationship.properties.get("coherence_incident_id")
    ]
    return (
        LifecycleInvariantResult(
            name="receipt_chain",
            passed=not chain_failure_details,
            detail="all sequential replay receipt chains verify"
            if not chain_failure_details
            else f"invalid receipt chains: {chain_failure_details}",
        ),
        LifecycleInvariantResult(
            name="retrieval_allocation",
            passed=not disallowed,
            detail="active retrieval candidates obey the Motive type allocation"
            if not disallowed
            else f"active rows violate Motive allocation: {len(disallowed)}",
        ),
        LifecycleInvariantResult(
            name="protected_retention",
            passed=not protected_pruned,
            detail="no protected row was pruned during replay"
            if not protected_pruned
            else f"protected rows pruned: {len(protected_pruned)}",
        ),
        LifecycleInvariantResult(
            name="rollup_invalidation",
            passed=not stale_live_rollups,
            detail="no stale derived rollup remains context-visible"
            if not stale_live_rollups
            else f"stale context-visible rollups: {len(stale_live_rollups)}",
        ),
        LifecycleInvariantResult(
            name="coherence_attribution",
            passed=not held_without_incident,
            detail="every anti-windup hold has an attributed incident"
            if not held_without_incident
            else f"holds without incident attribution: {held_without_incident}",
        ),
    )


async def _replay_corpus_lifecycle(
    *,
    client: Any,
    config: DreamConfig,
    corpus: CertificationCorpus,
    scope: MemoryScope,
    motive: Motive,
) -> tuple[str, ...]:
    """Execute the one canonical chronological lifecycle replay into ``client``."""
    run_uuids: list[str] = []
    formation = next(job for job in config.jobs if job.kind.value == "formation")
    for episode in corpus.chronological_episodes():
        metadata = {**episode.metadata, "motive": motive.name, "certification_corpus": corpus.name}
        replay_episode = episode.model_copy(update={"scope": scope, "metadata": metadata})
        await client.add_episode_bulk([replay_episode])
        result = await client.run_dream_job(job_name=formation.name, now=replay_episode.reference_time)
        if not result.job_runs or not result.job_runs[0].run_uuid:
            raise RuntimeError("sequential certification formation produced no receipted run")
        run_uuids.append(result.job_runs[0].run_uuid)

    for job in config.jobs:
        if job.kind.value not in {"consolidation", "pruning"}:
            continue
        result = await client.run_dream_job(job_name=job.name, now=corpus.chronological_episodes()[-1].reference_time)
        if result.job_runs and result.job_runs[0].run_uuid:
            run_uuids.append(result.job_runs[0].run_uuid)

    # The fixture's use/outcome history is processed only after all evidence
    # is formed, preserving outcome -> use -> memory attribution.
    for outcome in corpus.use_outcomes:
        relationship = next(
            (
                item
                for item in client.graph.active_relationships(scope=scope)
                if item.properties.get("memory_type") == outcome.memory_type.value
            ),
            None,
        )
        if relationship is None:
            continue
        used_at = corpus.chronological_episodes()[-1].reference_time + timedelta(seconds=outcome.judged_after_seconds)
        use = await client.record_memory_use(
            relationship_uuid=relationship.uuid,
            scope=scope,
            kind=UseEventKind.CITED_OR_USED,
            task_run_id=outcome.task_run_id,
            idempotency_key=f"{outcome.task_run_id}:use",
            used_at=used_at,
        )
        await client.record_memory_outcome(
            use_id=use.use_id,
            scope=scope,
            verdict=outcome.verdict,
            task_run_id=outcome.task_run_id,
            idempotency_key=f"{outcome.task_run_id}:outcome",
            judge_identity="certification-fixture",
            judge_version="v1",
            judged_at=used_at,
        )
    return tuple(run_uuids)


async def _run_sample(
    *,
    client_factory: Callable[[DreamConfig, Path], Any],
    base_config: DreamConfig,
    corpus: CertificationCorpus,
    scope: MemoryScope,
    motive: Motive,
    options: StatefulReplayOptions,
    repetition: int,
    scratch_dir: Path,
) -> StatefulReplaySample:
    config = _config_for_motive(base_config, motive)
    client = client_factory(config, scratch_dir / f"{motive.name}-{repetition}.sqlite")
    try:
        run_uuids = await _replay_corpus_lifecycle(
            client=client,
            config=config,
            corpus=corpus,
            scope=scope,
            motive=motive,
        )
        receipts = client.graph.receipts.receipts_for_scope(scope.key)
        return StatefulReplaySample(
            policy_name=motive.name,
            repetition=repetition,
            model_identifier=options.model_identifier,
            temperature=options.temperature,
            candidate_dispositions=_dispositions(receipts),
            graph_rows=_graph_rows(client, scope=scope, config=config),
            protected_invariants=_lifecycle_invariants(
                client, scope=scope, motive=motive, config=config, run_uuids=run_uuids
            ),
            run_uuids=run_uuids,
            graph_state_hash=client.graph.graph_state_hash(scope.key),
        )
    finally:
        client.graph.close()


def _flip_rate(samples: Sequence[StatefulReplaySample]) -> float:
    if len(samples) < 2:
        return 0.0
    reference = samples[0].candidate_dispositions
    differences = 0
    opportunities = 0
    for sample in samples[1:]:
        keys = set(reference) | set(sample.candidate_dispositions)
        opportunities += len(keys)
        differences += sum(reference.get(key) != sample.candidate_dispositions.get(key) for key in keys)
    return differences / opportunities if opportunities else 0.0


def _disposition_diff(
    baseline: StatefulReplaySample, candidate: StatefulReplaySample
) -> tuple[CandidateDispositionDiff, ...]:
    keys = sorted(set(baseline.candidate_dispositions) | set(candidate.candidate_dispositions))
    return tuple(
        CandidateDispositionDiff(
            candidate_key=key,
            baseline_disposition=baseline.candidate_dispositions.get(key),
            candidate_disposition=candidate.candidate_dispositions.get(key),
        )
        for key in keys
        if baseline.candidate_dispositions.get(key) != candidate.candidate_dispositions.get(key)
    )


def _graph_diff(baseline: StatefulReplaySample, candidate: StatefulReplaySample) -> EndStateGraphDiff:
    baseline_by_key = {f"{row['fingerprint']}:{row['fact_digest']}": row for row in baseline.graph_rows}
    candidate_by_key = {f"{row['fingerprint']}:{row['fact_digest']}": row for row in candidate.graph_rows}
    added = tuple(candidate_by_key[key] for key in sorted(set(candidate_by_key) - set(baseline_by_key)))
    removed = tuple(baseline_by_key[key] for key in sorted(set(baseline_by_key) - set(candidate_by_key)))
    changed = tuple(
        {
            "key": key,
            "baseline": baseline_by_key[key],
            "candidate": candidate_by_key[key],
        }
        for key in sorted(set(baseline_by_key) & set(candidate_by_key))
        if baseline_by_key[key] != candidate_by_key[key]
    )
    return EndStateGraphDiff(added_rows=added, removed_rows=removed, changed_rows=changed)


async def run_stateful_policy_comparison(
    *,
    client_factory: Callable[[DreamConfig, Path], Any],
    base_config: DreamConfig,
    corpus: CertificationCorpus,
    scope: MemoryScope,
    baseline_motive: Motive,
    candidate_motive: Motive,
    options: StatefulReplayOptions,
) -> StatefulPolicyComparison:
    """Run N fully isolated chronological replays for baseline and candidate."""
    if not callable(client_factory):
        raise ValueError("client_factory must be callable")
    baseline_contract_digest = payload_digest(
        {
            "motive": baseline_motive.model_dump(mode="json"),
            "base_config": base_config.model_dump(mode="json"),
            "model_identifier": options.model_identifier,
            "temperature": options.temperature,
        }
    )
    candidate_contract_digest = payload_digest(
        {
            "motive": candidate_motive.model_dump(mode="json"),
            "base_config": base_config.model_dump(mode="json"),
            "model_identifier": options.model_identifier,
            "temperature": options.temperature,
        }
    )
    with TemporaryDirectory(prefix="memotron-certification-") as directory:
        scratch_dir = Path(directory)
        baseline_samples = tuple(
            [
                await _run_sample(
                    client_factory=client_factory,
                    base_config=base_config,
                    corpus=corpus,
                    scope=scope,
                    motive=baseline_motive,
                    options=options,
                    repetition=index,
                    scratch_dir=scratch_dir,
                )
                for index in range(options.repetitions)
            ]
        )
        candidate_samples = tuple(
            [
                await _run_sample(
                    client_factory=client_factory,
                    base_config=base_config,
                    corpus=corpus,
                    scope=scope,
                    motive=candidate_motive,
                    options=options,
                    repetition=index,
                    scratch_dir=scratch_dir,
                )
                for index in range(options.repetitions)
            ]
        )
    baseline = baseline_samples[0]
    candidate = candidate_samples[0]
    disposition_diffs = _disposition_diff(baseline, candidate)
    graph_diff = _graph_diff(baseline, candidate)
    invariants = tuple(candidate.protected_invariants)
    baseline_flip_rate = _flip_rate(baseline_samples)
    candidate_flip_rate = _flip_rate(candidate_samples)
    replay_flip_rate = max(baseline_flip_rate, candidate_flip_rate)
    stability_score = 1.0 - replay_flip_rate
    decision_total = len(set(baseline.candidate_dispositions) | set(candidate.candidate_dispositions))
    policy_delta = len(disposition_diffs) / decision_total if decision_total else 0.0
    failures: list[str] = []
    if any(not invariant.passed for invariant in invariants):
        failures.append("protected_lifecycle_invariants_failed")
    if stability_score < options.minimum_stability_score:
        failures.append("replay_stability_below_threshold")
    if options.require_material_policy_delta and policy_delta <= replay_flip_rate:
        failures.append("policy_delta_within_replay_noise")
    return StatefulPolicyComparison(
        corpus_name=corpus.name,
        corpus_digest=corpus.corpus_digest,
        scope=scope,
        baseline_motive_name=baseline_motive.name,
        candidate_motive_name=candidate_motive.name,
        baseline_contract_digest=baseline_contract_digest,
        candidate_contract_digest=candidate_contract_digest,
        options=options,
        candidate_disposition_diffs=disposition_diffs,
        end_state_graph_diff=graph_diff,
        protected_invariants=invariants,
        baseline_flip_rate=baseline_flip_rate,
        candidate_flip_rate=candidate_flip_rate,
        replay_flip_rate=replay_flip_rate,
        stability_score=stability_score,
        policy_delta=policy_delta,
        passed=not failures,
        failures=tuple(failures),
        baseline_samples=baseline_samples,
        candidate_samples=candidate_samples,
    )


async def _invoke_benchmark_executor(
    executor: Callable[[BenchmarkAnswerRequest], Any], request: BenchmarkAnswerRequest
) -> str:
    response = executor(request)
    if inspect.isawaitable(response):
        response = await response
    if not isinstance(response, str) or not response.strip():
        raise ValueError("public benchmark answer executor must return a non-blank string")
    return response.strip()


async def _invoke_benchmark_judge(
    judge: Callable[[PublicBenchmarkQuestion, str], Any],
    question: PublicBenchmarkQuestion,
    response: str,
) -> float:
    verdict = judge(question, response)
    if inspect.isawaitable(verdict):
        verdict = await verdict
    if isinstance(verdict, bool):
        return 1.0 if verdict else 0.0
    if not isinstance(verdict, (int, float)) or not math.isfinite(float(verdict)):
        raise ValueError("public benchmark judge must return a boolean or finite score between zero and one")
    score = float(verdict)
    if not 0.0 <= score <= 1.0:
        raise ValueError("public benchmark judge score must be between zero and one")
    return score


async def _run_public_benchmark_sample(
    *,
    client_factory: Callable[[DreamConfig, Path], Any],
    base_config: DreamConfig,
    suite: PublicBenchmarkSuite,
    scope: MemoryScope,
    motive: Motive,
    options: StatefulReplayOptions,
    answer_executor: Callable[[BenchmarkAnswerRequest], Any],
    answer_judge: Callable[[PublicBenchmarkQuestion, str], Any],
    repetition: int,
    scratch_dir: Path,
) -> PublicBenchmarkSample:
    """Replay every independent source scenario before calling the answer runtime."""
    config = _config_for_motive(base_config, motive)
    scores: list[PublicBenchmarkCaseScore] = []
    for scenario_index, scenario in enumerate(suite.scenarios):
        scenario_scope = MemoryScope(
            kind=scope.kind,
            scope_id=f"{scope.scope_id}-benchmark-{scenario_index:05d}",
        )
        client = client_factory(
            config,
            scratch_dir / f"public-{motive.name}-{repetition}-{scenario_index}.sqlite",
        )
        try:
            run_uuids = await _replay_corpus_lifecycle(
                client=client,
                config=config,
                corpus=scenario.corpus,
                scope=scenario_scope,
                motive=motive,
            )
            invariants = _lifecycle_invariants(
                client,
                scope=scenario_scope,
                motive=motive,
                config=config,
                run_uuids=run_uuids,
            )
            for question in scenario.questions:
                profile = await client.profile(
                    scope=scenario_scope,
                    as_of=question.asked_at,
                    motive=motive,
                )
                relationship_uuids = tuple(
                    sorted({item.relationship_uuid for item in (*profile.static_facts, *profile.dynamic_facts)})
                )
                request = BenchmarkAnswerRequest(
                    source=suite.source,
                    suite_digest=suite.suite_digest,
                    scenario_id=scenario.scenario_id,
                    scenario_digest=scenario.scenario_digest,
                    question_id=question.question_id,
                    category=question.category,
                    question=question.question,
                    asked_at=question.asked_at,
                    motive_name=motive.name,
                    model_identifier=options.model_identifier,
                    profile_context=profile.rendered_context,
                    profile_relationship_uuids=relationship_uuids,
                )
                response = await _invoke_benchmark_executor(answer_executor, request)
                score = await _invoke_benchmark_judge(answer_judge, question, response)
                scores.append(
                    PublicBenchmarkCaseScore(
                        scenario_id=scenario.scenario_id,
                        question_id=question.question_id,
                        category=question.category,
                        score=score,
                        response_digest=payload_digest({"response": response}),
                        profile_relationship_uuids=relationship_uuids,
                        lifecycle_invariants=invariants,
                    )
                )
        finally:
            client.graph.close()
    if not scores:
        raise RuntimeError("public benchmark replay produced no scored questions")
    return PublicBenchmarkSample(
        repetition=repetition,
        scores=tuple(scores),
        mean_score=sum(item.score for item in scores) / len(scores),
    )


async def run_public_benchmark(
    *,
    client_factory: Callable[[DreamConfig, Path], Any],
    base_config: DreamConfig,
    suite: PublicBenchmarkSuite,
    scope: MemoryScope,
    motive: Motive,
    options: StatefulReplayOptions,
    answer_executor: Callable[[BenchmarkAnswerRequest], Any],
    answer_judge: Callable[[PublicBenchmarkQuestion, str], Any],
    answer_runtime_identifier: str,
    judge_identifier: str,
) -> PublicBenchmarkReport:
    """Score one Motive against source-provided public benchmark questions.

    The executor receives only the formed profile and question.  The judge is
    the only callable that receives expected answers, so benchmark gold cannot
    enter model context.  One isolated SQLite graph is created per scenario and
    repetition; no production graph can be reached through this API.
    """
    if not callable(client_factory):
        raise ValueError("client_factory must be callable")
    if not callable(answer_executor):
        raise ValueError("public benchmark answer_executor must be callable")
    if not callable(answer_judge):
        raise ValueError("public benchmark answer_judge must be callable")
    runtime_id = answer_runtime_identifier.strip()
    judge_id = judge_identifier.strip()
    if not runtime_id or not judge_id:
        raise ValueError("public benchmark runtime and judge identifiers cannot be blank")
    with TemporaryDirectory(prefix="memotron-public-benchmark-") as directory:
        scratch_dir = Path(directory)
        samples = tuple(
            [
                await _run_public_benchmark_sample(
                    client_factory=client_factory,
                    base_config=base_config,
                    suite=suite,
                    scope=scope,
                    motive=motive,
                    options=options,
                    answer_executor=answer_executor,
                    answer_judge=answer_judge,
                    repetition=repetition,
                    scratch_dir=scratch_dir,
                )
                for repetition in range(options.repetitions)
            ]
        )
    sample_means = [item.mean_score for item in samples]
    mean_score = sum(sample_means) / len(sample_means)
    variance = sum((value - mean_score) ** 2 for value in sample_means) / len(sample_means)
    standard_deviation = math.sqrt(variance)
    # A score can range from zero to one, therefore its population standard
    # deviation is at most one half.  This maps exact replay agreement to one
    # and the maximally unstable split to zero for the existing stability gate.
    stability_score = 1.0 - min(1.0, 2.0 * standard_deviation)
    lifecycle_invariants_passed = all(
        invariant.passed for sample in samples for score in sample.scores for invariant in score.lifecycle_invariants
    )
    failures: list[str] = []
    if not lifecycle_invariants_passed:
        failures.append("protected_lifecycle_invariants_failed")
    if stability_score < options.minimum_stability_score:
        failures.append("benchmark_score_stability_below_threshold")
    # T1-29: `mean_score` was computed, reported, and never compared to anything, so this
    # function returned `passed=True` for a run that answered NOTHING correctly -- in the
    # module that underwrites quality claims about the product.
    #
    # Stability could not have caught it and must not be asked to: it is
    # `1.0 - min(1.0, 2 * stddev)` over the per-repetition means, so a suite scoring 0.0
    # every time has stddev 0 and a PERFECT 1.0 stability score, while a genuinely capable
    # but variable run (means 0.9/0.4/0.8 -> stability 0.568) fails. Consistent total
    # failure was the best-scoring input to the only numeric gate there was.
    #
    # Two checks, because they answer different questions. The zero check is unconditional
    # and cannot be configured away -- "answered nothing correctly" is not a policy
    # position. The threshold above it is the tunable quality bar, and defaults to 0.0 so
    # that no invented constant is smuggled in here; see the field comment.
    if mean_score <= 0.0:
        failures.append("benchmark_mean_score_is_zero")
    elif mean_score < options.minimum_mean_score:
        failures.append("benchmark_mean_score_below_threshold")
    return PublicBenchmarkReport(
        suite_name=suite.name,
        suite_source=suite.source,
        suite_digest=suite.suite_digest,
        motive_name=motive.name,
        options=options,
        answer_runtime_identifier=runtime_id,
        judge_identifier=judge_id,
        samples=samples,
        mean_score=mean_score,
        score_standard_deviation=standard_deviation,
        stability_score=stability_score,
        lifecycle_invariants_passed=lifecycle_invariants_passed,
        passed=not failures,
        failures=tuple(failures),
    )


__all__ = [
    "BenchmarkAnswerRequest",
    "CandidateDispositionDiff",
    "CertificationCorpus",
    "CertificationUseOutcome",
    "EndStateGraphDiff",
    "LifecycleInvariantResult",
    "LoCoMoCorpusAdapter",
    "LocalOfficialBenchmarkJudge",
    "LongMemEvalCorpusAdapter",
    "LongMemEvalOfficialJudge",
    "MemoryAgentBenchCorpusAdapter",
    "OpenAICompatibleBenchmarkRuntime",
    "PolicyAliasState",
    "PolicyContractVersion",
    "PolicyShadowStage",
    "ProfileTopEvidenceExecutor",
    "PublicBenchmarkCaseScore",
    "PublicBenchmarkQuestion",
    "PublicBenchmarkReport",
    "PublicBenchmarkSample",
    "PublicBenchmarkScenario",
    "PublicBenchmarkSuite",
    "StatefulPolicyComparison",
    "StatefulReplayOptions",
    "StatefulReplaySample",
    "builtin_motive_fixture_corpora",
    "certification_config",
    "run_public_benchmark",
    "run_stateful_policy_comparison",
]
