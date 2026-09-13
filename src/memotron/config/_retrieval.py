"""How memory is selected and rendered back into an agent's context.

RetrievalPolicy is the six-stage pipeline's parameter set; ProfilePolicy governs
what the rendered profile looks like and what fits in the token budget.
RetrievalContract is the reproducibility half -- its digest lands on receipts, so
a result set can be re-derived from (graph state, contract, query, as_of)."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class ProfilePolicy(BaseModel):
    max_static_facts: int = Field(default=20, ge=0)
    max_dynamic_facts: int = Field(default=8, ge=0)
    static_relationship_types: tuple[str, ...] | None = None
    dynamic_relationship_types: tuple[str, ...] | None = None
    dynamic_window_seconds: int | None = Field(default=None, ge=0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    include_metadata: bool = False
    render_mode: str = "legacy"
    """WS-5: Rendering mode.

    ``"legacy"`` (default) — byte-for-byte current output:
        ``"Memory profile for {scope.key}"`` / ``"Static facts:"`` / ``"Recent facts:"``
        flat list.  All new WS-5 callers that pass no render_mode get this invariant.

    ``"typed"`` — groups facts by memory_type under clear headings
        (identity / requirement / preference / directive / state / decision / incident).
        ROLLUP-typed facts sort first when WS-4 emits them; the ordering hook is keyed
        on memory_type so no WS-4 dependency is needed here.
    """
    per_type_max_facts: dict[str, int] = Field(default_factory=dict)
    """WS-5: Per-memory-type cap.  Empty = no per-type cap (legacy behaviour).

    Keys are MemoryType values (``"directive"``, ``"anchor"``, etc.).  When set,
    facts for that type are capped *before* the global max_static_facts /
    max_dynamic_facts cap is applied so that one type cannot crowd out another.

    Example: ``{"directive": 5}`` prevents more than 5 directive facts in the profile.
    """
    reference_mode: bool = False
    """WS-5: Just-in-time references mode (opt-in).

    When ``True``, facts that do not fit within ``token_budget`` (or any overflow facts)
    are returned as lightweight reference lines instead of being silently dropped.
    Each reference line has the form:
        ``[REF:{memory_type}] {relationship_uuid} — {one-line fact label}``
    An agent can expand a reference on demand via ``memory_evidence(relationship_uuid)``.

    When ``False`` (default), overflow facts are silently trimmed (legacy behaviour).
    References appear in the rendered_context after the typed or legacy section.
    """
    max_type_budget_share: float | None = Field(default=0.40, gt=0.0, le=1.0)
    """WS-24: ceiling on the token-budget share ANY single memory type may take.

    This replaces the WS-20 T24 per-type budget FLOOR (``floor_types`` /
    ``floor_share``), which guaranteed a named type at least 25% of the profile
    budget.  No knowledge-representation tradition attaches retrieval budget to
    a type, and the floor is the mechanism that turned a classification error
    into a retrieval failure: 148 field-inventory facts landed in ``identity``
    and then held the floor that ``identity`` guaranteed, crowding the
    genuinely actionable facts out of context at equal priority.

    A cap is the honest hedge in the same place — it can only ever *reduce* one
    type's claim on context, never manufacture relevance for a type that has
    nothing to say.  Excess share is redistributed to the types still under the
    cap; when only one type is present the cap is inert (there is nothing to
    redistribute to).  ``None`` disables it.

    Which facts are worth context remains the job of the query-conditioned
    rerank, and ``pinned`` remains the intentional always-include mechanism —
    per-fact and operator-set, which is the correct granularity.
    """

    @field_validator("static_relationship_types", "dynamic_relationship_types")
    @classmethod
    def normalize_relationship_type_filter(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is None:
            return None
        normalized: list[str] = []
        for relationship_type in value:
            normalized_type = relationship_type.strip().replace(" ", "_").upper()
            if not normalized_type:
                raise ValueError("profile relationship type filters cannot include blank values")
            normalized.append(normalized_type)
        return tuple(dict.fromkeys(normalized))

    @field_validator("per_type_max_facts")
    @classmethod
    def normalize_per_type_max_facts(cls, value: dict[str, int]) -> dict[str, int]:
        normalized: dict[str, int] = {}
        for memory_type, cap in value.items():
            key = memory_type.strip().lower()
            if not key:
                raise ValueError("per_type_max_facts keys cannot be blank")
            if cap < 0:
                raise ValueError("per_type_max_facts values must be non-negative")
            normalized[key] = cap
        return normalized

    @field_validator("render_mode")
    @classmethod
    def validate_render_mode(cls, value: str) -> str:
        allowed = {"legacy", "typed"}
        normalized = value.strip().lower()
        if normalized not in allowed:
            raise ValueError(f"render_mode must be one of {allowed!r}, got {value!r}")
        return normalized


class RetrievalPolicy(BaseModel):
    """WS-5: Deterministic policy for the six-stage retrieval pipeline.

    Stages: scope/temporal filter → lexical+vector candidate generation →
    seed-node resolution → bounded weighted expansion → current-truth and
    governance filtering → weighted rerank.  Every knob here is pinned into
    the RetrievalContract digest so a search is byte-replayable.

    All scoring is deterministic: lexical overlap, hashed-trigram cosine
    similarity (LocalEmbeddingTransport), and closed-form decay terms.  No
    model call happens at retrieval time.
    """

    # Stage 2 — candidate generation.
    lexical_weight: float = Field(default=1.0, ge=0.0)
    """Weight of the normalized lexical token-overlap score in relevance."""
    vector_weight: float = Field(default=1.0, ge=0.0)
    """Weight of the embedding cosine similarity in relevance."""
    vector_min_similarity: float = Field(default=0.35, ge=0.0, le=1.0)
    """Cosine floor below which a vector-only match is not a candidate."""
    max_candidates: int = Field(default=64, ge=1)
    """Cap on directly generated candidates before expansion (per search)."""

    # Stage 3 — seed-node resolution.
    max_seed_nodes: int = Field(default=8, ge=0)
    """Entity nodes resolved as expansion seeds.  0 disables expansion."""

    # Stage 4 — bounded weighted expansion (beam, never raw DFS).
    max_hops: int = Field(default=2, ge=0, le=4)
    """Expansion depth from seed nodes.  0 disables expansion."""
    beam_width: int = Field(default=16, ge=1)
    """Highest-scored frontier entries kept per hop."""
    entity_edge_weight: float = Field(default=0.6, ge=0.0, le=1.0)
    """Relevance carried across an entity co-mention hop."""
    rollup_member_edge_weight: float = Field(default=0.8, ge=0.0, le=1.0)
    """Relevance carried from a ROLLUP hit to its demoted members."""
    member_rollup_edge_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    """Relevance carried from a member fact up to its covering ROLLUP."""

    # Stage 6 — rerank.  final = relevance_weight * relevance
    #                          + confidence_weight * confidence
    #                          + recency_weight * recency_decay
    #                          + scope_priority_weight * scope_priority,
    # then multiplied by the per-type weight (default 1.0).
    relevance_weight: float = Field(default=1.0, ge=0.0)
    confidence_weight: float = Field(default=0.3, ge=0.0)
    recency_weight: float = Field(default=0.2, ge=0.0)
    recency_half_life_days: float = Field(default=30.0, gt=0.0)
    scope_priority_weight: float = Field(default=0.4, ge=0.0)
    """Weight of the caller's scope ordering in the rerank — a SOFT preference.

    ``scope_priority`` is ``(scope_count - scope_rank) / scope_count``, so it
    contributes to the score rather than partitioning by it: a strongly matching
    fact from a later scope CAN outrank a weakly matching fact from an earlier
    one.  That is deliberate (a precise agent-scope answer should beat a barely
    relevant customer-scope row), and it is a real behaviour change from the
    hard scope-rank partition that predated the weighted rerank.  Set
    :attr:`strict_scope_tiering` to restore the hard partition.

    Raising this weight approximates strict tiering but does not guarantee it.
    With ``N`` scopes the adjacent-rank gap in the scope term is
    ``scope_priority_weight / N``, while every other term is bounded by its own
    weight, so under the default per-type weights strict order holds only while
    ``scope_priority_weight > N × (relevance_weight + confidence_weight +
    recency_weight + utility_weight)`` — with the shipped defaults and three
    scopes, ``> 4.5``.  Any ``type_weights`` entry or Motive
    ``retrieval_budget_share`` boost multiplies the whole base and invalidates
    that bound, which is why the guarantee is a flag and not a number."""
    strict_scope_tiering: bool = Field(default=False)
    """Hard-partition results by scope rank instead of scoring scope in.

    ``False`` (default): one ranking across all scopes, with scope as a weighted
    term (see :attr:`scope_priority_weight`).  ``True``: every result from scope
    rank 0 precedes every result from rank 1, and so on, with the weighted score
    ordering results WITHIN each rank — the pre-rerank contract, restored
    exactly and independent of any weight arithmetic.

    Pinned rows (WS-20 T23) keep their reserved slots either way: guaranteed
    inclusion is an access guarantee, not a ranking preference.

    Pinned into the RetrievalContract like every other knob, so a search's
    ``retrieval_policy_digest`` records which ordering produced it."""
    utility_weight: float = Field(default=0.0, ge=0.0)
    """WS-15 T11: weight of the receipt-derived utility signal in the rerank.

    The utility term is ``use_need = (1 − e^(−use_stability)) × outcome_quality``
    read from the rebuildable :class:`MemoryUtilityProjection` for each final
    candidate — the SAME estimand retention uses, decayed against the SAME
    anchor as recency (``as_of`` or now) so a pinned search stays exactly
    reproducible.  The default ``0.0`` keeps ranking byte-identical to the
    pre-utility pipeline and skips the projection read entirely."""
    utility_weight_auto_floor_events: int | None = Field(default=None, ge=0)
    """WS-28 T2: event-volume floor that gates ``utility_weight``'s effective
    value on read, independent of the field above.

    ``None`` (default) — the auto-gate is OFF, byte-identical to the
    pre-WS-28 pipeline in every respect: a policy left at ``utility_weight
    == 0.0`` never even reads the utility projection (the read-path guard
    is ``policy.utility_weight == 0.0 and utility_weight_auto_floor_events
    is None`` short-circuiting before any event-plane access).  This is a
    hard requirement, not a style choice — it is what keeps a vanilla
    default policy from ever touching the event plane, which
    ``tests/test_retrieval.py::test_utility_weight_default_zero_keeps_ranking_byte_identical``
    locks in explicitly.

    Set to a non-negative integer to opt a scope into usage-aware rerank
    once real usage accumulates: when ``utility_weight`` is left at 0.0 (no
    explicit override) and the scope's summed
    ``MemoryUtilityProjection.impression_count`` across the search's final
    candidate set reaches this floor, ``utility_weight_when_unlocked``
    activates for that search and the flip is receipted
    (``ReceiptDecisionType.RETRIEVAL_UTILITY_AUTO_ENABLED``).  An explicit
    non-zero ``utility_weight`` always wins and this floor is never
    consulted — see :func:`memotron.retrieval.resolve_effective_utility_weight`."""
    utility_weight_when_unlocked: float = Field(default=0.2, ge=0.0)
    """WS-28 T2: the effective ``utility_weight`` once
    ``utility_weight_auto_floor_events`` is crossed (see above).  Unused
    unless that floor is configured; irrelevant when ``utility_weight`` is
    itself set to a non-zero override."""
    type_weights: dict[str, float] = Field(default_factory=dict)
    """Per-memory-type score multiplier.  Empty = every type weighs 1.0.

    Keys are MemoryType values (``"directive"``, ``"rollup"``, etc.).  A Motive
    with ``retrieval_budget_share`` set boosts its ``allowed_memory_types`` on
    top of these weights at rerank time (see client.search_context).
    """

    @field_validator("type_weights")
    @classmethod
    def normalize_type_weights(cls, value: dict[str, float]) -> dict[str, float]:
        normalized: dict[str, float] = {}
        for memory_type, weight in value.items():
            key = memory_type.strip().lower()
            if not key:
                raise ValueError("type_weights keys cannot be blank")
            if weight < 0:
                raise ValueError("type_weights values must be non-negative")
            normalized[key] = float(weight)
        return normalized


class RetrievalContract(BaseModel):
    """WS-5: Immutable, fully resolved retrieval policy for one search.

    Mirrors FormationContract at the read boundary: the contract is data, not
    prose — its digest becomes the ``retrieval_policy_digest`` stamped on every
    use event and on the search receipt, so a retrieval result set is exactly
    reproducible from (graph state, contract, query).

    ``schema_version`` history:

    - 1 — WS-5 original shape.
    - 2 — WS-15 T11: ``RetrievalPolicy`` gained ``utility_weight`` (stage-6
      rerank reads the receipt-derived utility projection).  EVERY contract
      digest changes at this bump, including searches that leave the new knob
      at its 0.0 default — the digest names the policy schema that produced
      the ranking, and that schema changed.  Use-event digests recorded under
      schema 1 remain internally consistent: a stored
      ``retrieval_policy_digest`` is compared only against contracts rebuilt
      from the same recorded policy dump, never across schema versions.
    """

    model_config = {"frozen": True}

    schema_version: int = 2
    operation: str = Field(min_length=1)
    scope_keys: tuple[str, ...] = Field(min_length=1)
    query_digest: str = Field(min_length=1)
    limit: int = Field(ge=1)
    policy: RetrievalPolicy
    embedding_identifier: str = Field(min_length=1)
    motive: str | None = None
    as_of: str | None = None
