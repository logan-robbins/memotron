"""What the extractor is told to look for, and how it is prompted.

A DreamInstructionSet is the schema half of extraction -- which node labels and
relationship types may be produced, with what properties and what minimum
confidence. The prompt types beside it are the wording half. Keeping them together
is deliberate: a prompt that asks for a relationship the instruction set forbids
is a config error, and both halves have to be readable at once to see it."""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator

from memotron.config._actionability import (
    ActionabilityPolicy,
)
from memotron.config._claim_mode import (
    RELATIONSHIP_TYPE_MEMORY_TYPE_MAP,
)
from memotron.config._predicates import (
    DEFAULT_MAX_PREDICATE_WORDS,
)
from memotron.models import (
    DreamJobKind,
    MemoryScope,
    MemoryType,
    RelationshipCardinality,
    ScopeKind,
)

# Research-grade system prompt for LLM extraction transports.
# Explains the temporal graph model, cardinality semantics, confidence calibration,
# graph-context deduplication rules, and required JSON output schema.
DEFAULT_EXTRACTION_SYSTEM_PROMPT = """\
You are a temporal knowledge graph memory extractor for an agent memory system.

Your job is to read a source episode and extract durable, typed, scoped relationship memories \
that will be materialized into a property graph. That graph is the agent's long-term memory — \
it must be accurate, temporally precise, and free of duplicates.

GRAPH MODEL
Every memory is a directed edge between two named entity nodes:

  [subject]  —[RELATIONSHIP_TYPE]→  [object]

Entities are extracted ONCE, as their own roster, before any edge — see
OUTPUT FORMAT below. A relation may only name a subject/object that appears
in that roster; it can never introduce a node on the fly.

Key edge attributes:
• valid_from — ISO-8601 UTC timestamp when this fact became true in the world (not when recorded).
• valid_to   — ISO-8601 UTC timestamp when the fact stopped being true. Null means "still true".
• confidence — Float 0.0–1.0 reflecting extraction certainty (see calibration below).
• subject_properties / object_properties — Entity-state attributes true at valid_from time,
  seeded from that entity's own roster properties and refined per edge if the source narrows them.

TEMPORAL EXTRACTION RULES
1. Default: use the episode reference_time as valid_from unless the source states otherwise.
2. Explicit start: if the source says "starting March 2026", "effective Q1", or "since last week",
   convert that anchor to an ISO-8601 UTC timestamp for valid_from.
3. Explicit end: if the source says "until the audit", "through June 30", or "temporary",
   convert to ISO-8601 UTC for valid_to. Never leave a clearly expiring fact without valid_to.
4. Supersession: when a new fact replaces an older stated fact for the same subject–predicate key,
   extract the new version with the correct valid_from. Keep the same predicate so the graph can
   automatically supersede the older version using its cardinality rules.
5. Node properties reflect the entity's state at valid_from. They can evolve across episodes —
   always record the value that was true at valid_from, not the current application state.
6. Do not invent temporal bounds. Only add valid_from / valid_to when they are stated or
   unambiguously implied by the source.

CONFIDENCE CALIBRATION
0.95–1.0  Explicitly stated as a requirement, policy, or fact with no qualification.
0.80–0.94 Strongly implied, or stated with minor hedging ("generally", "usually").
0.60–0.79 Inferred from context, or explicitly uncertain ("I believe", "probably").
< 0.60    Do not extract — confidence is too low to produce a useful graph fact.

GRAPH CONTEXT HANDLING
Existing graph context is included so you can avoid duplicates and detect corrections:
• If a proposed memory matches an active context entry (same subject, predicate, object),
  emit it when this episode independently corroborates it. Include the source_text from this
  episode. The materializer will deterministically reinforce the existing relationship and
  receipt the new observation; never manufacture a duplicate from graph context alone.
• If a proposed memory contradicts an active context entry (same subject and predicate,
  different object), extract only the new version with the correct valid_from.
  The graph will supersede the old entry based on its configured cardinality rules.
• Never copy graph context entries verbatim into the output.

OUTPUT FORMAT
Cookbook shape: entities first, as their own list, then relations that may
ONLY name an entity from that list. A relation naming anything else is
rejected outright — this is the structural guard against inventing a node
that was never actually extracted. Return exactly one JSON object — no
prose, no markdown, no extra fields:
{
  "entities": [
    {
      "name":        "<canonical entity name, exactly as it will appear in relations below>",
      "type":        "<one of the configured allowed node labels>",
      "description": "<one sentence, grounded in this episode, that disambiguates this entity from any other of the same name>",
      "properties":  { "<key>": "<value at valid_from time>" }
    }
  ],
  "relations": [
    {
      "source":             "<an entity name from the list above>",
      "predicate":          "<relationship verb or phrase>",
      "target":             "<an entity name from the list above>",
      "relationship_type":  "<one of the configured allowed types>",
      "confidence":         0.0,
      "valid_from":         "<ISO-8601 UTC>",
      "valid_to":           "<ISO-8601 UTC or null>",
      "source_text":        "<exact quote from the episode supporting this memory>",
      "claim_mode":         "<descriptive_assertion|requirement|preference|directive|correction|report_of_behavior>",
      "metadata":           {}
    }
  ]
}
Every entity you list must be used by at least one relation, and every
relation's source/target must be one of the names you listed — an
unlisted name is not a shortcut, it is a dropped candidate.\
"""


