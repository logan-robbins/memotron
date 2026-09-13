"""``get_context()``: an INCREMENTAL, LLM-maintained orientation artifact.

Problem this replaces
----------------------
``Memotron.profile()`` (client.py) assembles its ``rendered_context`` FROM
SCRATCH on every call: rank every context-visible fact by confidence, cut to
``max_static_facts`` / ``max_dynamic_facts``, render.  Two measured failure
modes follow directly from "from scratch every call":

1. Confidence ranks whether a fact is TRUE, not whether it is WORTH CARRYING.
   A corpus produced 17 true-and-useless facts ("Jedai Gateway
   supports/provides/handles/enables ...") that filled the profile because
   they were all confident and nothing scored them on usefulness.
2. The rendered context reshuffles on every call (ranking is a function of
   the *whole* current fact set), so it cannot serve resume-after-compaction,
   where the entire point is that the agent's orientation brief is the same
   words it read five minutes ago, plus whatever changed.

``get_context()`` is the fix: a versioned artifact, persisted per scope, that
an LLM edits INCREMENTALLY from a delta rather than a full graph re-read.

Algorithm
---------
::

    caller (profile()) --> facts: list[MemoryProfileFact]  (already visibility-
                            and confidence-filtered; this module never reads
                            the graph for facts, only for artifact storage)
                                |
                                v
    1. current_marks   = fingerprint every fact by (text, confidence~2dp,
                          observed_count, pinned) + tag its ContextRole
    2. watermark        = payload_digest(uuid -> fingerprint)   [receipts.py]
    3. stored           = last artifact for scope.key (dream_decisions table)
    4. stored.watermark == watermark?  --> return stored VERBATIM.  ZERO calls.
    5. delta            = current_marks - stored.fact_marks (new / changed / removed)
    6. prompt           = previous sections (JSON) + delta (recency-capped for
                          the volatile "recent" role only)
    7. transport.synthesize(...)  -->  {"standing": [...], "recent": [...],
                                        "structure": [...]}
       transport error / malformed JSON --> return `stored` unchanged (its
       watermark stays stale, so the NEXT call retries the LLM); if there is
       no `stored` yet, fall back to a deterministic, un-persisted rendering
       of the raw facts (`degraded=True`) rather than raising.
    8. _enforce_role_budgets(...)  -->  hard per-role token caps (see below)
    9. persist new ContextArtifact version; return it.

No query, no relevance rank to seed selection from — so *recency* seeds
"recent" (role RECENT sorted by `last_seen_at or valid_from`, capped by
`ContextPolicy.recent_seed_limit`); "standing" and "structure" deltas are
never recency-capped because they are rare and always worth telling the LLM
about (this mirrors, at the fact-selection layer, the same idea as
`Memotron._expand_retrieval_candidates`'s recency/relevance-ranked beam
frontier in client.py — but operates over the already-bounded context-visible
tier `profile()` computes, not a fresh multi-hop graph walk; see the
ownership note at the bottom of this docstring).

Storage: no schema change
--------------------------
This module owns no SQL schema.  A maintained artifact is one MORE
"decision" in the graph's existing, generic, append-only decision log
(`StorageBackend.dream_decisions` / `.record_dream_decision`,
`graph.py`), keyed by `job_name=f"context-artifact:{scope.key}"` so
`dream_decisions(job_name=..., limit=1)` seeks the existing
`dream_decisions_job_name_idx` index straight to the latest version. The
whole `ContextArtifact` (including the per-uuid fingerprint map needed for
the NEXT delta) round-trips through `DreamDecisionRecord.details: dict[str,
Any]`, which is exactly the free-form JSON escape hatch that field already
is.  `job_kind` is stamped `DreamJobKind.CONTEXT_MAINTENANCE` — its own
member, not reused from FORMATION/CONSOLIDATION/PRUNING/COHERENCE, so a
maintained-context update is never mixed into `client.dream_decisions()` /
the admin "recent decisions" feed under the same kind as a real
consolidation decision.  (An earlier version of this module reused
`DreamJobKind.CONSOLIDATION` here because `models.py` was owned by another
agent at the time; `models.py` is this module's own file now, so that
workaround is gone.)  Still distinguishable, belt-and-suspenders, by
`job_name` starting with `"context-artifact:"` and by
`decision_type == "CONTEXT_ARTIFACT_UPDATED"`.

Budget: reserve by ROLE, not by memory TYPE
--------------------------------------------
`ProfilePolicy.max_type_budget_share` (config.py) already tells this story:
a per-TYPE floor is exactly the mechanism that let 148 misclassified facts
sitting under one label crowd out everything actionable, because the floor
attached to a fine-grained, extraction-assigned, error-prone label. This
module reserves budget by a SMALL FIXED set of THREE roles instead —
STANDING / RECENT / STRUCTURE (see `ContextRole`) — each a hard, independent
token slice of the artifact (`ContextPolicy.role_budget_shares`, default
50/30/20, enforced in code by `_enforce_role_budgets`, never by asking the
LLM nicely). A flood of new `state` facts can only ever compete for the
RECENT slice; it structurally cannot evict a `directive` sitting in
STANDING, because they are never drawn from the same pool. This is the
"bounded by construction" property: with three roles fixed by this module
(not configurable per-label, not growable by a classification bug), the
worst a misclassified fact can do is take a seat in the wrong ALREADY-FIXED
room, never manufacture a new guaranteed room for itself. Quality control
(dropping true-but-useless facts) is a SEPARATE job, delegated to the LLM's
edit under the "what step does this let the agent skip?" instruction in
`_SYSTEM_PROMPT` — the role budget is a quantity backstop, not a quality
filter.

Zero-call cost
--------------
The no-change path is: one `dream_decisions(job_name=..., limit=1)` SELECT
(existing index seek) + one `payload_digest` over the caller-supplied fact
list already in memory. No LLM call, no write. This is the path exercised
on every `profile()` call where nothing in the scope's context-visible tier
changed since the artifact was last built.

Ownership note (read before extending)
---------------------------------------
This module is intentionally self-contained: it takes `facts:
list[MemoryProfileFact]` (from `models.py`, read-only) and a
`StorageBackend` (read-only — only its existing public
`dream_decisions` / `record_dream_decision` methods are called) as inputs,
and defines every new type it needs (`ContextRole`, `ContextPolicy`,
`ContextArtifact`, `ContextFactMark`, `ContextTransport`) itself rather than
adding fields to `models.py` or `config.py`. It does NOT implement the
multi-hop beam-search graph expansion `client.py` (~3820,
`_expand_retrieval_candidates` / `_retrieval_seed_nodes`) uses for
QUERY-conditioned retrieval: those are private methods of `Memotron`
outside this module's file ownership, and are tuned for lexical/vector
relevance scoring a query-less orientation brief has no query to rank
against. Recency-seeding here instead ranks within the already-bounded
context-visible tier `profile()` hands in. A future pass that also wants
graph-hop expansion for the maintained artifact belongs in `client.py`
itself (the file that owns those private methods), not here.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field

from memotron.extraction import parse_first_json_object, strip_markdown_fences
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
    OpenAICompatibleChatTransport,
)
from memotron.models import (
    DreamDecisionRecord,
    DreamJobKind,
    MemoryProfileFact,
    MemoryScope,
    RelationshipStatus,
)
from memotron.receipts import payload_digest
from memotron.retrieval import use_need as _use_need
from memotron.storage import StorageBackend

# ---------------------------------------------------------------------------
# Roles: the budget partition (see module docstring, "Budget: reserve by ROLE")
# ---------------------------------------------------------------------------


class ContextRole(StrEnum):
    """The three behavioral buckets a maintained context artifact reserves
    token budget for, independently of the memory-type taxonomy in
    `models.py`. Fixed at three by design — see module docstring."""

    STANDING = "standing"
    """Durable directives, preferences, requirements, identity/anchors — how
    to behave here. Never recency-limited: rare, and always worth surfacing."""
    RECENT = "recent"
    """Short-lived state — what was recently being worked on. The ONLY role
    that is recency-capped going into the LLM prompt (see
    `ContextPolicy.recent_seed_limit`), because it is the only role a busy
    session can flood."""
    STRUCTURE = "structure"
    """Project/component shape, critical commands, routing facts ("X lives
    at Y, go straight there"; "you are already authenticated to Z")."""


_ROLE_BY_MEMORY_TYPE: dict[str, ContextRole] = {
    "directive": ContextRole.STANDING,
    "preference": ContextRole.STANDING,
    "requirement": ContextRole.STANDING,
    "anchor": ContextRole.STANDING,
    "state": ContextRole.RECENT,
    "decision": ContextRole.STRUCTURE,
    "incident": ContextRole.STRUCTURE,
    "theme": ContextRole.STRUCTURE,
    "rollup": ContextRole.STRUCTURE,
}


def role_for_memory_type(memory_type: str | None) -> ContextRole:
    """Map a `MemoryType` value to its budget role. Unknown/None -> STRUCTURE
    (a routing/reference fact is the safer default than silently treating an
    unclassified fact as a standing directive)."""
    return _ROLE_BY_MEMORY_TYPE.get((memory_type or "").strip().lower(), ContextRole.STRUCTURE)


def _estimate_tokens(text: str) -> int:
    """Deterministic ~4-chars-per-token estimator, ceiling division.

    Intentionally NOT imported from `client.py` (`Memotron._estimate_tokens`
    is a private method of a class this module does not own); this is the
    same one-line formula, duplicated the same way `extraction.py` and
    `synthesis.py` each carry their own `strip_markdown_fences` rather than
    share one across the transport-adjacent modules.
    """
    return (len(text) + 3) // 4


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextPolicy:
    """Tunables for one scope's maintained context artifact.

    Lives here, not in `config.py`'s `ProfilePolicy`, per file ownership: a
    new policy for a module this task owns outright stays out of the
    shared config schema another agent is actively restructuring.
    """

    token_budget: int = 2000
    """Approximate token ceiling for the rendered artifact (4-chars/token
    estimator, matching every other budget in this codebase)."""

    role_budget_shares: dict[str, float] = field(
        default_factory=lambda: {
            ContextRole.STANDING.value: 0.5,
            ContextRole.RECENT.value: 0.3,
            ContextRole.STRUCTURE.value: 0.2,
        }
    )
    """Fixed, independent per-role fraction of `token_budget`. Deliberately
    NOT redistributed when a role is empty (unlike the legacy per-type CAP in
    client.py, which redistributes unused headroom) — the whole point is that
    each role's budget is a hard constant, not a function of what the other
    roles happen to contain this call."""

    recent_seed_limit: int = 40
    """Cap on how many RECENT-role new/changed facts are described in one
    delta prompt, most-recent-first. STANDING/STRUCTURE deltas are never
    capped. This is the "recency seeds it" mechanism: with no query, recency
    is what decides which of a flood of new state facts the LLM even hears
    about."""

    confidence_precision: int = 2
    """Round confidence to this many decimals before fingerprinting a fact.
    WS-16 bounded-accumulation reinforcement nudges confidence on every
    corroborating observation; without rounding, a fact would count as
    "changed" on every microscopic confidence tick and defeat the zero-call
    no-change path almost permanently."""

    temporal_qualifiers_enabled: bool = True
    """WS-27 T5: informative-only "as of ..." qualifiers on fact lines this
    module builds itself (the deterministic fallback render, and the delta
    labels shown to the LLM).  A qualifier is added ONLY when it changes the
    reading — a superseded/non-active status, an explicit ``valid_to``, or a
    ``valid_from`` within ``recency_window_seconds`` — never on every line
    (see :func:`_temporal_qualifier`). ``True`` by default: this is a
    presentation-layer addition that only ever APPENDS to a fact's own text,
    so it cannot regress an existing exact-match caller looking for that text
    as a substring."""

    recency_window_seconds: float = 14 * 24 * 3600.0
    """WS-27 T5: a fact whose ``valid_from`` falls within this many seconds
    of "now" is recent enough that an agent reading it should know it is
    freshly established, so it is qualified even though it is not
    superseded and carries no explicit ``valid_to``. Default 14 days."""

    def __post_init__(self) -> None:
        if self.token_budget <= 0:
            raise ValueError("token_budget must be greater than zero")
        if self.recent_seed_limit <= 0:
            raise ValueError("recent_seed_limit must be greater than zero")
        if self.confidence_precision < 0:
            raise ValueError("confidence_precision cannot be negative")
        if self.recency_window_seconds < 0:
            raise ValueError("recency_window_seconds cannot be negative")
        required = {role.value for role in ContextRole}
        provided = set(self.role_budget_shares)
        if provided != required:
            raise ValueError(
                f"role_budget_shares must have exactly the keys {sorted(required)}, got {sorted(provided)}"
            )
        for role, share in self.role_budget_shares.items():
            if share < 0:
                raise ValueError(f"role_budget_shares[{role!r}] cannot be negative")
        total = sum(self.role_budget_shares.values())
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"role_budget_shares must sum to 1.0, got {total}")

    def responsive_to(
        self,
        injected_waste_rate_by_role: dict[str, float],
        *,
        damping: float = 0.5,
        min_share: float = 0.05,
    ) -> ContextPolicy:
        """WS-28 T4: an OPT-IN copy of this policy whose ``role_budget_shares``
        respond to ``memory_evolution()``'s measured
        ``injected_waste_rate_by_role``.

        Nothing in :func:`get_context` calls this automatically — a caller
        (an operator, a scheduled job) decides whether and when to build and
        adopt the returned policy for a LATER call.  See
        :func:`responsive_role_budget_shares` for the adjustment itself."""
        return replace(
            self,
            role_budget_shares=responsive_role_budget_shares(
                self.role_budget_shares,
                injected_waste_rate_by_role=injected_waste_rate_by_role,
                damping=damping,
                min_share=min_share,
            ),
        )


