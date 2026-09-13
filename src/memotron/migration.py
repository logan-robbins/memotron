"""WS-13: Tenant memory migration — rename or import a project's memory.

``tenant_id`` is not an opaque label: it is baked as a literal substring into
``graph_key``/``truth_key``/``truth_prefix`` on nodes and relationships
(dreaming.py's ``node_identity_key``/``truth_identity``), into ``scope_key``
columns on episodes/use-events/outcome-events/live-artifacts, and into
``governance_keys`` (the per-scope content DEK, ``graph.py:get_or_create_governance_key``).
Renaming a project's tenant id, or importing one local project's memory into
another, is therefore a real data migration, not a config edit.

Design note (supersedes an earlier plan draft): this module does NOT call
``dreaming.DreamEngine._materialize_episode`` (nor its public
``materialize_episode`` wrapper). That function's only call site
(``_run_formation``) surrounds it with ceremony that assumes its input is
freshly LLM-extracted, not-yet-trusted candidate output: PII redaction,
per-candidate salience scoring, a ``CANDIDATE_EXTRACTED`` receipt emitted for
every candidate before any gate, Motive ``allowed_memory_types`` filtering, and
untrusted-directive/generated-output-authority gates. A migrated fact is the
opposite — it is an already-existing, already-vetted, already-ACTIVE
relationship from the source tenant's graph that already passed all of this
once. Re-running it through candidate-stage gates a second time would be
dishonest (a fake ``CANDIDATE_EXTRACTED`` receipt claiming fresh extraction)
and functionally risky (a destination-tenant salience/Motive filter could
silently drop a fact the user never asked to have filtered — silent data loss
dressed up as a migration).

Instead, migration is its own first-class write path that:

* reads the source via the same public, scope-filtered primitives
  ``erasure.py``'s ``sweep_scope`` already proved correct for "walk everything
  for one scope" (``active_relationships``, ``episodes_for_scope``,
  ``nodes_for_scope``, ``scopes``);
* recomputes scope-dependent identity for the destination scope using the
  SAME key-construction helpers formation uses
  (``dreaming.node_identity_key`` / ``dreaming.truth_identity`` — extracted
  from ``_materialize_episode`` specifically so this module does not
  duplicate that logic);
* writes through the same low-level graph primitives formation itself writes
  through (``upsert_node`` / ``add_relationship``), without formation's
  dedup/supersession/candidate-gating machinery — a migrated fact is copied
  structurally as-is, not re-decided against the destination's prior state;
* mints its own honest receipt lineage (``run_kind="migration"``,
  ``TENANT_MIGRATION_RELATIONSHIP_MATERIALIZED``) under the destination
  tenant, and never touches the source tenant's existing receipts/checkpoints.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from memotron.agent_memory import ProjectMemoryConfig, ProjectMemorySettings, project_scope
from memotron.crypto import ContentKeyUnavailableError
from memotron.dreaming import _ContentProtection, node_identity_key, truth_identity
from memotron.graph import PropertyGraphStore
from memotron.models import MemoryScope, RelationshipCardinality, ScopeKind
from memotron.receipts import ReceiptDecisionType, ReceiptRun, config_effective_policy_digest

_MIGRATION_JOB_NAME = "tenant-memory-migration"
_NODE_RESERVED_PROPERTY_KEYS = frozenset(
    {
        "name",
        "scope_kind",
        "scope_id",
        "scope_key",
        "graph_key",
        # WS-17 T16b: node name embeddings are content-DERIVED plaintext — they
        # must never be copied into a destination whose names are sealed, and a
        # plaintext destination re-stamps them on its next formation touch.
        "name_embedding",
        "embedding_identifier",
    }
)
_PROJECT_MEMORY_SETTINGS_FIELDS = frozenset(ProjectMemorySettings.model_fields)

# ``MENTIONS`` (episode -> entity) edges are structural navigation, not memory
# content: they are excluded from graph_state_hash for the same reason
# (dreaming.py) and from every erasure-sweep reader (erasure.py). Migration
# does not recreate "Episode" graph nodes for its copied episodes (only raw
# `episodes` table rows, for provenance), so a MENTIONS edge has no valid
# destination node to point at even if it were migrated.
_MENTIONS_RELATIONSHIP_TYPE = "MENTIONS"


def _memory_relationships(store: PropertyGraphStore, *, scope: MemoryScope) -> list[Any]:
    return [
        relationship
        for relationship in store.active_relationships(scope=scope)
        if relationship.type != _MENTIONS_RELATIONSHIP_TYPE
    ]


class TenantMigrationPreview(BaseModel):
    """Read-only preview of what a tenant migration would move.

    ``blocked_reason`` is set (and non-migratable) when the source scope has
    ever been written under CRYPTO_SHRED governance: node/truth keys there are
    keyed commitments over the OLD scope key, and v1 does not recompute
    crypto-shred commitments under a new tenant id (user-approved exclusion).

    ``active_relationship_count`` counts TENANT-scope facts;
    ``agent_scoped_relationship_count`` counts facts held in the agent scopes
    of the tenant's registered agents. Both move. They are reported separately
    because in ``multi-agent`` mode the agent-scope figure is routinely the
    overwhelming majority, and showing the tenant count alone would materially
    misstate the size of the migration.
    """

    model_config = {"frozen": True}

    source_tenant_id: str
    dest_tenant_id: str
    episode_count: int
    active_relationship_count: int
    agent_scoped_relationship_count: int = 0
    agent_scoped_episode_count: int = 0
    node_count: int
    agent_registration_count: int
    has_llm_credentials: bool
    has_prompt_override: bool
    blocked_reason: str | None = None


class TenantMigrationResult(BaseModel):
    """Outcome of a completed (or copy-only) tenant migration."""

    model_config = {"frozen": True}

    source_tenant_id: str
    dest_tenant_id: str
    episodes_migrated: int
    relationships_migrated: int
    agent_scoped_relationships_migrated: int = 0
    agent_scoped_episodes_migrated: int = 0
    nodes_migrated: int
    agents_migrated: int
    agent_motive_assignments_migrated: int
    llm_credentials_migrated: bool
    prompt_override_migrated: bool
    prompt_versions_migrated: int
    project_memory_config_versions_migrated: int
    purged_source: bool
    run_uuid: str | None


def _require_tenant_id(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{label} cannot be blank")
    return normalized


def _source_governance_blocked_reason(store: PropertyGraphStore, *, scope: MemoryScope) -> str | None:
    """WS-13: data-driven CRYPTO_SHRED detection.

    A ``governance_keys`` row for *scope* (subject_key="", the same subject
    key ``DreamEngine._content_protection`` uses for memory content — tenant
    LLM credentials use a distinct ``subject_key="llm_credentials"`` and must
    never be confused with this check) exists if and only if content for this
    scope was ever written under CRYPTO_SHRED governance, regardless of
    whether the key has since been shredded. This is read directly off the
    store rather than resolved from a ``GovernancePolicy``/control-plane
    object, so the check is correct for both a bare CLI invocation (no
    tenant-policy config in hand) and a control-plane-backed caller alike.
    """
    if store.governance_key_state(scope.key) is not None:
        return (
            f"source scope {scope.key!r} was written under CRYPTO_SHRED governance "
            "(a content-protection key exists for it); its node/truth keys are keyed "
            "commitments over the source scope key and v1 tenant migration does not "
            "recompute crypto-shred commitments under a new tenant id — this "
            "migration is refused"
        )
    return None


def _tenant_agent_scopes(store: PropertyGraphStore, *, tenant_id: str) -> tuple[MemoryScope, ...]:
    """Agent scopes owned by *tenant_id*, in a stable order."""
    return tuple(
        MemoryScope(kind=ScopeKind.AGENT, scope_id=str(agent["agent_id"])) for agent in store.tenant_agents(tenant_id)
    )


def _first_governance_blocked_reason(store: PropertyGraphStore, *, scopes: Iterable[MemoryScope]) -> str | None:
    """Refuse if ANY scope in the migration set is under CRYPTO_SHRED governance.

    Agent scopes are checked alongside the tenant scope: they move too, so a
    crypto-shredded agent scope must block the migration for exactly the same
    reason the tenant scope does.
    """
    for scope in scopes:
        reason = _source_governance_blocked_reason(store, scope=scope)
        if reason is not None:
            return reason
    return None


def preview_tenant_migration(
    store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
    dest_store: PropertyGraphStore | None = None,
) -> TenantMigrationPreview:
    """Read-only preview of a would-be migration. Never writes."""

    source_tenant_id = _require_tenant_id(source_tenant_id, "source_tenant_id")
    dest_tenant_id = _require_tenant_id(dest_tenant_id, "dest_tenant_id")
    del dest_store  # same-store and cross-store previews both read only from `store`.
    source_scope = project_scope(source_tenant_id)
    agent_scopes = _tenant_agent_scopes(store, tenant_id=source_tenant_id)
    return TenantMigrationPreview(
        source_tenant_id=source_tenant_id,
        dest_tenant_id=dest_tenant_id,
        episode_count=len(store.episodes_for_scope(source_scope.key)),
        active_relationship_count=len(_memory_relationships(store, scope=source_scope)),
        agent_scoped_relationship_count=sum(len(_memory_relationships(store, scope=scope)) for scope in agent_scopes),
        agent_scoped_episode_count=sum(len(store.episodes_for_scope(scope.key)) for scope in agent_scopes),
        node_count=len(store.nodes_for_scope(source_scope.key)),
        agent_registration_count=len(store.tenant_agents(source_tenant_id)),
        has_llm_credentials=store.tenant_llm_credential_state(source_tenant_id) is not None,
        has_prompt_override=store.tenant_prompt_override(source_tenant_id) is not None,
        blocked_reason=_first_governance_blocked_reason(store, scopes=(source_scope, *agent_scopes)),
    )


def _resolve_dest_protection(store: PropertyGraphStore, *, scope_key: str) -> _ContentProtection | None:
    """Resolve destination content protection from live store state, not config.

    Mirrors the source-side detection in :func:`_source_governance_blocked_reason`:
    if the destination scope has never been protected, migrated content is
    written as plaintext (today's legacy/no-governance behaviour); if it has,
    the SAME (already-provisioned) destination DEK is reused to seal the
    migrated content — never the source's key. A destination scope whose key
    was already crypto-shredded fails fast via ``ContentKeyUnavailableError``.
    """
    if store.governance_key_state(scope_key) is None:
        return None
    try:
        key = store.get_or_create_governance_key(scope_key)
    except ContentKeyUnavailableError as exc:
        raise ValueError(
            f"destination scope {scope_key!r} has been crypto-shredded and can no longer accept new content"
        ) from exc
    return _ContentProtection(key=key)


def _entity_label(node: Any, scope_label: str) -> str:
    for label in node.labels:
        if label != scope_label:
            return label
    return "Entity"


def _extra_node_properties(node: Any) -> dict[str, Any]:
    return {key: value for key, value in node.properties.items() if key not in _NODE_RESERVED_PROPERTY_KEYS}


def _migrate_episodes(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    source_scope: MemoryScope,
    dest_scope: MemoryScope,
    now: datetime,
) -> dict[str, str]:
    """Copy raw episode rows for provenance under the destination scope.

    Raw episodes are immutable evidence; nothing about their body/name/
    reference_time is re-derived. Each gets a FRESH uuid (same-file and
    cross-file migration must behave identically, and same-file cannot reuse
    a uuid already present in the ``episodes`` table), so relationships that
    reference these episodes are rewritten via the returned uuid map.
    """
    episode_uuid_map: dict[str, str] = {}
    for episode in source_store.episodes_for_scope(source_scope.key):
        migrated = episode.model_copy(
            update={
                "uuid": uuid4().hex,
                "scope": dest_scope,
                "metadata": {
                    **episode.metadata,
                    "migration_source_tenant_id": source_tenant_id,
                    "migration_source_episode_uuid": episode.uuid,
                    "migrated_at": now.isoformat(),
                },
            }
        )
        dest_store.add_episode(migrated)
        episode_uuid_map[episode.uuid] = migrated.uuid
    return episode_uuid_map


def _remap_episode_reference(value: Any, episode_uuid_map: dict[str, str]) -> Any:
    if isinstance(value, str):
        return episode_uuid_map.get(value, value)
    return value


def _migrate_relationships(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    source_scope: MemoryScope,
    dest_scope: MemoryScope,
    dest_protection: _ContentProtection | None,
    episode_uuid_map: dict[str, str],
    run: ReceiptRun,
    now: datetime,
) -> tuple[int, int]:
    source_scope_label = source_scope.kind.value.title()
    dest_scope_label = dest_scope.kind.value.title()
    relationships_migrated = 0
    dest_node_keys_created: set[str] = set()
    # Per-row receipts must carry a contiguous before -> after state-hash chain
    # (replay.py's fail-closed artifact census + no-silent-mutation check), but
    # graph_state_hash is uncached and O(scope): calling it twice per migrated
    # row is O(N^2) queries on precisely the large graphs this module exists to
    # move. The tracker scans the destination scope ONCE and folds in each row
    # this loop writes, producing byte-identical digests for a linear cost.
    # Correct because migration is the sole writer of dest_scope here and
    # ``add_relationship`` is a pure insert — it never rewrites sibling rows.
    state = dest_store.scope_state_hash_tracker(dest_scope.key)

    for relationship in _memory_relationships(source_store, scope=source_scope):
        subject_node = source_store.get_node(relationship.source_uuid)
        object_node = source_store.get_node(relationship.target_uuid)
        subject_label = _entity_label(subject_node, source_scope_label)
        object_label = _entity_label(object_node, source_scope_label)
        subject_name = str(subject_node.properties.get("name", ""))
        predicate = str(relationship.properties.get("predicate", ""))
        # WS-17 T16: a canonicalized row's truth keys were built on its CANONICAL
        # predicate — recompute the destination keys from the same form so the
        # move lands on the identical slot a fresh formation write would use.
        truth_predicate = str(relationship.properties.get("predicate_canonical") or predicate)
        object_name = str(relationship.properties.get("object", object_node.properties.get("name", "")))
        cardinality = RelationshipCardinality(
            relationship.properties.get("truth_cardinality", RelationshipCardinality.MULTI_ACTIVE.value)
        )

        dest_subject_key = node_identity_key(
            scope_key=dest_scope.key, label=subject_label, name=subject_name, protection=dest_protection
        )
        dest_object_key = node_identity_key(
            scope_key=dest_scope.key, label=object_label, name=object_name, protection=dest_protection
        )
        dest_truth_key, dest_truth_prefix, dest_object_commitment = truth_identity(
            scope_key=dest_scope.key,
            subject=subject_name,
            predicate=truth_predicate,
            object_value=object_name,
            cardinality=cardinality,
            protection=dest_protection,
        )

        dest_subject_node, subject_created = dest_store.upsert_node(
            labels=(subject_label, dest_scope_label),
            key=dest_subject_key,
            properties={
                "name": dest_protection.seal(subject_name) if dest_protection is not None else subject_name,
                "scope_kind": dest_scope.kind.value,
                "scope_id": dest_scope.scope_id,
                "scope_key": dest_scope.key,
                **_extra_node_properties(subject_node),
            },
            valid_from=subject_node.valid_from,
            valid_to=subject_node.valid_to,
        )
        if subject_created:
            dest_node_keys_created.add(dest_subject_key)
        dest_object_node, object_created = dest_store.upsert_node(
            labels=(object_label, dest_scope_label),
            key=dest_object_key,
            properties={
                "name": dest_protection.seal(object_name) if dest_protection is not None else object_name,
                "scope_kind": dest_scope.kind.value,
                "scope_id": dest_scope.scope_id,
                "scope_key": dest_scope.key,
                **_extra_node_properties(object_node),
            },
            valid_from=object_node.valid_from,
            valid_to=object_node.valid_to,
        )
        if object_created:
            dest_node_keys_created.add(dest_object_key)

        new_properties = dict(relationship.properties)
        new_properties.update(
            {
                "scope_kind": dest_scope.kind.value,
                "scope_id": dest_scope.scope_id,
                "scope_key": dest_scope.key,
                "truth_key": dest_truth_key,
                "truth_prefix": dest_truth_prefix,
            }
        )
        fact_text = str(relationship.properties.get("fact", f"{subject_name} {predicate} {object_name}"))
        source_text = relationship.properties.get("source_text")
        embedding = relationship.properties.get("embedding")
        object_embedding = relationship.properties.get("object_embedding")
        if dest_protection is not None:
            new_properties["fact"] = dest_protection.seal(fact_text)
            new_properties["object"] = dest_protection.seal(object_name)
            new_properties["source_text"] = (
                dest_protection.seal_optional(source_text) if isinstance(source_text, str) else None
            )
            new_properties["embedding"] = (
                dest_protection.seal_vector(list(embedding)) if isinstance(embedding, list) and embedding else embedding
            )
            new_properties["object_embedding"] = (
                dest_protection.seal_vector(list(object_embedding))
                if isinstance(object_embedding, list) and object_embedding
                else object_embedding
            )
            new_properties["fact_commitment"] = dest_protection.commit("fact", fact_text)
            new_properties["object_commitment"] = dest_object_commitment
        else:
            new_properties["object"] = object_name
            new_properties["object_commitment"] = None
            new_properties.pop("fact_commitment", None)

        old_episode_uuid = relationship.properties.get("episode_uuid")
        if isinstance(old_episode_uuid, str):
            new_properties["episode_uuid"] = _remap_episode_reference(old_episode_uuid, episode_uuid_map)
        old_episode_uuids = relationship.properties.get("episode_uuids")
        if isinstance(old_episode_uuids, list):
            new_properties["episode_uuids"] = [
                _remap_episode_reference(value, episode_uuid_map) for value in old_episode_uuids
            ]

        state_before = state.digest
        new_relationship = dest_store.add_relationship(
            source_uuid=dest_subject_node.uuid,
            target_uuid=dest_object_node.uuid,
            relationship_type=relationship.type,
            properties=new_properties,
            valid_from=relationship.valid_from,
            valid_to=relationship.valid_to,
        )
        state_after = state.record(new_relationship)

        sensitive_payload = dest_protection.seal(fact_text) if dest_protection is not None else None
        dest_store.receipts.emit(
            run,
            decision_type=ReceiptDecisionType.TENANT_MIGRATION_RELATIONSHIP_MATERIALIZED,
            decision_reason=f"migrated_from_tenant:{source_tenant_id}",
            decision_result="materialized",
            created_at=now,
            relationship_type=relationship.type,
            memory_type=relationship.properties.get("memory_type"),
            claim_mode=relationship.properties.get("claim_mode"),
            directive_stance=relationship.properties.get("directive_stance"),
            truth_key=dest_truth_key,
            relationship_uuid=new_relationship.uuid,
            graph_state_hash_before=state_before,
            graph_state_hash_after=state_after,
            sensitive_payload=sensitive_payload,
            sensitive_payload_encrypted=dest_protection is not None,
        )
        relationships_migrated += 1

    return relationships_migrated, len(dest_node_keys_created)


def _migrate_agents(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> int:
    migrated = 0
    for agent in source_store.tenant_agents(source_tenant_id):
        dest_store.register_tenant_agent(
            tenant_id=dest_tenant_id,
            agent_id=agent["agent_id"],
            name=agent["agent_name"],
            source=f"migrated:{agent['source']}",
        )
        migrated += 1
    return migrated


def _migrate_agent_motive_assignments(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> int:
    migrated = 0
    for assignment in source_store.agent_motive_assignments(source_tenant_id):
        dest_store.set_agent_motive_assignment(
            tenant_id=dest_tenant_id,
            agent_id=assignment["agent_id"],
            motive_name=assignment["motive_name"],
            source=f"migrated:{assignment['source']}",
        )
        migrated += 1
    return migrated


def _migrate_llm_credentials(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> bool:
    credentials = source_store.tenant_llm_credentials(source_tenant_id)
    if credentials is None:
        return False
    dest_store.set_tenant_llm_credentials(
        tenant_id=dest_tenant_id,
        provider=credentials["provider"],
        api_key=credentials["api_key"],
        base_url=credentials["base_url"],
        model=credentials["model"],
        # The embedding endpoint IS the tenant's vector space; a migration that
        # dropped it would silently re-space every vector it just moved.
        embedding_provider=credentials["embedding_provider"],
        embedding_base_url=credentials["embedding_base_url"],
        embedding_model=credentials["embedding_model"],
    )
    return True


def _migrate_prompt_override(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> bool:
    override = source_store.tenant_prompt_override(source_tenant_id)
    if override is None:
        return False
    dest_store.set_tenant_prompt_override(
        tenant_id=dest_tenant_id,
        prompt_profile=override["prompt_profile"],
        prompt_profile_version=override["prompt_profile_version"],
        override=override["override"],
    )
    return True


def _migrate_prompt_versions(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> int:
    migrated = 0
    # Oldest first: save_tenant_prompt_version() always activates the version it
    # just wrote, so replaying oldest -> newest reproduces the source's active tip.
    for version in reversed(source_store.tenant_prompt_versions(source_tenant_id)):
        dest_store.save_tenant_prompt_version(
            tenant_id=dest_tenant_id,
            prompt_text=version["prompt_text"],
            motive_name=version["motive_name"],
            source_profile=version["source_profile"],
            source_profile_version=version["source_profile_version"],
        )
        migrated += 1
    return migrated


def _migrate_project_memory_config_versions(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
) -> int:
    migrated = 0
    # Oldest first, same reasoning as _migrate_prompt_versions.
    for version in reversed(source_store.project_memory_config_versions(source_tenant_id)):
        config = ProjectMemoryConfig.model_validate(version).model_dump(
            mode="json", include=set(_PROJECT_MEMORY_SETTINGS_FIELDS)
        )
        dest_store.save_project_memory_config(
            tenant_id=dest_tenant_id,
            config=config,
            configured_by=f"migrated:{version['configured_by']}",
        )
        migrated += 1
    return migrated


def _scope_watermark(store: PropertyGraphStore, *, scope: MemoryScope) -> tuple[Any, ...]:
    """Everything about *scope* that a concurrent write could change and a purge destroy.

    ``graph_state_hash`` is the canonical content digest over the scope's
    relationship rows, so it catches an inserted, edited, superseded, demoted,
    pinned, or re-scoped fact — not merely a changed row COUNT, which a
    delete-plus-insert would leave untouched. The counts alongside it cover the
    row classes that live outside that digest but inside
    ``purge_tenant_state``'s blast radius for this scope: entity nodes, raw
    episodes, and the use / outcome / prune-ghost evidence plane.
    """
    return (
        store.graph_state_hash(scope.key),
        len(_memory_relationships(store, scope=scope)),
        len(store.nodes_for_scope(scope.key)),
        len(store.episodes_for_scope(scope.key)),
        len(store.use_events(scope_key=scope.key)),
        len(store.outcome_events(scope_key=scope.key)),
        len(store.prune_ghosts(scope_key=scope.key)),
    )


def _source_watermark(store: PropertyGraphStore, *, scopes: Iterable[MemoryScope]) -> dict[str, tuple[Any, ...]]:
    """Watermark every scope this migration reads from, keyed by scope key."""
    return {scope.key: _scope_watermark(store, scope=scope) for scope in scopes}


def _verify_source_unchanged(
    store: PropertyGraphStore,
    *,
    scopes: Iterable[MemoryScope],
    watermark: dict[str, tuple[Any, ...]],
) -> None:
    """Re-derive the source watermark and refuse to purge if anything moved.

    Called INSIDE :meth:`PropertyGraphStore.exclusive_write_transaction`, so it
    reads a source no other connection can be writing to, and the purge that
    follows runs under the same lock. A write that landed after the copy read
    the source is therefore detected here and aborts the migration with the
    source fully intact — never purged out from under its author.
    """
    current = _source_watermark(store, scopes=scopes)
    if current == watermark:
        return
    changed = sorted(
        scope_key for scope_key in {*current, *watermark} if current.get(scope_key) != watermark.get(scope_key)
    )
    raise RuntimeError(
        "tenant migration aborted: the source changed after it was copied "
        f"(scopes {changed!r} no longer match the state that was migrated). "
        "A concurrent write landed between the copy and the purge; purging now "
        "would destroy it. The source is untouched and the destination holds a "
        "complete copy of the pre-change state — re-run the migration to pick "
        "up the newer writes, or use purge_source=False and reconcile manually."
    )


def _verify_migration_counts(
    preview: TenantMigrationPreview,
    *,
    relationships_migrated: int,
    episodes_migrated: int,
    agent_relationships_migrated: int,
    agent_episodes_migrated: int,
) -> None:
    """Fail fast BEFORE the destructive purge step if anything is short.

    Never over-verifies node counts: the preview's node_count includes
    entities referenced only by superseded/pruned relationships (history we
    deliberately do not migrate — only ACTIVE state moves), so it is not a
    1:1 lower bound on migrated nodes and is reported for sizing only.
    """
    expectations = (
        ("active relationships", relationships_migrated, preview.active_relationship_count),
        ("episodes", episodes_migrated, preview.episode_count),
        (
            "agent-scoped relationships",
            agent_relationships_migrated,
            preview.agent_scoped_relationship_count,
        ),
        ("agent-scoped episodes", agent_episodes_migrated, preview.agent_scoped_episode_count),
    )
    for label, migrated, expected in expectations:
        if migrated != expected:
            raise RuntimeError(
                f"tenant migration verification failed: migrated {migrated} {label} but the "
                f"preview counted {expected}; refusing to purge the source"
            )


def _migrate_agent_scopes(
    source_store: PropertyGraphStore,
    dest_store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    agent_scopes: Iterable[MemoryScope],
    run: ReceiptRun,
    now: datetime,
) -> tuple[int, int, int]:
    """Copy agent-scoped memory into a DIFFERENT graph file.

    The destination scope key is IDENTICAL to the source's, because an agent
    scope key does not embed a tenant id — this is a same-key copy between two
    files, never a re-keying. Content protection is still resolved per scope
    against the DESTINATION store, so migrated content is sealed under the
    destination file's key and never the source's.

    Not used for same-file migrations: there the rows already sit at their
    final scope key and only ownership changes.
    """
    relationships_migrated = 0
    episodes_migrated = 0
    nodes_migrated = 0
    for scope in agent_scopes:
        protection = _resolve_dest_protection(dest_store, scope_key=scope.key)
        episode_uuid_map = _migrate_episodes(
            source_store,
            dest_store,
            source_tenant_id=source_tenant_id,
            source_scope=scope,
            dest_scope=scope,
            now=now,
        )
        scope_relationships, scope_nodes = _migrate_relationships(
            source_store,
            dest_store,
            source_tenant_id=source_tenant_id,
            source_scope=scope,
            dest_scope=scope,
            dest_protection=protection,
            episode_uuid_map=episode_uuid_map,
            run=run,
            now=now,
        )
        episodes_migrated += len(episode_uuid_map)
        relationships_migrated += scope_relationships
        nodes_migrated += scope_nodes
    return relationships_migrated, episodes_migrated, nodes_migrated


def migrate_tenant_memory(
    store: PropertyGraphStore,
    *,
    source_tenant_id: str,
    dest_tenant_id: str,
    dest_store: PropertyGraphStore | None = None,
    purge_source: bool = True,
    now: datetime | None = None,
) -> TenantMigrationResult:
    """Move (or, with ``purge_source=False``, copy) one tenant's memory.

    Ordering is COPY -> VERIFY -> LOCK -> RE-VERIFY -> PURGE. Every write before
    the purge is additive to the destination; the only destructive step
    (``purge_tenant_state`` on the source) runs last, and only after two checks:

    * a count check against the preview (``_verify_migration_counts``), which
      catches an incomplete copy; and
    * a re-derivation of the source's content watermark — a per-scope
      ``graph_state_hash`` plus node / episode / use-event / outcome-event /
      prune-ghost counts (``_verify_source_unchanged``) — taken INSIDE
      ``exclusive_write_transaction``, the same ``BEGIN IMMEDIATE`` transaction
      that then runs the purge.

    The guarantee that buys is precise and holds with concurrent writers, which
    ``multi-agent`` mode does not merely permit but expects. A write to the
    source can only land in one of three places:

    * *before the copy read it* — it is copied, and the watermark still matches;
    * *between the copy and the lock* — the re-verify sees a watermark that no
      longer matches what was copied and raises, so the purge never runs and the
      write survives in the source (the destination is left holding a duplicate
      of the pre-change state, which is the documented failure mode);
    * *after the lock is taken* — SQLite blocks it for the whole verify->purge
      window, so it commits only after the purge and survives.

    In every case the outcome is duplicate data or a loud failure, never silent
    loss. A ``RuntimeError`` from either check leaves the source fully intact
    and returns no ``TenantMigrationResult`` — a failed migration never reports
    success. What the watermark does NOT cover is derived state the purge also
    clears but nothing can reconstruct memory from: processed-episode marks and
    ``dream_decisions`` debug records. Those are regenerated by the next dream
    cycle from the raw episodes, which the purge preserves.

    Source raw episodes and sealed credentials are preserved by
    ``purge_tenant_state`` itself (unchanged, reused as-is); this function never
    touches the source tenant's ``memory_receipts``/``run_checkpoints``.

    Agent-scoped memory moves too, but by a different mechanism, because an
    agent scope key (``agent:<agent_id>``) does not embed a tenant id:

    * **Same graph file** — the rows are ALREADY at their final scope key, so
      there is nothing to copy; ownership moves with the ``tenant_agents``
      rows. The agents are deregistered from the source *before*
      ``purge_tenant_state`` runs, because that purge's blast radius includes
      the agent scope of every agent still registered to the tenant — which
      would otherwise delete memory that now belongs to the destination.
    * **Across graph files** — each agent scope is copied to the destination
      file under the SAME scope key (a same-key copy, never a re-keying).
    """
    source_tenant_id = _require_tenant_id(source_tenant_id, "source_tenant_id")
    dest_tenant_id = _require_tenant_id(dest_tenant_id, "dest_tenant_id")
    target_store = dest_store if dest_store is not None else store
    same_store = target_store is store
    if same_store and source_tenant_id == dest_tenant_id:
        # Source and destination scope keys would be identical: the copy would
        # duplicate every fact into the scope it read them from and the purge
        # would then delete both copies. Refuse explicitly rather than let the
        # post-copy watermark check report it as a phantom concurrent write.
        # (Across two graph files the same tenant id IS meaningful — that is an
        # import of a project's memory into another file under its own name.)
        raise ValueError(
            "source_tenant_id and dest_tenant_id are the same within one graph file; there is nothing to migrate"
        )

    preview = preview_tenant_migration(
        store,
        source_tenant_id=source_tenant_id,
        dest_tenant_id=dest_tenant_id,
        dest_store=dest_store,
    )
    if preview.blocked_reason is not None:
        raise ValueError(preview.blocked_reason)

    moment = now or datetime.now(UTC)
    source_scope = project_scope(source_tenant_id)
    dest_scope = project_scope(dest_tenant_id)
    agent_scopes = _tenant_agent_scopes(store, tenant_id=source_tenant_id)
    dest_protection = _resolve_dest_protection(target_store, scope_key=dest_scope.key)
    # Watermark the source BEFORE the first read that feeds the copy, so the
    # pre-purge re-verify below compares against exactly the state that was
    # copied. See this function's docstring for the guarantee it underwrites.
    source_scopes = (source_scope, *agent_scopes)
    watermark = _source_watermark(store, scopes=source_scopes)

    episode_uuid_map = _migrate_episodes(
        store,
        target_store,
        source_tenant_id=source_tenant_id,
        source_scope=source_scope,
        dest_scope=dest_scope,
        now=moment,
    )

    run: ReceiptRun | None = None
    relationships_migrated = 0
    nodes_migrated = 0
    agent_relationships_migrated = 0
    agent_episodes_migrated = 0
    if preview.active_relationship_count + preview.agent_scoped_relationship_count > 0:
        run = target_store.receipts.begin_run(
            run_kind="migration",
            job_name=_MIGRATION_JOB_NAME,
            scope_key=dest_scope.key,
            effective_policy_digest=config_effective_policy_digest(
                {
                    "migration_source_tenant_id": source_tenant_id,
                    "migration_dest_tenant_id": dest_tenant_id,
                    "purge_source": purge_source,
                }
            ),
            tenant_id=dest_tenant_id,
            graph_state_hash_before=target_store.graph_state_hash(dest_scope.key),
        )
        relationships_migrated, nodes_migrated = _migrate_relationships(
            store,
            target_store,
            source_tenant_id=source_tenant_id,
            source_scope=source_scope,
            dest_scope=dest_scope,
            dest_protection=dest_protection,
            episode_uuid_map=episode_uuid_map,
            run=run,
            now=moment,
        )
        if same_store:
            # Nothing to COPY: an agent scope key is identical on both sides, so
            # these rows are already where they belong. They are still counted —
            # from a LIVE re-read of the store, never from the preview. Assigning
            # the preview's own numbers here and then checking them against the
            # preview would be a tautology that can never fail; re-counting means
            # the check below actually re-derives what is present and catches an
            # agent scope that changed since the preview was taken.
            agent_relationships_migrated = sum(len(_memory_relationships(store, scope=scope)) for scope in agent_scopes)
            agent_episodes_migrated = sum(len(store.episodes_for_scope(scope.key)) for scope in agent_scopes)
        else:
            (
                agent_relationships_migrated,
                agent_episodes_migrated,
                agent_nodes_migrated,
            ) = _migrate_agent_scopes(
                store,
                target_store,
                source_tenant_id=source_tenant_id,
                agent_scopes=agent_scopes,
                run=run,
                now=moment,
            )
            nodes_migrated += agent_nodes_migrated
        if run.next_event_index > 0:
            target_store.receipts.checkpoint(run, graph_state_hash_after=target_store.graph_state_hash(dest_scope.key))

    agents_migrated = _migrate_agents(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )
    motive_assignments_migrated = _migrate_agent_motive_assignments(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )
    llm_credentials_migrated = _migrate_llm_credentials(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )
    prompt_override_migrated = _migrate_prompt_override(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )
    prompt_versions_migrated = _migrate_prompt_versions(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )
    project_memory_config_versions_migrated = _migrate_project_memory_config_versions(
        store, target_store, source_tenant_id=source_tenant_id, dest_tenant_id=dest_tenant_id
    )

    _verify_migration_counts(
        preview,
        relationships_migrated=relationships_migrated,
        episodes_migrated=len(episode_uuid_map),
        agent_relationships_migrated=agent_relationships_migrated,
        agent_episodes_migrated=agent_episodes_migrated,
    )

    purged_source = False
    if purge_source:
        if same_store:
            # Move agent ownership off the source BEFORE purging: the purge
            # sweeps the agent scope of every agent still registered to the
            # tenant, and those scopes now belong to the destination. This runs
            # before the exclusive transaction below because it commits on its
            # own (which would drop the lock mid-critical-section) and because
            # it moves ownership only — it deletes no memory, so an abort after
            # it still loses nothing.
            for scope in agent_scopes:
                store.deregister_tenant_agent(tenant_id=source_tenant_id, agent_id=scope.scope_id)
        with store.exclusive_write_transaction():
            # Nothing else may write the source from here until the purge
            # commits. Re-derive the watermark under that lock: if a write
            # landed after the copy, this raises and the purge never runs.
            _verify_source_unchanged(store, scopes=source_scopes, watermark=watermark)
            # preserve_receipts keeps the source tenant's OWN receipt chain in
            # place rather than deleting and re-inserting it. Deleting first and
            # restoring after would commit twice, and a crash between those two
            # commits would destroy the source's audit history permanently;
            # never removing the rows leaves no such window.
            #
            # This is the last statement in the block on purpose: it commits
            # internally, which ends the critical section exactly at the point
            # the destructive write lands.
            store.purge_tenant_state(source_tenant_id, preserve_receipts=True)
        purged_source = True
        if same_store:
            surviving = sum(len(_memory_relationships(store, scope=scope)) for scope in agent_scopes)
            if surviving != preview.agent_scoped_relationship_count:
                raise RuntimeError(
                    "tenant migration post-condition failed: "
                    f"{preview.agent_scoped_relationship_count} agent-scoped facts were "
                    f"expected to survive the source purge but {surviving} remain"
                )

    return TenantMigrationResult(
        source_tenant_id=source_tenant_id,
        dest_tenant_id=dest_tenant_id,
        episodes_migrated=len(episode_uuid_map),
        relationships_migrated=relationships_migrated,
        agent_scoped_relationships_migrated=agent_relationships_migrated,
        agent_scoped_episodes_migrated=agent_episodes_migrated,
        nodes_migrated=nodes_migrated,
        agents_migrated=agents_migrated,
        agent_motive_assignments_migrated=motive_assignments_migrated,
        llm_credentials_migrated=llm_credentials_migrated,
        prompt_override_migrated=prompt_override_migrated,
        prompt_versions_migrated=prompt_versions_migrated,
        project_memory_config_versions_migrated=project_memory_config_versions_migrated,
        purged_source=purged_source,
        run_uuid=run.run_uuid if run is not None else None,
    )


__all__ = [
    "TenantMigrationPreview",
    "TenantMigrationResult",
    "migrate_tenant_memory",
    "preview_tenant_migration",
]
