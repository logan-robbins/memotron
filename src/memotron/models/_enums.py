"""Enumerated domain vocabularies.

Every one of these is layer 0 -- no hard dependency on any other definition in
this package (verified with ``scripts/verify/layer.py``), which is why they can
lead. Eleven of the twelve ordering constraints in the original module were a
model defaulting a field to a member of one of these, so defining them first
removes almost all of the ordering pressure from the rest of the split.
"""

from __future__ import annotations

from enum import StrEnum


class ScopeKind(StrEnum):
    AGENT = "agent"
    TENANT = "tenant"
    USER = "user"
    CUSTOMER = "customer"


class EpisodeType(StrEnum):
    TEXT = "text"
    MESSAGE = "message"
    JSON = "json"


class DreamJobKind(StrEnum):
    FORMATION = "formation"
    CONSOLIDATION = "consolidation"
    PRUNING = "pruning"
    COHERENCE = "coherence"
    CONTEXT_MAINTENANCE = "context_maintenance"
    """A `context.get_context()` artifact update -- not a scheduled DreamJob
    (there is no DreamJob/DreamConfig.jobs entry of this kind; it is written
    synchronously inside `Memotron.profile(maintain_context=True)`).  Its
    only purpose is to stamp `DreamDecisionRecord.job_kind` so a maintained
    context artifact's `dream_decisions` row is distinguishable from a real
    FORMATION/CONSOLIDATION/PRUNING/COHERENCE decision by kind, not only by
    `job_name` string-matching `"context-artifact:"` or by
    `decision_type == "CONTEXT_ARTIFACT_UPDATED"`. Introduced after context.py's
    `_store_artifact` was found (live-gateway verification pass) to still be
    stamping `CONSOLIDATION`, which put every maintained-context update in the
    same `job_kind` bucket as genuine rollup-consolidation decisions."""


class RelationshipStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    PRUNED = "pruned"


class RelationshipCardinality(StrEnum):
    MULTI_ACTIVE = "multi_active"
    SINGLE_ACTIVE = "single_active"


class MemoryIngestionMode(StrEnum):
    CLIENT_MANAGED = "client_managed"
    MANAGED_DREAMING = "managed_dreaming"


class UseEventKind(StrEnum):
    """Observed stage in the retrieval-to-behaviour lifecycle."""

    RETRIEVED = "retrieved"
    INJECTED = "injected"
    CITED_OR_USED = "cited_or_used"


