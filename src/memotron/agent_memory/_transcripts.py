"""Mining a finished session for what was actually used, and judging how it went.

Four methods over the session AFTER it ends: which memories were cited, which
runbook commands ran, and an LLM judge scoring the outcomes. This is where the
feedback signal comes from -- `judge_session_outcomes` is what eventually moves a
memory's utility, so a silent failure here degrades retention quality weeks later
with nothing pointing back to it."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import TYPE_CHECKING, Any

from memotron.agent_memory._common import _normalize_non_blank
from memotron.agent_memory._protocol import ComposedAgentMemoryPlatform
from memotron.agent_memory._results import CitationScanResult, OutcomeJudgeResult, RunbookCaptureResult
from memotron.crypto import SHREDDED_CONTENT_PLACEHOLDER, is_sealed_content
from memotron.dreaming import content_tokens
from memotron.models import (
    EpisodeType,
    MemoryScope,
    OutcomeVerdict,
    UseEvent,
    UseEventKind,
)
from memotron.synthesis import SynthesisTransport, strip_markdown_fences
from memotron.transcripts import (
    DEFAULT_RUNBOOK_MIN_REPEATS,
    TranscriptTurn,
    assistant_text_corpus,
    derive_checkpoint,
    repeated_commands,
)

SESSION_JUDGE_SYSTEM_PROMPT = (
    "You judge whether cited memories helped this session's outcome. Output "
    'STRICT JSON {"verdicts": [{"use_id": string, "verdict": "positive"|'
    '"negative"|"inconclusive", "reason": string}]}. Judge ONLY the offered '
    'use_ids. A reason must be at most 12 words. Prefer "inconclusive" '
    "unless the transcript shows clear evidence the memory helped or hurt; "
    "never infer success from silence."
)
SESSION_JUDGE_VERSION = "v1"
_CITATION_MIN_CONTENT_TOKENS = 3
_SESSION_JUDGE_MAX_ASSISTANT_TURNS = 6
_SESSION_JUDGE_MAX_CITED = 6

if TYPE_CHECKING:
    _Base = ComposedAgentMemoryPlatform
else:
    # Plain object at runtime, so the composed class's MRO is unchanged.
    _Base = object


class AgentMemoryTranscriptMixin(_Base):
    """Composed into :class:`AgentMemoryPlatform`."""

    # Provided by the composing backend.
    client: Any
    tenant_id: Any

    async def record_transcript_citations(
        self,
        *,
        agent_id: str,
        turns: tuple[TranscriptTurn, ...],
        session_id: str,
        task_run_id: str,
    ) -> CitationScanResult:
        """WS-15 T8/T9: deterministic transcript-citation scan at a hook boundary.

        Candidates are the session's INJECTED/RETRIEVED use events (task-run
        ids under the ``claude:{session_id}:`` prefix) across the agent's
        authorized scopes.  A candidate is CITED when its relationship uuid
        appears verbatim in the assistant-authored transcript text (REF lines
        cite uuids), or when ALL of the fact's content tokens (casefolded,
        non-stopword, at least :data:`_CITATION_MIN_CONTENT_TOKENS` of them —
        trivial one-word facts never match) appear in that corpus.  Fact text
        is decrypt-on-read; a crypto-shredded (or purged) row can only be
        cited by uuid.  Each hit records one ``CITED_OR_USED`` use event under
        the idempotency key ``{session_id}:cited:{relationship_uuid}`` — a
        repeated firing finds the stored event and records nothing new.  No
        transcript or no candidates is a no-op, never an error.
        """
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        normalized_session = _normalize_non_blank(session_id, "session_id")
        normalized_task_run = _normalize_non_blank(task_run_id, "task_run_id")
        scopes = self._session_scopes(agent_id)
        corpus = assistant_text_corpus(turns)
        corpus_tokens = content_tokens(corpus)
        candidates = self._session_use_events(
            scopes=scopes,
            session_id=normalized_session,
            kinds={UseEventKind.INJECTED, UseEventKind.RETRIEVED},
        )
        use_event_ids: list[str] = []
        cited_relationship_uuids: list[str] = []
        seen: dict[tuple[str, str], MemoryScope] = {}
        for event_scope, event in candidates:
            seen.setdefault((event_scope.key, event.relationship_uuid), event_scope)
        for (scope_key, relationship_uuid), event_scope in seen.items():
            if not corpus:
                break
            matched_by = ""
            if relationship_uuid in corpus:
                matched_by = "uuid"
            else:
                try:
                    relationship = self.client.graph.get_relationship(relationship_uuid)
                except ValueError:
                    # Hard-purged row (crypto-shred rollup): uuid path only.
                    continue
                fact_text = str(self.client.graph.reveal(scope_key, relationship.properties.get("fact", "")))
                if fact_text == SHREDDED_CONTENT_PLACEHOLDER:
                    continue  # crypto-shredded: uuid path only
                fact_tokens = content_tokens(fact_text)
                if len(fact_tokens) >= _CITATION_MIN_CONTENT_TOKENS and fact_tokens <= corpus_tokens:
                    matched_by = "tokens"
            if not matched_by:
                continue
            idempotency_key = f"{normalized_session}:cited:{relationship_uuid}"
            existing = self.client.graph.use_event_for_idempotency_key(
                scope_key=scope_key, idempotency_key=idempotency_key
            )
            if existing is not None:
                use_event_ids.append(existing.use_id)
                cited_relationship_uuids.append(relationship_uuid)
                continue
            recorded = await self.client.record_memory_use(
                relationship_uuid=relationship_uuid,
                scope=event_scope,
                kind=UseEventKind.CITED_OR_USED,
                task_run_id=normalized_task_run,
                idempotency_key=idempotency_key,
                metadata={
                    "agent_id": agent_id,
                    "session_id": normalized_session,
                    "citation_source": "transcript_scan",
                    "matched_by": matched_by,
                },
            )
            use_event_ids.append(recorded.use_id)
            cited_relationship_uuids.append(relationship_uuid)
        return CitationScanResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            session_id=normalized_session,
            task_run_id=normalized_task_run,
            scanned_events=len(candidates),
            cited_count=len(cited_relationship_uuids),
            use_event_ids=tuple(use_event_ids),
            cited_relationship_uuids=tuple(cited_relationship_uuids),
        )

    async def record_runbook_commands(
        self,
        *,
        agent_id: str,
        turns: tuple[TranscriptTurn, ...],
        task_run_id: str,
        min_repeats: int = DEFAULT_RUNBOOK_MIN_REPEATS,
    ) -> RunbookCaptureResult:
        """WS-28 T3: deterministic runbook capture at a lifecycle boundary.

        A command invoked ``>= min_repeats`` times with a stable (EXACT)
        shape in this transcript window is queued as a ``directive`` fact
        (relationship type ``SHOULD``) carrying the VERBATIM invocation as
        both the fact object and ``metadata["verbatim_command"]`` — never
        paraphrased.  ``metadata["runbook_capture"] = True`` also makes the
        write-side dedup guard treat every candidate for this fact as an
        identifier conflict (``dreaming._find_reinforce_target``'s
        ``force_identifier_conflict``), so a DIFFERENT command can never be
        paraphrase-merged into this one — a runbook line is an identifier by
        policy.  A genuine repeat of the SAME command still reinforces
        normally (exact-object match, pass 1).

        This is deterministic shape-matching only — no semantic selection,
        no LLM, and no per-turn publication: it runs once per lifecycle
        boundary, reusing the SAME already-parsed ``turns`` the citation scan
        shares, exactly like the adoption contract requires.  Queuing is
        idempotent at the episode-observation level in effect (formation
        reinforces an exact-object repeat rather than duplicating it); no
        commands below the threshold produce a no-op empty result.
        """
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        self._authorize(agent_id=agent_id, scope=scope)
        normalized_task_run = _normalize_non_blank(task_run_id, "task_run_id")
        commands = repeated_commands(turns, min_repeats=min_repeats)
        if not commands:
            return RunbookCaptureResult(tenant_id=self.tenant_id, agent_id=agent_id)
        policy = self.client.resolve_policy(tenant_id=self.tenant_id, agent_id=agent_id, scope=scope)
        episode_uuids: list[str] = []
        for command in commands:
            body = json.dumps(
                {
                    "memories": [
                        {
                            "subject": agent_id,
                            "predicate": "runs",
                            "object": command,
                            "relationship_type": "SHOULD",
                            "confidence": 0.9,
                            "source_text": command,
                            "claim_mode": "directive",
                            "metadata": {
                                "runbook_capture": True,
                                "verbatim_command": command,
                            },
                        }
                    ]
                },
                sort_keys=True,
            )
            result = await self.client.add_episode(
                name=f"agent-memory-runbook:{agent_id}:{sha256(command.encode('utf-8')).hexdigest()[:16]}",
                episode_body=body,
                source=EpisodeType.JSON,
                source_description="agent memory runbook capture",
                scope=scope,
                metadata={
                    "agent_memory": True,
                    "agent_memory_event": "runbook",
                    "tenant_id": self.tenant_id,
                    "agent_id": agent_id,
                    "_verified_source_authority": "agent",
                    "task_run_id": normalized_task_run,
                },
                motive=policy.motive_name,
            )
            episode_uuids.append(result.episode_uuid)
        return RunbookCaptureResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            captured_commands=commands,
            episode_uuids=tuple(episode_uuids),
        )

    async def judge_session_outcomes(
        self,
        *,
        agent_id: str,
        turns: tuple[TranscriptTurn, ...],
        session_id: str,
        task_run_id: str,
    ) -> OutcomeJudgeResult:
        """WS-15 T10: judge the session's cited memories with the tenant LLM.

        The judge is the client's :class:`SynthesisTransport` (one mechanism
        for every platform-internal LLM call — same sealed-credential/env
        construction path, same identifier scheme; the judge differs from
        rollup synthesis only in its system prompt and identity, not its wire
        protocol).  No transport configured returns an explicit
        ``no_judge_configured`` result with zero events — never a stub
        verdict.  The judge reads the session-end checkpoint, the last
        assistant turns, and the cited facts, and must answer STRICT JSON
        verdicts over exactly the offered use_ids; ``inconclusive`` (recorded
        as :attr:`OutcomeVerdict.UNKNOWN`, the platform's explicit
        inconclusive verdict) is preferred absent clear transcript evidence —
        success is never inferred from silence.  An out-of-contract response
        (malformed JSON, unknown use_id, invalid verdict) is receipted
        (``SESSION_OUTCOME_JUDGE_REJECTED``) and raised as ``ValueError`` with
        zero events recorded.  Verdicts are recorded via
        ``record_memory_outcome`` with ``judge_identity=
        "session-judge:{transport.identifier}"``, ``judge_version="v1"``, and
        idempotency key ``{session_id}:outcome:{use_id}`` — already-judged
        use_ids are skipped, so repeated firings never duplicate outcomes.
        """
        scope = self.agent_scope(agent_id)
        agent_id = scope.scope_id
        normalized_session = _normalize_non_blank(session_id, "session_id")
        normalized_task_run = _normalize_non_blank(task_run_id, "task_run_id")
        transport: SynthesisTransport | None = self.client.rollup_synthesis_transport
        if transport is None:
            return OutcomeJudgeResult(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                session_id=normalized_session,
                task_run_id=normalized_task_run,
                judge_configured=False,
                skipped_reason="no_judge_configured",
            )
        judge_identity = f"session-judge:{transport.identifier}"
        scopes = self._session_scopes(agent_id)
        cited = self._session_use_events(
            scopes=scopes,
            session_id=normalized_session,
            kinds={UseEventKind.CITED_OR_USED},
        )
        # Deterministic bound: newest cited rows first, capped so verdicts fit
        # the transport's modest output budget; drop rows already judged under
        # this session's idempotency keys.
        pending: list[tuple[MemoryScope, UseEvent]] = []
        for event_scope, event in sorted(cited, key=lambda item: (item[1].used_at, item[1].use_id), reverse=True):
            if (
                self.client.graph.outcome_event_for_idempotency_key(
                    scope_key=event_scope.key,
                    idempotency_key=f"{normalized_session}:outcome:{event.use_id}",
                )
                is None
            ):
                pending.append((event_scope, event))
            if len(pending) >= _SESSION_JUDGE_MAX_CITED:
                break
        if not pending:
            return OutcomeJudgeResult(
                tenant_id=self.tenant_id,
                agent_id=agent_id,
                session_id=normalized_session,
                task_run_id=normalized_task_run,
                judge_configured=True,
                judge_identity=judge_identity,
                skipped_reason="no_cited_memories" if not cited else "already_judged",
            )
        prompt = self._render_session_judge_prompt(
            turns=turns,
            session_id=normalized_session,
            pending=pending,
        )
        raw_response = await transport.synthesize(prompt, system_prompt=SESSION_JUDGE_SYSTEM_PROMPT)
        pending_by_use_id = {event.use_id: (event_scope, event) for event_scope, event in pending}

        async def _reject(reason: str) -> None:
            await self.client.record_outcome_judge_rejection(
                scope=scope,
                session_id=normalized_session,
                task_run_id=normalized_task_run,
                reason=reason,
                judge_identifier=judge_identity,
                cited_use_ids=tuple(pending_by_use_id),
            )
            raise ValueError(f"session outcome judge response rejected: {reason}")

        try:
            parsed = json.loads(strip_markdown_fences(raw_response))
        except json.JSONDecodeError:
            await _reject("verdicts_json_parse_failed")
        if not isinstance(parsed, dict) or not isinstance(parsed.get("verdicts"), list):
            await _reject("verdicts_json_shape_invalid")
        verdict_map = {
            "positive": OutcomeVerdict.POSITIVE,
            "negative": OutcomeVerdict.NEGATIVE,
            "inconclusive": OutcomeVerdict.UNKNOWN,
        }
        validated: list[tuple[MemoryScope, UseEvent, OutcomeVerdict, str]] = []
        for entry in parsed["verdicts"]:
            if not isinstance(entry, dict):
                await _reject("verdict_entry_not_object")
            use_id = str(entry.get("use_id", ""))
            if use_id not in pending_by_use_id:
                await _reject(f"unknown_use_id:{use_id[:80]}")
            raw_verdict = str(entry.get("verdict", ""))
            if raw_verdict not in verdict_map:
                await _reject(f"invalid_verdict:{raw_verdict[:40]}")
            event_scope, event = pending_by_use_id[use_id]
            validated.append((event_scope, event, verdict_map[raw_verdict], str(entry.get("reason", "")).strip()))
        outcome_event_ids: list[str] = []
        for event_scope, event, verdict, reason in validated:
            outcome = await self.client.record_memory_outcome(
                use_id=event.use_id,
                scope=event_scope,
                verdict=verdict,
                # The outcome must bind to the use event's own task run — the
                # store enforces exact task_run_id equality with the use event.
                task_run_id=event.task_run_id,
                idempotency_key=f"{normalized_session}:outcome:{event.use_id}",
                judge_identity=judge_identity,
                judge_version=SESSION_JUDGE_VERSION,
                metadata={
                    "agent_id": agent_id,
                    "session_id": normalized_session,
                    "hook_task_run_id": normalized_task_run,
                    "judge_reason": reason[:280],
                },
            )
            outcome_event_ids.append(outcome.outcome_id)
        return OutcomeJudgeResult(
            tenant_id=self.tenant_id,
            agent_id=agent_id,
            session_id=normalized_session,
            task_run_id=normalized_task_run,
            judge_configured=True,
            judge_identity=judge_identity,
            judged_count=len(outcome_event_ids),
            outcome_event_ids=tuple(outcome_event_ids),
        )

    def _render_session_judge_prompt(
        self,
        *,
        turns: tuple[TranscriptTurn, ...],
        session_id: str,
        pending: list[tuple[MemoryScope, UseEvent]],
    ) -> str:
        """Deterministic judge prompt: checkpoint + assistant tail + cited facts.

        Fact text is decrypt-on-read for plain scopes; sealed (crypto-shred
        governed) content never reaches the LLM — such rows are offered by
        uuid only.
        """
        checkpoint = derive_checkpoint(turns, event="session-end", session_id=session_id)
        assistant_tail = [turn.text for turn in turns if turn.role == "assistant" and turn.text][
            -_SESSION_JUDGE_MAX_ASSISTANT_TURNS:
        ]
        lines = ["Session checkpoint:", checkpoint, "", "Recent assistant turns:"]
        lines.extend(f"- {text[:400]}" for text in assistant_tail)
        lines.extend(["", "Cited memories (use_id | fact):"])
        for _event_scope, event in pending:
            fact_text = ""
            try:
                relationship = self.client.graph.get_relationship(event.relationship_uuid)
            except ValueError:
                relationship = None
            if relationship is not None:
                raw_fact = relationship.properties.get("fact", "")
                if not is_sealed_content(raw_fact):
                    fact_text = str(raw_fact)
            lines.append(f"- {event.use_id} | {fact_text[:300] if fact_text else '[content unavailable]'}")
        return "\n".join(lines)
