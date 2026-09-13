"""Memotron MCP server — exposes the full SDK surface as MCP tools.

Transport: streamable-HTTP on /mcp (port 8000). Health probe on /health.

Environment variables
---------------------
MEMOTRON_GRAPH_PATH   Path to the SQLite graph file (default: :memory:).
                         Set to /data/memotron.sqlite in K8s (PVC mount).
LITELLM_API_KEY          JedAI Gateway virtual key. Enables live LLM extraction,
                         rollup synthesis, and dream-agent decisions. Without it
                         extraction is deterministic and rule-based.
LITELLM_API_BASE         Gateway base URL override, including /v1
                         (default: https://preview.jedai-gateway.wdprapps.disney.com/v1).
MEMOTRON_LLM_MODEL    Model override (undated gateway alias, e.g.
                         claude-haiku-4-5). Default: claude-haiku-4-5.
MEMOTRON_OPERATIONAL_STORE_DSN
                         Postgres DSN for the Operational Store. When set it
                         wins over MEMOTRON_GRAPH_PATH; unset keeps SQLite.
                         Assembled in the pod from Vault-injected parts, see
                         docs/operational-store-deployment.md.
MEMOTRON_OPERATIONAL_STORE_POOL_MIN_SIZE / _POOL_MAX_SIZE
                         Per-replica pool bounds (default 2 / 10).
MEMOTRON_OPERATIONAL_STORE_APPLICATION_NAME
                         application_name reported to pg_stat_activity.
MEMOTRON_REQUIRE_GATEWAY_IDENTITY
                         Arm the per-request identity guard (#126). UNSET BY
                         DEFAULT, and the default is the vulnerability: with it
                         unset `_scope` trusts the caller-supplied scope and any
                         caller reaching the ingress can read or write ANY
                         tenant's memory by naming it. Set to 1/true/yes to make
                         every request resolve its gateway virtual key to a
                         `key_principals` row and refuse scopes outside that
                         principal's allowlist. Enabling it with no rows bound
                         refuses everything -- see `memotron.mcp_auth`.
LOG_LEVEL                DEBUG | INFO | WARNING | ERROR (default: INFO).
"""

from __future__ import annotations

import json
from typing import Any

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import Response

from memotron import (
    ArtifactClass,
    ArtifactDirective,
    ConversationTurn,
    DirectiveStance,
    DreamConfig,
    DreamInstructionSet,
    DreamJob,
    DreamJobKind,
    Memotron,
    EpisodeType,
    MemoryScope,
    MemoryType,
    NodeInstruction,
    OutcomeVerdict,
    PersistentArtifact,
    ProfilePolicy,
    RelationshipCardinality,
    RelationshipInstruction,
    ScopeKind,
    UseEventKind,
    builtin_memory_bank,
)
from memotron.config import storage_settings_from_env
from memotron.mcp_auth import (
    authorize_scope,
    authorize_tenant,
    require_explicit_scope,
    require_fleet_wide_read,
    require_fleet_wide_write,
)
from memotron.runtime import build_transports_from_env

# ---------------------------------------------------------------------------
# FastMCP instance.  Construction is now identity-only: name, instructions, tools.
# Everything about *serving* -- bind address, Host allowlist, session mode, ASGI
# middleware -- belongs to `runtime.serve_mcp_http` and is decided in one call there.
#
# That split is not cosmetic.  Under the old SDK this object carried `host`, `port`
# and `transport_security`, the entry point reassigned `mcp.settings.host` afterwards,
# and the reassignment did NOT re-evaluate the security settings.  The server bound
# 0.0.0.0 while accepting localhost Host headers only, so every request through the
# ingress got 421 "Invalid Host header" while /health -- outside that middleware --
# kept returning 200 and every pod reported Ready.  That is what deployed `latest`
# did (image 0.1.0-a1daaf4, measured 2026-09-02).
#
# FastMCP 4 makes the mistake unrepresentable rather than merely fixed: there is no
# `.settings` to reassign, and `FastMCP(host=...)` raises
# `TypeError: FastMCP() no longer accepts 'host'` at import.  Verified against
# fastmcp 4.0.3.  Do not reintroduce a serving argument here.
# ---------------------------------------------------------------------------

#: This server's bind defaults. `0.0.0.0` because this is the deployed one and a
#: container must accept traffic from outside its own network namespace; the `Host`
#: allowlist, not the bind address, is what bounds who may reach it.
MCP_HOST = "0.0.0.0"
MCP_PORT = 8000

mcp = FastMCP(
    "memotron",
    instructions=(
        "Memotron enterprise agent memory. "
        "Use add_memory / add_session / add_episode / add_context to ingest facts, "
        "then search / semantic_search / profile to retrieve. "
        "run_due_dreams executes offline formation + consolidation + pruning. "
        "run_coherence_scan detects cross-artifact contradictions (skills vs. memory)."
    ),
)

# Shared client — initialised once (see build_client() called from entry point).
_client: Memotron | None = None


def get_client() -> Memotron:
    if _client is None:
        raise RuntimeError("Memotron client not initialised. Call build_client() first.")
    return _client