class OutcomeVerdict(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"
    CORRECTED = "corrected"
    UNKNOWN = "unknown"


class OutcomeAttributionMethod(StrEnum):
    """Named, receipt-bound credit rule for a task involving multiple memories."""

    CITED_FULL_CREDIT = "cited_full_credit"
    UNCITED_ZERO_CREDIT = "uncited_zero_credit"
    UNCITED_FRACTIONAL_CREDIT = "uncited_fractional_credit"
    NEGATIVE_COHERENCE_FLAG = "negative_coherence_flag"


class MemoryAuthority(StrEnum):
    """Non-malleable origin class used when current truth is superseded."""

    GENERATED_OUTPUT = "generated_output"
    UNTRUSTED = "untrusted"
    AGENT = "agent"
    USER = "user"
    OPERATOR = "operator"
    SYSTEM = "system"


class ArtifactClass(StrEnum):
    """WS-10: Class of persistent artifact that can govern agent behavior.

    An agent does not learn from memory alone.  Its effective behavior is
    governed by a heterogeneous set of persistent artifacts that are authored
    and updated on different cadences and carry different execution precedence:

    * ``MEMORY``  — declarative facts/knowledge objects (the property graph);
      updated continuously by dreaming/feedback; advisory at execution time.
    * ``SKILL``   — procedural instructions (self- or user-authored); updated
      rarely; executed step-by-step, so it dominates at run time.
    * ``FILE``    — notes, prior work, scratch directories; accreted, rarely
      garbage-collected; referenced situationally.

    Dreaming reconciles MEMORY-vs-MEMORY contradictions.  The coherence cycle
    extends reconciliation across these classes.
    """

    MEMORY = "memory"
    SKILL = "skill"
    FILE = "file"
    GENERATED_OUTPUT = "generated_output"


class DirectiveStance(StrEnum):
    """WS-10: How a directive relates to a behavior on its subject.

    A small closed vocabulary so cross-artifact contradiction can be evaluated
    deterministically without a language model.  ``REQUIRE``/``FORBID`` are the
    prescriptive poles; ``ASSERT`` claims a state already holds (e.g. a skill
    that encodes "the cost-efficiency check is sufficient"); ``GUIDE`` encodes
    a procedure; ``PREFER`` is a soft inclination.
    """

    REQUIRE = "require"
    FORBID = "forbid"
    PREFER = "prefer"
    ASSERT = "assert"
    GUIDE = "guide"


class ClaimMode(StrEnum):
    """What an extracted statement *means* before it becomes graph state.

    Memory type describes storage and lifecycle policy; claim mode describes the
    assertion being made.  Keeping them separate prevents a descriptive report
    of prior behaviour from silently becoming a normative directive.
    """

    DESCRIPTIVE_ASSERTION = "descriptive_assertion"
    REQUIREMENT = "requirement"
    PREFERENCE = "preference"
    DIRECTIVE = "directive"
    CORRECTION = "correction"
    REPORT_OF_BEHAVIOR = "report_of_behavior"


class CoherenceIncidentKind(StrEnum):
    """WS-10: Why a cross-artifact coherence incident was raised."""

    ESCALATION_WINDUP = "escalation_windup"
    CROSS_ARTIFACT_CONTRADICTION = "cross_artifact_contradiction"


class CoherenceIncidentStatus(StrEnum):
    """WS-10: Lifecycle of a coherence incident (mirrors RelationshipStatus style)."""

    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class MemoryType(StrEnum):
    """First-class taxonomy for memory relationships.

    Each type carries its own governance defaults, cardinality semantics,
    and retrieval budget allocation (WS-5). Cross-walked to the CoALA
    taxonomy (working / episodic / semantic / procedural).

    anchor     — semantic: how to ADDRESS a thing — its exact identifier, path,
                 host, or owning system. The name is deliberately narrow: the
                 old name for this type was ``identity``, which read as "facts
                 about what something is" and absorbed 84% of a documentation
                 corpus (every descriptive sentence is arguably an identity).
                 An anchor sends the agent somewhere; prose does not.
                 Low churn, conservative dedup — never merge "Mike"/"Michael".
    preference — semantic: stable user/customer preferences.
                 Moderate churn, multi-active.
    requirement — semantic: explicit obligations and approval gates.
                 Single-active (contradiction-collapsing), high trust.
    directive  — procedural: agent operating lessons and behavioural rules.
                 Highest churn, tightest controls (the D2 problem).
    state      — episodic: time-bounded situational facts (open ticket, sprint).
                 Short retention, explicit valid_to required.
    decision   — semantic/episodic: ADRs, operator corrections, roadmap decisions.
                 Single-active, high-audit, long retention, full supersession lineage.
    incident   — semantic/episodic: postmortems and production events.
                 Single-active, high-audit, long retention.
    """

    ANCHOR = "anchor"
    PREFERENCE = "preference"
    REQUIREMENT = "requirement"
    DIRECTIVE = "directive"
    STATE = "state"
    DECISION = "decision"
    INCIDENT = "incident"
    ROLLUP = "rollup"


class ArchivedMatchDisposition(StrEnum):
    """What retrieval *did* with the archived rows it matched.

    The report is populated in both retrieval modes; this field is what makes
    the two unambiguous.

    ``REVIVED``
        The default.  Retrieval revived every reported row as a side effect of
        the read (the historical behaviour), so ``matches`` is the list of rows
        that were brought back and they are also present in ``results``.
    ``AVAILABLE_TO_RESTORE``
        ``DreamConfig.pure_read_retrieval`` is on, so retrieval mutated
        nothing.  ``matches`` is the list of rows that *could* be revived by
        the explicit :meth:`memotron.Memotron.restore_archived_memory`
        curation action; none of them are in ``results``.
    """

    REVIVED = "revived"
    AVAILABLE_TO_RESTORE = "available_to_restore"


class QuarantineStatus(StrEnum):
    """Lifecycle of one stored extraction candidate (WS-24, extended for the
    staged EXTRACT / RESOLVE / GOVERN pipeline).

    ``PENDING`` is the stage-1 state: the candidate is stored raw, exactly as
    produced by extraction, before stage-3 governance has run over it.  Every
    other status is a stage-3 VERDICT, never a stage-1 outcome:

    * ``QUARANTINED`` — governance marked the row and kept it (retained,
      receipted, inspectable, promotable) instead of aborting the run.
    * ``PROMOTED`` — governance accepted the row and it now backs a materialized
      relationship (``promoted_relationship_uuid``).
    * ``DISCARDED`` — an operator permanently dismissed a quarantined row.

    A row moves ``PENDING -> {QUARANTINED, PROMOTED}`` automatically as part of
    formation, or later via an explicit re-govern pass over already-stored rows
    (no re-extraction); ``QUARANTINED -> {PROMOTED, DISCARDED}`` moves only via
    operator adjudication or re-govern.
    """

    PENDING = "pending"
    QUARANTINED = "quarantined"
    PROMOTED = "promoted"
    DISCARDED = "discarded"