def responsive_role_budget_shares(
    shares: dict[str, float],
    *,
    injected_waste_rate_by_role: dict[str, float],
    damping: float = 0.5,
    min_share: float = 0.05,
) -> dict[str, float]:
    """WS-28 T4: an opt-in adjustment of ``role_budget_shares`` in response to
    ``memory_evolution()``'s measured ``injected_waste_rate_by_role`` (the
    token-weighted injected-but-never-cited share per :class:`ContextRole`).

    A role with a HIGHER measured waste rate is shrunk toward *min_share*
    (the fraction of the way there is ``damping * waste_rate``); the
    reclaimed share is redistributed to the roles with NO shrinkage (an
    unmeasured role, or a measured rate of ``0.0``), in proportion to their
    own current share, so the result still sums to exactly 1.0 — every other
    role budget invariant this module enforces (see
    ``ContextPolicy.__post_init__``) still holds on the output. ``{}`` (no
    measurement for any role) returns *shares* unchanged.

    Pure and deterministic: the same ``(shares, rates)`` always produce the
    same result.  This function only ever COMPUTES a candidate share map —
    it never mutates a live policy or writes anything; the caller decides
    whether and when to adopt it (see ``ContextPolicy.responsive_to``).
    """
    if not injected_waste_rate_by_role:
        return dict(shares)
    if not (0.0 <= damping <= 1.0):
        raise ValueError("damping must be within [0.0, 1.0]")
    if min_share < 0.0:
        raise ValueError("min_share must be non-negative")
    adjusted: dict[str, float] = {}
    shrunk: dict[str, bool] = {}
    reclaimed = 0.0
    for role, share in shares.items():
        rate = max(0.0, min(1.0, injected_waste_rate_by_role.get(role, 0.0)))
        if rate <= 0.0 or share <= min_share:
            adjusted[role] = share
            shrunk[role] = False
            continue
        reduction = damping * rate * (share - min_share)
        adjusted[role] = share - reduction
        shrunk[role] = True
        reclaimed += reduction
    if reclaimed <= 0.0:
        return adjusted
    receiving_roles = [role for role, was_shrunk in shrunk.items() if not was_shrunk]
    receiving_total = sum(shares[role] for role in receiving_roles)
    if not receiving_roles or receiving_total <= 0.0:
        # Every role shrank (all measured and all rated positively) -- the
        # only way to keep the total at 1.0 is to give the reclaimed share
        # back evenly rather than to no one.
        bonus = reclaimed / len(adjusted)
        return {role: value + bonus for role, value in adjusted.items()}
    for role in receiving_roles:
        adjusted[role] += reclaimed * shares[role] / receiving_total
    return adjusted