def _normalize_non_blank_text(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} cannot be blank")
    return normalized


def _normalize_prompt_lines(value: Any, field_name: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raise ValueError(f"{field_name} must be a sequence of strings")
    if not isinstance(value, list | tuple):
        raise ValueError(f"{field_name} must be a sequence of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} entries must be strings")
        stripped = item.strip()
        if not stripped:
            raise ValueError(f"{field_name} entries cannot be blank")
        normalized.append(stripped)
    return tuple(normalized)


RESERVED_NODE_PROPERTIES: tuple[str, ...] = (
    "name",
    "scope_kind",
    "scope_id",
    "scope_key",
    "graph_key",
)


UNIVERSAL_NODE_PROPERTIES: tuple[str, ...] = ("description", "aliases")


class NodeInstruction(BaseModel):
    label: str = Field(min_length=1)
    query: str = Field(min_length=1)
    properties: tuple[str, ...] = ()
    strict_properties: bool = True
    """When True (default), extracted properties not in the allowed list raise an error.
    Set False for LLM transports where the model may invent valid-sounding property
    keys that are not in the schema — unknown keys are silently stripped."""
    required_properties: tuple[str, ...] = ()
    """WS-27 T2: property keys a candidate for this label MUST set (non-blank),
    on top of the ``strict_properties`` allow-list above.  ``strict_properties``
    answers "may this key be set at all"; this answers "must it be" — e.g. a
    ``Credential`` node requires a ``reference`` (never a raw secret value), an
    ``Environment`` node requires its canonical ``status``.  A candidate missing
    a required key is quarantined (``REQUIRED_PROPERTY_MISSING``), never
    rejected outright — a later policy change (or an operator correction) can
    still re-govern it into the graph without re-extraction.  Every key here
    must already be a member of ``properties`` (or a universal property);
    requiring a key extraction is never allowed to set is a config mistake and
    fails fast at construction, not at the first quarantined candidate."""
    require_description: bool = False
    """WS-27 T1: when True, an entity carrying this label MUST set a non-blank
    ``description`` — the disambiguator both the entity-resolution score and
    the reading agent rely on (e.g. "virtual key" is meaningless without
    knowing WHICH credential).  A description-less candidate is quarantined
    (``ENTITY_DESCRIPTION_MISSING``), not rejected: ``description`` is already
    a :data:`UNIVERSAL_NODE_PROPERTIES` key on every label, so nothing about
    the candidate's shape is wrong, only its completeness.  Default ``False``
    keeps every existing instruction set byte-for-byte — this is opt-in per
    label, not a blanket new requirement."""
    aliases: tuple[str, ...] = ()
    """WS-27 T1: EXTRA property key names (beyond the universal ``aliases``
    key every label already accepts — see :data:`UNIVERSAL_NODE_PROPERTIES`)
    that this label ALSO treats as alias-bearing.  Most tenants never set
    this: the universal ``aliases`` key is enough.  It exists for a tenant
    whose own extraction vocabulary already names alternate surface forms
    differently (``aka``, ``also_known_as``, ...) and wants THAT key
    recognised for this label without renaming it in the source documents."""

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("node instruction label cannot be blank")
        return normalized

    @field_validator("required_properties", "aliases")
    @classmethod
    def normalize_key_tuple(cls, value: Any, info: ValidationInfo) -> tuple[str, ...]:
        if value is None:
            return ()
        if isinstance(value, str):
            raise ValueError(f"{info.field_name} must be a sequence of strings, not a bare string")
        normalized: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise ValueError(f"{info.field_name} entries must be strings")
            stripped = item.strip()
            if not stripped:
                raise ValueError(f"{info.field_name} entries cannot be blank")
            normalized.append(stripped)
        return tuple(dict.fromkeys(normalized))

    @model_validator(mode="after")
    def validate_required_properties_are_allowed(self) -> NodeInstruction:
        allowed = set(self.properties) | set(UNIVERSAL_NODE_PROPERTIES) | set(self.aliases)
        unknown = [key for key in self.required_properties if key not in allowed]
        if unknown:
            raise ValueError(
                f"NodeInstruction {self.label!r} requires {unknown!r}, which is not in "
                f"its own allowed property set {sorted(allowed)!r} — add it to `properties` "
                "(or `aliases`) first, or extraction can never satisfy the requirement."
            )
        return self


class FewShotExample(BaseModel):
    """A structured few-shot extraction example for inclusion in a prompt profile."""

    description: str = ""
    episode_body: str = Field(min_length=1)
    reference_time_note: str = ""
    expected_memories: tuple[dict[str, Any], ...] = ()

    @field_validator("episode_body")
    @classmethod
    def normalize_episode_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("episode_body cannot be blank")
        return normalized

    @field_validator("expected_memories", mode="before")
    @classmethod
    def normalize_expected_memories(cls, value: Any) -> tuple[dict[str, Any], ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise ValueError("expected_memories must be a list or tuple of dicts")

    def render(self, index: int) -> str:
        header = f"--- Example {index}"
        if self.description:
            header += f" ({self.description})"
        header += " ---"
        lines = [header]
        if self.reference_time_note:
            lines.append(f"Context: {self.reference_time_note}")
        lines.append(f"Episode text: {self.episode_body}")
        if self.expected_memories:
            lines.append("Expected output:")
            lines.append(json.dumps(self.rendered_expected_output(), indent=2))
        lines.append("---")
        return "\n".join(lines)

    def rendered_expected_output(self) -> dict[str, Any]:
        """``expected_memories`` shown in the SAME shape the prompt demands.

        The examples are STORED flat (one dict per memory, carrying ``subject``,
        ``subject_label``, ``predicate``, ``object``, ``object_label``, ...)
        because that is what ``ExtractedMemory`` validates.  The [0025] prompt,
        however, instructs the model to "Return exactly one JSON object with an
        entities array and a relations array".  Rendering the stored shape
        verbatim put ``{"memories": [...]}`` under the heading "Expected
        output", directly contradicting that instruction.

        A worked example outranks an instruction: the model copied the examples,
        returned ``memories``, and its entity objects -- ``{name, type,
        description}`` -- were validated as if each were a relation, failing
        with five missing fields (subject, predicate, object,
        relationship_type, confidence).  Measured on the jedai-gateway ingest:
        one ``model_validation_failed`` rejection per extracted entity, on every
        episode, while relations materialized normally.

        So the example is PROJECTED into the cookbook envelope at render time:
        each memory's endpoints become entity-roster entries (deduplicated by
        name, first label and description win) and the memory itself becomes a
        relation naming those entities.  Stored example data is untouched, and
        instruction and demonstration finally agree.
        """
        entities: dict[str, dict[str, Any]] = {}
        relations: list[dict[str, Any]] = []

        def remember_entity(name: Any, label: Any, properties: Any) -> None:
            if not isinstance(name, str) or not name.strip():
                return
            key = name.strip()
            entry = entities.setdefault(key, {"name": key})
            if isinstance(label, str) and label.strip():
                entry.setdefault("type", label.strip())
            if isinstance(properties, dict):
                description = properties.get("description")
                if isinstance(description, str) and description.strip():
                    entry.setdefault("description", description.strip())

        for memory in self.expected_memories:
            remember_entity(
                memory.get("subject"),
                memory.get("subject_label"),
                memory.get("subject_properties"),
            )
            remember_entity(
                memory.get("object"),
                memory.get("object_label"),
                memory.get("object_properties"),
            )
            relation = {
                key: value
                for key, value in memory.items()
                if key
                not in {
                    "subject",
                    "object",
                    "subject_label",
                    "object_label",
                    "subject_properties",
                    "object_properties",
                }
            }
            relation["source"] = memory.get("subject")
            relation["target"] = memory.get("object")
            relations.append(relation)

        return {"entities": list(entities.values()), "relations": relations}


class DreamPromptOverride(BaseModel):
    goal: str | None = None
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()

    @field_validator("goal")
    @classmethod
    def normalize_optional_goal(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, "prompt override goal")

    @field_validator("include", "exclude", "rules", "examples", mode="before")
    @classmethod
    def normalize_lines(cls, value: Any, info: ValidationInfo) -> tuple[str, ...]:
        return _normalize_prompt_lines(value, f"prompt override {info.field_name}")

    @property
    def is_empty(self) -> bool:
        return self.goal is None and not self.include and not self.exclude and not self.rules and not self.examples


class DreamPromptProfile(BaseModel):
    """Formation extraction profile; it never authorizes mutations or synthesis."""

    name: str = Field(min_length=1)
    version: str = Field(default="v1", min_length=1)
    goal: str = Field(min_length=1)
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()
    examples: tuple[str, ...] = ()
    few_shot_examples: tuple[FewShotExample, ...] = ()
    prompt_text: str | None = None
    motive_goal: str | None = None

    @field_validator("name", "version", "goal")
    @classmethod
    def normalize_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"prompt profile {info.field_name}")

    @field_validator("include", "exclude", "rules", "examples", mode="before")
    @classmethod
    def normalize_lines(cls, value: Any, info: ValidationInfo) -> tuple[str, ...]:
        return _normalize_prompt_lines(value, f"prompt profile {info.field_name}")

    @field_validator("few_shot_examples", mode="before")
    @classmethod
    def normalize_few_shot_examples(cls, value: Any) -> tuple[FewShotExample, ...]:
        if value is None:
            return ()
        if isinstance(value, (list, tuple)):
            return tuple(FewShotExample.model_validate(item) if isinstance(item, dict) else item for item in value)
        raise ValueError("few_shot_examples must be a list or tuple of FewShotExample objects")

    @field_validator("prompt_text")
    @classmethod
    def normalize_prompt_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _normalize_non_blank_text(value, "prompt profile prompt_text")

    @field_validator("motive_goal")
    @classmethod
    def normalize_motive_goal(cls, value: str | None) -> str | None:
        return None if value is None else _normalize_non_blank_text(value, "prompt profile motive_goal")

    @property
    def key(self) -> str:
        return f"{self.name}@{self.version}"

    def with_override(self, override: DreamPromptOverride | None) -> DreamPromptProfile:
        if override is None or override.is_empty:
            return self
        return self.model_copy(
            update={
                "goal": override.goal or self.goal,
                "include": (*self.include, *override.include),
                "exclude": (*self.exclude, *override.exclude),
                "rules": (*self.rules, *override.rules),
                "examples": (*self.examples, *override.examples),
            }
        )

    def with_motive_goal(self, goal: str | None) -> DreamPromptProfile:
        """Render Motive intent explicitly without conflating it with the profile."""
        return self if goal is None else self.model_copy(update={"motive_goal": goal})

    def render_prompt(self) -> str:
        if self.prompt_text is not None:
            return (
                self.prompt_text
                if self.motive_goal is None
                else f"{self.prompt_text}\nResolved Motive goal: {self.motive_goal}"
            )
        lines = [
            f"Dream prompt profile: {self.key}",
            f"Memory formation goal: {self.goal}",
        ]
        if self.motive_goal is not None:
            lines.append(f"Resolved Motive goal: {self.motive_goal}")
        self._append_prompt_lines(lines, "Look for:", self.include)
        self._append_prompt_lines(lines, "Skip:", self.exclude)
        self._append_prompt_lines(lines, "Prompt rules:", self.rules)
        self._append_prompt_lines(lines, "Examples:", self.examples)
        if self.few_shot_examples:
            lines.append("Few-shot extraction examples:")
            for i, example in enumerate(self.few_shot_examples, 1):
                lines.append(example.render(i))
        return "\n".join(lines)

    def _append_prompt_lines(self, lines: list[str], heading: str, values: tuple[str, ...]) -> None:
        if not values:
            return
        lines.append(heading)
        lines.extend(f"- {value}" for value in values)


class DreamAgentConfig(BaseModel):
    agent_id: str = Field(default="dream-agent", min_length=1)
    name: str = Field(default="Dream Agent", min_length=1)
    scope: MemoryScope = Field(default_factory=lambda: MemoryScope(kind=ScopeKind.AGENT, scope_id="dream-agent"))
    decision_policy: str = Field(
        default=(
            "Decide whether the proposed offline memory maintenance action should run. "
            "Record a concise rationale summary and structured decision details."
        ),
        min_length=1,
    )

    @field_validator("agent_id", "name", "decision_policy")
    @classmethod
    def normalize_agent_text(cls, value: str, info: ValidationInfo) -> str:
        return _normalize_non_blank_text(value, f"dream agent {info.field_name}")

    def created_by(self, job_kind: DreamJobKind) -> str:
        return f"dream-agent:{self.agent_id}:{job_kind.value}"


class RelationshipInstruction(BaseModel):
    type: str = Field(min_length=1)
    source_label: str = Field(min_length=1)
    target_label: str = Field(min_length=1)
    open_predicate: bool = False
    """Accept ANY predicate the model emits, not just :attr:`type`.

    The entity vocabulary is closed and the relation vocabulary is open --
    Anthropic's knowledge-graph guide extracts ``EntityType`` from a ``Literal``
    enum while ``Relation.predicate`` is a free "short verb phrase".  The
    asymmetry is load-bearing: entity types are what resolution and traversal
    key on, so they must be closed, while predicates never need canonicalizing
    because retrieval ANN-matches the EMBEDDED FACT SENTENCE, not the predicate
    label (``dreaming.py``: "embed the full fact text for storage").  A closed
    predicate set buys nothing the fact embedding does not already provide, and
    costs every candidate whose verb happens to fall outside it.

    ``source_label`` / ``target_label`` accept ``"*"`` on an open instruction,
    meaning any label in :attr:`DreamInstructionSet.allowed_labels`.  Endpoint
    labels are still validated against that closed set, so an open predicate
    cannot smuggle in an untyped entity.
    """
    query: str = Field(min_length=1)
    temporal_semantics: str = Field(
        default="The relationship is valid from the episode reference time unless a more specific valid_from is provided.",
        min_length=1,
    )
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    cardinality: RelationshipCardinality = RelationshipCardinality.MULTI_ACTIVE
    memory_type: MemoryType | None = None
    """The semantic memory type for this relationship.

    When not set explicitly, resolved from RELATIONSHIP_TYPE_MEMORY_TYPE_MAP
    (REQUIRES → requirement, PREFERS → preference, SHOULD → directive).
    If the relationship type is not in the canonical map and memory_type is not
    set, config validation raises a ValueError (fail-fast).
    """

    @field_validator("type")
    @classmethod
    def normalize_relationship_type(cls, value: str) -> str:
        normalized = value.strip().replace(" ", "_").upper()
        if not normalized:
            raise ValueError("relationship instruction type cannot be blank")
        return normalized

    @model_validator(mode="after")
    def resolve_memory_type(self) -> RelationshipInstruction:
        if self.memory_type is None:
            resolved = RELATIONSHIP_TYPE_MEMORY_TYPE_MAP.get(self.type)
            if resolved is None:
                raise ValueError(
                    f"RelationshipInstruction type {self.type!r} is not in the canonical "
                    f"RELATIONSHIP_TYPE_MEMORY_TYPE_MAP. Set memory_type explicitly. "
                    f"Known types: {sorted(RELATIONSHIP_TYPE_MEMORY_TYPE_MAP)}"
                )
            self.memory_type = resolved
        return self


# Default importance weights by MemoryType value (Generative Agents formula — importance axis).
# Higher weight = more important = higher salience, all else equal.
# These are pure-Python constants; no LLM or network call is made.
DEFAULT_IMPORTANCE_WEIGHTS: dict[str, float] = {
    MemoryType.ANCHOR.value: 0.9,
    MemoryType.REQUIREMENT.value: 0.9,
    MemoryType.DECISION.value: 0.85,
    MemoryType.INCIDENT.value: 0.80,
    MemoryType.PREFERENCE.value: 0.65,
    MemoryType.DIRECTIVE.value: 0.5,
    MemoryType.STATE.value: 0.4,
}


class SalienceRubric(BaseModel):
    """WS-2: Generative Agents salience formula — recency × importance × relevance.

    All three components are deterministic and hermetic in the default path (no LLM,
    no network calls). The rubric is a quality gate on extracted memories before they
    enter the graph.

    No-op invariant
    ---------------
    When min_salience == 0.0 AND max_memories_per_episode is None, the rubric is
    completely inert: memories pass unfiltered and in their original order.  This is
    the default so that existing tests and the offline simulation are unaffected.

    Components
    ----------
    recency:
        Exponential decay from memory.valid_from relative to the episode reference_time.
        half_life_seconds controls how fast recency decays.  Memories at/after
        reference_time (formation is the common case) → recency ≈ 1.0.

    importance:
        Deterministic per-type weight from DEFAULT_IMPORTANCE_WEIGHTS, optionally scaled
        by confidence (scale_by_confidence=True, default False to keep it pure-type-weight).
        Custom importance_weights override the defaults per type.

    relevance:
        Cosine similarity between the memory's fact embedding and the episode-body
        embedding, computed via the injected EmbeddingTransport.  When embeddings are
        unavailable or unconfigured, defaults to 1.0 (no penalty).

    Downstream (WS-3)
    -----------------
    Motives will override min_salience, max_memories_per_episode, and importance_weights
    per job at construction time; the rubric object is carried on DreamInstructionSet
    and overridable per DreamJob.
    """

    min_salience: float = Field(default=0.0, ge=0.0, le=1.0)
    """Memories below this threshold are dropped.  Default 0.0 = keep everything (no-op)."""

    max_memories_per_episode: int | None = Field(default=None, ge=1)
    """Cap on memories surviving per episode after score-sort.  None = unlimited (no-op)."""

    half_life_seconds: float = Field(default=86400.0, gt=0.0)
    """Recency half-life in seconds.  Default 24 h.  Memories at/after reference_time → 1.0."""

    importance_weights: dict[str, float] = Field(default_factory=dict)
    """Per-type importance overrides (keyed by MemoryType.value).
    Merged on top of DEFAULT_IMPORTANCE_WEIGHTS — only listed types are overridden."""

    scale_by_confidence: bool = False
    """When True, importance is multiplied by the memory's confidence score.
    Keeps importance and confidence independent by default."""

    @field_validator("importance_weights")
    @classmethod
    def normalize_importance_weights(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for memory_type, weight in value.items():
            normalized_type = memory_type.strip().lower()
            if not normalized_type:
                raise ValueError("importance_weights keys cannot be blank")
            if weight < 0.0 or weight > 1.0:
                raise ValueError("importance_weights values must be between 0.0 and 1.0")
            normalized[normalized_type] = weight
        return normalized

    @property
    def is_noop(self) -> bool:
        """True when the rubric makes no filtering or ordering changes."""
        return self.min_salience == 0.0 and self.max_memories_per_episode is None

    def importance_for(self, memory_type: str | None, confidence: float) -> float:
        """Deterministic importance weight for a memory type, optionally scaled by confidence."""
        if memory_type is not None:
            override = self.importance_weights.get(memory_type.strip().lower())
            if override is not None:
                weight = override
            else:
                weight = DEFAULT_IMPORTANCE_WEIGHTS.get(memory_type.strip().lower(), 0.5)
        else:
            weight = 0.5
        if self.scale_by_confidence:
            weight = weight * max(0.0, min(1.0, confidence))
        return weight

    def recency_for(self, memory_valid_from: Any, reference_time: Any) -> float:
        """Exponential recency decay.

        memory_valid_from: datetime or None.
        reference_time:    datetime (episode reference time).

        Returns 1.0 when memory_valid_from >= reference_time (formation case)
        or when memory_valid_from is None.  Decays as age exceeds half_life_seconds.
        """
        if memory_valid_from is None:
            return 1.0
        # Compute age in seconds from the memory's valid_from to reference_time.
        # If the memory is newer than or equal to the reference time → age ≤ 0 → recency = 1.0.
        try:
            age_seconds = (reference_time - memory_valid_from).total_seconds()
        except (TypeError, AttributeError):
            return 1.0
        if age_seconds <= 0:
            return 1.0
        # Generative Agents: recency = exp(-λ · age), λ = ln(2) / half_life
        lam = math.log(2.0) / self.half_life_seconds
        return math.exp(-lam * age_seconds)


class DreamInstructionSet(BaseModel):
    name: str = Field(default="default", min_length=1)
    node_instructions: tuple[NodeInstruction, ...]
    relationship_instructions: tuple[RelationshipInstruction, ...]
    truth_goal: str = (
        "Prefer durable, support-useful facts. Preserve temporality and supersede older contradictory observations."
    )
    system_prompt: str = Field(default=DEFAULT_EXTRACTION_SYSTEM_PROMPT, min_length=1)
    max_predicate_words: int = Field(default=DEFAULT_MAX_PREDICATE_WORDS, ge=1)
    """Word ceiling for an extracted ``predicate`` — the truth-slot guard.

    The rendered contract asks for 1–3 words and the default tolerates 4 (see
    :data:`DEFAULT_MAX_PREDICATE_WORDS`).  Raise it for a domain whose relations
    are legitimately longer; the value is rendered into the prompt AND enforced
    by ``InstructionalExtractor._validate_memory``, so the model is never told
    one ceiling and judged by another."""
    salience_rubric: SalienceRubric = Field(default_factory=SalienceRubric)
    """WS-2: Extraction quality gate.  Default (min_salience=0.0, max_memories_per_episode=None)
    is a complete no-op — nothing is filtered or reordered, so existing tests are unaffected.
    Override to enforce per-episode caps and salience floors.  WS-3 Motives will set these
    per job by selecting or constructing a rubric at job-setup time."""

    @field_validator("system_prompt")
    @classmethod
    def normalize_system_prompt(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("system_prompt cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_instruction_set(self) -> DreamInstructionSet:
        if not self.node_instructions:
            raise ValueError("at least one node instruction is required")
        if not self.relationship_instructions:
            raise ValueError("at least one relationship instruction is required")
        # WS-27 T6: the tenant vocabulary extension contract's fail-fast half.
        # A relationship instruction's endpoint labels are validated against
        # this SAME instruction set's node labels at CONSTRUCTION time, not
        # only per-candidate at extraction time (``InstructionalExtractor
        # ._validate_memory``'s LABEL_ENDPOINT_MISMATCH/LABEL_NOT_ALLOWED,
        # which only ever sees what a model actually emitted).  A tenant who
        # mistypes an endpoint label while adding a relationship type
        # otherwise validates cleanly and then silently rejects every
        # candidate of that type at run time; failing here instead makes a
        # typo a config error, not a mystery gap in production.  ``"*"``
        # (open-predicate wildcard) is exempt by construction.
        allowed = self.allowed_labels
        for instruction in self.relationship_instructions:
            if instruction.source_label != "*" and instruction.source_label not in allowed:
                raise ValueError(
                    f"RelationshipInstruction {instruction.type!r} source_label "
                    f"{instruction.source_label!r} is not one of this instruction set's "
                    f"node labels {sorted(allowed)!r} (or '*')"
                )
            if instruction.target_label != "*" and instruction.target_label not in allowed:
                raise ValueError(
                    f"RelationshipInstruction {instruction.type!r} target_label "
                    f"{instruction.target_label!r} is not one of this instruction set's "
                    f"node labels {sorted(allowed)!r} (or '*')"
                )
        return self

    @property
    def allowed_labels(self) -> set[str]:
        return {instruction.label for instruction in self.node_instructions}

    @property
    def allowed_relationship_types(self) -> set[str]:
        return {instruction.type for instruction in self.relationship_instructions}

    def render_prompt(
        self,
        prompt_profile: DreamPromptProfile | None = None,
        actionability: ActionabilityPolicy | None = None,
    ) -> str:
        # ``NodeInstruction.query`` / ``RelationshipInstruction.query`` are
        # GOVERNANCE metadata (what selects into this label/type for validation
        # purposes) and are deliberately NOT rendered here.  MEASURED: a
        # ``RelationshipInstruction(type="GOVERNS", ...,
        # query="...State the GOVERNED thing as the object.")`` was written as a
        # selection criterion but the model read it as a dictated predicate and
        # emitted ``governs`` five times for one document's decision, flattening
        # five distinct table-row facts into one governance relation.  Only a
        # human-authored prompt (``DreamPromptProfile.prompt_text`` /
        # ``system_prompt`` — see ``ingest/kb_config.py``'s ``KB_EXTRACTION_PROMPT``
        # for the pilot's ontology) should ever tell the model how to use a
        # label or type; this method renders only the CLOSED VOCABULARY those
        # labels/types form and the STRUCTURAL facts (endpoints, cardinality)
        # validation actually enforces, both of which must stay byte-accurate
        # to ``self`` regardless of what any authored prompt says.
        node_lines = [f"- {instruction.label}" for instruction in self.node_instructions]
        relationship_lines = [
            (
                f"- {instruction.type} ({instruction.source_label} -> {instruction.target_label}): "
                f"Temporal rule: {instruction.temporal_semantics} "
                f"Truth cardinality: {instruction.cardinality.value}."
            )
            for instruction in self.relationship_instructions
        ]
        lines = [
            f"Dream instruction set: {self.name}",
            f"Truth goal: {self.truth_goal}",
        ]
        if prompt_profile is not None:
            lines.append(prompt_profile.render_prompt())
        # The closed vocabularies are restated as flat enumerations here,
        # independent of the bare label/type lists above.  Every item below is
        # enforced by InstructionalExtractor._validate_memory; a violation
        # costs the model that memory, which is worth saying out loud.
        label_names = ", ".join(sorted(self.allowed_labels))
        type_names = ", ".join(instruction.type for instruction in self.relationship_instructions)
        contract_lines = [
            "Schema contract (a memory that breaks any of these is discarded):",
            (
                f"- subject_label and object_label must be exactly one of: {label_names}. "
                "Never invent a label. What kind of thing a node is belongs in its "
                "properties, never in its label."
            ),
            f"- relationship_type must be exactly one of: {type_names}.",
            # The predicate contract.  Without it the ONLY mention of `predicate`
            # in this prompt was the field list, so the model invented its own
            # granularity and folded the object into the relation — which moves
            # every restatement to a different truth slot and silently kills
            # supersession, reinforcement, and rollup clustering.
            (
                "- predicate is a SHORT VERB PHRASE naming the RELATION ONLY: target "
                f"1-3 words, lowercase, at most {self.max_predicate_words}. subject + "
                "predicate IS the truth slot this graph supersedes and reinforces on, so "
                "the same fact restated later must produce the SAME predicate."
            ),
            (
                "- predicate must NOT contain the object, must NOT contain a noun phrase "
                "naming a technology, system, document, or value, and must NOT run past "
                "'to' into another verb. Everything after the relation verb belongs in "
                "object. A final preposition the relation genuinely needs is fine "
                "('resides in', 'publishes to')."
            ),
            ('- correct:   predicate="decided", object="use DynamoDB for the reservation ledger"'),
            '- incorrect: predicate="decided to use DynamoDB", object="DynamoDB"',
            (
                # MEASURED: the per-node property whitelist used to be rendered
                # here as "Allowed properties: kind, role, ...", generated
                # straight from ``NodeInstruction.properties``.  That whitelist
                # is a validation-time GOVERNANCE list, not extraction guidance,
                # and is no longer rendered ([0025], sibling change to the
                # relationship-query removal above); the label/type-agnostic
                # rule below applies uniformly instead.  Node instructions with
                # ``strict_properties=False`` silently strip an unknown key
                # rather than reject the candidate, so omission costs nothing.
                "- subject_properties and object_properties capture attribute values "
                "the document states about that node (a status, a tier, an "
                "identifier, a region). Omit anything you are unsure of; never "
                "invent a property key. `description` is always allowed on any "
                "label and should be set for every entity: one sentence, specific "
                "enough to tell two same-named entities apart later."
            ),
            (
                # WS-27 T1: aliases are UNIVERSAL_NODE_PROPERTIES, same standing
                # as description, so this line is unconditional regardless of
                # which instruction sets set require_description/aliases.
                "- `aliases` is also always allowed on any label: a list of "
                "alternate names, abbreviations, or acronyms the document uses "
                'for the SAME entity (e.g. ["GCX"] for "guest content '
                'experience"). Set it whenever the document uses more than one '
                "surface form for one entity; omit it otherwise."
            ),
            (
                # WS-27 T4: the generic, label-agnostic form of the Concept
                # catch-all guard.  Any instruction set with a general/catch-all
                # label benefits from this even without repeating the label's
                # name here — the closed label list above already tells the
                # model which labels exist; this tells it how to CHOOSE among
                # them.
                "- Prefer the MOST SPECIFIC label that genuinely fits an entity. "
                "A general or catch-all label is a LAST RESORT for something no "
                "more specific label describes, never a default reached for "
                "convenience — a vocabulary where one general label absorbs most "
                "of the corpus has failed to type its entities."
            ),
            (f"- Never set these reserved property keys: {', '.join(RESERVED_NODE_PROPERTIES)}."),
        ]
        confidence_floors = [
            f"{instruction.type} >= {instruction.min_confidence}"
            for instruction in self.relationship_instructions
            if instruction.min_confidence > 0
        ]
        if confidence_floors:
            contract_lines.append(f"- Minimum confidence per relationship type: {'; '.join(confidence_floors)}.")
        # WS-24: the actionability contract.  Rendered only when the gate is on,
        # so a deployment that disables it gets a byte-identical legacy prompt.
        # Few-shot, because the earlier predicate contract in this repository
        # moved a comparable failure from 8 stray labels to 0 by showing rather
        # than telling.
        if actionability is not None and actionability.enabled:
            contract_lines.extend(
                [
                    (
                        "- saves_step is REQUIRED on every memory. Memory is a shortcut for "
                        "the agent's NEXT ACTION, not a description of the world. Before "
                        "emitting a memory, answer: WHAT STEP DOES KNOWING THIS LET THE "
                        "AGENT SKIP? Write that answer, in one short sentence, as "
                        "saves_step. If you cannot name a step, the fact is not a memory."
                    ),
                    (
                        '- KEEP: subject="gh CLI", predicate="is authenticated via", '
                        'object="the workstation keychain", saves_step="the agent can call '
                        'gh directly instead of running an auth flow first".'
                    ),
                    (
                        '- KEEP: subject="Special Offers content", predicate="lives at", '
                        'object="content/special-offers", saves_step="the agent goes '
                        'straight to that path instead of searching the repository".'
                    ),
                    (
                        '- KEEP: subject="virtual keys", predicate="start with", '
                        'object="the sk- prefix", saves_step="the agent recognises a virtual '
                        'key on sight instead of calling the key API to classify it".'
                    ),
                    (
                        '- KEEP: subject="production gateway", predicate="accepts", '
                        'object="service-account and agent keys only", saves_step="the agent '
                        'stops sending user keys to prod and skips a round of 401 debugging".'
                    ),
                    (
                        '- DROP: subject="Special Offers", predicate="has field", '
                        'object="data.descriptions.offer_intro". No step is saved — the agent '
                        "must open the schema anyway, and a renamed field makes the memory "
                        "actively misleading. Do not emit it."
                    ),
                    (
                        '- EXCEPTION, keep: subject="the authorship frontmatter", '
                        'predicate="gates", object="whether a page is human-certified", '
                        'saves_step="the agent checks one field before trusting a page '
                        'instead of hunting for provenance". A field that CONTROLS BEHAVIOUR '
                        "is a rule about what to do, not an inventory entry."
                    ),
                    (
                        "- Never inventory a data shape: fields, properties, columns, "
                        "attributes, and schema listings are not memories unless they gate, "
                        "control, or require an action."
                    ),
                ]
            )
            if actionability.allow_abstain:
                contract_lines.append(
                    "- If a fact is worth noticing but you cannot name the step it saves, "
                    "OR none of the relationship types above genuinely fit, emit it with "
                    '"abstain": true and a short "abstain_reason" INSTEAD of forcing a '
                    "type. Abstaining is a correct answer and is always better than "
                    "picking the closest type; abstained candidates are quarantined for "
                    "operator review, never discarded."
                )
        json_fields = (
            # [0025] Cookbook shape: entities are their own list (name, type,
            # description, optional properties); relations may only name an
            # entity from that list. A relation naming anything else is
            # rejected, never stored -- the structural guard against
            # phrase-nodes that label-checking alone cannot give.
            "Return exactly one JSON object with an entities array and a relations array. "
            "Each entity must include name, type, description, and optional properties. "
            "Each relation must include source, predicate, target, relationship_type, "
            "confidence, valid_from, "
            + ("saves_step, " if actionability is not None and actionability.enabled else "")
            + "and optional valid_to, instruction_id, source_text, claim_mode, and metadata. "
            "source and target must each be a name from the entities array -- naming anything "
            "else drops the relation. An entity's properties become that node's "
            "subject_properties / object_properties wherever it appears as source or target; "
            "set them once, on the entity, never restated per relation. "
            "claim_mode must be one of descriptive_assertion, requirement, preference, "
            "directive, correction, or report_of_behavior."
        )
        lines.extend(
            [
                "Create only these node labels:",
                *node_lines,
                "Create only these relationship types:",
                *relationship_lines,
                *contract_lines,
                json_fields,
                "Do not return prose, markdown, or fields outside the JSON object.",
            ]
        )
        return "\n".join(lines)
