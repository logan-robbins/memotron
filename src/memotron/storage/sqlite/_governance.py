"""Tenant/agent identity, envelope keys, and crypto-shred.

Mirrors :mod:`memotron.storage.postgres._governance`. The consequential part is
key lifecycle: get_or_create_governance_key mints a scope DEK, shred_governance_key
destroys it, and afterwards reveal returns the shredded placeholder while the
receipt chain still verifies -- receipts hash the stored representation, never raw
text. purge_tenant_state is the same idea at tenant granularity."""

from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from memotron.crypto import (
    CONTENT_KEY_BYTES,
    ContentKeyUnavailableError,
    reveal_content,
    reveal_json,
    seal_content,
)
from memotron.identity import (
    agent_id_key,
    agent_name_key,
    normalize_agent_id,
    normalize_agent_name,
)
from memotron.models import (
    DreamDecisionRecord,
    FormationContractAttestation,
    ScopeKind,
)
from memotron.storage._shared import (
    tenant_agent_from_row,
    tenant_prompt_version_from_row,
)
from memotron.storage._shared._governance import (
    formation_signer_id,
    llm_credential_is_readable,
    resolve_embedding_endpoint,
)

LLM_CREDENTIAL_SUBJECT_KEY = "llm_credentials"


if TYPE_CHECKING:
    # Type-checking only: at runtime the base is plain `object`, so the composed
    # MRO is unchanged. See _protocol.py for why this is not a real base class.
    from memotron.storage.sqlite._protocol import ComposedSQLiteBackend

    _Base = ComposedSQLiteBackend
else:
    _Base = object


