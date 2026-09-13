"""Versioned schema for the Operational Store.

Replaces the SQLite backend's ad-hoc ``CREATE TABLE IF NOT EXISTS`` script plus
``ALTER TABLE`` probing with an ordered, recorded migration list.  Rules:

* Migrations are append-only.  Never edit an applied version; add a new one.
* Every migration is idempotent in its own right (``IF NOT EXISTS``) so a
  half-applied deploy is recoverable.
* ``properties`` is real ``jsonb``, not a JSON string column.  That is what lets
  the scope/status/truth filters be indexed and pushed into SQL instead of being
  re-implemented in Python.

Indexes that replace the SQLite generated columns
-------------------------------------------------
SQLite backed the context-visible read with virtual generated columns
(``gen_scope_key``, ``gen_status``, ``gen_context_visible``) and one composite
index.  Postgres gets the same access path from expression indexes on the jsonb
document.  ``relationships_ctx_idx`` is *partial*: the context-visibility term
lives in the index predicate, so the index holds only rows in the working-context
tier and the planner can seek it on ``(scope_key, status)``.

Note the visibility semantics carried over from SQLite: a memory is
context-visible when ``active_in_context`` is true *or absent*, and demoted only
when it is explicitly false.  Hence ``IS DISTINCT FROM 'false'::jsonb`` rather
than a plain equality test, which would drop rows where the key is missing.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- 1

_V1_CORE = """
CREATE TABLE IF NOT EXISTS nodes (
    uuid        text PRIMARY KEY,
    graph_key   text NOT NULL UNIQUE,
    labels      jsonb NOT NULL,
    properties  jsonb NOT NULL,
    created_at  text NOT NULL,
    valid_from  text,
    valid_to    text,
    version     bigint NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS relationships (
    uuid         text PRIMARY KEY,
    source_uuid  text NOT NULL REFERENCES nodes(uuid),
    target_uuid  text NOT NULL REFERENCES nodes(uuid),
    type         text NOT NULL,
    properties   jsonb NOT NULL,
    created_at   text NOT NULL,
    valid_from   text,
    valid_to     text,
    version      bigint NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS relationships_type_idx ON relationships(type);
CREATE INDEX IF NOT EXISTS relationships_source_idx ON relationships(source_uuid);
CREATE INDEX IF NOT EXISTS relationships_target_idx ON relationships(target_uuid);

-- The context-visible read (default working-context tier).  Partial on
-- visibility, so the tier is the only thing in the index.
CREATE INDEX IF NOT EXISTS relationships_ctx_idx
    ON relationships ((properties->>'scope_key'), (properties->>'status'), created_at, uuid)
    WHERE (properties->'active_in_context') IS DISTINCT FROM 'false'::jsonb;

-- Whole-scope reads: state hash, scope enumeration, erasure sweeps.
CREATE INDEX IF NOT EXISTS relationships_scope_idx
    ON relationships ((properties->>'scope_key'), uuid);

-- The duplicate check paid on every synchronous fact write.  Partial on active
-- status because that is the only status the probe asks for.
CREATE INDEX IF NOT EXISTS relationships_truth_key_idx
    ON relationships ((properties->>'truth_key'))
    WHERE properties->>'status' = 'active';

CREATE INDEX IF NOT EXISTS relationships_truth_prefix_idx
    ON relationships ((properties->>'truth_prefix'))
    WHERE properties->>'status' = 'active';

CREATE INDEX IF NOT EXISTS nodes_scope_idx ON nodes ((properties->>'scope_key'));

CREATE TABLE IF NOT EXISTS episodes (
    uuid        text PRIMARY KEY,
    payload     jsonb NOT NULL,
    created_at  text NOT NULL
);

CREATE INDEX IF NOT EXISTS episodes_scope_event_idx
    ON episodes (
        (payload->'scope'->>'kind'),
        (payload->'scope'->>'scope_id'),
        (payload->'metadata'->>'agent_memory_event')
    );

CREATE TABLE IF NOT EXISTS processed_episodes (
    episode_uuid  text PRIMARY KEY REFERENCES episodes(uuid),
    processed_at  text NOT NULL
);

CREATE TABLE IF NOT EXISTS episode_processing (
    episode_uuid  text NOT NULL REFERENCES episodes(uuid),
    consumer_key  text NOT NULL,
    processed_at  text NOT NULL,
    PRIMARY KEY (episode_uuid, consumer_key)
);

CREATE INDEX IF NOT EXISTS episode_processing_consumer_idx
    ON episode_processing(consumer_key, processed_at);

CREATE TABLE IF NOT EXISTS job_state (
    job_name  text PRIMARY KEY,
    last_run  text NOT NULL
);

CREATE TABLE IF NOT EXISTS dream_job_runs (
    uuid      text PRIMARY KEY,
    ran_at    text NOT NULL,
    job_name  text NOT NULL,
    job_kind  text NOT NULL,
    payload   jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS dream_job_runs_ran_at_idx ON dream_job_runs(ran_at DESC, uuid DESC);
CREATE INDEX IF NOT EXISTS dream_job_runs_job_name_idx ON dream_job_runs(job_name, ran_at DESC);

CREATE TABLE IF NOT EXISTS dream_decisions (
    uuid           text PRIMARY KEY,
    ran_at         text NOT NULL,
    job_name       text NOT NULL,
    job_kind       text NOT NULL,
    agent_id       text NOT NULL,
    decision_type  text NOT NULL,
    payload        jsonb NOT NULL
);

CREATE INDEX IF NOT EXISTS dream_decisions_ran_at_idx ON dream_decisions(ran_at DESC, uuid DESC);
CREATE INDEX IF NOT EXISTS dream_decisions_job_name_idx ON dream_decisions(job_name);
CREATE INDEX IF NOT EXISTS dream_decisions_agent_id_idx ON dream_decisions(agent_id);
CREATE INDEX IF NOT EXISTS dream_decisions_decision_type_idx ON dream_decisions(decision_type text_pattern_ops);
CREATE INDEX IF NOT EXISTS dream_decisions_scope_idx ON dream_decisions ((payload->>'scope_key'));

CREATE TABLE IF NOT EXISTS governance_keys (
    scope_key      text NOT NULL,
    subject_key    text NOT NULL DEFAULT '',
    wrapped_dek    text NOT NULL,
    kek_id         text NOT NULL,
    key_algorithm  text NOT NULL,
    created_at     text NOT NULL,
    shredded_at    text NOT NULL DEFAULT '',
    PRIMARY KEY (scope_key, subject_key)
);

CREATE TABLE IF NOT EXISTS formation_contract_signing_keys (
    signer_id            text PRIMARY KEY,
    wrapped_private_key  text NOT NULL,
    created_at           text NOT NULL
);

CREATE TABLE IF NOT EXISTS memory_use_events (
    use_id             text PRIMARY KEY,
    relationship_uuid  text NOT NULL REFERENCES relationships(uuid),
    scope_key          text NOT NULL,
    kind               text NOT NULL,
    task_run_id        text NOT NULL,
    idempotency_key    text NOT NULL,
    used_at            text NOT NULL,
    payload            jsonb NOT NULL,
    UNIQUE (scope_key, idempotency_key)
);

CREATE INDEX IF NOT EXISTS memory_use_events_relationship_idx
    ON memory_use_events(relationship_uuid, used_at);
CREATE INDEX IF NOT EXISTS memory_use_events_task_idx
    ON memory_use_events(scope_key, task_run_id, used_at);

CREATE TABLE IF NOT EXISTS memory_outcome_events (
    outcome_id       text PRIMARY KEY,
    use_id           text NOT NULL REFERENCES memory_use_events(use_id),
    scope_key        text NOT NULL,
    task_run_id      text NOT NULL,
    idempotency_key  text NOT NULL,
    judged_at        text NOT NULL,
    payload          jsonb NOT NULL,
    UNIQUE (scope_key, idempotency_key)
);

CREATE INDEX IF NOT EXISTS memory_outcome_events_use_idx
    ON memory_outcome_events(use_id, judged_at);
CREATE INDEX IF NOT EXISTS memory_outcome_events_scope_idx
    ON memory_outcome_events(scope_key, task_run_id);

CREATE TABLE IF NOT EXISTS memory_prune_ghosts (
    relationship_uuid   text PRIMARY KEY REFERENCES relationships(uuid),
    scope_key           text NOT NULL,
    prune_receipt_uuid  text NOT NULL,
    pruned_at           text NOT NULL,
    reason              text NOT NULL,
    restorable          boolean NOT NULL DEFAULT true,
    restored_at         text
);

CREATE INDEX IF NOT EXISTS memory_prune_ghosts_scope_idx
    ON memory_prune_ghosts(scope_key, restorable, restored_at);

CREATE TABLE IF NOT EXISTS live_artifacts (
    scope_key          text NOT NULL,
    artifact_id        text NOT NULL,
    artifact_class     text NOT NULL,
    payload            jsonb NOT NULL,
    registered_at      text NOT NULL,
    updated_at         text NOT NULL,
    active             boolean NOT NULL DEFAULT true,
    quarantined_at     text,
    positive_outcomes  integer NOT NULL DEFAULT 0,
    negative_outcomes  integer NOT NULL DEFAULT 0,
    last_outcome_at    text,
    PRIMARY KEY (scope_key, artifact_id)
);

CREATE INDEX IF NOT EXISTS live_artifacts_active_idx
    ON live_artifacts(scope_key, active, updated_at);

CREATE TABLE IF NOT EXISTS live_artifact_outcome_events (
    event_id         text PRIMARY KEY,
    scope_key        text NOT NULL,
    artifact_id      text NOT NULL,
    task_run_id      text NOT NULL,
    idempotency_key  text NOT NULL,
    verdict          text NOT NULL,
    occurred_at      text NOT NULL,
    payload          jsonb NOT NULL,
    UNIQUE (scope_key, idempotency_key),
    FOREIGN KEY (scope_key, artifact_id)
        REFERENCES live_artifacts(scope_key, artifact_id)
);

CREATE INDEX IF NOT EXISTS live_artifact_outcome_events_artifact_idx
    ON live_artifact_outcome_events(scope_key, artifact_id, occurred_at);

CREATE TABLE IF NOT EXISTS live_artifact_versions (
    scope_key       text NOT NULL,
    artifact_id     text NOT NULL,
    version_digest  text NOT NULL,
    payload         jsonb NOT NULL,
    captured_at     text NOT NULL,
    PRIMARY KEY (scope_key, artifact_id, version_digest)
);

CREATE INDEX IF NOT EXISTS live_artifact_versions_latest_idx
    ON live_artifact_versions(scope_key, artifact_id, captured_at DESC, version_digest DESC);

CREATE TABLE IF NOT EXISTS coherence_repair_monitors (
    scope_key                         text NOT NULL,
    incident_id                       text NOT NULL,
    relationship_uuid                 text NOT NULL,
    artifact_id                       text NOT NULL,
    prior_artifact_version_digest     text NOT NULL,
    repaired_artifact_version_digest  text NOT NULL,
    prior_payload                     jsonb NOT NULL,
    status                            text NOT NULL,
    opened_at                         text NOT NULL,
    recovered_at                      text,
    reopened_at                       text,
    PRIMARY KEY (scope_key, incident_id)
);

CREATE INDEX IF NOT EXISTS coherence_repair_monitors_relationship_idx
    ON coherence_repair_monitors(scope_key, relationship_uuid, status);

CREATE TABLE IF NOT EXISTS policy_contract_versions (
    contract_digest        text PRIMARY KEY,
    payload                jsonb NOT NULL,
    certification          jsonb NOT NULL,
    certification_passed   boolean NOT NULL,
    staged_at              text NOT NULL
);

CREATE TABLE IF NOT EXISTS policy_aliases (
    scope_key                 text NOT NULL,
    alias                     text NOT NULL,
    contract_digest           text NOT NULL REFERENCES policy_contract_versions(contract_digest),
    previous_contract_digest  text,
    updated_at                text NOT NULL,
    PRIMARY KEY (scope_key, alias)
);

CREATE TABLE IF NOT EXISTS policy_shadow_stages (
    stage_id                    text PRIMARY KEY,
    scope_key                   text NOT NULL,
    alias                       text NOT NULL,
    active_contract_digest      text NOT NULL,
    candidate_contract_digest   text NOT NULL REFERENCES policy_contract_versions(contract_digest),
    corpus_digest               text NOT NULL,
    required_episode_count      integer NOT NULL,
    observed_episode_count      integer NOT NULL,
    allowed_disposition_delta   double precision NOT NULL,
    report                      jsonb,
    status                      text NOT NULL,
    started_at                  text NOT NULL,
    completed_at                text
);

CREATE INDEX IF NOT EXISTS policy_shadow_stages_lookup_idx
    ON policy_shadow_stages(scope_key, alias, candidate_contract_digest, status, started_at DESC);

CREATE TABLE IF NOT EXISTS tenant_llm_credentials (
    tenant_id       text PRIMARY KEY,
    provider        text NOT NULL,
    api_key_sealed  text NOT NULL,
    base_url        text NOT NULL DEFAULT '',
    model           text NOT NULL DEFAULT '',
    created_at      text NOT NULL,
    updated_at      text NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_agents (
    tenant_id     text NOT NULL,
    agent_id      text NOT NULL,
    agent_id_key  text NOT NULL DEFAULT '',
    name          text NOT NULL DEFAULT '',
    name_key      text NOT NULL DEFAULT '',
    source        text NOT NULL DEFAULT 'runtime',
    created_at    text NOT NULL,
    last_seen_at  text NOT NULL,
    PRIMARY KEY (tenant_id, agent_id)
);

CREATE INDEX IF NOT EXISTS tenant_agents_tenant_idx ON tenant_agents(tenant_id, last_seen_at);
CREATE INDEX IF NOT EXISTS tenant_agents_id_key_idx ON tenant_agents(tenant_id, agent_id_key);
CREATE INDEX IF NOT EXISTS tenant_agents_name_key_idx ON tenant_agents(tenant_id, name_key);

CREATE TABLE IF NOT EXISTS agent_motive_assignments (
    tenant_id    text NOT NULL,
    agent_id     text NOT NULL,
    motive_name  text NOT NULL,
    source       text NOT NULL,
    created_at   text NOT NULL,
    updated_at   text NOT NULL,
    PRIMARY KEY (tenant_id, agent_id),
    FOREIGN KEY (tenant_id, agent_id) REFERENCES tenant_agents(tenant_id, agent_id)
);

CREATE INDEX IF NOT EXISTS agent_motive_assignments_tenant_idx
    ON agent_motive_assignments(tenant_id, motive_name, updated_at);

CREATE TABLE IF NOT EXISTS tenant_prompt_overrides (
    tenant_id               text PRIMARY KEY,
    prompt_profile          text NOT NULL,
    prompt_profile_version  text NOT NULL,
    override                jsonb NOT NULL,
    created_at              text NOT NULL,
    updated_at              text NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_prompt_versions (
    tenant_id               text NOT NULL,
    version                 text NOT NULL,
    prompt_text             text NOT NULL,
    motive_name             text NOT NULL DEFAULT '',
    source_profile          text NOT NULL DEFAULT '',
    source_profile_version  text NOT NULL DEFAULT '',
    active                  boolean NOT NULL DEFAULT false,
    created_at              text NOT NULL,
    updated_at              text NOT NULL,
    PRIMARY KEY (tenant_id, version)
);

CREATE INDEX IF NOT EXISTS tenant_prompt_versions_tenant_idx
    ON tenant_prompt_versions(tenant_id, active, created_at);

CREATE TABLE IF NOT EXISTS project_memory_config_versions (
    tenant_id      text NOT NULL,
    version        text NOT NULL,
    config         jsonb NOT NULL,
    configured_by  text NOT NULL,
    active         boolean NOT NULL DEFAULT false,
    created_at     text NOT NULL,
    updated_at     text NOT NULL,
    PRIMARY KEY (tenant_id, version)
);

CREATE INDEX IF NOT EXISTS project_memory_config_versions_tenant_idx
    ON project_memory_config_versions(tenant_id, active, created_at);
"""

# --------------------------------------------------------------------------- 2

_V2_STATE_HASH = """
-- Incremental per-scope state hash.
--
-- ``relationship_state_tuples.tuple_json`` is the canonical per-row JSON array
-- for one memory relationship, encoded in Python with exactly the separators and
-- key ordering the SQLite backend uses, so the hash value is engine-independent.
-- Maintaining it on write turns the state hash from a whole-scope recompute
-- (plus one subject lookup per row) into a single indexed aggregate.
--
-- MENTIONS edges are deliberately absent: the hash covers the reconstructable
-- memory-relationship set that byte replay folds from receipts.
CREATE TABLE IF NOT EXISTS relationship_state_tuples (
    relationship_uuid  text PRIMARY KEY REFERENCES relationships(uuid) ON DELETE CASCADE,
    scope_key          text NOT NULL,
    tuple_json         text NOT NULL
);

CREATE INDEX IF NOT EXISTS relationship_state_tuples_scope_idx
    ON relationship_state_tuples(scope_key, relationship_uuid);

-- Memoised scope hash.  ``dirty`` is set by any write touching the scope and
-- cleared when the aggregate is recomputed, so the receipt bracket's second read
-- is free rather than a second full pass.
CREATE TABLE IF NOT EXISTS scope_state_hash (
    scope_key   text PRIMARY KEY,
    state_hash  text NOT NULL,
    dirty       boolean NOT NULL DEFAULT true,
    updated_at  timestamptz NOT NULL DEFAULT now()
);
"""

# --------------------------------------------------------------------------- 3

_V3_VECTOR = """
-- Retrieval fallback (DW-001 contingency).  Embeddings are stored as
-- ``double precision[]`` because that works on any managed Postgres with no
-- extension; ``embedding_vec`` is added separately when pgvector is present so
-- the same rows can be served by an ANN index.  Either way the comparison
-- happens in SQL, never in Python.
CREATE TABLE IF NOT EXISTS relationship_embeddings (
    relationship_uuid  text PRIMARY KEY REFERENCES relationships(uuid) ON DELETE CASCADE,
    scope_key          text NOT NULL,
    dimensions         integer NOT NULL,
    embedding          double precision[] NOT NULL,
    sealed             boolean NOT NULL DEFAULT false,
    updated_at         timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS relationship_embeddings_scope_idx
    ON relationship_embeddings(scope_key);

-- Cosine distance over plain float8[].  IMMUTABLE + PARALLEL SAFE so the planner
-- may use it freely.  Vectors are stored L2-normalised, so the dot product is
-- the cosine; the norms are still divided out defensively.
CREATE OR REPLACE FUNCTION dw_cosine_similarity(a double precision[], b double precision[])
RETURNS double precision
LANGUAGE sql IMMUTABLE PARALLEL SAFE STRICT
AS $$
    SELECT CASE
        WHEN array_length(a, 1) IS DISTINCT FROM array_length(b, 1) THEN NULL
        WHEN na = 0 OR nb = 0 THEN 0
        ELSE dot / (na * nb)
    END
    FROM (
        SELECT
            SUM(x * y) AS dot,
            sqrt(SUM(x * x)) AS na,
            sqrt(SUM(y * y)) AS nb
        FROM unnest(a, b) AS t(x, y)
    ) s;
$$;
"""


# --------------------------------------------------------------------------- 4

_V4_RECEIPT_LEDGER = """
-- WS-11 hash-chained receipt ledger.  Faithful translation of ``_RECEIPTS_DDL``
-- in ``memotron.storage.receipts``, with three deliberate changes:
--
-- * ``sensitive_payload_encrypted`` is a real ``boolean`` instead of SQLite's
--   0/1 integer.  The canonical payload emits ``true``/``false`` either way
--   (``_row_canonical_payload`` coerces with ``bool()``), so the receipt hashes
--   are unaffected.
-- * ``REAL`` becomes ``double precision`` — the same IEEE-754 binary64 the
--   canonical encoder rounds with ``repr(round(x, 10))``.
-- * SQLite's ``_migrate`` probed for ten late-added columns with ``ALTER TABLE
--   ADD COLUMN``; the full final column set is declared here instead.
--
-- ``canonical_payload``, ``event_payload`` and ``retention_components`` stay
-- ``text``.  They are not ``_json``-suffixed document columns: their exact bytes
-- are hashed (``payload_digest`` / ``event_payload_digest``), so re-encoding
-- them as ``jsonb`` — which normalises key order and whitespace — would break
-- every chain.  ``created_at`` stays ISO-8601 ``text`` for the same reason.
CREATE TABLE IF NOT EXISTS memory_receipts (
    receipt_uuid                     text PRIMARY KEY,
    schema_version                   integer NOT NULL,
    tenant_id                        text,
    agent_id                         text,
    scope_key                        text NOT NULL,
    run_uuid                         text NOT NULL,
    run_kind                         text NOT NULL,
    episode_uuid                     text,
    episode_digest                   text,
    event_index                      integer NOT NULL,
    decision_type                    text NOT NULL,
    candidate_uuid                   text,
    candidate_digest                 text,
    source_span_digest               text,
    motive_name                      text,
    motive_version_digest            text,
    effective_policy_digest          text NOT NULL,
    prompt_profile                   text,
    model_identifier                 text,
    extractor_identifier             text,
    embedding_identifier             text,
    formation_contract_digest        text,
    formation_contract_source_trace  text,
    formation_contract_attestation   text,
    use_event_id                     text,
    outcome_event_id                 text,
    event_payload                    text,
    event_payload_digest             text,
    retention_components             text,
    memory_type                      text,
    claim_mode                       text,
    directive_stance                 text,
    relationship_type                text,
    truth_key                        text,
    salience_score                   double precision,
    salience_threshold               double precision,
    dedup_threshold                  double precision,
    dedup_match_relationship_uuid    text,
    dedup_score                      double precision,
    governance_policy_digest         text,
    redaction_digest_before          text,
    redaction_digest_after           text,
    decision_reason                  text NOT NULL,
    decision_result                  text NOT NULL,
    relationship_uuid                text,
    superseded_relationship_uuid     text,
    successor_relationship_uuid      text,
    graph_state_hash_before          text,
    graph_state_hash_after           text,
    sensitive_payload                text,
    sensitive_payload_encrypted      boolean NOT NULL,
    canonical_payload                text NOT NULL,
    payload_digest                   text NOT NULL,
    previous_receipt_hash            text NOT NULL,
    receipt_hash                     text NOT NULL,
    created_at                       text NOT NULL
);

-- THE integrity constraint.  ``verify_chain`` proves the chain is dense by
-- walking ``event_index`` 0..n-1, and replay reads a gap as a missing receipt;
-- this index is what stops two concurrent writers from both claiming an index.
CREATE UNIQUE INDEX IF NOT EXISTS memory_receipts_run_event_idx
    ON memory_receipts(run_uuid, event_index);

-- ``receipts_for_scope`` (erasure sweep, no-silent-mutation check).  The trailing
-- columns are the read's exact ORDER BY, with ``COLLATE "C"`` so the index order
-- is byte order — what SQLite's BINARY collation gives.  ``scope_key`` keeps the
-- database default collation so the equality predicate can still seek it.
CREATE INDEX IF NOT EXISTS memory_receipts_scope_order_idx
    ON memory_receipts(scope_key, created_at COLLATE "C", run_uuid COLLATE "C", event_index);

-- ``negative_space``: the non-materializing ``decision_result`` gate plus the
-- half-open ``created_at`` window and the same byte-ordered tie-break.
CREATE INDEX IF NOT EXISTS memory_receipts_negative_space_idx
    ON memory_receipts(decision_result, created_at COLLATE "C", run_uuid COLLATE "C", event_index);

CREATE INDEX IF NOT EXISTS memory_receipts_episode_uuid_idx ON memory_receipts(episode_uuid);
CREATE INDEX IF NOT EXISTS memory_receipts_decision_type_idx ON memory_receipts(decision_type);
CREATE INDEX IF NOT EXISTS memory_receipts_motive_name_idx ON memory_receipts(motive_name);
CREATE INDEX IF NOT EXISTS memory_receipts_created_at_idx
    ON memory_receipts(created_at COLLATE "C");
CREATE INDEX IF NOT EXISTS memory_receipts_candidate_digest_idx
    ON memory_receipts(candidate_digest);

-- One checkpoint per run: the primary key is the uniqueness the ledger relies on
-- to reject a second checkpoint for the same run.
CREATE TABLE IF NOT EXISTS run_checkpoints (
    run_uuid                 text PRIMARY KEY,
    run_kind                 text NOT NULL,
    job_name                 text NOT NULL,
    tenant_id                text,
    agent_id                 text,
    scope_key                text NOT NULL,
    motive_version_digest    text,
    effective_policy_digest  text NOT NULL,
    first_receipt_hash       text NOT NULL,
    last_receipt_hash        text NOT NULL,
    receipt_count            integer NOT NULL,
    merkle_root              text NOT NULL,
    graph_state_hash_before  text,
    graph_state_hash_after   text,
    replay_status            text NOT NULL,
    created_at               text NOT NULL
);
"""


# --------------------------------------------------------------------------- 5

_V5_INDEX_CORRECTIONS = """
-- ``dream_decisions_scope_idx`` (migration 1) indexes ``payload->>'scope_key'``,
-- which never matches: a serialised DreamDecisionRecord carries its scope as a
-- nested object and ``MemoryScope.key`` is a computed property, not a stored
-- field.  The index was therefore empty of useful entries and the scope sweep
-- fell back to a filter.  Replace it with one on the terms that are actually
-- persisted.
DROP INDEX IF EXISTS dream_decisions_scope_idx;

CREATE INDEX IF NOT EXISTS dream_decisions_scope_terms_idx
    ON dream_decisions ((payload->'scope'->>'kind'), (payload->'scope'->>'scope_id'));

-- SQLite enforced one agent per casefolded id and one per casefolded name within
-- a tenant with UNIQUE indexes; migration 1 created both as non-unique, so the
-- database could not reject a duplicate and ``ON CONFLICT`` could not name them
-- as arbiters.  Promote both, restoring DW-018's binding invariant to the engine
-- rather than leaving it to an advisory lock in application code.
DROP INDEX IF EXISTS tenant_agents_id_key_idx;
DROP INDEX IF EXISTS tenant_agents_name_key_idx;

CREATE UNIQUE INDEX IF NOT EXISTS tenant_agents_agent_id_key_unique_idx
    ON tenant_agents (tenant_id, agent_id_key)
    WHERE agent_id_key <> '';

CREATE UNIQUE INDEX IF NOT EXISTS tenant_agents_name_key_unique_idx
    ON tenant_agents (tenant_id, name_key)
    WHERE name_key <> '';

-- The newest-first artifact listing tie-breaks on artifact_id; without it in the
-- index the plan carries a sort node purely for the tiebreak.
DROP INDEX IF EXISTS live_artifacts_active_idx;

CREATE INDEX IF NOT EXISTS live_artifacts_active_idx
    ON live_artifacts(scope_key, active, updated_at DESC, artifact_id);
"""


# --------------------------------------------------------------------------- 6

# The SQLite-first canonicalization registries (WS-17), the raw/quarantine store
# (WS-24), and the WS-26 derivation-DAG epoch layer, brought to Postgres parity.
# Column types, primary keys, and CHECK constraints mirror the SQLite DDL
# (``storage/sqlite.py``) so the interface behaves identically on both engines;
# JSON-bearing columns stay ``text`` (a serialized string), not ``jsonb``, so the
# row→dict mappers are byte-identical to the substrate's.  ``epoch_id`` is folded
# straight into the two registries here rather than added by a later ALTER (the
# SQLite backend's ``_ensure_epoch_registry_columns`` probe) — an append-only
# migration has no legacy rows to widen.
_V6_REGISTRIES_QUARANTINE_AND_EPOCHS = """
-- WS-17 T16: per-scope canonical predicate registry.
CREATE TABLE IF NOT EXISTS predicate_canon (
    scope_key             text NOT NULL,
    predicate_normalized  text NOT NULL,
    canonical_predicate   text NOT NULL,
    decided_by            text NOT NULL,
    embedding_identifier  text,
    cosine                double precision,
    created_at            text NOT NULL,
    epoch_id              text NOT NULL DEFAULT '',
    PRIMARY KEY (scope_key, predicate_normalized)
);

CREATE INDEX IF NOT EXISTS predicate_canon_scope_idx
    ON predicate_canon(scope_key, created_at COLLATE "C");

-- WS-17 T16b: per-scope entity alias registry.
CREATE TABLE IF NOT EXISTS entity_canon (
    scope_key             text NOT NULL,
    name_normalized       text NOT NULL,
    canonical_name        text NOT NULL,
    status                text NOT NULL CHECK (status IN ('active','proposed','rejected')),
    link_score            double precision,
    link_signals          text,
    decided_by            text NOT NULL,
    embedding_identifier  text,
    proposed_at           text NOT NULL,
    resolved_at           text,
    resolved_by           text,
    epoch_id              text NOT NULL DEFAULT '',
    PRIMARY KEY (scope_key, name_normalized)
);

CREATE INDEX IF NOT EXISTS entity_canon_scope_idx
    ON entity_canon(scope_key, status, proposed_at COLLATE "C");

-- WS-24: the raw/quarantine store — stage-1 output (PENDING) and stage-3
-- abstain verdicts (QUARANTINED/PROMOTED/DISCARDED) share one table.
CREATE TABLE IF NOT EXISTS quarantined_candidates (
    candidate_uuid              text PRIMARY KEY,
    scope_key                   text NOT NULL,
    episode_uuid                text,
    reason                      text NOT NULL,
    detail                      text NOT NULL DEFAULT '',
    saves_step                  text,
    subject                     text NOT NULL DEFAULT '',
    predicate                   text NOT NULL DEFAULT '',
    object                      text NOT NULL DEFAULT '',
    proposed_relationship_type  text,
    proposed_memory_type        text,
    candidate_payload           text NOT NULL,
    candidate_digest            text NOT NULL,
    instruction_set             text,
    motive_name                 text,
    quarantined_at              text NOT NULL,
    status                      text NOT NULL DEFAULT 'quarantined',
    resolved_at                 text,
    resolved_by                 text,
    resolution_note             text,
    promoted_relationship_uuid  text
);

CREATE INDEX IF NOT EXISTS quarantined_candidates_scope_idx
    ON quarantined_candidates(scope_key, status, quarantined_at COLLATE "C");

-- WS-26 T2: the derivation DAG's branch spine.  parent_epoch_id NULL marks a
-- scope's root epoch.  Forking copies a pointer, never relationship rows.
CREATE TABLE IF NOT EXISTS graph_epochs (
    epoch_id                 text PRIMARY KEY,
    scope_key                text NOT NULL,
    parent_epoch_id          text,
    created_at               text NOT NULL,
    label                    text NOT NULL DEFAULT '',
    status                   text NOT NULL DEFAULT 'open',
    tier                     text,
    overrides_json           text NOT NULL DEFAULT '{}',
    shadow_store_path        text,
    pre_adopt_snapshot_json  text
);

CREATE INDEX IF NOT EXISTS graph_epochs_scope_idx
    ON graph_epochs(scope_key, created_at COLLATE "C");

-- WS-26 T2: which receipted run(s) populated an epoch.  No FK to graph_epochs:
-- epochs.py records a run here on the SHADOW store, whose own graph_epochs table
-- never carries a row for the epoch it is computing (that bookkeeping row lives
-- only in the main store) — epoch_id is a cross-store coordination key.
CREATE TABLE IF NOT EXISTS epoch_runs (
    epoch_id     text NOT NULL,
    run_uuid     text NOT NULL,
    recorded_at  text NOT NULL,
    PRIMARY KEY (epoch_id, run_uuid)
);

-- WS-26 T2/T6: the per-scope HEAD pointer.  adopt/rollback flip this row only.
CREATE TABLE IF NOT EXISTS active_epochs (
    scope_key   text PRIMARY KEY,
    epoch_id    text NOT NULL REFERENCES graph_epochs(epoch_id),
    updated_at  text NOT NULL
);
"""


# --------------------------------------------------------------------------- 7

# The last SQLite-first surface reaching Postgres parity: the dream-worker claim
# ledger (WS-17 #17) and the multi-agent promotion-endorsement store (WS-19).
# ``dream_claims`` is a single-row-per-key lease table; ``exclusive_write_transaction``
# (a pg_advisory_xact_lock, the faithful twin of SQLite's BEGIN IMMEDIATE)
# serialises the check-then-claim critical section across replicas.
_V7_CLAIMS_AND_ENDORSEMENTS = """
-- WS-17 #17: the dream worker's claim ledger — one live lease per claim key.
CREATE TABLE IF NOT EXISTS dream_claims (
    claim_key        text PRIMARY KEY,
    claimed_by_run   text NOT NULL,
    claimed_at       text NOT NULL
);

CREATE INDEX IF NOT EXISTS dream_claims_run_idx ON dream_claims(claimed_by_run);

-- WS-19: object-level promotion endorsements — one vote per (candidate, agent).
CREATE TABLE IF NOT EXISTS promotion_endorsements (
    candidate_episode_uuid  text NOT NULL,
    agent_id                text NOT NULL,
    rationale               text NOT NULL,
    endorsed_at             text NOT NULL,
    PRIMARY KEY (candidate_episode_uuid, agent_id)
);

CREATE INDEX IF NOT EXISTS promotion_endorsements_episode_idx
    ON promotion_endorsements(candidate_episode_uuid, endorsed_at COLLATE "C");
"""


# --------------------------------------------------------------------------- 8

# WS-26 T8: the re-dream recompute tier now rides the WS-11 run checkpoint, so a
# receipt reader can tell a governance-only re-dream from a full re-extract
# without joining to the epoch row.  Additive, nullable — an ordinary
# (non-re-dream) run leaves both NULL.
_V8_REDREAM_TIER_ON_CHECKPOINT = """
ALTER TABLE run_checkpoints ADD COLUMN IF NOT EXISTS redream_tier text;
ALTER TABLE run_checkpoints ADD COLUMN IF NOT EXISTS redream_tier_reason text;
"""


# ``(version, name, sql)`` in application order.  Append only.

#: The per-key registry (DW-026 / DW-030). `key_alias` is the PRIMARY KEY, so global
#: uniqueness is enforced by the database rather than by a comment -- that property is
#: the whole reason this is not a row in `tenant_agents`, whose uniqueness is
#: tenant-scoped.
#:
#: THIS IS VERSION 9, NOT PART OF V1, AND THAT MATTERS. It was first written inside
#: _V1_CORE, where it worked on every fresh database and would have reached no existing
#: one: `migrate()` skips any version already in `schema_migrations`, so a deployed store
#: would never create the table and `bind_key_principal` would raise UndefinedTable at
#: runtime. Every test passed, because the test fixture drops and recreates the schema on
#: every run and therefore always applied V1 fresh. See the header rule at the top of this
#: file: migrations are append-only.
_V9_KEY_PRINCIPALS = """
CREATE TABLE IF NOT EXISTS key_principals (
    key_alias                text PRIMARY KEY,
    principal_id             text NOT NULL,
    tenant_id                text NOT NULL,
    agent_id                 text,
    role                     text NOT NULL DEFAULT 'user',
    default_scope_key        text,
    allowed_scope_keys_json  text NOT NULL DEFAULT '[]',
    created_at               text NOT NULL,
    updated_at               text NOT NULL
);

CREATE INDEX IF NOT EXISTS key_principals_tenant_idx ON key_principals(tenant_id);
"""
#: The index the base ledger's docstring has always claimed to seek, and which existed on
#: SQLite only (`storage/receipts.py`, `memory_receipts_relationship_uuid_idx`). Without it
#: `latest_formation_receipt_digest` is a sequential scan of an append-only ledger that only
#: grows. Its caller is the promotion lineage path (`agent_memory/_publish.py`).
#:
#: VERSION 10, NOT AN EDIT TO AN APPLIED VERSION -- migrations are append-only (see the header).
#: 9 is the per-key registry directly above; both landed together in the combined identity +
#: storage branch, and a store that has already recorded 9 will apply only 10.
_V10_RECEIPT_RELATIONSHIP_INDEX = """
CREATE INDEX IF NOT EXISTS memory_receipts_relationship_uuid_idx
    ON memory_receipts (relationship_uuid);
"""

_V11_TENANT_LLM_EMBEDDING = """
-- WS-17 T18 reached SQLite and never Postgres (#163). SQLite's method took
-- embedding_provider/base_url/model and this engine's did not, so
-- `_migrate_llm_credentials` -- which passes all three unconditionally -- raised
-- TypeError against any Postgres store. Additive and defaulted to '' so existing
-- rows keep their meaning: blank is "no embedding endpoint configured", which
-- resolves to the hermetic LocalEmbeddingTransport exactly as before.
--
-- A NEW version rather than an edit to _V1_CORE. `migrate()` skips versions already
-- in schema_migrations, so amending an applied migration reaches every fresh
-- database and no existing one -- and `latest` is now an existing one.
ALTER TABLE tenant_llm_credentials
    ADD COLUMN IF NOT EXISTS embedding_provider text NOT NULL DEFAULT '';
ALTER TABLE tenant_llm_credentials
    ADD COLUMN IF NOT EXISTS embedding_base_url text NOT NULL DEFAULT '';
ALTER TABLE tenant_llm_credentials
    ADD COLUMN IF NOT EXISTS embedding_model text NOT NULL DEFAULT '';
"""


MIGRATIONS: tuple[tuple[int, str, str], ...] = (
    (1, "core_graph_and_operational_tables", _V1_CORE),
    (2, "incremental_scope_state_hash", _V2_STATE_HASH),
    (3, "vector_retrieval_fallback", _V3_VECTOR),
    (4, "receipt_ledger", _V4_RECEIPT_LEDGER),
    (5, "index_corrections", _V5_INDEX_CORRECTIONS),
    (6, "registries_quarantine_and_epochs", _V6_REGISTRIES_QUARANTINE_AND_EPOCHS),
    (7, "dream_claims_and_promotion_endorsements", _V7_CLAIMS_AND_ENDORSEMENTS),
    (8, "redream_tier_on_checkpoint", _V8_REDREAM_TIER_ON_CHECKPOINT),
    (9, "key_principals", _V9_KEY_PRINCIPALS),
    (10, "receipt_relationship_index", _V10_RECEIPT_RELATIONSHIP_INDEX),
    (11, "tenant_llm_embedding_endpoint", _V11_TENANT_LLM_EMBEDDING),
)


# Applied opportunistically after the numbered migrations, only when the pgvector
# extension is actually installed.  Kept out of MIGRATIONS because availability is
# an environment property: the same schema version is valid with or without it.
PGVECTOR_COLUMN = """
ALTER TABLE relationship_embeddings
    ADD COLUMN IF NOT EXISTS embedding_vec vector;
"""
"""The column the pgvector read path needs. Dimensionless ON PURPOSE: this table stores
``dimensions`` PER ROW (see the CREATE TABLE above), because the embedding width follows the
configured transport -- 256 for the local hasher, 1536+ for a gateway model. A ``vector(N)``
column would pin every scope to one transport."""

PGVECTOR_INDEX = """
CREATE INDEX IF NOT EXISTS relationship_embeddings_vec_idx
    ON relationship_embeddings USING hnsw (embedding_vec vector_cosine_ops);
"""
"""An ANN index, and PURELY an optimization -- the ``<=>`` operator works without it.

It cannot currently succeed: HNSW requires a fixed dimension and the column above deliberately
has none, so Postgres raises ``InvalidParameterValue: column does not have dimensions``. Kept,
separated, and attempted non-fatally so the day someone pins a dimension it starts working with
no further change.

This statement used to share a transaction with the column above, which is why a store with
pgvector correctly installed reported "pgvector unavailable": the index raised, the transaction
rolled back the column too, and the whole path was disabled. Observed on latest 2026-09-04 with
`vector 0.8.5` installed and working."""