def build_client(graph_path: str = ":memory:") -> Memotron:
    """Create and install the shared Memotron client from environment."""
    global _client

    extraction, dream_agent = build_transports_from_env()

    instructions = DreamInstructionSet(
        name="default",
        node_instructions=(
            NodeInstruction(
                label="Entity",
                query="Durable people, organizations, agents, systems, and concepts.",
                properties=("kind", "role", "tier", "region", "system"),
                strict_properties=False,
            ),
        ),
        relationship_instructions=(
            RelationshipInstruction(
                type="REQUIRES",
                source_label="Entity",
                target_label="Entity",
                query="Hard requirements, compliance obligations, and approval gates.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
            ),
            RelationshipInstruction(
                type="PREFERS",
                source_label="Entity",
                target_label="Entity",
                query="Communication, format, and workflow preferences.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="SHOULD",
                source_label="Entity",
                target_label="Entity",
                query="Behavioral directives and operating lessons for agents.",
                cardinality=RelationshipCardinality.MULTI_ACTIVE,
            ),
            RelationshipInstruction(
                type="IS",
                source_label="Entity",
                target_label="Entity",
                query="Identity facts: role, title, affiliation, status.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.ANCHOR,
            ),
            RelationshipInstruction(
                type="DECIDES",
                source_label="Entity",
                target_label="Entity",
                query="Architectural, product, and operational decisions.",
                cardinality=RelationshipCardinality.SINGLE_ACTIVE,
                memory_type=MemoryType.DECISION,
            ),
        ),
    )

    config = DreamConfig(
        instruction_sets=(instructions,),
        memory_bank=builtin_memory_bank(),
        jobs=(
            DreamJob(
                name="formation-default",
                kind=DreamJobKind.FORMATION,
                cadence_seconds=60,
                motive="build-user-profile",
            ),
            DreamJob(
                name="consolidation-default",
                kind=DreamJobKind.CONSOLIDATION,
                cadence_seconds=300,
                rollup_consolidation=True,
            ),
            DreamJob(
                name="pruning-default",
                kind=DreamJobKind.PRUNING,
                cadence_seconds=600,
            ),
        ),
        # Postgres Operational Store when the deployment injects a DSN;
        # None (the default) leaves the client on the SQLite graph_path below.
        storage=storage_settings_from_env(),
    )

    kwargs: dict[str, Any] = {
        "graph_path": graph_path,
        "config": config,
        "extraction_transport": extraction,
    }
    if dream_agent is not None:
        kwargs["dream_agent_transport"] = dream_agent

    _client = Memotron(**kwargs)
    return _client


# ---------------------------------------------------------------------------
# Health route (K8s liveness / readiness probes)
# ---------------------------------------------------------------------------


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return Response("ok", media_type="text/plain")


@mcp.tool()
async def configure_tenant_llm(
    tenant_id: str,
    provider: str,
    api_key: str,
    base_url: str = "",
    model: str = "",
) -> str:
    """Store a tenant LLM API key and apply it to the running MCP platform."""

    # Takes a caller-named tenant AND a base_url, so naming another tenant redirects
    # that tenant's extraction traffic to a caller-chosen endpoint on its next dream
    # cycle. Not addressable by a MemoryScope, so `_scope` cannot cover it.
    authorize_tenant(tenant_id, operation="configure_tenant_llm")

    result = get_client().configure_tenant_llm_credentials(
        tenant_id=tenant_id,
        provider=provider,
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
    return json.dumps(result)


@mcp.tool()
async def tenant_llm_status(tenant_id: str) -> str:
    """Return non-secret status for one tenant's stored LLM credentials."""

    authorize_tenant(tenant_id, operation="tenant_llm_status")

    return json.dumps(get_client().tenant_llm_credential_state(tenant_id))


@mcp.tool()
async def clear_tenant_llm(tenant_id: str) -> str:
    """Remove one tenant's stored LLM credentials."""

    authorize_tenant(tenant_id, operation="clear_tenant_llm")

    return json.dumps({"cleared": get_client().clear_tenant_llm_credentials(tenant_id)})


# ---------------------------------------------------------------------------
# Scope helper
# ---------------------------------------------------------------------------


#: Tools that legitimately do NOT resolve a caller-named scope, and therefore never reach
#: ``_scope``'s authorization check. Enumerated so that adding a tool which bypasses the
#: guard fails a test rather than silently escaping it -- see
#: ``tests/test_mcp_scope_guard.py::test_every_tool_is_scoped_or_declared``.
#:
#: These are NOT safe by virtue of being listed. Three are fleet-wide across every scope
#: and three take a ``tenant_id``, which is a different authorization axis entirely --
#: ``configure_tenant_llm`` WRITES a sealed credential and ``clear_tenant_llm`` DELETES one,
#: both against a caller-named tenant. Authenticating at the door does not authorize them;
#: that is tracked as the remainder of #126.
#:
#: ``clear_tenant_llm`` is here because the test found it, not because a human did. It was
#: missing from the set this constant was first written with: that list came from parsing
#: the source for tools whose signature omits ``scope_kind``, and the parser reported five.
#: Reading the registry the server actually publishes reported six. The one it lost is the
#: destructive one.
_CROSS_SCOPE_TOOLS: frozenset[str] = frozenset(
    {
        "configure_tenant_llm",  # takes tenant_id; writes a sealed credential
        "tenant_llm_status",  # takes tenant_id
        "clear_tenant_llm",  # takes tenant_id; DELETES that tenant's credential
        "dream_history",  # fleet-wide across every scope
        "dream_decisions",  # fleet-wide
    }
)


#: Tools that DECLARE ``scope_kind`` but default it to ``""`` and read the empty pair as
#: *every scope in the store*. They pass the "is it scoped" tripwire and can still skip
#: ``_scope`` entirely by omitting the argument -- a guard whose test is satisfied by the
#: exact move that walks around it. ``mcp_auth.require_explicit_scope`` refuses the
#: omission when the identity guard is armed; this constant is what keeps the list honest.
_OPTIONAL_SCOPE_TOOLS: frozenset[str] = frozenset(
    {
        "run_due_dreams",
        "run_dream_job",
        "coherence_incidents",
        # Moved out of `_CROSS_SCOPE_TOOLS` by B3, which gave it a scope. It guards the
        # OMISSION with `require_fleet_wide_read`/`_write` rather than
        # `require_explicit_scope`, because a legitimate ADMIN fleet-wide sweep still
        # exists and those two are the stricter guard for it -- the write arm refuses even
        # when identity is disarmed, which `require_explicit_scope` does not.
        "remediate_coherence",
    }
)


def _scope(scope_kind: str, scope_id: str) -> MemoryScope:
    """Resolve a caller-named scope, and refuse it if the caller may not name it.

    THE choke point: 33 of 39 tools build their scope here, so one authorization call
    covers all of them with no signature changes. With
    ``MEMOTRON_REQUIRE_GATEWAY_IDENTITY`` unset this is byte-for-byte the previous
    behaviour.

    "33 of 39 tools reach the store only through here" would be too strong, and an earlier
    version of this docstring said it. Three of the 33 (``_OPTIONAL_SCOPE_TOOLS``) can reach
    the store while skipping this function entirely, by omitting the scope arguments -- which
    is precisely why ``require_explicit_scope`` exists. A guard's own docstring overstating
    its reach is how the next reader stops looking for the gap.
    """
    kind = ScopeKind(scope_kind)
    scope = MemoryScope(kind=kind, scope_id=scope_id)
    authorize_scope(scope)
    return scope


# ---------------------------------------------------------------------------
# Memory ingestion tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def add_memory(
    subject: str,
    predicate: str,
    object: str,
    relationship_type: str,
    scope_kind: str,
    scope_id: str,
    confidence: float = 0.85,
    source_text: str = "",
) -> str:
    """Add an exact memory fact to the graph immediately (bypass extraction).

    scope_kind: 'user' | 'customer' | 'agent' | 'tenant'
    relationship_type: e.g. 'REQUIRES', 'PREFERS', 'SHOULD', 'IS', 'DECIDES'
    Returns JSON with relationship_uuid, episode_uuid.
    """
    scope = _scope(scope_kind, scope_id)
    result = await get_client().add_memory(
        subject=subject,
        predicate=predicate,
        object=object,
        relationship_type=relationship_type,
        scope=scope,
        confidence=confidence,
        source_text=source_text or None,
    )
    return json.dumps(
        {
            "relationship_uuid": result.relationship_uuid,
            "episode_uuid": result.episode_uuid,
            "queued_for_dreaming": result.queued_for_dreaming,
        }
    )