# ---------------------------------------------------------------------------
# WS-27 T5: temporal-qualifier legibility (informative-only)
# ---------------------------------------------------------------------------
#
# An as-of qualifier belongs on a fact line ONLY when it changes the
# reading — a stable current fact renders bare, because a qualifier on
# every line is token spend that fights the very budget this module
# enforces (WS-28's pollution accounting).  Three cases change the reading:
#   1. the fact's status is no longer ACTIVE (superseded/archived/etc.) —
#      always marked, however old, whenever such a fact is shown at all
#      (e.g. a historical/evidence view; the live artifact normally excludes
#      these via `profile()`'s visibility filter before they ever reach here).
#   2. an explicit `valid_to` is set — a bounded/temporary fact, whose
#      expiry is itself part of the fact.
#   3. `valid_from` falls within the configured recency window — an agent
#      benefits from knowing a fact was JUST established, distinct from one
#      that has held for months.
# Everything else renders bare.


def _temporal_qualifier(fact: MemoryProfileFact, *, now: datetime, recency_window_seconds: float) -> str | None:
    """The as-of qualifier for one fact, or ``None`` for a bare line.

    Pure and deterministic — never asks an LLM to decide currency, so it is
    a hard guarantee wherever it is applied, not a hint the model may drop.
    """
    if fact.status != RelationshipStatus.ACTIVE:
        if fact.valid_to is not None:
            return f"superseded {fact.valid_to.date().isoformat()}"
        return f"superseded ({fact.status.value})"
    if fact.valid_to is not None:
        return f"valid until {fact.valid_to.date().isoformat()}"
    if fact.valid_from is not None:
        age_seconds = (now - fact.valid_from).total_seconds()
        if 0 <= age_seconds <= recency_window_seconds:
            return f"as of {fact.valid_from.date().isoformat()}"
    return None


