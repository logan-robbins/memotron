"""JedAI portal KB tenant configuration -- all pilot policy in one place.

INGEST.md §2 / §4c / §5c / §6c reduced to a single ``DreamConfig`` plus the
transports that make the run non-deterministic end to end.

Isolation contract
------------------
This pilot owns its own graph and its own tenant. It must never open the
spymaster graph. :func:`assert_isolation` enforces that loudly, the way
``demo/setup.sh`` step 5 does, and every entry point calls it before opening a
store.

Vector space is chosen once, here
---------------------------------
``text-embedding-3`` (3072 dims) is sealed onto the tenant before the first
episode. INGEST.md §5c: the embedding identifier is stamped per stored vector
and the identifier guard refuses mixed-space cosine, so changing the model later
forces a full purge and re-extract. This is a fresh graph, so the final vector
space is picked now.

Deviations from INGEST.md, with reasons
--------------------------------------
1. **T16/T17/T18/T19 and the alias registry have landed since INGEST.md was
   written** (INGEST.md cites ``33336e5``; HEAD is ``b678dd6``). INGEST.md
   scheduled these as "Phase 1" and proposed the alias registry as net-new
   "WS-A". They are all config-reachable today:
   ``PredicateCanonicalizationPolicy`` (T16), ``RollupConsolidationPolicy
   .cross_prefix_duplicate_threshold`` (T17), ``OpenAICompatibleEmbeddingTransport``
   (T18), ``ConsolidationSynthesisProfile.model_identifier`` (T19), and
   ``EntityResolutionPolicy`` (the alias registry). The pilot therefore enables
   all of them rather than running INGEST.md's degraded Phase 0.
2. **The two RBAC scopes are built but the pilot slice populates only the
   external one.** All 60 ``visibility: jedai`` pages live outside the pilot
   subtrees (they are under ``backstage/``, ``internal/``, ``platform/
   authentication``, ``products/jedai-mcps``, ``solution_engineering/
   mcp-platform``). The routing rule is implemented and tested against
   frontmatter, so widening the slice needs no code change.
3. **``strict_properties=True`` is kept on the LLM path.** INGEST.md §4c allows
   flipping it to ``False`` for LLM transports. Keeping it ``True`` makes
   key-invention noise *visible* as receipted ``property_key_not_allowed``
   rejections, which is exactly the measurement the pilot exists to produce.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import OrderedDict
from pathlib import Path

from memotron.agents import OpenAICompatibleDreamAgentTransport
from memotron.config import (
    ConfidencePolicy,
    ConsolidationSynthesisProfile,
    DedupPolicy,
    DreamAgentConfig,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    DreamPromptProfile,
    EntityResolutionPolicy,
    MemoryBank,
    MemoryHealthPolicy,
    Motive,
    NodeInstruction,
    PredicateCanonicalizationPolicy,
    PruningPolicy,
    RelationshipInstruction,
    RollupConsolidationPolicy,
    SalienceRubric,
    SupersessionPolicy,
    default_prompt_profiles,
)
from memotron.embedding import OpenAICompatibleEmbeddingTransport
from memotron.extraction import OpenAICompatibleExtractionTransport
from memotron.gateway import (
    DEFAULT_GATEWAY_EMBEDDING_MODEL,
    DEFAULT_GATEWAY_MODEL,
    GATEWAY_API_KEY_ENV,
    gateway_base_url_from_env,
)
from memotron.models import (
    DreamJobKind,
    MemoryScope,
    MemoryType,
    RelationshipCardinality,
    ScopeKind,
)
from memotron.synthesis import OpenAICompatibleSynthesisTransport

# --------------------------------------------------------------------------
# Identity and isolation
# --------------------------------------------------------------------------

INGEST_DIR = Path(__file__).resolve().parent
REPO_ROOT = INGEST_DIR.parent

TENANT_ID = "jedai-portal-kb"
GRAPH_PATH = INGEST_DIR / ".memotron" / "portal-kb.sqlite"
MANIFEST_PATH = INGEST_DIR / ".manifest.json"

#: The graph this pilot must NEVER open.
PROTECTED_GRAPH = REPO_ROOT / ".memotron" / "spymaster.sqlite"

EXTERNAL_SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id=TENANT_ID)
INTERNAL_SCOPE = MemoryScope(kind=ScopeKind.TENANT, scope_id=f"{TENANT_ID}-internal")

SCOPES = {"external": EXTERNAL_SCOPE, "internal": INTERNAL_SCOPE}

INSTRUCTION_SET_NAME = "jedai-kb"
MOTIVE_NAME = "ingest-portal-kb"

FORMATION_JOB = "kb-formation"
CONSOLIDATION_JOB = "kb-consolidation"
PRUNING_JOB = "kb-pruning"


def assert_isolation(graph_path: Path = GRAPH_PATH) -> None:
    """Fail loudly if the pilot would touch the protected spymaster graph."""
    resolved = graph_path.expanduser().resolve()
    protected = PROTECTED_GRAPH.expanduser().resolve()
    if resolved == protected:
        raise SystemExit(
            f"FAILED isolation: pilot graph resolved onto the protected graph\n"
            f"  resolved  {resolved}\n  protected {protected}"
        )
    if not resolved.is_relative_to(INGEST_DIR):
        raise SystemExit(f"FAILED isolation: pilot graph must live under {INGEST_DIR}\n  resolved {resolved}")


def scope_for(frontmatter: dict[str, object]) -> MemoryScope:
    """Route a page to the external or RBAC-gated internal scope.

    Mirrors the portal's own per-route RBAC: ``visibility: jedai`` is the gate.
    """
    visibility = str(frontmatter.get("visibility") or "").strip().lower()
    return INTERNAL_SCOPE if visibility == "jedai" else EXTERNAL_SCOPE


# --------------------------------------------------------------------------
# Canonical entity table (INGEST.md §4c, driver-side)
# --------------------------------------------------------------------------

#: alias (normalized) -> canonical name. Feeds ``EntityResolutionPolicy.synonyms``,
#: the deterministic bridge that collapses the Goal-1 fragmentation gauntlet.
#: Both sides are normalized (casefold + whitespace collapse) by the policy, so a
#: pair that normalizes to itself is rejected at construction -- only genuine
#: surface variants belong here.
ENTITY_SYNONYMS: dict[str, str] = {
    # The Goal-1 gauntlet: INGEST.md §1 counted 7 surface forms in the pilot slice.
    "the gateway": "Jedai Gateway",
    "jedai-gateway": "Jedai Gateway",
    "jedai gw": "Jedai Gateway",
    "gateway": "Jedai Gateway",
    "gateway v2": "Jedai Gateway",
    "jedai gateway v2": "Jedai Gateway",
    "dscribe": "D-Scribe",
    "d scribe": "D-Scribe",
    "guest content experience": "GCX",
    "knowledge base": "Jedai Knowledge Base",
    "knowledgebase": "Jedai Knowledge Base",
    "jedai kb": "Jedai Knowledge Base",
    "vertex search": "Vertex AI Search",
    "vertex": "Vertex AI Search",
    "litellm proxy": "LiteLLM",
}

#: Never alias these together -- the false-merge canaries (INGEST.md G1-2).
#: The engine additionally hard-blocks links between names carrying differing
#: identifier-like tokens, so this list is a belt-and-braces assertion target.
DISTINCT_ID_CANARIES: tuple[str, ...] = (
    "kb_ds_source_plandisney_pocket_guides_wdw_en_us_v1",
    "kb_ds_source_plandisney_pocket_guides_dlr_en_us_v1",
    "kb_ds_source_dscribe_special_offers_wdw_en_us_v1",
    "kb_ds_source_dscribe_special_offers_dlr_en_us_v1",
    "kb_ds_source_dscribe_special_offers_aulani_en_us_v1",
    "kb_ds_source_dscribe_facility_guest_wdw_en_us_v1",
    "kb_ds_source_dscribe_facility_guest_dlr_en_us_v1",
    "kb_ds_source_dscribe_facility_guest_aulani_en_us_v1",
    "kb_ds_source_zendesk_call_center_en_us_v1",
    "kb_ds_source_dscribe_photopass_page_aulani_en_us_v1",
)


# --------------------------------------------------------------------------
# Instruction set -- encyclopedic types only
# --------------------------------------------------------------------------

#: Extended past the default ``kind, role, tier, region, system`` so a KB entity
#: can carry its home slug and owning team as searchable node coordinates
#: (INGEST.md §4c: ``_node_search_text`` feeds seed resolution and candidates).
ENTITY_PROPERTIES: tuple[str, ...] = (
    "kind",
    "role",
    "tier",
    "region",
    "system",
    "team",
    "env",
    "slug",
    # Added after measuring the first full run: these are the keys the extractor
    # actually reached for on this corpus, ranked by rejection count.
    "status",
    "availability_target",
    "rpo",
    "deployment_id",
    "data_capabilities",
    "regulations",
    "format",
    "cadence",
)


# --------------------------------------------------------------------------
# The entity vocabulary: CLOSED.  The relation vocabulary: OPEN.
# --------------------------------------------------------------------------
#
# The asymmetry is Anthropic's knowledge-graph guide: `EntityType` is a closed
# `Literal` enum while `Relation.predicate` is a free "short verb phrase".
# Entity types are what resolution and traversal key on, so they must be closed.
# Predicates never need canonicalizing because retrieval ANN-matches the
# EMBEDDED FACT SENTENCE, not the predicate label.
#
# Three anchored layers -- nothing here is invented:
#   general      schema.org + SKOS (skos:Concept = "units of thought")
#   platform     Backstage Software Catalog, definitions verbatim
#   operational  ADR (Nygard) for Decision; OTel semconv for Environment
#
# WHY THIS EXISTS. Earlier runs declared ONE label ("Entity") and all relations
# as Entity -> Entity, so the engine's endpoint-label check had nothing to bite
# on: 207 nodes for 147 facts -- more nodes than facts, meaning nearly every
# fact minted a node nothing else referenced ("routing decisions, access
# control, cost tracking, and operational policies"). A closed, typed
# vocabulary rejects a four-item list because it cannot claim to be any type.

_DESC: tuple[str, ...] = ("description",)

# WS-27 T1: every label below sets `require_description=True`. The prompt
# already asked for one ("EVERY ENTITY GETS a description"); this is that
# ask ENFORCED -- a description-less entity is quarantined
# (`entity_description_missing`) rather than silently materialized
# indistinguishable from any other same-named node.
NODE_INSTRUCTIONS: tuple[NodeInstruction, ...] = (
    # ---- platform layer: Backstage Software Catalog (definitions verbatim)
    NodeInstruction(
        label="Component",
        query=(
            'Backstage Component: "a piece of software, for example a mobile '
            'feature, web site, backend service or data pipeline". Jedai '
            "Gateway, D-Scribe, LiteLLM. NOT a capability it offers, and never "
            "a list of capabilities."
        ),
        properties=(*_DESC, "tier", "status", "system"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="API",
        query=(
            'Backstage API: "form boundaries between components" and are '
            '"implemented by components". One interface per node, named as the '
            "docs name it, with its URL as the `url` property."
        ),
        properties=(*_DESC, "url", "env"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="Resource",
        query=(
            'Backstage Resource: "the infrastructure a component needs to '
            "operate at runtime, like BigTable databases, Pub/Sub topics, S3 "
            'buckets or CDNs". Also the addressable artifact an agent must '
            "otherwise hunt for: a data store id, a content path, a BAPPID. "
            "The identifier or path ITSELF is the node name, verbatim. Each "
            "distinct identifier is a DISTINCT node -- never merge two that "
            "differ by resort, locale, or version."
        ),
        properties=(*_DESC, "system", "region", "format", "cadence", "slug"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="System",
        query=(
            'Backstage System: "a collection of resources and components that '
            'exposes one or several public APIs". The Jedai Platform itself.'
        ),
        properties=(*_DESC, "tier", "region"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="Domain",
        query=(
            'Backstage Domain: "group a collection of systems that share '
            'terminology, domain models, metrics, KPIs, business purpose".'
        ),
        properties=_DESC,
        strict_properties=False,
        require_description=True,
    ),
    # ---- operational layer: ADR + OpenTelemetry semantic conventions
    NodeInstruction(
        label="Environment",
        query=(
            "One deployment environment, named exactly as the docs name it: "
            "latest, stage, load, integration, production. OTel "
            "`deployment.environment`. One per node -- never a phrase covering "
            "several."
        ),
        properties=(*_DESC, "availability_target", "rpo", "deployment_id", "status"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="Decision",
        query=(
            "An ADR: one ratified decision with an identifier, a status, and a "
            "rationale, e.g. 'D002: Standard Environments'. The decision RECORD "
            "is this node; the thing it governs is a separate node, and the "
            "relation runs from the decision to what it governs."
        ),
        properties=(*_DESC, "status"),
        strict_properties=False,
        require_description=True,
    ),
    # WS-27 T2: `reference` is REQUIRED, and no raw secret VALUE key exists
    # on this label's whitelist at all -- a candidate that only carries a
    # value therefore already fails as an unknown property key
    # (`property_key_not_allowed` under `strict_properties`; here it is
    # silently pruned, then `reference`'s absence trips
    # `required_property_missing`), giving the "never a value" half of the
    # contract for free from the closed property whitelist, and the
    # "always a reference" half from `required_properties`.
    NodeInstruction(
        label="Credential",
        query=("One credential kind the docs name: virtual key, service account key, agent key, personal key."),
        properties=(*_DESC, "env", "status", "reference"),
        strict_properties=False,
        require_description=True,
        required_properties=("reference",),
    ),
    NodeInstruction(
        label="Model",
        query=(
            "One model alias exactly as the gateway registers it, e.g. "
            "claude-haiku-4-5. 'GPT-4o, Claude, Gemini and others' is NOT a "
            "model -- it is a list, and must not be emitted."
        ),
        properties=(*_DESC, "status"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="Tool",
        query=("Something an agent INVOKES rather than operates: a CLI, an SDK, an Admin UI, a command."),
        properties=(*_DESC, "system"),
        strict_properties=False,
        require_description=True,
    ),
    # ---- general layer: schema.org + SKOS
    NodeInstruction(
        label="Group",
        query=(
            'Backstage Group / schema.org Organization: "an organizational '
            'entity, such as a team, a business unit". Also a named role a '
            "permission attaches to: Team Admin, Jedai Council, Org Admin."
        ),
        properties=(*_DESC, "system"),
        strict_properties=False,
        require_description=True,
    ),
    NodeInstruction(
        label="Event",
        query=(
            "schema.org Event: something that happened or is scheduled at a "
            "point in time -- an incident, a deploy, a migration, a cutover."
        ),
        properties=(*_DESC, "status"),
        strict_properties=False,
        require_description=True,
    ),
    # WS-27 T4: the Concept catch-all guardrail. `MemoryHealthPolicy
    # .general_labels` defaults to exactly `("Concept",)`, so
    # `kb_config()`'s health policy below measures and blocks on this
    # label's share with no per-tenant configuration needed here.
    NodeInstruction(
        label="Concept",
        query=(
            'skos:Concept -- "units of thought: ideas, meanings, or categories '
            'of objects and events". A domain TERM the docs define and reuse. '
            "GUARD: a concept must sit in a hierarchy (something broader or "
            "narrower than it) AND be referenced more than once. A term "
            "mentioned once with nothing above or below it is a noun phrase, "
            "not a concept -- do not emit it. This is the catch-all, and a "
            "catch-all is how a previous vocabulary absorbed 84% of this "
            "corpus, so apply the guard strictly: prefer the most specific "
            "label above that fits; Concept is the LAST RESORT, never a "
            "default."
        ),
        properties=_DESC,
        strict_properties=False,
        require_description=True,
    ),
)

#: OPEN predicates. Ordered -- the first instruction whose endpoint labels admit
#: a candidate wins, so the specific ones precede the catch-all. The predicate
#: itself is any short verb phrase; only the memory TYPE is decided here, since
#: memory_type is a policy selector (cardinality, dedup, retention, budget).
RELATIONSHIP_INSTRUCTIONS: tuple[RelationshipInstruction, ...] = (
    RelationshipInstruction(
        type="GOVERNS",
        source_label="Decision",
        target_label="*",
        query=(
            "A ratified decision and the thing whose behaviour it fixes. State "
            "the GOVERNED thing as the object -- an agent asks about the "
            "environment or the component, never about the decision id."
        ),
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.DECISION,
        min_confidence=0.5,
        open_predicate=True,
    ),
    RelationshipInstruction(
        type="REQUIRES",
        source_label="*",
        target_label="Credential",
        query=(
            "A credential kind something permits, demands, or REFUSES. The "
            "refusals matter most: they are what stops an agent."
        ),
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.REQUIREMENT,
        min_confidence=0.5,
        open_predicate=True,
    ),
    RelationshipInstruction(
        type="MUST",
        source_label="Group",
        target_label="*",
        query="An obligation, gate, or required order of operations for a team or role.",
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.REQUIREMENT,
        min_confidence=0.5,
        open_predicate=True,
    ),
    RelationshipInstruction(
        type="SHOULD",
        source_label="*",
        target_label="Tool",
        query="Operating guidance directing someone to invoke a specific tool.",
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.DIRECTIVE,
        min_confidence=0.6,
        open_predicate=True,
    ),
    RelationshipInstruction(
        type="HAS_STATE",
        source_label="Environment",
        target_label="*",
        query=(
            "A measurable operating characteristic of an environment stated as "
            "a target or a limit: availability target, RPO, budget cap."
        ),
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.STATE,
        min_confidence=0.5,
        open_predicate=True,
    ),
    # The catch-all: everything structural and addressing. `partOf`,
    # `reachable at`, `backed by`, `routes to`, `ingests from` all land here as
    # free verb phrases -- Backstage relation names are a HINT in the prompt,
    # not an enum in the schema.
    RelationshipInstruction(
        type="RELATES_TO",
        source_label="*",
        target_label="*",
        query=(
            "Any other durable relation between two typed entities. Use a SHORT "
            "VERB PHRASE as the predicate -- 'part of', 'reachable at', 'backed "
            "by', 'routes to', 'ingests from', 'owned by', 'depends on'. "
            "Backstage's relation names are good defaults. The predicate is free "
            "text; state the full claim in the fact sentence."
        ),
        cardinality=RelationshipCardinality.MULTI_ACTIVE,
        memory_type=MemoryType.ANCHOR,
        min_confidence=0.5,
        open_predicate=True,
    ),
)


def kb_instruction_set() -> DreamInstructionSet:
    return DreamInstructionSet(
        name=INSTRUCTION_SET_NAME,
        node_instructions=NODE_INSTRUCTIONS,
        relationship_instructions=RELATIONSHIP_INSTRUCTIONS,
        truth_goal=(
            "Record encyclopedic, durable facts stated by Disney JedAI platform "
            "documentation. The document IS the ground truth; a later revision of the "
            "same section supersedes the earlier one."
        ),
    )


# --------------------------------------------------------------------------
# Prompt profile -- canonicalization + predicate stability rules
# --------------------------------------------------------------------------

KB_PROFILE_NAME = "jedai-kb-docs"
KB_PROFILE_VERSION = "v1"

# ONE authored extraction prompt (~1400 tokens; measured with tiktoken
# cl100k_base), replacing what used to be ASSEMBLED at runtime by
# concatenating NODE_INSTRUCTIONS' and RELATIONSHIP_INSTRUCTIONS' ``query``
# fields (13 + 6 of them) into ~12,000 characters no human ever read as a
# whole. That generation was the direct cause of a measured defect:
# ``RelationshipInstruction(type="GOVERNS", ..., query="...State the GOVERNED
# thing as the object.")`` was written as a selection criterion, but rendered
# as prose it read to the model as a dictated predicate, and it emitted
# "governs" five times for D002 -- flattening a five-row Promotion Tollgate
# table into one governance relation. This constant is written by hand,
# grounded in the actual corpus (ingest/kb_config.py's own NODE_INSTRUCTIONS/
# RELATIONSHIP_INSTRUCTIONS still govern validation -- cardinality,
# memory_type, min_confidence, the closed label/type sets -- but their
# ``query`` text no longer feeds a prompt; see
# ``DreamInstructionSet.render_prompt`` in ``src/memotron/config.py``).
#
# This is the DEFAULT prompt_text for the ``jedai-kb-docs`` profile, not a
# hardcode: DreamPromptProfile / tenant_prompt_overrides / set_tenant_prompt_
# override (graph.py) and the admin Prompts screen all continue to read and
# override it exactly as they would any other profile's prompt_text -- see
# ``AgentMemoryPlatform.apply_tenant_prompt_to_client`` (agent_memory.py).
KB_EXTRACTION_PROMPT = """\
Build an encyclopedic knowledge graph of the Disney JedAI platform from its documentation portal: what each component is, what it requires, what was decided about it, and its current configuration. The document is ground truth; a later revision supersedes an earlier one.

