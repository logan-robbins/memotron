from __future__ import annotations

from memotron.config._actionability import (
    _FIELD_PATH_PATTERN as _FIELD_PATH_PATTERN,
)
from memotron.config._actionability import (
    DEFAULT_BEHAVIOUR_MARKERS as DEFAULT_BEHAVIOUR_MARKERS,
)
from memotron.config._actionability import (
    DEFAULT_EMPTY_JUSTIFICATIONS as DEFAULT_EMPTY_JUSTIFICATIONS,
)
from memotron.config._actionability import (
    DEFAULT_INVENTORY_NOUNS as DEFAULT_INVENTORY_NOUNS,
)
from memotron.config._actionability import (
    DEFAULT_INVENTORY_PREDICATES as DEFAULT_INVENTORY_PREDICATES,
)
from memotron.config._actionability import (
    ActionabilityPolicy as ActionabilityPolicy,
)
from memotron.config._actionability import (
    MissingJustification as MissingJustification,
)
from memotron.config._actionability import (
    _actionability_tokens as _actionability_tokens,
)
from memotron.config._actionability import (
    _has_field_path as _has_field_path,
)
from memotron.config._actionability import (
    _justification_defect as _justification_defect,
)
from memotron.config._actionability import (
    _phrase_in_tokens as _phrase_in_tokens,
)
from memotron.config._actionability import (
    actionability_violation as actionability_violation,
)
from memotron.config._actionability import (
    inventory_screen_violation as inventory_screen_violation,
)
from memotron.config._claim_mode import (
    _CLAIM_MODE_MEMORY_TYPES as _CLAIM_MODE_MEMORY_TYPES,
)
from memotron.config._claim_mode import (
    _CLAIM_MODE_STANCES as _CLAIM_MODE_STANCES,
)
from memotron.config._claim_mode import (
    RELATIONSHIP_TYPE_MEMORY_TYPE_MAP as RELATIONSHIP_TYPE_MEMORY_TYPE_MAP,
)
from memotron.config._claim_mode import (
    claim_mode_stance as claim_mode_stance,
)
from memotron.config._claim_mode import (
    default_claim_mode_for_memory_type as default_claim_mode_for_memory_type,
)
from memotron.config._claim_mode import (
    validate_claim_mode_for_memory_type as validate_claim_mode_for_memory_type,
)
from memotron.config._control_plane import (
    MemoryControlPlane as MemoryControlPlane,
)
from memotron.config._dream_config import (
    DreamConfig as DreamConfig,
)
from memotron.config._dream_config import (
    DreamJob as DreamJob,
)
from memotron.config._dream_config import (
    _dedupe_prompt_profiles as _dedupe_prompt_profiles,
)
from memotron.config._dream_config import (
    default_config as default_config,
)
from memotron.config._governance import (
    AuditVerbosity as AuditVerbosity,
)
from memotron.config._governance import (
    ErasureBehavior as ErasureBehavior,
)
from memotron.config._governance import (
    GovernancePolicy as GovernancePolicy,
)
from memotron.config._governance import (
    PiiSensitivity as PiiSensitivity,
)
from memotron.config._instructions import (
    DEFAULT_EXTRACTION_SYSTEM_PROMPT as DEFAULT_EXTRACTION_SYSTEM_PROMPT,
)
from memotron.config._instructions import (
    DEFAULT_IMPORTANCE_WEIGHTS as DEFAULT_IMPORTANCE_WEIGHTS,
)
from memotron.config._instructions import (
    RESERVED_NODE_PROPERTIES as RESERVED_NODE_PROPERTIES,
)
from memotron.config._instructions import (
    UNIVERSAL_NODE_PROPERTIES as UNIVERSAL_NODE_PROPERTIES,
)
from memotron.config._instructions import (
    DreamAgentConfig as DreamAgentConfig,
)
from memotron.config._instructions import (
    DreamInstructionSet as DreamInstructionSet,
)
from memotron.config._instructions import (
    DreamPromptOverride as DreamPromptOverride,
)
from memotron.config._instructions import (
    DreamPromptProfile as DreamPromptProfile,
)
from memotron.config._instructions import (
    FewShotExample as FewShotExample,
)
from memotron.config._instructions import (
    NodeInstruction as NodeInstruction,
)
from memotron.config._instructions import (
    RelationshipInstruction as RelationshipInstruction,
)
from memotron.config._instructions import (
    SalienceRubric as SalienceRubric,
)
from memotron.config._instructions import (
    _normalize_non_blank_text as _normalize_non_blank_text,
)
from memotron.config._instructions import (
    _normalize_prompt_lines as _normalize_prompt_lines,
)
from memotron.config._metadata import (
    MetadataFilterScalar as MetadataFilterScalar,
)
from memotron.config._metadata import (
    MetadataFilterValue as MetadataFilterValue,
)
from memotron.config._metadata import (
    _metadata_value_matches as _metadata_value_matches,
)
from memotron.config._metadata import (
    _validate_metadata_filter_scalar as _validate_metadata_filter_scalar,
)
from memotron.config._metadata import (
    metadata_matches_filter as metadata_matches_filter,
)
from memotron.config._metadata import (
    validate_metadata_filter as validate_metadata_filter,
)
from memotron.config._motive import (
    ConsolidationSynthesisProfile as ConsolidationSynthesisProfile,
)
from memotron.config._motive import (
    DreamMode as DreamMode,
)
from memotron.config._motive import (
    FormationContract as FormationContract,
)
from memotron.config._motive import (
    MemoryBank as MemoryBank,
)
from memotron.config._motive import (
    Motive as Motive,
)
from memotron.config._motive import (
    PromptPack as PromptPack,
)
from memotron.config._motive import (
    default_dream_modes as default_dream_modes,
)
from memotron.config._policies import (
    CoherencePolicy as CoherencePolicy,
)
from memotron.config._policies import (
    ConfidencePolicy as ConfidencePolicy,
)
from memotron.config._policies import (
    DedupPolicy as DedupPolicy,
)
from memotron.config._policies import (
    DreamContextPolicy as DreamContextPolicy,
)
from memotron.config._policies import (
    DreamEpisodeFilter as DreamEpisodeFilter,
)
from memotron.config._policies import (
    EntityResolutionPolicy as EntityResolutionPolicy,
)
from memotron.config._policies import (
    GrowthPolicy as GrowthPolicy,
)
from memotron.config._policies import (
    MemoryHealthPolicy as MemoryHealthPolicy,
)
from memotron.config._policies import (
    PredicateCanonicalizationPolicy as PredicateCanonicalizationPolicy,
)
from memotron.config._policies import (
    PruningPolicy as PruningPolicy,
)
from memotron.config._policies import (
    RetentionPolicy as RetentionPolicy,
)
from memotron.config._policies import (
    RollupConsolidationPolicy as RollupConsolidationPolicy,
)
from memotron.config._policies import (
    SupersessionPolicy as SupersessionPolicy,
)
from memotron.config._policies import (
    _default_authority_ranks as _default_authority_ranks,
)
from memotron.config._policies import (
    _default_class_precedence as _default_class_precedence,
)
from memotron.config._policies import (
    _default_memory_severity as _default_memory_severity,
)
from memotron.config._predicates import (
    DEFAULT_MAX_PREDICATE_WORDS as DEFAULT_MAX_PREDICATE_WORDS,
)
from memotron.config._predicates import (
    _contains_subsequence as _contains_subsequence,
)
from memotron.config._predicates import (
    _predicate_tokens as _predicate_tokens,
)
from memotron.config._predicates import (
    predicate_shape_violation as predicate_shape_violation,
)
from memotron.config._prompts import (
    _base_prompt_profiles as _base_prompt_profiles,
)
from memotron.config._prompts import (
    default_prompt_profiles as default_prompt_profiles,
)
from memotron.config._retrieval import (
    ProfilePolicy as ProfilePolicy,
)
from memotron.config._retrieval import (
    RetrievalContract as RetrievalContract,
)
from memotron.config._retrieval import (
    RetrievalPolicy as RetrievalPolicy,
)
from memotron.config._storage_env import (
    DEFAULT_OPERATIONAL_STORE_POOL_MAX_SIZE as DEFAULT_OPERATIONAL_STORE_POOL_MAX_SIZE,
)
from memotron.config._storage_env import (
    DEFAULT_OPERATIONAL_STORE_POOL_MIN_SIZE as DEFAULT_OPERATIONAL_STORE_POOL_MIN_SIZE,
)
from memotron.config._storage_env import (
    OPERATIONAL_STORE_APPLICATION_NAME_ENV as OPERATIONAL_STORE_APPLICATION_NAME_ENV,
)
from memotron.config._storage_env import (
    OPERATIONAL_STORE_DSN_ENV as OPERATIONAL_STORE_DSN_ENV,
)
from memotron.config._storage_env import (
    OPERATIONAL_STORE_POOL_MAX_SIZE_ENV as OPERATIONAL_STORE_POOL_MAX_SIZE_ENV,
)
from memotron.config._storage_env import (
    OPERATIONAL_STORE_POOL_MIN_SIZE_ENV as OPERATIONAL_STORE_POOL_MIN_SIZE_ENV,
)
from memotron.config._storage_env import (
    _pool_bound_from_env as _pool_bound_from_env,
)
from memotron.config._storage_env import (
    storage_settings_from_env as storage_settings_from_env,
)
from memotron.config._tenancy import (
    AgentMemoryPolicy as AgentMemoryPolicy,
)
from memotron.config._tenancy import (
    EffectiveMemoryPolicy as EffectiveMemoryPolicy,
)
from memotron.config._tenancy import (
    MemoryPrincipal as MemoryPrincipal,
)
from memotron.config._tenancy import (
    PrincipalRole as PrincipalRole,
)
from memotron.config._tenancy import (
    ScopeMemoryPolicy as ScopeMemoryPolicy,
)
from memotron.config._tenancy import (
    TenantMemoryPolicy as TenantMemoryPolicy,
)
from memotron.config._tenancy import (
    principal_from_registry_row as principal_from_registry_row,
)
from memotron.models import (
    DreamJobKind as DreamJobKind,
)
from memotron.models import (
    RelationshipCardinality as RelationshipCardinality,
)