@mcp.tool()
async def add_session(
    name: str,
    turns_json: str,
    scope_kind: str,
    scope_id: str,
    motive: str = "",
    turns_per_episode: int = 8,
) -> str:
    """Ingest a conversation session as windowed episodes for offline extraction.

    turns_json: JSON array of {role, content, timestamp?} objects.
                timestamp is an ISO-8601 string; omit for current time.
    motive: optional Motive name from the built-in bank, e.g. 'build-user-profile'.
    Returns JSON with session_id, episodes_created, episode_uuids.
    """
    from datetime import UTC, datetime

    raw_turns = json.loads(turns_json)
    turns = []
    for t in raw_turns:
        ts_str = t.get("timestamp")
        ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(UTC)
        turns.append(ConversationTurn(role=t["role"], content=t["content"], timestamp=ts))

    scope = _scope(scope_kind, scope_id)
    result = await get_client().add_session(
        name=name,
        turns=turns,
        scope=scope,
        turns_per_episode=turns_per_episode,
        **({"motive": motive} if motive else {}),
    )
    return json.dumps(
        {
            "session_id": result.session_id,
            "episodes_created": result.episodes_created,
            "episode_uuids": result.episode_uuids,
        }
    )


@mcp.tool()
async def add_episode(
    name: str,
    episode_body: str,
    scope_kind: str,
    scope_id: str,
    source: str = "text",
    motive: str = "",
    trusted: bool = True,
) -> str:
    """Queue a raw episode for offline formation by the Dream Engine.

    source: 'text' | 'json' | 'message'
    For JSON source, episode_body must be a JSON string with a 'memories' array.
    Returns JSON with episode_uuid, queued_for_dreaming.
    """
    scope = _scope(scope_kind, scope_id)
    kwargs: dict[str, Any] = {
        "name": name,
        "episode_body": episode_body,
        "source": EpisodeType(source),
        "scope": scope,
        "trusted": trusted,
    }
    if motive:
        kwargs["motive"] = motive
    result = await get_client().add_episode(**kwargs)
    return json.dumps(
        {
            "episode_uuid": result.episode_uuid,
            "queued_for_dreaming": result.queued_for_dreaming,
        }
    )


@mcp.tool()
async def add_context(
    name: str,
    content: str,
    scope_kind: str,
    scope_id: str,
    trusted: bool = True,
    custom_id: str = "",
) -> str:
    """Ingest a raw document; Memotron chunks it into episodes for dreaming.

    Useful for knowledge-base ingestion, agent settings files, or policy docs.
    Returns JSON with chunk_count, episode_uuids.
    """
    scope = _scope(scope_kind, scope_id)
    result = await get_client().add_context(
        name=name,
        content=content,
        scopes=[scope],
        trusted=trusted,
        **({"custom_id": custom_id} if custom_id else {}),
    )
    return json.dumps(
        {
            "chunk_count": result.chunks_created,
            "episode_uuids": result.episode_uuids,
        }
    )


# ---------------------------------------------------------------------------
# Retrieval tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def search(
    query: str,
    scope_kind: str,
    scope_id: str,
    limit: int = 20,
    relationship_types: str = "",
) -> str:
    """Search active memory facts by keyword.

    relationship_types: comma-separated list to restrict, e.g. 'REQUIRES,PREFERS'.
    Returns JSON array of {subject, predicate, object, relationship_type,
    confidence, observed_count, relationship_uuid, status}.
    """
    scope = _scope(scope_kind, scope_id)
    types = [r.strip() for r in relationship_types.split(",") if r.strip()] or None
    results = await get_client().search(
        query=query,
        scope=scope,
        limit=limit,
        **({"relationship_types": set(types)} if types else {}),
    )
    return json.dumps(
        [
            {
                "subject": r.subject,
                "predicate": r.predicate,
                "object": r.object,
                "relationship_type": r.relationship_type,
                "confidence": r.confidence,
                "observed_count": r.observed_count,
                "relationship_uuid": r.relationship_uuid,
                "status": r.status,
            }
            for r in results
        ]
    )


@mcp.tool()
async def semantic_search(
    query: str,
    scope_kind: str,
    scope_id: str,
    limit: int = 10,
    min_similarity: float = 0.5,
) -> str:
    """Semantic search using embedding cosine over stored relationship vectors.

    Returns JSON array of {subject, predicate, object, similarity, relationship_uuid}.
    """
    scope = _scope(scope_kind, scope_id)
    results = await get_client().semantic_search(
        query=query,
        scope=scope,
        limit=limit,
        min_similarity=min_similarity,
    )
    return json.dumps(
        [
            {
                "subject": r.subject,
                "predicate": r.predicate,
                "object": r.object,
                "relationship_type": r.relationship_type,
                "similarity": r.similarity if hasattr(r, "similarity") else None,
                "relationship_uuid": r.relationship_uuid,
            }
            for r in results
        ]
    )