def _qualified_fact_text(
    fact: MemoryProfileFact,
    *,
    now: datetime,
    recency_window_seconds: float,
    enabled: bool = True,
) -> str:
    """``fact.fact``, with an informative-only qualifier APPENDED (never
    interleaved or prepended) so the original fact text always survives as a
    contiguous substring of the result."""
    if not enabled:
        return fact.fact
    qualifier = _temporal_qualifier(fact, now=now, recency_window_seconds=recency_window_seconds)
    return fact.fact if qualifier is None else f"{fact.fact} ({qualifier})"


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


class ContextFactMark(BaseModel):
    """One fact's fingerprint as folded into an artifact version — enough to
    detect NEW / CHANGED / REMOVED on the next call without re-reading the
    graph, and enough to write a one-line delta description into the prompt
    without needing to re-fetch removed facts (which are, by definition, no
    longer in the caller's `facts` list)."""

    model_config = {"frozen": True}

    fingerprint: str
    role: str
    label: str
    """First 150 chars of the fact text — long enough for the LLM to act on,
    short enough that a flood of facts does not blow up the delta prompt."""


_LABEL_MAX_CHARS = 150


class ContextArtifact(BaseModel):
    """The maintained, versioned orientation brief for one scope.

    Round-trips through `DreamDecisionRecord.details` verbatim
    (`model_dump(mode="json")` / `model_validate(...)`) — see the module
    docstring's "Storage: no schema change" section.
    """

    model_config = {"frozen": True}

    scope_key: str
    version: int
    watermark: str
    """`payload_digest` over `{uuid: fingerprint}` for every fact folded into
    this version. Equality with a freshly computed watermark is the ENTIRE
    zero-LLM-call fast path."""
    sections: dict[str, list[str]]
    """Keyed by `ContextRole.value`; always exactly the three role keys."""
    fact_marks: dict[str, ContextFactMark]
    """uuid -> mark, for every fact folded into this version. Diffed against
    the next call's current facts to build the next delta."""
    rendered_text: str
    model_identifier: str
    updated_at: datetime
    degraded: bool = False
    """True only for an ephemeral, UN-PERSISTED fallback artifact returned
    when there is no prior good version AND the transport failed (see
    `get_context`). A `degraded` artifact's `watermark` is always `""`,
    which can never equal a real watermark, so the very next call retries
    the LLM rather than treating the fallback as settled."""
    role_budget_shares: dict[str, float] = Field(default_factory=dict)
    """WS-28 T4: the ``ContextPolicy.role_budget_shares`` actually enforced
    for this version — an ordinary, byte-for-byte audit trail of which
    shares (default, or a caller's ``ContextPolicy.responsive_to(...)``
    adjustment) produced this artifact, via this module's own existing
    versioned-record mechanism.  Defaulted so a pre-WS-28 persisted artifact
    still validates (``{}``)."""


# ---------------------------------------------------------------------------
# Transport (mirrors synthesis.SynthesisTransport's shape; a fresh Protocol
# per module docstring's ownership note, so this module's transport contract
# does not silently move if synthesis.py's does)
# ---------------------------------------------------------------------------


class ContextTransport(Protocol):
    @property
    def identifier(self) -> str:
        """Short stable string naming the provider and model behind this transport."""
        ...

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        """Return the model's raw text for one system+user exchange.

        Transport-level failures (missing key, HTTP error, malformed
        provider envelope) raise `ValueError`. Content-level validation
        (strict JSON, the three-section shape) is `get_context`'s job.
        """
        ...