# ---------------------------------------------------------------------------
# WS-7: Enterprise governance enums and GovernancePolicy
# ---------------------------------------------------------------------------


"""Node property keys the graph itself owns.

An extracted candidate may never set them — doing so would let a model overwrite
node identity or scope routing.  Declared here (not in ``extraction``) so the
rendered prompt and the validator that enforces it read from ONE definition.
"""


"""[0025] Node property keys allowed on EVERY label, regardless of that
label's own ``NodeInstruction.properties`` whitelist.

Cookbook-shaped output ([0025]) makes ``description`` a required, top-level
field of every entity the model extracts (see ``OUTPUT FORMAT`` in
``DEFAULT_EXTRACTION_SYSTEM_PROMPT``) — the opposite of ``RESERVED_NODE_
PROPERTIES``: those a candidate may NEVER set because the graph owns them,
this one every candidate SHOULD set and no per-label whitelist should have to
enumerate it to allow it.  Declared here, not in ``extraction``, for the same
reason ``RESERVED_NODE_PROPERTIES`` is: the validator that enforces this and
anything that documents the contract read from ONE definition.

WS-27 T1: ``aliases`` is promoted to first-class here for the SAME reason —
"GCX" and "guest content experience" must both resolve to one entity
regardless of which label extracted them, so every label accepts a list of
alternate surface names without having to enumerate the key in its own
``properties`` whitelist.  ``InstructionalExtractor`` reads the list off the
entity's properties at materialization time and writes each alias into
``entity_canon`` (see ``DreamEngine._register_declared_aliases``,
``dreaming.py``) — the SAME per-scope registry
:class:`EntityResolutionPolicy`'s similarity engine reads and writes.
"""