@mcp.tool()
async def archived_matches(
    query: str,
    scope_kind: str,
    scope_id: str,
    semantic: bool = False,
) -> str:
    """List archived (pruned) memories a query matches — without reviving any.

    Always a pure read, unlike search/semantic_search, which by default revive
    the archived memories they match (and report them the same way). Use this
    to see what is recoverable without touching it, then
    restore_archived_memory for the exact relationship_uuid you want back.
    Returns JSON with count, disposition, and matches[{relationship_uuid, fact,
    pruned_at, pruned_reason, prune_receipt_uuid}].
    """
    scope = _scope(scope_kind, scope_id)
    report = get_client().archived_matches(query=query, scope=scope, semantic=semantic)
    return json.dumps(report.model_dump(mode="json"))


@mcp.tool()
async def profile(
    scope_kind: str,
    scope_id: str,
    token_budget: int = 2000,
    render_mode: str = "typed",
    motive: str = "",
) -> str:
    """Get agent-ready memory context for a scope.

    render_mode: 'typed' (grouped by type, ROLLUP-first) | 'legacy' (flat).
    Returns JSON with rendered_context (ready for prompt injection), tokens_used,
    fact_count, static_facts, dynamic_facts.
    """
    scope = _scope(scope_kind, scope_id)
    policy = ProfilePolicy(render_mode=render_mode, reference_mode=True)
    kwargs: dict[str, Any] = {
        "scope": scope,
        "token_budget": token_budget,
        "policy": policy,
    }
    if motive:
        kwargs["motive"] = motive
    result = await get_client().profile(**kwargs)
    return json.dumps(
        {
            "rendered_context": result.rendered_context,
            "tokens_used": result.tokens_used,
            "fact_count": len(result.static_facts) + len(result.dynamic_facts),
            "static_fact_count": len(result.static_facts),
            "dynamic_fact_count": len(result.dynamic_facts),
            "injected_items": [item.model_dump(mode="json") for item in result.injected_items],
            "reference_items": [item.model_dump(mode="json") for item in result.reference_items],
        }
    )


@mcp.tool()
async def record_memory_use(
    relationship_uuid: str,
    scope_kind: str,
    scope_id: str,
    kind: str,
    task_run_id: str,
    idempotency_key: str,
    rank: int | None = None,
    retrieval_score: float | None = None,
    candidate_set_size: int | None = None,
    context_budget_competition: int | None = None,
    retrieval_policy_digest: str = "",
) -> str:
    """Append a receipt-backed retrieved, injected, or cited_or_used event."""
    event = await get_client().record_memory_use(
        relationship_uuid=relationship_uuid,
        scope=_scope(scope_kind, scope_id),
        kind=UseEventKind(kind),
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
        rank=rank,
        retrieval_score=retrieval_score,
        candidate_set_size=candidate_set_size,
        context_budget_competition=context_budget_competition,
        retrieval_policy_digest=retrieval_policy_digest or None,
    )
    return event.model_dump_json()


@mcp.tool()
async def record_memory_outcome(
    use_id: str,
    scope_kind: str,
    scope_id: str,
    verdict: str,
    task_run_id: str,
    idempotency_key: str,
    judge_identity: str,
    judge_version: str,
    attribution_method: str = "cited_full_credit",
) -> str:
    """Append an outcome tied to one use event; this never mutates memory truth."""
    event = await get_client().record_memory_outcome(
        use_id=use_id,
        scope=_scope(scope_kind, scope_id),
        verdict=OutcomeVerdict(verdict),
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
        judge_identity=judge_identity,
        judge_version=judge_version,
        attribution_method=attribution_method,
    )
    return event.model_dump_json()


@mcp.tool()
async def memory_utility(scope_kind: str, scope_id: str, relationship_uuid: str = "") -> str:
    """Return replay-derived utility state, separated from memory truth fields."""
    projection = await get_client().memory_utility(
        scope=_scope(scope_kind, scope_id), relationship_uuid=relationship_uuid or None
    )
    return json.dumps([item.model_dump(mode="json") for item in projection])


@mcp.tool()
async def retrieval_negative_space(scope_kind: str, scope_id: str, task_run_id: str = "") -> str:
    """Show retrieved memories that were never injected or cited/used."""
    entries = await get_client().retrieval_negative_space(
        scope=_scope(scope_kind, scope_id), task_run_id=task_run_id or None
    )
    return json.dumps([entry.model_dump(mode="json") for entry in entries])


@mcp.tool()
async def entity_neighborhood(
    scope_kind: str,
    scope_id: str,
    entity: str,
    limit: int = 20,
    relationship_types: str = "",
) -> str:
    """Return graph edges connected to a named entity.

    Returns JSON array of {direction, relationship_type, subject, object,
    confidence, status, observed_count}.
    """
    scope = _scope(scope_kind, scope_id)
    types = {r.strip() for r in relationship_types.split(",") if r.strip()} or None
    edges = await get_client().entity_neighborhood(
        scope=scope,
        entity=entity,
        **({"relationship_types": types} if types else {}),
    )
    return json.dumps(
        [
            {
                "direction": e.direction,
                "relationship_type": e.relationship_type,
                "subject": e.subject,
                "object": e.object,
                "confidence": e.confidence,
                "status": e.status,
                "observed_count": e.observed_count,
                "relationship_uuid": e.relationship_uuid,
            }
            for e in edges[:limit]
        ]
    )


# ---------------------------------------------------------------------------
# Evidence and lineage tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def memory_evidence(
    relationship_uuid: str,
    scope_kind: str,
    scope_id: str,
) -> str:
    """Retrieve raw source episodes that created or reinforced a graph fact.

    Returns JSON with the fact plus its source episode bodies and metadata.
    """
    scope = _scope(scope_kind, scope_id)
    ev = await get_client().memory_evidence(
        relationship_uuid=relationship_uuid,
        scope=scope,
    )
    return json.dumps(
        {
            "subject": ev.subject,
            "predicate": ev.predicate,
            "object": ev.object,
            "confidence": ev.confidence,
            "observed_count": ev.observed_count,
            "episode_count": len(ev.episodes),
            "episodes": [{"body": ep.body[:500], "created_at": ep.created_at.isoformat()} for ep in ev.episodes],
        }
    )