EXTRACT: identities (what a component, data source, data store, team, or environment IS); requirements, prerequisites, and constraints stated as such; ratified decisions with their status and effective date; configuration state (update cadence, feed format, region, availability target, endpoint, backing data store, owning team); the exact Vertex data store ID per brand/locale.

SKIP: example payloads and illustrative sample records ("Test Canada Room Offer" is a fixture, not a platform fact); navigational prose and card blurbs that only point at another page; anything the page frames as hypothetical, deprecated, or "coming soon" without a date.

ENTITY TYPES -- closed vocabulary. Each: definition, positive examples, one counter-example.

Component (Backstage: "a piece of software -- a mobile feature, web site, backend service, or data pipeline"): e.g. Jedai Gateway, D-Scribe, LiteLLM. NOT its capabilities -- "handles routing, access control, and cost tracking" describes a Component; it is not one itself.

API (Backstage: "forms boundaries between components"): the named interface a Component implements, e.g. the Gateway's OpenAI-compatible endpoint, the LiteLLM /chat/completions interface. NOT the payload or model it carries.

Resource (Backstage: infrastructure or an addressable artifact a Component needs): a data store, content path, BAPPID, or Vertex data-store id, e.g. kb_ds_source_dscribe_special_offers_wdw_en_us_v1, BAPP0248028. Each distinct identifier is its OWN node -- never merge two differing by resort, locale, or version. NOT a schema field inside the resource.

