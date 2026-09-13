"""Schema DDL and the additive column/index migrations.

Mirrors :mod:`memotron.storage.postgres._migrations`, which isolates the same
concern. Every _ensure_* helper is idempotent and additive -- run on every
construction, safe on an already-migrated database -- because a store opened by an
older build must keep working rather than requiring a migration step."""

from __future__ import annotations

from typing import TYPE_CHECKING

from memotron.identity import (
    agent_id_key,
    agent_name_key,
    normalize_agent_name,
)
from memotron.storage.base import (
    normalize_key as normalize_key,
)

if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class MigrationsMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    def _migrate(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                uuid TEXT PRIMARY KEY,
                graph_key TEXT NOT NULL UNIQUE,
                labels_json TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT
            );

            CREATE TABLE IF NOT EXISTS relationships (
                uuid TEXT PRIMARY KEY,
                source_uuid TEXT NOT NULL REFERENCES nodes(uuid),
                target_uuid TEXT NOT NULL REFERENCES nodes(uuid),
                type TEXT NOT NULL,
                properties_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                valid_from TEXT,
                valid_to TEXT
            );

            CREATE INDEX IF NOT EXISTS relationships_type_idx ON relationships(type);

            CREATE TABLE IF NOT EXISTS episodes (
                uuid TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS processed_episodes (
                episode_uuid TEXT PRIMARY KEY REFERENCES episodes(uuid),
                processed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS episodes_scope_event_idx
                ON episodes(
                    json_extract(payload_json, '$.scope.kind'),
                    json_extract(payload_json, '$.scope.scope_id'),
                    json_extract(payload_json, '$.metadata.agent_memory_event')
                );

            CREATE TABLE IF NOT EXISTS episode_processing (
                episode_uuid TEXT NOT NULL REFERENCES episodes(uuid),
                consumer_key TEXT NOT NULL,
                processed_at TEXT NOT NULL,
                PRIMARY KEY (episode_uuid, consumer_key)
            );

            CREATE INDEX IF NOT EXISTS episode_processing_consumer_idx
                ON episode_processing(consumer_key, processed_at);

            CREATE TABLE IF NOT EXISTS dream_claims (
                claim_key TEXT PRIMARY KEY,
                claimed_by_run TEXT NOT NULL,
                claimed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS dream_claims_run_idx
                ON dream_claims(claimed_by_run);

            CREATE TABLE IF NOT EXISTS job_state (
                job_name TEXT PRIMARY KEY,
                last_run TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS dream_job_runs (
                uuid TEXT PRIMARY KEY,
                ran_at TEXT NOT NULL,
                job_name TEXT NOT NULL,
                job_kind TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS dream_job_runs_ran_at_idx ON dream_job_runs(ran_at);
            CREATE INDEX IF NOT EXISTS dream_job_runs_job_name_idx ON dream_job_runs(job_name);

            CREATE TABLE IF NOT EXISTS dream_decisions (
                uuid TEXT PRIMARY KEY,
                ran_at TEXT NOT NULL,
                job_name TEXT NOT NULL,
                job_kind TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                decision_type TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS dream_decisions_ran_at_idx ON dream_decisions(ran_at);
            CREATE INDEX IF NOT EXISTS dream_decisions_job_name_idx ON dream_decisions(job_name);
            CREATE INDEX IF NOT EXISTS dream_decisions_agent_id_idx ON dream_decisions(agent_id);
            CREATE INDEX IF NOT EXISTS dream_decisions_decision_type_idx ON dream_decisions(decision_type);

            CREATE TABLE IF NOT EXISTS governance_keys (
                scope_key TEXT NOT NULL,
                subject_key TEXT NOT NULL DEFAULT '',
                wrapped_dek TEXT NOT NULL,
                kek_id TEXT NOT NULL,
                key_algorithm TEXT NOT NULL,
                created_at TEXT NOT NULL,
                shredded_at TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (scope_key, subject_key)
            );

            CREATE TABLE IF NOT EXISTS formation_contract_signing_keys (
                signer_id TEXT PRIMARY KEY,
                wrapped_private_key TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_use_events (
                use_id TEXT PRIMARY KEY,
                relationship_uuid TEXT NOT NULL REFERENCES relationships(uuid),
                scope_key TEXT NOT NULL,
                kind TEXT NOT NULL,
                task_run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                used_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                query_digest TEXT,
                UNIQUE(scope_key, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS memory_use_events_relationship_idx
                ON memory_use_events(relationship_uuid, used_at);
            CREATE INDEX IF NOT EXISTS memory_use_events_task_idx
                ON memory_use_events(scope_key, task_run_id, used_at);

            CREATE TABLE IF NOT EXISTS memory_outcome_events (
                outcome_id TEXT PRIMARY KEY,
                use_id TEXT NOT NULL REFERENCES memory_use_events(use_id),
                scope_key TEXT NOT NULL,
                task_run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                judged_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(scope_key, idempotency_key)
            );

            CREATE INDEX IF NOT EXISTS memory_outcome_events_use_idx
                ON memory_outcome_events(use_id, judged_at);

            CREATE TABLE IF NOT EXISTS memory_prune_ghosts (
                relationship_uuid TEXT PRIMARY KEY REFERENCES relationships(uuid),
                scope_key TEXT NOT NULL,
                prune_receipt_uuid TEXT NOT NULL,
                pruned_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                restorable INTEGER NOT NULL DEFAULT 1,
                restored_at TEXT
            );

            CREATE INDEX IF NOT EXISTS memory_prune_ghosts_scope_idx
                ON memory_prune_ghosts(scope_key, restorable, restored_at);

            CREATE TABLE IF NOT EXISTS quarantined_candidates (
                candidate_uuid TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                episode_uuid TEXT,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                saves_step TEXT,
                subject TEXT NOT NULL DEFAULT '',
                predicate TEXT NOT NULL DEFAULT '',
                object TEXT NOT NULL DEFAULT '',
                proposed_relationship_type TEXT,
                proposed_memory_type TEXT,
                candidate_payload TEXT NOT NULL,
                candidate_digest TEXT NOT NULL,
                instruction_set TEXT,
                motive_name TEXT,
                quarantined_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'quarantined',
                resolved_at TEXT,
                resolved_by TEXT,
                resolution_note TEXT,
                promoted_relationship_uuid TEXT
            );

            CREATE INDEX IF NOT EXISTS quarantined_candidates_scope_idx
                ON quarantined_candidates(scope_key, status, quarantined_at);

            CREATE TABLE IF NOT EXISTS live_artifacts (
                scope_key TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                artifact_class TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                quarantined_at TEXT,
                positive_outcomes INTEGER NOT NULL DEFAULT 0,
                negative_outcomes INTEGER NOT NULL DEFAULT 0,
                last_outcome_at TEXT,
                PRIMARY KEY (scope_key, artifact_id)
            );

            CREATE INDEX IF NOT EXISTS live_artifacts_active_idx
                ON live_artifacts(scope_key, active, updated_at);

            CREATE TABLE IF NOT EXISTS live_artifact_outcome_events (
                event_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                task_run_id TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                verdict TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(scope_key, idempotency_key),
                FOREIGN KEY (scope_key, artifact_id)
                    REFERENCES live_artifacts(scope_key, artifact_id)
            );

            CREATE INDEX IF NOT EXISTS live_artifact_outcome_events_artifact_idx
                ON live_artifact_outcome_events(scope_key, artifact_id, occurred_at);

            CREATE TABLE IF NOT EXISTS live_artifact_versions (
                scope_key TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                version_digest TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, artifact_id, version_digest)
            );

            CREATE INDEX IF NOT EXISTS live_artifact_versions_latest_idx
                ON live_artifact_versions(scope_key, artifact_id, captured_at DESC);

            CREATE TABLE IF NOT EXISTS coherence_repair_monitors (
                scope_key TEXT NOT NULL,
                incident_id TEXT NOT NULL,
                relationship_uuid TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                prior_artifact_version_digest TEXT NOT NULL,
                repaired_artifact_version_digest TEXT NOT NULL,
                prior_payload_json TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_at TEXT NOT NULL,
                recovered_at TEXT,
                reopened_at TEXT,
                PRIMARY KEY (scope_key, incident_id)
            );

            CREATE INDEX IF NOT EXISTS coherence_repair_monitors_relationship_idx
                ON coherence_repair_monitors(scope_key, relationship_uuid, status);

            CREATE TABLE IF NOT EXISTS policy_contract_versions (
                contract_digest TEXT PRIMARY KEY,
                payload_json TEXT NOT NULL,
                certification_json TEXT NOT NULL,
                certification_passed INTEGER NOT NULL,
                staged_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS policy_aliases (
                scope_key TEXT NOT NULL,
                alias TEXT NOT NULL,
                contract_digest TEXT NOT NULL REFERENCES policy_contract_versions(contract_digest),
                previous_contract_digest TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, alias)
            );

            CREATE TABLE IF NOT EXISTS policy_shadow_stages (
                stage_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                alias TEXT NOT NULL,
                active_contract_digest TEXT NOT NULL,
                candidate_contract_digest TEXT NOT NULL REFERENCES policy_contract_versions(contract_digest),
                corpus_digest TEXT NOT NULL,
                required_episode_count INTEGER NOT NULL,
                observed_episode_count INTEGER NOT NULL,
                allowed_disposition_delta REAL NOT NULL,
                report_json TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE INDEX IF NOT EXISTS policy_shadow_stages_lookup_idx
                ON policy_shadow_stages(scope_key, alias, candidate_contract_digest, status, started_at DESC);

            CREATE TABLE IF NOT EXISTS tenant_llm_credentials (
                tenant_id TEXT PRIMARY KEY,
                provider TEXT NOT NULL,
                api_key_sealed TEXT NOT NULL,
                base_url TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tenant_agents (
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                agent_id_key TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL DEFAULT '',
                name_key TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'runtime',
                created_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, agent_id)
            );

            CREATE INDEX IF NOT EXISTS tenant_agents_tenant_idx ON tenant_agents(tenant_id, last_seen_at);

            CREATE TABLE IF NOT EXISTS key_principals (
                key_alias TEXT PRIMARY KEY,
                principal_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                agent_id TEXT,
                role TEXT NOT NULL DEFAULT 'user',
                default_scope_key TEXT,
                allowed_scope_keys_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS key_principals_tenant_idx
                ON key_principals(tenant_id);

            CREATE TABLE IF NOT EXISTS agent_motive_assignments (
                tenant_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                motive_name TEXT NOT NULL,
                source TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, agent_id),
                FOREIGN KEY (tenant_id, agent_id)
                    REFERENCES tenant_agents(tenant_id, agent_id)
            );

            CREATE INDEX IF NOT EXISTS agent_motive_assignments_tenant_idx
                ON agent_motive_assignments(tenant_id, motive_name, updated_at);

            CREATE TABLE IF NOT EXISTS tenant_prompt_overrides (
                tenant_id TEXT PRIMARY KEY,
                prompt_profile TEXT NOT NULL,
                prompt_profile_version TEXT NOT NULL,
                override_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tenant_prompt_versions (
                tenant_id TEXT NOT NULL,
                version TEXT NOT NULL,
                prompt_text TEXT NOT NULL,
                motive_name TEXT NOT NULL DEFAULT '',
                source_profile TEXT NOT NULL DEFAULT '',
                source_profile_version TEXT NOT NULL DEFAULT '',
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, version)
            );

            CREATE INDEX IF NOT EXISTS tenant_prompt_versions_tenant_idx
            ON tenant_prompt_versions(tenant_id, active, created_at);

            CREATE TABLE IF NOT EXISTS project_memory_config_versions (
                tenant_id TEXT NOT NULL,
                version TEXT NOT NULL,
                config_json TEXT NOT NULL,
                configured_by TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, version)
            );

            CREATE INDEX IF NOT EXISTS project_memory_config_versions_tenant_idx
            ON project_memory_config_versions(tenant_id, active, created_at);

            CREATE TABLE IF NOT EXISTS predicate_canon (
                scope_key TEXT NOT NULL,
                predicate_normalized TEXT NOT NULL,
                canonical_predicate TEXT NOT NULL,
                decided_by TEXT NOT NULL,
                embedding_identifier TEXT,
                cosine REAL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (scope_key, predicate_normalized)
            );

            CREATE INDEX IF NOT EXISTS predicate_canon_scope_idx
            ON predicate_canon(scope_key, created_at);

            CREATE TABLE IF NOT EXISTS entity_canon (
                scope_key TEXT NOT NULL,
                name_normalized TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','proposed','rejected')),
                link_score REAL,
                link_signals TEXT,
                decided_by TEXT NOT NULL,
                embedding_identifier TEXT,
                proposed_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                PRIMARY KEY (scope_key, name_normalized)
            );

            CREATE INDEX IF NOT EXISTS entity_canon_scope_idx
            ON entity_canon(scope_key, status, proposed_at);

            CREATE TABLE IF NOT EXISTS promotion_endorsements (
                candidate_episode_uuid TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                rationale TEXT NOT NULL,
                endorsed_at TEXT NOT NULL,
                PRIMARY KEY (candidate_episode_uuid, agent_id)
            );

            CREATE INDEX IF NOT EXISTS promotion_endorsements_episode_idx
            ON promotion_endorsements(candidate_episode_uuid, endorsed_at);

            -- WS-26 T2: the derivation DAG's branch spine.  ``parent_epoch_id``
            -- NULL means a scope's root epoch (created lazily by
            -- ``ensure_root_epoch``).  Forking never copies relationship rows —
            -- only this bookkeeping row and (once populated) the active_epochs
            -- pointer move.
            CREATE TABLE IF NOT EXISTS graph_epochs (
                epoch_id TEXT PRIMARY KEY,
                scope_key TEXT NOT NULL,
                parent_epoch_id TEXT,
                created_at TEXT NOT NULL,
                label TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                tier TEXT,
                overrides_json TEXT NOT NULL DEFAULT '{}',
                shadow_store_path TEXT,
                pre_adopt_snapshot_json TEXT
            );

            CREATE INDEX IF NOT EXISTS graph_epochs_scope_idx
            ON graph_epochs(scope_key, created_at);

            -- WS-26 T2: which receipted run(s) populated an epoch — the join
            -- between the derivation DAG's run nodes and its epoch nodes.
            -- No REFERENCES graph_epochs(epoch_id): epochs.py records a run
            -- here on the SHADOW store that computed it, whose own
            -- graph_epochs table never carries a row for the epoch it is
            -- computing (that bookkeeping row lives only in the main store).
            -- epoch_id is a cross-store coordination key, not a same-store FK.
            CREATE TABLE IF NOT EXISTS epoch_runs (
                epoch_id TEXT NOT NULL,
                run_uuid TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                PRIMARY KEY (epoch_id, run_uuid)
            );

            -- WS-26 T2/T6: the per-scope HEAD pointer.  A scope resolves to
            -- exactly one active epoch once initialized; adopt/rollback flip
            -- this row only — no relationship or registry row is touched by
            -- the flip itself.
            CREATE TABLE IF NOT EXISTS active_epochs (
                scope_key TEXT PRIMARY KEY,
                epoch_id TEXT NOT NULL REFERENCES graph_epochs(epoch_id),
                updated_at TEXT NOT NULL
            );
            """
        )
        self._ensure_tenant_agent_identity_schema()
        self._ensure_governance_keys_schema()
        self._ensure_relationship_context_index()
        self._ensure_tenant_llm_embedding_schema()
        self._ensure_epoch_registry_columns()
        self._ensure_use_event_query_digest_schema()
        self._connection.commit()

    def _ensure_use_event_query_digest_schema(self) -> None:
        """WS-22 T29: additive ``query_digest`` column on memory_use_events.

        Mirrors the tenant_llm_credentials additive-column pattern: existing
        stores gain a nullable column (existing rows read back NULL — the
        UseEvent model default), new stores create it inline.  The canonical
        event payload lives in ``payload_json``; the column exists so the
        repeat-search lineage is queryable without hydrating every event.
        """
        existing = {str(row[1]) for row in self._connection.execute("PRAGMA table_info(memory_use_events)").fetchall()}
        if "query_digest" not in existing:
            self._connection.execute("ALTER TABLE memory_use_events ADD COLUMN query_digest TEXT")

    def _ensure_epoch_registry_columns(self) -> None:
        """WS-26 T9: additive ``epoch_id`` lineage column on the canonicalization
        registries (``entity_canon`` / ``predicate_canon``).

        The primary key stays ``(scope_key, name_normalized)`` /
        ``(scope_key, predicate_normalized)`` — a name/predicate is still one
        row per scope, exactly as WS-17 defined it.  ``epoch_id`` records
        which epoch's recompute last wrote or overlaid that row, purely for
        lineage/diff/audit (T5's registry deltas) and for
        :meth:`_registry_state_digest`.  A branched re-dream's registry
        isolation comes from running against a physically separate shadow
        store (see ``memotron.epochs``), never from filtering this column
        inside one shared database — so this column is additive bookkeeping,
        not a live query-time discriminator, and existing rows read back
        ``epoch_id=''`` (the universal base layer), byte-identical to
        pre-WS-26 behaviour.
        """
        for table in ("entity_canon", "predicate_canon"):
            existing = {str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})").fetchall()}
            if "epoch_id" not in existing:
                self._connection.execute(f"ALTER TABLE {table} ADD COLUMN epoch_id TEXT NOT NULL DEFAULT ''")

    def _ensure_tenant_llm_embedding_schema(self) -> None:
        """WS-17 T18: additive embedding-endpoint columns on tenant_llm_credentials.

        Mirrors the ReceiptLedger additive-column pattern: existing rows keep
        their schema and semantics; the three new columns default to '' (no
        embedding endpoint configured — hermetic LocalEmbeddingTransport).
        """
        existing = {
            str(row[1]) for row in self._connection.execute("PRAGMA table_info(tenant_llm_credentials)").fetchall()
        }
        additive_columns = {
            "embedding_provider": "TEXT NOT NULL DEFAULT ''",
            "embedding_base_url": "TEXT NOT NULL DEFAULT ''",
            "embedding_model": "TEXT NOT NULL DEFAULT ''",
        }
        for column, sql_type in additive_columns.items():
            if column not in existing:
                self._connection.execute(f"ALTER TABLE tenant_llm_credentials ADD COLUMN {column} {sql_type}")

    def _ensure_tenant_agent_identity_schema(self) -> None:
        """Backfill canonical identity keys and enforce tenant-local uniqueness."""

        columns = {str(row["name"]) for row in self._connection.execute("PRAGMA table_info(tenant_agents)").fetchall()}
        if "agent_id_key" not in columns:
            self._connection.execute("ALTER TABLE tenant_agents ADD COLUMN agent_id_key TEXT NOT NULL DEFAULT ''")
        if "name_key" not in columns:
            self._connection.execute("ALTER TABLE tenant_agents ADD COLUMN name_key TEXT NOT NULL DEFAULT ''")

        rows = self._connection.execute(
            "SELECT tenant_id, agent_id, name FROM tenant_agents ORDER BY tenant_id, created_at"
        ).fetchall()
        claimed_ids: dict[tuple[str, str], str] = {}
        claimed_names: dict[tuple[str, str], str] = {}
        for row in rows:
            tenant_id = str(row["tenant_id"])
            agent_id = str(row["agent_id"])
            agent_name = str(row["name"]).strip() or f"{agent_id} agent"
            try:
                normalized_id_key = agent_id_key(agent_id)
                normalized_name = normalize_agent_name(agent_name)
                normalized_name_key = agent_name_key(normalized_name)
            except ValueError as exc:
                raise RuntimeError(
                    f"tenant agent registry contains an invalid identity for "
                    f"tenant {tenant_id!r}, agent_id {agent_id!r}: {exc}"
                ) from exc
            id_slot = (tenant_id, normalized_id_key)
            name_slot = (tenant_id, normalized_name_key)
            if id_slot in claimed_ids:
                raise RuntimeError(
                    f"tenant {tenant_id!r} has case-insensitive duplicate agent IDs "
                    f"{claimed_ids[id_slot]!r} and {agent_id!r}"
                )
            if name_slot in claimed_names:
                raise RuntimeError(
                    f"tenant {tenant_id!r} has duplicate agent name {normalized_name!r} "
                    f"for agent IDs {claimed_names[name_slot]!r} and {agent_id!r}"
                )
            claimed_ids[id_slot] = agent_id
            claimed_names[name_slot] = agent_id
            self._connection.execute(
                """
                UPDATE tenant_agents
                SET agent_id_key = ?, name = ?, name_key = ?
                WHERE tenant_id = ? AND agent_id = ?
                """,
                (
                    normalized_id_key,
                    normalized_name,
                    normalized_name_key,
                    tenant_id,
                    agent_id,
                ),
            )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS tenant_agents_agent_id_key_unique_idx
            ON tenant_agents(tenant_id, agent_id_key)
            """
        )
        self._connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS tenant_agents_name_key_unique_idx
            ON tenant_agents(tenant_id, name_key)
            """
        )

    def _ensure_governance_keys_schema(self) -> None:
        """WS-12: fail fast on a pre-envelope governance_keys table (raw DEK storage).

        Old graphs stored the DEK itself as ``key_bytes_hex``; the envelope schema
        persists only the KEK-wrapped DEK.  There is no in-place migration of raw
        key material — regenerate the graph (all non-raw data is regenerable).
        """
        columns = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_info(governance_keys)").fetchall()
        }
        if columns and "wrapped_dek" not in columns:
            raise RuntimeError(
                "governance_keys table uses the pre-WS-12 raw-key schema; "
                "regenerate the graph store (envelope key management stores only wrapped DEKs)"
            )

    def _ensure_relationship_context_index(self) -> None:
        """WS-3×WS-4 §101: build the index-backed two-tier read of the relationship store.

        Promote the JSON properties that define context-visibility — ``scope_key``,
        ``status``, and the ``active_in_context`` demotion flag — into VIRTUAL generated
        columns, then build a composite index over them.  This lets the default
        context-visible profile read (:meth:`context_visible_relationships`) seek the
        bounded working set via the index instead of scanning the whole store, while the
        evidence/search/historical paths continue to read the full store.

        ``gen_context_visible`` is 1 when context-visible (flag true or absent) and 0 when
        demoted (flag false), mirroring the Python rule
        ``properties.get("active_in_context") is not False``.

        VIRTUAL generated columns compute on read, so this migrates existing graph files
        without rewriting any rows.  Idempotent: only missing columns are added.
        """
        # table_xinfo (not table_info) lists generated columns too, so re-opening an
        # already-migrated graph file detects the existing gen_* columns and stays idempotent.
        existing = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_xinfo(relationships)").fetchall()
        }
        generated_columns = {
            "gen_scope_key": "json_extract(properties_json, '$.scope_key')",
            "gen_status": "json_extract(properties_json, '$.status')",
            "gen_context_visible": "(json_extract(properties_json, '$.active_in_context') IS NOT 0)",
        }
        for column, expression in generated_columns.items():
            if column not in existing:
                self._connection.execute(
                    f"ALTER TABLE relationships ADD COLUMN {column} GENERATED ALWAYS AS ({expression}) VIRTUAL"
                )
        self._connection.execute(
            "CREATE INDEX IF NOT EXISTS relationships_ctx_idx "
            "ON relationships(gen_scope_key, gen_status, gen_context_visible)"
        )
        # WS-5: endpoint indexes so retrieval expansion frontiers seek instead of
        # scanning — SQLite never auto-indexes the child side of an FK.
        self._connection.execute("CREATE INDEX IF NOT EXISTS relationships_source_idx ON relationships(source_uuid)")
        self._connection.execute("CREATE INDEX IF NOT EXISTS relationships_target_idx ON relationships(target_uuid)")
        # WS-5: same generated-column treatment for nodes, so seed-node
        # resolution and the erasure sweep seek one scope instead of scanning
        # every tenant's nodes.
        existing_node_columns = {
            str(row["name"]) for row in self._connection.execute("PRAGMA table_xinfo(nodes)").fetchall()
        }
        if "gen_scope_key" not in existing_node_columns:
            self._connection.execute(
                "ALTER TABLE nodes ADD COLUMN gen_scope_key "
                "GENERATED ALWAYS AS (json_extract(properties_json, '$.scope_key')) VIRTUAL"
            )
        self._connection.execute("CREATE INDEX IF NOT EXISTS nodes_scope_idx ON nodes(gen_scope_key)")