@mcp.tool()
async def truth_timeline(
    scope_kind: str,
    scope_id: str,
    subject: str,
    predicate: str,
    relationship_type: str,
) -> str:
    """Return the full lineage for a scoped subject/predicate truth slot.

    Useful for auditing why a current memory is active and what it replaced.
    Returns JSON array of {status, is_current, object, valid_from, valid_to,
    confidence, relationship_uuid, superseded_by_relationship_uuid}.
    """
    scope = _scope(scope_kind, scope_id)
    entries = await get_client().truth_timeline(
        scope=scope,
        subject=subject,
        predicate=predicate,
        relationship_type=relationship_type,
    )
    return json.dumps(
        [
            {
                "status": e.status,
                "is_current": e.is_current,
                "object": e.object,
                "valid_from": e.valid_from.isoformat() if e.valid_from else None,
                "valid_to": e.valid_to.isoformat() if e.valid_to else None,
                "confidence": e.confidence,
                "relationship_uuid": e.relationship_uuid,
                "superseded_by": e.superseded_by_relationship_uuid,
            }
            for e in entries
        ]
    )


@mcp.tool()
async def memory_evolution(
    scope_kind: str,
    scope_id: str,
) -> str:
    """Prove that dreaming is compressing and evolving memory, not just recording.

    Returns JSON proof with compression_ratio, semantic_dedup_rate, active_fact_count,
    episode_count, rollup_count, demoted_count, and evolution signals.
    """
    scope = _scope(scope_kind, scope_id)
    proof = await get_client().memory_evolution(scope=scope)
    return json.dumps(
        {
            "episode_count": proof.episode_count,
            "active_fact_count": proof.active_relationship_count,
            "context_visible_count": proof.context_visible_relationship_count,
            "rollup_count": proof.rollup_relationship_count,
            "demoted_count": proof.demoted_relationship_count,
            "compression_ratio": proof.compression_ratio,
            "semantic_dedup_rate": proof.semantic_dedup_rate,
            "per_type_active_counts": proof.per_type_active_counts,
            # `s.value` never existed. A signal carries `observed` (did it fire) and `count`
            # (how many). Emitting both rather than guessing which one "value" meant — the
            # tool raised before this, so there is no working payload shape to preserve.
            "signals": {s.name: {"observed": s.observed, "count": s.count} for s in proof.signals},
        }
    )


# ---------------------------------------------------------------------------
# Dream engine tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def run_due_dreams(
    scope_kind: str = "",
    scope_id: str = "",
) -> str:
    """Execute all due dream jobs (formation → consolidation → pruning).

    Pass scope_kind + scope_id to restrict to one scope; omit both for global run.
    Returns JSON with jobs_run, relationships_created, reinforced, superseded, pruned.
    """
    kwargs: dict[str, Any] = {}
    if scope_kind and scope_id:
        kwargs["scope"] = _scope(scope_kind, scope_id)
    else:
        require_explicit_scope("run_due_dreams")
    results = await get_client().run_due_dreams(**kwargs)
    # `results` is a DreamRunResult, not a list of job runs. `len()` on it raised TypeError,
    # so this tool was dead for every caller — the operator-facing way to trigger dreaming.
    # DreamRunResult exposes aggregates over job_runs; the two it lacks are summed here.
    totals = {
        "jobs_run": len(results.job_runs),
        "relationships_created": results.created_relationships,
        "reinforced": sum(r.reinforced_relationships for r in results.job_runs),
        "superseded": sum(r.superseded_relationships for r in results.job_runs),
        "pruned": sum(r.pruned_relationships for r in results.job_runs),
        "episodes_processed": results.processed_episodes,
    }
    return json.dumps(totals)


@mcp.tool()
async def run_dream_job(
    job_name: str,
    scope_kind: str = "",
    scope_id: str = "",
) -> str:
    """Force a specific dream job by name, bypassing cadence.

    job_name examples: 'formation-default', 'consolidation-default', 'pruning-default'.
    Returns the job run summary as JSON.
    """
    kwargs: dict[str, Any] = {"job_name": job_name}
    if scope_kind and scope_id:
        kwargs["scope"] = _scope(scope_kind, scope_id)
    else:
        require_explicit_scope("run_dream_job")
    result = await get_client().run_dream_job(**kwargs)
    return json.dumps(
        {
            # A DreamRunResult wraps job_runs; it has no job_name/job_kind of its own. The
            # named job is the single run inside it.
            "job_name": result.job_runs[0].job_name if result.job_runs else job_name,
            "job_kind": result.job_runs[0].job_kind if result.job_runs else None,
            "relationships_created": result.created_relationships,
            "reinforced": sum(r.reinforced_relationships for r in result.job_runs),
            "superseded": sum(r.superseded_relationships for r in result.job_runs),
            "pruned": sum(r.pruned_relationships for r in result.job_runs),
            "episodes_processed": result.processed_episodes,
            "decision_count": result.decision_count,
        }
    )


@mcp.tool()
async def dream_history(
    limit: int = 10,
    job_name: str = "",
) -> str:
    """List persisted dream job run records for audit and monitoring.

    Returns JSON array of {job_name, job_kind, run_at, episodes_processed,
    relationships_created, reinforced, superseded, pruned, decision_count}.
    """
    require_fleet_wide_read("dream_history")

    kwargs: dict[str, Any] = {"limit": limit}
    if job_name:
        kwargs["job_name"] = job_name
    records = await get_client().dream_history(**kwargs)
    return json.dumps(
        [
            {
                "job_name": r.job_name,
                "job_kind": r.job_kind,
                "run_at": r.ran_at.isoformat(),
                "episodes_processed": r.processed_episodes,
                "relationships_created": r.created_relationships,
                "reinforced": r.reinforced_relationships,
                "superseded": r.superseded_relationships,
                "pruned": r.pruned_relationships,
                "decision_count": r.decision_count,
            }
            for r in records
        ]
    )


@mcp.tool()
async def dream_decisions(
    limit: int = 20,
    job_name: str = "",
    agent_id: str = "",
) -> str:
    """List dream-agent decisions for audit (formation, consolidation, pruning, coherence).

    Returns JSON array of {job_name, job_kind, agent_id, decision_type, summary, created_at}.
    """
    require_fleet_wide_read("dream_decisions")

    kwargs: dict[str, Any] = {"limit": limit}
    if job_name:
        kwargs["job_name"] = job_name
    if agent_id:
        kwargs["agent_id"] = agent_id
    records = await get_client().dream_decisions(**kwargs)
    return json.dumps(
        [
            {
                "job_name": r.job_name,
                "job_kind": r.job_kind,
                "agent_id": r.agent_id,
                "decision_type": r.decision_type,
                "summary": r.summary,
                "created_at": r.ran_at.isoformat(),
            }
            for r in records
        ]
    )