System (Backstage: "a collection of resources and components exposing public APIs"): e.g. the Jedai Platform, the DLP Platform. NOT a single Component or Resource.

Domain (Backstage: systems sharing terminology and business purpose): a grouping above System. NOT a System itself.

Environment (OTel deployment.environment): one deployment target, named exactly as the docs name it -- latest, stage, load, integration, production. One per node. NOT "all lower environments," a phrase covering several.

Decision (ADR): one ratified decision with an id, status, rationale, e.g. "D002: Standard Platform Environments," "D004: 99.99% Availability Target Strategy." The record is this node; what it fixes is a SEPARATE node. NOT the environment or component it governs.

Credential: one credential kind the docs name -- virtual key, service account key, agent key. Set its `reference` property to how the docs point at it (an env var name, a vault path, a key id) -- REQUIRED, and NEVER the literal secret value.

Model: one model alias as the gateway registers it, e.g. claude-haiku-4-5, text-embedding-3. NOT a list -- "GPT-4o, Claude, Gemini, and additional approved models" names no single model; extract only aliases individually named.

Tool: something an agent invokes rather than operates -- a CLI (e.g. gh), an SDK, an Admin UI. NOT the platform hosting it.

Group (schema.org Organization): a team, business unit, or named permission role, e.g. Team Admin, Jedai Council. NOT a person.