class GovernancePlaneMixin(_Base):
    """Composed into :class:`SQLiteStorageBackend`."""

    # -- the per-key registry (DW-026 / DW-030) -----------------------------------

    @staticmethod
    def _normalize_key_alias(key_alias: str) -> str:
        """Canonical alias. Stripped, and blank refused.

        Normalised identically on write and read on purpose: #158 F2 was exactly this
        divergence one layer up -- the client cached on a raw tenant id while storage
        stripped it, so a whitespace variant pinned a revoked credential indefinitely.
        """
        normalized = key_alias.strip()
        if not normalized:
            raise ValueError("key_alias cannot be blank")
        return normalized

    def bind_key_principal(
        self,
        *,
        key_alias: str,
        principal_id: str,
        tenant_id: str,
        agent_id: str | None = None,
        role: str = "user",
        default_scope_key: str | None = None,
        allowed_scope_keys: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        alias = self._normalize_key_alias(key_alias)
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_principal = principal_id.strip()
        if not normalized_principal:
            raise ValueError("principal_id cannot be blank")
        now = datetime.now(UTC).isoformat()

        existing = self._connection.execute(
            "SELECT tenant_id, created_at FROM key_principals WHERE key_alias = ?",
            (alias,),
        ).fetchone()
        if existing is not None and str(existing["tenant_id"]) != normalized_tenant:
            # The DW-030 guarantee. The gateway enforces global alias uniqueness; so does
            # this table, so a second tenant cannot claim an alias and make the lookup
            # ambiguous. Refused rather than overwritten: overwriting would silently move
            # a live caller to another tenant.
            raise ValueError(f"key_alias {alias!r} is already bound to tenant {str(existing['tenant_id'])!r}")

        created = str(existing["created_at"]) if existing is not None else now
        self._connection.execute(
            """
            INSERT INTO key_principals (
                key_alias, principal_id, tenant_id, agent_id, role,
                default_scope_key, allowed_scope_keys_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(key_alias) DO UPDATE SET
                principal_id = excluded.principal_id,
                agent_id = excluded.agent_id,
                role = excluded.role,
                default_scope_key = excluded.default_scope_key,
                allowed_scope_keys_json = excluded.allowed_scope_keys_json,
                updated_at = excluded.updated_at
            """,
            (
                alias,
                normalized_principal,
                normalized_tenant,
                agent_id,
                role.strip() or "user",
                default_scope_key,
                json.dumps(list(allowed_scope_keys)),
                created,
                now,
            ),
        )
        self._commit()
        resolved = self.principal_for_key_alias(alias)
        assert resolved is not None
        return resolved

    def principal_for_key_alias(self, key_alias: str) -> dict[str, Any] | None:
        alias = self._normalize_key_alias(key_alias)
        row = self._connection.execute(
            """
            SELECT key_alias, principal_id, tenant_id, agent_id, role,
                   default_scope_key, allowed_scope_keys_json
            FROM key_principals
            WHERE key_alias = ?
            """,
            (alias,),
        ).fetchone()
        if row is None:
            return None
        return {
            "key_alias": str(row["key_alias"]),
            "principal_id": str(row["principal_id"]),
            "tenant_id": str(row["tenant_id"]),
            "agent_id": None if row["agent_id"] is None else str(row["agent_id"]),
            "role": str(row["role"]),
            "default_scope_key": (None if row["default_scope_key"] is None else str(row["default_scope_key"])),
            "allowed_scope_keys": tuple(json.loads(str(row["allowed_scope_keys_json"]))),
        }

    def unbind_key_principal(self, key_alias: str) -> bool:
        alias = self._normalize_key_alias(key_alias)
        cursor = self._connection.execute("DELETE FROM key_principals WHERE key_alias = ?", (alias,))
        self._commit()
        return bool(cursor.rowcount)

    def register_tenant_agent(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        name: str,
        source: str = "runtime",
    ) -> dict[str, Any]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_agent = normalize_agent_id(agent_id)
        normalized_name = normalize_agent_name(name)
        normalized_agent_key = agent_id_key(normalized_agent)
        normalized_name_key = agent_name_key(normalized_name)
        normalized_source = source.strip() or "runtime"
        now = datetime.now(UTC).isoformat()
        existing = self._connection.execute(
            """
            SELECT tenant_id, agent_id, agent_id_key, name, name_key, source,
                   created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = ? AND agent_id_key = ?
            """,
            (normalized_tenant, normalized_agent_key),
        ).fetchone()
        if existing is not None:
            if str(existing["name_key"]) != normalized_name_key:
                raise ValueError(
                    f"agent_id {normalized_agent!r} is already registered to "
                    f"agent_name {str(existing['name'])!r} in tenant {normalized_tenant!r}"
                )
            self._connection.execute(
                """
                UPDATE tenant_agents
                SET last_seen_at = ?
                WHERE tenant_id = ? AND agent_id_key = ?
                """,
                (now, normalized_tenant, normalized_agent_key),
            )
            self._commit()
            return {
                "tenant_id": normalized_tenant,
                "agent_id": str(existing["agent_id"]),
                "agent_name": str(existing["name"]),
                "source": str(existing["source"]),
                "created_at": str(existing["created_at"]),
                "last_seen_at": now,
                "created": False,
            }
        name_owner = self._connection.execute(
            """
            SELECT agent_id
            FROM tenant_agents
            WHERE tenant_id = ? AND name_key = ?
            """,
            (normalized_tenant, normalized_name_key),
        ).fetchone()
        if name_owner is not None:
            raise ValueError(
                f"agent_name {normalized_name!r} is already registered to "
                f"agent_id {str(name_owner['agent_id'])!r} in tenant {normalized_tenant!r}"
            )
        try:
            self._connection.execute(
                """
                INSERT INTO tenant_agents (
                    tenant_id, agent_id, agent_id_key, name, name_key, source,
                    created_at, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_tenant,
                    normalized_agent,
                    normalized_agent_key,
                    normalized_name,
                    normalized_name_key,
                    normalized_source,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError as exc:
            self._connection.rollback()
            raced_registration = self.tenant_agent(
                tenant_id=normalized_tenant,
                agent_id=normalized_agent,
            )
            if (
                raced_registration is not None
                and agent_name_key(str(raced_registration["agent_name"])) == normalized_name_key
            ):
                return self.register_tenant_agent(
                    tenant_id=normalized_tenant,
                    agent_id=normalized_agent,
                    name=normalized_name,
                    source=normalized_source,
                )
            raise ValueError(
                f"agent identity collision in tenant {normalized_tenant!r}; agent_id and agent_name must each be unique"
            ) from exc
        self._commit()
        return {
            "tenant_id": normalized_tenant,
            "agent_id": normalized_agent,
            "agent_name": normalized_name,
            "source": normalized_source,
            "created_at": now,
            "last_seen_at": now,
            "created": True,
        }

    def known_tenant_ids(self) -> tuple[str, ...]:
        """Return every tenant id this graph holds persisted tenant state for.

        Tenant state lives in several tenant-keyed tables; a tenant that only
        ever registered an agent, or only ever sealed a credential, is just as
        real as one that saved a project-memory policy. The union is what an
        operator surface must reason about when it has to decide whether a
        launch-supplied tenant id actually matches this graph.
        """
        tables = (
            "tenant_agents",
            "tenant_llm_credentials",
            "tenant_prompt_versions",
            "tenant_prompt_overrides",
            "project_memory_config_versions",
            "agent_motive_assignments",
        )
        found: set[str] = set()
        for table in tables:
            try:
                rows = self._connection.execute(f"SELECT DISTINCT tenant_id FROM {table}").fetchall()
            except sqlite3.OperationalError:
                continue
            for row in rows:
                value = str(row["tenant_id"]).strip()
                if value:
                    found.add(value)
        return tuple(sorted(found))

    def tenant_agent(self, *, tenant_id: str, agent_id: str) -> dict[str, Any] | None:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_agent_key = agent_id_key(agent_id)
        row = self._connection.execute(
            """
            SELECT tenant_id, agent_id, name, source, created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = ? AND agent_id_key = ?
            """,
            (normalized_tenant, normalized_agent_key),
        ).fetchone()
        return tenant_agent_from_row(row) if row is not None else None

    def tenant_agents(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        rows = self._connection.execute(
            """
            SELECT tenant_id, agent_id, name, source, created_at, last_seen_at
            FROM tenant_agents
            WHERE tenant_id = ?
            ORDER BY created_at, agent_id
            """,
            (normalized_tenant,),
        ).fetchall()
        return tuple(tenant_agent_from_row(row) for row in rows)

    def deregister_tenant_agent(self, *, tenant_id: str, agent_id: str) -> bool:
        """Detach one agent from a tenant WITHOUT touching its agent-scope memory.

        An agent scope key (``agent:<agent_id>``) does not embed a tenant id, so
        deregistration is an ownership change, not an erasure: rows in
        ``agent:<agent_id>`` are deliberately left alone. Tenant migration uses
        this to move agent ownership before ``purge_tenant_state`` runs, so the
        purge's agent sweep cannot delete memory that now belongs to the
        destination tenant.

        The agent's ``agent_motive_assignments`` row is removed first, since it
        holds a foreign key onto ``tenant_agents``. Returns False when the agent
        was not registered to this tenant.
        """
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_agent_key = agent_id_key(normalize_agent_id(agent_id))
        row = self._connection.execute(
            "SELECT agent_id FROM tenant_agents WHERE tenant_id = ? AND agent_id_key = ?",
            (normalized_tenant, normalized_agent_key),
        ).fetchone()
        if row is None:
            return False
        self._connection.execute(
            "DELETE FROM agent_motive_assignments WHERE tenant_id = ? AND agent_id = ?",
            (normalized_tenant, str(row["agent_id"])),
        )
        self._connection.execute(
            "DELETE FROM tenant_agents WHERE tenant_id = ? AND agent_id_key = ?",
            (normalized_tenant, normalized_agent_key),
        )
        self._connection.commit()
        return True

    def set_agent_motive_assignment(
        self,
        *,
        tenant_id: str,
        agent_id: str,
        motive_name: str,
        source: str,
    ) -> dict[str, str]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_agent = self._normalize_agent_id(agent_id)
        normalized_motive = self._normalize_non_blank(motive_name, "motive_name")
        normalized_source = self._normalize_non_blank(source, "source")
        registered = self._connection.execute(
            """
            SELECT 1 FROM tenant_agents
            WHERE tenant_id = ? AND agent_id = ?
            """,
            (normalized_tenant, normalized_agent),
        ).fetchone()
        if registered is None:
            raise ValueError(f"agent {normalized_agent!r} is not registered for tenant {normalized_tenant!r}")
        now = datetime.now(UTC).isoformat()
        existing = self._connection.execute(
            """
            SELECT created_at FROM agent_motive_assignments
            WHERE tenant_id = ? AND agent_id = ?
            """,
            (normalized_tenant, normalized_agent),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing is not None else now
        self._connection.execute(
            """
            INSERT INTO agent_motive_assignments (
                tenant_id, agent_id, motive_name, source, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id, agent_id) DO UPDATE SET
                motive_name = excluded.motive_name,
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                normalized_tenant,
                normalized_agent,
                normalized_motive,
                normalized_source,
                created_at,
                now,
            ),
        )
        self._commit()
        return {
            "tenant_id": normalized_tenant,
            "agent_id": normalized_agent,
            "motive_name": normalized_motive,
            "source": normalized_source,
            "created_at": created_at,
            "updated_at": now,
        }

    def agent_motive_assignment(self, *, tenant_id: str, agent_id: str) -> dict[str, str] | None:
        row = self._connection.execute(
            """
            SELECT tenant_id, agent_id, motive_name, source, created_at, updated_at
            FROM agent_motive_assignments
            WHERE tenant_id = ? AND agent_id = ?
            """,
            (
                self._normalize_tenant_id(tenant_id),
                self._normalize_agent_id(agent_id),
            ),
        ).fetchone()
        return (
            {
                key: str(row[key])
                for key in (
                    "tenant_id",
                    "agent_id",
                    "motive_name",
                    "source",
                    "created_at",
                    "updated_at",
                )
            }
            if row is not None
            else None
        )

    def agent_motive_assignments(self, tenant_id: str) -> tuple[dict[str, str], ...]:
        rows = self._connection.execute(
            """
            SELECT tenant_id, agent_id, motive_name, source, created_at, updated_at
            FROM agent_motive_assignments
            WHERE tenant_id = ?
            ORDER BY agent_id
            """,
            (self._normalize_tenant_id(tenant_id),),
        ).fetchall()
        return tuple(
            {
                key: str(row[key])
                for key in (
                    "tenant_id",
                    "agent_id",
                    "motive_name",
                    "source",
                    "created_at",
                    "updated_at",
                )
            }
            for row in rows
        )

    def set_tenant_prompt_override(
        self,
        *,
        tenant_id: str,
        prompt_profile: str,
        prompt_profile_version: str,
        override: dict[str, Any],
    ) -> dict[str, Any]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_profile = self._normalize_non_blank(prompt_profile, "prompt_profile")
        normalized_version = self._normalize_non_blank(prompt_profile_version, "prompt_profile_version")
        now = datetime.now(UTC).isoformat()
        existing = self._connection.execute(
            "SELECT created_at FROM tenant_prompt_overrides WHERE tenant_id = ?",
            (normalized_tenant,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing is not None else now
        override_json = self._json_dumps(override, "tenant prompt override")
        self._connection.execute(
            """
            INSERT INTO tenant_prompt_overrides (
                tenant_id, prompt_profile, prompt_profile_version, override_json, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                prompt_profile = excluded.prompt_profile,
                prompt_profile_version = excluded.prompt_profile_version,
                override_json = excluded.override_json,
                updated_at = excluded.updated_at
            """,
            (
                normalized_tenant,
                normalized_profile,
                normalized_version,
                override_json,
                created_at,
                now,
            ),
        )
        self._commit()
        return self.tenant_prompt_override(normalized_tenant) or {}

    def tenant_prompt_override(self, tenant_id: str) -> dict[str, Any] | None:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        row = self._connection.execute(
            """
            SELECT tenant_id, prompt_profile, prompt_profile_version, override_json, created_at, updated_at
            FROM tenant_prompt_overrides WHERE tenant_id = ?
            """,
            (normalized_tenant,),
        ).fetchone()
        if row is None:
            return None
        override = json.loads(str(row["override_json"]))
        if not isinstance(override, dict):
            raise ValueError(f"tenant prompt override for {normalized_tenant!r} is invalid")
        return {
            "tenant_id": str(row["tenant_id"]),
            "prompt_profile": str(row["prompt_profile"]),
            "prompt_profile_version": str(row["prompt_profile_version"]),
            "override": override,
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def save_tenant_prompt_version(
        self,
        *,
        tenant_id: str,
        prompt_text: str,
        motive_name: str = "",
        source_profile: str = "",
        source_profile_version: str = "",
    ) -> dict[str, Any]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_text = self._normalize_non_blank(prompt_text, "prompt_text")
        normalized_motive = motive_name.strip()
        normalized_profile = source_profile.strip()
        normalized_profile_version = source_profile_version.strip()
        version = self._next_tenant_prompt_version(normalized_tenant)
        now = datetime.now(UTC).isoformat()
        self._connection.execute(
            "UPDATE tenant_prompt_versions SET active = 0 WHERE tenant_id = ?",
            (normalized_tenant,),
        )
        self._connection.execute(
            """
            INSERT INTO tenant_prompt_versions (
                tenant_id,
                version,
                prompt_text,
                motive_name,
                source_profile,
                source_profile_version,
                active,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                normalized_tenant,
                version,
                normalized_text,
                normalized_motive,
                normalized_profile,
                normalized_profile_version,
                now,
                now,
            ),
        )
        self._commit()
        return self.active_tenant_prompt_version(normalized_tenant) or {}

    def active_tenant_prompt_version(self, tenant_id: str) -> dict[str, Any] | None:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        row = self._connection.execute(
            """
            SELECT tenant_id, version, prompt_text, motive_name, source_profile,
                   source_profile_version, active, created_at, updated_at
            FROM tenant_prompt_versions
            WHERE tenant_id = ? AND active = 1
            ORDER BY created_at DESC, version DESC
            LIMIT 1
            """,
            (normalized_tenant,),
        ).fetchone()
        return tenant_prompt_version_from_row(row)

    def tenant_prompt_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        rows = self._connection.execute(
            """
            SELECT tenant_id, version, prompt_text, motive_name, source_profile,
                   source_profile_version, active, created_at, updated_at
            FROM tenant_prompt_versions
            WHERE tenant_id = ?
            ORDER BY created_at DESC, version DESC
            """,
            (normalized_tenant,),
        ).fetchall()
        return tuple(version for row in rows if (version := tenant_prompt_version_from_row(row)) is not None)

    def _next_tenant_prompt_version(self, tenant_id: str) -> str:
        rows = self._connection.execute(
            "SELECT version FROM tenant_prompt_versions WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        highest = 0
        for row in rows:
            version = str(row["version"])
            if version.startswith("tenant-v") and version.removeprefix("tenant-v").isdigit():
                highest = max(highest, int(version.removeprefix("tenant-v")))
        return f"tenant-v{highest + 1}"

    def save_project_memory_config(
        self,
        *,
        tenant_id: str,
        config: dict[str, Any],
        configured_by: str,
    ) -> dict[str, Any]:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_configured_by = self._normalize_non_blank(configured_by, "configured_by")
        config_json = self._json_dumps(config, "project memory config")
        version = self._next_project_memory_config_version(normalized_tenant)
        now = datetime.now(UTC).isoformat()
        self._connection.execute(
            "UPDATE project_memory_config_versions SET active = 0 WHERE tenant_id = ?",
            (normalized_tenant,),
        )
        self._connection.execute(
            """
            INSERT INTO project_memory_config_versions (
                tenant_id, version, config_json, configured_by, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 1, ?, ?)
            """,
            (
                normalized_tenant,
                version,
                config_json,
                normalized_configured_by,
                now,
                now,
            ),
        )
        self._commit()
        return self.active_project_memory_config(normalized_tenant) or {}

    def active_project_memory_config(self, tenant_id: str) -> dict[str, Any] | None:
        row = self._connection.execute(
            """
            SELECT tenant_id, version, config_json, configured_by, active, created_at, updated_at
            FROM project_memory_config_versions
            WHERE tenant_id = ? AND active = 1
            ORDER BY created_at DESC, rowid DESC
            LIMIT 1
            """,
            (self._normalize_tenant_id(tenant_id),),
        ).fetchone()
        return self._project_memory_config_from_row(row)

    def project_memory_config_versions(self, tenant_id: str) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT tenant_id, version, config_json, configured_by, active, created_at, updated_at
            FROM project_memory_config_versions
            WHERE tenant_id = ?
            ORDER BY created_at DESC, rowid DESC
            """,
            (self._normalize_tenant_id(tenant_id),),
        ).fetchall()
        return tuple(config for row in rows if (config := self._project_memory_config_from_row(row)) is not None)

    def _project_memory_config_from_row(self, row: Any) -> dict[str, Any] | None:
        if row is None:
            return None
        payload = json.loads(str(row["config_json"]))
        if not isinstance(payload, dict):
            raise ValueError("stored project memory config must be a JSON object")
        return {
            **payload,
            "tenant_id": str(row["tenant_id"]),
            "version": str(row["version"]),
            "configured_by": str(row["configured_by"]),
            "active": bool(row["active"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _next_project_memory_config_version(self, tenant_id: str) -> str:
        rows = self._connection.execute(
            "SELECT version FROM project_memory_config_versions WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchall()
        highest = 0
        for row in rows:
            version = str(row["version"])
            if version.startswith("project-v") and version.removeprefix("project-v").isdigit():
                highest = max(highest, int(version.removeprefix("project-v")))
        return f"project-v{highest + 1}"

    def get_or_create_governance_key(self, scope_key: str, subject_key: str = "") -> bytes:
        """Return (or generate) the per-scope 256-bit content DEK.

        The DEK is generated randomly, wrapped by the KeyManager's KEK, and
        persisted only in wrapped form.  A crypto-shredded scope no longer
        accepts crypto-governed writes: requesting its key is a hard error.
        """
        row = self._connection.execute(
            "SELECT wrapped_dek, shredded_at FROM governance_keys WHERE scope_key = ? AND subject_key = ?",
            (scope_key, subject_key),
        ).fetchone()
        if row is not None:
            if str(row["wrapped_dek"]):
                return self.key_manager.unwrap_dek(str(row["wrapped_dek"]))
            raise ContentKeyUnavailableError(
                f"governance key for scope {scope_key!r} was crypto-shredded; the scope's "
                "content is unrecoverable and the scope no longer accepts crypto-governed writes"
            )

        dek = secrets.token_bytes(CONTENT_KEY_BYTES)
        self._connection.execute(
            """
            INSERT INTO governance_keys (
                scope_key, subject_key, wrapped_dek, kek_id, key_algorithm, created_at, shredded_at
            )
            VALUES (?, ?, ?, ?, ?, ?, '')
            """,
            (
                scope_key,
                subject_key,
                self.key_manager.wrap_dek(dek),
                self.key_manager.key_id(),
                "AES-256-GCM envelope; HMAC-SHA256 commitments",
                datetime.now(UTC).isoformat(),
            ),
        )
        self._commit()
        return dek

    def shred_governance_key(self, scope_key: str, subject_key: str = "") -> bool:
        """Destroy the wrapped DEK so the scope's sealed content becomes unrecoverable.

        Zeroes ``wrapped_dek`` and stamps ``shredded_at``.  The DEK was random and
        exists nowhere else, so the KEK cannot regenerate it — this is the
        cryptographic erase.  Returns True if a key existed, False otherwise.

        Production: additionally call KMS.ScheduleKeyDeletion on a per-scope CMK.
        """
        row = self._connection.execute(
            "SELECT wrapped_dek FROM governance_keys WHERE scope_key = ? AND subject_key = ?",
            (scope_key, subject_key),
        ).fetchone()
        if row is None:
            return False
        self._connection.execute(
            """
            UPDATE governance_keys
            SET wrapped_dek = '', shredded_at = ?
            WHERE scope_key = ? AND subject_key = ?
            """,
            (datetime.now(UTC).isoformat(), scope_key, subject_key),
        )
        self._commit()
        return True

    def get_governance_key(self, scope_key: str, subject_key: str = "") -> bytes | None:
        """Return the unwrapped DEK, or None if the key was shredded or never created."""
        row = self._connection.execute(
            "SELECT wrapped_dek FROM governance_keys WHERE scope_key = ? AND subject_key = ?",
            (scope_key, subject_key),
        ).fetchone()
        if row is None:
            return None
        wrapped = str(row["wrapped_dek"])
        if not wrapped:
            return None  # Shredded
        return self.key_manager.unwrap_dek(wrapped)

    def governance_key_state(self, scope_key: str, subject_key: str = "") -> dict[str, Any] | None:
        """Key-lifecycle facts for the erasure certificate.

        Returns None when no key was ever provisioned; otherwise a dict with
        ``shredded``, ``created_at``, ``shredded_at``, ``kek_id``, and
        ``key_algorithm``.  Never returns key material.
        """
        row = self._connection.execute(
            """
            SELECT wrapped_dek, kek_id, key_algorithm, created_at, shredded_at
            FROM governance_keys WHERE scope_key = ? AND subject_key = ?
            """,
            (scope_key, subject_key),
        ).fetchone()
        if row is None:
            return None
        return {
            "scope_key": scope_key,
            "subject_key": subject_key,
            "shredded": not str(row["wrapped_dek"]),
            "kek_id": str(row["kek_id"]),
            "key_algorithm": str(row["key_algorithm"]),
            "created_at": str(row["created_at"]),
            "shredded_at": str(row["shredded_at"]),
        }

    def _formation_signer_rows(self, signer_id: str) -> tuple[str | None, tuple[tuple[str, str], ...]]:
        """Two SELECTs for the signer diagnostic. No decision here -- see `_shared/_governance`."""
        row = self._connection.execute(
            "SELECT wrapped_private_key FROM formation_contract_signing_keys WHERE signer_id = ?",
            (signer_id,),
        ).fetchone()
        others = self._connection.execute(
            """
            SELECT signer_id, created_at FROM formation_contract_signing_keys
            WHERE signer_id != ? ORDER BY created_at
            """,
            (signer_id,),
        ).fetchall()
        wrapped = None if row is None else str(row["wrapped_private_key"])
        return wrapped, tuple((str(r["signer_id"]), str(r["created_at"])) for r in others)

    def attest_formation_contract(
        self, *, contract_digest: str, certification_verdict: str = "uncertified"
    ) -> FormationContractAttestation:
        """Sign a DSSE/in-toto verdict using a graph-local KMS-wrapped key.

        The private Ed25519 material is never stored in the graph database in
        plaintext; the configured ``KeyManager`` owns the wrapping key.  A
        production graph can therefore substitute a KMS-backed manager without
        changing the receipt or attestation format.
        """
        signer_id = formation_signer_id(self.key_manager)
        row = self._connection.execute(
            "SELECT wrapped_private_key FROM formation_contract_signing_keys WHERE signer_id = ?",
            (signer_id,),
        ).fetchone()
        if row is None:
            private_key = Ed25519PrivateKey.generate().private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
            self._connection.execute(
                """
                INSERT INTO formation_contract_signing_keys (signer_id, wrapped_private_key, created_at)
                VALUES (?, ?, ?)
                """,
                (signer_id, self.key_manager.wrap_dek(private_key), datetime.now(UTC).isoformat()),
            )
            self._commit()
        else:
            private_key = self.key_manager.unwrap_dek(str(row["wrapped_private_key"]))
        from memotron.attestations import attest_formation_contract

        return attest_formation_contract(
            contract_digest=contract_digest,
            certification_verdict=certification_verdict,
            private_key_bytes=private_key,
            key_id=signer_id,
        )

    def set_tenant_llm_credentials(
        self,
        *,
        tenant_id: str,
        provider: str,
        api_key: str,
        base_url: str = "",
        model: str = "",
        embedding_provider: str | None = None,
        embedding_base_url: str | None = None,
        embedding_model: str | None = None,
    ) -> dict[str, Any]:
        """Seal a tenant's LLM credential.

        The three ``embedding_*`` arguments are tri-state: ``None`` (the
        default) PRESERVES whatever embedding endpoint the tenant already has,
        a value sets it, and ``""`` explicitly clears it.  Rotating an
        extraction key must never silently move a populated tenant into a
        different vector space, which is what an unconditional overwrite did.
        """
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_provider = self._normalize_llm_provider(provider)
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ValueError("api_key cannot be blank")
        now = datetime.now(UTC).isoformat()
        credential_key = self.get_or_create_governance_key(
            f"tenant:{normalized_tenant}",
            subject_key=LLM_CREDENTIAL_SUBJECT_KEY,
        )
        sealed_key = seal_content(normalized_key, credential_key)
        existing = self._connection.execute(
            """
            SELECT created_at, embedding_provider, embedding_base_url, embedding_model
            FROM tenant_llm_credentials WHERE tenant_id = ?
            """,
            (normalized_tenant,),
        ).fetchone()
        created_at = str(existing["created_at"]) if existing is not None else now
        # WS-17 T18: additive embedding-endpoint entry — same sealed key, same provider
        # vocabulary; blank means "no embedding endpoint configured". The tri-state rule
        # lives in _shared/_governance because Postgres needs the identical one and did not
        # get it (#163); only the row read below is engine-specific.
        normalized_embedding_provider, embedding_base_url, embedding_model = resolve_embedding_endpoint(
            embedding_provider=embedding_provider,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            existing=(
                (
                    str(existing["embedding_provider"]),
                    str(existing["embedding_base_url"]),
                    str(existing["embedding_model"]),
                )
                if existing is not None
                else None
            ),
        )
        self._connection.execute(
            """
            INSERT INTO tenant_llm_credentials (
                tenant_id, provider, api_key_sealed, base_url, model,
                embedding_provider, embedding_base_url, embedding_model,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tenant_id) DO UPDATE SET
                provider = excluded.provider,
                api_key_sealed = excluded.api_key_sealed,
                base_url = excluded.base_url,
                model = excluded.model,
                embedding_provider = excluded.embedding_provider,
                embedding_base_url = excluded.embedding_base_url,
                embedding_model = excluded.embedding_model,
                updated_at = excluded.updated_at
            """,
            (
                normalized_tenant,
                normalized_provider,
                sealed_key,
                base_url.strip(),
                model.strip(),
                normalized_embedding_provider,
                embedding_base_url,
                embedding_model,
                created_at,
                now,
            ),
        )
        self._commit()
        return self.tenant_llm_credential_state(normalized_tenant) or {}

    def tenant_llm_credentials(self, tenant_id: str) -> dict[str, Any] | None:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        row = self._connection.execute(
            "SELECT * FROM tenant_llm_credentials WHERE tenant_id = ?",
            (normalized_tenant,),
        ).fetchone()
        if row is None:
            return None
        key = self.get_governance_key(
            f"tenant:{normalized_tenant}",
            subject_key=LLM_CREDENTIAL_SUBJECT_KEY,
        )
        if key is None:
            raise ContentKeyUnavailableError(f"tenant LLM credentials for {normalized_tenant!r} are unavailable")
        api_key = reveal_content(str(row["api_key_sealed"]), key)
        if not isinstance(api_key, str) or not api_key:
            raise ValueError(f"tenant LLM credential for {normalized_tenant!r} is invalid")
        return {
            "tenant_id": normalized_tenant,
            "provider": str(row["provider"]),
            "api_key": api_key,
            "base_url": str(row["base_url"]),
            "model": str(row["model"]),
            "embedding_provider": str(row["embedding_provider"]),
            "embedding_base_url": str(row["embedding_base_url"]),
            "embedding_model": str(row["embedding_model"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def _llm_credential_is_shredded(self, normalized_tenant: str) -> bool:
        """Was this tenant's credential key DELIBERATELY destroyed (RTBF crypto-shred)?

        Added after review: without it, a shredded tenant reported `api_key_unreadable`, the
        same signal as "the KEK is wrong". Those are opposite situations -- one is a designed,
        certificate-issuing erasure, the other is an incident -- and an operator paged by the
        second must not be paged by the first.

        Requires BOTH an empty ``wrapped_dek`` and a ``shredded_at`` timestamp. Red-team
        review pointed out that `shredded` alone is literally "the column is empty", so
        anything that blanks that column -- a buggy job, a mis-scoped sweep, a hostile writer
        -- would have downgraded itself from the alarm to routine compliance work. Demanding
        the timestamp means the claim rests on evidence the erasure path actually writes;
        a blanked key with no erasure record stays `api_key_unreadable`, which is correct,
        because that is exactly what it is.
        """
        state = self.governance_key_state(f"tenant:{normalized_tenant}", subject_key=LLM_CREDENTIAL_SUBJECT_KEY)
        if not state or not state.get("shredded"):
            return False
        return bool(str(state.get("shredded_at") or "").strip())

    def _llm_credential_is_readable(self, normalized_tenant: str, sealed: str) -> bool:
        """Key lookup is per-engine; the DECISION is shared (#123).

        Split this way on purpose: the lookup reaches the store, which is what makes the two
        engines differ at all, while the "can it be decrypted" rule must not diverge. #163 and
        #164 were both one engine changing and the other not.
        """
        try:
            key = self.get_governance_key(f"tenant:{normalized_tenant}", subject_key=LLM_CREDENTIAL_SUBJECT_KEY)
        except (ValueError, ContentKeyUnavailableError):
            # ONLY key failures. An earlier version caught bare `Exception`, which meant a
            # database fault was reported as "credential unreadable" -- the same dishonest
            # status this whole change exists to remove, reintroduced inside the fix for it.
            # A storage error is a fault and must propagate as one.
            return False
        return llm_credential_is_readable(sealed, key)

    def tenant_llm_credential_state(self, tenant_id: str) -> dict[str, Any] | None:
        """Non-secret status for one tenant's LLM credential.

        ``has_api_key`` MEANS "a USABLE key is present", not "a row exists". (#123)

        It was hardcoded ``True`` whenever the row existed, so a credential sealed under a
        KEK the process can no longer unwrap -- every Postgres pod after a restart, before
        the durable-KEK fix -- reported itself **configured**. `probe_kek.py` prints
        ``[status still reports CONFIGURED]`` on exactly that, and the operator saw a healthy
        tenant whose credential could not be read.

        The semantics changed rather than a second field being added, because every consumer
        is asking the usable question: `admin_server:619` renders it as ``llm_configured``,
        and `_tenant.py:65` gates on it. A caller wanting the distinction has
        ``api_key_unreadable``, which separates *no credential* from *a credential we cannot
        decrypt* -- the second is an operational emergency and the first is not.

        Costs one unwrap per call. Status is not a hot path, and the alternative is reporting
        a number that is not true.
        """
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        row = self._connection.execute(
            """
            SELECT tenant_id, provider, base_url, model, api_key_sealed,
                   embedding_provider, embedding_base_url, embedding_model,
                   created_at, updated_at
            FROM tenant_llm_credentials WHERE tenant_id = ?
            """,
            (normalized_tenant,),
        ).fetchone()
        if row is None:
            return None
        readable = self._llm_credential_is_readable(normalized_tenant, str(row["api_key_sealed"]))
        shredded = False if readable else self._llm_credential_is_shredded(normalized_tenant)
        return {
            "tenant_id": str(row["tenant_id"]),
            "provider": str(row["provider"]),
            "has_api_key": readable,
            # UNREADABLE excludes a deliberate shred: page on the first, not the second.
            "api_key_unreadable": not readable and not shredded,
            "api_key_shredded": shredded,
            "base_url": str(row["base_url"]),
            "model": str(row["model"]),
            "embedding_provider": str(row["embedding_provider"]),
            "embedding_base_url": str(row["embedding_base_url"]),
            "embedding_model": str(row["embedding_model"]),
            "created_at": str(row["created_at"]),
            "updated_at": str(row["updated_at"]),
        }

    def clear_tenant_llm_credentials(self, tenant_id: str) -> bool:
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        cursor = self._connection.execute(
            "DELETE FROM tenant_llm_credentials WHERE tenant_id = ?",
            (normalized_tenant,),
        )
        self._commit()
        return cursor.rowcount > 0

    def purge_tenant_state(
        self,
        tenant_id: str,
        *,
        agent_ids: Iterable[str] = (),
        preserve_receipts: bool = False,
    ) -> dict[str, Any]:
        """Reset generated state for a hosted tenant without deleting raw episodes.

        With ``preserve_receipts=True`` the tenant's ``memory_receipts`` and
        ``run_checkpoints`` rows are left untouched. Tenant migration uses this
        so a move never has to delete a receipt chain and re-insert it: the
        source's audit history is simply never removed, which leaves no window
        in which a crash could lose it. Defaults to False, so the admin
        "Purge generated state" action keeps its existing behaviour.
        """
        normalized_tenant = self._normalize_tenant_id(tenant_id)
        normalized_agents = {
            self._normalize_agent_id(agent_id)
            for agent_id in agent_ids
            if agent_id is not None and str(agent_id).strip()
        }
        normalized_agents.update(str(agent["agent_id"]) for agent in self.tenant_agents(normalized_tenant))
        ordered_agents = tuple(sorted(normalized_agents))
        scope_keys = tuple(
            dict.fromkeys(
                (
                    f"{ScopeKind.TENANT.value}:{normalized_tenant}",
                    *(f"{ScopeKind.AGENT.value}:{agent_id}" for agent_id in ordered_agents),
                )
            )
        )
        scope_key_set = set(scope_keys)

        episode_uuids = {episode.uuid for episode in self.episodes() if episode.scope.key in scope_key_set}
        # Graph-plane reads and deletions route through the Memory Graph
        # surface so a split deployment purges the right store (cross-store
        # rule: the purge anchors here; the graph deletions are idempotent).
        node_uuids: set[str] = {
            node.uuid for node in self._memory_graph.nodes() if node.properties.get("scope_key") in scope_key_set
        }
        relationship_uuids: set[str] = {
            relationship.uuid
            for relationship in self._memory_graph.relationships()
            if relationship.properties.get("scope_key") in scope_key_set
            or relationship.source_uuid in node_uuids
            or relationship.target_uuid in node_uuids
        }

        decision_uuids: set[str] = set()
        for row in self._connection.execute("SELECT uuid, payload_json FROM dream_decisions").fetchall():
            record = DreamDecisionRecord.model_validate_json(str(row["payload_json"]))
            if (
                record.agent_scope.key in scope_key_set
                or (record.scope is not None and record.scope.key in scope_key_set)
                or record.agent_id in normalized_agents
            ):
                decision_uuids.add(str(row["uuid"]))

        counts: dict[str, Any] = {
            "tenant_id": normalized_tenant,
            "agent_ids": list(ordered_agents),
            "scope_keys": list(scope_keys),
            "raw_episodes_preserved": len(episode_uuids),
            "credentials_preserved": self.tenant_llm_credential_state(normalized_tenant) is not None,
        }
        # One logical purge, one commit (parity with the Postgres plane and
        # the RTBF guarantee): every operational delete below commits together
        # or not at all.  Graph-plane deletes routed to a *different* engine in
        # split mode remain independently durable per the cross-store rule.
        with self.transaction():
            counts["processed_episodes_reset"] = self._delete_by_ids(
                "processed_episodes",
                "episode_uuid",
                episode_uuids,
            )
            counts["episode_processing_reset"] = self._delete_by_ids(
                "episode_processing",
                "episode_uuid",
                episode_uuids,
            )
            # Utility observations are derived runtime state and carry foreign keys
            # into the relationship graph. Delete children before their parents.
            counts["memory_outcome_events_deleted"] = self._delete_by_scope_keys(
                "memory_outcome_events",
                scope_keys,
            )
            counts["memory_use_events_deleted"] = self._delete_by_scope_keys(
                "memory_use_events",
                scope_keys,
            )
            counts["memory_prune_ghosts_deleted"] = self._delete_by_scope_keys(
                "memory_prune_ghosts",
                scope_keys,
            )
            counts["quarantined_candidates_deleted"] = self._delete_by_scope_keys(
                "quarantined_candidates",
                scope_keys,
            )
            counts["relationships_deleted"] = self._memory_graph.delete_relationships(relationship_uuids)
            counts["nodes_deleted"] = self._memory_graph.delete_nodes(node_uuids)
            counts["dream_decisions_deleted"] = self._delete_by_ids(
                "dream_decisions",
                "uuid",
                decision_uuids,
            )
            counts["memory_receipts_deleted"] = (
                0 if preserve_receipts else self._delete_by_scope_keys("memory_receipts", scope_keys)
            )
            counts["run_checkpoints_deleted"] = (
                0 if preserve_receipts else self._delete_by_scope_keys("run_checkpoints", scope_keys)
            )
            counts["dream_job_runs_deleted"] = self._connection.execute("DELETE FROM dream_job_runs").rowcount
            counts["job_state_deleted"] = self._connection.execute("DELETE FROM job_state").rowcount
            counts["agent_motive_assignments_deleted"] = self._connection.execute(
                "DELETE FROM agent_motive_assignments WHERE tenant_id = ?",
                (normalized_tenant,),
            ).rowcount
            counts["tenant_agents_deleted"] = self._connection.execute(
                "DELETE FROM tenant_agents WHERE tenant_id = ?",
                (normalized_tenant,),
            ).rowcount
            counts["tenant_prompt_overrides_deleted"] = self._connection.execute(
                "DELETE FROM tenant_prompt_overrides WHERE tenant_id = ?",
                (normalized_tenant,),
            ).rowcount
            counts["tenant_prompt_versions_deleted"] = self._connection.execute(
                "DELETE FROM tenant_prompt_versions WHERE tenant_id = ?",
                (normalized_tenant,),
            ).rowcount
            counts["project_memory_config_versions_deleted"] = self._connection.execute(
                "DELETE FROM project_memory_config_versions WHERE tenant_id = ?",
                (normalized_tenant,),
            ).rowcount
            self._commit()
        return counts

    def reveal(self, scope_key: str, value: Any) -> Any:
        """WS-12 decrypt-on-read: resolve a stored content field for a read surface.

        Plaintext passes through; sealed content opens under the scope's live DEK
        and resolves to the shredded placeholder after crypto-shred.
        """
        from memotron.crypto import is_sealed_content

        if not is_sealed_content(value):
            return value
        return reveal_content(value, self.get_governance_key(scope_key))

    def reveal_vector(self, scope_key: str, value: Any) -> list[float] | None:
        """WS-12 decrypt-on-read for stored embedding vectors.

        Plaintext lists pass through; sealed vectors open under a live DEK and
        resolve to None (skip) once the scope is shredded.
        """
        if isinstance(value, list):
            return value or None
        from memotron.crypto import is_sealed_content

        if not is_sealed_content(value):
            return None
        revealed = reveal_json(value, self.get_governance_key(scope_key))
        return revealed if isinstance(revealed, list) and revealed else None

    def _delete_by_ids(self, table: str, column: str, values: Iterable[str]) -> int:
        ordered_values = tuple(sorted({str(value) for value in values if str(value).strip()}))
        if not ordered_values:
            return 0
        cursor = self._connection.execute(
            self._in_clause_sql(
                f"DELETE FROM {table} WHERE {column} IN ({{placeholders}})",
                ordered_values,
            ),
            ordered_values,
        )
        return cursor.rowcount

    def _delete_by_scope_keys(self, table: str, scope_keys: Iterable[str]) -> int:
        ordered_scope_keys = tuple(sorted({str(scope_key) for scope_key in scope_keys if str(scope_key).strip()}))
        if not ordered_scope_keys:
            return 0
        cursor = self._connection.execute(
            self._in_clause_sql(
                f"DELETE FROM {table} WHERE scope_key IN ({{placeholders}})",
                ordered_scope_keys,
            ),
            ordered_scope_keys,
        )
        return cursor.rowcount