@mcp.tool()
async def redream_epochs(scope_kind: str, scope_id: str) -> str:
    """WS-26 T5: list every epoch ever created for a scope, plus its HEAD.

    Mirrors the admin ``GET /api/epochs`` surface for operator MCP clients.
    Returns JSON {epochs: [...], active_epoch_id}.
    """
    scope = _scope(scope_kind, scope_id)
    client = get_client()
    epochs = await client.redream_epochs(scope=scope)
    active_epoch_id = await client.redream_active_epoch(scope=scope)
    return json.dumps({"epochs": epochs, "active_epoch_id": active_epoch_id})


@mcp.tool()
async def redream_diff(scope_kind: str, scope_id: str, epoch_id: str) -> str:
    """WS-26 T5: diff HEAD's current epoch against a recomputed branch epoch.

    Mirrors the admin ``GET /api/epochs/diff`` surface. Returns JSON
    {epoch_id, diff: {added, removed, changed, unchanged, ...registry deltas}}.
    Raises ValueError when epoch_id is blank (mirrors the admin 400).
    """
    if not epoch_id:
        raise ValueError("epoch_id is required")
    scope = _scope(scope_kind, scope_id)
    diff = await get_client().redream_diff(scope=scope, epoch_id=epoch_id)
    return json.dumps({"epoch_id": epoch_id, "diff": diff.model_dump(mode="json")})


# ---------------------------------------------------------------------------
# Memory correction tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def correct_memory(
    relationship_uuid: str,
    corrected_object: str,
    scope_kind: str,
    scope_id: str,
    reason: str = "operator_corrected",
) -> str:
    """Correct a graph fact — supersedes the old value and creates a corrected successor.

    Preserves full lineage; the old fact remains visible via truth_timeline.
    Returns JSON with new_relationship_uuid, superseded_relationship_uuid.
    """
    scope = _scope(scope_kind, scope_id)
    result = await get_client().correct_memory(
        relationship_uuid=relationship_uuid,
        corrected_object=corrected_object,
        scope=scope,
        reason=reason,
    )
    return json.dumps(
        {
            "new_relationship_uuid": result.corrected_relationship_uuid,
            # `superseded_relationship_uuid` never existed, and neither does a uuid for it:
            # `superseded_relationships` is a COUNT. Renamed the key to match what is actually
            # returned rather than inventing a uuid the model cannot supply. My first attempt
            # here wrapped it in list() on the assumption it held uuids; mypy caught that.
            "superseded_relationships": result.superseded_relationships,
        }
    )


@mcp.tool()
async def forget_memory(
    relationship_uuid: str,
    scope_kind: str,
    scope_id: str,
    reason: str = "operator_removed",
) -> str:
    """Soft-retire a graph fact — excluded from active retrieval but evidence is preserved.

    Use for bad or outdated memories. Never destroys source episodes or timeline.
    Returns JSON with forgotten_relationship_uuid, forgotten_at.
    """
    scope = _scope(scope_kind, scope_id)
    result = await get_client().forget_memory(
        relationship_uuid=relationship_uuid,
        scope=scope,
        reason=reason,
    )
    return json.dumps(
        {
            "forgotten_relationship_uuid": result.relationship_uuid,
            "forgotten_at": result.pruned_at.isoformat() if result.pruned_at else None,
        }
    )


@mcp.tool()
async def restore_archived_memory(
    relationship_uuid: str,
    scope_kind: str,
    scope_id: str,
    reason: str = "operator_restored",
) -> str:
    """Revive one archived (pruned) memory — the explicit inverse of forget_memory.

    Use this for a uuid reported in a search's archived matches. By default a
    search already revives what it matches; where the deployment sets
    pure_read_retrieval (retrieval takes no row locks, so it can run on more
    than one replica) this is the only way an archived memory comes back.
    Refuses a crypto-shredded or non-restorable memory. Returns JSON with
    relationship_uuid, status, restored_at, restore_receipt_uuid.
    """
    scope = _scope(scope_kind, scope_id)
    result = await get_client().restore_archived_memory(
        relationship_uuid=relationship_uuid,
        scope=scope,
        reason=reason,
        requested_by="operator",
    )
    return json.dumps(
        {
            "relationship_uuid": result.relationship_uuid,
            "status": result.status.value,
            "restored_at": result.restored_at.isoformat(),
            "restore_receipt_uuid": result.restore_receipt_uuid,
            "prune_receipt_uuid": result.prune_receipt_uuid,
            "ghost_regret_rate": result.ghost_regret_rate,
            "ghost_regret_alert": result.ghost_regret_alert,
        }
    )


# ---------------------------------------------------------------------------
# Coherence tools (WS-10)
# ---------------------------------------------------------------------------


@mcp.tool()
async def register_artifact(
    artifact_id: str,
    artifact_class: str,
    scope_kind: str,
    scope_id: str,
    location: str,
    author: str,
    self_authored: bool,
    updated_at_iso: str,
    directives_json: str,
) -> str:
    """Register a persistent live artifact (skill or file) for coherence scanning.

    artifact_class: 'skill' | 'file'
    directives_json: JSON array of {subject, instruction, stance} where
                     stance is 'require' | 'forbid' | 'prefer' | 'assert' | 'guide'.
    updated_at_iso: ISO-8601 timestamp of when the artifact was last modified.

    After registering one or more artifacts, call run_coherence_scan to detect
    escalation-windup and cross-artifact contradictions.
    """
    from datetime import datetime

    scope = _scope(scope_kind, scope_id)
    directives = [
        ArtifactDirective(
            subject=d["subject"],
            instruction=d["instruction"],
            stance=DirectiveStance(d["stance"]),
        )
        for d in json.loads(directives_json)
    ]
    artifact = PersistentArtifact(
        artifact_id=artifact_id,
        artifact_class=ArtifactClass(artifact_class),
        scope=scope,
        location=location,
        author=author,
        self_authored=self_authored,
        updated_at=datetime.fromisoformat(updated_at_iso),
        directives=directives,
    )
    stored = get_client().register_live_artifact(artifact)
    return json.dumps(
        {
            "registered": stored.artifact_id,
            "directive_count": len(stored.directives),
            "scope_key": stored.scope.key,
        }
    )