_SYSTEM_PROMPT = (
    "You maintain a small ORIENTATION BRIEF for an autonomous agent about to resume "
    "work in a memory scope, possibly right after a context-window compaction. You "
    "are given the brief's PREVIOUS state and a DELTA of what changed in the "
    "underlying memory graph since it was last built. Update the brief; do not "
    "re-derive it from scratch, and do not re-describe facts that are unchanged.\n\n"
    "The brief is an orientation aid, never a field inventory or a catalogue. For "
    "every line you keep or add, it must be true that: this line lets the agent SKIP "
    "a step it would otherwise have to take (a lookup, a question, a mistake). Drop "
    "descriptive prose and enumerations of facts that carry no action.\n\n"
    "Every bullet must be traceable to a specific fact in PREVIOUS CONTEXT or the "
    "DELTA below. Never invent a bullet that describes how to write this brief, "
    "comments on these instructions, or gives meta-guidance about brevity or style "
    "-- those instructions govern YOUR editing behavior; they are never content for "
    "the brief itself, no matter how much they resemble a standing convention.\n\n"
    "Organize your answer into three arrays of short, self-contained bullet strings:\n"
    '  "standing": durable directives, preferences, and conventions — how to behave here.\n'
    '  "recent": what was recently being worked on or decided — short-lived state.\n'
    '  "structure": project/component shape and routing facts — where things live, '
    "what commands to run, what the agent is already authenticated to.\n\n"
    "Apply the delta precisely: fold NEW lines in, rewrite CHANGED lines in place, and "
    "delete REMOVED lines plus anything that depended only on them. Keep everything "
    "else byte-for-byte if it is still true.\n\n"
    "If a delta hands you more NEW/CHANGED material for one array than its target size "
    "allows, that is a reason to pick the most useful lines and trim the rest short -- "
    "never a reason to leave the array empty. An array left at zero throws away "
    "everything in it permanently: unchanged facts are never re-described to you on a "
    "later call, so anything you drop now, and that never changes again, disappears "
    "from the brief for good.\n\n"
    "Respond with ONLY a JSON object of the exact shape "
    '{"standing": [...], "recent": [...], "structure": [...]}. No prose, no markdown '
    "fences, no keys other than these three."
)


class OpenAICompatibleContextTransport(OpenAICompatibleChatTransport):
    """The only context-maintenance transport: OpenAI-compatible chat
    completions against the JedAI Gateway.

    Construction, auth, request building and response decoding are
    :class:`~memotron.gateway.OpenAICompatibleChatTransport`'s, shared with the
    other four transports — including the ALWAYS-explicit `max_tokens` (omitting
    it lets the gateway apply its own 4096 default, which truncates
    Claude-family responses to zero-length content on this gateway). What is
    specific to context maintenance is here: `timeout_seconds` defaults to 300,
    not 60, and the error wording is `context maintenance ...`.

    `response_format={"type": "json_object"}` is still sent for
    forward-compatibility / documentation clarity, but the gateway runs
    `drop_params: true` and silently discards it, so the model may answer
    with fenced JSON regardless — `get_context` parses with
    `extraction.strip_markdown_fences` + `extraction.parse_first_json_object`
    rather than assuming a bare JSON body.
    """

    DEFAULT_MODEL = DEFAULT_GATEWAY_MODEL

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        timeout_seconds: float = 300.0,
        max_output_tokens: int = 2048,
    ) -> None:
        super().__init__(
            model=model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="context maintenance",
        )
        self.max_output_tokens = max_output_tokens
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be greater than zero")

    @property
    def identifier(self) -> str:
        return f"openai-compatible:{self.model}"

    async def synthesize(self, prompt: str, *, system_prompt: str) -> str:
        return await asyncio.to_thread(self._synthesize_sync, prompt, system_prompt)

    def _synthesize_sync(self, prompt: str, system_prompt: str) -> str:
        # Key first, payload second -- the order this transport has always had.
        api_key = self._resolve_api_key()
        payload = self._chat_payload(system=system_prompt, user=prompt, max_tokens=self.max_output_tokens)
        body = self._post("/chat/completions", payload, api_key=api_key)
        return self._chat_content(body, content_kind="a string")


# ---------------------------------------------------------------------------
# Persistence: dream_decisions is the existing generic store (see module
# docstring, "Storage: no schema change")
# ---------------------------------------------------------------------------


def _artifact_job_name(scope: MemoryScope) -> str:
    return f"context-artifact:{scope.key}"


def _load_artifact(graph: StorageBackend, scope: MemoryScope) -> ContextArtifact | None:
    records = graph.dream_decisions(job_name=_artifact_job_name(scope), limit=1)
    if not records:
        return None
    return ContextArtifact.model_validate(records[0].details)


def _store_artifact(graph: StorageBackend, scope: MemoryScope, artifact: ContextArtifact) -> None:
    graph.record_dream_decision(
        DreamDecisionRecord(
            ran_at=artifact.updated_at,
            job_name=_artifact_job_name(scope),
            job_kind=DreamJobKind.CONTEXT_MAINTENANCE,
            agent_id="context-maintainer",
            agent_name="Context Maintainer",
            agent_scope=scope,
            decision_type="CONTEXT_ARTIFACT_UPDATED",
            summary=f"maintained context artifact for {scope.key} updated to v{artifact.version}",
            scope=scope,
            details=artifact.model_dump(mode="json"),
        )
    )


# ---------------------------------------------------------------------------
# Fingerprinting and diffing
# ---------------------------------------------------------------------------


def _fact_mark(
    fact: MemoryProfileFact,
    *,
    precision: int,
    now: datetime,
    recency_window_seconds: float,
    temporal_qualifiers_enabled: bool,
) -> ContextFactMark:
    # The fingerprint (what decides NEW/CHANGED/REMOVED and the zero-call
    # watermark fast path) is deliberately UNAFFECTED by the qualifier: aging
    # out of the recency window is not itself a "change" worth an LLM call,
    # it is a presentation nuance the NEXT rebuild (triggered by something
    # that actually changed) will reflect correctly.
    fingerprint_payload = {
        "fact": fact.fact,
        "confidence": round(fact.confidence, precision),
        "observed_count": fact.observed_count,
        "pinned": fact.pinned,
    }
    label = _qualified_fact_text(
        fact,
        now=now,
        recency_window_seconds=recency_window_seconds,
        enabled=temporal_qualifiers_enabled,
    )
    return ContextFactMark(
        fingerprint=payload_digest(fingerprint_payload),
        role=role_for_memory_type(fact.memory_type).value,
        label=label[:_LABEL_MAX_CHARS],
    )