"""Word ceiling for an extracted ``predicate`` (:class:`DreamInstructionSet`).

The rendered contract asks for 1–3 words; the validator tolerates 4 so a
genuinely relational four-word phrase the repository's own corpora use
(``requires before contract signing``, ``requires before vendor onboarding``)
is never punished for the extra preposition.  Raise
``DreamInstructionSet.max_predicate_words`` for a domain with legitimately
longer relations.
"""


# ---------------------------------------------------------------------------
# WS-24: the actionability gate — "what step does knowing this let me skip?"
# ---------------------------------------------------------------------------

"""Head verbs of a *containment* relation — the shape an inventory entry takes.

Deliberately narrow.  Locational and behavioural relations that merely sound
similar (``stores``, ``retains``, ``lists``, ``publishes to``, ``lives at``)
are NOT here: an inventory entry is the claim that an artifact *possesses* a
named part, and only the possession verbs express it."""

"""Nouns that name a data-shape part rather than a thing an agent can act on."""

"""The EXCEPTION vocabulary.

A field DOES deserve memory when it *controls behaviour* — ``the authorship
frontmatter gates whether content is human-certified, check it before trusting
a page`` is a rule about what to do, not an inventory entry.  When any of these
markers appears in the fact or in its stated saved step, the inventory screen
stands down."""

"""Justifications that are grammatically present and semantically absent."""


# ---------------------------------------------------------------------------
# Operational Store selection from the deployment environment
# ---------------------------------------------------------------------------

"""Postgres DSN for the Operational Store.

Set only where a managed instance is configured (LATEST and above).  In the
pod the value is assembled by the kubelet from the individual Vault-injected
parts — see ``.helm/templates/secret-operational-store.yaml`` and
``docs/operational-store-deployment.md``.  Absent (the default, the local
substrate, and the hermetic test path), storage stays SQLite.
"""


"""Per-replica pool bounds; the Helm values carry the same defaults.

``replicas x max_size`` must stay inside the instance connection budget —
the arithmetic is in ``docs/operational-store-sizing.md``.
"""