@mcp.tool()
async def record_artifact_outcome(
    artifact_id: str,
    scope_kind: str,
    scope_id: str,
    verdict: str,
    task_run_id: str,
    idempotency_key: str,
    metadata_json: str = "{}",
) -> str:
    """Record a governed-run outcome for a registered skill/file artifact.

    Positive outcomes raise contribution; negative/corrected outcomes lower it.
    Once the policy's minimum evidence is met, a sufficiently poor score moves
    the artifact into quarantine review and removes it from live projection.
    """
    projection = await get_client().record_live_artifact_outcome(
        scope=_scope(scope_kind, scope_id),
        artifact_id=artifact_id,
        verdict=OutcomeVerdict(verdict),
        task_run_id=task_run_id,
        idempotency_key=idempotency_key,
        metadata=json.loads(metadata_json),
    )
    return projection.model_dump_json()


@mcp.tool()
async def run_coherence_scan(
    scope_kind: str,
    scope_id: str,
) -> str:
    """Scan a scope for cross-artifact coherence issues.

    Detects: ESCALATION_WINDUP (memory escalating against a stale governing skill/file)
             CROSS_ARTIFACT_CONTRADICTION (conflicting stances across artifact classes).

    Marks implicated memories with coherence_hold (anti-windup actuator).
    Attributes the stale governing artifact as the culprit and proposes its repair.

    Register artifacts with register_artifact before calling this.
    Returns JSON with incident_count and incidents array.
    """
    scope = _scope(scope_kind, scope_id)
    report = await get_client().run_coherence_scan(scope=scope)
    return json.dumps(
        {
            "incident_count": report.incident_count,
            "incidents": [
                {
                    "kind": i.kind.value,
                    "subject": i.subject,
                    "summary": i.summary,
                    "proposed_repair": i.proposed_repair,
                    "governing_artifact": (
                        {
                            "artifact_id": i.governing_directive.artifact_id,
                            "artifact_class": i.governing_directive.artifact_class.value,
                            "precedence": i.governing_directive.precedence,
                            "author": i.governing_directive.author,
                        }
                        if i.governing_directive
                        else None
                    ),
                    "status": i.status.value,
                }
                for i in report.incidents
            ],
        }
    )


@mcp.tool()
async def coherence_incidents(
    scope_kind: str = "",
    scope_id: str = "",
    limit: int = 50,
) -> str:
    """List persisted coherence incidents from the audit log.

    Incidents are recorded each time run_coherence_scan or a COHERENCE dream job runs.
    Omit scope to return all incidents across scopes.
    Returns JSON array of {kind, subject, summary, proposed_repair, status, created_at}.
    """
    kwargs: dict[str, Any] = {}
    if scope_kind and scope_id:
        kwargs["scope"] = _scope(scope_kind, scope_id)
    else:
        require_explicit_scope("coherence_incidents")
    incidents = await get_client().coherence_incidents(**kwargs)
    return json.dumps(
        [
            {
                "kind": i.kind.value,
                "subject": i.subject,
                "summary": i.summary,
                "proposed_repair": i.proposed_repair,
                "status": i.status.value,
            }
            for i in incidents[:limit]
        ]
    )


@mcp.tool()
async def remediate_coherence(
    scope_kind: str = "",
    scope_id: str = "",
    dry_run: bool = True,
    retire_held_directives: bool = False,
) -> str:
    """Coherence remediation sweep. Pass a scope to bound it; omit it to sweep the fleet.

    Detects escalation windup and optionally applies anti-windup holds and
    soft-retires the futilely-escalated directives.

    Give scope_kind + scope_id to remediate ONE scope: that needs only the ordinary
    permission to name it, so a tenant's own operator can run it.
    Omit both to sweep every scope in the store, which is ADMIN-only, and whose
    apply path is refused outright where the deployment cannot identify its callers.

    Always run with dry_run=True first to preview the plan before applying.
    retire_held_directives: soft-retire memories already on coherence_hold
                            (evidence preserved, never destroyed).

    Returns JSON report with scope_count, affected_scope_count, total_windup,
    total_contradiction, total_held, total_retired per scope.
    """
    kwargs: dict[str, Any] = {}
    if scope_kind and scope_id:
        # BOUNDED (B3). The ordinary scope guard is the right check here and it is a
        # NARROWER one than the fleet-wide pair below: `_scope` routes through
        # `authorize_scope`, so a caller may remediate a scope its principal is allowed to
        # name and no other. That is what makes this reachable by a tenant's own operator
        # instead of ADMIN-or-nobody, and it is the "scoped variant" both fleet-wide
        # guards' docstrings said was owed.
        #
        # Note this path does NOT consult the fleet-wide guards, deliberately. A scoped
        # write is the same shape as every other scoped write on this surface
        # (`add_memory`, `forget_memory`): one named scope, authorized by the principal's
        # allowlist. "Every scope" is the case that cannot be authorized that way, which is
        # why it keeps a separate, stricter rule.
        kwargs["scopes"] = [_scope(scope_kind, scope_id)]
    else:
        require_fleet_wide_read("remediate_coherence")
        if not dry_run:
            # The APPLY path is a fleet-wide WRITE and is held to a stricter rule than the
            # preview above: `require_fleet_wide_read` returns immediately when identity is
            # disarmed, which is how this tool became callable by anyone who could reach the
            # MCP port on every environment except `latest`. `dry_run=True` still previews.
            #
            # Since B3 there is a remedy the caller can act on rather than only a refusal:
            # naming a scope. The refusal text is left alone -- widening it to advertise the
            # scoped path would also advertise, to an unauthenticated caller, that a write
            # they cannot make fleet-wide is available one argument away.
            require_fleet_wide_write("remediate_coherence")

    report = await get_client().remediate_coherence(
        dry_run=dry_run,
        retire_held_directives=retire_held_directives,
        **kwargs,
    )
    return json.dumps(
        {
            "dry_run": report.dry_run,
            "scope_count": report.scope_count,
            "affected_scope_count": report.affected_scope_count,
            "total_windup": report.total_windup,
            "total_contradiction": report.total_contradiction,
            "total_held": report.total_held,
            "total_retired": report.total_retired,
            "scopes": [
                {
                    "scope": sr.scope.key,
                    "incident_count": sr.report.incident_count,
                    "held": len(sr.held_relationship_uuids),
                    "retired": len(sr.retired_relationship_uuids),
                }
                for sr in report.scopes
                if sr.report.incident_count > 0
            ],
        }
    )