def _compute_watermark(marks: dict[str, ContextFactMark]) -> str:
    return payload_digest({uuid: mark.fingerprint for uuid, mark in marks.items()})


def _datetime_sort_key(value: datetime | None) -> datetime:
    return value if value is not None else datetime.min.replace(tzinfo=UTC)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def _ordered_delta_uuids(
    uuids: set[str],
    *,
    facts_by_uuid: dict[str, MemoryProfileFact],
    fallback_marks: dict[str, ContextFactMark],
    recent_seed_limit: int,
    use_need_by_uuid: dict[str, float] | None = None,
) -> list[str]:
    """Order one delta group (NEW / CHANGED / REMOVED) for the prompt.

    STANDING members: all of them, uuid-sorted (deterministic, never capped
    — they are rare).  STRUCTURE members (WS-28 T2 — "critical commands,
    routing facts" is exactly the frequently-used-command home): all of
    them, ordered by demonstrated ``use_need`` descending, tie-broken by
    uuid — with no usage data (or none passed) this is byte-identical to the
    plain uuid sort STANDING still uses.  RECENT members: most-recent-first
    when a live fact is available (NEW/CHANGED), else uuid order (REMOVED
    has no live fact to rank by), capped to `recent_seed_limit` — the
    "recency seeds it" mechanism for the one role a busy session can flood.
    """
    resolved_use_need = use_need_by_uuid or {}
    role_of: dict[str, str] = {}
    for uuid in uuids:
        fact = facts_by_uuid.get(uuid)
        role_of[uuid] = fact.memory_type if fact is not None else fallback_marks[uuid].role
    standing = sorted(u for u in uuids if role_for_memory_type(role_of[u]) == ContextRole.STANDING)
    structure = sorted(
        (u for u in uuids if role_for_memory_type(role_of[u]) == ContextRole.STRUCTURE),
        key=lambda u: (-resolved_use_need.get(u, 0.0), u),
    )
    recent = [u for u in uuids if role_for_memory_type(role_of[u]) == ContextRole.RECENT]
    recent.sort(
        key=lambda u: _datetime_sort_key(
            facts_by_uuid[u].last_seen_at or facts_by_uuid[u].valid_from if u in facts_by_uuid else None
        ),
        reverse=True,
    )
    recent = recent[:recent_seed_limit]
    return [*standing, *structure, *recent]


def _label_for(
    uuid: str,
    *,
    facts_by_uuid: dict[str, MemoryProfileFact],
    fallback_marks: dict[str, ContextFactMark],
    now: datetime,
    policy: ContextPolicy,
) -> tuple[str, str]:
    fact = facts_by_uuid.get(uuid)
    if fact is not None:
        # WS-27 T5: the SAME qualifier `_fact_mark` bakes into a stored mark's
        # `label` — computed fresh here (rather than reading a mark) because a
        # NEW/CHANGED uuid may not have a stored mark yet.
        text = _qualified_fact_text(
            fact,
            now=now,
            recency_window_seconds=policy.recency_window_seconds,
            enabled=policy.temporal_qualifiers_enabled,
        )
        return role_for_memory_type(fact.memory_type).value, text[:_LABEL_MAX_CHARS]
    mark = fallback_marks[uuid]
    return mark.role, mark.label


def _build_prompt(
    *,
    previous_sections: dict[str, list[str]] | None,
    facts_by_uuid: dict[str, MemoryProfileFact],
    stored_marks: dict[str, ContextFactMark],
    new_uuids: set[str],
    changed_uuids: set[str],
    removed_uuids: set[str],
    policy: ContextPolicy,
    now: datetime,
    use_need_by_uuid: dict[str, float] | None = None,
) -> str:
    lines: list[str] = []
    if previous_sections is None:
        lines.append("PREVIOUS CONTEXT: (none — first build for this scope)")
    else:
        lines.append("PREVIOUS CONTEXT (JSON):")
        lines.append(json.dumps(previous_sections, sort_keys=True))
    lines.append("")
    lines.append("DELTA SINCE LAST BUILD:")

    def emit(heading: str, uuids: set[str]) -> None:
        if not uuids:
            return
        ordered = _ordered_delta_uuids(
            uuids,
            facts_by_uuid=facts_by_uuid,
            fallback_marks=stored_marks,
            recent_seed_limit=policy.recent_seed_limit,
            use_need_by_uuid=use_need_by_uuid,
        )
        lines.append(f"{heading}:")
        for uuid in ordered:
            role, label = _label_for(
                uuid, facts_by_uuid=facts_by_uuid, fallback_marks=stored_marks, now=now, policy=policy
            )
            lines.append(f"- [{role}] {label}")

    emit("NEW", new_uuids)
    emit("CHANGED", changed_uuids)
    emit("REMOVED (no longer active — superseded, demoted, expired, or pruned; delete stale references)", removed_uuids)
    if not (new_uuids or changed_uuids or removed_uuids):
        lines.append("(none — this call should not normally reach the LLM with an empty delta)")
    lines.append("")
    # Per-role token targets, not just one combined figure: measured live
    # (claude-haiku-4-5, real gateway) that a single combined target left the
    # model unaware "recent" specifically has its own guaranteed ~30% share,
    # and under real pressure it dropped the "recent" array to zero rather
    # than writing a partial one that would fit -- reasonable under a
    # combined budget, wrong under the actual per-role enforcement this
    # module applies afterward (_enforce_role_budgets: one role's shortfall
    # never grants another more room). Spelling out each role's own target
    # tells the model the tradeoff it is actually facing.
    role_targets = ", ".join(
        f"{role.value}~{int(policy.token_budget * policy.role_budget_shares[role.value])}" for role in ContextRole
    )
    lines.append(
        f"Target size: approximately {policy.token_budget} tokens total, split independently per array "
        f"({role_targets} tokens). Each array's budget is enforced separately -- writing less in one array "
        "never gives another array more room, so do not drop or skip an array to make space elsewhere; "
        "fit each array to its OWN target instead, trimming the least-actionable lines first."
    )
    return "\n".join(lines)


