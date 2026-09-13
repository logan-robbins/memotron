from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import ValidationError

from memotron.config import (
    RESERVED_NODE_PROPERTIES,
    UNIVERSAL_NODE_PROPERTIES,
    ActionabilityPolicy,
    DreamInstructionSet,
    DreamPromptProfile,
    RelationshipInstruction,
    actionability_violation,
    default_claim_mode_for_memory_type,
    predicate_shape_violation,
    validate_claim_mode_for_memory_type,
)
from memotron.gateway import (
    DEFAULT_GATEWAY_BASE_URL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
    GatewayRequestError,
    GatewayRetryPolicy,
    OpenAICompatibleChatTransport,
)
from memotron.models import DreamContextMemory, Episode, EpisodeType, ExtractedMemory, MemoryScope

_log = logging.getLogger(__name__)


def parse_datetime(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    return datetime.fromisoformat(normalized)


@dataclass(frozen=True)
class ExtractionRequest:
    episode: Episode
    instructions: DreamInstructionSet
    graph_context: list[DreamContextMemory]
    prompt_profile: DreamPromptProfile | None
    prompt: str
    actionability: ActionabilityPolicy | None = None
    """WS-24: the effective actionability gate for this episode.  Rendered into
    ``prompt`` and enforced per candidate; ``None`` (or a disabled policy)
    leaves both the prompt and the validation path byte-identical to pre-gate."""
    entity_inventory: tuple[dict[str, str], ...] = ()
    """WS-17 T16b: canonical entity names (+ kinds) for the episode's scope,
    rendered into the prompt's ENTITY INVENTORY block.  Empty when entity
    resolution is disabled or the scope has no entities yet."""
    system_prompt_override: str | None = None
    """WS-21 T26: per-Motive extraction system prompt.  When set, LLM transports
    send this as the system message INSTEAD of ``instructions.system_prompt``;
    None (default) keeps the instruction-set system prompt byte-for-byte."""


def effective_system_prompt(request: ExtractionRequest) -> str:
    """The system message an LLM transport must send for *request*.

    WS-21 T26: the Motive-resolved ``system_prompt_override`` wins when set;
    otherwise the instruction set's ``system_prompt`` applies unchanged.  One
    function so every transport resolves the override identically.
    """
    if request.system_prompt_override is not None:
        return request.system_prompt_override
    return request.instructions.system_prompt


def strip_markdown_fences(text: str) -> str:
    """Remove a wrapping ``` fence if present (a transport formatting artifact).

    Gateways that proxy Claude models (e.g. LiteLLM with ``drop_params``)
    silently discard ``response_format={"type":"json_object"}``, so the model
    answers with fenced JSON even when asked for raw JSON, so every transport
    that parses model output must tolerate that.
    """
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = lines[1:] if len(lines) > 1 else lines
        if inner and inner[-1].strip() == "```":
            inner = inner[:-1]
        return "\n".join(inner).strip()
    return stripped


def parse_first_json_object(text: str, *, source: str) -> dict:
    """Return the first complete JSON object/array in *text* as a dict.

    Tolerates models that append prose after the JSON despite instructions.
    A bare list is wrapped as ``{"memories": [...]}`` so both shapes validate.
    """
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char in ("{", "["):
            try:
                obj, _ = decoder.raw_decode(text, index)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
            return _wrap_bare_array(obj)
    raise ValueError(f"{source} content did not contain a valid JSON object")


def _wrap_bare_array(items: Any) -> dict:
    """Name a bare top-level array by its CONTENT, not by assumption.

    A bare array used to be wrapped unconditionally as ``{"memories": [...]}``.
    Once the prompt began asking for an ``entities``/``relations`` envelope, a
    model that answered with a bare array of ENTITIES had each entity validated
    as if it were a relation, failing with five missing fields (subject,
    predicate, object, relationship_type, confidence).  Measured on the
    jedai-gateway ingest: 41 such rejections across 3 of 119 episodes -- 18, 13
    and 10 -- while the other 116 episodes returned the envelope correctly and
    materialized normally.

    Entity objects are recognisable: they carry ``name`` and no relation
    endpoints.  Naming them correctly costs nothing and stops a well-formed
    roster being reported as malformed relations.  An array carrying real
    relation fields keeps the historical ``memories`` reading exactly.
    """
    entries = [item for item in items if isinstance(item, dict)]
    if entries and all(
        "name" in entry and not ({"subject", "predicate", "object", "source", "target"} & entry.keys())
        for entry in entries
    ):
        return {"entities": list(items), "relations": []}
    return {"memories": items}


@dataclass(frozen=True)
class CookbookEnvelope:
    """[0025] Cookbook-shaped raw transport output: entities extracted as
    their own roster (``name``, ``type``, ``description``, optional
    ``properties``), then relations (``source``, ``predicate``, ``target``,
    plus the usual per-memory fields) that may only NAME an entity in that
    roster.  This is the structural guarantee against phrase-nodes that a
    closed label set alone does not give: a relation cannot mint a node that
    was never itself extracted and described.

    A transport returns this INSTEAD OF a flat ``list[dict[str, Any]]`` when
    the parsed JSON used the ``entities``/``relations`` keys.  The legacy flat
    ``memories`` shape (each entry already self-contained: ``subject``,
    ``subject_label``, ``subject_properties``, ...) remains fully supported
    and decodes to a plain list exactly as before -- see
    :func:`decode_extraction_envelope`.
    """

    entities: tuple[dict[str, Any], ...]
    relations: tuple[dict[str, Any], ...]


def decode_extraction_envelope(
    payload: dict[str, Any], *, source: str, require_memories_key: bool = False
) -> list[dict[str, Any]] | CookbookEnvelope:
    """Interpret one parsed JSON extraction response, envelope shape first.

    Shared by every transport so the two accepted envelope shapes are decoded
    identically everywhere: the cookbook shape (``entities`` + ``relations``,
    [0025], preferred and what the rendered prompt now asks for) and the
    legacy flat ``memories`` list (unchanged, still what
    ``RuleBasedExtractionTransport``'s ``EpisodeType.JSON`` test fixtures and
    any tenant prompt override predating [0025] produce). Exactly one shape
    may be present; a response mixing both is a malformed envelope.

    ``require_memories_key`` preserves each caller's PRE-EXISTING policy on a
    response with NEITHER shape: ``RuleBasedExtractionTransport`` always
    treated an absent ``memories`` key as zero candidates (episode bodies are
    test-authored and ``{}`` legitimately means "nothing to extract"), while
    ``OpenAICompatibleExtractionTransport`` always treated it as a malformed,
    fatal envelope (a real model that returns neither shape said nothing
    trustworthy).  Both behaviors predate this function and are preserved
    exactly, not merged.
    """
    has_cookbook = "entities" in payload or "relations" in payload
    has_memories = "memories" in payload
    if has_cookbook and has_memories:
        raise ValueError(
            f"{source} content mixed the 'entities'/'relations' cookbook shape with the legacy 'memories' shape"
        )
    if has_cookbook:
        entities = payload.get("entities", [])
        relations = payload.get("relations", [])
        if not isinstance(entities, list):
            raise ValueError(f"{source} 'entities' must be a list")
        if not isinstance(relations, list):
            raise ValueError(f"{source} 'relations' must be a list")
        for entity in entities:
            if not isinstance(entity, dict):
                raise ValueError(f"{source} entities entries must be objects")
        for relation in relations:
            if not isinstance(relation, dict):
                raise ValueError(f"{source} relations entries must be objects")
        return CookbookEnvelope(entities=tuple(entities), relations=tuple(relations))
    if require_memories_key and not has_memories:
        raise ValueError(f"{source} content must contain a memories list")
    memories = payload.get("memories", [])
    if not isinstance(memories, list):
        raise ValueError(f"{source} content must contain a memories list")
    for memory in memories:
        if not isinstance(memory, dict):
            raise ValueError(f"{source} memory entries must be objects")
    return memories


class ExtractionTransport(Protocol):
    async def extract_memories(self, request: ExtractionRequest) -> list[dict[str, Any]] | CookbookEnvelope:
        """Return raw candidates that will be strictly validated.

        Either a flat list of self-contained memory dicts (legacy shape) or a
        :class:`CookbookEnvelope` ([0025], the cookbook shape the rendered
        prompt now asks for) -- see :func:`decode_extraction_envelope`.
        """


class CandidateViolation(StrEnum):
    """The specific contract one candidate broke ([0024]).

    A bounded machine vocabulary so a ``CANDIDATE_SCHEMA_REJECTED`` receipt can
    name the violation in a form that survives crypto-shred redaction (the
    detailed message may carry model-authored text and is diverted into the
    sealed payload for a protected scope; this code never is).
    """

    MODEL_VALIDATION = "model_validation_failed"
    SCOPE_FORMAT_INVALID = "scope_format_invalid"
    SCOPE_MISMATCH = "scope_mismatch"
    RELATION_ENTITY_NOT_EXTRACTED = "relation_entity_not_extracted"
    """[0025] A cookbook-shaped ``relations[]`` entry named a ``source``/
    ``target`` absent from that same response's ``entities[]`` roster.
    Structural, like ``LABEL_NOT_ALLOWED`` -- there is no valid row to store,
    because the entity the relation would attach to was never itself
    extracted -- so this is a REJECTION, never a quarantine."""
    LABEL_NOT_ALLOWED = "label_not_allowed"
    RELATIONSHIP_TYPE_NOT_ALLOWED = "relationship_type_not_allowed"
    LABEL_ENDPOINT_MISMATCH = "label_endpoint_mismatch"
    PREDICATE_NOT_A_VERB_PHRASE = "predicate_not_a_verb_phrase"
    PROPERTY_KEY_NOT_ALLOWED = "property_key_not_allowed"
    PROPERTY_KEY_RESERVED = "property_key_reserved"
    CONFIDENCE_BELOW_MINIMUM = "confidence_below_minimum"
    ENTITY_REF_UNPAIRED = "entity_ref_link_confidence_unpaired"
    ENTITY_REF_BLANK = "entity_ref_blank"
    MEMORY_TYPE_UNRESOLVED = "memory_type_unresolved"
    # WS-24 — the actionability gate.  These three do not end a candidate's
    # life: they route it to quarantine (see QUARANTINE_VIOLATIONS), where it
    # stays inspectable and promotable instead of being dropped.
    ACTIONABILITY_JUSTIFICATION_MISSING = "actionability_justification_missing"
    ACTIONABILITY_JUSTIFICATION_NOT_SUBSTANTIVE = "actionability_justification_not_substantive"
    ACTIONABILITY_INVENTORY_ENTRY = "actionability_inventory_entry"
    ACTIONABILITY_ABSTAINED = "actionability_abstained"
    # WS-27 T1/T2 — ontology-completeness governance judgements.  Like the
    # WS-24 trio above, a structurally valid entity that is merely
    # INCOMPLETE (no description where the label demands one; missing a
    # required property key) is quarantined, never rejected: a later policy
    # change or operator correction can still re-govern it without
    # re-extraction.
    ENTITY_DESCRIPTION_MISSING = "entity_description_missing"
    """WS-27 T1: a ``require_description`` label's entity carried no non-blank
    ``description``.  ``description`` is already a
    :data:`~memotron.config.UNIVERSAL_NODE_PROPERTIES` key on every label,
    so nothing about the candidate's SHAPE is wrong — only its
    completeness."""
    REQUIRED_PROPERTY_MISSING = "required_property_missing"
    """WS-27 T2: a label's ``NodeInstruction.required_properties`` key was
    absent or blank (e.g. a ``Credential`` entity with no ``reference``)."""


QUARANTINE_VIOLATIONS: frozenset[CandidateViolation] = frozenset(
    {
        CandidateViolation.ACTIONABILITY_JUSTIFICATION_MISSING,
        CandidateViolation.ACTIONABILITY_JUSTIFICATION_NOT_SUBSTANTIVE,
        CandidateViolation.ACTIONABILITY_INVENTORY_ENTRY,
        CandidateViolation.ACTIONABILITY_ABSTAINED,
        # Extraction / knowledge-graph pipeline restructure: these four used to
        # be REJECTIONS (dropped, never stored) even though the candidate was
        # already a structurally valid entity/relation — a closed-vocabulary
        # label, a legal endpoint pairing, a parseable envelope.  Only the
        # judgement about whether the row is a GOOD memory failed: confidence
        # below the configured floor, a property key outside the instruction
        # set's whitelist, or a predicate shaped so it would corrupt its own
        # truth key.  None of that is a reason to make the row unrecoverable —
        # quarantine keeps it stored, receipted, and promotable exactly like an
        # actionability failure, and a later policy change (a lowered
        # min_confidence, a widened property whitelist) can re-govern it into
        # the graph without re-running extraction.
        CandidateViolation.CONFIDENCE_BELOW_MINIMUM,
        CandidateViolation.PROPERTY_KEY_NOT_ALLOWED,
        CandidateViolation.PROPERTY_KEY_RESERVED,
        CandidateViolation.PREDICATE_NOT_A_VERB_PHRASE,
        # WS-27 T1/T2: see the two violations' own docstrings above.
        CandidateViolation.ENTITY_DESCRIPTION_MISSING,
        CandidateViolation.REQUIRED_PROPERTY_MISSING,
    }
)
"""Violations whose correct disposition is quarantine, not rejection.

A REJECTION means the candidate is not even a structurally storable
entity/relation — malformed envelope, unresolvable scope, a label or
relationship type outside the closed vocabulary, an endpoint mismatch, an
unpaired entity-ref.  Nothing about it can be stored, because there is no valid
row shape to store.  Everything in this set, by contrast, IS a structurally
valid candidate; only a stage-3 governance judgement disqualifies it, and that
judgement is revisable.  Splitting the two here, once, is what keeps every
downstream path (receipts, counts, the health abstain rate, re-govern) from
having to re-derive the distinction."""


class CandidateSchemaError(ValueError):
    """One candidate violated the instruction set's schema contract.

    A ``ValueError`` subclass so the operator-facing single-candidate path
    (:meth:`InstructionalExtractor.validate_memory`, used by
    ``Memotron.add_memory``) keeps failing fast byte-for-byte as before,
    while batch extraction can catch *precisely this class* and drop the one
    offending candidate instead of the whole episode's batch.  Anything else
    out of validation stays fatal.
    """

    def __init__(
        self,
        message: str,
        *,
        violation: CandidateViolation,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.violation = violation
        self.field = field


@dataclass(frozen=True)
class CandidateRejection:
    """One candidate dropped by per-candidate schema validation ([0024]).

    Carries everything the engine needs to receipt the drop: the raw candidate
    (for its digest), the machine violation code, the offending field, and the
    detailed human reason.  Rejections are *returned*, never swallowed — a
    caller that ignores them breaks the "no candidate is silently dropped"
    guarantee.
    """

    index: int
    raw: dict[str, Any]
    violation: CandidateViolation
    field: str | None
    reason: str


@dataclass(frozen=True)
class CandidateAbstention:
    """WS-24: one candidate the actionability gate routed to quarantine.

    Distinct from :class:`CandidateRejection` because the disposition is
    different in kind: a rejection is a malformed candidate with nothing worth
    keeping, an abstention is a well-formed candidate that is simply not a
    memory.  The raw candidate travels intact so the quarantine store can hold
    it, digest it, and hand it back to an operator who decides otherwise.
    """

    index: int
    raw: dict[str, Any]
    violation: CandidateViolation
    reason: str
    saves_step: str | None = None
    subject: str = ""
    predicate: str = ""
    object: str = ""
    proposed_relationship_type: str | None = None


@dataclass(frozen=True)
class ExtractionResult:
    """The validated candidates of one episode plus every per-candidate drop.

    Returning all three is what makes candidate disposition non-fatal AND
    non-lossy: one bad candidate can no longer discard its siblings, an
    unactionable one is retained instead of vanishing, and the reason for
    either reaches the receipt ledger.
    """

    memories: list[ExtractedMemory] = field(default_factory=list)
    rejections: tuple[CandidateRejection, ...] = ()
    quarantined: tuple[CandidateAbstention, ...] = ()


def _label_admits(declared: str, actual: str) -> bool:
    """``"*"`` on an open instruction admits any label already validated closed."""
    return declared == "*" or declared == actual


def match_relationship_instruction(
    *,
    instructions: DreamInstructionSet,
    relationship_type: str,
    subject_label: str,
    object_label: str,
) -> RelationshipInstruction | None:
    """Exact-typed instruction first, then an open-predicate instruction.

    Exact match wins so a declared structural relation keeps its declared
    cardinality and memory type; the open instruction is the catch-all that
    makes the relation vocabulary open without loosening the entity vocabulary.
    """
    for instruction in instructions.relationship_instructions:
        if instruction.type == relationship_type and not instruction.open_predicate:
            return instruction
    for instruction in instructions.relationship_instructions:
        if (
            instruction.open_predicate
            and _label_admits(instruction.source_label, subject_label)
            and _label_admits(instruction.target_label, object_label)
        ):
            return instruction
    return None


class InstructionalExtractor:
    """Builds extraction prompts, calls one transport, and validates memory output."""

    # One definition, shared with the rendered prompt that tells the model about it.
    _reserved_node_properties = frozenset(RESERVED_NODE_PROPERTIES)
    _universal_node_properties = frozenset(UNIVERSAL_NODE_PROPERTIES)

    def __init__(self, transport: ExtractionTransport | None = None) -> None:
        self._transport = transport or RuleBasedExtractionTransport()

    async def extract(
        self,
        *,
        episode: Episode,
        instructions: DreamInstructionSet,
        graph_context: list[DreamContextMemory] | None = None,
        prompt_profile: DreamPromptProfile | None = None,
        entity_inventory: list[dict[str, str]] | None = None,
        system_prompt_override: str | None = None,
        actionability: ActionabilityPolicy | None = None,
    ) -> ExtractionResult:
        """Extract one episode's candidates, validating each one INDEPENDENTLY.

        The fatal/non-fatal boundary lives here and nowhere else:

        * **Fatal** — anything raised by the transport.  A malformed response
          *envelope* (not valid JSON, neither a ``memories`` list nor an
          ``entities``/``relations`` pair, entries that are not objects) is a
          transport/contract failure: nothing about the episode can be
          trusted, so it propagates and the run aborts.
        * **Non-fatal, dropped** — a :class:`CandidateSchemaError` from
          validating a single candidate, OR ([0025]) a cookbook-shaped
          relation whose ``source``/``target`` names an entity absent from
          that same response's roster (:meth:`_expand_cookbook_envelope`,
          which runs before any candidate reaches ``_validate_memory`` at
          all).  Either way the candidate is dropped and reported in
          ``ExtractionResult.rejections``; every sibling candidate from the
          same episode still materializes.
        * **Non-fatal, retained** (WS-24) — an actionability failure or an
          explicit abstain.  The candidate is well-formed but is not a memory,
          so it is reported in ``ExtractionResult.quarantined`` and the engine
          files it in the quarantine store instead of the graph.

        Real models invent a label, a property key, or a relationship type from
        time to time.  Aborting a 1,000-episode ingest over one stray property
        is not robustness, it is a fuse in the wrong place.
        """
        context = graph_context or []
        inventory = tuple(entity_inventory or ())
        request = ExtractionRequest(
            episode=episode,
            instructions=instructions,
            graph_context=context,
            prompt_profile=prompt_profile,
            prompt=self.render_episode_prompt(
                episode=episode,
                instructions=instructions,
                graph_context=context,
                prompt_profile=prompt_profile,
                entity_inventory=entity_inventory,
                actionability=actionability,
            ),
            actionability=actionability,
            entity_inventory=inventory,
            system_prompt_override=system_prompt_override,
        )
        # Envelope-level failures raise out of here and stay fatal.
        raw_result = await self._transport.extract_memories(request)
        validated: list[ExtractedMemory] = []
        quarantined: list[CandidateAbstention] = []
        if isinstance(raw_result, CookbookEnvelope):
            # [0025] Resolve every relation against ITS OWN response's entity
            # roster before any of them reach _validate_memory.  A relation
            # naming an entity absent from that roster is rejected here,
            # indexed by its position in ``relations`` -- it never competes
            # for an index with the resolved candidates below.
            raw_memories, rejections = self._expand_cookbook_envelope(raw_result)
        else:
            raw_memories, rejections = raw_result, []
        for index, raw in enumerate(raw_memories):
            memory, rejection, abstention = self._process_candidate(
                raw,
                index=index,
                episode=episode,
                instructions=instructions,
                actionability=actionability,
            )
            if memory is not None:
                validated.append(memory)
            if rejection is not None:
                rejections.append(rejection)
            if abstention is not None:
                quarantined.append(abstention)
        return ExtractionResult(
            memories=validated,
            rejections=tuple(rejections),
            quarantined=tuple(quarantined),
        )

    @staticmethod
    def _expand_cookbook_envelope(
        envelope: CookbookEnvelope,
    ) -> tuple[list[dict[str, Any]], list[CandidateRejection]]:
        """[0025] Resolve ``relations[]`` against ``entities[]``, one response.

        A resolved relation is expanded into the flat per-candidate shape
        ``_validate_memory`` already validates: the entity's ``type`` becomes
        ``subject_label``/``object_label`` and its ``description`` (plus any
        ``properties``) is merged into ``subject_properties``/
        ``object_properties`` — so every existing rule (label closure,
        predicate shape, property whitelist, confidence floor, entity-ref
        pairing) applies completely unchanged past this point.  An
        unresolved relation — its ``source`` or ``target`` names no entity in
        THIS SAME response's roster — is rejected here, never handed to
        ``_validate_memory`` at all: there is no label to validate, because
        the node it would attach to was never extracted.
        """
        roster: dict[str, dict[str, Any]] = {}
        for entity in envelope.entities:
            name = entity.get("name")
            if isinstance(name, str) and name.strip():
                roster[name.strip()] = entity

        def merged_properties(entity: dict[str, Any]) -> dict[str, Any]:
            properties = entity.get("properties")
            merged = dict(properties) if isinstance(properties, dict) else {}
            description = entity.get("description")
            if isinstance(description, str) and description.strip():
                merged.setdefault("description", description.strip())
            return merged

        resolved: list[dict[str, Any]] = []
        rejections: list[CandidateRejection] = []
        for position, relation in enumerate(envelope.relations):
            source_name = relation.get("source")
            target_name = relation.get("target")
            source_entity = roster.get(source_name.strip()) if isinstance(source_name, str) else None
            target_entity = roster.get(target_name.strip()) if isinstance(target_name, str) else None
            if source_entity is None or target_entity is None:
                unresolved_field, unresolved_name = (
                    ("source", source_name) if source_entity is None else ("target", target_name)
                )
                rejections.append(
                    CandidateRejection(
                        index=position,
                        raw=dict(relation),
                        violation=CandidateViolation.RELATION_ENTITY_NOT_EXTRACTED,
                        field=unresolved_field,
                        reason=(
                            f"relation names {unresolved_name!r} as {unresolved_field}, "
                            "which does not appear in this response's entities[] roster"
                        ),
                    )
                )
                continue
            flat = dict(relation)
            flat.pop("source", None)
            flat.pop("target", None)
            flat["subject"] = source_name
            flat["object"] = target_name
            flat["subject_label"] = source_entity.get("type")
            flat["object_label"] = target_entity.get("type")
            flat["subject_properties"] = {
                **merged_properties(source_entity),
                **(flat.get("subject_properties") or {}),
            }
            flat["object_properties"] = {
                **merged_properties(target_entity),
                **(flat.get("object_properties") or {}),
            }
            resolved.append(flat)
        return resolved, rejections

    def _process_candidate(
        self,
        raw: dict[str, Any],
        *,
        index: int,
        episode: Episode,
        instructions: DreamInstructionSet,
        actionability: ActionabilityPolicy | None,
    ) -> tuple[ExtractedMemory | None, CandidateRejection | None, CandidateAbstention | None]:
        """Validate/govern ONE raw candidate — the unit shared by :meth:`extract`
        (looping over a fresh transport response) and :meth:`regovern_candidate`
        (replaying a single stored raw candidate with no transport call at all).

        Exactly one of the three return slots is populated:
        ``(memory, None, None)`` on a fully governed pass, ``(None, rejection,
        None)`` on a stage-1 structural failure (never storable), or ``(None,
        None, abstention)`` on a stage-3 governance quarantine (well-formed,
        stored, marked).  Keeping this as one function is what makes re-govern
        produce IDENTICAL verdicts to the original extraction pass for the same
        input — there is no second implementation of the decision to drift.
        """
        abstain_reason = self._abstain_reason(raw, actionability)
        if abstain_reason is not None:
            return (
                None,
                None,
                self._abstention(
                    index=index,
                    raw=raw,
                    violation=CandidateViolation.ACTIONABILITY_ABSTAINED,
                    reason=abstain_reason,
                ),
            )
        try:
            memory = self._validate_memory(raw, episode, instructions, actionability=actionability)
        except CandidateSchemaError as exc:
            if exc.violation in QUARANTINE_VIOLATIONS:
                _log.info(
                    "episode %s: candidate %d quarantined (%s); it is retained and promotable, not discarded",
                    episode.uuid,
                    index,
                    exc.violation.value,
                )
                return (
                    None,
                    None,
                    self._abstention(
                        index=index,
                        raw=raw,
                        violation=exc.violation,
                        reason=str(exc),
                    ),
                )
            _log.warning(
                "episode %s: candidate %d rejected (%s); its siblings are unaffected",
                episode.uuid,
                index,
                exc.violation.value,
            )
            return (
                None,
                CandidateRejection(
                    index=index,
                    raw=dict(raw),
                    violation=exc.violation,
                    field=exc.field,
                    reason=str(exc),
                ),
                None,
            )
        return memory, None, None

    def regovern_candidate(
        self,
        raw: dict[str, Any],
        *,
        episode: Episode,
        instructions: DreamInstructionSet,
        actionability: ActionabilityPolicy | None = None,
    ) -> tuple[ExtractedMemory | None, CandidateRejection | None, CandidateAbstention | None]:
        """Re-run stage-1 structural validation + stage-3 governance over one
        ALREADY-STORED raw candidate — no transport call, no LLM, no re-extraction.

        This is the unit re-govern is built on: ``raw`` is the exact JSON a
        transport produced, persisted verbatim in the raw/quarantine store at
        extraction time.  Passing it back through the same
        :meth:`_process_candidate` logic used at extraction time — now under
        (possibly changed) ``instructions``/``actionability`` policy — yields the
        SAME verdict shape (memory / rejection / abstention) a fresh extraction
        would have produced, so changing a threshold and re-governing an already
        stored raw graph is indistinguishable, from the caller's side, from
        having extracted it correctly the first time.
        """
        return self._process_candidate(
            raw,
            index=0,
            episode=episode,
            instructions=instructions,
            actionability=actionability,
        )

    @staticmethod
    def _abstain_reason(raw: dict[str, Any], actionability: ActionabilityPolicy | None) -> str | None:
        """WS-24: the extractor's explicit "none of these / not actionable" outcome.

        Checked BEFORE schema validation on purpose: an abstaining candidate is
        exactly the one that could not pick a relationship type, so demanding it
        pass the type contract first would make abstention impossible — which is
        the failure the abstain path exists to remove.
        """
        if raw.get("abstain") is not True:
            return None
        if actionability is None or not actionability.enabled or not actionability.allow_abstain:
            return None
        stated = str(raw.get("abstain_reason") or "").strip()
        return f"extractor abstained: {stated}" if stated else "extractor abstained"

    @staticmethod
    def _abstention(
        *,
        index: int,
        raw: dict[str, Any],
        violation: CandidateViolation,
        reason: str,
    ) -> CandidateAbstention:
        def text(key: str) -> str:
            value = raw.get(key)
            return value.strip() if isinstance(value, str) else ""

        saves_step = text("saves_step")
        relationship_type = text("relationship_type")
        return CandidateAbstention(
            index=index,
            raw=dict(raw),
            violation=violation,
            reason=reason,
            saves_step=saves_step or None,
            subject=text("subject"),
            predicate=text("predicate"),
            object=text("object"),
            proposed_relationship_type=relationship_type or None,
        )

    def validate_memory(
        self,
        raw: dict[str, Any],
        *,
        episode: Episode,
        instructions: DreamInstructionSet,
        actionability: ActionabilityPolicy | None = None,
    ) -> ExtractedMemory:
        """Validate ONE explicit candidate, raising :class:`CandidateSchemaError`.

        The operator write path (``Memotron.add_memory``) — a single
        deliberate fact, not a model-produced batch.  There is no sibling to
        protect and nobody to receipt the drop for, so a contract violation here
        stays a hard error the caller sees immediately.  Batch extraction uses
        :meth:`extract`, which drops, quarantines, and receipts instead.

        ``actionability`` defaults to ``None`` here — an operator writing one
        fact by hand HAS decided it is worth remembering, and that decision is
        the thing the gate exists to approximate for a model.  Pass a policy
        explicitly to hold an operator write to the same contract.
        """
        return self._validate_memory(raw, episode, instructions, actionability=actionability)

    def render_episode_prompt(
        self,
        *,
        episode: Episode,
        instructions: DreamInstructionSet,
        graph_context: list[DreamContextMemory] | None = None,
        prompt_profile: DreamPromptProfile | None = None,
        entity_inventory: list[dict[str, str]] | None = None,
        actionability: ActionabilityPolicy | None = None,
    ) -> str:
        episode_context = {
            "episode_uuid": episode.uuid,
            "episode_name": episode.name,
            "episode_source": episode.source.value,
            "source_description": episode.source_description,
            "scope": episode.scope.key,
            "reference_time": episode.reference_time.isoformat(),
            "metadata": episode.metadata,
            "body": episode.body,
        }
        graph_context_payload = [memory.model_dump(mode="json") for memory in (graph_context or [])]
        sections = [
            instructions.render_prompt(prompt_profile=prompt_profile, actionability=actionability),
            "Existing graph context:",
            json.dumps(graph_context_payload, sort_keys=True),
        ]
        # WS-17 T16b: compact scope-wide canonical entity inventory (names +
        # kinds only) so the extractor can resolve definite references and
        # abbreviations against entities the graph already knows.  Rendered
        # only when the engine supplies a non-empty inventory — prompts are
        # byte-identical to the legacy shape otherwise.
        if entity_inventory:
            sections.extend(
                [
                    "Entity inventory (canonical entity names already in this scope):",
                    json.dumps(list(entity_inventory), sort_keys=True),
                    (
                        "Entity resolution rules: when a mention (a definite reference, "
                        "abbreviation, or surface variant such as 'the gateway' for "
                        "'Jedai Gateway') refers to an inventory entity, keep the mention "
                        "text as subject/object and add subject_entity_ref plus "
                        "subject_link_confidence (or object_entity_ref plus "
                        "object_link_confidence) naming that inventory entity with your "
                        "confidence in [0,1] that they are the same real-world entity. "
                        "Emit entity_ref and link_confidence together or not at all. "
                        "Omit both for genuinely new entities. Never abbreviate or alter "
                        "identifier-like names (IDs, codes); distinct identifiers are "
                        "distinct entities."
                    ),
                ]
            )
        sections.extend(
            [
                "Episode context:",
                json.dumps(episode_context, sort_keys=True),
            ]
        )
        return "\n\n".join(sections)

    def _validate_memory(
        self,
        raw: dict[str, Any],
        episode: Episode,
        instructions: DreamInstructionSet,
        *,
        actionability: ActionabilityPolicy | None = None,
    ) -> ExtractedMemory:
        payload = dict(raw)
        # Never mutate the transport's raw candidate: non-strict property
        # pruning below deletes keys from the validated model's dicts, and the
        # rejection record has to be able to digest the candidate as delivered.
        for property_field in ("subject_properties", "object_properties"):
            value = payload.get(property_field)
            if isinstance(value, dict):
                payload[property_field] = dict(value)
        payload.setdefault("valid_from", episode.reference_time)
        payload.setdefault("scope", episode.scope)
        if isinstance(payload.get("scope"), str):
            payload["scope"] = self._parse_scope(payload["scope"])
        if isinstance(payload.get("valid_from"), str):
            payload["valid_from"] = parse_datetime(payload["valid_from"])
        if isinstance(payload.get("valid_to"), str):
            payload["valid_to"] = parse_datetime(payload["valid_to"])
        try:
            memory = ExtractedMemory.model_validate(payload)
        except ValidationError as exc:
            raise CandidateSchemaError(
                f"memory in episode {episode.uuid} failed validation: {exc}",
                violation=CandidateViolation.MODEL_VALIDATION,
            ) from exc

        if memory.scope != episode.scope:
            raise CandidateSchemaError(
                f"episode {episode.uuid} memory scope {memory.scope.key if memory.scope else '<none>'} "
                f"does not match episode scope {episode.scope.key}",
                violation=CandidateViolation.SCOPE_MISMATCH,
                field="scope",
            )
        if memory.subject_label not in instructions.allowed_labels:
            raise CandidateSchemaError(
                f"memory subject label {memory.subject_label!r} is not allowed by instruction set {instructions.name!r}",
                violation=CandidateViolation.LABEL_NOT_ALLOWED,
                field="subject_label",
            )
        if memory.object_label not in instructions.allowed_labels:
            raise CandidateSchemaError(
                f"memory object label {memory.object_label!r} is not allowed by instruction set {instructions.name!r}",
                violation=CandidateViolation.LABEL_NOT_ALLOWED,
                field="object_label",
            )
        # The relation vocabulary is OPEN, the entity vocabulary is CLOSED.  An
        # exact-typed instruction is matched first; failing that, an
        # ``open_predicate`` instruction whose endpoint labels admit this
        # candidate accepts any verb phrase.  Endpoint labels are validated
        # above against the closed label set either way, so an open predicate
        # can never introduce an untyped entity.
        relationship_instruction = match_relationship_instruction(
            instructions=instructions,
            relationship_type=memory.relationship_type,
            subject_label=memory.subject_label,
            object_label=memory.object_label,
        )
        if relationship_instruction is None:
            raise CandidateSchemaError(
                f"memory relationship type {memory.relationship_type!r} is not allowed by "
                f"instruction set {instructions.name!r} for endpoints "
                f"({memory.subject_label!r} -> {memory.object_label!r})",
                violation=CandidateViolation.RELATIONSHIP_TYPE_NOT_ALLOWED,
                field="relationship_type",
            )
        if not relationship_instruction.open_predicate:
            if memory.subject_label != relationship_instruction.source_label:
                raise CandidateSchemaError(
                    f"memory subject label {memory.subject_label!r} does not match "
                    f"{memory.relationship_type} source label {relationship_instruction.source_label!r}",
                    violation=CandidateViolation.LABEL_ENDPOINT_MISMATCH,
                    field="subject_label",
                )
            if memory.object_label != relationship_instruction.target_label:
                raise CandidateSchemaError(
                    f"memory object label {memory.object_label!r} does not match "
                    f"{memory.relationship_type} target label {relationship_instruction.target_label!r}",
                    violation=CandidateViolation.LABEL_ENDPOINT_MISMATCH,
                    field="object_label",
                )
        # The truth-slot guard.  ``scope:subject:predicate`` IS the slot, so a
        # predicate that swallowed the object routes every later restatement of
        # the same fact to a different slot and silently disables supersession,
        # reinforcement, and rollup clustering.  Rejected, never rewritten: a
        # shortened predicate would fabricate a truth key the episode never
        # stated.  Per-candidate rejection is non-fatal and receipted
        # (CANDIDATE_SCHEMA_REJECTED), so a fold costs one candidate.
        predicate_reason = predicate_shape_violation(
            predicate=memory.predicate,
            object_text=memory.object,
            max_words=instructions.max_predicate_words,
        )
        if predicate_reason is not None:
            raise CandidateSchemaError(
                f"memory in episode {episode.uuid}: {predicate_reason}",
                violation=CandidateViolation.PREDICATE_NOT_A_VERB_PHRASE,
                field="predicate",
            )
        self._validate_node_properties(
            label=memory.subject_label,
            properties=memory.subject_properties,
            instructions=instructions,
            field_name="subject_properties",
        )
        self._validate_node_properties(
            label=memory.object_label,
            properties=memory.object_properties,
            instructions=instructions,
            field_name="object_properties",
        )
        if memory.confidence < relationship_instruction.min_confidence:
            raise CandidateSchemaError(
                f"memory confidence {memory.confidence} is below {memory.relationship_type} minimum "
                f"{relationship_instruction.min_confidence}",
                violation=CandidateViolation.CONFIDENCE_BELOW_MINIMUM,
                field="confidence",
            )
        # WS-17 T16b: entity_ref + link_confidence travel together per side, and
        # a supplied ref must actually name an entity (confidence bounds are
        # enforced by the ExtractedMemory field constraints).
        for side in ("subject", "object"):
            entity_ref = getattr(memory, f"{side}_entity_ref")
            link_confidence = getattr(memory, f"{side}_link_confidence")
            if (entity_ref is None) != (link_confidence is None):
                raise CandidateSchemaError(
                    f"memory {side}_entity_ref and {side}_link_confidence must be supplied together or not at all",
                    violation=CandidateViolation.ENTITY_REF_UNPAIRED,
                    field=f"{side}_entity_ref",
                )
            if entity_ref is not None and not entity_ref.strip():
                raise CandidateSchemaError(
                    f"memory {side}_entity_ref cannot be blank",
                    violation=CandidateViolation.ENTITY_REF_BLANK,
                    field=f"{side}_entity_ref",
                )
        # WS-24: the actionability gate.  Last of the per-candidate checks on
        # purpose — a malformed candidate is a REJECTION and a well-formed but
        # unactionable one is a QUARANTINE, so every structural contract is
        # settled before the judgement call is made.
        if actionability is not None and actionability.enabled:
            verdict = actionability_violation(
                subject=memory.subject,
                predicate=memory.predicate,
                object_text=memory.object,
                saves_step=memory.saves_step,
                policy=actionability,
            )
            if verdict is not None:
                code, reason = verdict
                raise CandidateSchemaError(
                    f"memory in episode {episode.uuid}: {reason}",
                    violation=CandidateViolation(code),
                    field="saves_step",
                )
        memory_type = relationship_instruction.memory_type
        if memory_type is None:  # DreamConfig validation normally makes this unreachable.
            raise CandidateSchemaError(
                f"relationship type {memory.relationship_type!r} has no memory_type",
                violation=CandidateViolation.MEMORY_TYPE_UNRESOLVED,
                field="relationship_type",
            )
        claim_mode = memory.claim_mode or default_claim_mode_for_memory_type(memory_type)
        coerced_from = None
        try:
            validate_claim_mode_for_memory_type(claim_mode=claim_mode, memory_type=memory_type)
        except ValueError:
            # memory_type is deterministic (governed by the instruction set's
            # relationship_type mapping); an extractor-supplied claim_mode is
            # a judgment call that can conflict with it (e.g. a transport
            # tagging a DECIDES/decision candidate as claim_mode=directive).
            # Coerce to the type-consistent default instead of discarding the
            # whole episode's candidate batch over one mismatched field.
            #
            # The log line is operator convenience, NOT the record: the original
            # value travels on ``claim_mode_coerced_from`` so formation emits a
            # CANDIDATE_CLAIM_MODE_COERCED receipt for it. A directive silently
            # downgraded to an assertion must be discoverable from the receipt
            # ledger, never only by grepping application logs.
            _log.warning(
                "episode %s: claim_mode %r invalid for memory_type %r; coercing to the type-consistent default",
                episode.uuid,
                claim_mode.value,
                memory_type.value,
            )
            coerced_from = claim_mode
            claim_mode = default_claim_mode_for_memory_type(memory_type)
        return memory.model_copy(update={"claim_mode": claim_mode, "claim_mode_coerced_from": coerced_from})

    def _validate_node_properties(
        self,
        *,
        label: str,
        properties: dict[str, Any],
        instructions: DreamInstructionSet,
        field_name: str,
    ) -> None:
        instruction = next(
            node_instruction for node_instruction in instructions.node_instructions if node_instruction.label == label
        )
        allowed = set(instruction.properties) | self._universal_node_properties | set(instruction.aliases)
        unknown = [key for key in properties if key not in self._reserved_node_properties and key not in allowed]
        reserved = [key for key in properties if key in self._reserved_node_properties]
        if reserved:
            raise CandidateSchemaError(
                f"{field_name} cannot set reserved node property {reserved[0]!r}",
                violation=CandidateViolation.PROPERTY_KEY_RESERVED,
                field=field_name,
            )
        if unknown and instruction.strict_properties:
            raise CandidateSchemaError(
                f"{field_name} key {unknown[0]!r} is not allowed for node label {label!r} "
                f"by instruction set {instructions.name!r}",
                violation=CandidateViolation.PROPERTY_KEY_NOT_ALLOWED,
                field=field_name,
            )
        # Non-strict: silently remove keys not in the allowed list so the memory still materialises.
        for key in unknown:
            del properties[key]
        # WS-27 T2: required vs optional properties per type.  Checked AFTER
        # unknown-key pruning so a required key never counts as "present" via
        # a since-stripped, non-strict key of the same name.
        missing_required = [key for key in instruction.required_properties if not str(properties.get(key, "")).strip()]
        if missing_required:
            raise CandidateSchemaError(
                f"{field_name} is missing required property {missing_required[0]!r} for node "
                f"label {label!r} (instruction set {instructions.name!r})",
                violation=CandidateViolation.REQUIRED_PROPERTY_MISSING,
                field=field_name,
            )
        # WS-27 T1: required disambiguating description.  `description` is a
        # UNIVERSAL_NODE_PROPERTIES key so it always survives the pruning
        # above regardless of `strict_properties`; only its PRESENCE is
        # judged here.
        if instruction.require_description and not str(properties.get("description", "")).strip():
            raise CandidateSchemaError(
                f"{field_name} is missing a description for node label {label!r}, which "
                f"requires one (instruction set {instructions.name!r})",
                violation=CandidateViolation.ENTITY_DESCRIPTION_MISSING,
                field=field_name,
            )

    def _parse_scope(self, value: str) -> MemoryScope:
        if ":" not in value:
            raise CandidateSchemaError(
                f"scope must be formatted as kind:id, got {value!r}",
                violation=CandidateViolation.SCOPE_FORMAT_INVALID,
                field="scope",
            )
        kind, scope_id = value.split(":", 1)
        return MemoryScope(kind=kind.strip(), scope_id=scope_id.strip())


class RuleBasedExtractionTransport:
    """Local transport for deterministic POC runs and tests using the same extractor contract."""

    async def extract_memories(self, request: ExtractionRequest) -> list[dict[str, Any]] | CookbookEnvelope:
        episode = request.episode
        if episode.source == EpisodeType.JSON:
            return self._parse_json_body(episode)
        return self._parse_memory_lines(episode)

    def _parse_json_body(self, episode: Episode) -> list[dict[str, Any]] | CookbookEnvelope:
        try:
            payload = json.loads(episode.body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"episode {episode.uuid} has invalid JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"episode {episode.uuid} JSON body must be an object")
        return decode_extraction_envelope(payload, source=f"episode {episode.uuid}")

    def _parse_memory_lines(self, episode: Episode) -> list[dict[str, Any]]:
        memories: list[dict[str, Any]] = []
        for line in episode.body.splitlines():
            stripped = line.strip()
            if not stripped.startswith("Memory:"):
                continue
            fields = stripped.removeprefix("Memory:").strip()
            memory: dict[str, Any] = {}
            for part in fields.split(";"):
                if not part.strip():
                    continue
                if "=" not in part:
                    raise ValueError(f"malformed memory field in episode {episode.uuid}: {part.strip()}")
                key, value = part.split("=", 1)
                memory[key.strip()] = value.strip()
            memories.append(memory)
        return memories


class OpenAICompatibleExtractionTransport(OpenAICompatibleChatTransport):
    """OpenAI-compatible chat-completions transport for LLM memory extraction.

    The only network extraction transport.  Its defaults target the JedAI
    Gateway (:data:`~memotron.gateway.DEFAULT_GATEWAY_BASE_URL` /
    :data:`~memotron.gateway.GATEWAY_API_KEY_ENV`); pass ``base_url`` and
    ``api_key_env`` explicitly to reach any other OpenAI-compatible endpoint.

    Construction, auth, request building and response decoding come from
    :class:`~memotron.gateway.OpenAICompatibleChatTransport`.  What is specific to
    extraction is its two measured defaults below, its use of the retrying POST,
    and the deliberate re-wrap of the retry's error — see
    :meth:`_extract_memories_sync`.
    """

    DEFAULT_MODEL = DEFAULT_GATEWAY_MODEL

    DEFAULT_MAX_TOKENS = 16384
    """MEASURED. Omitting ``max_tokens`` let the JedAI Gateway apply its own
    4096-token default, and a 12k-character instruction set asking for every
    candidate in a dense section overran it: ``finish_reason="length"`` with
    ``completion_tokens=4096`` and a content length of ZERO -- the truncated
    body carried no parseable JSON object, so the envelope check raised and the
    whole run aborted. Three consecutive portal rebuilds died this way. An
    explicit ceiling is the fix; a retry on truncation would only pay twice for
    the same overrun."""

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        api_key_env: str = GATEWAY_API_KEY_ENV,
        api_key: str | None = None,
        timeout_seconds: float = 300.0,
        retry_policy: GatewayRetryPolicy | None = None,
    ) -> None:
        # ORDER IS BEHAVIOUR, not style. `int(max_tokens)` has always run before
        # any of the five shared field checks, so `max_tokens="abc"` raises
        # int()'s own ValueError ("invalid literal for int() ...") and
        # `max_tokens=None` raises TypeError, whatever else is also wrong with
        # the arguments. Moving the conversion after `super().__init__` would
        # turn both into "model cannot be blank" / "timeout_seconds must be
        # greater than zero" for exactly the callers who need the real reason.
        # `model.strip()` is hoisted for the same reason: it precedes the
        # conversion, so a non-str model still raises AttributeError first.
        stripped_model = model.strip()
        self.max_tokens = int(max_tokens)
        self.retry_policy = retry_policy or GatewayRetryPolicy()
        # The 300s default is MEASURED alongside DEFAULT_MAX_TOKENS. 60s was
        # survivable only because the 4096-token gateway default truncated every
        # long response early; once the ceiling was raised, a dense section
        # legitimately generates for longer than a minute and the read timed out
        # mid-run. The two settings are coupled: raising the token ceiling
        # without raising the timeout just trades a truncation failure for a
        # timeout failure.
        super().__init__(
            model=stripped_model,
            base_url=base_url,
            api_key_env=api_key_env,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            error_label="LLM extraction",
        )
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than zero")

    async def extract_memories(self, request: ExtractionRequest) -> list[dict[str, Any]] | CookbookEnvelope:
        return await asyncio.to_thread(self._extract_memories_sync, request)

    def _extract_memories_sync(self, request: ExtractionRequest) -> list[dict[str, Any]] | CookbookEnvelope:
        # Key first, payload second -- the order this transport has always had.
        # effective_system_prompt() can raise on a malformed request, and a
        # caller with no key configured must still be told that first.
        api_key = self._resolve_api_key()
        payload = self._chat_payload(
            system=effective_system_prompt(request),
            user=request.prompt,
            max_tokens=self.max_tokens,
        )
        # Retried, unlike dream-agent and synthesis. This path previously
        # hand-rolled its call and caught only HTTPError and URLError -- which
        # MISSES http.client.RemoteDisconnected, because it is raised out of
        # getresponse() and urllib does not wrap it in a URLError.
        #
        # Measured: ingesting the ten jedai-gateway pages died on a
        # RemoteDisconnected after 10 facts of 119 episodes. A single transient
        # disconnect mid-run discarded every remaining episode, on a corpus that
        # takes several minutes to extract -- the longer the run, the likelier
        # it is to be killed by one dropped connection.
        #
        # The re-wrap to a PLAIN ValueError is deliberate and load-bearing, not
        # tidying: DreamEngine._run_job catches GatewayRequestError around
        # formation and, when `retryable`, receipts it as an EMBEDDING outage and
        # checkpoints the run. Letting extraction's GatewayRequestError escape
        # would silently route extraction outages into that branch and mislabel
        # them. Extraction failures are meant to propagate uncheckpointed.
        try:
            response_body = self._post_with_retry(
                "/chat/completions", payload, policy=self.retry_policy, api_key=api_key
            )
        except GatewayRequestError as exc:
            raise ValueError(str(exc)) from exc

        # Response VALIDATION stays outside the retry, as it does for embeddings:
        # a body the gateway really sent but we cannot parse is not transient and
        # must not be paid for twice.
        content = self._chat_content(response_body, content_kind="a JSON string")
        # A gateway that drops response_format (LiteLLM's drop_params) lets the
        # model answer with fenced JSON, so strip fences and tolerate trailing
        # prose before parsing.
        parsed = parse_first_json_object(strip_markdown_fences(content), source="LLM extraction message")
        return decode_extraction_envelope(parsed, source="LLM extraction message", require_memories_key=True)