Event (schema.org Event): something that happened or is scheduled -- an incident, a deploy, a cutover. NOT a recurring policy.

Concept (skos:Concept, "units of thought"): a domain term sitting in a hierarchy (something broader/narrower) AND referenced more than once -- e.g. "four nines" as a class of the broader "Availability Target" hierarchy, mentioned across D001 and D004. NOT a measured value: "6h RPO" and "99.99% (four 9s) availability target" naming ONE environment's threshold are numbers, not concepts -- record those as HAS_STATE facts on the Environment they bound, never as Concept nodes.

CHOOSE THE MOST SPECIFIC LABEL. Before writing Concept, check every label above: is this actually a Component, Resource, System, Environment, Credential, Model, Tool, Group, or Event? Concept is the LAST RESORT for a genuine domain term that fits none of them, never a default reached for convenience -- a previous vocabulary that used one general label for everything absorbed 84% of this corpus into it, which is exactly the failure this ordering exists to prevent.

ONE ENTITY PER NODE. A comma-separated list is never one entity: "routing decisions, access control, cost tracking, and operational policies" is several candidate facts about one Component, not one node. Same for "GPT-4o, Claude, Gemini, and additional approved AI models" -- extract named members individually or skip the list.

EVERY ENTITY GETS A description: one sentence, grounded in this document, specific enough to tell two same-named entities apart later. A description-less entity is quarantined, not stored -- omitting one costs the whole entity, not just the sentence.

ALIASES. When the document uses more than one surface form for the SAME entity (an acronym, an abbreviation, a prior name -- "GCX" for "guest content experience", "the gateway" for "Jedai Gateway"), set that entity's `aliases` to the list of the OTHER surface forms alongside its canonical name. Omit `aliases` for an entity with only one surface form; never invent one that is not in the document.

PREDICATES ARE OPEN. Use the document's own verb or noun phrase; never invent one, and never let a relationship type below hand you a verb to reuse. Pick relationship_type by the STRUCTURAL role of the pair, never by matching a word: GOVERNS pairs a Decision with what its ruling fixes; REQUIRES pairs anything with a Credential it permits, demands, or refuses; MUST pairs a Group with an obligation; SHOULD pairs anything with a Tool to invoke; HAS_STATE pairs an Environment with a measured characteristic; RELATES_TO is the catch-all for any other durable pairing. The bucket never dictates the predicate word -- five GOVERNS facts should use five different predicates if the document states five different verbs.

TABLES AND BOLD-KEY BULLETS. Every row of an attribute table is a candidate fact per column: a "Promotion Tollgate" column names a distinct requirement per environment row -- five rows, five separate facts, never one "has tollgates" fact. A bold-key bullet ("integration: External team lower environment (treated as production by Jedai)") states its own fact even when a neighboring cell describes the same entity differently ("Mirrors production" for Infrastructure) -- extract both; the generic claim must never crowd out the specific one.

DO NOT EXTRACT: prose naming no address and enabling no action; a capability restatement ("X supports/provides/handles/enables Y") with nothing an agent could act on; a field, property, or column inventory (schema reference tables) unless the field controls behavior.