def _validate_sections(payload: dict[str, Any]) -> dict[str, list[str]]:
    if not isinstance(payload, dict):
        raise ValueError("context maintenance response must be a JSON object")
    sections: dict[str, list[str]] = {}
    for role in ContextRole:
        value = payload.get(role.value, [])
        if not isinstance(value, list):
            raise ValueError(f"context maintenance response field {role.value!r} must be a list")
        sections[role.value] = [str(line).strip() for line in value if str(line).strip()]
    return sections


# ---------------------------------------------------------------------------
# Role-budget enforcement and rendering (the hard, code-level guarantee —
# never trust the LLM to have honoured the requested budget)
# ---------------------------------------------------------------------------


def _header(scope: MemoryScope, version: int) -> str:
    return f"Memory context for {scope.key} [maintained v{version}]"


def _enforce_role_budgets(
    sections: dict[str, list[str]], policy: ContextPolicy, *, header_text: str
) -> tuple[dict[str, list[str]], int]:
    """Greedily fill each role's lines, in the ORDER the caller gave them,
    up to that role's own fixed token share. A role's overflow is silently
    dropped (the maintained artifact is a living brief, not a JIT-reference
    surface like the legacy per-call profile) — it never spills into another
    role's budget. `header_text` (the exact rendered header line, reserved
    before the per-role split) makes the reservation exact rather than an
    estimate. Returns (enforced_sections, total_tokens_used)."""
    header_tokens = _estimate_tokens(header_text + "\n")
    usable = max(0, policy.token_budget - header_tokens)
    enforced: dict[str, list[str]] = {}
    tokens_used = header_tokens
    for role in ContextRole:
        share = policy.role_budget_shares[role.value]
        role_budget = int(usable * share)
        lines = sections.get(role.value, [])
        heading_tokens = _estimate_tokens(f"{role.value.upper()}:\n")
        used = heading_tokens
        kept: list[str] = []
        for line in lines:
            line_tokens = _estimate_tokens(f"- {line}\n")
            if used + line_tokens <= role_budget:
                kept.append(line)
                used += line_tokens
            else:
                break
        enforced[role.value] = kept
        tokens_used += used
    return enforced, tokens_used


def _render(*, scope: MemoryScope, version: int, sections: dict[str, list[str]]) -> str:
    lines = [_header(scope, version)]
    for role in ContextRole:
        lines.append(f"{role.value.upper()}:")
        role_lines = sections.get(role.value, [])
        if role_lines:
            lines.extend(f"- {line}" for line in role_lines)
        else:
            lines.append("- None")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The public entrypoint
# ---------------------------------------------------------------------------