# ---------------------------------------------------------------------------
# Human adjudication tools (WS-16 T14) — operator surface
# ---------------------------------------------------------------------------


@mcp.tool()
async def pending_supersession_reviews(
    scope_kind: str,
    scope_id: str,
    limit: int = 50,
) -> str:
    """List gate-parked supersession challengers awaiting human adjudication.

    Each item is a challenger the truth gate parked instead of letting it
    supersede current truth (lower authority, temporary high-severity change,
    or insufficient corroboration). gate_reason says why it parked;
    current_incumbent_uuid is the ACTIVE row it disputes.
    Resolve items with resolve_supersession_review.
    Returns a JSON array in deterministic (created_at, uuid) order.
    """
    items = await get_client().pending_supersession_reviews(
        scope=_scope(scope_kind, scope_id),
        limit=limit,
    )
    return json.dumps([item.model_dump(mode="json") for item in items])


@mcp.tool()
async def resolve_supersession_review(
    relationship_uuid: str,
    scope_kind: str,
    scope_id: str,
    decision: str,
    reason: str,
    resolved_by: str,
) -> str:
    """Adjudicate one parked supersession challenger (operator action).

    decision: 'approve' makes the parked candidate current truth through the
    normal supersession machinery (incumbent superseded with lineage, candidate
    reactivated); 'reject' keeps current truth and stamps the challenger
    operator_rejected. Every row mutation is receipted in an operator run.
    Returns JSON with the final status and any superseded incumbent uuids.
    """
    if decision not in {"approve", "reject"}:
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    result = await get_client().resolve_supersession_review(
        relationship_uuid=relationship_uuid,
        scope=_scope(scope_kind, scope_id),
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        resolved_by=resolved_by,
    )
    return result.model_dump_json()


@mcp.tool()
async def resolve_coherence_incident(
    incident_id: str,
    scope_kind: str,
    scope_id: str,
    status: str,
    statement: str,
    resolved_by: str,
    allow_held: bool = False,
) -> str:
    """Transition a coherence incident's lifecycle status (operator action).

    status: 'acknowledged' | 'resolved' — forward-only from OPEN.
    Resolving does NOT release any anti-windup coherence_hold (hold release is
    outcome-driven per WS-10); pass allow_held=True to resolve the incident
    record while its hold persists.
    Returns JSON with previous_status and the new status.
    """
    if status not in {"acknowledged", "resolved"}:
        raise ValueError(f"status must be 'acknowledged' or 'resolved', got {status!r}")
    result = await get_client().resolve_coherence_incident(
        incident_id=incident_id,
        scope=_scope(scope_kind, scope_id),
        status=status,  # type: ignore[arg-type]
        statement=statement,
        resolved_by=resolved_by,
        allow_held=allow_held,
    )
    return result.model_dump_json()


@mcp.tool()
async def resolve_disambiguation_request(
    request_id: str,
    scope_kind: str,
    scope_id: str,
    runtime_trace: str,
    artifact_version: str,
    statement: str,
    resolved_by: str,
) -> str:
    """Supply the evidence a low-confidence coherence incident requested.

    request_id is the disambiguation request's incident_id (list them with the
    SDK's coherence_disambiguation_requests). Records the runtime trace, the
    competing artifact version metadata, and the operator statement in a
    receipted operator run; the request then lists as status 'resolved' with
    the evidence attached.
    """
    result = await get_client().resolve_disambiguation_request(
        request_id=request_id,
        scope=_scope(scope_kind, scope_id),
        runtime_trace=runtime_trace,
        artifact_version=artifact_version,
        statement=statement,
        resolved_by=resolved_by,
    )
    return result.model_dump_json()


@mcp.tool()
async def pending_entity_alias_proposals(
    scope_kind: str,
    scope_id: str,
    limit: int = 50,
) -> str:
    """List mid-band entity alias proposals awaiting human adjudication (WS-17 T16b).

    Each item is a surface name whose composed link score landed inside
    [review_threshold, auto_link_threshold) — or an auto-linked alias demoted
    by a truth conflict.  canonical_name is the entity it would bridge to;
    link_signals carries the composed-score breakdown; sample_fact_uuids are
    ACTIVE facts mentioning the surface. Resolve items with
    resolve_entity_alias_proposal.
    Returns a JSON array in deterministic (proposed_at, name) order.
    """
    items = await get_client().pending_entity_alias_proposals(
        scope=_scope(scope_kind, scope_id),
        limit=limit,
    )
    return json.dumps([item.model_dump(mode="json") for item in items])


@mcp.tool()
async def resolve_entity_alias_proposal(
    name: str,
    scope_kind: str,
    scope_id: str,
    decision: str,
    reason: str,
    resolved_by: str,
) -> str:
    """Adjudicate one proposed entity alias (operator action, WS-17 T16b).

    decision: 'approve' activates the alias (receipted ENTITY_ALIAS_RESOLVED)
    and runs the receipted backfill so existing ACTIVE rows recorded under the
    surface collapse onto the canonical truth slots; 'reject' stamps the row
    rejected — the same pair is never re-proposed unless a later composed
    score beats the rejection record by at least 0.05.
    Returns JSON with the resulting status and backfill counts.
    """
    if decision not in {"approve", "reject"}:
        raise ValueError(f"decision must be 'approve' or 'reject', got {decision!r}")
    result = await get_client().resolve_entity_alias_proposal(
        scope=_scope(scope_kind, scope_id),
        name=name,
        decision=decision,  # type: ignore[arg-type]
        reason=reason,
        resolved_by=resolved_by,
    )
    return result.model_dump_json()