CANONICALIZE. Resolve pronouns, bare nouns ("the gateway"), slugs, and initialisms to the canonical name before writing subject or object. Reproduce identifiers, environment names, paths, and URLs verbatim. Reuse the exact predicate from "Existing graph context" when restating a fact already in the graph; introduce a new predicate only for a genuinely new kind of claim. Set subject_properties.slug / object_properties.slug to the documentation slug a fact came from, and .system to the owning platform component when the page states it. Extract only what the documentation asserts -- never infer, generalize, or import outside knowledge about Disney systems.\
"""


def kb_prompt_profile() -> DreamPromptProfile:
    canonical_names = "; ".join(sorted(set(ENTITY_SYNONYMS.values())))
    return DreamPromptProfile(
        name=KB_PROFILE_NAME,
        version=KB_PROFILE_VERSION,
        # Not rendered (prompt_text short-circuits DreamPromptProfile.render_prompt);
        # kept descriptive for the admin Prompts screen / anyone inspecting the
        # object directly.
        goal=(
            "Encyclopedic Disney JedAI platform knowledge graph; the full authored "
            "extraction contract lives in prompt_text (KB_EXTRACTION_PROMPT)."
        ),
        # Canonical-name enumeration is dynamic (sourced from ENTITY_SYNONYMS, the
        # deterministic entity-resolution bridge) and appended to the static
        # authored prompt rather than baked into it, so the two never drift apart.
        prompt_text=(f"{KB_EXTRACTION_PROMPT}\n\nCanonical names for this corpus: {canonical_names}."),
    )


# --------------------------------------------------------------------------
# Motive
# --------------------------------------------------------------------------


def kb_motive() -> Motive:
    return Motive(
        name=MOTIVE_NAME,
        goal=(
            "Ingest the JedAI documentation portal as encyclopedic platform knowledge, "
            "keeping the document as ground truth."
        ),
        # ROLLUP is deliberately ABSENT, which makes the consolidation job skip
        # rollup consolidation wholesale with a `motive_rollup_not_allowed`
        # receipt.  Consolidation is correct only when members are NOT
        # individually actionable; once the actionability gate is selecting for
        # step-saving facts, every survivor is by construction a fact that must
        # keep its own context slot.  Measured on the first portal graph, the
        # old behaviour demoted three correct Vertex data-store routing facts
        # for resembling a catalogue and manufactured two rollups of word salad.
        allowed_memory_types=(
            MemoryType.ANCHOR,
            MemoryType.REQUIREMENT,
            MemoryType.DECISION,
            MemoryType.DIRECTIVE,
            MemoryType.STATE,
        ),
        prompt_profile=KB_PROFILE_NAME,
        prompt_profile_version=KB_PROFILE_VERSION,
        # No prompt_override: kb_prompt_profile() sets prompt_text, which makes
        # DreamPromptProfile.render_prompt() return it verbatim -- a
        # DreamPromptOverride's goal/include/exclude/rules updates would never
        # be rendered on top of it (with_override() does not touch
        # prompt_text). The canonicalization rules an override used to layer
        # on here are folded directly into KB_EXTRACTION_PROMPT instead.
        # Measured on the first full run: min_salience=0.35 dropped 68 of 203
        # candidates (33%). For an encyclopedic KB built from operator-curated
        # docs, that is too aggressive -- a low-salience fact from the portal is
        # still true, and cross-page corroboration is the intended quality signal.
        # Lowered to 0.20; the per-episode cap still bounds the worst case.
        # MEASURED, then disabled. Salience was the DOMINANT filter and it was
        # filtering the wrong way. On the decision-log slice it quarantined all
        # 11 candidates as `below_salience_threshold` -- including
        # "D002 governs production", "NA Platform assigned BAPP0248028", and
        # "integration mirrors production", which are the highest-value facts in
        # the corpus. In an earlier run it killed 81 of 239 candidates while
        # letting "Jedai Gateway supports X" through. It scores prose-shaped
        # statements above short structured ones, which is exactly inverted for a
        # corpus whose value lives in tables and bold-key bullets.
        #
        # min_salience=0.0 hands the accept/reject decision to the actionability
        # gate, which asks the question we actually care about ("what step does
        # knowing this let the agent skip?") instead of estimating importance.
        # Two filters competing for the same decision is how the wrong one won.
        # The per-episode cap stays as the backstop against a runaway episode.
        salience_rubric=SalienceRubric(
            min_salience=0.0,
            max_memories_per_episode=14,
            scale_by_confidence=True,
        ),
        # Scalar Motive override would flatten the per-type map, so it is left None
        # and DedupPolicy.memory_type_thresholds carries the real policy
        # (INGEST.md §4c: "Motive override is scalar").
        dedup_threshold=None,
        retrieval_budget_share=0.5,
    )


# --------------------------------------------------------------------------
# The config
# --------------------------------------------------------------------------


def kb_dedup_policy() -> DedupPolicy:
    """Per-type dedup floors chosen from INGEST.md §4a's measured cosine table.

    Real distinct data-store ID pairs reached **0.895** trigram cosine, so the
    previous report's 0.90 left a 0.005 margin on a live pair. 0.97 leaves ~0.075.
    Identifier-bearing types (identity, state) get 0.97; decision gets 0.95.
    """
    return DedupPolicy(
        cosine_threshold=0.88,
        memory_type_thresholds={
            "anchor": 0.97,
            "state": 0.97,
            "decision": 0.95,
            "requirement": 0.92,
            "directive": 0.90,
        },
    )


def kb_config() -> DreamConfig:
    agent = DreamAgentConfig(
        agent_id="jedai-kb-dreamer",
        name="JedAI KB Dream Agent",
        # IMPORTANT: the dream agent gates at EPISODE granularity, not per candidate.
        # A rejected decision discards the whole section, and the episode simply
        # stays pending forever.
        #
        # Measured the hard way: an earlier version of this policy said "reject
        # speculation, example/sample payload values, and navigational filler."
        # The agent generalized that and rejected 17 of 37 episodes wholesale --
        # every one of the 11 platform-decision-log sections ("a platform decision
        # log entry, not authoritative product documentation") plus the 6 index /
        # overview sections ("navigational/structural metadata (portal index)").
        # That silently deleted the entire temporal band and the cross-page join
        # hub from the graph while the run reported success.
        #
        # Content-selection criteria therefore belong in the PROMPT PROFILE, which
        # shapes what gets extracted from a section, never in the decision policy,
        # which decides whether the section is looked at at all. Per-candidate
        # quality is already enforced downstream by the salience rubric, the Motive
        # type filter, and schema validation.
        decision_policy=(
            "Every episode you see is a section of Disney's own JedAI documentation "
            "portal, ingested by an operator. That includes product docs, platform "
            "decision logs, runbooks, reference tables, and index or overview pages -- "
            "all of them are in scope and all of them are authoritative. Approve "
            "formation, consolidation, and pruning for them. Do not reject an episode "
            "for being short, structural, navigational, or for being a decision log "
            "rather than product documentation. Per-candidate quality is enforced "
            "downstream by the salience rubric, the Motive type filter, and schema "
            "validation, so you do not need to pre-filter content. Withhold approval "
            "only if an episode body is empty or plainly is not portal content."
        ),
    )
    rollup = RollupConsolidationPolicy(
        cluster_threshold=0.75,
        min_cluster_size=3,
        max_depth=2,
        # T17 cross-prefix duplicate sweep (INGEST.md §4c) -- engine default, held
        # explicitly so the pilot's policy is auditable in one place.
        # WS-25 D1: DISABLED for this document Motive.  Demotion must follow
        # contradiction, not resemblance.  This sweep runs independently of the
        # Motive->ROLLUP gate (dreaming.py: "the duplicate sweep above still
        # ran") and demotes on embedding cosine alone, and INGEST.md §4a
        # measured genuinely DISTINCT data-store IDs at 0.895 -- a 0.035 margin
        # under this threshold.  Measured on the first portal graph, three
        # correct Vertex data-store routing facts (MR-24/25/27) left context
        # with nothing contradicting them.  A near-duplicate restatement is
        # cheap; a silently demoted routing fact is a memory that failed.
        cross_prefix_duplicate_threshold=None,
        max_duplicate_demotions_per_run=32,
    )
    return DreamConfig(
        # MEASURED, and deliberately raised. The 0.40 default blocked formation
        # twice on this corpus (max_type_share 0.857, then 0.910). Some of that
        # was the real disease -- a catch-all type absorbing prose -- but not
        # all: a documentation KB read for "where do I go and what will stop me"
        # is LEGITIMATELY dominated by addressing facts. A threshold tuned for a
        # balanced behavioural mix is the wrong instrument here, the same way
        # compression ratio was the wrong headline metric for this workload.
        # 0.75 still catches a true collapse (0.910 would trip it) without
        # demanding a type balance the corpus does not have. The entropy floor
        # is relaxed to match; both remain blocking, not warning.
        health=MemoryHealthPolicy(
            max_type_share_block=0.75,
            min_type_entropy_block=0.20,
            # WS-27 T4: a SEPARATE axis from max_type_share_block above (node
            # LABEL share, not relationship memory_type share) -- general_
            # labels defaults to exactly ("Concept",), the one catch-all this
            # instruction set declares, so no override is needed here. Left
            # at the policy default (0.40): unlike max_type_share_block, this
            # gate has no measured live-ingest trip yet motivating a
            # different ceiling for this corpus.
        ),
        instruction_sets=(kb_instruction_set(),),
        prompt_profiles=(*default_prompt_profiles(), kb_prompt_profile()),
        consolidation_profiles=(
            ConsolidationSynthesisProfile(
                name="kb-rollup-synthesis",
                version="v1",
                objective=(
                    "Name the shared concept of the cited documentation facts in at most "
                    "two sentences, without introducing any fact not present in them."
                ),
                rules=(
                    "Cite every source relationship.",
                    "Preserve temporal bounds and uncertainty.",
                    "Do not resolve contradictory children into a false consensus.",
                    "Never invent an identifier, product name, or team that is not in the members.",
                ),
                # T19 (WS-18): opts consolidation into LLM theme summaries behind the
                # deterministic entailment gate. Without this, theme labels are
                # token bags (INGEST.md §5b.2).
                model_identifier=f"litellm:{_chat_model()}",
            ),
        ),
        jobs=(
            DreamJob(
                name=FORMATION_JOB,
                kind=DreamJobKind.FORMATION,
                cadence_seconds=1,
                max_items_per_run=2000,
                instruction_set=INSTRUCTION_SET_NAME,
                prompt_profile=KB_PROFILE_NAME,
                prompt_profile_version=KB_PROFILE_VERSION,
                motive=MOTIVE_NAME,
                agent=agent,
            ),
            DreamJob(
                name=CONSOLIDATION_JOB,
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=1,
                max_items_per_run=2000,
                instruction_set=INSTRUCTION_SET_NAME,
                prompt_profile=KB_PROFILE_NAME,
                prompt_profile_version=KB_PROFILE_VERSION,
                consolidation_profile="kb-rollup-synthesis",
                consolidation_profile_version="v1",
                motive=MOTIVE_NAME,
                # The job declares the capability; the GATE is the Motive, whose
                # allowed_memory_types omits ROLLUP -- consolidation then skips
                # with a `motive_rollup_not_allowed` receipt.  One lever, not two.
                rollup_consolidation=True,
                rollup_consolidation_policy=rollup,
                agent=agent,
            ),
            DreamJob(
                name=PRUNING_JOB,
                kind=DreamJobKind.PRUNING,
                cadence_seconds=1,
                instruction_set=INSTRUCTION_SET_NAME,
                agent=agent,
            ),
        ),
        memory_bank=MemoryBank(motives=(kb_motive(),)),
        dedup=kb_dedup_policy(),
        # INGEST.md §2.3 / §8.4: for a KB whose ground truth IS the document, a doc
        # edit must flip truth in ONE sync cycle. The default (2) parks an
        # equal-authority correction as `insufficient_corroboration` once the old
        # fact is repeated on >= corroboration_margin pages. Receipts still record
        # the dispute. Keep the default in agent-facing tenants.
        supersession=SupersessionPolicy(corroboration_required=1),
        confidence=ConfidencePolicy(),
        # T16: canonical predicate registry -- stops a paraphrased predicate on a
        # re-extracted edited section from opening a NEW truth slot instead of
        # superseding (INGEST.md §6b.2, the biggest edit-and-resync risk).
        predicate_canonicalization=PredicateCanonicalizationPolicy(
            enabled=True,
            embedding_threshold=0.85,
        ),
        # The alias registry INGEST.md §4c proposed as net-new "WS-A". Operator
        # synonyms are the deterministic bridge; the composed offline signals sum
        # to at most 0.6 (below review_threshold), so nothing auto-links without
        # an operator synonym or extractor attestation, and differing
        # identifier-like tokens are hard-blocked regardless of score.
        entity_resolution=EntityResolutionPolicy(
            enabled=True,
            synonyms=dict(ENTITY_SYNONYMS),
            auto_link_threshold=0.85,
            review_threshold=0.65,
            # NOTE: the engine defaults (max_inventory=500, max_pair_scan=512) are
            # kept. `_entity_link_signals` re-embeds the mention name once per
            # candidate (README §9.1), which made that fan-out ruinously slow --
            # but ResilientEmbeddingTransport memoizes embeddings, so the repeats
            # are free and full alias-detection recall is affordable again.
        ),
        pruning=PruningPolicy(),
    )


# --------------------------------------------------------------------------
# Transports -- every one of these is a real gateway call
# --------------------------------------------------------------------------


def _chat_model() -> str:
    return os.environ.get("MEMOTRON_LLM_MODEL", "").strip() or DEFAULT_GATEWAY_MODEL


def _embedding_model() -> str:
    return os.environ.get("MEMOTRON_EMBEDDING_MODEL", "").strip() or DEFAULT_GATEWAY_EMBEDDING_MODEL


class ResilientEmbeddingTransport:
    """Retrying + memoizing wrapper around the gateway embedding transport.

    Driver-side mitigation for the two engine issues documented in
    ingest/README.md §9.1 and §9.3. It wraps, and never replaces, a real
    ``OpenAICompatibleEmbeddingTransport`` -- every vector still comes from
    ``text-embedding-3`` over the gateway.

    **Memo.** ``DreamEngine._entity_link_signals`` (``dreaming.py:5140``) embeds
    the mention name once per candidate node even though the value is
    candidate-independent, so a single episode issues the same embedding request
    dozens of times. Embedding is a pure function of (model, text), so an
    in-process LRU makes every repeat free. This collapses the engine's
    O(mentions x inventory) network fan-out to O(distinct strings) without
    touching ``src/`` and without changing a single stored vector.

    **Retry.** ``OpenAICompatibleEmbeddingTransport`` states plainly: "No retry
    framework: transient failures surface as ValueError." A single
    ``RemoteDisconnected`` from the gateway therefore aborts an entire formation
    run, and because the run never reaches its checkpoint, all work since the last
    checkpoint is discarded. That is exactly how the first mitigated pilot run
    died at episode ~14 of 37. Bounded exponential backoff makes ingest survivable.

    ``identifier`` is delegated verbatim -- it is the vector-space guard the
    engine uses to refuse mixed-space cosine, and must never be synthesized here.
    """

    def __init__(
        self,
        inner: OpenAICompatibleEmbeddingTransport,
        *,
        attempts: int = 5,
        base_delay: float = 1.5,
        cache_size: int = 8192,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._base_delay = base_delay
        self._cache_size = cache_size
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self.hits = 0
        self.misses = 0
        self.retries = 0

    @property
    def identifier(self) -> str:
        return self._inner.identifier

    def _call(self, fn, *args):
        last: Exception | None = None
        for attempt in range(self._attempts):
            try:
                return fn(*args)
            except Exception as exc:  # transient gateway/network failure
                last = exc
                if attempt == self._attempts - 1:
                    break
                self.retries += 1
                time.sleep(self._base_delay * (2**attempt))
        raise RuntimeError(
            f"embedding failed after {self._attempts} attempts against "
            f"{self._inner.base_url} (model {self._inner.model}): {last}"
        ) from last

    def _remember(self, text: str, vector: list[float]) -> list[float]:
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)
        return vector

    def embed(self, text: str) -> list[float]:
        cached = self._cache.get(text)
        if cached is not None:
            self.hits += 1
            self._cache.move_to_end(text)
            return cached
        self.misses += 1
        return self._remember(text, self._call(self._inner.embed, text))

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        missing = [t for t in dict.fromkeys(texts) if t not in self._cache]
        if missing:
            self.misses += len(missing)
            for text, vector in zip(missing, self._call(self._inner.embed_batch, missing), strict=True):
                self._remember(text, vector)
        out: list[list[float]] = []
        for text in texts:
            vector = self._cache.get(text)
            if vector is None:  # cache evicted mid-batch; fetch it back
                vector = self._remember(text, self._call(self._inner.embed, text))
            else:
                self.hits += 1
            out.append(vector)
        return out

    def stats(self) -> str:
        total = self.hits + self.misses
        rate = (self.hits / total * 100) if total else 0.0
        return (
            f"embeddings: {self.misses} gateway calls, {self.hits} memo hits "
            f"({rate:.0f}% avoided), {self.retries} retries"
        )


def require_gateway_key() -> None:
    """Fail fast, naming only the variable -- never a value."""
    if not os.environ.get(GATEWAY_API_KEY_ENV, "").strip():
        raise SystemExit(
            f"{GATEWAY_API_KEY_ENV} is not set. The pilot has no deterministic fallback "
            f"path: extraction, dream-agent decisions, theme synthesis, and embeddings "
            f"all run against the JedAI Gateway. Export {GATEWAY_API_KEY_ENV} "
            f"(e.g. `set -a && . ./.env && set +a`) and retry."
        )


def build_transports() -> dict[str, object]:
    """Construct the four gateway transports used by the pilot."""
    require_gateway_key()
    base_url = gateway_base_url_from_env()
    chat, embed = _chat_model(), _embedding_model()
    return {
        "extraction_transport": OpenAICompatibleExtractionTransport(
            model=chat, base_url=base_url, api_key_env=GATEWAY_API_KEY_ENV
        ),
        "dream_agent_transport": OpenAICompatibleDreamAgentTransport(
            model=os.environ.get("MEMOTRON_DREAM_AGENT_MODEL", "").strip() or chat,
            base_url=base_url,
            api_key_env=GATEWAY_API_KEY_ENV,
        ),
        "embedding_transport": ResilientEmbeddingTransport(
            OpenAICompatibleEmbeddingTransport(model=embed, base_url=base_url, api_key_env=GATEWAY_API_KEY_ENV)
        ),
        "rollup_synthesis_transport": OpenAICompatibleSynthesisTransport(
            model=chat, base_url=base_url, api_key_env=GATEWAY_API_KEY_ENV
        ),
    }


def seal_tenant_credentials(client) -> dict[str, object]:
    """Seal the gateway credential onto the tenant, chat + embeddings together.

    .. warning::

       **Do not call this while the tenant id equals a TENANT scope's scope_id.**
       Measured on this pilot: ``set_tenant_llm_credentials(tenant_id="jedai-portal-kb")``
       calls ``get_or_create_governance_key(f"tenant:{tenant_id}",
       subject_key="llm_credentials")`` (``graph.py:3952``), which inserts a
       ``governance_keys`` row whose ``scope_key`` is ``tenant:jedai-portal-kb`` --
       byte-identical to ``MemoryScope(kind=TENANT, scope_id="jedai-portal-kb").key``.
       ``PropertyGraphStore.scope_content_is_protected`` (``graph.py:171``) tests
       only for the presence of ANY ``governance_keys`` row for that ``scope_key``,
       so it then reports the memory scope as crypto-shred content-protected.
       ``DreamEngine.content_embedding_transport`` (``dreaming.py:725``)
       consequently forces the scope onto the hermetic 256-dim
       ``LocalEmbeddingTransport`` for reads AND writes, receipted once per run as
       ``embedding_transport_downgraded`` with reason
       ``crypto_shred_scope_content_never_sent_to_embedding_endpoint``.

       Net effect: sealing the gateway credential silently reverts the tenant to
       trigram vectors -- exactly the Phase-0 blindness INGEST.md §5a measures --
       while the operator believes ``text-embedding-3`` is in use.

    This driver therefore does NOT seal. It passes explicit transports to
    ``Memotron(...)``, which are consulted directly, so sealed credentials are
    never read on this path. Sealing is only required for the runtime-resolution
    consumers (``memotron-local-platform``, the agent-memory MCP server).

    The engine-side fix is reported in ingest/README.md; it is not made here
    because this kit does not own ``src/``.
    """
    require_gateway_key()
    base_url = gateway_base_url_from_env()
    return client.graph.set_tenant_llm_credentials(
        tenant_id=TENANT_ID,
        provider="litellm",
        api_key=os.environ[GATEWAY_API_KEY_ENV],
        base_url=base_url,
        model=_chat_model(),
        embedding_provider="litellm",
        embedding_base_url=base_url,
        embedding_model=_embedding_model(),
    )


def assert_embeddings_live(client) -> None:
    """Fail fast if this graph would silently downgrade to trigram vectors.

    Guards the standing 'never deterministic' rule: a content-protected scope
    forces ``LocalEmbeddingTransport`` regardless of what transport was passed in.
    """
    downgraded = [scope.key for scope in SCOPES.values() if client.graph.scope_content_is_protected(scope.key)]
    if downgraded:
        raise SystemExit(
            "FAILED: these KB scopes are marked content-protected, so every embedding "
            "would silently downgrade to the hermetic 256-dim trigram transport instead "
            f"of {_embedding_model()}:\n  " + "\n  ".join(downgraded) + "\n"
            "This graph was built by a version of the pilot that sealed tenant "
            "credentials. Run `uv run ingest/sync.py --reset` and re-ingest."
        )


def build_client(*, graph_path: Path = GRAPH_PATH):
    """Open the isolated pilot graph with every gateway transport attached."""
    from memotron import Memotron

    assert_isolation(graph_path)
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    client = Memotron(graph_path=graph_path, config=kb_config(), **build_transports())
    # Deliberately NOT seal_tenant_credentials(client) -- see its docstring.
    assert_embeddings_live(client)
    return client


# --------------------------------------------------------------------------
# --show
# --------------------------------------------------------------------------


def show() -> None:
    config = kb_config()
    motive = config.memory_bank.motive(MOTIVE_NAME)
    instructions = config.instruction_set(INSTRUCTION_SET_NAME)
    key_present = bool(os.environ.get(GATEWAY_API_KEY_ENV, "").strip())

    print("=" * 72)
    print("EFFECTIVE POLICY -- ingest/kb_config.py")
    print("=" * 72)
    print("\n[identity + isolation]")
    print(f"  tenant_id            {TENANT_ID}")
    print(f"  graph_path           {GRAPH_PATH}")
    print(f"  manifest             {MANIFEST_PATH}")
    print(f"  protected graph      {PROTECTED_GRAPH}  (never opened)")
    print(f"  external scope       {EXTERNAL_SCOPE.key}")
    print(f"  internal scope       {INTERNAL_SCOPE.key}   (visibility: jedai)")

    print("\n[gateway -- names only, never values]")
    print(f"  key env var          {GATEWAY_API_KEY_ENV}  ({'present' if key_present else 'MISSING'})")
    print(f"  base url             {gateway_base_url_from_env()}")
    print(f"  chat model           {_chat_model()}")
    print(f"  embedding model      {_embedding_model()}  (3072 dims; sealed before episode 1)")
    print("  transports           extraction, dream-agent, embeddings, rollup-synthesis")

    print("\n[instruction set]")
    print(f"  name                 {instructions.name}")
    print(f"  entity properties    {', '.join(ENTITY_PROPERTIES)}")
    print(f"  strict_properties    {instructions.node_instructions[0].strict_properties}")
    for rel in instructions.relationship_instructions:
        print(
            f"    {rel.type:<12} -> {rel.memory_type.value:<12} "
            f"{rel.cardinality.value:<14} min_conf={rel.min_confidence}"
        )

    print("\n[motive]")
    print(f"  name                 {motive.name}")
    print(f"  allowed types        {', '.join(t.value for t in motive.allowed_memory_types)}")
    rollup_ok = MemoryType.ROLLUP in motive.allowed_memory_types
    print(f"  rollup allowed       {rollup_ok}  <- False gates rollup consolidation off")
    print(f"  min_salience         {motive.salience_rubric.min_salience}")
    print(f"  max_memories/episode {motive.salience_rubric.max_memories_per_episode}")
    print(f"  prompt profile       {motive.prompt_profile}@{motive.prompt_profile_version}")

    print("\n[dedup -- INGEST.md §4a measured max distinct-ID cosine = 0.895]")
    print(f"  global               {config.dedup.cosine_threshold}")
    for memory_type, threshold in sorted(config.dedup.memory_type_thresholds.items()):
        margin = threshold - 0.895
        print(f"    {memory_type:<12} {threshold}   margin over 0.895 = {margin:+.3f}")

    print("\n[supersession]")
    print(f"  corroboration_margin   {config.supersession.corroboration_margin}")
    print(
        f"  corroboration_required {config.supersession.corroboration_required}   <- doc edit flips truth in ONE sync"
    )

    consolidation = next(j for j in config.jobs if j.kind is DreamJobKind.CONSOLIDATION)
    rollup_policy = consolidation.rollup_consolidation_policy
    print("\n[consolidation]")
    print(f"  cluster_threshold      {rollup_policy.cluster_threshold}")
    print(f"  min_cluster_size       {rollup_policy.min_cluster_size}")
    print(f"  max_depth              {rollup_policy.max_depth}")
    print(f"  cross_prefix_dup       {rollup_policy.cross_prefix_duplicate_threshold}   (T17 sweep)")
    print(f"  rollup synth model   {config.consolidation_profiles[0].model_identifier}   (T19)")

    print("\n[entity resolution -- INGEST.md's proposed 'WS-A', already landed]")
    print(f"  enabled                {config.entity_resolution.enabled}")
    print(f"  operator synonyms      {len(config.entity_resolution.synonyms)}")
    print(f"  auto_link_threshold    {config.entity_resolution.auto_link_threshold}")
    print(f"  review_threshold       {config.entity_resolution.review_threshold}")
    print(
        f"  max_pair_scan          {config.entity_resolution.max_pair_scan}"
        "   (engine default; affordable only via the embedding memo, README §9.1)"
    )
    print(f"  distinct-ID canaries   {len(DISTINCT_ID_CANARIES)}  (must never merge)")

    print("\n[predicate canonicalization -- T16]")
    print(f"  enabled                {config.predicate_canonicalization.enabled}")
    print(f"  embedding_threshold    {config.predicate_canonicalization.embedding_threshold}")
    print("=" * 72)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="JedAI portal KB tenant configuration")
    parser.add_argument("--show", action="store_true", help="print the effective policy")
    args = parser.parse_args(argv)
    if args.show:
        show()
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