async def get_context(
    *,
    graph: StorageBackend,
    scope: MemoryScope,
    facts: list[MemoryProfileFact],
    policy: ContextPolicy | None = None,
    transport: ContextTransport | None = None,
    now: datetime | None = None,
) -> ContextArtifact:
    """Load-or-incrementally-update the maintained context artifact for `scope`.

    `facts` MUST already be the caller's fully visibility- and
    confidence-filtered context-visible fact list (i.e. exactly what
    `Memotron._profile_facts` returns) — this function never reads the
    graph for facts, only for artifact storage, so it never re-derives or
    re-checks visibility itself. See the module docstring for the full
    algorithm; in short: unchanged watermark -> return `stored` with zero
    transport calls and zero writes; otherwise diff against the stored
    fingerprint map, ask the transport to fold the delta into the previous
    sections, hard-enforce the per-role token budget in code, persist, and
    return the new version. A transport failure degrades to `stored`
    (unchanged) rather than raising; with no `stored` at all it degrades to
    a deterministic, un-persisted rendering of the raw facts.
    """
    resolved_policy = policy or ContextPolicy()
    resolved_now = now or datetime.now(UTC)

    current_marks = {
        fact.relationship_uuid: _fact_mark(
            fact,
            precision=resolved_policy.confidence_precision,
            now=resolved_now,
            recency_window_seconds=resolved_policy.recency_window_seconds,
            temporal_qualifiers_enabled=resolved_policy.temporal_qualifiers_enabled,
        )
        for fact in facts
    }
    watermark = _compute_watermark(current_marks)
    stored = _load_artifact(graph, scope)

    if stored is not None and stored.watermark == watermark:
        return stored  # the common case: zero LLM calls, zero writes.

    if not current_marks and stored is None:
        # Nothing has ever been recorded for this scope. Nothing to
        # summarize, so don't spend an LLM call manufacturing an empty
        # answer — but DO persist it, so a repeat call hits the fast path
        # above instead of re-entering this branch.
        empty_sections = {role.value: [] for role in ContextRole}
        artifact = ContextArtifact(
            scope_key=scope.key,
            version=1,
            watermark=watermark,
            sections=empty_sections,
            fact_marks={},
            rendered_text=f"{_header(scope, 1)}\n(no facts recorded for this scope yet)",
            model_identifier="none (empty scope)",
            updated_at=resolved_now,
            degraded=False,
            role_budget_shares=resolved_policy.role_budget_shares,
        )
        _store_artifact(graph, scope, artifact)
        return artifact

    # WS-28 T2: usage-ranked STRUCTURE fill.  Read only past the zero-call
    # fast path above (a no-change call never pays for this), and only here
    # -- not at read time -- so `get_context()`'s own "no query, no live
    # graph read for facts" contract (see module docstring) is unaffected;
    # this is a read of the utility PROJECTION for artifact-ordering only.
    use_need_by_uuid: dict[str, float] = {
        projection.relationship_uuid: _use_need(
            use_stability=projection.use_stability,
            outcome_quality=projection.outcome_quality,
        )
        for projection in graph.utility_projection(scope_key=scope.key, as_of=resolved_now)
    }

    facts_by_uuid = {fact.relationship_uuid: fact for fact in facts}
    stored_marks = stored.fact_marks if stored is not None else {}
    new_uuids = set(current_marks) - set(stored_marks)
    removed_uuids = set(stored_marks) - set(current_marks)
    changed_uuids = {
        uuid
        for uuid in set(current_marks) & set(stored_marks)
        if current_marks[uuid].fingerprint != stored_marks[uuid].fingerprint
    }

    resolved_transport = transport or OpenAICompatibleContextTransport()
    prompt = _build_prompt(
        previous_sections=stored.sections if stored is not None else None,
        facts_by_uuid=facts_by_uuid,
        stored_marks=stored_marks,
        new_uuids=new_uuids,
        changed_uuids=changed_uuids,
        removed_uuids=removed_uuids,
        policy=resolved_policy,
        now=resolved_now,
        use_need_by_uuid=use_need_by_uuid,
    )

    try:
        raw = await resolved_transport.synthesize(prompt, system_prompt=_SYSTEM_PROMPT)
        sections = _validate_sections(
            parse_first_json_object(strip_markdown_fences(raw), source="context maintenance response")
        )
    except (ValueError, KeyError, TypeError):
        if stored is not None:
            return stored
        return _fallback_artifact(
            scope=scope,
            facts=facts,
            current_marks=current_marks,
            policy=resolved_policy,
            now=resolved_now,
            use_need_by_uuid=use_need_by_uuid,
        )

    next_version = stored.version + 1 if stored is not None else 1
    enforced_sections, _tokens_used = _enforce_role_budgets(
        sections, resolved_policy, header_text=_header(scope, next_version)
    )
    rendered_text = _render(scope=scope, version=next_version, sections=enforced_sections)
    artifact = ContextArtifact(
        scope_key=scope.key,
        version=next_version,
        watermark=watermark,
        sections=enforced_sections,
        fact_marks=current_marks,
        rendered_text=rendered_text,
        model_identifier=resolved_transport.identifier,
        updated_at=resolved_now,
        degraded=False,
        role_budget_shares=resolved_policy.role_budget_shares,
    )
    _store_artifact(graph, scope, artifact)
    return artifact


def _fallback_artifact(
    *,
    scope: MemoryScope,
    facts: list[MemoryProfileFact],
    current_marks: dict[str, ContextFactMark],
    policy: ContextPolicy,
    now: datetime,
    use_need_by_uuid: dict[str, float] | None = None,
) -> ContextArtifact:
    """Deterministic, un-persisted rendering used only when the transport
    fails AND there is no prior good artifact to degrade to. `watermark=""`
    guarantees the next call can never treat this as settled (a real
    watermark is a 64-char hex sha256 digest and can never be the empty
    string), so the LLM is retried on every subsequent call until it
    succeeds."""
    resolved_use_need = use_need_by_uuid or {}
    facts_by_role: dict[str, list[MemoryProfileFact]] = {role.value: [] for role in ContextRole}
    for fact in facts:
        facts_by_role[role_for_memory_type(fact.memory_type).value].append(fact)
    sections: dict[str, list[str]] = {}
    for role in ContextRole:
        role_facts = facts_by_role[role.value]
        if role is ContextRole.STRUCTURE:
            # WS-28 T2: STRUCTURE ("critical commands, routing facts") is the
            # frequently-used-command home -- fill it in demonstrated-use
            # order first, falling back to the ORIGINAL (confidence,
            # observed_count) ordering as the tie-break, so a scope with no
            # use events yet renders byte-identically to before.
            ordered_role_facts = sorted(
                role_facts,
                key=lambda f: (
                    resolved_use_need.get(f.relationship_uuid, 0.0),
                    f.confidence,
                    f.observed_count,
                ),
                reverse=True,
            )
        else:
            ordered_role_facts = sorted(role_facts, key=lambda f: (f.confidence, f.observed_count), reverse=True)
        # WS-27 T5: this IS the hard, fully-deterministic guarantee (no LLM in the
        # loop to trust) — a stable current fact renders bare, a recently-established
        # or superseded one never appears without its marker.
        role_lines: list[str] = [
            _qualified_fact_text(
                fact,
                now=now,
                recency_window_seconds=policy.recency_window_seconds,
                enabled=policy.temporal_qualifiers_enabled,
            )
            for fact in ordered_role_facts
        ]
        sections[role.value] = role_lines
    enforced_sections, _tokens_used = _enforce_role_budgets(sections, policy, header_text=_header(scope, 0))
    rendered_text = _render(scope=scope, version=0, sections=enforced_sections)
    return ContextArtifact(
        scope_key=scope.key,
        version=0,
        watermark="",
        sections=enforced_sections,
        fact_marks=current_marks,
        rendered_text=rendered_text,
        model_identifier="deterministic-fallback",
        updated_at=now,
        degraded=True,
        role_budget_shares=policy.role_budget_shares,
    )
